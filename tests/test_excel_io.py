"""OneDrive上Excelのスナップショット読み（excel_io）のテスト。

2026-07-29 要望: 商品管理シート.xlsm は OneDrive 上にあるため、
ポータルが元ファイルを開いたまま解析してロック衝突しないようにする。
読み込みは一時コピー経由に統一し、コピー失敗（同期・Excel保存の瞬間的
ロック）はリトライで抜ける。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import excel_io
from portal_app.services.excel_io import excel_read_snapshot


class ExcelReadSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "商品管理シート.xlsm"
        self.source.write_bytes(b"dummy-excel-bytes")

    def test_snapshot_copies_and_cleans_up(self):
        """コピーが作られて中身が一致し、終了後に削除される。"""
        with excel_read_snapshot(self.source) as snapshot:
            self.assertNotEqual(snapshot, self.source)
            self.assertEqual(snapshot.read_bytes(), b"dummy-excel-bytes")
            self.assertTrue(snapshot.name.endswith("商品管理シート.xlsm"))
        self.assertFalse(snapshot.exists())

    def test_retries_on_transient_permission_error(self):
        """瞬間的なロック（PermissionError）はリトライで抜ける。"""
        real_copy = excel_io.shutil.copyfile
        calls = {"n": 0}

        def flaky_copy(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("locked by OneDrive/Excel")
            return real_copy(src, dst)

        with patch.object(excel_io.shutil, "copyfile", side_effect=flaky_copy), \
             patch.object(excel_io.time, "sleep") as sleep_mock:
            with excel_read_snapshot(self.source) as snapshot:
                self.assertTrue(snapshot.exists())
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_gives_japanese_hint_after_all_retries_fail(self):
        """リトライ全滅時は日本語の案内付き PermissionError になる。"""
        with patch.object(excel_io.shutil, "copyfile", side_effect=PermissionError("still locked")), \
             patch.object(excel_io.time, "sleep"):
            with self.assertRaises(PermissionError) as ctx:
                with excel_read_snapshot(self.source):
                    pass
        self.assertIn("少し待ってから再実行", str(ctx.exception))

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            with excel_read_snapshot(Path(self._tmp.name) / "無い.xlsx"):
                pass

    def test_snapshot_removed_even_on_error_inside_block(self):
        """読み込み処理が例外を投げてもコピーは削除される。"""
        captured = {}
        with self.assertRaises(RuntimeError):
            with excel_read_snapshot(self.source) as snapshot:
                captured["path"] = snapshot
                raise RuntimeError("parse error")
        self.assertFalse(captured["path"].exists())


if __name__ == "__main__":
    unittest.main()
