"""Командная строка: ``python -m sverka check <папка или zip> ...`` и ``python -m sverka serve``.

``check`` выполняет ту же полную проверку, что и веб-интерфейс (``run_service.RunService``): подготовка
документов, модель с инструментами, валидация, сверка. Нужны настройки ``SVERKA_PROVIDER_*``. Прежний
автономный расчет по правилам из командной строки отключен: он обходил модель и валидацию.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

from . import __version__, config
from . import contracts as c
from .money import fmt

_REPORT_NAME = re.compile(r"карточк|кфз", re.I)
_CONTEXT_NAME = re.compile(r"readme", re.I)
_JUNK = re.compile(r"^(~\$|\.)|^thumbs\.db$|^desktop\.ini$", re.I)
_STATUS = {"verified": "подтверждено", "discrepancy": "расхождение", "insufficient_evidence": "нет данных"}


def _date(s: str):
    for f in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"дата должна быть в формате ДД.ММ.ГГГГ: {s}")


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _input_files(source: str) -> list[tuple[str, bytes]]:
    """(имя, содержимое) каждого файла набора: папка (без вложенных папок) или zip-архив."""
    if os.path.isdir(source):
        names = sorted(n for n in os.listdir(source) if os.path.isfile(os.path.join(source, n)) and not _JUNK.match(n))
        return [(n, _read(os.path.join(source, n))) for n in names]
    if not os.path.isfile(source):
        raise ValueError(f"не папка и не zip-архив: {source}")
    from .api import ApiError, _unpack_zip  # тот же разбор архива, что при загрузке: лимиты, имена cp866

    with open(source, "rb") as f:
        data = f.read((config.MAX_UPLOAD_MB * 1024 * 1024) + 1)
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"архив больше {config.MAX_UPLOAD_MB} МБ")
    try:
        return _unpack_zip(data, os.path.basename(source))
    except ApiError as exc:
        raise ValueError(exc.error.message) from None


def _roles(names: list[str], report: str | None, context: list[str]) -> dict[str, str]:
    """Роль каждого файла. Отчет задается ``--report`` или определяется по имени, если такой файл один."""
    if report is None:
        candidates = [n for n in names if _REPORT_NAME.search(n)]
        if len(candidates) != 1:
            raise ValueError("укажите файл отчета параметром --report (по именам файлов он не определяется однозначно)")
        report = candidates[0]
    elif report not in names:
        raise ValueError(f"файл отчета не найден в наборе: {report}")
    roles = {}
    for name in names:
        is_context = name in context or (not context and _CONTEXT_NAME.search(name))
        roles[name] = "report" if name == report else "context" if is_context else "primary"
    return roles


def check(a) -> int:
    from .run_service import RunService
    from .storage import SqliteStore

    scope = c.Scope(contract=a.contract, report_row=a.row, date_from=a.period_from, date_to=a.period_to, confirmed=True)
    say = (lambda m: None) if a.quiet else (lambda m: print("  " + m, file=sys.stderr, flush=True))
    files = _input_files(a.input)
    if not files:
        raise ValueError(f"в наборе нет файлов: {a.input}")
    if len({n for n, _ in files}) != len(files):
        raise ValueError("в наборе есть файлы с одинаковыми именами; загрузите их через веб-интерфейс")
    roles = _roles([n for n, _ in files], a.report, a.context or [])
    store = SqliteStore(a.data or config.DATA_DIR)
    try:
        workspace = store.create_workspace()
        versions = [store.save_document(workspace.workspace_id, filename=n, role=roles[n], data=d) for n, d in files]
        say(f"Загружено файлов: {len(versions)}; рабочая область {workspace.workspace_id}")
        run = store.create_run(
            workspace.workspace_id, c.StartRunRequest(version_ids=[v.version_id for v in versions], scope=scope)
        )
        service = RunService(store, trace_dir=os.path.join(a.data or config.DATA_DIR, "traces"))
        say("Идет проверка: подготовка документов, модель, валидация, сверка...")
        run = service.execute(run)
    finally:
        store.close()
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "run.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
    for issue in run.issues:
        print(f"ЗАМЕЧАНИЕ [{issue.code}]: {issue.message}", file=sys.stderr)
    if run.result is None:
        reason = run.failure.message if run.failure else run.status
        print(f"\nПРОВЕРКА НЕ ВЫПОЛНЕНА: {reason}\nСостояние: {path}", file=sys.stderr)
        return 2
    result = run.result
    print(f"\nЗАКЛЮЧЕНИЕ: {result.conclusion.summary}")
    print(f"\n{'Гр.':>3}  {'Статус':<13} {'В отчете':>16} {'По документам':>16} {'Расхождение':>14}  Показатель")
    for i in result.indicators:
        column = "" if i.column is None else i.column
        print(
            f"{column:>3}  {_STATUS[i.status]:<13} {_money(i.reported_value):>16} {_money(i.expected_value):>16} "
            f"{_money(i.diff):>14}  {i.label}"
        )
    for e in result.exclusions:
        print(f"Не учтен: {e.label} — {e.explanation}")
    for u in result.unresolved:
        print(f"Не разобран: {u.label} — {u.reason}")
    print(f"\nРезультат: {path}")
    # Код возврата: 0 - все показатели подтверждены, 1 - есть расхождения или непроверенные показатели,
    # 2 - проверка не выполнена.
    return 0 if result.indicators and all(i.status == "verified" for i in result.indicators) else 1


def _money(value) -> str:
    return "—" if value is None else fmt(value)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sverka", description="Сверка итогового отчета (КФЗ) с первичными документами")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="проверить набор документов (нужны настройки SVERKA_PROVIDER_*)")
    p.add_argument("input", help="папка или zip-архив с отчетом и первичными документами")
    p.add_argument("--contract", required=True, help="номер договора, например 24/03/15-01")
    p.add_argument("--row", required=True, help="проверяемая строка отчета, например «3 этап, 2026 г.»")
    p.add_argument("--period-from", type=_date, required=True, help="начало периода, ДД.ММ.ГГГГ")
    p.add_argument("--period-to", type=_date, required=True, help="конец периода, ДД.ММ.ГГГГ")
    p.add_argument("--report", help="имя файла итогового отчета (если не определяется по имени)")
    p.add_argument("--context", action="append", help="имя файла-описания, который не проверяется (можно повторять)")
    p.add_argument("--out", default="out", help="папка для результата (по умолчанию ./out)")
    p.add_argument("--data", help="каталог хранилища (по умолчанию SVERKA_DATA_DIR)")
    p.add_argument("--quiet", action="store_true", help="не печатать ход обработки")
    s = sub.add_parser("serve", help="запустить веб-интерфейс")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("sverka.web:app", host=a.host, port=a.port, log_level="info")
        return 0
    try:
        return check(a)
    except Exception as exc:  # любая ошибка означает, что проверка не выполнена: код 2, а не 1
        text = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
        print(f"ОШИБКА: {text}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
