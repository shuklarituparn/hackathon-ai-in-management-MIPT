"""Вывод результата: JSON, XLSX, HTML и вырезки подтверждающих фрагментов."""
from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

from PIL import Image, ImageDraw

from .money import fmt, fmt_date

STATUS_COLOR = {"ОК": "#1a7f37", "РАСХОЖДЕНИЕ": "#c62828", "НЕПРОВЕРЕНО": "#9a6700"}


# ---------------- JSON ----------------
def _default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, default=_default)


# ---------------- вырезки ----------------
def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", name)[:80]


def _crop(img: Image.Image, bbox, pad_y: int, max_w: int = 1000, full_width: bool = True) -> Image.Image:
    x0, y0, x1, y1 = [int(v) for v in bbox]
    W, H = img.size
    cx0, cx1 = (0, W) if full_width else (max(0, x0 - 40), min(W, x1 + 40))
    cy0, cy1 = max(0, y0 - pad_y), min(H, y1 + pad_y)
    out = img.crop((cx0, cy0, cx1, cy1)).convert("RGB")
    d = ImageDraw.Draw(out)
    d.rectangle([x0 - cx0 - 6, y0 - cy0 - 5, x1 - cx0 + 6, y1 - cy0 + 5], outline=(214, 40, 40), width=4)
    if out.width > max_w:
        k = max_w / out.width
        out = out.resize((max_w, max(1, int(out.height * k))), Image.LANCZOS)
    return out


def _doc_fragment_image(dc, loc: dict):
    """Изображение фрагмента документа с рамкой вокруг подтверждающей строки."""
    t = loc.get("type")
    bbox = loc.get("bbox")
    if not bbox:
        return None
    try:
        if t == "scan":
            img = dc.page_images.get(loc.get("page", 1))
            if img is None:
                return None
            h = bbox[3] - bbox[1]
            return _crop(img, bbox, pad_y=int(h * 5))
        if t == "pdf":
            import pdfplumber
            with pdfplumber.open(dc.path) as pdf:
                page = pdf.pages[loc.get("page", 1) - 1]
                k = 130 / 72.0
                img = page.to_image(resolution=130).original
            b = [v * k for v in bbox]
            return _crop(img, b, pad_y=int((b[3] - b[1]) * 5))
    except Exception:
        return None
    return None


def make_evidence(run: dict, out_dir: str | None) -> dict[str, bytes]:
    """Создает вырезки для сумм документов и строки отчета. Возвращает {имя файла: PNG}."""
    result, ctx = run["result"], run["ctx"]
    images: dict[str, bytes] = {}

    def put(name: str, img) -> str | None:
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        images[name] = buf.getvalue()
        return name

    by_file = {d.file: d for d in ctx.get("docs", [])}
    for dd in result["documents"]:
        d, dc = by_file.get(dd["file"]), ctx["contents"].get(dd["file"])
        if d is None or dc is None:
            continue
        ev = d.evidence.get("amount")
        if ev is None:
            continue
        name = put(f"{_safe(os.path.splitext(d.file)[0])}__сумма.png", _doc_fragment_image(dc, ev.loc))
        ev.image = name
        if "amount" in dd["evidence"]:
            dd["evidence"]["amount"]["image"] = name
    for ind in result["indicators"]:
        for s in ind["sources"]:
            d = by_file.get(s["file"])
            if d is not None and d.evidence.get("amount") is not None and s.get("image") is None:
                if ind["role"] == "article":
                    s["image"] = d.evidence["amount"].image
    # Строка отчета: шапка таблицы и проверяемая строка с рамкой.
    rep, row = ctx.get("report"), ctx.get("target_row")
    if rep is not None and rep.image is not None and row is not None and row.bbox:
        tb = rep.meta.get("_table_bbox") or row.bbox
        img = rep.image.convert("RGB")
        x0, y0, x1, y1 = [int(v) for v in tb]
        crop = img.crop((max(0, x0 - 10), max(0, y0 - 10), min(img.width, x1 + 10), min(img.height, y1 + 10)))
        d = ImageDraw.Draw(crop)
        rb = row.bbox
        d.rectangle([rb[0] - x0 + 10 - 2, rb[1] - y0 + 10 - 1, rb[2] - x0 + 10 + 2, rb[3] - y0 + 10 + 1],
                    outline=(214, 40, 40), width=3)
        if crop.width < 2200:
            k = 2200 / crop.width
            crop = crop.resize((2200, int(crop.height * k)), Image.LANCZOS)
        result["params"]["report_image"] = put("отчет__проверяемая_строка.png", crop)
    if out_dir:
        ev_dir = os.path.join(out_dir, "evidence")
        os.makedirs(ev_dir, exist_ok=True)
        for name, data in images.items():
            with open(os.path.join(ev_dir, name), "wb") as f:
                f.write(data)
    return images


# ---------------- XLSX ----------------
def to_xlsx(result: dict, path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    bold, wrap = Font(bold=True), Alignment(wrap_text=True, vertical="top")
    fills = {"ОК": "C6EFCE", "РАСХОЖДЕНИЕ": "FFC7CE", "НЕПРОВЕРЕНО": "FFEB9C"}

    def sheet(title, header, rows, widths, status_col=None):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill, c.alignment = bold, PatternFill("solid", fgColor="D9E1F2"), wrap
        for r in rows:
            ws.append([float(v) if isinstance(v, Decimal) else (fmt_date(v) if isinstance(v, date) else v) for v in r])
            for c in ws[ws.max_row]:
                c.alignment = wrap
                if isinstance(c.value, float):
                    c.number_format = "#,##0.00"
            if status_col is not None:
                cell = ws.cell(ws.max_row, status_col)
                if cell.value in fills:
                    cell.fill = PatternFill("solid", fgColor=fills[cell.value])
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        return ws

    p, v = result["params"], result["verdict"]
    ws = wb.active
    ws.title = "Заключение"
    rows = [("Заключение", v.get("status", "")), ("Отчет", p.get("report_file", "")),
            ("Проверяемая строка", p.get("row", "")), ("Договор", p.get("contract", "")),
            ("Период", f"{fmt_date(p.get('period_from'))} - {fmt_date(p.get('period_to'))}" if p.get("period_from") else ""),
            ("Сформировано", result["meta"].get("generated_at", ""))]
    for k, val in rows:
        ws.append([k, val])
        ws.cell(ws.max_row, 1).font = bold
    ws.append([])
    ws.append(["Сводка"])
    ws.cell(ws.max_row, 1).font = bold
    for line in v.get("summary", []):
        ws.append(["", line])
        ws.cell(ws.max_row, 2).alignment = wrap
    for e in result.get("errors", []):
        ws.append(["Ошибка", e])
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 130

    sheet("Показатели",
          ["Графа", "Показатель", "Значение в отчете", "Ожидаемо по документам", "Расхождение", "Статус",
           "Категория", "Пояснение", "Документы-источники"],
          [(i["column"], i["name"], i["report_value"], i["expected_value"], i["diff"], i["status"], i["category"],
            i["explanation"], "; ".join(s["file"] for s in i["sources"] if s["file"])) for i in result["indicators"]],
          [8, 46, 18, 20, 16, 16, 24, 90, 60], status_col=6)
    src_rows = []
    for i in result["indicators"]:
        for s in i["sources"]:
            if s["file"]:
                src_rows.append((i["column"], i["name"], s["file"], s["doc_type"], s["number"], s["date"] or s["period"],
                                 s["amount"], s["fragment"], s["where"]))
    sheet("Источники", ["Графа", "Показатель", "Файл", "Тип", "Номер", "Дата / период", "Сумма",
                        "Подтверждающий фрагмент", "Где находится"], src_rows, [8, 40, 40, 20, 16, 16, 16, 70, 46])
    sheet("Документы",
          ["Файл", "Тип", "Номер", "Дата", "Период", "Договор", "Сумма", "Статья затрат", "Уверенность статьи",
           "Статус", "Причина", "Фрагмент с суммой", "Где находится", "OCR"],
          [(d["file"], d["doc_type"], d["number"], d["date"], d["period"], ", ".join(d["contracts"]), d["amount"],
            d["article"], d["article_confidence"], d["status"], d["reason"],
            d["evidence"].get("amount", {}).get("text", ""), d["evidence"].get("amount", {}).get("where", ""),
            "да" if d["ocr"] else "") for d in result["documents"]],
          [40, 20, 16, 12, 16, 16, 16, 30, 10, 16, 70, 60, 46, 6])
    sheet("Замечания", ["Категория", "Важность", "Файлы", "Описание"],
          [(x["category"], x["severity"], ", ".join(x["files"]), x["text"]) for x in result["issues"]],
          [28, 12, 40, 120])
    sheet("Арифметика отчета", ["Проверка", "Статус", "Левая часть", "Правая часть", "Примечание"],
          [(c["check"], c["status"], c["left"], c["right"], c["note"]) for c in result["report_checks"]],
          [90, 14, 18, 18, 60])
    wb.save(path)


# ---------------- HTML ----------------
_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Сверка отчета: {{ p.report_file }}</title>
<style>
:root{--ok:#1a7f37;--bad:#c62828;--unk:#9a6700;--line:#d8dee4;--bg:#f6f8fa;--ink:#1f2328;--mute:#59636e}
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:var(--ink);margin:0;background:#fff}
main{max-width:1280px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
.mute{color:var(--mute)}.verdict{border-radius:8px;padding:16px 20px;margin:16px 0;color:#fff}
.verdict h3{margin:0 0 8px;font-size:19px;letter-spacing:.3px}.verdict ul{margin:0;padding-left:20px}
.v-bad{background:var(--bad)}.v-ok{background:var(--ok)}.v-unk{background:var(--unk)}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.card{flex:1 1 150px;border:1px solid var(--line);border-radius:8px;padding:10px 14px;background:var(--bg)}
.card b{display:block;font-size:22px}.params{display:grid;grid-template-columns:220px 1fr;gap:4px 16px;margin:8px 0}
table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}
th{background:var(--bg);font-weight:600}td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.st{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px;font-weight:600;white-space:nowrap}
.st-ОК{background:var(--ok)}.st-РАСХОЖДЕНИЕ{background:var(--bad)}.st-НЕПРОВЕРЕНО{background:var(--unk)}
tr.sub td{color:var(--mute)}tr.main td:nth-child(2){font-weight:600}
details{margin:6px 0}summary{cursor:pointer;color:#0969da}
.frag{background:var(--bg);border-left:3px solid #0969da;padding:4px 8px;margin:4px 0;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;white-space:pre-wrap}
img.ev{max-width:100%;border:1px solid var(--line);border-radius:4px;margin:4px 0;display:block}
.tag{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 8px;font-size:12px;background:var(--bg);white-space:nowrap}
.doc-учтен{color:var(--ok)}.doc-x{color:var(--bad);font-weight:600}.small{font-size:12.5px}
.scroll{overflow-x:auto}
</style></head><body><main>
<h1>Сверка итогового отчета с первичными документами</h1>
<div class="mute">{{ p.report_title or p.report_file }} · сформировано {{ meta.generated_at }} · версия {{ meta.version }}</div>

<div class="verdict {{ vclass }}"><h3>{{ v.status }}</h3><ul>{% for l in v.summary %}<li>{{ l }}</li>{% endfor %}</ul></div>
{% if errors %}<div class="frag" style="border-color:var(--bad)">{% for e in errors %}{{ e }}
{% endfor %}</div>{% endif %}

<div class="cards">
<div class="card"><b style="color:var(--ok)">{{ v.counts.get('ОК',0) }}</b>показателей подтверждено</div>
<div class="card"><b style="color:var(--bad)">{{ v.counts.get('РАСХОЖДЕНИЕ',0) }}</b>с расхождением</div>
<div class="card"><b style="color:var(--unk)">{{ v.counts.get('НЕПРОВЕРЕНО',0) }}</b>не проверено</div>
<div class="card"><b>{{ docs|length }}</b>первичных документов</div>
<div class="card"><b>{{ docs|selectattr('status','equalto','учтен')|list|length }}</b>учтено в расчете</div>
</div>

<h2>Параметры проверки</h2>
<div class="params">
<div class="mute">Итоговый отчет</div><div>{{ p.report_file }} ({{ p.report_schema }}, {{ p.report_source }})</div>
<div class="mute">Проверяемая строка</div><div>{{ p.row }}</div>
<div class="mute">Договор</div><div>{{ p.contract }}</div>
<div class="mute">Период первичных документов</div><div>{{ d(p.period_from) }} - {{ d(p.period_to) }} <span class="mute">({{ p.period_source }})</span></div>
<div class="mute">Допуск на округление</div><div>{{ m(p.tolerance) }} руб.</div>
{% for k, val in (p.report_meta or {}).items() %}<div class="mute">{{ k }} (из шапки отчета)</div><div>{{ m(val) }} руб.</div>{% endfor %}
</div>
{% if p.report_image %}<details open><summary>Строка отчета, как она распознана</summary><img class="ev" data-ev="{{ p.report_image }}" alt="строка отчета"></details>{% endif %}

<h2>Показатели отчета</h2>
<div class="scroll"><table>
<tr><th>Гр.</th><th>Показатель</th><th>В отчете</th><th>Ожидаемо по документам</th><th>Расхождение</th><th>Статус</th><th>Категория</th><th>Пояснение и источники</th></tr>
{% for i in inds %}
<tr class="{{ 'main' if i.role in ('article','total') else 'sub' }}">
<td>{{ i.column }}</td><td>{{ i.name }}</td><td class="num">{{ m(i.report_value) }}</td><td class="num">{{ m(i.expected_value) }}</td>
<td class="num">{{ m(i.diff) if i.diff is not none and i.diff != 0 else ('0,00' if i.diff is not none else '') }}</td>
<td><span class="st st-{{ i.status }}">{{ i.status }}</span></td><td>{{ i.category }}</td>
<td>{{ i.explanation }}{% if i.report_note %}<div class="small mute">Отчет: {{ i.report_note }}</div>{% endif %}
{% if i.sources %}<details {{ 'open' if i.status == 'РАСХОЖДЕНИЕ' and i.role == 'article' else '' }}><summary>Источники: {{ i.sources|length }}</summary>
{% for s in i.sources %}<div class="small"><b>{{ s.file or s.fragment }}</b>{% if s.number %} · {{ s.doc_type }} {{ s.number }}{% endif %}{% if s.date or s.period %} · {{ s.date or s.period }}{% endif %} · <b>{{ m(s.amount) }}</b>
{% if s.file %}<div class="frag">{{ s.fragment }}</div><span class="mute">{{ s.where }}</span>{% if s.image %}<img class="ev" data-ev="{{ s.image }}" alt="фрагмент">{% endif %}{% endif %}</div>{% endfor %}</details>{% endif %}
{% if i.excluded %}<details><summary>Не учтены: {{ i.excluded|length }}</summary>{% for e in i.excluded %}<div class="small"><b>{{ e.file }}</b> · <span class="doc-x">{{ e.status }}</span>{% if e.amount is not none %} · {{ m(e.amount) }}{% endif %}<br><span class="mute">{{ e.reason }}</span></div>{% endfor %}</details>{% endif %}
</td></tr>{% endfor %}
</table></div>

<h2>Первичные документы</h2>
<div class="scroll"><table>
<tr><th>Файл</th><th>Тип, номер</th><th>Дата / период</th><th>Договор</th><th>Сумма</th><th>Статья затрат</th><th>Статус</th><th>Обоснование и фрагмент</th></tr>
{% for x in docs %}<tr>
<td>{{ x.file }}{% if x.ocr %} <span class="tag">OCR {{ x.ocr_confidence|round|int if x.ocr_confidence else '' }}%</span>{% endif %}</td>
<td>{{ x.doc_type }}{% if x.number %} № {{ x.number }}{% endif %}</td>
<td>{{ d(x.date) }}{% if x.period %}<div class="small mute">{{ x.period }}</div>{% endif %}</td>
<td>{{ x.contracts|join(', ') }}</td><td class="num">{{ m(x.amount) }}</td>
<td>{{ x.article }}{% if x.article %}<div class="small mute">уверенность {{ '%.0f'|format(x.article_confidence*100) }}%</div>{% endif %}</td>
<td><span class="{{ 'doc-учтен' if x.status == 'учтен' else 'doc-x' }}">{{ x.status }}</span></td>
<td class="small">{{ x.reason }}
{% set ev = x.evidence.get('amount') %}{% if ev %}<details><summary>Фрагмент с суммой</summary><div class="frag">{{ ev.text }}</div><span class="mute">{{ ev.where }}</span>{% if ev.image %}<img class="ev" data-ev="{{ ev.image }}" alt="фрагмент">{% endif %}
{% for k in ('number','date','period','contract') %}{% if x.evidence.get(k) %}<div><span class="mute">{{ x.evidence[k].what }}:</span> {{ x.evidence[k].text }} <span class="mute">({{ x.evidence[k].where }})</span></div>{% endif %}{% endfor %}</details>{% endif %}
{% for w in x.warnings %}<div class="mute">{{ w }}</div>{% endfor %}</td></tr>{% endfor %}
</table></div>

{% if issues %}<h2>Замечания</h2>
<table><tr><th>Категория</th><th>Описание</th></tr>
{% for x in issues %}<tr><td><span class="tag">{{ x.category }}</span></td><td>{{ x.text }}</td></tr>{% endfor %}</table>{% endif %}

{% if checks %}<h2>Внутренняя арифметика отчета</h2>
<details><summary>Проверок: {{ checks|length }}, нарушений: {{ checks|selectattr('status','equalto','НАРУШЕНО')|list|length }}</summary>
<table><tr><th>Проверка</th><th>Статус</th><th>Левая часть</th><th>Правая часть</th></tr>
{% for c in checks %}<tr><td>{{ c.check }}</td><td>{{ c.status }}</td><td class="num">{{ m(c.left) }}</td><td class="num">{{ m(c.right) }}</td></tr>{% endfor %}</table></details>{% endif %}

{% if rows %}<h2>Все строки отчета</h2><div class="scroll"><table>
<tr><th>Строка</th>{% for c in cols %}<th>{{ c }}</th>{% endfor %}</tr>
{% for r in rows %}<tr {{ 'style="background:#fff8c5"' if r.is_target else '' }}><td>{{ r.label }}</td>{% for c in cols %}<td class="num">{{ m(r.cells.get(c)) }}</td>{% endfor %}</tr>{% endfor %}
</table></div>{% endif %}

{% if service %}<h2>Служебные файлы</h2><ul>{% for s in service %}<li>{{ s.file }}: {{ s.role }}</li>{% endfor %}</ul>{% endif %}
<script>const EV={{ evmap|safe }};document.querySelectorAll('img[data-ev]').forEach(function(i){if(EV[i.dataset.ev]){i.src=EV[i.dataset.ev]}else{i.remove()}});</script>
<p class="mute small">Время обработки: {{ meta.elapsed_sec }} с. Локальная LLM: {{ meta.llm }}. Все расчеты выполнены локально, внешние сервисы не используются.</p>
</main></body></html>"""


def to_html(result: dict, images: dict[str, bytes] | None = None) -> str:
    from jinja2 import Environment

    images = images or {}
    env = Environment(autoescape=True)

    evmap = json.dumps({n: "data:image/png;base64," + base64.b64encode(b).decode() for n, b in images.items()},
                       ensure_ascii=False)

    v = result.get("verdict", {})
    st = v.get("status", "")
    vclass = "v-bad" if "НЕДОСТОВЕРЕН" in st or "НЕ ВЫПОЛНЕНА" in st else "v-ok" if st == "ОТЧЕТ ДОСТОВЕРЕН" else "v-unk"
    rows = result.get("report_rows", [])
    cols = sorted({c for r in rows for c in r["cells"]}, key=int) if rows else []
    return env.from_string(_HTML).render(
        p=_Obj(result.get("params", {})), v=_Obj({"status": st, "summary": v.get("summary", []),
                                                  "counts": v.get("counts", {})}),
        meta=_Obj(result.get("meta", {})), inds=[_Obj(i) for i in result.get("indicators", [])],
        docs=[_Obj(x) for x in result.get("documents", [])], issues=[_Obj(x) for x in result.get("issues", [])],
        checks=[_Obj(c) for c in result.get("report_checks", [])], rows=[_Obj(r) for r in rows], cols=cols,
        service=[_Obj(s) for s in result.get("service_files", [])], errors=result.get("errors", []),
        vclass=vclass, m=fmt, d=fmt_date, evmap=evmap)


class _Obj(dict):
    """dict с доступом через точку для шаблона; отсутствующие ключи дают None."""
    def __getattr__(self, k):
        v = self.get(k)
        return _Obj(v) if isinstance(v, dict) else v


def write_all(run: dict, out_dir: str) -> dict[str, str]:
    """Сохраняет result.json, report.xlsx, report.html и вырезки. Возвращает пути к файлам."""
    os.makedirs(out_dir, exist_ok=True)
    images = make_evidence(run, out_dir)
    result = run["result"]
    paths = {"json": os.path.join(out_dir, "result.json"), "xlsx": os.path.join(out_dir, "report.xlsx"),
             "html": os.path.join(out_dir, "report.html")}
    with open(paths["json"], "w", encoding="utf-8") as f:
        f.write(to_json(result))
    to_xlsx(result, paths["xlsx"])
    with open(paths["html"], "w", encoding="utf-8") as f:
        f.write(to_html(result, images))
    return paths
