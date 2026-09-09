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

// ── закреплённый провайдер: настройка, которую панель не правит ──

{
  const model = { id: "openai/gpt-4o-mini", supported_parameters: ["temperature"], temperature_capped: false };
  const pinned = paramWarnings(model, {}, { provider: { order: ["openai"], allow_fallbacks: false } });
  check("закреплённый провайдер предупреждает", pinned.length === 1, JSON.stringify(pinned));
  check("в предупреждении назван провайдер", /openai/.test(pinned[0] || ""), pinned[0]);
  check("сказано, чем кончится смена модели", /404/.test(pinned[0] || ""), pinned[0]);

  const noFallback = paramWarnings(model, {}, { provider: { allow_fallbacks: false } });
  check("запрет фолбэка предупреждает отдельно", noFallback.length === 1, JSON.stringify(noFallback));
  check("и говорит именно про фолбэк", /фолбэк/.test(noFallback[0] || ""), noFallback[0]);

  check("без extra_body про провайдера молчим", paramWarnings(model, {}, {}).length === 0);
  check("без extra_body вовсе — тоже молчим", paramWarnings(model, {}).length === 0);
  check(
    "про провайдера говорим, даже если модели нет в каталоге",
    paramWarnings(null, {}, { provider: { order: ["openai"] } }).length === 1
  );
  check(
    "закреплённый провайдер и незаявленный параметр — два повода",
    paramWarnings(model, { top_k: 40 }, { provider: { order: ["openai"] } }).length === 2
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

// ── итог ──

if (failures.length) {
  console.error(`ПРОВАЛЕНО ${failures.length} из ${passed + failures.length}:`);
  failures.forEach((f) => console.error("  - " + f));
  process.exit(1);
}
console.log(`ОК: ${passed} утверждений о клиенте`);
