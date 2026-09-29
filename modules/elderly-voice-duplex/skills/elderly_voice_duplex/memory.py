"""把 skill3（长程记忆）接进语音链路。

语音服务原来只接了自己和 skill4，结果是**网页上说话没有记忆**——每次接通都是
第一次见老人。这条链路接上之后：

  1. 每轮回复前 `prepare_turn` 取回相关记忆，塞进 system prompt
  2. 挂断时 `close_session` 触发 skill3 的后台提炼，并等它跑完
  3. 建家属卡片时把 skill3 的 chronicle 传给 skill4，`近期回忆` 才有内容

和 family_card.py 一样是薄适配：用 skill3 自己的 Settings/MemoryService，
不自己拼字段、不改它的协议。skill3 不在时全部降级，通话照常。

四个踩过的坑（编排层实测，见 modules/orchestrator/orchestrator.py）：
  - `MemoryService.start()` 必须调，否则后台提炼 job 永远停在 queued
  - `close_session` 必须带 `expected_session_version`，否则提炼 job 根本不创建
  - `prepare_turn` 必须给 `context.query`，否则检索一个词都没有、永远 empty
  - `execute()` 的 `principal` 是**第三个位置参数**，且 request 必须带
    `request_id`；漏了直接 TypeError
"""
from __future__ import annotations

import logging
import os
import sys
import uuid

log = logging.getLogger("elderly-voice-duplex.memory")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
MODULES = os.path.join(PROJECT, "modules")
SKILL3_SRC = os.path.join(MODULES, "life-memoir-retriever", "src")
STATE_DIR = os.path.join(PROJECT, ".cache", "voice-duplex")

# 和编排层用不同的库文件：两边同时跑会互相覆盖对方的 session。
# 注意别把它做成模块常量：STATE_DIR 是可以换的（测试就用临时目录），
# 导入时算好的路径不会跟着变，换目录就会静默连到旧库上。
def _store_path() -> str:
    return os.path.join(STATE_DIR, "memory.sqlite3")

_svc = None
_tried = False
# elder_id -> Principal。skill3 的 execute 会校验 principal 覆盖的 user_ids，
# 共用一个"默认用户"的 principal 的话，换个 elder_id 就被判越权。
_principals: dict = {}
# session_id -> expected_session_version。必须是**每通话一份**：
# close_session 要用它，共用一个模块级变量的话两通电话会互相覆盖。
_versions: dict[str, int] = {}


def _paths_ready() -> bool:
    if not os.path.isdir(SKILL3_SRC):
        return False
    if SKILL3_SRC not in sys.path:
        sys.path.insert(0, SKILL3_SRC)
    return True


def _principal(elder_id: str):
    if elder_id not in _principals:
        from life_memoir.config import Principal
        _principals[elder_id] = Principal(
            "host", frozenset({elder_id}),
            frozenset({"host", "maintenance"}),
            frozenset({"consent", "review"}))
    return _principals[elder_id]


def _service():
    """懒加载 skill3。失败返回 None，调用方据此降级。

    服务是**进程级单例**，建起来就不再关：`close()` 会把 scheduler 一起收掉，
    下一通电话就报 "memory service is closed"（实测）。要关走 shutdown()。
    """
    global _svc, _tried
    if _svc is not None:
        return _svc
    if _tried:
        return None
    _tried = True
    if not _paths_ready():
        log.info("skill3 源码不在 %s，记忆功能不可用", SKILL3_SRC)
        return None
    try:
        from life_memoir.config import Settings
        from life_memoir.service import MemoryService

        os.makedirs(STATE_DIR, exist_ok=True)
        settings = Settings(
            storage_path=_store_path(),
            # fixture 后端只认文档里的 demo 句子，提不出真记忆；联调要打真模型
            backend="chat_completions",
            base_url=os.environ.get("EVD_LLM_BASE", "http://127.0.0.1:8000/v1"),
            model=os.environ.get("EVD_LLM_MODEL", "qwen3.6-35b-a3b"),
            location="local",
            # 这个模型思考很长，默认 20s/45s 会把提炼打成 MODEL_TIMEOUT（实测）
            timeout_seconds=120, total_budget_seconds=300)
        _svc = MemoryService(settings)
        log.info("skill3 已接入，记忆可用（库 %s）", _store_path())
    except Exception as exc:  # noqa: BLE401  接不上不影响通话
        log.info("skill3 接入失败，记忆不可用: %s: %s",
                 type(exc).__name__, exc)
        return None
    return _svc


def available() -> bool:
    """skill3 是否可用（给 /health 用）。"""
    return _service() is not None


async def start(elder_id: str, session_id: str) -> bool:
    """接通电话：开 session。失败只记日志，不影响通话。"""
    svc = _service()
    if svc is None:
        return False
    try:
        # 不 start() 的话 scheduler 不建，后台提炼 job 永远停在 queued
        await svc.start()
        await _set_policy(svc, elder_id)
        await _call(svc, elder_id, "open_session", user_id=elder_id,
                    session_id=session_id, locale="zh")
        return True
    except Exception as exc:  # noqa: BLE401
        log.info("skill3 开 session 失败（不影响通话）: %s: %s",
                 type(exc).__name__, exc)
        return False


async def _set_policy(svc, elder_id: str) -> bool:
    """开授权。失败要**说出来**：它静默失败的话 close_session 会因为
    "no_eligible_content" 跳过提炼，表现是"说了话但永远记不住"，而且
    一路都不报错，极难查。"""
    res = await _call(svc, elder_id, "set_policy", user_id=elder_id,
                      expected_version=0, consent_ref="consent",
                      grants={"long_term_memory": True,
                              "profile_learning": True,
                              "family_digest": True,
                              "remote_analysis": False})
    if res.get("status") == "ok":
        return True
    code = (res.get("error") or {}).get("code")
    if code == "VERSION_CONFLICT":
        # 上一通电话已经开过授权了，grant 内容也一样，幂等视为成功。
        # （skill3 没有 get_policy，拿不到当前版本号，只能这样兜。）
        return True
    log.warning("skill3 授权没设置成功: %s —— 这会让挂断时的提炼被跳过，"
                "记忆一条都不产出", res.get("error"))
    return False


async def prepare_turn(session_id: str, turn_id: str, transcript: str,
                       elder_id: str = "default") -> list[dict]:
    """取回这一轮相关的记忆。返回空列表表示没有或没接上。"""
    svc = _service()
    if svc is None:
        return []
    try:
        from datetime import datetime, timezone

        prepared = await _call(
            svc, elder_id, "prepare_turn", session_id=session_id,
            turn={"turn_id": turn_id, "speaker": "user", "is_final": True,
                  "occurred_at": datetime.now(timezone.utc).isoformat(),
                  "text_locale": "zh", "text": transcript},
            # 必须给 query：检索是拿 query 分词匹配记忆内容，不给就一个词
            # 都没有，永远 empty
            context={"query": transcript})
        data = prepared.get("data") or {}
        ctx = data.get("context") or {}
        log.info("prepare_turn 返回 status=%s match=%s memories=%d "
                 "preferences=%d version=%s",
                 prepared.get("status"), ctx.get("match_status"),
                 len(ctx.get("memories") or []),
                 len(ctx.get("preferences") or []),
                 (data.get("observation") or {}).get("session_version"))
        # story/observation 进 memories，preference 进 preferences——
        # 只读一个会漏另一类（"最喜欢看的电影是泰坦尼克号"是 preference）
        items = (ctx.get("memories") or []) + (ctx.get("preferences") or [])
        obs = data.get("observation") or {}
        if obs.get("session_version") is not None:
            _versions[session_id] = obs["session_version"]
        return items
    except Exception as exc:  # noqa: BLE401
        log.warning("skill3 取记忆失败（不影响本轮回复）: %s: %s",
                    type(exc).__name__, exc, exc_info=True)
        return []


async def close(elder_id: str, session_id: str,
                drain_seconds: float = 90.0) -> None:
    """挂断：关 session 触发后台提炼，并等它跑完。

    两步都不能少：不带 expected_session_version 的话提炼 job 根本不创建；
    不等它跑完就返回，job 永远停在 queued（实测 4 个 job 全卡住）。
    """
    svc = _service()
    if svc is None:
        return
    version = _versions.pop(session_id, None)
    try:
        if version is not None:
            res = await _call(svc, elder_id, "close_session",
                              session_id=session_id,
                              expected_session_version=version,
                              reason="completed")
            log.info("close_session -> status=%s error=%s",
                     res.get("status"),
                     (res.get("error") or {}).get("message"))
        else:
            log.info("close_session 跳过：没拿到 session_version")
        await _drain_jobs(svc, elder_id, drain_seconds)
    except Exception as exc:  # noqa: BLE401
        log.info("skill3 关 session 失败（不影响挂断）: %s: %s",
                 type(exc).__name__, exc)


# session_id -> 老人要求"讲给家属听"的轮次。必须是每通话一份：
# 共用一个模块级变量的话两通电话会互相覆盖。
_share_marks: dict[str, set[str]] = {}


def mark_share(session_id: str, turn_id: str) -> None:
    """标记这一轮要分享给家属。"""
    _share_marks.setdefault(session_id, set()).add(turn_id)


def shared_turns(session_id: str) -> list[str]:
    return sorted(_share_marks.get(session_id, ()))


def clear_share(session_id: str) -> None:
    _share_marks.pop(session_id, None)


async def _extraction_state(svc, elder_id: str, session_id: str) -> str:
    """本次通话的提炼 job 状态：ok / failed / none。

    「分享了一句但卡片没变化」最常见的成因就是提炼失败——条目是提炼创建
    的，提炼失败了就一个字都没有，后面全免谈。所以必须先说清楚这件事，
    不能只报"提升 0 条"（那听着像"没啥可分享"，把人往错方向引）。
    """
    try:
        res = await _call(svc, elder_id, "list_jobs", user_id=elder_id)
        jobs = (res.get("data") or {}).get("items") or []
        mine = [j for j in jobs
                if (j.get("snapshot") or {}).get("session_id") == session_id
                and j.get("kind") == "session_extract"]
        if not mine:
            return "none"
        if any(j.get("state") == "failed" for j in mine):
            return "failed"
        if any(j.get("state") in ("queued", "running") for j in mine):
            return "pending"
        return "ok"
    except Exception:  # noqa: BLE401  查不到状态不影响主流程
        return "unknown"


async def promote_shared(elder_id: str, session_id: str) -> dict:
    """把标了"分享"的轮次产出的条目提升为可分享。

    skill3 的隐私模型：条目默认 allowed_uses=["conversation"]，只有显式
    revise_entry(set_allowed_uses) 提升后才进 family_digest 视图；
    而 preference/observation 类条目**根本不进家属视图**（_allowed 里写死）。
    所以这里只提升 story/detail，偏好类会如实告诉调用方"这类不能分享"。

    必须在挂断后的提炼跑完再调——条目是那时候才创建的。
    """
    turns = _share_marks.get(session_id)
    if not turns:
        return {"promoted": 0, "skipped": 0, "entries": [], "note": "",
                "extraction": "none"}
    svc = _service()
    if svc is None:
        return {"promoted": 0, "skipped": 0, "entries": [],
                "note": "skill3 不可用", "extraction": "unknown"}
    extraction = await _extraction_state(svc, elder_id, session_id)
    try:
        ent = await _call(svc, elder_id, "list_entries", user_id=elder_id)
        items = (ent.get("data") or {}).get("items") or []
        wanted = {(session_id, t) for t in turns}
        promoted, skipped, names, unshared = 0, 0, [], []
        for e in items:
            refs = {(r.get("session_id"), r.get("turn_id"))
                    for r in e.get("source_refs") or []}
            if not refs & wanted:
                continue
            if "family_digest" in (e.get("allowed_uses") or []):
                continue                      # 已经分享过了
            if e.get("kind") not in ("story", "detail"):
                # 偏好/近况：skill4 的家属视图不展示这类，明说而不是假装成功
                skipped += 1
                unshared.append(e.get("content") or "")
                continue
            res = await _call(
                svc, elder_id, "revise_entry", entry_id=e["entry_id"],
                expected_version=e["version"], action="set_allowed_uses",
                allowed_uses=["conversation", "family_digest"],
                decision_ref="consent")
            if res.get("status") == "ok":
                promoted += 1
                names.append(e.get("content") or "")

        note = ""
        if extraction == "failed":
            note = ("提炼失败（skill3 后台 job 的模型输出没通过校验），"
                    "这一句没有存下来，让老人再说一遍即可")
        elif extraction == "pending":
            note = "提炼还没跑完，稍后卡片才会更新"
        elif not promoted and not skipped:
            note = "这一句没有产出可分享的条目"
        elif skipped:
            note = ("其中 %d 条是偏好/近况，skill4 的家属视图不展示这类"
                    % skipped)
        return {"promoted": promoted, "skipped": skipped,
                "entries": names, "note": note, "extraction": extraction,
                "unshared": unshared}
    except Exception as exc:  # noqa: BLE401
        log.warning("提升分享条目失败: %s: %s", type(exc).__name__, exc,
                    exc_info=True)
        return {"promoted": 0, "skipped": 0, "entries": [],
                "note": f"{type(exc).__name__}: {exc}",
                "extraction": extraction}


def shutdown() -> None:
    global _svc, _tried
    svc, _svc, _tried = _svc, None, False
    if svc is None:
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(svc.close())
    except Exception:  # noqa: BLE401  没有事件循环就只丢引用，让 GC 收
        pass


async def chronicle(elder_id: str = "default") -> dict | None:
    """skill3 的回忆摘要，喂给 skill4 的卡片。

    用 skill4 自己的 normalize_c_chronicle 转，不自己拼字段——它的
    degraded / not_ready 都会被转成 warnings 带进卡片，不会静默当成
    "没有回忆"。拿不到就返回 None。
    """
    svc = _service()
    if svc is None:
        return None
    try:
        # skill4 的源码路径自己挂，别依赖 family_card 先被导入——那样换个
        # 调用顺序就 ModuleNotFoundError，而这里又静默返回 None，极难查。
        skill4_src = os.path.join(MODULES, "family-digest-sync", "src")
        if os.path.isdir(skill4_src) and skill4_src not in sys.path:
            sys.path.insert(0, skill4_src)
        from family_digest.adapters.upstream import normalize_c_chronicle

        res = await _call(svc, elder_id, "get_chronicle", user_id=elder_id,
                          purpose="family_digest")
        if not res or not res.get("data"):
            return None
        # 两边的契约对不齐，得先对齐再交给她的适配器：
        #  - skill3 的 view_version 是 int，skill4 的 ChronicleInput 要 str
        #  - skill3 的 narrative 有时是 list，直接 str() 会变成 "[]"
        # 已报 skill4（她的 normalize_c_chronicle 声明接受"C 的真实 envelope"，
        # 但真实 envelope 过不了自己的校验）。
        raw = dict(res["data"])
        ver = raw.get("view_version")
        raw["view_version"] = str(ver) if ver not in (None, "") else None
        narrative = (raw.get("content") or {}).get("narrative")
        if isinstance(narrative, list):
            # 每条形如 {"text": "据本人讲述：…", "event_refs": [...], ...}。
            # 取 text 字段，不能 str() 整个 dict——那会把 Python repr
            # （"{'event_refs': ...}"）当成回忆正文显示给家属。
            raw = dict(raw)
            raw["content"] = dict(raw.get("content") or {})
            raw["content"]["narrative"] = "\n".join(
                x.get("text", "") if isinstance(x, dict) else str(x)
                for x in narrative)
        parsed = normalize_c_chronicle(raw)
        return parsed.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE401
        # 带堆栈：这里静默返回 None 的话，卡片只会显示"尚未生成"，
        # 排查时完全看不出是契约不符还是权限不够（实测吃过这个亏）。
        log.warning("skill3 取回忆摘要失败（不影响卡片）: %s: %s",
                    type(exc).__name__, exc, exc_info=True)
        return None


# ------------------------------------------------------------ 内部

async def _call(svc, elder_id: str, operation: str, **request):
    """统一走 skill3 的 execute。

    两个坑：`principal` 是**第三个位置参数**（漏了直接 TypeError），
    request 里必须带 `request_id`（它的失败回执要用）。
    """
    return await svc.execute(
        operation, {"request_id": uuid.uuid4().hex, **request},
        _principal(elder_id))


async def _drain_jobs(svc, elder_id: str, budget: float) -> None:
    """轮询 job 直到没有 running/queued 或超时。"""
    import asyncio
    import time
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        try:
            res = await _call(svc, elder_id, "list_jobs", user_id=elder_id)
            jobs = (res.get("data") or {}).get("items") or []
        except Exception:  # noqa: BLE401  读不到状态就别等了
            return
        if not any(j.get("state") in ("running", "queued") for j in jobs):
            return
        await asyncio.sleep(1.0)
