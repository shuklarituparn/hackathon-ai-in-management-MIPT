"""Веб-интерфейс и HTTP API (FastAPI)."""
from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, api, config, ocr
from .contracts import CONTRACT_VERSION
from .engine import run_check
from .render import to_json, write_all

app = FastAPI(title="Сверка КФЗ с первичными документами", version=__version__)
JOBS = os.path.join(config.DATA_DIR, "jobs")
EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "case19")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Assets of the new chat interface (T09). The legacy upload page stays at "/" until it is replaced.
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# Workspace, document, run, conversation and evidence API (T02); storage is created on first use.
api.install(app)

_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Сверка отчета с первичными документами</title><style>
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2328;max-width:820px;margin:40px auto;padding:0 20px}
h1{font-size:24px}form{border:1px solid #d8dee4;border-radius:8px;padding:20px;margin:16px 0;background:#f6f8fa}
label{display:block;margin:10px 0 4px;font-weight:600}input[type=text]{width:100%;padding:7px;border:1px solid #d8dee4;border-radius:6px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}button{margin-top:16px;padding:9px 18px;border:0;border-radius:6px;background:#1f6feb;color:#fff;font-size:15px;cursor:pointer}
button.sec{background:#59636e}.mute{color:#59636e;font-size:13px}#wait{display:none;margin-top:12px}
</style></head><body>
<h1>Сверка итогового отчета с первичными документами</h1>
<p>Загрузите итоговый отчет (карточку учета фактических затрат) и первичные документы: отдельными файлами или одним zip-архивом.
Поддерживаются PDF, сканы, PNG/JPG, DOCX, XLSX. Обработка выполняется локально, данные не передаются внешним сервисам.</p>
<form action="/check" method="post" enctype="multipart/form-data" onsubmit="document.getElementById('wait').style.display='block'">
<label>Файлы набора или zip-архив</label><input type="file" name="files" multiple required>
<div class="mute">Параметры ниже необязательны: по умолчанию они берутся из файла описания набора (README) или из шапки отчета.</div>
<div class="row"><div><label>Номер договора</label><input type="text" name="contract" placeholder="24/03/15-01"></div>
<div><label>Проверяемая строка отчета</label><input type="text" name="row" placeholder="3 этап, 2026 г."></div></div>
<div class="row"><div><label>Начало периода</label><input type="text" name="period_from" placeholder="01.01.2026"></div>
<div><label>Конец периода</label><input type="text" name="period_to" placeholder="31.05.2026"></div></div>
<button type="submit">Проверить</button>
<div id="wait" class="mute">Идет распознавание и сверка, обычно это занимает 15-40 секунд...</div></form>
__DEMO__
<p class="mute">API: <code>POST /api/check</code> (multipart, поле <code>files</code>) возвращает результат в JSON. Состояние сервиса: <a href="/health">/health</a>. Версия __VER__.</p>
</body></html>"""
_DEMO = """<form action="/demo" method="post" onsubmit="document.getElementById('wait2').style.display='block'">
<b>Демонстрационный набор</b><div class="mute">Кейс из задания: карточка затрат за 3 этап и 22 первичных документа.</div>
<button class="sec" type="submit">Запустить на демонстрационном наборе</button>
<div id="wait2" class="mute" style="display:none;margin-top:12px">Идет обработка...</div></form>"""


def _parse_date(s: str | None):
    if not s or not s.strip():
        return None
    for f in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), f).date()
        except ValueError:
            pass
    raise HTTPException(400, f"Дата должна быть в формате ДД.ММ.ГГГГ: {s}")


def _save_uploads(files: list[UploadFile], dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    limit, total = config.MAX_UPLOAD_MB * 1024 * 1024, 0
    for up in files:
        name = os.path.basename((up.filename or "file").replace("\\", "/")) or "file"
        with open(os.path.join(dest, name), "wb") as out:
            while chunk := up.file.read(1 << 20):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, f"Размер загрузки превышает {config.MAX_UPLOAD_MB} МБ")
                out.write(chunk)


def _run(src: str, job: str, **kw) -> dict:
    # Один zip-архив передаем как архив, несколько файлов как папку.
    names = os.listdir(src)
    target = os.path.join(src, names[0]) if len(names) == 1 and names[0].lower().endswith(".zip") else src
    run = run_check(target, **kw)
    write_all(run, os.path.join(JOBS, job, "out"))
    if run["ctx"].get("tmp"):
        shutil.rmtree(run["ctx"]["tmp"], ignore_errors=True)
    return run


@app.get("/", response_class=HTMLResponse)
def index():
    return _PAGE.replace("__DEMO__", _DEMO if os.path.isdir(EXAMPLE) else "").replace("__VER__", __version__)


@app.get("/app", response_class=HTMLResponse)
def chat_app():
    return FileResponse(os.path.join(STATIC, "index.html"), media_type="text/html")


def _data_dir_writable() -> bool:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
    except OSError:
        return False
    return os.access(config.DATA_DIR, os.W_OK)


def _provider_state() -> dict:
    """Whether the model provider of the live check can be called. Names of missing settings only, no values."""
    from .model_adapter import ProviderConfig

    try:
        cfg = ProviderConfig.from_env()
    except ValueError as exc:
        return {"configured": False, "problem": str(exc)}
    missing = cfg.missing()
    return {"configured": not missing, "name": cfg.name, "model": cfg.model, "missing": missing}


@app.get("/health")
def health():
    # Configuration state only: never return URLs, keys or other values that may carry credentials.
    return {"status": "ok", "version": __version__, "contracts": CONTRACT_VERSION, "ocr": ocr.available(),
            "llm_configured": bool(config.LLM_URL), "data_dir_writable": _data_dir_writable(),
            "run_mode": config.RUN_MODE, "provider": _provider_state(), "demo": os.path.isdir(EXAMPLE)}


@app.post("/check")
def check(files: list[UploadFile] = File(...), contract: str = Form(""), row: str = Form(""),
          period_from: str = Form(""), period_to: str = Form("")):
    job = uuid.uuid4().hex[:12]
    src = os.path.join(JOBS, job, "input")
    _save_uploads(files, src)
    _run(src, job, contract=contract.strip() or None, row=row.strip() or None,
         period_from=_parse_date(period_from), period_to=_parse_date(period_to))
    return RedirectResponse(f"/jobs/{job}", status_code=303)


@app.post("/demo")
def demo():
    if not os.path.isdir(EXAMPLE):
        raise HTTPException(404, "Демонстрационный набор не найден")
    job = uuid.uuid4().hex[:12]
    os.makedirs(os.path.join(JOBS, job), exist_ok=True)
    run = run_check(EXAMPLE)
    write_all(run, os.path.join(JOBS, job, "out"))
    return RedirectResponse(f"/jobs/{job}", status_code=303)


@app.post("/api/check")
def api_check(files: list[UploadFile] = File(...), contract: str = Form(""), row: str = Form(""),
              period_from: str = Form(""), period_to: str = Form("")):
    job = uuid.uuid4().hex[:12]
    src = os.path.join(JOBS, job, "input")
    _save_uploads(files, src)
    run = _run(src, job, contract=contract.strip() or None, row=row.strip() or None,
               period_from=_parse_date(period_from), period_to=_parse_date(period_to))
    run["result"]["meta"]["job"] = job
    return Response(to_json(run["result"]), media_type="application/json")


def _job_file(job: str, name: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", job):
        raise HTTPException(404, "Задание не найдено")
    path = os.path.join(JOBS, job, "out", name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Файл не найден")
    return path


@app.get("/jobs/{job}", response_class=HTMLResponse)
def job_page(job: str):
    html = open(_job_file(job, "report.html"), encoding="utf-8").read()
    bar = (f'<div style="background:#1f2328;color:#fff;padding:10px 24px;font:14px sans-serif">'
           f'<a style="color:#fff;margin-right:20px" href="/">Новая проверка</a>'
           f'<a style="color:#9cd0ff;margin-right:20px" href="/jobs/{job}/result.json">Скачать JSON</a>'
           f'<a style="color:#9cd0ff;margin-right:20px" href="/jobs/{job}/report.xlsx">Скачать XLSX</a>'
           f'<a style="color:#9cd0ff" href="/jobs/{job}/report.html" download>Скачать HTML</a></div>')
    return html.replace("<body>", "<body>" + bar, 1)


@app.get("/jobs/{job}/{name}")
def job_download(job: str, name: str):
    if name not in ("result.json", "report.xlsx", "report.html"):
        raise HTTPException(404, "Файл не найден")
    return FileResponse(_job_file(job, name), filename=name)


@app.exception_handler(Exception)
async def on_error(request, exc):   # любые непредвиденные ошибки отдаем понятным сообщением
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
