# Jev Ultrafast study: indexed-action browser agent pattern

Status: qualitative document study for issue #171, written 2026-09-30. Research only. Nothing was run, nothing was
sent to TypeSafe, and no browser tool, dependency, credential or daemon egress rule is added. The owner makes the
final call; any adoption would be a separate implementation issue and a separate owner decision.

## Scope and recorded decisions

Owner decisions (2026-09-24, updated 2026-09-28): no browser-automation need is selected, so this is a design
reference; no targets, no baseline, no benchmark; public-source reading only; the owner decides adopt-pattern,
adopt-tool or ignore. #160 and #170 are separate open issues; this study does not depend on either, and does not
assume Bonsai 2 (#170). Findings on TypeSafe terms, retention and training belong on #160 and are only pointed at here.

## Sources

All read 2026-09-30. Repository text was read through a summarizing fetch tool, so quotes are as returned by it and
should be re-checked against the pages before being relied on.

- Repository README: <https://github.com/browser-use/jev-ultrafast> (raw `README.md`)
- Repository `LICENSE`: MIT, copyright Browser Use
- Vendor homepage: <https://typesafe.ai>
- Third-party pricing/limits articles surfaced by search (secondary, not relied on for claims below)

## What Jev Ultrafast does

- **Indexed state.** Each observation produces a fresh element table of interactive controls, one numbered row per
  control with type, label and current value, e.g. `[1] button  Change ticket type · Round trip`. The model sees
  this table plus visible text, not the full DOM and not screenshots in the default loop. Offscreen article bodies
  and footers are deliberately left out of context.
- **Action and target selection.** The model emits an operation (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL`, `WAIT`,
  `DONE`, `BLOCKED`) and a compatible element index in a single call. This is a closed set of typed choices, not
  free-form tool-call text.
- **Index stability.** Indices are only valid for the observation that produced them. The executor rechecks page
  freshness and click occlusion and validates the target (document, form values, target, nearby context) before
  acting, so an action chosen against a stale table is rejected rather than landing on a different control.
- **Speculative fan-out.** Each operation has its own target head, all evaluated in the same round trip; only the
  head matching the chosen operation executes (a `CLICK` uses only `click_target`). Two decisions, one network call.
- **Model/API split.** TypeSafe Jev (a hosted API, key `TYPESAFE_API_KEY`) picks operation and target. A separate
  small text LLM (the example uses an OpenRouter key and `inception/mercury-2.5`; other OpenAI-compatible models are
  said to work) produces text only when the operation is `TYPE_TEXT`.
- **Evidence the project reports.** One flagship task (Google Flights) at about 7.1 s; task time 9.45 s to 7.09 s and
  browser calls 1,092 to 101, with the project's own caveat: three repeats of one task on one browser profile, not a
  reliability benchmark. We make no performance claim from this and did not measure anything.

## Limitations (as stated by the project)

Shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling and arbitrary keyboard widgets are unsupported.
Coverage is common HTML and ARIA controls, not the full accessible-name computation. A `DONE` choice still requires
independent outcome verification.

## Licensing and access

- **Code license:** MIT, copyright Browser Use. This covers the repository, not the hosted Jev service.
- **Service access:** this is a different matter from the repository license. The repository README (read today)
  still says the Browser Use Cloud waitlist is open. The vendor homepage describes Jev as in "early access" with
  sign-in at console.typesafe.ai and does not state waitlist status there. The owner reported on 2026-09-28 that the
  waitlist is gone and Jev is open. **We could not verify that from a vendor page today**; the public pages we read
  are inconsistent or silent. Treat "open" as the owner's report, not an independently verified fact. Secondary
  articles dated mid/late September 2026 still describe waitlist gating and are not authoritative.
- **Terms, retention and training:** the homepage does not state them and points to the Terms of Use and Privacy
  Policy, which this study did not read. Secondary sources claim zero data retention when routed through an
  aggregator gateway; that is unverified and does not describe the direct API. These findings are to be shared on #160 (not posted by this run),
  not duplicated here.

## What can be borrowed independent of Jev

These are design ideas that need no Jev API and no third-party service:

1. **Indexed, compact observation.** Presenting a page as a numbered table of interactive controls with visible
   text only is a general technique usable with any model, including a local one.
2. **Closed-set actions.** A small typed operation set with a validated target index is easier to constrain,
   log and approve than arbitrary scripts, and maps naturally onto an approval policy.
3. **Freshness guard.** Binding each action to the observation it was chosen against and rejecting it if the page
   changed or the target is occluded.
4. **Separate text generation from action choice.** Only the step that must write text needs a generative model.
5. **Independent outcome verification.** Never accept the agent's own `DONE`; check the end state separately.
6. **Honest scoping.** The project's explicit unsupported list is a useful template for stating limits.

## What must not be adopted

- **Sending page content to a hosted model by default.** The index table carries labels, values and visible text,
  which can contain private data, form contents or credentials. Sending it to TypeSafe or an OpenRouter-hosted
  model is content egress and would need its own terms review, allowlist and owner approval. It conflicts with the
  recorded boundary for this issue.
- **Treating page text as instructions.** Page content is untrusted input. Prompt injection (a page telling the agent
  to click or type something) is a separate risk from egress. Typed operations and a freshness check narrow what an
  injected instruction can do, but they do not stop an injected page from steering the model to a legitimate-looking
  control. A closed action set is not a security boundary; approval and origin restrictions would still be needed.
  This study cannot establish runtime protections that do not exist in the harness.
- **Reusing the project's performance numbers.** Single task, three repeats, self-reported.
- **A new daemon dependency, API keys, or egress rule** on the strength of this reading alone.

## Qualitative assessment

Criteria, since the recorded decisions give authority and scope but not a bar: (a) does the pattern transfer to a
Harness workflow that is currently supported or selected; (b) is the benefit available without a third-party
service; (c) can it be bounded by the existing approval and egress model; (d) is the evidence beyond vendor
self-report.

- (a) No browser-automation workflow is selected, so there is nothing to transfer to yet. Agent Harness Web is a
  first-party static PWA whose checks are the existing `tests/web_*.mjs` scripts; nothing here argues for changing
  them.
- (b) Yes for the pattern ideas listed above; no for the hosted Jev model.
- (c) Unestablished. Content egress and injection handling would need design work first.
- (d) Weak. Evidence is a vendor demo, and nothing was independently measured.

## Recommendation

**Insufficient reason to adopt** the tool or the pattern now. Keep this document as a design reference. If a
browser-automation need is later selected, revisit the borrowable ideas (indexed observation, closed-set typed
actions, freshness guard, independent verification) with a local or already-approved model first. A live Jev
evaluation would need terms review and a separate owner decision. No implementation issue is filed: this document
alone does not authorize one, and the recorded decision for this issue is to stop at research. Final call is the
owner's.
