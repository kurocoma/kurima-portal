"""出荷確定 — 当日CSVガード・反映済みCSV再送ガードのテスト（2026-08-18 実障害対策）。

実障害: 当日の「出荷確定CSV作成」前に「反映」を実行し、自動選択された最新有効候補
＝前日分CSV（154件）がそのままNEへ再送された。既存の stale_csv_selected ガードは
「チェック後に新しいCSVができた」ケース専用のため素通りしていた。

固定する仕様:
  - 本日作成でないCSVは実反映を csv_not_created_today で停止する
  - 反映成功履歴（sha256）と同一内容のCSVは duplicate_upload で停止する
  - stat 失敗・履歴なしはブロックしない（従来動作のまま）
  - 反映成功時に履歴へ追記される（_record_uploaded_csv）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import shipment_confirmation as sc

VALID_HEADER = '"伝票番号","発送伝票番号","出荷予定日"'


def _write_valid_csv(path: Path, *, rows: int = 2, shipping_date: str | None = None) -> None:
    shipping_date = shipping_date or datetime.now().strftime("%Y/%m/%d")
    lines = [VALID_HEADER]
    for index in range(rows):
        lines.append(f'"7{index:04d}","62870000000{index}","{shipping_date}"')
    path.write_text("\n".join(lines) + "\n", encoding="cp932")


def _set_mtime(path: Path, when: datetime) -> None:
    timestamp = time.mktime(when.timetuple())
    os.utime(path, (timestamp, timestamp))


class CsvCreatedTodayTest(unittest.TestCase):
    def test_today_file_is_true(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "yamato_to-ne2608200000.csv"
            _write_valid_csv(path)
            self.assertIs(sc._csv_created_today(path), True)

    def test_yesterday_file_is_false(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "yamato_to-ne2608190000.csv"
            _write_valid_csv(path)
            _set_mtime(path, datetime.now() - timedelta(days=1))
            self.assertIs(sc._csv_created_today(path), False)

    def test_missing_file_is_none(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.csv"
            self.assertIsNone(sc._csv_created_today(path))


class UploadedCsvHistoryTest(unittest.TestCase):
    def test_record_and_lookup_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            history = Path(tmp) / "uploaded_csv_history.jsonl"
            csv_path = Path(tmp) / "yamato_to-ne2608200000.csv"
            _write_valid_csv(csv_path)
            with mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", history):
                sha256 = sc._sha256_of_file(csv_path)
                self.assertIsNone(sc._uploaded_csv_history_entry(sha256))
                sc._record_uploaded_csv(csv_path, rows=2)
                entry = sc._uploaded_csv_history_entry(sha256)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["name"], csv_path.name)
            self.assertEqual(entry["rows"], 2)

    def test_broken_history_lines_are_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            history = Path(tmp) / "uploaded_csv_history.jsonl"
            history.write_text('not-json\n{"sha256": "abc", "name": "x.csv"}\n', encoding="utf-8")
            with mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", history):
                entry = sc._uploaded_csv_history_entry("abc")
                self.assertIsNotNone(entry)
                self.assertIsNone(sc._uploaded_csv_history_entry("missing"))


class UploadBlockReasonTest(unittest.TestCase):
    def test_old_csv_is_blocked(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "yamato_to-ne2608190000.csv"
            _write_valid_csv(path)
            _set_mtime(path, datetime.now() - timedelta(days=1))
            with mock.patch.object(
                sc, "UPLOADED_CSV_HISTORY_PATH", Path(tmp) / "history.jsonl"
            ):
                blocked = sc._upload_block_reason(path)
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked[0], "csv_not_created_today")
            self.assertIn("本日作成ではありません", blocked[1])

    def test_already_uploaded_csv_is_blocked(self) -> None:
        with TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            path = Path(tmp) / "yamato_to-ne2608200000.csv"
            _write_valid_csv(path)
            with mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", history):
                sc._record_uploaded_csv(path, rows=2)
                blocked = sc._upload_block_reason(path)
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked[0], "duplicate_upload")
            self.assertIn("反映済み", blocked[1])

    def test_fresh_csv_is_not_blocked(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "yamato_to-ne2608200000.csv"
            _write_valid_csv(path)
            with mock.patch.object(
                sc, "UPLOADED_CSV_HISTORY_PATH", Path(tmp) / "history.jsonl"
            ):
                self.assertIsNone(sc._upload_block_reason(path))


class UploadGuardIntegrationTest(unittest.TestCase):
    """upload_next_engine_shipment_csv がブラウザ起動前にガードで停止することを固定する。"""

    def _run_upload(self, csv_path: Path, **kwargs):
        return asyncio.run(
            sc.upload_next_engine_shipment_csv(
                execute=True,
                confirm_upload=True,
                upload_csv=csv_path,
                **kwargs,
            )
        )

    def test_yesterday_csv_is_rejected_before_upload(self) -> None:
        with TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            csv_path = Path(tmp) / "yamato_to-ne2608190000.csv"
            _write_valid_csv(csv_path, shipping_date="2026/08/19")
            _set_mtime(csv_path, datetime.now() - timedelta(days=1))
            with (
                mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", audit_dir / "history.jsonl"),
                mock.patch.object(sc, "AUDIT_LOG_DIR", audit_dir),
                mock.patch.object(sc, "AUDIT_LOG_PATH", audit_dir / "audit.jsonl"),
                # 前段の stale 判定（実フォルダの最新CSV参照）を無効化して当日ガードを分離検証
                mock.patch.object(sc, "_newer_candidate_than", return_value=None),
            ):
                result = self._run_upload(csv_path)
        self.assertFalse(result.executed)
        self.assertEqual(result.skipped_reason, "csv_not_created_today")
        self.assertTrue(any("本日作成ではありません" in warning for warning in result.warnings))

    def test_duplicate_csv_is_rejected_before_upload(self) -> None:
        with TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            csv_path = Path(tmp) / "yamato_to-ne2608200000.csv"
            _write_valid_csv(csv_path)
            with (
                mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", audit_dir / "history.jsonl"),
                mock.patch.object(sc, "AUDIT_LOG_DIR", audit_dir),
                mock.patch.object(sc, "AUDIT_LOG_PATH", audit_dir / "audit.jsonl"),
                # 前段の stale 判定（実フォルダの最新CSV参照）を無効化して重複ガードを分離検証
                mock.patch.object(sc, "_newer_candidate_than", return_value=None),
            ):
                sc._record_uploaded_csv(csv_path, rows=2)
                result = self._run_upload(csv_path)
        self.assertFalse(result.executed)
        self.assertEqual(result.skipped_reason, "duplicate_upload")

    def test_allow_old_csv_skips_day_guard(self) -> None:
        """allow_old_csv 指定時は前日CSVでも当日/重複ガードを通過して実反映へ進む。

        ブラウザ起動を避けるため、実反映の最初の処理（find_portal_paths）を
        目印例外に差し替え、そこへ到達した＝ガードを通過したことを確認する。
        """

        class _GuardsPassed(Exception):
            pass

        with TemporaryDirectory() as tmp:
            audit_dir = Path(tmp) / "audit"
            csv_path = Path(tmp) / "yamato_to-ne2608190000.csv"
            _write_valid_csv(csv_path, shipping_date="2026/08/19")
            _set_mtime(csv_path, datetime.now() - timedelta(days=1))
            with (
                mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", audit_dir / "history.jsonl"),
                mock.patch.object(sc, "AUDIT_LOG_DIR", audit_dir),
                mock.patch.object(sc, "AUDIT_LOG_PATH", audit_dir / "audit.jsonl"),
                mock.patch.object(sc, "_newer_candidate_than", return_value=None),
                mock.patch.object(sc, "find_portal_paths", side_effect=_GuardsPassed),
            ):
                with self.assertRaises(_GuardsPassed):
                    self._run_upload(csv_path, allow_old_csv=True)


class DescribeFreshnessTest(unittest.TestCase):
    def test_none_path_returns_unknown(self) -> None:
        info = sc.describe_upload_csv_freshness(None)
        self.assertIsNone(info["created_today"])
        self.assertIsNone(info["already_uploaded_at"])

    def test_uploaded_file_reports_timestamp(self) -> None:
        with TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.jsonl"
            csv_path = Path(tmp) / "yamato_to-ne2608200000.csv"
            _write_valid_csv(csv_path)
            with mock.patch.object(sc, "UPLOADED_CSV_HISTORY_PATH", history):
                sc._record_uploaded_csv(csv_path, rows=2)
                info = sc.describe_upload_csv_freshness(csv_path)
            self.assertIs(info["created_today"], True)
            self.assertIsNotNone(info["already_uploaded_at"])


if __name__ == "__main__":
    unittest.main()
