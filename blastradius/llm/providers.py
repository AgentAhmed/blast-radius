"""LLM provider slots.

Every provider here speaks the OpenAI chat-completions API, so one client works for all of them.
A slot without an API key is simply skipped, so slots can be left as templates until a key is added.
Local Ollama comes last and needs no key; it is tried only if it is running.

Environment variables (see .env.example):
  LLM_PROVIDER_ORDER   comma list, default: groq,gemini,openrouter,custom,ollama
  <NAME>_API_KEY       key for a provider (empty = skipped)
  <NAME>_MODELS        comma list overriding the default models, tried in order
  <NAME>_RPM           requests per minute we allow ourselves for that provider
  CUSTOM_LLM_BASE_URL  any other OpenAI-compatible endpoint (Cerebras, Mistral, Together, ...)
"""
import os
from dataclasses import dataclass, field


@dataclass
class Provider:
    name: str
    base_url: str
    models: list[str]
    api_key_env: str | None = None   # None = no key needed (local model)
    rpm: int = 30
    local: bool = False
    extra_headers: dict = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) if self.api_key_env else None

    @property
    def configured(self) -> bool:
        if not self.base_url or not self.models:
            return False
        return self.local or bool(self.api_key)


# Defaults for each slot. Model lists can be overridden with <NAME>_MODELS without code changes.
_SLOTS = {
    "groq": dict(
        base_url="https://api.groq.com/openai/v1",
        models=["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
        api_key_env="GROQ_API_KEY", rpm=30,
    ),
    "gemini": dict(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        models=["gemini-2.5-flash"],
        api_key_env="GEMINI_API_KEY", rpm=10,
    ),
    "openrouter": dict(
        base_url="https://openrouter.ai/api/v1",
        models=["openai/gpt-oss-20b:free"],
        api_key_env="OPENROUTER_API_KEY", rpm=20,
        extra_headers={"X-Title": "Blast Radius"},
    ),
    "custom": dict(
        base_url="",  # filled from CUSTOM_LLM_BASE_URL
        models=[],    # filled from CUSTOM_MODELS
        api_key_env="CUSTOM_LLM_API_KEY", rpm=30,
    ),
    "ollama": dict(
        base_url="http://localhost:11434/v1",
        models=["qwen3:8b"],
        api_key_env=None, rpm=1000, local=True,
    ),
}

DEFAULT_ORDER = "groq,gemini,openrouter,custom,ollama"


def _env_list(name: str) -> list[str] | None:
    raw = os.getenv(name, "").strip()
    return [m.strip() for m in raw.split(",") if m.strip()] or None


def load_providers() -> list[Provider]:
    """All provider slots in fallback order, configured or not."""
    order = _env_list("LLM_PROVIDER_ORDER") or DEFAULT_ORDER.split(",")
    providers = []
    for name in order:
        if name not in _SLOTS:
            raise ValueError(f"Unknown LLM provider '{name}' in LLM_PROVIDER_ORDER")
        slot = dict(_SLOTS[name])
        prefix = name.upper()
        slot["models"] = _env_list(f"{prefix}_MODELS") or slot["models"]
        if os.getenv(f"{prefix}_RPM"):
            slot["rpm"] = int(os.environ[f"{prefix}_RPM"])
        if name == "custom":
            slot["base_url"] = os.getenv("CUSTOM_LLM_BASE_URL", "")
        if name == "ollama":
            slot["base_url"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/v1"
        providers.append(Provider(name=name, **slot))
    return providers
