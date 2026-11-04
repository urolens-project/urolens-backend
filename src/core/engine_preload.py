"""Loads the AI engine when the server starts, instead of on the first upload.

The first inference otherwise pays for loading the weights, and on a small
instance that makes the first upload after every deploy or restart time out.
`/health/ready` reads `isModelReady()`, so the load balancer only sends traffic
once the model is in memory.
"""
from __future__ import annotations

import logging
import os
import time

import numpy as np

logger = logging.getLogger(__name__)

# Uvicorn only configures its own loggers, so an INFO line from this module's logger would
# be dropped; the load time goes to uvicorn's, which prints at INFO.
_startupLog = logging.getLogger("uvicorn.error")

_modelReady = False


def isModelReady() -> bool:
    """Whether the engine was loaded and ran its warm-up at startup."""
    return _modelReady


def _torchThreads() -> int:
    raw = os.getenv("TORCH_NUM_THREADS", "").strip()
    if not raw:
        return 1
    try:
        threads = int(raw)
    except ValueError:
        threads = 0
    if threads < 1:
        logger.warning("TORCH_NUM_THREADS must be a whole number >= 1, got %r; using 1.", raw)
        return 1
    return threads


def preloadEngine() -> bool:
    """Load the engine and the input gate and run one inference on a blank image.

    Never raises: a failure is logged and `isModelReady()` stays False, so the app
    still starts (uploads then load the engine lazily, or fail with their own
    error) while `/health/ready` reports the model as not ready.

    Returns:
        True if the engine is loaded and warm.
    """
    global _modelReady
    _modelReady = False
    start = time.perf_counter()
    try:
        import torch
        from urolens_ai.inference.input_gate import get_gate
        from urolens_ai.inference.yolo_engine import get_engine

        torch.set_num_threads(_torchThreads())
        engine = get_engine()
        blank = np.zeros((480, 640, 3), dtype=np.float32)
        # Straight to the models: the input gate would reject a blank image, but the
        # first real image must not pay for loading either model or for the first pass.
        get_gate().slide_probability(blank)
        engine.run(blank)
    except Exception:
        logger.exception("AI engine preload failed; /health/ready will report the model as not ready")
        return False
    _modelReady = True
    _startupLog.info("AI engine preloaded in %.1f s", time.perf_counter() - start)
    return True
