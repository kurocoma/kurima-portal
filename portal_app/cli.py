from __future__ import annotations

import argparse
import ctypes
import re
import sys
from pathlib import Path

from portal_app.env import load_env_file

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

load_env_file()

from portal_app.services.clickpost import (
    ClickPostBuyerDownloadResult,
    ClickPostConversionResult,
    ClickPostOrderListSnapshot,
    ClickPostPreparationResult,
    ClickPostProductDownloadResult,
    ClickPostPaymentPrintResult,
    ClickPostImportPaymentPrintResult,
    ClickPostTrackingExportResult,
    ClickPostTrackingReflectionResult,
    LetterPackAddressResult,
    ClickPostUploadResult,
    complete_clickpost_payments_and_print_sync,
    create_clickpost_tracking_reflection_csv,
    create_clickpost_csv,
    create_letterpack_address_csv,
    download_clickpost_buyer_data_sync,
    download_clickpost_product_data_sync,
    export_clickpost_tracking_for_csv_sync,
    inspect_clickpost_order_list_sync,
    import_pay_print_clickpost_csv_sync,
    prepare_clickpost_sync,
    preview_clickpost_tracking_reflection,
    preview_clickpost_csv,
    preview_letterpack_addresses,
    upload_clickpost_csv_sync,
)
from portal_app.services.inventory import analyze_latest_inventory
from portal_app.services.letterpack_pdf import (
    LetterPackLabelPdfResult,
    create_letterpack_label_pdf,
)
from portal_app.services.next_engine_downloader import download_next_engine_order_details_sync
from portal_app.services.next_engine_invoice import (
    InvoiceBatchDownloadResult,
    InvoiceDownloadTestResult,
    download_yamato_invoice_batch_sync,
    test_invoice_download_and_restore_sync,
)
from portal_app.services.ne02_order_details import (
    Ne02OrderDetailDownloadResult,
    download_ne02_order_details_sync,
)
from portal_app.services.next_engine_order_status import (
    OrderStatusBatchRestoreResult,
    OrderStatusSnapshot,
    inspect_next_engine_order_sync,
    restore_next_engine_print_wait_batch_sync,
    restore_next_engine_print_wait_sync,
)
from portal_app.services.next_engine_yamato import (
    YamatoBuyerDownloadResult,
    YamatoCustomShippingDownloadResult,
    YamatoOrderListSnapshot,
    YamatoProductDownloadResult,
    download_yamato_buyer_data_sync,
    download_yamato_custom_shipping_data_sync,
    download_yamato_product_data_sync,
    inspect_yamato_order_list_sync,
)
from portal_app.services.shipment_confirmation import (
    ShipmentConfirmationResult,
    ShipmentSlipImportResult,
    ShipmentUploadResult,
    YamatoTrackingExportResult,
    create_shipment_slip_import_csv,
    confirm_next_engine_shipment_sync,
    download_yamato_tracking_export_sync,
    preview_shipment_slip_import,
    upload_next_engine_shipment_csv_sync,
)
from portal_app.services.takaesu_orders import (
    TakaesuOrderDownloadResult,
    TakaesuOrderSheetResult,
    TakaesuOrderWorkflowResult,
    create_takaesu_order_sheet_csv,
    download_takaesu_order_details_sync,
    prepare_takaesu_order_workflow_sync,
    preview_takaesu_order_sheet,
)
from portal_app.services.yamato_b2_workflow import (
    YamatoB2PreparationResult,
    prepare_yamato_b2_sync,
)
from portal_app.services.yamato_b2_import import (
    YamatoB2ImportResult,
    import_yamato_b2_csv_sync,
)
from portal_app.services.yamato_conversion import (
    YamatoConversionResult,
    create_ne_to_yamato_csv,
    preview_ne_to_yamato_conversion,
)


def check() -> int:
    result = analyze_latest_inventory()
    print(f"portal_root={result.paths.portal_root}")
    print(f"master_book={result.paths.master_book}")
    print(f"source_csv={result.source_csv}")
    print(f"source_rows={result.source_rows}")
    print(f"normal_rows={result.normal_count}")
    print(f"choice_rows={result.choice_count}")
    for warning in result.warnings:
        print(f"warning={warning}")
    return 0


def download_next_engine(*, headed: bool, slow_mo_ms: int) -> int:
    result = download_next_engine_order_details_sync(
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    print(f"downloaded_file={result.downloaded_file}")
    print(f"source_filename={result.source_filename}")
    print(f"saved_at={result.saved_at:%Y-%m-%d %H:%M:%S}")

    inventory = analyze_latest_inventory()
    print(f"source_rows={inventory.source_rows}")
    print(f"normal_rows={inventory.normal_count}")
    print(f"choice_rows={inventory.choice_count}")
    return 0


def confirm_next_engine_shipment(
    *,
    execute: bool,
    order_numbers: tuple[str, ...],
    sample_input: str | None,
    expected_contract: str | None,
    fetch_yamato_tracking: bool,
    write_import_csv: bool,
    execute_upload: bool,
    confirm_upload: bool,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> int:
    result = confirm_next_engine_shipment_sync(
        execute=execute,
        order_numbers=order_numbers,
        sample_input=sample_input,
        expected_contract=expected_contract,
        fetch_yamato_tracking=fetch_yamato_tracking,
        write_import_csv=write_import_csv,
        execute_upload=execute_upload,
        confirm_upload=confirm_upload,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
        preview_limit=preview_limit,
    )
    _print_shipment_confirmation_result(result)
    return 2 if execute and result.skipped_reason else 0


def build_shipment_confirmation_csv(
    *,
    write: bool,
    order_numbers: tuple[str, ...],
    preview_limit: int,
    buyer_lookback_days: int | None = None,
    clickpost_lookback_days: int | None = None,
    letterpack_lookback_days: int | None = None,
    yamato_lookback_days: int | None = None,
) -> int:
    result = (
        create_shipment_slip_import_csv(
            order_numbers=order_numbers,
            preview_limit=preview_limit,
            buyer_lookback_days=buyer_lookback_days,
            clickpost_lookback_days=clickpost_lookback_days,
            letterpack_lookback_days=letterpack_lookback_days,
            yamato_lookback_days=yamato_lookback_days,
        )
        if write
        else preview_shipment_slip_import(
            order_numbers=order_numbers,
            preview_limit=preview_limit,
            buyer_lookback_days=buyer_lookback_days,
            clickpost_lookback_days=clickpost_lookback_days,
            letterpack_lookback_days=letterpack_lookback_days,
            yamato_lookback_days=yamato_lookback_days,
        )
    )
    _print_shipment_slip_import_result(result)
    return 0 if not result.warnings else 2


def upload_next_engine_shipment_confirmation(
    *,
    execute: bool,
    confirm_upload: bool,
    csv_file: str | None,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> int:
    result = upload_next_engine_shipment_csv_sync(
        execute=execute,
        confirm_upload=confirm_upload,
        upload_csv=Path(csv_file) if csv_file else None,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
        preview_limit=preview_limit,
    )
    _print_shipment_upload_result(result)
    return 2 if execute and result.skipped_reason else 0


def download_yamato_tracking_export(
    *,
    execute: bool,
    target_date: str | None,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> int:
    result = download_yamato_tracking_export_sync(
        execute=execute,
        target_date=target_date,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
        preview_limit=preview_limit,
    )
    _print_yamato_tracking_export_result(result)
    return 2 if execute and result.skipped_reason else 0


def download_takaesu_order_details(
    *,
    execute: bool,
    target_date: str | None,
    order_numbers: tuple[str, ...],
    sample_input: str | None,
    expected_contract: str | None,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = download_takaesu_order_details_sync(
        execute=execute,
        target_date=target_date,
        order_numbers=order_numbers,
        sample_input=sample_input,
        expected_contract=expected_contract,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_takaesu_order_download_result(result)
    return 2 if execute and result.skipped_reason else 0


def build_takaesu_order_sheet(
    *,
    write: bool,
    source_csv: str | None,
    output_csv: str | None,
    preview_limit: int,
) -> int:
    result = (
        create_takaesu_order_sheet_csv(
            source_csv=Path(source_csv) if source_csv else None,
            output_csv=Path(output_csv) if output_csv else None,
            preview_limit=preview_limit,
        )
        if write
        else preview_takaesu_order_sheet(
            source_csv=Path(source_csv) if source_csv else None,
            preview_limit=preview_limit,
        )
    )
    _print_takaesu_order_sheet_result(result)
    return 0 if not result.warnings else 2


def prepare_takaesu_order_sheet(
    *,
    dry_run: bool,
    execute_download: bool,
    write_order_sheet: bool,
    source_csv: str | None,
    output_csv: str | None,
    sample_input: str | None,
    expected_contract: str | None,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> int:
    result = prepare_takaesu_order_workflow_sync(
        dry_run=dry_run,
        execute_download=execute_download,
        write_order_sheet=write_order_sheet,
        source_csv=Path(source_csv) if source_csv else None,
        output_csv=Path(output_csv) if output_csv else None,
        sample_input=sample_input,
        expected_contract=expected_contract,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
        preview_limit=preview_limit,
    )
    _print_takaesu_order_workflow_result(result)
    return 0 if not (result.order_sheet and result.order_sheet.warnings) else 2


def download_ne02_order_details(
    *,
    execute: bool,
    target_date: str | None,
    order_numbers: tuple[str, ...],
    sample_input: str | None,
    expected_contract: str | None,
) -> int:
    result = download_ne02_order_details_sync(
        execute=execute,
        target_date=target_date,
        order_numbers=order_numbers,
        sample_input=sample_input,
        expected_contract=expected_contract,
    )
    _print_ne02_order_detail_download_result(result)
    return 2 if execute and result.skipped_reason else 0


def inspect_next_engine_order(*, order_no: str, headed: bool, slow_mo_ms: int) -> int:
    snapshot = inspect_next_engine_order_sync(
        order_no,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_snapshot("current", snapshot)
    return 0


def restore_next_engine_print_wait(
    *,
    order_no: str,
    execute: bool,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = restore_next_engine_print_wait_sync(
        order_no,
        execute=execute,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    print(f"order_no={result.order_no}")
    print(f"executed={result.executed}")
    print(f"changed={result.changed}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    _print_snapshot("before", result.before)
    if result.after_clear:
        _print_snapshot("after_clear", result.after_clear)
    if result.after_restore:
        _print_snapshot("after_restore", result.after_restore)
    for index, message in enumerate(result.dialog_messages, start=1):
        print(f"dialog_{index}={message}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    if result.skipped_reason and result.skipped_reason.startswith("restore_failed"):
        return 2
    return 0


def restore_next_engine_print_wait_batch(
    *,
    order_numbers: tuple[str, ...],
    execute: bool,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = restore_next_engine_print_wait_batch_sync(
        order_numbers,
        execute=execute,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_restore_batch_result(result)
    return 0 if not result.failed_order_numbers else 2


def test_next_engine_invoice_download(
    *,
    order_no: str,
    execute: bool,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = test_invoice_download_and_restore_sync(
        order_no,
        execute=execute,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_invoice_result(result)
    return 0


def download_next_engine_yamato_invoices(
    *,
    execute: bool,
    order_numbers: tuple[str, ...],
    verify_statuses: bool,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = download_yamato_invoice_batch_sync(
        execute=execute,
        order_numbers_filter=order_numbers,
        verify_statuses=verify_statuses,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_invoice_batch_result(result)
    return 0 if not result.error and not result.skipped_reason else 2


def inspect_next_engine_yamato_orders(*, headed: bool, slow_mo_ms: int) -> int:
    snapshot = inspect_yamato_order_list_sync(
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_yamato_snapshot("yamato", snapshot)
    return 0


def download_next_engine_yamato_buyer(
    *,
    execute: bool,
    order_numbers: tuple[str, ...],
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = download_yamato_buyer_data_sync(
        execute=execute,
        order_numbers_filter=order_numbers,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_yamato_buyer_result(result)
    return 0


def download_next_engine_yamato_product(
    *,
    execute: bool,
    output_type: str,
    order_numbers: tuple[str, ...],
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = download_yamato_product_data_sync(
        execute=execute,
        output_type=output_type,
        order_numbers_filter=order_numbers,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_yamato_product_result(result)
    return 0


def download_next_engine_yamato_custom_shipping(
    *,
    execute: bool,
    order_numbers: tuple[str, ...],
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = download_yamato_custom_shipping_data_sync(
        execute=execute,
        order_numbers_filter=order_numbers,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_yamato_custom_shipping_result(result)
    return 0


def inspect_next_engine_clickpost_orders(*, headed: bool, slow_mo_ms: int) -> int:
    snapshot = inspect_clickpost_order_list_sync(
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_clickpost_snapshot("clickpost", snapshot)
    return 0


def download_next_engine_clickpost_buyer(*, execute: bool, headed: bool, slow_mo_ms: int) -> int:
    result = download_clickpost_buyer_data_sync(
        execute=execute,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_clickpost_buyer_result(result)
    return 0


def download_next_engine_clickpost_product(
    *,
    execute: bool,
    output_type: str,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = download_clickpost_product_data_sync(
        execute=execute,
        output_type=output_type,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_clickpost_product_result(result)
    return 0


def convert_clickpost_csv(
    *,
    write: bool,
    buyer_csv: str | None,
    product_csv: str | None,
    preview_limit: int,
) -> int:
    buyer_path = Path(buyer_csv) if buyer_csv else None
    product_path = Path(product_csv) if product_csv else None
    result = (
        create_clickpost_csv(
            buyer_csv=buyer_path,
            product_csv=product_path,
            preview_limit=preview_limit,
        )
        if write
        else preview_clickpost_csv(
            buyer_csv=buyer_path,
            product_csv=product_path,
            preview_limit=preview_limit,
        )
    )
    _print_clickpost_conversion_result(result)
    return 0


def convert_letterpack_addresses(
    *,
    write: bool,
    buyer_csv: str | None,
    product_csv: str | None,
    preview_limit: int,
) -> int:
    buyer_path = Path(buyer_csv) if buyer_csv else None
    product_path = Path(product_csv) if product_csv else None
    result = (
        create_letterpack_address_csv(
            buyer_csv=buyer_path,
            product_csv=product_path,
            preview_limit=preview_limit,
        )
        if write
        else preview_letterpack_addresses(
            buyer_csv=buyer_path,
            product_csv=product_path,
            preview_limit=preview_limit,
        )
    )
    _print_letterpack_address_result(result)
    return 0


def create_letterpack_pdf(
    *,
    address_csv: str | None,
    output_pdf: str | None,
    refresh_address_csv: bool,
    preview_limit: int,
    message_box: bool,
) -> int:
    result = create_letterpack_label_pdf(
        address_csv=Path(address_csv) if address_csv else None,
        output_pdf=Path(output_pdf) if output_pdf else None,
        refresh_address_csv=refresh_address_csv,
        preview_limit=preview_limit,
    )
    _print_letterpack_label_pdf_result(result)
    if message_box and result.output_pdf:
        _show_message_box(
            "LetterPack PDF saved",
            f"PDF saved.\n\nFolder:\n{result.output_pdf.parent}\n\nFile:\n{result.output_pdf.name}",
        )
    return 0 if result.output_pdf else 2


def reflect_clickpost_tracking(
    *,
    write: bool,
    tracking_csv: str,
    buyer_csv: str | None,
    preview_limit: int,
) -> int:
    result = (
        create_clickpost_tracking_reflection_csv(
            tracking_csv=Path(tracking_csv),
            buyer_csv=Path(buyer_csv) if buyer_csv else None,
            preview_limit=preview_limit,
        )
        if write
        else preview_clickpost_tracking_reflection(
            tracking_csv=Path(tracking_csv),
            buyer_csv=Path(buyer_csv) if buyer_csv else None,
            preview_limit=preview_limit,
        )
    )
    _print_clickpost_tracking_reflection_result(result)
    return 0 if not result.warnings else 2


def upload_clickpost_csv(
    *,
    execute: bool,
    csv_file: str | None,
    headed: bool,
    slow_mo_ms: int,
    wait_at_payment_seconds: int,
) -> int:
    result = upload_clickpost_csv_sync(
        csv_file=Path(csv_file) if csv_file else None,
        execute=execute,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
        wait_at_payment_seconds=wait_at_payment_seconds,
    )
    _print_clickpost_upload_result(result)
    return 0 if result.ready_for_payment or not result.executed else 2


def complete_clickpost_payment_print(
    *,
    execute: bool,
    output_dir: str | None,
    headed: bool,
    slow_mo_ms: int,
    max_payments: int,
    message_box: bool,
) -> int:
    print("skipped_reason=disabled_existing_payment_recovery")
    print("use_command=run-clickpost-import-payment-print")
    if execute:
        print("error=ClickPost payment/print must start from CSV import for this workflow.")
        return 2
    return 0


def run_clickpost_import_payment_print(
    *,
    execute: bool,
    csv_file: str | None,
    output_dir: str | None,
    headed: bool,
    slow_mo_ms: int,
    max_payments: int,
    message_box: bool,
) -> int:
    result = import_pay_print_clickpost_csv_sync(
        csv_file=Path(csv_file) if csv_file else None,
        execute=execute,
        output_dir=Path(output_dir) if output_dir else None,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
        max_payments=max_payments,
    )
    _print_clickpost_import_payment_print_result(result)
    if message_box and result.downloaded_pdf:
        _show_message_box(
            "ClickPost PDF saved",
            f"PDF saved.\n\nFolder:\n{result.download_dir}\n\nFile:\n{result.downloaded_pdf.name}",
        )
    return 0 if (not result.executed or result.downloaded_pdf) else 2


def export_clickpost_tracking_for_import_csv(
    *,
    execute: bool,
    csv_file: str | None,
    output_dir: str | None,
    update_workbook: bool,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = export_clickpost_tracking_for_csv_sync(
        csv_file=Path(csv_file) if csv_file else None,
        execute=execute,
        output_dir=Path(output_dir) if output_dir else None,
        update_workbook=update_workbook,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_clickpost_tracking_export_result(result)
    return 0 if (not result.executed or result.tracking_rows == result.target_rows) else 2


def prepare_clickpost(
    *,
    dry_run: bool,
    sample_input: str | None,
    expected_contract: str | None,
    fetch_next_engine: bool,
    execute_downloads: bool,
    write_conversion: bool,
    write_letterpack_addresses: bool,
    tracking_csv: str | None,
    write_tracking_reflection: bool,
    upload: bool,
    execute_upload: bool,
    output_type: str,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> int:
    if dry_run:
        _print_flow_dry_run_plan(
            flow_id="ed415d6f-ab6e-421b-ae62-0e5a0b7a70f3",
            flow_name="クリックポストver3_sharepoint移行後",
            sample_input=sample_input,
            expected_contract=expected_contract,
            steps=(
                "prepare-clickpost: plan buyer/product download, CSV conversion, and optional upload",
                "download-next-engine-clickpost-buyer: Next Engine buyer CSV boundary",
                "download-next-engine-clickpost-product: Next Engine product CSV boundary",
                "convert-clickpost-csv: direct CSV conversion boundary",
                "convert-letterpack-addresses: PAD letterpack Excel macro replacement boundary",
                "upload-clickpost-csv: side-effect upload boundary",
                "reflect-clickpost-tracking: PAD okurizyo_hanei tracking reflection table boundary",
            ),
        )
        return 0

    result = prepare_clickpost_sync(
        fetch_next_engine=fetch_next_engine,
        execute_downloads=execute_downloads,
        write_conversion=write_conversion,
        write_letterpack_addresses=write_letterpack_addresses,
        tracking_csv=Path(tracking_csv) if tracking_csv else None,
        write_tracking_reflection=write_tracking_reflection,
        upload=upload,
        execute_upload=execute_upload,
        output_type=output_type,
        headed=headed,
        slow_mo_ms=slow_mo_ms,
        preview_limit=preview_limit,
    )
    _print_clickpost_preparation_result(result)
    return 0 if not result.consistency_warnings else 2


def convert_yamato_ne_to_b2(
    *,
    write: bool,
    source_csv: str | None,
    preview_limit: int,
) -> int:
    source_path = Path(source_csv) if source_csv else None
    result = (
        create_ne_to_yamato_csv(source_csv=source_path, preview_limit=preview_limit)
        if write
        else preview_ne_to_yamato_conversion(source_csv=source_path, preview_limit=preview_limit)
    )
    _print_yamato_conversion_result(result)
    return 0


def prepare_yamato_b2(
    *,
    dry_run: bool,
    sample_input: str | None,
    expected_contract: str | None,
    fetch_next_engine: bool,
    execute_downloads: bool,
    check_invoices: bool,
    execute_invoices: bool,
    verify_invoice_statuses: bool,
    check_custom_shipping: bool,
    execute_custom_shipping: bool,
    custom_shipping_order_numbers: tuple[str, ...],
    write_conversion: bool,
    output_type: str,
    headed: bool,
    slow_mo_ms: int,
    preview_limit: int,
) -> int:
    if dry_run:
        _print_flow_dry_run_plan(
            flow_id="54214037-e937-44c7-997c-db677b6bf167",
            flow_name="ヤマト伝票作成_sharepoint移行後",
            sample_input=sample_input,
            expected_contract=expected_contract,
            steps=(
                "prepare-yamato-b2: plan buyer/product/invoice/custom-shipping retrieval and B2 conversion",
                "download-next-engine-yamato-buyer: Next Engine buyer CSV boundary",
                "download-next-engine-yamato-product: Next Engine product CSV boundary",
                "download-next-engine-yamato-invoices: invoice PDF side-effect boundary",
                "download-next-engine-yamato-custom-shipping: custom-shipping CSV side-effect boundary",
                "convert-yamato-ne-to-b2: direct CSV conversion boundary",
            ),
        )
        return 0

    result = prepare_yamato_b2_sync(
        fetch_next_engine=fetch_next_engine,
        execute_downloads=execute_downloads,
        check_invoices=check_invoices,
        execute_invoices=execute_invoices,
        verify_invoice_statuses=verify_invoice_statuses,
        check_custom_shipping=check_custom_shipping,
        execute_custom_shipping=execute_custom_shipping,
        custom_shipping_order_numbers=custom_shipping_order_numbers,
        write_conversion=write_conversion,
        output_type=output_type,
        headed=headed,
        slow_mo_ms=slow_mo_ms,
        preview_limit=preview_limit,
    )
    _print_yamato_b2_preparation_result(result)
    return 0 if not result.consistency_warnings else 2


def import_yamato_b2(
    *,
    csv_file: str | None,
    check_login: bool,
    open_import_page: bool,
    select_file_dry_run: bool,
    execute_import: bool,
    confirm_import: bool,
    headed: bool,
    slow_mo_ms: int,
) -> int:
    result = import_yamato_b2_csv_sync(
        csv_file=Path(csv_file) if csv_file else None,
        check_login=check_login,
        open_import_page=open_import_page,
        select_file_dry_run=select_file_dry_run,
        execute_import=execute_import,
        confirm_import=confirm_import,
        headless=not headed,
        slow_mo_ms=slow_mo_ms,
    )
    _print_yamato_b2_import_result(result)
    return 0


def _print_snapshot(label: str, snapshot: OrderStatusSnapshot) -> None:
    print(f"{label}_order_no={snapshot.order_no}")
    print(f"{label}_status_value={snapshot.status_value}")
    print(f"{label}_status_text={snapshot.status_text}")
    print(f"{label}_confirmation_checked={snapshot.confirmation_checked}")
    print(f"{label}_confirmation_hidden_value={snapshot.confirmation_hidden_value}")
    print(f"{label}_captured_at={snapshot.captured_at:%Y-%m-%d %H:%M:%S}")


def _print_shipment_confirmation_result(result: ShipmentConfirmationResult) -> None:
    print(f"flow_id={result.flow_id}")
    print(f"flow_name={result.flow_name}")
    print(f"executed={result.executed}")
    print(f"order_numbers={','.join(result.order_numbers)}")
    if result.source_sample_input:
        print(f"sample_input={result.source_sample_input}")
    if result.expected_contract:
        print(f"expected_contract={result.expected_contract}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.shipment_import:
        print(f"shipment_import_rows={result.shipment_import.target_rows}")
        print(f"shipment_import_warnings={len(result.shipment_import.warnings)}")
    if result.yamato_tracking_export:
        print(f"yamato_tracking_ready={result.yamato_tracking_export.ready_to_import}")
        print(f"yamato_tracking_rows={result.yamato_tracking_export.source_rows}")
        if result.yamato_tracking_export.skipped_reason:
            print(f"yamato_tracking_skipped_reason={result.yamato_tracking_export.skipped_reason}")
    if result.shipment_upload:
        print(f"shipment_upload_ready={result.shipment_upload.ready_to_upload}")
        print(f"shipment_upload_rows={result.shipment_upload.source_rows}")
        if result.shipment_upload.skipped_reason:
            print(f"shipment_upload_skipped_reason={result.shipment_upload.skipped_reason}")
    for index, step in enumerate(result.steps, start=1):
        print(f"step_{index}={step.subflow}:{step.status}:{step.target}")
    for side_effect in result.side_effects:
        print(f"side_effect={side_effect}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_shipment_slip_import_result(result: ShipmentSlipImportResult) -> None:
    print(f"target_order_numbers={','.join(result.target_order_numbers)}")
    print(f"buyer_rows={result.buyer_rows}")
    print(f"tracking_rows={result.tracking_rows}")
    print(f"target_rows={result.target_rows}")
    print(f"output_rows={result.output_rows}")
    if result.output_csv:
        print(f"output_csv={result.output_csv}")
    else:
        print("output_csv=(dry_run)")
    print(f"source_files={len(result.source_files)}")
    print(f"scanned_count={result.scanned_count}")
    print(f"duplicate_count={result.duplicate_count}")
    print(f"buyer_matched_count={result.buyer_matched_count}")
    print(f"tracking_matched_count={result.tracking_matched_count}")
    print(f"unresolved_count={result.unresolved_count}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    for warning in result.warnings:
        print(f"warning={warning}")
    print(f"preview_rows={len(result.preview_rows)}")


def _print_shipment_upload_result(result: ShipmentUploadResult) -> None:
    print(f"executed={result.executed}")
    print(f"ready_to_upload={result.ready_to_upload}")
    if result.upload_csv:
        print(f"upload_csv={result.upload_csv}")
    print(f"source_rows={result.source_rows}")
    print(f"source_headers={','.join(result.source_headers)}")
    print(f"preview_rows={len(result.preview_rows)}")
    if result.confirmation_text:
        print(f"confirmation_text={result.confirmation_text[:300]}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    for error in result.errors:
        print(f"error={error}")
    for warning in result.warnings:
        print(f"warning={warning}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_yamato_tracking_export_result(result: YamatoTrackingExportResult) -> None:
    print(f"executed={result.executed}")
    print(f"target_date={result.target_date}")
    print(f"ready_to_import={result.ready_to_import}")
    if result.export_csv:
        print(f"export_csv={result.export_csv}")
    print(f"source_rows={result.source_rows}")
    print(f"source_headers={','.join(result.source_headers)}")
    print(f"preview_rows={len(result.preview_rows)}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    for warning in result.warnings:
        print(f"warning={warning}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_takaesu_order_download_result(result: TakaesuOrderDownloadResult) -> None:
    print(f"flow_id={result.flow_id}")
    print(f"flow_name={result.flow_name}")
    print(f"executed={result.executed}")
    if result.target_date:
        print(f"target_date={result.target_date}")
    print(f"order_numbers={','.join(result.order_numbers)}")
    if result.source_sample_input:
        print(f"sample_input={result.source_sample_input}")
    if result.expected_contract:
        print(f"expected_contract={result.expected_contract}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.source_filename:
        print(f"source_filename={result.source_filename}")
    if result.output_rows is not None:
        print(f"output_rows={result.output_rows}")
    if result.order_sheet:
        print(f"order_sheet_rows={result.order_sheet.output_rows}")
        print(f"order_sheet_warnings={len(result.order_sheet.warnings)}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    for index, step in enumerate(result.steps, start=1):
        print(f"step_{index}={step.subflow}:{step.status}:{step.target}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_takaesu_order_sheet_result(result: TakaesuOrderSheetResult) -> None:
    print(f"source_csv={result.source_csv}")
    print(f"master_book={result.master_book}")
    print(f"order_workbook={result.order_workbook}")
    print(f"output_csv={result.output_csv}")
    print(f"source_rows={result.source_rows}")
    print(f"normal_rows={result.normal_rows}")
    print(f"choice_rows={result.choice_rows}")
    print(f"output_rows={result.output_rows}")
    for warning in result.warnings:
        print(f"warning={warning}")
    for index, row in enumerate(result.preview_rows, 1):
        print(f"preview_row_{index}={row}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_takaesu_order_workflow_result(result: TakaesuOrderWorkflowResult) -> None:
    print(f"flow_id={result.flow_id}")
    print(f"flow_name={result.flow_name}")
    print(f"executed={result.executed}")
    print(f"wrote_order_sheet={result.wrote_order_sheet}")
    if result.source_sample_input:
        print(f"sample_input={result.source_sample_input}")
    if result.expected_contract:
        print(f"expected_contract={result.expected_contract}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.download:
        print(f"download_executed={result.download.executed}")
        if result.download.downloaded_file:
            print(f"downloaded_file={result.download.downloaded_file}")
        if result.download.output_rows is not None:
            print(f"download_output_rows={result.download.output_rows}")
    if result.order_sheet:
        print(f"order_sheet_source_csv={result.order_sheet.source_csv}")
        print(f"order_sheet_output_csv={result.order_sheet.output_csv}")
        print(f"order_sheet_rows={result.order_sheet.output_rows}")
        print(f"order_sheet_warnings={len(result.order_sheet.warnings)}")
    for index, step in enumerate(result.steps, start=1):
        print(f"step_{index}={step.subflow}:{step.status}:{step.target}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_ne02_order_detail_download_result(result: Ne02OrderDetailDownloadResult) -> None:
    print(f"flow_id={result.flow_id}")
    print(f"flow_name={result.flow_name}")
    print(f"executed={result.executed}")
    if result.target_date:
        print(f"target_date={result.target_date}")
    print(f"order_numbers={','.join(result.order_numbers)}")
    if result.source_sample_input:
        print(f"sample_input={result.source_sample_input}")
    if result.expected_contract:
        print(f"expected_contract={result.expected_contract}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.output_rows is not None:
        print(f"output_rows={result.output_rows}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    for index, step in enumerate(result.steps, start=1):
        print(f"step_{index}={step.subflow}:{step.status}:{step.target}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_flow_dry_run_plan(
    *,
    flow_id: str,
    flow_name: str,
    sample_input: str | None,
    expected_contract: str | None,
    steps: tuple[str, ...],
) -> None:
    print(f"flow_id={flow_id}")
    print(f"flow_name={flow_name}")
    print("executed=False")
    print("dry_run=True")
    if sample_input:
        print(f"sample_input={sample_input}")
    if expected_contract:
        print(f"expected_contract={expected_contract}")
    for index, step in enumerate(steps, start=1):
        print(f"step_{index}={step}")


def _print_invoice_result(result: InvoiceDownloadTestResult) -> None:
    print(f"order_no={result.order_no}")
    print(f"executed={result.executed}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.error:
        print(f"error={result.error}")
    _print_snapshot("before", result.before)
    if result.after_download:
        _print_snapshot("after_download", result.after_download)
    if result.restore_result:
        print(f"restore_changed={result.restore_result.changed}")
        if result.restore_result.skipped_reason:
            print(f"restore_skipped_reason={result.restore_result.skipped_reason}")
        _print_snapshot("restore_before", result.restore_result.before)
        if result.restore_result.after_clear:
            _print_snapshot("restore_after_clear", result.restore_result.after_clear)
        if result.restore_result.after_restore:
            _print_snapshot("restore_after_restore", result.restore_result.after_restore)
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_invoice_batch_result(result: InvoiceBatchDownloadResult) -> None:
    print(f"executed={result.executed}")
    _print_yamato_snapshot("before_list", result.before_list)
    print(f"status_verified={result.status_verified}")
    print(f"before_status_count={len(result.before_statuses)}")
    print(f"before_status_summary={_status_summary(result.before_statuses)}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    print(f"after_status_count={len(result.after_statuses)}")
    print(f"after_status_summary={_status_summary(result.after_statuses)}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.error:
        print(f"error={result.error}")
    for index, message in enumerate(result.dialog_messages, start=1):
        print(f"dialog_{index}={message}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_restore_batch_result(result: OrderStatusBatchRestoreResult) -> None:
    print(f"executed={result.executed}")
    print(f"target_count={len(result.order_numbers)}")
    print(f"result_count={len(result.results)}")
    if result.failed_order_numbers:
        print(f"failed_order_numbers={','.join(result.failed_order_numbers)}")
    for item in result.results:
        final_snapshot = item.after_restore or item.after_clear or item.before
        print(
            f"order_status={item.order_no},"
            f"before={item.before.status_value}:{item.before.status_text},"
            f"after={final_snapshot.status_value}:{final_snapshot.status_text},"
            f"changed={item.changed},"
            f"skipped={item.skipped_reason or ''}"
        )
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _status_summary(snapshots: tuple[OrderStatusSnapshot, ...]) -> str:
    counts: dict[str, int] = {}
    for snapshot in snapshots:
        label = snapshot.status_text or snapshot.status_value
        counts[label] = counts.get(label, 0) + 1
    return ",".join(f"{label}:{count}" for label, count in sorted(counts.items()))


def _parse_order_numbers(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return tuple()
    return tuple(value for value in re.split(r"[\s,]+", raw.strip()) if value)


def _print_yamato_snapshot(label: str, snapshot: YamatoOrderListSnapshot) -> None:
    print(f"{label}_count={snapshot.count}")
    print(f"{label}_selected_shipping_options={','.join(snapshot.selected_shipping_options)}")
    print(f"{label}_captured_at={snapshot.captured_at:%Y-%m-%d %H:%M:%S}")
    print(f"{label}_order_numbers={','.join(snapshot.order_numbers)}")


def _print_yamato_buyer_result(result: YamatoBuyerDownloadResult) -> None:
    print(f"executed={result.executed}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.source_filename:
        print(f"source_filename={result.source_filename}")
    _print_yamato_snapshot("snapshot", result.snapshot)
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_yamato_product_result(result: YamatoProductDownloadResult) -> None:
    print(f"executed={result.executed}")
    print(f"output_type={result.output_type}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.source_filename:
        print(f"source_filename={result.source_filename}")
    _print_yamato_snapshot("snapshot", result.snapshot)
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_yamato_custom_shipping_result(result: YamatoCustomShippingDownloadResult) -> None:
    print(f"executed={result.executed}")
    print(f"pattern_name={result.pattern_name}")
    print(f"ready_to_download={result.ready_to_download}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.order_numbers_filter:
        print(f"order_numbers_filter={','.join(result.order_numbers_filter)}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.source_filename:
        print(f"source_filename={result.source_filename}")
    if result.warning_text:
        print(f"warning_text={result.warning_text}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_clickpost_snapshot(label: str, snapshot: ClickPostOrderListSnapshot) -> None:
    print(f"{label}_count={snapshot.count}")
    print(f"{label}_selected_shipping_options={','.join(snapshot.selected_shipping_options)}")
    print(f"{label}_captured_at={snapshot.captured_at:%Y-%m-%d %H:%M:%S}")
    print(f"{label}_order_numbers={','.join(snapshot.order_numbers)}")


def _print_clickpost_buyer_result(result: ClickPostBuyerDownloadResult) -> None:
    print(f"executed={result.executed}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.source_filename:
        print(f"source_filename={result.source_filename}")
    _print_clickpost_snapshot("snapshot", result.snapshot)
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_clickpost_product_result(result: ClickPostProductDownloadResult) -> None:
    print(f"executed={result.executed}")
    print(f"output_type={result.output_type}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.downloaded_file:
        print(f"downloaded_file={result.downloaded_file}")
    if result.source_filename:
        print(f"source_filename={result.source_filename}")
    _print_clickpost_snapshot("snapshot", result.snapshot)
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_clickpost_conversion_result(result: ClickPostConversionResult) -> None:
    print(f"buyer_csv={result.buyer_csv}")
    print(f"product_csv={result.product_csv}")
    print(f"buyer_rows={result.buyer_rows}")
    print(f"product_rows={result.product_rows}")
    print(f"target_rows={result.target_rows}")
    print(f"output_rows={result.output_rows}")
    if result.output_csv:
        print(f"output_csv={result.output_csv}")
    else:
        print("output_csv=(dry_run)")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    for warning in result.warnings:
        print(f"warning={warning}")
    print(f"preview_rows={len(result.preview_rows)}")


def _print_letterpack_address_result(result: LetterPackAddressResult) -> None:
    print(f"buyer_csv={result.buyer_csv}")
    print(f"product_csv={result.product_csv}")
    print(f"buyer_rows={result.buyer_rows}")
    print(f"product_rows={result.product_rows}")
    print(f"target_rows={result.target_rows}")
    print(f"output_rows={result.output_rows}")
    if result.output_csv:
        print(f"output_csv={result.output_csv}")
    else:
        print("output_csv=(dry_run)")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    for warning in result.warnings:
        print(f"warning={warning}")
    print(f"preview_rows={len(result.preview_rows)}")


def _print_letterpack_label_pdf_result(result: LetterPackLabelPdfResult) -> None:
    print(f"address_csv={result.address_csv}")
    print(f"output_rows={result.output_rows}")
    print(f"page_count={result.page_count}")
    if result.output_pdf:
        print(f"output_pdf={result.output_pdf}")
    else:
        print("output_pdf=(not_created)")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    for warning in result.warnings:
        print(f"warning={warning}")
    print(f"preview_rows={len(result.preview_rows)}")


def _print_clickpost_tracking_reflection_result(result: ClickPostTrackingReflectionResult) -> None:
    print(f"buyer_csv={result.buyer_csv}")
    print(f"tracking_csv={result.tracking_csv}")
    print(f"buyer_rows={result.buyer_rows}")
    print(f"tracking_rows={result.tracking_rows}")
    print(f"target_rows={result.target_rows}")
    print(f"output_rows={result.output_rows}")
    if result.output_csv:
        print(f"output_csv={result.output_csv}")
    else:
        print("output_csv=(dry_run)")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    for warning in result.warnings:
        print(f"warning={warning}")
    print(f"preview_rows={len(result.preview_rows)}")


def _print_clickpost_upload_result(result: ClickPostUploadResult) -> None:
    print(f"executed={result.executed}")
    print(f"csv_file={result.csv_file}")
    print(f"target_rows={result.target_rows}")
    print(f"ready_for_payment={result.ready_for_payment}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.warning_text:
        print(f"warning_text={result.warning_text}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_clickpost_payment_print_result(result: ClickPostPaymentPrintResult) -> None:
    print(f"executed={result.executed}")
    print(f"payment_attempts={result.payment_attempts}")
    print(f"payments_completed={result.payments_completed}")
    print(f"remaining_payment_buttons={result.remaining_payment_buttons}")
    print(f"print_target_rows={result.print_target_rows}")
    print(f"download_dir={result.download_dir}")
    if result.downloaded_pdf:
        print(f"downloaded_pdf={result.downloaded_pdf}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.warning_text:
        print(f"warning_text={result.warning_text}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_clickpost_import_payment_print_result(result: ClickPostImportPaymentPrintResult) -> None:
    print(f"executed={result.executed}")
    print(f"csv_file={result.csv_file}")
    print(f"csv_sha256={result.csv_sha256}")
    print(f"target_rows={result.target_rows}")
    print(f"ready_for_payment={result.ready_for_payment}")
    print(f"payment_attempts={result.payment_attempts}")
    print(f"payments_completed={result.payments_completed}")
    print(f"remaining_payment_buttons={result.remaining_payment_buttons}")
    print(f"print_target_rows={result.print_target_rows}")
    print(f"download_dir={result.download_dir}")
    if result.downloaded_pdf:
        print(f"downloaded_pdf={result.downloaded_pdf}")
    print(f"tracking_rows={result.tracking_rows}")
    if result.tracking_csv:
        print(f"tracking_csv={result.tracking_csv}")
    print(f"workbook_updated={result.workbook_updated}")
    if result.workbook_path:
        print(f"workbook_path={result.workbook_path}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.warning_text:
        print(f"warning_text={result.warning_text}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _print_clickpost_tracking_export_result(result: ClickPostTrackingExportResult) -> None:
    print(f"executed={result.executed}")
    print(f"csv_file={result.csv_file}")
    print(f"target_rows={result.target_rows}")
    print(f"tracking_rows={result.tracking_rows}")
    if result.output_csv:
        print(f"output_csv={result.output_csv}")
    print(f"workbook_updated={result.workbook_updated}")
    if result.workbook_path:
        print(f"workbook_path={result.workbook_path}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.warning_text:
        print(f"warning_text={result.warning_text}")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")


def _show_message_box(title: str, message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0)
    except Exception:
        pass


def _print_clickpost_preparation_result(result: ClickPostPreparationResult) -> None:
    print(f"audit_path={result.audit_path}")
    if result.buyer:
        print(f"buyer_executed={result.buyer.executed}")
        print(f"buyer_count={result.buyer.snapshot.count}")
        if result.buyer.downloaded_file:
            print(f"buyer_downloaded_file={result.buyer.downloaded_file}")
        if result.buyer.skipped_reason:
            print(f"buyer_skipped_reason={result.buyer.skipped_reason}")
    else:
        print("buyer=(skipped)")

    if result.product:
        print(f"product_executed={result.product.executed}")
        print(f"product_count={result.product.snapshot.count}")
        if result.product.downloaded_file:
            print(f"product_downloaded_file={result.product.downloaded_file}")
        if result.product.skipped_reason:
            print(f"product_skipped_reason={result.product.skipped_reason}")
    else:
        print("product=(skipped)")

    print(f"conversion_buyer_csv={result.conversion.buyer_csv}")
    print(f"conversion_product_csv={result.conversion.product_csv}")
    print(f"conversion_target_rows={result.conversion.target_rows}")
    if result.conversion.output_csv:
        print(f"conversion_output_csv={result.conversion.output_csv}")
    else:
        print("conversion_output_csv=(dry_run)")
    print(f"letterpack_target_rows={result.letterpack.target_rows}")
    if result.letterpack.output_csv:
        print(f"letterpack_output_csv={result.letterpack.output_csv}")
    else:
        print("letterpack_output_csv=(dry_run)")
    if result.tracking_reflection:
        print(f"tracking_reflection_rows={result.tracking_reflection.target_rows}")
        if result.tracking_reflection.output_csv:
            print(f"tracking_reflection_output_csv={result.tracking_reflection.output_csv}")
        else:
            print("tracking_reflection_output_csv=(dry_run)")
    else:
        print("tracking_reflection=(skipped)")

    if result.upload:
        print(f"upload_executed={result.upload.executed}")
        print(f"upload_ready_for_payment={result.upload.ready_for_payment}")
        if result.upload.skipped_reason:
            print(f"upload_skipped_reason={result.upload.skipped_reason}")
    else:
        print("upload=(skipped)")

    for warning in result.consistency_warnings:
        print(f"consistency_warning={warning}")


def _print_yamato_conversion_result(result: YamatoConversionResult) -> None:
    print(f"source_csv={result.source_csv}")
    print(f"master_book={result.master_book}")
    print(f"source_rows={result.source_rows}")
    print(f"output_rows={result.output_rows}")
    print(f"duplicate_rows_removed={result.duplicate_rows_removed}")
    print(f"item_master_rows={result.item_master_rows}")
    print(f"address_adjusted_rows={result.address_adjusted_rows}")
    print(f"address_review_rows={result.address_review_rows}")
    if result.output_csv:
        print(f"output_csv={result.output_csv}")
    else:
        print("output_csv=(dry_run)")
    if result.audit_path:
        print(f"audit_path={result.audit_path}")
    if result.unmapped_item_codes:
        print("unmapped_item_codes=" + ",".join(result.unmapped_item_codes))
    for warning in result.warnings:
        print(f"warning={warning}")
    for index, item in enumerate(result.address_reviews[:20], start=1):
        status = "review" if item.requires_review else "adjusted"
        print(
            f"address_{index}={status},row={item.row_number},"
            f"order_no={item.order_no},reasons={';'.join(item.reasons)}"
        )
    print(f"preview_rows={len(result.preview_rows)}")


def _print_yamato_b2_preparation_result(result: YamatoB2PreparationResult) -> None:
    print(f"audit_path={result.audit_path}")
    if result.buyer:
        print(f"buyer_executed={result.buyer.executed}")
        print(f"buyer_count={result.buyer.snapshot.count}")
        if result.buyer.downloaded_file:
            print(f"buyer_downloaded_file={result.buyer.downloaded_file}")
        if result.buyer.skipped_reason:
            print(f"buyer_skipped_reason={result.buyer.skipped_reason}")
    else:
        print("buyer=(skipped)")

    if result.product:
        print(f"product_executed={result.product.executed}")
        print(f"product_count={result.product.snapshot.count}")
        print(f"product_output_type={result.product.output_type}")
        if result.product.downloaded_file:
            print(f"product_downloaded_file={result.product.downloaded_file}")
        if result.product.skipped_reason:
            print(f"product_skipped_reason={result.product.skipped_reason}")
    else:
        print("product=(skipped)")

    if result.invoice:
        print(f"invoice_executed={result.invoice.executed}")
        print(f"invoice_before_count={result.invoice.before_list.count}")
        print(f"invoice_status_verified={result.invoice.status_verified}")
        print(f"invoice_before_status_summary={_status_summary(result.invoice.before_statuses)}")
        print(f"invoice_after_status_summary={_status_summary(result.invoice.after_statuses)}")
        if result.invoice.downloaded_file:
            print(f"invoice_downloaded_file={result.invoice.downloaded_file}")
        if result.invoice.skipped_reason:
            print(f"invoice_skipped_reason={result.invoice.skipped_reason}")
        if result.invoice.error:
            print(f"invoice_error={result.invoice.error}")
    else:
        print("invoice=(skipped)")

    if result.custom_shipping:
        print(f"custom_shipping_executed={result.custom_shipping.executed}")
        print(f"custom_shipping_ready_to_download={result.custom_shipping.ready_to_download}")
        if result.custom_shipping.downloaded_file:
            print(f"custom_shipping_downloaded_file={result.custom_shipping.downloaded_file}")
        if result.custom_shipping.skipped_reason:
            print(f"custom_shipping_skipped_reason={result.custom_shipping.skipped_reason}")
    else:
        print("custom_shipping=(skipped)")

    print(f"conversion_source_csv={result.conversion.source_csv}")
    if result.conversion.output_csv:
        print(f"conversion_output_csv={result.conversion.output_csv}")
    else:
        print("conversion_output_csv=(dry_run)")
    print(f"conversion_source_rows={result.conversion.source_rows}")
    print(f"conversion_output_rows={result.conversion.output_rows}")
    print(f"address_adjusted_rows={result.conversion.address_adjusted_rows}")
    print(f"address_review_rows={result.conversion.address_review_rows}")
    for warning in result.consistency_warnings:
        print(f"consistency_warning={warning}")


def _print_yamato_b2_import_result(result: YamatoB2ImportResult) -> None:
    print(f"step={result.step}")
    print(f"csv_file={result.csv_file}")
    print(f"source_rows={result.source_rows}")
    print(f"ready_to_import={result.ready_to_import}")
    print(f"browser_executed={result.browser_executed}")
    print(f"file_selected={result.file_selected}")
    print(f"import_executed={result.import_executed}")
    if result.skipped_reason:
        print(f"skipped_reason={result.skipped_reason}")
    if result.page_title:
        print(f"page_title={result.page_title}")
    if result.page_url:
        print(f"page_url={result.page_url}")
    if result.screenshot_path:
        print(f"screenshot_path={result.screenshot_path}")
    if result.html_path:
        print(f"html_path={result.html_path}")
    print(f"audit_path={result.audit_path}")
    for warning in result.warnings:
        print(f"warning={warning}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")

    download_parser = subparsers.add_parser("download-next-engine")
    download_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    download_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    shipment_parser = subparsers.add_parser("confirm-next-engine-shipment")
    shipment_parser.add_argument(
        "--execute",
        action="store_true",
        help="実際の出荷確定を要求します。現時点では未実装理由を返し、外部状態は変更しません。",
    )
    shipment_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="出荷確定の実行計画だけを確認します。既定動作です。",
    )
    shipment_parser.add_argument(
        "--order-nos",
        default="",
        help="対象のNext Engine伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    shipment_parser.add_argument("--sample-input", help="G7 sample_inputs のJSONです。")
    shipment_parser.add_argument("--expected-contract", help="G7 expected_outputs の契約JSONです。")
    shipment_parser.add_argument(
        "--fetch-yamato-tracking",
        action="store_true",
        help="ヤマトB2 Cloud発行済データCSVの取得境界を実行します。--executeなしではdry-run検証のみです。",
    )
    shipment_parser.add_argument(
        "--write-import-csv",
        action="store_true",
        help="出荷実績アップロード用CSVを完成データへ書き込みます。--execute指定時のみ書き込みます。",
    )
    shipment_parser.add_argument(
        "--execute-upload",
        action="store_true",
        help="Next Engineへ出荷実績CSVをアップロードします。--execute指定時のみ外部反映します。",
    )
    shipment_parser.add_argument(
        "--confirm-upload",
        action="store_true",
        help="NEへの実反映を明示確認します。--execute --execute-upload に加えてこの指定がないと実反映しません。",
    )
    shipment_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    shipment_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    shipment_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    shipment_import_parser = subparsers.add_parser("build-shipment-confirmation-csv")
    shipment_import_parser.add_argument(
        "--write",
        action="store_true",
        help="ネクストエンジン\\完成データ\\yamato_to-neYYMMDDHHMM.csv（3列）を作成します。未指定時は確認のみです。",
    )
    shipment_import_parser.add_argument(
        "--order-nos",
        default="",
        help="対象のNext Engine伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    shipment_import_parser.add_argument(
        "--scanned-codes",
        default="",
        help="納品書バーコードのスキャン値です。正規化して伝票番号として扱います（--order-nos と併用可）。",
    )
    shipment_import_parser.add_argument(
        "--buyer-lookback-days",
        type=int,
        default=None,
        help="購入者データの取込日数です。未指定時は KURIMA_SHIPMENT_BUYER_LOOKBACK_DAYS（既定20日）です。",
    )
    shipment_import_parser.add_argument(
        "--clickpost-lookback-days",
        type=int,
        default=None,
        help="クリックポスト送り状番号CSVの取込日数です。既定20日です。",
    )
    shipment_import_parser.add_argument(
        "--letterpack-lookback-days",
        type=int,
        default=None,
        help="レターパックCSVの取込日数です。既定30日です。",
    )
    shipment_import_parser.add_argument(
        "--yamato-lookback-days",
        type=int,
        default=None,
        help="ヤマト出荷データ（yamato-okurizyo）の取込日数です。既定30日です。",
    )
    shipment_import_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    shipment_upload_parser = subparsers.add_parser("upload-next-engine-shipment-confirmation")
    shipment_upload_parser.add_argument(
        "--execute",
        action="store_true",
        help="Next Engineへ出荷実績CSVを実際にアップロードします。--confirm-upload も必要です。",
    )
    shipment_upload_parser.add_argument(
        "--confirm-upload",
        action="store_true",
        help="NEへの実反映を明示確認します。--execute 単独では実反映しません。",
    )
    shipment_upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="アップロード前チェックのみ行います。既定動作です。",
    )
    shipment_upload_parser.add_argument(
        "--csv-file",
        help="アップロードする出荷実績CSV（3列: 伝票番号,発送伝票番号,出荷予定日）を指定します。未指定時は 完成データ の最新 yamato_to-ne*.csv です。",
    )
    shipment_upload_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    shipment_upload_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    shipment_upload_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    yamato_tracking_parser = subparsers.add_parser("download-yamato-tracking-export")
    yamato_tracking_parser.add_argument(
        "--execute",
        action="store_true",
        help="ヤマトB2 Cloudから発行済データCSVを実際にダウンロードします。",
    )
    yamato_tracking_parser.add_argument(
        "--target-date",
        help="B2 Cloudの出荷予定日です。未指定時は当日です。",
    )
    yamato_tracking_parser.add_argument(
        "--yamato-target-date",
        dest="target_date",
        help="--target-date の別名です（出荷確定画面と揃えた名前）。",
    )
    yamato_tracking_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_tracking_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    yamato_tracking_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    takaesu_parser = subparsers.add_parser("download-takaesu-order-details")
    takaesu_parser.add_argument(
        "--execute",
        action="store_true",
        help="高江洲発注明細を実際に取得します。現時点では未実装理由を返し、外部状態は変更しません。",
    )
    takaesu_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="発注明細取得の実行計画だけを確認します。既定動作です。",
    )
    takaesu_parser.add_argument("--target-date", help="対象日付です。")
    takaesu_parser.add_argument(
        "--order-nos",
        default="",
        help="対象伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    takaesu_parser.add_argument("--sample-input", help="G7 sample_inputs のJSONです。")
    takaesu_parser.add_argument("--expected-contract", help="G7 expected_outputs の契約JSONです。")
    takaesu_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    takaesu_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    takaesu_sheet_parser = subparsers.add_parser("build-takaesu-order-sheet")
    takaesu_sheet_parser.add_argument(
        "--write",
        action="store_true",
        help="ネクストエンジン\\発注関連\\高江洲発注書.csv を作成します。未指定時は確認のみです。",
    )
    takaesu_sheet_parser.add_argument(
        "--source-csv",
        help="高江洲受注明細CSVを明示します。未指定時は受注明細一覧-高江洲の最新data*.csvです。",
    )
    takaesu_sheet_parser.add_argument(
        "--output-csv",
        help="書き出し先CSVを明示します。--write 指定時だけ使います。",
    )
    takaesu_sheet_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    takaesu_prepare_parser = subparsers.add_parser("prepare-takaesu-order-sheet")
    takaesu_prepare_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Main相当の実行計画だけを出力し、CSV読込、ダウンロード、書き込みは行いません。",
    )
    takaesu_prepare_parser.add_argument(
        "--execute-download",
        action="store_true",
        help="Next Engineから高江洲受注明細CSVを実際にダウンロードします。",
    )
    takaesu_prepare_parser.add_argument(
        "--write-order-sheet",
        action="store_true",
        help="高江洲発注書CSVを書き込みます。",
    )
    takaesu_prepare_parser.add_argument("--source-csv", help="発注書作成に使う受注明細CSVを指定します。")
    takaesu_prepare_parser.add_argument("--output-csv", help="発注書CSVの出力先を指定します。")
    takaesu_prepare_parser.add_argument("--sample-input", help="G7 sample_inputs のJSONです。")
    takaesu_prepare_parser.add_argument("--expected-contract", help="G7 expected_outputs の契約JSONです。")
    takaesu_prepare_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    takaesu_prepare_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    takaesu_prepare_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    ne02_parser = subparsers.add_parser("download-ne02-order-details")
    ne02_parser.add_argument(
        "--execute",
        action="store_true",
        help="NE02受注明細を実際に取得します。現時点では未実装理由を返し、外部状態は変更しません。",
    )
    ne02_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="NE02受注明細取得の実行計画だけを確認します。既定動作です。",
    )
    ne02_parser.add_argument("--target-date", help="対象日付です。")
    ne02_parser.add_argument(
        "--order-nos",
        default="",
        help="対象伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    ne02_parser.add_argument("--sample-input", help="G7 sample_inputs のJSONです。")
    ne02_parser.add_argument("--expected-contract", help="G7 expected_outputs の契約JSONです。")

    inspect_parser = subparsers.add_parser("inspect-next-engine-order")
    inspect_parser.add_argument("--order-no", required=True, help="確認するNext Engine伝票番号です。")
    inspect_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    inspect_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    restore_parser = subparsers.add_parser("restore-next-engine-print-wait")
    restore_parser.add_argument("--order-no", required=True, help="対象のNext Engine伝票番号です。")
    restore_parser.add_argument(
        "--execute",
        action="store_true",
        help="実際に確認済チェックを保存してステータスを戻します。未指定なら状態確認のみです。",
    )
    restore_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    restore_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    restore_batch_parser = subparsers.add_parser("restore-next-engine-print-wait-batch")
    restore_batch_parser.add_argument(
        "--order-nos",
        required=True,
        help="Comma, space, or newline separated Next Engine order numbers.",
    )
    restore_batch_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually restore each order to print-wait. Omit for dry-run status checks.",
    )
    restore_batch_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser.",
    )
    restore_batch_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright slow motion in milliseconds.",
    )

    invoice_parser = subparsers.add_parser("test-next-engine-invoice-download")
    invoice_parser.add_argument("--order-no", required=True, help="テスト対象のNext Engine伝票番号です。")
    invoice_parser.add_argument(
        "--execute",
        action="store_true",
        help="納品書PDFを実際に取得し、最後に印刷待ちへ戻します。未指定なら状態確認のみです。",
    )
    invoice_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    invoice_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    yamato_invoice_parser = subparsers.add_parser("download-next-engine-yamato-invoices")
    yamato_invoice_parser.add_argument(
        "--execute",
        action="store_true",
        help="ヤマト対象伝票の納品書PDFを一括取得し、対象伝票のステータスを記録します。",
    )
    yamato_invoice_parser.add_argument(
        "--order-nos",
        default="",
        help="対象を絞るNext Engine伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    yamato_invoice_parser.add_argument(
        "--verify-statuses",
        action="store_true",
        help="Open each order detail before/after invoice download and verify the Next Engine status.",
    )
    yamato_invoice_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_invoice_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    yamato_inspect_parser = subparsers.add_parser("inspect-next-engine-yamato-orders")
    yamato_inspect_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_inspect_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    yamato_buyer_parser = subparsers.add_parser("download-next-engine-yamato-buyer")
    yamato_buyer_parser.add_argument(
        "--execute",
        action="store_true",
        help="購入者データCSVを実際に取得します。未指定なら対象件数確認のみです。",
    )
    yamato_buyer_parser.add_argument(
        "--order-nos",
        default="",
        help="対象を絞るNext Engine伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    yamato_buyer_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_buyer_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    yamato_product_parser = subparsers.add_parser("download-next-engine-yamato-product")
    yamato_product_parser.add_argument(
        "--execute",
        action="store_true",
        help="商品情報データCSVを実際に取得します。未指定なら対象件数確認のみです。",
    )
    yamato_product_parser.add_argument(
        "--output-type",
        default="D_ALL",
        choices=("D_ALL", "D_KEPIN", "S_ALL", "S_KEPIN", "SETS_ALL"),
        help="明細一覧の出力タイプです。既定は伝票明細単位の D_ALL です。",
    )
    yamato_product_parser.add_argument(
        "--order-nos",
        default="",
        help="対象を絞るNext Engine伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    yamato_product_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_product_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    yamato_custom_parser = subparsers.add_parser("download-next-engine-yamato-custom-shipping")
    yamato_custom_parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "配送情報CSVを実際に取得します。実行するとNext Engine側で"
            "配送情報ダウンロード済みとして処理されます。未指定なら確認モーダル表示までです。"
        ),
    )
    yamato_custom_parser.add_argument(
        "--order-nos",
        default="",
        help="カスタム配送情報CSVの伝票番号条件に指定する伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    yamato_custom_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_custom_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    clickpost_inspect_parser = subparsers.add_parser("inspect-next-engine-clickpost-orders")
    clickpost_inspect_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_inspect_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    clickpost_buyer_parser = subparsers.add_parser("download-next-engine-clickpost-buyer")
    clickpost_buyer_parser.add_argument(
        "--execute",
        action="store_true",
        help="購入者データCSVを実際にダウンロードします。未指定時は対象件数確認のみです。",
    )
    clickpost_buyer_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_buyer_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    clickpost_product_parser = subparsers.add_parser("download-next-engine-clickpost-product")
    clickpost_product_parser.add_argument(
        "--execute",
        action="store_true",
        help="商品情報データCSVを実際にダウンロードします。未指定時は対象件数確認のみです。",
    )
    clickpost_product_parser.add_argument(
        "--output-type",
        default="D_ALL",
        choices=("D_ALL", "D_KEPIN", "S_ALL", "S_KEPIN", "SETS_ALL"),
        help="明細一覧の出力タイプです。",
    )
    clickpost_product_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_product_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    clickpost_convert_parser = subparsers.add_parser("convert-clickpost-csv")
    clickpost_convert_parser.add_argument(
        "--write",
        action="store_true",
        help="完成したデータ\\clickpostimport.csv を作成します。未指定時は確認のみです。",
    )
    clickpost_convert_parser.add_argument("--buyer-csv", help="購入者データCSVを指定します。")
    clickpost_convert_parser.add_argument("--product-csv", help="商品情報データCSVを指定します。")
    clickpost_convert_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    letterpack_convert_parser = subparsers.add_parser("convert-letterpack-addresses")
    letterpack_convert_parser.add_argument(
        "--write",
        action="store_true",
        help="完成したデータ\\letterpack_addressbook.csv を作成します。未指定時は確認のみです。",
    )
    letterpack_convert_parser.add_argument("--buyer-csv", help="購入者データCSVを指定します。")
    letterpack_convert_parser.add_argument("--product-csv", help="商品情報データCSVを指定します。")
    letterpack_convert_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    letterpack_pdf_parser = subparsers.add_parser("create-letterpack-label-pdf")
    letterpack_pdf_parser.add_argument(
        "--address-csv",
        help="PDF化する letterpack_addressbook.csv を指定します。未指定時は最新NEデータから再作成します。",
    )
    letterpack_pdf_parser.add_argument("--output-pdf", help="PDF保存先を指定します。")
    letterpack_pdf_parser.add_argument(
        "--skip-address-csv-refresh",
        action="store_true",
        help="最新NEデータから letterpack_addressbook.csv を再作成せず、既存CSVを使います。",
    )
    letterpack_pdf_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )
    letterpack_pdf_parser.add_argument(
        "--no-message-box",
        action="store_true",
        help="PDF保存後の保存先メッセージボックスを表示しません。",
    )

    clickpost_reflect_parser = subparsers.add_parser("reflect-clickpost-tracking")
    clickpost_reflect_parser.add_argument(
        "--write",
        action="store_true",
        help="完成したデータ\\clickpost_tracking_reflection.csv を作成します。未指定時は確認のみです。",
    )
    clickpost_reflect_parser.add_argument(
        "--tracking-csv",
        required=True,
        help="クリックポストのマイページから抽出した申込日時/お問い合わせ番号/お届け先氏名CSVです。",
    )
    clickpost_reflect_parser.add_argument("--buyer-csv", help="購入者データCSVを指定します。")
    clickpost_reflect_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    clickpost_upload_parser = subparsers.add_parser("upload-clickpost-csv")
    clickpost_upload_parser.add_argument(
        "--execute",
        action="store_true",
        help="クリックポストへCSVをアップロードして支払手続き画面まで進めます。",
    )
    clickpost_upload_parser.add_argument("--csv-file", help="アップロードするCSVを指定します。")
    clickpost_upload_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_upload_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    clickpost_upload_parser.add_argument(
        "--wait-at-payment-seconds",
        type=int,
        default=0,
        help="支払手続き画面到達後、手入力のためブラウザを開いたまま待つ秒数です。",
    )

    clickpost_payment_print_parser = subparsers.add_parser("complete-clickpost-payment-print")
    clickpost_payment_print_parser.add_argument(
        "--execute",
        action="store_true",
        help="未決済のクリックポスト申込を支払い、まとめ印字PDFを保存します。",
    )
    clickpost_payment_print_parser.add_argument("--output-dir", help="PDF保存先フォルダを指定します。")
    clickpost_payment_print_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_payment_print_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    clickpost_payment_print_parser.add_argument(
        "--max-payments",
        type=int,
        default=20,
        help="この実行で処理する最大支払い件数です。",
    )
    clickpost_payment_print_parser.add_argument(
        "--no-message-box",
        action="store_true",
        help="PDF保存後の保存先メッセージボックスを表示しません。",
    )

    clickpost_import_payment_print_parser = subparsers.add_parser("run-clickpost-import-payment-print")
    clickpost_import_payment_print_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run CSV import, wallet payment, multiple print, and PDF save.",
    )
    clickpost_import_payment_print_parser.add_argument("--csv-file", help="CSV file to import. Defaults to clickpostimport.csv.")
    clickpost_import_payment_print_parser.add_argument("--output-dir", help="PDF output folder.")
    clickpost_import_payment_print_parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser while running.",
    )
    clickpost_import_payment_print_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright slow motion in milliseconds.",
    )
    clickpost_import_payment_print_parser.add_argument(
        "--max-payments",
        type=int,
        default=20,
        help="Maximum wallet payments to complete in this run.",
    )
    clickpost_import_payment_print_parser.add_argument(
        "--no-message-box",
        action="store_true",
        help="Do not show a message box after saving the PDF.",
    )

    clickpost_tracking_export_parser = subparsers.add_parser("export-clickpost-tracking-for-csv")
    clickpost_tracking_export_parser.add_argument(
        "--execute",
        action="store_true",
        help="ClickPostマイページから今回CSV分のお問い合わせ番号を取得し、CSV保存とxlsm貼り付けを実行します。",
    )
    clickpost_tracking_export_parser.add_argument("--csv-file", help="照合元のクリックポスト取込CSVです。未指定時はclickpostimport.csvです。")
    clickpost_tracking_export_parser.add_argument("--output-dir", help="お問い合わせ番号CSVの保存先フォルダです。")
    clickpost_tracking_export_parser.add_argument(
        "--no-workbook-update",
        action="store_true",
        help="クリックポストcsv変換.xlsm の送り状csv貼り付けシートを更新しません。",
    )
    clickpost_tracking_export_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_tracking_export_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )

    clickpost_prepare_parser = subparsers.add_parser("prepare-clickpost")
    clickpost_prepare_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="G9検証用に実行計画だけを出力し、CSV読込、ダウンロード、アップロードは行いません。",
    )
    clickpost_prepare_parser.add_argument("--sample-input", help="G7 sample_inputs のJSONです。")
    clickpost_prepare_parser.add_argument("--expected-contract", help="G7 expected_outputs の契約JSONです。")
    clickpost_prepare_parser.add_argument(
        "--fetch-next-engine",
        action="store_true",
        help="Next Engine のクリックポスト/レターパック対象を確認します。",
    )
    clickpost_prepare_parser.add_argument(
        "--execute-downloads",
        action="store_true",
        help="購入者データCSVと商品情報データCSVを実際にダウンロードします。",
    )
    clickpost_prepare_parser.add_argument(
        "--write-conversion",
        action="store_true",
        help="clickpostimport.csv を作成します。",
    )
    clickpost_prepare_parser.add_argument(
        "--write-letterpack-addresses",
        action="store_true",
        help="letterpack_addressbook.csv を作成します。未指定時は件数確認のみです。",
    )
    clickpost_prepare_parser.add_argument(
        "--tracking-csv",
        help="reflect-clickpost-tracking に渡すクリックポストお問い合わせ番号CSVです。",
    )
    clickpost_prepare_parser.add_argument(
        "--write-tracking-reflection",
        action="store_true",
        help="tracking-csv を使って clickpost_tracking_reflection.csv を作成します。",
    )
    clickpost_prepare_parser.add_argument(
        "--upload",
        action="store_true",
        help="クリックポストアップロードのdry-run確認を含めます。",
    )
    clickpost_prepare_parser.add_argument(
        "--execute-upload",
        action="store_true",
        help="クリックポストへCSVを実際にアップロードして支払手続き画面まで進めます。",
    )
    clickpost_prepare_parser.add_argument(
        "--output-type",
        default="D_ALL",
        choices=("D_ALL", "D_KEPIN", "S_ALL", "S_KEPIN", "SETS_ALL"),
        help="明細一覧の出力タイプです。",
    )
    clickpost_prepare_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    clickpost_prepare_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    clickpost_prepare_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    yamato_convert_parser = subparsers.add_parser("convert-yamato-ne-to-b2")
    yamato_convert_parser.add_argument(
        "--write",
        action="store_true",
        help="完成データへ ne-to-yamatoYYMMDDHHMM.csv を作成します。未指定時は確認のみです。",
    )
    yamato_convert_parser.add_argument(
        "--source-csv",
        help="変換元 ne-yamato CSV を明示します。未指定時は ne-yamatocsv の最新CSVです。",
    )
    yamato_convert_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="確認用に保持する先頭行数です。",
    )

    yamato_prepare_parser = subparsers.add_parser("prepare-yamato-b2")
    yamato_prepare_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="G9検証用に実行計画だけを出力し、CSV読込、ダウンロード、変換は行いません。",
    )
    yamato_prepare_parser.add_argument("--sample-input", help="G7 sample_inputs のJSONです。")
    yamato_prepare_parser.add_argument("--expected-contract", help="G7 expected_outputs の契約JSONです。")
    yamato_prepare_parser.add_argument(
        "--fetch-next-engine",
        action="store_true",
        help="Next Engineのヤマト対象を確認します。単体ではダウンロードせずドライランです。",
    )
    yamato_prepare_parser.add_argument(
        "--execute-downloads",
        action="store_true",
        help="購入者データCSVと商品情報データCSVを実際にダウンロードします。",
    )
    yamato_prepare_parser.add_argument(
        "--check-invoices",
        action="store_true",
        help="Confirm Yamato invoice PDF batch targets without downloading the PDF.",
    )
    yamato_prepare_parser.add_argument(
        "--execute-invoices",
        action="store_true",
        help="Download Yamato invoice PDFs with shipping-output enabled before custom shipping CSV.",
    )
    yamato_prepare_parser.add_argument(
        "--verify-invoice-statuses",
        action="store_true",
        help="Open each order detail before/after invoice download and verify the Next Engine status.",
    )
    yamato_prepare_parser.add_argument(
        "--check-custom-shipping",
        action="store_true",
        help="配送情報CSVの確認モーダルまで開きます。最終ダウンロードはしません。",
    )
    yamato_prepare_parser.add_argument(
        "--execute-custom-shipping",
        action="store_true",
        help=(
            "配送情報CSVを実際にダウンロードします。Next Engine側で配送情報ダウンロード済みとして"
            "処理される状態変更操作です。"
        ),
    )
    yamato_prepare_parser.add_argument(
        "--order-nos",
        default="",
        help="購入者/商品/納品書/配送情報CSVの対象を絞るNext Engine伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    yamato_prepare_parser.add_argument(
        "--custom-shipping-order-nos",
        default="",
        help="配送情報CSVの伝票番号条件に指定する伝票番号です。カンマ、空白、改行区切りに対応します。",
    )
    yamato_prepare_parser.add_argument(
        "--write-conversion",
        action="store_true",
        help="B2取込用 ne-to-yamato CSV を完成データへ作成します。未指定時は確認のみです。",
    )
    yamato_prepare_parser.add_argument(
        "--output-type",
        default="D_ALL",
        choices=("D_ALL", "D_KEPIN", "S_ALL", "S_KEPIN", "SETS_ALL"),
        help="商品情報データCSV取得時の明細一覧出力タイプです。",
    )
    yamato_prepare_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_prepare_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=0,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    yamato_prepare_parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="変換結果プレビューの先頭行数です。",
    )

    yamato_b2_import_parser = subparsers.add_parser("import-yamato-b2")
    yamato_b2_import_parser.add_argument(
        "--csv-file",
        help="B2へ取り込む ne-to-yamato CSV です。未指定時は完成データの最新CSVです。",
    )
    yamato_b2_import_parser.add_argument(
        "--check-login",
        action="store_true",
        help="Yamato B2 Cloudへログインできるかだけ確認します。CSV選択や取込は行いません。",
    )
    yamato_b2_import_parser.add_argument(
        "--open-import-page",
        action="store_true",
        help="B2取込画面まで遷移します。CSV選択や取込は行いません。",
    )
    yamato_b2_import_parser.add_argument(
        "--select-file-dry-run",
        action="store_true",
        help="B2取込画面でCSVファイル選択まで確認します。取込開始は押しません。",
    )
    yamato_b2_import_parser.add_argument(
        "--execute-import",
        action="store_true",
        help="B2へCSVを実際に取り込みます。--confirm-import も必要です。",
    )
    yamato_b2_import_parser.add_argument(
        "--confirm-import",
        action="store_true",
        help="B2実取込を明示確認します。単体では何も実行しません。",
    )
    yamato_b2_import_parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    yamato_b2_import_parser.add_argument(
        "--slow-mo-ms",
        type=int,
        default=150,
        help="Playwright 操作ごとの待機ミリ秒です。",
    )
    args = parser.parse_args()

    if args.command == "check":
        return check()
    if args.command == "download-next-engine":
        return download_next_engine(
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "confirm-next-engine-shipment":
        return confirm_next_engine_shipment(
            execute=args.execute,
            order_numbers=_parse_order_numbers(args.order_nos),
            sample_input=args.sample_input,
            expected_contract=args.expected_contract,
            fetch_yamato_tracking=args.fetch_yamato_tracking,
            write_import_csv=args.write_import_csv,
            execute_upload=args.execute_upload,
            confirm_upload=args.confirm_upload,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            preview_limit=args.preview_limit,
        )
    if args.command == "build-shipment-confirmation-csv":
        return build_shipment_confirmation_csv(
            write=args.write,
            order_numbers=(
                *_parse_order_numbers(args.order_nos),
                *_parse_order_numbers(args.scanned_codes),
            ),
            preview_limit=args.preview_limit,
            buyer_lookback_days=args.buyer_lookback_days,
            clickpost_lookback_days=args.clickpost_lookback_days,
            letterpack_lookback_days=args.letterpack_lookback_days,
            yamato_lookback_days=args.yamato_lookback_days,
        )
    if args.command == "upload-next-engine-shipment-confirmation":
        return upload_next_engine_shipment_confirmation(
            execute=args.execute,
            confirm_upload=args.confirm_upload,
            csv_file=args.csv_file,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            preview_limit=args.preview_limit,
        )
    if args.command == "download-yamato-tracking-export":
        return download_yamato_tracking_export(
            execute=args.execute,
            target_date=args.target_date,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            preview_limit=args.preview_limit,
        )
    if args.command == "download-takaesu-order-details":
        return download_takaesu_order_details(
            execute=args.execute,
            target_date=args.target_date,
            order_numbers=_parse_order_numbers(args.order_nos),
            sample_input=args.sample_input,
            expected_contract=args.expected_contract,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "build-takaesu-order-sheet":
        return build_takaesu_order_sheet(
            write=args.write,
            source_csv=args.source_csv,
            output_csv=args.output_csv,
            preview_limit=args.preview_limit,
        )
    if args.command == "prepare-takaesu-order-sheet":
        return prepare_takaesu_order_sheet(
            dry_run=args.dry_run,
            execute_download=args.execute_download,
            write_order_sheet=args.write_order_sheet,
            source_csv=args.source_csv,
            output_csv=args.output_csv,
            sample_input=args.sample_input,
            expected_contract=args.expected_contract,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            preview_limit=args.preview_limit,
        )
    if args.command == "download-ne02-order-details":
        return download_ne02_order_details(
            execute=args.execute,
            target_date=args.target_date,
            order_numbers=_parse_order_numbers(args.order_nos),
            sample_input=args.sample_input,
            expected_contract=args.expected_contract,
        )
    if args.command == "inspect-next-engine-order":
        return inspect_next_engine_order(
            order_no=args.order_no,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "restore-next-engine-print-wait":
        return restore_next_engine_print_wait(
            order_no=args.order_no,
            execute=args.execute,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "restore-next-engine-print-wait-batch":
        return restore_next_engine_print_wait_batch(
            order_numbers=_parse_order_numbers(args.order_nos),
            execute=args.execute,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "test-next-engine-invoice-download":
        return test_next_engine_invoice_download(
            order_no=args.order_no,
            execute=args.execute,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "download-next-engine-yamato-invoices":
        return download_next_engine_yamato_invoices(
            execute=args.execute,
            order_numbers=_parse_order_numbers(args.order_nos),
            verify_statuses=args.verify_statuses,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "inspect-next-engine-yamato-orders":
        return inspect_next_engine_yamato_orders(
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "download-next-engine-yamato-buyer":
        return download_next_engine_yamato_buyer(
            execute=args.execute,
            order_numbers=_parse_order_numbers(args.order_nos),
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "download-next-engine-yamato-product":
        return download_next_engine_yamato_product(
            execute=args.execute,
            output_type=args.output_type,
            order_numbers=_parse_order_numbers(args.order_nos),
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "download-next-engine-yamato-custom-shipping":
        return download_next_engine_yamato_custom_shipping(
            execute=args.execute,
            order_numbers=_parse_order_numbers(args.order_nos),
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "inspect-next-engine-clickpost-orders":
        return inspect_next_engine_clickpost_orders(
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "download-next-engine-clickpost-buyer":
        return download_next_engine_clickpost_buyer(
            execute=args.execute,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "download-next-engine-clickpost-product":
        return download_next_engine_clickpost_product(
            execute=args.execute,
            output_type=args.output_type,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "convert-clickpost-csv":
        return convert_clickpost_csv(
            write=args.write,
            buyer_csv=args.buyer_csv,
            product_csv=args.product_csv,
            preview_limit=args.preview_limit,
        )
    if args.command == "convert-letterpack-addresses":
        return convert_letterpack_addresses(
            write=args.write,
            buyer_csv=args.buyer_csv,
            product_csv=args.product_csv,
            preview_limit=args.preview_limit,
        )
    if args.command == "create-letterpack-label-pdf":
        return create_letterpack_pdf(
            address_csv=args.address_csv,
            output_pdf=args.output_pdf,
            refresh_address_csv=not args.skip_address_csv_refresh,
            preview_limit=args.preview_limit,
            message_box=not args.no_message_box,
        )
    if args.command == "reflect-clickpost-tracking":
        return reflect_clickpost_tracking(
            write=args.write,
            tracking_csv=args.tracking_csv,
            buyer_csv=args.buyer_csv,
            preview_limit=args.preview_limit,
        )
    if args.command == "upload-clickpost-csv":
        return upload_clickpost_csv(
            execute=args.execute,
            csv_file=args.csv_file,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            wait_at_payment_seconds=args.wait_at_payment_seconds,
        )
    if args.command == "complete-clickpost-payment-print":
        return complete_clickpost_payment_print(
            execute=args.execute,
            output_dir=args.output_dir,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            max_payments=args.max_payments,
            message_box=not args.no_message_box,
        )
    if args.command == "run-clickpost-import-payment-print":
        return run_clickpost_import_payment_print(
            execute=args.execute,
            csv_file=args.csv_file,
            output_dir=args.output_dir,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            max_payments=args.max_payments,
            message_box=not args.no_message_box,
        )
    if args.command == "export-clickpost-tracking-for-csv":
        return export_clickpost_tracking_for_import_csv(
            execute=args.execute,
            csv_file=args.csv_file,
            output_dir=args.output_dir,
            update_workbook=not args.no_workbook_update,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    if args.command == "prepare-clickpost":
        return prepare_clickpost(
            dry_run=args.dry_run,
            sample_input=args.sample_input,
            expected_contract=args.expected_contract,
            fetch_next_engine=args.fetch_next_engine,
            execute_downloads=args.execute_downloads,
            write_conversion=args.write_conversion,
            write_letterpack_addresses=args.write_letterpack_addresses,
            tracking_csv=args.tracking_csv,
            write_tracking_reflection=args.write_tracking_reflection,
            upload=args.upload,
            execute_upload=args.execute_upload,
            output_type=args.output_type,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            preview_limit=args.preview_limit,
        )
    if args.command == "convert-yamato-ne-to-b2":
        return convert_yamato_ne_to_b2(
            write=args.write,
            source_csv=args.source_csv,
            preview_limit=args.preview_limit,
        )
    if args.command == "prepare-yamato-b2":
        target_order_numbers = _parse_order_numbers(args.order_nos) or _parse_order_numbers(
            args.custom_shipping_order_nos
        )
        return prepare_yamato_b2(
            dry_run=args.dry_run,
            sample_input=args.sample_input,
            expected_contract=args.expected_contract,
            fetch_next_engine=args.fetch_next_engine,
            execute_downloads=args.execute_downloads,
            check_invoices=args.check_invoices,
            execute_invoices=args.execute_invoices,
            verify_invoice_statuses=args.verify_invoice_statuses,
            check_custom_shipping=args.check_custom_shipping,
            execute_custom_shipping=args.execute_custom_shipping,
            custom_shipping_order_numbers=target_order_numbers,
            write_conversion=args.write_conversion,
            output_type=args.output_type,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
            preview_limit=args.preview_limit,
        )
    if args.command == "import-yamato-b2":
        return import_yamato_b2(
            csv_file=args.csv_file,
            check_login=args.check_login,
            open_import_page=args.open_import_page,
            select_file_dry_run=args.select_file_dry_run,
            execute_import=args.execute_import,
            confirm_import=args.confirm_import,
            headed=args.headed,
            slow_mo_ms=args.slow_mo_ms,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
