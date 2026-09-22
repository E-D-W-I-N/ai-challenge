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
  abort: null,         // AbortController активного потока
  lastMetrics: null,   // метрики последнего ответа — из них плитка «Контекст»
  applying: null,      // незавершённое применение настроек панели
  planMoving: false,   // переход по кнопке шапки уже летит
  panelDirty: false,   // правка панели не доехала до агента
  stick: true,         // лента примотана к низу — доматывать новые ответы
  baseModel: "",       // модель, с которой чат открыли: с ней сверяем смену
  contextStale: false, // модель сменили — прежняя доля окна к новой не относится
  contextPast: false,  // доля окна осталась от прошлого обмена: последний упал
  statusTimer: null,   // таймер, гасящий строку состояния
  prompts: new Map(),  // промпты обменов этой вкладки (см. promptKey)
  memory: null,        // три слоя памяти — ответ ручки, прочитанный на открытие вкладки
  memoryNote: "",      // почему слоёв не видно: читаем, чат не открыт, ручка ответила ошибкой
};

// Ключ промпта в `state.prompts`: чат и номер реплики-ответа в его истории.
// Карта живёт только до перезагрузки страницы, и это не недоделка: промпт —
// производная истории, которая и так в базе, и со следующим сворачиванием
// он устаревает. Показать собранный заново было бы нечестнее, чем ничего.
const promptKey = (agentId, index) => agentId + ":" + index;

const $ = (sel) => document.querySelector(sel);

// Сколько пикселей от низа ленты ещё считается «читатель внизу».
const STICK_SLACK = 80;

// Через сколько гаснет «Применено — со следующего сообщения».
const STATUS_FADE_MS = 5000;

// ─────────────────────────── иконки ───────────────────────────

// Нарисованы путями: CDN нам недоступен, а по картинке читается, что делает
// кнопка.
const ICONS = {
  panelLeft: "M3 3h18v18H3zM9 3v18",
  panelRight: "M3 3h18v18H3zM15 3v18",
  chat: "M21 12a8 8 0 0 1-8 8H7l-4 3v-5a8 8 0 0 1 8-11h2a8 8 0 0 1 8 8z",
  bot: "M12 3v3M6 8h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM9 13h.01M15 13h.01",
  copy: "M9 9h10v10H9zM5 15H4V4h11v1",
  refresh: "M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5",
  dots: "M12 5h.01M12 12h.01M12 19h.01",
  send: "M4 12l16-8-6 16-2.5-6.5z",
  stop: "M7 7h10v10H7z",
  sun: "M12 5V3M12 21v-2M5 12H3M21 12h-2M6.5 6.5L5 5M19 19l-1.5-1.5M6.5 17.5L5 19M19 5l-1.5 1.5M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0z",
  moon: "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z",
  clock: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  pencil: "M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  lines: "M4 6h16M4 10h16M4 14h12M4 18h7",
  branch: "M7 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM7 9v10M17 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4zM17 9v2a4 4 0 0 1-4 4H7",
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

// ─────────────────────────── формат ───────────────────────────

const fmt = {
  sec: (ms) => (ms === null || ms === undefined ? "—" : (ms / 1000).toFixed(2)),
  rate: (v) => (v ? v.toFixed(1) : "—"),
  cost: (v) => (v === null || v === undefined ? "—" : "$" + Number(v).toFixed(6)),
  pct: (v) => (v === null || v === undefined ? "—" : v.toFixed(1) + " %"),
  // Токены: тысячи разделяются, от десяти тысяч — «12.4k». Прочерк остаётся
  // прочерком: ноль — это ответ, а «неизвестно» — его отсутствие, и на экране
  // они обязаны выглядеть по-разному.
  tokens: (v) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (!isFinite(n)) return "—";
    // Порог для «M» стоит там, где округление в «k» уже дало бы «1000k»:
    // такое число читается как миллион, миллионом его и пишем.
    if (Math.abs(n) >= 999500) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (Math.abs(n) >= 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  },
};

// Поле итога по чату. Считает его агент, клиент только показывает: здесь
// не складывается ни одного слагаемого — два места, где числа считаются,
// разъезжаются молча. Итога нет вовсе — это `null`, прочерк, а не ноль.
function totalField(name) {
  const total = state.current && state.current.usage_total;
  return total ? total[name] : null;
}

// Заполнение контекста — единственное в панели, что не про весь диалог: доля
// окна, занятая последним обменом. При смене модели сбрасывается: окно
// у новой другое. Сброс поднимает сама смена модели в панели, а не
// расхождение имён: провайдер вправе вернуть не то имя, которое просили
// (на `openrouter/auto` — всегда), и плитка гасла бы после каждого ответа.
function has(value) {
  return value !== null && value !== undefined;
}

// Метрики упавшего обмена приходят с пустыми числами, и класть такой набор
// поверх прежнего значило бы гасить плитки ровно тогда, когда числа нужнее
// всего. Поэтому набор не заменяется, а **сливается по полям**: новое число
// побеждает, пустое поле оставляет прежнее.
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
// функция — разбросай снятие по веткам потока, и нашёлся бы путь, где плитка
// осталась бы погашенной навсегда, — и снимает её **пришедшая доля окна**,
// а не сам факт вызова: иначе на экран вышла бы доля окна прошлой модели.
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

// ─────────────────────────── markdown ─────────────────────────

// Свой разбор: заголовки, списки, цитаты, код, жирный, курсив, ссылки.
// Текст модели сначала экранируется целиком, поэтому разметка из ответа
// не может стать разметкой страницы.
function escapeHtml(text) {
  return String(text)
    // Меткой inline-кода служит \u0000: пришли модель его в тексте, разбор
    // подставил бы на его место чужой кусок. Выбрасываем до всего остального.
    .replace(/\u0000/g, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Метка, которой на время разбора подменяется inline-код: внутри кода
// разметка не разбирается.
const CODE_MARK = "\u0000";

function inlineMarkdown(text) {
  let out = escapeHtml(text);
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return CODE_MARK + (codes.length - 1) + CODE_MARK;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  // Ссылка только на http(s): javascript: в href из ответа модели недопустим.
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" rel="noreferrer noopener" target="_blank">$1</a>'
  );
  out = out.replace(
    new RegExp(CODE_MARK + "(\\d+)" + CODE_MARK, "g"),
    (_, i) => "<code>" + (codes[Number(i)] || "") + "</code>"
  );
  return out;
}

function renderMarkdown(text) {
  const lines = String(text || "").split("\n");
  const html = [];
  let list = null;        // "ul" | "ol" | null
  let paragraph = [];
  let code = null;        // накопитель строк внутри ```

  const closeParagraph = () => {
    if (paragraph.length) {
      html.push("<p>" + inlineMarkdown(paragraph.join("\n")) + "</p>");
      paragraph = [];
    }
  };
  const closeList = () => {
    if (list) { html.push("</" + list + ">"); list = null; }
  };
  const openList = (kind) => {
    if (list !== kind) { closeList(); html.push("<" + kind + ">"); list = kind; }
  };
  const pushCode = () => html.push("<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>");

  for (const raw of lines) {
    if (code !== null) {
      if (/^\s*```/.test(raw)) { pushCode(); code = null; } else { code.push(raw); }
      continue;
    }
    if (/^\s*```/.test(raw)) { closeParagraph(); closeList(); code = []; continue; }

    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { closeParagraph(); closeList(); continue; }

    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      closeParagraph(); closeList();
      const level = heading[1].length;
      html.push("<h" + level + ">" + inlineMarkdown(heading[2]) + "</h" + level + ">");
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      closeParagraph(); closeList(); html.push("<hr>"); continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      closeParagraph(); closeList();
      html.push("<blockquote>" + inlineMarkdown(quote[1]) + "</blockquote>");
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      closeParagraph(); openList("ul");
      html.push("<li>" + inlineMarkdown(bullet[1]) + "</li>");
      continue;
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ordered) {
      closeParagraph(); openList("ol");
      html.push("<li>" + inlineMarkdown(ordered[1]) + "</li>");
      continue;
    }
    closeList();
    paragraph.push(line);
  }
  if (code !== null) pushCode();
  closeParagraph();
  closeList();
  return html.join("");
}

// ─────────────────────────── сеть ─────────────────────────────

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const err = new Error(await detail(res));
    // Код ответа едет вместе с текстом: отказ ручки плана (409) несёт
    // директиву, написанную **для модели**, и человеку её показывать нельзя —
    // свой человеческий текст клиент выбирает по коду, а не по словам в теле.
    err.status = res.status;
    throw err;
  }
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

async function loadAgents(selectId) {
  const data = await api("/api/agents");
  state.agents = data.agents;
  state.hasKey = data.has_key;
  // Пустой список — это пустой экран, в который нечего написать: список
  // начинается пустым, и удалить последний чат тоже можно.
  if (!state.agents.length) {
    const created = await api("/api/agents", json("POST", {}));
    state.agents = created.agents;
    return openAgent(created.agents[0].id);
  }
  renderList();
  const wanted = selectId || (state.current && state.current.id);
  const exists = state.agents.some((a) => a.id === wanted);
  await openAgent(exists ? wanted : state.agents[0].id);
}

function renderList() {
  const box = $("#agent-list");
  box.innerHTML = "";
  state.agents.forEach((agent) => box.appendChild(listItem(agent)));
}

// Пометка ветки: от кого чат отделился и сколько сообщений унёс. Одна функция
// на список слева и на панель справа — двумя они назвали бы разные числа.
//
// Имя родителя клиент находит сам, по id: копия рядом с родством разошлась
// бы с ним на первом же переименовании. Родителя могли удалить — это штатно,
// и пометка говорит именно это, а не молчит: иначе ветка выглядела бы
// обычным чатом, хотя начало разговора в ней чужое.
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
  open.onclick = () => openAgent(agent.id);

  const actions = el("div", "item-actions");
  actions.append(
    iconButton("pencil", "Переименовать",
      (ev) => { ev.stopPropagation(); startRename(row, agent); }, "mini"),
    iconButton("trash", "Удалить чат",
      (ev) => { ev.stopPropagation(); askDelete(agent); }, "mini danger")
  );
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
    if (state.current) renderFeed(state.current);
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
      try {
        await api("/api/agents/" + agent.id, { method: "DELETE" });
      } catch (err) {
        hint(String(err.message || err), true);
        return;
      }
      if (state.current && state.current.id === agent.id) state.current = null;
      // Чата нет — и промптам его обменов держаться не за что.
      [...state.prompts.keys()]
        .filter((key) => key.startsWith(agent.id + ":"))
        .forEach((key) => state.prompts.delete(key));
      await loadAgents();
    }
  );
}

// ─────────────────────── открытие агента ──────────────────────

async function openAgent(agentId) {
  if (!agentId) return;
  stopStream();
  let agent;
  try {
    agent = await api("/api/agents/" + agentId);
  } catch (err) {
    // Агента вытеснили или стёрли перезапуском — обновляем список.
    return loadAgents();
  }
  state.current = agent;
  state.panelDirty = false;
  // Чат открывают, чтобы увидеть последнее сообщение: отмотанная лента
  // прошлого чата к новому отношения не имеет.
  state.stick = true;
  // Чат открыт заново: доля окна относится к той модели, что у него сейчас,
  // а числа — только его собственные.
  resetMetrics(agent);
  renderList();
  renderFeed(agent);
  // Шапка задачи — часть открытого чата, а не ленты: у одного она есть,
  // у соседнего её нет вовсе, и оставленная от прошлого она показывала бы
  // чужой план.
  renderTask(agent);
  fillPanel(agent);
  renderTiles();

  // Открыт другой чат — первые два слоя теперь его, а не прежние. Читаем их
  // заново, но только если вкладка открыта: закрытой они не нужны.
  state.memory = null;
  // Значок новых записей — про **этот** чат, и число ему даёт сам чат, а не
  // слои. Своего вызова ему здесь не нужно: обе ветки ниже кончаются
  // отрисовкой, а она его считает.
  if (memoryTabOpen()) loadMemory();
  else renderMemory();

  const input = $("#input");
  input.value = "";
  autoGrow(input);
  setBusy(false);
  hint("");
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
  const keep = state.stick ? null : feed.scrollTop;
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
}

function userBubble(text) {
  return el("div", "msg-user", text);
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
    iconButton("copy", "Копировать ответ", () => copyText(turn.content)),
    iconButton("refresh", "Перегенерировать", () => regenerate()),
    iconButton("dots", "Показать сырой текст", () => showRaw(card, turn)),
    // Ветка отсюда — у каждого ответа и при любой стратегии: ветвление про
    // структуру разговора, а не про то, что уезжает в модель, и от выбора
    // в переключателе оно не зависит.
    iconButton("branch", "Ветка отсюда", () => forkFrom(agent, index))
  );
  // Кнопка появляется у всякого ответа, чей промпт клиент видел, — при любой
  // стратегии: у окна это единственный способ прочитать отброшенное начало.
  // Промпт берётся из этой же вкладки, не сохранился — показывать нечего.
  const prompt = state.prompts.get(promptKey(agent.id, index));
  if (prompt) {
    actions.appendChild(
      iconButton("lines", "Показать промпт запроса", () => showPrompt(card, prompt))
    );
  }
  head.appendChild(actions);
  card.appendChild(head);

  if (turn.reasoning) card.appendChild(thinkingBlock(turn.reasoning));

  const body = el("div", "card-body md");
  body.innerHTML = renderMarkdown(turn.content);
  card.appendChild(body);

  const usage = usageLine(turn);
  if (usage) card.appendChild(usage);

  if (turn.error) card.appendChild(el("div", "card-error", turn.error));
  return card;
}

// Ветка отсюда: новый чат уносит разговор по эту карточку включительно
// и открывается. `index` — номер реплики-ответа, значит унести надо
// `index + 1` сообщений: карточка входит в ветку вместе со своим вопросом.
// Пока идёт ответ, уходить из чата нельзя: история родителя дописывается
// в конце обмена, и ветка унесла бы разговор без него.
async function forkFrom(agent, index) {
  if (state.busy) return;
  let created;
  try {
    created = await api("/api/agents/" + agent.id + "/fork", json("POST", { at: index + 1 }));
  } catch (err) {
    hint(String(err.message || err), true);
    return;
  }
  state.current = null;
  await loadAgents(created.agents[0].id);
  // Открытие чата гасит подсказку, поэтому говорим после него: ветка
  // открылась молча, и без строки было бы непонятно, тот это чат или нет.
  hint("Ветка заведена: унесено " + fmt.tokens(index + 1) + " сообщений.");
}

// Что под ответом: всё про этот обмен и только про него. Итог по диалогу
// живёт в плитках справа, и одно число нигде не показывается дважды: лента
// про обмен, панель про разговор.
//
// Подписи полные — «входные токены», а не «входные»: сокращённая оставляет
// гадать, токены это или что-то другое. Пропущенное поле не пишется вовсе:
// «входные токены 0» вместо «неизвестно» было бы неправдой, а сумму, о
// которой смолчал провайдер, считает сервер — сложи её здесь, и «всего»
// разошлось бы с плиткой. У оборванного ответа строка есть: он оплачен.
function cutNote(m) {
  if (m.summarized) return " (сводка вместо " + fmt.tokens(m.summarized) + " сообщений)";
  if (m.dropped) return " (окно: отброшено " + fmt.tokens(m.dropped) + " сообщений)";
  return "";
}

// Служебные вызовы — те, что идут к модели ДО ответа и кладут в промпт свою
// врезку; остался один, сворачивание. Один индекс на оба показа — строку
// состояния и подпись врезки в просмотре промпта, — а не две таблицы:
// разошлись бы они молча. Какой вызов идёт, говорит сервер полем `strategy`.
const SERVICE_CALLS = {
  summary: { status: "Сворачиваю начало разговора…", role: "сводка начала разговора" },
  facts: { role: "факты о разговоре" },
  // Работа по плану — третьей строкой того же индекса. Служебного вызова
  // за ней нет (план правит сама модель посреди обмена), но показов у неё
  // те же два, и разъехаться им двумя таблицами незачем.
  plan: { status: "Модель работает по плану…", role: "план задачи" },
};

function usageLine(turn) {
  const m = turn.metrics;
  if (!m) return null;

  const tokens = [];
  if (has(m.prompt_tokens)) {
    // Сколько сообщений не уехало дословно — приписано к тому числу, которое
    // от этого уменьшилось. Отдельной плитки нет: плитки про весь диалог.
    // Два ключа, два разных слова: сводка начало **заменила** и его ещё можно
    // прочитать в промпте, окно его **отбросило** совсем.
    tokens.push("входные токены " + fmt.tokens(m.prompt_tokens) + cutNote(m));
  }
  if (has(m.completion_tokens)) {
    // Токены рассуждения провайдер кладёт **внутрь** completion_tokens.
    // Названы отдельным числом, а не вычтены молча: вычитание сделало бы
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
  // Оборотов больше одного — значит модель звала инструменты, и вызовов было
  // столько же. Числа рядом — сумма по всем оборотам, и без их числа обмен
  // из трёх вызовов выглядел бы одним непомерно дорогим.
  if (m.turns > 1) how.push("оборотов " + m.turns);
  if (m.provider) how.push(m.provider);

  if (!tokens.length && !how.length) return null;
  const box = el("div", "card-usage");
  if (tokens.length) box.appendChild(el("div", "usage-tokens", tokens.join(" · ")));
  if (how.length) box.appendChild(el("div", "usage-how", how.join(" · ")));
  return box;
}

// Строка состояния в карточке: что происходит, пока ответа ещё нет. Индикатор
// неопределённый: у одного вызова к модели доли выполнения нет, и полоса
// с процентами называла бы числа, которых никто не знает.
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

// Что уехало в модель этим запросом — целиком и в том же порядке.
// Переключатель, как «Показать сырой текст».
//
// Роли подписаны: без подписей теряется главное — что начало разговора уехало
// одной врезкой, а не двадцатью сообщениями. Место врезки называет сервер
// полем `summary_at`, а чем она занята — полем `strategy`. Разбирать текст
// сообщений клиент не вправе: вторая копия `Agent.build_prompt` разошлась
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
  // Блок задачи — третьей врезкой, и ветка стоит третьей по тому же правилу:
  // порядок веток повторяет порядок сообщений в промпте. Едет он при
  // включённом процессе **всегда**, даже с пустым списком шагов.
  if (index === prompt.planAt) return SERVICE_CALLS.plan.role;
  // Врезки может не быть вовсе, и слот придёт пустым. Сравнение строгое
  // именно поэтому: подставь сюда ноль «по умолчанию», и запасная подпись
  // встала бы над первым сообщением промпта там, где врезки нет.
  if (index === prompt.summaryAt) {
    const call = SERVICE_CALLS[prompt.strategy];
    return call ? call.role : "врезка вместо начала разговора";
  }
  if (msg.role === "system") return "системный промпт";
  if (msg.role === "assistant") return "ответ модели";
  return "сообщение пользователя";
}

function showPrompt(card, prompt) {
  const shown = card.querySelector(".prompt-view");
  if (shown) {
    shown.remove();
    return;
  }
  const box = el("div", "prompt-view");
  box.appendChild(el("div", "prompt-title", "Промпт запроса — что уехало в модель"));
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

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    hint("Скопировано.");
  } catch (e) {
    hint("Скопировать не вышло — браузер не дал доступ к буферу.", true);
  }
}

// Лента доматывается вниз, только если читатель и так внизу. Отмотал
// вверх — новые куски ответа не дёргают её у него под руками.
function atBottom(feed) {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight <= STICK_SLACK;
}

function scrollFeed() {
  if (!state.stick) return;
  const feed = $("#feed");
  feed.scrollTop = feed.scrollHeight;
}

// ─────────────────────────── задача ───────────────────────────

// Этапы задачи по-русски — одна карта на всё, что их называет. Сам этап
// нигде не хранится и здесь не вычисляется: сервер выводит его из списка
// шагов (`plan.stage_of`) и присылает готовым. Клиент только переводит —
// посчитай он этап заново, у одного состояния стало бы два ответа.
const STAGE_LABELS = {
  planning: "план",
  approval: "утверждение",
  execution: "работа",
  validation: "проверка",
  done: "готово",
  paused: "пауза",
};

// Значок шага — по статусу, и все три **разные**. Не цветом: он пропадает
// и в чёрно-белом, и у дальтоника, и на записи экрана. Цвет рядом есть,
// но вторым признаком, а не единственным.
const STEP_MARKS = { done: "✓", in_progress: "▶", pending: "○" };

// На паузе текущий шаг помечен иначе: значок обязан говорить то же, что
// кнопка «Продолжить». Иначе зритель записи поверит глазам, а не подписи
// в двенадцать пикселей, и решит, что пауза не сработала.
const PAUSED_MARK = "‖";

// Кнопки шапки: по одной на **переход**, а не на этап.
//
// `stages` — этапы, где кнопка уместна; `null` значит «всегда, пока процесс
// включён». Видны только уместные: кнопка, которой ручка ответит 409, —
// обещание, которого интерфейс не держит.
//
// `say` — текст-константа, которую кнопка отправляет обменом **сама**, и
// привязан он к переходу, а не к целевому этапу: и первый шаг по плану,
// и возврат из проверки ведут в `execution`, но просить надо разного.
// У «Переоткрыть» `say` нет вовсе: «переоткрыть» значит «я не согласен»,
// и почему именно — знает только человек.
//
// Готового текста отказа здесь нет: его строит `planDenial` по **свежему**
// этапу, и второй источник рядом с таблицей разошёлся бы с первым молча.
const PLAN_ACTIONS = [
  {
    action: "approve",
    label: "Утвердить план",
    stages: ["approval"],
    say: "План утверждён, выполняй первый шаг",
  },
  {
    action: "pause",
    label: "Пауза",
    stages: ["planning", "approval", "execution", "validation"],
  },
  {
    action: "resume",
    label: "Продолжить",
    stages: ["paused"],
    say: "Пауза снята, продолжай с текущего шага",
  },
  {
    action: "reopen",
    label: "Переоткрыть",
    stages: ["done"],
  },
  {
    action: "reset",
    label: "Выйти из режима задачи",
    stages: null,
    off: true,
  },
];

// Цепочка этапов: обмен кончился сменой этапа — следующий клиент шлёт сам.
//
// Сервер кончает обмен на первой же смене этапа, и отсюда главное, чего
// не было видно на живом прогоне: у каждого этапа своя карточка в ленте,
// свой промпт и своя кнопка «Показать промпт запроса». Но бросать задачу
// на первом переходе нельзя — человек попросил сделать работу.
//
// Ключ — **переход**, а не целевой этап: «шаги сделаны, проверяй» и
// «проверка нашла проблемы, исправляй» ведут в один `execution`, но просят
// разного, и одной константой на оба модель делала бы работу заново.
//
// Переходы, которые ждут человека, в таблице не стоят **вовсе**: её молчание
// и значит «дальше ход человека», а второго списка запрещённых этапов рядом
// не заводим — две половины одного правила разошлись бы молча.
const STAGE_CHAIN = {
  "execution>validation":
    "Все шаги отмечены сделанными — перечитай результат и проверь работу.",
  "validation>execution":
    "Проверка нашла проблемы — исправь их, начиная с возвращённого в работу шага.",
};

// Сколько обменов подряд клиент вправе затеять сам на одно сообщение человека.
// Первый ограничитель — цепочка встаёт, едва этап перестал меняться, — но
// на кругах `работа → проверка → работа` не срабатывает никогда, а платит
// за них человек. Четыре — это два полных круга. Выход из потолка человек
// обязан видеть словами: молча вставшая цепочка неотличима от сломанной.
const MAX_STAGE_CHAIN = 4;

// Включён ли рабочий процесс — одно место на весь клиент. Менять `workflow`
// можно ровно двумя путями: команда `/task` включает, кнопка «Выйти из режима
// задачи» гасит. Органа в панели нет намеренно — два способа на один переход
// расходятся молча.
function planOn(agent) {
  return Boolean(agent) && agent.workflow === "plan";
}

// Идёт ли задача прямо сейчас: процесс включён **и** план непуст. Команда
// `/task` смотрит сюда: затереть чужой список шагов молча нельзя.
function taskRunning(agent) {
  return planOn(agent) && (((agent.plan || {}).steps) || []).length > 0;
}

// Шапка задачи: этап, шаги и кнопки. У обычного чата её нет вовсе — не пустая
// и не свёрнутая, а её нет: обычный чат обязан остаться обычным.
function renderTask(agent) {
  const box = $("#task");
  box.innerHTML = "";
  const on = planOn(agent);
  if (!on) {
    box.className = "task hidden";
    return;
  }

  const plan = agent.plan || {};
  const steps = plan.steps || [];
  // Этап — классом на самой шапке, и оформление висит на нём. Иначе список
  // выглядит одинаково на работе и на паузе, и «Продолжить» осталась бы
  // единственным свидетельством того, что работа стоит.
  box.className = "task stage-" + (plan.stage || "unknown");

  // «Задача · работа, шаг 3 из 5». Номер называет сервер (`current`, с нуля),
  // и приписка появляется только при непустом списке: «шаг 1 из 0» было бы
  // выдумкой на ровном месте.
  let title = "Задача · " + (STAGE_LABELS[plan.stage] || plan.stage || "—");
  if (steps.length && plan.current !== null && plan.current !== undefined) {
    title += ", шаг " + (plan.current + 1) + " из " + steps.length;
  }

  const head = el("header", "task-head");
  head.appendChild(el("span", "task-stage", title));

  const actions = el("div", "task-actions");
  PLAN_ACTIONS.forEach((entry) => {
    if (entry.stages && !entry.stages.includes(plan.stage)) return;
    const btn = el("button", "task-btn" + (entry.off ? " off" : ""), entry.label);
    btn.type = "button";
    // Пока идёт ответ, переходы запрещены и на сервере: обмен читает план
    // живым. Кнопка поэтому гаснет, а не молчит в ответ на нажатие:
    // молчаливый отказ читается как поломка.
    btn.disabled = state.busy || state.planMoving;
    btn.onclick = () => planMove(entry);
    actions.appendChild(btn);
  });
  head.appendChild(actions);
  box.appendChild(head);

  if (!steps.length) {
    box.appendChild(el("p", "task-empty", "Плана ещё нет — модель составит его первым ответом."));
    return;
  }
  const list = el("ol", "task-steps");
  steps.forEach((step, index) => {
    const row = el("li", "task-step " + (step.status || "pending") +
      (index === plan.current ? " current" : ""));
    const paused = plan.stage === "paused" && index === plan.current;
    row.append(
      el("span", "task-mark",
        paused ? PAUSED_MARK : (STEP_MARKS[step.status] || STEP_MARKS.pending)),
      el("span", "task-step-title", step.title || "")
    );
    list.appendChild(row);
  });
  box.appendChild(list);
}

// Человеческий текст отказа — из **свежего** этапа, а не из таблицы действий.
// Поводов два: этап под кнопкой оказался не тот или чат занят ответом, —
// и назови причину по действию, второй получил бы фразу про этап, которого
// никто не менял. Поэтому спрашиваем уже **перечитанный** план: не подходит
// действию — называем этап, подходит — причину не выдумываем вовсе.
function planDenial(entry, plan) {
  const stage = (plan || {}).stage;
  if (entry.stages && !entry.stages.includes(stage)) {
    return "«" + entry.label + "» сейчас нельзя: задача на этапе «" +
      (STAGE_LABELS[stage] || stage) + "».";
  }
  return "«" + entry.label + "» не вышло — попробуйте ещё раз.";
}

// Свежий чат от сервера — и в открытый, и в строку списка разом: тело у них
// одно, и разъезжаться им не с чего.
function mergeAgent(id, fresh) {
  if (state.current && state.current.id === id) {
    state.current = { ...state.current, ...fresh };
  }
  const listed = state.agents.find((a) => a.id === id);
  if (listed) Object.assign(listed, fresh);
}

// Один переход по кнопке. Порядок строгий: сперва ручка плана, потом то, что
// кнопка делает сверх неё. Отправь она обмен раньше — запрос уехал бы
// с правилом прошлого этапа в системном сообщении и с прошлым блоком задачи.
async function planMove(entry) {
  // Замок «переход уже летит». Кнопки перерисовываются на каждый кадр, и
  // локальным признаком второй щелчок не удержать, а `state.busy` между
  // щелчком и ответом ручки ещё ложь. Без замка второй запрос получает 409
  // и перечитывает чат **посреди** потока: лента перерисовывается, а куски
  // капают в отцепленный узел. Образец — замок `settled` у правки памяти.
  if (state.busy || state.planMoving || !state.current) return;
  const id = state.current.id;
  state.planMoving = true;
  // Перерисовка не украшение: замок обязан быть виден, иначе кнопка молчит
  // в ответ на нажатие, а молчание читается как поломка.
  renderTask(state.current);
  try {
    return await planMoved(entry, id);
  } finally {
    state.planMoving = false;
    renderTask(state.current);
  }
}

// Сам переход. Вынесен из `planMove` затем, чтобы замок снимался в `finally`
// ровно одного места, а выходов у перехода четыре.
async function planMoved(entry, id) {
  let moved;
  try {
    moved = await api("/api/agents/" + id + "/plan/" + entry.action, { method: "POST" });
  } catch (err) {
    // Прочее (сети нет, чата нет) говорит само за себя, и переводить его
    // не во что.
    if (err.status !== 409) {
      hint(String(err.message || err), true);
      return;
    }
    // 409 — тело такого отказа написано для модели, и человеку едет наш
    // текст. Но только если открыт всё тот же чат: уйди человек в соседний,
    // пока летел POST, фраза назвала бы этап соседа.
    if (!state.current || state.current.id !== id) return;
    // Перечитываем **прежде**, чем говорить: фраза строится из свежего этапа.
    // Заодно уходит с экрана кнопка, которая только что отказала. Посреди
    // обмена не перечитываем вовсе: перерисовка унесла бы вопрос и набежавший
    // текст, а обмен кончится тем же `refreshCurrent` сам.
    if (!state.busy) await refreshCurrent(null);
    hint(planDenial(entry, state.current && state.current.plan), true);
    return;
  }
  if (!state.current || state.current.id !== id) return;
  state.current.plan = moved.plan;
  renderTask(state.current);

  // «Выйти из режима задачи»: план стёрт ручкой, процесс гасится следом.
  // Подтверждений в этом продукте нет, но происшедшее обязано быть названо —
  // исчезнувшая шапка сама по себе могла бы значить что угодно.
  if (entry.off) {
    try {
      mergeAgent(id, await api("/api/agents/" + id, json("PATCH", { workflow: "off" })));
    } catch (err) {
      hint(String(err.message || err), true);
      return;
    }
    renderTask(state.current);
    hint("Задача сброшена: чат снова обычный.");
    return;
  }

  if (entry.say) {
    await exchange("/api/agents/" + id + "/messages", { text: entry.say }, entry.say, false, id);
  }
}

// ── команда /task: единственный способ включить режим задачи ──
// Разбирается на клиенте, и на сервер уезжает обычный текст уже без команды:
// слеш в начале `text` не обязан быть волшебным на другом клиенте.
const TASK_CMD = "/task";

// Команда это или обычный текст. После имени обязан идти пробел или конец
// строки: `/taskать` — слово, а не команда. Пустая строка значит «команда
// без описания», `null` — «не команда»: первое просит подсказки, второе
// уезжает обменом как есть.
function taskCommand(text) {
  if (text === TASK_CMD) return "";
  if (text.startsWith(TASK_CMD) && /\s/.test(text.charAt(TASK_CMD.length))) {
    return text.slice(TASK_CMD.length).trim();
  }
  return null;
}

async function startTask(description) {
  const agent = state.current;
  // Пустое описание не отправляем вовсе: обмен из одной команды включил бы
  // процесс и не сказал бы модели, чем заниматься. Форма памяти ведёт себя
  // так же — пустой текст она не отпускает.
  if (!description) {
    hint("После /task напишите, что за задача — например: /task собрать ТЗ на приложение.", true);
    return;
  }
  // Чужой план молча не затираем: сбросить его можно кнопкой в шапке, и
  // сказано об этом там же, где отказали.
  if (taskRunning(agent)) {
    hint("Задача в этом чате уже идёт: нажмите «Выйти из режима задачи» в шапке или заведите новый чат.", true);
    return;
  }
  // Процесс включается **раньше** обмена и строго последовательно: уедь
  // сообщение первым, промпт собрался бы без правила этапа, без блока задачи
  // и без инструментов.
  let updated;
  try {
    updated = await api("/api/agents/" + agent.id, json("PATCH", { workflow: "plan" }));
  } catch (err) {
    hint(String(err.message || err), true);
    return;
  }
  mergeAgent(agent.id, updated);
  renderTask(state.current);
  await exchange("/api/agents/" + agent.id + "/messages",
    { text: description }, description, true, agent.id);
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

function setBusy(busy) {
  state.busy = busy;
  const send = $("#send");
  send.innerHTML = "";
  send.appendChild(icon(busy ? "stop" : "send"));
  send.classList.toggle("stop", busy);
  send.title = busy ? "Остановить" : "Отправить";
  send.disabled = !busy && !state.hasKey;
  $("#input").disabled = busy;
  // Шапка задачи гаснет и оживает вместе с полем ввода: `setBusy` —
  // единственное место, где занятость меняется, и второй такой заводить
  // незачем.
  renderTask(state.current);
  // Про ключ в интерфейсе не говорим и менять его отсюда нельзя: репозиторий
  // публичный, ключ живёт в .env и остаётся делом того, кто поднял сервер.
  if (!state.hasKey) hint("Стенд не настроен: нет .env — вызова к модели не будет.", true);
}

function stopStream() {
  if (state.abort) {
    state.abort.abort();
    state.abort = null;
  }
  setBusy(false);
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
  if (!text || !state.current || !state.hasKey) return;

  // `/task` — не сообщение, а команда: включает режим задачи и только после
  // успеха отправляет описание обменом. Разбор здесь, а не в `exchange`:
  // через него идут перегенерация и обмены кнопок, а команд среди них нет.
  const described = taskCommand(text);
  if (described !== null) return startTask(described);

  const id = state.current.id;
  await exchange("/api/agents/" + id + "/messages", { text }, text, true, id);
}

async function regenerate() {
  if (state.busy || !state.current || !state.hasKey) return;
  const id = state.current.id;
  await exchange("/api/agents/" + id + "/regenerate", null, null, false, id);
}

// Один обмен: рисуем пузырь вопроса, карточку ответа и стримим в неё.
//
// `questionText` — что показать пузырём; `null` значит «пузыря нет», и это
// перегенерация, заменяющая последний ответ.
//
// `fromInput` — **откуда** этот текст взялся, и это отдельный признак
// намеренно: у кнопок шапки пузырь рисовать надо, а поле не трогать — текст
// в него никто не набирал. Слей их в один признак, и «Утвердить план»
// стирало бы недописанный вопрос. Отсюда же и возврат текста в поле
// у упавшего обмена: вернуть можно только то, что оттуда и взяли.
//
// `agentId` — чат, **для которого** этот обмен. Не `state.current`: между
// входом сюда и первой отрисовкой лежат два ожидания, а у `/task` ещё и
// PATCH; `state.busy` в это время ещё ложь, и человек вправе открыть другой
// чат. Уедь обмен как ни в чём не бывало — пузырь и поток встали бы в чужую
// ленту, а запись ушла бы в покинутый чат.
//
// `chain` — который это обмен подряд из затеянных **самим клиентом**.
// Счётчик едет параметром, а не лежит в `state`: цепочку рвёт что угодно,
// и сбрасывать его пришлось бы в каждом из этих мест поимённо, а забытое
// место молча продлило бы чужую цепочку.
async function exchange(path, body, questionText, fromInput = false, agentId = null,
                        chain = 0) {
  const feed = $("#feed");

  // Инвариант живёт здесь, а не у вызывающих: через `exchange` проходит
  // всякая отправка, и третий путь к нему не сможет его обойти.
  if (state.applying) await state.applying;
  if (!(await ensurePanelApplied())) {
    hint("Настройки панели не применились — сообщение не отправлено.", true);
    return false;
  }
  // Тот же довод, что и у ожидания выше: проверка стоит там, где через неё
  // ходят все. Место её — после ожиданий и **до** первой правки экрана:
  // поле ввода ещё не тронуто, и набранное остаётся у человека.
  if (agentId && (!state.current || state.current.id !== agentId)) {
    hint("Открыт другой чат — сообщение не отправлено.", true);
    return false;
  }
  const agent = state.current;
  // Поле чистим только теперь: до этой строки отправка могла не состояться.
  // И только если текст пришёл из него — обмен, затеянный кнопкой шапки,
  // чужого черновика не касается.
  if (fromInput) {
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
  hint("");

  const controller = new AbortController();
  state.abort = controller;
  let answer = "";
  let reasoning = "";
  let thinking = null;
  let failure = null;
  let cancelled = false;
  // Был ли исполнен хоть один вызов инструмента. Нужен ровно для отмены:
  // сказать «отметки плана остались» можно только там, где их правда
  // успели поставить.
  let toolRan = false;
  let status = null;
  let prompt = null;
  let committed = false;
  // Каким этап был и каким стал — приезжает кадром `done` **данными**:
  // сравнивать строки собственной шапки с прошлой отрисовки клиент не вправе,
  // это была бы догадка о том, что сервер знает точно.
  let stageFrom = null;
  let stageTo = null;

  try {
    await streamPost(
      path,
      body,
      (e) => {
        switch (e.event) {
          case "compressing":
            // Служебный вызов — отдельное обращение к модели ДО ответа:
            // пауза уже идёт, и карточка обязана сказать, из-за чего она
            // пустая. Событие приходит, только когда вызов правда будет.
            // Вызов незнакомый (сервер новее клиента) — молчим: назвать
            // его наугад чужим именем хуже, чем не назвать вовсе.
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
            // Промпт собран — значит служебный вызов позади и дальше пойдёт
            // ответ: строке состояния больше нечего показывать.
            if (status) { status.remove(); status = null; }
            // Промпт держим у каждого обмена, а не только у того, где есть
            // врезка: у «Всей истории» он повторяет ленту, зато окно начало
            // **отбрасывает**, и прочитать уехавшее больше негде.
            if (e.resolved_messages) {
              prompt = {
                messages: e.resolved_messages,
                // Слотов четыре, все четыре называет сервер, все бывают
                // пустыми и в одном промпте встречаются вместе: обе памяти
                // и блок задачи едут при любой обрезке.
                memoryAt: e.memory_at,
                workingAt: e.working_at,
                planAt: e.plan_at,
                summaryAt: e.summary_at,
                strategy: e.strategy,
              };
            }
            break;
          case "tool":
            // Кадр на каждый **исполненный** вызов, и шапка двигается
            // **тогда же**, а не после `done`: человек видит шаги в тот же
            // момент, что и модель. План в кадре уже новый.
            toolRan = true;
            if (e.plan && state.current && state.current.id === agent.id) {
              state.current.plan = e.plan;
              renderTask(state.current);
            }
            // Оборот продолжается, текста ещё нет — карточка обязана сказать,
            // чем занята пауза. Текст из того же индекса, что и подпись блока
            // задачи в просмотре промпта.
            if (!status) {
              status = cardStatus(SERVICE_CALLS.plan.status);
              card.insertBefore(status, bodyEl);
            } else {
              status.querySelector(".card-status-text").textContent = SERVICE_CALLS.plan.status;
            }
            scrollFeed();
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
            // Пошёл текст — обещать строке состояния больше нечего. Гасить её
            // на одном `start`, как раньше, уже мало: после вызова инструмента
            // оборот продолжается словами, а второго `start` на обмене нет.
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
          case "error":
            failure = e.message;
            // Перерисовка здесь не лишняя: метрики упавшего обмена меняют
            // показанное (пометкой «из прошлого обмена», а на частичных
            // числах — и значением), а `done` после ошибки приходит не всегда.
            if (e.metrics) { keepMetrics(e.metrics); renderTiles(); }
            break;
          case "done":
            // Записался ли обмен в историю — знает агент, и говорит прямо.
            committed = e.committed === true;
            // У «отменён» **один** признак на двух вестников: наш оборванный
            // поток и слово сервера. Свой «Стоп» рвёт поток локально, и кадра
            // `done` тогда не приходит вовсе; а отмена от соседней вкладки
            // через `/cancel` приезжает именно сюда. Не прочитай мы её —
            // цепочка отправила бы следующий обмен **сама**, уже после
            // просьбы остановиться, и счёт пришёл бы человеку.
            if (e.cancelled) cancelled = true;
            stageFrom = e.stage_from || null;
            stageTo = e.stage_to || null;
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
    if (err.name === "AbortError") cancelled = true;
    else failure = String(err.message || err);
  }

  card.classList.remove("busy");
  state.abort = null;
  setBusy(false);

  // Отмена — **отдельный исход**, а не «ничего не было»: пока идут обороты
  // инструментов, текста нет по устройству, и «Стоп» жмут именно тогда.
  // Про отметки плана сказано только там, где вызов правда исполнился:
  // исполненный задним числом не откатывается, и человек обязан знать,
  // что сдвинувшиеся у него на глазах галочки остались.
  if (cancelled) {
    const kept = toolRan
      ? " Отметки плана, которые модель успела поставить, остались."
      : "";
    if (answer) {
      // Модель успела заговорить: начатый ответ агент записывает, и он
      // приедет перерисовкой по серверу.
      hint("Остановлено: начатый ответ записан." + kept);
    } else {
      // Текста не было вовсе — обмен не записан, и в ленте ему места нет.
      // Вопрос возвращается в поле: набирать его заново незачем.
      if (questionText !== null) {
        const bubbles = feed.querySelectorAll(".msg-user");
        if (bubbles.length) bubbles[bubbles.length - 1].remove();
      }
      card.remove();
      if (fromInput) {
        const input = $("#input");
        if (!input.value) { input.value = questionText; autoGrow(input); }
      }
      hint("Остановлено: ответ не записан, вопрос вернулся в поле." + kept);
    }
  }

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
      if (fromInput) {
        const input = $("#input");
        if (!input.value) { input.value = questionText; autoGrow(input); }
      }
    }
  }

  // Лента и список перерисовываются по серверу: на экране должно быть ровно
  // то, что у агента в истории. Промпт привязывается к ответу, только если
  // обмен **доехал до истории**: упавший её не удлиняет, а его кадр `start`
  // уже уехал — привяжи по длине, и он лёг бы под ключ прошлого ответа.
  await refreshCurrent(committed ? prompt : null);

  // Цепочку рвут «Стоп» и падение: продолжать недоехавшее значило бы просить
  // модель проверить работу, которой она не делала, а счёт придёт человеку.
  // Переключение чата и закрытая вкладка рвут её сами. `cancelled` тут
  // поднимается двумя путями, и второй — отмена от соседней вкладки —
  // единственный, каким до цепочки вообще доходит отмена.
  if (cancelled || failure) return;
  await continueStage(stageFrom, stageTo, agent.id, chain);
}

// Следующий этап — следующий обмен, и затевает его клиент.
//
// Продолжаем только по **названному** переходу (`STAGE_CHAIN`): этап
// не сменился — цепочка встала, и это её главный ограничитель. Чат
// проверяется заново: между концом обмена и этой строкой лежит
// `refreshCurrent`, и человек вправе уйти в соседний чат.
async function continueStage(from, to, id, chain) {
  const say = from && to && from !== to ? STAGE_CHAIN[from + ">" + to] : null;
  if (!say) return;
  if (!state.current || state.current.id !== id || !planOn(state.current)) return;
  if (chain >= MAX_STAGE_CHAIN) {
    hint("Задача прошла " + MAX_STAGE_CHAIN + " этапа подряд сама — дальше не иду: " +
      "посмотрите, что вышло, и напишите, что делать.");
    return;
  }
  await exchange("/api/agents/" + id + "/messages", { text: say }, say, false, id, chain + 1);
}

async function refreshCurrent(prompt) {
  if (!state.current) return;
  try {
    const fresh = await api("/api/agents/" + state.current.id);
    state.current = fresh;
    // Промпт привязываем к реплике до перерисовки: номер ответа известен
    // только теперь. Сюда он доезжает, только если обмен записался, —
    // значит последняя реплика истории и есть его ответ.
    if (prompt) {
      state.prompts.set(promptKey(fresh.id, fresh.transcript.length - 1), prompt);
    }
    const listed = state.agents.find((a) => a.id === fresh.id);
    if (listed) { listed.history_len = fresh.history_len; listed.label = fresh.label; }
    renderList();
    renderFeed(fresh);
    // План приехал в том же теле, что история: обмен мог его подвинуть
    // вызовом инструмента, а завершить задачу — и вовсе сменить этап.
    renderTask(fresh);
    // Итог по чату и число сообщений приехали вместе с агентом: плитки
    // перерисовываем, иначе панель отстаёт на один обмен.
    renderTiles();
    // Панель намеренно не перерисовываем: пользователь мог печатать в ней
    // прямо сейчас, и затирать его текст ответом сервера нельзя.
  } catch (e) { /* чат исчез — список обновится при следующем открытии */ }
  // Обмен меняет краткосрочный слой: история выросла, а со сворачиванием
  // могла добавиться и сводка. Это событие, а не отрисовка, и при закрытой
  // вкладке оно молчит.
  if (memoryTabOpen()) loadMemory();
}

// ─────────────────────── панель настроек ──────────────────────

// Страницы панели. Переключение перечисляет их поимённо: страница, забытая
// в списке, осталась бы на экране поверх открытой — и видно это только
// глазами. Список здесь один на всех.
const PANEL_TABS = ["model", "agent", "memory", "profile"];

const NUMBER_FIELDS = [
  "temperature", "max_tokens", "top_p", "top_k", "min_p",
  "repetition_penalty", "presence_penalty", "frequency_penalty",
];

// Управление контекстом — не параметры сэмплирования: в тело запроса они
// не уезжают ни одним ключом, сжатие наше. Отдельным списком именно поэтому:
// попади они в PROVIDER_PARAMS, панель ругалась бы, что модель их
// не заявляет. Здесь только числовая половина — стратегия читается своей
// строкой, как `response_format`.
const CONTEXT_NUMBERS = ["keep_last", "compress_every"];

// Какие числа контекста работают при каждой стратегии — и как они при ней
// называются. Одно поле подписано по-разному не для разнообразия: под окном
// `keep_last` это размер окна, под суммаризацией — хвост, остающийся
// дословным. Неприменимого поля на экране нет вовсе: принимать число и молча
// его игнорировать — та же неправда, с которой борется названная обрезка.
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

// Пометка ветки в панели: та же правда, что в списке, плюс не влезшее
// в узкую строку — ветка самостоятельна, и правки здесь родителя
// не задевают.
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
  $("#f-system").value = agent.system || "";
  // Стоп-строки — по одной в строке: список строк, а не JSON руками.
  $("#f-stop").value = (agent.stop || []).join("\n");
  fillResponseFormat(agent.response_format);
  // Модель ставим сразу, не дожидаясь каталога: панель — источник правды,
  // и её пустоту нельзя пролить в агента.
  setModelOptions([{ id: agent.model }], agent.model);
  state.baseModel = agent.model;
  fillModels(agent.model).then(renderWarnings);
  saveStatus("");
}

// Стратегия — выбор из закрытого списка, пустого значения у него нет.
// Чужое значение (чат записан сервером другой версии) select снимает
// с выбора совсем — тогда показываем `full`: сервер такой конфиг читает
// так же, и панель обязана показывать то, что правда произойдёт.
function fillStrategy(value) {
  const select = $("#f-strategy");
  select.value = value || "full";
  if (!select.value) select.value = "full";
  syncStrategyFields();
}

// Показ полей контекста по стратегии — именно показ, а не правка конфига:
// спрятанное поле сохраняет значение и уезжает в агента как уезжало. Очисти
// мы его при скрытии, «посмотреть другую стратегию» стоило бы пользователю
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

function setModelOptions(models, current) {
  const select = $("#f-model");
  select.innerHTML = "";
  models.forEach((m) => {
    const price = m.prompt_price_per_m
      ? "  ·  $" + m.prompt_price_per_m + " / $" + m.completion_price_per_m + " за 1M"
      : "";
    const opt = el("option", "", m.id + price);
    opt.value = m.id;
    if (m.id === current) opt.selected = true;
    select.appendChild(opt);
  });
}

async function fillModels(current) {
  if (!state.models.length) {
    try {
      state.models = (await api("/api/models")).models || [];
    } catch (e) {
      state.models = [];
    }
  }
  const options = state.models.some((m) => m.id === current)
    ? state.models
    : [{ id: current }, ...state.models];
  setModelOptions(options, current);
}

// Пустое поле значит «не отправлять параметр»: сервер получает null.
function readNumber(name) {
  const raw = ($("#f-" + name).value || "").trim();
  if (!raw) return null;
  const value = Number(raw.replace(",", "."));
  if (!Number.isFinite(value)) throw new Error(name + ": нужно число или пусто");
  return value;
}

// Стоп-строки: по одной в строке, пустые не в счёт. Отдельной функцией
// без DOM — разбор проверяется без браузера.
function readStopLines(text) {
  const lines = String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  return lines.length ? lines : null;
}

// Формат ответа: частый случай выбирается из списка, редкий пишется JSON.
// Кривой JSON — понятная ошибка, а не молчаливая отправка мусора провайдеру.
function parseResponseFormat(kind, raw) {
  if (!kind) return null;
  if (kind === "json_object") return { type: "json_object" };
  const text = String(raw || "").trim();
  if (!text) return null;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    throw new Error("формат ответа: это не JSON — " + e.message);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("формат ответа: нужен объект JSON");
  }
  return parsed;
}

// Параметры панели в терминах OpenRouter. Наши поля — `system` и `model` —
// параметрами не уходят и по `supported_parameters` не проверяются.
const PROVIDER_PARAMS = [...NUMBER_FIELDS, "stop", "response_format"];

// Чем заданные параметры не сойдутся с выбранной моделью. Предупреждать надо
// **до** отправки: с provider.require_parameters=true незаявленный параметр
// выкашивает провайдеров, и вместо ответа придёт невнятная ошибка. Отдельной
// функцией без DOM — решение проверяется без браузера.
function paramWarnings(model, settings, extraBody, baseModel) {
  const warnings = [];

  // Чат, привязанный к одному поставщику, на чужой модели ответа не получит.
  // Говорим об этом ровно в тот момент, когда модель меняют: постоянная
  // надпись про настройку, которой не видно, только сбивает с толку.
  const pinned = ((extraBody || {}).provider || {}).order;
  if (Array.isArray(pinned) && pinned.length && baseModel && settings.model !== baseModel) {
    warnings.push(
      `Этот чат привязан к одному поставщику моделей — ${pinned.join(", ")}. ` +
        `Если у него нет «${settings.model}», ответа не будет: вернётся ошибка. ` +
        `Раньше здесь стояла «${baseModel}».`
    );
  }

  // Каталог не загрузился или модель в нём не нашлась — про параметры молчим:
  // пугать предупреждением, которого не на чем основать, хуже.
  if (!model) return warnings;

  const declared = model.supported_parameters || [];
  if (declared.length) {
    const missing = PROVIDER_PARAMS.filter(
      (name) => settings[name] !== null && settings[name] !== undefined && !declared.includes(name)
    );
    if (missing.length) {
      warnings.push(
        `«${model.id}» не заявляет ${missing.join(", ")}. ` +
          "Запрос уходит с provider.require_parameters, поэтому подходящего " +
          "провайдера может не найтись — вместо ответа придёт ошибка."
      );
    }
  }

  const cap = model.temperature_cap;
  if (
    model.temperature_capped &&
    settings.temperature !== null &&
    settings.temperature !== undefined &&
    cap !== null &&
    cap !== undefined &&
    settings.temperature > cap
  ) {
    warnings.push(
      `«${model.id}» обрезает temperature на ${cap.toFixed(1)}: ` +
        `на ${settings.temperature} ` +
        "запрос вернётся с ошибкой, хотя temperature эта модель и заявляет."
    );
  }
  return warnings;
}

// Строка состояния гаснет сама: «Применено» — сообщение о событии, а не
// постоянная подпись. Ошибка не гаснет: её надо прочитать и исправить.
function saveStatus(text, isError) {
  const el = $("#save-status");
  el.className = "save-status" + (isError ? " error" : "");
  el.textContent = text || "";
  if (state.statusTimer) clearTimeout(state.statusTimer);
  state.statusTimer = null;
  if (!text || isError) return;
  state.statusTimer = setTimeout(() => {
    if (el.textContent === text) el.textContent = "";
    state.statusTimer = null;
  }, STATUS_FADE_MS);
}

// Одинаковы ли два значения конфига. Не `==`: `null` — это «параметр
// не отправлять», и он не равен ни нулю, ни пустой строке, а `==` их уравнял бы.
// Не `JSON.stringify`: порядок ключей в объекте от провайдера не гарантирован.
function sameValue(a, b) {
  const empty = (v) => v === null || v === undefined;
  if (empty(a) || empty(b)) return empty(a) && empty(b);
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((item, i) => sameValue(item, b[i]));
  }
  if (typeof a === "object" || typeof b === "object") {
    if (typeof a !== "object" || typeof b !== "object") return false;
    const keys = Object.keys(a);
    if (keys.length !== Object.keys(b).length) return false;
    return keys.every((k) => Object.prototype.hasOwnProperty.call(b, k) && sameValue(a[k], b[k]));
  }
  return a === b;
}

// Что сейчас набрано в панели. Бросает, если поле не разобрать.
function readPanel() {
  const patch = {
    system: $("#f-system").value,
    model: $("#f-model").value,
    stop: readStopLines($("#f-stop").value),
    response_format: parseResponseFormat(
      $("#f-response_format_kind").value,
      $("#f-response_format").value
    ),
    strategy: $("#f-strategy").value,
    // Выключатель памяти уезжает при любой стратегии: он не про историю,
    // а про слой поверх неё, и прятать его не за чем.
  };
  PANEL_NUMBERS.forEach((name) => { patch[name] = readNumber(name); });
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

// Инвариант чата: **сообщение уходит только тогда, когда конфиг агента равен
// тому, что показывает панель**. На событии `change` его не удержать: у него
// один шанс выстрелить, а упустить его можно по-разному. Событие оставлено
// ради отзывчивости, а истина проверяется прямо перед отправкой.
async function ensurePanelApplied() {
  if (!state.current) return true;
  // Неразобранное поле помечает панель грязной внутри applySettings — отсюда
  // это видно по `panelDirty`, и отправка не состоится.
  await applySettings();
  return !state.panelDirty;
}

function applySettings() {
  if (!state.current) return Promise.resolve();
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
  state.applying = (async () => {
    try {
      const updated = await api("/api/agents/" + id, json("PATCH", patch));
      if (state.current && state.current.id === id) {
        state.current = { ...state.current, ...updated };
        // Ответ ручки — целый чат, и `plan` в нём есть всегда: правка панели
        // его не меняет, но перерисовать шапку по свежему телу дешевле, чем
        // держать в голове, какие правки её не задевают.
        renderTask(state.current);
      }
      const listed = state.agents.find((a) => a.id === id);
      if (listed) Object.assign(listed, updated);
      state.panelDirty = false;
      // Модель сменили — плитка контекста гаснет сразу, а не после следующего
      // ответа: окно у новой модели другое. Правка температуры её не трогает.
      if (updated.model !== before.model) state.contextStale = true;
      renderTiles();
      // Сравнивается конфиг агента до и после, а не панель с панелью: сервер
      // по дороге нормализует, и расхождение формой записи изменением
      // не является. И только поля панели: по стенограмме и занятости
      // «изменилось» было бы правдой всегда.
      if (fields.some((name) => !sameValue(before[name], updated[name]))) {
        saveStatus("Применено — со следующего сообщения.");
      }
    } catch (err) {
      // Правка не доехала. Забыть про неё нельзя: в панели у пользователя
      // одно, у агента другое, а `change` уже отработал и сам не повторится.
      state.panelDirty = true;
      saveStatus(String(err.message || err), true);
    }
  })();
  return state.applying;
}


// ────────────────────── вкладка «Память» ──────────────────────

// Три слоя памяти агента — по разделу на каждый, от короткого к долгому;
// данные приходят одним ответом `GET /api/agents/{id}/memory`.
//
// Ответ лежит в `state.memory`, и отрисовка берёт всё оттуда: **запрос идёт
// на открытие вкладки, а не на отрисовку**. Панель перерисовывается на каждый
// обмен и на каждую правку конфига, и запрос внутри неё превратил бы один
// поход на сервер в поток. Перечитывается память там, где она правда
// изменилась: при открытии другого чата и после обмена, — и оба раза молча,
// пока вкладка закрыта.

// Типы записей: токен для сервера и русская подпись. Одна карта на список
// записей и на дропдаун формы — второй таблицей они разъехались бы молча.
// Слова те же, что в `MEMORY_LABELS` на сервере: ими память подписана
// и в промпте.
const MEMORY_KINDS = [
  ["profile", "о собеседнике"],
  ["decision", "решение"],
  ["knowledge", "факт"],
];

const memoryKindLabel = (kind) =>
  (MEMORY_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Типы записей рабочей памяти — состояние задачи. Список свой, а не общий
// с долговременной: слои разные. Слова те же, что в `WORKING_LABELS`
// на сервере, — ими запись подписана и в промпте.
const WORKING_KINDS = [
  ["goal", "цель"],
  ["limit", "ограничение"],
  ["decision", "решение"],
  ["question", "открытый вопрос"],
];

const workingKindLabel = (kind) =>
  (WORKING_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Запись рабочей памяти так, как она уезжает в промпт: «подпись типа:
// содержимое» (`working_lines`). В той же форме она и переносится
// в долговременную — иначе говорила бы не то, что показано.
const workingLine = (record) => workingKindLabel(record.kind) + ": " + record.content;

// Правка записи прямо в списке: Enter сохраняет, Escape отменяет, потеря
// фокуса — тоже сохраняет. Идиом тот же, что у переименования чата слева:
// второй способ правки на той же странице читался бы как другое действие.
//
// Правится и текст, и **тип**. Список типов открывается **пустым**, как
// и в форме добавления: умолчания у типа нет нигде, и «оставить прежний» —
// это не выбор, а его отсутствие. Предвыбери мы нынешний тип, правка текста
// молча пересылала бы его обратно, затирая правку из соседней вкладки.
function startRecordEdit(row, record, kinds, commit) {
  const shown = row.querySelector(".mem-text");
  // Поле и список — в одном блоке: уход фокуса с поля на список это не конец
  // правки, а её продолжение, и различить их можно только на общем родителе.
  const box = el("div", "mem-edit-box");
  const input = el("input", "mem-edit");
  input.value = record.content;
  const kind = el("select", "mem-edit-kind control");
  fillKinds(kind, kinds, "— оставить тип —");
  box.append(input, kind);
  row.replaceChild(box, shown);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const text = (input.value || "").trim();
    const patch = {};
    // Пустой текст — не правка, а потеря записи: удаление здесь рядом,
    // и делать его вслепую очисткой поля нельзя. Текст слово в слово прежний
    // и невыбранный тип тоже не едут: ручке нечего было бы делать.
    if (text && text !== record.content) patch.content = text;
    if (kind.value && kind.value !== record.kind) patch.kind = kind.value;
    if (save && Object.keys(patch).length) await commit(patch);
    renderMemory();
  };

  const keys = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); finish(false); }
  };
  input.onkeydown = keys;
  kind.onkeydown = keys;
  // `focusout` всплывает, `blur` — нет: слушаем блок и смотрим, куда фокус
  // ушёл. Остался внутри — правка продолжается, ушёл наружу — сохраняем.
  // На самом поле щелчок по списку типов убрал бы список из-под курсора.
  box.onfocusout = (ev) => {
    if (!box.contains(ev.relatedTarget)) finish(true);
  };
}

function memoryTabOpen() {
  return !$("#tab-memory").classList.contains("hidden");
}

// Открытие вкладки — единственное место, откуда слои запрашиваются впервые.
async function loadMemory() {
  const id = state.current && state.current.id;
  state.memoryNote = "Читаю память…";
  renderMemory();
  try {
    // Чата ещё нет — общий слой всё равно можно показать: он не про чат.
    // Первые два раздела в этом случае честно говорят, что показывать нечего.
    state.memory = id
      ? await api("/api/agents/" + id + "/memory")
      : { short_term: null, working: null, long_term: await api("/api/memory") };
    state.memoryNote = "";
  } catch (err) {
    state.memory = null;
    state.memoryNote = String(err.message || err);
  }
  renderMemory();
}

// Сколько первых реплик не уедет в модель дословно при нынешней стратегии —
// и каким словом это называется.
//
// Расчёт повторяет серверный (`Agent.context_cut`): ручка отдаёт его
// **входы**, а не готовое число. Считается по конфигу агента, а не по полям
// панели: в панели может стоять непролитая правка, а раздел говорит о том,
// что уедет сейчас. Незнакомая стратегия читается как «вся история».
function shortTermCut(agent, layers) {
  const total = layers.short_term.messages;
  const keep = agent && agent.keep_last !== undefined ? agent.keep_last : null;
  const strategy = agent ? agent.strategy : "full";
  // Пустое поле — резать нечем: идиом тот же, что у сервера, `null` это
  // «не делать», а не «делать с нулём».
  const nothing = keep === null || keep === undefined;
  if (strategy === "window") {
    // Окно режет ровно столько, сколько просили: зажима по «докуда дочитала
    // память» больше нет ни здесь, ни на сервере — читать её стало некому,
    // а записи в ней от длины разговора не зависят вовсе.
    return {
      cut: nothing ? 0 : Math.max(0, total - keep),
      word: "отброшено окном",
    };
  }
  if (strategy === "summary") {
    const summaries = layers.short_term.summaries || [];
    const last = summaries.length ? summaries[summaries.length - 1] : null;
    const upto = last && typeof last.upto === "number" ? last.upto : 0;
    return { cut: nothing || upto <= 0 ? 0 : Math.min(upto, total), word: "заменено сводкой" };
  }
  return { cut: 0, word: "" };
}

function memRow(label, value) {
  const row = el("div", "mem-row");
  row.append(el("span", "mem-k", label), el("span", "mem-v", value));
  return row;
}

const memNote = (text) => el("p", "mem-note", text);

// Раздела нет данных — говорим почему: читаем, не открыт чат или ручка
// ответила ошибкой. Пустой раздел молчал бы о разнице между «пусто»
// и «не доехало».
const memBlank = () => memNote(state.memoryNote || "Чат ещё не открыт.");

function renderMemory() {
  renderShortTerm($("#mem-short"));
  renderWorking($("#mem-working"));
  renderLongTerm($("#mem-long"));
}

function renderShortTerm(box) {
  box.innerHTML = "";
  const layers = state.memory;
  if (!layers || !layers.short_term) { box.appendChild(memBlank()); return; }
  const total = layers.short_term.messages;
  const cut = shortTermCut(state.current, layers);
  box.append(
    memRow("Сообщений в истории", fmt.tokens(total)),
    memRow("Уезжает дословно", fmt.tokens(total - cut.cut)),
    memNote(cut.cut
      ? "Остальные " + fmt.tokens(cut.cut) + " — " + cut.word + "."
      : "Вся история уезжает в модель дословно.")
  );

  // Сводки — здесь, под историей: сводка не память, а замена той её части,
  // что не уехала дословно. Выключи сворачивание — не пропадёт ничего.
  const summaries = layers.short_term.summaries || [];
  if (!summaries.length) return;
  box.appendChild(el("div", "mem-sub", "Сводки"));
  summaries.forEach((item) => {
    const row = el("div", "mem-item column");
    row.append(
      el("div", "mem-text", item.content),
      memNote("вместо первых " + fmt.tokens(item.upto) + " сообщений")
    );
    box.appendChild(row);
  });
}

function renderWorking(box) {
  box.innerHTML = "";
  const working = state.memory && state.memory.working;
  // Форма прячется вместе со слоем: область рабочей памяти — разговор,
  // и пока слоя нет на экране — чат не открыт или ручка ответила отказом, —
  // записывать некуда.
  $("#mem-work-form").classList.toggle("hidden", !working);
  if (!working) { box.appendChild(memBlank()); return; }

  const records = working.records || [];
  if (!records.length) box.appendChild(memNote("Записей нет."));
  records.forEach((record) => {
    const line = workingLine(record);
    const row = el("div", "mem-item");
    // Продвижение записи через границу слоёв: то, что записали как состояние
    // задачи, оказалось верным и после неё. Это нажатие, а не автоматика:
    // границу слоёв проводит человек — он один и пишет в оба.
    const btn = el("button", "mem-btn", "Запомнить надолго");
    btn.type = "button";
    btn.title = "Запомнить надолго";
    btn.onclick = () => promote(line);
    row.append(
      el("div", "mem-text", line),
      btn,
      iconButton("pencil", "Поправить запись",
        () => startRecordEdit(row, record, WORKING_KINDS,
          (patch) => editWorking(record, patch)), "mini"),
      iconButton("trash", "Удалить запись", () => dropWorking(record), "mini danger")
    );
    box.appendChild(row);
  });

}

function renderLongTerm(box) {
  box.innerHTML = "";
  const long = state.memory && state.memory.long_term;
  if (!long) { box.appendChild(memBlank()); return; }
  const records = long.records || [];
  if (!records.length) { box.appendChild(memNote("Записей нет.")); return; }
  records.forEach((record) => {
    const row = el("div", "mem-item");
    row.append(
      el("div", "mem-kind", memoryKindLabel(record.kind)),
      el("div", "mem-text", record.content),
      iconButton("pencil", "Поправить запись",
        () => startRecordEdit(row, record, MEMORY_KINDS,
          (patch) => editMemory(record, patch)), "mini"),
      iconButton("trash", "Забыть запись", () => forget(record), "mini danger")
    );
    box.appendChild(row);
  });
}

function memoryStatus(text, isError) {
  const box = $("#mem-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

function workingStatus(text, isError) {
  const box = $("#mem-work-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

// ── рабочая память правится руками ──
// Тем же набором, каким правится долговременная, и по тем же правилам: тип
// обязателен и без умолчания, список пополняется **ответом ручки**. Второй
// способ на той же странице читался бы как другое действие.
const workingUrl = (seq) => {
  const id = state.current && state.current.id;
  return "/api/agents/" + id + "/working" + (seq === undefined ? "" : "/" + seq);
};

async function addWorking(kind, content) {
  const text = (content || "").trim();
  if (!text) {
    workingStatus("Текст записи пуст: записывать нечего.", true);
    return false;
  }
  if (!(state.current && state.current.id)) {
    workingStatus("Чат ещё не открыт: рабочая память живёт в разговоре.", true);
    return false;
  }
  try {
    const record = await api(workingUrl(), json("POST", { kind, content: text }));
    const working = state.memory && state.memory.working;
    if (working) working.records = [...(working.records || []), record];
    renderMemory();
    workingStatus("Записано: " + workingKindLabel(record.kind) + ".");
    return true;
  } catch (err) {
    workingStatus(String(err.message || err), true);
    return false;
  }
}

// Правка метит запись человеком — это делает сервер, и ответ приходит уже
// с новым автором. Подставь клиент своё «human» — экран говорил бы о записи
// то, чего в базе нет.
async function editWorking(record, patch) {
  try {
    const updated = await api(workingUrl(record.seq), json("PATCH", patch));
    const working = state.memory && state.memory.working;
    if (working) {
      working.records = (working.records || [])
        .map((item) => (item.seq === updated.seq ? updated : item));
    }
    workingStatus("Запись поправлена: с этой минуты она ваша.");
  } catch (err) {
    workingStatus(String(err.message || err), true);
  }
}

async function dropWorking(record) {
  try {
    await api(workingUrl(record.seq), { method: "DELETE" });
  } catch (err) {
    workingStatus(String(err.message || err), true);
    return;
  }
  const working = state.memory && state.memory.working;
  if (working) {
    working.records = (working.records || []).filter((item) => item.seq !== record.seq);
  }
  renderMemory();
  workingStatus("Запись убрана.");
}

// Правка долговременной записи — тот же путь и тот же ответ: с этой минуты
// запись человека, и служебный вызов её не перепишет.
async function editMemory(record, patch) {
  try {
    const updated = await api("/api/memory/" + record.seq, json("PATCH", patch));
    const long = state.memory && state.memory.long_term;
    if (long) {
      long.records = (long.records || [])
        .map((item) => (item.seq === updated.seq ? updated : item));
    }
    memoryStatus("Запись поправлена: с этой минуты она ваша.");
  } catch (err) {
    memoryStatus(String(err.message || err), true);
  }
}

// Новая запись долговременной памяти. Список пополняется **записанным
// ответом**, а не присланным телом: номер выдаёт база, текст чистит
// `redact()`. Дедупликации нет намеренно: отличить «то же самое»
// от «похожего» может только человек, а лишняя удаляется одной кнопкой.
async function remember(kind, content) {
  const text = (content || "").trim();
  if (!text) {
    memoryStatus("Текст записи пуст: записывать нечего.", true);
    return false;
  }
  try {
    const record = await api("/api/memory", json("POST", { kind, content: text }));
    const long = state.memory && state.memory.long_term;
    if (long) long.records = [...(long.records || []), record];
    renderMemory();
    memoryStatus("Запомнено: " + memoryKindLabel(record.kind) + ".");
    return true;
  } catch (err) {
    memoryStatus(String(err.message || err), true);
    return false;
  }
}

async function forget(record) {
  try {
    await api("/api/memory/" + record.seq, { method: "DELETE" });
  } catch (err) {
    memoryStatus(String(err.message || err), true);
    return;
  }
  const long = state.memory && state.memory.long_term;
  if (long) long.records = (long.records || []).filter((item) => item.seq !== record.seq);
  renderMemory();
  memoryStatus("Запись забыта.");
}

// «Запомнить надолго»: перенос записи через границу слоёв. Кнопка
// не записывает, а **спрашивает**: кладёт строку в ту же форму, которой
// слой пополняют руками, и снимает выбор типа. Форма одна и та же, а значит
// и правила у переноса те же: без выбранного типа не уходит ничего. Зашитый
// в кнопку тип был единственным местом во всём слое, где его выбирал код.
function promote(line) {
  $("#mem-content").value = line;
  const kind = $("#mem-kind");
  kind.value = "";
  kind.focus();
  memoryStatus("Выберите тип записи и нажмите «Запомнить»: при переносе тип выбирает человек.");
}

// Тип записи уезжает тот, что выбран в списке: умолчания у него нет ни здесь,
// ни на сервере — `_kind_field` отказывает и отсутствию ключа тоже.
async function addFromForm() {
  const kind = $("#mem-kind").value;
  // Тип не выбран — не шлём вовсе: ручка ответит 400, и незачем спрашивать
  // сервер о том, что видно здесь. Отказ при этом тот же по смыслу —
  // «тип записи выбирает человек».
  if (!kind) {
    memoryStatus("Тип записи не выбран: о собеседнике, решение или факт.", true);
    return;
  }
  const field = $("#mem-content");
  const saved = await remember(kind, field.value);
  if (saved) field.value = "";
}

// Та же форма для рабочего слоя: тип обязателен и здесь, и умолчания
// у него нет — слои устроены одинаково, и второе правило на втором слое
// разошлось бы с первым молча.
async function addWorkingFromForm() {
  const kind = $("#mem-work-kind").value;
  if (!kind) {
    workingStatus("Тип записи не выбран: цель, ограничение, решение или открытый вопрос.", true);
    return;
  }
  const field = $("#mem-work-content");
  const saved = await addWorking(kind, field.value);
  if (saved) field.value = "";
}

// Опции дропдауна — из той же карты, что и подписи в списке.
//
// Первым пунктом — пустой: **умолчания у типа нет и в форме**, ровно как
// на сервере. Уберём его — список возьмёт первый настоящий, и не тронувший
// его человек запишет «о собеседнике», ничего не выбрав. Список типов —
// параметром: формы две, а правило одно.
function fillKinds(select, kinds, blankLabel) {
  select.innerHTML = "";
  const blank = el("option", "", blankLabel || "— выберите тип —");
  blank.value = "";
  select.appendChild(blank);
  kinds.forEach(([token, label]) => {
    const option = el("option", "", label);
    option.value = token;
    select.appendChild(option);
  });
}

// ─────────────────────────── профиль ──────────────────────────
// Профиль — про то, **как** с человеком разговаривать. Он один на всю базу,
// как долговременная память, и пишет в него только человек: профиль это
// распоряжение («отвечай кратко»), а не наблюдение («пишет на Kotlin»).
// Запрашивается лениво, на открытие вкладки, ровно как слои памяти.

// Поля — тем же списком, что `PROFILE_FIELDS` на сервере, и в том же порядке:
// им же собран блок `[как отвечать]` в промпте.
const PROFILE_FIELDS = ["style", "format", "context"];

const profileInput = (name) => $("#profile-" + name);

function profileStatus(text, isError) {
  const box = $("#profile-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

function showProfile(values) {
  PROFILE_FIELDS.forEach((name) => {
    profileInput(name).value = values[name] || "";
  });
}

async function loadProfile() {
  try {
    const answer = await api("/api/profile");
    showProfile(answer.profile || {});
    profileStatus("");
  } catch (err) {
    profileStatus(String(err.message || err), true);
  }
}

// Правка одного поля: уезжает **только тронутое** — иначе вторая вкладка,
// правящая формат, затирала бы стиль, набранный в первой. Пустая строка
// поле снимает, отдельной кнопки «очистить» нет.
async function saveProfile(name) {
  const field = profileInput(name);
  try {
    const answer = await api("/api/profile", json("PATCH", { [name]: field.value }));
    const values = answer.profile || {};
    // Показываем **записанное**, а не набранное: текст по дороге чистит
    // `redact()`, и поле обязано показывать то, что уедет в промпт.
    showProfile(values);
    profileStatus(PROFILE_FIELDS.some((key) => values[key])
      ? "Профиль сохранён: он уезжает системным сообщением в каждый запрос."
      : "Профиль пуст: к запросам не добавляется ничего.");
  } catch (err) {
    profileStatus(String(err.message || err), true);
  }
}

// ─────────────────────────── плитки ───────────────────────────

// Плитки справа — про весь диалог, а не про последний ответ. Числа одного
// обмена написаны под ним самим в ленте, и подписей «накопленное» здесь нет:
// в панели всё и так про разговор. Плиток шесть, сетка 2×3.
const TILES = [
  ["Входные токены", () => fmt.tokens(totalField("prompt_tokens"))],
  ["Выходные токены", () => fmt.tokens(totalField("completion_tokens"))],
  ["Всего токенов", () => fmt.tokens(totalField("total_tokens"))],
  ["Стоимость", () => fmt.cost(totalField("cost_usd")), true],
  // Сообщение — одна реплика: и вопрос, и ответ. Считает их сервер полем
  // `history_len`. Внутрь сумм число не убрано намеренно: у чата с молчащим
  // usage сумм нет вовсе, а сообщения были. Сжатие счётчик не растит.
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

// ─────────────────── сворачивание и тема ──────────────────────

const KEYS = { sidebar: "ui.sidebar", panel: "ui.panel", theme: "ui.theme" };

function store(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* приватный режим — не беда */ }
}
function read(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (e) { return fallback; }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = $("#theme-toggle");
  btn.innerHTML = "";
  const ico = el("span", "foot-icon");
  ico.appendChild(icon(theme === "dark" ? "sun" : "moon"));
  btn.append(ico, el("span", "foot-label", theme === "dark" ? "Светлая тема" : "Тёмная тема"));
}

const NARROW = window.matchMedia("(max-width: 940px)");

// Свёрнутый борт оставляет на экране кнопку разворота: иначе вернуть его
// было бы нечем.
function applyCollapsed(which, collapsed) {
  $("#app").classList.toggle(which === "sidebar" ? "no-sidebar" : "no-panel", collapsed);
  const id = which === "sidebar" ? "restore-sidebar" : "restore-panel";
  const existing = document.getElementById(id);
  if (existing) existing.remove();
  if (collapsed) {
    const btn = iconButton(
      which === "sidebar" ? "panelLeft" : "panelRight",
      which === "sidebar" ? "Показать список" : "Показать настройки",
      () => setCollapsed(which, false)
    );
    btn.id = id;
    btn.classList.add("floating-toggle", which === "sidebar" ? "left" : "right");
    document.querySelector(".chat").appendChild(btn);
  }
  syncBackdrop();
}

function isCollapsed(which) {
  return $("#app").classList.contains(which === "sidebar" ? "no-sidebar" : "no-panel");
}

function setCollapsed(which, collapsed) {
  // На узком окне свёрнутость — это состояние ящика, а не выбор пользователя:
  // запоминать её нельзя, иначе она переедет на широкое окно и там оба борта
  // окажутся закрыты без причины.
  if (!NARROW.matches) store(KEYS[which], collapsed ? "1" : "0");
  applyCollapsed(which, collapsed);
  // Ящики не соседствуют: открыли один — второй закрывается.
  if (NARROW.matches && !collapsed) {
    const other = which === "sidebar" ? "panel" : "sidebar";
    if (!isCollapsed(other)) applyCollapsed(other, true);
  }
}

function openDrawers() {
  return ["sidebar", "panel"].filter((which) => !isCollapsed(which));
}

function closeDrawers() {
  openDrawers().forEach((which) => applyCollapsed(which, true));
}

// Затемнение под открытым ящиком: по клику в него ящик закрывается.
function syncBackdrop() {
  const existing = document.querySelector(".backdrop");
  const needed = NARROW.matches && openDrawers().length > 0;
  if (!needed) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  const backdrop = el("div", "backdrop");
  backdrop.onclick = closeDrawers;
  document.body.appendChild(backdrop);
}

// Какие борта свёрнуты при данной ширине. Отдельной функцией без DOM —
// решение проверяется без браузера.
function layoutFor(narrow, stored) {
  // На узком окне борта — ящики поверх ленты, и оба закрыты: иначе от чата
  // остаётся полоска посередине.
  if (narrow) return { sidebar: true, panel: true };
  return { sidebar: stored.sidebar === "1", panel: stored.panel === "1" };
}

// Ширина окна изменилась: на узком закрываем оба борта, на широком
// возвращаем то, что пользователь выбрал сам.
function applyWidth() {
  const want = layoutFor(NARROW.matches, {
    sidebar: read(KEYS.sidebar, "0"),
    panel: read(KEYS.panel, "0"),
  });
  applyCollapsed("sidebar", want.sidebar);
  applyCollapsed("panel", want.panel);
}

// ─────────────────── новый чат и подтверждения ────────────────

// Что должен закрыть Escape: диалог подтверждения всегда важнее ящиков.
// Отдельной функцией без DOM — решение проверяется без браузера.
function escapeAction(hasDialog, narrow, openDrawerCount) {
  if (hasDialog) return "dialog";
  if (narrow && openDrawerCount > 0) return "drawers";
  return null;
}

function confirmBox(title, text, confirmLabel, onYes) {
  const wrap = el("div", "confirm");
  const box = el("div", "confirm-box");
  const row = el("div", "confirm-row");

  const close = () => {
    document.removeEventListener("keydown", onKey);
    wrap.remove();
  };
  // Escape закрывает диалог всегда, а не только на узком окне: выйти
  // из подтверждения необратимого действия надо уметь не глядя.
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
  };
  document.addEventListener("keydown", onKey);

  const no = el("button", "", "Отмена");
  no.type = "button";
  no.onclick = close;
  const yes = el("button", "primary", confirmLabel);
  yes.type = "button";
  yes.onclick = () => { close(); onYes(); };
  row.append(no, yes);
  box.append(el("h3", "", title), el("p", "", text), row);
  wrap.appendChild(box);
  wrap.onclick = (ev) => { if (ev.target === wrap) close(); };
  document.body.appendChild(wrap);
}

async function newChat() {
  stopStream();
  const created = await api("/api/agents", json("POST", {}));
  state.current = null;
  await loadAgents(created.agents[0].id);
  $("#input").focus();
}

// ─────────────────────────── старт ────────────────────────────

function init() {
  $("#sidebar-toggle").appendChild(icon("panelLeft"));
  $("#panel-toggle").appendChild(icon("panelRight"));
  $("#sidebar-toggle").onclick = () => setCollapsed("sidebar", true);
  $("#panel-toggle").onclick = () => setCollapsed("panel", true);

  applyTheme(read(KEYS.theme, "light"));
  $("#theme-toggle").onclick = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    store(KEYS.theme, next);
    applyTheme(next);
  };

  applyWidth();
  NARROW.addEventListener("change", applyWidth);
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const what = escapeAction(
      Boolean(document.querySelector(".confirm")),
      NARROW.matches,
      openDrawers().length
    );
    // Диалог закрывает себя сам — свой обработчик он вешает поверх этого.
    if (what === "drawers") closeDrawers();
  });

  $("#new-chat").onclick = () => newChat();

  // Настройки применяются по change: у полей ввода это потеря фокуса,
  // у списков — выбор. Отдельной кнопки сохранения нет.
  $("#panel-body").addEventListener("change", (ev) => {
    // Пролив панели — про конфиг чата, и поля его носят приставку `f-`.
    // Поля вкладок «Память» и «Профиль» её не носят: у обоих свои ручки,
    // и PATCH чата был бы запросом ни о чём. Сохраняются они тем же
    // событием `change` — отдельной кнопки сохранения в панели нет нигде.
    const id = String(ev.target.id || "");
    if (id.startsWith("profile-")) return saveProfile(id.slice("profile-".length));
    if (!id.startsWith("f-")) return;
    if (id === "f-response_format_kind") syncResponseFormat();
    // Показ полей меняется на самом выборе, а не после сохранения: пролив
    // конфига ходит на сервер, и ждать ответа, чтобы убрать с экрана поле,
    // которое уже ни на что не влияет, — значит снова обещать не то.
    if (id === "f-strategy") syncStrategyFields();
    applySettings();
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      const which = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      PANEL_TABS.forEach((name) => $("#tab-" + name).classList.toggle("hidden", name !== which));
      // Память запрашивается здесь и только здесь: лениво, на открытие
      // вкладки (отрисовка своего запроса не делает). Открытие — ещё
      // и отметка «показано»: обмен и смена чата читают слои без неё,
      // иначе считать было бы нечего.
      if (which === "memory") loadMemory();
      // Профиль — тем же порядком и по тому же доводу: лениво, на открытие
      // вкладки. Он глобальный, и перечитывать его на смену чата незачем.
      if (which === "profile") loadProfile();
    };
  });

  $("#feed").addEventListener("scroll", () => {
    state.stick = atBottom($("#feed"));
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
  $("#mem-add").onclick = () => addFromForm();
  $("#mem-work-add").onclick = () => addWorkingFromForm();

  setBusy(false);
  renderTiles();
  // Чата ещё нет — значит нет и задачи: шапку прячем сразу, не дожидаясь
  // списка. Разметка прячет её и сама, но полагаться на это нельзя: оттуда
  // её однажды уберут, а `renderTask` останется.
  renderTask(null);
  renderMemory();
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
    layoutFor,
    escapeAction,
    readStopLines,
    parseResponseFormat,
    paramWarnings,
    fmt,
  };
}
