from datetime import date, timedelta

import pytest

from weather_tool import WeatherToolInput, calculate_weather_score, select_best_window


def test_clear_day_scores_higher_than_rainy_day() -> None:
    common = {
        "daylight_duration": 12 * 3600,
        "temp_max": 27,
        "wind_speed_max": 12,
        "sunshine_weight": 0.45,
        "rain_weight": 0.35,
        "heat_weight": 0.10,
        "wind_weight": 0.10,
    }
    clear, _ = calculate_weather_score(
        sunshine_duration=10 * 3600, rain_probability=5, **common
    )
    rainy, _ = calculate_weather_score(
        sunshine_duration=2 * 3600, rain_probability=90, **common
    )
    assert clear > rainy


def test_select_best_window_requires_consecutive_dates() -> None:
    start = date(2026, 8, 1)
    rows = [
        {"date": start.isoformat(), "weather_score": 99},
        {"date": (start + timedelta(days=2)).isoformat(), "weather_score": 99},
        {"date": (start + timedelta(days=3)).isoformat(), "weather_score": 50},
    ]
    best, _ = select_best_window(rows, 2)
    assert best["dates"] == ["2026-08-03", "2026-08-04"]


def test_weather_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="权重之和"):
        WeatherToolInput(city="上海", sunshine_weight=0.5)
