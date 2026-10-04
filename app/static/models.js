"use strict";

// One catalogue/controller and one searchable provider/model field for every purpose.
// Runtime credentials never enter the browser; the compatible endpoint is global.
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
  async function saveConnection(baseUrl) {
    const revision = ++connectionRevision, previous = saveRequest;
    for (const picker of pickers) if (picker.value().provider === "compatible") picker.invalidate();
    const pending = (previous || Promise.resolve()).catch(() => {}).then(() => api("/api/model-settings", {
      method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({compatible_base_url: baseUrl.trim()})
    })).then(saved => {
      if (revision === connectionRevision) {
        settings = saved;
        for (const picker of pickers) if (picker.value().provider === "compatible") picker.load(true);
      }
      return saved;
    });
    saveRequest = pending;
    try { return await pending; }
    finally { if (saveRequest === pending) saveRequest = null; }
  }
  function create({ host, modelId, providerId, refreshId, statusId, title = "Модель", purpose = "generation",
    active = () => true, onCatalog = () => {}, onChange = () => {} }) {
    const provider = el("select", "control"); provider.id = providerId;
    for (const [value, label] of [["openrouter", "OpenRouter"], ["compatible", "OpenAI-совместимый"]]) {
      const option = el("option", "", label); option.value = value; provider.append(option);
    }
    provider.value = "openrouter";
    const model = el("input", "control model-search"); model.id = modelId; model.type = "text";
    model.placeholder = "Найти модель или ввести ID"; model.autocomplete = "off";
    const list = el("datalist"); list.id = modelId + "-catalogue"; model.setAttribute("list", list.id);
    const refresh = el("button", "mcp-button model-refresh", "↻"); refresh.id = refreshId; refresh.type = "button";
    refresh.title = "Обновить каталог моделей"; refresh.setAttribute("aria-label", refresh.title);
    const status = el("span", "hint model-status"); status.id = statusId; status.setAttribute("role", "status");
    model.setAttribute("aria-describedby", statusId);
    function field(label, input) { const node = el("label", "field"); node.append(el("span", "field-label", label), input); return node; }
    const row = el("div", "model-selector-row"); row.append(field("Провайдер", provider), field(title, model), refresh);
    host.append(row, list, status); host.classList.add("model-selector");
    let modelsShown = [], catalogueNote = "";
    let request = 0, controller = null, pending = null, previousProvider = "openrouter";
    const drafts = new Map();
    const value = () => ({provider: provider.value, model: model.value.trim()});
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
      const revision = connectionRevision, selectedProvider = provider.value, ticket = ++request;
      controller?.abort(); controller = null; pending = null;
      try {
        const config = await connection();
        if (ticket !== request || revision !== connectionRevision || selectedProvider !== provider.value || !active()) return;
        const base = selectedProvider === "openrouter" ? "https://openrouter.ai/api/v1" : config.compatible_base_url;
        const identity = `${selectedProvider}:${base}:${purpose}`;
        if (!force && catalogues.has(identity)) { catalogue(catalogues.get(identity)); return; }
        controller = new AbortController(); pending = identity; refresh.disabled = true; status.textContent = "Загрузка моделей…";
        const data = await api(`/api/models?provider=${selectedProvider}&purpose=${purpose}`, {signal: controller.signal});
        if (ticket !== request || revision !== connectionRevision || selectedProvider !== provider.value || !active()) return;
        const models = data.models || []; catalogues.set(identity, models); catalogue(models);
      } catch (error) {
        if (error.name !== "AbortError" && ticket === request && active()) {
          catalogue([], "Каталог недоступен. Можно ввести точный ID или обновить список.");
        }
      } finally { if (ticket === request) { controller = null; pending = null; refresh.disabled = false; } }
    }
    function restore(config = {}) {
      if (provider.dataset.dirty || model.dataset.dirty) return;
      stop(); provider.value = config.provider === "compatible" ? "compatible" : "openrouter";
      model.value = config.model || ""; previousProvider = provider.value;
      drafts.clear(); drafts.set(previousProvider, model.value); catalogue([], "Каталог ещё не загружен.");
    }
    function set(config = {}) {
      provider.dataset.dirty = ""; model.dataset.dirty = ""; restore(config);
    }
    provider.onchange = () => {
      drafts.set(previousProvider, model.value); stop();
      model.value = drafts.get(provider.value) ?? ""; previousProvider = provider.value;
      provider.dataset.dirty = model.dataset.dirty = "true"; catalogue([], "Каталог ещё не загружен."); onChange(); load();
    };
    model.oninput = () => { model.dataset.dirty = "true"; };
    model.onchange = () => { model.dataset.dirty = "true"; drafts.set(provider.value, model.value); if (!controller) catalogue(modelsShown, catalogueNote); onChange(); };
    refresh.onclick = () => load(true);
    const picker = {value, load, stop, cancel: stop, active, restore, set, invalidate: () => { stop(); catalogue([], "Сервер изменён. Каталог обновляется при открытии сценария."); }, pending: () => pending};
    pickers.add(picker); return picker;
  }
  return {create, connection, saveConnection};
}
if (typeof module !== "undefined") module.exports = createModelSelectors;
else globalThis.createModelSelectors = createModelSelectors;
