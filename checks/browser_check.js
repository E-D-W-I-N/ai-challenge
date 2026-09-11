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
// Само слово onclick в экранированном тексте безвредно: проверяем, что
// тега не получилось, а не что подстроки нет.
hasNot("обработчик в тексте не оживает", '<div onclick="alert(1)">клик</div>', "<div");
has("амперсанд экранируется", "Тим & Стас", "&amp;");
has("кавычка экранируется", 'он сказал "да"', "&quot;");
has("html внутри жирного остаётся текстом", "**<b>жирно</b>**", "<strong>&lt;b&gt;");
hasNot("html внутри блока кода экранируется", "```\n<script>x</script>\n```", "<script");
hasNot("html внутри inline-кода экранируется", "`<script>x</script>`", "<script");
hasNot("html в заголовке экранируется", "# <img src=x>", "<img");

// ── ссылки: только http(s), и никаких схем-исполнителей ──

hasNot("javascript: в ссылку не превращается", "[клик](javascript:alert(1))", "<a ");
hasNot("data: в ссылку не превращается", "[клик](data:text/html,<script>x</script>)", "<a ");
has("https-ссылка становится ссылкой", "[док](https://example.com/a)", '<a href="https://example.com/a"');
has("у ссылки есть rel", "[док](https://example.com)", 'rel="noreferrer noopener"');
has("у ссылки есть target", "[док](https://example.com)", 'target="_blank"');
has("подпись ссылки на месте", "[док](https://example.com)", ">док</a>");

// ── разметка разбирается ──

has("заголовок", "# Заголовок", "<h1>Заголовок</h1>");
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
}

// ── раскладка узкого окна ──

check(
  "узкое окно сворачивает оба борта и не смотрит на сохранённый выбор",
  JSON.stringify(layoutFor(true, { sidebar: "1", panel: "0" })) ===
    JSON.stringify({ sidebar: true, panel: true }),
  JSON.stringify(layoutFor(true, { sidebar: "1", panel: "0" }))
);
check(
  "широкое окно возвращает выбор пользователя",
  JSON.stringify(layoutFor(false, { sidebar: "1", panel: "0" })) ===
    JSON.stringify({ sidebar: true, panel: false }),
  JSON.stringify(layoutFor(false, { sidebar: "1", panel: "0" }))
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
  check("в предупреждении названы модель и все незаявленные",
    /top_k, min_p/.test(unsupported[0] || "") && /gpt-4o-mini/.test(unsupported[0] || ""),
    unsupported[0]);

  check(
    "заявленные параметры молчат",
    paramWarnings(plain, { temperature: 0.5, max_tokens: 100, stop: ["СТОП"] }).length === 0
  );
  check(
    "незаданные параметры не считаются незаявленными",
    paramWarnings(plain, { temperature: null, top_k: null, min_p: undefined }).length === 0
  );
  check(
    "stop и response_format проверяются наравне с числами",
    paramWarnings(
      { id: "m", supported_parameters: ["temperature"], temperature_capped: false },
      { stop: ["КОНЕЦ"], response_format: { type: "json_object" } }
    ).length === 1
  );

  // Главная ловушка: модель заявляет temperature и всё равно вернёт 400.
  const hot = paramWarnings(capped, { temperature: 1.2 });
  check("обрезанная температура предупреждает, хотя параметр заявлен", hot.length === 1, JSON.stringify(hot));
  check("в предупреждении назван потолок", /1\.0/.test(hot[0] || ""), hot[0]);
  check("температура под потолком молчит", paramWarnings(capped, { temperature: 1.0 }).length === 0);

  // Не на чем основать — не пугаем.
  check("модель не найдена в каталоге — молчим", paramWarnings(null, { top_k: 40 }).length === 0);
  check("модель не отдала supported_parameters — молчим",
    paramWarnings({ id: "m", supported_parameters: [] }, { top_k: 40 }).length === 0);
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
  check("названы поставщик и обе модели, без имён полей конфига",
    /openai/.test(changed[0]) && /вторая\/модель/.test(changed[0]) &&
      /первая\/модель/.test(changed[0]) && !/provider|extra_body|order|404/.test(changed[0]),
    changed[0]);
  check(
    "чат без привязки молчит и на смене модели",
    paramWarnings(model, { model: "вторая/модель" }, {}, "первая/модель").length === 0
  );
}

// ── что закрывает Escape ──

check("открытый диалог важнее ящиков", escapeAction(true, true, 2) === "dialog",
  escapeAction(true, true, 2));
check("без диалога Escape закрывает ящики узкого окна", escapeAction(false, true, 1) === "drawers",
  escapeAction(false, true, 1));
check("на широком окне Escape не трогает борта", escapeAction(false, false, 2) === null);

// ── стоп-строки и формат ответа из панели ──

check(
  "стоп-строки читаются по одной в строке, пустые не в счёт",
  JSON.stringify(readStopLines(" КОНЕЦ \n\n СТОП ")) === JSON.stringify(["КОНЕЦ", "СТОП"]),
  JSON.stringify(readStopLines(" КОНЕЦ \n\n СТОП "))
);
check("пустое поле — параметр не отправляется", readStopLines("   ") === null);

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
    check(
      `панель → запрос без события change: ${key}`,
      sent && JSON.stringify(sent.config[key]) === JSON.stringify(expected),
      sent ? `ушло ${JSON.stringify(sent.config[key])}, ждали ${JSON.stringify(expected)}`
           : "сообщение не ушло вовсе"
    );
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
  // Серверная половина — check_whole_history в run_checks.py: лишние поля → 400.
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
    card.querySelectorAll(".icon-btn")[2].dispatchEvent(new Evt("click"));
    await settle(120);
    const repeated = server.state.requests.filter((r) => r.path.endsWith("/regenerate"));
    check(
      "перегенерация не шлёт тела вовсе: вопрос помнит агент",
      repeated.length === 1 && repeated[0].body === null,
      JSON.stringify(repeated.map((r) => r.body))
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
    const refresh = card.querySelectorAll(".icon-btn")[2];
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

    check("клиент заводит один чат сам и сразу его открывает",
      server.state.agents.length === 1 && Boolean(client.state.current),
      String(server.state.agents.length));
    check("имя у него по умолчанию", /^Новый чат \d+$/.test(client.state.current.label),
      client.state.current.label);
    check("и он пуст: ни переписки, ни черновика, ни промпта за пользователя",
      client.state.current.transcript.length === 0 && $("#input").value === "" &&
        $("#f-system").value === "",
      JSON.stringify([client.state.current.transcript, $("#input").value]));

    // Удалили единственный чат — появился свежий, а не пустой экран.
    const before = client.state.current.id;
    const row = $("#agent-list").querySelectorAll(".item")[0];
    row.querySelectorAll(".mini")[1].dispatchEvent(new Evt("click"));
    const dialog = $(".confirm");
    check("удаление спрашивает подтверждение", Boolean(dialog), "диалога нет");
    const buttons = dialog.querySelectorAll(".primary");
    buttons[0].dispatchEvent(new Evt("click"));
    await settle(80);

    check("после удаления последнего чата появляется свежий, с новым номером",
      server.state.agents.length === 1 && client.state.current.id !== before &&
        client.state.current.label === "Новый чат 2",
      JSON.stringify({ живых: server.state.agents.length, было: before, стало: client.state.current && client.state.current.label }));
  }

  // ── «Новый чат» встаёт в конец списка ──
  // Серверная половина — check_chat_list в run_checks.py.
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
    check("новый чат встаёт последним, а порядок прежних не тронут",
      names().length === 3 && /^Новый чат/.test(names()[2]) &&
        names()[0] === "первый чат" && names()[1] === "второй чат", names().join(" | "));
    check("и он же открыт",
      client.state.current && client.state.current.label === names()[2],
      client.state.current && client.state.current.label);
    check("на сервере тот же порядок",
      server.state.agents.map((a) => a.label).join(" | ") === names().join(" | "),
      server.state.agents.map((a) => a.label).join(" | "));
  }

  // ── лента рисуется из стенограммы ──
  //
  // Клиент после каждого обмена перечитывает агента и перерисовывает ленту
  // целиком: на экране должно быть ровно то, что у агента в истории, а не то,
  // что клиент дорисовал по дороге. Значит формат стенограммы — часть
  // поведения. Серверная половина — check_transcript в run_checks.py.
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
        metrics: {
          model: "особая/модель", provider: "поставщик",
          first_token_ms: 1500, ttft_ms: 4000,
        },
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

    check("текст вопроса показан", nodes[0].textContent === "мой вопрос", nodes[0].textContent);
    check("текст ответа показан и разобран как markdown",
      nodes[1].querySelector(".card-body").innerHTML.includes("<strong>жирный</strong>"),
      nodes[1].querySelector(".card-body").innerHTML);
    check("имя модели и провайдер берутся из метрик реплики, а не из конфига чата",
      nodes[1].querySelector(".card-model").textContent === "особая/модель" &&
        nodes[1].querySelector(".card-tag").textContent === "поставщик",
      nodes[1].querySelector(".card-model").textContent);
    check("рассуждение показано свёрнутым блоком",
      Boolean(nodes[1].querySelector(".think")) &&
        nodes[1].querySelector(".think-body").textContent === "я подумал",
      String(nodes[1].querySelector(".think")));
    check("у целого ответа блока ошибки нет",
      nodes[1].querySelector(".card-error") === null, "блок ошибки появился");

    check("оборванный ответ помечен, и текст ошибки показан",
      nodes[3].classList.contains("failed") && nodes[3].querySelector(".card-error") &&
        nodes[3].querySelector(".card-error").textContent === "оборвалось",
      nodes[3].className);

    // Плитки справа рисуются из метрик последнего ответа. «Первый токен»
    // показывает момент, когда модель заговорила вообще, а не когда пошёл
    // ответ: на думающей модели это разные числа, и ttft включал бы
    // в себя всё размышление.
    const tile = (label) =>
      $("#tiles").querySelectorAll(".tile")
        .find((t) => t.querySelector(".tile-k").textContent === label);
    const first = tile("Первый токен, с");
    check("плитка «Первый токен» показывает первый токен вообще, а не начало ответа",
      first && first.querySelector(".tile-v").textContent === "1.50",
      first && first.querySelector(".tile-v").textContent);
    check("насколько ответ отстал от рассуждения — видно подписью",
      first && first.querySelector(".tile-sub") &&
        first.querySelector(".tile-sub").textContent === "ответ +2.50 с",
      first && String(first.querySelector(".tile-sub")));

    // Пустая стенограмма — это заставка, а не пустая лента с нулём узлов.
    $("#agent-list").querySelectorAll(".item-open")[0].dispatchEvent(new Evt("click"));
    await settle(40);
    check("пустой чат показывает заставку, а не пустоту",
      Boolean($("#feed").querySelector(".empty")), $("#feed").textContent);
  }

  // ── переименование чата в списке слева ──
  //
  // Настоящий маршрут: клик по кнопке, клавиша, запрос к серверу, имя
  // в списке. Греп по исходнику здесь описывал бы реализацию — переименование
  // ломается, не тронув ни одной из тех строк.
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

    // 1. Кнопка открывает поле прямо в строке, со старым именем внутри.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    let field = row().querySelector(".item-rename");
    check("кнопка открывает в строке поле с нынешним именем",
      field && field.value === "первый чат", field && field.value);
    check("пока переименовываем, кнопки открытия чата в строке нет",
      row().querySelector(".item-open") === null, "кнопка осталась");

    // 2. Enter сохраняет: правка уходит на сервер и видна в списке.
    field.value = "новое имя";
    field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    await settle(40);
    check("Enter отправляет новое имя на сервер",
      patches().length === 1 && patches()[0].body.label === "новое имя" &&
        server.state.agents[0].label === "новое имя",
      JSON.stringify(patches().map((r) => r.body)));
    check("список показывает новое имя, а поле закрылось",
      title() && title().textContent === "новое имя" &&
        row().querySelector(".item-rename") === null,
      title() && title().textContent);

    // 3. Escape отменяет: ни запроса, ни следа в списке.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    field = row().querySelector(".item-rename");
    field.value = "передумал";
    field.dispatchEvent(new Evt("keydown", { key: "Escape" }));
    await settle(40);
    check("Escape не шлёт запроса и оставляет прежнее имя",
      patches().length === 1 && title() && title().textContent === "новое имя",
      JSON.stringify(patches().map((r) => r.body)));

    // 4. Потеря фокуса сохраняет: имя не должно теряться молча.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    field = row().querySelector(".item-rename");
    field.focus();
    field.value = "по потере фокуса";
    field.blur();
    await settle(40);
    check("потеря фокуса тоже сохраняет, и список это показывает",
      patches().length === 2 && patches()[1].body.label === "по потере фокуса" &&
        title() && title().textContent === "по потере фокуса",
      JSON.stringify(patches().map((r) => r.body)));

    // 5. Пустое имя чат не стирает: запроса нет, имя прежнее.
    row().querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    field = row().querySelector(".item-rename");
    field.value = "   ";
    field.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    await settle(40);
    check("пустым именем чат не переименовать: ни запроса, ни следа в списке",
      patches().length === 2 && title() && title().textContent === "по потере фокуса",
      JSON.stringify(patches().map((r) => r.body)));
  }

  // ── где оказывается лента ──
  //
  // Утверждения здесь про **положение ленты**, а не про внутренний флаг:
  // флаг ведёт себя как задумано и при ленте, оставшейся в нуле, — содержимое
  // пересоздаётся, и браузер обнуляет прокрутку.
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
    }

    // 3. Отмотал вверх во время ответа — лента остаётся там, где её оставили.
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
    }

    // 4. Прижатая лента доматывается сама: обмен затевает сам читатель.
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
  // Жалоба заказчика: строка появлялась на каждое сообщение. Причина в том,
  // что пролив панели случается перед каждой отправкой, а сообщение
  // показывалось по факту пролива. Проливать надо всегда — сообщать не о чем.
  // Утверждения ниже про то, что видно на экране.
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

    // ...и перегенерация тоже: правок не было и здесь.
    const card = $("#feed").querySelector(".card");
    card.querySelectorAll(".icon-btn")[2].dispatchEvent(new Evt("click"));
    await settle(140);
    check("перегенерация без правок тоже молчит", shown() === "", shown());

    // 2. Правка есть — сообщение появляется.
    $("#f-system").value = "ПРАВКА";
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("после настоящей правки сообщение есть", /Применено/.test(shown()), shown());

    // 3. Правка, а сразу за ней отправка: сообщение остаётся тем же самым,
    //    а не появляется вторым — таймер у него один, назначенный правкой.
    const timer = client.state.statusTimer;
    $("#input").value = "второй";
    $("#composer").requestSubmit();
    await settle(120);
    check("отправка сразу после правки не показывает второе сообщение",
      /Применено/.test(shown()) && client.state.statusTimer === timer,
      `${shown()} | таймер ${client.state.statusTimer === timer ? "тот же" : "новый"}`);

    // 4. Вторая правка перевешивает таймер, а не копит второй, и через пять
    //    секунд после последней правки строка гаснет сама.
    $("#f-system").value = "ВТОРАЯ ПРАВКА";
    $("#f-system").dispatchEvent(new Evt("change"));
    await settle(40);
    check("вторая правка перевешивает таймер, а не копит второй",
      /Применено/.test(shown()) && Boolean(client.state.statusTimer) &&
        client.state.statusTimer !== timer,
      `${shown()} | ${timer === client.state.statusTimer ? "тот же таймер" : "новый таймер"}`);
    await new Promise((r) => setTimeout(r, 5100));
    check("и через пять секунд после последней правки строка пуста", shown() === "", shown());
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
    check("а говорит, что не так, и делает это красным",
      shown().length > 0 && /error/.test($("#save-status").className),
      `${shown()} | ${$("#save-status").className}`);
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
