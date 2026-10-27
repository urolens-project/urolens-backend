"""Normal-range limits from the Smart Diagnosis rules config.

The flag `ABOVE_NORMAL_MAX` on a field compares a particle's count with the
`normal_range_max` the rule engine itself uses. Those limits live in the engine's
YAML config (`RULE_ENGINE_CONFIG_PATH`, the variable the engine reads), never in
this codebase. A particle can be listed under several conditions with different
limits; the lowest one is used, because a flag is a prompt to look closer and the
conservative reading is the right one.

If the variable is unset or the file cannot be read this returns `{}` and logs
one line; the flag is then simply absent. It never raises.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from ..schemas.patient_portal import PARTICLE_LABELS

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV = "RULE_ENGINE_CONFIG_PATH"


def readNormalMax(path: str) -> dict[str, float]:
    """Lowest `normal_range_max` per particle across every condition in the YAML at `path`.

    Names are returned exactly as configured, including any that are not in
    `PARTICLE_LABELS` (`unknownParticleNames` finds those).

    Raises:
        OSError: the file cannot be opened.
        ValueError: the YAML is malformed or has no `conditions` mapping.
    """
    import yaml  # a dependency of the AI engine; imported here so a missing module only disables the flag

    with open(path, encoding="utf-8") as handle:
        try:
            config: Any = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise ValueError(f"not valid YAML: {exc}") from exc

    conditions = config.get("conditions") if isinstance(config, dict) else None
    if not isinstance(conditions, dict):
        raise ValueError("no 'conditions' mapping")

    lowest: dict[str, float] = {}
    for condition in conditions.values():
        particles = condition.get("particles") if isinstance(condition, dict) else None
        if not isinstance(particles, dict):
            continue
        for name, entry in particles.items():
            limit = entry.get("normal_range_max") if isinstance(entry, dict) else None
            if isinstance(limit, bool) or not isinstance(limit, (int, float)):
                continue
            lowest[str(name)] = min(float(limit), lowest.get(str(name), float(limit)))
    return lowest


def unknownParticleNames(thresholds: dict[str, float]) -> list[str]:
    """Configured particle names that are not in `PARTICLE_LABELS`, sorted."""
    return sorted(name for name in thresholds if name not in PARTICLE_LABELS)


@lru_cache(maxsize=4)
def _loadFrom(path: str) -> dict[str, float]:
    if not path:
        logger.info("%s is not set; ABOVE_NORMAL_MAX field flags are off.", CONFIG_PATH_ENV)
        return {}
    try:
        thresholds = readNormalMax(path)
    except (OSError, ValueError, ImportError) as exc:
        logger.warning(
            "Could not read the rules config at %s (%s); ABOVE_NORMAL_MAX field flags are off.",
            path, exc,
        )
        return {}
    unknown = unknownParticleNames(thresholds)
    if unknown:
        logger.error(
            "Rules config lists particles that are not in PARTICLE_LABELS: %s. "
            "They are ignored for field flags.", unknown,
        )
    return {name: limit for name, limit in thresholds.items() if name in PARTICLE_LABELS}


def loadNormalMax() -> dict[str, float]:
    """`{particle: normal_range_max}` for flags, cached per config path."""
    return _loadFrom(os.environ.get(CONFIG_PATH_ENV, ""))


def clearCache() -> None:
    """Forget the cached config (tests, or after the file is replaced)."""
    _loadFrom.cache_clear()
