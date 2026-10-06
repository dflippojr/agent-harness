"""Skills lifecycle, session injection and advisory review scheduling."""
from __future__ import annotations
from pathlib import Path
from harness.modules import ModuleRuntime, HarnessError, Completion
from .service import SkillStore, SkillError, skill_instructions, SKILLS_TOOL_PROMPT
from .skill_review import SkillReviewer

class SkillsRuntime(ModuleRuntime):
    def init(self):
        if not self.effective():
            return
        cfg, m = self.cfg, self.manager
        reviewer = SkillReviewer(cfg.skills, m.db, idle=self.idle,
            model=cfg.models.get(cfg.default_model) if cfg.modules.local_model else None,
            chat=m.runner.chat,
            hosted_chat=self.hosted_review if cfg.skills.reviewer_base_url else None,
            local_review=cfg.skills.local_review and cfg.profile == "full" and cfg.modules.local_model)
        self.service = SkillStore(cfg.skills, m.db, cfg.data_dir, cfg.sandbox.image, reviewer=reviewer)

    def toolkit(self):
        return self.service

    def start(self):
        if self.service is not None:
            self.service.reconcile()
            if self.service.reviewer is not None:
                self.service.reviewer.start()

    async def stop(self):
        if self.service is not None and self.service.reviewer is not None:
            await self.service.reviewer.stop()

    def add_skills(self, system, project, skills, session_meta, missing):
        try:
            frozen = self.service.resolve_for_session(project, skills, session_meta, missing=missing)
        except SkillError as e:
            raise HarnessError(e.status, str(e)) from e
        if frozen:
            system += "\n\n" + skill_instructions(frozen)
        if self.service.can_propose(session_meta):
            system += "\n\n" + SKILLS_TOOL_PROMPT
        return system, frozen

    def idle(self) -> bool:
        """True only when the GPU scheduler, inference gate, modules' GPU work (images), and GPU guard are all idle."""
        sch = self.manager.scheduler
        if sch.holder or sch.paused or sch._waiters:
            return False
        if self.manager.runner.generating or self.manager.runner.gate.busy or self.manager.runner.gate.exclusive:
            return False
        if self.manager.modules.busy():
            return False
        if self.manager.guard is not None and (self.manager.guard.active or self.manager.guard.manual):
            return False
        return True

    async def hosted_review(self, messages: list[dict]):
        """Owner-triggered hosted review only. Never used for silent background Qwen work."""
        import httpx
        cfg = self.cfg.skills
        headers = {}
        if cfg.reviewer_api_key_file:
            key_path = Path(cfg.reviewer_api_key_file)
            headers["Authorization"] = "Bearer " + key_path.read_text(encoding="utf-8").strip()
        url = cfg.reviewer_base_url.rstrip("/") + "/v1/chat/completions"
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, json={"model": cfg.reviewer_model, "messages": messages, "max_tokens": 1200},
                                     headers=headers)
            resp.raise_for_status()
            content = (((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return Completion(content=content)

    def metrics(self, out, db):
        with db.lock:
            skill_proposals = dict(db.conn.execute("SELECT status, COUNT(*) FROM skill_proposals GROUP BY status").fetchall())
            skill_installed = db.conn.execute("SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM skill_installed").fetchone()
            skill_reviews = dict(db.conn.execute("SELECT status, COUNT(*) FROM skill_review_jobs GROUP BY status").fetchall())
        out.metric("harness_skill_proposals", "gauge", "Skill proposals by status.",
                   [({"status": st}, n) for st, n in skill_proposals.items()])
        out.metric("harness_skills_installed", "gauge", "Installed instruction skills.",
                   [({"enabled": "true"}, skill_installed[1] or 0), ({"enabled": "false"}, (skill_installed[0] or 0) - (skill_installed[1] or 0))])
        out.metric("harness_skill_reviews", "gauge", "Advisory skill reviews by status.",
                   [({"status": st}, n) for st, n in skill_reviews.items()])
