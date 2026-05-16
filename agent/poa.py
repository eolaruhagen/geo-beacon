"""Pure POA math functions — no DB or FastAPI imports.

All functions take primitives and return plain Python values. The caller
is responsible for normalizing raw weights to probabilities.
"""
from __future__ import annotations

import math


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def initial_poa_weights(
    cell_centers: list[tuple[float, float]],  # (lat, lon) per cell
    cell_elev_m: list[float],
    cell_cover: list[str],
    cell_has_trail: list[bool],
    pls_lat: float,
    pls_lon: float,
    pls_elev_m: float,
    sigma_m: float = 750.0,
) -> list[float]:
    """Compute the raw_w array per spec §7.

    Returns un-normalized weights; caller normalizes so sum == 1.
    Formula per cell:
        dist_term     = exp(-d² / (2·σ²))
        trail_term    = 1.5 if cell overlaps trail else 1.0
        downhill_term = 1.0 + 0.002 · max(0, pls_elev - cell_elev)
        cover_term    = 0.7 if dominant_cover='dense' else 1.0
        raw_w = dist_term · trail_term · downhill_term · cover_term
    """
    sigma2 = 2.0 * sigma_m * sigma_m
    weights: list[float] = []
    for (lat, lon), elev, cover, has_trail in zip(
        cell_centers, cell_elev_m, cell_cover, cell_has_trail
    ):
        d = _haversine_m(pls_lat, pls_lon, lat, lon)
        dist_term = math.exp(-(d * d) / sigma2)
        trail_term = 1.5 if has_trail else 1.0
        downhill_term = 1.0 + 0.002 * max(0.0, pls_elev_m - elev)
        cover_term = 0.7 if cover == "dense" else 1.0
        weights.append(dist_term * trail_term * downhill_term * cover_term)
    return weights
