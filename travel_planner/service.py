"""Application service that composes data, scoring, clustering and routing."""

from __future__ import annotations

from datetime import date, datetime, timezone
import logging
import os
from typing import Any
from urllib.parse import urlparse

import requests

from tool_errors import tool_error
from .clustering import capacitated_k_medoids, trim_clusters_to_capacity
from .routing import OrsClient, build_day_route, serialize_poi, symmetric_matrix
from .schemas import TravelPlannerInput
from .scoring import score_poi
from .sources import WikivoyageClient, extract_pois, is_university_attraction
from .weather_assignment import assignment_costs, cluster_indoor_ratio, hungarian, weather_badness

logger = logging.getLogger(__name__)


def _request_status(exc: requests.RequestException) -> int | None:
    return exc.response.status_code if exc.response is not None else None


def _upstream_name(exc: requests.RequestException) -> str:
    request = getattr(exc, "request", None)
    response = getattr(exc, "response", None)
    url = getattr(request, "url", None) or getattr(response, "url", None) or ""
    host = (urlparse(str(url)).hostname or "").lower()
    if "openrouteservice.org" in host:
        return "OpenRouteService"
    if "wikivoyage.org" in host or "wikimedia.org" in host:
        return "Wikivoyage"
    return "upstream"


def plan_trip(config: TravelPlannerInput) -> dict[str, Any]:
    """Execute one deterministic planning run and return a serializable result."""
    city = config.city
    ors_api_key = os.getenv("ORS_API_KEY")
    if not ors_api_key:
        return tool_error(
            "MISSING_ORS_API_KEY",
            "缺少 ORS_API_KEY，无法把景点映射到地图并计算交通时间矩阵。",
            details={"required_environment_variable": "ORS_API_KEY"}, query_city=city,
        )
    weather_days = sorted(
        (item.model_dump() for item in config.weather_days), key=lambda item: item["date"]
    )[: config.trip_days]
    weights = {
        "cross_page": config.alpha_cross_page, "completeness": config.beta_completeness,
        "editorial": config.gamma_editorial, "preference": config.delta_preference,
        "freshness": config.epsilon_freshness,
    }
    warnings: list[str] = []
    logger.info("travel planning started", extra={"event": "tool_started", "tool_name": "plan_wikivoyage_trip", "city": city})
    try:
        pages = WikivoyageClient(config.language).collect_pages(city, config.max_related_pages)
        if not pages:
            return tool_error(
                "WIKIVOYAGE_PAGE_NOT_FOUND",
                f"在 {config.language}.wikivoyage.org 中没有找到与“{city}”匹配的可用页面。",
                query_city=city,
            )
        pois = extract_pois(pages)
        if not pois:
            return tool_error(
                "NO_LISTINGS", "找到城市页面，但未解析到 See/Do 景点 Listing。可尝试更换 language。",
                details={"source_pages": [page.title for page in pages]}, query_city=city,
            )
        university_filtered_count = 0
        if config.exclude_universities:
            before_filter_count = len(pois)
            pois = [poi for poi in pois if not is_university_attraction(poi)]
            university_filtered_count = before_filter_count - len(pois)
            if not pois:
                return tool_error("NO_LISTINGS_AFTER_FILTERING", "排除大学和校园类景点后，没有剩余可规划景点。", query_city=city)

        ors_client = OrsClient(ors_api_key)
        geocoder_failure: tuple[int | None, str] | None = None
        for poi in pois:
            if poi.latitude is not None and poi.longitude is not None:
                continue
            query = ", ".join(part for part in (poi.name, poi.address, city) if part)
            try:
                match = ors_client.geocode(query)
            except requests.RequestException as exc:
                status_code = _request_status(exc)
                geocoder_failure = (status_code, _upstream_name(exc))
                if status_code in {401, 403}:
                    warnings.append(
                        "ORS Geocoder 未授权；已跳过缺少坐标的景点，"
                        "并继续使用 Wikivoyage 自带坐标。"
                    )
                else:
                    warnings.append(
                        "ORS Geocoder 暂时不可用；已跳过缺少坐标的景点。"
                    )
                logger.warning(
                    "ORS geocoder unavailable; using source coordinates",
                    extra={
                        "event": "upstream_degraded",
                        "tool_name": "plan_wikivoyage_trip",
                        "city": city,
                        "error_code": (
                            "ORS_GEOCODER_FORBIDDEN"
                            if status_code in {401, 403}
                            else "ORS_GEOCODER_UNAVAILABLE"
                        ),
                    },
                )
                break
            if match is None:
                continue
            latitude, longitude, confidence = match
            poi.map_match_confidence = round(confidence, 3)
            if confidence >= 0.45:
                poi.latitude, poi.longitude = latitude, longitude
                poi.coordinate_source = "openrouteservice_geocoder"

        now = datetime.now(timezone.utc)
        for poi in pois:
            score_poi(poi, config.interests, weights, now)
        ranked_all = sorted(pois, key=lambda item: item.score, reverse=True)
        usable = [poi for poi in ranked_all if not poi.permanently_closed and poi.latitude is not None and poi.longitude is not None and poi.visit_minutes <= config.daily_minutes - 60]
        if len(usable) < config.trip_days:
            if geocoder_failure is not None:
                status_code, provider = geocoder_failure
                return tool_error(
                    "ORS_GEOCODER_UNAVAILABLE",
                    "ORS Geocoder 不可用，且 Wikivoyage 自带坐标不足以生成路线；"
                    "请为 ORS Key 开通 Geocoder 权限后重试。",
                    details={
                        "provider": provider,
                        "status_code": status_code,
                        "parsed_count": len(pois),
                        "usable_count": len(usable),
                        "required_count": config.trip_days,
                    },
                    retryable=status_code not in {401, 403},
                    query_city=city,
                )
            return tool_error(
                "INSUFFICIENT_MAPPABLE_ATTRACTIONS", "可可靠映射到地图且未疑似停业的景点少于游玩天数。",
                details={"parsed_count": len(pois), "usable_count": len(usable), "required_count": config.trip_days}, query_city=city,
            )

        total_capacity = config.trip_days * (config.daily_minutes - 60)
        selected = []
        estimated_load = 0
        for poi in usable:
            load = poi.visit_minutes + (15 if selected else 0)
            if len(selected) < config.trip_days or estimated_load + load <= total_capacity:
                selected.append(poi)
                estimated_load += load
            if len(selected) >= config.max_candidates:
                break
        if len(selected) < config.trip_days:
            return tool_error("CAPACITY_TOO_SMALL", "每日时间预算不足以容纳至少一个景点组。", query_city=city)

        coordinates = [(float(poi.latitude), float(poi.longitude)) for poi in selected]
        travel_minutes, distance_km, fallback_count = ors_client.matrix(coordinates, config.map_profile)
        if fallback_count:
            warnings.append(f"ORS Matrix 有 {fallback_count} 个不可达单元，已用直线距离的保守估算替代。")
        capacity_without_lunch = config.daily_minutes - 60
        clusters, _, unassigned = capacitated_k_medoids(selected, travel_minutes, config.trip_days, capacity_without_lunch)
        clusters, capacity_dropped = trim_clusters_to_capacity(clusters, selected, travel_minutes, capacity_without_lunch)
        symmetric_travel = symmetric_matrix(travel_minutes)
        medoids = [min(cluster, key=lambda candidate: sum(symmetric_travel[candidate][other] for other in cluster)) for cluster in clusters]
        dropped_indices = sorted(set(unassigned + capacity_dropped))
        if dropped_indices:
            warnings.append("部分低优先级景点因容量或实际交通时间约束未排入日程：" + "、".join(selected[index].name for index in dropped_indices))

        badness_values: list[float] = []
        weather_components: list[dict[str, float]] = []
        for forecast in weather_days:
            badness, components = weather_badness(forecast, config.bad_weather_rain_weight, config.bad_weather_wind_weight, config.bad_weather_heat_weight)
            badness_values.append(badness)
            weather_components.append(components)
        cost_matrix, assignment_details = assignment_costs(clusters, selected, weather_days, badness_values)
        cluster_to_day = hungarian(cost_matrix)

        itinerary_by_day: list[dict[str, Any]] = []
        for cluster_index, day_index in enumerate(cluster_to_day):
            cluster = clusters[cluster_index]
            forecast = weather_days[day_index]
            travel_date = date.fromisoformat(str(forecast["date"]))
            closure_conflicts = [selected[index].name for index in cluster if travel_date.weekday() in selected[index].closed_weekdays]
            if closure_conflicts:
                warnings.append(f"{forecast['date']} 仍存在闭馆冲突，请在出发前复核：" + "、".join(closure_conflicts))
            itinerary_by_day.append({
                "date": forecast["date"], "weekday": travel_date.strftime("%A"), "weather": forecast,
                "bad_weather_score": badness_values[day_index], "bad_weather_components": weather_components[day_index],
                "cluster": cluster_index + 1, "medoid": selected[medoids[cluster_index]].name,
                "indoor_ratio": round(cluster_indoor_ratio(cluster, selected), 3),
                "assignment_cost": round(cost_matrix[cluster_index][day_index], 2),
                "closure_conflicts": closure_conflicts,
                "attractions": [serialize_poi(selected[index]) for index in cluster],
                **build_day_route(cluster, selected, travel_minutes, distance_km, forecast.get("sunset")),
            })
        itinerary_by_day.sort(key=lambda item: item["date"])

        if not os.getenv("WIKIMEDIA_USER_AGENT"):
            warnings.append("未设置 WIKIMEDIA_USER_AGENT；正式运行时应配置项目名和可联系信息。")
        warnings.extend((
            "开放时间、价格、停业状态来自社区维护内容，必须在出发前到景点官网复核。",
            "日内时间轴是估算值；ORS profile 不包含公共交通班次，当前仅支持步行、驾车或骑行矩阵。",
        ))
        scheduled_indices = {index for cluster in clusters for index in cluster}
        scheduled_pois = sorted((selected[index] for index in scheduled_indices), key=lambda item: item.score, reverse=True)
        scheduled_ids = {id(item) for item in scheduled_pois}
        excluded = [item for item in ranked_all if id(item) not in scheduled_ids][:10]
        logger.info("travel planning completed", extra={"event": "tool_succeeded", "tool_name": "plan_wikivoyage_trip", "city": city})
        return {
            "status": "ok", "city": city, "language": config.language, "trip_days": config.trip_days,
            "map_profile": config.map_profile, "daily_minutes": config.daily_minutes,
            "filters_applied": {"exclude_universities": config.exclude_universities, "university_attractions_removed": university_filtered_count},
            "score_formula": "alpha*CrossPage + beta*Completeness + gamma*Editorial + delta*Preference + epsilon*Freshness - Penalty",
            "score_weights": weights, "weather_formula": "a*Rain + b*Wind + c*Heat",
            "weather_weights": {"rain": config.bad_weather_rain_weight, "wind": config.bad_weather_wind_weight, "heat": config.bad_weather_heat_weight},
            "attraction_ranking": [serialize_poi(poi, rank=index + 1) for index, poi in enumerate(scheduled_pois)],
            "itinerary": itinerary_by_day, "date_cluster_assignment_costs": assignment_details,
            "excluded_candidates": [serialize_poi(poi) for poi in excluded],
            "source_pages": [{"title": page.title, "url": page.url, "page_id": page.page_id, "revision_id": page.revision_id, "revision_timestamp": page.revision_timestamp, "page_type": page.kind} for page in pages],
            "attribution": {"content_source": "Wikivoyage contributors", "content_license": "CC BY-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-sa/4.0/", "map_services": "OpenRouteService Geocoder and Matrix API"},
            "warnings": warnings,
        }
    except requests.Timeout:
        logger.warning("upstream timeout", extra={"event": "tool_failed", "tool_name": "plan_wikivoyage_trip", "city": city, "error_code": "UPSTREAM_TIMEOUT"})
        return tool_error(
            "UPSTREAM_TIMEOUT",
            "Wikivoyage 或 OpenRouteService 请求超时，请稍后重试。",
            retryable=True,
            query_city=city,
        )
    except requests.RequestException as exc:
        status_code = _request_status(exc)
        provider = _upstream_name(exc)
        if provider == "OpenRouteService" and status_code in {401, 403}:
            error_code = "ORS_AUTH_FAILED"
            message = "OpenRouteService 鉴权失败，请检查或轮换 ORS_API_KEY。"
            retryable = False
        elif status_code == 429:
            error_code = "UPSTREAM_RATE_LIMITED"
            message = f"{provider} 已达到请求频率上限，请稍后重试。"
            retryable = True
        else:
            error_code = "UPSTREAM_HTTP_ERROR"
            message = f"访问 {provider} 失败。"
            retryable = status_code is None or status_code >= 500
        logger.error(
            "upstream request failed",
            extra={
                "event": "tool_failed",
                "tool_name": "plan_wikivoyage_trip",
                "city": city,
                "error_code": error_code,
            },
        )
        return tool_error(
            error_code,
            message,
            details={"provider": provider, "status_code": status_code},
            retryable=retryable,
            query_city=city,
        )
    except (ValueError, RuntimeError) as exc:
        logger.exception("travel planning failed", extra={"event": "tool_failed", "tool_name": "plan_wikivoyage_trip", "city": city, "error_code": "PLANNING_ERROR"})
        return tool_error("PLANNING_ERROR", str(exc), query_city=city)
