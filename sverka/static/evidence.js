/* Read-only, version-pinned evidence viewer. All requests stay in the selected workspace. */
"use strict";

(function () {
  const ID = (prefix) => new RegExp("^" + prefix + "_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$");
  const body = document.querySelector("#evidence-body");
  const params = new URLSearchParams(location.hash.slice(1));
  const workspace = params.get("workspace");
  const blockId = params.get("block");
  const versionId = params.get("version");
  let sourceHash = "";

  // Known source hashes and internal references are metadata; decimal text is preserved verbatim.
  function copy(value) {
    let text = String(value);
    return text
      .replace(/\b(?:ws|doc|ver|blk|art|run|msg)_[A-Za-z0-9][A-Za-z0-9_-]{0,63}\b/g, "документ")
      .replace(/(?:\bSHA(?:-?1|-?256)?|\bcommit|коммит|хеш|хэш)\s*[:=]?\s*[a-f0-9]{7,64}\b/gi, "")
      .replace(/\b[a-f0-9]{7,64}\b/gi, (token) => {
        const known = sourceHash && sourceHash.toLowerCase().startsWith(token.toLowerCase());
        return known || (/[a-f]/i.test(token) && [40, 64].includes(token.length)) ? "…" : token;
      })
      .replace(/\b(?:workspace_id|run_id|version_id|document_id|block_id|artifact_id|message_id|sha256)\b/g, "ссылка")
      .replace(/\bSVERKA_[A-Z0-9_]+\b/g, "настройка приложения")
      .replace(/\b[A-Za-z_][\w.]*(?:Error|Exception)\b/g, "ошибка")
      .replace(/\b(?:missing_credentials|provider_error|timeout|budget_exhausted|internal_error|invalid_input)\b/g, "ошибка обработки")
      .replace(/\/api\/[^\s<>"']+/g, "ссылка на документ");
  }

  function node(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = value;
      else el.setAttribute(key, ["alt", "title", "aria-label"].includes(key) ? copy(value) : String(value));
    }
    for (const child of children.flat()) {
      if (child !== undefined && child !== null) el.append(child instanceof Node ? child : copy(child));
    }
    return el;
  }

  function valid(condition) {
    if (!condition) throw new Error("invalid evidence");
  }

  function artifactPath(path) {
    if (typeof path !== "string" || !path.startsWith("/api/workspaces/" + workspace + "/artifacts/")) return false;
    const id = path.slice(("/api/workspaces/" + workspace + "/artifacts/").length);
    return ID("art").test(id);
  }

  function validate(view) {
    valid(view && view.block && view.version && view.block.locator);
    const { block, version } = view;
    valid(block.block_id === blockId && block.version_id === versionId && version.version_id === versionId);
    valid(version.workspace_id === workspace);
    valid(ID("doc").test(version.document_id) && typeof version.original_filename === "string");
    valid(artifactPath(view.original_url));
    const artifactBase = "/api/workspaces/" + workspace + "/artifacts/";
    valid(ID("art").test(version.original_artifact_id) && view.original_url === artifactBase + version.original_artifact_id);
    valid(view.artifact_url === null || view.artifact_url === undefined || artifactPath(view.artifact_url));
    valid(typeof block.text === "string");
    const loc = block.locator;
    valid(loc.kind === "page_region" && Number.isInteger(loc.page) && loc.page > 0);
    valid(ID("art").test(loc.render_id));
    valid(!view.artifact_url || view.artifact_url === artifactBase + loc.render_id);
    valid(Number.isFinite(loc.image_width) && loc.image_width > 0 && Number.isFinite(loc.image_height) && loc.image_height > 0);
    const box = loc.box;
    valid(box && [box.x0, box.y0, box.x1, box.y1].every(Number.isFinite));
    valid(box.x0 >= 0 && box.y0 >= 0 && box.x1 > box.x0 && box.y1 > box.y0);
    valid(box.x1 <= loc.image_width && box.y1 <= loc.image_height);
    return view;
  }

  function failed(notFound) {
    body.dataset.state = "error";
    body.replaceChildren(node("div", { class: "alert alert-bad", role: "alert", "data-evidence-error": notFound ? "not_found" : "error" },
      node("h1", null, "Фрагмент недоступен"),
      node("p", null, notFound
        ? "Этот фрагмент не найден среди документов сверки. Доказательство недоступно; другой файл не подставляется."
        : "Не удалось открыть сохраненный фрагмент. Вернитесь к результату проверки и откройте ссылку еще раз.")));
  }

  function render(view) {
    const { block, version } = view;
    const loc = block.locator;
    const W = loc.image_width, H = loc.image_height, box = loc.box;
    sourceHash = typeof version.sha256 === "string" ? version.sha256 : "";
    document.title = copy(version.original_filename) + " — Фрагмент документа";
    const date = new Date(version.created_at);
    const metadata = "Сохраненная версия документа" + (Number.isNaN(date.valueOf()) ? "" : " от " + date.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }));
    const heading = node("section", { class: "ev-doc", "data-version-id": version.version_id },
      node("h1", { class: "fname" }, version.original_filename), node("p", { class: "ev-meta" }, metadata));
    const warning = node("p", { class: "alert alert-warn small", hidden: true });
    const notes = [];
    const transform = loc.transform || {};
    if ([90, 180, 270].includes(transform.rotation)) notes.push("страница повернута на " + transform.rotation + "°");
    if (transform.crop && typeof transform.crop === "object") notes.push("показана часть страницы");
    const pageLabel = "Страница " + loc.page + (notes.length ? " (" + notes.join(", ") + ")" : "");
    const caption = node("figcaption", { class: "small" }, pageLabel + ". Нужное место выделено маркером.");
    const figure = node("figure", { class: "page" });
    const zoom = node("select", { id: "page-zoom" },
      node("option", { value: "fit" }, "По ширине окна"),
      ...[100, 150, 200].map((percent) => node("option", { value: String(percent) }, percent + "%")));
    const toolbar = node("div", { class: "viewer-toolbar" }, node("strong", null, "Страница " + loc.page),
      node("label", { class: "viewer-zoom", for: "page-zoom" }, "Масштаб", zoom));
    if (view.artifact_url) {
      const image = node("img", { class: "page-img", alt: "Страница " + loc.page + " документа " + version.original_filename });
      const mark = node("div", { class: "page-mark", "aria-hidden": "true", "data-box": [box.x0, box.y0, box.x1, box.y1].join(",") });
      // Coordinates already refer to the stored, rotated/cropped render. Never apply its transform again.
      mark.style.left = box.x0 / W * 100 + "%";
      mark.style.top = box.y0 / H * 100 + "%";
      mark.style.width = (box.x1 - box.x0) / W * 100 + "%";
      mark.style.height = (box.y1 - box.y0) / H * 100 + "%";
      const frame = node("div", { class: "page-frame" }, image, mark);
      frame.style.maxWidth = W + "px";
      const scroller = node("div", { class: "page-scroll" }, frame);
      const centerMark = () => {
        const region = mark.getBoundingClientRect();
        const viewport = scroller.getBoundingClientRect();
        scroller.scrollTo({
          top: scroller.scrollTop + region.top - viewport.top + region.height / 2 - scroller.clientHeight / 2,
          left: scroller.scrollLeft + region.left - viewport.left + region.width / 2 - scroller.clientWidth / 2,
        });
      };
      zoom.addEventListener("change", () => {
        const fit = zoom.value === "fit";
        frame.style.width = fit ? "100%" : W * Number(zoom.value) / 100 + "px";
        frame.style.maxWidth = fit ? W + "px" : "none";
        centerMark();
      });
      image.addEventListener("load", () => {
        if (image.naturalWidth !== W || image.naturalHeight !== H) {
          mark.hidden = true;
          caption.textContent = copy(pageLabel + ". Выделение недоступно; см. текст фрагмента ниже.");
          warning.hidden = false;
          warning.textContent = "Размер изображения отличается от сохраненного. Выделение скрыто, чтобы не указывать неверное место; текст фрагмента доступен ниже.";
        } else requestAnimationFrame(centerMark);
      });
      image.addEventListener("error", () => {
        mark.hidden = true;
        image.hidden = true;
        zoom.disabled = true;
        caption.textContent = copy(pageLabel + ". Изображение недоступно.");
        warning.hidden = false;
        warning.textContent = "Изображение страницы не загрузилось. Текст фрагмента и исходный файл доступны ниже.";
      });
      image.src = view.artifact_url;
      figure.append(scroller, caption);
    } else {
      zoom.disabled = true;
      caption.textContent = copy(pageLabel + ". Изображение недоступно.");
      figure.append(node("p", { class: "alert alert-warn" }, "Изображение страницы не сохранено. Текст фрагмента и исходный файл доступны ниже."), caption);
    }
    const recognized = ["ocr", "reocr"].includes(block.method);
    const excerpt = node("section", { class: "ev-text" }, node("h2", null, recognized ? "Распознанный текст" : "Текст фрагмента"), node("pre", { class: "quote" }, block.text));
    const original = node("p", { class: "original" }, node("a", { class: "btn", href: view.original_url, target: "_blank", rel: "noopener" }, "Открыть исходный файл"));
    body.replaceChildren(heading, toolbar, figure, warning, excerpt, original);
    body.dataset.state = "ready";
  }

  async function open() {
    if (!ID("ws").test(workspace) || !ID("blk").test(blockId) || !ID("ver").test(versionId)) { failed(false); return; }
    try {
      const response = await fetch("/api/workspaces/" + workspace + "/evidence/" + blockId, { credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" } });
      if (!response.ok) { failed(response.status === 404); return; }
      render(validate(await response.json()));
    } catch (_) {
      failed(false);
    }
  }

  open();
})();
