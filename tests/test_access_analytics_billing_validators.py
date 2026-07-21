"""アクセス解析取得・請求関連取得の新規バリデータ5種の単体テスト（前回evalフィードバック4）。

対象:
- access_analytics_rakuten.validate_rakuten_device_csv
- billing_statements_yahoo.classify_statement_state
- billing_statements_yahoo.validate_yahoo_statement_csv
- billing_statements_rakuten.validate_shop_detail_csv
- billing_statements_rakuten.validate_summary_csv

ノートの負例（ヘッダー改変・列数不一致・BOM付与・orphan店舗行・
「未確定」を「確定」扱いしない等）を含む。ファイルシステム・実サイトには一切触れない
（すべてbytesを直接組み立てて渡す純関数テスト）。
"""

from __future__ import annotations

import csv
import io
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.access_analytics_rakuten import (  # noqa: E402
    EXPECTED_HEADER_28,
    AccessAnalyticsError,
    validate_rakuten_device_csv,
)
from portal_app.services.billing_statements_rakuten import (  # noqa: E402
    EXPECTED_SHOP_DETAIL_HEADER_17,
    EXPECTED_SUMMARY_HEADER_12,
    BillPayError,
    validate_shop_detail_csv,
    validate_summary_csv,
)
from portal_app.services.billing_statements_yahoo import (  # noqa: E402
    EXPECTED_BILLING_RECEIPT_HEADER_7,
    EXPECTED_SETTLEMENT_HEADER_10,
    YahooBillingStatementsError,
    classify_statement_state,
    validate_yahoo_statement_csv,
)


def _rakuten_csv_bytes(rows: list[list[str]]) -> bytes:
    text = "\n".join(",".join(row) for row in rows) + "\n"
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def _rakuten_data_row(*, management_number: str = "ABC-001") -> list[str]:
    return [
        "1", "101", "", "12345", "テスト商品", management_number, "item001",
        "10000", "1", "1", "5", "5", "20%", "10000", "1", "1", "0", "4",
        "0", "0", "0", "30", "0", "0", "0%", "0", "0", "10",
    ]


def _rakuten_valid_rows(*, device_line: str = "対象端末: PC", period_line: str = "対象期間: 2026-07-10") -> list[list[str]]:
    return [
        ["楽天市場 商品ページ分析"],
        ["ショップ: テストショップ"],
        [period_line],
        ["表示形式: 日次"],
        [device_line],
        list(EXPECTED_HEADER_28),
        _rakuten_data_row(),
    ]


class ValidateRakutenDeviceCsvTest(unittest.TestCase):
    def test_valid_28_columns_pc(self) -> None:
        data = _rakuten_csv_bytes(_rakuten_valid_rows())
        row_count, header = validate_rakuten_device_csv(
            data, target_date=date(2026, 7, 10), expected_device_label="PC"
        )
        self.assertEqual(row_count, 1)
        self.assertEqual(header, EXPECTED_HEADER_28)

    def test_column_count_mismatch_raises_schema_mismatch(self) -> None:
        rows = _rakuten_valid_rows()
        rows[-1] = rows[-1][:-1]  # 28列 → 27列に破壊
        data = _rakuten_csv_bytes(rows)
        with self.assertRaises(AccessAnalyticsError) as ctx:
            validate_rakuten_device_csv(
                data, target_date=date(2026, 7, 10), expected_device_label="PC"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_MISMATCH")

    def test_device_label_mismatch_raises_data_not_updated(self) -> None:
        rows = _rakuten_valid_rows(device_line="対象端末: スマートフォン")
        data = _rakuten_csv_bytes(rows)
        with self.assertRaises(AccessAnalyticsError) as ctx:
            validate_rakuten_device_csv(
                data, target_date=date(2026, 7, 10), expected_device_label="PC"
            )
        self.assertEqual(ctx.exception.state, "DATA_NOT_UPDATED")

    def test_period_mismatch_raises_data_not_updated(self) -> None:
        rows = _rakuten_valid_rows(period_line="対象期間: 2026-07-11")
        data = _rakuten_csv_bytes(rows)
        with self.assertRaises(AccessAnalyticsError) as ctx:
            validate_rakuten_device_csv(
                data, target_date=date(2026, 7, 10), expected_device_label="PC"
            )
        self.assertEqual(ctx.exception.state, "DATA_NOT_UPDATED")

    def test_missing_bom_raises_schema_mismatch(self) -> None:
        text = "\n".join(",".join(row) for row in _rakuten_valid_rows()) + "\n"
        data = text.encode("utf-8")  # BOMなし
        with self.assertRaises(AccessAnalyticsError) as ctx:
            validate_rakuten_device_csv(
                data, target_date=date(2026, 7, 10), expected_device_label="PC"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_MISMATCH")

    def test_duplicate_management_number_raises_schema_mismatch(self) -> None:
        rows = _rakuten_valid_rows()
        rows.append(_rakuten_data_row())  # 同一商品管理番号を2行目にも追加
        data = _rakuten_csv_bytes(rows)
        with self.assertRaises(AccessAnalyticsError) as ctx:
            validate_rakuten_device_csv(
                data, target_date=date(2026, 7, 10), expected_device_label="PC"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_MISMATCH")


class ClassifyStatementStateTest(unittest.TestCase):
    def test_provisional(self) -> None:
        self.assertEqual(classify_statement_state("未確定"), "provisional")

    def test_final_exact_match(self) -> None:
        self.assertEqual(classify_statement_state("確定"), "final")

    def test_final_with_surrounding_whitespace(self) -> None:
        self.assertEqual(classify_statement_state(" 確定 "), "final")

    def test_unknown_for_other_text(self) -> None:
        self.assertEqual(classify_statement_state("処理中"), "unknown")

    def test_text_containing_both_tokens_is_provisional_not_final(self) -> None:
        # 「未確定」に「確定」が部分文字列として含まれるため、必ず未確定を先に判定する。
        self.assertEqual(classify_statement_state("未確定（確定待ち）"), "provisional")

    def test_confirmed_suffix_without_exact_match_is_unknown(self) -> None:
        # strip()後に完全一致しない「確定済み」は final 扱いしない。
        self.assertEqual(classify_statement_state("確定済み"), "unknown")


def _cp932_crlf_bytes(rows: list[list[str]]) -> bytes:
    # csv.writer で書き出すことで、金額列（例: "10,000"）の埋め込みカンマを
    # 正しく引用符でエスケープする（単純な ",".join だと列がずれて壊れるため）。
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return buffer.getvalue().encode("cp932")


class ValidateYahooStatementCsvTest(unittest.TestCase):
    def _billing_rows(self, *, used_on: str = "2026/07/01") -> list[list[str]]:
        return [
            list(EXPECTED_BILLING_RECEIPT_HEADER_7),
            [used_on, "ORDER001", "商品代金", "", "1000", "100", "1100"],
        ]

    def _settlement_rows(self, *, billing_tax: str = "100", receipt_tax: str = "50") -> list[list[str]]:
        return [
            list(EXPECTED_SETTLEMENT_HEADER_10),
            [
                "2026/07/01", "ORDER001", "商品代金", "1000", billing_tax, "1100",
                "支払手数料", "500", receipt_tax, "550",
            ],
        ]

    def test_valid_billing_csv(self) -> None:
        data = _cp932_crlf_bytes(self._billing_rows())
        row_count = validate_yahoo_statement_csv(
            data, statement_type="billing", target_month="2026-07"
        )
        self.assertEqual(row_count, 1)

    def test_valid_settlement_csv_with_dual_tax_columns(self) -> None:
        data = _cp932_crlf_bytes(self._settlement_rows())
        row_count = validate_yahoo_statement_csv(
            data, statement_type="settlement", target_month="2026-07"
        )
        self.assertEqual(row_count, 1)

    def test_header_change_raises_schema_drift(self) -> None:
        rows = self._billing_rows()
        rows[0] = list(rows[0])
        rows[0][-1] = "金額(税込)"  # ヘッダー改変
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(YahooBillingStatementsError) as ctx:
            validate_yahoo_statement_csv(
                data, statement_type="billing", target_month="2026-07"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_bom_raises_schema_drift(self) -> None:
        data = b"\xef\xbb\xbf" + _cp932_crlf_bytes(self._billing_rows())
        with self.assertRaises(YahooBillingStatementsError) as ctx:
            validate_yahoo_statement_csv(
                data, statement_type="billing", target_month="2026-07"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_column_count_mismatch_raises_schema_drift(self) -> None:
        header = ",".join(EXPECTED_BILLING_RECEIPT_HEADER_7)
        bad_row = "2026/07/01,ORDER001,商品代金,,1000,100"  # 6列（7列必要）
        text = header + "\r\n" + bad_row + "\r\n"
        data = text.encode("cp932")
        with self.assertRaises(YahooBillingStatementsError) as ctx:
            validate_yahoo_statement_csv(
                data, statement_type="billing", target_month="2026-07"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_invalid_cp932_bytes_raise_schema_drift(self) -> None:
        header = ",".join(EXPECTED_BILLING_RECEIPT_HEADER_7).encode("cp932")
        data = header + b"\r\n" + b"\x82\xff\r\n"  # cp932として不正なバイト列
        with self.assertRaises(YahooBillingStatementsError) as ctx:
            validate_yahoo_statement_csv(
                data, statement_type="billing", target_month="2026-07"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_month_mismatch_raises_schema_drift(self) -> None:
        data = _cp932_crlf_bytes(self._billing_rows(used_on="2026/08/01"))
        with self.assertRaises(YahooBillingStatementsError) as ctx:
            validate_yahoo_statement_csv(
                data, statement_type="billing", target_month="2026-07"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_non_numeric_settlement_tax_raises_schema_drift(self) -> None:
        data = _cp932_crlf_bytes(self._settlement_rows(billing_tax="N/A"))
        with self.assertRaises(YahooBillingStatementsError) as ctx:
            validate_yahoo_statement_csv(
                data, statement_type="settlement", target_month="2026-07"
            )
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")


class ValidateShopDetailCsvTest(unittest.TestCase):
    def _row(self, *, url: str = "https://example.com/shop001", amount_7: str = "-") -> list[str]:
        return [
            "2026/07/10", "S0001", "D0001", "shop001", url, "テスト店舗",
            "10,000", amount_7, "0", "支払", "商品代金", "商品A",
            "2026/07/01", "2026/07/31", "10,000", "1,000", "10%",
        ]

    def test_valid_17_columns(self) -> None:
        rows = [list(EXPECTED_SHOP_DETAIL_HEADER_17), self._row()]
        data = _cp932_crlf_bytes(rows)
        result = validate_shop_detail_csv(data)
        self.assertEqual(result.row_count, 1)
        self.assertEqual(result.issue_date, "2026/07/10")
        self.assertEqual(len(result.identity_hash), 64)

    def test_column_count_mismatch_raises_schema_drift(self) -> None:
        rows = [list(EXPECTED_SHOP_DETAIL_HEADER_17), self._row()[:-1]]
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(BillPayError) as ctx:
            validate_shop_detail_csv(data)
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_fullwidth_url_header_is_required_not_halfwidth(self) -> None:
        # 5列目は一次情報どおり全角「ＵＲＬ」を要求し、半角「URL」では拒否する
        # （半角へ自動修正しないことの回帰確認）。
        header = list(EXPECTED_SHOP_DETAIL_HEADER_17)
        header[4] = "URL"  # 全角→半角へ壊す
        data = _cp932_crlf_bytes([header, self._row()])
        with self.assertRaises(BillPayError) as ctx:
            validate_shop_detail_csv(data)
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_lone_dash_and_signed_negative_are_distinguished(self) -> None:
        # 単独 "-" は欠損として許容し、"-500" は符号付き負数として許容する
        # （どちらも例外を送出しない）。
        rows = [
            list(EXPECTED_SHOP_DETAIL_HEADER_17),
            self._row(amount_7="-"),
            self._row(amount_7="-500"),
        ]
        data = _cp932_crlf_bytes(rows)
        result = validate_shop_detail_csv(data)
        self.assertEqual(result.row_count, 2)

    def test_malformed_amount_raises_schema_drift(self) -> None:
        rows = [list(EXPECTED_SHOP_DETAIL_HEADER_17), self._row(amount_7="12,3")]
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(BillPayError) as ctx:
            validate_shop_detail_csv(data)
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_identity_mismatch_across_rows_raises_schema_drift(self) -> None:
        rows = [
            list(EXPECTED_SHOP_DETAIL_HEADER_17),
            self._row(),
            self._row(url="https://example.com/shop002"),
        ]
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(BillPayError) as ctx:
            validate_shop_detail_csv(data)
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")


class ValidateSummaryCsvTest(unittest.TestCase):
    def _enterprise_row(self) -> list[str]:
        return [
            "C0001", "本社", "-", "-", "2026/07/15", "1,000", "2026/06/30",
            "1,500", "2026/07/10", "500", "2026/07/20", "2026/07/25",
        ]

    def _shop_row(self, *, billing: str = "200", payment: str = "300", settlement: str = "100") -> list[str]:
        return [
            "-", "テスト店舗", "shop001", "https://example.com", "2026/07/15",
            billing, "2026/06/30", payment, "2026/07/10", settlement,
            "2026/07/20", "2026/07/25",
        ]

    def test_valid_enterprise_to_shop_group(self) -> None:
        rows = [list(EXPECTED_SUMMARY_HEADER_12), self._enterprise_row(), self._shop_row()]
        data = _cp932_crlf_bytes(rows)
        result = validate_summary_csv(data)
        self.assertEqual(result.row_count, 2)
        self.assertEqual(result.group_count, 1)

    def test_orphan_shop_row_raises_orphan_shop_row(self) -> None:
        rows = [list(EXPECTED_SUMMARY_HEADER_12), self._shop_row()]  # 企業行なしで店舗行のみ
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(BillPayError) as ctx:
            validate_summary_csv(data)
        self.assertEqual(ctx.exception.state, "ORPHAN_SHOP_ROW")

    def test_settlement_amount_mismatch_raises_schema_drift(self) -> None:
        rows = [
            list(EXPECTED_SUMMARY_HEADER_12),
            self._enterprise_row(),
            self._shop_row(billing="200", payment="300", settlement="999"),  # 300-200 != 999
        ]
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(BillPayError) as ctx:
            validate_summary_csv(data)
        self.assertEqual(ctx.exception.state, "SCHEMA_DRIFT")

    def test_empty_enterprise_group_raises_error(self) -> None:
        # 企業行はあるが店舗行が1件もないgroup
        rows = [list(EXPECTED_SUMMARY_HEADER_12), self._enterprise_row()]
        data = _cp932_crlf_bytes(rows)
        with self.assertRaises(BillPayError) as ctx:
            validate_summary_csv(data)
        self.assertEqual(ctx.exception.state, "EMPTY_ENTERPRISE_GROUP")


if __name__ == "__main__":
    unittest.main()
