"""Generic service-agent workflow: a policy without the collection roles (verify / account / promise / close) gets
setup -> one 'assistant' node with every openapi operation as an api_call tool -> end_conversation -> end_call nodes.

The openapi below is contract-shaped (every body is an envelope {"data": {...}, "call": {...}}); its names are made up."""
import copy
import json
import pathlib
import re
import sys
import unittest

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import generate as G  # noqa: E402
from builder import testmode  # noqa: E402


def envelope(data_props, data_required, ref_call=True):
    return {"type": "object", "required": ["data"], "properties": {
        "data": {"type": "object", "properties": data_props, "required": data_required},
        "call": {"$ref": "#/components/schemas/CallMeta"} if ref_call else {"type": "object", "properties": {}}}}


def op(op_id, summary, schema):
    return {"post": {"operationId": op_id, "summary": summary, "description": summary + " Errors: HTTP 404 means nothing found.",
                     "requestBody": {"content": {"application/json": {"schema": schema}}},
                     "responses": {"200": {"description": "ok"}, "404": {"description": "not found"}}}}


OPENAPI = {
    "openapi": "3.0.3", "info": {"title": "contract", "version": "1"},
    "servers": [{"url": "https://svc.example/api/v2"}],
    "components": {"schemas": {"CallMeta": {"type": "object", "properties": {
        "ani": {"type": "string", "description": "Caller's phone number."},
        "lang": {"type": "string", "default": "xx"}}}}},
    "paths": {
        "/orders.list": op("orders.list", "List the caller's orders.", envelope({
            "clientNo": {"type": "string", "description": "Client number."},
            "flow": {"type": "string", "enum": ["Orders"], "default": "Orders"}}, ["clientNo"])),
        "/orders.detail": op("orders.detail", "Get one order's details.", envelope({
            "orderRef": {"type": "string", "description": "Order reference exactly as orders.list returned it."}}, ["orderRef"])),
        "/orders.slot.change": op("orders.slot.change", "Change the delivery slot.", envelope({
            "orderRef": {"type": "string"},
            "newSlot": {"type": "integer", "description": "Slot 1 to 4."},
            "mode": {"type": "string", "enum": ["A"]}}, ["orderRef", "newSlot"])),
        "/receipt.send": op("receipt.send", "Send the receipt by e-mail.", envelope({
            "clientNumber": {"type": "string"},
            "email": {"type": "string"}}, ["clientNumber", "email"])),
    },
}

POLICY = {
    "language": "Turkish",
    "variables": [{"name": "clientNumber", "meaning": "the client number from the phone menu; never asked",
                   "example": "90000001"},
                  {"name": "now", "meaning": "date and time of the call", "example": "2026-10-12T10:30:00+03:00"}],
    "clock": {"now_variable": "now"},
    "rules": [{"id": "1", "text": "Start from orders.list; a single order is taken without a question."},
              {"id": "2", "text": "Never read more than one order's details at once."},
              {"id": "3", "text": "~~Old rule that was struck.~~"}],
    "phrases": {"pick_order": "Hangi siparişinizi öğrenmek istersiniz?",
                "to_menu": "Sizi ana menüye yönlendiriyorum."},
    "triggers": [{"name": "agent_request", "detect": "Caller asks for a person", "action": "transfer",
                  "say": "Sizi müşteri temsilcimize aktarıyorum."}],
    "tools": {},
}


def body_props(t):
    return t["function"]["parameters"]["properties"]["body"]["properties"]


def run_hook(src, name, value, vs=None):
    """A hook the way a strict sandbox runs it (separate globals and locals)."""
    g = {"vars": vs if vs is not None else {}, name: value}
    exec(compile(src, "snippet", "exec"), g, {})
    return value


class GenericWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = G.generate_workflow(copy.deepcopy(POLICY), None, copy.deepcopy(OPENAPI))
        cls.tools = {t["function"]["name"]: t for t in cls.wf["nodes"]["assistant"]["tools"]}

    def test_shape_and_lint(self):
        self.assertEqual(G.lint_workflow(self.wf), [])
        self.assertEqual(self.wf["initial_node"], "setup")
        self.assertEqual(set(self.wf["nodes"]), {"setup", "assistant"} | {"end_" + r for r in G.END_REASONS})
        for r in G.END_REASONS:
            self.assertEqual(self.wf["nodes"]["end_" + r]["terminal"]["function"]["name"], r)
        self.assertEqual(json.loads(json.dumps(self.wf)), self.wf)
        self.assertEqual(self.wf["test_variables"], {"clientNumber": "90000001", "now": "2026-10-12T10:30:00+03:00"})

    def test_every_operation_is_a_tool_with_a_safe_name(self):
        self.assertEqual(set(self.tools), {"orders_list", "orders_detail", "orders_slot_change", "receipt_send", "end_conversation"})
        for name, t in self.tools.items():
            self.assertRegex(name, r"^[A-Za-z0-9_]+$")
        url = self.tools["orders_list"]["function"]["parameters"]["properties"]["url"]["default"]
        self.assertEqual(url, "https://svc.example/api/v2/orders.list")

    def test_body_bakes_variables_and_constants(self):
        b = body_props(self.tools["orders_list"])
        self.assertEqual(b["clientNo"]["default"], "{{clientNumber}}")     # clientNo ~ clientNumber
        self.assertEqual(b["flow"]["default"], "Orders")                     # schema default
        self.assertEqual(b["lang"]["default"], "xx")                         # $ref'd call object, default
        self.assertNotIn("ani", b)                                           # optional metadata nobody can fill
        self.assertEqual(self.tools["orders_list"]["function"]["parameters"]["properties"]["body"]["required"], [])
        c = body_props(self.tools["orders_slot_change"])
        self.assertEqual(c["mode"]["default"], "A")                          # a single enum value
        self.assertEqual(c["newSlot"], {"type": "integer", "default": "{{newSlot}}", "description": "Slot 1 to 4."})
        self.assertEqual(set(self.tools["orders_slot_change"]["function"]["parameters"]["properties"]["body"]["required"]),
                         {"orderRef", "newSlot"})
        self.assertEqual(body_props(self.tools["receipt_send"])["clientNumber"]["default"], "{{clientNumber}}")
        visible = G.visible_tool_schema(self.tools["orders_detail"], {"clientNumber", "now"})
        self.assertEqual(set(visible["parameters"]), {"orderRef"})

    def test_timeouts_and_retries(self):
        for name, t in self.tools.items():
            if name == "end_conversation":
                continue
            self.assertEqual(t["config"]["timeoutMs"], 15000)
        self.assertEqual(self.tools["orders_list"]["config"]["maxRetries"], 1)
        self.assertEqual(self.tools["orders_detail"]["config"]["maxRetries"], 1)
        self.assertEqual(self.tools["orders_slot_change"]["config"]["maxRetries"], 0)
        self.assertEqual(self.tools["receipt_send"]["config"]["maxRetries"], 0)

    def test_pre_hook_rebuilds_the_envelope(self):
        pre = self.tools["orders_list"]["pre_process_code"]
        cfg = {"url": "u", "method": "POST", "body": {"clientNo": "90000001", "flow": "Orders", "lang": "xx"}}
        status, out = testmode.run_code_locally("config = params\n" + pre + "\nreturn config", cfg, {})
        self.assertEqual(status, "success")
        self.assertEqual(out["body"], {"data": {"clientNo": "90000001", "flow": "Orders"}, "call": {"lang": "xx"}})
        # strict sandbox; unresolved placeholders are dropped; numbers coerced to the schema type
        pre = self.tools["orders_slot_change"]["pre_process_code"]
        cfg = run_hook(pre, "config", {"body": json.dumps({"orderRef": 123, "newSlot": "3", "mode": "A", "lang": "{{lang}}"})})
        self.assertEqual(cfg["body"], {"data": {"orderRef": "123", "newSlot": 3, "mode": "A"}})
        self.assertNotIn("{{", pre + self.tools["orders_list"]["post_process_code"])

    def test_post_hook_unwraps_and_explains_errors(self):
        post = self.tools["orders_list"]["post_process_code"]
        ok = {"status": "success", "status_code": 200, "header": {"x": "1"},
              "data": {"data": {"orders": [{"orderRef": "A1"}]}, "result": {"returnCode": "1", "messageText": "fine"}}}
        run_hook(post, "result", ok)
        self.assertNotIn("header", ok)
        self.assertEqual(ok["data"]["orders"], [{"orderRef": "A1"}])
        self.assertTrue(ok["data"]["ok"])
        err = {"status": "error", "status_code": 404,
               "data": {"result": {"returnCode": "E_102", "type": "WARNING", "messageText": "secret text"}}}
        run_hook(post, "result", err)
        self.assertFalse(err["data"]["ok"])
        self.assertIn("E_102", err["data"]["what_to_do_next"])
        self.assertIn("404", err["data"]["what_to_do_next"])
        self.assertNotIn("secret text", json.dumps(err))

    def test_end_conversation_routes_on_the_reason(self):
        t = self.tools["end_conversation"]
        self.assertEqual(t["function"]["handler"], "__function__:code_exec")
        self.assertEqual(t["function"]["parameters"]["properties"]["reason"]["enum"], list(G.END_REASONS))
        code = t["function"]["parameters"]["properties"]["code"]["default"]
        vs = {}
        status, out = testmode.run_code_locally(code, {"reason": "main_menu"}, vs)
        self.assertEqual((status, out["ok"], vs.get("w_end_reason")), ("success", True, "main_menu"))
        vs = {}
        status, out = testmode.run_code_locally(code, {"reason": "whatever"}, vs)
        self.assertFalse(out["ok"])
        self.assertNotIn("w_end_reason", vs)
        tr = {x["target_node"]: x["condition"]["conditions"] for x in self.wf["nodes"]["assistant"]["transitions"]}
        self.assertEqual(tr["end_main_menu"], [{"variable": "w_end_reason", "operator": "equals", "operand": "main_menu"},
                                               {"variable": "end_conversation_args.reason", "operator": "equals", "operand": "main_menu"}])

    def test_setup_uses_the_call_now(self):
        code = self.wf["nodes"]["setup"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
        vs = {"now": "2026-10-12T10:30:00+03:00"}
        run_hook(code, "params", {}, vs)
        self.assertEqual((vs["w_today"], vs["w_now_time"], vs["w_weekday"], vs["w_clock_ok"]), ("2026-10-12", "10:30", "Monday", "yes"))
        self.assertIn("12 October 2026", vs["w_now_text"])
        self.assertEqual(vs["w_lang"], "Turkish")
        self.assertEqual(vs["clientNumber"], "")              # an unset call variable never shows as a placeholder
        vs = {"now": "", "clientNumber": "90000001"}
        run_hook(code, "params", {}, vs)
        self.assertEqual(vs["w_clock_ok"], "no")              # no readable now: only then the sandbox clock
        self.assertEqual(vs["clientNumber"], "90000001")

    def test_system_prompt(self):
        sp = self.wf["system_prompt"]
        for s in ("Speak Turkish", "{{clientNumber}}", "{{now}}", "{{w_now_text}}", "never ask the caller",
                  "Start from orders.list", "Never read more than one order", "Hangi siparişinizi öğrenmek istersiniz?",
                  "Sizi müşteri temsilcimize aktarıyorum.", "orders_slot_change: Change the delivery slot.", "end_conversation"):
            self.assertIn(s, sp)
        self.assertNotIn("Old rule that was struck", sp)
        self.assertLess(G.prompt_sizes(self.wf, ["clientNumber", "now"])["assistant"], 10000)
        oe = self.wf["nodes"]["assistant"]["on_enter"]
        self.assertEqual(oe["type"], "llm")
        self.assertIn("orders_list", oe["prompt"])            # needs nothing from the caller: can run at the start
        self.assertNotIn("receipt_send", oe["prompt"])


class GenericLanguageAndGreetingTest(unittest.TestCase):
    def test_language_detected_from_phrases(self):
        p = copy.deepcopy(POLICY)
        del p["language"]
        wf = G.generate_workflow(p, None, OPENAPI)
        self.assertIn("Speak Turkish", wf["system_prompt"])
        p["phrases"] = {"bye": "Thank you for calling, we are glad to help you."}
        p["triggers"] = []
        self.assertIn("Speak English", G.generate_workflow(p, None, OPENAPI)["system_prompt"])
        self.assertEqual(G.generic_language({"language": "tr-TR"}, {}), "Turkish")

    def test_static_greeting_only_in_the_call_language(self):
        p = copy.deepcopy(POLICY)
        p["opening"] = {"line": "Merhaba, size nasıl yardımcı olabilirim?"}
        wf = G.generate_workflow(p, None, OPENAPI)
        self.assertEqual(wf["nodes"]["assistant"]["on_enter"], {"type": "tts", "text": "Merhaba, size nasıl yardımcı olabilirim?"})
        p["opening"] = {"line": "Hello, how can I help you today?"}
        self.assertEqual(G.generate_workflow(p, None, OPENAPI)["nodes"]["assistant"]["on_enter"]["type"], "llm")

    def test_name_key(self):
        self.assertEqual(G.name_key("customerNo"), G.name_key("customerNumber"))
        self.assertEqual(G.name_key("customer_id"), G.name_key("CustomerNumber"))
        self.assertNotEqual(G.name_key("cardNumber"), G.name_key("customerNumber"))


class GenericFallbacksTest(unittest.TestCase):
    def test_policy_tools_without_openapi(self):
        p = copy.deepcopy(POLICY)
        p["tools"] = {"orders.list": {"url": "https://svc.example/api/v2/orders.list", "method": "POST", "fields": {
            "data": {"type": "object", "required": True, "properties": {
                "clientNo": {"type": "string", "required": True}, "flow": {"type": "string", "default": "Orders"}}}}},
            "no_url_tool": {"fields": {}}}
        wf = G.generate_workflow(p)
        self.assertEqual(G.lint_workflow(wf), [])
        tools = {t["function"]["name"]: t for t in wf["nodes"]["assistant"]["tools"]}
        self.assertEqual(set(tools), {"orders_list", "end_conversation"})
        cfg = run_hook(tools["orders_list"]["pre_process_code"], "config", {"body": {"clientNo": "7", "flow": "Orders"}})
        self.assertEqual(cfg["body"], {"data": {"clientNo": "7", "flow": "Orders"}})

    def test_list_fields_and_duplicate_leaf_names(self):
        oa = copy.deepcopy(OPENAPI)
        oa["paths"] = {"/x": op("x.set", "Set tags.", {"type": "object", "required": ["data", "meta"], "properties": {
            "data": {"type": "object", "required": ["tags", "lang"], "properties": {
                "tags": {"type": "array", "items": {"type": "string"}}, "lang": {"type": "string"}}},
            "meta": {"type": "object", "required": ["lang"], "properties": {"lang": {"type": "string"}}}}})}
        wf = G.generate_workflow(copy.deepcopy(POLICY), None, oa)
        t = wf["nodes"]["assistant"]["tools"][0]
        self.assertEqual(set(body_props(t)), {"tags", "data_lang", "meta_lang"})
        self.assertEqual(t["config"]["maxRetries"], 0)
        cfg = run_hook(t["pre_process_code"], "config", {"body": {"tags": "a, b", "data_lang": "x", "meta_lang": "y"}})
        self.assertEqual(cfg["body"], {"data": {"tags": ["a", "b"], "lang": "x"}, "meta": {"lang": "y"}})

    def test_no_tools_at_all(self):
        p = {"rules": [{"id": "1", "text": "Answer questions about opening hours."}], "variables": []}
        wf = G.generate_workflow(p)
        self.assertEqual(G.lint_workflow(wf), [])
        self.assertEqual([t["function"]["name"] for t in wf["nodes"]["assistant"]["tools"]], ["end_conversation"])
        self.assertIn("{{now}}", wf["system_prompt"])

    def test_collection_policy_keeps_its_graph(self):
        fixture = KIT / "tests" / "fixtures_policy"
        files = sorted(fixture.glob("*.json")) if fixture.is_dir() else []
        if not files:
            self.skipTest("no collection fixture")
        wf = G.generate_workflow(json.loads(files[0].read_text()))
        self.assertIn("greet_verify", wf["nodes"])


class GenericOnTheRepoMockTest(unittest.TestCase):
    """The repository's own mock openapi (final-mock/openapi.json), when it is there."""

    def test_mock_openapi(self):
        path = KIT.parent.parent / "final-mock" / "openapi.json"
        if not path.exists():
            self.skipTest("final-mock/openapi.json not in this checkout")
        oa = json.loads(path.read_text())
        p = {"language": "Turkish", "variables": [{"name": "customerNumber"}, {"name": "now"}], "tools": {}}
        wf = G.generate_workflow(p, None, oa)
        self.assertEqual(G.lint_workflow(wf), [])
        names = [t["function"]["name"] for t in wf["nodes"]["assistant"]["tools"]]
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_]+", n) for n in names))
        self.assertGreaterEqual(len(names), 2)
        for t in wf["nodes"]["assistant"]["tools"][:-1]:
            for hk in ("pre_process_code", "post_process_code"):
                self.assertLess(len(t[hk]), 23000)


if __name__ == "__main__":
    unittest.main()
