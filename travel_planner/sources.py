"""Wikivoyage API access and listing extraction."""

from __future__ import annotations

from html import unescape
import os
import re
from typing import Any

import mwparserfromhell
import requests

from .schemas import Poi, SourcePage

WIKIVOYAGE_API = "https://{language}.wikivoyage.org/w/api.php"
LISTING_TYPES = {
    "see": "see", "do": "do", "listing": "listing", "attraction": "see",
    "activity": "do", "看": "see", "景点": "see", "景點": "see",
    "做": "do", "活动": "do", "活動": "do",
}
INDOOR_KEYWORDS = (
    "博物馆", "博物館", "美术馆", "美術館", "艺术馆", "藝術館", "纪念馆",
    "紀念館", "展览馆", "展覽館", "剧院", "劇院", "室内", "室內", "商场",
    "商場", "水族馆", "水族館", "museum", "gallery", "theatre", "theater",
    "aquarium", "indoor", "shopping mall",
)
MIXED_KEYWORDS = (
    "宫殿", "宮殿", "寺", "教堂", "古镇", "古鎮", "历史街区", "歷史街區",
    "palace", "temple", "church", "historic district", "old town",
)
OUTDOOR_KEYWORDS = (
    "公园", "公園", "花园", "花園", "广场", "廣場", "步道", "山", "海滩",
    "海灘", "动物园", "動物園", "植物园", "植物園", "游船", "park", "garden",
    "square", "trail", "beach", "zoo", "outdoor", "cruise",
)
NIGHT_KEYWORDS = (
    "夜景", "夜游", "夜遊", "日落", "灯光", "燈光", "观景台", "觀景台",
    "night view", "nightlife", "sunset", "observation deck", "light show",
)
CLOSED_KEYWORDS = (
    "永久关闭", "永久關閉", "已经关闭", "已經關閉", "停止营业", "停止營業",
    "permanently closed", "no longer operating", "closed down",
)
UNIVERSITY_CAMPUS_KEYWORDS = ("大学城", "大學城", "校园", "校園", "校区", "校區", "campus")
CROWD_KEYWORDS = ("热门", "熱門", "排队", "排隊", "拥挤", "擁擠", "popular", "crowded", "queue")
WEEKDAY_PATTERNS = {
    0: ("周一", "星期一", "礼拜一", "禮拜一", "monday", "mon"),
    1: ("周二", "星期二", "礼拜二", "禮拜二", "tuesday", "tue"),
    2: ("周三", "星期三", "礼拜三", "禮拜三", "wednesday", "wed"),
    3: ("周四", "星期四", "礼拜四", "禮拜四", "thursday", "thu"),
    4: ("周五", "星期五", "礼拜五", "禮拜五", "friday", "fri"),
    5: ("周六", "星期六", "礼拜六", "禮拜六", "saturday", "sat"),
    6: ("周日", "星期日", "星期天", "礼拜日", "禮拜日", "sunday", "sun"),
}


def plain_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        text = mwparserfromhell.parse(str(value)).strip_code(normalize=True, collapse=True)
    except Exception:
        text = str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def classify_page(title: str, city: str, wikitext: str) -> str:
    lowered = f"{title}\n{wikitext[:3000]}".casefold()
    if title.casefold() == city.casefold():
        return "city"
    if any(word in lowered for word in ("itinerary", "旅行路线", "旅行路線", "行程", "路线", "路線")):
        return "itinerary"
    if city.casefold() in title.casefold() or any(word in lowered for word in ("district", "行政区", "行政區", "区", "區")):
        return "district"
    return "related"


class WikivoyageClient:
    def __init__(self, language: str, session: requests.Session | None = None) -> None:
        self.api_url = WIKIVOYAGE_API.format(language=language)
        user_agent = os.getenv(
            "WIKIMEDIA_USER_AGENT",
            "TravelPlannerAgent/0.1 (local educational project; configure contact)",
        )
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(self.api_url, params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"MediaWiki API 错误：{payload['error']}")
        return payload

    def search_pages(self, city: str, limit: int) -> list[dict[str, Any]]:
        payload = self._get({
            "action": "query", "list": "search", "srsearch": city,
            "srnamespace": 0, "srlimit": min(limit * 3, 30),
            "format": "json", "formatversion": 2, "utf8": 1,
        })
        results = payload.get("query", {}).get("search", [])
        exact = [item for item in results if item.get("title", "").casefold() == city.casefold()]
        others = [item for item in results if item not in exact]
        return (exact + others)[: max(limit * 2, limit)]

    def fetch_page(self, title: str, city: str) -> SourcePage | None:
        payload = self._get({
            "action": "query", "prop": "revisions|info", "inprop": "url",
            "rvprop": "ids|timestamp|content", "rvslots": "main", "titles": title,
            "format": "json", "formatversion": 2, "utf8": 1,
        })
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return None
        page = pages[0]
        revisions = page.get("revisions", [])
        if not revisions:
            return None
        revision = revisions[0]
        wikitext = revision.get("slots", {}).get("main", {}).get("content", "")
        return SourcePage(
            title=page.get("title", title), page_id=int(page.get("pageid", 0)),
            revision_id=int(revision.get("revid", 0)),
            revision_timestamp=revision.get("timestamp", ""), url=page.get("fullurl", ""),
            kind=classify_page(page.get("title", title), city, wikitext), wikitext=wikitext,
        )

    def collect_pages(self, city: str, limit: int) -> list[SourcePage]:
        results = self.search_pages(city, limit)
        chosen_titles: list[str] = []
        for item in results:
            title = item.get("title", "")
            snippet = plain_text(item.get("snippet", ""))
            if title.casefold() == city.casefold() or city.casefold() in title.casefold() or city.casefold() in snippet.casefold():
                if title and title not in chosen_titles:
                    chosen_titles.append(title)
            if len(chosen_titles) >= limit:
                break
        if not chosen_titles and results:
            chosen_titles.append(results[0].get("title", city))
        return [page for title in chosen_titles[:limit] if (page := self.fetch_page(title, city)) is not None]


def _template_params(template: Any) -> dict[str, str]:
    return {
        str(param.name).strip().casefold().replace("_", " "): plain_text(param.value)
        for param in template.params
    }


def _first_param(params: dict[str, str], *names: str) -> str:
    return next((params[name.casefold()] for name in names if params.get(name.casefold())), "")


def _parse_float(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ".")) if value else None
    try:
        return float(match.group()) if match else None
    except ValueError:
        return None


def _estimate_visit_minutes(text: str, section_type: str) -> int:
    lowered = text.casefold()
    hour_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|至|到)?\s*(\d+(?:\.\d+)?)?\s*(?:小时|小時|hours?|hrs?)", lowered)
    if hour_match:
        first = float(hour_match.group(1))
        second = float(hour_match.group(2)) if hour_match.group(2) else first
        return int(min(480, max(30, (first + second) / 2 * 60)))
    minute_match = re.search(r"(\d+)\s*(?:分钟|分鐘|minutes?|mins?)", lowered)
    if minute_match:
        return min(480, max(30, int(minute_match.group(1))))
    if any(keyword in lowered for keyword in ("主题公园", "主題公園", "theme park", "动物园", "動物園", "zoo")):
        return 300
    if any(keyword in lowered for keyword in INDOOR_KEYWORDS):
        return 150
    if any(keyword in lowered for keyword in OUTDOOR_KEYWORDS):
        return 120
    return 150 if section_type == "do" else 90


def _indoor_probability(text: str) -> float:
    lowered = text.casefold()
    indoor = sum(keyword in lowered for keyword in INDOOR_KEYWORDS)
    outdoor = sum(keyword in lowered for keyword in OUTDOOR_KEYWORDS)
    if indoor and not outdoor:
        return 1.0
    if outdoor and not indoor:
        return 0.0
    return 0.5


def _closed_weekdays(hours: str) -> set[int]:
    lowered = hours.casefold()
    closed_words = ("闭馆", "閉館", "休息", "关闭", "關閉", "closed", "not open")
    result: set[int] = set()
    for weekday, aliases in WEEKDAY_PATTERNS.items():
        for alias in aliases:
            position = lowered.find(alias)
            if position >= 0:
                context = lowered[max(0, position - 12): position + len(alias) + 12]
                if any(word in context for word in closed_words):
                    result.add(weekday)
                    break
    return result


def _listing_type(template_name: str, params: dict[str, str]) -> str | None:
    normalized = template_name.strip().casefold().replace("template:", "")
    if normalized not in LISTING_TYPES:
        return None
    listing_type = LISTING_TYPES[normalized]
    if listing_type == "listing":
        supplied = _first_param(params, "type", "类型", "類型").casefold()
        if supplied in ("see", "do"):
            listing_type = supplied
    return listing_type


def extract_pois(pages: list[SourcePage]) -> list[Poi]:
    merged: dict[str, Poi] = {}
    for page in pages:
        for template in mwparserfromhell.parse(page.wikitext).filter_templates(recursive=True):
            params = _template_params(template)
            section_type = _listing_type(str(template.name), params)
            if section_type not in ("see", "do"):
                continue
            name = _first_param(params, "name", "名称", "名稱", "1")
            key = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", name.casefold())
            if not key:
                continue
            description = _first_param(params, "content", "description", "描述", "简介", "簡介")
            address = _first_param(params, "address", "地址")
            hours = _first_param(params, "hours", "开放时间", "開放時間", "时间", "時間")
            latitude = _parse_float(_first_param(params, "lat", "latitude", "纬度", "緯度"))
            longitude = _parse_float(_first_param(params, "long", "lon", "longitude", "经度", "經度"))
            combined = " ".join((name, description, address, hours))
            candidate = Poi(
                key=key, name=name, section_type=section_type, description=description,
                address=address, directions=_first_param(params, "directions", "交通", "到达", "到達"),
                hours=hours, price=_first_param(params, "price", "价格", "價格", "费用", "費用"),
                website=_first_param(params, "url", "website", "官网", "官網"),
                latitude=latitude, longitude=longitude,
                coordinate_source="wikivoyage" if latitude is not None and longitude is not None else "missing",
                last_edit=_first_param(params, "lastedit", "last edit", "最后更新", "最後更新"),
                source_pages={page.title}, source_kinds={page.kind},
                source_revision_times=[page.revision_timestamp],
                visit_minutes=_estimate_visit_minutes(combined, section_type),
                indoor_probability=_indoor_probability(combined),
                night_view=any(keyword in combined.casefold() for keyword in NIGHT_KEYWORDS),
                closed_weekdays=_closed_weekdays(hours),
                crowd_sensitive=any(keyword in combined.casefold() for keyword in CROWD_KEYWORDS),
                permanently_closed=any(keyword in combined.casefold() for keyword in CLOSED_KEYWORDS),
            )
            if key not in merged:
                merged[key] = candidate
                continue
            existing = merged[key]
            existing.source_pages.update(candidate.source_pages)
            existing.source_kinds.update(candidate.source_kinds)
            existing.source_revision_times.extend(candidate.source_revision_times)
            for attribute in ("address", "directions", "hours", "price", "website", "last_edit"):
                if not getattr(existing, attribute) and getattr(candidate, attribute):
                    setattr(existing, attribute, getattr(candidate, attribute))
            if len(candidate.description) > len(existing.description):
                existing.description = candidate.description
            if existing.latitude is None and candidate.latitude is not None:
                existing.latitude, existing.longitude = candidate.latitude, candidate.longitude
                existing.coordinate_source = "wikivoyage"
            existing.night_view = existing.night_view or candidate.night_view
            existing.crowd_sensitive = existing.crowd_sensitive or candidate.crowd_sensitive
            existing.permanently_closed = existing.permanently_closed or candidate.permanently_closed
            existing.closed_weekdays.update(candidate.closed_weekdays)
    return list(merged.values())


def is_university_attraction(poi: Poi) -> bool:
    normalized_name = re.sub(r"\s+", " ", poi.name.casefold()).strip()
    if any(keyword in normalized_name for keyword in UNIVERSITY_CAMPUS_KEYWORDS):
        return True
    if re.search(r"(?:大学|大學|学院|學院)(?:[（(][^）)]*[）)])?$", normalized_name):
        return True
    return bool(
        re.search(r"\b(?:university|college)\b", normalized_name)
        and not re.search(r"\b(?:museum|gallery|library|park)\b", normalized_name)
    )
