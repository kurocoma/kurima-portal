from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from portal_app.services.next_engine_downloader import (
    APP_ROOT,
    STORAGE_STATE_PATH,
    NextEngineOrderDetailDownloader,
    _chromium_launch_options,
    _headless_default,
    _next_engine_storage_lock,
)
from portal_app.services.next_engine_order_status import (
    ORDER_INPUT_URL,
    OrderStatusRestoreResult,
    OrderStatusSnapshot,
    PRINTED_STATUS,
    PRINT_WAIT_STATUS,
    inspect_next_engine_order,
    restore_next_engine_print_wait,
)
from portal_app.services.next_engine_yamato import (
    NextEngineYamatoClient,
    YamatoOrderListSnapshot,
    _snapshot_order_list,
)
from portal_app.services.paths import find_portal_paths
from portal_app.settings import download_timeout_ms, nav_timeout_ms


ORDER_LIST_PRINT_WAIT_URL = "https://main.next-engine.com/Userjyuchu/index?search_condi=17"
INVOICE_ACTION_ID = "#extension_execute_mainfunction_4"
INVOICE_DOWNLOAD_BUTTON_ID = "#btn_nouhinsho_dl_exec"
INVOICE_AUDIT_LOG_DIR = APP_ROOT / "logs" / "next_engine_status"
INVOICE_AUDIT_LOG_PATH = INVOICE_AUDIT_LOG_DIR / "invoice_download_audit.jsonl"
INVOICE_BATCH_AUDIT_LOG_PATH = INVOICE_AUDIT_LOG_DIR / "invoice_batch_download_audit.jsonl"


@dataclass(frozen=True)
class InvoiceDownloadTestResult:
    order_no: str
    executed: bool
    before: OrderStatusSnapshot
    downloaded_file: Path | None
    after_download: OrderStatusSnapshot | None
    restore_result: OrderStatusRestoreResult | None
    skipped_reason: str | None
    error: str | None
    audit_path: Path | None


@dataclass(frozen=True)
class InvoiceBatchDownloadResult:
    executed: bool
    before_list: YamatoOrderListSnapshot
    status_verified: bool
    before_statuses: tuple[OrderStatusSnapshot, ...]
    downloaded_file: Path | None
    after_statuses: tuple[OrderStatusSnapshot, ...]
    skipped_reason: str | None
    error: str | None
    dialog_messages: tuple[str, ...]
    audit_path: Path | None


async def download_yamato_invoice_batch(
    *,
    execute: bool,
    order_numbers_filter: tuple[str, ...] = tuple(),
    verify_statuses: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    client: NextEngineYamatoClient | None = None,
) -> InvoiceBatchDownloadResult:
    resolved_headless = _headless_default() if headless is None else headless
    # client を渡すと共有セッション（1ブラウザ/1ログイン）で before_list/一括DL を実行する。
    # 渡さなければ従来どおり自前セッションを開く（後方互換）。
    owned_client = client or NextEngineYamatoClient(
        headless=resolved_headless, slow_mo_ms=slow_mo_ms
    )
    async with owned_client._open_filtered_order_list(
        order_numbers_filter=order_numbers_filter,
    ) as page:
        before_list = await _snapshot_order_list(page)

    before_statuses = (
        await _inspect_order_statuses(
            before_list.order_numbers,
            headless=resolved_headless,
            slow_mo_ms=slow_mo_ms,
        )
        if verify_statuses
        else tuple()
    )

    if not execute:
        return InvoiceBatchDownloadResult(
            executed=False,
            before_list=before_list,
            status_verified=verify_statuses,
            before_statuses=before_statuses,
            downloaded_file=None,
            after_statuses=tuple(),
            skipped_reason="dry_run",
            error=None,
            dialog_messages=tuple(),
            audit_path=None,
        )

    if before_list.count == 0:
        result = InvoiceBatchDownloadResult(
            executed=True,
            before_list=before_list,
            status_verified=verify_statuses,
            before_statuses=before_statuses,
            downloaded_file=None,
            after_statuses=tuple(),
            skipped_reason="no_orders",
            error=None,
            dialog_messages=tuple(),
            audit_path=INVOICE_BATCH_AUDIT_LOG_PATH,
        )
        _append_invoice_batch_audit(result)
        return result

    not_print_wait = [
        snapshot for snapshot in before_statuses if snapshot.status_value != PRINT_WAIT_STATUS
    ]
    if verify_statuses and not_print_wait:
        result = InvoiceBatchDownloadResult(
            executed=True,
            before_list=before_list,
            status_verified=verify_statuses,
            before_statuses=before_statuses,
            downloaded_file=None,
            after_statuses=tuple(),
            skipped_reason="start_status_not_print_wait",
            error=None,
            dialog_messages=tuple(),
            audit_path=INVOICE_BATCH_AUDIT_LOG_PATH,
        )
        _append_invoice_batch_audit(result)
        return result

    downloaded_file: Path | None = None
    after_statuses: tuple[OrderStatusSnapshot, ...] = tuple()
    dialog_messages: tuple[str, ...] = tuple()
    error: str | None = None
    try:
        downloaded_file, dialog_messages = await _download_yamato_invoice_pdf_batch(
            expected_order_numbers=before_list.order_numbers,
            order_numbers_filter=order_numbers_filter,
            headless=resolved_headless,
            slow_mo_ms=slow_mo_ms,
            client=owned_client,
        )
        if verify_statuses:
            after_statuses = await _inspect_order_statuses(
                before_list.order_numbers,
                headless=resolved_headless,
                slow_mo_ms=slow_mo_ms,
            )
            not_printed = [
                snapshot for snapshot in after_statuses if snapshot.status_value != PRINTED_STATUS
            ]
            if not_printed:
                order_numbers = ",".join(snapshot.order_no for snapshot in not_printed)
                error = f"printed_status_not_confirmed:{order_numbers}"
    except Exception as exc:
        error = str(exc)
        if verify_statuses:
            after_statuses = await _inspect_order_statuses(
                before_list.order_numbers,
                headless=resolved_headless,
                slow_mo_ms=slow_mo_ms,
            )

    result = InvoiceBatchDownloadResult(
        executed=True,
        before_list=before_list,
        status_verified=verify_statuses,
        before_statuses=before_statuses,
        downloaded_file=downloaded_file,
        after_statuses=after_statuses,
        skipped_reason=None,
        error=error,
        dialog_messages=dialog_messages,
        audit_path=INVOICE_BATCH_AUDIT_LOG_PATH,
    )
    _append_invoice_batch_audit(result)
    if error:
        # ここで止まると対象伝票が「印刷済み」のまま残り得るため、どの伝票かを
        # エラー文へ必ず明示する（利用者が手動復旧できるようにする）。
        orders = ", ".join(before_list.order_numbers)
        if downloaded_file is not None:
            notice = (
                f"※納品書PDFは取得済みのため「印刷済み」へ変更済みの伝票: {orders}"
                "（『その他の操作・納品書印刷待ちへ復旧』で戻せます）"
            )
        else:
            notice = (
                f"※対象伝票: {orders}。ダウンロード途中の失敗のため「印刷済み」へ"
                "変更された可能性があります（『その他の操作・納品書印刷待ちへ復旧』で確認・復旧できます）"
            )
        raise RuntimeError(f"ヤマト対象の納品書一括ダウンロードに失敗しました。{notice}")
    return result


def download_yamato_invoice_batch_sync(
    *,
    execute: bool,
    order_numbers_filter: tuple[str, ...] = tuple(),
    verify_statuses: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> InvoiceBatchDownloadResult:
    return asyncio.run(
        download_yamato_invoice_batch(
            execute=execute,
            order_numbers_filter=order_numbers_filter,
            verify_statuses=verify_statuses,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def test_invoice_download_and_restore(
    order_no: str,
    *,
    execute: bool,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> InvoiceDownloadTestResult:
    resolved_headless = _headless_default() if headless is None else headless
    before = await inspect_next_engine_order(
        order_no,
        headless=resolved_headless,
        slow_mo_ms=slow_mo_ms,
    )

    if not execute:
        return InvoiceDownloadTestResult(
            order_no=order_no,
            executed=False,
            before=before,
            downloaded_file=None,
            after_download=None,
            restore_result=None,
            skipped_reason="dry_run",
            error=None,
            audit_path=None,
        )

    if before.status_value != PRINT_WAIT_STATUS:
        result = InvoiceDownloadTestResult(
            order_no=order_no,
            executed=True,
            before=before,
            downloaded_file=None,
            after_download=None,
            restore_result=None,
            skipped_reason=f"start_status_not_print_wait:{before.status_value}",
            error=None,
            audit_path=INVOICE_AUDIT_LOG_PATH,
        )
        _append_invoice_audit(result)
        return result

    downloaded_file: Path | None = None
    after_download: OrderStatusSnapshot | None = None
    restore_result: OrderStatusRestoreResult | None = None
    caught: Exception | None = None

    try:
        downloaded_file = await _download_invoice_pdf(
            order_no,
            headless=resolved_headless,
            slow_mo_ms=slow_mo_ms,
        )
        after_download = await inspect_next_engine_order(
            order_no,
            headless=resolved_headless,
            slow_mo_ms=slow_mo_ms,
        )
    except Exception as exc:
        caught = exc
    finally:
        restore_result = await restore_next_engine_print_wait(
            order_no,
            execute=True,
            headless=resolved_headless,
            slow_mo_ms=slow_mo_ms,
        )

    final_snapshot = (
        restore_result.after_restore
        or restore_result.after_clear
        or restore_result.before
    )
    if caught is None and final_snapshot.status_value != PRINT_WAIT_STATUS:
        caught = RuntimeError(
            f"復旧後ステータスが印刷待ちではありません: {final_snapshot.status_text}"
        )

    result = InvoiceDownloadTestResult(
        order_no=order_no,
        executed=True,
        before=before,
        downloaded_file=downloaded_file,
        after_download=after_download,
        restore_result=restore_result,
        skipped_reason=None,
        error=str(caught) if caught else None,
        audit_path=INVOICE_AUDIT_LOG_PATH,
    )
    _append_invoice_audit(result)

    if caught is not None:
        raise RuntimeError(
            "納品書ダウンロードテスト中に失敗しました。復旧処理は実行済みです。"
        ) from caught

    return result


def test_invoice_download_and_restore_sync(
    order_no: str,
    *,
    execute: bool,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> InvoiceDownloadTestResult:
    return asyncio.run(
        test_invoice_download_and_restore(
            order_no,
            execute=execute,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def _download_invoice_pdf(
    order_no: str,
    *,
    headless: bool,
    slow_mo_ms: int,
) -> Path:
    paths = find_portal_paths()
    destination_dir = paths.portal_root / "ネクストエンジン" / "ne_納品書pdf"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / (
        f"納品書_test_{order_no}_{datetime.now():%y%m%d%H%M%S}.pdf"
    )

    login_client = NextEngineOrderDetailDownloader(
        paths=paths,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
    )

    with _next_engine_storage_lock():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **_chromium_launch_options(headless, slow_mo_ms)
            )
            try:
                context_kwargs: dict[str, object] = {
                    "accept_downloads": True,
                    "locale": "ja-JP",
                    "viewport": {"width": 1400, "height": 900},
                }
                if STORAGE_STATE_PATH.exists():
                    context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
                context = await browser.new_context(**context_kwargs)
                try:
                    page = await context.new_page()
                    page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
                    await login_client._login(page)
                    await page.goto(
                        ORDER_LIST_PRINT_WAIT_URL,
                        wait_until="domcontentloaded",
                        timeout=nav_timeout_ms(),
                    )
                    await page.wait_for_selector(
                        f'input[name="qid[]"][value="{order_no}"]',
                        timeout=30000,
                    )
                    await page.wait_for_timeout(1500)

                    await _select_only_order(page, order_no)
                    await page.locator(INVOICE_ACTION_ID).click()
                    await page.wait_for_selector(INVOICE_DOWNLOAD_BUTTON_ID, timeout=30000)
                    await _set_invoice_options(page)

                    async with page.expect_download(timeout=download_timeout_ms(120000)) as download_info:
                        await page.locator(INVOICE_DOWNLOAD_BUTTON_ID).click()
                    download = await download_info.value
                    await download.save_as(str(destination))
                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                    await page.wait_for_timeout(3000)
                    return destination
                finally:
                    await context.close()
            finally:
                await browser.close()


async def _download_yamato_invoice_pdf_batch(
    *,
    expected_order_numbers: tuple[str, ...],
    order_numbers_filter: tuple[str, ...],
    headless: bool,
    slow_mo_ms: int,
    client: NextEngineYamatoClient | None = None,
) -> tuple[Path, tuple[str, ...]]:
    paths = find_portal_paths()
    destination_dir = paths.portal_root / "ネクストエンジン" / "ne_納品書pdf"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"納品書_{datetime.now():%y%m%d%H%M%S}.pdf"

    used_client = client or NextEngineYamatoClient(headless=headless, slow_mo_ms=slow_mo_ms)
    dialog_messages: list[str] = []
    async with used_client._open_filtered_order_list(
        order_numbers_filter=order_numbers_filter,
    ) as page:
        page.on("dialog", lambda dialog: asyncio.create_task(_accept_batch_dialog(dialog, dialog_messages)))
        current = await _snapshot_order_list(page)
        if current.order_numbers != expected_order_numbers:
            raise RuntimeError(
                "納品書一括取得直前の対象伝票番号が事前確認と一致しません。"
            )

        await page.locator("#all_check").click()
        await page.wait_for_function(
            """
            (expectedCount) => {
              return document.querySelectorAll('input[name="qid[]"]:checked').length === expectedCount;
            }
            """,
            arg=current.count,
            timeout=30000,
        )
        checked_order_numbers = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('input[name="qid[]"]:checked'))
              .map((element) => element.value)
              .filter(Boolean)
            """
        )
        if tuple(str(value) for value in checked_order_numbers) != expected_order_numbers:
            raise RuntimeError("納品書一括取得前の選択伝票番号が検索結果と一致しません。")

        await page.locator(INVOICE_ACTION_ID).scroll_into_view_if_needed(timeout=10000)
        await page.locator(INVOICE_ACTION_ID).click()
        await page.wait_for_selector(INVOICE_DOWNLOAD_BUTTON_ID, timeout=30000)
        await _set_invoice_options(page, mode="H")

        async with page.expect_download(timeout=download_timeout_ms(180000)) as download_info:
            await page.locator(INVOICE_DOWNLOAD_BUTTON_ID).click()
        download = await download_info.value
        await download.save_as(str(destination))
        await page.context.storage_state(path=str(STORAGE_STATE_PATH))
        await page.wait_for_timeout(3000)
        return destination, tuple(dialog_messages)


async def _inspect_order_statuses(
    order_numbers: tuple[str, ...],
    *,
    headless: bool,
    slow_mo_ms: int,
) -> tuple[OrderStatusSnapshot, ...]:
    if not order_numbers:
        return tuple()

    paths = find_portal_paths()
    login_client = NextEngineOrderDetailDownloader(
        paths=paths,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
    )
    snapshots: list[OrderStatusSnapshot] = []
    with _next_engine_storage_lock():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **_chromium_launch_options(headless, slow_mo_ms)
            )
            try:
                context_kwargs: dict[str, object] = {
                    "accept_downloads": True,
                    "locale": "ja-JP",
                    "viewport": {"width": 1400, "height": 900},
                }
                if STORAGE_STATE_PATH.exists():
                    context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
                context = await browser.new_context(**context_kwargs)
                try:
                    page = await context.new_page()
                    await login_client._login(page)
                    for order_no in order_numbers:
                        await page.goto(
                            ORDER_INPUT_URL.format(order_no=order_no),
                            wait_until="domcontentloaded",
                            timeout=nav_timeout_ms(),
                        )
                        await page.wait_for_selector("#jyuchu_denpyo_no", timeout=30000)
                        await page.wait_for_selector("#jyuchu_jyotai_kbn", timeout=30000)
                        await page.wait_for_selector("#chk_kakunin_check_kbn", timeout=30000)
                        snapshots.append(await _snapshot_order_page(page))
                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                finally:
                    await context.close()
            finally:
                await browser.close()
    return tuple(snapshots)


async def _snapshot_order_page(page) -> OrderStatusSnapshot:
    data = await page.evaluate(
        """
        () => {
          const norm = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const status = document.querySelector("#jyuchu_jyotai_kbn");
          const confirmation = document.querySelector("#chk_kakunin_check_kbn");
          const hidden = document.querySelector("#kakunin_check_kbn");
          return {
            orderNo: document.querySelector("#jyuchu_denpyo_no")?.value || "",
            statusValue: status?.value || "",
            statusText: status ? norm(status.options[status.selectedIndex]?.textContent || "") : "",
            confirmationChecked: !!confirmation?.checked,
            confirmationHiddenValue: hidden?.value || "",
            pageTitle: document.title || "",
          };
        }
        """
    )
    return OrderStatusSnapshot(
        order_no=str(data["orderNo"]),
        status_value=str(data["statusValue"]),
        status_text=str(data["statusText"]),
        confirmation_checked=bool(data["confirmationChecked"]),
        confirmation_hidden_value=str(data["confirmationHiddenValue"]),
        page_title=str(data["pageTitle"]),
        captured_at=datetime.now(),
    )


async def _accept_batch_dialog(dialog, dialog_messages: list[str]) -> None:
    dialog_messages.append(dialog.message)
    await dialog.accept()


async def _select_only_order(page, order_no: str) -> None:
    checked_values = await page.evaluate(
        """
        (orderNo) => {
          document.querySelectorAll('input[name="qid[]"]').forEach((element) => {
            element.checked = false;
            element.dispatchEvent(new Event("change", { bubbles: true }));
          });
          const target = document.querySelector(`input[name="qid[]"][value="${orderNo}"]`);
          if (!target) return [];
          target.checked = true;
          target.dispatchEvent(new Event("click", { bubbles: true }));
          target.dispatchEvent(new Event("change", { bubbles: true }));
          return Array.from(document.querySelectorAll('input[name="qid[]"]:checked'))
            .map((element) => element.value);
        }
        """,
        order_no,
    )
    if checked_values != [order_no]:
        raise RuntimeError(f"対象伝票だけを選択できませんでした: {checked_values}")


async def _set_invoice_options(page, *, mode: str = "U") -> None:
    await _check_if_present(page, f'input[name="mode"][value="{mode}"]')
    await _check_if_present(page, 'input[name="ss"][value="-1"]')
    await _check_if_present(page, 'input[name="output"][value="PDF"]')


async def _check_if_present(page, selector: str) -> None:
    locator = page.locator(selector)
    if await locator.count() == 0:
        return
    try:
        await locator.first.check(timeout=5000)
    except PlaywrightTimeoutError:
        return


def _append_invoice_audit(result: InvoiceDownloadTestResult) -> None:
    INVOICE_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "order_no": result.order_no,
        "executed": result.executed,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "skipped_reason": result.skipped_reason,
        "error": result.error,
        "before": _snapshot_payload(result.before),
        "after_download": _snapshot_payload(result.after_download),
        "restore": _restore_payload(result.restore_result),
    }
    with INVOICE_AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_invoice_batch_audit(result: InvoiceBatchDownloadResult) -> None:
    INVOICE_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "yamato_invoice_batch",
        "executed": result.executed,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "skipped_reason": result.skipped_reason,
        "error": result.error,
        "status_verified": result.status_verified,
        "before_list": _yamato_list_payload(result.before_list),
        "before_statuses": [_snapshot_payload(snapshot) for snapshot in result.before_statuses],
        "after_statuses": [_snapshot_payload(snapshot) for snapshot in result.after_statuses],
        "dialog_messages": list(result.dialog_messages),
    }
    with INVOICE_BATCH_AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _yamato_list_payload(snapshot: YamatoOrderListSnapshot) -> dict[str, object]:
    payload = asdict(snapshot)
    payload["captured_at"] = snapshot.captured_at.isoformat(timespec="seconds")
    return payload


def _restore_payload(result: OrderStatusRestoreResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "executed": result.executed,
        "changed": result.changed,
        "skipped_reason": result.skipped_reason,
        "before": _snapshot_payload(result.before),
        "after_clear": _snapshot_payload(result.after_clear),
        "after_restore": _snapshot_payload(result.after_restore),
        "dialog_messages": list(result.dialog_messages),
    }


def _snapshot_payload(snapshot: OrderStatusSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    payload = asdict(snapshot)
    payload["captured_at"] = snapshot.captured_at.isoformat(timespec="seconds")
    return payload
