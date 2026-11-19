"""The SSE event streams' payloads as one discriminated union, for the OpenAPI document (#1014, agent-ops#637 stage 2).

Every frame of a session's event stream (`/sessions/{ref}/events`, `/chats/{ref}/events`,
`/api/v1/sessions/{ref}/events`) carries one event as its `data:` JSON, and its `event:` line repeats the event's
`type`. The session-list streams (`/events`, `/api/v1/events`) carry the status-level subset (`LIST_TYPES`) with
`seq: null`. `SessionEvent` is the union of every event the Server emits, keyed on `type`; `SessionListEvent` is the
list streams' part of it.

This is documentation only. Nothing here runs when the Server emits an event or streams it: `install` adds the
schemas to the OpenAPI document, and the tests check every event the Server emits against them
(`tests/sse_event_capture.py`, `tests/test_event_schema.py`). A new event type, or a new field of one, belongs here
in the same change.

The `data` object of each type lists every field it can carry. A field that is not always there is optional
(`NotRequired`); one that can be null says so. A few fields hold an object whose keys belong to another layer (a
tool's arguments, the run record, a CLI's rate-limit report): those are open objects.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, TypedDict, Union

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, with_config

STRICT = ConfigDict(extra="forbid")
OPEN = ConfigDict(extra="allow")
REF = "#/components/schemas/{model}"

# The event types the session-list streams forward (harness.api.GLOBAL_TYPES; a test keeps the two equal).
LIST_TYPES = frozenset({"session_created", "status", "approval_requested", "approval_decided", "run_finished", "queue"})

AnyObject = dict[str, Any]


# --- session lifecycle -------------------------------------------------------------------------------------------

@with_config(STRICT)
class SessionSkill(TypedDict):
    """A skill the session runs with, frozen at creation."""
    slug: str
    title: str
    version: int
    content_hash: str


@with_config(STRICT)
class SessionCreatedEventData(TypedDict):
    """A session was created. The App fields are present only for an App's session."""
    project: str
    target: str
    model: str
    backend: str
    title: str
    app: NotRequired[str]
    app_tools: NotRequired[list[str]]
    job_id: NotRequired[str]
    skills: NotRequired[list[SessionSkill]]


@with_config(STRICT)
class ContentEventData(TypedDict):
    """A message's text."""
    content: str


@with_config(STRICT)
class StatusEventData(TypedDict):
    """The session's status changed (`queued`, `running`, `waiting_approval`, `waiting_limit`, `done`, `failed`,
    `cancelled`, ...). A run's end carries its stop reason and, when it has one, the answer."""
    status: str
    stop_reason: NotRequired[str]
    answer: NotRequired[str]


@with_config(STRICT)
class QueueEventData(TypedDict):
    """The session's place in the run queue (1 is next). Ephemeral."""
    position: int


@with_config(STRICT)
class ResumedEventData(TypedDict):
    """The Server restarted and picked the session's run up again in this status."""
    status: str


@with_config(STRICT)
class ForkedEventData(TypedDict):
    """This session was forked from `parent` at `turn`; `backend` and `model` when the fork changed them."""
    parent: str
    turn: int
    summary_note: NotRequired[str]
    backend: NotRequired[str]
    model: NotRequired[str]
    tool_profile_reset: NotRequired[AnyObject]


@with_config(STRICT)
class RunFinishedEventData(TypedDict):
    """A run ended. `run` is the run record (absent on the session-list streams); the job fields are present for a
    job's session, `ungrounded_quotes` when the answer quotes text no source contains."""
    status: str
    stop_reason: str
    answer: str
    run: NotRequired[AnyObject]
    job_id: NotRequired[str]
    job_status: NotRequired[str]
    job_reason: NotRequired[str]
    ungrounded_quotes: NotRequired[list[str]]


@with_config(STRICT)
class ErrorEventData(TypedDict):
    """Something failed. A run's failure also carries `code`, `provider` and `retryable`."""
    message: str
    code: NotRequired[str]
    provider: NotRequired[str]
    retryable: NotRequired[bool]


@with_config(STRICT)
class StuckEventData(TypedDict):
    """The run stopped repeating a failing tool call."""
    reason: str
    tool: str
    count: int


# --- model turns -------------------------------------------------------------------------------------------------

@with_config(STRICT)
class AssistantEventData(TypedDict):
    """One model turn: its text, reasoning and tool calls (OpenAI chat format), with token counts and speeds.
    `totals` (the session's running totals) is present on a local model's turns."""
    content: str
    reasoning: str
    tool_calls: list[AnyObject]
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    prompt_tps: float
    gen_tps: float
    totals: NotRequired[AnyObject]


@with_config(STRICT)
class DeltaEventData(TypedDict):
    """Streamed text of the turn in progress: `kind` is `content` or `reasoning`. Ephemeral."""
    kind: str
    text: str


@with_config(STRICT)
class PromptProgressEventData(TypedDict):
    """How far the model server has read a long prompt, in tokens. Ephemeral."""
    processed: int
    total: int
    cached: int


@with_config(STRICT)
class ModelWakingEventData(TypedDict):
    """The local model is loading; the turn waits for it."""
    model: str
    expected_seconds: int


@with_config(STRICT)
class ModelReadyEventData(TypedDict):
    """The local model answered after waking."""
    model: str
    seconds: int


@with_config(STRICT)
class LlmRetryEventData(TypedDict):
    """A model call failed and is retried."""
    attempt: int
    error: str


@with_config(STRICT)
class TurnMetricsEventData(TypedDict):
    """One turn's context-efficiency counts (#159, #639). A row with only the activity counts closes a run whose
    last increments no model turn committed."""
    turn: int
    dead_end_retries: int
    compaction_correlated_retries: dict[str, int]
    edit_mismatches: int
    elide_only_compactions: int
    read_artifact_calls: int
    read_artifact_repeats: int
    prompt_tokens: NotRequired[int]
    completion_tokens: NotRequired[int]
    cache_tokens: NotRequired[int | None]
    recomputed_tokens: NotRequired[int | None]
    schema_tokens: NotRequired[int]
    schema_count: NotRequired[int]
    estimated: NotRequired[bool]
    composition: NotRequired[dict[str, int]]


@with_config(STRICT)
class QuotesEventData(TypedDict):
    """Quoted passages in the answer that no source the session read contains."""
    quotes: list[str]


# --- tools and approvals -----------------------------------------------------------------------------------------

@with_config(STRICT)
class ToolCallEventData(TypedDict):
    """The model called a tool; `decision` is the policy's (`allow`, `ask`, `deny`) and `reason` why."""
    id: str
    name: str
    args: AnyObject
    decision: str
    reason: str


@with_config(STRICT)
class ToolResultEventData(TypedDict):
    """A tool's result. `output` keeps the middle of a long result out (`output_chars` is the full length);
    `artifact_id` names the stored full result when there is one. `tainted` marks a result from untrusted content;
    `verify` is the project's verify checks' report, after an edit."""
    id: str
    name: str
    ok: bool
    seconds: float
    output_chars: int
    output: str
    artifact_id: NotRequired[str | None]
    tainted: NotRequired[bool]
    verify: NotRequired[AnyObject]


@with_config(STRICT)
class ApprovalRequestedEventData(TypedDict):
    """A tool call waits for the owner's decision. `smart` is the smart reviewer's advice, when it ran."""
    id: str
    tool_call_id: str
    tool: str
    args: AnyObject
    reason: str
    detail: str
    smart: NotRequired[AnyObject]


@with_config(STRICT)
class ApprovalDecidedEventData(TypedDict):
    """An approval was decided: `status` is `approved` or `denied`; `expired` marks one denied for waiting too long."""
    id: str
    status: str
    note: str
    expired: NotRequired[bool]


@with_config(OPEN)
class SmartReviewEventData(TypedDict):
    """The smart reviewer's record of one tool call (`harness.smart_approvals`)."""
    tool: str
    mode: str
    outcome: str
    recommendation: str
    reason: str
    confidence: float
    risk_flags: list[str]
    escalate_reason: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    policy_fingerprint: str


@with_config(OPEN)
class ApprovalAutoApprovedEventData(TypedDict):
    """A tool call was approved without asking: approval `id`, with the smart reviewer's record (the
    `smart_review` fields) when it decided."""
    id: str
    tool: NotRequired[str]
    mode: NotRequired[str]
    outcome: NotRequired[str]
    recommendation: NotRequired[str]
    reason: NotRequired[str]
    confidence: NotRequired[float]
    risk_flags: NotRequired[list[str]]
    escalate_reason: NotRequired[str]
    provider: NotRequired[str]
    model: NotRequired[str]
    prompt_tokens: NotRequired[int]
    completion_tokens: NotRequired[int]
    cost_usd: NotRequired[float]
    latency_ms: NotRequired[int]
    policy_fingerprint: NotRequired[str]


@with_config(STRICT)
class AppToolCallEventData(TypedDict):
    """The model called one of the App's tools; the App answers within `timeout_seconds`."""
    call_id: str
    name: str
    args: AnyObject
    timeout_seconds: float


@with_config(STRICT)
class AppToolResultEventData(TypedDict):
    """The App answered a tool call."""
    call_id: str
    ok: bool
    chars: int


@with_config(STRICT)
class TaintAddedEventData(TypedDict):
    """The session read untrusted content (`kind`, from `origin`); `sources` is how many it holds now."""
    kind: str
    origin: str
    sources: int


@with_config(STRICT)
class TaintClearedEventData(TypedDict):
    """The owner cleared these taint origins."""
    cleared: list[str]


# --- agent state, compaction and checkpoints ---------------------------------------------------------------------

@with_config(STRICT)
class StateEventData(TypedDict):
    """The agent's working state (goal, plan, errors, next step, notes) after `update_state`."""
    state: AnyObject


@with_config(STRICT)
class NotesEventData(TypedDict):
    """The agent's notes."""
    notes: str


@with_config(STRICT)
class CompactionEventData(TypedDict):
    """The context was compacted. `tier` is `mask`, `trim`, `elide`, `summary` or `round_reset`; each tier carries the
    counts it has. A summary pass adds its own token counts and the session's `totals`."""
    tier: str
    tokens_before: NotRequired[int]
    tokens_after: NotRequired[int]
    tokens_saved: NotRequired[int]
    characters_saved: NotRequired[int]
    context_tokens: NotRequired[int]
    summary_averted: NotRequired[bool]
    reason: NotRequired[str]
    summary: NotRequired[str]
    summarized_messages: NotRequired[int]
    prompt_tokens: NotRequired[int]
    completion_tokens: NotRequired[int]
    totals: NotRequired[AnyObject]


@with_config(STRICT)
class CompactionStartedEventData(TypedDict):
    """A summary pass began over `messages` messages."""
    messages: int
    tokens_before: int
    context_tokens: int


@with_config(STRICT)
class CompactingEventData(TypedDict):
    """Progress of a summary pass: `reading` the prompt, then `writing` the summary. Ephemeral."""
    phase: str
    processed: NotRequired[int]
    total: NotRequired[int]
    cached: NotRequired[int]
    tokens: NotRequired[int]
    max_tokens: NotRequired[int]


@with_config(STRICT)
class CheckpointEventData(TypedDict):
    """The workspace was checkpointed after `turn` at commit `head`, or the checkpoint was skipped (`status`,
    `reason`)."""
    turn: NotRequired[int]
    head: NotRequired[str]
    status: NotRequired[str]
    reason: NotRequired[str]


@with_config(STRICT)
class RewoundEventData(TypedDict):
    """The session was rewound to the checkpoint after `turn`."""
    turn: int
    head: str


# --- workspace, branch and review --------------------------------------------------------------------------------

@with_config(STRICT)
class WorkspaceReadyEventData(TypedDict):
    """The session's workspace was cloned and its branch created."""
    repo: str
    branch: str
    base_branch: str
    base_commit: str


@with_config(STRICT)
class BranchSavedEventData(TypedDict):
    """The session branch was saved at the end of a run: `commits` lists its commits ahead of the base, one line
    each."""
    branch: str
    auto_commit: bool
    published: bool
    head: str
    commits: list[str]


@with_config(STRICT)
class ReviewEventData(TypedDict):
    """A review action (`push`, `merge`, `discard`, ...) and the review state it left."""
    action: str
    state: str
    detail: str
    head: str


@with_config(STRICT)
class SandboxSetupEventData(TypedDict):
    """The project's sandbox setup command ran."""
    command: str
    ok: bool
    detail: str


@with_config(STRICT)
class SandboxDiskLimitEventData(TypedDict):
    """The sandbox hit a disk limit; `stopped` when the run was stopped for it."""
    reason: str
    stopped: bool


@with_config(STRICT)
class TargetWaitingEventData(TypedDict):
    """The session's machine is offline; the run waits for it (`during` names the tool call it interrupted)."""
    target: str
    during: NotRequired[str]


@with_config(STRICT)
class TargetOnlineEventData(TypedDict):
    """The session's machine is back after `seconds`."""
    target: str
    seconds: int
    during: NotRequired[str]


# --- resources, backends and limits ------------------------------------------------------------------------------

@with_config(STRICT)
class GpuPauseReason(TypedDict):
    """One thing holding the GPU: `kind` (`game`, `plex`, `vram`, `memory`, `manual`, ...), a stable `key` and a
    readable `detail`."""
    key: str
    kind: str
    detail: str


@with_config(STRICT)
class GpuPausedEventData(TypedDict):
    """The GPU is paused for something else; the run waits. `reason` says why in words."""
    reason: str
    reasons: list[GpuPauseReason]
    resume_after_seconds: float


@with_config(STRICT)
class SecondsEventData(TypedDict):
    """A wait ended after `seconds`."""
    seconds: int


@with_config(STRICT)
class WaitingMemoryEventData(TypedDict):
    """The host is short of memory; the run waits for it to recover."""
    reason: str
    waiting_for: str
    available_bytes: int
    threshold_bytes: int


@with_config(STRICT)
class BackendEventData(TypedDict):
    """A hosted backend's event: an authentication mode it fell back to or needs."""
    backend: str
    auth: str


@with_config(STRICT)
class BillingWarningEventData(TypedDict):
    """A hosted backend's billing warning."""
    backend: str
    message: str


@with_config(STRICT)
class LimitWaitingEventData(TypedDict):
    """A hosted backend's usage limit is reached; the run waits until `resets_at` (epoch seconds)."""
    backend: str
    resets_at: float


@with_config(OPEN)
class RateLimitEventData(TypedDict):
    """A hosted CLI's rate-limit report, as the CLI sent it (open: its fields are the CLI's)."""
    status: NotRequired[str]
    utilization: NotRequired[float]
    resetsAt: NotRequired[float]
    rateLimitType: NotRequired[str]


# --- chat snippets -----------------------------------------------------------------------------------------------

@with_config(STRICT)
class SnippetStartedEventData(TypedDict):
    """A chat code snippet started running."""
    id: str
    language: str
    label: str
    toolchain: str
    source: str
    origin: str


@with_config(STRICT)
class SnippetResultEventData(TypedDict):
    """A chat code snippet's result. A run the Server found unfinished at startup has only `id`, `status` and
    `reasons` when its language is no longer known."""
    id: str
    status: str
    reasons: list[str]
    language: NotRequired[str]
    label: NotRequired[str]
    toolchain: NotRequired[AnyObject]
    compile: NotRequired[AnyObject | None]
    run: NotRequired[AnyObject | None]
    duration_ms: NotRequired[int]
    truncated: NotRequired[bool]
    error: NotRequired[str]
    limits: NotRequired[AnyObject]


# --- the union ---------------------------------------------------------------------------------------------------

# type -> (data schema, ephemeral). An ephemeral event is never stored: `seq` is null and it has no `ts`.
EVENTS: dict[str, tuple[type, bool]] = {
    "session_created": (SessionCreatedEventData, False),
    "user_message": (ContentEventData, False),
    "app_context": (ContentEventData, False),
    "status": (StatusEventData, False),
    "queue": (QueueEventData, True),
    "resumed": (ResumedEventData, False),
    "forked": (ForkedEventData, False),
    "run_finished": (RunFinishedEventData, False),
    "error": (ErrorEventData, False),
    "stuck": (StuckEventData, False),
    "assistant": (AssistantEventData, False),
    "delta": (DeltaEventData, True),
    "prompt_progress": (PromptProgressEventData, True),
    "model_waking": (ModelWakingEventData, False),
    "model_ready": (ModelReadyEventData, False),
    "llm_retry": (LlmRetryEventData, False),
    "turn_metrics": (TurnMetricsEventData, False),
    "quote_check": (QuotesEventData, False),
    "ungrounded_quotes": (QuotesEventData, False),
    "tool_call": (ToolCallEventData, False),
    "tool_result": (ToolResultEventData, False),
    "approval_requested": (ApprovalRequestedEventData, False),
    "approval_decided": (ApprovalDecidedEventData, False),
    "approval_auto_approved": (ApprovalAutoApprovedEventData, False),
    "smart_review": (SmartReviewEventData, False),
    "app_tool_call": (AppToolCallEventData, False),
    "app_tool_result": (AppToolResultEventData, False),
    "taint_added": (TaintAddedEventData, False),
    "taint_cleared": (TaintClearedEventData, False),
    "state": (StateEventData, False),
    "notes": (NotesEventData, False),
    "compaction": (CompactionEventData, False),
    "compaction_started": (CompactionStartedEventData, False),
    "compacting": (CompactingEventData, True),
    "checkpoint": (CheckpointEventData, False),
    "rewound": (RewoundEventData, False),
    "workspace_ready": (WorkspaceReadyEventData, False),
    "branch_saved": (BranchSavedEventData, False),
    "review": (ReviewEventData, False),
    "sandbox_setup": (SandboxSetupEventData, False),
    "sandbox_disk_limit": (SandboxDiskLimitEventData, False),
    "target_waiting": (TargetWaitingEventData, False),
    "target_online": (TargetOnlineEventData, False),
    "gpu_paused": (GpuPausedEventData, False),
    "gpu_resumed": (SecondsEventData, False),
    "waiting_memory": (WaitingMemoryEventData, False),
    "memory_recovered": (SecondsEventData, False),
    "backend_auth_required": (BackendEventData, False),
    "backend_fallback": (BackendEventData, False),
    "billing_warning": (BillingWarningEventData, False),
    "limit_waiting": (LimitWaitingEventData, False),
    "rate_limit": (RateLimitEventData, False),
    "snippet_started": (SnippetStartedEventData, False),
    "snippet_result": (SnippetResultEventData, False),
}


def _name(type_: str) -> str:
    return "".join(part.capitalize() for part in type_.split("_")) + "Event"


def _envelope(type_: str, data: type, ephemeral: bool) -> type:
    """The frame's whole JSON for one event type. A stored event's `seq` orders the session's events (and is the
    frame's `id:`); the session-list streams send it as null."""
    if ephemeral:
        fields = {"seq": None, "session_id": str, "type": Literal[type_], "data": data}  # type: ignore[valid-type]
        doc = f"A `{type_}` event. Ephemeral: never stored or replayed, so `seq` is null and there is no `ts`."
    else:
        fields = {"seq": Union[int, None], "session_id": str, "ts": float,
                  "type": Literal[type_], "data": data}  # type: ignore[valid-type]
        doc = (f"A `{type_}` event. `seq` is its place in the session's stored events (null on the session-list "
               "streams); `ts` is when it happened, in epoch seconds.")
    cls = TypedDict(_name(type_), fields)  # type: ignore[misc]
    cls.__doc__ = doc
    cls.__pydantic_config__ = STRICT  # type: ignore[attr-defined]
    return cls


ENVELOPES: dict[str, type] = {type_: _envelope(type_, data, ephemeral) for type_, (data, ephemeral) in EVENTS.items()}
# Built from the table, so a type checker sees only Any; the adapter and the JSON schema see the whole union.
SessionEvent: Any = Annotated[Union[tuple(ENVELOPES.values())], Field(discriminator="type")]
_ADAPTER = TypeAdapter(SessionEvent)

UNION = "SessionEvent"
LIST_UNION = "SessionListEvent"


def components() -> dict[str, dict]:
    """The schemas `install` adds to the OpenAPI components: every event and data schema, `SessionEvent` and
    `SessionListEvent`."""
    schema = _ADAPTER.json_schema(ref_template=REF)
    out = dict(schema.pop("$defs"))
    out[UNION] = {"description": "One event of a session's SSE stream (the frame's `data:`), keyed on `type`; the "
                                 "frame's `event:` line repeats the type.", **schema}
    listed = sorted(LIST_TYPES)
    out[LIST_UNION] = {
        "description": "One event of the session-list SSE streams: the status-level types, with `seq` null and no "
                       "`run` in `run_finished`.",
        "discriminator": {"propertyName": "type",
                          "mapping": {type_: REF.format(model=_name(type_)) for type_ in listed}},
        "oneOf": [{"$ref": REF.format(model=_name(type_))} for type_ in listed],
    }
    return out


# The streams' paths, before the admin aliases copy them (harness.admin), and the union each one carries.
STREAMS = {"/sessions/{ref}/events": UNION, "/chats/{ref}/events": UNION, "/api/v1/sessions/{ref}/events": UNION,
           "/events": LIST_UNION, "/api/v1/events": LIST_UNION}


def document(schema: dict) -> dict:
    """Add the event schemas to an OpenAPI document and point each event stream's 200 response at its union."""
    existing = schema.setdefault("components", {}).setdefault("schemas", {})
    for name, item in components().items():
        if name in existing and existing[name] != item:
            raise RuntimeError(f"the OpenAPI component {name} is taken: rename the event schema")
        existing[name] = item
    for path, union in STREAMS.items():
        operation = schema.get("paths", {}).get(path, {}).get("get")
        if operation is not None:
            operation["responses"]["200"]["content"] = {
                "text/event-stream": {"schema": {"$ref": REF.format(model=union)}}}
    return schema


def install(app) -> None:
    """Document the event streams in `app`'s OpenAPI document. Call before anything copies its paths (the admin
    aliases do)."""
    original = app.openapi

    def openapi_with_events():
        return document(original())

    app.openapi = openapi_with_events


def mismatch(event: dict) -> str | None:
    """Why an event's wire form (the JSON of its SSE frame) does not match `SessionEvent`, or None when it does.
    For tests: the Server never calls it."""
    if isinstance(event, dict) and event.get("type") not in EVENTS:
        return f"the event type {event.get('type')!r} is not in {UNION}: add it to harness/event_schema.py"
    try:
        _ADAPTER.validate_python(event, strict=True)
    except ValidationError as e:
        return "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                         f" (got {error['input']!r:.200})" for error in e.errors())
    return None
