"""Loopback HTTP adapter. Tokens and user scopes are supplied by the host."""
from __future__ import annotations

import hmac
import copy
import uuid
from contextlib import asynccontextmanager
from typing import Annotated
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import create_model
from .config import Principal
from .models import Model, REQUESTS, Envelope
from .service import MemoryService, status_code


ROUTES = {
    "set_policy": ("PUT", "/users/{user_id}/policy", "user_id"),
    "open_session": ("POST", "/sessions", None),
    "observe_turn": ("POST", "/sessions/{session_id}/turns", "session_id"),
    "prepare_turn": ("POST", "/sessions/{session_id}/prepare-turn", "session_id"),
    "build_context": ("POST", "/sessions/{session_id}/context", "session_id"),
    "close_session": ("POST", "/sessions/{session_id}/close", "session_id"),
    "get_job": ("GET", "/jobs/{job_id}", "job_id"),
    "list_jobs": ("GET", "/users/{user_id}/jobs", "user_id"),
    "list_entries": ("GET", "/users/{user_id}/entries", "user_id"),
    "revise_entry": ("PATCH", "/entries/{entry_id}", "entry_id"),
    "forget_entries": ("POST", "/users/{user_id}/forget", "user_id"),
    "get_chronicle": ("GET", "/users/{user_id}/chronicle", "user_id"),
    "review_chronicle_event": ("PATCH", "/chronicle/events/{event_id}", "event_id"),
}


def create_app(service: MemoryService, credentials: dict[str, Principal]) -> FastAPI:
    if not credentials or any(len(key) < 24 for key in credentials):
        raise ValueError("configure at least one host token of 24+ characters")

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        yield
        await service.close()

    app = FastAPI(title="Life Memoir Memory API", version="1.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        principal = next((p for key, p in credentials.items() if hmac.compare_digest(supplied, key)), None)
        if principal is None:
            return JSONResponse(service._failure(request.headers.get("x-request-id", "unknown"), "UNAUTHENTICATED", None), status_code=401)
        request.state.principal = principal
        # No browser cookie authentication, no permissive CORS, no raw-body logging.
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors contain the rejected input; do not echo personal text.
        return JSONResponse(service._failure(request.headers.get("x-request-id", "unknown"), "VALIDATION_ERROR", None), status_code=422)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": "0.1.0", "backend": service.settings.backend}

    def register(op, method, path, path_field):
        request_model = REQUESTS[op]
        excluded = {path_field} | ({"request_id"} if method == "GET" else set())
        fields = {}
        for name, field in request_model.model_fields.items():
            if name in excluded:
                continue
            transport_field = copy.deepcopy(field)
            if method == "GET":
                # URL parameters are strings; normalize their scalar types once,
                # then run the strict shared request model in service.execute.
                transport_field.metadata = [m for m in transport_field.metadata if type(m).__name__ != "Strict"]
            fields[name] = (field.annotation, transport_field)
        transport_model = create_model(request_model.__name__ + "HTTP", __base__=Model, **fields)

        async def invoke(request, payload):
            values = payload.model_dump(mode="json")
            values.update(request.path_params)
            if method == "GET":
                values["request_id"] = request.headers.get("x-request-id", "read-" + uuid.uuid4().hex)
            expected_key = request.headers.get("idempotency-key")
            if expected_key and expected_key != values.get("request_id"):
                return JSONResponse(service._failure(values.get("request_id", "unknown"), "IDEMPOTENCY_CONFLICT", None), status_code=409)
            result = await service.execute(op, values, request.state.principal)
            return JSONResponse(result, status_code=status_code(result))

        async def endpoint(request: Request, payload):
            return await invoke(request, payload)

        endpoint.__name__ = op
        endpoint.__annotations__["payload"] = Annotated[transport_model, Query()] if method == "GET" else transport_model
        parameters = [{"name": path_field, "in": "path", "required": True, "schema": {"type": "string", "maxLength": 128}}] if path_field else []
        app.add_api_route("/v1/memory" + path, endpoint, methods=[method], response_model=Envelope,
                         operation_id=op, openapi_extra={"parameters": parameters, "security": [{"HostToken": []}]})

    for op, (method, path, field) in ROUTES.items():
        register(op, method, path, field)
    original = app.openapi

    def openapi():
        schema = original()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["HostToken"] = {"type": "http", "scheme": "bearer"}
        return schema

    app.openapi = openapi
    return app
