"""ネコポス新規カード（依頼5）— プロファイルと変換ルールのテスト。

認識合わせ対応（2026-07-20 依頼5・ユーザー回答）:
  - 対象受注はNE保存検索「ネコポス」(search_condi=16)。保存検索は印刷待ち条件も
    含む（ユーザー回答）ため、発送方法の再検索はスキップする（shipping_options空）
  - 出力ファイルは既存ヤマトと別prefixに分離（最新ファイル自動選択の混線防止）
  - 送り状種別はネコポス(7)へ上書きし、変更が起きた場合は変更前の値を警告に明示
    （NEカスタムパターンの実出力値は検証用受注69589/69586/69585で確認する運用）
  - ネコポスで指定できない列（温度区分・配達指定日・時間指定・コレクト額・営業所
    止置き）は空欄化し、クール・代引が入っていた受注は受注番号付きで警告（出力は続行）
  - 既存ヤマトフロー（YAMATO_PROFILE）の挙動は一切変えない
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.yamato_conversion import (
    YAMATO_INPUT_HEADERS,
    transform_ne_to_yamato,
)
from portal_app.services.yamato_flow_profile import (
    NEKOPOS_PROFILE,
    YAMATO_PROFILE,
    profile_for_mode,
)


def _source_row(**overrides: str) -> dict[str, str]:
    row = {header: "" for header in YAMATO_INPUT_HEADERS}
    row["お客様管理番号"] = overrides.pop("order", "69589")
    row["届け先名（漢字）"] = overrides.pop("name", "テスト太郎")
    row.update(overrides)
    return row


def _frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=YAMATO_INPUT_HEADERS)


class ProfileDefinitionTest(unittest.TestCase):
    def test_nekopos_profile_values(self):
        # 実機調査(2026-07-21)の確定値: ネコポスは保存検索ではなくオリジナルステータス
        # 検索（originalStatus.search(118)）。着地はプレーンな受注一覧。
        self.assertEqual(
            NEKOPOS_PROFILE.order_list_url, "https://main.next-engine.com/Userjyuchu/index"
        )
        self.assertEqual(NEKOPOS_PROFILE.original_status_id, 118)
        self.assertEqual(NEKOPOS_PROFILE.shipping_options, tuple())  # 再検索スキップ
        self.assertEqual(NEKOPOS_PROFILE.invoice_type_override, "7")
        self.assertTrue(NEKOPOS_PROFILE.clear_unsupported_columns)

    def test_prefixes_are_separated_from_yamato(self):
        """最新ファイル自動選択の混線防止: ヤマトとprefix・フォルダを共有しない。"""
        self.assertNotEqual(NEKOPOS_PROFILE.source_dir_name, YAMATO_PROFILE.source_dir_name)
        self.assertNotEqual(NEKOPOS_PROFILE.source_prefix, YAMATO_PROFILE.source_prefix)
        self.assertNotEqual(NEKOPOS_PROFILE.output_prefix, YAMATO_PROFILE.output_prefix)
        self.assertNotEqual(NEKOPOS_PROFILE.data_prefix, YAMATO_PROFILE.data_prefix)
        # ne-to-nekopos が ne-to-yamato の prefix 検索に誤マッチしないこと（前方一致の衝突なし）
        self.assertFalse(NEKOPOS_PROFILE.output_prefix.startswith(YAMATO_PROFILE.output_prefix))
        self.assertFalse(YAMATO_PROFILE.output_prefix.startswith(NEKOPOS_PROFILE.output_prefix))

    def test_yamato_profile_keeps_legacy_values(self):
        """既存ヤマトフローの挙動を変えない（従来の定数と同値）。"""
        self.assertIn("search_condi=17", YAMATO_PROFILE.order_list_url)
        self.assertEqual(
            YAMATO_PROFILE.shipping_options,
            ("20 : ヤマト(発払い)B2v6", "21 : ヤマト(コレクト)B2v6"),
        )
        self.assertIsNone(YAMATO_PROFILE.original_status_id)
        self.assertIsNone(YAMATO_PROFILE.invoice_type_override)
        self.assertFalse(YAMATO_PROFILE.clear_unsupported_columns)

    def test_profile_for_mode(self):
        self.assertIs(profile_for_mode("nekopos"), NEKOPOS_PROFILE)
        self.assertIs(profile_for_mode("yamato"), YAMATO_PROFILE)
        self.assertIs(profile_for_mode(None), YAMATO_PROFILE)
        self.assertIs(profile_for_mode("unknown"), YAMATO_PROFILE)


class NekoposTransformTest(unittest.TestCase):
    def test_invoice_type_override_with_warning(self):
        """送り状種別0（発払い）→7へ上書きし、変更前の値を警告に明示する。"""
        source = _frame([_source_row(送り状種別="0")])
        converted, warnings_out, _r, _a = transform_ne_to_yamato(
            source, {}, profile=NEKOPOS_PROFILE
        )
        self.assertEqual(converted.iloc[0]["送り状種別"], "7")
        override_warnings = [w for w in warnings_out if "送り状種別" in w]
        self.assertEqual(len(override_warnings), 1)
        self.assertIn("0", override_warnings[0])

    def test_invoice_type_already_nekopos_no_warning(self):
        """既に7なら変更なし・警告なし（NEパターンが7を出す場合は静かに通る）。"""
        source = _frame([_source_row(送り状種別="7")])
        converted, warnings_out, _r, _a = transform_ne_to_yamato(
            source, {}, profile=NEKOPOS_PROFILE
        )
        self.assertEqual(converted.iloc[0]["送り状種別"], "7")
        self.assertFalse(any("送り状種別" in w for w in warnings_out))

    def test_unsupported_columns_cleared_with_suspicious_warning(self):
        """温度区分・時間指定・コレクト額を空欄化し、クール/代引の受注番号を警告する。"""
        source = _frame(
            [
                _source_row(
                    order="69586",
                    送り状種別="7",
                    温度区分="2",
                    時間指定コード="0812",
                    **{"コレクト代金引換額（税込）": "3000", "コレクト内消費税額": "272"},
                ),
                _source_row(order="69585", name="テスト次郎", 送り状種別="7"),
            ]
        )
        converted, warnings_out, _r, _a = transform_ne_to_yamato(
            source, {}, profile=NEKOPOS_PROFILE
        )
        first = converted.iloc[0]
        self.assertEqual(first["温度区分"], "")
        self.assertEqual(first["時間指定コード"], "")
        self.assertEqual(first["コレクト代金引換額（税込）"], "")
        self.assertEqual(first["コレクト内消費税額"], "")
        self.assertTrue(any("指定できない項目を空欄化" in w for w in warnings_out))
        suspicious = [w for w in warnings_out if "クール便・代引" in w]
        self.assertEqual(len(suspicious), 1)
        self.assertIn("69586", suspicious[0])
        self.assertNotIn("69585", suspicious[0])

    def test_zero_collect_amount_is_not_suspicious(self):
        """NEパターンは代引でない受注にもコレクト額「0」を出す（2026-07-21実CSVで確認）。
        ゼロ値は空欄化するが、警告・代引疑いを出さない（誤検知防止）。"""
        source = _frame(
            [
                _source_row(
                    order="69589",
                    送り状種別="7",
                    **{"コレクト代金引換額（税込）": "0"},
                )
            ]
        )
        converted, warnings_out, _r, _a = transform_ne_to_yamato(
            source, {}, profile=NEKOPOS_PROFILE
        )
        self.assertEqual(converted.iloc[0]["コレクト代金引換額（税込）"], "")
        self.assertFalse(any("空欄化" in w for w in warnings_out))
        self.assertFalse(any("クール便・代引" in w for w in warnings_out))

    def test_nekopos_clears_future_delivery_date_too(self):
        """依頼3の翌日ルールに該当しない先日付の配達指定日も、ネコポスでは空欄化する。"""
        source = _frame(
            [_source_row(送り状種別="7", 出荷予定日="2026/07/21", 配達指定日="2026/08/01")]
        )
        converted, _w, _r, _a = transform_ne_to_yamato(source, {}, profile=NEKOPOS_PROFILE)
        self.assertEqual(converted.iloc[0]["配達指定日"], "")

    def test_yamato_profile_unchanged_passthrough(self):
        """ヤマト（既定プロファイル）は送り状種別・温度区分等を素通しする（回帰確認）。"""
        source = _frame(
            [
                _source_row(
                    送り状種別="0",
                    温度区分="2",
                    出荷予定日="2026/07/21",
                    配達指定日="2026/08/01",
                    時間指定コード="0812",
                )
            ]
        )
        converted, warnings_out, _r, _a = transform_ne_to_yamato(source, {})
        row = converted.iloc[0]
        self.assertEqual(row["送り状種別"], "0")
        self.assertEqual(row["温度区分"], "2")
        self.assertEqual(row["配達指定日"], "2026/08/01")
        self.assertEqual(row["時間指定コード"], "0812")
        self.assertFalse(any("空欄化" in w for w in warnings_out))


if __name__ == "__main__":
    unittest.main()
