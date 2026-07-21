"""出荷確定 — NE反映候補CSVの高速選定テスト（2026-07-22 フリーズ修正）。

認識合わせ対応（ユーザー報告「アップロード前チェックでフリーズ」）:
  - 旧実装は OneDrive 同期の完成データ内 yamato_to-ne*.csv 1,000本超を毎回**全行読み**
    しており、オンデマンド回収で分単位のフリーズになっていた
  - 新実装は mtime降順に並べ、**ヘッダー行のみ**読み、最初に一致したファイルで打ち切る
  - 選定結果は従来と同一（「3列ヘッダーを持つ最新の有効ファイル」）であることを固定する
  - 誤アップロード防止の仕様は維持: 対象prefix以外（ne-to-yamato* 等）は候補にしない・
    有効ファイルが無ければ None（最新CSVへのフォールバック禁止）
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.shipment_confirmation import (
    _latest_completion_csv_in,
    _read_csv_header_only,
)

VALID_HEADER = "伝票番号,発送伝票番号,出荷予定日"


def _write(directory: Path, name: str, text: str, *, encoding: str = "cp932", mtime: float | None = None) -> Path:
    path = directory / name
    path.write_text(text, encoding=encoding, newline="")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class LatestCompletionCsvTest(unittest.TestCase):
    def test_picks_newest_valid_file(self):
        """mtime降順で最初の有効ファイル＝最新の有効ファイルを選ぶ（従来と同一の選定）。"""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "yamato_to-ne_old.csv", f"{VALID_HEADER}\r\n1,2,3\r\n", mtime=now - 300)
            newest = _write(directory, "yamato_to-ne_new.csv", f"{VALID_HEADER}\r\n4,5,6\r\n", mtime=now - 10)
            # 最新だがヘッダー不一致のファイルはスキップして次に新しい有効ファイルを選ぶ
            _write(directory, "yamato_to-ne_broken.csv", "A,B\r\n1,2\r\n", mtime=now)
            warnings: list[str] = []
            self.assertEqual(_latest_completion_csv_in(directory, warnings), newest)
            self.assertEqual(warnings, [])

    def test_other_prefixes_are_never_candidates(self):
        """ne-to-yamato*・clickpostimport 等の別用途CSVは絶対に候補にしない（誤アップロード防止）。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "ne-to-yamato9999.csv", f"{VALID_HEADER}\r\n1,2,3\r\n")
            _write(directory, "clickpostimport.csv", f"{VALID_HEADER}\r\n1,2,3\r\n")
            warnings: list[str] = []
            self.assertIsNone(_latest_completion_csv_in(directory, warnings))
            self.assertTrue(any("見つかりません" in w for w in warnings))

    def test_no_fallback_when_headers_invalid(self):
        """有効ヘッダーのファイルが無ければ None（最新CSVへのフォールバック禁止）。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, "yamato_to-ne_bad.csv", "X,Y,Z\r\n1,2,3\r\n")
            warnings: list[str] = []
            self.assertIsNone(_latest_completion_csv_in(directory, warnings))
            self.assertTrue(any("3列ヘッダー" in w for w in warnings))

    def test_header_only_read_does_not_parse_body(self):
        """本文が巨大でもヘッダー1行の判定で選定できる（全行読み廃止の確認）。"""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            big_body = "\r\n".join(f"{i},9{i},2026/07/22" for i in range(50000))
            big = _write(
                directory, "yamato_to-ne_big.csv", f"{VALID_HEADER}\r\n{big_body}\r\n", mtime=now
            )
            warnings: list[str] = []
            started = time.perf_counter()
            self.assertEqual(_latest_completion_csv_in(directory, warnings), big)
            elapsed = time.perf_counter() - started
            # 全行パースなら数百ms〜のところ、ヘッダーのみなら余裕で収まる緩い上限
            self.assertLess(elapsed, 0.5)


class ReadCsvHeaderOnlyTest(unittest.TestCase):
    def test_encodings_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cp932 = _write(directory, "a.csv", f"{VALID_HEADER}\r\n1,2,3\r\n", encoding="cp932")
            utf8sig = _write(directory, "b.csv", f"{VALID_HEADER}\r\n1,2,3\r\n", encoding="utf-8-sig")
            empty = _write(directory, "c.csv", "", encoding="cp932")
            expected = ("伝票番号", "発送伝票番号", "出荷予定日")
            self.assertEqual(_read_csv_header_only(cp932), expected)
            self.assertEqual(_read_csv_header_only(utf8sig), expected)
            self.assertEqual(_read_csv_header_only(empty), tuple())
            self.assertEqual(_read_csv_header_only(directory / "missing.csv"), tuple())


if __name__ == "__main__":
    unittest.main()
