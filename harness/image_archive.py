"""Durable, de-duplicated archive for generated images.

The live gallery belongs to the image work directory.  This archive lives beside (not
inside) dated database snapshots so each PNG is stored once rather than once per night.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Database


class ImageArchiveError(RuntimeError):
    pass


class ImageArchive:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.enabled = bool(cfg.backup.enabled and cfg.images.enabled)
        self.root = Path(cfg.backup.dir) / "images"
        self.last_reconciliation: dict = {}

    def source(self, job: dict) -> Path:
        return Path(self.cfg.images.work_dir) / "images" / f"{job['id']}.png"

    def paths(self, job: dict) -> tuple[Path, Path]:
        iid = str(job.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", iid):
            raise ImageArchiveError("image job id is not safe to archive")
        stamp = datetime.fromtimestamp(float(job["created_at"]), timezone.utc)
        folder = self.root / f"{stamp.year:04d}" / f"{stamp.month:02d}"
        return folder / f"{iid}.png", folder / f"{iid}.json"

    @staticmethod
    def _hash(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes) -> None:
        partial = path.with_name(path.name + ".partial")
        try:
            with partial.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(partial, path)
        finally:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def _atomic_copy(cls, source: Path, dest: Path, expected_size: int, expected_hash: str) -> None:
        partial = dest.with_name(dest.name + ".partial")
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, partial.open("wb") as out:
                while chunk := src.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise ImageArchiveError("source PNG changed while it was being archived")
            os.replace(partial, dest)
        finally:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _metadata(job: dict, size: int, digest: str) -> dict:
        created = datetime.fromtimestamp(float(job["created_at"]), timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "job_id": job["id"],
            "created_at": created,
            "source": job["source"],
            "model": job["model"],
            "workflow": job["model"],
            "prompt": job["prompt"],
            "dimensions": {"width": job["width"], "height": job["height"]},
            "seed": job["seed"],
            "bytes": size,
            "sha256": digest,
        }

    def _free_space(self) -> dict:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError as e:
            return {"free_bytes": 0, "free_space_warning": f"archive free space could not be measured: {e}"}
        minimum = int(self.cfg.backup.image_archive_min_free_gb * 2**30)
        warning = ""
        if free < minimum:
            warning = (f"image archive has {free / 2**30:.1f} GB free; "
                       f"configured warning threshold is {self.cfg.backup.image_archive_min_free_gb:g} GB")
        return {"free_bytes": free, "free_space_warning": warning}

    def archive(self, job: dict, source: Path | None = None) -> dict:
        """Archive one PNG, then mark its row only after target verification.

        Existing matching files are reused.  A stale ``.partial`` is overwritten and
        removed, making a retry after interruption safe.
        """
        if not self.enabled:
            return {"archived": False, "disabled": True}
        source = source or self.source(job)
        if not source.is_file():
            raise ImageArchiveError("source PNG is missing")
        size, digest = self._hash(source)
        if int(job.get("bytes") or size) != size:
            raise ImageArchiveError("source PNG byte count does not match the completed image row")
        expected = str(job.get("sha256") or "")
        if expected and expected != digest:
            raise ImageArchiveError("source PNG SHA-256 does not match the completed image row")

        png, sidecar = self.paths(job)
        png.parent.mkdir(parents=True, exist_ok=True)
        valid = False
        if png.is_file():
            try:
                valid = self._hash(png) == (size, digest)
            except OSError:
                valid = False
        if not valid:
            self._atomic_copy(source, png, size, digest)
        archived_size, archived_hash = self._hash(png)
        if archived_size != size or archived_hash != digest:
            raise ImageArchiveError("archived PNG failed byte-count or SHA-256 verification")

        metadata = (json.dumps(self._metadata(job, size, digest), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            current = sidecar.read_bytes()
        except OSError:
            current = b""
        if current != metadata:
            self._atomic_bytes(sidecar, metadata)
        # Read it back as part of the same success boundary; malformed/truncated JSON is never marked archived.
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        if saved.get("job_id") != job["id"] or saved.get("bytes") != size or saved.get("sha256") != digest:
            raise ImageArchiveError("image archive metadata failed verification")

        archived_at = float(job.get("archived_at") or time.time())
        self.db.update_image(job["id"], sha256=digest, archive_bytes=size, archived_at=archived_at,
                             archive_error="", archive_deleted_at=None)
        return {"archived": True, "bytes": size, "sha256": digest, "archived_at": archived_at}

    def record_error(self, job: dict, error: Exception | str) -> None:
        self.db.update_image(job["id"], archive_error=str(error)[:1000], archived_at=None, archive_bytes=0)

    def reconcile(self, now: float | None = None) -> dict:
        now = now or time.time()
        report = {"last_reconciliation": now, "archived": 0, "missing": 0, "errors": 0, "bytes": 0,
                  "warnings": []}
        if not self.enabled:
            report.update({"enabled": False})
            self.last_reconciliation = report
            return report
        report.update({"enabled": True, "path": str(self.root), **self._free_space()})
        if report["free_space_warning"]:
            report["warnings"].append(report["free_space_warning"])
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for partial in self.root.rglob("*.partial"):
                try:
                    partial.unlink()
                except OSError as e:
                    report["warnings"].append(f"could not remove interrupted archive file: {e}")
        except OSError as e:
            report["errors"] = 1
            report["warnings"].append(f"image archive destination is not writable: {e}")
            self.last_reconciliation = report
            return report

        for job in self.db.images_for_archive():
            if job.get("archive_deleted_at") is not None:
                continue
            png, sidecar = self.paths(job)
            expected = str(job.get("sha256") or "")
            valid = False
            if png.is_file() and sidecar.is_file():
                try:
                    size, digest = self._hash(png)
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    valid = (meta.get("job_id") == job["id"] and meta.get("bytes") == size
                             and meta.get("sha256") == digest and (not expected or expected == digest))
                except (OSError, ValueError, TypeError):
                    valid = False
                if valid:
                    self.db.update_image(job["id"], sha256=digest, archive_bytes=size,
                                         archived_at=job.get("archived_at") or now, archive_error="")
                    report["archived"] += 1
                    report["bytes"] += size
                    continue
            try:
                result = self.archive(job)
                report["archived"] += 1
                report["bytes"] += result["bytes"]
            except (OSError, ValueError, ImageArchiveError) as e:
                self.record_error(job, e)
                report["missing"] += 1
                report["errors"] += 1
                report["warnings"].append(f"image {job['id']}: {e}")
        self.last_reconciliation = report
        return report

    def health(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "last_reconciliation": 0, "archived": 0, "missing": 0,
                    "errors": 0, "bytes": 0, "retained": 0}
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN archived_at IS NOT NULL THEN 1 ELSE 0 END) AS archived, "
                "SUM(CASE WHEN archive_error != '' THEN 1 ELSE 0 END) AS errors, "
                "SUM(CASE WHEN archive_deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS retained, "
                "COALESCE(SUM(archive_bytes), 0) AS bytes FROM images WHERE status = 'done'"
            ).fetchone()
        archived, errors, retained = int(row["archived"] or 0), int(row["errors"] or 0), int(row["retained"] or 0)
        health = {"enabled": True, "path": str(self.root),
                  "last_reconciliation": self.last_reconciliation.get("last_reconciliation", 0),
                  "archived": archived, "missing": max(0, int(row["total"] or 0) - archived - retained),
                  "errors": errors, "retained": retained, "bytes": int(row["bytes"] or 0),
                  "retention_days": self.cfg.backup.image_archive_keep_days,
                  **self._free_space()}
        return health

    def _retention_candidates(self, now: float) -> list[dict]:
        days = self.cfg.backup.image_archive_keep_days
        if days <= 0:
            return []
        cutoff = now - days * 86400
        return [job for job in self.db.images_for_archive()
                if job.get("archived_at") is not None and float(job["created_at"]) < cutoff]

    @staticmethod
    def _confirmation(jobs: list[dict]) -> str:
        content = "\n".join(f"{j['id']}:{j.get('archive_bytes') or 0}:{j.get('sha256') or ''}" for j in jobs)
        return hashlib.sha256(content.encode()).hexdigest() if jobs else ""

    def retention_preview(self, now: float | None = None) -> dict:
        now = now or time.time()
        jobs = self._retention_candidates(now) if self.enabled else []
        return {"enabled": self.enabled, "keep_days": self.cfg.backup.image_archive_keep_days,
                "count": len(jobs), "bytes": sum(int(j.get("archive_bytes") or 0) for j in jobs),
                "confirmation": self._confirmation(jobs)}

    def apply_retention(self, confirmation: str, now: float | None = None) -> dict:
        now = now or time.time()
        jobs = self._retention_candidates(now) if self.enabled else []
        expected = self._confirmation(jobs)
        if not expected or confirmation != expected:
            raise ImageArchiveError("retention preview is missing or stale; preview the affected count and bytes again")
        removed = 0
        removed_bytes = 0
        errors: list[str] = []
        for job in jobs:
            png, sidecar = self.paths(job)
            try:
                for path in (sidecar, png):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                self.db.update_image(job["id"], archived_at=None, archive_bytes=0, archive_error="",
                                     archive_deleted_at=now)
                removed += 1
                removed_bytes += int(job.get("archive_bytes") or 0)
            except OSError as e:
                errors.append(f"image {job['id']}: {e}")
        return {"removed": removed, "bytes": removed_bytes, "errors": errors}
