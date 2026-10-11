# freya-statement-mock

Cloudflare Worker that mocks Freya Bank's voice statement/debt services (test environment), per the bank's
service contract of 1 October 2026 (`Build the Builder - Final Case/sources/documents/2026-10-01-service-contract.md`)
and its test data (`test-environment/test-customers.json`). Every test customer gets exactly the bank's answer:
same HTTP status, same body (byte for byte, number literals like `0.0` included).

## Endpoints

All services: `POST`, JSON body `{"data": {...}, "call": {"ani": "...", "lang": "tr"}}`, answer
`{"data": {...}, "result": {...}}`; errors carry only `result`. Only `data` decides the answer (token, headers
and `call` are not checked, as in the bank's test environment).

| Path | Looked up by | Not in the test data |
|---|---|---|
| `POST /voice/v1/cards.list` | `data.customerNo` (string or number) | 404 `FB_IVR_CHANNEL_102` |
| `POST /voice/v1/cards.balance` | `data.cardNumber` (exactly as cards.list gave it) | 404 `FB_IVR_CHANNEL_120` |
| `POST /voice/v1/statement.week.get` | `data.cardNumber` | 404 `FB_IVR_CHANNEL_121` |
| `POST /voice/v1/statement.week.decide` | `data.cardNumber` + `data.newStatementWeek` (1-4, string or number) | 400 `INVALID_ARGUMENTS_ERROR` (also the card's current week) |
| `POST /voice/v1/statement.week.change` | `data.cardNumber` + `data.newStatementWeek` | 400 `INVALID_ARGUMENTS_ERROR` (also the card's current week) |
| `POST /voice/v1/contact.email.get` | `data.customerNo` (`intAddressInfoType` E/P/B required, same answer) | 400 `INVALID_ARGUMENTS_ERROR` |
| `POST /voice/v1/statement.pdf.send` | `data.customerNo` (all five fields required) | 404 `FB_IVR_CHANNEL_107` |
| `POST /oauth2/token` | contract section 2; returns a test token (never checked) | |
| `GET /health` | `{"ok": true}` | |

- Required field missing or empty: 400 `INVALID_ARGUMENTS_ERROR`, message names the field (e.g. `card number cannot be empty`).
  Required: cards.list `customerNo`; cards.balance / week.get `cardNumber`; decide / change `cardNumber`,
  `newStatementWeek`, `statementDay`; contact.email.get `customerNo`, `intAddressInfoType`; pdf.send `customerNo`,
  `cardNumber`, `currencyType`, `statementDate`, `emailAddress`.
- Malformed JSON body: 400 `INVALID_ARGUMENTS_ERROR`. Unknown path: 404 `NOT_FOUND`. Wrong method: 405 `METHOD_NOT_ALLOWED`.
- `delaySeconds` in the data (cards.balance of `540000******7211`, customer 94907600): the answer comes after 12 s.
- Unfilled template variables (`"{{customerNumber}}"`, `%7B%7B...`), e.g. from the panel's "Test API Request"
  button, get the sample record (customer 90000001, card `540000******1234`) instead of an error.
- Stateless: `statement.week.change` does not alter later answers (same as the bank's test data).
- Generated error answers get a random 32-hex `guid`; answers from the test data keep their own guid.

## Files

- `worker.js`: the Worker (ES module). `records.js`: the RECORDS tables, generated, do not edit.
- `gen_records.py`: `python3 final-mock/gen_records.py [path/to/test-customers.json]` regenerates `records.js`.
- `gen_openapi.py` -> `openapi.json`: OpenAPI 3.0 for the builder (operationIds `cards_list`, `cards_balance`,
  `statement_week_get`, `statement_week_decide`, `statement_week_change`, `contact_email_get`, `statement_pdf_send`).
  The server URL is a placeholder, `https://freya-statement-mock.REPLACE.workers.dev`; put the real one in after deploy.
- `test_mock.mjs`: replays every answer of test-customers.json plus the error cases.

## Test

```bash
node final-mock/test_mock.mjs            # all pairs + error cases, includes one real 12 s delay
SKIP_SLOW=1 node final-mock/test_mock.mjs
```

test-customers.json is read from `$TEST_CUSTOMERS_JSON` or `../Build the Builder - Final Case/test-environment/`.

Locally in the Workers runtime:

```bash
cd final-mock && npx wrangler dev --port 8787
curl -s http://localhost:8787/health
curl -s -X POST http://localhost:8787/voice/v1/cards.list -H 'content-type: application/json' \
  -d '{"data":{"customerNo":"90000001","aiFlow":"CardOps"},"call":{"ani":"5550000000","lang":"tr"}}'
```

## Deploy

```bash
cd final-mock
npx wrangler login     # once
npx wrangler deploy    # -> https://freya-statement-mock.<subdomain>.workers.dev
curl -s https://freya-statement-mock.<subdomain>.workers.dev/health
```

Then replace `REPLACE` in `openapi.json` (servers[0].url) with the subdomain. Bundle: about 350 KiB
(16 KiB gzip), well under the free-plan limit.
