# Risks and pending decisions

Reference for the owners. Scope/dates are in [the team plan](README.md); technical policy is in [architecture](../architecture/README.md). Avoid maintaining another copy here.

| Risk | Owner | Action / trigger |
|---|---|---|
| Provider credentials/network/limits unverified | Guillermo | One available provider smoke check in the first block; no model comparison project |
| Clean Docker/OCR live path unverified | Aadi | Build early; another teammate completes G6 before final freeze |
| Eva's preparation/validation chain expands | Eva + Aadi | Keep thin adapters and publish fixtures early. Aadi takes fixture/formula tests; defer full DOCX rendering/complex overlays |
| Harness/UI compete for time | Guillermo | Plain HTML/JS, polling and fixture cards. Aadi helps artifact routes |
| Unsafe legacy decisions enter new runtime | Aadi + Eva | Bypass old report repair/classifier/orchestrator; run D01–D06 regressions |
| Plausible evidence IDs hide incorrect interpretation/coordinates | Eva | Check transcription/conflicts and actual crops; preserve transforms; unresolved after bounded rereads |
| Equality is mistaken for complete accounting support | Aadi | Apply supplied-scope/month coverage before equality; keep partial totals explicit |
| Fresh model output differs or provider fails during demo | Guillermo + Aadi | Save replay metadata/accepted facts and a labeled recording; still prove live processing |
| Input overwrite, answer leakage or stale evidence | Eva + Aadi | Complete input manifest, test-only oracle, immutable versions and old-reference checks |
| Contract changes or run-state races break integration | Aadi + Guillermo | One schema owner; update examples/consumers together; atomic completion and late-cancel tests |
| Different schedules delay a critical handoff | Each owner; Aadi integrates | Publish partial working interfaces before going offline; use written reproductions/reviews and track artifact readiness rather than shared working hours |
| Optional work or stale documentation consumes release buffer | All; Guillermo owns docs | Stop optional features at freeze; compare claims to tested commit; submit at internal target |
| Local/team deployment mistaken for public tenant isolation | Aadi | Document/bind intended instance; public authentication is deferred |

## Confirm during each owner's first work block

- Guillermo: usable provider/account, submission mechanism and receipt process; defense length/connectivity if specified.
- Eva: OCR execution through reused primitives or a hosted adapter behind the same evidence contract.
- Aadi: demo machine and another teammate's clean Docker environment.

These choices do not block fixture-based development, but real provider/OCR access must work for G2. Hour estimates can be rebalanced after that checkpoint.

## Response to failures

Keep unreadable documents and possible relevance visible. Unresolved fields remain unresolved; partial sums never become complete expectations. A provider failure produces a visible failed/interrupted run and explicit retry. Do not substitute answer-key values, unlabelled replay or the old rule-only pipeline for live processing.

Deferred work: public auth, PostgreSQL/distributed jobs, arbitrary code agents, broad format/handwriting support, training/benchmarks and incremental recalculation. These are roadmap topics, not delivered features.
