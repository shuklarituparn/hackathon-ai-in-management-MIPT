"""Отнесение первичного документа к виду затрат (статье калькуляции).

Порядок: 1) явный код статьи в документе; 2) словарь признаков с весами;
3) необязательная локальная LLM для неоднозначных случаев.
"""
from __future__ import annotations

import json
import re
import urllib.request

from . import config
from .models import PrimaryDoc

KIND_NAMES = {
    "labor": "Затраты на оплату труда",
    "insurance": "Страховые взносы",
    "materials": "Материальные затраты",
    "prod_overhead": "Общепроизводственные затраты",
    "gen_overhead": "Общехозяйственные затраты",
    "travel": "Командировочные расходы",
    "contractors": "Работы сторонних организаций",
    "other_direct": "Прочие прямые затраты",
}

# (регулярное выражение, вес). Явные формулировки весят больше косвенных признаков.
LEXICON: dict[str, list[tuple[str, float]]] = {
    "labor": [(r"оплат\w* труда", 5), (r"заработн\w+ плат", 5), (r"расч[её]тн\w+ ведомост", 3),
              (r"вид начислени", 2), (r"отпускн", 2), (r"преми[яи]\b", 1.5), (r"табел", 1.5)],
    "insurance": [(r"страхов\w+ взнос", 6), (r"начислен\w+ взнос", 4), (r"социальн\w+ страхован", 3),
                  (r"база начислени", 2), (r"тариф", 1)],
    "materials": [(r"материальн\w+ затрат", 5), (r"товарн\w+ накладн", 3), (r"торг-?12", 3),
                  (r"поставк\w+ (?:материал|товар|комплектующ)", 3), (r"комплектующ", 2), (r"сырь[яе]", 2),
                  (r"приобретени\w+ материал", 3), (r"расходн\w+ материал", 2), (r"реактив", 1.5)],
    "prod_overhead": [(r"общепроизводствен", 6), (r"производственн\w+ назначени", 3),
                      (r"накладн\w+ расход\w+ производствен", 3),
                      (r"производственн\w+ (?:деятельност|операци|процесс|оборудован|участ)", 2.5),
                      (r"испытательн", 2), (r"стенд", 2), (r"лаборатор", 2), (r"калибровк", 2),
                      (r"метрологи", 2), (r"техническ\w+ обслуживани", 2), (r"оборудовани", 1.5),
                      (r"измерительн", 1.5), (r"эксплуатаци", 1.5), (r"поверк", 1.5), (r"ремонт", 1.5),
                      (r"временное пользовани", 1), (r"аренд", 1), (r"выполнени\w+ работ\w* этап", 1.5),
                      (r"диагностик", 1), (r"амортизаци", 1.5), (r"электроэнерги", 1), (r"цех", 1.5)],
    "gen_overhead": [(r"общехозяйствен", 6), (r"административ", 2.5), (r"управлен", 2), (r"документооборот", 2),
                     (r"организацион", 2), (r"\bсвяз[ьи]\b", 1.5), (r"лицензи", 1.5), (r"инфраструктур", 1.5),
                     (r"информационн", 1), (r"аналитическ", 1), (r"координаци", 1), (r"совещани", 1),
                     (r"отч[её]тн", 1), (r"канцеляр", 2), (r"охран", 1.5), (r"юридическ", 2), (r"бухгалтер", 2),
                     (r"аудит", 1.5), (r"почтов", 1.5), (r"интернет", 1.5), (r"банковск", 1.5),
                     (r"уборк|клининг", 1.5), (r"совместной работы", 1), (r"программн", 1), (r"согласовани", 0.5)],
    "travel": [(r"командиров", 5), (r"суточн", 3), (r"авиабилет|проезд", 2), (r"проживани", 2)],
    "contractors": [(r"соисполнител", 5), (r"сторонн\w+ организаци", 4), (r"субподряд", 4)],
    "other_direct": [(r"прочи\w+ прям", 5)],
}
# Отрицания: «не относятся к производственной операции» не должно давать баллы производственной статье.
_NEG = re.compile(r"\bне\s+(?:относ\w+|связан\w+|явля\w+|включа\w+)(?:(?!\sи\s|\sа\s|\sно\s)[^.;,])*", re.I)


def score(text: str, kinds: list[str]) -> dict[str, tuple[float, list[str]]]:
    t = _NEG.sub(" ", text.lower().replace("ё", "е"))
    res = {}
    for k in kinds:
        total, hits = 0.0, []
        for rx, w in LEXICON.get(k, []):
            m = re.search(rx, t)
            if m:
                total += w
                hits.append(m.group(0))
        res[k] = (total, hits)
    return res


def classify(doc: PrimaryDoc, kinds: list[str], code_map: dict[str, str]) -> None:
    """Заполняет doc.kind, doc.kind_conf, doc.kind_reason. kinds: виды затрат, присутствующие в отчете."""
    # 1) Явные коды статей в строках документа (0201 -> статья 0200).
    codes = {it.code for it in doc.items if it.code}
    mapped = {code_map.get(c[:2]) for c in codes if c and len(c) >= 2} - {None}
    if len(mapped) == 1:
        doc.kind, doc.kind_conf, doc.kind_source = mapped.pop(), 1.0, "code"
        doc.kind_reason = "код статьи в документе: " + ", ".join(sorted(codes))
        return
    # 2) Словарь признаков.
    sc = score(doc.description + " " + doc.doc_type, kinds)
    ranked = sorted(sc.items(), key=lambda kv: -kv[1][0])
    if not ranked or ranked[0][1][0] <= 0:
        doc.kind, doc.kind_conf, doc.kind_reason = None, 0.0, "признаки статьи затрат не найдены"
        _llm(doc, kinds)
        return
    top, (ts, hits) = ranked[0]
    second = ranked[1][1][0] if len(ranked) > 1 else 0.0
    conf = (ts - second) / ts
    if ts < 2.0:
        conf = min(conf, 0.3)
    doc.kind, doc.kind_conf, doc.kind_source = top, round(conf, 2), "rules"
    doc.kind_reason = "признаки: " + ", ".join(hits[:6])
    if len(ranked) > 1 and second > 0:
        doc.kind_reason += f"; альтернатива: {KIND_NAMES.get(ranked[1][0], ranked[1][0])} ({second:g} против {ts:g})"
    if conf < config.CLASSIFY_MIN_CONF:
        _llm(doc, kinds)


def _llm(doc: PrimaryDoc, kinds: list[str]) -> None:
    """Локальная LLM как арбитр для неоднозначных документов. Отключена, если SVERKA_LLM_URL не задан."""
    if not config.LLM_URL:
        return
    options = "\n".join(f"{i + 1}. {KIND_NAMES.get(k, k)}" for i, k in enumerate(kinds))
    prompt = ("Ты бухгалтер. Определи, к какой статье калькуляции затрат относится первичный документ.\n"
              f"Статьи:\n{options}\n0. Ни одна из перечисленных\n\n"
              f"Текст документа:\n{doc.description[:1200]}\n\n"
              'Ответь только JSON вида {"n": номер статьи, "why": "краткая причина"}.')
    body = json.dumps({"model": config.LLM_MODEL, "temperature": 0, "stream": False,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request(config.LLM_URL + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as r:
            ans = json.loads(r.read())["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", ans, re.S)
        data = json.loads(m.group(0)) if m else {}
        n = int(data.get("n", 0))
        if 1 <= n <= len(kinds):
            doc.kind, doc.kind_source = kinds[n - 1], "llm"
            doc.kind_conf = max(doc.kind_conf, 0.6)
            doc.kind_reason = f"локальная LLM ({config.LLM_MODEL}): {str(data.get('why', ''))[:200]}"
    except Exception as e:
        doc.warnings.append(f"Локальная LLM недоступна, использованы правила: {type(e).__name__}")
