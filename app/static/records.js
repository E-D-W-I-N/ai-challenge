// Редактируемые слои и MCP: один домен, общий state передаёт app.js.
(function (root) {
"use strict";

function createChatRecords({ state, $, el, iconButton, api, json, fmt, onMcpChange }) {
// ────────────────────── вкладка «Память» ──────────────────────

// Три слоя памяти агента — по разделу на каждый, в порядке от короткого
// к долгому. Данные приходят одним ответом `GET /api/agents/{id}/memory`:
// `short_term` — счётчик сообщений этого чата, `working` — записи о состоянии
// задачи, `long_term` — общий на всю базу список.
//
// Ответ лежит в `state.memory`, и отрисовка берёт всё оттуда: **запрос идёт
// на открытие вкладки, а не на отрисовку**. Панель перерисовывается на каждый
// обмен и на каждую правку конфига, а слои столько раз не меняются — запрос
// внутри отрисовки превратил бы один поход на сервер в поток.
//
// Перечитывается память там, где она правда изменилась: при открытии другого
// чата (первые два слоя — его собственные) и после обмена (история выросла,
// память обновилась). И то и другое — события, а не отрисовки, и оба молчат,
// пока вкладка закрыта.

// Типы записей: токен для сервера и русская подпись. Одна карта на список
// записей и на дропдаун формы — его опции строятся отсюда же (`fillKinds`).
// Второй таблицей подписи разъехались бы молча: в списке стояло бы одно
// слово, а в форме другое. Слова те же, что в `MEMORY_LABELS` на сервере, —
// ими же память подписана и в промпте.
const MEMORY_KINDS = [
  ["profile", "о собеседнике"],
  ["decision", "решение"],
  ["knowledge", "факт"],
];

const memoryKindLabel = (kind) =>
  (MEMORY_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Типы записей рабочей памяти — состояние задачи. Список свой, а не общий
// с долговременной: слои разные, и «цель» в одном не значит того же, что
// «о собеседнике» в другом. Слова те же, что в `WORKING_LABELS` на сервере, — ими
// же запись подписана и в промпте.
const WORKING_KINDS = [
  ["goal", "цель"],
  ["limit", "ограничение"],
  ["decision", "решение"],
  ["question", "открытый вопрос"],
];

const workingKindLabel = (kind) =>
  (WORKING_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Запись рабочей памяти так, как она уезжает в промпт: «подпись типа:
// содержимое» (`working_lines`, app/agent.py).
const workingLine = (record) => workingKindLabel(record.kind) + ": " + record.content;

// Виды инвариантов: токен для сервера и русская подпись. Карта та же, что
// `INVARIANT_LABELS` на сервере, — ею же инвариант подписан и в промпте.
//
// Ни одно слово не совпадает со словом соседних слоёв: «решение» уже занято
// и долговременной памятью, и рабочей, и назови мы вид инварианта тем же
// словом, утверждение «это запись из слоя инвариантов» стало бы зелёным
// и на чужой записи. Поэтому «техническое решение», а не «решение».
const INVARIANT_KINDS = [
  ["architecture", "архитектура"],
  ["technical", "техническое решение"],
  ["stack", "ограничение стека"],
  ["business", "бизнес-правило"],
];

const invariantKindLabel = (kind) =>
  (INVARIANT_KINDS.find(([token]) => token === kind) || [kind, kind])[1];

// Запрещённые слова: список — на экране, строка через запятую — в поле.
// Разбор и сборка одной парой на форму и на правку: две копии разошлись бы
// на первом же слове с пробелом внутри.
const bannedText = (words) => (words || []).join(", ");
const parseBanned = (text) =>
  String(text || "").split(",").map((w) => w.trim()).filter(Boolean);

// Правка записи прямо в списке: Enter сохраняет, Escape отменяет, потеря
// фокуса — тоже сохраняет. Идиом тот же, что у переименования чата слева:
// второй способ правки на той же странице читался бы как другое действие.
//
// Человек правит текст и тип вместе; форма не выбирает тип за него.
//
// Список типов открывается **пустым**, как и в форме добавления: умолчания
// у типа нет нигде, и «оставить прежний» — это не выбор, а его отсутствие.
// Предвыбери мы здесь нынешний тип, правка текста молча пересылала бы его
// обратно — и запись, которой тип поправили в соседней вкладке, вернулась бы
// к старому.
function startRecordEdit(row, record, kinds, commit, opts) {
  // `opts` — чем этот слой отличается от соседних: есть ли у записи третье
  // поле (`banned`) и чем перерисовывать список после правки. Два слоя
  // памяти отличий не имеют вовсе и зовут функцию без него.
  const withBanned = Boolean(opts && opts.banned);
  const redraw = (opts && opts.redraw) || renderMemory;
  const shown = row.querySelector(".mem-text");
  // Поле и список — в одном блоке: уход фокуса с поля на список это не конец
  // правки, а её продолжение, и различить их можно только на общем родителе.
  const box = el("div", "mem-edit-box");
  const input = el("textarea", "mem-edit control area");
  input.rows = 4;
  input.setAttribute("aria-label", "Текст записи");
  input.value = record.content;
  const kind = el("select", "mem-edit-kind control");
  kind.setAttribute("aria-label", "Тип записи");
  fillKinds(kind, kinds, "— оставить тип —");
  box.append(input, kind);
  // Третье поле — только у инвариантов, у которых оно и есть: слова
  // правятся тем же нажатием, что текст и вид. Второй функции правки
  // на третий слой не заводим — правила у них одни, и вторая копия
  // разошлась бы с первой на первом же исправлении.
  let banned = null;
  if (withBanned) {
    banned = el("input", "mem-edit-banned control");
    banned.value = bannedText(record.banned);
    banned.title = "Запрещённые слова через запятую";
    banned.setAttribute("aria-label", banned.title);
    box.appendChild(banned);
  }
  row.replaceChild(box, shown);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const text = (input.value || "").trim();
    const patch = {};
    // Пустой текст — не правка, а потеря записи: удаление здесь рядом,
    // и делать его вслепую очисткой поля нельзя. Текст слово в слово прежний
    // и невыбранный тип тоже не едут: ручке нечего было бы делать.
    if (text && text !== record.content) patch.content = text;
    if (kind.value && kind.value !== record.kind) patch.kind = kind.value;
    // Слова — тем же правилом: уезжают только тронутые. Пустое поле здесь
    // законно и значит «ничего не запрещено», в отличие от пустого текста:
    // сторожить нечего — не то же самое, что записывать нечего.
    if (banned) {
      const words = parseBanned(banned.value);
      if (words.join("\u0000") !== (record.banned || []).join("\u0000")) patch.banned = words;
    }
    if (save && Object.keys(patch).length && await commit(patch) === false) {
      settled = false;
      return;
    }
    box.remove();
    redraw();
  };

  const keys = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); finish(true); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); finish(false); }
  };
  input.onkeydown = keys;
  kind.onkeydown = keys;
  if (banned) banned.onkeydown = keys;
  // `focusout` всплывает, `blur` — нет: слушаем блок и смотрим, куда фокус
  // ушёл. Остался внутри — правка продолжается; ушёл наружу — сохраняем,
  // ровно как раньше сохранял уход фокуса с поля. Вешай мы это на само поле,
  // щелчок по списку типов убрал бы список прямо из-под курсора.
  box.onfocusout = (ev) => {
    if (!box.contains(ev.relatedTarget)) finish(true);
  };
}

function memoryTabOpen() {
  return state.workspace === "settings" && state.section === "memory";
}

// Открытие вкладки — единственное место, откуда слои запрашиваются впервые.
let memoryRequest = 0;
async function loadMemory(force = false) {
  const app = state.settingsScope === "app", scope = state.settingsScope;
  const container = $(app ? "#mem-long" : "#mem-working");
  if (container.querySelector(".mem-edit-box")) return;
  const id = app ? null : state.current?.id, ticket = ++memoryRequest;
  if (!app && !id) { renderMemory(); return; }
  state.memoryNote = "Читаю память…";
  renderMemory();
  const owned = () => ticket === memoryRequest && memoryTabOpen() && state.settingsScope === scope && (app || id === state.current?.id);
  try {
    const answer = await api(app ? "/api/memory" : "/api/agents/" + id + "/memory");
    if (!owned()) return;
    if (app) state.longMemory = answer;
    else state.memory = answer;
    state.memoryNote = "";
  } catch (err) {
    if (!owned()) return;
    if (app) state.longMemory = null; else state.memory = null;
    state.memoryNote = String(err.message || err);
  }
  renderMemory();
}

// Сколько первых реплик не уедет в модель дословно при нынешней стратегии —
// и каким словом это называется.
//
// Расчёт повторяет серверный (`Agent.context_cut` и `summary_cover`): ручка
// отдаёт его **входы** — длину истории и докуда покрывает последняя сводка,
// — а не готовое число. Сводки при этом лежат в краткосрочном разделе:
// сводка не запомненное, а чем заменено то, что не уехало дословно.
//
// Считается по конфигу агента, а не по полям панели: в панели может стоять
// непролитая правка, а раздел говорит о том, что уедет сейчас. Незнакомая
// стратегия читается как «вся история» — ровно как на сервере.
function shortTermCut(agent, layers) {
  const total = layers.short_term.messages;
  const keep = agent && agent.keep_last !== undefined ? agent.keep_last : null;
  const strategy = agent ? agent.strategy : "full";
  // Пустое поле — резать нечем: идиом тот же, что у сервера, `null` это
  // «не делать», а не «делать с нулём».
  const nothing = keep === null || keep === undefined;
  if (strategy === "window") {
    // Окно режет ровно столько, сколько просили: зажима по «докуда дочитала
    // память» больше нет ни здесь, ни на сервере — читать её стало некому,
    // а записи в ней от длины разговора не зависят вовсе.
    return {
      cut: nothing ? 0 : Math.max(0, total - keep),
      word: "отброшено окном",
    };
  }
  if (strategy === "summary") {
    const summaries = layers.short_term.summaries || [];
    const last = summaries.length ? summaries[summaries.length - 1] : null;
    const upto = last && typeof last.upto === "number" ? last.upto : 0;
    return { cut: nothing || upto <= 0 ? 0 : Math.min(upto, total), word: "заменено сводкой" };
  }
  return { cut: 0, word: "" };
}

function memRow(label, value) {
  const row = el("div", "mem-row");
  row.append(el("span", "mem-k", label), el("span", "mem-v", value));
  return row;
}

const memNote = (text) => el("p", "mem-note", text);

// Раздела нет данных — говорим почему: читаем, не открыт чат или ручка
// ответила ошибкой. Пустой раздел молчал бы о разнице между «пусто»
// и «не доехало».
const memBlank = () => memNote(state.memoryNote || "Чат ещё не открыт.");

function renderMemory() {
  if (state.settingsScope === "chat") {
    renderShortTerm($("#mem-short"));
    if (!$("#mem-working").querySelector(".mem-edit-box")) renderWorking($("#mem-working"));
  } else if (state.settingsScope === "app" && !$("#mem-long").querySelector(".mem-edit-box")) renderLongTerm($("#mem-long"));
}

function renderShortTerm(box) {
  box.innerHTML = "";
  const layers = state.memory;
  if (!layers || !layers.short_term) { box.appendChild(memBlank()); return; }
  const total = layers.short_term.messages;
  const cut = shortTermCut(state.current, layers);
  box.append(
    memRow("Сообщений в истории", fmt.tokens(total)),
    memRow("Уезжает дословно", fmt.tokens(total - cut.cut)),
    memNote(cut.cut
      ? "Остальные " + fmt.tokens(cut.cut) + " — " + cut.word + "."
      : "Вся история уезжает в модель дословно.")
  );

  // Сводки — здесь, под историей: сводка не память, а замена той её части,
  // что не уехала дословно. Выключи сворачивание — не пропадёт ничего,
  // история цела и сводка соберётся заново; памятью её делал только сосед
  // по разделу.
  const summaries = layers.short_term.summaries || [];
  if (!summaries.length) return;
  box.appendChild(el("div", "mem-sub", "Сводки"));
  summaries.forEach((item) => {
    const row = el("div", "mem-item column");
    row.append(
      el("div", "mem-text", item.content),
      memNote("вместо первых " + fmt.tokens(item.upto) + " сообщений")
    );
    box.appendChild(row);
  });
}

function renderWorking(box) {
  box.innerHTML = "";
  const working = state.memory && state.memory.working;
  // Форма прячется вместе со слоем: область рабочей памяти — разговор,
  // и пока слоя нет на экране — чат не открыт или ручка ответила отказом, —
  // записывать некуда.
  $("#mem-work-form").classList.toggle("hidden", !working);
  if (!working) { box.appendChild(memBlank()); return; }

  const records = working.records || [];
  const ownerId = state.current && state.current.id;
  if (!records.length) box.appendChild(memNote("Записей нет."));
  records.forEach((record) => {
    const line = workingLine(record);
    const row = el("div", "mem-item");
    row.append(
      el("div", "mem-text", line),
      iconButton("pencil", "Поправить запись",
        () => startRecordEdit(row, record, WORKING_KINDS,
          (patch) => editWorking(record, patch, ownerId)), "mini"),
      iconButton("trash", "Удалить запись", () => dropWorking(record, ownerId), "mini danger")
    );
    box.appendChild(row);
  });

}

function renderLongTerm(box) {
  box.innerHTML = "";
  const long = state.longMemory;
  if (!long) { box.appendChild(memBlank()); return; }
  const records = long.records || [];
  if (!records.length) { box.appendChild(memNote("Записей нет.")); return; }
  records.forEach((record) => {
    const row = el("div", "mem-item");
    row.append(
      el("div", "mem-kind", memoryKindLabel(record.kind)),
      el("div", "mem-text", record.content),
      iconButton("pencil", "Поправить запись",
        () => startRecordEdit(row, record, MEMORY_KINDS,
          (patch) => editMemory(record, patch)), "mini"),
      iconButton("trash", "Забыть запись", () => forget(record), "mini danger")
    );
    box.appendChild(row);
  });
}

function memoryStatus(text, isError) {
  const box = $("#mem-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

function workingStatus(text, isError) {
  const box = $("#mem-work-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

// ── рабочая память правится руками ──
//
// Тем же набором, каким правится долговременная, и по тем же правилам: тип
// обязателен и без умолчания, список пополняется **ответом ручки** (номер
// выдаёт база, текст по дороге чистит `redact()`), перечитывать слой для
// этого незачем. Второй способ на той же странице читался бы как другое
// действие, и слои разъехались бы на первой правке.
const workingUrl = (seq, id = state.current && state.current.id) => {
  return "/api/agents/" + id + "/working" + (seq === undefined ? "" : "/" + seq);
};

async function addWorking(kind, content) {
  const text = (content || "").trim();
  if (!text) {
    workingStatus("Текст записи пуст: записывать нечего.", true);
    return false;
  }
  if (!(state.current && state.current.id)) {
    workingStatus("Чат ещё не открыт: рабочая память живёт в разговоре.", true);
    return false;
  }
  const ownerId = state.current.id;
  try {
    const record = await api(workingUrl(undefined, ownerId), json("POST", { kind, content: text }));
    if (ownerId !== state.current?.id) return false;
    const working = state.memory && state.memory.working;
    if (working) working.records = [...(working.records || []), record];
    renderMemory();
    workingStatus("Записано: " + workingKindLabel(record.kind) + ".");
    return true;
  } catch (err) {
    workingStatus(String(err.message || err), true);
    return false;
  }
}

async function editWorking(record, patch, ownerId) {
  if (ownerId !== (state.current && state.current.id)) return false;
  try {
    const updated = await api(workingUrl(record.seq, ownerId), json("PATCH", patch));
    if (ownerId !== (state.current && state.current.id)) return true;
    const working = state.memory && state.memory.working;
    if (working) {
      working.records = (working.records || [])
        .map((item) => (item.seq === updated.seq ? updated : item));
    }
    workingStatus("Запись поправлена.");
    return true;
  } catch (err) {
    if (ownerId !== (state.current && state.current.id)) return false;
    workingStatus(String(err.message || err), true);
    return false;
  }
}

async function dropWorking(record, ownerId) {
  if (ownerId !== (state.current && state.current.id)) return;
  try {
    await api(workingUrl(record.seq, ownerId), { method: "DELETE" });
  } catch (err) {
    if (ownerId !== (state.current && state.current.id)) return;
    workingStatus(String(err.message || err), true);
    return;
  }
  if (ownerId !== (state.current && state.current.id)) return;
  const working = state.memory && state.memory.working;
  if (working) {
    working.records = (working.records || []).filter((item) => item.seq !== record.seq);
  }
  renderMemory();
  workingStatus("Запись убрана.");
}

// Правка долговременной записи заменяет запись ответом API.
async function editMemory(record, patch) {
  try {
    const updated = await api("/api/memory/" + record.seq, json("PATCH", patch));
    const long = state.longMemory;
    if (long) {
      long.records = (long.records || [])
        .map((item) => (item.seq === updated.seq ? updated : item));
    }
    memoryStatus("Запись поправлена: с этой минуты она ваша.");
    return true;
  } catch (err) {
    memoryStatus(String(err.message || err), true);
    return false;
  }
}

// Новая запись долговременной памяти. Список пополняется **записанным
// ответом**, а не присланным телом: номер выдаёт база, а текст по дороге
// чистит `redact()`. Перечитывать слой целиком для этого незачем.
//
// Дедупликации нет намеренно: второй клик по тому же факту заводит вторую
// запись. Отличить «то же самое» от «похожего» может только человек, и
// удаляется лишняя одной кнопкой.
async function remember(kind, content) {
  const text = (content || "").trim();
  if (!text) {
    memoryStatus("Текст записи пуст: записывать нечего.", true);
    return false;
  }
  try {
    const record = await api("/api/memory", json("POST", { kind, content: text }));
    const long = state.longMemory;
    if (long) long.records = [...(long.records || []), record];
    renderMemory();
    memoryStatus("Запомнено: " + memoryKindLabel(record.kind) + ".");
    return true;
  } catch (err) {
    memoryStatus(String(err.message || err), true);
    return false;
  }
}

async function forget(record) {
  try {
    await api("/api/memory/" + record.seq, { method: "DELETE" });
  } catch (err) {
    memoryStatus(String(err.message || err), true);
    return;
  }
  const long = state.longMemory;
  if (long) long.records = (long.records || []).filter((item) => item.seq !== record.seq);
  renderMemory();
  memoryStatus("Запись забыта.");
}

// Тип записи уезжает тот, что выбран в списке: умолчания у него нет ни здесь,
// ни на сервере — `_kind_field` отказывает и отсутствию ключа тоже.
async function addFromForm() {
  const kind = $("#mem-kind").value;
  // Тип не выбран — не шлём вовсе: ручка ответит 400, и незачем спрашивать
  // сервер о том, что видно здесь. Отказ при этом тот же по смыслу —
  // «тип записи выбирает человек».
  if (!kind) {
    memoryStatus("Тип записи не выбран: о собеседнике, решение или факт.", true);
    return;
  }
  const field = $("#mem-content");
  const saved = await remember(kind, field.value);
  if (saved) field.value = "";
}

// Та же форма для рабочего слоя: тип обязателен и здесь, и умолчания
// у него нет — слои устроены одинаково, и второе правило на втором слое
// разошлось бы с первым молча.
async function addWorkingFromForm() {
  const kind = $("#mem-work-kind").value;
  if (!kind) {
    workingStatus("Тип записи не выбран: цель, ограничение, решение или открытый вопрос.", true);
    return;
  }
  const field = $("#mem-work-content");
  const saved = await addWorking(kind, field.value);
  if (saved) field.value = "";
}

// Опции дропдауна — из той же карты, что и подписи в списке.
//
// Первым пунктом — пустой: **умолчания у типа нет и в форме**, ровно как
// на сервере, где `_kind_field` отказывает и отсутствующему ключу. Уберём
// пустой пункт — список возьмёт первый настоящий, и пользователь, не тронувший
// его, запишет «о собеседнике», ничего не выбрав: сервер за него не выбирает, а
// форма выбрала бы. День про явный выбор, и выбор обязан быть нажатием
// человека в обоих местах.
// Список типов — параметром: формы две, а правило одно, и вторая копия
// правила разошлась бы с первой на первой же правке.
function fillKinds(select, kinds, blankLabel) {
  select.innerHTML = "";
  const blank = el("option", "", blankLabel || "— выберите тип —");
  blank.value = "";
  select.appendChild(blank);
  kinds.forEach(([token, label]) => {
    const option = el("option", "", label);
    option.value = token;
    select.appendChild(option);
  });
}

// ─────────────────────────── профиль ──────────────────────────
//
// Профиль — про то, **как** с человеком разговаривать: стиль, формат
// и контекст его работы. Он один на всю базу, как долговременная память,
// и пишет в него только человек: профиль это распоряжение («отвечай кратко»),
// а не наблюдение о собеседнике («пишет на Kotlin») — выводить распоряжения
// из разговора агент не вправе.
//
// Запрашивается лениво, на открытие вкладки, ровно как слои памяти: панель
// перерисовывается на каждый обмен, и запрос внутри отрисовки превратил бы
// один поход на сервер в поток.

// Поля — тем же списком, что `PROFILE_FIELDS` на сервере, и в том же порядке:
// им же собран блок `[как отвечать]` в промпте.
const PROFILE_FIELDS = ["style", "format", "context"];

const profileInput = (name) => $("#profile-" + name);

function profileStatus(text, isError) {
  const box = $("#profile-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

async function loadProfile() {
  if (state.profileLoading) return state.profileLoading;
  const held = Object.fromEntries(PROFILE_FIELDS.map((name) => [name, profileInput(name).value]));
  state.profileLoading = (async () => {
    try {
      const answer = await api("/api/profile");
      state.profile = answer.profile || {};
      if (!state.profileDirty.size) state.profileError = "";
      PROFILE_FIELDS.forEach((name) => {
        if (!state.profileDirty.has(name) && profileInput(name).value === held[name]) profileInput(name).value = state.profile[name] || "";
      });
      if (!state.profileDirty.size) profileStatus("");
    } catch (err) {
      state.profileError = String(err.message || err);
      profileStatus(state.profileError, true);
    }
  })();
  await state.profileLoading;
  state.profileLoading = null;
}

// Правка одного поля: уезжает **только тронутое**, остальные не называются
// вовсе — иначе вторая вкладка, правящая формат, затирала бы стиль, набранный
// в первой. Пустая строка поле снимает: отдельной кнопки «очистить» нет,
// ровно как у системного промпта чата.
async function saveProfile(name) {
  if (!PROFILE_FIELDS.includes(name)) return;
  const field = profileInput(name);
  const sent = field.value;
  profileStatus("Сохраняю…");
  try {
    const answer = await api("/api/profile", json("PATCH", { [name]: sent }));
    const values = answer.profile || {};
    // Показываем **записанное**, а не набранное: текст по дороге чистит
    // `redact()`, и поле обязано показывать то, что уедет в промпт.
    state.profile = values;
    state.profileError = "";
    if (field.value === sent) {
      field.value = values[name] || "";
      state.profileDirty.delete(name);
    }
    profileStatus(PROFILE_FIELDS.some((key) => values[key])
      ? "Профиль сохранён: он уезжает системным сообщением в каждый запрос."
      : "Профиль пуст: к запросам не добавляется ничего.");
  } catch (err) {
    state.profileError = String(err.message || err);
    profileStatus(state.profileError, true);
  }
}

// ───────────────────────── инварианты ─────────────────────────
//
// Чего ассистент не вправе предлагать: архитектура, технические решения,
// ограничения стека, бизнес-правила. Слой глобальный, как долговременная
// память и профиль, и пишет в него только человек: инвариант это
// распоряжение, а распоряжений из разговора агент не выводит.
//
// От профиля он отличается наклонением наоборот: профиль про **форму**
// ответа («отвечай кратко»), инвариант про его **суть** («Java не
// предлагать»). Оба едут системным сообщением, и оба задал человек.
//
// Запрещённые слова видны и правятся здесь — и только здесь: в промпт они
// не уезжают ни одним символом. Перечисленный запрет сам по себе подсказка
// его употребить, а законный отказ («почему не Java?») без запрещённого
// слова не написать. Смотрит на них сторож ответа, а не модель.
//
// Запрашивается лениво, на открытие вкладки, ровно как слои памяти
// и профиль: панель перерисовывается на каждый обмен, и запрос внутри
// отрисовки превратил бы один поход на сервер в поток.

function invariantStatus(text, isError) {
  const box = $("#inv-status");
  box.className = "hint" + (isError ? " error" : "");
  box.textContent = text || "";
}

async function loadInvariants() {
  if ($("#inv-list").querySelector(".mem-edit-box")) return;
  try {
    const answer = await api("/api/invariants");
    state.invariants = answer.records || [];
  } catch (err) {
    state.invariants = null;
    invariantStatus(String(err.message || err), true);
  }
  renderInvariants();
}

function renderInvariants() {
  const box = $("#inv-list");
  if (box.querySelector(".mem-edit-box")) return;
  box.innerHTML = "";
  const records = state.invariants;
  if (!records) { box.appendChild(memNote($("#inv-status").textContent || "Читаю…")); return; }
  if (!records.length) { box.appendChild(memNote("Инвариантов нет.")); return; }
  records.forEach((record) => {
    const row = el("div", "mem-item");
    row.append(
      el("div", "mem-kind", invariantKindLabel(record.kind)),
      el("div", "mem-text", record.content),
      iconButton("pencil", "Поправить инвариант",
        () => startRecordEdit(row, record, INVARIANT_KINDS,
          (patch) => editInvariant(record, patch),
          { banned: true, redraw: renderInvariants }), "mini"),
      iconButton("trash", "Убрать инвариант", () => dropInvariant(record), "mini danger")
    );
    // Слова видны только тут: строка под инвариантом говорит, чем сторож
    // будет проверять ответ. В промпт она не уезжает.
    const words = record.banned || [];
    const note = memNote(
      words.length ? "запрещённые слова: " + bannedText(words) : "запрещённых слов нет"
    );
    note.className = "mem-note banned";
    row.appendChild(note);
    box.appendChild(row);
  });
}

// Новый инвариант: список пополняется **ответом ручки**, а не присланным
// телом — номер выдаёт база, а текст по дороге чистит `redact()`. Довод
// и форма те же, что у долговременной памяти.
async function addInvariant(kind, content, banned) {
  const text = (content || "").trim();
  if (!text) {
    invariantStatus("Текст инварианта пуст: записывать нечего.", true);
    return false;
  }
  try {
    const record = await api("/api/invariants", json("POST", { kind, content: text, banned }));
    state.invariants = [...(state.invariants || []), record];
    renderInvariants();
    invariantStatus("Записано: " + invariantKindLabel(record.kind) + ".");
    return true;
  } catch (err) {
    invariantStatus(String(err.message || err), true);
    return false;
  }
}

async function editInvariant(record, patch) {
  try {
    const updated = await api("/api/invariants/" + record.seq, json("PATCH", patch));
    state.invariants = (state.invariants || [])
      .map((item) => (item.seq === updated.seq ? updated : item));
    invariantStatus("Инвариант поправлен.");
    return true;
  } catch (err) {
    invariantStatus(String(err.message || err), true);
    return false;
  }
}

async function dropInvariant(record) {
  try {
    await api("/api/invariants/" + record.seq, { method: "DELETE" });
  } catch (err) {
    invariantStatus(String(err.message || err), true);
    return;
  }
  state.invariants = (state.invariants || []).filter((item) => item.seq !== record.seq);
  renderInvariants();
  invariantStatus("Инвариант убран.");
}

// Вид обязателен и здесь: умолчания у него нет ни в форме, ни на сервере —
// правило то же, что у обоих слоёв памяти. А вот пустой список слов законен:
// у большинства инвариантов сторожить нечего, их держит сам текст.
async function addInvariantFromForm() {
  const kind = $("#inv-kind").value;
  if (!kind) {
    invariantStatus("Вид инварианта не выбран: архитектура, техническое решение, "
      + "ограничение стека или бизнес-правило.", true);
    return;
  }
  const field = $("#inv-content");
  const words = $("#inv-banned");
  const saved = await addInvariant(kind, field.value, parseBanned(words.value));
  if (saved) { field.value = ""; words.value = ""; }
}

// ───────────────────────── инструменты MCP ─────────────────────────
//
// App-wide URL editor and server/tool state. Reading stays lazy, on opening
// the visible Tools section, just like the other global records.
// Запрашивается лениво, на открытие вкладки — ровно как слои
// памяти, профиль и инварианты: панель перерисовывается на каждый обмен,
// и запрос внутри отрисовки превратил бы один поход на сервер в поток.

function mcpStatus(text, error = false) {
  const box = $("#mcp-config-status");
  box.textContent = text;
  box.className = "hint" + (error ? " error" : "");
}

function acceptMcp(answer) {
  if (answer.config && state.mcpConfig && answer.config.revision < state.mcpConfig.revision) return false;
  state.mcp = answer.servers || [];
  state.mcpDisabled = answer.disabled === true;
  state.remindersAvailable = !state.mcpDisabled && state.mcp.some(reminderServer);
  onMcpChange?.();
  if (answer.config) state.mcpConfig = answer.config;
  return true;
}

function mcpDraftRow(row = { name: "", url: "", enabled: false }) {
  const line = el("div", "mcp-config-row");
  line.dataset.enabled = String(row.enabled);
  line.dataset.originalName = row.name;
  line.dataset.originalUrl = row.url;
  const nameLabel = el("label", "field", "Имя сервера");
  const name = el("input", "control mcp-name");
  name.value = row.name;
  name.placeholder = "server";
  name.setAttribute("aria-label", "Имя сервера MCP");
  const urlLabel = el("label", "field", "Streamable HTTP URL");
  const url = el("input", "control mcp-url");
  url.type = "url";
  url.value = row.url;
  url.placeholder = "http://127.0.0.1:8016/mcp";
  url.setAttribute("aria-label", "Streamable HTTP URL сервера MCP");
  [name, url].forEach((input) => { input.oninput = () => { state.mcpDirty = true; state.mcpDraftVersion = (state.mcpDraftVersion || 0) + 1; }; });
  nameLabel.appendChild(name); urlLabel.appendChild(url);
  const remove = el("button", "mcp-button", "Убрать");
  remove.type = "button";
  remove.onclick = () => { line.remove(); state.mcpDirty = true; state.mcpDraftVersion = (state.mcpDraftVersion || 0) + 1; };
  line.append(nameLabel, urlLabel, remove);
  $("#mcp-config-rows").appendChild(line);
}

function renderMcpConfig() {
  if (!state.mcpDirty) {
    const rows = $("#mcp-config-rows");
    rows.innerHTML = "";
    const saved = (state.mcpConfig || {}).servers || [];
    (saved.length ? saved : [{ name: "", url: "", enabled: false }]).forEach(mcpDraftRow);
  }
  $("#mcp-add").onclick = () => { if (state.settingsScope !== "app") return; mcpDraftRow(); state.mcpDirty = true; state.mcpDraftVersion = (state.mcpDraftVersion || 0) + 1; };
  $("#mcp-config-form").onsubmit = (event) => {
    event.preventDefault();
    const rows = Array.from($("#mcp-config-rows").children).map((line) => {
      const name = line.querySelector(".mcp-name").value.trim();
      const url = line.querySelector(".mcp-url").value.trim();
      return { name, url, enabled: line.dataset.enabled === "true" && name === line.dataset.originalName && url === line.dataset.originalUrl };
    });
    mutateMcp("/api/mcp/config", "PUT", { revision: (state.mcpConfig || {}).revision || 0, servers: rows });
  };
  $("#mcp-save").disabled = !!state.mcpMutation;
}

async function mutateMcp(path, method, body) {
  if (state.settingsScope !== "app" || state.mcpMutation) return;
  if (method !== "PUT" && state.mcpDirty) { mcpStatus("Сначала сохраните изменённые URL.", true); return; }
  stopMcpPolling();
  const epoch = state.mcpEpoch;
  const version = state.mcpDraftVersion || 0;
  let changed = false;
  state.mcpMutation = true;
  renderMcpConfig(); renderMcp();
  mcpStatus("Ожидание текущих вызовов и обновление подключения…");
  try {
    const answer = await api(path, json(method, body));
    acceptMcp(answer); changed = true;
    if (method === "PUT" && version === (state.mcpDraftVersion || 0)) state.mcpDirty = false;
    if (epoch === state.mcpEpoch && toolsVisible()) {
      mcpStatus(method === "PUT" ? "URL сохранены. Подключите нужный сервер." : "Состояние подключения обновлено.");
    }
  } catch (err) {
    if (epoch === state.mcpEpoch && toolsVisible()) mcpStatus(String(err.message || err), true);
  } finally {
    state.mcpMutation = false;
    if (epoch === state.mcpEpoch && toolsVisible()) { renderMcpConfig(); renderMcp(); if (changed) loadMcp(); }
    else if (toolsVisible()) loadMcp();
  }
}

async function loadMcp(discover = false) {
  const discovery = discover && state.workspace === "settings" && state.settingsScope === "chat" && document.visibilityState !== "hidden";
  if ((!toolsVisible() && !discovery) || state.mcpRequest || state.mcpMutation) return;
  if (state.mcpTimer !== null) clearTimeout(state.mcpTimer);
  state.mcpTimer = null;
  const epoch = state.mcpEpoch, scope = state.settingsScope, owner = state.current?.id;
  const owned = () => epoch === state.mcpEpoch && scope === state.settingsScope && (scope !== "chat" || owner === state.current?.id);
  const controller = new AbortController();
  state.mcpRequest = controller;
  try {
    const headers = state.settingsScope === "chat" && state.current ? { "X-Chat-ID": state.current.id } : {};
    const answer = await api("/api/mcp", { signal: controller.signal, headers });
    if (!owned()) return;
    acceptMcp(answer);
    mcpStatus(state.mcpDisabled ? "MCP отключён." : "");
  } catch (err) {
    if (!owned()) return;
    state.mcp = null; state.remindersAvailable = false; onMcpChange?.();
    mcpStatus(String(err.message || err), true);
  } finally {
    if (state.mcpRequest === controller) state.mcpRequest = null;
  }
  if (!owned() || !toolsVisible()) return;
  renderMcpConfig(); renderMcp();
  // Один отложенный GET после завершения предыдущего: медленная ручка
  // не создаёт параллельных запросов, уход отменяет и таймер, и GET.
  state.mcpTimer = setTimeout(() => {
    state.mcpTimer = null;
    loadMcp();
  }, 2000);
}

function toolsVisible() {
  return state.workspace === "settings" && state.section === "mcp" && document.visibilityState !== "hidden";
}

function stopMcpPolling() {
  state.mcpEpoch += 1;
  if (state.mcpTimer !== null) clearTimeout(state.mcpTimer);
  state.mcpTimer = null;
  if (state.mcpRequest) {
    state.mcpRequest.abort();
    state.mcpRequest = null;
  }
}

function remindersBlock(data) {
  const ownerId = state.current?.id;
  const owned = () => state.settingsScope === "chat" && state.current?.id === ownerId;
  const box = el("section", "mem-reminders");
  box.setAttribute("aria-label", "Напоминания");
  box.appendChild(el("h4", "mem-kind", "напоминания — ждёт: " + (data.waiting ?? 0)
    + " · сработало: " + (data.fired ?? 0)
    + (data.running ? " · выполняется: " + data.running : "")
    + (data.failed ? " · ошибка: " + data.failed : "")
    + (data.unbound ? " · не привязано: " + data.unbound : "")));
  const items = data.items || [];
  if (!items.length) box.appendChild(memNote("напоминаний нет"));
  for (const item of items) {
    const row = el("div", "reminder-item");
    row.appendChild(el("div", "mem-text", "№" + item.id + " — " + item.text));
    const deadline = fmt.clock(item.due_at);
    const line = item.state + " · раз: " + item.fired
      + (item.state === "ждёт" || item.every ? " · следующее в " + deadline : " · срок " + deadline)
      + (item.error ? " · " + item.error : "");
    row.appendChild(el("div", "mem-note", line));
    if (item.can_cancel) {
      const cancel = el("button", "mcp-button", "Снять");
      cancel.type = "button";
      cancel.setAttribute("aria-label", "Снять напоминание №" + item.id);
      cancel.onclick = async () => {
        const chat = ownerId;
        if (!chat || !owned() || cancel.disabled) return;
        cancel.disabled = true;
        try {
          await api("/api/agents/" + encodeURIComponent(chat) + "/reminders/"
            + encodeURIComponent(data.server_name) + "/" + item.id + "/cancel", json("POST", {}));
          if (owned() && toolsVisible()) await loadMcp();
        } catch (error) {
          if (!owned()) return;
          cancel.disabled = false;
          mcpStatus(String(error.message || error), true);
        }
      };
      row.appendChild(cancel);
    }
    box.appendChild(row);
  }
  return box;
}

function reminderServer(server) {
  return server.status === "ok" && (Object.hasOwn(server, "reminders") || Object.hasOwn(server, "reminders_error"));
}
function renderMcp() {
  const box = $("#mcp-list");
  const expanded = new Set(Array.from(box.querySelectorAll("details")).filter((node) => node.open).map((node) => node.dataset.tool));
  box.innerHTML = "";
  const servers = state.mcp;
  // Пустой менеджер назван словами, а не показан пустым экраном: «не
  // подключён» и «не доехало» — разные новости, ровно как у слоёв памяти.
  if (!servers) { box.appendChild(memNote("Не читается: ручка ответила ошибкой.")); return; }
  if (!servers.length) { box.appendChild(memNote("MCP не подключён.")); return; }
  servers.filter(server => state.settingsScope !== "chat" || reminderServer(server)).forEach((server) => {
    const row = el("article", "mcp-server");
    const head = el("header", "mcp-server-head");
    head.append(el("h3", "mcp-server-name", server.name),
      el("span", "mcp-server-status" + (server.status === "ok" ? " ok" : " down"), server.status === "ok" ? "Подключён" : server.status === "disconnected" ? "Отключён" : "Не отвечает"));
    row.appendChild(head);
    if (server.url && state.settingsScope === "app") {
      row.appendChild(el("p", "mem-note", server.url));
      const controls = el("div", "mcp-config-actions");
      const connect = el("button", "mem-add", server.status === "ok" ? "Переподключить" : "Подключить");
      connect.type = "button";
      connect.disabled = !!state.mcpMutation || state.mcpDisabled;
      connect.onclick = () => mutateMcp("/api/mcp/connect", "POST", { name: server.name, revision: state.mcpConfig.revision });
      const disconnect = el("button", "mcp-button", "Отключить");
      disconnect.type = "button";
      disconnect.disabled = !!state.mcpMutation || server.status === "disconnected";
      disconnect.onclick = () => mutateMcp("/api/mcp/disconnect", "POST", { name: server.name, revision: state.mcpConfig.revision });
      controls.append(connect, disconnect); row.appendChild(controls);
    }
    if (server.error) row.appendChild(el("p", "hint error", server.error));
    if (server.reminders_error) row.appendChild(el("p", "hint error", server.reminders_error));
    const tools = state.settingsScope === "chat" ? [] : server.tools || [];
    if (!tools.length && state.settingsScope !== "chat") row.appendChild(memNote("инструментов нет"));
    tools.forEach((tool) => {
      const card = el("div", "mcp-tool");
      card.append(el("h4", "mcp-tool-name", tool.name),
        el("p", "mcp-tool-description", tool.description || "Описания нет."));
      // Схема свёрнута: она нужна, когда спрашивают «что умеет», а не всегда.
      const schema = el("details", "mem-note");
      schema.dataset.tool = server.name + ":" + tool.name;
      schema.open = expanded.has(schema.dataset.tool);
      schema.appendChild(el("summary", "", "Схема параметров"));
      const code = el("pre", "", JSON.stringify(tool.schema || {}, null, 2));
      code.tabIndex = 0;
      code.setAttribute("aria-label", "Схема параметров " + tool.name);
      schema.appendChild(code);
      card.appendChild(schema);
      row.appendChild(card);
    });
    if (server.reminders && state.settingsScope === "chat" && state.current) row.appendChild(remindersBlock({ ...server.reminders, server_name: server.name }));
    box.appendChild(row);
  });
}


return { MEMORY_KINDS, WORKING_KINDS, INVARIANT_KINDS, PROFILE_FIELDS, memoryTabOpen, loadMemory, renderMemory, workingStatus, loadProfile, saveProfile, loadInvariants, loadMcp, toolsVisible, stopMcpPolling, fillKinds, addFromForm, addWorkingFromForm, addInvariantFromForm };
}

if (typeof module !== "undefined") module.exports = createChatRecords;
else root.createChatRecords = createChatRecords;
})(globalThis);
