from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.async_api import Browser, Page, async_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URLS = {
    "mercury": "https://cp.mercuryinsurance.com/",
}

CLICK_LISTENER = r"""
(() => {
  if (window.__manualPortalProbeInstalled) return;
  window.__manualPortalProbeInstalled = true;

  function cleanText(value) {
    return String(value || '').replace(/\s+/g, ' ').trim().slice(0, 180);
  }

  function selectorPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 7) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        part += '#' + node.id;
        parts.unshift(part);
        break;
      }
      const testId = node.getAttribute('data-testid') || node.getAttribute('data-test');
      if (testId) part += `[data-testid="${testId}"]`;
      else if (node.getAttribute('name')) part += `[name="${node.getAttribute('name')}"]`;
      else if (node.className && typeof node.className === 'string') {
        const classes = node.className.trim().split(/\s+/).slice(0, 2);
        if (classes.length) part += '.' + classes.join('.');
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children)
          .filter(child => child.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  }

  function payloadFor(el, eventType) {
    const actionable = el.closest(
      'a,button,input,select,textarea,[role="button"],[role="link"],[aria-label],[data-testid],[data-test]'
    ) || el;
    return {
      type: eventType,
      tag: actionable.tagName && actionable.tagName.toLowerCase(),
      text: cleanText(actionable.innerText || actionable.textContent),
      ariaLabel: cleanText(actionable.getAttribute('aria-label')),
      role: actionable.getAttribute('role') || '',
      id: actionable.id || '',
      name: actionable.getAttribute('name') || '',
      inputType: actionable.getAttribute('type') || '',
      href: actionable.href || actionable.getAttribute('href') || '',
      selector: selectorPath(actionable),
      url: location.href,
      ts: Date.now()
    };
  }

  document.addEventListener('click', event => {
    console.log('__MANUAL_PORTAL_PROBE__' + JSON.stringify(payloadFor(event.target, 'click')));
  }, true);

  document.addEventListener('submit', event => {
    console.log('__MANUAL_PORTAL_PROBE__' + JSON.stringify(payloadFor(event.target, 'submit')));
  }, true);

  document.addEventListener('change', event => {
    const target = event.target;
    if (!target || !target.matches('select,input[type="checkbox"],input[type="radio"]')) return;
    console.log('__MANUAL_PORTAL_PROBE__' + JSON.stringify(payloadFor(target, 'change')));
  }, true);
})();
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a visible Chrome session and record manual portal navigation."
    )
    parser.add_argument("--carrier", default="mercury")
    parser.add_argument("--url", default=None)
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--out-dir", default=None)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    carrier = args.carrier.lower()
    url = args.url or DEFAULT_URLS.get(carrier)
    if not url:
        raise SystemExit(f"No default URL for carrier {carrier!r}; pass --url")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir or PROJECT_ROOT / "storage" / "manual-traces" / f"{carrier}-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "events.jsonl"
    profile_dir = Path(
        args.profile_dir
        or PROJECT_ROOT / "storage" / "browser-profiles" / f"manual-{carrier}"
    )
    profile_dir.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    chrome = _chrome_binary()
    command = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--window-size=1400,950",
        url,
    ]
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--no-sandbox")

    chrome_log = (out_dir / "chrome.log").open("wb")
    proc = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=chrome_log,
        start_new_session=(os.name == "posix"),
    )
    browser: Browser | None = None
    try:
        await _wait_for_port(port)
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            await ctx.add_init_script(CLICK_LISTENER)
            for page in ctx.pages:
                await _attach_page(page, log_path)
            ctx.on("page", lambda page: asyncio.create_task(_attach_page(page, log_path)))

            print(f"Manual probe running for {carrier}.")
            print(f"Drive the visible Chrome window. No credentials or input values are recorded.")
            print(f"Trace: {log_path}")
            print("Press Enter here when you are done.")
            await asyncio.to_thread(sys.stdin.readline)
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        _terminate_process_group(proc)
        chrome_log.close()
        print(f"Manual probe stopped. Trace: {log_path}")


async def _attach_page(page: Page, log_path: Path) -> None:
    await _install_listener(page)
    await _log_event(
        log_path,
        {
            "type": "page",
            "event": "attached",
            "url": _sanitize_url(page.url),
            "title": await _safe_title(page),
            "ts": _now_ms(),
        },
    )

    page.on(
        "console",
        lambda msg: asyncio.create_task(_handle_console(log_path, msg.text)),
    )
    page.on(
        "framenavigated",
        lambda frame: asyncio.create_task(_handle_navigation(page, frame, log_path)),
    )
    page.on(
        "response",
        lambda response: asyncio.create_task(_handle_response(response, log_path)),
    )
    page.on(
        "download",
        lambda download: asyncio.create_task(_handle_download(download, log_path)),
    )


async def _install_listener(page: Page) -> None:
    try:
        await page.evaluate(CLICK_LISTENER)
    except Exception:
        pass


async def _handle_console(log_path: Path, text: str) -> None:
    marker = "__MANUAL_PORTAL_PROBE__"
    if not text.startswith(marker):
        return
    try:
        payload = json.loads(text[len(marker) :])
    except json.JSONDecodeError:
        return
    payload["url"] = _sanitize_url(payload.get("url", ""))
    payload["href"] = _sanitize_url(payload.get("href", ""))
    await _log_event(log_path, payload)


async def _handle_navigation(page: Page, frame, log_path: Path) -> None:
    if frame != page.main_frame:
        return
    await _install_listener(page)
    await _log_event(
        log_path,
        {
            "type": "navigation",
            "url": _sanitize_url(page.url),
            "title": await _safe_title(page),
            "ts": _now_ms(),
        },
    )


async def _handle_response(response, log_path: Path) -> None:
    url = response.url
    headers = response.headers
    content_type = headers.get("content-type", "")
    content_disposition = headers.get("content-disposition", "")
    if not _looks_documentish(url, content_type, content_disposition):
        return
    await _log_event(
        log_path,
        {
            "type": "response",
            "url": _sanitize_url(url),
            "status": response.status,
            "contentType": content_type,
            "contentDisposition": content_disposition[:300],
            "contentLength": headers.get("content-length", ""),
            "ts": _now_ms(),
        },
    )


async def _handle_download(download, log_path: Path) -> None:
    await _log_event(
        log_path,
        {
            "type": "download",
            "url": _sanitize_url(download.url),
            "suggestedFilename": download.suggested_filename,
            "ts": _now_ms(),
        },
    )


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def _log_event(log_path: Path, event: dict) -> None:
    line = json.dumps(event, sort_keys=True)
    with log_path.open("a") as f:
        f.write(line + "\n")
    print(_summary(event), flush=True)


def _summary(event: dict) -> str:
    kind = event.get("type", "event")
    if kind in {"click", "submit", "change"}:
        label = event.get("text") or event.get("ariaLabel") or event.get("id") or event.get("name")
        return f"{kind}: {event.get('tag', '')} {label!r} @ {_sanitize_url(event.get('url', ''))}"
    if kind == "response":
        return f"response: {event.get('status')} {event.get('contentType')} {_sanitize_url(event.get('url', ''))}"
    if kind == "download":
        return f"download: {event.get('suggestedFilename')!r}"
    return f"{kind}: {_sanitize_url(event.get('url', ''))}"


def _looks_documentish(url: str, content_type: str, disposition: str) -> bool:
    blob = f"{url} {content_type} {disposition}".lower()
    return bool(
        re.search(
            r"pdf|octet-stream|download|document|policy|declaration|id.?card|proof",
            blob,
        )
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sanitize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("javascript:"):
        return "javascript:"
    try:
        parts = urlsplit(url)
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return url
    query_keys = [key for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
    query = "&".join(f"{key}=..." for key in query_keys[:12])
    if len(query_keys) > 12:
        query += "&..."
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _chrome_binary() -> str:
    configured = os.environ.get("CHROME_BINARY")
    if configured:
        return configured
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Google Chrome or Chromium binary not found")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError(f"Chrome CDP port {port} did not open")


def _terminate_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            proc.wait(timeout=5)
            return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
