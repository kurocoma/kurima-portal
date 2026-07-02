from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

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
from portal_app.services.paths import PortalPaths, find_portal_paths


ORDER_LIST_PRINT_WAIT_URL = "https://main.next-engine.com/Userjyuchu/index?search_condi=17"
ORDER_DETAIL_LIST_URL = "https://main.next-engine.com/Userjyuchumeisai"
CUSTOM_DATA_DOWNLOAD_URL = "https://odd.next-engine.com/download.html"
YAMATO_SHIPPING_OPTIONS = [
    "20 : ヤマト(発払い)B2v6",
    "21 : ヤマト(コレクト)B2v6",
]
CUSTOM_DELIVERY_FOLDER = "配送情報"
CUSTOM_DELIVERY_PATTERN = "新【共通】ヤマトB2V6（店舗名出力）"
CUSTOM_DELIVERY_READY_TEXT = "配送情報をダウンロードできます。"
MEISAI_OUTPUT_TYPES = frozenset({"D_ALL", "D_KEPIN", "S_ALL", "S_KEPIN", "SETS_ALL"})
YAMATO_AUDIT_LOG_DIR = APP_ROOT / "logs" / "next_engine_yamato"
YAMATO_AUDIT_LOG_PATH = YAMATO_AUDIT_LOG_DIR / "yamato_download_audit.jsonl"


@dataclass(frozen=True)
class YamatoOrderListSnapshot:
    captured_at: datetime
    count: int
    order_numbers: tuple[str, ...]
    selected_shipping_options: tuple[str, ...]


@dataclass(frozen=True)
class YamatoBuyerDownloadResult:
    executed: bool
    snapshot: YamatoOrderListSnapshot
    downloaded_file: Path | None
    source_filename: str | None
    audit_path: Path | None
    skipped_reason: str | None


@dataclass(frozen=True)
class YamatoProductDownloadResult:
    executed: bool
    snapshot: YamatoOrderListSnapshot
    downloaded_file: Path | None
    source_filename: str | None
    audit_path: Path | None
    skipped_reason: str | None
    output_type: str


@dataclass(frozen=True)
class YamatoCustomShippingDownloadResult:
    executed: bool
    pattern_name: str
    ready_to_download: bool
    downloaded_file: Path | None
    source_filename: str | None
    audit_path: Path | None
    skipped_reason: str | None
    warning_text: str | None
    order_numbers_filter: tuple[str, ...] = tuple()


async def inspect_yamato_order_list(
    *,
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoOrderListSnapshot:
    client = NextEngineYamatoClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.inspect_order_list(order_numbers_filter=order_numbers_filter)


def inspect_yamato_order_list_sync(
    *,
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoOrderListSnapshot:
    return asyncio.run(
        inspect_yamato_order_list(
            order_numbers_filter=order_numbers_filter,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def download_yamato_buyer_data(
    *,
    execute: bool,
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoBuyerDownloadResult:
    client = NextEngineYamatoClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.download_buyer_data(
        execute=execute,
        order_numbers_filter=order_numbers_filter,
    )


def download_yamato_buyer_data_sync(
    *,
    execute: bool,
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoBuyerDownloadResult:
    return asyncio.run(
        download_yamato_buyer_data(
            execute=execute,
            order_numbers_filter=order_numbers_filter,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def download_yamato_product_data(
    *,
    execute: bool,
    output_type: str = "D_ALL",
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoProductDownloadResult:
    client = NextEngineYamatoClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.download_product_data(
        execute=execute,
        output_type=output_type,
        order_numbers_filter=order_numbers_filter,
    )


def download_yamato_product_data_sync(
    *,
    execute: bool,
    output_type: str = "D_ALL",
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoProductDownloadResult:
    return asyncio.run(
        download_yamato_product_data(
            execute=execute,
            output_type=output_type,
            order_numbers_filter=order_numbers_filter,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


async def download_yamato_custom_shipping_data(
    *,
    execute: bool,
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoCustomShippingDownloadResult:
    client = NextEngineYamatoClient(headless=headless, slow_mo_ms=slow_mo_ms)
    return await client.download_custom_shipping_data(
        execute=execute,
        order_numbers_filter=order_numbers_filter,
    )


def download_yamato_custom_shipping_data_sync(
    *,
    execute: bool,
    order_numbers_filter: tuple[str, ...] = tuple(),
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> YamatoCustomShippingDownloadResult:
    return asyncio.run(
        download_yamato_custom_shipping_data(
            execute=execute,
            order_numbers_filter=order_numbers_filter,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


class NextEngineYamatoClient:
    def __init__(self, *, headless: bool | None, slow_mo_ms: int) -> None:
        self.headless = _headless_default() if headless is None else headless
        self.slow_mo_ms = slow_mo_ms
        self.paths = find_portal_paths()
        self.login_client = NextEngineOrderDetailDownloader(
            paths=self.paths,
            headless=self.headless,
            slow_mo_ms=slow_mo_ms,
        )
        # 共有セッション（prepare で複数取得を1ブラウザ/1ログインに束ねる）用。
        # None のとき各取得は従来どおり自前でブラウザ起動＋ログインする（後方互換）。
        self.shared_context = None

    @asynccontextmanager
    async def open_shared_session(self):
        """NEセッションを1回だけ開く（ブラウザ起動＋ログイン＋「メイン機能」ハンドシェイクを1回）。

        コンテキスト内では self.shared_context がセットされ、以降の
        download_buyer_data / download_product_data / download_custom_shipping_data や
        （client を渡した）invoice 取得が、同一 context の新ページで**再ログイン無し**に走る。
        prepare の各取得が毎回ブラウザ/ログインを開き直していた無駄（5起動/5ログイン）を
        1起動/1ログインに削減するためのもの。ロックはこの1回だけ保持する。
        """
        lock = _next_engine_storage_lock()
        lock.__enter__()
        playwright = None
        browser = None
        context = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                **_chromium_launch_options(self.headless, self.slow_mo_ms)
            )
            context_kwargs: dict[str, object] = {
                "accept_downloads": True,
                "locale": "ja-JP",
                "viewport": {"width": 1400, "height": 900},
            }
            if STORAGE_STATE_PATH.exists():
                context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
            context = await browser.new_context(**context_kwargs)
            login_page = await context.new_page()
            await self.login_client._login(login_page)  # メイン機能ハンドシェイク込み・1回だけ
            await context.storage_state(path=str(STORAGE_STATE_PATH))
            await login_page.close()
            self.shared_context = context
            yield context
        finally:
            self.shared_context = None
            try:
                if context is not None:
                    await context.storage_state(path=str(STORAGE_STATE_PATH))
            except Exception:
                pass
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()
            lock.__exit__(None, None, None)

    async def inspect_order_list(
        self,
        *,
        order_numbers_filter: tuple[str, ...] = tuple(),
    ) -> YamatoOrderListSnapshot:
        async with self._open_filtered_order_list(
            order_numbers_filter=order_numbers_filter,
        ) as page:
            return await _snapshot_order_list(page)

    async def download_buyer_data(
        self,
        *,
        execute: bool,
        order_numbers_filter: tuple[str, ...] = tuple(),
    ) -> YamatoBuyerDownloadResult:
        async with self._open_filtered_order_list(
            order_numbers_filter=order_numbers_filter,
        ) as page:
            snapshot = await _snapshot_order_list(page)
            if not execute:
                return YamatoBuyerDownloadResult(
                    executed=False,
                    snapshot=snapshot,
                    downloaded_file=None,
                    source_filename=None,
                    audit_path=None,
                    skipped_reason="dry_run",
                )

            if snapshot.count == 0:
                result = YamatoBuyerDownloadResult(
                    executed=True,
                    snapshot=snapshot,
                    downloaded_file=None,
                    source_filename=None,
                    audit_path=YAMATO_AUDIT_LOG_PATH,
                    skipped_reason="no_orders",
                )
                _append_audit(result)
                return result

            destination = _next_yamato_file_path(
                self.paths,
                ("ネクストエンジン受注データ", "購入者データ"),
                "dataヤマト",
                ".csv",
            )
            source_filename = await _download_ne_csv_from_current_page(
                page,
                destination,
                debug_label="yamato_buyer_download",
            )

            result = YamatoBuyerDownloadResult(
                executed=True,
                snapshot=snapshot,
                downloaded_file=destination,
                source_filename=source_filename,
                audit_path=YAMATO_AUDIT_LOG_PATH,
                skipped_reason=None,
            )
            _append_audit(result)
            return result

    async def download_product_data(
        self,
        *,
        execute: bool,
        output_type: str,
        order_numbers_filter: tuple[str, ...] = tuple(),
    ) -> YamatoProductDownloadResult:
        _validate_meisai_output_type(output_type)
        async with self._open_filtered_order_list(
            order_numbers_filter=order_numbers_filter,
        ) as page:
            snapshot = await _snapshot_order_list(page)
            if not execute:
                return YamatoProductDownloadResult(
                    executed=False,
                    snapshot=snapshot,
                    downloaded_file=None,
                    source_filename=None,
                    audit_path=None,
                    skipped_reason="dry_run",
                    output_type=output_type,
                )

            if snapshot.count == 0:
                result = YamatoProductDownloadResult(
                    executed=True,
                    snapshot=snapshot,
                    downloaded_file=None,
                    source_filename=None,
                    audit_path=YAMATO_AUDIT_LOG_PATH,
                    skipped_reason="no_orders",
                    output_type=output_type,
                )
                _append_product_audit(result)
                return result

            meisai_page = await _open_meisai_page(page, snapshot, output_type=output_type)
            destination = _next_yamato_file_path(
                self.paths,
                ("ネクストエンジン受注データ", "商品情報データ"),
                "dataヤマト",
                ".csv",
            )
            source_filename = await _download_ne_csv_from_current_page(
                meisai_page,
                destination,
                debug_label="yamato_product_download",
            )

            result = YamatoProductDownloadResult(
                executed=True,
                snapshot=snapshot,
                downloaded_file=destination,
                source_filename=source_filename,
                audit_path=YAMATO_AUDIT_LOG_PATH,
                skipped_reason=None,
                output_type=output_type,
            )
            _append_product_audit(result)
            return result

    async def download_custom_shipping_data(
        self,
        *,
        execute: bool,
        order_numbers_filter: tuple[str, ...] = tuple(),
    ) -> YamatoCustomShippingDownloadResult:
        async with self._open_custom_data_download_page() as page:
            warning_text = await _open_custom_shipping_download_modal(
                page,
                order_numbers_filter=order_numbers_filter,
            )
            if not execute:
                return YamatoCustomShippingDownloadResult(
                    executed=False,
                    pattern_name=CUSTOM_DELIVERY_PATTERN,
                    ready_to_download=True,
                    downloaded_file=None,
                    source_filename=None,
                    audit_path=None,
                    skipped_reason="dry_run",
                    warning_text=warning_text,
                    order_numbers_filter=order_numbers_filter,
                )

            destination = _next_yamato_file_path(
                self.paths,
                ("ne-yamatocsv",),
                "ne-yamato",
                ".csv",
            )
            try:
                async with page.expect_download(timeout=90000) as download_info:
                    await page.locator(".bootbox.modal .modal-footer a.btn-success").click()
                download = await download_info.value
            except PlaywrightTimeoutError:
                status_message = await _custom_shipping_status_message(page)
                if status_message:
                    result = YamatoCustomShippingDownloadResult(
                        executed=True,
                        pattern_name=CUSTOM_DELIVERY_PATTERN,
                        ready_to_download=True,
                        downloaded_file=None,
                        source_filename=None,
                        audit_path=YAMATO_AUDIT_LOG_PATH,
                        skipped_reason="no_downloadable_data",
                        warning_text=f"{warning_text} {status_message}",
                        order_numbers_filter=order_numbers_filter,
                    )
                    _append_custom_shipping_audit(result)
                    return result
                raise
            await download.save_as(str(destination))

            result = YamatoCustomShippingDownloadResult(
                executed=True,
                pattern_name=CUSTOM_DELIVERY_PATTERN,
                ready_to_download=True,
                downloaded_file=destination,
                source_filename=download.suggested_filename,
                audit_path=YAMATO_AUDIT_LOG_PATH,
                skipped_reason=None,
                warning_text=warning_text,
                order_numbers_filter=order_numbers_filter,
            )
            _append_custom_shipping_audit(result)
            return result

    def _open_filtered_order_list(
        self,
        *,
        order_numbers_filter: tuple[str, ...] = tuple(),
    ):
        return _FilteredOrderListSession(
            self,
            order_numbers_filter=order_numbers_filter,
        )

    def _open_custom_data_download_page(self):
        return _CustomDataDownloadSession(self)


class _FilteredOrderListSession:
    def __init__(
        self,
        client: NextEngineYamatoClient,
        *,
        order_numbers_filter: tuple[str, ...],
    ) -> None:
        self.client = client
        self.order_numbers_filter = order_numbers_filter
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.storage_lock = None
        self._shared = False

    async def __aenter__(self):
        # 共有セッション中はロック/ブラウザは client 側が保持済み。二重取得を避ける。
        if self.client.shared_context is None:
            self.storage_lock = _next_engine_storage_lock()
            self.storage_lock.__enter__()
        try:
            return await self._open()
        except Exception:
            await self.__aexit__(None, None, None)
            raise

    async def _open(self):
        if self.client.shared_context is not None:
            # 共有 context に新ページを開くだけ（再ログイン・再起動なし）。
            self._shared = True
            self.context = self.client.shared_context
            self.page = await self.context.new_page()
            await self._goto_order_list()
            await self.page.wait_for_timeout(1500)
            await _filter_yamato_shipping_methods(
                self.page,
                order_numbers_filter=self.order_numbers_filter,
            )
            return self.page

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            **_chromium_launch_options(self.client.headless, self.client.slow_mo_ms)
        )
        context_kwargs: dict[str, object] = {
            "accept_downloads": True,
            "locale": "ja-JP",
            "viewport": {"width": 1400, "height": 900},
        }
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        self.context = await self.browser.new_context(**context_kwargs)
        self.page = await self.context.new_page()
        await self.client.login_client._login(self.page)
        await self._goto_order_list()
        await self.page.wait_for_timeout(1500)
        await _filter_yamato_shipping_methods(
            self.page,
            order_numbers_filter=self.order_numbers_filter,
        )
        await self.context.storage_state(path=str(STORAGE_STATE_PATH))
        return self.page

    async def _goto_order_list(self) -> None:
        assert self.page is not None
        await self.page.goto(ORDER_LIST_PRINT_WAIT_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_selector("#jyuchu_dlg_open", timeout=30000)
            return
        except PlaywrightTimeoutError:
            await self.client.login_client._login(self.page)
            await self.page.goto(ORDER_LIST_PRINT_WAIT_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await self.page.wait_for_selector("#jyuchu_dlg_open", timeout=30000)
            except PlaywrightTimeoutError:
                # 失敗時に着地ページの証拠(HTML/PNG)を残す。NEのbase→main移行で main への遷移が
                # 確立できないと base に着地し #jyuchu_dlg_open が出ない（[[portal-tool-yamato-b2]] 参照）。
                await _save_yamato_debug_artifacts(self.page, "order_list_search_button_missing")
                raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._shared:
                # 共有 context は閉じない。作成したページだけ閉じる。
                if self.page is not None:
                    try:
                        await self.page.close()
                    except Exception:
                        pass
            else:
                if self.context is not None:
                    await self.context.close()
                if self.browser is not None:
                    await self.browser.close()
                if self.playwright is not None:
                    await self.playwright.stop()
        finally:
            if self.storage_lock is not None:
                self.storage_lock.__exit__(exc_type, exc, tb)
                self.storage_lock = None


class _CustomDataDownloadSession:
    def __init__(self, client: NextEngineYamatoClient) -> None:
        self.client = client
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.storage_lock = None
        self._shared = False

    async def __aenter__(self):
        if self.client.shared_context is None:
            self.storage_lock = _next_engine_storage_lock()
            self.storage_lock.__enter__()
        try:
            return await self._open()
        except Exception:
            await self.__aexit__(None, None, None)
            raise

    async def _open(self):
        if self.client.shared_context is not None:
            # 共有 context に新ページを開くだけ（再ログイン・再起動なし）。
            self._shared = True
            self.context = self.client.shared_context
            self.page = await self.context.new_page()
            await self._goto_custom_data_download_page()
            await self.page.wait_for_timeout(1500)
            return self.page

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            **_chromium_launch_options(self.client.headless, self.client.slow_mo_ms)
        )
        context_kwargs: dict[str, object] = {
            "accept_downloads": True,
            "locale": "ja-JP",
            "viewport": {"width": 1400, "height": 900},
        }
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        self.context = await self.browser.new_context(**context_kwargs)
        self.page = await self.context.new_page()
        await self.client.login_client._login(self.page)
        await self._goto_custom_data_download_page()
        await self.page.wait_for_timeout(1500)
        await self.context.storage_state(path=str(STORAGE_STATE_PATH))
        return self.page

    async def _goto_custom_data_download_page(self) -> None:
        assert self.page is not None
        await self.page.goto(CUSTOM_DATA_DOWNLOAD_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_selector("#tree", timeout=30000)
            return
        except PlaywrightTimeoutError:
            await self.client.login_client._login(self.page)
            await self.page.goto(CUSTOM_DATA_DOWNLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await self.page.wait_for_selector("#tree", timeout=30000)
                return
            except PlaywrightTimeoutError:
                await _save_yamato_debug_artifacts(self.page, "custom_data_tree_missing")
                raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._shared:
                if self.page is not None:
                    try:
                        await self.page.close()
                    except Exception:
                        pass
            else:
                if self.context is not None:
                    await self.context.close()
                if self.browser is not None:
                    await self.browser.close()
                if self.playwright is not None:
                    await self.playwright.stop()
        finally:
            if self.storage_lock is not None:
                self.storage_lock.__exit__(exc_type, exc, tb)
                self.storage_lock = None


async def _filter_yamato_shipping_methods(
    page,
    *,
    order_numbers_filter: tuple[str, ...] = tuple(),
) -> None:
    await page.locator("#jyuchu_dlg_open").click()
    await page.wait_for_selector("#sea_jyuchu_search_field05", timeout=30000)
    updated = await page.evaluate(
        """
        (wantedTexts) => {
          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const wanted = wantedTexts.map(normalize);
          const select = document.querySelector("#sea_jyuchu_search_field05");
          if (!select) return { ok: false, reason: "select_not_found" };
          const options = Array.from(select.options);
          if (!wanted.every((text) => options.some((option) => normalize(option.textContent) === text))) {
            return { ok: false, reason: "option_not_found" };
          }
          for (const option of options) {
            option.selected = wanted.includes(normalize(option.textContent));
          }
          select.dispatchEvent(new Event("change", { bubbles: true }));
          return { ok: true };
        }
        """,
        YAMATO_SHIPPING_OPTIONS,
    )
    if not updated.get("ok"):
        raise RuntimeError(f"ヤマト発送方法を選択できませんでした: {updated}")

    await _apply_order_list_order_filter(
        page,
        order_numbers_filter=order_numbers_filter,
    )
    await _click_search(page)
    await page.wait_for_timeout(3500)
    await page.wait_for_function(
        "() => !document.body.innerText.includes('件数取得中')",
        timeout=60000,
    )


async def _apply_order_list_order_filter(
    page,
    *,
    order_numbers_filter: tuple[str, ...],
) -> None:
    normalized = tuple(
        str(value).strip()
        for value in order_numbers_filter
        if str(value).strip()
    )
    if not normalized:
        return

    updated = await page.evaluate(
        """
        (values) => {
          const dispatch = (element) => {
            element.dispatchEvent(new Event("input", { bubbles: true }));
            element.dispatchEvent(new Event("change", { bubbles: true }));
          };
          const multi = document.querySelector("#sea_jyuchu_search_field01_multi");
          if (multi) {
            multi.value = values.join("\\n");
            dispatch(multi);
          }
          const single = document.querySelector("#sea_jyuchu_search_field01");
          if (single && values.length === 1) {
            single.value = values[0];
            dispatch(single);
          }
          return Boolean(multi) || Boolean(single);
        }
        """,
        list(normalized),
    )
    if updated:
        return

    raise RuntimeError("受注伝票管理の伝票番号フィルタが見つかりません。")


async def _click_search(page) -> None:
    for selector in [
        "#ne_dlg_btn3_searchJyuchuDlg",
        "#ne_dlg_btn2_searchJyuchuDlg",
        'input[onclick="searchJyuchu.search()"]',
    ]:
        locator = page.locator(selector)
        if await locator.count() > 0:
            await locator.first.click()
            return
    raise RuntimeError("受注伝票管理の検索ボタンが見つかりません。")


async def _find_open_next_engine_main_page(context):
    for candidate in reversed(context.pages):
        try:
            if not candidate.is_closed() and "main.next-engine.com" in candidate.url:
                await candidate.bring_to_front()
                return candidate
        except Exception:
            continue
    return None


async def _open_next_engine_main_from_base(page):
    if "base.next-engine.org" not in page.url:
        return page

    main_page = await _find_open_next_engine_main_page(page.context)
    if main_page is not None:
        return main_page

    launch_locator = page.locator(
        'a[href*="/apps/launch/?id=274908"], a[href*="/apps/launch"][target="base_app_1"]'
    )
    launch = await _first_visible_locator(launch_locator, timeout=10000)
    if launch is None:
        return page

    before_pages = set(page.context.pages)
    await launch.click(force=True, timeout=15000)
    await page.wait_for_timeout(2500)

    for candidate in reversed(page.context.pages):
        if candidate in before_pages:
            continue
        try:
            if candidate.is_closed():
                continue
            await candidate.wait_for_load_state("domcontentloaded", timeout=60000)
            await candidate.bring_to_front()
            return candidate
        except Exception:
            continue

    main_page = await _find_open_next_engine_main_page(page.context)
    return main_page or page


async def _open_meisai_page(
    page,
    snapshot: YamatoOrderListSnapshot,
    *,
    output_type: str,
):
    await page.locator("#all_check").click()
    await page.wait_for_function(
        """
        (expectedCount) => {
          return document.querySelectorAll('input[name="qid[]"]:checked').length === expectedCount;
        }
        """,
        arg=snapshot.count,
        timeout=30000,
    )
    checked_order_numbers = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('input[name="qid[]"]:checked'))
          .map((element) => element.value)
          .filter(Boolean)
        """
    )
    if tuple(str(value) for value in checked_order_numbers) != snapshot.order_numbers:
        raise RuntimeError("明細一覧を開く前の選択伝票番号が検索結果と一致しません。")

    await page.locator("#extension_execute_mainfunction_6").scroll_into_view_if_needed(timeout=10000)
    await page.locator("#extension_execute_mainfunction_6 a.app_icon").click(force=True, timeout=10000)
    await page.wait_for_selector("#dialog_meisai", timeout=10000)
    await page.locator(f'#dialog_meisai input[name="type"][value="{output_type}"]').check()

    async with page.context.expect_page(timeout=60000) as new_page_info:
        await page.locator("#btn_meisai_exec").click()
    meisai_page = await new_page_info.value
    await meisai_page.wait_for_load_state("domcontentloaded", timeout=60000)
    if "base.next-engine.org" in meisai_page.url:
        meisai_page = await _open_next_engine_main_from_base(meisai_page)
        if "base.next-engine.org" in meisai_page.url:
            try:
                if meisai_page is not page:
                    await meisai_page.close()
            except Exception:
                pass
            meisai_page = await _open_next_engine_main_from_base(page)
        await meisai_page.goto(ORDER_DETAIL_LIST_URL, wait_until="domcontentloaded", timeout=60000)
        await meisai_page.wait_for_timeout(2500)
    meisai_page = await _wait_for_meisai_download_link(meisai_page)
    return meisai_page


def _validate_meisai_output_type(output_type: str) -> None:
    if output_type not in MEISAI_OUTPUT_TYPES:
        allowed = ", ".join(sorted(MEISAI_OUTPUT_TYPES))
        raise ValueError(f"明細一覧の出力タイプが不正です: {output_type}。allowed={allowed}")


async def _wait_for_meisai_download_link(page):
    for attempt in range(1, 4):
        if "base.next-engine.org" in page.url:
            try:
                page = await _open_next_engine_main_from_base(page)
                await page.goto(ORDER_DETAIL_LIST_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
            except Exception:
                pass
        locator = page.locator("#searchJyuchu_table_dl_lnk")
        if await _is_visible(locator, timeout=20000):
            return page
        if attempt < 3:
            await _reload_next_engine_download_page(page)

    screenshot, html = await _save_yamato_debug_artifacts(page, "yamato_meisai_download_link_missing")
    title = await _safe_page_title(page)
    excerpt = await _safe_page_excerpt(page)
    raise RuntimeError(
        "Next Engine detail-list download link was not visible. "
        f"url={page.url} title={title} screenshot={screenshot} html={html} body_excerpt={excerpt}"
    )


async def _download_ne_csv_from_current_page(page, destination: Path, *, debug_label: str) -> str | None:
    locator = page.locator("#searchJyuchu_table_dl_lnk")
    last_error = ""
    for attempt in range(1, 4):
        await _prepare_next_engine_download_click(page)
        visible_locator = await _first_visible_locator(locator, timeout=20000)
        if visible_locator is None:
            last_error = "download_link_not_visible"
            if attempt < 3:
                await _reload_next_engine_download_page(page)
                continue
            break

        try:
            async with page.expect_download(timeout=60000) as download_info:
                await visible_locator.click(force=True)
            download = await download_info.value
            await download.save_as(str(destination))
            return download.suggested_filename
        except PlaywrightTimeoutError as exc:
            last_error = f"download_timeout:{exc}"
            if attempt < 3:
                await _reload_next_engine_download_page(page)
                continue
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}:{exc}"
            if attempt < 3:
                await _reload_next_engine_download_page(page)
                continue
            break

    screenshot, html = await _save_yamato_debug_artifacts(page, f"{debug_label}_failed")
    title = await _safe_page_title(page)
    excerpt = await _safe_page_excerpt(page)
    raise RuntimeError(
        "Next Engine CSV download did not start. "
        f"label={debug_label} url={page.url} title={title} last_error={last_error} "
        f"screenshot={screenshot} html={html} body_excerpt={excerpt}"
    )


async def _prepare_next_engine_download_click(page) -> None:
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        await page.evaluate(
            """
            () => {
              document
                .querySelectorAll(".modal-backdrop, #cm-ov, #cc--main, .bootbox, .popover, .tooltip")
                .forEach((element) => element.remove());
              document.body.classList.remove("modal-open");
            }
            """
        )
    except Exception:
        pass
    await page.wait_for_timeout(500)


async def _reload_next_engine_download_page(page) -> None:
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
    except Exception:
        await page.wait_for_timeout(2500)


async def _ensure_custom_delivery_pattern_visible(page) -> None:
    """「配送情報」フォルダを確実に展開し、子パターンが本文に現れる状態にする。

    タイトルのクリックはトグルのため、可視判定→未表示ならクリック→再判定を繰り返す
    （既に展開済みなら最初の判定で return し、無駄なクリックで畳まない）。
    展開できないときはツリーを再読込して再試行し、最後まで駄目なら証拠を残して明示エラー。
    """

    async def pattern_visible() -> bool:
        try:
            return bool(
                await page.evaluate(
                    "(p) => document.body.innerText.includes(p)", CUSTOM_DELIVERY_PATTERN
                )
            )
        except Exception:
            return False

    for _reload in range(2):
        folder = page.locator("#tree a.dynatree-title").filter(has_text=CUSTOM_DELIVERY_FOLDER)
        for _click in range(3):
            if await pattern_visible():
                return
            if await folder.count() > 0:
                try:
                    await folder.first.click()
                except Exception:
                    pass
            await page.wait_for_timeout(1500)
        if await pattern_visible():
            return
        # 展開できない → ツリーを初期状態に戻して再試行
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("#tree", timeout=30000)
            await page.wait_for_timeout(1500)
        except Exception:
            break

    if not await pattern_visible():
        await _save_yamato_debug_artifacts(page, "custom_delivery_pattern_not_expanded")
        raise RuntimeError(
            "カスタムデータ作成で「配送情報」フォルダを展開できませんでした"
            f"（パターン『{CUSTOM_DELIVERY_PATTERN}』が表示されません）。"
        )


async def _open_custom_shipping_download_modal(
    page,
    *,
    order_numbers_filter: tuple[str, ...] = tuple(),
) -> str:
    delivery_folder = page.locator("#tree a.dynatree-title").filter(has_text=CUSTOM_DELIVERY_FOLDER)
    if await delivery_folder.count() == 0:
        raise RuntimeError("カスタムデータ作成で配送情報フォルダが見つかりません。")
    # 「配送情報」フォルダのクリックはトグル（展開/折りたたみ）で、初期展開状態次第では
    # 子パターンが表示されず wait_for_function が30秒タイムアウトしていた（実機解析で確認）。
    # パターンが本文に現れるまでクリック展開を試み、駄目ならツリーを再読込して再試行する。
    await _ensure_custom_delivery_pattern_visible(page)

    pattern = page.locator("#tree a.dynatree-title").filter(has_text=CUSTOM_DELIVERY_PATTERN)
    if await pattern.count() == 0:
        raise RuntimeError(f"カスタムデータ作成で対象パターンが見つかりません: {CUSTOM_DELIVERY_PATTERN}")
    await pattern.first.click()
    await page.wait_for_timeout(1500)
    await _apply_custom_shipping_order_filter(
        page,
        order_numbers_filter=order_numbers_filter,
    )

    await page.locator("#download_button").scroll_into_view_if_needed(timeout=10000)
    await page.locator("#download_button").click()
    await page.wait_for_selector(".bootbox.modal", timeout=60000)
    await page.wait_for_function(
        "(readyText) => document.body.innerText.includes(readyText)",
        arg=CUSTOM_DELIVERY_READY_TEXT,
        timeout=60000,
    )
    warning_text = await page.evaluate(
        """
        () => {
          const modal = document.querySelector(".bootbox.modal");
          return modal ? modal.innerText.replace(/\\s+/g, " ").trim() : "";
        }
        """
    )
    if "配送情報ダウンロード済み" not in str(warning_text):
        raise RuntimeError("カスタム配送情報ダウンロードの注意文を確認できませんでした。")
    return str(warning_text)


async def _apply_custom_shipping_order_filter(
    page,
    *,
    order_numbers_filter: tuple[str, ...],
) -> None:
    if not order_numbers_filter:
        return

    normalized = tuple(str(value).strip() for value in order_numbers_filter if str(value).strip())
    if not normalized:
        return

    await page.locator('select[name="where[1].type"]').select_option("20")
    await page.wait_for_selector('textarea[name="where[1].value1"]', timeout=10000)
    await page.locator('textarea[name="where[1].value1"]').fill("\n".join(normalized))
    await page.locator('textarea[name="where[1].value1"]').dispatch_event("change")


async def _custom_shipping_status_message(page) -> str | None:
    return await page.evaluate(
        """
        () => {
          const text = document.body.innerText || "";
          const messages = [
            "受注伝票の更新に失敗しました。",
            "ダウンロードできるデータはありません。",
          ];
          const found = messages.filter((message) => text.includes(message));
          return found.length ? found.join(" ") : null;
        }
        """
    )


async def _snapshot_order_list(page) -> YamatoOrderListSnapshot:
    data = await page.evaluate(
        """
        () => {
          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const orderNumbers = Array.from(document.querySelectorAll('input[name="qid[]"]'))
            .map((element) => element.value)
            .filter(Boolean);
          const shippingSelect = document.querySelector("#sea_jyuchu_search_field05");
          const selectedShippingOptions = shippingSelect
            ? Array.from(shippingSelect.selectedOptions).map((option) => normalize(option.textContent))
            : [];
          return { orderNumbers, selectedShippingOptions };
        }
        """
    )
    order_numbers = tuple(str(value) for value in data["orderNumbers"])
    return YamatoOrderListSnapshot(
        captured_at=datetime.now(),
        count=len(order_numbers),
        order_numbers=order_numbers,
        selected_shipping_options=tuple(str(value) for value in data["selectedShippingOptions"]),
    )


def _next_yamato_file_path(
    paths: PortalPaths,
    relative_parts: tuple[str, ...],
    prefix: str,
    suffix: str,
) -> Path:
    destination_dir = paths.portal_root.joinpath("ネクストエンジン", *relative_parts)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    candidate = destination_dir / f"{prefix}{timestamp}{suffix}"
    if not candidate.exists():
        return candidate

    for index in range(1, 100):
        indexed = destination_dir / f"{prefix}{timestamp}_{index:02d}{suffix}"
        if not indexed.exists():
            return indexed
    raise RuntimeError("保存ファイル名を決定できませんでした。")


def _next_home_file_path(
    relative_parts: tuple[str, ...],
    prefix: str,
    suffix: str,
) -> Path:
    destination_dir = Path.home().joinpath(*relative_parts)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    candidate = destination_dir / f"{prefix}{timestamp}{suffix}"
    if not candidate.exists():
        return candidate

    for index in range(1, 100):
        indexed = destination_dir / f"{prefix}{timestamp}_{index:02d}{suffix}"
        if not indexed.exists():
            return indexed
    raise RuntimeError("保存ファイル名を決定できませんでした。")


async def _is_visible(locator, *, timeout: int) -> bool:
    return await _first_visible_locator(locator, timeout=timeout) is not None


async def _first_visible_locator(locator, *, timeout: int):
    try:
        count = await locator.count()
        if count <= 1:
            return locator if await locator.is_visible(timeout=timeout) else None
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible(timeout=500):
                return candidate
    except PlaywrightTimeoutError:
        return None
    except Exception:
        return None
    return None


async def _safe_page_title(page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def _safe_page_excerpt(page) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""
    return " ".join(text.split())[:500]


async def _save_yamato_debug_artifacts(page, label: str) -> tuple[Path | None, Path | None]:
    YAMATO_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%y%m%d%H%M%S")
    base = YAMATO_AUDIT_LOG_DIR / f"{label}_{timestamp}"
    screenshot_path: Path | None = base.with_suffix(".png")
    html_path: Path | None = base.with_suffix(".html")
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        screenshot_path = None
    try:
        html_path.write_text(await page.content(), encoding="utf-8")
    except Exception:
        html_path = None
    return screenshot_path, html_path


def _append_audit(result: YamatoBuyerDownloadResult) -> None:
    YAMATO_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "buyer",
        "executed": result.executed,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "source_filename": result.source_filename,
        "skipped_reason": result.skipped_reason,
        "snapshot": _snapshot_payload(result.snapshot),
    }
    with YAMATO_AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_product_audit(result: YamatoProductDownloadResult) -> None:
    YAMATO_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "product",
        "executed": result.executed,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "source_filename": result.source_filename,
        "skipped_reason": result.skipped_reason,
        "output_type": result.output_type,
        "snapshot": _snapshot_payload(result.snapshot),
    }
    with YAMATO_AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_custom_shipping_audit(result: YamatoCustomShippingDownloadResult) -> None:
    YAMATO_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "custom_shipping",
        "executed": result.executed,
        "pattern_name": result.pattern_name,
        "ready_to_download": result.ready_to_download,
        "downloaded_file": str(result.downloaded_file) if result.downloaded_file else None,
        "source_filename": result.source_filename,
        "skipped_reason": result.skipped_reason,
        "warning_text": result.warning_text,
        "order_numbers_filter": list(result.order_numbers_filter),
    }
    with YAMATO_AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _snapshot_payload(snapshot: YamatoOrderListSnapshot) -> dict[str, object]:
    payload = asdict(snapshot)
    payload["captured_at"] = snapshot.captured_at.isoformat(timespec="seconds")
    return payload
