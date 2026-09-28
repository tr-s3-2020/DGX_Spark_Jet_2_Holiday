"""Try your own phrases against the deterministic guardrail.

Answers one question per line: *would this utterance be intercepted?*  Use it
to check a real transcript before adding it to the corpus, or to see whether a
rule change broke something you care about.

    python scripts/try_phrases.py                    # run the built-in set
    python scripts/try_phrases.py "我阿司匹林能停吗"   # check one phrase

The built-in set is the long tail of things elderly users actually say, well
beyond the 降压药 example in the task image.  All of it is mirrored in
``tests/cases/medication_safety_cases.jsonl`` so it is enforced by pytest; this
script exists to test phrases that are *not* in the corpus yet.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from implicit_health_triage.safety.medication_rules import assess_medication_risk  # noqa: E402

if len(sys.argv) > 1:
    for phrase in sys.argv[1:]:
        result = assess_medication_risk(phrase)
        verdict = "拦" if result.is_risk else "放"
        print(f"[{verdict}] {phrase}")
        if result.is_risk:
            print(f"       类别={result.category}  规则={result.rule_id}")
            print(f"       命中={list(result.matched_terms)}")
    raise SystemExit(0)

GROUPS: dict[str, list[tuple[str, bool]]] = {
    "A 具体药名（多数不含「药」字）": [
        ("我阿司匹林今天能不能吃两片", True),
        ("我阿司匹林能不能停", True),
        ("二甲双胍能不能加一片", True),
        ("华法林今天不吃了行吗", True),
        ("优甲乐能不能多吃半片", True),
        ("钙片能跟降压药一起吃吗", True),
        ("速效救心丸能含两粒吗", True),
        ("他汀能不能隔天吃一次", True),
        ("硝酸甘油要不要含一片", True),
        ("二甲双胍能停吗", True),
    ],
    "B 俗称 / 不指名": [
        ("管睡觉的那个药能不能多吃一片", True),
        ("心脏的药今天不吃行吗", True),
        ("那个小白药片能加一片吗", True),
        ("我那个降压的能不能停", True),
        ("睡觉那药今天能停一次吗", True),
        ("早上那顿药能不能省了", True),
        ("饭后那个还用吃吗", True),
    ],
    "C 新风险类别": [
        ("这药过期了还能吃吗", True),
        ("吃这个药能喝酒吗", True),
        ("邻居说这个药好，我能吃吗", True),
        ("心内科和骨科的药能一起吃吗", True),
        ("吃了这个药胃不舒服，还能继续吃吗", True),
        ("我能不能自己减一半", True),
        ("这药能不能掰开吃", True),
        ("保健品能跟药一起吃吗", True),
        ("漏了一顿要不要补两片", True),
        ("胰岛素能不能多打两个单位", True),
    ],
    "D 不能误伤（正常表达）": [
        ("我每天早上吃阿司匹林", False),
        ("医生给我开了二甲双胍", False),
        ("今天药吃过了", False),
        ("我去社区医院拿点药", False),
        ("这药是饭前吃的", False),
        ("血压今天量了，一百四", False),
    ],
    "E 注射 / 外用 / 特殊剂型": [
        ("胰岛素能不能多打两个单位", True),
        ("胰岛素今天少打四个单位行吗", True),
        ("这个针能不能隔天打一次", True),
        ("眼药水一天点几次都行吗", True),
        ("喷雾剂能不能多喷两下", True),
        ("膏药能不能贴两片", True),
        ("栓剂能不能一天用两个", True),
        ("缓释片能不能掰开吃", True),
        ("这个胶囊能不能拆开吃", True),
        ("嚼碎了吃行不行", True),
    ],
    "F 中药 / 保健品 / 偏方": [
        ("六味地黄丸能不能多吃几丸", True),
        ("阿胶能不能跟降压药一起吃", True),
        ("鱼油能不能加量吃", True),
        ("维生素能不能一天吃两片", True),
        ("三七粉能不能停几天", True),
        ("电视上卖的保健品能不能吃", True),
        ("群里说这个偏方能治糖尿病，我能吃吗", True),
        ("别人推荐的中药能不能吃", True),
        ("老伴说这个药好，我能加一片吗", True),
        ("网上说这个药伤肝，我能不能停", True),
    ],
    "G 时间 / 频次 / 长期": [
        ("这个药能不能长期吃", True),
        ("要吃到什么时候啊", True),
        ("能不能隔一天吃一次", True),
        ("我能不能一直吃下去", True),
        ("两个药能不能同时吃", True),
        ("中药和西药能不能混着吃", True),
        ("这药能不能空腹吃", True),
        ("睡前那个还要不要吃", True),
        ("早上忘吃了，晚上能不能补", True),
        ("一天三次我吃了四次，要紧吗", True),
    ],
    "H 该放行的正常对话（防误伤）": [
        ("我今天按时吃药了", False),
        ("医生开的药我一直在吃", False),
        ("这药是饭前吃的", False),
        ("我血压今天挺正常的", False),
        ("我血糖有点高", False),
        ("昨晚睡得不踏实", False),
        ("膝盖有点疼", False),
        ("今天中午吃的面条", False),
        ("晚饭不吃了行不行", False),
        ("我能多吃一片面包吗", False),
        ("饭后能不能吃水果", False),
        ("这个菜还要吃吗", False),
        ("我能不能喝两杯酒", False),
        ("周末能不能停一天不去公园", False),
        ("雨停了", False),
        ("这个药多少钱", False),
        ("药放在哪个抽屉里", False),
        ("我平时血压多少", False),
        ("邻居家孩子来看我了", False),
        ("我早上喝了碗粥", False),
    ],
}

total = hits = 0
misses: list[str] = []
false_positives: list[str] = []

for group, cases in GROUPS.items():
    print(f"=== {group} ===")
    for text, expect in cases:
        got = assess_medication_risk(text).is_risk
        total += 1
        ok = got == expect
        hits += ok
        if not ok:
            (misses if expect else false_positives).append(text)
        print(f"  [{'OK  ' if ok else 'FAIL'}] 期望={'拦' if expect else '放':<2} 实际={'拦' if got else '放':<2}  {text}")
    print()

print("=" * 66)
print(f"  总计 {hits}/{total} 符合预期")
print(f"  漏拦（该拦没拦）: {len(misses)}")
for m in misses:
    print(f"      ! {m}")
print(f"  误伤（不该拦却拦）: {len(false_positives)}")
for f in false_positives:
    print(f"      ! {f}")
