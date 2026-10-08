"""Image persistence on the core's shared, transactional SQLite database.

The frozen schema stays in the core baseline so historical installs and backups
remain readable even while this add-on is absent. Live image queries belong here.
"""

from __future__ import annotations

import json
import sqlite3
import time

from harness.modules import AsyncDatabase, database_reads, database_writes

AND_JOIN = " AND "


class ImageStore:
    """Use the core writer queue and pooled readers without owning their lifetime."""

    def __init__(self, db):
        self._db = db
        self.aio = AsyncDatabase(self)

    def __getattr__(self, name):
        return getattr(self._db, name)

    # images
    @database_writes
    def insert_image(self, job: dict) -> None:
        cols = ["id", "session_id", "source", "prompt", "model", "aspect_ratio", "resolution", "width", "height",
                "seed", "base_model", "lora", "lora_revision", "lora_sha256",
                "parent_id", "operation", "model_revision", "feather", "scale", "upscale_model",
                "requested_upscale"]
        defaults = {"session_id": "", "resolution": "auto", "base_model": "", "lora": "", "lora_revision": "",
                    "lora_sha256": "", "parent_id": "", "operation": "generate", "model_revision": "",
                    "feather": 0, "scale": 1, "upscale_model": "", "requested_upscale": "none"}
        values = [job[c] if c in job else defaults[c] for c in cols]
        provenance = job.get("provenance") or {}
        status = job.get("status") or "queued"
        created = job.get("created_at") or time.time()
        with self.lock:
            self.conn.execute(
                f"INSERT INTO images ({','.join(cols)}, provenance, status, created_at) VALUES "
                f"({','.join('?' * len(cols))}, ?, ?, ?)",
                values + [json.dumps(provenance), status, created])

    @database_writes
    def update_image(self, iid: str, **fields) -> None:
        if "provenance" in fields and not isinstance(fields["provenance"], str):
            fields = {**fields, "provenance": json.dumps(fields["provenance"])}
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE images SET {sets} WHERE id = ?", [*fields.values(), iid])

    @database_reads
    def get_image(self, iid: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM images WHERE id = ?", (iid,)).fetchone()
        return self._image_row(row)

    @database_writes
    def delete_image(self, iid: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM images WHERE id = ?", (iid,)).rowcount == 1

    @database_reads
    def list_images(self, limit: int = 60, status: tuple = (), operations: tuple = ()) -> list[dict]:
        query, params = "SELECT * FROM images", []
        clauses = []
        if status:
            clauses.append(f"status IN ({','.join('?' * len(status))})")
            params.extend(status)
        if operations:
            clauses.append(f"COALESCE(operation, 'generate') IN ({','.join('?' * len(operations))})")
            params.extend(operations)
        if clauses:
            query += " WHERE " + AND_JOIN.join(clauses)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY created_at DESC LIMIT ?", [*params, limit]).fetchall()
        return [self._image_row(r) for r in rows]

    def _image_row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        raw = out.get("provenance")
        if isinstance(raw, str):
            try:
                out["provenance"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                out["provenance"] = {}
        elif raw is None:
            out["provenance"] = {}
        return out

    @database_reads
    def images_for_archive(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM images WHERE status = 'done' ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    @database_reads
    def find_image_upscale(self, parent_id: str, upscale: str) -> dict | None:
        """Return the existing derived upscale for this parent and scale, if any (including failed)."""
        choice = str(upscale or "").strip().lower().replace("×", "x")
        scale = 0
        if choice in ("2x", "2"):
            scale = 2
        elif choice in ("4x", "4"):
            scale = 4
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM images WHERE parent_id = ? AND operation = 'upscale' AND "
                "(requested_upscale = ? OR scale = ?) ORDER BY created_at DESC LIMIT 1",
                (parent_id, choice, scale)).fetchone()
        return dict(row) if row else None

    @database_reads
    def image_children(self, parent_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM images WHERE parent_id = ? ORDER BY created_at DESC", (parent_id,)).fetchall()
        return [dict(r) for r in rows]

