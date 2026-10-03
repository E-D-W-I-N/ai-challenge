// Минимальные DOM/события для настоящих клиентских скриптов под Node.
// Явные HTTP/SSE-ответы сценариев находятся в fixtures.js; серверные
// вычисления здесь не повторяются. CSS и геометрию этот стенд не проверяет.
//
// Правило стенда: **лучше упасть, чем соврать**. Там, где повторить браузер
// дёшево, стенд его повторяет, а где нельзя — бросает с внятным текстом
// вместо неправдоподобного ответа: тихо разошедшийся с браузером стенд даёт
// зелёное утверждение о том, что в браузере сломано.

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
  replaceChildren(...nodes) {
    for (const child of [...this.children]) this.removeChild(child);
    this._text = ""; this._html = "";
    this.append(...nodes);
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

  removeAttribute(name) {
    delete this.attributes[name];
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

    // Статический текст нужен формам и навигации. У родителя с детьми
    // textContent собирается из потомков, поэтому текст получает лист.
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

// Browser primitives only; scenario routes live separately from the DOM.
const { buildServer } = require("./fixtures.js");
function boot(html, options) {
  const document = buildDocument(html);
  const server = buildServer(options);
  const store = {};
  globalThis.document = document;
  const windowListeners = {};
  globalThis.window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener: (type, handler) => (windowListeners[type] ||= []).push(handler),
    dispatchEvent: (event) => (windowListeners[event.type] || []).slice().forEach((handler) => handler(event)),
  };
  globalThis.localStorage = {
    getItem: (key) => store[key] ?? null,
    setItem: (key, value) => { store[key] = String(value); },
  };
  globalThis.fetch = server.fetchStub;
  return { document, server };
}
const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
module.exports = { boot, settle, Evt, El };
