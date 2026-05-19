"""Lazy Claude-comprehension PDF analysis.

When the user clicks "Wrong documents" on the docs they received, we want to
hand the auto-repair Claude an honest description of what those docs were so
it knows what to do better. We spawn `claude -p` with the PDFs attached as
file paths and ask Claude to use its native PDF Read to describe each.

Not on the happy path — only runs at the moment of negative feedback.

Result shape (parsed from claude's JSON tail):
    [
        {"name": "Auto ID Card.pdf", "label": "auto id card",
         "description": "USAA member 1234 auto ID card", "category": "policy_doc"},
        ...
    ]

Categories: policy_doc | cover_or_brochure | login_or_error | other
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from .models import Document

log = logging.getLogger(__name__)

ANALYSIS_DIR = Path("storage/analyses")
ANALYSIS_TIMEOUT_SECONDS = 180  # claude can be slow on multi-PDF reads

PROMPT_TEMPLATE = """\
You are an analyst inspecting insurance-related PDFs a user just received
from a carrier portal.

For EACH file listed below, open it with your Read tool and assess:
- A one-line `label` (short, lowercase): what kind of doc this looks like.
  Examples: "declarations page", "auto id card", "policy booklet",
  "marketing brochure", "login page screenshot", "error page", "unknown".
- A `description` (one or two short sentences): the specific content you
  see. Mention policy numbers, named insured, coverage types, effective
  dates — whatever's there. If it's not a real policy doc, say so.
- A `category` (one of):
    - `policy_doc` — has actual insurance policy information (declarations,
      coverages, limits, ID card, COI, formal policy text). Auto, home,
      life, health, anything that's a real insurance instrument.
    - `cover_or_brochure` — a marketing brochure, FAQ, cover letter,
      promotional flier. Not a policy.
    - `login_or_error` — captured a login form, an error page, an empty PDF,
      or anything indicating the carrier site failed to return the actual
      doc.
    - `other` — anything else (terms of service, account statement, etc.).

Files to analyze:
{file_list}

After reading them, output a single JSON array (and nothing after it). Each
element MUST have keys: `name`, `label`, `description`, `category`. Example:

```json
[
  {{"name": "Declarations Page.pdf", "label": "declarations page", "description": "USAA auto policy declarations for member 12345, $50k/$100k BI, effective 2026-01-15", "category": "policy_doc"}}
]
```

Use exactly the file names you were given. Do not invent files. If a PDF
won't open or has no readable text, return it with category "login_or_error"
and a description saying so.
"""


async def analyze(
    session_id: str,
    docs: list[Document],
    doc_bytes: dict[str, bytes],
) -> list[dict]:
    """Run claude over the session's PDFs and return per-doc analysis.

    Writes the result to storage/analyses/<session_id>.json so a future
    re-trigger doesn't pay for it twice.
    """
    cached = _load_cached(session_id)
    if cached is not None:
        log.info(
            "pdf_analyzer: reusing cached analysis session=%s docs=%d",
            session_id, len(cached),
        )
        return cached

    if not docs:
        return []

    # Materialize PDFs to a stable dir claude can read.
    pdf_dir = Path("storage/repair") / session_id / "for_analysis"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    file_paths: list[Path] = []
    for doc in docs:
        body = doc_bytes.get(doc.id)
        if not body:
            continue
        # Sanitize filename to avoid CLI quoting issues; preserve a sane name.
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", doc.name) or f"{doc.id}.pdf"
        target = pdf_dir / safe
        try:
            target.write_bytes(body)
            file_paths.append(target)
        except OSError as e:
            log.warning("pdf_analyzer: write %s failed: %s", target, e)

    if not file_paths:
        return []

    file_list_str = "\n".join(f"- {p.resolve()}" for p in file_paths)
    prompt = PROMPT_TEMPLATE.format(file_list=file_list_str)

    started = time.time()
    try:
        result_text = await _run_claude(prompt)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "pdf_analyzer: claude run failed session=%s: %s", session_id, e
        )
        return []

    parsed = _extract_json_array(result_text)
    if parsed is None:
        log.warning(
            "pdf_analyzer: could not parse JSON from claude output "
            "session=%s len=%d head=%r",
            session_id, len(result_text), result_text[:240],
        )
        return []

    # Normalize: enforce required keys, drop noise.
    normalized: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        label = str(item.get("label") or "unknown")
        description = str(item.get("description") or "")
        category = str(item.get("category") or "other")
        if category not in {
            "policy_doc",
            "cover_or_brochure",
            "login_or_error",
            "other",
        }:
            category = "other"
        normalized.append(
            {
                "name": name,
                "label": label,
                "description": description,
                "category": category,
            }
        )

    _save_cache(session_id, normalized, elapsed_ms=int((time.time() - started) * 1000))
    log.info(
        "pdf_analyzer: analyzed session=%s docs=%d elapsed=%dms",
        session_id, len(normalized), int((time.time() - started) * 1000),
    )
    return normalized


def _extract_json_array(text: str) -> list | None:
    """Pull the first balanced JSON array out of claude's text response.

    Claude sometimes wraps output in ```json blocks; sometimes adds prose
    after. We find the first `[` and walk to the matching `]`."""
    if not text:
        return None
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


async def _run_claude(prompt: str) -> str:
    """Spawn `claude -p <prompt>` with PDF Read enabled. Return stdout text."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        "Glob",
        "Grep",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=ANALYSIS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"claude pdf-analyzer exceeded {ANALYSIS_TIMEOUT_SECONDS}s"
        ) from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr.decode()[:400]}"
        )
    try:
        outer = json.loads(stdout.decode())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude returned non-JSON stdout: {e}") from e
    return outer.get("result", "") or ""


def _cache_path(session_id: str) -> Path:
    return ANALYSIS_DIR / f"{session_id}.json"


def _load_cached(session_id: str) -> list[dict] | None:
    p = _cache_path(session_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if isinstance(data.get("docs"), list):
            return data["docs"]
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _save_cache(
    session_id: str, docs: list[dict], *, elapsed_ms: int
) -> None:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "saved_at": time.time(),
        "elapsed_ms": elapsed_ms,
        "docs": docs,
    }
    _cache_path(session_id).write_text(json.dumps(payload, indent=2))
