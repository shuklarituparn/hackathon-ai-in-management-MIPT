"""The complete check (T11): uploads -> preparation -> harness -> validation -> reconciliation -> saved result.

``RunService`` is the API's ``RunLauncher``. ``start`` returns at once and the check runs in a background
thread; ``execute`` is the same check run synchronously (command line, tests). One check:

1. The run becomes ``running``. A provider that is not configured fails the run at once with
   ``missing_credentials``; nothing is prepared or guessed.
2. ``ExtractionHarness`` prepares every input with ``DocumentTools`` and interprets the report and each primary
   through the model/tool loop. Every proposal is validated by this run's ``ExtractionValidator`` (T06). Stage
   counters are saved to the run while it works; the trace is written under ``<trace_dir>/<workspace>/``.
3. A completed extraction is reconciled in code (``reconciliation.reconcile``) and the run is saved once as
   ``completed`` with the report, the primaries, the result, the issues and the replay manifest. A chat
   message with the conclusion is saved for the conversation.

What the states mean:

* ``completed``: the check ran. Its result may still contain unverified indicators, unreadable documents and
  documents without a validated extraction; those are in ``result.unresolved`` and ``issues``.
* ``failed``: no result. ``failure.code`` says why: ``missing_credentials``, ``provider_error``, ``timeout``,
  ``budget_exhausted``, ``invalid_input`` (for example no report among the inputs) or ``internal_error``.
* ``cancelled``: the API saves that state before it calls ``cancel``. The store refuses any later save of a
  finished run, so a result that arrives after a cancellation is dropped here and never shown.

Nothing in this module reads a document or calculates a financial value itself.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from . import config, extraction, reconciliation
from . import contracts as c
from .document_tools import DocumentTools
from .harness import CancelToken, ExtractionHarness, ExtractionResult, HarnessLimits, HarnessProgress, JsonlTrace
from .model_adapter import OpenAICompatibleProvider, ProviderConfig

RUN_SERVICE_VERSION = "1"
log = logging.getLogger(__name__)

__all__ = [
    "CHECK_LIMIT_DEFAULTS",
    "RUN_SERVICE_VERSION",
    "RunService",
    "check_limits",
    "provider_from_env",
    "provider_problem",
]

_FAILURE_TEXT = {
    "missing_credentials": "Модель не настроена",
    "provider_error": "Ошибка обращения к модели",
    "timeout": "Превышено время ожидания",
    "budget_exhausted": "Исчерпан лимит обращений к модели",
    "invalid_input": "Набор документов не подходит для проверки",
    "internal_error": "Внутренняя ошибка проверки",
}


def _now() -> datetime:
    return datetime.now(UTC)


#: Per-document budget of a full check. The scanned report needs more turns than the harness default of
#: 12 requests and 24 tool calls: with those the real model spent them all before its first proposal and
#: left no room for a re-inspection round (docs/release-check.md). ``SVERKA_HARNESS_*`` still overrides.
CHECK_LIMIT_DEFAULTS = {
    "SVERKA_HARNESS_DOC_MAX_REQUESTS": "20",
    "SVERKA_HARNESS_DOC_MAX_TOOL_CALLS": "40",
}


def check_limits(env: Mapping[str, str] | None = None) -> HarnessLimits:
    """Harness limits for a check: the service defaults, overridden by the environment."""
    env = os.environ if env is None else env
    merged = {**CHECK_LIMIT_DEFAULTS, **{k: v for k, v in env.items() if v.strip()}}
    return HarnessLimits.from_env(merged)


def provider_from_env() -> OpenAICompatibleProvider:
    """The provider configured by ``SVERKA_PROVIDER_*``. Raises ``ValueError`` for an invalid setting."""
    return OpenAICompatibleProvider(ProviderConfig.from_env())


def provider_problem(factory: Callable[[], Any] = provider_from_env) -> str | None:
    """Why the configured provider cannot be called, or None when it can. Never contains a credential."""
    try:
        provider = factory()
    except ValueError as exc:
        return str(exc)
    missing = getattr(getattr(provider, "config", None), "missing", None)
    names = missing() if callable(missing) else []
    return ("не заданы " + ", ".join(names)) if names else None


class RunService:
    """Runs checks. Implements ``api.RunLauncher``; ``store`` is both the document and the run store."""

    def __init__(
        self,
        store: Any,
        *,
        provider_factory: Callable[[], Any] = provider_from_env,
        limits: HarnessLimits | None = None,
        tools: DocumentTools | None = None,
        trace_dir: str | None = None,
        instructions: Any = None,
        reconcile: Callable[..., c.ReconciliationResult] = reconciliation.reconcile,
    ) -> None:
        self.store = store
        self.provider_factory = provider_factory
        self.limits = limits
        self.tools = tools or DocumentTools(store)
        self.trace_dir = trace_dir
        self.instructions = instructions
        self.reconcile = reconcile
        self._guard = threading.Lock()
        self._tokens: dict[str, CancelToken] = {}
        self._threads: dict[str, threading.Thread] = {}

    # ---------- RunLauncher ----------

    def start(self, run: c.Run) -> None:
        """Start ``run`` in the background and return at once."""
        token = self._register(run)
        thread = threading.Thread(target=self.execute, args=(run, token), name=f"sverka-{run.run_id}", daemon=True)
        with self._guard:
            self._threads[run.run_id] = thread
        thread.start()

    def cancel(self, run: c.Run) -> None:
        """Ask a running check to stop. The API has already saved the run as cancelled."""
        with self._guard:
            token = self._tokens.get(run.run_id)
        if token is not None:
            token.cancel()

    def wait(self, run_id: str, timeout: float | None = None) -> bool:
        """Wait for a background check to end. Returns False when it is still running after ``timeout``."""
        with self._guard:
            thread = self._threads.get(run_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ---------- one check ----------

    def execute(self, run: c.Run, cancel: CancelToken | None = None) -> c.Run:
        """Run the whole check for ``run`` and return the run as stored afterwards. Never raises."""
        token = cancel or self._register(run)
        try:
            self._execute(run, token)
        except Exception as exc:  # a defect must end the run as failed, never leave it running
            log.exception("check %s failed unexpectedly", run.run_id)
            try:
                self._fail(run, "internal_error", f"{type(exc).__name__}: {exc}"[:300])
            except Exception:  # the store itself is unavailable; a restart marks the run interrupted
                log.exception("could not record the failure of %s", run.run_id)
        finally:
            with self._guard:
                self._tokens.pop(run.run_id, None)
                self._threads.pop(run.run_id, None)
        try:
            return self.store.get_run(run.workspace_id, run.run_id)
        except Exception:  # the store is unavailable: report the run as it was handed in
            log.exception("could not read %s after the check", run.run_id)
            return run

    def _register(self, run: c.Run) -> CancelToken:
        with self._guard:
            return self._tokens.setdefault(run.run_id, CancelToken())

    def _execute(self, run: c.Run, token: CancelToken) -> None:
        if not self._save(run, token, status="running", stage="prepare"):
            return  # cancelled before it started
        try:
            provider = self.provider_factory()
        except ValueError as exc:  # an invalid SVERKA_PROVIDER_* value
            self._fail(run, "missing_credentials", f"{exc}; документы не проверялись")
            return
        problem = provider_problem(lambda: provider)
        if problem is not None:
            self._fail(run, "missing_credentials", problem + "; документы не проверялись")
            return
        validator = extraction.ExtractionValidator()
        tools = self.tools.with_validator(validator)
        harness = ExtractionHarness(
            provider=provider,
            tools=tools,
            store=self.store,
            limits=self.limits or check_limits(),
            instructions=self.instructions or extraction.ExtractionInstructions(),
            tool_versions={
                **tools.tool_versions,
                "extraction": extraction.EXTRACTION_VERSION,
                "run_service": RUN_SERVICE_VERSION,
            },
        )

        def on_progress(snapshot: HarnessProgress) -> None:
            stage = "prepare" if snapshot.event == "document_prepared" else "extract"
            self._save(run, token, stage=stage, progress=list(snapshot.stages))

        outcome = harness.extract(run, cancel=token, on_progress=on_progress, trace=self._trace(run))
        replay = outcome.replay.model_copy(
            update={"settings": {**outcome.replay.settings, "reconciliation.tolerance": format(config.TOLERANCE, "f")}}
        )
        if outcome.status == "cancelled" or token():
            self._save(run, token, status="cancelled", progress=outcome.progress, issues=outcome.issues, replay=replay)
            return
        if outcome.status != "completed" or outcome.report is None:
            failure = outcome.failure or c.RunFailure(
                code="internal_error", message="extraction ended without a report"
            )
            self._fail(
                run, failure.code, failure.message, progress=outcome.progress, issues=outcome.issues, replay=replay
            )
            return

        if not outcome.report.indicators:
            message = "в отчете не найдено ни одного показателя проверяемой строки"
            self._fail(
                run, "invalid_input", message, progress=outcome.progress, issues=self._issues(outcome), replay=replay
            )
            return

        progress = [*outcome.progress, c.StageProgress(stage="reconcile", total=1)]
        self._save(run, token, stage="reconcile", progress=progress)
        try:
            result = self.reconcile(run.scope, outcome.report, outcome.primaries, outcome.manifest)
        except Exception as exc:
            log.exception("reconciliation of %s failed", run.run_id)
            self._fail(
                run,
                "internal_error",
                f"reconciliation failed: {type(exc).__name__}: {exc}"[:300],
                progress=[*outcome.progress, c.StageProgress(stage="reconcile", total=1, failed=1)],
                issues=outcome.issues,
                replay=replay,
            )
            return
        done = [
            *outcome.progress,
            c.StageProgress(stage="reconcile", done=1, total=1),
            c.StageProgress(stage="save", done=1, total=1),
        ]
        saved = self._save(
            run,
            token,
            status="completed",
            stage="save",
            progress=done,
            report=outcome.report,
            primaries=outcome.primaries,
            result=result,
            issues=self._issues(outcome),
            replay=replay,
        )
        if saved:
            self._say(
                run, result.conclusion.summary, [c.ConclusionCard(run_id=run.run_id, conclusion=result.conclusion)]
            )

    @staticmethod
    def _issues(outcome: ExtractionResult) -> list[c.Finding]:
        """Run issues: what the harness recorded, plus findings the validator left on the report."""
        issues = list(outcome.issues)
        if outcome.report is not None:
            issues.extend(outcome.report.findings)
        return issues

    def _trace(self, run: c.Run) -> JsonlTrace | None:
        if not self.trace_dir:
            return None
        folder = os.path.join(self.trace_dir, run.workspace_id)
        os.makedirs(folder, exist_ok=True)
        return JsonlTrace(os.path.join(folder, f"{run.run_id}.jsonl"))

    # ---------- persistence ----------

    def _save(self, run: c.Run, token: CancelToken, **update: Any) -> bool:
        """Apply ``update`` to the stored run. False when the run is already finished (cancelled or interrupted
        meanwhile): the caller's work is then late and is dropped."""
        try:
            current = self.store.get_run(run.workspace_id, run.run_id)
            self.store.save_run(current.model_copy(update={**update, "updated_at": _now()}))
            return True
        except c.ToolFailure:
            token.cancel()  # nobody is waiting for this check any more
            return False

    def _fail(self, run: c.Run, code: c.RunFailureCode, message: str, **update: Any) -> None:
        failure = c.RunFailure(code=code, message=f"{_FAILURE_TEXT[code]}: {message}")
        token = self._register(run)
        if self._save(run, token, status="failed", failure=failure, **update):
            card = c.RunStatusCard(run_id=run.run_id, status="failed", stage=None, failure=failure)
            self._say(run, f"Проверка не выполнена. {failure.message}", [card])

    def _say(self, run: c.Run, text: str, cards: list[Any]) -> None:
        """Save the outcome as a chat message. The run itself is already saved; a failure here is logged."""
        try:
            self.store.append_message(
                c.ChatMessage(
                    workspace_id=run.workspace_id,
                    message_id=c.new_id("msg"),
                    run_id=run.run_id,
                    role="assistant",
                    text=text,
                    cards=cards,
                    created_at=_now(),
                )
            )
        except Exception:
            log.exception("could not save the result message of %s", run.run_id)
