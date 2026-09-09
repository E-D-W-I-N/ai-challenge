"use strict";

// День 6: ленту диалога хранит агент на сервере. Клиент держит только id
// агента и шлёт новое сообщение текстом — ни истории, ни модели в теле
// запроса больше нет.

const state = {
  scenarios: [],
  roster: [],
  hasKey: false,
  current: null,        // выбранный сценарий
  chat: null,           // { id, spec, feed, входное поле, busy } — агент главного экрана
  columns: new Map(),   // label -> DOM-ссылки колонки-субагента
  runBlock: null,       // вложенный блок прогона в ленте чата
  running: false,
  abort: null,          // AbortController активного потока прогона
  judge: null,          // блок «Вердикт» текущего прогона
};

const $ = (sel) => document.querySelector(sel);

function fmtMs(v) {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? (v / 1000).toFixed(2) + " с" : Math.round(v) + " мс";
}
function fmtCost(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toFixed(6);
}
function fmtNum(v) {
  return v === null || v === undefined ? "—" : String(v);
}

// --- сеть ---------------------------------------------------------------

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (e) { /* тело не JSON — остаётся код статуса */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// SSE поверх fetch: и чат, и прогон идут одним и тем же POST-потоком.
// AbortController нужен не для красоты — оборванный fetch закрывает соединение,
// сервер это видит и гасит вызов к модели.
async function streamEvents(path, body, onEvent, signal) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const parsed = await res.json();
      if (parsed && parsed.detail) {
        detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      }
    } catch (e) { /* тело не JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }

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

// Тело сообщения — только текст. Всё остальное у агента уже есть.
function sendTo(agentId, text, onEvent, signal) {
  return streamEvents(`/api/agents/${agentId}/messages`, { text }, onEvent, signal);
}

// --- загрузка дня --------------------------------------------------------

async function loadDay() {
  const data = await api("/api/scenarios");
  state.scenarios = data.scenarios;
  state.roster = data.roster;
  state.hasKey = !!data.has_key;

  const badge = $("#key-status");
  badge.textContent = data.has_key ? "OPENROUTER_API_KEY найден" : "нет .env с OPENROUTER_API_KEY";
  badge.className = "badge " + (data.has_key ? "ok" : "bad");

  renderSidebar();
  renderRoster();
  await newChatAgent(0);
  refreshRegistry();
}

function renderSidebar() {
  const list = $("#scenario-list");
  list.innerHTML = "";
  if (!state.scenarios.length) {
    list.innerHTML = '<p class="empty-hint">Сценариев нет — day.py не отдал ни одного</p>';
    return;
  }
  const ul = document.createElement("ul");
  ul.className = "scenarios";
  state.scenarios.forEach((sc) => {
    const li = document.createElement("li");
    li.textContent = sc.title;
    li.onclick = () => selectScenario(sc.index);
    li.dataset.index = String(sc.index);
    ul.appendChild(li);
  });
  list.appendChild(ul);
}

// Реестр процесса виден в сайдбаре: сто агентов — это сто строк здесь,
// а не сто вкладок и не сто процессов.
async function refreshRegistry() {
  let data;
  try {
    data = await api("/api/agents");
  } catch (e) {
    return;
  }
  $("#registry-badge").textContent = `агентов: ${data.live} из ${data.max_agents}`;
  const box = $("#agent-list");
  box.innerHTML = "";
  data.agents.forEach((a) => {
    const row = document.createElement("div");
    row.className = "agent-row" + (state.chat && a.id === state.chat.id ? " active" : "");
    row.innerHTML = `<span class="agent-row-label"></span><span class="agent-row-id"></span>`;
    row.querySelector(".agent-row-label").textContent =
      (a.parent_id ? "↳ " : "") + a.label + (a.history_len ? ` · ${a.history_len}` : "");
    row.querySelector(".agent-row-id").textContent = a.id;
    row.title = `${a.model}\nокно памяти ${a.history_limit}, реплик ${a.history_len}`;
    box.appendChild(row);
  });
}

// --- чат с агентом главного экрана ---------------------------------------

function renderRoster() {
  const sel = $("#roster-select");
  sel.innerHTML = "";
  state.roster.forEach((spec, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = spec.label;
    sel.appendChild(opt);
  });
  sel.onchange = () => newChatAgent(Number(sel.value));
}

async function newChatAgent(rosterIndex) {
  const spec = state.roster[rosterIndex] || state.roster[0];
  if (!spec) return;
  // Прошлый собеседник уходит вместе со своими субагентами: реестр не должен
  // копить брошенные диалоги.
  if (state.chat) {
    try { await api(`/api/agents/${state.chat.id}`, { method: "DELETE" }); } catch (e) { /* уже нет */ }
  }
  const created = await api("/api/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent: agentPayload(spec) }),
  });
  const agent = created.agents[0];
  state.chat = { id: agent.id, spec, busy: false, rosterIndex };
  $("#chat-agent-id").textContent = `${agent.id} · ${agent.model} · окно памяти ${agent.history_limit}`;
  $("#chat-feed").innerHTML = "";
  hideStage();
  appendChat("system-note", chatHint(spec));
  setChatBusy(false);
  refreshRegistry();
}

// Конфиг для ручки создания: ровно те поля, которые она принимает.
function agentPayload(spec) {
  return {
    label: spec.label,
    model: spec.model,
    messages: spec.messages || [],
    temperature: spec.temperature ?? null,
    max_tokens: spec.max_tokens ?? null,
    stop: spec.stop ?? null,
    response_format: spec.response_format ?? null,
    extra_body: spec.extra_body || {},
    system: spec.system || "",
    note: spec.note || "",
    repeats: spec.repeats ?? 1,
    history_limit: spec.history_limit ?? null,
  };
}

function chatHint(spec) {
  const lines = [spec.note || "Агент готов."];
  if (state.scenarios.length) {
    lines.push("Команда /прогон <номер> запускает сценарий субагентами:");
    state.scenarios.forEach((sc) => lines.push(`  /прогон ${sc.index + 1} — ${sc.title}`));
  }
  if (!state.hasKey) lines.push("Нужен .env с OPENROUTER_API_KEY — без него вызова не будет.");
  return lines.join("\n");
}

function appendChat(kind, text) {
  const el = document.createElement("div");
  el.className = "msg " + kind;
  if (kind !== "system-note") {
    const head = document.createElement("div");
    head.className = "role";
    head.textContent = kind === "user" ? "вы" : kind;
    el.appendChild(head);
  }
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text || "";
  el.appendChild(body);
  const feed = $("#chat-feed");
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
  return body;
}

function setChatBusy(busy) {
  if (state.chat) state.chat.busy = busy;
  const locked = busy || !state.hasKey;
  $("#chat-input").disabled = locked;
  $("#chat-send").disabled = locked;
  $("#chat-input").placeholder = state.hasKey
    ? "Спросить агента или /прогон 1…"
    : "Нужен .env с OPENROUTER_API_KEY";
  $("#chat-form").classList.toggle("locked", !state.hasKey);
}

function autoGrow(input) {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

async function sendChat() {
  const input = $("#chat-input");
  const text = (input.value || "").trim();
  if (!text || !state.chat || state.chat.busy) return;
  const isCommand = text.startsWith("/") && !text.startsWith("//");
  if (!isCommand && !state.hasKey) return;

  input.value = "";
  autoGrow(input);
  const questionBox = appendChat("user", text).parentElement;
  setChatBusy(true);

  const controller = new AbortController();
  state.abort = controller;
  let body = null;
  let answer = "";
  let failed = null;

  try {
    await sendTo(state.chat.id, text, (e) => {
      if (isCommand) {
        handleRunEvent(e);
        return;
      }
      switch (e.event) {
        case "delta":
          if (!body) body = appendChat("assistant", "");
          answer += e.text;
          body.textContent = answer;
          $("#chat-feed").scrollTop = $("#chat-feed").scrollHeight;
          break;
        case "done":
          if (e.text) {
            if (!body) body = appendChat("assistant", "");
            answer = e.text;
            body.textContent = answer;
          }
          break;
        case "error":
          failed = e.message;
          break;
      }
    }, controller.signal);
  } catch (err) {
    if (err.name !== "AbortError") failed = String(err.message || err);
  }

  if (failed) {
    // Обмена не было: агент вопрос не запомнил, и в ленте его оставлять нельзя —
    // текст возвращается в поле ввода, чтобы можно было повторить.
    if (!answer) {
      questionBox.remove();
      if (!input.value) { input.value = text; autoGrow(input); }
    }
    appendChat("failed", failed);
  }
  state.abort = null;
  setChatBusy(false);
  refreshRegistry();
}

// --- сценарий и колонки-субагенты ----------------------------------------

function hideStage() {
  $("#stage").classList.add("hidden");
  $("#verdict").classList.add("hidden");
  $("#summary").classList.add("hidden");
  state.columns.clear();
  state.current = null;
  document.querySelectorAll("#scenario-list li").forEach((li) => li.classList.remove("active"));
}

async function selectScenario(index) {
  const sc = state.scenarios[index];
  if (!sc || !state.chat) return;
  stopRun();
  state.current = sc;
  document.querySelectorAll("#scenario-list li").forEach((li) => {
    li.classList.toggle("active", Number(li.dataset.index) === index);
  });

  // Агенты колонок создаются заранее: с колонкой можно переписываться
  // до «Старта» — она уже настоящая сессия, а не заготовка.
  let spawned;
  try {
    spawned = await api(`/api/scenarios/${index}/agents?parent=${state.chat.id}`, { method: "POST" });
  } catch (err) {
    appendChat("failed", `не удалось создать субагентов: ${err.message}`);
    return;
  }

  $("#stage").classList.remove("hidden");
  // Колонки первыми: дропдаун берёт текущую модель из state.columns, и она
  // должна быть там до того, как соберутся сами дропдауны.
  renderColumns(spawned.agents, sc.layout);
  renderScenarioBar(sc);
  resetVerdict();
  $("#summary").classList.add("hidden");
  refreshRegistry();
}

// Шапка сценария — одна компактная строка: название, модели, «Старт».
// description стенд не показывает: он уходит судье, а на экране от него
// только лишняя высота, которой не хватает колонкам.
function renderScenarioBar(sc) {
  const bar = $("#scenario-bar");
  bar.innerHTML = `
    <h2 class="scenario-title"></h2>
    <div id="pickers" class="pickers"></div>
    <button class="start" id="start-btn">Старт</button>`;
  bar.querySelector(".scenario-title").textContent = sc.title;
  $("#start-btn").onclick = startRun;
  buildPickers(sc);
}

// Дропдаун модели на каждую колонку. Каталог ключа не требует —
// живой ещё до того, как пользователь создаст .env.
async function buildPickers(sc) {
  const box = $("#pickers");
  const needsTemperature = sc.sessions.some((s) => s.temperature !== null && s.temperature !== undefined);
  const hotTemperature = sc.sessions.some((s) => (s.temperature ?? 0) > 1.0);
  const params = new URLSearchParams();
  const requires = [];
  if (needsTemperature) requires.push("temperature");
  if (sc.sessions.some((s) => s.stop && s.stop.length)) requires.push("stop");
  if (sc.sessions.some((s) => s.response_format)) requires.push("response_format");
  if (requires.length) params.set("requires", requires.join(","));
  params.set("exclude_free", "true");
  // День 4: anthropic/* обрезает температуру на 1.0 и вернёт 400 на 1.2.
  if (hotTemperature) params.set("exclude_temperature_capped", "true");

  let models = [];
  try {
    models = (await api("/api/models?" + params.toString())).models || [];
  } catch (e) {
    box.innerHTML = '<span class="hint">каталог моделей недоступен — берём модели из сценария</span>';
    return;
  }

  box.innerHTML = "";
  sc.sessions.forEach((s) => {
    const col = state.columns.get(s.label);
    const wrap = document.createElement("label");
    wrap.className = "model-picker";
    if (sc.sessions.length > 1) {
      const name = document.createElement("span");
      name.className = "picker-label";
      name.textContent = s.label;
      wrap.appendChild(name);
    }
    const sel = document.createElement("select");
    const current = col ? col.model : s.model;
    const options = models.some((m) => m.id === current)
      ? models
      : [{ id: current, name: current + " (из сценария)", prompt_price_per_m: 0, completion_price_per_m: 0 }, ...models];
    options.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.id;
      const price = m.prompt_price_per_m
        ? `  ·  $${m.prompt_price_per_m}/$${m.completion_price_per_m} за 1M`
        : "";
      opt.textContent = m.id + price;
      if (m.id === current) opt.selected = true;
      sel.appendChild(opt);
    });
    // Модель меняется прямо на живом агенте: в теле сообщения её больше нет.
    // Выбор запоминается на агенте и переносится на свежий набор колонок,
    // который спавнит «Старт», — иначе он молча откатился бы на day.py.
    sel.onchange = async () => {
      const entry = state.columns.get(s.label);
      if (!entry) return;
      try {
        const patched = await api(`/api/agents/${entry.agentId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: sel.value }),
        });
        entry.model = patched.model;
        entry.modelId.textContent = patched.model;
      } catch (err) {
        setStatus(entry, "error", `модель не сменилась: ${err.message}`);
        sel.value = entry.model;
      }
    };
    wrap.appendChild(sel);
    box.appendChild(wrap);
  });
}

const STAT_FIELDS = [
  ["ttft_ms", "TTFT", fmtMs],
  ["tokens_per_second", "ток/с", (v) => (v ? v.toFixed(1) : "—")],
  ["tokens_out", "сгенерировано", fmtNum],
  ["elapsed_ms", "прошло", fmtMs],
  ["prompt_tokens", "prompt", fmtNum],
  ["completion_tokens", "completion", fmtNum],
  ["total_tokens", "total", fmtNum],
  ["reasoning_tokens", "reasoning", fmtNum],
  ["cost_usd", "стоимость", fmtCost],
  ["finish_reason", "finish_reason", (v) => v || "—"],
  ["provider", "провайдер", (v) => v || "—"],
  ["context_fill_pct", "контекст", (v) => (v === null || v === undefined ? "—" : v.toFixed(1) + " %")],
];

const PLACEHOLDER = "{{depends_on}}";

// Тело сообщения строим через textContent: промпт печатается как есть,
// без интерпретации разметки. {{depends_on}} подсвечиваем отдельным span.
function messageBody(content) {
  const body = document.createElement("div");
  body.className = "body";
  const text = typeof content === "string" ? content : JSON.stringify(content);
  if (!text.includes(PLACEHOLDER)) {
    body.textContent = text;
    return body;
  }
  text.split(PLACEHOLDER).forEach((part, i) => {
    if (i) {
      const ph = document.createElement("span");
      ph.className = "ph";
      ph.textContent = PLACEHOLDER;
      body.appendChild(ph);
    }
    body.appendChild(document.createTextNode(part));
  });
  return body;
}

// system сворачивается по клику, но открыт по умолчанию: зритель должен
// видеть, что инструкция есть и что в ней написано.
function promptMessage(m) {
  const role = String(m.role || "?").toLowerCase();
  if (role === "system") {
    const el = document.createElement("details");
    el.className = "msg system";
    el.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "system";
    el.append(summary, messageBody(m.content));
    return el;
  }
  const el = document.createElement("div");
  el.className = "msg " + (role === "assistant" ? "assistant" : "user");
  const head = document.createElement("div");
  head.className = "role";
  head.textContent = role;
  el.append(head, messageBody(m.content));
  return el;
}

function renderPrompt(col, messages, resolved) {
  col.promptBox.innerHTML = "";
  (messages || []).forEach((m) => col.promptBox.appendChild(promptMessage(m)));
  if (!col.dependsOn) return;

  const hint = document.createElement("div");
  hint.className = "dep-hint";
  if (resolved) {
    hint.textContent = `промпт после подстановки вывода колонки «${col.dependsOn}»`;
  } else if ((messages || []).some((m) => typeof m.content === "string" && m.content.includes(PLACEHOLDER))) {
    hint.textContent = `${PLACEHOLDER} заменится выводом колонки «${col.dependsOn}» — итоговый промпт появится здесь на старте`;
  } else {
    hint.textContent = `колонка стартует после колонки «${col.dependsOn}»`;
  }
  col.promptBox.appendChild(hint);
}

function scrollChat(col) {
  col.chat.scrollTop = col.chat.scrollHeight;
}

// Сообщение, дописанное после промпта сценария: ручной вопрос или ответ модели.
function appendMessage(col, role, text, caption) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  const head = document.createElement("div");
  head.className = "role";
  head.textContent = caption || role;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text || "";
  el.append(head, body);
  col.chat.appendChild(el);
  scrollChat(col);
  return body;
}

function setStatus(col, kind, text) {
  col.status.className = "status" + (kind ? " " + kind : "");
  col.status.textContent = text;
}

function setBusy(col, busy) {
  col.busy = busy;
  col.root.classList.toggle("busy", busy);
  if (!col.input) return;
  col.input.disabled = busy || !state.hasKey;
  col.sendBtn.disabled = busy || !state.hasKey;
}

function buildComposer(col) {
  const form = document.createElement("form");
  form.className = "composer";

  const input = document.createElement("textarea");
  input.className = "composer-input";
  input.rows = 1;
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "send";
  send.textContent = "Отправить";

  input.title = "Enter — отправить, Shift+Enter — перенос строки";
  if (state.hasKey) {
    input.placeholder = "Спросить эту колонку…";
  } else {
    // Без ключа поле недоступно, но видно, чего не хватает.
    input.placeholder = "Нужен .env с OPENROUTER_API_KEY";
    input.disabled = true;
    send.disabled = true;
    form.classList.add("locked");
  }

  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    sendManual(col);
  });

  form.append(input, send);
  col.input = input;
  col.sendBtn = send;
  return form;
}

// agents — то, что вернула ручка спавна или run_start: у каждой колонки есть
// живой агент, и колонка адресуется его id.
function renderColumns(agents, layout) {
  const box = $("#columns");
  box.className = "columns" + (layout === "single" ? " single" : "");
  box.innerHTML = "";
  state.columns.clear();

  agents.forEach((a) => {
    const col = document.createElement("div");
    col.className = "column";

    const head = document.createElement("header");
    head.innerHTML = `<h3></h3><div class="modelid"></div><div class="agent-id"></div>
      <div class="note"></div>`;
    head.querySelector("h3").textContent = a.label;
    head.querySelector(".modelid").textContent = a.model;
    head.querySelector(".agent-id").textContent = a.id;
    const note = head.querySelector(".note");
    if (a.note) note.textContent = a.note; else note.remove();

    // Лента: промпт сверху (виден до «Старта»), ответы и ручные вопросы — под ним.
    const chat = document.createElement("div");
    chat.className = "chat";
    const promptBox = document.createElement("div");
    promptBox.className = "prompt";
    chat.appendChild(promptBox);

    const stats = document.createElement("div");
    stats.className = "stats";
    const values = {};
    STAT_FIELDS.forEach(([key, title]) => {
      const row = document.createElement("div");
      row.className = "stat";
      row.innerHTML = `<span class="k"></span><span class="v">—</span>`;
      row.querySelector(".k").textContent = title;
      values[key] = row.querySelector(".v");
      stats.appendChild(row);
    });

    // Подпись к панели: при серии метрики в ней относятся к конкретному
    // прогону, и это должно быть написано, а не подразумеваться.
    const statsNote = document.createElement("div");
    statsNote.className = "stats-note hidden";

    // Доля уникальных ответов — то, ради чего серия и делается.
    const uniq = document.createElement("div");
    uniq.className = "uniq hidden";

    const status = document.createElement("div");
    status.className = "status";
    status.textContent = "готова к разговору";

    const entry = {
      label: a.label,
      agentId: a.id,
      model: a.model,
      root: col,
      modelId: head.querySelector(".modelid"),
      values,
      chat,
      promptBox,
      statsNote,
      uniq,
      status,
      dependsOn: a.depends_on || null,
      base: (a.seed_messages || []).map((m) => ({ role: m.role, content: m.content })),
      answer: null,       // тело ответа сценария, в него стримятся delta
      repeatsTotal: 1,    // длина серии: приходит в session_start
      repeats: new Map(), // индекс прогона -> тело его блока
      texts: [],          // тексты прогонов серии — из них считается уникальность
      lastMetrics: null,
      busy: false,
    };

    // Лента скроллится, метрики и поле ввода остаются на месте.
    col.append(head, chat, uniq, statsNote, stats, status, buildComposer(entry));
    box.appendChild(col);

    renderPrompt(entry, entry.base, false);
    state.columns.set(a.label, entry);
  });

  // Стартовый промпт колонки приезжает отдельным запросом: ручка спавна
  // отдаёт конфиг без стенограммы, а показать промпт надо до «Старта».
  agents.forEach(async (a) => {
    const entry = state.columns.get(a.label);
    if (!entry || entry.base.length) return;
    try {
      const full = await api(`/api/agents/${a.id}`);
      entry.base = (full.seed_messages || []).map((m) => ({ role: m.role, content: m.content }));
      renderPrompt(entry, entry.base, false);
    } catch (e) { /* промпт не показали — колонка всё равно работает */ }
  });
}

// Ответ сценария. При repeats=1 это одно сообщение assistant на колонку,
// при серии — по сообщению на прогон с подписью «прогон N из M».
function answerBlock(col, repeat) {
  if (repeat === undefined || repeat === null) {
    if (!col.answer) col.answer = appendMessage(col, "assistant", "");
    return col.answer;
  }
  if (!col.repeats.has(repeat)) {
    col.repeats.set(
      repeat,
      appendMessage(col, "assistant", "", `assistant · прогон ${repeat + 1} из ${col.repeatsTotal}`)
    );
  }
  return col.repeats.get(repeat);
}

// Метрики прогона дописываются под его же ответом и больше не меняются:
// панель внизу показывает текущий прогон, а прошлые остаются в ленте.
function repeatMetricsLine(body, metrics) {
  if (!metrics) return;
  const parts = [];
  if (metrics.ttft_ms !== null && metrics.ttft_ms !== undefined) parts.push("TTFT " + fmtMs(metrics.ttft_ms));
  if (metrics.tokens_per_second) parts.push(metrics.tokens_per_second.toFixed(1) + " ток/с");
  if (metrics.completion_tokens || metrics.tokens_out) parts.push((metrics.completion_tokens || metrics.tokens_out) + " токенов");
  if (metrics.cost_usd !== null && metrics.cost_usd !== undefined) parts.push(fmtCost(metrics.cost_usd));
  if (metrics.finish_reason) parts.push(metrics.finish_reason);
  if (!parts.length) return;
  const line = document.createElement("div");
  line.className = "repeat-metrics";
  line.textContent = parts.join("  ·  ");
  body.parentElement.appendChild(line);
}

// «уникальных ответов: N из M» — счётчик растёт по ходу серии.
function updateUniq(col) {
  if (col.repeatsTotal <= 1 || !col.texts.length) return;
  const unique = new Set(col.texts.map((t) => t.trim())).size;
  col.uniq.classList.remove("hidden");
  col.uniq.textContent =
    `уникальных ответов: ${unique} из ${col.texts.length}` +
    ` (${Math.round((100 * unique) / col.texts.length)} %)`;
}

function applyMetrics(col, metrics) {
  if (!metrics) return;
  col.lastMetrics = metrics;
  STAT_FIELDS.forEach(([key, , fmt]) => {
    const el = col.values[key];
    if (!el) return;
    el.textContent = fmt(metrics[key]);
    el.classList.toggle("length", key === "finish_reason" && metrics[key] === "length");
    el.classList.toggle("err", key === "finish_reason" && metrics.error);
  });
}

// --- ручной вопрос колонке: тело запроса — только текст -------------------

async function sendManual(col) {
  const text = (col.input.value || "").trim();
  if (!text || col.busy || !state.hasKey) return;

  col.input.value = "";
  autoGrow(col.input);
  const questionBox = appendMessage(col, "user", text).parentElement;
  const body = appendMessage(col, "assistant", "");
  setBusy(col, true);
  setStatus(col, "", "генерация…");

  // Обмен не состоялся: агент вопрос не запомнил, значит и в ленте его быть
  // не должно — иначе кадр врёт про то, что видит модель. Текст возвращается
  // в поле ввода, чтобы можно было повторить.
  const rollback = () => {
    questionBox.remove();
    body.parentElement.remove();
    if (!col.input.value) {
      col.input.value = text;
      autoGrow(col.input);
    }
  };

  let answer = "";
  let failed = false;
  try {
    await sendTo(col.agentId, text, (e) => {
      switch (e.event) {
        case "delta":
          answer += e.text;
          body.textContent = answer;
          applyMetrics(col, e.metrics);
          scrollChat(col);
          break;
        case "metrics":
          applyMetrics(col, e.metrics);
          break;
        case "error":
          failed = true;
          applyMetrics(col, e.metrics);
          setStatus(col, "error", e.message);
          break;
        case "done":
          applyMetrics(col, e.metrics);
          answer = e.text || answer;
          body.textContent = answer;
          break;
      }
    });
  } catch (err) {
    failed = true;
    setStatus(col, "error", String(err.message || err));
  }

  if (answer) {
    if (!col.status.classList.contains("error")) setStatus(col, "", "готово");
  } else if (failed) {
    rollback();
  }

  setBusy(col, false);
  scrollChat(col);
  refreshRegistry();
}

// --- блок «Вердикт»: ответ модели-судьи -----------------------------------

function resetVerdict() {
  state.judge = null;
  const box = $("#verdict");
  box.className = "verdict hidden";
  box.innerHTML = "";
}

function verdictBox(modelLine) {
  const box = $("#verdict");
  box.className = "verdict";
  box.innerHTML = `<header><h3>Вердикт</h3><div class="judge-model"></div>
    <div class="judge-conflict hidden"></div></header>
    <div class="verdict-body"></div><div class="verdict-status"></div>`;
  box.querySelector(".judge-model").textContent = modelLine;
  return {
    root: box,
    model: box.querySelector(".judge-model"),
    conflict: box.querySelector(".judge-conflict"),
    body: box.querySelector(".verdict-body"),
    status: box.querySelector(".verdict-status"),
    text: "",
    cost: null,
  };
}

function verdictStatus(kind, text) {
  if (!state.judge) return;
  state.judge.status.className = "verdict-status" + (kind ? " " + kind : "");
  state.judge.status.textContent = text;
}

function startVerdict(e) {
  const judge = verdictBox("судит " + e.model + (e.agent ? " · " + e.agent : ""));
  // Пока судья молчит, в кадре должно быть видно, что он работает,
  // а не пустой блок.
  judge.root.classList.add("busy");
  if (e.conflicts && e.conflicts.length) {
    judge.conflict.classList.remove("hidden");
    judge.conflict.textContent =
      "судья совпал с моделью колонки " + e.conflicts.map((c) => `«${c}»`).join(", ");
  }
  state.judge = judge;
  verdictStatus("", "судья читает ответы колонок…");
}

function appendVerdict(text) {
  if (!state.judge) return;
  state.judge.text += text;
  state.judge.body.textContent = state.judge.text;
  verdictStatus("", "судья пишет…");
}

function finishVerdict(e) {
  if (!state.judge) return;
  state.judge.root.classList.remove("busy");
  if (e.text) {
    state.judge.text = e.text;
    state.judge.body.textContent = e.text;
  }
  if (e.metrics && e.metrics.cost_usd !== null && e.metrics.cost_usd !== undefined) {
    state.judge.cost = e.metrics.cost_usd;
  }
  verdictStatus("", "готово");
}

// Вердикт — надстройка над прогоном: его ошибка не трогает ни колонки,
// ни сводку, только сам блок.
function failVerdict(message) {
  if (!state.judge) state.judge = verdictBox("судья");
  state.judge.root.classList.remove("busy");
  verdictStatus("error", message);
}

function skipVerdict(message) {
  state.judge = verdictBox("судья не вызывался");
  verdictStatus("", message);
}

// --- вложенный блок прогона в ленте чата ----------------------------------

// Прогон живёт в ленте чата отдельным блоком с дорожками по субагентам.
// Дорожка — это витрина: полная сессия колонки открывается кнопкой.
function runBlock(title) {
  const el = document.createElement("div");
  el.className = "run-block";
  el.innerHTML = `<div class="run-head"><span class="run-title"></span><span class="run-state"></span></div>
    <div class="lanes"></div><div class="run-foot"></div>`;
  el.querySelector(".run-title").textContent = title;
  const feed = $("#chat-feed");
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
  return {
    root: el,
    lanes: el.querySelector(".lanes"),
    state: el.querySelector(".run-state"),
    foot: el.querySelector(".run-foot"),
    byLabel: new Map(),
  };
}

function addLane(block, label, agentId) {
  const lane = document.createElement("div");
  lane.className = "lane";
  lane.innerHTML = `<div class="lane-head"><span class="lane-label"></span>
      <span class="lane-agent"></span>
      <button class="ghost lane-open" type="button">Открыть сессию</button></div>
    <div class="lane-body"></div><div class="lane-foot"></div>`;
  lane.querySelector(".lane-label").textContent = label;
  lane.querySelector(".lane-agent").textContent = agentId || "";
  lane.querySelector(".lane-open").onclick = () => openLane(label);
  block.lanes.appendChild(lane);
  const entry = {
    root: lane,
    body: lane.querySelector(".lane-body"),
    foot: lane.querySelector(".lane-foot"),
    text: "",
  };
  block.byLabel.set(label, entry);
  return entry;
}

function lane(label) {
  if (!state.runBlock) return null;
  return state.runBlock.byLabel.get(label) || addLane(state.runBlock, label, "");
}

// «Открыть сессию» — это переход к колонке: она и есть отдельная сессия
// с собственным полем ввода и собственной историей на сервере.
function openLane(label) {
  const col = state.columns.get(label);
  if (!col) return;
  $("#stage").classList.remove("hidden");
  col.root.scrollIntoView({ behavior: "smooth", block: "nearest" });
  col.root.classList.add("highlight");
  setTimeout(() => col.root.classList.remove("highlight"), 1200);
  if (col.input && !col.input.disabled) col.input.focus();
}

function laneTail(entry) {
  // В дорожке видно, что модель пишет; целиком ответ читают в колонке.
  const text = entry.text;
  entry.body.textContent = text.length > 400 ? "…" + text.slice(-400) : text;
  const feed = $("#chat-feed");
  feed.scrollTop = feed.scrollHeight;
}

function laneFoot(entry, metrics, extra) {
  const parts = [];
  if (metrics) {
    if (metrics.ttft_ms !== null && metrics.ttft_ms !== undefined) parts.push("TTFT " + fmtMs(metrics.ttft_ms));
    if (metrics.completion_tokens || metrics.tokens_out) parts.push((metrics.completion_tokens || metrics.tokens_out) + " токенов");
    if (metrics.cost_usd !== null && metrics.cost_usd !== undefined) parts.push(fmtCost(metrics.cost_usd));
    if (metrics.finish_reason) parts.push(metrics.finish_reason);
  }
  if (extra) parts.unshift(extra);
  entry.foot.textContent = parts.join("  ·  ");
}

// --- прогон сценария ------------------------------------------------------

function resetRunUi() {
  state.running = false;
  const btn = $("#start-btn");
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Старт";
  }
  state.columns.forEach((col) => setBusy(col, false));
  setChatBusy(false);
}

// Обрывает активный прогон. Оборванный fetch закрывает соединение — сервер
// видит обрыв и гасит вызовы к модели, а не дожёвывает их за наш счёт.
function stopRun() {
  if (state.abort) {
    state.abort.abort();
    state.abort = null;
  }
  resetRunUi();
}

// «Старт» — это та же команда /прогон, что и в чате: один путь, один поток
// событий. Сервер сам убивает предыдущий набор субагентов и спавнит свежий.
function startRun() {
  if (!state.current || state.running || !state.chat) return;
  $("#chat-input").value = `/прогон ${state.current.index + 1}`;
  sendChat();
}

const runTotals = { cost: 0, tokens: 0, done: 0, expected: 0, startedAt: 0 };
const runStarted = new Set();
const runSettled = new Set();

function handleRunEvent(e) {
  const col = e.session ? state.columns.get(e.session) : null;
  const track = e.session ? lane(e.session) : null;

  switch (e.event) {
    case "command_start":
      state.running = true;
      runTotals.cost = 0;
      runTotals.tokens = 0;
      runTotals.done = 0;
      runTotals.startedAt = performance.now();
      runStarted.clear();
      runSettled.clear();
      state.runBlock = runBlock("Прогон: " + e.title);
      state.runBlock.state.textContent = "спавним субагентов…";
      resetVerdict();
      $("#summary").classList.add("hidden");
      if ($("#start-btn")) {
        $("#start-btn").disabled = true;
        $("#start-btn").textContent = "идёт прогон…";
      }
      break;

    case "run_start": {
      // Набор свежий: колонки перерисовываем по нему, старые id больше не живут.
      const sc = state.scenarios[e.scenario];
      if (sc) {
        state.current = sc;
        document.querySelectorAll("#scenario-list li").forEach((li) => {
          li.classList.toggle("active", Number(li.dataset.index) === e.scenario);
        });
      }
      const byLabel = new Map((e.agents || []).map((a) => [a.session, a.agent]));
      const agents = (e.sessions || []).map((s) => ({ ...s, id: byLabel.get(s.label) || "", seed_messages: s.messages }));
      $("#stage").classList.remove("hidden");
      renderColumns(agents, e.layout);
      // Шапка после колонок: в e.sessions уже стоит выбранная пользователем
      // модель, и дропдаун должен встать на неё, а не откатиться на day.py.
      renderScenarioBar(sc || { title: e.title, sessions: e.sessions || [] });
      state.columns.forEach((c) => setBusy(c, true));
      runTotals.expected = agents.length;
      state.runBlock.state.textContent = `${agents.length} субагентов`;
      agents.forEach((a) => addLane(state.runBlock, a.label, a.id));
      break;
    }

    case "session_waiting":
      if (col) setStatus(col, "waiting", `ждёт вывод колонки «${e.on}»`);
      if (track) laneFoot(track, null, `ждёт «${e.on}»`);
      break;

    case "session_start":
      runStarted.add(e.session);
      if (col) {
        col.repeatsTotal = e.repeats || 1;
        setStatus(col, "", col.repeatsTotal > 1
          ? `генерация… прогон 1 из ${col.repeatsTotal}`
          : "генерация…");
        // Для колонки с depends_on это первый момент, когда известен
        // итоговый промпт: заменяем предварительный текст на него.
        if (e.resolved_messages) {
          col.base = e.resolved_messages.map((m) => ({ role: m.role, content: m.content }));
          renderPrompt(col, col.base, true);
        }
      }
      if (track) laneFoot(track, null, "генерация…");
      break;

    case "repeat_start":
      if (col) {
        col.repeatsTotal = e.repeats || col.repeatsTotal;
        answerBlock(col, e.repeat);
        setStatus(col, "", `генерация… прогон ${e.repeat + 1} из ${col.repeatsTotal}`);
        // Панель метрик подписана прогоном: видно, к чему относятся цифры.
        col.statsNote.classList.remove("hidden");
        col.statsNote.textContent = `метрики прогона ${e.repeat + 1} из ${col.repeatsTotal}`;
      }
      if (track) {
        track.text = "";
        laneFoot(track, null, `прогон ${e.repeat + 1} из ${e.repeats}`);
      }
      break;

    case "delta":
      if (col) {
        answerBlock(col, e.repeat).textContent += e.text;
        applyMetrics(col, e.metrics);
        scrollChat(col);
      }
      if (track) {
        track.text += e.text;
        laneTail(track);
      }
      break;

    case "metrics":
      if (col) applyMetrics(col, e.metrics);
      break;

    case "repeat_done":
      if (col) {
        applyMetrics(col, e.metrics);
        repeatMetricsLine(answerBlock(col, e.repeat), e.metrics);
        col.texts.push(e.text || "");
        updateUniq(col);
        // Сумма по прогону складывается здесь: session_done у серии несёт
        // метрики последнего прогона и второй раз их считать нельзя.
        if (e.metrics) {
          if (e.metrics.cost_usd) runTotals.cost += e.metrics.cost_usd;
          if (e.metrics.total_tokens) runTotals.tokens += e.metrics.total_tokens;
        }
        scrollChat(col);
      }
      if (track) laneFoot(track, e.metrics, `прогон ${e.repeat + 1} готов`);
      break;

    case "repeat_error":
      // Падение одного прогона не хоронит колонку: серия идёт дальше.
      if (col) {
        const body = answerBlock(col, e.repeat);
        body.classList.add("failed");
        body.textContent = e.message;
        applyMetrics(col, e.metrics);
        scrollChat(col);
      }
      if (track) laneFoot(track, e.metrics, "прогон упал");
      break;

    case "session_error":
      runSettled.add(e.session);
      if (col) {
        setStatus(col, "error", e.message);
        applyMetrics(col, e.metrics);
        setBusy(col, false);
      }
      if (track) {
        track.root.classList.add("failed");
        laneFoot(track, e.metrics, e.message);
      }
      break;

    case "session_done":
      runSettled.add(e.session);
      if (col) {
        applyMetrics(col, e.metrics);
        // У серии суммы уже сложены по repeat_done — иначе последний
        // прогон посчитался бы дважды.
        if (e.metrics && !(e.repeats > 1)) {
          if (e.metrics.cost_usd) runTotals.cost += e.metrics.cost_usd;
          if (e.metrics.total_tokens) runTotals.tokens += e.metrics.total_tokens;
        }
        if (e.repeats > 1) {
          // Серия, из которой не выжил ни один прогон, — это провал колонки,
          // а не «готово»: ответов ноль, и строка состояния обязана это
          // сказать, иначе она противоречит красным блокам в ленте.
          if (!(e.texts || []).length) {
            setStatus(col, "error", `ни один прогон не удался — 0 из ${e.repeats}`);
            col.statsNote.textContent = "метрик удачных прогонов нет";
          } else {
            col.statsNote.textContent = `метрики последнего прогона из ${e.repeats}`;
            updateUniq(col);
          }
        }
        if (!col.status.classList.contains("error")) setStatus(col, "", "готово · можно спрашивать дальше");
        setBusy(col, false);
      }
      if (track) {
        track.text = e.text || track.text;
        laneTail(track);
        laneFoot(track, e.metrics, "готово");
      }
      runTotals.done += 1;
      break;

    case "judge_start": startVerdict(e); break;
    case "judge_delta": appendVerdict(e.text); break;
    case "judge_done": finishVerdict(e); break;
    case "judge_error": failVerdict(e.message); break;
    case "judge_skipped": skipVerdict(e.message); break;

    case "run_done":
      renderSummary(runTotals, e.wall_clock_ms);
      if (state.runBlock) state.runBlock.state.textContent = "прогон закончен";
      break;

    case "command_done":
      if (state.runBlock) {
        state.runBlock.foot.textContent = "сводка прогона дописана в память агента — следующий вопрос её увидит";
      }
      resetRunUi();
      refreshRegistry();
      break;

    case "error":
      appendChat("failed", e.message);
      if (state.runBlock) state.runBlock.state.textContent = "прогон не состоялся";
      resetRunUi();
      break;
  }
}

// Сравнение колонок имеет смысл только когда колонок больше одной: «самая
// быстрая» на единственной колонке сравнивать не с чем. На прерванном прогоне
// сравнения нет вовсе — часть колонок не отработала, и победитель среди
// уцелевших сказал бы неправду. Остаются итоги по тому, что успело досчитаться.
function renderSummary(totals, wallClockMs, interrupted) {
  const rows = [
    ["суммарная стоимость", fmtCost(totals.cost)],
    ["суммарно токенов", String(totals.tokens)],
    [interrupted ? "wall-clock до обрыва" : "wall-clock", fmtMs(wallClockMs)],
  ];
  if (interrupted) {
    rows.unshift(["прогон", `прерван · ${totals.done} из ${totals.expected} колонок`]);
  }
  // Отдельной строкой: видно, во что обошёлся вердикт, и это не смешано
  // с суммой по колонкам.
  if (state.judge && state.judge.cost !== null && state.judge.cost !== undefined) {
    rows.push(["стоимость судьи", fmtCost(state.judge.cost)]);
  }

  if (!interrupted && state.columns.size > 1) {
    const cols = [...state.columns.entries()]
      .map(([label, c]) => ({ label, m: c.lastMetrics }))
      .filter((x) => x.m && !x.m.error);
    const fastest = cols
      .filter((x) => x.m.ttft_ms !== null && x.m.ttft_ms !== undefined)
      .sort((a, b) => a.m.ttft_ms - b.m.ttft_ms)[0];
    const cheapest = cols
      .filter((x) => x.m.cost_usd !== null && x.m.cost_usd !== undefined)
      .sort((a, b) => a.m.cost_usd - b.m.cost_usd)[0];
    if (fastest) rows.push(["самая быстрая", `${fastest.label} · ${fmtMs(fastest.m.ttft_ms)}`]);
    if (cheapest) rows.push(["самая дешёвая", `${cheapest.label} · ${fmtCost(cheapest.m.cost_usd)}`]);
  }

  const box = $("#summary");
  box.classList.remove("hidden");
  box.innerHTML = "";
  rows.forEach(([k, v]) => {
    const cell = document.createElement("div");
    cell.innerHTML = '<div class="k"></div><div class="v"></div>';
    cell.querySelector(".k").textContent = k;
    cell.querySelector(".v").textContent = v;
    box.appendChild(cell);
  });
}

// --- сворачивание сайдбара ------------------------------------------------

// Во время записи список сценариев нужен только в момент выбора: дальше это
// ширина, которой не хватает колонкам. Состояние переживает перезагрузку —
// после `--reload` ведущему не приходится сворачивать заново.
const SIDEBAR_KEY = "ui.sidebar.collapsed";

function readCollapsed() {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch (e) {
    return false;   // приватный режим или запрет на хранилище — не повод падать
  }
}

function applySidebar(collapsed) {
  const btn = $("#sidebar-toggle");
  $("#sidebar").classList.toggle("collapsed", collapsed);
  btn.textContent = collapsed ? "›" : "‹";
  btn.title = collapsed ? "Показать сценарии" : "Свернуть сценарии";
  btn.setAttribute("aria-label", btn.title);
  btn.setAttribute("aria-expanded", String(!collapsed));
}

function initSidebar() {
  applySidebar(readCollapsed());
  $("#sidebar-toggle").onclick = () => {
    const collapsed = !$("#sidebar").classList.contains("collapsed");
    applySidebar(collapsed);
    try {
      localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
    } catch (e) {
      /* не сохранилось — свернуть всё равно можно, просто забудется */
    }
  };
}

function initChat() {
  const input = $("#chat-input");
  input.title = "Enter — отправить, Shift+Enter — перенос строки";
  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#chat-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    sendChat();
  });
  $("#chat-reset").onclick = () => newChatAgent(state.chat ? state.chat.rosterIndex : 0);
  setChatBusy(false);
}

initSidebar();
initChat();
loadDay();
