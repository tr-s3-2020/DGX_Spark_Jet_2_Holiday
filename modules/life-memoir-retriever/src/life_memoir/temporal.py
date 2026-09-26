"""Conservative year constraints. No language-model arithmetic and no guessed dates."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from .models import TimeClaim


def number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if "十" in text:
        a, b = text.split("十", 1)
        if (a and a not in digits) or (b and b not in digits):
            return None
        return digits.get(a, 1) * 10 + digits.get(b, 0)
    return digits.get(text)


def role_for(text: str) -> str | None:
    if "出生" in text or "年生的" in text:
        return "birth"
    if "扳手" in text:
        return "wrench"
    if "车间" in text and any(w in text for w in ("调", "转")):
        return "workshop"
    if "进厂" in text:
        return "factory"
    return None


def parse_time(text: str, source_id: str, anchors: dict[str, str], occurred_at: str | None = None) -> list[dict]:
    """Small, explicitly bounded parser for offline fixtures and literal corrections.

    Real backends propose the same TimeClaim schema; this parser is not a general
    Chinese temporal NLU system. Unsupported expressions remain unresolved.
    """
    common = {"raw_text": text[:512], "source_id": source_id,
              "calendar": "lunar" if "农历" in text else "gregorian",
              "approximate": any(x in text for x in ("大概", "左右", "前后", "约", "来岁")),
              "subject": "ambiguous" if any(x in text for x in ("他二十", "父亲", "母亲", "爸爸", "妈妈")) else "self"}
    year = re.search(r"(1[0-9]{3}|20[0-9]{2})\s*年", text)
    if year:
        return [TimeClaim(relation="in_year", year_value=int(year[1]), **common).model_dump()]
    if "去年" in text and occurred_at:
        year_value = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).year - 1
        return [TimeClaim(relation="in_year", year_value=year_value, **common).model_dump()]
    offset = re.search(r"([一二两三四五六七八九十0-9]+)\s*年后", text)
    if offset:
        n = number(offset[1])
        anchor = anchors.get("factory") if "进厂" in text else None
        if n is not None:
            return [TimeClaim(relation="years_after", offset_years=n, anchor_ref=anchor, **common).model_dump()]
    age = re.search(r"([一二两三四五六七八九十0-9]+)\s*(周岁|虚岁|岁)", text)
    if age:
        n = number(age[1])
        if n is not None:
            return [TimeClaim(relation="at_age", age_value=n,
                age_basis={"周岁": "completed_years", "虚岁": "nominal_years"}.get(age[2], "unknown"),
                anchor_ref=anchors.get("birth"), **common).model_dump()]
    if "车间" in text and "时候" in text and anchors.get("workshop"):
        return [TimeClaim(relation="not_before_event", anchor_ref=anchors["workshop"], **common).model_dump()]
    if "年轻时" in text:
        return [TimeClaim(relation="life_stage", **common).model_dump()]
    return [TimeClaim(relation="unresolved", **common).model_dump()]


def resolve(entries: list[dict], max_nodes: int = 10000) -> dict[str, dict]:
    """Resolve only available, permitted entries. Missing anchors stay unknown.

    Possible windows are uncertainty bounds, never employment durations.
    Multiple incompatible claims remain alternatives, never averaged.
    """
    by_id = {e["entry_id"]: e for e in entries}
    memo: dict[str, dict] = {}
    visiting: set[str] = set()

    def unknown(eid, display="年份未详", state="unresolved"):
        return {"event_id": eid, "version": by_id[eid]["version"], "state": state,
                "alternatives": [{"alternative_id": "unknown", "kind": "unknown", "display": display,
                "earliest_year": None, "latest_year": None, "approximate": False,
                "basis": "reported", "assumption_notes": [], "dependency_entry_ids": [eid], "derivation": None}]}

    def visit(eid: str, depth=0):
        if eid in memo:
            return memo[eid]
        if eid in visiting:
            return unknown(eid, "时间关系存在循环，待核实", "conflicted")
        if depth > 64 or len(memo) >= max_nodes:
            return unknown(eid, "超出本轮推理范围，待核实")
        e = by_id[eid]
        if e.get("time_rejected"):
            return unknown(eid, "本人已否定该时间推算，年份待核实")
        visiting.add(eid)
        alternatives = []
        conflicted = False
        for i, c in enumerate(e.get("time_assertions", [])):
            raw = c["raw_text"]
            a = {"alternative_id": hashlib.sha256(f'{eid}:{e["version"]}:{i}'.encode()).hexdigest()[:16],
                 "kind": "unknown", "display": raw, "earliest_year": None, "latest_year": None,
                 "approximate": c.get("approximate", False), "basis": "reported", "assumption_notes": [],
                 "dependency_entry_ids": [eid], "derivation": None}
            relation = c["relation"]
            if c.get("subject", "self") != "self" or c.get("calendar") != "gregorian":
                a["assumption_notes"].append("人物或历法需核实；保留原话")
            elif relation == "in_year" and c.get("year_value") is not None:
                y = c["year_value"]
                a.update(kind="year", display=f'{"约" if a["approximate"] else ""}{y}年')
                if not a["approximate"]:
                    a.update(earliest_year=y, latest_year=y)
            elif relation == "life_stage":
                a.update(kind="life_stage")
            elif relation in {"at_age", "years_after", "after_event", "before_event", "not_before_event"}:
                anchor = c.get("anchor_ref")
                a.update(kind="relative")
                if anchor in by_id:
                    r = visit(anchor, depth + 1)
                    if r["state"] == "conflicted":
                        conflicted = True
                    for base in r["alternatives"]:
                        a["dependency_entry_ids"] += base["dependency_entry_ids"]
                    if len(r["alternatives"]) == 1:
                        base = r["alternatives"][0]
                        lo, hi = base["earliest_year"], base["latest_year"]
                        if lo is not None and hi is not None and not base["approximate"] and not base["assumption_notes"]:
                            if relation == "at_age" and c.get("age_value") is not None and c.get("age_basis") != "nominal_years":
                                age = c["age_value"]
                                a.update(kind="possible_window", basis="derived")
                                if c.get("age_basis") != "completed_years":
                                    a["assumption_notes"].append("按周岁试算，计龄方式待确认")
                                if not a["approximate"]:
                                    a.update(earliest_year=lo + age, latest_year=hi + age + 1,
                                             display=f"{lo + age}—{hi + age + 1}年间（据年龄推算）",
                                             derivation={"rule_id": "completed-age-year-envelope", "input_entry_ids": [anchor, eid],
                                             "input_versions": [by_id[anchor]["version"], e["version"]], "expression": f"[{lo}+{age}, {hi}+{age}+1]"})
                            elif relation == "years_after" and c.get("offset_years") is not None:
                                n = c["offset_years"]
                                if not a["approximate"]:
                                    a.update(kind="possible_window", basis="derived", earliest_year=lo + n, latest_year=hi + n,
                                        display=f"{lo+n}—{hi+n}年间（据相对时间推算）",
                                        derivation={"rule_id": "year-offset", "input_entry_ids": [anchor, eid],
                                        "input_versions": [by_id[anchor]["version"], e["version"]], "expression": f"[{lo}+{n}, {hi}+{n}]"})
                else:
                    a["assumption_notes"].append("缺少可用锚点，暂不换算年份")
            a["dependency_entry_ids"] = sorted(set(a["dependency_entry_ids"]))
            alternatives.append(a)
        visiting.remove(eid)
        if not alternatives:
            result = unknown(eid)
        else:
            exact = [a for a in alternatives if a["earliest_year"] is not None and a["latest_year"] is not None]
            if len(exact) > 1:
                lo = max(a["earliest_year"] for a in exact)
                hi = min(a["latest_year"] for a in exact)
                if lo > hi:
                    conflicted = True
                elif len(exact) == len(alternatives):
                    merged = dict(exact[0])
                    merged.update(earliest_year=lo, latest_year=hi, display=f"{lo}年" if lo == hi else f"{lo}—{hi}年间",
                                  basis="derived", dependency_entry_ids=sorted(set(x for a in exact for x in a["dependency_entry_ids"])),
                                  derivation={"rule_id": "compatible-intersection", "input_entry_ids": [eid], "input_versions": [e["version"]], "expression": f"intersection=[{lo}, {hi}]"})
                    alternatives = [merged]
            state = "conflicted" if conflicted else "tentative" if any(a["assumption_notes"] or a["approximate"] for a in alternatives) else "resolved" if all(a["earliest_year"] is not None for a in alternatives) else "unresolved"
            result = {"event_id": eid, "version": e["version"], "state": state, "alternatives": alternatives}
        memo[eid] = result
        return result

    for eid in by_id:
        memo[eid] = visit(eid)
    return memo


def chronicle(entries: list[dict]) -> dict:
    stories = [e for e in entries if e["kind"] in {"story", "detail"} and e.get("record_status", "active") == "active"]
    resolutions = resolve(stories)
    items, narrative = [], []
    for e in stories:
        resolution = resolutions[e["entry_id"]]
        items.append({"event_id": e["entry_id"], "entry_id": e["entry_id"], "version": e["version"],
                      "title": e["content"], "content_locale": e["content_locale"],
                      "detail_refs": e["source_refs"], "time_resolution": resolution,
                      "prior_time_review": e.get("time_confirmation")})
        narrative.append({"text": "据本人讲述：" + e["content"], "event_refs": [e["entry_id"]],
                          "evidence_refs": [r["evidence_id"] for r in e["source_refs"]], "resolution_refs": []})
    # A stable reading order, not a claim of ordering among overlapping windows.
    items.sort(key=lambda x: (x["time_resolution"]["alternatives"][0]["earliest_year"] or 99999, x["event_id"]))
    return {"items": items, "narrative": narrative, "narrative_method": "source-linked template (offline); not a live model biography"}


def markdown(view: dict) -> str:
    def escape(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")
    rows = ["# 个人年谱", "", "系统据获准记录整理；推算年份保留范围，可由本人复核。", "",
            "| 时间／时期 | 事件 | 时间依据 |", "|---|---|---|"]
    for e in view["items"]:
        r = e["time_resolution"]
        label = "；".join(a["display"] for a in r["alternatives"])
        bases = "/".join(sorted({a["basis"] for a in r["alternatives"]}))
        rows.append(f'| {escape(label)} | {escape(e["title"])} | {r["state"]} · {bases} · {e["event_id"]} |')
    rows += ["", "## 整理文字", ""]
    rows += [escape(s["text"]) + "〔" + ", ".join(s["event_refs"]) + "〕" for s in view["narrative"]]
    return "\n".join(rows)
