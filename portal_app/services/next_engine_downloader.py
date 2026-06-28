from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from portal_app.services.credentials import load_next_engine_credential
from portal_app.services.paths import PortalPaths, find_portal_paths


LOGIN_URL = "https://base.next-engine.org/users/sign_in/"
PLATFORM_URL = "https://base.next-engine.org/"
ORDER_DETAIL_URL = "https://main.next-engine.com/Userjyuchumeisai"
DATA_FILE_PREFIX = "data"

ORDER_STATUS_OPTIONS = [
    "1 : 受注メール取込済",
    "2 : 起票済(CSV/手入力)",
    "20 : 納品書印刷待ち",
    "30 : 納品書印刷中",
]
PAYMENT_OPTION = "2 : 入金済み"

APP_ROOT = Path(__file__).resolve().parents[2]
STORAGE_STATE_PATH = APP_ROOT / "data" / "storage" / "next_engine.json"
LOG_DIR = APP_ROOT / "logs" / "next_engine"
CHROMIUM_EXECUTABLE_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE"
CHROMIUM_EXECUTABLE_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]


@dataclass(frozen=True)
class NextEngineDownloadResult:
    downloaded_file: Path
    source_filename: str
    saved_at: datetime


async def download_next_engine_order_details(
    *,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> NextEngineDownloadResult:
    paths = find_portal_paths()
    downloader = NextEngineOrderDetailDownloader(paths=paths, headless=headless, slow_mo_ms=slow_mo_ms)
    return await downloader.run()


async def download_order_details_to_directory(
    *,
    destination_dir: Path,
    order_status_options: Iterable[str] | None = None,
    payment_options: Iterable[str] | None = None,
    data_file_prefix: str = DATA_FILE_PREFIX,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> NextEngineDownloadResult:
    paths = find_portal_paths()
    downloader = NextEngineOrderDetailDownloader(
        paths=paths,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
        destination_dir=destination_dir,
        order_status_options=order_status_options,
        payment_options=payment_options,
        data_file_prefix=data_file_prefix,
    )
    return await downloader.run()


def download_next_engine_order_details_sync(
    *,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> NextEngineDownloadResult:
    return asyncio.run(
        download_next_engine_order_details(headless=headless, slow_mo_ms=slow_mo_ms)
    )


def download_order_details_to_directory_sync(
    *,
    destination_dir: Path,
    order_status_options: Iterable[str] | None = None,
    payment_options: Iterable[str] | None = None,
    data_file_prefix: str = DATA_FILE_PREFIX,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
) -> NextEngineDownloadResult:
    return asyncio.run(
        download_order_details_to_directory(
            destination_dir=destination_dir,
            order_status_options=order_status_options,
            payment_options=payment_options,
            data_file_prefix=data_file_prefix,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
    )


class NextEngineOrderDetailDownloader:
    def __init__(
        self,
        *,
        paths: PortalPaths,
        headless: bool | None,
        slow_mo_ms: int,
        destination_dir: Path | None = None,
        order_status_options: Iterable[str] | None = None,
        payment_options: Iterable[str] | None = None,
        data_file_prefix: str = DATA_FILE_PREFIX,
    ) -> None:
        self.paths = paths
        self.headless = _headless_default() if headless is None else headless
        self.slow_mo_ms = slow_mo_ms
        self.destination_dir = destination_dir
        self.order_status_options = (
            tuple(ORDER_STATUS_OPTIONS)
            if order_status_options is None
            else tuple(order_status_options)
        )
        self.payment_options = (
            (PAYMENT_OPTION,)
            if payment_options is None
            else tuple(payment_options)
        )
        self.data_file_prefix = data_file_prefix
        self.credential = load_next_engine_credential()

    async def run(self) -> NextEngineDownloadResult:
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                **_chromium_launch_options(self.headless, self.slow_mo_ms)
            )
            try:
                context = await self._new_context(browser)
                try:
                    page = await context.new_page()
                    _auto_accept_dialogs(page)

                    await self._login(page)
                    result = await self._download_order_detail_csv(page)

                    await context.storage_state(path=str(STORAGE_STATE_PATH))
                    return result
                finally:
                    await context.close()
            finally:
                await browser.close()

    async def _new_context(self, browser: Browser) -> BrowserContext:
        kwargs: dict[str, object] = {
            "accept_downloads": True,
            "locale": "ja-JP",
            "viewport": {"width": 1366, "height": 900},
        }
        if STORAGE_STATE_PATH.exists():
            kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        return await browser.new_context(**kwargs)

    async def _login(self, page: Page, *, open_main: bool = False) -> None:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)
        await self._dismiss_cookie_banner(page)

        login_input = page.locator("#user_login_code")
        if await _is_visible(login_input, timeout=5000):
            await login_input.fill(self.credential.login_id)
            await page.locator("#user_password").fill(self.credential.password)
            await page.locator('input[name="commit"]').click()
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            await self._handle_news_page(page)

        await self._close_extra_news_pages(page)
        await self._remove_backdrops(page)
        if open_main:
            await self._open_main_function(page)
        await self._close_extra_news_pages(page)

    async def _open_main_function(self, page: Page) -> None:
        main_link = page.get_by_role("link", name="メイン機能")
        if await _is_visible(main_link, timeout=8000):
            await main_link.click()
            await page.wait_for_load_state("domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        try:
            consent = page.locator("#cm-acceptAll, button:has-text('同意します')")
            if await _is_visible(consent, timeout=2500):
                await consent.click()
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass

        await page.evaluate(
            'document.querySelectorAll("#cm-ov, #cc--main").forEach(e => e.remove())'
        )

    async def _handle_news_page(self, page: Page) -> None:
        if _is_next_engine_news_page(page):
            await self._dismiss_news_page(page)
            try:
                await page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1000)
            except Exception:
                pass
        else:
            await self._dismiss_news_page(page)
        await self._close_extra_news_pages(page)

    async def _dismiss_news_page(self, page: Page) -> None:
        candidates = [
            page.get_by_role("button", name="あとで見る", exact=True),
            page.locator("button:has-text('あとで見る')"),
            page.locator('button.markasread[data-newsonly="1"]'),
            page.get_by_text("すべて既読にする", exact=True),
            page.locator("a:has-text('すべて既読にする')"),
        ]
        for locator in candidates:
            if await _is_visible(locator, timeout=1500):
                await locator.click()
                await page.wait_for_timeout(1000)
                return

    async def _close_extra_news_pages(self, page: Page) -> None:
        for candidate in list(page.context.pages):
            if candidate == page:
                continue
            try:
                if not _is_next_engine_news_page(candidate):
                    continue
                await self._dismiss_news_page(candidate)
                await candidate.close()
            except Exception:
                continue

    async def _download_order_detail_csv(self, page: Page) -> NextEngineDownloadResult:
        await self._goto_order_detail_page(page)

        await self._open_search_panel(page)
        if self.order_status_options:
            await self._set_select_by_option_texts(page, self.order_status_options)
        if self.payment_options:
            await self._open_detail_search(page)
            await self._set_select_by_option_texts(page, self.payment_options)

        await self._click_search(page)
        await self._wait_for_search_result_or_download(page)
        await self._remove_backdrops(page)

        if await self._is_no_results(page):
            raise RuntimeError("Next Engine の検索結果が0件でした。CSVは保存していません。")

        download = await self._click_download(page)
        destination = self._next_data_path()
        await download.save_as(str(destination))
        return NextEngineDownloadResult(
            downloaded_file=destination,
            source_filename=download.suggested_filename,
            saved_at=datetime.now(),
        )

    async def _goto_order_detail_page(self, page: Page) -> None:
        last_error = ""
        last_screenshot: Path | None = None
        last_html: Path | None = None
        for attempt in range(1, 4):
            try:
                await page.goto(ORDER_DETAIL_URL, wait_until="domcontentloaded", timeout=60000)
                await self._remove_backdrops(page)
                await page.wait_for_selector("#jyuchu_dlg_open", timeout=30000)
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if "base.next-engine.org" in page.url:
                    try:
                        await self._open_main_function(page)
                    except Exception:
                        pass
                last_screenshot, last_html = await self._save_debug_artifacts(
                    page,
                    f"order_detail_page_not_ready_attempt_{attempt}",
                )
                if attempt < 3:
                    await page.wait_for_timeout(1500)

        title = await self._safe_page_title(page)
        excerpt = await self._safe_page_excerpt(page)
        raise RuntimeError(
            "Next Engine の受注明細一覧を直接開けませんでした。"
            f" url={page.url} title={title} last_error={last_error}"
            f" screenshot={last_screenshot} html={last_html} body_excerpt={excerpt}"
        )

    async def _open_search_panel(self, page: Page) -> None:
        candidates = [
            page.locator("#jyuchu_dlg_open"),
            page.get_by_role("button", name="検索画面を開く"),
            page.locator("button:has-text('検索画面を開く')"),
            page.locator("input[value='検索画面を開く']"),
        ]
        await _click_first_visible(candidates, "検索画面を開く")
        await page.wait_for_timeout(1500)

    async def _open_detail_search(self, page: Page) -> None:
        candidates = [
            page.get_by_role("link", name="詳細検索"),
            page.locator("a:has-text('詳細検索')"),
            page.locator("button:has-text('詳細検索')"),
        ]
        try:
            await _click_first_visible(candidates, "詳細検索")
            await page.wait_for_timeout(800)
        except RuntimeError:
            pass

    async def _click_search(self, page: Page) -> None:
        candidates = [
            page.locator("#ne_dlg_btn2_searchJyuchuDlg"),
            page.locator("#ne_dlg_btn3_searchJyuchuDlg"),
            page.locator('input[onclick="searchJyuchu.search()"]'),
            page.locator("input[type='button'][value*='検']"),
            page.get_by_role("button", name="検索"),
            page.locator("input[type='button'][value='検索']"),
            page.locator("button:has-text('検索')"),
        ]
        try:
            await _click_first_visible(candidates, "検索")
        except RuntimeError:
            await self._save_debug_artifacts(page, "search_click_failed")
            raise

    async def _click_download(self, page: Page) -> Download:
        candidates = [
            page.locator("#searchJyuchu_table_dl_lnk"),
            page.get_by_text("【ダウンロード】", exact=True),
            page.locator("a:has-text('ダウンロード')"),
            page.locator("button:has-text('ダウンロード')"),
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                await self._prepare_next_engine_download_click(page)
                async with page.expect_download(timeout=90000) as download_info:
                    await _click_first_visible(candidates, "ダウンロード")
                return await download_info.value
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await self._save_debug_artifacts(page, f"download_click_failed_attempt_{attempt}")
                if attempt < 3:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await self._wait_for_download_link(page)

        raise RuntimeError(f"Next Engine のCSVダウンロードを開始できませんでした。{last_error}")

    async def _wait_for_search_result_or_download(self, page: Page) -> None:
        try:
            await page.wait_for_function(
                """
                () => {
                  const body = document.body ? document.body.innerText : "";
                  return Boolean(document.querySelector("#searchJyuchu_table_dl_lnk"))
                    || body.includes("結果はありません")
                    || body.includes("検索結果はありません");
                }
                """,
                timeout=60000,
            )
        except PlaywrightTimeoutError as exc:
            screenshot, html = await self._save_debug_artifacts(page, "search_results_missing")
            title = await self._safe_page_title(page)
            excerpt = await self._safe_page_excerpt(page)
            raise RuntimeError(
                "Next Engine の検索結果が表示されませんでした。"
                f" url={page.url} title={title} screenshot={screenshot}"
                f" html={html} body_excerpt={excerpt}"
            ) from exc

    async def _is_no_results(self, page: Page) -> bool:
        return await page.evaluate(
            """
            () => {
              const body = document.body ? document.body.innerText : "";
              return body.includes("結果はありません") || body.includes("検索結果はありません");
            }
            """
        )

    async def _wait_for_download_link(self, page: Page) -> None:
        try:
            await page.wait_for_selector("#searchJyuchu_table_dl_lnk", timeout=30000)
        except PlaywrightTimeoutError as exc:
            screenshot, html = await self._save_debug_artifacts(page, "download_link_missing")
            raise RuntimeError(
                "Next Engine のダウンロードリンクが表示されませんでした。"
                f" url={page.url} screenshot={screenshot} html={html}"
            ) from exc

    async def _prepare_next_engine_download_click(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await self._remove_backdrops(page)

    async def _set_select_by_option_texts(self, page: Page, option_texts: Iterable[str]) -> None:
        requested = list(option_texts)
        updated = await page.evaluate(
            """
            (requested) => {
              const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const wants = requested.map(normalize);
              for (const select of document.querySelectorAll("select")) {
                const options = Array.from(select.options || []);
                const optionTexts = options.map((option) => normalize(option.textContent));
                if (!wants.every((want) => optionTexts.includes(want))) continue;

                for (const option of options) {
                  option.selected = wants.includes(normalize(option.textContent));
                }
                select.dispatchEvent(new Event("change", { bubbles: true }));
                return {
                  ok: true,
                  name: select.getAttribute("name") || "",
                  id: select.id || "",
                };
              }
              return { ok: false };
            }
            """,
            requested,
        )
        if not updated.get("ok"):
            await self._save_debug_artifacts(page, "select_not_found")
            raise RuntimeError(f"選択肢を持つ select が見つかりません: {', '.join(requested)}")

    async def _remove_backdrops(self, page: Page) -> None:
        await page.evaluate(
            """
            () => {
              document.querySelectorAll(
                ".modal-backdrop, #cm-ov, #cc--main, .bootbox, .popover, .tooltip"
              ).forEach((element) => element.remove());
              document.body.classList.remove("modal-open");
            }
            """
        )

    async def _save_debug_artifacts(self, page: Page, label: str) -> tuple[Path | None, Path | None]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(ch for ch in label if ch.isalnum() or ch in ("_", "-"))
        screenshot_path: Path | None = LOG_DIR / f"{timestamp}_{safe_label}.png"
        html_path: Path | None = LOG_DIR / f"{timestamp}_{safe_label}.html"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            screenshot_path = None
        try:
            html_path.write_text(await page.content(), encoding="utf-8")
        except Exception:
            html_path = None
        return screenshot_path, html_path

    async def _safe_page_title(self, page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            return ""

    async def _safe_page_excerpt(self, page: Page) -> str:
        try:
            text = await page.locator("body").inner_text(timeout=3000)
            return " ".join(text.split())[:500]
        except Exception:
            return ""

    def _next_data_path(self) -> Path:
        destination_dir = self.destination_dir or self.paths.order_csv_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%y%m%d%H%M")
        candidate = destination_dir / f"{self.data_file_prefix}{timestamp}.csv"
        if not candidate.exists():
            return candidate

        for index in range(1, 100):
            indexed = destination_dir / f"{self.data_file_prefix}{timestamp}_{index:02d}.csv"
            if not indexed.exists():
                return indexed
        raise RuntimeError("保存ファイル名を決定できませんでした。")


async def _click_first_visible(locators, label: str) -> None:
    for locator in locators:
        try:
            visible_locator = await _first_visible_locator(locator, timeout=2500)
            if visible_locator is not None:
                await visible_locator.click()
                return
        except Exception:
            continue
    raise RuntimeError(f"{label} をクリックできませんでした。")


async def _is_visible(locator, *, timeout: int) -> bool:
    return await _first_visible_locator(locator, timeout=timeout) is not None


async def _first_visible_locator(locator, *, timeout: int):
    try:
        count = await locator.count()
        if count == 0:
            return None
        if count == 1:
            return locator if await locator.is_visible(timeout=timeout) else None

        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible(timeout=timeout):
                return candidate
    except PlaywrightTimeoutError:
        return None
    return None


def _is_next_engine_news_page(page: Page) -> bool:
    return "/Usernotice/news" in page.url


def _auto_accept_dialogs(page: Page) -> None:
    async def accept_dialog(dialog) -> None:
        await dialog.accept()

    page.on("dialog", lambda dialog: asyncio.create_task(accept_dialog(dialog)))


def _headless_default() -> bool:
    raw = os.environ.get("NEXT_ENGINE_HEADLESS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _chromium_launch_options(headless: bool, slow_mo_ms: int) -> dict[str, object]:
    options: dict[str, object] = {
        "headless": headless,
        "slow_mo": slow_mo_ms,
    }

    executable_path = _find_chromium_executable()
    if executable_path is not None:
        options["executable_path"] = str(executable_path)
    return options


def _find_chromium_executable() -> Path | None:
    configured = os.environ.get(CHROMIUM_EXECUTABLE_ENV)
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return configured_path

    for candidate in CHROMIUM_EXECUTABLE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None
