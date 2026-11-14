/* Chat results and evidence panel (T09).
 *
 * Plain browser JavaScript over the T02 API (/api/...). Rules this file keeps:
 * - Financial values come only from structured run results and cards; legacy reply envelopes yield prose only.
 * - Decimal values arrive as strings and are formatted as strings: no Number() conversion, no arithmetic, no totals.
 *   null is shown as unknown, distinct from a real zero.
 * - Every evidence click is resolved through GET /evidence/{id}; the returned version and URLs are used as given.
 * - Untrusted text (file names, OCR, quotes, messages) is inserted with textContent only.
 * - No model calls and no credentials here: the backend talks to the configured model.
 */
"use strict";

(function () {
  // ======================= constants =======================

  const KEY_CURRENT = "sverka.workspace";
  const KEY_RECENT = "sverka.recentWorkspaces";
  const KEY_HISTORY = "sverka.history";
  const KEY_LAYOUT = "sverka.layout";
  const KEY_PREFS = "sverka.workspacePrefs.";
  const POLL_MS = 1500;
  const POLL_MAX_MS = 15000;
  const NBSP = " ";
  const MINUS = "−";

  const ACTIVE = new Set(["queued", "running"]);
  const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
  const STAGES = ["prepare", "extract", "validate", "reconcile", "save"];
  const STAGE_LABEL = {
    prepare: "Подготовка документов",
    extract: "Извлечение данных",
    validate: "Проверка извлеченных данных",
    reconcile: "Сверка",
    save: "Сохранение результата",
  };
  const RUN_STATUS = {
    queued: { label: "В очереди", mark: "dots", tone: "active" },
    running: { label: "Выполняется", mark: "dots", tone: "active" },
    completed: { label: "Завершена", mark: "tick", tone: "ok" },
    failed: { label: "Не выполнена", mark: "cross", tone: "bad" },
    cancelled: { label: "Отменена", mark: "dash", tone: "muted" },
    interrupted: { label: "Прервана", mark: "bang", tone: "warn" },
  };
  const IND_STATUS = {
    verified: { label: "Подтверждено", mark: "tick", tone: "ok" },
    discrepancy: { label: "Расхождение", mark: "neq", tone: "bad" },
    insufficient_evidence: { label: "Недостаточно данных", mark: "query", tone: "warn" },
  };
  // Auditor's tick marks, drawn as single strokes so they read as pen marks rather than icons.
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MARKS = {
    tick: "M3.5 10.8 C5.3 12.2 6.6 13.7 7.7 15.6 C9.9 10.6 12.9 6.6 16.8 3.8",
    neq: "M3.8 7.6 L16.2 7.4 M3.8 12.6 L16.2 12.4 M13.2 3.4 L6.8 16.6",
    query: "M6.6 7.1 C6.5 4.7 8.3 3.3 10.2 3.3 C12.3 3.3 13.8 4.7 13.8 6.6 C13.8 9.1 10.4 9.5 10.4 12.2 M10.4 15.8 L10.4 16.2",
    cross: "M5.2 5.2 L14.8 14.8 M14.8 5.2 L5.2 14.8",
    dash: "M4.8 10.2 L15.2 9.8",
    dots: "M4.8 10 L5 10 M10 10 L10.2 10 M15.2 10 L15.4 10",
    bang: "M10 3.6 L10 11.8 M10 15.8 L10 16.2",
  };
  // A hand-drawn loop, slightly open at the end like a quick pencil circle.
  const PENCIL_LOOP = "M10 24 C6 11 30 3 56 3.5 C83 4 98 11 96 22 C94 34 68 39 45 38 C21 37 5 31 7 19 C8 14 13 10 21 7.5";
  const COVERAGE = { complete: "полное", partial: "частичное", none: "нет" };
  const PROBLEM = {
    amount_mismatch: "суммы не совпадают",
    duplicate: "дублирование документа",
    wrong_period: "документ другого периода",
    wrong_contract: "документ другого договора",
    omitted_document: "документ не отражен в отчете",
    report_arithmetic: "арифметика отчета",
    missing_coverage: "документы покрывают не весь период",
    no_supporting_document: "нет подтверждающего документа",
    unreadable_input: "документ не прочитан",
    unresolved_interpretation: "толкование не определено",
    conflicting_versions: "противоречивые версии",
    unreadable_report_value: "значение в отчете не прочитано",
  };
  const EXCLUSION = {
    duplicate: "дубликат",
    outside_period: "вне периода",
    other_contract: "другой договор",
    unrelated: "не относится к проверке",
    context_only: "справочный документ",
  };
  const ROLE = { report: "отчет", primary: "первичный", context: "справочный" };
  const ROLE_TITLE = { report: "Отчет", primary: "Первичные документы", context: "Справочные документы" };
  const METHOD = {
    native_text: "текстовый слой PDF",
    ocr: "распознавание текста на скане",
    reocr: "повторное распознавание участка",
    vision: "распознавание изображения моделью",
    spreadsheet: "ячейка таблицы",
    docx: "текст документа Word",
  };
  const ROLE_ONE = { report: "Итоговый отчет", primary: "Первичный документ", context: "Справочный документ" };
  // What the assistant is doing in each stage, written for the person watching.
  const ACTIVITY = {
    prepare: "Читаю документы и распознаю сканы",
    extract: "Извлекаю данные из документов",
    validate: "Проверяю найденные значения",
    reconcile: "Сверяю суммы с отчетом",
    save: "Сохраняю результат",
  };
  const CATEGORY = {
    workforce_intensity: "Трудоемкость",
    materials: "Материальные затраты",
    payroll_total: "Оплата труда, всего",
    payroll_account: "Оплата труда, счет 211",
    payroll_main: "Основная заработная плата",
    payroll_additional: "Дополнительная заработная плата",
    leave_reserve: "Резерв отпусков",
    contributions_total: "Страховые взносы, всего",
    contributions_account: "Взносы, счет 213",
    contributions_main: "Взносы на основную зарплату",
    contributions_additional: "Взносы на дополнительную зарплату",
    contributions_reserve: "Взносы на резерв отпусков",
    production_overhead: "Общепроизводственные затраты",
    administrative_overhead: "Общехозяйственные затраты",
    total: "Итого",
    unrelated: "Не относится к отчету",
  };
  const FIELD = {
    contract: "Договор",
    report_row: "Строка отчета",
    date_from: "Начало периода",
    date_to: "Конец периода",
    version_ids: "Документы",
    text: "Вопрос",
  };
  const EVALUATION = {
    literal: "значение без формулы",
    derived: "формула вычислена приложением",
    unresolved: "формула не вычислена (неизвестные входные данные или неподдерживаемая функция)",
    error: "формула дает ошибку",
  };
  const FAILURE = {
    missing_credentials: "не настроен доступ к модели",
    provider_error: "ошибка внешнего сервиса модели",
    timeout: "превышено время ожидания",
    budget_exhausted: "исчерпан лимит запросов",
    invalid_input: "некорректные входные данные",
    internal_error: "внутренняя ошибка",
  };
  const FAILURE_ACTION = {
    missing_credentials: "Обратитесь к организатору сверки, чтобы подключить модель.",
    provider_error: "Не удалось получить ответ от модели. Попробуйте повторить проверку позже.",
    timeout: "Повторите проверку. Если ожидание снова прервется, обратитесь к организатору сверки.",
    budget_exhausted: "Доступный лимит обращений к модели закончился. Обратитесь к организатору сверки.",
    invalid_input: "Проверьте выбранный отчет, первичные документы и параметры проверки.",
    internal_error: "Попробуйте повторить проверку. Если ошибка повторится, обратитесь к организатору сверки.",
  };
  const HTTP_TEXT = {
    400: "Некорректный запрос",
    404: "Не найдено на сервере",
    409: "Конфликт",
    413: "Слишком большой файл или архив",
    415: "Формат не поддерживается",
    422: "Данные запроса не прошли проверку",
    500: "Внутренняя ошибка сервера",
    502: "Сервер недоступен",
    503: "Сервис недоступен",
    504: "Превышено время ожидания",
  };
  const ID_RE = {};
  for (const prefix of ["ws", "doc", "ver", "blk", "art", "run", "msg"]) {
    ID_RE[prefix] = new RegExp("^" + prefix + "_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$");
  }
  const DECIMAL_RE = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/;
  const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

  // ======================= DOM helpers =======================

  const $ = (selector, root) => (root || document).querySelector(selector);

  /** Internal references belong to requests and data attributes, never to visible or accessible copy. */
  function displayText(value) {
    return String(value)
      .replace(/\b(?:ws|doc|ver|blk|art|run|msg)_[A-Za-z0-9][A-Za-z0-9_-]{0,63}\b/g, (id) => {
        if (id.startsWith("run_") && state.runs.has(id)) return "проверка №" + runNumber(id);
        if (id.startsWith("ver_")) {
          const version = versionById(id);
          return version ? "версия документа от " + formatTime(version.created_at) : "версия документа";
        }
        return { ws: "сверка", doc: "документ", blk: "фрагмент", art: "файл", run: "проверка", msg: "сообщение" }[id.split("_")[0]] || "документ";
      })
      .replace(/(?:\bSHA(?:-?1|-?256)?|\bcommit|коммит|хеш|хэш)\s*[:=]?\s*[a-f0-9]{7,64}\b/gi, "")
      .replace(/\b[a-f0-9]{7,64}\b/gi, (token) => {
        // A100000 is a cell address, and a long decimal fraction is still money. Short hashes need
        // an explicit label above or a known runtime value; shape alone cannot distinguish them.
        const lower = token.toLowerCase();
        const known = state.documents.some((v) => typeof v.sha256 === "string" && v.sha256.toLowerCase().startsWith(lower))
          || runsInOrder().some((r) => r.replay && typeof r.replay.app_version === "string" && r.replay.app_version.toLowerCase().startsWith(lower));
        return known || (/[a-f]/i.test(token) && [40, 64].includes(token.length)) ? "…" : token;
      })
      .replace(/\s*\(?(?:[A-Za-z_][\w.]*(?:Error|Exception)|T\d{2})\)?/g, "")
      .replace(/\b(?:workspace_id|run_id|version_id|document_id|block_id|artifact_id|message_id|sha256)\b/g, "ссылка")
      .replace(/\b(?:missing_credentials|provider_error|timeout|budget_exhausted|invalid_input|internal_error)\b/g, (token) => FAILURE[token])
      .replace(/\bSVERKA_[A-Z0-9_]+\b/g, "настройка приложения")
      .replace(/\/api\/[^\s<>"']+/g, "ссылка на документ");
  }

  /** Same-origin relative paths only; rejects javascript:, data:, protocol-relative and absolute URLs. */
  function isSafeUrl(url) {
    return typeof url === "string" && /^\/(?!\/)[^\s\\]*$/.test(url);
  }

  /** Build an element. Strings become text nodes; there is deliberately no HTML-string path. */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") el.className = value;
        else if (key.startsWith("on")) {
          if (typeof value === "function") el.addEventListener(key.slice(2), value);
        } else if (key === "href" || key === "src") {
          if (isSafeUrl(value)) el.setAttribute(key, value);
        } else el.setAttribute(key, value === true ? "" : ["title", "aria-label", "alt", "placeholder"].includes(key) ? displayText(value) : String(value));
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : displayText(child));
    }
    return el;
  }

  /** Replace a container's children, keeping keyboard focus on the element with the same data-fkey. */
  function setChildren(container, ...nodes) {
    const key = focusKey(container);
    container.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
    restoreFocus(container, key);
  }

  function replaceNode(oldNode, newNode) {
    const key = focusKey(oldNode);
    oldNode.replaceWith(newNode);
    restoreFocus(newNode, key);
  }

  function focusKey(root) {
    const active = document.activeElement;
    return active && root.contains(active) ? active.getAttribute("data-fkey") : null;
  }

  function restoreFocus(root, key) {
    if (!key) return;
    const target = root.querySelector('[data-fkey="' + CSS.escape(key) + '"]');
    if (target) target.focus();
  }

  function domId(prefix, key) {
    return prefix + "-" + String(key).replace(/[^A-Za-z0-9_-]/g, "_");
  }

  function code(text) {
    return h("code", null, text);
  }

  function announce(text) {
    const el = $("#announcer");
    el.textContent = "";
    window.setTimeout(() => {
      el.textContent = displayText(text);
    }, 30);
  }

  function plural(n, one, few, many) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  // ======================= formatting =======================

  /** Format a decimal string without converting it to a number: groups digits, keeps every fraction digit.
   * Returns null for null/undefined and undefined for a malformed value. */
  function formatDecimal(value, signed) {
    if (value === null || value === undefined) return null;
    if (typeof value !== "string" || !DECIMAL_RE.test(value)) return undefined;
    const negative = value.charAt(0) === "-";
    const body = negative ? value.slice(1) : value;
    const parts = body.split(".");
    const zero = /^0(\.0+)?$/.test(body);
    const grouped = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
    const sign = zero ? "" : negative ? MINUS : signed ? "+" : "";
    return sign + grouped + (parts.length > 1 ? "," + parts[1] : "");
  }

  function amount(value, unit, opts) {
    const options = opts || {};
    const text = formatDecimal(value, options.signed);
    if (text === null) {
      return h("span", { class: "val val-null", "data-null": "true" }, options.unknown || "неизвестно");
    }
    if (text === undefined) {
      return h("span", { class: "val val-bad" }, String(value) + " (некорректное значение)");
    }
    return h(
      "span",
      { class: "val", "data-value": value },
      h("span", { class: "num" }, text),
      unit ? h("span", { class: "unit" }, NBSP + unit) : null,
    );
  }

  function formatIsoDate(value) {
    const m = typeof value === "string" ? ISO_DATE_RE.exec(value) : null;
    return m ? m[3] + "." + m[2] + "." + m[1] : String(value);
  }

  function formatTime(value) {
    const t = Date.parse(value);
    if (Number.isNaN(t)) return String(value);
    return new Date(t).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatSize(bytes) {
    if (typeof bytes !== "number" || bytes < 0) return "";
    if (bytes < 1024) return bytes + " Б";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1).replace(".", ",") + " КБ";
    return (bytes / 1024 / 1024).toFixed(1).replace(".", ",") + " МБ";
  }

  function svg(className, viewBox, d, extra) {
    const el = document.createElementNS(SVG_NS, "svg");
    el.setAttribute("class", className);
    el.setAttribute("viewBox", viewBox);
    el.setAttribute("aria-hidden", "true");
    el.setAttribute("focusable", "false");
    if (extra) for (const [k, v] of Object.entries(extra)) el.setAttribute(k, v);
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("pathLength", "1");
    el.append(path);
    return el;
  }

  function mark(kind) {
    return svg("mark mark-" + kind, "0 0 20 20", MARKS[kind] || MARKS.query);
  }

  /** Status in words, preceded by its tick mark; the colour repeats what the words already say. */
  function badge(info, fallback) {
    const item = info || { label: String(fallback), mark: "query", tone: "muted" };
    return h("span", { class: "badge tone-" + item.tone }, mark(item.mark), h("span", { class: "badge-text" }, item.label));
  }

  /** Wrap a discrepancy amount in a red-pencil loop. */
  function circled(node) {
    return h("span", { class: "circled" }, node, svg("pencil", "0 0 100 42", PENCIL_LOOP, { preserveAspectRatio: "none" }));
  }

  // ======================= storage =======================

  const storage = {
    available: true,
    get(key) {
      try {
        return window.localStorage.getItem(key);
      } catch (e) {
        storage.available = false;
        return null;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(key, value);
        return true;
      } catch (e) {
        storage.available = false;
        return false;
      }
    },
    getJson(key) {
      const raw = storage.get(key);
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch (e) {
        return null;
      }
    },
    setJson(key, value) {
      return storage.set(key, JSON.stringify(value));
    },
  };

  // ======================= HTTP =======================

  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details || null;
    }
  }

  const isAbort = (e) => Boolean(e) && e.name === "AbortError";
  const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

  async function request(method, path, options) {
    const opts = options || {};
    const init = { method, headers: { Accept: "application/json" }, signal: opts.signal, cache: "no-store" };
    if (opts.json !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    } else if (opts.form) {
      init.body = opts.form;
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (e) {
      if (isAbort(e)) throw e;
      throw new ApiError(0, "network", "Нет связи с сервером. Проверьте, что приложение запущено, и повторите.");
    }
    let text;
    try {
      text = await response.text();
    } catch (e) {
      if (isAbort(e)) throw e;
      throw new ApiError(0, "network", "Ответ сервера оборвался. Повторите.");
    }
    let body;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch (e) {
        body = undefined;
      }
    }
    if (!response.ok) throw httpError(response.status, body);
    if (body === undefined) {
      throw new ApiError(response.status, "malformed", "Не удалось прочитать ответ приложения. Повторите действие.");
    }
    return body;
  }

  function httpError(status, body) {
    let code = "http";
    let message = "";
    let details = null;
    if (isObj(body)) {
      const err = body.error;
      if (isObj(err) && typeof err.message === "string") {
        code = typeof err.code === "string" ? err.code : "error";
        message = humanMessage(status, err.message);
        details = isObj(err.details) ? err.details : null;
      } else if (Array.isArray(body.detail)) {
        code = "validation";
        message = body.detail.map(validationText).join(" ");
      } else if (typeof body.detail === "string") {
        message = humanMessage(status, body.detail);
      } else if (typeof err === "string") {
        message = humanMessage(status, err);
      }
    }
    const prefix = HTTP_TEXT[status] || "Ошибка сервера (" + status + ")";
    return new ApiError(status, code, message ? prefix + ". " + message : prefix + ".", details);
  }

  /** Rephrase the server's known English messages; keep Russian ones; drop other English detail. */
  function humanMessage(status, text) {
    if (/\b(?:harness|trace|traceback|tool_call|SVERKA_\w*|[A-Za-z_]+(?:Error|Exception))\b/i.test(text)) return "";
    const zip = /^(.+?) (is not a valid zip archive|is damaged|contains no files)$/.exec(text);
    if (zip) return "Архив «" + zip[1] + "» поврежден или пуст.";
    const limit = /exceeds (\d+) MB/.exec(text);
    if (limit) return "Лимит " + limit[1] + " МБ.";
    if (/encrypted/.test(text)) return "Зашифрованные файлы не поддерживаются.";
    if (/already has an active check/.test(text)) return "В этой сверке уже идет проверка.";
    return /[А-Яа-яЁё]/.test(text) ? displayText(text) : "";
  }

  function validationText(item) {
    if (!isObj(item)) return "Проверьте введенные данные.";
    const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body" && p !== "scope") : [];
    const field = loc.length ? FIELD[loc[loc.length - 1]] : null;
    return field ? "Проверьте поле «" + field + "»." : "Проверьте введенные данные.";
  }

  function malformed(what) {
    return new ApiError(200, "malformed", "Сервер вернул некорректный ответ (" + what + ").");
  }

  function expect(condition, what) {
    if (!condition) throw malformed(what);
  }

  function checkList(value, check, what) {
    expect(Array.isArray(value), what);
    return value.map(check);
  }

  function checkWorkspace(x) {
    expect(isObj(x) && ID_RE.ws.test(x.workspace_id), "рабочее пространство");
    return x;
  }

  function checkVersion(x) {
    expect(
      isObj(x) &&
        ID_RE.ver.test(x.version_id) &&
        ID_RE.doc.test(x.document_id) &&
        typeof x.original_filename === "string" &&
        Object.prototype.hasOwnProperty.call(ROLE, x.role) &&
        ID_RE.art.test(x.original_artifact_id),
      "версия документа",
    );
    return x;
  }

  function checkRun(x) {
    expect(
      isObj(x) &&
        ID_RE.run.test(x.run_id) &&
        Object.prototype.hasOwnProperty.call(RUN_STATUS, x.status) &&
        Array.isArray(x.input_version_ids) &&
        isObj(x.scope) &&
        (x.progress === undefined || Array.isArray(x.progress)) &&
        (x.issues === undefined || Array.isArray(x.issues)),
      "проверка",
    );
    if (x.status === "completed") {
      const r = x.result;
      expect(
        isObj(r) && Array.isArray(r.indicators) && isObj(r.conclusion) && isObj(r.conclusion.grand_total),
        "результат проверки",
      );
      for (const ind of r.indicators) expect(isObj(ind) && typeof ind.entry_id === "string", "показатель");
    }
    return x;
  }

  function checkMessage(x) {
    expect(
      isObj(x) &&
        ID_RE.msg.test(x.message_id) &&
        ["user", "assistant", "system"].includes(x.role) &&
        typeof x.text === "string" &&
        (x.cards === undefined || Array.isArray(x.cards)) &&
        (x.evidence_ids === undefined || Array.isArray(x.evidence_ids)),
      "сообщение",
    );
    return x;
  }

  function checkEvidence(x, blockId) {
    expect(isObj(x) && isObj(x.block) && isObj(x.version) && isObj(x.block.locator), "фрагмент");
    expect(x.block.block_id === blockId, "сервер вернул другой фрагмент");
    expect(x.block.version_id === x.version.version_id, "версия фрагмента не совпадает с версией документа");
    expect(isSafeUrl(x.original_url), "ссылка на исходный файл");
    expect(x.artifact_url === null || x.artifact_url === undefined || isSafeUrl(x.artifact_url), "ссылка на изображение");
    checkVersion(x.version);
    return x;
  }

  // ======================= state =======================

  function idleUpload() {
    return { busy: false, error: null, message: null };
  }

  const state = {
    gen: 0,
    controller: new AbortController(),
    health: null,
    healthError: null,
    workspaceId: null,
    wsStatus: "none", // none | loading | ready | error
    wsError: null,
    wsCreating: false,
    wsCreateError: null,
    documents: [],
    runs: new Map(),
    runOrder: [],
    messages: [],
    messageIds: new Set(),
    replyMessageIds: new Set(),
    pending: [],
    pendingSeq: 0,
    selection: { report: null, primary: new Set(), context: new Set() },
    confirmed: false,
    selectedRunId: null,
    noRunChosen: false,
    starting: false,
    startError: null,
    startNotice: null,
    cancelling: new Set(),
    runErrors: new Map(),
    pollErrors: new Map(),
    polls: new Map(),
    replyPoll: null,
    replyPollAttempts: 0,
    waitingReplies: new Set(),
    waitingOutcomes: new Map(),
    uploads: { report: idleUpload(), primary: idleUpload(), context: idleUpload() },
    replace: { docId: null, busy: false, error: null },
    sending: false,
    expanded: new Set(),
    openOlder: new Set(),
    evidence: { req: 0, status: "idle", blockId: null, view: null, error: null, expectVersion: null, trigger: null },
    nodes: new Map(),
    setupOpen: true,
    renaming: false,
    collapsedReports: new Set(),
    scrollTo: null,
  };

  function ctx() {
    return { gen: state.gen, signal: state.controller.signal };
  }

  function workspaceBase() {
    return "/api/workspaces/" + encodeURIComponent(state.workspaceId);
  }

  function ready() {
    return state.wsStatus === "ready";
  }

  function runsInOrder() {
    return state.runOrder.map((id) => state.runs.get(id));
  }

  function runNumber(runId) {
    const index = state.runOrder.indexOf(runId);
    return index < 0 ? "?" : String(index + 1);
  }

  function activeRun() {
    for (const run of runsInOrder()) if (ACTIVE.has(run.status)) return run;
    return null;
  }

  function versionById(versionId) {
    return state.documents.find((v) => v.version_id === versionId) || null;
  }

  function latestVersionOf(documentId) {
    let latest = null;
    for (const v of state.documents) if (v.document_id === documentId) latest = v;
    return latest;
  }

  function isFixtureRun(run) {
    return (run.issues || []).some((i) => isObj(i) && i.code === "fixture_result");
  }

  // ======================= workspace =======================

  function resetWorkspace(id) {
    state.gen += 1;
    state.controller.abort();
    state.controller = new AbortController();
    for (const timer of state.polls.values()) window.clearTimeout(timer);
    state.polls.clear();
    window.clearTimeout(state.replyPoll);
    state.replyPoll = null;
    state.replyPollAttempts = 0;
    state.waitingReplies = new Set();
    state.waitingOutcomes = new Map();
    state.workspaceId = id;
    state.wsStatus = id ? "loading" : "none";
    state.wsError = null;
    state.documents = [];
    state.runs = new Map();
    state.runOrder = [];
    state.messages = [];
    state.messageIds = new Set();
    state.replyMessageIds = new Set();
    state.pending = [];
    state.selection = { report: null, primary: new Set(), context: new Set() };
    state.confirmed = false;
    state.selectedRunId = null;
    state.noRunChosen = false;
    state.starting = false;
    state.startError = null;
    state.startNotice = null;
    state.cancelling = new Set();
    state.runErrors = new Map();
    state.pollErrors = new Map();
    state.uploads = { report: idleUpload(), primary: idleUpload(), context: idleUpload() };
    state.replace = { docId: null, busy: false, error: null };
    state.sending = false;
    state.expanded = new Set();
    state.openOlder = new Set();
    state.evidence = {
      req: state.evidence.req + 1,
      status: "idle",
      blockId: null,
      view: null,
      error: null,
      expectVersion: null,
      trigger: null,
    };
    state.nodes = new Map();
    state.setupOpen = true;
    state.renaming = false;
    state.collapsedReports = new Set();
    state.scrollTo = null;
    document.body.classList.remove("evidence-open");
    $("#timeline").replaceChildren();
    for (const form of document.querySelectorAll("form.upload")) form.reset();
    setScopeInputs({});
  }

  async function openWorkspace(id) {
    resetWorkspace(id);
    renderAll();
    if (!ID_RE.ws.test(id)) {
      state.wsStatus = "error";
      state.wsError = new ApiError(0, "invalid", "Ссылка на сверку повреждена. Откройте ее из списка сохраненных сверок.");
      renderAll();
      return;
    }
    const { gen, signal } = ctx();
    const base = workspaceBase();
    try {
      const [docs, runs, messages] = await Promise.all([
        request("GET", base + "/documents", { signal }),
        request("GET", base + "/runs", { signal }),
        request("GET", base + "/messages", { signal }),
      ]);
      if (gen !== state.gen) return;
      state.documents = checkList(docs, checkVersion, "список документов");
      const runList = checkList(runs, checkRun, "список проверок");
      const messageList = checkList(messages, checkMessage, "сообщения");
      for (const run of runList) upsertRun(run);
      setMessages(messageList);
      restorePrefs();
      state.wsStatus = "ready";
      state.setupOpen = runList.length === 0; // a reconciliation with results opens on its conversation
      storage.set(KEY_CURRENT, id);
      setLocationHash(id);
      syncHistory(true);
      for (const run of runList) if (ACTIVE.has(run.status)) schedulePoll(run.run_id, POLL_MS);
      announce("Открыта сверка «" + historyTitle(loadHistory()[0] || {}) + "»");
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      state.wsStatus = "error";
      state.wsError = e;
      updateHistory(id, { unavailable: true }, false);
    }
    renderAll();
  }

  function setLocationHash(id) {
    if (window.location.hash.slice(1) === id) return;
    try {
      window.history.replaceState(null, "", "#" + id);
    } catch (e) {
      // a sandboxed page may refuse; the stored ID still restores the workspace
    }
  }

  async function createWorkspace() {
    if (state.wsCreating) return;
    const gen = state.gen;
    state.wsCreating = true;
    state.wsCreateError = null;
    showWorkspace();
    try {
      const ws = checkWorkspace(await request("POST", "/api/workspaces"));
      state.wsCreating = false;
      if (gen !== state.gen) {
        updateHistory(ws.workspace_id, {}, true);
        showWorkspace();
        return;
      }
      await openWorkspace(ws.workspace_id);
    } catch (e) {
      state.wsCreating = false;
      if (gen === state.gen) state.wsCreateError = e;
      showWorkspace();
    }
  }

  // ---------- saved reconciliations (the sidebar list) ----------
  // The API cannot list workspaces, so this browser keeps the list: ID, a readable title, last run status and the
  // time of the latest activity. Entries are never dropped automatically; an inaccessible one is only marked.

  function loadHistory() {
    const raw = storage.getJson(KEY_HISTORY);
    if (Array.isArray(raw)) return raw.filter((x) => isObj(x) && ID_RE.ws.test(x.id));
    const old = storage.getJson(KEY_RECENT); // earlier versions kept plain IDs
    return Array.isArray(old) ? old.filter((x) => typeof x === "string" && ID_RE.ws.test(x)).map((id) => ({ id })) : [];
  }

  /** Merge ``patch`` into the entry for ``id``. ``bump`` moves it to the top (new activity) and adds it if missing. */
  function updateHistory(id, patch, bump) {
    const list = loadHistory();
    const index = list.findIndex((x) => x.id === id);
    if (index < 0 && !bump) return;
    const before = index >= 0 ? list[index] : { id };
    const item = Object.assign({}, before, patch);
    if (!bump && JSON.stringify(item) === JSON.stringify(before)) return;
    if (index >= 0) list.splice(index, 1);
    if (bump) list.unshift(item);
    else list.splice(index, 0, item);
    storage.setJson(KEY_HISTORY, list.slice(0, 50));
  }

  function autoTitle() {
    const runs = runsInOrder();
    const last = runs[runs.length - 1];
    const scope = last ? last.scope : readScopeInputs();
    if (scope && scope.contract && scope.report_row) return "Договор " + scope.contract + ", " + scope.report_row;
    const report = state.documents.find((v) => v.role === "report");
    return report ? report.original_filename : null;
  }

  function syncHistory(bump) {
    if (!ready()) return;
    const runs = runsInOrder();
    const last = runs[runs.length - 1];
    let latest = 0;
    for (const x of [...state.documents, ...runs, ...state.messages]) {
      const t = Date.parse(x.updated_at || x.created_at);
      if (t > latest) latest = t;
    }
    updateHistory(
      state.workspaceId,
      {
        auto: autoTitle(),
        status: last ? last.status : null,
        files: new Set(state.documents.map((v) => v.document_id)).size,
        updated: new Date(bump || !latest ? Date.now() : latest).toISOString(),
        unavailable: false,
      },
      bump,
    );
  }

  function historyTitle(item) {
    return item.custom || item.auto || "Новая сверка";
  }

  function prefsKey() {
    return KEY_PREFS + state.workspaceId;
  }

  function savePrefs() {
    if (!state.workspaceId || !ready()) return;
    storage.setJson(prefsKey(), {
      report: state.selection.report,
      primary: Array.from(state.selection.primary),
      context: Array.from(state.selection.context),
      selectedRunId: state.selectedRunId,
      noRun: state.noRunChosen === true,
      scope: readScopeInputs(),
    });
  }

  function restorePrefs() {
    const prefs = storage.getJson(prefsKey());
    const byId = new Map(state.documents.map((v) => [v.version_id, v]));
    const ofRole = (ids, role) =>
      new Set((Array.isArray(ids) ? ids : []).filter((id) => byId.has(id) && byId.get(id).role === role));
    if (isObj(prefs)) {
      const report = byId.get(prefs.report);
      state.selection.report = report && report.role === "report" ? report.version_id : null;
      state.selection.primary = ofRole(prefs.primary, "primary");
      state.selection.context = ofRole(prefs.context, "context");
    } else {
      // Default: the latest version of each document; a report only when there is exactly one report document.
      const latest = new Map();
      for (const v of state.documents) latest.set(v.document_id, v);
      const reports = [];
      for (const v of latest.values()) {
        if (v.role === "report") reports.push(v);
        else state.selection[v.role].add(v.version_id);
      }
      state.selection.report = reports.length === 1 ? reports[0].version_id : null;
    }
    const runIds = state.runOrder;
    if (isObj(prefs) && runIds.includes(prefs.selectedRunId)) state.selectedRunId = prefs.selectedRunId;
    else {
      const completed = runsInOrder().filter((r) => r.status === "completed");
      const fallback = completed.length ? completed[completed.length - 1] : runsInOrder()[runIds.length - 1];
      state.selectedRunId = fallback ? fallback.run_id : null;
    }
    if (isObj(prefs) && isObj(prefs.scope)) setScopeInputs(prefs.scope);
    else {
      const last = runsInOrder()[runIds.length - 1];
      setScopeInputs(last ? last.scope : {});
    }
    state.confirmed = false;
  }

  // ======================= documents =======================

  async function upload(role, form) {
    const slot = state.uploads[role];
    if (!ready() || slot.busy) return;
    const input = form.querySelector('input[type="file"]');
    const files = Array.from(input.files || []);
    if (!files.length) {
      state.uploads[role] = { busy: false, error: "Выберите файл для загрузки.", message: null };
      renderUploads();
      return;
    }
    if (role === "report" && files.length !== 1) {
      state.uploads[role] = { busy: false, error: "Загрузите один файл отчета.", message: null };
      renderUploads();
      return;
    }
    const body = new FormData();
    for (const file of files) body.append("files", file, file.name);
    body.append("role", role);
    state.uploads[role] = { busy: true, error: null, message: null };
    renderUploads();
    const { gen, signal } = ctx();
    try {
      const versions = checkList(
        await request("POST", workspaceBase() + "/documents", { form: body, signal }),
        checkVersion,
        "загруженные версии",
      );
      if (gen !== state.gen) return;
      addVersions(versions);
      if (role === "report") state.selection.report = versions[versions.length - 1].version_id;
      else for (const v of versions) state.selection[role].add(v.version_id);
      resetConfirmation();
      savePrefs();
      const names = versions.map((v) => v.original_filename).join(", ");
      state.uploads[role] = {
        busy: false,
        error: null,
        message: "Загружено " + versions.length + " " + plural(versions.length, "файл", "файла", "файлов") + ": " + names,
      };
      form.reset();
      announce(state.uploads[role].message);
      syncHistory(true);
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      state.uploads[role] = { busy: false, error: e.message, message: null };
      await syncDocuments(gen, signal);
    }
    renderAll();
  }

  async function uploadReplacement(documentId, file) {
    if (!ready() || state.replace.busy) return;
    const previous = latestVersionOf(documentId);
    if (!previous || !file) return;
    const body = new FormData();
    body.append("files", file, file.name);
    body.append("role", previous.role);
    body.append("document_id", documentId);
    state.replace = { docId: documentId, busy: true, error: null };
    showDocs();
    const { gen, signal } = ctx();
    try {
      const versions = checkList(
        await request("POST", workspaceBase() + "/documents", { form: body, signal }),
        checkVersion,
        "новая версия",
      );
      if (gen !== state.gen) return;
      expect(versions.length === 1 && versions[0].document_id === documentId, "новая версия документа");
      const added = versions[0];
      addVersions(versions);
      const sel = state.selection;
      if (added.role === "report") {
        if (sel.report === previous.version_id || sel.report === null) sel.report = added.version_id;
      } else if (sel[added.role].delete(previous.version_id)) sel[added.role].add(added.version_id);
      resetConfirmation();
      savePrefs();
      state.replace = { docId: null, busy: false, error: null };
      announce("Загружена новая версия документа «" + added.original_filename + "».");
      syncHistory(true);
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      state.replace = { docId: documentId, busy: false, error: e.message };
      await syncDocuments(gen, signal);
    }
    renderAll();
  }

  function addVersions(versions) {
    const known = new Set(state.documents.map((v) => v.version_id));
    for (const v of versions) if (!known.has(v.version_id)) state.documents.push(v);
  }

  async function syncDocuments(gen, signal) {
    try {
      const docs = checkList(
        await request("GET", workspaceBase() + "/documents", { signal }),
        checkVersion,
        "список документов",
      );
      if (gen === state.gen) state.documents = docs;
    } catch (e) {
      // The upload error is already shown; the list keeps its last known state.
    }
  }

  function onSelectionChange(event) {
    const input = event.target;
    if (!input || !input.dataset || !input.dataset.versionId) return;
    const vid = input.dataset.versionId;
    const role = input.dataset.role;
    if (role === "report") {
      if (input.checked) state.selection.report = vid;
    } else if (input.checked) state.selection[role].add(vid);
    else state.selection[role].delete(vid);
    resetConfirmation();
    savePrefs();
    renderScope();
  }

  // ======================= scope =======================

  const scopeInputs = () => ({
    contract: $("#scope-contract"),
    report_row: $("#scope-row"),
    date_from: $("#scope-from"),
    date_to: $("#scope-to"),
  });

  function readScopeInputs() {
    const inputs = scopeInputs();
    return {
      contract: inputs.contract.value.trim(),
      report_row: inputs.report_row.value.trim(),
      date_from: inputs.date_from.value,
      date_to: inputs.date_to.value,
    };
  }

  function setScopeInputs(scope) {
    const inputs = scopeInputs();
    for (const key of Object.keys(inputs)) {
      const value = scope && typeof scope[key] === "string" ? scope[key] : "";
      inputs[key].value = value;
    }
  }

  function resetConfirmation() {
    state.confirmed = false;
    $("#scope-confirm").checked = false;
  }

  function validIsoDate(value) {
    const m = ISO_DATE_RE.exec(value);
    if (!m) return false;
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    return d.getUTCFullYear() === Number(m[1]) && d.getUTCMonth() === Number(m[2]) - 1 && d.getUTCDate() === Number(m[3]);
  }

  function selectedVersionIds() {
    const sel = state.selection;
    const ids = [];
    if (sel.report) ids.push(sel.report);
    for (const v of state.documents) if (v.role === "primary" && sel.primary.has(v.version_id)) ids.push(v.version_id);
    for (const v of state.documents) if (v.role === "context" && sel.context.has(v.version_id)) ids.push(v.version_id);
    return ids;
  }

  function startBlockers() {
    const blockers = [];
    if (!ready()) {
      blockers.push("Откройте или создайте рабочее пространство.");
      return blockers;
    }
    const sel = state.selection;
    const report = sel.report ? versionById(sel.report) : null;
    if (!report || report.role !== "report") blockers.push("Выберите ровно один отчет.");
    const primaries = Array.from(sel.primary).filter((id) => versionById(id));
    if (!primaries.length) blockers.push("Выберите хотя бы один первичный документ.");
    const scope = readScopeInputs();
    if (!scope.contract) blockers.push("Укажите договор.");
    if (!scope.report_row) blockers.push("Укажите строку отчета.");
    const fromOk = validIsoDate(scope.date_from);
    const toOk = validIsoDate(scope.date_to);
    if (!fromOk) blockers.push("Укажите начало периода.");
    if (!toOk) blockers.push("Укажите конец периода.");
    if (fromOk && toOk && scope.date_from > scope.date_to) blockers.push("Начало периода позже его конца.");
    const active = activeRun();
    if (active) blockers.push("Уже выполняется проверка №" + runNumber(active.run_id) + ": дождитесь ее завершения или отмените.");
    if (!state.confirmed) blockers.push("Подтвердите параметры проверки.");
    if (state.starting) blockers.push("Запуск уже отправлен.");
    return blockers;
  }

  async function startRun() {
    if (state.starting) return; // double submission guard; the button is disabled as well
    const blockers = startBlockers();
    if (blockers.length) {
      renderScope();
      return;
    }
    const scope = readScopeInputs();
    const body = {
      version_ids: selectedVersionIds(),
      scope: {
        contract: scope.contract,
        report_row: scope.report_row,
        date_from: scope.date_from,
        date_to: scope.date_to,
        coverage_basis: "supplied_bundle",
        confirmed: true,
      },
      predecessor_run_id: null,
    };
    state.starting = true;
    state.startError = null;
    state.startNotice = null;
    renderScope();
    const { gen, signal } = ctx();
    try {
      const run = checkRun(await request("POST", workspaceBase() + "/runs", { json: body, signal }));
      if (gen !== state.gen) return;
      upsertRun(run);
      state.selectedRunId = run.run_id; // the user just started it, so it becomes the follow-up target
      state.noRunChosen = false;
      resetConfirmation();
      savePrefs();
      state.scrollTo = "run:" + run.run_id;
      state.setupOpen = false; // the conversation takes the screen once a check is under way
      syncHistory(true);
      if (ACTIVE.has(run.status)) schedulePoll(run.run_id, POLL_MS);
      announce("Проверка №" + runNumber(run.run_id) + ": " + RUN_STATUS[run.status].label);
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      if (e.status === 409) {
        state.startNotice =
          "Новая проверка не запущена: в рабочем пространстве уже выполняется другая проверка. Она показана в разговоре.";
        await refreshRuns(gen, signal);
      } else {
        state.startError = "Проверка не запущена. " + e.message;
      }
    } finally {
      if (gen === state.gen) {
        state.starting = false;
        renderAll();
      }
    }
  }

  // ======================= runs =======================

  function byCreated(a, b) {
    const ra = state.runs.get(a);
    const rb = state.runs.get(b);
    const ta = Date.parse(ra.created_at);
    const tb = Date.parse(rb.created_at);
    if (Number.isNaN(ta) || Number.isNaN(tb) || ta === tb) return 0;
    return ta - tb;
  }

  /** Store a run snapshot. Finished runs are final, and an older snapshot never replaces a newer one, so late
   * polling or cancel responses cannot move a run backwards. Selection is never touched here. */
  function upsertRun(run) {
    const current = state.runs.get(run.run_id);
    if (current) {
      if (TERMINAL.has(current.status)) return false;
      const a = Date.parse(current.updated_at);
      const b = Date.parse(run.updated_at);
      if (!Number.isNaN(a) && !Number.isNaN(b) && b < a) return false;
      state.runs.set(run.run_id, run);
      watchOutcome(run);
      return true;
    }
    state.runs.set(run.run_id, run);
    state.runOrder.push(run.run_id);
    state.runOrder.sort(byCreated);
    watchOutcome(run);
    return true;
  }

  function watchOutcome(run) {
    if (!["completed", "failed"].includes(run.status)) return;
    state.waitingOutcomes.set(run.run_id, 0);
    scheduleReplyPoll();
  }

  /** T11 saves its outcome after the final run snapshot. It is not an answer to a user's question. */
  function isOutcomeMessage(message) {
    if (state.replyMessageIds.has(message.message_id)) return false;
    if (!matchesOutcomeMessage(message)) return false;
    const first = state.messages.find((m) => m.run_id === message.run_id && !state.replyMessageIds.has(m.message_id) && matchesOutcomeMessage(m));
    return !first || first.message_id === message.message_id;
  }

  function matchesOutcomeMessage(message) {
    const run = state.runs.get(message.run_id);
    const cards = message.cards || [];
    if (!run || message.role !== "assistant" || cards.length !== 1 || !isObj(cards[0])) return false;
    return run.status === "completed" && isObj(run.result) && cards[0].kind === "conclusion" && message.text === run.result.conclusion.summary
      || run.status === "failed" && isObj(run.failure) && cards[0].kind === "run_status" && cards[0].status === "failed" && message.text === "Проверка не выполнена. " + run.failure.message;
  }

  function hasReply(message) {
    const index = state.messages.findIndex((m) => m.message_id === message.message_id);
    return index >= 0 && state.messages.slice(index + 1).some((m) => m.role !== "user" && (m.run_id || null) === (message.run_id || null) && !isOutcomeMessage(m));
  }

  async function refreshRuns(gen, signal) {
    try {
      const runs = checkList(await request("GET", workspaceBase() + "/runs", { signal }), checkRun, "список проверок");
      if (gen !== state.gen) return;
      for (const run of runs) {
        upsertRun(run);
        if (ACTIVE.has(run.status)) schedulePoll(run.run_id, POLL_MS);
      }
    } catch (e) {
      if (!isAbort(e) && gen === state.gen) state.startError = "Не удалось обновить список проверок. " + e.message;
    }
  }

  function schedulePoll(runId, delay) {
    if (state.polls.has(runId)) return;
    const gen = state.gen;
    const timer = window.setTimeout(() => pollRun(runId, gen, delay), delay);
    state.polls.set(runId, timer);
  }

  async function pollRun(runId, gen, delay) {
    state.polls.delete(runId);
    if (gen !== state.gen) return;
    const known = state.runs.get(runId);
    if (known && TERMINAL.has(known.status)) return;
    const signal = state.controller.signal;
    try {
      const run = checkRun(
        await request("GET", workspaceBase() + "/runs/" + encodeURIComponent(runId), { signal }),
      );
      if (gen !== state.gen) return;
      expect(run.run_id === runId, "сервер вернул другую проверку");
      state.pollErrors.delete(runId);
      upsertRun(run);
      const current = state.runs.get(runId);
      if (ACTIVE.has(current.status)) schedulePoll(runId, POLL_MS);
      else announce("Проверка №" + runNumber(runId) + ": " + RUN_STATUS[current.status].label);
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      const next = Math.min(delay * 2, POLL_MAX_MS);
      state.pollErrors.set(runId, { message: e.message, retry: Math.round(next / 1000) });
      schedulePoll(runId, next);
    }
    renderAll();
  }

  async function cancelRun(runId) {
    if (state.cancelling.has(runId)) return;
    state.cancelling.add(runId);
    state.runErrors.delete(runId);
    renderTimeline();
    const { gen, signal } = ctx();
    try {
      const run = checkRun(
        await request("POST", workspaceBase() + "/runs/" + encodeURIComponent(runId) + "/cancel", { signal }),
      );
      if (gen !== state.gen) return;
      upsertRun(run);
      announce("Проверка №" + runNumber(runId) + ": " + RUN_STATUS[state.runs.get(runId).status].label);
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      state.runErrors.set(runId, "Отмена не выполнена. " + e.message);
    } finally {
      if (gen === state.gen) {
        state.cancelling.delete(runId);
        renderAll();
      }
    }
  }

  function selectRun(runId, scroll) {
    if (runId !== null && !state.runs.has(runId)) return;
    state.selectedRunId = runId;
    state.noRunChosen = runId === null; // an explicit "no check" choice survives a refresh
    savePrefs();
    showRuns();
    updateRunSelectionMarkers();
    renderComposer();
    if (scroll && runId) {
      const node = document.getElementById(domId("run", runId));
      if (node) node.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  // ======================= conversation =======================

  function setMessages(list) {
    state.messages = [];
    state.messageIds = new Set();
    mergeMessages(list);
  }

  function mergeMessages(list, ordered) {
    if (ordered) {
      const received = new Set(list.map((m) => m.message_id));
      state.messages = [...list, ...state.messages.filter((m) => !received.has(m.message_id))];
      state.messageIds = new Set(state.messages.map((m) => m.message_id));
      return;
    }
    for (const message of list) {
      if (state.messageIds.has(message.message_id)) continue;
      state.messageIds.add(message.message_id);
      state.messages.push(message);
    }
  }

  function submitComposer() {
    if (!ready() || state.sending || state.waitingReplies.size || !state.selectedRunId) return;
    const textarea = $("#composer-text");
    const text = textarea.value.trim();
    if (!text) {
      $("#composer-status").textContent = "Введите вопрос.";
      $("#composer-status").dataset.error = "1";
      return;
    }
    $("#composer-status").dataset.error = "";
    const runId = state.selectedRunId; // captured now: later polling cannot change the target of this question
    const item = {
      localId: "p" + ++state.pendingSeq,
      text,
      runId,
      status: "sending",
      error: null,
      known: new Set(state.messageIds),
    };
    state.pending.push(item);
    textarea.value = "";
    textarea.style.height = "";
    state.scrollTo = "pending:" + item.localId;
    renderPanels();
    sendPending(item);
  }

  async function sendPending(item) {
    if (state.sending) return;
    state.sending = true;
    item.status = "sending";
    item.error = null;
    renderTimeline();
    renderComposer();
    const { gen, signal } = ctx();
    try {
      const response = await request("POST", workspaceBase() + "/messages", {
        json: { text: item.text, run_id: item.runId },
        signal,
      });
      if (gen !== state.gen) return;
      const saved = checkList(response, checkMessage, "ответ на сообщение");
      expect(saved.length >= 1 && saved[0].role === "user", "первым должно идти сохраненное сообщение пользователя");
      removePending(item);
      for (const reply of saved.slice(1)) if (reply.role !== "user") state.replyMessageIds.add(reply.message_id);
      mergeMessages(saved);
      if (saved.length === 1) waitForReply(saved[0].message_id);
      state.scrollTo = "msg:" + saved[saved.length - 1].message_id;
      syncHistory(true);
      announce(saved.length > 1 ? "Получен ответ" : "Сообщение сохранено, ответа нет");
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      await reconcilePending(item, e, gen, signal);
    } finally {
      if (gen === state.gen) {
        state.sending = false;
        renderAll();
      }
    }
  }

  /** After a failed send, ask the server whether the user's message was saved before posting it again. */
  async function reconcilePending(item, error, gen, signal) {
    try {
      const list = checkList(
        await request("GET", workspaceBase() + "/messages", { signal }),
        checkMessage,
        "сообщения",
      );
      if (gen !== state.gen) return;
      const saved = list.find(
        (m) => !item.known.has(m.message_id) && m.role === "user" && m.text === item.text && (m.run_id || null) === (item.runId || null),
      );
      mergeMessages(list, true);
      if (saved) {
        removePending(item);
        if (!hasReply(saved)) waitForReply(saved.message_id);
        announce("Сообщение было сохранено сервером");
      } else {
        item.status = "failed";
        item.error = error.message;
      }
    } catch (e) {
      if (isAbort(e) || gen !== state.gen) return;
      item.status = "unknown";
      item.error = error.message;
    }
  }

  function retryPending(item) {
    if (state.sending || state.waitingReplies.size) return;
    // Check first: if the earlier attempt was saved, retrying must not create a second copy.
    state.sending = true;
    item.status = "checking";
    renderTimeline();
    const { gen, signal } = ctx();
    reconcilePending(item, new ApiError(0, "retry", item.error || ""), gen, signal).then(() => {
      if (gen !== state.gen) return;
      state.sending = false;
      if (state.pending.includes(item) && item.status === "failed") sendPending(item);
      else renderAll();
    });
  }

  function removePending(item) {
    state.pending = state.pending.filter((p) => p !== item);
  }

  function waitForReply(messageId) {
    state.waitingReplies.add(messageId);
    state.replyPollAttempts = 0;
    scheduleReplyPoll();
  }

  function scheduleReplyPoll() {
    if (state.replyPoll !== null || (!state.waitingReplies.size && !state.waitingOutcomes.size)) return;
    const gen = state.gen;
    state.replyPoll = window.setTimeout(async () => {
      state.replyPoll = null;
      if (gen !== state.gen) return;
      try {
        const list = checkList(await request("GET", workspaceBase() + "/messages", { signal: state.controller.signal }), checkMessage, "сообщения");
        if (gen !== state.gen) return;
        mergeMessages(list, true);
        for (const id of state.waitingReplies) {
          const original = state.messages.find((m) => m.message_id === id);
          if (original && hasReply(original)) state.waitingReplies.delete(id);
        }
      } catch (e) {
        if (isAbort(e) || gen !== state.gen) return;
      }
      for (const [runId, attempts] of state.waitingOutcomes) {
        if (state.messages.some((m) => m.run_id === runId && isOutcomeMessage(m)) || attempts >= 5) state.waitingOutcomes.delete(runId);
        else state.waitingOutcomes.set(runId, attempts + 1);
      }
      // Recovery is bounded; a later reload can still retrieve a reply saved after the wait ends.
      state.replyPollAttempts += 1;
      if (state.replyPollAttempts >= 80) state.waitingReplies.clear();
      renderAll();
      scheduleReplyPoll();
    }, POLL_MS);
  }

  // ======================= evidence =======================

  async function openEvidence(blockId, options) {
    const opts = options || {};
    if (!ready()) return;
    const req = state.evidence.req + 1;
    state.evidence = {
      req,
      status: "loading",
      blockId,
      view: null,
      error: null,
      expectVersion: opts.expectVersion || null,
      trigger: opts.trigger || null,
    };
    document.body.classList.add("evidence-open");
    renderPanels();
    showEvidence();
    markEvidenceButtons();
    $("#evidence-body").focus({ preventScroll: true });
    if (!ID_RE.blk.test(blockId)) {
      state.evidence.status = "error";
      state.evidence.error = new ApiError(0, "invalid", "Некорректная ссылка на фрагмент.");
      showEvidence();
      return;
    }
    const { gen, signal } = ctx();
    try {
      const view = checkEvidence(
        await request("GET", workspaceBase() + "/evidence/" + encodeURIComponent(blockId), { signal }),
        blockId,
      );
      if (gen !== state.gen || req !== state.evidence.req) return; // a later click wins
      state.evidence.status = "ready";
      state.evidence.view = view;
    } catch (e) {
      if (isAbort(e) || gen !== state.gen || req !== state.evidence.req) return;
      state.evidence.status = "error";
      state.evidence.error = e;
    }
    showEvidence();
  }

  function closeEvidence() {
    document.body.classList.remove("evidence-open");
    renderPanels();
    const trigger = state.evidence.trigger;
    if (trigger && document.body.contains(trigger)) trigger.focus();
  }

  function reopenEvidence() {
    document.body.classList.add("evidence-open");
    renderPanels();
    $("#evidence-body").focus({ preventScroll: true });
  }

  // ======================= layout: resizable columns, hideable sidebar =======================

  const LIMITS = { side: [200, 480, 280], ev: [300, 760, 420] }; // min, max, default
  const layout = Object.assign({ side: LIMITS.side[2], ev: LIMITS.ev[2], sideHidden: false }, storage.getJson(KEY_LAYOUT) || {});

  const narrow = () => window.matchMedia("(max-width: 759px)").matches;

  function clampWidth(key, value) {
    const [min, max, fallback] = LIMITS[key];
    const room = Math.max(min, Math.round(window.innerWidth * (key === "side" ? 0.4 : 0.5)));
    const number = Number(value);
    return Math.round(Math.min(Math.min(max, room), Math.max(min, Number.isFinite(number) ? number : fallback)));
  }

  function applyLayout() {
    const root = document.documentElement;
    for (const key of ["side", "ev"]) {
      const width = clampWidth(key, layout[key]);
      root.style.setProperty(key === "side" ? "--side-w" : "--ev-w", width + "px");
      const splitter = $(key === "side" ? "#side-splitter" : "#ev-splitter");
      splitter.setAttribute("aria-valuemin", String(LIMITS[key][0]));
      splitter.setAttribute("aria-valuemax", String(LIMITS[key][1]));
      splitter.setAttribute("aria-valuenow", String(width));
    }
    document.body.classList.toggle("side-hidden", Boolean(layout.sideHidden));
    const shown = narrow() ? document.body.classList.contains("side-drawer") : !layout.sideHidden;
    $("#side-show").setAttribute("aria-expanded", shown ? "true" : "false");
  }

  function saveLayout() {
    storage.setJson(KEY_LAYOUT, layout);
  }

  /** A column divider: drag with a pointer, arrows (Shift for bigger steps), Home/End, double-click to reset. */
  function wireSplitter(splitter, key, direction) {
    splitter.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      document.body.classList.add("resizing");
      const startX = event.clientX;
      const startWidth = clampWidth(key, layout[key]);
      const move = (e) => {
        layout[key] = clampWidth(key, startWidth + direction * (e.clientX - startX));
        applyLayout();
      };
      const stop = () => {
        splitter.removeEventListener("pointermove", move);
        splitter.removeEventListener("pointerup", stop);
        splitter.removeEventListener("pointercancel", stop);
        document.body.classList.remove("resizing");
        saveLayout();
      };
      splitter.addEventListener("pointermove", move);
      splitter.addEventListener("pointerup", stop);
      splitter.addEventListener("pointercancel", stop);
    });
    splitter.addEventListener("keydown", (event) => {
      const grow = key === "side" ? "ArrowRight" : "ArrowLeft";
      const shrink = key === "side" ? "ArrowLeft" : "ArrowRight";
      const step = event.shiftKey ? 64 : 16;
      let width = clampWidth(key, layout[key]);
      if (event.key === grow) width += step;
      else if (event.key === shrink) width -= step;
      else if (event.key === "Home") width = LIMITS[key][0];
      else if (event.key === "End") width = LIMITS[key][1];
      else return;
      event.preventDefault();
      layout[key] = clampWidth(key, width);
      applyLayout();
      saveLayout();
    });
    splitter.addEventListener("dblclick", () => {
      layout[key] = LIMITS[key][2];
      applyLayout();
      saveLayout();
    });
  }

  function setSidebar(open) {
    if (narrow()) document.body.classList.toggle("side-drawer", open);
    else {
      layout.sideHidden = !open;
      saveLayout();
    }
    applyLayout();
    const target = open ? $("#ws-new") : $("#side-show");
    if (target) target.focus();
  }

  /** A file picker styled as a drop zone: chosen or dropped files are listed before the upload button is used. */
  function wireDropZone(form) {
    const input = form.querySelector('input[type="file"]');
    const zone = form.querySelector(".drop");
    const list = form.querySelector(".drop-files");
    const show = () => {
      const files = Array.from(input.files || []);
      setChildren(list, files.slice(0, 6).map((f) => h("span", { class: "file-chip" }, f.name)), files.length > 6 ? h("span", { class: "file-chip" }, "ещё " + (files.length - 6)) : null);
      zone.classList.toggle("has-files", files.length > 0);
    };
    input.addEventListener("change", show);
    form.addEventListener("reset", () => window.setTimeout(show, 0));
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragging");
      if (event.dataTransfer && event.dataTransfer.files.length && !input.disabled) {
        input.files = event.dataTransfer.files;
        show();
      }
    });
  }

  async function copyLink() {
    const status = $("#ws-copy-status");
    const link = window.location.origin + window.location.pathname + "#" + state.workspaceId;
    try {
      await navigator.clipboard.writeText(link);
      status.textContent = "Ссылка скопирована.";
    } catch (e) {
      status.textContent = "Не удалось скопировать. Сохраните ссылку из адресной строки браузера.";
    }
  }

  /** Buttons that open evidence. ``opts.label`` names a single fragment (default «Показать»); several are numbered. */
  function evidenceButtons(ids, opts) {
    const options = opts || {};
    const list = Array.isArray(ids) ? ids.filter((x) => typeof x === "string") : [];
    if (!list.length) return null;
    return h(
      "span",
      { class: "ev-list" },
      list.map((id, index) => {
        const text = list.length === 1 ? options.label || "Показать" : "Фрагмент " + (index + 1);
        const button = h(
          "button",
          {
            type: "button",
            class: "ev-btn",
            "data-block-id": id,
            "data-fkey": "ev:" + (options.key ? options.key + ":" : "") + id,
            "aria-label": (options.context ? options.context + ": " : "") + text.toLowerCase(),
          },
          svg("icon", "0 0 20 20", "M8.8 14.6 a5.8 5.8 0 1 1 0 -11.6 a5.8 5.8 0 0 1 0 11.6 Z M13 13 L17 17"),
          text,
        );
        button.addEventListener("click", () => openEvidence(id, { expectVersion: options.expectVersion, trigger: button }));
        return button;
      }),
    );
  }

  function markEvidenceButtons() {
    const current = state.evidence.blockId;
    for (const button of document.querySelectorAll(".ev-btn")) {
      button.classList.toggle("ev-current", button.getAttribute("data-block-id") === current);
    }
  }

  // ======================= rendering: header, workspace, documents =======================

  /** Sections are rebuilt only when their inputs change, so polling does not reset open sections, chosen files,
   * focus or the evidence image. */
  const rendered = {};

  function renderOnce(name, signature, render) {
    const sig = JSON.stringify(signature);
    if (rendered[name] === sig) return;
    rendered[name] = sig;
    render();
  }

  function runsSignature() {
    return runsInOrder().map((r) => [r.run_id, r.status, r.updated_at]);
  }

  const base = () => [state.workspaceId, state.wsStatus];

  function currentHistoryItem() {
    return loadHistory().find((x) => x.id === state.workspaceId) || { id: state.workspaceId };
  }

  function showWorkspace() {
    const title = ready() ? historyTitle(currentHistoryItem()) : null;
    renderOnce(
      "workspace",
      [...base(), title, state.renaming, state.wsError && state.wsError.message, state.wsCreating, state.wsCreateError && state.wsCreateError.message, storage.available],
      renderWorkspace,
    );
    renderOnce("history", [state.workspaceId, state.wsStatus, storage.get(KEY_HISTORY), new Date().toDateString()], renderHistory);
  }

  function showDocs() {
    renderOnce("docs", [...base(), state.documents.map((v) => v.version_id), state.replace], renderDocList);
  }

  function showRuns() {
    renderOnce("followup", [...base(), runsSignature(), state.selectedRunId], renderFollowupSelect);
  }

  function renderPanels() {
    const setupOpen = ready() && state.setupOpen;
    // A new reconciliation has nothing to converse about yet: the setup takes the whole column.
    const empty = !state.runOrder.length && !state.messages.length && !state.pending.length;
    $(".chat").classList.toggle("setup-first", setupOpen && empty);
    const panel = $("#setup-panel");
    const setupButton = $("#setup-toggle");
    panel.hidden = !setupOpen;
    setupButton.hidden = !ready();
    setupButton.setAttribute("aria-expanded", setupOpen ? "true" : "false");
    const docs = new Set(state.documents.map((v) => v.document_id)).size;
    setupButton.textContent = setupOpen ? "Свернуть документы" : docs ? "Документы: " + docs : "Документы и параметры";
    setupButton.title = "Документы и параметры проверки";
    const evidenceOpen = document.body.classList.contains("evidence-open");
    const evidenceButton = $("#evidence-toggle");
    evidenceButton.hidden = !ready() || state.evidence.status === "idle";
    evidenceButton.setAttribute("aria-expanded", evidenceOpen ? "true" : "false");
    evidenceButton.textContent = evidenceOpen ? "Скрыть фрагмент" : "Показать фрагмент";
  }

  function showEvidence() {
    renderOnce("evidence", [...base(), state.evidence.req, state.evidence.status, state.documents.length], renderEvidence);
  }

  function showMode() {
    renderOnce("mode", [state.health && state.health.run_mode, state.health && state.health.provider && state.health.provider.configured, state.healthError], renderMode);
  }

  function renderAll() {
    syncHistory(false);
    showMode();
    showWorkspace();
    renderPanels();
    renderUploads();
    showDocs();
    renderScope();
    showRuns();
    renderTimeline();
    renderComposer();
    showEvidence();
  }

  function renderMode() {
    const el = $("#mode-banner");
    const mode = state.health && state.health.run_mode;
    if (state.healthError) setChildren(el, h("p", null, "Сервер не отвечает на проверку состояния."));
    else if (mode === "fixture")
      setChildren(el, h("p", null, h("strong", null, "Демонстрационный режим. "), "Проверка показывает пример результата; документы не анализируются."));
    else if (mode === "disabled")
      setChildren(el, h("p", null, h("strong", null, "Проверка отключена. "), "Обратитесь к организатору сверки, чтобы включить ее."));
    else if (mode === "live" && state.health.provider && state.health.provider.configured === false)
      setChildren(el, h("p", null, h("strong", null, "Модель не подключена. "), "Для проверки документов обратитесь к организатору сверки."));
    else el.replaceChildren();
  }

  const PENCIL_ICON = "M4 16 L4.6 12.8 L12.9 4.5 C13.5 3.9 14.5 3.9 15.1 4.5 L15.5 4.9 C16.1 5.5 16.1 6.5 15.5 7.1 L7.2 15.4 Z";

  /** Chat header: the reconciliation's title (renameable), or what to do when there is none or it failed. */
  function renderWorkspace() {
    const status = $("#ws-status");
    const parts = [];
    if (state.wsStatus === "none") {
      parts.push(
        h("p", { class: "ws-title" }, "Сверка не выбрана"),
        h("p", { class: "small mute" }, "Начните новую сверку или откройте сохраненную в списке слева."),
      );
    } else if (state.wsStatus === "loading") {
      parts.push(h("p", { class: "ws-title mute" }, "Открываю сверку…"));
    } else if (ready()) {
      parts.push(state.renaming ? renameForm() : titleRow());
    } else {
      parts.push(
        h(
          "div",
          { class: "alert alert-bad", role: "alert" },
          h("p", null, "Эту сверку не удалось открыть. ", state.wsError ? state.wsError.message : ""),
          h("p", { class: "small" }, "Она остается в списке слева. Новая сверка создается только кнопкой «Новая сверка»."),
          h("button", { type: "button", "data-fkey": "ws-retry", onclick: () => openWorkspace(state.workspaceId) }, "Повторить"),
        ),
      );
    }
    if (!storage.available) {
      parts.push(
        h("p", { class: "alert alert-warn small" }, "Браузер не сохраняет список сверок. Чтобы вернуться к этой сверке, сохраните ссылку на страницу."),
      );
    }
    if (state.wsCreateError) {
      parts.push(h("p", { class: "alert alert-bad", role: "alert" }, "Новую сверку создать не удалось. " + state.wsCreateError.message));
    }
    setChildren(status, parts);
    // The open reconciliation is exposed only as an attribute (for links and tests), never as text.
    if (state.workspaceId) document.body.setAttribute("data-workspace", state.workspaceId);
    else document.body.removeAttribute("data-workspace");
    $("#ws-copy").hidden = !ready();
    const button = $("#ws-new");
    button.disabled = state.wsCreating;
    button.textContent = state.wsCreating ? "Создание…" : "Новая сверка";
  }

  function titleRow() {
    return h(
      "div",
      { class: "title-row" },
      h("p", { class: "ws-title" }, historyTitle(currentHistoryItem())),
      h(
        "button",
        {
          type: "button",
          class: "icon-btn",
          "aria-label": "Переименовать сверку",
          title: "Переименовать",
          "data-fkey": "rename",
          onclick: () => {
            state.renaming = true;
            showWorkspace();
            const input = $("#ws-rename");
            if (input) input.select();
          },
        },
        svg("icon", "0 0 20 20", PENCIL_ICON),
      ),
    );
  }

  function renameForm() {
    const input = h("input", { id: "ws-rename", value: historyTitle(currentHistoryItem()), maxlength: "120", "aria-label": "Название сверки" });
    const finish = (save) => {
      if (save) updateHistory(state.workspaceId, { custom: input.value.trim() || null }, false);
      state.renaming = false;
      showWorkspace();
      const again = document.querySelector('[data-fkey="rename"]');
      if (again) again.focus();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    return h(
      "form",
      { class: "rename", onsubmit: (event) => (event.preventDefault(), finish(true)) },
      input,
      h("button", { type: "submit", class: "small primary" }, "Сохранить"),
      h("button", { type: "button", class: "small ghost", onclick: () => finish(false) }, "Отмена"),
    );
  }

  function shortTime(iso) {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const d = new Date(t);
    const today = new Date().toDateString() === d.toDateString();
    return today
      ? d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
  }

  /** Saved reconciliations, newest activity first, grouped like a chat history. */
  function renderHistory() {
    const nav = $("#ws-history");
    const list = loadHistory();
    if (!list.length) {
      setChildren(nav, h("p", { class: "history-empty" }, "Сохраненных сверок пока нет. Нажмите «Новая сверка», чтобы загрузить отчет и первичные документы."));
      return;
    }
    const startOfToday = new Date(new Date().toDateString()).getTime();
    const groups = [
      ["Сегодня", []],
      ["Вчера", []],
      ["Ранее", []],
    ];
    for (const item of list) {
      const t = Date.parse(item.updated || "");
      const index = Number.isNaN(t) ? 2 : t >= startOfToday ? 0 : t >= startOfToday - 86400000 ? 1 : 2;
      groups[index][1].push(item);
    }
    setChildren(
      nav,
      groups
        .filter((group) => group[1].length)
        .map(([title, items]) =>
          h("section", { class: "history-group" }, h("h2", null, title), h("ul", null, items.map((item) => h("li", null, historyButton(item))))),
        ),
    );
  }

  function historyButton(item) {
    const current = item.id === state.workspaceId;
    const info = item.status ? RUN_STATUS[item.status] : null;
    return h(
      "button",
      {
        type: "button",
        class: "hist-item" + (current ? " current" : "") + (item.unavailable ? " unavailable" : ""),
        "aria-current": current ? "page" : null,
        "data-workspace-id": item.id,
        "data-fkey": "hist:" + item.id,
        onclick: () => {
          document.body.classList.remove("side-drawer");
          if (!current || !ready()) openWorkspace(item.id);
        },
      },
      h("span", { class: "hist-title" }, historyTitle(item)),
      h(
        "span",
        { class: "hist-meta" },
        item.unavailable ? h("span", { class: "warn-text" }, "Не открывается") : info ? badge(info, item.status) : h("span", null, "Проверок не было"),
        item.files ? h("span", { class: "hist-files" }, item.files + " " + plural(item.files, "файл", "файла", "файлов")) : null,
        item.updated ? h("span", { class: "hist-time" }, shortTime(item.updated)) : null,
      ),
    );
  }

  function renderUploads() {
    for (const form of document.querySelectorAll("form.upload")) {
      const role = form.dataset.role;
      const slot = state.uploads[role];
      const disabled = !ready() || slot.busy;
      form.querySelector('input[type="file"]').disabled = disabled;
      const button = form.querySelector('button[type="submit"]');
      button.disabled = disabled;
      button.setAttribute("aria-busy", slot.busy ? "true" : "false");
      const status = form.querySelector(".upload-status");
      renderOnce("upload-" + role, [...base(), slot], () => {
        if (slot.busy) setChildren(status, h("span", { class: "mute" }, "Загрузка…"));
        else if (slot.error) setChildren(status, h("span", { class: "alert alert-bad" }, "Не загружено. " + slot.error));
        else if (slot.message) setChildren(status, h("span", { class: "ok-text" }, slot.message));
        else status.replaceChildren();
      });
    }
  }

  function renderDocList() {
    const container = $("#doc-list");
    if (!ready()) {
      container.replaceChildren();
      return;
    }
    if (!state.documents.length) {
      setChildren(container, h("p", { class: "mute" }, "Документы еще не загружены."));
      return;
    }
    const groups = [];
    for (const role of ["report", "primary", "context"]) {
      const docs = new Map();
      for (const v of state.documents) {
        if (v.role !== role) continue;
        if (!docs.has(v.document_id)) docs.set(v.document_id, []);
        docs.get(v.document_id).push(v);
      }
      if (!docs.size) continue;
      const nameCount = new Map();
      for (const versions of docs.values()) {
        const name = versions[versions.length - 1].original_filename;
        nameCount.set(name, (nameCount.get(name) || 0) + 1);
      }
      groups.push(
        h(
          "div",
          { class: "doc-group" },
          h("h3", null, ROLE_TITLE[role], " ", h("span", { class: "mute" }, "(" + docs.size + ")")),
          h(
            "ul",
            { class: "docs" },
            Array.from(docs.entries()).map(([docId, versions]) => renderDocument(role, docId, versions, nameCount)),
          ),
        ),
      );
    }
    setChildren(container, groups);
  }

  function renderDocument(role, docId, versions, nameCount) {
    const latest = versions[versions.length - 1];
    const older = versions.slice(0, -1).reverse();
    const sameName = nameCount.get(latest.original_filename) > 1;
    const replacing = state.replace.docId === docId;
    const item = h(
      "li",
      { class: "doc", "data-document-id": docId },
      versionRow(role, latest, versions.length, versions.length),
      sameName
        ? h("p", { class: "small note" }, "Такое же имя у другого документа; это отдельный документ.")
        : null,
      older.length
        ? h(
            "details",
            {
              class: "older",
              open: state.openOlder.has(docId),
              ontoggle: (event) => {
                if (event.target.open) state.openOlder.add(docId);
                else state.openOlder.delete(docId);
              },
            },
            h("summary", null, "Предыдущие версии: " + older.length),
            h(
              "ul",
              null,
              older.map((v) => h("li", null, versionRow(role, v, versions.indexOf(v) + 1, versions.length))),
            ),
          )
        : null,
      replaceControls(docId, latest, replacing),
    );
    return item;
  }

  function versionRow(role, v, index, count) {
    const sel = state.selection;
    const checked = role === "report" ? sel.report === v.version_id : sel[role].has(v.version_id);
    const inputId = domId("sel", v.version_id);
    const input = h("input", {
      type: role === "report" ? "radio" : "checkbox",
      name: role === "report" ? "sel-report" : null,
      id: inputId,
      checked,
      "data-version-id": v.version_id,
      "data-role": role,
      "data-fkey": "sel:" + v.version_id,
    });
    input.checked = checked;
    const successor = state.documents.find((x) => x.predecessor_version_id === v.version_id);
    return h(
      "div",
      { class: "version" + (successor ? " superseded" : ""), "data-version-id": v.version_id },
      input,
      h(
        "label",
        { for: inputId },
        h("span", { class: "fname" }, v.original_filename),
        h(
          "span",
          { class: "vmeta meta-row" },
          h("span", null, "версия " + index + " из " + count),
          h("span", null, String(v.format).toUpperCase() + (v.size_bytes !== undefined ? ", " + formatSize(v.size_bytes) : "")),
          h("span", null, formatTime(v.created_at)),
        ),
        v.predecessor_version_id ? h("span", { class: "vmeta" }, "Заменяет версию от " + versionTime(v.predecessor_version_id)) : null,
        successor ? h("span", { class: "vmeta warn-text" }, "Устарела: есть новая версия от " + formatTime(successor.created_at)) : null,
      ),
    );
  }

  function replaceControls(docId, latest, open) {
    if (!open) {
      return h(
        "button",
        {
          type: "button",
          class: "link small",
          "data-fkey": "replace-open:" + docId,
          onclick: () => {
            state.replace = { docId, busy: false, error: null };
            showDocs();
            const input = document.getElementById(domId("replace-file", docId));
            if (input) input.focus();
          },
        },
        "Загрузить новую версию",
      );
    }
    const fileId = domId("replace-file", docId);
    const input = h("input", { type: "file", id: fileId, "data-fkey": "replace-file:" + docId, disabled: state.replace.busy });
    return h(
      "div",
      { class: "replace" },
      h("label", { for: fileId }, "Новая версия документа «" + latest.original_filename + "» (" + ROLE[latest.role] + ")"),
      input,
      h(
        "div",
        { class: "row-actions" },
        h(
          "button",
          {
            type: "button",
            "data-fkey": "replace-go:" + docId,
            disabled: state.replace.busy,
            onclick: () => {
              const file = input.files && input.files[0];
              if (!file) {
                state.replace.error = "Выберите файл.";
                showDocs();
                return;
              }
              uploadReplacement(docId, file);
            },
          },
          state.replace.busy ? "Загрузка…" : "Загрузить как новую версию",
        ),
        h(
          "button",
          {
            type: "button",
            class: "ghost",
            "data-fkey": "replace-cancel:" + docId,
            disabled: state.replace.busy,
            onclick: () => {
              state.replace = { docId: null, busy: false, error: null };
              showDocs();
            },
          },
          "Отмена",
        ),
      ),
      h("p", { class: "mute small" }, "Старая версия и результаты прежних проверок сохраняются."),
      state.replace.error ? h("p", { class: "alert alert-bad", role: "alert" }, state.replace.error) : null,
    );
  }

  // ======================= rendering: scope and runs =======================

  function renderScope() {
    const sel = state.selection;
    const report = sel.report ? versionById(sel.report) : null;
    const primaries = state.documents.filter((v) => v.role === "primary" && sel.primary.has(v.version_id));
    const contexts = state.documents.filter((v) => v.role === "context" && sel.context.has(v.version_id));
    const scope = readScopeInputs();
    const ids = selectedVersionIds();
    const byDoc = new Map();
    for (const id of ids) {
      const v = versionById(id);
      if (v) byDoc.set(v.document_id, (byDoc.get(v.document_id) || 0) + 1);
    }
    const multi = Array.from(byDoc.values()).some((n) => n > 1);
    const period =
      validIsoDate(scope.date_from) && validIsoDate(scope.date_to)
        ? formatIsoDate(scope.date_from) + " – " + formatIsoDate(scope.date_to)
        : "не указан";
    renderOnce("scope-summary", [...base(), ids, scope, sel.report], () => setChildren(
      $("#scope-summary"),
      ready()
        ? [
            h(
              "dl",
              { class: "kv" },
              h("dt", null, "Отчет"),
              h("dd", null, report ? report.original_filename : h("span", { class: "warn-text" }, "не выбран")),
              h("dt", null, "Первичные"),
              h("dd", null, String(primaries.length)),
              h("dt", null, "Справочные"),
              h("dd", null, String(contexts.length)),
              h("dt", null, "Договор"),
              h("dd", null, scope.contract || "не указан"),
              h("dt", null, "Строка"),
              h("dd", null, scope.report_row || "не указана"),
              h("dt", null, "Период"),
              h("dd", null, period),
            ),
            ids.length
              ? h(
                  "details",
                  null,
                  h("summary", null, "Документы для проверки: " + ids.length),
                  h(
                    "ol",
                    { class: "small" },
                    ids.map((id) => {
                      const v = versionById(id);
                      return h("li", null, v ? v.original_filename + " (" + ROLE[v.role] + ")" : "документ не найден");
                    }),
                  ),
                )
              : null,
            multi ? h("p", { class: "alert alert-warn small" }, "Выбрано несколько версий одного документа.") : null,
          ]
        : null,
    ));
    $("#scope-confirm").checked = state.confirmed;
    $("#scope-confirm").disabled = !ready();
    for (const input of Object.values(scopeInputs())) input.disabled = !ready();
    const blockers = startBlockers();
    const button = $("#run-start");
    button.disabled = blockers.length > 0;
    button.setAttribute("aria-busy", state.starting ? "true" : "false");
    button.textContent = state.starting ? "Запуск…" : "Запустить проверку";
    renderOnce("blockers", [...base(), blockers], () => setChildren($("#run-blockers"), blockers.map((b) => h("li", null, b))));
    renderOnce("start-status", [...base(), state.startError, state.startNotice], () => {
      const status = $("#run-start-status");
      if (state.startError) setChildren(status, h("p", { class: "alert alert-bad", role: "alert" }, state.startError));
      else if (state.startNotice) setChildren(status, h("p", { class: "alert alert-warn", role: "alert" }, state.startNotice));
      else status.replaceChildren();
    });
  }

  function renderFollowupSelect() {
    const select = $("#followup-run");
    const runs = runsInOrder();
    const describe = (run) => "проверке №" + runNumber(run.run_id) + " (" + RUN_STATUS[run.status].label.toLowerCase() + ", " + formatTime(run.created_at) + ")";
    select.replaceChildren(...runs.map((run) => h("option", { value: run.run_id }, describe(run))));
    select.value = state.selectedRunId || "";
    select.disabled = !ready() || runs.length < 2;
    select.hidden = runs.length < 2;
    const single = $("#followup-single");
    single.hidden = runs.length !== 1;
    single.textContent = runs.length === 1 ? displayText(describe(runs[0])) : "";
    $(".composer-target").hidden = !ready() || runs.length === 0;
  }

  function renderComposer() {
    const button = $("#composer-send");
    const textarea = $("#composer-text");
    const hasRuns = state.runOrder.length > 0;
    const usable = ready() && hasRuns && state.selectedRunId !== null;
    const waiting = state.sending || state.waitingReplies.size > 0;
    button.disabled = !usable || waiting;
    button.setAttribute("aria-busy", waiting ? "true" : "false");
    textarea.disabled = !usable;
    textarea.placeholder = !ready()
      ? "Откройте или начните сверку"
      : hasRuns
        ? "Спросите о результате проверки"
        : "Вопросы можно задать после первой проверки";
    const status = $("#composer-status");
    const run = state.selectedRunId ? state.runs.get(state.selectedRunId) : null;
    if (waiting) status.textContent = "Ревизор готовит ответ… Следующий вопрос можно отправить после ответа.";
    else if (run && ACTIVE.has(run.status)) status.textContent = "Проверка №" + runNumber(run.run_id) + " еще идет: ответ опирается на ее текущее состояние.";
    else if (status.dataset.error !== "1") status.textContent = "";
  }

  // ======================= rendering: timeline =======================

  function timelineItems() {
    const items = [];
    const runs = runsInOrder();
    let ri = 0;
    state.messages.forEach((message, index) => {
      const t = Date.parse(message.created_at);
      while (ri < runs.length && !(Date.parse(runs[ri].created_at) > t)) {
        items.push({ key: "run:" + runs[ri].run_id, run: runs[ri] });
        ri += 1;
      }
      items.push({ key: "msg:" + message.message_id, message, index });
    });
    while (ri < runs.length) {
      items.push({ key: "run:" + runs[ri].run_id, run: runs[ri] });
      ri += 1;
    }
    for (const item of state.pending) items.push({ key: "pending:" + item.localId, pending: item });
    return items;
  }

  function itemSignature(item) {
    if (item.run) {
      const run = item.run;
      const pollError = state.pollErrors.get(run.run_id);
      return [
        run.status,
        run.updated_at,
        run.stage,
        state.cancelling.has(run.run_id),
        state.runErrors.get(run.run_id) || "",
        pollError ? pollError.message + pollError.retry : "",
      ].join("|");
    }
    if (item.message) {
      return item.message.role === "user" ? (hasReply(item.message) ? "answered" : state.waitingReplies.has(item.message.message_id) ? "waiting" : "open") : "fixed";
    }
    return item.pending.status + "|" + (item.pending.error || "");
  }

  function renderTimeline() {
    const container = $("#timeline");
    if (!ready()) {
      setChildren(
        container,
        h(
          "div",
          { class: "intro" },
          h("p", null, "Создайте или откройте рабочее пространство, чтобы загрузить документы и запустить проверку."),
        ),
      );
      state.nodes = new Map();
      return;
    }
    const items = timelineItems();
    const wanted = new Set(items.map((i) => i.key));
    for (const [key, entry] of state.nodes) {
      if (!wanted.has(key)) {
        entry.node.remove();
        state.nodes.delete(key);
      }
    }
    for (const child of Array.from(container.children)) if (!child.hasAttribute("data-key")) child.remove();
    let previous = null;
    for (const item of items) {
      const sig = itemSignature(item);
      let entry = state.nodes.get(item.key);
      if (!entry || entry.sig !== sig) {
        const node = buildItem(item);
        node.setAttribute("data-key", item.key);
        if (entry) replaceNode(entry.node, node);
        entry = { node, sig };
        state.nodes.set(item.key, entry);
      }
      const expectedPosition = previous ? previous.nextSibling : container.firstChild;
      if (expectedPosition !== entry.node) container.insertBefore(entry.node, expectedPosition);
      previous = entry.node;
    }
    if (!items.length) {
      container.append(
        h(
          "div",
          { class: "intro" },
          h("p", null, "Загрузите отчет и первичные документы, подтвердите параметры и запустите проверку."),
          h("p", { class: "mute" }, "Вывод и все показатели появятся здесь; по ним можно задавать вопросы."),
        ),
      );
    }
    updateRunSelectionMarkers();
    markEvidenceButtons();
    if (state.scrollTo) {
      const target = state.nodes.get(state.scrollTo);
      state.scrollTo = null;
      if (target) target.node.scrollIntoView({ block: "start" });
    }
  }

  function buildItem(item) {
    if (item.run) return renderRunBlock(item.run);
    if (item.message) return renderMessage(item.message, item.index);
    return renderPending(item.pending);
  }

  function updateRunSelectionMarkers() {
    for (const [key, entry] of state.nodes) {
      if (!key.startsWith("run:")) continue;
      const runId = key.slice(4);
      const slot = entry.node.querySelector(".sel-slot");
      if (!slot) continue;
      const selected = runId === state.selectedRunId;
      entry.node.classList.toggle("selected", selected);
      setChildren(
        slot,
        selected
          ? h("span", { class: "badge tone-active" }, "Вопросы задаются по этой проверке")
          : h(
              "button",
              { type: "button", class: "link", "data-fkey": "select-run:" + runId, onclick: () => selectRun(runId, false) },
              "Задавать вопросы по этой проверке",
            ),
      );
    }
  }

  function avatar() {
    return h("span", { class: "avatar", "aria-hidden": "true" }, svg("icon", "0 0 20 20", MARKS.tick));
  }

  function renderRunBlock(run) {
    const number = runNumber(run.run_id);
    const scope = run.scope || {};
    const inputs = Array.isArray(run.input_version_ids) ? run.input_version_ids : [];
    const active = ACTIVE.has(run.status);
    const titleId = domId("run-title", run.run_id);
    return h(
      "article",
      { class: "msg msg-assistant run-block" + (active ? " is-active" : ""), id: domId("run", run.run_id), "data-run-id": run.run_id, "aria-labelledby": titleId },
      avatar(),
      h(
        "div",
        { class: "msg-body" },
        h(
          "header",
          { class: "run-head" },
          h("h3", { id: titleId }, "Проверка №" + number),
          badge(RUN_STATUS[run.status], run.status),
          active ? h("span", { class: "badge tone-active" }, "Активная проверка") : null,
          isFixtureRun(run)
            ? h("span", { class: "stamp synthetic", role: "note", title: "Пример: документы не анализировались, значения и фрагменты не относятся к загруженным файлам." }, "Пример результата")
            : null,
          h("span", { class: "vmeta" }, formatTime(run.created_at)),
          h("span", { class: "sel-slot" }),
        ),
        h(
          "p",
          { class: "scope-line" },
          "Договор ",
          h("strong", null, String(scope.contract || "")),
          ", строка «",
          String(scope.report_row || ""),
          "», период ",
          formatIsoDate(scope.date_from),
          "–",
          formatIsoDate(scope.date_to),
          ".",
        ),
        h(
          "details",
          { class: "inputs" },
          h("summary", null, "Документы проверки: " + inputs.length),
          h(
            "ul",
            { class: "small" },
            inputs.map((id) => {
              const v = versionById(id);
              return h("li", null, v ? v.original_filename + " (" + ROLE[v.role] + ")" : "документ не найден в списке");
            }),
          ),
        ),
        run.predecessor_run_id && state.runs.has(run.predecessor_run_id)
          ? h("p", { class: "small" }, "Повторная проверка после проверки №" + runNumber(run.predecessor_run_id) + ".")
          : null,
        renderProgress(run),
        renderIssues(run),
        run.failure ? renderFailure(run.failure) : null,
        state.runErrors.get(run.run_id) ? h("p", { class: "alert alert-bad", role: "alert" }, state.runErrors.get(run.run_id)) : null,
        active
          ? h(
              "div",
              { class: "row-actions" },
              h(
                "button",
                {
                  type: "button",
                  class: "danger",
                  "data-fkey": "cancel:" + run.run_id,
                  disabled: state.cancelling.has(run.run_id),
                  onclick: () => cancelRun(run.run_id),
                },
                state.cancelling.has(run.run_id) ? "Отмена…" : "Отменить проверку",
              ),
            )
          : null,
        run.status === "completed" && isObj(run.result) ? [renderCompletion(run), renderResult(run)] : null,
        run.status === "completed" && !isObj(run.result) ? h("p", { class: "alert alert-bad" }, "Проверка завершена, но результат отсутствует.") : null,
      ),
    );
  }

  /** A saved result is also the source of its automatic chat commentary: reloads cannot duplicate it. */
  function renderCompletion(run) {
    const indicators = run.result.indicators;
    const counts = (status) => indicators.filter((i) => i.status === status).length;
    const discrepancy = counts("discrepancy");
    const missing = counts("insufficient_evidence");
    const question = discrepancy ? "Объясни найденные расхождения и покажи подтверждающие документы."
      : missing ? "Каких документов не хватает для полной проверки?"
        : "Покажи, какие документы подтверждают итог отчета.";
    return h(
      "div",
      { class: "run-completion", role: "status", "aria-live": "polite" },
      h("p", null, h("strong", null, "Проверка №" + runNumber(run.run_id) + " завершена."), isFixtureRun(run) ? " Это пример: загруженные документы не анализировались." : " Результат готов."),
      h("p", null, "Показателей: " + indicators.length + ". Подтверждено: " + counts("verified") + "; расхождений: " + discrepancy + "; недостаточно данных: " + missing + "."),
      h("p", { class: "small mute" }, "Откройте показатель, чтобы увидеть расчет и документы. Отчет можно свернуть и продолжить разговор ниже."),
      h("button", {
        type: "button", class: "suggestion", onclick: () => {
          selectRun(run.run_id, false);
          const textarea = $("#composer-text");
          textarea.value = question;
          textarea.dispatchEvent(new Event("input"));
          textarea.focus();
        },
      }, discrepancy ? "Объяснить расхождения" : missing ? "Каких документов не хватает?" : "Показать подтверждения"),
    );
  }

  /** What the assistant is doing, from the run's real stage counters: no percentages, no time estimates. */
  function renderProgress(run) {
    const progress = new Map((run.progress || []).filter((p) => isObj(p) && STAGES.includes(p.stage)).map((p) => [p.stage, p]));
    const active = ACTIVE.has(run.status);
    const pollError = state.pollErrors.get(run.run_id);
    const parts = [];
    if (active || progress.size) {
      const rows = STAGES.filter((stage) => active || progress.has(stage)).map((stage) => {
        const p = progress.get(stage);
        const current = active && stage === run.stage;
        const done = !current && p && p.total > 0 && p.done >= p.total;
        const step = current ? "current" : done ? "done" : p ? "partial" : "pending";
        const counts = [];
        if (p && p.total > 0) counts.push(p.done + " из " + p.total);
        else if (p && p.done > 0) counts.push(String(p.done));
        if (p && p.failed > 0) counts.push("с ошибкой " + p.failed);
        return h(
          "li",
          { "data-stage": stage, "data-state": step, class: p && p.failed > 0 ? "has-failed" : null },
          h("span", { class: "step-icon", "aria-hidden": "true" }, step === "done" ? mark("tick") : step === "current" ? h("span", { class: "spinner" }) : null),
          h("span", { class: "stage" }, STAGE_LABEL[stage]),
          counts.length ? h("span", { class: "stage-count" }, counts.join(", ")) : null,
        );
      });
      const list = h("ul", { class: "activity" }, rows);
      if (active) {
        parts.push(
          h("p", { class: "activity-title", role: "status" }, ACTIVITY[run.stage] ? "Сейчас: " + ACTIVITY[run.stage].toLowerCase() + "." : "Проверка ждет своей очереди."),
          list,
          h("p", { class: "hint" }, "Ход проверки обновляется сам."),
        );
      } else {
        const failed = Array.from(progress.values()).filter((p) => p.failed > 0).length;
        parts.push(h("details", { class: "progress-done" }, h("summary", null, "Ход проверки" + (failed ? ", этапов с ошибками: " + failed : "")), list));
      }
    }
    if (pollError) {
      parts.push(h("p", { class: "alert alert-warn small", role: "status" }, "Не удалось обновить состояние. " + pollError.message + " Повтор через " + pollError.retry + " с."));
    }
    return parts.length ? h("div", { class: "run-progress" }, parts) : null;
  }

  /** Russian text for a run note. Backend notes are English sentences that may quote Russian names, so known
   *  codes are translated and any other English sentence falls back to a neutral line. */
  const FAILURE_REASON = {
    timeout: "истекло время",
    budget_exhausted: "исчерпан лимит запросов к модели",
    provider_error: "ошибка сервиса модели",
    missing_credentials: "модель не подключена",
    cancelled: "проверка отменена",
    invalid_input: "некорректные входные данные",
    internal_error: "внутренняя ошибка",
    unreadable: "файл не читается",
    unsupported: "формат не поддерживается",
  };

  /** "col15" -> "гр. 15"; other internal entry names are not shown. */
  function columnName(entry) {
    const m = /^col(\d+)$/.exec(String(entry || "").trim());
    return m ? "гр. " + m[1] : null;
  }

  function issueText(issue) {
    const text = String(issue.message || "");
    const file = (re) => {
      const m = re.exec(text);
      return m ? "«" + displayText(m[1]) + "»" : null;
    };
    const quoted = /«(.+?)»/.exec(text);
    switch (issue.code) {
      case "row_label_not_found":
        return quoted
          ? "Подпись строки «" + displayText(quoted[1]) + "» не найдена в распознанном тексте отчета."
          : "Подпись проверяемой строки не найдена в распознанном тексте отчета.";
      case "reinspection_incomplete": {
        const name = file(/^(.+?): \d+ reinspection/);
        return (name ? "По файлу " + name : "По одному из файлов") + " не все повторные проверки выполнены; спорные значения оставлены непроверенными.";
      }
      case "document_unreadable": {
        const name = file(/^(.+?): /);
        const reason = /: unsupported:/.test(text) ? " (" + FAILURE_REASON.unsupported + ")" : "";
        return (name ? "Файл " + name : "Один из файлов") + " не удалось прочитать" + reason + "; его данные в проверке не учтены.";
      }
      case "extraction_failed": {
        const name = file(/^(.+?): no validated extraction/);
        const code = /no validated extraction \((\w+)\)/.exec(text);
        const reason = code && FAILURE_REASON[code[1]] ? " (" + FAILURE_REASON[code[1]] + ")" : "";
        return (name ? "По файлу " + name : "По одному из файлов") + " модель не дала проверенных значений" + reason + "; его данные в проверке не учтены.";
      }
      case "trace_incomplete":
        return "Журнал обращений к модели записан не полностью; на результат проверки это не влияет.";
      case "no_indicators":
        return "Модель не предложила ни одного показателя проверяемой строки.";
      case "column_gap": {
        const cols = /column\(s\) (.+) of the row/.exec(text);
        return cols ? "Для граф " + cols[1] + " проверяемой строки значения не предложены." : "Для части граф проверяемой строки значения не предложены.";
      }
      case "duplicate_entry": {
        const col = quoted ? columnName(quoted[1]) : null;
        if (/header is dropped/.test(text)) return "Поле шапки отчета повторяет уже использованное обозначение и не учтено.";
        return (col ? "Показатель " + col : "Один из показателей") + " предложен дважды; учтен первый вариант.";
      }
      case "duplicate_category": {
        const cols = (/: (.+)\.$/.exec(text) || [, ""])[1].split(",").map(columnName).filter(Boolean);
        return (
          (quoted ? "Статья «" + displayText(quoted[1]) + "»" : "Одна статья затрат") +
          " отнесена сразу к нескольким показателям" + (cols.length ? ": " + cols.join(", ") : "") + "."
        );
      }
      case "component_cycle": {
        const col = quoted ? columnName(quoted[1]) : null;
        return "Составные части показателя" + (col ? " " + col : "") + " ссылаются друг на друга по кругу и не учтены.";
      }
      case "normalized_value":
        return quoted ? "«" + displayText(quoted[1]) + "» — сокращенная запись напечатанного текста." : "Значение записано сокращенно по сравнению с напечатанным текстом.";
      case "ocr_text_differs":
        return quoted ? "Распознанный текст немного отличается от «" + displayText(quoted[1]) + "»." : "Распознанный текст немного отличается от значения.";
      case "category_uncertain":
        return "Статья затрат документа не установлена однозначно.";
      case "amount_words_mismatch":
        return "Сумма прописью не совпадает с суммой цифрами.";
      case "allocations_differ_from_amount":
        return "Разбивка суммы по статьям не совпадает с общей суммой документа.";
      case "conflicting_periods":
        return "В документе указаны разные периоды.";
      default:
        // Notes from reconciliation are already Russian; an English sentence is never shown as is.
        if (/[A-Za-z]{2,}\s+[A-Za-z]{2,}/.test(text)) return "Часть данных требует дополнительной проверки.";
        return humanMessage(0, text) || "Часть данных требует дополнительной проверки.";
    }
  }

  function renderIssues(run) {
    const issues = (run.issues || []).filter(isObj);
    if (!issues.length) return null;
    return h(
      "ul",
      { class: "issues small" },
      issues.map((issue) => h("li", null, issueText(issue), " ", evidenceButtons(issue.evidence_ids, { key: run.run_id + ":issue" }))),
    );
  }

  function renderFailure(failure) {
    return h(
      "div",
      { class: "alert alert-bad", role: "alert" },
      h("strong", null, "Проверка не выполнена"),
      FAILURE[failure.code] ? " (" + FAILURE[failure.code] + ")" : "",
      ". ",
      FAILURE_ACTION[failure.code] || FAILURE_ACTION.internal_error,
    );
  }

  function categoryName(category) {
    return CATEGORY[category] || "Другая статья";
  }

  function fileName(versionId, fallback) {
    const v = versionById(versionId);
    return v ? v.original_filename : fallback;
  }

  function versionTime(versionId) {
    const v = versionById(versionId);
    return v ? formatTime(v.created_at) : "ранее";
  }

  function indicatorTitle(ind) {
    return ind.column !== null && ind.column !== undefined ? "Гр. " + ind.column + " «" + String(ind.label) + "»" : String(ind.label);
  }

  function renderResult(run) {
    const result = run.result;
    const indicators = result.indicators;
    const lookup = (entryId) => indicators.find((i) => i.entry_id === entryId) || null;
    const collapsed = state.collapsedReports.has(run.run_id);
    const section = h("section", { class: "result" + (collapsed ? " report-collapsed" : ""), "aria-label": "Результат проверки №" + runNumber(run.run_id) });
    const toggle = h(
      "button",
      {
        type: "button",
        class: "link report-toggle",
        "aria-expanded": collapsed ? "false" : "true",
        "data-fkey": "report:" + run.run_id,
        onclick: () => {
          const open = state.collapsedReports.has(run.run_id);
          if (open) state.collapsedReports.delete(run.run_id);
          else state.collapsedReports.add(run.run_id);
          section.classList.toggle("report-collapsed", !open);
          toggle.setAttribute("aria-expanded", open ? "true" : "false");
          toggle.textContent = open ? "Свернуть отчет" : "Показать отчет: " + indicators.length + " " + plural(indicators.length, "показатель", "показателя", "показателей");
        },
      },
      collapsed ? "Показать отчет: " + indicators.length + " " + plural(indicators.length, "показатель", "показателя", "показателей") : "Свернуть отчет",
    );
    section.append(
      renderVerdict(result, lookup),
      renderConclusion(result.conclusion, lookup, run.run_id),
      h("div", { class: "report-bar" }, toggle),
      renderIndicatorTable(indicators, run.run_id, "run:" + run.run_id),
      renderExclusions(result.exclusions, run.run_id, "Исключенные документы набора"),
      renderUnresolved(result.unresolved, run.run_id),
      h("p", { class: "mute small report-extra" }, "Допуск округления ", amount(result.tolerance, null), " руб. Все суммы взяты из сохраненного результата проверки."),
    );
    return section;
  }

  /** The verdict in one line, rearranged from the backend's grand total and statuses; nothing is recomputed. */
  function renderVerdict(result, lookup) {
    const g = isObj(result.conclusion.grand_total) ? result.conclusion.grand_total : {};
    const total = g.entry_id ? lookup(g.entry_id) : null;
    const unit = total && total.unit ? total.unit : "руб.";
    const components = Array.isArray(result.conclusion.component_discrepancies) ? result.conclusion.component_discrepancies : [];
    let headline;
    let tone;
    if (g.status === "discrepancy" && typeof g.diff === "string" && DECIMAL_RE.test(g.diff)) {
      const magnitude = formatDecimal(g.diff.replace(/^-/, ""), false);
      headline = "Отчет не подтвержден: итог " + (g.diff.charAt(0) === "-" ? "занижен" : "завышен") + " на " + magnitude + NBSP + unit;
      tone = "bad";
    } else if (g.status === "verified") {
      headline = "Итог отчета подтвержден документами";
      tone = "ok";
    } else if (g.status === "insufficient_evidence") {
      headline = components.length
        ? "Итог нельзя подтвердить, но найдено расхождение по " + components.length + " " + plural(components.length, "статье", "статьям", "статьям")
        : "Итог нельзя подтвердить: документов недостаточно";
      tone = components.length ? "bad" : "warn";
    } else {
      headline = "Проверка завершена";
      tone = "muted";
    }
    const count = (status) => result.indicators.filter((i) => i.status === status).length;
    const chips = [
      ["verified", count("verified"), ["подтвержден", "подтверждено", "подтверждено"]],
      ["discrepancy", count("discrepancy"), ["расхождение", "расхождения", "расхождений"]],
      ["insufficient_evidence", count("insufficient_evidence"), ["недостаточно данных", "недостаточно данных", "недостаточно данных"]],
    ];
    return h(
      "div",
      { class: "verdict tone-" + tone },
      h("p", { class: "verdict-line" }, headline),
      h(
        "p",
        { class: "verdict-counts" },
        chips.map(([status, n, words]) =>
          h("span", { class: "count-chip tone-" + IND_STATUS[status].tone }, mark(IND_STATUS[status].mark), n + " " + plural(n, words[0], words[1], words[2])),
        ),
      ),
    );
  }

  function renderConclusion(conclusion, lookup, runId) {
    const g = isObj(conclusion.grand_total) ? conclusion.grand_total : {};
    const totalInd = g.entry_id ? lookup(g.entry_id) : null;
    const totalUnit = totalInd ? totalInd.unit : null;
    const label = (entryId) => {
      const ind = lookup(entryId);
      return ind ? indicatorTitle(ind) : "показатель отчета";
    };
    const components = Array.isArray(conclusion.component_discrepancies) ? conclusion.component_discrepancies : [];
    const incomplete = Array.isArray(conclusion.incomplete_indicators) ? conclusion.incomplete_indicators : [];
    const explained = Array.isArray(g.explained_by) ? g.explained_by : [];
    return h(
      "section",
      { class: "conclusion" },
      h("h4", null, "Заключение"),
      h("p", { class: "summary-text" }, String(conclusion.summary || "")),
      h(
        "div",
        { class: "grand", "data-entry": g.entry_id || "" },
        h("h5", null, totalInd ? "Итог, " + indicatorTitle(totalInd) : "Итог отчета"),
        badge(IND_STATUS[g.status], g.status),
        h(
          "dl",
          { class: "kv" },
          h("dt", null, "Разница итога"),
          h("dd", { class: "g-diff" }, amount(g.diff, totalUnit, { signed: true })),
          explained.length ? h("dt", null, "Объясняется расхождениями по") : null,
          explained.length ? h("dd", { class: "g-explained" }, explained.map(label).join("; ")) : null,
          h("dt", null, "Необъясненная часть"),
          h("dd", { class: "g-unexplained" }, amount(g.unexplained_diff, totalUnit, { signed: true, unknown: "не определена" })),
        ),
        explained.length
          ? h("p", { class: "small note" }, "Расхождение итога — это те же расхождения по статьям, а не дополнительная сумма; они не складываются.")
          : null,
        g.status === "insufficient_evidence"
          ? h("p", { class: "small note" }, "Итог не подтвержден: полное ожидаемое значение и разница неизвестны.")
          : null,
      ),
      components.length
        ? h(
            "div",
            { class: "components" },
            h("h5", null, "Подтвержденные расхождения по статьям: " + components.length),
            h(
              "ul",
              null,
              components.map((d) => {
                const ind = lookup(d.entry_id);
                return h(
                  "li",
                  { "data-entry": d.entry_id },
                  h("strong", null, label(d.entry_id)),
                  ": ",
                  amount(d.diff, ind ? ind.unit : null, { signed: true }),
                  d.problem ? " (" + (PROBLEM[d.problem] || d.problem) + ")" : "",
                  h("div", { class: "small" }, String(d.explanation || "")),
                  evidenceButtons(d.evidence_ids, { key: runId + ":cd:" + d.entry_id, label: "Показать в отчете", context: label(d.entry_id) }),
                );
              }),
            ),
          )
        : null,
      incomplete.length
        ? h(
            "div",
            { class: "incomplete" },
            h("h5", null, "Недостаточно данных: " + incomplete.length + " " + plural(incomplete.length, "показатель", "показателя", "показателей")),
            h("p", { class: "small" }, incomplete.map(label).join("; ")),
          )
        : null,
    );
  }

  function renderIndicatorTable(indicators, runId, contextKey) {
    const rows = [];
    for (const ind of indicators) {
      const key = contextKey + ":" + ind.entry_id;
      const detailId = domId("ind", key);
      const expanded = state.expanded.has(key);
      const detailCell = h("td", { colspan: "6" });
      const detail = h("tr", { class: "ind-detail", id: detailId, hidden: !expanded }, detailCell);
      if (expanded) detailCell.append(renderIndicatorDetails(ind, runId, key));
      const toggle = h(
        "button",
        {
          type: "button",
          class: "link ind-toggle",
          "aria-expanded": expanded ? "true" : "false",
          "aria-controls": detailId,
          "data-fkey": "toggle:" + key,
        },
        expanded ? "Скрыть" : "Подробнее",
      );
      toggle.addEventListener("click", () => {
        const open = !state.expanded.has(key);
        if (open) state.expanded.add(key);
        else state.expanded.delete(key);
        if (open && !detailCell.firstChild) detailCell.append(renderIndicatorDetails(ind, runId, key));
        detail.hidden = !open;
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        toggle.textContent = open ? "Скрыть" : "Подробнее";
        markEvidenceButtons();
      });
      const info = IND_STATUS[ind.status];
      const diff = amount(ind.diff, null, { signed: true });
      // The report's own column number sits in the ledger margin with the tick mark. Amount cells hold numbers
      // only; the row's unit is printed once beside the label to keep the table narrow.
      rows.push(
        h(
          "tr",
          { class: "ind-row tone-" + (info ? info.tone : "muted"), "data-entry": ind.entry_id, "data-status": ind.status },
          h(
            "td",
            { class: "c-col", "data-label": "Графа" },
            h("span", { class: "col-num" }, ind.column !== null && ind.column !== undefined ? String(ind.column) : "–", mark(info ? info.mark : "query")),
          ),
          h(
            "th",
            { scope: "row", class: "c-label", "data-label": "Показатель" },
            h("span", { class: "ind-label" }, String(ind.label)),
            h(
              "span",
              { class: "ind-meta" },
              h("span", { class: "c-status" }, badge(info, ind.status)),
              h("span", { class: "ind-unit", title: "Единица измерения" }, ind.unit ? String(ind.unit) : "единица не указана"),
              toggle,
            ),
          ),
          h("td", { class: "c-reported", "data-label": "В отчете" }, amount(ind.reported_value, null, { unknown: "не прочитано" })),
          h("td", { class: "c-observed", "data-label": "По документам" }, observedCell(ind)),
          h("td", { class: "c-expected", "data-label": "Ожидаемо полностью" }, amount(ind.expected_value, null)),
          h("td", { class: "c-diff", "data-label": "Разница" }, ind.status === "discrepancy" && ind.diff ? circled(diff) : diff),
        ),
        detail,
      );
    }
    return h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "ind-table" },
        h("caption", null, "Показатели отчета: " + indicators.length),
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            h("th", { scope: "col" }, "Графа"),
            h("th", { scope: "col" }, "Показатель"),
            h("th", { scope: "col", class: "num-col" }, "В отчете"),
            h("th", { scope: "col", class: "num-col" }, "По документам"),
            h("th", { scope: "col", class: "num-col" }, "Ожидаемо полностью"),
            h("th", { scope: "col", class: "num-col" }, "Разница"),
          ),
        ),
        h("tbody", null, rows),
      ),
    );
  }

  function observedCell(ind) {
    const value = amount(ind.observed_sum, null, { unknown: "нет данных" });
    if (ind.observed_sum !== null && ind.observed_sum !== undefined && ind.coverage !== "complete") {
      return h("span", { class: "partial" }, value, " ", h("span", { class: "tag tag-partial" }, "частичная сумма"));
    }
    return value;
  }

  function renderIndicatorDetails(ind, runId, key) {
    const contributions = Array.isArray(ind.contributions) ? ind.contributions : [];
    const exclusions = Array.isArray(ind.exclusions) ? ind.exclusions : [];
    const gaps = Array.isArray(ind.gaps) ? ind.gaps : [];
    return h(
      "div",
      { class: "ind-details" },
      h("p", { class: "explanation" }, String(ind.explanation || "")),
      h(
        "dl",
        { class: "kv small" },
        h("dt", null, "Покрытие"),
        h("dd", null, COVERAGE[ind.coverage] || String(ind.coverage)),
        h("dt", null, "Проблема"),
        h("dd", null, ind.problem ? PROBLEM[ind.problem] || ind.problem : "нет"),
        h("dt", null, "Категория"),
        h("dd", null, ind.category ? categoryName(ind.category) : "не указана"),
        h("dt", null, "Единица"),
        h("dd", null, ind.unit ? String(ind.unit) : "не указана"),
      ),
      ind.coverage !== "complete" && ind.observed_sum !== null && ind.observed_sum !== undefined
        ? h("p", { class: "small note" }, "Сумма по документам частичная: это не полное ожидаемое значение.")
        : null,
      Array.isArray(ind.reported_evidence_ids) && ind.reported_evidence_ids.length
        ? h("p", { class: "ev-line" }, h("span", { class: "mute" }, "Значение в отчете: "), evidenceButtons(ind.reported_evidence_ids, { key, label: "Показать в отчете", context: String(ind.label) }))
        : h("p", { class: "mute small" }, "Нет ссылки на фрагмент отчета."),
      contributions.length ? renderContributions(ind, contributions, key) : h("p", { class: "mute small" }, "Вкладов из документов нет."),
      exclusions.length ? renderExclusions(exclusions, runId, "Не учтены для этого показателя", key) : null,
      gaps.length ? renderGaps(gaps, key) : null,
    );
  }

  function renderContributions(ind, contributions, key) {
    const terms = [];
    contributions.forEach((c, index) => {
      const text = formatDecimal(c.amount, false);
      const shown = text === null || text === undefined ? String(c.amount) : text;
      if (index === 0) terms.push(shown);
      else if (shown.charAt(0) === MINUS) terms.push(" " + MINUS + " " + shown.slice(1));
      else terms.push(" + " + shown);
    });
    return h(
      "div",
      { class: "contributions" },
      h(
        "table",
        { class: "sub-table" },
        h("caption", null, "Вклады в сумму по документам: " + contributions.length),
        h(
          "thead",
          null,
          h("tr", null, h("th", { scope: "col" }, "Источник"), h("th", { scope: "col" }, "Статья"), h("th", { scope: "col" }, "Сумма"), h("th", { scope: "col" }, "Доказательство")),
        ),
        h(
          "tbody",
          null,
          contributions.map((c) => {
            const v = versionById(c.version_id);
            return h(
              "tr",
              { "data-version-id": c.version_id },
              h("td", { "data-label": "Источник" }, String(c.label || ""), v ? h("div", { class: "vmeta" }, v.original_filename) : null),
              h("td", { "data-label": "Статья" }, categoryName(c.category)),
              h("td", { "data-label": "Сумма", class: "num-cell" }, amount(c.amount, ind.unit)),
              h("td", { "data-label": "Доказательство" }, evidenceButtons(c.evidence_ids, { key: key + ":c", expectVersion: c.version_id, label: "Показать", context: String(c.label || "") })),
            );
          }),
        ),
      ),
      h(
        "p",
        { class: "breakdown" },
        h("span", { class: "mute" }, "Слагаемые: "),
        h("span", { class: "num terms" }, terms.join("")),
        h("br"),
        h("span", { class: "mute" }, "Сумма по документам: "),
        amount(ind.observed_sum, ind.unit, { unknown: "не определена" }),
      ),
    );
  }

  function renderExclusions(exclusions, runId, title, key) {
    const list = Array.isArray(exclusions) ? exclusions.filter(isObj) : [];
    if (!list.length) return title.startsWith("Исключенные") ? h("p", { class: "mute small" }, "Исключенных документов нет.") : null;
    return h(
      "div",
      { class: "exclusions" },
      h("h5", null, title + ": " + list.length),
      h(
        "ul",
        null,
        list.map((e) =>
          h(
            "li",
            { "data-version-id": e.version_id },
            h("strong", null, String(e.label || "")),
            " ",
            h("span", { class: "tag" }, EXCLUSION[e.reason] || String(e.reason)),
            h("div", { class: "small" }, String(e.explanation || "")),
            e.amount !== null && e.amount !== undefined ? h("div", { class: "small" }, "Сумма документа: ", amount(e.amount, null)) : null,
            e.duplicate_of ? h("div", { class: "small" }, "Повторяет документ «" + fileName(e.duplicate_of, "другой документ набора") + "»") : null,
            evidenceButtons(e.evidence_ids, { key: (key || runId) + ":x", expectVersion: e.version_id }),
          ),
        ),
      ),
    );
  }

  function renderUnresolved(unresolved, runId) {
    const list = Array.isArray(unresolved) ? unresolved.filter(isObj) : [];
    if (!list.length) return h("p", { class: "mute small" }, "Нерешенных документов нет.");
    return h(
      "div",
      { class: "unresolved" },
      h("h5", null, "Нерешенные документы: " + list.length),
      h(
        "ul",
        null,
        list.map((u) =>
          h(
            "li",
            { "data-version-id": u.version_id },
            h("strong", null, String(u.label || "")),
            h("div", { class: "small" }, String(u.reason || "")),
            Array.isArray(u.possible_categories) && u.possible_categories.length
              ? h("div", { class: "small" }, "Возможные статьи: ", u.possible_categories.map(categoryName).join(", "))
              : null,
            evidenceButtons(u.evidence_ids, { key: runId + ":u", expectVersion: u.version_id }),
          ),
        ),
      ),
    );
  }

  function renderGaps(gaps, key) {
    return h(
      "div",
      { class: "gaps" },
      h("h5", null, "Пробелы в доказательствах: " + gaps.length),
      h(
        "ul",
        null,
        gaps.filter(isObj).map((g) =>
          h(
            "li",
            null,
            String(g.message || ""),
            Array.isArray(g.version_ids) && g.version_ids.length ? h("div", { class: "small" }, "Документы: ", g.version_ids.map((id) => fileName(id, "без названия")).join(", ")) : null,
            evidenceButtons(g.evidence_ids, { key: key + ":g" }),
          ),
        ),
      ),
    );
  }

  // ======================= rendering: messages =======================

  const ROLE_LABEL = { user: "Вы", assistant: "Ревизор", system: "Уведомление" };

  /** Old replies sometimes stored prose followed by the model's answer object. Read its text field only:
   * cards and financial values always remain the separately stored, typed result. Nothing is written back. */
  function legacyAnswerText(value) {
    const source = String(value)
      .replace(/<(think|analysis)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "")
      .replace(/<(?:think|analysis)\b[^>]*>[\s\S]*$/gi, "");
    const allowed = new Set(["kind", "text", "entry_ids", "evidence_ids", "include_conclusion"]);
    const isEnvelope = (data) => isObj(data) && typeof data.text === "string"
      && Object.keys(data).every((key) => allowed.has(key))
      && ["entry_ids", "evidence_ids"].every((key) => data[key] === undefined || (Array.isArray(data[key]) && data[key].every((id) => typeof id === "string")))
      && (data.kind === undefined || ["answer", "out_of_scope"].includes(data.kind))
      && (data.include_conclusion === undefined || typeof data.include_conclusion === "boolean");
    let result = "", cursor = 0, search = 0, finalText = null;
    while (search < source.length) {
      const start = source.indexOf("{", search);
      if (start < 0) break;
      let depth = 0, quoted = false, escaped = false, end = start;
      for (; end < source.length; end += 1) {
        const char = source[end];
        if (quoted) {
          if (escaped) escaped = false;
          else if (char === "\\") escaped = true;
          else if (char === '"') quoted = false;
        } else if (char === '"') quoted = true;
        else if (char === "{") depth += 1;
        else if (char === "}" && --depth === 0) { end += 1; break; }
      }
      const candidate = source.slice(start, end);
      let data = null;
      try { data = JSON.parse(candidate); } catch (_) { /* An old incomplete wrapper may still have a complete text string. */ }
      const complete = isEnvelope(data);
      let text = complete ? data.text : null;
      if (complete) finalText = text;
      const looksLikeEnvelope = /^\{\s*"(?:kind|text|entry_ids|evidence_ids|include_conclusion)"\s*:/.test(candidate)
        && /"text"\s*:/.test(candidate);
      if (text === null && looksLikeEnvelope && data === null) {
        const match = /"text"\s*:\s*("(?:[^"\\]|\\.)*")/.exec(candidate);
        if (match) {
          try { text = JSON.parse(match[1]); } catch (_) { /* Keep only readable surrounding prose. */ }
        }
      }
      if (text !== null || looksLikeEnvelope) {
        result += source.slice(cursor, start).replace(/```(?:json)?\s*$/i, "") + "\n" + (text || "") + "\n";
        cursor = end;
        const fence = /^\s*```/.exec(source.slice(cursor));
        if (fence) cursor += fence[0].length;
        search = cursor;
      } else search = start + 1;
    }
    // A final envelope supersedes any earlier model draft, which may repeat or contradict its answer.
    return finalText !== null ? finalText.trim() : (result + source.slice(cursor)).trim();
  }

  function readableAssistantText(message) {
    const run = message.run_id ? state.runs.get(message.run_id) : null;
    const indicators = [
      ...(run && isObj(run.result) && Array.isArray(run.result.indicators) ? run.result.indicators : []),
      ...(message.cards || []).filter((card) => isObj(card) && card.kind === "indicator" && isObj(card.indicator)).map((card) => card.indicator),
    ];
    const labels = new Map(indicators.filter((ind) => typeof ind.entry_id === "string").map((ind) => [ind.entry_id, String(ind.label || ind.title || "показатель")]));
    let text = legacyAnswerText(message.text);
    // Resolve references with this reply's run, so a historical result keeps its own indicator names.
    for (const [entryId, label] of labels) {
      const escaped = entryId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      text = text.replace(new RegExp("\\b" + escaped + "\\b", "g"), () => label);
    }
    text = text.replace(/\bcol(\d+)\b/g, "графа $1");
    const technical = { ...PROBLEM, ...EXCLUSION };
    for (const [key, info] of Object.entries(IND_STATUS)) technical[key] = info.label;
    for (const [key, label] of Object.entries(technical)) text = text.replace(new RegExp("\\b" + key + "\\b", "g"), () => label);
    return text || "Дополнительное пояснение недоступно. Сохраненный результат проверки доступен ниже.";
  }

  /** A small, safe Markdown subset for assistant prose; citations remain typed evidence buttons. */
  function inlineProse(text) {
    const fragment = document.createDocumentFragment();
    const source = String(text).replace(/\[([^\]]+)\]\([^\n)]*\)/g, "$1");
    const tokens = /\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`|\*([^*\n]+)\*/g;
    let start = 0;
    for (const match of source.matchAll(tokens)) {
      fragment.append(displayText(source.slice(start, match.index)));
      fragment.append(h(match[1] || match[2] ? "strong" : match[3] ? "code" : "em", null, match[1] || match[2] || match[3] || match[4]));
      start = match.index + match[0].length;
    }
    fragment.append(displayText(source.slice(start)));
    return fragment;
  }

  function renderProse(text) {
    const container = h("div", { class: "msg-text prose" });
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      if (/^```/.test(line)) {
        const codeLines = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) codeLines.push(lines[i++]);
        i += 1;
        container.append(h("pre", null, h("code", null, codeLines.join("\n"))));
        continue;
      }
      const heading = /^#{1,6}\s+(.+)$/.exec(line);
      if (heading) { container.append(h("h4", null, inlineProse(heading[1]))); i += 1; continue; }
      const listItem = /^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(line);
      if (listItem) {
        const list = h(listItem[1] ? "ul" : "ol");
        while (i < lines.length) {
          const item = /^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(lines[i]);
          if (!item || Boolean(item[1]) !== Boolean(listItem[1])) break;
          list.append(h("li", null, inlineProse(item[2])));
          i += 1;
        }
        container.append(list);
        continue;
      }
      const paragraph = [line];
      i += 1;
      while (i < lines.length && lines[i].trim() && !/^(?:#{1,6}\s|```|\s*(?:[-*+]|\d+[.)])\s)/.test(lines[i])) paragraph.push(lines[i++]);
      container.append(h("p", null, inlineProse(paragraph.join("\n"))));
    }
    return container;
  }

  function messageText(message) {
    if (message.role === "system") return humanMessage(0, message.text) || "Не удалось получить ответ. Повторите вопрос.";
    if (message.role !== "assistant") return message.text;
    if (message.text.startsWith("Комментарий ассистента недоступен (")) {
      return "Не удалось получить дополнительное пояснение. Сохраненный результат проверки доступен ниже.";
    }
    const failure = (message.cards || []).find((card) => isObj(card) && card.kind === "run_status" && card.status === "failed" && isObj(card.failure));
    if (failure) return "Проверка не выполнена: " + (FAILURE[failure.failure.code] || "не удалось обработать документы") + ".";
    return readableAssistantText(message);
  }

  function renderMessage(message, index) {
    const unanswered = message.role === "user" && !hasReply(message);
    const run = message.run_id ? state.runs.get(message.run_id) : null;
    return h(
      "article",
      { class: "msg msg-" + message.role, "data-message-id": message.message_id },
      message.role === "assistant" ? avatar() : null,
      h(
        "div",
        { class: "msg-body" },
        h(
          "header",
          { class: "msg-head" },
          h("strong", null, ROLE_LABEL[message.role] || "Сообщение"),
          h("span", { class: "vmeta" }, formatTime(message.created_at)),
          message.run_id && run ? h("span", { class: "vmeta" }, "о проверке №" + runNumber(message.run_id)) : null,
        ),
        message.role === "assistant" ? renderProse(messageText(message)) : h("div", { class: "msg-text" }, messageText(message)),
        Array.isArray(message.evidence_ids) && message.evidence_ids.length
          ? h("p", { class: "ev-line" }, h("span", { class: "mute" }, "Фрагменты из ответа: "), evidenceButtons(message.evidence_ids, { key: message.message_id }))
          : null,
        Array.isArray(message.cards) && message.cards.length ? renderMessageCards(message) : null,
        unanswered ? h("p", { class: "small note", role: "status" }, state.waitingReplies.has(message.message_id) ? "Ревизор готовит ответ…" : "Ответ на это сообщение не сохранен. Повторный вопрос будет новым сообщением.") : null,
      ),
    );
  }

  function renderMessageCards(message) {
    const seen = new Set();
    const cards = [];
    for (const card of message.cards) {
      if (!isObj(card)) continue;
      const id = card.kind + ":" + card.run_id + ":" + (isObj(card.indicator) ? card.indicator.entry_id : "");
      if (seen.has(id)) continue;
      seen.add(id);
      cards.push(card);
    }
    const statusCards = cards.filter((c) => c.kind === "run_status");
    const conclusions = cards.filter((c) => c.kind === "conclusion" && isObj(c.conclusion));
    const indicators = cards
      .filter((c) => c.kind === "indicator" && isObj(c.indicator) && typeof c.indicator.entry_id === "string")
      .map((c) => c.indicator);
    const unknown = cards.length - statusCards.length - conclusions.length - indicators.length;
    const run = state.runs.get(message.run_id);
    const parts = [];
    if (statusCards.length) parts.push("состояние");
    if (conclusions.length) parts.push("вывод");
    if (indicators.length) parts.push(indicators.length + " " + plural(indicators.length, "показатель", "показателя", "показателей"));
    const lookup = (entryId) => {
      const fromCards = indicators.find((i) => i.entry_id === entryId);
      if (fromCards) return fromCards;
      const saved = run && isObj(run.result) ? run.result.indicators.find((i) => i.entry_id === entryId) : null;
      return saved || null;
    };
    const key = "msg:" + message.message_id;
    return h(
      "details",
      { class: "msg-cards" },
      h("summary", null, "Результат проверки" + (run ? " №" + runNumber(message.run_id) : "") + ", на который опирается ответ: " + parts.join(", ")),
      statusCards.map((card) =>
        h(
          "p",
          { class: "small" },
          "Состояние на момент ответа: ",
          badge(RUN_STATUS[card.status], card.status),
          STAGE_LABEL[card.stage] ? ", этап «" + STAGE_LABEL[card.stage] + "»" : "",
          isObj(card.failure) ? ". " + (FAILURE_ACTION[card.failure.code] || FAILURE_ACTION.internal_error) : "",
        ),
      ),
      conclusions.map((card) => renderConclusion(card.conclusion, lookup, message.run_id)),
      indicators.length ? renderIndicatorTable(indicators, message.run_id, key) : null,
      unknown > 0 ? h("p", { class: "alert alert-warn small" }, "Карточки неизвестного типа не показаны: " + unknown) : null,
    );
  }

  function renderPending(item) {
    const run = item.runId ? state.runs.get(item.runId) : null;
    const status = {
      sending: h("p", { class: "mute small", role: "status" }, "Ревизор готовит ответ…"),
      checking: h("p", { class: "mute small" }, "Проверка, сохранено ли сообщение…"),
      failed: h(
        "div",
        { class: "alert alert-bad", role: "alert" },
        h("p", null, "Не отправлено. " + (item.error || "")),
        h("button", { type: "button", "data-fkey": "retry:" + item.localId, onclick: () => retryPending(item) }, "Повторить"),
        " ",
        h(
          "button",
          {
            type: "button",
            class: "ghost",
            "data-fkey": "discard:" + item.localId,
            onclick: () => {
              removePending(item);
              const textarea = $("#composer-text");
              if (!textarea.value) textarea.value = item.text;
              renderTimeline();
              textarea.focus();
            },
          },
          "Вернуть в поле ввода",
        ),
      ),
      unknown: h(
        "div",
        { class: "alert alert-warn", role: "alert" },
        h("p", null, "Неизвестно, сохранено ли сообщение: " + (item.error || "")),
        h("button", { type: "button", "data-fkey": "retry:" + item.localId, onclick: () => retryPending(item) }, "Проверить и повторить"),
      ),
    }[item.status];
    return h(
      "article",
      { class: "msg msg-user pending", "data-pending": item.status },
      h(
        "header",
        { class: "msg-head" },
        h("strong", null, "Вы"),
        item.runId && run ? h("span", { class: "vmeta" }, "о проверке №" + runNumber(item.runId)) : null,
      ),
      h("div", { class: "msg-text" }, item.text),
      status,
    );
  }

  // ======================= rendering: evidence panel =======================

  function renderEvidence() {
    const body = $("#evidence-body");
    const ev = state.evidence;
    body.setAttribute("data-state", ready() ? ev.status : "idle");
    body.setAttribute("data-block", ev.blockId || "");
    if (!ready() || ev.status === "idle") {
      setChildren(
        body,
        h(
          "p",
          { class: "mute" },
          "Нажмите на фрагмент в карточке показателя: здесь появится страница, ячейка или цитата из той версии документа, которая использовалась в проверке.",
        ),
      );
      return;
    }
    if (ev.status === "loading") {
      setChildren(body, h("p", { class: "mute" }, "Открываю фрагмент…"));
      return;
    }
    if (ev.status === "error") {
      const notFound = ev.error && ev.error.status === 404;
      const run = state.selectedRunId ? state.runs.get(state.selectedRunId) : null;
      setChildren(
        body,
        h(
          "div",
          { class: "alert alert-bad", role: "alert", "data-evidence-error": notFound ? "not_found" : "error" },
          h(
            "p",
            null,
            notFound
              ? "Этот фрагмент не найден среди документов сверки. Доказательство недоступно; другой файл не подставляется."
              : "Фрагмент не открылся. " + (ev.error ? ev.error.message : ""),
          ),
          notFound && run && isFixtureRun(run)
            ? h("p", { class: "small" }, "В синтетическом примере ссылки на фрагменты не связаны с загруженными файлами.")
            : null,
        ),
      );
      return;
    }
    const view = ev.view;
    const block = view.block;
    const version = view.version;
    const latest = latestVersionOf(version.document_id);
    const newer = latest && latest.version_id !== version.version_id ? latest : null;
    const siblings = state.documents.filter((v) => v.document_id === version.document_id);
    const position = siblings.findIndex((v) => v.version_id === version.version_id);
    const expected = ev.expectVersion ? versionById(ev.expectVersion) : null;
    const parts = [
      h(
        "section",
        { class: "ev-doc", "data-version-id": version.version_id, "data-newer-version-id": newer ? newer.version_id : null },
        h("h3", { class: "fname" }, version.original_filename),
        h(
          "p",
          { class: "ev-meta meta-row" },
          h("span", null, ROLE_ONE[version.role] || "Документ"),
          h("span", null, "загружен " + formatTime(version.created_at)),
          siblings.length > 1 && position >= 0 ? h("span", null, "версия " + (position + 1) + " из " + siblings.length) : null,
        ),
        newer
          ? h(
              "p",
              { class: "alert alert-warn small ev-newer" },
              "Есть более новая версия этого документа от " + formatTime(newer.created_at) + ". Здесь показана версия, на которую ссылается результат.",
            )
          : null,
        ev.expectVersion && ev.expectVersion !== version.version_id
          ? h(
              "p",
              { class: "alert alert-warn small" },
              "Результат ссылается на " + (expected ? "версию от " + formatTime(expected.created_at) : "другой документ") + ", а фрагмент взят из версии от " + formatTime(version.created_at) + ".",
            )
          : null,
      ),
      h("p", { class: "small ev-method" }, "Как найдено: " + (METHOD[block.method] || "способ не указан"), isObj(block.quality) ? qualityText(block.quality) : ""),
    ];
    const locator = block.locator;
    if (locator.kind === "page_region") parts.push(renderPageRegion(view, locator));
    else if (locator.kind === "cell") parts.push(renderCell(block, locator));
    else if (locator.kind === "docx") parts.push(renderDocx(block, locator));
    else parts.push(h("p", { class: "alert alert-warn" }, "Не удалось показать этот фрагмент. Откройте исходный документ по ссылке ниже."));
    if (block.derived_from) {
      parts.push(h("p", { class: "small" }, "Получен из фрагмента ", evidenceButtons([block.derived_from], { key: "derived" })));
    }
    const downloadable = !["pdf", "png", "jpeg", "txt"].includes(version.format);
    parts.push(
      h(
        "p",
        { class: "original" },
        h(
          "a",
          { class: "btn", href: view.original_url, target: "_blank", rel: "noopener", download: downloadable ? version.original_filename : null },
          downloadable ? "Скачать исходный файл" : "Открыть исходный файл",
        ),
        " ",
        h("span", { class: "mute small" }, "«" + version.original_filename + "»"),
      ),
    );
    setChildren(body, parts);
    markEvidenceButtons();
  }

  function qualityText(q) {
    return typeof q.confidence === "number" ? ", уверенность " + Math.round(q.confidence) + "%" : "";
  }

  function pct(value, total) {
    return (value / total) * 100 + "%";
  }

  function renderPageRegion(view, loc) {
    const box = loc.box;
    const W = loc.image_width;
    const H = loc.image_height;
    const t = isObj(loc.transform) ? loc.transform : {};
    const notes = [];
    if (t.rotation) notes.push("страница повернута на " + t.rotation + "°");
    if (isObj(t.crop)) notes.push("показана часть страницы");
    const figure = h("figure", { class: "page" });
    const caption = h(
      "figcaption",
      { class: "small" },
      "Страница " + loc.page + (notes.length ? " (" + notes.join(", ") + ")" : "") + ". Нужное место выделено маркером.",
    );
    const warning = h("p", { class: "alert alert-warn small", hidden: true });
    if (!view.artifact_url) {
      figure.append(h("p", { class: "alert alert-bad" }, "Изображение страницы не сохранено."), caption);
      return h("div", null, figure, renderText(regionTextTitle(view.block), view.block.text));
    }
    const img = h("img", {
      class: "page-img",
      src: view.artifact_url,
      alt: "Страница " + loc.page + " документа " + view.version.original_filename,
      "data-image-width": W,
      "data-image-height": H,
    });
    // The box is in the stored render's pixel space. Percentages of the displayed image scale it to any size;
    // the render already includes rotation/crop, so no transform is applied here.
    const mark = h("div", {
      class: "page-mark",
      "aria-hidden": "true",
      "data-box": [box.x0, box.y0, box.x1, box.y1].join(","),
    });
    mark.style.left = pct(box.x0, W);
    mark.style.top = pct(box.y0, H);
    mark.style.width = pct(box.x1 - box.x0, W);
    mark.style.height = pct(box.y1 - box.y0, H);
    const frame = h("div", { class: "page-frame" }, img, mark);
    const scroller = h("div", { class: "page-scroll" }, frame);
    let zoomed = false;
    const zoom = h(
      "button",
      {
        type: "button",
        class: "small",
        "aria-pressed": "false",
        onclick: () => {
          zoomed = !zoomed;
          frame.classList.toggle("zoomed", zoomed);
          frame.style.width = zoomed ? W + "px" : "";
          zoom.setAttribute("aria-pressed", zoomed ? "true" : "false");
          zoom.textContent = zoomed ? "Вписать в панель" : "Показать в натуральную величину";
          mark.scrollIntoView({ block: "center", inline: "center" });
        },
      },
      "Показать в натуральную величину",
    );
    img.addEventListener("load", () => {
      if (img.naturalWidth !== W || img.naturalHeight !== H) {
        warning.hidden = false;
        warning.textContent =
          "Изображение страницы сохранено в другом размере; выделение подогнано пропорционально и может быть неточным.";
      }
    });
    img.addEventListener("error", () => {
      warning.hidden = false;
      warning.textContent = "Изображение страницы не загрузилось.";
      mark.hidden = true;
    });
    figure.append(scroller, caption);
    const fullPage = "/static/evidence.html#" + new URLSearchParams({ workspace: state.workspaceId, block: view.block.block_id, version: view.version.version_id });
    return h("div", { class: "ev-region" }, h("div", { class: "row-actions" }, zoom, h("a", { href: fullPage, target: "_blank", rel: "noopener", class: "small" }, "Открыть страницу в новой вкладке")), figure, warning, renderText(regionTextTitle(view.block), view.block.text));
  }

  /** Only OCR and re-OCR text was recognized; native PDF text and vision readings are labelled as such. */
  function regionTextTitle(block) {
    return block.method === "ocr" || block.method === "reocr" ? "Распознанный текст" : "Текст фрагмента";
  }

  function renderText(title, text) {
    return h("div", { class: "ev-text" }, h("h4", null, title), h("pre", { class: "quote" }, String(text)));
  }

  function renderCell(block, loc) {
    const missing = (value, text) => (value === null || value === undefined ? h("span", { class: "val-null" }, text) : h("code", null, String(value)));
    return h(
      "div",
      { class: "ev-cell" },
      h(
        "dl",
        { class: "kv" },
        h("dt", null, "Лист"),
        h("dd", { class: "cell-sheet" }, String(loc.sheet)),
        h("dt", null, "Ячейка"),
        h("dd", { class: "cell-ref" }, code(String(loc.ref))),
        h("dt", null, "Текст фрагмента"),
        h("dd", { class: "cell-text" }, String(block.text)),
        h("dt", null, "Формула в файле"),
        h("dd", { class: "cell-formula" }, missing(loc.formula, "нет формулы")),
        h("dt", null, "Значение, сохраненное в файле"),
        h("dd", { class: "cell-cached" }, missing(loc.cached_value, "не сохранено в файле")),
        h("dt", null, "Значение, вычисленное приложением"),
        h("dd", { class: "cell-derived" }, loc.derived_value === null || loc.derived_value === undefined ? h("span", { class: "val-null" }, "не вычислялось") : amount(loc.derived_value, null)),
        h("dt", null, "Состояние вычисления"),
        h("dd", { class: "cell-evaluation", "data-evaluation": loc.evaluation }, EVALUATION[loc.evaluation] || String(loc.evaluation)),
        Array.isArray(loc.inputs) && loc.inputs.length ? h("dt", null, "Входные ячейки") : null,
        Array.isArray(loc.inputs) && loc.inputs.length ? h("dd", { class: "cell-inputs" }, loc.inputs.map((x) => [code(String(x)), " "])) : null,
      ),
      h("p", { class: "mute small" }, "Сохраненное значение и результат вычисления показаны отдельно. Если формулу не удалось проверить, значение остается неизвестным."),
    );
  }

  function renderDocx(block, loc) {
    let address;
    if (loc.paragraph !== null && loc.paragraph !== undefined) address = "Абзац " + (loc.paragraph + 1);
    else address = "Таблица " + (loc.table + 1) + ", строка " + (loc.row + 1) + ", ячейка " + (loc.cell + 1);
    return h(
      "div",
      { class: "ev-docx" },
      h("p", { class: "docx-address" }, address, h("span", { class: "mute small" }, " (нумерация с 1)")),
      h("blockquote", { class: "quote docx-quote" }, String(loc.quote)),
      block.text && block.text !== loc.quote ? renderText("Текст фрагмента", block.text) : null,
    );
  }

  // ======================= wiring =======================

  async function loadHealth() {
    try {
      const health = await request("GET", "/health");
      state.health = isObj(health) ? health : null;
      state.healthError = null;
    } catch (e) {
      state.healthError = e.message;
    }
    showMode();
  }

  function wire() {
    $("#ws-new").addEventListener("click", createWorkspace);
    for (const form of document.querySelectorAll("form.upload")) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        upload(form.dataset.role, form);
      });
      wireDropZone(form);
    }
    const textarea = $("#composer-text");
    textarea.addEventListener("input", () => {
      textarea.style.height = "";
      textarea.style.height = Math.min(textarea.scrollHeight, 200) + "px";
    });
    $("#doc-list").addEventListener("change", (event) => {
      if (event.target.matches("input[data-version-id]")) onSelectionChange(event);
    });
    const scopeForm = $("#scope-form");
    scopeForm.addEventListener("input", (event) => {
      if (event.target.id === "scope-confirm") return;
      resetConfirmation();
      savePrefs();
      renderScope();
      syncHistory(false);
      showWorkspace();
    });
    $("#scope-confirm").addEventListener("change", (event) => {
      state.confirmed = event.target.checked;
      renderScope();
    });
    scopeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      startRun();
    });
    $("#followup-run").addEventListener("change", (event) => selectRun(event.target.value || null, false));
    $("#composer").addEventListener("submit", (event) => {
      event.preventDefault();
      submitComposer();
    });
    $("#composer-text").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        submitComposer();
      }
    });
    $("#evidence-close").addEventListener("click", closeEvidence);
    $("#evidence-toggle").addEventListener("click", () =>
      document.body.classList.contains("evidence-open") ? closeEvidence() : reopenEvidence(),
    );
    $("#setup-toggle").addEventListener("click", () => {
      state.setupOpen = !state.setupOpen;
      renderPanels();
    });
    $("#side-hide").addEventListener("click", () => setSidebar(false));
    $("#side-show").addEventListener("click", () =>
      setSidebar(narrow() ? !document.body.classList.contains("side-drawer") : Boolean(layout.sideHidden)),
    );
    $("#ws-copy").addEventListener("click", copyLink);
    wireSplitter($("#side-splitter"), "side", 1);
    wireSplitter($("#ev-splitter"), "ev", -1);
    window.addEventListener("resize", applyLayout);
    window.addEventListener("hashchange", () => {
      const id = window.location.hash.slice(1);
      if (ID_RE.ws.test(id) && id !== state.workspaceId) openWorkspace(id);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (document.body.classList.contains("side-drawer")) setSidebar(false);
      else if (document.body.classList.contains("evidence-open")) closeEvidence();
    });
  }

  function boot() {
    applyLayout();
    wire();
    renderAll();
    loadHealth();
    // A link with #ws_… opens that reconciliation; otherwise the last one opened in this browser.
    const fromLink = window.location.hash.slice(1);
    const stored = ID_RE.ws.test(fromLink) ? fromLink : storage.get(KEY_CURRENT);
    if (stored) openWorkspace(stored);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
