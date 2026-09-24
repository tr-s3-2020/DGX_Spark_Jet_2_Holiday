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

SYSTEM_PROMPT = """你是"小伴"，一位陪伴独居老人的 AI 家人。

说话方式：
- 语速慢、句子短，一次只说一件事，说完留时间给老人接
- 用"您"，像晚辈对长辈那样温和，不居高临下
- 老人说话停顿时不要急，更不要替他把话说完
- 不打断、不纠正老人的口误，不和他们争辩
- 记得老人之前说过的事，自然地接上（"您昨天说腿沉，今天好些没？"）

绝对不做：
- 不给任何诊断、用药剂量、治疗方案建议；老人问起就温和转开，建议联系子女或社区医生
- 不说"您应该/必须"，不批评老人的生活习惯
- 不编造自己记不住的事，忘了就说忘了，请老人再讲一遍
"""

# ------------------------------------------------------- 在想词 vs 已说完

# 老人卡在词上时的典型口头填充。注意都是"话没说完"的信号。
HESITATION_MARKERS = (
    "嗯", "呃", "啊", "诶", "唉", "哦",
    "这个", "那个", "就是", "就是说", "怎么说", "我是说",
    "然后呢", "后来呢", "我想想", "让我想想", "我寻思",
    "反正", "其实吧", "对了",
)

# 这些词出现时，通常是在换话题/收尾，而不是卡住。
TOPIC_SHIFT_MARKERS = ("对了，", "还有", "另外", "话说")

_SENT_END = tuple("。！？!?…~；;")
_CLAUSE_END = tuple("，,、")

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

        # 4) 只有逗号/顿号：可能是长句中间的长停顿，倾向等一等
        if text.endswith(_CLAUSE_END):
            return TurnJudgement(
                TurnSignal.UNCERTAIN,
                f"只到停顿标点：{text[-8:]!r}",
                filler="诶，您说。")

        # 5) 太短，信息不足
        if len(text) <= 3:
            return TurnJudgement(TurnSignal.UNCERTAIN, "发言过短",
                                 filler="嗯，我在听。")

        # 6) 换话题词开头且前面有完整句 -> 说完
        self._streak = 0
        return TurnJudgement(TurnSignal.COMPLETE, "自然收尾")

    def _thinking(self, reason: str) -> TurnJudgement:
        self._streak += 1
        if self._streak < self.min_hits:
            return TurnJudgement(TurnSignal.UNCERTAIN, f"{reason}（首次）",
                                 filler=None)
        return TurnJudgement(TurnSignal.THINKING, reason,
                             filler="诶，您慢慢想，我等您。")


# ------------------------------------------------------------------ 垫音生成

FILLER_PROMPT = """老人在和你说话，说到一半停住了，明显还在想词。

老人刚说的内容：
"{partial}"

请生成一句很短的接应话（{max_chars} 字以内），目的是告诉老人"我不急，你慢慢想"。
要求：
- 只输出这一句话，不要解释、不要引号、不要标点堆砌
- 语气自然，像家里晚辈在电话里应一声
- 不要替老人把话接下去，不要提问
- 示例风格："诶，您慢慢想。" / "嗯，我在听呢。" / "不着急，您想着说。"
"""


def build_filler_prompt(partial: str, max_chars: int) -> str:
    return FILLER_PROMPT.format(partial=partial[-200:], max_chars=max_chars)


# ------------------------------------------------------- 医疗安全围栏
# 用户 spec 里用 NeMo Guardrails + Colang 做生成前拦截。这里先落一层
# 确定性的关键词/正则预过滤：命中就直接返回标准话术，根本不进模型。
# 好处是零依赖、可单测、行为确定；Guardrails 后续可作为第二层叠加。

SAFETY_PATTERNS: tuple[tuple[str, str], ...] = (
    # (正则, 分级)
    (r"(能不能|可不可以|能|可以).{0,6}(吃|服|用).{0,4}(两|三|2|3|双).{0,3}(片|颗|粒|丸)", "P0"),
    (r"(加倍|加量|多服|多吃).{0,4}(药|片|颗)", "P0"),
    (r"(停[药止]|不吃了|断了).{0,4}(药|胰岛素|降压)", "P0"),
    (r"(胸[口]?(痛|疼|闷|压)|喘不上气|呼吸困难|心慌得厉害|出冷汗|半边.{0,3}麻|嘴歪)", "P0"),
    (r"(摔|跌).{0,4}(倒|一跤|下)|起不来", "P0"),
    (r"(头晕|头痛|恶心|肚子[痛疼]|腿[沉肿]|睡不[着着])", "P1"),
)

SAFETY_REPLIES = {
    "P0": ("这个我不敢给您拿主意，您先按平时医生说的来。要不要现在给"
           "{kin}打个电话？我帮您拨。"),
    "P1": ("听着是有点不舒服。您先坐下歇会儿，缓缓神。今天觉得怎么样，"
           "跟我说说，我记着，回头告诉{kin}。"),
}

DEFAULT_KIN = "孩子"


def check_safety(text: str, kin: str = DEFAULT_KIN) -> tuple[str, str] | None:
    """命中安全规则时返回 (分级, 标准话术)，否则 None。"""
    for pattern, level in SAFETY_PATTERNS:
        if re.search(pattern, text):
            return level, SAFETY_REPLIES[level].format(kin=kin)
    return None


def build_reply_prompt(partial: str, history: list[dict], kin: str) -> str:
    """主回复的系统提示（历史由调用方按 messages 传入）。"""
    return (SYSTEM_PROMPT
            + "\n\n老人的子女称呼为「" + kin + "」，提及家人时用这个称呼。")
