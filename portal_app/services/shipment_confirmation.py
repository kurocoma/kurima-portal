from __future__ import annotations

import asyncio
import csv
import json
import os
from dataclasses import asdict, dataclass, replace
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


FLOW_ID = "e020c90f-c86d-4be9-ace7-e38751e80d2f"
FLOW_NAME = "NE出荷確定"
AUDIT_LOG_DIR = APP_ROOT / "logs" / "shipment_confirmation"
AUDIT_LOG_PATH = AUDIT_LOG_DIR / "shipment_confirmation_audit.jsonl"
NEXT_ENGINE_SHIPMENT_UPLOAD_URL = "https://main.next-engine.com/Userlogine"
YAMATO_B2_URL = "https://newb2web.kuronekoyamato.co.jp/system_error.html?api=0"
SHIPMENT_COMPLETION_DIR_PARTS = ("ネクストエンジン", "完成データ")
YAMATO_TRACKING_DIR_PARTS = ("ネクストエンジン", "yamato-okurizyo")
YAMATO_TRACKING_REQUIRED_HEADERS = ("お客様管理番号", "伝票番号", "出荷予定日")
SHIPMENT_IMPORT_HEADERS = (
    "店舗",
    "受注番号",
    "送り先名",
    "伝票番号",
    "発送伝票番号",
    "出荷予定日",
)


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
) -> ShipmentSlipImportResult:
    return _build_shipment_slip_import(
        order_numbers=tuple(_clean_records(order_numbers)),
        write=False,
        preview_limit=preview_limit,
    )


def create_shipment_slip_import_csv(
    *,
    order_numbers: Iterable[str] = (),
    preview_limit: int = 20,
) -> ShipmentSlipImportResult:
    return _build_shipment_slip_import(
        order_numbers=tuple(_clean_records(order_numbers)),
        write=True,
        preview_limit=preview_limit,
    )


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
                    await page.goto(NEXT_ENGINE_SHIPMENT_UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.locator('input[type="file"][name="_n_file"], input[name="_n_file"]').set_input_files(
                        str(preview.upload_csv)
                    )
                    await _click_upload_button(page)
                    await page.wait_for_load_state("domcontentloaded", timeout=60000)
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
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 20,
) -> ShipmentUploadResult:
    return asyncio.run(
        upload_next_engine_shipment_csv(
            execute=execute,
            upload_csv=upload_csv,
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

    login_id = os.environ.get("YAMATO_B2_LOGIN_ID", "").strip()
    password = os.environ.get("YAMATO_B2_PASSWORD", "").strip()
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
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            **_chromium_launch_options(_headless_default() if headless is None else headless, slow_mo_ms)
        )
        try:
            context = await browser.new_context(
                accept_downloads=True,
                locale="ja-JP",
                viewport={"width": 1366, "height": 900},
            )
            try:
                page = await context.new_page()
                await page.goto(YAMATO_B2_URL, wait_until="domcontentloaded", timeout=60000)
                await _click_text_if_visible(page, "ログイン画面へ")
                await _fill_first_visible(page, ("input[name='username']", "input#username"), login_id)
                await _fill_first_visible(page, ("input[name='CSTMR_PSWD']", "input[type='password']"), password)
                await _click_b2_login_submit(page)
                await _click_text_if_visible(page, "送り状発行システムB2クラウド", optional=True)
                await _click_text_if_visible(page, "発行済データの検索", optional=True)
                await _fill_first_visible(page, ("input[name='shipment_plan_from']", "input#shipment_plan_from"), export_date)
                await _click_text_if_visible(page, "検索")
                await _check_first_visible(page, ("input[type='checkbox']",))
                await _click_text_if_visible(page, "外部ファイルに出力")
                await _check_first_visible(page, ("input[name='check_title']", "input#check_title"), optional=True)
                await _click_text_if_visible(page, "ファイル出力")
                async with page.expect_download(timeout=90000) as download_info:
                    await _click_text_if_visible(page, "ダウンロード")
                download = await download_info.value
                await download.save_as(str(destination))
            finally:
                await context.close()
        finally:
            await browser.close()

    result = _build_yamato_tracking_export_result(
        source_csv=destination,
        target_date=export_date,
        preview_limit=preview_limit,
        executed=True,
        skipped_reason=None,
        audit_path=AUDIT_LOG_PATH,
    )
    _append_audit_payload("yamato_tracking_export", result)
    return result


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
) -> ShipmentSlipImportResult:
    portal_root = find_portal_paths().portal_root
    warnings: list[str] = []
    source_files: list[Path] = []
    normalized_targets = tuple(_normalize_denpyo_no(value) for value in order_numbers if _normalize_denpyo_no(value))

    buyer_rows, buyer_files = _load_buyer_rows(portal_root, warnings)
    source_files.extend(buyer_files)
    buyers_by_denpyo = {
        _normalize_denpyo_no(_cell(row, "伝票番号")): row
        for row in buyer_rows
        if _normalize_denpyo_no(_cell(row, "伝票番号"))
    }

    tracking_maps, tracking_files, tracking_row_count = _load_tracking_maps(portal_root, warnings)
    source_files.extend(tracking_files)

    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for denpyo_no in normalized_targets:
        buyer = buyers_by_denpyo.get(denpyo_no, {})
        order_no = _cell(buyer, "受注番号")
        tracking_no = _tracking_no_for_denpyo(
            denpyo_no=denpyo_no,
            order_no=order_no,
            tracking_maps=tracking_maps,
        )
        if not buyer:
            warnings.append(f"購入者データに伝票番号 {denpyo_no} が見つかりません。")
        if not tracking_no:
            warnings.append(f"発送伝票番号が見つかりません: 伝票番号={denpyo_no}")
        key = (order_no, tracking_no)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "店舗": _cell(buyer, "店舗"),
                "受注番号": order_no,
                "送り先名": _cell(buyer, "送り先名"),
                "伝票番号": denpyo_no,
                "発送伝票番号": tracking_no,
                "出荷予定日": date.today().strftime("%Y/%m/%d"),
            }
        )

    output_csv: Path | None = None
    if write:
        output_csv = portal_root / "ネクストエンジン" / "完成データ" / "shipment_confirmation_import.csv"
        _write_csv(output_csv, rows, SHIPMENT_IMPORT_HEADERS)

    result = ShipmentSlipImportResult(
        target_order_numbers=normalized_targets,
        output_csv=output_csv,
        source_files=tuple(dict.fromkeys(source_files)),
        buyer_rows=len(buyer_rows),
        tracking_rows=tracking_row_count,
        target_rows=len(rows),
        output_rows=len(rows),
        warnings=tuple(dict.fromkeys(warnings)),
        preview_rows=tuple(rows[:preview_limit]),
        audit_path=AUDIT_LOG_PATH if write else None,
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
    warnings: list[str] = []
    source = upload_csv or _latest_completion_csv(warnings)
    rows: list[dict[str, str]] = []
    headers: tuple[str, ...] = tuple()
    if source is not None:
        headers, rows = _read_csv_with_headers(source, warnings)

    missing_headers = [header for header in SHIPMENT_IMPORT_HEADERS if header not in headers]
    if missing_headers:
        warnings.append("出荷実績CSVの必須ヘッダーが不足しています: " + ", ".join(missing_headers))

    ready_to_upload = bool(source and source.is_file() and rows and not missing_headers)
    if source is None:
        warnings.append("ネクストエンジン\\完成データ にアップロード候補CSVがありません。")

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
    )


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
) -> YamatoTrackingExportResult:
    warnings: list[str] = []
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
        if path.is_file() and path.suffix.lower() == ".csv"
    ]
    if not files:
        return None

    valid_files: list[Path] = []
    for path in files:
        temp_warnings: list[str] = []
        headers, _rows = _read_csv_with_headers(path, temp_warnings)
        if all(header in headers for header in SHIPMENT_IMPORT_HEADERS):
            valid_files.append(path)
    if valid_files:
        return max(valid_files, key=lambda path: path.stat().st_mtime)

    warnings.append("必須ヘッダーを持つ出荷実績CSVがないため、最新CSVを候補として表示します。")
    return max(files, key=lambda path: path.stat().st_mtime)


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


async def _click_text_if_visible(page, text: str, *, optional: bool = False) -> None:
    candidates = [
        page.get_by_text(text, exact=True),
        page.get_by_role("button", name=text, exact=True),
        page.get_by_role("link", name=text, exact=True),
        page.locator(f"a:has-text('{text}')"),
        page.locator(f"button:has-text('{text}')"),
        page.locator(f"input[value*='{text}']"),
        page.locator(f"span:has-text('{text}')"),
    ]
    for locator in candidates:
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    if await _is_ignored_link(candidate):
                        continue
                    await candidate.click()
                    await page.wait_for_timeout(500)
                    return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    if not optional:
        raise RuntimeError(f"{text} をクリックできませんでした。")


async def _click_b2_login_submit(page) -> None:
    try:
        used_js = await page.evaluate(
            """() => {
              if (typeof func_request_Link === "function") {
                func_request_Link("LOGIN");
                return true;
              }
              return false;
            }"""
        )
        if used_js:
            await page.wait_for_timeout(500)
            return
    except Exception:
        pass

    candidates = [
        page.locator("input[type='submit'][value='ログイン']"),
        page.locator("input[type='button'][value='ログイン']"),
        page.locator("button[type='submit']:has-text('ログイン')"),
        page.get_by_role("button", name="ログイン", exact=True),
        page.locator("a.login:has-text('ログイン')"),
    ]
    for locator in candidates:
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    await candidate.click()
                    await page.wait_for_timeout(500)
                    return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    raise RuntimeError("B2ログインボタンをクリックできませんでした。")


async def _is_ignored_link(locator) -> bool:
    try:
        href = await locator.evaluate(
            """(element) => {
              const link = element.closest("a");
              return link ? link.href : "";
            }"""
        )
    except Exception:
        return False
    return "LoginCaution.pdf" in str(href)


async def _fill_first_visible(page, selectors: tuple[str, ...], value: str) -> None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    await candidate.fill(value)
                    return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    raise RuntimeError(f"入力欄が見つかりません: {', '.join(selectors)}")


async def _check_first_visible(page, selectors: tuple[str, ...], *, optional: bool = False) -> None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    await candidate.check()
                    return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    if not optional:
        raise RuntimeError(f"チェックボックスが見つかりません: {', '.join(selectors)}")


def _load_buyer_rows(portal_root: Path, warnings: list[str]) -> tuple[list[dict[str, str]], list[Path]]:
    directories = (
        portal_root / "ネクストエンジン" / "ネクストエンジン受注データ" / "購入者データ",
        portal_root / "しまのやさん" / "csv格納" / "ネクストエンジン受注データ" / "購入者データ",
        portal_root / "CP・LPP宛名作成ツール" / "ネクストエンジン受注データ" / "購入者データ",
    )
    rows: list[dict[str, str]] = []
    files: list[Path] = []
    seen_orders: set[str] = set()
    for directory in directories:
        for path in _recent_files(directory, days=20):
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
) -> tuple[dict[str, dict[str, str]], list[Path], int]:
    source_files: list[Path] = []
    row_count = 0
    maps = {
        "yamato": {},
        "clickpost": {},
        "letterpack": {},
        "shimanoya": {},
    }

    yamato_dir = portal_root / "ネクストエンジン" / "yamato-okurizyo"
    for path in _recent_files(yamato_dir, days=30):
        source_files.append(path)
        for row in _read_csv(path, warnings):
            row_count += 1
            denpyo_no = _normalize_denpyo_no(_cell(row, "お客様管理番号"))
            tracking_no = _cell(row, "伝票番号")
            if denpyo_no and tracking_no and denpyo_no not in maps["yamato"]:
                maps["yamato"][denpyo_no] = tracking_no

    clickpost_file = portal_root / "CP・LPP宛名作成ツール" / "完成したデータ" / "uploadfile.csv"
    if clickpost_file.is_file():
        source_files.append(clickpost_file)
        for row in _read_csv(clickpost_file, warnings):
            row_count += 1
            denpyo_no = _normalize_denpyo_no(_cell(row, "伝票番号"))
            tracking_no = _cell(row, "発送伝票番号")
            if denpyo_no and tracking_no:
                maps["clickpost"].setdefault(denpyo_no, tracking_no)

    letterpack_dir = portal_root / "CP・LPP宛名作成ツール" / "完成したデータ"
    for path in _recent_files(letterpack_dir, days=30, name_contains="レターパック"):
        source_files.append(path)
        for row in _read_csv(path, warnings):
            row_count += 1
            denpyo_no = _normalize_denpyo_no(_cell(row, "伝票番号"))
            tracking_no = _cell(row, "送り状番号") or _cell(row, "発送伝票番号")
            if denpyo_no and tracking_no:
                maps["letterpack"].setdefault(denpyo_no, tracking_no)

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


def _tracking_no_for_denpyo(
    *,
    denpyo_no: str,
    order_no: str,
    tracking_maps: dict[str, dict[str, str]],
) -> str:
    shimanoya_key = order_no[-7:] if order_no else ""
    return (
        tracking_maps["yamato"].get(denpyo_no)
        or tracking_maps["shimanoya"].get(shimanoya_key)
        or tracking_maps["clickpost"].get(denpyo_no)
        or tracking_maps["letterpack"].get(denpyo_no)
        or ""
    )


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


def _cell(row: dict[str, object], key: str) -> str:
    return str(row.get(key) or "").strip()


def _normalize_denpyo_no(value: str) -> str:
    text = str(value or "").strip().replace("00000", "").replace("D", "").replace("d", "")
    if text.endswith(".0"):
        text = text[:-2]
    return text.lstrip("0") or text


def _normalize_shimanoya_order_suffix(value: str) -> str:
    return str(value or "").strip().replace("a", "").replace("r", "").replace("R", "")


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
        "result": _json_safe(asdict(result)),
    }
    with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


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
