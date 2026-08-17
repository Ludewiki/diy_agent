import importlib
import os
from unittest.mock import patch

from pydantic import ValidationError
import pytest

from tool_errors import tool_error
from travel_planner.schemas import TravelPlannerInput
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


def test_importing_entrypoint_does_not_create_model_or_http_session() -> None:
    import weather_window

    with patch("langchain_deepseek.ChatDeepSeek", side_effect=AssertionError("model created")), patch("requests.Session", side_effect=AssertionError("http session created")):
        importlib.reload(weather_window)
