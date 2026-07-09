"""SF6 Engine の MCP (Model Context Protocol) サーバ.

実装済みの決定論ロジック層 (フレームデータ照会・確定反撃・コンボ・セットプレイ) を
MCP ツールとして公開する。自然言語の解釈と回答生成はホスト側 LLM
(Claude Desktop / 将来の Bot) が担うため、本サーバには LLM 段
(intent_parser / generate_answer) を含めない。

参照: ADR-017 (実装済み機能を AWS リモート MCP サーバとして切り出す)。
"""
