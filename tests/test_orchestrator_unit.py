import asyncio
from contextlib import asynccontextmanager

from backend import orchestrator
from backend.config import settings
from backend.models import Carrier, Document, SessionState
from backend.session_manager import SessionManager


def test_should_use_stored_state_requires_state():
    assert not orchestrator._should_use_stored_state(Carrier.USAA, "u", None)


def test_should_use_stored_state_keeps_non_usaa_reuse():
    assert orchestrator._should_use_stored_state(
        Carrier.GEICO, "u", {"cookies": [], "origins": []}
    )


def test_should_use_stored_state_rejects_stale_usaa(monkeypatch):
    monkeypatch.setattr(settings, "usaa_quick_path_max_age_seconds", 300)
    monkeypatch.setattr(orchestrator.storage, "saved_at", lambda carrier, username: 1000.0)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1301.0)

    assert not orchestrator._should_use_stored_state(
        Carrier.USAA, "u", {"cookies": [], "origins": []}
    )


def test_should_use_stored_state_accepts_fresh_usaa(monkeypatch):
    monkeypatch.setattr(settings, "usaa_quick_path_max_age_seconds", 300)
    monkeypatch.setattr(orchestrator.storage, "saved_at", lambda carrier, username: 1000.0)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1299.0)

    assert orchestrator._should_use_stored_state(
        Carrier.USAA, "u", {"cookies": [], "origins": []}
    )


def test_should_use_stored_state_rejects_usaa_without_timestamp(monkeypatch):
    monkeypatch.setattr(orchestrator.storage, "saved_at", lambda carrier, username: None)

    assert not orchestrator._should_use_stored_state(
        Carrier.USAA, "u", {"cookies": [], "origins": []}
    )


def test_discard_stale_carrier_state_uses_optional_hook():
    calls = []

    class Flow:
        def discard_stale_state(self, username):
            calls.append(username)

    orchestrator._discard_stale_carrier_state(Flow(), "u")

    assert calls == ["u"]


async def test_login_context_mfa_flow_completes(monkeypatch):
    flow = _LoginContextFlow(mfa_required=True)
    monkeypatch.setattr(orchestrator, "get_flow", lambda carrier: flow)
    monkeypatch.setattr(orchestrator.storage, "load", lambda carrier, username: None)
    monkeypatch.setattr(orchestrator.storage, "save", lambda *args, **kwargs: None)
    manager = SessionManager()
    session = manager.create(Carrier.USAA, "u")

    task = asyncio.create_task(
        orchestrator.execute_login(manager, session.id, Carrier.USAA, "u", "p")
    )
    await _wait_for_state(manager, session.id, SessionState.MFA_REQUIRED)
    manager.submit_mfa(session.id, "123456")
    await task

    final = manager.get(session.id)
    assert final.state == SessionState.DONE
    assert flow.submitted_code == "123456"
    assert final.docs[0].id == "doc-0"


async def test_login_context_no_mfa_fetches_docs(monkeypatch):
    flow = _LoginContextFlow(mfa_required=False)
    monkeypatch.setattr(orchestrator, "get_flow", lambda carrier: flow)
    monkeypatch.setattr(orchestrator.storage, "load", lambda carrier, username: None)
    monkeypatch.setattr(orchestrator.storage, "save", lambda *args, **kwargs: None)
    manager = SessionManager()
    session = manager.create(Carrier.USAA, "u")

    await orchestrator.execute_login(manager, session.id, Carrier.USAA, "u", "p")

    final = manager.get(session.id)
    assert final.state == SessionState.DONE
    assert flow.fetch_count == 1


async def test_login_context_block_sets_error(monkeypatch):
    flow = _BlockingLoginContextFlow()
    monkeypatch.setattr(orchestrator, "get_flow", lambda carrier: flow)
    monkeypatch.setattr(orchestrator.storage, "load", lambda carrier, username: None)
    manager = SessionManager()
    session = manager.create(Carrier.USAA, "u")

    await orchestrator.execute_login(manager, session.id, Carrier.USAA, "u", "p")

    final = manager.get(session.id)
    assert final.state == SessionState.ERROR
    assert final.error == "USAA login blocked after password submit"


class _FakeContext:
    async def cookies(self):
        return []

    async def storage_state(self):
        return {"cookies": [], "origins": []}


class _FakePage:
    pass


class _LoginContextFlow:
    def __init__(self, mfa_required: bool) -> None:
        self._mfa_required = mfa_required
        self.submitted_code = None
        self.fetch_count = 0

    def context_options(self):
        return {"user_agent": "test"}

    @asynccontextmanager
    async def login_context(self, runner, username, password, context_options):
        yield _FakeContext(), _FakePage()

    async def mfa_required(self, page):
        return self._mfa_required

    async def submit_mfa(self, page, code):
        self.submitted_code = code

    async def fetch_documents(self, page, http, ctx):
        self.fetch_count += 1
        doc = Document(
            id="doc-0",
            name="Policy.pdf",
            content_type="application/pdf",
            size_bytes=8,
        )
        return [doc], {"doc-0": b"%PDF doc"}


class _BlockingLoginContextFlow(_LoginContextFlow):
    def __init__(self) -> None:
        super().__init__(mfa_required=False)

    @asynccontextmanager
    async def login_context(self, runner, username, password, context_options):
        raise RuntimeError("USAA login blocked after password submit")
        yield


async def _wait_for_state(manager: SessionManager, session_id: str, state: SessionState):
    for _ in range(100):
        if manager.get(session_id).state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"session did not reach {state}")
