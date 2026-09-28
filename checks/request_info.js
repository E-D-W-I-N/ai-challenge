// Compare the actual provider bodies with the persisted transcript in the real UI.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { boot, settle, Evt } = require("./dom.js");

async function main() {
  const fixture = JSON.parse(fs.readFileSync(0, "utf8"));
  const html = fs.readFileSync(path.join(__dirname, "../app/static/index.html"), "utf8");
  const env = boot(html, { agents: [{ transcript: fixture.transcript, history_len: fixture.transcript.length, temperature: .99 }] });
  const $ = (selector) => env.document.querySelector(selector);
  const client = require("../app/static/app.js");
  client.init(); await settle(30);
  $("#workspace-chat").dispatchEvent(new Evt("click"));
  const card = $("#feed").querySelector(".card");
  const info = card.querySelectorAll("button").find((button) => button.title === "Информация о запросе");
  info.dispatchEvent(new Evt("click"));
  const displayed = () => card.querySelectorAll(".request-json").map((node) => JSON.parse(node.textContent));
  assert.deepEqual(displayed(), fixture.received);
  assert.equal(client.state.prompts.size, 0, "cold page must use persisted bodies");
  assert.equal(card.querySelector("script"), null);
  assert(card.querySelector(".request-json").textContent.includes("<script>question</script>"));
  $("#f-temperature").value = ".12";
  $("#f-temperature").dispatchEvent(new Evt("change")); await settle(10);
  assert.deepEqual(displayed(), fixture.received, "edited settings changed the captured request");
  globalThis.window.dispatchEvent(new Evt("pagehide"));
  console.log(`Actual ${fixture.received.length}-round provider bodies equal persisted/escaped client JSON after config change`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
