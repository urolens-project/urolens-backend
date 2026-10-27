"""Particle-count helpers shared by the readers of a result's counts.

A result keeps the AI's counts in `ai_findings` and the MedTech-settled counts
(AI counts with any corrections applied) in `particle_classes`. Readers that
show counts to patients and physicians must use the settled ones, or a
correction never reaches them.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def toWholeCount(value: Any) -> int:
    """A count as a whole number, rounding half up.

    Accepts ints, floats and numeric text such as `"7.0"` (override values are
    stored as text). Anything else, a negative or a non-finite number gives 0.
    Python's `round` is not used: it rounds halves to even (`round(2.5) == 2`).
    """
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number < 0:
        return 0
    return int(Decimal(str(number)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def settledCounts(aiFindings: Any, particleClasses: Any) -> dict[str, Any]:
    """The counts to report: `ai_findings` overlaid with `particle_classes`.

    The same merge `SmartDiagnosisService.run` uses. `particle_classes` can be
    stored as a list (class names only, no counts); that shape adds nothing.
    """
    ai = aiFindings if isinstance(aiFindings, dict) else {}
    settled = particleClasses if isinstance(particleClasses, dict) else {}
    return {**ai, **settled}
