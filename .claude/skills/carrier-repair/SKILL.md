---
name: carrier-repair
description: Diagnostic playbook for fixing carrier-adapter failures in the infer-takehome auto-repair loop. Invoke at the start of every repair turn before touching backend/carriers/<carrier>.py.
---

# Carrier-adapter repair playbook

You are an auto-repair turn for a carrier flow that just failed. The
controller will replay `flow.fetch_documents()` against a FRESH Chrome
context loaded with the saved `auth_state.json` and the carrier's
`context_options`. **Your patch only earns STATUS=DONE if THAT path
returns ≥1 document.** Delivering PDFs via `delivered/` is great for
the user but does not pass verification.

## Step 1 — gather ground truth, in this order

1. **`storage/logs/app.log`** — grep for the carrier name and your
   `SESSION_ID`. The exception in `context.json` is usually a
   downstream symptom. The real failure is one to several `WARNING`
   lines earlier. Read those first.

2. **`storage/debug/<carrier>/`** — every debug DOM dump the adapter
   wrote at failure time. These are the literal page state the broken
   code saw — more reliable than re-probing now (the live page may
   have moved on; cookies may have aged out). Extract every
   document-action element:

       grep -ohE '<button[^>]+data-testid="[^"]+"[^>]*>' \
         storage/debug/<carrier>/*.html | sort -u
       grep -ohE 'class="[^"]*document[^"]*"' \
         storage/debug/<carrier>/*.html | sort -u

   The `data-testid`, classes, and ARIA labels you see here are
   GROUND TRUTH for what the live carrier emits.

3. **Compare against the broken code's selectors**:

       grep -nE "data-testid\^=|page\.locator|aria-label" \
         backend/carriers/<carrier>.py

   Make a two-column list — "what the code looks for" vs "what the
   page emits." The bug is almost always one of these patterns:

   - **Character-level typo** — adjacent letters swapped, `view`
     vs `read`, camelCase vs kebab-case, plural vs singular. Example:
     code expects `viewDocument-` but page emits `readDocument-`.
     **The fix is exactly that 4-character flip — nothing else.**
     Do not speculate about additional patterns; do not add fallback
     selectors. Match what the dump shows.
   - **CSS class rename** — carrier shipped new UI, the old class
     name is gone. New name visible in the dump.
   - **URL move** — `DOCS_URL_CANDIDATES` is stale; the new path is
     visible in `app.log` (e.g., a 301/302 Location header was logged
     earlier in the session).
   - **Auth wall** — every authenticated URL 302s to a login page;
     the dump title is "Page Not Found" or "Member Account Login".
     This is NOT a code bug; the saved cookies have expired.
     **Write `STATUS: NEED_HUMAN` with reason "cookies expired —
     needs fresh login from a working path".** Do not patch.

## Step 2 — sanity-check before patching

If you found a typo or a clear DOM-vs-code mismatch in step 1, that
IS the bug. Patch precisely that — don't enrich the selector with
speculative alternatives "in case the page is mixed-version." The
verifier's replay is deterministic; the patch should be too.

If your candidate fix is "add `button.document-actions` because it
might also work" — STOP. Open the dump again and read the actual
button HTML. If the dump shows `data-testid="readDocument-N"` and the
code says `viewDocument-`, the fix is `viewDocument-` →
`readDocument-`. Period.

## Step 3 — fresh-Chrome reality check (USAA / Akamai-gated carriers)

The verifier's replay browser is a brand-new Chrome process with a
fresh profile directory and ONLY the cookies from `auth_state.json`.
For carriers behind Akamai bot-management + OAuth gates (USAA),
cookies alone may not survive the gate — the verifier's fresh chrome
gets bounced to `/my/logon` even though the live repair browser
(warmed, multi-hour CDP-attached) can reach `/my/documents` with the
same cookies.

You can test this in ~2 minutes:

    cat > /tmp/verify_repro.py <<'PY'
    import asyncio, json
    from backend.carriers.usaa import UsaaFlow
    from backend.playwright_runner import runner
    flow = UsaaFlow()
    storage_state = json.loads(open(
      "storage/repair/<SID>/auth_state.json").read())["storage_state"] \
      if "storage_state" in open("storage/repair/<SID>/auth_state.json").read() \
      else json.loads(open("storage/repair/<SID>/auth_state.json").read())
    opts = flow.context_options_for_username("<USERNAME>")
    opts.pop("_initial_url", None)
    opts["_chrome_profile_dir"] = "storage/browser-profiles/verify-repro"
    async def main():
        await runner.start()
        async with runner.new_context(storage_state=storage_state, **opts) as ctx:
            page = await ctx.new_page()
            await page.goto("https://www.usaa.com/my/documents?akredirect=true",
                            timeout=30000)
            await asyncio.sleep(2)
            print("title:", await page.title())
            print("url:", page.url)
        await runner.shutdown()
    asyncio.run(main())
    PY
    uv run python /tmp/verify_repro.py

If `title` is `Member Account Login | USAA` or url contains
`/my/logon`, the cookies are insufficient for fresh chrome and your
patch can't pass the verifier no matter what. In that case:

- **Still deliver PDFs** to `storage/repair/<SID>/delivered/` via
  CDP-attached probes if you can — the user benefits even if the
  verifier rejects.
- **Apply your selector fix** anyway — it's still the right code
  change for future runs once the cookie/profile situation is fixed.
- **Write `STATUS: NEED_HUMAN`** with a specific escalation: the
  verifier needs to attach to the live repair browser via CDP
  (`cdp_endpoint.txt`) instead of launching fresh chrome. Reference
  this exact recommendation so the human can fix `auto_repair.py`.

## Step 4 — declare DONE with specifics

If your fix passes the step 3 reproduction (or the carrier isn't
fresh-chrome-gated), write `STATUS: DONE` with:

- One sentence on the bug found (where it lives, the typo / rename /
  URL move).
- One sentence on the patch (what selector / URL is now correct).
- Confirmation that `fetch_documents` was the actual broken function
  (don't claim DONE if you only patched a helper).

If you're escalating due to a fresh-Chrome OAuth gate, write
`STATUS: NEED_HUMAN: fresh-chrome verifier cannot survive OAuth; needs
CDP attach to live repair browser. <SHORT PROOF>` — short proof is
the `title:` / `url:` output from the step 3 repro.

## Step 5 — what happens after DONE

The controller (`backend/auto_repair.py`):

1. Replays `flow.fetch_documents()` in fresh Chrome with your
   `context_options`. If it returns ≥1 doc, passes; otherwise
   rejects and you'll be resumed with a `verification_failures` entry
   in `context.json`.
2. On verify pass: saves your `git diff HEAD` to
   `storage/patches/` for restart-reapply, AND force-pushes to
   `auto-repair/<session_id>` on GitHub (via the PAT at
   `storage/secrets/github_pat`).
3. On verify reject: renames your STATUS to `STATUS_REJECTED_turn<ts>`,
   appends the rejection reason to `context.json`. The cadence loop
   resumes you in ~5 min.

You see this lifecycle one turn at a time. Don't iterate forever:
after 3 rejection turns without new evidence, write NEED_HUMAN with
your best theory.

## Anti-patterns (don't do these)

- **Speculative selector unions.** `button[data-testid^='X'],
  button.maybe, button[id^='Y']` looks defensive but usually means
  "I didn't actually verify what the page shows." Pick one. Justify
  it against the dump.
- **Patching unrelated subsystems.** A selector bug doesn't need
  changes to `login_context`, `_os_browser_login`, or URL candidates.
  Each touched area must trace to evidence in step 1.
- **Claiming DONE because PDFs reached the user.** That's a
  different success metric (`delivered/`). DONE means
  `fetch_documents` works on a fresh context.
