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
  panelDirty: false,   // правка панели не доехала до агента
  stick: true,         // лента примотана к низу — доматывать новые ответы
  baseModel: "",       // модель, с которой чат открыли: с ней сверяем смену
  contextStale: false, // модель сменили — прежняя доля окна к новой не относится
  contextPast: false,  // доля окна осталась от прошлого обмена: последний упал
  statusTimer: null,   // таймер, гасящий строку состояния
  prompts: new Map(),  // промпты обменов этой вкладки (см. promptKey)
  memory: null,        // три слоя памяти — ответ ручки, прочитанный на открытие вкладки
  memoryNote: "",      // почему слоёв не видно: читаем, чат не открыт, ручка ответила ошибкой
  log: new Map(),      // журнал работы по чатам (см. logEvent)
};

// Ключ промпта в `state.prompts`: чат и номер реплики-ответа в его истории.
//
// Карта живёт только до перезагрузки страницы, и это не недоделка: промпт —
// производная истории, которая и так лежит в базе. Записывать его туда значило
// бы хранить копию разговора в каждой строке — и хранить её устаревшей, потому
// что сводка со следующим сворачиванием меняется. Обновил страницу — кнопки
// у старых ответов нет, и это честнее, чем показать промпт, собранный заново
// и не тот, что уехал.
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

// Заполнение контекста — единственное в правой панели, что не про весь диалог:
// доля окна, занятая последним обменом. Усреднять её по диалогу нечего, а при
// смене модели она сбрасывается: окно у новой модели другое, и прежний процент
// к ней не относится. Плитка молчит прочерком, пока не придёт первый ответ
// на новой модели.
//
// Сброс поднимает сама смена модели в панели (`state.contextStale`), а не
// расхождение имён: провайдер вправе вернуть не то имя, которое просили, —
// на `openrouter/auto` он так и делает **всегда**, — и сверка имён гасила бы
// плитку после каждого ответа, навсегда.
function has(value) {
  return value !== null && value !== undefined;
}

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
  // Полоса этапов — вместе с лентой и по тому же ответу: состояние приехало
  // с чатом, и спрашивать его отдельно незачем.
  renderStages(agent);
  renderFeed(agent);
  fillPanel(agent);
  taskStatus("");
  renderTiles();

  // Открыт другой чат — первые два слоя теперь его, а не прежние. Читаем их
  // заново, но только если вкладка открыта: закрытой они не нужны.
  state.memory = null;
  // Значок новых записей — про **этот** чат, и число ему даёт сам чат, а не
  // слои: закрытая вкладка его не гасит, а показывает, сколько агент завёл
  // в открытом чате с тех пор, как ему показывали память. Своего вызова ему
  // здесь не нужно: обе ветки ниже кончаются отрисовкой, а она его считает.
  if (memoryTabOpen()) loadMemory();
  else renderMemory();
  // Журнал — про **этот** чат: в нём работа открытого, а не общий поток.
  // Запроса за ним нет вовсе, он и так в памяти вкладки.
  renderLog();

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
  // стратегии. У окна это единственный способ прочитать отброшенное начало,
  // а решать за читателя, что у «Всей истории» смотреть незачем, значит
  // прятать от него ровно то, что уехало в модель. Промпт берётся из этой же
  // вкладки — не сохранился (страницу перезагрузили), значит и показывать
  // нечего.
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

// Служебные шаги обмена — те, на которых собеседник ждёт, а текста ещё
// нет. Их три, потому что их три и на сервере: сворачивание идёт **до**
// ответа, вызов инструмента и ожидание продолжения — посреди него.
// Середина до сих пор молчала: модель уходила за кнопкой, карточка стояла
// пустой, и понять, что работа идёт, было неоткуда.
//
// Один индекс на оба показа, а не две таблицы: строка состояния обещала бы
// одно, а подпись называла бы другое, и разошлись бы они молча. Какой шаг
// идёт, говорит сервер полем `strategy`; клиент про это не догадывается
// по тексту сообщений. Врезка рабочей памяти подписана здесь же, хотя
// служебного вызова за ней нет вовсе: её текст в промпте не изменился —
// изменилось только то, чья это работа.
const SERVICE_CALLS = {
  summary: { status: "Сворачиваю начало разговора…", role: "сводка начала разговора" },
  facts: { role: "факты о разговоре" },
  tool: { status: "Исполняю вызов инструмента…" },
  continue: { status: "Жду продолжения после вызова…" },
};

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

  if (!tokens.length && !how.length && !m.stage_note) return null;
  const box = el("div", "card-usage");
  // Отказ инструменту — отдельной строкой и первой: модель просила перевести
  // задачу, её не пустили, и молчать об этом нельзя. Без строки это выглядело
  // бы так, будто она ничего и не просила. Строка приезжает готовой, с теми же
  // подписями этапов, что на полосе, — клиент её не сочиняет.
  if (m.stage_note) box.appendChild(el("div", "usage-stage", m.stage_note));
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

  await exchange("/api/agents/" + state.current.id + "/messages", { text }, text);
}

async function regenerate() {
  if (state.busy || !state.current || !state.hasKey) return;
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
  hint("");

  // Журнал работы: пишем сюда же, где события и приходят, — второго места,
  // знающего про ход обмена, заводить нельзя. Чат запоминаем **на входе**:
  // пока идёт обмен, открыть могли уже соседний, и запись обязана лечь
  // в журнал того чата, который её породил.
  const logId = agent.id;
  // Переходы этапа приезжают целым журналом, а не событием: что случилось
  // в **этом** обмене — это строки, которых в нём не было до него. Сверяем
  // по номерам, а не по длине: номер строки её и опознаёт.
  const seenMoves = new Set(((agent.task_log) || []).map((m) => m.seq));
  logEvent(
    logId,
    "обмен начат",
    questionText === null ? "перегенерация последнего ответа" : oneLine(questionText, 90)
  );
  // Строка сворачивания заводится по событию, а подробность к ней приезжает
  // с числами ответа: сколько сообщений свернулось, говорят они. Дописываем
  // в ту же строку, а не заводим вторую, — работа была одна.
  let folded = null;

  const controller = new AbortController();
  state.abort = controller;
  let answer = "";
  let reasoning = "";
  let thinking = null;
  let failure = null;
  let status = null;
  let prompt = null;
  let committed = false;

  try {
    await streamPost(
      path,
      body,
      (e) => {
        switch (e.event) {
          case "compressing":
            // В журнал сворачивание идёт своей строкой, а число свёрнутых
            // сообщений допишется в неё же, когда приедут числа ответа:
            // раньше его не знает никто. У вызова инструмента и продолжения
            // строку заводит не этот кадр, а отладочный — тот, что приезжает
            // с содержимым: строка без содержимого была бы ровно тем
            // пересказом, от которого журнал и уходит.
            if (e.strategy === "summary") folded = logEvent(logId, "сворачивание", "идёт");
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
            // Промпт собран — значит служебный вызов позади и дальше пойдёт
            // ответ: строке состояния больше нечего показывать.
            if (status) { status.remove(); status = null; }
            // Этап приехал вместе с промптом — тот, с которым обмен уехал
            // в модель. Двигать его инструмент будет уже по ходу ответа,
            // и новое состояние приедет кадром `done`. Поля вкладки при этом
            // не трогаем: в них мог набирать человек, и затирать набранное
            // кадром обмена нельзя.
            if (e.task) applyTask(e.task);
            // Промпт держим у каждого обмена, а не только у того, где есть
            // врезка. У «Всей истории» он и правда повторяет ленту, зато
            // скользящее окно начало **отбрасывает** — и прочитать, что
            // именно уехало в модель, больше негде. Врезки может не быть
            // вовсе: `summary_at` придёт пустым, и подписывать в промпте
            // будет просто нечего.
            if (e.resolved_messages) {
              prompt = {
                messages: e.resolved_messages,
                // Слотов три: врезка долговременной памяти, врезка рабочей
                // и врезка стратегии. Все три называет сервер, все три бывают
                // пустыми, и в одном промпте они встречаются вместе — обе
                // памяти едут при любой обрезке и ни одну не отменяют.
                memoryAt: e.memory_at,
                workingAt: e.working_at,
                summaryAt: e.summary_at,
                strategy: e.strategy,
              };
              // Он же — строкой журнала, с телом: что уехало в модель,
              // ролями и текстом, как уехало. Второй раз спрашивать сервер
              // не о чем — кадр `start` это уже привёз.
              logEvent(
                logId,
                "запрос к модели",
                "сообщений: " + e.resolved_messages.length,
                false,
                promptText(e.resolved_messages)
              );
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
            // Текст пошёл — шаг кончился, и строке состояния больше нечего
            // показывать. У сворачивания её снимал кадр `start`, но вызов
            // инструмента стоит **после** него, и снимать её там уже некому.
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
          case "debug":
            // Содержимое шага — дословно и **по ходу**: строка появляется
            // в журнале тогда же, когда шаг случился, а не в конце обмена.
            logDebug(logId, e);
            break;
          case "error":
            failure = e.message;
            logEvent(logId, "ошибка", oneLine(e.message, 120), true, e.message);
            // Перерисовка здесь не лишняя: метрики упавшего обмена меняют
            // показанное (пометкой «из прошлого обмена», а на частичных
            // числах — и значением), а `done` после ошибки приходит не всегда.
            if (e.metrics) { keepMetrics(e.metrics); renderTiles(); }
            break;
          case "done":
            // Записался ли обмен в историю — знает агент, и говорит прямо.
            committed = e.committed === true;
            // Этап приезжает здесь, а не кадром `start`: двигает его
            // инструмент по ходу ответа, и в `start` он ещё прежний.
            // Полоса переезжает **сразу после ответа** и без единого
            // лишнего запроса — журнал приезжает тем же кадром.
            if (e.task) {
              if (state.current) state.current.task_log = e.task_log || [];
              applyTask(e.task);
            }
            if (e.text) answer = e.text;
            if (e.reasoning) reasoning = e.reasoning;
            if (e.metrics) keepMetrics(e.metrics);
            // Сколько сообщений свернулось — в ту же строку журнала, что
            // завело событие: работа была одна, и двумя строками она
            // читалась бы как две.
            if (folded && e.metrics && has(e.metrics.summarized)) {
              folded.detail = "свёрнуто сообщений: " + fmt.tokens(e.metrics.summarized);
            }
            // Переходы **человека** — по новым строкам журнала переходов:
            // кнопка утверждения плана ходит мимо обмена, и отладочного
            // кадра за ней нет. Вызовы модели сюда не идут: их строку уже
            // завёл кадр `debug`, и завести её второй раз значило бы
            // рассказать об одной работе дважды.
            (e.task_log || [])
              .filter((m) => !seenMoves.has(m.seq) && m.who !== "agent")
              .forEach((m) => logMove(logId, m, e.metrics));
            if (status) { status.remove(); status = null; }
            if (!e.error) {
              // Сырой текст ответа целиком — телом строки: лента показывает
              // его разметкой, а здесь он нужен таким, каким пришёл.
              logEvent(logId, "ответ получен", logAnswerDetail(e.metrics), false, answer);
            }
            bodyEl.innerHTML = renderMarkdown(answer);
            renderTiles();
            break;
        }
      },
      controller.signal
    );
  } catch (err) {
    if (err.name === "AbortError") logEvent(logId, "отменено", "поток остановлен", true);
    else {
      failure = String(err.message || err);
      logEvent(logId, "ошибка", oneLine(failure, 120), true, failure);
    }
  }

  // Обмен кончился, чем бы ни кончился: шага, который «идёт», больше нет.
  if (status) { status.remove(); status = null; }
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

  // Лента и список слева перерисовываются по серверу: на экране должно быть
  // ровно то, что у агента в истории, а не то, что мы дорисовали по дороге.
  //
  // Промпт привязывается к ответу, только если обмен **доехал до истории**.
  // Упавший обмен её не удлиняет, а событие `start` у него уже уехало — со
  // своим промптом и своей сводкой. Привяжи его по длине истории, и он лёг бы
  // под ключ **прошлого** ответа: кнопка под давней карточкой показала бы
  // чужой запрос, внутри которого лежит сам этот ответ.
  await refreshCurrent(committed ? prompt : null);
}

async function refreshCurrent(prompt) {
  if (!state.current) return;
  try {
    const fresh = await api("/api/agents/" + state.current.id);
    state.current = fresh;
    // Промпт привязываем к реплике до перерисовки: номер ответа в истории
    // известен только теперь, а рисовать карточку с кнопкой уже пора. Сюда он
    // доезжает, только если обмен записался, — значит последняя реплика
    // истории и есть его ответ.
    if (prompt) {
      state.prompts.set(promptKey(fresh.id, fresh.transcript.length - 1), prompt);
    }
    const listed = state.agents.find((a) => a.id === fresh.id);
    if (listed) { listed.history_len = fresh.history_len; listed.label = fresh.label; }
    renderList();
    renderFeed(fresh);
    // Полоса и журнал — по тому же свежему ответу: этап мог переехать
    // служебным вызовом этого обмена, и на экране обязано стоять
    // состояние сервера, а не то, что мы дорисовали по дороге.
    renderStages(fresh);
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
//
// «Задачи» среди них больше нет: вкладка повторяла шапку чата — тот же этап,
// те же два поля, — а два места для одного и того же расходятся на первой же
// правке. Оба поля и журнал переходов переехали под полосу, туда, где на них
// и смотрят.
const PANEL_TABS = ["model", "agent", "memory", "profile", "log"];

const NUMBER_FIELDS = [
  "temperature", "max_tokens", "top_p", "top_k", "min_p",
  "repetition_penalty", "presence_penalty", "frequency_penalty",
];

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
// **до** отправки: на каждом вызове стоит provider.require_parameters=true,
// и параметр, которого модель не заявляет, выкашивает провайдеров — вместо
// ответа придёт ошибка, по которой не понять, что виноват один переключатель.
// Отдельной функцией без DOM — решение проверяется без браузера.
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
      }
      const listed = state.agents.find((a) => a.id === id);
      if (listed) Object.assign(listed, updated);
      state.panelDirty = false;
      // Модель сменили — плитка контекста гаснет сразу, а не после следующего
      // ответа: окно у новой модели другое. Правка температуры её не трогает.
      if (updated.model !== before.model) state.contextStale = true;
      renderTiles();
      // Сравнивается не панель с панелью, а конфиг агента до и после:
      // сервер по дороге нормализует (пустой список стоп-строк становится
      // `null`), и панель, разошедшаяся с агентом только формой записи,
      // изменением не является. Сравниваются только поля панели: стенограмма
      // и занятость живут своей жизнью, и по ним «изменилось» было бы
      // правдой всегда.
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

// Три слоя памяти агента — по разделу на каждый, в порядке от короткого
// к долгому. Данные приходят одним ответом `GET /api/agents/{id}/memory`:
// `short_term` — счётчик сообщений этого чата, `working` — записи о состоянии
// задачи и сводки этого чата, `long_term` — выключатель чата и общий на всю
// базу список.
//
// Ответ лежит в `state.memory`, и отрисовка берёт всё оттуда: **запрос идёт
// на открытие вкладки, а не на отрисовку**. Панель перерисовывается на каждый
// обмен и на каждую правку конфига, а слои столько раз не меняются — запрос
// внутри отрисовки превратил бы один поход на сервер в поток.
//
// Перечитывается память там, где она правда изменилась: при открытии другого
// чата (первые два слоя — его собственные) и после обмена (история выросла,
// память обновилась). И то и другое — события, а не отрисовки, и оба молчат,
// пока вкладка закрыта.

// Типы записей: токен для сервера и русская подпись. Одна карта на список
// записей и на дропдаун формы — его опции строятся отсюда же (`fillKinds`).
// Второй таблицей подписи разъехались бы молча: в списке стояло бы одно
// слово, а в форме другое. Слова те же, что в `MEMORY_LABELS` на сервере, —
// ими же память подписана и в промпте.
const MEMORY_KINDS = [
  ["profile", "о собеседнике"],
  ["decision", "решение"],
  ["knowledge", "факт"],
];

const memoryKindLabel = (kind) =>
  (MEMORY_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Типы записей рабочей памяти — состояние задачи. Список свой, а не общий
// с долговременной: слои разные, и «цель» в одном не значит того же, что
// «о собеседнике» в другом. Слова те же, что в `WORKING_LABELS` на сервере, — ими
// же запись подписана и в промпте.
const WORKING_KINDS = [
  ["goal", "цель"],
  ["limit", "ограничение"],
  ["decision", "решение"],
  ["question", "открытый вопрос"],
];

const workingKindLabel = (kind) =>
  (WORKING_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Запись рабочей памяти так, как она уезжает в промпт: «подпись типа:
// содержимое» (`working_lines`, app/agent.py). В той же форме она и
// продвигается в долговременную память — иначе запись говорила бы не то,
// что показано.
const workingLine = (record) => workingKindLabel(record.kind) + ": " + record.content;

// Правка записи прямо в списке: Enter сохраняет, Escape отменяет, потеря
// фокуса — тоже сохраняет. Идиом тот же, что у переименования чата слева:
// второй способ правки на той же странице читался бы как другое действие.
//
// Правится и текст, и **тип**: агент кладёт запись не в тот слой и не того
// типа примерно с той же частотой, и до сих пор второе чинилось только
// удалением с заведением заново — дырой ровно посреди «явного выбора»,
// ради которого день и делался. Ручка оба поля принимала с самого начала,
// не спрашивал их только экран.
//
// Список типов открывается **пустым**, как и в форме добавления: умолчания
// у типа нет нигде, и «оставить прежний» — это не выбор, а его отсутствие.
// Предвыбери мы здесь нынешний тип, правка текста молча пересылала бы его
// обратно — и запись, которой тип поправили в соседней вкладке, вернулась бы
// к старому.
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
  // ушёл. Остался внутри — правка продолжается; ушёл наружу — сохраняем,
  // ровно как раньше сохранял уход фокуса с поля. Вешай мы это на само поле,
  // щелчок по списку типов убрал бы список прямо из-под курсора.
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
// Расчёт повторяет серверный (`Agent.context_cut` и `summary_cover`): ручка
// отдаёт его **входы** — длину истории и докуда покрывает последняя сводка,
// — а не готовое число. Сводки при этом лежат в краткосрочном разделе:
// сводка не запомненное, а чем заменено то, что не уехало дословно.
//
// Считается по конфигу агента, а не по полям панели: в панели может стоять
// непролитая правка, а раздел говорит о том, что уедет сейчас. Незнакомая
// стратегия читается как «вся история» — ровно как на сервере.
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
  // что не уехала дословно. Выключи сворачивание — не пропадёт ничего,
  // история цела и сводка соберётся заново; памятью её делал только сосед
  // по разделу.
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
//
// Тем же набором, каким правится долговременная, и по тем же правилам: тип
// обязателен и без умолчания, список пополняется **ответом ручки** (номер
// выдаёт база, текст по дороге чистит `redact()`), перечитывать слой для
// этого незачем. Второй способ на той же странице читался бы как другое
// действие, и слои разъехались бы на первой правке.
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
// ответом**, а не присланным телом: номер выдаёт база, а текст по дороге
// чистит `redact()`. Перечитывать слой целиком для этого незачем.
//
// Дедупликации нет намеренно: второй клик по тому же факту заводит вторую
// запись. Отличить «то же самое» от «похожего» может только человек, и
// удаляется лишняя одной кнопкой.
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

// «Запомнить надолго»: перенос записи через границу слоёв — единственное
// место, где запись меняет слой, и до сих пор единственное, где тип выбирал
// не человек. В коде стояло `knowledge`, и это прямо против нашего же
// правила: умолчания у типа нет ни в форме, ни на сервере. Мало того, что
// выбрано за человека, — выбрано ещё и наугад: «о собеседнике» подходит
// переносимой записи ничуть не реже.
//
// Поэтому кнопка не записывает, а **спрашивает**: кладёт строку в ту же
// форму, которой долговременный слой пополняют руками, и снимает выбор типа.
// Форма одна и та же — второго способа записать в этот слой не заводим, —
// а значит и правила у переноса те же: без выбранного типа не уходит ничего.
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
// на сервере, где `_kind_field` отказывает и отсутствующему ключу. Уберём
// пустой пункт — список возьмёт первый настоящий, и пользователь, не тронувший
// его, запишет «о собеседнике», ничего не выбрав: сервер за него не выбирает, а
// форма выбрала бы. День про явный выбор, и выбор обязан быть нажатием
// человека в обоих местах.
// Список типов — параметром: формы две, а правило одно, и вторая копия
// правила разошлась бы с первой на первой же правке.
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

// ──────────────────────── состояние задачи ────────────────────
//
// Этап задачи, текущий шаг и ожидаемое действие. Этап переключает человек —
// кнопкой полосы в шапке чата, — а два поля правит во вкладке «Задача».
// Выводить состояние из разговора агент не вправе: довод тот же, по которому
// он не пишет ни в один слой памяти.
//
// Запроса за ним нет: состояние приезжает с самим чатом (`GET
// /api/agents/{id}`), потому что полоса стоит в шапке и рисуется тем же
// ответом, что и лента. Второй источник того же числа разошёлся бы с первым.

// Этапы: токен для сервера и русская подпись. Одна карта на полосу и на
// строку врезки — слова те же, что в `TASK_STAGE_LABELS` на сервере, ими же
// этап подписан в промпте. Порядок тот же, в каком задача их проходит.
const TASK_STAGES = [
  ["planning", "планирование"],
  ["execution", "работа"],
  ["validation", "проверка"],
  ["done", "готово"],
];

// Два поля свободного текста — тем же списком, что `TASK_TEXT_FIELDS`
// на сервере, и в том же порядке: им же собраны строки врезки. Подписи —
// те же, какими строки подписаны в промпте (`TASK_LABELS`): по ним строка
// под полосой и узнаёт своё поле, а второй таблицей они разъехались бы.
const TASK_TEXT_FIELDS = ["step", "expecting"];
const TASK_LINE_LABELS = { step: "сейчас", expecting: "ожидается" };

const TASK_DEFAULT_STAGE = TASK_STAGES[0][0];

// Состояние открытого чата — или умолчание: этап у задачи есть всегда,
// и чат, которого сервер ещё не прислал, стоит на первом этапе, а не
// ни на каком.
function taskOf(agent) {
  const task = (agent && agent.task) || {};
  return {
    stage: task.stage || TASK_DEFAULT_STAGE,
    step: task.step || "",
    expecting: task.expecting || "",
    // Строки блока задачи — те самые, что уезжают в промпт, и собраны они
    // сервером. Под полосой стоит ровно то, что **видит модель**: собери их
    // здесь сами — и зашитые умолчания этапов стали бы второй таблицей,
    // а на экране и в промпте оказались бы два разных текста.
    lines: task.lines || [],
    // Куда с этого этапа можно уйти — списком с сервера, по его карте
    // переходов. Считать достижимость здесь нельзя: карта одна, и вторая
    // её копия на клиенте разошлась бы с первой молча. Не прислали — значит
    // некуда, и полоса приглушит всё, кроме текущего.
    moves: task.moves || [],
  };
}

function taskStatus(text, isError) {
  const box = $("#task-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

// Полоса этапов — **показ**, а не пульт: она говорит, где задача стоит
// и куда с этого места можно уйти. Нажатием этап не меняется и меняться
// не должен: у каждого перехода ровно один механизм, и ни один из них
// не начинается со щелчка по полосе.
//
// Текущий выделен, достижимые по карте видны обычными, недостижимые
// приглушены — запрет виден **до** того, как его попытались нарушить,
// а не строкой отказа после. Карту спрашивает сервер и присылает ответ
// полем `moves`.
//
// Кружок — часть подписи, а не отдельный узел: из сети клиент ничего
// не тянет, и одному символу незачем становиться картинкой.
function renderStages(agent) {
  const box = $("#stages");
  box.innerHTML = "";
  const task = taskOf(agent);
  const current = task.stage;
  TASK_STAGES.forEach(([token, label], i) => {
    if (i) box.appendChild(el("span", "stage-arrow", "──▶"));
    const reachable = token === current || task.moves.indexOf(token) >= 0;
    const mark = el(
      "span",
      "stage" + (token === current ? " current" : (reachable ? "" : " far")),
      (token === current ? "● " : "○ ") + label
    );
    mark.dataset.stage = token;
    mark.title = token === current
      ? "Задача сейчас здесь"
      : (reachable ? "Отсюда сюда перейти можно" : "Отсюда сюда перейти нельзя");
    box.appendChild(mark);
  });

  // Единственная дверь из планирования в работу — и единственный переход,
  // который делает человек. Инструмент по этому ребру не ходит намеренно:
  // «план утверждён» — решение человека, и выводить его из текста разговора
  // нельзя. Кнопка стоит только на планировании: на остальных этапах
  // утверждать нечего.
  //
  // И только тогда, когда в чате есть хотя бы один ответ модели: утверждать
  // нечего, пока плана не предложили, а кнопка в пустом чате обещала бы
  // решение, которого человек ещё не принимал.
  if (current === TASK_DEFAULT_STAGE && hasAnswer(agent)) {
    const ok = el("button", "stage-approve", "Утвердить план");
    ok.type = "button";
    ok.id = "approve-plan";
    ok.title = "План утверждён: задача переходит в работу, агент приступает";
    ok.onclick = () => approvePlan();
    box.appendChild(ok);
  }

  // Под полосой — то же, что уедет в блок задачи: чем заняты, чего ждут
  // и что велено модели на этом этапе. Первая строка здесь пропущена —
  // это сам этап, и он нарисован полосой выше. Строки собрал сервер:
  // под полосой обязано стоять ровно то, что видит модель.
  //
  // Два поля из них правятся щелчком: «сейчас» и «ожидается» вписывает
  // человек, а инструкция этапа зашита и человеком не правится вовсе —
  // она про то, как вести себя на этапе, а не про эту задачу.
  const note = $("#stage-note");
  note.innerHTML = "";
  task.lines.slice(1).forEach((line, i) => {
    if (i) note.appendChild(el("span", "stage-dot", " · "));
    const name = TASK_TEXT_FIELDS.find((f) => line.startsWith(TASK_LINE_LABELS[f] + ": "));
    const row = el("span", "stage-line" + (name ? " editable" : ""));
    row.appendChild(el("span", "stage-line-text", line));
    if (name) {
      row.title = "Щёлкните, чтобы поправить";
      row.dataset.field = name;
      // Щелчок по уже открытой правке — не вторая правка: поле лежит
      // внутри той же строки, и его клик всплывает сюда.
      row.onclick = () => {
        if (!row.querySelector(".stage-edit")) startStageEdit(row, name, task[name]);
      };
    }
    note.appendChild(row);
  });

  renderTaskLog(agent);
}

// Записанное состояние кладётся и в открытый чат, и в его запись в списке
// слева: полоса рисуется по `state.current`, а список переживает открытие
// соседнего чата. Показываем **записанное** из ответа ручки, а не набранное:
// текст по дороге чистит `redact()`.
function applyTask(task) {
  if (state.current) state.current.task = task;
  const row = state.agents.find((a) => state.current && a.id === state.current.id);
  if (row) row.task = task;
  // Полоса, строки под ней и журнал — одной отрисовкой: под полосой стоит
  // то же состояние, и рисовать его двумя проходами значило бы заводить
  // второе место, где оно может отстать.
  renderStages(state.current);
}

// Есть ли в чате хотя бы один ответ модели. По стенограмме, а не по длине
// истории: половина её — вопросы человека, а утверждать нечего, пока плана
// не предложили.
const hasAnswer = (agent) =>
  ((agent && agent.transcript) || []).some((turn) => turn.role === "assistant");

// Утверждение плана — единственная правка этапа, которую шлёт клиент.
// Полоса на переходы не жмёт вовсе: у каждого перехода ровно один
// механизм, и три из четырёх принадлежат инструменту модели.
//
// И кнопка **сама отправляет обмен**: человек утвердил план — значит,
// дальше работают, и требовать от него ещё и написать «делай» значило бы
// просить подтверждение дважды. Агент получает вопрос уже на новом этапе,
// с его инструкцией в блоке задачи.
async function approvePlan() {
  if (!(state.current && state.current.id)) return;
  try {
    const answer = await api(taskUrl() + "/approve", { method: "POST" });
    applyTask(answer.task);
    taskStatus("План утверждён: задача в работе.");
  } catch (err) {
    taskStatus(String(err.message || err), true);
    return;
  }
  if (!state.busy && state.hasKey) {
    await exchange(
      "/api/agents/" + state.current.id + "/messages",
      { text: APPROVED_TEXT },
      APPROVED_TEXT
    );
  }
}

// Чем кнопка отправляет обмен. Обычная реплика человека, а не тайный
// сигнал: в ленте она стоит как всё остальное, в историю ложится как всё
// остальное, и модель читает её ровно так же.
const APPROVED_TEXT =
  "План утверждён. Выполни первый шаг плана прямо сейчас "
  + "и покажи результат работы, а не намерение её начать.";

// Журнал этапа: кто, когда и куда увёл задачу — и увёл ли. Отклонённые
// попытки стоят здесь наравне с удавшимися: строка «агент просил „готово“ —
// отклонено» это доказательство, что автомат работает. Строк «остались там
// же» здесь не бывает вовсе — их не пишет и сервер.
const TASK_WHO = { agent: "агент", human: "человек" };

const clock = (at) => {
  const d = new Date((at || 0) * 1000);
  const two = (n) => String(n).padStart(2, "0");
  return two(d.getHours()) + ":" + two(d.getMinutes());
};

function renderTaskLog(agent) {
  const box = $("#task-log");
  const toggle = $("#task-log-toggle");
  if (!box || !toggle) return;
  box.innerHTML = "";
  const moves = (agent && agent.task_log) || [];
  // Переходов не было — прятать нечего и разворачивать нечего.
  $("#task-moves").classList.toggle("hidden", !moves.length);
  if (!moves.length) return;

  // Свёрнутой строкой: журнал это летопись, а не состояние, и разворачивают
  // её, когда спрашивают. В шапке чата место дорогое — там стоит то, где
  // задача **сейчас**.
  toggle.textContent = "переходов: " + moves.length;
  // Развёрнут ли журнал, помнит сам список, а не отдельное поле состояния:
  // перерисовка идёт на каждый обмен, и второе место для того же ответа
  // разъехалось бы с первым — журнал схлопывался бы под рукой.
  const open = !box.classList.contains("hidden");
  toggle.setAttribute("aria-expanded", String(open));
  toggle.onclick = () => {
    const now = box.classList.contains("hidden");
    box.classList.toggle("hidden", !now);
    toggle.setAttribute("aria-expanded", String(now));
  };

  moves.forEach((m) => {
    const row = el("div", "move" + (m.ok ? "" : " denied"));
    row.append(
      el("span", "move-path", stageLabel(m.stage_from) + " → " + stageLabel(m.stage_to)),
      el("span", "move-who", TASK_WHO[m.who] || m.who),
      // Отклонённая попытка названа отклонённой и стоит рядом с удавшимися:
      // журнал обязан показывать не только то, что переходы случаются, но
      // и то, что запрещённые не случаются.
      el("span", "move-ok", m.ok ? "" : "отклонено"),
      el("span", "move-at", clock(m.at))
    );
    box.appendChild(row);
  });
}

const stageLabel = (stage) =>
  (TASK_STAGES.find(([token]) => token === stage) || [stage, stage])[1];

const taskUrl = () => "/api/agents/" + (state.current && state.current.id) + "/task";

// Правка одного поля: уезжает **только тронутое**, остальные не называются
// вовсе — иначе вторая вкладка, правящая «ожидается», затирала бы шаг,
// набранный в первой. Пустая строка поле снимает, и тогда оно не едет
// во врезку вовсе. Довод и форма те же, что у профиля.
async function saveTask(name, value) {
  if (!(state.current && state.current.id)) return;
  try {
    const answer = await api(taskUrl(), json("PATCH", { [name]: value }));
    applyTask(answer.task);
    taskStatus("Состояние задачи сохранено: оно уезжает в блок задачи каждого запроса.");
  } catch (err) {
    taskStatus(String(err.message || err), true);
  }
}

// Правка строки под полосой — тем же приёмом, каким правятся записи памяти:
// поле встаёт на место строки, Enter и уход фокуса сохраняют, Escape
// отменяет, текст слово в слово прежний не шлёт ничего, замок не даёт одной
// правке уехать дважды. Второго способа не заводить: правится здесь то же
// самое, что и там, — строка текста.
function startStageEdit(row, name, was) {
  const input = el("input", "stage-edit");
  input.value = was;
  row.replaceChild(input, row.querySelector(".stage-line-text"));
  input.focus();
  input.select();

  let settled = false;
  const finish = (save) => {
    if (settled) return;
    settled = true;
    const text = (input.value || "").trim();
    if (save && text !== was) saveTask(name, text);
    else renderStages(state.current);
  };
  input.onkeydown = (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); finish(false); }
  };
  input.onblur = () => finish(true);
}

// ──────────────────────── журнал работы ───────────────────────
//
// Что в этом чате происходило: обмен за обменом, сверху вниз по времени.
// Не то же, что журнал переходов под полосой, — тот летопись **состояния**,
// одна строка на переход, и живёт он в базе. Этот про **работу**: вопрос,
// сворачивание, вызов инструмента и его исход, числа ответа, ошибки
// и отмены.
//
// Копится он в памяти вкладки, ровно как промпт обмена (`state.prompts`),
// и по тому же доводу: всё это производные событий, которые клиент и так
// получает потоком, — ни колонки, ни таблицы под них нет и заводить нечего.
// Отсюда и честная строка во вкладке: показано происходившее **с момента
// открытия страницы**. Кнопки очистки нет — её роль играет перезагрузка.
//
// Ключ — чат: журнал показывает работу **открытого** чата, а не общий
// поток. Сложи их в один список, и переключение чата выдавало бы чужие
// обмены за свои.
const logTabOpen = () => !$("#tab-log").classList.contains("hidden");

// Время события — с секундами, в отличие от журнала переходов: там строка
// на переход и их за задачу три, здесь несколько строк на один обмен, и
// без секунд они слиплись бы в одну минуту.
const logClock = (at) => {
  const d = new Date((at || 0) * 1000);
  const two = (n) => String(n).padStart(2, "0");
  return two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
};

// Запись в журнал. Возвращает саму запись: у сворачивания подробность
// становится известна позже самого события — сколько сообщений свёрнуто,
// говорят числа ответа, — и дописывается она в ту же строку, а не второй.
//
// `body` — **дословное** содержимое шага: сообщения запроса, сырой ответ,
// аргументы вызова. Строка показывает подпись, тело раскрывается по клику:
// покажи его сразу — вкладка утонула бы на первом же обмене.
function logEvent(agentId, what, detail, bad, body) {
  if (!agentId) return null;
  const entry = {
    at: Date.now() / 1000,
    what,
    detail: detail || "",
    bad: Boolean(bad),
    body: body || "",
    open: false,
  };
  const list = state.log.get(agentId) || [];
  list.push(entry);
  state.log.set(agentId, list);
  // Рисуем только при открытой вкладке: закрытой отрисовка не нужна, а
  // событий на обмен приходит много.
  if (logTabOpen()) renderLog();
  return entry;
}

function renderLog() {
  const box = $("#log-list");
  if (!box) return;
  box.innerHTML = "";
  const list = (state.current && state.log.get(state.current.id)) || [];
  if (!list.length) {
    box.appendChild(el("div", "log-empty", "Пока ничего не происходило."));
    return;
  }
  // Самое свежее внизу — порядок записи и есть порядок показа.
  list.forEach((entry) => {
    const item = el("div", "log-item");
    const row = el(
      "div",
      "log-row" + (entry.bad ? " bad" : "") + (entry.body ? " has-body" : "")
    );
    row.append(
      el("span", "log-at", logClock(entry.at)),
      el("span", "log-what", entry.what),
      el("span", "log-detail", entry.detail)
    );
    item.appendChild(row);
    if (entry.body) {
      // Раскрытое держится на самой записи, а не на разметке: журнал
      // перерисовывается на каждое событие, и раскрытое тело схлопывалось бы
      // под руками читателя на следующей же строке.
      row.onclick = () => { entry.open = !entry.open; renderLog(); };
      const body = el("pre", "log-body", entry.body);
      body.classList.toggle("hidden", !entry.open);
      item.appendChild(body);
    }
    box.appendChild(item);
  });
  box.scrollTop = box.scrollHeight;
}

// Сообщения запроса дословно: роль и текст, как уехало. Вызов инструмента
// в сообщении модели — тем же json, каким он и приехал: журнал показывает
// содержимое, а не его пересказ.
function promptText(messages) {
  return (messages || [])
    .map((m) => {
      const calls = m.tool_calls ? "\n" + JSON.stringify(m.tool_calls) : "";
      return "[" + m.role + "]\n" + (m.content || "") + calls;
    })
    .join("\n\n");
}

// Отладочный кадр — строкой журнала. Тексты в нём собрал сервер, клиент
// только подписывает строку и складывает тело: сочини он довод отказа сам,
// на клиенте завелась бы вторая карта переходов.
function logDebug(agentId, e) {
  if (e.step === "compress_request") {
    logEvent(agentId, "сворачивание: запрос", "сообщений: " + (e.messages || []).length,
      false, promptText(e.messages));
  } else if (e.step === "compress_reply") {
    logEvent(agentId, "сворачивание: пересказ", oneLine(e.text, 90), false, e.text);
  } else if (e.step === "tool") {
    logToolCall(agentId, e);
  } else if (e.step === "continue") {
    logEvent(agentId, "продолжение", "сообщений: " + (e.messages || []).length,
      false, promptText(e.messages));
  }
}

// Вызов инструмента: что просила модель, что решил код и что ему ответили.
// Одной строкой, а не тремя: работа была одна, и разложенная на три строки
// она читалась бы как три разных события.
//
// Исходов четыре, и сюда доезжают все четыре — в отличие от журнала
// переходов, где «этап уже такой» и «нет такого этапа» переходами не были
// и строки не завели. Довод отказа берётся из `note`, собранной сервером.
function logToolCall(agentId, e) {
  const asked = e.stage_to
    ? "просил «" + stageLabel(e.stage_to) + "»"
    : "этап не назван";
  const why = e.note && e.note.indexOf(": ") >= 0
    ? e.note.slice(e.note.indexOf(": ") + 2)
    : "";
  const decided = {
    moved: "применено: " + stageLabel(e.stage_from) + " → " + stageLabel(e.stage_to),
    denied: "отклонено" + (why ? ": " + why : ""),
    same: "этап уже такой",
    unknown: "отклонено: такого этапа нет",
  }[e.outcome] || e.outcome;
  const body = [
    "инструмент: " + e.name,
    "аргументы: " + e.arguments,
    "этап был: " + stageLabel(e.stage_from),
    "решение кода: " + decided,
    "ответ инструмента модели: " + e.reply,
  ]
    .concat(e.note ? ["строка под ответом: " + e.note] : [])
    .join("\n");
  logEvent(agentId, "вызов инструмента", asked + " — " + decided,
    e.outcome !== "moved" && e.outcome !== "same", body);
}

// Вопрос одной строкой: журнал это лента событий, а не вторая копия
// переписки, и абзац в ней сломал бы столбик времени.
function oneLine(text, limit) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > limit ? flat.slice(0, limit - 1) + "…" : flat;
}

// Строка про переход, сделанный **человеком**: кнопка утверждения плана
// ходит мимо обмена, отладочного кадра за ней нет, и узнать о ней можно
// только по новой строке журнала переходов.
//
// Переходы модели сюда не идут: их строку заводит кадр `debug`, и там она
// богаче — с аргументами вызова и ответом инструмента. Две строки об одной
// работе читались бы как две работы.
//
// Отказ называет **причину**, и берётся она из строки, собранной сервером
// (`stage_note`), а не сочиняется здесь: карта переходов одна, и вторая её
// копия на клиенте разошлась бы с первой молча. Отказов на обмен бывает
// несколько, и склеены они точкой с запятой — свой ищем по названному
// в нём этапу.
function logMove(agentId, move, metrics) {
  const label = stageLabel(move.stage_to);
  const path = stageLabel(move.stage_from) + " → " + label;
  const what = move.who === "agent" ? "вызов инструмента" : "переход вручную";
  if (move.ok) {
    logEvent(agentId, what, "просил «" + label + "» — применено: " + path);
    return;
  }
  const note = String((metrics && metrics.stage_note) || "")
    .split("; ")
    .find((one) => one.indexOf("«" + label + "»") >= 0) || "";
  const why = note.indexOf(": ") >= 0 ? note.slice(note.indexOf(": ") + 2) : "";
  logEvent(agentId, what, "просил «" + label + "» — отклонено" + (why ? ": " + why : ""), true);
}

// Числа ответа — те же, что в строке под ним, и теми же помощниками:
// вторым форматированием они разошлись бы с лентой молча.
function logAnswerDetail(m) {
  const parts = [];
  if (m && has(m.prompt_tokens)) parts.push("вход " + fmt.tokens(m.prompt_tokens));
  if (m && has(m.completion_tokens)) parts.push("выход " + fmt.tokens(m.completion_tokens));
  if (m && has(m.elapsed_ms)) parts.push(fmt.sec(m.elapsed_ms) + " с");
  return parts.join(" · ");
}

// ─────────────────────────── профиль ──────────────────────────
//
// Профиль — про то, **как** с человеком разговаривать: стиль, формат
// и контекст его работы. Он один на всю базу, как долговременная память,
// и пишет в него только человек: профиль это распоряжение («отвечай кратко»),
// а не наблюдение о собеседнике («пишет на Kotlin») — выводить распоряжения
// из разговора агент не вправе.
//
// Запрашивается лениво, на открытие вкладки, ровно как слои памяти: панель
// перерисовывается на каждый обмен, и запрос внутри отрисовки превратил бы
// один поход на сервер в поток.

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

// Правка одного поля: уезжает **только тронутое**, остальные не называются
// вовсе — иначе вторая вкладка, правящая формат, затирала бы стиль, набранный
// в первой. Пустая строка поле снимает: отдельной кнопки «очистить» нет,
// ровно как у системного промпта чата.
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

// Плитки справа — про весь диалог, а не про последний ответ: сколько всего
// ушло в модель, сколько она вернула, во что это обошлось и сколько было
// сообщений. Числа одного обмена написаны под ним самим в ленте, и подписей
// «накопленное» здесь больше нет — в панели теперь всё и так про разговор.
//
// Плиток шесть, сетка 2×3: пустых клеток в последнем ряду не остаётся.
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
    // и PATCH чата при выборе типа записи или правке стиля был бы запросом
    // ни о чём. Профиль при этом сохраняется тем же событием `change`,
    // что и настройки: у поля ввода это потеря фокуса, отдельной кнопки
    // сохранения нет нигде в панели.
    const id = String(ev.target.id || "");
    if (id.startsWith("profile-")) return saveProfile(id.slice("profile-".length));
    // Поля задачи — тем же событием и по той же причине: у состояния своя
    // ручка, и PATCH чата при правке текущего шага был бы запросом ни о чём.
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
      // вкладки. Отрисовка своего запроса не делает — см. `loadMemory`.
      // Открытие вкладки — ещё и отметка «показано»: счётчик новых записей
      // с этой минуты считает заново. Обмен и смена чата читают слои без
      // отметки, иначе считать было бы нечего.
      if (which === "memory") loadMemory();
      // Профиль — тем же порядком и по тому же доводу: лениво, на открытие
      // вкладки. Он глобальный, и перечитывать его на смену чата незачем.
      if (which === "profile") loadProfile();
      // Журнал ничего не запрашивает: он уже в памяти вкладки. Рисуем его
      // на открытие — при закрытой вкладке отрисовка молчит, а событий
      // на обмен приходит много.
      if (which === "log") renderLog();
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
  // Полоса рисуется сразу, ещё до первого ответа сервера: у чата, которого
  // нет, она показывает первый этап — тот же, с которого начнётся любая
  // задача, — и не мигает пустым местом на открытии.
  renderStages(null);
  renderMemory();
  renderLog();
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
