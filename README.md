# Infer Take-Home — Carrier Portal Document Puller

A small web app that signs into a personal-lines auto insurance carrier portal, handles MFA in-flow, and pulls policy documents back to the browser.

> Reviewer notes: see [ASSIGNMENT.md](./ASSIGNMENT.md) for the prompt, [ARCHITECTURE.md](./ARCHITECTURE.md) for the design. **You can run the full UI flow without credentials** via the mock carrier mode below.

## Stack

- **Backend:** Python 3.11+ • FastAPI • Playwright (async) • httpx • Server-Sent Events
- **Frontend:** vanilla HTML / CSS / JS (single file each, no build step)
- **Active carriers:** USAA and Geico. Progressive / Allstate / State Farm appear in the dropdown but are stubbed.

## Quickstart

```bash
uv sync                              # installs deps (uv 0.10+)
uv run playwright install chromium   # ~150 MB browser download
uv run uvicorn backend.main:app --port 8000
# open http://localhost:8000
```

For live carrier flows, fill `.env` first:

```bash
cp .env.example .env
# set USAA_USERNAME / USAA_PASSWORD or GEICO_USERNAME / GEICO_PASSWORD
```

## Run modes

### Mock mode (no credentials needed — great for reviewers)

```bash
CARRIER_MOCK=1 uv run uvicorn backend.main:app --port 8000
```

The Playwright flow is replaced with `MockFlow`: it sleeps ~1s total, requests an MFA code from the UI, then returns three valid 503-byte PDFs. This exercises every code path (state machine, SSE, MFA round-trip, session reuse) without a real carrier round-trip. Reviewers can:

1. Pick USAA or Geico
2. Enter anything for username / password (e.g. `demo@example.com` / `pw`)
3. Submit, then enter any MFA code
4. See three docs render inline with an end-to-end latency readout

To exercise edge cases:

```bash
MOCK_BAD_PASSWORD=1 CARRIER_MOCK=1 uv run uvicorn backend.main:app  # surfaces the error UI
MOCK_SKIP_MFA=1   CARRIER_MOCK=1 uv run uvicorn backend.main:app   # carrier-trusts-device path
```

### Live carrier mode

With real credentials in `.env`, run plain `uvicorn` (no `CARRIER_MOCK`). USAA uses installed Google Chrome over CDP because USAA's Akamai edge blocks normal Playwright-launched Chromium before or during login. The first run does full login + email/phone MFA + doc fetch. The second run (same username) tries the stored `storage_state` quick path.

## Run flow

1. Pick a carrier from the dropdown.
2. Enter portal username + password, submit.
3. When MFA is required, the UI surfaces a code input. Enter the SMS/email code.
4. Policy documents render inline (PDF preview + download button per doc).
5. Subsequent runs for the same user reuse the saved session and **skip MFA** if it's still valid.

Total post-MFA latency (carrier-side network + Playwright + doc download + render) is the target metric; the UI shows it in milliseconds when docs land.

## Tests

```bash
uv run pytest                                      # no Chromium for the regular suite
RUN_LIVE_SMOKE=1 uv run pytest tests/test_usaa_smoke.py -v -s   # interactive
RUN_LIVE_SMOKE=1 uv run pytest tests/test_geico_smoke.py -v -s  # interactive
uv run python -m scripts.run_usaa_once                         # live USAA helper
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
    usaa.py            # USAA-specific Playwright flow
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

Target is **< 8s from MFA submit to docs rendered**. USAA is bot-sensitive, so the implementation returns the first verified PDF immediately rather than walking every document row before rendering.

| Step | Estimate |
|---|---|
| MFA POST → background task fills code | ~0.3 s |
| USAA submits + lands on authenticated page | ~1.7 s |
| Open latest document row and capture PDF | ~4.4 s |
| SSE event with doc list to client | ~0.1 s |
| `<embed>` requests PDF bytes from in-memory cache | ~0.5–1.5 s |
| **Measured USAA run** | **6.34 s to first PDF after MFA** |

Key trick: once Playwright has authenticated, the USAA adapter navigates straight to the real document-center route, clicks the first document row, and races PDF response/download/popup signals. It returns the first verified PDF immediately instead of waiting for page-wide `networkidle` or walking every document row.

## Session reuse

After a successful run we persist Playwright's `storage_state` to `storage/sessions/<sha256(carrier+user)>.json`. On the next `POST /api/login` for that same `(carrier, username)`, the orchestrator:

1. Opens a Playwright context with the stored cookies.
2. Navigates to the carrier's dashboard URL.
3. If we land on the dashboard (not a login page), skip MFA entirely and go straight to fetching docs.
4. If the carrier expired the session, fall through to fresh login.

This is what makes "reliability and session reuse" measurable in the Loom — back-to-back runs visibly skip MFA on the second one.

Latest USAA local check: stored state skipped MFA and returned one PDF in ~4.81s.

## Known limitations / out of scope

- **Session state on disk is unencrypted.** Demo only — prod should encrypt with an env-key or OS keychain. Trivial to add (~10 lines).
- **Single-user in-process state.** The session manager assumes one user at a time. Concurrent users would need either a Redis-backed manager or process-per-user.
- **MFA timeout is 90s.** If the user doesn't type the code in time, the session errors out. No "resend code" flow.
- **USAA requires headed Chromium.** The adapter uses a Chrome-like context and debug dumps under `/tmp` when Akamai or the portal shape blocks progress.
- **HTTPS termination is the deployer's problem.** Local demo is plain HTTP.

## Credentials sourcing

Per the assignment: real credentials must come from a friend or family member. Geico was the chosen target because (a) no detectable bot wall on `ecams.geico.com`, (b) SMS/email MFA (vs. authenticator-app, which would block this design), (c) ~12% US market share so credentials are findable. See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full rationale and the Progressive contingency.
