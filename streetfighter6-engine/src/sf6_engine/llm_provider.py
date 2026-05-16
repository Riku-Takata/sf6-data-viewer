"""LLM プロバイダーの抽象インターフェース。

環境変数 LLM_PROVIDER で切り替え可能:
  ollama  : Ollama (Gemma 等のローカル/クラウドLLM) ← デフォルト
  gemini  : Gemini Flash API (Google)

設計原則: このファイルはどのプロバイダーにも依存しない。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    """generate() の返り値。"""
    text: str
    usage: dict | None = field(default=None)  # {"prompt_tokens": N, "completion_tokens": N}


class LLMProvider(ABC):
    """LLM バックエンドの共通インターフェース。

    各メソッドは async。呼び出し側は asyncio.run() で実行するか、
    async コンテキスト内で await する。
    """

    @abstractmethod
    async def generate(self, prompt: str, system: str = "") -> LLMResponse:
        """テキスト生成。

        Args:
            prompt: ユーザープロンプト。
            system: システムプロンプト (空文字はデフォルト動作)。

        Returns:
            LLMResponse: 生成テキストとオプションのトークン使用量。
        """
        ...

    @abstractmethod
    async def generate_structured(
        self, prompt: str, schema: dict, system: str = ""
    ) -> dict:
        """構造化出力 (JSON)。

        Args:
            prompt: ユーザープロンプト。
            schema: 出力に期待する JSON Schema (dict)。
            system: 追加のシステムプロンプト。

        Returns:
            dict: JSON パース済みの出力。スキーマに準拠しない場合は ValueError。
        """
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """テキスト埋め込みベクトルを生成。

        Args:
            text: 埋め込み対象のテキスト。

        Returns:
            list[float]: 埋め込みベクトル。
        """
        ...
