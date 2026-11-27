"""CloudWatch Embedded Metric Format: one JSON line to stdout -> metric, no API call needed."""
from __future__ import annotations

import json
import os
import time


def emit(name: str, value: float = 1, unit: str = "Count", **dims: str) -> None:
    if os.environ.get("KARTOPS_MODE", "local") != "aws" and not os.environ.get("KARTOPS_METRICS"):
        return
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000),
                 "CloudWatchMetrics": [{"Namespace": "KartOps", "Dimensions": [list(dims)] if dims else [[]],
                                        "Metrics": [{"Name": name, "Unit": unit}]}]},
        name: value, **dims}))
