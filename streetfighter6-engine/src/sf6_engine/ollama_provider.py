"""Ollama バックエンドの LLMProvider 実装。

Ollama (https://ollama.ai) は Gemma, Llama, Mistral 等のオープンモデルを
ローカルまたはリモートサーバーで実行するためのランタイム。

環境変数:
  OLLAMA_BASE_URL    : Ollama サーバーの URL (default: http://localhost:11434)
  OLLAMA_MODEL       : 使用モデル名 (default: gemma3:4b)
  OLLAMA_EMBED_MODEL : 埋め込みモデル名 (default: nomic-embed-text)

AWS EC2 での利用例:
  OLLAMA_BASE_URL=http://<ec2-private-ip>:11434
  ※ セキュリティグループでポート11434をsf6_engine実行元のIPのみ許可すること
"""
from __future__ import annotations

import json
import logging

import httpx

from sf6_engine.llm_provider import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Ollama REST API を使った LLM プロバイダー。

    対応モデル例 (ollama pull <model> で導入):
      gemma4:e2b         - Gemma 4 Edge 2B (7.2GB, 要 ~10GB RAM) ← デフォルト
      gemma4:e4b         - Gemma 4 Edge 4B (9.6GB, 要 ~12GB RAM, 128K context)
      gemma4:26b         - Gemma 4 26B MoE (18GB, 要 ~22GB RAM, 256K context)
      gemma3:4b          - Gemma 3 4B (2.5GB, 要 ~4GB RAM) ← RAM が少ない場合の代替
      nomic-embed-text   - テキスト埋め込み用 (別途 pull が必要)

    Args:
        model      : 生成用モデル名。
        base_url   : Ollama サーバーの URL。
        embed_model: 埋め込み用モデル名。
        timeout    : リクエストタイムアウト秒数。大きいモデルほど長く設定する。
    """

    def __init__(
        self,
        model: str = "gemma4:e2b",
        base_url: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
        timeout: float = 120.0,
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.embed_model = embed_model
        self.timeout = timeout
        # 事実参照QAでは再現性が精度に直結するため温度0を既定にする
        # (Ollama デフォルトの 0.8 では intent 分類や転記が実行ごとにブレる)
        self.temperature = temperature
        # 全 API 呼び出しの実測トークン数を累積 (Ollama が応答に含める値)
        from sf6_engine.token_usage import UsageTracker
        self.usage = UsageTracker()

    async def generate(self, prompt: str, system: str = "") -> LLMResponse:
        """Ollama の /api/generate エンドポイントでテキスト生成。"""
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()

        self.usage.record(data.get("prompt_eval_count"), data.get("eval_count"))
        return LLMResponse(
            text=data["response"],
            usage={
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
            },
        )

    async def generate_structured(
        self, prompt: str, schema: dict, system: str = ""
    ) -> dict:
        """JSON モードで構造化出力を生成。

        Ollama の format="json" と、スキーマをシステムプロンプトに埋め込む方式を組み合わせる。
        Gemma 系モデルは JSON モードへの追従が良好。
        """
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        json_instruction = (
            f"あなたは以下のJSONスキーマに厳密に従ったJSONオブジェクトのみを出力します。"
            f"説明文や追加テキストは一切出力しないこと。\n\nスキーマ:\n{schema_str}"
        )
        full_system = f"{system}\n\n{json_instruction}" if system else json_instruction

        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "system": full_system,
            "format": "json",
            "stream": False,
            "options": {"temperature": self.temperature},
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()

        self.usage.record(data.get("prompt_eval_count"), data.get("eval_count"))
        raw = data["response"].strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed: {e}\nraw response: {raw[:200]}")
            raise ValueError(f"Ollama が有効な JSON を返しませんでした: {e}") from e

    async def embed(self, text: str) -> list[float]:
        """Ollama の /api/embed エンドポイントでテキスト埋め込みを生成。

        nomic-embed-text モデルは 768 次元のベクトルを出力する。
        pgvector のインデックスと次元数を合わせること。
        """
        payload = {
            "model": self.embed_model,
            "input": text,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/embed", json=payload)
            resp.raise_for_status()
            data = resp.json()

        embeddings = data.get("embeddings")
        if not embeddings:
            raise ValueError("Ollama embed API からベクトルが返りませんでした")
        self.usage.record(data.get("prompt_eval_count"), 0)
        return embeddings[0]

    async def is_available(self) -> bool:
        """Ollama サーバーが起動しているか確認。"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def list_models(self) -> list[str]:
        """インストール済みモデルの一覧を返す。"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        return [m["name"] for m in data.get("models", [])]
