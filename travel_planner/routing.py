"""OpenRouteService integration and deterministic daily route optimization."""

from __future__ import annotations

import math
from typing import Any

import requests

from .schemas import Poi

ORS_GEOCODER_URL = "https://api.openrouteservice.org/geocode/search"
ORS_MATRIX_URL = "https://api.openrouteservice.org/v2/matrix/{profile}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return earth_radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def symmetric_matrix(matrix: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    return [[0.0 if row == column else (matrix[row][column] + matrix[column][row]) / 2 for column in range(size)] for row in range(size)]


class OrsClient:
    def __init__(self, api_key: str, session: requests.Session | None = None) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "TravelPlannerAgent/0.1"})

    def geocode(self, query: str) -> tuple[float, float, float] | None:
        response = self.session.get(
            ORS_GEOCODER_URL,
            params={"api_key": self.api_key, "text": query, "size": 1}, timeout=25,
        )
        response.raise_for_status()
        features = response.json().get("features", [])
        if not features:
            return None
        feature = features[0]
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if len(coordinates) < 2:
            return None
        confidence = float(feature.get("properties", {}).get("confidence", 0.5))
        return float(coordinates[1]), float(coordinates[0]), confidence

    def matrix(
        self, coordinates: list[tuple[float, float]], profile: str,
    ) -> tuple[list[list[float]], list[list[float]], int]:
        locations = [[longitude, latitude] for latitude, longitude in coordinates]
        response = self.session.post(
            ORS_MATRIX_URL.format(profile=profile),
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            json={"locations": locations, "metrics": ["duration", "distance"]}, timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        durations_raw, distances_raw = payload.get("durations"), payload.get("distances")
        if not durations_raw or not distances_raw:
            raise RuntimeError("ORS Matrix 未返回 durations/distances")
        fallback_count = 0
        duration_minutes: list[list[float]] = []
        distance_km: list[list[float]] = []
        for row_index, row in enumerate(durations_raw):
            duration_row: list[float] = []
            distance_row: list[float] = []
            for column_index, value in enumerate(row):
                distance_value = distances_raw[row_index][column_index]
                if value is None or distance_value is None:
                    fallback_count += 1
                    lat1, lon1 = coordinates[row_index]
                    lat2, lon2 = coordinates[column_index]
                    km = haversine_km(lat1, lon1, lat2, lon2)
                    duration_row.append(km / 4.2 * 60 * 1.25)
                    distance_row.append(km * 1.25)
                else:
                    duration_row.append(float(value) / 60)
                    distance_row.append(float(distance_value) / 1000)
            duration_minutes.append(duration_row)
            distance_km.append(distance_row)
        return duration_minutes, distance_km, fallback_count


def shortest_open_path(indices: list[int], matrix: list[list[float]]) -> tuple[list[int], float]:
    if len(indices) <= 1:
        return indices[:], 0.0
    if len(indices) > 10:
        best_path, best_cost = [], math.inf
        for start in indices:
            remaining, path, cost = set(indices), [start], 0.0
            remaining.remove(start)
            while remaining:
                next_index = min(remaining, key=lambda item: matrix[path[-1]][item])
                cost += matrix[path[-1]][next_index]
                path.append(next_index)
                remaining.remove(next_index)
            if cost < best_cost:
                best_path, best_cost = path, cost
        return best_path, best_cost
    count = len(indices)
    dp: dict[tuple[int, int], tuple[float, list[int]]] = {(1 << pos, pos): (0.0, [pos]) for pos in range(count)}
    for mask in range(1, 1 << count):
        for last in range(count):
            state = dp.get((mask, last))
            if state is None:
                continue
            cost, path = state
            for next_position in range(count):
                if mask & (1 << next_position):
                    continue
                next_mask = mask | (1 << next_position)
                next_cost = cost + matrix[indices[last]][indices[next_position]]
                old = dp.get((next_mask, next_position))
                if old is None or next_cost < old[0]:
                    dp[(next_mask, next_position)] = (next_cost, path + [next_position])
    best_cost, positions = min(dp[((1 << count) - 1, last)] for last in range(count))
    return [indices[position] for position in positions], best_cost


def _format_time(total_minutes: int) -> str:
    total_minutes %= 24 * 60
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def build_day_route(
    cluster: list[int], pois: list[Poi], travel_minutes: list[list[float]],
    distance_km: list[list[float]], sunset: str | None,
) -> dict[str, Any]:
    ordered, _ = shortest_open_path(cluster, travel_minutes)
    if any(pois[index].night_view for index in ordered):
        ordered = [index for index in ordered if not pois[index].night_view] + [index for index in ordered if pois[index].night_view]
    current, lunch_inserted, previous = 9 * 60, False, None
    timeline: list[dict[str, Any]] = []
    route_labels: list[str] = []
    total_travel = total_distance = 0.0
    for position, index in enumerate(ordered):
        if previous is not None:
            leg_minutes, leg_distance = travel_minutes[previous][index], distance_km[previous][index]
            total_travel += leg_minutes
            total_distance += leg_distance
            timeline.append({"type": "transfer", "from": pois[previous].name, "to": pois[index].name, "minutes": round(leg_minutes), "distance_km": round(leg_distance, 2)})
            current += round(leg_minutes)
        if not lunch_inserted and current >= 11 * 60 + 30 and position > 0:
            timeline.append({"type": "meal_break", "name": "午餐区（在相邻景点附近选择）", "start": _format_time(current), "end": _format_time(current + 60), "minutes": 60})
            route_labels.append("午餐区")
            current += 60
            lunch_inserted = True
        start, end = current, current + pois[index].visit_minutes
        timeline.append({"type": "attraction", "name": pois[index].name, "start": _format_time(start), "end": _format_time(end), "visit_minutes": pois[index].visit_minutes, "indoor_probability": pois[index].indoor_probability, "night_view": pois[index].night_view})
        route_labels.append(pois[index].name)
        current, previous = end, index
    if not lunch_inserted:
        timeline.append({"type": "meal_break", "name": "午餐区（在当日景点附近选择）", "start": "12:00", "end": "13:00", "minutes": 60, "note": "时间轴较短，请按实际开放时间调整插入位置"})
        route_labels.append("午餐区")
        current += 60
    return {
        "route_summary": " → ".join(route_labels), "timeline": timeline,
        "attraction_count": len(ordered),
        "visit_minutes": sum(pois[index].visit_minutes for index in ordered),
        "travel_minutes": round(total_travel), "distance_km": round(total_distance, 2),
        "estimated_end": _format_time(current), "sunset": sunset,
    }


def serialize_poi(poi: Poi, rank: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": poi.name, "score": poi.score, "score_components": poi.score_components,
        "penalty_reasons": poi.penalty_reasons, "section_type": poi.section_type,
        "description": poi.description[:300], "address": poi.address, "hours": poi.hours,
        "price": poi.price, "website": poi.website,
        "coordinates": {"latitude": poi.latitude, "longitude": poi.longitude},
        "coordinate_source": poi.coordinate_source, "map_match_confidence": poi.map_match_confidence,
        "source_pages": sorted(poi.source_pages), "source_page_types": sorted(poi.source_kinds),
        "visit_minutes": poi.visit_minutes, "indoor_probability": poi.indoor_probability,
        "night_view": poi.night_view, "closed_weekdays": sorted(poi.closed_weekdays),
    }
    if rank is not None:
        result["rank"] = rank
    return result
