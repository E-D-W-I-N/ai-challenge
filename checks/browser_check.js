// Проверка клиентского кода настоящими вызовами, а не grep'ом по исходнику.
//
//     node checks/browser_check.js
//
// Разбор markdown — самая опасная поверхность демо: в него попадает текст
// от модели, и он же кладётся в innerHTML. Проверять его чтением исходника
// бессмысленно — переедет экранирование за разбор, и grep этого не заметит.
// Поэтому здесь настоящие payload'ы и утверждения про выход, а дальше —
// настоящий маршрут: правка в поле панели → отправка → тело запроса.
//
// app.js под node подключается модулем: в браузере `module` не существует,
// и файл просто запускается сам.

const path = require("path");

// Единственное, что app.js трогает на загрузке, — matchMedia для раскладки.
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };

const app = require(path.join(__dirname, "..", "app", "static", "app.js"));
const { renderMarkdown } = app;

const failures = [];
let passed = 0;

// Клиент асинхронный насквозь, и его исключение всплывает необработанным
// отказом промиса: без этого обработчика node просто убивает процесс, и
// от набора не остаётся ни одной строки — ни зелёной, ни красной.
process.on("unhandledRejection", (err) => {
  const first = err && err.stack ? String(err.stack).split("\n")[0] : String(err);
  failures.push("клиент уронил необработанный отказ промиса: " + first);
});

function check(name, condition, detail) {
  if (condition) passed += 1;
  else failures.push(`${name}${detail ? ": " + detail : ""}`);
}

function has(name, input, needle) {
  const out = renderMarkdown(input);
  check(name, out.includes(needle), `в выходе нет ${JSON.stringify(needle)} — ${out}`);
}

function hasNot(name, input, needle) {
  const out = renderMarkdown(input);
  check(name, !out.includes(needle), `в выходе есть ${JSON.stringify(needle)} — ${out}`);
}

// ── разметка из ответа модели не должна становиться разметкой страницы ──

hasNot("script экранируется", "<script>alert(1)</script>", "<script");
has("script остаётся текстом", "<script>alert(1)</script>", "&lt;script&gt;");
hasNot("img/onerror экранируется", '<img src=x onerror="alert(1)">', "<img");
hasNot("iframe экранируется", "<iframe src=evil></iframe>", "<iframe");
// Само слово onclick в экранированном тексте безвредно: проверяем, что
// тега не получилось, а не что подстроки нет.
hasNot("обработчик в тексте не оживает", '<div onclick="alert(1)">клик</div>', "<div");
has("амперсанд экранируется", "Тим & Стас", "&amp;");
has("кавычка экранируется", 'он сказал "да"', "&quot;");
hasNot("html внутри жирного тоже экранируется", "**<b>жирно</b>**", "<b>");
has("html внутри жирного остаётся текстом", "**<b>жирно</b>**", "<strong>&lt;b&gt;");
hasNot("html внутри блока кода экранируется", "```\n<script>x</script>\n```", "<script");
hasNot("html внутри inline-кода экранируется", "`<script>x</script>`", "<script");
hasNot("html в заголовке экранируется", "# <img src=x>", "<img");
hasNot("html в цитате экранируется", "> <img src=x>", "<img");
hasNot("html в пункте списка экранируется", "- <img src=x>", "<img");
hasNot("html в подписи ссылки экранируется", "[<img src=x>](https://ok.example)", "<img");

// ── ссылки: только http(s), и никаких схем-исполнителей ──

hasNot("javascript: в ссылку не превращается", "[клик](javascript:alert(1))", "<a ");
hasNot("data: в ссылку не превращается", "[клик](data:text/html,<script>x</script>)", "<a ");
hasNot("vbscript: в ссылку не превращается", "[клик](vbscript:msgbox)", "<a ");
hasNot("file: в ссылку не превращается", "[клик](file:///etc/passwd)", "<a ");
has("http-ссылка становится ссылкой", "[док](http://example.com/a)", '<a href="http://example.com/a"');
has("https-ссылка становится ссылкой", "[док](https://example.com/a)", '<a href="https://example.com/a"');
has("у ссылки есть rel", "[док](https://example.com)", 'rel="noreferrer noopener"');
has("у ссылки есть target", "[док](https://example.com)", 'target="_blank"');
has("подпись ссылки на месте", "[док](https://example.com)", ">док</a>");

// ── разметка разбирается ──

has("заголовок первого уровня", "# Заголовок", "<h1>Заголовок</h1>");
has("заголовок второго уровня", "## Второй", "<h2>Второй</h2>");
has("заголовок третьего уровня", "### Третий", "<h3>Третий</h3>");
has("жирный", "**важно**", "<strong>важно</strong>");
has("курсив", "*косо*", "<em>косо</em>");
has("inline-код", "вот `x = 1` тут", "<code>x = 1</code>");
has("блок кода", "```\nprint(1)\n```", "<pre><code>print(1)</code></pre>");
has("маркированный список", "- раз\n- два", "<ul><li>раз</li><li>два</li></ul>");
has("нумерованный список", "1. раз\n2. два", "<ol><li>раз</li><li>два</li></ol>");
has("цитата", "> мысль", "<blockquote>мысль</blockquote>");
has("разделитель", "---", "<hr>");
has("абзац", "просто текст", "<p>просто текст</p>");

// ── разбор не съедает и не путает содержимое ──

hasNot("разметка внутри inline-кода не разбирается", "`**не жирный**`", "<strong>");
has("подчёркивание в имени не делает курсив", "some_var_name", "some_var_name");
check(
  "два абзаца остаются двумя",
  (renderMarkdown("первый\n\nвторой").match(/<p>/g) || []).length === 2,
  renderMarkdown("первый\n\nвторой")
);
check(
  "пустой ввод даёт пустой выход",
  renderMarkdown("") === "" && renderMarkdown(null) === "",
  JSON.stringify([renderMarkdown(""), renderMarkdown(null)])
);
{
  // Метка, которой разбор подменяет inline-код: если модель пришлёт её
  // в тексте, подставить чужой кусок она не должна.
  const mark = String.fromCharCode(0);
  const out = renderMarkdown(`до ${mark}0${mark} после`);
  check("метка inline-кода из текста модели безвредна", !out.includes("<code>"), out);
  check("текст вокруг метки уцелел", out.includes("до") && out.includes("после"), out);
}

// ── маршрут целиком: правка в панели → отправка → тело запроса ──
//
// Инвариант: сообщение уходит с тем конфигом, что показан в панели.
// Проверяется не ручка, а код, который выполняется в браузере: `checks/dom.js`
// даёт минимальный DOM и минимальный сервер, записывающий, с каким конфигом
// ушло каждое сообщение.
//
// Правку вносим **без события `change`** — просто ставим значение в поле,
// как пользователь, ещё не убравший из него курсор: событие — единственный
// шанс доставить правку, и поводов его упустить сколько угодно.

const fs = require("fs");
const HTML = fs.readFileSync(path.join(__dirname, "..", "app", "static", "index.html"), "utf8");

// Поле панели → каким оно уезжает в конфиг агента.
const PANEL_ROUTE = [
  ["f-system", "НОВЫЙ ПРОМПТ", "system", "НОВЫЙ ПРОМПТ"],
  ["f-model", "вторая/модель", "model", "вторая/модель"],
  ["f-temperature", "0.9", "temperature", 0.9],
  ["f-max_tokens", "555", "max_tokens", 555],
  ["f-top_p", "0.8", "top_p", 0.8],
  ["f-top_k", "40", "top_k", 40],
  ["f-min_p", "0.05", "min_p", 0.05],
  ["f-repetition_penalty", "1.2", "repetition_penalty", 1.2],
  ["f-presence_penalty", "0.4", "presence_penalty", 0.4],
  ["f-frequency_penalty", "0.6", "frequency_penalty", 0.6],
  ["f-stop", "КОНЕЦ\nСТОП", "stop", ["КОНЕЦ", "СТОП"]],
];

function freshClient(options) {
  Object.keys(require.cache).forEach((key) => delete require.cache[key]);
  const dom = require(path.join(__dirname, "dom.js"));
  // Свои чаты добавляем ПОСЛЕ распаковки options, иначе options.chats затрёт
  // список, а не дополнит его.
  const seeded = [
    { label: "первый чат", system: "СТАРЫЙ ПРОМПТ", model: "первая/модель" },
    { label: "второй чат", system: "ЧУЖОЙ ПРОМПТ", model: "вторая/модель" },
    ...((options && options.chats) || []),
  ];
  const env = dom.boot(HTML, { ...options, chats: seeded });
  const client = require(path.join(__dirname, "..", "app", "static", "app.js"));
  return { ...env, ...dom, client, $: (sel) => env.document.querySelector(sel) };
}

// Пропавший узел — красное утверждение, а не исключение: упавший маршрут
// уносит с собой все проверки после себя, и от диагностики остаётся одна
// строка. Кнопку ищем по назначению, а не по номеру в ряду: индекс начинает
// жать не то, оставаясь зелёным.
function cardButton(card, title) {
  if (!card) {
    check(`в ленте есть карточка с кнопкой «${title}»`, false, "карточки нет вовсе");
    return { dispatchEvent() {} };
  }
  const titles = card.querySelectorAll(".icon-btn").map((b) => b.title);
  const btn = card.querySelectorAll(".icon-btn").find((b) => b.title === title);
  if (btn) return btn;
  check("в карточке есть кнопка «" + title + "»", false, titles.join(" | "));
  return { dispatchEvent() {} };
}

// Текст строки под ответом — или внятное «строки нет».
function usageText(node, cls) {
  const el = node && node.querySelector(cls);
  return el ? el.textContent : "(строки " + cls + " нет)";
}

const NO_TILE = "(плитки нет)";
const shownAs = (tile) => tile.v + " / " + tile.sub;
function tileOf($, name) {
  const el = $("#tiles").children.find((t) => t.querySelector(".tile-k").textContent === name);
  if (!el) return { el: null, v: NO_TILE, sub: NO_TILE, row: NO_TILE };
  const sub = el.querySelector(".tile-sub");
  const row = el.querySelector(".tile-row");
  return {
    el,
    v: row.querySelector(".tile-v").textContent,
    sub: sub ? sub.textContent : "",
    row: row.children.map((c) => c.className).join(","),
  };
}

async function routeChecks() {
  // ── каждое поле панели доезжает до запроса, даже без события change ──
  for (const [field, typed, key, expected] of PANEL_ROUTE) {
    const { client, server, $, settle } = freshClient();
    // Поле, которого нет в разметке, роняет маршрут исключением — и уносит
    // с собой все утверждения после себя. Диагностика при поломке важнее
    // краткости: ловим здесь и краснеем именно этой строкой.
    try {
      client.init();
      await settle(20);

      $("#" + field).value = typed;          // правка есть на экране...
      $("#input").value = "вопрос";          // ...а `change` не выстрелил
      $("#composer").requestSubmit();
      await settle(80);
    } catch (err) {
      check(`панель → запрос: ${key} доезжает без падения`, false,
        `клиент упал на поле ${field}: ${err && err.message}`);
      continue;
    }

    const sent = server.state.sent[0];
    check(`панель → запрос: ${key} без события change`, Boolean(sent), "сообщение не ушло вовсе");
    if (sent) {
      check(
        `панель → запрос: ${key} доехал новым`,
        JSON.stringify(sent.config[key]) === JSON.stringify(expected),
        `ушло ${JSON.stringify(sent.config[key])}, ждали ${JSON.stringify(expected)}`
      );
    }
  }

  // ── формат ответа: список, а не поле ──
  {
    const { client, server, $, settle } = freshClient();
    client.init();
    await settle(20);
    $("#f-response_format_kind").value = "json_object";
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(80);
    const sent = server.state.sent[0];
    check(
      "панель → запрос: response_format доехал новым",
      sent && JSON.stringify(sent.config.response_format) === JSON.stringify({ type: "json_object" }),
      JSON.stringify(sent && sent.config.response_format)
    );
  }

  // ── в теле сообщения только текст: ленту хранит агент ──
  //
  // Историю помнит агент, а не браузер: клиент шлёт вопрос и ничего больше,
  // а перегенерация — вовсе пустое тело.
  {
    const { client, server, $, settle, Evt } = freshClient();
    client.init();
    await settle(20);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(80);

    const posted = server.state.requests.filter((r) => r.path.endsWith("/messages"));
    check("сообщение ушло одним POST", posted.length === 1, String(posted.length));
    check(
      "в теле сообщения нет ничего, кроме text",
      posted[0] && JSON.stringify(Object.keys(posted[0].body || {})) === '["text"]',
      JSON.stringify(posted[0] && posted[0].body)
    );

    const card = $("#feed").querySelector(".card");
    cardButton(card, "Перегенерировать").dispatchEvent(new Evt("click"));
    await settle(120);
    const repeated = server.state.requests.filter((r) => r.path.endsWith("/regenerate"));
    check(
      "перегенерация не шлёт тела вовсе: вопрос помнит агент",
      repeated.length === 1 && repeated[0].body === null,
      JSON.stringify(repeated.map((r) => r.body))
    );
  }

  // ── разметка из ответа не становится разметкой страницы — на самой карточке ──
  //
  // Утверждение о том, что оказалось в ленте, а не о том, что вернула
  // функция: экранирование можно снять на месте вызова, и проверки самого
  // `renderMarkdown` этого не заметят. Заодно стережётся `el()` — он ставит
  // текст через `textContent`: у узла, набранного текстом, `innerHTML` пуст.
  {
    const EVIL = '<img src=x onerror="alert(1)">';
    const { client, $, settle, Evt } = freshClient({
      chats: [{
        label: EVIL,
        history_len: 2,
        transcript: [
          { role: "user", content: EVIL, error: null, reasoning: "", metrics: null },
          { role: "assistant", content: EVIL, error: null, reasoning: "", metrics: null },
        ],
      }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const body = $("#feed").querySelector(".card-body");
    check(
      "в теле ответа тега из ответа модели нет",
      body && !body.innerHTML.includes("<img"),
      body && body.innerHTML
    );
    check(
      "в теле ответа тег остался текстом",
      body && body.innerHTML.includes("&lt;img"),
      body && body.innerHTML
    );

    const bubble = $("#feed").querySelector(".msg-user");
    check(
      "вопрос показан текстом, а не разметкой",
      bubble && bubble.textContent === EVIL && bubble.innerHTML === "",
      bubble && JSON.stringify([bubble.textContent, bubble.innerHTML])
    );

    const title = $("#agent-list").querySelectorAll(".item-title")[2];
    check(
      "имя чата в списке показано текстом, а не разметкой",
      title && title.textContent === EVIL && title.innerHTML === "",
      title && JSON.stringify([title.textContent, title.innerHTML])
    );
  }

  // ── нераспознаваемое поле: сообщение не уходит вовсе ──
  {
    const { client, server, $, settle } = freshClient();
    client.init();
    await settle(20);
    $("#f-temperature").value = "жарко";
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(80);
    check("с нечитаемым полем сообщение не отправляется", server.state.sent.length === 0,
      JSON.stringify(server.state.sent));
    check("и клиент говорит почему", /не применились/.test($("#composer-hint").textContent),
      $("#composer-hint").textContent);
    check("текст сообщения остался в поле ввода", $("#input").value === "вопрос", $("#input").value);
  }

  // ── лента про обмен, панель про диалог ──
  //
  // Разделение, ради которого затевалась правка: под ответом — числа этого
  // обмена, справа — итог по всему разговору. Проверяется оно вместе, одним
  // маршрутом: два обмена с разными usage, и после второго плитки обязаны
  // показать **сумму**, а строки в ленте — остаться каждая при своём.
  {
    const plan = [
      { prompt_tokens: 1240, completion_tokens: 312, total_tokens: 1552, cost_usd: 0.000186,
        tokens_per_second: 12.44, elapsed_ms: 2350, first_token_ms: 420, ttft_ms: 420,
        context_fill_pct: 1.2 },
      { prompt_tokens: 12000, completion_tokens: 500, total_tokens: 12500, cost_usd: 0.0004,
        tokens_per_second: 9.5, elapsed_ms: 5000, first_token_ms: 380, ttft_ms: 380,
        context_fill_pct: 9.8 },
    ];
    const { client, $, settle } = freshClient({ usage: (i) => plan[i] });
    client.init();
    await settle(30);

    const PANEL = ["Входные токены", "Выходные токены", "Всего токенов",
                   "Стоимость", "Сообщений", "Контекст"];
    const tiles = () => {
      const out = {};
      PANEL.forEach((name) => { out[name] = tileOf($, name); });
      return out;
    };
    const labels = () => $("#tiles").children.map((t) => t.querySelector(".tile-k").textContent);
    const rowOf = (card, cls) => usageText(card, cls);
    const cards = () => $("#feed").querySelectorAll(".card");

    // Сетка — две колонки: пять плиток оставили бы пустую клетку, девять —
    // лишний ряд. Поэтому утверждение на **число** плиток.
    check("плиток ровно шесть — сетка 2×3, пустых клеток нет",
      $("#tiles").children.length === 6, "плиток " + $("#tiles").children.length);
    check("и это те самые шесть", labels().join(" | ") === PANEL.join(" | "), labels().join(" | "));

    const empty = tiles();
    check("до первого ответа входные токены — прочерк, а не ноль",
      empty["Входные токены"].v === "—", shownAs(empty["Входные токены"]));
    // Ноль сообщений — это знание, а не незнание: разговора не было.
    check("до первого ответа сообщений ноль", empty["Сообщений"].v === "0", shownAs(empty["Сообщений"]));
    check("и контекст пуст", empty["Контекст"].v === "—", shownAs(empty["Контекст"]));

    $("#input").value = "первый вопрос";
    $("#composer").requestSubmit();
    await settle(90);

    const one = tiles();
    check("после первого обмена панель показывает его целиком",
      [one["Входные токены"].v, one["Выходные токены"].v, one["Всего токенов"].v,
       one["Стоимость"].v, one["Сообщений"].v].join(" | ") ===
        "1 240 | 312 | 1 552 | $0.000186 | 1",
      [one["Входные токены"].v, one["Выходные токены"].v, one["Всего токенов"].v,
       one["Стоимость"].v, one["Сообщений"].v].join(" | "));
    check("контекст — доля окна по последнему ответу",
      one["Контекст"].v === "1.2 %", one["Контекст"].v);
    check("подписей в плитках больше нет: панель и так вся про диалог",
      PANEL.every((name) => tileOf($, name).sub === ""),
      PANEL.map((name) => name + ":" + tileOf($, name).sub).join(" | "));

    // Цена в девять знаков занимает ширину плитки целиком: окажись в строке
    // значения кто-то ещё, на экране останется «$0.000...».
    check("в строке значения стоимости никого, кроме самого значения",
      tileOf($, "Стоимость").row === "tile-v small", tileOf($, "Стоимость").row);

    check("под ответом — числа этого обмена",
      rowOf(cards()[0], ".usage-tokens") === "входные токены 1 240 · выходные токены 312 · всего токенов 1 552 · $0.000186",
      rowOf(cards()[0], ".usage-tokens"));
    check("и второй строкой — как прошёл вызов",
      rowOf(cards()[0], ".usage-how") === "12.4 ток/с за 2.35 с · первый токен 0.42 с · стенд",
      rowOf(cards()[0], ".usage-how"));

    $("#input").value = "второй вопрос";
    $("#composer").requestSubmit();
    await settle(90);

    const two = tiles();
    check("после второго обмена в панели сумма, а не последний обмен",
      [two["Входные токены"].v, two["Выходные токены"].v, two["Всего токенов"].v,
       two["Стоимость"].v, two["Сообщений"].v].join(" | ") ===
        "13.2k | 812 | 14.1k | $0.000586 | 2",
      [two["Входные токены"].v, two["Выходные токены"].v, two["Всего токенов"].v,
       two["Стоимость"].v, two["Сообщений"].v].join(" | "));
    check("контекст не усредняется — он по последнему ответу",
      two["Контекст"].v === "9.8 %", two["Контекст"].v);
    check("а строки в ленте остались каждая при своём обмене",
      cards().map((c) => rowOf(c, ".usage-tokens")).join(" | ") ===
        "входные токены 1 240 · выходные токены 312 · всего токенов 1 552 · $0.000186 | " +
        "входные токены 12k · выходные токены 500 · всего токенов 12.5k · $0.000400",
      cards().map((c) => rowOf(c, ".usage-tokens")).join(" | "));
    check("и «как прошёл вызов» у второго ответа своё",
      rowOf(cards()[1], ".usage-how") === "9.5 ток/с за 5.00 с · первый токен 0.38 с · стенд",
      rowOf(cards()[1], ".usage-how"));
  }

  // ── выход думающей модели: рассуждение названо отдельным числом ──
  //
  // Провайдер кладёт токены рассуждения **внутрь** completion_tokens: выход
  // заметно больше видимого текста, и без оговорки число выглядит враньём.
  // Вычитать его нельзя — «выход» перестанет быть тем, что прислал провайдер.
  {
    const think = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "думал",
        metrics: { prompt_tokens: 100, completion_tokens: 60, total_tokens: 160,
                   cost_usd: 0.0001, reasoning_tokens: 40 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "думающая", transcript: think, history_len: think.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    const line = usageText($("#feed"), ".usage-tokens");
    check("рассуждение названо отдельным числом внутри выхода",
      line === "входные токены 100 · выходные токены 60 (из них 40 рассуждение) · всего токенов 160 · $0.000100", line);
    check("в панели выходные токены — то, что прислал провайдер, без вычитаний",
      tileOf($, "Выходные токены").v === "60", tileOf($, "Выходные токены").v);
  }

  // ── итог берётся с сервера, а не складывается в браузере ──
  //
  // Инвариант дня: сумму считает агент, клиент её только показывает. Сервер
  // называет заведомо другие числа, чем те, что лежат в репликах: показать
  // клиент обязан серверные.
  {
    const talk = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "",
        metrics: { prompt_tokens: 10, completion_tokens: 12, total_tokens: 22, cost_usd: 0.000002 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{
        label: "чужой счёт", transcript: talk, history_len: talk.length, exchanges: 42,
        usage_total: { prompt_tokens: 777777, completion_tokens: 999999,
                       total_tokens: 1777776, cost_usd: 0.5 },
      }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const v = (name) => tileOf($, name).v;
    check("входные токены — серверные, а не сумма реплик", v("Входные токены") === "777.8k", v("Входные токены"));
    check("выходные токены — серверные", v("Выходные токены") === "1M", v("Выходные токены"));
    check("всего токенов — серверное", v("Всего токенов") === "1.8M", v("Всего токенов"));
    check("стоимость — серверная", v("Стоимость") === "$0.500000", v("Стоимость"));
    check("и сообщения считает тоже сервер", v("Сообщений") === "42", v("Сообщений"));
    // И при этом строка под ответом показывает числа самой реплики: клиент
    // не подгоняет одно под другое, он показывает оба источника как есть.
    check("а строка под ответом — числа своей реплики",
      usageText($("#feed"), ".usage-tokens") === "входные токены 10 · выходные токены 12 · всего токенов 22 · $0.000002",
      usageText($("#feed"), ".usage-tokens"));
  }
}

// ── итог ──

routeChecks()
  .catch((err) => failures.push("маршрут клиента упал: " + (err && err.stack)))
  .then(() => {
    if (failures.length) {
      console.error(`ПРОВАЛЕНО ${failures.length} из ${passed + failures.length}:`);
      failures.forEach((f) => console.error("  - " + f));
      process.exit(1);
    }
    console.log(`ОК: ${passed} утверждений о клиенте`);
  });
