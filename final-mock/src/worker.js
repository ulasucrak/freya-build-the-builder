// Data-driven mock of a bank's HTTP services for Cloudflare Workers.
// All service knowledge lives in data.json: for each route (method + path) an ordered list of rules. A rule
// matches when every field in `match` equals the request's field (JSON body, else query string); the first
// matching rule answers with its `status` and `body`. A route with no matching rule answers its `default`.
// Unknown paths answer 404. Every request is logged (wrangler tail) so test calls can be checked.
import DATA from "../data.json";

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8" } });

function fieldsOf(url, body) {
  const out = {};
  for (const [k, v] of url.searchParams) out[k] = v;
  if (body && typeof body === "object") Object.assign(out, body);
  return out;
}

function matches(rule, fields) {
  for (const [k, v] of Object.entries(rule.match || {})) {
    if (String(fields[k] ?? "").trim().toLowerCase() !== String(v).trim().toLowerCase()) return false;
  }
  return true;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    let body = null;
    if (request.method !== "GET" && request.method !== "HEAD") {
      try { body = await request.json(); } catch { body = null; }
    }
    const fields = fieldsOf(url, body);
    const route = (DATA.routes || []).find(
      (r) => r.path === url.pathname && (!r.method || r.method.toUpperCase() === request.method));
    console.log(JSON.stringify({ method: request.method, path: url.pathname, fields }));
    if (!route) return json(404, DATA.not_found || { error: "not found" });
    const rule = (route.rules || []).find((r) => matches(r, fields));
    const ans = rule || route.default || { status: 404, body: { error: "not found" } };
    return json(ans.status || 200, ans.body ?? {});
  },
};
