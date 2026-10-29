// Behaviour test of clara/app/index.html in jsdom: boot with a token, render a
// snapshot, stream events, show the approval sheet, answer it. Run with
//   node tests/app_page_test.mjs            (needs: npm install jsdom)
// pytest runs it through tests/test_app_page.py when node and jsdom are present.
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "clara", "app", "index.html"), "utf8");

const calls = [];
const snapshot = {
  id: "s1", project: "/proj", busy: false, mode: "default", pending: [], todo: [], seq: 0, title: "earlier question",
  messages: [
    { role: "user", content: "[context: project=/proj, date=2026-10-25, os=Linux]\n\nearlier question" },
    { role: "assistant", content: "", tool_calls: [{ id: "c1", type: "function", function: { name: "read_file", arguments: "{\"path\": \"hello.txt\"}" } }] },
    { role: "tool", name: "read_file", content: "1\thello" },
    { role: "assistant", content: "Here is **bold** and `code`:\n```py\nprint(1)\n```" },
  ],
};
const responses = {
  "GET /api/status": { version: "0.1.0", model: "qwen", base_url: "http://127.0.0.1:8000/v1", server: { kind: "llama-server", n_ctx: 8192 }, project: "/proj", sandbox: "off", allowed_hosts: ["127.0.0.1"], guard: { allowed: 1, blocked: 0 } },
  "GET /api/audit": { entries: [] },
  "GET /api/sessions": { sessions: [{ id: "s1", title: "earlier question", messages: 4, busy: false }] },
  "GET /api/sessions/s1": snapshot,
  "POST /api/sessions/s1/messages": { ok: true },
  "POST /api/sessions/s1/ticket": { ticket: "tkt-1", ttl: 120 },
  "POST /api/permissions/p1": { ok: true },
};

const dom = new JSDOM(html, { url: "http://127.0.0.1:8765/#token=abc", runScripts: "dangerously", pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (path, opts) => {
      const method = (opts && opts.method) || "GET";
      calls.push({ method, path, auth: opts && opts.headers && opts.headers.Authorization, body: opts && opts.body });
      const key = method + " " + path.split("?")[0];
      if (!(key in responses)) return { ok: false, status: 404, json: async () => ({ error: "no mock for " + key }) };
      return { ok: true, status: 200, json: async () => responses[key] };
    };
    const sources = [];
    window.EventSource = class { constructor(url) { this.url = url; sources.push(this); } close() {} };
    window.__sources = sources;
    window.scrollTo = () => {};
    window.navigator.serviceWorker = undefined;
  } });

const { window } = dom;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(50);
const doc = window.document;
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

// token taken from the hash, stored, hash removed, used as bearer
if (window.localStorage.getItem("clara-token") !== "abc") fail("token not stored from hash");
if (window.location.hash) fail("hash not cleared");
if (!calls.every((c) => c.auth === "Bearer abc")) fail("bearer header missing on " + JSON.stringify(calls));

// snapshot rendered: user turn without the context prefix, tool card, markdown
const you = doc.querySelector(".you");
if (!you || you.textContent.includes("[context")) fail("user turn not rendered or context prefix shown");
if (!doc.querySelector(".tool .name") || doc.querySelector(".tool .name").textContent !== "read_file(hello.txt)") fail("tool card from history missing");
if (!doc.querySelector(".reply strong") || !doc.querySelector(".reply pre code")) fail("markdown not rendered");
if (doc.getElementById("title").textContent !== "earlier question") fail("title not set");
if (!window.__sources.length || !window.__sources[0].url.includes("/api/sessions/s1/events")) fail("event stream not opened");
if (window.__sources[0].url.includes("abc") || !window.__sources[0].url.includes("ticket=tkt-1")) fail("stream URL must carry a ticket, never the token");

// stream events
const es = window.__sources[window.__sources.length - 1];
const push = (ev) => es.onmessage({ data: JSON.stringify(ev) });
push({ seq: 1, kind: "user", text: "write a file" });
if (!doc.body.classList.contains("busy")) fail("busy state not set");
push({ seq: 2, kind: "reasoning", delta: "thinking…" });
push({ seq: 3, kind: "text", delta: "Sure, " });
push({ seq: 4, kind: "text", delta: "doing it." });
push({ seq: 5, kind: "tool_start", id: "t9", name: "write_file", summary: "write_file(a.txt)" });
push({ seq: 6, kind: "permission", id: "p1", tool: "write_file", summary: "write_file(a.txt)", warnings: ["target is outside the project folder"], preview: "content here" });
const sheet = doc.getElementById("sheet");
if (!sheet.classList.contains("show")) fail("approval sheet not shown");
if (!doc.getElementById("sheet-what").textContent.includes("write_file(a.txt)") || !doc.getElementById("sheet-what").textContent.includes("content here")) fail("sheet content wrong");
if (!doc.getElementById("sheet-flags").textContent.includes("outside the project")) fail("warning not shown");
const allow = [...doc.querySelectorAll("#sheet-btns button")].find((b) => b.textContent === "Allow once");
allow.click();
await sleep(10);
const perm = calls.find((c) => c.path === "/api/permissions/p1");
if (!perm || perm.body !== JSON.stringify({ answer: "allow" })) fail("allow not posted: " + JSON.stringify(perm));
if (sheet.classList.contains("show")) fail("sheet still shown after answering");
push({ seq: 7, kind: "tool_result", id: "t9", name: "write_file", text: "wrote a.txt", error: false, decision: "ok", seconds: 0.1 });
if (!doc.querySelector(".tool .state").textContent.startsWith("ok") && ![...doc.querySelectorAll(".tool .state")].some((s) => s.textContent.startsWith("ok"))) fail("tool result not shown");
push({ seq: 8, kind: "warning", text: "prompt is large" });
if (!doc.querySelector(".warn")) fail("warning not shown");
push({ seq: 9, kind: "turn_end", text: "Sure, doing it." });
push({ seq: 10, kind: "idle" });
if (doc.body.classList.contains("busy")) fail("busy state not cleared");
const last = [...doc.querySelectorAll(".reply")].pop();
if (!last || !last.textContent.includes("Sure, doing it.")) fail("streamed text not rendered");

// model output is escaped, not executed
push({ seq: 11, kind: "user", text: "x" });
push({ seq: 12, kind: "text", delta: "<img src=x onerror=\"window.__xss=1\"> <script>window.__xss=2</script> `<b onmouseover=x>` **<i>bold</i>** # <a href=\"javascript:alert(1)\">h</a>\n- <u>li</u>" });
await sleep(10);
if (window.__xss) fail("model output executed as HTML");
if (doc.querySelector(".reply img, .reply b, .reply i, .reply a, .reply u")) fail("model output injected an element");
const lastReply = [...doc.querySelectorAll(".reply")].pop();
if (!lastReply.querySelector("code") || lastReply.querySelector("code").textContent !== "<b onmouseover=x>") fail("inline code not shown as text: " + lastReply.innerHTML);

// sending a message posts to the API and clears the box
doc.getElementById("input").value = "hello there";
doc.getElementById("send").click();
await sleep(10);
const sent = calls.find((c) => c.path === "/api/sessions/s1/messages");
if (!sent || sent.body !== JSON.stringify({ text: "hello there" })) fail("message not posted");
console.log("app page: all checks passed (" + calls.length + " API calls)");
