"use strict";

// One global server and one searchable model field for every purpose.
// Credentials are submitted separately and never stored in this controller.
function createModelSelectors({ $, el, api }) {
  const catalogues = new Map(), pickers = new Set();
  let settings = null, settingsRequest = null, saveRequest = null, connectionRevision = 0;
  async function connection() {
    if (saveRequest) { await saveRequest; return settings; }
    if (settings) return settings;
    if (!settingsRequest) {
      const revision = connectionRevision;
      settingsRequest = api("/api/model-settings").then(async data => {
        if (revision === connectionRevision) settings = data;
        else if (saveRequest) await saveRequest;
        return settings;
      }).finally(() => { settingsRequest = null; });
    }
    return settingsRequest;
  }
  async function saveConnection(patch) {
    const revision = ++connectionRevision, previous = saveRequest;
    catalogues.clear();
    for (const picker of pickers) picker.invalidate();
    const pending = (previous || Promise.resolve()).catch(() => {}).then(() => api("/api/model-settings", {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(patch)
    })).then(saved => {
      settings = saved;
      if (revision === connectionRevision) {
        for (const picker of pickers) picker.load(true);
      }
      return saved;
    });
    saveRequest = pending;
    try { return await pending; }
    finally { if (saveRequest === pending) saveRequest = null; }
  }
  function create({ host, modelId, refreshId, statusId, reasoningId = modelId + "-reasoning", title = "Модель", purpose = "generation",
    active = () => true, onCatalog = () => {}, onChange = () => {} }) {
    const model = el("input", "control model-search"); model.id = modelId; model.type = "text";
    model.placeholder = "Найти модель или ввести ID"; model.autocomplete = "off";
    const list = el("datalist"); list.id = modelId + "-catalogue"; model.setAttribute("list", list.id);
    const refresh = el("button", "mcp-button model-refresh", "↻"); refresh.id = refreshId; refresh.type = "button";
    refresh.title = "Обновить каталог моделей"; refresh.setAttribute("aria-label", refresh.title);
    const status = el("span", "hint model-status"); status.id = statusId; status.setAttribute("role", "status");
    model.setAttribute("aria-describedby", statusId);
    function field(label, input) { const node = el("label", "field"); node.append(el("span", "field-label", label), input); return node; }
    const row = el("div", "model-selector-row"); row.append(field(title, model), refresh);
    const reasoning = el("input", "control"); reasoning.type = "checkbox"; reasoning.id = reasoningId;
    const toggle = el("label", "rag-toggle"); toggle.append(reasoning, el("span", "", "Рассуждения (если модель поддерживает)"));
    host.append(row, list, status, toggle); host.classList.add("model-selector");
    let modelsShown = [], catalogueNote = "";
    let request = 0, controller = null, pending = null;
    const value = () => ({model: model.value.trim(), reasoning_enabled: reasoning.checked});
    function stop() { if (pending) catalogues.delete(pending); request++; controller?.abort(); controller = null; pending = null; refresh.disabled = false; }
    function catalogue(models, message = "") {
      modelsShown = models; catalogueNote = message;
      list.replaceChildren();
      for (const item of models) {
        const option = el("option"); option.value = item.id;
        const prices = item.prompt_price_per_m != null && item.completion_price_per_m != null
          ? ` · $${item.prompt_price_per_m} / $${item.completion_price_per_m} за 1M` : "";
        option.textContent = (item.name && item.name !== item.id ? item.name : item.id) + prices;
        list.append(option);
      }
      status.textContent = message || (models.length ? `Моделей: ${models.length}` : "Каталог пуст. Можно ввести точный ID.");
      if (model.value && !models.some(item => item.id === model.value)) status.textContent += " · Введённый ID сохранён";
      onCatalog(models);
    }
    async function load(force = false) {
      if (!active()) return;
      const revision = connectionRevision, ticket = ++request;
      controller?.abort(); controller = null; pending = null;
      try {
        const config = await connection();
        if (ticket !== request || revision !== connectionRevision || !active()) return;
        const identity = `${config.base_url}:${config.revision}:${purpose}`;
        if (!force && catalogues.has(identity)) { catalogue(catalogues.get(identity)); return; }
        controller = new AbortController(); pending = identity; refresh.disabled = true; status.textContent = "Загрузка моделей…";
        const data = await api(`/api/models?purpose=${purpose}`, {signal: controller.signal});
        if (ticket !== request || revision !== connectionRevision || !active()) return;
        if ((data.base_url != null && data.base_url !== config.base_url) ||
            (data.revision != null && data.revision !== config.revision)) {
          settings = null;
          throw new Error("Каталог принадлежит другому подключению.");
        }
        const models = data.models || []; catalogues.set(identity, models); catalogue(models);
      } catch (error) {
        if (error.name !== "AbortError" && ticket === request && active()) {
          catalogue([], "Каталог недоступен. Можно ввести точный ID или обновить список.");
        }
      } finally { if (ticket === request) { controller = null; pending = null; refresh.disabled = false; } }
    }
    function restore(config = {}) {
      if (!reasoning.dataset.dirty) reasoning.checked = config.reasoning_enabled === true;
      if (model.dataset.dirty) return;
      stop(); model.value = config.model || ""; catalogue([], "Каталог ещё не загружен.");
    }
    function set(config = {}) {
      model.dataset.dirty = ""; reasoning.dataset.dirty = ""; restore(config);
    }
    model.oninput = () => { model.dataset.dirty = "true"; };
    model.onchange = () => { model.dataset.dirty = "true"; if (!controller) catalogue(modelsShown, catalogueNote); onChange(); };
    reasoning.onchange = () => { reasoning.dataset.dirty = "true"; onChange(); };
    refresh.onclick = () => load(true);
    const picker = {value, load, stop, cancel: stop, active, restore, set, invalidate: () => { stop(); catalogue([], "Сервер изменён. Каталог обновляется при открытии сценария."); }, pending: () => pending};
    pickers.add(picker); return picker;
  }
  return {create, connection, saveConnection};
}
if (typeof module !== "undefined") module.exports = createModelSelectors;
else globalThis.createModelSelectors = createModelSelectors;
