from datetime import datetime, timezone

from travel_planner.clustering import capacitated_k_medoids
from travel_planner.routing import shortest_open_path
from travel_planner.schemas import Poi, SourcePage
from travel_planner.scoring import score_poi
from travel_planner.sources import extract_pois, is_university_attraction
from travel_planner.weather_assignment import hungarian, weather_badness


def _poi(name: str, score: float, visit_minutes: int = 90) -> Poi:
    return Poi(key=name, name=name, section_type="see", score=score, visit_minutes=visit_minutes)


def test_extracts_and_merges_wikivoyage_listings() -> None:
    pages = [
        SourcePage("上海", 1, 10, "2026-01-01T00:00:00Z", "https://example/1", "city", "{{see|name=外滩|lat=31.24|long=121.49|content=著名历史建筑群}}"),
        SourcePage("上海/黄浦", 2, 11, "2026-02-01T00:00:00Z", "https://example/2", "district", "{{see|name=外滩|hours=全天|price=免费}}"),
    ]
    pois = extract_pois(pages)
    assert len(pois) == 1
    assert pois[0].source_pages == {"上海", "上海/黄浦"}
    assert pois[0].hours == "全天"


def test_university_filter_does_not_remove_university_museum() -> None:
    assert is_university_attraction(_poi("复旦大学", 10))
    assert not is_university_attraction(_poi("University Museum", 10))


def test_scoring_is_explainable() -> None:
    poi = Poi(
        key="museum", name="城市博物馆", section_type="see", description="推荐的历史艺术博物馆",
        latitude=31.2, longitude=121.4, hours="09:00-17:00", price="50", website="https://example.com",
        source_pages={"City", "District"}, source_kinds={"city", "district"},
        source_revision_times=["2026-01-01T00:00:00Z"],
    )
    weights = {"cross_page": .2, "completeness": .2, "editorial": .2, "preference": .25, "freshness": .15}
    score_poi(poi, ["历史", "博物馆"], weights, datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert poi.score > 0
    assert set(poi.score_components) == {"cross_page", "completeness", "editorial", "preference", "freshness", "penalty"}


def test_capacity_clustering_and_route() -> None:
    pois = [_poi("A", 100), _poi("B", 90), _poi("C", 80), _poi("D", 70)]
    matrix = [[0, 5, 50, 55], [5, 0, 45, 50], [50, 45, 0, 5], [55, 50, 5, 0]]
    clusters, _, unassigned = capacitated_k_medoids(pois, matrix, 2, 240)
    assert not unassigned
    assert sorted(map(len, clusters)) == [2, 2]
    route, cost = shortest_open_path([0, 1], matrix)
    assert set(route) == {0, 1}
    assert cost == 5


def test_hungarian_and_weather_badness() -> None:
    assert hungarian([[9, 1], [2, 8]]) == [1, 0]
    clear, _ = weather_badness({"rain_probability_pct": 0, "wind_speed_max_kmh": 10, "temp_max_c": 25}, .5, .25, .25)
    storm, _ = weather_badness({"rain_probability_pct": 100, "wind_speed_max_kmh": 50, "temp_max_c": 40}, .5, .25, .25)
    assert clear < storm
