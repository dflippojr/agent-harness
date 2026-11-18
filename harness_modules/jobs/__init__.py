"""Scheduled jobs and templates, registered through the optional module interface."""
from harness.modules import Module, enabled_flag, lazy


def _principal_capabilities(owner, scopes):
    return {"jobs": owner}


_JOB = ("name", "prompt", "cron", "--project", "--backend", "--model", "--notify", "--enabled:bool",
        "--catch_up_minutes:int")
_TEMPLATE = ("name", "prompt", "--project", "--backend", "--model")

MODULE = Module(
    name="jobs", switches=("jobs",), title="Scheduled jobs and templates",
    docs=("docs/modules.md",), runtime_enabled=enabled_flag("jobs"),
    runtime=lazy(".runtime", "JobsRuntime", call=True),
    owner_routes=lazy(".routes", "owner_routes"), settings=lazy(".settings", "specs", call=True),
    principal_capabilities=_principal_capabilities,
    admin_paths=frozenset(['/jobs', '/jobs/preview', '/jobs/{jid}', '/jobs/{jid}/run', '/templates', '/templates/{tid}']),
    cli=(
        ("jobs list", "GET", "/jobs", "list scheduled jobs", ()),
        ("jobs show", "GET", "/jobs/{jid}", "show a scheduled job", ()),
        ("jobs create", "POST", "/jobs", "schedule a job", _JOB),
        ("jobs update", "PUT", "/jobs/{jid}", "replace a scheduled job", _JOB),
        ("jobs delete", "DELETE", "/jobs/{jid}", "delete a scheduled job", ()),
        ("jobs run", "POST", "/jobs/{jid}/run", "run a scheduled job now", ()),
        ("jobs preview", "GET", "/jobs/preview", "show a cron expression's next runs", ("cron",)),
        ("templates list", "GET", "/templates", "list task templates", ()),
        ("templates create", "POST", "/templates", "save a task template", _TEMPLATE),
        ("templates update", "PUT", "/templates/{tid}", "replace a task template", _TEMPLATE),
        ("templates delete", "DELETE", "/templates/{tid}", "delete a task template", ()),
    ),
    cli_groups={"jobs": "scheduled jobs", "templates": "task templates"},
)
