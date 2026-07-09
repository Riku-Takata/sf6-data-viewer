"""AWS Bedrock Titan Text Embeddings V2 による埋め込み生成 (ADR-017).

Ollama 非依存の埋め込みヘルパー。サーバレス (AWS Lambda) で動かせるよう、
doc_chunks の再埋め込みスクリプトと MCP の search_system_docs が共用する。

環境変数:
  AWS_REGION           : Bedrock のリージョン (デフォルト ap-northeast-1)
  BEDROCK_EMBED_MODEL  : モデルID (デフォルト amazon.titan-embed-text-v2:0)
  BEDROCK_EMBED_DIM    : 出力次元 256/512/1024 (デフォルト 1024)

前提: 実行プリンシパルに bedrock:InvokeModel (Titan V2) 権限が必要。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

_MODEL_ID = os.getenv("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
EMBED_DIM = int(os.getenv("BEDROCK_EMBED_DIM", "1024"))


@lru_cache(maxsize=1)
def _client():
    """bedrock-runtime クライアント (プロセス内で再利用)。"""
    import boto3  # 遅延 import: boto3 未導入の環境でモジュール import を壊さない
    return boto3.client("bedrock-runtime", region_name=_REGION)


def embed_text(text: str, dimensions: int = EMBED_DIM, normalize: bool = True) -> list[float]:
    """テキストを Titan V2 で埋め込みベクトルに変換する。

    Args:
        text:       埋め込む入力テキスト。
        dimensions: 出力次元 (256/512/1024)。doc_chunks は 1024 で統一。
        normalize:  L2 正規化するか (コサイン類似度検索では True 推奨)。

    Returns:
        list[float]: 長さ ``dimensions`` の埋め込みベクトル。

    Raises:
        RuntimeError: 返却ベクトルの次元が想定と異なる場合。
        botocore.exceptions.ClientError: InvokeModel 失敗時 (権限・モデルアクセス等)。
    """
    body = json.dumps({
        "inputText": text,
        "dimensions": dimensions,
        "normalize": normalize,
    })
    resp = _client().invoke_model(modelId=_MODEL_ID, body=body)
    out = json.loads(resp["body"].read())
    vec = out["embedding"]
    if len(vec) != dimensions:
        raise RuntimeError(
            f"Titan が想定外の次元を返しました: {len(vec)} != {dimensions}"
        )
    return vec
