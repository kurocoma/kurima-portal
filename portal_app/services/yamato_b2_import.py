from __future__ import annotations

import asyncio
import csv
import html
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from portal_app.services.next_engine_downloader import APP_ROOT, _chromium_launch_options
from portal_app.services.paths import find_portal_paths
from portal_app.services.yamato_conversion import COMPLETE_DIR_NAME, YAMATO_OUTPUT_HEADERS, latest_csv


DEFAULT_YAMATO_B2_MEMBER_LOGIN_URL = (
    "https://bmypage.kuronekoyamato.co.jp/bmypage/servlet/"
    "jp.co.kuronekoyamato.wur.hmp.servlet.user.HMPLGI0010JspServlet"
)
DEFAULT_YAMATO_B2_CLOUD_ENTRY_URL = (
    "https://bmypageapi.kuronekoyamato.co.jp/bmypageapi/sendToSpecified?sendTo=2"
)
DEFAULT_YAMATO_B2_URL = DEFAULT_YAMATO_B2_CLOUD_ENTRY_URL
YAMATO_B2_IMPORT_AUDIT_LOG_PATH = (
    APP_ROOT / "logs" / "next_engine_yamato" / "yamato_b2_import_audit.jsonl"
)
YAMATO_B2_DEBUG_DIR = APP_ROOT / "logs" / "next_engine_yamato" / "b2_import_debug"
DEFAULT_STORAGE_STATE_PATH = APP_ROOT / "data" / "storage" / "yamato_b2.json"
OPEN_B2_BROWSER_HANDLES: list[dict[str, object]] = []

LOGIN_ID_ENV = "YAMATO_B2_LOGIN_ID"
PASSWORD_ENV = "YAMATO_B2_PASSWORD"
URL_ENV = "YAMATO_B2_URL"
STORAGE_STATE_ENV = "YAMATO_B2_STORAGE_STATE"
HEADLESS_ENV = "YAMATO_B2_HEADLESS"

IMPORT_PAGE_LABELS = (
    "外部データから発行",
    "外部データ取込み",
    "外部データ取り込み",
    "外部データ取込",
    "データ取込み",
    "データ取り込み",
    "送り状発行",
)
IMPORT_SUBMIT_LABELS = (
    "取込み開始",
    "取込開始",
    "取り込み開始",
    "読み込み開始",
    "登録",
    "次へ",
)
IMPORT_CONFIRM_LABELS = (
    "登録",
    "確定",
    "はい",
    "OK",
)


@dataclass(frozen=True)
class YamatoB2ImportResult:
    step: str
    csv_file: Path | None
    source_rows: int
    source_headers: tuple[str, ...]
    ready_to_import: bool
    browser_executed: bool
    import_executed: bool
    file_selected: bool
    skipped_reason: str | None
    warnings: tuple[str, ...]
    page_title: str | None
    page_url: str | None
    page_excerpt: str | None
    screenshot_path: Path | None
    html_path: Path | None
    audit_path: Path


YAMATO_B2_IMPORT_PAGE_URL = "https://newb2web.kuronekoyamato.co.jp/ex_data_import.html"
YAMATO_B2_NEXT_ENGINE_IMPORT_LABEL = (
    "\u30cd\u30af\u30b9\u30c8\u30a8\u30f3\u30b8\u30f3\u53d6\u8fbc"
)
YAMATO_B2_IMPORT_PATTERN_SELECTORS = (
    "#torikomi_pattern",
    "select[name='torikomi_pattern']",
)
YAMATO_B2_FILE_INPUT_SELECTORS = (
    "#filename",
    "#input_file",
    "input[type='file'][accept*='.csv']",
    "input[type='file']",
)
YAMATO_B2_IMPORT_START_ROW = "2"


def import_yamato_b2_csv_sync(
    *,
    csv_file: Path | None = None,
    check_login: bool = False,
    open_import_page: bool = False,
    select_file_dry_run: bool = False,
    execute_import: bool = False,
    confirm_import: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    keep_browser_open: bool = False,
) -> YamatoB2ImportResult:
    return asyncio.run(
        import_yamato_b2_csv(
            csv_file=csv_file,
            check_login=check_login,
            open_import_page=open_import_page,
            select_file_dry_run=select_file_dry_run,
            execute_import=execute_import,
            confirm_import=confirm_import,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            keep_browser_open=keep_browser_open,
        )
    )


async def import_yamato_b2_csv(
    *,
    csv_file: Path | None = None,
    check_login: bool = False,
    open_import_page: bool = False,
    select_file_dry_run: bool = False,
    execute_import: bool = False,
    confirm_import: bool = False,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    keep_browser_open: bool = False,
) -> YamatoB2ImportResult:
    step = _requested_step(
        check_login=check_login,
        open_import_page=open_import_page,
        select_file_dry_run=select_file_dry_run,
        execute_import=execute_import,
    )
    warnings: list[str] = []
    selected_csv = _resolve_csv_file(csv_file)
    rows, headers, csv_warnings = _read_b2_csv(selected_csv)
    warnings.extend(csv_warnings)
    csv_ready = _csv_ready(headers=headers, rows=len(rows), warnings=warnings)

    if step == "validate_csv":
        result = _result(
            step=step,
            csv_file=selected_csv,
            source_rows=len(rows),
            source_headers=headers,
            ready_to_import=csv_ready,
            browser_executed=False,
            import_executed=False,
            file_selected=False,
            skipped_reason="dry_run",
            warnings=warnings,
        )
        _append_import_audit(result)
        return result

    if not csv_ready:
        result = _result(
            step=step,
            csv_file=selected_csv,
            source_rows=len(rows),
            source_headers=headers,
            ready_to_import=False,
            browser_executed=False,
            import_executed=False,
            file_selected=False,
            skipped_reason="invalid_csv",
            warnings=warnings,
        )
        _append_import_audit(result)
        return result

    if execute_import and not confirm_import:
        result = _result(
            step=step,
            csv_file=selected_csv,
            source_rows=len(rows),
            source_headers=headers,
            ready_to_import=True,
            browser_executed=False,
            import_executed=False,
            file_selected=False,
            skipped_reason="confirm_import_required",
            warnings=warnings,
        )
        _append_import_audit(result)
        return result

    login_id = os.environ.get(LOGIN_ID_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "").strip()
    if not login_id or not password:
        result = _result(
            step=step,
            csv_file=selected_csv,
            source_rows=len(rows),
            source_headers=headers,
            ready_to_import=True,
            browser_executed=False,
            import_executed=False,
            file_selected=False,
            skipped_reason="missing_yamato_b2_credentials",
            warnings=warnings,
        )
        _append_import_audit(result)
        return result

    browser_result = await _run_browser_step(
        step=step,
        csv_file=selected_csv,
        login_id=login_id,
        password=password,
        headless=_yamato_b2_headless_default() if headless is None else headless,
        slow_mo_ms=slow_mo_ms,
        keep_browser_open=keep_browser_open,
        warnings=warnings,
    )
    result = _result(
        step=step,
        csv_file=selected_csv,
        source_rows=len(rows),
        source_headers=headers,
        ready_to_import=csv_ready and browser_result["ready_to_import"],
        browser_executed=True,
        import_executed=bool(browser_result["import_executed"]),
        file_selected=bool(browser_result["file_selected"]),
        skipped_reason=browser_result["skipped_reason"],
        warnings=warnings,
        page_title=browser_result["page_title"],
        page_url=browser_result["page_url"],
        page_excerpt=browser_result["page_excerpt"],
        screenshot_path=browser_result["screenshot_path"],
        html_path=browser_result["html_path"],
    )
    _append_import_audit(result)
    return result


def _requested_step(
    *,
    check_login: bool,
    open_import_page: bool,
    select_file_dry_run: bool,
    execute_import: bool,
) -> str:
    if execute_import:
        return "execute_import"
    if select_file_dry_run:
        return "select_file_dry_run"
    if open_import_page:
        return "open_import_page"
    if check_login:
        return "check_login"
    return "validate_csv"


def _resolve_csv_file(csv_file: Path | None) -> Path:
    if csv_file is not None:
        return csv_file.expanduser().resolve()
    paths = find_portal_paths()
    completed_dir = paths.portal_root / "ネクストエンジン" / COMPLETE_DIR_NAME
    return latest_csv(completed_dir, prefix="ne-to-yamato").resolve()


def _read_b2_csv(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...], list[str]]:
    warnings: list[str] = []
    last_error: UnicodeDecodeError | None = None
    for encoding in ("cp932", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding, newline="") as fp:
                reader = csv.DictReader(fp)
                headers = tuple(reader.fieldnames or ())
                rows = [dict(row) for row in reader]
            if encoding != "cp932":
                warnings.append("B2取込CSVはCP932ではなくUTF-8 BOMとして読み取られました。")
            return rows, headers, warnings
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise UnicodeDecodeError("cp932", b"", 0, 1, str(last_error or "CSVを読み取れません。"))


def _csv_ready(*, headers: tuple[str, ...], rows: int, warnings: list[str]) -> bool:
    ready = True
    expected = tuple(YAMATO_OUTPUT_HEADERS)
    if rows <= 0:
        warnings.append("B2取込CSVにデータ行がありません。")
        ready = False
    if headers != expected:
        expected_set = set(expected)
        actual_set = set(headers)
        missing = [header for header in expected if header not in actual_set]
        unexpected = [header for header in headers if header not in expected_set]
        if missing:
            warnings.append("B2必須ヘッダーが不足しています: " + ", ".join(missing))
        if unexpected:
            warnings.append("B2想定外ヘッダーがあります: " + ", ".join(unexpected))
        if not missing and not unexpected:
            warnings.append("B2ヘッダーの並び順が想定と異なります。")
        ready = False
    return ready


async def _run_browser_step(
    *,
    step: str,
    csv_file: Path,
    login_id: str,
    password: str,
    headless: bool,
    slow_mo_ms: int,
    keep_browser_open: bool,
    warnings: list[str],
) -> dict[str, object]:
    page_title: str | None = None
    page_url: str | None = None
    page_excerpt: str | None = None
    screenshot_path: Path | None = None
    html_path: Path | None = None
    file_selected = False
    import_executed = False
    ready_to_import = False
    skipped_reason: str | None = None

    keep_open = keep_browser_open and not headless
    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await playwright.chromium.launch(
            **_chromium_launch_options(headless, slow_mo_ms)
        )
        storage_state = _storage_state_path()
        context_kwargs: dict[str, object] = {
            "accept_downloads": True,
            "locale": "ja-JP",
            "viewport": {"width": 1366, "height": 900},
        }
        if storage_state.exists():
            context_kwargs["storage_state"] = str(storage_state)
        context = await browser.new_context(**context_kwargs)
        await _install_b2_page_workarounds(context)
        page = await context.new_page()
        try:
            await _login_to_b2(page, login_id=login_id, password=password, warnings=warnings)
            storage_state.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(storage_state))

            if step in {"open_import_page", "select_file_dry_run", "execute_import"}:
                page, ready_to_import = await _open_import_page(
                    page, login_id=login_id, password=password, warnings=warnings
                )
                if not ready_to_import:
                    skipped_reason = "import_page_not_confirmed"
                else:
                    pattern_selected = await _select_next_engine_import_pattern(
                        page,
                        warnings=warnings,
                    )
                    if not pattern_selected:
                        ready_to_import = False
                        skipped_reason = "import_pattern_not_selected"

            if step in {"select_file_dry_run", "execute_import"} and ready_to_import:
                file_selected = await _select_csv_file(page, csv_file, warnings=warnings)
                if not file_selected:
                    skipped_reason = "file_input_not_found"
                    warnings.append("B2取込画面でCSVファイル入力欄を確認できませんでした。")

            if step == "execute_import" and file_selected:
                import_executed = await _execute_import_clicks(page, warnings=warnings)
                if not import_executed:
                    skipped_reason = "import_completion_not_confirmed"
        except Exception as exc:
            skipped_reason = f"browser_error:{type(exc).__name__}"
            warnings.append(str(exc))
        finally:
            debug_state = await _capture_b2_debug_state(page, step, warnings=warnings)
            page_title = debug_state["page_title"]
            page_url = debug_state["page_url"]
            page_excerpt = debug_state["page_excerpt"]
            screenshot_path = debug_state["screenshot_path"]
            html_path = debug_state["html_path"]
    finally:
        if keep_open and browser is not None and context is not None:
            _remember_open_b2_browser(
                playwright=playwright,
                browser=browser,
                context=context,
            )
        else:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            await playwright.stop()

    if step == "check_login":
        ready_to_import = True
    return {
        "ready_to_import": ready_to_import,
        "file_selected": file_selected,
        "import_executed": import_executed,
        "skipped_reason": skipped_reason,
        "page_title": page_title,
        "page_url": page_url,
        "page_excerpt": page_excerpt,
        "screenshot_path": screenshot_path,
        "html_path": html_path,
    }


def _remember_open_b2_browser(*, playwright, browser, context) -> None:
    OPEN_B2_BROWSER_HANDLES.append(
        {
            "playwright": playwright,
            "browser": browser,
            "context": context,
            "kept_at": datetime.now().isoformat(timespec="seconds"),
        }
    )


async def _install_b2_page_workarounds(context) -> None:
    await context.add_init_script(
        """
        (() => {
          const ensureChatIcon = () => {
            if (document.getElementById("chat_icon_img")) {
              return;
            }
            const img = document.createElement("img");
            img.id = "chat_icon_img";
            img.alt = "";
            img.style.display = "none";
            (document.body || document.documentElement).appendChild(img);
          };

          const originalAddEventListener = window.addEventListener.bind(window);
          window.addEventListener = (type, listener, options) => {
            if (type === "resize" && typeof listener === "function") {
              return originalAddEventListener(type, function wrappedResize(event) {
                ensureChatIcon();
                return listener.call(this, event);
              }, options);
            }
            return originalAddEventListener(type, listener, options);
          };

          document.addEventListener("DOMContentLoaded", ensureChatIcon, { once: false });
          window.addEventListener("load", ensureChatIcon, { once: false });
          setTimeout(ensureChatIcon, 0);
        })();
        """
    )


async def _capture_b2_debug_state(page, step: str, *, warnings: list[str]) -> dict[str, object]:
    page_title: str | None = None
    page_url: str | None = None
    page_excerpt: str | None = None
    screenshot_path: Path | None = None
    html_path: Path | None = None

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        page_title = await page.title()
    except Exception as exc:
        warnings.append(f"B2デバッグ情報のタイトル取得をスキップしました: {exc}")
    try:
        page_url = _sanitize_url(page.url)
    except Exception as exc:
        warnings.append(f"B2デバッグ情報のURL取得をスキップしました: {exc}")
    try:
        page_excerpt = _sanitize_excerpt(await _page_text_excerpt(page))
    except Exception as exc:
        warnings.append(f"B2デバッグ情報の本文取得をスキップしました: {exc}")
    try:
        screenshot_path, html_path = await _save_debug_artifacts(page, step)
    except Exception as exc:
        warnings.append(f"B2デバッグファイル保存をスキップしました: {exc}")
    return {
        "page_title": page_title,
        "page_url": page_url,
        "page_excerpt": page_excerpt,
        "screenshot_path": screenshot_path,
        "html_path": html_path,
    }


async def _login_to_b2(
    page, *, login_id: str, password: str, warnings: list[str], skip_initial_goto: bool = False
) -> None:
    if not skip_initial_goto:
        await page.goto(_yamato_b2_url(), wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
        await _follow_meta_refresh_if_present(page)
        await page.wait_for_timeout(1000)
    if _is_b2_system_error_url(page.url):
        warnings.append("B2の入口URLがシステムエラー画面だったため、ログイン入口へ切り替えました。")
        await page.goto(DEFAULT_YAMATO_B2_MEMBER_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
    if await _page_contains_any(
        page,
        ("送り状発行システムB2クラウド", "ログアウト", "メインメニュー"),
        timeout=3000,
    ):
        await _click_text_if_visible(page, "送り状発行システムB2クラウド", optional=True)
        return

    await _click_text_if_visible(page, "ログイン画面へ", optional=True)
    await _follow_meta_refresh_if_present(page)
    await page.wait_for_timeout(1000)
    user_filled = await _fill_first_visible(
        page,
        ("input[name='username']", "input#username", "input[type='text']"),
        login_id,
        optional=True,
    )
    if not user_filled:
        await page.goto(DEFAULT_YAMATO_B2_MEMBER_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)
        user_filled = await _fill_first_visible(
            page,
            ("input[name='username']", "input#username", "input[type='text']"),
            login_id,
            optional=True,
    )
    if not user_filled:
        raise RuntimeError("B2ログインID入力欄を確認できませんでした。")

    await _fill_first_visible(
        page,
        ("input[name='CSTMR_PSWD']", "input[name='password']", "input[type='password']"),
        password,
    )
    await _click_b2_login_submit(page)
    await page.wait_for_timeout(1000)
    if await _is_b2_member_login_page(page):
        await page.evaluate(
            """
            () => {
              if (typeof func_request_Link === "function") {
                func_request_Link("LOGIN");
              }
            }
            """
        )
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        warnings.append("B2ログイン後の画面ロード完了待ちがタイムアウトしました。")
    await page.wait_for_timeout(2500)
    if await _is_b2_member_login_page(page):
        raise RuntimeError("B2ログイン後もログイン画面に留まっています。")
    if not await _page_contains_any(
        page,
        ("送り状発行システムB2クラウド", "ログアウト", "メインメニュー"),
        timeout=5000,
    ):
        warnings.append("B2ログイン後の代表テキストを確認できませんでした。画面キャプチャを確認してください。")
    await _click_text_if_visible(page, "送り状発行システムB2クラウド", optional=True)


async def _is_b2_member_login_page(page) -> bool:
    try:
        return await page.locator("input#code1, input[name='CSTMR_PSWD']").first.is_visible(
            timeout=1000
        )
    except Exception:
        return False


async def _follow_meta_refresh_if_present(page) -> None:
    try:
        content = await page.content()
    except Exception:
        return
    match = re.search(
        r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"'][^\"']*url=([^\"']+)[\"']",
        content,
        flags=re.IGNORECASE,
    )
    if not match:
        return
    refresh_url = html.unescape(match.group(1).strip())
    if not refresh_url:
        return
    await page.goto(refresh_url, wait_until="domcontentloaded", timeout=60000)


async def _click_b2_error_login_link(page, *, warnings: list[str]) -> bool:
    """system_error 画面の「ログイン画面へ」(a#login, href=javascript::) を押してメンバーズログインへ遷移する。

    システムエラーを抜けられたら True を返す。
    """
    for selector in ("a#login", "#login", "a.w200#login", "a:has-text('ログイン画面へ')"):
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=5000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(2000)
            await _follow_meta_refresh_if_present(page)
            if not _is_b2_system_error_url(page.url):
                return True
        except Exception:
            continue
    return False


async def _retry_b2_system_error(
    page, *, login_id: str, password: str, warnings: list[str], attempts: int = 3
):
    """B2クラウドがシステムエラー画面(system_error.html)を返したときに復帰を試みる。

    復帰経路: エラー画面の「ログイン画面へ」(a#login)を押す → メンバーズログイン → 再ログイン → B2クラウドへ入り直す。
    システムエラーでなければ即座に何もせず返す（正常系は no-op）。
    """
    for index in range(1, attempts + 1):
        if not _is_b2_system_error_url(page.url):
            return page
        warnings.append(
            f"B2システムエラー画面を検出（再試行 {index}/{attempts}）。『ログイン画面へ』から再ログインします。"
        )
        await page.wait_for_timeout(2000)
        moved = await _click_b2_error_login_link(page, warnings=warnings)
        try:
            # moved=True ならメンバーズログイン画面に居るので入口gotoを省略してそのフォームを使う。
            await _login_to_b2(
                page,
                login_id=login_id,
                password=password,
                warnings=warnings,
                skip_initial_goto=moved,
            )
        except Exception as exc:
            warnings.append(f"B2再ログインに失敗（再試行 {index}）: {exc}")
            if moved:
                # 直接クリック経路が失敗したら、入口からの通常ログインでフォールバック。
                try:
                    await _login_to_b2(
                        page, login_id=login_id, password=password, warnings=warnings
                    )
                except Exception as exc2:
                    warnings.append(f"B2再ログイン(入口経由)も失敗: {exc2}")
                    continue
            else:
                continue
        page = await _enter_b2_cloud(page, warnings=warnings)
    if _is_b2_system_error_url(page.url):
        warnings.append(
            "B2クラウドのシステムエラーが解消しませんでした。時間をおいて再実行してください。"
        )
    return page


async def _enter_and_locate_import_page(page, *, warnings: list[str]):
    """B2クラウドへ入り、外部データ取込画面まで遷移する。到達できたら (page, True)。

    重要: 取込画面へは**UIクリック導線**（B2クラウド→外部データから発行タイル）でのみ遷移する。
    取込URL(ex_data_import.html)への page.goto は「URL直接指定」と判定され system_error を誘発するため使わない
    （実機で確認: 手動クリック導線では出ないが直URLでは system_error。エラー画面の原因欄にも
    「②ブックマーク等でURLを直接指定」「④複数タブ」と明記）。
    """
    page = await _enter_b2_cloud(page, warnings=warnings)
    if await _is_import_page(page):
        return page, True
    if await _click_external_data_import_tile(page, warnings=warnings):
        return page, True

    for label in IMPORT_PAGE_LABELS:
        clicked = await _click_text_if_visible(page, label, optional=True)
        if not clicked:
            continue
        await page.wait_for_timeout(1200)
        if await _is_import_page(page):
            return page, True
    return page, False


async def _open_import_page(page, *, login_id: str, password: str, warnings: list[str]):
    page, ok = await _enter_and_locate_import_page(page, warnings=warnings)
    if ok:
        return page, True

    # 取込画面に到達できず、B2クラウドがシステムエラー(useServiceのybmContextRoot未定義等で system_error.html)に
    # 落ちた場合は、「ログイン画面へ」→再ログイン→B2クラウド入り直し で復帰し、もう一度だけ取込画面遷移を試す。
    # （再ログイン後はページが初期化され直り useService が通る、というユーザー確認済みの手動復帰経路を自動化）
    if _is_b2_system_error_url(page.url):
        page = await _retry_b2_system_error(
            page, login_id=login_id, password=password, warnings=warnings
        )
        page, ok = await _enter_and_locate_import_page(page, warnings=warnings)
        if ok:
            return page, True

    warnings.append("B2取込画面への遷移候補をクリックできませんでした。保存HTMLでセレクタ確認が必要です。")
    return page, False


async def _click_external_data_import_tile(page, *, warnings: list[str]) -> bool:
    try:
        clicked = await page.evaluate(
            """() => {
              if (typeof pageGuard === "function") {
                pageGuard(false);
              }
              window.guardFlg = false;
              for (const selector of ["#pageGuardBack", "#pageGuardLoader", "#pageGuardLoaderText"]) {
                const element = document.querySelector(selector);
                if (element) {
                  element.remove();
                }
              }

              const tile = document.querySelector("#ex_data_import");
              if (!tile) {
                return false;
              }
              if (tile.scrollIntoView) {
                tile.scrollIntoView({ block: "center", inline: "center" });
              }
              const jq = window.jQuery || window.$;
              if (jq) {
                setTimeout(() => jq(tile).trigger("click"), 0);
              } else {
                setTimeout(() => {
                  tile.dispatchEvent(new MouseEvent("click", {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                  }));
                }, 0);
              }
              return true;
            }"""
        )
        if clicked:
            try:
                await page.wait_for_url("**/ex_data_import.html", timeout=10000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(2000)
            if await _is_import_page(page):
                return True
    except Exception as exc:
        warnings.append(f"B2 external-data tile direct click failed: {exc}")

    selectors = (
        "#ex_data_import a",
        "#ex_data_import",
        "div.topitem:has-text('外部データから発行')",
        "h3:has-text('外部データから発行')",
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await _reset_b2_page_guard(page)
            await locator.wait_for(state="attached", timeout=10000)
            await locator.scroll_into_view_if_needed(timeout=5000)
            await _activate_js_link_locator(page, locator, force=True)
            await page.wait_for_timeout(5000)
            if await _is_import_page(page):
                return True
        except Exception:
            continue

    try:
        await _reset_b2_page_guard(page)
        clicked = await page.evaluate(
            """() => {
              const tile = document.querySelector("#ex_data_import");
              if (!tile) return false;
              if (tile.scrollIntoView) {
                tile.scrollIntoView({ block: "center", inline: "center" });
              }
              const jq = window.jQuery || window.$;
              if (jq) {
                setTimeout(() => jq(tile).trigger("click"), 0);
              } else {
                setTimeout(() => tile.click(), 0);
              }
              return true;
            }"""
        )
        if clicked:
            await page.wait_for_timeout(5000)
            if await _is_import_page(page):
                return True
    except Exception as exc:
        warnings.append(f"B2メインメニューの外部データから発行をJavaScriptでクリックできませんでした: {exc}")
    return False


async def _reset_b2_page_guard(page) -> None:
    try:
        await page.evaluate(
            """() => {
              if (typeof pageGuard === "function") {
                pageGuard(false);
              }
              window.guardFlg = false;
              for (const selector of ["#pageGuardBack", "#pageGuardLoader", "#pageGuardLoaderText"]) {
                const element = document.querySelector(selector);
                if (element) {
                  element.remove();
                }
              }
            }"""
        )
    except Exception:
        return


async def _enter_b2_cloud(page, *, warnings: list[str]):
    if await _is_b2_cloud_page(page):
        return page

    # B2クラウド(newb2web)へは**UIクリック導線**で入る。sendToSpecified 等の「直URL遷移」は
    # system_error を誘発するため使わない（実機ライブ解析で確定。エラー画面の原因欄にも
    # 「②ブックマーク等でURL直接指定」と明記）。確定した導線:
    #   メンバーズメニュー「送り状発行システムB2クラウド」= a[href*="openSelectedForyouService('06')"] → サービス詳細(SV0102)
    #   → 「このサービスを利用する」= ybmCommonJs.useService('06','1_2') → B2クラウド TOP(newb2web/main_menu.html)

    # STEP1: メンバーズメニュー → サービス詳細(SV0102)。既にSV0102（「このサービスを利用する」有）ならスキップ。
    on_detail = await _page_contains_any(page, ("このサービスを利用する",), timeout=1500)
    if not on_detail:
        try:
            entry = page.locator("a[href*=\"openSelectedForyouService('06')\"]").first
            if await entry.count() == 0:
                entry = page.get_by_text("送り状発行システムB2クラウド").first
            if await entry.count() > 0:
                page = await _click_with_optional_popup(page, entry)
                await page.wait_for_timeout(2500)
                if await _is_b2_cloud_page(page):
                    return page
        except Exception as exc:
            warnings.append(f"メンバーズの「送り状発行システムB2クラウド」クリックに失敗: {exc}")

    # STEP2: サービス詳細「このサービスを利用する」(useService('06','1_2')) → B2クラウド TOP
    if await _page_contains_any(page, ("このサービスを利用する",), timeout=3000):
        locators = [
            page.locator('a[onclick*="useService(\'06\'"]').first,
            page.get_by_text("このサービスを利用する", exact=True).first,
            page.locator("a.js-add-menu-01ex").first,
        ]
        for locator in locators:
            try:
                if await locator.count() > 0 and await locator.is_visible(timeout=2000):
                    page = await _click_with_optional_popup(page, locator)
                    await page.wait_for_timeout(3000)
                    if await _is_b2_cloud_page(page):
                        return page
            except Exception:
                continue
        try:
            page = await _evaluate_with_optional_popup(
                page,
                """() => {
                  if (window.ybmCommonJs && typeof ybmCommonJs.useService === "function") {
                    ybmCommonJs.useService("06", "1_2");
                  }
                }""",
            )
        except Exception as exc:
            warnings.append(f"B2クラウド起動(useService)に失敗: {exc}")
    return page


async def _click_with_optional_popup(page, locator):
    popup_task = asyncio.create_task(page.context.wait_for_event("page", timeout=10000))
    try:
        await _activate_js_link_locator(page, locator)
    except Exception:
        await _cancel_popup_wait(popup_task)
        raise
    return await _page_after_possible_popup(page, popup_task)


async def _evaluate_with_optional_popup(page, script: str):
    popup_task = asyncio.create_task(page.context.wait_for_event("page", timeout=10000))
    try:
        await page.evaluate(script)
    except Exception:
        await _cancel_popup_wait(popup_task)
        raise
    return await _page_after_possible_popup(page, popup_task)


async def _page_after_possible_popup(page, popup_task):
    try:
        active_page = await popup_task
        await active_page.wait_for_load_state("domcontentloaded", timeout=60000)
    except Exception:
        active_page = page
    await active_page.wait_for_timeout(5000)
    await _follow_meta_refresh_if_present(active_page)
    await active_page.wait_for_timeout(1000)
    return active_page


async def _cancel_popup_wait(popup_task) -> None:
    popup_task.cancel()
    try:
        await popup_task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _is_b2_cloud_page(page) -> bool:
    # system_error.html も同一ドメインのため、エラー画面を「B2クラウド到達」と誤認しないよう除外する。
    return (
        "newb2web.kuronekoyamato.co.jp" in page.url
        and not _is_b2_system_error_url(page.url)
    )


def _is_b2_system_error_url(url: str | None) -> bool:
    return bool(url and "newb2web.kuronekoyamato.co.jp/system_error" in url)


async def _is_import_page(page) -> bool:
    if "newb2web.kuronekoyamato.co.jp/ex_data_import" in page.url:
        return True
    if await _file_input_visible(page):
        return True
    if "bmypage.kuronekoyamato.co.jp/bmypage/SV0102" in page.url:
        return False
    return await _page_contains_any(
        page,
        ("取込みパターン", "取込み開始", "紐付け設定"),
        timeout=1000,
    )


async def _select_next_engine_import_pattern(page, *, warnings: list[str]) -> bool:
    selector = ", ".join(YAMATO_B2_IMPORT_PATTERN_SELECTORS)
    try:
        await page.locator(selector).first.wait_for(state="attached", timeout=15000)
    except Exception as exc:
        warnings.append(f"B2 import pattern selector was not found: {exc}")
        return False

    if await _selected_import_pattern_text(page) == YAMATO_B2_NEXT_ENGINE_IMPORT_LABEL:
        return True

    pattern = page.locator(selector).first
    try:
        await pattern.select_option(label=YAMATO_B2_NEXT_ENGINE_IMPORT_LABEL, timeout=10000)
    except Exception:
        try:
            selected = await pattern.evaluate(
                """(select, label) => {
                  const options = Array.from(select.options || []);
                  const option = options.find(
                    (item) => (item.textContent || "").trim() === label
                  );
                  if (!option) {
                    return false;
                  }
                  select.selectedIndex = options.indexOf(option);
                  select.dispatchEvent(new Event("input", { bubbles: true }));
                  select.dispatchEvent(new Event("change", { bubbles: true }));
                  return true;
                }""",
                YAMATO_B2_NEXT_ENGINE_IMPORT_LABEL,
            )
            if not selected:
                warnings.append("B2 Next Engine import pattern option was not found.")
                return False
        except Exception as exc:
            warnings.append(f"B2 Next Engine import pattern could not be selected: {exc}")
            return False

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(1000)

    if await _selected_import_pattern_text(page) != YAMATO_B2_NEXT_ENGINE_IMPORT_LABEL:
        warnings.append("B2 Next Engine import pattern selection was not confirmed.")
        return False

    try:
        await page.locator("#file_button, #filename, #input_file").first.wait_for(
            state="attached",
            timeout=15000,
        )
    except Exception as exc:
        warnings.append(f"B2 file selector did not appear after pattern selection: {exc}")
        return False
    await _set_b2_import_start_row(page)
    return True


async def _selected_import_pattern_text(page) -> str:
    selector = ", ".join(YAMATO_B2_IMPORT_PATTERN_SELECTORS)
    try:
        return await page.locator(selector).first.evaluate(
            """(select) => {
              const option = select.options ? select.options[select.selectedIndex] : null;
              return option ? (option.textContent || "").trim() : "";
            }"""
        )
    except Exception:
        return ""


async def _select_csv_file(page, csv_file: Path, *, warnings: list[str]) -> bool:
    # B2の「ファイル選択」はネイティブのファイルダイアログを開く方式。file_chooser で読ませると
    # B2のJSがCSVを解析し「データ抜粋」「紐付け」が埋まり、取込み開始が有効化される（実機確認済み）。
    # 隠し #input_file への set_input_files だけでは B2 のJSが読み込まず、紐付けが空のまま取込不可になる。
    csv_str = str(csv_file)
    loaded = False
    for button_sel in ("#file_button", "a#file_button", "text=ファイル選択"):
        try:
            button = page.locator(button_sel).first
            if await button.count() == 0:
                continue
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await button.click(timeout=5000)
            chooser = await fc_info.value
            await chooser.set_files(csv_str)
            loaded = True
            break
        except Exception:
            continue

    if not loaded:
        # フォールバック: 隠し file input へ直接セット（旧方式・環境により有効な場合あり）
        for selector in YAMATO_B2_FILE_INPUT_SELECTORS:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                for index in range(count):
                    candidate = locator.nth(index)
                    await candidate.wait_for(state="attached", timeout=5000)
                    await candidate.set_input_files(csv_str)
                    await candidate.evaluate(
                        """(element) => {
                          element.dispatchEvent(new Event("input", { bubbles: true }));
                          element.dispatchEvent(new Event("change", { bubbles: true }));
                        }"""
                    )
                    loaded = True
            except Exception:
                continue

    await _set_b2_import_start_row(page)
    try:
        await _wait_for_csv_file_ready(page, csv_file.name)
    except PlaywrightTimeoutError:
        pass
    if await _b2_import_start_enabled(page) and await _b2_selected_file_matches(page, csv_file.name):
        return True
    if loaded:
        warnings.append("B2 CSV file was set, but the import start button was not enabled.")
    return False


async def _set_b2_import_start_row(page) -> None:
    try:
        start_row = page.locator("#torikomi_strat_row").first
        if await start_row.count():
            await start_row.fill(YAMATO_B2_IMPORT_START_ROW)
            await start_row.evaluate(
                """(element) => {
                  element.dispatchEvent(new Event("input", { bubbles: true }));
                  element.dispatchEvent(new Event("change", { bubbles: true }));
                }"""
            )
    except Exception:
        return


async def _wait_for_csv_file_ready(page, expected_file_name: str) -> None:
    await page.wait_for_function(
        """(expectedFileName) => {
          const valueOf = (selector) => {
            const element = document.querySelector(selector);
            return element && "value" in element ? element.value || "" : "";
          };
          const displayValue = valueOf("#file");
          const fileValues = Array.from(
            document.querySelectorAll("#filename, #input_file, input[type='file']")
          ).map((input) => {
            const selected = input.files && input.files[0] ? input.files[0].name : "";
            return selected || input.value || "";
          });
          const hasExpectedFile = [displayValue, ...fileValues].some(
            (value) => value && value.includes(expectedFileName)
          );
          const start = document.querySelector("#import_start");
          const startEnabled = !!(start
            && !start.hasAttribute("disabled")
            && !start.classList.contains("disable"));
          return hasExpectedFile && startEnabled;
        }""",
        arg=expected_file_name,
        timeout=15000,
    )


async def _b2_selected_file_matches(page, expected_file_name: str) -> bool:
    try:
        return bool(
            await page.evaluate(
                """(expectedFileName) => {
                  const valueOf = (selector) => {
                    const element = document.querySelector(selector);
                    return element && "value" in element ? element.value || "" : "";
                  };
                  const displayValue = valueOf("#file");
                  const fileValues = Array.from(
                    document.querySelectorAll("#filename, #input_file, input[type='file']")
                  ).map((input) => {
                    const selected = input.files && input.files[0] ? input.files[0].name : "";
                    return selected || input.value || "";
                  });
                  return [displayValue, ...fileValues].some(
                    (value) => value && value.includes(expectedFileName)
                  );
                }""",
                expected_file_name,
            )
        )
    except Exception:
        return False


async def _execute_import_clicks(page, *, warnings: list[str]) -> bool:
    clicked = False
    try:
        if await page.locator("#import_start").is_visible(timeout=3000) and await _b2_import_start_enabled(page):
            await page.locator("#import_start").click()
            clicked = True
            try:
                await page.wait_for_url("**/ex_import_result_display.html", timeout=60000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(5000)
    except Exception:
        pass

    if not clicked:
        for label in IMPORT_SUBMIT_LABELS:
            if await _click_text_if_visible(page, label, optional=True):
                clicked = True
                break
    if not clicked:
        warnings.append("B2取込開始ボタンを確認できませんでした。")
        return False

    await page.wait_for_timeout(2000)
    for label in IMPORT_CONFIRM_LABELS:
        if await _click_text_if_visible(page, label, optional=True):
            await page.wait_for_timeout(2000)
            break

    if "newb2web.kuronekoyamato.co.jp/ex_import_result_display" in page.url:
        await _append_b2_result_warnings(page, warnings)
        return True
    text = await _page_text_excerpt(page)
    if any(word in text for word in ("取込み結果一覧", "正常取込件数", "エラー件数")):
        await _append_b2_result_warnings(page, warnings)
        return True
    warnings.append("B2取込実行後の完了文言を確認できませんでした。")
    return False


async def _append_b2_result_warnings(page, warnings: list[str]) -> None:
    try:
        counts = await page.evaluate(
            """() => {
              const value = (selector) => {
                const element = document.querySelector(selector);
                return element ? element.textContent.trim() : "";
              };
              return {
                imported: value("#num_of_import"),
                issuable: value("#num_of_issuable"),
                warning: value("#num_of_warning"),
                error: value("#num_of_error"),
                unissuable: value("#num_of_issuunable"),
              };
            }"""
        )
    except Exception:
        return

    imported = _count_from_label(str(counts.get("imported", "")))
    issuable = _count_from_label(str(counts.get("issuable", "")))
    warning_count = _count_from_label(str(counts.get("warning", "")))
    error_count = _count_from_label(str(counts.get("error", "")))
    unissuable = _count_from_label(str(counts.get("unissuable", "")))
    if error_count or warning_count or unissuable or (imported and issuable != imported):
        warnings.append(
            "B2取込結果: "
            f"取込み{imported}件, 発行可能{issuable}件, "
            f"確認必要{warning_count}件, 修正必要{error_count}件, 発送不可{unissuable}件"
        )


def _count_from_label(value: str) -> int:
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


async def _b2_import_start_enabled(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const start = document.querySelector("#import_start");
                  return !!(start
                    && !start.hasAttribute("disabled")
                    && !start.classList.contains("disable"));
                }"""
            )
        )
    except Exception:
        return False


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


async def _click_text_if_visible(page, text: str, *, optional: bool = False) -> bool:
    candidates = [
        page.get_by_role("button", name=text, exact=True),
        page.get_by_role("link", name=text, exact=True),
        page.locator(f"a.login:has-text('{text}')"),
        page.locator(f"a:has-text('{text}')"),
        page.locator(f"button:has-text('{text}')"),
        page.locator(f"input[value*='{text}']"),
        page.get_by_text(text, exact=True),
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
                    await _activate_js_link_locator(page, candidate)
                    await page.wait_for_timeout(500)
                    return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    if not optional:
        raise RuntimeError(f"{text} をクリックできませんでした。")
    return False


async def _activate_js_link_locator(page, locator, *, force: bool = False) -> bool:
    try:
        await locator.click(force=force, timeout=5000)
        return True
    except Exception:
        pass

    try:
        return bool(
            await locator.evaluate(
                """(element) => {
                  const unique = (items) => Array.from(new Set(items.filter(Boolean)));
                  const candidates = unique([
                    element,
                    element.closest("a"),
                    element.querySelector ? element.querySelector("a") : null,
                    element.closest(".topitem"),
                    element.closest(".topmenu"),
                    element.parentElement,
                  ]);
                  const events = ["pointerdown", "mousedown", "pointerup", "mouseup", "click"];
                  for (const target of candidates) {
                    try {
                      if (target.scrollIntoView) {
                        target.scrollIntoView({ block: "center", inline: "center" });
                      }
                      for (const eventName of events) {
                        target.dispatchEvent(new MouseEvent(eventName, {
                          bubbles: true,
                          cancelable: true,
                          view: window,
                        }));
                      }
                      if (typeof target.click === "function") {
                        target.click();
                      }
                      const jq = window.jQuery || window.$;
                      if (jq) {
                        jq(target).trigger("click");
                      }
                    } catch (error) {
                      continue;
                    }
                  }
                  return candidates.length > 0;
                }"""
            )
        )
    except Exception:
        return False


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


async def _fill_first_visible(
    page,
    selectors: tuple[str, ...],
    value: str,
    *,
    optional: bool = False,
) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = await locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible(timeout=2500):
                    await candidate.fill(value)
                    return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    if not optional:
        raise RuntimeError(f"入力欄が見つかりません: {', '.join(selectors)}")
    return False


async def _file_input_visible(page) -> bool:
    locator = page.locator("input[type='file']")
    try:
        count = await locator.count()
        for index in range(count):
            if await locator.nth(index).is_visible(timeout=1000):
                return True
    except Exception:
        return False
    return False


async def _page_contains_any(page, values: Iterable[str], *, timeout: int) -> bool:
    try:
        text = await page.locator("body").inner_text(timeout=timeout)
    except Exception:
        return False
    return any(value in text for value in values)


async def _page_text_excerpt(page) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""
    return " ".join(text.split())[:1000]


async def _save_debug_artifacts(page, label: str) -> tuple[Path | None, Path]:
    YAMATO_B2_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(ch for ch in label if ch.isalnum() or ch in ("_", "-"))
    screenshot_path = YAMATO_B2_DEBUG_DIR / f"{timestamp}_{safe_label}.png"
    html_path = YAMATO_B2_DEBUG_DIR / f"{timestamp}_{safe_label}.html"
    await _mask_debug_sensitive_fields(page)
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True, timeout=60000)
    except Exception:
        screenshot_path = None
    html_path.write_text(await page.content(), encoding="utf-8")
    return screenshot_path, html_path


async def _mask_debug_sensitive_fields(page) -> None:
    try:
        await page.evaluate(
            """
            () => {
              const selectors = [
                "input[name='username']",
                "input#code1",
                "input[name='CSTMR_PSWD']",
                "input#password",
                "input[name='KOJIN']",
                "input#kojin"
              ];
              for (const selector of selectors) {
                for (const input of document.querySelectorAll(selector)) {
                  input.value = input.type === "password" ? "********" : "";
                }
              }
            }
            """
        )
    except Exception:
        return


def _result(
    *,
    step: str,
    csv_file: Path | None,
    source_rows: int,
    source_headers: tuple[str, ...],
    ready_to_import: bool,
    browser_executed: bool,
    import_executed: bool,
    file_selected: bool,
    skipped_reason: str | None,
    warnings: list[str],
    page_title: str | None = None,
    page_url: str | None = None,
    page_excerpt: str | None = None,
    screenshot_path: Path | None = None,
    html_path: Path | None = None,
) -> YamatoB2ImportResult:
    return YamatoB2ImportResult(
        step=step,
        csv_file=csv_file,
        source_rows=source_rows,
        source_headers=source_headers,
        ready_to_import=ready_to_import,
        browser_executed=browser_executed,
        import_executed=import_executed,
        file_selected=file_selected,
        skipped_reason=skipped_reason,
        warnings=tuple(warnings),
        page_title=page_title,
        page_url=page_url,
        page_excerpt=page_excerpt,
        screenshot_path=screenshot_path,
        html_path=html_path,
        audit_path=YAMATO_B2_IMPORT_AUDIT_LOG_PATH,
    )


def _append_import_audit(result: YamatoB2ImportResult) -> None:
    YAMATO_B2_IMPORT_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "yamato_b2_import",
        "result": _json_safe(asdict(result)),
    }
    with YAMATO_B2_IMPORT_AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _sanitize_url(url: str | None) -> str | None:
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _sanitize_excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"(お客様コード[:：]?\s*)[0-9-]+", r"\1<redacted>", text)


def _yamato_b2_url() -> str:
    configured = os.environ.get(URL_ENV, "").strip()
    if configured and not _is_b2_system_error_url(configured):
        return configured
    return DEFAULT_YAMATO_B2_URL


def _storage_state_path() -> Path:
    configured = os.environ.get(STORAGE_STATE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_STORAGE_STATE_PATH


def _yamato_b2_headless_default() -> bool:
    raw = os.environ.get(HEADLESS_ENV, os.environ.get("NEXT_ENGINE_HEADLESS", "true"))
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def resolve_default_b2_csv() -> Path | None:
    """最新のB2取込CSVパスを返す（無ければ None）。UI表示・受け渡し用。"""
    try:
        return _resolve_csv_file(None)
    except Exception:
        return None


async def run_b2_import_over_cdp(
    cdp_endpoint: str,
    *,
    csv_file: Path | None,
    execute_import: bool,
    confirm_import: bool,
) -> dict:
    """CDP接続した実Chrome上でB2取込ステップを試行する（モードB）。

    - ブラウザは閉じない（playwright.stop() で切断のみ＝実Chromeは印刷用に開いたまま）。
      connect_over_cdp した実Chromeは Playwright の所有ではないため browser.close() は呼ばない
      （呼ぶとユーザーのタブ/コンテキストを巻き込む恐れがあるため）。
    - B2縮退（[[portal-tool-yamato-b2]]）や失敗時も例外を投げず warnings に記録して返す
      （＝手動フォールバック。呼び出し元は開いたままのChromeで手動取込・印刷できる）。
    """
    warnings: list[str] = []
    selected_csv = _resolve_csv_file(csv_file)
    rows, headers, csv_warnings = _read_b2_csv(selected_csv)
    warnings.extend(csv_warnings)
    if not _csv_ready(headers=headers, rows=len(rows), warnings=warnings):
        return {
            "ok": False,
            "import_executed": False,
            "file_selected": False,
            "ready_to_import": False,
            "skipped_reason": "invalid_csv",
            "warnings": warnings,
            "csv_file": str(selected_csv),
            "execute": False,
        }

    login_id = os.environ.get(LOGIN_ID_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "").strip()
    execute = bool(execute_import and confirm_import)

    import_executed = False
    file_selected = False
    ready_to_import = False
    skipped_reason: str | None = None

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
        # 既存の実Chromeのコンテキスト/タブを再利用する（閉じないため browser.close() は使わない）。
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        await _install_b2_page_workarounds(context)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            if login_id and password:
                await _login_to_b2(page, login_id=login_id, password=password, warnings=warnings)
            page, ready_to_import = await _open_import_page(
                page, login_id=login_id, password=password, warnings=warnings
            )
            if not ready_to_import:
                skipped_reason = "import_page_not_confirmed"
            elif not await _select_next_engine_import_pattern(page, warnings=warnings):
                skipped_reason = "import_pattern_not_selected"
            else:
                file_selected = await _select_csv_file(page, selected_csv, warnings=warnings)
                if not file_selected:
                    skipped_reason = "file_input_not_found"
                elif execute:
                    import_executed = await _execute_import_clicks(page, warnings=warnings)
                    if not import_executed:
                        skipped_reason = "import_completion_not_confirmed"
        except Exception as exc:
            skipped_reason = f"browser_error:{type(exc).__name__}"
            warnings.append(str(exc))
    finally:
        # 実Chromeは閉じない。driver を止めてCDP接続だけ切る（印刷まで開いたまま）。
        try:
            await playwright.stop()
        except Exception:
            pass

    return {
        "ok": import_executed if execute else file_selected,
        "import_executed": import_executed,
        "file_selected": file_selected,
        "ready_to_import": ready_to_import,
        "skipped_reason": skipped_reason,
        "warnings": warnings,
        "csv_file": str(selected_csv),
        "execute": execute,
    }


def run_b2_import_over_cdp_sync(
    cdp_endpoint: str,
    *,
    csv_file: Path | None,
    execute_import: bool,
    confirm_import: bool,
) -> dict:
    return asyncio.run(
        run_b2_import_over_cdp(
            cdp_endpoint,
            csv_file=csv_file,
            execute_import=execute_import,
            confirm_import=confirm_import,
        )
    )
