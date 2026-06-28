from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from portal_app.services.next_engine_downloader import (
    APP_ROOT,
    STORAGE_STATE_PATH,
    NextEngineOrderDetailDownloader,
    _chromium_launch_options,
    _headless_default,
)
from portal_app.services.paths import find_portal_paths


ORDER_INPUT_URL = (
    "https://main.next-engine.com/Userjyuchu/jyuchuInp"
    "?kensaku_denpyo_no={order_no}&jyuchu_meisai_order=jyuchu_meisai_gyo"
)
PRINT_WAIT_STATUS = "20"
PRINTED_STATUS = "40"
DRAFT_STATUS = "2"
AUDIT_LOG_DIR = APP_ROOT / "logs" / "next_engine_status"
AUDIT_LOG_PATH = AUDIT_LOG_DIR / "order_status_audit.jsonl"


@dataclass(frozen=True)
class OrderStatusSnapshot:
    order_no: str
    status_value: str
    status_text: str
    confirmation_checked: bool
    confirmation_hidden_value: str
    page_title: str
    captured_at: datetime


@dataclass(frozen=True)
class OrderStatusRestoreResult:
    order_no: str
    before: OrderStatusSnapshot
    after_clear: OrderStatusSnapshot | None
    after_restore: OrderStatusSnapshot | None
    executed: bool
    changed: bool
    skipped_reason: str | None
    dialog_messages: tuple[str, ...]
    audit_path: Path | None


@dataclass(frozen=True)
class OrderStatusBatchRestoreResult:
    order_numbers: tuple[str, ...]
    results: tuple[OrderStatusRestoreResult, ...]
    executed: bool
    failed_order_numbers: tuple[str, ...]
    audit_path: Path | None


async def inspect_next_engine_order(
    order_no: str,
    *,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> OrderStatusSnapshot:
    client = NextEngineOrderStatusClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.inspect(order_no)


def inspect_next_engine_order_sync(
    order_no: str,
    *,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> OrderStatusSnapshot:
    return asyncio.run(
        inspect_next_engine_order(
            order_no,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def restore_next_engine_print_wait(
    order_no: str,
    *,
    execute: bool,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> OrderStatusRestoreResult:
    client = NextEngineOrderStatusClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.restore_print_wait(order_no, execute=execute)


def restore_next_engine_print_wait_sync(
    order_no: str,
    *,
    execute: bool,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> OrderStatusRestoreResult:
    return asyncio.run(
        restore_next_engine_print_wait(
            order_no,
            execute=execute,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def restore_next_engine_print_wait_batch(
    order_numbers: tuple[str, ...],
    *,
    execute: bool,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> OrderStatusBatchRestoreResult:
    client = NextEngineOrderStatusClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.restore_print_wait_batch(order_numbers, execute=execute)


def restore_next_engine_print_wait_batch_sync(
    order_numbers: tuple[str, ...],
    *,
    execute: bool,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> OrderStatusBatchRestoreResult:
    return asyncio.run(
        restore_next_engine_print_wait_batch(
            order_numbers,
            execute=execute,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


class NextEngineOrderStatusClient:
    def __init__(self, *, headless: bool | None, slow_mo_ms: int) -> None:
        self.headless = _headless_default() if headless is None else headless
        self.slow_mo_ms = slow_mo_ms
        self.login_client = NextEngineOrderDetailDownloader(
            paths=find_portal_paths(),
            headless=self.headless,
            slow_mo_ms=slow_mo_ms,
        )

    async def inspect(self, order_no: str) -> OrderStatusSnapshot:
        async with self._open_order_page(order_no) as session:
            return await self._snapshot(session.page)

    async def restore_print_wait(self, order_no: str, *, execute: bool) -> OrderStatusRestoreResult:
        async with self._open_order_page(order_no) as session:
            before = await self._snapshot(session.page)
            if before.order_no != order_no:
                raise RuntimeError(
                    f"開いた伝票番号が一致しません: expected={order_no}, actual={before.order_no}"
                )

            if not execute:
                return OrderStatusRestoreResult(
                    order_no=order_no,
                    before=before,
                    after_clear=None,
                    after_restore=None,
                    executed=False,
                    changed=False,
                    skipped_reason="dry_run",
                    dialog_messages=tuple(session.dialog_messages),
                    audit_path=None,
                )

            if before.status_value == PRINT_WAIT_STATUS:
                result = OrderStatusRestoreResult(
                    order_no=order_no,
                    before=before,
                    after_clear=None,
                    after_restore=None,
                    executed=True,
                    changed=False,
                    skipped_reason="already_print_wait",
                    dialog_messages=tuple(session.dialog_messages),
                    audit_path=AUDIT_LOG_PATH,
                )
                _append_audit(result)
                return result

            if before.status_value not in (PRINTED_STATUS, DRAFT_STATUS):
                result = OrderStatusRestoreResult(
                    order_no=order_no,
                    before=before,
                    after_clear=None,
                    after_restore=None,
                    executed=True,
                    changed=False,
                    skipped_reason=f"unsupported_status:{before.status_value}",
                    dialog_messages=tuple(session.dialog_messages),
                    audit_path=AUDIT_LOG_PATH,
                )
                _append_audit(result)
                return result

            after_clear = before
            if before.status_value == PRINTED_STATUS and before.confirmation_checked:
                await self._set_confirmation(session.page, checked=False)
                await self._save(session.page)
                after_clear = await self._snapshot(session.page)

            await self._set_confirmation(session.page, checked=True)
            await self._save(session.page)
            after_restore = await self._snapshot(session.page)
            restored = after_restore.status_value == PRINT_WAIT_STATUS

            result = OrderStatusRestoreResult(
                order_no=order_no,
                before=before,
                after_clear=after_clear,
                after_restore=after_restore,
                executed=True,
                changed=restored,
                skipped_reason=None if restored else f"restore_failed:{after_restore.status_value}",
                dialog_messages=tuple(session.dialog_messages),
                audit_path=AUDIT_LOG_PATH,
            )
            _append_audit(result)
            return result

    async def restore_print_wait_batch(
        self,
        order_numbers: tuple[str, ...],
        *,
        execute: bool,
    ) -> OrderStatusBatchRestoreResult:
        results: list[OrderStatusRestoreResult] = []
        failed: list[str] = []
        dialog_messages: list[str] = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **_chromium_launch_options(self.headless, self.slow_mo_ms)
            )
            try:
                context = await self._new_context(browser)
                try:
                    page = await context.new_page()
                    page.on(
                        "dialog",
                        lambda dialog: asyncio.create_task(
                            _accept_status_dialog(dialog, dialog_messages)
                        ),
                    )
                    await self.login_client._login(page)

                    for order_no in order_numbers:
                        dialog_start_index = len(dialog_messages)
                        await self._goto_order_page(
                            page,
                            order_no,
                            ensure_login=False,
                        )
                        result = await self._restore_current_order_page(
                            page,
                            order_no,
                            execute=execute,
                            dialog_messages=dialog_messages,
                            dialog_start_index=dialog_start_index,
                        )
                        results.append(result)
                        final_snapshot = result.after_restore or result.after_clear or result.before
                        if execute and final_snapshot.status_value != PRINT_WAIT_STATUS:
                            failed.append(order_no)

                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                finally:
                    await context.close()
            finally:
                await browser.close()

        return OrderStatusBatchRestoreResult(
            order_numbers=order_numbers,
            results=tuple(results),
            executed=execute,
            failed_order_numbers=tuple(failed),
            audit_path=AUDIT_LOG_PATH if execute else None,
        )

    async def _restore_current_order_page(
        self,
        page: Page,
        order_no: str,
        *,
        execute: bool,
        dialog_messages: list[str],
        dialog_start_index: int,
    ) -> OrderStatusRestoreResult:
        before = await self._snapshot(page)
        if before.order_no != order_no:
            raise RuntimeError(
                f"髢九＞縺滉ｼ晉･ｨ逡ｪ蜿ｷ縺御ｸ閾ｴ縺励∪縺帙ｓ: expected={order_no}, actual={before.order_no}"
            )

        current_dialog_messages = lambda: tuple(dialog_messages[dialog_start_index:])

        if not execute:
            return OrderStatusRestoreResult(
                order_no=order_no,
                before=before,
                after_clear=None,
                after_restore=None,
                executed=False,
                changed=False,
                skipped_reason="dry_run",
                dialog_messages=current_dialog_messages(),
                audit_path=None,
            )

        if before.status_value == PRINT_WAIT_STATUS:
            result = OrderStatusRestoreResult(
                order_no=order_no,
                before=before,
                after_clear=None,
                after_restore=None,
                executed=True,
                changed=False,
                skipped_reason="already_print_wait",
                dialog_messages=current_dialog_messages(),
                audit_path=AUDIT_LOG_PATH,
            )
            _append_audit(result)
            return result

        if before.status_value not in (PRINTED_STATUS, DRAFT_STATUS):
            result = OrderStatusRestoreResult(
                order_no=order_no,
                before=before,
                after_clear=None,
                after_restore=None,
                executed=True,
                changed=False,
                skipped_reason=f"unsupported_status:{before.status_value}",
                dialog_messages=current_dialog_messages(),
                audit_path=AUDIT_LOG_PATH,
            )
            _append_audit(result)
            return result

        after_clear = before
        if before.status_value == PRINTED_STATUS and before.confirmation_checked:
            await self._set_confirmation(page, checked=False)
            await self._save(page)
            after_clear = await self._snapshot(page)

        await self._set_confirmation(page, checked=True)
        await self._save(page)
        after_restore = await self._snapshot(page)
        if after_restore.status_value == DRAFT_STATUS:
            await page.wait_for_timeout(1000)
            await self._set_confirmation(page, checked=True)
            await self._save(page)
            after_restore = await self._snapshot(page)
        restored = after_restore.status_value == PRINT_WAIT_STATUS

        result = OrderStatusRestoreResult(
            order_no=order_no,
            before=before,
            after_clear=after_clear,
            after_restore=after_restore,
            executed=True,
            changed=restored,
            skipped_reason=None if restored else f"restore_failed:{after_restore.status_value}",
            dialog_messages=current_dialog_messages(),
            audit_path=AUDIT_LOG_PATH,
        )
        _append_audit(result)
        return result

    def _open_order_page(self, order_no: str):
        return _OrderPageSession(self, order_no)

    async def _new_context(self, browser: Browser) -> BrowserContext:
        kwargs: dict[str, object] = {
            "accept_downloads": True,
            "locale": "ja-JP",
            "viewport": {"width": 1400, "height": 900},
        }
        if STORAGE_STATE_PATH.exists():
            kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        return await browser.new_context(**kwargs)

    async def _goto_order_page(
        self,
        page: Page,
        order_no: str,
        *,
        ensure_login: bool = True,
    ) -> None:
        if ensure_login:
            await self.login_client._login(page)
        await page.goto(
            ORDER_INPUT_URL.format(order_no=order_no),
            wait_until="domcontentloaded",
            timeout=60000,
        )
        try:
            await self._wait_order_page_fields(page)
        except PlaywrightTimeoutError:
            await self.login_client._login(page)
            await page.goto(
                ORDER_INPUT_URL.format(order_no=order_no),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            try:
                await self._wait_order_page_fields(page)
            except PlaywrightTimeoutError:
                await _save_order_status_debug_artifacts(page, "order_input_missing")
                raise
        await page.wait_for_timeout(1000)

    async def _wait_order_page_fields(self, page: Page) -> None:
        await page.wait_for_selector("#jyuchu_denpyo_no", timeout=30000)
        await page.wait_for_selector("#jyuchu_jyotai_kbn", timeout=30000)
        await page.wait_for_selector("#chk_kakunin_check_kbn", timeout=30000)

    async def _snapshot(self, page: Page) -> OrderStatusSnapshot:
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
            order_no=data["orderNo"],
            status_value=data["statusValue"],
            status_text=data["statusText"],
            confirmation_checked=data["confirmationChecked"],
            confirmation_hidden_value=data["confirmationHiddenValue"],
            page_title=data["pageTitle"],
            captured_at=datetime.now(),
        )

    async def _set_confirmation(self, page: Page, *, checked: bool) -> None:
        checkbox = page.locator("#chk_kakunin_check_kbn")
        await checkbox.wait_for(state="visible", timeout=30000)
        if await checkbox.is_checked() != checked:
            await checkbox.set_checked(checked)
            await page.wait_for_timeout(500)

    async def _save(self, page: Page) -> None:
        await page.locator("#syusei_btn").click()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(2500)


class _OrderPageSession:
    def __init__(self, client: NextEngineOrderStatusClient, order_no: str) -> None:
        self.client = client
        self.order_no = order_no
        self.dialog_messages: list[str] = []
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            **_chromium_launch_options(self.client.headless, self.client.slow_mo_ms)
        )
        self.context = await self.client._new_context(self.browser)
        self.page = await self.context.new_page()
        self.page.on("dialog", lambda dialog: asyncio.create_task(self._accept_dialog(dialog)))
        await self.client._goto_order_page(self.page, self.order_no)
        await self.context.storage_state(path=str(STORAGE_STATE_PATH))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def _accept_dialog(self, dialog) -> None:
        message = dialog.message
        self.dialog_messages.append(message)
        if "承認額" in message and "承認番号" in message:
            await dialog.dismiss()
            return
        if "再計算しますか" in message:
            await dialog.dismiss()
            return
        await dialog.accept()


async def _accept_status_dialog(dialog, dialog_messages: list[str]) -> None:
    message = dialog.message
    dialog_messages.append(message)
    if "再計算しますか" in message:
        await dialog.dismiss()
        return
    if "承認" in message and "承認番号" in message:
        await dialog.dismiss()
        return
    await dialog.accept()


def _append_audit(result: OrderStatusRestoreResult) -> None:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "order_no": result.order_no,
        "executed": result.executed,
        "changed": result.changed,
        "skipped_reason": result.skipped_reason,
        "before": _snapshot_payload(result.before),
        "after_clear": _snapshot_payload(result.after_clear),
        "after_restore": _snapshot_payload(result.after_restore),
        "dialog_messages": list(result.dialog_messages),
    }
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


async def _save_order_status_debug_artifacts(page: Page, label: str) -> None:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d%H%M%S")
    base = AUDIT_LOG_DIR / f"{label}_{timestamp}"
    await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")


def _snapshot_payload(snapshot: OrderStatusSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    payload = asdict(snapshot)
    payload["captured_at"] = snapshot.captured_at.isoformat(timespec="seconds")
    return payload
