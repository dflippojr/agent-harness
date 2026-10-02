"""Frozen pre-versioning schema history (issue #256).

These are the columns `Database` added one by one before `PRAGMA user_version` was used. The list is
frozen: never append to it. A new schema change is a numbered migration module (see docs/migrations.md).
A database at `user_version` 0 is brought to `BASELINE_VERSION` by running `SCHEMA` and this list.
"""

# Column definitions reused across the migration table below.
TEXT_EMPTY = "TEXT NOT NULL DEFAULT ''"
TEXT_LOCAL = "TEXT NOT NULL DEFAULT 'local'"
INT_ZERO = "INTEGER NOT NULL DEFAULT 0"
TEXT_EMPTY_LIST = "TEXT NOT NULL DEFAULT '[]'"
TEXT_EMPTY_OBJECT = "TEXT NOT NULL DEFAULT '{}'"

# Columns added after a table first shipped: (table, column, definition).
LEGACY_COLUMNS = [
    # Conversation kind: 'agent' (default, every pre-existing row) or 'chat' (Chat home, no project workspace).
    ("sessions", "kind", "TEXT NOT NULL DEFAULT 'agent'"),
    # Secret for deciding one approval from a notification button, without a session cookie or JSON body.
    ("approvals", "token", TEXT_EMPTY),
    # Phase 3: git-backed projects. `review` is '' | merged | pushed | discarded.
    ("sessions", "branch", TEXT_EMPTY),
    ("sessions", "base_branch", TEXT_EMPTY),
    ("sessions", "base_commit", TEXT_EMPTY),
    ("sessions", "review", TEXT_EMPTY),
    ("sessions", "review_detail", TEXT_EMPTY),
    # Set when cleanup deleted the workspace (or the user discarded it).
    ("sessions", "workspace_removed", INT_ZERO),
    # Phase 6e: sessions created through the app API, their registered tools and metadata; key scopes.
    ("sessions", "app_id", TEXT_EMPTY),
    ("sessions", "app_tools", TEXT_EMPTY_LIST),
    ("sessions", "app_metadata", TEXT_EMPTY_OBJECT),
    # Issue #57: human-owned Agent Harness Web data. v1 has one stable owner; guests own nothing.
    ("sessions", "owner_id", "TEXT NOT NULL DEFAULT 'owner'"),
    ("api_keys", "scopes", "TEXT NOT NULL DEFAULT 'inference'"),
    ("api_keys", "kind", "TEXT NOT NULL DEFAULT 'device'"),
    ("api_keys", "origins", TEXT_EMPTY_LIST),
    # Phase 7d: sessions started by a scheduled job, and the STATUS the job's answer ended with (ok | attention).
    ("sessions", "job_id", TEXT_EMPTY),
    ("sessions", "job_status", TEXT_EMPTY),
    # Phase 8a: local inference or a hosted CLI session backend.
    ("sessions", "backend", TEXT_LOCAL),
    # Issue #166: sessions started together from one prompt to compare backends/models share a group id.
    ("sessions", "compare_group", TEXT_EMPTY),
    ("jobs", "backend", TEXT_LOCAL),
    ("templates", "backend", TEXT_LOCAL),
    # UI refresh: explicit image resolution while preserving model-native defaults for old callers.
    ("images", "resolution", "TEXT NOT NULL DEFAULT 'auto'"),
    ("images", "base_model", TEXT_EMPTY),
    ("images", "lora", TEXT_EMPTY),
    ("images", "lora_revision", TEXT_EMPTY),
    ("images", "lora_sha256", TEXT_EMPTY),
    # Issue #86: durable image archive state. The canonical digest detects later source corruption.
    ("images", "sha256", TEXT_EMPTY),
    ("images", "archive_bytes", INT_ZERO),
    ("images", "archived_at", "REAL"),
    ("images", "archive_error", TEXT_EMPTY),
    ("images", "archive_deleted_at", "REAL"),
    # Issue #87: opt-in Real-ESRGAN derived images keep the original PNG unchanged.
    ("images", "parent_id", TEXT_EMPTY),
    # Issue #88: masked edits retain their pinned model revision and feathering input.
    ("images", "operation", "TEXT NOT NULL DEFAULT 'generate'"),
    ("images", "model_revision", TEXT_EMPTY),
    ("images", "feather", INT_ZERO),
    ("images", "scale", "INTEGER NOT NULL DEFAULT 1"),
    ("images", "upscale_model", TEXT_EMPTY),
    ("images", "requested_upscale", "TEXT NOT NULL DEFAULT 'none'"),
    # Issue #29: usage attribution names the credential class, never the key or its file reference.
    ("usage", "credential_source", "TEXT NOT NULL DEFAULT 'subscription'"),
    # Issue #63: '' (credential-free public clone) or 'github' (the member's own GitHub connection).
    ("member_projects", "source_auth", TEXT_EMPTY),
    # Issue #17: frozen owner-approved instruction skills for a session.
    ("sessions", "skills", TEXT_EMPTY_LIST),
    # Issue #18: sanitized smart-review recommendation on the ordinary approval row.
    ("approvals", "smart", TEXT_EMPTY_OBJECT),
    # Issue #66: freeze hosted effort at session start; app-scoped settings live beside the token.
    ("sessions", "effort", TEXT_EMPTY),
    # Issue #66: in-flight app sessions keep the defaults they started with if the app is revoked.
    ("sessions", "app_defaults", TEXT_EMPTY_OBJECT),
    # Issue #92: enough to reproduce a generation; old rows stay readable with {}.
    # Keep this PR's migration after every migration already present on main.
    ("images", "provenance", TEXT_EMPTY_OBJECT),
]

BASELINE_VERSION = 45
assert len(LEGACY_COLUMNS) == BASELINE_VERSION
