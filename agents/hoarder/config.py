from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SourceConfig:
    id: str
    enabled: bool
    path: str


@dataclass(frozen=True)
class HoarderConfig:
    sources: list[SourceConfig]


def _parse_source(item: dict[str, Any]) -> SourceConfig:
    return SourceConfig(
        id=str(item.get("id", "")).strip(),
        enabled=bool(item.get("enabled", False)),
        path=str(item.get("path", "")).strip(),
    )


def load_hoarder_config(config_path: str | Path | None = None) -> HoarderConfig:
    path = Path(config_path) if config_path else Path(__file__).with_name("config.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    raw_sources = data.get("sources", [])
    sources = [_parse_source(item) for item in raw_sources if isinstance(item, dict)]
    sources = [s for s in sources if s.id and s.path]

    if not sources:
        raise ValueError("No sources configured in newsletter-adk/agents/hoarder/config.yaml")

    return HoarderConfig(sources=sources)
