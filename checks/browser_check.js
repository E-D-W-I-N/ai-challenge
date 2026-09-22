// Проверка клиентского кода настоящими вызовами, а не grep'ом по исходнику.
//
//     node checks/browser_check.js
//
// Разбор markdown — самая опасная поверхность демо: текст от модели кладётся
// в innerHTML, и grep по исходнику этого не заметит. Поэтому здесь настоящие
// payload'ы и настоящий маршрут: правка в панели → отправка → тело запроса.
// app.js подключается модулем: в браузере `module` не существует.

const path = require("path");

// Единственное, что app.js трогает на загрузке, — matchMedia для раскладки.
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };

const app = require(path.join(__dirname, "..", "app", "static", "app.js"));
const { renderMarkdown } = app;

const failures = [];
let passed = 0;

// Клиент асинхронный насквозь, и его исключение всплывает необработанным
// отказом промиса: без обработчика node убивает процесс, и от набора
// не остаётся ни строки.
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
// С provider.require_parameters=true незаявленный параметр выкашивает
// провайдеров. Поэтому панель предупреждает **до** отправки и называет
// ровно те параметры, что уедут.

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
// Инвариант: сообщение уходит с тем конфигом, что показан в панели.
// Проверяется код браузера, а не ручка. Правку вносим **без события
// `change`** — как пользователь, ещё не убравший курсор из поля.

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
  // Управление контекстом — тоже поля панели, а не константы: два чата рядом,
  // у одного окно задано, — это и есть сравнение расхода «до/после».
  // Стратегия оттуда же: иначе переключатель переключал бы только картинку.
  ["f-strategy", "window", "strategy", "window"],
  // Суммаризация — вторая строка про ту же ручку не для симметрии: значения,
  // которого нет в разметке `<select>`, поле не примет, и строка покраснеет.
  // Забудь про `<option>` — выбрать вариант было бы нечем.
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
// уносит все проверки после себя. Кнопку ищем по назначению, а не по номеру
// в ряду: индекс начинает жать не то, оставаясь зелёным.
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
    // Поле, которого нет в разметке, роняет маршрут и уносит все утверждения
    // после себя: ловим здесь и краснеем именно этой строкой.
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
  // Стратегия — выбор из закрытого списка. Чужое значение (чат от сервера
  // другой версии) сервер читает как «не резать», и панель обязана показать
  // то же: с пустым полем первая же правка отправила бы его обратно.
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
  // Поле, принимающее число и молча его игнорирующее, врёт так же, как
  // молчаливая обрезка. При `full` чисел нет, у окна одно, у суммаризации
  // два, и одно поле подписано по-разному. Переключение проверяется **сразу
  // после события**: показ обязан смениться на самом выборе.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "со сводкой", strategy: "summary", keep_last: 6, compress_every: 10 }],
    });
    // До `init()` `syncStrategyFields` не отработал ни разу, и числа прячет
    // сама разметка: иначе при первой отрисовке панель обещала бы два
    // рабочих поля при `full`.
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
  // Историю помнит агент: клиент шлёт вопрос и ничего больше, а
  // перегенерация — вовсе пустое тело.
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
  // Утверждение о том, что оказалось в ленте, а не что вернула функция:
  // экранирование можно снять на месте вызова.
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
  // Под ответом числа этого обмена, справа итог по разговору: после второго
  // обмена плитки обязаны показать **сумму**, а строки в ленте — своё.
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

    // Номер дня в шапке — единственное место, где ветка называет себя вслух,
    // и чужой номер видно первым же кадром записи. Утверждение живёт здесь:
    // свой запуск стенда ради одной строки дороже самой строки.
    check("в шапке стоит номер этого дня",
      $(".brand-sub").textContent === "чат · день 13", $(".brand-sub").textContent);

    // Подсказка поля — единственное место, откуда человек узнаёт про /task.
    check("в подсказке поля ввода названа команда /task",
      $("#input").placeholder.includes("/task"), $("#input").placeholder);

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
  // Провайдер кладёт токены рассуждения **внутрь** completion_tokens, и без
  // оговорки число выглядит враньём. Вычитать нельзя — «выход» перестанет
  // быть тем, что прислал провайдер.
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
  // Седьмой плитки у сжатия нет намеренно: плитки про весь диалог, а
  // свернулось — в этом обмене, рядом с уменьшенным им числом.
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
  // Обрезка бывает только выбранная и всегда названная. Окно теряет начало
  // **совсем**, и слово у него другое, чем у сводки: та заменила.
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
  // Память едет **поверх** любого варианта и ничего не заменяет: реплики
  // уехали все до одной, и приписывать нечего.
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
  // Пометка появляется только там, где сводка уехала: «сводка вместо
  // 0 сообщений» под каждым ответом была бы шумом.
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
  // Сумму считает агент, клиент только показывает. Сервер называет заведомо
  // другие числа, чем лежат в репликах: показать надо серверные.
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
  // У метрик ошибки все числа пустые, и класть такой набор поверх прежнего
  // значило бы гасить плитки там, где числа нужнее всего. Набор сливается
  // по полям, а доля окна помечается прошлой.
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
  // Окно у новой модели другое. Гасит плитку сама смена модели в панели,
  // а не расхождение имён: на `openrouter/auto` провайдер возвращает не то
  // имя. Токенов и цены это не касается — они за весь разговор.
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
  // Сжатие — отдельный вызов к модели ДО ответа: без строки состояния пауза
  // выглядит зависанием. Смотрим и середину потока: строка обязана быть видна
  // до ответа и уйти с ним. Процентов в ней нет — у одного вызова доли
  // выполнения нет. Сворачивается **второй** обмен: у него в промпте есть
  // хвост истории с ответом модели.
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
    // правка уехавшего запроса, собранного из слепка конфига.
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
    // Врезка кнопку не заводит: её заводит увиденный промпт. У обмена без
    // сводки есть что показать, а подпись врезки при пустом `summary_at`
    // вылезать не смеет.
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
    // Обмен, не доехавший до истории, её не удлиняет, а кадр `start` уехал:
    // привяжи его по длине — он лёг бы под ключ **прошлого** ответа.
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
  // `system` по умолчанию пуст, и сводка в промпте такого чата стоит нулевым
  // сообщением: ноль — не «сводки нет», и кнопка обязана быть на месте.
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
  // Подписать врезку клиент обязан по слоту от сервера, а не по тексту, и
  // подписать **фактами**, а не сводкой. Приезжает она без всякой стратегии:
  // обрезка у чата «вся история», а записи о задаче встают в промпт. Строки
  // состояния у неё нет: её вписал человек.
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
  // Три врезки стоят в промпте втроём и ровно в этом порядке; все три места
  // называет сервер разными полями кадра `start`. Считай клиент слоты сам —
  // подписал бы памятью факты, едва врезок стало больше одной.
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
  // Обратная половина: `memory_at` пуст и когда слой пуст, и когда хранилища
  // нет. Подставь клиент ноль — подпись встала бы над системным промптом.
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
  // `memory_at` равен нулю, и ноль — не «памяти нет»: нестрогое сравнение
  // спрятало бы врезку, которая в модель уехала.
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
  // Событие о сворачивании приходит не на каждом обмене, и мигающая без
  // повода строка была бы шумом. Кнопку заводит увиденный запрос.
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
  // Окно — единственная стратегия, теряющая реплики совсем, а лента
  // показывает всю историю: кнопка обязана быть и здесь, хотя слот пуст.
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
  // Ветвление живёт поверх любой стратегии, и кнопка стоит у каждого ответа.
  // Весь путь: с какой точкой уходит запрос, что открылось и видна ли пометка.
  // Имя родителя клиент находит сам, по id.
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
        // пережила. Пометка обязана сказать это, а не смолчать — иначе ветка
        // выглядела бы обычным чатом с чужим началом разговора.
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

    // И обратно к родителю: у него своя история и своя панель. Это и есть
    // «переключайтесь между ветками» — отдельного переключателя не нужно.
    open(2);
    await settle(40);
    check("вернулись к родителю — разговор у него целиком",
      $("#feed").textContent.includes("второй вопрос"), $("#feed").textContent.slice(0, 200));
    check("и пометка ветки в панели снова спрятана",
      $("#branch-note").classList.contains("hidden"), $("#branch-note").textContent);
  }

  // ── вкладка «Память»: три слоя видны и управляются ──
  // Весь круг: вкладка открылась, в каждом разделе своё, факт продвинут
  // нажатием, запись заведена формой и удалена по номеру — и всё это на один
  // поход за слоями. Чатов в посеве три, по ветви формулы «уезжает дословно»:
  // неисполненная ветвь вольна врать словом.
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

    // Лениво, и в обе стороны: обмен слои меняет, но при закрытой вкладке
    // за ними никто не идёт. Разница видна только так — обменом до открытия.
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

    // Краткосрочная: длину истории называет сервер, а сколько уедет дословно
    // считается по той же формуле, что режет промпт. Восемь сообщений, окно
    // в два — отброшены шесть: окно режет ровно столько, сколько просили.
    check("в краткосрочной — длина истории и сколько из неё уезжает дословно",
      /Сообщений в истории/.test(shown("#mem-short")) && /Уезжает дословно/.test(shown("#mem-short")) &&
        nums("#mem-short") === "8|2",
      shown("#mem-short"));
    check("и сказано, куда делось начало: отброшено окном",
      /Остальные 6 — отброшено окном\./.test(shown("#mem-short")), shown("#mem-short"));
    // Сводка — рядом с историей, а не в рабочей памяти: она не запомненное,
    // а чем заменено не уехавшее. Выключи сворачивание — не пропадёт ничего.
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
    // Умолчания у типа нет ни на сервере, ни в форме: предвыбери она первый
    // настоящий тип — «явно выбирал» стало бы «форма выбрала за него».
    check("до выбора тип не подставлен: список стоит на пустом пункте",
      $("#mem-kind").value === "", JSON.stringify($("#mem-kind").value));

    // ── «Запомнить надолго»: запись переезжает в долговременный слой,
    //    и тип у неё спрашивают ──
    // Единственное место, где запись меняет слой, и где тип выбирал код.
    // Теперь кнопка кладёт строку в ту же форму и ждёт выбора.
    const promote = $("#mem-working").querySelectorAll(".mem-btn")
      .find((b) => b.title === "Запомнить надолго");
    if (!promote) check("у записи есть кнопка «Запомнить надолго»", false, "кнопки нет");
    else {
      promote.dispatchEvent(new Evt("click"));
      await settle(40);
      check("«Запомнить надолго» сама ничего не записывает: тип ещё не выбран",
        posts().length === 0, JSON.stringify(posts().map((r) => r.body)));
      // Фокус переводится на список типов тем же нажатием: без этого человек
      // смотрел бы на запись, которая молча никуда не уехала.
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
    // Пустая запись уехала бы в промпт строкой «решение: », ничего не сказав.
    // Выбор типа — не правка конфига: поле памяти уезжало бы PATCH'ем ни о чём.
    // Слепок снимается **до** выбора: считается прирост.
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
    // Ключ, вставленный в текст, сервер вырезает по дороге в базу (`redact`).
    // Показывай клиент своё тело — на экране остался бы ключ, которого нет.
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
    // Обратная сторона ленивости: запрос висит на открытии **этой** вкладки.
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
  // Ручка трёх слоёв отказывает как любая другая: занятая база — 503. Тогда
  // у вкладки нет записей, и форма не предлагает записать в никуда. Причина
  // названа: «пусто» и «не доехало» — разные новости.
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
  // Человек **явно выбирает, что и куда** сохраняется. Весь круг: правка
  // и удаление уходят с номером **этой** записи, тип правится наравне
  // с текстом, форма без типа не шлёт ничего, списки правятся ответами ручек.
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
      // Тип у посева — «факт», чтобы правка меняла его на настоящий другой:
      // смена на тот же самый ничего бы не отправила.
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
      // Уход фокуса сохраняет, Escape отменяет, пустое поле и прежний текст
      // не шлют ничего, замок не даёт правке уехать дважды. Считаем PATCH'и.
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
      // Запись не того типа встречается не реже, чем не с той формулировкой,
      // а чинилась только удалением с заведением заново.
      const beforeKind = patchCount();
      rows()[0].querySelectorAll(".mini").find((b) => b.title === "Поправить запись")
        .dispatchEvent(new Evt("click"));
      await settle(20);
      const kindBox = $("#mem-working").querySelector(".mem-edit-kind");
      if (!kindBox) check("правка открывает и список типов, а не одно поле текста", false, "списка нет");
      else {
        check("правка открывает и список типов, а не одно поле текста", true, "");
        // Пустой пункт у правки подписан **иначе**, чем у формы добавления:
        // «— оставить тип —» единственное, чем сказано, что невыбранный тип
        // значит «прежний». Подпись обещана в README и CLAUDE.md.
        check("и он открыт пустым, и пустой пункт назван «оставить», а не «выберите»",
          kindBox.value === "" && kindBox.children[0].textContent === "— оставить тип —",
          JSON.stringify([kindBox.value, kindBox.children[0].textContent]));

        // Щелчок по списку — не конец правки, хотя фокус с поля ушёл:
        // по `blur` поля список исчезал бы прямо из-под курсора.
        $("#mem-working").querySelector(".mem-edit").blur(kindBox);
        await settle(20);
        check("уход фокуса с поля на список правку не заканчивает",
          patchCount() === beforeKind && Boolean($("#mem-working").querySelector(".mem-edit-kind")),
          patchCount() + " | " + Boolean($("#mem-working").querySelector(".mem-edit-kind")));

        // Тип берём **исключительный для слоя**: «открытый вопрос» есть
        // в `WORKING_KINDS` и нет в `MEMORY_KINDS`. «Решение» лежит в обоих
        // нарочно, и на нём утверждение держалось бы на совпадении.
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
    // «ограничение: », ничего не сказав. Слои устроены одинаково, и второй
    // отказ обязан стеречься наравне с первым.
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
    // Список правится ответом ручки: за слоями ходили ровно один раз.
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
    // Слои устроены одинаково: в оба пишет человек, и набор один — правка
    // текста, правка типа, удаление.
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

      // И тип тем же движением: правка, работающая в одном слое из двух,
      // разъехалась бы с соседним на первом же исправлении.
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
        // в `MEMORY_KINDS` и нет в `WORKING_KINDS` — единственное место,
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

  // ══════════════════ состояние задачи на экране ══════════════════
  //
  // Сервер умеет всё: план, этапы, инструменты, цикл оборотов. Работающий
  // код, которого не видно на экране, считается несделанным — проверки ниже
  // про то, что видно и что двигается.

  // Шапка задачи глазами читателя: строка этапа, значки шагов, кнопки.
  // Ищем по назначению, а не по номеру: индекс начнёт называть не то.
  const NO_TASK = "(шапки задачи нет)";
  const taskStage = ($) => {
    const node = $("#task").querySelector(".task-stage");
    return node ? node.textContent : NO_TASK;
  };
  const taskMarks = ($) => $("#task").querySelectorAll(".task-mark").map((m) => m.textContent);
  const taskTitles = ($) =>
    $("#task").querySelectorAll(".task-step-title").map((m) => m.textContent);
  const taskButtons = ($) => $("#task").querySelectorAll(".task-btn").map((b) => b.textContent);
  const taskButton = ($, label) => {
    const btn = $("#task").querySelectorAll(".task-btn").find((b) => b.textContent === label);
    if (btn) return btn;
    check(`в шапке задачи есть кнопка «${label}»`, false, taskButtons($).join(" | ") || NO_TASK);
    return { dispatchEvent() {} };
  };
  const taskHidden = ($) => $("#task").classList.contains("hidden");

  // Планы шести этапов. Этап нигде не хранится — он ответ правил по списку
  // и флажкам, — поэтому здесь списки и флажки, а не ярлыки: иначе проверка
  // спрашивала бы у стенда то, что сама и подсказала.
  const STEP = (title, status) => ({ title, status });
  const PLANS = {
    planning: { steps: [], approved: false, finished: false, paused: false },
    approval: { steps: [STEP("собрать требования", "pending")],
                approved: false, finished: false, paused: false },
    execution: { steps: [STEP("собрать требования", "done"), STEP("накидать структуру", "in_progress"),
                         STEP("сверить с заказчиком", "pending")],
                 approved: true, finished: false, paused: false },
    // У проверки и завершения шагов **больше одного** и все `done`: текущего
    // шага нет, и приписка «шаг k из n» была бы выдумкой. На плане из одного
    // шага правда и враньё неотличимы.
    validation: { steps: [STEP("собрать требования", "done"), STEP("накидать структуру", "done")],
                  approved: true, finished: false, paused: false },
    done: { steps: [STEP("собрать требования", "done"), STEP("накидать структуру", "done")],
            approved: true, finished: true, paused: false },
    paused: { steps: [STEP("собрать требования", "done"), STEP("накидать структуру", "pending")],
              approved: true, finished: false, paused: true },
  };
  // Копия, а не ссылка: планы отсюда попадают прямо в состояние стенда,
  // и правящая их проверка иначе правила бы заготовку — следующий стенд
  // поднялся бы не с того этапа.
  const withPlan = (label, stage) =>
    ({ label, workflow: "plan", plan: JSON.parse(JSON.stringify(PLANS[stage])) });

  // Тексты-константы кнопок: по одной на **переход**, отдельными значениями —
  // чтобы «у продолжения своя» стояло на сравнении двух строк.
  const SAY_APPROVE = "План утверждён, выполняй первый шаг";
  const SAY_RESUME = "Пауза снята, продолжай с текущего шага";

  // Ушедшее на сервер, отобранное по смыслу: пролив панели шлёт свой PATCH
  // перед каждой отправкой, и «PATCH ушёл» без отбора было бы правдой всегда.
  const patchedWorkflow = (server, value) =>
    server.state.requests.findIndex(
      (r) => r.method === "PATCH" && r.body && r.body.workflow === value);
  const postedMessage = (server) =>
    server.state.requests.findIndex(
      (r) => r.method === "POST" && r.path.endsWith("/messages"));
  const postedPlan = (server, action) =>
    server.state.requests.findIndex(
      (r) => r.method === "POST" && r.path.endsWith("/plan/" + action));

  // ── обычный чат остаётся обычным, а чат с задачей обзаводится шапкой ──
  // Утверждение об отсутствии стоит там, где присутствие достижимо: оба чата
  // в одном стенде, и «шапки нет» значит «её нет», а не «её нет ни у кого».
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [withPlan("с задачей", "execution")],
    });
    client.init();
    await settle(30);
    check("у обычного чата шапки задачи нет вовсе",
      taskHidden($) && !$("#task").children.length,
      taskStage($) + " / детей: " + $("#task").children.length);

    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("у чата с задачей шапка есть", !taskHidden($), NO_TASK);
    check("этап назван по-русски, и текущий шаг назван номером",
      taskStage($) === "Задача · работа, шаг 2 из 3", taskStage($));
    check("шаги показаны все и в своём порядке",
      JSON.stringify(taskTitles($)) ===
        JSON.stringify(["собрать требования", "накидать структуру", "сверить с заказчиком"]),
      JSON.stringify(taskTitles($)));
    // Три статуса — три **разных** значка: цвет пропадает и в чёрно-белом,
    // и у дальтоника, и на записи экрана, а шаги обязаны читаться сразу.
    const marks = taskMarks($);
    check("у трёх статусов три разных значка",
      marks.length === 3 && new Set(marks).size === 3, JSON.stringify(marks));

    // Возврат в обычный чат гасит шапку: она про открытый чат, а не про ленту.
    $("#agent-list").querySelectorAll(".item-open")[0].dispatchEvent(new Evt("click"));
    await settle(40);
    check("вернулись в обычный чат — шапки снова нет",
      taskHidden($) && !$("#task").children.length, taskStage($));
  }

  // ── /task <описание>: сперва процесс, потом обмен ──
  // Порядок здесь и есть смысл команды: уедь сообщение первым, промпт
  // собрался бы без правила этапа, блока задачи и инструментов.
  {
    const { client, server, $, settle } = freshClient();
    client.init();
    await settle(30);
    $("#input").value = "/task собрать ТЗ на мобильное приложение";
    $("#composer").requestSubmit();
    await settle(150);

    const patchAt = patchedWorkflow(server, "plan");
    const postAt = postedMessage(server);
    check("/task: ушёл PATCH с workflow: plan", patchAt >= 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("/task: ушёл и обмен", postAt >= 0, "обмена нет");
    check("/task: PATCH раньше обмена", patchAt >= 0 && postAt > patchAt,
      "PATCH на " + patchAt + ", обмен на " + postAt);
    // Самая прямая форма того же: конфиг в момент запроса — тот, с которым
    // сообщение уехало бы в модель.
    check("/task: обмен уехал уже с включённым процессом",
      server.state.sent[0] && server.state.sent[0].config.workflow === "plan",
      JSON.stringify(server.state.sent[0] && server.state.sent[0].config.workflow));
    check("/task: в обмен уехало описание без самой команды",
      server.state.sent[0] && server.state.sent[0].text === "собрать ТЗ на мобильное приложение",
      JSON.stringify(server.state.sent[0] && server.state.sent[0].text));
    check("/task: и шапка появилась", !taskHidden($), NO_TASK);
  }

  // ── /task без описания и /task при идущей задаче: не уходит ничего ──
  // Обе ветки про одно: молчаливого действия быть не должно. Пустая команда
  // включила бы процесс ни о чём, вторая затёрла бы чужой список шагов.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("с задачей", "execution")],
    });
    client.init();
    await settle(30);
    $("#input").value = "/task   ";
    $("#composer").requestSubmit();
    await settle(80);
    check("/task без описания: не ушло ничего",
      server.state.sent.length === 0 && patchedWorkflow(server, "plan") < 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("/task без описания: сказано, чего не хватает",
      $("#composer-hint").textContent.includes("/task") &&
        $("#composer-hint").classList.contains("error"),
      $("#composer-hint").textContent);

    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "/task другая задача";
    $("#composer").requestSubmit();
    await settle(80);
    check("/task при идущей задаче: не ушло ничего",
      server.state.sent.length === 0,
      JSON.stringify(server.state.sent.map((x) => x.text)));
    check("/task при идущей задаче: сказано, что задача идёт и как из неё выйти",
      $("#composer-hint").textContent.includes("уже идёт") &&
        $("#composer-hint").textContent.includes("Выйти из режима задачи"),
      $("#composer-hint").textContent);
    check("и план на экране остался прежним",
      JSON.stringify(taskTitles($)).includes("накидать структуру"),
      JSON.stringify(taskTitles($)));
  }

  // ── кнопки: на каждом этапе видны ровно уместные ──
  // Кнопка, которой ручка ответит 409, — обещание, которого интерфейс
  // не держит. Утверждение про **весь набор** разом: лишняя рядом с нужной
  // осталась бы незамеченной.
  {
    const EXIT = "Выйти из режима задачи";
    const EXPECTED = {
      planning: ["Пауза", EXIT],
      approval: ["Утвердить план", "Пауза", EXIT],
      execution: ["Пауза", EXIT],
      validation: ["Пауза", EXIT],
      done: ["Переоткрыть", EXIT],
      paused: ["Продолжить", EXIT],
    };
    // Заголовок сверяется **целиком**, одним утверждением на все шесть:
    // «назван словами» проверяло лишь то, что он нарисовался. Английское имя
    // этапа не поймало бы ничто, как и «шаг 1 из 2» без текущего шага.
    const HEADS = {
      planning: "Задача · план",
      approval: "Задача · утверждение, шаг 1 из 1",
      execution: "Задача · работа, шаг 2 из 3",
      validation: "Задача · проверка",
      done: "Задача · готово",
      paused: "Задача · пауза, шаг 2 из 2",
    };
    const stages = Object.keys(EXPECTED);
    const { client, $, settle, Evt } = freshClient({
      chats: stages.map((stage) => withPlan("этап " + stage, stage)),
    });
    client.init();
    await settle(30);
    const heads = [];
    for (let i = 0; i < stages.length; i += 1) {
      $("#agent-list").querySelectorAll(".item-open")[i + 2].dispatchEvent(new Evt("click"));
      await settle(40);
      heads.push(taskStage($));
      check(`этап ${stages[i]}: кнопки ровно уместные`,
        JSON.stringify(taskButtons($)) === JSON.stringify(EXPECTED[stages[i]]),
        JSON.stringify(taskButtons($)));
    }
    check("заголовок шапки назван целиком на всех шести этапах",
      JSON.stringify(heads) === JSON.stringify(stages.map((x) => HEADS[x])),
      JSON.stringify(heads));
  }

  // ── «Утвердить план»: ручка, затем обмен своей константой ──
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("на утверждении", "approval")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    // Человек набирал вопрос и не дописал. Кнопка шапки шлёт обмен мимо
    // поля: стирать чужой черновик ей нечем и незачем.
    const DRAFT = "недописанный вопрос";
    $("#input").value = DRAFT;
    taskButton($, "Утвердить план").dispatchEvent(new Evt("click"));
    await settle(200);

    check("кнопка шапки не тронула недописанное в поле ввода",
      $("#input").value === DRAFT, JSON.stringify($("#input").value));

    const approveAt = postedPlan(server, "approve");
    const postAt = postedMessage(server);
    check("«Утвердить план»: ушёл POST на ручку утверждения", approveAt >= 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("«Утвердить план»: обмен ушёл следом, а не раньше",
      approveAt >= 0 && postAt > approveAt, "ручка на " + approveAt + ", обмен на " + postAt);
    check("«Утвердить план»: обмен уехал текстом-константой этого перехода",
      server.state.sent[0] && server.state.sent[0].text === SAY_APPROVE,
      JSON.stringify(server.state.sent[0] && server.state.sent[0].text));
    check("и этап на экране сменился на работу",
      taskStage($).startsWith("Задача · работа"), taskStage($));
  }

  // ── «Продолжить»: своя константа, не такая, как у утверждения ──
  // Текст привязан к **переходу**, а не к целевому этапу: обе кнопки ведут
  // в `execution`, но просить надо разного.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("на паузе", "paused")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("на паузе этап так и назван", taskStage($).startsWith("Задача · пауза"), taskStage($));
    taskButton($, "Продолжить").dispatchEvent(new Evt("click"));
    await settle(200);

    const resumeAt = postedPlan(server, "resume");
    const postAt = postedMessage(server);
    check("«Продолжить»: ушёл POST на снятие паузы", resumeAt >= 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("«Продолжить»: обмен ушёл следом", resumeAt >= 0 && postAt > resumeAt,
      "ручка на " + resumeAt + ", обмен на " + postAt);
    check("«Продолжить»: константа своя, а не такая же, как у утверждения",
      server.state.sent[0] && server.state.sent[0].text === SAY_RESUME &&
        SAY_RESUME !== SAY_APPROVE,
      JSON.stringify(server.state.sent[0] && server.state.sent[0].text));
  }

  // ── «Переоткрыть»: только ручка, обмена нет ──
  // Обратная половина к двум предыдущим и рядом нарочно: «обмена нет» имеет
  // смысл только там, где «обмен есть» достижимо.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("завершённая", "done")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("у завершённой задачи этап так и назван",
      taskStage($).startsWith("Задача · готово"), taskStage($));
    taskButton($, "Переоткрыть").dispatchEvent(new Evt("click"));
    await settle(200);
    check("«Переоткрыть»: ушёл POST на переоткрытие", postedPlan(server, "reopen") >= 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("«Переоткрыть»: обмена не было вовсе — что не так, напишет человек",
      server.state.sent.length === 0,
      JSON.stringify(server.state.sent.map((x) => x.text)));
    check("а этап вернулся в проверку",
      taskStage($).startsWith("Задача · проверка"), taskStage($));
  }

  // ── «Выйти из режима задачи»: сброс, затем выключенный процесс ──
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("с задачей", "execution")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    taskButton($, "Выйти из режима задачи").dispatchEvent(new Evt("click"));
    await settle(200);

    const resetAt = postedPlan(server, "reset");
    const offAt = patchedWorkflow(server, "off");
    check("«Выйти»: ушёл POST на сброс", resetAt >= 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("«Выйти»: и процесс погашен PATCH'ем следом",
      offAt >= 0 && offAt > resetAt, "сброс на " + resetAt + ", PATCH на " + offAt);
    check("«Выйти»: шапка исчезла", taskHidden($) && !$("#task").children.length,
      taskStage($));
    check("«Выйти»: и происшедшее названо словами, а не одной пропавшей шапкой",
      $("#composer-hint").textContent.includes("сброшена"),
      $("#composer-hint").textContent);
    check("«Выйти»: обмена при этом не было",
      server.state.sent.length === 0, JSON.stringify(server.state.sent.map((x) => x.text)));
  }

  // ── кадр tool двигает галочку ДО конца обмена ──
  // Человек видит шаги в тот же момент, что и модель. Проверяется в окне
  // между кадрами `tool` и `done`: обнови клиент шапку на `done`, и в этом
  // окне список был бы ещё прежним.
  {
    const BEFORE = [STEP("собрать требования", "pending"), STEP("накидать структуру", "pending")];
    const AFTER = [STEP("собрать требования", "done"), STEP("накидать структуру", "in_progress")];
    const { client, server, $, settle, Evt } = freshClient({
      delay: 60,
      chats: [{ label: "с задачей", workflow: "plan",
                plan: { steps: BEFORE, approved: true, finished: false, paused: false } }],
      tools: [{ name: "update_plan", ok: true, message: "План записан.",
                plan: { steps: AFTER, approved: true, finished: false, paused: false } }],
    });
    client.init();
    await settle(40);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(60);
    const started = JSON.stringify(taskMarks($));
    $("#input").value = "делай";
    $("#composer").requestSubmit();
    // Кадры идут по одному с паузой: `start`, `tool`, `delta`, `done`.
    // Окно ловим после второго и до последнего.
    await settle(170);
    check("кадр tool: обмен в этот момент ещё идёт", client.state.busy === true,
      "обмен уже кончился — окно поймано не то");
    check("кадр tool: список шагов уже новый",
      JSON.stringify(taskMarks($)) !== started &&
        JSON.stringify(taskMarks($)) === JSON.stringify(["✓", "▶"]),
      "было " + started + ", стало " + JSON.stringify(taskMarks($)));
    check("кадр tool: и этап на экране пересчитан",
      taskStage($) === "Задача · работа, шаг 2 из 2", taskStage($));
    // Дословно: это единственное, что видит человек, пока модель правит план.
    // Проверка на «узел есть» стерегла бы разметку, а не обещание.
    check("кадр tool: карточка говорит дословно, чем занята пауза",
      usageText($("#feed"), ".card-status-text") === "Модель работает по плану…",
      usageText($("#feed"), ".card-status-text"));
    // Пока идёт ответ, переходы отказаны и на сервере. Кнопка обязана гаснуть,
    // а не молчать: молчаливый отказ читается как поломка.
    check("пока идёт ответ, кнопки шапки погашены",
      $("#task").querySelectorAll(".task-btn").every((b) => b.disabled === true),
      JSON.stringify($("#task").querySelectorAll(".task-btn").map((b) => b.disabled)));
    // План сдвинулся — клиент шлёт продолжение сам, и ждать надо его тоже.
    // Второй обмен несёт тот же вызов, план не двигается, и цепочка встаёт.
    await settle(800);
    check("обмен дошёл до конца, и шапка осталась новой",
      client.state.busy === false && JSON.stringify(taskMarks($)) === JSON.stringify(["✓", "▶"]),
      JSON.stringify(taskMarks($)));
    check("а строка состояния погасла, когда пошёл текст",
      !$("#feed").querySelector(".card-status"),
      usageText($("#feed"), ".card-status"));
    check("и кнопки шапки снова живые",
      $("#task").querySelectorAll(".task-btn").every((b) => b.disabled === false) &&
        taskButtons($).length > 0,
      JSON.stringify($("#task").querySelectorAll(".task-btn").map((b) => b.disabled)));
  }

  // ══════════════ цепочка: один сделанный шаг — один обмен ══════════════
  //
  // Сервер кончает обмен на первом отмеченном шаге, и дальше ход клиента.
  // Отсюда главное — у каждого шага своя карточка в ленте и своя кнопка
  // промпта.
  //
  // Тексты продолжений сюда **не списаны**: списанная строка утверждает
  // формулировку, а стеречь надо свойства — три перехода дают три разные
  // строки, ни одна не весть о чужой отметке и ни одна не пересказ правила.
  // Сцены складывают сюда то, что клиент **правда отправил**, по переходу;
  // утверждение о трёх стоит ниже, когда собраны все три.
  const SAID = {};
  // Длиннее этого продолжение — уже пересказ правила этапа. Правило
  // пересобирается перед каждым оборотом и потому свежее: приказ в двух
  // местах стареет молча.
  const SAY_MAX = 60;
  // Потолок цепочки, тот же, что в `MAX_CHAIN` у клиента. Число здесь
  // списано, а не выведено: клиент своих констант наружу не отдаёт, и
  // утверждение обязано стоять на числе обменов, которые он правда отправил.
  const CEILING = 12;

  // Планы, которыми вызов инструмента двигает этап. Шагов в каждом два:
  // на плане из одного шага «все сделаны» и «один в работе» неотличимы.
  const WORKING = {
    steps: [STEP("собрать требования", "done"), STEP("накидать структуру", "in_progress")],
    approved: true, finished: false, paused: false,
  };
  const CHECKED = {
    steps: [STEP("собрать требования", "done"), STEP("накидать структуру", "done")],
    approved: true, finished: false, paused: false,
  };
  // Докуда стенд качает этап в сцене потолка — вдвое выше самого потолка:
  // снятый потолок обязан дать красное утверждение, а не бесконечный обмен.
  const SWINGS = CEILING * 2;
  const copyPlan = (plan) => JSON.parse(JSON.stringify(plan));
  const moveTo = (plan) =>
    [{ name: "update_plan", ok: true, message: "План записан.", plan: copyPlan(plan) }];

  // ── шаг за шагом: обмен на каждую отметку, и у каждого свой промпт ──
  // Ровно то, чего не хватало на живом прогоне: работа шла одной карточкой
  // на все пять шагов, и ни один промпт, кроме первого, человек не видел.
  {
    const THREE = (a, b, c) => ({
      steps: [STEP("собрать требования", a), STEP("накидать структуру", b),
              STEP("свести вместе", c)],
      approved: true, finished: false, paused: false,
    });
    // Три отметки подряд: этап у первых двух **не меняется**, и режь клиент
    // по смене этапа — продолжения не было бы вовсе.
    const MARCH = [
      THREE("done", "in_progress", "pending"),
      THREE("done", "done", "in_progress"),
      THREE("done", "done", "done"),
    ];
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "по шагам", workflow: "plan",
                plan: THREE("in_progress", "pending", "pending") }],
      // Четвёртый обмен вызовов не несёт: план не двигается, и цепочка
      // встаёт сама — её главный ограничитель, а не потолок.
      tools: (i) => (i < MARCH.length ? moveTo(MARCH[i]) : []),
      // Числа у каждого обмена свои: обменов стало вчетверо больше, и цена
      // этого обязана быть на экране, а не в счёте от провайдера.
      usage: { prompt_tokens: 1000, completion_tokens: 100, total_tokens: 1100,
               cost_usd: 0.001 },
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "делай";
    $("#composer").requestSubmit();
    await settle(900);

    const said = server.state.sent.map((x) => x.text);
    check("отметка шага внутри работы продолжает цепочку: обмен на каждый шаг",
      said.length === 4, JSON.stringify(said));
    // Текст держится **перехода**: два шага внутри работы получили одно
    // и то же продолжение, уход в проверку — другое. Утверждение стоит
    // на отправленном, а не на списанной константе.
    check("текст продолжения — на переход: два шага одним, «проверяй» другим",
      said[1] === said[2] && said[3] !== said[1], JSON.stringify(said.slice(1)));
    SAID.step = said[1];
    SAID.toValidation = said[3];
    // И каждое — **указание**, а не весть о чужой отметке и не пересказ
    // правила: что делать и каким вызовом, сказано правилом этапа, а оно
    // на обороте свежее. Здесь уехали два перехода из трёх, третий —
    // в сцене потолка.
    check("продолжения коротки: указание, а не пересказ правила",
      said.slice(1).every((t) => t.length <= SAY_MAX),
      JSON.stringify(said.slice(1).map((t) => t.length)));
    check("и ни одно не сообщает, что шаг уже отмечен",
      !said.slice(1).some((t) => t.includes("отмеч")),
      JSON.stringify(said.slice(1)));
    check("план не сдвинулся — цепочка встала сама, без потолка",
      client.state.busy === false && said.length === 4 &&
        !$("#composer-hint").textContent.includes("дальше не иду"),
      $("#composer-hint").textContent);

    const cards = $("#feed").querySelectorAll(".card");
    const titles = (c) => c.querySelectorAll(".icon-btn").map((b) => b.title);
    check("у каждого шага своя карточка в ленте",
      cards.length === 4, cards.length + " карточек");
    // Цена цепочки видна: под каждым обменом свои числа, а в плитках — сумма
    // по чату. Восемь реплик на три шага — это решение человека, и оно
    // обязано быть посчитанным у него на глазах.
    check("под каждым обменом цепочки свои числа",
      cards.every((c) => usageText(c, ".usage-tokens").includes("$0.001")),
      JSON.stringify(cards.map((c) => usageText(c, ".usage-tokens"))));
    check("а в плитках — сумма по чату за все четыре обмена",
      tileOf($, "Стоимость").v === "$0.004000" && tileOf($, "Сообщений").v === "8",
      tileOf($, "Стоимость").v + " за " + tileOf($, "Сообщений").v + " сообщений");
    check("и у каждой своя кнопка «Показать промпт запроса»",
      cards.length === 4 &&
        cards.every((c) => titles(c).includes("Показать промпт запроса")),
      JSON.stringify(cards.map((c) => titles(c).join(","))));

    // И промпты у карточек **разные**: блок задачи в каждом показывает тот
    // список, с которым уехал её обмен. Одна карточка на все шаги давала бы
    // один промпт — беда, ради которой всё и затеяно.
    // Пропавшая карточка — красное утверждение, а не исключение: упавший
    // маршрут унёс бы все проверки после себя.
    const taskBlock = (card) => {
      const shown = card && card.querySelector(".prompt-view");
      const texts = shown
        ? shown.querySelectorAll(".prompt-text").map((t) => t.textContent) : [];
      return texts.find((t) => t.startsWith("[задача]")) || "";
    };
    cards.forEach((c) =>
      cardButton(c, "Показать промпт запроса").dispatchEvent(new Evt("click")));
    await settle(20);
    const roles = (card) =>
      (card ? card.querySelectorAll(".prompt-role") : []).map((r) => r.textContent);
    check("в промпте карточки врезка задачи подписана по слоту",
      roles(cards[3]).includes("план задачи"), JSON.stringify(roles(cards[3])));
    const blocks = cards.map(taskBlock);
    check("у каждой карточки в промпте свой список шагов: четыре разных",
      blocks.every(Boolean) && new Set(blocks).size === 4 &&
        blocks[0].includes("собрать требования — in_progress") &&
        blocks[2].includes("свести вместе — in_progress") &&
        blocks[3].includes("свести вместе — done"),
      JSON.stringify(blocks));
  }

  // ── потолок держит цепочку, и выход из него назван словами ──
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "работа идёт", workflow: "plan", plan: copyPlan(WORKING) }],
      // Этап качается: работа → проверка → работа. Ограничитель «план
      // не сдвинулся» на кругах не срабатывает **никогда**, и упереться
      // цепочка обязана в потолок. Качается до `SWINGS`: со снятым потолком
      // мутация дала бы повисший node вместо красного утверждения.
      tools: (i) => (i < SWINGS ? moveTo(i % 2 === 0 ? CHECKED : WORKING) : []),
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "доделывай";
    $("#composer").requestSubmit();
    await settle(2000);

    const said = server.state.sent.map((x) => x.text);
    check("потолок: один обмен человека и " + CEILING + " своих, дальше стоп",
      client.state.busy === false && said.length === CEILING + 1,
      said.length + " обменов, идёт: " + client.state.busy);
    // Оба перехода ведут в **разные** стороны одного круга, и тексты у них
    // разные; уход в проверку — тот же, что в прошлой сцене: ключ и правда
    // переход, а не номер обмена.
    check("текст продолжения — на переход, а не на целевой этап",
      said[1] !== said[2] && said[1] === SAID.toValidation,
      JSON.stringify(said.slice(0, 3)));
    SAID.toExecution = said[2];
    // Третий переход, `validation>execution`: тоже короткое указание
    // и без вести о чужой отметке.
    check("и «исправляй» — такое же короткое указание",
      said[2].length <= SAY_MAX && !said[2].includes("отмеч"), said[2]);
    check("и выход из потолка назван словами, а не молчанием",
      $("#composer-hint").textContent.includes("дальше не иду"),
      $("#composer-hint").textContent);
    check("у каждого обмена цепочки своя карточка в ленте",
      $("#feed").querySelectorAll(".card").length === CEILING + 1,
      $("#feed").querySelectorAll(".card").length + " карточек");
  }

  // ── три перехода — три разные строки ──
  // Собраны из того, что клиент отправил в двух сценах выше. Одна константа
  // на все переходы — и «проверяй» с «исправляй» просили бы одного и того же.
  check("три перехода дали три разные строки",
    new Set([SAID.step, SAID.toValidation, SAID.toExecution]).size === 3,
    JSON.stringify(SAID));

  // ── этапы, которые ждут человека, цепочку не продолжают ──
  // Утверждение об отсутствии стоит там, где присутствие достижимо:
  // четвёртый чат уходит в `validation`, и продолжение у него есть.
  {
    const NEEDS_APPROVAL = {
      steps: [STEP("собрать требования", "pending")],
      approved: false, finished: false, paused: false,
    };
    const PAUSED = { ...copyPlan(WORKING), paused: true };
    const FINISHED = { ...copyPlan(CHECKED), finished: true };
    const TARGETS = [NEEDS_APPROVAL, PAUSED, FINISHED, CHECKED];
    const { client, server, $, settle, Evt } = freshClient({
      chats: [
        { label: "в утверждение", workflow: "plan", plan: copyPlan(WORKING) },
        { label: "на паузу", workflow: "plan", plan: copyPlan(WORKING) },
        { label: "в готово", workflow: "plan", plan: copyPlan(CHECKED) },
        { label: "в проверку", workflow: "plan", plan: copyPlan(WORKING) },
      ],
      // Продолжение (пятый обмен) вызовов уже не несёт: план на нём
      // не двигается, и цепочка встаёт сама — её ограничитель, а не потолок.
      tools: (i) => (i < TARGETS.length ? moveTo(TARGETS[i]) : []),
    });
    client.init();
    await settle(30);
    for (let i = 0; i < TARGETS.length; i += 1) {
      $("#agent-list").querySelectorAll(".item-open")[2 + i].dispatchEvent(new Evt("click"));
      await settle(40);
      $("#input").value = "двигай " + i;
      $("#composer").requestSubmit();
      await settle(300);
    }

    const said = server.state.sent.map((x) => x.text);
    check("на approval, paused и done цепочка не продолжается вовсе",
      JSON.stringify(said.slice(0, 3)) ===
        JSON.stringify(["двигай 0", "двигай 1", "двигай 2"]),
      JSON.stringify(said));
    check("а на validation — продолжается, и тем же стендом",
      said.length === 5 && said[3] === "двигай 3" && said[4] === SAID.toValidation,
      JSON.stringify(said));
  }

  // ── «Стоп» посреди цепочки её обрывает ──
  // Цепочку рвут «Стоп», пауза, переключение чата и закрытая вкладка.
  // Первое проверяется в окне: обмен цепочки начался, следующего ещё нет.
  {
    const { client, server, $, settle, Evt } = freshClient({
      delay: 40,
      chats: [{ label: "качается", workflow: "plan", plan: copyPlan(WORKING) }],
      tools: (i) => moveTo(i % 2 === 0 ? CHECKED : WORKING),
    });
    client.init();
    await settle(40);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(60);
    $("#input").value = "доделывай";
    $("#composer").requestSubmit();
    await settle(330);
    check("«Стоп» жмут посреди второго обмена цепочки",
      server.state.sent.length === 2 && client.state.busy === true,
      server.state.sent.length + " обменов, идёт: " + client.state.busy);
    $("#composer").requestSubmit();
    await settle(600);
    check("«Стоп» посреди цепочки её обрывает: третьего обмена нет",
      server.state.sent.length === 2, JSON.stringify(server.state.sent.map((x) => x.text)));
    check("и отмена названа словами",
      $("#composer-hint").textContent.includes("Остановлено"),
      $("#composer-hint").textContent);
  }

  // ── отмена, пришедшая кадром `done`, цепочку останавливает ──
  // Отмену ставит не только наша кнопка: соседняя вкладка зовёт `/cancel`,
  // и поток дочитывается до конца кадром `done` с `cancelled`. Своего
  // признака у клиента тут нет, и не прочитай он слово сервера — цепочка
  // отправила бы следующий обмен **сама**. Сцена собрана так, чтобы падать
  // было на чём: этап сдвинут, и продолжение достижимо.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "отменят снаружи", workflow: "plan", plan: copyPlan(WORKING) }],
      tools: (i) => moveTo(CHECKED),
      cancelled: true,
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "доделывай";
    $("#composer").requestSubmit();
    await settle(400);

    check("отменённый снаружи обмен этап всё-таки сдвинул — сцена та",
      taskStage($).startsWith("Задача · проверка"), taskStage($));
    check("отмена приехала кадром done, а не обрывом: поток дочитан до конца",
      client.state.busy === false && $("#feed").querySelectorAll(".card").length === 1,
      "карточек: " + $("#feed").querySelectorAll(".card").length);
    check("цепочка на отмене встала: продолжения нет",
      server.state.sent.length === 1,
      JSON.stringify(server.state.sent.map((x) => x.text)));
    check("и человеку сказано, что остановлено, а не молча",
      $("#composer-hint").textContent.includes("Остановлено"),
      $("#composer-hint").textContent);
  }

  // ── упавший обмен цепочку не продолжает ──
  // Вторая половина того же условия, что и «Стоп»: вызов исполнился и этап
  // сдвинул, а ответа не случилось. Продолжить значило бы просить модель
  // проверить работу, которой она не делала.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [{ label: "упадёт", workflow: "plan", plan: copyPlan(WORKING) }],
      tools: (i) => moveTo(CHECKED),
      fail: { message: "провайдер ответил 502" },
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "доделывай";
    $("#composer").requestSubmit();
    await settle(400);

    check("упавший обмен этап всё-таки сдвинул — сцена та",
      taskStage($).startsWith("Задача · проверка"), taskStage($));
    check("но цепочка на нём встала: продолжения нет",
      server.state.sent.length === 1,
      JSON.stringify(server.state.sent.map((x) => x.text)));
    check("и человеку названа причина, а не продолжение",
      $("#composer-hint").textContent.includes("502"), $("#composer-hint").textContent);
  }

  // ── отказ ручки: человеку человеческое ──
  // Ручка отказывает **директивой, написанной для модели**, и менять её
  // нельзя; но совать её человеку тоже нельзя, и клиент говорит своё
  // по коду. Случай настоящий: вторая вкладка утвердила план раньше.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("на утверждении", "approval")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    server.state.agents[2].plan.approved = true;
    taskButton($, "Утвердить план").dispatchEvent(new Evt("click"));
    await settle(200);

    const shown = $("#composer-hint").textContent;
    // То, что ручка отдаёт на самом деле. Без этой строки утверждение
    // «директивы на экране нет» стояло бы на догадке о стенде: заодно видно,
    // что отказ вообще случился.
    const refused = await fetch("/api/agents/" + server.state.agents[2].id + "/plan/approve",
      { method: "POST" });
    const body = await refused.json();
    check("ручка и правда отказывает директивой, написанной для модели",
      refused.status === 409 && body.detail.includes("НЕ утверждай"),
      refused.status + " " + body.detail);
    check("а человек видит человеческий текст — и свежий этап в нём назван",
      shown.includes("Утвердить план") && shown.includes("этапе «работа»") &&
        $("#composer-hint").classList.contains("error"), shown);
    check("и директивы для модели на экране нет",
      !shown.includes("НЕ утверждай") && !shown.includes("НЕ выполнен"), shown);
    check("шапка перечитана: кнопки, которая только что отказала, больше нет",
      !taskButtons($).includes("Утвердить план"), JSON.stringify(taskButtons($)));
  }

  // ── тот же 409, но этап не менялся: причину не выдумываем ──
  // Поводов у отказа два, а код один: не тот этап или чат занят ответом.
  // Назови причину по **действию**, и на экран уехала бы фраза про этап,
  // которого никто не менял.
  {
    const { client, server, $, settle, Evt } = freshClient({
      chats: [withPlan("на утверждении", "approval")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    // Вторая вкладка занята ответом. Этап при этом тот же, что был.
    server.state.agents[2].busy = true;
    const stageBefore = taskStage($);
    taskButton($, "Утвердить план").dispatchEvent(new Evt("click"));
    await settle(200);

    const shown = $("#composer-hint").textContent;
    // Что ручка отдаёт на самом деле. Без этой строки «этап не менялся»
    // стояло бы на догадке: проверялось бы отсутствие фразы там, где, может,
    // и отказа-то не было.
    const refused = await fetch("/api/agents/" + server.state.agents[2].id + "/plan/approve",
      { method: "POST" });
    check("занятый чат отказывает тем же кодом 409, что и не тот этап",
      refused.status === 409, String(refused.status));
    check("этап при этом не менялся", taskStage($) === stageBefore,
      stageBefore + " → " + taskStage($));
    check("на экране сказано, что не вышло, и названа кнопка",
      shown.includes("Утвердить план") && shown.includes("не вышло"), shown);
    check("и фразы про этап в нём нет — причину не выдумываем",
      !shown.includes("этап"), shown);
    check("кнопка при этом осталась: отказали не из-за этапа",
      taskButtons($).includes("Утвердить план"), JSON.stringify(taskButtons($)));
  }

  // ── просмотр промпта: блок задачи подписан своей ролью ──
  // Врезок в одном промпте теперь четыре, и клиент подписывает их по номерам
  // от сервера, а не по словам внутри сообщений. Считай он слоты сам —
  // подписал бы памятью блок задачи ровно тогда, когда врезок больше одной.
  {
    const MEMORY = "[долговременная память]\nо собеседнике: пишу на Kotlin\n[конец долговременной памяти]";
    const FACTS = "[факты о разговоре]\nцель: собрать ТЗ\n[конец фактов о разговоре]";
    const SUM = "[пересказ начала разговора, свёрнуто сообщений: 2]\nбыло то-то";
    const { client, $, settle, Evt } = freshClient({
      memory: MEMORY,
      facts: FACTS,
      service: { insert: SUM, covered: 0, strategy: "summary" },
      // Системный промпт задан нарочно: врезки нумеруются от длины собранного
      // начала промпта, и без него все четыре сдвинулись бы на единицу —
      // проверялся бы сдвиг, а не порядок.
      chats: [{ ...withPlan("с задачей", "execution"), system: "СИСТЕМНЫЙ ПРОМПТ" }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    $("#input").value = "вопрос при включённом процессе";
    $("#composer").requestSubmit();
    await settle(200);

    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    const texts = view ? view.querySelectorAll(".prompt-text").map((r) => r.textContent) : [];
    check("врезок в промпте четыре, и блок задачи подписан своей ролью в своём месте",
      JSON.stringify(roles) === JSON.stringify([
        "системный промпт", "долговременная память", "факты о разговоре",
        "план задачи", "сводка начала разговора", "сообщение пользователя",
      ]), JSON.stringify(roles));
    check("и подпись стоит на том сообщении, а не на соседнем",
      texts[3] && texts[3].startsWith("[задача]") && texts[2] === FACTS && texts[4] === SUM,
      JSON.stringify(texts));
  }

  // ── у обычного чата блока задачи в промпте нет вовсе ──
  // Обратная половина: `plan_at` приходит пустым, и подпись не вылезает
  // ни над одним сообщением. Сравнение строгое именно поэтому.
  {
    const { client, $, settle, Evt } = freshClient();
    client.init();
    await settle(30);
    $("#input").value = "обычный вопрос";
    $("#composer").requestSubmit();
    await settle(200);
    const card = $("#feed").querySelector(".card");
    cardButton(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const view = card.querySelector(".prompt-view");
    const roles = view ? view.querySelectorAll(".prompt-role").map((r) => r.textContent) : [];
    check("без рабочего процесса подпись плана не вылезает ни над одним сообщением",
      JSON.stringify(roles) === JSON.stringify(["системный промпт", "сообщение пользователя"]),
      JSON.stringify(roles));
  }

  // ── «оборотов N» — только когда оборотов правда больше одного ──
  // Обмен из трёх вызовов стоит втрое, и числа рядом — сумма по всем трём.
  // Без их числа он выглядел бы одним непомерно дорогим вызовом. А при одном
  // обороте называть нечего: это обычный обмен.
  {
    const { client, $, settle } = freshClient({
      usage: (i) => (i === 0
        ? { turns: 3, prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 }
        : { turns: 1, prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 }),
    });
    client.init();
    await settle(30);
    $("#input").value = "первый — с вызовами";
    $("#composer").requestSubmit();
    await settle(200);
    $("#input").value = "второй — обычный";
    $("#composer").requestSubmit();
    await settle(200);

    const cards = $("#feed").querySelectorAll(".card");
    check("обмен из нескольких оборотов называет их число",
      usageText(cards[0], ".usage-how").includes("оборотов 3"),
      usageText(cards[0], ".usage-how"));
    check("а обычный обмен об оборотах молчит",
      !usageText(cards[1], ".usage-how").includes("оборотов"),
      usageText(cards[1], ".usage-how"));
  }

  // ── /task без пробела командой не является ──
  // Сторож в `taskCommand` был, а прикрыт не был: сними его — и сообщение
  // «/taskbar не открывается» перестаёт быть сообщением. Уходит PATCH
  // `workflow: "plan"`, а описанием задачи становится «bar не открывается».
  {
    const { client, server, $, settle } = freshClient();
    client.init();
    await settle(30);
    $("#input").value = "/taskbar не открывается";
    $("#composer").requestSubmit();
    await settle(150);
    check("/taskbar командой не является: процесс не включался",
      patchedWorkflow(server, "plan") < 0,
      JSON.stringify(server.state.requests.map((r) => r.method + " " + r.path)));
    check("и уехал весь ввод целиком, вместе со слешем",
      server.state.sent[0] && server.state.sent[0].text === "/taskbar не открывается",
      JSON.stringify(server.state.sent[0] && server.state.sent[0].text));
  }

  // ── чат переключили посреди отправки: не уезжает ничего ──
  // Между PATCH команды и обменом лежит ответ ручки, а `state.busy` в это
  // время ложь. Уедь обмен как ни в чём не бывало — пузырь и поток встали бы
  // в чужую ленту, а запись ушла бы в покинутый чат; к концу обмена следов
  // не останется, и человек наберёт сообщение снова, уже не туда.
  {
    const { client, server, $, settle, Evt } = freshClient({ lag: 60, delay: 40 });
    client.init();
    await settle(500);
    const left = server.state.agents[0].id;
    $("#input").value = "/task собрать ТЗ";
    $("#composer").requestSubmit();
    // Уходим в соседний чат, не дожидаясь ответа ручки.
    $("#agent-list").querySelectorAll(".item-open")[1].dispatchEvent(new Evt("click"));

    // Смотрим в чужую ленту **посреди** обмена, а не после него: к концу
    // всё перерисуется по серверу, и следов не останется ни в том мире,
    // ни в этом — утверждение после было бы зелёным всегда.
    await settle(220);
    check("чат и правда сменился, пока летел запрос",
      client.state.current && client.state.current.id === server.state.agents[1].id,
      client.state.current && client.state.current.id);
    check("и в чужой ленте не появилось ни пузыря, ни карточки",
      $("#feed").querySelectorAll(".msg-user").length === 0 &&
        $("#feed").querySelectorAll(".card").length === 0,
      $("#feed").querySelectorAll(".msg-user").length + " пузырей, " +
        $("#feed").querySelectorAll(".card").length + " карточек");

    await settle(600);
    check("обмен не ушёл вовсе", server.state.sent.length === 0,
      JSON.stringify(server.state.sent.map((x) => x.id + ":" + x.text)));
    check("и сказано, почему сообщение не ушло",
      $("#composer-hint").textContent.includes("Открыт другой чат"),
      $("#composer-hint").textContent);
    // Обратная половина: PATCH-то доехал, и режим остался включённым там,
    // где команду набрали, — в том чате появилась шапка. Это честный остаток,
    // а не потеря: человек видит его глазами и вправе повторить.
    check("режим задачи остался включённым в том чате, где команду набрали",
      server.state.agents.find((a) => a.id === left).workflow === "plan",
      server.state.agents.find((a) => a.id === left).workflow);
  }

  // ── отказ кнопки не называет этап чужого чата ──
  // `refreshCurrent` перечитывает **текущий** чат, каким бы он ни был: уйди
  // человек в соседний обычный, пока летел POST, он получил бы красную строку
  // про этап чата, у которого задачи нет вовсе.
  {
    const { client, server, $, settle, Evt } = freshClient({
      // Отказ приходит заведомо позже открытия соседнего чата: иначе окно
      // ловилось бы гонкой двух одинаковых задержек, а не устройством.
      lag: (method, path) => (path.endsWith("/plan/approve") ? 160 : 20),
      chats: [withPlan("на утверждении", "approval")],
    });
    client.init();
    await settle(300);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(150);
    // Вторая вкладка успела утвердить план раньше: наш POST получит 409.
    server.state.agents[2].plan.approved = true;
    taskButton($, "Утвердить план").dispatchEvent(new Evt("click"));
    // И, не дожидаясь отказа, уходим в обычный чат.
    $("#agent-list").querySelectorAll(".item-open")[0].dispatchEvent(new Evt("click"));
    await settle(500);

    check("успели уйти в соседний чат до отказа",
      client.state.current && client.state.current.id === server.state.agents[0].id,
      client.state.current && client.state.current.id);
    check("у соседа шапки нет — задачи у него нет вовсе", taskHidden($), taskStage($));
    check("и про этап ему не сказано ни слова",
      !$("#composer-hint").textContent.includes("этап"),
      $("#composer-hint").textContent);
  }

  // ── два щелчка по кнопке дают один переход ──
  // Без замка второй щелчок успевает в окно, пока летит первый POST: первый
  // утверждает план и начинает обмен, второй получает 409, и его ветка отказа
  // перечитывает чат посреди потока.
  {
    const { client, server, $, settle, Evt } = freshClient({
      lag: 60,
      chats: [withPlan("на утверждении", "approval")],
    });
    client.init();
    await settle(400);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(200);
    const btn = taskButton($, "Утвердить план");
    btn.dispatchEvent(new Evt("click"));
    btn.dispatchEvent(new Evt("click"));
    await settle(700);

    const approves = server.state.requests.filter((r) => r.path.endsWith("/plan/approve"));
    check("два щелчка дали один запрос на утверждение", approves.length === 1,
      "запросов: " + approves.length);
    check("и один обмен, а не два", server.state.sent.length === 1,
      JSON.stringify(server.state.sent.map((x) => x.text)));
  }

  // ── отказ посреди обмена ленту не рвёт ──
  // Из нашего интерфейса сюда не попасть (замок не даст), а от второй
  // вкладки запросто. Перечитывание в этот момент уносит с экрана вопрос
  // и набежавший текст, а куски капают в отцепленный узел.
  {
    const { client, server, $, settle, Evt } = freshClient({
      delay: 60,
      lag: (method, path) => (path.endsWith("/plan/pause") ? 120 : 10),
      chats: [withPlan("в работе", "execution")],
    });
    client.init();
    await settle(200);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(100);
    // Вторая вкладка уже приостановила задачу сама: наш «Пауза» получит 409.
    server.state.agents[2].plan.paused = true;
    taskButton($, "Пауза").dispatchEvent(new Evt("click"));
    // И сразу, не дожидаясь ответа, — обычное сообщение.
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(170);

    check("отказ прилетел, пока обмен ещё идёт", client.state.busy === true,
      "обмен уже кончился — окно поймано не то");
    check("лента не перерисована: живая карточка на месте",
      $("#feed").querySelectorAll(".busy").length === 1,
      "живых карточек: " + $("#feed").querySelectorAll(".busy").length);
    // Сказано при этом всё равно, и названа кнопка. Какими именно словами —
    // стерегут две сцены отказа рядом; здесь проверяется, что молчания нет.
    check("но сказано всё равно, и названа кнопка",
      $("#composer-hint").textContent.includes("Пауза") &&
        $("#composer-hint").classList.contains("error"),
      $("#composer-hint").textContent);
  }

  // ── «Стоп» в фазе без текста: сказано, что остановлено ──
  // Пока идут обороты инструментов, текста нет по устройству, и строка
  // состояния прямо обещает паузу. «Стоп» жмут именно тогда — и получали
  // пустую ленту и ни слова, при том что галочки в шапке уже сдвинулись.
  {
    const BEFORE = [STEP("собрать требования", "pending"), STEP("накидать структуру", "pending")];
    const AFTER = [STEP("собрать требования", "done"), STEP("накидать структуру", "in_progress")];
    const { client, server, $, settle, Evt } = freshClient({
      delay: 60,
      chats: [{ label: "с задачей", workflow: "plan",
                plan: { steps: BEFORE, approved: true, finished: false, paused: false } }],
      tools: [{ name: "update_plan", ok: true, message: "План записан.",
                plan: { steps: AFTER, approved: true, finished: false, paused: false } }],
    });
    client.init();
    await settle(40);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(60);
    $("#input").value = "делай";
    $("#composer").requestSubmit();
    await settle(170);
    check("«Стоп» жмут в фазе, где текста нет по устройству",
      client.state.busy === true && !$("#feed").querySelector(".card-body").textContent,
      "окно поймано не то");
    // Кнопка отправки стала «Стоп»: та же форма, тот же submit.
    $("#composer").requestSubmit();
    await settle(500);

    const said = $("#composer-hint").textContent;
    check("отмена названа словами, а не пустой лентой", said.includes("Остановлено"), said);
    check("сказано, что ответ не записан", said.includes("не записан"), said);
    check("и что отметки плана остались", said.includes("Отметки плана"), said);
    check("вопрос вернулся в поле ввода", $("#input").value === "делай",
      JSON.stringify($("#input").value));
    check("в ленте обмена нет: агент его не запомнил",
      $("#feed").querySelectorAll(".msg-user").length === 0 &&
        $("#feed").querySelectorAll(".card").length === 0,
      $("#feed").querySelectorAll(".msg-user").length + " пузырей, " +
        $("#feed").querySelectorAll(".card").length + " карточек");
    // А вот галочки остались, и это не недочёт: исполненный вызов задним
    // числом не откатывается — так решено на сервере, и отмена этого
    // не меняет.
    check("галочки, поставленные до отмены, остались",
      JSON.stringify(taskMarks($)) === JSON.stringify(["✓", "▶"]),
      JSON.stringify(taskMarks($)));
  }

  // ── «Стоп» на обычном чате про отметки плана молчит ──
  // Обратная половина: обещать «отметки остались» там, где ни одного вызова
  // не было, значит врать в другую сторону.
  {
    const { client, $, settle } = freshClient({ delay: 60 });
    client.init();
    await settle(40);
    $("#input").value = "обычный вопрос";
    $("#composer").requestSubmit();
    await settle(40);
    $("#composer").requestSubmit();
    await settle(400);
    const said = $("#composer-hint").textContent;
    check("отмена обычного обмена тоже названа словами", said.includes("Остановлено"), said);
    check("а про отметки плана в ней ни слова", !said.includes("Отметки плана"), said);
  }

  // ── пауза видна самим видом списка ──
  // Остановленный шаг рисовался ровно как работающий, и зритель записи
  // поверил бы глазам, а не подписи в двенадцать пикселей. Оформление висит
  // на классе этапа — его и проверяем: цвета стенду не видны.
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [withPlan("в работе", "execution"), withPlan("на паузе", "paused")],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    const working = taskMarks($)[1];
    check("в работе шапка помечена своим этапом, а текущий шаг — значком работы",
      $("#task").classList.contains("stage-execution") && working === "▶",
      $("#task").className + " / " + working);

    $("#agent-list").querySelectorAll(".item-open")[3].dispatchEvent(new Evt("click"));
    await settle(40);
    const paused = taskMarks($)[1];
    check("на паузе шапка помечена паузой",
      $("#task").classList.contains("stage-paused") &&
        !$("#task").classList.contains("stage-execution"),
      $("#task").className);
    check("и значок остановленного шага не такой, как у работающего",
      paused !== working && paused === "‖", JSON.stringify([working, paused]));
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
