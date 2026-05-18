from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class SessionState(str, Enum):
    IDLE = "IDLE"
    LOGGING_IN = "LOGGING_IN"
    MFA_REQUIRED = "MFA_REQUIRED"
    AUTHENTICATING = "AUTHENTICATING"
    FETCHING_DOCS = "FETCHING_DOCS"
    DONE = "DONE"
    ERROR = "ERROR"


class Carrier(str, Enum):
    USAA = "usaa"
    GEICO = "geico"
    PROGRESSIVE = "progressive"
    ALLSTATE = "allstate"
    STATE_FARM = "state_farm"


class LoginRequest(BaseModel):
    carrier: Carrier
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    session_id: str


class MfaRequest(BaseModel):
    code: str = Field(min_length=1)


class Document(BaseModel):
    id: str
    name: str
    content_type: str = "application/pdf"
    size_bytes: int


class StatusEvent(BaseModel):
    event: Literal[
        "state_change", "docs_ready", "error", "heartbeat"
    ] = "state_change"
    state: SessionState
    detail: str | None = None
    docs: list[Document] | None = None
    error: str | None = None
    server_ts_ms: int | None = None
    timings_ms: dict[str, int] | None = None
