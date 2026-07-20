"""在庫明細確認のNE受注状態の取得範囲テスト。

認識合わせ対応（2026-07-20 依頼1「詳細設定でステータスを選ぶ際に印刷済みまで出力する」）:
  - 取得範囲は「現行(1/2/20/30) + 40:納品書印刷済」で確定（ユーザー回答: 常に40込みに変更）
  - 0:取込情報不足 と 50:出荷確定済(完了) は含めない（ユーザー確認済みの初期仕様）
  - 選択肢テキストは NE 検索画面の option テキストと完全一致が必要
    （next_engine_downloader._set_select_by_option_texts が完全一致照合のため、
    「40 : 納品書印刷済」の半角コロン前後スペース表記を固定する）
  - 高江洲タブ（TAKAESU_ORDER_STATUS_OPTIONS）は独立定数のまま影響を受けない
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.next_engine_downloader import ORDER_STATUS_OPTIONS
from portal_app.services.takaesu_orders import TAKAESU_ORDER_STATUS_OPTIONS


def test_order_status_options_include_printed():
    """依頼1: 印刷済み(40)までを取得範囲に含める。0と50は含めない。"""
    assert ORDER_STATUS_OPTIONS == [
        "1 : 受注メール取込済",
        "2 : 起票済(CSV/手入力)",
        "20 : 納品書印刷待ち",
        "30 : 納品書印刷中",
        "40 : 納品書印刷済",
    ]
    joined = "\n".join(ORDER_STATUS_OPTIONS)
    assert "取込情報不足" not in joined
    assert "出荷確定" not in joined


def test_takaesu_options_unchanged():
    """高江洲タブは従来どおり独立した定数で、40込みのセットを維持している。"""
    assert list(TAKAESU_ORDER_STATUS_OPTIONS) == [
        "1 : 受注メール取込済",
        "2 : 起票済(CSV/手入力)",
        "20 : 納品書印刷待ち",
        "30 : 納品書印刷中",
        "40 : 納品書印刷済",
    ]
