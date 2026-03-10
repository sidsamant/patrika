from __future__ import annotations

import os
from pathlib import Path


ENV_PATH = Path(__file__).with_name(".env")


def load_local_env(env_path: Path = ENV_PATH) -> None:
    """Populate process env from a local .env file without overriding existing values."""
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)

    # The Google client expects GOOGLE_API_KEY. Keep GEMINI_API_KEY as a fallback alias.
    if not os.environ.get("GOOGLE_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]
