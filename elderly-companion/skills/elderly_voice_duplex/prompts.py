"""老人特化的对话策略：在想词还是已说完、垫音、医疗安全围栏。

这是 elderly-voice-duplex 真正的"技能"所在——ASR/TTS 只是管道，判断老人
是在喘口气想词还是已经说完了、以及这时候该不该接话，才是体验的关键。

三层结构：
  1. HesitationScorer  纯本地启发式，零延迟，处理绝大多数情况
  2. build_filler_prompt  拿不准时问模型，让垫音贴合当下语境
  3. SAFETY_GUARD      医疗问题的硬围栏（用药/胸痛/跌倒等），直接转标准话术
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ------------------------------------------------------------------ 角色设定

SYSTEM_PROMPT = """You are "Buddy", an AI companion for an elderly person living alone.

How you speak:
- Short sentences, slow pace, one thing at a time -- then leave room for them
- Warm and familiar, like a grandchild on the phone. Never condescending.
- When they pause mid-sentence, don't rush them and don't finish their thought
- Never interrupt, never correct their wording, never argue
- Remember what they told you before and pick it up naturally
  ("You said your legs felt heavy yesterday -- any better today?")

Never do:
- Give any diagnosis, dosage, or treatment advice. If they ask, gently steer
  away and suggest calling their family or their doctor.
- Say "you should" or "you must", or criticise their habits
- Invent things you don't remember -- say so and ask them to tell you again
"""

# ------------------------------------------------------- 在想词 vs 已说完

# 老人卡在词上时的典型口头填充。注意都是"话没说完"的信号。
HESITATION_MARKERS = (
    "um", "uh", "er", "ah", "hmm", "mm",
    "well", "you know", "i mean", "like",
    "let me think", "how do i say", "sort of", "kind of",
    "the thing is", "what's it called",
)

# 这些词出现时，通常是在换话题/收尾，而不是卡住。
TOPIC_SHIFT_MARKERS = ("by the way,", "also", "another thing")

_SENT_END = tuple(".!?")
_CLAUSE_END = tuple(",;:")

# 结尾是这些，说明话头断了
_TRAILING = ("...", "…", "--")


class TurnSignal(str, Enum):
    """老人这一轮发言的状态。"""
    COMPLETE = "complete"      # 说完了，可以接话
    THINKING = "thinking"      # 在想词，只该给垫音
    UNCERTAIN = "uncertain"    # 拿不准，交给模型判断


@dataclass
class TurnJudgement:
    signal: TurnSignal
    reason: str
    filler: str | None = None   # THINKING 时建议的垫音


class HesitationScorer:
    """本地启发式判定老人是否还在想词。零延迟，是主力判断。"""

    def __init__(self,
                 hesitation: tuple[str, ...] = HESITATION_MARKERS,
                 # 连续两次判定都在想词，才真的给垫音，避免一停顿就插嘴
                 min_hits: int = 1):
        self.markers = hesitation
        self.min_hits = min_hits
        self._streak = 0

    def _marker_hit(self, text: str) -> str | None:
        """返回命中的填充词（取最靠后的那个，最接近当下状态）。"""
        tail = text[-12:]
        hit = None
        for m in self.markers:
            idx = tail.rfind(m)
            if idx != -1 and (hit is None or idx > hit[1]):
                hit = (m, idx)
        return hit[0] if hit else None

    def judge(self, partial: str) -> TurnJudgement:
        text = partial.strip()
        if not text:
            self._streak = 0
            return TurnJudgement(TurnSignal.COMPLETE, "空输入")

        # 1) 明显的话头断裂
        if text.endswith(_TRAILING):
            return self._thinking(f"结尾是省略号：{text[-8:]!r}")

        # 2) 结尾撞上填充词 -> 在想词
        marker = self._marker_hit(text)
        if marker is not None:
            return self._thinking(f"结尾是填充词 {marker!r}")

        # 3) 有句末标点 -> 说完了
        if text.endswith(_SENT_END):
            self._streak = 0
            return TurnJudgement(TurnSignal.COMPLETE, "有句末标点")

        # 只到逗号/顿号：可能是长句中间的长停顿，倾向等一等
        if text.endswith(_CLAUSE_END):
            return TurnJudgement(
                TurnSignal.UNCERTAIN,
                f"only a pause mark: {text[-8:]!r}",
                filler="Go on, I'm listening.")

        # 5) 太短，信息不足
        if len(text) <= 3:
            return TurnJudgement(TurnSignal.UNCERTAIN, "utterance too short",
                                 filler="Mm-hm, I'm here.")

        # 6) 换话题词开头且前面有完整句 -> 说完
        self._streak = 0
        return TurnJudgement(TurnSignal.COMPLETE, "自然收尾")

    def _thinking(self, reason: str) -> TurnJudgement:
        self._streak += 1
        if self._streak < self.min_hits:
            return TurnJudgement(TurnSignal.UNCERTAIN, f"{reason} (first time)",
                                 filler=None)
        return TurnJudgement(TurnSignal.THINKING, reason,
                             filler="Take your time, I'm right here.")


# ------------------------------------------------------------------ 垫音生成

FILLER_PROMPT = """An elderly person is talking to you and has stopped mid-sentence,
clearly still searching for a word.

What they just said:
"{partial}"

Produce one very short acknowledgement ({max_chars} characters or fewer) that
tells them you're in no hurry.
- Output only that one line. No explanation, no quotes.
- Natural, like a grandchild acknowledging on the phone
- Do NOT finish their sentence for them, and do NOT ask a question
- Style examples: "Take your time." / "I'm listening." / "No rush at all."
"""


def build_filler_prompt(partial: str, max_chars: int) -> str:
    return FILLER_PROMPT.format(partial=partial[-200:], max_chars=max_chars)


# ------------------------------------------------------- 医疗安全围栏
# 用户 spec 里用 NeMo Guardrails + Colang 做生成前拦截。这里先落一层
# 确定性的关键词/正则预过滤：命中就直接返回标准话术，根本不进模型。
# 好处是零依赖、可单测、行为确定；Guardrails 后续可作为第二层叠加。
#
# 设计要点：P0 匹配的是**剂量模式**而不是药名。实测 ASR 会把
# "blood pressure pills" 听成近音词，但只要"can I take two ... pills"这个
# 剂量问法在，围栏就照样拦得住——药名同音字替换不会让它失效。

SAFETY_PATTERNS: tuple[tuple[str, str], ...] = (
    # P0: 自行增减剂量 / 停药
    (r"(can|could|should) i (take|have) (two|three|2|3|double|extra)", "P0"),
    (r"(double|increase|up) my (dose|dosage|pills|medication)", "P0"),
    (r"(stop|quit) (taking )?(my )?(pills|medication|insulin)", "P0"),
    (r"(skip|missed) my (dose|pills|medication)", "P0"),
    # P0: 急症
    (r"chest (pain|pressure|tight)|can'?t breathe|shortness of breath", "P0"),
    (r"(arm|face|leg) (is )?numb|my mouth (is |'s )?(crooked|drooping)", "P0"),
    (r"(fell|fallen) (down|over)|can'?t get up", "P0"),
    # P1: 体征不适
    (r"(dizzy|light.?headed|headache|nauseous|stomach ?ache)", "P1"),
    (r"(legs?|feet) (feel |felt )?(heavy|swollen|weak)", "P1"),
    (r"(couldn'?t|cannot|can'?t) sleep|no sleep last night", "P1"),
    (r"(feel|feeling) (tired|exhausted|unwell|off)", "P1"),
)

SAFETY_REPLIES = {
    "P0": ("I'm not able to advise you on that, dear. Please stick with what "
           "your doctor told you. Would you like me to call {kin} right now?"),
    "P1": ("That doesn't sound very comfortable. Sit down and rest a moment. "
           "Tell me how today went -- I'll make a note of it for {kin}."),
}

DEFAULT_KIN = "your son"


def check_safety(text: str, kin: str = DEFAULT_KIN) -> tuple[str, str] | None:
    """命中安全规则时返回 (分级, 标准话术)，否则 None。"""
    for pattern, level in SAFETY_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return level, SAFETY_REPLIES[level].format(kin=kin)
    return None


def build_reply_prompt(partial: str, history: list[dict], kin: str) -> str:
    """主回复的系统提示（历史由调用方按 messages 传入）。"""
    return (SYSTEM_PROMPT
            + "\n\nThe elderly person's family member to mention is called "
            + kin + ". Use that name when you refer to them.")
