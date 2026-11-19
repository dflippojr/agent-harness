"""Scheduled-job and task-template persistence on the core's shared, transactional SQLite database (#992).

The jobs and templates tables stay in the core's frozen baseline, so existing installs and backups keep their schema
while this add-on is absent (#334). Their queries live here: `JobStore(db)` runs them on the core's writer queue and
pooled readers. A job's sessions are core session rows, read through the core (`Database.job_sessions`).
"""

from __future__ import annotations

import time

from harness.modules import ModuleStore, database_reads, database_writes


class JobStore(ModuleStore):
    """Scheduled jobs and task templates."""

    @database_reads
    def list_jobs(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM jobs ORDER BY name").fetchall()]

    @database_reads
    def get_job(self, jid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return dict(row) if row else None

    @database_writes
    def insert_job(self, job: dict) -> None:
        now = time.time()
        row = {**job, "created_at": now, "updated_at": now}
        with self.lock:
            self.conn.execute(f"INSERT INTO jobs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                              [int(v) if isinstance(v, bool) else v for v in row.values()])

    @database_writes
    def update_job(self, jid: str, **fields) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                              [*(int(v) if isinstance(v, bool) else v for v in fields.values()), jid])

    @database_writes
    def delete_job(self, jid: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount == 1

    # templates
    @database_reads
    def list_templates(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM templates ORDER BY name").fetchall()]

    @database_reads
    def get_template(self, tid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM templates WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    @database_writes
    def upsert_template(self, t: dict) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO templates (id, name, project, backend, model, prompt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                "project = excluded.project, backend = excluded.backend, model = excluded.model, prompt = excluded.prompt, "
                "updated_at = excluded.updated_at",
                (t["id"], t["name"], t["project"], t.get("backend") or "local", t.get("model") or "",
                 t["prompt"], now, now),
            )

    @database_writes
    def delete_template(self, tid: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM templates WHERE id = ?", (tid,)).rowcount == 1
