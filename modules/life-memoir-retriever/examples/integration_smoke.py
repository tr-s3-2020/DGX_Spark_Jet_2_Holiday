"""Synthetic HTTP integration check; no UI, audio or live-model claims."""
import os
import time
import uuid
import httpx

BASE = os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8765").rstrip("/")
SID = "fixture-" + uuid.uuid4().hex[:12]


def main():
    with httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + os.environ["MEMORY_API_TOKEN"]}, timeout=5) as client:
        def send(method, path, **body):
            if method != "GET":
                body.setdefault("request_id", uuid.uuid4().hex)
            response = client.request(method, "/v1/memory" + path, **({"params": body} if method == "GET" else {"json": body}))
            response.raise_for_status()
            result = response.json()
            assert result["status"] != "error", result
            return result

        send("PUT", "/users/fixture-elder/policy", request_id="fixture-policy-v1", expected_version=0,
             grants=dict(long_term_memory=True, profile_learning=True, family_digest=False, remote_analysis=False),
             consent_ref="fixture-consent")
        send("POST", "/sessions", user_id="fixture-elder", session_id=SID, locale="zh-CN")
        prepared = send("POST", f"/sessions/{SID}/prepare-turn", turn=dict(
            turn_id="turn-1", speaker="user", is_final=True, occurred_at="2026-09-26T14:00:00+08:00",
            text_locale="zh-CN", text="我是1950年出生的。我二十周岁时进厂。进厂整三年后，我调去了维修车间。以后问题简短一点。"))
        closed = send("POST", f"/sessions/{SID}/close", expected_session_version=prepared["data"]["observation"]["session_version"], reason="completed")
        job_id = closed["data"]["job_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = send("GET", f"/jobs/{job_id}")["data"]
            assert job["state"] not in {"failed", "cancelled"}, job
            if job["state"] == "completed":
                view = send("GET", "/users/fixture-elder/chronicle", purpose="conversation")
                if view["data"]["state"] == "ready":
                    break
            time.sleep(0.1)
        else:
            raise TimeoutError("fixture jobs did not publish within 15 seconds")
        windows = {(a["earliest_year"], a["latest_year"]) for e in view["data"]["content"]["items"] for a in e["time_resolution"]["alternatives"]}
        assert {(1950, 1950), (1970, 1971), (1973, 1974)} <= windows
        send("POST", "/sessions", user_id="fixture-elder", session_id=SID + "-next", locale="zh-CN")
        ctx = send("POST", f"/sessions/{SID}-next/context", purpose="conversation", query="进厂")
        assert ctx["data"]["memories"] and ctx["data"]["preferences"]
        send("POST", f"/sessions/{SID}-next/close", expected_session_version=1, reason="completed")
        print("PASS: synthetic HTTP flow, background publish, year windows, next-session recall.")


if __name__ == "__main__":
    main()
