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
const { renderMarkdown, layoutFor, escapeAction, readStopLines, parseResponseFormat } = app;

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
