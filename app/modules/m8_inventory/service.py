import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.config import INV_RULE_VERSION, SAMPLES
from app.errors import DomainError
from app.normalize import rows_from, serial
from app.pipeline import run as replay_run
from . import rules, store as inv_store


def _run_id(raw, params):
    digest = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256(
        (digest + json.dumps(params, sort_keys=True, ensure_ascii=False) + INV_RULE_VERSION).encode()
    ).hexdigest()
    return 'INV' + key[:17], digest


def _replay(raw):
    if raw is None:
        return replay_run((SAMPLES / 'dirty.csv').read_bytes(), persist=False)
    return replay_run(raw, persist=False)


def take_stock(raw=None, params=None, replay_bytes=None):
    """执行一次票号盘存。

    raw：盘存CSV（号段/打印/申领）；replay_bytes：M1-M7渠道事件，用于电子出票交叉核对。
    同一盘存内容重跑复用批次且不重复占用；票号状态单调，已确认票号不能回退。
    """
    if raw is None:
        raw = (SAMPLES / 'inventory.csv').read_bytes()
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if not isinstance(params, (dict, type(None))):
        raise DomainError('PARAMS', '参数必须是对象')
    params = params or {}
    run_id, digest = _run_id(raw, params)
    created = datetime.now(timezone.utc).isoformat()

    db = inv_store.connect()
    try:
        if inv_store.run_exists(db, run_id):
            return inv_store.fetch(run_id)
        replay = _replay(replay_bytes)
        parsed = rows_from(raw, rules.INV_REQUIRED)
        ops, parse_bad = rules.parse_ops(parsed['raw'])
        state = inv_store.load_state(db)
        state['quarantine'] = list(parsed['quarantine']) + parse_bad
        rules.apply_ops(state, ops, replay, run_id)

        snapshot = {
            'manifest': {
                'run_id': run_id, 'sha256': digest, 'rules': INV_RULE_VERSION,
                'params': params, 'created': created, 'bytes': len(raw),
                'encoding': parsed['encoding'],
                'replay_run': replay['manifest']['run_id'],
            },
            'input_count': parsed['input_count'], 'blank_count': parsed['blank_count'],
            'segments': rules.segment_view(state),
            'ledger': rules.number_ledger(state),
            'channel_inv': rules.reconcile(replay['payments'], replay['tickets'], state),
            'quarantine': state['quarantine'],
            'metrics': {
                '号段': len(state['segments']),
                '已售': sum(1 for e in state['numbers'].values() if e['state'] == 'sold'),
                '待核实': sum(1 for e in state['numbers'].values() if e['state'] == 'pending'),
                '已占用': sum(1 for e in state['numbers'].values() if e['state'] == 'allocated'),
                '可发放': sum(1 for e in state['numbers'].values() if e['state'] == 'available'),
                '隔离': len(state['quarantine']),
            },
        }
        with db:  # 同一事务提交占用、票款对账快照与业务键
            try:
                inv_store.persist(db, state, ops, run_id, serial(snapshot), created)
            except sqlite3.IntegrityError as exc:
                # 触发器拦截的状态回退/并发冲突：整批回滚，不留半条占用
                raise DomainError('INV_REVERSE', f'盘存落库被状态守卫拒绝：{exc}', 409)
    finally:
        db.close()
    return inv_store.fetch(run_id)


def latest():
    runs = inv_store.history(limit=1)
    if not runs:
        return take_stock()
    return inv_store.fetch(runs[0]['id'])
