"""Deterministic attraction scoring."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re

from .schemas import Poi

RECOMMEND_KEYWORDS = (
    "必看", "必去", "推荐", "推薦", "不容错过", "不容錯過", "亮点", "亮點",
    "must-see", "must see", "recommended", "highlight", "do not miss",
)
UNCERTAIN_KEYWORDS = (
    "可能", "据说", "據說", "似乎", "待确认", "待確認", "possibly", "perhaps",
    "reportedly", "unclear",
)
INTEREST_ALIASES = {
    "历史": ("历史", "歷史", "古迹", "古蹟", "遗址", "遺址", "history", "historic", "heritage"),
    "艺术": ("艺术", "藝術", "美术", "美術", "画廊", "畫廊", "art", "gallery"),
    "博物馆": ("博物馆", "博物館", "museum"),
    "建筑": ("建筑", "建築", "architecture", "building"),
    "自然": ("自然", "公园", "公園", "花园", "花園", "山", "natural", "park", "garden"),
    "亲子": ("亲子", "親子", "儿童", "兒童", "家庭", "family", "children", "kids"),
    "美食": ("美食", "餐厅", "餐廳", "小吃", "food", "restaurant", "cuisine"),
    "购物": ("购物", "購物", "商场", "商場", "shopping", "mall", "market"),
    "夜景": ("夜景", "夜游", "夜遊", "观景", "觀景", "night", "sunset", "view"),
}


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(cleaned[: len(datetime.now().strftime(fmt))], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.search(r"(20\d{2})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?", cleaned)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2) or 1), int(match.group(3) or 1),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _interest_match(text: str, interests: list[str]) -> float:
    if not interests:
        return 50.0
    lowered = text.casefold()
    matched = 0
    for interest in interests:
        interest_lower = interest.casefold().strip()
        terms = {interest_lower}
        for canonical, aliases in INTEREST_ALIASES.items():
            if canonical in interest_lower or any(alias.casefold() in interest_lower for alias in aliases):
                terms.update(alias.casefold() for alias in aliases)
        if any(term and term in lowered for term in terms):
            matched += 1
    return 100 * matched / len(interests)


def score_poi(
    poi: Poi,
    interests: list[str],
    weights: dict[str, float],
    now: datetime,
) -> None:
    """Update a POI with explainable score components and penalties."""
    kind_count = len(poi.source_kinds.intersection({"city", "district", "itinerary"}))
    cross_page = min(100.0, kind_count / 3 * 100 + max(0, len(poi.source_pages) - kind_count) * 8)
    completeness = 25 * sum((
        poi.latitude is not None and poi.longitude is not None,
        bool(poi.hours), bool(poi.price), bool(poi.website),
    ))
    combined = " ".join((poi.name, poi.description, poi.address)).casefold()
    editorial = 80.0 if poi.section_type == "see" else 75.0
    if any(keyword in combined for keyword in RECOMMEND_KEYWORDS):
        editorial = min(100.0, editorial + 20)
    if len(poi.description) < 20:
        editorial = max(0.0, editorial - 15)
    preference = _interest_match(combined, interests)
    timestamps = [_parse_timestamp(poi.last_edit)]
    timestamps.extend(_parse_timestamp(item) for item in poi.source_revision_times)
    valid_timestamps = [item for item in timestamps if item is not None]
    latest = max(valid_timestamps) if valid_timestamps else None
    age_days = max(0, (now - latest).days) if latest else 3650
    freshness = 100 * math.pow(2, -age_days / 730) if latest else 0.0
    penalty = 0.0
    reasons: list[str] = []
    for condition, amount, reason in (
        (poi.permanently_closed, 100, "疑似永久停业"),
        (poi.latitude is None or poi.longitude is None, 35, "坐标缺失"),
        (age_days > 5 * 365, 20, "数据超过五年未更新"),
        (any(keyword in combined for keyword in UNCERTAIN_KEYWORDS), 15, "描述含不确定表述"),
        (len(poi.description) < 10, 8, "描述信息不足"),
    ):
        if condition:
            penalty += amount
            reasons.append(reason)
    components = {
        "cross_page": cross_page, "completeness": completeness, "editorial": editorial,
        "preference": preference, "freshness": freshness,
    }
    weighted = sum(weights[name] * value for name, value in components.items()) / sum(weights.values())
    poi.score = round(max(0.0, weighted - penalty), 2)
    poi.score_components = {name: round(value, 2) for name, value in components.items()}
    poi.score_components["penalty"] = round(penalty, 2)
    poi.penalty_reasons = reasons
