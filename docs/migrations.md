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
   (`SchemaTooNewError`) without writing to the file. Upgrade the harness or restore a backup.

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
