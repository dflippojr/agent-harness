# Context Language Models research backlog

Added: 2026-10-01 at the user's request. Status: concept to investigate.

Source: [Context Language Models](https://arxiv.org/abs/2609.37725), Shao et al., 2026-09-29. See the [full text](https://arxiv.org/html/2609.37725v1) and [authors' code](https://github.com/facebookresearch/context-language-models).

Research question: would agent-edited working context improve our current [masking and compaction](compaction.md), artifact recovery, saved state, and round reset?

First inspect the paper's live-context synchronization and map it to the local backend. Compare a prototype with the existing baseline on identical task/model snapshots and a forced restart. Measure success, lost constraints, recoverability, repeated errors, tokens, and elapsed time. Verify instruction boundaries, valid tool-call pairing, revision history, and rollback. Investigate hosted CLI support separately. Treat Suffix Cache Reuse as a serving-stack question with its own feasibility check.

The paper's reported compute reductions are unverified here and do not establish subscription quota or PC resource savings. This note proposes research and changes no runtime behavior.

The personal memory library holds the cross-project research capsule at `categories/project-ideas/capsules/context-language-models.md`.
