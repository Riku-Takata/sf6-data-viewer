"""LLM トークン使用量の計測とコスト換算。

Ollama は /api/generate・/api/embed の応答に実測トークン数
(prompt_eval_count / eval_count) を返すため、それをプロバイダ層で記録する。

呼び出し種別 (intent / answer / embed 等) は contextvars ベースのラベルで
付与する。async 安全なので Discord ボットの並行処理でも混線しない。

コスト換算は env で単価を指定する (ローカル gemma4 は 0 円なので、
「API モデルに載せ替えた場合いくらか」の試算に使う):
    SF6_COST_INPUT_PER_MTOK  : 入力 100万トークンあたり単価 (例: 0.15)
    SF6_COST_OUTPUT_PER_MTOK : 出力 100万トークンあたり単価 (例: 0.60)
    SF6_COST_CURRENCY        : 表示通貨 (既定 USD)

注意: gemma4 と API モデルのトークナイザは別物のため、換算には
1〜2割程度の誤差が乗る。傾向把握・比較用として扱うこと。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

_current_label: ContextVar[str] = ContextVar("sf6_usage_label", default="other")

Totals = dict[str, dict[str, int]]


@contextmanager
def usage_label(name: str):
    """この with ブロック内の LLM 呼び出しに種別ラベルを付ける。"""
    token = _current_label.set(name)
    try:
        yield
    finally:
        _current_label.reset(token)


class UsageTracker:
    """ラベル別の累積トークンカウンタ (プロバイダ1つにつき1個)。"""

    def __init__(self) -> None:
        self._by_label: Totals = {}

    def record(self, prompt_tokens: int | None, completion_tokens: int | None = 0) -> None:
        label = _current_label.get()
        d = self._by_label.setdefault(
            label, {"prompt": 0, "completion": 0, "calls": 0})
        d["prompt"] += int(prompt_tokens or 0)
        d["completion"] += int(completion_tokens or 0)
        d["calls"] += 1

    def totals(self) -> Totals:
        """現時点の累積スナップショット (コピー) を返す。"""
        return {k: dict(v) for k, v in self._by_label.items()}


def usage_diff(before: Totals, after: Totals) -> Totals:
    """2つのスナップショットの差分 (1質問分の消費など) を返す。"""
    out: Totals = {}
    for label, a in after.items():
        b = before.get(label, {})
        d = {k: a.get(k, 0) - b.get(k, 0) for k in ("prompt", "completion", "calls")}
        if any(d.values()):
            out[label] = d
    return out


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float | None:
    """env の単価設定でコストを試算する。単価未設定なら None。"""
    in_rate = os.getenv("SF6_COST_INPUT_PER_MTOK")
    out_rate = os.getenv("SF6_COST_OUTPUT_PER_MTOK")
    if in_rate is None and out_rate is None:
        return None
    return (prompt_tokens * float(in_rate or 0)
            + completion_tokens * float(out_rate or 0)) / 1_000_000


def format_usage(usage: Totals) -> str:
    """使用量をログ用の1行に整形する (コスト設定があれば換算も併記)。"""
    if not usage:
        return "tokens: (計測なし)"
    parts = []
    total_p = total_c = 0
    for label in sorted(usage):
        d = usage[label]
        total_p += d["prompt"]
        total_c += d["completion"]
        parts.append(f"{label}: in={d['prompt']}/out={d['completion']} ({d['calls']}回)")
    line = "tokens: " + " | ".join(parts) + f" | 合計 in={total_p} out={total_c}"
    cost = estimate_cost(total_p, total_c)
    if cost is not None:
        currency = os.getenv("SF6_COST_CURRENCY", "USD")
        line += f" | 換算 {cost:.6f} {currency}"
    return line
