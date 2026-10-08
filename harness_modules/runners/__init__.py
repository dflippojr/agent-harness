"""Optional remote runner and Mac client support."""
from harness.modules import Module


def _runtime(manager, module):
    from .runtime import RunnersRuntime
    return RunnersRuntime(manager, module)


def _owner_routes():
    from .routes import owner_routes
    return owner_routes


def _public_routes():
    from .routes import public_routes
    return public_routes


def _capabilities(owner, scopes):
    return {"runners": owner}


MODULE = Module(
    name="runners", switches=("runners",), title="Remote runners and Mac client",
    docs=("docs/mac-client.md", "docs/modules.md"), runtime=_runtime,
    owner_routes=_owner_routes, public_routes=_public_routes,
    principal_capabilities=_capabilities,
    admin_paths=frozenset({"/runners", "/runners/{name}/update", "/runner-pairing-codes",
                           "/runner-pairing-codes/{pid}"}),
    cli=(
        ("runner-pairing-codes list", "GET", "/runner-pairing-codes", "list active Mac pairing codes", ()),
        ("runner-pairing-codes create", "POST", "/runner-pairing-codes", "make a one-time Mac pairing code",
         ("--name", "--runner", "--ttl_seconds:int")),
        ("runner-pairing-codes revoke", "DELETE", "/runner-pairing-codes/{pid}", "revoke a Mac pairing code", ()),
        ("runner list", "GET", "/runners", "list the server's runners", ()),
        ("runner update", "POST", "/runners/{name}/update", "ask a runner to update itself", ()),
    ),
    cli_groups={"runner-pairing-codes": "Mac pairing codes"},
)
