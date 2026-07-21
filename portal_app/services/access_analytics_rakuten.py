"""楽天RMSの商品ページ分析CSVを端末別に取得する。

一次情報: Obsidian「楽天市場-デバイス別アクセス数取得手順.md」
（2026-07-12 観測）。実サイトへの接続・ログインは未検証（認証情報なし）。
DOM操作はノートの観測済み契約を転記した実装であり、初回実行時に実地検証が必要。

自動ログイン（2026-07-12 ユーザー許可により追加）: ログイン画面への
リダイレクトを検知した場合のみ、環境変数 KURIMA_RAKUTEN_LOGIN_ID /
KURIMA_RAKUTEN_LOGIN_PASSWORD が設定されていればPlaywrightでログインを
試みる（`_attempt_rakuten_login`）。ログインフォームのDOM構造は未観測のため
一般的なパターンで実装しており、初回実行時の実地検証が必要。追加認証
（2段階認証等）は自動突破せず AUTH_REQUIRED_MANUAL で停止する。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from portal_app.services.execution_logger import APP_ROOT
from portal_app.services.paths import find_access_analytics_paths


RAKUTEN_ITEM_ACCESS_URL = "https://datatool.rms.rakuten.co.jp/access/item"
# 未ログイン状態で RAKUTEN_ITEM_ACCESS_URL へアクセスすると
# mainmenu.rms.rakuten.co.jp の「認証エラー」ページ（ログインフォームなし）へ
# 着地することを実接続で確認した（2026-07-12）。R-Loginへは以下の直URLで
# 入る（既存の稼働実績あり。日別売上集計データダウンロードプロジェクト
# `src/downloader/rakuten.py` を参照して2026-07-13に採用）。
RAKUTEN_LOGIN_ENTRY_URL = (
    "https://glogin.rms.rakuten.co.jp/"
    "?module=BizAuth&action=BizAuthCustomerAttest&sp_id=1"
)
DEVICE_OPTIONS = {
    "pc": "PC",
    "sdApp": "楽天市場アプリ",
    "sdWeb": "スマートフォン",
}
LOADING_TEXT = "読み込み中です。しばらくお待ちください。"
EXPECTED_HEADER_28 = (
    "#",
    "ジャンル",
    "カタログID",
    "商品ID",
    "商品名",
    "商品管理番号",
    "商品番号",
    "売上",
    "売上件数",
    "売上個数",
    "アクセス人数",
    "ユニークユーザー数",
    "転換率",
    "客単価",
    "総購入件数",
    "新規購入件数",
    "リピート購入件数",
    "未購入アクセス人数",
    "レビュー投稿数",
    "レビュー総合評価（点）",
    "総レビュー数",
    "滞在時間（秒）",
    "直帰数",
    "離脱数",
    "離脱率",
    "お気に入り登録ユーザ数",
    "お気に入り総ユーザ数",
    "在庫数",
)

_PROFILE_ENV = "KURIMA_ACCESS_ANALYTICS_RAKUTEN_CHROME_PROFILE"
_AUDIT_PATH = APP_ROOT / "logs" / "access_analytics" / "rakuten_device_access.jsonl"
_DEVICE_FILE_KEYS = {
    "pc": "pc",
    "sdApp": "app",
    "sdWeb": "smartphone_web",
    "all": "all",
}
_NO_DATA_MARKERS = (
    "該当するデータがありません",
    "対象データがありません",
    "データがありません",
    "0件",
)

# 自動ログイン（2026-07-12 ユーザー許可により追加。Obsidianノートの既存方針
# 「初回・期限切れ時のログインは人が手動で行う」から本フローに限り意図的に逸脱する）。
# 環境変数は KURIMA_RAKUTEN_LOGIN_ID / KURIMA_RAKUTEN_LOGIN_PASSWORD
# （portal_tool/.env.example への追記を試みたが、本セッションのハーネスは .env* に
# 一致するファイルへの Read/Bash/Edit を一律で拒否するため、実際には追記できていない。
# 詳細は turn-000-report.md に記載）。値は関数ローカル変数としてのみ扱い、
# ログ・監査JSONL・manifest・例外メッセージ・戻り値のdataclassには一切含めない。
# R-Loginは2段階（2026-07-13、ユーザーの実操作・実画面で確認）:
#   1. R-Login ID + 管理（KANRI）パスワード → glogin.rms.rakuten.co.jp のフォーム
#      環境変数 KURIMA_RAKUTEN_KANRI_LOGIN_ID / KURIMA_RAKUTEN_KANRI_PASSWORD
#   2. 「楽天会員ログインへ」ボタン押下後、楽天会員ログイン（ID/メールアドレス + パスワード）
#      環境変数 KURIMA_RAKUTEN_LOGIN_ID / KURIMA_RAKUTEN_LOGIN_PASSWORD
# ユーザーの既存 .env 資産（KURIMA_RAKUTEN_LOGIN_ID等）は2段階目の値だったため、
# 変数名の意味をユーザー指定に合わせて確定した。
_LOGIN_ID_ENV = "KURIMA_RAKUTEN_KANRI_LOGIN_ID"
_LOGIN_PASSWORD_ENV = "KURIMA_RAKUTEN_KANRI_PASSWORD"
_MEMBER_EMAIL_ENV = "KURIMA_RAKUTEN_LOGIN_ID"
_MEMBER_PASSWORD_ENV = "KURIMA_RAKUTEN_LOGIN_PASSWORD"
# 実R-Loginフォームのセレクタ。#rlogin-username-ja / #rlogin-password-ja /
# button.rf-button-primary[name="submit"] は既存の稼働実績あり
# （日別売上集計データダウンロードプロジェクト `src/downloader/rakuten.py`
# を参照して2026-07-13に採用）。汎用パターンは未観測サイトへのフォールバック。
_LOGIN_ID_SELECTORS = (
    "#rlogin-username-ja",
    'input[name="login_id"]',
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[name*="id" i]',
    'input[name="login"]',
    'input#login_handle',
)
_LOGIN_PASSWORD_SELECTORS = (
    "#rlogin-password-ja",
    'input[name="passwd"]',
    'input[type="password"]',
)
_LOGIN_SUBMIT_SELECTORS = (
    'button.rf-button-primary[name="submit"]',
    'button[name="submit"]',
    'button[type="submit"]',
    'input[type="submit"]',
)
# 2段階目（楽天会員ログイン、ID入力→次へ→パスワード入力の場合がある）。
_MEMBER_ID_SELECTORS = ('input[type="text"]', 'input[name="u"]')
_MEMBER_NEXT_SELECTORS = ('div[role="button"]:has-text("次へ")', 'button:has-text("次へ")')
_MEMBER_LOGIN_SELECTORS = (
    'div[role="button"]:has-text("次へ")',
    'div[role="button"]:has-text("ログイン")',
)
_TWO_FACTOR_MARKERS = (
    "認証コード",
    "確認コード",
    "ワンタイムパスワード",
    "二段階認証",
    "セキュリティコード",
    "verification code",
    "文字認証",
    "画像で認証",
    "音声で認証",
)


class AccessAnalyticsError(RuntimeError):
    """利用者向けstateを保持するアクセス解析取得エラー。"""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class RakutenDeviceAccessCsv:
    device: str
    target_date: date
    downloaded_file: Path
    source_sha256: str
    row_count: int
    header_columns: tuple[str, ...]


@dataclass(frozen=True)
class RakutenDeviceAccessResult:
    executed: bool
    target_date: date
    csv_files: tuple[RakutenDeviceAccessCsv, ...]
    skipped_reason: str | None
    warnings: tuple[str, ...]


def _normalise_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("target_date は YYYY-MM-DD で指定してください。") from exc


def _profile_dir() -> Path:
    override = os.environ.get(_PROFILE_ENV, "").strip()
    return (
        Path(override)
        if override
        else APP_ROOT / "data" / "access_analytics_rakuten_chrome_profile"
    )


def _headless_value(value: bool | None) -> bool:
    if value is not None:
        return value
    return os.environ.get("KURIMA_BROWSER_HEADLESS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_records() -> list[dict[str, object]]:
    manifest_path = find_access_analytics_paths().manifest_path
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _append_manifest(record: dict[str, object]) -> None:
    """manifest書込は集約層 access_analytics.append_access_analytics_manifest に一本化する。

    集約層（access_analytics.py）が本モジュールを import するため、
    ここでは遅延importで循環importを避ける。
    """
    from portal_app.services.access_analytics import append_access_analytics_manifest

    append_access_analytics_manifest(record)


def _append_audit(result: RakutenDeviceAccessResult) -> None:
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "executed": result.executed,
        "target_date": result.target_date.isoformat(),
        "skipped_reason": result.skipped_reason,
        "warnings": list(result.warnings),
        "files": [
            {
                "device": item.device,
                "filename": item.downloaded_file.name,
                "sha256": item.source_sha256,
                "row_count": item.row_count,
            }
            for item in result.csv_files
        ],
    }
    with _AUDIT_PATH.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_csv_rows(data: bytes) -> tuple[str, list[list[str]]]:
    if not data.startswith(b"\xef\xbb\xbf"):
        raise AccessAnalyticsError(
            "楽天の商品ページ分析CSVにUTF-8 BOMがありません。",
            state="SCHEMA_MISMATCH",
        )
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise AccessAnalyticsError(
            "楽天の商品ページ分析CSVをUTF-8として読めませんでした。",
            state="SCHEMA_MISMATCH",
        ) from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise AccessAnalyticsError(
            "楽天の商品ページ分析CSVの解析に失敗しました。",
            state="SCHEMA_MISMATCH",
        ) from exc
    return text, rows


def validate_rakuten_device_csv(
    data: bytes,
    *,
    target_date: date | str,
    expected_device_label: str,
    no_data_confirmed: bool = False,
) -> tuple[int, tuple[str, ...]]:
    """ノート記載の6行メタデータと28列明細を検証する純関数。"""

    requested = _normalise_date(target_date)
    text, rows = _read_csv_rows(data)
    if len(rows) < 6:
        raise AccessAnalyticsError(
            "楽天の商品ページ分析CSVに6行のメタデータ／ヘッダーがありません。",
            state="SCHEMA_MISMATCH",
        )

    period_text = " ".join(rows[2])
    period_dates: set[date] = set()
    for pattern in (
        r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ):
        for match in re.finditer(pattern, period_text):
            try:
                period_dates.add(
                    date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                )
            except ValueError:
                continue
    if period_dates != {requested}:
        raise AccessAnalyticsError(
            "CSVの3行目にある対象期間が要求日の1日分と一致しません。",
            state="DATA_NOT_UPDATED",
        )

    device_text = " ".join(rows[4])
    if expected_device_label not in device_text:
        raise AccessAnalyticsError(
            "CSVの5行目にある端末が要求端末と一致しません。",
            state="DATA_NOT_UPDATED",
        )

    header = tuple(rows[5])
    if header != EXPECTED_HEADER_28:
        raise AccessAnalyticsError(
            "楽天の商品ページ分析CSVの28列ヘッダーが変更されています。",
            state="SCHEMA_MISMATCH",
        )

    data_rows = [row for row in rows[6:] if any(cell.strip() for cell in row)]
    if not data_rows:
        if not no_data_confirmed and not any(marker in text for marker in _NO_DATA_MARKERS):
            raise AccessAnalyticsError(
                "CSVは0件でしたが、画面上の明示的な0件表示を確認できません。",
                state="DATA_NOT_UPDATED",
            )
        return 0, header

    management_numbers: set[str] = set()
    for row_number, row in enumerate(data_rows, start=7):
        if len(row) != len(EXPECTED_HEADER_28):
            raise AccessAnalyticsError(
                f"CSVの{row_number}行目が28列ではありません。",
                state="SCHEMA_MISMATCH",
            )
        management_number = row[5].strip()
        if not management_number:
            raise AccessAnalyticsError(
                f"CSVの{row_number}行目で商品管理番号が空です。",
                state="SCHEMA_MISMATCH",
            )
        if management_number in management_numbers:
            raise AccessAnalyticsError(
                "CSV内で商品管理番号が重複しています。",
                state="SCHEMA_MISMATCH",
            )
        management_numbers.add(management_number)
        for column_index, column_name in ((10, "アクセス人数"), (11, "ユニークユーザー数")):
            value = row[column_index].strip()
            if not re.fullmatch(r"\d+", value):
                raise AccessAnalyticsError(
                    f"CSVの{row_number}行目の{column_name}が0以上の整数ではありません。",
                    state="SCHEMA_MISMATCH",
                )
    return len(data_rows), header


def _visible_file_snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() in {".tmp", ".crdownload", ".part"}:
            continue
        stat = path.stat()
        snapshot[path] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


async def _wait_for_stable_download(
    directory: Path,
    before: dict[Path, tuple[int, int]],
    *,
    timeout_seconds: float = 30.0,
) -> Path:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    previous_sizes: dict[Path, int] = {}
    stable_counts: dict[Path, int] = {}
    while asyncio.get_running_loop().time() < deadline:
        current = _visible_file_snapshot(directory)
        candidates = [path for path, stamp in current.items() if before.get(path) != stamp]
        if len(candidates) > 1:
            raise AccessAnalyticsError(
                "ダウンロード候補が複数あり、安全に1件へ確定できません。",
                state="AMBIGUOUS_DOWNLOAD",
            )
        if len(candidates) == 1:
            candidate = candidates[0]
            size = current[candidate][0]
            stable_counts[candidate] = (
                stable_counts.get(candidate, 0) + 1
                if previous_sizes.get(candidate) == size and size > 0
                else 0
            )
            previous_sizes[candidate] = size
            if stable_counts[candidate] >= 2:
                return candidate
        await asyncio.sleep(0.5)
    raise AccessAnalyticsError(
        "楽天の商品ページ分析CSVを回収できませんでした。",
        state="DOWNLOAD_FAILED",
    )


async def _capture_download(page, click_locator, download_dir: Path, destination: Path) -> Path:
    before = _visible_file_snapshot(download_dir)
    try:
        async with page.expect_download(timeout=30_000) as download_info:
            await click_locator.click()
        download = await download_info.value
        failure = await download.failure()
        if failure:
            raise AccessAnalyticsError(
                f"楽天CSVのダウンロードが失敗しました: {failure}",
                state="DOWNLOAD_FAILED",
            )
        await download.save_as(str(destination))
        return destination
    except PlaywrightTimeoutError:
        fallback = await _wait_for_stable_download(download_dir, before)
        if fallback.resolve() != destination.resolve():
            shutil.move(str(fallback), destination)
        return destination


async def _calendar_month_texts(page) -> list[str]:
    headers = page.locator(".daterangepicker .drp-calendar th.month")
    return [await headers.nth(index).inner_text() for index in range(await headers.count())]


def _month_header_matches(value: str, target: date) -> bool:
    parsed = _parse_month_header(value)
    if parsed is not None:
        return parsed == (target.year, target.month)
    compact = re.sub(r"\s+", "", value)
    candidates = {
        f"{target.year}年{target.month}月",
        f"{target.year}/{target.month}",
        f"{target.year}/{target.month:02d}",
        target.strftime("%B%Y").lower(),
        target.strftime("%b%Y").lower(),
    }
    return any(candidate.lower() in compact.lower() for candidate in candidates)


def _parse_month_header(value: str) -> tuple[int, int] | None:
    compact = re.sub(r"\s+", "", value)
    # 実RMSの月ヘッダーは「6月 2026」（月が先・年が後）だった
    # （2026-07-13、ログイン後の実画面で確認）。年が先のパターンより先に判定する。
    month_first = re.match(r"^(\d{1,2})月(20\d{2})$", compact)
    if month_first and 1 <= int(month_first.group(1)) <= 12:
        return int(month_first.group(2)), int(month_first.group(1))
    numeric = re.search(r"(20\d{2})\D+(\d{1,2})", compact)
    if numeric and 1 <= int(numeric.group(2)) <= 12:
        return int(numeric.group(1)), int(numeric.group(2))
    english_months = {
        name.lower(): number
        for number, name in enumerate(
            (
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ),
            start=1,
        )
    }
    english_months.update(
        {name[:3]: number for name, number in tuple(english_months.items())}
    )
    lowered = compact.lower()
    year_match = re.search(r"20\d{2}", lowered)
    if year_match:
        for name, month_number in english_months.items():
            if name in lowered:
                return int(year_match.group()), month_number
    return None


async def _check_rms_radio(page, radio_locator) -> None:
    """RMSのラジオボタンをチェックする。

    RMSのラジオは装飾用の `<span class="rms-check-box">` に覆われており、
    Playwrightの `check()` は「span intercepts pointer events」で
    30秒タイムアウトする（2026-07-13、実画面で確認）。
    まず通常の check() を短いタイムアウトで試し、遮断された場合は
    ラベル経由クリック → force clickの順にフォールバックする。
    """
    radio = radio_locator.first
    try:
        await radio.check(timeout=3_000)
        return
    except PlaywrightTimeoutError:
        pass

    # 装飾spanを含むラベル（またはラジオの親）をクリックする。
    element_id = await radio.get_attribute("id")
    if element_id:
        label = page.locator(f'label[for="{element_id}"]')
        if await label.count() > 0:
            await label.first.click()
            if await radio.is_checked():
                return

    await radio.check(force=True)


async def _select_target_date(page, target: date) -> None:
    date_input = page.locator('input[data-toggle="daterangepicker"]').first
    await date_input.wait_for(state="visible")
    # readonly入力をfillせず、観測済みのdaterangepicker UIだけを操作する。
    await date_input.click()
    picker = page.locator(".daterangepicker").filter(has=page.locator(".drp-calendar")).first
    await picker.wait_for(state="visible")

    for _ in range(36):
        month_texts = await _calendar_month_texts(page)
        for calendar_index, month_text in enumerate(month_texts):
            if not _month_header_matches(month_text, target):
                continue
            calendar = page.locator(".daterangepicker .drp-calendar").nth(calendar_index)
            day = calendar.locator("td.available:not(.off)").filter(
                has_text=re.compile(rf"^\s*{target.day}\s*$")
            )
            if await day.count() == 0:
                continue
            # 期間指定UI（range picker）なので開始日・終了日として同じ日を2回クリックし、
            # 単日（開始=終了）の期間にする。
            await day.first.click()
            await page.wait_for_timeout(200)
            day_again = calendar.locator("td.available:not(.off)").filter(
                has_text=re.compile(rf"^\s*{target.day}\s*$")
            )
            if await day_again.count() > 0:
                await day_again.first.click()
                await page.wait_for_timeout(200)
            apply_button = page.get_by_role("button", name="決定", exact=True)
            if await apply_button.count() == 0:
                apply_button = page.get_by_role("button", name="適用", exact=True)
            await apply_button.first.click()
            return

        visible_year_months = [
            parsed
            for text in month_texts
            if (parsed := _parse_month_header(text)) is not None
        ]
        target_pair = (target.year, target.month)
        if visible_year_months and target_pair < min(visible_year_months):
            move_button = page.locator(".daterangepicker .prev.available")
        else:
            move_button = page.locator(".daterangepicker .next.available")
        # 未来方向の .next は「表示可能な最新月まで来ている」ときDOMから消える
        # （2026-07-13、実画面で確認）。存在しない移動ボタンを押そうとして
        # 30秒タイムアウトするのを防ぐ。
        if await move_button.count() == 0:
            break
        await move_button.last.click()
        await page.wait_for_timeout(150)
    raise AccessAnalyticsError(
        "指定日を日付ピッカーで選択できませんでした。",
        state="PAGE_CONTRACT_CHANGED",
    )


async def _find_first_visible(page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    return None


async def _click_login_submit(page) -> bool:
    button = await _find_first_visible(page, _LOGIN_SUBMIT_SELECTORS)
    if button is None:
        named = page.get_by_role(
            "button", name=re.compile(r"ログイン|次へ|Next|Login", re.IGNORECASE)
        )
        if await named.count() == 0:
            return False
        button = named.first
    await button.click()
    return True


async def _login_body_text(page) -> str:
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return ""


async def _has_two_factor_prompt(page) -> bool:
    text = await _login_body_text(page)
    return any(marker in text for marker in _TWO_FACTOR_MARKERS)


async def _attempt_rakuten_login(page, *, target_url: str) -> None:
    """ログイン画面へリダイレクトされた場合にのみ呼ばれる自動ログインの試行。

    5画面フロー（2026-07-13、日別売上集計データダウンロードプロジェクト
    `src/downloader/rakuten.py` の稼働実績を移植）:
      1. R-Login ID + 管理（KANRI）パスワード
      2. 楽天会員ログイン（ID入力→次へ→パスワード入力。Cookie記憶時は
         パスワード画面へ直接進むことがある）
      3. 「次へ」ボタン（存在する場合のみ）
      4. RMS利用確認（存在する場合のみ）
      5. お知らせ画面「RMSメインメニューへ進む」（存在する場合のみ）
    環境変数が未設定なら何もせず戻り、呼び出し元が従来どおりNEEDS_LOGINを
    送出する。2段階認証等の追加認証を検知した場合は自動突破せず
    AUTH_REQUIRED_MANUAL で停止する（Obsidianノート「追加認証は人が行う」
    方針は維持）。
    """

    login_id = os.environ.get(_LOGIN_ID_ENV, "").strip()
    login_password = os.environ.get(_LOGIN_PASSWORD_ENV, "").strip()
    if not login_id or not login_password:
        return
    member_email = os.environ.get(_MEMBER_EMAIL_ENV, "").strip()
    member_password = os.environ.get(_MEMBER_PASSWORD_ENV, "").strip()

    # --- 1画面目: R-Login ID + KANRIパスワード ---
    await page.goto(RAKUTEN_LOGIN_ENTRY_URL, wait_until="domcontentloaded")
    id_field = await _find_first_visible(page, _LOGIN_ID_SELECTORS)
    if id_field is None:
        raise AccessAnalyticsError(
            "楽天RMSログインフォームのID欄を特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await id_field.fill(login_id)
    password_field = await _find_first_visible(page, _LOGIN_PASSWORD_SELECTORS)
    if password_field is None:
        raise AccessAnalyticsError(
            "楽天RMSログインフォームのパスワード欄を特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await password_field.fill(login_password)
    if not await _click_login_submit(page):
        raise AccessAnalyticsError(
            "楽天RMSログインフォームの送信ボタンを特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await page.wait_for_load_state("domcontentloaded")

    if await _has_two_factor_prompt(page):
        raise AccessAnalyticsError(
            "楽天RMSログインで追加認証が要求されました。人手でログインしてください。",
            state="AUTH_REQUIRED_MANUAL",
        )

    # --- 2画面目: 楽天会員ログイン（ID記憶済みならパスワード欄へ直接進む） ---
    if member_email and member_password:
        await page.wait_for_timeout(3_000)
        pw_input = await _find_first_visible(page, _LOGIN_PASSWORD_SELECTORS)
        id_input = await _find_first_visible(page, _MEMBER_ID_SELECTORS)

        if pw_input is None:
            for attempt in range(1, 4):
                id_input = await _find_first_visible(page, _MEMBER_ID_SELECTORS)
                if id_input is not None:
                    break
                if attempt >= 3:
                    break
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(3_000)

            if id_input is not None:
                await id_input.fill(member_email)
                next_button = await _find_first_visible(page, _MEMBER_NEXT_SELECTORS)
                if next_button is not None:
                    await next_button.click()
                await page.wait_for_timeout(3_000)
                pw_input = await _find_first_visible(page, _LOGIN_PASSWORD_SELECTORS)

        if pw_input is not None:
            await pw_input.fill(member_password)
            login_button = await _find_first_visible(page, _MEMBER_LOGIN_SELECTORS)
            if login_button is not None:
                await login_button.click(force=True)
            else:
                await _click_login_submit(page)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(3_000)

            if await _has_two_factor_prompt(page):
                raise AccessAnalyticsError(
                    "楽天会員ログインで追加認証が要求されました。人手でログインしてください。",
                    state="AUTH_REQUIRED_MANUAL",
                )

    # --- 3〜5画面目: ログイン後に挟まる中間画面（出たものだけ順に片づける） ---
    #
    # 2026-07-14 の headless 実接続で判明した最重要の罠:
    # 「重要なお知らせ」画面は
    #     <input type="checkbox" name="confirm" value="...">
    #     <button class="btn-reset btn-round btn-red">RMSメインメニューへ進む</button>
    # という構成で、(a) チェックボックスにチェックを入れてからでないと先へ進めず、
    # (b) ボタンには type="submit" 属性が無い（旧実装は
    #     'button.btn-reset.btn-round.btn-red[type="submit"]' で探しており永久に不一致）。
    # この画面を抜けないと datatool.rms.rakuten.co.jp 側のセッションが確立せず、
    # 直後に商品ページ分析の直URLへ行っても ?act=app_login_error に落ちる
    # （＝「ログインが切れています」に見える）。headed 実行時はこの画面が
    # 表示されず露見しなかった。
    # 中間画面は出る/出ないが状況で変わるため、固定の順序ではなくループで処理する。
    for _ in range(5):
        confirm_box = page.locator('input[type="checkbox"][name="confirm"]')
        if await confirm_box.count() > 0 and await confirm_box.first.is_visible():
            if not await confirm_box.first.is_checked():
                await confirm_box.first.check()

        clicked = False
        for candidate in (
            page.locator("button", has_text="RMSメインメニューへ進む"),
            page.locator("button.btn-reset.btn-round.btn-red"),
            page.locator('button.rf-button-primary[name="submit"]'),
        ):
            if await candidate.count() > 0 and await candidate.first.is_visible():
                await candidate.first.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1_500)
                clicked = True
                break

        if not clicked:
            break

    await page.goto(target_url, wait_until="domcontentloaded")


async def _assert_logged_in(page) -> None:
    parsed = urlparse(page.url)
    if parsed.hostname != "datatool.rms.rakuten.co.jp" or "login" in parsed.path.lower():
        raise AccessAnalyticsError(
            "楽天RMSのログインが切れています。",
            state="NEEDS_LOGIN",
        )


async def _page_confirms_no_data(page) -> bool:
    marker = page.get_by_text(
        re.compile(r"(?:該当するデータ|対象データ|データ)がありません")
    )
    for index in range(await marker.count()):
        if await marker.nth(index).is_visible():
            return True
    return False


def _quarantine(path: Path, *, label: str) -> None:
    if not path.exists():
        return
    quarantine = find_access_analytics_paths().root / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    destination = quarantine / f"{datetime.now():%Y%m%dT%H%M%S}_{label}_{path.name}"
    shutil.move(str(path), destination)


def _commit_csv(
    temporary: Path,
    *,
    target: date,
    device: str,
    device_label: str,
    batch_id: str,
    no_data_confirmed: bool,
) -> RakutenDeviceAccessCsv:
    data = temporary.read_bytes()
    try:
        row_count, header = validate_rakuten_device_csv(
            data,
            target_date=target,
            expected_device_label=device_label,
            no_data_confirmed=no_data_confirmed,
        )
    except Exception:
        _quarantine(temporary, label=device)
        raise

    paths = find_access_analytics_paths()
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256(data).hexdigest()
    filename = f"rakuten_item_access_{device}_{target:%Y%m%d}.csv"
    destination = paths.raw_dir / filename
    if destination.exists() and _sha256(destination) == sha256:
        temporary.unlink(missing_ok=True)
    else:
        if destination.exists():
            destination = paths.raw_dir / (
                f"{destination.stem}_{datetime.now():%H%M%S}{destination.suffix}"
            )
        temporary.replace(destination)

    artifact_id = uuid4().hex
    _append_manifest(
        {
            "artifact_id": artifact_id,
            "batch_id": batch_id,
            "mall": "rakuten",
            "category": "device_access",
            "filename": destination.name,
            "relative_path": destination.resolve().relative_to(paths.root.resolve()).as_posix(),
            "sha256": sha256,
            "row_count": row_count,
            "device": device,
            "device_label": device_label,
            "target_label": target.isoformat(),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return RakutenDeviceAccessCsv(
        device=device,
        target_date=target,
        downloaded_file=destination,
        source_sha256=sha256,
        row_count=row_count,
        header_columns=header,
    )


def _read_saved_result(target: date, *, include_all: bool) -> RakutenDeviceAccessResult:
    paths = find_access_analytics_paths()
    wanted = {"pc", "app", "smartphone_web"}
    if include_all:
        wanted.add("all")
    newest_by_device: dict[str, dict[str, object]] = {}
    for record in _manifest_records():
        if record.get("mall") != "rakuten" or record.get("target_label") != target.isoformat():
            continue
        device = str(record.get("device", ""))
        if device in wanted:
            newest_by_device[device] = record
    files: list[RakutenDeviceAccessCsv] = []
    warnings: list[str] = []
    labels = {
        "all": "すべて",
        "pc": "PC",
        "app": "楽天市場アプリ",
        "smartphone_web": "スマートフォン",
    }
    for device in sorted(wanted):
        record = newest_by_device.get(device)
        if not record:
            warnings.append(f"{device} の保存済みmanifestがありません。")
            continue
        relative = str(record.get("relative_path", ""))
        candidate = (paths.root / relative).resolve()
        try:
            candidate.relative_to(paths.root.resolve())
        except ValueError:
            warnings.append(f"{device} のmanifestパスが保存ルート外です。")
            continue
        if not candidate.is_file():
            warnings.append(f"{device} の保存済みファイルが見つかりません。")
            continue
        data = candidate.read_bytes()
        row_count, header = validate_rakuten_device_csv(
            data,
            target_date=target,
            expected_device_label=labels[device],
            no_data_confirmed=int(record.get("row_count") or 0) == 0,
        )
        sha256 = hashlib.sha256(data).hexdigest()
        if record.get("sha256") and record.get("sha256") != sha256:
            warnings.append(f"{device} のSHA-256がmanifestと一致しません。")
            continue
        files.append(
            RakutenDeviceAccessCsv(
                device=device,
                target_date=target,
                downloaded_file=candidate,
                source_sha256=sha256,
                row_count=row_count,
                header_columns=header,
            )
        )
    result = RakutenDeviceAccessResult(
        executed=False,
        target_date=target,
        csv_files=tuple(files),
        skipped_reason="dry-run: 保存済みmanifestとCSVのみ検証しました。",
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


async def download_rakuten_device_access(
    *,
    execute: bool,
    target_date: date | str,
    include_all: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> RakutenDeviceAccessResult:
    """要求日のPC・アプリ・スマートフォンWeb CSVを個別に取得する。"""

    target = _normalise_date(target_date)
    if not execute:
        return _read_saved_result(target, include_all=include_all)

    paths = find_access_analytics_paths()
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = _profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    batch_id = uuid4().hex
    batch_dir = paths.staging_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    devices = list(DEVICE_OPTIONS.items())
    if include_all:
        devices.append(("all", "すべて"))
    collected: list[RakutenDeviceAccessCsv] = []
    warnings: list[str] = []

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                accept_downloads=True,
                downloads_path=str(batch_dir),
                headless=_headless_value(headless),
                locale="ja-JP",
                slow_mo=max(0, int(slow_mo_ms)),
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(RAKUTEN_ITEM_ACCESS_URL, wait_until="domcontentloaded")
                try:
                    await _assert_logged_in(page)
                except AccessAnalyticsError as exc:
                    if exc.state != "NEEDS_LOGIN":
                        raise
                    # ログイン画面へリダイレクトされた場合のみ自動ログインを試みる
                    # （環境変数未設定なら_attempt_rakuten_loginは何もせず戻り、
                    # 直後のassertが従来どおりNEEDS_LOGINを送出する）。
                    await _attempt_rakuten_login(page, target_url=RAKUTEN_ITEM_ACCESS_URL)
                    await _assert_logged_in(page)
                daily = page.locator('input[type="radio"][value="daily"]')
                if await daily.count() == 0:
                    raise AccessAnalyticsError(
                        "日次ラジオボタンが見つかりません。",
                        state="PAGE_CONTRACT_CHANGED",
                    )
                await _check_rms_radio(page, daily)
                await _select_target_date(page, target)

                for radio_value, device_label in devices:
                    device_radio = page.locator(
                        f'input[type="radio"][value="{radio_value}"]'
                    )
                    if await device_radio.count() == 0:
                        raise AccessAnalyticsError(
                            f"端末 {device_label} のラジオボタンが見つかりません。",
                            state="PAGE_CONTRACT_CHANGED",
                        )
                    await _check_rms_radio(page, device_radio)
                    # 「読み込み中です。〜」はDOMに常時存在するとは限らない
                    # （2026-07-13の実画面ではヒット0件だった）。存在するときだけ
                    # 非表示になるまで待ち、無ければ短い固定待ちで再描画を待つ。
                    loading = page.get_by_text(LOADING_TEXT, exact=True)
                    if await loading.count() > 0:
                        await loading.wait_for(state="hidden", timeout=60_000)
                    else:
                        await page.wait_for_timeout(2_000)
                    await _assert_logged_in(page)
                    no_data_confirmed = await _page_confirms_no_data(page)

                    csv_button = page.get_by_role(
                        "button", name=re.compile(r"^全商品CSV")
                    )
                    if await csv_button.count() == 0:
                        raise AccessAnalyticsError(
                            "全商品CSVボタンが見つかりません。",
                            state="PAGE_CONTRACT_CHANGED",
                        )
                    await csv_button.first.click()
                    dialog = page.get_by_role("dialog")
                    await dialog.wait_for(state="visible")
                    heading = dialog.get_by_text("CSVダウンロード", exact=True)
                    if await heading.count() == 0:
                        raise AccessAnalyticsError(
                            "CSVダウンロードダイアログを確認できません。",
                            state="PAGE_CONTRACT_CHANGED",
                        )
                    # ダイアログ内のラジオもRMSの装飾spanに覆われている可能性がある。
                    await _check_rms_radio(
                        page,
                        dialog.get_by_role("radio", name="すべての項目", exact=True),
                    )
                    await _check_rms_radio(
                        page, dialog.get_by_role("radio", name="全件", exact=True)
                    )
                    destination = batch_dir / f"{radio_value}_{uuid4().hex}.csv"
                    await _capture_download(
                        page,
                        # 実ダイアログの「ダウンロード」ボタンは
                        # <button><div><span>ダウンロード</span></div></button> と
                        # ネストしており、get_by_role(name=..., exact=True) では
                        # アクセシブル名が一致せずヒットしない（2026-07-13、実画面で確認）。
                        dialog.locator("button", has_text="ダウンロード").first,
                        batch_dir,
                        destination,
                    )
                    await _assert_logged_in(page)
                    collected.append(
                        _commit_csv(
                            destination,
                            target=target,
                            device=_DEVICE_FILE_KEYS[radio_value],
                            device_label=device_label,
                            batch_id=batch_id,
                            no_data_confirmed=no_data_confirmed,
                        )
                    )
            finally:
                await context.close()
    except AccessAnalyticsError:
        raise
    except PlaywrightTimeoutError as exc:
        raise AccessAnalyticsError(
            "楽天RMS画面の応答待ちがタイムアウトしました。",
            state="PAGE_CONTRACT_CHANGED",
        ) from exc
    finally:
        if batch_dir.exists() and not any(batch_dir.iterdir()):
            batch_dir.rmdir()

    result = RakutenDeviceAccessResult(
        executed=True,
        target_date=target,
        csv_files=tuple(collected),
        skipped_reason=None,
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


def download_rakuten_device_access_sync(
    *,
    execute: bool,
    target_date: date | str,
    include_all: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> RakutenDeviceAccessResult:
    return asyncio.run(
        download_rakuten_device_access(
            execute=execute,
            target_date=target_date,
            include_all=include_all,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )
