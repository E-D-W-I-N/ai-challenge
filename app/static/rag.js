"use strict";

// Durable stages and published index share the operator-configured directory.
function createRagInspector({ state, $, el, api }) {
  let historical = false;
  let epoch = 0, controller = null, timer = null, indexId = null;
  let previewEpoch = 0, documentRequest = 0, chunkRequest = 0, textRequest = 0;
  let documentOffset = 0, chunkOffset = 0, selectedDocument = null, selectedChunk = null;
  const PAGE = 25;
  let selectedStep = null;
  const steps = [["documents", "Документы"], ["chunks", "Чанки"], ["embeddings", "Эмбеддинги"], ["save", "Индекс"]];
  const navigation = new Map();
  for (const [id, title] of steps) {
    const node = button(title, () => { if (node.disabled) return; selectedStep = id; updateControls(); syncPreview().catch(showError); loadGenerationModels(); });
    node.className = "mcp-button rag-step"; node.id = "rag-step-" + id; navigation.set(id, node); $("#rag-navigation").append(node);
  }
  let vectorView = null, vectorVersion = 0, vectorFingerprint = null;
  let working = false, hasChunks = true, hasVectors = true, submitting = false, seeded = false, lastStatus = null;
  const workingQuery = () => working ? "&working=true" : "";
  const visible = () => !historical && state.workspace === "settings" && state.section === "rag" && document.visibilityState !== "hidden";
  const labels = { missing: "Индекс отсутствует", stale: "Корпус изменён — перестройте индекс", running: "Операция выполняется",
    ready: "Индекс готов", complete: "Операция завершена", interrupted: "Операция прервана", error: "Ошибка операции" };
  function stop() {
    epoch++;
    for (const picker of generationPickers) picker.stop();
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
      if (!seeded && stages?.chunks?.semantic_config) {
        for (const [id, key] of [["semantic-base-url", "base_url"], ["semantic-model", "model"], ["semantic-auth-mode", "auth_mode"]]) {
          const input = $("#rag-" + id);
          if (input && !input.dataset.dirty) {
            const value = stages.chunks.semantic_config[key] ?? "openrouter";
            if (id === "semantic-model") semanticPicker.setModels([], value);
            else input.value = value;
          }
        }
      }
      if (!seeded) {
        const preparation = stages?.corpus?.preparation_config;
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
          for (const [id, key] of [["base-url", "base_url"], ["model", "model"], ["dimensions", "dimensions"], ["revision", "revision"]]) {
            const input = $("#rag-" + id); if (input && !input.dataset.dirty) input.value = defaults[key] ?? "";
          }
        }
        const urls = $("#rag-urls"); if (urls && !urls.dataset.dirty && stages?.corpus) urls.value = stages.corpus.urls.join("\n");
        for (const picker of generationPickers) picker.seed();
        seeded = true;
      }
      if ($("#rag-manifest-label")) $("#rag-manifest-label").hidden = !data.manifest_available;
      updateControls();
      if (initializing) loadGenerationModels();
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
      const node = $("#rag-" + id); if (node) node.disabled = busy || !enabled;
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
      body.semantic_auth_mode = $("#rag-semantic-auth-mode").value;
      body.semantic_base_url = $("#rag-semantic-base-url").value.trim();
      body.semantic_model = $("#rag-semantic-model").value.trim();
    }
    return body;
  }
  function preparationOptions() {
    const body = {urls: $("#rag-urls").value.split(/\n/).map(x => x.trim()).filter(Boolean), use_manifest: $("#rag-manifest").checked,
      preparation_strategy: $("#rag-preparation-strategy").value};
    if (body.preparation_strategy === "llm") {
      body.preparation_timeout_seconds = Number($("#rag-preparation-timeout-seconds").value);
      for (const key of ["auth_mode", "base_url", "model"]) body["preparation_" + key] = $("#rag-preparation-" + key.replaceAll("_", "-")).value.trim();
    }
    return body;
  }
  async function start(kind) {
    if (submitting || lastStatus?.operation?.state === "running") return;
    submitting = true; updateControls();
    try {
      const body = kind === "ingest" ? preparationOptions()
        : kind === "chunks" ? chunkOptions()
        : kind === "embeddings" ? {base_url: $("#rag-base-url").value.trim(), model: $("#rag-model").value.trim(), dimensions: $("#rag-dimensions").value ? Number($("#rag-dimensions").value) : null, revision: $("#rag-revision").value.trim()} : {};
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
  const modelCatalogues = new Map();
  function createGenerationPicker(prefix, active, buttonPrefix = prefix + "-") {
    const authMode = $("#rag-" + prefix + "-auth-mode"), endpoint = $("#rag-" + prefix + "-base-url"), model = $("#rag-" + prefix + "-model");
    const refreshButton = $("#rag-" + buttonPrefix + "model-refresh"), statusNode = $("#rag-" + buttonPrefix + "model-status");
    let modelRequest = 0, modelController = null, pendingModelSource = null;
    function modelSource() {
      const mode = authMode.value;
      const base = endpoint.value.trim().replace(/\/+$/, "");
      return {mode, base, key: `${mode}:${base}`};
    }
    function cancelModelRequest() {
      modelRequest++; modelController?.abort(); modelController = null; pendingModelSource = null;
      refreshButton.disabled = false;
    }
    function setModels(models, current) {
      const select = model;
      const options = [{id: "", label: "Выберите модель"}, ...models];
      if (current && !models.some(item => item.id === current)) options.splice(1, 0, {id: current, label: current + " · нет в списке"});
      select.replaceChildren();
      for (const item of options) {
        const price = item.prompt_price_per_m ? ` · $${item.prompt_price_per_m} / $${item.completion_price_per_m} за 1M` : "";
        const option = el("option", "", (item.label || item.id) + price); option.value = item.id;
        option.selected = item.id === current; select.append(option);
      }
      select.value = current || "";
    }
    function catalogueMessage(entry, current) {
      if (entry.error) return entry.error + (current ? " Текущая модель сохранена." : "");
      if (!entry.models.length) return "Сервер не вернул моделей." + (current ? " Текущая модель сохранена." : "");
      if (current && !entry.models.some(item => item.id === current)) return "Текущая модель отсутствует в каталоге; выбор сохранён.";
      return `Доступно моделей: ${entry.models.length}`;
    }
    async function loadModels(force = false) {
      if (!visible() || !active()) return;
      const source = modelSource(), select = model, status = statusNode;
      const cached = modelCatalogues.get(source.key);
      if (!force && cached && !cached.retry) {
        setModels(cached.models, select.value); status.textContent = catalogueMessage(cached, select.value); return;
      }
      if (!force && pendingModelSource === source.key) return;
      cancelModelRequest();
      const request = modelRequest; modelController = new AbortController(); pendingModelSource = source.key;
      const signal = modelController.signal;
      setModels(cached?.models || [], select.value);
      status.textContent = "Загрузка моделей…"; refreshButton.disabled = true;
      try {
        const official = source.mode === "openrouter" && source.base === "https://openrouter.ai/api/v1";
        const data = official && state.models.length && !force && !cached?.retry ? {models: state.models}
          : await api(official ? "/api/models" : `/api/rag/models?auth_mode=${source.mode}&base_url=${encodeURIComponent(source.base)}`, {signal});
        if (request !== modelRequest || source.key !== modelSource().key || !visible()) return;
        const models = data.models || [];
        if (official) state.models = models;
        const entry = {models}; modelCatalogues.set(source.key, entry);
        setModels(models, select.value); status.textContent = catalogueMessage(entry, select.value);
      } catch (error) {
        if (error.name === "AbortError" || request !== modelRequest || source.key !== modelSource().key || !visible()) return;
        const entry = {models: cached?.models || [], error: "Не удалось загрузить модели. Проверьте URL и обновите список."};
        modelCatalogues.set(source.key, entry); status.textContent = catalogueMessage(entry, select.value);
      } finally {
        if (request === modelRequest) { modelController = null; pendingModelSource = null; refreshButton.disabled = false; }
      }
    }
    refreshButton.onclick = () => loadModels(true);
    const drafts = new Map();
    let previousAuthMode = "openrouter";
    authMode.onchange = () => {
      drafts.set(previousAuthMode, {endpoint: endpoint.value, model: model.value});
      const draft = drafts.get(authMode.value) || (authMode.value === "omlx"
        ? {endpoint: "http://127.0.0.1:8005/v1", model: ""}
        : {endpoint: "https://openrouter.ai/api/v1", model: "openai/gpt-4.1-mini"});
      cancelModelRequest(); endpoint.value = draft.endpoint; setModels([], draft.model); previousAuthMode = authMode.value;
      for (const input of [endpoint, model, authMode]) input.dataset.dirty = "true";
      loadModels();
    };
    endpoint.oninput = () => {
      endpoint.dataset.dirty = "true"; cancelModelRequest(); setModels([], model.value);
      statusNode.textContent = "URL изменён — обновите список моделей.";
    };
    endpoint.onchange = () => loadModels();
    model.onchange = () => {
      model.dataset.dirty = "true";
      const entry = modelCatalogues.get(modelSource().key);
      if (entry && !modelController) statusNode.textContent = catalogueMessage(entry, model.value);
    };
    return {active, load: loadModels, cancel: cancelModelRequest, setModels,
      seed: () => { previousAuthMode = authMode.value; },
      restore: (config) => {
        for (const [input, key] of [[endpoint, "base_url"], [model, "model"], [authMode, "auth_mode"]]) {
          if (input.dataset.dirty) continue;
          if (input === model) setModels([], config[key]); else input.value = config[key];
        }
      },
      stop: () => {
        const interrupted = modelCatalogues.get(pendingModelSource);
        if (interrupted) interrupted.retry = true;
        cancelModelRequest();
      }};
  }
  const semanticPicker = createGenerationPicker("semantic", () => selectedStep === "chunks" && $("#rag-strategy").value === "semantic", "");
  const preparationPicker = createGenerationPicker("preparation", () => selectedStep === "documents" && $("#rag-preparation-strategy").value === "llm");
  const generationPickers = [semanticPicker, preparationPicker];
  function loadGenerationModels() {
    for (const picker of generationPickers) {
      if (picker.active()) picker.load(); else picker.stop();
    }
  }
  function updatePreparationFields() {
    const llm = $("#rag-preparation-strategy").value === "llm";
    $("#rag-preparation-fields").hidden = !llm;
  }
  $("#rag-preparation-strategy").onchange = () => {
    $("#rag-preparation-strategy").dataset.dirty = "true";
    updatePreparationFields(); loadGenerationModels();
  };
  const strategy = $("#rag-strategy");
  if (strategy) strategy.onchange = () => {
    strategy.dataset.dirty = "true";
    const semantic = strategy.value === "semantic";
    $("#rag-semantic-fields").hidden = !semantic;
    $("#rag-size-label").textContent = semantic ? "Максимальный размер чанка, символов" : "Размер чанка, символов";
    $("#rag-size").max = semantic ? "12000" : "100000";
    if (semantic) loadGenerationModels(); else semanticPicker.cancel();
  };
  for (const id of ["preparation-timeout-seconds", "preparation-strategy", "preparation-base-url", "preparation-model", "preparation-auth-mode", "urls", "base-url", "model", "dimensions", "revision", "semantic-base-url", "semantic-model", "semantic-auth-mode", "size", "overlap", "strategy"]) {
    const input = $("#rag-" + id); if (input && !input.oninput) input.oninput = () => { input.dataset.dirty = "true"; };
  }
  for (const [id, kind] of [["ingest", "ingest"], ["split", "chunks"], ["embed", "embeddings"], ["save", "save"]]) {
    const node = $("#rag-" + id); if (node) node.onclick = () => start(kind);
  }
  function snapshotHeader(saved) {
    if (state.section !== "rag") return;
    $("#settings-description").textContent = saved ? "Источники и контекст конкретного ответа" : "Документы, чанки и фактическая индексация";
    $("#settings-scope").textContent = saved ? "Для сохранённого ответа" : "Для всего приложения";
  }
  function showSnapshot(snapshot) {
    stop(); historical = true; snapshotHeader(true);
    $("#rag-workflow").hidden = true;
    const target = $("#rag-answer-snapshot"); target.hidden = false;
    const back = button("К текущему индексу", open);
    target.replaceChildren(el("h3", "", "Контекст сохранённого ответа"),
      el("p", "hint", "Этот снимок использован при генерации ответа и не меняется при перестройке индекса."), back,
      el("h3", "", "Запрос"), el("p", "rag-snapshot-query", snapshot.query || ""),
      detail("Индекс и параметры поиска", {version: snapshot.version, index: snapshot.index, top_k: snapshot.top_k, duration_seconds: snapshot.duration_seconds}));
    for (const [i, hit] of (snapshot.hits || []).entries()) {
      const node = el("details", "rag-detail rag-snapshot-hit");
      node.append(el("summary", "", `${i + 1}. ${hit.title || hit.source || hit.chunk_id} · cosine ${Number.isFinite(hit.score) ? hit.score.toFixed(4) : "—"}`));
      const {text, ...metadata} = hit;
      node.append(el("pre", "rag-snapshot-text", text || ""), detail("Метаданные фрагмента", metadata)); target.append(node);
    }
    const context = el("details", "rag-detail");
    context.append(el("summary", "", "Точный контекст для модели"), el("pre", "rag-snapshot-context", snapshot.context || "")); target.append(context);
  }
  function clearSnapshot() {
    if (!historical) return;
    stop(); historical = false; snapshotHeader(false); $("#rag-workflow").hidden = false;
    $("#rag-answer-snapshot").hidden = true; $("#rag-answer-snapshot").replaceChildren();
    if (visible()) { refresh(); loadGenerationModels(); }
  }
  function open() {
    historical = false; snapshotHeader(false); $("#rag-workflow").hidden = false; $("#rag-answer-snapshot").hidden = true;
    stop(); refresh(); loadGenerationModels();
  }
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", stop);
    document.addEventListener("visibilitychange", () => { stop(); if (visible()) { refresh(); loadGenerationModels(); } });
  }
  return { open, stop, showSnapshot, clearSnapshot };
}
if (typeof module !== "undefined") module.exports = createRagInspector;
else globalThis.createRagInspector = createRagInspector;
