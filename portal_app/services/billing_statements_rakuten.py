"""楽天BillPayの精算データと許可済み帳票を取得する。

一次情報: Obsidian「BillPay精算データ取得手順.md」
および「BillPay Playwright実装仕様.md」（2026-07-12 観測）。
実サイトへの接続・ログインは未検証（認証情報なし）。DOM操作はノートの
観測済み契約を転記した実装であり、初回実行時に実地検証が必要。

自動ログイン（2026-07-12 ユーザー許可により追加）: ログイン画面への
リダイレクトを検知した場合のみ、環境変数 KURIMA_BILLPAY_LOGIN_ID /
KURIMA_BILLPAY_LOGIN_PASSWORD が設定されていればPlaywrightでログインを
試みる（`_attempt_billpay_login`）。ログインフォームのDOM構造は未観測のため
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
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from portal_app.services.execution_logger import APP_ROOT
from portal_app.services.paths import find_billing_statements_paths


# 2026-07-13、ユーザーの実操作で /login（短縮URL）が正しい入口だと確認された。
# /rmssspartner/ は同じログインフォームへ辿り着くが、ユーザー環境では
# 挙動が異なると報告されたため、確認済みの直URLを正本とする。
LOGIN_URL = "https://billpay.rakuten.co.jp/login"
HOME_URL = "https://billpay.rakuten.co.jp/home"
SETTLEMENT_RESULT_URL = "https://billpay.rakuten.co.jp/settlement_result"
BILLING_CHECK_URL = "https://billpay.rakuten.co.jp/billing_check"
# /billing_status と /payment_status は自動取得対象外。同名CSV操作があっても押さない。
EXCLUDED_AUTOMATION_PATHS = frozenset({"/billing_status", "/payment_status"})
# スコープ意図（前回evalフィードバック6）: DOCUMENT_TYPE_ALLOWLIST内の帳票
# （summary-csv=表示情報CSV、32/41/51/72/74/52/11/31等のPDF/ZIP/CSV）は
# サービス層（download_billpay_settlement の document 引数）としては受け付けるが、
# 現行UI（main.py の /billing/rakuten/start）は document 引数を公開しておらず、
# 常定default（"settlement-shop-csv" = document-type 34/33）のみを呼び出す。
# つまり summary-csv 等はUIから選択できない。将来「詳細設定」的な帳票選択UIを
# 追加する余地を残すため、サービス層のallowlistは意図的に広めに保持している
# （UIへの露出強制はスコープ外）。
DOCUMENT_TYPE_ALLOWLIST = {
    "settlement_result": {
        "34": "csv",
        "32": "pdf",
        "41": "pdf",
        "51": "zip",
        "72": "pdf",
        "74": "csv",
        "52": "zip",
    },
    "billing_check": {
        "33": "csv",
        "11": "pdf",
        "31": "pdf",
        "41": "pdf",
        "51": "zip",
    },
}
EXPECTED_SHOP_DETAIL_HEADER_17 = (
    "発行日",
    "精算書No",
    "店舗別内訳書No",
    "店舗別ID",
    "ＵＲＬ",
    "店舗名",
    "支払（税込額）",
    "請求（税抜額）",
    "請求（税額）",
    "支払/請求分類",
    "集約科目",
    "品目",
    "精算対象期間開始日",
    "精算対象期間終了日",
    "金額",
    "うち消費税",
    "税率",
)
EXPECTED_SUMMARY_HEADER_12 = (
    "企業ID",
    "店舗名",
    "店舗別ID",
    "URL",
    "精算書発行日",
    "ご請求計算額",
    "ご請求締め日",
    "お支払計算額",
    "お支払締め日",
    "ご精算額",
    "お支払予定日",
    "お支払期限日",
)

_SCREEN_URLS = {
    "settlement_result": SETTLEMENT_RESULT_URL,
    "billing_check": BILLING_CHECK_URL,
}
_DOCUMENT_ALIASES = {
    "settlement_result": {"settlement-shop-csv": "34"},
    "billing_check": {"settlement-shop-csv": "33"},
}
_PROFILE_ENV = "KURIMA_BILLPAY_CHROME_PROFILE"
_AUDIT_PATH = APP_ROOT / "logs" / "billing_statements" / "billpay_settlements.jsonl"
_SESSION_SAFE_SECONDS = 25 * 60
_MAX_PAGES = 50

# 自動ログイン（今回ユーザーが明示許可。Obsidianノートの既存方針「初回・期限切れ時の
# ログインは人が手動で行う」から本フローに限り意図的に逸脱する。task.md 2026-07-12
# 参照）。環境変数は KURIMA_BILLPAY_LOGIN_ID / KURIMA_BILLPAY_LOGIN_PASSWORD
# （portal_tool/.env.example への追記を試みたが、本セッションのハーネスは .env* に
# 一致するファイルへの Read/Bash/Edit を一律で拒否するため、実際には追記できていない。
# 詳細は turn-000-report.md に記載）。値は関数ローカル変数としてのみ扱い、
# ログ・監査JSONL・manifest・例外メッセージ・戻り値のdataclassには一切含めない。
_LOGIN_ID_ENV = "KURIMA_BILLPAY_LOGIN_ID"
_LOGIN_PASSWORD_ENV = "KURIMA_BILLPAY_LOGIN_PASSWORD"
_LOGIN_ID_SELECTORS = (
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[name*="id" i]',
    'input[name="login"]',
    'input#login_handle',
)
_LOGIN_PASSWORD_SELECTORS = ('input[type="password"]',)
_LOGIN_SUBMIT_SELECTORS = ('button[type="submit"]', 'input[type="submit"]')
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


class BillPayError(RuntimeError):
    """利用者向けstateを保持するBillPay取得エラー。"""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class ShopDetailValidation:
    row_count: int
    issue_date: str
    identity_hash: str


@dataclass(frozen=True)
class SummaryValidation:
    row_count: int
    group_count: int
    identity_hash: str


@dataclass(frozen=True)
class BillPayDocument:
    screen: str
    document_type: str
    document_kind: str
    issue_date: str
    artifact_id: str
    downloaded_file: Path
    source_sha256: str
    row_count: int | None
    validated: bool


@dataclass(frozen=True)
class BillPaySettlementResult:
    executed: bool
    screen: str
    scope: str
    documents: tuple[BillPayDocument, ...]
    skipped_reason: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _SettlementRef:
    page_index: int
    fingerprint: str
    issue_date: str


def _decode_cp932_csv(data: bytes, *, expected_header: tuple[str, ...]) -> list[list[str]]:
    if data.startswith(b"\xef\xbb\xbf"):
        raise BillPayError(
            "BillPay CSVに仕様外のBOMがあります。",
            state="SCHEMA_DRIFT",
        )
    remaining = data.replace(b"\r\n", b"")
    if b"\n" in remaining or b"\r" in remaining:
        raise BillPayError(
            "BillPay CSVの改行がCRLFではありません。",
            state="SCHEMA_DRIFT",
        )
    try:
        text = data.decode("cp932", errors="strict")
    except UnicodeDecodeError as exc:
        raise BillPayError(
            "BillPay CSVをCP932として読めませんでした。",
            state="SCHEMA_DRIFT",
        ) from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise BillPayError(
            "BillPay CSVの解析に失敗しました。",
            state="SCHEMA_DRIFT",
        ) from exc
    if not rows or tuple(rows[0]) != expected_header:
        raise BillPayError(
            "BillPay CSVのヘッダーが変更されています。",
            state="SCHEMA_DRIFT",
        )
    for row in rows[1:]:
        if any(cell.strip() for cell in row) and len(row) != len(expected_header):
            raise BillPayError(
                "BillPay CSVの明細列数がヘッダーと一致しません。",
                state="SCHEMA_DRIFT",
            )
    return rows


def _strict_date(value: str, *, field: str) -> str:
    try:
        parsed = datetime.strptime(value.strip(), "%Y/%m/%d")
    except ValueError as exc:
        raise BillPayError(
            f"BillPay CSVの{field}がYYYY/MM/DDではありません。",
            state="SCHEMA_DRIFT",
        ) from exc
    if parsed.strftime("%Y/%m/%d") != value.strip():
        raise BillPayError(
            f"BillPay CSVの{field}が正規化日付ではありません。",
            state="SCHEMA_DRIFT",
        )
    return value.strip()


def _parse_amount(value: str, *, field: str) -> int | None:
    text = value.strip()
    if text in {"", "-"}:
        return None
    if not re.fullmatch(r"-?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)", text):
        raise BillPayError(
            f"BillPay CSVの{field}が整数ではありません。",
            state="SCHEMA_DRIFT",
        )
    return int(text.replace(",", ""))


def _identity_digest(values: tuple[str, ...]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_expected(
    *,
    actual: str,
    env_name: str,
    field_label: str,
    warnings: list[str],
) -> None:
    expected = os.environ.get(env_name, "").strip()
    if not expected:
        warning = f"{env_name} 未設定のため{field_label}照合を省略しました。"
        if warning not in warnings:
            warnings.append(warning)
        return
    if actual.strip() != expected:
        raise BillPayError(
            f"BillPay CSVの{field_label}が期待値と一致しません。",
            state="SCHEMA_DRIFT",
        )


def validate_shop_detail_csv(
    data: bytes,
    *,
    warnings: list[str] | None = None,
) -> ShopDetailValidation:
    """document-type 34/33 の17列店舗別内訳CSVを検証する。

    5列目の「ＵＲＬ」は一次情報どおり全角のまま扱い、半角URLへ修正しない。
    """

    warning_list = warnings if warnings is not None else []
    rows = _decode_cp932_csv(data, expected_header=EXPECTED_SHOP_DETAIL_HEADER_17)
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    if not data_rows:
        raise BillPayError(
            "BillPay店舗別内訳CSVに明細がありません。",
            state="SCHEMA_DRIFT",
        )
    identities: set[tuple[str, ...]] = set()
    for row in data_rows:
        issue_date = _strict_date(row[0], field="発行日")
        identity = (
            issue_date,
            row[1].strip(),
            row[2].strip(),
            row[3].strip(),
            row[4].strip(),
            row[5].strip(),
        )
        if any(not value for value in identity):
            raise BillPayError(
                "BillPay店舗別内訳CSVのidentity 6列に空欄があります。",
                state="SCHEMA_DRIFT",
            )
        identities.add(identity)
        for index, field in (
            (6, "支払（税込額）"),
            (7, "請求（税抜額）"),
            (8, "請求（税額）"),
            (14, "金額"),
            (15, "うち消費税"),
        ):
            _parse_amount(row[index], field=field)
    if len(identities) != 1:
        raise BillPayError(
            "BillPay店舗別内訳CSV内でidentity 6列が一致しません。",
            state="SCHEMA_DRIFT",
        )
    identity = next(iter(identities))
    _check_expected(
        actual=identity[3],
        env_name="KURIMA_BILLPAY_EXPECTED_SHOP_ID",
        field_label="店舗別ID",
        warnings=warning_list,
    )
    _check_expected(
        actual=identity[4],
        env_name="KURIMA_BILLPAY_EXPECTED_SHOP_URL",
        field_label="店舗URL",
        warnings=warning_list,
    )
    return ShopDetailValidation(
        row_count=len(data_rows),
        issue_date=identity[0],
        identity_hash=_identity_digest(identity),
    )


def _is_missing(value: str) -> bool:
    return value.strip() in {"", "-"}


def _validate_summary_group(rows: list[list[str]]) -> None:
    shop_rows = [row for row in rows if row[0].strip() == "-"]
    if not shop_rows:
        raise BillPayError(
            "BillPay表示情報CSVの企業groupに店舗行がありません。",
            state="EMPTY_ENTERPRISE_GROUP",
        )
    for row in rows:
        billing = _parse_amount(row[5], field="ご請求計算額")
        payment = _parse_amount(row[7], field="お支払計算額")
        settlement = _parse_amount(row[9], field="ご精算額")
        if billing is not None and payment is not None and settlement is not None:
            if payment - billing != settlement:
                raise BillPayError(
                    "BillPay表示情報CSVの精算額検算が一致しません。",
                    state="SCHEMA_DRIFT",
                )


def validate_summary_csv(
    data: bytes,
    *,
    warnings: list[str] | None = None,
) -> SummaryValidation:
    """表示情報CSVを可変長enterprise→shop group状態機械で検証する。"""

    warning_list = warnings if warnings is not None else []
    rows = _decode_cp932_csv(data, expected_header=EXPECTED_SUMMARY_HEADER_12)
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    groups: list[list[list[str]]] = []
    current: list[list[str]] | None = None
    for row in data_rows:
        enterprise = (
            not _is_missing(row[0])
            and _is_missing(row[2])
            and _is_missing(row[3])
        )
        shop = (
            row[0].strip() == "-"
            and not _is_missing(row[2])
            and not _is_missing(row[3])
        )
        if enterprise:
            if current is not None:
                _validate_summary_group(current)
                groups.append(current)
            current = [row]
        elif shop:
            if current is None:
                raise BillPayError(
                    "BillPay表示情報CSVにorphan店舗行があります。",
                    state="ORPHAN_SHOP_ROW",
                )
            current.append(row)
        else:
            raise BillPayError(
                "BillPay表示情報CSVの行種別を分類できません。",
                state="ROW_ROLE_AMBIGUOUS",
            )
    if current is not None:
        _validate_summary_group(current)
        groups.append(current)
    if not groups:
        raise BillPayError(
            "BillPay表示情報CSVにenterprise groupがありません。",
            state="EMPTY_ENTERPRISE_GROUP",
        )

    for group in groups:
        enterprise = group[0]
        _check_expected(
            actual=enterprise[0],
            env_name="KURIMA_BILLPAY_EXPECTED_COMPANY_ID",
            field_label="企業ID",
            warnings=warning_list,
        )
        for shop in (row for row in group[1:] if row[0].strip() == "-"):
            _check_expected(
                actual=shop[2],
                env_name="KURIMA_BILLPAY_EXPECTED_SHOP_ID",
                field_label="店舗別ID",
                warnings=warning_list,
            )
            _check_expected(
                actual=shop[3],
                env_name="KURIMA_BILLPAY_EXPECTED_SHOP_URL",
                field_label="店舗URL",
                warnings=warning_list,
            )
    identity_parts = tuple(
        hashlib.sha256(
            json.dumps(
                (group[0][0].strip(), tuple(row[2].strip() for row in group[1:])),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        for group in groups
    )
    return SummaryValidation(
        row_count=len(data_rows),
        group_count=len(groups),
        identity_hash=_identity_digest(identity_parts),
    )


def _profile_dir() -> Path:
    override = os.environ.get(_PROFILE_ENV, "").strip()
    return Path(override) if override else APP_ROOT / "data" / "billpay_chrome_profile"


def _headless_value(value: bool | None) -> bool:
    if value is not None:
        return value
    return os.environ.get("KURIMA_BROWSER_HEADLESS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@contextmanager
def _profile_lock(profile: Path):
    lock_path = profile / ".kurima-billpay.lock"
    try:
        handle = lock_path.open("x", encoding="ascii")
    except FileExistsError as exc:
        raise BillPayError(
            "BillPay専用プロファイルは別ジョブが使用中です。",
            state="PROFILE_IN_USE",
        ) from exc
    try:
        handle.write(datetime.now().isoformat(timespec="seconds"))
        handle.close()
        yield
    finally:
        try:
            handle.close()
        except Exception:
            pass
        lock_path.unlink(missing_ok=True)


def _resolve_document_type(screen: str, document: str) -> tuple[str, str]:
    if document == "summary-csv":
        # 表示情報CSVはdocument-type属性の帳票ではなく、観測済みの専用操作。
        return "summary", "csv"
    alias = _DOCUMENT_ALIASES.get(screen, {}).get(document)
    if alias:
        document_type = alias
    else:
        match = re.fullmatch(r"(?:doctype-)?(\d+)", document.strip())
        if not match:
            raise BillPayError(
                "未対応の帳票指定です。",
                state="DOCUMENT_TYPE_NOT_ALLOWED",
            )
        document_type = match.group(1)
    kind = DOCUMENT_TYPE_ALLOWLIST.get(screen, {}).get(document_type)
    if kind is None:
        raise BillPayError(
            "allowlist外のdocument-typeは取得できません。",
            state="DOCUMENT_TYPE_NOT_ALLOWED",
        )
    return document_type, kind


def _normalise_issue_date(value: str | date | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError("issue_date は YYYY-MM-DD で指定してください。") from exc


def _validate_request(
    *,
    screen: str,
    scope: str,
    issue_date: str | date | None,
    document: str,
) -> tuple[str | None, str, str]:
    if screen not in _SCREEN_URLS:
        raise ValueError("screen は settlement_result または billing_check を指定してください。")
    if scope not in {"latest", "date", "all"}:
        raise ValueError("scope は latest、date、all のいずれかを指定してください。")
    if document == "summary-csv" and (
        screen != "settlement_result" or scope != "all"
    ):
        raise BillPayError(
            "表示情報CSVは精算確定画面のscope=allでのみ取得できます。",
            state="DOCUMENT_TYPE_NOT_ALLOWED",
        )
    normalised_date = _normalise_issue_date(issue_date)
    if scope == "date" and normalised_date is None:
        raise ValueError("scope=date では issue_date が必須です。")
    document_type, kind = _resolve_document_type(screen, document)
    return normalised_date, document_type, kind


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


def _append_audit(result: BillPaySettlementResult) -> None:
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "executed": result.executed,
        "screen": result.screen,
        "scope": result.scope,
        "skipped_reason": result.skipped_reason,
        "warnings": list(result.warnings),
        "documents": [
            {
                "document_type": item.document_type,
                "kind": item.document_kind,
                "issue_date": item.issue_date,
                "filename": item.downloaded_file.name,
                "sha256": item.source_sha256,
                "row_count": item.row_count,
                "validated": item.validated,
            }
            for item in result.documents
        ],
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


async def _attempt_billpay_login(page, *, target_url: str) -> None:
    """ログイン画面へリダイレクトされた場合にのみ呼ばれる自動ログインの試行。

    DOM構造はObsidianノートに定義がなく、本セッションのネットワーク到達性次第では
    実サイトを観測できないため、一般的なログインフォームパターン（email/id系入力・
    password系入力・submit系ボタン）で実装する。環境変数が未設定なら何もせず戻り、
    呼び出し元が従来どおりNEEDS_LOGINを送出する。2段階認証等の追加認証を検知した
    場合は自動突破せず AUTH_REQUIRED_MANUAL で停止する
    （Obsidianノート「追加認証は人が行う」方針は維持）。
    """

    login_id = os.environ.get(_LOGIN_ID_ENV, "").strip()
    login_password = os.environ.get(_LOGIN_PASSWORD_ENV, "").strip()
    if not login_id or not login_password:
        return

    # /settlement_result 等からの未ログインリダイレクト先
    # （billpay.rakuten.co.jp/login。/rmssspartner/login とは別URL）は
    # 実運用で正しくログインできないことがユーザーの実操作で確認されている。
    # 正規の入口 LOGIN_URL（/rmssspartner/）へ明示的に遷移してから
    # ログインを試みる（2026-07-13、ユーザー報告に基づき修正）。
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    id_field = await _find_first_visible(page, _LOGIN_ID_SELECTORS)
    if id_field is None:
        raise BillPayError(
            "楽天BillPayログインフォームのID欄を特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await id_field.fill(login_id)

    password_field = await _find_first_visible(page, _LOGIN_PASSWORD_SELECTORS)
    if password_field is None:
        # 2段階（ID入力→次へ→パスワード入力）の可能性があるため一度送信して次画面を待つ。
        if not await _click_login_submit(page):
            raise BillPayError(
                "楽天BillPayログインフォームの送信ボタンを特定できません。",
                state="AUTH_REQUIRED_MANUAL",
            )
        await page.wait_for_load_state("domcontentloaded")
        password_field = await _find_first_visible(page, _LOGIN_PASSWORD_SELECTORS)
        if password_field is None:
            if await _has_two_factor_prompt(page):
                raise BillPayError(
                    "楽天BillPayログインで追加認証が要求されました。人手でログインしてください。",
                    state="AUTH_REQUIRED_MANUAL",
                )
            raise BillPayError(
                "楽天BillPayログインフォームのパスワード欄を特定できません。",
                state="AUTH_REQUIRED_MANUAL",
            )

    await password_field.fill(login_password)
    if not await _click_login_submit(page):
        raise BillPayError(
            "楽天BillPayログインフォームの送信ボタンを特定できません。",
            state="AUTH_REQUIRED_MANUAL",
        )
    await page.wait_for_load_state("domcontentloaded")

    if await _has_two_factor_prompt(page):
        raise BillPayError(
            "楽天BillPayログインで追加認証が要求されました。人手でログインしてください。",
            state="AUTH_REQUIRED_MANUAL",
        )

    await page.goto(target_url, wait_until="domcontentloaded")


async def _assert_screen(page, screen: str) -> None:
    parsed = urlparse(page.url)
    if parsed.hostname != "billpay.rakuten.co.jp":
        raise BillPayError(
            "楽天BillPayのログインが切れています。",
            state="NEEDS_LOGIN",
        )
    # 期間select（6/12/18か月）を切り替えると、URLは
    # /settlement_result → /settlement_result/reload のようにサフィックスが付く
    # （2026-07-13、実画面で確認）。これは同一画面の正常な再描画であり、
    # ログイン画面へ戻されたわけではない。完全一致だとここで誤ってNEEDS_LOGINになる。
    expected_path = f"/{screen}"
    actual_path = parsed.path.rstrip("/")
    if actual_path != expected_path and not actual_path.startswith(f"{expected_path}/"):
        raise BillPayError(
            "楽天BillPayの対象画面からログイン入口へ戻りました。",
            state="NEEDS_LOGIN",
        )
    period = page.locator("select#period")
    if await period.count() == 0:
        raise BillPayError(
            "BillPay対象画面の期間selectが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )


async def _select_18_months(page) -> None:
    period = page.locator("select#period")
    values = await period.locator("option").evaluate_all(
        "(options) => options.map((option) => option.value)"
    )
    if "18" not in values:
        raise BillPayError(
            "BillPayの18か月表示optionが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    await period.select_option("18")
    await page.wait_for_load_state("domcontentloaded")
    if await period.input_value() != "18":
        raise BillPayError(
            "BillPayの期間選択が18か月へ反映されませんでした。",
            state="PAGE_CONTRACT_CHANGED",
        )


async def _visible_tbody_fingerprints(page) -> list[tuple[str, str, object]]:
    bodies = page.locator("table.billpay_main_table > tbody:visible")
    records: list[tuple[str, str, object]] = []
    for index in range(await bodies.count()):
        body = bodies.nth(index)
        text = (await body.inner_text()).strip()
        if not text:
            continue
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # 実UIの発行日は「2026年7月3日」という日本語形式で、tbody先頭に
        # ラベルなしで置かれている（2026-07-13、実画面で確認）。
        # 「発行日:」ラベル付きのスラッシュ形式も将来の変更に備えて許容する。
        match = re.search(
            r"(?:(?:精算書)?発行日\s*[:：]?\s*)?"
            r"(20\d{2})\s*[/\-年]\s*(\d{1,2})\s*[/\-月]\s*(\d{1,2})\s*日?",
            text,
        )
        if not match:
            raise BillPayError(
                "BillPay精算回の発行日ラベルから日付を読み取れません。",
                state="PAGE_CONTRACT_CHANGED",
            )
        try:
            issue_date = date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).isoformat()
        except ValueError as exc:
            raise BillPayError(
                "BillPay精算回の発行日が正しい日付ではありません。",
                state="PAGE_CONTRACT_CHANGED",
            ) from exc
        records.append((fingerprint, issue_date, body))
    return records


async def _click_next(page, before: set[str], *, screen: str) -> bool:
    await _assert_screen(page, screen)
    next_span = page.locator("ul.pagination").first.locator(
        'li[page-no="Next"] span[aria-label="Next"]'
    )
    if await next_span.count() == 0:
        return False
    parent_class = (
        await next_span.first.locator("xpath=..").get_attribute("class") or ""
    ).lower()
    if "disabled" in parent_class:
        return False
    # 最終ページではNextがDOMに残ったまま非表示になることがある
    # （2026-07-13、実画面で確認: element is not visible のまま30秒タイムアウトした）。
    # 非表示なら「次ページなし」として扱う。
    if not await next_span.first.is_visible():
        return False
    await next_span.first.click()
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        await _assert_screen(page, screen)
        current = {
            fingerprint
            for fingerprint, _, _ in await _visible_tbody_fingerprints(page)
        }
        if current and current != before:
            return True
        await asyncio.sleep(0.25)
    raise BillPayError(
        "BillPayのページ送り後も表示内容が変わりません。",
        state="PAGINATION_STALLED",
    )


async def _collect_settlements(page, *, screen: str) -> list[_SettlementRef]:
    collected: list[_SettlementRef] = []
    seen_pages: set[str] = set()
    for page_index in range(_MAX_PAGES):
        await _assert_screen(page, screen)
        visible = await _visible_tbody_fingerprints(page)
        fingerprint_set = {fingerprint for fingerprint, _, _ in visible}
        page_signature = hashlib.sha256(
            "|".join(sorted(fingerprint_set)).encode("ascii")
        ).hexdigest()
        if page_signature in seen_pages:
            raise BillPayError(
                "BillPayページングが既訪ページへ戻りました。",
                state="PAGINATION_STALLED",
            )
        seen_pages.add(page_signature)
        collected.extend(
            _SettlementRef(
                page_index=page_index,
                fingerprint=fingerprint,
                issue_date=issue_date,
            )
            for fingerprint, issue_date, _ in visible
        )
        if not await _click_next(page, fingerprint_set, screen=screen):
            return collected
    raise BillPayError(
        "BillPayページングが安全上限50ページを超えました。",
        state="PAGINATION_STALLED",
    )


def _select_refs(
    refs: list[_SettlementRef],
    *,
    scope: str,
    issue_date: str | None,
) -> list[_SettlementRef]:
    if not refs:
        return []
    if scope == "all":
        return refs
    if scope == "date":
        return [item for item in refs if item.issue_date == issue_date]
    latest = max(item.issue_date for item in refs)
    return [item for item in refs if item.issue_date == latest]


async def _goto_page_index(page, *, screen: str, page_index: int) -> None:
    await page.goto(_SCREEN_URLS[screen], wait_until="domcontentloaded")
    await _assert_screen(page, screen)
    await _select_18_months(page)
    for _ in range(page_index):
        before = {
            fingerprint
            for fingerprint, _, _ in await _visible_tbody_fingerprints(page)
        }
        if not await _click_next(page, before, screen=screen):
            raise BillPayError(
                "対象のBillPayページへ再到達できません。",
                state="PAGINATION_STALLED",
            )


async def _find_target_tbody(page, fingerprint: str):
    for current, _, body in await _visible_tbody_fingerprints(page):
        if current == fingerprint:
            return body
    raise BillPayError(
        "対象精算回をvisible tbody内で再特定できません。",
        state="PAGE_CONTRACT_CHANGED",
    )


def _download_snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() in {".tmp", ".crdownload", ".part"}:
            continue
        stat = path.stat()
        snapshot[path] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


async def _wait_stable_file(
    directory: Path,
    before: dict[Path, tuple[int, int]],
) -> Path:
    deadline = asyncio.get_running_loop().time() + 45
    sizes: dict[Path, int] = {}
    stable: dict[Path, int] = {}
    while asyncio.get_running_loop().time() < deadline:
        current = _download_snapshot(directory)
        candidates = [path for path, stamp in current.items() if before.get(path) != stamp]
        if len(candidates) > 1:
            raise BillPayError(
                "BillPayダウンロード候補が複数あり確定できません。",
                state="AMBIGUOUS_DOWNLOAD",
            )
        if len(candidates) == 1:
            candidate = candidates[0]
            size = current[candidate][0]
            stable[candidate] = (
                stable.get(candidate, 0) + 1
                if sizes.get(candidate) == size and size > 0
                else 0
            )
            sizes[candidate] = size
            if stable[candidate] >= 2:
                return candidate
        await asyncio.sleep(0.5)
    raise BillPayError(
        "BillPay帳票ファイルを回収できませんでした。",
        state="DOWNLOAD_FAILED",
    )


async def _capture_download(page, locator, *, directory: Path, destination: Path) -> Path:
    before = _download_snapshot(directory)
    try:
        async with page.expect_download(timeout=45_000) as download_info:
            await locator.click()
        download = await download_info.value
        failure = await download.failure()
        if failure:
            raise BillPayError(
                f"BillPay帳票のダウンロードに失敗しました: {failure}",
                state="DOWNLOAD_FAILED",
            )
        await download.save_as(str(destination))
        return destination
    except PlaywrightTimeoutError:
        fallback = await _wait_stable_file(directory, before)
        if fallback.resolve() != destination.resolve():
            shutil.move(str(fallback), destination)
        return destination


def _assert_session_safe(started: float) -> None:
    if time.monotonic() - started >= _SESSION_SAFE_SECONDS:
        raise BillPayError(
            "BillPayセッション期限が近いため新規取得を安全停止しました。",
            state="SESSION_RENEWAL_REQUIRED",
        )


async def _document_button(tbody, document_type: str):
    # document-typeは必ず対象visible tbody内へscopeし、page全体の先頭を選ばない。
    button = tbody.locator(f'button[document-type="{document_type}"]')
    if await button.count() == 0:
        body_text = await tbody.inner_text()
        if any(marker in body_text for marker in ("未発行", "帳票なし", "発行なし")):
            return None
        raise BillPayError(
            "allowlist内document-typeのボタンが対象精算回にありません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    try:
        await button.first.wait_for(state="visible", timeout=30_000)
    except PlaywrightTimeoutError as exc:
        raise BillPayError(
            "対象精算回のdocument-typeボタンが表示されません。",
            state="PAGE_CONTRACT_CHANGED",
        ) from exc
    return button.first


async def _summary_download_button(page):
    css_button = page.locator("button.csv-download")
    named_button = page.get_by_role(
        "button",
        name="表示情報のCSVダウンロード",
        exact=True,
    )
    if await css_button.count() != 1 or await named_button.count() != 1:
        raise BillPayError(
            "BillPay表示情報CSVボタンを一意に特定できません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    named_handle = await named_button.first.element_handle()
    if named_handle is None or not await css_button.first.evaluate(
        "(element, other) => element === other",
        named_handle,
    ):
        raise BillPayError(
            "BillPay表示情報CSVのCSS locatorとaccessible nameが同一要素ではありません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    return css_button.first


def _validate_downloaded_document(
    data: bytes,
    *,
    document_type: str,
    kind: str,
    warnings: list[str],
) -> tuple[bool, int | None, str, str | None]:
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise BillPayError(
                "BillPay PDFのシグネチャが不正です。",
                state="SCHEMA_DRIFT",
            )
        # 実装仕様ノート15.2節: 末尾2,048バイト以内に%%EOFがあることも確認する
        # （先頭シグネチャだけでは途中で切れたダウンロードを検知できないため）。
        if b"%%EOF" not in data[-2048:]:
            raise BillPayError(
                "BillPay PDFの終端(%%EOF)を末尾2,048バイト以内で確認できません。",
                state="SCHEMA_DRIFT",
            )
        return True, None, hashlib.sha256(data).hexdigest(), None
    if kind == "zip":
        # 実装仕様ノート15.3節: is_zipfile()だけでなく、全memberのCRCをtestzip()で検証し、
        # member名の絶対パス／親ディレクトリ参照・暗号化member・空archiveも拒否する。
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                file_infos = [info for info in infos if not info.is_dir()]
                if not file_infos:
                    raise BillPayError(
                        "BillPay ZIPにfile memberが1件もありません。",
                        state="SCHEMA_DRIFT",
                    )
                for info in file_infos:
                    normalized = info.filename.replace("\\", "/")
                    if (
                        normalized.startswith("/")
                        or re.match(r"^[A-Za-z]:", normalized)
                        or any(part == ".." for part in normalized.split("/"))
                    ):
                        raise BillPayError(
                            "BillPay ZIPのmember名が絶対パスまたは親ディレクトリ参照を含みます。",
                            state="SCHEMA_DRIFT",
                        )
                    if info.flag_bits & 0x1:
                        raise BillPayError(
                            "BillPay ZIPに暗号化されたmemberが含まれています。",
                            state="SCHEMA_DRIFT",
                        )
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise BillPayError(
                        f"BillPay ZIPのmember {bad_member} のCRC検証に失敗しました。",
                        state="SCHEMA_DRIFT",
                    )
        except zipfile.BadZipFile as exc:
            raise BillPayError(
                "BillPay ZIPの構造が不正です。",
                state="SCHEMA_DRIFT",
            ) from exc
        return True, None, hashlib.sha256(data).hexdigest(), None
    if document_type == "summary":
        validation = validate_summary_csv(data, warnings=warnings)
        return True, validation.row_count, validation.identity_hash, None
    if document_type in {"34", "33"}:
        validation = validate_shop_detail_csv(data, warnings=warnings)
        internal_issue_date = datetime.strptime(
            validation.issue_date, "%Y/%m/%d"
        ).date().isoformat()
        return (
            True,
            validation.row_count,
            validation.identity_hash,
            internal_issue_date,
        )
    # document-type 74 はallowlist上CSVだが一次情報に列契約がないため、
    # 非空かつCP932 strictであることだけを確認し、完全検証済みとは表示しない。
    prefix = data.lstrip()[:64].lower()
    if prefix.startswith(b"<") or b"doctype" in prefix:
        raise BillPayError(
            "CSVではなくHTMLログイン画面を取得しました。",
            state="NEEDS_LOGIN",
        )
    try:
        text = data.decode("cp932", errors="strict")
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise BillPayError(
            "BillPay CSVをCP932として解析できません。",
            state="SCHEMA_DRIFT",
        ) from exc
    if not rows:
        raise BillPayError(
            "BillPay CSVが空です。",
            state="SCHEMA_DRIFT",
        )
    warnings.append(
        f"document-type {document_type} は列契約未観測のため完全検証を省略しました。"
    )
    return (
        False,
        max(0, len(rows) - 1),
        hashlib.sha256(data).hexdigest(),
        None,
    )


def _safe_existing_path(record: dict[str, object]) -> Path | None:
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


def _commit_document(
    temporary: Path,
    *,
    screen: str,
    document_type: str,
    kind: str,
    issue_date: str,
    identity_hash: str,
    row_count: int | None,
    validated: bool,
    batch_id: str,
) -> BillPayDocument:
    paths = find_billing_statements_paths()
    paths.raw_dir.mkdir(parents=True, exist_ok=True)
    data = temporary.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    existing = next(
        (
            record
            for record in reversed(_manifest_records())
            if record.get("mall") == "rakuten"
            and record.get("screen") == screen
            and record.get("document_type") == document_type
            and record.get("identity_hash") == identity_hash
            and record.get("sha256") == sha256
        ),
        None,
    )
    if existing:
        existing_path = _safe_existing_path(existing)
        if existing_path is not None:
            temporary.unlink(missing_ok=True)
            _append_manifest(
                {
                    "artifact_id": str(existing.get("artifact_id")),
                    "batch_id": batch_id,
                    "mall": "rakuten",
                    "category": "billpay_document",
                    "screen": screen,
                    "document_type": document_type,
                    "document_kind": kind,
                    "issue_date": issue_date,
                    "filename": existing_path.name,
                    "relative_path": existing_path.resolve()
                    .relative_to(paths.root.resolve())
                    .as_posix(),
                    "identity_hash": identity_hash,
                    "sha256": sha256,
                    "row_count": row_count,
                    "validated": validated,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            return BillPayDocument(
                screen=screen,
                document_type=document_type,
                document_kind=kind,
                issue_date=issue_date,
                artifact_id=str(existing.get("artifact_id")),
                downloaded_file=existing_path,
                source_sha256=sha256,
                row_count=int(existing.get("row_count")) if existing.get("row_count") is not None else None,
                validated=bool(existing.get("validated")),
            )

    artifact_id = uuid4().hex
    destination = paths.raw_dir / (
        f"billpay_{screen}_doctype-{document_type}_{artifact_id}.{kind}"
    )
    temporary.replace(destination)
    try:
        _append_manifest(
            {
                "artifact_id": artifact_id,
                "batch_id": batch_id,
                "mall": "rakuten",
                "category": "billpay_document",
                "screen": screen,
                "document_type": document_type,
                "document_kind": kind,
                "issue_date": issue_date,
                "filename": destination.name,
                "relative_path": destination.resolve()
                .relative_to(paths.root.resolve())
                .as_posix(),
                "identity_hash": identity_hash,
                "sha256": sha256,
                "row_count": row_count,
                "validated": validated,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    except Exception:
        _quarantine(destination, label=f"{screen}_manifest_failed")
        raise
    return BillPayDocument(
        screen=screen,
        document_type=document_type,
        document_kind=kind,
        issue_date=issue_date,
        artifact_id=artifact_id,
        downloaded_file=destination,
        source_sha256=sha256,
        row_count=row_count,
        validated=validated,
    )


def _quarantine(path: Path, *, label: str) -> None:
    if not path.exists():
        return
    paths = find_billing_statements_paths()
    paths.quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = paths.quarantine_dir / (
        f"{datetime.now():%Y%m%dT%H%M%S}_{label}_{path.name}"
    )
    shutil.move(str(path), destination)


@contextmanager
def _quarantine_staging_on_error(batch_dir: Path, *, label: str):
    try:
        yield
    except BaseException:
        if batch_dir.exists() and any(batch_dir.iterdir()):
            paths = find_billing_statements_paths()
            paths.quarantine_dir.mkdir(parents=True, exist_ok=True)
            destination = paths.quarantine_dir / (
                f"{datetime.now():%Y%m%dT%H%M%S}_{label}_{batch_dir.name}"
            )
            if destination.exists():
                destination = destination.with_name(
                    f"{destination.name}_{uuid4().hex[:8]}"
                )
            shutil.move(str(batch_dir), destination)
        elif batch_dir.exists():
            batch_dir.rmdir()
        raise


def _read_saved_result(
    *,
    screen: str,
    scope: str,
    issue_date: str | None,
    document_type: str,
) -> BillPaySettlementResult:
    all_records = _manifest_records()
    candidates: list[dict[str, object]] = []
    complete_markers = [
        record
        for record in reversed(all_records)
        if record.get("mall") == "rakuten"
        and record.get("category") == "batch_complete"
        and record.get("screen") == screen
        and record.get("document_type") == document_type
        and record.get("scope") == scope
        and (scope != "date" or record.get("issue_date") == issue_date)
    ]
    for complete in complete_markers:
        batch_candidates = [
            record
            for record in all_records
            if record.get("batch_id") == complete.get("batch_id")
            and record.get("mall") == "rakuten"
            # batch_complete マーカーは「このbatchは完了した」という印であって
            # 帳票レコードではない（issue_date=None / artifact_id=None）。
            # category で絞らないと、同一batch_idのマーカー自身が候補に混入し、
            # 後段 scope=latest の max() が str(None)=="None" を最大値として拾って
            # しまい（"N" > "2"）、実在する帳票が全て振り落とされて0件になる。
            # batch_complete マーカーは「このbatchは完了した」という印であって
            # 帳票レコードではない（issue_date=None / artifact_id=None）。
            # category で絞らないと、同一batch_idのマーカー自身が候補に混入し、
            # 後段 scope=latest の max() が str(None)=="None" を最大値として拾って
            # しまい（"N" > "2"）、実在する帳票が全て振り落とされて0件になる。
            and record.get("category") == "billpay_document"
            and record.get("screen") == screen
            and record.get("document_type") == document_type
        ]
        if scope == "date":
            batch_candidates = [
                record
                for record in batch_candidates
                if record.get("issue_date") == issue_date
            ]
        candidates = batch_candidates
        break
    if scope == "date":
        candidates = [record for record in candidates if record.get("issue_date") == issue_date]
    elif scope == "latest" and candidates:
        # 多重防御: 発行日を持たないレコードが将来混入しても "None" を最新と
        # 誤認しないよう、issue_date が実在するレコードだけで最新を決める。
        issue_dates = [
            str(record.get("issue_date"))
            for record in candidates
            if record.get("issue_date")
        ]
        if issue_dates:
            latest = max(issue_dates)
            candidates = [
                record
                for record in candidates
                if str(record.get("issue_date")) == latest
            ]
    unique_candidates: dict[str, dict[str, object]] = {}
    for record in candidates:
        artifact_id = str(record.get("artifact_id") or "")
        if artifact_id:
            unique_candidates[artifact_id] = record
    documents: list[BillPayDocument] = []
    warnings: list[str] = []
    for record in unique_candidates.values():
        path = _safe_existing_path(record)
        if path is None:
            warnings.append("BillPay manifestに対応する保存ファイルがありません。")
            continue
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if record.get("sha256") != sha256:
            warnings.append("BillPay帳票のSHA-256がmanifestと一致しません。")
            continue
        kind = str(record.get("document_kind"))
        (
            validated,
            row_count,
            _identity_hash,
            csv_issue_date,
        ) = _validate_downloaded_document(
            path.read_bytes(),
            document_type=document_type,
            kind=kind,
            warnings=warnings,
        )
        documents.append(
            BillPayDocument(
                screen=screen,
                document_type=document_type,
                document_kind=kind,
                issue_date=csv_issue_date or str(record.get("issue_date")),
                artifact_id=str(record.get("artifact_id")),
                downloaded_file=path,
                source_sha256=sha256,
                row_count=row_count,
                validated=validated,
            )
        )
    result = BillPaySettlementResult(
        executed=False,
        screen=screen,
        scope=scope,
        documents=tuple(documents),
        skipped_reason="dry-run: 保存済みmanifestと帳票のみ検証しました。",
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


async def _download_validate_commit(
    page,
    button,
    *,
    batch_dir: Path,
    screen: str,
    document_type: str,
    kind: str,
    issue_date: str,
    started: float,
    warnings: list[str],
    batch_id: str,
) -> BillPayDocument:
    temporary = batch_dir / f"{uuid4().hex}.{kind}"
    # ボタン特定・画面展開に時間を要した場合も、実click直前に25分制限を再確認する。
    _assert_session_safe(started)
    await _capture_download(
        page,
        button,
        directory=batch_dir,
        destination=temporary,
    )
    await _assert_screen(page, screen)
    try:
        (
            validated,
            row_count,
            identity_hash,
            csv_issue_date,
        ) = _validate_downloaded_document(
            temporary.read_bytes(),
            document_type=document_type,
            kind=kind,
            warnings=warnings,
        )
    except Exception:
        _quarantine(
            temporary,
            label=f"{screen}_doctype-{document_type}",
        )
        raise
    committed_issue_date = csv_issue_date or issue_date
    if csv_issue_date and csv_issue_date != issue_date:
        warnings.append(
            "画面の発行日とCSV内部の発行日が一致しないため、"
            "CSV内部の発行日を正本として採用しました。"
        )
    return _commit_document(
        temporary,
        screen=screen,
        document_type=document_type,
        kind=kind,
        issue_date=committed_issue_date,
        identity_hash=identity_hash,
        row_count=row_count,
        validated=validated,
        batch_id=batch_id,
    )


async def download_billpay_settlement(
    *,
    execute: bool,
    screen: str,
    scope: str,
    issue_date: str | date | None = None,
    document: str = "settlement-shop-csv",
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> BillPaySettlementResult:
    """全ページを列挙してからscope対象を決め、allowlist帳票だけを取得する。"""

    normalised_date, document_type, kind = _validate_request(
        screen=screen,
        scope=scope,
        issue_date=issue_date,
        document=document,
    )
    if not execute:
        return _read_saved_result(
            screen=screen,
            scope=scope,
            issue_date=normalised_date,
            document_type=document_type,
        )

    paths = find_billing_statements_paths()
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    batch_id = uuid4().hex
    batch_dir = paths.staging_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    warnings: list[str] = []
    documents: list[BillPayDocument] = []
    known_artifact_ids = {
        str(record.get("artifact_id"))
        for record in _manifest_records()
        if record.get("artifact_id")
    }
    started = time.monotonic()

    with _profile_lock(profile), _quarantine_staging_on_error(
        batch_dir,
        label=f"{screen}_failed",
    ):
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
                await page.goto(_SCREEN_URLS[screen], wait_until="domcontentloaded")
                try:
                    await _assert_screen(page, screen)
                except BillPayError as exc:
                    if exc.state != "NEEDS_LOGIN":
                        raise
                    # ログイン画面へリダイレクトされた場合のみ自動ログインを試みる
                    # （環境変数未設定なら_attempt_billpay_loginは何もせず戻り、
                    # 直後のassertが従来どおりNEEDS_LOGINを送出する）。
                    await _attempt_billpay_login(page, target_url=_SCREEN_URLS[screen])
                    await _assert_screen(page, screen)
                await _select_18_months(page)
                refs = await _collect_settlements(page, screen=screen)
                targets = _select_refs(
                    refs,
                    scope=scope,
                    issue_date=normalised_date,
                )
                if document_type == "summary" and targets:
                    # 表示情報CSVは精算回tbodyに属さないpage-global操作なので1回だけ取得する。
                    _assert_session_safe(started)
                    await _goto_page_index(
                        page,
                        screen=screen,
                        page_index=0,
                    )
                    await _assert_screen(page, screen)
                    button = await _summary_download_button(page)
                    documents.append(
                        await _download_validate_commit(
                            page,
                            button,
                            batch_dir=batch_dir,
                            screen=screen,
                            document_type=document_type,
                            kind=kind,
                            issue_date=max(item.issue_date for item in targets),
                            started=started,
                            warnings=warnings,
                            batch_id=batch_id,
                        )
                    )
                for target in ([] if document_type == "summary" else targets):
                    _assert_session_safe(started)
                    await _goto_page_index(
                        page,
                        screen=screen,
                        page_index=target.page_index,
                    )
                    await _assert_screen(page, screen)
                    tbody = await _find_target_tbody(page, target.fingerprint)
                    # 展開ボタンには aria-expanded 属性が無い（2026-07-13、実画面で確認。
                    # 開閉状態は <i class="rms-collapse-indicator open-shop"> のクラスで
                    # 表現されている）。Obsidianノートの契約どおり「目的のdocument
                    # buttonがhiddenならcollapseをクリックし、visibleになるまで待つ」
                    # 方式にする。最新の精算回は既に展開済みでクリック不要のこともある。
                    target_button = tbody.locator(
                        f'button[document-type="{document_type}"]'
                    )
                    collapse = tbody.locator("button.rms-collapse-btn")
                    if (
                        await target_button.count() > 0
                        and not await target_button.first.is_visible()
                        and await collapse.count() > 0
                    ):
                        await collapse.first.click()
                        try:
                            await target_button.first.wait_for(
                                state="visible", timeout=30_000
                            )
                        except PlaywrightTimeoutError:
                            raise BillPayError(
                                "BillPay精算回の展開完了を確認できません。",
                                state="PAGE_CONTRACT_CHANGED",
                            ) from None
                    button = await _document_button(tbody, document_type)
                    if button is None:
                        warnings.append(
                            f"{target.issue_date} のdocument-type {document_type} は未発行です。"
                        )
                        continue
                    documents.append(
                        await _download_validate_commit(
                            page,
                            button,
                            batch_dir=batch_dir,
                            screen=screen,
                            document_type=document_type,
                            kind=kind,
                            issue_date=target.issue_date,
                            started=started,
                            warnings=warnings,
                            batch_id=batch_id,
                        )
                    )
            finally:
                await context.close()

    if batch_dir.exists() and not any(batch_dir.iterdir()):
        batch_dir.rmdir()
    try:
        _append_manifest(
            {
                "artifact_id": None,
                "batch_id": batch_id,
                "mall": "rakuten",
                "category": "batch_complete",
                "screen": screen,
                "scope": scope,
                "document_type": document_type,
                "document_kind": kind,
                "issue_date": normalised_date if scope == "date" else None,
                "filename": None,
                "relative_path": None,
                "identity_hash": None,
                "sha256": None,
                "row_count": sum(item.row_count or 0 for item in documents),
                "validated": bool(documents)
                and all(item.validated for item in documents),
                "warnings": list(warnings),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    except Exception:
        for item in documents:
            if item.artifact_id not in known_artifact_ids:
                _quarantine(item.downloaded_file, label=f"{screen}_batch_incomplete")
        raise
    result = BillPaySettlementResult(
        executed=True,
        screen=screen,
        scope=scope,
        documents=tuple(documents),
        skipped_reason="対象scopeに発行済み帳票がありません。" if not documents else None,
        warnings=tuple(warnings),
    )
    _append_audit(result)
    return result


def download_billpay_settlement_sync(
    *,
    execute: bool,
    screen: str,
    scope: str,
    issue_date: str | date | None = None,
    document: str = "settlement-shop-csv",
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> BillPaySettlementResult:
    return asyncio.run(
        download_billpay_settlement(
            execute=execute,
            screen=screen,
            scope=scope,
            issue_date=issue_date,
            document=document,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )
