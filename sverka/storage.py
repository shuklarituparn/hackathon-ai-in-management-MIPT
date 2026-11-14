"""Immutable document storage and run persistence on files and SQLite (T02).

Layout under the data directory (``/data`` in Docker, ``SVERKA_DATA_DIR`` elsewhere)::

    sverka.db              metadata; schema version in PRAGMA user_version
    blobs/ab/<sha256>      file contents, content-addressed and never modified

``SqliteStore`` implements ``interfaces.DocumentStore`` and ``interfaces.RunStore`` with the same rules as
``fakes.InMemoryStore``:

* Filenames are metadata only; bytes are stored under their SHA-256, so equal names never overwrite.
* Every ID is resolved inside its workspace. An ID from another workspace is ``not_found``.
* Versions, artifacts and evidence blocks are never modified. A replacement upload is a new version.
* Each write is one SQLite transaction; blob files are written to a temporary file and renamed. A crash
  leaves either the old state or the new one, never metadata pointing at a missing or partial file.
* At most one queued/running check per workspace (enforced by a unique index).
* Runs left queued/running by a previous process become ``interrupted`` when the store opens. Only the process
  that holds ``sverka.lock`` in the data directory does this, so a second process on the same data (a CLI
  command, another worker) never interrupts a live check.
* A ``page_region`` evidence block must point at a page render or crop of its own version, never at the
  original file.
* A finished run (completed, failed, cancelled, interrupted) cannot be saved again, so a late completion
  after cancellation is rejected rather than overwriting the cancellation.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
import threading
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import contracts as c
from .interfaces import ActiveRunConflict

SCHEMA_VERSION = 1
ACTIVE_STATUSES = ("queued", "running")
RENDER_KINDS = ("page_render", "crop")  # artifacts a page_region locator may draw a box on
FINISHED_STATUSES = ("completed", "failed", "cancelled", "interrupted")
# Fields fixed when a run is created; save_run refuses to change them.
RUN_IDENTITY = ("workspace_id", "run_id", "predecessor_run_id", "input_version_ids", "scope", "created_at")

_FORMATS = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".txt": "txt",
}

_SCHEMA = """
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces,
    version_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE versions (
    version_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces,
    document_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX versions_by_document ON versions (workspace_id, document_id);
CREATE TABLE blocks (
    workspace_id TEXT NOT NULL REFERENCES workspaces,
    block_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (workspace_id, block_id)
);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_run_per_workspace ON runs (workspace_id) WHERE status IN ('queued', 'running');
CREATE TABLE messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL REFERENCES workspaces,
    run_id TEXT,
    data TEXT NOT NULL
);
"""


def clean_filename(filename: str) -> str:
    """Display name for an uploaded file: last path component, NFC-normalized. Metadata only."""
    name = unicodedata.normalize("NFC", filename or "").replace("\\", "/")
    name = PurePosixPath(name).name.strip()
    return name if name not in ("", ".", "..") else "file"


def document_format(filename: str) -> str:
    return _FORMATS.get(PurePosixPath(filename.lower()).suffix, "other")


def media_type_for(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_run_update(current: c.Run, new: c.Run) -> None:
    """Shared save_run rules: finished runs are final and identity fields never change."""
    if current.status in FINISHED_STATUSES:
        raise c.ToolFailure("invalid_reference", f"run {new.run_id} is already {current.status}")
    changed = [f for f in RUN_IDENTITY if getattr(current, f) != getattr(new, f)]
    if changed:
        raise c.ToolFailure("invalid_reference", f"run {new.run_id} cannot change {', '.join(changed)}")


def _owner_lock(path: Path):
    """Try to become the data directory's owning process. Returns (open lock file or None, owner?).

    The lock is released when the file is closed or the process exits, so a crashed server never blocks the
    next start. Where the filesystem does not support locking, this process is treated as the owner."""
    handle = open(path, "a+b")  # noqa: SIM115 - held open for the store's lifetime
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        handle.close()
        return None, False  # another process owns this data directory
    except OSError:
        return handle, True  # locking unsupported here: behave as the single process
    return handle, True


class SqliteStore:
    """Persistent DocumentStore and RunStore. Safe to share between threads of one process."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.data_dir = Path(data_dir)
        self.blob_dir = self.data_dir / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._owner_file, self.is_owner = _owner_lock(self.data_dir / "sverka.lock")
        # Rollback journal (not WAL): works on Docker Desktop bind mounts where WAL shared memory can fail.
        self._db = sqlite3.connect(self.data_dir / "sverka.db", check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA synchronous = FULL")
        self._migrate()
        self.recovered_runs: list[str] = []
        if self.is_owner:  # only the owning process cleans up after a previous one
            self._remove_partial_blobs()
            self.recovered_runs = self._interrupt_unfinished_runs()

    def close(self) -> None:
        with self._lock:
            self._db.close()
            if self._owner_file is not None:
                self._owner_file.close()
                self._owner_file = None

    # ---------- infrastructure ----------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than this application supports ({SCHEMA_VERSION})"
                )
            if version == 0:
                self._db.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")

    def _remove_partial_blobs(self) -> None:
        for tmp in self.blob_dir.glob("*/.*.tmp"):
            tmp.unlink(missing_ok=True)

    def _interrupt_unfinished_runs(self) -> list[str]:
        interrupted = []
        with self._tx() as db:
            rows = db.execute("SELECT data FROM runs WHERE status IN ('queued', 'running') ORDER BY rowid").fetchall()
            for (data,) in rows:
                run = c.Run.model_validate_json(data)
                issue = c.Finding(
                    code="interrupted_by_restart",
                    message="Приложение остановилось до завершения проверки. Запустите проверку снова.",
                )
                run = run.model_copy(
                    update={"status": "interrupted", "updated_at": _now(), "issues": [*run.issues, issue]}
                )
                run = c.Run.model_validate(run.model_dump())
                db.execute(
                    "UPDATE runs SET status = ?, data = ? WHERE run_id = ?",
                    (run.status, run.model_dump_json(), run.run_id),
                )
                interrupted.append(run.run_id)
        return interrupted

    def _blob_path(self, sha: str) -> Path:
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise c.ToolFailure("invalid_reference", "malformed content hash")
        return self.blob_dir / sha[:2] / sha

    def _write_blob(self, data: bytes) -> str:
        sha = _sha(data)
        path = self._blob_path(sha)
        if path.exists():
            try:
                if _sha(path.read_bytes()) == sha:
                    return sha
            except OSError:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{sha}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return sha

    def _read_blob(self, sha: str) -> bytes:
        try:
            data = self._blob_path(sha).read_bytes()
        except FileNotFoundError:
            raise c.ToolFailure("unreadable", "stored file is missing") from None
        if _sha(data) != sha:
            raise c.ToolFailure("unreadable", "stored file failed its integrity check")
        return data

    def _require_workspace(self, db: sqlite3.Connection, workspace_id: str) -> None:
        if db.execute("SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone() is None:
            raise c.ToolFailure("not_found", f"workspace {workspace_id} not found")

    def _one(self, sql: str, args: tuple, missing: str):
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        if row is None:
            raise c.ToolFailure("not_found", missing)
        return row[0]

    # ---------- workspaces ----------
    def create_workspace(self) -> c.Workspace:
        ws = c.Workspace(workspace_id=c.new_id("ws"), created_at=_now())
        with self._tx() as db:
            db.execute("INSERT INTO workspaces VALUES (?, ?)", (ws.workspace_id, ws.model_dump_json()))
        return ws

    def get_workspace(self, workspace_id: str) -> c.Workspace:
        data = self._one(
            "SELECT data FROM workspaces WHERE workspace_id = ?", (workspace_id,), f"workspace {workspace_id} not found"
        )
        return c.Workspace.model_validate_json(data)

    # ---------- documents ----------
    def save_document(
        self, workspace_id: str, *, filename: str, role: str, data: bytes, document_id: str | None = None
    ) -> c.DocumentVersion:
        self.get_workspace(workspace_id)
        name = clean_filename(filename)
        sha = self._write_blob(data)  # before the transaction: a crash leaves only an unreferenced blob
        version_id = c.new_id("ver")
        artifact = c.ArtifactRef(
            workspace_id=workspace_id,
            artifact_id=c.new_id("art"),
            version_id=version_id,
            kind="original",
            media_type=media_type_for(name),
            sha256=sha,
            size_bytes=len(data),
        )
        with self._tx() as db:
            self._require_workspace(db, workspace_id)
            predecessor = None
            if document_id is not None:
                row = db.execute(
                    "SELECT version_id FROM versions WHERE workspace_id = ? AND document_id = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (workspace_id, document_id),
                ).fetchone()
                if row is None:
                    raise c.ToolFailure("not_found", f"document {document_id} not found")
                predecessor = row[0]
            version = c.DocumentVersion(
                workspace_id=workspace_id,
                document_id=document_id or c.new_id("doc"),
                version_id=version_id,
                predecessor_version_id=predecessor,
                sha256=sha,
                size_bytes=len(data),
                role=role,
                original_filename=name,
                format=document_format(name),
                original_artifact_id=artifact.artifact_id,
                created_at=_now(),
            )
            db.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
                (artifact.artifact_id, workspace_id, version_id, sha, artifact.model_dump_json()),
            )
            db.execute(
                "INSERT INTO versions VALUES (?, ?, ?, ?)",
                (version_id, workspace_id, version.document_id, version.model_dump_json()),
            )
        return version

    def get_version(self, workspace_id: str, version_id: str) -> c.DocumentVersion:
        self.get_workspace(workspace_id)
        data = self._one(
            "SELECT data FROM versions WHERE workspace_id = ? AND version_id = ?",
            (workspace_id, version_id),
            f"version {version_id} not found",
        )
        return c.DocumentVersion.model_validate_json(data)

    def list_versions(self, workspace_id: str) -> list[c.DocumentVersion]:
        self.get_workspace(workspace_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM versions WHERE workspace_id = ? ORDER BY rowid", (workspace_id,)
            ).fetchall()
        return [c.DocumentVersion.model_validate_json(r[0]) for r in rows]

    def read_original(self, workspace_id: str, version_id: str) -> bytes:
        version = self.get_version(workspace_id, version_id)
        return self.read_artifact(workspace_id, version.original_artifact_id)[1]

    # ---------- artifacts ----------
    def save_artifact(
        self, workspace_id: str, version_id: str, *, kind: str, media_type: str, data: bytes
    ) -> c.ArtifactRef:
        self.get_version(workspace_id, version_id)
        sha = self._write_blob(data)
        ref = c.ArtifactRef(
            workspace_id=workspace_id,
            artifact_id=c.new_id("art"),
            version_id=version_id,
            kind=kind,
            media_type=media_type,
            sha256=sha,
            size_bytes=len(data),
        )
        with self._tx() as db:
            db.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
                (ref.artifact_id, workspace_id, version_id, sha, ref.model_dump_json()),
            )
        return ref

    def get_artifact(self, workspace_id: str, artifact_id: str) -> c.ArtifactRef:
        self.get_workspace(workspace_id)
        data = self._one(
            "SELECT data FROM artifacts WHERE workspace_id = ? AND artifact_id = ?",
            (workspace_id, artifact_id),
            f"artifact {artifact_id} not found",
        )
        return c.ArtifactRef.model_validate_json(data)

    def read_artifact(self, workspace_id: str, artifact_id: str) -> tuple[c.ArtifactRef, bytes]:
        ref = self.get_artifact(workspace_id, artifact_id)
        return ref, self._read_blob(ref.sha256)

    # ---------- evidence ----------
    def save_blocks(self, workspace_id: str, blocks: Sequence[c.EvidenceBlock]) -> None:
        with self._tx() as db:
            self._require_workspace(db, workspace_id)
            for b in blocks:
                if (
                    db.execute(
                        "SELECT 1 FROM versions WHERE workspace_id = ? AND version_id = ?", (workspace_id, b.version_id)
                    ).fetchone()
                    is None
                ):
                    raise c.ToolFailure("invalid_reference", f"block {b.block_id} names unknown version {b.version_id}")
                if b.locator.kind == "page_region":
                    row = db.execute(
                        "SELECT data FROM artifacts WHERE workspace_id = ? AND artifact_id = ?",
                        (workspace_id, b.locator.render_id),
                    ).fetchone()
                    ref = c.ArtifactRef.model_validate_json(row[0]) if row is not None else None
                    if ref is None or ref.version_id != b.version_id or ref.kind not in RENDER_KINDS:
                        raise c.ToolFailure(
                            "invalid_reference",
                            f"block {b.block_id} must name a page render or crop of version {b.version_id}",
                        )
                row = db.execute(
                    "SELECT data FROM blocks WHERE workspace_id = ? AND block_id = ?", (workspace_id, b.block_id)
                ).fetchone()
                if row is not None:
                    if c.EvidenceBlock.model_validate_json(row[0]) != b:
                        raise c.ToolFailure(
                            "invalid_reference", f"block {b.block_id} already exists with other content"
                        )
                    continue
                db.execute(
                    "INSERT INTO blocks VALUES (?, ?, ?, ?)",
                    (workspace_id, b.block_id, b.version_id, b.model_dump_json()),
                )

    def get_block(self, workspace_id: str, block_id: str) -> c.EvidenceBlock:
        self.get_workspace(workspace_id)
        data = self._one(
            "SELECT data FROM blocks WHERE workspace_id = ? AND block_id = ?",
            (workspace_id, block_id),
            f"evidence {block_id} not found",
        )
        return c.EvidenceBlock.model_validate_json(data)

    # ---------- runs ----------
    def create_run(self, workspace_id: str, request: c.StartRunRequest) -> c.Run:
        now = _now()
        with self._tx() as db:
            self._require_workspace(db, workspace_id)
            for vid in request.version_ids:
                if (
                    db.execute(
                        "SELECT 1 FROM versions WHERE workspace_id = ? AND version_id = ?", (workspace_id, vid)
                    ).fetchone()
                    is None
                ):
                    raise c.ToolFailure("invalid_reference", f"version {vid} is not in workspace {workspace_id}")
            if request.predecessor_run_id is not None and (
                db.execute(
                    "SELECT 1 FROM runs WHERE workspace_id = ? AND run_id = ?",
                    (workspace_id, request.predecessor_run_id),
                ).fetchone()
                is None
            ):
                raise c.ToolFailure("not_found", f"run {request.predecessor_run_id} not found")
            run = c.Run(
                workspace_id=workspace_id,
                run_id=c.new_id("run"),
                predecessor_run_id=request.predecessor_run_id,
                input_version_ids=list(request.version_ids),
                scope=request.scope.model_copy(deep=True),  # the returned run must not share the caller's scope
                status="queued",
                created_at=now,
                updated_at=now,
            )
            try:
                db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?)",
                    (run.run_id, workspace_id, run.status, run.model_dump_json()),
                )
            except sqlite3.IntegrityError:
                raise ActiveRunConflict(f"workspace {workspace_id} already has an active check") from None
        return run

    def get_run(self, workspace_id: str, run_id: str) -> c.Run:
        self.get_workspace(workspace_id)
        data = self._one(
            "SELECT data FROM runs WHERE workspace_id = ? AND run_id = ?",
            (workspace_id, run_id),
            f"run {run_id} not found",
        )
        return c.Run.model_validate_json(data)

    def list_runs(self, workspace_id: str) -> list[c.Run]:
        self.get_workspace(workspace_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM runs WHERE workspace_id = ? ORDER BY rowid", (workspace_id,)
            ).fetchall()
        return [c.Run.model_validate_json(r[0]) for r in rows]

    def save_run(self, run: c.Run) -> None:
        run = c.Run.model_validate(run.model_dump())  # model_copy skips validation; check before writing
        with self._tx() as db:
            self._require_workspace(db, run.workspace_id)
            row = db.execute(
                "SELECT data FROM runs WHERE workspace_id = ? AND run_id = ?", (run.workspace_id, run.run_id)
            ).fetchone()
            if row is None:
                raise c.ToolFailure("not_found", f"run {run.run_id} not found")
            check_run_update(c.Run.model_validate_json(row[0]), run)
            db.execute(
                "UPDATE runs SET status = ?, data = ? WHERE run_id = ?", (run.status, run.model_dump_json(), run.run_id)
            )

    # ---------- conversation ----------
    def list_messages(self, workspace_id: str) -> list[c.ChatMessage]:
        self.get_workspace(workspace_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM messages WHERE workspace_id = ? ORDER BY seq", (workspace_id,)
            ).fetchall()
        return [c.ChatMessage.model_validate_json(r[0]) for r in rows]

    def append_message(self, message: c.ChatMessage) -> None:
        with self._tx() as db:
            self._require_workspace(db, message.workspace_id)
            if message.run_id is not None and (
                db.execute(
                    "SELECT 1 FROM runs WHERE workspace_id = ? AND run_id = ?", (message.workspace_id, message.run_id)
                ).fetchone()
                is None
            ):
                raise c.ToolFailure("not_found", f"run {message.run_id} not found")
            try:
                db.execute(
                    "INSERT INTO messages (message_id, workspace_id, run_id, data) VALUES (?, ?, ?, ?)",
                    (message.message_id, message.workspace_id, message.run_id, message.model_dump_json()),
                )
            except sqlite3.IntegrityError:
                raise c.ToolFailure("invalid_reference", f"message {message.message_id} already exists") from None
