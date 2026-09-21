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

  // Потеря фокуса, как в браузере: сначала blur, следом focusout — и change,
  // но только если значение поменялось. Иначе проверке пришлось бы дёргать
  // `change` руками, то есть проверять свою догадку о браузере.
  //
  // `next` — узел, **получающий** фокус: так браузер и отличает «ушли со
  // страницы» от «щёлкнули по соседнему полю той же формы». Едет он полем
  // `relatedTarget`, как в браузере, и у `focusout` он единственный способ
  // узнать, что фокус остался внутри блока. Без аргумента — фокус потерян
  // вовсе, и `relatedTarget` пуст, тоже как в браузере.
  blur(next) {
    if (documentRef.activeElement === this) documentRef.activeElement = next || null;
    const changed = this._valueAtFocus !== null && this._valueAtFocus !== this.value;
    this._valueAtFocus = null;
    const to = next || null;
    this.dispatchEvent(new Evt("blur", { relatedTarget: to }));
    // `focusout` всплывает — на этом и держится правка записи памяти: поле
    // с текстом и список типов лежат в одном блоке, и слушает он блок, а не
    // каждое поле по отдельности.
    this.dispatchEvent(new Evt("focusout", { relatedTarget: to }));
    if (changed) this.dispatchEvent(new Evt("change"));
  }

  // Лежит ли узел внутри этого — как `Node.contains` в браузере, включая
  // «сам в себе». Пустой аргумент — `false`: фокус, ушедший в никуда,
  // внутри блока не остался.
  contains(node) {
    let walk = node || null;
    while (walk) {
      if (walk === this) return true;
      walk = walk.parentElement;
    }
    return false;
  }

  select() {}
  requestSubmit() {
    this.dispatchEvent(new Evt("submit"));
  }
}

// Инлайновый обработчик (`el.onclick = ...`) — такой же слушатель, только
// в единственном экземпляре. Держим его в общем списке, чтобы порядок
// вызова совпадал с браузерным.
const INLINE_EVENTS = [
  "click", "change", "keydown", "keyup", "submit", "input", "scroll",
  "blur", "focus", "focusout",
];

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
    // Служебный вызов перед ответом: словарь или функция (номер обмена) →
    // null | `{insert, covered, strategy}`. Задано — обмен идёт так, как на
    // сервере идёт обмен со сжатием: сперва кадр `compressing` (служебный
    // вызов уже пошёл, ответа ещё нет), потом `start` с промптом, где вместо
    // начала разговора стоит врезка.
    //
    // `strategy` называет, какой это был вызов, — ровно как сервер: клиент по
    // нему подписывает и строку состояния, и роль врезки в просмотре промпта.
    // Не задана — сводка, с неё служебные вызовы начались.
    service: (options && options.service) || null,
    // Вызовы инструментов на обмене: список или функция (номер обмена) →
    // список `{name, ok, message, plan}`. Кадр приходит на **исполненный**
    // вызов, и план в нём уже новый — как на сервере, где `plan_view()`
    // читается прямо на месте правки.
    tools: (options && options.tools) || null,
    // Врезка рабочей памяти: готовая строка или null. Своя, а не часть
    // `service`, потому что и на сервере она своя: записи вписывает человек,
    // и едут они при любом варианте обрезки, а не по заказу стратегии.
    // Служебного вызова за ней нет — и кадра `compressing` про неё тоже.
    facts: (options && options.facts) || null,
    // Долговременная память: готовая врезка строкой или null. Слой глобальный
    // и наполняется руками, поэтому у стенда он один на все чаты.
    memory: (options && options.memory) || null,
    // Записи долговременной памяти: список на всю базу, как на сервере, и
    // не привязанный ни к одному чату. Врезка выше — уже готовая строка,
    // а это то, что вкладка «Память» показывает, добавляет и удаляет.
    // Автора у записи нет: пишет в этот слой только человек, и поле,
    // у которого одно значение, не различало бы ничего.
    records: ((options && options.records) || []).map((seed, i) => ({
      seq: i + 1, at: i, ...seed,
    })),
    // Секрет, который сервер вырезает из всего, что уезжает в базу
    // (`redact`, `app/store.py`). Стенд чистит тем же способом — подменой
    // на «***» — и только когда секрет задан, ровно как сервер без ключа
    // не чистит ничего. Без этого половина инварианта «список пополняется
    // ответом ручки, а не присланным телом» через интерфейс ненаблюдаема:
    // присланное и записанное совпадали бы посимвольно.
    secret: (options && options.secret) || "",
    // Ручка трёх слоёв отвечает отказом: так проверяется, что вкладка говорит
    // причину, а не показывает пустые разделы.
    failLayers: Boolean(options && options.failLayers),
    // Рабочий слой чата: записи о состоянии задачи и сводки. Лежит отдельно
    // от самого чата —
    // ровно как на сервере, где у них свои таблицы, а не колонка в `sessions`.
    // Ключ — имя чата: чаты стенд и так заводит по именам.
    //
    // Записи материализуются один раз, при сборке, и дальше **живут**: их
    // правит и удаляет вкладка, и ответ ручки обязан показывать записанное.
    // Собери стенд их заново в каждом ответе — правка исчезала бы к следующему
    // чтению, и проверка правки прошла бы на подложном равенстве.
    working: {},
    models: [
      { id: "первая/модель", supported_parameters: [], prompt_price_per_m: 0, completion_price_per_m: 0 },
      { id: "вторая/модель", supported_parameters: [], prompt_price_per_m: 0, completion_price_per_m: 0 },
    ],
  };

  const SAMPLING = [
    "temperature", "max_tokens", "top_p", "top_k", "min_p",
    "repetition_penalty", "presence_penalty", "frequency_penalty",
  ];

  // Управление контекстом едет наружу тем же путём, что и сэмплирование:
  // панель обязана отличать пустое окно памяти («не режем») от нуля. Здесь
  // это карта «поле → умолчание»: у стратегии умолчание не пустое, а `full`,
  // как в `AgentSpec`, — «не выбрано» у неё состояния нет.
  const CONTEXT = { strategy: "full", keep_last: null, compress_every: null, workflow: "off" };

  // ── состояние задачи ──
  //
  // План и есть состояние: список шагов и три флажка. Этап и текущий шаг
  // не хранятся ни здесь, ни на сервере — они **вычисляются** (`plan.stage_of`),
  // и стенд считает их теми же правилами и тем же порядком. Ярлык этапа,
  // лежащий рядом с работой, расходится с работой молча.
  const emptyPlan = () => ({ steps: [], approved: false, finished: false, paused: false });

  // Текущий — тот, что `in_progress`; его нет — первый не-`done`. Одно место
  // на оба правила, как на сервере.
  function currentStep(plan) {
    const steps = ((plan || {}).steps) || [];
    const active = steps.findIndex((s) => s.status === "in_progress");
    if (active >= 0) return active;
    const undone = steps.findIndex((s) => s.status !== "done");
    return undone >= 0 ? undone : null;
  }

  // Разбор упорядоченный, а не поиск по таблице: условия этапов пересекаются.
  // План неутверждённый, но со всеми шагами `done` подходит и под `approval`,
  // и под `validation` сразу, и порядок решает, какой из них правда.
  function stageOf(plan) {
    const steps = ((plan || {}).steps) || [];
    const current = currentStep(plan);
    if ((plan || {}).paused) return ["paused", current];
    if ((plan || {}).finished) return ["done", current];
    if (!steps.length) return ["planning", null];
    if (!(plan || {}).approved) return ["approval", current];
    if (steps.every((s) => s.status === "done")) return ["validation", current];
    return ["execution", current];
  }

  const deepCopy = (value) => JSON.parse(JSON.stringify(value));

  // План так, как его отдаёт сервер: сам список плюс вычисленные `stage`
  // и `current`. Одна форма на тело чата, на ручки кнопок и на кадр `tool`.
  function planView(plan) {
    const [stage, current] = stageOf(plan);
    return { ...deepCopy(plan || emptyPlan()), stage, current };
  }

  // Пять переходов человека — с теми же условиями, что у `plan.apply`, и тем же
  // кодом отказа: стенд, отвечающий 200 на `approve` не на том этапе, оставил
  // бы зелёной кнопку, которая в браузере краснеет.
  //
  // Текст отказа — серверный, директивой **для модели**: человеческий текст
  // по коду 409 выбирает сам клиент, и подсунь ему стенд готовую человеческую
  // строку, проверять было бы нечего.
  function planApply(plan, action) {
    const [stage] = stageOf(plan);
    const deny = (why) => ({
      error: action + " НЕ выполнен, состояние не изменилось: " + why +
             " НЕ утверждай, что это сделано. Допустимо: …",
    });
    if (action === "approve") {
      if (stage !== "approval") return deny("этап сейчас " + stage + ", а не approval.");
      return { plan: { ...plan, approved: true } };
    }
    if (action === "reopen") {
      if (!plan.finished) return deny("задача не завершена.");
      return { plan: { ...plan, finished: false } };
    }
    if (action === "pause") {
      if (plan.paused) return deny("задача уже на паузе.");
      if (plan.finished) return deny("задача завершена, приостанавливать нечего.");
      return { plan: { ...plan, paused: true } };
    }
    if (action === "resume") {
      if (!plan.paused) return deny("задача не на паузе, снимать нечего.");
      return { plan: { ...plan, paused: false } };
    }
    // `reset` законен всегда: на нём держится и «начать другую задачу»,
    // и «вернуться в обычный чат».
    return { plan: emptyPlan() };
  }
  const PLAN_MOVES = ["approve", "reopen", "pause", "resume", "reset"];

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
    // Происхождение: у чата, заведённого сам по себе, его нет — `null`,
    // как на сервере. У ветки — `{parent_id, forked_at}`, и имени родителя
    // в нём нет: клиент находит его сам по id.
    branch: null,
    // Состояние задачи: ключ есть у чата **всегда**, и у выключенного тоже —
    // ровно как на сервере, где `workflow` и `plan` едут одним телом и
    // разъехаться внутри него не вправе.
    plan: emptyPlan(),
    ...Object.fromEntries(SAMPLING.map((n) => [n, null])),
    ...CONTEXT,
  });

  // Счётчик, как на сервере: только растёт и номера не переиспользует.
  // Иначе после удаления чата новый получил бы тот же id и то же имя, и
  // проверка «после удаления появился свежий» прошла бы на подложном равенстве.
  let issued = 0;
  const nextId = () => "ag_" + (issued += 1);

  // Номера записей памяти, как на сервере: AUTOINCREMENT. Только растут и
  // номер удалённой заново не выдаётся — иначе вторая вкладка удалила бы
  // не ту запись, и проверка этого не заметила бы.
  let issuedMemory = state.records.length;
  const MEMORY_KINDS = ["profile", "decision", "knowledge"];
  const WORKING_KINDS = ["goal", "limit", "decision", "question"];

  // Номера рабочих записей — один счётчик на всю базу, как на сервере:
  // у `working_memory` первичный ключ AUTOINCREMENT, а чат в ней колонкой.
  let issuedWorking = 0;
  Object.entries((options && options.working) || {}).forEach(([label, seed]) => {
    state.working[label] = {
      records: (seed.records || []).map((record, i) => ({
        seq: (issuedWorking += 1), at: i, ...record,
      })),
      summaries: seed.summaries || [],
    };
  });

  // Чат так, как его отдаёт сервер. План наружу едет вычисленным: `stage`
  // и `current` — ответ правил, а не колонка.
  const view = (agent) => ({ ...agent, plan: planView(agent.plan) });

  // Рабочая память чата, которому её не сеяли: слой есть у каждого, просто
  // пустой — ровно как на сервере.
  const workingOf = (agent) => {
    const have = state.working[agent.label];
    if (have) return have;
    state.working[agent.label] = { records: [], summaries: [] };
    return state.working[agent.label];
  };

  // Итог по чату считает сервер, и стенд считает его теми же правилами:
  // реплика без метрик и поле `null` пропускаются, а чат без чисел даёт `null`,
  // а не нули. Клиенту сумму складывать нельзя — он её только показывает.
  const USAGE_FIELDS = ["prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"];
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
    // Число сообщений считает сервер — это длина истории, реплика к реплике.
    // Заданное в seed не трогаем: им проверяется, что клиент показывает
    // серверное число, а не пересчитывает стенограмму сам.
    if (!("history_len" in seed)) agent.history_len = (agent.transcript || []).length;
    state.agents.push(agent);
  });

  const config = (agent) => {
    const out = { model: agent.model, system: agent.system,
                  stop: agent.stop, response_format: agent.response_format };
    SAMPLING.forEach((n) => { out[n] = agent[n]; });
    Object.keys(CONTEXT).forEach((n) => { out[n] = agent[n]; });
    return out;
  };

  const json = (data) => ({
    ok: true,
    status: 200,
    json: async () => JSON.parse(JSON.stringify(data)),
  });

  const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });

  // Секрет сервер вырезает из всего, что уезжает в базу (`redact`), и ручка
  // отдаёт записанное, а не присланное. Стенд чистит тем же способом.
  const clean = (text) =>
    (state.secret ? text.trim().split(state.secret).join("***") : text.trim());

  // Разбор тела записи — один на оба слоя, как `_record_body` на сервере:
  // лишние поля 400, тип обязателен и **без умолчания**, текст непустой,
  // пустое тело правки 400. Вторая копия правил на втором слое разошлась бы
  // с первой молча, а слой с разбором щедрее серверного прятал бы дыру:
  // форма осталась бы зелёной, получая в браузере 400.
  function recordBody(body, kinds, patch) {
    const keys = Object.keys(body || {});
    const unknown = keys.filter((k) => k !== "kind" && k !== "content");
    if (unknown.length) return { error: fail(400, "лишние поля: " + unknown.join(", ")) };
    if (patch && !keys.length) {
      return { error: fail(400, "тело правки пустое: назовите kind, content или оба") };
    }
    const out = {};
    if (!patch || keys.includes("kind")) {
      const kind = body && body.kind;
      if (typeof kind !== "string" || !kinds.includes(kind)) {
        return { error: fail(400, "kind: одно из " + kinds.join(", ")) };
      }
      out.kind = kind;
    }
    if (!patch || keys.includes("content")) {
      const content = body && body.content;
      if (typeof content !== "string" || !content.trim()) {
        return { error: fail(400, "content: непустая строка") };
      }
      out.content = clean(content);
    }
    return { value: out };
  }

  // Промпт стенд собирает в том же порядке, в каком его собирает сервер, и
  // шлёт кадром `start` всегда — как настоящий сервер. Стенд режет историю
  // так же: иначе проверить, что кнопка промпта показывает у окна уехавшее,
  // было бы не на чем — полная история в ленте и так лежит.
  //
  // Врезка долговременной памяти этого чата — или null. Одно условие на три
  // случая, как на сервере: памяти нет вовсе или выключатель чата в «off».
  const memoryInsert = () => state.memory;

  const factsInsert = () => state.facts;

  // Блок задачи для промпта: список шагов со статусами. Форма своя — важен
  // не текст, а то, что блок стоит в промпте на своём месте и подписан
  // клиентом по номеру слота, а не по словам внутри.
  const planInsert = (agent) =>
    "[задача]\n" +
    ((((agent.plan || {}).steps) || []).map((step, i) =>
      (i + 1) + ". " + step.title + " — " + step.status).join("\n") || "плана ещё нет");

  // Начало промпта — сообщения **до** истории и номер каждой врезки в них,
  // одним ответом. Формула слота здесь не считается, а берётся из длины уже
  // собранного начала: сервер делает так же (`Agent.prompt_head`), и по той же
  // причине — сумма предыдущих врезок, переписанная вторым местом, расходится
  // молча. Стенд от сервера нарочно независим, но две копии формулы **внутри
  // стенда** независимостью не являются.
  //
  // Порядок тот же, что на сервере: долговременная память, рабочая, блок
  // задачи, врезка стратегии. От общего к частному — и обе памяти с блоком
  // задачи едут при любом варианте обрезки, ни одну из них не отменяя.
  function promptHead(agent, service) {
    const messages = [];
    const slots = { memory_at: null, working_at: null, plan_at: null, summary_at: null };
    if (agent.system) messages.push({ role: "system", content: agent.system });

    const memory = memoryInsert();
    if (memory) {
      slots.memory_at = messages.length;
      messages.push({ role: "user", content: memory });
    }
    const facts = factsInsert();
    if (facts) {
      slots.working_at = messages.length;
      messages.push({ role: "user", content: facts });
    }
    // Блок задачи — при включённом рабочем процессе **всегда**, даже
    // с пустым списком шагов: без него модель не знает, что план вообще
    // ведётся. При выключенном слот пуст, и в промпте не меняется ни слова.
    if (agent.workflow === "plan") {
      slots.plan_at = messages.length;
      messages.push({ role: "user", content: planInsert(agent) });
    }
    // Окно — единственная стратегия, которая режет **без** врезки: вместо
    // отброшенного начала не встаёт ничего, слот у неё пуст, и в промпте
    // остаётся ровно хвост.
    if (service) {
      slots.summary_at = messages.length;
      messages.push({ role: "user", content: service.insert });
    }
    return { messages, slots };
  }

  function resolvedPrompt(agent, text, service) {
    const { messages } = promptHead(agent, service);
    const tail = (agent.transcript || []).map((t) => ({ role: t.role, content: t.content }));
    if (service) {
      tail.splice(0, service.covered || 0);
    } else if (agent.strategy === "window" && typeof agent.keep_last === "number") {
      // Пустое поле — отбрасывать нечем, ровно как на сервере.
      tail.splice(0, Math.max(0, tail.length - agent.keep_last));
    }
    return [...messages, ...tail, { role: "user", content: text }];
  }

  // Чем занят служебный вызов — тем же словом, каким это называет сервер.
  const serviceStrategy = (service) => (service && service.strategy) || "summary";

  // Кадр `start` — **один** на оба потока: и на удачный обмен, и на упавший.
  // Слоты в нём не пересчитываются: их отдаёт та же сборка начала промпта,
  // что этот промпт и собрала. Слот упавшего обмена через интерфейс
  // ненаблюдаем вовсе — карточка падения промпта не показывает, — и вторую
  // копию формулы не поймало бы ничто.
  function startFrame(agent, text, resolved, service) {
    return {
      event: "start",
      agent: agent.id,
      question: text,
      resolved_messages: resolved,
      ...promptHead(agent, service).slots,
      strategy: service ? serviceStrategy(service) : agent.strategy,
    };
  }

  // Кадры исполненных вызовов. План стенд **применяет** к агенту, а не
  // подставляет в ответ: собери он его заново в каждом кадре, следующий GET
  // вернул бы прежний, и утверждение «галочка сдвинулась» прошло бы
  // на подложном равенстве.
  function toolCalls(agent, index) {
    const calls = typeof state.tools === "function" ? state.tools(index) : state.tools;
    return (calls || []).map((call) => {
      if (call.plan) agent.plan = deepCopy(call.plan);
      return {
        event: "tool",
        agent: agent.id,
        name: call.name,
        ok: call.ok !== false,
        message: call.message || "",
        plan: planView(agent.plan),
      };
    });
  }

  function sse(agent, text) {
    // Записываем конфиг в момент прихода запроса: именно он уехал бы в модель.
    const index = state.sent.length;
    state.sent.push({ id: agent.id, text, config: config(agent) });
    const failed = typeof state.fail === "function" ? state.fail(index) : state.fail;
    const service = typeof state.service === "function" ? state.service(index) : state.service;
    // Промпт собирается до записи обмена в стенограмму: в модель уехало то,
    // что было в истории **до** этого вопроса.
    const resolved = resolvedPrompt(agent, text, service);
    // Кадр `start` собирается **до** вызовов инструментов: блок задачи в нём
    // тот, что уехал в модель, а не тот, что получился после правок. Сервер
    // ведёт себя так же — кадр `start` на обмене один, и промпт следующих
    // оборотов не пересобирается.
    const start = startFrame(agent, text, resolved, service);
    const toolFrames = toolCalls(agent, index);
    // Упавший обмен получает те же кадры до места падения: сервер сворачивает
    // и собирает промпт **до** вызова, и о том, что вызов потом упал, кадр
    // `start` знать не может. Подай стенд у падения пустой промпт — и клиент,
    // раздающий чужие промпты упавших обменов, остался бы зелёным.
    if (failed) return errorStream(agent, text, failed, service, start, toolFrames);
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

    // Кадр `metrics` настоящий сервер шлёт только когда числа пришли:
    // пустого кадра с `metrics: null` там не бывает, и здесь его тоже нет.
    const frames = [
      // Место врезки в промпте и чем она занята называет сервер — клиент не
      // разбирает текст сообщений. Кадр `compressing` приходит только
      // на сворачивание: служебный вызов на обмене остался один.
      ...(service ? [{ event: "compressing", agent: agent.id, strategy: serviceStrategy(service) }] : []),
      start,
      // Кадры вызовов стоят **между** `start` и `delta`: вызов исполнен,
      // результат уехал модели, и слово она скажет следующим оборотом.
      ...toolFrames,
      { event: "delta", text: state.reply, metrics: null },
      ...(usage ? [{ event: "metrics", metrics }] : []),
      { event: "done", text: state.reply, reasoning: "", metrics: usage ? metrics : null, committed: true },
    ].map((e) => "data: " + JSON.stringify(e) + "\n\n");

    return streamOf(frames);
  }

  // Обмен, упавший на провайдере: в историю он не пишется — текста нет,
  // а `usage_total` и число обменов остаются прежними, как на сервере.
  function errorStream(agent, text, failed, service, start, toolFrames) {
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
      ...(service ? [{ event: "compressing", agent: agent.id, strategy: serviceStrategy(service) }] : []),
      start,
      // Вызов мог успеть исполниться и до падения: то, что исполнилось,
      // остаётся исполненным — откат задним числом был бы враньём про то,
      // что модель видела.
      ...(toolFrames || []),
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
        agents: state.agents.map((a) => ({ ...view(a), transcript: undefined })),
      });
    }
    if (path === "/api/agents" && method === "POST") {
      const id = nextId();
      const agent = blank(id, "Новый чат " + id.slice(3));
      state.agents.push(agent);
      return json({ created: 1, agents: [view(agent)] });
    }

    // ── долговременная память: ручки глобальные, без agent_id ──
    //
    // Проверки границы здесь те же, что на сервере, и это не придирка:
    // стенд, принимающий то, чего сервер не принимает, оставляет зелёной
    // форму, которая в браузере получает 400. Тип записи поэтому обязателен
    // и умолчания не имеет — ровно как `_kind_field`.
    if (path === "/api/memory" && method === "GET") {
      return json({ total: state.records.length, records: state.records });
    }
    if (path === "/api/memory" && method === "POST") {
      const parsed = recordBody(body, MEMORY_KINDS, false);
      if (parsed.error) return parsed.error;
      // Автор в теле не спрашивается и прийти оттуда не может: его ставит
      // путь, которым запись попала в память. Ручка человека — человеком.
      const record = { seq: (issuedMemory += 1), ...parsed.value,
                       at: state.records.length };
      state.records.push(record);
      return json(record);
    }
    const memoryMatch = /^\/api\/memory\/(\d+)$/.exec(path);
    // Правка долговременной записи: номер не меняется — он и есть её
    // идентичность, — а автором становится человек, как на сервере.
    if (memoryMatch && method === "PATCH") {
      const seq = Number(memoryMatch[1]);
      const record = state.records.find((r) => r.seq === seq);
      if (!record) return fail(404, "записи памяти " + seq + " нет");
      const parsed = recordBody(body, MEMORY_KINDS, true);
      if (parsed.error) return parsed.error;
      Object.assign(record, parsed.value);
      return json(record);
    }
    if (memoryMatch && method === "DELETE") {
      const seq = Number(memoryMatch[1]);
      const i = state.records.findIndex((r) => r.seq === seq);
      if (i < 0) {
        return { ok: false, status: 404, json: async () => ({ detail: "записи памяти " + seq + " нет" }) };
      }
      state.records.splice(i, 1);
      return json({ deleted: seq });
    }

    const match = /^\/api\/agents\/([^/]+)(\/.*)?$/.exec(path);
    if (!match) return { ok: false, status: 404, json: async () => ({ detail: "нет такой ручки" }) };
    const agent = state.agents.find((a) => a.id === match[1]);
    if (!agent) return { ok: false, status: 404, json: async () => ({ detail: "агента нет" }) };
    const tail = match[2] || "";

    if (tail === "/messages" && method === "POST") return sse(agent, body.text);
    if (tail === "/regenerate" && method === "POST") return sse(agent, "перегенерация");
    // Ветвление: новый чат с копией начала истории и копией конфига — ровно
    // то, что делает сервер. Ответ в том же виде, что у создания: ветка и
    // есть обычный чат.
    if (tail === "/fork" && method === "POST") {
      const at = body && body.at;
      if (typeof at !== "number" || !Number.isInteger(at) || at < 0 || at > agent.transcript.length) {
        return {
          ok: false,
          status: 400,
          json: async () => ({ detail: "at: целое число от 0 до " + agent.transcript.length }),
        };
      }
      const id = nextId();
      const child = Object.assign(blank(id, "Ветка " + id.slice(3)), config(agent));
      child.transcript = agent.transcript.slice(0, at).map((turn) => ({ ...turn }));
      child.history_len = child.transcript.length;
      child.usage_total = sumUsage(child.transcript);
      child.branch = { parent_id: agent.id, forked_at: at };
      // План ветка уносит целиком, с флажками: задача у неё та же, и
      // утверждённый план обязан остаться утверждённым.
      child.plan = deepCopy(agent.plan);
      state.agents.push(child);
      return json({ created: 1, live: state.agents.length, agents: [view(child)] });
    }
    // Три слоя разом — тем же составом, что у сервера: счётчик сообщений
    // и сводки этого чата, его записи о задаче и общий на всю базу список.
    // Сводки едут с краткосрочным слоем, а не с рабочей памятью: сводка
    // не запомненное, а чем заменено то, что не уехало дословно.
    if (tail === "/memory" && method === "GET") {
      // Ручка умеет и отказать — занятая база отвечает 503, — и без этого
      // ветка «слои не доехали» через интерфейс ненаблюдаема: вкладке тогда
      // нечего показывать и некуда записывать.
      if (state.failLayers) return fail(503, "база занята: попробуйте ещё раз");
      const working = workingOf(agent);
      return json({
        short_term: {
          messages: agent.history_len,
          summaries: working.summaries || [],
        },
        working: { records: working.records },
        long_term: { records: state.records },
      });
    }
    // ── рабочая память: ручки под чатом, набор тот же, что у долговременной ──
    //
    // Границы те же, что на сервере, и разбор тела — общий с долговременным
    // слоем: тип обязателен и без умолчания, текст непустой, лишние поля 400,
    // пустое тело правки 400, чужой номер 404.
    if (tail === "/working" && method === "GET") {
      const working = workingOf(agent);
      return json({ total: working.records.length, records: working.records });
    }
    if (tail === "/working" && method === "POST") {
      const parsed = recordBody(body, WORKING_KINDS, false);
      if (parsed.error) return parsed.error;
      const working = workingOf(agent);
      const record = { seq: (issuedWorking += 1), ...parsed.value,
                       at: working.records.length };
      working.records.push(record);
      return json(record);
    }
    const workingMatch = /^\/working\/(\d+)$/.exec(tail);
    if (workingMatch && method === "PATCH") {
      const seq = Number(workingMatch[1]);
      const working = workingOf(agent);
      const record = working.records.find((r) => r.seq === seq);
      if (!record) return fail(404, "записи рабочей памяти " + seq + " в этом чате нет");
      const parsed = recordBody(body, WORKING_KINDS, true);
      if (parsed.error) return parsed.error;
      Object.assign(record, parsed.value);
      return json(record);
    }
    if (workingMatch && method === "DELETE") {
      const seq = Number(workingMatch[1]);
      const working = workingOf(agent);
      const i = working.records.findIndex((r) => r.seq === seq);
      if (i < 0) return fail(404, "записи рабочей памяти " + seq + " в этом чате нет");
      working.records.splice(i, 1);
      return json({ deleted: seq });
    }
    // ── состояние задачи: пять кнопок человека ──
    //
    // Отказ — 409, ровно как на сервере: тело здесь пустое и кривым быть
    // не может, а «не тот этап» это состояние, а не ошибка запроса.
    const planMatch = /^\/plan\/([a-z]+)$/.exec(tail);
    if (planMatch && method === "POST") {
      if (!PLAN_MOVES.includes(planMatch[1])) return fail(404, "нет такой ручки плана");
      // Занятый чат — тот же 409, что и «не тот этап», и это важно: два
      // разных повода под одним кодом. Обмен идёт на слепке конфига, но план
      // читает живой, и переход посреди него дал бы промпт одного этапа
      // с правилом другого.
      if (agent.busy) {
        return fail(409, "агент " + agent.id + " уже занят: дождитесь текущего ответа");
      }
      const moved = planApply(agent.plan, planMatch[1]);
      if (moved.error) return fail(409, moved.error);
      agent.plan = moved.plan;
      return json({ plan: planView(agent.plan) });
    }
    if (tail === "/cancel") return json({ cancelled: agent.id });
    if (!tail && method === "GET") return json(view(agent));
    if (!tail && method === "DELETE") {
      state.agents.splice(state.agents.indexOf(agent), 1);
      return json({ killed: [agent.id] });
    }
    if (!tail && method === "PATCH") {
      if ("model" in body && !String(body.model || "").trim()) {
        return { ok: false, status: 400, json: async () => ({ detail: "model обязателен" }) };
      }
      Object.assign(agent, body);
      return json(view(agent));
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
