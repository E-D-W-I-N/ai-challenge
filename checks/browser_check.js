// Execute the real vanilla client against explicit offline HTTP/SSE scenarios.
// Server computations are tested in Python; this file checks the UI boundary.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { TextDecoder, TextEncoder } = require("util");
const { boot, settle, Evt } = require("./dom.js");
const { json, failure, deferred, stream, start, success, turns, planning, execution } = require("./fixtures.js");
const STATIC = process.env.CHECK_STATIC_DIR || path.join(__dirname, "..", "app", "static");
const HTML = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const failures = [];
let passed = 0;
function check(name, condition, detail = "") {
  if (condition) passed++;
  else failures.push(name + (detail ? ": " + detail : ""));
}
process.on("unhandledRejection", (error) => failures.push("Unhandled client rejection: " + error.stack));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function freshClient(options = {}, classic = false) {
  const env = boot(HTML, options);
  let client;
  if (classic) {
    // Execute the actual HTML scripts, in their actual order, without CommonJS.
    const context = vm.createContext({ document, window, localStorage, fetch,
      AbortController, TextDecoder, TextEncoder, setTimeout, clearTimeout, console });
    for (const match of HTML.matchAll(/<script\b[^>]*src="([^"]+)"[^>]*>/g)) {
      const filename = path.join(STATIC, path.basename(match[1]));
      vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
    }
    client = vm.runInContext("({state})", context);
    client.init = () => {}; // Browser bootstrap already ran in the script.
  } else {
    for (const key of Object.keys(require.cache)) if (key.startsWith(STATIC + path.sep)) delete require.cache[key];
    client = require(path.join(STATIC, "app.js"));
  }
  const $ = (selector) => env.document.querySelector(selector);
  const click = (id) => $("#" + id).dispatchEvent(new Evt("click"));
  const requests = (method, route) => env.server.state.requests.filter((r) =>
    r.method === method && (typeof route === "string" ? r.path === route : route.test(r.path)));
  const send = (text) => { $("#input").value = text; $("#composer").requestSubmit(); };
  const open = (index) => $("#agent-list").querySelectorAll(".item-open")[index].dispatchEvent(new Evt("click"));
  return { ...env, client, $, click, requests, send, open };
}
const tile = ($, name) => $("#tiles").children.find((node) => node.querySelector(".tile-k").textContent === name)?.querySelector(".tile-v").textContent;
const text = (node, selector) => node.querySelector(selector)?.textContent || "";
const button = (node, title) => node.querySelectorAll("button").find((b) => b.title === title);

async function scenario(name, run) {
  try { await run(); } catch (error) { failures.push(name + ": " + error.stack); }
  finally { window.dispatchEvent?.(new Evt("pagehide")); }
}

async function main() {
  for (const classic of [false, true]) await scenario("Automatic due result and late chat refresh " + classic, async () => {
    const { client, server, $, requests, open } = freshClient({}, classic);
    client.init(); await settle(30);
    $("#input").value = "unsent draft";
    $("#f-system").value = "unsaved setting";
    const delayed = [{ ...turns[0], content: "[Напоминание №7] Выполни сейчас" },
      { ...turns[1], content: "Автоматический свежий итог", metrics: { tool_calls: [{ name: "git_log", server: "git", ms: 3, ok: true }] } }];
    Object.assign(server.state.agents[0], { transcript: delayed, history_len: 2 });
    await settle(1100);
    check("Idle chat receives real server result without Tools or send " + classic,
      $("#feed").textContent.includes("Автоматический свежий итог") && $("#feed").textContent.includes("git_log")
      && requests("GET", "/api/mcp").length === 0 && requests("POST", /\/messages$/).length === 0);
    check("Background refresh preserves composer and settings drafts " + classic,
      $("#input").value === "unsent draft" && $("#f-system").value === "unsaved setting");
    const late = deferred(); server.respond("GET", "/api/agents/ag_1", () => late.promise);
    document.fire(new Evt("visibilitychange")); await settle(10);
    const controller = client.state.chatRequest;
    open(1); await settle(20);
    late.resolve(json({ ...server.state.agents[0], transcript: [{ ...turns[1], content: "LATE BACKGROUND" }] })); await settle(10);
    check("Chat switch aborts and ignores late background GET " + classic,
      controller.signal.aborted && client.state.current.id === "ag_2" && !$("#feed").textContent.includes("LATE BACKGROUND"));
    window.dispatchEvent(new Evt("pagehide"));
    check("Pagehide cancels selected-chat timer " + classic, client.state.chatTimer === null);
  });

  await scenario("Foreground prompt stays on its committed answer before due append", async () => {
    const { client, server, $, send } = freshClient();
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(success.map((event) =>
      event.event === "done" ? { ...event, answer_index: 1 } : event), {
        finish: () => Object.assign(server.state.agents[0], { transcript: [...turns,
          { ...turns[0], content: "scheduled task" }, { ...turns[1], content: "scheduled answer" }], history_len: 4 }),
      }));
    client.init(); await settle(30); send("question"); await settle(30);
    check("Foreground info is bound to SSE answer_index, never newest background row",
      client.state.prompts.has("ag_1:1") && !client.state.prompts.has("ag_1:3"));
  });

  await scenario("Markdown and parameters", async () => {
    const { client } = freshClient();
    for (const input of ["<script>x</script>", "**<img src=x onerror=x>**", "`<script>x</script>`", "```\n<img src=x>\n```", "# <iframe>", "> <img>", "- <img>"]) {
      const rendered = client.renderMarkdown(input);
      check("HTML stays text: " + input, !/<(?:script|img|iframe)\b/.test(rendered), rendered);
    }
    for (const scheme of ["javascript:alert(1)", "data:text/html,x", "vbscript:x", "file:///tmp/x"]) {
      check("Dangerous link stays text: " + scheme, !client.renderMarkdown("[click](" + scheme + ")").includes("<a "));
    }
    const safe = client.renderMarkdown('[<img src=x>](https://example.com)');
    check("Safe links escape labels and isolate the destination", safe.includes('href="https://example.com"') && safe.includes('rel="noreferrer noopener"') && safe.includes('target="_blank"') && !safe.includes("<img"), safe);
    check("Basic markup renders", client.renderMarkdown("**bold** and `code`").includes("<strong>bold</strong>") && client.renderMarkdown("**bold** and `code`").includes("<code>code</code>"));
    check("Inline placeholder injection is harmless", !client.renderMarkdown("a\u00000\u0000b").includes("<code>"));
    check("Unsupported parameter is reported", client.paramWarnings({ supported_parameters: ["top_p"] }, { temperature: 0.5 }, {}, "m").some((line) => line.includes("temperature")));
    check("Provider temperature cap is reported", client.paramWarnings({ supported_parameters: ["temperature"], temperature_capped: true, temperature_cap: 1 }, { temperature: 1.2 }, {}, "m").length === 1);
  });

  // Both loading paths execute production files; the classic path also catches
  // missing sibling scripts, their order and browser-only dependency wiring.
  for (const classic of [false, true]) await scenario("Panel to request " + classic, async () => {
    const { client, server, $, requests, send } = freshClient({}, classic);
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(success, { fragmented: true,
      finish: () => Object.assign(server.state.agents[0], { transcript: turns, history_len: 2 }) }));
    client.init(); await settle(30);
    $("#f-model").value = "вторая/модель";
    $("#f-system").value = "НОВЫЙ ПРОМПТ";
    $("#f-temperature").value = "0.9";
    $("#f-stop").value = "КОНЕЦ\nСТОП";
    $("#f-response_format_kind").value = "json_object";
    $("#f-strategy").value = "window";
    $("#f-keep_last").value = "2";
    send("вопрос"); await settle(60);
    const patch = requests("PATCH", "/api/agents/ag_1")[0];
    check("Unblurred fields are saved before sending " + classic, patch?.body.model === "вторая/модель" && patch.body.system === "НОВЫЙ ПРОМПТ" && patch.body.temperature === 0.9 && same(patch.body.stop, ["КОНЕЦ", "СТОП"]) && same(patch.body.response_format, { type: "json_object" }) && patch.body.strategy === "window" && patch.body.keep_last === 2, JSON.stringify(patch));
    check("One text-only request follows the config PATCH " + classic, requests("POST", /\/messages$/).length === 1 && same(requests("POST", /\/messages$/)[0].body, { text: "вопрос" }) && server.state.requests.findIndex((r) => r.method === "PATCH") < server.state.requests.findIndex((r) => /\/messages$/.test(r.path)));
    check("Fragmented Cyrillic SSE reaches committed card " + classic, $("#feed").textContent.includes("ответ модели") && client.state.prompts.size === 1);
  });

  for (const classic of [false, true]) await scenario("Tool events before done and persisted badges " + classic, async () => {
    const calls = [
      { name: "git_log", server: "git", ms: 12.4, ok: true },
      { name: "git_diff_stat", server: "git", ms: 3, ok: false },
    ];
    const metrics = { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, tool_calls: calls };
    const transcript = [turns[0], { ...turns[1], metrics }, ...turns];
    const { client, server, $, send, open } = freshClient({ agents: [{ transcript, history_len: 4 }, {}] }, classic);
    const badges = (card) => card.querySelectorAll(".tool-badge").map((node) => node.textContent).join(" | ");
    const expected = "git_log · git · 12 мс | ошибка вызова: git_diff_stat · git · 3 мс";
    client.init(); await settle(30);
    const saved = $("#feed").querySelectorAll(".card");
    check("Saved success/error badges share the same row " + classic, badges(saved[0]) === expected && saved[0].querySelectorAll(".tool-badge.failed").length === 1);
    check("Tool-free answer has no tool row " + classic, !saved[1].querySelector(".card-tools"));
    check("Tool badges leave token values separate " + classic, text(saved[0], ".usage-tokens") === "входные токены 10 · выходные токены 5 · всего токенов 15" && $("#tiles").children.length === 6);

    const reached = deferred(), release = deferred();
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([
      start,
      { event: "tool_call", ...calls[0], arguments: { n: 1 }, result: "offline git result" },
      { event: "tool_call", ...calls[1], arguments: { ref: "bad" }, result: "fixture failure" },
      { event: "delta", text: "ответ модели" },
      { event: "done", text: "ответ модели", committed: true, metrics },
    ], {
      beforeRead: async (index) => { if (index === 3) { reached.resolve(); await release.promise; } },
      finish: () => Object.assign(server.state.agents[0], { transcript: [...transcript, turns[0], { ...turns[1], metrics }], history_len: 6 }),
    }));
    send("вопрос");
    await Promise.race([reached.promise, new Promise((_, reject) => setTimeout(() => reject(new Error("Tool stream did not reach the gate")), 1000))]);
    const live = $("#feed").querySelectorAll(".card").slice(-1)[0];
    check("Both tool events are visible while done is blocked " + classic, client.state.busy && live.classList.contains("busy") && badges(live) === expected && live.querySelectorAll(".tool-badge.failed").length === 1);
    release.resolve(); await settle(30);
    const rendered = $("#feed").querySelectorAll(".card").slice(-1)[0];
    check("Done rerenders tool badges from persisted metrics " + classic, !client.state.busy && rendered !== live && badges(rendered) === expected && client.state.current.transcript.slice(-1)[0].metrics.tool_calls.length === 2);
    open(1); await settle(20); open(0); await settle(20);
    check("Reopening chat preserves success/error tool badges " + classic, badges($("#feed").querySelectorAll(".card").slice(-1)[0]) === expected && $("#tiles").children.length === 6);
  });

  await scenario("Unset, invalid and stale panel saves", async () => {
    const { client, server, $, requests, send } = freshClient({ agents: [{ temperature: 0.5, stop: ["old"] }] });
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(success));
    client.init(); await settle(30);
    $("#f-temperature").value = ""; $("#f-stop").value = "";
    send("вопрос"); await settle(30);
    check("Blank fields unset saved values", requests("PATCH", "/api/agents/ag_1")[0]?.body.temperature === null && requests("PATCH", "/api/agents/ag_1")[0]?.body.stop === null);
    $("#f-temperature").value = "invalid"; send("blocked"); await settle(10);
    check("Invalid number does not send", requests("POST", /\/messages$/).length === 1 && client.state.panelDirty);
    $("#f-temperature").value = "0.2";
    const delayed = deferred();
    server.respond("PATCH", "/api/agents/ag_1", () => delayed.promise);
    $("#f-temperature").dispatchEvent(new Evt("change")); await settle(10);
    $("#f-response_format_kind").value = "custom";
    $("#f-response_format").value = "{invalid";
    $("#f-response_format").dispatchEvent(new Evt("change"));
    delayed.resolve(json({ ...server.state.agents[0], temperature: 0.2 })); await settle(20);
    check("Old save cannot erase new invalid JSON", client.state.panelDirty && $("#save-status").classList.contains("error") && $("#f-response_format").value === "{invalid");
    send("blocked JSON"); await settle(10);
    check("Invalid JSON does not send", requests("POST", /\/messages$/).length === 1);
  });

  await scenario("Workspace, desktop sidebar and confirmation", async () => {
    const { client, server, $, click, send, requests } = freshClient();
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(success, { delay: 25,
      finish: () => Object.assign(server.state.agents[0], { transcript: turns, history_len: 2 }) }));
    client.init(); await settle(30);
    const feed = $("#feed"); $("#input").value = "draft";
    click("workspace-settings"); click("workspace-chat");
    check("Workspace preserves mounted feed, selection and draft", $("#feed") === feed && client.state.current.id === "ag_1" && $("#input").value === "draft");
    click("sidebar-toggle");
    check("Desktop sidebar records collapsed state", $("#app").classList.contains("no-sidebar"));
    click("restore-sidebar");
    check("Desktop sidebar reopens", !$("#app").classList.contains("no-sidebar"));
    const trash = $("#agent-list").querySelectorAll(".mini").find((b) => b.title === "Удалить чат");
    trash.focus(); trash.dispatchEvent(new Evt("click"));
    const dialog = $(".confirm-box");
    check("Confirmation captures focus", dialog.attributes.role === "dialog" && dialog.contains(document.activeElement));
    document.fire(new Evt("keydown", { key: "Tab" }));
    check("Confirmation traps Tab", dialog.contains(document.activeElement));
    document.fire(new Evt("keydown", { key: "Escape" }));
    check("Confirmation restores focus without deleting", !$(".confirm") && document.activeElement === trash && requests("DELETE", /./).length === 0);
    send("вопрос"); await settle(35);
    const controller = client.state.abort;
    click("workspace-settings");
    check("Workspace switch keeps stream active", client.state.busy && !controller.signal.aborted);
    await settle(100); click("workspace-chat");
    check("Stream finishes in original workspace", !client.state.busy && $("#feed").textContent.includes("ответ модели"));
  });

  await scenario("Server metric values, unknown and model change", async () => {
    const metrics = { model: "первая/модель", prompt_tokens: 100, completion_tokens: 60, total_tokens: 160,
      cost_usd: 0.0001, context_fill_pct: 1.2, reasoning_tokens: 40 };
    const { client, server, $, open } = freshClient({ agents: [{ transcript: [turns[0], { ...turns[1], reasoning: "думал", metrics }],
      history_len: 18, usage_total: { prompt_tokens: 700, completion_tokens: 80, total_tokens: 780, cost_usd: 0.0004 } }, {}] });
    client.init(); await settle(30);
    check("Tiles use server totals and message count", tile($, "Входные токены") === "700" && tile($, "Всего токенов") === "780" && tile($, "Сообщений") === "18" && tile($, "Стоимость") === "$0.000400");
    check("Reasoning remains within provider completion tokens", text($("#feed"), ".usage-tokens").includes("60 (из них 40 рассуждение)"));
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([start,
      { event: "error", message: "HTTP 402", metrics: { error: "HTTP 402", prompt_tokens: null, context_fill_pct: null } }]));
    $("#input").value = "failed"; $("#composer").requestSubmit(); await settle(20);
    const contextTile = $("#tiles").children.find((node) => node.querySelector(".tile-k").textContent === "Контекст");
    check("Error keeps known metrics and marks context as previous", tile($, "Всего токенов") === "780" && tile($, "Контекст") === "1.2 %" && contextTile.querySelector(".tile-v").classList.contains("past"));
    $("#f-model").value = "вторая/модель"; $("#f-model").dispatchEvent(new Evt("change")); await settle(20);
    check("Model change resets context only", tile($, "Контекст") === "—" && tile($, "Всего токенов") === "780");
    open(1); await settle(20);
    check("Empty neighbour uses unknown rather than zero", tile($, "Входные токены") === "—" && tile($, "Стоимость") === "—" && tile($, "Контекст") === "—" && tile($, "Сообщений") === "0");
  });

  await scenario("Context notes and guard with/without usage", async () => {
    const transcript = [
      turns[0], { ...turns[1], metrics: { prompt_tokens: 400, summarized: 10 } },
      turns[0], { ...turns[1], metrics: { prompt_tokens: 120, dropped: 14 } },
      turns[0], { ...turns[1], metrics: { prompt_tokens: 200 } },
      turns[0], { ...turns[1], metrics: { banned_hits: [{ word: "Java", rule: "ограничение стека: только Python" }] } },
      turns[0], { ...turns[1], metrics: { prompt_tokens: 10, banned_hits: [{ word: "C++", rule: "архитектура: монолит" }] } },
    ];
    const { client, $ } = freshClient({ agents: [{ transcript, history_len: 10 }] });
    client.init(); await settle(30);
    const cards = $("#feed").querySelectorAll(".card");
    check("Summary and window report distinct cuts", text(cards[0], ".usage-tokens").includes("сводка вместо 10 сообщений") && text(cards[1], ".usage-tokens").includes("окно: отброшено 14 сообщений"));
    check("No cut produces no cut note", !/сводка|отброшено/.test(text(cards[2], ".usage-tokens")));
    check("Guard remains visible without numeric usage", !cards[3].querySelector(".usage-tokens") && text(cards[3], ".card-guard").includes("Java") && text(cards[3], ".card-guard").includes("только Python"));
    check("Guard and numeric usage coexist", cards[4].querySelector(".usage-tokens") && text(cards[4], ".card-guard").includes("C++") && !cards[2].querySelector(".card-guard"));
  });

  await scenario("Committed prompt slots and failed stream ownership", async () => {
    const frames = [
      { event: "compressing", strategy: "summary" },
      { ...start, strategy: "summary", memory_at: 0, working_at: 1, task_at: 2, summary_at: 3,
        resolved_messages: [{ role: "user", content: "same text" }, { role: "user", content: "same text" },
          { role: "user", content: "same text" }, { role: "user", content: "same text" },
          { role: "assistant", content: "old answer" }, { role: "user", content: "вопрос" }] },
      { event: "delta", text: '<script>x</script> [bad](javascript:alert(1))' },
      { event: "done", committed: true, text: '<script>x</script> [bad](javascript:alert(1))' },
    ];
    const saved = [turns[0], { ...turns[1], content: '<script>x</script> [bad](javascript:alert(1))' }];
    const { client, server, $, send } = freshClient();
    const completed = deferred();
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(frames, { delay: 20,
      finish: () => { Object.assign(server.state.agents[0], { transcript: saved, history_len: 2 }); completed.resolve(); } }));
    client.init(); await settle(30); send("вопрос"); await settle(25);
    check("Compression announces the service before reply", $("#feed").querySelector(".card-status")?.textContent.includes("Сворачиваю"));
    await completed.promise; await settle(10);
    const card = $("#feed").querySelector(".card");
    check("Real card escapes model HTML and unsafe links", !card.querySelector(".card-body").innerHTML.includes("<script") && !card.querySelector(".card-body").innerHTML.includes("<a "));
    button(card, "Показать промпт запроса").dispatchEvent(new Evt("click"));
    const roles = card.querySelectorAll(".prompt-role").map((node) => node.textContent);
    check("Slots use numeric positions, including zero", same(roles, ["долговременная память", "факты о разговоре", "состояние задачи", "сводка начала разговора", "ответ модели", "сообщение пользователя"]), JSON.stringify(roles));
    const savedPrompt = [...client.state.prompts.values()][0];
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([
      { ...start, resolved_messages: [{ role: "user", content: "FAILED PROMPT" }] },
      { event: "error", message: "HTTP 402", metrics: { error: "HTTP 402", prompt_tokens: null, context_fill_pct: null } },
    ]));
    send("failed question"); await settle(30);
    check("Failed start does not replace committed prompt", client.state.prompts.size === 1 && [...client.state.prompts.values()][0] === savedPrompt);
    check("Failed stream returns question and leaves history intact", $("#input").value === "failed question" && $("#composer-hint").textContent.includes("402") && client.state.current.history_len === 2);
  });

  await scenario("Profile menu, partial PATCH, dirty reread and global scope", async () => {
    const { client, server, $, click, requests, open } = freshClient();
    server.respond("GET", "/api/profile", { profile: { style: "кратко", format: "списком" } });
    server.respond("PATCH", "/api/profile", () => json({ profile: { style: "ясно ***", format: "списком" } }));
    client.init(); await settle(30);
    check("Profile is lazy", requests("GET", "/api/profile").length === 0);
    click("profile-toggle"); await settle(10); click("profile-edit");
    check("Profile menu opens shared editor", client.state.section === "profile" && $("#profile-style").value === "кратко" && requests("GET", "/api/profile").length === 1);
    $("#profile-style").value = "ясно fixture"; $("#profile-style").dispatchEvent(new Evt("input")); $("#profile-style").dispatchEvent(new Evt("change")); await settle(10);
    check("Profile PATCH sends only touched field and displays API result", same(requests("PATCH", "/api/profile")[0]?.body, { style: "ясно fixture" }) && $("#profile-style").value === "ясно ***" && $("#profile-format").value === "списком" && requests("PATCH", /^\/api\/agents\//).length === 0);
    server.respond("PATCH", "/api/profile", () => failure("профиль занят"));
    $("#profile-context").value = "не терять"; $("#profile-context").dispatchEvent(new Evt("input")); $("#profile-context").dispatchEvent(new Evt("change")); await settle(10);
    click("workspace-chat"); click("workspace-settings"); await settle(10);
    check("Profile failure survives reread without losing dirty text", $("#profile-context").value === "не терять" && $("#profile-status").textContent.includes("занят"));
    server.respond("PATCH", "/api/profile", () => json({ profile: { format: "списком", context: "не терять" } }));
    $("#profile-context").dispatchEvent(new Evt("change")); await settle(10);
    server.respond("GET", "/api/profile", { profile: { format: "списком", context: "не терять" } });
    open(1); await settle(20); click("profile-toggle"); await settle(10);
    check("Profile data follows global API across chats", $("#profile-summary").textContent.includes("не терять"));
  });

  // One shared editor matrix: scope/id/kind discriminate the three real routes.
  // Enter+focusout and failure/retry are exercised once, not on every layer.
  for (const kind of ["working", "long", "invariant"]) await scenario("Record CRUD " + kind, async () => {
    const isWorking = kind === "working", isInvariant = kind === "invariant";
    const container = isInvariant ? "#inv-list" : isWorking ? "#mem-working" : "#mem-long";
    const base = isInvariant ? "/api/invariants" : isWorking ? "/api/agents/ag_1/working" : "/api/memory";
    const record = { seq: 37, kind: isInvariant ? "stack" : isWorking ? "goal" : "knowledge", content: "old", banned: ["Java"], at: 1 };
    const changed = { ...record, content: "saved ***", kind: isInvariant ? "architecture" : isWorking ? "question" : "profile", banned: ["C++", ".NET"] };
    const { client, server, $, click, requests } = freshClient();
    const memory = { short_term: { messages: 0, summaries: [] }, working: { records: isWorking ? [record] : [] }, long_term: { records: isWorking ? [] : [record] } };
    server.respond("GET", "/api/agents/ag_1/memory", memory);
    server.respond("GET", "/api/invariants", { records: [record], total: 1 });
    server.respond("PATCH", base + "/37", () => json(changed));
    server.respond("POST", base, () => json({ ...changed, seq: 84 }));
    server.respond("DELETE", base + "/37", () => json({ deleted: 37 }));
    client.init(); await settle(30); click(isInvariant ? "tab-btn-invariants" : "tab-btn-memory"); await settle(10);
    $(container).querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    const editor = $(container).querySelector(".mem-edit");
    const select = $(container).querySelector(".mem-edit-kind");
    editor.value = "saved fixture"; select.value = changed.kind;
    if (isInvariant) $(container).querySelector(".mem-edit-banned").value = "C++, .NET";
    if (kind === "long") {
      server.respond("PATCH", base + "/37", () => failure("память занята"));
      editor.dispatchEvent(new Evt("keydown", { key: "Enter" })); await settle(10);
      click("workspace-chat"); click("workspace-settings"); await settle(10);
      check("Record failure keeps the mounted editor and draft", $(container).querySelector(".mem-edit") === editor && editor.value === "saved fixture" && $("#mem-status").textContent.includes("занята"));
      server.respond("PATCH", base + "/37", () => json(changed));
    }
    const before = requests("PATCH", base + "/37").length;
    editor.dispatchEvent(new Evt("keydown", { key: "Enter" }));
    if (isWorking) editor.blur();
    await settle(20);
    const body = { content: "saved fixture", kind: changed.kind, ...(isInvariant ? { banned: ["C++", ".NET"] } : {}) };
    check("Editor PATCH has distinct id/scope/kind " + kind, requests("PATCH", base + "/37").length === before + 1 && same(requests("PATCH", base + "/37").slice(-1)[0]?.body, body), JSON.stringify(requests("PATCH", base + "/37")));
    check("Editor uses API record rather than request text " + kind, $(container).textContent.includes("saved ***") && !$(container).querySelector(".mem-edit"));
    if (kind === "long") {
      server.respond("PATCH", base + "/37", () => json({ ...changed, kind: "knowledge" }));
      $(container).querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
      const type = $(container).querySelector(".mem-edit-kind"); type.value = "knowledge";
      type.blur(); await settle(10);
      check("Kind-only edit omits unchanged content", same(requests("PATCH", base + "/37").slice(-1)[0].body, { kind: "knowledge" }));
    }
    const prefix = isInvariant ? "inv" : isWorking ? "mem-work" : "mem";
    $("#" + prefix + "-content").value = "new record"; click(prefix + "-add"); await settle(10);
    check("Missing record kind does not send " + kind, requests("POST", base).length === 0);
    $("#" + prefix + "-kind").value = changed.kind;
    if (isInvariant) $("#inv-banned").value = "C++, .NET";
    click(prefix + "-add"); await settle(10);
    check("Add uses correct layer and kind " + kind, same(requests("POST", base)[0]?.body, { kind: changed.kind, content: "new record", ...(isInvariant ? { banned: ["C++", ".NET"] } : {}) }) && $(container).querySelectorAll(".mem-item").length === 2);
    $(container).querySelector(".mem-item").querySelectorAll(".mini").slice(-1)[0].dispatchEvent(new Evt("click"));
    await settle(10);
    check("Delete targets the existing id " + kind, requests("DELETE", base + "/37").length === 1 && $(container).querySelectorAll(".mem-item").length === 1);
    check("Layer controls never PATCH chat config " + kind, requests("PATCH", "/api/agents/ag_1").length === 0);
  });

  await scenario("Working editor and late GET do not cross chats", async () => {
    const { client, server, $, click, open, requests } = freshClient();
    server.respond("GET", "/api/agents/ag_1/memory", { short_term: { messages: 0, summaries: [] }, working: { records: [{ seq: 9, kind: "question", content: "first chat", at: 1 }] }, long_term: { records: [] } });
    server.respond("GET", "/api/agents/ag_2/memory", { short_term: { messages: 0, summaries: [] }, working: { records: [] }, long_term: { records: [] } });
    client.init(); await settle(30); click("tab-btn-memory"); await settle(10);
    $("#mem-working").querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    const editor = $("#mem-working").querySelector(".mem-edit"); editor.value = "stale draft";
    open(1); await settle(20); editor.dispatchEvent(new Evt("keydown", { key: "Enter" })); await settle(10);
    check("Old working callback cannot PATCH a new chat", requests("PATCH", /\/working\//).length === 0 && !$("#mem-working").textContent.includes("first chat"));
    const late = deferred(); server.respond("GET", "/api/agents/ag_1/memory", () => late.promise);
    open(0); await settle(10); open(1); await settle(10);
    late.resolve(json({ short_term: { messages: 99, summaries: [] }, working: { records: [{ seq: 99, kind: "question", content: "LATE OLD RECORD" }] }, long_term: { records: [] } })); await settle(10);
    check("Late memory response does not populate neighbour", client.state.current.id === "ag_2" && !$("#mem-working").textContent.includes("LATE OLD RECORD"));
  });

  await scenario("MCP names/schema and hidden/late request cancellation", async () => {
    const { client, server, $, click, requests } = freshClient();
    const servers = [{ name: "local", status: "ok", tools: [{ name: "local__echo", description: "echo fixture", schema: { type: "object", properties: { text: { type: "string" } } } }] }, { name: "down", status: "down", error: "fixture unavailable", tools: [] }];
    server.respond("GET", "/api/mcp", { servers });
    client.init(); await settle(30); click("tab-btn-mcp"); await settle(10);
    check("MCP renders callable name, schema and down reason", $("#mcp-list").textContent.includes("local__echo") && $("#mcp-list").textContent.includes('"type": "string"') && $("#mcp-list").textContent.includes("fixture unavailable"));
    const pending = deferred(); server.respond("GET", "/api/mcp", () => pending.promise);
    click("workspace-chat"); click("workspace-settings");
    const controller = client.state.mcpRequest; click("workspace-chat");
    pending.resolve(json({ servers: [{ name: "LATE SERVER", status: "ok", tools: [] }] })); await settle(10);
    check("Leaving MCP aborts and ignores late response", controller.signal.aborted && !$("#mcp-list").textContent.includes("LATE SERVER"));
    server.respond("GET", "/api/mcp", { servers });
    const before = requests("GET", "/api/mcp").length;
    document.visibilityState = "hidden"; click("workspace-settings"); await settle(10);
    check("Hidden document does not poll MCP", requests("GET", "/api/mcp").length === before);
    document.visibilityState = "visible"; document.fire(new Evt("visibilitychange")); await settle(10);
    check("Visible MCP reopens with one GET", requests("GET", "/api/mcp").length === before + 1);
  });

  for (const classic of [false, true]) await scenario("Reminder polling lifecycle " + classic, async () => {
    const { client, server, $, click, requests } = freshClient({}, classic);
    const reminder = { id: 18, text: "<script>offline reminder</script>", every: null, due_at: 1700000000, fired: 0, state: "ждёт" };
    const recurring = { id: 29, text: "повтор", every: 60, due_at: 1700000060, fired: 4, state: "сработало" };
    const response = (item) => ({ servers: [
      { name: "remind", status: "ok", tools: [{ name: "remind", description: "fixture", schema: { type: "object" } }],
        reminders: { total: 2, waiting: item.fired ? 0 : 1, fired: item.fired ? 2 : 1, items: [item, recurring] } },
      { name: "echo", status: "ok", tools: [] },
    ] });
    server.respond("GET", "/api/mcp", response(reminder));
    client.init(); await settle(30); click("tab-btn-mcp"); await settle(10);
    const listing = $("#mcp-list");
    check("Reminder aggregate uses server counts/id/text and recurring occurrences " + classic,
      listing.querySelectorAll(".mem-reminders").length === 1 && listing.textContent.includes("ждёт: 1 · сработало: 1")
      && listing.textContent.includes("№18 — <script>offline reminder</script>") && listing.textContent.includes("раз: 4 · следующее в") && !listing.querySelector("script"));
    check("Read-only reminder page has no form or chat request " + classic,
      !listing.querySelector("input") && !listing.querySelector("button") && requests("POST", /\/messages$/).length === 0);
    listing.querySelector("details").open = true;
    const fired = { ...reminder, state: "сработало", fired: 1 };
    server.respond("GET", "/api/mcp", response(fired));
    const before = requests("GET", "/api/mcp").length;
    await settle(2100);
    check("Scheduled 2s GET refreshes fired state and preserves expanded schema " + classic,
      requests("GET", "/api/mcp").length === before + 1 && listing.textContent.includes("ждёт: 0 · сработало: 2")
      && listing.querySelector("details").open && client.state.mcpTimer !== null);

    document.visibilityState = "hidden"; document.fire(new Evt("visibilitychange"));
    const hiddenCount = requests("GET", "/api/mcp").length;
    await settle(2100);
    check("Hidden document stops scheduled GETs " + classic, requests("GET", "/api/mcp").length === hiddenCount && client.state.mcpTimer === null);
    document.visibilityState = "visible"; document.fire(new Evt("visibilitychange")); await settle(10);
    check("Visibility reentry fetches promptly and starts one timer " + classic, requests("GET", "/api/mcp").length === hiddenCount + 1 && client.state.mcpTimer !== null);
    click("tab-btn-model"); await settle(10);
    check("Leaving Tools stops timer " + classic, client.state.mcpTimer === null);

    const pending = deferred(); server.respond("GET", "/api/mcp", () => pending.promise);
    click("tab-btn-mcp"); await settle(10);
    const controller = client.state.mcpRequest;
    click("workspace-chat");
    server.respond("GET", "/api/mcp", response(fired)); click("workspace-settings"); await settle(10);
    // The aborted request resolves after the new entry has already rendered.
    pending.resolve(json({ servers: [{ name: "LATE SERVER", tools: [] }] })); await settle(10);
    check("Workspace exit aborts GET and rejects old epoch after reentry " + classic,
      controller.signal.aborted && !listing.textContent.includes("LATE SERVER") && listing.textContent.includes("ждёт: 0 · сработало: 2") && client.state.mcpTimer !== null);
    window.dispatchEvent(new Evt("pagehide"));
    const closedCount = requests("GET", "/api/mcp").length;
    await settle(2100);
    check("Pagehide stops all scheduled requests " + classic, client.state.mcpTimer === null && requests("GET", "/api/mcp").length === closedCount);
  });

  await scenario("Stop and chat switch abort the original stream", async () => {
    const { client, server, $, send, open, requests } = freshClient();
    server.respond("POST", "/api/agents/ag_1/messages", (request) => stream(success, { delay: 30, signal: request.signal }));
    server.respond("POST", "/api/agents/ag_1/cancel", () => json({ cancelled: true }));
    client.init(); await settle(30); send("stopped question"); await settle(10);
    const stopped = client.state.abort; $("#composer").requestSubmit(); await settle(40);
    check("Stop passes cancellation to fetch and the origin server", stopped.signal.aborted && requests("POST", "/api/agents/ag_1/cancel").length === 1 && !client.state.busy && client.state.prompts.size === 0);
    send("old chat question"); await settle(10);
    const switched = client.state.abort; open(1); await settle(40);
    check("Switch aborts original stream without leaking prompt or reply", switched.signal.aborted && client.state.current.id === "ag_2" && !$("#feed").textContent.includes("ответ модели") && client.state.prompts.size === 0);
  });

  await scenario("Task commands, refusal, double Enter and cross-chat PATCH", async () => {
    const { client, server, $, send, requests, open } = freshClient({ agents: [{ task: planning }, {}] });
    const taskPath = "/api/agents/ag_1/task";
    server.respond("PATCH", taskPath, () => failure("не было ни одного обмена: разложить задачу на шаги", 409));
    client.init(); await settle(30); send("/task-next"); await settle(20);
    check("Gate refusal leaves stage and command, no exchange", requests("POST", /\/messages$/).length === 0 && client.state.current.task.stage === "planning" && $("#input").value === "/task-next" && $("#composer-hint").textContent.includes("не было ни одного обмена"));
    const taskReply = deferred(); server.respond("PATCH", taskPath, () => taskReply.promise);
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(success));
    send("/task-next"); $("#composer").requestSubmit(); await settle(10);
    check("Double Enter sends one task PATCH", requests("PATCH", taskPath).length === 2 && same(requests("PATCH", taskPath).slice(-1)[0].body, { move: "next", from: "planning" }));
    server.state.agents[0].task = execution; taskReply.resolve(json({ task: execution })); await settle(30);
    check("Transition exchange follows PATCH exactly once", requests("POST", "/api/agents/ag_1/messages").length === 1 && requests("POST", "/api/agents/ag_1/messages")[0].body.text === "Приступай к работе.");
    const crossChat = deferred(); server.respond("PATCH", taskPath, () => crossChat.promise);
    send("/task-next"); await settle(10); open(1); await settle(20);
    server.state.agents[0].task = { ...execution, stage: "validation", label: "проверка" };
    crossChat.resolve(json({ task: server.state.agents[0].task })); await settle(20);
    check("Late task PATCH never sends transition into neighbour", client.state.current.id === "ag_2" && requests("POST", /\/messages$/).length === 1 && $("#task-head").classList.contains("hidden"));
    open(0); await settle(20);
    check("Reopening origin shows server stage", client.state.current.task.stage === "validation");
  });

  await scenario("Branch uses answer index and server-owned identity", async () => {
    const { client, server, $, requests } = freshClient({ agents: [{ transcript: turns, history_len: 2 }] });
    const child = { ...server.state.agents[0], id: "branch-7", label: "server branch", branch: { parent_id: "ag_1", forked_at: 2 } };
    server.respond("POST", "/api/agents/ag_1/fork", () => { server.state.agents.push(child); return json({ created: 1, agents: [child] }); });
    client.init(); await settle(30);
    button($("#feed").querySelector(".card"), "Ветка отсюда").dispatchEvent(new Evt("click")); await settle(20);
    check("Branch request includes question and answer", same(requests("POST", "/api/agents/ag_1/fork")[0]?.body, { at: 2 }));
    check("Client opens returned branch and preserves parent", client.state.current.id === "branch-7" && server.state.agents[0].history_len === 2 && $("#branch-note").textContent.includes("первый чат"));
    server.respond("POST", "/api/agents/branch-7/regenerate", () => stream(success));
    button($("#feed").querySelector(".card"), "Перегенерировать").dispatchEvent(new Evt("click")); await settle(20);
    check("Regeneration sends no copied history or question body", requests("POST", "/api/agents/branch-7/regenerate").length === 1 && requests("POST", "/api/agents/branch-7/regenerate")[0].body === null);
  });
}

main().then(async () => {
  await settle(10);
  if (failures.length) {
    console.error(`ПРОВАЛЕНО ${failures.length} из ${passed + failures.length}:`);
    for (const problem of failures) console.error("  - " + problem);
    process.exitCode = 1;
  } else console.log(`ОК: ${passed} утверждений о клиенте`);
});
