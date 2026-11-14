# Release check

Results of the checks that need the real environment. T16 extends this file with the release gates; this first section records the T11 live run.

## T11: baseline with the real model (14 Nov 2026)

**What was run.** The supplied 23-document bundle (one scanned report, 22 primary documents, context files) through the whole application: preparation with OCR, the model loop with tools, validation, reconciliation. The test compares the result with the V01 baseline: statuses of all 15 indicators, the observed sums of the checked ones and the three exclusions.

```bash
python -m pytest -q -m live tests/test_live_flow.py
```

**Environment.** Linux container, Python 3.13, Tesseract 5.3.4 (`rus`, `eng`). Provider: DeepSeek, model `deepseek-flash`, thinking enabled, temperature omitted, `max_tokens`. No key is stored in the repository.

| Run | Code | Result | Time | Requests | Tokens in / out |
|---|---|---|---|---|---|
| 1 | `main` at `a3130c3` | Failed: 3 verified, 0 discrepancies, 12 insufficient (expected 5, 2, 8); exclusions correct | 7m21s | 72 | 883k / 84k |
| 2 | Instructions: one row per band, early proposal, overhead rule | Passed | 6m18s | 50 | 620k / 77k |
| 3 | Same as run 2 | Failed: 4 verified, 2 discrepancies, 9 insufficient; the report used all 12 requests before its first proposal and one value stayed unread | 6m19s | 53 | 599k / 77k |
| 4 | This commit: run 2 plus 20 requests per document, row sections, one reading per header | Passed | 7m50s | 57 | 904k / 100k |
| 5 | Same as run 4 | Passed | 6m58s | 57 | 812k / 88k |

**What the first run got wrong, and what changed.**

- The model re-read three report rows in one OCR band, which garbled two cells and dropped the `0,00` in column 4. The instructions now say to re-read one row at a time and, if cells are still wrong, in two or three sections.
- On the report the model used all 12 requests before its first proposal, so no re-inspection round was left. The instructions now ask for an early proposal, and a check gives one document 20 requests and 40 tool calls (`run_service.check_limits`; the run totals are unchanged).
- The model left act СВ-0426 (communications for the administrative group, no cost code printed) without a category, which made columns 15–17 partial. The instructions now say how to choose between general and production overhead from the description.

No expected value was given to the model. The changes are in `sverka/skills/extraction.md` (version 2) and `sverka/run_service.py`.

**Limitations.**

- The model is not deterministic. The final settings passed twice in two runs; that is not a guarantee for every run. A run that misses shows the affected indicator as insufficient evidence, not as a wrong figure: in runs 1 and 3 no indicator was reported verified or discrepant against the baseline.
- Two report header fields stay unresolved in every run and do not affect the result: the customer name (OCR reads `000 "CTI"` for `ООО "СТЦ"`) and the stage period (printed as two dated lines with the day in quotes).
- Only the baseline was run live. Missing March (V02), the changed-file cases and the chat UI were not run with the real model.
- Do not run two OCR-heavy checks at once on a small machine: two parallel runs stalled in Tesseract for over ten minutes on 4 cores.

## T12: Docker packaging review (14 Nov 2026)

Source: the review fix for [PR #33](https://github.com/shuklarituparn/hackathon-mipt/pull/33), based on
`d73db70`. Tested image ID: `sha256:ed37af166953441af44d176e10990779988c347c3d8af26228b8b38ddc47a3e9`.
Environment: Windows Docker Desktop, Docker 29.1.3, Compose 2.40.3, Linux container with Python 3.13.

The image now includes the placeholder environment example, Compose file, Dockerfile and pytest
configuration. CI runs the four packaging tests inside the image. A health test now isolates its
provider settings so the offline suite also works when Compose loads a real `.env`.

| Check | Result |
|---|---|
| `docker compose build sverka` | Passed |
| Full offline suite inside Compose (`--ignore=tests/test_cases.py -m 'not live and not ocr'`) | 770 passed, 1 skipped (optional browser suite), 23 deselected; 79.48s |
| `docker compose run --rm --no-deps --entrypoint python sverka -m pytest -q -m live tests/test_provider_live.py` | 2 passed; 26.64s |
| Ruff and `git diff --check` | Passed |
| `/app`, static assets, OCR `rus`/`eng`, thread limit, provider configuration and writable data | Passed |
| Upload and saved missing-key failure; recreate container with the same data mount | Original bytes, document versions, run and chat message preserved |
| Image secret check | `/app/.env` absent; placeholder `.env.example` present |

The two provider tests prove connectivity, tool use and image input on a small fixture. They do not
replace both full use cases through the chat UI. V11 remains open for those manual checks and a
restart with the completed results. No full financial check was run during this packaging review.

## T09: highlighted evidence tabs and readable follow-ups (14 Nov 2026)

Source: `guillermo/t09-evidence-chat-polish`, based on `d88dccc`. The new evidence tab uses the existing
version-scoped API. No financial calculation, shared API schema or saved result was changed.

| Check | Result |
|---|---|
| Full offline suite inside Docker | 813 passed, 1 skipped (optional browser suite), 23 deselected; 110.85s |
| Browser and static suite | 29 passed; 160.87s |
| Final real-provider probes on the existing completed run | Discrepancy explanation, unrelated programming redirect and incomplete-evidence explanation all passed without repair |
| Real uploaded PDF in the separate viewer | Highlight geometry, initial centering, excerpt, source identity and readable page checked in Chrome |
| Ruff and `git diff --check` | Passed |
| Independent review | Two filename regressions found, fixed and independently rechecked; no remaining actionable finding |

The provider probes used the saved financial result and read-only evidence access; they did not rerun
OCR or append chat messages. The unrelated question received a short Russian redirect with no result
cards. An intermediate repeated probe returned the safe fallback; a subsequent repeat and all three
final probes passed. This does not establish deterministic model behaviour.

The app was recreated on port 8001 after confirming no active checks. Its completed run and five
saved messages were preserved. Old model envelopes are cleaned only for display; stored history is
not rewritten. Full financial use cases through the UI remain the separate V11/T16 acceptance checks.

## T13: both use cases through the chat (14 Nov 2026)

Source: completed runs saved by the local Compose application (DeepSeek `deepseek-flash`, real provider) and
read back from a copy of its data volume. The exact commit of that container was not recorded. No values were
given to the model; the comparison uses [05-validation.md](planning/05-validation.md).

| Run (local time) | Input | Result | Time |
|---|---|---|---|
| 19:35 | Complete bundle: report, 22 primaries, context file | 5 verified, 2 discrepancies, 8 insufficient; columns 15 and 17 +12,500.00 from four overhead sources; ADM-27 copy, ОРГ-0603 and ADM-75 excluded (V01) | 7m27s |
| 20:33 | Same bundle | Same statuses, sums and exclusions | 8m17s |
| 21:08 | Without the March payroll statement and the context file | 3 verified, 1 discrepancy, 11 insufficient; payroll partial 4,456,674.83, payroll and total expected/diff null; column 15 +12,500.00 kept (V02) | 7m13s |

This closes the earlier note that missing March was not run with the real model. Two runs show an English
validator note in the chat (`row_label_not_found`, `reinspection_incomplete`); it does not change the result.
README screenshots come from these runs, served by a separate local server with the model unreachable.
