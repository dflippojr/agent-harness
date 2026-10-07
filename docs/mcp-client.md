# Owner-pinned MCP client

The optional `harness_modules.mcp_client` module offers owner-configured stdio MCP tools to native local-model
agent sessions on the tower. It is off by default, including in the full profile. Enable `modules.mcp_client: true`
in the installer/file configuration. If `module_packages` is explicit, include `harness_modules.mcp_client`.
An absent or unselected module contributes no tools or settings keys. There are no server-management API or CLI
operations in v1: server configuration is file-only, per the owner's #260 decision.

Add `mcp_servers` to an owner project in `projects.yaml`:

```yaml
projects:
  example:
    mcp_servers:
      - name: example
        image: owner/example@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        command: [example-server, --stdio]
        mount_workspace: false
        env:
          API_TOKEN: {secret_file: C:/private/example-token}
        rules:
          - {tool: mcp__example__lookup, action: ask}
```

The owner must preinstall the exact image digest locally; the client uses `--pull=never`. Tags without digests,
URL entries, the reserved name `harness`, duplicate or overlapping server namespaces and member-project entries
are rejected. A name may contain underscores, but a project cannot configure both `a` and `a__b`, whose tool
prefixes overlap. Environment
values come only from UTF-8 secret files. Values are passed through Docker's process environment, never command
arguments; the daemon's other environment credentials are excluded. Do not put secrets in the command itself.

Each configured server gets a separate container for each session at its first native tool listing. Containers
use `--network none`, dropped capabilities, `no-new-privileges`, and the configured sandbox memory/CPU/pids limits.
They have no host mount unless `mount_workspace: ro` or `rw` explicitly grants the session workspace. They never
receive the Docker socket or harness login volumes. Egress approvals and allowlists do not affect these containers.
Containers are removed when a run finishes, fails, is cancelled or the daemon shuts down. A follow-up run recreates
them; an old sidecar left by a daemon crash is removed before reconnection.

Tools are named `mcp__<server>__<tool>`. They default to `ask`; project policy rules and per-server `rules` use
the complete name and can allow or deny. Other server namespaces remain denied even under wildcard allow rules.
Calls use ordinary approval, tool-call and result events, context-efficiency accounting, persistent session taint,
and an `mcp_tool_call` trace span. Hosted backends, Chat, App-tools-only and member sessions do not receive them.

The client implements newline-delimited JSON-RPC and initialization for protocol `2024-11-05`, as described in
the [MCP stdio transport](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports) and
[lifecycle](https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle) specifications. It lists tools
once per run, including pagination, with at most 128 tools and 1 MB per protocol message. Response timeout is
30 seconds. Resources, prompts, sampling, elicitation and remote HTTP transport are outside v1. Server-initiated
requests receive an unsupported-method response. Tests use a local Python fake stdio server and inspect Docker
argv; they never pull or run an MCP image.
Arguments are validated against the complete input schema before approval. Local schema references are supported;
external schema retrieval is disabled, so schemas cannot cause network requests from the daemon.
Every tool on a server with a rw workspace grant participates in native workspace checkpoints, rewind and quota checks, even if its schema claims it is read-only.
