"""CLI client for a persistent service: no per-command volatile session loss."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
import httpx
from .config import Settings, Principal
from .models import REQUESTS, AnalysisResult
from .http import ROUTES, create_app
from .service import MemoryService


def main():
    parser = argparse.ArgumentParser(description="Life memoir memory service / JSON client")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", required=True)
    serve.add_argument("--auth", required=True, help="Host identity mapping; tokens are environment variables")
    schema = sub.add_parser("schemas")
    schema.add_argument("--out", required=True)
    for operation in REQUESTS:
        cmd = sub.add_parser(operation)
        cmd.add_argument("--input", required=True, help="JSON file or - for stdin")
        cmd.add_argument("--url", default="http://127.0.0.1:8765")
        cmd.add_argument("--token-env", default="MEMORY_API_TOKEN")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            import uvicorn
            settings = Settings.load(args.config)
            mappings = json.loads(Path(args.auth).read_text())
            credentials = {}
            for m in mappings:
                secret = os.environ.get(m["token_env"], "")
                if len(secret) < 24:
                    raise ValueError("host token is missing or too short")
                credentials[secret] = Principal(m["caller_id"], frozenset(m["user_ids"]), frozenset(m["roles"]), frozenset(m.get("decision_refs", [])))
            app = create_app(MemoryService(settings), credentials)
            uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)
            return
        if args.command == "schemas":
            output = Path(args.out)
            output.mkdir(parents=True, exist_ok=True)
            service = MemoryService(Settings(storage_path=":memory:"))
            app = create_app(service, {"schema-only-not-a-live-secret": Principal("schema", frozenset())})
            (output / "openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
            for name, cls in {**REQUESTS, "analysis_result": AnalysisResult}.items():
                (output / (name + ".schema.json")).write_text(json.dumps(cls.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
            service.db.close()
            print("Generated OpenAPI and shared request schemas.")
            return
        body = json.loads(sys.stdin.read() if args.input == "-" else Path(args.input).read_text())
        body = REQUESTS[args.command].model_validate(body).model_dump(mode="json")
        secret = os.environ.get(args.token_env)
        if not secret:
            raise ValueError("host token environment variable is not set")
        method, path, path_field = ROUTES[args.command]
        if path_field:
            from urllib.parse import quote
            path = path.replace("{" + path_field + "}", quote(body.pop(path_field), safe=""))
        headers = {"Authorization": "Bearer " + secret}
        if method == "GET":
            headers["X-Request-ID"] = body.pop("request_id")
            body = {k: v for k, v in body.items() if v is not None}
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.request(method, args.url.rstrip("/") + "/v1/memory" + path, headers=headers,
                                      **({"params": body} if method == "GET" else {"json": body}))
        data = response.json()
        print(json.dumps(data, ensure_ascii=False))
        sys.exit(3 if data.get("status") == "degraded" else 1 if data.get("status") == "error" else 0)
    except Exception as exc:
        # Avoid emitting request bodies and credentials inside exception strings.
        print(json.dumps({"error": "CLI_CONFIGURATION_OR_TRANSPORT_ERROR", "type": type(exc).__name__}), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
