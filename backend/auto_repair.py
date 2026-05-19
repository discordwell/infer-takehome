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
        log.info("auto-repair disabled; skipping kick for session %s", session_id)
        return False

    try:
        out_dir = REPAIR_ROOT / session_id
        out_dir.mkdir(parents=True, exist_ok=True)

        saved_state = None
        try:
            saved_state = storage.load(carrier, username)
            if saved_state is not None:
                (out_dir / "auth_state.json").write_text(json.dumps(saved_state))
        except Exception as cap_err:  # noqa: BLE001
            log.warning("could not copy saved storage_state: %s", cap_err)

        if saved_state is not None:
            try:
                cdp_endpoint = await repair_browser.spawn(
                    session_id,
                    carrier,
                    saved_state,
                    INITIAL_URLS.get(carrier),
                )
                (out_dir / "cdp_endpoint.txt").write_text(cdp_endpoint + "\n")
            except Exception as br_err:  # noqa: BLE001
                log.warning(
                    "repair browser spawn for %s failed: %s",
                    carrier,
                    br_err,
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
        result = await _run_claude(prompt, resume_session_id=None)
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


async def _run_claude(prompt: str, resume_session_id: str | None) -> dict:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--allowedTools",
        *ALLOWED_TOOLS,
    ]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=PER_TURN_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"claude subprocess exceeded {PER_TURN_TIMEOUT_SECONDS}s"
        ) from None
    except asyncio.CancelledError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr.decode()[:500]}"
        )
    return json.loads(stdout.decode())


async def _check_done(session_id: str, carrier: str) -> bool:
    status_file = REPAIR_ROOT / session_id / "STATUS"
    if not status_file.exists():
        return False
    first_line = status_file.read_text().split("\n", 1)[0].strip()
    normalized = first_line.lstrip("#`* ").upper()
    if normalized.startswith("DONE") or normalized.startswith("NEED_HUMAN"):
        async with _lock:
            _active.pop(carrier, None)
        await repair_browser.cleanup(carrier)
        log.info(
            "auto-repair complete for %s: %s", carrier, first_line[:200]
        )
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
    try:
        result = await _run_claude(
            resume_prompt, resume_session_id=claude_session_id
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
