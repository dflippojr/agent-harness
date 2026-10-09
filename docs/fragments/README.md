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
markers fails, and running `build.py` yourself also passes. Without `--base` (on main, locally) only this tree's
fragments count, so main stays in sync. The post-merge job (a separate issue) is what rewrites regions on main;
PRs should add fragments only.
