"""Shared Python SDK environment, without inheriting an event-emission channel."""

import os
from pathlib import Path
import sys

SDK_ROOT = Path(__file__).resolve().parents[2]


def python_environment() -> dict[str, str]:
    env = os.environ.copy()
    package_root = str(SDK_ROOT)
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    env["HARNESS_PYTHON"] = sys.executable
    # SDK availability is independent of access to a live source's IPC/state.
    env.pop("HARNESS_GENERATOR_SOCKET", None)
    env.pop("HARNESS_GENERATOR_FD", None)
    env.pop("HARNESS_GENERATOR_TOKEN", None)
    env.pop("HARNESS_GENERATOR_STATE_DIR", None)
    return env
