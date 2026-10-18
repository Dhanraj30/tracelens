"""Environment configuration. All values come from env vars (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    falkordb_host: str = field(default_factory=lambda: _env("FALKORDB_HOST", "localhost"))
    falkordb_port: int = field(default_factory=lambda: int(_env("FALKORDB_PORT", "6379")))
    falkordb_username: str = field(default_factory=lambda: _env("FALKORDB_USERNAME", ""))
    falkordb_password: str = field(default_factory=lambda: _env("FALKORDB_PASSWORD", ""))
    falkordb_ssl: bool = field(default_factory=lambda: _env("FALKORDB_SSL", "false").lower() == "true")
    graph_name: str = field(default_factory=lambda: _env("GRAPH_NAME", "tracelens"))
    auto_seed: bool = field(default_factory=lambda: _env("AUTO_SEED", "true").lower() == "true")
    query_timeout_ms: int = field(default_factory=lambda: int(_env("QUERY_TIMEOUT_MS", "5000")))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "20")))
    # optional second provider, tried automatically when the primary fails (then the deterministic template)
    llm_fallback_base_url: str = field(default_factory=lambda: _env("LLM_FALLBACK_BASE_URL", ""))
    llm_fallback_api_key: str = field(default_factory=lambda: _env("LLM_FALLBACK_API_KEY", ""))
    llm_fallback_model: str = field(default_factory=lambda: _env("LLM_FALLBACK_MODEL", ""))

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def llm_providers(self) -> list[tuple[str, str, str]]:
        """(base_url, api_key, model) in the order to try."""
        out = []
        if self.llm_configured:
            out.append((self.llm_base_url, self.llm_api_key, self.llm_model))
        if self.llm_fallback_base_url and self.llm_fallback_api_key and self.llm_fallback_model:
            out.append((self.llm_fallback_base_url, self.llm_fallback_api_key, self.llm_fallback_model))
        return out


settings = Settings()
