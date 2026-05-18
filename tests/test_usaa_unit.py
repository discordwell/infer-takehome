import asyncio

from backend.carriers import usaa
from backend.carriers.usaa import UsaaFlow
from backend.config import settings


def test_usaa_document_body_validation():
    assert UsaaFlow._is_document_body(b"%PDF-1.7\nbody", "application/pdf")
    assert UsaaFlow._is_document_body(b"binary body", "application/octet-stream")
    assert not UsaaFlow._is_document_body(
        b"<!doctype html><html></html>", "application/pdf"
    )
    assert not UsaaFlow._is_document_body(b"", "application/pdf")


def test_usaa_single_document_publishes_first_pdf_bytes():
    flow = UsaaFlow()
    docs, doc_bytes = flow._single_document(
        b"%PDF-1.7\nbody", "application/pdf", "Policy Declaration"
    )

    assert len(docs) == 1
    assert docs[0].id == "usaa-doc-0"
    assert docs[0].name == "Policy Declaration.pdf"
    assert docs[0].size_bytes == len(b"%PDF-1.7\nbody")
    assert doc_bytes == {"usaa-doc-0": b"%PDF-1.7\nbody"}


def test_usaa_merge_documents_dedupes_and_renumbers():
    flow = UsaaFlow()
    target_docs = []
    target_bytes = {}
    seen = set()

    docs1, bytes1 = flow._single_document(b"%PDF one", "application/pdf", "One")
    docs2, bytes2 = flow._single_document(b"%PDF one", "application/pdf", "Duplicate")
    docs3, bytes3 = flow._single_document(b"%PDF two", "application/pdf", "Two")

    flow._merge_documents(target_docs, target_bytes, seen, docs1, bytes1)
    flow._merge_documents(target_docs, target_bytes, seen, docs2, bytes2)
    flow._merge_documents(target_docs, target_bytes, seen, docs3, bytes3)

    assert [d.id for d in target_docs] == ["usaa-doc-0", "usaa-doc-1"]
    assert [d.name for d in target_docs] == ["One.pdf", "Two.pdf"]
    assert target_bytes == {
        "usaa-doc-0": b"%PDF one",
        "usaa-doc-1": b"%PDF two",
    }


def test_usaa_selects_latest_unique_renewal_per_policy():
    rows = [
        {
            "index": 0,
            "title": "Renters Policy Renewal",
            "dateDelivered": "05/12/2026",
            "account": "*-002",
            "rowText": "Renters Policy Renewal 05/12/2026 *-002 Options",
        },
        {
            "index": 1,
            "title": "Automobile Policy *********-7104 Renewal / Auto ID Cards",
            "dateDelivered": "06/11/2025",
            "account": "",
            "rowText": (
                "Automobile Policy *********-7104 Renewal / Auto ID Cards "
                "06/11/2025 Options"
            ),
        },
        {
            "index": 2,
            "title": "Renters Policy *********-002 Renewal",
            "dateDelivered": "05/12/2025",
            "account": "*-002",
            "rowText": "Renters Policy *********-002 Renewal 05/12/2025 *-002 Options",
        },
        {
            "index": 3,
            "title": "Automobile Policy *********-7104 Renewal / Auto ID Cards",
            "dateDelivered": "12/11/2024",
            "account": "*7104",
            "rowText": (
                "Automobile Policy *********-7104 Renewal / Auto ID Cards "
                "12/11/2024 *7104 Options"
            ),
        },
    ]

    selected = UsaaFlow._select_first_unique_usaa_document_candidates(rows)

    assert [candidate.index for candidate in selected] == [0, 1]
    assert [candidate.policy_key for candidate in selected] == [
        "renters:002",
        "auto:7104",
    ]


def test_usaa_declaration_docs_are_initial_policy_candidates():
    rows = [
        {
            "index": 0,
            "title": "Auto and Property Insurance Statement",
            "dateDelivered": "05/01/2026",
            "account": "*-002",
            "rowText": "Auto and Property Insurance Statement 05/01/2026 *-002",
        },
        {
            "index": 1,
            "title": "Homeowners Policy Declarations",
            "dateDelivered": "03/10/2021",
            "account": "*-884",
            "rowText": "Homeowners Policy Declarations 03/10/2021 *-884 Options",
        },
        {
            "index": 2,
            "title": "Automobile Insurance Policy Declarations",
            "dateDelivered": "12/11/2020",
            "account": "*7104",
            "rowText": "Automobile Insurance Policy Declarations 12/11/2020 *7104",
        },
    ]

    selected = UsaaFlow._select_first_unique_usaa_document_candidates(rows)

    assert [candidate.index for candidate in selected] == [1, 2]
    assert [candidate.document_kind for candidate in selected] == [
        "initial",
        "initial",
    ]


def test_usaa_latest_policy_packet_preferred_for_same_policy():
    rows = [
        {
            "index": 0,
            "title": "Automobile Insurance Policy Declarations",
            "dateDelivered": "12/11/2024",
            "account": "*7104",
            "rowText": "Automobile Insurance Policy Declarations 12/11/2024 *7104",
        },
        {
            "index": 1,
            "title": "Automobile Policy *********-7104 Renewal / Auto ID Cards",
            "dateDelivered": "06/11/2025",
            "account": "",
            "rowText": (
                "Automobile Policy *********-7104 Renewal / Auto ID Cards "
                "06/11/2025 *7104 Options"
            ),
        },
    ]

    selected = UsaaFlow._select_first_unique_usaa_document_candidates(rows)

    assert [candidate.index for candidate in selected] == [1]


def test_usaa_short_renew_title_counts_as_policy_renewal():
    rows = [
        {
            "index": 0,
            "title": "PC AUTO POL - RENEW",
            "dateDelivered": "12/11/2025",
            "account": "*7104",
            "rowText": "PC AUTO POL - RENEW 12/11/2025 *7104 Options",
        }
    ]

    selected = UsaaFlow._select_first_unique_usaa_document_candidates(rows)

    assert len(selected) == 1
    assert selected[0].document_kind == "renewal"
    assert selected[0].policy_key == "auto:7104"


def test_usaa_live_shape_selects_latest_available_policy_packets():
    rows = [
        {
            "index": 0,
            "title": "Renters Policy Renewal",
            "dateDelivered": "05/12/2026",
            "account": "*-002",
            "rowText": "Renters Policy Renewal 05/12/2026 *-002 Options",
        },
        {
            "index": 1,
            "title": "PC AUTO POL - RENEW",
            "dateDelivered": "12/11/2025",
            "account": "*7104",
            "rowText": "PC AUTO POL - RENEW 12/11/2025 *7104 Options",
        },
        {
            "index": 2,
            "title": "Automobile Policy *********-7104 Renewal / Auto ID Cards",
            "dateDelivered": "06/10/2023",
            "account": "*7104",
            "rowText": (
                "Automobile Policy *********-7104 Renewal / Auto ID Cards "
                "06/10/2023 *7104 Options"
            ),
        },
        {
            "index": 3,
            "title": "Renters Policy *********-002 Renewal",
            "dateDelivered": "05/11/2022",
            "account": "*-002",
            "rowText": "Renters Policy *********-002 Renewal 05/11/2022 *-002 Options",
        },
    ]

    selected = UsaaFlow._select_first_unique_usaa_document_candidates(rows)

    assert [candidate.index for candidate in selected] == [0, 1]


def test_usaa_timing_snapshot_uses_first_label_occurrence():
    flow = UsaaFlow()
    flow._timings = [
        ("mfa_code_received", 0.0),
        ("doc_pdf_bytes", 1.234),
        ("doc_pdf_bytes", 2.0),
        ("docs_ready_publish", 2.5),
    ]

    assert flow.timing_snapshot() == {
        "mfa_code_received": 0,
        "doc_pdf_bytes": 1234,
        "docs_ready_publish": 2500,
    }


def test_usaa_discard_stale_state_moves_profile(tmp_path, monkeypatch):
    profile = tmp_path / "usaa-chrome"
    profile.mkdir()
    (profile / "Preferences").write_text("{}")
    monkeypatch.setattr(usaa, "USAA_CHROME_PROFILE_DIR", profile)
    monkeypatch.setattr(settings, "usaa_login_driver", "playwright")
    monkeypatch.setattr(usaa.time, "time", lambda: 12345)

    UsaaFlow().discard_stale_state("u")

    moved = tmp_path / "stale" / "usaa-chrome-12345"
    assert not profile.exists()
    assert (moved / "Preferences").read_text() == "{}"


def test_usaa_context_options_default_to_os_browser_profile(tmp_path, monkeypatch):
    profile = tmp_path / "os-profile"
    monkeypatch.setattr(settings, "usaa_login_driver", "os_browser")
    monkeypatch.setattr(settings, "usaa_os_browser_profile_dir", str(profile))

    options = UsaaFlow().context_options()

    assert options["_launch_chrome_cdp"] is True
    assert options["_chrome_profile_dir"] == str(profile)


def test_usaa_context_options_for_username_scopes_os_browser_profile(
    tmp_path, monkeypatch
):
    profile = tmp_path / "os-profile"
    monkeypatch.setattr(settings, "usaa_login_driver", "os_browser")
    monkeypatch.setattr(settings, "usaa_os_browser_profile_dir", str(profile))

    first = UsaaFlow().context_options_for_username("alice@example.com")
    second = UsaaFlow().context_options_for_username("bob@example.com")

    assert first["_chrome_profile_dir"].startswith(str(profile))
    assert second["_chrome_profile_dir"].startswith(str(profile))
    assert first["_chrome_profile_dir"] != second["_chrome_profile_dir"]
    assert "alice" not in first["_chrome_profile_dir"]


def test_usaa_context_options_can_use_playwright_profile(tmp_path, monkeypatch):
    profile = tmp_path / "usaa-cdp"
    monkeypatch.setattr(settings, "usaa_login_driver", "playwright")
    monkeypatch.setattr(usaa, "USAA_CHROME_PROFILE_DIR", profile)

    options = UsaaFlow().context_options()

    assert options["_chrome_profile_dir"] == str(profile)


def test_usaa_invalid_login_driver_rejected(monkeypatch):
    monkeypatch.setattr(settings, "usaa_login_driver", "bogus")

    try:
        UsaaFlow().context_options()
    except RuntimeError as e:
        assert "USAA_LOGIN_DRIVER" in str(e)
    else:
        raise AssertionError("invalid driver should raise")


def test_usaa_unavailable_block_detection():
    body = (
        "We are unable to complete your request. "
        "Our system is currently unavailable. Please try again later."
    ).lower()

    assert UsaaFlow._is_unavailable_block_text(body)
    assert not UsaaFlow._is_unavailable_block_text("password mismatch")


def test_usaa_debug_html_redacts_password_values():
    html = (
        '<input name="memberId" value="demo-user">'
        '<input name="password" type="password" value="secret">'
    )

    sanitized = UsaaFlow._sanitize_debug_html(html)

    assert 'value="demo-user"' in sanitized
    assert "secret" not in sanitized
    assert 'value="[redacted]"' in sanitized


def test_usaa_os_browser_fill_retries_until_value_matches(monkeypatch):
    flow = UsaaFlow()
    calls: list[str] = []
    matches = iter((False, True))

    async def focus(selector: str, port: int) -> None:
        calls.append(f"focus:{selector}:{port}")

    async def paste(value: str) -> None:
        calls.append(f"paste:{len(value)}")

    async def value_matches(selector: str, value: str, port: int) -> bool:
        calls.append(f"match:{selector}:{port}:{len(value)}")
        return next(matches)

    async def fallback(selector: str, value: str, port: int) -> None:
        raise AssertionError("DOM fallback should not run after a successful paste")

    monkeypatch.setattr(flow, "_focus_chrome_selector", focus)
    monkeypatch.setattr(flow, "_replace_focused_text", paste)
    monkeypatch.setattr(flow, "_chrome_selector_value_matches", value_matches)
    monkeypatch.setattr(flow, "_set_chrome_selector_value", fallback)

    asyncio.run(
        flow._replace_chrome_selector_text(
            "input[type='password']", "secret", 9222, field_label="password"
        )
    )

    assert calls == [
        "focus:input[type='password']:9222",
        "paste:6",
        "match:input[type='password']:9222:6",
        "focus:input[type='password']:9222",
        "paste:6",
        "match:input[type='password']:9222:6",
    ]


def test_usaa_os_browser_fill_uses_dom_fallback_after_failed_pastes(monkeypatch):
    flow = UsaaFlow()
    paste_count = 0
    fallback_calls: list[tuple[str, int, int]] = []
    matches = iter((False, False, False, True))

    async def focus(selector: str, port: int) -> None:
        pass

    async def paste(value: str) -> None:
        nonlocal paste_count
        paste_count += 1

    async def value_matches(selector: str, value: str, port: int) -> bool:
        return next(matches)

    async def fallback(selector: str, value: str, port: int) -> None:
        fallback_calls.append((selector, len(value), port))

    monkeypatch.setattr(flow, "_focus_chrome_selector", focus)
    monkeypatch.setattr(flow, "_replace_focused_text", paste)
    monkeypatch.setattr(flow, "_chrome_selector_value_matches", value_matches)
    monkeypatch.setattr(flow, "_set_chrome_selector_value", fallback)

    asyncio.run(
        flow._replace_chrome_selector_text(
            "input[type='password']", "secret", 9222, field_label="password"
        )
    )

    assert paste_count == 3
    assert fallback_calls == [("input[type='password']", 6, 9222)]
