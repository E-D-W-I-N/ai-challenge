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
  // Суммаризация — вторая строка про ту же ручку не для симметрии: значения,
  // которого нет в разметке `<select>`, поле не примет вовсе, и строка
  // покраснеет на пустом значении. Забудь про `<option>` — переключатель знал
  // бы вариант, а выбрать его было бы нечем.
  ["f-strategy", "summary", "strategy", "summary"],
  ["f-keep_last", "6", "keep_last", 6],
  ["f-compress_every", "10", "compress_every", 10],
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
    // «Факты» были стратегией ещё вчера, и чат с ними мог остаться в базе:
    // теперь это значение — такое же незнакомое, как любое другое.
    check("в списке вариантов три пункта, и «Фактов» среди них нет",
      [...$("#f-strategy").querySelectorAll("option")].map((o) => o.value).join(",") ===
        "full,window,summary",
      [...$("#f-strategy").querySelectorAll("option")].map((o) => o.value).join(","));
  }

  // ── видны только те поля, что работают при выбранной стратегии ──
  //
  // Поле, которое принимает число и молча его игнорирует, врёт ровно так же,
  // как молчаливая обрезка: по виду оно рабочее. Поэтому при `full` чисел нет
  // вовсе, у окна одно, у суммаризации два — и одно и то же поле подписано
  // по-разному, потому что значит разное. Вариантов ровно три: «Факты»
  // из списка ушли — выписку агент ведёт при любом из них.
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
      $(".brand-sub").textContent === "чат · день 13", $(".brand-sub").textContent);

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

  // ── врезка памяти обрезкой не считается: приписки под ответом нет ──
  //
  // Раньше врезка фактов заменяла собой начало разговора, и число заменённых
  // реплик стояло под ответом третьим ключом. Теперь память едет **поверх**
  // любого варианта и не заменяет собой ничего: реплики уехали все до одной,
  // и приписывать под ответом нечего. Приписка осталась ровно там, где
  // реплики правда не уехали, — у окна и у сводки.
  {
    const withMemory = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "",
        metrics: { prompt_tokens: 200, completion_tokens: 50, total_tokens: 250,
                   cost_usd: 0.0001 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "с памятью", transcript: withMemory, history_len: withMemory.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("под ответом с врезкой памяти приписки про срез нет",
      usageText($("#feed"), ".usage-tokens") ===
        "входные токены 200 · выходные токены 50 · всего токенов 250 · $0.000100",
      usageText($("#feed"), ".usage-tokens"));
    check("и плиток по-прежнему шесть — своей у памяти нет",
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
    // соседний вариант, не дожидаясь конца генерации.
    $("#f-strategy").value = "window";
    $("#f-strategy").dispatchEvent(new Evt("change"));
    check("стратегию видно переключить и пока идёт ответ",
      shownContext($) === "keep_last=Размер окна, сообщений", shownContext($));

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

  // ── врезка рабочей памяти: своя подпись и свой слот ──
  //
  // Подписать врезку клиент обязан по слоту от сервера, а не по тексту
  // сообщения, и подписать **фактами**, а не сводкой: под записью о задаче
  // «сводка начала разговора» была бы враньём ровно там, где подпись для
  // того и заведена.
  //
  // Врезка приезжает своим слотом, без всякой стратегии: обрезка у чата —
  // «вся история», а записи о задаче всё равно встают в промпт. Строки
  // состояния у неё нет и быть не может: служебного вызова за ней не стоит
  // — её вписал человек, и паузы перед ответом она не даёт.
  {
    const FACTS = "[факты о разговоре]\nцель: собрать ТЗ\nсрок: май\n[конец фактов о разговоре]";
    const { client, $, settle, Evt } = freshClient({
      delay: 60,
      facts: FACTS,
    });
    client.init();
    await settle(40);
    $("#input").value = "вопрос с фактами";
    $("#composer").requestSubmit();
    await settle(90);          // ответ ещё идёт

    const status = $("#feed").querySelector(".card-status");
    check("строки состояния у врезки рабочей памяти нет: служебного вызова за ней нет",
      !status, status && status.textContent);

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

  // ── три врезки разом: обе памяти и врезка стратегии ──
  //
  // Главное место дня на клиенте. Долговременная память — слой не этого
  // разговора, рабочая — состояние этой задачи, сводка — свёрнутое начало
  // самого разговора; в одном промпте они стоят втроём и ровно в этом
  // порядке. Все три места называет сервер, тремя разными полями кадра
  // `start`, и подписать их клиент обязан по ним, а не по тексту сообщений:
  // считай он слоты сам — подписал бы памятью факты ровно тогда, когда
  // врезок больше одной.
  {
    const MEMORY = "[долговременная память]\nо собеседнике: пишу на Kotlin\n[конец долговременной памяти]";
    const FACTS = "[факты о разговоре]\nцель: собрать ТЗ\n[конец фактов о разговоре]";
    const SUM = "[пересказ начала разговора, свёрнуто сообщений: 2]\nбыло то-то";
    const { client, server, $, settle, Evt } = freshClient({
      memory: MEMORY,
      facts: FACTS,
      service: { insert: SUM, covered: 0, strategy: "summary" },
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
    check("врезок в промпте три, и подписаны они разными словами в нужном порядке",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "долговременная память", "факты о разговоре",
        "сводка начала разговора", "сообщение пользователя",
      ]), JSON.stringify(roles));
    check("и под подписями стоят те самые тексты, а не перепутанные местами",
      texts[1] === MEMORY && texts[2] === FACTS && texts[3] === SUM,
      JSON.stringify(texts));
  }

  // ── без памяти подпись не вылезает ни над одним сообщением ──
  //
  // Обратная половина: `memory_at` приходит пустым — и пустым он приходит
  // и когда слой пуст, и когда хранилища нет вовсе.
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
  // нажатием, запись заведена формой с явно выбранным типом и удалена по
  // номеру — и всё это на один поход за слоями, а не на каждую отрисовку.
  //
  // Чатов в посеве три, по одному на каждую ветвь формулы «уезжает
  // дословно»: окно, «вся история» и сводка. Ветвь, которую не исполнило
  // ни одно утверждение, вольна врать словом или брать не ту границу —
  // проверке это не видно.
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
        { label: "с памятью", strategy: "window", keep_last: 2, transcript: talk.slice(0, 6) },
        // Чат без обрезки: вся история уезжает дословно, и раздел обязан
        // сказать это словами, а не пустым местом.
        { label: "без обрезки", strategy: "full", transcript: talk.slice(0, 4) },
        // Сводок две: срез считается по **последней**, и чат с одним
        // сворачиванием этого не различил бы.
        { label: "со сводками", strategy: "summary", keep_last: 2, transcript: talk },
      ],
      working: {
        "с памятью": {
          records: [
            { kind: "goal", content: "собрать ТЗ" },
            { kind: "limit", content: "только Kotlin" },
          ],
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
    // сообщений (шесть посева плюс обмен), окно просили в два — отброшены
    // шесть. Зажима по «докуда дочитала память» здесь больше нет: читать
    // её стало некому, и окно режет ровно столько, сколько просили.
    check("в краткосрочной — длина истории и сколько из неё уезжает дословно",
      /Сообщений в истории/.test(shown("#mem-short")) && /Уезжает дословно/.test(shown("#mem-short")) &&
        nums("#mem-short") === "8|2",
      shown("#mem-short"));
    check("и сказано, куда делось начало: отброшено окном",
      /Остальные 6 — отброшено окном\./.test(shown("#mem-short")), shown("#mem-short"));
    // Сводка — рядом с историей, а не в рабочей памяти: она не запомненное,
    // а чем заменено то, что не уехало дословно. Выключи сворачивание —
    // не пропадёт ничего, она соберётся заново.
    check("и сводка этого чата видна там же, со своей границей",
      texts("#mem-short").includes("говорили про ТЗ и сроки") &&
        /вместо первых 4 сообщений/.test(shown("#mem-short")), shown("#mem-short"));

    // Рабочая: записи строками «тип: содержимое» — в той же форме, в какой
    // они уезжают в промпт. Сводок здесь нет ни одной.
    check("записи рабочего слоя показаны строками «тип: содержимое»",
      texts("#mem-working").join(" | ") === "цель: собрать ТЗ | ограничение: только Kotlin",
      JSON.stringify(texts("#mem-working")));
    check("а сводок в рабочем слое нет: ни текста, ни заголовка над ним",
      !/говорили про ТЗ/.test(shown("#mem-working")) &&
        !/Сводки/.test(shown("#mem-working")),
      shown("#mem-working"));

    // Долговременная: общий список с подписью типа по-русски.
    check("в долговременной — запись с подписью типа",
      texts("#mem-long").join(" | ") === "пишу на Kotlin" &&
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent).join("|") === "о собеседнике",
      shown("#mem-long"));

    // ── форма: тип не предвыбран с самой первой отрисовки ──
    //
    // Умолчания у типа нет ни на сервере, ни в форме: список открывается
    // на пустом пункте. Предвыбери форма первый настоящий тип — «явно
    // выбирал» из задания стало бы «форма выбрала за него», ровно то, чего
    // `_kind_field` не даёт сделать серверу.
    check("до выбора тип не подставлен: список стоит на пустом пункте",
      $("#mem-kind").value === "", JSON.stringify($("#mem-kind").value));

    // ── «Запомнить надолго»: запись переезжает в долговременный слой,
    //    и тип у неё спрашивают ──
    //
    // Это единственное место, где запись меняет слой, и до сих пор
    // единственное, где тип выбирал код: в кнопке стояло `knowledge`. Против
    // собственного правила — и наугад: переносимой записи «о собеседнике»
    // подходит ничуть не реже. Теперь кнопка кладёт строку в ту же форму,
    // которой слой пополняют руками, и ждёт выбора.
    const promote = $("#mem-working").querySelectorAll(".mem-btn")
      .find((b) => b.title === "Запомнить надолго");
    if (!promote) check("у записи есть кнопка «Запомнить надолго»", false, "кнопки нет");
    else {
      promote.dispatchEvent(new Evt("click"));
      await settle(40);
      check("«Запомнить надолго» сама ничего не записывает: тип ещё не выбран",
        posts().length === 0, JSON.stringify(posts().map((r) => r.body)));
      // Фокус переводится на список типов — тем же нажатием: без этого
      // «спрашиваю тип» осталось бы строкой состояния под чужим разделом,
      // а человек смотрел бы на запись, которая молча никуда не уехала.
      check("а кладёт запись в форму слоя, в той же форме «тип: содержимое», без типа и с фокусом на нём",
        $("#mem-content").value === "цель: собрать ТЗ" && $("#mem-kind").value === "" &&
          document.activeElement === $("#mem-kind"),
        JSON.stringify([$("#mem-content").value, $("#mem-kind").value,
                        document.activeElement && document.activeElement.id]));
      check("и сказано, чего ждут: при переносе тип выбирает человек",
        /при переносе тип выбирает человек/.test($("#mem-status").textContent),
        $("#mem-status").textContent);

      // Правила у переноса те же, что у добавления руками: форма одна.
      $("#mem-add").dispatchEvent(new Evt("click"));
      await settle(40);
      check("без выбранного типа перенос не уходит вовсе",
        posts().length === 0, JSON.stringify(posts().map((r) => r.body)));
      check("и форма говорит, чего не хватает",
        /Тип записи не выбран/.test($("#mem-status").textContent), $("#mem-status").textContent);

      $("#mem-kind").value = "knowledge";
      $("#mem-kind").dispatchEvent(new Evt("change"));
      $("#mem-add").dispatchEvent(new Evt("click"));
      await settle(40);
      check("с выбранным типом перенос уходит добавлением — и типом человека",
        posts().length === 1 && posts()[0].path === "/api/memory" && posts()[0].body.kind === "knowledge",
        JSON.stringify(posts().map((r) => r.body)));
      check("и текстом самой записи, в той же форме «тип: содержимое»",
        posts().length === 1 && posts()[0].body.content === "цель: собрать ТЗ",
        JSON.stringify(posts().map((r) => r.body && r.body.content)));
      check("запись появилась в долговременном списке тут же",
        texts("#mem-long").join(" | ") === "пишу на Kotlin | цель: собрать ТЗ", shown("#mem-long"));
      check("и рядом с ней подпись типа — «факт», тот, что выбрали",
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent).join("|") ===
          "о собеседнике|факт",
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent).join("|"));
      check("поле формы после переноса пусто — второй клик не заведёт ту же запись молча",
        $("#mem-content").value === "", $("#mem-content").value);
    }

    // ── форма: тип выбран, текста нет ──
    //
    // Пустая запись уехала бы в промпт строкой «решение: » и заняла бы место
    // врезки, ничего не сказав.
    // Выбор типа — не правка конфига чата. Пролив панели слушает `change`
    // на всей панели разом, и поле памяти, попавшее в его сито, уезжало бы
    // PATCH'ем ни о чём. Слепок снимается **до** выбора: обмен выше свой
    // PATCH уже сделал, и считается прирост, а не общее число.
    const beforeForm = posts().length;
    const patchesBeforeKind = patches().length;
    $("#mem-kind").value = "decision";
    $("#mem-kind").dispatchEvent(new Evt("change"));
    await settle(40);
    $("#mem-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("с пустым текстом добавление не уходит",
      posts().length === beforeForm, JSON.stringify(posts().slice(beforeForm).map((r) => r.body)));
    check("и причина названа: записывать нечего",
      /Текст записи пуст/.test($("#mem-status").textContent), $("#mem-status").textContent);

    $("#mem-content").value = "  оплата только картой  ";
    $("#mem-add").dispatchEvent(new Evt("click"));
    await settle(40);
    const added = posts().slice(-1)[0];
    check("форма шлёт тот тип, что выбран в списке",
      Boolean(added) && added.body.kind === "decision" && added.body.content === "оплата только картой",
      JSON.stringify(added && added.body));
    check("и запись встала в список третьей",
      texts("#mem-long").join(" | ") === "пишу на Kotlin | цель: собрать ТЗ | оплата только картой",
      shown("#mem-long"));
    check("поле ввода после успеха пусто — второй клик не заведёт ту же запись молча",
      $("#mem-content").value === "", $("#mem-content").value);
    check("выбор типа записи конфиг чата не трогает: PATCH'ей не прибавилось",
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
    check("обрезки у него нет — не срезано ничего, и сказано именно это",
      /Вся история уезжает в модель дословно\./.test(shown("#mem-short")), shown("#mem-short"));
    check("записей у него своих нет, и об этом сказано словами",
      /Записей нет\./.test(shown("#mem-working")), shown("#mem-working"));
    check("а долговременный слой у обоих чатов один и тот же",
      texts("#mem-long").join(" | ") === "пишу на Kotlin | оплата только картой | ключ ***",
      shown("#mem-long"));
    check("слои перечитаны, потому что чат другой — второй раз, а не третий",
      layerCalls().length === 2, JSON.stringify(layerCalls().map((r) => r.path)));

    // ── сводка: срез считается по последней, а не по первой ──
    open(4);
    await settle(60);
    check("у суммаризации срез — по последней сводке",
      nums("#mem-short") === "8|4", shown("#mem-short"));
    check("и слово своё: начало заменено сводкой",
      /Остальные 4 — заменено сводкой\./.test(shown("#mem-short")), shown("#mem-short"));
    check("обе сводки видны рядом с историей, каждая со своей границей",
      texts("#mem-short").join(" | ") === "первое сворачивание | второе сворачивание" &&
        /вместо первых 2 сообщений/.test(shown("#mem-short")) &&
        /вместо первых 4 сообщений/.test(shown("#mem-short")),
      shown("#mem-short"));

    // ── переключение на соседнюю вкладку за слоями не ходит ──
    //
    // Обратная сторона ленивости: запрос висит на открытии **этой** вкладки,
    // а не на переключении вообще. Считаем после трёх открытий чатов.
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

  // ── слои не доехали: показывать нечего, и записывать тоже ──
  //
  // Ручка трёх слоёв отказывает так же, как любая другая: занятая база — 503.
  // Тогда у вкладки нет записей, и форма не предлагает записать в никуда —
  // записать было бы некуда. Причина при этом названа, а не заменена пустым
  // разделом: «пусто» и «не доехало» — разные новости.
  {
    const { client, $, settle, Evt } = freshClient({ failLayers: true });
    client.init();
    await settle(30);
    document.querySelectorAll(".tab").find((t) => t.dataset.tab === "memory")
      .dispatchEvent(new Evt("click"));
    await settle(40);
    check("слои не доехали — раздел называет причину, а не молчит",
      /база занята/.test($("#mem-working").textContent), $("#mem-working").textContent);
    check("и формы рабочей памяти нет: записывать некуда",
      $("#mem-work-form").classList.contains("hidden"), "форма показана");
  }

  // ── рабочий слой правится руками ──
  //
  // Задание дня требует, чтобы человек **явно выбирал, что и куда**
  // сохраняется, — и с этого дня другого пути в оба слоя памяти нет вовсе.
  // Здесь проверяется весь круг: правка и удаление уходят с номером **этой**
  // записи, тип правится наравне с текстом, форма без выбранного типа
  // не шлёт ничего, а списки правятся ответами ручек, а не перечитыванием
  // слоёв.
  {
    const talk = [];
    for (let i = 0; i < 2; i += 1) {
      talk.push({ role: "user", content: "вопрос " + i, error: null, reasoning: "", metrics: null });
      talk.push({ role: "assistant", content: "ответ " + i, error: null, reasoning: "", metrics: null });
    }
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "правки", strategy: "full", transcript: talk }],
      working: {
        "правки": {
          records: [
            { kind: "goal", content: "собрать ТЗ" },
            { kind: "limit", content: "только Kotlin" },
          ],
        },
      },
      // Тип у посева — «факт», чтобы правка ниже меняла его на настоящий
      // другой: смена типа на тот же самый ничего бы не отправила, и
      // утверждение о ней стояло бы на пустом месте.
      records: [{ kind: "knowledge", content: "пишу на Kotlin" }],
    });
    client.init();
    await settle(30);

    const openTab = (which) =>
      document.querySelectorAll(".tab").find((t) => t.dataset.tab === which)
        .dispatchEvent(new Evt("click"));
    const requests = (method, re) =>
      server.state.requests.filter((r) => r.method === method && re.test(r.path));
    const layerCalls = () => requests("GET", /\/memory$/);
    const texts = (sel) => $(sel).querySelectorAll(".mem-text").map((n) => n.textContent);
    const rows = () => $("#mem-working").querySelectorAll(".mem-item");
    const seqOf = (i) => server.state.working["правки"].records[i].seq;

    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    openTab("memory");
    await settle(40);
    check("вместе со слоем показана и форма: записывать есть куда",
      !$("#mem-work-form").classList.contains("hidden"), "форма спрятана");

    // ── правка: уходит номер именно этой записи ──
    const pencil = rows()[0].querySelectorAll(".mini").find((b) => b.title === "Поправить запись");
    if (!pencil) check("у записи рабочего слоя есть кнопка правки", false, "кнопки нет");
    else {
      pencil.dispatchEvent(new Evt("click"));
      await settle(20);
      const field = $("#mem-working").querySelector(".mem-edit");
      check("правка открывает поле с прежним текстом записи, а не пустое",
        Boolean(field) && field.value === "собрать ТЗ", field && JSON.stringify(field.value));
      field.value = "собрать ТЗ к маю";
      field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
      await settle(40);
      const patched = requests("PATCH", /\/working\//);
      check("правка шлёт номер именно этой записи, а не соседней",
        patched.length === 1 && patched[0].path.endsWith("/working/" + seqOf(0)),
        JSON.stringify(patched.map((r) => r.path)));
      check("и телом — только новый текст",
        patched.length === 1 && JSON.stringify(patched[0].body) === JSON.stringify({ content: "собрать ТЗ к маю" }),
        JSON.stringify(patched.map((r) => r.body)));
      check("в списке стоит поправленное",
        texts("#mem-working")[0] === "цель: собрать ТЗ к маю", JSON.stringify(texts("#mem-working")));
      // ── остальные четыре выхода из правки ──
      //
      // Enter — только один из пяти. Уход фокуса сохраняет (иначе правка
      // теряется молча от нажатия мимо), Escape отменяет, пустое поле и текст
      // слово в слово прежний не шлют ничего, а замок не даёт одной правке
      // уехать дважды. Считаем PATCH'и: лишний и пропавший видны одинаково.
      const patchCount = () => requests("PATCH", /\/working\//).length;
      const editRow = async (index, value, finish) => {
        rows()[index].querySelectorAll(".mini")
          .find((b) => b.title === "Поправить запись").dispatchEvent(new Evt("click"));
        await settle(20);
        const field = $("#mem-working").querySelector(".mem-edit");
        if (!field) return null;
        if (value !== null) field.value = value;
        finish(field);
        await settle(40);
        return field;
      };

      const beforeEscape = patchCount();
      await editRow(0, "правка, которой не будет",
        (f) => f.dispatchEvent(new Evt("keydown", { key: "Escape" })));
      check("Escape отменяет правку: запрос не уходит",
        patchCount() === beforeEscape, patchCount() + " против " + beforeEscape);
      check("и в списке остаётся прежний текст",
        texts("#mem-working")[0] === "цель: собрать ТЗ к маю", JSON.stringify(texts("#mem-working")));

      await editRow(0, "", (f) => f.dispatchEvent(new Evt("keydown", { key: "Enter" })));
      check("пустое поле правкой не считается: запись не теряется молча",
        patchCount() === beforeEscape && texts("#mem-working")[0] === "цель: собрать ТЗ к маю",
        patchCount() + " | " + JSON.stringify(texts("#mem-working")));

      await editRow(0, null, (f) => f.dispatchEvent(new Evt("keydown", { key: "Enter" })));
      check("текст слово в слово прежний тоже не шлют: правки в нём нет",
        patchCount() === beforeEscape, patchCount() + " против " + beforeEscape);

      // Потеря фокуса сохраняет — и это единственный выход, который случается
      // сам: человек нажал мимо поля, а правку терять нельзя.
      await editRow(0, "собрать ТЗ к концу мая", (f) => f.blur());
      check("уход фокуса правку сохраняет, а не теряет",
        patchCount() === beforeEscape + 1 &&
          texts("#mem-working")[0] === "цель: собрать ТЗ к концу мая",
        patchCount() + " | " + JSON.stringify(texts("#mem-working")));

      // Enter и следующий за ним уход фокуса — одна правка, а не две:
      // без замка ушли бы два запроса на одно нажатие.
      const beforeTwice = patchCount();
      await editRow(0, "собрать ТЗ к июню", (f) => {
        f.dispatchEvent(new Evt("keydown", { key: "Enter" }));
        f.blur();
      });
      check("Enter и уход фокуса за ним — одна правка, а не две",
        patchCount() === beforeTwice + 1, patchCount() + " против " + (beforeTwice + 1));

      // ── тип записи правится тем же движением, что и текст ──
      //
      // Агент кладёт запись не того типа не реже, чем не с той формулировкой,
      // и до сих пор второе чинилось правкой, а первое — только удалением
      // с заведением заново. Ручка оба поля принимала с самого начала: дыра
      // была ровно посередине «явного выбора», ради которого день и делался.
      const beforeKind = patchCount();
      rows()[0].querySelectorAll(".mini").find((b) => b.title === "Поправить запись")
        .dispatchEvent(new Evt("click"));
      await settle(20);
      const kindBox = $("#mem-working").querySelector(".mem-edit-kind");
      if (!kindBox) check("правка открывает и список типов, а не одно поле текста", false, "списка нет");
      else {
        check("правка открывает и список типов, а не одно поле текста", true, "");
        // Пустой пункт у правки подписан **иначе**, чем у формы добавления,
        // и это не украшение: «— оставить тип —» — единственное, чем сказано,
        // что невыбранный тип значит «прежний», а не «выберите». Подпись
        // обещана и в README, и в CLAUDE.md — значит стережётся здесь же,
        // а не рядом отдельным утверждением.
        check("и он открыт пустым, и пустой пункт назван «оставить», а не «выберите»",
          kindBox.value === "" && kindBox.children[0].textContent === "— оставить тип —",
          JSON.stringify([kindBox.value, kindBox.children[0].textContent]));

        // Щелчок по списку — не конец правки, хотя фокус с поля и ушёл.
        // Сохраняй мы по `blur` самого поля, список типов исчезал бы
        // из-под курсора ровно в тот момент, когда к нему потянулись.
        $("#mem-working").querySelector(".mem-edit").blur(kindBox);
        await settle(20);
        check("уход фокуса с поля на список правку не заканчивает",
          patchCount() === beforeKind && Boolean($("#mem-working").querySelector(".mem-edit-kind")),
          patchCount() + " | " + Boolean($("#mem-working").querySelector(".mem-edit-kind")));

        // Тип берём **исключительный для этого слоя**: «открытый вопрос»
        // есть в `WORKING_KINDS` и нет в `MEMORY_KINDS`. «Решение» подошло бы
        // обоим — оно нарочно лежит в обоих списках, — и утверждение о том,
        // что правке рабочей записи дали именно её типы, держалось бы
        // на совпадении: подмени слой целиком, и оно бы промолчало.
        kindBox.value = "question";
        kindBox.blur();
        await settle(40);
        const kindPatch = requests("PATCH", /\/working\//).slice(-1)[0];
        check("выбранный тип уезжает правкой — и один, без текста, которого не трогали",
          patchCount() === beforeKind + 1 &&
            JSON.stringify(kindPatch.body) === JSON.stringify({ kind: "question" }),
          patchCount() + " | " + JSON.stringify(kindPatch && kindPatch.body));
        check("и в списке у записи новая подпись типа",
          texts("#mem-working")[0] === "открытый вопрос: собрать ТЗ к июню",
          JSON.stringify(texts("#mem-working")));
      }
    }

    // ── форма: тип выбирает человек, и до выбора не уходит ничего ──
    const adds = () => requests("POST", /\/working$/);
    check("тип в форме рабочей памяти не предвыбран",
      $("#mem-work-kind").value === "", JSON.stringify($("#mem-work-kind").value));
    $("#mem-work-content").value = "срок — конец мая";
    $("#mem-work-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("без выбранного типа запись не уходит вовсе",
      adds().length === 0, JSON.stringify(adds().map((r) => r.body)));
    check("и форма говорит, чего не хватает",
      /Тип записи не выбран/.test($("#mem-work-status").textContent),
      $("#mem-work-status").textContent);

    // Тип выбран, текста нет: пустая запись уехала бы в промпт строкой
    // «ограничение: » и заняла бы место врезки, ничего не сказав. У формы
    // долговременного слоя этот отказ уже стережётся — слои устроены
    // одинаково, и второй отказ обязан стеречься наравне с первым.
    $("#mem-work-kind").value = "goal";
    $("#mem-work-kind").dispatchEvent(new Evt("change"));
    $("#mem-work-content").value = "   ";
    $("#mem-work-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("с пустым текстом запись рабочей памяти тоже не уходит",
      adds().length === 0, JSON.stringify(adds().map((r) => r.body)));
    check("и причина названа: записывать нечего",
      /Текст записи пуст/.test($("#mem-work-status").textContent),
      $("#mem-work-status").textContent);
    $("#mem-work-content").value = "срок — конец мая";

    const patchesBefore = requests("PATCH", /^\/api\/agents\/[^/]+$/).length;
    $("#mem-work-kind").value = "limit";
    $("#mem-work-kind").dispatchEvent(new Evt("change"));
    await settle(40);
    check("выбор типа записи конфиг чата не трогает: PATCH'ей не прибавилось",
      requests("PATCH", /^\/api\/agents\/[^/]+$/).length === patchesBefore,
      JSON.stringify(requests("PATCH", /^\/api\/agents\/[^/]+$/).slice(patchesBefore).map((r) => r.body)));

    $("#mem-work-add").dispatchEvent(new Evt("click"));
    await settle(40);
    check("с выбранным типом запись уходит под чат, телом «тип и текст»",
      adds().length === 1 && adds()[0].path === "/api/agents/" + client.state.current.id + "/working" &&
        JSON.stringify(adds()[0].body) === JSON.stringify({ kind: "limit", content: "срок — конец мая" }),
      JSON.stringify(adds().map((r) => [r.path, r.body])));
    check("и в списке она появилась тут же — ответом ручки, а не перечитыванием",
      texts("#mem-working").slice(-1)[0] === "ограничение: срок — конец мая",
      JSON.stringify(texts("#mem-working")));
    check("поле после успеха пусто — второй клик не заведёт ту же запись молча",
      $("#mem-work-content").value === "", $("#mem-work-content").value);

    // ── удаление: уходит номер именно той записи ──
    //
    // Список правится ответом ручки, а не перечитыванием слоя: за всё время
    // раздела за слоями ходили ровно один раз — на открытие вкладки.
    const layersBefore = layerCalls().length;
    const mine = texts("#mem-working").indexOf("ограничение: срок — конец мая");
    const trash = rows()[mine].querySelectorAll(".mini").find((b) => b.title === "Удалить запись");
    if (!trash) check("у записи рабочего слоя есть кнопка удаления", false, "кнопки нет");
    else {
      const seq = seqOf(mine);
      trash.dispatchEvent(new Evt("click"));
      await settle(40);
      const dropped = requests("DELETE", /\/working\//);
      check("удаление шлёт номер именно этой записи, а не соседней",
        dropped.length === 1 && dropped[0].path.endsWith("/working/" + seq),
        JSON.stringify(dropped.map((r) => r.path)));
      check("из списка ушла она одна, соседние остались",
        !texts("#mem-working").includes("ограничение: срок — конец мая") &&
          texts("#mem-working").length === 2,
        JSON.stringify(texts("#mem-working")));
      check("правка, добавление и удаление за слоями не ходят",
        layerCalls().length === layersBefore,
        JSON.stringify(layerCalls().map((r) => r.path)));
    }

    // ── долговременная запись правится тем же движением ──
    //
    // Слои устроены одинаково: в оба пишет человек, и обоим полагается
    // один набор — правка текста, правка типа, удаление.
    const longRows = $("#mem-long").querySelectorAll(".mem-item");
    const longPencil = longRows[longRows.length - 1]
      .querySelectorAll(".mini").find((b) => b.title === "Поправить запись");
    if (!longPencil) check("у долговременной записи есть кнопка правки", false, "кнопки нет");
    else {
      longPencil.dispatchEvent(new Evt("click"));
      await settle(20);
      const field = $("#mem-long").querySelector(".mem-edit");
      field.value = "оплата только картой";
      field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
      await settle(40);
      const patched = requests("PATCH", /^\/api\/memory\//);
      check("правка долговременной записи шлёт её номер и новый текст",
        patched.length === 1 &&
          JSON.stringify(patched[0].body) === JSON.stringify({ content: "оплата только картой" }),
        JSON.stringify(patched.map((r) => [r.path, r.body])));
      check("и в списке стоит поправленное",
        texts("#mem-long").slice(-1)[0] === "оплата только картой",
        JSON.stringify(texts("#mem-long")));

      // И тип — здесь тоже, и тем же движением: слои устроены одинаково,
      // и правка, работающая в одном из двух, разъехалась бы с соседним
      // на первом же исправлении.
      const kinds = () =>
        $("#mem-long").querySelectorAll(".mem-kind").map((n) => n.textContent);
      const wasKind = kinds().slice(-1)[0];
      $("#mem-long").querySelectorAll(".mem-item").slice(-1)[0]
        .querySelectorAll(".mini").find((b) => b.title === "Поправить запись")
        .dispatchEvent(new Evt("click"));
      await settle(20);
      const longKind = $("#mem-long").querySelector(".mem-edit-kind");
      if (!longKind) check("у долговременной правки тоже есть список типов", false, "списка нет");
      else {
        check("у долговременной правки тоже есть список типов, и тоже пустой",
          longKind.value === "", JSON.stringify(longKind.value));
        // И здесь тип **исключительный для слоя**: «о собеседнике» есть
        // в `MEMORY_KINDS` и нет в `WORKING_KINDS`. Довод тот же, что
        // у соседнего слоя, и он не про симметрию: это единственное место,
        // где видно, что правке дали список **её** слоя.
        longKind.value = "profile";
        longKind.blur();
        await settle(40);
        const typed = requests("PATCH", /^\/api\/memory\//).slice(-1)[0];
        check("тип долговременной записи правится её номером и одним полем",
          JSON.stringify(typed.body) === JSON.stringify({ kind: "profile" }),
          JSON.stringify(typed && [typed.path, typed.body]));
        check("и подпись типа в списке сменилась на выбранную",
          wasKind === "факт" && kinds().slice(-1)[0] === "о собеседнике",
          JSON.stringify([wasKind, kinds()]));
      }
    }
  }

  // ── режим задачи: команды в поле ввода, шапка над лентой ──
  //
  // Команды разбираются на клиенте: сервер про слеши не знает. Главное здесь
  // — что уходит: `/task` и переходы «дальше»/«продолжай» шлют обмен, пауза,
  // шаг, ожидание и выход только двигают состояние, а команда не к месту
  // не шлёт вообще ничего.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "по этапам" }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const taskCalls = () =>
      server.state.requests.filter((r) => /\/task$/.test(r.path));
    const sentCount = () => server.state.sent.length;
    const asked = () => server.state.sent.map((m) => m.text);
    // След запросов: по нему видно порядок ручки и обмена.
    const trail = () => server.state.requests
      .filter((r) => /\/(task|messages)$/.test(r.path))
      .map((r) => r.path.slice(r.path.lastIndexOf("/")));
    const headLines = () =>
      $("#task-head").children.map((n) => n.textContent);
    const hintText = () => $("#composer-hint").textContent;
    const type = async (text) => {
      $("#input").value = text;
      $("#composer").requestSubmit();
      await settle(60);
    };

    check("режим выключен — шапки задачи нет вовсе",
      $("#task-head").classList.contains("hidden") && headLines().length === 0,
      JSON.stringify(headLines()));

    // ── /taskfoo — слово, а не команда ──
    //
    // После имени команды обязателен пробел или конец строки. Иначе любое
    // слово, начавшееся со слеша, уходило бы в ручку вместо модели.
    await type("/taskfoo это просто слово");
    check("«/taskfoo» ушло обычным сообщением, а не командой",
      sentCount() === 1 && taskCalls().length === 0 &&
        server.state.sent[0].text === "/taskfoo это просто слово",
      JSON.stringify([sentCount(), taskCalls().length]));

    // ── команда без обязательного текста ──
    await type("/task");
    check("«/task» без описания не шлёт ничего",
      sentCount() === 1 && taskCalls().length === 0,
      JSON.stringify([sentCount(), taskCalls().map((r) => r.method)]));
    check("и под полем сказано, чего не хватает",
      /описание/.test(hintText()), hintText());
    check("а набранное осталось в поле — его видно и можно дописать",
      $("#input").value === "/task", $("#input").value);

    // ── /task <описание>: включает режим и отправляет обмен ──
    await type("/task собрать ТЗ на приложение");
    check("«/task <описание>» шлёт и ручку, и обмен",
      taskCalls().length === 1 && taskCalls()[0].method === "POST" && sentCount() === 2,
      JSON.stringify([taskCalls().map((r) => [r.method, r.body]), sentCount()]));
    check("обменом уехало само описание, без слеша",
      server.state.sent[1].text === "собрать ТЗ на приложение",
      server.state.sent[1].text);
    check("шапка встала: этап назван словом, а не только цветом",
      headLines()[0] === "Задача · планирование", JSON.stringify(headLines()));
    check("и в ней видно ожидаемое действие — умолчание этапа",
      headLines().slice(-1)[0] === "ожидается: разложить задачу на шаги, затем /task-next",
      JSON.stringify(headLines()));
    check("шага человек не задавал — строки шага в шапке нет",
      headLines().length === 2, JSON.stringify(headLines()));

    // ── /task-step: пишет шаг и молчит ──
    await type("/task-step набрасываю разделы");
    check("«/task-step» шлёт ручку и **не** шлёт обмен",
      taskCalls().length === 2 && taskCalls()[1].method === "PATCH" && sentCount() === 2,
      JSON.stringify([taskCalls().map((r) => r.method), sentCount()]));
    check("и шаг виден в шапке",
      headLines()[1] === "шаг: набрасываю разделы", JSON.stringify(headLines()));

    // ── /task-expect: заданное перебивает умолчание ──
    await type("/task-expect показать черновик списком");
    check("заданное человеком действие перебило умолчание этапа",
      headLines().slice(-1)[0] === "ожидается: показать черновик списком",
      JSON.stringify(headLines()));
    await type("/task-expect");
    check("«/task-expect» без текста не шлёт ничего и говорит, чего ждёт",
      taskCalls().length === 3 && /ожидаем/.test(hintText()),
      JSON.stringify([taskCalls().length, hintText()]));

    // ── /task-next: двигает этап и сразу спрашивает модель ──
    //
    // Молчащий переход человек принимал за «ничего не произошло»: этап
    // сменился, а в ленте пусто. Порядок здесь и есть инвариант — ручка
    // раньше обмена, иначе промпт соберётся с правилом прошлого этапа.
    await type("/task-next");
    check("«/task-next» шлёт и ручку, и обмен",
      taskCalls().length === 4 && sentCount() === 3,
      JSON.stringify([taskCalls().length, sentCount()]));
    check("и ручку раньше обмена: этап сменился до сборки промпта",
      trail().slice(-2).join(" → ") === "/task → /messages",
      trail().join(" → "));
    check("обменом уехал текст перехода, а не сама команда",
      asked()[2] === "Приступай к работе.", asked()[2]);
    check("этап в шапке сменился",
      headLines()[0] === "Задача · выполнение", JSON.stringify(headLines()));
    check("а «ожидается» вернулось к умолчанию нового этапа",
      headLines().slice(-1)[0] === "ожидается: сделать текущий шаг, затем /task-next",
      JSON.stringify(headLines()));

    // Промпт этого обмена — с правилом нового этапа: ради этого ручка
    // и стоит раньше.
    {
      const card = $("#feed").querySelectorAll(".card").slice(-1)[0];
      cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
      await settle(20);
      const first = card.querySelector(".prompt-view")
        .querySelectorAll(".prompt-text").map((n) => n.textContent)[0];
      check("промпт этого обмена несёт правило нового этапа, а не прошлого",
        first.includes("Делай текущий шаг и только его.") &&
          !first.includes("Разложи задачу на шаги"),
        JSON.stringify(first));
    }

    // ── пауза выглядит иначе, чем работа, и модель не зовёт ──
    await type("/task-pause");
    check("пауза различима не только цветом: словом в шапке и своим классом",
      headLines()[0] === "Задача · пауза" &&
        $("#task-head").className.split(" ").includes("paused"),
      JSON.stringify([headLines()[0], $("#task-head").className]));
    check("«/task-pause» обмена не шлёт: остановка модели ничего не поручает",
      sentCount() === 3, String(sentCount()));

    // ── команда не к месту: не ушло ничего, сказано что не так ──
    const beforeBad = taskCalls().length;
    await type("/task-next");
    check("отказ ручки: обмена нет вовсе",
      sentCount() === 3 && trail().slice(-1)[0] === "/task",
      JSON.stringify([sentCount(), trail().slice(-2)]));
    check("и под полем сказано, что не так и что можно",
      /так не ходят/.test(hintText()) && /resume/.test(hintText()), hintText());
    check("состояние при этом цело: шапка та же",
      headLines()[0] === "Задача · пауза", JSON.stringify(headLines()));
    check("а отказанная команда осталась в поле",
      $("#input").value === "/task-next", $("#input").value);
    check("ручку она всё же спросила: таблица переходов живёт на сервере",
      taskCalls().length === beforeBad + 1, String(taskCalls().length));

    // ── снятие паузы возвращает туда же и зовёт продолжать ──
    await type("/task-resume");
    check("«/task-resume» вернул на тот же этап, с которого вставали",
      headLines()[0] === "Задача · выполнение", JSON.stringify(headLines()));
    check("и позвал модель продолжать",
      sentCount() === 4 && asked()[3] === "Продолжай.",
      JSON.stringify([sentCount(), asked()[3]]));

    // ── остальные два перехода: у каждого свой текст ──
    await type("/task-next");
    await type("/task-next");
    check("этапы дошли до последнего",
      headLines()[0] === "Задача · готово", JSON.stringify(headLines()));
    check("четыре перехода дали четыре разных текста",
      asked().length === 6 && new Set(asked().slice(2)).size === 4,
      JSON.stringify(asked().slice(2)));

    // ── /task-off: шапки нет вовсе ──
    await type("/task-off");
    check("«/task-off» стёр состояние: шапки снова нет",
      $("#task-head").classList.contains("hidden") && headLines().length === 0,
      JSON.stringify(headLines()));
    check("и обмена не случилось ни одного лишнего",
      sentCount() === 6, String(sentCount()));

    // ── после выключения слеш снова просто текст ──
    await type("обычное сообщение");
    check("обычное сообщение уходит обменом, как и раньше",
      sentCount() === 7 && server.state.sent[6].text === "обычное сообщение",
      JSON.stringify([sentCount(), server.state.sent[6].text]));
  }

  // ── отказ ручки: обмена нет вовсе ──
  //
  // Утверждение об отсутствии стоит там, где присутствие достижимо: у этого
  // перехода текст обмена есть, и молчит он только потому, что после отказа
  // ручки `runCommand` дальше не идёт. Пауза, которой некуда возвращаться,
  // — такая же строка в базе, как всякая другая.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "пауза без возврата" }],
      tasks: { "пауза без возврата": { stage: "paused" } },
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    $("#input").value = "/task-resume";
    $("#composer").requestSubmit();
    await settle(60);
    check("ручка отказала — обмена нет, хотя текст у этого перехода есть",
      server.state.sent.length === 0 &&
        server.state.requests.filter((r) => /\/messages$/.test(r.path)).length === 0,
      JSON.stringify(server.state.sent));
    check("и под полем сказано, что не так",
      /так не ходят/.test($("#composer-hint").textContent),
      $("#composer-hint").textContent);
    check("состояние цело: этап прежний",
      $("#task-head").children.map((n) => n.textContent)[0] === "Задача · пауза",
      JSON.stringify($("#task-head").children.map((n) => n.textContent)));
  }

  // ── врезка задачи подписана в просмотре промпта ──
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "с задачей" }],
      tasks: { "с задачей": { stage: "execution", step: "пишу проверку" } },
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("шапка поднялась вместе с чатом, а не после первой команды",
      $("#task-head").children.map((n) => n.textContent)[0] === "Задача · выполнение",
      JSON.stringify($("#task-head").children.map((n) => n.textContent)));

    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(80);
    const card = $("#feed").querySelectorAll(".card").slice(-1)[0];
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    await settle(20);
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    const texts = view ? view.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("в промпте подписаны правило этапа и врезка состояния",
      roles.join(" | ") === "системный промпт | состояние задачи | сообщение пользователя",
      roles.join(" | "));
    check("и врезка — та самая, с этапом и шагом",
      texts[1].startsWith("[задача]") && texts[1].includes("шаг: пишу проверку"),
      JSON.stringify(texts[1]));
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
