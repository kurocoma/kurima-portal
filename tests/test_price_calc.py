"""税込・割引価格計算（楽天RMS向け）の計算ロジックテスト。

対象: portal_app/static/price_calc.js（ブラウザ内計算の純粋関数）
根拠: 要件定義書 v1.0
  - §11 受入テストケース T01〜T07（期待値との突合）
  - §4.3 必須の不変条件（目標額を超えない最大値であること）
  - §8 入力チェック（V01〜V04 相当）

JS実装を node で実行し、期待値および Python 側の独立実装
（疑似コード 付録A を Python で書き直したもの）と突合する。
node が無い環境ではスキップする。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS_PATH = Path(__file__).resolve().parents[1] / "portal_app" / "static" / "price_calc.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node が無い環境では価格計算JSのテストをスキップ")


def _run_node(function_name: str, cases: list[dict]) -> list[dict]:
    """price_calc.js の関数を node で実行し、ケースごとの結果を返す。"""
    script = (
        'let s = "";'
        'process.stdin.setEncoding("utf8");'
        'process.stdin.on("data", (d) => { s += d; });'
        'process.stdin.on("end", () => {'
        f'  const pc = require({json.dumps(str(JS_PATH))});'
        '  const cases = JSON.parse(s);'
        '  const out = cases.map((c) => {'
        '    try {'
        f'      return {{ ok: true, r: pc.{function_name}(...c.args) }};'
        '    } catch (e) {'
        '      return { ok: false, err: String(e) };'
        '    }'
        '  });'
        '  process.stdout.write(JSON.stringify(out));'
        '});'
    )
    proc = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, f"node 実行に失敗: {proc.stderr}"
    return json.loads(proc.stdout)


def _calc(cases: list[tuple[int, int, int, int]]) -> list[dict]:
    out = _run_node("calculate", [{"args": list(c)} for c in cases])
    results = []
    for case, item in zip(cases, out):
        assert item["ok"], f"calculate{case} が例外: {item.get('err')}"
        results.append(item["r"])
    return results


# ---------- Python 側の独立実装（付録A 疑似コードの直訳。JSとの相互検証用） ----------

def _py_gross(net: int, tax_pct: int) -> int:
    return net + net * tax_pct // 100


def _py_calculate(unit_net: int, quantity: int, tax_pct: int, discount_bp: int) -> dict:
    unit_tax = unit_net * tax_pct // 100
    unit_gross = unit_net + unit_tax
    before_gross = unit_gross * quantity
    target_gross = before_gross * (10000 - discount_bp) // 10000
    candidate = -(-target_gross * 100 // (100 + tax_pct))  # ceil_div
    while candidate > 0 and _py_gross(candidate, tax_pct) > target_gross:
        candidate -= 1
    while _py_gross(candidate + 1, tax_pct) <= target_gross:
        candidate += 1
    after_net = candidate
    after_tax = after_net * tax_pct // 100
    after_gross = after_net + after_tax
    return {
        "unitTax": unit_tax,
        "beforeGross": before_gross,
        "targetGross": target_gross,
        "afterNet": after_net,
        "afterTax": after_tax,
        "afterGross": after_gross,
    }


# ---------- §11 受入テストケース T01〜T07 ----------

# (単価, 数量, 税率, 割引bp, 期待税額, 期待割引前税込, 期待割引後税抜, 期待割引後税込)
ACCEPTANCE_CASES = [
    ("T01", 4299, 1, 8, 0, 343, 4642, 4299, 4642),      # 基本
    ("T02", 3565, 3, 8, 500, 812, 11550, 10160, 10972),  # 目標一致
    ("T03", 3565, 5, 8, 1000, 1283, 19250, 16042, 17325),  # 目標一致
    ("T04", 4299, 3, 10, 500, 1224, 14184, 12249, 13473),  # 目標より1円低い
    ("T05", 4299, 3, 8, 0, 1031, 13926, 12895, 13926),   # 無割引でも税抜合計を補正
    ("T06", 1, 1, 8, 0, 0, 1, 1, 1),                     # 最小額
    ("T07", 1000, 2, 10, 9999, 0, 2200, 0, 0),           # 0円を許容（§13 Q6 初期仕様どおり）
]


@pytest.mark.parametrize(
    "no, unit_net, quantity, tax_pct, discount_bp, exp_tax, exp_before, exp_after_net, exp_after_gross",
    ACCEPTANCE_CASES,
    ids=[c[0] for c in ACCEPTANCE_CASES],
)
def test_acceptance_t01_t07(no, unit_net, quantity, tax_pct, discount_bp,
                            exp_tax, exp_before, exp_after_net, exp_after_gross):
    (r,) = _calc([(unit_net, quantity, tax_pct, discount_bp)])
    assert r["afterTax"] == exp_tax, f"{no}: 税額（割引後）"
    assert r["beforeGross"] == exp_before, f"{no}: 割引前税込"
    assert r["afterNet"] == exp_after_net, f"{no}: 割引後税抜（RMS登録値）"
    assert r["afterGross"] == exp_after_gross, f"{no}: 割引後税込"


def test_acceptance_display_values():
    """§4.7 計算例の表示系項目（1個当たり・実効割引率・目標差額）との突合。"""
    r_t02, r_t04 = _calc([(3565, 3, 8, 500), (4299, 3, 10, 500)])
    # T02: 1個当たり 3,657.33円 / 目標一致（差額0円）/ 実効割引率 5.004%
    assert r_t02["perUnitGrossX100"] == 365733
    assert r_t02["targetDifference"] == 0
    assert r_t02["effectiveRateMil"] == 5004
    # T04: 目標13,474円は再現不可 → 13,473円（差額1円）
    assert r_t04["targetGross"] == 13474
    assert r_t04["targetDifference"] == 1


# ---------- §4.3 必須の不変条件（広い入力範囲でJSとPythonを相互検証） ----------

def test_invariants_sweep():
    """割引後税込 ≤ 目標額、+1円で目標超過、税額=floor(税抜×税率) を全数確認する。"""
    unit_nets = [1, 9, 10, 99, 100, 101, 3565, 4299, 12345, 999999999]
    quantities = [1, 2, 3, 5, 100, 9999]
    tax_pcts = [8, 10]
    discount_bps = [0, 1, 99, 100, 500, 999, 1000, 5000, 9998, 9999]

    cases = [
        (p, q, t, d)
        for p in unit_nets
        for q in quantities
        for t in tax_pcts
        for d in discount_bps
    ]
    results = _calc(cases)

    for (p, q, t, d), r in zip(cases, results):
        label = f"unitNet={p}, quantity={q}, tax={t}%, discountBp={d}"
        # 不変条件1: 割引後税込価格 ≤ 目標割引後税込価格
        assert r["afterGross"] <= r["targetGross"], label
        # 不変条件2: 割引後税抜価格を1円増やすと目標額を超える
        assert _py_gross(r["afterNet"] + 1, t) > r["targetGross"], label
        # 不変条件3: 税額 = floor(割引後税抜価格 × 税率)
        assert r["afterTax"] == r["afterNet"] * t // 100, label
        # 税込の整合: 割引後税込 = 税抜 + 税額
        assert r["afterGross"] == r["afterNet"] + r["afterTax"], label
        # NaN・負数を出さない（§8.2）
        assert r["afterNet"] >= 0 and r["afterGross"] >= 0, label
        # Python 独立実装との全項目一致（実装間の相互検証）
        expected = _py_calculate(p, q, t, d)
        for key, value in expected.items():
            assert r[key] == value, f"{label}: {key} がPython実装と不一致"


def test_rms_overflow_flag():
    """§8: RMS登録価格が9桁を超える場合は rmsOverflow を立てる（コピー抑止用）。"""
    over, under = _calc([(999999999, 9999, 10, 0), (999999999, 1, 10, 0)])
    assert over["afterNet"] > 999999999
    assert over["rmsOverflow"] is True
    assert under["rmsOverflow"] is False


# ---------- §8 入力チェック（V01〜V04 相当） ----------

def _validate(rows: list[dict]) -> list[dict]:
    out = _run_node("validateRow", [{"args": [row]} for row in rows])
    results = []
    for row, item in zip(rows, out):
        assert item["ok"], f"validateRow({row}) が例外: {item.get('err')}"
        results.append(item["r"])
    return results


def test_validation_messages():
    base = {"product": "商品A", "quantity": "1", "taxPct": 10, "discount": "0.00"}
    results = _validate([
        {**base, "unitNet": ""},        # V01: 単価空欄 → 必須エラー
        {**base, "unitNet": "100.5"},   # V02: 小数 → 整数入力エラー
        {**base, "unitNet": "100", "quantity": "0"},   # V03: 数量0
        {**base, "unitNet": "100", "discount": "100"},  # V04: 割引率100%
        {"product": "", "unitNet": "", "quantity": "1", "taxPct": 10, "discount": "0.00"},  # §8.1 未使用行
        {**base, "unitNet": "3,565", "quantity": "3", "discount": "5"},  # 正常（カンマ・省略形を許容 §5.4）
    ])
    v01, v02, v03, v04, empty, ok = results

    assert v01["status"] == "error"
    assert v01["errors"]["unitNet"] == "商品単価（税抜）を入力してください。"
    assert v02["status"] == "error"
    assert "整数" in v02["errors"]["unitNet"]
    assert v03["status"] == "error"
    assert "1〜9,999" in v03["errors"]["quantity"]
    assert v04["status"] == "error"
    assert v04["errors"]["discount"] == "割引率は0.00%以上99.99%以下で入力してください。"
    # §8.1: 商品名を含め全入力が空の行は未使用行としてエラーを出さない
    assert empty["status"] == "empty"
    assert empty["errors"] == {}
    # 正常系: カンマ付き単価と「5」→5.00%の解釈
    assert ok["status"] == "ok"
    assert ok["values"] == {
        "product": "商品A", "unitNet": 3565, "quantity": 3, "taxPct": 10, "discountBp": 500,
    }


def test_discount_parse_variants():
    """§5.4: 「5」「5.0」「5.00」はいずれも5.00%として扱う。"""
    out = _run_node("parseDiscountBp", [{"args": ["5"]}, {"args": ["5.0"]}, {"args": ["5.00"]},
                                        {"args": ["0"]}, {"args": ["99.99"]}, {"args": ["5.004"]},
                                        {"args": ["abc"]}, {"args": ["-1"]}])
    values = [item["r"] for item in out]
    assert values[:5] == [500, 500, 500, 0, 9999]
    assert values[5] is None  # 小数第3位は入力不可（§5.4 小数第2位まで）
    assert values[6] is None
    assert values[7] is None
