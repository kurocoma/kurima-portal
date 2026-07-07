from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from portal_app.services.next_engine_downloader import (
    APP_ROOT,
    STORAGE_STATE_PATH,
    NextEngineOrderDetailDownloader,
    _chromium_launch_options,
    _headless_default,
    _next_engine_storage_lock,
)
from portal_app.services.paths import find_portal_paths

# ヤマトB2へのログインは、ヤマト伝票取込（/yamato）で稼働実績のある共通実装を使う。
# 独自セレクタの複製はログイン画面変更への追従漏れ・誤セレクタ事故（username欄が存在しない等）の
# 温床になるため、このモジュールではログイン処理を一切持たない。
from portal_app.services.yamato_b2_import import (
    LOGIN_ID_ENV as YAMATO_B2_LOGIN_ID_ENV,
    PASSWORD_ENV as YAMATO_B2_PASSWORD_ENV,
    B2LoginError,
    _capture_b2_debug_state,
    _click_text_if_visible as _b2_click_text,
    _enter_b2_cloud,
    _install_b2_page_workarounds,
    _is_b2_cloud_page,
    _login_to_b2,
    _reset_b2_page_guard,
    _storage_state_path as _b2_storage_state_path,
    _yamato_b2_headless_default,
)
from portal_app.settings import download_timeout_ms, nav_timeout_ms


FLOW_ID = "e020c90f-c86d-4be9-ace7-e38751e80d2f"
FLOW_NAME = "NE出荷確定"
AUDIT_LOG_DIR = APP_ROOT / "logs" / "shipment_confirmation"
AUDIT_LOG_PATH = AUDIT_LOG_DIR / "shipment_confirmation_audit.jsonl"
NEXT_ENGINE_SHIPMENT_UPLOAD_URL = "https://main.next-engine.com/Userlogine"
SHIPMENT_COMPLETION_DIR_PARTS = ("ネクストエンジン", "完成データ")
YAMATO_TRACKING_DIR_PARTS = ("ネクストエンジン", "yamato-okurizyo")
YAMATO_TRACKING_REQUIRED_HEADERS = ("お客様管理番号", "伝票番号", "出荷予定日")
# 画面プレビュー・手動確認用の6列（NEアップロードCSVの必須ヘッダーではない）。
SHIPMENT_IMPORT_HEADERS = (
    "店舗",
    "受注番号",
    "送り先名",
    "伝票番号",
    "発送伝票番号",
    "出荷予定日",
)
# ネクストエンジンへ反映する出荷実績CSVの実ヘッダー（VBA/実ファイルと一致する3列）。
SHIPMENT_UPLOAD_HEADERS = (
    "伝票番号",
    "発送伝票番号",
    "出荷予定日",
)
# 画面プレビュー行の列（6列＋マッピング結果の3列）。
SHIPMENT_PREVIEW_HEADERS = SHIPMENT_IMPORT_HEADERS + ("配送ソース", "状態", "警告")
# NE反映候補にできるファイル名の接頭辞。ne-to-yamato*.csv（ヤマトB2向け）等の誤反映を防ぐ。
SHIPMENT_UPLOAD_FILE_PREFIX = "yamato_to-ne"

DEFAULT_BUYER_LOOKBACK_DAYS = 20
DEFAULT_CLICKPOST_LOOKBACK_DAYS = 20
DEFAULT_LETTERPACK_LOOKBACK_DAYS = 30
DEFAULT_YAMATO_LOOKBACK_DAYS = 30

BUYER_LOOKBACK_ENV = "KURIMA_SHIPMENT_BUYER_LOOKBACK_DAYS"
CLICKPOST_LOOKBACK_ENV = "KURIMA_SHIPMENT_CLICKPOST_LOOKBACK_DAYS"
LETTERPACK_LOOKBACK_ENV = "KURIMA_SHIPMENT_LETTERPACK_LOOKBACK_DAYS"
YAMATO_LOOKBACK_ENV = "KURIMA_SHIPMENT_YAMATO_LOOKBACK_DAYS"


def _env_lookback_days(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _resolve_lookback_days(override: int | None, env_key: str, default: int) -> int:
    if override is not None and override > 0:
        return override
    return _env_lookback_days(env_key, default)


def shipment_lookback_defaults() -> dict[str, int]:
    """画面・CLIの既定値（環境変数で上書き可能な取込日数）を返す。"""
    return {
        "buyer": _env_lookback_days(BUYER_LOOKBACK_ENV, DEFAULT_BUYER_LOOKBACK_DAYS),
        "clickpost": _env_lookback_days(CLICKPOST_LOOKBACK_ENV, DEFAULT_CLICKPOST_LOOKBACK_DAYS),
        "letterpack": _env_lookback_days(LETTERPACK_LOOKBACK_ENV, DEFAULT_LETTERPACK_LOOKBACK_DAYS),
        "yamato": _env_lookback_days(YAMATO_LOOKBACK_ENV, DEFAULT_YAMATO_LOOKBACK_DAYS),
    }


@dataclass(frozen=True)
class ShipmentConfirmationStep:
    subflow: str
    target: str
    status: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ShipmentSlipImportResult:
    target_order_numbers: tuple[str, ...]
    output_csv: Path | None
    source_files: tuple[Path, ...]
    buyer_rows: int
    tracking_rows: int
    target_rows: int
    output_rows: int
    warnings: tuple[str, ...]
    preview_rows: tuple[dict[str, str], ...]
    audit_path: Path | None
    scanned_count: int = 0
    duplicate_count: int = 0
    buyer_matched_count: int = 0
    tracking_matched_count: int = 0
    unresolved_count: int = 0
    # 店舗ごとの対象件数（出現順）。プレビュー画面の「店舗別件数」表示に使う。
    store_counts: tuple[tuple[str, int], ...] = field(default=())


@dataclass(frozen=True)
class ShipmentUploadResult:
    executed: bool
    upload_csv: Path | None
    source_rows: int
    source_headers: tuple[str, ...]
    ready_to_upload: bool
    warnings: tuple[str, ...]
    preview_rows: tuple[dict[str, str], ...]
    confirmation_text: str | None
    skipped_reason: str | None
    audit_path: Path | None
    # ready_to_upload=False の根拠（ヘッダー欠落・空欄など。warnings は非ブロッキングの注意）。
    errors: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class YamatoTrackingExportResult:
    executed: bool
    target_date: str
    export_csv: Path | None
    source_rows: int
    source_headers: tuple[str, ...]
    ready_to_import: bool
    warnings: tuple[str, ...]
    preview_rows: tuple[dict[str, str], ...]
    skipped_reason: str | None
    audit_path: Path | None


@dataclass(frozen=True)
class ShipmentConfirmationResult:
    captured_at: datetime
    executed: bool
    flow_id: str
    flow_name: str
    order_numbers: tuple[str, ...]
    source_sample_input: Path | None
    expected_contract: Path | None
    shipment_import: ShipmentSlipImportResult | None
    shipment_upload: ShipmentUploadResult | None
    yamato_tracking_export: YamatoTrackingExportResult | None
    steps: tuple[ShipmentConfirmationStep, ...]
    side_effects: tuple[str, ...]
    skipped_reason: str | None
    audit_path: Path | None


def confirm_next_engine_shipment_sync(
    *,
    execute: bool,
    order_numbers: Iterable[str] = (),
    sample_input: str | Path | None = None,
    expected_contract: str | Path | None = None,
    fetch_yamato_tracking: bool = False,
    write_import_csv: bool = False,
    execute_upload: bool = False,
    confirm_upload: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 20,
    write_audit: bool = True,
) -> ShipmentConfirmationResult:
    """Plan or execute the NE shipment confirmation replacement.

    Live shipment confirmation changes Next Engine state, so the first G6
    implementation exposes only a dry-run service boundary. This gives G7/G8/G9
    a stable command and audit shape without performing the dangerous mutation.
    """

    sample_path = Path(sample_input) if sample_input else None
    expected_path = Path(expected_contract) if expected_contract else None
    records = tuple(_clean_records(order_numbers)) or _records_from_sample(sample_path)
    shipment_import = None
    shipment_upload = None
    yamato_tracking_export = None
    skipped_reason = None
    executed = False
    if fetch_yamato_tracking:
        yamato_tracking_export = download_yamato_tracking_export_sync(
            execute=execute,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
        )
    if records:
        shipment_import = (
            create_shipment_slip_import_csv(order_numbers=records, preview_limit=preview_limit)
            if execute and write_import_csv
            else preview_shipment_slip_import(order_numbers=records, preview_limit=preview_limit)
        )
    if execute_upload:
        shipment_upload = upload_next_engine_shipment_csv_sync(
            execute=execute,
            confirm_upload=confirm_upload,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
        )
    if execute:
        executed = any((fetch_yamato_tracking, write_import_csv, execute_upload))
        if not executed:
            skipped_reason = "no execution flags selected"

    result = ShipmentConfirmationResult(
        captured_at=datetime.now(),
        executed=executed,
        flow_id=FLOW_ID,
        flow_name=FLOW_NAME,
        order_numbers=records,
        source_sample_input=sample_path,
        expected_contract=expected_path,
        shipment_import=shipment_import,
        shipment_upload=shipment_upload,
        yamato_tracking_export=yamato_tracking_export,
        steps=_planned_steps(),
        side_effects=(
            "Next Engine shipment confirmation/status reflection",
            "tracking number retrieval/reflection",
        ),
        skipped_reason=skipped_reason,
        audit_path=AUDIT_LOG_PATH if write_audit else None,
    )
    if write_audit:
        _append_audit(result)
    return result


def preview_shipment_slip_import(
    *,
    order_numbers: Iterable[str] = (),
    preview_limit: int = 20,
    buyer_lookback_days: int | None = None,
    clickpost_lookback_days: int | None = None,
    letterpack_lookback_days: int | None = None,
    yamato_lookback_days: int | None = None,
) -> ShipmentSlipImportResult:
    return _build_shipment_slip_import(
        order_numbers=tuple(_clean_records(order_numbers)),
        write=False,
        preview_limit=preview_limit,
        buyer_lookback_days=buyer_lookback_days,
        clickpost_lookback_days=clickpost_lookback_days,
        letterpack_lookback_days=letterpack_lookback_days,
        yamato_lookback_days=yamato_lookback_days,
    )


def create_shipment_slip_import_csv(
    *,
    order_numbers: Iterable[str] = (),
    preview_limit: int = 20,
    buyer_lookback_days: int | None = None,
    clickpost_lookback_days: int | None = None,
    letterpack_lookback_days: int | None = None,
    yamato_lookback_days: int | None = None,
) -> ShipmentSlipImportResult:
    return _build_shipment_slip_import(
        order_numbers=tuple(_clean_records(order_numbers)),
        write=True,
        preview_limit=preview_limit,
        buyer_lookback_days=buyer_lookback_days,
        clickpost_lookback_days=clickpost_lookback_days,
        letterpack_lookback_days=letterpack_lookback_days,
        yamato_lookback_days=yamato_lookback_days,
    )


def write_shipment_confirmation_rows(
    rows: Iterable[dict[str, str]],
    *,
    preview_limit: int = 20,
) -> ShipmentSlipImportResult:
    """画面で確定した行（手動修正・手動追加を含む）からNE反映用の3列CSVを書き出す。

    出力契約: ヘッダーは厳密に `伝票番号,発送伝票番号,出荷予定日` の3列、
    出力先は ネクストエンジン\\完成データ、ファイル名は yamato_to-neYYMMDDHHMM.csv、
    文字コードは cp932、全フィールドをダブルクォートする（既存VBAと同じ形）。
    """
    warnings: list[str] = []
    cleaned: list[dict[str, str]] = []
    skipped = 0
    today = date.today().strftime("%Y/%m/%d")
    for row in rows:
        record = {header: _cell(row, header) for header in SHIPMENT_UPLOAD_HEADERS}
        if not any(record.values()):
            continue
        # 出荷予定日が空の行は「当日（実行日）」を既定値として補完する。
        # ソース由来の出荷予定日が入っている行はそのまま優先する。
        if not record["出荷予定日"]:
            record["出荷予定日"] = today
        if all(record.values()):
            cleaned.append(record)
        else:
            skipped += 1
            missing = [header for header in SHIPMENT_UPLOAD_HEADERS if not record[header]]
            warnings.append(
                f"未解決のため出力から除外しました: 伝票番号={record['伝票番号'] or '(空)'}"
                f"（{', '.join(missing)} が空）"
            )
    output_csv: Path | None = None
    if cleaned:
        output_csv = _next_completion_csv_path()
        _write_upload_rows(output_csv, cleaned)
    else:
        warnings.append("出力対象の行がありません（伝票番号・発送伝票番号・出荷予定日が揃った行が必要です）。")

    result = ShipmentSlipImportResult(
        target_order_numbers=tuple(record["伝票番号"] for record in cleaned),
        output_csv=output_csv,
        source_files=tuple(),
        buyer_rows=0,
        tracking_rows=0,
        target_rows=len(cleaned) + skipped,
        output_rows=len(cleaned),
        warnings=tuple(dict.fromkeys(warnings)),
        preview_rows=tuple(cleaned[:preview_limit]),
        audit_path=AUDIT_LOG_PATH if output_csv else None,
        scanned_count=len(cleaned) + skipped,
        unresolved_count=skipped,
    )
    if output_csv:
        _append_audit_payload("shipment_slip_import", result)
    return result


def preview_next_engine_shipment_upload(
    *,
    upload_csv: Path | None = None,
    preview_limit: int = 20,
) -> ShipmentUploadResult:
    return _build_shipment_upload_result(
        upload_csv=upload_csv,
        preview_limit=preview_limit,
        executed=False,
        confirmation_text=None,
        skipped_reason="dry_run",
        audit_path=None,
    )


async def upload_next_engine_shipment_csv(
    *,
    execute: bool,
    upload_csv: Path | None = None,
    confirm_upload: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 20,
) -> ShipmentUploadResult:
    preview = _build_shipment_upload_result(
        upload_csv=upload_csv,
        preview_limit=preview_limit,
        executed=False,
        confirmation_text=None,
        skipped_reason="dry_run" if not execute else None,
        audit_path=None,
    )
    if not execute:
        _append_audit_payload("shipment_upload_preview", preview)
        return preview

    # 実反映（NEの状態変更）は execute に加えて明示確認が必須。
    # --execute 単独／確認チェックなしのWeb実行では、絶対にアップロードしない。
    if not confirm_upload:
        result = _replace_upload_result(
            preview,
            executed=False,
            skipped_reason="confirmation_required",
            warnings=(
                *preview.warnings,
                "実反映には明示確認が必要です（CLI: --confirm-upload / Web UI: 確認チェックボックス）。",
            ),
            audit_path=AUDIT_LOG_PATH,
        )
        _append_audit_payload("shipment_upload", result)
        return result

    if not preview.ready_to_upload or preview.upload_csv is None:
        result = _replace_upload_result(
            preview,
            executed=True,
            skipped_reason="upload_csv_not_ready",
            audit_path=AUDIT_LOG_PATH,
        )
        _append_audit_payload("shipment_upload", result)
        return result

    paths = find_portal_paths()
    login_client = NextEngineOrderDetailDownloader(
        paths=paths,
        headless=_headless_default() if headless is None else headless,
        slow_mo_ms=slow_mo_ms,
    )
    with _next_engine_storage_lock():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **_chromium_launch_options(login_client.headless, slow_mo_ms)
            )
            try:
                context_kwargs: dict[str, object] = {
                    "accept_downloads": True,
                    "locale": "ja-JP",
                    "viewport": {"width": 1366, "height": 900},
                }
                if STORAGE_STATE_PATH.exists():
                    context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
                context = await browser.new_context(**context_kwargs)
                try:
                    page = await context.new_page()
                    await login_client._login(page)
                    await page.goto(NEXT_ENGINE_SHIPMENT_UPLOAD_URL, wait_until="domcontentloaded", timeout=nav_timeout_ms())
                    await page.locator(
                        'input[type="file"][name="_n_file"], input[name="_n_file"], input#_n_file'
                    ).first.set_input_files(str(preview.upload_csv))
                    await _click_upload_button(page)
                    await page.wait_for_load_state("domcontentloaded", timeout=nav_timeout_ms())
                    await page.wait_for_timeout(1500)
                    confirmation_text = await _page_text_excerpt(page)
                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                finally:
                    await context.close()
            finally:
                await browser.close()

    result = _replace_upload_result(
        preview,
        executed=True,
        confirmation_text=confirmation_text,
        skipped_reason=None,
        audit_path=AUDIT_LOG_PATH,
    )
    _append_audit_payload("shipment_upload", result)
    return result


def upload_next_engine_shipment_csv_sync(
    *,
    execute: bool,
    upload_csv: Path | None = None,
    confirm_upload: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 20,
) -> ShipmentUploadResult:
    return asyncio.run(
        upload_next_engine_shipment_csv(
            execute=execute,
            upload_csv=upload_csv,
            confirm_upload=confirm_upload,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
        )
    )


def preview_yamato_tracking_export(
    *,
    source_csv: Path | None = None,
    target_date: str | None = None,
    preview_limit: int = 20,
) -> YamatoTrackingExportResult:
    return _build_yamato_tracking_export_result(
        source_csv=source_csv,
        target_date=target_date,
        preview_limit=preview_limit,
        executed=False,
        skipped_reason="dry_run",
        audit_path=None,
    )


async def download_yamato_tracking_export(
    *,
    execute: bool,
    target_date: str | None = None,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 20,
) -> YamatoTrackingExportResult:
    if not execute:
        result = preview_yamato_tracking_export(target_date=target_date, preview_limit=preview_limit)
        _append_audit_payload("yamato_tracking_export_preview", result)
        return result

    login_id = os.environ.get(YAMATO_B2_LOGIN_ID_ENV, "").strip()
    password = os.environ.get(YAMATO_B2_PASSWORD_ENV, "").strip()
    if not login_id or not password:
        result = _build_yamato_tracking_export_result(
            source_csv=None,
            target_date=target_date,
            preview_limit=preview_limit,
            executed=True,
            skipped_reason="missing_yamato_b2_credentials",
            audit_path=AUDIT_LOG_PATH,
        )
        _append_audit_payload("yamato_tracking_export", result)
        return result

    export_date = target_date or date.today().strftime("%Y/%m/%d")
    destination = _next_yamato_tracking_path()
    browser_warnings: list[str] = []
    skipped_reason: str | None = None
    downloaded = False
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            **_chromium_launch_options(
                _yamato_b2_headless_default() if headless is None else headless, slow_mo_ms
            )
        )
        try:
            # ヤマト伝票取込（/yamato）と同じセッションの流儀: storage state を再利用し、
            # 成功時に保存する（多重ログイン・毎回入力を避ける）。
            storage_state = _b2_storage_state_path()
            context_kwargs: dict[str, object] = {
                "accept_downloads": True,
                "locale": "ja-JP",
                "viewport": {"width": 1366, "height": 900},
            }
            if storage_state.exists():
                context_kwargs["storage_state"] = str(storage_state)
            context = await browser.new_context(**context_kwargs)
            await _install_b2_page_workarounds(context)
            try:
                page = await context.new_page()
                try:
                    # 実績のある共通ログイン（実セレクタ #code1/CSTMR_PSWD・状態分類つき）。
                    await _login_to_b2(
                        page, login_id=login_id, password=password, warnings=browser_warnings
                    )
                    storage_state.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(storage_state))
                    # B2クラウド(main_menu)へはUIクリック導線で入る（直URLは system_error 誘発）。
                    page = await _enter_b2_cloud(page, warnings=browser_warnings)
                    if not await _is_b2_cloud_page(page):
                        raise RuntimeError(
                            "B2クラウド（main_menu）へ到達できませんでした。"
                            "時間をおいて再実行してください。"
                        )
                    await _export_issued_data_csv(
                        page,
                        export_date=export_date,
                        destination=destination,
                        warnings=browser_warnings,
                    )
                    downloaded = True
                except B2LoginError as exc:
                    # ログイン失敗の原因（still_on_login / needs_2fa / system_error /
                    # time_outside）をそのまま伝え、B2側要因かどうかを利用者が判別できるようにする。
                    skipped_reason = f"login_{exc.state}"
                    browser_warnings.append(str(exc))
                except Exception as exc:
                    skipped_reason = f"browser_error:{type(exc).__name__}"
                    browser_warnings.append(f"発行済データ取得に失敗しました: {exc}")
                finally:
                    # 証跡（スクリーンショット・HTML）を b2_import_debug に残す。
                    # 資格情報は _capture_b2_debug_state 側でマスクされる。
                    await _capture_b2_debug_state(page, "issued_data_export", warnings=browser_warnings)
            finally:
                await context.close()
        finally:
            await browser.close()

    result = _build_yamato_tracking_export_result(
        source_csv=destination if downloaded else None,
        target_date=export_date,
        preview_limit=preview_limit,
        executed=True,
        skipped_reason=skipped_reason,
        audit_path=AUDIT_LOG_PATH,
        extra_warnings=tuple(browser_warnings),
    )
    _append_audit_payload("yamato_tracking_export", result)
    return result


async def _export_issued_data_csv(
    page, *, export_date: str, destination: Path, warnings: list[str]
) -> None:
    """B2クラウドのメインメニューから発行済データを検索し、CSVを destination へ保存する。

    導線（実機画像 docs/yamatosyukkakakutei と要件資料で確定済みのセレクタ）:
    main_menu.html の a#issue_search → issue_search.html の #shipment_plan_from/to ＋ a#Search
    → 一覧 input.allCheck → a#issue_data_btn → 出力モーダル input#check_title → a#output_file
    → jQuery ダイアログの「ダウンロード」。URL直接指定は system_error を誘発するため使わない。
    """
    # 0) main_menu の描画完了（#issue_search の出現）を待つ。
    #    再ログイン直後などB2クラウド初回起動はローディング（灰色のpageGuard画面）が長引くことがあり、
    #    描画前にクリックを試みると「遷移できませんでした」で失敗する（実機で確認済み）。
    menu_ready = False
    for _attempt in range(3):
        try:
            await page.locator("#issue_search").first.wait_for(state="attached", timeout=30000)
            menu_ready = True
            break
        except PlaywrightTimeoutError:
            await _reset_b2_page_guard(page)
            await page.wait_for_timeout(2000)
    if not menu_ready:
        raise RuntimeError(
            "B2メインメニュー（発行済データの検索メニュー）が表示されませんでした。"
            "時間をおいて再実行してください。"
        )

    # 1) メニュー「発行済データの検索」→ 検索画面到達（2回まで再試行）
    reached_search = False
    for _attempt in range(2):
        await _reset_b2_page_guard(page)
        if not await _click_first_visible_locator(page, ("#issue_search", "a#issue_search")):
            if not await _b2_click_text(page, "発行済データの検索", optional=True):
                continue
        try:
            await page.locator("#shipment_plan_from").first.wait_for(state="attached", timeout=20000)
            reached_search = True
            break
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(2000)
    if not reached_search:
        raise RuntimeError("発行済データの検索画面（出荷予定日入力欄）に到達できませんでした。")
    # 画面JS（datepicker）の初期化を待つ。初期化前に入力すると既定値
    # （From=90日前〜To=当日）で上書きされ、対象日の絞り込みが効かない（実機で確認）。
    await page.wait_for_timeout(3000)

    # 2) 出荷予定日 From/To に対象日を設定して検索。
    #    Playwright の fill は datepicker に上書きされるため、JSで値を設定して
    #    input/change/blur を発火し、設定後の実値を読み戻して確認する。
    dates_ok = False
    for _attempt in range(2):
        await page.evaluate(
            """(target) => {
              for (const sel of ["#shipment_plan_from", "#shipment_plan_to"]) {
                const el = document.querySelector(sel);
                if (!el) continue;
                el.value = target;
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                el.dispatchEvent(new Event("blur", { bubbles: true }));
              }
            }""",
            export_date,
        )
        await page.wait_for_timeout(1000)
        values = await page.evaluate(
            """() => ({
              from: (document.querySelector("#shipment_plan_from") || {}).value || "",
              to: (document.querySelector("#shipment_plan_to") || {}).value || "",
            })"""
        )
        if values.get("from") == export_date and values.get("to") == export_date:
            dates_ok = True
            break
        await page.wait_for_timeout(1500)
    if not dates_ok:
        raise RuntimeError(
            f"出荷予定日を {export_date} に設定できませんでした"
            "（対象日以外のデータを誤取得しないため中断します）。"
        )

    # 検索は a#Search（jQuery ハンドラ）。trigger('click') が確実（実機で確認）。
    await _click_b2_jquery_button(page, "#Search", "検索")
    await page.wait_for_timeout(5000)

    # 3) 検索結果の有無を確認し、全選択する。
    result_count = await _read_b2_issue_search_result_count(page)
    if result_count == 0:
        raise RuntimeError(
            f"発行済データが見つかりません（対象日: {export_date}・検索結果0件）。"
            "B2で伝票を発行済みか、対象日を確認してください。"
        )
    if result_count is not None:
        warnings.append(f"発行済データ検索結果: {result_count}件（対象日: {export_date}）")
    if not await _check_first_visible(page, ("input.allCheck",), optional=True):
        raise RuntimeError("発行済データ一覧の全選択チェックボックスが見つかりませんでした。")
    await page.wait_for_timeout(800)

    # 4) 外部ファイルに出力 → モーダル（iframe内に出る）で見出し出力をチェック → ファイル出力。
    await _click_b2_jquery_button(page, "#issue_data_btn", "外部ファイルに出力")
    output_frame = await _find_frame_with_selector(page, "#output_file", timeout_ms=15000)
    if output_frame is None:
        raise RuntimeError("発行済データ外部出力モーダル（ファイル出力ボタン）が表示されませんでした。")
    try:
        title_check = output_frame.locator("#check_title").first
        if await title_check.count() > 0:
            await title_check.check()
    except Exception:
        warnings.append("見出し出力チェックを設定できませんでした（見出しなしで出力されます）。")
    await page.wait_for_timeout(500)

    # 5) ファイル出力 → （出る場合は）「ダウンロード」ダイアログ → CSV保存
    async with page.expect_download(timeout=download_timeout_ms(90000)) as download_info:
        await output_frame.locator("#output_file").first.click()
        for _ in range(15):
            await page.wait_for_timeout(1000)
            if await _click_b2_download_dialog_button(page):
                break
    download = await download_info.value
    await download.save_as(str(destination))
    warnings.append(f"発行済データCSVを保存しました: {destination.name}")


async def _click_b2_jquery_button(page, selector: str, label: str) -> None:
    """B2のjQueryハンドラ付きボタン（a#Search / a#issue_data_btn 等）を確実にクリックする。"""
    clicked = False
    try:
        clicked = bool(
            await page.evaluate(
                """(selector) => {
                  const btn = document.querySelector(selector);
                  if (!btn) return false;
                  const jq = window.jQuery || window.$;
                  if (jq) { jq(btn).trigger("click"); } else { btn.click(); }
                  return true;
                }""",
                selector,
            )
        )
    except Exception:
        clicked = False
    if not clicked and not await _click_first_visible_locator(page, (selector,)):
        if not await _b2_click_text(page, label, optional=True):
            raise RuntimeError(f"「{label}」をクリックできませんでした。")


async def _read_b2_issue_search_result_count(page) -> int | None:
    """検索結果ヘッダ「検索結果: x - y / N 件」から総件数 N を読む（読めなければ None）。"""
    try:
        return await page.evaluate(
            r"""() => {
              const body = document.body ? document.body.innerText : "";
              const m = body.match(/検索結果\s*[:：]\s*[0-9]+\s*-\s*[0-9]+\s*\/\s*([0-9]+)\s*件/);
              return m ? Number(m[1]) : null;
            }"""
        )
    except Exception:
        return None


async def _find_frame_with_selector(page, selector: str, *, timeout_ms: int = 15000):
    """全フレーム（iframe内モーダル対応）から selector を持つフレームを探す。"""
    waited = 0
    while waited <= timeout_ms:
        for frame in page.frames:
            try:
                if await frame.locator(selector).count() > 0:
                    return frame
            except Exception:
                continue
        await page.wait_for_timeout(1000)
        waited += 1000
    return None


async def _click_b2_download_dialog_button(page) -> bool:
    """ファイル出力後に出る「ダウンロード」ダイアログのボタンを全フレームから探して押す。"""
    for frame in page.frames:
        try:
            locator = frame.locator("a:has-text('ダウンロード'), button:has-text('ダウンロード')")
            if await locator.count() > 0 and await locator.first.is_visible(timeout=800):
                await locator.first.click()
                return True
        except Exception:
            continue
    return False


async def _click_first_visible_locator(page, selectors: tuple[str, ...]) -> bool:
    """セレクタ候補のうち最初に可視になった要素をクリックする（B2のJSリンクにも対応）。"""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            if not await locator.is_visible(timeout=2500):
                continue
            try:
                await locator.click(timeout=5000)
            except Exception:
                # href="javascript:" 形式のリンクは click イベント直接発火で押す。
                await locator.evaluate(
                    """(element) => {
                      element.dispatchEvent(new MouseEvent("click", {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                      }));
                      if (typeof element.click === "function") {
                        element.click();
                      }
                    }"""
                )
            await page.wait_for_timeout(500)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def download_yamato_tracking_export_sync(
    *,
    execute: bool,
    target_date: str | None = None,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 20,
) -> YamatoTrackingExportResult:
    return asyncio.run(
        download_yamato_tracking_export(
            execute=execute,
            target_date=target_date,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
        )
    )


def _planned_steps() -> tuple[ShipmentConfirmationStep, ...]:
    return (
        ShipmentConfirmationStep(
            subflow="Main",
            target="portal_tool/portal_app/services/shipment_confirmation.py",
            status="mapped",
            notes=("confirm_next_engine_shipment_sync orchestrates Yamato tracking export, shipment import CSV, and guarded NE upload.",),
        ),
        ShipmentConfirmationStep(
            subflow="ヤマト送り状番号取得",
            target="preview_yamato_tracking_export/download_yamato_tracking_export_sync",
            status="mapped",
            notes=("Dry-run validates yamato-okurizyo CSV; execute mode downloads from Yamato B2 Cloud when B2 credentials are configured.",),
        ),
        ShipmentConfirmationStep(
            subflow="しまのや送り状番号取得",
            target="Playwright Shimanoya tracking retrieval",
            status="legacy_disabled",
            notes=("Main's CALL is disabled; preserved as a non-reachable legacy branch.",),
        ),
        ShipmentConfirmationStep(
            subflow="出荷伝票読み込み",
            target="preview_shipment_slip_import/create_shipment_slip_import_csv",
            status="mapped",
            notes=("Replaces ne-yamato変換ツール.xlsm shipment-result Power Query with direct CSV processing.",),
        ),
        ShipmentConfirmationStep(
            subflow="downloadform",
            target="Playwright/browser download save_as or direct CSV output path",
            status="mapped",
            notes=("PAD save dialog is replaced by explicit file destinations.",),
        ),
        ShipmentConfirmationStep(
            subflow="NEに反映",
            target="preview_next_engine_shipment_upload/upload_next_engine_shipment_csv_sync",
            status="mapped_side_effect",
            notes=("Dry-run validates the latest completion CSV; execute mode uploads it to Next Engine Userlogine.",),
        ),
    )


def _build_shipment_slip_import(
    *,
    order_numbers: tuple[str, ...],
    write: bool,
    preview_limit: int,
    buyer_lookback_days: int | None = None,
    clickpost_lookback_days: int | None = None,
    letterpack_lookback_days: int | None = None,
    yamato_lookback_days: int | None = None,
) -> ShipmentSlipImportResult:
    portal_root = find_portal_paths().portal_root
    warnings: list[str] = []
    source_files: list[Path] = []

    scanned_count = len(order_numbers)
    normalized_all = [normalize_barcode_value(value) for value in order_numbers]
    normalized_all = [value for value in normalized_all if value]
    normalized_targets = tuple(dict.fromkeys(normalized_all))
    duplicate_count = len(normalized_all) - len(normalized_targets)
    if duplicate_count:
        warnings.append(f"重複スキャンを {duplicate_count} 件除外しました。")

    buyer_rows, buyer_files = _load_buyer_rows(
        portal_root,
        warnings,
        lookback_days=_resolve_lookback_days(
            buyer_lookback_days, BUYER_LOOKBACK_ENV, DEFAULT_BUYER_LOOKBACK_DAYS
        ),
    )
    source_files.extend(buyer_files)
    buyers_by_denpyo = {
        normalize_barcode_value(_cell(row, "伝票番号")): row
        for row in buyer_rows
        if normalize_barcode_value(_cell(row, "伝票番号"))
    }

    tracking_maps, tracking_files, tracking_row_count = _load_tracking_maps(
        portal_root,
        warnings,
        buyer_rows=buyer_rows,
        clickpost_lookback_days=_resolve_lookback_days(
            clickpost_lookback_days, CLICKPOST_LOOKBACK_ENV, DEFAULT_CLICKPOST_LOOKBACK_DAYS
        ),
        letterpack_lookback_days=_resolve_lookback_days(
            letterpack_lookback_days, LETTERPACK_LOOKBACK_ENV, DEFAULT_LETTERPACK_LOOKBACK_DAYS
        ),
        yamato_lookback_days=_resolve_lookback_days(
            yamato_lookback_days, YAMATO_LOOKBACK_ENV, DEFAULT_YAMATO_LOOKBACK_DAYS
        ),
    )
    source_files.extend(tracking_files)

    rows: list[dict[str, str]] = []
    buyer_matched = 0
    tracking_matched = 0
    for denpyo_no in normalized_targets:
        buyer = buyers_by_denpyo.get(denpyo_no, {})
        order_no = _cell(buyer, "受注番号")
        tracking_no, source_label, ambiguous, row_warnings = _resolve_tracking(
            denpyo_no=denpyo_no,
            order_no=order_no,
            tracking_maps=tracking_maps,
        )
        if buyer:
            buyer_matched += 1
        else:
            warnings.append(f"購入者データに伝票番号 {denpyo_no} が見つかりません。")
        if tracking_no:
            tracking_matched += 1
        else:
            warnings.append(f"発送伝票番号が見つかりません: 伝票番号={denpyo_no}")
        if not buyer:
            status = "購入者データ未一致"
        elif ambiguous:
            status = "複数候補あり"
        elif not tracking_no:
            status = "発送伝票番号未一致"
        else:
            status = "OK"
        shipping_date = (
            tracking_maps["yamato_date"].get(denpyo_no, "")
            if source_label == "ヤマト運輸"
            else ""
        ) or date.today().strftime("%Y/%m/%d")
        rows.append(
            {
                "店舗": _cell(buyer, "店舗"),
                "受注番号": order_no,
                "送り先名": _cell(buyer, "送り先名"),
                "伝票番号": denpyo_no,
                "発送伝票番号": tracking_no,
                "出荷予定日": shipping_date,
                "配送ソース": source_label,
                "状態": status,
                "警告": " / ".join(row_warnings),
            }
        )

    unresolved_count = sum(1 for row in rows if row["状態"] != "OK")

    # 店舗ごとの件数（出現順）。購入者データ未一致で店舗が分からない行は「店舗未一致」に集計する。
    store_counter: dict[str, int] = {}
    for row in rows:
        store = row["店舗"] or "店舗未一致"
        store_counter[store] = store_counter.get(store, 0) + 1
    store_counts = tuple(store_counter.items())

    output_csv: Path | None = None
    output_rows = len(rows)
    if write:
        # NE反映CSVは3列契約。未解決行（発送伝票番号なし等）は出力しない。
        complete = [row for row in rows if all(_cell(row, header) for header in SHIPMENT_UPLOAD_HEADERS)]
        if complete:
            output_csv = _next_completion_csv_path()
            output_rows = _write_upload_rows(output_csv, complete)
            if len(complete) < len(rows):
                warnings.append(
                    f"未解決の {len(rows) - len(complete)} 件はCSVへ出力していません（画面で修正後に再作成してください）。"
                )
        else:
            output_rows = 0
            warnings.append("出力対象の行がないため、出荷確定CSVは作成しませんでした。")

    result = ShipmentSlipImportResult(
        target_order_numbers=normalized_targets,
        output_csv=output_csv,
        source_files=tuple(dict.fromkeys(source_files)),
        buyer_rows=len(buyer_rows),
        tracking_rows=tracking_row_count,
        target_rows=len(rows),
        output_rows=output_rows,
        warnings=tuple(dict.fromkeys(warnings)),
        preview_rows=tuple(rows[:preview_limit]),
        audit_path=AUDIT_LOG_PATH if write else None,
        scanned_count=scanned_count,
        duplicate_count=duplicate_count,
        buyer_matched_count=buyer_matched,
        tracking_matched_count=tracking_matched,
        unresolved_count=unresolved_count,
        store_counts=store_counts,
    )
    if write:
        _append_audit_payload("shipment_slip_import", result)
    return result


def _build_shipment_upload_result(
    *,
    upload_csv: Path | None,
    preview_limit: int,
    executed: bool,
    confirmation_text: str | None,
    skipped_reason: str | None,
    audit_path: Path | None,
) -> ShipmentUploadResult:
    """NE反映候補CSVを解決・検証する（dry-run／execute前チェックの共通部）。

    必須（エラー＝ready_to_upload=False）: 3列ヘッダー・1行以上・3列とも空欄なし。
    警告（ブロックしない・画面で確認）: 発送伝票番号の非数字・伝票番号/発送伝票番号の重複・
    出荷予定日が今日以外／形式不正・ファイル名が yamato_to-ne*.csv でない。
    """
    warnings: list[str] = []
    errors: list[str] = []
    readable = True
    if upload_csv is not None:
        source: Path | None = Path(upload_csv)
        if not source.is_file():
            errors.append(f"指定されたCSVファイルが見つかりません: {source}")
            readable = False
        elif source.suffix.lower() != ".csv":
            errors.append(f"指定されたファイルがCSVではありません: {source.name}")
            readable = False
        elif not source.name.lower().startswith(SHIPMENT_UPLOAD_FILE_PREFIX):
            warnings.append(
                f"ファイル名が {SHIPMENT_UPLOAD_FILE_PREFIX}*.csv ではありません: {source.name}"
            )
    else:
        source = _latest_completion_csv(warnings)

    rows: list[dict[str, str]] = []
    headers: tuple[str, ...] = tuple()
    if source is not None and readable:
        headers, rows = _read_csv_with_headers(source, warnings)
        missing_headers = [header for header in SHIPMENT_UPLOAD_HEADERS if header not in headers]
        if missing_headers:
            errors.append("出荷実績CSVの必須ヘッダーが不足しています: " + ", ".join(missing_headers))
        elif not rows:
            errors.append("出荷実績CSVにデータ行がありません。")
        else:
            row_errors, row_warnings = _validate_upload_rows(rows)
            errors.extend(row_errors)
            warnings.extend(row_warnings)

    if source is None:
        errors.append(
            "ネクストエンジン\\完成データ に yamato_to-ne*.csv のアップロード候補がありません。"
        )

    ready_to_upload = bool(source and readable and rows and not errors)

    return ShipmentUploadResult(
        executed=executed,
        upload_csv=source,
        source_rows=len(rows),
        source_headers=headers,
        ready_to_upload=ready_to_upload,
        warnings=tuple(dict.fromkeys(warnings)),
        preview_rows=tuple(rows[:preview_limit]),
        confirmation_text=confirmation_text,
        skipped_reason=skipped_reason,
        audit_path=audit_path,
        errors=tuple(dict.fromkeys(errors)),
    )


def _validate_upload_rows(rows: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    """3列CSVの行検証。空欄はエラー、重複・非数字・日付は警告として返す。"""
    errors: list[str] = []
    warnings: list[str] = []
    blank_lines: dict[str, list[int]] = {header: [] for header in SHIPMENT_UPLOAD_HEADERS}
    bad_date_lines: list[int] = []
    non_digit_lines: list[int] = []
    denpyo_counts: dict[str, int] = {}
    tracking_counts: dict[str, int] = {}
    other_dates: set[str] = set()
    today = date.today().strftime("%Y/%m/%d")
    for line_no, row in enumerate(rows, start=2):  # 1行目はヘッダー
        for header in SHIPMENT_UPLOAD_HEADERS:
            if not _cell(row, header):
                blank_lines[header].append(line_no)
        denpyo = _cell(row, "伝票番号")
        tracking = _cell(row, "発送伝票番号")
        shipping_date = _cell(row, "出荷予定日")
        if denpyo:
            denpyo_counts[denpyo] = denpyo_counts.get(denpyo, 0) + 1
        if tracking:
            tracking_counts[tracking] = tracking_counts.get(tracking, 0) + 1
            if not tracking.isdigit():
                non_digit_lines.append(line_no)
        if shipping_date:
            if re.fullmatch(r"\d{4}/\d{2}/\d{2}", shipping_date) is None:
                bad_date_lines.append(line_no)
            elif shipping_date != today:
                other_dates.add(shipping_date)

    for header, lines in blank_lines.items():
        if lines:
            errors.append(f"『{header}』が空の行があります: 行 {_format_line_numbers(lines)}")
    duplicate_denpyo = [value for value, count in denpyo_counts.items() if count > 1]
    if duplicate_denpyo:
        warnings.append("伝票番号が重複しています: " + ", ".join(duplicate_denpyo[:5]))
    duplicate_tracking = [value for value, count in tracking_counts.items() if count > 1]
    if duplicate_tracking:
        warnings.append("発送伝票番号が重複しています: " + ", ".join(duplicate_tracking[:5]))
    if non_digit_lines:
        warnings.append(
            f"発送伝票番号に数字以外を含む行があります: 行 {_format_line_numbers(non_digit_lines)}"
        )
    if bad_date_lines:
        warnings.append(
            f"出荷予定日が YYYY/MM/DD 形式ではない行があります: 行 {_format_line_numbers(bad_date_lines)}"
        )
    if other_dates:
        warnings.append(
            f"出荷予定日が今日（{today}）以外の行があります: " + ", ".join(sorted(other_dates)[:5])
        )
    return errors, warnings


def _format_line_numbers(lines: list[int], limit: int = 5) -> str:
    shown = ", ".join(str(line) for line in lines[:limit])
    if len(lines) > limit:
        shown += f" ほか{len(lines) - limit}行"
    return shown


def _replace_upload_result(result: ShipmentUploadResult, **changes) -> ShipmentUploadResult:
    return replace(result, **changes)


def _build_yamato_tracking_export_result(
    *,
    source_csv: Path | None,
    target_date: str | None,
    preview_limit: int,
    executed: bool,
    skipped_reason: str | None,
    audit_path: Path | None,
    extra_warnings: tuple[str, ...] = (),
) -> YamatoTrackingExportResult:
    warnings: list[str] = list(extra_warnings)
    export_date = target_date or date.today().strftime("%Y/%m/%d")
    source = source_csv or _latest_yamato_tracking_csv(warnings)
    headers: tuple[str, ...] = tuple()
    rows: list[dict[str, str]] = []
    if source is not None:
        headers, rows = _read_csv_with_headers(source, warnings)
    missing_headers = [header for header in YAMATO_TRACKING_REQUIRED_HEADERS if header not in headers]
    if missing_headers:
        warnings.append("ヤマト発行済データCSVの必須ヘッダーが不足しています: " + ", ".join(missing_headers))
    if source is None:
        warnings.append("yamato-okurizyo に発行済データCSVがありません。")
    ready_to_import = bool(source and rows and not missing_headers)
    return YamatoTrackingExportResult(
        executed=executed,
        target_date=export_date,
        export_csv=source,
        source_rows=len(rows),
        source_headers=headers,
        ready_to_import=ready_to_import,
        warnings=tuple(dict.fromkeys(warnings)),
        preview_rows=tuple(rows[:preview_limit]),
        skipped_reason=skipped_reason,
        audit_path=audit_path,
    )


def _latest_yamato_tracking_csv(warnings: list[str]) -> Path | None:
    try:
        directory = find_portal_paths().portal_root.joinpath(*YAMATO_TRACKING_DIR_PARTS)
    except Exception as exc:
        warnings.append(f"ポータルパスを解決できません: {exc}")
        return None
    if not directory.is_dir():
        warnings.append(f"yamato-okurizyo フォルダが見つかりません: {directory}")
        return None
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".csv"
    ]
    if not files:
        return None
    valid_files: list[Path] = []
    for path in files:
        temp_warnings: list[str] = []
        headers, _rows = _read_csv_with_headers(path, temp_warnings)
        if all(header in headers for header in YAMATO_TRACKING_REQUIRED_HEADERS):
            valid_files.append(path)
    if valid_files:
        return max(valid_files, key=lambda path: path.stat().st_mtime)
    warnings.append("必須ヘッダーを持つヤマト発行済データCSVがないため、最新CSVを候補として表示します。")
    return max(files, key=lambda path: path.stat().st_mtime)


def _next_yamato_tracking_path() -> Path:
    directory = find_portal_paths().portal_root.joinpath(*YAMATO_TRACKING_DIR_PARTS)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    candidate = directory / f"data{timestamp}.csv"
    if not candidate.exists():
        return candidate
    for index in range(1, 100):
        indexed = directory / f"data{timestamp}_{index:02d}.csv"
        if not indexed.exists():
            return indexed
    raise RuntimeError("ヤマト発行済データCSVの保存ファイル名を決定できませんでした。")


def _latest_completion_csv(warnings: list[str]) -> Path | None:
    """NE反映候補CSVを自動選択する。

    候補は yamato_to-ne*.csv のみ（ne-to-yamato*.csv・shipment_confirmation_import.csv・
    clickpostimport.csv 等の別用途CSVは絶対に候補にしない）。さらに3列ヘッダー
    （伝票番号,発送伝票番号,出荷予定日）を持つファイルに限定し、最終更新が最新のものを返す。
    有効ファイルがなければ None（＝ready_to_upload=False）。誤アップロード防止のため
    「最新CSVへのフォールバック」はしない。
    """
    try:
        directory = find_portal_paths().portal_root.joinpath(*SHIPMENT_COMPLETION_DIR_PARTS)
    except Exception as exc:
        warnings.append(f"ポータルパスを解決できません: {exc}")
        return None
    if not directory.is_dir():
        warnings.append(f"完成データフォルダが見つかりません: {directory}")
        return None
    files = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".csv"
        and path.name.lower().startswith(SHIPMENT_UPLOAD_FILE_PREFIX)
    ]
    if not files:
        warnings.append(f"{SHIPMENT_UPLOAD_FILE_PREFIX}*.csv が見つかりません: {directory}")
        return None

    valid_files: list[Path] = []
    for path in files:
        temp_warnings: list[str] = []
        headers, _rows = _read_csv_with_headers(path, temp_warnings)
        if all(header in headers for header in SHIPMENT_UPLOAD_HEADERS):
            valid_files.append(path)
    if valid_files:
        return max(valid_files, key=lambda path: path.stat().st_mtime)

    warnings.append(
        "3列ヘッダー（伝票番号,発送伝票番号,出荷予定日）を持つ yamato_to-ne*.csv がありません。"
    )
    return None


def _read_csv_with_headers(path: Path, warnings: list[str]) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                rows = [dict(row) for row in reader]
                return tuple(reader.fieldnames or ()), rows
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            warnings.append(f"CSVを読み込めませんでした: {path.name}: {exc}")
            return tuple(), []
    warnings.append(f"CSVの文字コードを判定できませんでした: {path.name}")
    return tuple(), []


async def _click_upload_button(page) -> None:
    candidates = [
        page.get_by_role("button", name="出荷実績データCSVをアップロード"),
        page.locator("button:has-text('出荷実績データCSVをアップロード')"),
        page.locator("input[type='submit'][value*='出荷実績データCSV']"),
        page.locator("input[type='button'][value*='出荷実績データCSV']"),
    ]
    for locator in candidates:
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    await candidate.click()
                    return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    raise RuntimeError("出荷実績データCSVアップロードボタンが見つかりません。")


async def _page_text_excerpt(page) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""
    return " ".join(text.split())[:1000]


async def _check_first_visible(page, selectors: tuple[str, ...], *, optional: bool = False) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    await candidate.check()
                    return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    if not optional:
        raise RuntimeError(f"チェックボックスが見つかりません: {', '.join(selectors)}")
    return False


def _load_buyer_rows(
    portal_root: Path,
    warnings: list[str],
    *,
    lookback_days: int = DEFAULT_BUYER_LOOKBACK_DAYS,
) -> tuple[list[dict[str, str]], list[Path]]:
    """購入者データ3系統を直近 lookback_days 日分読み込み、受注番号で重複排除する。"""
    directories = (
        portal_root / "ネクストエンジン" / "ネクストエンジン受注データ" / "購入者データ",
        portal_root / "しまのやさん" / "csv格納" / "ネクストエンジン受注データ" / "購入者データ",
        portal_root / "CP・LPP宛名作成ツール" / "ネクストエンジン受注データ" / "購入者データ",
    )
    rows: list[dict[str, str]] = []
    files: list[Path] = []
    seen_orders: set[str] = set()
    for directory in directories:
        for path in _recent_files(directory, days=lookback_days):
            files.append(path)
            for row in _read_csv(path, warnings):
                order_no = _cell(row, "受注番号")
                if not order_no or order_no in seen_orders:
                    continue
                seen_orders.add(order_no)
                rows.append(
                    {
                        "店舗": _cell(row, "店舗"),
                        "受注番号": order_no,
                        "伝票番号": _cell(row, "伝票番号"),
                        "送り先名": _cell(row, "送り先名"),
                    }
                )
    return rows, files


def _load_tracking_maps(
    portal_root: Path,
    warnings: list[str],
    *,
    buyer_rows: list[dict[str, str]] | None = None,
    clickpost_lookback_days: int = DEFAULT_CLICKPOST_LOOKBACK_DAYS,
    letterpack_lookback_days: int = DEFAULT_LETTERPACK_LOOKBACK_DAYS,
    yamato_lookback_days: int = DEFAULT_YAMATO_LOOKBACK_DAYS,
) -> tuple[dict[str, dict], list[Path], int]:
    source_files: list[Path] = []
    row_count = 0
    maps: dict[str, dict] = {
        "yamato": {},
        "yamato_date": {},
        "clickpost": {},
        "clickpost_name": {},
        "clickpost_ambiguous": {},
        "letterpack": {},
        "shimanoya": {},
    }

    # 1. ヤマト運輸（yamato-okurizyo、直近N日）: 購入者.伝票番号 = ヤマト.お客様管理番号
    #    ヤマトの「出荷予定日」も保持し、行の出荷予定日に反映する。
    yamato_dir = portal_root / "ネクストエンジン" / "yamato-okurizyo"
    for path in _recent_files(yamato_dir, days=yamato_lookback_days):
        source_files.append(path)
        for row in _read_csv(path, warnings):
            row_count += 1
            denpyo_no = normalize_barcode_value(_cell(row, "お客様管理番号"))
            tracking_no = _cell(row, "伝票番号")
            if denpyo_no and tracking_no and denpyo_no not in maps["yamato"]:
                maps["yamato"][denpyo_no] = tracking_no
                shipping_date = _cell(row, "出荷予定日")
                if shipping_date:
                    maps["yamato_date"][denpyo_no] = shipping_date

    # 3-1. クリックポスト uploadfile.csv（伝票番号,発送伝票番号 の直接マップ）
    clickpost_dir = portal_root / "CP・LPP宛名作成ツール" / "完成したデータ"
    clickpost_file = clickpost_dir / "uploadfile.csv"
    if clickpost_file.is_file():
        source_files.append(clickpost_file)
        for row in _read_csv(clickpost_file, warnings):
            row_count += 1
            denpyo_no = normalize_barcode_value(_cell(row, "伝票番号"))
            tracking_no = _cell(row, "発送伝票番号")
            if denpyo_no and tracking_no:
                maps["clickpost"].setdefault(denpyo_no, tracking_no)

    # 3-2. クリックポストWebの送り状番号CSV（clickpost_tracking_numbers_*.csv、直近N日）。
    # 「お届け先氏名」と購入者データの「送り先名」を照合して 伝票番号 -> お問い合わせ番号 を作る。
    # 同姓同名・複数候補は自動確定せず（clickpost_ambiguous）、画面の手動修正対象に回す。
    tracking_names: dict[str, list[str]] = {}
    for path in _recent_files(
        clickpost_dir, days=clickpost_lookback_days, name_contains="clickpost_tracking_numbers"
    ):
        source_files.append(path)
        for row in _read_csv(path, warnings):
            row_count += 1
            name_key = _normalize_person_name(_cell(row, "お届け先氏名"))
            inquiry_no = _cell(row, "お問い合わせ番号")
            if not name_key or not inquiry_no:
                continue
            numbers = tracking_names.setdefault(name_key, [])
            if inquiry_no not in numbers:
                numbers.append(inquiry_no)
    if tracking_names:
        buyers_by_name: dict[str, list[str]] = {}
        for buyer in buyer_rows or []:
            name_key = _normalize_person_name(_cell(buyer, "送り先名"))
            denpyo_no = normalize_barcode_value(_cell(buyer, "伝票番号"))
            if not name_key or not denpyo_no:
                continue
            denpyos = buyers_by_name.setdefault(name_key, [])
            if denpyo_no not in denpyos:
                denpyos.append(denpyo_no)
        for name_key, numbers in tracking_names.items():
            denpyos = buyers_by_name.get(name_key, [])
            if not denpyos:
                continue
            if len(numbers) == 1 and len(denpyos) == 1:
                maps["clickpost_name"].setdefault(denpyos[0], numbers[0])
            else:
                # 曖昧一致: 自動確定しない。候補として保持し画面で選ばせる。
                for denpyo_no in denpyos:
                    maps["clickpost_ambiguous"].setdefault(denpyo_no, list(numbers))

    # 4. レターパック（ファイル名に「レターパック」を含むCSV、直近N日）: 送り状番号 -> 発送伝票番号
    for path in _recent_files(
        clickpost_dir, days=letterpack_lookback_days, name_contains="レターパック"
    ):
        source_files.append(path)
        for row in _read_csv(path, warnings):
            row_count += 1
            denpyo_no = normalize_barcode_value(_cell(row, "伝票番号"))
            tracking_no = _cell(row, "送り状番号") or _cell(row, "発送伝票番号")
            if denpyo_no and tracking_no:
                maps["letterpack"].setdefault(denpyo_no, tracking_no)

    # 2. しまのや出荷データ（最新1ファイルのみ。2列CSV「お客様管理番号,伝票番号」にも対応）
    shimanoya_dir = portal_root / "しまのやさん" / "csv格納" / "出荷データ"
    latest_shimanoya = _latest_file(shimanoya_dir)
    if latest_shimanoya is not None:
        source_files.append(latest_shimanoya)
        for row in _read_csv(latest_shimanoya, warnings):
            row_count += 1
            order_suffix = _normalize_shimanoya_order_suffix(_cell(row, "お客様管理番号"))
            tracking_no = _cell(row, "伝票番号") or _cell(row, "送り状番号")
            if order_suffix and tracking_no:
                maps["shimanoya"].setdefault(order_suffix, tracking_no)

    return maps, source_files, row_count


def _resolve_tracking(
    *,
    denpyo_no: str,
    order_no: str,
    tracking_maps: dict[str, dict],
) -> tuple[str, str, bool, list[str]]:
    """優先順位（ヤマト→しまのや→クリックポスト→レターパック）で発送伝票番号を解決する。

    Returns: (発送伝票番号, 配送ソース, 曖昧一致か, 行警告)
    複数ソースで異なる番号が見つかった場合は優先順位の高い方を採用し、競合警告を付ける。
    クリックポスト氏名照合の曖昧一致は自動確定しない（候補を警告に出して手動対象にする）。
    """
    candidates: list[tuple[str, str]] = []
    yamato_no = tracking_maps["yamato"].get(denpyo_no)
    if yamato_no:
        candidates.append(("ヤマト運輸", yamato_no))
    shimanoya_key = order_no[-7:] if order_no else ""
    shimanoya_no = tracking_maps["shimanoya"].get(shimanoya_key) if shimanoya_key else None
    if shimanoya_no:
        candidates.append(("しまのや", shimanoya_no))
    clickpost_no = tracking_maps["clickpost"].get(denpyo_no) or tracking_maps["clickpost_name"].get(
        denpyo_no
    )
    if clickpost_no:
        candidates.append(("クリックポスト", clickpost_no))
    letterpack_no = tracking_maps["letterpack"].get(denpyo_no)
    if letterpack_no:
        candidates.append(("レターパック", letterpack_no))

    row_warnings: list[str] = []
    if candidates:
        source_label, tracking_no = candidates[0]
        distinct = {number for _, number in candidates}
        if len(distinct) > 1:
            row_warnings.append(
                "複数ソースで発送伝票番号が競合しています: "
                + " / ".join(f"{label}={number}" for label, number in candidates)
                + f"（優先順位により {source_label} を採用）"
            )
        return tracking_no, source_label, False, row_warnings

    ambiguous = tracking_maps["clickpost_ambiguous"].get(denpyo_no)
    if ambiguous:
        row_warnings.append(
            "クリックポスト氏名照合で候補が複数あります（自動確定しません。手動で選択してください）: "
            + ", ".join(ambiguous[:5])
        )
        return "", "", True, row_warnings

    return "", "", False, row_warnings


def _recent_files(directory: Path, *, days: int, name_contains: str | None = None) -> list[Path]:
    if not directory.is_dir():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    files = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if name_contains and name_contains not in path.name:
            continue
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _latest_file(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    files = [path for path in directory.iterdir() if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _read_csv(path: Path, warnings: list[str]) -> list[dict[str, str]]:
    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            warnings.append(f"CSVを読み込めませんでした: {path.name}: {exc}")
            return []
    warnings.append(f"CSVの文字コードを判定できませんでした: {path.name}")
    return []


def _write_csv(path: Path, rows: Iterable[dict[str, str]], headers: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="cp932", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _next_completion_csv_path() -> Path:
    """NE反映CSVの出力先（完成データ\\yamato_to-neYYMMDDHHMM.csv）を決める。"""
    directory = find_portal_paths().portal_root.joinpath(*SHIPMENT_COMPLETION_DIR_PARTS)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    candidate = directory / f"{SHIPMENT_UPLOAD_FILE_PREFIX}{timestamp}.csv"
    if not candidate.exists():
        return candidate
    for index in range(2, 100):
        indexed = directory / f"{SHIPMENT_UPLOAD_FILE_PREFIX}{timestamp}_{index:02d}.csv"
        if not indexed.exists():
            return indexed
    raise RuntimeError("出荷確定CSVの保存ファイル名を決定できませんでした。")


def _write_upload_rows(path: Path, rows: Iterable[dict[str, str]]) -> int:
    """3列（伝票番号,発送伝票番号,出荷予定日）のNE反映CSVを書き出す。

    既存VBA（Module7 csv作成yamato_to_ne）と同じく cp932・全フィールドをダブルクォートする。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="cp932", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(SHIPMENT_UPLOAD_HEADERS)
        for row in rows:
            writer.writerow([_cell(row, header) for header in SHIPMENT_UPLOAD_HEADERS])
            count += 1
    return count


def _cell(row: dict[str, object], key: str) -> str:
    return str(row.get(key) or "").strip()


def normalize_barcode_value(value: str) -> str:
    """納品書バーコードのスキャン値を伝票番号へ正規化する（既存Excel PowerQueryと同じ思想）。

    規則（この順に適用）:
    1. 前後空白除去
    2. `D` / `d` 除去
    3. `00000` 除去
    4. Excel由来の末尾 `.0` 除去
    5. 数値として扱える場合は先頭ゼロを落とす
    """
    text = str(value or "").strip()
    text = text.replace("D", "").replace("d", "")
    text = text.replace("00000", "")
    if text.endswith(".0"):
        text = text[:-2]
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


# 後方互換の別名（既存コードは _normalize_denpyo_no を参照している）。
_normalize_denpyo_no = normalize_barcode_value


def _normalize_shimanoya_order_suffix(value: str) -> str:
    return str(value or "").strip().replace("a", "").replace("r", "").replace("R", "")


def _normalize_person_name(value: str) -> str:
    """氏名照合用の正規化（全角/半角空白を除去）。"""
    return str(value or "").replace("　", "").replace(" ", "").strip()


def _clean_records(values: Iterable[str]) -> list[str]:
    return [value.strip() for value in values if str(value).strip()]


def _records_from_sample(sample_path: Path | None) -> tuple[str, ...]:
    if not sample_path or not sample_path.is_file():
        return tuple()
    doc = json.loads(sample_path.read_text(encoding="utf-8"))
    records = doc.get("inputs", {}).get("target_records", [])
    if not isinstance(records, list):
        return tuple()
    return tuple(value for value in _clean_records(str(item) for item in records) if not value.startswith("${"))


def _append_audit(result: ShipmentConfirmationResult) -> None:
    _append_audit_payload("shipment_confirmation", result)


def _append_audit_payload(kind: str, result) -> None:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        # preview_rows 経由で実在個人名（送り先名等）が監査 jsonl に残らないようマスクする。
        "result": _mask_personal_fields(_json_safe(asdict(result))),
    }
    with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


# 監査ログに生の値を残してはいけない個人名フィールド（preview_rows 等の入れ子にも適用）。
_PERSONAL_NAME_KEYS = ("送り先名", "お届け先氏名")


def _mask_person_name(value) -> str:
    """個人名を「先頭1文字＋*」にマスクする（監査ログ用。空は空のまま）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return text[0] + "*" * max(len(text) - 1, 1)


def _mask_personal_fields(value):
    """監査ログへ書く構造から個人名フィールドを再帰的にマスクする。

    店舗・伝票番号・件数などの業務値は変更しない。対象キーは _PERSONAL_NAME_KEYS のみ。
    """
    if isinstance(value, dict):
        return {
            key: (
                _mask_person_name(item)
                if key in _PERSONAL_NAME_KEYS
                else _mask_personal_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_personal_fields(item) for item in value]
    return value


def _json_safe(value):
    if isinstance(value, Path):
        return _sanitize_path(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _sanitize_path(path: Path) -> str:
    raw = str(path)
    replacements = [(str(APP_ROOT), "${APP_ROOT}"), (str(Path.home()), "${USER_HOME}")]
    try:
        replacements.insert(0, (str(find_portal_paths().portal_root), "${PORTAL_ROOT}"))
    except Exception:
        pass
    for needle, replacement in replacements:
        if needle and raw.startswith(needle):
            return replacement + raw[len(needle) :]
    return raw
