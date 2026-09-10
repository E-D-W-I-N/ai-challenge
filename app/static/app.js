"use strict";

// Чат-клиент. Историю диалога хранит агент на сервере: клиент держит
// только id открытого чата и шлёт новый текст.
//
// Из сети не тянется ничего — ни шрифтов, ни библиотек, ни иконок: репозиторий
// публичный и обязан работать без интернета. Иконки — inline SVG ниже,
// markdown разбирается своими силами в renderMarkdown().

const state = {
  agents: [],          // всё, что вернул GET /api/agents
  hasKey: false,
  current: null,       // открытый агент (полный ответ GET /api/agents/{id})
  models: [],          // каталог моделей для дропдауна
  busy: false,
  abort: null,         // AbortController активного потока
  lastMetrics: null,   // метрики последнего ответа — из них плитка «Контекст»
  tab: "model",
  applying: null,      // незавершённое применение настроек панели
  panelDirty: false,   // правка панели не доехала до агента
  stick: true,         // лента примотана к низу — доматывать новые ответы
  baseModel: "",       // модель, с которой чат открыли: с ней сверяем смену
  contextStale: false, // модель сменили — прежняя доля окна к новой не относится
  statusTimer: null,   // таймер, гасящий строку состояния
};

const $ = (sel) => document.querySelector(sel);

// Сколько пикселей от низа ленты ещё считается «читатель внизу».
const STICK_SLACK = 80;

// Через сколько гаснет «Применено — со следующего сообщения».
const STATUS_FADE_MS = 5000;

// ─────────────────────────── иконки ───────────────────────────

// Минимальный набор, нарисованный путями: CDN нам недоступен, а картинки
// в кадре нужны — по ним читается, что делает кнопка.
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
};

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

// Кнопка с иконкой. `className` разводит два размера: крупные кнопки шапок
// и мелкие в строке списка.
function iconButton(name, title, onClick, className = "icon-btn") {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
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

// Итог по чату приходит с сервера полем `usage_total` — считает его агент,
// клиент только показывает. Здесь не складывается ни одного слагаемого,
// и досчитать «всего» вместо смолчавшего провайдера клиент тоже не вправе:
// два места, где числа считаются, разъезжаются молча.
function chatUsage() {
  return (state.current && state.current.usage_total) || null;
}

// Поле итога по чату. Итога нет вовсе — это `null`, то есть прочерк в плитке,
// а не ноль: в чате, где не было ни одного ответа с числами, ноль был бы враньём.
function totalField(name) {
  const total = chatUsage();
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
// Метрики нового ответа. Пометку «модель сменили» снимает **только** эта
// функция: с новыми метриками приходит и свежая доля окна, и разбросать
// снятие по веткам потока значило бы получить путь, на котором плитка
// осталась бы погашенной навсегда.
function keepMetrics(metrics) {
  state.lastMetrics = metrics;
  state.contextStale = false;
}

function contextFill() {
  if (state.contextStale) return null;
  const m = state.lastMetrics;
  if (!m || m.context_fill_pct === null || m.context_fill_pct === undefined) return null;
  return m.context_fill_pct;
}

// ─────────────────────────── markdown ─────────────────────────

// Свой разбор: заголовки, списки, цитаты, код, жирный, курсив, ссылки.
// Текст модели сначала экранируется целиком, поэтому разметка из ответа
// не может стать разметкой страницы.
function escapeHtml(text) {
  return String(text)
    // Меткой inline-кода служит \u0000, и если модель пришлёт его в тексте,
    // разбор подставил бы на его место чужой кусок. В рендере такому символу
    // делать нечего — выбрасываем до всего остального.
    .replace(/\u0000/g, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Метка, которой на время разбора подменяется inline-код: внутри кода
// разметка не разбирается, а в тексте от модели такого символа не бывает.
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
  return res.status === 204 ? null : res.json();
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
  // Пустой список — это пустой экран, в который нечего написать. Заводим
  // чистый чат сами: список начинается пустым, и удалить последний чат
  // тоже можно, а писать пользователю надо куда-то сразу.
  if (!state.agents.length) {
    const created = await api("/api/agents", json("POST", {}));
    state.agents = created.agents;
    return openAgent(created.agents[0].id);
  }
  renderList();
  const wanted = selectId || (state.current && state.current.id);
  const exists = state.agents.some((a) => a.id === wanted);
  await openAgent(exists ? wanted : (state.agents[0] || {}).id);
}

function renderList() {
  const box = $("#agent-list");
  box.innerHTML = "";
  // Список плоский: все чаты равны, никаких групп и разделов.
  state.agents.forEach((agent) => box.appendChild(listItem(agent)));
}

function listItem(agent) {
  const row = document.createElement("div");
  const active = state.current && agent.id === state.current.id;
  row.className = "item" + (active ? " active" : "");

  const open = document.createElement("button");
  open.type = "button";
  open.className = "item-open";
  const ico = document.createElement("span");
  ico.className = "item-icon";
  ico.appendChild(icon("chat"));
  const title = document.createElement("span");
  title.className = "item-title";
  title.textContent = agent.label;
  open.append(ico, title);
  open.title = agent.label + "\n" + agent.model;
  open.onclick = () => openAgent(agent.id);
  row.appendChild(open);

  const actions = document.createElement("div");
  actions.className = "item-actions";
  actions.append(
    iconButton("pencil", "Переименовать",
      (ev) => { ev.stopPropagation(); startRename(row, agent); }, "mini"),
    iconButton("trash", "Удалить чат",
      (ev) => { ev.stopPropagation(); askDelete(agent); }, "mini danger")
  );
  row.appendChild(actions);
  return row;
}

// Переименование прямо в списке: Enter сохраняет, Escape отменяет,
// потеря фокуса — тоже сохраняет, чтобы имя не терялось молча.
function startRename(row, agent) {
  const open = row.querySelector(".item-open");
  const input = document.createElement("input");
  input.className = "item-rename";
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
  // Чат открывают, чтобы увидеть последнее сообщение. Отмотанная лента
  // прошлого чата к новому отношения не имеет: иначе, отмотав один раз,
  // читатель выключил бы доматывание сразу для всех чатов.
  state.stick = true;
  // Чат открыт заново: доля окна относится к той модели, что у него сейчас.
  keepMetrics(lastAnswerMetrics(agent));
  renderList();
  renderFeed(agent);
  fillPanel(agent);
  renderTiles();

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
  // где читатель остановился. Оставить на усмотрение браузера нельзя —
  // он оставит ноль, то есть выбросит читателя в начало разговора.
  const keep = state.stick ? null : feed.scrollTop;
  feed.innerHTML = "";

  const turns = agent.transcript;
  if (!turns.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    const h = document.createElement("h2");
    h.textContent = agent.label;
    const p = document.createElement("p");
    p.textContent = "Напишите сообщение — историю разговора хранит сервер, а не браузер.";
    empty.append(h, p);
    feed.appendChild(empty);
    feed.scrollTop = 0;
    return;
  }

  turns.forEach((turn) => {
    feed.appendChild(turn.role === "user" ? userBubble(turn.content) : answerCard(agent, turn));
  });
  feed.scrollTop = keep === null ? feed.scrollHeight : keep;
}

function userBubble(text) {
  const el = document.createElement("div");
  el.className = "msg-user";
  el.textContent = text;
  return el;
}

// Шапка карточки ответа: иконка и имя модели. Одна на готовый ответ и на
// карточку, в которую ещё стримят. Провайдер стоит не здесь, а строкой под
// ответом, рядом со скоростью: это всё про то, как прошёл этот обмен, и жить
// оно должно в одном месте, а не в двух.
function cardHead(modelName) {
  const head = document.createElement("header");
  head.className = "card-head";
  const ico = document.createElement("span");
  ico.className = "card-icon";
  ico.appendChild(icon("bot"));
  const name = document.createElement("span");
  name.className = "card-model";
  name.textContent = modelName;
  head.append(ico, name);
  return head;
}

function answerCard(agent, turn) {
  const card = document.createElement("article");
  card.className = "card" + (turn.error ? " failed" : "");
  const head = cardHead((turn.metrics && turn.metrics.model) || agent.model);

  const actions = document.createElement("div");
  actions.className = "card-actions";
  actions.append(
    // Кнопки «метрики этого ответа» здесь больше нет: числа обмена написаны
    // под ним самим, а перекладывать их в плитки значило бы показывать
    // в панели то, что там больше не живёт, — она про весь диалог.
    iconButton("copy", "Копировать ответ", () => copyText(turn.content)),
    iconButton("refresh", "Перегенерировать", () => regenerate()),
    iconButton("dots", "Показать сырой текст", () => showRaw(card, turn))
  );
  head.appendChild(actions);
  card.appendChild(head);

  if (turn.reasoning) card.appendChild(thinkingBlock(turn.reasoning));

  const body = document.createElement("div");
  body.className = "card-body md";
  body.innerHTML = renderMarkdown(turn.content);
  card.appendChild(body);

  const usage = usageLine(turn);
  if (usage) card.appendChild(usage);

  if (turn.error) {
    const err = document.createElement("div");
    err.className = "card-error";
    err.textContent = turn.error;
    card.appendChild(err);
  }
  return card;
}

// Что под ответом: всё про этот обмен и только про него. Первой строкой —
// сколько токенов ушло в модель, сколько она вернула, сколько вышло вместе
// и во что обошлось; второй — как быстро отвечала и кто отвечал.
//
// Итог по всему диалогу живёт в плитках справа, и одно и то же число нигде
// не показывается дважды: лента про обмен, панель про разговор.
//
// Пропущенное поле не пишется вовсе: «входные 0» вместо «неизвестно» было бы
// неправдой, а сумму, о которой смолчал провайдер, досчитывает сервер (см.
// `_apply_usage` в `app/llm.py`) — сложи её здесь, и «всего» под ответом
// разошлось бы с «Всего токенов» в плитке. Строки нет, только если чисел нет
// совсем. У оборванного ответа она есть: его числа идут в итог чата, он оплачен.
function usageLine(turn) {
  const m = turn.metrics;
  if (!m) return null;
  const known = (v) => v !== null && v !== undefined;

  const tokens = [];
  if (known(m.prompt_tokens)) tokens.push("входные " + fmt.tokens(m.prompt_tokens));
  if (known(m.completion_tokens)) {
    // Токены рассуждения провайдер кладёт **внутрь** completion_tokens: на
    // думающей модели выход заметно больше видимого текста. Поэтому они
    // названы отдельным числом, а не вычтены молча: вычитание сделало бы
    // «выход» не тем, что прислал провайдер.
    const think = m.reasoning_tokens
      ? " (из них " + fmt.tokens(m.reasoning_tokens) + " рассуждение)"
      : "";
    tokens.push("выходные " + fmt.tokens(m.completion_tokens) + think);
  }
  if (known(m.total_tokens)) tokens.push("всего " + fmt.tokens(m.total_tokens));
  if (known(m.cost_usd)) tokens.push(fmt.cost(m.cost_usd));

  const how = [];
  if (m.tokens_per_second) {
    how.push(
      fmt.rate(m.tokens_per_second) + " ток/с" +
      (m.elapsed_ms ? " за " + fmt.sec(m.elapsed_ms) + " с" : "")
    );
  }
  // Первый токен — честный: на думающей модели это момент, когда модель
  // заговорила вообще, а не когда домыслила и пошёл ответ.
  const first = known(m.first_token_ms) ? m.first_token_ms : m.ttft_ms;
  if (known(first)) how.push("первый токен " + fmt.sec(first) + " с");
  if (m.provider) how.push(m.provider);

  if (!tokens.length && !how.length) return null;
  const box = document.createElement("div");
  box.className = "card-usage";
  if (tokens.length) box.appendChild(usageRow("usage-tokens", tokens));
  if (how.length) box.appendChild(usageRow("usage-how", how));
  return box;
}

function usageRow(className, parts) {
  const row = document.createElement("div");
  row.className = className;
  row.textContent = parts.join(" · ");
  return row;
}

function thinkingBlock(text) {
  const el = document.createElement("details");
  el.className = "think";
  const summary = document.createElement("summary");
  const ico = document.createElement("span");
  ico.className = "card-icon";
  ico.appendChild(icon("clock"));
  const label = document.createElement("span");
  label.textContent = "Рассуждение";
  summary.append(ico, label);
  const body = document.createElement("div");
  body.className = "think-body";
  body.textContent = text;
  el.append(summary, body);
  return el;
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

function scrollFeed(force) {
  const feed = $("#feed");
  if (force) state.stick = true;
  if (!state.stick) return;
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
  // всякая отправка, и третий путь к нему не сможет его обойти. Держать
  // свойство дисциплиной двух вызывающих — значит ждать, пока появится
  // третий и молча откроет дыру заново.
  if (state.applying) await state.applying;
  if (!(await ensurePanelApplied())) {
    hint("Настройки панели не применились — сообщение не отправлено.", true);
    return false;
  }
  // Текст забираем из поля только теперь: до этой строки отправка могла
  // не состояться, и стирать написанное было бы не за что.
  if (questionText !== null) {
    const input = $("#input");
    input.value = "";
    autoGrow(input);
  }

  // Обмен затевает сам читатель — и своим сообщением, и повтором ответа:
  // ленту к низу, чтобы увидеть, что из этого вышло.
  state.stick = true;
  if (questionText !== null) {
    if (feed.querySelector(".empty")) feed.innerHTML = "";
    feed.appendChild(userBubble(questionText));
  } else {
    // Перегенерация заменяет последний ответ: карточку убираем с экрана,
    // а на сервере пара «вопрос — ответ» снимается с истории тем же запросом.
    const cards = feed.querySelectorAll(".card");
    if (cards.length) cards[cards.length - 1].remove();
  }

  const card = document.createElement("article");
  card.className = "card busy";
  const head = cardHead(agent.model);
  const bodyEl = document.createElement("div");
  bodyEl.className = "card-body md";
  card.append(head, bodyEl);
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

  try {
    await streamPost(
      path,
      body,
      (e) => {
        switch (e.event) {
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
            if (e.metrics) keepMetrics(e.metrics);
            break;
          case "done":
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

  card.classList.remove("busy");
  state.abort = null;
  setBusy(false);

  if (failure) {
    card.classList.add("failed");
    const err = document.createElement("div");
    err.className = "card-error";
    err.textContent = failure;
    card.appendChild(err);
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
  await refreshCurrent();
}

async function refreshCurrent() {
  if (!state.current) return;
  try {
    const fresh = await api("/api/agents/" + state.current.id);
    state.current = fresh;
    const listed = state.agents.find((a) => a.id === fresh.id);
    if (listed) { listed.history_len = fresh.history_len; listed.label = fresh.label; }
    renderList();
    renderFeed(fresh);
    // Итог по чату и число обменов приехали вместе с агентом: плитки
    // перерисовываем, иначе панель отстаёт на один обмен.
    renderTiles();
    // Панель намеренно не перерисовываем: пользователь мог печатать в ней
    // прямо сейчас, и затирать его текст ответом сервера нельзя.
  } catch (e) { /* чат исчез — список обновится при следующем открытии */ }
}

// ─────────────────────── панель настроек ──────────────────────

const NUMBER_FIELDS = [
  "temperature", "max_tokens", "top_p", "top_k", "min_p",
  "repetition_penalty", "presence_penalty", "frequency_penalty", "history_limit",
];

function fillPanel(agent) {
  NUMBER_FIELDS.forEach((name) => {
    const el = $("#f-" + name);
    el.value = agent[name] === null || agent[name] === undefined ? "" : String(agent[name]);
  });
  $("#f-system").value = agent.system || "";
  // Стоп-строки — по одной в строке: список строк, а не JSON руками.
  $("#f-stop").value = (agent.stop || []).join("\n");
  fillResponseFormat(agent.response_format);
  // Модель ставим сразу, не дожидаясь каталога: пока он едет, поле иначе
  // пустое, а панель — источник правды, и её пустоту нельзя пролить в агента.
  setModelOptions([{ id: agent.model }], agent.model);
  state.baseModel = agent.model;
  fillModels(agent.model).then(renderWarnings);
  saveStatus("");
}

// Формат ответа: частый случай выбирается из списка, редкий пишется JSON.
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
    const opt = document.createElement("option");
    opt.value = m.id;
    const price = m.prompt_price_per_m
      ? "  ·  $" + m.prompt_price_per_m + " / $" + m.completion_price_per_m + " за 1M"
      : "";
    opt.textContent = m.id + price;
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

// Пустое поле значит «не отправлять параметр» — сервер получает null и
// перестаёт класть параметр в тело запроса к OpenRouter.
function readNumber(name) {
  const raw = ($("#f-" + name).value || "").trim();
  if (!raw) return null;
  const value = Number(raw.replace(",", "."));
  if (!Number.isFinite(value)) throw new Error(name + ": нужно число или пусто");
  return value;
}

// Стоп-строки: по одной в строке. Пустые строки и пробелы по краям
// выбрасываются, пустое поле значит «не отправлять параметр».
// Отдельной функцией без DOM — разбор проверяется без браузера.
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

// Параметры панели в терминах OpenRouter: имена совпадают один в один,
// кроме окна памяти — оно наше и в запрос не уходит.
const PROVIDER_PARAMS = [
  "temperature",
  "max_tokens",
  "top_p",
  "top_k",
  "min_p",
  "repetition_penalty",
  "presence_penalty",
  "frequency_penalty",
  "stop",
  "response_format",
];

// Чем заданные параметры не сойдутся с выбранной моделью. Отдельной функцией
// без DOM — решение проверяется без браузера.
//
// Предупреждать надо **до** отправки: на каждом вызове стоит
// provider.require_parameters=true, и параметр, которого модель не заявляет,
// выкашивает провайдеров. Вместо ответа приходит невнятная ошибка, и по ней
// не понять, что виноват один переключатель в панели.
function paramWarnings(model, settings, extraBody, baseModel) {
  const warnings = [];

  // Часть чатов привязана к одному поставщику модели, и другая модель
  // у него, скорее всего, не обслуживается — ответ не придёт. Говорим об
  // этом ровно в тот момент, когда модель действительно меняют: постоянная
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
  // пугать предупреждением, которого не на чем основать, хуже, чем не
  // предупредить.
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

// Строка состояния под панелью гаснет сама: «Применено» — сообщение
// о событии, а не постоянная подпись, и висеть всё время ей незачем.
// Ошибка не гаснет: её надо прочитать и исправить.
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

// Одинаковы ли два значения конфига. Не `==` и не `JSON.stringify`:
// у полей панели разные типы, и каждый врёт по-своему.
//
// `null` — это «параметр не отправлять», и он не равен ни нулю, ни пустой
// строке: `provider.require_parameters` включён, и заданный `top_p: 0`
// сужает список провайдеров, а незаданный — нет. `==` их бы уравнял.
// Стоп-строки — список, формат ответа — объект; их сравнивают поэлементно,
// а не по ссылке. Порядок ключей в объекте от провайдера не гарантирован,
// поэтому `JSON.stringify` тоже не годится.
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

// Изменила ли правка хоть что-нибудь. Сравниваются только поля панели:
// в ответе ручки едет ещё и то, что живёт своей жизнью, — стенограмма,
// длина истории, занятость, — и по ним «изменилось» было бы правдой всегда.
function configChanged(before, after, fields) {
  if (!before) return true;
  return fields.some((name) => !sameValue(before[name], after[name]));
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
  };
  NUMBER_FIELDS.forEach((name) => { patch[name] = readNumber(name); });
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
  warnings.forEach((text) => {
    const line = document.createElement("p");
    line.textContent = text;
    box.appendChild(line);
  });
  box.classList.toggle("hidden", !warnings.length);
  // Открыта вкладка «Агент» — про предупреждение всё равно должно быть видно.
  tab.classList.toggle("has-warn", warnings.length > 0);
}

// Инвариант чата: **сообщение уходит только тогда, когда конфиг агента
// равен тому, что показывает панель**. Держать его на событии `change`
// нельзя: у события ровно один шанс выстрелить, а поводов его упустить
// сколько угодно — значение поставили из кода, поле не потеряло фокус,
// вкладку спрятали.
//
// Поэтому событие оставлено только ради отзывчивости, а истина проверяется
// в единственном месте, которое обойти нельзя, — прямо перед отправкой.
// Панель проливается в агента, и если пролить не вышло, сообщение не уходит:
// показать на экране одно, а послать другое хуже, чем не послать.
async function ensurePanelApplied() {
  if (!state.current) return true;
  try {
    readPanel();
  } catch (err) {
    state.panelDirty = true;
    saveStatus(String(err.message || err), true);
    return false;
  }
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
  // «Применено» появлялось бы на каждое сообщение, даже когда пользователь
  // ничего не трогал.
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
      // изменением не является.
      if (configChanged(before, updated, fields)) {
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
  // Сообщения считает сервер и считает **все** ответы, а не только принёсшие
  // числа: карточек в ленте ровно столько же. Поэтому число едет отдельным
  // полем, а не внутри сумм: у чата с молчащим usage сумм нет вовсе, а ответы
  // в нём были.
  ["Сообщений", () => fmt.tokens(state.current ? state.current.exchanges : null)],
  ["Контекст", () => fmt.pct(contextFill())],
];

function renderTiles() {
  const box = $("#tiles");
  box.innerHTML = "";
  TILES.forEach(([label, value, small]) => {
    const tile = document.createElement("div");
    tile.className = "tile";
    const k = document.createElement("div");
    k.className = "tile-k";
    k.textContent = label;
    const row = document.createElement("div");
    row.className = "tile-row";
    const v = document.createElement("div");
    // Значению отдана вся ширина плитки: подписей рядом больше нет, и цене
    // в девять знаков ничего не мешает быть видной целиком.
    v.className = "tile-v" + (small ? " small" : "");
    v.textContent = value();
    row.appendChild(v);
    tile.append(k, row);
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
  const ico = document.createElement("span");
  ico.className = "foot-icon";
  ico.appendChild(icon(theme === "dark" ? "sun" : "moon"));
  const label = document.createElement("span");
  label.className = "foot-label";
  label.textContent = theme === "dark" ? "Светлая тема" : "Тёмная тема";
  btn.append(ico, label);
}

// Узкое окно: сайдбар и панель превращаются в ящики поверх ленты. Оба
// свёрнуты по умолчанию — иначе от чата остаётся полоска посередине.
const NARROW = window.matchMedia("(max-width: 940px)");

// Свёрнутые сайдбар и панель оставляют на экране кнопку разворота: иначе
// вернуть их было бы нечем.
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
  const backdrop = document.createElement("div");
  backdrop.className = "backdrop";
  backdrop.onclick = closeDrawers;
  document.body.appendChild(backdrop);
}

// Какие борта должны быть свёрнуты при данной ширине. Вынесено отдельной
// функцией без DOM: решение проверяется без браузера, в checks/browser_check.js.
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

// Что должен закрыть Escape. Диалог подтверждения всегда важнее ящиков:
// он поверх всего, и пока он открыт, Escape относится к нему.
// Отдельной функцией без DOM — решение проверяется без браузера.
function escapeAction(hasDialog, narrow, openDrawerCount) {
  if (hasDialog) return "dialog";
  if (narrow && openDrawerCount > 0) return "drawers";
  return null;
}

function confirmBox(title, text, confirmLabel, onYes) {
  const wrap = document.createElement("div");
  wrap.className = "confirm";
  const box = document.createElement("div");
  box.className = "confirm-box";
  const h = document.createElement("h3");
  h.textContent = title;
  const p = document.createElement("p");
  p.textContent = text;
  const row = document.createElement("div");
  row.className = "confirm-row";

  const close = () => {
    document.removeEventListener("keydown", onKey);
    wrap.remove();
  };
  // Escape закрывает диалог всегда, а не только на узком окне: это
  // подтверждение необратимого действия, и выйти из него надо уметь
  // не глядя. Обработчик снимается вместе с диалогом.
  const onKey = (ev) => {
    if (ev.key === "Escape") close();
  };
  document.addEventListener("keydown", onKey);

  const no = document.createElement("button");
  no.type = "button";
  no.textContent = "Отмена";
  no.onclick = close;
  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = "primary";
  yes.textContent = confirmLabel;
  yes.onclick = () => { close(); onYes(); };
  row.append(no, yes);
  box.append(h, p, row);
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
    if (ev.target.id === "f-response_format_kind") syncResponseFormat();
    applySettings();
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      state.tab = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $("#tab-model").classList.toggle("hidden", state.tab !== "model");
      $("#tab-agent").classList.toggle("hidden", state.tab !== "agent");
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

  setBusy(false);
  renderTiles();
  loadAgents().catch((err) => hint(String(err.message || err), true));
}

// В браузере файл просто запускается. Под node его подключают проверки:
// checks/browser_check.js гоняет разбор markdown и решение о раскладке
// настоящими вызовами, а не grep'ом по исходнику.
if (typeof module === "undefined") {
  init();
} else {
  module.exports = {
    init,
    state,
    renderMarkdown,
    escapeHtml,
    inlineMarkdown,
    layoutFor,
    escapeAction,
    readStopLines,
    parseResponseFormat,
    paramWarnings,
    sameValue,
    configChanged,
    fmt,
  };
}
