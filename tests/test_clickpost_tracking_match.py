"""クリックポスト送り状番号マッチング — 最新申込日時優先のテスト。

認識合わせ（2026-07-27 ユーザー要望）:
  - 動作確認の再実行などでマイページに同一宛先の申込が複数あるとき、
    「最新の申込日時」の行を正として送り状番号を採用する。
  - 申込日時が同じ（同一バッチ）またはパース不能な場合は、
    従来どおりマイページの表示順（若いindex）を採用する。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.clickpost import (
    _find_tracking_match,
    _match_imported_clickpost_tracking,
)


def _mypage_row(name: str, content: str, applied_at: str, tracking: str) -> dict[str, str]:
    return {
        "申込日時": applied_at,
        "お問い合わせ番号": tracking,
        "お届け先氏名": name,
        "内容品": content,
    }


class FindTrackingMatchTest(unittest.TestCase):
    def test_picks_newest_application_datetime(self):
        """同一宛先の申込が複数あるとき、最新の申込日時を採用する。"""
        rows = [
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:00", "OLD111111111"),
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:05", "NEW222222222"),
        ]
        index = _find_tracking_match(
            rows,
            {0, 1},
            target_name="山田 太郎",
            target_content="雑貨",
            require_content=True,
        )
        self.assertEqual(index, 1)

    def test_tie_keeps_table_order(self):
        """申込日時が同じ（同一バッチ）なら表示順の若い行を採用する。"""
        rows = [
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:00", "FIRST"),
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:00", "SECOND"),
        ]
        index = _find_tracking_match(
            rows,
            {0, 1},
            target_name="山田 太郎",
            target_content="雑貨",
            require_content=True,
        )
        self.assertEqual(index, 0)

    def test_parsable_datetime_beats_unparsable(self):
        """申込日時をパースできる行を、できない行より優先する。"""
        rows = [
            _mypage_row("山田 太郎", "雑貨", "不明", "BROKEN"),
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:00", "PARSED"),
        ]
        index = _find_tracking_match(
            rows,
            {0, 1},
            target_name="山田 太郎",
            target_content="雑貨",
            require_content=True,
        )
        self.assertEqual(index, 1)

    def test_all_unparsable_keeps_table_order(self):
        """全行パース不能なら従来どおり表示順の若い行を採用する。"""
        rows = [
            _mypage_row("山田 太郎", "雑貨", "", "FIRST"),
            _mypage_row("山田 太郎", "雑貨", "不明", "SECOND"),
        ]
        index = _find_tracking_match(
            rows,
            {0, 1},
            target_name="山田 太郎",
            target_content="雑貨",
            require_content=True,
        )
        self.assertEqual(index, 0)

    def test_no_candidate_returns_none(self):
        rows = [_mypage_row("別人", "雑貨", "2026/07/27 10:00", "X")]
        index = _find_tracking_match(
            rows,
            {0},
            target_name="山田 太郎",
            target_content="雑貨",
            require_content=True,
        )
        self.assertIsNone(index)


class MatchImportedTrackingTest(unittest.TestCase):
    def test_duplicate_applications_prefer_newest(self):
        """再実行で同一宛先が2回申込済みでも、最新バッチ内の最新行を正とする。"""
        import_rows = [{"お届け先氏名": "山田 太郎", "内容品": "雑貨"}]
        mypage_rows = [
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:00", "OLD111111111"),
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:05", "NEW222222222"),
        ]
        matched, warnings = _match_imported_clickpost_tracking(import_rows, mypage_rows)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["お問い合わせ番号"], "NEW222222222")
        self.assertEqual(warnings, [])

    def test_two_imports_same_name_consume_newest_first(self):
        """同一宛先2件の取込では、最新行から順に消費して重複採用しない。"""
        import_rows = [
            {"お届け先氏名": "山田 太郎", "内容品": "雑貨"},
            {"お届け先氏名": "山田 太郎", "内容品": "雑貨"},
        ]
        mypage_rows = [
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:00", "OLD111111111"),
            _mypage_row("山田 太郎", "雑貨", "2026/07/27 10:05", "NEW222222222"),
        ]
        matched, _warnings = _match_imported_clickpost_tracking(import_rows, mypage_rows)
        numbers = [row["お問い合わせ番号"] for row in matched]
        self.assertEqual(numbers, ["NEW222222222", "OLD111111111"])


if __name__ == "__main__":
    unittest.main()
