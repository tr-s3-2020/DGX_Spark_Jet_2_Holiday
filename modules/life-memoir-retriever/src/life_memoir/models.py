"""One set of strict request models shared by Python, CLI and HTTP."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

ID = Annotated[str, Field(min_length=1, max_length=128)]
Text = Annotated[str, Field(min_length=1, max_length=16000)]
Version = Annotated[int, Field(strict=True, ge=0)]
PositiveVersion = Annotated[int, Field(strict=True, ge=1)]
Locale = Annotated[str, Field(pattern=r"^(und|mul|[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*)$", max_length=64)]
Purpose = Literal["conversation", "family_digest"]
Kind = Literal["story", "detail", "recent", "observation", "preference"]
JobKind = Literal["session_extract", "story_reconcile", "preference_reconcile", "context_prepare", "chronicle_build", "language_variant"]
JobState = Literal["queued", "running", "completed", "skipped", "failed", "cancelled"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Request(Model):
    request_id: ID


class Grants(Model):
    long_term_memory: StrictBool
    profile_learning: StrictBool
    family_digest: StrictBool
    remote_analysis: StrictBool


class SetPolicy(Request):
    user_id: ID
    expected_version: Version
    grants: Grants
    consent_ref: ID


class Conditions(Model):
    noise: Literal["quiet", "noisy", "unknown"] = "unknown"
    communication_mode: Literal["voice", "text"] = "text"


class OpenSession(Request):
    user_id: ID
    session_id: ID
    locale: Locale
    reply_locale: Locale | None = None
    conditions: Conditions = Field(default_factory=Conditions)


class Controls(Model):
    stop_session: StrictBool = False
    skip_topic: Annotated[str, Field(max_length=500)] | None = None
    do_not_persist: StrictBool = False


class ResponseStyle(Model):
    question_form: Literal["open", "concrete", "none", "unknown"] = "unknown"
    tts_rate: Annotated[float, Field(gt=0, le=4)] | None = None


class Turn(Model):
    turn_id: ID
    speaker: Literal["user", "assistant"]
    text: Text
    is_final: StrictBool
    occurred_at: str
    text_locale: Locale = "und"
    conditions: Conditions | None = None
    response_style: ResponseStyle | None = None
    controls: Controls | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("occurred_at needs a timezone")
        return value

    @model_validator(mode="after")
    def role_fields(self):
        if self.speaker == "assistant" and self.controls is not None:
            raise ValueError("only user turns carry controls")
        if self.speaker == "user" and self.response_style is not None:
            raise ValueError("response_style describes an actual assistant reply")
        return self


class ObserveTurn(Request, Turn):
    session_id: ID


class ContextOptions(Model):
    query: Annotated[str, Field(max_length=2000)] = ""
    limit: Annotated[int, Field(strict=True, ge=1, le=8)] = 8
    max_chars: Annotated[int, Field(strict=True, ge=200, le=3000)] = 3000
    context_locale: Annotated[str, Field(max_length=64)] = "original"


class BuildContext(Request, ContextOptions):
    session_id: ID
    purpose: Purpose
    deadline_ms: Annotated[int, Field(strict=True, ge=10, le=100)] = 100


class PrepareTurn(Request):
    session_id: ID
    turn: Turn
    context: ContextOptions = Field(default_factory=ContextOptions)
    deadline_ms: Annotated[int, Field(strict=True, ge=10, le=100)] = 100

    @model_validator(mode="after")
    def user_only(self):
        if self.turn.speaker != "user":
            raise ValueError("prepare_turn accepts only a user turn")
        return self


class CloseSession(Request):
    session_id: ID
    expected_session_version: PositiveVersion
    reason: Literal["completed", "user_stopped", "disconnected"]


class GetJob(Request):
    job_id: ID


class Listing(Request):
    user_id: ID
    cursor: str | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20


class ListJobs(Listing):
    kind: JobKind | None = None
    state: JobState | None = None


class ListEntries(Listing):
    kind: Kind | None = None
    record_status: Literal["active", "pending", "superseded"] | None = None


class ReviseEntry(Request):
    entry_id: ID
    expected_version: PositiveVersion
    action: Literal["correct_content", "confirm_preference", "set_allowed_uses"]
    decision_ref: ID
    content: Text | None = None
    allowed_uses: list[Purpose] | None = Field(default=None, max_length=2)

    @model_validator(mode="after")
    def arguments(self):
        if (self.action == "correct_content") != (self.content is not None):
            raise ValueError("content belongs only to correct_content")
        if (self.action == "set_allowed_uses") != (self.allowed_uses is not None):
            raise ValueError("allowed_uses belongs only to set_allowed_uses")
        return self


class ForgetEntries(Request):
    user_id: ID
    scope: Literal["entries", "session"]
    decision_ref: ID
    entry_ids: list[ID] | None = Field(default=None, min_length=1, max_length=50)
    session_id: ID | None = None

    @model_validator(mode="after")
    def scope_fields(self):
        if self.scope == "entries" and (self.entry_ids is None or self.session_id is not None):
            raise ValueError("entries scope needs only entry_ids")
        if self.scope == "session" and (self.session_id is None or self.entry_ids is not None):
            raise ValueError("session scope needs only session_id")
        return self


class GetChronicle(Request):
    user_id: ID
    purpose: Purpose
    format: Literal["structured", "markdown"] = "structured"


class ReviewChronicleEvent(Request):
    event_id: ID
    expected_event_version: PositiveVersion
    action: Literal["confirm_time", "correct_time", "reject_inference"]
    decision_ref: ID
    resolution_version: PositiveVersion | None = None
    alternative_id: ID | None = None
    time_text: Text | None = None
    text_locale: Locale | None = None

    @model_validator(mode="after")
    def review_fields(self):
        if self.action == "correct_time":
            if not self.time_text or not self.text_locale or self.alternative_id is not None or self.resolution_version is not None:
                raise ValueError("correct_time needs only time_text and text_locale")
        elif not self.alternative_id or self.resolution_version is None or self.time_text is not None or self.text_locale is not None:
            raise ValueError("time decision needs only alternative_id and resolution_version")
        return self


REQUESTS = {
    "set_policy": SetPolicy, "open_session": OpenSession,
    "observe_turn": ObserveTurn, "prepare_turn": PrepareTurn,
    "build_context": BuildContext, "close_session": CloseSession,
    "get_job": GetJob, "list_jobs": ListJobs, "list_entries": ListEntries,
    "revise_entry": ReviseEntry, "forget_entries": ForgetEntries,
    "get_chronicle": GetChronicle, "review_chronicle_event": ReviewChronicleEvent,
}
READS = {"build_context", "get_job", "list_jobs", "list_entries", "get_chronicle"}


class TimeClaim(Model):
    relation: Literal["in_year", "at_age", "years_after", "before_event", "after_event", "not_before_event", "life_stage", "unresolved"]
    raw_text: Annotated[str, Field(min_length=1, max_length=512)]
    source_id: ID
    year_value: Annotated[int, Field(strict=True, ge=1000, le=2200)] | None = None
    age_value: Annotated[int, Field(strict=True, ge=0, le=130)] | None = None
    age_basis: Literal["completed_years", "nominal_years", "unknown"] = "unknown"
    offset_years: Annotated[int, Field(strict=True, ge=0, le=130)] | None = None
    anchor_ref: ID | None = None
    calendar: Literal["gregorian", "lunar", "unknown"] = "unknown"
    approximate: StrictBool = False
    subject: Literal["self", "other", "ambiguous"] = "self"


class Candidate(Model):
    local_id: ID
    kind: Kind
    content: Annotated[str, Field(min_length=1, max_length=1500)]
    content_locale: Locale
    basis: Literal["self_report", "family_report", "inferred"]
    source_ids: list[ID] = Field(min_length=1, max_length=20)
    evidence_quotes: dict[ID, Annotated[str, Field(min_length=1, max_length=512)]]
    conditions: dict[str, str] = Field(default_factory=dict)
    temporal_scope: Literal["session", "recent", "long_term"]
    time_assertions: list[TimeClaim] = Field(default_factory=list, max_length=16)
    target_entry_id: ID | None = None
    expected_version: PositiveVersion | None = None


class AnalysisResult(Model):
    candidates: list[Candidate] = Field(default_factory=list, max_length=80)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class Envelope(Model):
    api_version: Literal["1.0"] = "1.0"
    request_id: str
    status: Literal["ok", "accepted", "ignored", "degraded", "error"]
    data: dict[str, Any] | None
    error: dict[str, Any] | None
    meta: dict[str, Any]
