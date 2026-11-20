import csv
import io
import json

from flask import Blueprint, request, jsonify, Response
from app.errors import DomainError
from . import service, store as inv_store

bp = Blueprint('m8', __name__)


def _payload():
    if request.files:
        file = request.files.get('file')
        if file is None:
            raise DomainError('FILE', '请选择文件')
        raw = file.read()
        try:
            params = json.loads(request.form.get('params', '{}'))
        except ValueError:
            raise DomainError('PARAMS', '参数格式错误')
        replay = request.form.get('replay_text')
        return raw, params, replay.encode('utf-8') if replay else None
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DomainError('BODY', '请求必须是对象')
    raw = payload.get('text')
    if raw is not None and not isinstance(raw, str):
        raise DomainError('TEXT', '数据必须为文本')
    replay = payload.get('replay_text')
    if replay is not None and not isinstance(replay, str):
        raise DomainError('TEXT', '渠道事件必须为文本')
    return raw, payload.get('params'), replay.encode('utf-8') if replay else None


@bp.route('/api/m8', methods=['GET', 'POST'])
def endpoint():
    if request.method == 'GET':
        run_id = request.args.get('run')
        return jsonify(inv_store.fetch(run_id) if run_id else service.latest())
    raw, params, replay_bytes = _payload()
    return jsonify(service.take_stock(raw, params, replay_bytes))


@bp.route('/api/m8/history')
def inv_history():
    return jsonify(inv_store.history())


@bp.route('/api/m8/evidence.csv')
def evidence():
    run_id = request.args.get('run')
    result = inv_store.fetch(run_id) if run_id else service.latest()
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['盘存批次', '号段', '票号', '渠道', '状态', '来源', '金额 CNY'])
    for row in result['ledger']:
        writer.writerow([result['manifest']['run_id'], row['segment'], row['ticket'],
                         row['channel'], row['state'], row['source'], row.get('amount') or ''])
    for row in result['quarantine']:
        raw = row.get('raw', {})
        writer.writerow([result['manifest']['run_id'], raw.get('segment', ''),
                         raw.get('ticket', ''), raw.get('channel', ''),
                         row['code'], row.get('detail', ''), raw.get('amount', '')])
    return Response('\ufeff' + stream.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=inventory-evidence.csv'})
