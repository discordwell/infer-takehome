# Architecture

## Overview

Single-page web app + FastAPI backend. Backend drives a Playwright browser session per user-login attempt, surfaces MFA prompts to the frontend via Server-Sent Events, and serves policy documents back to the UI.

```
┌─────────────┐    POST /login          ┌──────────────────┐
│  Browser    │ ─────────────────────► │  FastAPI         │
│  (vanilla   │    GET  /status (SSE)  │  (uvicorn)       │
│   JS + HTML)│ ◄───────────────────── │                  │
│             │    POST /mfa           │  ┌────────────┐  │
│             │ ─────────────────────► │  │ Session    │  │
│             │    GET  /docs/{id}     │  │ Manager    │  │
│             │ ◄───────────────────── │  └────────────┘  │
└─────────────┘                        │  ┌────────────┐  │
                                       │  │ Playwright │  │
                                       │  │ (Chromium) │──┼──► carrier.com
                                       │  └────────────┘  │
                                       │  ┌────────────┐  │
                                       │  │ Disk store │  │
                                       │  │ (sessions/)│  │
                                       │  └────────────┘  │
                                       └──────────────────┘
```

## Components

### `backend/session_manager.py`
In-memory state machine, one entry per session. Holds an `asyncio.Event` the login task awaits when MFA is needed, and an `asyncio.Queue` SSE subscribers read from.

States: `IDLE → LOGGING_IN → MFA_REQUIRED → AUTHENTICATING → FETCHING_DOCS → DONE` (or `ERROR` from anywhere).

### `backend/storage.py`
Persists Playwright `storage_state` to `storage/sessions/<sha256(carrier+username)>.json`. Loaded on subsequent runs to skip MFA when the session is still valid.

### `backend/orchestrator.py`
The login orchestration is here, not on the carrier. It tries the quick-path first (load stored cookies → `flow.is_authenticated()` → fetch docs), and falls back to full login + MFA on failure. Keeping orchestration out of `CarrierFlow` means each new carrier only writes the four hooks.

### `backend/playwright_runner.py`
Singleton that owns the Playwright + Chromium lifecycle. `new_context()` is an async context manager; `http_from_context()` lifts cookies from a `BrowserContext` into an `httpx.AsyncClient` for fast post-auth fetches.

### `backend/carriers/base.py`
`CarrierFlow` ABC with four hooks: `login`, `mfa_required`, `submit_mfa`, `is_authenticated`, `fetch_documents`. Orchestration is in `orchestrator.py`.

### `backend/carriers/geico.py`
Geico-specific Playwright flow against `ecams.geico.com`. It remains wired as a secondary adapter and smoke-test target.

### `backend/carriers/usaa.py`
USAA-specific Playwright flow. USAA's Akamai edge rejects normal Playwright-launched Chromium, so this adapter uses installed Google Chrome over CDP. After MFA, it navigates directly to the document center, clicks the first document row, and races PDF response/download/popup signals so the first verified PDF is returned immediately.

### `backend/carriers/mock.py`
Drop-in replacement for testing the stack without real credentials. Enable with `CARRIER_MOCK=1`. Used by the integration tests and lets reviewers exercise the full UI flow.

### `backend/main.py`
FastAPI app: `POST /api/login`, `GET /api/status/{id}` (SSE), `POST /api/mfa/{id}`, `GET /api/docs/{id}/{doc_id}`. Mounts `frontend/` as static.

### `frontend/`
Five DOM states swapped by JS (form, waiting, MFA, docs, error). SSE via `EventSource`. PDFs rendered inline with `<embed>` + a download button per doc.

## Latency budget (MFA submit → docs rendered)

| Step | Estimate |
|---|---|
| MFA POST → background task fills code | ~0.3s |
| USAA submits MFA and leaves challenge | ~1.7s |
| Navigate to document center + capture first PDF | ~4.4s |
| SSE event with doc list to client | 0.1s |
| `<embed>` requests PDF bytes from in-memory cache | 0.5–1.5s |
| **Measured USAA total** | **6.34s** |

Target is < 8s; the measured live USAA run fits with margin.

## Session reuse

First run: full login + MFA. After success, persist `storage_state` to disk.

Second run for the same `(carrier, username)`: open Playwright with stored state, navigate to the docs page. If we land there (not redirected to login), we skip MFA and complete in ~4.81s on the latest USAA check. If expired, fall back to fresh login.

## Security caveats

- Cookies on disk are unencrypted; this is a demo. Prod: encrypt with an env-key or OS keychain.
- Credentials live in memory only for the duration of the login flow — never persisted.
- HTTPS termination is the deployer's problem; local demo runs on plain HTTP.

## Carrier choice — why USAA

Decision matrix (auto-insurance customer portals, US):

- **USAA (chosen and measured):** Real credentials were available from the user. The site is bot-sensitive, but installed Chrome over CDP reaches login. Email/SMS MFA works, the document center returns real policy PDFs, and session reuse was verified.
- **Geico:** Wired as a secondary adapter. Lower bot friction than USAA, but this account was not the measured final path.
- **Progressive (contingency):** Comparable flow shape, often device-trusts after first login. Swap takes ~half a day via `CarrierFlow`.
- **Allstate:** Acceptable, less common.
- **State Farm:** Less attractive for this take-home because it often leans authenticator-app.

## Self-healing auto-repair loop

When the orchestrator hits `ERROR`, `backend/auto_repair.capture_and_kick`
writes a small failure context to `storage/repair/<session_id>/` and spawns
`claude` as a subprocess inside the container. Claude reads
`backend/repair_prompt.md`, inspects the failing carrier with
`scripts/repair_probe.py`, optionally edits `backend/carriers/<carrier>.py`,
runs mock-mode tests, and writes a `STATUS` file (`DONE` or `NEED_HUMAN`).

If the first turn doesn't terminate, a 5-minute cadence loop registered in
`main.py` lifespan resumes the same claude session via `--resume` until
either:
- the STATUS file lands, or
- the 30-minute wall timer elapses (controller writes `NEED_HUMAN`), or
- `REPAIR_ENABLED=false` is set (kill switch).

Per-carrier dedup: at most one claude per carrier at a time — additional
failures for the same carrier fold in rather than spawning new subprocesses.

Auth: claude inside the container uses the OAuth token at
`/root/.claude/.credentials.json` (persisted on the `infer-claude-home`
named volume across rebuilds). No API key is in use.

```
orchestrator ERROR ──► capture_and_kick ──► claude -p (subprocess)
                       writes context.json     │
                       copies auth_state.json   ▼
                                            reads prompt
                                            probes site (logged-in via
                                              storage_state)
                                            edits carrier adapter
                                            runs pytest
                                            writes STATUS
                       ┌────────────────────────┘
                       ▼
        cadence_loop (every 5 min)
        claude --resume <session>
        loops until STATUS or 30-min wall timeout
```

Tuning env vars: `REPAIR_ENABLED`, `REPAIR_MAX_WALL_SECONDS`,
`REPAIR_RESUME_INTERVAL_SECONDS`, `REPAIR_PER_TURN_TIMEOUT_SECONDS`.

## Out of scope

- Multi-tenant credential isolation.
- Concurrent users (the in-process manager assumes one user at a time).
- Carriers beyond USAA/Geico (others stubbed in dropdown; abstraction is in place).
- Bot-defense evasion beyond installed Chrome over CDP and a reasonable UA.
- "Resend MFA code" flow if the user misses the 90s window.
- Auto-repair commits/pushes patches to git (claude writes patches to the
  container filesystem only; an external cron is expected to pick them up).
