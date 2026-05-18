import sys

from backend.config import settings
from backend.playwright_runner import _default_browser_headless


def test_default_browser_forces_headless_on_linux_without_display(monkeypatch):
    monkeypatch.setattr(settings, "playwright_headless", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    assert _default_browser_headless()


def test_default_browser_honors_headed_on_macos(monkeypatch):
    monkeypatch.setattr(settings, "playwright_headless", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)

    assert not _default_browser_headless()
