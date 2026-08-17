"""Backward-compatible imports for the modular travel planner.

New code may import from :mod:`travel_planner`; existing Agent code can keep
using ``from travel_planner_tool import plan_wikivoyage_trip``.
"""

from travel_planner import Poi, SourcePage, TravelPlannerInput, WeatherDayInput
from travel_planner import plan_wikivoyage_trip
from travel_planner.clustering import capacitated_k_medoids
from travel_planner.routing import shortest_open_path
from travel_planner.scoring import score_poi
from travel_planner.weather_assignment import hungarian, weather_badness

__all__ = [
    "Poi", "SourcePage", "TravelPlannerInput", "WeatherDayInput",
    "capacitated_k_medoids", "hungarian", "plan_wikivoyage_trip",
    "score_poi", "shortest_open_path", "weather_badness",
]
