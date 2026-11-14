# Tasks

Read the assigned section: [Eva](#eva) · [Aadi](#aadi) · [Guillermo](#guillermo) · [Shared release](#shared-release).

All work is proposed. New filenames/tests are targets, not existing features. P0 is required; T15 is optional. Hours include task-level tests; T16 adds integration time. S/M/H describe relative complexity.

Unless stated otherwise, module/skill/static paths are under `sverka/`, and `test_*.py` files are under `tests/`. Repository-root and documentation paths are written explicitly.

| Milestone | Epic / feature | Tasks |
|---|---|---|
| M1: contracts and first real check | E1 platform / F1 shared data and storage | T01–T02 |
| M1–M2: evidence | E2 documents / F2 source access, F3 validation | T03–T07 |
| M1–M2: chat workflow | E3 agent/interface / F4 harness, F5 chat | T08–T09 |
| M2: complete baseline | E4 verification / F6 reconciliation and wiring | T10–T11 |
| M3: submission | E5 delivery / F7 packaging, F8 docs/demo | T12–T14,T16 |
| After core passes | E3 review / F9 interpretation revision | T15 |

Shared contracts/policies: [architecture](../architecture/README.md#contracts). Exact expected values and test IDs: [validation](05-validation.md). Start dependencies allow development with fixtures; additional closing dependencies require live components.

## Eva

Eva supplies ordinary Python preparation, inspection and validation functions. Guillermo handles model calls and the tool loop. Start with T03 and the first part of T07; publish one PDF and one spreadsheet example early.

### T03 — Versioned evidence

**1.5h · S · P0 · Start: T01 · R01–R02,R10**

Give every source fragment a stable ID and location so a result opens the correct document version.

**Reuse/files:** `models.py` fragments; new `sverka/evidence.py`, `tests/test_evidence.py`.

**Work:** adapt fragments to `PreparedDocument`/`EvidenceBlock`; retain version ID, text, extraction method, page/image IDs, dimensions and transforms. Use the store interface with an in-memory implementation until T02 is ready.

**Done:** PDF/PNG, XLSX and DOCX locators round-trip; equal filenames with different versions stay distinct; invalid boxes/block IDs fail. Tests pass without a model.

**Out/risk:** no parser rewrite or persistence implementation. Main risk: evidence points to a different version/coordinate space.

### T04 — Prepare the supplied formats

**3h · H · P0 · Start: T03 · R01–R02,R05,R08,R13**

Preserve what each document says before interpretation.

**Reuse/files:** `readers.py` signature/native readers; `ocr.py` orientation/boxes; `evidence.py`; new `test_preparation.py`, `test_formula_states.py`.

**Work:** implement `prepare_document` for PDF/PNG/DOCX/XLSX. Render/index scanned content even with a small native watermark. Keep raw formulas, cached values, derived results and errors separately. Support the corpus's simple SUM/arithmetic with Decimal; unsupported dependencies stay unresolved. DOCX needs paragraph/table quotes, not full-page rendering.

**Done:** V03–V04 pass, including uncached payroll SUM, unknown dependency, rotated/watermarked scan and corrupt file. Literal `1000` remains `1000`.

**Out/risk:** no general Excel engine, handwriting or legacy-format project. Do not call report-value repair or let preparation choose final categories. Silent data loss is the main risk.

### T05 — Inspect a page, region or cells

**2h · M · P0 · Start: T03 · R10,R13**

Let the agent and user inspect the source behind a claim.

**Reuse/files:** crop helpers in `render.py:37`; `ocr.py`, `evidence.py`; tests in `test_evidence.py`/`test_preparation.py`.

**Work:** implement `read_evidence`, `inspect_page`, `inspect_crop`, `reocr_region`, `read_cells`. Return artifact IDs through T02's store interface. Re-OCR adds evidence without erasing the old reading. Begin against a prepared fixture; all of T04 need not be finished.

**Done:** V03 source clicks show the right amount on ordinary/rotated pages; cells include formulas/input lineage; invalid/foreign IDs fail.

**Out/risk:** no PDF editor or shell/filesystem tool. Main risk: correct text with an incorrect highlight.

### T06 — Extraction instructions and validation

**3.5h · H · P0 · Start: T03 · R01–R03,R06–R13**

Turn proposed fields into accepted observations or explicit unresolved fields.

**Files:** new `sverka/extraction.py`, `sverka/skills/extraction.md`, `tests/test_extraction.py`.

**Work:** supply request builders/instructions for report and primaries. Save a header/indicator inventory and require one observation per entry. Validate types, transcription where comparable, evidence/version references, required fields and contradictions. Return reinspection requests and possible categories for unresolved documents. Only the validator assigns acceptance.

**Done:** V01/V05/V08 tests cover all 15 baseline numeric fields, missing fields, bogus evidence and conflicting amounts/periods. Amount/category/period may cite different passages. Functions work without the harness or a live model.

**Out/risk:** no second agent loop or financial reconciliation. Preserve observed values separately from inference; valid JSON alone does not establish correctness.

### T07 — Complete dataset and expected-result fixtures

**2h · M · P0 · Start: T01 · All correctness requirements**

Provide trustworthy inputs and expected outputs for parallel development.

**Reuse/files:** `examples/case19/`, `scripts/make_cases.py`, `tests/fixtures/`, `tests/test_cases.py`.

**Work:** first restore the two missing May spreadsheets from `../data/` relative to the repository root (`D:/hackaton/data`), then publish a manifest/small typed examples. This is the supplied dataset, separate from the app's `./data` runtime directory. Add baseline, missing-March and targeted regression fixtures. The answer workbook is test-only. Label synthetic examples separately from recorded live extractions.

**Done:** manifest contains 1 report/22 primaries plus context; generated scenarios build; expected values match V01/V02. Renamed/reordered/corrupt inputs are available for V03–V08.

**Out/risk:** no training or broad benchmark. Do not make Aadi wait for live extraction fixtures, and do not leak the answer workbook into agent input.

## Aadi

Aadi owns shared schemas and route declarations. Publish examples early so the other streams can use fakes. Build reconciliation independently of OCR/model calls; start Docker before final integration.

### T01 — Shared contracts and runnable skeleton

**1.5h · M · P0 · Start: none · R01–R16**

Publish v1 function inputs/outputs, data schemas, statuses, errors and evidence locators from the written architecture. Eva and Guillermo review asynchronously; no meeting is required.

**Files:** new `sverka/contracts.py`, `tests/fixtures/contracts/`, `tests/test_contracts.py`; existing `web.py`/`config.py`.

**Work:** implement [v1 contracts](../architecture/README.md#contracts), tool errors and statuses. Publish five examples: validated report field, primary, unresolved field, result/conclusion and evidence locator. Start `/health` and a static placeholder; define storage/evidence/provider interfaces.

**Done:** Eva/Guillermo load examples and call fake services. Tests distinguish proposed/accepted fields, decimal strings, null/zero and invalid locators.

**Out/risk:** no full storage/extraction/UI. Contract churn blocks consumers. Update schemas/examples in the same change; obtain explicit review from affected consumers before breaking their interface.

### T02 — Immutable storage and API

**3h · M · P0 · Start: T01 · R10,R13–R15**

Keep documents, conversations and results addressable across refreshes/reruns.

**Reuse/files:** FastAPI health/upload-limit patterns in `web.py`; new `storage.py`, `test_storage.py`, `test_api.py`.

**Work:** files/SQLite under `/data`; originals by IDs/hash, filenames as metadata. Implement [routes](../architecture/README.md#proposed-http-routes), initially with injected run/chat stubs. Resolve workspace-scoped artifact IDs; bound uploads/archives. Publish the small save/read interface early. Write results atomically; unfinished restarted runs become interrupted.

**Done:** V10 passes for same-name versions, old evidence, refresh/restart, foreign IDs, traversal and partial writes. One active check per workspace.

**Out/risk:** no public login, PostgreSQL, distributed queue or old-job migration. Initialize/version SQLite. Overwritten inputs and stale evidence are the main risks.

### T10 — Reconcile validated facts

**4h · H · P0 · Start: T01 · R03–R09,R12**

Calculate results independently from the report, preserving uncertainty.

**Reuse/files:** `money.py` and calculation/explanation ideas from `engine.py`; new `reconciliation.py`, `test_reconciliation.py`.

**Work:** pure `reconcile(scope, report, primaries, manifest)`, implementing [the policies](../architecture/README.md#reconciliation-policies). Check acceptance/coverage before equality. Handle duplicates/conflicting versions, periods, allocations, units/signs and Decimal rounding. Emit every indicator, contribution and structured conclusion; distinguish partial sums from complete expectations.

**Done:** V01/V02/V05–V07 pass on typed fixtures before live wiring. Incomplete totals retain known component discrepancies without asserting a complete total delta.

**Out/risk:** no parsing, model calls, invented proration or patching old orchestration. Main risk: matching numbers falsely appear verified.

### T11 — Connect the complete live run

**1.5h · M · P0 · Start: T02,T04,T05,T06,T08,T10 · R01–R13,R17**

Connect real components from committed handoffs; test the first real slice as soon as compatible functions are available, while tasks are still in progress.

**Files:** new `run_service.py`, `test_live_flow.py`; `web.py` and supported `cli.py` entry points.

**Work:** uploads → preparation → harness → validation → reconciliation → saved chat result. Persist stage counts/issues. Cancel safely and ignore late completion. A completed check with missing evidence differs from provider failure. Any documented batch command uses this service or is disabled.

**Done:** real baseline and V08/V10 pass; corrupt documents remain visible; missing credentials/timeouts have clear states. Mocked extraction does not close this task.

**Out/risk:** no compensating features. Send interface/state failures to the affected owner with a fixture, reproduction command and observed result; no joint session is required.

### T12 — Local Docker startup

**2h · M · P0 · Start: T01; close after T09,T11 · R14–R15**

Package the same application that the team demonstrates.

**Reuse/files:** `Dockerfile`, Compose, requirements/config; new `.env.example`, `.dockerignore` if needed.

**Work:** start a skeleton early; include static assets, OCR languages/rendering dependencies and pinned direct dependencies. Mount `/data`; document/configure provider/model/keys/budgets/timeouts. Keep service name `sverka` if practical. Default runtime needs no Ollama/GPU. Health/config errors must be clear.

**Done:** V11 passes on another teammate's clean Docker setup, including both use cases and restart persistence. Environment example has placeholders only.

**Out/risk:** no Kubernetes, hosted multiuser service or model hosting. Existing Docker files have not passed clean-build acceptance.

## Guillermo

Guillermo owns model requests/tool dispatch and the browser interaction. Eva's tools are ordinary functions; Aadi's reconciliation returns numbers to display. Use plain HTML/JS and fixture payloads before real integration.

### T08 — Provider adapter and bounded harness

**4h · H · P0 · Start: T01 · R01–R03,R10–R13,R17**

Implement the controlled model/tool loop.

**Files:** new `harness.py`, `model_adapter.py`, `skills/assistant.md`, `test_harness.py`.

**Work:** connect one configured provider; dispatch allowed tools, validation requests, progress and cancellation. Interpret the report and every primary through this path. Use finite per-document/run budgets; initial two reinspection rounds per document, with room for all 23 initial documents. Save outputs/redacted replay configuration. Follow-ups retrieve saved results.

**Done:** V08 tests cover calls, malformed output/arguments, invalid references, timeout, cancellation and exhaustion. Fake-provider tests plus one real connectivity/tool-use check; no model comparison.

**Out/risk:** no shell execution, self-installation, model hosting, general multi-agent framework or token streaming. Never silently bypass the agent with old rules.

### T09 — Chat results and evidence panel

**4h · M · P0 · Start: T01 · R10–R11,R17**

Show the result directly in the conversation.

**Reuse/files:** table/excerpt ideas in `render.py:275`; new `static/index.html`, `app.js`, `styles.css`. Coordinate route registration with Aadi.

**Work:** upload/select files, confirm scope, start/poll/cancel runs and restore conversation. Render typed result cards with statuses, values, partial labels, formula and exclusions. Evidence opens PDF/PNG regions, XLSX context or DOCX quotes plus original-file link. Follow-ups stay tied to the selected run.

**Done:** V09 passes first against fixtures, then live data. Null is never displayed as zero; numbers come from structured results; a click opens the correct version/fragment.

**Out/risk:** no frontend framework migration, editor, dashboard or export-first flow. Avoid hiding unsupported fields for a cleaner screen.

### T13 — README, architecture and defense

**2h · S · P0 · Start: T01; close after T09,T11,T12 · R15–R19**

Describe the submitted build accurately.

**Reuse/files:** existing README/defense structure; `README.md`, `docs/DEFENSE.md`, `docs/architecture/`.

**Work:** draft early; finalize tested launch/stop, requirements, configuration, dependencies and external-service list. Align diagram, two use cases and limits with implementation. Explain local harness/API calls, evidence and deterministic arithmetic. Replace offline-only/no-LLM and unmeasured performance claims; prepare a realistic roadmap.

**Done:** V12 covers criterion 6's 8/6/6 deployment/README/architecture allocation and demonstrable defense claims. Another teammate follows instructions without oral help.

**Out/risk:** no large slide deck/marketing site. Documentation drifting from the running build is the main risk.

### T14 — Demonstration and continuity

**1h · S · P0 · Start: T07; close after T09,T11,T12 · R17–R19**

Prepare a short repeatable demonstration.

**Files:** `docs/DEFENSE.md`, demo manifests, credential-free saved reference run/recording.

**Work:** baseline → overhead evidence/contributions → exclusions → missing-March case → follow-up question. Guillermo records a run; Eva reviews amounts/evidence and Aadi checks startup independently. Measure live duration and keep a clearly labeled recording/saved run for network continuity.

**Done:** V12: another teammate follows the script; both scenarios work; claims match the tested commit.

**Out/risk:** no features added during rehearsal. Replay must never masquerade as live processing.

### T15 — Confirm an interpretation (optional)

**1h · M · P1 · Start: T02,T06,T08,T09,T11 · R18**

Allow a confirmed category/service-period change with evidence and reason.

**Files:** optional `corrections.py`, UI controls, `test_revisions.py`; route registration with Aadi.

**Work/deliverable:** record before/after decisions and create a linked full rerun. Preserve original files/observations.

**Done:** V13 shows the old result still accessible and the new calculation using the recorded interpretation.

**Out/risk:** no arbitrary amount edits, incremental recalculation or general review application. Ship after core gates only; ordinary added-document reruns do not depend on this.

## Shared release

### T16 — Acceptance, fixes and submission

**12 team h: 4h each · H · P0 · Start: T07,T09,T11,T12,T13,T14; T15 only if enabled · All requirements**

These are separate checks, not a four-hour joint session. Record results against the release commit; rerun affected checks after subsequent changes. Guillermo collects the results and owns submission. A joint rehearsal is optional.

- Eva checks real evidence/amounts and extraction regressions.
- Aadi checks reconciliation/API/persistence and clean Docker launch.
- Guillermo checks browser follow-ups, aligns docs, rehearses and submits.

**Deliverable:** `docs/release-check.md` records commit, commands, passes/failures/skips, environment/provider without secrets, measured duration and limitations. Run [G1–G7](05-validation.md#release-gates); inspect actual fragments, not just nonempty IDs.

**Done:** core gates pass, submitted build matches the tested commit and receipt is confirmed by 22:00 Istanbul before the 23:59 cutoff.

**Out/risk:** no new features. Do not count skipped OCR/live tests or mocked baseline runs as release success. Fix blockers before optional work consumes the buffer.
