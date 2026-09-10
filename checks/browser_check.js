// Проверка клиентского кода настоящими вызовами, а не grep'ом по исходнику.
//
//     node checks/browser_check.js
//
// Разбор markdown — самая опасная поверхность демо: в него попадает текст
// от модели, и он же кладётся в innerHTML. Проверять его чтением исходника
// бессмысленно — переедет экранирование за разбор, и grep этого не заметит.
// Поэтому здесь настоящие payload'ы и утверждения про выход.
//
// app.js под node подключается модулем: в браузере `module` не существует,
// и файл просто запускается сам.

const path = require("path");

// Единственное, что app.js трогает на загрузке, — matchMedia для раскладки.
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }) };

const app = require(path.join(__dirname, "..", "app", "static", "app.js"));
const { renderMarkdown, layoutFor, escapeAction, readStopLines, parseResponseFormat, paramWarnings } = app;

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

// ── раскладка узкого окна ──

check(
  "узкое окно сворачивает оба борта",
  JSON.stringify(layoutFor(true, { sidebar: "0", panel: "0" })) ===
    JSON.stringify({ sidebar: true, panel: true }),
  JSON.stringify(layoutFor(true, { sidebar: "0", panel: "0" }))
);
check(
  "узкое окно не смотрит на сохранённый выбор",
  JSON.stringify(layoutFor(true, { sidebar: "1", panel: "1" })) ===
    JSON.stringify({ sidebar: true, panel: true })
);
check(
  "широкое окно возвращает выбор пользователя",
  JSON.stringify(layoutFor(false, { sidebar: "1", panel: "0" })) ===
    JSON.stringify({ sidebar: true, panel: false }),
  JSON.stringify(layoutFor(false, { sidebar: "1", panel: "0" }))
);
check(
  "широкое окно по умолчанию показывает оба борта",
  JSON.stringify(layoutFor(false, { sidebar: "0", panel: "0" })) ===
    JSON.stringify({ sidebar: false, panel: false })
);

// ── предупреждение о параметрах, которых модель не потянет ──

{
  const plain = {
    id: "openai/gpt-4o-mini",
    supported_parameters: ["temperature", "max_tokens", "stop", "response_format"],
    temperature_capped: false,
    temperature_cap: null,
  };
  const capped = {
    id: "anthropic/claude-sonnet",
    supported_parameters: ["temperature", "max_tokens", "top_p"],
    temperature_capped: true,
    temperature_cap: 1.0,
  };

  const unsupported = paramWarnings(plain, { temperature: 0.5, top_k: 40, min_p: 0.05 });
  check("незаявленный параметр даёт предупреждение", unsupported.length === 1, JSON.stringify(unsupported));
  check("в предупреждении названы все незаявленные", /top_k, min_p/.test(unsupported[0] || ""), unsupported[0]);
  check("в предупреждении названа модель", /gpt-4o-mini/.test(unsupported[0] || ""), unsupported[0]);
  check(
    "предупреждение объясняет, чем это кончится",
    /require_parameters/.test(unsupported[0] || ""),
    unsupported[0]
  );

  check(
    "заявленные параметры молчат",
    paramWarnings(plain, { temperature: 0.5, max_tokens: 100, stop: ["СТОП"] }).length === 0
  );
  check(
    "незаданные параметры не считаются незаявленными",
    paramWarnings(plain, { temperature: null, top_k: null, min_p: undefined }).length === 0
  );
  check(
    "response_format проверяется наравне с числами",
    paramWarnings(
      { id: "m", supported_parameters: ["temperature"], temperature_capped: false },
      { response_format: { type: "json_object" } }
    ).length === 1
  );
  check(
    "stop проверяется наравне с числами",
    paramWarnings(
      { id: "m", supported_parameters: ["temperature"], temperature_capped: false },
      { stop: ["КОНЕЦ"] }
    ).length === 1
  );

  // Главная ловушка: модель заявляет temperature и всё равно вернёт 400.
  const hot = paramWarnings(capped, { temperature: 1.2 });
  check("обрезанная температура предупреждает, хотя параметр заявлен", hot.length === 1, JSON.stringify(hot));
  check("в предупреждении назван потолок", /1\.0/.test(hot[0] || ""), hot[0]);
  check("температура под потолком молчит", paramWarnings(capped, { temperature: 0.9 }).length === 0);
  check("ровно потолок молчит", paramWarnings(capped, { temperature: 1.0 }).length === 0);
  check(
    "оба повода дают два предупреждения",
    paramWarnings(capped, { temperature: 1.2, min_p: 0.1 }).length === 2,
    JSON.stringify(paramWarnings(capped, { temperature: 1.2, min_p: 0.1 }))
  );

  // Не на чем основать — не пугаем.
  check("модель не найдена в каталоге — молчим", paramWarnings(null, { top_k: 40 }).length === 0);
  check("модель не отдала supported_parameters — молчим",
    paramWarnings({ id: "m", supported_parameters: [] }, { top_k: 40 }).length === 0);
  check("окно памяти в запрос не уходит и не проверяется",
    paramWarnings(plain, { history_limit: 0 }).length === 0);
}

// ── привязка к поставщику: говорим в момент смены модели ──

{
  const model = { id: "вторая/модель", supported_parameters: ["temperature"], temperature_capped: false };
  const pinned = { provider: { order: ["openai"] } };

  check(
    "пока модель прежняя — молчим",
    paramWarnings(model, { model: "первая/модель" }, pinned, "первая/модель").length === 0
  );
  const changed = paramWarnings(model, { model: "вторая/модель" }, pinned, "первая/модель");
  check("на смене модели предупреждаем", changed.length === 1, JSON.stringify(changed));
  check("названы поставщик и обе модели",
    /openai/.test(changed[0]) && /вторая\/модель/.test(changed[0]) && /первая\/модель/.test(changed[0]),
    changed[0]);
  check("без имён полей конфига и кодов ошибок",
    !/provider|extra_body|order|404/.test(changed[0]), changed[0]);

  check(
    "чат без привязки молчит и на смене модели",
    paramWarnings(model, { model: "вторая/модель" }, {}, "первая/модель").length === 0
  );
  check(
    "запрет фолбэка сам по себе не предупреждает: смену модели он не ломает",
    paramWarnings(model, { model: "вторая/модель" }, { provider: { allow_fallbacks: false } }, "первая/модель")
      .length === 0
  );
  check(
    "без исходной модели молчим: сравнивать не с чем",
    paramWarnings(model, { model: "вторая/модель" }, pinned, "").length === 0
  );
  check(
    "привязка и незаявленный параметр — два повода",
    paramWarnings(model, { model: "вторая/модель", top_k: 40 }, pinned, "первая/модель").length === 2
  );
}

// ── что закрывает Escape ──

check(
  "Escape закрывает диалог подтверждения всегда",
  escapeAction(true, false, 0) === "dialog",
  escapeAction(true, false, 0)
);
check(
  "открытый диалог важнее ящиков",
  escapeAction(true, true, 2) === "dialog",
  escapeAction(true, true, 2)
);
check(
  "без диалога Escape закрывает ящики узкого окна",
  escapeAction(false, true, 1) === "drawers",
  escapeAction(false, true, 1)
);
check("на широком окне Escape не трогает борта", escapeAction(false, false, 2) === null);
check("закрывать нечего — Escape ничего не делает", escapeAction(false, true, 0) === null);

// ── стоп-строки и формат ответа из панели ──

check(
  "стоп-строки читаются по одной в строке",
  JSON.stringify(readStopLines("КОНЕЦ\nСТОП")) === JSON.stringify(["КОНЕЦ", "СТОП"]),
  JSON.stringify(readStopLines("КОНЕЦ\nСТОП"))
);
check("пробелы по краям срезаются", JSON.stringify(readStopLines("  КОНЕЦ  ")) === JSON.stringify(["КОНЕЦ"]));
check("пустые строки не считаются", JSON.stringify(readStopLines("a\n\n\n b ")) === JSON.stringify(["a", "b"]));
check("пустое поле — параметр не отправляется", readStopLines("   ") === null);
check("совсем пустое поле — тоже null", readStopLines("") === null);

check("формат по умолчанию не задан", parseResponseFormat("", "") === null);
check(
  "готовый вариант не требует писать JSON",
  JSON.stringify(parseResponseFormat("json_object", "")) === JSON.stringify({ type: "json_object" })
);
check(
  "свой JSON разбирается",
  JSON.stringify(parseResponseFormat("custom", '{"type":"json_schema"}')) ===
    JSON.stringify({ type: "json_schema" })
);
check("свой JSON пустым не отправляется", parseResponseFormat("custom", "   ") === null);
{
  let broke = false;
  try { parseResponseFormat("custom", "{не json"); } catch (e) { broke = /не JSON/.test(e.message); }
  check("кривой JSON даёт понятную ошибку, а не уезжает провайдеру", broke);
}
{
  let broke = false;
  try { parseResponseFormat("custom", "[1,2]"); } catch (e) { broke = /объект/.test(e.message); }
  check("массив вместо объекта тоже ошибка", broke);
}

// ── маршрут целиком: правка в панели → отправка → тело запроса ──
//
// Смена системного промпта ломалась трижды, и все три раза сервер был
// зелёным: расходились состояние панели и состояние агента. Значит проверять
// надо не ручку, а тот код, который выполняется в браузере, и по всему
// маршруту. `checks/dom.js` даёт минимальный DOM и минимальный сервер,
// который записывает, с каким конфигом ушло каждое сообщение.
//
// Правку вносим **без события `change`** — просто ставим значение в поле,
// как это делает пользователь, ещё не убрав из него курсор. Ровно так
// баг и воспроизводился: событие — один-единственный шанс доставить правку,
// и поводов его упустить сколько угодно.

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
  ["f-history_limit", "0", "history_limit", 0],
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

// Плитка по метке — заглушкой, если такой плитки нет. Иначе переименованная
// плитка роняет весь маршрут исключением, и вместо одного внятного «покраснело»
// получается «проверка упала», за которой не видно остальных утверждений.
// Кнопка карточки — по назначению, а не по номеру в ряду. Индекс уже дважды
// оказывался хрупким: стоит убрать или переставить кнопку, и утверждение
// начинает жать не то, оставаясь зелёным ровно до тех пор, пока новая кнопка
// случайно не делает то же самое.
function cardButton(card, title) {
  const titles = card.querySelectorAll(".icon-btn").map((b) => b.title);
  const btn = card.querySelectorAll(".icon-btn").find((b) => b.title === title);
  if (btn) return btn;
  // Пропавшая кнопка — это красное утверждение, а не упавший маршрут:
  // исключение унесло бы с собой все проверки, стоящие после него.
  check("в карточке есть кнопка «" + title + "»", false, titles.join(" | "));
  return { dispatchEvent() {} };
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
  // ── шапка страницы называет тот день, который в ней лежит ──
  //
  // Ветку с чужим номером дня видно первым же кадром записи, а из проверок
  // на это не смотрел никто.
  {
    const { client, $, settle } = freshClient();
    client.init();
    await settle(20);
    check("в шапке стоит номер этого дня",
      $(".brand-sub").textContent === "чат · день 8", $(".brand-sub").textContent);
  }

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

  // ── правка во время генерации ──
  {
    const { client, server, $, settle } = freshClient({ delay: 30 });
    client.init();
    await settle(20);
    $("#input").value = "первый";
    $("#composer").requestSubmit();
    await settle(40);
    check("во время генерации клиент занят", client.state.busy === true);
    $("#f-system").value = "ПРАВКА НА ЛЕТУ";
    await settle(200);
    $("#input").value = "второй";
    $("#composer").requestSubmit();
    await settle(250);
    const second = server.state.sent[1];
    check(
      "правка во время генерации уезжает со следующим сообщением",
      second && second.config.system === "ПРАВКА НА ЛЕТУ",
      JSON.stringify(server.state.sent.map((x) => x.config.system))
    );
    check(
      "текущий ответ правка не задела",
      server.state.sent[0].config.system === "СТАРЫЙ ПРОМПТ",
      server.state.sent[0].config.system
    );
  }

  // ── перегенерация подчиняется тому же правилу ──
  {
    const { client, server, $, settle } = freshClient();
    client.init();
    await settle(20);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(80);
    $("#f-system").value = "ПРОМПТ ДЛЯ ПОВТОРА";
    await client.state.applying;
    const card = $("#feed").querySelector(".card");
    const refresh = cardButton(card, "Перегенерировать");
    refresh.dispatchEvent(new (require(path.join(__dirname, "dom.js")).Evt)("click"));
    await settle(120);
    const last = server.state.sent[server.state.sent.length - 1];
    check(
      "перегенерация тоже идёт с тем, что показывает панель",
      last && last.config.system === "ПРОМПТ ДЛЯ ПОВТОРА",
      JSON.stringify(server.state.sent.map((x) => x.config.system))
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

  // ── пустой старт: писать надо куда-то сразу ──
  {
    const { client, server, $, settle, Evt } = freshClient({ bare: true });
    check("на чистом старте на сервере нет чатов", server.state.agents.length === 0,
      String(server.state.agents.length));
    client.init();
    await settle(60);

    check("клиент заводит один чат сам", server.state.agents.length === 1,
      String(server.state.agents.length));
    check("и сразу его открывает", Boolean(client.state.current), "чат не открыт");
    check("имя у него по умолчанию", /^Новый чат \d+$/.test(client.state.current.label),
      client.state.current.label);
    check("панель у него тоже пустая: промпт не придуман за пользователя",
      $("#f-system").value === "", JSON.stringify($("#f-system").value));
    check("и он пуст: ни переписки, ни черновика",
      client.state.current.transcript.length === 0 && $("#input").value === "",
      JSON.stringify([client.state.current.transcript, $("#input").value]));
    check("в списке ровно одна строка",
      $("#agent-list").querySelectorAll(".item").length === 1,
      String($("#agent-list").querySelectorAll(".item").length));

    // Удалили единственный чат — появился свежий, а не пустой экран.
    const before = client.state.current.id;
    const row = $("#agent-list").querySelectorAll(".item")[0];
    row.querySelectorAll(".mini")[1].dispatchEvent(new Evt("click"));
    const dialog = $(".confirm");
    check("удаление спрашивает подтверждение", Boolean(dialog), "диалога нет");
    const buttons = dialog.querySelectorAll(".primary");
    buttons[0].dispatchEvent(new Evt("click"));
    await settle(80);

    check("после удаления последнего чата появляется свежий",
      server.state.agents.length === 1 && client.state.current.id !== before,
      JSON.stringify({ живых: server.state.agents.length, было: before, стало: client.state.current && client.state.current.id }));
    check("и он тоже пустой", client.state.current.transcript.length === 0,
      JSON.stringify(client.state.current.transcript));
    check("номер имени не переиспользуется", client.state.current.label === "Новый чат 2",
      client.state.current.label);
  }

  // ── «Новый чат» встаёт в конец списка ──
  //
  // Порядок списка слева не стерёг никто: разворот сортировки реестра
  // наоборот не ронял ни одной проверки. Серверная половина —
  // check_list_order в run_checks.py.
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
      names().length === 3 && /^Новый чат/.test(names()[2]), names().join(" | "));
    check("и он же открыт",
      client.state.current && client.state.current.label === names()[2],
      client.state.current && client.state.current.label);
    check("порядок прежних не тронут",
      names()[0] === "первый чат" && names()[1] === "второй чат", names().join(" | "));
    check("на сервере тот же порядок",
      server.state.agents.map((a) => a.label).join(" | ") === names().join(" | "),
      server.state.agents.map((a) => a.label).join(" | "));
  }

  // ── лента рисуется из стенограммы ──
  //
  // Клиент после каждого обмена перечитывает агента и перерисовывает ленту
  // целиком: на экране должно быть ровно то, что у агента в истории, а не то,
  // что клиент дорисовал по дороге. Значит формат стенограммы — часть
  // поведения, и раньше его не стерёг никто: поле можно было убрать из ответа
  // ручки, и ни одна проверка не краснела. Серверная половина —
  // check_transcript_shape в run_checks.py.
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

    const feed = $("#feed");
    const nodes = feed.children.filter(
      (el) => el.classList.contains("msg-user") || el.classList.contains("card")
    );
    check("в ленте по узлу на реплику стенограммы", nodes.length === talk.length,
      `узлов ${nodes.length}, реплик ${talk.length}`);
    check("вопросы — пузырями, ответы — карточками",
      nodes.map((el) => (el.classList.contains("msg-user") ? "u" : "a")).join("") === "uaua",
      nodes.map((el) => el.className).join(" | "));

    // Кнопка «метрики этого ответа» убрана: числа обмена написаны под ним
    // самим, а плитки справа теперь про весь диалог, и класть в них один
    // выбранный ответ значило бы показывать в панели не то, что она обещает.
    check("кнопки «метрики этого ответа» в карточке нет",
      nodes[1].querySelectorAll(".icon-btn").every((b) => b.title !== "Метрики этого ответа"),
      nodes[1].querySelectorAll(".icon-btn").map((b) => b.title).join(" | "));
    check("а копирование, перегенерация и сырой текст на месте",
      nodes[1].querySelectorAll(".icon-btn").map((b) => b.title).join(" | ") ===
        "Копировать ответ | Перегенерировать | Показать сырой текст",
      nodes[1].querySelectorAll(".icon-btn").map((b) => b.title).join(" | "));

    check("текст вопроса показан", nodes[0].textContent === "мой вопрос", nodes[0].textContent);
    check("текст ответа показан и разобран как markdown",
      nodes[1].querySelector(".card-body").innerHTML.includes("<strong>жирный</strong>"),
      nodes[1].querySelector(".card-body").innerHTML);
    check("имя модели берётся из метрик реплики, а не из конфига чата",
      nodes[1].querySelector(".card-model").textContent === "особая/модель",
      nodes[1].querySelector(".card-model").textContent);
    check("провайдер показан под ответом, вместе с тем, как прошёл вызов",
      nodes[1].querySelector(".usage-how").textContent === "поставщик",
      String(nodes[1].querySelector(".usage-how")));
    check("и в шапке карточки его больше нет — одно число живёт в одном месте",
      nodes[1].querySelector(".card-head").textContent === "особая/модель",
      nodes[1].querySelector(".card-head").textContent);
    check("рассуждение показано свёрнутым блоком",
      Boolean(nodes[1].querySelector(".think")) &&
        nodes[1].querySelector(".think-body").textContent === "я подумал",
      String(nodes[1].querySelector(".think")));
    check("у целого ответа блока ошибки нет",
      nodes[1].querySelector(".card-error") === null, "блок ошибки появился");

    check("оборванный ответ помечен",
      nodes[3].classList.contains("failed"), nodes[3].className);
    check("и текст ошибки показан",
      nodes[3].querySelector(".card-error") &&
        nodes[3].querySelector(".card-error").textContent === "оборвалось",
      String(nodes[3].querySelector(".card-error")));

    // Пустая стенограмма — это заставка, а не пустая лента с нулём узлов.
    $("#agent-list").querySelectorAll(".item-open")[0].dispatchEvent(new Evt("click"));
    await settle(40);
    check("пустой чат показывает заставку, а не пустоту",
      Boolean($("#feed").querySelector(".empty")), $("#feed").textContent);
  }

  // ── переименование чата в списке слева ──
  //
  // Раньше это стереглось грепом по исходнику: «в app.js есть строка
  // function startRename», «есть miniButton("pencil")», «встречаются слова
  // Enter и Escape». Такая проверка описывает реализацию, а не поведение:
  // переименование можно сломать, не тронув ни одной из этих строк, и она
  // останется зелёной. Здесь — настоящий маршрут: клик по карандашу,
  // клавиша, запрос к серверу, имя в списке.
  {
    const { client, server, $, settle, Evt } = freshClient();
    client.init();
    await settle(30);

    const row = () => $("#agent-list").querySelectorAll(".item")[0];
    const title = () => row().querySelector(".item-title");
    const patches = () =>
      server.state.requests.filter((r) => r.method === "PATCH" && "label" in (r.body || {}));

    check("до переименования в строке показано имя чата",
      title() && title().textContent === "первый чат",
      title() && title().textContent);

    // 1. Карандаш открывает поле прямо в строке, со старым именем внутри.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    let field = row().querySelector(".item-rename");
    check("карандаш открывает поле ввода прямо в строке", Boolean(field), "поля нет");
    check("в поле стоит нынешнее имя", field && field.value === "первый чат",
      field && field.value);
    check("пока переименовываем, кнопки открытия чата в строке нет",
      row().querySelector(".item-open") === null, "кнопка осталась");

    // 2. Enter сохраняет: правка уходит на сервер и видна в списке.
    field.value = "новое имя";
    field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    await settle(40);
    check("Enter отправляет новое имя на сервер",
      patches().length === 1 && patches()[0].body.label === "новое имя",
      JSON.stringify(patches().map((r) => r.body)));
    check("имя на сервере поменялось",
      server.state.agents[0].label === "новое имя", server.state.agents[0].label);
    check("список показывает новое имя", title() && title().textContent === "новое имя",
      title() && title().textContent);
    check("поле ввода закрылось", row().querySelector(".item-rename") === null, "поле осталось");

    // 3. Escape отменяет: ни запроса, ни следа в списке.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    field = row().querySelector(".item-rename");
    field.value = "передумал";
    field.dispatchEvent(new Evt("keydown", { key: "Escape" }));
    await settle(40);
    check("Escape не шлёт запроса", patches().length === 1,
      JSON.stringify(patches().map((r) => r.body)));
    check("Escape оставляет прежнее имя", title() && title().textContent === "новое имя",
      title() && title().textContent);

    // 4. Потеря фокуса сохраняет: имя не должно теряться молча.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    field = row().querySelector(".item-rename");
    field.focus();
    field.value = "по потере фокуса";
    field.blur();
    await settle(40);
    check("потеря фокуса тоже сохраняет",
      patches().length === 2 && patches()[1].body.label === "по потере фокуса",
      JSON.stringify(patches().map((r) => r.body)));
    check("и список это показывает", title() && title().textContent === "по потере фокуса",
      title() && title().textContent);

    // 5. Пустое имя чат не стирает: запроса нет, имя прежнее.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    field = row().querySelector(".item-rename");
    field.value = "   ";
    field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    await settle(40);
    check("пустым именем чат не переименовать", patches().length === 2,
      JSON.stringify(patches().map((r) => r.body)));
    check("после пустого ввода имя осталось прежним",
      title() && title().textContent === "по потере фокуса",
      title() && title().textContent);
  }

  // ── где оказывается лента ──
  //
  // Утверждения здесь про **положение ленты**, а не про внутренний флаг.
  // Прошлая версия проверяла флаг — и пропустила регресс: флаг вёл себя
  // ровно как задумано, а лента при этом оставалась в нуле, потому что
  // содержимое пересоздаётся и браузер обнуляет прокрутку. Смотреть надо
  // на то, что видит читатель.
  {
    // Разговор должен быть длиннее экрана — иначе прокручивать нечего.
    // Высоту стенд считает по числу узлов, так что «длиннее» здесь значит
    // «больше сообщений», как и в браузере.
    const TALK = [];
    for (let i = 1; i <= 6; i += 1) {
      TALK.push({ role: "user", content: `вопрос ${i}`, error: null, reasoning: "", metrics: null });
      TALK.push({ role: "assistant", content: `ответ ${i}`, error: null, reasoning: "", metrics: null });
    }
    const withHistory = (label) => ({ label, transcript: TALK.slice(), history_len: TALK.length });
    const atBottom = (feed) => feed.scrollHeight - feed.scrollTop - feed.clientHeight <= 80;
    // Экран — единственное, что задаёт проверка: высоту содержимого считает
    // стенд, как браузер считал бы её раскладкой.
    const measure = (feed) => {
      feed.clientHeight = 120;
      return feed;
    };
    const UP = 40;   // куда отматывает читатель

    // 1. Чат открывают, чтобы увидеть последнее сообщение.
    {
      const { client, $, settle, Evt } = freshClient({
        chats: [withHistory("чат А"), withHistory("чат Б")],
      });
      client.init();
      await settle(30);
      const feed = measure($("#feed"));
      // Два первых чата в стенде — без истории; наши с историей идут за ними.
      $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
      await settle(40);
      check("открытый чат показывает конец разговора", atBottom(feed),
        `лента на ${feed.scrollTop} из ${feed.scrollHeight}`);

      // 2. Отмотал вверх в одном чате — другой всё равно открывается внизу.
      feed.scrollTop = UP;
      feed.dispatchEvent(new Evt("scroll"));
      $("#agent-list").querySelectorAll(".item-open")[3].dispatchEvent(new Evt("click"));
      await settle(40);
      check("отмотанная лента не переносится на другой чат", atBottom(feed),
        `лента на ${feed.scrollTop} из ${feed.scrollHeight}`);

      // ...и возврат в первый чат тоже показывает конец разговора.
      feed.scrollTop = UP;
      feed.dispatchEvent(new Evt("scroll"));
      $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
      await settle(40);
      check("и возврат в прежний чат — тоже", atBottom(feed),
        `лента на ${feed.scrollTop} из ${feed.scrollHeight}`);
    }

    // 3. Перегенерацию просит сам читатель — значит показать, что вышло.
    {
      const { client, $, settle, Evt } = freshClient({ chats: [withHistory("чат")] });
      client.init();
      await settle(30);
      const feed = measure($("#feed"));
      $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
      await settle(40);
      feed.scrollTop = UP;
      feed.dispatchEvent(new Evt("scroll"));
      const card = feed.querySelector(".card");
      cardButton(card, "Перегенерировать").dispatchEvent(new Evt("click"));
      await settle(150);
      check("перегенерация при отмотанной ленте показывает новый ответ", atBottom(feed),
        `лента на ${feed.scrollTop} из ${feed.scrollHeight}`);
    }

    // 4. Отмотал вверх во время ответа — лента остаётся там, где её оставили.
    {
      const { client, $, settle, Evt } = freshClient({ chats: [withHistory("чат")], delay: 25 });
      client.init();
      await settle(30);
      const feed = measure($("#feed"));
      // Открываем именно чат с историей: в пустом отматывать нечего.
      $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
      await settle(40);
      $("#input").value = "вопрос";
      $("#composer").requestSubmit();
      await settle(40);
      feed.scrollTop = UP;
      feed.dispatchEvent(new Evt("scroll"));
      await settle(300);
      check("отмотанная во время ответа лента остаётся на месте", feed.scrollTop === UP,
        `лента на ${feed.scrollTop}, ждали ${UP}`);
      check("и уж точно не в начале разговора", feed.scrollTop !== 0, String(feed.scrollTop));
    }

    // 5. Прижатая лента доматывается сама.
    {
      const { client, $, settle, Evt } = freshClient({ chats: [withHistory("чат")], delay: 5 });
      client.init();
      await settle(30);
      const feed = measure($("#feed"));
      $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
      await settle(40);
      $("#input").value = "вопрос";
      $("#composer").requestSubmit();
      await settle(200);
      check("прижатая лента доматывается к новому ответу", atBottom(feed),
        `лента на ${feed.scrollTop} из ${feed.scrollHeight}`);
    }
  }

  // ── «Применено» — о событии, а не о каждой отправке ──
  //
  // Жалоба заказчика: строка появлялась на каждое сообщение. Причина
  // в правке двумя кругами раньше: пролив панели переехал внутрь `exchange`
  // и стал случаться перед каждой отправкой, а сообщение показывалось
  // по факту пролива. Инвариант тут ни при чём — проливать надо всегда,
  // сообщать не о чем. Утверждения ниже — про то, что видно на экране.
  {
    const { client, $, settle, Evt } = freshClient();
    const shown = () => $("#save-status").textContent;
    client.init();
    await settle(20);

    // 1. Отправка без единой правки: панель проливается, экран молчит.
    $("#input").value = "первый";
    $("#composer").requestSubmit();
    await settle(120);
    check("отправка без правок ничего не сообщает", shown() === "", shown());

    $("#input").value = "второй";
    $("#composer").requestSubmit();
    await settle(120);
    check("и вторая отправка тоже", shown() === "", shown());

    // 2. Правка есть — сообщение появляется.
    $("#f-system").value = "ПРАВКА";
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("после настоящей правки сообщение есть", /Применено/.test(shown()), shown());

    // 3. Правка, а сразу за ней отправка: сообщение остаётся тем же самым,
    //    а не появляется вторым — таймер у него один, назначенный правкой.
    const timer = client.state.statusTimer;
    $("#input").value = "третий";
    $("#composer").requestSubmit();
    await settle(120);
    check("отправка сразу после правки не показывает второе сообщение",
      /Применено/.test(shown()) && client.state.statusTimer === timer,
      `${shown()} | таймер ${client.state.statusTimer === timer ? "тот же" : "новый"}`);

    // 4. Следующая отправка уже без правок — снова тишина.
    await settle(0);
    $("#save-status").textContent = "";
    $("#input").value = "четвёртый";
    $("#composer").requestSubmit();
    await settle(120);
    check("после применённой правки следующая отправка молчит", shown() === "", shown());
  }

  // ── пролив теми же значениями изменением не является ──
  {
    const { client, $, settle, Evt } = freshClient();
    const shown = () => $("#save-status").textContent;
    client.init();
    await settle(20);

    // Ставим в поля ровно то, что там уже стоит: панель «изменилась»
    // по событию, но конфиг агента — нет.
    $("#f-system").value = $("#f-system").value;
    $("#f-temperature").value = $("#f-temperature").value;
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("правка теми же значениями ничего не сообщает", shown() === "", shown());

    // Пустое поле не равно нулю: заданный ноль — это правка.
    $("#f-top_p").value = "0";
    $("#f-top_p").dispatchEvent(new Evt("change"));
    await settle(40);
    check("ноль в пустом поле — настоящая правка", /Применено/.test(shown()), shown());

    // ...а стереть ноль обратно в пустоту — тоже правка, в другую сторону.
    $("#save-status").textContent = "";
    $("#f-top_p").value = "";
    $("#f-top_p").dispatchEvent(new Evt("change"));
    await settle(40);
    check("и стереть его обратно — тоже", /Применено/.test(shown()), shown());

    // Стоп-строки: лишний перевод строки списком не становится.
    $("#save-status").textContent = "";
    $("#f-stop").value = "\n\n";
    $("#f-stop").dispatchEvent(new Evt("change"));
    await settle(40);
    check("пустые строки в стоп-списке правкой не считаются", shown() === "", shown());

    $("#f-stop").value = "КОНЕЦ";
    $("#f-stop").dispatchEvent(new Evt("change"));
    await settle(40);
    check("а настоящая стоп-строка — считается", /Применено/.test(shown()), shown());
  }

  // ── две правки подряд: таймер не залипает, и строка гаснет ──
  //
  // Единственное место, где проверяется гашение строки состояния: правка
  // показывает «Применено», вторая правка перевешивает таймер, а не копит
  // второй, и через пять секунд после последней правки строка пуста.
  {
    const { client, $, settle, Evt } = freshClient();
    const shown = () => $("#save-status").textContent;
    client.init();
    await settle(20);

    $("#f-system").value = "раз";
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    const first = client.state.statusTimer;
    $("#f-system").value = "два";
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("вторая правка перевешивает таймер, а не копит второй",
      /Применено/.test(shown()) && Boolean(client.state.statusTimer) &&
        client.state.statusTimer !== first,
      `${shown()} | ${first === client.state.statusTimer ? "тот же таймер" : "новый таймер"}`);

    await new Promise((r) => setTimeout(r, 5100));
    check("и через пять секунд после последней правки строка пуста",
      shown() === "", shown());
  }

  // ── неудачная правка: своя красная строка, а не «Применено» ──
  {
    const { client, $, settle, Evt } = freshClient();
    const shown = () => $("#save-status").textContent;
    client.init();
    await settle(20);

    // Пустая модель — сервер отвечает 400.
    $("#f-model").value = "";
    $("#f-model").dispatchEvent(new Evt("change"));
    await settle(60);
    check("провалившийся PATCH не сообщает о применении", !/Применено/.test(shown()), shown());
    check("а говорит, что не так", shown().length > 0, "строка пуста");
    check("и делает это красным",
      /error/.test($("#save-status").className), $("#save-status").className);
  }

  // ── перегенерация без правок ──
  {
    const { client, $, settle, Evt } = freshClient();
    const shown = () => $("#save-status").textContent;
    client.init();
    await settle(20);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(120);
    $("#save-status").textContent = "";

    const card = $("#feed").querySelector(".card");
    cardButton(card, "Перегенерировать").dispatchEvent(new Evt("click"));
    await settle(140);
    check("перегенерация без правок ничего не сообщает", shown() === "", shown());
  }

  // ── предупреждение про поставщика: только при смене модели ──
  {
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "закреплённый", model: "первая/модель",
                extra_body: { provider: { order: ["openai"] } } }],
    });
    client.init();
    await settle(20);
    const rows = $("#agent-list").querySelectorAll(".item-open");
    rows[2].dispatchEvent(new Evt("click"));
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

    const PANEL = ["Весь вход", "Весь выход", "Всего токенов", "Стоимость", "Обменов", "Контекст"];
    const tiles = () => {
      const out = {};
      PANEL.forEach((name) => { out[name] = tileOf($, name); });
      return out;
    };
    const labels = () => $("#tiles").children.map((t) => t.querySelector(".tile-k").textContent);
    const rowOf = (card, cls) => {
      const el = card.querySelector(cls);
      return el ? el.textContent : "(строки нет)";
    };
    const cards = () => $("#feed").querySelectorAll(".card");

    // Сетка — две колонки. Плиток пять оставили бы пустую клетку в последнем
    // ряду, девять — лишний ряд: и то и другое мы уже чинили. Утверждение
    // на **число** плиток стоит именно поэтому.
    check("плиток ровно шесть — сетка 2×3, пустых клеток нет",
      $("#tiles").children.length === 6, "плиток " + $("#tiles").children.length);
    check("и это те самые шесть", labels().join(" | ") === PANEL.join(" | "), labels().join(" | "));

    const empty = tiles();
    check("до первого ответа весь вход — прочерк, а не ноль",
      empty["Весь вход"].v === "—", shownAs(empty["Весь вход"]));
    check("и обменов ещё нет", empty["Обменов"].v === "—", shownAs(empty["Обменов"]));
    check("и контекст пуст", empty["Контекст"].v === "—", shownAs(empty["Контекст"]));

    $("#input").value = "первый вопрос";
    $("#composer").requestSubmit();
    await settle(90);

    const one = tiles();
    check("после первого обмена панель показывает его целиком",
      [one["Весь вход"].v, one["Весь выход"].v, one["Всего токенов"].v,
       one["Стоимость"].v, one["Обменов"].v].join(" | ") ===
        "1 240 | 312 | 1 552 | $0.000186 | 1",
      [one["Весь вход"].v, one["Весь выход"].v, one["Всего токенов"].v,
       one["Стоимость"].v, one["Обменов"].v].join(" | "));
    check("контекст — доля окна по последнему ответу",
      one["Контекст"].v === "1.2 %", one["Контекст"].v);
    check("подписей в плитках больше нет: панель и так вся про диалог",
      PANEL.every((name) => tileOf($, name).sub === ""),
      PANEL.map((name) => name + ":" + tileOf($, name).sub).join(" | "));

    // Цена в девять знаков занимает ширину плитки целиком. Ширин стенд
    // не знает, но обрезка берётся из разметки: если в строке значения
    // окажется кто-то ещё, на экране останется «$0.000...».
    check("в строке значения стоимости никого, кроме самого значения",
      tileOf($, "Стоимость").row === "tile-v small", tileOf($, "Стоимость").row);

    check("под ответом — числа этого обмена",
      rowOf(cards()[0], ".usage-tokens") === "вход 1 240 · выход 312 · всего 1 552 · $0.000186",
      rowOf(cards()[0], ".usage-tokens"));
    check("и второй строкой — как прошёл вызов",
      rowOf(cards()[0], ".usage-how") === "12.4 ток/с за 2.35 с · первый токен 0.42 с · стенд",
      rowOf(cards()[0], ".usage-how"));

    $("#input").value = "второй вопрос";
    $("#composer").requestSubmit();
    await settle(90);

    const two = tiles();
    check("после второго обмена в панели сумма, а не последний обмен",
      [two["Весь вход"].v, two["Весь выход"].v, two["Всего токенов"].v,
       two["Стоимость"].v, two["Обменов"].v].join(" | ") ===
        "13.2k | 812 | 14.1k | $0.000586 | 2",
      [two["Весь вход"].v, two["Весь выход"].v, two["Всего токенов"].v,
       two["Стоимость"].v, two["Обменов"].v].join(" | "));
    check("контекст не усредняется — он по последнему ответу",
      two["Контекст"].v === "9.8 %", two["Контекст"].v);
    check("а строки в ленте остались каждая при своём обмене",
      cards().map((c) => rowOf(c, ".usage-tokens")).join(" | ") ===
        "вход 1 240 · выход 312 · всего 1 552 · $0.000186 | " +
        "вход 12k · выход 500 · всего 12.5k · $0.000400",
      cards().map((c) => rowOf(c, ".usage-tokens")).join(" | "));
    check("и «как прошёл вызов» у второго ответа своё",
      rowOf(cards()[1], ".usage-how") === "9.5 ток/с за 5.00 с · первый токен 0.38 с · стенд",
      rowOf(cards()[1], ".usage-how"));
  }

  // ── контекст сбрасывается сменой модели ──
  //
  // Окно у новой модели другое, и прежний процент к ней не относится:
  // показывать его дальше значило бы врать ровно там, где цифра выглядит
  // убедительнее всего.
  {
    const { client, $, settle, Evt } = freshClient({
      usage: () => ({ prompt_tokens: 100, completion_tokens: 20, total_tokens: 120,
                      cost_usd: 0.0001, context_fill_pct: 42.5 }),
    });
    client.init();
    await settle(30);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(90);
    check("после ответа контекст заполнен", tileOf($, "Контекст").v === "42.5 %",
      tileOf($, "Контекст").v);

    $("#f-model").value = "вторая/модель";
    $("#f-model").dispatchEvent(new Evt("change"));
    await client.state.applying;
    await settle(40);
    check("сменили модель — контекст погас, не дожидаясь следующего ответа",
      tileOf($, "Контекст").v === "—", tileOf($, "Контекст").v);
    check("а токены и стоимость чата на месте: они от модели не зависят",
      tileOf($, "Всего токенов").v === "120" && tileOf($, "Стоимость").v === "$0.000100",
      tileOf($, "Всего токенов").v + " | " + tileOf($, "Стоимость").v);
  }

  // ── чисел нет — нет и строки с числами, а плитка показывает прочерк ──
  {
    const talk = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ без чисел", error: null, reasoning: "",
        metrics: { model: "особая/модель", provider: "поставщик" },
      },
      { role: "user", content: "второй", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "огрызок", error: "оборвалось", reasoning: "",
        metrics: { prompt_tokens: 90, completion_tokens: 4, total_tokens: 94, cost_usd: 0.00001 },
      },
      { role: "user", content: "третий", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "целый", error: null, reasoning: "",
        metrics: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "без чисел", transcript: talk, history_len: talk.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const cards = $("#feed").querySelectorAll(".card");
    check("у ответа без чисел строки с токенами нет вовсе",
      cards[0].querySelector(".usage-tokens") === null,
      String(cards[0].querySelector(".usage-tokens")));
    check("но известное про него показано: поставщик",
      cards[0].querySelector(".usage-how").textContent === "поставщик",
      String(cards[0].querySelector(".usage-how")));
    check("у оборванного ответа числа есть: они идут в итог чата",
      Boolean(cards[1].querySelector(".usage-tokens")) &&
        cards[1].querySelector(".usage-tokens").textContent ===
          "вход 90 · выход 4 · всего 94 · $0.000010",
      String(cards[1].querySelector(".usage-tokens")));
    const zeros = cards[2].querySelector(".usage-tokens");
    check("а нулевые числа — это ответ, и он показан нулями, а не прочерком",
      Boolean(zeros) && zeros.textContent === "вход 0 · выход 0 · всего 0 · $0.000000",
      zeros ? zeros.textContent : "строки нет вовсе");

    // Лента и панель обязаны сходиться: строка под каждым ответом — слагаемое,
    // плитка — их сумма. Расхождение здесь значит, что на экране две разные
    // правды, и какая из них настоящая, читателю не узнать.
    const shown = $("#feed").querySelectorAll(".usage-tokens")
      .map((el) => Number(/всего ([\d ]+)/.exec(el.textContent)[1].replace(/ /g, "")));
    check("строк с числами столько же, сколько ответов с числами",
      shown.length === 2, JSON.stringify(shown));
    check("сумма видимых строк — 94", shown.reduce((a, b) => a + b, 0) === 94, JSON.stringify(shown));
    check("и в плитке стоит она же", tileOf($, "Всего токенов").v === "94",
      tileOf($, "Всего токенов").v);
    check("обменов посчитано столько же, сколько строк с числами",
      tileOf($, "Обменов").v === "2", tileOf($, "Обменов").v);

    // Ноль и «неизвестно» на экране разные: это единственное, что отличает
    // «модель ничего не потратила» от «мы не знаем, сколько она потратила».
    check("формат: ноль остаётся нулём", client.fmt.tokens(0) === "0", client.fmt.tokens(0));
    check("формат: неизвестное — прочерк",
      client.fmt.tokens(null) === "—" && client.fmt.tokens(undefined) === "—",
      client.fmt.tokens(null));
    check("формат: тысячи разделяются", client.fmt.tokens(1240) === "1 240", client.fmt.tokens(1240));
    check("формат: девять с половиной тысяч ещё не k",
      client.fmt.tokens(9999) === "9 999", client.fmt.tokens(9999));
    check("формат: от десяти тысяч — k", client.fmt.tokens(10000) === "10k", client.fmt.tokens(10000));
    check("формат: k с десятой долей", client.fmt.tokens(12449) === "12.4k", client.fmt.tokens(12449));
    check("формат: сотни тысяч тоже k", client.fmt.tokens(128600) === "128.6k",
      client.fmt.tokens(128600));
    // «1000k» читается как миллион и им же и является — так и пишем.
    check("формат: у k есть потолок", client.fmt.tokens(999999) === "1M",
      client.fmt.tokens(999999));
    check("формат: до потолка всё ещё k", client.fmt.tokens(999499) === "999.5k",
      client.fmt.tokens(999499));
    check("формат: миллионы с десятой долей", client.fmt.tokens(1250000) === "1.3M",
      client.fmt.tokens(1250000));
  }

  // ── выход думающей модели: рассуждение названо отдельным числом ──
  //
  // Провайдер кладёт токены рассуждения **внутрь** completion_tokens: выход
  // выходит заметно больше видимого текста, и без оговорки число выглядит
  // враньём. Вычитать его нельзя — «выход» перестанет быть тем, что прислал
  // провайдер, и перестанет сходиться с итогом чата.
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
    const line = $("#feed").querySelector(".usage-tokens").textContent;
    check("рассуждение названо отдельным числом внутри выхода",
      line === "вход 100 · выход 60 (из них 40 рассуждение) · всего 160 · $0.000100", line);
    check("в панели весь выход — то, что прислал провайдер, без вычитаний",
      tileOf($, "Весь выход").v === "60", tileOf($, "Весь выход").v);
  }

  // ── первый токен честный и виден под ответом ──
  //
  // На думающей модели `ttft_ms` наступает, когда модель домыслила: показывать
  // его значило бы записать всё размышление в «модель молчала». Раньше это
  // стерёг греп по `app/static/app.js` на строку «Первый токен, с» — проверка,
  // которая ничего не проверяла: плитку можно было переименовать, а число
  // подменить, и греп остался бы зелёным. Здесь настоящий рендер.
  {
    const slow = [
      { role: "user", content: "вопрос", error: null, reasoning: "", metrics: null },
      {
        role: "assistant", content: "ответ", error: null, reasoning: "долго думал",
        metrics: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15,
                   first_token_ms: 500, ttft_ms: 2000, tokens_per_second: 4.2,
                   elapsed_ms: 2500, provider: "поставщик" },
      },
    ];
    const { client, $, settle, Evt } = freshClient({
      chats: [{ label: "думающая", transcript: slow, history_len: slow.length }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);
    const how = $("#feed").querySelector(".usage-how").textContent;
    check("под ответом стоит момент, когда модель заговорила вообще",
      how === "4.2 ток/с за 2.50 с · первый токен 0.50 с · поставщик", how);
    check("а не тот, когда она домыслила", !how.includes("2.00 с"), how);
  }

  // ── итог берётся с сервера, а не складывается в браузере ──
  //
  // Инвариант дня: сумму считает агент, клиент её только показывает. Держался
  // он на честном слове — клиент, складывающий метрики стенограммы сам, проходил
  // все проверки насквозь. Здесь сервер называет заведомо другие числа, чем те,
  // что лежат в репликах: показать клиент обязан серверные.
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
        label: "чужой счёт", transcript: talk, history_len: talk.length,
        usage_total: { prompt_tokens: 777777, completion_tokens: 999999,
                       total_tokens: 1777776, cost_usd: 0.5, answers: 5 },
      }],
    });
    client.init();
    await settle(30);
    $("#agent-list").querySelectorAll(".item-open")[2].dispatchEvent(new Evt("click"));
    await settle(40);

    const v = (name) => tileOf($, name).v;
    check("весь вход — серверный, а не сумма реплик", v("Весь вход") === "777.8k", v("Весь вход"));
    check("весь выход — серверный", v("Весь выход") === "1M", v("Весь выход"));
    check("всего токенов — серверное", v("Всего токенов") === "1.8M", v("Всего токенов"));
    check("стоимость — серверная", v("Стоимость") === "$0.500000", v("Стоимость"));
    check("и обмены считает тоже сервер", v("Обменов") === "5", v("Обменов"));
    // И при этом строка под ответом показывает числа самой реплики: клиент
    // не подгоняет одно под другое, он показывает оба источника как есть.
    check("а строка под ответом — числа своей реплики",
      $("#feed").querySelector(".usage-tokens").textContent ===
        "вход 10 · выход 12 · всего 22 · $0.000002",
      String($("#feed").querySelector(".usage-tokens")));
  }

  // ── у стриминговой карточки строки нет: числа приходят последним кадром ──
  {
    const { client, $, settle } = freshClient({
      delay: 25,
      usage: () => ({ prompt_tokens: 40, completion_tokens: 8, total_tokens: 48, cost_usd: 0.00002 }),
    });
    client.init();
    await settle(30);
    $("#input").value = "вопрос";
    $("#composer").requestSubmit();
    await settle(40);   // стрим ещё идёт: кадр с usage не пришёл
    check("пока идёт генерация, карточка занята",
      Boolean($("#feed").querySelector(".card.busy")), $("#feed").textContent);
    check("и строки токенов под ней нет",
      $("#feed").querySelector(".card-usage") === null,
      String($("#feed").querySelector(".card-usage")));
    await settle(160);  // стрим кончился, лента перечитана с сервера
    check("после ответа строка появляется",
      $("#feed").querySelector(".usage-tokens") !== null &&
        $("#feed").querySelector(".usage-tokens").textContent ===
          "вход 40 · выход 8 · всего 48 · $0.000020",
      String($("#feed").querySelector(".usage-tokens")));
  }
}

// ── сам стенд ──
//
// Стенд теперь единственная страховка от целого класса ошибок, и его
// собственное поведение закреплено так же, как поведение клиента. Стенд,
// тихо расходящийся с браузером, — тот же капкан, из-за которого проверка
// трижды смотрела не туда: утверждение зелёное, а в браузере сломано.
// Где повторить браузер дёшево — повторяем; где нельзя — падаем с текстом.

function stubChecks() {
  const dom = require(path.join(__dirname, "dom.js"));
  const fresh = () => {
    const env = dom.boot(HTML, { chats: [{ label: "чат" }] });
    return { env, doc: env.document, make: (tag) => env.document.createElement(tag) };
  };
  const throws = (body, pattern) => {
    try {
      body();
      return false;
    } catch (err) {
      return pattern.test(err.message);
    }
  };

  // 1. Высота растёт от содержимого, а присвоить её нельзя.
  {
    const { make } = fresh();
    const box = make("div");
    check("пустой узел не имеет высоты", box.scrollHeight === 0, String(box.scrollHeight));
    box.appendChild(make("div"));
    const one = box.scrollHeight;
    box.appendChild(make("div"));
    check("высота растёт от содержимого", box.scrollHeight > one, `${one} → ${box.scrollHeight}`);
    check(
      "присвоить высоту нельзя — стенд падает с объяснением",
      throws(() => { box.scrollHeight = 4000; }, /не присваивают/),
      "присвоение прошло молча"
    );
    box.clientHeight = 1000;
    check("высота не меньше экрана", box.scrollHeight >= 1000, String(box.scrollHeight));
  }

  // 2. Прокрутка зажата, как в браузере.
  {
    const { make } = fresh();
    const box = make("div");
    for (let i = 0; i < 10; i += 1) box.appendChild(make("div"));
    box.clientHeight = 100;
    const limit = box.scrollHeight - box.clientHeight;
    box.scrollTop = 1e6;
    check("вниз дальше края не уедешь", box.scrollTop === limit, `${box.scrollTop} при пределе ${limit}`);
    box.scrollTop = -50;
    check("вверх за ноль тоже", box.scrollTop === 0, String(box.scrollTop));
    box.scrollTop = 40;
    check("обычное значение принимается как есть", box.scrollTop === 40, String(box.scrollTop));
  }

  // 3. Составной селектор либо работает, либо падает — но не молчит.
  {
    const { doc, make } = fresh();
    const box = make("div");
    const btn = make("button");
    btn.className = "mini danger";
    btn.id = "цель";
    box.appendChild(btn);
    check("тег с классами находится", box.querySelector("button.mini.danger") === btn);
    check("класс с id находится", box.querySelector("#цель.mini") === btn);
    check("несовпадение по одному из классов — не находится", box.querySelector("button.mini.нет") === null);
    for (const bad of [".a .b", ".a > .b", ".a, .b", "[data-x]", "div:first-child", "*"]) {
      check(
        `селектор «${bad}» роняет стенд, а не отдаёт пустоту`,
        throws(() => box.querySelector(bad), /не умеет селектор|пустой селектор/),
        "вернул пустоту молча"
      );
    }
    check("документ ведёт себя так же", throws(() => doc.querySelector(".a .b"), /не умеет селектор/));
  }

  // 4. textContent видит то, что положили через innerHTML.
  {
    const { make } = fresh();
    const box = make("div");
    box.innerHTML = "<p>первый</p><p>второй</p>";
    check("текст читается сквозь разметку", box.textContent === "первыйвторой", box.textContent);
    box.innerHTML = "&lt;script&gt; &amp; кавычка &quot;";
    check("сущности разворачиваются", box.textContent === '<script> & кавычка "', box.textContent);
    box.innerHTML = "";
    check("после очистки текст пуст", box.textContent === "", box.textContent);
  }

  // 5. Событие всплывает до документа.
  {
    const { doc, make } = fresh();
    const deep = make("span");
    const middle = make("div");
    middle.appendChild(deep);
    doc.body.appendChild(middle);
    const seen = [];
    doc.addEventListener("click", (ev) => seen.push(ev.target === deep ? "документ" : "не тот target"));
    deep.dispatchEvent(new dom.Evt("click"));
    check("событие с глубокого узла доходит до документа", seen.join() === "документ", seen.join());

    const stopped = [];
    doc.addEventListener("keydown", () => stopped.push("документ"));
    const input = make("input");
    doc.body.appendChild(input);
    input.addEventListener("keydown", (ev) => ev.stopPropagation());
    input.dispatchEvent(new dom.Evt("keydown"));
    check("остановленное событие до документа не доходит", stopped.length === 0, stopped.join());
  }

  // 6. Потеря фокуса шлёт change — и только если значение поменялось.
  {
    const { make } = fresh();
    const field = make("textarea");
    const events = [];
    field.addEventListener("blur", () => events.push("blur"));
    field.addEventListener("change", () => events.push("change"));

    field.focus();
    field.blur();
    check("без правки change не шлётся", events.join() === "blur", events.join());

    events.length = 0;
    field.focus();
    field.value = "новое";
    field.blur();
    check("после правки идут blur и change, в этом порядке", events.join() === "blur,change", events.join());
  }

  // 7. Обработчики идут в порядке подписки, инлайновый — не исключение.
  {
    const { make } = fresh();
    const box = make("div");
    const order = [];
    box.onclick = () => order.push("инлайн");
    box.addEventListener("click", () => order.push("подписка"));
    box.dispatchEvent(new dom.Evt("click"));
    check("инлайновый раньше, если назначен раньше", order.join() === "инлайн,подписка", order.join());

    const other = make("div");
    const second = [];
    other.addEventListener("click", () => second.push("подписка"));
    other.onclick = () => second.push("инлайн");
    other.dispatchEvent(new dom.Evt("click"));
    check("и позже, если назначен позже", second.join() === "подписка,инлайн", second.join());

    const third = make("div");
    const replaced = [];
    third.onclick = () => replaced.push("первый");
    third.onclick = () => replaced.push("второй");
    third.dispatchEvent(new dom.Evt("click"));
    check("инлайновый один: переприсвоение заменяет", replaced.join() === "второй", replaced.join());
  }

  // 9. <select> с чужим значением теряет выбор, а не держит старое.
  {
    const { make } = fresh();
    const box = make("select");
    ["первая/модель", "вторая/модель"].forEach((id) => {
      const opt = make("option");
      opt.value = id;
      box.appendChild(opt);
    });
    check("без выбора показывает первую опцию", box.value === "первая/модель", box.value);
    box.value = "вторая/модель";
    check("существующая опция выбирается", box.value === "вторая/модель", box.value);
    box.value = "нет/такой/модели";
    check("значения, которого нет в списке, не остаётся — выбор снят",
      box.value === "", JSON.stringify(box.value));
    check("и selectedIndex это подтверждает", box.selectedIndex === -1, String(box.selectedIndex));
    box.value = "первая/модель";
    check("после снятия выбор снова назначается", box.value === "первая/модель", box.value);
    box.value = "";
    box.innerHTML = "";
    const opt = make("option");
    opt.value = "третья/модель";
    box.appendChild(opt);
    check("пересобранный список снова начинается с первой опции",
      box.value === "третья/модель", box.value);
  }

  // 8. Подмена содержимого обнуляет прокрутку — то, ради чего всё затевалось.
  {
    const { make } = fresh();
    const box = make("div");
    for (let i = 0; i < 10; i += 1) box.appendChild(make("div"));
    box.clientHeight = 100;
    box.scrollTop = 60;
    box.innerHTML = "";
    check("после подмены содержимого прокрутка в нуле", box.scrollTop === 0, String(box.scrollTop));
  }
}

// ── итог ──

try {
  stubChecks();
} catch (err) {
  failures.push("проверка стенда упала: " + (err && err.stack));
}

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
