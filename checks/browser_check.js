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
  // Порядок важен: свои чаты добавляем ПОСЛЕ распаковки options, иначе
  // options.chats затрёт список, а не дополнит его. `bare` — чистый старт:
  // на сервере нет ни одного чата, как при первом запуске.
  const seeded = (options && options.bare)
    ? []
    : [
        { label: "первый чат", system: "СТАРЫЙ ПРОМПТ", model: "первая/модель" },
        { label: "второй чат", system: "ЧУЖОЙ ПРОМПТ", model: "вторая/модель" },
        ...((options && options.chats) || []),
      ];
  const env = dom.boot(HTML, { ...options, chats: seeded });
  const client = require(path.join(__dirname, "..", "app", "static", "app.js"));
  return { ...env, ...dom, client, $: (sel) => env.document.querySelector(sel) };
}

// Кнопку в карточке ищем по назначению, а не по номеру в ряду: индекс
// начинает жать не то, оставаясь зелёным.
function cardButton(card, title) {
  const btn = card && card.querySelectorAll(".icon-btn").find((b) => b.title === title);
  if (btn) return btn;
  check(`в ленте есть карточка с кнопкой «${title}»`, false, String(card && card.className));
  return { dispatchEvent() {} };
}

async function routeChecks() {
  // ── каждое поле панели доезжает до запроса, даже без события change ──
  for (const [field, typed, key, expected] of PANEL_ROUTE) {
    const { client, server, $, settle } = freshClient();
    client.init();
    await settle(20);

    $("#" + field).value = typed;          // правка есть на экране...
    $("#input").value = "вопрос";          // ...а `change` не выстрелил
    $("#composer").requestSubmit();
    await settle(80);

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

    cardButton($("#feed").querySelector(".card"), "Перегенерировать")
      .dispatchEvent(new Evt("click"));
    await settle(120);
    const repeated = server.state.requests.filter((r) => r.path.endsWith("/regenerate"));
    check(
      "перегенерация не шлёт тела вовсе: вопрос помнит агент",
      repeated.length === 1 && repeated[0].body === null,
      JSON.stringify(repeated.map((r) => r.body))
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

  // ── лента рисуется из стенограммы ──
  //
  // Клиент после каждого обмена перечитывает агента и перерисовывает ленту
  // целиком: на экране должно быть ровно то, что у агента в истории, а не то,
  // что он дорисовал по дороге. Значит формат стенограммы — часть поведения.
  {
    const talk = [
      { role: "user", content: "мой вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "**жирный** ответ", error: null, reasoning: "я подумал",
        metrics: { model: "особая/модель", provider: "поставщик" },
      },
      { role: "user", content: "второй вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "огрыз", error: "оборвалось", reasoning: "",
        metrics: { model: "особая/модель", provider: "поставщик" },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "с историей", transcript: talk, history_len: talk.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const nodes = $("#feed").children.filter(
      (el) => el.classList.contains("msg-user") || el.classList.contains("card")
    );
    check("в ленте по узлу на реплику стенограммы", nodes.length === talk.length,
      `узлов ${nodes.length}, реплик ${talk.length}`);
    check("вопросы — пузырями, ответы — карточками",
      nodes.map((el) => (el.classList.contains("msg-user") ? "u" : "a")).join("") === "uaua",
      nodes.map((el) => el.className).join(" | "));
    check("вопрос показан текстом, а не разметкой",
      nodes[0].textContent === "мой вопрос" && nodes[0].innerHTML === "",
      JSON.stringify([nodes[0].textContent, nodes[0].innerHTML]));
    check("текст ответа разобран как markdown",
      nodes[1].querySelector(".card-body").innerHTML.includes("<strong>жирный</strong>"),
      nodes[1].querySelector(".card-body").innerHTML);
    check("имя модели берётся из метрик реплики, а не из конфига чата",
      nodes[1].querySelector(".card-model").textContent === "особая/модель",
      nodes[1].querySelector(".card-model").textContent);
    check("рассуждение показано свёрнутым блоком",
      nodes[1].querySelector(".think-body") &&
        nodes[1].querySelector(".think-body").textContent === "я подумал",
      String(nodes[1].querySelector(".think")));
    check("оборванный ответ помечен, и текст ошибки показан",
      nodes[3].classList.contains("failed") &&
        nodes[3].querySelector(".card-error").textContent === "оборвалось",
      nodes[3].className + " | " + String(nodes[3].querySelector(".card-error")));

    // Пустая стенограмма — это заставка, а не пустая лента с нулём узлов.
    $("#agent-list").querySelectorAll(".item-open")[0].dispatchEvent(new Evt("click"));
    await settle(40);
    check("пустой чат показывает заставку, а не пустоту",
      Boolean($("#feed").querySelector(".empty")), $("#feed").textContent);
  }

  // ── пустой старт: писать надо куда-то сразу ──
  {
    const { client, server, $, settle, Evt } = freshClient({ bare: true });
    client.init();
    await settle(60);

    check("на пустом сервере клиент заводит один чат сам", server.state.agents.length === 1,
      String(server.state.agents.length));
    check("и сразу его открывает", Boolean(client.state.current), "чат не открыт");
    check("панель у него пустая: промпт не придуман за пользователя",
      $("#f-system").value === "" && client.state.current.transcript.length === 0,
      JSON.stringify([$("#f-system").value, client.state.current.transcript]));

    // Удалили единственный чат — появился свежий, а не пустой экран.
    const before = client.state.current.id;
    const row = $("#agent-list").querySelectorAll(".item")[0];
    row.querySelectorAll(".mini")[1].dispatchEvent(new Evt("click"));
    $(".confirm").querySelectorAll(".primary")[0].dispatchEvent(new Evt("click"));
    await settle(80);
    check("после удаления последнего чата появляется свежий, с новым номером",
      server.state.agents.length === 1 && client.state.current.id !== before &&
        client.state.current.label === "Новый чат 2",
      JSON.stringify({ живых: server.state.agents.length, было: before,
                       стало: client.state.current && client.state.current.label }));
  }

  // ── «Новый чат» встаёт в конец списка ──
  {
    const { client, server, $, settle, Evt } = freshClient();
    client.init();
    await settle(30);
    const names = () =>
      $("#agent-list").querySelectorAll(".item-title").map((el) => el.textContent);

    check("сначала в списке два заведённых чата",
      names().join(" | ") === "первый чат | второй чат", names().join(" | "));

    $("#new-chat").dispatchEvent(new Evt("click"));
    await settle(60);
    check("новый чат встаёт последним, а не первым",
      names().length === 3 && /^Новый чат/.test(names()[2]) &&
        names()[0] === "первый чат" && names()[1] === "второй чат",
      names().join(" | "));
    check("и он же открыт",
      client.state.current && client.state.current.label === names()[2],
      client.state.current && client.state.current.label);
  }

  // ── переименование чата в списке слева ──
  //
  // Настоящий маршрут: клик по кнопке, клавиша, запрос к серверу, имя
  // в списке. Греп по исходнику («в app.js есть function startRename»)
  // описывал бы реализацию: переименование можно сломать, не тронув ни одной
  // из тех строк, и греп останется зелёным.
  {
    const { client, server, $, settle, Evt } = freshClient();
    client.init();
    await settle(30);

    const row = () => $("#agent-list").querySelectorAll(".item")[0];
    const title = () => row().querySelector(".item-title");
    const patches = () =>
      server.state.requests.filter((r) => r.method === "PATCH" && "label" in (r.body || {}));
    const rename = () => {
      row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
      return row().querySelector(".item-rename");
    };

    // 1. Кнопка открывает поле прямо в строке, со старым именем внутри.
    let field = rename();
    check("кнопка открывает поле ввода со старым именем внутри",
      field && field.value === "первый чат", field && field.value);

    // 2. Enter сохраняет: правка уходит на сервер и видна в списке.
    field.value = "новое имя";
    field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    await settle(40);
    check("Enter отправляет новое имя на сервер и список его показывает",
      patches().length === 1 && server.state.agents[0].label === "новое имя" &&
        title().textContent === "новое имя",
      JSON.stringify([patches().map((r) => r.body), title() && title().textContent]));

    // 3. Escape отменяет: ни запроса, ни следа в списке.
    field = rename();
    field.value = "передумал";
    field.dispatchEvent(new Evt("keydown", { key: "Escape" }));
    await settle(40);
    check("Escape не шлёт запроса и оставляет прежнее имя",
      patches().length === 1 && title().textContent === "новое имя",
      JSON.stringify([patches().map((r) => r.body), title() && title().textContent]));

    // 4. Потеря фокуса сохраняет: имя не должно теряться молча.
    field = rename();
    field.focus();
    field.value = "по потере фокуса";
    field.blur();
    await settle(40);
    check("потеря фокуса тоже сохраняет",
      patches().length === 2 && title().textContent === "по потере фокуса",
      JSON.stringify([patches().map((r) => r.body), title() && title().textContent]));

    // 5. Пустое имя чат не стирает: запроса нет, имя прежнее.
    field = rename();
    field.value = "   ";
    field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    await settle(40);
    check("пустым именем чат не переименовать",
      patches().length === 2 && title().textContent === "по потере фокуса",
      JSON.stringify([patches().map((r) => r.body), title() && title().textContent]));
  }

  // ── «Применено» — о событии, а не о каждой отправке ──
  //
  // Жалоба заказчика: строка появлялась на каждое сообщение. Проливать панель
  // надо перед каждой отправкой — сообщать не о чем. Утверждения ниже — про
  // то, что видно на экране.
  {
    const { client, $, settle, Evt } = freshClient();
    const shown = () => $("#save-status").textContent;
    client.init();
    await settle(20);

    $("#input").value = "первый";
    $("#composer").requestSubmit();
    await settle(120);
    check("отправка без правок ничего не сообщает", shown() === "", shown());

    // Пролив теми же значениями изменением не является: панель «изменилась»
    // по событию, а конфиг агента — нет.
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("правка теми же значениями тоже молчит", shown() === "", shown());

    $("#f-system").value = "ПРАВКА";
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("после настоящей правки сообщение есть", /Применено/.test(shown()), shown());

    // Пустое поле не равно нулю: заданный ноль — это правка.
    $("#save-status").textContent = "";
    $("#f-top_p").value = "0";
    $("#f-top_p").dispatchEvent(new Evt("change"));
    await settle(40);
    check("ноль в пустом поле — настоящая правка", /Применено/.test(shown()), shown());

    // Провалившийся PATCH — своя красная строка, а не «Применено».
    $("#f-model").value = "";
    $("#f-model").dispatchEvent(new Evt("change"));
    await settle(60);
    check("провалившаяся правка краснеет и объясняет, а не рапортует о применении",
      !/Применено/.test(shown()) && shown().length > 0 &&
        /error/.test($("#save-status").className),
      shown() + " | " + $("#save-status").className);
  }

  // ── предупреждение про поставщика: только при смене модели ──
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "закреплённый", model: "первая/модель",
                extra_body: { provider: { order: ["openai"] } } }],
    });
    client.init();
    await settle(20);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("на закреплённом чате предупреждения нет, пока модель прежняя",
      $("#model-warn").children.length === 0, $("#model-warn").textContent);
    $("#f-model").value = "вторая/модель";
    $("#f-model").dispatchEvent(new Evt("change"));
    await settle(40);
    const text = $("#model-warn").textContent;
    check("после смены модели предупреждение появляется", text.length > 0, text);
    check("и говорит человеческими словами, без имён полей конфига",
      /поставщик/i.test(text) && !/provider|extra_body|order|404/.test(text), text);
  }

  // ── где оказывается лента ──
  //
  // Утверждения про **положение ленты**, а не про внутренний флаг: флаг вёл
  // себя ровно как задумано, а лента оставалась в нуле — содержимое
  // пересоздаётся, и браузер обнуляет прокрутку.
  {
    const TALK = [];
    for (let i = 1; i <= 6; i += 1) {
      TALK.push({ role: "user", content: `вопрос ${i}`, error: null, reasoning: "", metrics: null });
      TALK.push({ role: "assistant", content: `ответ ${i}`, error: null, reasoning: "", metrics: null });
    }
    const atBottom = (feed) => feed.scrollHeight - feed.scrollTop - feed.clientHeight <= 80;
    const UP = 40;   // куда отматывает читатель

    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "чат", transcript: TALK, history_len: TALK.length }],
      delay: 25,
    });
    client.init();
    await settle(30);
    // Экран — единственное, что задаёт проверка: высоту содержимого считает
    // стенд, как браузер считал бы её раскладкой.
    const feed = $("#feed");
    feed.clientHeight = 120;
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    check("открытый чат показывает конец разговора", atBottom(feed),
      `лента на ${feed.scrollTop} из ${feed.scrollHeight}`);

    // Отмотал вверх во время ответа — лента остаётся там, где её оставили.
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(40);
    feed.scrollTop = UP;
    feed.dispatchEvent(new Evt("scroll"));
    await settle(300);
    check("отмотанная во время ответа лента остаётся на месте", feed.scrollTop === UP,
      `лента на ${feed.scrollTop}, ждали ${UP}`);
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
