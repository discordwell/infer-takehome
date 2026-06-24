"""End-to-end test of /api/login + SSE status + /api/mfa + /api/docs.

Uses MockFlow (CARRIER_MOCK=1) and a stubbed Playwright runner so the test
runs in <2s without launching Chromium. AsyncClient lets us read the SSE
stream and submit MFA concurrently — TestClient can't (each sync call blocks
the test thread).
"""

import asyncio
import json

import httpx
import pytest

from backend.main import app


@pytest.fixture
async def client(fake_playwright, mock_carrier, tmp_session_dir):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            yield c


async def _collect_states(
    client: httpx.AsyncClient, session_id: str, timeout: float = 5.0
) -> list[dict]:
    """Collect SSE payloads until DONE/ERROR or timeout."""
    events: list[dict] = []
    async with client.stream("GET", f"/api/status/{session_id}") as resp:
        assert resp.status_code == 200
        try:
            async with asyncio.timeout(timeout):
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        payload = json.loads(line[len("data:"):].strip())
                        events.append(payload)
                        if payload.get("state") in ("DONE", "ERROR"):
                            return events
        except asyncio.TimeoutError:
            return events
    return events


async def test_full_login_flow_with_mfa(client):
    r = await client.post(
        "/api/login",
        json={"carrier": "geico", "username": "alice", "password": "pw"},
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]

    # Start collecting stream
    collector = asyncio.create_task(_collect_states(client, sid))
    # Submit MFA after the orchestrator has time to reach MFA_REQUIRED
    # (MockFlow.login sleeps 0.4s before we're at MFA_REQUIRED)
    await asyncio.sleep(0.6)
    mfa_resp = await client.post(f"/api/mfa/{sid}", json={"code": "123456"})
    assert mfa_resp.status_code == 202

    events = await collector
    states = [e["state"] for e in events]
    assert "MFA_REQUIRED" in states
    assert "DONE" in states
    done = next(e for e in events if e["state"] == "DONE")
    assert done["event"] == "docs_ready"
    assert len(done["docs"]) == 3

    # Fetch a doc
    doc_resp = await client.get(f"/api/docs/{sid}/dec")
    assert doc_resp.status_code == 200
    assert doc_resp.headers["content-type"].startswith("application/pdf")
    assert doc_resp.content.startswith(b"%PDF")


async def test_sse_stays_open_during_feedback_recovery(client):
    """A user rejecting docs sets repair_kicked while the session stays in DONE.

    Regression: the reopened SSE must stay open to stream the live repair log
    and deliver replacement docs — only `repair_done` closes it. Before the fix,
    the stream closed on the first DONE snapshot, so none of that reached the
    client.
    """
    from backend.models import Document
    from backend.session_manager import manager

    # Drive a normal flow to DONE.
    sid = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "reject@x", "password": "pw"},
        )
    ).json()["session_id"]
    collector = asyncio.create_task(_collect_states(client, sid))
    await asyncio.sleep(0.6)
    await client.post(f"/api/mfa/{sid}", json={"code": "1"})
    await collector  # session is now DONE

    # Simulate feedback recovery having kicked a repair for this session.
    manager.get(sid).repair_kicked = True

    received: list[dict] = []

    async def reader() -> None:
        async with client.stream("GET", f"/api/status/{sid}") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    payload = json.loads(line[len("data:"):].strip())
                    received.append(payload)
                    if payload.get("event") == "repair_done":
                        return

    task = asyncio.create_task(reader())
    # Wait until the reader has actually subscribed before publishing, else the
    # events would have no queue to land in. On the buggy code the subscriber is
    # torn down on the first DONE snapshot, so this poll never sees it.
    try:
        for _ in range(100):
            if manager.get(sid).subscribers:
                break
            await asyncio.sleep(0.02)
        assert manager.get(sid).subscribers, "reader stream closed before repair output"

        # Live repair activity + replacement docs arrive while still in DONE.
        manager.publish_repair_log(
            sid, {"turn": 1, "kind": "text", "text": "searching for better docs"}
        )
        manager.set_docs(
            sid,
            [Document(id="betterdoc", name="better.pdf", size_bytes=4)],
            {"betterdoc": b"%PDF"},
        )
        manager.publish_repair_done(sid, verdict="DONE", first_line="STATUS: DONE")

        await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()

    events = [e.get("event") for e in received]
    assert "repair_log" in events, f"missing live repair log: {events}"
    assert any(
        e.get("event") == "docs_ready"
        and e.get("docs")
        and any(d["id"] == "betterdoc" for d in e["docs"])
        for e in received
    ), f"replacement docs never delivered: {received}"
    assert events[-1] == "repair_done", f"stream did not end on repair_done: {events}"


async def test_sse_reconnect_after_repair_done_does_not_hang(client):
    """A client reconnecting AFTER the repair concluded must replay the terminal
    verdict and close on its own — not hang forever on the repair_kicked session
    whose repair_done already fired.
    """
    from backend.session_manager import manager

    sid = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "reconnect@x", "password": "pw"},
        )
    ).json()["session_id"]
    collector = asyncio.create_task(_collect_states(client, sid))
    await asyncio.sleep(0.6)
    await client.post(f"/api/mfa/{sid}", json={"code": "1"})
    await collector  # session is now DONE

    # Repair was kicked and concluded while the client was disconnected.
    manager.get(sid).repair_kicked = True
    manager.publish_repair_done(sid, verdict="NEED_HUMAN", first_line="needs a human")

    received: list[dict] = []

    async def reader() -> None:
        async with client.stream("GET", f"/api/status/{sid}") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    received.append(json.loads(line[len("data:"):].strip()))

    # On the buggy code this stream would hang (heartbeats forever) and the
    # wait_for would time out. With the terminal replay it closes promptly.
    await asyncio.wait_for(asyncio.create_task(reader()), timeout=5.0)
    assert any(e.get("event") == "repair_done" for e in received), received


async def test_mfa_for_unknown_session_404s(client):
    r = await client.post("/api/mfa/does-not-exist", json={"code": "0"})
    assert r.status_code == 404


async def test_status_for_unknown_session_404s(client):
    r = await client.get("/api/status/nope")
    assert r.status_code == 404


async def test_mfa_in_wrong_state_409s(client):
    sid = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "carol", "password": "pw"},
        )
    ).json()["session_id"]
    # immediately submit — state is IDLE/LOGGING_IN, not MFA_REQUIRED
    r = await client.post(f"/api/mfa/{sid}", json={"code": "1"})
    assert r.status_code == 409


async def test_doc_for_unknown_doc_id_404s(client):
    sid = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "bob", "password": "pw"},
        )
    ).json()["session_id"]
    collector = asyncio.create_task(_collect_states(client, sid))
    await asyncio.sleep(0.6)
    await client.post(f"/api/mfa/{sid}", json={"code": "1"})
    await collector

    r = await client.get(f"/api/docs/{sid}/no-such-doc")
    assert r.status_code == 404


async def test_doc_with_non_ascii_name_renders(client):
    """A doc whose scraped name carries an em-dash + quote must still serve
    (200) with a wire-safe Content-Disposition. Before the fix, latin-1
    header encoding raised on the em-dash and the PDF fetch 500'd.
    """
    from backend.models import Document
    from backend.session_manager import manager

    sid = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "special@x", "password": "pw"},
        )
    ).json()["session_id"]
    collector = asyncio.create_task(_collect_states(client, sid))
    await asyncio.sleep(0.6)
    await client.post(f"/api/mfa/{sid}", json={"code": "1"})
    await collector

    # Inject a doc whose name mirrors realistic scraped portal text.
    sess = manager.get(sid)
    sess.docs.append(
        Document(id="dec-em", name='Auto Policy – Declarations".pdf', size_bytes=13)
    )
    sess.doc_bytes["dec-em"] = b"%PDF-1.4 body"

    r = await client.get(f"/api/docs/{sid}/dec-em")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 body"
    cd = r.headers["content-disposition"]
    assert "filename*=utf-8''" in cd
    assert "%E2%80%93" in cd  # em-dash preserved, percent-encoded


async def test_second_uid_gets_423_on_busy_carrier(
    fake_playwright, mock_carrier, tmp_session_dir
):
    """First browser claims geico; a second browser (separate cookie jar) gets
    423 carrier-busy until the first releases the slot."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as browser_a, httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as browser_b:
            ra = await browser_a.post(
                "/api/login",
                json={"carrier": "geico", "username": "a", "password": "pw"},
            )
            assert ra.status_code == 200
            sid_a = ra.json()["session_id"]
            # The cookie was issued on the first response.
            assert "demo_uid" in browser_a.cookies

            # Second browser (no cookie) tries the same carrier.
            rb = await browser_b.post(
                "/api/login",
                json={"carrier": "geico", "username": "b", "password": "pw"},
            )
            assert rb.status_code == 423
            body = rb.json()
            assert body["detail"] == "carrier-busy"
            assert body["boring_url"] == "/api/cache"
            assert "demo_uid" in browser_b.cookies

            # Second browser CAN take a different carrier.
            rb2 = await browser_b.post(
                "/api/login",
                json={"carrier": "mercury", "username": "b", "password": "pw"},
            )
            assert rb2.status_code == 200

            # Drain SSE so the slots release cleanly before fixture teardown.
            await _collect_states(browser_a, sid_a, timeout=1.0)


async def test_same_uid_reload_returns_existing_session(
    fake_playwright, mock_carrier, tmp_session_dir
):
    """Reloading the page (same cookie, same carrier) returns the in-flight
    session rather than minting a new one."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as browser:
            r1 = await browser.post(
                "/api/login",
                json={"carrier": "geico", "username": "x", "password": "pw"},
            )
            sid1 = r1.json()["session_id"]

            r2 = await browser.post(
                "/api/login",
                json={"carrier": "geico", "username": "x", "password": "pw"},
            )
            assert r2.json()["session_id"] == sid1

            await _collect_states(browser, sid1, timeout=1.0)


async def test_cache_endpoint_is_per_uid(
    fake_playwright, mock_carrier, tmp_session_dir
):
    """After browser A completes a flow, only browser A sees those docs at
    /api/cache. Browser B sees its own (empty) cache."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as browser_a, httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as browser_b:
            sid = (
                await browser_a.post(
                    "/api/login",
                    json={
                        "carrier": "geico",
                        "username": "cache@x",
                        "password": "pw",
                    },
                )
            ).json()["session_id"]
            collector = asyncio.create_task(_collect_states(browser_a, sid))
            await asyncio.sleep(0.6)
            await browser_a.post(f"/api/mfa/{sid}", json={"code": "1"})
            await collector

            cache_a = (await browser_a.get("/api/cache")).json()
            assert any(
                r["carrier"] == "geico" and r["docs"]
                for r in cache_a["results"]
            )

            cache_b = (await browser_b.get("/api/cache")).json()
            assert cache_b["results"] == []


async def test_session_reuse_skips_mfa(client, tmp_session_dir):
    # First login — full flow with MFA
    sid1 = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "reuse@x.com", "password": "pw"},
        )
    ).json()["session_id"]
    collector1 = asyncio.create_task(_collect_states(client, sid1))
    await asyncio.sleep(0.6)
    await client.post(f"/api/mfa/{sid1}", json={"code": "1"})
    events1 = await collector1
    assert "DONE" in [e["state"] for e in events1]
    assert any(tmp_session_dir.iterdir()), "session must be persisted"

    # Second login — same user — should skip MFA
    sid2 = (
        await client.post(
            "/api/login",
            json={"carrier": "geico", "username": "reuse@x.com", "password": "pw"},
        )
    ).json()["session_id"]
    events2 = await _collect_states(client, sid2, timeout=5.0)
    states2 = [e["state"] for e in events2]
    assert "MFA_REQUIRED" not in states2, f"quick-path should skip MFA, got {states2}"
    assert "DONE" in states2
