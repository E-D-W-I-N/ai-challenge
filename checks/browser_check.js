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

// ── предупреждение о параметрах, которых модель не заявляет ──
//
// На каждом вызове стоит provider.require_parameters=true: параметр, которого
// модель не заявляет, выкашивает провайдеров, и вместо ответа приходит ошибка,
// по которой не понять, что виноват один переключатель. Поэтому панель
// предупреждает **до** отправки — и называет ровно те параметры, что уедут
// в запрос.

{
  const { paramWarnings } = app;
  const plain = { id: "м/одна", supported_parameters: ["top_p"] };
  const named = paramWarnings(plain, { model: "м/одна", temperature: 0.5 }, {}, "м/одна");
  check("параметр, которого модель не заявляет, назван",
    named.length === 1 && named[0].includes("temperature"), JSON.stringify(named));
  check("о заявленном параметре панель молчит",
    paramWarnings(plain, { model: "м/одна", top_p: 0.5 }, {}, "м/одна").length === 0,
    JSON.stringify(paramWarnings(plain, { model: "м/одна", top_p: 0.5 }, {}, "м/одна")));

  const capped = { id: "anthropic/м", supported_parameters: ["temperature"],
                   temperature_capped: true, temperature_cap: 1.0 };
  const over = paramWarnings(capped, { model: "anthropic/м", temperature: 1.2 }, {}, "anthropic/м");
  check("потолок temperature назван, хотя параметр заявлен",
    over.length === 1 && over[0].includes("1.0"), JSON.stringify(over));

  const pinned = paramWarnings(plain, { model: "м/одна", top_p: 0.5 },
    { provider: { order: ["стенд"] } }, "м/другая");
  check("чат, привязанный к поставщику, предупреждает о смене модели",
    pinned.length === 1 && pinned[0].includes("стенд"), JSON.stringify(pinned));
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
  // Управление контекстом — тоже поля панели, а не константы в коде: два чата
  // рядом, у одного окно памяти задано, у другого нет, — это и есть сравнение
  // расхода «до/после», которого просит задание дня. Стратегия — оттуда же:
  // выбранная в панели, она обязана доехать до запроса тем же путём, что и
  // числа рядом с ней, иначе переключатель переключал бы только картинку.
  ["f-strategy", "window", "strategy", "window"],
  // Факты — вторая строка про ту же ручку не для симметрии: значения, которого
  // нет в разметке `<select>`, поле не примет вовсе, и строка покраснеет на
  // пустом значении. Забудь про `<option>` — переключатель знал бы стратегию,
  // а выбрать её было бы нечем.
  ["f-strategy", "facts", "strategy", "facts"],
  ["f-keep_last", "6", "keep_last", 6],
  ["f-compress_every", "10", "compress_every", 10],
  // Выключатель долговременной памяти — тем же путём и при любой стратегии:
  // он не про историю, а про слой поверх неё, и прятать его не за чем.
  ["f-memory", "off", "memory", "off"],
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

// Числа контекста, какими их видит пользователь: только показанные и с той
// подписью, что стоит над полем. Скрытое поле в строку не попадает вовсе.
function shownContext($) {
  return ["keep_last", "compress_every"]
    .filter((name) => !$("#field-" + name).classList.contains("hidden"))
    .map((name) => name + "=" + $("#label-" + name).textContent)
    .join(" | ");
}

const NO_TILE = "(плитки нет)";
function tileOf($, name) {
  const el = $("#tiles").children.find((t) => t.querySelector(".tile-k").textContent === name);
  if (!el) return { el: null, v: NO_TILE, row: NO_TILE };
  return {
    el,
    v: el.querySelector(".tile-v").textContent,
    row: el.children.slice(1).map((c) => c.className).join(","),
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

  // ── переключатель показывает то, что правда случится с историей ──
  //
  // Стратегия — выбор из закрытого списка, и пустого значения у него нет.
  // Чат, записанный сервером другой версии, может принести значение, которого
  // в списке нет: сервер читает такой конфиг как «не резать», и панель обязана
  // показать то же самое. Покажи она пустое поле — пользователь увидел бы
  // выбор, которого не делал, а первая же правка соседнего поля отправила бы
  // его обратно.
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [
        { label: "со сводкой", strategy: "summary", keep_last: 6, compress_every: 10 },
        { label: "из будущего", strategy: "факты" },
      ],
    });
    client.init();
    await settle(20);
    const open = (i) => $("#agent-list").querySelectorAll(".item-open")[i].dispatchEvent(new Evt("click"));
    open(2);
    await settle(40);
    check("панель показывает выбранную стратегию",
      $("#f-strategy").value === "summary", $("#f-strategy").value);
    open(3);
    await settle(40);
    check("незнакомая стратегия показана как «вся история», а не пустым полем",
      $("#f-strategy").value === "full", $("#f-strategy").value);
  }

  // ── видны только те поля, что работают при выбранной стратегии ──
  //
  // Поле, которое принимает число и молча его игнорирует, врёт ровно так же,
  // как молчаливая обрезка: по виду оно рабочее. Поэтому при `full` чисел нет
  // вовсе, у окна одно, у суммаризации два — и одно и то же поле подписано
  // по-разному, потому что значит разное.
  //
  // Переключение проверяется **сразу после события**, без ожидания: пролив
  // конфига ходит на сервер, а показ обязан смениться на самом выборе.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "со сводкой", strategy: "summary", keep_last: 6, compress_every: 10 }],
    });
    // До `init()` чатов ещё нет, и `syncStrategyFields` не отработал ни разу:
    // числа прячет сама разметка. Покажи она их — при первой отрисовке, пока
    // список едет с сервера, панель обещала бы два рабочих поля при `full`.
    check("до первого чата числа спрятаны самой разметкой", shownContext($) === "", shownContext($));
    client.init();
    await settle(20);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const pick = (value) => {
      $("#f-strategy").value = value;
      $("#f-strategy").dispatchEvent(new Evt("change"));   // ответа сервера не ждём
    };

    check("суммаризация показывает оба числа и называет их по-своему",
      shownContext($) === "keep_last=Хранить последних, сообщений | " +
        "compress_every=Сжимать каждые, сообщений", shownContext($));

    pick("window");
    check("окно показывает одно число, и это размер окна",
      shownContext($) === "keep_last=Размер окна, сообщений", shownContext($));

    pick("facts");
    check("факты показывают одно число, и это хвост, а не размер окна",
      shownContext($) === "keep_last=Хранить последних, сообщений", shownContext($));

    pick("full");
    check("вся история не показывает ни одного числа",
      shownContext($) === "", shownContext($));

    // Спрятанное поле — не очищенное: скрытие это показ, а не правка конфига.
    pick("summary");
    check("переключение туда-обратно не теряет набранного",
      $("#f-keep_last").value === "6" && $("#f-compress_every").value === "10",
      `${$("#f-keep_last").value} / ${$("#f-compress_every").value}`);

    // И в конфиг спрятанное поле уезжает прежним, а не `null`: ручки
    // принимают оба числа при любой стратегии, просто не всякая их читает.
    pick("full");
    await settle(40);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(80);
    const sent = server.state.sent[0];
    check("спрятанные поля уезжают в конфиг прежними, а не пустыми",
      sent && sent.config.keep_last === 6 && sent.config.compress_every === 10 &&
        sent.config.strategy === "full",
      JSON.stringify(sent && [sent.config.strategy, sent.config.keep_last, sent.config.compress_every]));
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

    // Номер дня в шапке — единственное место, где ветка называет себя вслух:
    // чужой номер видно первым же кадром записи. Утверждение живёт здесь, на
    // уже поднятом клиенте, а не отдельным сценарием: свой запуск стенда ради
    // одной строки текста дороже самой строки.
    check("в шапке стоит номер этого дня",
      $(".brand-sub").textContent === "чат · день 11", $(".brand-sub").textContent);

    const empty = tiles();
    check("до первого ответа входные токены — прочерк, а не ноль",
      empty["Входные токены"].v === "—", empty["Входные токены"].v);
    // Ноль сообщений — это знание, а не незнание: разговора не было.
    check("до первого ответа сообщений ноль", empty["Сообщений"].v === "0", empty["Сообщений"].v);
    check("и контекст пуст", empty["Контекст"].v === "—", empty["Контекст"].v);

    $("#input").value = "первый вопрос";
    $("#composer").requestSubmit();
    await settle(90);

    const one = tiles();
    check("после первого обмена панель показывает его целиком",
      [one["Входные токены"].v, one["Выходные токены"].v, one["Всего токенов"].v,
       one["Стоимость"].v, one["Сообщений"].v].join(" | ") ===
        "1 240 | 312 | 1 552 | $0.000186 | 2",
      [one["Входные токены"].v, one["Выходные токены"].v, one["Всего токенов"].v,
       one["Стоимость"].v, one["Сообщений"].v].join(" | "));
    check("контекст — доля окна по последнему ответу",
      one["Контекст"].v === "1.2 %", one["Контекст"].v);
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
        "13.2k | 812 | 14.1k | $0.000586 | 4",
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

  // ── сжатый обмен говорит, сколько сообщений уехало сводкой ──
  //
  // Седьмой плитки у сжатия нет намеренно: плитки — про весь диалог, а
  // свернулось — в этом обмене, и место факту рядом с числом, которое он
  // уменьшил. Сравнение «до/после» держат два чата рядом и плитка «Всего
  // токенов», а не новая клетка в сетке.
  {
    const folded = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "",
        metrics: { prompt_tokens: 400, completion_tokens: 50, total_tokens: 450,
                   cost_usd: 0.0001, summarized: 10 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "сжатый", transcript: folded, history_len: folded.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("под сжатым ответом сказано, сколько сообщений уехало сводкой",
      usageText($("#feed"), ".usage-tokens") ===
        "входные токены 400 (сводка вместо 10 сообщений) · выходные токены 50 · всего токенов 450 · $0.000100",
      usageText($("#feed"), ".usage-tokens"));
    check("а седьмой плитки у сжатия нет — сетка осталась 2×3",
      $("#tiles").children.length === 6, "плиток " + $("#tiles").children.length);
  }

  // ── обмен со скользящим окном говорит, сколько сообщений отброшено ──
  //
  // Главное требование дня: обрезка бывает только выбранная — и всегда
  // названная. Окно теряет начало разговора **совсем**, прочитать его потом
  // негде, и молчать о нём было бы хуже, чем о сводке. Слово другое, чем у
  // сводки, потому что и случай другой: та заменила, это отбросило.
  {
    const windowed = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "",
        metrics: { prompt_tokens: 120, completion_tokens: 50, total_tokens: 170,
                   cost_usd: 0.0001, dropped: 14 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "с окном", transcript: windowed, history_len: windowed.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("под ответом с окном сказано, сколько сообщений отброшено",
      usageText($("#feed"), ".usage-tokens") ===
        "входные токены 120 (окно: отброшено 14 сообщений) · выходные токены 50 · всего токенов 170 · $0.000100",
      usageText($("#feed"), ".usage-tokens"));
    check("а плиток по-прежнему шесть — у обрезки своей нет",
      $("#tiles").children.length === 6, "плиток " + $("#tiles").children.length);
  }

  // ── обмен с фактами говорит, сколько сообщений заменила выписка ──
  //
  // Слово то же, что у сводки, — «вместо»: начало разговора не отброшено, оно
  // заменено выпиской «ключ: значение», и прочитать её можно в промпте
  // запроса. Число берётся из метрик обмена, как и у двух других стратегий:
  // свой ключ у каждой, потому что говорят они разное.
  {
    const withFacts = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "",
        metrics: { prompt_tokens: 200, completion_tokens: 50, total_tokens: 250,
                   cost_usd: 0.0001, facts: 12 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "с фактами", transcript: withFacts, history_len: withFacts.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("под ответом с фактами сказано, сколько сообщений заменила выписка",
      usageText($("#feed"), ".usage-tokens") ===
        "входные токены 200 (факты вместо 12 сообщений) · выходные токены 50 · всего токенов 250 · $0.000100",
      usageText($("#feed"), ".usage-tokens"));
    check("и плиток по-прежнему шесть — своей у фактов нет",
      $("#tiles").children.length === 6, "плиток " + $("#tiles").children.length);
  }

  // ── несжатый обмен об этом молчит ──
  //
  // Пометка появляется только там, где сводка действительно уехала: «сводка
  // вместо 0 сообщений» под каждым ответом чата без сжатия была бы шумом.
  {
    const plain = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "",
        metrics: { prompt_tokens: 400, completion_tokens: 50, total_tokens: 450, cost_usd: 0.0001 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "несжатый", transcript: plain, history_len: plain.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("без обрезки под ответом про неё ни слова",
      usageText($("#feed"), ".usage-tokens") ===
        "входные токены 400 · выходные токены 50 · всего токенов 450 · $0.000100",
      usageText($("#feed"), ".usage-tokens"));
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
        label: "чужой счёт", transcript: talk, history_len: 42,
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

  // ── упавший обмен не гасит показанное ──
  //
  // У метрик ошибки все числа пустые: заполнены `error`, модель и время.
  // Класть такой набор поверх прежнего значило бы гасить плитки ровно там,
  // где числа нужнее всего: на записи переполнения видно ошибку, а сколько
  // контекста было занято — уже нет. Набор сливается по полям, и показанная
  // доля окна помечается прошлой, а не выдаётся за свежую.
  {
    const first = { prompt_tokens: 1240, completion_tokens: 312, total_tokens: 1552,
                    cost_usd: 0.000186, tokens_per_second: 12.44, elapsed_ms: 2350,
                    first_token_ms: 420, ttft_ms: 420, context_fill_pct: 1.2 };
    const { client, $, settle } = freshClient({
      usage: (i) => (i === 0 ? first : null),
      fail: (i) => (i === 0 ? null : { message: "HTTP 402: контекст переполнен" }),
    });
    client.init();
    await settle(30);

    $("#input").value = "первый вопрос";
    $("#composer").requestSubmit();
    await settle(90);
    $("#input").value = "второй вопрос";
    $("#composer").requestSubmit();
    await settle(90);

    const v = (name) => tileOf($, name).v;
    check("упавший обмен не гасит входные токены", v("Входные токены") === "1 240", v("Входные токены"));
    check("не гасит стоимость", v("Стоимость") === "$0.000186", v("Стоимость"));
    check("и не гасит долю окна", v("Контекст") === "1.2 %", v("Контекст"));
    check("показанная доля окна помечена прошлой",
      tileOf($, "Контекст").row === "tile-v past", tileOf($, "Контекст").row);
    check("ошибка названа под полем ввода",
      /402/.test($("#composer-hint").textContent), $("#composer-hint").textContent);
    check("а вопрос вернулся в поле ввода: обмена не было",
      $("#input").value === "второй вопрос", $("#input").value);
  }

  // ── смена модели гасит долю окна, но не токены ──
  //
  // Окно у новой модели другое, и прежний процент к ней не относится: плитка
  // молчит прочерком, пока не придёт первый ответ на новой модели. Гасит её
  // сама смена модели в панели, а не расхождение имён: на `openrouter/auto`
  // провайдер возвращает не то имя, которое просили, и сверка имён гасила бы
  // плитку после каждого ответа, навсегда. Токенов и цены это не касается —
  // они сложены за весь разговор, какой бы моделью он ни шёл.
  {
    const { client, $, settle, Evt } = freshClient({
      usage: (i) => ({ prompt_tokens: 1240, completion_tokens: 312, total_tokens: 1552,
                       cost_usd: 0.000186, context_fill_pct: i === 0 ? 1.2 : 7.5 }),
    });
    client.init();
    await settle(30);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(90);
    check("до смены модели доля окна показана", tileOf($, "Контекст").v === "1.2 %",
      tileOf($, "Контекст").v);

    $("#f-model").value = "вторая/модель";
    $("#f-model").dispatchEvent(new Evt("change"));
    await settle(60);
    check("смена модели гасит долю окна", tileOf($, "Контекст").v === "—",
      tileOf($, "Контекст").v);
    check("а всего токенов остаётся на месте", tileOf($, "Всего токенов").v === "1 552",
      tileOf($, "Всего токенов").v);
    check("и стоимость тоже", tileOf($, "Стоимость").v === "$0.000186",
      tileOf($, "Стоимость").v);

    // Гасить навсегда нельзя: первый же ответ на новой модели приносит свою
    // долю окна, и плитка обязана заговорить снова.
    $("#input").value = "второй вопрос";
    $("#composer").requestSubmit();
    await settle(90);
    check("первый ответ на новой модели зажигает долю окна обратно",
      tileOf($, "Контекст").v === "7.5 %", tileOf($, "Контекст").v);
    check("а токены к этому времени сложились за оба обмена",
      tileOf($, "Всего токенов").v === "3 104", tileOf($, "Всего токенов").v);

    // Открытие соседнего чата числа не сливает, а заменяет: показанное
    // относится к открытому разговору, и протечь из чужого не может.
    $("#agent-list").querySelectorAll(".item-open")[1].dispatchEvent(new Evt("click"));
    await settle(60);
    check("в соседнем чате без ответов всего токенов — прочерк",
      tileOf($, "Всего токенов").v === "—", tileOf($, "Всего токенов").v);
    check("и доля окна из прошлого чата не протекла",
      tileOf($, "Контекст").v === "—", tileOf($, "Контекст").v);
  }

  // ── пока сворачивается начало разговора, карточка об этом говорит ──
  //
  // Сжатие — отдельный вызов к модели ДО ответа: карточка стоит пустой,
  // и без строки состояния пауза выглядит зависанием. Кадры стенд подаёт
  // с задержкой, поэтому смотрим не только итог, но и середину потока:
  // строка обязана быть видна до ответа и уйти, как только он пошёл.
  //
  // Процентов в строке нет намеренно — сворачивание это один вызов, доли
  // выполнения у него не существует, и полоса называла бы выдуманное число.
  //
  // Сворачивается **второй** обмен, а не первый: у сжатого обмена в промпте
  // есть хвост истории, и в нём — ответ модели. Сверни первый, и подпись
  // «ответ модели» не исполнилась бы ни разу, а показ обязан различать, где
  // чей голос.
  const SUMMARY = "[пересказ начала разговора, свёрнуто сообщений: 4]\nговорили про сжатие";
  const OTHER_SUMMARY = "[пересказ начала разговора, свёрнуто сообщений: 6]\nсводка упавшего обмена";
  {
    const { client, $, settle, Evt } = freshClient({
      delay: 60,
      // Первый обмен обычный — он же станет хвостом промпта второго. Третий
      // сворачивается тоже, но падает: его промпт не должен достаться никому.
      service: (i) => (i === 0 ? null : { insert: i === 1 ? SUMMARY : OTHER_SUMMARY, covered: 0 }),
      fail: (i) => (i === 2 ? { message: "HTTP 502: провайдер не ответил" } : null),
    });
    client.init();
    await settle(40);

    $("#input").value = "первый вопрос";
    $("#composer").requestSubmit();
    await settle(400);
    check("обмен без сворачивания строки состояния не показывает",
      !$("#feed").querySelector(".card-status"),
      "строка состояния появилась: " + usageText($("#feed"), ".card-status"));

    $("#input").value = "второй вопрос";
    $("#composer").requestSubmit();
    await settle(90);          // пришёл кадр о сворачивании, ответ ещё не пошёл
    const status = $("#feed").querySelector(".card-status");
    check("пока идёт сворачивание, карточка говорит об этом",
      Boolean(status), "строки состояния в карточке нет");
    check("строка состояния называет, что происходит",
      Boolean(status) && status.textContent.includes("Сворачиваю начало разговора"),
      status && status.textContent);
    check("и показывает неопределённый индикатор, а не долю выполнения",
      Boolean(status) && Boolean(status.querySelector(".spinner")) &&
        !/%/.test(status.textContent),
      status && status.textContent);

    // Панель не запирается на время ответа: показ полей — это показ, а не
    // правка уехавшего запроса. Уехавший обмен всё равно собран из слепка
    // конфига, а пользователю может понадобиться посмотреть, что значит
    // соседняя стратегия, не дожидаясь конца генерации.
    $("#f-strategy").value = "facts";
    $("#f-strategy").dispatchEvent(new Evt("change"));
    check("стратегию видно переключить и пока идёт ответ",
      shownContext($) === "keep_last=Хранить последних, сообщений", shownContext($));

    await settle(70);          // пришёл кадр start: сворачивание позади
    check("ответ пошёл — строка состояния ушла",
      !$("#feed").querySelector(".card-status"),
      "строка осталась: " + usageText($("#feed"), ".card-status"));
    check("а ответ в это время ещё стримится",
      Boolean($("#feed").querySelector(".card.busy")), "карточка уже не в стриме");

    await settle(400);         // обмен дошёл до конца, лента перерисована
    check("после сжатого обмена строки состояния не осталось",
      !$("#feed").querySelector(".card-status"), "строка состояния осталась в ленте");

    // ── кнопка есть у каждого обмена, и у каждого — свой промпт ──
    //
    // Врезка кнопку не заводит: её заводит увиденный промпт. У обмена без
    // сводки показывать тоже есть что — системный промпт и вопрос, — а подпись
    // врезки при пустом `summary_at` вылезать не смеет ни над одним из них.
    const titles = (node) => node.querySelectorAll(".icon-btn").map((b) => b.title);
    const promptRoles = (node) => {
      const shown = node && node.querySelector(".prompt-view");
      return shown ? shown.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    };
    const promptTexts = (node) => {
      const shown = node && node.querySelector(".prompt-view");
      return shown ? shown.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    };
    let cards = $("#feed").querySelectorAll(".card");
    check("кнопка промпта есть и у обмена без сводки",
      cards[0] && titles(cards[0]).includes("Показать промпт запроса"),
      cards[0] && JSON.stringify(titles(cards[0])));
    cardButton(cards[0], "Показать промпт запроса").dispatchEvent(new Evt("click"));
    check("и под ней — его собственный промпт, без врезки и без чужого вопроса",
      JSON.stringify(promptRoles(cards[0])) ===
        JSON.stringify(["системный промпт", "сообщение пользователя"]) &&
        promptTexts(cards[0]).includes("первый вопрос") &&
        !promptTexts(cards[0]).includes(SUMMARY),
      JSON.stringify(promptRoles(cards[0])) + " " + JSON.stringify(promptTexts(cards[0])));
    check("без врезки запасная подпись роли не вылезает",
      !promptRoles(cards[0]).includes("врезка вместо начала разговора"),
      JSON.stringify(promptRoles(cards[0])));
    cardButton(cards[0], "Показать промпт запроса").dispatchEvent(new Evt("click"));

    const card = cards[1];
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    check("по кнопке показан промпт запроса — над ответом, а не под ним",
      Boolean(view) &&
        JSON.stringify(card.children.map((c) => c.className)) ===
          JSON.stringify(["card-head", "prompt-view", "card-body md", "card-usage"]),
      JSON.stringify(card.children.map((c) => c.className)));
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    const texts = view ? view.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("в промпте виден системный промпт",
      roles.includes("системный промпт") && texts.includes("СТАРЫЙ ПРОМПТ"),
      JSON.stringify(roles) + " " + JSON.stringify(texts));
    check("в промпте видна сводка — и подписана сводкой",
      roles.includes("сводка начала разговора") && texts.includes(SUMMARY),
      JSON.stringify(roles) + " " + JSON.stringify(texts));
    check("в промпте видно сообщение пользователя",
      roles.includes("сообщение пользователя") && texts.includes("второй вопрос"),
      JSON.stringify(roles) + " " + JSON.stringify(texts));
    // Хвост истории уехал как есть, и чей голос где — видно: вопрос
    // пользователя и ответ модели подписаны по-разному.
    check("роли идут в том порядке, в каком уехали: промпт, сводка, хвост, вопрос",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "сводка начала разговора",
        "сообщение пользователя", "ответ модели", "сообщение пользователя",
      ]),
      JSON.stringify(roles));

    // Второй клик возвращает карточку как было — как у «сырого текста».
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    check("второй клик убирает промпт",
      !card.querySelector(".prompt-view"), "промпт остался на экране");
    check("и ответ на месте",
      card.querySelector(".card-body").textContent.includes("ответ модели"),
      card.querySelector(".card-body").textContent);

    // ── упавший обмен не отдаёт свой промпт чужой карточке ──
    //
    // Обмен, не доехавший до истории, её не удлиняет — а кадр `start` у него
    // уехал, со своим промптом и своей сводкой. Привяжи его по длине истории,
    // и он лёг бы под ключ **прошлого** ответа: кнопка под давней карточкой
    // показала бы чужой запрос, внутри которого лежит сам этот ответ.
    $("#input").value = "упавший вопрос";
    $("#composer").requestSubmit();
    await settle(400);
    cards = $("#feed").querySelectorAll(".card");
    check("упавший обмен карточки в ленте не оставил", cards.length === 2, String(cards.length));
    // Кнопка теперь есть у обоих обменов — тем важнее, что под каждой лежит
    // его собственный запрос: промпт упавшего не достался ни одному из них.
    cardButton(cards[0], "Показать промпт запроса").dispatchEvent(new Evt("click"));
    check("и своего промпта первый обмен на чужой не сменил",
      promptTexts(cards[0]).includes("первый вопрос") &&
        !promptTexts(cards[0]).includes(OTHER_SUMMARY) &&
        !promptTexts(cards[0]).includes("упавший вопрос"),
      JSON.stringify(promptTexts(cards[0])));
    cardButton(cards[0], "Показать промпт запроса").dispatchEvent(new Evt("click"));
    cardButton(cards[1], "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const after = cards[1].querySelector(".prompt-view");
    const shown = after ? after.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("а у сжатого обмена под кнопкой остался его собственный промпт",
      shown.includes(SUMMARY) && shown.includes("второй вопрос") &&
        !shown.includes(OTHER_SUMMARY) && !shown.includes("упавший вопрос"),
      JSON.stringify(shown));
  }

  // ── чат без системного промпта: сводка встаёт первым сообщением ──
  //
  // `system` по умолчанию пуст, и такой чат — самый обычный: сводка в его
  // промпте стоит нулевым сообщением, а `summary_at` равен нулю. Ноль —
  // не «сводки нет», и кнопка обязана быть на месте.
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "без системного", system: "" }],
      service: { insert: SUMMARY, covered: 0 },
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "вопрос без системного";
    $("#composer").requestSubmit();
    await settle(150);

    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    check("без системного промпта сводка стоит первой, и кнопка на месте",
      JSON.stringify(roles) ===
        JSON.stringify(["сводка начала разговора", "сообщение пользователя"]),
      JSON.stringify(roles));
  }

  // ── извлечение фактов: та же карточка, другой служебный вызов ──
  //
  // Кадр служебного вызова один на оба, и назвать, чем занята пауза, может
  // только сервер: «Сворачиваю начало разговора…» под извлечением фактов было
  // бы враньём ровно там, где строка состояния для того и заведена. Тем же
  // полем подписана врезка в просмотре промпта — одним индексом на оба показа.
  {
    const FACTS = "[факты о разговоре]\nцель: собрать ТЗ\nсрок: май\n[дальше — последние сообщения как есть]";
    const { client, $, settle, Evt } = freshClient({
      delay: 60,
      service: { insert: FACTS, covered: 0, strategy: "facts" },
    });
    client.init();
    await settle(40);
    $("#input").value = "вопрос с фактами";
    $("#composer").requestSubmit();
    await settle(90);          // пришёл кадр служебного вызова, ответ ещё не пошёл

    const status = $("#feed").querySelector(".card-status");
    check("пока обновляются факты, карточка говорит именно об этом",
      Boolean(status) && status.textContent.includes("Обновляю факты") &&
        !status.textContent.includes("Сворачиваю"),
      status && status.textContent);
    check("и индикатор тот же неопределённый, без долей выполнения",
      Boolean(status) && Boolean(status.querySelector(".spinner")) &&
        !/%/.test(status.textContent),
      status && status.textContent);

    await settle(400);
    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    const texts = view ? view.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("в промпте видна выписка фактов — и подписана фактами, а не сводкой",
      roles.includes("факты о разговоре") && !roles.includes("сводка начала разговора") &&
        texts.includes(FACTS),
      JSON.stringify(roles) + " " + JSON.stringify(texts));
    check("порядок тот же: системный промпт, врезка, вопрос",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "факты о разговоре", "сообщение пользователя",
      ]),
      JSON.stringify(roles));
  }

  // ── две врезки разом: долговременная память и врезка стратегии ──
  //
  // Главное место дня на клиенте. Память — слой не этого разговора, врезка
  // стратегии — выписка про этот; в одном промпте они стоят вдвоём, память
  // первой. Оба места называет сервер, двумя разными полями кадра `start`,
  // и подписать их клиент обязан по ним, а не по тексту сообщений: считай он
  // слоты сам — подписал бы памятью факты ровно тогда, когда врезок две.
  {
    const MEMORY = "[долговременная память]\nпрофиль: пишу на Kotlin\n[конец долговременной памяти]";
    const FACTS = "[факты о разговоре]\nцель: собрать ТЗ\n[дальше — последние сообщения как есть]";
    const { client, server, $, settle, Evt } = freshClient({
      memory: MEMORY,
      service: { insert: FACTS, covered: 0, strategy: "facts" },
    });
    client.init();
    await settle(30);
    $("#input").value = "вопрос с памятью и фактами";
    $("#composer").requestSubmit();
    await settle(150);

    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    const texts = view ? view.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("врезок в промпте две, и подписаны они разными словами в нужном порядке",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "долговременная память", "факты о разговоре",
        "сообщение пользователя",
      ]), JSON.stringify(roles));
    check("и под подписями стоят те самые тексты, а не перепутанные местами",
      texts[1] === MEMORY && texts[2] === FACTS,
      JSON.stringify(texts));

    // ── выключатель памяти уезжает PATCH'ем и виден в следующем промпте ──
    //
    // Переключение — на самом выборе, как у стратегии. Смотрим не только
    // на запрос: поле, доехавшее до сервера, но не изменившее промпт,
    // означало бы, что клиент показывает одно, а модель видит другое.
    $("#f-memory").value = "off";
    $("#f-memory").dispatchEvent(new Evt("change"));
    await settle(60);
    const patched = server.state.requests.filter((r) => r.method === "PATCH");
    check("переключение долговременной памяти уехало PATCH'ем",
      patched.length > 0 && patched[patched.length - 1].body.memory === "off",
      JSON.stringify(patched.map((r) => r.body && r.body.memory)));

    $("#input").value = "вопрос без памяти";
    $("#composer").requestSubmit();
    await settle(150);
    const second = $("#feed").querySelectorAll(".card")[1];
    cardButton(second, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const off = second.querySelector(".prompt-view");
    const offRoles = off ? off.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    check("с выключенной памятью врезка одна, и это врезка стратегии",
      JSON.stringify(offRoles) === JSON.stringify([
        "системный промпт", "факты о разговоре", "сообщение пользователя",
        "ответ модели", "сообщение пользователя",
      ]), JSON.stringify(offRoles));
  }

  // ── без памяти подпись не вылезает ни над одним сообщением ──
  //
  // Обратная половина: `memory_at` приходит пустым — и пустым он приходит
  // и когда память выключена, и когда её нет вовсе, и когда хранилища нет.
  // Сравнение строгое именно поэтому: подставь клиент ноль «по умолчанию»,
  // и подпись памяти встала бы над системным промптом у каждого чата.
  {
    const { client, $, settle, Evt } = freshClient({ delay: 20 });
    client.init();
    await settle(30);
    $("#input").value = "вопрос без памяти вовсе";
    $("#composer").requestSubmit();
    await settle(200);
    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    check("без памяти её подпись не вылезает ни над одним сообщением промпта",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "сообщение пользователя",
      ]), JSON.stringify(roles));
  }

  // ── чат без системного промпта: память стоит нулевым сообщением ──
  //
  // `memory_at` равен нулю, и ноль — не «памяти нет». Тот же случай, что
  // у сводки без системного промпта, и та же цена ошибки: нестрогое
  // сравнение здесь спрятало бы врезку, которая в модель уехала.
  {
    const MEMORY = "[долговременная память]\nрешение: оплата только картой\n[конец долговременной памяти]";
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "без системного", system: "" }],
      memory: MEMORY,
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "вопрос без системного";
    $("#composer").requestSubmit();
    await settle(150);

    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    check("без системного промпта память стоит первой, и подпись у неё своя",
      JSON.stringify(roles) === JSON.stringify([
        "долговременная память", "сообщение пользователя",
      ]), JSON.stringify(roles));
  }

  // ── без сворачивания карточка молчит, но промпт показать даёт ──
  //
  // Обратная половина: событие о сворачивании приходит не на каждом обмене,
  // и строка состояния, мигающая без повода, была бы шумом. Кнопка промпта —
  // не такая: её заводит не врезка, а увиденный запрос, и он есть у каждого
  // обмена.
  {
    const { client, $, settle, Evt } = freshClient({ delay: 60 });
    client.init();
    await settle(40);
    $("#input").value = "вопрос без сжатия";
    $("#composer").requestSubmit();
    await settle(90);
    check("без сворачивания строки состояния нет",
      !$("#feed").querySelector(".card-status"),
      "строка состояния появилась: " + usageText($("#feed"), ".card-status"));
    await settle(400);
    const card = $("#feed").querySelector(".card");
    check("а кнопка промпта у такого обмена есть",
      card && card.querySelectorAll(".icon-btn").map((b) => b.title)
        .includes("Показать промпт запроса"),
      card && JSON.stringify(card.querySelectorAll(".icon-btn").map((b) => b.title)));
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    check("в промпте несжатого обмена — системный промпт и вопрос, врезки нет",
      JSON.stringify(roles) ===
        JSON.stringify(["системный промпт", "сообщение пользователя"]),
      JSON.stringify(roles));
  }

  // ── у скользящего окна промпт показывает ровно то, что уехало ──
  //
  // Окно — единственная стратегия, которая теряет реплики совсем: вместо
  // отброшенного начала в промпт не встаёт ничего, и прочитать уехавшее
  // больше негде — лента-то показывает всю историю. Ровно поэтому кнопка
  // обязана быть и здесь, хотя врезки у окна нет и `summary_at` пуст.
  {
    const past = [
      { role: "user", content: "давний вопрос", error: null, reasoning: "", metrics: null },
      { role: "assistant", content: "давний ответ", error: null, reasoning: "", metrics: null },
      { role: "user", content: "свежий вопрос", error: null, reasoning: "", metrics: null },
      { role: "assistant", content: "свежий ответ", error: null, reasoning: "", metrics: null },
    ];
    const { client, $, settle, Evt } = freshClient({
      delay: 60,
      chats: [{
        label: "окно", system: "ПРОМПТ ОКНА", strategy: "window", keep_last: 2,
        transcript: past, history_len: past.length,
      }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "вопрос в окне";
    $("#composer").requestSubmit();
    await settle(400);

    const card = $("#feed").querySelectorAll(".card")[2];
    check("у обмена со скользящим окном кнопка промпта есть",
      card && card.querySelectorAll(".icon-btn").map((b) => b.title)
        .includes("Показать промпт запроса"),
      card && JSON.stringify(card && card.querySelectorAll(".icon-btn").map((b) => b.title)));
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    const texts = view ? view.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("в промпте окна видно окно, а отброшенное начало — нет",
      texts.includes("свежий вопрос") && texts.includes("свежий ответ") &&
        !texts.includes("давний вопрос") && !texts.includes("давний ответ"),
      JSON.stringify(texts));
    check("врезки у окна в промпте нет, и роли подписаны как обычно",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "сообщение пользователя", "ответ модели",
        "сообщение пользователя",
      ]),
      JSON.stringify(roles));
    // Лента при этом показывает историю целиком: промпт — про уехавшее,
    // а не про то, что помнит чат.
    check("а в ленте отброшенное начало осталось на месте",
      $("#feed").textContent.includes("давний вопрос"),
      $("#feed").textContent.slice(0, 200));
  }

  // ── ветвление: «Ветка отсюда» уносит разговор по карточку и открывает его ──
  //
  // Ветвление живёт поверх любой стратегии, поэтому кнопка стоит у каждого
  // ответа, а не только там, где что-то срезано. Проверяется весь путь:
  // с какой точкой уходит запрос, что открылось после него и видно ли
  // в списке и в панели, что этот чат — ветка, от кого и с какого места.
  //
  // Имя родителя клиент находит сам, по его id: сервер присылает только id,
  // и переименование родителя обязано быть видно в пометке сразу.
  {
    const talk = [
      { role: "user", content: "первый вопрос", error: null, reasoning: "", metrics: null },
      { role: "assistant", content: "первый ответ", error: null, reasoning: "", metrics: null },
      { role: "user", content: "второй вопрос", error: null, reasoning: "", metrics: null },
      { role: "assistant", content: "второй ответ", error: null, reasoning: "", metrics: null },
    ];
    const { client, server, $, settle, Evt } = freshClient({
      chats: [
        { label: "родитель", system: "ПРОМПТ РОДИТЕЛЯ", transcript: talk, history_len: talk.length },
        // Ветка чата, которого в списке нет: родителя удалили, а ветка его
        // пережила — она самостоятельный чат. Пометка обязана сказать это,
        // а не смолчать: смолчи она, ветка выглядела бы обычным чатом, хотя
        // начало разговора в ней чужое.
        { label: "сирота", branch: { parent_id: "ag_нет-такого", forked_at: 2 } },
      ],
    });
    client.init();
    await settle(20);
    const rows = () => $("#agent-list").querySelectorAll(".item");
    const noteOf = (i) => {
      const node = rows()[i] && rows()[i].querySelector(".item-branch");
      return node ? node.textContent : "(пометки нет)";
    };
    const open = (i) => rows()[i].querySelector(".item-open").dispatchEvent(new Evt("click"));

    check("у обычного чата пометки ветки в списке нет",
      noteOf(2) === "(пометки нет)", noteOf(2));
    check("ветка удалённого родителя всё равно помечена веткой",
      noteOf(3) === "ветка от удалённого чата — унесено 2 сообщений", noteOf(3));

    // Открываем родителя и ветвимся от первого ответа: карточка входит
    // в ветку целиком, вместе со своим вопросом, — это два сообщения.
    open(2);
    await settle(40);
    check("у родителя в панели пометки ветки нет",
      $("#branch-note").classList.contains("hidden"), $("#branch-note").textContent);

    const first = $("#feed").querySelectorAll(".card")[0];
    check("кнопка «Ветка отсюда» есть у ответа при любой стратегии",
      Boolean(first) && first.querySelectorAll(".icon-btn").map((b) => b.title)
        .includes("Ветка отсюда"),
      first && first.querySelectorAll(".icon-btn").map((b) => b.title).join(" | "));
    cardButton(first, "Ветка отсюда").dispatchEvent(new Evt("click"));
    await settle(80);

    const forks = server.state.requests.filter((r) => /\/fork$/.test(r.path));
    check("ветвление ушло одним запросом", forks.length === 1, String(forks.length));
    check("и унести просит ровно по эту карточку включительно",
      Boolean(forks[0] && forks[0].body) && forks[0].body.at === 2,
      forks[0] && JSON.stringify(forks[0].body));
    check("ветка открылась сама, без второго клика",
      $("#feed").textContent.includes("первый вопрос"), $("#feed").textContent.slice(0, 120));
    check("и в ней ровно унесённое начало, а не весь разговор родителя",
      !$("#feed").textContent.includes("второй вопрос"), $("#feed").textContent.slice(0, 200));
    check("в панели открытой ветки сказано, от кого она и сколько унесла",
      /ветка от «родитель» — унесено 2 сообщений/.test($("#branch-note").textContent) &&
        !$("#branch-note").classList.contains("hidden"),
      $("#branch-note").textContent);
    check("и там же — что ветка самостоятельна",
      /удаление родителя эту ветку не удалит/.test($("#branch-note").textContent),
      $("#branch-note").textContent);
    check("в списке у ветки та же пометка, что в панели",
      noteOf(4) === "ветка от «родитель» — унесено 2 сообщений", noteOf(4));

    // Переименование родителя видно в пометке сразу: имени в ней не
    // хранится, оно найдено по id.
    const row = rows()[2];
    const rename = row.querySelectorAll(".mini").find((b) => b.title === "Переименовать");
    if (!rename) check("у строки списка есть кнопка «Переименовать»", false, "её нет");
    else {
      rename.dispatchEvent(new Evt("click"));
      const field = row.querySelector(".item-rename");
      field.value = "родитель под новым именем";
      field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
      await settle(60);
      check("переименовали родителя — пометка ветки называет новое имя",
        noteOf(4) === "ветка от «родитель под новым именем» — унесено 2 сообщений", noteOf(4));
    }

    // И обратно к родителю: у него своя история целиком и своя панель.
    // Это и есть «переключайтесь между ветками» — ветка обычный чат, и
    // отдельного переключателя ей не нужно.
    open(2);
    await settle(40);
    check("вернулись к родителю — разговор у него целиком",
      $("#feed").textContent.includes("второй вопрос"), $("#feed").textContent.slice(0, 200));
    check("и пометка ветки в панели снова спрятана",
      $("#branch-note").classList.contains("hidden"), $("#branch-note").textContent);
  }

  // ── вкладка «Память»: три слоя видны и управляются ──
  //
  // Первое место в интерфейсе, где видны рабочий и долговременный слои: до
  // сих пор факты и сводки можно было разглядеть только в просмотре промпта,
  // а долговременной памяти не было вовсе. Проверяется весь круг: вкладка
  // открылась, в каждом разделе своё, факт продвинут в долговременный слой
  // нажатием, запись заведена формой с явно выбранным родом и удалена по
  // номеру — и всё это на один поход за слоями, а не на каждую отрисовку.
  //
  // Чатов в посеве четыре, по одному на стратегию с обрезкой плюс тот, у кого
  // выписки ещё нет: число «уезжает дословно» считается по всем четырём
  // ветвям одной формулы, и ветвь, которую не исполнило ни одно утверждение,
  // вольна врать словом или брать не ту границу — проверке это не видно.
  {
    const SECRET = "sk-стенд-секрет-длинный-достаточно";
    const talk = [];
    for (let i = 0; i < 4; i += 1) {
      talk.push({ role: "user", content: "вопрос " + i, error: null, reasoning: "", metrics: null });
      talk.push({ role: "assistant", content: "ответ " + i, error: null, reasoning: "", metrics: null });
    }
    const { client, server, $, settle, Evt } = freshClient({
      secret: SECRET,
      chats: [
        { label: "с фактами", strategy: "facts", keep_last: 2, transcript: talk.slice(0, 6) },
        // Тот же выбор, что у соседа, но выписки ещё нет ни одной: фактов
        // нет — не режется ничего, ровно как на сервере. Число рядом со
        // стратегией само по себе не значит ничего.
        { label: "без выписки", strategy: "facts", keep_last: 2, transcript: talk.slice(0, 4) },
        // Окно — единственная стратегия, которая начало **отбрасывает**,
        // а не заменяет, и слово под числами обязано это различать. Заодно
        // выключатель памяти у него в «выключена»: строка о том, едут ли
        // записи в промпт этого чата, есть у каждого положения.
        { label: "окно", strategy: "window", keep_last: 2, memory: "off",
          transcript: talk.slice(0, 6) },
        // Сводок две: срез считается по **последней**, и чат с одним
        // сворачиванием этого не различил бы.
        { label: "со сводками", strategy: "summary", keep_last: 2, transcript: talk },
      ],
      working: {
        "с фактами": {
          facts: [{ key: "цель", value: "собрать ТЗ" }, { key: "язык", value: "Kotlin" }],
          // Выписка отстала от истории: последнее извлечение прочитало две
          // первые реплики. Хвост от этого уезжает длиннее просимых двух —
          // ровно так режет сервер (`facts_cover`), и раздел обязан назвать
          // то число, которое правда уедет, а не то, которое просили.
          facts_upto: 2,
          summaries: [{ seq: 0, upto: 4, content: "говорили про ТЗ и сроки" }],
        },
        "со сводками": {
          summaries: [
            { seq: 0, upto: 2, content: "первое сворачивание" },
            { seq: 1, upto: 4, content: "второе сворачивание" },
          ],
        },
      },
      records: [{ kind: "profile", content: "пишу на Kotlin" }],
    });
    client.init();
    await settle(30);
    const open = (i) => $("#agent-list").querySelectorAll(".item-open")[i].dispatchEvent(new Evt("click"));
    open(2);
    await settle(40);

    const layerCalls = () =>
      server.state.requests.filter((r) => /\/memory$/.test(r.path) && r.method === "GET");
    const memoryCalls = () => server.state.requests.filter((r) => /^\/api\/memory/.test(r.path));
    const posts = () => memoryCalls().filter((r) => r.method === "POST");
    const patches = () => server.state.requests.filter((r) => r.method === "PATCH");
    const openTab = (which) =>
      document.querySelectorAll(".tab").find((t) => t.dataset.tab === which)
        .dispatchEvent(new Evt("click"));
    const texts = (sel) => $(sel).querySelectorAll(".mem-text").map((n) => n.textContent);
    const nums = (sel) => $(sel).querySelectorAll(".mem-v").map((n) => n.textContent).join("|");
    const shown = (sel) => $(sel).textContent;

    // Лениво, и в обе стороны. Обмен слои меняет — история выросла, — но при
    // закрытой вкладке за ними всё равно никто не идёт: перечитывание висит
    // на событии, а не на самой отрисовке, и разница видна только так —
    // обменом до открытия вкладки.
    $("#input").value = "вопрос при закрытой вкладке";
    $("#composer").requestSubmit();
    await settle(120);
    check("пока вкладка «Память» закрыта, за слоями не ходят вовсе — и после обмена тоже",
      layerCalls().length === 0, JSON.stringify(layerCalls().map((r) => r.path)));

    openTab("memory");
    await settle(40);

    check("третья вкладка открылась",
      !$("#tab-memory").classList.contains("hidden"), "она спрятана");
    check("и спрятала обе прежние",
      $("#tab-model").classList.contains("hidden") && $("#tab-agent").classList.contains("hidden"),
      `модель ${$("#tab-model").classList.contains("hidden")}, агент ${$("#tab-agent").classList.contains("hidden")}`);
    check("за слоями сходили ровно один раз — на открытие вкладки",
      layerCalls().length === 1, JSON.stringify(layerCalls().map((r) => r.path)));

    // Краткосрочная: длину истории называет сервер, а сколько из неё уедет
    // дословно — считается по той же формуле, что режет промпт. Восемь
    // сообщений (шесть посева плюс обмен), хвост просили в два, но выписка
    // прочитала только две первые реплики — заменены ими ровно две, и
    // дословно уезжают шесть. Меньшее из двух, как в `facts_cover`: срез
    // по одному хвосту унёс бы начало, которого выписка никогда не видела.
    check("в краткосрочной — длина истории и сколько из неё уезжает дословно",
      /Сообщений в истории/.test(shown("#mem-short")) && /Уезжает дословно/.test(shown("#mem-short")) &&
        nums("#mem-short") === "8|6",
      shown("#mem-short"));
    check("и сказано, куда делось начало: выписка, а не окно и не сводка",
      /Остальные 2 — заменено выпиской фактов\./.test(shown("#mem-short")), shown("#mem-short"));

    // Рабочая: факты строками «ключ: значение» — в той же форме, в какой они
    // уезжают в промпт, и сводка со своей границей.
    check("факты рабочего слоя показаны строками «ключ: значение»",
      texts("#mem-working").slice(0, 2).join(" | ") === "цель: собрать ТЗ | язык: Kotlin",
      JSON.stringify(texts("#mem-working")));
    check("и сводка этого чата видна там же, со своей границей",
      texts("#mem-working").includes("говорили про ТЗ и сроки") &&
        /вместо первых 4 сообщений/.test(shown("#mem-working")), shown("#mem-working"));

    // Долговременная: общий список с подписью рода по-русски — и строка
    // о том, что с этими записями будет в промпте **этого** чата.
    check("в долговременной — запись с подписью рода",
      texts("#mem-long").join(" | ") === "пишу на Kotlin" &&
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent).join("|") === "профиль",
      shown("#mem-long"));
    check("и сказано, что у этого чата записи в промпт едут",
      /Записи едут врезкой в промпт этого чата при любой стратегии\./.test(shown("#mem-long")),
      shown("#mem-long"));

    // ── «Запомнить надолго»: факт переезжает в долговременный слой ──
    const promote = $("#mem-working").querySelectorAll(".mem-btn")
      .find((b) => b.title === "Запомнить надолго");
    if (!promote) check("у факта есть кнопка «Запомнить надолго»", false, "кнопки нет");
    else {
      promote.dispatchEvent(new Evt("click"));
      await settle(40);
      check("«Запомнить надолго» шлёт добавление с родом «знание»",
        posts().length === 1 && posts()[0].path === "/api/memory" && posts()[0].body.kind === "knowledge",
        JSON.stringify(posts().map((r) => r.body)));
      check("и текстом самого факта, в той же форме «ключ: значение»",
        posts().length === 1 && posts()[0].body.content === "цель: собрать ТЗ",
        JSON.stringify(posts().map((r) => r.body && r.body.content)));
      check("запись появилась в долговременном списке тут же",
        texts("#mem-long").join(" | ") === "пишу на Kotlin | цель: собрать ТЗ", shown("#mem-long"));
      check("и рядом с ней подпись рода — «знание»",
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent).join("|") ===
          "профиль|знание",
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent).join("|"));
    }

    // ── форма: род выбирает человек, и до выбора не уходит ничего ──
    //
    // Умолчания у рода нет ни на сервере, ни в форме: список открывается
    // на пустом пункте. Предвыбери форма первый настоящий род — «явно
    // выбирал» из задания стало бы «форма выбрала за него», ровно то, чего
    // `_kind_field` не даёт сделать серверу.
    const beforeForm = posts().length;
    check("до выбора род не подставлен: список стоит на пустом пункте",
      $("#mem-kind").value === "", JSON.stringify($("#mem-kind").value));
    $("#mem-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("без выбранного рода добавление не уходит вовсе",
      posts().length === beforeForm, JSON.stringify(posts().slice(beforeForm).map((r) => r.body)));
    check("и форма говорит, чего не хватает",
      /Род записи не выбран/.test($("#mem-status").textContent), $("#mem-status").textContent);

    // Род выбран, текста нет: пустая запись уехала бы в промпт строкой
    // «решение: » и заняла бы место врезки, ничего не сказав.
    // Выбор рода — не правка конфига чата. Пролив панели слушает `change`
    // на всей панели разом, и поле памяти, попавшее в его сито, уезжало бы
    // PATCH'ем ни о чём. Слепок снимается **до** выбора: обмен выше свой
    // PATCH уже сделал, и считается прирост, а не общее число.
    const patchesBeforeKind = patches().length;
    $("#mem-kind").value = "decision";
    $("#mem-kind").dispatchEvent(new Evt("change"));
    await settle(40);
    $("#mem-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("с пустым текстом добавление тоже не уходит",
      posts().length === beforeForm, JSON.stringify(posts().slice(beforeForm).map((r) => r.body)));
    check("и причина названа: записывать нечего",
      /Текст записи пуст/.test($("#mem-status").textContent), $("#mem-status").textContent);

    $("#mem-content").value = "  оплата только картой  ";
    $("#mem-add").dispatchEvent(new Evt("click"));
    await settle(40);
    const added = posts().slice(-1)[0];
    check("форма шлёт тот род, что выбран в списке",
      Boolean(added) && added.body.kind === "decision" && added.body.content === "оплата только картой",
      JSON.stringify(added && added.body));
    check("и запись встала в список третьей",
      texts("#mem-long").join(" | ") === "пишу на Kotlin | цель: собрать ТЗ | оплата только картой",
      shown("#mem-long"));
    check("поле ввода после успеха пусто — второй клик не заведёт ту же запись молча",
      $("#mem-content").value === "", $("#mem-content").value);
    check("выбор рода записи конфиг чата не трогает: PATCH'ей не прибавилось",
      patches().length === patchesBeforeKind,
      JSON.stringify(patches().slice(patchesBeforeKind).map((r) => r.body)));

    // ── список пополняется записанным, а не присланным ──
    //
    // Ключ, случайно вставленный в текст, сервер вырезает по дороге в базу
    // (`redact`), и ручка отдаёт уже чистую строку. Показывай клиент своё
    // тело запроса — на экране остался бы ключ, которого в базе нет.
    $("#mem-kind").value = "knowledge";
    $("#mem-content").value = "ключ " + SECRET;
    $("#mem-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("в запросе ушёл текст как есть",
      posts().slice(-1)[0].body.content === "ключ " + SECRET,
      JSON.stringify(posts().slice(-1)[0].body.content));
    check("а в списке стоит записанное: секрет вычищен ответом ручки",
      texts("#mem-long").slice(-1)[0] === "ключ ***", JSON.stringify(texts("#mem-long")));

    // ── удаление: уходит номер именно той записи ──
    const rows = $("#mem-long").querySelectorAll(".mem-item");
    const trash = rows[1].querySelectorAll(".mini").find((b) => b.title === "Забыть запись");
    if (!trash) check("у записи памяти есть кнопка «Забыть запись»", false, "кнопки нет");
    else {
      trash.dispatchEvent(new Evt("click"));
      await settle(40);
      const deleted = memoryCalls().filter((r) => r.method === "DELETE");
      check("удаление шлёт номер именно этой записи, а не соседней",
        deleted.length === 1 && deleted[0].path === "/api/memory/2",
        JSON.stringify(deleted.map((r) => r.path)));
      check("из списка ушла она одна, соседние остались",
        texts("#mem-long").join(" | ") === "пишу на Kotlin | оплата только картой | ключ ***",
        shown("#mem-long"));
    }

    // Весь круг — продвижение, добавление, удаление — прошёл на одном походе
    // за слоями: список правится по ответу ручки, а не перечитыванием.
    check("за всё время слои перечитаны один раз, а не на каждую перерисовку",
      layerCalls().length === 1, JSON.stringify(layerCalls().map((r) => r.path)));

    // ── открыли другой чат: первые два слоя теперь его ──
    open(3);
    await settle(60);
    check("в другом чате краткосрочная показывает его историю, а не прежнюю",
      nums("#mem-short") === "4|4", shown("#mem-short"));
    check("выписки у него нет — не срезано ничего, и сказано именно это",
      /Вся история уезжает в модель дословно\./.test(shown("#mem-short")), shown("#mem-short"));
    check("фактов у него своих нет, и об этом сказано словами",
      /Фактов ещё нет/.test(shown("#mem-working")), shown("#mem-working"));
    check("а долговременный слой у обоих чатов один и тот же",
      texts("#mem-long").join(" | ") === "пишу на Kotlin | оплата только картой | ключ ***",
      shown("#mem-long"));
    check("слои перечитаны, потому что чат другой — второй раз, а не третий",
      layerCalls().length === 2, JSON.stringify(layerCalls().map((r) => r.path)));

    // ── окно: начало не заменено, а отброшено, и памяти этот чат не видит ──
    open(4);
    await settle(60);
    check("у окна дословно уезжает ровно окно",
      nums("#mem-short") === "6|2", shown("#mem-short"));
    check("и слово под числами — «отброшено», а не «заменено»",
      /Остальные 4 — отброшено окном\./.test(shown("#mem-short")), shown("#mem-short"));
    check("у чата с выключенной памятью сказано, что записи в его промпт не едут",
      /Выключатель этого чата стоит в «выключена»/.test(shown("#mem-long")), shown("#mem-long"));
    check("но сами записи он видит: слой общий, а выключатель про промпт",
      texts("#mem-long").join(" | ") === "пишу на Kotlin | оплата только картой | ключ ***",
      shown("#mem-long"));

    // ── сводка: срез считается по последней, а не по первой ──
    open(5);
    await settle(60);
    check("у суммаризации срез — по последней сводке",
      nums("#mem-short") === "8|4", shown("#mem-short"));
    check("и слово своё: начало заменено сводкой",
      /Остальные 4 — заменено сводкой\./.test(shown("#mem-short")), shown("#mem-short"));
    check("обе сводки видны в рабочем слое, каждая со своей границей",
      texts("#mem-working").join(" | ") === "первое сворачивание | второе сворачивание" &&
        /вместо первых 2 сообщений/.test(shown("#mem-working")) &&
        /вместо первых 4 сообщений/.test(shown("#mem-working")),
      shown("#mem-working"));

    // ── переключение на соседнюю вкладку за слоями не ходит ──
    //
    // Обратная сторона ленивости: запрос висит на открытии **этой** вкладки,
    // а не на переключении вообще. Считаем после четырёх открытий чатов.
    const beforeSwitch = layerCalls().length;
    openTab("agent");
    await settle(40);
    openTab("model");
    await settle(40);
    check("переключение на «Агент» и «Модель» за слоями не ходит",
      layerCalls().length === beforeSwitch, JSON.stringify(layerCalls().map((r) => r.path)));
    check("и вкладка «Память» при этом спрятана",
      $("#tab-memory").classList.contains("hidden"), "она открыта");
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
