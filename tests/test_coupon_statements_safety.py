"""クーポン明細取得の「安全側」挙動の単体テスト。

test_coupon_statements.py（期間計算・命名・パス解決）に対して、こちらは
実運用で効く防御の検証:

- 保存先が Excel でロックされているときに型付きエラー（FILE_LOCKED）になること
- ダウンロード物がCSVでないときに保存せず既存ファイルを守ること
- 長い待機ループの中で「実行を中止」が効くこと
- 複製ブックの参照セルが静的な文字列になり、テンプレートは無改変であること
- 画面（/coupon・/coupon/preview）が最低限描画されること

いずれもブラウザ・実サイトへはアクセスしない。
"""

from __future__ import annotations

import asyncio
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
    CouponStatementsError,
    copy_coupon_workbook,
    raise_if_cancelled,
    read_cell_text,
    read_workbook_source_cells,
    replace_file,
    resolve_coupon_csv_path,
    resolve_period,
    set_static_cell_value,
    validate_coupon_csv_bytes,
)
from portal_app.services.paths import CouponPaths  # noqa: E402
from portal_app.services.progress_jobs import JobCancelled  # noqa: E402


VALID_CSV = (
    '"注文番号","ステータス","店舗発行クーポン利用額"\r\n'
    '"123456-20260701-0000001","100","500"\r\n'
).encode("cp932")

LOGIN_HTML = (
    "<!DOCTYPE html><html><head><title>認証</title></head>"
    "<body>認証が必要です</body></html>"
).encode("utf-8")

SHEET_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    "<worksheet><sheetData>"
    '<row r="2"><c r="A2" s="8" t="str">'
    '<f ca="1">SUBSTITUTE(OneDriveUrlToLocalPath(C2),"a","b")</f>'
    "<v>C:\\Users\\kamiz\\入金明細\\クーポン\\</v>"
    "</c>"
    '<c r="C2" s="9" t="str"><f ca="1">LEFT(CELL("filename"),1)</f><v>https://example/</v></c>'
    "</row>"
    '<row r="9"><c r="A9" s="1" t="str">'
    '<f ca="1">_xlfn.CONCAT(TEXT(EDATE(TODAY(),-1),"yymm"),C8)</f><v>2411_coupon.csv</v>'
    "</c></row>"
    "</sheetData></worksheet>"
)

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxml'
    'formats-officedocument.spreadsheetml.calcChain+xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.'
    'sheet.macroEnabled.main+xml"/>'
    "</Types>"
)

WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/calcChain" Target="calcChain.xml"/>'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _make_template(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("[Content_Types].xml", CONTENT_TYPES)
        book.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        book.writestr("xl/worksheets/sheet1.xml", SHEET_XML)
        book.writestr("xl/calcChain.xml", '<calcChain><c r="A2" i="1"/></calcChain>')
        book.writestr("xl/vbaProject.bin", b"\x00\x01binary")
        book.writestr("customXml/item2.xml", "<DataMashup/>")


class ValidateCsvTest(unittest.TestCase):
    def test_valid_coupon_csv_passes(self) -> None:
        self.assertIsNone(validate_coupon_csv_bytes(VALID_CSV))

    def test_html_error_page_is_rejected(self) -> None:
        reason = validate_coupon_csv_bytes(LOGIN_HTML)
        self.assertIsNotNone(reason)
        self.assertIn("HTML", reason or "")

    def test_empty_is_rejected(self) -> None:
        self.assertIsNotNone(validate_coupon_csv_bytes(b"   \r\n"))

    def test_undecodable_bytes_are_rejected(self) -> None:
        reason = validate_coupon_csv_bytes(b"\xff\xfe\xff\xfe\xff\xfe")
        self.assertIsNotNone(reason)

    def test_csv_without_expected_columns_is_rejected(self) -> None:
        payload = '"氏名","住所"\r\n"山田","那覇市"\r\n'.encode("cp932")
        reason = validate_coupon_csv_bytes(payload)
        self.assertIsNotNone(reason)
        self.assertIn("列名", reason or "")

    def test_truncated_multibyte_tail_does_not_break_decoding(self) -> None:
        # 64KB 読みでマルチバイト文字の途中で切れても、最後の改行までで判定する。
        payload = VALID_CSV + '"注文番'.encode("cp932")[:-1]
        self.assertIsNone(validate_coupon_csv_bytes(payload))


class ReplaceFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "src.csv"
        self.source.write_bytes(b"new")
        self.destination = self.root / "dst.csv"

    def test_moves_file(self) -> None:
        replace_file(self.source, self.destination, label="保存先のCSV")
        self.assertEqual(self.destination.read_bytes(), b"new")
        self.assertFalse(self.source.exists())

    def test_locked_destination_becomes_typed_error(self) -> None:
        with mock.patch.object(Path, "replace", side_effect=PermissionError("使用中")):
            with self.assertRaises(CouponStatementsError) as ctx:
                replace_file(self.source, self.destination, label="保存先のCSV")
        self.assertEqual(ctx.exception.state, "FILE_LOCKED")
        self.assertIn("Excel", str(ctx.exception))
        self.assertIn("閉じて", str(ctx.exception))

    def test_cross_device_error_falls_back_to_move(self) -> None:
        with mock.patch.object(Path, "replace", side_effect=OSError(18, "EXDEV")):
            replace_file(self.source, self.destination, label="保存先のCSV")
        self.assertEqual(self.destination.read_bytes(), b"new")

    def test_move_failure_is_typed_error(self) -> None:
        with mock.patch.object(Path, "replace", side_effect=OSError(18, "EXDEV")):
            with mock.patch(
                "portal_app.services.coupon_statements.shutil.move",
                side_effect=PermissionError("使用中"),
            ):
                with self.assertRaises(CouponStatementsError) as ctx:
                    replace_file(self.source, self.destination, label="保存先のCSV")
        self.assertEqual(ctx.exception.state, "FILE_LOCKED")


class CommitCsvTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.paths = CouponPaths(
            root=self.root / "クーポン",
            csv_dir=self.root / "クーポン" / "csv",
            template_path=self.root / "クーポン" / "tpl.xlsm",
        )
        self.paths.csv_dir.mkdir(parents=True)
        self.period = resolve_period(date(2026, 8, 3))
        self.destination = self.paths.csv_dir / self.period.csv_name
        # 隔離先をテスト用ディレクトリへ逃がす（実 data/ を汚さない）
        patcher = mock.patch.object(mod, "APP_ROOT", self.root / "app")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _staged(self, payload: bytes) -> Path:
        staged = self.root / "staged.csv"
        staged.write_bytes(payload)
        return staged

    def test_saves_valid_csv(self) -> None:
        path, warnings = mod._commit_csv(
            self._staged(VALID_CSV), paths=self.paths, period=self.period
        )
        self.assertEqual(path, self.destination)
        self.assertEqual(path.read_bytes(), VALID_CSV)
        self.assertEqual(warnings, [])

    def test_replacing_existing_csv_warns(self) -> None:
        self.destination.write_bytes(VALID_CSV)
        _, warnings = mod._commit_csv(
            self._staged(VALID_CSV), paths=self.paths, period=self.period
        )
        self.assertTrue(any("置き換え" in warning for warning in warnings))

    def test_invalid_download_keeps_existing_csv(self) -> None:
        self.destination.write_bytes(VALID_CSV)
        with self.assertRaises(CouponStatementsError) as ctx:
            mod._commit_csv(self._staged(LOGIN_HTML), paths=self.paths, period=self.period)
        self.assertEqual(ctx.exception.state, "INVALID_DOWNLOAD")
        # 既存の正常CSVが壊れていないこと（これがこの防御の目的）
        self.assertEqual(self.destination.read_bytes(), VALID_CSV)
        quarantine = self.root / "app" / "data" / "coupon_statements" / "quarantine"
        self.assertTrue(list(quarantine.glob("2607_coupon_*.bin")))

    def test_locked_destination_keeps_existing_csv(self) -> None:
        self.destination.write_bytes(VALID_CSV)
        with mock.patch.object(Path, "replace", side_effect=PermissionError("使用中")):
            with self.assertRaises(CouponStatementsError) as ctx:
                mod._commit_csv(self._staged(VALID_CSV), paths=self.paths, period=self.period)
        self.assertEqual(ctx.exception.state, "FILE_LOCKED")
        self.assertEqual(self.destination.read_bytes(), VALID_CSV)


class CancelTest(unittest.TestCase):
    def test_raise_if_cancelled(self) -> None:
        raise_if_cancelled(None)
        raise_if_cancelled(lambda: False)
        with self.assertRaises(JobCancelled):
            raise_if_cancelled(lambda: True)

    def test_wait_loop_stops_on_cancel(self) -> None:
        """長い待機（既定30分）の途中でも中止要求で抜けること。"""

        page = _FakePage(body_text="処理中です")
        deadline = 10**9  # 実質無限（中止で抜けられなければテストが終わらない）

        async def run() -> None:
            await mod._wait_for_download_form(
                page, deadline=deadline, cancel_check=lambda: True
            )

        with self.assertRaises(JobCancelled):
            asyncio.run(asyncio.wait_for(run(), timeout=10))

    def test_wait_loop_reports_zero_rows(self) -> None:
        page = _FakePage(body_text="この条件でのデータ件数は0件です")

        async def run() -> bool:
            return await mod._wait_for_download_form(
                page, deadline=10**9, cancel_check=lambda: False
            )

        self.assertFalse(asyncio.run(asyncio.wait_for(run(), timeout=10)))


class _FakeLocator:
    def __init__(self, text: str = "", count: int = 0) -> None:
        self._text = text
        self._count = count

    async def count(self) -> int:
        return self._count

    @property
    def first(self) -> "_FakeLocator":
        return self

    async def is_visible(self) -> bool:
        return self._count > 0

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    """_wait_for_download_form が触る範囲だけを持つ最小のページ代役。"""

    def __init__(self, body_text: str) -> None:
        self._body_text = body_text

    def locator(self, selector: str) -> _FakeLocator:
        if selector == "body":
            return _FakeLocator(text=self._body_text, count=1)
        return _FakeLocator()


class StaticCellTest(unittest.TestCase):
    def test_static_cell_drops_formula_and_keeps_style(self) -> None:
        updated = set_static_cell_value(SHEET_XML, "A2", "D:\\portal\\クーポン\\")
        self.assertEqual(read_cell_text(updated, "A2"), "D:\\portal\\クーポン\\")
        self.assertIn('<c r="A2" s="8" t="inlineStr">', updated)
        self.assertNotIn("OneDriveUrlToLocalPath", updated)
        self.assertNotIn("kamiz", updated)
        # 他セルの数式は残す
        self.assertIn('LEFT(CELL("filename"),1)', updated)

    def test_static_cell_escapes_special_characters(self) -> None:
        updated = set_static_cell_value(SHEET_XML, "A9", "a&b<c>.csv")
        self.assertIn("a&amp;b&lt;c&gt;.csv", updated)
        self.assertEqual(read_cell_text(updated, "A9"), "a&b<c>.csv")

    def test_missing_cell_raises(self) -> None:
        with self.assertRaises(CouponStatementsError) as ctx:
            set_static_cell_value(SHEET_XML, "Z99", "x")
        self.assertEqual(ctx.exception.state, "TEMPLATE_CONTRACT_CHANGED")

    def test_calc_chain_parts_are_removed(self) -> None:
        self.assertNotIn("calcChain", mod.remove_calc_chain_override(CONTENT_TYPES))
        self.assertIn("workbook.xml", mod.remove_calc_chain_override(CONTENT_TYPES))
        rels = mod.remove_calc_chain_relationship(WORKBOOK_RELS)
        self.assertNotIn("calcChain", rels)
        self.assertIn("worksheets/sheet1.xml", rels)


class CopyWorkbookStaticTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.template = self.root / "【テンプレート】○○月クーポン利用 .xlsm"
        _make_template(self.template)
        self.paths = CouponPaths(
            root=self.root, csv_dir=self.root / "csv", template_path=self.template
        )
        self.period = resolve_period(date(2026, 8, 3))

    def test_copy_has_static_cells_and_no_calc_chain(self) -> None:
        before = self.template.read_bytes()
        destination, created, _ = copy_coupon_workbook(self.paths, self.period)
        self.assertTrue(created)
        with zipfile.ZipFile(destination) as book:
            names = book.namelist()
            sheet_xml = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
            content_types = book.read("[Content_Types].xml").decode("utf-8")
            rels = book.read("xl/_rels/workbook.xml.rels").decode("utf-8")
            self.assertEqual(book.read("xl/vbaProject.bin"), b"\x00\x01binary")
        self.assertNotIn("xl/calcChain.xml", names)
        self.assertNotIn("calcChain", content_types)
        self.assertNotIn("calcChain", rels)
        # A2/A9 は数式ごと静的化（開いた月・マクロ有効可否で値が動かない）
        self.assertNotIn("OneDriveUrlToLocalPath", sheet_xml)
        self.assertNotIn("EDATE(TODAY()", sheet_xml)
        self.assertNotIn("kamiz", sheet_xml)
        values = read_workbook_source_cells(destination)
        self.assertEqual(values["filePath"], str(self.root) + "\\")
        self.assertEqual(values["fileName"], "2607_coupon.csv")
        # テンプレートは無改変
        self.assertEqual(self.template.read_bytes(), before)

    def test_locked_destination_is_reported_as_file_locked(self) -> None:
        with mock.patch.object(Path, "replace", side_effect=PermissionError("使用中")):
            with mock.patch(
                "portal_app.services.coupon_statements.shutil.move",
                side_effect=PermissionError("使用中"),
            ):
                with self.assertRaises(CouponStatementsError) as ctx:
                    copy_coupon_workbook(self.paths, self.period)
        self.assertEqual(ctx.exception.state, "FILE_LOCKED")
        # 一時ファイルを残さない
        leftovers = [path.name for path in self.root.iterdir() if path.name.startswith(".")]
        self.assertEqual(leftovers, [])


class ResolveCsvPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "csv").mkdir()

    def test_returns_saved_csv(self) -> None:
        (self.root / "csv" / "2607_coupon.csv").write_bytes(VALID_CSV)
        with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": str(self.root)}):
            path = resolve_coupon_csv_path(date(2026, 8, 3))
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "2607_coupon.csv")

    def test_returns_none_when_missing(self) -> None:
        with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": str(self.root)}):
            self.assertIsNone(resolve_coupon_csv_path(date(2026, 8, 3)))

    def test_returns_none_when_folder_unresolved(self) -> None:
        with mock.patch(
            "portal_app.services.coupon_statements.find_coupon_paths",
            side_effect=FileNotFoundError("no folder"),
        ):
            self.assertIsNone(resolve_coupon_csv_path(date(2026, 8, 3)))


class HistoryFallbackTest(unittest.TestCase):
    def test_row_markers_cover_common_date_formats(self) -> None:
        markers = mod._history_row_markers(resolve_period(date(2026, 8, 3)))
        self.assertIn("2026-07-01", markers)
        self.assertIn("2026/07/01", markers)
        self.assertIn("2026/7/1", markers)
        self.assertIn("2026年7月31日", markers)

    def test_history_helpers_exist(self) -> None:
        # 「ダウンロード利用履歴」からの回収経路（task 明記）が実装されていること
        self.assertTrue(callable(mod._open_download_history))
        self.assertTrue(callable(mod._find_history_row))
        self.assertTrue(callable(mod._download_from_history))
        self.assertEqual(mod._DOWNLOAD_HISTORY_LABEL, "ダウンロード利用履歴")


class LoginEnvScopeTest(unittest.TestCase):
    def test_env_is_restored_after_context(self) -> None:
        env = {
            "KURIMA_RAKUTEN_KANRI_LOGIN_ID": "",
            "KURIMA_RAKUTEN_KANRI_PASSWORD": "",
            "KURIMA_RAKUTEN_LOGIN_ID": "",
            "KURIMA_RAKUTEN_LOGIN_PASSWORD": "",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch(
                "portal_app.services.credentials.load_site_credential",
                return_value=("id", "pw"),
            ):
                with mod.rakuten_login_env() as filled:
                    self.assertEqual(os.environ["KURIMA_RAKUTEN_LOGIN_ID"], "id")
                    self.assertTrue(filled)
            # with を抜けたら他カードへ副作用を残さない
            self.assertEqual(os.environ["KURIMA_RAKUTEN_LOGIN_ID"], "")
            self.assertEqual(os.environ["KURIMA_RAKUTEN_KANRI_PASSWORD"], "")


try:  # TestClient は httpx が要る。未導入の環境ではこのクラスごとスキップする。
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient

    _HAS_TEST_CLIENT = True
except Exception:  # pragma: no cover - 環境依存
    _HAS_TEST_CLIENT = False


@unittest.skipUnless(_HAS_TEST_CLIENT, "httpx / fastapi.testclient が未導入のためスキップ")
class CouponRoutesTest(unittest.TestCase):
    """ルート登録と最低限の描画（カードが画面から使えること）を担保する。"""

    @classmethod
    def setUpClass(cls) -> None:
        from portal_app.main import app

        cls.client = TestClient(app)
        cls.app = app

    def test_routes_are_registered(self) -> None:
        paths = {route.path for route in self.app.routes}
        for path in ("/coupon", "/coupon/preview", "/coupon/start", "/coupon/download/csv"):
            self.assertIn(path, paths)

    def test_coupon_page_renders(self) -> None:
        response = self.client.get("/coupon")
        self.assertEqual(response.status_code, 200)
        self.assertIn("クーポン明細取得", response.text)
        self.assertIn("/coupon/start", response.text)

    def test_preview_renders_without_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope")
            with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": missing}):
                with mock.patch(
                    "portal_app.services.paths.candidate_portal_roots",
                    return_value=[Path(missing)],
                ):
                    response = self.client.get("/coupon/preview")
        self.assertEqual(response.status_code, 200)

    def test_preview_shows_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coupon_dir = Path(tmp) / "クーポン"
            (coupon_dir / "csv").mkdir(parents=True)
            period = resolve_period()
            (coupon_dir / "csv" / period.csv_name).write_bytes(VALID_CSV)
            with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": str(coupon_dir)}):
                response = self.client.get("/coupon/preview")
        self.assertEqual(response.status_code, 200)
        self.assertIn(period.csv_name, response.text)

    def test_download_csv_returns_404_without_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coupon_dir = Path(tmp) / "クーポン"
            (coupon_dir / "csv").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"KURIMA_COUPON_DIR": str(coupon_dir)}):
                response = self.client.get("/coupon/download/csv")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
