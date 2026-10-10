"use strict";

// Durable stages and published index share the operator-configured directory.
function createRagInspector({ state, $, el, api, modelSelectors, onCurrentIndex }) {
  let historical = false;
  let epoch = 0, controller = null, timer = null, indexId = null;
  let previewEpoch = 0, documentRequest = 0, chunkRequest = 0, textRequest = 0;
  let documentOffset = 0, chunkOffset = 0, selectedDocument = null, selectedChunk = null;
  const PAGE = 25;
  let selectedStep = null;
  const steps = [["documents", "Документы"], ["chunks", "Чанки"], ["embeddings", "Эмбеддинги"], ["save", "Индекс"]];
  const navigation = new Map();
  for (const [id, title] of steps) {
    const node = button(title, () => { if (node.disabled) return; selectedStep = id; updateControls(); syncPreview().catch(showError); loadModelsForStage(); });
    node.className = "mcp-button rag-step"; node.id = "rag-step-" + id; navigation.set(id, node); $("#rag-navigation").append(node);
  }
  let vectorView = null, vectorVersion = 0, vectorFingerprint = null;
  let working = false, hasChunks = true, hasVectors = true, submitting = false, seeded = false, lastStatus = null;
  const workingQuery = () => working ? "&working=true" : "";
  const visible = () => state.user?.role === "admin" && !historical && state.workspace === "settings" && state.settingsScope === "app" && state.section === "rag" && document.visibilityState !== "hidden";
  const labels = { missing: "Индекс отсутствует", stale: "Корпус изменён — перестройте индекс", running: "Операция выполняется",
    ready: "Индекс готов", complete: "Операция завершена", interrupted: "Операция прервана", error: "Ошибка операции" };
  function stop() {
    epoch++;
    for (const picker of modelPickers) picker.stop();
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
  function selectionRows(target, selected) {
    for (const row of target.querySelectorAll(".rag-list-row")) {
      const active = row.dataset.itemId === selected;
      row.classList.toggle("selected", active);
      row.setAttribute("aria-pressed", String(active));
    }
  }
  function listRow(id, title, metadata, action) {
    const row = button("", action); row.className = "mcp-button rag-list-row";
    row.dataset.itemId = id;
    row.append(el("span", "rag-row-title", title), el("span", "muted rag-row-meta", metadata));
    return row;
  }
  async function documents(offset = documentOffset) {
    const token = epoch, preview = previewEpoch, request = ++documentRequest;
    const data = await api(`/api/rag/documents?offset=${offset}&limit=${PAGE}${workingQuery()}`);
    if (token !== epoch || preview !== previewEpoch || request !== documentRequest || !visible()) return;
    documentOffset = offset;
    const target = $("#rag-documents"); target.replaceChildren(el("h3", "", working ? "Загруженные документы" : "Документы сохранённого индекса"));
    data.items.forEach((item) => {
      const row = listRow(item.document_id, item.title, `${item.words} слов · ${item.characters} символов`, () => selectDocument(item).catch(showError));
      target.append(row);
    });
    selectionRows(target, selectedDocument?.document_id);
    pager(target, offset, data.items.length, (next) => documents(next).catch(showError));
  }
  async function selectDocument(item, offset = 0) {
    const changed = selectedDocument?.document_id !== item.document_id;
    selectedDocument = item;
    selectionRows($("#rag-documents"), item.document_id);
    if (changed) {
      selectedChunk = null; textRequest++; vectorView = null;
      $("#rag-chunk").replaceChildren();
      $("#rag-document-preview").replaceChildren();
    }
    const token = epoch, preview = previewEpoch, request = ++chunkRequest;
    const target = $("#rag-chunks");
    target.replaceChildren();
    if (working && (changed || !$("#rag-document-preview").children.length)) {
      const text = await api(`/api/rag/documents/${encodeURIComponent(item.document_id)}?offset=0&limit=10000`);
      if (token !== epoch || preview !== previewEpoch || request !== chunkRequest || !visible()) return;
      const cleaned = detail(`Очищенный документ · первые ${Math.min(text.characters, 10000)} из ${text.characters} символов`, {});
      cleaned.querySelector("pre").textContent = text.text;
      $("#rag-document-preview").replaceChildren(el("h3", "", item.title), detail("Метаданные документа", item), cleaned);
    }
    if (!hasChunks) return;
    const data = await api(`/api/rag/documents/${encodeURIComponent(item.document_id)}/chunks?offset=${offset}&limit=${PAGE}${workingQuery()}`);
    if (token !== epoch || preview !== previewEpoch || request !== chunkRequest || !visible() || selectedDocument.document_id !== item.document_id) return;
    chunkOffset = offset;
    if (!working) target.replaceChildren(el("h3", "", item.title), detail("Метаданные документа", item));
    data.items.forEach((chunk) => target.append(listRow(chunk.chunk_id, chunk.section || "Без раздела", `[${chunk.start}, ${chunk.end}) · ${chunk.end - chunk.start} символов`, () => selectChunk(chunk).catch(showError))));
    selectionRows(target, selectedChunk);
    pager(target, offset, data.items.length, (next) => selectDocument(item, next).catch(showError));
  }
  async function selectChunk(item) {
    if (selectedChunk !== item.chunk_id) { $("#rag-chunk").replaceChildren(); vectorView = null; }
    selectedChunk = item.chunk_id;
    selectionRows($("#rag-chunks"), selectedChunk);
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
    vector.hidden = !hasVectors || !["embeddings", "save"].includes(selectedStep); target.append(vector);
    vectorView = { node: vector, invalidate: () => {
      loaded = false;
      for (const node of [...vector.querySelectorAll("pre")]) node.remove();
      vector.hidden = !hasVectors || !["embeddings", "save"].includes(selectedStep);
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
      const initializing = !seeded;
      const stages = data.stages;
      if (!seeded) {
        const preparation = stages?.corpus?.preparation_config;
        if (stages?.chunks?.semantic_config) semanticPicker.restore(stages.chunks.semantic_config);
        const strategy = $("#rag-preparation-strategy");
        if (!strategy.dataset.dirty) strategy.value = stages?.corpus?.preparation_strategy || "programmatic";
        if (preparation) preparationPicker.restore(preparation);
        const timeout = $("#rag-preparation-timeout-seconds");
        if (!timeout.dataset.dirty) timeout.value = data.operation?.preparation_report?.config?.timeout_seconds ?? preparation?.timeout_seconds ?? 600;
        updatePreparationFields();
        if (stages?.chunks) {
          const saved = stages.chunks;
          for (const [id, value] of [["size", saved.size], ["overlap", saved.overlap], ["strategy", saved.strategy === "semantic" ? "semantic" : "fixed"]]) {
            const input = $("#rag-" + id); if (input && !input.dataset.dirty) input.value = value;
          }
          $("#rag-strategy")?.onchange?.();
        }
        const defaults = data.embedding_defaults;
        if (defaults) {
          embeddingPicker.restore(defaults);
          for (const [id, key] of [["dimensions", "dimensions"], ["revision", "revision"]]) {
            const input = $("#rag-" + id); if (!input.dataset.dirty) input.value = defaults[key] ?? "";
          }
        }
        const urls = $("#rag-urls"); if (urls && !urls.dataset.dirty && stages?.corpus) urls.value = stages.corpus.urls.join("\n");
        seeded = true;
      }
      if ($("#rag-manifest-label")) $("#rag-manifest-label").hidden = !data.manifest_available;
      updateControls();
      if (initializing) loadModelsForStage();
      await syncPreview();
    } catch (error) { if (error.name !== "AbortError" && token === epoch) showError(error); }
    finally {
      if (token === epoch) { controller = null; if (visible()) timer = setTimeout(refresh, 1000); }
    }
  }
  async function syncPreview() {
    const data = lastStatus || {}, stages = data.stages || {};
    working = selectedStep !== "save" ? !!stages.corpus : !data.index && !!stages.corpus;
    hasChunks = working ? !!stages.chunks : !!data.index;
    hasVectors = working ? !!stages.embeddings : !!data.index;
    const generation = working ? `working:${stages.corpus.fingerprint}:${stages.chunks?.fingerprint || ""}`
      : data.index ? `published:${data.index.index_id}` : null;
    const nextVectorFingerprint = working ? stages.embeddings?.embedding_fingerprint : data.index?.embedding_fingerprint;
    let preview = previewEpoch;
    if (generation !== indexId || (generation && !$("#rag-documents").children.length)) {
      vectorView = null; preview = ++previewEpoch; indexId = generation; selectedDocument = null; selectedChunk = null;
      for (const id of ["documents", "document-preview", "chunks", "chunk"]) $("#rag-" + id).replaceChildren();
      if (generation) await documents(0);
    }
    if (generation !== indexId || preview !== previewEpoch || !visible()) return;
    if (vectorFingerprint !== nextVectorFingerprint) {
      vectorFingerprint = nextVectorFingerprint; vectorVersion++;
      vectorView?.invalidate();
    }
    updateControls();
  }
  // Patch mounted nodes; polling never detaches focused controls or details.
  let opNodes = null, savedNodes = null;
  function renderOperation(op) {
    const target = $("#rag-operation");
    if (!opNodes) {
      const summary = el("p"), counts = el("p"), model = el("p");
      const actual = detail("Фактическое состояние операции", {}); target.append(summary, counts, model, actual);
      opNodes = {summary, counts, model, actual};
    }
    target.hidden = !op; if (!op) return;
    const kinds = {ingest: "Загрузка документов", chunks: "Разбиение на чанки", embeddings: "Создание эмбеддингов", save: "Сохранение индекса", index: "Построение индекса"};
    opNodes.summary.textContent = `${kinds[op.kind] || "Операция"} · ${labels[op.state] || op.state}${op.duration_seconds == null ? "" : ` · ${op.duration_seconds} с`}`;
    opNodes.counts.textContent = [["Документов", op.documents], ["чанков", op.chunks], ["из кэша", op.cached], ["вычислено", op.computed]]
      .filter(([, value]) => value != null).map(([title, value]) => `${title}: ${value}`).join(" · ");
    opNodes.counts.hidden = !opNodes.counts.textContent;
    opNodes.model.textContent = op.config ? `Модель: ${op.config.model} · размерность: ${op.dimension ?? "ещё неизвестна"}` : "";
    opNodes.model.hidden = !opNodes.model.textContent;
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
      node.className = "mcp-button rag-step" + (data.operation?.state === "running" && data.operation.stage === id ? " running" : "");
      node.title = data.operation?.state === "running" && data.operation.stage === id ? "Выполняется сейчас" : "";
    }
    $("#rag-index").hidden = selectedStep !== "save" || !data.index;
    $("#rag-ingestion").hidden = selectedStep !== "documents" || !data.ingestion;
    $("#rag-documents").hidden = false;
    $("#rag-document-preview").hidden = !["documents", "chunks"].includes(selectedStep);
    $("#rag-chunks").hidden = !["chunks", "embeddings", "save"].includes(selectedStep);
    $("#rag-chunk").hidden = !["chunks", "embeddings", "save"].includes(selectedStep);
    if (vectorView) vectorView.node.hidden = !hasVectors || !["embeddings", "save"].includes(selectedStep);
    $("#rag-delete-index").hidden = !data.index;
    for (const [id, enabled] of [["ingest", true], ["split", !!stages.corpus], ["embed", !!stages.chunks], ["save", !!stages.embeddings], ["delete-chunks", !!stages.chunks || !!data.index], ["delete-embeddings", !!stages.embeddings || !!data.index], ["delete-index", !!data.index]]) {
      const node = $("#rag-" + id); if (node) node.disabled = busy || !enabled || (id === "embed" && !$("#rag-model").value);
    }
    $("#rag-stage-heading").textContent = steps.find(([id]) => id === selectedStep)[1];
    const summary = $("#rag-stage-status");
    if (summary) {
      summary.textContent = selectedStep === "documents" ? `Загружено документов: ${stages.corpus?.documents ?? 0}`
        : selectedStep === "chunks" ? `Чанков: ${stages.chunks?.chunks ?? 0}`
        : selectedStep === "embeddings" ? (stages.embeddings ? `Эмбеддинги готовы · размерность: ${stages.embeddings.dimension}` : "Эмбеддинги ещё не созданы")
        : data.index ? "Индекс опубликован" : "Готово к сохранению индекса";
    }
  }
  function chunkOptions() {
    const body = {strategy: $("#rag-strategy").value, size: Number($("#rag-size").value), overlap: Number($("#rag-overlap").value)};
    if (body.strategy === "semantic") {
      body.semantic_reasoning_enabled = semanticPicker.value().reasoning_enabled;
      body.semantic_model = $("#rag-semantic-model").value.trim();
    }
    return body;
  }
  function preparationOptions() {
    const body = {urls: $("#rag-urls").value.split(/\n/).map(x => x.trim()).filter(Boolean), use_manifest: $("#rag-manifest").checked,
      preparation_strategy: $("#rag-preparation-strategy").value};
    if (body.preparation_strategy === "llm") {
      body.preparation_reasoning_enabled = preparationPicker.value().reasoning_enabled;
      body.preparation_timeout_seconds = Number($("#rag-preparation-timeout-seconds").value);
      body.preparation_model = $("#rag-preparation-model").value.trim();
    }
    return body;
  }
  async function start(kind) {
    if (submitting || lastStatus?.operation?.state === "running") return;
    submitting = true; updateControls();
    try {
      const body = kind === "ingest" ? preparationOptions()
        : kind === "chunks" ? chunkOptions()
        : kind === "embeddings" ? {reasoning_enabled: embeddingPicker.value().reasoning_enabled, model: $("#rag-model").value.trim(), dimensions: $("#rag-dimensions").value ? Number($("#rag-dimensions").value) : null, revision: $("#rag-revision").value.trim()} : {};
      const model = kind === "embeddings" ? body.model : body.semantic_model ?? body.preparation_model;
      if (model !== undefined && !model) throw new Error("Выберите модель или введите её ID.");
      await api(`/api/rag/operations/${kind}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      if (lastStatus) lastStatus.operation = {...lastStatus.operation, state: "running"};
      $("#rag-error").textContent = "";
      open();
    } catch (error) { showError(error); }
    finally { submitting = false; updateControls(); }
  }
  async function clearStage(kind) {
    if (submitting || lastStatus?.operation?.state === "running") return;
    submitting = true; updateControls();
    try {
      await api(`/api/rag/stages/${kind}`, {method: "DELETE"});
      open();
    } catch (error) { showError(error); }
    finally { submitting = false; updateControls(); }
  }
  for (const [id, kind] of [["delete-chunks", "chunks"], ["delete-embeddings", "embeddings"], ["delete-index", "index"]]) {
    const node = $("#rag-" + id); if (node) node.onclick = () => clearStage(kind);
  }
  const semanticPicker = modelSelectors.create({host: $("#rag-semantic-picker"), modelId: "rag-semantic-model",
    refreshId: "rag-model-refresh", statusId: "rag-model-status", title: "Модель разбиения",
    active: () => visible() && selectedStep === "chunks" && $("#rag-strategy").value === "semantic"});
  const preparationPicker = modelSelectors.create({host: $("#rag-preparation-picker"), modelId: "rag-preparation-model",
    refreshId: "rag-preparation-model-refresh", statusId: "rag-preparation-model-status", title: "Модель подготовки",
    active: () => visible() && selectedStep === "documents" && $("#rag-preparation-strategy").value === "llm"});
  const embeddingPicker = modelSelectors.create({host: $("#rag-embedding-picker"), modelId: "rag-model",
    refreshId: "rag-embedding-model-refresh", statusId: "rag-embedding-model-status", title: "Модель эмбеддингов", purpose: "embedding",
    active: () => visible() && selectedStep === "embeddings", onChange: () => { if (lastStatus) updateControls(); }});
  semanticPicker.set({model: ""});
  preparationPicker.set({model: ""});
  embeddingPicker.set({model: ""});
  const modelPickers = [semanticPicker, preparationPicker, embeddingPicker];
  function loadModelsForStage() {
    for (const picker of modelPickers) {
      if (picker.active()) picker.load(); else picker.stop();
    }
  }
  function updatePreparationFields() {
    const llm = $("#rag-preparation-strategy").value === "llm";
    $("#rag-preparation-fields").hidden = !llm;
  }
  $("#rag-preparation-strategy").onchange = () => {
    $("#rag-preparation-strategy").dataset.dirty = "true";
    updatePreparationFields(); loadModelsForStage();
  };
  const strategy = $("#rag-strategy");
  if (strategy) strategy.onchange = () => {
    strategy.dataset.dirty = "true";
    const semantic = strategy.value === "semantic";
    $("#rag-semantic-fields").hidden = !semantic;
    $("#rag-size-label").textContent = semantic ? "Максимальный размер чанка, символов" : "Размер чанка, символов";
    $("#rag-size").max = semantic ? "12000" : "100000";
    if (semantic) loadModelsForStage(); else semanticPicker.cancel();
  };
  for (const id of ["preparation-timeout-seconds", "preparation-strategy", "urls", "dimensions", "revision", "size", "overlap", "strategy"]) {
    const input = $("#rag-" + id); if (input && !input.oninput) input.oninput = () => { input.dataset.dirty = "true"; };
  }
  for (const [id, kind] of [["ingest", "ingest"], ["split", "chunks"], ["embed", "embeddings"], ["save", "save"]]) {
    const node = $("#rag-" + id); if (node) node.onclick = () => start(kind);
  }
  function snapshotHeader(saved) {
    if (state.section !== "rag" || (!saved && state.settingsScope !== "app")) return;
    $("#settings-description").textContent = saved ? "Источники и контекст конкретного ответа" : "Документы, чанки и фактическая индексация";
    $("#settings-scope").textContent = saved ? "Для сохранённого ответа" : "Для всего приложения";
  }
  function syncChatControls() {
    const unavailable = historical || state.settingsScope !== "chat" || !state.current;
    $("#rag-workflow").hidden = historical || state.settingsScope !== "app" || state.user?.role !== "admin";
    $("#rag-current-chat").hidden = unavailable;
    $("#rag-current-chat").querySelectorAll(".control").forEach((field) => { field.disabled = unavailable; });
    if (state.section === "rag") {
      $("#settings-scope").hidden = !historical;
      $("#save-status").classList.toggle("hidden", unavailable);
    }
  }
  function showSnapshot(snapshot) {
    stop(); historical = true; snapshotHeader(true); syncChatControls();
    $("#rag-workflow").hidden = true;
    const target = $("#rag-answer-snapshot"); target.hidden = false;
    const back = button("К текущему индексу", onCurrentIndex || open);
    back.hidden = state.user?.role !== "admin";
    // Presentation follows the immutable answer flags, never the current chat controls.
    const rewriteEnabled = snapshot.config?.rewrite_enabled ?? snapshot.rewrite?.enabled ?? false;
    const filterEnabled = snapshot.config?.filter_enabled === true;
    const parameters = {version: snapshot.version, index: snapshot.index, top_k: snapshot.top_k,
      duration_seconds: snapshot.duration_seconds, retrieval_seconds: snapshot.timings?.retrieval_seconds};
    if (rewriteEnabled) parameters.rewrite_seconds = snapshot.timings?.rewrite_seconds;
    if (snapshot.version >= 3) parameters.selection = {candidates_k: snapshot.config.candidates_k, final_k: snapshot.config.final_k, ...snapshot.selection};
    if (filterEnabled) parameters.filter = {top_k: snapshot.config.top_k ?? snapshot.top_k,
      similarity_threshold: snapshot.config.similarity_threshold};
    if (filterEnabled && snapshot.config.candidates_k != null) parameters.filter.candidates_k = snapshot.config.candidates_k;
    if (filterEnabled && snapshot.config.final_k != null) parameters.filter.final_k = snapshot.config.final_k;
    target.replaceChildren(el("h3", "", "Контекст сохранённого ответа"),
      el("p", "hint", "Этот снимок сохранён вместе с ответом и не меняется при перестройке индекса."), back);
    const query = el("details", "rag-detail"); query.append(el("summary", "", rewriteEnabled ? "Исходный и поисковый запросы" : "Запрос"));
    if (rewriteEnabled) {
      query.append(el("h3", "", "Исходный запрос"), el("p", "rag-snapshot-original-query", snapshot.original_query ?? snapshot.query ?? ""),
        el("h3", "", "Запрос для поиска"), el("p", "rag-snapshot-query", snapshot.query || ""));
    } else {
      query.append(el("p", "rag-snapshot-query", snapshot.query ?? snapshot.original_query ?? ""));
    }
    target.append(query);
    const diagnostics = el("details", "rag-detail"); diagnostics.append(el("summary", "", "Параметры и выполнение поиска"), detail("Индекс и параметры поиска", parameters));
    if (snapshot.answer_policy?.weak_context_enabled === true) {
      target.append(detail("Защита от слабого контекста", snapshot.answer_policy));
    }
    if (snapshot.answer) {
      const status = {answered: "Источники и цитаты", verified: "Цитаты проверены", insufficient: "Недостаточно информации", receipt: "Подтверждение напоминания: цитаты не требуются"};
      target.append(el("h3", "", status[snapshot.answer.status] || "Состояние проверки цитат"));
      if (snapshot.answer.reason) {
        const reasons = {low_similarity: snapshot.answer_policy?.gate_stage === "candidates" ? "Все кандидаты ниже порога cosine. Реранкинг не выполнялся; показан предварительный порядок поиска." : "Ни один выбранный фрагмент не достиг порога cosine.", model: "Модель не нашла в источниках достаточно данных для ответа."};
        target.append(el("p", "hint", reasons[snapshot.answer.reason] || snapshot.answer.reason));
      }
      for (const citation of ["answered", "verified"].includes(snapshot.answer.status) ? snapshot.answer.citations || [] : []) {
        const node = el("details", "rag-detail rag-snapshot-citation");
        node.append(el("summary", "", `[${citation.source_id}] ${citation.title || citation.source || citation.chunk_id}`),
          el("blockquote", "rag-source-quote", citation.quote), detail("Источник цитаты", citation));
        target.append(node);
      }
    }
    if (rewriteEnabled) {
      diagnostics.append(detail("Уточнение запроса: модель, токены и стоимость", {...snapshot.rewrite, usage: snapshot.rewrite?.usage ?? "Неизвестно"}),
        detail("История для уточнения запроса", snapshot.history_used || []));
    }
    const rerankEnabled = snapshot.config?.rerank_enabled ?? snapshot.rerank?.enabled ?? false;
    const rerankPerformed = rerankEnabled && Array.isArray(snapshot.rerank?.source_ids) && snapshot.rerank.source_ids.length > 0;
    if (rerankEnabled && !rerankPerformed) diagnostics.append(el("p", "hint", "Ранжирование было включено, но не выполнялось или не завершилось."));
    if (rerankEnabled && snapshot.rerank) diagnostics.append(detail("Ранжирование: модель, токены и стоимость", {...snapshot.rerank,
      usage: snapshot.rerank?.usage ?? "Неизвестно", duration_seconds: snapshot.timings?.rerank_seconds ?? snapshot.rerank?.duration_seconds}));
    target.append(diagnostics);
    const hits = snapshot.hits || [];
    let candidates = (snapshot.version >= 3 || filterEnabled || rerankEnabled) && snapshot.candidates ? snapshot.candidates : hits;
    if (snapshot.version >= 3) candidates = [...candidates].sort((a, b) => a.final_rank - b.final_rank);
    else if (rerankPerformed) {
      // Legacy permutations describe the filtered rerank input, not all candidates.
      candidates = candidates.map((hit) => {
        const after = hits.findIndex(item => item.chunk_id === hit.chunk_id);
        return {...hit, original_rank: after < 0 ? "—" : snapshot.rerank.source_ids[after],
          final_rank: after < 0 ? "—" : after + 1};
      }).sort((a, b) => (typeof a.final_rank === "number" ? a.final_rank : Infinity) - (typeof b.final_rank === "number" ? b.final_rank : Infinity));
    }
    target.append(el("h3", "", rerankPerformed ? "Фрагменты · порядок до и после ранжирования" : "Фрагменты поиска"));
    if (!candidates.length) target.append(el("p", "hint", "Подходящих фрагментов нет"));
    else {
      const table = el("table", "rag-ranking-table"), head = el("thead"), headings = el("tr"), body = el("tbody");
      for (const label of ["Источник", ...(rerankPerformed ? ["До → после"] : []), "Cosine"]) headings.append(el("th", "", label));
      head.append(headings); table.append(head, body);
      const textView = el("div", "rag-selected-fragment");
      const decisions = {kept: "В контексте", threshold: "Ниже порога", final_cap: "Лимит фрагментов"};
      function select(hit, row) {
        for (const node of body.querySelectorAll("tr")) {
          const selected = node === row; node.classList.toggle("selected", selected);
          node.querySelector("button").setAttribute("aria-pressed", String(selected));
        }
        const {text, decision, ...metadata} = hit;
        if (!rerankPerformed) { delete metadata.original_rank; delete metadata.final_rank; }
        if (filterEnabled && decision !== undefined) metadata.decision = decision;
        textView.replaceChildren(el("h3", "", hit.section || hit.title || hit.chunk_id),
          el("pre", "rag-snapshot-text", text || ""), detail("Источник и метаданные фрагмента", metadata));
      }
      for (const [i, hit] of candidates.entries()) {
        const after = hits.findIndex(item => item.chunk_id === hit.chunk_id);
        const row = el("tr", (filterEnabled || rerankEnabled ? "rag-snapshot-candidate" : "") + (after >= 0 ? " rag-snapshot-hit" : ""));
        const source = el("td"); const choose = button(hit.title || hit.source || hit.chunk_id, () => select(hit, row));
        choose.className = "rag-fragment-button"; choose.setAttribute("aria-pressed", "false");
        source.append(choose, el("span", "hint", hit.section || ""));
        if (after >= 0) source.append(el("span", "hint rag-context-selection", "В контексте"));
        else if (filterEnabled && hit.decision) source.append(el("span", "hint", decisions[hit.decision] || hit.decision));
        row.append(source);
        if (rerankPerformed) row.append(el("td", "rag-rank", `${hit.original_rank ?? i + 1} → ${hit.final_rank ?? (after < 0 ? "—" : after + 1)}`));
        row.append(el("td", "rag-cosine", Number.isFinite(hit.score) ? hit.score.toFixed(4) : "—"));
        body.append(row); if (i === 0) select(hit, row);
      }
      target.append(table, textView);
    }
    const context = el("details", "rag-detail");
    context.append(el("summary", "", "Точный контекст для модели"), el("pre", "rag-snapshot-context", snapshot.context || "")); target.append(context);
  }
  function clearSnapshot() {
    if (!historical) return;
    stop(); historical = false; snapshotHeader(false); syncChatControls(); $("#rag-workflow").hidden = state.settingsScope !== "app";
    $("#rag-answer-snapshot").hidden = true; $("#rag-answer-snapshot").replaceChildren();
    if (visible()) { refresh(); loadModelsForStage(); }
  }
  function open() {
    historical = false; snapshotHeader(false); syncChatControls(); $("#rag-workflow").hidden = state.settingsScope !== "app"; $("#rag-answer-snapshot").hidden = true;
    stop(); refresh(); loadModelsForStage();
  }
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", stop);
    document.addEventListener("visibilitychange", () => { stop(); if (visible()) { refresh(); loadModelsForStage(); } });
  }
  return { open, stop, showSnapshot, clearSnapshot, syncChatControls };
}
if (typeof module !== "undefined") module.exports = createRagInspector;
else globalThis.createRagInspector = createRagInspector;
