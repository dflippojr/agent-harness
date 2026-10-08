"""Image generation inside a running daemon: the ModuleRuntime the core drives (harness/modules.py)."""

from __future__ import annotations

import asyncio

from harness.modules import ModuleRuntime, ServerControl

from .archive import ImageArchive


class ImagesRuntime(ModuleRuntime):
    """The archive exists whenever the module is present (backups and retention work with generation switched
    off); the ImageService only while ``images.enabled`` is on."""

    def __init__(self, manager, module):
        super().__init__(manager, module)
        self.archive = ImageArchive(manager.cfg, manager.db)
        self.backup = self.archive
        self.service = None   # service.ImageService while image generation is switched on

    def init(self) -> None:
        if not self.effective("images"):
            return
        from .service import ImageService
        cfg, m = self.cfg, self.manager
        self.service = ImageService(cfg.images, m.db, m.runner,
                                    ServerControl(cfg.gpu_guard, cfg.models[cfg.default_model]),
                                    notify=self._finished, archive=self.archive,
                                    edit_enabled=self.effective("image_edit"))

    def _finished(self, job: dict) -> None:
        notifier, ok = self.manager.notifier, job["status"] == "done"
        notifier.send({"topic": self.cfg.notify.topic, "title": "Image ready" if ok else "Image failed",
                       "message": (job["prompt"][:200] if ok else job["error"][:300]), "priority": 2 if ok else 3,
                       "tags": ["frame_with_picture" if ok else "x"],
                       "click": notifier.link(f"/#/images/{job['id']}")})

    def wire_resources(self, guard, warmer) -> None:
        if self.service is not None:
            self.service.memory_low = lambda: guard.memory.low()
            self.service.want_model = lambda: not self.cfg.gpu_guard.lazy_load or warmer.pinned()

    def start(self) -> None:
        if self.service is not None:
            self.service.start()

    async def stop(self) -> None:
        if self.service is not None:
            await self.service.stop()

    def toolkit(self):
        return self.service

    @property
    def gpu_taken(self) -> bool:
        return self.service is not None and self.service.gpu_taken

    def busy(self) -> bool:
        return self.service is not None and self.service.phase != "idle"

    def gpu_holders(self) -> tuple[str, ...]:
        return ("ComfyUI",) if self.gpu_taken else ()

    def gpu_hold(self) -> None:
        if self.service is not None:
            self.service.hold()

    def gpu_resume(self, session_ids) -> None:
        if self.service is not None:
            self.service.drain_after_sessions(session_ids)

    def features(self) -> dict:
        return {"images": self.service is not None, "image_upscale": self.service is not None}

    async def app_root(self) -> dict:
        return {"image_modes": (await asyncio.to_thread(self.service.mode_catalog)) if self.service else {}}

    def metrics(self, out, db) -> None:
        svc = self.service
        if svc is not None:
            with db.lock:
                images = db.conn.execute("SELECT model, source, status, COUNT(*), COALESCE(SUM(seconds), 0) "
                                         "FROM images GROUP BY 1, 2, 3").fetchall()
            out.metric("harness_images_total", "counter", "Image jobs by model, source and status.",
                       [({"model": mo, "source": so, "status": st}, n) for mo, so, st, n, _ in images])
            out.metric("harness_images_seconds_total", "counter", "Time spent on image jobs (ComfyUI execution).",
                       [({"model": mo}, sum(s for mo2, _, st, _, s in images if mo2 == mo and st == "done"))
                        for mo in sorted({row[0] for row in images})])
            out.metric("harness_images_gpu_taken", "gauge",
                       "1 while image generation or upscaling has the GPU (language model unloaded).",
                       [({}, 1 if svc.gpu_taken else 0)])
            out.metric("harness_images_queued", "gauge", "Image jobs waiting.", [({}, svc.queue.qsize())])
            upscale = svc.status().get("upscale") or {}
            out.metric("harness_images_upscale_available", "gauge",
                       "1 when optional Real-ESRGAN 2×/4× weights are installed.",
                       [({}, 1 if upscale.get("available") else 0)])
        archive = self.archive.health()
        if archive.get("enabled"):
            out.metric("harness_image_archive_last_reconciliation_timestamp_seconds", "gauge",
                       "Last image archive reconciliation.", [({}, archive.get("last_reconciliation", 0))])
            out.metric("harness_image_archive_images", "gauge", "Image archive jobs by state.",
                       [({"state": state}, archive.get(state, 0))
                        for state in ("archived", "missing", "errors", "retained")])
            out.metric("harness_image_archive_bytes", "gauge", "Verified bytes in the image archive.",
                       [({}, archive.get("bytes", 0))])
            out.metric("harness_image_archive_free_bytes", "gauge", "Free space on the image archive volume.",
                       [({}, archive.get("free_bytes", 0))])
            out.metric("harness_image_archive_free_space_warning", "gauge",
                       "1 when image archive free space is below its configured threshold.",
                       [({}, 1 if archive.get("free_space_warning") else 0)])
