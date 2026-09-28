"""FastAPI surface for ``POST /v1/triage`` (task guide sections 29-31)."""

from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from ..guardrails_runtime import GuardrailsRuntime
from ..main import build_service, reset_service
from ..schemas import PartialIgnoredResponse, TriageRequest, TriageResponse
from ..service import ImplicitHealthTriageService
from ..settings import Settings, get_settings


class SessionHistory:
    """Bounded, LRU-evicted buffer of recent elder turns per session.

    Used so a pressure follow-up such as "不要跟我说问医生，你直接告诉我。" can
    still be judged as a medication question after an earlier medication turn
    (task guide section 36.3, doc 1 group E).

    Two bounds apply, and both matter:

    * **per session** — ``max_turns`` keeps only the most recent turns.
    * **number of sessions** — ``max_sessions`` evicts the least recently used
      session once the limit is reached.  Without it the mapping grows for the
      lifetime of the process, one entry per call, which is a slow leak in a
      long-running service.

    In-memory only: a restart clears it, which is fine because the buffer only
    sharpens follow-up detection.
    """

    def __init__(self, max_turns: int = 3, max_sessions: int = 200) -> None:
        self._max_turns = max(1, max_turns)
        self._max_sessions = max(1, max_sessions)
        self._turns: OrderedDict[str, deque[str]] = OrderedDict()
        self._evicted = 0

    def get(self, session_id: str) -> list[str]:
        turns = self._turns.get(session_id)
        if turns is None:
            return []
        self._turns.move_to_end(session_id)  # LRU touch
        return list(turns)

    def add(self, session_id: str, text: str) -> None:
        turns = self._turns.get(session_id)
        if turns is None:
            turns = deque(maxlen=self._max_turns)
            self._turns[session_id] = turns
        else:
            self._turns.move_to_end(session_id)

        if text:
            turns.append(text)

        while len(self._turns) > self._max_sessions:
            self._turns.popitem(last=False)
            self._evicted += 1

    def drop(self, session_id: str) -> bool:
        """Forget one session. Returns whether it existed."""

        return self._turns.pop(session_id, None) is not None

    def clear(self) -> None:
        self._turns.clear()
        self._evicted = 0

    def stats(self) -> dict[str, int]:
        return {
            "active_sessions": len(self._turns),
            "max_sessions": self._max_sessions,
            "max_turns_per_session": self._max_turns,
            "evicted_sessions": self._evicted,
        }

    def __len__(self) -> int:
        return len(self._turns)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._turns


def create_app(
    settings: Settings | None = None,
    service: ImplicitHealthTriageService | None = None,
) -> FastAPI:
    """Build the FastAPI app. Tests inject their own service."""

    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Section 48.4: an unusable guardrail config fails the startup rather
        # than silently shipping a demo without the safety fence.
        app.state.settings = settings
        app.state.service = service or build_service(settings)
        app.state.history = SessionHistory(
            settings.context_history_turns, settings.context_max_sessions
        )
        yield
        rails: GuardrailsRuntime | None = getattr(app.state.service, "guardrails", None)
        if rails is not None:
            await rails.aclose()
        reset_service()

    app = FastAPI(
        title="implicit-health-triage",
        version="0.1.0",
        description=(
            "隐式健康探针与用药安全技能：健康信号抽取 + NeMo Guardrails 生成前阻断。"
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.history = SessionHistory(
        settings.context_history_turns, settings.context_max_sessions
    )

    @app.get(
        "/health",
        summary="存活探针",
        description=(
            "返回服务状态、当前后端配置与会话缓冲占用。可用作启动探针。\n\n"
            "注意：`guardrails_enabled` 反映的是**配置**，不是围栏的实际就绪状态——"
            "围栏在首次请求时惰性初始化。"
        ),
    )
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "module": "implicit-health-triage",
            "llm_provider": settings.llm_provider,
            "guardrails_enabled": settings.guardrails_enabled,
            "sessions": app.state.history.stats(),
        }

    @app.delete(
        "/v1/session/{session_id}",
        summary="结束一个会话",
        description=(
            "清除该 `session_id` 的上下文缓冲。通话结束时调用，可立即释放内存，"
            "并避免同一 `session_id` 被复用时串上下文。\n\n"
            "会话缓冲本身有 LRU 上限（默认 200 个），不调用此接口也不会无限增长。"
        ),
    )
    async def drop_session(session_id: str) -> dict[str, Any]:
        existed = app.state.history.drop(session_id)
        if not existed:
            raise HTTPException(status_code=404, detail=f"unknown session_id: {session_id}")
        return {"session_id": session_id, "status": "dropped"}

    @app.post(
        "/v1/triage",
        response_model=TriageResponse | PartialIgnoredResponse,
        summary="处理一轮老人对话",
        description=(
            "输入老人一轮说话的**成句文本**（`is_final=true`），返回结构化健康信号、"
            "是否被用药安全围栏拦截，以及**可直接送 TTS 播报的回复**。\n\n"
            "### 拿到结果后怎么处理\n\n"
            "| `response_mode` | `response` | 调用方要做的 |\n"
            "|---|---|---|\n"
            "| `normal_chat` | 空串 `\"\"` | 走自己的闲聊回复，**空串不要播报** |\n"
            "| `health_care` | 1~2 句关切回复 | 直接送 TTS |\n"
            "| `medication_safety` | 固定安全话术（常量） | 直接送 TTS，**原样播报** |\n\n"
            "> ⚠️ 命中 `medication_safety` 后，**不得让任何其他模型补话**，"
            "否则整套安全围栏失效。该回复是模块级常量、永不经过模型生成。\n\n"
            "### 两种响应形状\n\n"
            "- 正常：含 `result` 字段\n"
            "- `is_final=false`：含 `status=\"ignored_partial\"`，无 `result`\n\n"
            "以**是否存在 `result` 字段**区分。"
        ),
        responses={
            200: {
                "description": "处理成功。两种可能形状见上方说明与 Examples。",
                "content": {
                    "application/json": {
                        "examples": {
                            "normal_chat": {
                                "summary": "无健康信息",
                                "value": {
                                    "session_id": "s1",
                                    "turn_id": "t1",
                                    "result": {
                                        "text": "今天楼下花开得挺漂亮。",
                                        "health_signal": {
                                            "type": "无",
                                            "detail": "",
                                            "severity": "轻微",
                                        },
                                        "guardrail_triggered": False,
                                        "response_mode": "normal_chat",
                                        "response": "",
                                        "rule_id": None,
                                    },
                                },
                            },
                            "health_care": {
                                "summary": "隐式健康信号",
                                "value": {
                                    "session_id": "s1",
                                    "turn_id": "t2",
                                    "result": {
                                        "text": "今天早上起来腿沉得很，买菜走两步就得歇着。",
                                        "health_signal": {
                                            "type": "身体不适",
                                            "detail": "下肢沉重/乏力",
                                            "severity": "中等",
                                        },
                                        "guardrail_triggered": False,
                                        "response_mode": "health_care",
                                        "response": "听起来您今天走路比平时费劲些。您今天平时该吃的药都按原来的安排吃了吗？",
                                        "rule_id": None,
                                    },
                                },
                            },
                            "medication_safety": {
                                "summary": "用药安全拦截（生成前阻断）",
                                "value": {
                                    "session_id": "s1",
                                    "turn_id": "t3",
                                    "result": {
                                        "text": "我降压药今天能不能吃两颗？",
                                        "health_signal": {
                                            "type": "用药",
                                            "detail": "涉及具体用药调整询问",
                                            "severity": "需留意",
                                        },
                                        "guardrail_triggered": True,
                                        "response_mode": "medication_safety",
                                        "response": "这个涉及具体的用药剂量，我不能替您决定增加、减少或停止服药。请先按照医生给您的处方或药品说明来服用，如果不确定，最好联系医生或药师确认。",
                                        "rule_id": "MEDICATION_CHANGE_REQUEST",
                                    },
                                },
                            },
                            "ignored_partial": {
                                "summary": "is_final=false 被忽略",
                                "value": {
                                    "session_id": "s1",
                                    "turn_id": "t4",
                                    "status": "ignored_partial",
                                },
                            },
                        }
                    }
                },
            }
        },
    )
    async def triage(request: TriageRequest) -> Any:
        # Section 31: an ASR partial is not a finished thought. "我这个药……"
        # must never trigger the final pipeline.
        if not request.is_final:
            return PartialIgnoredResponse(
                session_id=request.session_id,
                turn_id=request.turn_id,
            )

        history: list[str] = app.state.history.get(request.session_id)
        result = await app.state.service.triage(
            request.text,
            history=history,
            session_id=request.session_id,
            turn_id=request.turn_id,
        )
        app.state.history.add(request.session_id, request.text)

        return TriageResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            result=result,
        )

    return app


app = create_app()
