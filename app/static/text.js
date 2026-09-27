// Чистые функции: без DOM, состояния чата и запросов.
(function (root) {
"use strict";

// ─────────────────────────── формат ───────────────────────────

const fmt = {
  clock: (epoch) => {
    const date = new Date(epoch * 1000);
    return [date.getHours(), date.getMinutes(), date.getSeconds()]
      .map((value) => String(value).padStart(2, "0")).join(":");
  },
  sec: (ms) => (ms === null || ms === undefined ? "—" : (ms / 1000).toFixed(2)),
  rate: (v) => (v ? v.toFixed(1) : "—"),
  cost: (v) => (v === null || v === undefined ? "—" : "$" + Number(v).toFixed(6)),
  pct: (v) => (v === null || v === undefined ? "—" : v.toFixed(1) + " %"),
  // Токены: тысячи разделяются, от десяти тысяч — «12.4k». Прочерк остаётся
  // прочерком: ноль — это ответ, а «неизвестно» — его отсутствие, и на экране
  // они обязаны выглядеть по-разному.
  tokens: (v) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (!isFinite(n)) return "—";
    // Порог для «M» стоит там, где округление в «k» уже дало бы «1000k»:
    // такое число читается как миллион, миллионом его и пишем.
    if (Math.abs(n) >= 999500) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (Math.abs(n) >= 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  },
};


function has(value) {
  return value !== null && value !== undefined;
}


// ─────────────────────────── markdown ─────────────────────────

// Свой разбор: заголовки, списки, цитаты, код, жирный, курсив, ссылки.
// Текст модели сначала экранируется целиком, поэтому разметка из ответа
// не может стать разметкой страницы.
function escapeHtml(text) {
  return String(text)
    // Меткой inline-кода служит \u0000: пришли модель его в тексте, разбор
    // подставил бы на его место чужой кусок. Выбрасываем до всего остального.
    .replace(/\u0000/g, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Метка, которой на время разбора подменяется inline-код: внутри кода
// разметка не разбирается.
const CODE_MARK = "\u0000";

function inlineMarkdown(text) {
  let out = escapeHtml(text);
  const codes = [];
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return CODE_MARK + (codes.length - 1) + CODE_MARK;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  // Ссылка только на http(s): javascript: в href из ответа модели недопустим.
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" rel="noreferrer noopener" target="_blank">$1</a>'
  );
  out = out.replace(
    new RegExp(CODE_MARK + "(\\d+)" + CODE_MARK, "g"),
    (_, i) => "<code>" + (codes[Number(i)] || "") + "</code>"
  );
  return out;
}

function renderMarkdown(text) {
  const lines = String(text || "").split("\n");
  const html = [];
  let list = null;        // "ul" | "ol" | null
  let paragraph = [];
  let code = null;        // накопитель строк внутри ```

  const closeParagraph = () => {
    if (paragraph.length) {
      html.push("<p>" + inlineMarkdown(paragraph.join("\n")) + "</p>");
      paragraph = [];
    }
  };
  const closeList = () => {
    if (list) { html.push("</" + list + ">"); list = null; }
  };
  const openList = (kind) => {
    if (list !== kind) { closeList(); html.push("<" + kind + ">"); list = kind; }
  };
  const pushCode = () => html.push("<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>");

  for (const raw of lines) {
    if (code !== null) {
      if (/^\s*```/.test(raw)) { pushCode(); code = null; } else { code.push(raw); }
      continue;
    }
    if (/^\s*```/.test(raw)) { closeParagraph(); closeList(); code = []; continue; }

    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { closeParagraph(); closeList(); continue; }

    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      closeParagraph(); closeList();
      const level = heading[1].length;
      html.push("<h" + level + ">" + inlineMarkdown(heading[2]) + "</h" + level + ">");
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      closeParagraph(); closeList(); html.push("<hr>"); continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      closeParagraph(); closeList();
      html.push("<blockquote>" + inlineMarkdown(quote[1]) + "</blockquote>");
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      closeParagraph(); openList("ul");
      html.push("<li>" + inlineMarkdown(bullet[1]) + "</li>");
      continue;
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ordered) {
      closeParagraph(); openList("ol");
      html.push("<li>" + inlineMarkdown(ordered[1]) + "</li>");
      continue;
    }
    closeList();
    paragraph.push(line);
  }
  if (code !== null) pushCode();
  closeParagraph();
  closeList();
  return html.join("");
}


const NUMBER_FIELDS = [
  "temperature", "max_tokens", "top_p", "top_k", "min_p",
  "repetition_penalty", "presence_penalty", "frequency_penalty",
];


// Стоп-строки: по одной в строке, пустые не в счёт. Отдельной функцией
// без DOM — разбор проверяется без браузера.
function readStopLines(text) {
  const lines = String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  return lines.length ? lines : null;
}

// Формат ответа: частый случай выбирается из списка, редкий пишется JSON.
// Кривой JSON — понятная ошибка, а не молчаливая отправка мусора провайдеру.
function parseResponseFormat(kind, raw) {
  if (!kind) return null;
  if (kind === "json_object") return { type: "json_object" };
  const text = String(raw || "").trim();
  if (!text) return null;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    throw new Error("формат ответа: это не JSON — " + e.message);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("формат ответа: нужен объект JSON");
  }
  return parsed;
}

// Параметры панели в терминах OpenRouter. Наши поля — `system` и `model` —
// параметрами не уходят и по `supported_parameters` не проверяются.
const PROVIDER_PARAMS = [...NUMBER_FIELDS, "stop", "response_format"];

// Чем заданные параметры не сойдутся с выбранной моделью. Предупреждать надо
// **до** отправки: на каждом вызове стоит provider.require_parameters=true,
// и параметр, которого модель не заявляет, выкашивает провайдеров — вместо
// ответа придёт ошибка, по которой не понять, что виноват один переключатель.
// Отдельной функцией без DOM — решение проверяется без браузера.
function paramWarnings(model, settings, extraBody, baseModel) {
  const warnings = [];

  // Чат, привязанный к одному поставщику, на чужой модели ответа не получит.
  // Говорим об этом ровно в тот момент, когда модель меняют: постоянная
  // надпись про настройку, которой не видно, только сбивает с толку.
  const pinned = ((extraBody || {}).provider || {}).order;
  if (Array.isArray(pinned) && pinned.length && baseModel && settings.model !== baseModel) {
    warnings.push(
      `Этот чат привязан к одному поставщику моделей — ${pinned.join(", ")}. ` +
        `Если у него нет «${settings.model}», ответа не будет: вернётся ошибка. ` +
        `Раньше здесь стояла «${baseModel}».`
    );
  }

  // Каталог не загрузился или модель в нём не нашлась — про параметры молчим:
  // пугать предупреждением, которого не на чем основать, хуже.
  if (!model) return warnings;

  const declared = model.supported_parameters || [];
  if (declared.length) {
    const missing = PROVIDER_PARAMS.filter(
      (name) => settings[name] !== null && settings[name] !== undefined && !declared.includes(name)
    );
    if (missing.length) {
      warnings.push(
        `«${model.id}» не заявляет ${missing.join(", ")}. ` +
          "Запрос уходит с provider.require_parameters, поэтому подходящего " +
          "провайдера может не найтись — вместо ответа придёт ошибка."
      );
    }
  }

  const cap = model.temperature_cap;
  if (
    model.temperature_capped &&
    settings.temperature !== null &&
    settings.temperature !== undefined &&
    cap !== null &&
    cap !== undefined &&
    settings.temperature > cap
  ) {
    warnings.push(
      `«${model.id}» обрезает temperature на ${cap.toFixed(1)}: ` +
        `на ${settings.temperature} ` +
        "запрос вернётся с ошибкой, хотя temperature эта модель и заявляет."
    );
  }
  return warnings;
}


// Unset остаётся unset; порядок ключей JSON не влияет на сравнение конфига.
function sameValue(a, b) {
  const empty = (v) => v === null || v === undefined;
  if (empty(a) || empty(b)) return empty(a) && empty(b);
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((item, i) => sameValue(item, b[i]));
  }
  if (typeof a === "object" || typeof b === "object") {
    if (typeof a !== "object" || typeof b !== "object") return false;
    const keys = Object.keys(a);
    if (keys.length !== Object.keys(b).length) return false;
    return keys.every((k) => Object.prototype.hasOwnProperty.call(b, k) && sameValue(a[k], b[k]));
  }
  return a === b;
}

const helpers = { fmt, has, escapeHtml, renderMarkdown, NUMBER_FIELDS, readStopLines, parseResponseFormat, paramWarnings, sameValue };
if (typeof module !== "undefined") module.exports = helpers;
else root.ChatText = helpers;
})(globalThis);
