from flask import render_template,request
from app import store
from app.pipeline import run
from app.config import TITLE,MODULES,COLUMNS,INV_COLUMNS,SAMPLES,DEFAULTS
from app.modules.m8_inventory import service as inv_service, store as inv_store
def view(stage):
    if not 1<=stage<=8: return "页面不存在",404
    run_id=request.args.get("run")
    if stage==8:
        result=store.fetch(run_id) if run_id else run()
        inv=inv_store.fetch(request.args["inv"]) if request.args.get("inv") else inv_service.latest()
        sample=(SAMPLES/"inventory.csv").read_text(encoding="utf-8-sig")
        sold={t['seat'] for t in result['tickets']};paid={t['seat'] for t in result['payments']};held={t['seat'] for t in result['leases']}
        seats=[{'seat':f'A-{i:02d}','state':'已出票' if f'A-{i:02d}' in sold else '待出票' if f'A-{i:02d}' in paid else '已过期' if f'A-{i:02d}' in held else '空闲'} for i in range(1,17)]
        return render_template("m8.html",title=TITLE,modules=MODULES,stage=stage,result=result,
                               data=inv,sample=sample,defaults=DEFAULTS,columns={**COLUMNS,**INV_COLUMNS},
                               history=store.history(),inv_history=inv_store.history(),seats=seats)
    result=store.fetch(run_id) if run_id else run()
    sample=(SAMPLES/"dirty.csv").read_text(encoding="utf-8-sig")
    data=result["stages"][stage-1]
    sold={t['seat'] for t in result['tickets']};paid={t['seat'] for t in result['payments']};held={t['seat'] for t in result['leases']}
    seats=[{'seat':f'A-{i:02d}','state':'已出票' if f'A-{i:02d}' in sold else '待出票' if f'A-{i:02d}' in paid else '已过期' if f'A-{i:02d}' in held else '空闲'} for i in range(1,17)]
    return render_template(f"m{stage}.html",title=TITLE,modules=MODULES,stage=stage,result=result,data=data,
                           sample=sample,defaults=DEFAULTS,columns=COLUMNS,history=store.history(),seats=seats)
