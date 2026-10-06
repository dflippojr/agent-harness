# SQLite schema migrations

The daemon's database (`<data dir>/harness.sqlite3`) records its schema version in `PRAGMA user_version`. `Database(path)`
brings it up to date on every start, before the daemon serves anything (issue #256).

## How a database gets to the current version

1. **Version 0** (a new file, or any install from before versioning): run `SCHEMA`, `APP_SETTINGS_SCHEMA` and
   the frozen add-column list `harness/migrations/baseline.py::LEGACY_COLUMNS`, then stamp version **45**.
   This step is idempotent, so it also repairs an old install missing some legacy columns. No backup.
2. **Numbered steps**: every `harness/migrations/NNNN_<name>.py` above the current version runs in order,
   each in its own `BEGIN IMMEDIATE` ... `COMMIT` together with its `user_version` bump.
3. **Too new**: if `user_version` is higher than the newest step this code knows, the daemon refuses to start
   (`SchemaTooNewError`) without writing to the file. Upgrade the harness or restore a backup
   (`python -m harness.backup_restore`, see INSTALL.md "Backups and restore").

`python -m harness.doctor` reports the version read-only: OK when current, WARN when behind (it migrates on
the next start), FAIL when too new.

## Adding a schema migration

- Add a new file `harness/migrations/NNNN_<short_name>.py`: four digits, the next number after the highest
  existing one (the first is `0046`), lowercase name. Numbers must be gap-free and unique; the loader fails
  fast on duplicates, gaps, or a module without `up`, so CI catches two PRs that picked the same number.
  If another PR merged first with your number, renumber yours.
- Expose one function:

  ```python
  def up(conn: sqlite3.Connection) -> None:
      conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
  ```

- Use `conn.execute` only. Never `executescript` (it commits implicitly, breaking the step's transaction)
  and never `BEGIN`/`COMMIT`/`ROLLBACK`: the runner owns the transaction.
- **Do not edit `SCHEMA` or `baseline.py`.** A fresh database is bootstrapped to version 45 and then runs the
  same numbered steps as an upgraded one, so both end with the same schema. Add new tables in a migration
  too (`CREATE TABLE ...`), not in `SCHEMA`.
- If the change needs a value in a JSON column, add the column name to `JSON_COLUMNS` in `harness/db.py` as
  before.
- Test it: open a database built at the previous version with `Database(path)` and compare against a fresh
  one (`tests/test_migrations.py` has the helpers; `Database(path, migrations=[(46, up), ...])` runs an
  explicit step list).

### Rebuilding a table

SQLite can't drop constraints, change a column type, or (portably) rename/drop columns in place. Rebuild:

```python
def up(conn):
    conn.execute("CREATE TABLE jobs_new (id TEXT PRIMARY KEY, name TEXT NOT NULL, ...)")
    conn.execute("INSERT INTO jobs_new (id, name, ...) SELECT id, name, ... FROM jobs")
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_name ON jobs(name)")  # recreate the table's indexes
```

Recreate every index and trigger the old table had; they are dropped with it.

## Backups and rollback

Before the first pending step on an existing database, the runner writes a verified copy to
`<data dir>/pre-migration/harness-v<from>-<YYYYmmddTHHMMSS>.sqlite3`. That includes a pre-versioning file
(version 0) that already held tables: it is bootstrapped to 45 first (add-only), then backed up as v45 before
any numbered step. Only a fresh, empty database gets no backup. These files are never deleted automatically.

If a step raises, that step is rolled back (its `user_version` bump with it), earlier steps stay applied, and
the daemon fails to start with a `MigrationError` naming the step and the backup path. Fixing the migration
and restarting resumes from the failed step.

There are no down-migrations. To roll back a release: stop the daemon, move `harness.sqlite3`, `harness.sqlite3-wal`
and `harness.sqlite3-shm` aside, copy the `pre-migration` file to `harness.sqlite3`, and start the older harness.

## Per-App stores

Each App's store (`<data dir>/apps/<app_id>/harness.sqlite3`, see [App API](app-api.md#where-an-apps-data-lives))
is a `Database` too. It runs the same steps when it opens, and a store that needs a numbered step gets its own
`<data dir>/apps/<app_id>/pre-migration/` backup. The one-time move of older App sessions out of the main store
backs up the main store to `<data dir>/pre-migration/harness-app-stores-<YYYYmmddTHHMMSS>.sqlite3` first. To roll
that move back, restore that file as above and delete `<data dir>/apps/`.

The next one-time move takes those App sessions' files (working directories, checkpoint snapshots, transcripts) out
of `<data dir>/workspaces/`, `checkpoints/` and `transcripts/` into the App's folder (`workspaces/`, `checkpoints/`,
`transcripts/` under `<data dir>/apps/<app_id>/`) and points each session's stored working directory there. Before
it changes an App's store it backs that store up to
`<data dir>/apps/<app_id>/pre-migration/harness-app-files-<YYYYmmddTHHMMSS>.sqlite3`. It moves the files first and
updates the paths last, so a start cut short is finished by the next one; a later start finds nothing to do. To roll
it back, stop the daemon, move the session folders and transcripts back to the owner's folders and restore that
backup over the App's `harness.sqlite3`.

<a id="web-store"></a>
### Agent Harness Web's store

Agent Harness Web is an App with its own store, `<data dir>/apps/app-web/harness.sqlite3` (#330 decision 4). At the
first start after the upgrade, right after the App sessions move above, every session left in the main store (the
owner's, whether started from Web, the CLI or the admin API, and the members') moves there with every row tied to
it: events, approvals, artifacts, checkpoints, App tool calls, smart reviews, review drafts and secret dismissals.
The search index is rebuilt in Web's store from the events. The step:

1. backs up the main store to `<data dir>/pre-migration/harness-web-store-<YYYYmmddTHHMMSS>.sqlite3` (never deleted
   automatically; a second attempt in the same second gets `-1`, `-2`, ...);
2. copies the rows in one transaction on Web's store, which compares its row counts with the main store's, table by
   table, and rolls back on any difference;
3. deletes the sessions from the main store, registers Web in the App registry (`api_keys` row `app-web`, kind
   `web`, no usable token) and records the move in the `web_store` meta key, all in one transaction.

It logs the counts before and after. If anything fails the main store is left as it was, the daemon keeps serving the
sessions from it, and the next start tries again. A later start finds no sessions in the main store and writes
nothing. Session files don't move: their rows already point at them, and working directories can be large.

`HARNESS_WEB_STORE_MIGRATION=dry-run` logs what would move (sessions and rows per table) and changes nothing: the
main store keeps serving the sessions. No new schema step is needed (Web's row uses the existing `api_keys` columns).

To roll the move back: stop the daemon, restore the `harness-web-store-*` backup over `harness.sqlite3` as above,
move `<data dir>/apps/app-web/` aside, and start the older harness.

Step 0050 (`app_retention`) adds `sessions.retention_days` and, on the App registry (`api_keys`), `retention_days`,
`erase_after` and `erased_at` (#330 decision 5).

Step 0051 (`end_users`) adds the `end_users` registry table (read through `db.for_app(app_id)`, so it lives in the App's
own store and goes with `drop_app`), `sessions.end_user` and `usage.end_user` (#365).

Step 0052 (`member_api_keys`) adds the `member_api_keys` table to the main store: one AES-GCM-sealed provider API key per
(member, backend) plus its last four characters (#393). The master key is `<data_dir>/member-keys.key`, outside the database.
