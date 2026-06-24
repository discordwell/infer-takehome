# Claudepad

## Session Summaries

### 2026-06-23 — Treat a zero-document fetch as a failure (central orchestrator guard)

Maintenance pass. Closed a real reliability gap in the core login→docs flow:
a carrier that returned **0 documents** was published to the user as a
successful `DONE` ("0 documents retrieved") instead of erroring.

- **Bug:** the orchestrator never checked that a fetch returned anything. Two
  consequences: (a) on the **quick path** (stored cookies), an empty fetch
  "succeeded" with nothing rather than falling back to a fresh login when the
  saved session was merely stale — a session-reuse reliability hole; (b) on
  the **full-login path**, a genuine adapter failure that returned `([], {})`
  surfaced as a misleading 0-doc `DONE` instead of `ERROR` (so the user got
  no error and auto-repair never engaged). `backend/carriers/geico.py:247`
  was the concrete silent-empty offender (it only `log.warning`s on no links,
  then returns empty); the quick path could do the same for any carrier.
  Notably the **auto-repair verifier already enforced** this invariant
  (`auto_repair.py:807` → `"fetch_documents returned 0 documents"`), so the
  main orchestrator path was inconsistent with the project's own contract.
- **Fix:** new `orchestrator.NoDocumentsError` + pure `_require_documents()`
  helper, wired into `_fetch_documents_with_progress` — the single chokepoint
  all three fetch sites (USAA-direct quick, generic quick, full login) funnel
  through, so the guarantee holds for every carrier and any future one.
  Empty quick-path fetch → `NoDocumentsError` → caught → `_try_quick_path`
  returns False → fresh login. Empty full-login fetch → propagates → top-level
  handler → `set_error("carrier returned no documents")` + auto-repair kick.
  Identity-preserving on the happy path (returns the tuple unchanged). Left
  Geico's warning + debug-dump in place — diagnostics still fire, then the
  orchestrator raises.
- **Tests:** +6 (`tests/test_orchestrator_unit.py`) — pure `_require_documents`
  (pass-through incl. identity, raise-on-empty, raise-with-orphan-bytes),
  `_try_quick_path` empty→False vs docs→True, and full-login empty→ERROR with
  auth-state-saved-before-fetch (mirrors the existing fetch-failure test).
- **Verify:** full offline suite **244 passed, 2 skipped** (~18s, was 238);
  `ruff check` clean. Adversarial sub-agent review clean across quick path
  (both sub-branches), full-login, happy-path identity, partial-progress
  interleaving, and all six carriers' top-level `fetch_documents`. Confirmed
  no carrier returns `([], {})` as a legitimate success (USAA/generic/Mercury
  all raise; Geico's silent-empty is exactly the bug). ARCHITECTURE.md
  orchestrator section documents the guard. Committed on `main`; not pushed
  (orchestrator handles push).

### 2026-06-18 — Fix stringly-typed mock env flags (CARRIER_MOCK=true was a no-op)

Maintenance pass. Fixed a reviewer-facing latent config bug and removed the
dead settings behind it.

- **Bug:** the mock-carrier toggles (CARRIER_MOCK, MOCK_BAD_PASSWORD,
  MOCK_BAD_MFA, MOCK_SKIP_MFA, MOCK_QUICK_PATH_OK) were read with a literal
  `os.getenv(...) == "1"`, but `.env.example` (which the README tells reviewers
  to `cp`) and `mock.py`'s own docstring document them with pydantic-bool
  spellings (`true`/`false`). So `CARRIER_MOCK=true` silently ran the LIVE
  carriers instead of the mock, and `.env.example`'s shipped
  `MOCK_QUICK_PATH_OK=true` evaluated to *False* — a trap on the exact
  no-credentials path reviewers exercise. Meanwhile `config.py` defined five
  `*_mock*`/`carrier_mock` Settings fields that nothing ever read.
  `auto_repair.is_enabled()` already parsed `REPAIR_ENABLED` the tolerant way.
- **Fix:** new `backend/env_flags.env_truthy(name, default)` — accepts
  `1/true/yes/on` (case-insensitive, whitespace-trimmed); unrecognized →
  `default` so a typo never silently flips a flag. Routed registry's
  `CARRIER_MOCK`, mock.py's four flags, and `auto_repair`'s `REPAIR_ENABLED`
  through it. Removed the five never-read Settings fields from `config.py`
  (the wrong abstraction — these flags must be read at call time, after
  per-process monkeypatch, not from the import-frozen `settings` singleton)
  with a pointer comment. Also fixed a stale `base.py` docstring that named a
  nonexistent `runner.py`/`FlowRunner` (→ `orchestrator.execute_login`).
- **Strictly additive:** every previously-working input maps identically; the
  only deltas are the documented widening (`true/yes/on` now accepted) plus the
  two intended fixes. Adversarial sub-agent review clean; confirmed at real
  process level (`CARRIER_MOCK=true uv run python -c …` → `MockFlow`;
  unset → `UsaaFlow`).
- **Tests:** +38 (`tests/test_env_flags.py`) — `env_truthy`
  spellings/defaults/typo-safety, registry mock-selection incl. the
  `CARRIER_MOCK=true` regression, and the mock flags incl. the
  `MOCK_QUICK_PATH_OK=true` regression.
- **Verify:** full offline suite **238 passed, 2 skipped** (~16s, was 200);
  `ruff check` clean. Committed on `main`; not pushed (orchestrator handles
  push).

### 2026-06-17 — Fix SSE keepalive on DONE + guarantee terminal repair_done

Maintenance pass. Fixed a real latent bug in the SSE termination logic that
silently broke the feedback-recovery ("Wrong documents") live UI + same-run
doc delivery, then closed the follow-on hang class it exposed.

- **Primary bug:** `feedback_recovery.trigger` sets `repair_kicked = True`
  with the comment `# SSE will keep stream open`, but the auto-repair flow
  never transitions session state — so during feedback recovery the session
  stays in `DONE` the whole time. The SSE `event_gen` in `main.py` honored
  `repair_kicked` only on `ERROR`, not `DONE`: it broke unconditionally on
  the first `DONE` snapshot. So the reopened feedback-recovery stream closed
  before any live repair log or re-delivered `docs_ready` reached the client
  (the frontend's `rejectedDocIds` guard was a band-aid over this). The
  orchestrator-ERROR repair path worked; only the user-rejected (DONE) path
  was broken.
- **Primary fix:** extracted pure `main._should_close_stream(evt, *,
  repair_active)` — `repair_done` always terminal; `DONE`/`ERROR` terminal
  only when no repair is in flight. Unifies the two branches and makes `DONE`
  honor `repair_kicked` symmetrically. Verified strict behavior-preservation
  for happy-path DONE, ERROR±repair_kicked, and repair_done (adversarial
  sub-agent review + revert-test: the integration test hangs/fails on the
  old code).
- **Follow-on (exposed by the primary fix):** holding the stream open on
  `repair_kicked` surfaced three "held open but no `repair_done` will ever
  come" hangs: (a) a client reconnecting AFTER a real `repair_done` (network
  blip during a multi-minute repair — affects the *production* ERROR path
  too), (b) `REPAIR_ENABLED=false` feedback, (c) a kick folded into an
  already-active carrier repair (its `repair_done` only reaches the owning
  session).
- **Invariant fix:** a `repair_kicked` session is always guaranteed a
  terminal `repair_done`, delivered live AND replayed on reconnect.
  `session_manager.publish_repair_done` records the verdict on
  `Session.repair_terminal`; `subscribe()` replays it FIRST (ahead of
  repair_log + snapshot) so a post-conclusion reconnect closes immediately.
  `feedback_recovery` publishes a `NEED_HUMAN` terminal in the disabled +
  folded cases. Bonus: the disabled/folded terminals also fix a latent
  email-watcher hang (previously those sat on the 5h `notify_wall_seconds`).
- **Tests:** +16 (`tests/test_sse_termination.py` unit-tests the pure rule;
  `test_integration.py` adds two SSE regression tests — stays-open-during-
  feedback-recovery and reconnect-after-done-doesn't-hang, both proven to
  fail on the pre-fix code; `test_session_manager.py` covers terminal-replay
  ordering + success-path doc re-delivery; `test_feedback_recovery.py` covers
  disabled + folded terminals). Two adversarial sub-agent reviews, both clean.
- **Verify:** full offline suite **200 passed, 2 skipped** (~15s, was 184
  passed); `ruff check` clean. Docs: ARCHITECTURE.md "Live auto-repair UI"
  updated with the `_should_close_stream` rule + the terminal-replay
  invariant. Committed on `main`; not pushed (orchestrator handles push).

### 2026-06-17 — Fix STATUS-verdict parser bug + first auto_repair tests

Maintenance pass. Found and fixed a real latent bug in the auto-repair
loop's completion detection:

- **Bug:** `repair_prompt.md` tells claude to write `STATUS: DONE` (lines
  130, 165, and the "What happens after STATUS: DONE" heading), but the
  parser in `auto_repair._check_done` did
  `first_line.lstrip("#`* ").upper()` then `startswith("DONE")` — it
  stripped markdown decoration but NOT a literal `STATUS:` label. So a
  successful repair that wrote `STATUS: DONE` was never recognized; the
  session would loop until the 5h wall-clock timeout wrote NEED_HUMAN.
- **Fix:** extracted a pure `parse_status_verdict(body)` helper that
  tolerates an optional `STATUS`/`STATUS:` prefix (plus tabs), and rewired
  `_check_done` to use it. `first_line` still computed for logging.
  Verified purely additive — strict superset of the old accepting set, no
  reclassification of any input the old parser accepted (independent
  adversarial verifier confirmed). Reconciled the prompt wording with a
  one-line note that `STATUS: DONE` is accepted.
- **Tests:** new `tests/test_auto_repair.py` (54 tests) — the first tests
  for `auto_repair.py` (the largest backend module, previously 0 tests).
  Covers `parse_status_verdict` (incl. the regression case), plus the pure
  helpers `_translate_stream_event`, `_stringify_tool_result`,
  `_preview_dict`, `is_enabled`.
- **Hygiene:** fixed all 7 ruff findings (unused imports incl.
  `LoginResponse` in main.py, ambiguous `l`, f-string-without-placeholder)
  and added a `[tool.ruff]` config (py311, line-length 88) so the lint
  baseline is reproducible.
- **Verify:** full offline suite 184 passed, 2 skipped (~13s, was 130
  passed); `ruff check` clean. Committed on `main`; not pushed
  (orchestrator handles push).

### 2026-05-19 16:10Z — Auto-repair end-to-end happy path proven on GitHub

Session `3f12c5d996ca4e70a3b87a3deeb9c3a8` produced the first
auto-repair branch from a verified-DONE fix:
`auto-repair/3f12c5d996ca4e70a3b87a3deeb9c3a8` at commit `0b4b6977`,
a 6-line viewDocument-→readDocument- selector fix authored by the
in-container claude, signed by `auto-repair bot
<auto-repair@infer.local>`, body containing the full skill-driven
diagnosis.

Got there by chaining four fixes today:

1. **Verifier Chrome-CDP context_options** (commit `5b3c97d`) —
   `_verify_fix` was using plain headless Playwright; USAA's Akamai
   blocked it with ERR_HTTP2_PROTOCOL_ERROR. Now reads username
   from context.json, calls `flow.context_options_for_username()`,
   uses a dedicated `storage/browser-profiles/verify/<carrier>-<sid>`
   profile dir.

2. **Verifier CDP-attach to live repair browser** (commit `8a6f95c`)
   — fresh chromium with new profile dir can't survive USAA's
   OAuth gate even with valid cookies; the live repair browser
   (warm, multi-hour CDP) can. `_verify_fix` now prefers attaching
   to `cdp_endpoint.txt` when present, falls back to fresh-launch
   when not.

3. **carrier-repair skill** (commit `8a6f95c`) — `.claude/skills/
   carrier-repair/SKILL.md` with the diagnostic playbook (step 1
   ground-truth-gathering, step 2 sanity-check, step 3 fresh-Chrome
   OAuth-gate repro, step 4 DONE-criteria, anti-patterns). The
   in-container claude turn 1 dropped from ~9-26 min to ~5 min and
   stopped speculating about extra selector patterns.

4. **Verifier importlib.reload** (commit `61c9a7f`) — _verify_fix
   replays in the same Python process that imported
   `backend.carriers.<carrier>` at FastAPI boot, so sys.modules
   held the OLD UsaaFlow class even after claude patched the file
   on disk. Now reloads the carrier module + registry before
   `get_flow()`. Surfaced by in-container claude turn 3 of session
   `2a208776` — that turn nailed the bug exactly.

5. **`git apply` not `--3way`** (commit `1d8c4e0`) — the side-clone
   for the GitHub push lacks the image-baseline blob the patch
   references; plain apply works as long as main matches the
   image's working-tree state at deploy time.

The demo also required the deliberate `viewDocument-` break to be
on main during the test (commit `36538b3`, reverted by `e061579`).
Without it the push step correctly declines a no-op branch against
main — a safety feature, not a bug.

USAA session lifetime turned out to be ~60-70 min — cookies
refreshed via `run_usaa_once` need to be synced to prod and a test
kicked within minutes for the verifier's live-CDP replay to still
have a valid USAA session. The Chrome MCP browser at
cordwell@gmail.com works to grab MFA codes (Gemini's AI overview
even spells out "USAA one-time security code is XXXXXX" in plain
text).

Prod reverted to clean main, container redeployed at `e061579`.
The `auto-repair/3f12c5d996ca4e70a3b87a3deeb9c3a8` branch is left
on GitHub as visible proof of the working chain.

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
