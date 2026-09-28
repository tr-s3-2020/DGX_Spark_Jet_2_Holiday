from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .schemas import IgnoredOutput, TriageInput, TriageOutput
from .skill import ImplicitHealthTriageSkill


def create_app(skill: ImplicitHealthTriageSkill | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.skill = skill or ImplicitHealthTriageSkill()
        try:
            yield
        finally:
            if skill is None:
                await app.state.skill.aclose()

    app = FastAPI(title="implicit-health-triage Task 2", version="1.0.0", lifespan=lifespan)

    @app.post("/v1/implicit-health-triage", response_model=TriageOutput | IgnoredOutput)
    async def triage(body: TriageInput):
        try:
            result = await app.state.skill.handle(body)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Safety gate unavailable") from exc
        return result if result is not None else IgnoredOutput()

    return app


app = create_app()
