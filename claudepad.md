# Claudepad

## Session Summaries

_Add session summaries above this line before context compaction. Keep only the 20 most recent._

## Key Findings

### Final state (2026-05-17)

- Active measured carrier is **USAA**, using installed Google Chrome over CDP because USAA/Akamai blocks normal Playwright Chromium.
- Live USAA full MFA run returned one real PDF in **6.34s** from MFA code receipt to helper completion; first PDF bytes arrived at **6.046s**.
- Live USAA stored-session run skipped MFA and returned one PDF in **4.812s**.
- Hosted at `https://infer.discordwell.com` on `ovh2` via Docker Compose on `127.0.0.1:8310` behind Caddy.
- Offline suite: **27 passed, 2 skipped**.

### Project shape (2026-05-15)

- Infer take-home: carrier portal doc puller. Repo `github.com/discordwell/infer-takehome` (private). Deadline 72h from 2026-05-14.
- Stack confirmed: Python 3.11 + FastAPI + Playwright async + httpx + vanilla HTML/JS. SSE for status. uv-managed.
- Initial active carrier was **Geico** (`ecams.geico.com`); final measured carrier is **USAA**. Others (Progressive, Allstate, State Farm) appear in dropdown but are stubbed.
- Mock carrier path enabled via `CARRIER_MOCK=1` env — reviewers can exercise full flow without credentials.

### Geico portal notes

- Login URL: `https://ecams.geico.com/ecams/login` (redirects to `https://ecams.geico.com/`).
- Customer login is **not** behind Akamai/PerimeterX — default Playwright works, no stealth needed.
- Login form is React-rendered: inputs have no name/id/aria. Use label-based locator (`get_by_label("Email/User ID/Policy")`) with fallback to first visible `input[type=text]:visible` / `input[type=password]:visible`.
- Cookie banner (`#onetrust-reject-all-handler`) appears on first visit; dismiss before filling the form.
- Form selectors verified live by `scripts/verify_geico_form.py`. Post-MFA + docs page NOT yet verified — requires real credentials.

### Credential sourcing strategy

- Plan was to ask 4–5 close contacts (family > friends), prioritize Geico > Progressive > Allstate.
- User asked Claude to do the carrier research instead of picking blindly. Geico won on (low bot defenses, SMS/email MFA, common enough to find creds).

### Architectural decisions

- **Orchestrator (`backend/orchestrator.py`) owns flow control**, not `CarrierFlow`. Each carrier only writes 5 hooks (login, mfa_required, submit_mfa, is_authenticated, fetch_documents).
- **Latency trick:** after Playwright auth, lift `BrowserContext.cookies()` into `httpx.AsyncClient` and `asyncio.gather` parallel PDF fetches. Mock-mode measured 813ms post-MFA → docs-rendered.
- **Session reuse:** stored Playwright `storage_state` at `storage/sessions/<sha256(carrier+user)>.json`, unencrypted, gitignored. Quick-path on second login tries the stored cookies; falls back to full login on failure.
- **State machine:** `IDLE → LOGGING_IN → MFA_REQUIRED → AUTHENTICATING → FETCHING_DOCS → DONE | ERROR`. `submit_mfa` transitions to AUTHENTICATING atomically so a rapid second POST gets 409 (race fix from code review).

### Test coverage

- 25 tests, ~5s, no Chromium needed: state machine (10), storage (9), integration (6 — uses MockFlow + stubbed Playwright via `tests/conftest.py`).
- `tests/test_geico_smoke.py` is the live end-to-end test — skipped without `GEICO_USERNAME`/`GEICO_PASSWORD` in env. Prompts on stdin for MFA code.

### Code-review fixes applied (2026-05-15)

- **MFA race fix:** `submit_mfa` now transitions to AUTHENTICATING immediately so duplicates 409. New test `test_rapid_double_submit_rejects_second`.
- **Quick-path timeout:** `geico.is_authenticated` was 23s on slow fail (`networkidle` + `domcontentloaded`); dropped to 8s + 3s text scan.
- **Frontend double-submit guard:** disable submit buttons during requests.
- **EventSource cleanup:** close on `DONE` / `ERROR` client-side too.

### Historical pending items — now resolved via USAA

- Geico full flow was superseded by USAA because the user supplied real USAA credentials.
- Live post-MFA latency was measured on USAA at 6.34s.
- A second USAA run verified MFA-skip via stored session.
