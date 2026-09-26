"""Single-process SQLite memory service with volatile turns and bounded workers."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from pydantic import ValidationError
from .backends import BackendError, make_backend
from .config import Settings, Principal, configured_decision
from .models import REQUESTS, READS, AnalysisResult
from .temporal import chronicle, markdown, parse_time


def token(prefix):
    return prefix + "-" + uuid.uuid4().hex[:20]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Fault(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


class MemoryService:
    def __init__(self, settings=None, *, backend=None, decision_verifier=configured_decision, clock=time.time):
        self.settings = settings or Settings()
        self.backend = backend or make_backend(self.settings)
        self.verify_decision = decision_verifier
        self.clock = clock
        self.lock = threading.RLock()
        self.buffers: dict[str, dict] = {}
        self.cache: dict[tuple, list] = {}
        self.foreground_busy = False
        self.tasks: set[asyncio.Task] = set()
        self.runner = None
        self.wake = asyncio.Event()
        self.closed = False
        self.last_sweep = 0.0
        self.model_semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        path = self.settings.storage_path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=0.1)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("CREATE TABLE IF NOT EXISTS objects (space TEXT NOT NULL, id TEXT NOT NULL, uid TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(space,id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS objects_user ON objects(space,uid)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, uid TEXT NOT NULL, result TEXT NOT NULL)")
        self.db.execute("PRAGMA user_version=1")
        self.db.commit()
        if path != ":memory:":
            os.chmod(path, 0o600)
        with self.db:
            for session in self._all("session"):
                if session["state"] == "open":
                    session.update(state="closed", input_lost=True)
                    self._put("session", session["session_id"], session["user_id"], session)
            for job in self._all("job"):
                if job["state"] in {"running", "queued"}:
                    if job["kind"] == "session_extract":
                        job.update(state="failed", failure={"code": "INPUT_UNAVAILABLE"})
                    else:
                        job.update(state="queued")
                    self._save_job(job)

    def _put(self, space, key, uid, payload):
        self.db.execute("INSERT INTO objects VALUES (?,?,?,?) ON CONFLICT(space,id) DO UPDATE SET uid=excluded.uid,payload=excluded.payload", (space, key, uid, encoded(payload)))

    def _get(self, space, key):
        row = self.db.execute("SELECT payload FROM objects WHERE space=? AND id=?", (space, key)).fetchone()
        return json.loads(row[0]) if row else None

    def _all(self, space, uid=None):
        rows = self.db.execute("SELECT payload FROM objects WHERE space=?" + (" AND uid=?" if uid else ""), (space, uid) if uid else (space,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _delete(self, space, key):
        self.db.execute("DELETE FROM objects WHERE space=? AND id=?", (space, key))

    def _user(self, uid):
        return self._get("user", uid) or {"user_id": uid, "policy_version": 0, "memory_revision": 0,
            "grants": {k: False for k in ("long_term_memory", "profile_learning", "family_digest", "remote_analysis")}}

    def _bump(self, uid):
        u = self._user(uid)
        u["memory_revision"] += 1
        self._put("user", uid, uid, u)
        self.cache = {k: v for k, v in self.cache.items() if k[0] != uid}
        for view in self._all("view", uid):
            self._delete("view", view["view_id"])
        return u["memory_revision"]

    def _own(self, space, key, principal):
        item = self._get(space, key)
        if item is None or item["user_id"] not in principal.user_ids:
            raise Fault("NOT_FOUND", 404)
        return item

    def _maintain(self, principal):
        if "maintenance" not in principal.roles:
            raise Fault("PERMISSION_DENIED", 403)

    def _decision(self, principal, uid, ref):
        self._maintain(principal)
        if not self.verify_decision(principal, uid, ref):
            raise Fault("PERMISSION_DENIED", 403)

    def _uid_for(self, req, principal):
        if "user_id" in req:
            uid = req["user_id"]
            if uid not in principal.user_ids:
                raise Fault("NOT_FOUND", 404)
            return uid
        for field, space in (("session_id", "session"), ("entry_id", "entry"), ("event_id", "entry"), ("job_id", "job")):
            if field in req:
                return self._own(space, req[field], principal)["user_id"]
        raise Fault("VALIDATION_ERROR")

    async def start(self):
        if self.runner is None:
            self.runner = asyncio.create_task(self._scheduler())
        return self

    async def close(self):
        self.closed = True
        self.wake.set()
        if self.runner:
            self.runner.cancel()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*(list(self.tasks) + ([self.runner] if self.runner else [])), return_exceptions=True)
        self.buffers.clear()
        self.db.close()

    def set_foreground_busy(self, busy: bool):
        self.foreground_busy = bool(busy)
        self.wake.set()

    async def execute(self, operation: str, request: dict, principal: Principal) -> dict:
        """Return the common envelope. HTTP status is separately derived by status_code."""
        started = time.perf_counter()
        if not isinstance(principal, Principal):
            return self._failure(request.get("request_id", "unknown"), "UNAUTHENTICATED", None)
        try:
            req = REQUESTS[operation].model_validate(request).model_dump(mode="json")
        except (KeyError, ValidationError):
            return self._failure(str(request.get("request_id", "unknown")), "VALIDATION_ERROR", None)
        result = await asyncio.to_thread(self._execute, operation, req, principal, started)
        self.wake.set()
        return result

    def _failure(self, rid, code, user):
        return {"api_version": "1.0", "request_id": rid, "status": "error", "data": None,
                "error": {"code": code, "message": code, "retryable": code in {"STORAGE_UNAVAILABLE", "SERVICE_BUSY"}},
                "meta": {"policy_version": user["policy_version"] if user else None,
                         "memory_revision": user["memory_revision"] if user else None, "replayed": False, "warnings": []}}

    def _execute(self, operation, req, principal, started):
        budget = req.get("deadline_ms", 5000) / 1000
        if not self.lock.acquire(timeout=max(0, budget - (time.perf_counter() - started))):
            return self._failure(req["request_id"], "SERVICE_BUSY", None)
        uid = None
        try:
            with self.db:
                self._sweep()
                uid = self._uid_for(req, principal)
                if "family" in principal.roles and operation not in {"build_context", "get_chronicle"}:
                    raise Fault("PERMISSION_DENIED", 403)
                key = hashlib.sha256(encoded([principal.caller_id, uid, operation, req["request_id"]]).encode()).hexdigest()
                fingerprint = hashlib.sha256(encoded(req).encode()).hexdigest()
                receipt = self.db.execute("SELECT fingerprint,result FROM receipts WHERE key=?", (key,)).fetchone() if operation not in READS else None
                replayed = receipt is not None
                if receipt and receipt[0] != fingerprint:
                    raise Fault("IDEMPOTENCY_CONFLICT", 409)
                if receipt:
                    data, status, warnings, extra = self._replay(operation, req, json.loads(receipt[1]), uid, principal, started)
                else:
                    data, status, warnings, extra = getattr(self, "_op_" + operation)(req, uid, principal, started)
                    if operation not in READS and status != "ignored":
                        # References only: no transcript or historical response bodies in receipts.
                        summary = {k: data[k] for k in ("session_id", "session_version", "turn_id", "speaker", "job_id", "entry_id", "event_id", "scope", "deleted_entry_ids", "invalidated_entry_ids") if k in data}
                        if operation == "prepare_turn":
                            summary = {k: v for k, v in data["observation"].items() if k in {"turn_id", "speaker", "session_version"}}
                        self.db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (key, fingerprint, uid, encoded(summary)))
                user = self._user(uid)
                meta = {"policy_version": user["policy_version"], "memory_revision": user["memory_revision"], "replayed": replayed, "warnings": warnings}
                meta.update(extra)
                if operation in {"prepare_turn", "build_context"}:
                    total = (time.perf_counter() - started) * 1000
                    meta.setdefault("timings", {"memory_ms": total})
                    meta["timings"]["total_ms"] = round(total, 3)
                return {"api_version": "1.0", "request_id": req["request_id"], "status": status, "data": data, "error": None, "meta": meta}
        except Fault as error:
            return self._failure(req["request_id"], error.code, self._user(uid) if uid else None)
        except sqlite3.Error:
            return self._failure(req["request_id"], "STORAGE_UNAVAILABLE", None)
        finally:
            self.lock.release()

    def _result(self, data, status="ok", warnings=None, extra=None):
        return data, status, warnings or [], extra or {}

    def _replay(self, op, req, receipt, uid, principal, started):
        if op == "set_policy":
            self._decision(principal, uid, req["consent_ref"])
            u = self._user(uid)
            return self._result({"policy_version": u["policy_version"], "grants": u["grants"]})
        if op in {"open_session", "close_session"}:
            s = self._get("session", req["session_id"])
            if op == "open_session":
                return self._result({k: s[k] for k in ("session_id", "session_version", "state", "locale", "reply_locale")})
            job = self._get("job", receipt.get("job_id", ""))
            return self._result({"session_version": s["session_version"], "job_id": receipt.get("job_id"), "state": job["state"] if job else "skipped"})
        if op in {"observe_turn", "prepare_turn"}:
            s = self._get("session", req["session_id"])
            b = self.buffers.get(req["session_id"])
            observation = {**receipt, "storage": "volatile", "effective_controls": copy.deepcopy(b["controls"]) if b else {}, "state": s["state"]}
            if op == "observe_turn":
                return self._result(observation)
            ctx, status, warnings, extra = self._context({**req["context"], "session_id": req["session_id"], "purpose": "conversation", "deadline_ms": req["deadline_ms"]}, uid, principal, started)
            return self._result({"observation": observation, "context": ctx}, status, warnings, extra)
        if op == "forget_entries":
            self._decision(principal, uid, req["decision_ref"])
            return self._result({**receipt, "memory_revision": self._user(uid)["memory_revision"]})
        if op in {"revise_entry", "review_chronicle_event"}:
            self._decision(principal, uid, req["decision_ref"])
            e = self._get("entry", req.get("entry_id", req.get("event_id")))
            return self._result({"entry": e, "state": "current" if e else "deleted"})
        return self._result(receipt)

    def _op_set_policy(self, r, uid, p, started):
        self._decision(p, uid, r["consent_ref"])
        u = self._user(uid)
        if u["policy_version"] != r["expected_version"]:
            raise Fault("VERSION_CONFLICT", 409)
        u.update(grants=r["grants"], policy_version=u["policy_version"] + 1)
        self._put("user", uid, uid, u)
        self._bump(uid)
        self._cancel_jobs(uid, reason="POLICY_CHANGED")
        self._schedule_history(uid)
        return self._result({"policy_version": u["policy_version"], "grants": u["grants"]})

    def _op_open_session(self, r, uid, p, started):
        if "host" not in p.roles:
            raise Fault("PERMISSION_DENIED", 403)
        if self._get("session", r["session_id"]):
            raise Fault("VERSION_CONFLICT", 409)
        s = {"session_id": r["session_id"], "user_id": uid, "session_version": 1, "state": "open",
             "locale": r["locale"], "reply_locale": r["reply_locale"] or r["locale"], "updated_at": self.clock(), "forgotten": False}
        self._put("session", s["session_id"], uid, s)
        self.buffers[s["session_id"]] = {"turns": [], "controls": {}, "conditions": r["conditions"], "updated_at": self.clock()}
        self._visible(uid, "conversation", cache=True)
        return self._result({**{k: s[k] for k in ("session_id", "session_version", "state", "locale", "reply_locale")}, "policy_version": self._user(uid)["policy_version"]})

    def _observe(self, sid, turn, uid, p):
        if "host" not in p.roles:
            raise Fault("PERMISSION_DENIED", 403)
        if not turn["is_final"]:
            return None
        s = self._get("session", sid)
        if s["state"] != "open" or sid not in self.buffers:
            raise Fault("SESSION_CLOSED", 409)
        b = self.buffers[sid]
        same = next((t for t in b["turns"] if t["turn_id"] == turn["turn_id"] and t["speaker"] == turn["speaker"]), None)
        if same:
            if same["fingerprint"] != hashlib.sha256(encoded(turn).encode()).hexdigest():
                raise Fault("TURN_CONFLICT", 409)
        else:
            if len(b["turns"]) >= self.settings.max_session_turns or sum(len(t["text"]) for t in b["turns"]) + len(turn["text"]) > self.settings.max_session_chars:
                raise Fault("SESSION_CAPACITY_REACHED", 409)
            if turn["speaker"] == "assistant" and not any(t["speaker"] == "user" and t["turn_id"] == turn["turn_id"] for t in b["turns"]):
                raise Fault("VALIDATION_ERROR")
            # IDs in persisted references stay short regardless of client ID length.
            t = {**turn, "source_id": token("turn"), "fingerprint": hashlib.sha256(encoded(turn).encode()).hexdigest()}
            if turn.get("controls"):
                controls = turn["controls"]
                b["controls"]["do_not_persist"] = controls["do_not_persist"]
                if controls["stop_session"]:
                    b["controls"]["stop_session"] = True
                if controls["skip_topic"]:
                    b["controls"]["skip_topic"] = controls["skip_topic"]
            if turn.get("conditions"):
                b["conditions"] = turn["conditions"]
            b["turns"].append(t)
            b["updated_at"] = self.clock()
            s["session_version"] += 1
            s["updated_at"] = self.clock()
            self._put("session", sid, uid, s)
        return {"turn_id": turn["turn_id"], "speaker": turn["speaker"], "session_version": s["session_version"], "storage": "volatile", "effective_controls": copy.deepcopy(b["controls"])}

    def _op_observe_turn(self, r, uid, p, started):
        observation = self._observe(r["session_id"], {k: v for k, v in r.items() if k not in {"request_id", "session_id"}}, uid, p)
        return self._result(observation or {}, "ok" if observation else "ignored")

    def _op_prepare_turn(self, r, uid, p, started):
        observation = self._observe(r["session_id"], r["turn"], uid, p)
        if observation is None:
            return self._result({"observation": None, "context": None}, "ignored", extra={"timings": {"memory_ms": 0}, "cache_status": "bypass", "retrieval_complete": False, "deadline_exceeded": False})
        ctx, status, warnings, extra = self._context({**r["context"], "session_id": r["session_id"], "purpose": "conversation", "deadline_ms": r["deadline_ms"]}, uid, p, started)
        return self._result({"observation": observation, "context": ctx}, status, warnings, extra)

    def _allowed(self, entry, user, purpose):
        grant = "profile_learning" if entry["kind"] in {"observation", "preference"} else "long_term_memory"
        if not user["grants"][grant] or purpose not in entry["allowed_uses"] or entry.get("expires_at") and entry["expires_at"] <= self.clock():
            return False
        if purpose == "family_digest" and (not user["grants"]["family_digest"] or entry["kind"] in {"preference", "observation"}):
            return False
        return entry["record_status"] != "superseded"

    def _visible(self, uid, purpose, cache=False):
        u = self._user(uid)
        key = (uid, purpose, u["policy_version"], u["memory_revision"])
        if key in self.cache:
            cached = self.cache[key]
            if all(self._allowed(e, u, purpose) for e in cached):
                return copy.deepcopy(cached), "hit"
            del self.cache[key]
        entries = [e for e in self._all("entry", uid) if self._allowed(e, u, purpose)]
        available = {e["entry_id"] for e in entries}
        # Dependency-derived facts cannot survive removal of supporting observations.
        while True:
            kept = [e for e in entries if set(e.get("dependency_entry_ids", [])).issubset(available)]
            if len(kept) == len(entries):
                break
            entries = kept
            available = {e["entry_id"] for e in entries}
        if cache:
            self.cache[key] = copy.deepcopy(entries)
        return entries, "miss"

    def _context(self, r, uid, p, started):
        purpose = r["purpose"]
        u = self._user(uid)
        if "family" in p.roles and purpose != "family_digest":
            raise Fault("PERMISSION_DENIED", 403)
        if purpose == "family_digest" and not u["grants"]["family_digest"]:
            raise Fault("PERMISSION_DENIED", 403)
        b = self.buffers.get(r["session_id"], {})
        state = {} if purpose == "family_digest" else {"controls": copy.deepcopy(b.get("controls", {})), "conditions": b.get("conditions", {})}
        current = next((t for t in reversed(b.get("turns", [])) if t["speaker"] == "user"), None)
        if current and purpose == "conversation":
            state["current_text"] = current["text"][:min(500, r["max_chars"] // 3)]
        out = {"session_state": state, "memories": [], "preferences": [], "review_suggestions": [], "interaction_hints": [], "match_status": "empty"}
        stop = state.get("controls", {}).get("stop_session", False)
        deadline = started + r["deadline_ms"] / 1000
        entries, cache_status = self._visible(uid, purpose, cache=True)
        warnings = []
        timed_out = time.perf_counter() >= deadline
        query = r.get("query", "")
        words = set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", query.lower()))
        def score(e):
            return (sum(1 for w in words if w in e["content"].lower()), e["updated_at"])
        entries.sort(key=score, reverse=True)
        used = len(encoded(state))
        count = 0
        if not stop:
            for e in entries:
                if time.perf_counter() >= deadline:
                    timed_out = True
                    break
                if e["kind"] == "observation":
                    continue
                if any(state.get("conditions", {}).get(k) != v for k, v in e.get("conditions", {}).items()):
                    continue
                if e["kind"] not in {"preference", "recent"} and (not words or score(e)[0] == 0):
                    continue
                if e["record_status"] == "pending":
                    item = {"entry_id": e["entry_id"], "question": "以后希望继续采用这种交流方式吗？", "source_refs": e["source_refs"]}
                    target = "review_suggestions"
                else:
                    fields = ("entry_id", "kind", "content", "content_locale", "basis", "source_refs", "version", "conditions")
                    item = {k: e[k] for k in fields}
                    item["expires_at"] = stamp(e["expires_at"]) if e.get("expires_at") else None
                    target = "preferences" if e["kind"] == "preference" else "memories"
                if purpose == "family_digest" and target != "memories":
                    continue
                if count >= r["limit"] or used + len(encoded(item)) > r["max_chars"]:
                    continue
                out[target].append(item)
                used += len(encoded(item))
                count += 1
        if stop:
            out["match_status"] = "not_searched"
        else:
            out["match_status"] = "partial" if timed_out and count else "not_searched" if timed_out else "matched" if count else "empty"
        if timed_out:
            warnings.append("CONTEXT_DEADLINE_EXCEEDED")
        if r.get("context_locale", "original") != "original" and any(e["content_locale"] != r["context_locale"] for group in ("memories", "preferences") for e in out[group]):
            warnings.append("LANGUAGE_VARIANT_UNAVAILABLE")
        extra = {"timings": {"memory_ms": round((time.perf_counter() - started) * 1000, 3)}, "cache_status": cache_status,
                 "retrieval_complete": not timed_out and not stop, "deadline_exceeded": timed_out}
        return self._result(out, "degraded" if warnings else "ok", warnings, extra)

    def _op_build_context(self, r, uid, p, started):
        return self._context(r, uid, p, started)

    def _op_close_session(self, r, uid, p, started):
        if "host" not in p.roles:
            raise Fault("PERMISSION_DENIED", 403)
        s = self._get("session", r["session_id"])
        if s["state"] != "open":
            raise Fault("SESSION_CLOSED", 409)
        if s["session_version"] != r["expected_session_version"]:
            raise Fault("VERSION_CONFLICT", 409)
        s.update(state="closed", session_version=s["session_version"] + 1, closed_at=self.clock())
        self._put("session", s["session_id"], uid, s)
        u = self._user(uid)
        b = self.buffers.get(s["session_id"], {})
        # Conservative first release: one no-save turn excludes the whole session
        # from persistence, including later references to that protected content.
        forbidden = any(t.get("controls", {}).get("do_not_persist", False) for t in b.get("turns", []) if t.get("controls"))
        eligible = (u["grants"]["long_term_memory"] or u["grants"]["profile_learning"]) and bool(b.get("turns")) and not forbidden
        if not eligible:
            self.buffers.pop(s["session_id"], None)
            return self._result({"job_id": None, "state": "skipped", "reason": "no_eligible_content", "session_version": s["session_version"]})
        job = self._enqueue(uid, "session_extract", session=s)
        if job["state"] == "failed":
            self.buffers.pop(s["session_id"], None)
        return self._result({"job_id": job["job_id"], "state": job["state"], "session_version": s["session_version"]}, "accepted" if job["state"] == "queued" else "ok")

    def _op_get_job(self, r, uid, p, started):
        self._maintain(p)
        return self._result(self._public_job(self._get("job", r["job_id"])))

    def _page(self, values, r):
        try:
            offset = int(r["cursor"]) if r.get("cursor") is not None else 0
            if offset < 0:
                raise ValueError()
        except ValueError:
            raise Fault("VALIDATION_ERROR") from None
        end = offset + r["limit"]
        return {"items": values[offset:end], "next_cursor": str(end) if end < len(values) else None}

    def _op_list_jobs(self, r, uid, p, started):
        self._maintain(p)
        values = [self._public_job(j) for j in self._all("job", uid) if (not r["kind"] or j["kind"] == r["kind"]) and (not r["state"] or j["state"] == r["state"])]
        return self._result(self._page(values, r))

    def _op_list_entries(self, r, uid, p, started):
        self._maintain(p)
        entries = [e for e in self._all("entry", uid) if (not r["kind"] or e["kind"] == r["kind"]) and (not r["record_status"] or e["record_status"] == r["record_status"])]
        for e in entries:
            e["evidence"] = [self._get("evidence", ref["evidence_id"]) for ref in e["source_refs"]]
        return self._result(self._page(entries, r))

    def _op_revise_entry(self, r, uid, p, started):
        self._decision(p, uid, r["decision_ref"])
        e = self._get("entry", r["entry_id"])
        if e["version"] != r["expected_version"]:
            raise Fault("VERSION_CONFLICT", 409)
        if r["action"] == "correct_content":
            e["content"] = r["content"]
            # Keep original evidence separate; the verified correction is a new source.
            ev = self._correction_evidence(uid, e, r["content"], r["decision_ref"])
            e["source_refs"] = [ev]
            e["time_assertions"] = parse_time(e["content"], ev["evidence_id"], {})
            e.update(basis="self_report", record_status="active", dependency_entry_ids=[])
            e.pop("time_rejected", None)
            e.pop("time_confirmation", None)
        elif r["action"] == "confirm_preference":
            if e["kind"] != "preference" or e["record_status"] != "pending":
                raise Fault("VALIDATION_ERROR")
            e.update(record_status="active", basis="self_report", confirmed_by=r["decision_ref"], expires_at=None)
        else:
            uses = sorted(set(r["allowed_uses"]))
            if "family_digest" in uses and (not self._user(uid)["grants"]["family_digest"] or e["kind"] in {"observation", "preference"}):
                raise Fault("PERMISSION_DENIED", 403)
            e["allowed_uses"] = uses
        e["version"] += 1
        e["updated_at"] = self.clock()
        self._put("entry", e["entry_id"], uid, e)
        self._invalidate(uid, {e["entry_id"]})
        self._schedule_history(uid)
        return self._result({"entry": e, "version": e["version"]})

    def _correction_evidence(self, uid, entry, text, decision_ref):
        ev_id = token("evidence")
        ev = {"evidence_id": ev_id, "user_id": uid, "session_id": None, "turn_id": None,
              "quote": text, "text_locale": entry["content_locale"], "decision_ref": decision_ref, "version": 1}
        self._put("evidence", ev_id, uid, ev)
        return {k: ev[k] for k in ("evidence_id", "session_id", "turn_id")}

    def _op_forget_entries(self, r, uid, p, started):
        self._decision(p, uid, r["decision_ref"])
        if r["scope"] == "session":
            s = self._own("session", r["session_id"], p)
            if s["user_id"] != uid:
                raise Fault("NOT_FOUND", 404)
            selected = {e["entry_id"] for e in self._all("entry", uid) if any(ref["session_id"] == r["session_id"] for ref in e["source_refs"])}
            s.update(forgotten=True, state="closed")
            self._put("session", s["session_id"], uid, s)
            self.buffers.pop(s["session_id"], None)
            self._cancel_jobs(uid, session_id=s["session_id"], reason="SOURCE_FORGOTTEN")
        else:
            selected = set(r["entry_ids"])
            for eid in selected:
                e = self._own("entry", eid, p)
                if e["user_id"] != uid:
                    raise Fault("NOT_FOUND", 404)
        deleted = self._remove_entries(uid, selected)
        self._schedule_history(uid)
        return self._result({"deleted_entry_ids": sorted(deleted), "invalidated_entry_ids": [], "memory_revision": self._user(uid)["memory_revision"], "scope": r["scope"]})

    def _remove_entries(self, uid, selected):
        entries = self._all("entry", uid)
        deleted = set(selected)
        while True:
            extra = {e["entry_id"] for e in entries if set(e.get("dependency_entry_ids", [])) & deleted}
            if extra.issubset(deleted):
                break
            deleted |= extra
        for eid in deleted:
            self._delete("entry", eid)
        referenced = {r["evidence_id"] for e in self._all("entry", uid) for r in e["source_refs"]}
        for evidence in self._all("evidence", uid):
            if evidence["evidence_id"] not in referenced:
                self._delete("evidence", evidence["evidence_id"])
        # Do not let active raw buffers reintroduce deleted personal content.
        affected_sessions = {ref["session_id"] for e in entries if e["entry_id"] in deleted for ref in e["source_refs"] if ref["session_id"]}
        for sid in affected_sessions:
            self.buffers.pop(sid, None)
            s = self._get("session", sid)
            if s:
                s.update(forgotten=True, state="closed")
                self._put("session", sid, uid, s)
        self._invalidate(uid, deleted)
        return deleted

    def _invalidate(self, uid, ids):
        self._bump(uid)
        self._cancel_jobs(uid, entry_ids=ids, reason="SOURCE_CHANGED")

    def _op_get_chronicle(self, r, uid, p, started):
        if "family" in p.roles and r["purpose"] != "family_digest":
            raise Fault("PERMISSION_DENIED", 403)
        u = self._user(uid)
        if not u["grants"]["long_term_memory"] or r["purpose"] == "family_digest" and not u["grants"]["family_digest"]:
            raise Fault("PERMISSION_DENIED", 403)
        view = self._get("view", uid + ":" + r["purpose"])
        if view:
            visible_ids = {e["entry_id"] for e in self._visible(uid, r["purpose"])[0]}
            dependencies = {eid for item in view["content"]["items"] for a in item["time_resolution"]["alternatives"] for eid in a["dependency_entry_ids"]}
            if not dependencies.issubset(visible_ids):
                view = None
        if not view or view["memory_revision"] != u["memory_revision"] or view["policy_version"] != u["policy_version"]:
            return self._result({"state": "not_ready", "view_version": None, "based_on_memory_revision": None, "format": r["format"], "content": None, "warnings": ["CHRONICLE_NOT_READY"]}, "degraded", ["CHRONICLE_NOT_READY"])
        content = view["content"] if r["format"] == "structured" else markdown(view["content"])
        if len(encoded(content)) > self.settings.max_chronicle_chars:
            raise Fault("CHRONICLE_TOO_LARGE")
        return self._result({"state": "ready", "view_version": view["memory_revision"], "based_on_memory_revision": view["memory_revision"], "format": r["format"], "content": content, "warnings": []})

    def _op_review_chronicle_event(self, r, uid, p, started):
        self._decision(p, uid, r["decision_ref"])
        e = self._get("entry", r["event_id"])
        if e["kind"] not in {"story", "detail"}:
            raise Fault("VALIDATION_ERROR")
        if e["version"] != r["expected_event_version"]:
            raise Fault("VERSION_CONFLICT", 409)
        old_views = [v["memory_revision"] for v in self._all("view", uid)]
        if r["action"] == "correct_time":
            ev = self._correction_evidence(uid, e, r["time_text"], r["decision_ref"])
            e["source_refs"].append(ev)
            e["time_assertions"] = parse_time(r["time_text"], ev["evidence_id"], {})
            e.pop("time_rejected", None)
        else:
            view = self._get("view", uid + ":conversation")
            current = next((x for x in view["content"]["items"] if x["event_id"] == e["entry_id"]), None) if view else None
            if not current or current["time_resolution"]["version"] != r["resolution_version"] or not any(a["alternative_id"] == r["alternative_id"] for a in current["time_resolution"]["alternatives"]):
                raise Fault("VERSION_CONFLICT", 409)
            if r["action"] == "reject_inference":
                e["time_rejected"] = True
            else:
                e["time_confirmation"] = {"decision_ref": r["decision_ref"], "alternative_id": r["alternative_id"], "resolution_version": r["resolution_version"]}
        e["version"] += 1
        self._put("entry", e["entry_id"], uid, e)
        self._invalidate(uid, {e["entry_id"]})
        jobs = self._schedule_history(uid)
        return self._result({"event_id": e["entry_id"], "event_version": e["version"], "time_state": "pending_rebuild", "invalidated_view_versions": sorted(set(old_views)), "queued_job_ids": jobs})

    def _sweep(self):
        now = self.clock()
        if now - self.last_sweep < 1:
            return
        self.last_sweep = now
        for uid in {e["user_id"] for e in self._all("entry") if e.get("expires_at") and e["expires_at"] <= now}:
            expired = {e["entry_id"] for e in self._all("entry", uid) if e.get("expires_at") and e["expires_at"] <= now}
            self._remove_entries(uid, expired)
            self._schedule_history(uid)
        for sid, b in list(self.buffers.items()):
            s = self._get("session", sid)
            deadline = s.get("closed_at", now) + self.settings.total_budget_seconds if s["state"] == "closed" else b["updated_at"] + self.settings.session_idle_seconds
            if now >= deadline:
                self.buffers.pop(sid, None)
                if s["state"] == "open":
                    s.update(state="closed", input_lost=True)
                    self._put("session", sid, s["user_id"], s)
                self._cancel_jobs(s["user_id"], session_id=sid, reason="INPUT_UNAVAILABLE")

    def _save_job(self, job):
        self._put("job", job["job_id"], job["user_id"], job)

    def _public_job(self, job):
        return {k: v for k, v in job.items() if k not in {"user_id", "dedup_key", "deadline", "source_ids"}}

    def _enqueue(self, uid, kind, *, session=None, depends_on=None):
        u = self._user(uid)
        sources = [] if session else self._visible(uid, "conversation")[0]
        snapshot = {"policy_version": u["policy_version"], "memory_revision": u["memory_revision"],
            "session_id": session["session_id"] if session else None, "session_version": session["session_version"] if session else None,
            "source_entry_versions": [{"entry_id": e["entry_id"], "version": e["version"]} for e in sources]}
        dedup = hashlib.sha256(encoded([uid, kind, snapshot]).encode()).hexdigest()
        for j in self._all("job", uid):
            if j["dedup_key"] == dedup and j["state"] in {"queued", "running", "completed"}:
                return j
        pending = sum(j["state"] in {"queued", "running"} for j in self._all("job"))
        job = {"job_id": token("job"), "user_id": uid, "kind": kind, "state": "queued" if pending < self.settings.max_pending_jobs else "failed",
            "depends_on": depends_on or [], "snapshot": snapshot, "published_revision": None,
            "created_entry_ids": [], "updated_entry_ids": [], "skipped_reasons": [],
            "failure": None if pending < self.settings.max_pending_jobs else {"code": "BACKGROUND_QUEUE_FULL"},
            "dedup_key": dedup, "created_at": self.clock(), "deadline": self.clock() + self.settings.total_budget_seconds,
            "timings": {}, "warnings": []}
        self._save_job(job)
        return job

    def _schedule_history(self, uid, include_preference=True):
        u = self._user(uid)
        if not (u["grants"]["long_term_memory"] or u["grants"]["profile_learning"]):
            return []
        # Derived jobs use stable records; none retain the volatile transcript.
        kinds = ["context_prepare"]
        if u["grants"]["long_term_memory"]:
            kinds += ["story_reconcile", "chronicle_build"]
        if u["grants"]["profile_learning"] and include_preference:
            kinds += ["preference_reconcile"]
        result = []
        story_id = None
        for kind in kinds:
            j = self._enqueue(uid, kind, depends_on=[story_id] if kind == "chronicle_build" and story_id else [])
            result.append(j["job_id"])
            if kind == "story_reconcile":
                story_id = j["job_id"]
        return result

    def _cancel_jobs(self, uid, *, entry_ids=None, session_id=None, reason):
        for j in self._all("job", uid):
            if j["state"] not in {"running", "queued"}:
                continue
            deps = {x["entry_id"] for x in j["snapshot"]["source_entry_versions"]}
            if entry_ids is None and session_id is None or entry_ids is not None and deps & entry_ids or session_id and j["snapshot"]["session_id"] == session_id:
                j.update(state="cancelled", skipped_reasons=[reason])
                self._save_job(j)

    def _valid_job(self, j):
        if j["state"] not in {"queued", "running"} or self._user(j["user_id"])["policy_version"] != j["snapshot"]["policy_version"]:
            return False
        sid = j["snapshot"]["session_id"]
        if sid:
            s = self._get("session", sid)
            return bool(s and not s["forgotten"] and s["session_version"] == j["snapshot"]["session_version"] and sid in self.buffers)
        return all((e := self._get("entry", s["entry_id"])) and e["version"] == s["version"] for s in j["snapshot"]["source_entry_versions"])

    async def _scheduler(self):
        while True:
            with self.lock, self.db:
                self._sweep()
                for j in self._all("job"):
                    if j["state"] != "queued":
                        continue
                    if self.clock() >= min(j["deadline"], j["created_at"] + self.settings.max_queue_wait_seconds):
                        j.update(state="failed", failure={"code": "QUEUE_TIMEOUT"})
                        self._save_job(j)
                        if j["kind"] == "session_extract":
                            self.buffers.pop(j["snapshot"]["session_id"], None)
                        continue
                    dependencies = [self._get("job", jid) for jid in j["depends_on"]]
                    if any(d is None or d["state"] in {"failed", "cancelled"} for d in dependencies):
                        j.update(state="cancelled", skipped_reasons=["DEPENDENCY_FAILED"])
                        self._save_job(j)
                        continue
                    if any(d["state"] not in {"completed", "skipped"} for d in dependencies):
                        continue
                    model_job = j["kind"] in {"session_extract", "preference_reconcile"}
                    if model_job and self.settings.foreground_priority and self.foreground_busy:
                        continue
                    if len(self.tasks) >= self.settings.max_concurrency + 3:
                        break
                    j.update(state="running")
                    j["timings"]["queue_ms"] = round((self.clock() - j["created_at"]) * 1000, 3)
                    self._save_job(j)
                    task = asyncio.create_task(self._run_job(j["job_id"]))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _run_job(self, job_id):
        begin = time.perf_counter()
        try:
            with self.lock:
                job = self._get("job", job_id)
                if not self._valid_job(job):
                    raise BackendError("SOURCE_CHANGED")
                uid = job["user_id"]
                remaining = job["deadline"] - self.clock()
                existing = self._visible(uid, "conversation")[0]
                turns = copy.deepcopy(self.buffers[job["snapshot"]["session_id"]]["turns"]) if job["kind"] == "session_extract" else []
                u = self._user(uid)
            async with asyncio.timeout(max(0.001, remaining)):
                result = None
                if job["kind"] in {"session_extract", "preference_reconcile"}:
                    if self.backend.location == "remote" and not u["grants"]["remote_analysis"]:
                        raise BackendError("REMOTE_ANALYSIS_NOT_ALLOWED")
                    if job["kind"] == "preference_reconcile":
                        existing = [e for e in existing if e["kind"] in {"preference", "observation"}]
                    inputs = {"eligible_turns": [{k: v for k, v in t.items() if k != "fingerprint"} for t in turns],
                              "existing_entries": existing, "existing_evidence": [],
                              "allowed_categories": {"stories": u["grants"]["long_term_memory"], "profiles": u["grants"]["profile_learning"]}}
                    with self.lock:
                        evidence_ids = {r["evidence_id"] for e in existing for r in e["source_refs"]}
                        inputs["existing_evidence"] = [self._get("evidence", eid) for eid in evidence_ids]
                    async with self.model_semaphore:
                        # Recheck immediately before sending, after any semaphore wait.
                        with self.lock:
                            if not self._valid_job(self._get("job", job_id)):
                                raise BackendError("SOURCE_CHANGED")
                        result = await self.backend.analyze("extract_candidates" if turns else "reconcile_preferences", inputs)
                        result = AnalysisResult.model_validate(result)
                elif job["kind"] == "chronicle_build":
                    # Composition is intentionally grounded/deterministic in 0.1.0.
                    views = {"conversation": await asyncio.to_thread(chronicle, existing)}
                    if u["grants"]["family_digest"]:
                        with self.lock:
                            family_entries = self._visible(uid, "family_digest")[0]
                        views["family_digest"] = await asyncio.to_thread(chronicle, family_entries)
                else:
                    await asyncio.sleep(0)
                with self.lock, self.db:
                    current = self._get("job", job_id)
                    if not self._valid_job(current):
                        raise BackendError("SOURCE_CHANGED")
                    if result is not None:
                        created, updated = self._commit_candidates(uid, current, result, turns, existing)
                        current.update(created_entry_ids=created, updated_entry_ids=updated, warnings=result.warnings)
                        if created or updated:
                            current["published_revision"] = self._bump(uid)
                    elif job["kind"] == "chronicle_build":
                        latest = self._user(uid)
                        if latest["memory_revision"] != job["snapshot"]["memory_revision"]:
                            raise BackendError("SOURCE_CHANGED")
                        for purpose, content in views.items():
                            self._put("view", uid + ":" + purpose, uid, {"view_id": uid + ":" + purpose, "user_id": uid,
                                      "memory_revision": latest["memory_revision"], "policy_version": latest["policy_version"], "content": content})
                    elif job["kind"] == "context_prepare":
                        self._visible(uid, "conversation", cache=True)
                    current.update(state="completed")
                    current["timings"]["run_ms"] = round((time.perf_counter() - begin) * 1000, 3)
                    self._save_job(current)
                    if job["kind"] == "session_extract":
                        self.buffers.pop(job["snapshot"]["session_id"], None)
                    if current["published_revision"] is not None:
                        self._schedule_history(uid, include_preference=job["kind"] != "preference_reconcile")
        except asyncio.CancelledError:
            raise
        except (TimeoutError, BackendError, ValidationError) as error:
            code = "MODEL_TIMEOUT" if isinstance(error, TimeoutError) else error.code if isinstance(error, BackendError) else "MODEL_OUTPUT_INVALID"
            with self.lock, self.db:
                job = self._get("job", job_id)
                if job["state"] == "running":
                    job.update(state="cancelled" if code == "SOURCE_CHANGED" else "failed", failure=None if code == "SOURCE_CHANGED" else {"code": code}, skipped_reasons=[code] if code == "SOURCE_CHANGED" else [])
                    self._save_job(job)
                if job["kind"] == "session_extract":
                    self.buffers.pop(job["snapshot"]["session_id"], None)
                elif code == "SOURCE_CHANGED":
                    # The mutation already scheduled learning, or was itself a
                    # learning result. Rebuild views without creating a feedback loop.
                    self._schedule_history(job["user_id"], include_preference=False)
        except Exception:
            # Do not log provider payloads, source text, or secrets on unexpected errors.
            with self.lock, self.db:
                job = self._get("job", job_id)
                if job["state"] == "running":
                    job.update(state="failed", failure={"code": "BACKGROUND_INTERNAL_ERROR"})
                    self._save_job(job)
                if job["kind"] == "session_extract":
                    self.buffers.pop(job["snapshot"]["session_id"], None)
        finally:
            self.wake.set()

    def _commit_candidates(self, uid, job, result, turns, existing):
        u = self._user(uid)
        sources = {t["source_id"]: t for t in turns if t["speaker"] == "user"}
        old = {e["entry_id"]: e for e in existing}
        historical = {ev["evidence_id"]: ev for e in existing for r in e["source_refs"] if (ev := self._get("evidence", r["evidence_id"]))}
        prepared = []
        local_ids = {}
        for candidate in result.candidates:
            c = candidate.model_dump()
            if c["local_id"] in local_ids:
                raise BackendError()
            if job["kind"] == "preference_reconcile" and c["kind"] != "preference":
                raise BackendError()
            if c["basis"] == "family_report":
                # This input contract carries elder/assistant turns only.
                raise BackendError("UNSUPPORTED_SOURCE_ROLE")
            if c["temporal_scope"] == "session":
                continue
            grant = "profile_learning" if c["kind"] in {"preference", "observation"} else "long_term_memory"
            if not u["grants"][grant]:
                continue
            if set(c["source_ids"]) != set(c["evidence_quotes"]):
                raise BackendError()
            for source_id, quote in c["evidence_quotes"].items():
                source = sources.get(source_id) if turns else historical.get(source_id)
                if source is None or quote not in source.get("text", source.get("quote", "")):
                    raise BackendError()
            for claim in c["time_assertions"]:
                if claim["source_id"] not in c["source_ids"] or claim["raw_text"] not in c["evidence_quotes"][claim["source_id"]]:
                    raise BackendError()
            target = c["target_entry_id"]
            if target:
                if target not in old or old[target]["version"] != c["expected_version"] or old[target]["kind"] != c["kind"]:
                    raise BackendError("SOURCE_CHANGED")
                if old[target]["basis"] != "inferred":
                    raise BackendError("MODEL_CANNOT_OVERWRITE_SELF_REPORT")
            eid = target or token("entry")
            local_ids[c["local_id"]] = eid
            prepared.append((eid, c))
        created, updated = [], []
        for eid, c in prepared:
            refs = []
            evidence_map = {}
            dependencies = []
            for source_id, quote in c["evidence_quotes"].items():
                if turns:
                    t = sources[source_id]
                    evidence_id = token("evidence")
                    evidence = {"evidence_id": evidence_id, "user_id": uid, "session_id": job["snapshot"]["session_id"],
                                "turn_id": t["turn_id"], "quote": quote, "text_locale": t["text_locale"], "occurred_at": t["occurred_at"], "version": 1}
                    self._put("evidence", evidence_id, uid, evidence)
                else:
                    evidence = historical[source_id]
                    dependencies += [e["entry_id"] for e in existing if any(ref["evidence_id"] == source_id for ref in e["source_refs"]) and e["entry_id"] != eid]
                refs.append({k: evidence[k] for k in ("evidence_id", "session_id", "turn_id")})
                evidence_map[source_id] = evidence["evidence_id"]
            for claim in c["time_assertions"]:
                claim["source_id"] = evidence_map[claim["source_id"]]
                anchor = claim.get("anchor_ref")
                if anchor in local_ids:
                    claim["anchor_ref"] = local_ids[anchor]
                elif anchor and anchor not in old:
                    claim["anchor_ref"] = None
            prior = old.get(eid)
            ttl = self.settings.observation_ttl_seconds if c["kind"] == "observation" else self.settings.inferred_ttl_seconds if c["basis"] == "inferred" else self.settings.recent_ttl_seconds if c["kind"] == "recent" else None
            entry = {"entry_id": eid, "user_id": uid, "kind": c["kind"], "content": c["content"], "content_locale": c["content_locale"],
                     "basis": c["basis"], "record_status": "pending" if c["basis"] == "inferred" else "active",
                     "conditions": c["conditions"], "source_refs": refs, "time_assertions": c["time_assertions"],
                     "allowed_uses": prior["allowed_uses"] if prior else ["conversation"], "version": prior["version"] + 1 if prior else 1,
                     "expires_at": self.clock() + ttl if ttl else None, "created_at": prior["created_at"] if prior else self.clock(),
                     "updated_at": self.clock(), "dependency_entry_ids": sorted(set(dependencies))}
            self._put("entry", eid, uid, entry)
            (updated if prior else created).append(eid)
        return created, updated

    async def wait_idle(self, timeout=10):
        """Demo/test helper only; never call it before speaking to a participant."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if not any(j["state"] in {"running", "queued"} for j in self._all("job")):
                    return
            self.wake.set()
            await asyncio.sleep(0.02)
        raise TimeoutError("background jobs did not finish")


def status_code(envelope):
    if envelope["status"] != "error":
        return 202 if envelope["status"] == "accepted" else 200
    code = envelope["error"]["code"]
    if code == "UNAUTHENTICATED": return 401
    if code == "PERMISSION_DENIED": return 403
    if code == "NOT_FOUND": return 404
    if code in {"VERSION_CONFLICT", "TURN_CONFLICT", "SESSION_CLOSED", "IDEMPOTENCY_CONFLICT", "SESSION_CAPACITY_REACHED"}: return 409
    if code in {"STORAGE_UNAVAILABLE", "SERVICE_BUSY"}: return 503
    return 422
