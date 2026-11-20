import json
from app.store import connection

# 状态只允许沿 available -> allocated -> pending -> sold 上行；
# SQL 内联秩表保证即使绕过规则层，已确认票号也不能重新变成可发放。
_RANK_CASE = ("CASE excluded.state WHEN 'available' THEN 0 WHEN 'allocated' THEN 1 "
              "WHEN 'pending' THEN 2 WHEN 'sold' THEN 3 END")
_RANK_COL = ("CASE inv_numbers.state WHEN 'available' THEN 0 WHEN 'allocated' THEN 1 "
             "WHEN 'pending' THEN 2 WHEN 'sold' THEN 3 END")


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS inv_runs(
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, created TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS inv_segments(
        segment TEXT PRIMARY KEY, channel TEXT NOT NULL, start_no TEXT NOT NULL,
        end_no TEXT NOT NULL, line INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS inv_numbers(
        ticket TEXT PRIMARY KEY, segment TEXT NOT NULL, channel TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('available','allocated','pending','sold')),
        source TEXT NOT NULL, updated_run TEXT NOT NULL,
        line INTEGER NOT NULL, amount TEXT, ref TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS inv_refs(
        ref TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT NOT NULL, line INTEGER NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_inv_numbers_seg ON inv_numbers(segment, ticket)")
    # 轻量迁移：为旧库补 ref 列（新库列已存在则忽略）
    cols = {r[1] for r in db.execute("PRAGMA table_info(inv_numbers)").fetchall()}
    if 'ref' not in cols:
        db.execute("ALTER TABLE inv_numbers ADD COLUMN ref TEXT NOT NULL DEFAULT ''")
    # 触发器兜底：任何写入路径都不允许票号状态沿阶梯下行
    db.execute("""CREATE TRIGGER IF NOT EXISTS trg_inv_number_no_revert
        BEFORE UPDATE OF state ON inv_numbers
        WHEN (CASE NEW.state WHEN 'available' THEN 0 WHEN 'allocated' THEN 1
              WHEN 'pending' THEN 2 WHEN 'sold' THEN 3 END)
           < (CASE OLD.state WHEN 'available' THEN 0 WHEN 'allocated' THEN 1
              WHEN 'pending' THEN 2 WHEN 'sold' THEN 3 END)
        BEGIN SELECT RAISE(ABORT, 'INV_REVERSE: 已确认票号不能回退'); END""")


def connect():
    db = connection()
    _ensure(db)
    return db


def run_exists(db, run_id):
    return db.execute("SELECT 1 FROM inv_runs WHERE id=?", (run_id,)).fetchone() is not None


def load_state(db):
    state = {'segments': {}, 'numbers': {}, 'refs': set(), 'quarantine': []}
    for row in db.execute("SELECT * FROM inv_segments ORDER BY rowid"):
        state['segments'][row['segment']] = {'segment': row['segment'], 'channel': row['channel'],
                                             'start': row['start_no'], 'end': row['end_no'],
                                             'line': row['line']}
    for row in db.execute("SELECT * FROM inv_numbers ORDER BY ticket"):
        state['numbers'][row['ticket']] = {'ticket': row['ticket'], 'segment': row['segment'],
                                           'channel': row['channel'], 'state': row['state'],
                                           'source': row['source'], 'updated_run': row['updated_run'],
                                           'line': row['line'], 'amount': row['amount'],
                                           'ref': row['ref'] if 'ref' in row.keys() else ''}
    for row in db.execute("SELECT ref FROM inv_refs"):
        state['refs'].add(row['ref'])
    return state


def persist(db, state, ops, run_id, snapshot, created):
    """整批原子落库：号段/票号/业务键/快照同事务，失败不留半条。"""
    for seg in state['segments'].values():
        db.execute("""INSERT OR IGNORE INTO inv_segments(segment,channel,start_no,end_no,line)
            VALUES(?,?,?,?,?)""",
                   (seg['segment'], seg['channel'], seg['start'], seg['end'], seg['line']))
    for entry in state['numbers'].values():
        db.execute(f"""INSERT INTO inv_numbers(ticket,segment,channel,state,source,updated_run,line,amount,ref)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticket) DO UPDATE SET
              state=CASE WHEN {_RANK_CASE} >= {_RANK_COL} THEN excluded.state ELSE inv_numbers.state END,
              source=CASE WHEN {_RANK_CASE} >= {_RANK_COL} THEN excluded.source ELSE inv_numbers.source END,
              updated_run=CASE WHEN {_RANK_CASE} >= {_RANK_COL} THEN excluded.updated_run
                               ELSE inv_numbers.updated_run END,
              line=CASE WHEN {_RANK_CASE} >= {_RANK_COL} THEN excluded.line ELSE inv_numbers.line END,
              amount=COALESCE(excluded.amount, inv_numbers.amount),
              ref=CASE WHEN excluded.ref='' THEN inv_numbers.ref ELSE excluded.ref END""",
                   (entry['ticket'], entry['segment'], entry['channel'], entry['state'], entry['source'],
                    entry['updated_run'], entry['line'], entry['amount'], entry.get('ref', '')))
    for op in ops:
        if op['kind'] in ('print', 'allocate') and op['ref'] in state['refs']:
            db.execute("""INSERT OR IGNORE INTO inv_refs(ref,kind,run_id,line)
                VALUES(?,?,?,?)""", (op['ref'], op['kind'], run_id, op['_line']))
    db.execute("INSERT OR IGNORE INTO inv_runs(id,payload,created) VALUES(?,?,?)",
               (run_id, json.dumps(snapshot, ensure_ascii=False), created))


def fetch(run_id):
    with connect() as db:
        row = db.execute("SELECT payload FROM inv_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        from app.errors import DomainError
        raise DomainError('NOT_FOUND', '盘存批次不存在', 404)
    return json.loads(row['payload'])


def history(limit=100):
    with connect() as db:
        rows = db.execute("SELECT id, created FROM inv_runs ORDER BY rowid DESC LIMIT ?",
                          (limit,)).fetchall()
    return [dict(row) for row in rows]
