import pytest
from app.config import SAMPLES
from app.modules.m8_inventory import service, store as inv_store

HEADER = 'id,kind,segment,seg_start,seg_end,channel,time,ticket,note,count,amount,ref\n'


def seg(line, name, start, end, channel='官方'):
    return f"S{line},segment,{name},{start},{end},{channel},,,,,,\n"


def prt(line, segment, ticket, note, ref, channel='官方', amount='100',
        t='2026-11-17T10:06:00+08:00'):
    return f"R{line},print,{segment},,,{channel},{t},{ticket},{note},,{amount},{ref}\n"


def test_sample_stocktake_balances(isolate):
    result = service.take_stock((SAMPLES / 'inventory.csv').read_bytes())
    assert result['metrics']['号段'] == 3
    assert result['metrics']['已售'] == 6
    assert result['metrics']['待核实'] == 2
    assert result['metrics']['已占用'] == 2
    assert result['metrics']['可发放'] == 50
    # 号段内票号守恒：每张票纸恰处于一种状态
    for seg_view in result['segments']:
        assert (seg_view['sold'] + seg_view['pending'] + seg_view['allocated']
                + seg_view['available']) == seg_view['total']


def test_same_stocktake_is_idempotent(isolate):
    raw = (SAMPLES / 'inventory.csv').read_bytes()
    first = service.take_stock(raw)
    second = service.take_stock(raw)
    assert first['manifest']['run_id'] == second['manifest']['run_id']
    assert first['metrics'] == second['metrics']
    with inv_store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM inv_numbers').fetchone()[0] == 60


def test_late_receipt_updates_occupancy_and_reconcile_together(isolate):
    service.take_stock((SAMPLES / 'inventory.csv').read_bytes())
    before = service.latest()
    official = next(c for c in before['channel_inv'] if c['channel'] == '官方')
    assert official['pending_amount'] == '100' and official['paper_sold'] == '300'
    after = service.take_stock((SAMPLES / 'inventory_late.csv').read_bytes())
    led = {e['ticket']: e for e in after['ledger']}
    assert led['00000010']['state'] == 'sold'
    official = next(c for c in after['channel_inv'] if c['channel'] == '官方')
    assert official['pending_amount'] == '0' and official['paper_sold'] == '400'
    assert official['delta'] == '100'
    # 同一补回执批次重跑不重复确认、不重复占用
    again = service.take_stock((SAMPLES / 'inventory_late.csv').read_bytes())
    assert again['metrics'] == after['metrics']


def test_late_receipt_keeps_same_printer_ref(isolate):
    # 补回执是同一条打印记录晚到：ref 不变即可重新评估，不应当作重复提交
    service.take_stock((SAMPLES / 'inventory.csv').read_bytes())
    after = service.take_stock((SAMPLES / 'inventory_late.csv').read_bytes())
    dup = [q for q in after['quarantine'] if q['code'] == 'DUPLICATE']
    assert dup == []
    led = {e['ticket']: e for e in after['ledger']}
    assert led['00000010']['ref'] == 'PRT-00000010'
    assert led['00000024']['ref'] == 'PRT-00000024'


def test_same_ref_other_ticket_is_duplicate(isolate):
    service.take_stock((SAMPLES / 'inventory.csv').read_bytes())
    evil = (HEADER + 'X,print,官方票纸-A卷,,,官方,2026-11-17T19:00:00+08:00,'
            '00000011,回执,,100,PRT-00000001\n').encode()
    out = service.take_stock(evil)
    assert any(q['code'] == 'DUPLICATE' for q in out['quarantine'])
    led = {e['ticket']: e for e in out['ledger']}
    assert led['00000011']['state'] == 'available'


def test_confirmed_ticket_never_back_to_available(isolate):
    service.take_stock((SAMPLES / 'inventory.csv').read_bytes())
    after = service.take_stock((SAMPLES / 'inventory_late.csv').read_bytes())
    led = {e['ticket']: e for e in after['ledger']}
    assert led['00000001']['state'] == 'sold'
    assert any(q['code'] == 'INV_REVERSE' for q in after['quarantine'])
    # 数据库层同样守住单调性：直接 UPDATE 降级被触发器拒绝
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with inv_store.connect() as db:
            db.execute("UPDATE inv_numbers SET state='available' WHERE ticket='00000001'")
    with inv_store.connect() as db:
        kept = db.execute("SELECT state FROM inv_numbers WHERE ticket='00000001'").fetchone()[0]
    assert kept == 'sold'


def test_receipt_before_electronic_issue_waits_then_confirms(isolate):
    first = HEADER + seg(2, 'A卷', 1, 2) + prt(3, 'A卷', '1', '回执', 'EARLY',
                                              t='2026-11-17T09:00:00+08:00')
    r1 = service.take_stock(first.encode())
    assert {e['ticket']: e for e in r1['ledger']}['1']['state'] == 'pending'
    second = HEADER + seg(2, 'A卷', 1, 2) + prt(3, 'A卷', '1', '回执', 'EARLY',
                                               t='2026-11-17T10:30:00+08:00')
    r2 = service.take_stock(second.encode())
    assert {e['ticket']: e for e in r2['ledger']}['1']['state'] == 'sold'


def test_http_module_and_page(client):
    reply = client.get('/api/m8')
    assert reply.status_code == 200
    assert reply.json['metrics']['号段'] == 3
    assert client.get('/m/8').status_code == 200
    assert client.get('/api/m8/history').status_code == 200
    evidence = client.get('/api/m8/evidence.csv')
    assert evidence.status_code == 200 and '盘存批次' in evidence.text
    missing = client.get('/api/m8?run=NOPE')
    assert missing.status_code == 404


def test_http_idempotent_post(client):
    text = (HEADER + seg(2, 'X卷', 1, 3)).encode()
    a = client.post('/api/m8', json={'text': text.decode()}).json
    b = client.post('/api/m8', json={'text': text.decode()}).json
    assert a['manifest']['run_id'] == b['manifest']['run_id']
