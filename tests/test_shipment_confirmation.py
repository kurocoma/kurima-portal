"""出荷確定サービスの単体テスト（合成データのみ・実在個人情報は使わない）。

対象:
- 出荷予定日が空の行の既定値が「当日（実行日）」になること
- プレビュー結果に店舗ごとの件数（store_counts）が入ること
- normalize_barcode_value の5規則（空白/D除去/00000除去/.0除去/先頭ゼロ）
- アップロード候補CSVの行検証（空欄=エラーで反映不可、重複・日付・非数字=警告で反映可）
- NE反映候補の自動選択（yamato_to-ne*.csv かつ3列ヘッダーの最新のみ。別用途CSVは拒否）
- 発送伝票番号の解決優先順位（ヤマト→しまのや→クリックポスト→レターパック）と競合警告
- 監査 jsonl に生の送り先名（個人名）を書かないこと（マスクされること）

実フォルダへ書き込まないよう、KURIMA_PORTAL_ROOT を一時フォルダへ向けて実行する。
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portal_app.services.shipment_confirmation as shipment_confirmation_module  # noqa: E402
from portal_app.services.shipment_confirmation import (  # noqa: E402
    _latest_completion_csv,
    _resolve_tracking,
    create_shipment_slip_import_csv,
    normalize_barcode_value,
    preview_next_engine_shipment_upload,
    preview_shipment_slip_import,
    write_shipment_confirmation_rows,
)


TODAY = date.today().strftime("%Y/%m/%d")


def _write_csv(path: Path, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="cp932", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(headers)
        writer.writerows(rows)


class TempPortalRootMixin:
    """一時フォルダを KURIMA_PORTAL_ROOT として使う（実データ・実フォルダ非依存）。"""

    def setUp(self) -> None:  # noqa: N802 (unittest命名)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # find_portal_paths() がポータルルートと認識する最小レイアウト
        (root / "商品管理シート.xlsm").write_bytes(b"")
        (root / "ネクストエンジン" / "発注関連" / "受注明細一覧").mkdir(parents=True)
        self._env_backup = os.environ.get("KURIMA_PORTAL_ROOT")
        os.environ["KURIMA_PORTAL_ROOT"] = str(root)
        self.portal_root = root

    def tearDown(self) -> None:  # noqa: N802
        if self._env_backup is None:
            os.environ.pop("KURIMA_PORTAL_ROOT", None)
        else:
            os.environ["KURIMA_PORTAL_ROOT"] = self._env_backup
        self._tmp.cleanup()


class WriteRowsDefaultShippingDateTest(TempPortalRootMixin, unittest.TestCase):
    def test_empty_shipping_date_defaults_to_today(self) -> None:
        result = write_shipment_confirmation_rows(
            [{"伝票番号": "68001", "発送伝票番号": "400000000001", "出荷予定日": ""}],
            preview_limit=5,
        )
        self.assertEqual(result.output_rows, 1)
        self.assertEqual(result.preview_rows[0]["出荷予定日"], TODAY)
        assert result.output_csv is not None
        with result.output_csv.open("r", encoding="cp932", newline="") as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual(rows[0]["出荷予定日"], TODAY)

    def test_source_shipping_date_wins_over_default(self) -> None:
        result = write_shipment_confirmation_rows(
            [{"伝票番号": "68002", "発送伝票番号": "400000000002", "出荷予定日": "2026/07/01"}],
            preview_limit=5,
        )
        self.assertEqual(result.output_rows, 1)
        self.assertEqual(result.preview_rows[0]["出荷予定日"], "2026/07/01")

    def test_row_without_tracking_is_still_excluded(self) -> None:
        # 出荷予定日を当日補完しても、発送伝票番号が無い行はCSVへ出力しない（既存契約の維持）
        result = write_shipment_confirmation_rows(
            [{"伝票番号": "68003", "発送伝票番号": "", "出荷予定日": ""}],
            preview_limit=5,
        )
        self.assertEqual(result.output_rows, 0)
        self.assertIsNone(result.output_csv)


class PreviewStoreCountsTest(TempPortalRootMixin, unittest.TestCase):
    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        buyer_dir = (
            self.portal_root / "ネクストエンジン" / "ネクストエンジン受注データ" / "購入者データ"
        )
        _write_csv(
            buyer_dir / "buyer.csv",
            ("店舗", "受注番号", "伝票番号", "送り先名"),
            [
                ("テスト店舗A", "A0001", "68101", "架空 太郎"),
                ("テスト店舗A", "A0002", "68102", "架空 次郎"),
                ("テスト店舗B", "B0001", "68103", "架空 三郎"),
            ],
        )
        yamato_dir = self.portal_root / "ネクストエンジン" / "yamato-okurizyo"
        _write_csv(
            yamato_dir / "data0001.csv",
            ("お客様管理番号", "伝票番号", "出荷予定日"),
            [
                ("68101", "400100000001", "2026/07/01"),
                ("68102", "400100000002", ""),
            ],
        )

    def test_store_counts_per_store(self) -> None:
        result = preview_shipment_slip_import(
            order_numbers=("68101", "68102", "68103"), preview_limit=10
        )
        self.assertEqual(dict(result.store_counts), {"テスト店舗A": 2, "テスト店舗B": 1})

    def test_mapping_default_shipping_date_is_today(self) -> None:
        result = preview_shipment_slip_import(
            order_numbers=("68101", "68102", "68103"), preview_limit=10
        )
        by_denpyo = {row["伝票番号"]: row for row in result.preview_rows}
        # ヤマトCSVに出荷予定日がある行はその値を優先
        self.assertEqual(by_denpyo["68101"]["出荷予定日"], "2026/07/01")
        # ヤマト側の出荷予定日が空の行・ソース未一致の行は当日が既定値
        self.assertEqual(by_denpyo["68102"]["出荷予定日"], TODAY)
        self.assertEqual(by_denpyo["68103"]["出荷予定日"], TODAY)

    def test_store_counts_bucket_for_unmatched_rows(self) -> None:
        result = preview_shipment_slip_import(order_numbers=("99999",), preview_limit=10)
        self.assertEqual(dict(result.store_counts), {"店舗未一致": 1})


class NormalizeBarcodeValueTest(unittest.TestCase):
    """normalize_barcode_value の5規則を1規則1ケース以上で検証する。"""

    CASES = (
        # 規則1: 前後空白除去
        ("rule1_whitespace", "  68001  ", "68001"),
        # 規則2: D / d 除去
        ("rule2_upper_d", "D68002", "68002"),
        ("rule2_lower_d", "68003d", "68003"),
        # 規則3: 00000 除去
        ("rule3_five_zeros", "50000012", "512"),
        # 規則4: Excel由来の末尾 .0 除去
        ("rule4_trailing_dot_zero", "68531.0", "68531"),
        # 規則5: 数値なら先頭ゼロを落とす
        ("rule5_leading_zeros", "068531", "68531"),
        ("rule5_all_zeros_keeps_single_zero", "0000", "0"),
        # 複合: 画面プレースホルダ例（D除去→00000除去→先頭ゼロ）
        ("combined_scanner_example", "D0000068531", "68531"),
    )

    def test_rules(self) -> None:
        for label, raw, expected in self.CASES:
            with self.subTest(label=label, raw=raw):
                self.assertEqual(normalize_barcode_value(raw), expected)


class ValidateUploadRowsTest(TempPortalRootMixin, unittest.TestCase):
    """アップロード候補CSVの行検証。空欄=エラー（反映不可）、重複・日付・非数字=警告（反映可）。"""

    def _preview(self, filename: str, rows: list[tuple[str, str, str]]):
        path = self.portal_root / "upload_cases" / filename
        _write_csv(path, ("伝票番号", "発送伝票番号", "出荷予定日"), rows)
        return preview_next_engine_shipment_upload(upload_csv=path, preview_limit=10)

    def test_blank_cells_are_errors_and_block_upload(self) -> None:
        result = self._preview(
            "yamato_to-ne_blank_case.csv",
            [
                ("", "400000000001", TODAY),  # 伝票番号 空
                ("68002", "", TODAY),  # 発送伝票番号 空
                ("68003", "400000000003", ""),  # 出荷予定日 空
            ],
        )
        self.assertFalse(result.ready_to_upload)
        joined = " / ".join(result.errors)
        for header in ("伝票番号", "発送伝票番号", "出荷予定日"):
            self.assertIn(f"『{header}』が空の行があります", joined)

    def test_duplicates_dates_and_non_digits_are_warnings_not_blocking(self) -> None:
        result = self._preview(
            "yamato_to-ne_warn_case.csv",
            [
                ("68011", "400000000011", TODAY),
                ("68011", "400000000012", TODAY),  # 伝票番号 重複
                ("68012", "4000ABC00013", TODAY),  # 発送伝票番号 非数字
                ("68013", "400000000011", TODAY),  # 発送伝票番号 重複
                ("68014", "400000000014", "2026-07-05"),  # 日付形式外
                ("68015", "400000000015", "2020/01/01"),  # 今日以外
            ],
        )
        self.assertTrue(result.ready_to_upload)
        self.assertEqual(result.errors, ())
        joined = " / ".join(result.warnings)
        self.assertIn("伝票番号が重複しています", joined)
        self.assertIn("発送伝票番号が重複しています", joined)
        self.assertIn("数字以外を含む行があります", joined)
        self.assertIn("YYYY/MM/DD 形式ではない行があります", joined)
        self.assertIn(f"今日（{TODAY}）以外の行があります", joined)


class LatestCompletionCsvTest(TempPortalRootMixin, unittest.TestCase):
    """NE反映候補は yamato_to-ne*.csv（3列ヘッダー）の最新のみ。別用途CSVは絶対に選ばない。"""

    UPLOAD_HEADERS = ("伝票番号", "発送伝票番号", "出荷予定日")

    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        self.completion_dir = self.portal_root / "ネクストエンジン" / "完成データ"

    def _put(
        self,
        name: str,
        headers: tuple[str, ...],
        rows: list[tuple[str, ...]],
        *,
        age_seconds: float,
    ) -> Path:
        path = self.completion_dir / name
        _write_csv(path, headers, rows)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_only_latest_valid_yamato_to_ne_is_selected(self) -> None:
        row = ("68001", "400000000001", TODAY)
        # 別用途CSV（より新しくても候補にしてはならない）
        self._put("ne-to-yamato2607050900.csv", self.UPLOAD_HEADERS, [row], age_seconds=10)
        self._put("shipment_confirmation_import.csv", self.UPLOAD_HEADERS, [row], age_seconds=10)
        # ヘッダー不正の yamato_to-ne（最新でも候補にしない）
        self._put("yamato_to-nebroken.csv", ("A", "B"), [("1", "2")], age_seconds=0)
        # 有効な yamato_to-ne 2つ（新しい方が選ばれる）
        self._put("yamato_to-ne2607050001.csv", self.UPLOAD_HEADERS, [row], age_seconds=300)
        expected = self._put(
            "yamato_to-ne2607050002.csv", self.UPLOAD_HEADERS, [row], age_seconds=100
        )

        warnings: list[str] = []
        selected = _latest_completion_csv(warnings)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, expected.name)

        # dry-run 経由でも同じ候補が選ばれ、反映可と判定される
        result = preview_next_engine_shipment_upload(preview_limit=5)
        self.assertIsNotNone(result.upload_csv)
        self.assertEqual(result.upload_csv.name, expected.name)
        self.assertTrue(result.ready_to_upload)

    def test_no_candidate_when_only_foreign_csvs_exist(self) -> None:
        row = ("68001", "400000000001", TODAY)
        self._put("ne-to-yamato2607050900.csv", self.UPLOAD_HEADERS, [row], age_seconds=10)
        self._put("shipment_confirmation_import.csv", self.UPLOAD_HEADERS, [row], age_seconds=10)

        warnings: list[str] = []
        self.assertIsNone(_latest_completion_csv(warnings))
        self.assertTrue(any("yamato_to-ne" in warning for warning in warnings))

        result = preview_next_engine_shipment_upload(preview_limit=5)
        self.assertFalse(result.ready_to_upload)


def _tracking_maps(**overrides) -> dict[str, dict]:
    maps: dict[str, dict] = {
        "yamato": {},
        "yamato_date": {},
        "clickpost": {},
        "clickpost_name": {},
        "clickpost_ambiguous": {},
        "letterpack": {},
        "shimanoya": {},
    }
    maps.update(overrides)
    return maps


class ResolveTrackingPriorityTest(unittest.TestCase):
    """発送伝票番号の解決優先順位（ヤマト→しまのや→クリックポスト→レターパック）と競合警告。"""

    def test_yamato_wins_over_all_with_conflict_warning(self) -> None:
        maps = _tracking_maps(
            yamato={"68001": "400000000001"},
            shimanoya={"1234567": "300000000001"},
            clickpost={"68001": "200000000001"},
            letterpack={"68001": "100000000001"},
        )
        tracking, source, ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="A1234567", tracking_maps=maps
        )
        self.assertEqual((tracking, source, ambiguous), ("400000000001", "ヤマト運輸", False))
        self.assertEqual(len(warns), 1)
        self.assertIn("競合しています", warns[0])
        self.assertIn("優先順位により ヤマト運輸 を採用", warns[0])

    def test_shimanoya_wins_over_clickpost_and_letterpack(self) -> None:
        maps = _tracking_maps(
            shimanoya={"1234567": "300000000001"},
            clickpost={"68001": "200000000001"},
            letterpack={"68001": "100000000001"},
        )
        tracking, source, _ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="A1234567", tracking_maps=maps
        )
        self.assertEqual((tracking, source), ("300000000001", "しまのや"))
        self.assertIn("優先順位により しまのや を採用", warns[0])

    def test_clickpost_wins_over_letterpack(self) -> None:
        maps = _tracking_maps(
            clickpost={"68001": "200000000001"},
            letterpack={"68001": "100000000001"},
        )
        tracking, source, _ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="", tracking_maps=maps
        )
        self.assertEqual((tracking, source), ("200000000001", "クリックポスト"))
        self.assertIn("優先順位により クリックポスト を採用", warns[0])

    def test_clickpost_name_match_is_used_when_direct_map_is_empty(self) -> None:
        maps = _tracking_maps(clickpost_name={"68001": "200000000002"})
        tracking, source, ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="", tracking_maps=maps
        )
        self.assertEqual((tracking, source, ambiguous, warns), ("200000000002", "クリックポスト", False, []))

    def test_letterpack_alone_resolves_without_warning(self) -> None:
        maps = _tracking_maps(letterpack={"68001": "100000000001"})
        tracking, source, ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="", tracking_maps=maps
        )
        self.assertEqual((tracking, source, ambiguous, warns), ("100000000001", "レターパック", False, []))

    def test_same_number_across_sources_has_no_conflict_warning(self) -> None:
        maps = _tracking_maps(
            yamato={"68001": "400000000001"},
            clickpost={"68001": "400000000001"},
        )
        tracking, source, _ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="", tracking_maps=maps
        )
        self.assertEqual((tracking, source), ("400000000001", "ヤマト運輸"))
        self.assertEqual(warns, [])

    def test_ambiguous_clickpost_name_match_is_not_auto_resolved(self) -> None:
        maps = _tracking_maps(
            clickpost_ambiguous={"68001": ["200000000001", "200000000002"]}
        )
        tracking, source, ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="", tracking_maps=maps
        )
        self.assertEqual((tracking, source, ambiguous), ("", "", True))
        self.assertIn("候補が複数あります", warns[0])

    def test_no_match_returns_empty(self) -> None:
        tracking, source, ambiguous, warns = _resolve_tracking(
            denpyo_no="68001", order_no="", tracking_maps=_tracking_maps()
        )
        self.assertEqual((tracking, source, ambiguous, warns), ("", "", False, []))


class AuditMaskTest(TempPortalRootMixin, unittest.TestCase):
    """監査 jsonl に生の送り先名（個人名）を書かない（先頭1文字＋* にマスクされる）。"""

    def test_audit_jsonl_masks_recipient_name(self) -> None:
        buyer_dir = (
            self.portal_root / "ネクストエンジン" / "ネクストエンジン受注データ" / "購入者データ"
        )
        _write_csv(
            buyer_dir / "buyer.csv",
            ("店舗", "受注番号", "伝票番号", "送り先名"),
            [("テスト店舗A", "A0001", "68101", "架空 太郎")],
        )
        yamato_dir = self.portal_root / "ネクストエンジン" / "yamato-okurizyo"
        _write_csv(
            yamato_dir / "data0001.csv",
            ("お客様管理番号", "伝票番号", "出荷予定日"),
            [("68101", "400100000001", TODAY)],
        )
        audit_dir = self.portal_root / "audit_test"
        audit_path = audit_dir / "shipment_confirmation_audit.jsonl"
        with mock.patch.object(
            shipment_confirmation_module, "AUDIT_LOG_DIR", audit_dir
        ), mock.patch.object(shipment_confirmation_module, "AUDIT_LOG_PATH", audit_path):
            result = create_shipment_slip_import_csv(order_numbers=("68101",), preview_limit=10)

        self.assertTrue(audit_path.is_file())
        text = audit_path.read_text(encoding="utf-8")
        # 生の送り先名は監査ログに現れない（マスク値のみ）
        self.assertNotIn("架空 太郎", text)
        self.assertIn("架****", text)
        # 画面プレビュー（サービス戻り値）はマスクしない（手動確認用の表示はそのまま）
        self.assertEqual(result.preview_rows[0]["送り先名"], "架空 太郎")


if __name__ == "__main__":
    unittest.main()
