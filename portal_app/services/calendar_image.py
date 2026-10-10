"""Shopify 営業日カレンダー画像（500x500 JPG）の生成。

元ツール: 開発案件/shopifyカレンダー作成/generate_calendar.py（--closed 対応版）を移植。
日曜・土曜・日本の祝日（jpholiday で自動判定）・臨時休業日をピンク背景で塗る。
お盆・年末年始は祝日ではないため、臨時休業日として指定する必要がある。
"""

from __future__ import annotations

import calendar
import io
import os
from dataclasses import dataclass
from datetime import date

import jpholiday
from PIL import Image, ImageDraw, ImageFont

CANVAS_W = 500
CANVAS_H = 500
MARGIN_X = 15
HEADER_H = 55
WEEKDAY_H = 38

PINK = (255, 217, 221)
WHITE = (255, 255, 255)
BLACK = (30, 30, 30)
RED = (220, 40, 40)
BLUE = (50, 90, 200)
GREY_LINE = (210, 210, 210)

BOLD_FONTS = (
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\meiryob.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
)
REGULAR_FONTS = (
    r"C:\Windows\Fonts\YuGothR.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
)

# お盆・年末年始の入力漏れを防ぐため、画面で注意を出す月。
CLOSED_DAYS_REMINDER = {
    1: "年始の休業日を入力してください（例: 1-3）。",
    8: "お盆の休業日を入力してください（例: 13-16）。",
    12: "年末の休業日を入力してください（例: 29-31）。",
}


class ClosedDaysError(ValueError):
    """臨時休業日の入力が読めない・その月に存在しない日を含むとき。"""


@dataclass(frozen=True)
class CalendarMonth:
    year: int
    month: int

    @property
    def key(self) -> str:
        """保存・入力欄で使う識別子（例 2026_10）。"""
        return f"{self.year}_{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{self.year}年{self.month}月"

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    def next(self) -> CalendarMonth:
        if self.month == 12:
            return CalendarMonth(self.year + 1, 1)
        return CalendarMonth(self.year, self.month + 1)


def current_and_next(today: date) -> tuple[CalendarMonth, CalendarMonth]:
    current = CalendarMonth(today.year, today.month)
    return current, current.next()


def parse_closed_days(spec: str, target: CalendarMonth) -> tuple[int, ...]:
    """'13-16' / '13,15' / '13-16,20'（全角・読点・空白も可）を日付番号の昇順タプルにする。"""
    normalized = (
        (spec or "")
        .translate(str.maketrans("０１２３４５６７８９－ー〜～、，", "0123456789----,,"))
        .replace(" ", "")
        .replace("\u3000", "")
    )
    days: set[int] = set()
    for part in normalized.split(","):
        if not part:
            continue
        try:
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start > end:
                    raise ClosedDaysError(
                        f"{target.label}の休業日「{part}」は開始日が終了日より後になっています。"
                    )
                days.update(range(start, end + 1))
            else:
                days.add(int(part))
        except ValueError as exc:
            if isinstance(exc, ClosedDaysError):
                raise
            raise ClosedDaysError(
                f"{target.label}の休業日「{part}」を読み取れません。"
                "「13-16」や「13,15」の形で入力してください。"
            ) from exc
    out_of_range = sorted(d for d in days if d < 1 or d > target.days)
    if out_of_range:
        raise ClosedDaysError(
            f"{target.label}は{target.days}日までです（入力: {', '.join(map(str, out_of_range))}）。"
        )
    return tuple(sorted(days))


def format_closed_days(days: tuple[int, ...]) -> str:
    """parse_closed_days の逆。連続する日は範囲表記にまとめる（例 (13,14,15,20) -> '13-15,20'）。"""
    parts: list[str] = []
    run: list[int] = []
    for day in days:
        if run and day == run[-1] + 1:
            run.append(day)
            continue
        if run:
            parts.append(_run_text(run))
        run = [day]
    if run:
        parts.append(_run_text(run))
    return ",".join(parts)


def _run_text(run: list[int]) -> str:
    return str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}"


def holidays_in(target: CalendarMonth) -> list[tuple[int, str]]:
    """その月の祝日（振替休日・国民の休日を含む）を (日, 名称) で返す。画面表示用。"""
    result = []
    for day in range(1, target.days + 1):
        name = jpholiday.is_holiday_name(date(target.year, target.month, day))
        if name:
            result.append((day, name))
    return result


def _load_font(candidates: tuple[str, ...], size: int):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _month_weeks(target: CalendarMonth) -> list[list[date | None]]:
    """日曜始まりの週ごとの日付（月外は None）。"""
    cal = calendar.Calendar(firstweekday=6)
    return [
        [date(target.year, target.month, day) if day else None for day, _ in week]
        for week in cal.monthdays2calendar(target.year, target.month)
    ]


def render_calendar(target: CalendarMonth, closed_days: tuple[int, ...] = ()) -> bytes:
    """カレンダー画像を JPEG バイト列で返す。"""
    closed = set(closed_days)
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), WHITE)
    draw = ImageDraw.Draw(img)

    header_font = _load_font(BOLD_FONTS, 24)
    weekday_font = _load_font(BOLD_FONTS, 18)
    date_font = _load_font(REGULAR_FONTS, 18)

    draw.text((MARGIN_X, 14), target.label, font=header_font, fill=BLACK)
    draw.line([(MARGIN_X, HEADER_H), (CANVAS_W - MARGIN_X, HEADER_H)], fill=GREY_LINE, width=1)

    grid_left = MARGIN_X
    grid_right = CANVAS_W - MARGIN_X
    grid_top = HEADER_H + 4
    grid_bottom = CANVAS_H - 10
    cell_w = (grid_right - grid_left) / 7

    weekdays = ["日", "月", "火", "水", "木", "金", "土"]
    wd_colors = [RED, BLACK, BLACK, BLACK, BLACK, BLACK, BLUE]
    wd_bottom = grid_top + WEEKDAY_H
    for i, (label, color) in enumerate(zip(weekdays, wd_colors)):
        cx = grid_left + cell_w * i + cell_w / 2
        bbox = draw.textbbox((0, 0), label, font=weekday_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (cx - tw / 2, grid_top + (WEEKDAY_H - th) / 2 - 3),
            label,
            font=weekday_font,
            fill=color,
        )
    draw.line([(grid_left, wd_bottom), (grid_right, wd_bottom)], fill=GREY_LINE, width=1)

    weeks = _month_weeks(target)
    row_h = (grid_bottom - wd_bottom) / len(weeks)
    for r, week in enumerate(weeks):
        for c, day in enumerate(week):
            if day is None:
                continue
            x0 = grid_left + cell_w * c
            y0 = wd_bottom + row_h * r
            is_sun = c == 0
            is_sat = c == 6
            is_off = bool(jpholiday.is_holiday(day)) or day.day in closed
            if is_sun or is_sat or is_off:
                draw.rectangle([x0, y0, x0 + cell_w, y0 + row_h], fill=PINK)
            if is_sun or is_off:
                fill = RED
            elif is_sat:
                fill = BLUE
            else:
                fill = BLACK
            draw.text((x0 + 9, y0 + 7), str(day.day), font=date_font, fill=fill)

    for r in range(1, len(weeks)):
        y = wd_bottom + row_h * r
        draw.line([(grid_left, y), (grid_right, y)], fill=GREY_LINE, width=1)

    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()
