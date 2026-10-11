# S1 - Builder readiness for the FINAL CASE (Turkish card statement debt)

Paths below are relative to build-the-builder-v3/build-the-builder unless absolute.

## TL;DR
Today the builder produces a USELESS agent for this case: with an openapi of the bank's 7 services
(cards.list, cards.balance, statement.week.get/decide/change, contact.email.get, statement.pdf.send)
`generate_workflow` raises `ValueError: policy has no verify_identity tool` (builder/generate.py:1549-1551, roles
verify/account/promise/close are mandatory), the fallback policy fails the same way, and pipeline.generate_safe
(pipeline.py:296-309) returns `minimal_workflow()` (pipeline.py:245): ONE conversation node, NO tools, system prompt
"Follow the bank's brief below exactly." + first 60 000 chars of the doc block, no {{customerNumber}}/{{now}}, no
language line, and the LLM prompt stage is skipped for "minimal" (pipeline.py:518). Verified in a scratch run with a
contract-shaped openapi (scratchpad/fake_openapi.json): `generate_workflow -> ValueError policy has no verify_identity tool`.

## 1. INPUT (folder)
- build.py passes the folder; pipeline.read_briefs -> extract.read_documents (extract.py:229) which RGLOBs
  recursively (hidden files skipped). pipeline.expand_briefs (pipeline.py:96) is NOT recursive (top level only) - used
  only for the file list / fallback `source.briefs` -> inconsistent but harmless.
- Files read (15): BRIEF.md, cloudflare-mock/README.md (+ example worker.js, wrangler.toml, example-response.json),
  sources/documents/* (faq, ivr-prompt-sheet, service-contract), sources/emails/* (+ attachments analysis),
  sources/meetings/transcript, statement-debt-call-flow.pdf, test-environment/test-customers.json + .md.
- Ordering: only 4/15 files have a Date header/line -> order_known=False -> sorted by relative path + NOTE "latest dated
  wins". File-name dates (2026-10-09-..., 2026-10-07-transcript) are NOT used (_doc_date only reads headers).
- Size: NO cap anywhere in extraction. test-customers.json re-indented = 544 737 chars; doc block total 664 715 chars
  (~180-200k tokens) sent to 5 parallel calls (2x part1, 2x part2, rules) + resolve call. The probe shows it does
  not crash (opus answered part1 in ~51 s, part2 in ~90 s) but it is slow/expensive, drowns the rules in test data
  and the rules pass may list test-data rows as "rules". prompts.py cuts brief to 60 000 chars (MAX_BRIEF_CHARS),
  pipeline.fix_policy to 50 000, minimal_workflow to 60 000 -> in path order that cut drops the transcript, the PDF
  and most mails.
- PDF: extract._read_bytes (extract.py:160) keeps "printable strings" of the raw file -> 9 853 chars of compressed
  garbage (FlateDecode). A stdlib zlib + BT/ET TJ/Tj parser recovers the whole flow (tested in this session: "Call
  arrives from IVR", "Second factor needed? -> Back to IVR", "Card list service", "Ask last 4 digits", "Ask last 6
  digits", "TL payment status", "Read USD / EUR debt or say there is none", "Offer: change cut-off date or e-mail
  the statement", "IVR: transfer to agent" ...). Fonts here map bytes 1:1 (ToUnicode bfchar identity; <0001>->"fi").
- Missing files: README.md (top level) and example-scenarios.md named in BRIEF.md are NOT in the folder (only
  cloudflare-mock/README.md, which the extractor will see under the bare name "README.md"). Nothing breaks.
- Docs keyed by bare file name (docs.dates[f.name]) -> two files with the same name collide (not the case here).

## 2. DOMAIN ASSUMPTIONS (debt collection everywhere)
- extract.py CONTRACT/PART keys: identity questions, third_party, products/bands (days late), amounts floor/full,
  dates/deadlines, promise, callback windows, triggers(transfer/note/callback/close), notes, outcomes/closing table,
  close_call reasons (align_close_reasons, close_tool). No `language` key is extracted at all (TOP_KEYS has none).
- policy_schema: REQUIRED_KEYS = all collection sections; TOOL_ROLES = verify_identity..close_call; validate() on a
  non-collection policy -> "missing top-level key ..." -> pipeline spends an LLM policy_fix (120 s) for nothing.
- generate.py: fixed graph setup -> greet_verify -> load_account -> prepare_account -> negotiate -> turn_state ->
  close_check/wrap_up/finish/farewell/close_api/end. ROLES keyword match (verify/identity, account, promise, note,
  callback, transfer, close); verify/account/promise/close are REQUIRED (raise). Tools that match no role are
  silently ignored (never become workflow tools). All hooks (codegen/*.py: verify_pre/post, promise_pre/post,
  check_offer, note_*, callback_*, close_*, prepare_account, turn_state) + oracle.py are collection logic.
- Generic fallback today = pipeline.fallback_policy (tools + verbatim rules) -> same generator -> same ValueError ->
  minimal_workflow (no tools). There is no "unknown tool" path and no scenario-type (S1-S7) path in the generator.
- Self-test: builder/testmode.py fakes the bank by roles (verify/account/promise/note ...), casegen builds cases from
  bands/identity/callback, selftest health-checks the practice bank (FREYA_BANK_TEAM_KEY sink + ledger). Meaningless
  for this case.
- What the workflow looks like today for this case: minimal single node, English-agnostic, no tools -> every scored
  call that needs a debt read fails.

## 3. TOOLS + VARIABLES
- openapi -> tools: generate._openapi_tools (generate.py:227) reads only top-level requestBody properties. Contract
  bodies are envelopes {"data": {...}, "call": {...}} -> fields become `data`/`call` of type object; _api_tool
  (generate.py:414) coerces non-scalars to "string" with default "{{data}}" -> the model would send "data": "<text>"
  -> 400. extract.parse_openapi DOES keep nested `properties` (and `default`, e.g. aiFlow=CardOps) in policy.tools,
  but _api_tool ignores them.
- Tool name = operationId or last path segment: "cards.list" -> function name with a dot (LLM function names usually
  must match ^[A-Za-z0-9_-]+$) -> sanitize needed (keep URL as is).
- Baking: a body field is baked from a call variable only when the NAMES ARE EQUAL (generate.py:436). Contract field
  is `customerNo`, call variable `customerNumber` -> not baked; the model must fill it (and in minimal wf it does not
  even know the number). Need a name-normalising match (No/Num/Nr ~ Number) or an extracted field->variable binding.
- Responses: only load_account uses response_mapping; generic tools would rely on <tool>_response.data.* (auto
  flattened). Error envelope (HTTP 404 WARNING vs 500 ERROR, result.returnCode != "1", no `data`) needs a post hook
  with what_to_do_next; one test answer takes >10 s -> timeout must be >= 15 000 ms (DEFAULT timeout_ms 12000).
  Side-effect calls (week.change, pdf.send) maxRetries 0 (contract section 7).
- Call variables: workflow only shows them as {{name}} in default_system_prompt + `test_variables` from
  policy.variables[].example; the extract CONTRACT for variables has no `example`, so test_variables is usually
  empty. now: setup.py parses vars[now_var] (oracle.parse_now) and FALLS BACK TO THE SANDBOX CLOCK when unparsable
  (setup.py:5-9) -> violates "never the system clock" only if `now` is missing; fine otherwise.

## 4. LANGUAGE
- policy.language never extracted -> policy_schema default "English" -> system prompt "Speak English: ..."
  (generate.py:1311). static_greeting/static_entry are English-only (generate.py:588, 623) and return ''/None for
  non-English (OK: the model speaks). Fixed English tts: farewell "Thank you for your time. Goodbye."
  (generate.py ~1719), NEGOTIATE_ENTER, PRIVACY_LINE etc. are English instructions (fine as instructions, but spoken
  tts lines must be Turkish). oracle.py parse_amount/parse_spoken_date/speak_date/money are English-only (irrelevant
  if the collection hooks are not used).
- Needed: (a) extract `language` (+ verbatim Turkish phrases are already kept by the rules pass); (b) generic
  workflow system prompt "Speak Turkish" + "read amounts/dates in Turkish words: 5000.13 TL -> 'beş bin Türk Lirası
  on üç kuruş', 05.11.2026 -> '5 Kasım'"; (c) best: post hook adds spoken forms (`<field>_say`) for dd.MM.yyyy dates
  (Turkish month names) and amounts (Turkish number words + currency per block TL/USD/EUR/GBP) so the LLM copies
  instead of verbalising; (d) any tts line (farewell/transfer) taken from the brief's verbatim phrases, not English
  constants.

## 5. SELF-TEST
- self_test_and_repair uses casegen (collection cases), testmode fake bank by collection roles, health check on the
  practice bank host when FREYA_BANK_TEAM_KEY is set. For this case it is meaningless and can waste the budget.
- Use: `--no-test` (skips self-test AND repair; --repair-rounds 0 is then irrelevant). Keep --budget-min 28 default
  (extraction on this folder ~2-4 min; prompts stage up to 240 s). Suggested run_command:
  `python build.py {case_folder} --openapi final-mock/openapi.json --slot final --no-test`
  (+ unset FREYA_BANK_TEAM_KEY is not needed with --no-test).

## 7. RECOMMENDATION - minimal generic changes, ranked by impact (3 coders, disjoint files)

### Task G (highest impact) - generic "service agent" workflow. Files: builder/generate.py, NEW builder/codegen/generic_setup.py, NEW builder/codegen/generic_pre.py, NEW builder/codegen/generic_post.py, NEW tests/test_generate_generic.py
- In generate_workflow: if any of verify/account/promise/close role is missing -> `return generic_workflow(p, opts, P)`
  instead of raising (collection path untouched -> existing 498 tests unaffected).
- generic_workflow: setup (code: parse {{now}} -> w_today/w_now_text/w_weekday, never datetime.now unless now missing)
  -> `assistant` conversation node with EVERY policy tool as api_call:
  * function name sanitized `re.sub(r"[^A-Za-z0-9_]", "_", op)` (cards.list -> cards_list), URL unchanged;
  * nested request schemas flattened: model sees leaf fields (cardNumber, requestedWeek, email...), pre hook
    rebuilds the envelope into config["body"] from an embedded path map; constants from schema default/single
    enum (aiFlow=CardOps) baked; leaf whose normalised name matches a call variable (lowercase alnum; no|num|nr|id ->
    number; customerNo ~ customerNumber) baked from {{var}};
  * post hook: unwrap result["data"] envelope; ok = status 200 and result.returnCode in ("1",1,None); on error put
    `what_to_do_next` "service answered <code>/<type> (HTTP n): follow the rule for this case; never read messageText";
    add spoken forms for dd.MM.yyyy / YYYY-MM-DD dates and numeric amounts in policy.language (TR month names,
    number words, kuruş/cent), never invent zeros for missing blocks;
  * timeoutMs 15000; maxRetries 0 for tools whose name has change|send|create|update|delete|set, 1 otherwise.
  * terminal: code_exec tool `end_conversation(reason)` (reason enum: done / transfer_to_agent / main_menu /
    no_response) -> sets w_end_reason -> transitions to end_call nodes (verified pattern "tool sets var -> end node").
  * system prompt: "Speak <language>", call variables `customerNumber = {{customerNumber}}`, `now = {{now}}` with
    "never ask for a variable's value; every date decision uses now", ALL policy.rules (after override marks,
    reuse _phase_rules/shown_rules 'all'), phrases verbatim, tool lines. test_variables from variables[].example.
- Acceptance: generate_workflow(policy, None, final-mock/openapi.json) returns a workflow that passes lint_workflow /
  Freya dry run; 7 tools named [A-Za-z0-9_]; cards_list body baked customerNo={{customerNumber}}, aiFlow=CardOps;
  pre hook run locally (testmode.run_code_locally) produces {"data":{...},"call":{...}}; post hook on a 404
  FB_IVR_CHANNEL_102 body yields what_to_do_next; system prompt contains "Turkish", "{{customerNumber}}", "{{now}}";
  old tests (python -m pytest tests -q) still 498 OK.

### Task X - extraction input hygiene + language. Files: builder/extract.py, tests/test_extract_inputs.py (new)
- read_documents: data files > 32 KB (json/csv) -> compact summary (structure, record count, first record, note
  "data file, N records, not shown"), never the full 545 KB; per-file cap ~40 KB with "[cut]".
- PDF: stdlib text extraction (zlib FlateDecode streams, BT/ET, Tj/TJ literal + hex strings, ToUnicode bfchar map),
  fallback to today's printable strings only if nothing is found.
- _doc_date: fall back to a leading YYYY-MM-DD in the file name (makes order mostly known).
- CONTRACT: add `language` (language of the calls, e.g. "Turkish") to PART1_KEYS/TOP_KEYS; variables entries get
  `example` (value example from the docs, e.g. 2026-10-12T10:30:00+03:00) so test_variables is emitted.
- Acceptance: read_documents(final folder) total < 150 000 chars; the PDF doc contains "Card list service" and
  "Ask last 4 digits"; extract_policy(...)["language"] == "Turkish" on the case (or unit test with a stub llm);
  practice briefs unchanged (tools/eval_extract.py or existing tests green).

### Task P - pipeline/schema/prompts glue. Files: builder/pipeline.py, builder/policy_schema.py, builder/prompts.py, tests/test_pipeline_generic.py (new)
- policy_schema.validate: when the tools have no collection roles, do not report missing collection sections
  (no policy_fix LLM round wasted); normalise keeps `language`.
- pipeline: self-test auto-skipped (trace note) when the workflow has no collection roles (generic) even without
  --no-test; minimal_workflow keeps tools via generic path; expand_briefs recursive like read_documents.
- prompts.py RULES/SYSTEM: neutral wording (drop "account data / state the debt" assumptions unless the step
  exists) + "all spoken text in policy.language; keep quoted phrases verbatim in their language".
- Acceptance: Builder(...).run() with stubbed deps on a generic policy -> status ok, no casegen, prompts stage runs
  (how != "minimal"); validate(generic policy) == [].

Run command after the changes:
`python build.py {case_folder} --openapi final-mock/openapi.json --slot final --no-test`
(final-mock/openapi.json: give every op an operationId without dots or rely on the sanitizer; describe request
schemas fully nested, with `default` for fixed values like aiFlow, and the 200 + error responses.)

## 6. PROBE RUN (practice openapi as stand-in; outputs redirected to scratchpad/probe, SCORE_LOG too)
Command: build.py "<Final Case>" --openapi tools/openapi.json --slot final-probe --no-test --budget-min 12
--out-dir/--trace-dir/--state-file in scratchpad/probe.
- extract part1 x2: ok 50-52 s each; part2 x2: ok 88-96 s each (665K-char prompt accepted by the model).
- extract verbatim RULES pass: FAILED TimeoutError after 210 s (round-1 deadline) -> rules fall back to
  numbered_rules(docs) (deterministic numbered-only parse): most of the bank's unnumbered rules (mails, transcript,
  prompt sheet rows) are lost. Cause: the rules prompt asks for one entry per TABLE ROW verbatim -> test-customers
  data + long sources make the answer huge. Fix = Task X input hygiene (data files summarised, size cap).
- replicates disagreed on `identity` -> resolve call (collection section irrelevant to this case: wasted time).
- Rest of the run (generate/prompts/dry-run/save) was still in progress when this report was written; see
  scratchpad/probe/stderr.log, probe/out/final-probe.*.json and probe/traces/ for the final state.
- Extracted policy (probe/out/final-probe.policy.json, extract ok in 222 s, 19 warnings):
  * GOOD: variables = customerNumber ("never asks"), now ("every date decision uses now"); clock.now_variable=now,
    timezone Europe/Istanbul, service date format dd.MM.yyyy noted; identity.questions = [] (IVR verified);
    27 domain triggers (agent_request, payment_request, silence, out_of_scope_question, angry_or_abusive_caller,
    service_error, slow_service, non_turkish_speech, same_card_name/same_last_four/same_last_six,
    no_registered_email, other_email_address, week_change_already_used, ...); 38 Turkish verbatim phrases
    (card_selection, same_name_last_four, same_last_four_ask_six, ...). -> the LLM extraction already captures the
    case; only the generator cannot use it.
  * BAD: language = None (-> "English"); bands 0; rules = 40 from the numbered fallback: 18 analysis, 12 kickoff,
    4 contract, 4 cloudflare-mock README + 2 BRIEF (hackathon meta-instructions "build your mock on Cloudflare..."
    would enter the agent prompt); decisions-recap (latest, overriding), transcript, IVR sheet, FAQ contribute 0 rules.
  * Task X should also exclude the event's meta-instructions: generic rule = files/sections addressed to the builder
    team (BRIEF.md "What you build/submit/scored", cloudflare-mock/) are context, not agent rules -> e.g. mark docs
    outside sources/ as "context" in documents_block and tell the rules prompt to list only rules for the phone agent.
- validate stage then started (bands empty -> likely a policy_fix LLM round); with the practice openapi the generator
  would build the collection graph (greet_verify/verify_identity etc.) - wrong for this case; with the real final
  openapi it would fall back to minimal_workflow (no tools).

### Probe final result (completed, exit 0, ~6 min total)
- validate: errors (75.5 s incl. a policy_fix LLM round): identity.questions empty, identity.fail.outcome empty,
  bands empty, trigger agent_request/payment_request "transfer without queue".
- generate: ok, 13 nodes = the collection graph (setup, greet_verify[verify_identity, schedule_callback,
  transfer_to_agent, close_call], load_account, prepare_account, negotiate[check_offer, create_promise,
  transfer_to_agent, close_call], turn_cap, close_check, wrap_up, finish, farewell, close_api, end_closed, end_forced).
- prompts: 7 LLM edits kept (the model tried to adapt: "never call check_offer or create_promise; a wish to pay goes
  to an agent"), 9 dropped. dry_run ok; saved to Freya as workflow 43fd676f-2748-4cdf-91f0-349b18a98095 (slot
  final-probe; state file in scratchpad, not in the kit's .freya-agents.json).
- system prompt starts "Speak English"; farewell tts "Thank you for your time. Goodbye."; test_variables absent;
  the 27 triggers appear in negotiate with their Turkish say-lines (e.g. "Sizi müşteri temsilcimize aktarıyorum.").
- Fatal for this case even with a matching openapi: greet_verify -> load_account needs verify_identity
  all_verified, but there are no identity questions -> the agent can never leave greet_verify, never loads cards or
  debt. Confirms that Task G (generic workflow) is required; prompt edits cannot fix the graph.
