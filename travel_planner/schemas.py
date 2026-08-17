"""Pydantic inputs and internal domain objects for travel planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class WeatherDayInput(BaseModel):
    date: str = Field(description="日期，ISO 8601 格式 YYYY-MM-DD")
    weather: str | None = Field(default=None, description="天气文字说明")
    rain_probability_pct: float = Field(default=0, ge=0, le=100)
    precipitation_mm: float = Field(default=0, ge=0)
    wind_speed_max_kmh: float = Field(default=0, ge=0)
    temp_max_c: float = Field(default=25)
    sunset: str | None = Field(default=None, description="可选日落时间，例如 18:42")


class TravelPlannerInput(BaseModel):
    city: str = Field(min_length=1, description="旅游城市，如上海")
    trip_days: int = Field(ge=1, le=7, description="连续游玩天数")
    weather_days: list[WeatherDayInput] = Field(
        description="天气工具返回的逐日天气；至少包含 trip_days 天"
    )
    interests: list[str] = Field(default_factory=list, description="用户兴趣")
    language: str = Field(default="zh", pattern=r"^[a-z][a-z0-9-]{1,11}$")
    daily_minutes: int = Field(default=480, ge=240, le=720)
    max_related_pages: int = Field(default=8, ge=1, le=15)
    max_candidates: int = Field(default=20, ge=3, le=35)
    exclude_universities: bool = True
    map_profile: Literal["foot-walking", "driving-car", "cycling-regular"] = "foot-walking"
    alpha_cross_page: float = Field(default=0.20, ge=0)
    beta_completeness: float = Field(default=0.20, ge=0)
    gamma_editorial: float = Field(default=0.20, ge=0)
    delta_preference: float = Field(default=0.25, ge=0)
    epsilon_freshness: float = Field(default=0.15, ge=0)
    bad_weather_rain_weight: float = Field(default=0.50, ge=0)
    bad_weather_wind_weight: float = Field(default=0.25, ge=0)
    bad_weather_heat_weight: float = Field(default=0.25, ge=0)

    @model_validator(mode="after")
    def validate_input(self) -> "TravelPlannerInput":
        if len(self.weather_days) < self.trip_days:
            raise ValueError("weather_days 的天数不能少于 trip_days")
        if len({item.date for item in self.weather_days}) != len(self.weather_days):
            raise ValueError("weather_days 中存在重复日期")
        for item in self.weather_days:
            try:
                date.fromisoformat(item.date)
            except ValueError as exc:
                raise ValueError(f"无效天气日期：{item.date}") from exc
        if self.alpha_cross_page + self.beta_completeness + self.gamma_editorial + self.delta_preference + self.epsilon_freshness <= 0:
            raise ValueError("景点评分权重之和必须大于 0")
        if self.bad_weather_rain_weight + self.bad_weather_wind_weight + self.bad_weather_heat_weight <= 0:
            raise ValueError("天气权重之和必须大于 0")
        return self


@dataclass
class SourcePage:
    title: str
    page_id: int
    revision_id: int
    revision_timestamp: str
    url: str
    kind: str
    wikitext: str = field(repr=False)


@dataclass
class Poi:
    key: str
    name: str
    section_type: str
    description: str = ""
    address: str = ""
    directions: str = ""
    hours: str = ""
    price: str = ""
    website: str = ""
    latitude: float | None = None
    longitude: float | None = None
    coordinate_source: str = "missing"
    map_match_confidence: float | None = None
    last_edit: str = ""
    source_pages: set[str] = field(default_factory=set)
    source_kinds: set[str] = field(default_factory=set)
    source_revision_times: list[str] = field(default_factory=list)
    visit_minutes: int = 90
    indoor_probability: float = 0.5
    night_view: bool = False
    closed_weekdays: set[int] = field(default_factory=set)
    crowd_sensitive: bool = False
    permanently_closed: bool = False
    score: float = 0.0
    score_components: dict[str, float] = field(default_factory=dict)
    penalty_reasons: list[str] = field(default_factory=list)
