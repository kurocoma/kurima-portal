"""Shopifyカレンダーの純ロジック単体テスト（Shopify への実通信はしない）。

対象:
- 臨時休業日の読み取り（範囲・個別・全角・不正値・月外の日）と表記の戻し
- 今月/来月の算出（12月→翌年1月）
- 画像の大きさと休業日の塗り
- テーマ設定（settings_data.json）からの参照箇所・幅の判定
- ファイル差し替え後の確認（別名登録の検出）と、権限不足時に何も送らないこと
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from portal_app.services import shopify_admin, shopify_calendar
from portal_app.services.calendar_image import (
    PINK,
    CalendarMonth,
    ClosedDaysError,
    current_and_next,
    format_closed_days,
    parse_closed_days,
    render_calendar,
)

DEC = CalendarMonth(2026, 12)


class ClosedDaysTest(unittest.TestCase):
    def test_range_and_single(self):
        self.assertEqual(parse_closed_days("13-16,20", DEC), (13, 14, 15, 16, 20))

    def test_fullwidth_and_japanese_separators(self):
        self.assertEqual(parse_closed_days("２９〜３１、２", DEC), (2, 29, 30, 31))

    def test_blank_is_no_closed_days(self):
        self.assertEqual(parse_closed_days("  ", DEC), ())

    def test_out_of_month_is_rejected(self):
        with self.assertRaisesRegex(ClosedDaysError, "30日まで"):
            parse_closed_days("31", CalendarMonth(2026, 11))

    def test_unreadable_is_rejected(self):
        with self.assertRaisesRegex(ClosedDaysError, "読み取れません"):
            parse_closed_days("年末", DEC)

    def test_reversed_range_is_rejected(self):
        with self.assertRaisesRegex(ClosedDaysError, "開始日"):
            parse_closed_days("31-29", DEC)

    def test_format_round_trip(self):
        days = (13, 14, 15, 20, 29, 30, 31)
        self.assertEqual(format_closed_days(days), "13-15,20,29-31")
        self.assertEqual(parse_closed_days(format_closed_days(days), DEC), days)


class MonthsTest(unittest.TestCase):
    def test_december_rolls_over_to_next_year(self):
        current, upcoming = current_and_next(date(2026, 12, 15))
        self.assertEqual((current.key, upcoming.key), ("2026_12", "2027_01"))

    def test_plan_maps_fixed_filenames(self):
        plan = shopify_calendar.build_plan(date(2026, 10, 4), {"2026_11": (3,)})
        self.assertEqual(
            [(t.key, days, name) for t, days, name in plan.items],
            [("2026_10", (), "calendar_current.jpg"), ("2026_11", (3,), "calendar_next.jpg")],
        )


class RenderTest(unittest.TestCase):
    def test_size_and_closed_day_is_pink(self):
        image = Image.open(io.BytesIO(render_calendar(DEC, (29,)))).convert("RGB")
        self.assertEqual(image.size, (500, 500))
        # 2026/12/29（火）は5週目・3列目。セル右下寄り（文字の無い位置）がピンク。
        plain = Image.open(io.BytesIO(render_calendar(DEC))).convert("RGB")
        x, y = 15 + (470 / 7) * 2 + 55, 97 + ((490 - 97) / 5) * 4 + 60
        pixel = image.getpixel((int(x), int(y)))
        self.assertTrue(all(abs(a - b) <= 6 for a, b in zip(pixel, PINK)), pixel)
        self.assertEqual(plain.getpixel((int(x), int(y)))[:3], (255, 255, 255))


def _settings(sidebar_images, home_blocks, *, comment=True):
    data = {
        "current": {
            "sections": {
                "sidebar": {
                    "type": "sidebar",
                    "blocks": {
                        f"b{i}": {"type": "side_block", "settings": {"type": "calender", "image": img}}
                        for i, img in enumerate(sidebar_images)
                    },
                },
                "home": {
                    "type": "custom-content",
                    "settings": {"title": "営業日カレンダー"},
                    "blocks": home_blocks,
                },
            }
        }
    }
    text = json.dumps(data, ensure_ascii=False)
    return ("/*\n * IMPORTANT: auto-generated\n */\n" + text) if comment else text


class ThemeAnalysisTest(unittest.TestCase):
    def test_before_setup_reports_month_files_and_width(self):
        content = _settings(
            ["shopify://shop_images/2026_08.jpg", "shopify://shop_images/2026_09.jpg"],
            {
                "old": {"type": "image", "disabled": True, "settings": {"image": "shopify://shop_images/2026_04.jpg", "width": "100%"}},
                "a": {"type": "image", "settings": {"image": "shopify://shop_images/2026_08.jpg", "width": "100%"}},
                "b": {"type": "image", "settings": {"image": "shopify://shop_images/2026_09.jpg", "width": "50%"}},
            },
        )
        check = shopify_calendar.analyze_theme_settings(content)
        self.assertFalse(check.setup_done)
        self.assertEqual(len(check.month_refs), 4)  # 無効化ブロック（2026_04）は数えない
        self.assertEqual(check.width_mismatch, ["営業日カレンダー: 100% / 50%"])

    def test_after_setup(self):
        content = _settings(
            ["shopify://shop_images/calendar_current.jpg", "shopify://shop_images/calendar_next.jpg"],
            {
                "a": {"type": "image", "settings": {"image": "shopify://shop_images/calendar_current.jpg", "width": "100%"}},
                "b": {"type": "image", "settings": {"image": "shopify://shop_images/calendar_next.jpg", "width": "100%"}},
            },
            comment=False,
        )
        check = shopify_calendar.analyze_theme_settings(content)
        self.assertTrue(check.setup_done)
        self.assertEqual((len(check.current_refs), len(check.next_refs)), (2, 2))
        self.assertEqual(check.width_mismatch, [])


CFG = shopify_admin.ShopifyConfig(shop="kurima-okinawa", client_id="id", client_secret="secret")


class WaitUntilReadyTest(unittest.TestCase):
    def _node(self, status, url):
        return {"node": {"id": "gid://shopify/MediaImage/1", "fileStatus": status, "updatedAt": "2026-10-04T00:00:00Z", "image": {"url": url}}}

    def test_ready_with_same_name(self):
        responses = [
            self._node("UPLOADED", None),
            self._node("READY", "https://cdn.shopify.com/s/files/1/x/files/calendar_current.jpg?v=2"),
        ]
        with mock.patch.object(shopify_admin, "graphql", side_effect=responses):
            found = shopify_admin.wait_until_ready(
                CFG, "gid://shopify/MediaImage/1", "calendar_current.jpg", sleep=lambda _: None
            )
        self.assertEqual(found.status, "READY")

    def test_renamed_upload_is_an_error(self):
        renamed = self._node("READY", "https://cdn.shopify.com/s/files/1/x/files/calendar_current_3f2a.jpg?v=1")
        with mock.patch.object(shopify_admin, "graphql", return_value=renamed), self.assertRaisesRegex(
            shopify_admin.ShopifyError, "別名"
        ):
            shopify_admin.wait_until_ready(
                CFG, "gid://shopify/MediaImage/1", "calendar_current.jpg", sleep=lambda _: None
            )

    def test_failed_processing_is_an_error(self):
        failed = self._node("FAILED", None)
        failed["node"]["fileErrors"] = [{"message": "画像が壊れています"}]
        with mock.patch.object(shopify_admin, "graphql", return_value=failed), self.assertRaisesRegex(
            shopify_admin.ShopifyError, "画像が壊れています"
        ):
            shopify_admin.wait_until_ready(
                CFG, "gid://shopify/MediaImage/1", "calendar_current.jpg", sleep=lambda _: None
            )


class PublishGuardTest(unittest.TestCase):
    def test_missing_write_files_stops_before_upload(self):
        plan = shopify_calendar.build_plan(date(2026, 10, 4), {})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            shopify_admin, "shopify_config", return_value=CFG
        ), mock.patch.object(
            shopify_admin, "access_token", return_value=("token", ("read_products", "write_products"))
        ), mock.patch.object(shopify_admin, "replace_image_file") as upload:
            with self.assertRaisesRegex(shopify_admin.ShopifyAccessDenied, "write_files"):
                shopify_calendar.publish(plan, folder=Path(tmp))
            upload.assert_not_called()

    def test_missing_config_is_reported(self):
        plan = shopify_calendar.build_plan(date(2026, 10, 4), {})
        with mock.patch.object(
            shopify_admin, "shopify_config", return_value=None
        ), self.assertRaisesRegex(shopify_admin.ShopifyError, "SHOPIFY_CLIENT_ID"):
            shopify_calendar.publish(plan)


class ClosedDaysStorageTest(unittest.TestCase):
    def test_save_keeps_other_months(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            shopify_calendar.save_closed_days({"2026_08": (13, 14, 15, 16)}, folder)
            shopify_calendar.save_closed_days({"2026_12": (29, 30, 31)}, folder)
            self.assertEqual(
                shopify_calendar.load_closed_days(folder),
                {"2026_08": "13-16", "2026_12": "29-31"},
            )


if __name__ == "__main__":
    unittest.main()
