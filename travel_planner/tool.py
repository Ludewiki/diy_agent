"""LangChain adapter for the travel-planning application service."""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

from .schemas import TravelPlannerInput
from .service import plan_trip


@tool(args_schema=TravelPlannerInput)
def plan_wikivoyage_trip(
    city: str,
    trip_days: int,
    weather_days: list[dict[str, Any]],
    interests: list[str] | None = None,
    language: str = "zh",
    daily_minutes: int = 480,
    max_related_pages: int = 8,
    max_candidates: int = 20,
    exclude_universities: bool = True,
    map_profile: str = "foot-walking",
    alpha_cross_page: float = 0.20,
    beta_completeness: float = 0.20,
    gamma_editorial: float = 0.20,
    delta_preference: float = 0.25,
    epsilon_freshness: float = 0.15,
    bad_weather_rain_weight: float = 0.50,
    bad_weather_wind_weight: float = 0.25,
    bad_weather_heat_weight: float = 0.25,
) -> dict[str, Any]:
    """从 Wikivoyage 获取景点并结合地图交通时间和天气生成多日攻略。"""
    return plan_trip(TravelPlannerInput(
        city=city, trip_days=trip_days, weather_days=weather_days,
        interests=interests or [], language=language, daily_minutes=daily_minutes,
        max_related_pages=max_related_pages, max_candidates=max_candidates,
        exclude_universities=exclude_universities, map_profile=map_profile,
        alpha_cross_page=alpha_cross_page, beta_completeness=beta_completeness,
        gamma_editorial=gamma_editorial, delta_preference=delta_preference,
        epsilon_freshness=epsilon_freshness,
        bad_weather_rain_weight=bad_weather_rain_weight,
        bad_weather_wind_weight=bad_weather_wind_weight,
        bad_weather_heat_weight=bad_weather_heat_weight,
    ))
