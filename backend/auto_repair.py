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
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import repair_browser, storage

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

MAX_REPAIR_WALL_SECONDS = int(os.environ.get("REPAIR_MAX_WALL_SECONDS", "1800"))
RESUME_INTERVAL_SECONDS = int(os.environ.get("REPAIR_RESUME_INTERVAL_SECONDS", "300"))
PER_TURN_TIMEOUT_SECONDS = int(
    os.environ.get("REPAIR_PER_TURN_TIMEOUT_SECONDS", "300")
)

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
) -> bool:
    """Write failure context and (if not deduped) spawn claude -p."""
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
            "step": step,
            "exception": f"{type(exception).__name__}: {exception}",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
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
    try:
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
        log.info(
            "auto-repair turn 1 for %s: %s",
            carrier,
            (result.get("result") or "")[:240],
        )
        await _check_done(session_id, carrier)
    except Exception as e:  # noqa: BLE001
        log.exception("auto-repair first turn for %s failed: %s", carrier, e)
        async with _lock:
            _active.pop(carrier, None)


def _make_chunk_publisher(
    session_id: str, *, turn: int
) -> Callable[[dict], None]:
    """Return an on_chunk callback that translates raw stream-json events
    into display chunks and pushes them to the session's SSE subscribers.

    Imported lazily so this module stays importable without main.py wiring."""
    from .session_manager import manager

    def _publish(event: dict) -> None:
        chunk = _translate_stream_event(event, turn=turn)
        if chunk is None:
            return
        try:
            manager.publish_repair_log(session_id, chunk)
        except Exception as e:  # noqa: BLE001
            log.debug("publish_repair_log dropped: %s", e)

    return _publish


def _translate_stream_event(event: dict, *, turn: int) -> dict | None:
    """Map a stream-json line to a display chunk, or None to skip."""
    evt_type = event.get("type")
    if evt_type == "assistant":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                return {"turn": turn, "kind": "text", "text": text}
            if kind == "tool_use":
                tool = block.get("name") or "tool"
                inp = block.get("input") or {}
                preview = _preview_dict(inp)
                return {
                    "turn": turn,
                    "kind": "tool_use",
                    "tool": tool,
                    "input_preview": preview,
                }
    elif evt_type == "user":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                text = _stringify_tool_result(content)
                if not text:
                    continue
                return {
                    "turn": turn,
                    "kind": "tool_result",
                    "text_preview": text[:600],
                }
    elif evt_type == "result":
        text = (event.get("result") or "").strip()
        if not text:
            return None
        return {"turn": turn, "kind": "turn_end", "text": text[:600]}
    return None


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
                line = await proc.stdout.readline()
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
            read_task.cancel()
        # Drain remaining stdout after process exit.
        try:
            await asyncio.wait_for(read_task, timeout=5.0)
        except asyncio.TimeoutError:
            read_task.cancel()
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


async def _check_done(session_id: str, carrier: str) -> bool:
    status_file = REPAIR_ROOT / session_id / "STATUS"
    if not status_file.exists():
        return False
    body = status_file.read_text()
    first_line = body.split("\n", 1)[0].strip()
    normalized = first_line.lstrip("#`* ").upper()
    if normalized.startswith("DONE") or normalized.startswith("NEED_HUMAN"):
        verdict = "DONE" if normalized.startswith("DONE") else "NEED_HUMAN"
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
    return False


async def cadence_loop() -> None:
    """Long-running background task. Resumes active repairs every 5 minutes."""
    log.info(
        "auto-repair cadence loop starting (interval=%ds, max wall=%ds)",
        RESUME_INTERVAL_SECONDS,
        MAX_REPAIR_WALL_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(RESUME_INTERVAL_SECONDS)
            if not is_enabled():
                continue
            await _cadence_tick()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.exception("auto-repair cadence tick crashed: %s", e)


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
        "Continue. Status check: what have you found? What's next? "
        "If you've finished, write the STATUS file now."
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
