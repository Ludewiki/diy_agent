import importlib
import json
import os
from unittest.mock import patch

from pydantic import ValidationError
import pytest
import requests
from langchain_core.messages import AIMessage, ToolMessage

from logging_config import redact_sensitive_text
from tool_errors import tool_error
from travel_planner.schemas import Poi, SourcePage, TravelPlannerInput
from travel_planner.service import plan_trip


WEATHER = [{"date": "2026-08-01"}, {"date": "2026-08-02"}]


def test_travel_schema_rejects_duplicate_dates() -> None:
    with pytest.raises(ValidationError, match="重复日期"):
        TravelPlannerInput(city="上海", trip_days=2, weather_days=[WEATHER[0], WEATHER[0]])


def test_tools_share_error_contract() -> None:
    result = tool_error("EXAMPLE", "message", details={"retryable": False}, query_city="上海")
    assert list(result) == ["status", "error_code", "message", "details", "query_city"]
    assert result["status"] == "error"


def test_missing_key_fails_before_network() -> None:
    config = TravelPlannerInput(city="上海", trip_days=2, weather_days=WEATHER)
    with patch.dict(os.environ, {}, clear=True):
        result = plan_trip(config)
    assert result["error_code"] == "MISSING_ORS_API_KEY"
    assert set(("status", "error_code", "message", "details")) <= result.keys()


def test_sensitive_request_values_are_redacted() -> None:
    raw = (
        "403 for https://example.test?q=东京&api_key=secret-value%3D "
        "Authorization=another-secret Bearer bearer-secret"
    )
    redacted = redact_sensitive_text(raw)
    assert "secret-value" not in redacted
    assert "another-secret" not in redacted
    assert "bearer-secret" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_geocoder_forbidden_degrades_to_wikivoyage_coordinates() -> None:
    config = TravelPlannerInput(
        city="东京",
        trip_days=2,
        weather_days=WEATHER,
        exclude_universities=False,
    )
    page = SourcePage(
        "东京",
        1,
        2,
        "2026-01-01T00:00:00Z",
        "https://example.test/tokyo",
        "city",
        "",
    )
    pois = [
        Poi(key="missing", name="缺少坐标", section_type="see"),
        Poi(
            key="one",
            name="景点一",
            section_type="see",
            latitude=35.68,
            longitude=139.76,
        ),
        Poi(
            key="two",
            name="景点二",
            section_type="see",
            latitude=35.69,
            longitude=139.70,
        ),
    ]
    request = requests.Request(
        "GET",
        "https://api.openrouteservice.org/geocode/search?api_key=secret-value",
    ).prepare()
    response = requests.Response()
    response.status_code = 403
    response.request = request
    forbidden = requests.HTTPError(
        "403 Forbidden",
        request=request,
        response=response,
    )
    matrix = (
        [[0.0, 10.0], [10.0, 0.0]],
        [[0.0, 1.0], [1.0, 0.0]],
        0,
    )
    with (
        patch.dict(os.environ, {"ORS_API_KEY": "secret-value"}),
        patch(
            "travel_planner.service.WikivoyageClient.collect_pages",
            return_value=[page],
        ),
        patch("travel_planner.service.extract_pois", return_value=pois),
        patch(
            "travel_planner.service.OrsClient.geocode",
            side_effect=forbidden,
        ),
        patch(
            "travel_planner.service.OrsClient.matrix",
            return_value=matrix,
        ),
    ):
        result = plan_trip(config)

    assert result["status"] == "ok"
    assert any("Geocoder 未授权" in item for item in result["warnings"])
    assert "secret-value" not in json.dumps(result, ensure_ascii=False)


def test_agent_plain_text_is_used_when_structured_output_is_missing() -> None:
    import weather_window

    payload = {
        "status": "ok",
        "city": "索契",
        "trip_days": 3,
        "itinerary": [{"date": "2026-09-08", "route_summary": "公园 → 博物馆"}],
        "source_pages": [
            {"title": "索契", "url": "https://zh.wikivoyage.org/wiki/索契"}
        ],
        "warnings": [],
    }

    class FakeAgent:
        def invoke(self, *_args, **_kwargs):
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id="tool-1",
                        name="plan_wikivoyage_trip",
                    ),
                    AIMessage(content="这是 DeepSeek 返回的普通文本行程。"),
                ]
            }

    with patch("weather_window.create_travel_agent", return_value=FakeAgent()):
        answer = weather_window.run_prompt("规划索契旅行")

    assert answer.answer == "这是 DeepSeek 返回的普通文本行程。"
    assert [item.title for item in answer.reference] == ["索契"]


def test_tool_result_builds_answer_when_model_returns_no_final_text() -> None:
    import weather_window

    payload = {
        "status": "ok",
        "city": "索契",
        "trip_days": 2,
        "itinerary": [
            {"date": "2026-09-08", "route_summary": "公园 → 午餐区"},
            {"date": "2026-09-09", "route_summary": "博物馆 → 海滨"},
        ],
        "source_pages": [],
        "warnings": ["ORS Geocoder 已降级。"],
    }

    class FakeAgent:
        def invoke(self, *_args, **_kwargs):
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id="tool-2",
                        name="plan_wikivoyage_trip",
                    ),
                    AIMessage(content=""),
                ]
            }

    with patch("weather_window.create_travel_agent", return_value=FakeAgent()):
        answer = weather_window.run_prompt("规划索契旅行")

    assert "2026-09-08 至 2026-09-09" in answer.answer
    assert "公园 → 午餐区" in answer.answer
    assert "ORS Geocoder 已降级" in answer.answer


def test_plain_json_model_answer_is_unwrapped() -> None:
    import weather_window

    class FakeAgent:
        def invoke(self, *_args, **_kwargs):
            return {
                "messages": [
                    AIMessage(
                        content='```json\n{"answer":"可读的最终行程","reference":[]}\n```'
                    )
                ]
            }

    with patch("weather_window.create_travel_agent", return_value=FakeAgent()):
        answer = weather_window.run_prompt("规划索契旅行")

    assert answer.answer == "可读的最终行程"


def test_importing_entrypoint_does_not_create_model_or_http_session() -> None:
    import weather_window

    with patch("langchain_deepseek.ChatDeepSeek", side_effect=AssertionError("model created")), patch("requests.Session", side_effect=AssertionError("http session created")):
        importlib.reload(weather_window)
