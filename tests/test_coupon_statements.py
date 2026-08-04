"""クーポン明細取得の純ロジック単体テスト。

対象（ブラウザ操作を伴わない部分だけ）:
- 前月期間の算出（年跨ぎ・月末日数）
- YYMM 命名と保存ファイル名（{YYMM}_coupon.csv / {N}月クーポン利用 .xlsm）
- 保存先パス解決（KURIMA_COUPON_DIR / KURIMA_PORTAL_ROOT の複数PC対応）
- 既存ブックの表記ゆれ検出と「上書きしない」複製
- 複製ブックの Power Query 参照セル（filePath / fileName）書き換え
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import coupon_statements as mod  # noqa: E402
from portal_app.services.coupon_statements import (  # noqa: E402
    COUPON_TEMPLATE_ID,
    COUPON_TEMPLATE_LABEL,
    CSV_DOWNLOAD_URL,
    ORDER_DATE_LABEL_KEYWORD,
    SHIPPING_DATE_RADIO_ID,
    CouponStatementsError,
    copy_coupon_workbook,
    ensure_rakuten_login_env,
    find_existing_workbook,
    previous_month,
    read_workbook_source_cells,
    replace_cached_cell_value,
    resolve_period,
    stamp_workbook_source,
    workbook_name_variants,
)
from portal_app.services.paths import CouponPaths, find_coupon_paths  # noqa: E402


SHEET_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet><sheetData>'
    '<row r="2"><c r="A2" s="8" t="str">'
    '<f ca="1">SUBSTITUTE(OneDriveUrlToLocalPath(C2),"a","b")</f>'
    "<v>C:\\Users\\kamiz\\株式会社しまのや\\くりまポータル - ドキュメント\\入金明細\\クーポン\\</v>"
    "</c>"
    '<c r="C2" s="9" t="str"><f ca="1">LEFT(CELL("filename"),1)</f><v>https://example/</v></c>'
    "</row>"
    '<row r="9"><c r="A9" s="1" t="str">'
    '<f ca="1">_xlfn.CONCAT(TEXT(EDATE(TODAY(),-1),"yymm"),C8)</f><v>2411_coupon.csv</v>'
    "</c></row>"
    "</sheetData></worksheet>"
)


class PreviousMonthTest(unittest.TestCase):
    def test_mid_year(self) -> None:
        self.assertEqual(previous_month(date(2026, 8, 3)), (2026, 7))

    def test_year_boundary(self) -> None:
        self.assertEqual(previous_month(date(2026, 1, 15)), (2025, 12))

    def test_end_of_month_execution(self) -> None:
        self.assertEqual(previous_month(date(2026, 3, 31)), (2026, 2))


class PeriodTest(unittest.TestCase):
    def test_period_covers_whole_previous_month(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        self.assertEqual(period.start_date, date(2026, 7, 1))
        self.assertEqual(period.end_date, date(2026, 7, 31))
        self.assertEqual(period.start_text, "2026-07-01")
        self.assertEqual(period.end_text, "2026-07-31")
        self.assertEqual(period.label, "2026年7月")

    def test_february_last_day(self) -> None:
        self.assertEqual(resolve_period(date(2026, 3, 1)).end_date, date(2026, 2, 28))

    def test_february_last_day_in_leap_year(self) -> None:
        self.assertEqual(resolve_period(date(2028, 3, 1)).end_date, date(2028, 2, 29))

    def test_yymm_and_file_names(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        self.assertEqual(period.yymm, "2607")
        self.assertEqual(period.csv_name, "2607_coupon.csv")
        self.assertEqual(period.workbook_name, "7月クーポン利用 .xlsm")

    def test_yymm_zero_padded_month(self) -> None:
        period = resolve_period(date(2026, 2, 10))
        self.assertEqual(period.yymm, "2601")
        self.assertEqual(period.csv_name, "2601_coupon.csv")
        self.assertEqual(period.workbook_name, "1月クーポン利用 .xlsm")

    def test_yymm_across_year_boundary(self) -> None:
        period = resolve_period(date(2026, 1, 5))
        self.assertEqual(period.yymm, "2512")
        self.assertEqual(period.csv_name, "2512_coupon.csv")
        self.assertEqual(period.workbook_name, "12月クーポン利用 .xlsm")


class PathResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_explicit_coupon_dir_wins(self) -> None:
        coupon_dir = self.root / "coupon"
        coupon_dir.mkdir()
        with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": str(coupon_dir)}):
            paths = find_coupon_paths()
        self.assertEqual(paths.root, coupon_dir)
        self.assertEqual(paths.csv_dir, coupon_dir / "csv")
        self.assertTrue(paths.template_path.name.endswith(".xlsm"))

    def test_portal_root_layout(self) -> None:
        coupon_dir = self.root / "入金明細" / "クーポン"
        coupon_dir.mkdir(parents=True)
        with mock.patch.dict(
            os.environ, {"KURIMA_COUPON_DIR": "", "KURIMA_PORTAL_ROOT": str(self.root)}
        ):
            paths = find_coupon_paths()
        self.assertEqual(paths.root, coupon_dir)

    def test_template_name_override(self) -> None:
        coupon_dir = self.root / "coupon"
        coupon_dir.mkdir()
        with mock.patch.dict(
            os.environ,
            {"KURIMA_COUPON_DIR": str(coupon_dir), "KURIMA_COUPON_TEMPLATE_NAME": "tpl.xlsm"},
        ):
            paths = find_coupon_paths()
        self.assertEqual(paths.template_path, coupon_dir / "tpl.xlsm")

    def test_missing_dir_raises_with_candidates(self) -> None:
        # 実行PCの実フォルダを拾わないよう、候補探索そのものを差し替える
        # （candidate_portal_roots は既定候補＝ホームディレクトリ配下も返すため）。
        missing = self.root / "nope"
        with mock.patch(
            "portal_app.services.paths.candidate_portal_roots", return_value=[missing]
        ):
            with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": ""}):
                with self.assertRaises(FileNotFoundError) as ctx:
                    find_coupon_paths()
        self.assertIn(str(missing), str(ctx.exception))


class WorkbookNameTest(unittest.TestCase):
    def test_variants_cover_existing_naming(self) -> None:
        variants = workbook_name_variants(5)
        self.assertIn("5月クーポン利用 .xlsm", variants)
        self.assertIn("5月クーポン利用.xlsm", variants)
        self.assertIn("５月クーポン利用.xlsm", variants)

    def test_two_digit_month(self) -> None:
        self.assertIn("12月クーポン利用 .xlsm", workbook_name_variants(12))
        self.assertIn("１２月クーポン利用.xlsm", workbook_name_variants(12))

    def test_find_existing_detects_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "５月クーポン利用.xlsm").write_bytes(b"x")
            found = find_existing_workbook(root, 5)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "５月クーポン利用.xlsm")

    def test_find_existing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_existing_workbook(Path(tmp), 5))


class CachedCellRewriteTest(unittest.TestCase):
    def test_replaces_only_target_cell(self) -> None:
        updated = replace_cached_cell_value(SHEET_XML, "A2", "D:\\portal\\クーポン\\")
        self.assertIn("<v>D:\\portal\\クーポン\\</v>", updated)
        self.assertNotIn("kamiz", updated)
        # 数式は残す（Excelで開いた際の再計算経路を壊さない）
        self.assertIn("OneDriveUrlToLocalPath", updated)
        # 他セルは触らない
        self.assertIn("<v>https://example/</v>", updated)
        self.assertIn("<v>2411_coupon.csv</v>", updated)

    def test_replaces_file_name_cell(self) -> None:
        updated = replace_cached_cell_value(SHEET_XML, "A9", "2607_coupon.csv")
        self.assertIn("<v>2607_coupon.csv</v>", updated)
        self.assertNotIn("<v>2411_coupon.csv</v>", updated)

    def test_escapes_xml_special_characters(self) -> None:
        updated = replace_cached_cell_value(SHEET_XML, "A9", "a&b<c>.csv")
        self.assertIn("<v>a&amp;b&lt;c&gt;.csv</v>", updated)

    def test_missing_cell_raises(self) -> None:
        with self.assertRaises(CouponStatementsError) as ctx:
            replace_cached_cell_value(SHEET_XML, "Z99", "x")
        self.assertEqual(ctx.exception.state, "TEMPLATE_CONTRACT_CHANGED")


def _make_template(path: Path) -> None:
    """テンプレート xlsm の最小再現（sheet1.xml と他パートを1つずつ）。"""

    with zipfile.ZipFile(path, "w") as book:
        book.writestr("xl/worksheets/sheet1.xml", SHEET_XML)
        book.writestr("xl/vbaProject.bin", b"\x00\x01binary")
        book.writestr("customXml/item2.xml", "<DataMashup/>")


class StampWorkbookTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_stamp_rewrites_source_cells_and_keeps_other_parts(self) -> None:
        book = self.root / "book.xlsm"
        _make_template(book)
        stamp_workbook_source(
            book,
            file_path_value="D:\\portal\\入金明細\\クーポン",
            file_name_value="2607_coupon.csv",
        )
        values = read_workbook_source_cells(book)
        # 末尾の区切り文字は自動補完される（M式が filePath & "csv\" & fileName で結合するため）
        self.assertEqual(values["filePath"], "D:\\portal\\入金明細\\クーポン\\")
        self.assertEqual(values["fileName"], "2607_coupon.csv")
        with zipfile.ZipFile(book) as archive:
            self.assertEqual(archive.read("xl/vbaProject.bin"), b"\x00\x01binary")
            self.assertIn("customXml/item2.xml", archive.namelist())


class CopyWorkbookTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.template = self.root / "【テンプレート】○○月クーポン利用 .xlsm"
        _make_template(self.template)
        self.paths = CouponPaths(
            root=self.root,
            csv_dir=self.root / "csv",
            template_path=self.template,
        )

    def test_creates_copy_with_stamped_source(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        destination, created, warnings = copy_coupon_workbook(self.paths, period)
        self.assertTrue(created)
        self.assertEqual(destination.name, "7月クーポン利用 .xlsm")
        self.assertEqual(warnings, [])
        values = read_workbook_source_cells(destination)
        self.assertEqual(values["filePath"], str(self.root) + "\\")
        self.assertEqual(values["fileName"], "2607_coupon.csv")

    def test_existing_file_is_not_overwritten(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        existing = self.root / "7月クーポン利用 .xlsm"
        existing.write_bytes(b"already here")
        destination, created, warnings = copy_coupon_workbook(self.paths, period)
        self.assertFalse(created)
        self.assertEqual(destination, existing)
        self.assertEqual(existing.read_bytes(), b"already here")
        self.assertTrue(any("スキップ" in warning for warning in warnings))

    def test_existing_variant_name_is_respected(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        existing = self.root / "7月クーポン利用.xlsm"  # 拡張子前のスペースなし
        existing.write_bytes(b"already here")
        destination, created, _ = copy_coupon_workbook(self.paths, period)
        self.assertFalse(created)
        self.assertEqual(destination, existing)
        self.assertFalse((self.root / "7月クーポン利用 .xlsm").exists())

    def test_missing_template_is_reported_not_raised(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        self.template.unlink()
        destination, created, warnings = copy_coupon_workbook(self.paths, period)
        self.assertIsNone(destination)
        self.assertFalse(created)
        self.assertTrue(any("テンプレート" in warning for warning in warnings))

    def test_no_temp_file_left_behind(self) -> None:
        period = resolve_period(date(2026, 8, 3))
        copy_coupon_workbook(self.paths, period)
        leftovers = [path.name for path in self.root.iterdir() if path.name.startswith(".")]
        self.assertEqual(leftovers, [])


class DownloadContractTest(unittest.TestCase):
    """手順書（rakuten-playwright-daily-sales-download.md）との契約差分を固定する。"""

    def test_csv_download_url_matches_document(self) -> None:
        self.assertEqual(
            CSV_DOWNLOAD_URL,
            "https://csvdl-rp.rms.rakuten.co.jp/rms/mall/csvdl/CD02_01_001"
            "?dataType=opp_order#result",
        )

    def test_coupon_template_is_value_3_not_all_columns(self) -> None:
        self.assertEqual(COUPON_TEMPLATE_ID, "3")
        self.assertNotEqual(COUPON_TEMPLATE_ID, "-1")
        self.assertEqual(COUPON_TEMPLATE_LABEL, "クーポン")

    def test_order_date_is_used_and_shipping_radio_excluded(self) -> None:
        self.assertEqual(ORDER_DATE_LABEL_KEYWORD, "注文日")
        # 手順書の r06 は「発送日」。クーポン明細では選択対象から外す。
        self.assertEqual(SHIPPING_DATE_RADIO_ID, "r06")

    def test_zero_row_marker_from_document(self) -> None:
        self.assertIn("この条件でのデータ件数は0件です", mod._NO_DATA_MARKERS)

    def test_auth_required_marker_from_document(self) -> None:
        self.assertEqual(mod._AUTH_REQUIRED_MARKER, "認証が必要です")

    def test_data_wait_default_is_longer_than_daily_sales_120s(self) -> None:
        self.assertGreaterEqual(mod._DEFAULT_DATA_WAIT_SECONDS, 600)


class LoginEnvFallbackTest(unittest.TestCase):
    """認証3種のうち R-Login / 楽天会員は env 未設定なら ID・PW.xlsx から補う。"""

    def test_fills_only_missing_keys(self) -> None:
        env = {
            "KURIMA_RAKUTEN_KANRI_LOGIN_ID": "already-set",
            "KURIMA_RAKUTEN_KANRI_PASSWORD": "already-set-pw",
            "KURIMA_RAKUTEN_LOGIN_ID": "",
            "KURIMA_RAKUTEN_LOGIN_PASSWORD": "",
        }
        calls: list[str] = []

        def fake_loader(label: str):
            calls.append(label)
            return ("member-id", "member-pw")

        with mock.patch.dict(os.environ, env):
            with mock.patch(
                "portal_app.services.credentials.load_site_credential", fake_loader
            ):
                filled = ensure_rakuten_login_env()
                self.assertEqual(os.environ["KURIMA_RAKUTEN_KANRI_LOGIN_ID"], "already-set")
                self.assertEqual(os.environ["KURIMA_RAKUTEN_LOGIN_ID"], "member-id")
                self.assertEqual(os.environ["KURIMA_RAKUTEN_LOGIN_PASSWORD"], "member-pw")
        self.assertEqual(calls, ["楽天(ユーザー認証)"])
        self.assertEqual(filled, ["KURIMA_RAKUTEN_LOGIN_ID"])

    def test_missing_workbook_does_not_raise(self) -> None:
        env = {
            "KURIMA_RAKUTEN_KANRI_LOGIN_ID": "",
            "KURIMA_RAKUTEN_KANRI_PASSWORD": "",
            "KURIMA_RAKUTEN_LOGIN_ID": "",
            "KURIMA_RAKUTEN_LOGIN_PASSWORD": "",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch(
                "portal_app.services.credentials.load_site_credential",
                side_effect=FileNotFoundError("no xlsx"),
            ):
                self.assertEqual(ensure_rakuten_login_env(), [])

    def test_no_call_when_all_env_present(self) -> None:
        env = {
            "KURIMA_RAKUTEN_KANRI_LOGIN_ID": "a",
            "KURIMA_RAKUTEN_KANRI_PASSWORD": "b",
            "KURIMA_RAKUTEN_LOGIN_ID": "c",
            "KURIMA_RAKUTEN_LOGIN_PASSWORD": "d",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch(
                "portal_app.services.credentials.load_site_credential",
                side_effect=AssertionError("呼ばれてはいけない"),
            ):
                self.assertEqual(ensure_rakuten_login_env(), [])


if __name__ == "__main__":
    unittest.main()
