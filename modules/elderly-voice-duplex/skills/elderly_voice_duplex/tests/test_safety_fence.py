#!/usr/bin/env python3
"""医疗安全围栏的回归测试：该拦的要拦，不该拦的别拦。

这条用例是冲着踩过的坑写的。原来的正则只覆盖"能不能吃两颗""加倍吃"这类
说法，而老人问改药量的说法远不止这些：

    "需要加药" "能加点药吗" "药量能不能加" "我的降压药用量能加吗"

一条都没命中——医疗围栏静默失效，比误拦危险得多。现在按
「动作+药」和「药+动作」两个方向拦，并且每条都有对应的反例
（"药店/药箱"不是加药，"和饭一起吃"不是改量，"多吃点饭"不是多吃药）。

顺带修了两个误拦："睡不着急"被当成"睡不着"，"加个菜"差点被当成加药。

用法：
    python3 tests/test_safety_fence.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.elderly_voice_duplex.prompts import (  # noqa: E402
    SAFETY_PATTERNS, check_safety)


def match(text: str) -> str | None:
    for pattern, level in SAFETY_PATTERNS:
        if re.search(pattern, text):
            return level
    return None


# (句子, 期望分级或 None)
CASES: list[tuple[str, str | None]] = [
    # ---- 原有用例，不能回归 ----
    ("我那个降压药今天能不能吃两颗？", "P0"),
    ("能不能多吃一片降压药", "P0"),
    ("我可以加倍吃吗", "P0"),
    ("药能不能停了", "P0"),
    ("我今天没吃降压药", "P0"),
    ("胸口疼得厉害", "P0"),
    ("喘不上气", "P0"),
    ("摔了一跤", "P0"),
    ("头晕", "P1"),
    ("腿沉得很", "P1"),
    ("睡不着急", None),              # "着急"不是"睡不着"

    # ---- 改变药量的其它说法（用户实际说过的）----
    ("需要加药", "P0"),
    ("我想加一点药", "P0"),
    ("能加点药吗", "P0"),
    ("加一片药行不行", "P0"),
    ("增加药量可以吗", "P0"),
    ("加大剂量", "P0"),
    ("药量能不能加", "P0"),
    ("我的降压药用量能加吗", "P0"),
    ("剂量可以增一点吗", "P0"),
    ("减点药没事吧", "P0"),
    ("减少用量", "P0"),
    ("换个药吃", "P0"),
    ("我把药停了", "P0"),

    # ---- 不该拦的：把药当物件、问配伍、说症状陈述 ----
    ("今天天气不错，我下楼溜达了一圈", None),
    ("昨天小明打电话来说周末要来看我", None),
    ("外面太阳挺好的", None),
    ("我去药店买了盒感冒药", None),       # 药店不是"加药"
    ("家里该添个药箱了", None),           # 药箱不是药
    ("我这个药能和饭一起吃吗", None),     # 问配伍，不是改量
    ("这个药能不能治好我的病", None),
    ("药快过期了能扔吗", None),
    ("医生给我开了三种药", None),
    ("药放在柜子里了", None),
    ("我想去公园溜达", None),
    ("儿子给我买了新手机", None),
    ("早上吃了两个包子", None),
    ("晚上想加个菜", None),              # 加菜不是加药
    ("晚上想多吃点饭", None),            # 多吃不是多吃药
    ("我吃了很多药还是睡不着", "P1"),    # 这是真症状，该拦 P1
]


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("[safety-fence] 医疗围栏该拦的拦、不该拦的不拦")

    bad = []
    for text, want in CASES:
        got = match(text)
        if got != want:
            bad.append((text, want, got))
    ok &= check(f"{len(CASES)} 条说法全部符合预期", not bad,
                "; ".join(f"{t!r} 期望={w} 实际={g}" for t, w, g in bad))

    # 命中时必须给出对应分级的标准话术，且话术里要带上子女称呼
    hit = check_safety("需要加药", "小明")
    ok &= check("命中 P0 时给出标准话术并带上称呼",
                hit is not None and hit[0] == "P0" and "小明" in hit[1],
                repr(hit))

    # 空输入不该命中任何规则
    ok &= check("空输入不命中", match("") is None and
                check_safety("", "小明") is None)

    print("[safety-fence] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


sys.exit(main())
