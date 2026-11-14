"""HTTP API for workspaces, documents, runs, conversation and evidence (T02).

Routes: docs/architecture/README.md#proposed-http-routes. Bodies and responses are the v1 contract models;
errors are ``{"error": ToolError}`` with a matching HTTP status.

Running a check and answering chat messages are injected (``RunLauncher``, ``ChatResponder``):

* ``SVERKA_RUN_MODE=live`` (default): ``run_service.RunService`` runs the complete check in the background and
  ``harness.FollowUpResponder`` answers questions about a saved run. Both need ``SVERKA_PROVIDER_*``; without
  it a check fails at once with ``missing_credentials`` and a question gets the saved cards without a comment.
* ``SVERKA_RUN_MODE=disabled``: a started check fails at once with a clear "not connected" state.
* ``SVERKA_RUN_MODE=fixture``: interface development only. A check completes with the synthetic example
  result and carries a ``fixture_result`` issue saying that no documents were checked.

Workspace IDs separate data for a local/team installation; they are not authentication.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import re
import threading
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import __version__, config
from . import contracts as c
from .interfaces import ActiveRunConflict
from .storage import ACTIVE_STATUSES, SqliteStore, clean_filename

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

# Served inline; anything else is a download. PDFs are exempt from the sandbox CSP (browser viewers need it).
SAFE_INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "text/plain"}
ERROR_STATUS = {"not_found": 404, "invalid_reference": 400, "unreadable": 422, "unsupported": 415, "timeout": 504}
_CHUNK = 1 << 20
_JUNK = re.compile(r"^(~\$|\.)|^thumbs\.db$|^desktop\.ini$", re.I)
_ID_RE = {p: re.compile(rf"^{p}_[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$") for p in ("ws", "doc", "ver", "blk", "art", "run")}


def _now() -> datetime:
    return datetime.now(UTC)


class ApiError(Exception):
    """An HTTP error with a typed body."""

    def __init__(self, status: int, code: c.ToolErrorCode, message: str):
        super().__init__(message)
        self.status = status
        self.error = c.ToolError(code=code, message=message)


def _check_id(value: str, prefix: str, what: str) -> None:
    if not _ID_RE[prefix].fullmatch(value):
        raise ApiError(404, "not_found", f"{what} not found")


# ======================= injected services =======================


class RunLauncher(Protocol):
    """Starts and cancels checks. ``start`` must return quickly; long work runs in the background (T11).
    ``cancel`` is called after the run is already saved as cancelled; late results are rejected by the store."""

    def start(self, run: c.Run) -> None: ...

    def cancel(self, run: c.Run) -> None: ...


class ChatResponder(Protocol):
    """Returns assistant replies to a saved user message (T08 replaces the stub)."""

    def respond(self, message: c.ChatMessage) -> list[c.ChatMessage]: ...


@dataclass
class Services:
    store: Any  # interfaces.DocumentStore and interfaces.RunStore
    launcher: RunLauncher
    responder: ChatResponder


class DisabledRunLauncher:
    """``SVERKA_RUN_MODE=disabled``: the check fails at once and says why."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def start(self, run: c.Run) -> None:
        failure = c.RunFailure(
            code="internal_error", message="Сервис проверки отключен (SVERKA_RUN_MODE); документы не проверялись."
        )
        self.store.save_run(run.model_copy(update={"status": "failed", "failure": failure, "updated_at": _now()}))

    def cancel(self, run: c.Run) -> None:
        pass


class FixtureRunLauncher:
    """Interface development only: completes the check with the labelled synthetic example result.

    Limitation: the example cites block IDs (``blk_kfz_c15``, ``blk_act118_amount``, ...) that do not exist in
    the workspace, so ``GET .../evidence/{eid}`` returns 404 for them. Blocks are not seeded on purpose: they
    would have to point at the user's uploaded files and show the wrong document. Exercise the evidence panel
    with blocks saved through the store (see ``tests/test_api.py``) or with real evidence once T04/T05 land."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def start(self, run: c.Run) -> None:
        from .fakes import load_example

        issue = c.Finding(
            code="fixture_result",
            message="Синтетический пример результата для разработки интерфейса; документы не проверялись.",
        )
        replay = c.ReplayManifest(
            input_sha256={v: self.store.get_version(run.workspace_id, v).sha256 for v in run.input_version_ids},
            app_version=__version__,
            settings={"run_mode": "fixture"},
        )
        result = load_example("result").model_copy(update={"scope": run.scope})
        update = {"status": "completed", "stage": "save", "result": result, "issues": [*run.issues, issue]}
        self.store.save_run(run.model_copy(update={**update, "replay": replay, "updated_at": _now()}))

    def cancel(self, run: c.Run) -> None:
        pass


_STATUS_TEXT = {
    "queued": "Проверка поставлена в очередь.",
    "running": "Проверка выполняется.",
    "completed": "Проверка завершена. Результаты ниже.",
    "failed": "Проверка не выполнена.",
    "cancelled": "Проверка отменена.",
    "interrupted": "Проверка прервана перезапуском приложения. Запустите ее снова.",
}


class SavedResultResponder:
    """Stub until the harness (T08): answers with the selected run's saved state as typed cards."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def respond(self, message: c.ChatMessage) -> list[c.ChatMessage]:
        cards: list[Any] = []
        if message.run_id is None:
            text = "Ассистент еще не подключен. Загрузите документы и запустите проверку: результат появится здесь."
        else:
            run = self.store.get_run(message.workspace_id, message.run_id)
            text = _STATUS_TEXT[run.status]
            if run.failure is not None:
                text += f" {run.failure.message}"
            elif any(i.code == "fixture_result" for i in run.issues):
                text += " Это синтетический пример, документы не проверялись."
            cards.append(c.RunStatusCard(run_id=run.run_id, status=run.status, stage=run.stage, failure=run.failure))
            if run.result is not None:
                cards.append(c.ConclusionCard(run_id=run.run_id, conclusion=run.result.conclusion))
                cards.extend(c.IndicatorCard(run_id=run.run_id, indicator=i) for i in run.result.indicators)
        reply = c.ChatMessage(
            workspace_id=message.workspace_id,
            message_id=c.new_id("msg"),
            run_id=message.run_id,
            role="assistant",
            text=text,
            cards=cards,
            created_at=_now(),
        )
        return [reply]


def default_services(data_dir: str | None = None) -> Services:
    data_dir = data_dir or config.DATA_DIR
    store = SqliteStore(data_dir)
    if config.RUN_MODE == "fixture":
        return Services(store=store, launcher=FixtureRunLauncher(store), responder=SavedResultResponder(store))
    if config.RUN_MODE != "live":
        return Services(store=store, launcher=DisabledRunLauncher(store), responder=SavedResultResponder(store))
    # Imported here: the harness and its model framework are needed only when checks really run.
    from .extraction import ExtractionInstructions
    from .harness import FollowUpResponder
    from .run_service import RunService, provider_from_env

    launcher = RunService(store, trace_dir=os.path.join(data_dir, "traces"))
    try:
        responder: ChatResponder = FollowUpResponder(
            provider=provider_from_env(), runs=store, documents=store, instructions=ExtractionInstructions()
        )
    except ValueError:  # an invalid provider setting: checks report it; questions get the saved cards
        log.warning("the model provider settings are invalid; follow-up answers show saved results only")
        responder = SavedResultResponder(store)
    return Services(store=store, launcher=launcher, responder=responder)


_init_lock = threading.Lock()


def get_services(request: Request) -> Services:
    state = request.app.state
    if getattr(state, "services", None) is None:
        with _init_lock:
            if getattr(state, "services", None) is None:
                state.services = default_services()
    return state.services


Svc = Annotated[Services, Depends(get_services)]


def _artifact_url(workspace_id: str, artifact_id: str) -> str:
    return f"/api/workspaces/{workspace_id}/artifacts/{artifact_id}"


# ======================= uploads =======================


def _member_name(info: zipfile.ZipInfo) -> str | None:
    """Display name of an archive member, or None for folders and system files. Never used as a path."""
    name = info.filename
    if not info.flag_bits & 0x800:  # not flagged UTF-8: Windows archives use cp866
        try:
            name = name.encode("cp437").decode("cp866")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    parts = [p for p in unicodedata.normalize("NFC", name).replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts or info.is_dir() or parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts[:-1]):
        return None
    if _JUNK.search(parts[-1]):
        return None
    return clean_filename(parts[-1])


def _unpack_zip(data: bytes, archive_name: str) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        members = [(info, name) for info in zf.infolist() if (name := _member_name(info)) is not None]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
        raise ApiError(400, "unreadable", f"{archive_name} is not a valid zip archive") from None
    if len(members) > config.MAX_ARCHIVE_FILES:
        raise ApiError(413, "unsupported", f"{archive_name} has more than {config.MAX_ARCHIVE_FILES} files")
    if not members:
        raise ApiError(400, "unreadable", f"{archive_name} contains no files")
    limit, total, out = config.MAX_ARCHIVE_MB * 1024 * 1024, 0, []
    try:
        for info, name in members:
            if info.flag_bits & 0x1:
                raise ApiError(415, "unsupported", f"{archive_name}: encrypted file {name} is not supported")
            buf = bytearray()
            with zf.open(info) as member:
                while chunk := member.read(_CHUNK):
                    total += len(chunk)
                    if total > limit:  # counted while reading: declared sizes in the archive can lie
                        raise ApiError(
                            413, "unsupported", f"{archive_name} unpacks to more than {config.MAX_ARCHIVE_MB} MB"
                        )
                    buf += chunk
            out.append((name, bytes(buf)))
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError, OSError, ValueError):
        raise ApiError(400, "unreadable", f"{archive_name} is damaged") from None
    return out


def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """Read and unpack everything before saving anything, so a rejected upload stores nothing."""
    limit, total, items = config.MAX_UPLOAD_MB * 1024 * 1024, 0, []
    for upload in files:
        buf = bytearray()
        while chunk := upload.file.read(_CHUNK):
            total += len(chunk)
            if total > limit:
                raise ApiError(413, "unsupported", f"upload exceeds {config.MAX_UPLOAD_MB} MB")
            buf += chunk
        name = clean_filename(upload.filename or "file")
        if name.lower().endswith(".zip"):
            items.extend(_unpack_zip(bytes(buf), name))
        else:
            items.append((name, bytes(buf)))
    if not items:
        raise ApiError(400, "invalid_reference", "no files uploaded")
    return items


# ======================= routes =======================


@router.post("/workspaces", status_code=201, response_model=c.Workspace)
def create_workspace(svc: Svc):
    return svc.store.create_workspace()


@router.get("/workspaces/{wid}/documents", response_model=list[c.DocumentVersion])
def list_documents(wid: str, svc: Svc):
    _check_id(wid, "ws", "workspace")
    return svc.store.list_versions(wid)


@router.post("/workspaces/{wid}/documents", status_code=201, response_model=list[c.DocumentVersion])
def upload_documents(
    wid: str,
    svc: Svc,
    files: Annotated[list[UploadFile], File()],
    role: Annotated[c.DocumentRole, Form()],
    document_id: Annotated[str | None, Form()] = None,
):
    """Upload files (or zip archives, unpacked) with one role. ``document_id`` adds a replacement version of
    an existing document and takes exactly one file."""
    _check_id(wid, "ws", "workspace")
    svc.store.get_workspace(wid)
    if document_id is not None:
        _check_id(document_id, "doc", "document")
    items = _read_uploads(files)
    if document_id is not None and len(items) != 1:
        raise ApiError(400, "invalid_reference", "a replacement version takes exactly one file")
    return [
        svc.store.save_document(wid, filename=name, role=role, data=data, document_id=document_id)
        for name, data in items
    ]


@router.post("/workspaces/{wid}/runs", status_code=202, response_model=c.Run)
def start_run(wid: str, body: c.StartRunRequest, svc: Svc):
    """Start a check and return at once; poll GET .../runs/{rid} for progress."""
    _check_id(wid, "ws", "workspace")
    if not body.scope.confirmed:
        raise ApiError(422, "invalid_reference", "confirm the scope before starting a check")
    run = svc.store.create_run(wid, body)
    try:
        svc.launcher.start(run)
    except Exception as exc:  # never leave an active run behind that nobody is executing
        failure = c.RunFailure(code="internal_error", message=f"Проверку не удалось запустить: {type(exc).__name__}")
        try:
            current = svc.store.get_run(wid, run.run_id)
            svc.store.save_run(
                current.model_copy(update={"status": "failed", "failure": failure, "updated_at": _now()})
            )
        except c.ToolFailure:
            pass
    return svc.store.get_run(wid, run.run_id)


@router.get("/workspaces/{wid}/runs", response_model=list[c.Run])
def list_runs(wid: str, svc: Svc):
    _check_id(wid, "ws", "workspace")
    return svc.store.list_runs(wid)


@router.get("/workspaces/{wid}/runs/{rid}", response_model=c.Run)
def get_run(wid: str, rid: str, svc: Svc):
    _check_id(wid, "ws", "workspace")
    _check_id(rid, "run", "run")
    return svc.store.get_run(wid, rid)


@router.post("/workspaces/{wid}/runs/{rid}/cancel", response_model=c.Run)
def cancel_run(wid: str, rid: str, svc: Svc):
    """Cancel a queued/running check. Idempotent: a finished run is returned unchanged."""
    _check_id(wid, "ws", "workspace")
    _check_id(rid, "run", "run")
    run = svc.store.get_run(wid, rid)
    if run.status in ACTIVE_STATUSES:
        try:  # saved first, so a result that arrives later is rejected by the store
            svc.store.save_run(run.model_copy(update={"status": "cancelled", "updated_at": _now()}))
        except c.ToolFailure:
            pass  # it finished meanwhile; report the state it reached
        else:
            svc.launcher.cancel(run)
    return svc.store.get_run(wid, rid)


@router.get("/workspaces/{wid}/messages", response_model=list[c.ChatMessage])
def list_messages(wid: str, svc: Svc):
    _check_id(wid, "ws", "workspace")
    return svc.store.list_messages(wid)


@router.post("/workspaces/{wid}/messages", status_code=201, response_model=list[c.ChatMessage])
def post_message(wid: str, body: c.PostMessageRequest, svc: Svc):
    """Save the user's message and the assistant's replies; returns them in order."""
    _check_id(wid, "ws", "workspace")
    if body.run_id is not None:
        svc.store.get_run(wid, body.run_id)
    user = c.ChatMessage(
        workspace_id=wid,
        message_id=c.new_id("msg"),
        run_id=body.run_id,
        role="user",
        text=body.text,
        created_at=_now(),
    )
    svc.store.append_message(user)
    try:
        replies = svc.responder.respond(user)
    except Exception as exc:  # never leave the user's message unanswered without saying so
        log.exception("chat responder failed for message %s", user.message_id)
        failed = c.ChatMessage(
            workspace_id=wid,
            message_id=c.new_id("msg"),
            run_id=body.run_id,
            role="system",
            text=f"Ответ не получен ({type(exc).__name__}). Сообщение сохранено, задайте вопрос еще раз.",
            created_at=_now(),
        )
        replies = [failed]
    for reply in replies:
        svc.store.append_message(reply)
    return [user, *replies]


@router.get("/workspaces/{wid}/evidence/{eid}", response_model=c.EvidenceView)
def get_evidence(wid: str, eid: str, svc: Svc):
    _check_id(wid, "ws", "workspace")
    _check_id(eid, "blk", "evidence")
    block = svc.store.get_block(wid, eid)
    version = svc.store.get_version(wid, block.version_id)
    render = block.locator.render_id if block.locator.kind == "page_region" else None
    return c.EvidenceView(
        block=block,
        version=version,
        artifact_url=_artifact_url(wid, render) if render else None,
        original_url=_artifact_url(wid, version.original_artifact_id),
    )


@router.get("/workspaces/{wid}/artifacts/{aid}")
def get_artifact(wid: str, aid: str, svc: Svc):
    """Serve a stored original, page render or crop. Artifacts never change, so they cache well."""
    _check_id(wid, "ws", "workspace")
    _check_id(aid, "art", "artifact")
    ref, data = svc.store.read_artifact(wid, aid)
    if ref.kind == "original":
        filename = svc.store.get_version(wid, ref.version_id).original_filename
    else:
        filename = ref.artifact_id + (mimetypes.guess_extension(ref.media_type) or "")
    inline = ref.media_type in SAFE_INLINE_TYPES
    headers = {
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if ref.media_type != "application/pdf":
        headers["Content-Security-Policy"] = "sandbox"
    return Response(data, media_type=ref.media_type if inline else "application/octet-stream", headers=headers)


# ======================= installation =======================


def _error(status: int, error: c.ToolError) -> JSONResponse:
    return JSONResponse({"error": error.model_dump(mode="json")}, status_code=status)


async def _on_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return _error(exc.status, exc.error)


async def _on_tool_failure(request: Request, exc: c.ToolFailure) -> JSONResponse:
    return _error(ERROR_STATUS[exc.error.code], exc.error)


async def _on_conflict(request: Request, exc: ActiveRunConflict) -> JSONResponse:
    return _error(409, c.ToolError(code="invalid_reference", message=str(exc)))


async def _limit_body(request: Request, call_next):
    """Reject oversized uploads from their declared length, before the body is spooled to disk."""
    if request.method == "POST" and request.url.path.startswith("/api/"):
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > config.MAX_UPLOAD_MB * 1024 * 1024 + _CHUNK:
            return _error(413, c.ToolError(code="unsupported", message=f"upload exceeds {config.MAX_UPLOAD_MB} MB"))
    return await call_next(request)


def install(app: FastAPI, services: Services | None = None) -> None:
    """Register the API routes, error handlers and upload limit on ``app``."""
    app.include_router(router)
    app.add_exception_handler(ApiError, _on_api_error)
    app.add_exception_handler(c.ToolFailure, _on_tool_failure)
    app.add_exception_handler(ActiveRunConflict, _on_conflict)
    app.middleware("http")(_limit_body)
    if services is not None:
        app.state.services = services
