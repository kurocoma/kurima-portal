from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from portal_app.services.next_engine_downloader import APP_ROOT
from portal_app.services.next_engine_invoice import (
    InvoiceBatchDownloadResult,
    download_yamato_invoice_batch_sync,
)
from portal_app.services.next_engine_yamato import (
    YamatoBuyerDownloadResult,
    YamatoCustomShippingDownloadResult,
    YamatoProductDownloadResult,
    download_yamato_buyer_data_sync,
    download_yamato_custom_shipping_data_sync,
    download_yamato_product_data_sync,
)
from portal_app.services.yamato_conversion import (
    ORDER_NO_COLUMN,
    YamatoConversionResult,
    create_ne_to_yamato_csv,
    preview_ne_to_yamato_conversion,
)


YAMATO_B2_AUDIT_LOG_DIR = APP_ROOT / "logs" / "next_engine_yamato"
YAMATO_B2_PREP_AUDIT_LOG_PATH = YAMATO_B2_AUDIT_LOG_DIR / "yamato_b2_prepare_audit.jsonl"
DENPYO_NO_COLUMN = "伝票番号"


@dataclass(frozen=True)
class YamatoB2PreparationResult:
    buyer: YamatoBuyerDownloadResult | None
    product: YamatoProductDownloadResult | None
    invoice: InvoiceBatchDownloadResult | None
    custom_shipping: YamatoCustomShippingDownloadResult | None
    conversion: YamatoConversionResult
    consistency_warnings: tuple[str, ...]
    audit_path: Path


def prepare_yamato_b2_sync(
    *,
    fetch_next_engine: bool,
    execute_downloads: bool,
    check_invoices: bool,
    execute_invoices: bool,
    verify_invoice_statuses: bool = False,
    check_custom_shipping: bool,
    execute_custom_shipping: bool,
    custom_shipping_order_numbers: tuple[str, ...],
    write_conversion: bool,
    output_type: str,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> YamatoB2PreparationResult:
    buyer: YamatoBuyerDownloadResult | None = None
    product: YamatoProductDownloadResult | None = None
    invoice: InvoiceBatchDownloadResult | None = None
    custom_shipping: YamatoCustomShippingDownloadResult | None = None
    workflow_warnings: list[str] = []
    target_order_numbers = custom_shipping_order_numbers

    if execute_downloads:
        fetch_next_engine = True
    if execute_invoices:
        check_invoices = True
    if execute_custom_shipping:
        check_custom_shipping = True

    if fetch_next_engine:
        buyer = download_yamato_buyer_data_sync(
            execute=execute_downloads,
            order_numbers_filter=target_order_numbers,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
        )
        product = download_yamato_product_data_sync(
            execute=execute_downloads,
            output_type=output_type,
            order_numbers_filter=target_order_numbers,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
        )

    if check_invoices:
        invoice = download_yamato_invoice_batch_sync(
            execute=execute_invoices,
            order_numbers_filter=target_order_numbers,
            verify_statuses=verify_invoice_statuses,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
        )

    effective_custom_shipping_order_numbers = target_order_numbers
    if (
        not effective_custom_shipping_order_numbers
        and invoice
        and invoice.before_list.order_numbers
    ):
        effective_custom_shipping_order_numbers = invoice.before_list.order_numbers
    if (
        execute_custom_shipping
        and execute_invoices
        and invoice
        and (invoice.error or invoice.skipped_reason or not invoice.downloaded_file)
    ):
        workflow_warnings.append(
            "配送情報CSVは納品書PDF一括DLが完了していないためスキップしました。"
        )
        check_custom_shipping = False
        execute_custom_shipping = False

    if check_custom_shipping:
        custom_shipping = download_yamato_custom_shipping_data_sync(
            execute=execute_custom_shipping,
            order_numbers_filter=effective_custom_shipping_order_numbers,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
        )

    source_csv = custom_shipping.downloaded_file if custom_shipping and custom_shipping.downloaded_file else None
    conversion_write = write_conversion
    if execute_custom_shipping and custom_shipping and not custom_shipping.downloaded_file:
        conversion_write = False
    if workflow_warnings:
        conversion_write = False
    conversion = (
        create_ne_to_yamato_csv(source_csv=source_csv, preview_limit=preview_limit)
        if conversion_write
        else preview_ne_to_yamato_conversion(source_csv=source_csv, preview_limit=preview_limit)
    )

    warnings = workflow_warnings + _consistency_warnings(
        buyer=buyer,
        product=product,
        invoice=invoice,
        custom_shipping=custom_shipping,
        conversion=conversion,
    )
    result = YamatoB2PreparationResult(
        buyer=buyer,
        product=product,
        invoice=invoice,
        custom_shipping=custom_shipping,
        conversion=conversion,
        consistency_warnings=tuple(warnings),
        audit_path=YAMATO_B2_PREP_AUDIT_LOG_PATH,
    )
    _append_prepare_audit(result)
    return result


def _consistency_warnings(
    *,
    buyer: YamatoBuyerDownloadResult | None,
    product: YamatoProductDownloadResult | None,
    invoice: InvoiceBatchDownloadResult | None,
    custom_shipping: YamatoCustomShippingDownloadResult | None,
    conversion: YamatoConversionResult,
) -> list[str]:
    warnings: list[str] = []

    if buyer and product:
        buyer_orders = set(buyer.snapshot.order_numbers)
        product_orders = set(product.snapshot.order_numbers)
        if buyer_orders != product_orders:
            warnings.append(
                "購入者データと商品情報データの検索対象伝票番号が一致しません。"
            )

    if invoice and invoice.executed:
        if invoice.error:
            warnings.append(f"納品書PDF一括ダウンロードでエラーが発生しました: {invoice.error}")
        if invoice.skipped_reason:
            warnings.append(
                f"納品書PDF一括ダウンロードが完了していません: {invoice.skipped_reason}"
            )
        if invoice.executed and not invoice.downloaded_file and not invoice.skipped_reason:
            warnings.append("納品書PDF一括ダウンロードの出力ファイルを確認できません。")

    if buyer and invoice:
        buyer_orders = set(buyer.snapshot.order_numbers)
        invoice_orders = set(invoice.before_list.order_numbers)
        if buyer_orders and invoice_orders and buyer_orders != invoice_orders:
            warnings.append("購入者データと納品書PDF一括DLの対象伝票番号が一致しません。")

    if buyer and buyer.executed and buyer.downloaded_file:
        _compare_downloaded_orders(
            warnings,
            label="購入者データ",
            path=buyer.downloaded_file,
            column=DENPYO_NO_COLUMN,
            expected=set(buyer.snapshot.order_numbers),
        )

    if product and product.executed and product.downloaded_file:
        _compare_downloaded_orders(
            warnings,
            label="商品情報データ",
            path=product.downloaded_file,
            column=DENPYO_NO_COLUMN,
            expected=set(product.snapshot.order_numbers),
        )

    if custom_shipping and custom_shipping.executed and custom_shipping.downloaded_file:
        custom_orders = _read_csv_values(custom_shipping.downloaded_file, ORDER_NO_COLUMN)
        converted_orders = _read_csv_values(conversion.source_csv, ORDER_NO_COLUMN)
        if custom_orders != converted_orders:
            warnings.append(
                "配送情報CSVと変換元CSVのお客様管理番号が一致しません。"
            )
        if invoice and invoice.before_list.order_numbers:
            invoice_orders = set(invoice.before_list.order_numbers)
            if custom_orders != invoice_orders:
                warnings.append("配送情報CSVと納品書PDF一括DLの対象伝票番号が一致しません。")
    elif custom_shipping and custom_shipping.executed and not custom_shipping.downloaded_file:
        warnings.append(
            f"配送情報CSVの実ダウンロードが完了していません: {custom_shipping.skipped_reason}"
        )

    if conversion.address_review_rows:
        warnings.append(
            f"住所/建物名に手動確認が必要な行があります: {conversion.address_review_rows}件"
        )

    return warnings


def _compare_downloaded_orders(
    warnings: list[str],
    *,
    label: str,
    path: Path,
    column: str,
    expected: set[str],
) -> None:
    actual = _read_csv_values(path, column)
    if not actual:
        warnings.append(f"{label}CSVから伝票番号を読み取れませんでした。")
        return
    if actual != expected:
        warnings.append(f"{label}CSVの伝票番号が検索対象と一致しません。")


def _read_csv_values(path: Path, column: str) -> set[str]:
    for encoding in ("cp932", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding, newline="") as fp:
                reader = csv.DictReader(fp)
                if not reader.fieldnames or column not in reader.fieldnames:
                    return set()
                return {
                    str(row.get(column, "")).strip()
                    for row in reader
                    if str(row.get(column, "")).strip()
                }
        except UnicodeDecodeError:
            continue
    return set()


def _append_prepare_audit(result: YamatoB2PreparationResult) -> None:
    YAMATO_B2_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "yamato_b2_prepare",
        "buyer": _download_payload(result.buyer),
        "product": _download_payload(result.product),
        "invoice": _invoice_payload(result.invoice),
        "custom_shipping": _custom_shipping_payload(result.custom_shipping),
        "conversion": {
            "source_csv": str(result.conversion.source_csv),
            "output_csv": str(result.conversion.output_csv) if result.conversion.output_csv else None,
            "source_rows": result.conversion.source_rows,
            "output_rows": result.conversion.output_rows,
            "address_adjusted_rows": result.conversion.address_adjusted_rows,
            "address_review_rows": result.conversion.address_review_rows,
        },
        "consistency_warnings": list(result.consistency_warnings),
    }
    with result.audit_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _download_payload(
    result: YamatoBuyerDownloadResult | YamatoProductDownloadResult | None,
) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "executed": result.executed,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "source_filename": result.source_filename,
        "skipped_reason": result.skipped_reason,
        "snapshot_count": result.snapshot.count,
    }


def _custom_shipping_payload(
    result: YamatoCustomShippingDownloadResult | None,
) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "executed": result.executed,
        "ready_to_download": result.ready_to_download,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "source_filename": result.source_filename,
        "skipped_reason": result.skipped_reason,
    }


def _invoice_payload(result: InvoiceBatchDownloadResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "executed": result.executed,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "skipped_reason": result.skipped_reason,
        "error": result.error,
        "status_verified": result.status_verified,
        "snapshot_count": result.before_list.count,
        "before_status_count": len(result.before_statuses),
        "after_status_count": len(result.after_statuses),
    }
