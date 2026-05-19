"""Auto-repair: spawn `claude` inside the container to diagnose carrier flow
failures.

The orchestrator calls `capture_and_kick()` from its top-level exception
handler. That writes a small failure context + the carrier's most recent
saved storage_state into `storage/repair/<session_id>/` and spawns
`claude -p` as a subprocess.

`cadence_loop()` is a long-running asyncio task registered in `main.py`. It
resumes any active repair sessions every 5 minutes via `claude --resume`
until each writes a `STATUS` file (DONE / NEED_HUMAN) or the wall-time
limit is hit.

Kill switch: set `REPAIR_ENABLED=false` to skip both new kicks and resumes.

Per-carrier dedup: only one active claude per carrier at a time — additional
failures for the same carrier are logged but do not spawn another claude.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import auto_repair_patches, repair_browser, storage

log = logging.getLogger(__name__)

REPAIR_ROOT = Path("storage/repair")
PROMPT_PATH = Path(__file__).parent / "repair_prompt.md"

# Public landing URLs per carrier — the repair browser navigates here so
# claude lands on the carrier site with cookies loaded. Claude can navigate
# elsewhere via the saved storage_state mode or by driving the CDP browser
# directly. about:blank fallback for carriers we don't have a default for.
INITIAL_URLS = {
    "mercury": "https://www.mercuryinsurance.com/",
    "usaa": "https://www.usaa.com/",
    "geico": "https://ecams.geico.com/",
    "progressive": "https://account.progressive.com/",
    "allstate": "https://myaccount.allstate.com/",
    "state_farm": "https://www.statefarm.com/",
}

MAX_REPAIR_WALL_SECONDS = int(os.environ.get("REPAIR_MAX_WALL_SECONDS", "18000"))
RESUME_INTERVAL_SECONDS = int(os.environ.get("REPAIR_RESUME_INTERVAL_SECONDS", "300"))
PER_TURN_TIMEOUT_SECONDS = int(
    os.environ.get("REPAIR_PER_TURN_TIMEOUT_SECONDS", "300")
)
# Verification budget per attempt. We re-run the carrier flow against the
# saved storage_state once claude declares DONE to confirm the fix actually
# repaired the failing step rather than being a confident-sounding no-op.
VERIFY_TIMEOUT_SECONDS = int(os.environ.get("REPAIR_VERIFY_TIMEOUT_SECONDS", "90"))
# stream-json lines can occasionally exceed asyncio's default 64 KB
# StreamReader limit (large tool_result blobs with DOM dumps). Raise to 10 MB.
STREAM_LINE_LIMIT = 10 * 1024 * 1024
# How long to keep storage/repair/<sid>/ and storage/debug/<carrier>/<file>
# artifacts before garbage-collecting them. Default: 7 days. Cleanup runs
# once at controller startup and then every CLEANUP_INTERVAL_SECONDS.
ARTIFACT_TTL_SECONDS = int(
    os.environ.get("REPAIR_ARTIFACT_TTL_SECONDS", str(7 * 86400))
)
CLEANUP_INTERVAL_SECONDS = int(
    os.environ.get("REPAIR_CLEANUP_INTERVAL_SECONDS", "3600")
)
DEBUG_ROOT = Path("storage/debug")

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Carriers that should get a headed (xvfb-wrapped) repair browser. Akamai-
# protected carriers like USAA fingerprint headless chromium even after
# cookies are loaded. Override via REPAIR_HEADED_CARRIERS env var (CSV).
HEADED_CARRIERS = {
    c.strip().lower()
    for c in os.environ.get("REPAIR_HEADED_CARRIERS", "usaa").split(",")
    if c.strip()
}

_active: dict[str, dict] = {}
_lock = asyncio.Lock()
_inflight_tasks: set[asyncio.Task] = set()


def _track_task(task: asyncio.Task) -> None:
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)


def is_enabled() -> bool:
    # Default OFF. Production opts in via REPAIR_ENABLED=true in
    # docker-compose.prod.yml. Keeps tests, local dev, and any environment
    # that doesn't explicitly want auto-repair from spawning claude subprocesses.
    return os.environ.get("REPAIR_ENABLED", "").lower() in ("true", "1", "yes")


async def capture_and_kick(
    session_id: str,
    carrier: str,
    username: str,
    exception: BaseException,
    step: str = "unknown",
    *,
    kick_reason: str = "orchestrator_error",
    extra_context: dict | None = None,
) -> bool:
    """Write failure context and (if not deduped) spawn claude -p.

    kick_reason: 'orchestrator_error' (default — the carrier flow itself
    threw) or 'user_rejected' (user clicked "wrong documents" on docs they
    received successfully).
    extra_context: optional dict merged into context.json — e.g., the
    feedback-recovery path passes the prior pdf_analysis here so claude
    knows what was rejected and why.
    """
    if not is_enabled():
        log.info(
            "capture_and_kick skipped: REPAIR_ENABLED not truthy "
            "(session=%s carrier=%s exception=%s)",
            session_id, carrier, exception.__class__.__name__,
        )
        return False

    log.info(
        "capture_and_kick starting session=%s carrier=%s step=%s exception=%s",
        session_id, carrier, step, repr(exception)[:200],
    )

    try:
        out_dir = REPAIR_ROOT / session_id
        out_dir.mkdir(parents=True, exist_ok=True)

        saved_state = None
        try:
            saved_state = storage.load(carrier, username)
            if saved_state is not None:
                (out_dir / "auth_state.json").write_text(json.dumps(saved_state))
                log.info(
                    "capture_and_kick: copied saved storage_state "
                    "session=%s carrier=%s cookies=%d",
                    session_id, carrier,
                    len(saved_state.get("cookies") or []),
                )
            else:
                log.info(
                    "capture_and_kick: no saved storage_state on disk "
                    "session=%s carrier=%s username_hash=%s",
                    session_id, carrier, storage.user_hash(username),
                )
        except Exception as cap_err:  # noqa: BLE001
            log.warning("could not copy saved storage_state: %s", cap_err)

        if saved_state is not None:
            headed = carrier in HEADED_CARRIERS
            try:
                cdp_endpoint = await repair_browser.spawn(
                    session_id,
                    carrier,
                    saved_state,
                    INITIAL_URLS.get(carrier),
                    headed=headed,
                    username=username,
                )
                (out_dir / "cdp_endpoint.txt").write_text(cdp_endpoint + "\n")
            except Exception as br_err:  # noqa: BLE001
                log.warning(
                    "repair browser spawn failed (carrier=%s headed=%s): %s",
                    carrier, headed, br_err,
                )
        else:
            log.info(
                "capture_and_kick: skipping repair browser spawn (no auth_state) "
                "session=%s carrier=%s",
                session_id, carrier,
            )

        context_obj = {
            "session_id": session_id,
            "carrier": carrier,
            "username": username,
            "step": step,
            "kick_reason": kick_reason,
            "exception": f"{type(exception).__name__}: {exception}",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if extra_context:
            context_obj.update(extra_context)
        (out_dir / "context.json").write_text(json.dumps(context_obj, indent=2))
    except Exception as cap_err:  # noqa: BLE001
        log.warning("auto-repair context capture failed: %s", cap_err)

    return await _kick(session_id, carrier)


async def _kick(session_id: str, carrier: str) -> bool:
    async with _lock:
        if carrier in _active:
            log.info(
                "auto-repair already active for %s (session %s); "
                "folding new failure %s",
                carrier,
                _active[carrier]["session_id"],
                session_id,
            )
            return False
        _active[carrier] = {
            "session_id": session_id,
            "claude_session_id": None,
            "started_at": time.time(),
            "last_turn_at": time.time(),
            "turns": 0,
        }

    _track_task(asyncio.create_task(_run_first_turn(session_id, carrier)))
    return True


async def _run_first_turn(session_id: str, carrier: str) -> None:
    if not PROMPT_PATH.exists():
        log.warning(
            "repair prompt missing at %s; cannot run repair", PROMPT_PATH
        )
        async with _lock:
            _active.pop(carrier, None)
        return
    prompt_template = PROMPT_PATH.read_text()
    prompt = (
        f"{prompt_template}\n\n---\n\n"
        f"Session ID: {session_id}\nCarrier: {carrier}\n\n"
        "Begin work."
    )
    on_chunk = _make_chunk_publisher(session_id, turn=1)
    try:
        result = await _run_claude(
            prompt,
            resume_session_id=None,
            session_id=session_id,
            carrier=carrier,
            turn_number=1,
            on_chunk=on_chunk,
        )
        async with _lock:
            if carrier in _active:
                _active[carrier]["claude_session_id"] = result.get("session_id")
                _active[carrier]["last_turn_at"] = time.time()
                _active[carrier]["turns"] = 1
    except Exception as e:  # noqa: BLE001
        log.exception("auto-repair first turn for %s failed: %s", carrier, e)
    # Always check STATUS — claude may have written it even if our reader
    # died (e.g. stream-json line longer than buffer limit).
    try:
        done = await _check_done(session_id, carrier)
    except Exception as e:  # noqa: BLE001
        log.warning("_check_done after first turn raised: %s", e)
        done = False
    if not done:
        async with _lock:
            info = _active.get(carrier)
            if (
                info is not None
                and info["session_id"] == session_id
                and not info.get("claude_session_id")
            ):
                log.warning(
                    "first turn ended without claude_session_id and no "
                    "terminal STATUS — popping carrier=%s session=%s",
                    carrier, session_id,
                )
                _active.pop(carrier, None)


def _make_chunk_publisher(
    session_id: str, *, turn: int
) -> Callable[[dict], None]:
    """Return an on_chunk callback that translates raw stream-json events
    into display chunks and pushes them to the session's SSE subscribers.

    Imported lazily so this module stays importable without main.py wiring."""
    from .session_manager import manager

    def _publish(event: dict) -> None:
        for chunk in _translate_stream_event(event, turn=turn):
            try:
                manager.publish_repair_log(session_id, chunk)
            except Exception as e:  # noqa: BLE001
                log.debug("publish_repair_log dropped: %s", e)

    return _publish


def _translate_stream_event(event: dict, *, turn: int) -> list[dict]:
    """Map a stream-json line to zero or more display chunks.

    Assistant messages routinely contain [text, tool_use, text] sequences;
    returning a list (not a single chunk) avoids dropping later blocks.
    """
    out: list[dict] = []
    evt_type = event.get("type")
    if evt_type == "assistant":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                out.append({"turn": turn, "kind": "text", "text": text})
            elif kind == "tool_use":
                tool = block.get("name") or "tool"
                inp = block.get("input") or {}
                out.append(
                    {
                        "turn": turn,
                        "kind": "tool_use",
                        "tool": tool,
                        "input_preview": _preview_dict(inp),
                    }
                )
    elif evt_type == "user":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if block.get("type") != "tool_result":
                continue
            text = _stringify_tool_result(block.get("content"))
            if not text:
                continue
            out.append(
                {
                    "turn": turn,
                    "kind": "tool_result",
                    "text_preview": text[:600],
                }
            )
    elif evt_type == "result":
        text = (event.get("result") or "").strip()
        if text:
            out.append({"turn": turn, "kind": "turn_end", "text": text[:600]})
    return out


def _stringify_tool_result(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for piece in content:
            if isinstance(piece, dict) and piece.get("type") == "text":
                parts.append(piece.get("text") or "")
        return "\n".join(p for p in parts if p).strip()
    return ""


def _preview_dict(d: dict) -> str:
    try:
        s = json.dumps(d, default=str)
    except (TypeError, ValueError):
        s = str(d)
    return s[:400]


async def _run_claude(
    prompt: str,
    resume_session_id: str | None,
    session_id: str,
    carrier: str,
    turn_number: int,
    on_chunk: Callable[[dict], None] | None = None,
) -> dict:
    """Spawn claude -p in stream-json mode and persist per-turn transcripts.

    Side effects (under storage/repair/<session_id>/turns/<turn_number>/):
      - stream.jsonl  — raw line stream from claude
      - stderr.txt    — full stderr after the process exits
      - meta.json     — turn metadata (cost, duration, denials, etc.)
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        *ALLOWED_TOOLS,
    ]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])

    turn_dir = REPAIR_ROOT / session_id / "turns" / f"{turn_number:03d}"
    turn_dir.mkdir(parents=True, exist_ok=True)
    stream_path = turn_dir / "stream.jsonl"
    started_at = time.time()
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))

    log.info(
        "claude turn starting carrier=%s session=%s turn=%d resume=%s "
        "prompt_bytes=%d timeout=%ds turn_dir=%s",
        carrier, session_id, turn_number,
        resume_session_id or "<none>",
        len(prompt), PER_TURN_TIMEOUT_SECONDS, turn_dir,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LINE_LIMIT,
    )

    final_result: dict | None = None
    fallback_session_id: str | None = None

    async def _read_stdout() -> None:
        nonlocal final_result, fallback_session_id
        assert proc.stdout is not None
        try:
            stream_file = stream_path.open("ab")
        except Exception as e:  # noqa: BLE001
            log.warning("could not open stream.jsonl for write: %s", e)
            stream_file = None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError as e:
                    # Line longer than StreamReader limit — drain the
                    # rest of the chunk by reading raw bytes and skip
                    # past the next newline so we can resume on the
                    # following line instead of crashing the reader.
                    log.warning(
                        "stream readline exceeded limit, skipping line: %s", e
                    )
                    try:
                        await proc.stdout.read(STREAM_LINE_LIMIT)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if not line:
                    return
                if stream_file is not None:
                    try:
                        stream_file.write(line)
                        stream_file.flush()
                    except Exception:  # noqa: BLE001
                        pass
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue
                evt_type = event.get("type")
                if (
                    evt_type == "system"
                    and event.get("subtype") == "init"
                    and event.get("session_id")
                ):
                    fallback_session_id = event["session_id"]
                elif evt_type == "result":
                    final_result = event
                if on_chunk is not None:
                    try:
                        on_chunk(event)
                    except Exception as e:  # noqa: BLE001
                        log.debug("on_chunk raised: %s", e)
        finally:
            if stream_file is not None:
                try:
                    stream_file.close()
                except Exception:  # noqa: BLE001
                    pass

    read_task = asyncio.create_task(_read_stdout())
    timed_out = False
    try:
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=PER_TURN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        # Drain remaining stdout. readline returns empty when the pipe closes
        # — no need to cap with a timeout, which would silently lose buffered
        # events.
        await read_task
    except asyncio.CancelledError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        read_task.cancel()
        await _write_turn_meta(
            turn_dir, cmd, prompt, resume_session_id, session_id, carrier,
            turn_number, started_iso, started_at, b"",
            exit_code=proc.returncode, final_result=None,
            fallback_session_id=fallback_session_id, note="cancelled",
        )
        raise

    stderr_bytes = b""
    if proc.stderr is not None:
        try:
            stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
    try:
        (turn_dir / "stderr.txt").write_bytes(stderr_bytes)
    except Exception as e:  # noqa: BLE001
        log.warning("could not write turn stderr: %s", e)

    await _write_turn_meta(
        turn_dir, cmd, prompt, resume_session_id, session_id, carrier,
        turn_number, started_iso, started_at, stderr_bytes,
        exit_code=proc.returncode, final_result=final_result,
        fallback_session_id=fallback_session_id,
        note="timeout" if timed_out else None,
    )

    if timed_out:
        raise RuntimeError(
            f"claude subprocess exceeded {PER_TURN_TIMEOUT_SECONDS}s"
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr_bytes.decode()[:500]}"
        )

    if final_result is not None:
        return final_result
    return {"session_id": fallback_session_id, "result": ""}


async def _write_turn_meta(
    turn_dir: Path,
    cmd: list[str],
    prompt: str,
    resume_session_id: str | None,
    session_id: str,
    carrier: str,
    turn_number: int,
    started_iso: str,
    started_at: float,
    stderr_bytes: bytes,
    exit_code: int | None,
    final_result: dict | None,
    fallback_session_id: str | None,
    note: str | None = None,
) -> None:
    ended_at = time.time()
    ended_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_at))
    duration_ms = int((ended_at - started_at) * 1000)
    meta: dict = {
        "turn": turn_number,
        "carrier": carrier,
        "session_id": session_id,
        "resume_session_id": resume_session_id,
        "prompt_size_bytes": len(prompt),
        "prompt_preview": prompt[:500],
        "command_argv_preview": cmd[:3] + ["<prompt elided>"] + cmd[3:][:8],
        "started_at": started_iso,
        "ended_at": ended_iso,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "stderr_bytes": len(stderr_bytes),
        "stderr_preview": stderr_bytes.decode(errors="replace")[:500],
        "note": note,
        "fallback_session_id": fallback_session_id,
    }
    cost = 0.0
    denials_count = 0
    if final_result:
        denials = final_result.get("permission_denials") or []
        denials_count = len(denials)
        cost = float(final_result.get("total_cost_usd") or 0.0)
        meta.update({
            "claude_session_id": final_result.get("session_id"),
            "num_turns": final_result.get("num_turns"),
            "cost_usd": cost,
            "duration_api_ms": final_result.get("duration_api_ms"),
            "stop_reason": final_result.get("stop_reason"),
            "service_tier": final_result.get("service_tier"),
            "is_error": final_result.get("is_error"),
            "result_preview": (final_result.get("result") or "")[:500],
            "permission_denials": [
                {
                    "tool": d.get("tool_name"),
                    "input_keys": list((d.get("tool_input") or {}).keys())[:5],
                }
                for d in denials
            ],
            "model_usage_keys": list((final_result.get("modelUsage") or {}).keys()),
        })

    try:
        (turn_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    except Exception as e:  # noqa: BLE001
        log.warning("could not write turn meta to %s: %s", turn_dir, e)

    log.info(
        "claude turn done carrier=%s session=%s turn=%d exit=%s duration=%dms "
        "cost=$%.4f num_turns=%s denials=%d stop=%s note=%s result=%r",
        carrier, session_id, turn_number,
        exit_code, duration_ms, cost,
        meta.get("num_turns"), denials_count,
        meta.get("stop_reason"), note or "ok",
        (meta.get("result_preview") or "")[:120],
    )


async def _verify_fix(
    session_id: str, carrier: str
) -> tuple[bool, str | None]:
    """Verify a claude-claimed fix actually works.

    Two checks:
      1. `git diff backend/carriers/<carrier>.py` is non-empty (claude wrote
         something).
      2. The carrier's `fetch_documents` succeeds against a fresh browser
         loaded with the saved storage_state (the same cookies that were
         live when the original failure happened).

    Returns (passed, reason). On pass, reason is None.
    """
    out_dir = REPAIR_ROOT / session_id

    # 1) git diff non-empty
    diff_target = f"backend/carriers/{carrier}.py"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", "/app", "diff", "--", diff_target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        if not stdout.strip():
            return (
                False,
                f"git diff for {diff_target} is empty — claude declared DONE "
                "but did not modify the adapter",
            )
        log.info(
            "verify carrier=%s session=%s git-diff bytes=%d",
            carrier, session_id, len(stdout),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("verify: git diff check raised: %s", e)
        # Don't fail solely on git diff infrastructure error; continue.

    # 2) Replay fetch_documents against the saved storage_state
    auth_file = out_dir / "auth_state.json"
    if not auth_file.exists():
        return (
            False,
            "no auth_state.json on disk — cannot replay carrier flow to "
            "verify the fix",
        )
    try:
        from .carriers.registry import get_flow
        from .models import Carrier
        from .playwright_runner import http_from_context, runner as pw_runner

        storage_state = json.loads(auth_file.read_text())
        flow = get_flow(Carrier(carrier))
    except Exception as e:  # noqa: BLE001
        return False, f"verify setup failed: {type(e).__name__}: {e}"

    # Build the same context the carrier itself uses, so the verify browser
    # gets the carrier's stealth init script / Chrome CDP launch / UA /
    # viewport. Without this, USAA + other Akamai-protected sites reject the
    # verify browser as bot-shaped — claude's fix would look broken even
    # when it isn't (see session 71a7e651 STATUS dump). Use a dedicated
    # verify profile dir to avoid conflicting with the live orchestrator
    # or the long-lived repair browser, which may both be open against the
    # carrier's real profile at the same time.
    ctx_opts: dict = {}
    try:
        context_data = json.loads((out_dir / "context.json").read_text())
        username = context_data.get("username")
    except Exception:  # noqa: BLE001
        username = None
    try:
        if username and hasattr(flow, "context_options_for_username"):
            ctx_opts = dict(flow.context_options_for_username(username))
        else:
            ctx_opts = dict(flow.context_options())
    except Exception as e:  # noqa: BLE001
        log.warning(
            "verify: building context_options failed for %s: %s — falling "
            "back to bare context",
            carrier, e,
        )
        ctx_opts = {}
    # Don't reuse the live carrier's Chrome profile — concurrent Chromium
    # processes can't share a profile dir, and the orchestrator / repair
    # browser may still hold it.
    verify_profile = Path("storage/browser-profiles/verify") / f"{carrier}-{session_id[:12]}"
    verify_profile.mkdir(parents=True, exist_ok=True)
    ctx_opts["_chrome_profile_dir"] = str(verify_profile)
    # _initial_url is only meaningful when login_context spawns Chrome to
    # land on a login page; the verify path navigates via fetch_documents.
    ctx_opts.pop("_initial_url", None)

    # Prefer attaching to the live repair browser via CDP when available.
    # USAA's Akamai + OAuth gate (and similar carrier defenses) rejects
    # fresh-chrome cold starts even when given the saved cookies, because
    # the bot-management stack tracks more state than cookies alone (TLS
    # fingerprint, accumulated cache, JS challenge history). The live
    # repair browser has accumulated that state across the orchestrator's
    # quick-path + claude's probes; reusing it is the only way the verify
    # replay can pass on those carriers.
    cdp_endpoint: str | None = None
    cdp_file = out_dir / "cdp_endpoint.txt"
    if cdp_file.exists():
        try:
            cdp_endpoint = cdp_file.read_text().strip() or None
        except OSError:
            cdp_endpoint = None

    async def _run_replay(ctx) -> tuple[list, dict] | None:
        page = await ctx.new_page()
        try:
            http = await http_from_context(ctx)
            try:
                return await asyncio.wait_for(
                    flow.fetch_documents(page, http, ctx),
                    timeout=VERIFY_TIMEOUT_SECONDS,
                )
            finally:
                await http.aclose()
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        docs: list = []
        if cdp_endpoint:
            from playwright.async_api import async_playwright

            log.info(
                "verify: attaching to live repair browser carrier=%s "
                "session=%s endpoint=%s",
                carrier, session_id, cdp_endpoint,
            )
            async with async_playwright() as pw:
                browser = await asyncio.wait_for(
                    pw.chromium.connect_over_cdp(cdp_endpoint),
                    timeout=10.0,
                )
                try:
                    if not browser.contexts:
                        return False, "CDP browser has no context"
                    ctx = browser.contexts[0]
                    result = await _run_replay(ctx)
                    docs, _doc_bytes = result if result else ([], {})
                finally:
                    # Disconnects the CDP handle without terminating the
                    # chromium process (which belongs to repair_browser).
                    await browser.close()
        else:
            async with pw_runner.new_context(
                storage_state=storage_state, **ctx_opts
            ) as ctx:
                result = await _run_replay(ctx)
                docs, _doc_bytes = result if result else ([], {})
        if not docs:
            return False, "fetch_documents returned 0 documents"
        log.info(
            "verify PASSED carrier=%s session=%s docs=%d",
            carrier, session_id, len(docs),
        )
        return True, None
    except asyncio.TimeoutError:
        return (
            False,
            f"fetch_documents did not return within "
            f"{VERIFY_TIMEOUT_SECONDS}s",
        )
    except Exception as e:  # noqa: BLE001
        return False, f"fetch_documents raised {type(e).__name__}: {e}"


async def _persist_and_push_patch(
    session_id: str, carrier: str, status_body: str
) -> None:
    """Capture the verified patch as a full working-tree diff, persist it
    under storage/patches/ for restart-reapply, then force-push it to the
    ``auto-repair/<session_id>`` branch on GitHub.

    Best-effort: any failure here is logged but doesn't undo the verifier's
    accept — the in-container code is already running with the fix.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", "/app", "diff", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "persist-and-push: capturing diff failed carrier=%s session=%s: %s",
            carrier, session_id, e,
        )
        return
    patch_content = stdout.decode("utf-8", "replace")
    if not patch_content.strip():
        log.warning(
            "persist-and-push: empty diff carrier=%s session=%s "
            "(verifier accepted but working tree is clean — skipping)",
            carrier, session_id,
        )
        return

    summary = (status_body or "").strip()[:4000]
    try:
        path = auto_repair_patches.record(
            session_id, carrier, patch_content, summary
        )
        log.info(
            "persist-and-push: patch recorded carrier=%s session=%s path=%s",
            carrier, session_id, path,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "persist-and-push: record() raised carrier=%s session=%s: %s",
            carrier, session_id, e,
        )

    try:
        ok, msg = await auto_repair_patches.push_to_branch(
            session_id, carrier, patch_content, summary
        )
        if ok:
            log.info(
                "persist-and-push: branch published carrier=%s session=%s "
                "branch=%s",
                carrier, session_id, msg,
            )
        else:
            log.warning(
                "persist-and-push: GitHub push failed carrier=%s session=%s "
                "reason=%s",
                carrier, session_id, msg,
            )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "persist-and-push: push_to_branch raised carrier=%s session=%s: %s",
            carrier, session_id, e,
        )


async def _on_verification_failed(
    session_id: str, carrier: str, reason: str, rejected_status_body: str
) -> None:
    """Reject the STATUS=DONE: move it aside, append the reason to
    context.json, leave the session active so the cadence loop resumes
    claude with the new context."""
    log.warning(
        "verification REJECTED carrier=%s session=%s reason=%s",
        carrier, session_id, reason,
    )
    out_dir = REPAIR_ROOT / session_id
    try:
        rejected_path = (
            out_dir / f"STATUS_REJECTED_turn{int(time.time())}"
        )
        (out_dir / "STATUS").rename(rejected_path)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("could not move rejected STATUS aside: %s", e)
        try:
            (out_dir / "STATUS").unlink()
        except Exception:  # noqa: BLE001
            pass

    ctx_path = out_dir / "context.json"
    try:
        ctx_obj = json.loads(ctx_path.read_text())
    except Exception:  # noqa: BLE001
        ctx_obj = {}
    history = ctx_obj.get("verification_failures") or []
    history.append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": reason,
        "rejected_status_first_line": (
            rejected_status_body.split("\n", 1)[0][:240]
        ),
    })
    ctx_obj["verification_failures"] = history
    try:
        ctx_path.write_text(json.dumps(ctx_obj, indent=2))
    except Exception as e:  # noqa: BLE001
        log.warning("could not append verification failure to context: %s", e)


async def _check_done(session_id: str, carrier: str) -> bool:
    # Poll for early deliveries on every check — claude may drop verified
    # PDFs in delivered/ before writing STATUS, and we want them to reach the
    # user the moment they hit disk.
    try:
        from . import repair_deliver

        repair_deliver.deliver_if_present(session_id)
    except Exception as e:  # noqa: BLE001
        log.debug("early deliver poll failed: %s", e)

    status_file = REPAIR_ROOT / session_id / "STATUS"
    if not status_file.exists():
        return False
    body = status_file.read_text()
    first_line = body.split("\n", 1)[0].strip()
    normalized = first_line.lstrip("#`* ").upper()
    if not (normalized.startswith("DONE") or normalized.startswith("NEED_HUMAN")):
        return False

    verdict = "DONE" if normalized.startswith("DONE") else "NEED_HUMAN"

    if verdict == "DONE":
        log.info(
            "verifying DONE claim carrier=%s session=%s first_line=%r",
            carrier, session_id, first_line[:200],
        )
        passed, reason = await _verify_fix(session_id, carrier)
        if not passed:
            await _on_verification_failed(session_id, carrier, reason or "?", body)
            return False  # keep the session active; cadence will resume claude
        await _persist_and_push_patch(session_id, carrier, body)

    # Ship any PDFs claude downloaded into delivered/ to the user's session
    # BEFORE we tear down the carrier slot. Per the user's spec: poll on
    # every _check_done (not just on STATUS landing) so docs flow as soon as
    # they hit disk.
    try:
        from . import repair_deliver

        repair_deliver.deliver_if_present(session_id)
    except Exception as e:  # noqa: BLE001
        log.warning("repair_deliver failed: %s", e)

    async with _lock:
        _active.pop(carrier, None)
    await repair_browser.cleanup(carrier)
    log.info(
        "STATUS complete carrier=%s session=%s verdict=%s first_line=%r "
        "body_bytes=%d",
        carrier, session_id, verdict, first_line[:200], len(body),
    )
    log.info(
        "STATUS full body carrier=%s session=%s:\n%s",
        carrier, session_id, body[:4000],
    )
    try:
        from .session_manager import manager

        manager.publish_repair_done(
            session_id, verdict=verdict, first_line=first_line[:240]
        )
    except Exception as e:  # noqa: BLE001
        log.debug("publish_repair_done dropped: %s", e)
    return True


async def cadence_loop() -> None:
    """Long-running background task. Resumes active repairs every 5 minutes
    and garbage-collects aged repair / debug artifacts."""
    log.info(
        "auto-repair cadence loop starting (interval=%ds, max wall=%ds, "
        "artifact ttl=%ds)",
        RESUME_INTERVAL_SECONDS,
        MAX_REPAIR_WALL_SECONDS,
        ARTIFACT_TTL_SECONDS,
    )
    try:
        cleanup_old_artifacts()
    except Exception as e:  # noqa: BLE001
        log.warning("startup artifact cleanup raised: %s", e)
    last_cleanup = time.time()
    while True:
        try:
            await asyncio.sleep(RESUME_INTERVAL_SECONDS)
            if not is_enabled():
                continue
            await _cadence_tick()
            if time.time() - last_cleanup > CLEANUP_INTERVAL_SECONDS:
                try:
                    cleanup_old_artifacts()
                except Exception as e:  # noqa: BLE001
                    log.warning("periodic artifact cleanup raised: %s", e)
                last_cleanup = time.time()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.exception("auto-repair cadence tick crashed: %s", e)


def cleanup_old_artifacts() -> None:
    """Prune storage/repair/<sid>/ and storage/debug/<carrier>/ entries
    whose mtime is older than ARTIFACT_TTL_SECONDS.

    Active repair sessions (carriers currently in `_active`) are
    skipped — they may have recent mtimes but we still keep them.
    """
    cutoff = time.time() - ARTIFACT_TTL_SECONDS
    active_session_ids = {info["session_id"] for info in _active.values()}
    pruned = 0
    kept_active = 0
    for root in (REPAIR_ROOT, DEBUG_ROOT):
        if not root.exists():
            continue
        for entry in root.iterdir():
            try:
                if entry.is_dir() and entry.name in active_session_ids:
                    kept_active += 1
                    continue
                if entry.stat().st_mtime >= cutoff:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                pruned += 1
            except Exception as e:  # noqa: BLE001
                log.warning("artifact cleanup: failed to prune %s: %s", entry, e)
    if pruned or kept_active:
        log.info(
            "artifact cleanup: pruned=%d kept_active=%d ttl=%ds",
            pruned, kept_active, ARTIFACT_TTL_SECONDS,
        )


async def _cadence_tick() -> None:
    async with _lock:
        snapshot = dict(_active)
    for carrier, info in snapshot.items():
        if await _check_done(info["session_id"], carrier):
            continue
        if time.time() - info["started_at"] > MAX_REPAIR_WALL_SECONDS:
            await _timeout_repair(info["session_id"], carrier)
            continue
        await _resume_one(info["session_id"], carrier, info["claude_session_id"])


async def _resume_one(
    session_id: str, carrier: str, claude_session_id: str | None
) -> None:
    if not claude_session_id:
        log.warning(
            "no claude session id recorded for %s repair; skipping resume",
            carrier,
        )
        return
    resume_prompt = (
        "Continue. **Re-read context.json first** — it may have a "
        "`verification_failures` array appended since your last turn. "
        "That array means a previous STATUS=DONE you wrote was rejected "
        "because the controller replayed the failing carrier step and it "
        "still didn't work. The most recent entry tells you exactly which "
        "error came back. Use that as the real signal for what's broken — "
        "the original `exception` field in context.json may be a downstream "
        "symptom, not the root cause. "
        "Status check: what have you found? What's next? "
        "If you've truly finished, write a fresh STATUS file."
    )
    async with _lock:
        next_turn = (
            (_active[carrier]["turns"] + 1) if carrier in _active else 0
        )
    on_chunk = _make_chunk_publisher(session_id, turn=next_turn)
    try:
        result = await _run_claude(
            resume_prompt,
            resume_session_id=claude_session_id,
            session_id=session_id,
            carrier=carrier,
            turn_number=next_turn,
            on_chunk=on_chunk,
        )
        async with _lock:
            if carrier in _active:
                _active[carrier]["last_turn_at"] = time.time()
                _active[carrier]["turns"] += 1
                turns = _active[carrier]["turns"]
            else:
                turns = -1
        log.info(
            "auto-repair resume turn %d for %s: %s",
            turns,
            carrier,
            (result.get("result") or "")[:240],
        )
        await _check_done(session_id, carrier)
    except Exception as e:  # noqa: BLE001
        log.exception("auto-repair resume for %s failed: %s", carrier, e)


async def _timeout_repair(session_id: str, carrier: str) -> None:
    log.warning(
        "auto-repair timeout (>%ds) for %s session %s",
        MAX_REPAIR_WALL_SECONDS,
        carrier,
        session_id,
    )
    try:
        status_file = REPAIR_ROOT / session_id / "STATUS"
        if not status_file.exists():
            status_file.write_text(
                f"NEED_HUMAN: auto-repair exceeded "
                f"{MAX_REPAIR_WALL_SECONDS}s wall time\n"
            )
    except Exception as e:  # noqa: BLE001
        log.warning("failed to write timeout STATUS: %s", e)
    async with _lock:
        _active.pop(carrier, None)
    await repair_browser.cleanup(carrier)


def active_repairs_snapshot() -> dict[str, dict]:
    """Read-only snapshot for diagnostics (e.g. /api/dev endpoint)."""
    return {k: dict(v) for k, v in _active.items()}


async def shutdown() -> None:
    """Cancel any in-flight repair turns and kill any repair browsers.
    Called from main.py lifespan teardown."""
    if _inflight_tasks:
        log.info(
            "auto-repair shutdown: cancelling %d in-flight task(s)",
            len(_inflight_tasks),
        )
        for task in list(_inflight_tasks):
            task.cancel()
        await asyncio.gather(*_inflight_tasks, return_exceptions=True)
    await repair_browser.cleanup_all()
