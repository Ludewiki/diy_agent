"""Capacity-constrained K-Medoids used to group nearby attractions by day."""

from __future__ import annotations

import math

from .routing import shortest_open_path, symmetric_matrix
from .schemas import Poi


def _initial_medoids(pois: list[Poi], distance: list[list[float]], k: int) -> list[int]:
    first = max(range(len(pois)), key=lambda index: pois[index].score)
    medoids = [first]
    while len(medoids) < k:
        candidates = [index for index in range(len(pois)) if index not in medoids]
        medoids.append(max(candidates, key=lambda index: (min(distance[index][medoid] for medoid in medoids), pois[index].score)))
    return medoids


def _assign_to_medoids(
    pois: list[Poi], distance: list[list[float]], medoids: list[int],
    capacity_minutes: float, transfer_buffer_minutes: int = 15,
) -> tuple[list[list[int]], list[int]]:
    clusters = [[medoid] for medoid in medoids]
    loads = [pois[medoid].visit_minutes for medoid in medoids]
    remaining = [index for index in range(len(pois)) if index not in medoids]

    def regret(index: int) -> tuple[float, int]:
        ordered = sorted(distance[index][medoid] for medoid in medoids)
        difference = ordered[1] - ordered[0] if len(ordered) > 1 else ordered[0]
        return difference, pois[index].visit_minutes

    remaining.sort(key=regret, reverse=True)
    unassigned: list[int] = []
    for index in remaining:
        required = pois[index].visit_minutes + transfer_buffer_minutes
        feasible = [i for i, _ in enumerate(medoids) if loads[i] + required <= capacity_minutes]
        if not feasible:
            unassigned.append(index)
            continue
        chosen = min(feasible, key=lambda i: (distance[index][medoids[i]], loads[i]))
        clusters[chosen].append(index)
        loads[chosen] += required
    return clusters, unassigned


def capacitated_k_medoids(
    pois: list[Poi], travel_minutes: list[list[float]], k: int,
    capacity_minutes: float, max_iterations: int = 12,
) -> tuple[list[list[int]], list[int], list[int]]:
    distance = symmetric_matrix(travel_minutes)
    medoids = _initial_medoids(pois, distance, k)
    best_clusters: list[list[int]] = []
    best_medoids = medoids[:]
    best_unassigned = list(range(len(pois)))
    best_objective = math.inf
    for _ in range(max_iterations):
        clusters, unassigned = _assign_to_medoids(pois, distance, medoids, capacity_minutes)
        objective = sum(distance[item][medoids[i]] for i, cluster in enumerate(clusters) for item in cluster) + sum(1000 + pois[item].score for item in unassigned)
        if objective < best_objective:
            best_clusters, best_medoids, best_unassigned = [cluster[:] for cluster in clusters], medoids[:], unassigned[:]
            best_objective = objective
        new_medoids = [min(cluster, key=lambda candidate: sum(distance[candidate][other] for other in cluster)) for cluster in clusters]
        if new_medoids == medoids:
            break
        medoids = new_medoids
    return best_clusters, best_medoids, best_unassigned


def trim_clusters_to_capacity(
    clusters: list[list[int]], pois: list[Poi], matrix: list[list[float]],
    capacity_without_lunch: int,
) -> tuple[list[list[int]], list[int]]:
    dropped: list[int] = []
    trimmed: list[list[int]] = []
    for original in clusters:
        cluster = original[:]
        while len(cluster) > 1:
            _, route_minutes = shortest_open_path(cluster, matrix)
            if sum(pois[index].visit_minutes for index in cluster) + route_minutes <= capacity_without_lunch:
                break
            removable = min(cluster, key=lambda index: (pois[index].score, -pois[index].visit_minutes))
            cluster.remove(removable)
            dropped.append(removable)
        trimmed.append(cluster)
    return trimmed, dropped
