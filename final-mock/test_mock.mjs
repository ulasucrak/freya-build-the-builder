// Test of the mock against the bank's own test data:  node final-mock/test_mock.mjs
//
// For EVERY answer in test-environment/test-customers.json it builds the contract request, calls the worker's
// fetch() and asserts the same HTTP status and a deep-equal body (plus the exact JSON text). Then the error
// cases of the contract (unknown customer/card/week, empty fields, malformed JSON, wrong path/method) and the
// extras (template variables, /health, delaySeconds).
//
// test-customers.json is looked up in $TEST_CUSTOMERS_JSON, then next to the repo ("Build the Builder - Final Case").
// Set SKIP_SLOW=1 to skip the one real 12-second delay check.
//
// Live check (optional):  node final-mock/test_mock.mjs --url https://freya-statement-mock.ulasucrak.workers.dev
// Same requests, but sent over HTTP to the deployed Worker instead of calling worker.fetch() in-process: the pairs run
// 8 at a time, 20 s timeout per request, the real 12 s delaySeconds case included (SKIP_SLOW is ignored), and the
// summary line reads "live <url>: N/N request/response pairs identical".

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const argv = process.argv.slice(2);
const urlArg = argv.findIndex((a) => a === "--url" || a.startsWith("--url="));
const LIVE = urlArg < 0 ? null
  : (argv[urlArg].startsWith("--url=") ? argv[urlArg].slice(6) : argv[urlArg + 1] || "").trim().replace(/\/+$/, "");
if (LIVE !== null && !/^https?:\/\/[^/\s]+/.test(LIVE)) {
  console.error("usage: node final-mock/test_mock.mjs [--url https://<worker>.workers.dev]");
  process.exit(2);
}
const CONCURRENCY = 8; // live mode: requests in flight
const TIMEOUT_MS = 20000; // live mode: per request
const worker = LIVE ? null : (await import("./worker.js")).default;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CASE_REL = path.join("Build the Builder - Final Case", "test-environment", "test-customers.json");
const SRC = [process.env.TEST_CUSTOMERS_JSON, path.join(HERE, "..", CASE_REL), path.join(HERE, "..", "..", "..", CASE_REL)]
  .find((p) => p && fs.existsSync(p));
if (!SRC) {
  console.error("test-customers.json not found; set TEST_CUSTOMERS_JSON");
  process.exit(2);
}
const RAW = fs.readFileSync(SRC, "utf8");
const DATA = JSON.parse(RAW);
const FAST = { DELAY_SCALE: "0" }; // env for the bulk run: delaySeconds skipped (checked separately below)
const CALL = { ani: "5550000000", lang: "tr" };

let passed = 0;
let failed = 0;
const failures = [];

// One request -> { status, headers, text }. Local: the worker's fetch() in-process. Live (--url): real HTTP to the
// deployed Worker (env is ignored, so the real delaySeconds wait happens), 20 s timeout covering the body read,
// one retry for a network error / timeout (a wrong answer is never retried).
async function send(pathname, init, env = FAST) {
  if (!LIVE) {
    const res = await worker.fetch(new Request("https://mock.test" + pathname, init), env);
    return { status: res.status, headers: res.headers, text: await res.text() };
  }
  for (let attempt = 1; ; attempt++) {
    try {
      const res = await fetch(LIVE + pathname, { ...init, signal: AbortSignal.timeout(TIMEOUT_MS) });
      return { status: res.status, headers: res.headers, text: await res.text() };
    } catch (e) {
      if (attempt >= 2) throw new Error(`${init.method} ${pathname}: ${e.cause?.code || e.name}: ${e.message}`);
    }
  }
}

async function call(pathname, body, { method = "POST", env = FAST, raw = false } = {}) {
  const init = { method, headers: { "Content-Type": "application/json", Authorization: "Bearer test" } };
  if (method !== "GET" && method !== "HEAD" && body !== undefined) init.body = raw ? body : JSON.stringify(body);
  const { status, headers, text } = await send(pathname, init, env);
  let json = null;
  try { json = JSON.parse(text); } catch { /* not JSON */ }
  return { status, text, json, headers };
}

async function check(name, fn) {
  try {
    await fn();
    passed++;
    return true;
  } catch (e) {
    failed++;
    failures.push(`${name}: ${e.message.split("\n").slice(0, 6).join(" | ")}`);
    return false;
  }
}

// Runs fn(item) for all items, `limit` at a time.
async function pool(items, limit, fn) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) await fn(items[next++]);
  }));
}

const T_START = Date.now();
if (LIVE) {
  console.log(`LIVE mode: ${LIVE}  (${CONCURRENCY} at a time, ${TIMEOUT_MS / 1000} s timeout per request, real delaySeconds wait)`);
  try {
    const h = await send("/health", { method: "GET" });
    if (h.status !== 200) throw new Error(`GET /health -> HTTP ${h.status}`);
  } catch (e) {
    console.error(`live URL not reachable: ${e.message}`);
    process.exit(2);
  }
}

function expectError(res, status, code, type, messagePart) {
  assert.equal(res.status, status, `status ${res.status} != ${status}`);
  assert.ok(res.json && res.json.result, "no result object");
  assert.equal(res.json.data, undefined, "error answer must have no data");
  const r = res.json.result;
  assert.equal(r.returnCode, code);
  assert.equal(r.code, code);
  assert.equal(r.type, type);
  assert.equal(r.messageText, r.message);
  assert.match(r.guid, /^[0-9a-f]{32}$/);
  assert.equal(r.details.guid, r.guid);
  if (messagePart) assert.ok(r.message.includes(messagePart), `message "${r.message}" lacks "${messagePart}"`);
}

// ------------------------------------------------------------------------------------------------ 1. every pair

const customers = DATA.customers;
const firstCard = {}; // customerNo -> a card of that customer (for statement.pdf.send requests)
const currentDay = {}; // cardNumber -> statementDay from statement.week.get (for decide/change requests)
for (const cu of customers) {
  const s = cu.services;
  for (const card of Object.keys(s["cards.balance"] || {})) firstCard[cu.customerNo] ??= card;
  for (const [card, e] of Object.entries(s["statement.week.get"] || {})) {
    currentDay[card] = e.response.data ? e.response.data.statementDay : 1;
  }
}

const pairs = []; // [label, path, body, entry]
for (const cu of customers) {
  const cno = cu.customerNo;
  const s = cu.services;
  for (const [svc, node] of Object.entries(s)) {
    const p = "/voice/v1/" + svc;
    if (svc === "cards.list") {
      pairs.push([`${svc} ${cno}`, p, { data: { customerNo: cno, aiFlow: "CardOps" }, call: CALL }, node]);
      pairs.push([`${svc} ${cno} (number)`, p, { data: { customerNo: Number(cno), aiFlow: "CardOps" }, call: CALL }, node, true]);
    } else if (svc === "contact.email.get") {
      for (const t of ["E", "P", "B"]) {
        pairs.push([`${svc} ${cno} ${t}`, p, { data: { customerNo: Number(cno), intAddressInfoType: t }, call: CALL }, node, t !== "E"]);
      }
      pairs.push([`${svc} ${cno} (string)`, p, { data: { customerNo: cno, intAddressInfoType: "E" }, call: CALL }, node, true]);
    } else if (svc === "statement.pdf.send") {
      const card = firstCard[cno] || "540000******0000";
      const bal = (s["cards.balance"] || {})[card];
      const date = bal && bal.response.data ? bal.response.data.statementDate : "05.10.2026";
      const email = s["contact.email.get"].response.data ? s["contact.email.get"].response.data.ibanEmail : "";
      const data = { customerNo: cno, cardNumber: card, currencyType: "TL", statementDate: date, emailAddress: email || "test@freyabank.example" };
      pairs.push([`${svc} ${cno}`, p, { data, call: CALL }, node]);
      pairs.push([`${svc} ${cno} (number, USD)`, p, { data: { ...data, customerNo: Number(cno), currencyType: "USD" }, call: CALL }, node, true]);
    } else if (svc === "cards.balance" || svc === "statement.week.get") {
      for (const [card, e] of Object.entries(node)) {
        pairs.push([`${svc} ${card}`, p, { data: { cardNumber: card }, call: CALL }, e]);
      }
    } else if (svc === "statement.week.decide" || svc === "statement.week.change") {
      for (const [card, weeks] of Object.entries(node)) {
        for (const [w, e] of Object.entries(weeks)) {
          const data = { cardNumber: card, newStatementWeek: w, statementDay: currentDay[card] ?? 1 };
          pairs.push([`${svc} ${card} w${w}`, p, { data, call: CALL }, e]);
          pairs.push([`${svc} ${card} w${w} (number)`, p, { data: { ...data, newStatementWeek: Number(w) }, call: CALL }, e, true]);
        }
      }
    } else {
      throw new Error("unknown service in test data: " + svc);
    }
  }
}

let filePairs = 0;
let filePairsOk = 0;
const liveSlow = []; // live mode: the delaySeconds pair(s) with the seconds they really took
await pool(pairs, LIVE ? CONCURRENCY : 1, async ([label, p, body, entry, variant]) => {
  if (!variant) filePairs++;
  const t0 = Date.now();
  const ok = await check(label, async () => {
    const res = await call(p, body);
    assert.equal(res.status, entry.http, `status ${res.status} != ${entry.http}`);
    assert.deepStrictEqual(res.json, entry.response);
    assert.ok(!("delaySeconds" in res.json), "delaySeconds leaked into the body");
    assert.match(res.headers.get("content-type") || "", /application\/json/);
    if (LIVE && entry.delaySeconds) {
      const waited = (Date.now() - t0) / 1000;
      liveSlow.push(`${label} answered after ${waited.toFixed(1)}s (delaySeconds ${entry.delaySeconds})`);
      assert.ok(waited >= entry.delaySeconds - 0.05, `answered after ${waited}s, expected >= ${entry.delaySeconds}s`);
    }
  });
  if (!variant && ok) filePairsOk++;
});

// The answer text keeps the file's number literals (e.g. "transferAmount":0.0), not just equal values.
await check("number literals preserved", async () => {
  const res = await call("/voice/v1/cards.balance", { data: { cardNumber: "540000******1234" } });
  assert.ok(res.text.includes('"transferAmount":0.0'), "0.0 literal lost");
  assert.ok(res.text.includes('"statementDebt":5000.13'));
  assert.ok(RAW.includes('"transferAmount": 0.0'));
});

// ------------------------------------------------------------------------------------------------ 2. not found

await check("cards.list unknown customer -> 404 FB_IVR_CHANNEL_102", async () => {
  expectError(await call("/voice/v1/cards.list", { data: { customerNo: "12345678", aiFlow: "CardOps" } }), 404,
    "FB_IVR_CHANNEL_102", "WARNING", "Müşteriye ait geçerli kart bilgisi bulunamadı.");
});
await check("cards.balance unknown card -> 404 FB_IVR_CHANNEL_120", async () => {
  expectError(await call("/voice/v1/cards.balance", { data: { cardNumber: "540000******9999" } }), 404, "FB_IVR_CHANNEL_120", "WARNING");
});
await check("cards.balance other mask (5400********1234) is not a card-list number -> 404", async () => {
  expectError(await call("/voice/v1/cards.balance", { data: { cardNumber: "5400********1234" } }), 404, "FB_IVR_CHANNEL_120", "WARNING");
});
await check("statement.week.get unknown card -> 404 FB_IVR_CHANNEL_121", async () => {
  expectError(await call("/voice/v1/statement.week.get", { data: { cardNumber: "1234" } }), 404, "FB_IVR_CHANNEL_121", "WARNING");
});
for (const svc of ["statement.week.decide", "statement.week.change"]) {
  await check(`${svc} unknown card -> 400`, async () => {
    expectError(await call("/voice/v1/" + svc, { data: { cardNumber: "540000******9999", newStatementWeek: 2, statementDay: 5 } }),
      400, "INVALID_ARGUMENTS_ERROR", "ERROR");
  });
  await check(`${svc} current week (no entry) -> 400`, async () => {
    expectError(await call("/voice/v1/" + svc, { data: { cardNumber: "540000******1234", newStatementWeek: "1", statementDay: 5 } }),
      400, "INVALID_ARGUMENTS_ERROR", "ERROR", "newStatementWeek");
  });
  await check(`${svc} week 5 -> 400`, async () => {
    expectError(await call("/voice/v1/" + svc, { data: { cardNumber: "540000******1234", newStatementWeek: 5, statementDay: 5 } }),
      400, "INVALID_ARGUMENTS_ERROR", "ERROR");
  });
  await check(`${svc} missing statementDay -> 400`, async () => {
    expectError(await call("/voice/v1/" + svc, { data: { cardNumber: "540000******1234", newStatementWeek: 2 } }),
      400, "INVALID_ARGUMENTS_ERROR", "ERROR", "statementDay cannot be empty");
  });
  await check(`${svc} missing newStatementWeek -> 400`, async () => {
    expectError(await call("/voice/v1/" + svc, { data: { cardNumber: "540000******1234", statementDay: 5 } }),
      400, "INVALID_ARGUMENTS_ERROR", "ERROR", "newStatementWeek cannot be empty");
  });
}
await check("contact.email.get unknown customer -> 400", async () => {
  expectError(await call("/voice/v1/contact.email.get", { data: { customerNo: 11111111, intAddressInfoType: "E" } }),
    400, "INVALID_ARGUMENTS_ERROR", "ERROR");
});
await check("contact.email.get empty intAddressInfoType -> 400", async () => {
  expectError(await call("/voice/v1/contact.email.get", { data: { customerNo: 90000001, intAddressInfoType: "" } }),
    400, "INVALID_ARGUMENTS_ERROR", "ERROR", "intAddressInfoType cannot be empty");
});
const pdfData = { customerNo: "94907600", cardNumber: "540000******7211", currencyType: "TL", statementDate: "05.10.2026", emailAddress: "x@freyabank.example" };
await check("statement.pdf.send customer without entry -> 404 FB_IVR_CHANNEL_107", async () => {
  expectError(await call("/voice/v1/statement.pdf.send", { data: pdfData }), 404, "FB_IVR_CHANNEL_107", "WARNING", "Pdf dokümanı bulunamadı");
});
await check("statement.pdf.send unknown customer -> 404 FB_IVR_CHANNEL_107", async () => {
  expectError(await call("/voice/v1/statement.pdf.send", { data: { ...pdfData, customerNo: "1" } }), 404, "FB_IVR_CHANNEL_107", "WARNING");
});
for (const f of ["customerNo", "cardNumber", "currencyType", "statementDate", "emailAddress"]) {
  await check(`statement.pdf.send empty ${f} -> 400`, async () => {
    const res = await call("/voice/v1/statement.pdf.send", { data: { ...pdfData, customerNo: "90000001", [f]: "  " } });
    expectError(res, 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "cannot be empty");
  });
}

// ------------------------------------------------------------------------------------------------ 3. invalid requests

await check("cards.balance empty card number -> 400 'card number cannot be empty'", async () => {
  expectError(await call("/voice/v1/cards.balance", { data: { cardNumber: "" } }), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "card number cannot be empty");
});
await check("cards.list missing customerNo -> 400", async () => {
  expectError(await call("/voice/v1/cards.list", { data: { aiFlow: "CardOps" } }), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "customer number cannot be empty");
});
await check("cards.list without data envelope -> 400", async () => {
  expectError(await call("/voice/v1/cards.list", { customerNo: "90000001" }), 400, "INVALID_ARGUMENTS_ERROR", "ERROR");
});
await check("malformed JSON -> 400", async () => {
  expectError(await call("/voice/v1/cards.list", "{\"data\": {", { raw: true }), 400, "INVALID_ARGUMENTS_ERROR", "ERROR", "JSON");
});
await check("empty body -> 400", async () => {
  expectError(await call("/voice/v1/statement.week.get", "", { raw: true }), 400, "INVALID_ARGUMENTS_ERROR", "ERROR");
});
await check("JSON array body -> 400", async () => {
  expectError(await call("/voice/v1/cards.list", [1, 2]), 400, "INVALID_ARGUMENTS_ERROR", "ERROR");
});
await check("unknown path -> 404", async () => {
  expectError(await call("/voice/v1/cards.delete", { data: {} }), 404, "NOT_FOUND", "ERROR");
});
await check("path without /voice/v1 -> 404", async () => {
  expectError(await call("/cards.list", { data: { customerNo: "90000001" } }), 404, "NOT_FOUND", "ERROR");
});
await check("GET on a service -> 405", async () => {
  const res = await call("/voice/v1/cards.list", undefined, { method: "GET" });
  expectError(res, 405, "METHOD_NOT_ALLOWED", "ERROR");
  assert.equal(res.headers.get("allow"), "POST");
});

// ------------------------------------------------------------------------------------------------ 4. extras

const sample = (svc, ...keys) => {
  const cu = customers.find((c) => c.customerNo === "90000001");
  return keys.reduce((n, k) => n[k], cu.services[svc]);
};
await check("template customerNo -> sample cards.list", async () => {
  const res = await call("/voice/v1/cards.list", { data: { customerNo: "{{customerNumber}}", aiFlow: "CardOps" } });
  assert.equal(res.status, 200);
  assert.deepStrictEqual(res.json, sample("cards.list").response);
});
await check("url-encoded template -> sample contact.email.get", async () => {
  const res = await call("/voice/v1/contact.email.get", { data: { customerNo: "%7B%7BcustomerNumber%7D%7D", intAddressInfoType: "{{type}}" } });
  assert.equal(res.status, 200);
  assert.deepStrictEqual(res.json, sample("contact.email.get").response);
});
await check("template cardNumber -> sample cards.balance", async () => {
  const res = await call("/voice/v1/cards.balance", { data: { cardNumber: "{{cardNumber}}" } });
  assert.equal(res.status, 200);
  assert.deepStrictEqual(res.json, sample("cards.balance", "540000******1234").response);
});
await check("template card + week -> sample statement.week.decide", async () => {
  const res = await call("/voice/v1/statement.week.decide", { data: { cardNumber: "{{cardNumber}}", newStatementWeek: "{{week}}", statementDay: "{{day}}" } });
  assert.equal(res.status, 200);
  assert.deepStrictEqual(res.json, sample("statement.week.decide", "540000******1234", "2").response);
});
await check("template customer -> sample statement.pdf.send", async () => {
  const res = await call("/voice/v1/statement.pdf.send", { data: { customerNo: "{{customerNumber}}", cardNumber: "{{cardNumber}}", currencyType: "{{currency}}", statementDate: "{{date}}", emailAddress: "{{email}}" } });
  assert.equal(res.status, 200);
  assert.deepStrictEqual(res.json, sample("statement.pdf.send").response);
});
await check("whitespace around customerNo / cardNumber / week is trimmed", async () => {
  let res = await call("/voice/v1/cards.list", { data: { customerNo: " 90000001 " } });
  assert.deepStrictEqual(res.json, sample("cards.list").response);
  res = await call("/voice/v1/statement.week.change", { data: { cardNumber: " 540000******1234 ", newStatementWeek: " 3 ", statementDay: 5 } });
  assert.deepStrictEqual(res.json, sample("statement.week.change", "540000******1234", "3").response);
});
await check("change is stateless: week.get unchanged after a change", async () => {
  await call("/voice/v1/statement.week.change", { data: { cardNumber: "540000******1234", newStatementWeek: 2, statementDay: 5 } });
  const res = await call("/voice/v1/statement.week.get", { data: { cardNumber: "540000******1234" } });
  assert.deepStrictEqual(res.json, sample("statement.week.get", "540000******1234").response);
});
await check("GET /health -> 200 {ok:true}", async () => {
  const res = await call("/health", undefined, { method: "GET" });
  assert.equal(res.status, 200);
  assert.deepStrictEqual(res.json, { ok: true });
});
await check("OPTIONS preflight -> 204 + CORS", async () => {
  const res = await send("/voice/v1/cards.list", { method: "OPTIONS" });
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("access-control-allow-origin"), "*");
});
await check("POST /oauth2/token -> test token", async () => {
  const res = await send("/oauth2/token", { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: "grant_type=client_credentials&scope=voice.statement" });
  assert.equal(res.status, 200);
  const j = JSON.parse(res.text);
  assert.equal(j.token_type, "Bearer");
  assert.equal(j.expires_in, 1800);
});

// delaySeconds: the one slow answer waits for real (default env, as deployed)
const slow = [];
for (const cu of customers) {
  for (const [svc, node] of Object.entries(cu.services)) {
    const walk = (n, keys) => {
      if (n && typeof n === "object" && "http" in n && "response" in n) { if (n.delaySeconds) slow.push([svc, keys, n]); return; }
      for (const [k, v] of Object.entries(n || {})) walk(v, keys.concat(k));
    };
    walk(node, []);
  }
}
if (LIVE) {
  // the pairs above already waited for the real delay (timed there), nothing more to wait for
  for (const line of liveSlow) console.log("delay check: " + line);
  if (liveSlow.length !== slow.length) {
    failed++;
    failures.push(`delay check: ${liveSlow.length} of ${slow.length} slow entries answered with the expected wait`);
  }
} else if (process.env.SKIP_SLOW) {
  console.log(`SKIPPED real-delay check (${slow.length} slow entr${slow.length === 1 ? "y" : "ies"})`);
} else {
  for (const [svc, keys, e] of slow) {
    await check(`${svc} ${keys.join(" ")} waits ${e.delaySeconds}s`, async () => {
      assert.equal(svc, "cards.balance");
      const t0 = Date.now();
      const res = await call("/voice/v1/" + svc, { data: { cardNumber: keys[0] } }, { env: {} });
      const waited = (Date.now() - t0) / 1000;
      assert.ok(waited >= e.delaySeconds - 0.05, `answered after ${waited}s`);
      assert.equal(res.status, e.http);
      assert.deepStrictEqual(res.json, e.response);
      console.log(`delay check: ${svc} ${keys.join(" ")} answered after ${waited.toFixed(1)}s`);
    });
  }
}

// ------------------------------------------------------------------------------------------------ summary

console.log(`${LIVE ? `live ${LIVE}` : "test-customers.json"}: ${filePairsOk}/${filePairs} request/response pairs identical (status + body)`);
console.log(`all checks: ${passed} passed, ${failed} failed (incl. string/number variants and error cases)` +
  (LIVE ? `, ${((Date.now() - T_START) / 1000).toFixed(1)} s` : ""));
for (const f of failures.slice(0, 30)) console.log("FAIL " + f);
process.exit(failed ? 1 : 0);
