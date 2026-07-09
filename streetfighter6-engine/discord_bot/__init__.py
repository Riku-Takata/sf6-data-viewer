"""SF6 Discord Bot.

gemma4 (Ollama, ローカル) で自然言語を intent_parser により構造化し、AWS 上の
MCP サーバ経由でツールを実行する。MCP を「開発者公開インターフェース」として
外部ホストから使う dogfooding を兼ねる (ADR-017)。

bot は Ollama (ローカル LLM) と MCP サーバ (AWS) のみに依存し、Supabase / Bedrock
には直接アクセスしない (それらは MCP サーバ側が担う)。
"""
