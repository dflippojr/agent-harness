"""Response contracts shared by the legacy/admin and app session surfaces."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderFailureResponse(BaseModel):
    code: str
    provider: str
    message: str
    retryable: bool


class TokenTotalsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt_tokens: int = 0
    completion_tokens: int = 0


class RateLimitsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    rateLimitType: str = ""
    utilization: float | None = None


class SessionRunResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    rate_limits: RateLimitsResponse | None = None


class TaintResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    origin: str


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    tool: str
    args: dict
    reason: str = ""
    detail: str = ""
    status: str = "pending"


class WebSessionResponse(BaseModel):
    """Declared Web fields; preserve other additive fields and absent optional fields."""
    model_config = ConfigDict(extra="allow")
    id: str
    project: str
    target: str
    backend: str
    model: str
    title: str
    status: str
    stop_reason: str = ""
    created_at: float
    updated_at: float
    totals: TokenTotalsResponse = Field(default_factory=TokenTotalsResponse)
    run: SessionRunResponse = Field(default_factory=SessionRunResponse)
    answer: str = ""
    failure: ProviderFailureResponse | None = None
    last_event_seq: int = 0
    queue_position: int | None = None
    chat_summary: str = ""
    context_used: int = 0
    context_limit: int = 0
    taint: list[TaintResponse] = Field(default_factory=list)
    pending_approvals: list[ApprovalResponse] = Field(default_factory=list)
    repo_kind: str = ""
    branch: str = ""
    base_branch: str = ""
    review: str = ""
    review_detail: str = ""
    # SQLite returns 0/1, including on raw idempotency replays. Preserve the wire representation.
    workspace_removed: bool | int = 0
    push_target: str = ""


class SessionResponse(WebSessionResponse):
    app_tools: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def preserve_app_defaults(cls, data):
        # The original App contract always serialized these defaults. Only newly declared Web fields stay unset.
        if isinstance(data, dict):
            return {"stop_reason": "", "totals": {}, "run": {}, "answer": "", "app_tools": [], "metadata": {},
                    "failure": None, "last_event_seq": 0, "queue_position": None, **data}
        return data
