"use strict";

// Durable stages and published index share the operator-configured directory.
function createRagInspector({ state, $, el, api }) {
  let epoch = 0, controller = null, timer = null, indexId = null;
  let previewEpoch = 0, documentRequest = 0, chunkRequest = 0, textRequest = 0;
  let documentOffset = 0, chunkOffset = 0, selectedDocument = null, selectedChunk = null;
  const PAGE = 25;
  let selectedStep = null;
  const steps = [["documents", "Документы"], ["chunks", "Чанки"], ["embeddings", "Эмбеддинги"], ["save", "Индекс"]];
  const navigation = new Map();
  for (const [id, title] of steps) {
    const node = button(title, () => { selectedStep = id; updateControls(); });
    node.id = "rag-step-" + id; navigation.set(id, node); $("#rag-navigation").append(node);
  }
  let vectorView = null, vectorVersion = 0, vectorFingerprint = null;
  let working = false, hasChunks = true, hasVectors = true, submitting = false, seeded = false, lastStatus = null;
  const workingQuery = () => working ? "&working=true" : "";
  const visible = () => state.workspace === "settings" && state.section === "rag" && document.visibilityState !== "hidden";
  const labels = { missing: "Индекс отсутствует", stale: "Корпус изменён — перестройте индекс", running: "Операция выполняется",
    ready: "Индекс готов", complete: "Операция завершена", interrupted: "Операция прервана", error: "Ошибка операции" };
  function stop() {
    epoch++;
    clearTimeout(timer); timer = null;
    controller?.abort(); controller = null;
  }
  function button(text, action) {
    const node = el("button", "mcp-button", text); node.type = "button"; node.onclick = action; return node;
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
    const token = epoch, preview = previewEpoch, request = ++documentRequest;
    const data = await api(`/api/rag/documents?offset=${offset}&limit=${PAGE}${workingQuery()}`);
    if (token !== epoch || preview !== previewEpoch || request !== documentRequest || !visible()) return;
    documentOffset = offset;
    const target = $("#rag-documents"); target.replaceChildren(el("h3", "", working ? "Загруженные документы" : "Документы сохранённого индекса"));
    data.items.forEach((item) => {
      const row = el("div", "rag-item");
      row.append(button(item.title, () => selectDocument(item).catch(showError)), el("span", "muted", `${item.words} слов · ${item.characters} символов`));
      target.append(row);
    });
    pager(target, offset, data.items.length, (next) => documents(next).catch(showError));
  }
  async function selectDocument(item, offset = 0) {
    selectedDocument = item;
    const token = epoch, preview = previewEpoch, request = ++chunkRequest;
    const target = $("#rag-chunks");
    if (working) {
      const text = await api(`/api/rag/documents/${encodeURIComponent(item.document_id)}?offset=0&limit=10000`);
      if (token !== epoch || preview !== previewEpoch || request !== chunkRequest || !visible()) return;
      const cleaned = detail(`Очищенный документ · первые ${Math.min(text.characters, 10000)} из ${text.characters} символов`, {});
      cleaned.querySelector("pre").textContent = text.text;
      $("#rag-document-preview").replaceChildren(el("h3", "", item.title), detail("Метаданные документа", item), cleaned);
      target.replaceChildren();
    }
    if (!hasChunks) return;
    const data = await api(`/api/rag/documents/${encodeURIComponent(item.document_id)}/chunks?offset=${offset}&limit=${PAGE}${workingQuery()}`);
    if (token !== epoch || preview !== previewEpoch || request !== chunkRequest || !visible() || selectedDocument.document_id !== item.document_id) return;
    chunkOffset = offset;
    if (!working) target.replaceChildren(el("h3", "", item.title), detail("Метаданные документа", item));
    data.items.forEach((chunk) => target.append(button(`${chunk.section || "Без раздела"} · [${chunk.start}, ${chunk.end})`, () => selectChunk(chunk).catch(showError))));
    pager(target, offset, data.items.length, (next) => selectDocument(item, next).catch(showError));
  }
  async function selectChunk(item) {
    selectedChunk = item.chunk_id;
    const token = epoch, preview = previewEpoch, request = ++textRequest;
    const data = await api(`/api/rag/chunks/${encodeURIComponent(item.chunk_id)}${working ? "?working=true" : ""}`);
    if (token !== epoch || preview !== previewEpoch || request !== textRequest || !visible() || selectedChunk !== item.chunk_id) return;
    const target = $("#rag-chunk"); const { text, ...metadata } = data;
    target.replaceChildren(el("h3", "", "Выбранный чанк"), detail("Границы и метаданные", metadata, true), el("pre", "rag-text", text));
    const vector = el("details", "rag-detail"); vector.append(el("summary", "", "Показать сохранённый числовой вектор"));
    let loaded = false;
    vector.ontoggle = async () => {
      if (!vector.open || loaded || !hasVectors) return;
      const version = vectorVersion, vectorToken = epoch;
      try {
        const saved = await api(`/api/rag/chunks/${encodeURIComponent(item.chunk_id)}?vector=true${workingQuery()}`);
        if (vectorToken !== epoch || preview !== previewEpoch || request !== textRequest || version !== vectorVersion || !hasVectors || selectedChunk !== item.chunk_id || !visible()) return;
        vector.append(el("pre", "", JSON.stringify(saved.vector))); loaded = true;
      } catch (error) { showError(error); }
    };
    vector.hidden = !hasVectors || selectedStep !== "embeddings"; target.append(vector);
    vectorView = { node: vector, invalidate: () => {
      loaded = false;
      for (const node of [...vector.querySelectorAll("pre")]) node.remove();
      vector.hidden = !hasVectors || selectedStep !== "embeddings";
      if (hasVectors && vector.open) vector.ontoggle();
    }};
  }
  function showError(error) { if (visible()) $("#rag-error").textContent = error.message; }
  async function refresh() {
    if (!visible() || controller) return;
    const token = epoch; controller = new AbortController();
    try {
      const data = await api("/api/rag/status", { signal: controller.signal });
      if (token !== epoch || !visible()) return;
      $("#rag-error").textContent = data.error || data.operation?.error || data.operation?.warning || "";
      $("#rag-status").textContent = labels[data.state] || data.state;
      renderOperation(data.operation);
      renderIndex(data.index, data.ingestion);
      lastStatus = data;
      const stages = data.stages;
      if (!seeded && stages?.chunks?.semantic_config) {
        for (const [id, key] of [["semantic-base-url", "base_url"], ["semantic-model", "model"], ["semantic-auth-mode", "auth_mode"]]) {
          const input = $("#rag-" + id); if (input && !input.dataset.dirty) input.value = stages.chunks.semantic_config[key] ?? "openrouter";
        }
      }
      if (!seeded) {
        if (stages?.chunks) {
          const saved = stages.chunks;
          for (const [id, value] of [["size", saved.size], ["overlap", saved.overlap], ["strategy", saved.strategy === "semantic" ? "semantic" : "fixed"]]) {
            const input = $("#rag-" + id); if (input && !input.dataset.dirty) input.value = value;
          }
          $("#rag-strategy")?.onchange?.();
        }
        const defaults = data.embedding_defaults;
        if (defaults) {
          for (const [id, key] of [["base-url", "base_url"], ["model", "model"], ["dimensions", "dimensions"], ["revision", "revision"]]) {
            const input = $("#rag-" + id); if (input && !input.dataset.dirty) input.value = defaults[key] ?? "";
          }
        }
        const urls = $("#rag-urls"); if (urls && !urls.dataset.dirty && stages?.corpus) urls.value = stages.corpus.urls.join("\n");
        previousAuthMode = $("#rag-semantic-auth-mode").value;
        seeded = true;
      }
      if ($("#rag-manifest-label")) $("#rag-manifest-label").hidden = !data.manifest_available;
      updateControls();
      const generation = stages?.corpus ? `${stages.corpus.fingerprint}:${stages.chunks?.fingerprint || ""}` : data.index?.index_id;
      const nextVectorFingerprint = stages?.corpus ? stages.embeddings?.embedding_fingerprint : data.index?.embedding_fingerprint;
      working = !!stages?.corpus; hasChunks = !working || !!stages?.chunks; hasVectors = !working || !!stages?.embeddings;
      if (generation && (indexId !== generation || !$("#rag-documents").children.length)) {
        vectorView = null; previewEpoch++; indexId = generation; selectedDocument = null; selectedChunk = null;
        $("#rag-document-preview").replaceChildren(); $("#rag-chunks").replaceChildren(); $("#rag-chunk").replaceChildren();
        await documents(0);
      } else if (!generation) {
        vectorView = null; previewEpoch++; indexId = null; $("#rag-documents").replaceChildren(); $("#rag-document-preview").replaceChildren(); $("#rag-chunks").replaceChildren(); $("#rag-chunk").replaceChildren();
      }
      if (vectorFingerprint !== nextVectorFingerprint) {
        vectorFingerprint = nextVectorFingerprint; vectorVersion++;
        vectorView?.invalidate();
      }
    } catch (error) { if (error.name !== "AbortError" && token === epoch) showError(error); }
    finally {
      if (token === epoch) { controller = null; if (visible()) timer = setTimeout(refresh, 1000); }
    }
  }
  // Patch mounted nodes; polling never detaches focused controls or details.
  let opNodes = null, savedNodes = null;
  function renderOperation(op) {
    const target = $("#rag-operation");
    if (!opNodes) {
      const summary = el("p"), counts = el("p"), model = el("p");
      const list = el("ol", "rag-stages"), stages = [["documents", "Документы"], ["chunks", "Чанки"], ["embeddings", "Эмбеддинги"], ["save", "Сохранение"]].map(([stage, title]) => {
        const node = el("li", "", title); node.dataset.stage = stage; return node;
      });
      list.append(...stages);
      const actual = detail("Фактическое состояние операции", {}); target.append(summary, list, counts, model, actual);
      opNodes = {summary, counts, model, stages, actual};
    }
    target.hidden = !op; if (!op) return;
    opNodes.summary.textContent = `Операция: ${op.kind} · ${labels[op.state] || op.state} · ${op.duration_seconds} с`;
    opNodes.counts.textContent = `Документов: ${op.documents} · чанков: ${op.chunks} · из кэша: ${op.cached} · вычислено: ${op.computed}`;
    opNodes.model.textContent = op.config ? `Модель: ${op.config.model} · размерность: ${op.dimension ?? "ещё неизвестна"}` : "";
    opNodes.stages.forEach(node => node.className = node.dataset.stage === op.stage ? "active" : "");
    opNodes.actual.querySelector("pre").textContent = JSON.stringify(op, null, 2);
  }
  function renderIndex(info, ingestion) {
    const target = $("#rag-index");
    if (!savedNodes) {
      const heading = el("h3"), size = el("p"), counts = el("p"), model = el("p");
      const metadata = detail("Метаданные сохранённого индекса", {}), report = detail("Отчёт загрузки HTML", {});
      target.append(heading, size, counts, model, metadata); $("#rag-ingestion").append(report); savedNodes = {heading, size, counts, model, metadata, report};
    }
    const n = savedNodes;
    for (const node of [n.heading, n.size, n.counts, n.model, n.metadata]) node.hidden = !info;
    n.report.hidden = !ingestion;
    if (ingestion) n.report.querySelector("pre").textContent = JSON.stringify(ingestion, null, 2);
    if (!info) return;
    n.heading.textContent = `Сохранённый SQLite · ${labels[info.state || "ready"] || info.state}`;
    n.size.textContent = `${info.words} очищенных слов · ≈${(info.words / 500).toFixed(1)} страниц по 500 слов · ${info.size_bytes} байт`;
    n.counts.textContent = `Документов: ${info.rows.documents} · чанков/векторов: ${info.rows.chunks} · версия: ${info.version} · ${info.strategy}`;
    n.model.textContent = `Модель: ${info.embedding_config.model} · размерность: ${info.dimension}`;
    n.metadata.querySelector("pre").textContent = JSON.stringify(info, null, 2);
  }
  function updateControls() {
    const data = lastStatus || {}, stages = data.stages || {};
    const busy = submitting || data.operation?.state === "running";
    const available = {documents: true, chunks: !!stages.corpus, embeddings: !!stages.chunks, save: !!stages.embeddings || !!data.index};
    if (!selectedStep) selectedStep = available.save ? "save" : available.embeddings ? "embeddings" : available.chunks ? "chunks" : "documents";
    if (!available[selectedStep]) {
      const position = steps.findIndex(([id]) => id === selectedStep);
      selectedStep = steps.slice(0, position).reverse().find(([id]) => available[id])?.[0] || "documents";
    }
    for (const [id] of steps) {
      $("#rag-" + id + "-controls").hidden = selectedStep !== id;
      const node = navigation.get(id); node.disabled = !available[id];
      if (selectedStep === id) node.setAttribute("aria-current", "step");
      else node.removeAttribute("aria-current");
    }
    $("#rag-index").hidden = selectedStep !== "save";
    $("#rag-ingestion").hidden = selectedStep !== "documents";
    $("#rag-documents").hidden = selectedStep === "save";
    $("#rag-document-preview").hidden = !["documents", "chunks"].includes(selectedStep);
    $("#rag-chunks").hidden = !["chunks", "embeddings"].includes(selectedStep);
    $("#rag-chunk").hidden = !["chunks", "embeddings"].includes(selectedStep);
    if (vectorView) vectorView.node.hidden = !hasVectors || selectedStep !== "embeddings";
    $("#rag-delete-index").hidden = !data.index;
    for (const [id, enabled] of [["ingest", true], ["split", !!stages.corpus], ["embed", !!stages.chunks], ["save", !!stages.embeddings], ["delete-chunks", !!stages.chunks || !!data.index], ["delete-embeddings", !!stages.embeddings || !!data.index], ["delete-index", !!data.index]]) {
      const node = $("#rag-" + id); if (node) node.disabled = busy || !enabled;
    }
    const summary = $("#rag-stage-status");
    if (summary) {
      summary.textContent = selectedStep === "documents" ? `Загружено документов: ${stages.corpus?.documents ?? 0}`
        : selectedStep === "chunks" ? `Чанков: ${stages.chunks?.chunks ?? 0}`
        : selectedStep === "embeddings" ? (stages.embeddings ? `Эмбеддинги готовы · размерность: ${stages.embeddings.dimension}` : "Эмбеддинги ещё не созданы")
        : data.index ? "Индекс опубликован" : "Готово к сохранению индекса";
    }
    const usage = $("#rag-chunk-usage"), chunks = stages.chunks;
    const report = chunks?.strategy === "semantic" ? chunks.report : null;
    usage.hidden = !report;
    usage.textContent = "";
    if (report) {
      const tokens = report.calls === 0 ? 0 : report.usage?.total_tokens
        ?? (Number.isInteger(report.usage?.prompt_tokens) && Number.isInteger(report.usage?.completion_tokens)
          ? report.usage.prompt_tokens + report.usage.completion_tokens : "не сообщены");
      const cost = report.calls === 0 ? 0 : report.cost_usd;
      const billed = typeof cost === "number" && Number.isFinite(cost) && cost >= 0
        ? `${cost.toLocaleString("en-US", {maximumSignificantDigits: 12})} USD` : "недоступна";
      usage.textContent = `LLM: ${report.model || chunks.semantic_config?.model || "не сообщена"} · токены: ${tokens} · стоимость: ${billed}`;
    }
  }
  function chunkOptions() {
    const body = {strategy: $("#rag-strategy").value, size: Number($("#rag-size").value), overlap: Number($("#rag-overlap").value)};
    if (body.strategy === "semantic") {
      body.semantic_auth_mode = $("#rag-semantic-auth-mode").value;
      body.semantic_base_url = $("#rag-semantic-base-url").value.trim();
      body.semantic_model = $("#rag-semantic-model").value.trim();
    }
    return body;
  }
  async function start(kind) {
    if (submitting || lastStatus?.operation?.state === "running") return;
    submitting = true; updateControls();
    try {
      const body = kind === "ingest" ? {urls: $("#rag-urls").value.split(/\n/).map(x => x.trim()).filter(Boolean), use_manifest: $("#rag-manifest").checked}
        : kind === "chunks" ? chunkOptions()
        : kind === "embeddings" ? {base_url: $("#rag-base-url").value.trim(), model: $("#rag-model").value.trim(), dimensions: $("#rag-dimensions").value ? Number($("#rag-dimensions").value) : null, revision: $("#rag-revision").value.trim()} : {};
      await api(`/api/rag/operations/${kind}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      if (lastStatus) lastStatus.operation = {...lastStatus.operation, state: "running"};
      $("#rag-error").textContent = "";
      stop(); refresh();
    } catch (error) { showError(error); }
    finally { submitting = false; updateControls(); }
  }
  async function clearStage(kind) {
    if (submitting || lastStatus?.operation?.state === "running") return;
    submitting = true; updateControls();
    try {
      await api(`/api/rag/stages/${kind}`, {method: "DELETE"});
      stop(); refresh();
    } catch (error) { showError(error); }
    finally { submitting = false; updateControls(); }
  }
  for (const [id, kind] of [["delete-chunks", "chunks"], ["delete-embeddings", "embeddings"], ["delete-index", "index"]]) {
    const node = $("#rag-" + id); if (node) node.onclick = () => clearStage(kind);
  }
  const authMode = $("#rag-semantic-auth-mode");
  const semanticDrafts = new Map();
  let previousAuthMode = "openrouter";
  authMode.onchange = () => {
    const endpoint = $("#rag-semantic-base-url"), model = $("#rag-semantic-model");
    semanticDrafts.set(previousAuthMode, {endpoint: endpoint.value, model: model.value});
    const draft = semanticDrafts.get(authMode.value) || (authMode.value === "omlx"
      ? {endpoint: "http://127.0.0.1:8005/v1", model: ""}
      : {endpoint: "https://openrouter.ai/api/v1", model: "openai/gpt-4.1-mini"});
    endpoint.value = draft.endpoint; model.value = draft.model; previousAuthMode = authMode.value;
    for (const input of [endpoint, model, authMode]) input.dataset.dirty = "true";
  };
  const strategy = $("#rag-strategy");
  if (strategy) strategy.onchange = () => {
    strategy.dataset.dirty = "true";
    const semantic = strategy.value === "semantic";
    $("#rag-semantic-fields").hidden = !semantic;
    $("#rag-size").max = semantic ? "12000" : "100000";
  };
  for (const id of ["urls", "base-url", "model", "dimensions", "revision", "semantic-base-url", "semantic-model", "semantic-auth-mode", "size", "overlap", "strategy"]) {
    const input = $("#rag-" + id); if (input) input.oninput = () => { input.dataset.dirty = "true"; };
  }
  for (const [id, kind] of [["ingest", "ingest"], ["split", "chunks"], ["embed", "embeddings"], ["save", "save"]]) {
    const node = $("#rag-" + id); if (node) node.onclick = () => start(kind);
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
