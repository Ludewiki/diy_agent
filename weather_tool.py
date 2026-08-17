"""LangChain tool for fetching a forecast and selecting the best trip window.

The LLM only decides when to call this tool. Weather retrieval, scoring, and
sliding-window selection are deterministic Python code.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from statistics import fmean
from typing import Any

import requests
from langchain.tools import tool
from pydantic import BaseModel, Field, model_validator

from tool_errors import tool_error


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_SECONDS = 15
logger = logging.getLogger(__name__)


class WeatherToolInput(BaseModel):
    """Arguments visible to the agent."""

    city: str = Field(description="目的地城市，例如：上海、莫斯科")
    trip_days: int = Field(default=3, ge=1, le=7, description="连续游玩天数")
    forecast_days: int = Field(
        default=15,
        ge=1,
        le=16,
        description="从今天起检查多少天；当前数据源最多支持16天",
    )
    target_start_date: str | None = Field(
        default=None,
        description="希望筛选范围的开始日期，ISO格式 YYYY-MM-DD；不指定则从今天开始",
    )
    target_end_date: str | None = Field(
        default=None,
        description="希望筛选范围的结束日期，ISO格式 YYYY-MM-DD；须和开始日期同时提供",
    )
    sunshine_weight: float = Field(default=0.45, ge=0, le=1)
    rain_weight: float = Field(default=0.35, ge=0, le=1)
    heat_weight: float = Field(default=0.10, ge=0, le=1)
    wind_weight: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def validate_window_and_weights(self) -> "WeatherToolInput":
        if self.trip_days > self.forecast_days:
            raise ValueError("trip_days 不能大于 forecast_days")
        if (self.target_start_date is None) != (self.target_end_date is None):
            raise ValueError("target_start_date 和 target_end_date 必须同时提供")
        if self.target_start_date and self.target_end_date:
            try:
                start = date.fromisoformat(self.target_start_date)
                end = date.fromisoformat(self.target_end_date)
            except ValueError as exc:
                raise ValueError("目标日期必须使用 YYYY-MM-DD 格式") from exc
            if start > end:
                raise ValueError("target_start_date 不能晚于 target_end_date")
            if (end - start).days + 1 < self.trip_days:
                raise ValueError("目标日期范围短于 trip_days")
        total = (
            self.sunshine_weight
            + self.rain_weight
            + self.heat_weight
            + self.wind_weight
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError("四项权重之和必须等于 1")
        return self


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(value, high))


def _weather_description(code: int) -> str:
    """Convert WMO weather interpretation codes to a compact Chinese label."""
    if code == 0:
        return "晴"
    if code in {1, 2}:
        return "大致晴朗/多云"
    if code == 3:
        return "阴"
    if code in {45, 48}:
        return "雾"
    if code in {51, 53, 55, 56, 57}:
        return "毛毛雨"
    if code in {61, 63, 65, 66, 67}:
        return "雨"
    if code in {71, 73, 75, 77}:
        return "雪"
    if code in {80, 81, 82, 85, 86}:
        return "阵雨/阵雪"
    if code in {95, 96, 99}:
        return "雷暴"
    return "未知"


def calculate_weather_score(
    *,
    sunshine_duration: float,
    daylight_duration: float,
    rain_probability: float,
    temp_max: float,
    wind_speed_max: float,
    sunshine_weight: float,
    rain_weight: float,
    heat_weight: float,
    wind_weight: float,
) -> tuple[float, dict[str, float]]:
    """Normalize unlike weather units to 0..100, then apply the weights.

    Heat starts being penalized above 30 C and reaches its maximum penalty at
    40 C. Wind starts being penalized above 20 km/h and reaches its maximum at
    50 km/h. These thresholds are product policy and can be tuned later.
    """
    sunshine_score = _clamp(
        100.0 * sunshine_duration / daylight_duration
        if daylight_duration > 0
        else 0.0
    )
    rain_penalty = _clamp(rain_probability)
    heat_penalty = _clamp(max(0.0, temp_max - 30.0) / 10.0 * 100.0)
    wind_penalty = _clamp(max(0.0, wind_speed_max - 20.0) / 30.0 * 100.0)

    score = (
        sunshine_weight * sunshine_score
        - rain_weight * rain_penalty
        - heat_weight * heat_penalty
        - wind_weight * wind_penalty
    )
    components = {
        "sunshine_score": round(sunshine_score, 2),
        "rain_penalty": round(rain_penalty, 2),
        "heat_penalty": round(heat_penalty, 2),
        "wind_penalty": round(wind_penalty, 2),
    }
    return round(score, 2), components


def select_best_window(
    daily_weather: list[dict[str, Any]], trip_days: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rank all consecutive windows by mean score, then by their worst day."""
    if not daily_weather:
        raise ValueError("天气数据为空")
    if trip_days < 1 or trip_days > len(daily_weather):
        raise ValueError("trip_days 必须介于 1 和天气数据天数之间")

    windows: list[dict[str, Any]] = []
    for start in range(len(daily_weather) - trip_days + 1):
        days = daily_weather[start : start + trip_days]
        parsed_dates = [date.fromisoformat(day["date"]) for day in days]
        if any(
            current - previous != timedelta(days=1)
            for previous, current in zip(parsed_dates, parsed_dates[1:])
        ):
            # Skipped/missing forecast dates must never be treated as consecutive.
            continue
        scores = [float(day["weather_score"]) for day in days]
        windows.append(
            {
                "start_date": days[0]["date"],
                "end_date": days[-1]["date"],
                "average_score": round(fmean(scores), 2),
                "worst_day_score": round(min(scores), 2),
                "dates": [day["date"] for day in days],
            }
        )

    if not windows:
        raise ValueError(f"没有找到数据完整的连续 {trip_days} 天")

    # The worst-day tie-break prevents one excellent day from hiding a bad day.
    windows.sort(
        key=lambda item: (item["average_score"], item["worst_day_score"]),
        reverse=True,
    )
    return windows[0], windows


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("天气服务返回了非对象 JSON")
    return payload


def _geocode_city(city: str) -> dict[str, Any]:
    payload = _get_json(
        GEOCODING_URL,
        {"name": city, "count": 1, "language": "zh", "format": "json"},
    )
    results = payload.get("results") or []
    if not results:
        raise ValueError(f"找不到城市：{city}")
    return results[0]


def _tool_error(
    *,
    code: str,
    message: str,
    city: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a recoverable result that the agent can explain to the user."""
    return tool_error(code, message, details=details, query_city=city)


def _daily_value(daily: dict[str, Any], key: str, index: int) -> Any:
    values = daily.get(key)
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


@tool(args_schema=WeatherToolInput)
def find_best_weather_window(
    city: str,
    trip_days: int = 3,
    forecast_days: int = 15,
    target_start_date: str | None = None,
    target_end_date: str | None = None,
    sunshine_weight: float = 0.45,
    rain_weight: float = 0.35,
    heat_weight: float = 0.10,
    wind_weight: float = 0.10,
) -> dict[str, Any]:
    """查询城市未来逐日天气，并选出天气最适合的连续游玩日期。

    当用户询问近期出游日期、未来天气或最佳连续日期时调用。一次调用会
    同时完成天气查询、逐日评分和滑动窗口筛选，不需要模型自行做算术。
    实时逐日预报最多覆盖从今天起16天；若用户指定更远日期，本工具会返回
    FORECAST_OUT_OF_RANGE，Agent必须说明暂时无法可靠筛选，不能编造结果。
    """
    logger.info(
        "weather selection started",
        extra={"event": "tool_started", "tool_name": "find_best_weather_window", "city": city},
    )
    requested_start = (
        date.fromisoformat(target_start_date) if target_start_date else None
    )
    requested_end = date.fromisoformat(target_end_date) if target_end_date else None
    # Explicit ranges should see the provider's entire available horizon.
    requested_forecast_days = 16 if requested_start else forecast_days

    try:
        location = _geocode_city(city)
        forecast = _get_json(
            FORECAST_URL,
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "daily": ",".join(
                    [
                        "weather_code",
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_probability_max",
                        "precipitation_sum",
                        "sunshine_duration",
                        "daylight_duration",
                        "wind_speed_10m_max",
                    ]
                ),
                "timezone": "auto",
                "forecast_days": requested_forecast_days,
            },
        )
    except requests.RequestException:
        logger.exception(
            "weather service request failed",
            extra={"event": "tool_failed", "tool_name": "find_best_weather_window", "city": city, "error_code": "WEATHER_SERVICE_UNAVAILABLE"},
        )
        return _tool_error(
            code="WEATHER_SERVICE_UNAVAILABLE",
            message="天气服务暂时不可用，请稍后重试。",
            city=city,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _tool_error(
            code="LOCATION_OR_RESPONSE_ERROR",
            message=str(exc),
            city=city,
        )

    daily = forecast.get("daily")
    if not daily or not daily.get("time"):
        return _tool_error(
            code="EMPTY_FORECAST",
            message="天气服务未返回逐日预报。",
            city=city,
        )

    available_start = date.fromisoformat(daily["time"][0])
    available_end = date.fromisoformat(daily["time"][-1])
    if requested_start and requested_end and (
        requested_start < available_start or requested_end > available_end
    ):
        return _tool_error(
            code="FORECAST_OUT_OF_RANGE",
            message=(
                f"当前逐日天气预报只覆盖 {available_start.isoformat()} 至 "
                f"{available_end.isoformat()}，无法可靠比较用户指定的 "
                f"{requested_start.isoformat()} 至 {requested_end.isoformat()}。"
            ),
            city=city,
            details={
                "requested_start_date": requested_start.isoformat(),
                "requested_end_date": requested_end.isoformat(),
                "available_start_date": available_start.isoformat(),
                "available_end_date": available_end.isoformat(),
                "retry_on_or_after": (
                    requested_start - timedelta(days=15)
                ).isoformat(),
            },
        )

    rows: list[dict[str, Any]] = []
    skipped_dates: list[dict[str, Any]] = []
    for index, forecast_date in enumerate(daily["time"]):
        row_date = datetime.strptime(forecast_date, "%Y-%m-%d").date()
        if requested_start and requested_end and not (
            requested_start <= row_date <= requested_end
        ):
            continue

        required_keys = [
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "sunshine_duration",
            "daylight_duration",
            "wind_speed_10m_max",
        ]
        values = {key: _daily_value(daily, key, index) for key in required_keys}
        missing_fields = [key for key, value in values.items() if value is None]
        if missing_fields:
            skipped_dates.append(
                {"date": forecast_date, "missing_fields": missing_fields}
            )
            continue

        score, components = calculate_weather_score(
            sunshine_duration=float(values["sunshine_duration"]),
            daylight_duration=float(values["daylight_duration"]),
            rain_probability=float(values["precipitation_probability_max"]),
            temp_max=float(values["temperature_2m_max"]),
            wind_speed_max=float(values["wind_speed_10m_max"]),
            sunshine_weight=sunshine_weight,
            rain_weight=rain_weight,
            heat_weight=heat_weight,
            wind_weight=wind_weight,
        )
        weather_code = int(values["weather_code"])
        rows.append(
            {
                "date": forecast_date,
                "weather": _weather_description(weather_code),
                "weather_code": weather_code,
                "temp_min_c": values["temperature_2m_min"],
                "temp_max_c": values["temperature_2m_max"],
                "rain_probability_pct": values["precipitation_probability_max"],
                "precipitation_mm": _daily_value(daily, "precipitation_sum", index),
                "sunshine_hours": round(
                    float(values["sunshine_duration"]) / 3600.0, 1
                ),
                "wind_speed_max_kmh": values["wind_speed_10m_max"],
                "weather_score": score,
                "score_components": components,
            }
        )

    try:
        best_window, ranked_windows = select_best_window(rows, trip_days)
    except ValueError as exc:
        return _tool_error(
            code="INSUFFICIENT_COMPLETE_FORECAST",
            message=str(exc),
            city=city,
            details={
                "complete_days": len(rows),
                "trip_days": trip_days,
                "skipped_dates": skipped_dates,
            },
        )
    selected_dates = set(best_window["dates"])
    logger.info(
        "weather selection completed",
        extra={"event": "tool_succeeded", "tool_name": "find_best_weather_window", "city": city},
    )
    return {
        "status": "ok",
        "query_city": city,
        "resolved_location": {
            "name": location.get("name"),
            "admin1": location.get("admin1"),
            "country": location.get("country"),
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "timezone": forecast.get("timezone"),
        },
        "forecast_days": requested_forecast_days,
        "trip_days": trip_days,
        "target_start_date": target_start_date,
        "target_end_date": target_end_date,
        "best_window": {
            **best_window,
            "daily_weather": [row for row in rows if row["date"] in selected_dates],
        },
        "all_daily_weather": rows,
        "skipped_dates": skipped_dates,
        "top_windows": ranked_windows[:3],
        "score_formula": (
            f"{sunshine_weight}*sunshine_score - "
            f"{rain_weight}*rain_penalty - "
            f"{heat_weight}*heat_penalty - "
            f"{wind_weight}*wind_penalty"
        ),
        "source": {
            "name": "Open-Meteo",
            "forecast_url": FORECAST_URL,
            "geocoding_url": GEOCODING_URL,
        },
        "notice": "远期预报不确定性较高，建议出发前48小时重新运行。",
    }


if __name__ == "__main__":
    # Direct tool test without involving an LLM.
    result = find_best_weather_window.invoke(
        {"city": "上海", "trip_days": 3, "forecast_days": 15}
    )
    print(result["best_window"])
