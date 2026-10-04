"use strict";

// Чат-клиент. Историю диалога хранит агент на сервере: клиент держит
// только id открытого чата и шлёт новый текст.
//
// Из сети не тянется ничего — ни шрифтов, ни библиотек, ни иконок: репозиторий
// публичный и обязан работать без интернета.

const state = {
  agents: [],          // всё, что вернул GET /api/agents
  hasKey: false,
  current: null,       // открытый агент (полный ответ GET /api/agents/{id})
  models: [],          // каталог моделей для дропдауна
  busy: false,
  commanding: false,   // команда режима задачи в полёте — второй Enter не проводит ход дважды
  abort: null,         // AbortController активного потока
  lastMetrics: null,   // метрики последнего ответа — из них плитка «Контекст»
  applying: null,      // незавершённое применение настроек панели
  panelDirty: false,   // правка панели не доехала до агента
  stick: true,         // лента примотана к низу — доматывать новые ответы
  baseModel: "",       // модель, с которой чат открыли: с ней сверяем смену
  contextStale: false, // модель сменили — прежняя доля окна к новой не относится
  contextPast: false,  // доля окна осталась от прошлого обмена: последний упал
  statusTimer: null,   // таймер, гасящий строку состояния
  prompts: new Map(),  // промпты обменов этой вкладки (см. promptKey)
  memory: null,        // три слоя памяти — ответ ручки, прочитанный на открытие вкладки
  memoryNote: "",      // почему слоёв не видно: читаем, чат не открыт, ручка ответила ошибкой
  invariants: null,    // инварианты — ответ ручки, прочитанный на открытие вкладки
  remindersAvailable: false,
  mcp: null,           // серверы MCP — ответ ручки, прочитанный на открытие вкладки
  workspace: "chat",
  settingsScope: "chat",
  scopeSections: {chat: "model", app: "model", history: "rag"},
  navigationRevision: 0,
  agentsRequest: 0,
  creationRevision: 0,
  chatCreationError: "",
  sectionFocus: new Map(),
  longMemory: null,
  section: "model",
  feedScroll: 0,
  sectionScroll: new Map(),
  settingsRevision: 0,
  profile: null,
  profileError: "",
  profileLoading: null,
  profileDirty: new Set(),
  mcpRequest: null,
  mcpEpoch: 0,
  mcpTimer: null,
  chatTimer: null,
  chatRequest: null,
  chatEpoch: 0,
  mcpConfig: null,
  mcpDirty: false,
  mcpDraftVersion: 0,
  mcpMutation: false,
  mcpDisabled: false,
};

// Legacy SSE prompt fallback only. Exact outbound request bodies are carried
// by assistant transcript rows from the server and survive page/app restart.
const promptKey = (agentId, index) => agentId + ":" + index;

const $ = (sel) => document.querySelector(sel);

const { fmt, has, renderMarkdown, NUMBER_FIELDS, readStopLines, parseResponseFormat, paramWarnings, sameValue } =
  typeof module !== "undefined" ? require("./text.js") : globalThis.ChatText;
const createRecords = typeof module !== "undefined"
  ? require("./records.js") : globalThis.createChatRecords;
const {
  MEMORY_KINDS,
  WORKING_KINDS,
  INVARIANT_KINDS,
  PROFILE_FIELDS,
  memoryTabOpen,
  loadMemory,
  renderMemory,
  workingStatus,
  loadProfile,
  saveProfile,
  loadInvariants,
  loadMcp,
  toolsVisible,
  stopMcpPolling,
  fillKinds,
  addFromForm,
  addWorkingFromForm,
  addInvariantFromForm
} = createRecords({ state, $, el, iconButton, api, json, fmt, onMcpChange: refreshSettingsAvailability });

const createSelectors = typeof module !== "undefined" ? require("./models.js") : globalThis.createModelSelectors;
const modelSelectors = createSelectors({ $, el, api });
const chatModelPicker = modelSelectors.create({host: $("#chat-model-picker"), modelId: "f-model", providerId: "f-provider",
  refreshId: "chat-model-refresh", statusId: "chat-model-status", title: "Модель ответа", active: () => !!state.current && state.workspace === "settings" && state.settingsScope === "chat" && state.section === "model" && document.visibilityState !== "hidden",
  onCatalog: models => { state.models = models; renderWarnings(); }});
const rerankModelPicker = modelSelectors.create({host: $("#rag-rerank-picker"), modelId: "f-rag_rerank_model", providerId: "f-rag_rerank_provider",
  refreshId: "rag-rerank-model-refresh", statusId: "rag-rerank-model-status", title: "Модель ранжирования",
  active: () => !!state.current && state.workspace === "settings" && state.settingsScope === "chat" && state.section === "rag" && $("#f-rag_enabled").checked && $("#f-rag_rerank_enabled").checked && !$("#rag-current-chat").hidden});
const createRag = typeof module !== "undefined" ? require("./rag.js") : globalThis.createRagInspector;
const ragInspector = createRag({ state, $, el, api, modelSelectors, onCurrentIndex: () => openApplicationSettings("rag") });

// Сколько пикселей от низа ленты ещё считается «читатель внизу».
const STICK_SLACK = 80;

// Через сколько гаснет «Применено — со следующего сообщения».
const STATUS_FADE_MS = 5000;

// ─────────────────────────── иконки ───────────────────────────

// Нарисованы путями: CDN нам недоступен, а по картинке читается, что делает
// кнопка.
const ICONS = {
  panelLeft: "M3 3h18v18H3zM9 3v18",
  chat: "M21 12a8 8 0 0 1-8 8H7l-4 3v-5a8 8 0 0 1 8-11h2a8 8 0 0 1 8 8z",
  bot: "M12 3v3M6 8h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM9 13h.01M15 13h.01",
  refresh: "M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5",
  dots: "M12 5h.01M12 12h.01M12 19h.01",
  send: "M4 12l16-8-6 16-2.5-6.5z",
  stop: "M7 7h10v10H7z",
  clock: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  settings: "M9.5 2.5h5l.5 3 2 .8 2.5-1.6 2.5 4.3-2.3 1.8v2.4l2.3 1.8-2.5 4.3-2.5-1.6-2 .8-.5 3h-5l-.5-3-2-.8-2.5 1.6L2 15.5l2.3-1.8v-2.4L2 9.5l2.5-4.3L7 6.8l2-.8zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  wrench: "m14 7 3 3 4-4a6 6 0 0 1-8 8L5 22l-3-3 8-8a6 6 0 0 1 8-8z",
  pencil: "M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  lines: "M4 6h16M4 10h16M4 14h12M4 18h7",
  branch: "M7 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM7 9v10M17 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM17 9v2a4 4 0 0 1-4 4H7",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2",
  memory: "M4 6c0-5 16-5 16 0s-16 5-16 0zM4 6v12c0 5 16 5 16 0V6M4 12c0 5 16 5 16 0",
  shield: "M12 3 3 7v6c0 5 9 9 9 9s9-4 9-9V7zM8 12l3 3 5-6",
  tools: "m14 7 3 3 4-4a6 6 0 0 1-8 8L5 22l-3-3 8-8a6 6 0 0 1 8-8z",
};

// Узел одной строкой: тег, класс, текст. Текст ставится через textContent,
// а не innerHTML, — разметкой страницы он стать не может.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name] || "");
  svg.appendChild(path);
  return svg;
}

// `className` разводит два размера: крупные кнопки шапок и мелкие в списке.
function iconButton(name, title, onClick, className = "icon-btn") {
  const btn = el("button", className);
  btn.type = "button";
  btn.title = title;
  btn.setAttribute("aria-label", title);
  btn.appendChild(icon(name));
  btn.onclick = onClick;
  return btn;
}

// Поле итога по чату. Итог приходит с сервера полем `usage_total` — считает
// его агент, клиент только показывает: здесь не складывается ни одного
// слагаемого, и досчитать «всего» вместо смолчавшего провайдера клиент тоже
// не вправе — два места, где числа считаются, разъезжаются молча.
//
// Итога нет вовсе — это `null`, то есть прочерк в плитке, а не ноль: в чате,
// где не было ни одного ответа с числами, ноль был бы враньём.
function totalField(name) {
  const total = state.current && state.current.usage_total;
  return total ? total[name] : null;
}

// Заполнение контекста в строке метрик относится к последнему обмену:
// доля окна, занятая последним обменом. Усреднять её по диалогу нечего, а при
// смене модели она сбрасывается: окно у новой модели другое, и прежний процент
// к ней не относится. Плитка молчит прочерком, пока не придёт первый ответ
// на новой модели.
//
// Сброс поднимает сама смена модели в панели (`state.contextStale`), а не
// расхождение имён: провайдер вправе вернуть не то имя, которое просили, —
// на `openrouter/auto` он так и делает **всегда**, — и сверка имён гасила бы
// плитку после каждого ответа, навсегда.
// Метрики упавшего обмена приходят с пустыми числами: заполнены `error`,
// `model` и время, а `prompt_tokens`, `total_tokens`, `cost_usd`
// и `context_fill_pct` — `null`. Класть такой набор поверх прежнего значило бы
// гасить плитки ровно в тот момент, когда числа нужнее всего: на записи видно
// ошибку переполнения, а сколько контекста было занято — уже нет.
//
// Поэтому набор не заменяется, а **сливается по полям**: новое число
// побеждает, пустое поле оставляет прежнее. Если провайдер в ошибке всё же
// назвал часть чисел, показаны будут они, а остальное — прежнее.
function mergeMetrics(held, fresh) {
  if (!fresh) return held;
  if (!held) return { ...fresh };
  const out = { ...held };
  Object.keys(fresh).forEach((name) => {
    if (has(fresh[name])) out[name] = fresh[name];
  });
  return out;
}

// Метрики нового ответа. Пометку «модель сменили» снимает **только** эта
// функция: с новыми метриками приходит и свежая доля окна, и разбросать
// снятие по веткам потока значило бы получить путь, на котором плитка
// осталась бы погашенной навсегда.
//
// Снимает её теперь **пришедшая доля окна**, а не сам факт вызова: после
// слияния пустые метрики упавшего обмена несут прежний процент, и снятие
// «по любым метрикам» выпустило бы на экран долю окна прошлой модели.
function keepMetrics(metrics) {
  state.lastMetrics = mergeMetrics(state.lastMetrics, metrics);
  if (metrics && has(metrics.context_fill_pct)) {
    state.contextStale = false;
    state.contextPast = false;
  } else if (metrics && metrics.error) {
    // Обмен упал, а число на экране осталось прежним — плитка об этом скажет.
    state.contextPast = !!(state.lastMetrics && has(state.lastMetrics.context_fill_pct));
  }
}

// Последние известные числа **этого** чата: свёртка метрик его ответов теми же
// правилами, что и в потоке. Открытие чата состояние не сливает, а заменяет:
// иначе числа соседнего чата протекли бы в тот, который открыли следом.
function knownMetrics(agent) {
  let held = null;
  (agent.transcript || []).forEach((turn) => {
    if (turn.role === "assistant" && turn.metrics) held = mergeMetrics(held, turn.metrics);
  });
  return held;
}

function resetMetrics(agent) {
  state.lastMetrics = knownMetrics(agent);
  state.contextStale = false;
  const last = lastAnswerMetrics(agent);
  state.contextPast = !!(
    state.lastMetrics &&
    has(state.lastMetrics.context_fill_pct) &&
    !(last && has(last.context_fill_pct))
  );
}

function contextFill() {
  if (state.contextStale) return null;
  const m = state.lastMetrics;
  if (!m || !has(m.context_fill_pct)) return null;
  return m.context_fill_pct;
}

// Показанное число относится не к последнему обмену, а к прошлому: последний
// упал и своих чисел не принёс. Плитку это не удлиняет — значение остаётся
// на месте, приглушается цветом и объясняется подсказкой.
function contextIsPast() {
  return state.contextPast && contextFill() !== null;
}

// ─────────────────────────── сеть ─────────────────────────────

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

async function detail(res) {
  try {
    const body = await res.json();
    if (body && body.detail) {
      return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch (e) { /* тело не JSON — остаётся код статуса */ }
  return "HTTP " + res.status;
}

function json(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// SSE поверх fetch. AbortController нужен не для красоты: оборванный fetch
// закрывает соединение, сервер это видит и гасит вызов к модели.
async function streamPost(path, body, onEvent, signal) {
  const options = body === null ? { method: "POST", signal } : { ...json("POST", body), signal };
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await detail(res));

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      frame.split("\n").forEach((line) => {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      });
    }
  }
}

// ─────────────────────── список слева ─────────────────────────

async function loadAgents(selectId, navigationTicket = state.navigationRevision) {
  const request = ++state.agentsRequest;
  const data = await api("/api/agents");
  if (request !== state.agentsRequest) return;
  state.agents = data.agents;
  state.hasKey = data.has_key;
  renderList();
  if (navigationTicket !== state.navigationRevision) return;
  if (!state.agents.length) { clearChatSelection(); return; }
  const wanted = selectId || (state.current && state.current.id);
  const exists = state.agents.some((a) => a.id === wanted);
  await openAgent(exists ? wanted : state.agents[0].id, navigationTicket);
}

function renderList() {
  const box = $("#agent-list");
  box.innerHTML = "";
  state.agents.forEach((agent) => box.appendChild(listItem(agent)));
  if (!state.agents.length) box.appendChild(el("p", "list-empty", state.chatCreationError
    ? "Не удалось создать чат: " + state.chatCreationError + ". Повторите кнопкой «Новый чат»." : "Пока нет чатов"));
  renderWorkspaceHead();
}

function clearChatSelection() {
  stopStream(); stopChatPolling(); stopMcpPolling();
  state.current = null; state.memory = null; state.memoryNote = "";
  state.lastMetrics = null; state.contextPast = false; state.contextStale = false;
  state.panelDirty = false; state.settingsRevision += 1;
  ragInspector.clearSnapshot();
  const empty = el("div", "empty");
  empty.append(el("h2", "", "Пока нет чатов"), el("p", "", "Создайте чат кнопкой «Новый чат»."));
  $("#feed").replaceChildren(empty); $("#input").value = "";
  renderTiles(); renderTaskHead(); renderWorkspaceHead(); syncChatControls(); setBusy(false);
}

// Пометка ветки: от кого чат отделился и сколько сообщений унёс. Одна
// функция на список слева и на панель справа — двумя они назвали бы разные
// числа, и какое из двух правда, было бы не видно.
//
// Имя родителя клиент находит сам, по id из `branch`: в списке слева у него
// все чаты, и переименование родителя видно сразу, без перезагрузки. Сервер
// имени не присылает намеренно — копия рядом с родством разошлась бы с ним
// на первом же переименовании.
//
// Родителя могли удалить, и это штатно: ветка самостоятельный чат и его
// переживает. Тогда пометка говорит именно это, а не молчит: смолчи она —
// ветка выглядела бы обычным чатом, хотя начало разговора в ней чужое.
function branchNote(agent) {
  const branch = agent && agent.branch;
  if (!branch) return "";
  const parent = state.agents.find((a) => a.id === branch.parent_id);
  const from = parent ? "«" + parent.label + "»" : "удалённого чата";
  return "ветка от " + from + " — унесено " + fmt.tokens(branch.forked_at) + " сообщений";
}

function listItem(agent) {
  const active = state.current && agent.id === state.current.id;
  const row = el("div", "item" + (active ? " active" : ""));

  const open = el("button", "item-open");
  open.type = "button";
  if (active) open.setAttribute("aria-current", "page");
  const ico = el("span", "item-icon");
  ico.appendChild(icon("chat"));
  // Имя и пометка — в столбик: пометка обязана быть видна, а не только
  // в подсказке. Ветка от чужого начала разговора выглядит как обычный чат,
  // и по одному имени этого не узнать никак.
  const text = el("span", "item-text");
  text.appendChild(el("span", "item-title", agent.label));
  const note = branchNote(agent);
  if (note) text.appendChild(el("span", "item-branch", note));
  open.append(ico, text);
  open.title = agent.label + "\n" + agent.model + (note ? "\n" + note : "");
  open.onclick = async () => {
    const ticket = ++state.navigationRevision;
    if (state.current?.id !== agent.id && !await openAgent(agent.id, ticket)) return;
    if (ticket === state.navigationRevision) showWorkspace("chat");
  };

  const actions = el("div", "item-actions");
  actions.append(
    iconButton("pencil", "Переименовать",
      (ev) => { ev.stopPropagation(); startRename(row, agent); }, "mini"),
    iconButton("trash", "Удалить чат",
      (ev) => { ev.stopPropagation(); askDelete(agent); }, "mini danger")
  );
  const settings = iconButton("settings", "Настройки чата", async (ev) => {
    ev.stopPropagation();
    const ticket = ++state.navigationRevision;
    if (state.current?.id !== agent.id && !await openAgent(agent.id, ticket)) return;
    if (ticket === state.navigationRevision) { showSettings(state.scopeSections.chat, true, "chat"); loadMcp(true); }
  }, "mini item-settings");
  settings.dataset.agentId = agent.id;
  actions.insertBefore(settings, actions.children[actions.children.length - 1]);
  row.append(open, actions);
  return row;
}

// Переименование прямо в списке: Enter сохраняет, Escape отменяет,
// потеря фокуса — тоже сохраняет, чтобы имя не терялось молча.
function startRename(row, agent) {
  const open = row.querySelector(".item-open");
  const input = el("input", "item-rename");
  input.value = agent.label;
  row.replaceChild(input, open);
  row.classList.add("renaming");
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const name = (input.value || "").trim();
    if (save && name && name !== agent.label) {
      try {
        const updated = await api("/api/agents/" + agent.id, json("PATCH", { label: name }));
        Object.assign(agent, updated);
        if (state.current && state.current.id === agent.id) state.current.label = updated.label;
      } catch (err) {
        hint(String(err.message || err), true);
      }
    }
    renderList();
    if (state.current && !state.busy) renderFeed(state.current);
  };

  input.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); finish(false); }
  };
  input.onblur = () => finish(true);
}

// Удаление — с подтверждением: переписку нельзя терять одним промахом мыши.
function askDelete(agent) {
  confirmBox(
    "Удалить чат?",
    `«${agent.label}» удалится вместе со всей перепиской. Восстановить её будет неоткуда.`,
    "Удалить",
    async () => {
      const ticket = ++state.navigationRevision, creation = state.creationRevision;
      let replacementRevision = creation;
      try {
        await api("/api/agents/" + agent.id, { method: "DELETE" });
      } catch (err) {
        hint(String(err.message || err), true);
        return;
      }
      if (state.current?.id === agent.id) clearChatSelection();
      // Чата нет — и промптам его обменов держаться не за что.
      [...state.prompts.keys()]
        .filter((key) => key.startsWith(agent.id + ":"))
        .forEach((key) => state.prompts.delete(key));
      try {
        await loadAgents(undefined, ticket);
        if (!state.agents.length && creation === state.creationRevision) {
          replacementRevision = ++state.creationRevision;
          const replacement = await api("/api/agents", json("POST", {}));
          if (replacementRevision === state.creationRevision) state.chatCreationError = "";
          await loadAgents(replacement.agents[0].id, ticket);
          if (ticket === state.navigationRevision) showWorkspace("chat");
        }
      } catch (err) {
        if (replacementRevision !== state.creationRevision) return;
        state.chatCreationError = String(err.message || err);
        renderList(); hint(state.chatCreationError, true);
      }
    }
  );
}

// ─────────────────────── открытие агента ──────────────────────

async function openAgent(agentId, navigationTicket = state.navigationRevision) {
  if (!agentId) return;
  stopMcpPolling();
  stopChatPolling();
  const epoch = state.chatEpoch;
  stopStream();
  let agent;
  try {
    agent = await api("/api/agents/" + agentId);
  } catch (err) {
    // Агента вытеснили или стёрли перезапуском — обновляем список.
    if (navigationTicket === state.navigationRevision) return loadAgents();
    return false;
  }
  if (epoch !== state.chatEpoch || navigationTicket !== state.navigationRevision) return false;
  ragInspector.clearSnapshot();
  state.current = agent;
  state.settingsRevision += 1;
  // Рабочий редактор принадлежит прежнему чату; глобальные редакторы
  // сохраняются. Переключение рабочей области сюда не попадает.
  $("#mem-working").querySelectorAll(".mem-edit-box").forEach((box) => box.remove());
  $("#mem-work-content").value = "";
  $("#mem-work-kind").value = "";
  workingStatus("");
  state.feedScroll = 0;
  state.panelDirty = false;
  // Чат открывают, чтобы увидеть последнее сообщение: отмотанная лента
  // прошлого чата к новому отношения не имеет.
  state.stick = true;
  // Чат открыт заново: доля окна относится к той модели, что у него сейчас,
  // а числа — только его собственные.
  resetMetrics(agent);
  renderList();
  renderTaskHead();
  renderFeed(agent);
  fillPanel(agent);
  syncChatControls();
  renderTiles();
  scheduleChatPoll();

  // Открыт другой чат — первые два слоя теперь его, а не прежние. Читаем их
  // заново, но только если вкладка открыта: закрытой они не нужны.
  state.memory = null;
  state.memoryNote = "";
  // Значок новых записей — про **этот** чат, и число ему даёт сам чат, а не
  // слои: закрытая вкладка его не гасит, а показывает, сколько агент завёл
  // в открытом чате с тех пор, как ему показывали память. Своего вызова ему
  // здесь не нужно: обе ветки ниже кончаются отрисовкой, а она его считает.
  if (memoryTabOpen() && state.settingsScope === "chat") loadMemory(true);
  else renderMemory();

  const input = $("#input");
  input.value = "";
  autoGrow(input);
  setBusy(false);
  hint("");
  return true;
}

function lastAnswerMetrics(agent) {
  for (let i = agent.transcript.length - 1; i >= 0; i -= 1) {
    const turn = agent.transcript[i];
    if (turn.role === "assistant" && turn.metrics) return turn.metrics;
  }
  return null;
}

// ─────────────────────────── лента ────────────────────────────

function renderFeed(agent) {
  const feed = $("#feed");
  // Подмена содержимого обнуляет прокрутку, поэтому положение после
  // перерисовки задаётся здесь явно и всегда: либо низ, либо то место,
  // где читатель остановился. Иначе браузер выбросит его в начало разговора.
  const keep = state.stick ? null : state.workspace === "chat" ? feed.scrollTop : state.feedScroll;
  feed.innerHTML = "";

  const turns = agent.transcript;
  if (!turns.length) {
    const empty = el("div", "empty");
    empty.append(
      el("h2", "", agent.label),
      el("p", "", "Напишите сообщение — историю разговора хранит сервер, а не браузер.")
    );
    feed.appendChild(empty);
    feed.scrollTop = 0;
    return;
  }

  turns.forEach((turn, index) => {
    // Номер реплики в истории — он же ключ промпта: карточка обязана показать
    // промпт своего обмена, а не соседнего.
    feed.appendChild(
      turn.role === "user" ? userBubble(turn.content) : answerCard(agent, turn, index)
    );
  });
  feed.scrollTop = keep === null ? feed.scrollHeight : keep;
  if (keep !== null) state.feedScroll = keep;
}

function userBubble(text) {
  return el("div", "msg-user", text);
}

// Шапка над лентой: этап, текущий шаг и ожидаемое действие — три оси
// состояния задачи. Режим выключен — шапки нет вовсе.
//
// Кнопок здесь нет намеренно: состояние двигают командами в поле ввода,
// и второй способ на экране читался бы как другое действие. Этап назван
// словом, а не только цветом: по цвету его не различить ни дальтонику,
// ни на чёрно-белом экране.
function renderTaskHead() {
  const box = $("#task-head");
  const task = state.current && state.current.task;
  box.innerHTML = "";
  box.className = "task-head" + (task && state.workspace === "chat" ? " " + task.stage : " hidden");
  if (!task) return;
  box.appendChild(el("div", "task-stage", "Задача · " + task.label));
  if (task.step) box.appendChild(el("div", "task-line", "шаг: " + task.step));
  box.appendChild(el("div", "task-line", "ожидается: " + task.expects));
}

// Шапка карточки: одна на готовый ответ и на тот, в который ещё стримят.
// Провайдер стоит не здесь, а строкой под ответом, рядом со скоростью: всё
// про то, как прошёл обмен, живёт в одном месте, а не в двух.
function cardHead(modelName) {
  const head = el("header", "card-head");
  const ico = el("span", "card-icon");
  ico.appendChild(icon("bot"));
  head.append(ico, el("span", "card-model", modelName));
  return head;
}

function answerCard(agent, turn, index) {
  const card = el("article", "card" + (turn.error ? " failed" : ""));
  const head = cardHead((turn.metrics && turn.metrics.model) || agent.model);

  const actions = el("div", "card-actions");
  actions.append(
    iconButton("refresh", "Перегенерировать", () => regenerate()),
    iconButton("dots", "Показать сырой текст", () => showRaw(card, turn)),
    // Ветка отсюда — у каждого ответа и при любой стратегии: ветвление про
    // структуру разговора, а не про то, что уезжает в модель, и от выбора
    // в переключателе оно не зависит.
    iconButton("branch", "Ветка отсюда", () => forkFrom(agent, index))
  );
  const prompt = state.prompts.get(promptKey(agent.id, index));
  actions.appendChild(iconButton("lines", "Информация о запросе", () => showRequestInfo(card, turn, prompt)));
  head.appendChild(actions);
  card.appendChild(head);

  if (turn.reasoning) card.appendChild(thinkingBlock(turn.reasoning));

  const body = el("div", "card-body md");
  body.innerHTML = renderMarkdown(turn.content);
  card.appendChild(body);

  // Отметка сторожа — выше чисел: задетый запрет весомее приписки про обрезку.
  const guard = guardLine(turn);
  if (guard) card.appendChild(guard);

  // Бейджи вызовов инструментов — из метрик: перерисовка обязана показать
  // то же, что показал живой поток.
  const tools = toolLine(turn);
  if (tools) card.appendChild(tools);

  if (turn.rag) card.appendChild(ragSources(turn.rag));

  const usage = usageLine(turn);
  if (usage) card.appendChild(usage);

  if (turn.error) card.appendChild(el("div", "card-error", turn.error));
  return card;
}

function ragSources(snapshot) {
  const box = el("div", "card-rag");
  box.appendChild(el("div", "field-label", (snapshot.hits || []).length ? "Источники RAG" : "RAG · подходящих фрагментов нет"));
  const list = el("ol", "rag-answer-sources");
  for (const hit of snapshot.hits || []) {
    const item = el("li", "");
    item.appendChild(el("span", "", hit.title || hit.source || hit.chunk_id));
    if (hit.section) item.appendChild(el("span", "muted", " · " + hit.section));
    if (hit.source) item.appendChild(el("div", "muted rag-source-url", hit.source));
    list.appendChild(item);
  }
  box.appendChild(list);
  const inspect = el("button", "mcp-button", "Контекст и фрагменты ответа");
  inspect.type = "button";
  inspect.onclick = () => { ++state.navigationRevision; showSettings("rag", false, "history"); ragInspector.showSnapshot(snapshot); $("#panel-body").scrollTop = 0; };
  box.appendChild(inspect);
  return box;
}

// Ветка отсюда: новый чат уносит разговор по эту карточку включительно
// и открывается. Дальше он обычный чат — переключаться между ветками нечем
// и не надо, они уже в списке слева.
//
// `index` — номер реплики-ответа в истории, значит унести надо `index + 1`
// сообщений: карточка входит в ветку целиком, вместе со своим вопросом.
//
// Пока идёт ответ, уходить из чата нельзя: поток оборвался бы на середине,
// а история родителя дописывается только в конце обмена — ветка унесла бы
// разговор без него.
async function forkFrom(agent, index) {
  if (state.busy) return;
  const ticket = ++state.navigationRevision;
  state.creationRevision += 1;
  let created;
  try {
    created = await api("/api/agents/" + agent.id + "/fork", json("POST", { at: index + 1 }));
  } catch (err) {
    hint(String(err.message || err), true);
    return;
  }
  if (ticket === state.navigationRevision) state.current = null;
  await loadAgents(created.agents[0].id, ticket);
  if (ticket !== state.navigationRevision) return;
  showWorkspace("chat");
  // Открытие чата гасит подсказку, поэтому говорим после него: ветка
  // открылась молча, и без строки было бы непонятно, тот это чат или нет.
  hint("Ветка заведена: унесено " + fmt.tokens(index + 1) + " сообщений.");
}

// Что под ответом: всё про этот обмен и только про него. Первой строкой —
// сколько токенов ушло в модель, сколько она вернула, сколько вышло вместе
// и во что обошлось; второй — как быстро отвечала и кто отвечал.
//
// Итог по всему диалогу живёт в плитках справа, и одно и то же число нигде
// не показывается дважды: лента про обмен, панель про разговор.
//
// Подписи полные — «входные токены», а не «входные»: сокращённая подпись
// оставляет читателя гадать, токены это или что-то другое, а панель справа
// давно называет те же величины целиком. «Всего токенов» названо так же, как
// плитка; цене подпись не нужна — её называет знак доллара.
//
// Пропущенное поле не пишется вовсе: «входные токены 0» вместо «неизвестно»
// было бы неправдой, а сумму, о которой смолчал провайдер, считает сервер (см.
// `_apply_usage` в `app/llm.py`) — сложи её здесь, и «всего» под ответом
// разошлось бы с «Всего токенов» в плитке. Строки нет, только если чисел нет
// совсем. У оборванного ответа она есть: его числа идут в итог чата, он оплачен.
function cutNote(m) {
  if (m.summarized) return " (сводка вместо " + fmt.tokens(m.summarized) + " сообщений)";
  if (m.dropped) return " (окно: отброшено " + fmt.tokens(m.dropped) + " сообщений)";
  return "";
}

// Служебные вызовы — те, что идут к модели ДО ответа и кладут в промпт свою
// врезку. Остался один — сворачивание, — и у него своё: чем занята пауза
// перед ответом и как подписана врезка в просмотре промпта.
//
// Один индекс на оба показа, а не две таблицы: строка состояния обещала бы
// одно, а подпись называла бы другое, и разошлись бы они молча. Какой вызов
// идёт, говорит сервер полем `strategy`; клиент про это не догадывается
// по тексту сообщений. Врезка рабочей памяти подписана здесь же, хотя
// служебного вызова за ней больше нет: её текст в промпте не изменился —
// изменилось только то, чья это работа.
const SERVICE_CALLS = {
  summary: { status: "Сворачиваю начало разговора…", role: "сводка начала разговора" },
  facts: { role: "факты о разговоре" },
};

// Сторож нашёл в ответе слово из списка запрещённых. Строка **своя**, а не
// приписка к числам: у ответа без чисел строки с токенами нет вовсе, а эта
// обязана быть, — и задетый запрет весомее приписки про обрезку.
//
// Формулировка про найденное слово, а не приговор: код показывает совпадение,
// судит человек. Ложные срабатывания штатны и неустранимы — законный отказ
// содержит запрещённое слово («почему не Java?» → «Java здесь не подойдёт»),
// цитата вопроса, слово в имени пакета; словоформы сторож не ловит вовсе.
function guardLine(turn) {
  const hits = (turn.metrics && turn.metrics.banned_hits) || [];
  if (!hits.length) return null;
  const box = el("div", "card-guard");
  for (const hit of hits) {
    box.appendChild(el("div", "guard-hit", "«" + hit.word + "» задело запрет — " + hit.rule));
  }
  return box;
}

// Бейдж вызова инструмента: имя, сервер и сколько он занял. При ошибке —
// пометка: упавший вызов обязан быть виден, иначе ответ на его результате
// читался бы как удачный. Одна форма на живой бейдж из события и на бейдж
// из метрик после перерисовки — двумя они разъехались бы молча.
function toolBadge(run) {
  const text = run.name + " · " + run.server + " · " + Math.round(run.ms || 0) + " мс";
  return el(
    "div",
    "tool-badge" + (run.ok === false ? " failed" : ""),
    (run.ok === false ? "ошибка вызова: " : "") + text
  );
}

// Бейджи обмена при перерисовке ленты — из метрик, как отметка сторожа:
// иначе после перезагрузки страницы они пропали бы вместе с потоком.
function toolLine(turn) {
  const calls = (turn.metrics && turn.metrics.tool_calls) || [];
  if (!calls.length) return null;
  const box = el("div", "card-tools");
  for (const run of calls) box.appendChild(toolBadge(run));
  return box;
}

function usageLine(turn) {
  const m = turn.metrics;
  if (!m) return null;

  const tokens = [];
  if (has(m.prompt_tokens)) {
    // Сколько сообщений не уехало дословно — приписано ровно к тому числу,
    // которое от этого уменьшилось. Отдельной плитки у обрезки нет намеренно:
    // плитки про весь диалог, а срезано — в этом обмене.
    //
    // Два ключа, два разных слова: сводка начало **заменила** и его ещё можно
    // прочитать в промпте запроса, окно его **отбросило** совсем. Назови оба
    // одинаково — и «отброшено» стало бы неправдой про сводку, а «вместо»
    // про окно. Обрезка бывает только выбранная, и молчать о ней нельзя.
    tokens.push("входные токены " + fmt.tokens(m.prompt_tokens) + cutNote(m));
  }
  if (has(m.completion_tokens)) {
    // Токены рассуждения провайдер кладёт **внутрь** completion_tokens: на
    // думающей модели выход заметно больше видимого текста. Поэтому они
    // названы отдельным числом, а не вычтены молча: вычитание сделало бы
    // «выход» не тем, что прислал провайдер.
    const think = m.reasoning_tokens
      ? " (из них " + fmt.tokens(m.reasoning_tokens) + " рассуждение)"
      : "";
    tokens.push("выходные токены " + fmt.tokens(m.completion_tokens) + think);
  }
  if (has(m.total_tokens)) tokens.push("всего токенов " + fmt.tokens(m.total_tokens));
  if (has(m.cost_usd)) tokens.push(fmt.cost(m.cost_usd));

  const how = [];
  if (m.tokens_per_second) {
    how.push(
      fmt.rate(m.tokens_per_second) + " ток/с" +
      (m.elapsed_ms ? " за " + fmt.sec(m.elapsed_ms) + " с" : "")
    );
  }
  // Первый токен — честный: на думающей модели это момент, когда модель
  // заговорила вообще, а не когда домыслила и пошёл ответ.
  const first = has(m.first_token_ms) ? m.first_token_ms : m.ttft_ms;
  if (has(first)) how.push("первый токен " + fmt.sec(first) + " с");
  if (m.provider) how.push(m.provider);

  if (!tokens.length && !how.length) return null;
  const box = el("div", "card-usage");
  if (tokens.length) box.appendChild(el("div", "usage-tokens", tokens.join(" · ")));
  if (how.length) box.appendChild(el("div", "usage-how", how.join(" · ")));
  return box;
}

// Строка состояния в карточке: что происходит, пока ответа ещё нет.
// Индикатор неопределённый — крутится, но ничего не отмеряет: сворачивание
// это один вызов к модели, и доли выполнения у него нет. Полоса с процентами
// на его месте называла бы числа, которых никто не знает.
function cardStatus(text) {
  const row = el("div", "card-status");
  row.append(el("span", "spinner"), el("span", "card-status-text", text));
  return row;
}

function thinkingBlock(text) {
  const box = el("details", "think");
  const summary = document.createElement("summary");
  const ico = el("span", "card-icon");
  ico.appendChild(icon("clock"));
  summary.append(ico, el("span", "", "Рассуждение"));
  box.append(summary, el("div", "think-body", text));
  return box;
}

function showRaw(card, turn) {
  const body = card.querySelector(".card-body");
  if (card.dataset.raw === "1") {
    body.className = "card-body md";
    body.style.whiteSpace = "";
    body.innerHTML = renderMarkdown(turn.content);
    card.dataset.raw = "0";
  } else {
    body.className = "card-body";
    body.style.whiteSpace = "pre-wrap";
    body.textContent = turn.content;
    card.dataset.raw = "1";
  }
}

// Что уехало в модель этим запросом — целиком и в том же порядке: системный
// промпт, врезка (если она была), непокрытый ею хвост истории и сам вопрос. Переключатель,
// как «Показать сырой текст»: второй клик убирает показанное и возвращает
// карточку как была.
//
// Роли подписаны, потому что без подписей главное в этом показе теряется:
// читателю надо видеть, что начало разговора уехало одной врезкой, а не
// двадцатью сообщениями. Место врезки называет сервер полем `summary_at`
// события `start`, а чем она занята — полем `strategy` там же. Разбирать
// текст сообщений и угадывать по нему клиент не вправе: порядок сборки
// промпта живёт в `Agent.build_prompt`, и вторая его копия здесь разошлась
// бы с первой молча.
function promptRole(msg, index, prompt) {
  // Долговременная память — первой веткой, потому что первой она стоит и
  // в промпте: слой не этого разговора идёт раньше врезки, заменяющей его
  // начало. Порядок веток здесь обязан повторять порядок сообщений там.
  if (index === prompt.memoryAt) return "долговременная память";
  // Рабочая память — второй врезкой и по тому же правилу: она едет при любой
  // обрезке, стратегией не заказывается и ни одну не отменяет. Подпись берётся
  // из того же индекса, что и строка состояния её вызова.
  if (index === prompt.workingAt) return SERVICE_CALLS.facts.role;
  // Состояние задачи — третьей врезкой, тем же порядком, что в промпте.
  if (index === prompt.taskAt) return "состояние задачи";
  if (index === prompt.ragAt) return "контекст RAG";
  // Врезки стратегии может не быть вовсе — окно и «Вся история» не вставляют
  // ничего, и `summary_at` приходит пустым; память не едет, когда её нет или
  // выключатель чата в «выключено». Сравнение строгое именно поэтому:
  // подставь сюда ноль «по умолчанию», и запасная подпись встала бы
  // над первым сообщением промпта там, где никакой врезки нет.
  if (index === prompt.summaryAt) {
    const call = SERVICE_CALLS[prompt.strategy];
    return call ? call.role : "врезка вместо начала разговора";
  }
  if (msg.role === "system") return "системный промпт";
  if (msg.role === "assistant") return "ответ модели";
  return "сообщение пользователя";
}

function showRequestInfo(card, turn, legacyPrompt) {
  const shown = card.querySelector(".prompt-view");
  if (shown) { shown.remove(); return; }
  if (!turn.request_bodies || !turn.request_bodies.length) {
    if (legacyPrompt) return showPrompt(card, legacyPrompt);
    const missing = el("div", "prompt-view");
    missing.appendChild(el("div", "prompt-title", "JSON запроса недоступен: у этого сообщения нет сохранённого тела запроса."));
    card.insertBefore(missing, card.querySelector(".card-body"));
    return;
  }
  const box = el("div", "prompt-view");
  box.appendChild(el("div", "prompt-title", "Фактические JSON-запросы к модели · " + turn.request_bodies.length));
  turn.request_bodies.forEach((body, index) => {
    const row = el("div", "prompt-msg");
    row.appendChild(el("div", "prompt-role", "Запрос " + (index + 1)));
    const code = el("pre", "request-json", JSON.stringify(body, null, 2));
    code.tabIndex = 0;
    code.setAttribute("aria-label", "JSON запроса " + (index + 1));
    row.appendChild(code); box.appendChild(row);
  });
  card.insertBefore(box, card.querySelector(".card-body"));
}

function showPrompt(card, prompt) {
  const shown = card.querySelector(".prompt-view");
  if (shown) {
    shown.remove();
    return;
  }
  const box = el("div", "prompt-view");
  box.appendChild(el("div", "prompt-title", "JSON запроса недоступен. Промпт из события старого обмена (legacy)"));
  prompt.messages.forEach((msg, index) => {
    const row = el("div", "prompt-msg");
    row.append(
      el("div", "prompt-role", promptRole(msg, index, prompt)),
      el("div", "prompt-text", msg.content)
    );
    box.appendChild(row);
  });
  // Над телом ответа, но под «Рассуждением»: показанный промпт — про то, что
  // уехало в модель, и стоять ему выше ответа, к которому он привёл.
  card.insertBefore(box, card.querySelector(".card-body"));
}

// Лента доматывается вниз, только если читатель и так внизу. Отмотал
// вверх — новые куски ответа не дёргают её у него под руками.
function atBottom(feed) {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight <= STICK_SLACK;
}

function scrollFeed() {
  if (!state.stick || state.workspace !== "chat") return;
  const feed = $("#feed");
  feed.scrollTop = feed.scrollHeight;
}

// ─────────────────────── отправка сообщения ───────────────────

function hint(text, isError) {
  const el = $("#composer-hint");
  el.textContent = text || "";
  el.className = "composer-hint" + (isError ? " error" : "");
}

function autoGrow(input) {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 168) + "px";
}

function canCallModel() {
  return !!state.current && ($("#f-provider").value === "compatible" || state.hasKey);
}

function setBusy(busy) {
  state.busy = busy;
  const send = $("#send");
  send.innerHTML = "";
  send.appendChild(icon(busy ? "stop" : "send"));
  send.classList.toggle("stop", busy);
  send.title = busy ? "Остановить" : "Отправить";
  send.setAttribute("aria-label", send.title);
  send.disabled = !busy && !canCallModel();
  $("#input").disabled = busy;
  $("#chat-status").textContent = busy ? "Идёт ответ…" : "";
  // Про ключ в интерфейсе не говорим и менять его отсюда нельзя: репозиторий
  // публичный, ключ живёт в .env и остаётся делом того, кто поднял сервер.
  const unavailable = "OpenRouter не настроен на сервере приложения.";
  if (state.current && !canCallModel()) hint(unavailable, true);
  else if ($("#composer-hint").textContent === unavailable) hint("");
}

function stopStream() {
  if (state.abort) {
    state.abort.abort();
    state.abort = null;
  }
  setBusy(false);
}

// ─────────────────── команды режима задачи ────────────────────
//
// Разбираются здесь, в поле ввода: сервер про слеши не знает — у поля `text`
// одно значение, сообщение человека.
//
// `need` — обязательный текст: без него не уходит ничего, под полем подсказка.
// `asks` — что уедет обменом после **успешной** ручки; нет его — команда
// двигает состояние и молчит.

// Текст обмена — на **переход**, а не на этап: «дальше» с планирования значит
// не то же, что с выполнения. Правило этапа модель и так получит системным
// сообщением, и пересказывать его здесь нечего. Нет строки — нет и обмена:
// у паузы её и не должно быть, а «дальше» и «продолжай» покрыты все.
const MOVE_ASKS = {
  "planning:next": "Приступай к работе.",
  "execution:next": "Проверь сделанное.",
  "validation:next": "Подведи итог.",
  "paused:resume": "Продолжай.",
};

// Переход: ручка двигает этап, а текст обмена берётся по этапу **до** неё.
// Этот же этап едет полем `from` — ход обязан выйти из виденной вершины.
const moveCommand = (move) => ({
  asks: (arg, stage) => MOVE_ASKS[stage + ":" + move],
  run: (arg, id, stage) => taskApi(id, json("PATCH", { move, from: stage })),
});

const COMMANDS = {
  "/task": {
    need: "описание задачи",
    asks: (arg) => arg,
    run: (arg, id) => taskApi(id, json("POST", { description: arg })),
  },
  "/task-next": moveCommand("next"),
  "/task-step": {
    need: "текст шага",
    run: (arg, id) => taskApi(id, json("PATCH", { step: arg })),
  },
  "/task-expect": {
    need: "ожидаемое действие",
    run: (arg, id) => taskApi(id, json("PATCH", { expects: arg })),
  },
  "/task-pause": moveCommand("pause"),
  "/task-resume": moveCommand("resume"),
  "/task-off": { run: (arg, id) => taskApi(id, { method: "DELETE" }) },
};

// Команда или null — обычное сообщение. После имени обязателен пробел или
// конец строки: «/taskfoo» это слово, а не команда, и уйдёт в модель как есть.
// Отдельной функцией без DOM — разбор проверяется без браузера.
function parseCommand(text) {
  const m = /^(\/\S+)(?:\s+([\s\S]*))?$/.exec(String(text || ""));
  const cmd = m && COMMANDS[m[1]];
  return cmd ? { name: m[1], cmd, arg: (m[2] || "").trim() } : null;
}

// Ручка задачи — в **названный** чат: id снят до запроса и сверен после.
async function taskApi(id, options) {
  const answer = await api("/api/agents/" + id + "/task", options);
  if (state.current && state.current.id === id) state.current.task = answer.task;
  renderTaskHead();
  return answer;
}

async function runCommand(parsed) {
  if (parsed.cmd.need && !parsed.arg) {
    hint("После " + parsed.name + " нужен текст: " + parsed.cmd.need + ".", true);
    return;
  }
  // Замок на время команды: двойным Enter иначе уходят два хода и два обмена.
  if (state.commanding) return;
  state.commanding = true;
  // Чат и этап — до ручки: чат успевают переключить, а этап ручка уже сменит.
  const id = state.current.id;
  const from = (state.current.task || {}).stage;
  try {
    // Отказ ручки: на сервере ничего не тронуто, в модель не ушло, что не так
    // — сказано под полем, набранное осталось в нём.
    const answer = await parsed.cmd.run(parsed.arg, id, from).catch((err) => {
      hint(String(err.message || err), true);
      return null;
    });
    // Чат сменили, пока летел ответ: ход записан в свой, обмена чужому не будет.
    if (!answer || !state.current || state.current.id !== id) return;
    hint(answer.task ? "Задача · " + answer.task.label + " · ожидается: " + answer.task.expects
                     : "Режим задачи выключен.");
    const input = $("#input");
    input.value = "";
    autoGrow(input);
    // Ручка уже прошла — промпт соберётся с правилом **нового** этапа.
    // Молчащий переход человек принимал за «ничего не произошло».
    const ask = parsed.cmd.asks && parsed.cmd.asks(parsed.arg, from);
    if (ask && canCallModel()) {
      await exchange("/api/agents/" + id + "/messages", { text: ask }, ask);
    }
  } finally {
    state.commanding = false;
  }
}

async function send() {
  if (state.busy) {
    // Кнопка стала «Стоп»: рвём поток и просим агента прекратить генерацию.
    stopStream();
    if (state.current) {
      api("/api/agents/" + state.current.id + "/cancel", { method: "POST" }).catch(() => {});
    }
    return;
  }
  const input = $("#input");
  const text = (input.value || "").trim();
  if (!text || !state.current) return;

  // Команда состояние двигает, а в модель не ходит: ключ ей не нужен.
  const parsed = parseCommand(text);
  if (parsed) return runCommand(parsed);

  if (!canCallModel()) return;
  await exchange("/api/agents/" + state.current.id + "/messages", { text }, text);
}

async function regenerate() {
  if (state.busy || !state.current || !canCallModel()) return;
  await exchange("/api/agents/" + state.current.id + "/regenerate", null, null);
}

// Один обмен: рисуем пузырь вопроса, карточку ответа и стримим в неё.
async function exchange(path, body, questionText) {
  const feed = $("#feed");
  const agent = state.current;

  // Инвариант живёт здесь, а не у вызывающих: через `exchange` проходит
  // всякая отправка, и третий путь к нему не сможет его обойти.
  if (state.applying) await state.applying;
  if (!(await ensurePanelApplied())) {
    hint("Настройки панели не применились — сообщение не отправлено.", true);
    return false;
  }
  // Текст забираем из поля только теперь: до этой строки отправка могла
  // не состояться.
  if (questionText !== null) {
    const input = $("#input");
    input.value = "";
    autoGrow(input);
  }

  // Обмен затевает сам читатель: ленту к низу, чтобы увидеть, что вышло.
  state.stick = true;
  if (questionText !== null) {
    if (feed.querySelector(".empty")) feed.innerHTML = "";
    feed.appendChild(userBubble(questionText));
  } else {
    // Перегенерация заменяет последний ответ: карточку убираем с экрана,
    // а на сервере пара снимается с истории тем же запросом.
    const cards = feed.querySelectorAll(".card");
    if (cards.length) cards[cards.length - 1].remove();
  }

  const card = el("article", "card busy");
  const bodyEl = el("div", "card-body md");
  card.append(cardHead(agent.model), bodyEl);
  feed.appendChild(card);
  scrollFeed();

  setBusy(true);
  stopChatPolling();
  const epoch = state.chatEpoch;
  hint("");

  const controller = new AbortController();
  state.abort = controller;
  let answer = "";
  let reasoning = "";
  let thinking = null;
  let failure = null;
  let failureDiagnostics = null;
  let status = null;
  let prompt = null;
  let toolBox = null;
  let committed = false;
  let answerIndex = null;
  let terminalQuestion = null;

  try {
    await streamPost(
      path,
      body,
      (e) => {
        switch (e.event) {
          case "retrieval":
            const retrievalStatus = {rewrite: "Переформулирование запроса", search: "Поиск контекста", rerank: "Ранжирование фрагментов"}[e.stage] || "Поиск контекста";
            if (!status) { status = cardStatus(retrievalStatus); card.insertBefore(status, bodyEl); }
            else status.querySelector(".card-status-text").textContent = retrievalStatus;
            scrollFeed();
            break;
          case "compressing":
            // Служебный вызов — отдельное обращение к модели ДО ответа:
            // пауза уже идёт, и карточка обязана сказать, из-за чего она
            // пустая. Событие приходит, только когда вызов правда будет, —
            // строке состояния верить можно.
            //
            // Вызов незнакомый (сервер новее клиента) — молчим: назвать его
            // наугад чужим именем хуже, чем не назвать вовсе.
            //
            // Строка состояния берёт текст отсюда и меняет его на каждом
            // следующем кадре: вызовов на обмене однажды было два, и пауза
            // умела продолжаться, будучи занятой уже другим.
            if (SERVICE_CALLS[e.strategy] && SERVICE_CALLS[e.strategy].status) {
              if (!status) {
                status = cardStatus(SERVICE_CALLS[e.strategy].status);
                card.insertBefore(status, bodyEl);
              } else {
                status.querySelector(".card-status-text").textContent =
                  SERVICE_CALLS[e.strategy].status;
              }
              scrollFeed();
            }
            break;
          case "start":
            // Поиск и служебные вызовы завершены: генерация началась.
            const answerStatus = e.generation === false ? "Подходящих фрагментов нет" : "Генерация";
            if (!status) { status = cardStatus(answerStatus); card.insertBefore(status, bodyEl); }
            else status.querySelector(".card-status-text").textContent = answerStatus;
            // Промпт держим у каждого обмена, а не только у того, где есть
            // врезка. У «Всей истории» он и правда повторяет ленту, зато
            // скользящее окно начало **отбрасывает** — и прочитать, что
            // именно уехало в модель, больше негде. Врезки может не быть
            // вовсе: `summary_at` придёт пустым, и подписывать в промпте
            // будет просто нечего.
            if (e.resolved_messages) {
              prompt = {
                messages: e.resolved_messages,
                // Слоты памяти, задачи, стратегии и RAG называет сервер;
                // каждый бывает пустым
                // и в одном промпте встречаются вместе.
                memoryAt: e.memory_at,
                workingAt: e.working_at,
                taskAt: e.task_at,
                summaryAt: e.summary_at,
                ragAt: e.rag_at,
                strategy: e.strategy,
              };
            }
            break;
          case "reasoning":
            reasoning += e.text;
            if (!thinking) {
              thinking = thinkingBlock("");
              thinking.open = true;
              card.insertBefore(thinking, bodyEl);
            }
            thinking.querySelector(".think-body").textContent = reasoning;
            scrollFeed();
            break;
          case "delta":
            if (status) { status.remove(); status = null; }
            answer += e.text;
            bodyEl.innerHTML = renderMarkdown(answer);
            if (e.metrics) { keepMetrics(e.metrics); renderTiles(); }
            scrollFeed();
            break;
          case "metrics":
            // Ответ на новой модели пришёл — контексту снова есть что показать.
            keepMetrics(e.metrics);
            renderTiles();
            break;
          case "tool_call":
            // Агент исполнил вызов инструмента: бейдж встаёт сразу, не
            // дожидаясь конца обмена, — пауза на сервере иначе выглядела бы
            // зависанием. После перерисовки те же бейджи встанут из метрик.
            if (!toolBox) {
              toolBox = el("div", "card-tools");
              card.appendChild(toolBox);
            }
            toolBox.appendChild(toolBadge(e));
            scrollFeed();
            break;
          case "error":
            failure = e.message;
            if (e.request_bodies?.length) failureDiagnostics = {metrics: e.metrics ?? null, request_bodies: e.request_bodies};
            // Перерисовка здесь не лишняя: метрики упавшего обмена меняют
            // показанное (пометкой «из прошлого обмена», а на частичных
            // числах — и значением), а `done` после ошибки приходит не всегда.
            if (e.metrics) { keepMetrics(e.metrics); renderTiles(); }
            break;
          case "done":
            // Записался ли обмен в историю — знает агент, и говорит прямо.
            committed = e.committed === true;
            if (!committed) {
              terminalQuestion = typeof e.question === "string" ? e.question : null;
              if (e.error) failure = failure || e.error;
              if (e.request_bodies?.length) failureDiagnostics = {metrics: e.metrics ?? null, request_bodies: e.request_bodies};
            }
            answerIndex = e.answer_index ?? null;
            if (e.text) answer = e.text;
            if (e.reasoning) reasoning = e.reasoning;
            if (e.metrics) keepMetrics(e.metrics);
            bodyEl.innerHTML = renderMarkdown(answer);
            renderTiles();
            break;
        }
      },
      controller.signal
    );
  } catch (err) {
    if (err.name !== "AbortError") failure = String(err.message || err);
  }

  if (status) status.remove();
  card.classList.remove("busy");
  state.abort = null;
  setBusy(false);

  if (failure) {
    card.classList.add("failed");
    card.appendChild(el("div", "card-error", failure));
    hint(failure, true);
    if (!answer && questionText !== null) {
      // Обмена не было: агент вопрос не запомнил, и в ленте его быть не должно.
      // Текст возвращается в поле ввода, чтобы можно было повторить.
      const bubbles = feed.querySelectorAll(".msg-user");
      if (bubbles.length) bubbles[bubbles.length - 1].remove();
      card.remove();
      const input = $("#input");
      if (!input.value) { input.value = questionText; autoGrow(input); }
    }
  }

  if (!committed && terminalQuestion !== null && !$("#input").value) {
    $("#input").value = terminalQuestion; autoGrow($("#input"));
  }

  // Лента и список слева перерисовываются по серверу: на экране должно быть
  // ровно то, что у агента в истории, а не то, что мы дорисовали по дороге.
  //
  // Промпт привязывается к ответу, только если обмен **доехал до истории**.
  // Упавший обмен её не удлиняет, а событие `start` у него уже уехало — со
  // своим промптом и своей сводкой. Привяжи его по длине истории, и он лёг бы
  // под ключ **прошлого** ответа: кнопка под давней карточкой показала бы
  // чужой запрос, внутри которого лежит сам этот ответ.
  await refreshCurrent(committed ? prompt : null, answerIndex, agent.id, epoch);
  if (!committed && failureDiagnostics && epoch === state.chatEpoch && state.current?.id === agent.id) {
    const diagnostics = el("details", "failed-request-info");
    diagnostics.append(el("summary", "", "Информация о неудачном запросе"));
    const usage = usageLine({metrics: failureDiagnostics.metrics});
    if (usage) diagnostics.append(usage);
    if (failureDiagnostics.metrics?.cost_usd == null) diagnostics.append(el("p", "", "Стоимость вызова неизвестна"));
    for (const [i, payload] of failureDiagnostics.request_bodies.entries()) {
      diagnostics.append(el("div", "", `JSON запроса · ${i + 1}`), el("pre", "prompt-json", JSON.stringify(payload, null, 2)));
    }
    $("#composer-hint").append(diagnostics);
  }
  scheduleChatPoll();
}

async function refreshCurrent(prompt, answerIndex = null, id = state.current?.id, epoch = state.chatEpoch) {
  if (!state.current) return;
  try {
    const fresh = await api("/api/agents/" + id);
    if (epoch !== state.chatEpoch || state.current?.id !== id) return;
    state.current = fresh;
    // Промпт привязываем к реплике до перерисовки: номер ответа в истории
    // известен только теперь, а рисовать карточку с кнопкой уже пора. Сюда он
    // доезжает, только если обмен записался, — значит последняя реплика
    // истории и есть его ответ.
    if (prompt) {
      state.prompts.set(promptKey(fresh.id, answerIndex ?? fresh.transcript.length - 1), prompt);
    }
    const listed = state.agents.find((a) => a.id === fresh.id);
    if (listed) { listed.history_len = fresh.history_len; listed.label = fresh.label; }
    renderList();
    renderTaskHead();
    renderFeed(fresh);
    // Итог по чату и число сообщений приехали вместе с агентом: плитки
    // перерисовываем, иначе панель отстаёт на один обмен.
    renderTiles();
    // Панель намеренно не перерисовываем: пользователь мог печатать в ней
    // прямо сейчас, и затирать его текст ответом сервера нельзя.
  } catch (e) { /* чат исчез — список обновится при следующем открытии */ }
  // Обмен меняет краткосрочный слой: история выросла, а со сворачиванием
  // могла добавиться и сводка. Это событие, а не отрисовка, и при закрытой
  // вкладке оно молчит.
  if (memoryTabOpen() && state.settingsScope === "chat") loadMemory(true);
}

// Due results arrive without a send or a Tools visit. One visible-page request
// at a time; epochs keep late GETs away from another chat or foreground stream.
function stopChatPolling() {
  state.chatEpoch += 1;
  clearTimeout(state.chatTimer);
  state.chatTimer = null;
  state.chatRequest?.abort();
  state.chatRequest = null;
}

function scheduleChatPoll() {
  clearTimeout(state.chatTimer);
  state.chatTimer = null;
  if (!state.current || document.visibilityState === "hidden") return;
  state.chatTimer = setTimeout(pollCurrentChat, 1000);
}

async function pollCurrentChat() {
  state.chatTimer = null;
  if (!state.current || document.visibilityState === "hidden") return;
  if (state.busy) { scheduleChatPoll(); return; }
  const id = state.current.id, epoch = state.chatEpoch;
  const controller = new AbortController();
  state.chatRequest = controller;
  try {
    const known = state.current.history_revision;
    const path = "/api/agents/" + id + (known ? "?known_history_revision=" + encodeURIComponent(known) : "");
    const fresh = await api(path, { signal: controller.signal });
    if (state.chatEpoch !== epoch || state.current?.id !== id || state.busy || document.visibilityState === "hidden") return;
    if (fresh.unchanged) {
      $("#chat-status").textContent = fresh.busy ? "Выполняется задача…" : "";
      return;
    }
    const changed = fresh.history_revision && state.current.history_revision
      ? fresh.history_revision !== state.current.history_revision
      : JSON.stringify(fresh.transcript) !== JSON.stringify(state.current.transcript);
    state.current = fresh;
    $("#chat-status").textContent = fresh.busy ? "Выполняется задача…" : "";
    if (changed) {
      resetMetrics(fresh);
      const listed = state.agents.find((a) => a.id === id);
      if (listed) Object.assign(listed, { history_len: fresh.history_len, label: fresh.label });
      renderList(); renderTaskHead(); renderFeed(fresh); renderTiles();
    }
  } catch (error) { /* offline/transient GET retries on the next visible tick */ }
  finally {
    if (epoch === state.chatEpoch) { state.chatRequest = null; scheduleChatPoll(); }
  }
}

// ─────────────────────── панель настроек ──────────────────────

// Страницы панели. Переключение перечисляет их поимённо: страница, забытая
// в списке, осталась бы на экране поверх открытой — и видно это только
// глазами. Список здесь один на всех.
const PANEL_TABS = ["model", "agent", "memory", "profile", "invariants", "mcp", "rag"];

const SETTINGS_PAGES = {
  model: ["Модель", "Параметры ответа и генерации", "Для текущего чата", "bot"],
  agent: ["Агент", "Поведение и контекст помощника", "Для текущего чата", "user"],
  memory: ["Память", "Реплики, записи о задаче и сведения надолго", "Рабочая — этот чат · долговременная — все чаты", "memory"],
  profile: ["Профиль", "Как ассистент отвечает вам", "Для всех чатов", "user"],
  invariants: ["Инварианты", "Правила, которые задаёт человек", "Для всех чатов", "shield"],
  rag: ["Работа RAG", "Документы, чанки и фактическая индексация", "Для всего приложения", "memory"],
  mcp: ["Инструменты", "Серверы MCP и доступные инструменты", "Для всего приложения", "tools"],
};

function renderWorkspaceHead() {
  const title = state.workspace === "settings" && state.settingsScope === "app"
    ? "Настройки приложения" : state.current ? state.current.label : "AI Challenge";
  $("#chat-title").textContent = title;
  $("#chat-title").title = title;
  document.title = title + " — AI Challenge";
}

const SCOPE_TABS = {chat: ["model", "agent", "memory", "mcp", "rag"], app: ["model", "memory", "profile", "invariants", "mcp", "rag"], history: ["rag"]};
function allowedSettingsTabs(scope) {
  return SCOPE_TABS[scope]?.filter(name => scope !== "chat" || name !== "mcp" || state.remindersAvailable) || [];
}
function refreshSettingsAvailability() {
  if (state.workspace !== "settings" || state.settingsScope !== "chat") return;
  if (state.section === "mcp" && !state.remindersAvailable) {
    const focused = $("#tab-mcp").contains(document.activeElement) || document.activeElement === $("#tab-btn-mcp");
    showSettings("model");
    if (focused) $("#tab-btn-model").focus();
    return;
  }
  $("#tab-btn-mcp").hidden = !state.remindersAvailable;
}
function syncChatControls() {
  const chat = state.settingsScope === "chat", noChat = !state.current;
  $("#settings-no-chat").classList.toggle("hidden", !chat || !noChat);
  $("#save-status").classList.toggle("hidden", !chat || noChat || !["model", "agent", "rag"].includes(state.section));
  $("#model-chat-settings").hidden = !chat;
  $("#model-app-settings").hidden = state.settingsScope !== "app";
  $("#mem-short-layer").hidden = !chat;
  $("#mem-working-layer").hidden = !chat;
  $("#mem-long-layer").hidden = state.settingsScope !== "app";
  $("#mcp-application-controls").hidden = state.settingsScope !== "app";
  $("#mcp-chat-help").hidden = !chat;
  $("#mcp-heading").hidden = chat;
  ["model", "agent"].forEach(name => $("#tab-" + name).querySelectorAll(".control").forEach(field => {
    if (field.id !== "compatible-base-url") field.disabled = !chat || noChat;
  }));
  ragInspector.syncChatControls();
}
function loadVisibleSettings() {
  if (state.workspace !== "settings" || state.settingsScope === "history") return;
  if (state.section === "memory") loadMemory();
  if (state.section === "profile") loadProfile();
  if (state.section === "invariants") loadInvariants();
  if (state.section === "mcp") loadMcp();
  if (state.section === "rag") {
    if (state.settingsScope === "app") ragInspector.open();
    else rerankModelPicker.load();
  }
  if (state.section === "model") {
    if (state.settingsScope === "app") loadModelConnection();
    else chatModelPicker.load();
  }
}
function rememberSettingsPosition() {
  if (state.workspace !== "settings") return;
  const key = state.settingsScope + ":" + state.section;
  state.sectionScroll.set(key, $("#panel-body").scrollTop);
  const active = document.activeElement;
  if (active?.id && $("#panel-body").contains(active)) state.sectionFocus.set(key, active.id);
}
function showWorkspace(which, load = true) {
  if (which === state.workspace) return;
  rememberSettingsPosition();
  if (state.workspace === "chat") state.feedScroll = $("#feed").scrollTop;
  stopMcpPolling(); ragInspector.stop(); rerankModelPicker.stop(); chatModelPicker.stop();
  state.workspace = which;
  $("#chat-workspace").classList.toggle("hidden", which !== "chat");
  $("#panel").classList.toggle("hidden", which !== "settings");
  $("#workspace-chat").hidden = which !== "settings";
  $(".metrics-strip").hidden = which !== "chat";
  renderTaskHead(); renderWorkspaceHead();
  if (which === "chat") {
    $("#feed").scrollTop = state.stick ? $("#feed").scrollHeight : state.feedScroll;
    autoGrow($("#input"));
  } else if (load) loadVisibleSettings();
}
function openApplicationSettings(which = state.scopeSections.app) {
  ++state.navigationRevision;
  showSettings(which, true, "app");
  scheduleChatPoll();
}
function showSettings(which, load = true, scope = state.settingsScope) {
  if (!allowedSettingsTabs(scope).includes(which)) {
    if (scope === "chat" && which === "mcp") which = "model"; else return;
  }
  rememberSettingsPosition();
  showWorkspace("settings", false);
  if (scope !== state.settingsScope || state.section === "mcp") stopMcpPolling();
  ragInspector.stop(); rerankModelPicker.stop(); chatModelPicker.stop();
  if (scope !== "history") ragInspector.clearSnapshot();
  state.settingsScope = scope;
  $("#panel").classList.toggle("history-view", scope === "history");
  state.section = which;
  state.scopeSections[scope] = which;
  $("#settings-domain").textContent = scope === "app" ? "Настройки приложения" : scope === "chat" ? "Настройки чата" : "Сохранённый ответ";
  $(".settings-nav").hidden = scope === "history";
  document.querySelectorAll(".tab").forEach(tab => {
    tab.hidden = !allowedSettingsTabs(scope).includes(tab.dataset.tab);
    const selected = tab.dataset.tab === which;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const label = tab.dataset.tab === "mcp" && scope === "chat" ? "Напоминания" : tab.dataset.tab === "model" && scope === "app" ? "Модели" : tab.dataset.tab === "rag" ? (scope === "chat" ? "Поиск RAG" : "Индекс RAG") : SETTINGS_PAGES[tab.dataset.tab][0];
    tab.querySelector(".tab-label").textContent = label;
    if (tab.dataset.tab === "mcp") tab.querySelector(".tab-icon").replaceChildren(icon(scope === "chat" ? "clock" : "tools"));
  });
  PANEL_TABS.forEach(name => $("#tab-" + name).classList.toggle("hidden", name !== which));
  const [title, description] = SETTINGS_PAGES[which];
  $("#settings-title").textContent = which === "mcp" && scope === "chat" ? "Напоминания" : which === "rag" ? (scope === "chat" ? "Поиск RAG" : scope === "history" ? "Работа RAG — сохранённый ответ" : "Индекс RAG") : which === "model" && scope === "app" ? "Модели" : title;
  $("#settings-description").textContent = which === "memory" ? (scope === "app" ? "Долговременные сведения для всех чатов" : "История и рабочие записи этого чата") : which === "model" && scope === "app" ? "Подключение к совместимому серверу" : which === "rag" && scope === "chat" ? "Использование контекста в следующих ответах" : which === "mcp" && scope === "chat" ? "Состояние и отмена напоминаний этого чата" : description;
  $("#settings-scope").textContent = scope === "app" ? "Для всего приложения" : scope === "history" ? "Для сохранённого ответа" : "Для текущего чата";
  $("#settings-scope").hidden = which === "rag" || which === "model";
  $("#panel-body").scrollTop = state.sectionScroll.get(scope + ":" + which) || 0;
  syncChatControls(); renderWorkspaceHead();
  if (load) loadVisibleSettings();
  const focus = state.sectionFocus.get(scope + ":" + which);
  if (focus) $("#" + focus)?.focus({preventScroll: true});
}

function tabKeys(ev, tabs, active, select) {
  const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(ev.key)) return;
  ev.preventDefault();
  const index = tabs.indexOf(active);
  const next = ev.key === "Home" ? 0 : ev.key === "End" ? tabs.length - 1
    : (index + (["ArrowLeft", "ArrowUp"].includes(ev.key) ? -1 : 1) + tabs.length) % tabs.length;
  select(tabs[next]);
  tabs[next].focus();
}

// Управление контекстом — не параметры сэмплирования: в тело запроса они не
// уезжают ни одним ключом и по `supported_parameters` модели не проверяются.
// Сжатие наше, а не провайдерское. Отдельным списком именно поэтому: попади
// они в PROVIDER_PARAMS, панель ругалась бы, что модель их не заявляет.
//
// Здесь только числовая половина: третье поле контекста — стратегия — не
// число, а выбор из списка, и читается оно как `response_format`, своей
// строкой.
const CONTEXT_NUMBERS = ["keep_last", "compress_every"];

// Какие числа контекста работают при каждой стратегии — и как они при ней
// называются. Одно и то же поле подписано по-разному не для разнообразия:
// под окном `keep_last` — это буквально размер окна, дальше которого не
// уезжает ничего; под суммаризацией — хвост, который остаётся дословным,
// пока начало сворачивается. «Сжимать каждые N» описывает установившийся
// режим точно: период между сворачиваниями равен ровно N сообщениям (позднее
// только первое — оно наступает на `keep_last + N`).
//
// Неприменимого поля на экране нет вовсе: принимать число и молча его
// игнорировать — та же неправда, с которой борется названная обрезка.
const CONTEXT_LABELS = {
  full: {},
  window: { keep_last: "Размер окна, сообщений" },
  summary: {
    keep_last: "Хранить последних, сообщений",
    compress_every: "Сжимать каждые, сообщений",
  },
};

// Все числовые поля панели: и те, что уезжают в модель, и те, что про память.
const PANEL_NUMBERS = [...NUMBER_FIELDS, ...CONTEXT_NUMBERS];

// Пометка ветки в панели: та же правда, что в списке, плюс то, чего в узкую
// строку списка не влезло, — ветка самостоятельна. Знать это важно прежде,
// чем продолжать в ней разговор: правки здесь родителя не задевают, и
// наоборот.
function fillBranch(agent) {
  const box = $("#branch-note");
  const note = branchNote(agent);
  box.textContent = note
    ? "Это " +
      note +
      ". Дальше чат сам по себе: продолжение здесь родителя не меняет, " +
      "а удаление родителя эту ветку не удалит."
    : "";
  box.classList.toggle("hidden", !note);
}

function fillPanel(agent) {
  fillBranch(agent);
  PANEL_NUMBERS.forEach((name) => {
    const el = $("#f-" + name);
    el.value = agent[name] === null || agent[name] === undefined ? "" : String(agent[name]);
  });
  fillStrategy(agent.strategy);
  $("#f-rag_enabled").checked = agent.rag_enabled === true;
  for (const name of ["rag_rewrite_enabled", "rag_rerank_enabled"]) $("#f-" + name).checked = agent[name] === true;
  for (const [name, fallback] of Object.entries({rag_candidates_k: agent.rag_top_k != null ? Math.max(20, agent.rag_top_k) : Math.max(agent.rag_candidates_k ?? 20, agent.rag_final_k ?? 5), rag_final_k: agent.rag_top_k ?? agent.rag_final_k ?? 5})) {
    $("#f-" + name).value = String(agent[name] ?? fallback);
  }
  $("#f-system").value = agent.system || "";
  // Стоп-строки — по одной в строке: список строк, а не JSON руками.
  $("#f-stop").value = (agent.stop || []).join("\n");
  fillResponseFormat(agent.response_format);
  // Модель ставим сразу, не дожидаясь каталога: панель — источник правды,
  // и её пустоту нельзя пролить в агента.
  chatModelPicker.set({provider: agent.provider, model: agent.model});
  rerankModelPicker.set({provider: agent.rag_rerank_provider, model: agent.rag_rerank_model || "openai/gpt-6-luna"});
  syncRagFields();
  state.baseModel = agent.model;
  chatModelPicker.load().then(renderWarnings);
  saveStatus("");
}

// Стратегия — выбор из закрытого списка, и пустого значения у него нет.
// Значение, которого в списке не оказалось (чат записан сервером другой
// версии), select снимает с выбора совсем — тогда показываем `full`: сервер
// такой конфиг читает так же, и панель обязана показывать то, что на самом
// деле произойдёт с историей, а не пустое поле.
function fillStrategy(value) {
  const select = $("#f-strategy");
  select.value = value || "full";
  if (!select.value) select.value = "full";
  syncStrategyFields();
}

// Показ полей контекста по выбранной стратегии. Это именно показ, а не правка
// конфига: спрятанное поле сохраняет своё значение и уезжает в агента как
// уезжало — ручки принимают оба числа при любой стратегии, просто не всякая
// их читает. Очисти мы поле при скрытии — переключение туда-обратно теряло бы
// набранное, и тогда «посмотреть другую стратегию» стоило бы пользователю
// его настроек.
function syncStrategyFields() {
  const labels = CONTEXT_LABELS[$("#f-strategy").value] || CONTEXT_LABELS.full;
  CONTEXT_NUMBERS.forEach((name) => {
    const label = labels[name];
    $("#field-" + name).classList.toggle("hidden", !label);
    // Подпись ставим только видимому: прятать поле с чужим текстом внутри
    // незачем, а мелькнуть он успел бы.
    if (label) $("#label-" + name).textContent = label;
  });
}

function syncRagFields() {
  const enabled = $("#f-rag_enabled").checked;
  $("#rag-chat-settings").classList.toggle("hidden", !enabled);
  $("#rag-chat-parameters").classList.toggle("hidden", !enabled);
  const rerank = enabled && $("#f-rag_rerank_enabled").checked;
  $("#rag-rerank-fields").classList.toggle("hidden", !rerank);
  if (rerank) rerankModelPicker.load(); else rerankModelPicker.stop();
}

function fillResponseFormat(value) {
  const kind = $("#f-response_format_kind");
  const custom = $("#f-response_format");
  if (!value) {
    kind.value = "";
    custom.value = "";
  } else if (JSON.stringify(value) === JSON.stringify({ type: "json_object" })) {
    kind.value = "json_object";
    custom.value = "";
  } else {
    kind.value = "custom";
    custom.value = JSON.stringify(value, null, 2);
  }
  syncResponseFormat();
}

function syncResponseFormat() {
  $("#response-format-custom").classList.toggle(
    "hidden",
    $("#f-response_format_kind").value !== "custom"
  );
}

async function loadModelConnection() {
  const input = $("#compatible-base-url"), status = $("#model-settings-status");
  try {
    const saved = await modelSelectors.connection();
    if (!input.dataset.dirty) { input.value = saved.compatible_base_url; status.textContent = ""; }
  } catch (error) { status.textContent = error.message; }
}

// Пустое поле значит «не отправлять параметр»: сервер получает null.
function readNumber(name) {
  const raw = ($("#f-" + name).value || "").trim();
  if (!raw) return null;
  const value = Number(raw.replace(",", "."));
  if (!Number.isFinite(value)) throw new Error(name + ": нужно число или пусто");
  return value;
}

// Строка состояния гаснет сама: «Применено» — сообщение о событии, а не
// постоянная подпись. Ошибка не гаснет: её надо прочитать и исправить.
function saveStatus(text, isError) {
  const el = $("#save-status");
  el.className = "save-status" + (isError ? " error" : "")
    + (!["model", "agent", "rag"].includes(state.section) ? " hidden" : "");
  el.textContent = text || "";
  ragInspector.syncChatControls();
  if (state.statusTimer) clearTimeout(state.statusTimer);
  state.statusTimer = null;
  if (!text || isError) return;
  state.statusTimer = setTimeout(() => {
    if (el.textContent === text) el.textContent = "";
    state.statusTimer = null;
  }, STATUS_FADE_MS);
}

// Что сейчас набрано в панели. Бросает, если поле не разобрать.
function readPanel() {
  const patch = {
    system: $("#f-system").value,
    provider: $("#f-provider").value,
    model: $("#f-model").value.trim(),
    stop: readStopLines($("#f-stop").value),
    response_format: parseResponseFormat(
      $("#f-response_format_kind").value,
      $("#f-response_format").value
    ),
    strategy: $("#f-strategy").value,
    rag_enabled: $("#f-rag_enabled").checked,
    rag_rewrite_enabled: $("#f-rag_rewrite_enabled").checked,
    rag_rerank_enabled: $("#f-rag_rerank_enabled").checked,
    rag_rerank_provider: $("#f-rag_rerank_provider").value,
    rag_rerank_model: $("#f-rag_rerank_model").value.trim(),
  };
  PANEL_NUMBERS.forEach((name) => { patch[name] = readNumber(name); });
  for (const [name, min, max, integer] of [["rag_candidates_k", 1, 100, true], ["rag_final_k", 1, 100, true]]) {
    const value = readNumber(name);
    if (value === null || value < min || value > max || (integer && !Number.isInteger(value))) throw new Error(name + ": " + (integer ? "целое число" : "число") + " от " + min + " до " + max);
    patch[name] = value;
  }
  if (!patch.model) throw new Error("Выберите модель ответа или введите её ID");
  if (patch.rag_final_k > patch.rag_candidates_k) throw new Error("Чанков в ответе не может быть больше кандидатов для поиска");
  if (patch.rag_rerank_enabled && !patch.rag_rerank_model) throw new Error("Выберите модель ранжирования или введите её ID");
  return patch;
}

// Предупреждение пересчитывается на каждое изменение панели и на смену
// модели — по тому, что набрано прямо сейчас, а не по сохранённому.
function renderWarnings() {
  const box = $("#model-warn");
  const tab = $("#tab-btn-model");
  let warnings = [];
  try {
    const settings = readPanel();
    warnings = paramWarnings(
      state.models.find((m) => m.id === settings.model),
      settings,
      state.current && state.current.extra_body,
      state.baseModel
    );
  } catch (e) {
    warnings = [];   // поле не разобрать — про это скажет строка состояния
  }
  box.innerHTML = "";
  warnings.forEach((text) => box.appendChild(el("p", "", text)));
  box.classList.toggle("hidden", !warnings.length);
  // Открыта вкладка «Агент» — про предупреждение всё равно должно быть видно.
  tab.classList.toggle("has-warn", warnings.length > 0);
}

// Инвариант чата: **сообщение уходит только тогда, когда конфиг агента
// равен тому, что показывает панель**. Держать его на событии `change`
// нельзя: у события ровно один шанс выстрелить, а поводов его упустить
// сколько угодно — значение поставили из кода, поле не потеряло фокус.
// Поэтому событие оставлено ради отзывчивости, а истина проверяется прямо
// перед отправкой: показать на экране одно, а послать другое хуже, чем
// не послать вовсе.
async function ensurePanelApplied() {
  if (!state.current) return true;
  // Неразобранное поле помечает панель грязной внутри applySettings — отсюда
  // это видно по `panelDirty`, и отправка не состоится.
  await applySettings();
  return !state.panelDirty;
}

function applySettings() {
  if (!state.current) return Promise.resolve();
  const revision = ++state.settingsRevision;
  let patch;
  try {
    patch = readPanel();
  } catch (err) {
    // Поле не разобрать — правка не доехала, и сообщение с ней уйти не должно.
    state.panelDirty = true;
    saveStatus(String(err.message || err), true);
    renderWarnings();
    return Promise.resolve();
  }
  renderWarnings();

  const id = state.current.id;
  // Слепок до правки: пролив идёт перед каждой отправкой, и без сравнения
  // «Применено» появлялось бы на каждое сообщение.
  const before = { ...state.current };
  const fields = Object.keys(patch);
  saveStatus("Сохраняю…");
  state.applying = (async () => {
    try {
      const updated = await api("/api/agents/" + id, json("PATCH", patch));
      if (state.current && state.current.id === id) {
        state.current = { ...state.current, ...updated };
      }
      const listed = state.agents.find((a) => a.id === id);
      if (listed) Object.assign(listed, updated);
      if (revision !== state.settingsRevision || !state.current || state.current.id !== id) return;
      state.panelDirty = false;
      // Модель сменили — плитка контекста гаснет сразу, а не после следующего
      // ответа: окно у новой модели другое. Правка температуры её не трогает.
      if (updated.model !== before.model || (updated.provider || "openrouter") !== (before.provider || "openrouter")) state.contextStale = true;
      renderTiles();
      // Сравнивается не панель с панелью, а конфиг агента до и после:
      // сервер по дороге нормализует (пустой список стоп-строк становится
      // `null`), и панель, разошедшаяся с агентом только формой записи,
      // изменением не является. Сравниваются только поля панели: стенограмма
      // и занятость живут своей жизнью, и по ним «изменилось» было бы
      // правдой всегда.
      if (fields.some((name) => !sameValue(before[name], updated[name]))) {
        saveStatus("Применено — со следующего сообщения.");
      } else saveStatus("");
    } catch (err) {
      if (revision !== state.settingsRevision || !state.current || state.current.id !== id) return;
      // Правка не доехала. Забыть про неё нельзя: в панели у пользователя
      // одно, у агента другое, а `change` уже отработал и сам не повторится.
      state.panelDirty = true;
      saveStatus(String(err.message || err), true);
    }
  })();
  return state.applying;
}


// ─────────────────────────── плитки ───────────────────────────

// Метрики снизу — про весь диалог, кроме доли окна: сколько всего
// ушло в модель, сколько она вернула, во что это обошлось и сколько было
// сообщений. Числа одного обмена написаны под ним самим в ленте, и подписей
// «накопленное» здесь больше нет — в панели теперь всё и так про разговор.
//
// Метрик шесть; в компактном окне общая строка прокручивается горизонтально.
const TILES = [
  ["Входные токены", () => fmt.tokens(totalField("prompt_tokens"))],
  ["Выходные токены", () => fmt.tokens(totalField("completion_tokens"))],
  ["Всего токенов", () => fmt.tokens(totalField("total_tokens"))],
  ["Стоимость", () => fmt.cost(totalField("cost_usd")), true],
  // Сообщение — одна реплика: и вопрос, и ответ. Считает их сервер, полем
  // `history_len`: тем же, что знает список слева, и тем же, что считает SQL
  // у чата, которого нет в памяти. Внутрь сумм число не убрано намеренно —
  // у чата с молчащим usage сумм нет вовсе, а сообщения в нём были. Сжатие
  // счётчик не растит: сводка живёт вне истории.
  ["Сообщений", () => fmt.tokens(state.current ? state.current.history_len : null)],
  ["Контекст", () => fmt.pct(contextFill()), false, contextIsPast],
];

function renderTiles() {
  const box = $("#tiles");
  box.innerHTML = "";
  TILES.forEach(([label, value, small, past]) => {
    // Значению отдана вся ширина плитки: подписей рядом больше нет, и цене
    // в девять знаков ничего не мешает быть видной целиком.
    const faded = Boolean(past && past());
    const v = el("div", "tile-v" + (small ? " small" : "") + (faded ? " past" : ""), value());
    // Подсказка вместо второй строки: плитка от неё не растёт, а откуда
    // взялось число, сказано словами.
    if (faded) v.title = "число прошлого обмена: последний вызов упал";
    const tile = el("div", "tile");
    tile.append(el("div", "tile-k", label), v);
    box.appendChild(tile);
  });
}

// Desktop-список скрывается целиком; скрытые кнопки не получают фокус.
const SIDEBAR_KEY = "ui.sidebar";

function applyCollapsed(collapsed) {
  $("#app").classList.toggle("no-sidebar", collapsed);
  $("#sidebar").classList.toggle("hidden", collapsed);
  $("#restore-sidebar").setAttribute("aria-expanded", String(!collapsed));
}

function setCollapsed(collapsed) {
  try { localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0"); } catch (e) { /* приватный режим */ }
  applyCollapsed(collapsed);
  $(collapsed ? "#restore-sidebar" : "#sidebar-toggle").focus();
}

function restoreSidebar() {
  let collapsed = false;
  try { collapsed = localStorage.getItem(SIDEBAR_KEY) === "1"; } catch (e) { /* приватный режим */ }
  applyCollapsed(collapsed);
}

// ─────────────────── новый чат и подтверждения ────────────────

function confirmBox(title, text, confirmLabel, onYes) {
  const wrap = el("div", "confirm");
  const box = el("div", "confirm-box");
  const row = el("div", "confirm-row");
  const previousFocus = document.activeElement;
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-labelledby", "confirm-title");

  const close = () => {
    document.removeEventListener("keydown", onKey);
    wrap.remove();
    if (previousFocus && document.body.contains(previousFocus)) previousFocus.focus();
  };
  // Escape закрывает диалог всегда, а не только на узком окне: выйти
  // из подтверждения необратимого действия надо уметь не глядя.
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
    if (ev.key === "Tab") {
      ev.preventDefault();
      (document.activeElement === no ? yes : no).focus();
    }
  };
  document.addEventListener("keydown", onKey);

  const no = el("button", "", "Отмена");
  no.type = "button";
  no.onclick = close;
  const yes = el("button", "primary", confirmLabel);
  yes.type = "button";
  yes.onclick = () => { close(); onYes(); };
  row.append(no, yes);
  const heading = el("h3", "", title);
  heading.id = "confirm-title";
  box.append(heading, el("p", "", text), row);
  wrap.appendChild(box);
  wrap.onclick = (ev) => { if (ev.target === wrap) close(); };
  document.body.appendChild(wrap);
  no.focus();
}

async function newChat() {
  const ticket = ++state.navigationRevision;
  state.creationRevision += 1;
  stopStream();
  try {
    const created = await api("/api/agents", json("POST", {}));
    state.chatCreationError = "";
    if (ticket === state.navigationRevision) state.current = null;
    await loadAgents(created.agents[0].id, ticket);
    if (ticket !== state.navigationRevision) return;
    showWorkspace("chat"); $("#input").focus();
  } catch (err) {
    if (ticket !== state.navigationRevision) return;
    state.chatCreationError = String(err.message || err);
    renderList(); hint(state.chatCreationError, true);
  }
}

// ─────────────────────────── старт ────────────────────────────

function init() {
  $("#sidebar-toggle").appendChild(icon("panelLeft"));
  $("#restore-sidebar").appendChild(icon("panelLeft"));
  $("#sidebar-toggle").onclick = () => setCollapsed(true);
  $("#restore-sidebar").onclick = () => setCollapsed(false);
  $("#app-settings").appendChild(icon("wrench"));
  $("#app-settings").onclick = () => openApplicationSettings();
  $("#workspace-chat").onclick = () => { ++state.navigationRevision; showWorkspace("chat"); scheduleChatPoll(); };

  restoreSidebar();
  document.addEventListener("visibilitychange", () => {
    stopMcpPolling();
    if (toolsVisible()) loadMcp();
    else if (state.workspace === "settings" && state.settingsScope === "chat") loadMcp(true);
    stopChatPolling();
    if (document.visibilityState !== "hidden") pollCurrentChat();
  });
  window.addEventListener?.("pagehide", stopMcpPolling);
  window.addEventListener?.("pagehide", stopChatPolling);

  $("#new-chat").onclick = () => newChat();
  $("#compatible-base-url").oninput = () => { $("#compatible-base-url").dataset.dirty = "true"; $("#model-settings-status").textContent = "Изменения не сохранены"; };
  let connectionSave = 0;
  $("#compatible-base-url").onchange = async () => {
    const input = $("#compatible-base-url"), status = $("#model-settings-status"), draft = input.value, ticket = ++connectionSave;
    try {
      const saved = await modelSelectors.saveConnection(draft);
      if (ticket !== connectionSave || input.value !== draft) return;
      input.value = saved.compatible_base_url; input.dataset.dirty = ""; status.textContent = "URL сохранён";
    } catch (error) { if (ticket === connectionSave && input.value === draft) status.textContent = error.message; }
  };
  loadModelConnection();

  // Настройки применяются по change: у полей ввода это потеря фокуса,
  // у списков — выбор. Отдельной кнопки сохранения нет.
  $("#panel-body").addEventListener("change", (ev) => {
    // Пролив панели — про конфиг чата, и поля его носят приставку `f-`.
    // Поля вкладок «Память» и «Профиль» её не носят: у обоих свои ручки,
    // и PATCH чата при выборе типа записи или правке стиля был бы запросом
    // ни о чём. Профиль при этом сохраняется тем же событием `change`,
    // что и настройки: у поля ввода это потеря фокуса, отдельной кнопки
    // сохранения нет нигде в панели.
    const id = String(ev.target.id || "");
    if (id.startsWith("profile-")) return saveProfile(id.slice("profile-".length));
    if (!id.startsWith("f-") || state.settingsScope !== "chat") return;
    if (id === "f-response_format_kind") syncResponseFormat();
    // Показ полей меняется на самом выборе, а не после сохранения: пролив
    // конфига ходит на сервер, и ждать ответа, чтобы убрать с экрана поле,
    // которое уже ни на что не влияет, — значит снова обещать не то.
    if (id === "f-strategy") syncStrategyFields();
    if (id === "f-provider") setBusy(state.busy);
    if (id === "f-rag_enabled" || id === "f-rag_rewrite_enabled" || id === "f-rag_rerank_enabled") syncRagFields();
    applySettings();
  });
  $("#panel-body").addEventListener("focusin", ev => {
    if (ev.target.id) state.sectionFocus.set(state.settingsScope + ":" + state.section, ev.target.id);
  });
  $("#panel-body").addEventListener("input", (ev) => {
    if (state.settingsScope === "chat" && String(ev.target.id || "").startsWith("f-")) {
      state.settingsRevision += 1;
      state.panelDirty = true;
      saveStatus("Изменения не сохранены");
    }
    const name = String(ev.target.id || "").slice("profile-".length);
    if (PROFILE_FIELDS.includes(name)) state.profileDirty.add(name);
  });

  const tabs = Array.from(document.querySelectorAll(".tab"));
  tabs.forEach((tab) => {
    tab.querySelector(".tab-icon").appendChild(icon(SETTINGS_PAGES[tab.dataset.tab][3]));
    tab.onclick = () => showSettings(tab.dataset.tab);
    tab.onkeydown = (ev) => tabKeys(ev, tabs.filter(item => !item.hidden), tab, (next) => showSettings(next.dataset.tab));
  });

  $("#feed").addEventListener("scroll", () => {
    if (state.workspace === "chat") state.stick = atBottom($("#feed"));
  });

  const input = $("#input");
  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#composer").addEventListener("submit", (ev) => { ev.preventDefault(); send(); });

  fillKinds($("#mem-kind"), MEMORY_KINDS);
  fillKinds($("#mem-work-kind"), WORKING_KINDS);
  fillKinds($("#inv-kind"), INVARIANT_KINDS, "— выберите вид —");
  $("#mem-add").onclick = () => addFromForm();
  $("#mem-work-add").onclick = () => addWorkingFromForm();
  $("#inv-add").onclick = () => addInvariantFromForm();

  setBusy(false);
  renderTiles();
  renderTaskHead();
  renderMemory();
  syncChatControls();
  loadAgents().catch((err) => hint(String(err.message || err), true));
}

// В браузере файл просто запускается, под node его подключают проверки.
if (typeof module === "undefined") {
  init();
} else {
  module.exports = {
    init,
    state,
    renderMarkdown,
    readStopLines,
    parseCommand,
    parseResponseFormat,
    paramWarnings,
    fmt,
  };
}
