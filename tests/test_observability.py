from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.callbacks import build_tool_snapshot, extract_token_usage, tool_outcome
from benchmarks.runtime_benchmark import nearest_rank_percentile


def test_extract_token_usage_from_llm_output() -> None:
    response = SimpleNamespace(
        llm_output={
            "token_usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }
        }
    )
    assert extract_token_usage(response) == (11, 7, 18)


def test_extract_token_usage_from_message_metadata() -> None:
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 13,
            "output_tokens": 5,
            "total_tokens": 18,
        }
    )
    response = SimpleNamespace(
        llm_output=None,
        generations=[[SimpleNamespace(message=message)]],
    )
    assert extract_token_usage(response) == (13, 5, 18)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"status": "ok"}, "success"),
        ({"status": "error"}, "error"),
        (SimpleNamespace(content='{"status":"error"}'), "error"),
        ("plain output", "success"),
    ],
)
def test_tool_outcome(output: object, expected: str) -> None:
    assert tool_outcome(output) == expected


def test_nearest_rank_percentile_is_deterministic() -> None:
    values = [0.001, 0.002, 0.003, 0.004, 0.005]
    assert nearest_rank_percentile(values, 0.50) == 0.003
    assert nearest_rank_percentile(values, 0.95) == 0.005


def test_weather_tool_snapshot_uses_an_explicit_allowlist() -> None:
    snapshot = build_tool_snapshot(
        "find_best_weather_window",
        {
            "status": "ok",
            "query_city": "上海",
            "top_windows": [{"average_score": 90}],
            "internal_secret": "not-for-the-browser",
        },
    )
    assert snapshot == {
        "status": "ok",
        "query_city": "上海",
        "top_windows": [{"average_score": 90}],
    }


def test_unknown_tool_does_not_expose_its_output() -> None:
    assert build_tool_snapshot(
        "untrusted_tool",
        {"status": "ok", "payload": "private"},
    ) is None
