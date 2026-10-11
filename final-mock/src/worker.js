// Data-driven mock of a bank's HTTP services for Cloudflare Workers.
// data.json: routes (method + path) with ordered rules. A rule matches when every field in `match` equals the
// request's field (fields of the JSON body's `data` envelope, then the top level, then the query string; "3" == 3).
// The first matching rule answers with its `status` and `body`, after `delaySeconds` if set. A route whose
// `required` field is missing or empty answers its `invalid` answer; a route with no matching rule its `default`.
// Unknown paths answer 404. Every request is logged (wrangler tail).
import DATA from "../data.json";

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8" } });

function fieldsOf(url, body) {
  const out = {};
  for (const [k, v] of url.searchParams) out[k] = v;
  if (body && typeof body === "object") {
    Object.assign(out, body);
    if (body.data && typeof body.data === "object") Object.assign(out, body.data);
  }
  return out;
}

const norm = (v) => String(v ?? "").trim().toLowerCase();
const matches = (rule, fields) => Object.entries(rule.match || {}).every(([k, v]) => norm(fields[k]) === norm(v));

export default {
  async fetch(request) {
    const url = new URL(request.url);
    let body = null;
    if (request.method !== "GET" && request.method !== "HEAD") {
      try { body = await request.json(); } catch { body = null; }
    }
    const fields = fieldsOf(url, body);
    const route = (DATA.routes || []).find(
      (r) => (r.paths || [r.path]).includes(url.pathname) && (!r.method || r.method.toUpperCase() === request.method));
    console.log(JSON.stringify({ method: request.method, path: url.pathname, fields }));
    if (!route) return json(404, DATA.not_found || { error: "not found" });
    const missing = (route.required || []).find((k) => norm(fields[k]) === "");
    if (missing && route.invalid) {
      const b = JSON.parse(JSON.stringify(route.invalid.body).replaceAll("{field}", missing));
      return json(route.invalid.status || 400, b);
    }
    const rule = (route.rules || []).find((r) => matches(r, fields));
    const ans = rule || route.default || { status: 404, body: { error: "not found" } };
    if (ans.delaySeconds) await new Promise((res) => setTimeout(res, ans.delaySeconds * 1000));
    return json(ans.status || 200, ans.body ?? {});
  },
};
