# Claudepad

## Session Summaries

### 2026-05-19 — Multi-session support: per-uid slot, per-carrier lock, live Claude UI

- **Identity** (`backend/identity.py` new): `demo_uid` cookie issued on first
  response, 30-day HttpOnly, Secure via X-Forwarded-Proto (first hop only).
  NAT-resilient — two browsers on the same NAT get different uids.
- **Slot manager** (`backend/slot_manager.py` new): one active slot per uid
  with 60s idle TTL (refreshed by SSE heartbeats). Per-carrier exclusion —
  only one uid drives a given carrier at a time. Methods are deliberately
  sync; docstring warns that adding `await` would create a TOCTOU on carrier
  ownership.
- **`/api/login`** claims the slot BEFORE minting the Session (no phantom
  Session on 423). Returns `423 {"detail":"carrier-busy","boring_url":
  "/api/cache"}` when another uid owns the carrier. Same uid + same carrier
  resumes their in-flight session.
- **Boring endpoint** (`/api/cache`): per-uid past results via
  `result_store.latest_for_uid()`. Privacy — each uid only ever sees its own
  runs (per-uid index at `storage/results/_uid_index/<uid>.json`).
- **Live Claude UI**: `auto_repair._run_claude` switched to `--output-format
  stream-json --verbose`. Each assistant text / tool_use / tool_result block
  is translated to a display chunk (returns a list so multi-block messages
  aren't dropped) and pushed through `session_manager.publish_repair_log`
  to the triggering session's SSE as `repair_log` events. Final STATUS
  surfaced as `repair_done`. Per-turn transcripts persisted under
  `storage/repair/<session>/turns/<n>/`.
- **SSE keep-alive** on ERROR now gates on `session.repair_kicked` (set by
  the orchestrator when `capture_and_kick` returns True). Fixes the bug
  where per-carrier dedup would leave the stream hanging forever for the
  second-failing session.
- **Frontend**: new "boring" state (carrier-in-use banner + per-uid cache
  list), collapsible Claude log panel with color-coded chunks, verdict
  banner on `repair_done`.
- **Tests**: 30 new tests (slot manager, identity, per-uid cache,
  423-carrier-busy, same-uid reload, cache isolation). Conftest adds
  autouse `reset_slot_manager`, `reset_session_manager` (cancels pending
  orchestrator tasks), and `short_mfa_timeout` (2s) so test teardown doesn't
  sit on the 300s default MFA timeout. All 103 offline tests pass in ~13s.
- **Wet test**: two browser tabs across `127.0.0.1` and `localhost`
  (separate cookie jars). First claimed geico, second got the boring page;
  slot released on DONE so second could then claim; per-uid cache returned
  correct (privacy-isolated) data; repair-log panel rendered correctly.
- **Code-review fixes applied** (general-purpose sub-agent review):
  cleaner `_json()` helper instead of `_wrap_response` raw-header hack;
  claim before create in `/api/login`; tightened `X-Forwarded-Proto`
  parsing; `_translate_stream_event` returns a list; stdout drain is now
  unbounded (readline returns empty on EOF); `slot_manager` sync-only
  invariant documented.

### 2026-05-19 — Claude-in-container + self-healing auto-repair

- Installed `@anthropic-ai/claude-code` in the prod Docker image (Node 20 via
  NodeSource). Persistent `/root/.claude` on the `infer-claude-home` named
  volume. Container claude is authenticated against the user's Claude.ai
  plan (OAuth token extracted from local macOS Keychain and copied to the
  VPS, 600 perms, root-owned). No `ANTHROPIC_API_KEY` path — env var
  stripped from compose to lock in plan-only auth.
- `scripts/claude-remote` opens an interactive claude session in the
  deployed container via `ssh ovh2 + docker compose exec`.
- Self-healing loop: `backend/auto_repair.py` + `backend/repair_prompt.md`
  + `scripts/repair_probe.py`. On orchestrator ERROR the controller writes
  `storage/repair/<session_id>/context.json` (+ best-effort copy of any
  saved `storage_state` as `auth_state.json`) and spawns `claude -p` with
  `--allowedTools Read Write Edit Bash Glob Grep`. Claude is told to
  inspect the carrier site via `repair_probe.py` (which loads the saved
  storage_state into a fresh logged-in browser), edit
  `backend/carriers/<carrier>.py` if needed, run mock-mode pytest, and
  write a `STATUS` file (`DONE` or `NEED_HUMAN`).
- Cadence loop registered in `main.py` lifespan resumes any active repair
  every 5 minutes via `claude --resume`; per-carrier dedup, 30-min wall
  timeout, `REPAIR_ENABLED=false` kill switch.
- Default behavior: `auto_repair.is_enabled()` returns False unless
  `REPAIR_ENABLED` is explicitly set truthy (so tests don't spawn claude).
  `docker-compose.prod.yml` sets `REPAIR_ENABLED=true` so prod is opt-in
  via the compose file, not via in-code default.
- Verified end-to-end against a synthetic failure context: claude read
  context.json, ran `grep` + `pytest tests/test_integration.py` (6/6 green),
  recognized the failure as a smoke test, wrote a thoughtful NEED_HUMAN
  STATUS with a constructive suggestion. Wall time 105s, ~$0.30 against
  Claude plan quota (metered, not API-billed).
- Code review pass applied: subprocess kill on CancelledError + tracked
  in-flight task set + `auto_repair.shutdown()` called from main.py
  teardown; STATUS parser is now case-insensitive and lstrip-tolerant;
  `_check_done` also runs after resume turns; prompt updated to be honest
  about which context files are actually written.
- CDP attach added in follow-up: `backend/repair_browser.py` spawns a
  long-lived headless chromium with `--remote-debugging-port=0`, preloads
  cookies from the saved storage_state via a transient Playwright connect,
  then disconnects (chromium keeps running). `auto_repair.capture_and_kick`
  writes `cdp_endpoint.txt` so claude can attach via
  `repair_probe.py --cdp-endpoint <url> [--url <nav>]`. State persists
  across multiple probe calls in the same session. Cleanup hooks fire on
  STATUS, wall-time timeout, and lifespan teardown. Smoke-tested with a
  fake storage_state against example.com.

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
