"""NE反映の「古い固定CSVパス」ガードのテスト（2026-08-03/04 実障害の再発防止）。

障害: アップロード前チェック → CSV作成 → 反映 の順で操作すると、チェック時に
フォームへ固定された前回CSVのパスで反映が走り、1世代前のCSVをアップロードする。
ガード: 実反映時、明示指定CSVより新しい有効候補があれば stale_csv_selected で停止する。
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from portal_app.services import shipment_confirmation as sc

_HEADERS = ",".join(sc.SHIPMENT_UPLOAD_HEADERS)


def _write_upload_csv(path: Path, rows: int, *, mtime: float | None = None) -> Path:
    today = date.today().strftime("%Y/%m/%d")
    lines = [_HEADERS]
    for index in range(rows):
        lines.append(f"{70000 + index},{400000000000 + index},{today}")
    path.write_text("\n".join(lines) + "\n", encoding="cp932")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class NewerCandidateThanTest(unittest.TestCase):
    def test_returns_none_when_no_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            source = _write_upload_csv(Path(tmp) / "yamato_to-ne_old.csv", 1)
            with mock.patch.object(sc, "_latest_completion_csv", return_value=None):
                self.assertIsNone(sc._newer_candidate_than(source))

    def test_returns_none_when_source_is_latest(self) -> None:
        with TemporaryDirectory() as tmp:
            source = _write_upload_csv(Path(tmp) / "yamato_to-ne_old.csv", 1)
            with mock.patch.object(sc, "_latest_completion_csv", return_value=source):
                self.assertIsNone(sc._newer_candidate_than(source))

    def test_returns_newer_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            source = _write_upload_csv(Path(tmp) / "yamato_to-ne_old.csv", 1, mtime=1_000_000)
            newer = _write_upload_csv(Path(tmp) / "yamato_to-ne_new.csv", 2, mtime=2_000_000)
            with mock.patch.object(sc, "_latest_completion_csv", return_value=newer):
                self.assertEqual(sc._newer_candidate_than(source), newer)

    def test_returns_none_when_latest_is_older(self) -> None:
        # 指定CSVの方が新しい（例: チェック直後に反映）なら従来どおり通す。
        with TemporaryDirectory() as tmp:
            older = _write_upload_csv(Path(tmp) / "yamato_to-ne_a.csv", 1, mtime=1_000_000)
            source = _write_upload_csv(Path(tmp) / "yamato_to-ne_b.csv", 1, mtime=2_000_000)
            with mock.patch.object(sc, "_latest_completion_csv", return_value=older):
                self.assertIsNone(sc._newer_candidate_than(source))

    def test_returns_none_on_missing_source(self) -> None:
        with TemporaryDirectory() as tmp:
            newer = _write_upload_csv(Path(tmp) / "yamato_to-ne_new.csv", 1)
            missing = Path(tmp) / "yamato_to-ne_gone.csv"
            with mock.patch.object(sc, "_latest_completion_csv", return_value=newer):
                self.assertIsNone(sc._newer_candidate_than(missing))


class StaleCsvExecuteGuardTest(unittest.TestCase):
    def test_execute_blocks_when_newer_candidate_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_csv = _write_upload_csv(tmp_path / "yamato_to-ne_old.csv", 3, mtime=1_000_000)
            new_csv = _write_upload_csv(tmp_path / "yamato_to-ne_new.csv", 5, mtime=2_000_000)
            audit_path = tmp_path / "audit.jsonl"
            with (
                mock.patch.object(sc, "_latest_completion_csv", return_value=new_csv),
                mock.patch.object(sc, "AUDIT_LOG_PATH", audit_path),
            ):
                result = sc.upload_next_engine_shipment_csv_sync(
                    execute=True,
                    confirm_upload=True,
                    upload_csv=old_csv,
                )
            self.assertTrue(audit_path.is_file(), "監査ログに stale 停止が記録されること")
        self.assertFalse(result.executed)
        self.assertEqual(result.skipped_reason, "stale_csv_selected")
        self.assertTrue(any("新しい候補" in warning for warning in result.warnings))

    def test_execute_not_blocked_without_explicit_csv(self) -> None:
        # 明示指定なし（サーバー側で最新を解決）の経路はガード対象外。
        # ready_to_upload=False で通常の upload_csv_not_ready に落ちることを確認する
        # （ブラウザ起動前に返る安全な経路で検証）。
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            audit_path = tmp_path / "audit.jsonl"

            def _no_candidate(warnings: list[str]) -> None:
                warnings.append("候補なし（テスト）")
                return None

            with (
                mock.patch.object(sc, "_latest_completion_csv", _no_candidate),
                mock.patch.object(sc, "AUDIT_LOG_PATH", audit_path),
            ):
                result = sc.upload_next_engine_shipment_csv_sync(
                    execute=True,
                    confirm_upload=True,
                )
        self.assertEqual(result.skipped_reason, "upload_csv_not_ready")


if __name__ == "__main__":
    unittest.main()
