// Explicit HTTP/SSE scenario data. No prompt, task, validation or usage engine:
// Python checks exercise those server contracts against the real backend.
const { TextEncoder } = require("util");
const clone = (data) => JSON.parse(JSON.stringify(data));
const json = (data, status = 200) => ({ ok: status < 400, status, json: async () => clone(data) });
const failure = (detail, status = 503) => json({ detail }, status);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

function stream(frames, { delay = 0, finish = () => {}, fragmented = false, signal, beforeRead = async () => {} } = {}) {
  const encoded = new TextEncoder().encode(frames.map((e) => "data: " + JSON.stringify(e) + "\n\n").join(""));
  // One-byte fragments deliberately split Cyrillic UTF-8 and SSE boundaries.
  const chunks = fragmented ? Array.from(encoded, (byte) => Uint8Array.of(byte))
    : frames.map((e) => new TextEncoder().encode("data: " + JSON.stringify(e) + "\n\n"));
  let index = 0;
  return { ok: true, status: 200, body: { getReader: () => ({ read: async () => {
    await beforeRead(index);
    if (delay) await new Promise((done) => setTimeout(done, delay));
    if (signal?.aborted) throw Object.assign(new Error("Stream aborted"), { name: "AbortError" });
    if (index < chunks.length) return { done: false, value: chunks[index++] };
    finish();
    return { done: true };
  } }) } };
}

const start = {
  event: "start", strategy: "full", memory_at: null, working_at: null,
  task_at: null, summary_at: null,
  resolved_messages: [{ role: "user", content: "вопрос" }],
};
const success = [start, { event: "delta", text: "ответ модели" },
  { event: "done", text: "ответ модели", committed: true, metrics: null }];
const turns = [{ role: "user", content: "вопрос", metrics: null },
  { role: "assistant", content: "ответ модели", metrics: null }];
const planning = { stage: "planning", label: "планирование", description: "задача",
  step: "", expects: "разложить задачу на шаги, затем /task-next" };
const execution = { stage: "execution", label: "выполнение", description: "задача",
  step: "", expects: "сделать текущий шаг, затем /task-next" };

function buildServer(options = {}) {
  const agents = (options.agents || [{}, { label: "второй чат", model: "вторая/модель" }]).map((seed, i) => ({
    id: "ag_" + (i + 1), label: "первый чат", model: "первая/модель", system: "СТАРЫЙ ПРОМПТ",
    stop: null, response_format: null, extra_body: {}, strategy: "full", keep_last: null,
    compress_every: null, temperature: null, max_tokens: null, top_p: null, top_k: null,
    min_p: null, repetition_penalty: null, presence_penalty: null, frequency_penalty: null,
    transcript: [], history_len: 0, usage_total: null, branch: null, busy: false, task: null, ...clone(seed),
  }));
  const state = { agents, requests: [] };
  const routes = new Map(Object.entries(options.routes || {}));
  const respond = (method, path, answer) => routes.set(method + " " + path, answer);
  async function fetchStub(path, init = {}) {
    const request = { method: (init.method || "GET").toUpperCase(), path,
      body: init.body ? JSON.parse(init.body) : null, signal: init.signal };
    state.requests.push(request);
    const key = request.method + " " + path;
    if (routes.has(key)) {
      const answer = routes.get(key);
      return typeof answer === "function" ? answer(request) : json(answer);
    }
    if (path.startsWith("/api/models") && request.method === "GET") return json({ models: [
      { id: "первая/модель", supported_parameters: ["temperature", "top_p", "stop", "response_format"] },
      { id: "вторая/модель", supported_parameters: [] },
    ] });
    if (path === "/api/agents" && request.method === "GET") return json({
      has_key: true, live: agents.length, max_agents: 1000, agents,
    });
    const agent = agents.find((a) => path === "/api/agents/" + a.id);
    if (agent && request.method === "GET") return json(agent);
    // A transport echo lets panel tests inspect the config saved before send.
    // It performs no validation, defaults, redaction or backend computation.
    if (agent && request.method === "PATCH") { Object.assign(agent, request.body); return json(agent); }
    throw new Error("Scenario has no response for " + key);
  }
  return { state, respond, fetchStub };
}

module.exports = { buildServer, json, failure, deferred, stream, start, success, turns, planning, execution };
