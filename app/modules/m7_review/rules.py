from collections import Counter
from decimal import Decimal
from app.errors import BusinessError
from app.numbers import number, fmt
from app.report import report, quarantine
from app.modules.m6_quality.rules import classify

# 回收率可作为修正依据的可用区间；超出视为证据无效并隔离
RECOVERY_MIN = Decimal("0.5")
RECOVERY_MAX = Decimal("1.5")

BASES = {"OWN_SPIKE": "自身加标", "BATCH_AVG": "同批均值", "EXTERNAL": "外标未修正"}
ISSUABLE = {"OWN_SPIKE": ("可签发", "ISSUABLE"),
            "BATCH_AVG": ("可签发·借据", "ISSUABLE_BORROWED"),
            "EXTERNAL": ("暂缓签发", "HOLD")}

def sample_key(row):
    return "|".join([row["batch"], row["analyte"], row["sample_id"]])

def group_key(row):
    return row["batch"] + "|" + row["analyte"]

def ready(rows, errors):
    return bool(rows) and not errors and not any(r["status"] in {"OVER_RANGE", "CONTROL_FAIL"} for r in rows)

def build_evidence(ctx):
    """汇总加标回收证据：自身加标按样品，同批均值按批次+检测项。无效加标行隔离。"""
    parents = {sample_key(r): r for r in ctx["concentrations"] if r["kind"] == "sample"}
    by_sample = {}
    for sp in (r for r in ctx["concentrations"] if r["kind"] == "spike"):
        key = sample_key(sp)
        parent = parents.get(key)
        if parent is None:
            quarantine(ctx, sp, "SPIKE_ORPHAN"); continue
        curve = ctx["curves"].get((sp["batch"], sp["analyte"]))
        added = sp["level_value"] * sp["dilution_value"]
        if added <= 0 or (curve and sp["response"] > curve["max_response"]):
            quarantine(ctx, sp, "SPIKE_INVALID"); continue
        recovery = (sp["concentration"] - parent["concentration"]) / added
        if recovery < RECOVERY_MIN or recovery > RECOVERY_MAX:
            quarantine(ctx, sp, "SPIKE_RECOVERY"); continue
        by_sample[key] = {"recovery": recovery, "added": added, "spike_line": sp["line"],
                          "spike_concentration": sp["concentration"], "parent_concentration": parent["concentration"]}
    groups = {}
    for r in ctx["concentrations"]:
        if r["kind"] != "sample": continue
        key = sample_key(r)
        if key in by_sample: groups.setdefault(group_key(r), []).append(by_sample[key]["recovery"])
    batch_avg = {k: {"recovery": sum(v) / len(v), "n": len(v)} for k, v in groups.items()}
    return {"by_sample": by_sample, "batch_avg": batch_avg}

def available_bases(key, group, evidence):
    out = []
    if key in evidence.get("by_sample", {}): out.append("OWN_SPIKE")
    if group in evidence.get("batch_avg", {}): out.append("BATCH_AVG")
    out.append("EXTERNAL")
    return out

def default_basis(key, group, evidence):
    return available_bases(key, group, evidence)[0]

def resolve(samples, evidence, overrides):
    """按当前依据解析每份样品的签发视图；浓度、检出状态、可签发状态同源产出。

    samples 可来自流水线(Decimal)或库存JSON(字符串)，两种输入产出一致。
    依据不可用时抛 BusinessError，调用方须保留原报告。
    """
    resolved = []
    for s in samples:
        key, group = sample_key(s), group_key(s)
        basis = (overrides or {}).get(key) or default_basis(key, group, evidence)
        options = available_bases(key, group, evidence)
        if basis not in options:
            raise BusinessError("BASIS_UNAVAILABLE", "样品 %s 无此依据，已保留原报告" % s["sample_id"])
        lod = number(s["lod"])
        recovery, note = None, ""
        if basis == "OWN_SPIKE":
            recovery = number(evidence["by_sample"][key]["recovery"])
            note = "加标行%s" % evidence["by_sample"][key]["spike_line"]
        elif basis == "BATCH_AVG":
            recovery = number(evidence["batch_avg"][group]["recovery"])
            note = "n=%s" % evidence["batch_avg"][group]["n"]
        if recovery is None:
            shown = str(s["concentration"])
            status, qualifier = s["status"], s.get("qualifier", "")
        else:
            corrected = Decimal(fmt(number(s["concentration"]) / recovery, 4))
            shown = fmt(corrected, 4)
            status = classify(corrected, lod, s.get("status") == "OVER_RANGE")
            qualifier = "<" if status == "BELOW_LOD" else ""
        issuable, issuable_code = ("暂缓签发", "HOLD") if status == "OVER_RANGE" else ISSUABLE[basis]
        resolved.append({"key": key, "sample_id": s["sample_id"], "analyte": s["analyte"], "batch": s["batch"],
                         "line": s["line"], "kind": "sample", "unit": s["unit"], "lod": str(lod),
                         "external_concentration": str(s["concentration"]),
                         "basis": BASES[basis], "basis_code": basis,
                         "recovery": fmt(recovery, 4) if recovery is not None else "", "note": note,
                         "concentration": shown, "status": status, "qualifier": qualifier,
                         "issuable": issuable, "issuable_code": issuable_code, "options": options})
    return resolved

def apply(ctx):
    ctx["final"] = ctx["quality"]
    ctx["spike_evidence"] = build_evidence(ctx)
    ctx["overrides"] = {}
    samples = [r for r in ctx["quality"] if r["kind"] == "sample"]
    ctx["resolved"] = resolve(samples, ctx["spike_evidence"], ctx["overrides"])
    ctx["blocked"] = not ready(ctx["final"], ctx["curve_errors"])
    excluded = {r["line"] for r in ctx["quarantine"]}
    accepted = ctx["input_count"] - len(excluded) - ctx["blank_rows"]
    ctx["balance"] = {"input": ctx["input_count"], "accepted": accepted, "quarantined": len(excluded), "blank": ctx["blank_rows"]}
    tally = Counter(r["issuable_code"] for r in ctx["resolved"])
    return report(ctx["resolved"], 结论="阻断" if ctx["blocked"] else "可复核", 样品=len(samples),
                  可签发=tally["ISSUABLE"], 借据=tally["ISSUABLE_BORROWED"], 暂缓=tally["HOLD"], 隔离=len(excluded))
