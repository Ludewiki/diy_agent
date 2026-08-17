"""Match attraction clusters to forecast days with a minimum-cost assignment."""

from __future__ import annotations

from datetime import date
import math
from typing import Any

from .schemas import Poi


def weather_badness(
    weather: dict[str, Any], rain_weight: float, wind_weight: float, heat_weight: float,
) -> tuple[float, dict[str, float]]:
    rain = min(100.0, max(0.0, float(weather.get("rain_probability_pct", 0))))
    wind_speed = max(0.0, float(weather.get("wind_speed_max_kmh", 0)))
    temperature = float(weather.get("temp_max_c", 25))
    wind = min(100.0, max(0.0, (wind_speed - 20) / 30 * 100))
    heat = min(100.0, max(0.0, (temperature - 30) / 10 * 100))
    badness = (rain_weight * rain + wind_weight * wind + heat_weight * heat) / (rain_weight + wind_weight + heat_weight)
    return round(badness, 2), {"rain": round(rain, 2), "wind": round(wind, 2), "heat": round(heat, 2)}


def cluster_indoor_ratio(cluster: list[int], pois: list[Poi]) -> float:
    total = sum(pois[index].visit_minutes for index in cluster)
    return sum(pois[index].visit_minutes * pois[index].indoor_probability for index in cluster) / total if total > 0 else 0.5


def hungarian(cost: list[list[float]]) -> list[int]:
    row_count, column_count = len(cost), len(cost[0]) if cost else 0
    if row_count == 0 or row_count > column_count:
        raise ValueError("匈牙利算法要求行数不大于列数且矩阵非空")
    u, v = [0.0] * (row_count + 1), [0.0] * (column_count + 1)
    p, way = [0] * (column_count + 1), [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        p[0], column0 = row, 0
        min_values, used = [math.inf] * (column_count + 1), [False] * (column_count + 1)
        while True:
            used[column0] = True
            current_row, delta, column1 = p[column0], math.inf, 0
            for column in range(1, column_count + 1):
                if used[column]:
                    continue
                current = cost[current_row - 1][column - 1] - u[current_row] - v[column]
                if current < min_values[column]:
                    min_values[column], way[column] = current, column0
                if min_values[column] < delta:
                    delta, column1 = min_values[column], column
            for column in range(column_count + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    min_values[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if p[column] != 0:
            assignment[p[column] - 1] = column - 1
    return assignment


def assignment_costs(
    clusters: list[list[int]], pois: list[Poi], weather_days: list[dict[str, Any]],
    weather_badness_values: list[float],
) -> tuple[list[list[float]], list[dict[str, Any]]]:
    matrix: list[list[float]] = []
    detail_rows: list[dict[str, Any]] = []
    for cluster_index, cluster in enumerate(clusters):
        indoor_ratio = cluster_indoor_ratio(cluster, pois)
        crowd_ratio = sum(pois[index].crowd_sensitive for index in cluster) / len(cluster)
        has_night = any(pois[index].night_view for index in cluster)
        row: list[float] = []
        row_details: list[dict[str, float]] = []
        for day_index, weather in enumerate(weather_days):
            travel_date = date.fromisoformat(str(weather["date"]))
            badness = weather_badness_values[day_index]
            weather_mismatch = badness * (1 - indoor_ratio)
            closed = any(travel_date.weekday() in pois[index].closed_weekdays for index in cluster)
            closed_penalty = 10_000.0 if closed else 0.0
            crowd_penalty = 12.0 * crowd_ratio if travel_date.weekday() >= 5 else 0.0
            schedule_penalty = (20.0 if has_night and badness >= 55 else 0.0) + (3.0 if has_night and not weather.get("sunset") else 0.0)
            total = weather_mismatch + closed_penalty + crowd_penalty + schedule_penalty
            row.append(total)
            row_details.append({"weather_mismatch": round(weather_mismatch, 2), "closed_penalty": round(closed_penalty, 2), "crowd_penalty": round(crowd_penalty, 2), "schedule_penalty": round(schedule_penalty, 2), "total": round(total, 2)})
        matrix.append(row)
        detail_rows.append({"cluster": cluster_index + 1, "indoor_ratio": round(indoor_ratio, 3), "cost_by_date": {str(weather_days[index]["date"]): row_details[index] for index in range(len(weather_days))}})
    return matrix, detail_rows
