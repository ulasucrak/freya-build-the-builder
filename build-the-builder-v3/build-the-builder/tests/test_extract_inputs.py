"""Offline tests for extraction input hygiene (builder.extract, no model calls).

    cd build-the-builder && python -m unittest tests.test_extract_inputs -v

Covers: big json/csv data files summarised, per-file size cap, PDF text (FlateDecode content streams, Tj/TJ,
literal + hex strings, ToUnicode CMaps, object streams), file-name dates, the `language` section and
variables[].example, the "agent behaviour only" prompt wording and the verbatim rules call deadline.

Optional check on a real case folder (skipped unless set):
    EXTRACT_CASE_DIR=/path/to/case python -m unittest tests.test_extract_inputs.CaseFolder -v
"""
import copy
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
import zlib
from unittest import mock

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

from builder import extract, llm  # noqa: E402

OPENAPI = KIT / "tools" / "openapi.json"


# ---------------------------------------------------------------- a tiny PDF writer (test input only)

def _stream(dict_body, data, compress=True, length_ref=None):
    raw = zlib.compress(data) if compress else data
    length = b"%d 0 R" % length_ref if length_ref else b"%d" % len(raw)
    filt = b" /Filter /FlateDecode" if compress else b""
    return b"<< " + dict_body + b" /Length " + length + filt + b" >>\nstream\n" + raw + b"\nendstream", len(raw)


def _pdf(objects):
    out = bytearray(b"%PDF-1.7\n%\xb5\xed\xae\xfb\n")
    for num in sorted(objects):
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def _cid_codes():
    """code -> char for the Type0 test font (codes deliberately not equal to the characters)."""
    codes = {0x0003: " ", 0x0001: "fi", 0x0060: ":", 0x0061: "-"}
    for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        codes[0x0024 + i] = ch
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        codes[0x0044 + i] = ch
    for i, ch in enumerate("0123456789"):
        codes[0x0013 + i] = ch
    return codes


def _hex(text):
    rev = {v: k for k, v in _cid_codes().items()}
    return b"<" + "".join("%04X" % rev[c] for c in text).encode() + b">"


CMAP = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        b"4 beginbfchar\n<0003> <0020>\n<0001> <00660069>\n<0060> <003A>\n<0061> <002D>\nendbfchar\n"
        b"2 beginbfrange\n<0024> <003D> <0041>\n<0044> <005D> <0061>\nendbfrange\n"
        b"1 beginbfrange\n<0013> <001C> [<0030> <0031> <0032> <0033> <0034> <0035> <0036> <0037> <0038> <0039>]\n"
        b"endbfrange\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")


def make_text_pdf():
    """One page whose page tree lives in a compressed object stream; content stream length is indirect."""
    content = b"\n".join([
        b"q 0 0 1 rg 10 10 100 50 re f Q",
        b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (Call arrives \\(from the menu\\)) Tj ET",
        b"BT /F2 12 Tf 1 0 0 1 72 680 Tm " + _hex("Card list service") + b" Tj ET",
        b"BT /F2 12 Tf 72 660 Td [" + _hex("Ask") + b" -300 " + _hex("last") + b" 120 " + _hex(" 4 digits")
        + b"] TJ ET",
        b"BT /F1 12 Tf 72 640 Td [(Of) -20 (fer:) -400 (change)] TJ 0 -14 Td (the cut-off date) Tj ET",
        b"BT /F2 12 Tf 72 620 Td <0001> Tj " + _hex("nal step") + b" Tj ET",
        b"BT /F3 1 Tf 72 600 Td (\\000) Tj ET",
        b"BT /F1 10 Tf 72 580 Td (Caf\\351 menu) Tj T* (second line) Tj ET",
    ])
    inner = [(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
             (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
             (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 4 0 R /Contents 5 0 R >>")]
    header, body, off = b"", b"", 0
    for num, b in inner:
        header += b"%d %d " % (num, off)
        body += b + b"\n"
        off += len(b) + 1
    objstm, _ = _stream(b"/Type /ObjStm /N 3 /First %d" % len(header), header + body)
    content_obj, content_len = _stream(b"", content, length_ref=11)
    cmap_obj, _ = _stream(b"", CMAP)
    objects = {
        4: b"<< /Font << /F1 6 0 R /F2 7 0 R /F3 9 0 R >> >>",
        5: content_obj,
        6: b"<< /Type /Font /Subtype /TrueType /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        7: b"<< /Type /Font /Subtype /Type0 /BaseFont /Helvetica /Encoding /Identity-H /ToUnicode 8 0 R >>",
        8: cmap_obj,
        9: b"<< /Type /Font /Subtype /Type3 /FontMatrix [1 0 0 -1 0 0] /FirstChar 0 /LastChar 0 >>",
        10: objstm,
        11: b"%d" % content_len,
    }
    return _pdf(objects)


def make_image_only_pdf():
    img, _ = _stream(b"/Type /XObject /Subtype /Image /Width 1 /Height 1", b"\x00\x01\x02")
    content, _ = _stream(b"", b"q 1 0 0 1 0 0 cm /Im1 Do Q")
    return _pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 3: b"<< /Type /Page /Parent 2 0 R /Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
                 4: content, 5: img})


class PdfText(unittest.TestCase):
    def test_text_from_content_streams(self):
        text = extract._read_bytes(make_text_pdf(), "flow.pdf")
        self.assertTrue(text.startswith("(PDF text"), text[:80])
        for needle in ("Call arrives (from the menu)", "Card list service", "Ask last 4 digits",
                       "Offer: change", "the cut-off date", "final step", "Café menu", "second line"):
            self.assertIn(needle, text)
        self.assertNotIn("printable strings", text)
        self.assertNotIn("FlateDecode", text)
        lines = text.splitlines()
        self.assertIn("Offer: change", lines)       # Td with a vertical move starts a new line
        self.assertIn("the cut-off date", lines)

    def test_pdf_detected_by_magic_and_folder_read(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "diagram.bin").write_bytes(make_text_pdf())
            docs = extract.read_documents([d])
            self.assertIn("Card list service", docs[0][1])

    def test_fallback_to_printable_strings(self):
        text = extract._read_bytes(make_image_only_pdf(), "scan.pdf")
        self.assertTrue(text.startswith("(PDF, printable strings only)"), text[:80])
        broken = extract._read_bytes(b"%PDF-1.4\n1 0 obj\n<< /Length 5 /Filter /FlateDecode >>\nstream\n"
                                     b"xxxxx\nendstream\nendobj\n", "broken.pdf")
        self.assertTrue(broken.startswith("(PDF, printable strings only)"))

    def test_cmap_parsing(self):
        cmap, width = extract._pdf_cmap(CMAP)
        self.assertEqual(width, 2)
        self.assertEqual(cmap[0x0001], "fi")
        self.assertEqual(cmap[0x0024], "A")
        self.assertEqual(cmap[0x005D], "z")
        self.assertEqual(cmap[0x001C], "9")


class DataFilesAndCap(unittest.TestCase):
    def test_big_json_is_summarised(self):
        records = [{"recordNo": "%08d" % (i + 1), "label": "Person %d" % i,
                    "services": {"lookup": {"http": 200, "response": {"items": [{"n": i, "pad": "x" * 200}]}}}}
                   for i in range(300)]
        data = {"description": "Sample records for the test service", "records": records}
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "people.json").write_text(json.dumps(data, indent=2))
            self.assertGreater((d / "people.json").stat().st_size, extract.DATA_SUMMARY_BYTES)
            (d / "small.json").write_text(json.dumps({"a": [1, 2]}))
            docs = dict(extract.read_documents([d]))
        s = docs["people.json"]
        self.assertLess(len(s), 12000)
        self.assertIn("data file, 300 records, not shown", s)
        self.assertIn("Records: 300 at records", s)
        self.assertIn('"recordNo": "00000001"', s)               # first record shown
        self.assertNotIn("00000002", s)                          # the others are not
        self.assertIn("Sample records for the test service", s)  # short top-level values kept
        self.assertIn("recordNo, label, services", s)
        self.assertEqual(docs["small.json"], json.dumps({"a": [1, 2]}, indent=1))  # small data unchanged

    def test_big_csv_is_summarised(self):
        rows = ["id;name;amount"] + ["%d;name %d;%d.50" % (i, i, i) for i in range(3000)]
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "rows.csv"
            p.write_text("\n".join(rows))
            docs = extract.read_documents([p])
        s = docs[0][1]
        self.assertIn("data file, 3000 records, not shown", s)
        self.assertIn("Columns: id | name | amount", s)
        self.assertIn('"name": "name 0"', s)
        self.assertNotIn("name 2999", s)

    def test_per_file_cap(self):
        line = "Rule text that keeps going for a while to fill the file.\n"
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "long.md"
            p.write_text(line * 2000 + "Final decision: the last line.\n")
            text = extract.read_documents([p])[0][1]
        self.assertLessEqual(len(text), extract.MAX_DOC_CHARS + 100)
        self.assertGreater(len(text), extract.MAX_DOC_CHARS - 200)
        self.assertEqual(text.count("[cut:"), 1)
        self.assertTrue(text.startswith(line))
        self.assertTrue(text.rstrip().endswith("Final decision: the last line."))   # head and tail kept

    def test_total_budget_cuts_the_longest_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for i in range(5):
                (d / ("doc%d.md" % i)).write_text(("Long document %d line.\n" % i) * 1800)   # ~37K chars each
            (d / "short.md").write_text("Short memo.\n")
            whole = 5 * 1800 * len("Long document 0 line.\n") + len("Short memo.\n")
            self.assertEqual(sum(len(t) for _, t in extract.read_documents([d])), whole)   # under the budget: no cut
            with mock.patch.object(extract, "MAX_TOTAL_CHARS", 100000):
                docs = extract.read_documents([d])
        total = sum(len(t) for _, t in docs)
        self.assertLessEqual(total, 100000)
        self.assertGreater(total, 100000 - 2000)
        self.assertEqual(dict(docs)["short.md"], "Short memo.\n")
        self.assertTrue(all("[cut:" in t for n, t in docs if n != "short.md"))

    def test_nested_files_named_by_path(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for sub in ("x", "y"):
                (d / sub).mkdir()
                (d / sub / "README.md").write_text("1. Item from %s.\n" % sub)
            docs = extract.read_documents([d])
            self.assertEqual([n for n, _ in docs], ["x/README.md", "y/README.md"])
            self.assertIn('name="y/README.md"', extract.documents_block(docs))
            self.assertEqual([r["id"] for r in extract.numbered_rules(docs)], ["README.1", "README.1"])

    def test_big_json_without_records_is_capped_not_summarised(self):
        data = {"k%d" % i: "v" * 100 for i in range(600)}
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.json"
            p.write_text(json.dumps(data))
            text = extract.read_documents([p])[0][1]
        self.assertNotIn("data file", text)
        self.assertIn("[cut:", text)


class FileNameDates(unittest.TestCase):
    def test_leading_date_in_name(self):
        d = extract._doc_date(b"plain text", "plain text", "2031-04-05-notes.md")
        self.assertEqual(d.isoformat(), "2031-04-05T00:00:00")
        self.assertIsNone(extract._doc_date(b"x", "x", "notes-2031-04-05.md"))   # leading only
        self.assertIsNone(extract._doc_date(b"x", "x", "2031-13-45-notes.md"))    # not a date
        hdr = b"Date: Mon, 02 Mar 2031 09:00:00 +0000\n\nBody"
        self.assertEqual(extract._doc_date(hdr, hdr.decode(), "2031-01-01-mail.txt").isoformat(),
                         "2031-03-02T09:00:00")                                    # header wins

    def test_folder_with_dated_names_is_ordered(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "b").mkdir()
            (d / "b" / "2031-02-01-memo.md").write_text("Memo.")
            (d / "2031-01-15-contract.md").write_text("Contract.")
            (d / "a-mail.txt").write_text("Date: 2031-03-01\nMail.")
            docs = extract.read_documents([d])
            self.assertEqual([n for n, _ in docs], ["2031-01-15-contract.md", "b/2031-02-01-memo.md", "a-mail.txt"])
            self.assertTrue(docs.order_known)
            self.assertEqual(docs.dates["b/2031-02-01-memo.md"], "2031-02-01 00:00:00")
            (d / "undated.md").write_text("No date.")
            docs = extract.read_documents([d])
            self.assertFalse(docs.order_known)
            self.assertIn('date="2031-01-15 00:00:00"', extract.documents_block(docs))


def _stub_parts():
    """(part 1, part 2) answers of a stub model."""
    p1 = {"source": {"briefs": ["doc.md"]}, "language": "german",
          "variables": [{"name": "caller_ref", "meaning": "ref", "example": "A-17"},
                        {"name": "now", "meaning": "call time", "example": None}],
          "clock": {"now_variable": "now"}, "calendar": {"weekend": [6, 7], "holidays": []},
          "opening": {}, "third_party": {}, "identity": {"questions": [], "max_wrong": 0},
          "products": {"max_count": 1}, "bands": [], "callback": {}}
    p2 = {"amounts": {}, "dates": {}, "promise": {}, "triggers": [], "notes": {}, "outcomes": {"table": []},
          "phrases": {"bye": "Auf Wiederhören."}}
    return p1, p2


class LanguageAndContract(unittest.TestCase):
    def test_contract_keys(self):
        self.assertIn("language", extract.TOP_KEYS)
        self.assertIn("language", extract.PART1_KEYS)
        self.assertNotIn("language", extract.PART2_KEYS)
        self.assertIn("language", extract.CONTRACT)
        self.assertIn('"example"', extract.CONTRACT["variables"])
        self.assertIn("- language:", extract.part_prompt(extract.PART1_KEYS, "DOCS", {}, ""))

    def test_norm_language(self):
        self.assertEqual(extract.norm_language("german"), "German")
        self.assertEqual(extract.norm_language({"name": "Spanish"}), "Spanish")
        self.assertEqual(extract.norm_language(["French", "English"]), "French")
        for v in (None, "", "unknown", "null", 3):
            self.assertIsNone(extract.norm_language(v), v)

    def test_agent_behaviour_only(self):
        for txt in (extract.SYSTEM, extract.rules_prompt("DOCS")):
            low = txt.lower()
            for w in ("build", "deploy", "submi", "scor", "test"):
                self.assertIn(w, low)
            self.assertIn("phone agent", low)
        self.assertNotIn("outbound phone agent", extract.SYSTEM)

    def test_language_extracted_with_stub_llm(self):
        p1, p2 = _stub_parts()
        seen = {}

        def fake_ask_json(prompt, *, system=None, model=None, purpose, max_turns=1, timeout_s=0, retries=1,
                          deadline=None):
            seen[purpose] = (prompt, system, deadline)
            if purpose.startswith("extract policy part 1"):
                return copy.deepcopy(p1)
            if purpose.startswith("extract policy part 2"):
                return copy.deepcopy(p2)
            if purpose.startswith("extract verbatim rules"):
                return {"rules": [{"id": "doc.1", "text": "Rule one."}]}
            raise AssertionError(purpose)

        with tempfile.TemporaryDirectory() as d:
            brief = pathlib.Path(d) / "doc.md"
            brief.write_text("1. Rule one.\n")
            with mock.patch.object(llm, "ask_json", side_effect=fake_ask_json):
                warnings = []
                t0 = time.time()
                pol = extract.extract_policy([str(brief)], OPENAPI, budget_s=200, warnings=warnings)
        self.assertEqual(pol["language"], "German")
        self.assertEqual(list(pol), extract.TOP_KEYS)
        self.assertEqual(pol["variables"][0]["example"], "A-17")
        self.assertIsNone(pol["variables"][1]["example"])
        self.assertEqual(extract.local_validate(pol), [])
        self.assertIn("- language:", seen["extract policy part 1 (replicate 1)"][0])
        self.assertIn("build, test or host", seen["extract verbatim rules"][1])
        # the long verbatim rules answer may run until the end of the budget, not only round 1
        rules_deadline = seen["extract verbatim rules"][2]
        p1_deadline = seen["extract policy part 1 (replicate 1)"][2]
        self.assertGreater(rules_deadline, p1_deadline + 50)
        self.assertLessEqual(rules_deadline, t0 + 200)

    def test_language_missing_is_not_a_problem(self):
        p1, p2 = _stub_parts()
        del p1["language"]
        pol = extract.finalise([p1, p2, {"rules": []}], extract.parse_openapi(OPENAPI), [("doc.md", "x")], [])
        self.assertIsNone(pol["language"])
        self.assertEqual([p for p in extract.local_validate(pol) if "language" in p], [])


class ChunkedRulesPass(unittest.TestCase):
    def test_groups(self):
        small = [("a.md", "x" * 100), ("b.md", "y" * 100)]
        self.assertEqual(extract.rules_groups(small), [[0, 1]])
        big = [("d%d.md" % i, "z" * n) for i, n in enumerate([30000, 5000, 40000, 20000, 30000, 25000])]
        groups = extract.rules_groups(big)
        self.assertEqual(len(groups), 5)                           # 150K chars in 25K parts, whole documents
        self.assertEqual(sum(groups, []), list(range(6)))          # every document once, in order
        self.assertTrue(all(g for g in groups))
        self.assertEqual(len(extract.rules_groups([("one.md", "z" * 500000)])), 1)

    def run_flow(self, fail_part=None):
        p1, p2 = _stub_parts()
        calls = {}

        def fake_ask_json(prompt, *, system=None, model=None, purpose, max_turns=1, timeout_s=0, retries=1,
                          deadline=None):
            calls[purpose] = prompt
            if purpose.startswith("extract policy part 1"):
                return copy.deepcopy(p1)
            if purpose.startswith("extract policy part 2"):
                return copy.deepcopy(p2)
            if purpose.startswith("extract verbatim rules"):
                k = int(purpose.split("(part ")[1][0])
                if k == fail_part:
                    raise llm.LLMError("timed out")
                return {"rules": [{"id": "m%d.%d" % (k, j), "text": "Rule %d of part %d." % (j, k)}
                                  for j in (1, 2)]}
            raise AssertionError(purpose)

        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for i in range(3):
                (d / ("doc%d.md" % i)).write_text("1. Numbered rule of doc %d.\n2. Another.\n" % i)
            with mock.patch.object(llm, "ask_json", side_effect=fake_ask_json), \
                    mock.patch.object(extract, "RULES_CHUNK_CHARS", 20):
                warnings = []
                pol = extract.extract_policy([str(d)], OPENAPI, budget_s=200, warnings=warnings)
        return pol, calls, warnings

    def test_parts_in_parallel_and_in_order(self):
        pol, calls, _w = self.run_flow()
        parts = sorted(c for c in calls if c.startswith("extract verbatim rules"))
        self.assertEqual(parts, ["extract verbatim rules (part %d of 3)" % k for k in (1, 2, 3)])
        self.assertIn('List ONLY the rules found in these documents: "doc1.md"', calls[parts[1]])
        self.assertIn('name="doc0.md"', calls[parts[1]])                   # all documents stay as context
        self.assertEqual([r["id"] for r in pol["rules"]], ["m1.1", "m1.2", "m2.1", "m2.2", "m3.1", "m3.2"])

    def test_failed_part_falls_back_to_numbered_rules(self):
        pol, _calls, warnings = self.run_flow(fail_part=2)
        self.assertEqual([r["id"] for r in pol["rules"]], ["m1.1", "m1.2", "doc1.1", "doc1.2", "m3.1", "m3.2"])
        self.assertTrue(any("rules part 2 returned no rules" in w for w in warnings), warnings)

    def test_failed_part_keeps_prose_documents_as_paragraphs(self):
        docs = [("mail.txt", "Hi team,\n\nPlease say the due date twice.\n\nThanks"),
                ("rules.md", "1. First rule.\n2. Second rule.")]
        got = extract.document_rules(docs)
        self.assertEqual([r["id"] for r in got], ["mail.p1", "mail.p2", "mail.p3", "rules.1", "rules.2"])
        self.assertEqual(got[1]["text"], "Please say the due date twice.")


@unittest.skipUnless(os.environ.get("EXTRACT_CASE_DIR"), "set EXTRACT_CASE_DIR to a case folder")
class CaseFolder(unittest.TestCase):
    def test_case_folder_size_and_pdf(self):
        root = pathlib.Path(os.environ["EXTRACT_CASE_DIR"])
        docs = extract.read_documents([root])
        total = sum(len(t) for _, t in docs)
        print("\ncase folder: %d documents, %d chars" % (len(docs), total))
        self.assertLessEqual(total, extract.MAX_TOTAL_CHARS)
        for name, text in docs:
            self.assertLessEqual(len(text), extract.MAX_DOC_CHARS + 100, name)
            if name.lower().endswith(".pdf"):
                self.assertTrue(text.startswith("(PDF text"), name)
            if name.lower().endswith(extract.DATA_EXT) and (root / name).stat().st_size > extract.DATA_SUMMARY_BYTES:
                self.assertIn("not shown: summary only", text, name)


if __name__ == "__main__":
    unittest.main()
