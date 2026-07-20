"""税込・割引価格計算 — Excel貼り付け対応のパース関数テスト。

対象: portal_app/static/price_calc.js に追加した
  - parseClipboardTable（タブ区切りテキスト→行列への分解）
  - isTablePaste（複数セル判定。単一セルは通常貼り付けに任せる）
  - parseTaxPct（「8」「8%」「10%」等の税率パース）
  - parseIntegerYen / parseDiscountBp の通貨・%表記の受理拡張

認識合わせ対応（2026-07-20 ユーザー依頼「エクセルからのコピペ(縦方向の表形式)にも対応」）:
  - Excel は行末に改行、セル間にタブを付けてコピーする
  - 縦一列だけのコピー（単価のみ複数行）も行列として扱えること
  - 税率の不正値は受け付けない（§8）ため 8/10 以外は null
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


def _run_node(function_name: str, cases: list[dict]) -> list:
    script = (
        'let s = "";'
        'process.stdin.setEncoding("utf8");'
        'process.stdin.on("data", (d) => { s += d; });'
        'process.stdin.on("end", () => {'
        f'  const pc = require({json.dumps(str(JS_PATH))});'
        '  const cases = JSON.parse(s);'
        f'  const out = cases.map((c) => pc.{function_name}(...c.args));'
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


def test_parse_clipboard_table():
    """Excelコピー形式（タブ区切り・行末改行）を行列に分解できること。"""
    out = _run_node("parseClipboardTable", [
        # 5列×2行（Excelは末尾に必ず改行を付ける）
        {"args": ["商品A\t1000\t2\t8%\t5\r\n商品B\t2000\t1\t10%\t0\r\n"]},
        # 縦一列（単価のみ3行）— 今回の主要ユースケース
        {"args": ["100\r\n200\r\n300\r\n"]},
        # 空セルを含む行（Excelの空セルは空文字として上書き対象になる）
        {"args": ["商品C\t\t3"]},
        # LF改行のみ（Excel以外のアプリからのコピー）
        {"args": ["1\t2\n3\t4"]},
        # 空文字
        {"args": [""]},
    ])
    assert out[0] == [["商品A", "1000", "2", "8%", "5"], ["商品B", "2000", "1", "10%", "0"]]
    assert out[1] == [["100"], ["200"], ["300"]]
    assert out[2] == [["商品C", "", "3"]]
    assert out[3] == [["1", "2"], ["3", "4"]]
    assert out[4] == []


def test_is_table_paste():
    """複数セル（タブか複数行）のときだけ表貼り付けと判定すること。"""
    out = _run_node("isTablePaste", [
        {"args": ["3565"]},          # 単一セル → 通常貼り付け
        {"args": ["3565\r\n"]},      # 単一セル＋Excelの行末改行 → 通常貼り付け
        {"args": ["100\r\n200\r\n"]},  # 縦一列 → 表貼り付け
        {"args": ["商品A\t1000"]},     # タブあり → 表貼り付け
    ])
    assert out == [False, False, True, True]


def test_parse_tax_pct():
    """税率は 8/10 のみ受理（%・全角可）。不正値は null（§8: 不正値は受け付けない）。"""
    out = _run_node("parseTaxPct", [
        {"args": ["8"]}, {"args": ["10"]}, {"args": ["8%"]}, {"args": ["10％"]},
        {"args": ["８％"]}, {"args": ["8.0"]}, {"args": ["9"]}, {"args": ["80"]},
        {"args": ["1.0"]}, {"args": [""]}, {"args": ["軽減"]},
    ])
    assert out == [8, 10, 8, 10, 8, 8, None, None, None, None, None]


def test_parse_integer_yen_currency_forms():
    """通貨表記（¥・円・カンマ・全角）を受理し、小数・単位混在は従来どおり拒否すること。"""
    out = _run_node("parseIntegerYen", [
        {"args": ["¥3,565"]}, {"args": ["3565円"]}, {"args": ["￥1,000,000"]},
        {"args": ["３５６５"]}, {"args": ["100.5"]}, {"args": ["¥"]}, {"args": ["3円565"]},
    ])
    assert out == [3565, 3565, 1000000, 3565, None, None, None]


def test_parse_discount_bp_percent_forms():
    """%付き割引率（5%・5.00％）を受理し、上限・小数桁の制約は従来どおりであること。"""
    out = _run_node("parseDiscountBp", [
        {"args": ["5%"]}, {"args": ["5.00％"]}, {"args": ["0%"]}, {"args": ["99.99%"]},
        {"args": ["100%"]}, {"args": ["5.004%"]}, {"args": ["%"]},
    ])
    assert out == [500, 500, 0, 9999, 10000, None, None]
    # 100% は 10000bp としてパースされるが、validateRow 側の範囲チェック（0〜9999bp）で
    # エラーになることを確認する（V04 の挙動が貼り付けでも変わらないこと）
    row = _run_node("validateRow", [
        {"args": [{"product": "A", "unitNet": "100", "quantity": "1", "taxPct": 10, "discount": "100%"}]},
    ])[0]
    assert row["status"] == "error"
    assert row["errors"]["discount"] == "割引率は0.00%以上99.99%以下で入力してください。"
