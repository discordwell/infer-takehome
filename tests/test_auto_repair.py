"""Unit tests for the pure helpers in ``backend.auto_repair``.

``auto_repair.py`` is the largest backend module and drives the self-healing
loop, but most of it is integration-shaped (spawns claude, drives browsers).
These tests pin the pure, decision-making helpers that are cheap to exercise
in isolation — above all STATUS-verdict parsing, which gates whether a repair
is recognized as complete instead of looping until the wall-clock timeout.
"""

from __future__ import annotations

import pytest

from backend import auto_repair


class TestParseStatusVerdict:
    @pytest.mark.parametrize(
        "body, expected",
        [
            # Canonical bare verdicts.
            ("DONE", "DONE"),
            ("DONE\nfixed the selector", "DONE"),
            ("DONE: delivered better docs", "DONE"),
            ("NEED_HUMAN", "NEED_HUMAN"),
            ("NEED_HUMAN: session expired upstream", "NEED_HUMAN"),
            # Case-insensitive.
            ("done", "DONE"),
            ("need_human: stuck", "NEED_HUMAN"),
            # Markdown decoration on the first line.
            ("# DONE", "DONE"),
            ("**DONE**", "DONE"),
            ("`DONE`", "DONE"),
            ("  \t DONE", "DONE"),
            ("## NEED_HUMAN: out of ideas", "NEED_HUMAN"),
            # Regression guard: the repair prompt instructs claude to write
            # "STATUS: DONE", which the previous parser silently dropped
            # (it only stripped markdown, not the STATUS label) — so a
            # successful repair was never recognized.
            ("STATUS: DONE", "DONE"),
            ("STATUS: DONE\nsummary line", "DONE"),
            ("status: done", "DONE"),
            ("STATUS:DONE", "DONE"),
            ("STATUS DONE", "DONE"),
            ("**STATUS: DONE**", "DONE"),
            ("STATUS: NEED_HUMAN: blocked", "NEED_HUMAN"),
        ],
    )
    def test_recognized(self, body, expected):
        assert auto_repair.parse_status_verdict(body) == expected

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "   ",
            "working on it",
            "STATUS",  # label, but no verdict after it
            "STATUS REPORT: all good",  # has STATUS prefix but no verdict token
            "in progress, will write DONE later",
        ],
    )
    def test_unrecognized_returns_none(self, body):
        assert auto_repair.parse_status_verdict(body) is None

    def test_only_first_line_counts(self):
        # A verdict buried on a later line must NOT be promoted.
        assert auto_repair.parse_status_verdict("investigating\nDONE") is None

    def test_none_body_is_safe(self):
        assert auto_repair.parse_status_verdict(None) is None  # type: ignore[arg-type]


class TestTranslateStreamEvent:
    def test_assistant_text_block(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "  hello  "}]},
        }
        out = auto_repair._translate_stream_event(event, turn=2)
        assert out == [{"turn": 2, "kind": "text", "text": "hello"}]

    def test_assistant_blank_text_is_skipped(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "   "}]},
        }
        assert auto_repair._translate_stream_event(event, turn=1) == []

    def test_assistant_tool_use_block(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                ]
            },
        }
        out = auto_repair._translate_stream_event(event, turn=3)
        assert len(out) == 1
        chunk = out[0]
        assert chunk["kind"] == "tool_use"
        assert chunk["tool"] == "Bash"
        assert "ls" in chunk["input_preview"]

    def test_multi_block_message_returns_every_block(self):
        # The whole reason _translate_stream_event returns a list: a single
        # assistant message routinely carries [text, tool_use, text].
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "thinking"},
                    {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                    {"type": "text", "text": "done thinking"},
                ]
            },
        }
        out = auto_repair._translate_stream_event(event, turn=1)
        assert [c["kind"] for c in out] == ["text", "tool_use", "text"]

    def test_user_tool_result(self):
        event = {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "result body"}]
            },
        }
        out = auto_repair._translate_stream_event(event, turn=4)
        assert out == [
            {"turn": 4, "kind": "tool_result", "text_preview": "result body"}
        ]

    def test_user_tool_result_preview_is_truncated(self):
        event = {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "x" * 1000}]
            },
        }
        out = auto_repair._translate_stream_event(event, turn=1)
        assert len(out[0]["text_preview"]) == 600

    def test_result_event_becomes_turn_end(self):
        event = {"type": "result", "result": "final summary"}
        out = auto_repair._translate_stream_event(event, turn=7)
        assert out == [{"turn": 7, "kind": "turn_end", "text": "final summary"}]

    def test_unknown_event_type_yields_nothing(self):
        assert auto_repair._translate_stream_event({"type": "system"}, turn=1) == []
        assert auto_repair._translate_stream_event({}, turn=1) == []


class TestStringifyToolResult:
    def test_none(self):
        assert auto_repair._stringify_tool_result(None) == ""

    def test_string_is_stripped(self):
        assert auto_repair._stringify_tool_result("  hi  ") == "hi"

    def test_list_of_text_blocks_joined(self):
        content = [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ]
        assert auto_repair._stringify_tool_result(content) == "line one\nline two"

    def test_list_ignores_non_text_pieces(self):
        content = [
            {"type": "image", "source": "..."},
            {"type": "text", "text": "kept"},
        ]
        assert auto_repair._stringify_tool_result(content) == "kept"

    def test_unexpected_type_yields_empty(self):
        assert auto_repair._stringify_tool_result(123) == ""


class TestPreviewDict:
    def test_serializes_json(self):
        assert auto_repair._preview_dict({"a": 1}) == '{"a": 1}'

    def test_non_serializable_falls_back_to_str(self):
        # default=str keeps json.dumps from raising on odd values.
        out = auto_repair._preview_dict({"x": object()})
        assert "object" in out

    def test_truncated_to_400_chars(self):
        out = auto_repair._preview_dict({"k": "v" * 1000})
        assert len(out) == 400


class TestIsEnabled:
    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "Yes"])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("REPAIR_ENABLED", value)
        assert auto_repair.is_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "off"])
    def test_falsey_values_disable(self, monkeypatch, value):
        monkeypatch.setenv("REPAIR_ENABLED", value)
        assert auto_repair.is_enabled() is False

    def test_unset_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv("REPAIR_ENABLED", raising=False)
        assert auto_repair.is_enabled() is False
