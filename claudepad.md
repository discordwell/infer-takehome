# Claudepad

## Session Summaries

### 2026-05-19 06:45Z — Auto-repair pickup mechanism + verifier Chrome-CDP fix

Closed the "DONE-but-never-landed" gap that prior session
`f9dbc579...` flagged: the verifier was accepting `STATUS: DONE` and
then the in-container fix vanished on the next `deploy.sh` (which
rsyncs the whole tree, overwriting claude's edits to `/app`). Now
verified patches land in two complementary places.

- **New module** `backend/auto_repair_patches.py`:
  - `record()` — saves `git diff HEAD` to
    `storage/patches/<ts>__<carrier>__<sid>.patch` on the
    `infer-storage` named volume.
  - `apply_pending()` — at container boot, walks the dir and runs
    `git apply --check` then `git apply` for each. Clean apply →
    stays put (re-applies every restart). Context-not-present (i.e.
    deploy carried the upstream merge) → renamed `.LANDED`. Real
    conflict / error → renamed `.SKIPPED`. CLI entry
    `python -m backend.auto_repair_patches apply`.
  - `push_to_branch()` — side-clones via `tempfile.mkdtemp`, applies
    diff with `--3way`, commits as `auto-repair bot
    <auto-repair@infer.local>`, force-pushes to
    `auto-repair/<session_id>`. PAT loaded from
    `storage/secrets/github_pat`.

- **Dockerfile CMD** wraps in `sh -c` so `apply_pending` runs BEFORE
  `uvicorn` imports the carrier modules (otherwise the patched code
  wouldn't be live in-process). `|| true` so a broken patch can't
  block boot; `exec` so SIGTERM propagates.

- **`auto_repair.py`** gets `_persist_and_push_patch()` called from
  `_check_done` immediately after `_verify_fix` passes. Best-effort
  — any failure here doesn't undo the verifier accept.

- **Verifier fix** in `_verify_fix`: reads `username` from
  `context.json`, calls `flow.context_options_for_username()` (or
  bare `context_options()` if no username), drops `_initial_url`,
  routes to a dedicated `storage/browser-profiles/verify/<carrier>-
  <sid>` profile dir, and passes through to
  `pw_runner.new_context(storage_state=..., **opts)`. This switches
  the verify browser from plain headless Playwright to the carrier's
  Chrome-CDP stealth context — the previous session 71a7e651's USAA
  reject was caused by Akamai blocking the plain headless shape with
  `ERR_HTTP2_PROTOCOL_ERROR`, NOT by claude's fix being wrong.

- **`repair_prompt.md`** gets a new "What happens after STATUS: DONE"
  section so the in-container claude understands its DONE will be
  (a) verified, (b) persisted to the named volume, (c) pushed to a
  GitHub branch. Encourages keeping the working-tree diff clean.

- **GitHub auth**: fine-grained PAT, scope = contents:write on
  `discordwell/infer-takehome` only. Stored at
  `storage/secrets/github_pat` (root:root, 600) inside the container
  via `docker cp` after chown, and also at `INFER_AUTOREPAIR_GH_PAT=...`
  in local `.env` (gitignored). PAT never embedded in git config —
  `push_to_branch` reads it per-call and builds the auth URL inline.

- **Wet-tested**: synthetic patch → recorded → applied on restart →
  pushed to a throwaway `auto-repair/wet-test-push-*` branch on GitHub
  with the right commit author/message/diff. Branch + check branch +
  /tmp staging + container patch all cleaned up afterward.

- **E2E result** (session `32d0546c...`, turn 1 → NEED_HUMAN at
  06:48Z): the pickup mechanism was NOT exercised because claude
  correctly identified an upstream blocker — the saved
  `auth_state.json` for cordwell has all the right USAA cookie names
  (UsaaMbWebMemberLoggedIn, MemberGlobalSession, JSESSIONID,
  socureSessionCookie, akusaa, rlas3, khaos) but USAA invalidated
  the server-side session. Every `/my/documents`,
  `/my/auto-insurance/`, and `/inet/wc/document_center` URL either
  302s to `/my/logon` or returns "Page Not Found". The deliberate
  selector break therefore never gets exercised — there are no
  document buttons (real or broken) to find. Claude ran 3 CDP
  probes against the live repair browser to confirm. Wrote a clean
  NEED_HUMAN with three follow-ups: (a) wire WORKER_PROXY_CARRIERS
  through to a real macOS worker, (b) refresh cookies via user
  re-login, (c) distinct "session expired" SessionState. **No
  spurious GitHub push happened** — correct behavior, the new
  `_persist_and_push_patch` only fires after `_verify_fix` passes,
  not on NEED_HUMAN.

- **Wet test fully exercised the new path** (synthetic patch on
  `backend/__init__.py`): record → apply on restart → push → branch
  on GitHub at commit `e4366e0a`. Branch + check-branch + container
  patch + reverted file all cleaned up.

- **Verifier fix is in place** but couldn't be demonstrated because
  the upstream cookie issue means `fetch_documents` would return 0
  docs regardless of selector validity. To actually see verifier-
  pass → GitHub-push happen, we need fresh USAA cookies (user re-
  login through a working flow).

- **Prod state after this batch**: image rebuilt twice (pickup
  infrastructure deploy, then revert-deliberate-break deploy).
  Working tree clean. Verifier upgraded to Chrome-CDP context.
  `auto-repair/wet-test-push-*` branch cleaned from GitHub.

- Commits: `321182d` (pickup mechanism), `5b3c97d` (verifier fix).
  Both pushed to main; no auto-repair branches currently on origin.

### 2026-05-19 — Feedback recovery, live Claude doc delivery, email-on-done

Three new subsystems on top of multi-session + live-Claude-UI:

- **PDF analyzer** (`backend/pdf_analyzer.py`): lazy. Spawns `claude -p` with the
  rejected PDFs attached as file paths, Claude reads them via its native PDF
  Read, returns per-doc `{name, label, description, category}` where category
  is `policy_doc` / `cover_or_brochure` / `login_or_error` / `other`. Cached
  to `storage/analyses/<sid>.json`. Only runs on user "wrong docs" click —
  no cost on the happy path.
- **Feedback recovery** (`backend/feedback_recovery.py` + `POST /api/feedback`):
  user clicks "Wrong documents" → run analyzer → call
  `auto_repair.capture_and_kick` with `kick_reason="user_rejected"` and the
  analysis stuffed into `extra_context` (read by Claude via context.json).
  The repair prompt has a new "Feedback-recovery context" section telling
  Claude to focus on finding better docs rather than patching the adapter.
- **Same-run delivery bridge** (`backend/repair_deliver.py`): Claude drops
  candidate PDFs into `storage/repair/<sid>/delivered/`; the controller
  polls on every `_check_done` (early + after STATUS) and ships them to
  the user via `session_manager.set_docs` → `docs_ready` SSE → frontend
  renders. Idempotent via a `delivered.json` marker. The repair_prompt got
  a new "Deliver real PDFs to the waiting user" section explaining this.
- **Auth state refresh** (`backend/repair_browser.py`): `cleanup()` harvests
  `ctx.storage_state()` before terminating chromium and persists it via
  `storage.save(carrier, username)` — every successful repair extends saved
  auth so future quick-path runs keep working. Plumbed `username` through
  `spawn()` and onto the `RepairBrowser` dataclass.
- **Email notify** (`backend/email_notifier.py` + `POST /api/notify`): when
  any repair is active, UI shows email opt-in. Watcher coroutine awaits
  `session.repair_done_event` with a 5h cap (`notify_wall_seconds=18000`).
  On fire it Resend-sends with PDF attachments (base64), or falls back to
  links if total > 20MB (`email_max_attachment_bytes`). Verified domain:
  `Infer <noreply@mail.discordwell.com>` (key from
  `catena-takehome/.env`). httpx wrapper — no Resend SDK dep.
- **Frontend** (`frontend/{index.html,app.js,style.css}`): docs view gets
  ✓/✗ buttons. ✗ stashes rejected docs into a "Previous attempt" expander,
  shows "Looking for better documents…", reveals repair panel + email
  opt-in. Tracks `rejectedDocIds` so SSE snapshot replays don't re-render
  the same docs the user just rejected.
- **Session manager extensions**: `Session.repair_done_event` (asyncio.Event
  the watcher awaits), `notify_email`, `notify_started_at`,
  `feedback_recovery_active`, `pdf_analysis`. `publish_repair_done` sets
  the event.
- **`capture_and_kick`** gets `kick_reason` (default
  `"orchestrator_error"`) and `extra_context` kwargs. Reason is recorded
  in `context.json`; Claude branches on it via the repair prompt.
- **Tests**: 27 new across `test_repair_deliver`, `test_pdf_analyzer`,
  `test_email_notifier`, `test_feedback_recovery`. Mocks: claude
  subprocess (canned JSON via AsyncMock), Resend httpx (fake client). All
  130 offline tests pass in ~13s.
- **Wet test**: two-tab flow via `/?fresh=N` — login → MFA → docs render
  with feedback bar; ✓ releases slot; ✗ moves docs to expander, shows
  recovery panel + email opt-in. Email opt-in accepts and disables form.
  Fixed bug where SSE snapshot replay re-rendered rejected docs (now
  guarded by `rejectedDocIds` set).
- **NOT deployed yet**: other agent has uncommitted edits to Dockerfile +
  usaa.py + docker-compose.prod.yml in flight. Holding deploy until those
  land.

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
