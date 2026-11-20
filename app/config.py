from pathlib import Path
TITLE = '票务占座过期与出票一致性'
SHORT = 'seat-expiry'
STAGES = ['receive', 'seats', 'leases', 'payments', 'tickets', 'reconcile', 'replay']
MODULES = ['渠道接收', '座席映射', '租约回放', '支付归属', '出票核验', '渠道对账', '补传复核', '票号盘存']
COLUMNS = {'id': '事件号', 'channel': '渠道', 'seat': '座席', 'order': '订单', 'time': '时刻 UTC', 'type': '事件', 'expires': '到期 UTC', 'version': '版本', 'state': '状态', 'payment': '支付状态', 'ticket': '票号', 'reason': '原因', 'code': '原因码', 'line': '行号', 'count': '数量', 'lease': '租约号', 'amount': '金额 CNY', 'key': '业务键', 'detail': '依据', 'late_minutes': '迟到 min'}
INV_COLUMNS = {'kind': '记录类型', 'segment': '号段', 'seg_start': '起号', 'seg_end': '止号', 'ref': '业务参考', 'note': '回执状态', 'sold': '已售', 'pending': '待核实', 'allocated': '已占用', 'available': '可发放', 'total': '票纸数', 'ranges': '连号区间', 'paper_sold': '纸票确认 CNY', 'paper_tickets': '确认票', 'e_ticketed': '电子出票 CNY', 'e_tickets': '电子票', 'pending_amount': '待核实 CNY', 'delta': '悬单 CNY', 'source': '证据来源', 'updated_run': '盘存批次'}
DEFAULTS = {'observation_end': '2026-11-18T00:00:00+08:00', 'late_minutes': '10'}
RULE_VERSION = "2026.11.18-v1"
INV_RULE_VERSION = "2026.11.20-inv-v1"
SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"
MAX_BYTES = 4 * 1024 * 1024

COLUMNS.update({'start':'占座起 UTC','end':'租约止 UTC','paid':'支付 CNY','ticketed':'出票 CNY','delta':'悬单 CNY','payments':'支付笔','tickets':'出票笔'})

# 票号盘存：状态只允许沿此阶梯上行，已确认票号不能重新变成可发放。
TICKET_STATES = ('available', 'allocated', 'pending', 'sold')
STATE_RANK = {name: i for i, name in enumerate(TICKET_STATES)}
STATE_LABEL = {'available': '可发放', 'allocated': '已占用', 'pending': '待核实', 'sold': '已售'}
INV_KINDS = ('segment', 'print', 'allocate')
PRINT_NOTES = ('回执', '缺报')
