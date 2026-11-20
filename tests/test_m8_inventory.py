import pytest
from decimal import Decimal as D
from app.config import SAMPLES
from app.normalize import rows_from
from app.pipeline import run as replay_run
from app.modules.m8_inventory import rules, service, store as inv_store

HEADER = 'id,kind,segment,seg_start,seg_end,channel,time,ticket,note,count,amount,ref\n'


def seg(line, name, start, end, channel='官方'):
    return f"S{line},segment,{name},{start},{end},{channel},,,,,,\n"


def prt(line, segment, ticket, note, ref, channel='官方', amount='100',
        t='2026-11-17T10:06:00+08:00'):
    return f"R{line},print,{segment},,,{channel},{t},{ticket},{note},,{amount},{ref}\n"


def alc(line, segment, count, ref, channel='官方', amount='100'):
    return f"A{line},allocate,{segment},,,{channel},,,,{count},{amount},{ref}\n"


def replay():
    return replay_run((SAMPLES / 'dirty.csv').read_bytes(), persist=False)


def state_with(csv_text):
    parsed = rows_from(csv_text.encode('utf-8'), rules.INV_REQUIRED)
    ops, bad = rules.parse_ops(parsed['raw'])
    state = rules.new_state()
    state['quarantine'] = list(parsed['quarantine']) + bad
    rules.apply_ops(state, ops, replay(), 'INVTEST')
    return state


def test_segment_registers_consecutive_numbers():
    state = state_with(HEADER + seg(2, 'A卷', 1, 5))
    assert len(state['numbers']) == 5
    assert state['segments']['A卷']['start'] == '1'
    view = rules.segment_view(state)[0]
    assert view['available'] == 5 and view['sold'] == 0
    assert view['ranges'] == '1-5'


def test_missing_receipt_freezes_pending_not_reusable():
    state = state_with(HEADER + seg(2, 'A卷', 1, 3) + prt(3, 'A卷', '1', '缺报', 'P1'))
    assert state['numbers']['1']['state'] == 'pending'
    view = rules.segment_view(state)[0]
    assert view['pending'] == 1 and view['available'] == 2


def test_receipt_confirms_sold():
    state = state_with(HEADER + seg(2, 'A卷', 1, 3) + prt(3, 'A卷', '1', '回执', 'P1'))
    assert state['numbers']['1']['state'] == 'sold'


def test_confirmed_ticket_cannot_revert():
    state = state_with(HEADER + seg(2, 'A卷', 1, 3) + prt(3, 'A卷', '1', '回执', 'P1')
                       + prt(4, 'A卷', '1', '缺报', 'P2'))
    assert state['numbers']['1']['state'] == 'sold'
    assert any(q['code'] == 'INV_REVERSE' for q in state['quarantine'])


def test_allocate_takes_lowest_available_and_is_ref_idempotent():
    state = state_with(HEADER + seg(2, 'A卷', 1, 5) + alc(3, 'A卷', 2, 'BOX-1')
                       + alc(4, 'A卷', 2, 'BOX-1'))
    taken = [n for n, e in state['numbers'].items() if e['state'] == 'allocated']
    assert taken == ['1', '2']  # 同参考重跑不重复占用
    exhausted = state_with(HEADER + seg(2, 'A卷', 1, 5) + alc(3, 'A卷', 9, 'BOX-2'))
    assert any(q['code'] == 'INV_EXHAUSTED' for q in exhausted['quarantine'])
    assert all(e['state'] == 'available' for e in exhausted['numbers'].values())


def test_sold_number_never_reallocated():
    state = state_with(HEADER + seg(2, 'A卷', 1, 4) + prt(3, 'A卷', '1', '回执', 'P1')
                       + alc(4, 'A卷', 2, 'BOX-1'))
    taken = sorted(n for n, e in state['numbers'].items() if e['state'] == 'allocated')
    assert taken == ['2', '3']
    assert state['numbers']['1']['state'] == 'sold'


def test_cross_channel_and_orphan_print_quarantined():
    state = state_with(HEADER + seg(2, 'A卷', 1, 3, '官方')
                       + prt(3, 'A卷', '1', '回执', 'P1', channel='票社')
                       + prt(4, 'A卷', '99', '回执', 'P2'))
    codes = {q['code'] for q in state['quarantine']}
    assert 'CONFLICT' in codes and 'INV_NUMBER' in codes
    assert state['numbers']['1']['state'] == 'available'


def test_duplicate_print_ref_rejected():
    state = state_with(HEADER + seg(2, 'A卷', 1, 3) + prt(3, 'A卷', '1', '回执', 'DUP')
                       + prt(4, 'A卷', '2', '回执', 'DUP'))
    assert any(q['code'] == 'DUPLICATE' for q in state['quarantine'])
    assert state['numbers']['2']['state'] == 'available'


def test_same_ref_same_ticket_can_be_reevaluated():
    # 补回执：同一打印记录 ref 不变、票号不变，允许从缺报/待核实升级为已售
    first = state_with(HEADER + seg(2, 'A卷', 1, 2) + prt(3, 'A卷', '1', '缺报', 'PRT-1'))
    assert first['numbers']['1']['state'] == 'pending'
    second = state_with(HEADER + seg(2, 'A卷', 1, 2)
                        + prt(3, 'A卷', '1', '缺报', 'PRT-1')
                        + prt(4, 'A卷', '1', '回执', 'PRT-1',
                              t='2026-11-17T10:40:00+08:00'))
    assert not any(q['code'] == 'DUPLICATE' for q in second['quarantine'])
    assert second['numbers']['1']['state'] == 'sold'


def test_overlapping_segment_rejected():
    state = state_with(HEADER + seg(2, 'A卷', 1, 5) + seg(3, 'B卷', 4, 8))
    assert any(q['code'] == 'INV_TAKEN' for q in state['quarantine'])
    assert 'B卷' not in state['segments']


def test_reconcile_pending_amount_tracked_separately():
    payments = [{'channel': '官方', 'amount': D(100)}]
    state = rules.new_state()
    state['numbers'] = {
        '1': {'ticket': '1', 'segment': 'A', 'channel': '官方', 'state': 'sold', 'amount': '100'},
        '2': {'ticket': '2', 'segment': 'A', 'channel': '官方', 'state': 'pending', 'amount': '100'},
    }
    row = rules.reconcile(payments, [], state)[0]
    assert row['paper_sold'] == '100'
    assert row['pending_amount'] == '100'
    assert row['delta'] == '0'
    assert row['state'] == '待核实'
