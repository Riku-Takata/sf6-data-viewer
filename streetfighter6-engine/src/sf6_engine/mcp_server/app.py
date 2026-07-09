"""AWS Lambda エントリポイント — MCP サーバを API Gateway 経由で公開する (ADR-017).

構成:
  API Gateway (HTTP API) → Lambda → Mangum → FastMCP streamable-http (stateless/json)

設計:
  - Supabase 接続情報 (URL / anon key) は SSM Parameter Store から cold start 時に取得し、
    db.get_client() が読む前に os.environ へ注入する。
  - Bearer トークン認証: SSM の MCP_AUTH_TOKEN と Authorization ヘッダを照合する
    ASGI ミドルウェアを噛ませる (トークン未設定時は警告のみで通す = 初期検証用)。
  - FastMCP は stateless_http + json_response (server.py で設定済み) なので、
    各 POST に対し単一 JSON を返す。SSE/セッション/常駐サーバは不要。

環境変数:
  SUPABASE_URL_SSM_PATH       : 既定 /sf6/supabase-url
  SUPABASE_ANON_KEY_SSM_PATH  : 既定 /sf6/supabase-anon-key
  MCP_AUTH_TOKEN_SSM_PATH     : 既定 /sf6/mcp/auth-token (任意)
  AWS_REGION                  : Lambda が自動設定
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("sf6_mcp.app")
logger.setLevel(logging.INFO)

_MCP_AUTH_TOKEN: str | None = None


def _ssm_get(path: str, decrypt: bool = True) -> str | None:
    """SSM Parameter Store から値を取得する。失敗時は None。"""
    import boto3
    try:
        ssm = boto3.client("ssm")
        resp = ssm.get_parameter(Name=path, WithDecryption=decrypt)
        return resp["Parameter"]["Value"]
    except Exception as e:
        logger.warning("SSM get_parameter failed for %s: %s", path, e)
        return None


def _bootstrap_config() -> None:
    """cold start 時に SSM から設定を読み込み、環境変数へ注入する。

    db.get_client() (lru_cache) が初回に呼ばれる前に実行する必要がある。
    """
    global _MCP_AUTH_TOKEN

    mapping = [
        ("SUPABASE_URL", os.getenv("SUPABASE_URL_SSM_PATH", "/sf6/supabase-url")),
        ("SUPABASE_ANON_KEY", os.getenv("SUPABASE_ANON_KEY_SSM_PATH", "/sf6/supabase-anon-key")),
        # register_move_alias (move_aliases への書き込み) 専用。未登録なら
        # 読み取り系ツールは従来どおり動き、登録ツールのみエラーを返す。
        ("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_SERVICE_KEY_SSM_PATH", "/sf6/supabase-service-key")),
    ]
    for env_key, ssm_path in mapping:
        if not os.environ.get(env_key):
            val = _ssm_get(ssm_path)
            if val:
                os.environ[env_key] = val

    # 認証トークン (任意)
    token_path = os.getenv("MCP_AUTH_TOKEN_SSM_PATH", "/sf6/mcp/auth-token")
    _MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN") or _ssm_get(token_path)
    if not _MCP_AUTH_TOKEN:
        logger.warning("MCP_AUTH_TOKEN 未設定: 認証なしで公開されます (本番では設定すること)")


class _BearerAuthMiddleware:
    """Authorization: Bearer <token> を検証する最小 ASGI ミドルウェア。"""

    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        if auth != expected:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"unauthorized"}',
            })
            return

        await self.app(scope, receive, send)


# --- cold start: 設定読み込み → アプリ構築 ---
_bootstrap_config()

from mangum import Mangum  # noqa: E402
from mangum.protocols import LifespanCycle  # noqa: E402

from sf6_engine.mcp_server.server import mcp  # noqa: E402

_asgi_app = _BearerAuthMiddleware(mcp.streamable_http_app(), _MCP_AUTH_TOKEN)

# Mangum は HTTP リクエスト処理のみ担当 (lifespan="off")。
# _setup_event_loop() が永続イベントループを用意するので、先に構築して
# 後続の LifespanCycle が同一ループを共有するようにする。
# api_gateway_base_path: HTTP API の非 $default ステージは path に "/prod" が
# 含まれる (requestContext.http.path = "/prod/mcp")。これを剥がして MCP アプリの
# "/mcp" ルートに一致させる。StageName と一致させること (template-mcp.yaml)。
_BASE_PATH = os.getenv("MCP_BASE_PATH", "/prod")
_mangum = Mangum(_asgi_app, lifespan="off", api_gateway_base_path=_BASE_PATH)

# StreamableHTTP セッションマネージャは「1インスタンスにつき 1 度だけ run() 可能」。
# Mangum の auto/on は呼び出し毎に startup/shutdown するため warm invocation で破綻する。
# よって cold start で lifespan を 1 度だけ起動し、コンテナ生存中は起動したまま保持する。
# (Lambda の freeze/thaw を跨いでタスクグループが生き続ける = Mangum の warm start と同じ前提)
_lifespan = LifespanCycle(_asgi_app, "on")
_lifespan.__enter__()  # startup を 1 回だけ実行。shutdown は呼ばない (run-once 制約を満たす)


def handler(event, context):
    """API Gateway (HTTP API / payload v2) からの Lambda ハンドラ。"""
    return _mangum(event, context)

