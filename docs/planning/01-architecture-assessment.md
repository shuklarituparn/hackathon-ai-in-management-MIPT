# Current code and reuse

Reference for implementation. Target contracts/policies live in [architecture](../architecture/README.md); assignments are in [tasks](03-backlog.md).

## What exists

Reviewed commit `27ada2d`. Current flow: upload form/CLI → `engine.run_check()` → native readers/Tesseract → regex requisites/report parser → code/keyword classification → deduplication/filtering → Decimal aggregation → static HTML/JSON/XLSX and crops. The optional LLM only chooses an ambiguous document's category.

There is no chat, tool-calling harness, versioned evidence store or supported correction workflow. `.env.example` and CI workflows are absent. The bundled example lacks May payroll/contributions.

Focused probes verified native spreadsheet extraction and the six failures below. Full OCR/end-to-end tests and clean Docker startup remain unverified: the audit machine lacked dependencies/Tesseract and its Docker engine was stopped. A saved HTML report is not evidence that this snapshot works.

## Reuse decisions

| Code | Reuse/change | Task |
|---|---|---|
| [`models.py:11`](../../sverka/models.py#L11), fragments/evidence | Extend source locations with version/block IDs | T03 |
| [`readers.py:27`](../../sverka/readers.py#L27), `read_any` | Keep signature/native readers/read errors; fix formula states | T04 |
| [`ocr.py:67`](../../sverka/ocr.py#L67), `ocr_lines` | Keep orientation/text/boxes behind preparation/inspection | T04–T05 |
| [`render.py:37`](../../sverka/render.py#L37) | Reuse crop/highlight helpers with artifact IDs | T05 |
| [`money.py:18`](../../sverka/money.py#L18) | Reuse Decimal/normalization helpers with tests | T10 |
| [`engine.py:314`](../../sverka/engine.py#L314) | Reuse arithmetic/explanation ideas; new reconciliation takes validated facts | T10 |
| [`web.py:17`](../../sverka/web.py#L17) | Keep FastAPI/health/upload limits; replace basename storage/batch coupling | T02/T11 |
| Tests and `scripts/make_cases.py` | Reuse scenarios; fix inputs/assertions | T07 |
| Dockerfile/Compose | Adapt configuration/persistence; verify clean launch | T12 |
| [`render.py:133`](../../sverka/render.py#L133) onward | Optional export adapter after core chat works | Optional |

Keep `report.py`, `classify.py`, regex-authoritative `requisites.py` and old `run_check()` outside the new primary path. Do not repair unused legacy modules merely because they exist. Supported CLI commands must use the new service or be disabled.

## Confirmed defects

| ID | Reproduction/source | Required protection |
|---|---|---|
| D01 | `report.py:645–669` changed reliable typed 350 to 550 | Preserve observations; arithmetic inconsistency is a finding |
| D02 | `report.py:351–365` read `1000` as `10.00` | Preserve literal representation |
| D03 | `readers.py:236–287`: unsupported dependency became zero; Python rounding used | Preserve formula states; propagate unresolved dependencies |
| D04 | `classify.py:92–103`, `engine.py:256`: low-confidence category counted | Only accepted allocation enters confirmed aggregation |
| D05 | `engine.py:375–439`: equality bypassed reliability/month coverage | Check acceptance/coverage first |
| D06 | `engine.py:164–169,673–675`: cross-contract lookalike suppressed valid document | Exact hashes plus identity-aware duplicates |

Other fixes: basename uploads overwrite files (`web.py:61–72`); job IDs are not public authentication (`web.py:130–154`); UI/export/defense text claims no external calls. T02/T12 handle storage/deployment; T13 updates claims.

No runtime answer-key lookup or pasted expected totals were found. The main limitations are specialized layout/vocabulary and permissive decisions.
