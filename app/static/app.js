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
  lastMetrics: null,   // метрики последнего ответа — из них плитки
  applying: null,      // незавершённое применение настроек панели
  panelDirty: false,   // правка панели не доехала до агента
  stick: true,         // лента примотана к низу — доматывать новые ответы
  baseModel: "",       // модель, с которой чат открыли: с ней сверяем смену
  statusTimer: null,   // таймер, гасящий строку состояния
};

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
  activity: "M3 12h4l3 8 4-16 3 8h4",
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
  num: (v) => (v === null || v === undefined ? "—" : String(v)),
  rate: (v) => (v ? v.toFixed(1) : "—"),
  cost: (v) => (v === null || v === undefined ? "—" : "$" + Number(v).toFixed(6)),
  pct: (v) => (v === null || v === undefined ? "—" : v.toFixed(1) + " %"),
  text: (v) => v || "—",
};

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
  await openAgent(exists ? wanted : (state.agents[0] || {}).id);
}

function renderList() {
  const box = $("#agent-list");
  box.innerHTML = "";
  state.agents.forEach((agent) => box.appendChild(listItem(agent)));
}

function listItem(agent) {
  const active = state.current && agent.id === state.current.id;
  const row = el("div", "item" + (active ? " active" : ""));

  const open = el("button", "item-open");
  open.type = "button";
  const ico = el("span", "item-icon");
  ico.appendChild(icon("chat"));
  open.append(ico, el("span", "item-title", agent.label));
  open.title = agent.label + "\n" + agent.model;
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
  state.lastMetrics = lastAnswerMetrics(agent);
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

  turns.forEach((turn) => {
    feed.appendChild(turn.role === "user" ? userBubble(turn.content) : answerCard(agent, turn));
  });
  feed.scrollTop = keep === null ? feed.scrollHeight : keep;
}

function userBubble(text) {
  return el("div", "msg-user", text);
}

// Шапка карточки: одна на готовый ответ и на тот, в который ещё стримят.
function cardHead(modelName, provider) {
  const head = el("header", "card-head");
  const ico = el("span", "card-icon");
  ico.appendChild(icon("bot"));
  head.append(ico, el("span", "card-model", modelName));
  if (provider) head.appendChild(el("span", "card-tag", provider));
  return head;
}

function answerCard(agent, turn) {
  const card = el("article", "card" + (turn.error ? " failed" : ""));
  const head = cardHead(
    (turn.metrics && turn.metrics.model) || agent.model,
    turn.metrics && turn.metrics.provider
  );

  const actions = el("div", "card-actions");
  actions.append(
    iconButton("activity", "Метрики этого ответа", () => {
      state.lastMetrics = turn.metrics || state.lastMetrics;
      renderTiles();
      hint("Плитки справа показывают метрики выбранного ответа.");
    }),
    iconButton("copy", "Копировать ответ", () => copyText(turn.content)),
    iconButton("refresh", "Перегенерировать", () => regenerate()),
    iconButton("dots", "Показать сырой текст", () => showRaw(card, turn))
  );
  head.appendChild(actions);
  card.appendChild(head);

  if (turn.reasoning) card.appendChild(thinkingBlock(turn.reasoning));

  const body = el("div", "card-body md");
  body.innerHTML = renderMarkdown(turn.content);
  card.appendChild(body);

  if (turn.error) card.appendChild(el("div", "card-error", turn.error));
  return card;
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
            if (e.metrics) { state.lastMetrics = e.metrics; renderTiles(); }
            scrollFeed();
            break;
          case "metrics":
            state.lastMetrics = e.metrics;
            renderTiles();
            break;
          case "error":
            failure = e.message;
            if (e.metrics) state.lastMetrics = e.metrics;
            break;
          case "done":
            if (e.text) answer = e.text;
            if (e.reasoning) reasoning = e.reasoning;
            if (e.metrics) state.lastMetrics = e.metrics;
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
    // Панель намеренно не перерисовываем: пользователь мог печатать в ней
    // прямо сейчас, и затирать его текст ответом сервера нельзя.
  } catch (e) { /* чат исчез — список обновится при следующем открытии */ }
}

// ─────────────────────── панель настроек ──────────────────────

const NUMBER_FIELDS = [
  "temperature", "max_tokens", "top_p", "top_k", "min_p",
  "repetition_penalty", "presence_penalty", "frequency_penalty",
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
  // Модель ставим сразу, не дожидаясь каталога: панель — источник правды,
  // и её пустоту нельзя пролить в агента.
  setModelOptions([{ id: agent.model }], agent.model);
  state.baseModel = agent.model;
  fillModels(agent.model).then(renderWarnings);
  saveStatus("");
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

// Изменила ли правка хоть что-нибудь. Сравниваются только поля панели:
// стенограмма и занятость живут своей жизнью, и по ним «изменилось» было бы
// правдой всегда.
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

// Плитка «Первый токен» показывает момент, когда модель заговорила вообще
// (first_token_ms), а не когда пошёл ответ: на думающей модели это разные
// числа, и TTFT там включал бы всё размышление. Насколько ответ отстал
// от рассуждения, видно подписью справа.
const TILES = [
  ["Ток/с", (m) => fmt.rate(m.tokens_per_second), (m) => (m.tokens_out ? m.tokens_out + " ток" : "")],
  [
    "Первый токен, с",
    (m) => fmt.sec(m.first_token_ms === null || m.first_token_ms === undefined ? m.ttft_ms : m.first_token_ms),
    (m) => {
      const first = m.first_token_ms;
      if (first === null || first === undefined || m.ttft_ms === null || m.ttft_ms === undefined) return "";
      const gap = m.ttft_ms - first;
      return gap > 1 ? "ответ +" + fmt.sec(gap) + " с" : "";
    },
  ],
  ["Промпт, ток", (m) => fmt.num(m.prompt_tokens), () => ""],
  ["Ответ, ток", (m) => fmt.num(m.completion_tokens),
    (m) => (m.reasoning_tokens ? m.reasoning_tokens + " рассужд" : "")],
  ["Стоимость", (m) => fmt.cost(m.cost_usd), () => "", true],
  ["Время, с", (m) => fmt.sec(m.elapsed_ms), () => ""],
  ["Провайдер", (m) => fmt.text(m.provider), () => "", true],
  ["Контекст", (m) => fmt.pct(m.context_fill_pct), (m) => m.finish_reason || ""],
];

function renderTiles() {
  const box = $("#tiles");
  const m = state.lastMetrics || {};
  box.innerHTML = "";
  TILES.forEach(([label, value, sub, small]) => {
    const row = el("div", "tile-row");
    row.appendChild(el("div", "tile-v" + (small ? " small" : ""), state.lastMetrics ? value(m) : "—"));
    const subText = state.lastMetrics ? sub(m) : "";
    if (subText) row.appendChild(el("div", "tile-sub", subText));
    const tile = el("div", "tile");
    tile.append(el("div", "tile-k", label), row);
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
    if (ev.target.id === "f-response_format_kind") syncResponseFormat();
    applySettings();
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      const which = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $("#tab-model").classList.toggle("hidden", which !== "model");
      $("#tab-agent").classList.toggle("hidden", which !== "agent");
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
  };
}
