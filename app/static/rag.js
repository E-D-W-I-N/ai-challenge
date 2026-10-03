"use strict";

// Inspector reads actual CLI/index state; it never launches an indexing job.
function createRagInspector({ state, $, el, api }) {
  let epoch = 0, controller = null, timer = null, indexId = null;
  let documentOffset = 0, chunkOffset = 0, selectedDocument = null, selectedChunk = null;
  const PAGE = 25;
  const visible = () => state.workspace === "settings" && state.section === "rag" && document.visibilityState !== "hidden";
  const labels = { missing: "Индекс отсутствует", stale: "Корпус изменён — перестройте индекс", running: "Операция выполняется",
    ready: "Индекс готов", complete: "Операция завершена", interrupted: "Операция прервана", error: "Ошибка операции" };
  function stop() {
    epoch++;
    clearTimeout(timer); timer = null;
    controller?.abort(); controller = null;
  }
  function button(text, action) {
    const node = el("button", "btn", text); node.type = "button"; node.onclick = action; return node;
  }
  function detail(title, data, open = false) {
    const node = el("details", "rag-detail"); node.open = open;
    node.append(el("summary", "", title), el("pre", "", JSON.stringify(data, null, 2)));
    return node;
  }
  function pager(target, offset, count, action) {
    const row = el("div", "rag-pager");
    const previous = button("Назад", () => action(Math.max(0, offset - PAGE))); previous.disabled = offset === 0;
    const next = button("Далее", () => action(offset + PAGE)); next.disabled = count < PAGE;
    row.append(previous, el("span", "", `${offset + 1}–${offset + count}`), next); target.append(row);
  }
  async function documents(offset = documentOffset) {
    const token = epoch;
    const data = await api(`/api/rag/documents?offset=${offset}&limit=${PAGE}`);
    if (token !== epoch || !visible()) return;
    documentOffset = offset;
    const target = $("#rag-documents"); target.replaceChildren(el("h3", "", "Документы сохранённого индекса"));
    data.items.forEach((item) => {
      const row = el("div", "rag-item");
      row.append(button(item.title, () => selectDocument(item).catch(showError)), el("span", "muted", `${item.words} слов · ${item.characters} символов`));
      target.append(row);
    });
    pager(target, offset, data.items.length, (next) => documents(next).catch(showError));
  }
  async function selectDocument(item, offset = 0) {
    selectedDocument = item;
    const token = epoch;
    const data = await api(`/api/rag/documents/${encodeURIComponent(item.document_id)}/chunks?offset=${offset}&limit=${PAGE}`);
    if (token !== epoch || !visible() || selectedDocument.document_id !== item.document_id) return;
    chunkOffset = offset;
    const target = $("#rag-chunks"); target.replaceChildren(el("h3", "", item.title), detail("Метаданные документа", item));
    data.items.forEach((chunk) => target.append(button(`${chunk.section || "Без раздела"} · [${chunk.start}, ${chunk.end})`, () => selectChunk(chunk).catch(showError))));
    pager(target, offset, data.items.length, (next) => selectDocument(item, next).catch(showError));
  }
  async function selectChunk(item) {
    selectedChunk = item.chunk_id;
    const token = epoch;
    const data = await api(`/api/rag/chunks/${encodeURIComponent(item.chunk_id)}`);
    if (token !== epoch || !visible() || selectedChunk !== item.chunk_id) return;
    const target = $("#rag-chunk"); const { text, ...metadata } = data;
    target.replaceChildren(el("h3", "", "Выбранный чанк"), detail("Границы и метаданные", metadata, true), el("pre", "rag-text", text));
    const vector = el("details", "rag-detail"); vector.append(el("summary", "", "Показать сохранённый числовой вектор"));
    let loaded = false;
    vector.ontoggle = async () => {
      if (!vector.open || loaded) return;
      try {
        const saved = await api(`/api/rag/chunks/${encodeURIComponent(item.chunk_id)}?vector=true`);
        if (token !== epoch || selectedChunk !== item.chunk_id || !visible()) return;
        vector.append(el("pre", "", JSON.stringify(saved.vector))); loaded = true;
      } catch (error) { showError(error); }
    };
    target.append(vector);
  }
  function showError(error) { if (visible()) $("#rag-error").textContent = error.message; }
  async function refresh() {
    if (!visible() || controller) return;
    const token = epoch; controller = new AbortController();
    try {
      const data = await api("/api/rag/status", { signal: controller.signal });
      if (token !== epoch || !visible()) return;
      $("#rag-error").textContent = data.error || data.operation?.error || "";
      $("#rag-status").textContent = labels[data.state] || data.state;
      const target = $("#rag-operation"); target.replaceChildren();
      const op = data.operation;
      if (op) {
        target.append(el("p", "", `Операция: ${op.kind} · ${labels[op.state] || op.state} · ${op.duration_seconds} с`));
        const stages = el("ol", "rag-stages");
        ["documents", "chunks", "embeddings", "save"].forEach((stage) => stages.append(el("li", stage === op.stage ? "active" : "", stage)));
        target.append(stages, el("p", "", `Документов: ${op.documents} · чанков: ${op.chunks} · из кэша: ${op.cached} · вычислено: ${op.computed}`));
        if (op.config) target.append(el("p", "", `Модель: ${op.config.model} · размерность: ${op.dimension ?? "ещё неизвестна"}`));
        target.append(detail("Фактическое состояние операции", op));
      }
      const saved = $("#rag-index"); saved.replaceChildren();
      if (data.index) {
        const info = data.index;
        saved.append(el("h3", "", `Сохранённый SQLite · ${labels[info.state || "ready"] || info.state}`), el("p", "", `${info.words} очищенных слов · ≈${(info.words / 500).toFixed(1)} страниц по 500 слов · ${info.size_bytes} байт`),
          el("p", "", `Документов: ${info.rows.documents} · чанков/векторов: ${info.rows.chunks} · версия: ${info.version} · ${info.strategy}`),
          el("p", "", `Модель: ${info.embedding_config.model} · размерность: ${info.dimension}`), detail("Метаданные сохранённого индекса", info));
        if (indexId !== info.index_id || !$("#rag-documents").children.length) {
          indexId = info.index_id; selectedDocument = null; selectedChunk = null;
          $("#rag-chunks").replaceChildren(); $("#rag-chunk").replaceChildren();
          await documents(0);
        }
      } else {
        indexId = null; $("#rag-documents").replaceChildren(); $("#rag-chunks").replaceChildren(); $("#rag-chunk").replaceChildren();
      }
      if (data.ingestion) saved.append(detail("Отчёт загрузки HTML", data.ingestion));
    } catch (error) { if (error.name !== "AbortError" && token === epoch) showError(error); }
    finally {
      if (token === epoch) { controller = null; if (visible()) timer = setTimeout(refresh, 1000); }
    }
  }
  function open() { stop(); refresh(); }
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", stop);
    document.addEventListener("visibilitychange", () => { stop(); if (visible()) refresh(); });
  }
  return { open, stop };
}
if (typeof module !== "undefined") module.exports = createRagInspector;
else globalThis.createRagInspector = createRagInspector;
