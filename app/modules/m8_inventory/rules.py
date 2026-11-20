from decimal import Decimal as D
from app.config import STATE_RANK, INV_KINDS, PRINT_NOTES, STATE_LABEL
from app.errors import DomainError
from app.normalize import number, instant
from app.reasons import rejected
INV_REQUIRED = ['id', 'kind', 'segment', 'seg_start', 'seg_end', 'channel', 'time',
                'ticket', 'note', 'count', 'amount', 'ref']


def new_state():
    return {'segments': {}, 'numbers': {}, 'refs': set(), 'quarantine': []}


def ticket_text(value, width):
    value = number(value)
    if value < 0 or value != value.to_integral():
        raise DomainError('NUMBER', '票号必须为非负整数')
    return str(int(value)).zfill(width)


def _seg_width(row):
    try:
        return max(len(str(row['seg_start']).strip()), len(str(row['seg_end']).strip()))
    except (KeyError, TypeError):
        return 1


def parse_ops(rows):
    """把CSV行解析成强类型盘存操作；结构性错误逐行隔离，不中断整批。"""
    ops, bad = [], []
    for row in rows:
        try:
            kind = row['kind'].strip()
            if kind not in INV_KINDS:
                raise DomainError('INV_KIND', '盘存记录类型不支持')
            op = {'_line': row['_line'], 'id': row['id'].strip(), 'kind': kind,
                  'channel': row['channel'].strip(), 'ref': row['ref'].strip(),
                  'segment': row['segment'].strip()}
            if not op['id'] or not op['channel']:
                raise DomainError('MISSING', '事件号或渠道缺失')
            if op['channel'] not in ('官方', '票社', '团购'):
                raise DomainError('UNMAPPED', '渠道未登记')
            if kind == 'segment':
                width = _seg_width(row)
                start, end = ticket_text(row['seg_start'], 1), ticket_text(row['seg_end'], 1)
                start = start.zfill(width); end = end.zfill(width)
                if int(end) < int(start):
                    raise DomainError('RANGE', '号段止号早于起号')
                op.update({'start': start, 'end': end})
            else:
                if not op['segment'] or not op['ref']:
                    raise DomainError('MISSING', '号段或业务参考缺失')
                if kind == 'print':
                    op['time'] = instant(row['time'])
                    if row['note'].strip() not in PRINT_NOTES:
                        raise DomainError('STATE', '回执状态只能是回执或缺报')
                    op['note'] = row['note'].strip()
                    # 票号保留原始字符串（含前导零），规范化到号段宽度在 _canonical_ticket 完成
                    op['ticket'] = row['ticket'].strip()
                    if not op['ticket']:
                        raise DomainError('MISSING', '打印记录缺少票号')
                    op['amount'] = number(row['amount'])
                    if op['amount'] < 0:
                        raise DomainError('NEGATIVE', '票款不能为负')
                else:
                    count = number(row['count'])
                    if count < 1 or count != count.to_integral():
                        raise DomainError('RANGE', '申领数量必须为正整数')
                    op['count'] = int(count)
                    op['amount'] = number(row['amount'])
                    if op['amount'] < 0:
                        raise DomainError('NEGATIVE', '票款不能为负')
        except DomainError as exc:
            bad.append(rejected(row, exc.code, exc.message))
            continue
        ops.append(op)
    return ops, bad


def _register_numbers(state, name, channel, start, end, line):
    lo, hi = int(start), int(end)
    width = max(len(start), len(end))
    for value in range(lo, hi + 1):
        ticket = str(value).zfill(width)
        if ticket in state['numbers']:
            owner = state['numbers'][ticket]['segment']
            raise DomainError('INV_TAKEN', f'票号 {ticket} 已属号段 {owner}')
        state['numbers'][ticket] = {'ticket': ticket, 'segment': name, 'channel': channel,
                                    'state': 'available', 'source': '', 'updated_run': '',
                                    'line': line, 'amount': None}
    state['segments'][name] = {'segment': name, 'channel': channel, 'start': start, 'end': end,
                               'line': line}


def apply_segment(state, op, run_id):
    name = op['segment']
    if name in state['segments']:
        old = state['segments'][name]
        if (old['start'], old['end'], old['channel']) == (op['start'], op['end'], op['channel']):
            return  # 同号段同范围重报：幂等忽略
        state['quarantine'].append(rejected(op, 'INV_SEGMENT', '同号段重复登记且范围或渠道不一致'))
        return
    # 连号全场唯一：任何渠道的号段重叠或票号撞号都拒绝，避免临售重发撞上已印票号
    for other in state['segments'].values():
        if not (int(op['end']) < int(other['start']) or int(op['start']) > int(other['end'])):
            state['quarantine'].append(rejected(op, 'INV_TAKEN', f"与号段 {other['segment']} 连号重叠"))
            return
    try:
        _register_numbers(state, name, op['channel'], op['start'], op['end'], op['_line'])
    except DomainError as exc:
        state['quarantine'].append(rejected(op, exc.code, exc.message))


def _canonical_ticket(state, op):
    seg = state['segments'].get(op['segment'])
    if seg is None:
        raise DomainError('INV_SEGMENT', f"号段 {op['segment']} 未登记")
    width = max(len(seg['start']), len(seg['end']))
    digits = number(op['ticket'])
    if digits < 0 or digits != digits.to_integral():
        raise DomainError('NUMBER', '票号必须为非负整数')
    ticket = str(int(digits)).zfill(width)
    if not (int(seg['start']) <= int(ticket) <= int(seg['end'])):
        raise DomainError('INV_NUMBER', f"票号 {ticket} 不在号段 {op['segment']} 范围内")
    return ticket


def _find_number(state, op):
    op['ticket'] = _canonical_ticket(state, op)
    entry = state['numbers'].get(op['ticket'])
    if entry is None:
        raise DomainError('INV_NUMBER', f"票号 {op['ticket']} 不属于任何已发号段")
    if entry['segment'] != op['segment']:
        raise DomainError('INV_NUMBER', f"票号 {op['ticket']} 不属于号段 {op['segment']}")
    if entry['channel'] != op['channel']:
        raise DomainError('CONFLICT', '打印渠道与号段渠道不符')
    return entry


def _promote(state, entry, target, op, source, run_id):
    """状态阶梯只升不降；已确认票号不能重新变成可发放。"""
    current = entry['state']
    if STATE_RANK[target] < STATE_RANK[current]:
        state['quarantine'].append(rejected(
            op, 'INV_REVERSE', f"票号 {entry['ticket']} 已为{STATE_LABEL[current]}，不能回退到{STATE_LABEL[target]}"))
        return False
    if current != target or entry['source'] != source:
        entry['state'] = target
        entry['source'] = source
        entry['updated_run'] = run_id
        entry['line'] = op['_line']
    return True


def _as_instant(value):
    if hasattr(value, 'year'):
        return value
    return instant(value)


def apply_print(state, op, replay, run_id):
    # 同一打印记录补回执时 ref 不变、票号不变：允许重新评估（无变化即幂等）；
    # 同一 ref 指向另一张票号才是重复提交冲突。
    if op['ref'] in state['refs']:
        known = next((e for e in state['numbers'].values()
                      if e.get('ref') == op['ref']), None)
        try:
            canonical = _canonical_ticket(state, op)
        except DomainError as exc:
            state['quarantine'].append(rejected(op, exc.code, exc.message))
            return
        if known is None or known['ticket'] != canonical:
            state['quarantine'].append(rejected(op, 'DUPLICATE', '同一打印回执重复提交且票号不一致'))
            return
    try:
        entry = _find_number(state, op)
    except DomainError as exc:
        state['quarantine'].append(rejected(op, exc.code, exc.message))
        return
    state['refs'].add(op['ref'])
    entry['ref'] = op['ref']
    if entry['state'] == 'sold':
        # 已确认票号不能回退：重复回执幂等放行，缺报重报只留证据不改状态
        if op['note'] != '回执':
            state['quarantine'].append(rejected(
                op, 'INV_REVERSE', f"票号 {entry['ticket']} 已确认，缺报重报不能改回待核实"))
        return
    # 纸票按号段连号编号，电子出票走事件票号，两套编号不通用；
    # 按渠道+金额+打印时点交叉核对，一张电子出票只能核销一张纸票。
    issued = next((t for t in replay['tickets']
                   if t['channel'] == op['channel']
                   and D(str(t['amount'])) == op['amount']
                   and _as_instant(t['time']) <= op['time']
                   and not t.get('_paper_matched')), None)
    if issued is None:
        if op['note'] == '回执':
            # 回执先到、渠道电子出票晚到：暂挂待核实，补传后再次盘存即可确认
            _promote(state, entry, 'pending', op, '回执待电子出票核对', run_id)
            entry['amount'] = str(op['amount'])
            return
        _promote(state, entry, 'pending', op, '缺报待核实', run_id)
        entry['amount'] = str(op['amount'])
        return
    if op['note'] != '回执':
        # 机器已出纸但回执丢失：冻结在待核实，绝不按未用重发
        if not _promote(state, entry, 'pending', op, '缺报待核实', run_id):
            return
        entry['amount'] = str(op['amount'])
        return
    if not _promote(state, entry, 'sold', op, '打印回执', run_id):
        return
    issued['_paper_matched'] = True
    entry['amount'] = str(op['amount'])


def apply_allocate(state, op, run_id):
    if op['ref'] in state['refs']:
        state['quarantine'].append(rejected(op, 'DUPLICATE', '同一临售申领重复提交'))
        return
    seg = state['segments'].get(op['segment'])
    if seg is None or seg['channel'] != op['channel']:
        state['quarantine'].append(rejected(op, 'INV_SEGMENT', '号段未登记或渠道不符'))
        return
    pool = sorted((e for e in state['numbers'].values()
                   if e['segment'] == op['segment'] and e['state'] == 'available'),
                  key=lambda e: int(e['ticket']))
    if len(pool) < op['count']:
        state['quarantine'].append(rejected(
            op, 'INV_EXHAUSTED', f"号段可发票纸不足：需 {op['count']} 张，余 {len(pool)} 张"))
        return
    chosen = pool[:op['count']]
    state['refs'].add(op['ref'])
    for entry in chosen:
        _promote(state, entry, 'allocated', op, '临售发放', run_id)
        entry['amount'] = str(op['amount']) if op['amount'] > 0 else entry['amount']


def apply_ops(state, ops, replay, run_id):
    """号段先建档；打印按出纸时点排序以正确核销电子出票，申领按原始行序处理。"""
    segments = [op for op in ops if op['kind'] == 'segment']
    prints = sorted((op for op in ops if op['kind'] == 'print'),
                    key=lambda x: (x['time'], x['_line']))
    allocates = sorted((op for op in ops if op['kind'] == 'allocate'),
                       key=lambda x: x['_line'])
    for op in segments:
        apply_segment(state, op, run_id)
    for op in prints:
        apply_print(state, op, replay, run_id)
    for op in allocates:
        apply_allocate(state, op, run_id)
    return state


def _ranges(tickets):
    if not tickets:
        return ''
    nums = sorted(int(t) for t in tickets)
    parts = []
    start = prev = nums[0]
    width = max(len(t) for t in tickets)
    for value in nums[1:]:
        if value == prev + 1:
            prev = value
            continue
        parts.append((str(start).zfill(width), str(prev).zfill(width)))
        start = prev = value
    parts.append((str(start).zfill(width), str(prev).zfill(width)))
    return ';'.join(a if a == b else f'{a}-{b}' for a, b in parts)


def segment_view(state):
    rows = []
    for seg in sorted(state['segments'].values(), key=lambda s: (s['channel'], int(s['start']))):
        members = [e for e in state['numbers'].values() if e['segment'] == seg['segment']]
        count_by = {name: 0 for name in STATE_RANK}
        for entry in members:
            count_by[entry['state']] += 1
        rows.append({
            'segment': seg['segment'], 'channel': seg['channel'],
            'seg_start': seg['start'], 'seg_end': seg['end'],
            'total': len(members), 'sold': count_by['sold'], 'pending': count_by['pending'],
            'allocated': count_by['allocated'], 'available': count_by['available'],
            'state': '已发完' if count_by['available'] == 0 else '可继续发放',
            'ranges': _ranges([e['ticket'] for e in members if e['state'] == 'available']) or '无',
        })
    return rows


def number_ledger(state):
    return [dict(e) for e in sorted(state['numbers'].values(), key=lambda e: (e['segment'], int(e['ticket'])))]


def reconcile(payments, tickets, state):
    """票款对账：有效支付 - 已确认纸票；待核实金额单列，补回执后随占用一起更新。"""
    def money(value):
        return value if isinstance(value, D) else D(str(value))

    channels = {}

    def row_for(channel):
        return channels.setdefault(channel, {'channel': channel, 'paid': D(0), 'payments': 0,
                                             'e_ticketed': D(0), 'e_tickets': 0,
                                             'paper_sold': D(0), 'paper_tickets': 0,
                                             'pending_amount': D(0)})

    for pay in payments:
        row = row_for(pay['channel'])
        row['paid'] += money(pay['amount']); row['payments'] += 1
    for tick in tickets:
        row = row_for(tick['channel'])
        row['e_ticketed'] += money(tick['amount']); row['e_tickets'] += 1
    for entry in state['numbers'].values():
        row = row_for(entry['channel'])
        if entry['state'] == 'sold':
            row['paper_sold'] += D(entry['amount']); row['paper_tickets'] += 1
        elif entry['state'] == 'pending':
            row['pending_amount'] += D(entry['amount'])
    result = []
    for row in sorted(channels.values(), key=lambda r: r['channel']):
        row['delta'] = row['paid'] - row['paper_sold']
        row['state'] = '通过' if row['delta'] == 0 and row['pending_amount'] == 0 else '待核实'
        for key in ('paid', 'e_ticketed', 'paper_sold', 'pending_amount', 'delta'):
            row[key] = str(row[key])
        result.append(row)
    return result
