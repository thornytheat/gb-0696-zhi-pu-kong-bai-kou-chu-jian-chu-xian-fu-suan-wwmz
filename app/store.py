import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"runs.sqlite", timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    return db

def save(text, result):
    digest=hashlib.sha256((RULE_VERSION+"\n"+text).encode()).hexdigest()
    ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",(ident,digest,json.dumps(dict(result,rule_version=RULE_VERSION),ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    return item

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    current=get(ident)
    if decision=="通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
    with connect() as db:
        changed=db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",(decision,ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)

def switch_basis(ident,revision,key,basis):
    """切换单份样品的签发依据。先校验后落库：任何失败都不写库，原报告保留。"""
    from app.modules.m7_review.rules import resolve,available_bases,default_basis,sample_key,group_key,BASES
    if basis not in BASES: raise BusinessError("BASIS","依据类型无效")
    current=get(ident)
    result=current["result"]
    samples=[r for r in result.get("quality",[]) if r.get("kind")=="sample"]
    by_key={sample_key(s):s for s in samples}
    if key not in by_key: raise BusinessError("SAMPLE","样品不存在",404)
    evidence=result.get("spike_evidence",{"by_sample":{},"batch_avg":{}})
    group=group_key(by_key[key])
    if basis not in available_bases(key,group,evidence):
        raise BusinessError("BASIS_UNAVAILABLE","该样品无此依据，已保留原报告")
    overrides=dict(result.get("overrides",{}))
    if basis==default_basis(key,group,evidence): overrides.pop(key,None)
    else: overrides[key]=basis
    resolved=resolve(samples,evidence,overrides)
    new_result=dict(result,overrides=overrides,resolved=resolved)
    with connect() as db:
        changed=db.execute("UPDATE runs SET result=?,revision=revision+1 WHERE id=? AND revision=?",(json.dumps(new_result,ensure_ascii=False),ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)
