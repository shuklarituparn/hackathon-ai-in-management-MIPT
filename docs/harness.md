# Harness integration (T08)

`sverka.harness` interprets one report and its primary documents through a model/tool loop. It returns validator-produced observations for reconciliation; it does not calculate the financial result or persist a completed run.

Qwen-Agent's `FnCallAgent` drives model/tool iteration through `sverka.qwen_runtime`. `ExtractionHarness` and `FollowUpResponder` retain our document coordination, validation, budgets, cancellation, progress, and storage boundaries.

Install the pinned minimal dependency (`qwen-agent==0.0.34`) with the project dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Only our application tools are registered. Stock RAG, file/shell tools, code execution, and Qwen-Agent's optional Gradio UI are not enabled. The browser interface remains T09. The minimal package avoids the optional GUI dependencies' conflicting Pydantic pins.

The requirements also pin SoundFile, tqdm, and python-dateutil because this Qwen-Agent release imports them without declaring them in its minimal dependency list. Docker CI checks that the runtime imports successfully.

**Qwen-Agent is the framework; DeepSeek Flash remains the selected model.** The library itself needs no API key. `OpenAICompatibleProvider` names an HTTP format and can call the configured compatible provider.

## Configuration

The adapter calls `POST {BASE_URL}/chat/completions`. The selected endpoint/model must support function tools and image input. For example, a base URL ending in `/v1` is used with `/v1/chat/completions`. No provider or model is selected automatically.

| Variable | Default | Meaning |
|---|---|---|
| `SVERKA_PROVIDER_BASE_URL` | Unset; required | Endpoint base, without `/chat/completions` |
| `SVERKA_PROVIDER_MODEL` | Unset; required | Model identifier accepted by that endpoint |
| `SVERKA_PROVIDER_API_KEY` | Unset | Required with bearer authentication; backend only |
| `SVERKA_PROVIDER_NAME` | `openai-compatible` | Provider label recorded with the run |
| `SVERKA_PROVIDER_AUTH` | `bearer` | `bearer` or `none` |
| `SVERKA_PROVIDER_TIMEOUT` | `120` | Request timeout in seconds; see transport limit below |
| `SVERKA_PROVIDER_MAX_OUTPUT_TOKENS` | `8192` | Requested output cap; `0` omits it |
| `SVERKA_PROVIDER_MAX_TOKENS_FIELD` | `max_tokens` | `max_tokens` or `max_completion_tokens`, according to the endpoint |
| `SVERKA_PROVIDER_TEMPERATURE` | `0` | Sampling setting; an explicitly empty value omits it |
| `SVERKA_PROVIDER_THINKING` | Unset; omitted | Optional `enabled` or `disabled` for endpoints supporting DeepSeek's thinking parameter |

### DeepSeek

Use `deepseek-flash` with `https://api.deepseek.com` and a key from [DeepSeek Platform](https://platform.deepseek.com/api_keys). The current Flash model supports images and tool calls; `deepseek-v4-pro` does not support image input. See [model capabilities](https://api-docs.deepseek.com/quick_start/pricing/) and [vision input](https://api-docs.deepseek.com/guides/vision/).

1. Sign in to DeepSeek Platform and create an API key on the linked API Keys page.
2. Check the account's available balance and top up if needed. API requests use the platform balance; see [billing](https://api-docs.deepseek.com/quick_start/pricing/).
3. Enter the key locally with the masked prompt below. Keep it out of source control and chat messages.

Set these variables in the same PowerShell session used to run the tests:

```powershell
$env:SVERKA_PROVIDER_NAME = "deepseek"
$env:SVERKA_PROVIDER_BASE_URL = "https://api.deepseek.com"
$env:SVERKA_PROVIDER_MODEL = "deepseek-flash"
$env:SVERKA_PROVIDER_MAX_TOKENS_FIELD = "max_tokens"
$env:SVERKA_PROVIDER_THINKING = "enabled"
$env:SVERKA_PROVIDER_TEMPERATURE = " "  # omit the optional temperature parameter
$providerKey = Read-Host "DeepSeek API key" -AsSecureString
$env:SVERKA_PROVIDER_API_KEY = [System.Net.NetworkCredential]::new("", $providerKey).Password
python -m pytest -q -m live tests/test_provider_live.py
```

The adapter preserves DeepSeek's `reasoning_content` between tool-call turns, as required by its [thinking-mode API](https://api-docs.deepseek.com/guides/thinking_mode/). This is provider conversation state, not a user-facing financial explanation. The live check still requires credentials; transport tests alone do not establish model accuracy.

Choosing a Qwen model later requires a separate [Alibaba Model Studio key](https://www.alibabacloud.com/help/en/model-studio/get-api-key) and its matching regional endpoint. Installing Qwen-Agent does not require that switch.

### Harness limits

All limits below use the prefix `SVERKA_HARNESS_`. Load them with `HarnessLimits.from_env()`; the constructor alone uses defaults.

| Suffix | Default | Meaning |
|---|---|---|
| `MAX_REINSPECTION_ROUNDS` | `2` | Further proposal rounds after the first validation |
| `DOC_MAX_REQUESTS` | `12` | Provider attempts per document, including retries |
| `DOC_MAX_TOOL_CALLS` | `24` | Tool calls per document, including rejected calls |
| `DOC_MAX_SECONDS` | `420` | Time per document's extraction loop |
| `INITIAL_REQUESTS` | `4` | Requests reserved for each waiting document |
| `INITIAL_TOOL_CALLS` | `6` | Tool calls reserved for each waiting document |
| `INITIAL_SECONDS` | `60` | Extraction time reserved for each waiting document |
| `RUN_MAX_REQUESTS` | `200` | Provider attempts across the run |
| `RUN_MAX_TOOL_CALLS` | `320` | Tool calls across the run |
| `RUN_MAX_SECONDS` | `3600` | Whole extraction call, including preparation |
| `REQUEST_SECONDS` | `120` | Harness cap per provider attempt; the provider cap also applies |
| `PROVIDER_RETRIES` | `2` | Additional attempts after a retryable provider failure |
| `RETRY_BACKOFF_SECONDS` | `2` | Base delay between retries |
| `MAX_UNPRODUCTIVE_TURNS` | `3` | Consecutive responses without useful tool progress |
| `MAX_PROVIDER_FAILED_DOCUMENTS` | `3` | Consecutive provider-failed documents before stopping the run |
| `MAX_IMAGE_BYTES` | `6000000` | Maximum bytes per image sent to the model |
| `MAX_OVERVIEW_CHARS` | `60000` | Initial evidence overview size |
| `MAX_TOOL_RESULT_CHARS` | `20000` | Maximum model-facing tool result size |

A check started through the run service (API or command line) uses `DOC_MAX_REQUESTS=20` and `DOC_MAX_TOOL_CALLS=40` unless the environment sets them (`run_service.check_limits`): with the harness defaults the real model spent every turn on the scanned report before its first proposal. The run totals are unchanged.

The default budgets reserve an initial share for all 23 documents in the supplied bundle. Insufficient configured reservations fail explicitly. Unreadable inputs, cancellation, preparation time, or a provider failure can still stop processing; reservations do not promise successful extraction.

The transport checks the remaining deadline during response-body reads. Connection and header handling use urllib's socket inactivity timeout; a server that keeps sending incomplete headers can exceed the wall-clock budget until headers finish. The harness rejects responses that arrive after its deadline.

The HTTP application and the command line run this harness through `sverka/run_service.py` (T11), with `SVERKA_RUN_MODE=live` as the default. Compose forwards the `SVERKA_PROVIDER_*` variables and the main `SVERKA_HARNESS_*` limits into the container; copy `.env.example` to `.env` and set them there. An empty limit keeps its default. `/health` reports `provider.configured` and the names of missing settings, never their values. Existing `SVERKA_LLM_*` settings belong to the legacy classifier and do not configure this adapter.

## Run service (T11)

`RunService(store, trace_dir=...)` in `sverka/run_service.py` is the API's `RunLauncher`:

- `start(run)` returns at once and runs the check in a background thread; `execute(run)` runs it synchronously (command line, tests); `cancel(run)` stops it at the next model or tool call.
- Each check gets its own `ExtractionValidator` over the shared `DocumentTools` (`with_validator`), so prepared documents are reused between checks of one process.
- Stage counters (`prepare`, `extract`, `validate`, then `reconcile` and `save`) are saved to the run while it works. The trace is written to `<data dir>/traces/<workspace>/<run>.jsonl`, also for failed and cancelled checks.
- A completed extraction is reconciled and saved once as `completed`, with the report, primaries, result, issues and replay manifest, followed by an assistant message carrying the conclusion card. A `completed` check may still hold unverified indicators and unreadable documents.
- No result: `failed` with `missing_credentials`, `provider_error`, `timeout`, `budget_exhausted`, `invalid_input` or `internal_error`, and a message with the run status card.
- The API saves `cancelled` before it calls `cancel`. The store refuses to change a finished run, so a result that arrives later is dropped and no message is saved.

## Aadi: extraction boundary

The application supplies a `DocumentStore`, Eva's `EvidenceTools`, and a `Run` containing workspace-scoped input version IDs and a confirmed scope. Run this synchronous operation in the application's background execution path, not directly on the async request loop.

```python
from sverka.harness import CancelToken, ExtractionHarness, HarnessLimits, JsonlTrace
from sverka.model_adapter import OpenAICompatibleProvider, ProviderConfig

def extract_run(run, document_store, evidence_tools, trace_path, on_progress):
    # The service chooses a run-scoped path and creates its parent directory.
    provider = OpenAICompatibleProvider(ProviderConfig.from_env())
    harness = ExtractionHarness(
        provider=provider,
        tools=evidence_tools,
        store=document_store,
        limits=HarnessLimits.from_env(),
    )
    cancel = CancelToken()
    # Register cancel.cancel with the run service before starting extraction.
    outcome = harness.extract(
        run,
        cancel=cancel,
        on_progress=on_progress,
        trace=JsonlTrace(trace_path),
    )
    return outcome
```

The caller can also pass `prepared={version_id: PreparedDocument}` to reuse preparation already performed by the service. Remaining inputs are prepared by the harness. `on_progress` receives `HarnessProgress`; its `stages` contain `StageProgress` objects suitable for the run's progress field. Cancellation is checked around calls; an in-flight synchronous call finishes or times out before the cancellation is observed, and its late result is discarded.

`ExtractionResult` returns:

- `status` (`completed`, `failed`, or `cancelled`) and an optional `RunFailure`.
- `report` and `primaries`: validator outputs only.
- `manifest`, per-document `documents`, `issues`, and `progress`.
- `replay`, usage counters, and trace events when no external trace sink was supplied.

Extraction status `completed` means a validated report and recorded primary-document outcomes. It may contain unresolved fields or failed primaries. It is not permission to set `Run.status="completed"`: Aadi must reconcile the observations and manifest, attach the structured result, and persist the final run atomically. A failed or cancelled extraction must not produce a completed check.

A primary present in `manifest` but absent from `primaries` has no validated extraction. Reconciliation must treat it as possibly relevant, using `documents` and `issues`, rather than treating it as an absent or zero-cost document.

For follow-ups, construct `FollowUpResponder(provider=provider, runs=run_store, documents=document_store)` and inject it where T02 expects a responder with `respond(ChatMessage) -> list[ChatMessage]`. The run service/API remains responsible for saving the returned messages. The responder reads the selected saved run and cited evidence; it does not re-extract or reconcile. Typed cards come from the saved result. `FollowUpLimits` can be passed in Python; it has no environment-variable loader.

The follow-up skill limits answers to the check, supporting documents and explanations of its financial terms or method. Replies are Russian by default; explicit translation of the check is allowed. The provider must return a private, validated envelope with `kind` (`answer` or `out_of_scope`), `text`, `entry_ids`, `evidence_ids` and `include_conclusion`. Only readable text and stored cards reach the conversation. Mixed prose/JSON is accepted only when its final envelope validates; malformed references, code, internal fields and unsupported amounts require a bounded repair or a human-readable fallback. An out-of-scope reply becomes a fixed short invitation to ask about the check, with no result cards. Technical failure details stay in the trace.

User feedback can arrive as another chat message. Applying a confirmed category/service-period correction, saving its reason and evidence, and creating a linked rerun remain the planned T15 feature. The framework change does not implement that financial revision workflow.

The follow-up prose amount check is a heuristic: it checks decimal amounts and integers of at least 1,000, not every small integer or the meaning of a sentence. The UI must use structured cards for financial values.

## Eva: instructions and tools

`EvidenceTools` signatures in `sverka/interfaces.py` remain unchanged. The model submits `ReportProposal` or `PrimaryProposal`; only `validate_extraction` assigns accepted/unresolved observations. Its `ValidationOutcome.reinspection` drives further inspection within the limits.

The default `SkillInstructions` loads `sverka/skills/assistant.md` and appends `sverka/skills/extraction.md` when present. Eva's request builders can instead implement `Instructions`: a `versions` mapping plus `extraction_system()`, `extraction_request(DocumentRequest)`, and `followup_system()`. `DocumentRequest` supplies the prepared document, scope, validated report when available, and evidence overview. Pass that implementation as `instructions=` to the harness/responder.

Report proposals must preserve the printed category hierarchy: `category` identifies each indicator and `components` lists its direct additive child entry IDs. Primary requests include every validated report indicator, including entries with an unknown category, so those references stay intact. An unsplit amount is proposed once at its supported common parent, with the finer categories in `possible_categories`. T06's instructions and validator must enforce the same interpretation; the harness passes this structure through without deciding acceptance or inventing a split.

`harness_fakes.py` supplies scripted model responses and test evidence/validation. Its validator is a test double; it is not a substitute for T06.

T06 is implemented in `sverka/extraction.py`:

- `ExtractionValidator()` is the validator of one run. Pass it as `DocumentTools(store, validator=ExtractionValidator())`. It remembers the validated report, so a primary's categories must belong to that report's inventory, and the entries of the report's first submission, so later rounds must cover the same entries. `validate_extraction(proposal, prepared, report=...)` is the same check as a plain function.
- `ExtractionInstructions()` implements `Instructions`: the assistant skill, `skills/extraction.md` and the request builders `report_request`/`primary_request`. Pass it as `instructions=` to the harness and the responder. The default `SkillInstructions` also appends `skills/extraction.md`, but keeps the generic request text.
- A field is accepted only when the cited evidence of the same document shows the value: numbers and dates must be readable in a cited block, identifiers must match as a whole, and a period must be stated. A formula cell supports the value derived from it with basis `inferred`. A value the evidence does not show is never repaired; it stays unresolved with a reinspection request. Contradictions inside a document (amount in words, allocation lines, several stated periods) are recorded in `PrimaryExtraction.conflicts`, and report arithmetic that does not add up in `ReportExtraction.findings`.

## Trace persistence

The service passes a sink implementing `record(event: dict)`. `JsonlTrace` appends JSON lines to a caller-chosen path; `MemoryTrace` keeps events in memory. Without a supplied sink, extraction returns events in `outcome.trace`.

Events include model/tool requests and outputs, outcomes, and redacted configuration. Image entries reference stored artifacts and record hashes, rather than copying image bytes. Preserve those source artifacts with the trace. A sink failure adds `trace_incomplete` to extraction issues without silently changing the check's outcome.

`Run.replay` receives `outcome.replay`, which records input hashes and configuration/version metadata, including the installed Qwen-Agent version. It does not store the transcript. Aadi must persist the trace separately under the run's workspace and retain failed/cancelled runs' traces too. T08 adds no shared artifact kind or database schema for transcripts. Replaying accepted observations reproduces reconciliation; a fresh model call may differ.

## Checks

From the repository root, using the development environment. Ordinary tests exercise the installed Qwen-Agent loop with scripted model responses and need no provider key:

```bash
python -m ruff check .
python -m pytest -q tests/test_qwen_runtime.py tests/test_harness.py tests/test_model_adapter.py
python -m pytest -q --ignore=tests/test_cases.py -m "not live and not ocr"
git diff --check
```

Configure a real provider, then run:

```bash
python -m pytest -q -m live tests/test_provider_live.py
```

The live tests check real tool calling and image input against a known fixture, using fake evidence tools/validation. They do not establish full extraction quality or T11 integration. Without configuration they skip. The live check is pending for the T08 handoff; a skip is not a pass. Local development used Python 3.12.10; CI uses Python 3.13.
