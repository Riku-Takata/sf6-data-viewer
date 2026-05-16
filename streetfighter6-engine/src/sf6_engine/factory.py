"""LLM プロバイダーのファクトリー。

環境変数 LLM_PROVIDER でバックエンドを切り替える:
  ollama  : Ollama (Gemma 等) ← デフォルト
  gemini  : Gemini Flash (Google API)

使い方:
  from sf6_engine.factory import create_provider
  provider = create_provider()           # 環境変数で自動選択
  provider = create_provider("ollama")   # 明示的に指定
"""
from __future__ import annotations

import os

from sf6_engine.llm_provider import LLMProvider


def create_provider(provider_name: str | None = None) -> LLMProvider:
    """環境変数または引数でバックエンドを選択して LLMProvider を返す。

    Args:
        provider_name: "ollama" / "gemini"。None の場合は LLM_PROVIDER 環境変数を参照。
                       それも未設定なら "ollama" をデフォルトとして使用。

    Returns:
        LLMProvider の具体実装インスタンス。

    Raises:
        ValueError: 不明な provider_name が指定された場合。
        RuntimeError: 必要な環境変数が未設定の場合。
    """
    name = provider_name or os.getenv("LLM_PROVIDER", "ollama")

    if name == "ollama":
        from sf6_engine.ollama_provider import OllamaProvider
        return OllamaProvider(
            model=os.getenv("OLLAMA_MODEL", "gemma4:e2b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            embed_model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        )

    if name == "gemini":
        from sf6_engine.gemini_provider import GeminiProvider
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=gemini の場合、GEMINI_API_KEY が必要です")
        return GeminiProvider(api_key=api_key)

    raise ValueError(
        f"不明な LLM プロバイダー: '{name}'. "
        "LLM_PROVIDER 環境変数に 'ollama' または 'gemini' を設定してください。"
    )
