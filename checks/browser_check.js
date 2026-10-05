// Execute the real vanilla client against explicit offline HTTP/SSE scenarios.
// Server computations are tested in Python; this file checks the UI boundary.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { TextDecoder, TextEncoder } = require("util");
const { boot, settle, Evt } = require("./dom.js");
const { json, failure, deferred, stream, start, success, turns, planning, execution } = require("./fixtures.js");
const STATIC = process.argv[2] || path.join(__dirname, "..", "app", "static");
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
  const click = id => {
    if (id === "chat-settings") return $("#agent-list").querySelectorAll(".item-settings").find(row => row.dataset.agentId === client.state.current?.id)?.dispatchEvent(new Evt("click"));
    if (id === "application-settings") return $("#app-settings").dispatchEvent(new Evt("click"));
    return $("#" + id).dispatchEvent(new Evt("click"));
  };
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
  await scenario("Answered partial facts show neutral source cards without exact-quote claims", async () => {
    const rag = {version: 3, query: "neutral compound", config: {rewrite_enabled: false, candidates_k: 10, final_k: 3, rerank_enabled: false},
      answer_policy: {weak_context_enabled: true, similarity_threshold: .3},
      hits: [{chunk_id: "first", text: "First neutral object is blue.", score: .8}, {chunk_id: "second", text: "Second neutral object is green.", score: .6}],
      answer: {status: "answered", citations: [{source_id: 1, chunk_id: "first", source: "fixture:first", title: "First", quote: "Первый объект синего цвета"},
        {source_id: 2, chunk_id: "second", source: "fixture:second", title: "Second", quote: "Второй <b>зелёный</b> объект"}]}};
    const {client, $, requests} = freshClient({agents: [{rag_enabled: true, transcript: [turns[0], {...turns[1], content: "Первый синий [1], второй зелёный [2]. Данных о массе третьего нет.", rag}], history_len: 2}]});
    client.init(); await settle();
    const card = $("#feed").querySelector(".card"), cards = card.querySelectorAll(".rag-source-card");
    check("New status labels sources neutrally and retains the supported partial answer", card.textContent.includes("Источники и цитаты") && !card.textContent.includes("Цитаты проверены") && card.textContent.includes("Данных о массе третьего нет"));
    check("Paraphrased quotes are displayed as inert text with matching source links", cards.length === 2 && cards[1].querySelector("blockquote").textContent === "Второй <b>зелёный</b> объект" && !cards[1].querySelector("b") && (card.querySelector(".card-body").innerHTML.match(/class="rag-citation-ref"/g) || []).length === 2);
    const raw = button(card, "Показать сырой текст"); raw.dispatchEvent(new Evt("click")); raw.dispatchEvent(new Evt("click"));
    check("Neutral answered references survive raw view restoration", card.querySelector(".card-body").innerHTML.includes('href="#rag-source-1-2"'));
    const before = requests("GET", /^\/api\/rag\//).length;
    card.querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const historical = $("#rag-answer-snapshot");
    check("Answered history shows saved quotes and no verification claim or index fetch", historical.querySelectorAll(".rag-snapshot-citation").length === 2 && historical.textContent.includes("Источники и цитаты") && !historical.textContent.includes("Цитаты проверены") && requests("GET", /^\/api\/rag\//).length === before);
  });

  await scenario("Answer without inline references retains source cards and unchanged text", async () => {
    const content = "Первый объект синий. Данных о массе нет.";
    const rag = {version: 3, hits: [{chunk_id: "neutral", text: "Neutral item is blue.", score: .8}],
      answer: {status: "answered", citations: [{source_id: 1, chunk_id: "neutral", source: "fixture:neutral", title: "Neutral", quote: "Neutral <b>blue</b> item."}]}};
    const {client, $} = freshClient({agents: [{rag_enabled: true, transcript: [turns[0], {...turns[1], content, rag}], history_len: 2}]});
    client.init(); await settle();
    const card = $("#feed").querySelector(".card"), body = card.querySelector(".card-body"), sources = card.querySelectorAll(".rag-source-card");
    check("No-inline answer renders unchanged with no invented reference", body.textContent === content && !body.innerHTML.includes("rag-citation-ref"));
    check("No-inline citations retain safe source cards", sources.length === 1 && sources[0].id === "rag-source-1-1" && sources[0].querySelector("blockquote").textContent === "Neutral <b>blue</b> item." && !sources[0].querySelector("b"));
    const raw = button(card, "Показать сырой текст"); raw.dispatchEvent(new Evt("click")); raw.dispatchEvent(new Evt("click"));
    check("No-inline text and cards survive raw-view restoration", body.textContent === content && !body.innerHTML.includes("rag-citation-ref") && card.querySelectorAll(".rag-source-card").length === 1);
  });

  await scenario("Verified citations use saved ranks, inert quotes and safe local references", async () => {
    const rag = {version: 2, query: "neutral question", config: {rewrite_enabled: false, filter_enabled: false},
      answer_policy: {weak_context_enabled: true, similarity_threshold: .4},
      hits: [{chunk_id: "first", text: "unreferenced fragment"}, {chunk_id: "second", text: "<b>exact neutral quote</b>"},
        {chunk_id: "third", text: "neutral third quote"}], context: "exact pinned context",
      answer: {status: "verified", citations: [
        {source_id: 2, chunk_id: "second", source: "javascript:alert(1)", title: "<script>neutral title</script>", section: "section", quote: "<b>exact neutral quote</b>"},
        {source_id: 3, chunk_id: "third", source: "https://example.test/source", title: "Third source", quote: "neutral third quote"}]}};
    const {client, $, requests} = freshClient({agents: [{rag_enabled: true, rag_filter_enabled: false,
      transcript: [turns[0], {...turns[1], content: "Supported **answer** [2] and [3]. Code `[2]`.\n\n```\n[3]\n```", rag}], history_len: 2}]});
    client.init(); await settle();
    const card = $("#feed").querySelector(".card"), sources = card.querySelectorAll(".rag-source-card");
    check("Only cited saved ranks become cards, with exact inert quote text", sources.length === 2
      && sources[0].id === "rag-source-1-2" && sources[1].id === "rag-source-1-3"
      && sources[0].attributes.value === "2" && sources[0].querySelector("blockquote").textContent === "<b>exact neutral quote</b>"
      && !sources[0].querySelector("script") && !sources[0].querySelector("a")
      && sources[1].querySelector("a").attributes.href === "https://example.test/source");
    const rendered = card.querySelector(".card-body").innerHTML;
    check("Inline references link matching cards while preserving code", (rendered.match(/class="rag-citation-ref"/g) || []).length === 2
      && rendered.includes('href="#rag-source-1-2"') && rendered.includes('href="#rag-source-1-3"')
      && rendered.includes("<strong>answer</strong>") && rendered.includes("<code>[2]</code>")
      && rendered.includes("<pre><code>[3]</code></pre>"));
    check("Citation markup cannot leak into URL attributes or unverified text", !client.renderMarkdown("[2]", {}).includes("rag-citation-ref")
      && !client.renderMarkdown("[url](https://example.test/[2])", {2: "rag-source-1-2"}).includes("rag-citation-ref")
      && !client.renderMarkdown("[label][2] [2]: ref https://example.test/[2] \\[2]", {2: "rag-source-1-2"}).includes("rag-citation-ref")
      && !client.renderMarkdown("[2]", {2: 'rag-source-x" onclick="bad'}).includes("rag-citation-ref"));
    const raw = button(card, "Показать сырой текст"); raw.dispatchEvent(new Evt("click")); raw.dispatchEvent(new Evt("click"));
    check("Returning from raw view restores verified references", card.querySelector(".card-body").innerHTML === rendered);
    const before = requests("GET", /^\/api\/rag\//).length;
    card.querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const historical = $("#rag-answer-snapshot");
    check("Saved validation and shared safeguard inspect independently of filter toggle", historical.querySelectorAll(".rag-snapshot-citation").length === 2
      && historical.textContent.includes("Цитаты проверены") && historical.textContent.includes("similarity_threshold")
      && !historical.querySelector(".rag-snapshot-candidate") && requests("GET", /^\/api\/rag\//).length === before
      && $("#rag-current-chat").hidden && $("#save-status").classList.contains("hidden"));
  });
  await scenario("Shared safeguard stays visible without filtering; refusals and receipts have no citations", async () => {
    const {client, $, click, requests} = freshClient({agents: [{rag_enabled: true, rag_filter_enabled: false,
      transcript: [turns[0], {...turns[1], rag: {hits: [{title: "uncited neutral fragment"}], answer: {status: "insufficient", citations: []}}},
        {...turns[1], rag: {hits: [{title: "receipt fragment"}], answer: {status: "receipt", citations: []}}}], history_len: 3}]});
    client.init(); await settle();
    check("Refusals and receipts render no fabricated source cards", !$("#feed").querySelector(".rag-source-card")
      && !$("#feed").querySelector(".rag-answer-sources") && $("#feed").textContent.includes("недостаточно информации")
      && $("#feed").textContent.includes("напоминание запланировано"));
    click("chat-settings"); click("tab-btn-rag"); await settle();
    check("RAG ON reveals the shared threshold with Filtering OFF", !$("#rag-chat-settings").classList.contains("hidden")
      && !$("#rag-chat-parameters").classList.contains("hidden") && !$("#rag-threshold-field").classList.contains("hidden")
      && $("#rag-threshold-field").querySelector("#f-rag_similarity_threshold") === $("#f-rag_similarity_threshold"));
    $("#f-rag_similarity_threshold").value = ".55"; $("#f-rag_similarity_threshold").dispatchEvent(new Evt("change")); await settle();
    check("Visible shared threshold saves with Filtering OFF", requests("PATCH", "/api/agents/ag_1").at(-1).body.rag_similarity_threshold === .55
      && !("rag_filter_enabled" in requests("PATCH", "/api/agents/ag_1").at(-1).body));
    $("#f-rag_enabled").checked = false; $("#f-rag_enabled").dispatchEvent(new Evt("change")); await settle();
    check("RAG OFF hides safeguard while preserving its value", $("#rag-chat-settings").classList.contains("hidden") && Number($("#f-rag_similarity_threshold").value) === .55);
  });
  await scenario("Independent reasoning toggles in every model selector", async () => {
    const {client, $, click, open, requests} = freshClient({agents: [{reasoning_enabled: true, rag_rerank_reasoning_enabled: false}, {reasoning_enabled: false}]});
    client.init(); await settle(); click("chat-settings"); await settle();
    const ids = ["f-reasoning_enabled", "f-rag_rerank_reasoning_enabled", "rag-semantic-model-reasoning", "rag-preparation-model-reasoning", "rag-model-reasoning"];
    check("All five common selectors have independent conditional-support toggles", ids.every(id => $("#" + id)?.type === "checkbox") && $("#chat-model-picker").textContent.includes("если модель поддерживает"));
    check("Chat ON does not enable the four independent scenarios", $("#f-reasoning_enabled").checked && ids.slice(1).every(id => !$("#" + id).checked));
    $("#f-rag_rerank_reasoning_enabled").checked = true; $("#f-rag_rerank_reasoning_enabled").dispatchEvent(new Evt("change")); await settle();
    check("Chat PATCH persists distinct reasoning choices", requests("PATCH", "/api/agents/ag_1").at(-1).body.reasoning_enabled === true && requests("PATCH", "/api/agents/ag_1").at(-1).body.rag_rerank_reasoning_enabled === true);
    open(1); await settle(); check("Other chat defaults reasoning OFF", !$("#f-reasoning_enabled").checked && !$("#f-rag_rerank_reasoning_enabled").checked);
    open(0); await settle(); check("Saved chat reasoning restores independently", $("#f-reasoning_enabled").checked && $("#f-rag_rerank_reasoning_enabled").checked);
    const semantic = $("#rag-semantic-model-reasoning"); semantic.checked = true; semantic.dispatchEvent(new Evt("change"));
    check("Chunking ON cannot enable preparation or embeddings", semantic.checked && !$("#rag-preparation-model-reasoning").checked && !$("#rag-model-reasoning").checked);
    const before = requests("PATCH", "/api/agents/ag_1").length;
    click("application-settings"); click("tab-btn-rag"); await settle();
    $("#rag-model-reasoning").checked = true; $("#rag-model-reasoning").dispatchEvent(new Evt("change"));
    click("rag-embedding-model-refresh"); await settle();
    check("Embedding catalogue/status preserve its local dirty flag without chat PATCH", $("#rag-model-reasoning").checked && semantic.checked && requests("PATCH", "/api/agents/ag_1").length === before);
  });

  await scenario("Reminder capability discovery, disconnect fallback and stale owner", async () => {
    const {client, server, $, click, open, requests} = freshClient();
    const connected = {servers: [{name: "arbitrary-neutral", status: "ok", tools: [{name: "neutral__reminders", schema: {}}], reminders: {items: []}}]};
    server.respond("GET", "/api/mcp", {servers: [{name: "reminders", status: "ok", tools: [{name: "remind"}]}]});
    client.init(); await settle(); click("chat-settings"); await settle();
    check("Server name or a scheduling-looking tool cannot invent reminder capability", $("#tab-btn-mcp").hidden);
    const count = requests("GET", "/api/mcp").length; await settle(2100);
    check("Capability discovery does not start a global background poll", requests("GET", "/api/mcp").length === count && client.state.mcpTimer === null);
    server.respond("GET", "/api/mcp", connected); click("chat-settings"); await settle(); click("tab-btn-mcp"); await settle();
    check("Connected empty reminder list has a visible pane without catalogue", !$("#tab-btn-mcp").hidden && client.state.section === "mcp" && $("#mcp-list").textContent.includes("напоминаний нет") && !$("#mcp-list").querySelector(".mcp-tool"));
    server.respond("GET", "/api/mcp", {servers: [{...connected.servers[0], reminders: undefined, reminders_error: "neutral list unavailable"}]});
    document.fire(new Evt("visibilitychange")); await settle();
    check("Connected protocol list failure preserves reminder pane with actual error", !$("#tab-btn-mcp").hidden && $("#mcp-list").textContent.includes("neutral list unavailable"));
    server.respond("GET", "/api/mcp", {servers: [{...connected.servers[0], status: "disconnected"}]});
    $("#tab-btn-mcp").focus(); await settle(2100);
    check("Disconnect removes active pane and returns keyboard focus to visible model tab", $("#tab-btn-mcp").hidden && client.state.section === "model" && document.activeElement === $("#tab-btn-model") && client.state.mcpTimer === null);
    $("#tab-btn-memory").dispatchEvent(new Evt("keydown", {key: "ArrowDown"}));
    check("Keyboard navigation skips unavailable reminders", client.state.section === "rag");
    const late = deferred(); server.respond("GET", "/api/mcp", () => late.promise); click("chat-settings"); await settle();
    const old = requests("GET", "/api/mcp").at(-1); click("tab-btn-agent");
    check("Same-chat tab change preserves entry capability discovery", !old.signal.aborted && requests("GET", "/api/mcp").at(-1) === old);
    open(1); await settle();
    late.resolve(json(connected)); await settle();
    check("Old owner's discovery cannot update capability after chat switch", old.signal.aborted && client.state.current.id === "ag_2" && !client.state.remindersAvailable);
    server.respond("GET", "/api/mcp", connected); click("chat-settings"); await settle(); click("tab-btn-mcp"); await settle();
    server.respond("GET", "/api/mcp", () => failure("neutral discovery failed"));
    document.fire(new Evt("visibilitychange")); await settle();
    check("Owned failed GET removes unverified availability safely", !client.state.remindersAvailable && $("#tab-btn-mcp").hidden && client.state.section === "model");
  });

  await scenario("Deleting the last chat creates exactly one replacement; failures keep no ghost", async () => {
    const {client, server, $, click, requests} = freshClient({agents: [{transcript: turns, history_len: 2}]});
    client.init(); await settle();
    const replacement = {...server.state.agents[0], id: "replacement", label: "Replacement", transcript: [], history_len: 0};
    server.respond("DELETE", "/api/agents/ag_1", () => {server.state.agents = []; return json({deleted: "ag_1"});});
    server.respond("GET", "/api/agents", () => json({agents: server.state.agents, has_key: true}));
    server.respond("POST", "/api/agents", () => {server.state.agents = [replacement]; return json({agents: [replacement]});});
    server.respond("GET", "/api/agents/replacement", replacement);
    button($("#agent-list"), "Удалить чат").dispatchEvent(new Evt("click")); $(".confirm-row").querySelector(".primary").dispatchEvent(new Evt("click")); await settle();
    check("Successful last deletion creates one replacement and opens it", requests("POST", "/api/agents").length === 1 && client.state.current.id === "replacement" && !$("#feed").textContent.includes("ответ модели"));
    server.respond("DELETE", "/api/agents/replacement", () => failure("neutral deletion refused"));
    button($("#agent-list"), "Удалить чат").dispatchEvent(new Evt("click")); $(".confirm-row").querySelector(".primary").dispatchEvent(new Evt("click")); await settle();
    check("Failed deletion keeps the current chat and creates nothing", client.state.current.id === "replacement" && requests("POST", "/api/agents").length === 1);
    client.state.prompts.set("replacement:1", "neutral old prompt"); client.state.lastMetrics = {total_tokens: 50}; client.state.busy = true;
    server.respond("DELETE", "/api/agents/replacement", () => {server.state.agents = []; return json({deleted: "replacement"});});
    server.respond("POST", "/api/agents", () => failure("neutral replacement failed"));
    button($("#agent-list"), "Удалить чат").dispatchEvent(new Evt("click")); $(".confirm-row").querySelector(".primary").dispatchEvent(new Evt("click")); await settle();
    check("Failed replacement clears deleted transcript, metrics, prompts and busy", !client.state.current && !client.state.lastMetrics && !client.state.busy && !client.state.prompts.has("replacement:1") && !$("#feed").textContent.includes("Replacement"));
    check("Failed replacement exposes actual error and explicit new-chat retry", $("#agent-list").textContent.includes("neutral replacement failed") && !$("#new-chat").disabled);
    server.respond("POST", "/api/agents", () => {server.state.agents = [replacement]; return json({agents: [replacement]});});
    click("new-chat"); await settle();
    check("Explicit retry creates and selects a chat after replacement failure", client.state.current.id === "replacement" && !client.state.chatCreationError);
  });

  await scenario("Concurrent confirmed deletions claim a single replacement", async () => {
    const {client, server, $, requests} = freshClient(); client.init(); await settle();
    const first = deferred(), second = deferred(), created = deferred();
    server.respond("DELETE", "/api/agents/ag_1", () => first.promise);
    server.respond("DELETE", "/api/agents/ag_2", () => second.promise);
    server.respond("GET", "/api/agents", {agents: [], has_key: true});
    server.respond("POST", "/api/agents", () => created.promise);
    const rows = $("#agent-list").querySelectorAll(".item");
    for (const row of rows) { button(row, "Удалить чат").dispatchEvent(new Evt("click")); $(".confirm-row").querySelector(".primary").dispatchEvent(new Evt("click")); }
    first.resolve(json({deleted: "ag_1"})); second.resolve(json({deleted: "ag_2"})); await settle();
    check("Two successful deletion completions issue exactly one automatic creation", requests("POST", "/api/agents").length === 1);
    created.resolve(failure("neutral cleanup creation failed")); await settle();
  });

  await scenario("Late last deletion cannot replace a newer user's creation or global navigation", async () => {
    const {client, server, $, click, requests} = freshClient({agents: [{}]}); client.init(); await settle();
    const late = deferred(); server.respond("DELETE", "/api/agents/ag_1", () => late.promise);
    const newer = {...server.state.agents[0], id: "newer", label: "User choice"};
    server.respond("GET", "/api/agents", () => json({agents: server.state.agents, has_key: true}));
    server.respond("POST", "/api/agents", () => {server.state.agents = [newer]; return json({agents: [newer]});}); server.respond("GET", "/api/agents/newer", newer);
    button($("#agent-list"), "Удалить чат").dispatchEvent(new Evt("click")); $(".confirm-row").querySelector(".primary").dispatchEvent(new Evt("click"));
    click("new-chat"); await settle(); click("application-settings");
    late.resolve(json({deleted: "ag_1"})); await settle();
    check("Late delete does not create twice or steal current/global navigation", requests("POST", "/api/agents").length === 1 && client.state.current.id === "newer" && client.state.settingsScope === "app" && client.state.workspace === "settings");
  });

  await scenario("Settings domains, keyboard navigation and mounted drafts", async () => {
    const {client, server, $, click, requests} = freshClient();
    server.respond("GET", "/api/profile", {profile: {style: "neutral style"}});
    server.respond("GET", "/api/memory", {records: [{seq: 1, kind: "knowledge", content: "global record"}]});
    server.respond("GET", "/api/agents/ag_1/memory", {short_term: {messages: 0}, working: {records: []}, long_term: {records: []}});
    client.init(); await settle();
    $("#input").value = "composer draft";
    click("chat-settings"); await settle();
    check("Chat gear opens selected-chat model settings", client.state.settingsScope === "chat" && client.state.section === "model" && !$("#model-chat-settings").hidden && $("#model-app-settings").hidden);
    check("Chat navigation hides reminders without connected capability", same(document.querySelectorAll(".tab").filter(t => !t.hidden).map(t => t.dataset.tab), ["model", "agent", "memory", "rag"]));
    $("#f-system").value = "system draft"; $("#f-system").dispatchEvent(new Evt("input"));
    click("tab-btn-memory"); await settle();
    $("#mem-work-content").value = "working draft";
    check("Chat memory hides global form and reads only aggregate chat memory", !$("#mem-working-layer").hidden && !$("#mem-short-layer").hidden && $("#mem-long-layer").hidden && requests("GET", "/api/memory").length === 0);
    click("application-settings"); await settle();
    check("Avatar opens global model connection without chat form", client.state.settingsScope === "app" && !$("#model-app-settings").hidden && $("#model-chat-settings").hidden);
    check("Application navigation exposes only its six sections", same(document.querySelectorAll(".tab").filter(t => !t.hidden).map(t => t.dataset.tab), ["model", "memory", "profile", "invariants", "mcp", "rag"]));
    const before = requests("PATCH", "/api/agents/ag_1").length;
    $("#f-system").dispatchEvent(new Evt("change")); await settle();
    check("Hidden chat controls cannot PATCH from application settings", requests("PATCH", "/api/agents/ag_1").length === before);
    $("#tab-btn-model").dispatchEvent(new Evt("keydown", {key: "ArrowDown"})); await settle();
    check("Application keyboard skips hidden agent tab", client.state.section === "memory");
    check("Application memory uses global endpoint and hides personal forms", !$("#mem-long-layer").hidden && $("#mem-working-layer").hidden && $("#mem-short-layer").hidden && requests("GET", "/api/memory").length === 1 && $("#mem-long").textContent.includes("global record"));
    $("#mem-content").value = "global draft";
    const getCount = requests("GET", "/api/agents/ag_1").length;
    click("chat-settings"); await settle();
    check("Same-chat gear preserves drafts without reopening chat", requests("GET", "/api/agents/ag_1").length === getCount && $("#f-system").value === "system draft" && $("#mem-work-content").value === "working draft" && $("#mem-content").value === "global draft");
    click("tab-btn-rag"); await settle();
    check("Chat search never polls the index or shows pipeline", requests("GET", /^\/api\/rag\//).length === 0 && $("#rag-workflow").hidden && !$("#rag-current-chat").hidden);
    click("application-settings"); click("tab-btn-rag"); await settle();
    check("Application index hides chat usage and fetches status", !$("#rag-workflow").hidden && $("#rag-current-chat").hidden && requests("GET", "/api/rag/status").length === 1);
    click("workspace-chat");
    check("Explicit return preserves composer and mounted forms", client.state.workspace === "chat" && $("#input").value === "composer draft" && $("#workspace-chat").hidden);
  });

  await scenario("No-chat application settings and late navigation ownership", async () => {
    const empty = freshClient({agents: []});
    empty.server.respond("GET", "/api/memory", {records: []});
    empty.client.init(); await settle(); empty.click("application-settings"); empty.click("tab-btn-memory"); await settle();
    check("No-chat globals do not create or PATCH a chat", !empty.client.state.current && empty.requests("POST", "/api/agents").length === 0 && empty.requests("GET", "/api/memory").length === 1 && empty.requests("PATCH", /^\/api\/agents\//).length === 0);
    empty.click("tab-btn-mcp"); await settle();
    check("No-chat MCP global GET has no chat header", !empty.requests("GET", "/api/mcp").at(-1).headers?.["X-Chat-ID"] && !empty.$("#mcp-application-controls").hidden);
    window.dispatchEvent(new Evt("pagehide"));
    const {client, server, $, click, requests} = freshClient(); client.init(); await settle();
    const late = deferred(); server.respond("GET", "/api/agents/ag_2", () => late.promise);
    $("#agent-list").querySelectorAll(".item-settings")[1].dispatchEvent(new Evt("click")); await settle();
    click("application-settings");
    late.resolve(json(server.state.agents[1])); await settle();
    check("Late gear response cannot steal avatar navigation or current chat", client.state.settingsScope === "app" && client.state.current.id === "ag_1");
    server.respond("GET", "/api/agents/ag_2", server.state.agents[1]);
    $("#agent-list").querySelectorAll(".item-settings")[1].dispatchEvent(new Evt("click")); await settle();
    check("Row gear opens precisely its chat and settings", client.state.current.id === "ag_2" && client.state.settingsScope === "chat" && client.state.workspace === "settings");
    const created = deferred(); server.respond("POST", "/api/agents", () => created.promise);
    click("new-chat"); click("application-settings");
    created.resolve(json({agents: [{...server.state.agents[0], id: "created"}]})); await settle();
    check("Late creation cannot replace newer global navigation", client.state.settingsScope === "app" && client.state.workspace === "settings" && client.state.current.id === "ag_2");
  });

  await scenario("An old empty agent list cannot replace a newer created chat", async () => {
    const {client, server, $, click} = freshClient();
    const initial = deferred(); server.respond("GET", "/api/agents", () => initial.promise);
    client.init(); await settle();
    server.respond("GET", "/api/agents", {agents: server.state.agents, has_key: true});
    server.respond("POST", "/api/agents", {agents: [server.state.agents[0]]});
    click("new-chat"); await settle();
    initial.resolve(json({agents: [], has_key: true})); await settle();
    check("Newer list and selected chat survive a delayed initial empty response", client.state.current.id === "ag_1" && $("#agent-list").querySelectorAll(".item").length === 2);
  });

  await scenario("Global tools hide owned reminders and stale cancellation cannot target another chat", async () => {
    const {client, server, $, click, open, requests} = freshClient();
    server.respond("GET", "/api/mcp", {servers: [{name: "remind", status: "ok", url: "http://127.0.0.1:8018/mcp", tools: [], reminders: {items: [{id: 5, can_cancel: true, text: "neutral owned reminder", fired: 0, state: "ждёт"}]}}], config: {revision: 1, servers: []}});
    client.init(); await settle(); click("chat-settings"); await settle(); click("tab-btn-mcp"); await settle();
    const cancel = $("#mcp-list").querySelector("button");
    check("Chat tools have owned reminder actions and no server configuration", cancel.textContent === "Снять" && $("#mcp-application-controls").hidden && !$("#mcp-list").querySelector(".mcp-config-actions") && requests("GET", "/api/mcp").at(-1).headers["X-Chat-ID"] === "ag_1");
    open(1); await settle(); cancel.dispatchEvent(new Evt("click")); await settle();
    check("Detached reminder button cannot cancel under another chat", requests("POST", /\/reminders\//).length === 0);
    click("application-settings"); click("tab-btn-mcp"); await settle();
    check("Global tools show connections without reminder or chat headers", !$("#mcp-application-controls").hidden && !$("#mcp-list").querySelector(".mem-reminders") && !!$("#mcp-list").querySelector(".mcp-config-actions") && !requests("GET", "/api/mcp").at(-1).headers["X-Chat-ID"]);
  });

  await scenario("Per-chat RAG setting and immutable answer snapshot", async () => {
    const rag = {version: 1, query: "neutral original question", top_k: 5,
      index: {index_id: "saved-old-index", strategy: "semantic", dimension: 3}, duration_seconds: .2,
      hits: [{chunk_id: "old-chunk", document_id: "old-doc", title: "<script>neutral source</script>",
        source: "javascript:alert(1)", section: "Historic section", start: 0, end: 19, text: "<b>old exact text</b>", score: .9234}],
      context: "[neutral context]\n<b>old exact text</b>"};
    const {client, server, $, click, open, requests} = freshClient({agents: [{transcript: [turns[0], {...turns[1], rag}], history_len: 2}, {rag_enabled: true}]});
    client.init(); await settle();
    check("Legacy RAG defaults OFF", !$("#f-rag_enabled").checked);
    check("Chat RAG controls belong to the mounted RAG page", $("#tab-rag").querySelector("#f-rag_enabled") === $("#f-rag_enabled") && !$("#tab-agent").querySelector("#f-rag_enabled"));
    click("chat-settings"); click("tab-btn-rag");
    $("#f-rag_enabled").checked = true; $("#f-rag_enabled").dispatchEvent(new Evt("change")); await settle();
    check("Checkbox saves boolean per-chat config", requests("PATCH", "/api/agents/ag_1").at(-1).body.rag_enabled === true && client.state.current.rag_enabled === true);
    open(1); await settle(); click("chat-settings"); check("Other chat loads own enabled setting", $("#f-rag_enabled").checked);
    $("#f-rag_enabled").checked = false; $("#f-rag_enabled").dispatchEvent(new Evt("change")); await settle();
    open(0); await settle(); click("chat-settings"); click("tab-btn-rag"); check("Chat switching restores persisted RAG choice", $("#f-rag_enabled").checked);
    click("workspace-chat");
    const sourceBox = $("#feed").querySelector(".card-rag");
    check("Answer sources render as inert text", sourceBox.textContent.includes("<script>neutral source</script>") && !sourceBox.querySelector("a"));
    const late = deferred(); server.respond("GET", "/api/rag/status", () => late.promise);
    click("chat-settings"); click("tab-btn-rag"); await settle();
    const lateSettings = deferred(); server.respond("PATCH", "/api/agents/ag_1", () => lateSettings.promise);
    $("#f-rag_enabled").checked = false; $("#f-rag_enabled").dispatchEvent(new Evt("change")); await settle();
    click("workspace-chat");
    const before = requests("GET", /^\/api\/rag\//).length;
    sourceBox.querySelector("button").dispatchEvent(new Evt("click"));
    lateSettings.resolve(json({...server.state.agents[0], rag_enabled: false}));
    late.resolve(json({state: "ready", index: {index_id: "new-index", rows: {documents: 0, chunks: 0}}})); await settle();
    const snapshot = $("#rag-answer-snapshot");
    check("Historical inspector uses saved identity/context without live reads", !snapshot.hidden && $("#rag-workflow").hidden
      && snapshot.textContent.includes("saved-old-index") && snapshot.querySelector(".rag-snapshot-context").textContent === rag.context
      && snapshot.querySelector(".rag-snapshot-text").textContent === rag.hits[0].text
      && requests("GET", /^\/api\/rag\//).length === before);
    check("Legacy snapshot has one query and no refinement details", !snapshot.querySelector(".rag-snapshot-original-query") && snapshot.querySelector(".rag-snapshot-query").textContent === rag.query && !snapshot.textContent.includes("Переформулирование") && !snapshot.querySelector(".rag-snapshot-candidate"));
    check("Late current-index response cannot overwrite historical snapshot", !snapshot.textContent.includes("new-index") && snapshot.textContent.includes("0.9234"));
    check("Historical inspection hides chat editing and its save status", $("#rag-current-chat").hidden && $("#f-rag_enabled").disabled && $("#save-status").classList.contains("hidden") && !$("#settings-scope").hidden);
    server.respond("GET", "/api/rag/status", {state: "missing"});
    snapshot.querySelector("button").dispatchEvent(new Evt("click")); await settle();
    check("History returns to application index without chat controls", client.state.settingsScope === "app" && snapshot.hidden && !$("#rag-workflow").hidden && $("#rag-status").textContent === "Индекс отсутствует" && $("#rag-current-chat").hidden && $("#f-rag_enabled").disabled && $("#save-status").classList.contains("hidden"));
    click("workspace-chat"); $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click"));
    open(1); await settle();
    check("Selecting a chat clears history and returns to conversation", snapshot.hidden && !snapshot.children.length && client.state.workspace === "chat" && client.state.current.id === "ag_2");
  });
  await scenario("RAG refinement fields and saved candidate decisions", async () => {
    const rag = {version: 2, original_query: "А что затем?", query: "neutral rewritten query", index: {index_id: "neutral-v2"},
      config: {rewrite_enabled: true, filter_enabled: true, candidates_k: 20, final_k: 5, similarity_threshold: .3},
      history_used: [{user: "neutral earlier question", assistant: "neutral earlier answer"}],
      rewrite: {enabled: true, model: "neutral/model", query: "neutral rewritten query", usage: null, duration_seconds: .4},
      timings: {rewrite_seconds: .4, retrieval_seconds: .1}, context: "neutral kept context",
      hits: [{chunk_id: "kept", title: "neutral kept", score: .8, text: "neutral kept context"}],
      candidates: [{chunk_id: "kept", score: .8, text: "neutral kept context", decision: "kept"},
        {chunk_id: "below", score: .2, text: "neutral excluded", decision: "threshold"},
        {chunk_id: "cap", score: .7, text: "neutral cap", decision: "final_cap"}]};
    const {client, $, click, requests, open} = freshClient({agents: [{transcript: [turns[0], {...turns[1], rag}], history_len: 2},
      {rag_enabled: true, rag_rewrite_enabled: true, rag_filter_enabled: true, rag_candidates_k: 30, rag_final_k: 7, rag_similarity_threshold: .4}]});
    client.init(); await settle(); click("chat-settings"); click("tab-btn-rag");
    check("Legacy rewrite stays off with default candidate and answer counts", !$("#f-rag_rewrite_enabled").checked && $("#f-rag_candidates_k").value === "10" && $("#f-rag_final_k").value === "3" && $("#rag-chat-settings").classList.contains("hidden"));
    $("#f-rag_rewrite_enabled").checked = true;
    $("#f-rag_candidates_k").value = "12"; $("#f-rag_final_k").value = "4";
    $("#f-rag_rewrite_enabled").dispatchEvent(new Evt("change")); await settle();
    const patch = requests("PATCH", "/api/agents/ag_1").at(-1).body;
    check("Both counts and independent rewrite save without obsolete filter fields", patch.rag_rewrite_enabled === true && patch.rag_candidates_k === 12 && patch.rag_final_k === 4 && patch.rag_enabled === false && !("rag_top_k" in patch) && !("rag_filter_enabled" in patch) && patch.rag_similarity_threshold === .3);
    const count = requests("PATCH", "/api/agents/ag_1").length;
    $("#f-rag_candidates_k").value = "1.5"; $("#f-rag_candidates_k").dispatchEvent(new Evt("change")); await settle();
    check("Fractional candidate count blocks PATCH and preserves input", requests("PATCH", "/api/agents/ag_1").length === count && $("#f-rag_candidates_k").value === "1.5" && $("#save-status").textContent.includes("целое"));
    $("#f-rag_candidates_k").value = "3"; $("#f-rag_candidates_k").dispatchEvent(new Evt("change")); await settle();
    check("Answer count exceeding candidates blocks one atomic PATCH", requests("PATCH", "/api/agents/ag_1").length === count && $("#f-rag_final_k").value === "4" && $("#save-status").textContent.includes("больше"));
    $("#f-rag_candidates_k").value = "12";
    open(1); await settle(); check("Switch loads independent counts", $("#f-rag_candidates_k").value === "30" && $("#f-rag_final_k").value === "7" && !$("#rag-chat-settings").classList.contains("hidden") && !$("#rag-chat-parameters").classList.contains("hidden"));
    open(0); await settle(); check("Switch restores saved counts", $("#f-rag_candidates_k").value === "12" && $("#f-rag_final_k").value === "4" && $("#f-rag_rewrite_enabled").checked && $("#rag-chat-settings").classList.contains("hidden"));
    click("workspace-chat"); const before = requests("GET", /^\/api\/rag\//).length;
    $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const snapshot = $("#rag-answer-snapshot");
    check("V2 inspector shows original, rewritten, history and unknown usage", snapshot.querySelector(".rag-snapshot-original-query").textContent === rag.original_query && snapshot.querySelector(".rag-snapshot-query").textContent === rag.query && snapshot.textContent.includes("neutral earlier answer") && snapshot.textContent.includes("Неизвестно"));
    check("Saved candidate decisions and full context render without current-index reads", snapshot.querySelectorAll(".rag-snapshot-candidate").length === 3 && snapshot.textContent.includes("Ниже порога") && snapshot.textContent.includes("Лимит фрагментов") && snapshot.querySelector(".rag-snapshot-context").textContent === rag.context && requests("GET", /^\/api\/rag\//).length === before);
  });
  for (const [rewrite, filter] of [[false, false], [true, false], [false, true]]) {
    await scenario("Saved refinement flags " + rewrite + "/" + filter, async () => {
      const rag = {version: 2, original_query: "original neutral", query: "search neutral",
        config: {rewrite_enabled: rewrite, filter_enabled: filter, candidates_k: 20, final_k: 5, similarity_threshold: .3},
        rewrite: {enabled: rewrite, model: "neutral/rewrite-model", usage: {total_tokens: 11}},
        history_used: [{user: "history question", assistant: "history answer"}], timings: {rewrite_seconds: .5, retrieval_seconds: .2},
        index: {index_id: "saved-flags"}, candidates: [{chunk_id: "final", text: "final text", decision: "kept", score: .8}, {chunk_id: "reject", text: "excluded text", decision: "threshold", score: .1}],
        hits: [{chunk_id: "final", text: "final text", decision: "kept", score: .8}], context: "exact context"};
      const saved = JSON.stringify(rag);
      const {client, $, click, requests} = freshClient({agents: [{rag_enabled: true, rag_rewrite_enabled: !rewrite, rag_filter_enabled: !filter,
        transcript: [turns[0], {...turns[1], rag}], history_len: 2}]});
      client.init(); await settle();
      const before = requests("GET", /^\/api\/rag\//).length;
      $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
      const node = $("#rag-answer-snapshot"), content = node.textContent;
      check("Saved rewrite controls query/history/model visibility " + rewrite + "/" + filter,
        Boolean(node.querySelector(".rag-snapshot-original-query")) === rewrite
        && content.includes("neutral/rewrite-model") === rewrite && content.includes("history answer") === rewrite
        && content.includes("rewrite_seconds") === rewrite && !content.includes("Переформулирование выключено"));
      check("Saved filtering controls candidates/threshold visibility " + rewrite + "/" + filter,
        Boolean(node.querySelector(".rag-snapshot-candidate")) === filter && content.includes("similarity_threshold") === filter
        && content.includes("Ниже порога") === filter && (filter || !content.includes("decision")));
      check("Feature presentation preserves final data and saved capture " + rewrite + "/" + filter,
        node.querySelector(".rag-snapshot-context").textContent === rag.context && content.includes("final text")
        && content.includes("saved-flags") && JSON.stringify(client.state.current.transcript[1].rag) === saved
        && requests("GET", /^\/api\/rag\//).length === before);
      node.querySelector("button").dispatchEvent(new Evt("click")); await settle();
      click("chat-settings"); click("tab-btn-rag");
      $("#f-rag_rewrite_enabled").checked = !rewrite;
      $("#f-rag_rewrite_enabled").dispatchEvent(new Evt("change")); await settle();
      $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
      check("Changing current settings saves next-answer config without changing inspected snapshot " + rewrite + "/" + filter,
        requests("PATCH", "/api/agents/ag_1").at(-1).body.rag_rewrite_enabled === !rewrite
        && node.textContent === content && JSON.stringify(client.state.current.transcript[1].rag) === saved
        && $("#rag-current-chat").hidden && $("#save-status").classList.contains("hidden"));
    });
  }
  await scenario("RAG actual stages, deterministic no-hit and failed rewrite diagnostics", async () => {
    const {client, server, $, send, requests} = freshClient({agents: [{rag_enabled: true, rag_rewrite_enabled: true, rag_filter_enabled: true}]});
    const search = deferred(), nohit = deferred(), finish = deferred();
    const empty = {version: 2, original_query: "neutral", query: "rewritten", hits: [], candidates: [], context: ""};
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([
      {event: "retrieval", stage: "rewrite"}, {event: "retrieval", stage: "search"},
      {...start, generation: false, resolved_messages: []}, {event: "delta", text: "В базе не найдена подходящая информация"}, {event: "done", committed: true, answer_index: 1}
    ], {beforeRead: async (i) => { if (i === 1) await search.promise; if (i === 2) await nohit.promise; if (i === 3) await finish.promise; },
      finish: () => Object.assign(server.state.agents[0], {transcript: [turns[0], {...turns[1], content: "В базе не найдена подходящая информация", rag: empty}], history_len: 2})}));
    client.init(); await settle(); send("neutral"); await settle();
    check("Actual rewrite announces its stage", $("#feed").querySelector(".card-status-text").textContent === "Переформулирование запроса");
    search.resolve(); await settle(); check("Actual search announces its stage", $("#feed").querySelector(".card-status-text").textContent === "Поиск контекста");
    nohit.resolve(); await settle(); check("No-hit start does not announce generation", $("#feed").querySelector(".card-status-text").textContent === "Подходящих фрагментов нет");
    finish.resolve(); await settle(); check("No-hit answer has empty sources and no fabricated usage", $("#feed").querySelector(".card-rag").textContent.includes("подходящих фрагментов нет") && !$("#feed").querySelector(".card-rag").querySelector("li") && !$("#feed").querySelector(".card-usage"));
    const payload = {model: "neutral", messages: [{role: "user", content: "neutral rewrite input"}]};
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([{event: "error", message: "Neutral rewrite invalid", metrics: {prompt_tokens: 4, completion_tokens: 2, total_tokens: 6, cost_usd: .02}, request_bodies: [payload]}, {event: "done", committed: false, question: "retry", request_bodies: [payload], metrics: {cost_usd: .02}}]));
    send("retry"); await settle();
    check("Failed rewrite actual JSON/cost remain in transient diagnostics without a committed answer", $("#composer-hint").querySelector(".failed-request-info").textContent.includes("0.02") && $("#composer-hint").querySelector("pre").textContent === JSON.stringify(payload, null, 2) && client.state.current.history_len === 2 && requests("POST", "/api/agents/ag_1/messages").length === 2);
  });
  await scenario("RAG retrieval progress, generation and terminal restoration", async () => {
    const {client, server, $, send} = freshClient({agents: [{rag_enabled: true}]});
    const generation = deferred(), token = deferred(), finished = deferred();
    const savedRag = {version: 1, query: "neutral question", index: {index_id: "saved-neutral"}, top_k: 5, hits: [], context: "neutral saved context"};
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([
      {event: "retrieval", stage: "retrieval", query: "neutral question"}, {...start, rag_at: 0},
      {event: "delta", text: "neutral answer"}, {event: "done", committed: true, answer_index: 1}], {
      beforeRead: async (index) => { if (index === 1) await generation.promise; if (index === 2) await token.promise; },
      finish: () => { Object.assign(server.state.agents[0], {transcript: [turns[0], {...turns[1], rag: savedRag}], history_len: 2}); finished.resolve(); }
    }));
    client.init(); await settle(); send("neutral question"); await settle();
    check("Real retrieval event announces search before generation", $("#feed").querySelector(".card-status-text")?.textContent === "Поиск контекста");
    generation.resolve(); await settle();
    check("Start event changes progress to generation", $("#feed").querySelector(".card-status-text")?.textContent === "Генерация");
    token.resolve(); await finished.promise; await settle();
    check("Terminal exchange removes progress", !$("#feed").querySelector(".card-status"));
    const card = $("#feed").querySelector(".card"); button(card, "Информация о запросе").dispatchEvent(new Evt("click"));
    check("RAG prompt slot zero is preserved", card.querySelector(".prompt-role").textContent === "контекст RAG");
    server.respond("POST", "/api/agents/ag_1/regenerate", () => stream([
      {event: "retrieval", stage: "retrieval"}, {event: "error", message: "Neutral retrieval unavailable"},
      {event: "done", committed: false, restored: true, question: "neutral question"}]));
    button(card, "Перегенерировать").dispatchEvent(new Evt("click")); await settle();
    check("Failed regenerate restores previous answer snapshot", same(client.state.current.transcript[1].rag, savedRag)
      && $("#feed").querySelector(".card-rag") && !client.state.busy);
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([
      {event: "retrieval", stage: "retrieval"}, {event: "error", message: "Neutral index missing"},
      {event: "done", committed: false, question: "retry neutral question"}]));
    send("retry neutral question"); await settle();
    check("Retrieval failure restores input without false answer or stuck busy", $("#input").value === "retry neutral question"
      && !client.state.busy && $("#feed").querySelectorAll(".card").length === 1 && client.state.current.history_len === 2);
    server.respond("POST", "/api/agents/ag_1/messages", () => stream([
      {event: "retrieval", stage: "retrieval"},
      {event: "done", committed: false, cancelled: true, question: "cancelled neutral question", error: "Neutral cancellation"}]));
    send("cancelled neutral question"); await settle();
    check("Uncommitted cancellation restores terminal question without error event", $("#input").value === "cancelled neutral question" && !client.state.busy && client.state.current.history_len === 2);
  });
  await scenario("RAG inspector reads saved state without chat/key, paginates and reveals actual vector", async () => {
    const info = { index_id: "saved-1", words: 15555, size_bytes: 8192, rows: { documents: 1, chunks: 1 },
      version: 1, strategy: "structural", dimension: 3, embedding_config: { model: "offline-test" } };
    const operation = { kind: "index", state: "ready", stage: "save", duration_seconds: 1.25,
      documents: 1, chunks: 1, computed: 1, cached: 0, dimension: 3, config: { model: "offline-test" } };
    const doc = { document_id: "doc", title: "<script>archive</script>", words: 15555, characters: 50000 };
    const chunk = { chunk_id: "chunk", document_id: "doc", section: "Раздел", start: 120, end: 170, text: "Реальный текст" };
    const { client, server, $, click, requests } = freshClient({ agents: [] });
    server.respond("GET", "/api/rag/status", { state: "ready", index: info, operation, embedding_defaults: {model: "edited-before-poll", base_url: "http://127.0.0.1:8005/v1"} });
    server.respond("GET", "/api/rag/documents?offset=0&limit=25", { items: [doc] });
    server.respond("GET", "/api/rag/documents/doc/chunks?offset=0&limit=25", { items: [chunk] });
    server.respond("GET", "/api/rag/chunks/chunk", chunk);
    server.respond("GET", "/api/rag/chunks/chunk?vector=true", { ...chunk, vector: [0.2, 0.4, 0.8] });
    client.init(); await settle(); click("application-settings"); click("tab-btn-rag"); await settle();
    check("No selected chat hides and disables per-chat RAG without disabling workflow", $("#rag-current-chat").hidden && $("#f-rag_enabled").disabled && !$("#rag-workflow").hidden);
    check("No chat/key needed for saved index and actual CLI counts", $("#rag-status").textContent === "Индекс готов"
      && $("#rag-index").textContent.includes("15555") && $("#rag-operation").textContent.includes("1.25"));
    $("#rag-documents").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    $("#rag-chunks").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    check("Metadata/clean text visible and vectors fetched lazily", $("#rag-chunk").textContent.includes("Реальный текст")
      && $("#rag-chunk").textContent.includes("120") && requests("GET", "/api/rag/chunks/chunk?vector=true").length === 0);
    const vector = $("#rag-chunk").querySelectorAll("details").at(-1); vector.open = true; await vector.ontoggle();
    check("Actual saved vector displayed", vector.textContent.includes("[0.2,0.4,0.8]")
      && requests("GET", "/api/rag/chunks/chunk?vector=true").length === 1);
    const actual = $("#rag-operation").querySelector("details"); actual.open = true;
    const metadata = $("#rag-index").querySelector("details"); metadata.open = true;
    const draft = $("#rag-revision"); draft.value = "edited-before-poll"; draft.focus();
    await settle(2100);
    check("Repeated real polling preserves mounted details, vector, focus and draft", actual === $("#rag-operation").querySelector("details") && actual.open
      && metadata.open && vector.open && $("#rag-chunk").textContent.includes("Реальный текст") && document.activeElement === draft && draft.value === "edited-before-poll");
    actual.open = false; metadata.open = false; await settle(1100);
    check("User closed details stay closed on next poll", !actual.open && !metadata.open);
    const pending = deferred(); server.respond("GET", "/api/rag/status", () => pending.promise);
    click("tab-btn-model"); click("tab-btn-rag"); await settle(); click("tab-btn-model");
    pending.resolve(json({ state: "error", operation: { ...operation, error: "late error" }, index: info })); await settle();
    check("Late inspector response cannot write after leaving page", $("#rag-error").textContent !== "late error");
    server.respond("GET", "/api/rag/status", { state: "error", operation: { ...operation, state: "error", error: "embedding failed" }, index: info });
    click("tab-btn-rag"); await settle();
    check("Failure distinct from previous committed index", $("#rag-status").textContent === "Ошибка операции"
      && $("#rag-error").textContent === "embedding failed" && $("#rag-index").textContent.includes("15555"));
  });
  await scenario("RAG selection follows IDs, pages and parent changes while rejecting stale children", async () => {
    const {client, server, $, click} = freshClient({agents: []});
    const stages = {corpus: {fingerprint: "corpus", urls: []}, chunks: {fingerprint: "chunks"}, embeddings: {embedding_fingerprint: "vectors"}};
    const status = {state: "ready", stages};
    const docs = Array.from({length: 25}, (_, i) => ({document_id: "doc-" + i, title: "Same title", words: i + 1, characters: 30}));
    const chunks = Array.from({length: 25}, (_, i) => ({chunk_id: "chunk-" + i, section: "Same section", start: i, end: i + 1}));
    server.respond("GET", "/api/rag/status", status);
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: docs});
    server.respond("GET", "/api/rag/documents?offset=25&limit=25&working=true", {items: [{...docs[0], document_id: "page-two"}]});
    for (const doc of docs.slice(0, 2)) {
      server.respond("GET", "/api/rag/documents/" + doc.document_id + "?offset=0&limit=10000", {text: doc.document_id, characters: 30});
      server.respond("GET", "/api/rag/documents/" + doc.document_id + "/chunks?offset=0&limit=25&working=true", {items: chunks});
    }
    server.respond("GET", "/api/rag/documents/doc-0/chunks?offset=25&limit=25&working=true", {items: [{...chunks[0], chunk_id: "other-page"}]});
    server.respond("GET", "/api/rag/chunks/chunk-0?working=true", {chunk_id: "chunk-0", text: "Selected source"});
    const rows = (id) => $("#rag-" + id).querySelectorAll(".rag-list-row");
    const active = (row) => row.attributes["aria-pressed"] === "true" && row.classList.contains("selected");
    const press = (node) => node.dispatchEvent(new Evt("click"));
    const page = (id, label) => press($("#rag-" + id).querySelector(".rag-pager").querySelectorAll("button").find((node) => node.textContent === label));
    client.init(); await settle(); click("application-settings"); click("tab-btn-rag"); await settle();
    check("No item is selected implicitly", rows("documents").every((row) => row.attributes["aria-pressed"] === "false"));
    press(rows("documents")[0]); await settle(); press(rows("chunks")[0]); await settle();
    check("Duplicate titles and sections select only the matching ID", active(rows("documents")[0]) && !active(rows("documents")[1])
      && active(rows("chunks")[0]) && !active(rows("chunks")[1]));
    const selectedDetail = $("#rag-chunk").querySelector("details"); selectedDetail.open = true;
    await settle(1100); click("rag-step-chunks"); await settle();
    check("Polling and stage navigation preserve mounted selection within one source", active(rows("documents")[0]) && active(rows("chunks")[0])
      && $("#rag-chunk").querySelector("details") === selectedDetail && selectedDetail.open);
    page("chunks", "Далее"); await settle();
    check("Another chunk page never highlights a matching label", rows("chunks").every((row) => !active(row)) && $("#rag-chunk").textContent.includes("Selected source"));
    page("chunks", "Назад"); await settle();
    page("documents", "Далее"); await settle();
    check("Another document page keeps explicit detail without highlighting a duplicate title", rows("documents").every((row) => !active(row)) && active(rows("chunks")[0]));
    page("documents", "Назад"); await settle();
    check("Returning to pages restores highlights by ID", active(rows("documents")[0]) && active(rows("chunks")[0]));
    click("rag-step-embeddings"); await settle();
    const vector = $("#rag-chunk").querySelectorAll("details").at(-1), lateVector = deferred(), lateText = deferred(), lateDocument = deferred();
    server.respond("GET", "/api/rag/chunks/chunk-0?vector=true&working=true", () => lateVector.promise);
    server.respond("GET", "/api/rag/chunks/chunk-1?working=true", () => lateText.promise);
    server.respond("GET", "/api/rag/documents/doc-1?offset=0&limit=10000", () => lateDocument.promise);
    vector.open = true; const vectorFetch = vector.ontoggle(); press(rows("chunks")[1]); await settle();
    press(rows("documents")[1]);
    check("Changing parent immediately clears old children while new document is loading", active(rows("documents")[1]) && !active(rows("documents")[0])
      && !$("#rag-chunk").children.length && !$("#rag-chunks").children.length && !$("#rag-document-preview").children.length);
    lateText.resolve(json({chunk_id: "chunk-1", text: "Stale child"})); lateVector.resolve(json({vector: [9,9,9]})); await vectorFetch; await settle();
    check("Late text and vector cannot attach under a new parent", !$("#rag-chunk").children.length && !vector.textContent.includes("[9,9,9]"));
    lateDocument.resolve(json({text: "doc-1", characters: 30})); await settle();
    check("New parent has no selected child even when chunk labels and IDs repeat", active(rows("documents")[1]) && rows("chunks").every((row) => !active(row)));
    stages.corpus.fingerprint = "new-corpus"; await settle(1100);
    check("Generation invalidation clears selection and children", rows("documents").every((row) => !active(row)) && !$("#rag-chunk").children.length && !$("#rag-chunks").children.length);
  });
  await scenario("RAG reveals validated stages and keeps actual operation accounting", async () => {
    const {client, server, $, click} = freshClient({agents: []});
    const stages = {corpus: null, chunks: null, embeddings: null};
    const report = {model: "actual-boundaries", calls: 2, usage: {total_tokens: 120}, cost_usd: 0.003};
    const status = {state: "ready", stages, operation: {kind: "chunks", state: "complete", semantic_report: report},
      index: {index_id: "old-index", words: 10, size_bytes: 100, rows: {documents: 1, chunks: 1}, embedding_config: {model: "old"}}};
    server.respond("GET", "/api/models?provider=compatible&purpose=embedding", {models: [{id: "draft-to-preserve"}]});
    server.respond("GET", "/api/rag/status", status);
    server.respond("GET", "/api/rag/documents?offset=0&limit=25", {items: []});
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: []});
    client.init(); await settle(); click("application-settings"); click("tab-btn-rag"); await settle();
    const gate = (name) => $("#rag-" + name + "-controls");
    check("Legacy published index remains inspectable and deletable without enabling save or unavailable stages", gate("chunks").hidden && gate("embeddings").hidden
      && !gate("save").hidden && !$("#rag-step-save").disabled && $("#rag-save").disabled && !$("#rag-delete-index").hidden);
    const oldIndex = status.index;
    server.respond("DELETE", "/api/rag/stages/index", () => { status.index = null; return json({cleared: "index"}); });
    click("rag-delete-index"); await settle();
    check("Deleting legacy index returns to documents and locks index navigation", !gate("documents").hidden && gate("save").hidden && $("#rag-step-save").disabled);
    status.index = oldIndex;
    click("rag-step-documents");
    stages.corpus = {fingerprint: "current-corpus", documents: 1, urls: []};
    await settle(1100);
    check("Corpus unlocks chunk navigation without changing selected documents", gate("chunks").hidden && !$("#rag-step-chunks").disabled);
    click("rag-step-chunks");
    check("Selecting chunks shows one stage", !gate("chunks").hidden && gate("documents").hidden && gate("embeddings").hidden && gate("save").hidden);
    stages.chunks = {fingerprint: "semantic-one", strategy: "semantic", report};
    await settle(1100);
    check("Chunk completion keeps selected chunk stage", !gate("chunks").hidden && gate("embeddings").hidden);
    click("rag-step-embeddings"); await settle();
    const draft = $("#rag-model"), parameters = gate("embeddings").querySelector("details");
    draft.value = "draft-to-preserve"; draft.focus(); parameters.open = true;
    check("Semantic chunks reveal embeddings and accounting stays in actual operation JSON", !gate("embeddings").hidden && gate("save").hidden
      && same(JSON.parse($("#rag-operation").querySelector("pre").textContent).semantic_report, report));
    stages.embeddings = {embedding_fingerprint: "current-vectors"};
    await settle(1100);
    check("Vectors unlock index without leaving embeddings or remounting draft, focus or details", gate("save").hidden && !$("#rag-step-save").disabled && !gate("embeddings").hidden && draft.value === "draft-to-preserve"
      && document.activeElement === draft && parameters.open && gate("embeddings").querySelector("details") === parameters);
    click("rag-step-save");
    check("Index selection shows only save controls and index results", !gate("save").hidden && gate("embeddings").hidden && gate("chunks").hidden
      && !$("#rag-documents").hidden && !$("#rag-chunks").hidden && !$("#rag-chunk").hidden && !$("#rag-index").hidden && !$("#rag-delete-index").hidden);
    server.respond("DELETE", "/api/rag/stages/index", () => { status.index = null; return json({cleared: "index"}); });
    click("rag-delete-index"); await settle();
    check("Index deletion keeps selected index stage and upstream availability", !gate("save").hidden && !$("#rag-step-save").disabled
      && $("#rag-delete-index").hidden && !$("#rag-save").disabled && stages.embeddings.embedding_fingerprint === "current-vectors");
    click("rag-step-chunks");
    check("Return to chunks hides index and keeps document selector", !gate("chunks").hidden && $("#rag-index").hidden
      && !$("#rag-documents").hidden && !$("#rag-chunks").hidden);
    stages.chunks = {fingerprint: "fixed-two", strategy: "fixed"}; stages.embeddings = null;
    await settle(1100);
    check("Invalidated vectors hide save", gate("save").hidden);
    server.respond("DELETE", "/api/rag/stages/chunks", () => { stages.chunks = null; return json({cleared: "chunks"}); });
    click("rag-delete-chunks"); await settle();
    check("Deletion hides next stages while actual operation report remains inspectable", gate("embeddings").hidden && gate("save").hidden
      && same(JSON.parse($("#rag-operation").querySelector("pre").textContent).semantic_report, report) && draft.value === "draft-to-preserve" && parameters.open);
    click("rag-step-embeddings");
    check("Unavailable future navigation stays disabled", $("#rag-step-embeddings").disabled && !gate("chunks").hidden);
    stages.corpus = null; await settle(1100);
    check("Corpus invalidation returns to documents and keeps future steps locked", gate("chunks").hidden && !gate("documents").hidden && $("#rag-step-chunks").disabled);
  });
  await scenario("RAG status steps keep operation separate and inspect one source through late responses", async () => {
    const {client, server, $, click, requests} = freshClient({agents: []});
    const stages = {corpus: {fingerprint: "new-corpus", documents: 1, urls: []}, chunks: {fingerprint: "new-chunks", chunks: 1},
      embeddings: {embedding_fingerprint: "new-vectors", dimension: 3}};
    const status = {state: "stale", stages, index: {index_id: "saved-old", embedding_fingerprint: "old-vectors", words: 10,
      size_bytes: 100, rows: {documents: 1, chunks: 1}, embedding_config: {model: "old"}}, operation: null};
    const savedDoc = {document_id: "old", title: "Saved neutral document", words: 10, characters: 30};
    const workingDoc = {document_id: "new", title: "Working neutral document", words: 20, characters: 40};
    const savedChunk = {chunk_id: "old-chunk", section: "Saved", start: 0, end: 30, text: "Saved neutral text"};
    const workingChunk = {chunk_id: "new-chunk", section: "Working", start: 0, end: 40, text: "Working neutral text"};
    server.respond("GET", "/api/rag/status", status);
    server.respond("GET", "/api/rag/documents?offset=0&limit=25", {items: [savedDoc]});
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: [workingDoc]});
    server.respond("GET", "/api/rag/documents/old/chunks?offset=0&limit=25", {items: [savedChunk]});
    server.respond("GET", "/api/rag/documents/new?offset=0&limit=10000", {text: workingChunk.text, characters: 40});
    server.respond("GET", "/api/rag/documents/new/chunks?offset=0&limit=25&working=true", {items: [workingChunk]});
    server.respond("GET", "/api/rag/chunks/old-chunk", savedChunk);
    server.respond("GET", "/api/rag/chunks/new-chunk?working=true", workingChunk);
    server.respond("GET", "/api/rag/chunks/old-chunk?vector=true", {vector: [1, 0, 0]});
    client.init(); await settle(); click("application-settings"); click("tab-btn-rag"); await settle();
    check("Single mounted status navigation works without an operation", $("#rag-navigation").parentElement.classList.contains("rag-operation-card")
      && $("#rag-navigation").querySelectorAll("button").length === 4 && $("#rag-operation").hidden && $("#rag-step-save").attributes["aria-current"] === "step");
    check("Stale index uses saved documents despite current corpus", $("#rag-documents").textContent.includes(savedDoc.title)
      && !$("#rag-documents").textContent.includes(workingDoc.title));
    const select = async (id) => { $("#" + id).querySelector("button").dispatchEvent(new Evt("click")); await settle(); };
    await select("rag-documents"); await select("rag-chunks");
    let vector = $("#rag-chunk").querySelectorAll("details").at(-1);
    check("Index inspector reads saved metadata and text and delays its vector", !$("#rag-chunks").hidden && !$("#rag-chunk").hidden
      && $("#rag-chunk").textContent.includes(savedChunk.text) && !vector.hidden && requests("GET", "/api/rag/chunks/old-chunk?vector=true").length === 0);
    vector.open = true; await vector.ontoggle();
    check("Index exposes actual saved vector", vector.textContent.includes("[1,0,0]"));
    click("rag-step-embeddings"); await settle();
    check("Source change resets document and child selection before explicit selection", $("#rag-documents").querySelector(".rag-list-row").attributes["aria-pressed"] === "false"
      && !$("#rag-chunks").children.length && !$("#rag-chunk").children.length);
    await select("rag-documents"); await select("rag-chunks");
    const lateVector = deferred(); server.respond("GET", "/api/rag/chunks/new-chunk?vector=true&working=true", () => lateVector.promise);
    vector = $("#rag-chunk").querySelectorAll("details").at(-1); vector.open = true; const loading = vector.ontoggle();
    click("rag-step-save"); await settle(); await select("rag-documents"); await select("rag-chunks");
    lateVector.resolve(json({vector: [0, 1, 0]})); await loading; await settle();
    check("Late working vector cannot enter published inspector", $("#rag-chunk").textContent.includes(savedChunk.text)
      && !$("#rag-chunk").textContent.includes("[0,1,0]"));
    const lateDocuments = deferred(); server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", () => lateDocuments.promise);
    click("rag-step-chunks"); await settle(); click("rag-step-save"); await settle();
    lateDocuments.resolve(json({items: [workingDoc]})); await settle();
    check("Late working document page cannot replace published documents", $("#rag-documents").textContent.includes(savedDoc.title)
      && !$("#rag-documents").textContent.includes(workingDoc.title));
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: [workingDoc]});
    const lateChunk = deferred(); server.respond("GET", "/api/rag/chunks/new-chunk?working=true", () => lateChunk.promise);
    click("rag-step-chunks"); await settle(); await select("rag-documents"); await select("rag-chunks");
    click("rag-step-save"); await settle(); await select("rag-documents"); await select("rag-chunks");
    lateChunk.resolve(json(workingChunk)); await settle();
    check("Late working chunk text cannot replace published chunk", $("#rag-chunk").textContent.includes(savedChunk.text)
      && !$("#rag-chunk").textContent.includes(workingChunk.text));
    status.operation = {state: "running", kind: "embeddings", stage: "embeddings"}; await settle(1100);
    click("rag-step-documents"); await settle();
    check("Manual navigation and running operation stay distinct across poll", $("#rag-step-documents").attributes["aria-current"] === "step"
      && $("#rag-step-embeddings").classList.contains("running") && !$("#rag-step-documents").classList.contains("running") && $("#rag-save").disabled);
  });
  await scenario("Shared embedding picker preserves exact ID and catalogue ownership", async () => {
    const {client, server, $, click, requests} = freshClient({agents: []});
    const original = "namespace/vector-model", route = "/api/models?provider=compatible&purpose=embedding";
    const status = {state: "missing", embedding_defaults: {provider: "compatible", model: original, dimensions: 3, revision: "saved"}, stages: {
      corpus: {fingerprint: "embedding-corpus", documents: 1, urls: []}, chunks: {fingerprint: "embedding-chunks", chunks: 1}}};
    server.respond("GET", "/api/rag/status", status);
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: []});
    const initial = deferred(); server.respond("GET", route, () => initial.promise);
    server.respond("POST", "/api/rag/operations/embeddings", {});
    client.init(); await settle(); click("application-settings"); click("tab-btn-rag"); await settle();
    const model = $("#rag-model"), provider = $("#rag-provider"), list = $("#rag-model-catalogue"), message = $("#rag-embedding-model-status");
    check("Searchable embedding field retains saved ID while loading", model.tagName === "INPUT" && model.attributes.list === list.id && model.value === original && message.textContent.includes("Загрузка"));
    initial.resolve(failure("neutral catalogue error", 502)); await settle();
    check("Catalogue error preserves exact ID without inventing availability", model.value === original && list.children.length === 0 && message.textContent.includes("Каталог недоступен"));
    server.respond("GET", route, {models: [{id: "vector-model"}, {id: "other/vector-model"}]}); click("rag-embedding-model-refresh"); await settle();
    check("Embedding catalogue never rewrites model namespaces", model.value === original && list.children.map(n => n.value).join(",") === "vector-model,other/vector-model");
    model.value = ""; model.dispatchEvent(new Evt("change")); click("rag-embed"); await settle();
    check("Empty model blocks embedding operation", $("#rag-embed").disabled && requests("POST", "/api/rag/operations/embeddings").length === 0);
    model.value = "vector-model"; model.dispatchEvent(new Evt("change")); model.focus();
    const refresh = deferred(); server.respond("GET", route, () => refresh.promise); click("rag-embedding-model-refresh"); await settle();
    model.value = "manual/vector"; model.dispatchEvent(new Evt("input")); model.dispatchEvent(new Evt("change"));
    refresh.resolve(json({models: [{id: "vector-model"}]})); const calls = requests("GET", route).length; await settle(1100);
    check("Refresh/poll preserve manual ID, focus and mounted input", model.value === "manual/vector" && document.activeElement === model && model === $("#rag-model") && requests("GET", route).length === calls);
    const late = deferred(); server.respond("GET", route, () => late.promise); click("rag-embedding-model-refresh"); await settle();
    provider.value = "openrouter"; provider.dispatchEvent(new Evt("change")); await settle();
    late.resolve(json({models: [{id: "late-wrong-provider"}]})); await settle();
    check("Provider switch aborts late catalogue and preserves separate drafts", requests("GET", route).at(-1).signal.aborted && !list.children.some(n => n.value === "late-wrong-provider") && model.value === "");
    server.respond("GET", route, {models: []}); provider.value = "compatible"; provider.dispatchEvent(new Evt("change")); await settle();
    check("Empty catalogue restores compatible exact-ID draft", model.value === "manual/vector" && list.children.length === 0 && message.textContent.includes("Каталог пуст"));
    click("rag-embed"); await settle();
    check("Embedding payload uses shared provider/model with no scenario URL", same(requests("POST", "/api/rag/operations/embeddings").at(-1)?.body,
      {reasoning_enabled: false, provider: "compatible", model: "manual/vector", dimensions: 3, revision: "saved"}));
  });
  await scenario("RAG preparation methods have independent generation drafts and operation payloads", async () => {
    const {client, server, $, click, requests} = freshClient({agents: []});
    const status = {state: "missing", operation: {kind: "ingest", state: "complete", preparation_report: {config: {timeout_seconds: 1800}}}, stages: {corpus: {fingerprint: "prep-fixture", documents: 1, urls: []},
      chunks: {fingerprint: "prep-chunks", chunks: 1, strategy: "fixed", size: 1200, overlap: 180}}};
    server.respond("GET", "/api/rag/status", () => json(status));
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: []});
    server.respond("POST", "/api/rag/operations/ingest", {});
    server.respond("POST", "/api/rag/operations/chunks", {});
    client.init(); await settle(); client.state.models = [];
    click("application-settings"); click("tab-btn-rag"); await settle(); click("rag-step-documents"); await settle();
    const method = $("#rag-preparation-strategy"), model = $("#rag-preparation-model"), provider = $("#rag-preparation-provider");
    const timeout = $("#rag-preparation-timeout-seconds");
    check("Preparation timeout seeds saved operation report", Number(timeout.value) === 1800);
    timeout.value = "900"; timeout.dispatchEvent(new Evt("input"));
    $("#rag-urls").value = "https://example.test/neutral"; $("#rag-manifest").checked = false;
    click("rag-ingest"); await settle();
    check("Document preparation defaults to code and sends no LLM fields", method.value === "programmatic" && $("#rag-preparation-fields").hidden
      && same(requests("POST", "/api/rag/operations/ingest").at(-1)?.body, {urls: ["https://example.test/neutral"], use_manifest: false, preparation_strategy: "programmatic"}));
    await settle(1100);
    const cloud = deferred(); server.respond("GET", "/api/models?provider=openrouter&purpose=generation", () => cloud.promise);
    method.value = "llm"; method.dispatchEvent(new Evt("change")); await settle();
    model.value = "openai/gpt-6-luna"; model.dispatchEvent(new Evt("change")); model.focus();
    cloud.resolve(json({models: [{id: "openai/gpt-6-luna"}, {id: "prep-cloud-draft"}]})); await settle();
    model.value = "prep-cloud-draft"; model.dispatchEvent(new Evt("change"));
    const calls = requests("GET", "/api/models?provider=openrouter&purpose=generation").length;
    status.operation = {kind: "ingest", state: "complete", preparation_report: {model: "actual-prep-model", calls: 1,
      usage: {prompt_tokens: 321, completion_tokens: 123, total_tokens: 444}, cost_usd: 0.002, config: {timeout_seconds: 600}}};
    const actual = $("#rag-operation").querySelector("details"); actual.open = true;
    await settle(1100);
    check("Preparation poll keeps mounted model/focus/details and actual report", model === $("#rag-preparation-model") && model.value === "prep-cloud-draft"
      && Number(timeout.value) === 900 && document.activeElement === model && actual.open && actual.textContent.includes('"actual-prep-model"') && actual.textContent.includes('"total_tokens": 444')
      && actual.textContent.includes('"cost_usd": 0.002') && requests("GET", "/api/models?provider=openrouter&purpose=generation").length === calls);
    const localPath = "/api/models?provider=compatible&purpose=generation";
    server.respond("GET", localPath, {models: [{id: "prep-local-draft"}]});
    provider.value = "compatible"; provider.dispatchEvent(new Evt("change")); await settle();
    model.value = "prep-local-draft"; model.dispatchEvent(new Evt("change"));
    const local = deferred(); server.respond("GET", localPath, () => local.promise); click("rag-preparation-model-refresh"); await settle();
    click("rag-step-chunks"); await settle();
    local.resolve(json({models: [{id: "late-prep-local"}]})); await settle();
    check("Leaving preparation cancels its catalogue and cannot touch chunk settings", requests("GET", localPath).at(-1).signal.aborted
      && !model.textContent.includes("late-prep-local") && $("#rag-semantic-provider").value === "openrouter"
      && $("#rag-semantic-model").value === "openai/gpt-6-luna");
    click("rag-split"); await settle();
    check("Chunk payload excludes preparation settings", same(requests("POST", "/api/rag/operations/chunks").at(-1)?.body,
      {strategy: "fixed", size: 1200, overlap: 180}));
    await settle(1100); server.respond("GET", localPath, {models: [{id: "prep-local-draft"}]});
    click("rag-step-documents"); await settle(); click("rag-ingest"); await settle();
    check("LLM preparation sends only its independent generation settings", same(requests("POST", "/api/rag/operations/ingest").at(-1)?.body,
      {urls: ["https://example.test/neutral"], use_manifest: false, preparation_strategy: "llm", preparation_reasoning_enabled: false, preparation_timeout_seconds: 900, preparation_provider: "compatible", preparation_model: "prep-local-draft"}));
    provider.value = "openrouter"; provider.dispatchEvent(new Evt("change")); await settle();
    check("Preparation restores its own provider draft", model.value === "prep-cloud-draft");
    method.value = "programmatic"; method.dispatchEvent(new Evt("change"));
    method.value = "llm"; method.dispatchEvent(new Evt("change")); await settle();
    check("Code/LLM switching preserves preparation draft", model.value === "prep-cloud-draft" && Number(timeout.value) === 900 && !$("#rag-preparation-fields").hidden);
  });
  await scenario("Global connection saves own drafts and invalidate compatible catalogues", async () => {
    const {client, server, $, click, requests} = freshClient({agents: []});
    const initial = deferred(); server.respond("GET", "/api/model-settings", () => initial.promise);
    const writes = []; const first = deferred(), second = deferred();
    server.respond("PATCH", "/api/model-settings", request => { writes.push(request.body.compatible_base_url); return writes.length === 1 ? first.promise : writes.length === 2 ? second.promise : json(request.body); });
    client.init(); await settle(); click("application-settings");
    const input = $("#compatible-base-url"); check("Global server settings editable without selecting a chat", !input.disabled);
    input.value = "http://127.0.0.1:9001/v1"; input.dispatchEvent(new Evt("input")); input.dispatchEvent(new Evt("change")); await settle();
    input.value = "http://127.0.0.1:9002/v1"; input.dispatchEvent(new Evt("input")); input.dispatchEvent(new Evt("change")); await settle();
    check("Overlapping URL saves are serialized", writes.length === 1);
    first.resolve(json({compatible_base_url: writes[0]})); await settle();
    input.value = "http://127.0.0.1:9003/v1"; input.dispatchEvent(new Evt("input"));
    second.resolve(json({compatible_base_url: "http://127.0.0.1:9002/v1"})); await settle();
    initial.resolve(json({compatible_base_url: "http://127.0.0.1:8005/v1"})); await settle();
    check("Late GET and earlier saves cannot overwrite a newer unsaved URL", input.value === "http://127.0.0.1:9003/v1" && input.dataset.dirty === "true");
    input.dispatchEvent(new Evt("change")); await settle();
    check("Global URL PATCH contains only nonsecret server address", same(requests("PATCH", "/api/model-settings").at(-1).body, {compatible_base_url: input.value}) && writes.length === 3);
    server.respond("GET", "/api/rag/status", {state: "missing", stages: {corpus: {fingerprint: "catalog-corpus", documents: 1, urls: []},
      chunks: {fingerprint: "catalog-chunks", chunks: 1, strategy: "semantic", size: 512, overlap: 64,
      semantic_config: {provider: "compatible", model: "saved-generative"}}}});
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: []});
    const route = "/api/models?provider=compatible&purpose=generation", late = deferred(); server.respond("GET", route, () => late.promise);
    click("tab-btn-rag"); await settle(); click("rag-step-chunks"); await settle();
    const model = $("#rag-semantic-model"); check("Saved semantic picker restores provider/model without stage URL", model.value === "saved-generative" && $("#rag-semantic-provider").value === "compatible" && $("#rag-size-label").textContent.includes("Максимальный"));
    server.respond("GET", route, {models: [{id: "new-host-model"}]});
    input.value = "http://127.0.0.1:9004/v1"; input.dispatchEvent(new Evt("input")); input.dispatchEvent(new Evt("change")); await settle();
    late.resolve(json({models: [{id: "late-old-host"}]})); await settle();
    check("Global URL change cancels old compatible catalogue without losing model", requests("GET", route)[0].signal.aborted && model.value === "saved-generative" && $("#rag-semantic-model-catalogue").children.some(n => n.value === "new-host-model") && !$("#rag-semantic-model-catalogue").children.some(n => n.value === "late-old-host"));
    const hidden = deferred(); server.respond("GET", route, () => hidden.promise); click("rag-model-refresh"); await settle(); click("rag-step-documents"); await settle();
    hidden.resolve(json({models: [{id: "late-hidden"}]})); await settle();
    check("Leaving model stage aborts its request and rejects hidden response", requests("GET", route).at(-1).signal.aborted && !$("#rag-semantic-model-catalogue").children.some(n => n.value === "late-hidden"));
  });
  await scenario("Chat and rerank use independent shared selectors and saved ranking report", async () => {
    const a = {chunk_id: "a", title: "Neutral A", section: "First", score: .8, text: "<b>full text A</b>", decision: "kept"};
    const b = {chunk_id: "b", title: "Neutral B", section: "Second", score: .6, text: "full text B", decision: "kept"};
    const rag = {version: 2, query: "neutral", index: {index_id: "saved-reranked"}, config: {rerank_enabled: true, filter_enabled: false, rewrite_enabled: false, top_k: 2},
      rerank: {enabled: true, provider: "compatible", model: "ranker-only", source_ids: [2,1], usage: {total_tokens: 30}}, candidates: [a,b], hits: [b,a], context: "exact saved ranked context"};
    const {client, server, $, click, requests, open, send} = freshClient({agents: [{provider: "compatible", model: "chat-only", rag_enabled: true,
      transcript: [turns[0], {...turns[1], rag}], history_len: 2}, {provider: "openrouter", model: "cloud-only"}]});
    server.respond("GET", "/api/agents", () => json({has_key: false, agents: server.state.agents, live: 2, max_agents: 1000}));
    server.respond("POST", "/api/agents/ag_1/messages", () => stream(success));
    client.init(); await settle(); check("Compatible chat remains available without OpenRouter key", !$("#send").disabled && $("#f-provider").value === "compatible");
    click("chat-settings"); click("tab-btn-rag"); await settle();
    check("Both counts stay visible without a cosine filter", !$("#rag-chat-parameters").classList.contains("hidden") && !!$("#f-rag_candidates_k") && !!$("#f-rag_final_k") && !$("#f-rag_filter_enabled") && !$("#rag-threshold-field").classList.contains("hidden") && $("#rag-rerank-fields").classList.contains("hidden"));
    $("#f-rag_rerank_enabled").checked = true; $("#f-rag_rerank_enabled").dispatchEvent(new Evt("change"));
    $("#f-rag_rerank_provider").value = "compatible"; $("#f-rag_rerank_provider").dispatchEvent(new Evt("change"));
    $("#f-rag_rerank_model").value = "ranker-only"; $("#f-rag_rerank_model").dispatchEvent(new Evt("input")); $("#f-rag_rerank_model").dispatchEvent(new Evt("change")); await settle();
    check("Rerank picker saves own provider/model and preserves chat model", requests("PATCH", "/api/agents/ag_1").at(-1).body.rag_rerank_provider === "compatible" && requests("PATCH", "/api/agents/ag_1").at(-1).body.rag_rerank_model === "ranker-only" && $("#f-model").value === "chat-only" && !$("#rag-rerank-fields").classList.contains("hidden"));
    const saved = JSON.stringify(rag), before = requests("GET", /^\/api\/rag\//).length;
    $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const snapshot = $("#rag-answer-snapshot"), rows = snapshot.querySelectorAll(".rag-snapshot-candidate");
    check("Saved rerank report shows original/final ranks and cosine only", rows[0].textContent.includes("2 → 1") && rows[1].textContent.includes("1 → 2") && snapshot.textContent.includes("ranker-only") && !snapshot.textContent.includes("similarity_threshold"));
    rows[0].querySelector("button").dispatchEvent(new Evt("click"));
    check("Selecting one candidate shows its complete saved text without index reads", snapshot.querySelectorAll(".rag-snapshot-text").length === 1 && snapshot.querySelector(".rag-snapshot-text").textContent === b.text && requests("GET", /^\/api\/rag\//).length === before && JSON.stringify(client.state.current.transcript[1].rag) === saved);
    snapshot.querySelector("button").dispatchEvent(new Evt("click")); await settle(); click("workspace-chat"); send("neutral request"); await settle();
    check("Compatible chat posts without an OpenRouter key", requests("POST", "/api/agents/ag_1/messages").length === 1);
    open(1); await settle(); check("OpenRouter chat still needs its configured key", $("#send").disabled && $("#f-provider").value === "openrouter");
    open(0); await settle(); check("Chat switch restores independent provider and rerank selection", $("#f-provider").value === "compatible" && $("#f-model").value === "chat-only" && $("#f-rag_rerank_model").value === "ranker-only");
  });
  await scenario("All saved candidates appear in final order with a selected prefix", async () => {
    const candidates = [
      {chunk_id: "a", title: "Neutral A", score: .8, text: "full A", original_rank: 1, final_rank: 3, selected: false},
      {chunk_id: "b", title: "Neutral B", score: .6, text: "full B", original_rank: 2, final_rank: 2, selected: false},
      {chunk_id: "c", title: "Neutral C", score: -.1, text: "full C", original_rank: 3, final_rank: 1, selected: true}];
    const rag = {version: 3, query: "neutral", index: {index_id: "saved-selection"},
      config: {candidates_k: 3, final_k: 1, rewrite_enabled: false, rerank_enabled: true},
      rerank: {enabled: true, model: "saved-ranker", source_ids: [3,2,1]},
      selection: {ordering: "rerank", ordered_source_ids: [3,2,1], selected_source_ids: [3]},
      candidates, hits: [candidates[2]], context: "selected full C"};
    const saved = JSON.stringify(rag);
    const {client, $, requests, server, open} = freshClient({agents: [{transcript: [turns[0], {...turns[1], rag}], history_len: 2}, {}]});
    client.init(); await settle(); const before = requests("GET", /^\/api\/rag\//).length;
    $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const report = $("#rag-answer-snapshot"), rows = report.querySelector("tbody").querySelectorAll("tr");
    check("Three columns and all candidates follow final rank", same(Array.from(report.querySelectorAll("th")).map(n => n.textContent), ["Источник", "До → после", "Cosine"])
      && rows.length === 3 && rows[0].textContent.includes("Neutral C") && rows[0].textContent.includes("3 → 1") && rows[0].textContent.includes("-0.1000") && rows[2].textContent.includes("Neutral A") && rows[2].textContent.includes("1 → 3"));
    check("Only selected prefix has its unobtrusive source marker", rows[0].querySelector(".rag-context-selection")?.textContent === "В контексте" && !rows[1].querySelector(".rag-context-selection") && !rows[2].querySelector(".rag-context-selection"));
    rows[2].querySelector("button").dispatchEvent(new Evt("click"));
    check("Unselected full candidate text stays available without live index or mutation", report.querySelector(".rag-snapshot-text").textContent === "full A" && report.querySelector(".rag-snapshot-context").textContent === rag.context && requests("GET", /^\/api\/rag\//).length === before && JSON.stringify(client.state.current.transcript[1].rag) === saved);
    const off = {...rag, config: {...rag.config, rerank_enabled: false}, rerank: undefined,
      selection: {ordering: "cosine", ordered_source_ids: [1,2,3], selected_source_ids: [1]},
      candidates: candidates.map((c, i) => ({...c, final_rank: i + 1, selected: i === 0})), hits: [candidates[0]]};
    server.state.agents[0].transcript[1].rag = off;
    open(1); await settle(); open(0); await settle();
    $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    check("Rerank OFF retains all cosine candidates but no ranking diagnostics", report.querySelector("tbody").querySelectorAll("tr").length === 3 && same(Array.from(report.querySelectorAll("th")).map(n => n.textContent), ["Источник", "Cosine"])
      && !report.textContent.includes("saved-ranker") && !report.textContent.includes("До → после") && !report.textContent.includes("rerank_enabled") && !report.textContent.includes("original_rank") && !report.textContent.includes("final_rank"));
  });
  await scenario("Legacy filtered rerank ranks use saved final identity", async () => {
    const a = {chunk_id: "rejected", title: "Rejected A", score: .1, decision: "threshold", text: "full rejected"};
    const b = {chunk_id: "b", title: "Saved B", score: .8, decision: "kept", text: "full B"};
    const c = {chunk_id: "c", title: "Saved C", score: .6, decision: "kept", text: "full C"};
    const rag = {version: 2, query: "neutral", config: {filter_enabled: true, rerank_enabled: true},
      rerank: {source_ids: [2,1]}, candidates: [a,b,c], hits: [c,b], context: "saved C B"};
    const {client, $} = freshClient({agents: [{transcript: [turns[0], {...turns[1], rag}], history_len: 2}]});
    client.init(); await settle(); $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const rows = $("#rag-answer-snapshot").querySelector("tbody").querySelectorAll("tr");
    check("Legacy excluded candidate cannot shift saved permutation IDs", rows[0].textContent.includes("Saved C") && rows[0].textContent.includes("2 → 1") && rows[1].textContent.includes("Saved B") && rows[1].textContent.includes("1 → 2") && rows[2].textContent.includes("Rejected A") && rows[2].textContent.includes("— → —") && rows[2].textContent.includes("Ниже порога"));
  });
  await scenario("Configured rerank without a result does not claim a completed ranking", async () => {
    const rag = {version: 2, query: "neutral no-hit", index: {index_id: "saved-nohit"},
      config: {rerank_enabled: true, filter_enabled: true, top_k: 1, similarity_threshold: .3},
      candidates: [{chunk_id: "below", title: "Neutral below threshold", score: .1, decision: "threshold", text: "full rejected text"}], hits: [], context: ""};
    const {client, $, requests} = freshClient({agents: [{transcript: [turns[0], {...turns[1], rag}], history_len: 2}]});
    client.init(); await settle(); const before = requests("GET", /^\/api\/rag\//).length;
    $("#feed").querySelector(".card-rag").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    const node = $("#rag-answer-snapshot");
    check("No-hit report shows rejection and enabled-but-unperformed rerank", node.textContent.includes("Ниже порога") && node.textContent.includes("не выполнялось или не завершилось") && !node.textContent.includes("До → после") && !node.querySelector(".rag-rank") && requests("GET", /^\/api\/rag\//).length === before);
  });
  await scenario("RAG stage buttons use actual fields and preview before publication", async () => {
    const {client, server, $, click, requests} = freshClient({agents: []});
    const stages = {corpus: {fingerprint: "corpus-one", documents: 1, words: 20, urls: ["https://example.test/one"]},
      chunks: {fingerprint: "chunks-one", chunks: 1}, embeddings: null};
    server.respond("GET", "/api/rag/status", {state: "missing", stages, operation: {state: "complete", kind: "chunks"}, index: null});
    const doc = {document_id: "doc", title: "Neutral", words: 20};
    server.respond("GET", "/api/rag/documents?offset=0&limit=25&working=true", {items: [doc]});
    server.respond("GET", "/api/rag/documents/doc?offset=0&limit=10000", {text: "Neutral clean document", characters: 22});
    server.respond("GET", "/api/rag/documents/doc/chunks?offset=0&limit=25&working=true", {items: [{chunk_id: "chunk", start: 0, end: 22}]});
    server.respond("GET", "/api/rag/chunks/chunk?working=true", {chunk_id: "chunk", text: "Neutral clean document"});
    server.respond("POST", "/api/rag/operations/chunks", {operation_id: "split-one"});
    server.respond("POST", "/api/rag/operations/embeddings", {operation_id: "embed-one"});
    server.respond("DELETE", "/api/rag/stages/chunks", {cleared: "chunks"});
    server.respond("GET", "/api/models?provider=openrouter&purpose=generation", {models: [{id: "offline-boundaries"}]});
    server.respond("GET", "/api/models?provider=compatible&purpose=generation", {models: [{id: "local-generative"}]});
    server.respond("GET", "/api/models?provider=compatible&purpose=embedding", {models: [{id: "offline-model"}]});
    client.init(); await settle(); click("application-settings"); click("tab-btn-rag"); await settle();
    check("Loaded working docs are available without any published index", $("#rag-documents").textContent.includes("Neutral") && $("#rag-save").disabled);
    $("#rag-documents").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    $("#rag-chunks").querySelector("button").dispatchEvent(new Evt("click")); await settle();
    check("Document and chunk preview available before embeddings", $("#rag-document-preview").textContent.includes("Neutral clean document")
      && $("#rag-chunk").textContent.includes("Neutral clean document") && $("#rag-chunk").querySelectorAll("details").at(-1).hidden);
    $("#rag-strategy").value = "fixed"; $("#rag-size").value = "512"; $("#rag-overlap").value = "64";
    click("rag-split"); await settle();
    check("Fixed chunk form retains size label and sends configured character size and overlap", $("#rag-size-label").textContent === "Размер чанка, символов" && same(requests("POST", "/api/rag/operations/chunks")[0]?.body, {strategy: "fixed", size: 512, overlap: 64}));
    click("rag-step-chunks");
    $("#rag-strategy").value = "semantic"; $("#rag-strategy").dispatchEvent(new Evt("change"));
    await settle();
    $("#rag-semantic-model").value = "offline-boundaries"; $("#rag-semantic-model").dispatchEvent(new Evt("change"));
    click("rag-split"); await settle();
    check("Semantic choice sends independent provider/model", !$("#rag-semantic-fields").hidden && $("#rag-size-label").textContent === "Максимальный размер чанка, символов" && same(requests("POST", "/api/rag/operations/chunks").at(-1)?.body,
      {strategy: "semantic", size: 512, overlap: 64, semantic_provider: "openrouter", semantic_reasoning_enabled: false, semantic_model: "offline-boundaries"}));
    $("#rag-semantic-provider").value = "compatible"; $("#rag-semantic-provider").dispatchEvent(new Evt("change"));
    await settle();
    check("Compatible generation starts with its own model draft", $("#rag-semantic-model").value === "");
    $("#rag-semantic-model").value = "local-generative";
    click("rag-split"); await settle();
    check("Local semantic stage sends explicit auth and generative model", requests("POST", "/api/rag/operations/chunks").at(-1).body.semantic_provider === "compatible" && requests("POST", "/api/rag/operations/chunks").at(-1).body.semantic_model === "local-generative");
    $("#rag-semantic-provider").value = "openrouter"; $("#rag-semantic-provider").dispatchEvent(new Evt("change"));
    check("Switching provider restores generative drafts", $("#rag-semantic-model").value === "offline-boundaries");
    click("rag-step-embeddings"); await settle();
    $("#rag-model").value = "offline-model";
    $("#rag-dimensions").value = "3"; $("#rag-revision").value = "fixture";
    click("rag-embed"); await settle();
    check("Embedding button uses mounted fields and never starts save", same(requests("POST", "/api/rag/operations/embeddings")[0]?.body,
      {reasoning_enabled: false, provider: "compatible", model: "offline-model", dimensions: 3, revision: "fixture"}) && requests("POST", "/api/rag/operations/save").length === 0);
    stages.embeddings = {embedding_fingerprint: "vectors-one"};
    const vectorPath = "/api/rag/chunks/chunk?vector=true&working=true";
    server.respond("GET", vectorPath, {vector: [1, 0, 0]});
    await settle(1100);
    const vector = $("#rag-chunk").querySelectorAll("details").at(-1);
    vector.open = true; await vector.ontoggle();
    check("Embedding arrival keeps selected chunk and exposes actual vector", !vector.hidden && vector.textContent.includes("[1,0,0]"));
    const oldVector = deferred(); stages.embeddings = {embedding_fingerprint: "vectors-two"};
    server.respond("GET", vectorPath, () => oldVector.promise); await settle(1100);
    stages.embeddings = {embedding_fingerprint: "vectors-three"};
    server.respond("GET", vectorPath, {vector: [0, 0, 1]}); await settle(1100);
    oldVector.resolve(json({vector: [0, 1, 0]})); await settle();
    check("Changed model invalidates old and late vector while preserving selected chunk/details", vector.open && vector.textContent.includes("[0,0,1]")
      && !vector.textContent.includes("[0,1,0]") && $("#rag-chunk").textContent.includes("Neutral clean document"));
    server.respond("DELETE", "/api/rag/stages/embeddings", () => { stages.embeddings = null; return json({cleared: "embeddings"}); });
    click("rag-delete-embeddings"); await settle();
    check("Deleting embeddings removes mounted numerical vector and preserves chunk selection", vector.hidden && !vector.querySelector("pre")
      && $("#rag-chunk").textContent.includes("Neutral clean document") && $("#rag-save").disabled);
    click("rag-delete-chunks"); await settle();
    check("Delete chunks uses whitelisted stage endpoint", requests("DELETE", "/api/rag/stages/chunks").length === 1);
  });
  await scenario("Conditional selected-chat refresh uses revision and catches replacement", async () => {
    const old = { ...turns[1], content: "старый ответ" };
    const { client, server, $, requests } = freshClient({ agents: [{
      transcript: [turns[0], old], history_len: 2, history_revision: "rev-a",
    }] });
    const samePath = "/api/agents/ag_1?known_history_revision=rev-a";
    server.respond("GET", samePath, { unchanged: true, history_revision: "rev-a", busy: true });
    client.init(); await settle(1130);
    check("Unchanged poll transfers no transcript and reports running state",
      requests("GET", samePath).length === 1 && $("#feed").textContent.includes("старый ответ")
      && $("#chat-status").textContent.includes("Выполняется"));
    server.respond("GET", samePath, { ...server.state.agents[0],
      transcript: [turns[0], { ...old, content: "исправленный ответ" }],
      history_revision: "rev-b", busy: false });
    await settle(1070);
    check("Revision change updates same-length replacement",
      $("#feed").textContent.includes("исправленный ответ") && !$("#feed").textContent.includes("старый ответ"));
  });

  await scenario("Owned running reminder is cancellable from Tools", async () => {
    const { client, server, $, click, requests } = freshClient({ agents: [{ busy: true }] });
    const base = { servers: [{ name: "remind", url: "http://127.0.0.1:8018/mcp", status: "ok", tools: [],
      reminders: { waiting: 0, fired: 0, running: 1, items: [
        { id: 7, text: "active", status: "running", state: "выполняется", fired: 0, due_at: 1700000000, can_cancel: true },
        { id: 8, text: "other chat", status: "pending", state: "ждёт", fired: 0, due_at: 1700000000, can_cancel: false },
      ] } }], config: { revision: 1, servers: [] } };
    server.respond("GET", "/api/mcp", base);
    const path = "/api/agents/ag_1/reminders/remind/7/cancel";
    server.respond("POST", path, () => {
      server.respond("GET", "/api/mcp", { ...base, servers: [{ ...base.servers[0],
        reminders: { waiting: 1, fired: 0, items: [base.servers[0].reminders.items[1]] } }] });
      return json({ cancelled: true });
    });
    client.init(); await settle(30); click("chat-settings"); await settle(); click("tab-btn-mcp"); await settle(10);
    const buttons = $("#mcp-list").querySelectorAll("button").filter((node) => node.textContent === "Снять");
    check("Tools offers cancellation only for owned occurrence while chat is busy", buttons.length === 1);
    buttons[0].dispatchEvent(new Evt("click")); await settle(15);
    check("Cancellation uses scoped route and refreshes Tools without a message",
      requests("POST", path).length === 1 && requests("POST", /\/messages$/).length === 0
      && $("#mcp-list").querySelectorAll("button").filter((node) => node.textContent === "Снять").length === 0);
  });

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
    click("chat-settings"); click("workspace-chat");
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
    click("chat-settings");
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
    button(card, "Информация о запросе").dispatchEvent(new Evt("click"));
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
    click("application-settings"); click("tab-btn-profile"); await settle(10);
    check("Direct settings opens global profile editor", client.state.section === "profile" && $("#profile-style").value === "кратко" && requests("GET", "/api/profile").length === 1);
    $("#profile-style").value = "ясно fixture"; $("#profile-style").dispatchEvent(new Evt("input")); $("#profile-style").dispatchEvent(new Evt("change")); await settle(10);
    check("Profile PATCH sends only touched field and displays API result", same(requests("PATCH", "/api/profile")[0]?.body, { style: "ясно fixture" }) && $("#profile-style").value === "ясно ***" && $("#profile-format").value === "списком" && requests("PATCH", /^\/api\/agents\//).length === 0);
    server.respond("PATCH", "/api/profile", () => failure("профиль занят"));
    $("#profile-context").value = "не терять"; $("#profile-context").dispatchEvent(new Evt("input")); $("#profile-context").dispatchEvent(new Evt("change")); await settle(10);
    click("workspace-chat"); click("application-settings"); await settle(10);
    check("Profile failure survives reread without losing dirty text", $("#profile-context").value === "не терять" && $("#profile-status").textContent.includes("занят"));
    server.respond("PATCH", "/api/profile", () => json({ profile: { format: "списком", context: "не терять" } }));
    $("#profile-context").dispatchEvent(new Evt("change")); await settle(10);
    server.respond("GET", "/api/profile", { profile: { format: "списком", context: "не терять" } });
    open(1); await settle(20); click("application-settings"); click("tab-btn-profile"); await settle(10);
    check("Profile data follows global API across chats", $("#profile-context").value === "не терять");
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
    server.respond("GET", "/api/memory", memory.long_term);
    server.respond("GET", "/api/invariants", { records: [record], total: 1 });
    server.respond("PATCH", base + "/37", () => json(changed));
    server.respond("POST", base, () => json({ ...changed, seq: 84 }));
    server.respond("DELETE", base + "/37", () => json({ deleted: 37 }));
    client.init(); await settle(30); click(isWorking ? "chat-settings" : "application-settings"); click(isInvariant ? "tab-btn-invariants" : "tab-btn-memory"); await settle(10);
    $(container).querySelectorAll(".mini")[0].dispatchEvent(new Evt("click"));
    const editor = $(container).querySelector(".mem-edit");
    const select = $(container).querySelector(".mem-edit-kind");
    editor.value = "saved fixture"; select.value = changed.kind;
    if (isInvariant) $(container).querySelector(".mem-edit-banned").value = "C++, .NET";
    if (kind === "long") {
      server.respond("PATCH", base + "/37", () => failure("память занята"));
      editor.dispatchEvent(new Evt("keydown", { key: "Enter" })); await settle(10);
      click("workspace-chat"); click("application-settings"); await settle(10);
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
    client.init(); await settle(30); click("chat-settings"); click("tab-btn-memory"); await settle(10);
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
    client.init(); await settle(30); click("application-settings"); click("tab-btn-mcp"); await settle(10);
    check("MCP renders callable name, schema and down reason", $("#mcp-list").textContent.includes("local__echo") && $("#mcp-list").textContent.includes('"type": "string"') && $("#mcp-list").textContent.includes("fixture unavailable"));
    const pending = deferred(); server.respond("GET", "/api/mcp", () => pending.promise);
    click("workspace-chat"); click("application-settings");
    const controller = client.state.mcpRequest; click("workspace-chat");
    pending.resolve(json({ servers: [{ name: "LATE SERVER", status: "ok", tools: [] }] })); await settle(10);
    check("Leaving MCP aborts and ignores late response", controller.signal.aborted && !$("#mcp-list").textContent.includes("LATE SERVER"));
    server.respond("GET", "/api/mcp", { servers });
    const before = requests("GET", "/api/mcp").length;
    document.visibilityState = "hidden"; click("application-settings"); await settle(10);
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
    client.init(); await settle(30); click("chat-settings"); await settle(); click("tab-btn-mcp"); await settle(10);
    const listing = $("#mcp-list");
    check("Reminder aggregate uses server counts/id/text and recurring occurrences " + classic,
      listing.querySelectorAll(".mem-reminders").length === 1 && listing.textContent.includes("ждёт: 1 · сработало: 1")
      && listing.textContent.includes("№18 — <script>offline reminder</script>") && listing.textContent.includes("раз: 4 · следующее в") && !listing.querySelector("script"));
    check("Read-only reminder page has no form or chat request " + classic,
      !listing.querySelector("input") && !listing.querySelector("button") && requests("POST", /\/messages$/).length === 0);
    const fired = { ...reminder, state: "сработало", fired: 1 };
    server.respond("GET", "/api/mcp", response(fired));
    const before = requests("GET", "/api/mcp").length;
    await settle(2100);
    check("Scheduled 2s GET refreshes fired reminder state " + classic,
      requests("GET", "/api/mcp").length === before + 1 && listing.textContent.includes("ждёт: 0 · сработало: 2")
      && !listing.querySelector(".mcp-tool") && client.state.mcpTimer !== null);

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
    server.respond("GET", "/api/mcp", response(fired)); click("chat-settings"); await settle(10);
    // The aborted request resolves after the new entry has already rendered.
    pending.resolve(json({ servers: [{ name: "LATE SERVER", tools: [] }] })); await settle(10);
    check("Workspace exit aborts GET and rejects old epoch after reentry " + classic,
      controller.signal.aborted && !listing.textContent.includes("LATE SERVER") && listing.textContent.includes("ждёт: 0 · сработало: 2") && client.state.mcpTimer !== null);
    window.dispatchEvent(new Evt("pagehide"));
    const closedCount = requests("GET", "/api/mcp").length;
    await settle(2100);
    check("Pagehide stops all scheduled requests " + classic, client.state.mcpTimer === null && requests("GET", "/api/mcp").length === closedCount);
  });
  await scenario("Persisted outbound JSON info and legacy unavailable", async () => {
    const bodies = [
      { model: "sent/model", temperature: .37, max_tokens: 17, messages: [{ role: "user", content: "<script>question</script>" }], tools: [{ type: "function", function: { name: "remote__ping" } }] },
      { model: "sent/model", temperature: .37, messages: [{ role: "tool", tool_call_id: "call_1", content: "pong" }], tools: [{ type: "function", function: { name: "remote__ping" } }] },
    ];
    const { client, $, click } = freshClient({ agents: [{ transcript: [turns[0], { ...turns[1], request_bodies: bodies }, turns[0], turns[1]], history_len: 4 }] });
    client.init(); await settle(30); click("workspace-chat");
    const cards = $("#feed").querySelectorAll(".card");
    button(cards[0], "Информация о запросе").dispatchEvent(new Evt("click"));
    const displayed = cards[0].querySelectorAll(".request-json").map((node) => JSON.parse(node.textContent));
    check("Cold page renders exact persisted rounds including tools", same(displayed, bodies) && client.state.prompts.size === 0);
    check("JSON is text and never executes model markup", !cards[0].querySelector("script") && cards[0].querySelector(".request-json").textContent.includes("<script>"));
    $("#f-temperature").value = ".99"; $("#f-temperature").dispatchEvent(new Evt("change")); await settle(10);
    check("Edited settings do not rewrite request info", same(cards[0].querySelectorAll(".request-json").map((node) => JSON.parse(node.textContent)), bodies));
    button(cards[1], "Информация о запросе").dispatchEvent(new Evt("click"));
    check("Legacy message explicitly says JSON is unavailable", cards[1].querySelector(".prompt-view").textContent.includes("JSON запроса недоступен") && !cards[1].querySelector(".request-json"));
  });

  await scenario("MCP URL save/connect, draft preservation and stale GET", async () => {
    const { client, server, $, click, requests } = freshClient();
    const empty = { servers: [], config: { revision: 0, servers: [] } };
    const rows = [{ name: "custom", url: "http://127.0.0.1:8016/mcp", enabled: false }];
    const saved = { servers: [{ ...rows[0], status: "disconnected", tools: [] }], config: { revision: 1, servers: rows } };
    server.respond("GET", "/api/mcp", empty);
    client.init(); await settle(30); click("application-settings"); click("tab-btn-mcp"); await settle(10);
    const name = $(".mcp-name"), url = $(".mcp-url");
    name.value = "custom"; name.dispatchEvent(new Evt("input"));
    url.value = rows[0].url; url.dispatchEvent(new Evt("input"));
    const late = deferred(); server.respond("GET", "/api/mcp", () => late.promise);
    click("workspace-chat"); click("application-settings");
    const oldController = client.state.mcpRequest;
    const saving = deferred(); server.respond("PUT", "/api/mcp/config", () => saving.promise);
    $("#mcp-config-form").requestSubmit(); $("#mcp-config-form").requestSubmit();
    check("URL form submits one revision-checked save", requests("PUT", "/api/mcp/config").length === 1 && same(requests("PUT", "/api/mcp/config")[0].body, { revision: 0, servers: rows }));
    late.resolve(json(empty)); saving.resolve(json(saved)); await settle(15);
    check("Config mutation aborts stale GET and renders saved URL", oldController.signal.aborted && client.state.mcpConfig.revision === 1 && $(".mcp-url").value === rows[0].url);
    const connected = { servers: [{ ...rows[0], status: "ok", tools: [{ name: "ping", schema: {} }] }], config: { revision: 2, servers: [{ ...rows[0], enabled: true }] } };
    server.respond("POST", "/api/mcp/connect", connected);
    $("#mcp-list").querySelectorAll("button").find((b) => b.textContent === "Подключить").dispatchEvent(new Evt("click")); await settle(10);
    check("Connect uses persisted row name and revision", same(requests("POST", "/api/mcp/connect")[0].body, { name: "custom", revision: 1 }) && $("#mcp-list").textContent.includes("Подключён"));
    const edited = "http://127.0.0.1:8017/mcp";
    $(".mcp-url").value = edited; $(".mcp-url").dispatchEvent(new Evt("input"));
    const savingAgain = deferred(); server.respond("PUT", "/api/mcp/config", () => savingAgain.promise);
    $("#mcp-config-form").requestSubmit();
    check("Changed URL is saved disconnected", requests("PUT", "/api/mcp/config")[1].body.servers[0].enabled === false);
    $(".mcp-url").value = "http://127.0.0.1:8018/mcp"; $(".mcp-url").dispatchEvent(new Evt("input"));
    savingAgain.resolve(json({ ...saved, config: { revision: 3, servers: [{ ...rows[0], url: edited }] } })); await settle(10);
    check("Edit typed while save is pending survives its response", $(".mcp-url").value.endsWith("8018/mcp") && client.state.mcpDirty);
    server.respond("PUT", "/api/mcp/config", () => failure("fixture validation refusal", 400));
    $("#mcp-config-form").requestSubmit(); await settle(10);
    check("Save refusal preserves URL draft and visible reason", $(".mcp-url").value.endsWith("8018/mcp") && $("#mcp-config-status").textContent.includes("fixture validation refusal"));
    const delayedConnect = deferred(); server.respond("POST", "/api/mcp/connect", () => delayedConnect.promise);
    client.state.mcpDirty = false;
    $("#mcp-list").querySelectorAll("button").find((b) => b.textContent === "Подключить").dispatchEvent(new Evt("click"));
    const hiddenText = $("#mcp-list").textContent;
    click("workspace-chat");
    delayedConnect.resolve(json({ ...saved, config: { revision: 4, servers: rows } })); await settle(10);
    check("Late connection result updates version without populating hidden page", client.state.mcpConfig.revision === 4 && client.state.workspace === "chat" && $("#mcp-list").textContent === hiddenText);
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
