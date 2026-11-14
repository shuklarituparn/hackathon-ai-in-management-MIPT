# Aadi's tasks

Working index for Aadi (also written AADI or aadi; GitHub `shuklarituparn`). The [backlog](03-backlog.md#aadi), [architecture contracts](../architecture/README.md#contracts) and [validation](05-validation.md) are authoritative; if this page disagrees with them, they win. Keep the status table current when a task changes state.

**Deadline:** 14 Nov 2026, submit by 22:00 Istanbul (hard cutoff 23:59). Checkpoints: [team plan](README.md#checkpoints).

## Status

As of 14 Nov 2026.

| Task | Issue | Est. | Status | Next step |
|---|---|---:|---|---|
| T01 Contracts + skeleton | [#1](https://github.com/shuklarituparn/hackathon-mipt/issues/1) | 1.5h | Merged to `main` via [PR #19](https://github.com/shuklarituparn/hackathon-mipt/pull/19), with Guillermo's fix `1c340b5` (stricter grand-total checks, detached fake-store snapshots, atomic block batches); issue #1 closed | Done |
| T02 Storage + API | [#2](https://github.com/shuklarituparn/hackathon-mipt/issues/2) | 3h | Merged to `main` via [PR #20](https://github.com/shuklarituparn/hackathon-mipt/pull/20); issue #2 closed | Done; Eva uses `SqliteStore`/`fakes.InMemoryStore` |
| T12 Docker | [#12](https://github.com/shuklarituparn/hackathon-mipt/issues/12) | 2h | PR #33 adds the environment example, Compose limits and `OMP_THREAD_LIMIT=1`. The review fix includes packaging metadata and pytest configuration in the image; CI runs the packaging tests inside it. Docker startup, uploads and persistence after container recreation passed; see [release checks](../release-check.md) | V11 still needs both use cases through the UI with a real key and a restart. Keep #12 open until that passes |
| T10 Reconciliation | [#10](https://github.com/shuklarituparn/hackathon-mipt/issues/10) | 4h | Merged via [PR #23](https://github.com/shuklarituparn/hackathon-mipt/pull/23) at `71fa55b`; issue #10 closed. V01/V02/V05–V07 pass on typed fixtures. Follow-up [PR #25](https://github.com/shuklarituparn/hackathon-mipt/pull/25) (unitless report columns, unit/contract wording) merged at `461d665` | Done; Eva (T06) and Guillermo (T08) provide indicator `category`/`components` and copy units only as printed |
| T11 Live run | [#11](https://github.com/shuklarituparn/hackathon-mipt/issues/11) | 1.5h | Run service, API, `/health`, CLI and Compose merged to `main` via [PR #31](https://github.com/shuklarituparn/hackathon-mipt/pull/31). The live baseline (V01) passes with the real provider (DeepSeek `deepseek-flash`) after the instruction change in `skills/extraction.md` version 2; recorded in [release-check.md](../release-check.md) | Close #11 when the instruction change is merged. Remaining live checks (V02, V08–V10 with the UI) belong to T16 |
| T04 Preparation (Eva's, backup) | [#4](https://github.com/shuklarituparn/hackathon-mipt/issues/4) | 3h | Merged to `main` via [PR #27](https://github.com/shuklarituparn/hackathon-mipt/pull/27) (`Closes #4`), with a review fix: binary float noise in a cached formula result is no longer reported as a mismatch. V03/V04 tests pass with and without Tesseract | Done |
| T05 Inspection (Eva's, backup) | [#5](https://github.com/shuklarituparn/hackathon-mipt/issues/5) | 2h | Merged to `main` via [PR #28](https://github.com/shuklarituparn/hackathon-mipt/pull/28) (`Closes #5`), with a review fix: re-OCR of a region that overlaps an earlier one returns the stored reading instead of failing. The T08 harness extracts through the real tools | Done; T06 plugs its validator into `DocumentTools` |
| T06 Validation (Eva's, backup) | [#6](https://github.com/shuklarituparn/hackathon-mipt/issues/6) | 3.5h | Merged to `main` via [PR #30](https://github.com/shuklarituparn/hackathon-mipt/pull/30) (`Closes #6`): `sverka/extraction.py`, `sverka/skills/extraction.md`. An independent review found 15 defects in the first version; all fixed with tests. Exercised with a mechanical reader only, not with a real model | Done; watch how often a real model needs a second round |
| T16 Release checks | [#16](https://github.com/shuklarituparn/hackathon-mipt/issues/16) | 4h | Later | Reconciliation, API, persistence, clean Docker; record in `docs/release-check.md` |

Suggested order: T01 → T12 skeleton → T02 → T10 → T11 → finish T12 → T16.

## T01: shared contracts and runnable skeleton

Done means Eva and Guillermo load the examples and call fake services; tests distinguish proposed/accepted, decimal strings, null/zero and invalid locators.

What exists now:

| File | Content |
|---|---|
| `sverka/contracts.py` | Pydantic v1 models for every contract row, `ToolResult`/`ToolError`/`ToolFailure`, statuses, typed locators, policy invariants, `json_schema()`. `python -m sverka.contracts` prints the schema |
| `sverka/interfaces.py` | `DocumentStore`, `RunStore`, `EvidenceTools`, `ModelProvider` (placeholder for T08), `Reconcile` protocols |
| `sverka/fakes.py` | `InMemoryStore` (both stores), `load_example(name)`, `fake_reconcile` |
| `tests/fixtures/contracts/` | Five examples, `schema.json`, README (real vs synthetic) |
| `tests/test_contracts.py` | 81 tests: examples canonical, schema current, decimals, null/zero, locators, invariants, fakes, `/health` |
| `sverka/web.py` | `/health` adds `contracts`, `llm_configured`, `data_dir_writable` and no longer returns the LLM URL; `/app` serves `sverka/static/index.html` placeholder; legacy `/` unchanged |
| `requirements.txt` | Pins `pydantic==2.14.3` (now imported directly) |

Design decisions for reviewers to confirm:

1. **Allocation lines.** A primary's amount is split into `AllocationLine`s. A line that covers several finer categories (payroll code 0202, "additional payroll and leave reserve") is accepted at the parent category with `possible_categories`; this is why columns 8 and 9 are insufficient evidence.
2. **Issue-date rule.** With no explicit service period, the example infers `[issue_date, issue_date]` with `basis="inferred"`. Final rule is a T10 policy decision.
3. **Proposals** carry canonical strings (`"1145798.10"`, `"2026-01-31"`, `"2026-01-01/2026-01-31"`) plus the exact `source_text`; the validator parses them.
4. **Categories** are an open snake_case vocabulary (`KNOWN_CATEGORIES` lists the supplied form's), so other report layouts work.
5. **Chat cards** embed `IndicatorResult`/`Conclusion` rather than referencing them (Guillermo to confirm).
6. **Indices.** PDF pages are 1-based; DOCX paragraph/table indices are 0-based (python-docx order).
7. **IDs** are typed by prefix (`ws_`, `doc_`, `ver_`, `blk_`, `art_`, `run_`, `msg_`); use `contracts.new_id`.

Not verified: Docker build/health (no daemon in the session that wrote T01); CI runs it on the PR.

## T02: immutable storage and API

Done means V10 passes (same-name versions, old evidence, refresh/restart, foreign IDs, traversal, partial writes) and one active check per workspace.

What exists now (branch `aadi/t02-storage`, on top of T01):

| File | Content |
|---|---|
| `sverka/storage.py` | `SqliteStore(data_dir)`: `sverka.db` (schema version in `PRAGMA user_version`) + content-addressed `blobs/`. Implements `DocumentStore`/`RunStore`; rollback journal (not WAL) for Docker Desktop bind mounts |
| `sverka/api.py` | Routes from the [architecture table](../architecture/README.md#proposed-http-routes) plus `GET .../documents` and `GET .../runs`; `install(app, services)`; injected `RunLauncher`/`ChatResponder` with stubs; upload/zip limits; artifact headers |
| `sverka/web.py` | `api.install(app)`; `/health` adds `run_mode`. Legacy `/check`, `/demo`, `/api/check` untouched (T11 decides) |
| `sverka/config.py`, `docker-compose.yml` | `SVERKA_MAX_ARCHIVE_FILES` (500), `SVERKA_MAX_ARCHIVE_MB` (300), `SVERKA_RUN_MODE` (`disabled`/`fixture`) |
| `sverka/interfaces.py`, `sverka/fakes.py` | Added `get_workspace`, `list_runs`; fake now matches the real store (insertion order, filename cleaning, fixed run inputs) and returns detached copies |
| `tests/test_storage.py`, `tests/test_api.py` | 54 tests; shared store behaviour runs against both SQLite and the fake |
| `tests/test_fake_store_regressions.py` | Guillermo's snapshot and atomic-batch checks, now run against both stores |

Behaviour to know:

- Filenames are metadata (last path component, NFC); bytes live under their SHA-256, so equal names never overwrite and identical bytes share one blob. Reads verify the hash: a damaged file is `unreadable`, never wrong bytes.
- Every ID is resolved inside its workspace; a foreign or malformed ID is 404.
- Blocks, versions and artifacts are immutable; re-saving a block with other content is rejected. Evidence of an old version keeps resolving after a replacement upload.
- `save_run` refuses to touch a finished run or change its inputs/scope, so a late completion after cancel is rejected. Cancel saves `cancelled` first, then notifies the launcher.
- On start, queued/running runs become `interrupted` with an `interrupted_by_restart` issue. Only the process holding `sverka.lock` in the data directory does this, so a CLI command or second worker never interrupts a live check.
- A `page_region` block must point at a `page_render` or `crop` of its own version, never the original file.
- Zip: cp866 names decoded, `__MACOSX`/dotfiles/`~$` skipped, unpacked size counted while reading (bomb-safe), encrypted members refused; a rejected upload stores nothing.
- Artifacts: PDF/PNG/JPEG/text inline, everything else downloads as `application/octet-stream`; `nosniff`, sandbox CSP (not on PDFs), immutable caching.
- If the chat responder fails, the user's message is kept and a `system` message says the reply failed.

Handoffs: Eva can use `SqliteStore` or `InMemoryStore` for artifact/block IDs in T03/T05. Guillermo can run `SVERKA_RUN_MODE=fixture` to build T09 against real routes and typed cards; evidence clicks return 404 there, because the example's block IDs are not seeded (seeding would point evidence at the user's own uploads). T11 replaces `DisabledRunLauncher`/`SavedResultResponder` via `api.Services`.

## T10: reconcile validated facts

- Pure `reconcile(scope, report, primaries, manifest) -> ReconciliationResult` in `sverka/reconciliation.py`; no OCR/model calls. Implement the nine [policies](../architecture/README.md#reconciliation-policies).
- Reuse `money.py` (`q()` rounds half-up). Do not copy `engine.Reconciler`: `_missing_months` skips coverage with fewer than two monthly documents (D05), and a zero report value counts as verified without checking unresolved documents.
- Oracle: overhead `245000.00 + 318298.10 + 270000.00 + 300000.00 = 1133298.10` vs reported `1145798.10` → +12,500; total +12,500 explained by it (not 25,000); 5 verified (materials 0 in closed scope), 2 discrepancies, 8 insufficient. Missing March: payroll `observed_sum 4456674.83` partial, payroll/total `expected_value` and `diff` null, overhead +12,500 kept.
- Sources: production overhead = Акт 118, Счет-акт 174, СЗ КЛ-31, УПД 0426-77; administrative = ИА-15, ADM-27, Справка ИТ 03-26, СВ-0426, УПР-55. Exclusions: ADM-27 copy (identical bytes), ОРГ-0603 (June), ADM-75 (contract 24/03/16-02).
- Regressions: typed 350 never 550; 1000 never 10; unaccepted category not counted; unknown ≠ zero; duplicates identity-aware.
- Done: V01, V02, V05–V07 on typed fixtures.

What exists now (branch `aadi/t10-reconcile`, from `main`; independent of T02):

| File | Content |
|---|---|
| `sverka/reconciliation.py` | `reconcile(scope, report, primaries, manifest, *, tolerance=None, monthly_roots=MONTHLY_ROOTS)`; pure, no I/O |
| `tests/recon_fixtures.py` | Typed case19 bundle: 1 report, 22 primaries, README as context; builders for variants. May payroll/contributions are derived from oracle totals until T07; they equal the restored May files in PR #24 |
| `tests/test_reconciliation.py` | Baseline indicators vs the oracle, missing March, V05–V07, D01–D06, dependent discrepancies, purity, fixture freshness |
| `tests/test_reconciliation_*_regressions.py` | Currency/report-unit conversion, unknown allocations, conflict-group ordering and nested subtotals |
| `tests/fixtures/reconciliation/` | `baseline_result.json`, `missing_march_result.json` for T09 cards; regenerate with `python -m tests.recon_fixtures` |

How it decides (details in the module docstring):

- **Hierarchy from the report.** An indicator's `components` make their categories children of its category. A line counts for every indicator whose subtree contains its category.
- **Unsplit lines.** A line accepted at a parent category with `possible_categories` (payroll 0202 = additional + reserve; contributions 0300 without split) counts for the parent and blocks the finer indicators it might belong to. This is why columns 6, 8, 9, 11–14 are insufficient while 5, 7 and 10 verify.
- **Subtotals.** Process finer categories before their parents so nested ИТОГО rows are not counted twice.
- **Blockers.** Unresolved documents and lines block every category they could belong to; an allocation with unknown relevance keeps the whole document's relevance open. An unreadable document therefore keeps materials at "insufficient", not "verified zero".
- **Monthly roots.** `payroll_total` and `contributions_total` need each scope month covered by a full-month statement; two statements for one month conflict.
- **Duplicates.** Same bytes → duplicate. Same number, contract, type and counterparty: same date and amount → duplicate; a differing date or amount blocks the whole connected identity group before copies are excluded. Separate stated periods remain distinct. A superseded version (`predecessor_version_id`) is excluded.
- **Units.** Unsupported or contradictory currencies/units stay unresolved. Monetary result amounts, differences, tolerance and conclusions use RUB. The source `ReportExtraction` stays unchanged; scaled report values carry their original number/unit in the explanation and a `report_unit_normalized` finding. T09 should display each result's `unit`. Supported wording includes "в рублях", "руб., с НДС", "тысяч рублей" and "RUB (₽)". Only recognized VAT annotations are ignored; unknown suffixes, parenthesized scales and divisors stay unresolved. A money column that prints no unit (the supplied report prints none) is compared in rubles with a `report_unit_assumed` gap.
- **Contracts.** A reference matches when it names the whole scope contract number, allowing spaces inside it ("№ 24 / 03 / 15 - 01 от 09.04.2024"). A number inside a longer identifier ("24/03/15-01-доп") stays unresolved rather than excluded. Duplicate candidates match their references to the same scope contract; differing surrounding wording does not create separate identities.
- **Issue-date rule.** With no service period, the period is the issue date and the explanation says so.
- **Discrepancy category.** If the difference equals some excluded documents, it is `duplicate`/`wrong_period`/`wrong_contract`; if negative and equal to counted documents, `omitted_document`; else `amount_mismatch`.
- **Conclusion.** A discrepancy fully explained by more specific ones is not listed again; the grand total names the discrepancies it contains and any unexplained remainder.

Open points: the issue-date rule (single-day period) is my v1 choice; T06's validator may instead infer periods. Report arithmetic findings go into the indicator's `gaps` because the contract has no separate findings field.

## T11: connect the complete live run

- `sverka/run_service.py`, `tests/test_live_flow.py` (`@pytest.mark.live`). Uploads → preparation → harness → validation → reconciliation → saved chat result; persist stage progress.
- Safe cancel, ignore late completion (the fake store already refuses saves after a terminal state). Completed-with-missing-evidence ≠ provider failure; missing credentials/timeouts get clear `RunFailure` codes; corrupt documents stay visible.
- `cli.py check` must use this service or be disabled. Mocked extraction does not close the task.

What exists now (merged via PR #31, on top of T06):

| File | Content |
|---|---|
| `sverka/run_service.py` | `RunService`: the API's launcher. Background thread per check, `execute` for synchronous use, cancel token, per-run `ExtractionValidator`, stage counters saved while it works, JSONL trace under `<data>/traces/`, one atomic save of the completed run, chat message with the conclusion card. Failure codes: `missing_credentials`, `provider_error`, `timeout`, `budget_exhausted`, `invalid_input` (also a report without indicators), `internal_error` |
| `sverka/api.py`, `sverka/config.py` | `SVERKA_RUN_MODE=live` is the default: `RunService` plus `FollowUpResponder` with `ExtractionInstructions`. `disabled` and `fixture` are unchanged |
| `sverka/web.py` | `/health` adds `provider` (`configured`, `name`, `model`, names of missing settings; no values). Legacy `/`, `/check`, `/demo`, `/api/check` are untouched until T09 replaces the page |
| `sverka/cli.py` | `check` runs the same service; contract, row and period are required arguments; writes `run.json`. Archives are read with the upload route's reader; any error is exit code 2. The legacy rule-based command-line path is removed |
| `sverka/document_tools.py` | `with_validator(validator)`: per-check tools sharing the preparation cache |
| `docker-compose.yml` | Forwards `SVERKA_PROVIDER_*`; run mode defaults to `live` |
| `tests/test_live_flow.py`, `tests/case19_reader.py` | The whole flow with real storage, tools, harness, validator and reconciliation; the model is a mechanical reader that submits what the files print. Baseline (V01), missing March (V02) and corrupt inputs on the supplied scanned bundle (marked `ocr`); cancel with a late answer, missing credentials, timeout, provider error, unread document, restart. One `live` test runs the baseline with the configured provider |

The `live` test was run with DeepSeek on 14 Nov 2026 and passes after the instruction change (`skills/extraction.md` version 2); see [release-check.md](../release-check.md) for the runs and what the first one got wrong. To repeat it:

```bash
python -m pytest -q -m live tests/test_live_flow.py
```

## T04/T05: preparation and inspection (backup for Eva)

Implemented on `eva/t04-prepare-formats` (T04) and `eva/t05-inspect-evidence` (T05, on top of T04), as Aadi's standing duty to take Eva's work if she falls behind. Both are merged to `main`.

| File | Content |
|---|---|
| `sverka/preparation.py` | `prepare_version(store, ws, version_id)`: page renders and evidence blocks saved to the store. Native PDF text at `SVERKA_RENDER_DPI` (150); scans OCR'd at `SVERKA_OCR_DPI` (300) or more, upright, with `transform.rotation`; a large image with at most 40 native words (watermark, stamp) is OCR'd too and its native words stay as `native_text` on the same render. PNG/JPEG/TIFF frames OCR'd; DOCX quotes plus a warning for text the quotes cannot address (text boxes, nested tables, headers/footers); XLSX cells. Damaged/empty files are `unreadable`; DOC/XLS/zip/unknown are `unsupported` |
| `sverka/formulas.py` | Decimal evaluation: numbers, refs (also other sheets), ranges, `+ - * / ^ %`, `SUM`/`СУММ`, `ROUND`/`ОКРУГЛ` (half away from zero), `ABS`, `MIN`, `MAX`. Other workbooks, missing sheets, names, other functions, text/comparisons, dates, cycles and directly referenced empty cells stay `unresolved`; `#DIV/0!` and error inputs are `error`. A derived value is padded to the cell's format scale, never rounded |
| `sverka/document_tools.py` | `DocumentTools(store, validator=None)` implements `EvidenceTools` (T04: `prepare_document`, `validate_extraction`; T05: the inspection tools). Preparations are kept in a bounded per-process cache (128; retryable failures such as missing OCR are not cached); crops are unscaled `crop` artifacts; re-OCR adds `reocr` blocks on the full page render with `derived_from`; render PNGs carry page/transform/crop origin, so crops resolve after a restart. `validate_extraction` is `unsupported` until a T06 validator is passed |
| `sverka/evidence.py` | `spreadsheet_evidence` (derived/error states, cached-vs-derived warnings), rotation for native boxes on turned renders, `render_png`/`read_render_info` |
| `sverka/ocr.py` | `ocr_page_oriented` returns the applied turn; orientation is now always checked and kept only when OCR confidence improves (a watermark had masked a sideways scan) |

Known limits: rotated native PDFs (text layer on a `/Rotate` page) keep T03's word-level lines on the displayed page; docx text boxes are reported, not addressable; preparation is re-run (new render IDs) after a restart, while old block IDs keep resolving.

## T12: local Docker startup

- Add `.env.example` (placeholders only: provider, model, key, budgets, timeouts). Default runtime needs no Ollama/GPU; Compose still centers `SVERKA_LLM_*` and an unpinned `ollama/ollama:latest`.
- Once T08 lands, `/health` should report the `SVERKA_PROVIDER_*` configuration (without secrets) instead of the legacy `SVERKA_LLM_URL` (from the PR #20 review).
- Keep service `sverka` and the `/data` mount; include static assets and OCR languages.
- Done: V11 on a teammate's clean machine: `docker compose build --no-cache`, both use cases, stop/start keeps `/data`.

## Standing duties

- Schema owner and integrator: coordinate `web.py`/contract edits; register Guillermo's static/chat routes (and T15's if built).
- Assign each blocking defect to its owner with a reproduction.
- Early: confirm the demo machine and which teammate runs the clean Docker check.
- Backup: take Eva's fixture/formula tests if she falls behind; help artifact routes if UI slips.

## Workflow

- Branch `aadi/t0N-short-name` from `main`; PR title `T0N: ...`; assignee `shuklarituparn`; body `Closes #N` (or `Refs #N` for a draft). Issue numbers currently match task numbers (T02 = #2); check the issue before linking.
- Checks (Python 3.13):

```bash
python -m ruff check .
python -m pytest -q --ignore=tests/test_cases.py -m "not live and not ocr"
git diff --check
```

- Format only files you create or substantially rewrite (`python -m ruff format <file>`); do not reformat legacy modules in a feature PR.
