# Docs fragments

Parallel PRs used to conflict on, or drift in, the same doc sections. A PR now adds **one fragment file** here and
never edits a generated region. `scripts/docs/build.py` (stdlib plus PyYAML, no LLM, well under a second) assembles
the sections from the fragments.

## Fragment file

One YAML file per change, named `<issue>-<slug>.yaml` (digits, a dash, lowercase words joined by dashes), for
example `498-install.yaml`.

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Must be `1`. Bumped only for incompatible changes. |
| `kind` | yes | `module`, `doc-index`, `api-note` or `operator-note`. Each target accepts one kind. |
| `target` | yes | The generated section this feeds. Must be a key of `TARGETS` in `scripts/docs/build.py`. |
| `title` | yes | Single line, no `\|`. For `doc-index`, the row's Topic cell. |
| `summary` | no | Free text for kinds whose renderer uses it (`doc-index` ignores it). |
| `links` | `doc-index`: yes | List of `{text, href}`; rendered `[text](href)` joined with ` · `. |
| `order` | no | Integer, default `0`. Fragments sort by `(order, file name)`, so output is stable. |

Unknown fields, a wrong `kind` for the target and an unknown `target` are all rejected with the file name in the
message.

### Targets

| Target | File | Kind |
| --- | --- | --- |
| `readme-docs-index` | `README.md`, the `## Documentation` table | `doc-index` |

Other sections move over in follow-up issues; to convert one, wrap it in markers and add a `TARGETS` entry.

## Generated regions

```markdown
<!-- generated:begin readme-docs-index -->
...rewritten by build.py...
<!-- generated:end readme-docs-index -->
```

Only text between the markers changes. Everything outside is never touched.

## Commands

```
python scripts/docs/build.py                       # write all regions
python scripts/docs/build.py --check               # validate; region must equal this branch's fragments
python scripts/docs/build.py --check --base origin/main   # what CI runs on a PR
```

## The rule `--check` enforces

1. Every fragment must pass the schema.
2. Each generated region must equal the output of **either** the base branch's fragments (`--base`) **or** this
   branch's fragments (always).

So adding a fragment and leaving the region alone passes (region still equals main's output), a hand edit inside the
markers fails, and running `build.py` yourself also passes. Without `--base` (locally) only this tree's
fragments count. On main, CI runs `--check --fragments-only` (schema only) and `docs-regen.yml` rewrites the regions
after each merge (see `docs/CI-CD.md`); PRs should add fragments only.

## Tables generated from code (#500)

Three regions need no fragment: `scripts/docs/code_tables.py` derives them from the code itself.

| Region | File | Source |
| --- | --- | --- |
| `app-api-endpoints` | `docs/app-api.md` | every `/api/v1` route registration |
| `admin-api-endpoints` | `docs/admin-api.md` | every `/api/admin/v1` registration, plus the unversioned owner routes named in `ADMIN_PATHS` and each module's `admin_paths` |
| `config-registry-keys` | `docs/config-registry.md` | the settings registry (`build_registry`) under a pinned profile with every module present |

Routes are read statically from the `RouteTable` decorators (`@x_router`, `@x_routes`, `route_table`, `@app`), so the
daemon is not imported; the whole run takes well under a second. A route's summary is its `summary=` argument, else
the first line of the handler's docstring, else `TODO`. Auth is only what the handler itself asks for (`require_owner`
or `auth(request, "scope")`); `see source` means a helper decides. The source column is file and function name, not a
line number, so unrelated edits do not make the table stale. Settings use `SettingSpec.help` as the description;
installer-only, path-like, secret and per-backend defaults print `—`.

`--check` lists each `TODO` as a `warning:` and still passes. It fails when a table disagrees with the code, with
the same single-writer rule as fragments: on a **PR** (`--base`) a table may equal the base branch's committed text
(the PR left it alone) or what the PR's code produces, so a code change does not have to touch the docs and parallel
PRs do not conflict; a **hand edit** fails. On **main** CI runs `--check --fragments-only` (schema only), so a table
that lags a just-merged code change never fails there; `docs-regen.yml` rewrites the regions after each merge. Locally,
`python scripts/docs/build.py` regenerates them. Prose around the tables stays hand-written.

The settings table imports the real registry, which needs only PyYAML for the import itself (the generator stubs
`httpx`, which the config loader imports but this path never calls), so the regeneration job needs no extra
dependency.
