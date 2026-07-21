"""Yahoo!ストアクリエイターProの商品分析・全体分析CSVを取得する。

一次情報: Obsidian「Yahoo!ショッピング-デバイス別アクセス数取得手順.md」
（2026-07-12 観測）。実サイトへの接続・ログインは未検証（認証情報なし）。
DOM操作はノートの観測済み契約を転記した実装であり、初回実行時に実地検証が必要。

自動ログイン（2026-07-12 ユーザー許可により追加）: ログイン画面への
リダイレクトを検知した場合のみ、環境変数 KURIMA_YAHOO_LOGIN_ID /
KURIMA_YAHOO_LOGIN_PASSWORD が設定されていればPlaywrightでログインを
試みる（`_attempt_yahoo_login`）。ログインフォームのDOM構造は未観測のため
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


YAHOO_PRO_BASE = "https://pro.store.yahoo.co.jp/pro.{store_account}"
OVERALL_PATH = "/sales_manage/overall"
ITEM_REPORT_PATH = "/sales_manage/item_report"
DATE_FROM_SELECTOR = "#dailyInputDatepickerFrom"
DATE_TO_SELECTOR = "#dailyInputDatepickerTo"
DATE_APPLY_SELECTOR = "#dailyInputApplyButton"
OVERALL_DOWNLOAD_SELECTOR = "#dataTableCsvDownload"
PRODUCT_DOWNLOAD_SELECTOR = "#itemReportCsvDownload"
DEVICE_BUTTONS = (
    (".buttons-device_pc", "pc", "pc", "PC"),
    (".buttons-device_smt", "sp", "smartphone_web", "スマートフォンWeb"),
    (".buttons-device_app", "app", "app", "アプリ"),
    (".buttons-device_sum4", "all", "all", "合算値"),
)
EXPECTED_PRODUCT_HEADER_14 = (
    "商品名",
    "商品コード",
    "サブコード",
    "売上合計値（税込）",
    "注文数合計",
    "注文点数合計",
    "注文者数合計",
    "平均購買率",
    "お気に入り保存数",
    "カート投入数",
    "ページビュー（優良配送あり）",
    "ページビュー（優良配送なし）",
    "訪問者数",
    "貢献度（カテゴリ）",
)
EXPECTED_OVERALL_BASE_COLUMNS = frozenset(
    {"日付", "ページビュー", "セッション合計", "訪問者数"}
)

_PROFILE_ENV = "KURIMA_ACCESS_ANALYTICS_YAHOO_CHROME_PROFILE"
_STORE_ENV = "KURIMA_YAHOO_STORE_ACCOUNT"
_AUDIT_PATH = APP_ROOT / "logs" / "access_analytics" / "yahoo_access_reports.jsonl"

# 自動ログイン（2026-07-12 ユーザー許可により追加。Obsidianノートの既存方針
# 「初回・期限切れ時のログインは人が手動で行う」から本フローに限り意図的に逸脱する）。
# 環境変数は KURIMA_YAHOO_LOGIN_ID / KURIMA_YAHOO_LOGIN_PASSWORD
# （Yahoo! JAPAN ID。billing_statements_yahoo.py と共通。portal_tool/.env.example
# への追記を試みたが、本セッションのハーネスは .env* に一致するファイルへの
# Read/Bash/Edit を一律で拒否するため、実際には追記できていない。詳細は
# turn-000-report.md に記載）。値は関数ローカル変数としてのみ扱い、ログ・監査JSONL・
# manifest・例外メッセージ・戻り値のdataclassには一切含めない。
_LOGIN_ID_ENV = "KURIMA_YAHOO_LOGIN_ID"
_LOGIN_PASSWORD_ENV = "KURIMA_YAHOO_LOGIN_PASSWORD"
# 実フォームのセレクタ input[name="handle"] / input[name="password"] は
# 既存の稼働実績あり（日別売上集計データダウンロードプロジェクト
# `src/downloader/yahoo.py` を参照して2026-07-13に採用）。
_LOGIN_ID_SELECTORS = (
    'input[name="handle"]',
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[name*="id" i]',
    'input[name="login"]',
    'input#login_handle',
)
_LOGIN_PASSWORD_SELECTORS = ('input[name="password"]', 'input[type="password"]')
_LOGIN_SUBMIT_SELECTORS = ('button[type="submit"]', 'input[type="submit"]')
_TWO_FACTOR_MARKERS = (
    "認証コード",
    "確認コード",
    "ワンタイムパスワード",
    "二段階認証",
    "セキュリティコード",
    "verification code",
    # 2026-07-12実接続で確認: account.line.biz 経由のログインで
    # 「文字認証」（画像の文字を読み取って入力させるCAPTCHA）が表示される。
    "文字認証",
    "画像で認証",
    "音声で認証",
)


class YahooAccessAnalyticsError(RuntimeError):
    """利用者向けstateを保持するYahoo!アクセス解析取得エラー。"""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class YahooProductAccessResult:
    device: str
    period_start: date
    period_end: date
    downloaded_file: Path
    source_sha256: str
    row_count: int
    header_columns: tuple[str, ...]


@dataclass(frozen=True)
class YahooStoreOverallCsv:
    device: str
    period_start: date
    period_end: date
    downloaded_file: Path
    source_sha256: str
    row_count: int
    header_columns: tuple[str, ...]


@dataclass(frozen=True)
class YahooAccessAnalyticsResult:
    executed: bool
    period_start: date
    period_end: date
    product: YahooProductAccessResult | None
    overall: tuple[YahooStoreOverallCsv, ...]
    skipped_reason: str | None
    warnings: tuple[str, ...]


def _normalise_date(value: date | str, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} は YYYY-MM-DD で指定してください。") from exc


def _normalise_period(
    period_start: date | str, period_end: date | str
) -> tuple[date, date]:
    start = _normalise_date(period_start, field="period_start")
    end = _normalise_date(period_end, field="period_end")
    if start > end:
        raise ValueError("period_start は period_end 以下にしてください。")
    return start, end


def _profile_dir() -> Path:
    override = os.environ.get(_PROFILE_ENV, "").strip()
    return (
        Path(override)
        if override
        else APP_ROOT / "data" / "access_analytics_yahoo_chrome_profile"
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


def _store_account() -> str:
    value = os.environ.get(_STORE_ENV, "").strip()
    if not value:
        raise YahooAccessAnalyticsError(
            f"{_STORE_ENV} が設定されていません。",
            state="CONFIG_MISSING",
        )
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise YahooAccessAnalyticsError(
            f"{_STORE_ENV} の形式が不正です。",
            state="CONFIG_MISSING",
        )
    return value


def _account_fingerprint(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]


def _decode_cp932_csv(data: bytes) -> tuple[str, list[list[str]]]:
    prefix = data.lstrip()[:64].lower()
    if prefix.startswith(b"<") or b"doctype" in prefix:
        raise YahooAccessAnalyticsError(
            "CSVではなくHTMLログイン画面をダウンロードしました。",
            state="AUTH_REQUIRED",
        )
    try:
        text = data.decode("cp932", errors="strict")
    except UnicodeDecodeError as exc:
        raise YahooAccessAnalyticsError(
            "Yahoo! CSVをCP932として読めませんでした。",
            state="SCHEMA_MISMATCH",
        ) from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise YahooAccessAnalyticsError(
            "Yahoo! CSVの解析に失敗しました。",
            state="SCHEMA_MISMATCH",
        ) from exc
    if not rows:
        raise YahooAccessAnalyticsError(
            "Yahoo! CSVが空です。",
            state="DATA_NOT_UPDATED",
        )
    return text, rows


def validate_yahoo_product_csv(data: bytes) -> tuple[int, tuple[str, ...]]:
    """商品分析CSVの14列ヘッダーと全明細列数を検証する。"""

    _, rows = _decode_cp932_csv(data)
    header = tuple(rows[0])
    if header != EXPECTED_PRODUCT_HEADER_14:
        raise YahooAccessAnalyticsError(
            "Yahoo!商品分析CSVの14列ヘッダーが変更されています。",
            state="SCHEMA_MISMATCH",
        )
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    for row_number, row in enumerate(data_rows, start=2):
        if len(row) != len(EXPECTED_PRODUCT_HEADER_14):
            raise YahooAccessAnalyticsError(
                f"Yahoo!商品分析CSVの{row_number}行目が14列ではありません。",
                state="SCHEMA_MISMATCH",
            )
    return len(data_rows), header


def validate_yahoo_overall_csv(data: bytes) -> tuple[int, tuple[str, ...]]:
    """全体分析CSVを24列＋ノートに明記された基本4列で検証する。

    一次情報には24列の完全な列名一覧がないため、未観測の列名は捏造しない。
    """

    _, rows = _decode_cp932_csv(data)
    header = tuple(rows[0])
    if len(header) != 24 or not EXPECTED_OVERALL_BASE_COLUMNS.issubset(header):
        raise YahooAccessAnalyticsError(
            "Yahoo!全体分析CSVが24列ではないか、基本4列がありません。",
            state="SCHEMA_MISMATCH",
        )
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    for row_number, row in enumerate(data_rows, start=2):
        if len(row) != 24:
            raise YahooAccessAnalyticsError(
                f"Yahoo!全体分析CSVの{row_number}行目が24列ではありません。",
                state="SCHEMA_MISMATCH",
            )
    return len(data_rows), header


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_records() -> list[dict[str, object]]:
    manifest = find_access_analytics_paths().manifest_path
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _append_manifest(record: dict[str, object]) -> None:
    """manifest書込は集約層 access_analytics.append_access_analytics_manifest に一本化する。

    集約層（access_analytics.py）が本モジュールを import するため、
    ここでは遅延importで循環importを避ける。
    """
    from portal_app.services.access_analytics import append_access_analytics_manifest

    append_access_analytics_manifest(record)


def _append_audit(result: YahooAccessAnalyticsResult) -> None:
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, object]] = []
    if result.product:
        artifacts.append(
            {
                "type": "product",
                "device": result.product.device,
                "filename": result.product.downloaded_file.name,
                "sha256": result.product.source_sha256,
                "row_count": result.product.row_count,
            }
        )
    artifacts.extend(
        {
            "type": "overall",
            "device": item.device,
            "filename": item.downloaded_file.name,
            "sha256": item.source_sha256,
            "row_count": item.row_count,
        }
        for item in result.overall
    )
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "executed": result.executed,
        "target_label": f"{result.period_start.isoformat()}..{result.period_end.isoformat()}",
        "skipped_reason": result.skipped_reason,
        "warnings": list(result.warnings),
        "artifacts": artifacts,
    }
    with _AUDIT_PATH.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


async def _find_first_visible(page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    return None


async def _wait_first_visible(
    page, selectors: tuple[str, ...], *, timeout_ms: int, poll_ms: int = 500
):
    """ログイン画面はSPA描画で要素が遅れて現れるため、可視化をポーリングで待つ。"""
    waited = 0
    while True:
        candidate = await _find_first_visible(page, selectors)
        if candidate is not None:
            return candidate
        if waited >= timeout_ms:
            return None
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms


async def _wait_login_field(page, *, timeout_ms: int, poll_ms: int = 500):
    """ID欄またはパスワード欄の描画を待ち、先に現れた方を ("id"|"password", locator) で返す。

    ID記憶済み・再認証ページはID欄を出さずパスワード欄だけを表示するため
    どちらか一方を待つ。両方ないままtimeoutなら (None, None)。
    """
    waited = 0
    while True:
        id_field = await _find_first_visible(page, _LOGIN_ID_SELECTORS)
        if id_field is not None:
            return "id", id_field
        password_field = await _find_first_visible(page, _LOGIN_PASSWORD_SELECTORS)
        if password_field is not None:
            return "password", password_field
        if waited >= timeout_ms:
            return None, None
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms


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


async def _attempt_yahoo_login(page, *, target_url: str) -> None:
    """ログイン画面へリダイレクトされた場合にのみ呼ばれる自動ログインの試行。

    DOM構造はObsidianノートに定義がなく、本セッションのネットワーク到達性次第では
    実サイトを観測できないため、一般的なログインフォームパターン（email/id系入力・
    password系入力・submit系ボタン）で実装する。環境変数が未設定なら何もせず戻り、
    呼び出し元が従来どおりAUTH_REQUIREDを送出する。2段階認証等の追加認証を検知した
    場合は自動突破せず AUTH_REQUIRED_MANUAL で停止する
    （Obsidianノート「追加認証は人が行う」方針は維持）。
    """

    login_id = os.environ.get(_LOGIN_ID_ENV, "").strip()
    login_password = os.environ.get(_LOGIN_PASSWORD_ENV, "").strip()
    if not login_id or not login_password:
        return

    # ログインリダイレクト（pro.store → account.line.biz → login.yahoo.co.jp）と
    # ログイン画面のSPA描画は domcontentloaded より後に進むため、まず描画の
    # settle を待つ。2026-07-14の実行はこの待ちが無く、起動4秒でDOMを1回走査
    # しただけで「ID欄を特定できません」になった（稼働実績のある日別売上集計
    # `src/downloader/yahoo.py` はgoto直後に3秒待ってから判定している）。
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(3_000)

    # ストアクリエイターProの未ログインアクセスは account.line.biz の
    # 「LINEヤフーBusiness ID」ログイン方法選択画面へ着地する。
    # LINEアカウント／ビジネスアカウント／Yahoo! JAPAN ID／パスキー等が
    # 並ぶ選択画面なので、リンクの可視化を待ってから「Yahoo! JAPAN ID」を選ぶ
    # （2026-07-13、日別売上集計データダウンロードプロジェクト
    # `src/downloader/yahoo.py` の稼働実績を移植）。
    if "account.line.biz" in page.url:
        method_link = page.locator(':is(a, button):has-text("Yahoo! JAPAN ID")')
        try:
            await method_link.first.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            pass  # SSO自動通過等で選択画面が表示されないまま遷移した場合
        else:
            await method_link.first.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(3_000)

    # cookie復元・SSOでここまでにストアへ戻れていればフォーム入力は不要
    # （認証成否の最終判定は呼び出し元の_assert_authenticatedが行う）。
    if "pro.store.yahoo.co.jp" in page.url and (
        await _find_first_visible(page, _LOGIN_ID_SELECTORS) is None
    ):
        return

    field_kind, field = await _wait_login_field(page, timeout_ms=15_000)
    if field_kind is None:
        if await _has_two_factor_prompt(page):
            raise YahooAccessAnalyticsError(
                "Yahoo!ログインで追加認証（文字認証等）が要求されました。人手でログインしてください。",
                state="AUTH_REQUIRED_MANUAL",
            )
        raise YahooAccessAnalyticsError(
            f"Yahoo!ログインフォームのID欄を特定できません（URL: {page.url}）。",
            state="AUTH_REQUIRED_MANUAL",
        )
    if field_kind == "id":
        await field.fill(login_id)
        if not await _click_login_submit(page):
            raise YahooAccessAnalyticsError(
                "Yahoo!ログインフォームの送信ボタンを特定できません。",
                state="AUTH_REQUIRED_MANUAL",
            )
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(3_000)
    # field_kind == "password" はID記憶済み・再認証ページ。そのまま下の
    # パスワード入力フローに合流する。

    # --- パスキー画面の回避（存在する場合のみ）: 「他の方法でログイン」→「パスワード」 ---
    password_field = await _wait_first_visible(
        page, _LOGIN_PASSWORD_SELECTORS, timeout_ms=5_000
    )
    if password_field is None:
        alt_login = page.get_by_text("他の方法でログイン", exact=False)
        if await alt_login.count() > 0:
            await alt_login.last.click()
            await page.wait_for_timeout(3_000)
            password_option = page.get_by_role("button", name="パスワード")
            if await password_option.count() > 0:
                await password_option.first.wait_for(state="visible", timeout=10_000)
                await password_option.first.click()
                await page.wait_for_timeout(3_000)
            password_field = await _wait_first_visible(
                page, _LOGIN_PASSWORD_SELECTORS, timeout_ms=10_000
            )

    if password_field is None:
        if await _has_two_factor_prompt(page):
            raise YahooAccessAnalyticsError(
                "Yahoo!ログインで追加認証が要求されました。人手でログインしてください。",
                state="AUTH_REQUIRED_MANUAL",
            )
        raise YahooAccessAnalyticsError(
            f"Yahoo!ログインフォームのパスワード欄を特定できません（URL: {page.url}）。",
            state="AUTH_REQUIRED_MANUAL",
        )

    await password_field.fill(login_password)
    if not await _click_login_submit(page):
        raise YahooAccessAnalyticsError(
            "Yahoo!ログインフォームの送信ボタンを特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await page.wait_for_load_state("domcontentloaded")

    if await _has_two_factor_prompt(page):
        raise YahooAccessAnalyticsError(
            "Yahoo!ログインで追加認証が要求されました。人手でログインしてください。",
            state="AUTH_REQUIRED_MANUAL",
        )

    await page.goto(target_url, wait_until="domcontentloaded")


async def _assert_authenticated(page, account: str) -> None:
    parsed = urlparse(page.url)
    expected_prefix = f"/pro.{account}/"
    if (
        parsed.hostname != "pro.store.yahoo.co.jp"
        or not parsed.path.startswith(expected_prefix)
        or "login" in parsed.path.lower()
    ):
        raise YahooAccessAnalyticsError(
            "Yahoo!ストアクリエイターProの認証が必要です。",
            state="AUTH_REQUIRED",
        )


async def _apply_period(page, start: date, end: date) -> None:
    # ストアクリエイターProの商品分析・全体分析には value="daily" のラジオは
    # 存在せず、日次の期間入力欄（#dailyInputDatepickerFrom / To）と
    # 適用ボタン（#dailyInputApplyButton）が直接置かれている
    # （2026-07-13、ログイン後の実画面で確認。Obsidianノートの観測済み識別子と一致）。
    # 「日次ラジオを先にcheckする」処理は楽天RMSのパターンの流用ミスだったため削除した。
    start_input = page.locator(DATE_FROM_SELECTOR)
    end_input = page.locator(DATE_TO_SELECTOR)
    apply_button = page.locator(DATE_APPLY_SELECTOR)
    if not await start_input.count() or not await end_input.count() or not await apply_button.count():
        raise YahooAccessAnalyticsError(
            "Yahoo!日次期間入力のDOM契約を確認できません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    await start_input.fill(start.strftime("%Y/%m/%d"))
    await end_input.fill(end.strftime("%Y/%m/%d"))
    await apply_button.click()
    await page.wait_for_load_state("domcontentloaded")


async def _capture_download(page, click_locator, destination: Path) -> Path:
    try:
        async with page.expect_download(timeout=60_000) as download_info:
            await click_locator.click()
        download = await download_info.value
        failure = await download.failure()
        if failure:
            raise YahooAccessAnalyticsError(
                "Yahoo! CSVのダウンロードに失敗しました。",
                state="DOWNLOAD_FAILED",
            )
        await download.save_as(str(destination))
    except PlaywrightTimeoutError as exc:
        raise YahooAccessAnalyticsError(
            "Yahoo! CSVのダウンロード開始を確認できませんでした。",
            state="DOWNLOAD_FAILED",
        ) from None
    return destination


async def _select_device(page, selector: str, hidden_value: str) -> None:
    button = page.locator(selector)
    if await button.count() == 0:
        raise YahooAccessAnalyticsError(
            f"Yahoo!端末ボタン {selector} が見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    target = button.first
    # 実DOMのデバイスボタンは <span class="buttons-device_pc">PC</span> のような
    # 単純なspanで、hidden inputを内包していない（2026-07-13、実画面で確認）。
    # 以前のhidden value検証は誤った前提だったため、ボタンの表示ラベルで
    # 対象端末が正しいことを確認する方式に変更した。
    label = (await target.inner_text()).strip()
    expected_labels = {
        "pc": "PC",
        "sp": "スマホ",
        "app": "アプリ",
        "all": "合算値",
    }
    expected = expected_labels.get(hidden_value)
    if expected is not None and expected not in label:
        raise YahooAccessAnalyticsError(
            f"Yahoo!端末ボタン {selector} のラベルが期待値と一致しません"
            f"（期待: {expected} / 実際: {label}）。",
            state="PAGE_CONTRACT_CHANGED",
        )
    selected_tokens = ("active", "is-active", "selected", "current")
    table = page.locator("table").first
    before_table = await table.inner_text() if await table.count() else ""
    await target.click()
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        class_name = (await target.get_attribute("class") or "").lower()
        is_selected = any(token in class_name.split() for token in selected_tokens)
        current_table = await table.inner_text() if await table.count() else ""
        # 端末間で同値／0件なら表本文は変わらないため、active状態だけでも確定できる。
        if is_selected or current_table != before_table:
            return
        await asyncio.sleep(0.2)
    raise YahooAccessAnalyticsError(
        f"Yahoo!端末選択 {hidden_value} の反映を確認できません。",
        state="PAGE_CONTRACT_CHANGED",
    )


def _quarantine(path: Path, *, label: str) -> None:
    if not path.exists():
        return
    directory = find_access_analytics_paths().root / "quarantine"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.move(
        str(path),
        directory / f"{datetime.now():%Y%m%dT%H%M%S}_{label}_{path.name}",
    )


def _destination_for(
    *,
    category: str,
    device: str,
    start: date,
    end: date,
) -> Path:
    paths = find_access_analytics_paths()
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    if category == "product":
        name = (
            "yahoo_product_access_device-unspecified_"
            f"{start:%Y%m%d}_{end:%Y%m%d}.csv"
        )
    else:
        name = f"yahoo_store_overall_{device}_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    return paths.raw_dir / name


def _commit_file(
    temporary: Path,
    *,
    category: str,
    device: str,
    device_label: str,
    start: date,
    end: date,
    batch_id: str,
) -> YahooProductAccessResult | YahooStoreOverallCsv:
    data = temporary.read_bytes()
    try:
        if category == "product":
            row_count, header = validate_yahoo_product_csv(data)
        else:
            row_count, header = validate_yahoo_overall_csv(data)
    except Exception:
        _quarantine(temporary, label=f"{category}_{device}")
        raise

    paths = find_access_analytics_paths()
    destination = _destination_for(
        category=category,
        device=device,
        start=start,
        end=end,
    )
    sha256 = hashlib.sha256(data).hexdigest()
    if destination.exists() and _sha256(destination) == sha256:
        temporary.unlink(missing_ok=True)
    else:
        if destination.exists():
            destination = destination.with_name(
                f"{destination.stem}_{datetime.now():%H%M%S}{destination.suffix}"
            )
        temporary.replace(destination)

    _append_manifest(
        {
            "artifact_id": uuid4().hex,
            "batch_id": batch_id,
            "mall": "yahoo",
            "category": category,
            "filename": destination.name,
            "relative_path": destination.resolve().relative_to(paths.root.resolve()).as_posix(),
            "sha256": sha256,
            "row_count": row_count,
            "device": device,
            "device_label": device_label,
            "account_fingerprint": _account_fingerprint(_store_account()),
            "target_label": f"{start.isoformat()}..{end.isoformat()}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    common = {
        "device": device,
        "period_start": start,
        "period_end": end,
        "downloaded_file": destination,
        "source_sha256": sha256,
        "row_count": row_count,
        "header_columns": header,
    }
    if category == "product":
        return YahooProductAccessResult(**common)
    return YahooStoreOverallCsv(**common)


def _safe_manifest_path(record: dict[str, object]) -> Path | None:
    paths = find_access_analytics_paths()
    candidate = (paths.root / str(record.get("relative_path", ""))).resolve()
    try:
        candidate.relative_to(paths.root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _read_saved_result(
    start: date,
    end: date,
    *,
    account_fingerprint: str,
) -> YahooAccessAnalyticsResult:
    target_label = f"{start.isoformat()}..{end.isoformat()}"
    records = [
        record
        for record in _manifest_records()
        if record.get("mall") == "yahoo"
        and record.get("target_label") == target_label
        and record.get("account_fingerprint") == account_fingerprint
        # batch_complete マーカーは帳票レコードではない（relative_path=None /
        # device=None）。category で絞らないとマーカー自身が (category, device)
        # = ("batch_complete", "None") として候補に混入し、
        # 「batch_complete/None の保存済みファイルが見つかりません。」という
        # 実体のない警告を毎回出す（BillPayの0件バグと同一のバグパターン）。
        and record.get("category") in {"product", "overall"}
    ]
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        latest[(str(record.get("category")), str(record.get("device")))] = record
    warnings: list[str] = []
    product: YahooProductAccessResult | None = None
    overall: list[YahooStoreOverallCsv] = []
    for (category, device), record in latest.items():
        path = _safe_manifest_path(record)
        if path is None:
            warnings.append(f"{category}/{device} の保存済みファイルが見つかりません。")
            continue
        data = path.read_bytes()
        if category == "product":
            row_count, header = validate_yahoo_product_csv(data)
        elif category == "overall":
            row_count, header = validate_yahoo_overall_csv(data)
        else:
            continue
        sha256 = hashlib.sha256(data).hexdigest()
        if record.get("sha256") and record.get("sha256") != sha256:
            warnings.append(f"{category}/{device} のSHA-256がmanifestと一致しません。")
            continue
        if category == "product":
            product = YahooProductAccessResult(
                device="unspecified",
                period_start=start,
                period_end=end,
                downloaded_file=path,
                source_sha256=sha256,
                row_count=row_count,
                header_columns=header,
            )
        else:
            overall.append(
                YahooStoreOverallCsv(
                    device=device,
                    period_start=start,
                    period_end=end,
                    downloaded_file=path,
                    source_sha256=sha256,
                    row_count=row_count,
                    header_columns=header,
                )
            )
    result = YahooAccessAnalyticsResult(
        executed=False,
        period_start=start,
        period_end=end,
        product=product,
        overall=tuple(overall),
        skipped_reason="dry-run: 保存済みmanifestとCSVのみ検証しました。",
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


async def download_yahoo_access_reports(
    *,
    execute: bool,
    period_start: date | str,
    period_end: date | str,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YahooAccessAnalyticsResult:
    """商品分析1件とYahoo!公式の端末別全体分析4件を取得する。"""

    start, end = _normalise_period(period_start, period_end)
    account = _store_account()
    account_fingerprint = _account_fingerprint(account)
    if not execute:
        return _read_saved_result(
            start,
            end,
            account_fingerprint=account_fingerprint,
        )

    base_url = YAHOO_PRO_BASE.format(store_account=account)
    paths = find_access_analytics_paths()
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    batch_id = uuid4().hex
    batch_dir = paths.staging_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    product_result: YahooProductAccessResult | None = None
    overall_results: list[YahooStoreOverallCsv] = []

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                accept_downloads=True,
                downloads_path=str(batch_dir),
                headless=_headless_value(headless),
                locale="ja-JP",
                slow_mo=max(0, int(slow_mo_ms)),
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(base_url + ITEM_REPORT_PATH, wait_until="domcontentloaded")
                try:
                    await _assert_authenticated(page, account)
                except YahooAccessAnalyticsError as exc:
                    if exc.state != "AUTH_REQUIRED":
                        raise
                    # ログイン画面へリダイレクトされた場合のみ自動ログインを試みる
                    # （環境変数未設定なら_attempt_yahoo_loginは何もせず戻り、
                    # 直後のassertが従来どおりAUTH_REQUIREDを送出する）。
                    await _attempt_yahoo_login(page, target_url=base_url + ITEM_REPORT_PATH)
                    await _assert_authenticated(page, account)
                await _apply_period(page, start, end)
                await _assert_authenticated(page, account)
                product_button = page.locator(PRODUCT_DOWNLOAD_SELECTOR)
                if await product_button.count() == 0:
                    raise YahooAccessAnalyticsError(
                        "Yahoo!商品分析CSVボタンが見つかりません。",
                        state="PAGE_CONTRACT_CHANGED",
                    )
                product_tmp = batch_dir / f"product_{uuid4().hex}.csv"
                await _capture_download(page, product_button.first, product_tmp)
                committed_product = _commit_file(
                    product_tmp,
                    category="product",
                    device="unspecified",
                    device_label="商品分析（端末指定なし）",
                    start=start,
                    end=end,
                    batch_id=batch_id,
                )
                if not isinstance(committed_product, YahooProductAccessResult):
                    raise AssertionError("商品分析の結果型が不正です。")
                product_result = committed_product

                await page.goto(base_url + OVERALL_PATH, wait_until="domcontentloaded")
                await _assert_authenticated(page, account)
                await _apply_period(page, start, end)
                for selector, hidden_value, device, label in DEVICE_BUTTONS:
                    await _select_device(page, selector, hidden_value)
                    await _assert_authenticated(page, account)
                    download_button = page.locator(OVERALL_DOWNLOAD_SELECTOR)
                    if await download_button.count() == 0:
                        raise YahooAccessAnalyticsError(
                            "Yahoo!全体分析CSVボタンが見つかりません。",
                            state="PAGE_CONTRACT_CHANGED",
                        )
                    temporary = batch_dir / f"overall_{device}_{uuid4().hex}.csv"
                    await _capture_download(page, download_button.first, temporary)
                    committed = _commit_file(
                        temporary,
                        category="overall",
                        device=device,
                        device_label=label,
                        start=start,
                        end=end,
                        batch_id=batch_id,
                    )
                    if not isinstance(committed, YahooStoreOverallCsv):
                        raise AssertionError("全体分析の結果型が不正です。")
                    overall_results.append(committed)
            finally:
                await context.close()
    except YahooAccessAnalyticsError:
        raise
    except PlaywrightTimeoutError as exc:
        raise YahooAccessAnalyticsError(
            "Yahoo!ストアクリエイターProの応答待ちがタイムアウトしました。",
            state="PAGE_CONTRACT_CHANGED",
        ) from None
    except Exception:
        raise YahooAccessAnalyticsError(
            "Yahoo!ストアクリエイターProの自動操作に失敗しました。",
            state="PAGE_CONTRACT_CHANGED",
        ) from None
    finally:
        if batch_dir.exists() and not any(batch_dir.iterdir()):
            batch_dir.rmdir()

    _append_manifest(
        {
            "artifact_id": None,
            "batch_id": batch_id,
            "mall": "yahoo",
            "category": "batch_complete",
            "filename": None,
            "relative_path": None,
            "sha256": None,
            "row_count": (
                (product_result.row_count if product_result else 0)
                + sum(item.row_count for item in overall_results)
            ),
            "device": None,
            "device_label": None,
            "account_fingerprint": account_fingerprint,
            "target_label": f"{start.isoformat()}..{end.isoformat()}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    result = YahooAccessAnalyticsResult(
        executed=True,
        period_start=start,
        period_end=end,
        product=product_result,
        overall=tuple(overall_results),
        skipped_reason=None,
        warnings=tuple(),
    )
    _append_audit(result)
    return result


def download_yahoo_access_reports_sync(
    *,
    execute: bool,
    period_start: date | str,
    period_end: date | str,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YahooAccessAnalyticsResult:
    return asyncio.run(
        download_yahoo_access_reports(
            execute=execute,
            period_start=period_start,
            period_end=period_end,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )
