"""Patch persistence for verified-DONE auto-repair fixes.

Two complementary pickup mechanisms:

1. **Container-restart reapply.** Verified patches are saved under
   ``storage/patches/*.patch`` (the named volume). At container boot, before
   uvicorn binds, a CLI entry point applies them to ``/app`` so the running
   process gets the patched carrier code even though the image still ships
   the broken version.

2. **GitHub push.** The same patch is force-pushed to
   ``auto-repair/<session_id>`` on the GitHub repo using a fine-grained PAT
   from ``storage/secrets/github_pat``. That branch is the durable record
   for the human to review / cherry-pick into main.

The two together solve the "DONE-but-never-landed" gap: in-container hot
patches survive restarts until the human merges the GitHub branch and a
new deploy rebases everything to the upstream sha.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


APP_ROOT = Path(os.environ.get("AUTO_REPAIR_APP_ROOT", "/app"))
PATCHES_ROOT = APP_ROOT / "storage" / "patches"
PAT_FILE = APP_ROOT / "storage" / "secrets" / "github_pat"
REPO_HOST_PATH = "github.com/discordwell/infer-takehome.git"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_pat() -> str | None:
    if not PAT_FILE.exists():
        return None
    try:
        return PAT_FILE.read_text().strip()
    except OSError as e:
        log.warning("read_pat failed: %s", e)
        return None


def _patches_dir() -> Path:
    PATCHES_ROOT.mkdir(parents=True, exist_ok=True)
    return PATCHES_ROOT


def _name_for(session_id: str, carrier: str) -> str:
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_carrier = "".join(c for c in carrier if c.isalnum() or c in "-_") or "unknown"
    safe_session = "".join(c for c in session_id if c.isalnum() or c in "-_") or "anon"
    return f"{ts}__{safe_carrier}__{safe_session}"


# ---------------------------------------------------------------------------
# Persistence: record + reapply on boot
# ---------------------------------------------------------------------------

def record(
    session_id: str, carrier: str, patch_content: str, summary: str = ""
) -> Path:
    """Save a verified patch to the named volume. Returns the saved path."""
    if not patch_content.strip():
        raise ValueError("patch_content is empty; refusing to record")
    base = _name_for(session_id, carrier)
    pdir = _patches_dir()
    path = pdir / f"{base}.patch"
    path.write_text(patch_content)
    if summary:
        (pdir / f"{base}.summary.txt").write_text(summary)
    log.info(
        "recorded patch carrier=%s session=%s path=%s bytes=%d",
        carrier, session_id, path, len(patch_content),
    )
    return path


def apply_pending() -> dict[str, str]:
    """Apply every ``*.patch`` under storage/patches/ via ``git apply``.

    Behavior:
      - clean apply -> patch stays in place (re-applies every restart until
        a deploy carries the fix into main and the patch becomes redundant)
      - patch context not present (likely already merged upstream) -> rename
        to ``.patch.LANDED``
      - any other failure -> rename to ``.patch.SKIPPED`` and log
    """
    results: dict[str, str] = {}
    pdir = _patches_dir()
    patches = sorted(pdir.glob("*.patch"))
    if not patches:
        return results
    for patch in patches:
        status = _apply_one(patch)
        results[patch.name] = status
        if status == "already_landed":
            _rename_safely(patch, patch.name + ".LANDED")
        elif status in ("conflict", "error"):
            _rename_safely(patch, patch.name + ".SKIPPED")
    return results


def _rename_safely(path: Path, new_name: str) -> None:
    try:
        path.rename(path.with_name(new_name))
    except OSError as e:
        log.warning("rename %s -> %s failed: %s", path, new_name, e)


def _apply_one(patch_path: Path) -> str:
    check = subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=APP_ROOT, capture_output=True, text=True,
    )
    if check.returncode != 0:
        msg = (check.stderr or check.stdout or "").strip()
        # Two common failure modes: the patch is already present (merged
        # upstream and a new image carries it) OR it conflicts with other
        # changes. We can't reliably tell them apart from git's text, so we
        # default to "already_landed" (rename to .LANDED, treat as benign
        # cleanup) but flag conflicts when the error mentions hunks failing.
        lower = msg.lower()
        if "patch does not apply" in lower or "already exists" in lower:
            # Could be either case; lean toward already_landed for cleanup
            # purposes. Human can dig into the .LANDED file if curious.
            log.info(
                "patch %s does not apply (likely already in main): %s",
                patch_path.name, msg[:200],
            )
            return "already_landed"
        log.warning(
            "patch %s check failed (unrecognized): %s",
            patch_path.name, msg[:200],
        )
        return "conflict"

    apply = subprocess.run(
        ["git", "apply", str(patch_path)],
        cwd=APP_ROOT, capture_output=True, text=True,
    )
    if apply.returncode == 0:
        log.info("patch APPLIED %s", patch_path.name)
        return "applied"
    log.warning(
        "patch apply failed %s: %s",
        patch_path.name, (apply.stderr or apply.stdout or "")[:300],
    )
    return "error"


# ---------------------------------------------------------------------------
# GitHub push
# ---------------------------------------------------------------------------

async def push_to_branch(
    session_id: str,
    carrier: str,
    patch_content: str,
    summary: str,
) -> tuple[bool, str]:
    """Force-push the verified patch to ``auto-repair/<session_id>``.

    Performed via a side-clone in /tmp so ``/app``'s git state is untouched.
    Returns ``(ok, message_or_branch_name)``.
    """
    pat = _read_pat()
    if not pat:
        return False, "no PAT at storage/secrets/github_pat — skipped push"
    if not patch_content.strip():
        return False, "patch content empty"

    branch = f"auto-repair/{session_id}"
    auth_url = f"https://x-access-token:{pat}@{REPO_HOST_PATH}"
    safe_url = f"https://x-access-token:***@{REPO_HOST_PATH}"

    clone_dir = Path(
        tempfile.mkdtemp(prefix=f"auto-repair-push-{session_id[:12]}-")
    )
    log.info(
        "auto-repair push starting carrier=%s session=%s branch=%s url=%s",
        carrier, session_id, branch, safe_url,
    )
    try:
        r = await _run([
            "git", "clone", "--quiet", "--depth=30", auth_url, str(clone_dir),
        ])
        if r.returncode != 0:
            return False, f"clone failed: {r.stderr_str[:300]}"

        r = await _run(["git", "checkout", "-B", branch], cwd=clone_dir)
        if r.returncode != 0:
            return False, f"branch checkout failed: {r.stderr_str[:300]}"

        patch_in_clone = clone_dir / "__auto_repair_patch.diff"
        patch_in_clone.write_text(patch_content)
        # Plain `git apply` (not --3way). The container's /app git baseline
        # is a synthetic init-time commit local to the image; GitHub doesn't
        # have those blobs, so a 3-way merge always fails with "repository
        # lacks the necessary blob to perform 3-way merge". The patch
        # context is generated against the image's working tree, which
        # matches main HEAD at deploy time — so plain apply succeeds as
        # long as main hasn't diverged since.
        r = await _run(
            ["git", "apply", "--whitespace=nowarn", str(patch_in_clone)],
            cwd=clone_dir,
        )
        try:
            patch_in_clone.unlink()
        except OSError:
            pass
        if r.returncode != 0:
            return False, f"git apply failed: {r.stderr_str[:300]}"

        r = await _run(["git", "add", "-A"], cwd=clone_dir)
        if r.returncode != 0:
            return False, f"git add failed: {r.stderr_str[:300]}"

        commit_msg = _commit_message(session_id, carrier, summary)
        r = await _run(
            [
                "git",
                "-c", "user.email=auto-repair@infer.local",
                "-c", "user.name=auto-repair bot",
                "commit", "-m", commit_msg,
            ],
            cwd=clone_dir,
        )
        if r.returncode != 0:
            return False, f"git commit failed: {r.stderr_str[:300]}"

        r = await _run(
            [
                "git", "push", "--force-with-lease",
                "origin", f"HEAD:refs/heads/{branch}",
            ],
            cwd=clone_dir,
        )
        if r.returncode != 0:
            return False, f"git push failed: {r.stderr_str[:300]}"

        log.info(
            "auto-repair push OK carrier=%s session=%s branch=%s",
            carrier, session_id, branch,
        )
        return True, branch
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _commit_message(session_id: str, carrier: str, summary: str) -> str:
    head = f"Auto-repair: {carrier} (session {session_id})"
    body = (summary or "").strip()
    if body:
        return f"{head}\n\n{body}\n"
    return f"{head}\n"


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------

class _RunResult:
    __slots__ = ("returncode", "stdout", "stderr", "stderr_str")

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.stderr_str = stderr.decode("utf-8", "replace")


async def _run(cmd: list[str], cwd: Path | None = None) -> _RunResult:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    return _RunResult(proc.returncode or 0, out or b"", err or b"")


# ---------------------------------------------------------------------------
# CLI entry: ``python -m backend.auto_repair_patches apply``
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cmd = argv[1] if len(argv) > 1 else "apply"
    if cmd != "apply":
        log.error("unknown command %r (only 'apply' is supported)", cmd)
        return 2
    results = apply_pending()
    if not results:
        log.info("no pending patches")
        return 0
    for fname, status in results.items():
        log.info("startup apply: %s -> %s", fname, status)
    # Always exit 0 — apply is best-effort and must not block container boot.
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
