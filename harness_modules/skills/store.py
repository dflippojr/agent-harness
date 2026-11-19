"""Instruction-skill persistence on the core's shared, transactional SQLite database (#17, #992).

The skill tables stay in the core's frozen baseline, so existing installs and backups keep their schema while this
add-on is absent (#334). Their queries live here: `SkillRecordStore(db)` runs them on the core's writer queue and pooled
readers.
"""

from __future__ import annotations

import json
import sqlite3
import time

from harness.modules import ModuleStore, database_reads, database_writes

# The skill tables' JSON columns: stored as JSON text, read back as lists and objects.
_JSON = frozenset({"references", "examples", "manifest", "static_findings", "review", "findings"})


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in _JSON & out.keys():
        val = out[key]
        if isinstance(val, str) and val[:1] in "{[":
            try:
                out[key] = json.loads(val)
            except json.JSONDecodeError:
                pass
    return out


def _rows(rows: list[sqlite3.Row]) -> list[dict]:
    return [out for r in rows if (out := _row(r)) is not None]


class SkillRecordStore(ModuleStore):
    """Skill proposals, installed skills, their versions and project allowlists, rejected hashes and review jobs."""

    @database_writes
    def insert_skill_proposal(self, row: dict) -> None:
        cols = ("id", "slug", "title", "purpose", "activation_suggestion", "content_hash", "status",
                "source_session_id", "skill_md", "references", "examples", "manifest", "static_findings",
                "review", "review_status", "target_slug", "diff", "created_at", "updated_at")
        values = [json.dumps(row[c]) if c in _JSON else row[c] for c in cols]
        sql_cols = [f'"{c}"' if c == "references" else c for c in cols]
        with self.lock:
            self.conn.execute(
                f"INSERT INTO skill_proposals ({','.join(sql_cols)}) VALUES ({','.join('?' * len(cols))})", values)

    @database_writes
    def update_skill_proposal(self, pid: str, *, expected_status: str | tuple[str, ...] | None = None,
                              **fields) -> bool:
        if not fields:
            return False
        if "status" in fields and expected_status is None:
            raise ValueError("skill proposal status changes require expected_status")
        fields["updated_at"] = time.time()
        sets = ", ".join(f'"{k}" = ?' if k == "references" else f"{k} = ?" for k in fields)
        values = [json.dumps(v) if k in _JSON else v for k, v in fields.items()]
        query = f"UPDATE skill_proposals SET {sets} WHERE id = ?"
        params = [*values, pid]
        if expected_status is not None:
            statuses = (expected_status,) if isinstance(expected_status, str) else tuple(expected_status)
            query += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        with self.lock:
            return self.conn.execute(query, params).rowcount == 1

    @database_reads
    def skill_proposal(self, pid: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_proposals WHERE id = ?", (pid,)).fetchone())

    @database_reads
    def skill_proposal_by_hash(self, content_hash: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(
                "SELECT * FROM skill_proposals WHERE content_hash = ? ORDER BY created_at DESC LIMIT 1",
                (content_hash,)).fetchone())

    @database_reads
    def list_skill_proposals(self, slug: str | None = None) -> list[dict]:
        query, params = "SELECT * FROM skill_proposals", []
        if slug:
            query += " WHERE slug = ?"
            params.append(slug)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY created_at DESC", params).fetchall()
        return _rows(rows)

    @database_reads
    def skill_proposal_count(self, since: float, session_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM skill_proposals WHERE created_at >= ?"
        params: list = [since]
        if session_id:
            query += " AND source_session_id = ?"
            params.append(session_id)
        with self.lock:
            return int(self.conn.execute(query, params).fetchone()[0])

    @database_writes
    def delete_skill_proposal(self, pid: str, *, not_status: str | tuple[str, ...] | None = None) -> bool:
        query = "DELETE FROM skill_proposals WHERE id = ?"
        params: list = [pid]
        if not_status is not None:
            statuses = (not_status,) if isinstance(not_status, str) else tuple(not_status)
            query += f" AND status NOT IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        with self.lock:
            return self.conn.execute(query, params).rowcount == 1

    @database_writes
    def reject_skill_hash(self, content_hash: str, proposal_id: str, reason: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO skill_rejected (content_hash, proposal_id, reason, rejected_at) "
                "VALUES (?, ?, ?, ?)", (content_hash, proposal_id, reason, time.time()))

    @database_writes
    def clear_rejected_skill_hash(self, content_hash: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_rejected WHERE content_hash = ?", (content_hash,))

    @database_reads
    def skill_hash_rejected(self, content_hash: str) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM skill_rejected WHERE content_hash = ?",
                                    (content_hash,)).fetchone()
        return row is not None

    @database_reads
    def skill_installed(self, slug: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_installed WHERE slug = ?", (slug,)).fetchone())

    @database_reads
    def list_skill_installed(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM skill_installed ORDER BY slug").fetchall()
        return _rows(rows)

    @database_writes
    def upsert_skill_installed(self, row: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skill_installed (slug, title, purpose, current_version, current_hash, enabled, "
                "installed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET title=excluded.title, purpose=excluded.purpose, "
                "current_version=excluded.current_version, current_hash=excluded.current_hash, "
                "enabled=excluded.enabled, updated_at=excluded.updated_at",
                (row["slug"], row["title"], row.get("purpose") or "", row["current_version"], row["current_hash"],
                 int(row.get("enabled") or 0), row["installed_at"], row["updated_at"]))

    @database_writes
    def delete_skill_installed(self, slug: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_installed WHERE slug = ?", (slug,))

    @database_writes
    def insert_skill_version(self, row: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skill_versions (slug, version, content_hash, title, purpose, skill_md, \"references\", "
                "examples, manifest, installed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["slug"], row["version"], row["content_hash"], row["title"], row.get("purpose") or "",
                 row["skill_md"], json.dumps(row.get("references") or []), json.dumps(row.get("examples") or []),
                 json.dumps(row.get("manifest") or {}), row["installed_at"]))

    @database_reads
    def skill_version(self, slug: str, version: int) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_versions WHERE slug = ? AND version = ?",
                                          (slug, version)).fetchone())

    @database_reads
    def skill_version_by_hash(self, content_hash: str) -> dict | None:
        with self.lock:
            return _row(self.conn.execute("SELECT * FROM skill_versions WHERE content_hash = ?",
                                          (content_hash,)).fetchone())

    @database_reads
    def list_skill_versions(self, slug: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM skill_versions WHERE slug = ? ORDER BY version", (slug,)).fetchall()
        return _rows(rows)

    @database_reads
    def skill_allowlist(self, slug: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT project FROM skill_project_allowlist WHERE slug = ? ORDER BY project", (slug,)).fetchall()
        return [r[0] for r in rows]

    @database_reads
    def skill_allowlisted_slugs(self, project: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT slug FROM skill_project_allowlist WHERE project = ? ORDER BY slug", (project,)).fetchall()
        return [r[0] for r in rows]

    @database_writes
    def set_skill_allowlist(self, slug: str, projects: list[str]) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_project_allowlist WHERE slug = ?", (slug,))
            self.conn.executemany("INSERT INTO skill_project_allowlist (project, slug) VALUES (?, ?)",
                                  [(p, slug) for p in projects])

    @database_writes
    def clear_skill_allowlist(self, slug: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM skill_project_allowlist WHERE slug = ?", (slug,))

    @database_writes
    def insert_skill_review_job(self, row: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO skill_review_jobs (id, proposal_id, content_hash, status, mode, findings, error, "
                "created_at, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["proposal_id"], row["content_hash"], row["status"], row["mode"],
                 json.dumps(row.get("findings") or {}), row.get("error") or "", row["created_at"],
                 row.get("started_at"), row.get("finished_at")))

    @database_writes
    def update_skill_review_job(self, jid: str, **fields) -> None:
        values = [json.dumps(v) if k in _JSON else v for k, v in fields.items()]
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE skill_review_jobs SET {sets} WHERE id = ?", [*values, jid])

    @database_reads
    def list_skill_review_jobs(self, status: tuple[str, ...] | None = None) -> list[dict]:
        query, params = "SELECT * FROM skill_review_jobs", []
        if status:
            query += f" WHERE status IN ({','.join('?' * len(status))})"
            params.extend(status)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY created_at", params).fetchall()
        return _rows(rows)
