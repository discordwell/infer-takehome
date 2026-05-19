# Carrier Doc Puller

Small FastAPI + Playwright web app for pulling policy documents from personal-lines carrier portals.

Hosted demo: https://infer.discordwell.com

## What It Does

The app exposes a simple browser UI:

1. Pick a carrier.
2. Enter the portal username and password.
3. The backend drives the carrier login in Playwright.
4. If the carrier asks for MFA, the UI shows a code field and submits the code back to the running backend session.
5. The backend fetches policy documents and the UI renders the PDFs inline.

The important backend contract is stable:

- `POST /api/login` starts the carrier automation.
- `GET /api/status/{session_id}` streams state with Server-Sent Events.
- `POST /api/mfa/{session_id}` submits an MFA code.
- `GET /api/docs/{session_id}/{doc_id}` streams the fetched PDF bytes.

## Current Carrier Status

- **Mercury:** working live path. The app expands Document History groups, selects the visible `Auto Insurance Policy Declarations` row, captures Mercury's `Document/V1` PDF payload, and renders the declarations PDF.
- **USAA:** implemented, but bot-sensitive. Hosted USAA is proxied to a trusted local worker because login is more reliable from a real local Chrome profile. The document fetcher opens Document Center, widens the date range, filters each policy account, and returns the latest policy packet/renewal per policy type while ignoring billing statements.
- **Geico:** experimental adapter exists as a secondary fallback.
- **Progressive, Allstate, State Farm:** experimental generic adapters.
- **Mock mode:** deterministic no-credentials path for local review and tests.

Production currently runs Mercury directly on OVH. Only USAA is proxied through the local worker/tunnel.

## Prerequisites

- Python 3.11+
- `uv`
- Playwright Chromium:

```bash
uv sync
uv run playwright install chromium
```

For USAA live runs, install Google Chrome locally. The default USAA flow uses a real Chrome profile for the sensitive login step.

## Local Server Setup

Install dependencies once:

```bash
uv sync
uv run playwright install chromium
```

Create a local env file for live runs:

```bash
cp .env.example .env
```

Then edit `.env` with only the credentials you intend to test, for example:

```dotenv
USAA_USERNAME=...
USAA_PASSWORD=...
USAA_MFA_EMAIL=you@example.com
MERCURY_USERNAME=...
MERCURY_PASSWORD=...
```

Run the FastAPI server from the repo root:

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The same server serves the UI and API.

Useful environment toggles:

- `CARRIER_MOCK=1`: no live carrier login; deterministic mock docs.
- `DEV_PREFILL_CREDS=1`: prefill credentials from `.env` in the local UI.
- `WORKER_PROXY_CARRIERS=usaa`: only USAA proxies to a worker; other carriers run in this process.
- `SESSION_TTL_SECONDS=86400`: keep active UI sessions and fetched document bytes for 24 hours.
- `AUTH_STATE_MAX_AGE_SECONDS=2592000`: try saved browser auth state for up to 30 days.
- `PERSIST_COMPLETED_RESULTS=true`: persist completed document results under `storage/results/`.
- `USAA_QUICK_PATH_MAX_AGE_SECONDS=0`: force a fresh USAA login instead of stored-session reuse.
- `USAA_LOGIN_DRIVER=os_browser`: use real local Chrome for USAA credential submission.
- `USAA_OS_BROWSER_PROFILE_DIR=storage/browser-profiles/<name>`: choose the Chrome profile used by the USAA OS-browser path.
- `LOG_FILE_PATH=storage/logs/app.log`: write persistent rotating app logs.

## Run Locally

### No Credentials: Mock Mode

Use this for a quick local verification of the full UI, MFA, SSE, and PDF-rendering flow:

```bash
CARRIER_MOCK=1 \
WORKER_BASE_URL= \
USAA_WORKER_BASE_URL= \
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and:

1. Pick any carrier.
2. Enter any username and password.
3. Enter any MFA code when prompted.
4. Confirm the mock PDFs render.

Mock edge cases:

```bash
MOCK_BAD_PASSWORD=1 CARRIER_MOCK=1 uv run uvicorn backend.main:app
MOCK_BAD_MFA=1      CARRIER_MOCK=1 uv run uvicorn backend.main:app
MOCK_SKIP_MFA=1     CARRIER_MOCK=1 uv run uvicorn backend.main:app
```

### Live Mercury Locally

Create a local env file and fill credentials:

```bash
cp .env.example .env
# set MERCURY_USERNAME and MERCURY_PASSWORD
```

Run the app:

```bash
WORKER_BASE_URL= \
USAA_WORKER_BASE_URL= \
WORKER_PROXY_CARRIERS=usaa \
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000, choose Mercury, and submit the credentials. Mercury should run directly in the local Playwright browser; no tunnel is needed.

### Live USAA Locally

USAA defaults to `USAA_LOGIN_DRIVER=os_browser`, which launches real Chrome with a dedicated profile:

```bash
cp .env.example .env
# set USAA_USERNAME and USAA_PASSWORD
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

If macOS blocks the OS-browser step, grant Accessibility permission to the terminal/Codex app that runs `uvicorn`.

Fresh local USAA e2e run, bypassing stored session reuse:

```bash
CARRIER_MOCK=false \
DEV_PREFILL_CREDS=false \
WORKER_BASE_URL= \
USAA_WORKER_BASE_URL= \
WORKER_PROXY_CARRIERS= \
USAA_QUICK_PATH_MAX_AGE_SECONDS=0 \
USAA_LOGIN_DRIVER=os_browser \
USAA_OS_BROWSER_PROFILE_DIR=storage/browser-profiles/usaa-local-e2e \
PLAYWRIGHT_HEADLESS=false \
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8053
```

Open http://127.0.0.1:8053, choose USAA, enter credentials, complete MFA in the UI, and verify the rendered PDFs. For the current USAA account shape, the expected result is one latest auto policy packet and one latest renters/property policy packet.

## Hosted Worker Mode

The public site can proxy selected carrier automation to a trusted local worker. This is currently used for USAA only.

Local worker:

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8040
```

Reverse tunnel from local machine to OVH:

```bash
ssh -N -R 127.0.0.1:8041:127.0.0.1:8040 ovh2
```

Hosted env:

```bash
USAA_WORKER_BASE_URL=http://host.docker.internal:8041
WORKER_PROXY_CARRIERS=usaa
```

With that config:

- USAA runs on the local worker.
- Mercury and other non-proxied carriers run inside the hosted OVH container.

Use `WORKER_PROXY_CARRIERS=all` only when every carrier should route through the local worker.

## Tests

Regular suite:

```bash
uv run pytest
```

Live smoke tests are opt-in and require credentials:

```bash
RUN_LIVE_SMOKE=1 uv run pytest tests/test_usaa_smoke.py -v -s
RUN_LIVE_SMOKE=1 uv run pytest tests/test_geico_smoke.py -v -s
```

## Project Layout

```text
backend/
  main.py              FastAPI routes, SSE, frontend serving
  orchestrator.py      login -> MFA -> document flow
  session_manager.py   in-process session state and events
  storage.py           Playwright storage_state persistence
  result_store.py      completed document/result persistence
  playwright_runner.py shared Playwright/Chrome lifecycle
  carriers/
    usaa.py            USAA-specific flow
    geico.py           Experimental Geico-specific flow
    generic_portal.py  Mercury and other generic carrier flows
    mock.py            no-credentials mock carrier
frontend/
  index.html
  app.js
  style.css
tests/
  unit and integration tests
scripts/
  live/debug helpers
```

## Session Reuse

After the app crosses login/MFA, Playwright storage state is saved under `storage/sessions/` using a hash of `(carrier, username)`. This stores browser login state such as cookies and localStorage, not passwords. Later runs for the same carrier and username try that state first. If the carrier still trusts the session, the app skips MFA and goes straight to documents; otherwise it falls back to a full login.

If the carrier prompts for MFA and the user never completes it, the app does not promote that browser state to a reusable login. The user will need to start a new login later; no password is retained.

For debugging only, the app also writes non-reusable partial auth snapshots to `storage/partial-auth/` when a login reaches MFA. These files can help inspect pre-MFA cookies and URLs, but no production code loads them for session reuse and they are not exposed through the UI/API.

By default, non-USAA saved auth state is tried for 30 days (`AUTH_STATE_MAX_AGE_SECONDS=2592000`). USAA uses a shorter 30-minute freshness window (`USAA_QUICK_PATH_MAX_AGE_SECONDS=1800`) because stale USAA sessions waste live attempts. Set either value to `0` to disable that app-side freshness check. Carrier cookies can still expire earlier if the carrier invalidates them.

Active UI sessions default to 24 hours via `SESSION_TTL_SECONDS=86400`. Completed document results are also persisted to `storage/results/`, so existing `/api/status/{session_id}` and `/api/docs/{session_id}/{doc_id}` links can survive a container restart until that TTL expires.

## Persistent Logs

The app writes rotating logs to `storage/logs/app.log` by default. In Docker production, `/app/storage` is a named volume, so logs, debug artifacts, browser profiles, and saved auth state survive container rebuilds and redeploys.

Useful hosted checks:

```bash
ssh ovh2 'cd /opt/infer-takehome && docker compose -f docker-compose.prod.yml logs --tail=200 infer'
ssh ovh2 'docker run --rm -v infer-takehome_infer-storage:/storage busybox tail -n 200 /storage/logs/app.log'
```

## Notes And Limitations

- Credentials should be provided through `.env` or the UI. Do not commit real credentials.
- Session state is persisted unencrypted under `storage/`; this is acceptable for a local take-home demo, not production.
- Runtime session management is in-process. Multiple production users would need a shared store such as Redis.
- The app optimizes for first useful policy PDF visible, then can extend to more document rows as needed.
- HTTPS termination is handled by the deployment environment; local development is plain HTTP.
