"""Tests for tolerant boolean env-flag parsing and the mock-carrier toggles.

Regression coverage for the trap where mock flags were compared with a literal
``== "1"`` while ``.env.example`` documents them as ``true``/``false``. Under
the old code, the documented ``CARRIER_MOCK=true`` silently ran live carriers
and ``MOCK_QUICK_PATH_OK=true`` evaluated to *false*.
"""

import pytest

from backend.carriers import registry
from backend.carriers.mock import MockFlow
from backend.env_flags import env_truthy
from backend.models import Carrier


class TestEnvTruthy:
    @pytest.mark.parametrize(
        "value", ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON", " true "]
    )
    def test_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert env_truthy("SOME_FLAG") is True

    @pytest.mark.parametrize(
        "value", ["0", "false", "FALSE", "no", "No", "off", "OFF", " false "]
    )
    def test_falsy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert env_truthy("SOME_FLAG") is False

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert env_truthy("SOME_FLAG") is False
        assert env_truthy("SOME_FLAG", default=True) is True

    def test_empty_uses_default(self, monkeypatch):
        monkeypatch.setenv("SOME_FLAG", "")
        assert env_truthy("SOME_FLAG") is False
        assert env_truthy("SOME_FLAG", default=True) is True
        monkeypatch.setenv("SOME_FLAG", "   ")
        assert env_truthy("SOME_FLAG", default=True) is True

    def test_unrecognized_uses_default_not_silent_flip(self, monkeypatch):
        # A typo must fall back to the default rather than silently flipping a
        # flag the opposite way.
        monkeypatch.setenv("SOME_FLAG", "maybe")
        assert env_truthy("SOME_FLAG") is False
        assert env_truthy("SOME_FLAG", default=True) is True


class TestCarrierMockSelection:
    """`CARRIER_MOCK` must select MockFlow for documented truthy spellings."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_selects_mock(self, monkeypatch, value):
        monkeypatch.setenv("CARRIER_MOCK", value)
        flows = registry._build()
        assert isinstance(flows[Carrier.GEICO], MockFlow)
        assert isinstance(flows[Carrier.USAA], MockFlow)
        assert isinstance(flows[Carrier.MERCURY], MockFlow)

    def test_carrier_mock_true_regression(self, monkeypatch):
        # The headline bug: `.env.example`/mock.py document `CARRIER_MOCK=true`,
        # but the old `== "1"` check ran live carriers for it.
        monkeypatch.setenv("CARRIER_MOCK", "true")
        flows = registry._build()
        assert isinstance(flows[Carrier.USAA], MockFlow)

    @pytest.mark.parametrize("value", ["false", "0", "no", "off"])
    def test_falsy_selects_live(self, monkeypatch, value):
        monkeypatch.setenv("CARRIER_MOCK", value)
        flows = registry._build()
        assert not isinstance(flows[Carrier.USAA], MockFlow)

    def test_unset_selects_live(self, monkeypatch):
        monkeypatch.delenv("CARRIER_MOCK", raising=False)
        flows = registry._build()
        assert not isinstance(flows[Carrier.USAA], MockFlow)


class TestMockFlowFlags:
    async def test_skip_mfa_true(self, monkeypatch):
        monkeypatch.setenv("MOCK_SKIP_MFA", "true")
        assert await MockFlow().mfa_required(None) is False

    async def test_skip_mfa_default_requires_mfa(self, monkeypatch):
        monkeypatch.delenv("MOCK_SKIP_MFA", raising=False)
        assert await MockFlow().mfa_required(None) is True

    async def test_quick_path_default_ok(self, monkeypatch):
        monkeypatch.delenv("MOCK_QUICK_PATH_OK", raising=False)
        assert await MockFlow().is_authenticated(None) is True

    async def test_quick_path_true_is_ok_regression(self, monkeypatch):
        # `.env.example` ships MOCK_QUICK_PATH_OK=true; the old `== "1"` check
        # made it read as False. It must read as True.
        monkeypatch.setenv("MOCK_QUICK_PATH_OK", "true")
        assert await MockFlow().is_authenticated(None) is True

    async def test_quick_path_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("MOCK_QUICK_PATH_OK", "false")
        assert await MockFlow().is_authenticated(None) is False

    async def test_bad_password_true_raises(self, monkeypatch):
        monkeypatch.setenv("MOCK_BAD_PASSWORD", "true")
        with pytest.raises(RuntimeError, match="Invalid username or password"):
            await MockFlow().login(None, "u", "p")

    async def test_bad_mfa_true_raises(self, monkeypatch):
        monkeypatch.setenv("MOCK_BAD_MFA", "true")
        with pytest.raises(RuntimeError, match="Invalid verification code"):
            await MockFlow().submit_mfa(None, "123456")
