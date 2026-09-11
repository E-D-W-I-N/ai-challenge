// Минимальный DOM и минимальный сервер, чтобы гонять НАСТОЯЩИЙ app.js под node.
//
// Здесь ровно столько DOM, сколько трогает app.js, и ровно столько сервера,
// сколько нужно, чтобы записать, с каким конфигом ушло сообщение. Ничего
// из этого не попадает в приложение: файл живёт в checks/.
//
// Правило стенда: **лучше упасть, чем соврать**. Там, где повторить браузер
// дёшево, стенд его повторяет, а где нельзя — бросает с внятным текстом
// вместо неправдоподобного ответа: тихо разошедшийся с браузером стенд даёт
// зелёное утверждение о том, что в браузере сломано.

const { TextEncoder } = require("util");

// Условная высота одного узла. Пикселей стенд не считает — ему довольно
// того, что содержимое имеет высоту, а пустота не имеет.
const NODE_HEIGHT = 40;

// ── события ───────────────────────────────────────────────────────────────

class Evt {
  constructor(type, extra) {
    this.type = type;
    this.target = null;
    this.defaultPrevented = false;
    Object.assign(this, extra || {});
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
  stopPropagation() {
    this.stopped = true;
  }
}

// ── узел ──────────────────────────────────────────────────────────────────

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.classes = new Set();
    this.listeners = {};
    this.id = "";
    this._value = "";
    this.title = "";
    this.disabled = false;
    this.selected = false;
    this._selectCleared = false;
    this.open = false;
    this._text = "";
    this._html = "";
    this._scrollTop = 0;
    this.clientHeight = 0;
    this._valueAtFocus = null;

    const self = this;
    this.classList = {
      add: (...names) => names.forEach((n) => self.classes.add(n)),
      remove: (...names) => names.forEach((n) => self.classes.delete(n)),
      contains: (name) => self.classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !self.classes.has(name) : Boolean(force);
        if (on) self.classes.add(name);
        else self.classes.delete(name);
        return on;
      },
    };
  }

  // Высоту содержимого браузер считает раскладкой, стенд — числом узлов.
  // Точных пикселей это не даёт и не должно: важно другое — лента без
  // сообщений не может оказаться выше экрана, а с сообщениями может.
  get scrollHeight() {
    return Math.max(this.clientHeight, this.contentHeight());
  }

  set scrollHeight(_value) {
    throw new Error(
      "scrollHeight в браузере не присваивают: он считается по содержимому. " +
        "Добавьте узлов в ленту или задайте clientHeight."
    );
  }

  contentHeight() {
    if (!this.children.length) {
      return this._text || this._html ? NODE_HEIGHT : 0;
    }
    return this.children.reduce((sum, child) => sum + Math.max(NODE_HEIGHT, child.contentHeight()), 0);
  }

  // Прокрутка зажата между нулём и «дальше некуда», как в браузере: без
  // этого проверка могла бы поставить ленту туда, куда та не доезжает,
  // и утверждение о положении стало бы бессмысленным.
  get scrollTop() {
    return this._scrollTop;
  }

  set scrollTop(value) {
    const limit = Math.max(0, this.scrollHeight - this.clientHeight);
    this._scrollTop = Math.min(Math.max(0, Number(value) || 0), limit);
  }

  // У <select> значение — это значение выбранного <option>, как в браузере.
  // Присваивание значения, которого среди опций нет, браузер не игнорирует:
  // он снимает выбор совсем (selectedIndex = -1), и `.value` становится
  // пустым. Стенд обязан вести себя так же — иначе поле молча оставалось бы
  // при старом значении.
  get value() {
    if (this.tagName === "SELECT") {
      if (this._selectCleared) return "";
      const chosen = this.children.find((c) => c.selected) || this.children[0];
      return chosen ? chosen.value : "";
    }
    return this._value;
  }
  set value(next) {
    if (this.tagName === "SELECT") {
      const wanted = String(next);
      const match = this.children.find((c) => c.value === wanted);
      this.children.forEach((c) => { c.selected = c === match; });
      this._selectCleared = !match;
      return;
    }
    this._value = next === null || next === undefined ? "" : String(next);
  }

  get selectedIndex() {
    if (this.tagName !== "SELECT") return -1;
    if (this._selectCleared) return -1;
    const chosen = this.children.findIndex((c) => c.selected);
    return chosen >= 0 ? chosen : (this.children.length ? 0 : -1);
  }

  get className() {
    return [...this.classes].join(" ");
  }
  set className(value) {
    this.classes = new Set(String(value || "").split(/\s+/).filter(Boolean));
  }

  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    // В браузере textContent видит и то, что положили через innerHTML:
    // разметка там разобрана в узлы. Стенд её не разбирает, поэтому снимает
    // теги — иначе утверждение о показанном тексте молча смотрело бы в пустоту.
    if (this._html) return stripTags(this._html);
    return this._text;
  }
  set textContent(value) {
    this.children.forEach((c) => (c.parentElement = null));
    this.children = [];
    this._html = "";
    this._text = value === null || value === undefined ? "" : String(value);
  }

  get innerHTML() {
    return this._html;
  }
  set innerHTML(value) {
    // app.js кладёт сюда либо "" (очистка), либо готовый markdown. Разбирать
    // разметку обратно в узлы не нужно: её проверяет отдельный блок утверждений.
    // Список опций пересобран — выбор возвращается к умолчанию, как в браузере.
    this._selectCleared = false;
    this.children.forEach((c) => (c.parentElement = null));
    this.children = [];
    this._text = "";
    this._html = String(value || "");
    // Браузер при подмене содержимого обнуляет прокрутку. Без этого стенд
    // врал бы в самом важном месте: лента, которую никто не поставил
    // явно, «сохраняла» бы позицию, которой в браузере уже нет.
    this.scrollTop = 0;
  }

  appendChild(node) {
    if (node.parentElement) node.parentElement.removeChild(node);
    node.parentElement = this;
    this.children.push(node);
    this._text = "";
    return node;
  }
  append(...nodes) {
    nodes.forEach((n) => this.appendChild(n));
  }
  removeChild(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) this.children.splice(i, 1);
    node.parentElement = null;
    return node;
  }
  remove() {
    if (this.parentElement) this.parentElement.removeChild(this);
  }
  insertBefore(node, before) {
    const i = this.children.indexOf(before);
    if (node.parentElement) node.parentElement.removeChild(node);
    node.parentElement = this;
    this.children.splice(i < 0 ? this.children.length : i, 0, node);
    return node;
  }
  replaceChild(fresh, old) {
    const i = this.children.indexOf(old);
    if (i < 0) return old;
    if (fresh.parentElement) fresh.parentElement.removeChild(fresh);
    fresh.parentElement = this;
    this.children[i] = fresh;
    old.parentElement = null;
    return old;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  matches(sel) {
    return parseSelector(sel).every((part) => {
      if (part.startsWith("#")) return this.id === part.slice(1);
      if (part.startsWith(".")) return this.classes.has(part.slice(1));
      return this.tagName === part.toUpperCase();
    });
  }

  walk(visit) {
    for (const child of this.children) {
      visit(child);
      child.walk(visit);
    }
  }

  querySelector(sel) {
    let found = null;
    this.walk((node) => {
      if (!found && node.matches(sel)) found = node;
    });
    return found;
  }
  querySelectorAll(sel) {
    const out = [];
    this.walk((node) => {
      if (node.matches(sel)) out.push(node);
    });
    return out;
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push({ handler });
  }

  // Событие всплывает по дереву и доходит до документа: на первом держится
  // делегирование `change` на #panel-body, на втором — Escape, который
  // приложение слушает на document.
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    let node = this;
    while (node) {
      // Обработчики идут в порядке подписки, и инлайновый `on...` —
      // такой же обработчик, а не всегда последний: браузер их не
      // переставляет, и стенд не должен.
      (node.listeners[event.type] || []).slice().forEach((entry) => {
        if (entry.handler) entry.handler.call(node, event);
      });
      if (event.stopped) break;
      node = node.parentElement;
    }
    if (!event.stopped && documentRef) documentRef.fire(event);
    return !event.defaultPrevented;
  }

  focus() {
    documentRef.activeElement = this;
    this._valueAtFocus = this.value;
  }

  // Потеря фокуса, как в браузере: сначала blur, а следом change — но
  // только если значение поменялось. Иначе проверке пришлось бы дёргать
  // `change` руками, то есть проверять свою догадку о браузере.
  blur() {
    if (documentRef.activeElement === this) documentRef.activeElement = null;
    const changed = this._valueAtFocus !== null && this._valueAtFocus !== this.value;
    this._valueAtFocus = null;
    this.dispatchEvent(new Evt("blur"));
    if (changed) this.dispatchEvent(new Evt("change"));
  }

  select() {}
  requestSubmit() {
    this.dispatchEvent(new Evt("submit"));
  }
}

// Инлайновый обработчик (`el.onclick = ...`) — такой же слушатель, только
// в единственном экземпляре. Держим его в общем списке, чтобы порядок
// вызова совпадал с браузерным.
const INLINE_EVENTS = ["click", "change", "keydown", "keyup", "submit", "input", "scroll", "blur", "focus"];

INLINE_EVENTS.forEach((type) => {
  Object.defineProperty(El.prototype, "on" + type, {
    get() {
      const entry = (this.listeners[type] || []).find((e) => e.inline);
      return entry ? entry.handler : null;
    },
    set(handler) {
      const list = (this.listeners[type] = this.listeners[type] || []);
      const entry = list.find((e) => e.inline);
      if (entry) entry.handler = handler;
      else list.push({ handler, inline: true });
    },
    configurable: true,
  });
});

// ── разбор селекторов ─────────────────────────────────────────────────────

// Стенд понимает одиночный селектор: тег, #id, .класс и их сочетание
// (`button.mini.danger`). Комбинаторы и всё остальное он повторить не может
// и потому бросает: селектор, молча нашедший ноль, — это зелёное утверждение
// о том, чего никто не проверил.
function parseSelector(sel) {
  const text = String(sel).trim();
  if (!text) throw new Error("пустой селектор");
  if (/[\s>+~,[\]:()*]/.test(text)) {
    throw new Error(
      `стенд не умеет селектор «${text}»: только тег, #id, .класс и их сочетание. ` +
        "Найдите узел иначе — например по ближайшему id, а потом обходом детей."
    );
  }
  // Режем по границам # и . — не по \w: имена классов бывают и не латиницей,
  // а класс, которого разбор не увидел, молча расширил бы совпадение.
  const parts = text.match(/[#.]?[^#.]+/g) || [];
  if (!parts.length || parts.some((part) => !part.replace(/^[#.]/, "").length)) {
    throw new Error(`не разобрать селектор «${text}»`);
  }
  return parts;
}

function stripTags(html) {
  return String(html)
    .replace(/<[^>]*>/g, "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&");
}

// ── документ ──────────────────────────────────────────────────────────────

let documentRef = null;

function buildDocument(html) {
  const root = new El("html");
  root.dataset = {};
  const body = new El("body");
  root.appendChild(body);

  // Разметку берём из настоящего index.html: только id, классы и вложенность —
  // больше app.js от неё ничего и не требует.
  const stack = [body];
  const tagRe = /<(\/?)([a-zA-Z][\w-]*)([^>]*?)(\/?)>/g;
  let match;
  let textFrom = 0;
  while ((match = tagRe.exec(html))) {
    const [, closing, tag, attrs, selfClose] = match;

    // Текст между тегами — тоже часть разметки: в шапке им написан номер дня,
    // и раньше стенд его не видел вовсе. Достаётся он только листу, у которого
    // ещё нет детей: узел, у которого textContent собирается из детей,
    // подписывать нечем.
    const between = html.slice(textFrom, match.index).trim();
    textFrom = tagRe.lastIndex;
    const holder = stack[stack.length - 1];
    if (between && !between.startsWith("<") && !holder.children.length) {
      holder.textContent = between;
    }

    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new El(tag);
    const idMatch = /\bid="([^"]+)"/.exec(attrs);
    if (idMatch) el.id = idMatch[1];
    const classMatch = /\bclass="([^"]+)"/.exec(attrs);
    if (classMatch) el.className = classMatch[1];
    const dataTab = /\bdata-tab="([^"]+)"/.exec(attrs);
    if (dataTab) el.dataset.tab = dataTab[1];
    // value у <option> — от него зависит значение всего <select>.
    const valueMatch = /\bvalue="([^"]*)"/.exec(attrs);
    if (valueMatch) el.value = valueMatch[1];
    if (/\bselected\b/.test(attrs)) el.selected = true;
    stack[stack.length - 1].appendChild(el);
    const voidTag = /^(meta|link|input|br|hr|img)$/i.test(tag);
    if (!selfClose && !voidTag) stack.push(el);
  }

  const document = {
    documentElement: root,
    body,
    activeElement: null,
    listeners: {},
    createElement: (tag) => new El(tag),
    createElementNS: (_ns, tag) => new El(tag),
    getElementById: (id) => root.querySelector("#" + id),
    querySelector: (sel) => (root.matches(sel) ? root : root.querySelector(sel)),
    querySelectorAll: (sel) => root.querySelectorAll(sel),
    addEventListener: (type, handler) =>
      (document.listeners[type] = document.listeners[type] || []).push(handler),
    removeEventListener: (type, handler) => {
      const list = document.listeners[type] || [];
      const i = list.indexOf(handler);
      if (i >= 0) list.splice(i, 1);
    },
    // Событие, всплывшее с элемента, и событие, посланное самому документу, —
    // одно и то же для слушателя на document.
    fire: (event) => {
      (document.listeners[event.type] || []).slice().forEach((h) => h(event));
      return event;
    },
  };
  documentRef = document;
  return document;
}

// ── сервер ────────────────────────────────────────────────────────────────

// Ровно те ручки, которые зовёт клиент, и ровно тот контракт, что у настоящих.
// Главное здесь — `sent`: с каким конфигом агента пришло каждое сообщение.
function buildServer(options) {
  const encoder = new TextEncoder();
  const state = {
    agents: [],
    sent: [],       // {id, text, config} — конфиг агента в момент запроса
    requests: [],   // {method, path, body}
    reply: "ответ модели",
    // Числа usage у каждого ответа: словарь или функция (номер обмена) → словарь.
    // Без них кадры стрима отдавали бы `metrics: null`, плитки в стенде всегда
    // показывали бы прочерк, и проверять там было бы нечего.
    usage: (options && options.usage) || null,
    // Упавший обмен: словарь или функция (номер обмена) → null | описание
    // падения `{message, metrics, done}`. Настоящая ошибка провайдера приходит
    // кадром `error`, у метрик которого **все числа пустые**, и следом — `done`
    // с теми же метриками. Кадр `done` можно погасить (`done: false`): поток
    // вправе оборваться на ошибке, и тогда перерисовать плитки некому, кроме
    // самой ветки ошибки.
    fail: (options && options.fail) || null,
    models: [
      { id: "первая/модель", supported_parameters: [], prompt_price_per_m: 0, completion_price_per_m: 0 },
      { id: "вторая/модель", supported_parameters: [], prompt_price_per_m: 0, completion_price_per_m: 0 },
    ],
  };

  const SAMPLING = [
    "temperature", "max_tokens", "top_p", "top_k", "min_p",
    "repetition_penalty", "presence_penalty", "frequency_penalty",
  ];

  const blank = (id, label) => ({
    id,
    label,
    model: state.models[0].id,
    system: "",
    stop: null,
    response_format: null,
    extra_body: {},
    history_len: 0,
    busy: false,
    transcript: [],
    usage_total: null,
    exchanges: 0,
    ...Object.fromEntries(SAMPLING.map((n) => [n, null])),
  });

  // Счётчик, как на сервере: только растёт и номера не переиспользует.
  // Иначе после удаления чата новый получил бы тот же id и то же имя, и
  // проверка «после удаления появился свежий» прошла бы на подложном равенстве.
  let issued = 0;
  const nextId = () => "ag_" + (issued += 1);

  // Итог по чату считает сервер, и стенд считает его теми же правилами:
  // реплика без метрик и поле `null` пропускаются, а чат без чисел даёт `null`,
  // а не нули. Клиенту сумму складывать нельзя — он её только показывает.
  const USAGE_FIELDS = ["prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"];
  // Обмены считает сервер и считает все ответы, а не только принёсшие числа.
  const countAnswers = (transcript) =>
    (transcript || []).filter((turn) => turn.role === "assistant").length;
  function sumUsage(transcript) {
    const out = { prompt_tokens: null, completion_tokens: null, total_tokens: null, cost_usd: null };
    let answers = 0;
    (transcript || []).forEach((turn) => {
      if (turn.role !== "assistant" || !turn.metrics) return;
      let counted = false;
      USAGE_FIELDS.forEach((name) => {
        const value = turn.metrics[name];
        if (typeof value !== "number") return;
        out[name] = out[name] === null ? value : out[name] + value;
        counted = true;
      });
      if (counted) answers += 1;
    });
    if (!answers) return null;
    return out;
  }

  // Чаты, заведённые заранее, — как если бы их создали руками до открытия.
  ((options && options.chats) || [{ label: "чат" }]).forEach((seed) => {
    const agent = Object.assign(blank(nextId(), seed.label), seed);
    if (!("usage_total" in seed)) agent.usage_total = sumUsage(agent.transcript);
    if (!("exchanges" in seed)) agent.exchanges = countAnswers(agent.transcript);
    state.agents.push(agent);
  });

  const config = (agent) => {
    const out = { model: agent.model, system: agent.system,
                  stop: agent.stop, response_format: agent.response_format };
    SAMPLING.forEach((n) => { out[n] = agent[n]; });
    return out;
  };

  const json = (data) => ({
    ok: true,
    status: 200,
    json: async () => JSON.parse(JSON.stringify(data)),
  });

  function sse(agent, text) {
    // Записываем конфиг в момент прихода запроса: именно он уехал бы в модель.
    const index = state.sent.length;
    state.sent.push({ id: agent.id, text, config: config(agent) });
    const failed = typeof state.fail === "function" ? state.fail(index) : state.fail;
    if (failed) return errorStream(agent, text, failed);
    // Числа приходят последним кадром, как настоящий usage от OpenRouter:
    // до него в кадрах их нет, и плитки показывают прочерк.
    const usage = typeof state.usage === "function" ? state.usage(index) : state.usage;
    const metrics = { model: agent.model, provider: "стенд", ...(usage || {}) };
    agent.transcript.push({ role: "user", content: text, error: null, reasoning: "", metrics: null });
    agent.transcript.push({
      role: "assistant", content: state.reply, error: null, reasoning: "",
      metrics,
    });
    agent.history_len = agent.transcript.length;
    agent.usage_total = sumUsage(agent.transcript);
    agent.exchanges = countAnswers(agent.transcript);

    // Кадр `metrics` настоящий сервер шлёт только когда числа пришли:
    // пустого кадра с `metrics: null` там не бывает, и здесь его тоже нет.
    const frames = [
      { event: "start", agent: agent.id },
      { event: "delta", text: state.reply, metrics: null },
      ...(usage ? [{ event: "metrics", metrics }] : []),
      { event: "done", text: state.reply, reasoning: "", metrics: usage ? metrics : null, committed: true },
    ].map((e) => "data: " + JSON.stringify(e) + "\n\n");

    return streamOf(frames);
  }

  // Обмен, упавший на провайдере: в историю он не пишется — текста нет,
  // а `usage_total` и число обменов остаются прежними, как на сервере.
  function errorStream(agent, text, failed) {
    const metrics = {
      model: agent.model,
      provider: null,
      error: failed.message,
      ttft_ms: null,
      elapsed_ms: 120.5,
      prompt_tokens: null,
      completion_tokens: null,
      total_tokens: null,
      cost_usd: null,
      context_fill_pct: null,
      // Провайдер вправе назвать часть чисел и в ошибке — тогда они тут.
      ...(failed.metrics || {}),
    };
    const frames = [
      { event: "start", agent: agent.id },
      { event: "error", agent: agent.id, message: failed.message, metrics },
      ...(failed.done === false
        ? []
        : [{
            event: "done", text: "", reasoning: "", metrics,
            error: failed.message, committed: false, question: text,
          }]),
    ].map((e) => "data: " + JSON.stringify(e) + "\n\n");
    return streamOf(frames);
  }

  function streamOf(frames) {
    let i = 0;
    const pause = (options && options.delay) || 0;
    return {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (pause) await new Promise((r) => setTimeout(r, pause));
            return i < frames.length
              ? { done: false, value: encoder.encode(frames[i++]) }
              : { done: true, value: undefined };
          },
        }),
      },
    };
  }

  async function fetchStub(path, init) {
    const method = ((init && init.method) || "GET").toUpperCase();
    const body = init && init.body ? JSON.parse(init.body) : null;
    state.requests.push({ method, path, body });

    if (path.startsWith("/api/models")) return json({ total: state.models.length, models: state.models });

    if (path === "/api/agents" && method === "GET") {
      return json({
        has_key: true,
        live: state.agents.length,
        max_agents: 1000,
        evicted: 0,
        agents: state.agents.map((a) => ({ ...a, transcript: undefined })),
      });
    }
    if (path === "/api/agents" && method === "POST") {
      const id = nextId();
      const agent = blank(id, "Новый чат " + id.slice(3));
      state.agents.push(agent);
      return json({ created: 1, agents: [agent] });
    }

    const match = /^\/api\/agents\/([^/]+)(\/.*)?$/.exec(path);
    if (!match) return { ok: false, status: 404, json: async () => ({ detail: "нет такой ручки" }) };
    const agent = state.agents.find((a) => a.id === match[1]);
    if (!agent) return { ok: false, status: 404, json: async () => ({ detail: "агента нет" }) };
    const tail = match[2] || "";

    if (tail === "/messages" && method === "POST") return sse(agent, body.text);
    if (tail === "/regenerate" && method === "POST") return sse(agent, "перегенерация");
    if (tail === "/cancel") return json({ cancelled: agent.id });
    if (!tail && method === "GET") return json(agent);
    if (!tail && method === "DELETE") {
      state.agents.splice(state.agents.indexOf(agent), 1);
      return json({ killed: [agent.id] });
    }
    if (!tail && method === "PATCH") {
      if ("model" in body && !String(body.model || "").trim()) {
        return { ok: false, status: 400, json: async () => ({ detail: "model обязателен" }) };
      }
      Object.assign(agent, body);
      return json(agent);
    }
    return { ok: false, status: 405, json: async () => ({ detail: "не тот метод" }) };
  }

  return { state, fetchStub };
}

// ── сборка окружения ──────────────────────────────────────────────────────

function boot(html, options) {
  const document = buildDocument(html);
  const server = buildServer(options);
  const store = {};

  globalThis.document = document;
  globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };
  globalThis.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  };
  globalThis.navigator = { clipboard: { writeText: async () => {} } };
  globalThis.fetch = server.fetchStub;

  return { document, server };
}

// Дать очереди микрозадач и таймерам добежать: клиент асинхронный насквозь.
const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

module.exports = { boot, settle, Evt, El };
