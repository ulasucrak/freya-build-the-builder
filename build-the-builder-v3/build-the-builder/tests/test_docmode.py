"""Document mode: services from the documents, tools on the given base URL, nothing else."""
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from builder import docmode as D, generate as G, testmode as T  # noqa: E402

API = {"language": "en-GB",
       "variables": [{"name": "customerNumber", "example": "12345678"}, {"name": "now", "example": "2026-10-12T10:30:00+03:00"}],
       "services": [{"name": "list items", "method": "post", "path": "v1/items", "purpose": "list", "response": "items",
                     "fields": [{"name": "customerNumber", "type": "string", "required": True, "from_variable": "customerNumber"},
                                {"name": "choice", "type": "string", "required": True, "description": "what was picked"}]}]}
FLOW = {"system_prompt": "Customer {{customerNumber}} {{other}}", "procedure": "1. Greet.", "endings": [{"name": "done"}]}


class DocMode(unittest.TestCase):
    def test_assemble(self):
        wf = D.assemble(API, FLOW, "https://mock.example.workers.dev/")
        self.assertEqual(G.lint_workflow(wf), [])
        props = wf["nodes"]["assistant"]["tools"][0]["function"]["parameters"]["properties"]
        self.assertEqual(props["url"]["default"], "https://mock.example.workers.dev/v1/items")
        self.assertEqual(props["method"]["default"], "POST")
        self.assertEqual(props["body"]["properties"]["customerNumber"]["default"], "{{customerNumber}}")   # never the model
        self.assertEqual(props["body"]["required"], ["choice"])
        self.assertNotIn("{{other}}", wf["system_prompt"])                                                   # unknown braces broken up
        hosts = set(re.findall(r"https?://([^/\"']+)", json.dumps(wf)))
        self.assertEqual(hosts, {"mock.example.workers.dev"})                                                # no other service

    def test_calendar_and_end(self):
        wf = D.assemble(API, FLOW, "https://m.example")
        vs = {"now": "2026-10-12T10:30:00+03:00"}
        T.run_code_locally(wf["nodes"]["setup"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"], {}, vs)
        self.assertTrue(vs["w_calendar"].startswith("Today: Monday 12 October 2026"))
        end = [t for t in wf["nodes"]["assistant"]["tools"] if t["function"]["name"] == "end_call_now"][0]
        T.run_code_locally(end["function"]["parameters"]["properties"]["code"]["default"], {"ending": "done"}, vs)
        self.assertEqual(vs["w_end"], "yes")


if __name__ == "__main__":
    unittest.main()
