# Infer Take-Home — Carrier Portal Document Puller

A small web app that signs into a personal-lines auto insurance carrier portal, handles MFA in-flow, and pulls policy documents back to the browser.

> Reviewer notes: see [ASSIGNMENT.md](./ASSIGNMENT.md) for the prompt, [ARCHITECTURE.md](./ARCHITECTURE.md) for the design. **You can run the full UI flow without credentials** via the mock carrier mode below.

## Stack

- **Backend:** Python 3.11+ • FastAPI • Playwright (async) • httpx • Server-Sent Events
- **Frontend:** vanilla HTML / CSS / JS (single file each, no build step)
- **Active carrier:** Geico. Progressive / Allstate / State Farm appear in the dropdown but are stubbed — the `CarrierFlow` abstraction makes each one ~half a day to add.

## Quickstart

```bash
uv sync                              # installs deps (uv 0.10+)
uv run playwright install chromium   # ~150 MB browser download
uv run uvicorn backend.main:app --port 8000
# open http://localhost:8000
```

For the live Geico flow, fill `.env` first:

```bash
cp .env.example .env
# edit .env and set GEICO_USERNAME / GEICO_PASSWORD if you want to run the smoke test
```

## Run modes

### Mock mode (no credentials needed — great for reviewers)

```bash
CARRIER_MOCK=1 uv run uvicorn backend.main:app --port 8000
```

The dropdown still shows Geico, but the Playwright flow is replaced with `MockFlow`: it sleeps ~1s total, requests an MFA code from the UI, then returns three valid 503-byte PDFs. This exercises every code path (state machine, SSE, MFA round-trip, session reuse) without a real carrier round-trip. Reviewers can:

1. Pick Geico
2. Enter anything for username / password (e.g. `demo@example.com` / `pw`)
3. Submit, then enter any MFA code
4. See three docs render inline with an end-to-end latency readout

To exercise edge cases:

```bash
MOCK_BAD_PASSWORD=1 CARRIER_MOCK=1 uv run uvicorn backend.main:app  # surfaces the error UI
MOCK_SKIP_MFA=1   CARRIER_MOCK=1 uv run uvicorn backend.main:app   # carrier-trusts-device path
```

### Live Geico mode

With real `GEICO_USERNAME` / `GEICO_PASSWORD` in `.env`, run plain `uvicorn` (no `CARRIER_MOCK`). The first run does a full Playwright login + MFA prompt + doc fetch. The second run (same username) skips MFA via the stored `storage_state` — visible in the Loom.

## Run flow

1. Pick a carrier from the dropdown (Geico is the only one wired up).
2. Enter portal username + password, submit.
3. When MFA is required, the UI surfaces a code input. Enter the SMS/email code.
4. Policy documents render inline (PDF preview + download button per doc).
5. Subsequent runs for the same user reuse the saved session and **skip MFA** if it's still valid.

Total post-MFA latency (carrier-side network + Playwright + doc download + render) is the target metric; the UI shows it in milliseconds when docs land.

## Tests

```bash
uv run pytest                                      # 24 tests, ~5s — no Chromium
GEICO_USERNAME=… GEICO_PASSWORD=… uv run pytest tests/test_geico_smoke.py -v -s  # interactive
```

The smoke test launches real Chromium, prompts on stdin for the MFA code, and asserts ≥1 PDF is returned. It is skipped automatically when creds aren't set.

## Project layout

```
backend/
  main.py              # FastAPI app, routes, SSE
  session_manager.py   # state machine + asyncio events
  storage.py           # disk-persist Playwright storage_state by sha256(carrier+user)
  orchestrator.py      # drives the full login→MFA→docs flow
  playwright_runner.py # shared Playwright + Chromium lifecycle
  models.py            # Pydantic schemas
  config.py            # env-loaded settings
  carriers/
    base.py            # CarrierFlow ABC
    geico.py           # Geico-specific Playwright flow
    mock.py            # MockFlow for testing without creds
    registry.py        # carrier → flow lookup
frontend/
  index.html app.js style.css
tests/
  conftest.py
  test_session_manager.py test_storage.py test_integration.py test_geico_smoke.py
scripts/
  inspect_geico.py     # one-shot tool that captures Geico's form selectors
```

## Latency

Target is **< 8s from MFA submit to docs rendered**. Where the budget goes:

| Step | Estimate |
|---|---|
| MFA POST → background task fills code | ~0.2 s |
| Playwright submits + lands on dashboard | ~1.5–2.5 s |
| Lift cookies into httpx, fetch doc index + PDFs in parallel | ~1.5–3 s |
| SSE event with doc list to client | ~0.1 s |
| `<embed>` requests PDF bytes from in-memory cache | ~0.5–1.5 s |
| **Total** | **~4–7 s** |

Key trick: once Playwright has authenticated, we **lift cookies into an `httpx.AsyncClient` and fetch PDFs in parallel** (`asyncio.gather`). DOM-walking through Playwright would add several seconds.

## Session reuse

After a successful run we persist Playwright's `storage_state` to `storage/sessions/<sha256(carrier+user)>.json`. On the next `POST /api/login` for that same `(carrier, username)`, the orchestrator:

1. Opens a Playwright context with the stored cookies.
2. Navigates to the carrier's dashboard URL.
3. If we land on the dashboard (not a login page), skip MFA entirely and go straight to fetching docs.
4. If the carrier expired the session, fall through to fresh login.

This is what makes "reliability and session reuse" measurable in the Loom — back-to-back runs visibly skip MFA on the second one.

## Known limitations / out of scope

- **Session state on disk is unencrypted.** Demo only — prod should encrypt with an env-key or OS keychain. Trivial to add (~10 lines).
- **Single-user in-process state.** The session manager assumes one user at a time. Concurrent users would need either a Redis-backed manager or process-per-user.
- **Only Geico is wired end-to-end.** Others (Progressive, Allstate, State Farm) are stubbed; each is ~half a day to add via `CarrierFlow`.
- **MFA timeout is 90s.** If the user doesn't type the code in time, the session errors out. No "resend code" flow.
- **No bot-defense evasion beyond default Playwright + a desktop UA.** Geico's customer portal doesn't appear to need stealth (`ecams.geico.com` isn't behind Akamai); add `playwright-stealth` if a future portal does.
- **HTTPS termination is the deployer's problem.** Local demo is plain HTTP.

## Credentials sourcing

Per the assignment: real credentials must come from a friend or family member. Geico was the chosen target because (a) no detectable bot wall on `ecams.geico.com`, (b) SMS/email MFA (vs. authenticator-app, which would block this design), (c) ~12% US market share so credentials are findable. See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full rationale and the Progressive contingency.
