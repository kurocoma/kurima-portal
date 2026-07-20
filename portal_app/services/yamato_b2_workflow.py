from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from portal_app.services.next_engine_downloader import APP_ROOT
from portal_app.services.next_engine_invoice import (
    InvoiceBatchDownloadResult,
    download_yamato_invoice_batch,
    download_yamato_invoice_batch_sync,
)
from portal_app.services.next_engine_yamato import (
    NextEngineYamatoClient,
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
from portal_app.services.yamato_flow_profile import YAMATO_PROFILE, YamatoFlowProfile


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


async def _run_step(label: str, coro):
    """取得工程を実行し、失敗時に「どの工程で止まったか」をエラー文へ前置きする。

    生の Playwright 例外（例: Page.wait_for_function: Timeout ...）だけだと、どの取得で
    止まったか分かりにくいため、工程名を付けて可読性を上げる（ログ観測性の改善）。
    """
    try:
        return await coro
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"{label}でエラー: {message}") from exc


def _emit(progress, key: str, status: str, detail: str | None = None) -> None:
    """進捗コールバックを安全に呼ぶ（None なら何もしない）。"""
    if progress is None:
        return
    try:
        progress(key, status, detail)
    except Exception:
        pass


async def _tracked_step(progress, key: str, label: str, coro, *, done_detail=None):
    """工程を running→completed で進捗報告しつつ実行。失敗時は failed 報告＋工程名付きで再送出。

    done_detail(result) を渡すと、完了報告の detail にその戻り値（str | None）を表示する。
    """
    _emit(progress, key, "running")
    try:
        result = await coro
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        _emit(progress, key, "failed", message[:140])
        raise RuntimeError(f"{label}でエラー: {message}") from exc
    detail = None
    if done_detail is not None:
        try:
            detail = done_detail(result)
        except Exception:
            detail = None
    _emit(progress, key, "completed", detail)
    return result


def _printed_orders_text(invoice: InvoiceBatchDownloadResult | None) -> str | None:
    """納品書PDF一括DLで「印刷済み」へ進んだ伝票番号の一覧テキスト（該当なしなら None）。"""
    if invoice is None or not invoice.executed or not invoice.downloaded_file:
        return None
    orders = invoice.before_list.order_numbers
    if not orders:
        return None
    return ", ".join(orders)


def _printed_step_detail(invoice: InvoiceBatchDownloadResult | None) -> str | None:
    """納品書PDF取得ステップの完了 detail。ジョブが後段で止まっても、
    どの伝票が印刷済みへ進んだかを進捗ステップ上で常に確認できるようにする。"""
    text = _printed_orders_text(invoice)
    return f"印刷済みへ変更: {text}" if text else None


def _printed_orders_notice(invoice: InvoiceBatchDownloadResult | None) -> str:
    """後段の工程が失敗したとき、エラー文へ添える「印刷済みへ進んだ伝票」の明示（該当なしなら空文字）。"""
    text = _printed_orders_text(invoice)
    if not text:
        return ""
    return (
        f"※納品書印刷で「印刷済み」へ変更済みの伝票: {text}"
        "（『その他の操作・納品書印刷待ちへ復旧』で戻せます）"
    )


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
    progress=None,
    profile: YamatoFlowProfile = YAMATO_PROFILE,
) -> YamatoB2PreparationResult:
    if execute_downloads:
        fetch_next_engine = True
    if execute_invoices:
        check_invoices = True
    if execute_custom_shipping:
        check_custom_shipping = True
    # 個別セッション経路（verify_invoice_statuses）はモジュール関数がヤマト既定の
    # プロファイルでクライアントを作るため、ネコポス等では共有セッション経路に固定する。
    if profile.key != YAMATO_PROFILE.key:
        verify_invoice_statuses = False

    downloads_kwargs = dict(
        fetch_next_engine=fetch_next_engine,
        execute_downloads=execute_downloads,
        check_invoices=check_invoices,
        execute_invoices=execute_invoices,
        verify_invoice_statuses=verify_invoice_statuses,
        check_custom_shipping=check_custom_shipping,
        execute_custom_shipping=execute_custom_shipping,
        target_order_numbers=custom_shipping_order_numbers,
        output_type=output_type,
        headed=headed,
        slow_mo_ms=slow_mo_ms,
        progress=progress,
        profile=profile,
    )

    # 通常は共有セッション（1ブラウザ/1ログイン/メイン機能1回）で全取得を実行し、無駄な開閉を無くす。
    # verify_invoice_statuses（検証時のみ・既定OFF）はステータス照会が別セッション＋同ロックを取り
    # デッドロックするため、その場合だけ従来の個別セッション経路にフォールバックする。
    if verify_invoice_statuses:
        buyer, product, invoice, custom_shipping, workflow_warnings = _run_downloads_legacy(
            **downloads_kwargs
        )
    else:
        buyer, product, invoice, custom_shipping, workflow_warnings = asyncio.run(
            _run_downloads_shared(**downloads_kwargs)
        )

    # ④ 住所補正・B2取込CSV作成
    _emit(progress, "conversion", "running")
    try:
        result = _finalize_prepare(
            buyer=buyer,
            product=product,
            invoice=invoice,
            custom_shipping=custom_shipping,
            workflow_warnings=workflow_warnings,
            execute_custom_shipping=execute_custom_shipping,
            write_conversion=write_conversion,
            preview_limit=preview_limit,
            profile=profile,
        )
    except Exception as exc:
        _emit(progress, "conversion", "failed", (str(exc).strip() or type(exc).__name__)[:140])
        # ここで止まると納品書PDF取得で進んだ「印刷済み」が残るため、
        # どの伝票が変更済みかをエラー文に必ず明示する。
        notice = _printed_orders_notice(invoice)
        if notice:
            raise RuntimeError(f"{exc} {notice}") from exc
        raise
    _emit(progress, "conversion", "completed")
    return result


def _custom_shipping_plan(
    *,
    target_order_numbers: tuple[str, ...],
    invoice: InvoiceBatchDownloadResult | None,
    check_custom_shipping: bool,
    execute_custom_shipping: bool,
    execute_invoices: bool,
) -> tuple[tuple[str, ...], bool, bool, str | None]:
    """配送情報CSVの対象伝票番号と実行可否を決める（両経路で共通）。

    戻り値: (effective_order_numbers, do_download, execute_custom_shipping, warning)
    """
    effective = target_order_numbers
    if not effective and invoice and invoice.before_list.order_numbers:
        effective = invoice.before_list.order_numbers
    warning: str | None = None
    if (
        execute_custom_shipping
        and execute_invoices
        and invoice
        and (invoice.error or invoice.skipped_reason or not invoice.downloaded_file)
    ):
        warning = "配送情報CSVは納品書PDF一括DLが完了していないためスキップしました。"
        return effective, False, False, warning
    return effective, check_custom_shipping, execute_custom_shipping, warning


def _run_downloads_legacy(
    *,
    fetch_next_engine: bool,
    execute_downloads: bool,
    check_invoices: bool,
    execute_invoices: bool,
    verify_invoice_statuses: bool,
    check_custom_shipping: bool,
    execute_custom_shipping: bool,
    target_order_numbers: tuple[str, ...],
    output_type: str,
    headed: bool,
    slow_mo_ms: int,
    progress=None,
    profile: YamatoFlowProfile = YAMATO_PROFILE,
) -> tuple[
    YamatoBuyerDownloadResult | None,
    YamatoProductDownloadResult | None,
    InvoiceBatchDownloadResult | None,
    YamatoCustomShippingDownloadResult | None,
    list[str],
]:
    """従来どおり各取得を個別セッションで実行（verify_invoice_statuses 時のフォールバック）。"""
    buyer = product = invoice = custom_shipping = None
    workflow_warnings: list[str] = []

    if fetch_next_engine:
        _emit(progress, "ne_fetch", "running")
        try:
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
        except Exception as exc:
            _emit(progress, "ne_fetch", "failed", (str(exc).strip() or type(exc).__name__)[:140])
            raise
        _emit(progress, "ne_fetch", "completed")
    else:
        _emit(progress, "ne_fetch", "completed", "スキップ")

    if check_invoices:
        _emit(progress, "invoice", "running")
        try:
            invoice = download_yamato_invoice_batch_sync(
                execute=execute_invoices,
                order_numbers_filter=target_order_numbers,
                verify_statuses=verify_invoice_statuses,
                headless=not headed,
                slow_mo_ms=slow_mo_ms,
            )
        except Exception as exc:
            _emit(progress, "invoice", "failed", (str(exc).strip() or type(exc).__name__)[:140])
            raise
        _emit(progress, "invoice", "completed", _printed_step_detail(invoice))
    else:
        _emit(progress, "invoice", "completed", "スキップ")

    effective, do_custom, execute_custom_shipping, warning = _custom_shipping_plan(
        target_order_numbers=target_order_numbers,
        invoice=invoice,
        check_custom_shipping=check_custom_shipping,
        execute_custom_shipping=execute_custom_shipping,
        execute_invoices=execute_invoices,
    )
    if warning:
        workflow_warnings.append(warning)
    if do_custom:
        _emit(progress, "custom", "running")
        try:
            custom_shipping = download_yamato_custom_shipping_data_sync(
                execute=execute_custom_shipping,
                order_numbers_filter=effective,
                headless=not headed,
                slow_mo_ms=slow_mo_ms,
            )
        except Exception as exc:
            _emit(progress, "custom", "failed", (str(exc).strip() or type(exc).__name__)[:140])
            # ここで止まると納品書PDF取得で進んだ「印刷済み」が残るため、
            # どの伝票が変更済みかをエラー文に必ず明示する。
            notice = _printed_orders_notice(invoice)
            if notice:
                raise RuntimeError(f"{exc} {notice}") from exc
            raise
        _emit(progress, "custom", "completed")
    else:
        _emit(progress, "custom", "completed", "スキップ" if not warning else warning)

    return buyer, product, invoice, custom_shipping, workflow_warnings


async def _run_downloads_shared(
    *,
    fetch_next_engine: bool,
    execute_downloads: bool,
    check_invoices: bool,
    execute_invoices: bool,
    verify_invoice_statuses: bool,
    check_custom_shipping: bool,
    execute_custom_shipping: bool,
    target_order_numbers: tuple[str, ...],
    output_type: str,
    headed: bool,
    slow_mo_ms: int,
    progress=None,
    profile: YamatoFlowProfile = YAMATO_PROFILE,
) -> tuple[
    YamatoBuyerDownloadResult | None,
    YamatoProductDownloadResult | None,
    InvoiceBatchDownloadResult | None,
    YamatoCustomShippingDownloadResult | None,
    list[str],
]:
    """共有セッション（1ブラウザ/1ログイン/メイン機能1回）で全取得を実行する。

    各取得は同一 context の新ページで走り、ブラウザ再起動・再ログインを行わない。
    progress(key, status, detail) を工程ごとに呼び、どこまで進んだ/どこで止まったかを可視化する。
    """
    buyer = product = invoice = custom_shipping = None
    workflow_warnings: list[str] = []

    client = NextEngineYamatoClient(headless=not headed, slow_mo_ms=slow_mo_ms, profile=profile)
    async with client.open_shared_session():
        # ① NEデータ取得（購入者＋商品）
        if fetch_next_engine:
            _emit(progress, "ne_fetch", "running")
            try:
                buyer = await _run_step(
                    "購入者データ取得(NE)",
                    client.download_buyer_data(
                        execute=execute_downloads,
                        order_numbers_filter=target_order_numbers,
                    ),
                )
                product = await _run_step(
                    "商品情報データ取得(NE)",
                    client.download_product_data(
                        execute=execute_downloads,
                        output_type=output_type,
                        order_numbers_filter=target_order_numbers,
                    ),
                )
            except Exception as exc:
                _emit(progress, "ne_fetch", "failed", (str(exc).strip() or type(exc).__name__)[:140])
                raise
            _emit(progress, "ne_fetch", "completed")
        else:
            _emit(progress, "ne_fetch", "completed", "スキップ")

        # ② 納品書PDF取得
        if check_invoices:
            invoice = await _tracked_step(
                progress,
                "invoice",
                "納品書PDF取得(NE)",
                download_yamato_invoice_batch(
                    execute=execute_invoices,
                    order_numbers_filter=target_order_numbers,
                    verify_statuses=verify_invoice_statuses,
                    slow_mo_ms=slow_mo_ms,
                    client=client,
                ),
                done_detail=_printed_step_detail,
            )
        else:
            _emit(progress, "invoice", "completed", "スキップ")

        # ③ 配送情報CSV取得
        effective, do_custom, execute_custom_shipping, warning = _custom_shipping_plan(
            target_order_numbers=target_order_numbers,
            invoice=invoice,
            check_custom_shipping=check_custom_shipping,
            execute_custom_shipping=execute_custom_shipping,
            execute_invoices=execute_invoices,
        )
        if warning:
            workflow_warnings.append(warning)
        if do_custom:
            try:
                custom_shipping = await _tracked_step(
                    progress,
                    "custom",
                    "配送情報CSV取得(NE)",
                    client.download_custom_shipping_data(
                        execute=execute_custom_shipping,
                        order_numbers_filter=effective,
                    ),
                )
            except Exception as exc:
                # ここで止まると納品書PDF取得で進んだ「印刷済み」が残るため、
                # どの伝票が変更済みかをエラー文に必ず明示する。
                notice = _printed_orders_notice(invoice)
                if notice:
                    raise RuntimeError(f"{exc} {notice}") from exc
                raise
        else:
            _emit(progress, "custom", "completed", "スキップ" if not warning else warning)

    return buyer, product, invoice, custom_shipping, workflow_warnings


def _finalize_prepare(
    *,
    buyer: YamatoBuyerDownloadResult | None,
    product: YamatoProductDownloadResult | None,
    invoice: InvoiceBatchDownloadResult | None,
    custom_shipping: YamatoCustomShippingDownloadResult | None,
    workflow_warnings: list[str],
    execute_custom_shipping: bool,
    write_conversion: bool,
    preview_limit: int,
    profile: YamatoFlowProfile = YAMATO_PROFILE,
) -> YamatoB2PreparationResult:
    source_csv = custom_shipping.downloaded_file if custom_shipping and custom_shipping.downloaded_file else None
    conversion_write = write_conversion
    if execute_custom_shipping and custom_shipping and not custom_shipping.downloaded_file:
        conversion_write = False
    if workflow_warnings:
        conversion_write = False
    conversion = (
        create_ne_to_yamato_csv(source_csv=source_csv, preview_limit=preview_limit, profile=profile)
        if conversion_write
        else preview_ne_to_yamato_conversion(
            source_csv=source_csv, preview_limit=preview_limit, profile=profile
        )
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
