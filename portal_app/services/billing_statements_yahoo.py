"""Yahoo!の精算・請求・受取明細CSVを取得する。

一次情報: Obsidian「精算・請求・受取明細取得手順.md」
および「Playwright実装仕様.md」（2026-07-12 観測）。
実サイトへの接続・ログインは未検証（認証情報なし）。DOM操作はノートの
観測済み契約を転記した実装であり、初回実行時に実地検証が必要。

自動ログイン（2026-07-12 ユーザー許可により追加）: ログイン画面への
リダイレクトを検知した場合のみ、環境変数 KURIMA_YAHOO_LOGIN_ID /
KURIMA_YAHOO_LOGIN_PASSWORD が設定されていればPlaywrightでログインを
試みる（`_attempt_yahoo_login`。access_analytics_yahoo.py と同じ資格情報を
使う）。ログインフォームのDOM構造は未観測のため一般的なパターンで実装しており、
初回実行時の実地検証が必要。追加認証（2段階認証等）は自動突破せず
AUTH_REQUIRED_MANUAL で停止する。
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
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from portal_app.services.execution_logger import APP_ROOT
from portal_app.services.paths import find_billing_statements_paths


YAHOO_PRO_BASE = "https://pro.store.yahoo.co.jp/pro.{store_account}"
CLEARING_PATH = "/amount/clearing?targetYm={ym}"
DEMAND_PATH = "/amount/demand?targetYm={ym}"
RECEIVE_PATH = "/amount/receive?targetYm={ym}"
EXPECTED_BILLING_RECEIPT_HEADER_7 = (
    "利用日",
    "注文ID",
    "利用項目",
    "備考",
    "金額（税抜き）",
    "消費税",
    "金額（税込）",
)
EXPECTED_SETTLEMENT_HEADER_10 = (
    "利用日",
    "注文ID",
    "利用項目(請求)",
    "金額(請求：税抜き)",
    "消費税",
    "金額(請求：税込)",
    "利用項目(受取)",
    "金額(受取：税抜き)",
    "消費税",
    "金額(受取：税込)",
)
STATEMENT_PATHS = {
    "settlement": CLEARING_PATH,
    "billing": DEMAND_PATH,
    "receipt": RECEIVE_PATH,
}

_STORE_ENV = "KURIMA_YAHOO_STORE_ACCOUNT"
_PROFILE_ENV = "KURIMA_BILLING_STATEMENTS_YAHOO_CHROME_PROFILE"
_AUDIT_PATH = APP_ROOT / "logs" / "billing_statements" / "yahoo_statements.jsonl"
_NO_DATA_MARKERS = (
    "対象データはありません",
    "該当するデータがありません",
    "利用明細はありません",
    "データがありません",
    "0件",
)

# 自動ログイン（2026-07-12 ユーザー許可により追加。Obsidianノートの既存方針
# 「初回・期限切れ時のログインは人が手動で行う」から本フローに限り意図的に逸脱する）。
# 環境変数は KURIMA_YAHOO_LOGIN_ID / KURIMA_YAHOO_LOGIN_PASSWORD
# （Yahoo! JAPAN ID。access_analytics_yahoo.py と共通。portal_tool/.env.example
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


class YahooBillingStatementsError(RuntimeError):
    """利用者向けstateを保持するYahoo!請求関連取得エラー。"""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class YahooStatementFile:
    statement_type: str
    target_month: str
    settlement_closing_date: str | None
    statement_state: str
    downloaded_file: Path
    source_sha256: str
    row_count: int


@dataclass(frozen=True)
class YahooBillingStatementsResult:
    executed: bool
    target_month: str
    statement_state: str
    files: tuple[YahooStatementFile, ...]
    skipped_reason: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _PendingStatement:
    statement_type: str
    state: str
    closing_date: str | None
    temporary: Path
    source_name: str
    row_count: int


def classify_statement_state(status_text: str) -> str:
    if "未確定" in status_text:  # 「未確定」に「確定」が含まれるため必ず先に判定
        return "provisional"
    if status_text.strip() == "確定":
        return "final"
    return "unknown"


def _normalise_target_month(value: str | date) -> tuple[str, str]:
    if isinstance(value, date):
        compact = value.strftime("%Y%m")
    else:
        raw = str(value).strip()
        match = re.fullmatch(r"(20[0-9]{2})(?:[-/]?)([0-9]{2})", raw)
        if not match:
            raise ValueError("target_month は YYYY-MM で指定してください。")
        compact = "".join(match.groups())
    try:
        date(int(compact[:4]), int(compact[4:]), 1)
    except ValueError as exc:
        raise ValueError("target_month は YYYY-MM で指定してください。") from exc
    if not re.fullmatch(r"20[0-9]{4}", compact):
        raise ValueError("target_month は YYYY-MM で指定してください。")
    return compact, f"{compact[:4]}-{compact[4:]}"


def _normalise_types(types: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(str(value).strip() for value in types if str(value).strip()))
    if not requested:
        requested = ("billing", "receipt", "settlement")
    invalid = sorted(set(requested) - set(STATEMENT_PATHS))
    if invalid:
        raise ValueError(f"未対応の明細種別です: {', '.join(invalid)}")
    return requested


def _profile_dir() -> Path:
    override = os.environ.get(_PROFILE_ENV, "").strip()
    return (
        Path(override)
        if override
        else APP_ROOT / "data" / "billing_statements_yahoo_chrome_profile"
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
    account = os.environ.get(_STORE_ENV, "").strip()
    if not account or not re.fullmatch(r"[A-Za-z0-9._-]+", account):
        raise YahooBillingStatementsError(
            f"{_STORE_ENV} が未設定または不正です。",
            state="CONFIG_MISSING",
        )
    return account


def _account_fingerprint(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]


def _decode_statement_csv(data: bytes) -> list[list[str]]:
    prefix = data.lstrip()[:64].lower()
    if prefix.startswith(b"<") or b"doctype" in prefix:
        raise YahooBillingStatementsError(
            "CSVではなくHTMLログイン画面を取得しました。",
            state="AUTH_REQUIRED",
        )
    if data.startswith(b"\xef\xbb\xbf"):
        raise YahooBillingStatementsError(
            "Yahoo!明細CSVに仕様外のBOMがあります。",
            state="SCHEMA_DRIFT",
        )
    remaining = data.replace(b"\r\n", b"")
    if b"\n" in remaining or b"\r" in remaining:
        raise YahooBillingStatementsError(
            "Yahoo!明細CSVの改行がCRLFではありません。",
            state="SCHEMA_DRIFT",
        )
    try:
        text = data.decode("cp932", errors="strict")
    except UnicodeDecodeError as exc:
        raise YahooBillingStatementsError(
            "Yahoo!明細CSVをCP932として読めませんでした。",
            state="SCHEMA_DRIFT",
        ) from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise YahooBillingStatementsError(
            "Yahoo!明細CSVの解析に失敗しました。",
            state="SCHEMA_DRIFT",
        ) from exc
    if not rows:
        raise YahooBillingStatementsError(
            "Yahoo!明細CSVが空です。",
            state="SCHEMA_DRIFT",
        )
    return rows


def validate_yahoo_statement_csv(
    data: bytes,
    *,
    statement_type: str,
    target_month: str,
) -> int:
    """明細CSVの符号化・列順・対象月を検証し、明細行数を返す。"""

    compact, _ = _normalise_target_month(target_month)
    rows = _decode_statement_csv(data)
    expected = (
        EXPECTED_SETTLEMENT_HEADER_10
        if statement_type == "settlement"
        else EXPECTED_BILLING_RECEIPT_HEADER_7
    )
    if tuple(rows[0]) != expected:
        raise YahooBillingStatementsError(
            f"Yahoo!{statement_type}明細CSVのヘッダーが変更されています。",
            state="SCHEMA_DRIFT",
        )
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    month_prefix = f"{compact[:4]}/{compact[4:]}/"
    for row_number, row in enumerate(data_rows, start=2):
        if len(row) != len(expected):
            raise YahooBillingStatementsError(
                f"Yahoo!明細CSVの{row_number}行目の列数が一致しません。",
                state="SCHEMA_DRIFT",
            )
        used_on = row[0].strip()
        if used_on and not used_on.startswith(month_prefix):
            raise YahooBillingStatementsError(
                "Yahoo!明細CSVに対象月外の利用日があります。",
                state="SCHEMA_DRIFT",
            )
        if statement_type == "settlement":
            # 「消費税」が2列あるため辞書化せず、請求税=index 4、受取税=index 8で読む。
            billing_tax_yen = row[4].strip()
            receipt_tax_yen = row[8].strip()
            for value in (billing_tax_yen, receipt_tax_yen):
                if value and not re.fullmatch(r"-?\d+", value.replace(",", "")):
                    raise YahooBillingStatementsError(
                        "Yahoo!精算CSVの消費税列が整数ではありません。",
                        state="SCHEMA_DRIFT",
                    )
    return len(data_rows)


def _manifest_records() -> list[dict[str, object]]:
    manifest = find_billing_statements_paths().manifest_path
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
    """manifest書込は集約層 billing_statements.append_billing_manifest に一本化する。

    集約層（billing_statements.py）が本モジュールを import するため、
    ここでは遅延importで循環importを避ける。
    """
    from portal_app.services.billing_statements import append_billing_manifest

    append_billing_manifest(record)


def _append_virtual_state(
    *,
    batch_id: str,
    statement_type: str,
    target_month: str,
    state: str,
    closing_date: str | None = None,
    pending_records: list[dict[str, object]] | None = None,
) -> None:
    record = {
        "artifact_id": None,
        "batch_id": batch_id,
        "mall": "yahoo",
        "category": "statement",
        "statement_type": statement_type,
        "account_fingerprint": _account_fingerprint(_store_account()),
        "target_label": target_month,
        "state": state,
        "closing_date_label": closing_date,
        "filename": None,
        "relative_path": None,
        "sha256": None,
        "row_count": 0 if state == "no_data" else None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    if pending_records is not None:
        pending_records.append(record)
    else:
        _append_manifest(record)


def _append_audit(result: YahooBillingStatementsResult) -> None:
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "executed": result.executed,
        "target_month": result.target_month,
        "statement_state": result.statement_state,
        "skipped_reason": result.skipped_reason,
        "warnings": list(result.warnings),
        "files": [
            {
                "statement_type": item.statement_type,
                "state": item.statement_state,
                "closing_date": item.settlement_closing_date,
                "filename": item.downloaded_file.name,
                "sha256": item.source_sha256,
                "row_count": item.row_count,
            }
            for item in result.files
        ],
    }
    with _AUDIT_PATH.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


async def _assert_authenticated(page, account: str) -> None:
    parsed = urlparse(page.url)
    if (
        parsed.hostname != "pro.store.yahoo.co.jp"
        or not parsed.path.startswith(f"/pro.{account}/")
        or "login" in parsed.path.lower()
    ):
        raise YahooBillingStatementsError(
            "Yahoo!ストアクリエイターProの認証が必要です。",
            state="AUTH_REQUIRED",
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
    # settle を待つ。2026-07-14のaccess_analytics_yahoo実行はこの待ちが無く、
    # 起動4秒でDOMを1回走査しただけで「ID欄を特定できません」になった
    # （稼働実績のある日別売上集計 `src/downloader/yahoo.py` はgoto直後に
    # 3秒待ってから判定している）。
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(3_000)

    # ストアクリエイターProの未ログインアクセスは account.line.biz の
    # 「LINEヤフーBusiness ID」ログイン方法選択画面へ着地する。リンクの
    # 可視化を待ってから「Yahoo! JAPAN ID」を選ぶ（2026-07-13、日別売上集計
    # データダウンロードプロジェクト `src/downloader/yahoo.py` の稼働実績を移植）。
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
            raise YahooBillingStatementsError(
                "Yahoo!ログインで追加認証（文字認証等）が要求されました。人手でログインしてください。",
                state="AUTH_REQUIRED_MANUAL",
            )
        raise YahooBillingStatementsError(
            f"Yahoo!ログインフォームのID欄を特定できません（URL: {page.url}）。",
            state="AUTH_REQUIRED_MANUAL",
        )
    if field_kind == "id":
        await field.fill(login_id)
        if not await _click_login_submit(page):
            raise YahooBillingStatementsError(
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
            raise YahooBillingStatementsError(
                "Yahoo!ログインで追加認証が要求されました。人手でログインしてください。",
                state="AUTH_REQUIRED_MANUAL",
            )
        raise YahooBillingStatementsError(
            f"Yahoo!ログインフォームのパスワード欄を特定できません（URL: {page.url}）。",
            state="AUTH_REQUIRED_MANUAL",
        )

    await password_field.fill(login_password)
    if not await _click_login_submit(page):
        raise YahooBillingStatementsError(
            "Yahoo!ログインフォームの送信ボタンを特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await page.wait_for_load_state("domcontentloaded")

    if await _has_two_factor_prompt(page):
        raise YahooBillingStatementsError(
            "Yahoo!ログインで追加認証が要求されました。人手でログインしてください。",
            state="AUTH_REQUIRED_MANUAL",
        )

    await page.goto(target_url, wait_until="domcontentloaded")


async def _goto_statement_page(page, url: str, *, account: str, ym: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await _assert_authenticated(page, account)
    except YahooBillingStatementsError as exc:
        if exc.state != "AUTH_REQUIRED":
            raise
        # ログイン画面へリダイレクトされた場合のみ自動ログインを試みる
        # （環境変数未設定なら_attempt_yahoo_loginは何もせず戻り、
        # 直後のassertが従来どおりAUTH_REQUIREDを送出する）。
        await _attempt_yahoo_login(page, target_url=url)
        await _assert_authenticated(page, account)
    heading = page.get_by_role("heading", name="利用明細", exact=True)
    if await heading.count() == 0:
        raise YahooBillingStatementsError(
            "「利用明細」見出しが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    month_select = page.locator('select[name="targetYm"]')
    if await month_select.count() == 0:
        raise YahooBillingStatementsError(
            "対象年月selectが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    option_values = await month_select.locator("option").evaluate_all(
        "(options) => options.map((option) => option.value)"
    )
    if ym not in option_values:
        raise YahooBillingStatementsError(
            "指定年月を選択できません。",
            state="MONTH_UNAVAILABLE",
        )
    await month_select.select_option(ym)
    await page.wait_for_load_state("domcontentloaded")
    year, month = ym[:4], str(int(ym[4:]))
    target_heading = page.get_by_role(
        "heading",
        name=f"利用年月：{year}年{month}月度",
        exact=True,
    )
    # select_option後の再描画は非同期で、domcontentloadedの完了だけでは
    # 見出しがまだ旧月のままのことがある（2026-07-13、実画面で確認）。
    # 見出しが要求月に切り替わるまで待つ。
    try:
        await target_heading.first.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError:
        raise YahooBillingStatementsError(
            "選択後の利用年月見出しが要求月と一致しません。",
            state="PAGE_CONTRACT_CHANGED",
        ) from None


async def _body_text(page) -> str:
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return ""


async def _statement_state(page) -> str:
    """画面上の「確定」「未確定」表示から確定状態を判定する。

    注意: 確定状態の表示は**精算明細（clearing）画面の締め日行にのみ存在する**。
    請求明細（demand）・受取明細（receive）の画面には「確定」「未確定」の
    テキストが一切なく（2026-07-13、実画面で確認: 該当要素0件）、
    この関数は常に unknown を返す。請求・受取の確定状態は
    `_settlement_month_state()` で精算明細画面から取得すること。
    """
    candidates = page.get_by_text(re.compile(r"^(?:未確定|確定)$"))
    # 状態テキストも描画完了後に現れるため、出現を待ってから読む
    # （待たずに数えると0件になり、確定済みでもunknown扱いになる）。
    try:
        await candidates.first.wait_for(state="attached", timeout=10_000)
    except PlaywrightTimeoutError:
        return "unknown"
    states: list[str] = []
    for index in range(await candidates.count()):
        text = (await candidates.nth(index).inner_text()).strip()
        states.append(classify_statement_state(text))
    if "provisional" in states:
        return "provisional"
    if "unknown" in states:
        return "unknown"
    if "final" in states:
        return "final"
    return "unknown"


async def _settlement_month_state(page, *, account: str, ym: str) -> str:
    """精算明細画面を開いて、その月全体の確定状態を判定する。

    Obsidianノート「Yahoo!ショッピング-精算請求受取-Playwright実装仕様.md」の
    契約どおり、精算締め日行の状態を全件見て、1行でもprovisionalなら月も
    provisional、全行finalなら月もfinalとする。請求・受取明細の画面には
    確定状態表示がないため、その2帳票の確定判定にもこの月次状態を使う。
    """
    base_url = YAHOO_PRO_BASE.format(store_account=account)
    clearing_url = base_url + CLEARING_PATH.format(ym=ym)
    await _goto_statement_page(page, clearing_url, account=account, ym=ym)
    links = page.locator('a[href*="/amount/clearing/detailDay/"]')
    # 締め日リンクは描画完了後に現れるため、出現を待ってから数える。
    try:
        await links.first.wait_for(state="attached", timeout=15_000)
    except PlaywrightTimeoutError:
        return "unknown"
    if await links.count() == 0:
        return "unknown"
    return await _statement_state(page)


async def _has_explicit_no_data(page) -> bool:
    text = await _body_text(page)
    return any(marker in text for marker in _NO_DATA_MARKERS)


async def _displayed_count(page) -> int | None:
    matches = re.findall(r"(\d[\d,]*)\s*件", await _body_text(page))
    if not matches:
        return None
    return int(matches[-1].replace(",", ""))


async def _download_from_popup(
    page,
    *,
    batch_dir: Path,
    stem: str,
) -> tuple[list[tuple[Path, str]], int | None]:
    download_link = page.get_by_role("link", name="ダウンロード", exact=True)
    # 「ダウンロード」リンクは画面描画完了後に現れる。ページ遷移直後に数えると
    # 0件になり、実在するのに PAGE_CONTRACT_CHANGED になる
    # （2026-07-13、実画面で確認: 待てば1件存在する）。
    try:
        await download_link.first.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    if await download_link.count() == 0:
        if await _has_explicit_no_data(page):
            return [], 0
        raise YahooBillingStatementsError(
            "明細画面の「ダウンロード」リンクが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )

    popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=30_000))
    await download_link.first.click()
    try:
        landing = await popup_task
    except PlaywrightTimeoutError as exc:
        raise YahooBillingStatementsError(
            "明細ダウンロードpopupを確認できません。",
            state="PAGE_CONTRACT_CHANGED",
        ) from None
    try:
        await landing.wait_for_load_state("domcontentloaded")
        heading = landing.get_by_role("heading", name="利用詳細ダウンロード", exact=True)
        if await heading.count() == 0:
            raise YahooBillingStatementsError(
                "popupに「利用詳細ダウンロード」見出しがありません。",
                state="PAGE_CONTRACT_CHANGED",
            )
        displayed = await _displayed_count(landing)
        csv_links = landing.locator('a[onclick^="download_file"]')
        count = await csv_links.count()
        if count == 0:
            if await _has_explicit_no_data(landing):
                return [], 0
            raise YahooBillingStatementsError(
                "popupにCSVダウンロードリンクがありません。",
                state="PAGE_CONTRACT_CHANGED",
            )
        downloaded: list[tuple[Path, str]] = []
        for index in range(count):
            destination = batch_dir / f"{stem}_{index + 1}_{uuid4().hex}.csv"
            try:
                async with landing.expect_download(timeout=60_000) as download_info:
                    await csv_links.nth(index).click()
                download = await download_info.value
                failure = await download.failure()
                if failure:
                    raise YahooBillingStatementsError(
                        "Yahoo!明細CSVのダウンロードに失敗しました。",
                        state="DOWNLOAD_FAILED",
                    )
                await download.save_as(str(destination))
                downloaded.append((destination, download.suggested_filename))
            except PlaywrightTimeoutError as exc:
                raise YahooBillingStatementsError(
                    "Yahoo!明細CSVのダウンロード開始を確認できませんでした。",
                    state="DOWNLOAD_FAILED",
                ) from None
        return downloaded, displayed
    finally:
        await landing.close()


def _closing_date_from_url(url: str) -> str | None:
    match = re.search(r"/amount/clearing/detailDay/(\d{8})", url)
    if not match:
        return None
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def _check_displayed_count(
    *,
    statement_type: str,
    displayed: int | None,
    total_rows: int,
) -> None:
    if displayed is None:
        raise YahooBillingStatementsError(
            "明細ダウンロード画面の表示件数を確認できません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    expected = max(0, displayed - 1) if statement_type == "settlement" else displayed
    if total_rows != expected:
        raise YahooBillingStatementsError(
            "画面表示件数とCSV行数が一致しません。",
            state="SCHEMA_DRIFT",
        )


def _quarantine_batch(batch_dir: Path) -> None:
    if not batch_dir.exists() or not any(batch_dir.iterdir()):
        return
    paths = find_billing_statements_paths()
    paths.quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = paths.quarantine_dir / (
        f"yahoo_{datetime.now():%Y%m%dT%H%M%S}_{batch_dir.name}"
    )
    if destination.exists():
        destination = destination.with_name(f"{destination.name}_{uuid4().hex[:8]}")
    shutil.move(str(batch_dir), destination)


def _safe_source_name(statement_type: str, target: str, part: int) -> str:
    compact = target.replace("-", "")
    base = {
        "settlement": f"offset_{compact}",
        "billing": f"billing_{compact}",
        "receipt": f"receipt_{compact}",
    }[statement_type]
    suffix = f"_part{part}" if part > 1 else ""
    return f"{base}{suffix}.csv"


def _commit_pending(
    pending: _PendingStatement,
    *,
    target_month: str,
    batch_id: str,
    part: int,
    warnings: list[str],
) -> YahooStatementFile:
    paths = find_billing_statements_paths()
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256(pending.temporary.read_bytes()).hexdigest()
    logical = _safe_source_name(pending.statement_type, target_month, part)
    destination = paths.raw_dir / f"{pending.statement_type}_{pending.state}_{logical}"

    versioned_name = re.compile(
        rf"{re.escape(destination.stem)}_\d{{6}}_[0-9a-f]{{8}}"
        rf"{re.escape(destination.suffix)}"
    )
    previous: list[dict[str, object]] = []
    for record in _manifest_records():
        filename = record.get("filename")
        if (
            record.get("mall") != "yahoo"
            or record.get("statement_type") != pending.statement_type
            or record.get("target_label") != target_month
            or not isinstance(filename, str)
            or (
                filename != destination.name
                and versioned_name.fullmatch(filename) is None
            )
        ):
            continue
        previous.append(record)

    reusable: Path | None = None
    for record in reversed(previous):
        if record.get("sha256") != sha256:
            continue
        candidate = _safe_manifest_path(record)
        if candidate is None or candidate.name != record.get("filename"):
            continue
        try:
            candidate_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
        if candidate_sha256 == sha256:
            reusable = candidate
            break

    if reusable is not None:
        pending.temporary.unlink(missing_ok=True)
        destination = reusable
    elif destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == sha256:
        pending.temporary.unlink(missing_ok=True)
    else:
        if pending.state == "final" and any(
            record.get("sha256") and record.get("sha256") != sha256 for record in previous
        ):
            warnings.append(
                f"{pending.statement_type} の確定済み内容が前回から変化しました"
                "（FINAL_CONTENT_CHANGED）。"
            )
        if destination.exists():
            destination = destination.with_name(
                f"{destination.stem}_{datetime.now():%H%M%S}_{uuid4().hex[:8]}"
                f"{destination.suffix}"
            )
        pending.temporary.replace(destination)

    _append_manifest(
        {
            "artifact_id": uuid4().hex,
            "batch_id": batch_id,
            "mall": "yahoo",
            "category": "statement",
            "statement_type": pending.statement_type,
            "account_fingerprint": _account_fingerprint(_store_account()),
            "target_label": target_month,
            "state": pending.state,
            "closing_date_label": pending.closing_date,
            "filename": destination.name,
            "relative_path": destination.resolve().relative_to(paths.root.resolve()).as_posix(),
            "sha256": sha256,
            "row_count": pending.row_count,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return YahooStatementFile(
        statement_type=pending.statement_type,
        target_month=target_month,
        settlement_closing_date=pending.closing_date,
        statement_state=pending.state,
        downloaded_file=destination,
        source_sha256=sha256,
        row_count=pending.row_count,
    )


def _overall_state(states: list[str]) -> str:
    if "provisional" in states:
        return "provisional"
    if "unknown" in states:
        return "unknown"
    if "final" in states:
        return "final"
    return "no_data"


def _safe_manifest_path(record: dict[str, object]) -> Path | None:
    relative = record.get("relative_path")
    if not relative:
        return None
    paths = find_billing_statements_paths()
    candidate = (paths.root / str(relative)).resolve()
    try:
        candidate.relative_to(paths.root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _read_saved_result(
    *,
    target_month: str,
    requested_types: tuple[str, ...],
    account_fingerprint: str,
) -> YahooBillingStatementsResult:
    records = [
        record
        for record in _manifest_records()
        if record.get("mall") == "yahoo"
        and record.get("target_label") == target_month
        and record.get("statement_type") in requested_types
        and record.get("account_fingerprint") == account_fingerprint
    ]
    if records and records[-1].get("batch_id"):
        latest_batch_id = records[-1].get("batch_id")
        records = [
            record for record in records if record.get("batch_id") == latest_batch_id
        ]
    files: list[YahooStatementFile] = []
    states: list[str] = []
    warnings: list[str] = []
    for record in records:
        state = str(record.get("state") or "unknown")
        states.append(state)
        path = _safe_manifest_path(record)
        if path is None:
            if state not in {"provisional", "no_data"}:
                warnings.append("manifestに対応するYahoo!明細ファイルがありません。")
            continue
        data = path.read_bytes()
        statement_type = str(record.get("statement_type"))
        row_count = validate_yahoo_statement_csv(
            data,
            statement_type=statement_type,
            target_month=target_month,
        )
        sha256 = hashlib.sha256(data).hexdigest()
        if record.get("sha256") and record.get("sha256") != sha256:
            warnings.append("Yahoo!明細のSHA-256がmanifestと一致しません。")
            continue
        files.append(
            YahooStatementFile(
                statement_type=statement_type,
                target_month=target_month,
                settlement_closing_date=(
                    str(record.get("closing_date_label"))
                    if record.get("closing_date_label")
                    else None
                ),
                statement_state=state,
                downloaded_file=path,
                source_sha256=sha256,
                row_count=row_count,
            )
        )
    result = YahooBillingStatementsResult(
        executed=False,
        target_month=target_month,
        statement_state=_overall_state(states),
        files=tuple(files),
        skipped_reason="dry-run: 保存済みmanifestとCSVのみ検証しました。",
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


async def download_yahoo_statements(
    *,
    execute: bool,
    target_month: str | date,
    types: tuple[str, ...] = ("billing", "receipt", "settlement"),
    final_only: bool = True,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YahooBillingStatementsResult:
    """要求月のYahoo!請求・受取・精算CSVを確定状態を保ったまま取得する。"""

    ym, target_label = _normalise_target_month(target_month)
    requested_types = _normalise_types(types)
    account = _store_account()
    account_fingerprint = _account_fingerprint(account)
    if not execute:
        return _read_saved_result(
            target_month=target_label,
            requested_types=requested_types,
            account_fingerprint=account_fingerprint,
        )

    base_url = YAHOO_PRO_BASE.format(store_account=account)
    paths = find_billing_statements_paths()
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    batch_id = uuid4().hex
    batch_dir = paths.staging_dir / "_tmp" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    pending: list[_PendingStatement] = []
    virtual_records: list[dict[str, object]] = []
    warnings: list[str] = []
    states: list[str] = []
    halt_for_unknown_settlement = False

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
                for statement_type in requested_types:
                    url = base_url + STATEMENT_PATHS[statement_type].format(ym=ym)
                    await _goto_statement_page(page, url, account=account, ym=ym)
                    await _assert_authenticated(page, account)

                    if statement_type == "settlement":
                        detail_links = page.locator(
                            'a[href*="/amount/clearing/detailDay/"]'
                        )
                        # 締め日リンクは画面描画完了後に現れる。domcontentloaded直後に
                        # 数えると0件になり、確定済みの月でも誤ってunknown扱いになる
                        # （2026-07-13、実画面で確認: 待てば1件・ステータス「確定」）。
                        try:
                            await detail_links.first.wait_for(
                                state="attached", timeout=15_000
                            )
                        except PlaywrightTimeoutError:
                            pass
                        hrefs = [
                            href
                            for href in await detail_links.evaluate_all(
                                "(links) => links.map((link) => link.getAttribute('href'))"
                            )
                            if href
                        ]
                        if not hrefs:
                            if await _has_explicit_no_data(page):
                                states.append("no_data")
                                _append_virtual_state(
                                    batch_id=batch_id,
                                    statement_type=statement_type,
                                    target_month=target_label,
                                    state="no_data",
                                    pending_records=virtual_records,
                                )
                                continue
                            states.append("unknown")
                            warnings.append(
                                "精算締め日リンクが0件で、明示的なデータなし表示もないため"
                                "状態をunknownとして取得しませんでした。"
                            )
                            _append_virtual_state(
                                batch_id=batch_id,
                                statement_type=statement_type,
                                target_month=target_label,
                                state="unknown",
                                pending_records=virtual_records,
                            )
                            halt_for_unknown_settlement = True
                            break
                        for href_index, href in enumerate(hrefs, start=1):
                            if href.startswith("/amount/"):
                                detail_url = base_url + href
                            else:
                                detail_url = urljoin(base_url + "/", href)
                            parsed_detail = urlparse(detail_url)
                            if (
                                parsed_detail.hostname != "pro.store.yahoo.co.jp"
                                or not parsed_detail.path.startswith(f"/pro.{account}/")
                            ):
                                raise YahooBillingStatementsError(
                                    "精算締め日リンクがYahoo!対象ストア外を指しています。",
                                    state="PAGE_CONTRACT_CHANGED",
                                )
                            await page.goto(detail_url, wait_until="domcontentloaded")
                            await _assert_authenticated(page, account)
                            state = await _statement_state(page)
                            states.append(state)
                            closing_date = _closing_date_from_url(detail_url)
                            if final_only and state != "final":
                                warnings.append(
                                    f"精算 {closing_date or ''} は未確定のため取得しませんでした"
                                    "（NOT_FINALIZED）。"
                                )
                                _append_virtual_state(
                                    batch_id=batch_id,
                                    statement_type=statement_type,
                                    target_month=target_label,
                                    state=state,
                                    closing_date=closing_date,
                                    pending_records=virtual_records,
                                )
                                continue
                            downloads, displayed = await _download_from_popup(
                                page,
                                batch_dir=batch_dir,
                                stem=f"settlement_{href_index}",
                            )
                            if not downloads:
                                states[-1] = "no_data"
                                _append_virtual_state(
                                    batch_id=batch_id,
                                    statement_type=statement_type,
                                    target_month=target_label,
                                    state="no_data",
                                    closing_date=closing_date,
                                    pending_records=virtual_records,
                                )
                                continue
                            group_rows = 0
                            for part, (temporary, source_name) in enumerate(downloads, start=1):
                                row_count = validate_yahoo_statement_csv(
                                    temporary.read_bytes(),
                                    statement_type=statement_type,
                                    target_month=target_label,
                                )
                                group_rows += row_count
                                pending.append(
                                    _PendingStatement(
                                        statement_type=statement_type,
                                        state=state,
                                        closing_date=closing_date,
                                        temporary=temporary,
                                        source_name=source_name,
                                        row_count=row_count,
                                    )
                                )
                            _check_displayed_count(
                                statement_type=statement_type,
                                displayed=displayed,
                                total_rows=group_rows,
                            )
                        continue

                    # 請求（demand）・受取（receive）の画面には確定状態の表示が
                    # 存在しない（2026-07-13、実画面で確認）。この2帳票の確定判定は
                    # 精算明細画面の締め日行から取得した月次状態を使う。
                    # 画面遷移するので、データなし判定を先に済ませる。
                    explicit_no_data = await _has_explicit_no_data(page)
                    if explicit_no_data:
                        state = "no_data"
                    else:
                        state = await _settlement_month_state(page, account=account, ym=ym)
                        # 精算側から戻り、対象帳票の画面を開き直す。
                        await _goto_statement_page(page, url, account=account, ym=ym)
                    states.append(state)
                    if state == "no_data":
                        _append_virtual_state(
                            batch_id=batch_id,
                            statement_type=statement_type,
                            target_month=target_label,
                            state=state,
                            pending_records=virtual_records,
                        )
                        continue
                    if final_only and state != "final":
                        warnings.append(
                            f"{statement_type} は未確定のため取得しませんでした"
                            "（NOT_FINALIZED）。"
                        )
                        _append_virtual_state(
                            batch_id=batch_id,
                            statement_type=statement_type,
                            target_month=target_label,
                            state=state,
                            pending_records=virtual_records,
                        )
                        continue
                    downloads, displayed = await _download_from_popup(
                        page,
                        batch_dir=batch_dir,
                        stem=statement_type,
                    )
                    if not downloads:
                        states[-1] = "no_data"
                        _append_virtual_state(
                            batch_id=batch_id,
                            statement_type=statement_type,
                            target_month=target_label,
                            state="no_data",
                            pending_records=virtual_records,
                        )
                        continue
                    total_rows = 0
                    for temporary, source_name in downloads:
                        row_count = validate_yahoo_statement_csv(
                            temporary.read_bytes(),
                            statement_type=statement_type,
                            target_month=target_label,
                        )
                        total_rows += row_count
                        pending.append(
                            _PendingStatement(
                                statement_type=statement_type,
                                state=state,
                                closing_date=None,
                                temporary=temporary,
                                source_name=source_name,
                                row_count=row_count,
                            )
                        )
                    _check_displayed_count(
                        statement_type=statement_type,
                        displayed=displayed,
                        total_rows=total_rows,
                    )
            finally:
                await context.close()
    except YahooBillingStatementsError:
        _quarantine_batch(batch_dir)
        raise
    except Exception:
        _quarantine_batch(batch_dir)
        raise YahooBillingStatementsError(
            "Yahoo!請求関連画面の自動操作に失敗しました。",
            state="PAGE_CONTRACT_CHANGED",
        ) from None

    committed: list[YahooStatementFile] = []
    part_counts: dict[str, int] = {}
    if halt_for_unknown_settlement:
        _quarantine_batch(batch_dir)
    else:
        try:
            for item in pending:
                key = item.statement_type
                part_counts[key] = part_counts.get(key, 0) + 1
                committed.append(
                    _commit_pending(
                        item,
                        target_month=target_label,
                        batch_id=batch_id,
                        part=part_counts[key],
                        warnings=warnings,
                    )
                )
        except Exception:
            _quarantine_batch(batch_dir)
            raise
    for record in virtual_records:
        _append_manifest(record)
    _append_manifest(
        {
            "artifact_id": None,
            "batch_id": batch_id,
            "mall": "yahoo",
            "category": "batch_complete",
            "statement_type": None,
            "account_fingerprint": account_fingerprint,
            "target_label": target_label,
            "state": _overall_state(states),
            "closing_date_label": None,
            "filename": None,
            "relative_path": None,
            "sha256": None,
            "row_count": sum(item.row_count for item in committed),
            "warnings": list(warnings),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    if batch_dir.exists() and not any(batch_dir.iterdir()):
        batch_dir.rmdir()

    result = YahooBillingStatementsResult(
        executed=True,
        target_month=target_label,
        statement_state=_overall_state(states),
        files=tuple(committed),
        skipped_reason=(
            "精算状態を判定できないためバッチを安全停止し、取得済みCSVを隔離しました。"
            if halt_for_unknown_settlement
            else "確定済みのみの指定により未確定帳票を取得しませんでした。"
            if not committed
            and any(state in {"provisional", "unknown"} for state in states)
            else None
        ),
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


def download_yahoo_statements_sync(
    *,
    execute: bool,
    target_month: str | date,
    types: tuple[str, ...] = ("billing", "receipt", "settlement"),
    final_only: bool = True,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YahooBillingStatementsResult:
    return asyncio.run(
        download_yahoo_statements(
            execute=execute,
            target_month=target_month,
            types=types,
            final_only=final_only,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )
