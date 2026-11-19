"""Documentation-only response models for the routes Stables and the Hub read (#1015, agent-ops#637 stage 3).

A route lists its model with `responses=documented(Model)`, never `response_model=`: FastAPI then puts the model in
the OpenAPI document and leaves the payload alone (no field is dropped, renamed or validated on the way out). Every
model allows extra fields, so a reply may carry more than the document names. A `NotRequired` field is one the
Server sends only sometimes (a module, a state, a role); `X | None` is a field it always sends that may be null.

The test suite checks each real response of a documented route against its model (tests/response_model_capture.py),
so a model that contradicts what the Server sends fails the test that made the request.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from pydantic import ConfigDict


def documented(model: Any, status: int = 200, description: str = "Successful Response") -> dict[int | str, dict]:
    """The `responses=` entry for a documentation-only model of a route's success reply."""
    return {status: {"model": model, "description": description}}


class Open(TypedDict):
    """Base of every documented reply: the Server may add fields the model does not name."""
    __pydantic_config__ = ConfigDict(extra="allow")  # pydantic reads it from the TypedDict


# Health and identity -------------------------------------------------------------------------------------------------
class ProtocolRange(Open):
    min: int
    max: int


class HealthCapabilities(Open):
    profile: str
    required: dict[str, bool]
    modules: dict[str, bool]
    hosted_backends: list[str]
    config_registry: bool


class HealthConfig(Open):
    revision: int
    confirmed: bool
    supervised_restart: bool
    recovery: dict[str, Any] | str | None


class HealthResponse(Open):
    """Liveness, the release and build, the protocols and capabilities this Server offers, and its config state."""
    ok: bool
    profile: str
    release: str
    build_id: str
    protocols: dict[str, ProtocolRange]
    minimum_clients: dict[str, Any]
    capabilities: HealthCapabilities
    update_hint: dict[str, Any]
    build: dict[str, str]
    config: HealthConfig


class MeCapabilities(Open):
    admin: bool
    local_sessions: bool
    hosted_backends: bool
    accounts: bool


class MeUsage(Open):
    """A household member's disk and queue usage; empty for the owner and guests."""
    disk_used_bytes: NotRequired[int]
    disk_quota_bytes: NotRequired[int]
    disk_note: NotRequired[str]
    max_running: NotRequired[int]
    max_queued: NotRequired[int]
    running: NotRequired[int]
    queued: NotRequired[int]
    account_hint: NotRequired[str]


class MeNotify(Open):
    enabled: bool
    topic: str


class MeResponse(Open):
    """The caller's identity, role and capabilities (GET /me)."""
    login: str | None
    name: str | None
    public_url: str
    role: str
    user_id: str | None
    guest_until: str | None
    capabilities: MeCapabilities
    usage: MeUsage
    notify: MeNotify


class AppMeResponse(Open):
    """The principal a token speaks for (GET /api/v1/me): the owner, an App or device, or a household member."""
    role: str | None
    user_id: str
    login: str | None
    name: str | None
    public_url: str
    capabilities: MeCapabilities
    usage: NotRequired[MeUsage]


class GoogleOffer(Open):
    available: bool
    explanation: str


class AuthSessionResponse(Open):
    """Google sign-in availability and this browser's sign-in state; `csrf` only with a session."""
    google: GoogleOffer
    admitted: bool
    signed_in: bool
    role: str | None
    csrf: str | None


class QueueEntry(Open):
    session_id: str
    position: int


class ProjectView(Open):
    name: str
    description: str
    repo: bool
    homelab: bool
    target: str
    managed: bool


# Apps and keys -------------------------------------------------------------------------------------------------------
class AppStoreUsage(Open):
    requests: float
    prompt_tokens: float
    completion_tokens: float
    cost_usd: float


class AppStore(Open):
    sessions: NotRequired[dict[str, int] | None]
    usage: NotRequired[AppStoreUsage | None]
    errors: NotRequired[int | None]
    last_error: NotRequired[str | None]
    last_error_at: NotRequired[float | None]


class KeyResponse(Open):
    """An App, device, member or owner key, never its token."""
    id: str
    name: str
    kind: str
    role: str
    scopes: str
    origins: list[str]
    catalog_app_id: str
    created_at: float
    last_used_at: float | None
    revoked_at: float | None
    erase_after: float | None
    erased_at: float | None
    retention_days: float | None
    store: NotRequired[AppStore]


class ErasureResponse(Open):
    id: str
    name: str
    kind: str
    revoked_at: float
    erase_after: float


class ConfiguredLimits(Open):
    max_running: int | None
    max_queued: int | None


class EffectiveLimits(Open):
    max_running: int
    max_queued: int


class AppLimitsResponse(Open):
    app_id: str
    configured: ConfiguredLimits
    effective: EffectiveLimits
    running: int
    queued: int
    parked: int


# Pairing requests and the Hub claim ----------------------------------------------------------------------------------
class Disclosure(Open):
    scope: str
    tier: Literal["standard", "elevated"]
    text: str


class PairingRequestView(Open):
    """A pairing request as the owner sees it: never a code verifier, token or hash."""
    id: str
    kind: Literal["app", "hub"]
    name: str
    scopes: list[str]
    catalog_app_id: str
    origin: str
    browser: bool
    armed: bool
    state: Literal["pending", "armed", "claimed", "approved", "redeemed", "denied", "expired"]
    needs: Literal["", "approve", "claim", "confirm", "redeem"]
    created_at: float
    expires_at: float
    approved_at: float | None
    finished_at: float | None
    key_id: str
    elevated: list[str]
    disclosures: list[Disclosure]


class HubRecord(Open):
    key_id: str
    name: str
    kind: str
    claimed_at: float


class HubClaimResponse(Open):
    claimed: bool
    hub: HubRecord | None
    requests: list[PairingRequestView]


# Settings ------------------------------------------------------------------------------------------------------------
class SettingSpec(Open):
    key: str
    label: str
    help: str
    category: str
    type: str
    scope: str
    apply: str
    sensitivity: str
    readable: bool
    writable: bool
    available: bool
    file_only: bool
    minimum: float | None
    maximum: float | None
    enum: list[str] | None
    modules: list[str]
    default: Any
    guidance: NotRequired[str]


class Setting(SettingSpec):
    effective: Any
    configured: Any
    inherited: Any
    pending: Any
    source: str


class ConfigSchemaResponse(Open):
    schema_version: int
    supervised_restart: bool
    settings: list[SettingSpec]


class ConfigResponse(Open):
    """Every admin setting with the active and pending revisions and the restart state."""
    revision: int
    pending_revision: int | None
    confirmed: bool
    supervised_restart: bool
    restart_required: bool
    recovery: dict[str, Any] | str | None
    warning: str | None
    settings: list[Setting]


class SettingError(Open):
    code: str
    message: str


class SettingChange(TypedDict("SettingChangeBase", {"from": Any}), Open):  # `from` is a keyword in Python
    key: str
    to: Any
    apply: str
    action: str


class ConfigPlanResponse(Open):
    revision: int
    target_revision: int
    restart_required: bool
    changes: list[SettingChange]
    errors: dict[str, SettingError]


class ConfigDryRunResponse(ConfigPlanResponse):
    """What a change would do, without saving it (`dry_run`)."""
    dry_run: bool


class ConfigAppliedResponse(ConfigResponse):
    """The saved change's plan with the admin view after it."""
    target_revision: int
    changes: list[SettingChange]
    errors: dict[str, SettingError]


ConfigPatchReply = ConfigAppliedResponse | ConfigDryRunResponse
ConfigRollbackReply = ConfigAppliedResponse | ConfigPlanResponse


class RestartResponse(Open):
    accepted: bool
    target_revision: int
    status: str


class SmartApprovalsResponse(Open):
    """Smart-approval status and the most recent reviews."""
    mode: str
    enabled: bool
    configured: bool
    secret_ref: str
    recent: list[dict[str, Any]]


class PopularModel(Open):
    id: str
    label: str


class BackendView(Open):
    """A backend with its default model and, for hosted ones, sign-in state and usage."""
    name: str
    model: str
    effort: str
    available: NotRequired[bool]
    popular_models: NotRequired[list[PopularModel]]
    billing: NotRequired[str]
    today: NotRequired[dict[str, float]]
    week: NotRequired[dict[str, float]]


class Preflight(Open):
    ok: bool | None
    problems: list[str]
    checked_at: float | None


class GitHubPreflight(Open):
    ok: bool
    error: str
    message: str


class GitHubMemberStatus(Open):
    """A member's coarse GitHub state: no URL, username, scope or repository."""
    user_id: str
    display_name: str
    status: str
    last_used_at: float | None


class GitHubMemberAuthResponse(Open):
    """Member GitHub sign-in: whether it is set up and switched on, its last check, and each member's state."""
    configured: bool
    enabled: bool
    preflight: GitHubPreflight | None
    scopes_note: str
    members: list[GitHubMemberStatus]


class GoogleSigninResponse(Open):
    enabled: bool
    ready: bool
    preflight: Preflight | None


# Household accounts and audit ----------------------------------------------------------------------------------------
class GoogleLink(Open):
    """Coarse Google sign-in metadata for one member; never a Google subject or token."""
    linked: bool
    email: str
    linked_at: float | None
    last_sign_in_at: float | None
    active_web_sessions: int
    invitation_expires_at: float | None


class GoogleRevokedResponse(GoogleLink):
    revoked: int


class GoogleInvitationResponse(Open):
    code: str
    expires_at: float


class AccountResponse(Open):
    user_id: str
    account_hint: str
    login: str
    display_name: str
    enabled: bool
    disk_quota_bytes: int
    disk_used_bytes: int
    max_running: int
    max_queued: int
    running: int
    queued: int
    last_activity_at: float | None
    created_at: float
    updated_at: float
    google: GoogleLink


class AccountAuditRow(Open):
    """A household audit row; `ts` is in epoch seconds."""
    id: int
    ts: float
    actor_id: str
    target_id: str
    action: str
    outcome: str
    detail: str


class AuditRow(AccountAuditRow):
    actor_kind: str
    key_id: str
    source: str
    target_kind: str
    metadata: dict[str, Any]


class AuditPage(Open):
    items: list[AuditRow]
    next_before_id: int | None


class ProviderCredentialResponse(Open):
    """A provider grant; `available` says whether a key-based grant's secret file exists."""
    id: str
    app_id: str
    backend: str
    secret_ref: str
    policy: str
    models: list[str]
    created_at: float
    revoked_at: float | None
    app_name: NotRequired[str]
    available: NotRequired[bool]


# Maintenance ---------------------------------------------------------------------------------------------------------
class ContainerUsage(Open):
    name: NotRequired[str]
    state: NotRequired[str]
    size: NotRequired[str]


class RunnerUpdateResult(Open):
    ok: bool
    version: NotRequired[str]
    build_id: NotRequired[str]
    at: NotRequired[float]
    message: NotRequired[str]
    plist_changed: NotRequired[bool]


class RunnerResponse(Open):
    """A configured runner and whether it is polling."""
    name: str
    online: bool
    last_seen_seconds: float | None
    pending_requests: int
    info: NotRequired[dict[str, Any]]
    update_supported: NotRequired[bool]
    manual_update: NotRequired[str]
    last_update: NotRequired[RunnerUpdateResult | None]
    compatibility: NotRequired[dict[str, Any]]


class BackupState(Open):
    """`dir` is the backup module's: without it the core answers only `enabled`."""
    enabled: bool
    dir: NotRequired[str]


class ImageArchiveState(Open):
    enabled: bool


class MaintenanceResponse(Open):
    workspaces_mb: float
    checkpoints_mb: float
    free_gb: float
    total_gb: float
    quota_mb: float
    containers: list[ContainerUsage]
    runners: list[RunnerResponse]
    backup: BackupState
    image_archive: ImageArchiveState
    last_cleanup: NotRequired[dict[str, Any] | None]


class CleanupResponse(Open):
    at: float
    containers_removed: NotRequired[int]
    workspaces_removed: NotRequired[int]
    orphans_removed: NotRequired[int]
    kept: NotRequired[int]
    apps_erased: NotRequired[int]
    sessions_expired: NotRequired[int]
    removed: NotRequired[int]
    expired: NotRequired[int]
    errors: NotRequired[list[str]]
    warnings: NotRequired[list[str]]
