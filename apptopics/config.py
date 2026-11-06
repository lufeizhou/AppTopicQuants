from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    astro_mcp_url: str = field(default_factory=lambda: _env("ASTRO_MCP_URL", "http://127.0.0.1:8089/mcp"))
    deepseek_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = field(default_factory=lambda: _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    deepseek_model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-flash"))
    db_path: Path = field(default_factory=lambda: ROOT / _env("APPTOPICS_DB", "data/apptopics.duckdb"))
    store: str = field(default_factory=lambda: _env("APPTOPICS_STORE", "us").lower())
    weights_path: Path = field(default_factory=lambda: ROOT / "config" / "model.toml")

    _cache: tuple[float, dict] | None = None

    def weights(self) -> dict:
        """config/model.toml flattened: [stepN.name] / [appendixB.name] / [run.name] -> name. Cached by mtime
        (read many times per scoring run)."""
        m = self.weights_path.stat().st_mtime
        if self._cache is None or self._cache[0] != m:
            with open(self.weights_path, "rb") as f:
                self._cache = (m, flatten_model(tomllib.load(f)))
        return self._cache[1]

    def section(self, name: str) -> dict:
        return self.weights().get(name, {})


def flatten_model(raw: dict) -> dict:
    """Group tables ([step1.universe], [run.sources] ...) are lifted to their own name; a name may appear once."""
    out: dict = {}
    for k, v in raw.items():
        if isinstance(v, dict) and (k.startswith(("step", "appendix")) or k == "run"):
            for name, sec in v.items():
                if name in out:
                    raise ValueError(f"config/model.toml: section '{name}' defined twice")
                out[name] = sec
        else:
            if k in out:
                raise ValueError(f"config/model.toml: section '{k}' defined twice")
            out[k] = v
    return out


settings = Settings()

# Apple genre ids used for chart snapshots. Extend freely.
APPLE_GENRES: dict[str, str] = {
    "6000": "Business", "6001": "Weather", "6002": "Utilities", "6003": "Travel",
    "6004": "Sports", "6005": "Social Networking", "6006": "Reference", "6007": "Productivity",
    "6008": "Photo & Video", "6009": "News", "6010": "Navigation", "6011": "Music",
    "6012": "Lifestyle", "6013": "Health & Fitness", "6014": "Games", "6015": "Finance",
    "6016": "Entertainment", "6017": "Education", "6018": "Books", "6020": "Medical",
    "6021": "Magazines & Newspapers", "6023": "Food & Drink", "6024": "Shopping",
    "6026": "Developer Tools", "6027": "Graphics & Design",
}
