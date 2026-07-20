from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote

from portal_app.env import load_env_file

load_env_file()

from portal_app.log_paths import get_portal_logger, setup_file_logging

# 実行ログ・エラーログの出力先（既定: SharePoint 同期フォルダの
# 神里\くりまポータルエラーログ。KURIMA_LOG_DIR で上書き可、無ければ logs/ に fallback）。
RUNTIME_LOG_DIR = setup_file_logging()
portal_logger = get_portal_logger()

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from portal_app.services.access_analytics import (
    download_rakuten_device_access_sync,
    download_yahoo_access_reports_sync,
    latest_artifact_paths as latest_access_analytics_artifact_paths,
    read_access_analytics_manifest,
    read_access_analytics_preview,
    record_access_analytics_batch,
    resolve_artifact_path as resolve_access_analytics_artifact_path,
)
from portal_app.services.billing_statements import (
    download_billpay_settlement_sync,
    download_yahoo_statements_sync,
    latest_artifact_paths as latest_billing_artifact_paths,
    read_billing_manifest,
    read_billing_preview,
    resolve_artifact_path as resolve_billing_artifact_path,
)
from portal_app.services.clickpost import (
    create_clickpost_csv,
    find_clickpost_paths,
    import_pay_print_clickpost_csv_sync,
    prepare_clickpost_sync,
    preview_clickpost_csv,
    upload_clickpost_csv_sync,
)
from portal_app.services.inventory import analyze_latest_inventory, result_to_csv
from portal_app.services.inventory_pdf import inventory_result_to_pdf, takaesu_order_sheet_to_pdf
from portal_app.services.letterpack_pdf import create_letterpack_label_pdf
from portal_app.services.letterpack_tracking import write_letterpack_csv
from portal_app.services.ne02_order_details import download_ne02_order_details_sync
from portal_app.services.next_engine_downloader import (
    download_next_engine_order_details,
    download_next_engine_order_details_sync,
)
from portal_app.services.next_engine_order_status import restore_next_engine_print_wait_batch_sync
from portal_app.log_paths import error_log_file_name, run_log_file_name
from portal_app.services.paths import candidate_portal_roots, find_portal_paths, latest_order_csv
from portal_app.services.progress_jobs import (
    DuplicateJobError,
    JobCancelled,
    progress_jobs,
    read_job_history,
)
from portal_app.settings import (
    allowed_client_rules,
    client_allowed,
    download_timeout_ms,
    nav_timeout_ms,
    settings_snapshot,
)
from portal_app.services.shipment_confirmation import (
    confirm_next_engine_shipment_sync,
    create_shipment_slip_import_csv,
    download_yamato_tracking_export_sync,
    preview_next_engine_shipment_upload,
    preview_shipment_slip_import,
    shipment_lookback_defaults,
    upload_next_engine_shipment_csv_sync,
    write_shipment_confirmation_rows,
)
from portal_app.services.takaesu_orders import (
    create_takaesu_order_sheet_csv,
    default_takaesu_order_sheet_csv_path,
    download_takaesu_order_details_sync,
    prepare_takaesu_order_workflow_sync,
)
from portal_app.services import b2_chrome
from portal_app.services.yamato_b2_import import (
    import_yamato_b2_csv,
    import_yamato_b2_csv_sync,
    resolve_default_b2_csv,
    run_b2_import_over_cdp_sync,
)
from portal_app.services.yamato_b2_workflow import prepare_yamato_b2_sync
from portal_app.services.yamato_flow_profile import (
    NEKOPOS_PROFILE,
    YAMATO_PROFILE,
    YamatoFlowProfile,
    profile_for_mode,
)
from portal_app.services.yamato_conversion import (
    create_ne_to_yamato_csv,
    preview_ne_to_yamato_conversion,
)

APP_DIR = Path(__file__).resolve().parent
LOGS_ROOT = APP_DIR.parent / "logs"
LOG_VIEW_MAX_BYTES = 200_000


def _git_short_head() -> str:
    """ディスク上リポジトリの短縮コミットSHAを返す（git 不在・非リポジトリなら "unknown"）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=APP_DIR.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


# 稼働バージョンの可視化（S4）: uvicorn は --reload なしだと .py 変更を自動反映しないため、
# 「いま動いているコード」の commit と起動時刻をプロセス起動時に 1 回だけ確定して保持する。
# /health とフッターに出し、ディスク上の HEAD との乖離＝再起動忘れを検知できるようにする。
APP_VERSION = _git_short_head()
APP_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M")
LOG_VIEW_TEXT_SUFFIXES = {
    ".log",
    ".txt",
    ".jsonl",
    ".json",
    ".err",
    ".out",
    ".csv",
    ".md",
    ".html",
    ".yaml",
    ".yml",
}

app = FastAPI(title="くりまポータルツール")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
# 全画面共通のフッター表示用（S4）。リクエストによらず不変のため Jinja グローバルで配る。
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["app_started_at"] = APP_STARTED_AT
INVENTORY_TABS = {"normal", "takaesu"}

# アプリ読込（＝サーバー起動）を実行ログへ記録する。
portal_logger.info(
    "ポータル起動: app=くりまポータルツール version=%s log_dir=%s", APP_VERSION, RUNTIME_LOG_DIR
)

# logs/ の保持期間クリーンアップ（S6）。起動をブロックしないバックグラウンド実行で、
# ここが失敗してもサーバー本体は起動を継続する（フェイルセーフ）。
try:
    from portal_app.services.log_retention import start_background_cleanup

    start_background_cleanup()
except Exception:
    portal_logger.error("ログクリーンアップの開始に失敗しました", exc_info=True)


@app.middleware("http")
async def _restrict_clients(request: Request, call_next):
    """LAN 公開時の簡易アクセス制御（O3）。

    `KURIMA_ALLOWED_CLIENTS`（カンマ区切りの IP / CIDR / プレフィックス）に一致しない
    接続元を 403 で拒否する。**未設定なら無制限＝現行互換**。ループバック
    （127.0.0.1 / ::1）は常に許可し、ホストPC自身が設定ミスで締め出されないようにする。
    実決済（クリックポスト）・実取込（ヤマトB2）ボタンを LAN 上の任意の端末へ
    露出させないための防壁で、判定は接続ごとに env を読む（再起動不要ではないが
    .env 変更＋再起動のみで反映される）。
    """
    client_host = request.client.host if request.client else None
    if not client_allowed(client_host):
        portal_logger.warning(
            "アクセス拒否(403): %s %s from %s (KURIMA_ALLOWED_CLIENTS=%s)",
            request.method,
            request.url.path,
            client_host,
            ",".join(allowed_client_rules()),
        )
        return PlainTextResponse(
            "このパソコンからの利用は許可されていません。"
            "利用する場合は、ホストPCの .env の KURIMA_ALLOWED_CLIENTS に"
            f"このパソコンのIPアドレス（{client_host}）を追加して再起動してください。",
            status_code=403,
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def _log_unhandled_exception(request: Request, exc: Exception) -> PlainTextResponse:
    """未処理例外を traceback 付きでエラーログ（portal-error.log）へ記録する。"""
    portal_logger.error(
        "未処理の例外: %s %s -> %s",
        request.method,
        request.url.path,
        exc,
        exc_info=exc,
    )
    return PlainTextResponse("Internal Server Error", status_code=500)


@app.exception_handler(DuplicateJobError)
async def _duplicate_job_conflict(request: Request, exc: DuplicateJobError) -> PlainTextResponse:
    """二重実行ガード（S3）: 実行中の同一 workflow があるときの /start を 409 で拒否する。

    各画面の進捗JSは response.ok でないときレスポンス本文をそのまま表示するため、
    日本語メッセージを text/plain で返すだけで画面に対処文が出る（フロント側の変更不要）。
    """
    portal_logger.info(
        "二重実行を拒否: %s %s workflow=%s 実行中job=%s",
        request.method,
        request.url.path,
        exc.workflow,
        exc.existing_job_id,
    )
    return PlainTextResponse(str(exc), status_code=409)


def _parse_order_numbers(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return tuple()
    return tuple(value for value in raw.replace(",", " ").split() if value)


async def _read_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


async def _read_form_values(request: Request) -> dict[str, list[str]]:
    """urlencodedフォームの同名キーを捨てずに全値返す。"""

    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: list(values) for key, values in parsed.items()}


def _form_bool(form: dict[str, str], key: str) -> bool:
    return form.get(key) in {"1", "true", "on", "yes"}


def _form_headed(form: dict[str, str]) -> bool:
    """ブラウザ表示指定の後方互換読み取り。

    契約入力 headed（checkbox、未チェック時は送信されない）を正とし、
    browser_mode=front（他ページで使う select 方式）も互換で受け付ける。
    どちらも無ければ従来どおり False（バックグラウンド実行）。
    """
    return _form_bool(form, "headed") or form.get("browser_mode") == "front"


def _form_int(form: dict[str, str], key: str, default: int) -> int:
    try:
        return int(form.get(key, "") or default)
    except ValueError:
        return default


def _inventory_tab(request: Request) -> str:
    tab = request.query_params.get("tab", "normal")
    return tab if tab in INVENTORY_TABS else "normal"


def _inventory_response(
    request: Request,
    *,
    active_tab: str = "normal",
    result=None,
    download_result=None,
    takaesu_result=None,
    browser_mode: str = "background",
    slow_mo_ms: int = 150,
    takaesu_browser_mode: str = "background",
    takaesu_slow_mo_ms: int = 150,
    takaesu_download_ready: bool = False,
    defer_preview: bool = False,
    error: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "active_tab": active_tab,
            "defer_preview": defer_preview,
            "result": result,
            "download_result": download_result,
            "takaesu_result": takaesu_result,
            "takaesu_sheet": takaesu_result.order_sheet if takaesu_result else None,
            "takaesu_download_result": takaesu_result.download if takaesu_result else None,
            "takaesu_download_ready": takaesu_download_ready,
            "browser_mode": browser_mode,
            "slow_mo_ms": slow_mo_ms,
            "takaesu_browser_mode": takaesu_browser_mode,
            "takaesu_slow_mo_ms": takaesu_slow_mo_ms,
            "error": error,
        },
        status_code=status_code,
    )


def _yamato_selected_steps(form: dict[str, str]) -> dict[str, bool]:
    next_engine_selected = (
        _form_bool(form, "next_engine_workflow")
        or _form_bool(form, "ne_order_download")
        or _form_bool(form, "ne_invoice_download")
        or _form_bool(form, "ne_custom_data_import")
    )
    conversion_selected = (
        _form_bool(form, "shipping_conversion_workflow")
        or _form_bool(form, "address_fix")
        or _form_bool(form, "csv_create")
    )
    yamato_selected = (
        _form_bool(form, "yamato_import_workflow")
        or _form_bool(form, "b2_login")
        or _form_bool(form, "b2_import")
    )
    selected = {
        "ne_order_download": next_engine_selected,
        "ne_invoice_download": next_engine_selected,
        "ne_custom_data_import": next_engine_selected,
        "address_fix": conversion_selected,
        "csv_create": conversion_selected,
        "b2_login": yamato_selected,
        "b2_import": yamato_selected,
    }
    if _form_bool(form, "run_all") or form.get("preset") == "all":
        return {key: True for key in selected}
    return selected


def _yamato_restore_order_numbers_from_prepare_result(prepare_result) -> tuple[str, ...]:
    invoice = getattr(prepare_result, "invoice", None)
    before_list = getattr(invoice, "before_list", None)
    return tuple(getattr(before_list, "order_numbers", tuple()) or tuple())


def _yamato_printed_orders_notice(prepare_result, restore_result) -> str:
    """処理が途中で止まったとき、「印刷済み」へ進んだ伝票番号を利用者へ必ず明示する文。

    テストモードの復旧が完了済み（restore_result あり）の場合は、既に戻っているため何も出さない。
    """
    if prepare_result is None or restore_result is not None:
        return ""
    invoice = getattr(prepare_result, "invoice", None)
    if invoice is None or not invoice.executed or not invoice.downloaded_file:
        return ""
    orders = _yamato_restore_order_numbers_from_prepare_result(prepare_result)
    if not orders:
        return ""
    return (
        f"※納品書印刷で「印刷済み」へ変更済みの伝票: {', '.join(orders)}"
        "（『その他の操作・納品書印刷待ちへ復旧』で戻せます）"
    )


def _path_text(value) -> str | None:
    return str(value) if value else None


def _clickpost_conversion_summary(result) -> dict[str, object]:
    return {
        "buyer_csv": _path_text(getattr(result, "buyer_csv", None)),
        "product_csv": _path_text(getattr(result, "product_csv", None)),
        "output_csv": _path_text(getattr(result, "output_csv", None)),
        "target_rows": getattr(result, "target_rows", 0),
        "output_rows": getattr(result, "output_rows", 0),
        "warnings": list(getattr(result, "warnings", tuple()) or tuple()),
    }


def _clickpost_upload_summary(result) -> dict[str, object]:
    return {
        "csv_file": _path_text(getattr(result, "csv_file", None)),
        "target_rows": getattr(result, "target_rows", 0),
        "executed": getattr(result, "executed", False),
        "ready_for_payment": getattr(result, "ready_for_payment", False),
        "skipped_reason": getattr(result, "skipped_reason", None),
        "warning_text": getattr(result, "warning_text", None),
    }


def _clickpost_prepare_summary(result) -> dict[str, object]:
    buyer = getattr(result, "buyer", None)
    product = getattr(result, "product", None)
    invoice = getattr(result, "invoice", None)
    conversion = getattr(result, "conversion", None)
    letterpack = getattr(result, "letterpack", None)
    return {
        "buyer_file": _path_text(getattr(buyer, "downloaded_file", None)),
        "buyer_count": getattr(getattr(buyer, "snapshot", None), "count", None),
        "product_file": _path_text(getattr(product, "downloaded_file", None)),
        "product_count": getattr(getattr(product, "snapshot", None), "count", None),
        "invoice_file": _path_text(getattr(invoice, "downloaded_file", None)),
        "invoice_count": getattr(getattr(invoice, "before_list", None), "count", None),
        "invoice_restored": getattr(invoice, "restored", False),
        "clickpost_csv": _path_text(getattr(conversion, "output_csv", None)),
        "clickpost_rows": getattr(conversion, "output_rows", 0) if conversion else 0,
        "letterpack_csv": _path_text(getattr(letterpack, "output_csv", None)),
        "letterpack_rows": getattr(letterpack, "output_rows", 0) if letterpack else 0,
        "warnings": list(getattr(result, "consistency_warnings", tuple()) or tuple()),
    }


def _clickpost_import_payment_print_summary(result) -> dict[str, object]:
    return {
        "csv_file": _path_text(getattr(result, "csv_file", None)),
        "target_rows": getattr(result, "target_rows", 0),
        "executed": getattr(result, "executed", False),
        "ready_for_payment": getattr(result, "ready_for_payment", False),
        "payment_attempts": getattr(result, "payment_attempts", 0),
        "payments_completed": getattr(result, "payments_completed", 0),
        "remaining_payment_buttons": getattr(result, "remaining_payment_buttons", 0),
        "print_target_rows": getattr(result, "print_target_rows", 0),
        "downloaded_pdf": _path_text(getattr(result, "downloaded_pdf", None)),
        "tracking_csv": _path_text(getattr(result, "tracking_csv", None)),
        "tracking_rows": getattr(result, "tracking_rows", 0),
        "workbook_path": _path_text(getattr(result, "workbook_path", None)),
        "workbook_updated": getattr(result, "workbook_updated", False),
        "download_dir": _path_text(getattr(result, "download_dir", None)),
        "skipped_reason": getattr(result, "skipped_reason", None),
        "warning_text": getattr(result, "warning_text", None),
    }


def _letterpack_pdf_download_url(result, *, disposition: str = "attachment") -> str | None:
    output_pdf = getattr(result, "output_pdf", None)
    if not output_pdf:
        return None
    safe_name = quote(Path(output_pdf).name)
    return f"/clickpost/download/letterpack-pdf/{safe_name}?disposition={disposition}"


def _letterpack_pdf_summary(result) -> dict[str, object]:
    return {
        "address_csv": _path_text(getattr(result, "address_csv", None)),
        "output_pdf": _path_text(getattr(result, "output_pdf", None)),
        "output_rows": getattr(result, "output_rows", 0),
        "page_count": getattr(result, "page_count", 0),
        "warnings": list(getattr(result, "warnings", tuple()) or tuple()),
        "audit_path": _path_text(getattr(result, "audit_path", None)),
        "download_url": _letterpack_pdf_download_url(result),
        "open_url": _letterpack_pdf_download_url(result, disposition="inline"),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    status: dict[str, object]
    # データ鮮度: 最新の受注明細CSVのファイル名と更新日時。
    # 「いま画面に出ている集計はいつ時点のデータか」を見えるようにする。
    # フォルダ未解決・CSVなしでも 500 にせず「未取得」表示にする。
    freshness: dict[str, object] = {"ok": False}
    try:
        paths = find_portal_paths()
        status = {
            "ok": True,
            "portal_root": paths.portal_root,
            "master_book": paths.master_book,
            "order_csv_dir": paths.order_csv_dir,
        }
        try:
            latest_csv = latest_order_csv(paths.order_csv_dir)
            updated_at = datetime.fromtimestamp(latest_csv.stat().st_mtime)
            freshness = {
                "ok": True,
                "name": latest_csv.name,
                "updated_at": updated_at.strftime("%Y-%m-%d %H:%M"),
            }
        except Exception:
            pass
    except Exception as exc:
        status = {
            "ok": False,
            "error": str(exc),
            "candidates": candidate_portal_roots(),
        }

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "status": status,
            "freshness": freshness,
        },
    )


@app.get("/price-calc", response_class=HTMLResponse)
def price_calc(request: Request):
    # 税込・割引価格計算（楽天RMS向け）。計算はすべてブラウザ内JS
    # （static/price_calc.js）で行うため、サーバー側は画面を返すのみ。
    return templates.TemplateResponse("price_calc.html", {"request": request})


@app.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request):
    # プレビュー（商品マスタ読込・集計を含む重い処理）はGET時に実行せず、
    # /inventory/preview から非同期で読み込む（操作ボタンを即表示するため）。
    active_tab = _inventory_tab(request)
    return _inventory_response(request, active_tab=active_tab, defer_preview=True)


@app.get("/inventory/preview", response_class=HTMLResponse)
def inventory_preview(request: Request):
    # GET /inventory から遅延ロードされる結果fragment。
    # 重いプレビュー（商品マスタ読込・集計）はここで実行する。
    active_tab = _inventory_tab(request)
    if active_tab == "takaesu":
        try:
            takaesu_result = prepare_takaesu_order_workflow_sync(
                dry_run=False,
                execute_download=False,
                write_order_sheet=False,
                preview_limit=50,
                write_audit=False,
            )
            return templates.TemplateResponse(
                "_takaesu_results.html",
                {
                    "request": request,
                    "takaesu_result": takaesu_result,
                    "takaesu_sheet": takaesu_result.order_sheet if takaesu_result else None,
                    "takaesu_download_result": takaesu_result.download if takaesu_result else None,
                    "takaesu_download_ready": default_takaesu_order_sheet_csv_path().is_file(),
                    "takaesu_browser_mode": "background",
                    "takaesu_slow_mo_ms": 150,
                    "preview_error": None,
                },
            )
        except Exception as exc:
            return templates.TemplateResponse(
                "_takaesu_results.html",
                {
                    "request": request,
                    "takaesu_result": None,
                    "takaesu_sheet": None,
                    "takaesu_download_result": None,
                    "takaesu_download_ready": False,
                    "takaesu_browser_mode": "background",
                    "takaesu_slow_mo_ms": 150,
                    "preview_error": str(exc),
                },
            )

    try:
        result = analyze_latest_inventory()
        return templates.TemplateResponse(
            "_inventory_results.html",
            {
                "request": request,
                "result": result,
                "download_result": None,
                "browser_mode": "background",
                "slow_mo_ms": 150,
                "preview_error": None,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "_inventory_results.html",
            {
                "request": request,
                "result": None,
                "download_result": None,
                "browser_mode": "background",
                "slow_mo_ms": 150,
                "preview_error": str(exc),
            },
        )


@app.post("/inventory/fetch-next-engine", response_class=HTMLResponse)
async def inventory_fetch_next_engine(request: Request):
    form = await _read_form(request)
    browser_mode = form.get("browser_mode", "background")
    if browser_mode not in {"background", "front"}:
        browser_mode = "background"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0
    try:
        download_result = await download_next_engine_order_details(
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
        )
        result = analyze_latest_inventory()
        return _inventory_response(
            request,
            active_tab="normal",
            result=result,
            download_result=download_result,
            browser_mode=browser_mode,
            slow_mo_ms=slow_mo_ms if headed else 150,
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = analyze_latest_inventory()
        except Exception:
            pass
        return _inventory_response(
            request,
            active_tab="normal",
            result=current_result,
            browser_mode=browser_mode,
            slow_mo_ms=slow_mo_ms if headed else 150,
            error=str(exc),
            status_code=500,
        )


@app.get("/inventory/fetch-next-engine")
def inventory_fetch_next_engine_get():
    return RedirectResponse(url="/inventory", status_code=303)


@app.post("/inventory/fetch-next-engine/start")
async def inventory_fetch_next_engine_start(request: Request):
    # 通常タブの非同期版。進捗をフローチップに同期表示する。
    # ステップ: fetch(NE取得=チップ①) / aggregate(マスタ照合・集計=チップ②③)。
    form = await _read_form(request)
    browser_mode = form.get("browser_mode", "background")
    if browser_mode not in {"background", "front"}:
        browser_mode = "background"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0

    def worker(job_id: str) -> None:
        progress_jobs.update_step(job_id, "fetch", status="running")
        try:
            download_next_engine_order_details_sync(headless=not headed, slow_mo_ms=slow_mo_ms)
        except Exception:
            progress_jobs.update_step(job_id, "fetch", status="failed")
            raise
        progress_jobs.update_step(job_id, "fetch", status="completed")

        progress_jobs.update_step(job_id, "aggregate", status="running")
        try:
            result = analyze_latest_inventory()
        except Exception:
            progress_jobs.update_step(job_id, "aggregate", status="failed")
            raise
        progress_jobs.update_step(job_id, "aggregate", status="completed")

        progress_jobs.finish(
            job_id,
            message="集計が完了しました。",
            result={
                "source_rows": getattr(result, "source_rows", None),
                "normal_count": getattr(result, "normal_count", None),
                "choice_count": getattr(result, "choice_count", None),
            },
        )

    job_id = progress_jobs.start(
        title="在庫明細 集計",
        steps=[("fetch", "Next Engineから取得"), ("aggregate", "商品マスタ照合・集計")],
        worker=worker,
        workflow="inventory_fetch_next_engine",
        metadata={"browser_mode": browser_mode, "slow_mo_ms": slow_mo_ms},
    )
    return {"job_id": job_id}


@app.post("/inventory/takaesu/prepare", response_class=HTMLResponse)
async def inventory_takaesu_prepare(request: Request):
    form = await _read_form(request)
    browser_mode = form.get("browser_mode", "background")
    if browser_mode not in {"background", "front"}:
        browser_mode = "background"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0

    try:
        takaesu_result = await run_in_threadpool(
            prepare_takaesu_order_workflow_sync,
            dry_run=False,
            execute_download=True,
            write_order_sheet=True,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=50,
        )
        return _inventory_response(
            request,
            active_tab="takaesu",
            takaesu_result=takaesu_result,
            takaesu_browser_mode=browser_mode,
            takaesu_slow_mo_ms=slow_mo_ms if headed else 150,
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = await run_in_threadpool(
                prepare_takaesu_order_workflow_sync,
                dry_run=False,
                execute_download=False,
                write_order_sheet=False,
                preview_limit=50,
                write_audit=False,
            )
        except Exception:
            pass
        return _inventory_response(
            request,
            active_tab="takaesu",
            takaesu_result=current_result,
            takaesu_browser_mode=browser_mode,
            takaesu_slow_mo_ms=slow_mo_ms if headed else 150,
            error=str(exc),
            status_code=500,
        )


@app.get("/inventory/takaesu/prepare")
def inventory_takaesu_prepare_get():
    return RedirectResponse(url="/inventory?tab=takaesu", status_code=303)


@app.post("/inventory/takaesu/prepare/start")
async def inventory_takaesu_prepare_start(request: Request):
    # 通常タブと進捗を揃えるため、取得(download)と発注書作成(create)を分離した2ステップにする。
    # ステップ: fetch(NE取得=チップ①) / aggregate(マスタ照合・発注書作成=チップ②③)。
    form = await _read_form(request)
    browser_mode = form.get("browser_mode", "background")
    if browser_mode not in {"background", "front"}:
        browser_mode = "background"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0

    def worker(job_id: str) -> None:
        progress_jobs.update_step(job_id, "fetch", status="running")
        try:
            download = download_takaesu_order_details_sync(
                execute=True,
                headless=not headed,
                slow_mo_ms=slow_mo_ms,
                write_audit=False,
            )
        except Exception:
            progress_jobs.update_step(job_id, "fetch", status="failed")
            raise
        progress_jobs.update_step(job_id, "fetch", status="completed")

        progress_jobs.update_step(job_id, "aggregate", status="running")
        try:
            create_takaesu_order_sheet_csv(
                source_csv=download.downloaded_file,
                output_csv=default_takaesu_order_sheet_csv_path(),
                preview_limit=50,
                write_audit=True,
            )
        except Exception:
            progress_jobs.update_step(job_id, "aggregate", status="failed")
            raise
        progress_jobs.update_step(job_id, "aggregate", status="completed")
        progress_jobs.finish(job_id, message="高江洲発注書を作成しました。", result={})

    job_id = progress_jobs.start(
        title="高江洲発注書 作成",
        steps=[("fetch", "Next Engineから取得"), ("aggregate", "商品マスタ照合・集計")],
        worker=worker,
        workflow="inventory_takaesu_prepare",
        metadata={"browser_mode": browser_mode, "slow_mo_ms": slow_mo_ms},
    )
    return {"job_id": job_id}


@app.get("/inventory/takaesu/download")
def inventory_takaesu_download():
    path = default_takaesu_order_sheet_csv_path()
    if not path.is_file():
        return PlainTextResponse("高江洲発注書CSVがまだ作成されていません。", status_code=404)
    return FileResponse(
        path,
        media_type="text/csv; charset=shift_jis",
        filename=path.name,
    )


@app.get("/inventory/takaesu/download/pdf")
def inventory_takaesu_download_pdf():
    path = default_takaesu_order_sheet_csv_path()
    if not path.is_file():
        return PlainTextResponse("高江洲発注書がまだ作成されていません。", status_code=404)
    # CSVダウンロードと同一内容を保証するため、保存済み発注書CSV(cp932)をそのまま読んでPDF化する
    # （最新ソースからの再集計はしない＝CSVとPDFが食い違わない・ソース/マスタ依存で500にならない）。
    try:
        with path.open("r", encoding="cp932", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:
        return PlainTextResponse(f"発注書の読み込みに失敗しました: {exc}", status_code=500)
    pdf_bytes = takaesu_order_sheet_to_pdf(rows)
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="takaesu_order_sheet.pdf"'},
    )


@app.get("/inventory/download/pdf")
def inventory_download_pdf():
    result = analyze_latest_inventory()
    pdf_bytes = inventory_result_to_pdf(result)
    filename = f"inventory_detail_{result.generated_at:%Y%m%d_%H%M%S}.pdf"
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/inventory/download/pdf/{kind}")
def inventory_download_pdf_kind(kind: str):
    # combined = 通常商品とセット内訳をJANで対応付けた数量合算表（依頼2）
    if kind not in {"normal", "choice", "combined"}:
        return PlainTextResponse("kind must be normal, choice or combined", status_code=400)

    result = analyze_latest_inventory()
    pdf_bytes = inventory_result_to_pdf(result, kind)  # type: ignore[arg-type]
    filename = f"inventory_{kind}_{result.generated_at:%Y%m%d_%H%M%S}.pdf"
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/inventory/download/{kind}")
def inventory_download(kind: str):
    if kind not in {"normal", "choice"}:
        return PlainTextResponse("kind must be normal or choice", status_code=400)

    result = analyze_latest_inventory()
    csv_text = result_to_csv(result, kind)  # type: ignore[arg-type]
    filename = "inventory_normal.csv" if kind == "normal" else "inventory_choice.csv"
    return Response(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/yamato", response_class=HTMLResponse)
def yamato_delivery(request: Request):
    # プレビュー（商品マスタ読込を含む重い処理）はGET時に実行せず、
    # /yamato/preview から非同期で読み込む（操作ボタンを即表示するため）。
    return _yamato_response(request, result=None, defer_preview=True)


@app.get("/nekopos", response_class=HTMLResponse)
def nekopos_delivery(request: Request):
    # 依頼5: ネコポスカード。ヤマト伝票と同じ画面・同じエンドポイントを
    # mode=nekopos（NEKOPOS_PROFILE）で共用する。
    return _yamato_response(request, result=None, defer_preview=True, profile=NEKOPOS_PROFILE)


def _yamato_response(
    request: Request,
    *,
    result,
    message: str | None = None,
    error: str | None = None,
    prepare_result=None,
    restore_result=None,
    b2_import_result=None,
    defer_preview: bool = False,
    status_code: int = 200,
    profile: YamatoFlowProfile = YAMATO_PROFILE,
):
    return templates.TemplateResponse(
        "yamato.html",
        {
            "request": request,
            "result": result,
            "prepare_result": prepare_result,
            "restore_result": restore_result,
            "b2_import_result": b2_import_result,
            "message": message,
            "error": error,
            "defer_preview": defer_preview,
            "b2_browser": b2_chrome.status(),
            "b2_csv_path": _b2_csv_path_text(profile),
            "restore_order_nos_text": "\n".join(
                _yamato_restore_order_numbers_from_prepare_result(prepare_result)
            ),
            "mode": profile.key,
            "mode_label": profile.label,
        },
        status_code=status_code,
    )


def _b2_csv_path_text(profile: YamatoFlowProfile = YAMATO_PROFILE) -> str | None:
    csv_path = resolve_default_b2_csv(profile.output_prefix)
    return str(csv_path) if csv_path else None


@app.get("/yamato/preview", response_class=HTMLResponse)
def yamato_preview(request: Request):
    # GET /yamato・/nekopos から遅延ロードされる結果fragment。
    # 重いプレビュー（商品マスタ読込）はここで実行する。mode で対象CSVを切り替える。
    profile = profile_for_mode(request.query_params.get("mode"))
    try:
        result = preview_ne_to_yamato_conversion(preview_limit=30, profile=profile)
        return templates.TemplateResponse(
            "_yamato_results.html",
            {
                "request": request,
                "result": result,
                "message": None,
                "prepare_result": None,
                "restore_result": None,
                "b2_import_result": None,
                "preview_error": None,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "_yamato_results.html",
            {
                "request": request,
                "result": None,
                "message": None,
                "prepare_result": None,
                "restore_result": None,
                "b2_import_result": None,
                "preview_error": str(exc),
            },
        )


@app.get("/clickpost", response_class=HTMLResponse)
def clickpost_delivery(request: Request):
    # プレビュー（商品マスタ読込を含む重い処理）はGET時に実行せず、
    # /clickpost/preview から非同期で読み込む（操作ボタンを即表示するため）。
    return templates.TemplateResponse(
        "clickpost.html",
        {
            "request": request,
            "result": None,
            "prepare_result": None,
            "upload_result": None,
            "letterpack_pdf_result": None,
            "browser_mode": "background",
            "slow_mo_ms": 150,
            "message": None,
            "error": None,
            "defer_preview": True,
        },
    )


@app.get("/clickpost/preview", response_class=HTMLResponse)
def clickpost_preview(request: Request):
    # GET /clickpost から遅延ロードされる結果fragment。
    # 重いプレビュー（商品マスタ読込）はここで実行する。
    try:
        result = preview_clickpost_csv(preview_limit=30)
        return templates.TemplateResponse(
            "_clickpost_results.html",
            {
                "request": request,
                "result": result,
                "message": None,
                "preview_error": None,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "_clickpost_results.html",
            {
                "request": request,
                "result": None,
                "message": None,
                "preview_error": str(exc),
            },
        )


@app.get("/progress/active")
def progress_active():
    # 実行中（queued/running）ジョブの一覧（U4）。各画面のロード時の自動再アタッチと、
    # ナビ「実行履歴」の実行中バッジが使う軽量API（メモリ内走査のみ）。
    # 注意: /progress/{job_id} より先に登録すること（後だと job_id="active" に飲まれる）。
    jobs = progress_jobs.list_active()
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/progress/{job_id}")
def progress_status(job_id: str):
    snapshot = progress_jobs.snapshot(job_id)
    if snapshot is None:
        return Response(status_code=404)
    return snapshot


def _force_stop_job_browsers() -> None:
    """実行中ジョブのブラウザを強制終了して自動操作を即時停止する。

    - Playwright が起動したブラウザ（ms-playwright 配下の chrome.exe / headless_shell.exe）のみを
      パスで絞って kill する。ユーザーが個人的に開いている Google Chrome（Program Files 配下）は殺さない。
    - B2印刷/取込用の実Chrome（b2_chrome, 追跡PIDで管理）は close() で終了する。
    ジョブは通常1件ずつ逐次実行のため、実行中ジョブのブラウザのみが対象になる。
    """
    try:
        b2_chrome.close()
    except Exception:
        pass
    if sys.platform != "win32":
        return
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -like '*ms-playwright*' -and "
        "($_.Name -eq 'chrome.exe' -or $_.Name -eq 'headless_shell.exe') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


@app.post("/progress/{job_id}/cancel")
async def progress_cancel(job_id: str):
    # ①中止フラグを立て（実行中工程を記録）② 実行中ブラウザを強制終了して即時停止する。
    where = progress_jobs.request_cancel(job_id)
    await run_in_threadpool(_force_stop_job_browsers)
    return {"cancelling": True, "stopped_at": where}


@app.get("/clickpost/download/letterpack-pdf/{filename}")
def clickpost_download_letterpack_pdf(filename: str, disposition: str = "attachment"):
    safe_name = Path(filename).name
    paths = find_clickpost_paths()
    pdf_dir = (paths.completed_data_dir / "letterpack_label_pdfs").resolve()
    pdf_path = (pdf_dir / safe_name).resolve()
    if pdf_path.parent != pdf_dir or pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        return PlainTextResponse("レターパックPDFが見つかりません。", status_code=404)

    content_disposition_type = "inline" if disposition == "inline" else "attachment"
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=pdf_path.name,
        content_disposition_type=content_disposition_type,
    )


@app.get("/letterpack-tracking", response_class=HTMLResponse)
def letterpack_tracking_page(request: Request):
    # レターパック配送番号反映（依頼4）。スキャンとペア管理はブラウザ内JSで行い、
    # サーバーはCSV書き出し（/letterpack-tracking/create）だけを担当する。
    return templates.TemplateResponse("letterpack_tracking.html", {"request": request})


@app.post("/letterpack-tracking/create")
async def letterpack_tracking_create(request: Request):
    # ペア一覧（JSON文字列のフォーム値。フォームはurlencoded必須）を受け取り、
    # Excel互換の2列CSV（伝票番号,送り状番号・cp932）を「完成したデータ」へ書き出す。
    form = await _read_form(request)
    try:
        pairs = json.loads(form.get("pairs", "[]"))
        if not isinstance(pairs, list) or not all(isinstance(p, dict) for p in pairs):
            raise ValueError("ペア一覧の形式が不正です。")
        path = await run_in_threadpool(write_letterpack_csv, pairs)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "path": str(path), "rows": len(pairs)}


@app.get("/shipment-confirmation", response_class=HTMLResponse)
def shipment_confirmation_page(request: Request):
    # 出荷確定のWeb UI。マッピングや候補CSVの解決は重い＋実データ依存のため
    # GET では実行せず、画面のボタン（preview / upload-check / start系）から呼ぶ。
    return templates.TemplateResponse(
        "shipment_confirmation.html",
        {
            "request": request,
            "lookbacks": shipment_lookback_defaults(),
            "today": date.today().strftime("%Y/%m/%d"),
        },
    )


@app.get("/shipment-confirmation/status")
def shipment_confirmation_status():
    # 旧 GET /shipment-confirmation のJSONステータス（互換用に退避）。
    result = confirm_next_engine_shipment_sync(execute=False, write_audit=False)
    return {
        "flow_id": result.flow_id,
        "flow_name": result.flow_name,
        "executed": result.executed,
        "steps": [
            {
                "subflow": step.subflow,
                "target": step.target,
                "status": step.status,
            }
            for step in result.steps
        ],
        "side_effects": result.side_effects,
    }


def _shipment_lookback_arg(form: dict[str, str], key: str) -> int | None:
    value = _form_int(form, key, 0)
    return value if value > 0 else None


@app.post("/shipment-confirmation/preview")
async def shipment_confirmation_preview(request: Request):
    # スキャン済みバーコードと購入者・配送データのマッピング（同期・状態変更なし）。
    form = await _read_form(request)
    codes = _parse_order_numbers(form.get("scanned_codes"))
    preview_limit = max(1, min(_form_int(form, "preview_limit", 500), 1000))
    result = await run_in_threadpool(
        preview_shipment_slip_import,
        order_numbers=codes,
        preview_limit=preview_limit,
        buyer_lookback_days=_shipment_lookback_arg(form, "buyer_lookback_days"),
        clickpost_lookback_days=_shipment_lookback_arg(form, "clickpost_lookback_days"),
        letterpack_lookback_days=_shipment_lookback_arg(form, "letterpack_lookback_days"),
        yamato_lookback_days=_shipment_lookback_arg(form, "yamato_lookback_days"),
    )
    return {
        "rows": [dict(row) for row in result.preview_rows],
        "counts": {
            "scanned": result.scanned_count,
            "duplicates": result.duplicate_count,
            "buyer_matched": result.buyer_matched_count,
            "tracking_matched": result.tracking_matched_count,
            "unresolved": result.unresolved_count,
        },
        # 店舗ごとの件数（出現順）。プレビューの「店舗別件数」表示に使う。
        "store_counts": [
            {"store": store, "count": count} for store, count in result.store_counts
        ],
        "buyer_rows": result.buyer_rows,
        "tracking_rows": result.tracking_rows,
        "source_files": len(result.source_files),
        "warnings": list(result.warnings),
    }


@app.post("/shipment-confirmation/create/start")
async def shipment_confirmation_create_start(request: Request):
    # 画面で確定した行（手動修正・追加込み）から3列の出荷確定CSVを作成するジョブ。
    form = await _read_form(request)
    rows_json = form.get("rows_json", "").strip()
    codes = _parse_order_numbers(form.get("scanned_codes"))
    rows: list[dict[str, str]] = []
    if rows_json:
        try:
            parsed = json.loads(rows_json)
        except json.JSONDecodeError:
            return PlainTextResponse("rows_json をJSONとして読み取れません。", status_code=400)
        if not isinstance(parsed, list):
            return PlainTextResponse("rows_json は行の配列で指定してください。", status_code=400)
        rows = [row for row in parsed if isinstance(row, dict)]
    if not rows and not codes:
        return PlainTextResponse(
            "出力する行がありません。バーコードをスキャンしてマッピングを実行してください。",
            status_code=400,
        )
    lookbacks = {
        "buyer_lookback_days": _shipment_lookback_arg(form, "buyer_lookback_days"),
        "clickpost_lookback_days": _shipment_lookback_arg(form, "clickpost_lookback_days"),
        "letterpack_lookback_days": _shipment_lookback_arg(form, "letterpack_lookback_days"),
        "yamato_lookback_days": _shipment_lookback_arg(form, "yamato_lookback_days"),
    }

    def worker(job_id: str) -> None:
        progress_jobs.update_step(job_id, "create_csv", status="running")
        if rows:
            result = write_shipment_confirmation_rows(rows, preview_limit=30)
        else:
            result = create_shipment_slip_import_csv(
                order_numbers=codes,
                preview_limit=30,
                **lookbacks,
            )
        if result.output_csv is None:
            progress_jobs.update_step(job_id, "create_csv", status="failed")
            progress_jobs.fail(
                job_id,
                "出荷確定CSVを作成できませんでした: " + " / ".join(result.warnings[:3]),
            )
            return
        progress_jobs.update_step(
            job_id,
            "create_csv",
            status="completed",
            detail=f"{result.output_rows}件を出力しました。",
        )
        progress_jobs.finish(
            job_id,
            message=f"出荷確定CSVを作成しました（{result.output_rows}件）。",
            result={
                "output_csv": _path_text(result.output_csv),
                "output_rows": result.output_rows,
                "unresolved": result.unresolved_count,
                "warnings": list(result.warnings)[:5],
            },
        )

    job_id = progress_jobs.start(
        title="出荷確定CSV作成",
        steps=[("create_csv", "出荷確定CSV作成")],
        worker=worker,
        workflow="shipment_confirmation_create",
        metadata={"rows": len(rows), "scanned": len(codes)},
    )
    return {"job_id": job_id}


@app.post("/shipment-confirmation/fetch-yamato/start")
async def shipment_confirmation_fetch_yamato_start(request: Request):
    # ヤマトB2の発行済データ取得。execute なしは dry-run（既存CSVの検証・候補表示のみ）。
    form = await _read_form(request)
    execute = _form_bool(form, "execute")
    target_date = form.get("target_date", "").strip() or None
    headed = _form_headed(form)
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)

    def worker(job_id: str) -> None:
        progress_jobs.update_step(job_id, "fetch", status="running")
        result = download_yamato_tracking_export_sync(
            execute=execute,
            target_date=target_date,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=20,
        )
        if execute and result.skipped_reason:
            progress_jobs.update_step(job_id, "fetch", status="failed", detail=result.skipped_reason)
            # B2LoginError 等の原因メッセージ（time_outside/needs_2fa 等）をそのまま利用者へ伝える。
            # B2側要因の切り分け情報が途中で切れないよう、状態名（B2LOGIN_STATE=）を先頭に
            # 最大4件まで表示する。
            state_notes = [w for w in result.warnings if w.startswith("B2LOGIN_STATE=")]
            other_notes = [w for w in result.warnings if not w.startswith("B2LOGIN_STATE=")]
            detail = " / ".join([*state_notes, *other_notes][:4])
            progress_jobs.fail(
                job_id,
                f"発行済データを取得できませんでした（{result.skipped_reason}）。"
                + (f" {detail}" if detail else ""),
            )
            return
        progress_jobs.update_step(
            job_id,
            "fetch",
            status="completed",
            detail=f"{result.source_rows}件（{result.target_date}）",
        )
        progress_jobs.finish(
            job_id,
            message=(
                "ヤマト発行済データを取得しました。"
                if execute
                else "ヤマト発行済データを確認しました（取得は行っていません）。"
            ),
            result={
                "executed": result.executed,
                "target_date": result.target_date,
                "export_csv": _path_text(result.export_csv),
                "source_rows": result.source_rows,
                "ready_to_import": result.ready_to_import,
                "warnings": list(result.warnings)[:5],
            },
        )

    job_id = progress_jobs.start(
        title="ヤマト発行済データ取得",
        steps=[("fetch", "発行済データ取得" if execute else "発行済データ確認（dry-run）")],
        worker=worker,
        workflow="shipment_confirmation_fetch_yamato",
        metadata={"execute": execute, "target_date": target_date, "headed": headed},
    )
    return {"job_id": job_id}


@app.post("/shipment-confirmation/upload-check")
async def shipment_confirmation_upload_check(request: Request):
    # NE反映のアップロード前チェック（dry-run・状態変更なし・同期）。
    form = await _read_form(request)
    csv_file = form.get("csv_file", "").strip()
    preview_limit = max(1, min(_form_int(form, "preview_limit", 30), 100))
    result = await run_in_threadpool(
        preview_next_engine_shipment_upload,
        upload_csv=Path(csv_file) if csv_file else None,
        preview_limit=preview_limit,
    )
    updated_at = None
    if result.upload_csv is not None and result.upload_csv.is_file():
        updated_at = datetime.fromtimestamp(result.upload_csv.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    shipping_dates = sorted(
        {row.get("出荷予定日", "") for row in result.preview_rows if row.get("出荷予定日")}
    )
    return {
        "ready_to_upload": result.ready_to_upload,
        "upload_csv": _path_text(result.upload_csv),
        "updated_at": updated_at,
        "source_rows": result.source_rows,
        "source_headers": list(result.source_headers),
        "shipping_dates": shipping_dates,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "preview_rows": [dict(row) for row in result.preview_rows],
    }


@app.post("/shipment-confirmation/upload/start")
async def shipment_confirmation_upload_start(request: Request):
    # NE反映ジョブ。execute（実反映）は確認チェック（confirm_upload）がないと開始しない。
    # サービス層（upload_next_engine_shipment_csv）でも同じガードを二重に持つ。
    form = await _read_form(request)
    execute = _form_bool(form, "execute")
    confirm_upload = _form_bool(form, "confirm_upload")
    if execute and not confirm_upload:
        return PlainTextResponse(
            "「確認しました: このCSVをネクストエンジンへ反映します」にチェックしてください。",
            status_code=400,
        )
    csv_file = form.get("csv_file", "").strip()
    headed = _form_headed(form)
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    preview_limit = max(1, min(_form_int(form, "preview_limit", 30), 100))

    steps = [("check", "候補CSV検証")]
    if execute:
        steps.append(("upload", "ネクストエンジンへ反映"))

    def worker(job_id: str) -> None:
        progress_jobs.update_step(job_id, "check", status="running")
        result = upload_next_engine_shipment_csv_sync(
            execute=execute,
            confirm_upload=confirm_upload,
            upload_csv=Path(csv_file) if csv_file else None,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
        )
        if not result.ready_to_upload:
            progress_jobs.update_step(job_id, "check", status="failed")
            progress_jobs.fail(
                job_id,
                "アップロード候補CSVの検証に失敗しました: "
                + " / ".join([*result.errors, *result.warnings][:3]),
            )
            return
        progress_jobs.update_step(
            job_id,
            "check",
            status="completed",
            detail=f"{result.source_rows}件",
        )
        if execute:
            if result.executed and not result.skipped_reason:
                progress_jobs.update_step(job_id, "upload", status="completed")
            else:
                progress_jobs.update_step(
                    job_id, "upload", status="failed", detail=result.skipped_reason
                )
                progress_jobs.fail(
                    job_id,
                    f"ネクストエンジンへの反映が完了しませんでした（{result.skipped_reason or '理由不明'}）。",
                )
                return
        progress_jobs.finish(
            job_id,
            message=(
                "ネクストエンジンへ反映しました。結果ページ本文を確認してください。"
                if execute
                else "アップロード前チェックが完了しました（NEへは反映していません）。"
            ),
            result={
                "executed": result.executed,
                "ready_to_upload": result.ready_to_upload,
                "upload_csv": _path_text(result.upload_csv),
                "source_rows": result.source_rows,
                "confirmation_text": (result.confirmation_text or "")[:300] or None,
                "warnings": list(result.warnings)[:5],
            },
        )

    job_id = progress_jobs.start(
        title="ネクストエンジンへ反映" if execute else "NE反映 アップロード前チェック",
        steps=steps,
        worker=worker,
        workflow="shipment_confirmation_upload",
        metadata={"execute": execute, "confirm_upload": confirm_upload, "headed": headed},
    )
    return {"job_id": job_id}


@app.get("/takaesu")
def takaesu_order_status():
    result = prepare_takaesu_order_workflow_sync(dry_run=True, write_audit=False)
    return {
        "flow_id": result.flow_id,
        "flow_name": result.flow_name,
        "executed": result.executed,
        "steps": [
            {
                "subflow": step.subflow,
                "target": step.target,
                "status": step.status,
            }
            for step in result.steps
        ],
    }


@app.get("/ne02-order-details")
def ne02_order_detail_status():
    result = download_ne02_order_details_sync(execute=False, write_audit=False)
    return {
        "flow_id": result.flow_id,
        "flow_name": result.flow_name,
        "executed": result.executed,
        "steps": [
            {
                "subflow": step.subflow,
                "target": step.target,
                "status": step.status,
            }
            for step in result.steps
        ],
    }


@app.post("/clickpost/create-csv", response_class=HTMLResponse)
def clickpost_create_csv(request: Request):
    try:
        result = create_clickpost_csv(preview_limit=30)
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": result,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "message": "clickpostimport.csv を作成しました。",
                "error": None,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": None,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "message": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.post("/clickpost/create-csv-upload-check", response_class=HTMLResponse)
def clickpost_create_csv_upload_check(request: Request):
    try:
        result = create_clickpost_csv(preview_limit=30)
        upload_result = upload_clickpost_csv_sync(csv_file=result.output_csv, execute=False)
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": result,
                "prepare_result": None,
                "upload_result": upload_result,
                "letterpack_pdf_result": None,
                "message": "clickpostimport.csv を作成し、アップロード前チェックまで実行しました。",
                "error": None,
            },
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_clickpost_csv(preview_limit=30)
        except Exception:
            pass
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": current_result,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "message": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.post("/clickpost/create-csv-upload-check/start")
def clickpost_create_csv_upload_check_start():
    def worker(job_id: str) -> None:
        progress_jobs.update_step(
            job_id,
            "create_csv",
            status="running",
            detail="最新の購入者データと商品情報データからCSVを作成しています。",
        )
        result = create_clickpost_csv(preview_limit=30)
        progress_jobs.update_step(
            job_id,
            "create_csv",
            status="completed",
            detail=f"{result.output_rows}件を出力しました。",
        )

        progress_jobs.update_step(
            job_id,
            "upload_check",
            status="running",
            detail="作成したCSVをアップロード前チェックしています。",
        )
        upload_result = upload_clickpost_csv_sync(csv_file=result.output_csv, execute=False)
        progress_jobs.update_step(
            job_id,
            "upload_check",
            status="completed",
            detail=f"{upload_result.target_rows}件を確認しました。",
        )
        progress_jobs.finish(
            job_id,
            message="CSV作成とアップロード前チェックが完了しました。",
            result={
                "conversion": _clickpost_conversion_summary(result),
                "upload": _clickpost_upload_summary(upload_result),
            },
        )

    job_id = progress_jobs.start(
        title="クリックポスト CSV作成＋アップロード前チェック",
        steps=[
            ("create_csv", "CSV作成"),
            ("upload_check", "アップロード前チェック"),
        ],
        worker=worker,
        workflow="clickpost_csv_upload_check",
        metadata={"execute_upload": False},
    )
    return {"job_id": job_id}


@app.post("/clickpost/prepare", response_class=HTMLResponse)
async def clickpost_prepare(request: Request):
    form = await _read_form(request)
    browser_mode = form.get("browser_mode", "background")
    if browser_mode not in {"background", "front"}:
        browser_mode = "background"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0

    try:
        prepare_result = await run_in_threadpool(
            prepare_clickpost_sync,
            fetch_next_engine=True,
            execute_downloads=True,
            write_conversion=True,
            write_letterpack_addresses=True,
            upload=False,
            execute_upload=False,
            output_type="D_ALL",
            headed=headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=30,
            download_invoices=True,
            restore_invoices_after_download=False,
        )
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": prepare_result.conversion,
                "prepare_result": prepare_result,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "browser_mode": browser_mode,
                "slow_mo_ms": slow_mo_ms if headed else 150,
                "message": "Next Engine から購入者データ、受注明細データ、納品書PDFを取得し、clickpostimport.csv と letterpack_addressbook.csv を作成しました。対象伝票は「納品書印刷済」へ移動しています（ヤマトページの『納品書印刷待ちへ復旧』で戻せます）。",
                "error": None,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": None,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "browser_mode": browser_mode,
                "slow_mo_ms": slow_mo_ms if headed else 150,
                "message": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.post("/clickpost/prepare/start")
async def clickpost_prepare_start(request: Request):
    form = await _read_form(request)
    browser_mode = form.get("browser_mode", "background")
    if browser_mode not in {"background", "front"}:
        browser_mode = "background"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0

    def worker(job_id: str) -> None:
        def update_progress(step: str, status: str, detail: str | None = None) -> None:
            progress_jobs.update_step(job_id, step, status=status, detail=detail)

        prepare_result = prepare_clickpost_sync(
            fetch_next_engine=True,
            execute_downloads=True,
            write_conversion=True,
            write_letterpack_addresses=True,
            upload=False,
            execute_upload=False,
            output_type="D_ALL",
            headed=headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=30,
            download_invoices=True,
            restore_invoices_after_download=False,
            progress_callback=update_progress,
        )
        progress_jobs.finish(
            job_id,
            message="NE取得、納品書PDF取得、CSV作成が完了しました。対象伝票は「納品書印刷済」へ移動しています。",
            result={"prepare": _clickpost_prepare_summary(prepare_result)},
        )

    job_id = progress_jobs.start(
        title="クリックポスト NE取得＋CSV作成",
        steps=[
            ("buyer_download", "NE購入者データ取得"),
            ("product_download", "NE受注明細データ取得"),
            ("invoice_download", "NE納品書PDF取得"),
            ("clickpost_csv", "クリックポストCSV作成"),
            ("letterpack_csv", "レターパック住所CSV作成"),
        ],
        worker=worker,
        workflow="clickpost_prepare",
        metadata={
            "browser_mode": browser_mode,
            "headed": headed,
            "slow_mo_ms": slow_mo_ms,
            "execute_downloads": True,
        },
    )
    return {"job_id": job_id}


@app.post("/clickpost/import-payment-print/start")
async def clickpost_import_payment_print_start(request: Request):
    form = await _read_form(request)
    if not _form_bool(form, "confirm_execute"):
        return PlainTextResponse("実行確認にチェックしてください。", status_code=400)

    browser_mode = form.get("browser_mode", "front")
    if browser_mode not in {"background", "front"}:
        browser_mode = "front"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0
    max_payments = max(1, min(_form_int(form, "max_payments", 20), 100))

    def worker(job_id: str) -> None:
        def update_progress(step: str, status: str, detail: str | None = None) -> None:
            progress_jobs.update_step(job_id, step, status=status, detail=detail)

        result = import_pay_print_clickpost_csv_sync(
            execute=True,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
            max_payments=max_payments,
            progress_callback=update_progress,
        )
        message = (
            "クリックポスト取込・決済・送り状番号取得が完了しました。"
            if result.executed and not result.skipped_reason
            else "クリックポスト本番処理を終了しました。結果を確認してください。"
        )
        progress_jobs.finish(
            job_id,
            message=message,
            result={"import_payment_print": _clickpost_import_payment_print_summary(result)},
        )

    job_id = progress_jobs.start(
        title="クリックポスト 取込・決済・送り状番号取得",
        steps=[
            ("precheck", "CSV確認"),
            ("login", "クリックポストログイン"),
            ("csv_import", "CSVインポート"),
            ("payment", "決済"),
            ("print_pdf", "まとめ印字PDF保存"),
            ("tracking_export", "送り状番号取得"),
        ],
        worker=worker,
        workflow="clickpost_import_payment_print",
        metadata={
            "browser_mode": browser_mode,
            "headed": headed,
            "slow_mo_ms": slow_mo_ms,
            "max_payments": max_payments,
            "execute": True,
        },
    )
    return {"job_id": job_id}


@app.post("/clickpost/full-run/start")
async def clickpost_full_run_start(request: Request):
    form = await _read_form(request)
    if not _form_bool(form, "confirm_execute"):
        return PlainTextResponse("実行確認にチェックしてください。", status_code=400)

    browser_mode = form.get("browser_mode", "front")
    if browser_mode not in {"background", "front"}:
        browser_mode = "front"
    headed = browser_mode == "front"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150 if headed else 0)
    if not headed:
        slow_mo_ms = 0
    max_payments = max(1, min(_form_int(form, "max_payments", 20), 100))

    def worker(job_id: str) -> None:
        def update_progress(step: str, status: str, detail: str | None = None) -> None:
            progress_jobs.update_step(job_id, step, status=status, detail=detail)

        prepare_result = prepare_clickpost_sync(
            fetch_next_engine=True,
            execute_downloads=True,
            write_conversion=True,
            write_letterpack_addresses=True,
            upload=False,
            execute_upload=False,
            output_type="D_ALL",
            headed=headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=30,
            download_invoices=True,
            restore_invoices_after_download=False,
            progress_callback=update_progress,
        )

        progress_jobs.update_step(
            job_id,
            "letterpack_pdf",
            status="running",
            detail="letterpack_addressbook.csv からPDFを作成しています。",
        )
        letterpack_pdf_result = create_letterpack_label_pdf(
            refresh_address_csv=False,
            preview_limit=30,
        )
        letterpack_detail = (
            f"{letterpack_pdf_result.output_rows}件 / {letterpack_pdf_result.page_count}ページ"
            if letterpack_pdf_result.output_pdf
            else "出力対象なし"
        )
        progress_jobs.update_step(
            job_id,
            "letterpack_pdf",
            status="completed",
            detail=letterpack_detail,
        )

        import_result = import_pay_print_clickpost_csv_sync(
            execute=True,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
            max_payments=max_payments,
            progress_callback=update_progress,
        )
        message = (
            "NE取得、納品書PDF取得、レターパックPDF作成、クリックポスト本番処理が完了しました。"
            if import_result.executed and not import_result.skipped_reason
            else "3処理まとめ実行を終了しました。結果を確認してください。"
        )
        progress_jobs.finish(
            job_id,
            message=message,
            result={
                "prepare": _clickpost_prepare_summary(prepare_result),
                "letterpack_pdf": _letterpack_pdf_summary(letterpack_pdf_result),
                "import_payment_print": _clickpost_import_payment_print_summary(import_result),
            },
        )

    job_id = progress_jobs.start(
        title="クリックポスト 3処理まとめ実行",
        steps=[
            ("buyer_download", "NE購入者データ取得"),
            ("product_download", "NE受注明細データ取得"),
            ("invoice_download", "NE納品書PDF取得"),
            ("clickpost_csv", "クリックポストCSV作成"),
            ("letterpack_csv", "レターパック住所CSV作成"),
            ("letterpack_pdf", "レターパックPDF作成"),
            ("precheck", "CSV確認"),
            ("login", "クリックポストログイン"),
            ("csv_import", "CSVインポート"),
            ("payment", "決済"),
            ("print_pdf", "まとめ印字PDF保存"),
            ("tracking_export", "送り状番号取得"),
        ],
        worker=worker,
        workflow="clickpost_full_run",
        metadata={
            "browser_mode": browser_mode,
            "headed": headed,
            "slow_mo_ms": slow_mo_ms,
            "max_payments": max_payments,
            "execute": True,
        },
    )
    return {"job_id": job_id}


@app.post("/clickpost/upload-check", response_class=HTMLResponse)
def clickpost_upload_check(request: Request):
    try:
        result = preview_clickpost_csv(preview_limit=30)
        upload_result = upload_clickpost_csv_sync(execute=False)
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": result,
                "prepare_result": None,
                "upload_result": upload_result,
                "letterpack_pdf_result": None,
                "message": "クリックポストアップロード前チェックを実行しました。",
                "error": None,
            },
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_clickpost_csv(preview_limit=30)
        except Exception:
            pass
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": current_result,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "message": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.post("/clickpost/create-letterpack-pdf", response_class=HTMLResponse)
def clickpost_create_letterpack_pdf(request: Request):
    try:
        letterpack_pdf_result = create_letterpack_label_pdf(refresh_address_csv=False, preview_limit=30)
        message = (
            f"レターパック宛名PDFを作成しました: {letterpack_pdf_result.output_pdf}"
            if letterpack_pdf_result.output_pdf
            else "レターパック宛名PDFの出力対象がありません。"
        )
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": None,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": letterpack_pdf_result,
                "letterpack_pdf_download_url": _letterpack_pdf_download_url(letterpack_pdf_result),
                "letterpack_pdf_open_url": _letterpack_pdf_download_url(
                    letterpack_pdf_result,
                    disposition="inline",
                ),
                "message": message,
                "error": None,
            },
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_clickpost_csv(preview_limit=30)
        except Exception:
            pass
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": current_result,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "message": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.post("/clickpost/create-letterpack-pdf/start")
def clickpost_create_letterpack_pdf_start():
    def worker(job_id: str) -> None:
        progress_jobs.update_step(
            job_id,
            "letterpack_pdf",
            status="running",
            detail="letterpack_addressbook.csv からPDFを作成しています。",
        )
        result = create_letterpack_label_pdf(refresh_address_csv=False, preview_limit=30)
        detail = (
            f"{result.output_rows}件 / {result.page_count}ページ"
            if result.output_pdf
            else "出力対象なし"
        )
        progress_jobs.update_step(
            job_id,
            "letterpack_pdf",
            status="completed",
            detail=detail,
        )
        progress_jobs.finish(
            job_id,
            message=(
                "レターパック宛名PDFを作成しました。"
                if result.output_pdf
                else "レターパック宛名PDFの出力対象がありません。"
            ),
            result={"letterpack_pdf": _letterpack_pdf_summary(result)},
        )

    job_id = progress_jobs.start(
        title="レターパックPDF作成",
        steps=[("letterpack_pdf", "レターパックPDF作成")],
        worker=worker,
        workflow="letterpack_pdf",
        metadata={"refresh_address_csv": False},
    )
    return {"job_id": job_id}


@app.post("/yamato/create-ne-to-yamato", response_class=HTMLResponse)
def yamato_create_ne_to_yamato(request: Request):
    try:
        result = create_ne_to_yamato_csv(preview_limit=30)
        return _yamato_response(
            request,
            result=result,
            message="ne-to-yamato CSVを作成しました。",
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_ne_to_yamato_conversion(preview_limit=30)
        except Exception:
            pass
        return _yamato_response(
            request,
            result=current_result,
            error=str(exc),
            status_code=500,
        )


@app.post("/yamato/prepare", response_class=HTMLResponse)
async def yamato_prepare(request: Request):
    form = await _read_form(request)
    action = form.get("action", "check")
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    headed = _form_bool(form, "headed")
    verify_invoice_statuses = _form_bool(form, "verify_invoice_statuses")
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)
    preview_limit = _form_int(form, "preview_limit", 30)

    flags = {
        "fetch_next_engine": action in {"check", "download", "full"},
        "execute_downloads": action in {"download", "full"},
        "check_invoices": action in {"check", "full"},
        "execute_invoices": action == "full",
        "check_custom_shipping": action == "shipping-check",
        "execute_custom_shipping": action == "full",
        "write_conversion": action == "full",
    }

    try:
        prepare_result = await run_in_threadpool(
            prepare_yamato_b2_sync,
            **flags,
            verify_invoice_statuses=verify_invoice_statuses,
            custom_shipping_order_numbers=order_numbers,
            output_type="D_ALL",
            headed=headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
        )
        message = "ヤマト一括準備を実行しました。"
        return _yamato_response(
            request,
            result=prepare_result.conversion,
            prepare_result=prepare_result,
            message=message,
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_ne_to_yamato_conversion(preview_limit=30)
        except Exception:
            pass
        return _yamato_response(
            request,
            result=current_result,
            error=str(exc),
            status_code=500,
        )


@app.post("/yamato/run-selected", response_class=HTMLResponse)
async def yamato_run_selected(request: Request):
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    selected = _yamato_selected_steps(form)
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    csv_file_raw = form.get("csv_file", "").strip()
    headed = _form_headed(form)
    verify_invoice_statuses = _form_bool(form, "verify_invoice_statuses")
    test_restore_print_wait = _form_bool(form, "test_restore_print_wait")
    confirm_import = _form_bool(form, "confirm_import") or form.get("import_mode", "execute") == "execute"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)
    preview_limit = _form_int(form, "preview_limit", 30)

    prepare_result = None
    restore_result = None
    b2_import_result = None
    current_result = None

    selected_names = [key for key, enabled in selected.items() if enabled]
    try:
        if not selected_names:
            raise ValueError("実行する項目を1つ以上選択してください。")

        should_prepare = any(
            selected[key]
            for key in (
                "ne_order_download",
                "ne_invoice_download",
                "ne_custom_data_import",
                "address_fix",
                "csv_create",
            )
        )
        if should_prepare:
            prepare_result = await run_in_threadpool(
                prepare_yamato_b2_sync,
                fetch_next_engine=selected["ne_order_download"],
                execute_downloads=selected["ne_order_download"],
                check_invoices=selected["ne_invoice_download"],
                execute_invoices=selected["ne_invoice_download"],
                verify_invoice_statuses=verify_invoice_statuses,
                check_custom_shipping=selected["ne_custom_data_import"],
                execute_custom_shipping=selected["ne_custom_data_import"],
                custom_shipping_order_numbers=order_numbers,
                write_conversion=selected["csv_create"],
                output_type="D_ALL",
                headed=headed,
                slow_mo_ms=slow_mo_ms,
                preview_limit=preview_limit,
                profile=profile,
            )
            current_result = prepare_result.conversion

            # テストモード: 納品書PDF取得で「印刷済み」へ進んだ伝票を、開発済みの
            # 「納品書印刷待ちへ復旧」でまとめて戻す。B2取込の前に戻すことで、
            # 後続が失敗してもNEステータスは復旧済みの状態を保つ。
            invoice = prepare_result.invoice
            if test_restore_print_wait and invoice is not None and invoice.executed:
                restore_orders = _yamato_restore_order_numbers_from_prepare_result(prepare_result)
                if restore_orders:
                    restore_result = await run_in_threadpool(
                        restore_next_engine_print_wait_batch_sync,
                        restore_orders,
                        execute=True,
                        headless=not headed,
                        slow_mo_ms=slow_mo_ms,
                    )

        if selected["b2_login"] or selected["b2_import"]:
            b2_import_result = await import_yamato_b2_csv(
                csv_file=Path(csv_file_raw) if csv_file_raw else resolve_default_b2_csv(profile.output_prefix),
                check_login=selected["b2_login"] and not selected["b2_import"],
                open_import_page=False,
                select_file_dry_run=selected["b2_import"] and not confirm_import,
                execute_import=selected["b2_import"] and confirm_import,
                confirm_import=confirm_import,
                headless=not headed,
                slow_mo_ms=slow_mo_ms,
                keep_browser_open=headed,
            )

        if current_result is None:
            current_result = preview_ne_to_yamato_conversion(
                preview_limit=preview_limit, profile=profile
            )

        message = "選択した処理を実行しました。"
        if selected["b2_import"] and not confirm_import:
            message += " B2インポートは確認なしのためCSV選択までです。"
        if restore_result is not None:
            failed_orders = list(restore_result.failed_order_numbers)
            if failed_orders:
                message += (
                    " テストモード: 印刷待ちへ復旧できなかった伝票があります"
                    f"（{', '.join(failed_orders)}）。『その他の操作・納品書印刷待ちへ復旧』で手動復旧してください。"
                )
            else:
                message += f" テストモード: {len(restore_result.order_numbers)}件を印刷待ちへ復旧しました。"

        return _yamato_response(
            request,
            result=current_result,
            prepare_result=prepare_result,
            restore_result=restore_result,
            b2_import_result=b2_import_result,
            message=message,
            profile=profile,
        )
    except Exception as exc:
        if current_result is None:
            try:
                current_result = preview_ne_to_yamato_conversion(preview_limit=30, profile=profile)
            except Exception:
                pass
        # 途中で止まった場合、印刷済みへ進んだ伝票番号をエラー表示へ必ず明示する
        # （prepare 内の失敗は例外メッセージ側に明示済み。ここは B2 等の後段失敗を補う）。
        error_text = str(exc)
        printed_notice = _yamato_printed_orders_notice(prepare_result, restore_result)
        if printed_notice and printed_notice not in error_text:
            error_text = f"{error_text} {printed_notice}"
        return _yamato_response(
            request,
            result=current_result,
            prepare_result=prepare_result,
            restore_result=restore_result,
            b2_import_result=b2_import_result,
            error=error_text,
            status_code=500,
            profile=profile,
        )


@app.post("/yamato/run-selected/start")
async def yamato_run_selected_start(request: Request):
    # run-selected の非同期版。進捗をフローチップに同期表示するため、
    # 重い処理をバックグラウンドジョブ化し、フロントは /progress をポーリングする。
    # ステップは粗い2段階: prepare(受注取得〜CSV作成=チップ①〜⑤) / b2_import(B2取込=チップ⑥)。
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    selected = _yamato_selected_steps(form)
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    csv_file_raw = form.get("csv_file", "").strip()
    headed = _form_headed(form)
    verify_invoice_statuses = _form_bool(form, "verify_invoice_statuses")
    test_restore_print_wait = _form_bool(form, "test_restore_print_wait")
    confirm_import = _form_bool(form, "confirm_import") or form.get("import_mode", "execute") == "execute"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)
    preview_limit = _form_int(form, "preview_limit", 30)

    selected_names = [key for key, enabled in selected.items() if enabled]
    if not selected_names:
        return PlainTextResponse("実行する項目を1つ以上選択してください。", status_code=400)

    should_prepare = any(
        selected[key]
        for key in (
            "ne_order_download",
            "ne_invoice_download",
            "ne_custom_data_import",
            "address_fix",
            "csv_create",
        )
    )
    do_b2 = selected["b2_login"] or selected["b2_import"]

    steps: list[tuple[str, str]] = []
    do_restore = test_restore_print_wait and should_prepare and selected["ne_invoice_download"]
    if should_prepare:
        # 「どこで止まったか」を分かりやすくするため、受注取得を工程ごとに分割表示する。
        steps.extend(
            [
                ("ne_fetch", "NEデータ取得"),
                ("invoice", "納品書PDF取得"),
                ("custom", "配送情報CSV取得"),
                ("conversion", "住所補正・B2取込CSV作成"),
            ]
        )
    if do_restore:
        steps.append(("restore", "印刷待ちへ復旧（テストモード）"))
    if do_b2:
        steps.append(("b2_import", "ヤマトB2へ取込"))

    def worker(job_id: str) -> None:
        current_result = None
        prepare_result = None
        restore_result = None
        if should_prepare:
            # prepare が各工程の running/completed/failed を progress で報告する
            # → 失敗した工程だけが赤くなり、どこで止まったかが一目で分かる。
            def _prep_progress(key: str, status: str, detail: str | None = None) -> None:
                # 工程境界で中止要求をチェックし、要求されていれば安全に停止する（協調キャンセル）。
                if progress_jobs.is_cancel_requested(job_id):
                    raise JobCancelled()
                progress_jobs.update_step(job_id, key, status=status, detail=detail)

            prepare_result = prepare_yamato_b2_sync(
                fetch_next_engine=selected["ne_order_download"],
                execute_downloads=selected["ne_order_download"],
                check_invoices=selected["ne_invoice_download"],
                execute_invoices=selected["ne_invoice_download"],
                verify_invoice_statuses=verify_invoice_statuses,
                check_custom_shipping=selected["ne_custom_data_import"],
                execute_custom_shipping=selected["ne_custom_data_import"],
                custom_shipping_order_numbers=order_numbers,
                write_conversion=selected["csv_create"],
                output_type="D_ALL",
                headed=headed,
                slow_mo_ms=slow_mo_ms,
                preview_limit=preview_limit,
                progress=_prep_progress,
                profile=profile,
            )
            current_result = prepare_result.conversion

            # テストモード: 納品書PDF取得で「印刷済み」へ進んだ伝票を、開発済みの
            # 「納品書印刷待ちへ復旧」でまとめて戻す。B2取込の前に戻すことで、
            # 後続が失敗してもNEステータスは復旧済みの状態を保つ。
            if do_restore:
                invoice = prepare_result.invoice
                restore_orders = _yamato_restore_order_numbers_from_prepare_result(prepare_result)
                if invoice is None or not invoice.executed or not restore_orders:
                    progress_jobs.update_step(job_id, "restore", status="completed", detail="対象なし")
                else:
                    progress_jobs.update_step(job_id, "restore", status="running")
                    try:
                        restore_result = restore_next_engine_print_wait_batch_sync(
                            restore_orders,
                            execute=True,
                            headless=not headed,
                            slow_mo_ms=slow_mo_ms,
                        )
                    except Exception as exc:
                        # 復旧に失敗すると本番ステータスが「印刷済み」のまま残るため、
                        # 必ず気付けるようジョブを失敗させ、手動復旧に必要な伝票番号を残す。
                        progress_jobs.update_step(job_id, "restore", status="failed")
                        progress_jobs.fail(
                            job_id,
                            "印刷待ちへの復旧でエラーが発生しました。"
                            "『その他の操作・納品書印刷待ちへ復旧』で次の伝票を手動復旧してください: "
                            f"{', '.join(restore_orders)} / エラー: {exc}",
                        )
                        return
                    failed_orders = list(restore_result.failed_order_numbers)
                    if failed_orders:
                        progress_jobs.update_step(
                            job_id, "restore", status="failed", detail=f"復旧失敗 {len(failed_orders)}件"
                        )
                        progress_jobs.fail(
                            job_id,
                            "一部の伝票を印刷待ちへ復旧できませんでした。"
                            "『その他の操作・納品書印刷待ちへ復旧』で手動復旧してください: "
                            f"{', '.join(failed_orders)}",
                        )
                        return
                    progress_jobs.update_step(
                        job_id,
                        "restore",
                        status="completed",
                        detail=f"{len(restore_orders)}件を印刷待ちへ戻しました",
                    )

        b2_result = None
        if do_b2:
            progress_jobs.update_step(job_id, "b2_import", status="running")
            try:
                b2_result = import_yamato_b2_csv_sync(
                    csv_file=Path(csv_file_raw) if csv_file_raw else resolve_default_b2_csv(profile.output_prefix),
                    check_login=selected["b2_login"] and not selected["b2_import"],
                    open_import_page=False,
                    select_file_dry_run=selected["b2_import"] and not confirm_import,
                    execute_import=selected["b2_import"] and confirm_import,
                    confirm_import=confirm_import,
                    headless=not headed,
                    slow_mo_ms=slow_mo_ms,
                    keep_browser_open=headed,
                )
            except Exception as exc:
                progress_jobs.update_step(job_id, "b2_import", status="failed")
                # ここで止まった場合も、印刷済みへ進んだ伝票番号を失敗メッセージへ必ず明示する。
                printed_notice = _yamato_printed_orders_notice(prepare_result, restore_result)
                if printed_notice:
                    raise RuntimeError(f"{exc} {printed_notice}") from exc
                raise

            # B2の実取込が要求された(execute想定)のに完了していなければ、ジョブを失敗扱いにして理由を表示する。
            # 従来は例外が出なければ無条件に「完了」表示になり、B2失敗(skipped_reason等)が画面で分からなかった。
            expected_execute = selected["b2_import"] and confirm_import
            b2_executed = bool(getattr(b2_result, "import_executed", False))
            b2_skip = getattr(b2_result, "skipped_reason", None)
            b2_page = getattr(b2_result, "page_url", None)
            b2_warnings = list(getattr(b2_result, "warnings", ()) or ())
            if expected_execute and not b2_executed:
                progress_jobs.update_step(job_id, "b2_import", status="failed", detail=b2_skip)
                parts = [f"B2取込が完了しませんでした（理由: {b2_skip or '不明'}）。"]
                if current_result is not None and getattr(current_result, "output_csv", None):
                    parts.append("NEデータ取得・CSV作成は完了しています（/yamato で確認・再取込できます）。")
                printed_notice = _yamato_printed_orders_notice(prepare_result, restore_result)
                if printed_notice:
                    parts.append(printed_notice)
                if b2_page:
                    parts.append(f"B2画面: {b2_page}")
                if b2_warnings:
                    parts.append("警告: " + " / ".join(b2_warnings[:3]))
                progress_jobs.fail(job_id, " ".join(parts))
                return
            progress_jobs.update_step(job_id, "b2_import", status="completed", detail=b2_skip)

        message = "選択した処理を実行しました。"
        if selected["b2_import"] and not confirm_import:
            message += " B2インポートは確認なしのためCSV選択までです。"
        if restore_result is not None:
            message += f" テストモード: {len(restore_result.order_numbers)}件を印刷待ちへ復旧しました。"

        result_summary: dict[str, object] = {}
        if current_result is not None:
            result_summary["output_csv"] = _path_text(getattr(current_result, "output_csv", None))
            result_summary["output_rows"] = getattr(current_result, "output_rows", None)
        if restore_result is not None:
            result_summary["print_wait_restored"] = len(restore_result.order_numbers)
        if b2_result is not None:
            result_summary["b2_import_executed"] = bool(getattr(b2_result, "import_executed", False))
            result_summary["b2_skipped_reason"] = getattr(b2_result, "skipped_reason", None)
        progress_jobs.finish(job_id, message=message, result=result_summary)

    job_id = progress_jobs.start(
        title="ヤマト用CSV作成" if profile.key == "yamato" else f"{profile.label}用CSV作成",
        steps=steps,
        worker=worker,
        workflow=f"{profile.key}_run_selected",
        metadata={
            "headed": headed,
            "slow_mo_ms": slow_mo_ms,
            "confirm_import": confirm_import,
            "test_restore_print_wait": test_restore_print_wait,
            "mode": profile.key,
        },
    )
    return {"job_id": job_id}


@app.post("/yamato/restore-print-wait", response_class=HTMLResponse)
async def yamato_restore_print_wait(request: Request):
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    headed = _form_headed(form)
    execute = _form_bool(form, "execute") or form.get("restore_mode") == "execute"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)

    try:
        restore_result = await run_in_threadpool(
            restore_next_engine_print_wait_batch_sync,
            order_numbers,
            execute=execute,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
        )
        current_result = preview_ne_to_yamato_conversion(preview_limit=30, profile=profile)
        return _yamato_response(
            request,
            result=current_result,
            restore_result=restore_result,
            message="納品書印刷待ちへの復旧を実行しました。" if execute else "復旧対象を確認しました。",
            profile=profile,
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_ne_to_yamato_conversion(preview_limit=30, profile=profile)
        except Exception:
            pass
        return _yamato_response(
            request,
            result=current_result,
            error=str(exc),
            status_code=500,
            profile=profile,
        )


@app.post("/yamato/b2-import", response_class=HTMLResponse)
async def yamato_b2_import(request: Request):
    form = await _read_form(request)
    action = form.get("action", "validate")
    csv_file_raw = form.get("csv_file", "").strip()
    headed = _form_bool(form, "headed")
    confirm_import = _form_bool(form, "confirm_import")
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)

    try:
        b2_import_result = await import_yamato_b2_csv(
            csv_file=Path(csv_file_raw) if csv_file_raw else None,
            check_login=action == "check-login",
            open_import_page=action == "open-import-page",
            select_file_dry_run=action == "select-file-dry-run",
            execute_import=action == "execute-import",
            confirm_import=confirm_import,
            headless=not headed,
            slow_mo_ms=slow_mo_ms,
            keep_browser_open=headed,
        )
        current_result = preview_ne_to_yamato_conversion(preview_limit=30)
        message = (
            "B2取込を実行しました。"
            if b2_import_result.import_executed
            else "B2取込チェックを実行しました。"
        )
        return _yamato_response(
            request,
            result=current_result,
            b2_import_result=b2_import_result,
            message=message,
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = preview_ne_to_yamato_conversion(preview_limit=30)
        except Exception:
            pass
        return _yamato_response(
            request,
            result=current_result,
            error=str(exc),
            status_code=500,
        )


def _wait_for_cdp(endpoint: str | None, *, timeout: float = 12.0) -> bool:
    """Chromeのリモートデバッグポートが応答するまで待つ（CDP接続前）。"""
    if not endpoint:
        return False
    import time
    import urllib.request

    url = endpoint.rstrip("/") + "/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _b2_outcome_summary(outcome: dict) -> dict[str, object]:
    warnings = list(outcome.get("warnings") or ())
    return {
        "file_selected": bool(outcome.get("file_selected")),
        "import_executed": bool(outcome.get("import_executed")),
        "skipped_reason": outcome.get("skipped_reason"),
        "warnings": warnings[:3],
    }


@app.post("/yamato/b2-open/start")
async def yamato_b2_open_start(request: Request):
    # ⑥「B2取込ブラウザを開く」の非同期版。実Chromeを独立起動し（印刷まで開いたまま）、
    # automate=on のときのみ CDP接続で取込まで自動を試みる（縮退時は手動フォールバック）。
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    automate = _form_bool(form, "automate")
    csv_file_raw = form.get("csv_file", "").strip()
    csv_path = Path(csv_file_raw) if csv_file_raw else resolve_default_b2_csv(profile.output_prefix)
    if csv_path is None or not Path(csv_path).is_file():
        return PlainTextResponse(f"先に{profile.label}用CSVを作成してください。", status_code=400)
    csv_path = Path(csv_path)

    steps: list[tuple[str, str]] = [("open", "B2ブラウザを開く")]
    if automate:
        steps.append(("b2_import", "B2へ自動で取込"))

    def worker(job_id: str) -> None:
        progress_jobs.update_step(job_id, "open", status="running")
        try:
            state = b2_chrome.launch(csv_path=csv_path, enable_cdp=automate)
        except Exception:
            progress_jobs.update_step(job_id, "open", status="failed")
            raise
        progress_jobs.update_step(job_id, "open", status="completed")

        if not automate:
            progress_jobs.finish(
                job_id,
                message="B2ブラウザを開きました。取込・印刷を進めてください。",
                result={"mode": "manual", "pid": state.get("pid")},
            )
            return

        progress_jobs.update_step(job_id, "b2_import", status="running")
        endpoint = state.get("cdp_endpoint")
        if not _wait_for_cdp(endpoint):
            progress_jobs.update_step(job_id, "b2_import", status="failed", detail="cdp_not_ready")
            progress_jobs.finish(
                job_id,
                message="自動化に接続できませんでした。開いているB2ブラウザで手動で取込・印刷してください。",
                result={"mode": "auto_failed", "reason": "cdp_not_ready"},
            )
            return
        try:
            outcome = run_b2_import_over_cdp_sync(
                endpoint,
                csv_file=csv_path,
                execute_import=True,
                confirm_import=True,
            )
        except Exception as exc:
            # 自動化が落ちても実Chromeは開いたまま＝手動で継続できる。
            progress_jobs.update_step(job_id, "b2_import", status="failed", detail=str(exc))
            progress_jobs.finish(
                job_id,
                message="自動取込は失敗しました。開いているB2ブラウザで手動で取込・印刷してください。",
                result={"mode": "auto_failed", "error": str(exc)},
            )
            return

        if outcome.get("import_executed"):
            progress_jobs.update_step(job_id, "b2_import", status="completed")
            progress_jobs.finish(
                job_id,
                message="B2取込まで自動で完了しました。開いているブラウザで印刷してください。",
                result={"mode": "auto", **_b2_outcome_summary(outcome)},
            )
        else:
            reason = outcome.get("skipped_reason")
            progress_jobs.update_step(job_id, "b2_import", status="failed", detail=reason)
            progress_jobs.finish(
                job_id,
                message=(
                    f"自動取込は完了しませんでした（{reason or '理由不明'}）。"
                    "開いているB2ブラウザで手動で取込・印刷してください。"
                ),
                result={"mode": "auto_incomplete", **_b2_outcome_summary(outcome)},
            )

    job_id = progress_jobs.start(
        title="B2取込ブラウザ",
        steps=steps,
        worker=worker,
        workflow=f"{profile.key}_b2_open",
        metadata={"automate": automate, "mode": profile.key},
    )
    return {"job_id": job_id}


@app.post("/yamato/b2-open", response_class=HTMLResponse)
async def yamato_b2_open(request: Request):
    # JS無効時のフォールバック（モードA=手動のみ）。実Chromeを開いて元のカードへ戻る。
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    csv_file_raw = form.get("csv_file", "").strip()
    csv_path = Path(csv_file_raw) if csv_file_raw else resolve_default_b2_csv(profile.output_prefix)
    if csv_path is not None and Path(csv_path).is_file():
        try:
            await run_in_threadpool(b2_chrome.launch, csv_path=Path(csv_path), enable_cdp=False)
        except Exception:
            pass
    return RedirectResponse(url="/nekopos" if profile.key == "nekopos" else "/yamato", status_code=303)


@app.post("/yamato/run-full/start")
async def yamato_run_full_start(request: Request):
    # 「CSV作成からB2取込まで一括実行」: prepare（NE取得〜B2取込CSV作成）と
    # 実ChromeでのB2取込（b2-open の automate 相当）を 1 ジョブで続けて実行する。
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    headed = _form_headed(form)
    verify_invoice_statuses = _form_bool(form, "verify_invoice_statuses")
    test_restore_print_wait = _form_bool(form, "test_restore_print_wait")
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)
    preview_limit = _form_int(form, "preview_limit", 30)

    steps: list[tuple[str, str]] = [
        ("ne_fetch", "NEデータ取得"),
        ("invoice", "納品書PDF取得"),
        ("custom", "配送情報CSV取得"),
        ("conversion", "住所補正・B2取込CSV作成"),
    ]
    if test_restore_print_wait:
        steps.append(("restore", "印刷待ちへ復旧（テストモード）"))
    steps.extend(
        [
            ("b2_open", "B2ブラウザを開く"),
            ("b2_import", "B2へ自動で取込"),
        ]
    )

    def worker(job_id: str) -> None:
        def _prep_progress(key: str, status: str, detail: str | None = None) -> None:
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
            progress_jobs.update_step(job_id, key, status=status, detail=detail)

        prepare_result = prepare_yamato_b2_sync(
            fetch_next_engine=True,
            execute_downloads=True,
            check_invoices=True,
            execute_invoices=True,
            verify_invoice_statuses=verify_invoice_statuses,
            check_custom_shipping=True,
            execute_custom_shipping=True,
            custom_shipping_order_numbers=tuple(),
            write_conversion=True,
            output_type="D_ALL",
            headed=headed,
            slow_mo_ms=slow_mo_ms,
            preview_limit=preview_limit,
            progress=_prep_progress,
            profile=profile,
        )
        conversion = prepare_result.conversion

        # テストモード: 納品書PDF取得で「印刷済み」へ進んだ伝票を、B2取込の前に
        # 「納品書印刷待ちへ復旧」でまとめて戻す（run-selected と同じ扱い）。
        restore_result = None
        if test_restore_print_wait:
            invoice = prepare_result.invoice
            restore_orders = _yamato_restore_order_numbers_from_prepare_result(prepare_result)
            if invoice is None or not invoice.executed or not restore_orders:
                progress_jobs.update_step(job_id, "restore", status="completed", detail="対象なし")
            else:
                progress_jobs.update_step(job_id, "restore", status="running")
                try:
                    restore_result = restore_next_engine_print_wait_batch_sync(
                        restore_orders,
                        execute=True,
                        headless=not headed,
                        slow_mo_ms=slow_mo_ms,
                    )
                except Exception as exc:
                    progress_jobs.update_step(job_id, "restore", status="failed")
                    progress_jobs.fail(
                        job_id,
                        "印刷待ちへの復旧でエラーが発生しました。"
                        "『その他の操作・納品書印刷待ちへ復旧』で次の伝票を手動復旧してください: "
                        f"{', '.join(restore_orders)} / エラー: {exc}",
                    )
                    return
                failed_orders = list(restore_result.failed_order_numbers)
                if failed_orders:
                    progress_jobs.update_step(
                        job_id, "restore", status="failed", detail=f"復旧失敗 {len(failed_orders)}件"
                    )
                    progress_jobs.fail(
                        job_id,
                        "一部の伝票を印刷待ちへ復旧できませんでした。"
                        "『その他の操作・納品書印刷待ちへ復旧』で手動復旧してください: "
                        f"{', '.join(failed_orders)}",
                    )
                    return
                progress_jobs.update_step(
                    job_id,
                    "restore",
                    status="completed",
                    detail=f"{len(restore_orders)}件を印刷待ちへ戻しました",
                )

        printed_notice = _yamato_printed_orders_notice(prepare_result, restore_result)
        restore_note = (
            f" テストモード: {len(restore_result.order_numbers)}件を印刷待ちへ復旧しました。"
            if restore_result is not None
            else ""
        )

        # 新しく作成されたCSVのみをB2へ渡す（古いCSVへのフォールバックは事故のもとなので行わない）。
        output_csv = getattr(conversion, "output_csv", None)
        if not output_csv or not Path(output_csv).is_file():
            progress_jobs.update_step(job_id, "b2_open", status="failed", detail="CSV未作成")
            parts = ["B2取込CSVが作成されなかったため、B2取込を中止しました。"]
            if prepare_result.consistency_warnings:
                parts.append("警告: " + " / ".join(prepare_result.consistency_warnings[:3]))
            if printed_notice:
                parts.append(printed_notice)
            progress_jobs.fail(job_id, " ".join(parts))
            return

        progress_jobs.update_step(job_id, "b2_open", status="running")
        try:
            state = b2_chrome.launch(csv_path=Path(output_csv), enable_cdp=True)
        except Exception as exc:
            progress_jobs.update_step(job_id, "b2_open", status="failed")
            if printed_notice:
                raise RuntimeError(f"{exc} {printed_notice}") from exc
            raise
        progress_jobs.update_step(job_id, "b2_open", status="completed")

        progress_jobs.update_step(job_id, "b2_import", status="running")
        endpoint = state.get("cdp_endpoint")
        if not _wait_for_cdp(endpoint):
            progress_jobs.update_step(job_id, "b2_import", status="failed", detail="cdp_not_ready")
            progress_jobs.finish(
                job_id,
                message=(
                    "CSV作成は完了しましたが、自動化に接続できませんでした。"
                    "開いているB2ブラウザで手動で取込・印刷してください。" + restore_note
                ),
                result={"mode": "auto_failed", "reason": "cdp_not_ready"},
            )
            return
        try:
            outcome = run_b2_import_over_cdp_sync(
                endpoint,
                csv_file=Path(output_csv),
                execute_import=True,
                confirm_import=True,
            )
        except Exception as exc:
            # 自動化が落ちても実Chromeは開いたまま＝手動で継続できる。
            progress_jobs.update_step(job_id, "b2_import", status="failed", detail=str(exc))
            progress_jobs.finish(
                job_id,
                message=(
                    "CSV作成は完了しましたが、自動取込は失敗しました。"
                    "開いているB2ブラウザで手動で取込・印刷してください。" + restore_note
                ),
                result={"mode": "auto_failed", "error": str(exc)},
            )
            return

        if outcome.get("import_executed"):
            progress_jobs.update_step(job_id, "b2_import", status="completed")
            progress_jobs.finish(
                job_id,
                message="CSV作成からB2取込まで自動で完了しました。開いているブラウザで印刷してください。" + restore_note,
                result={
                    "mode": "auto",
                    "output_csv": _path_text(output_csv),
                    "output_rows": getattr(conversion, "output_rows", None),
                    **(
                        {"print_wait_restored": len(restore_result.order_numbers)}
                        if restore_result is not None
                        else {}
                    ),
                    **_b2_outcome_summary(outcome),
                },
            )
        else:
            reason = outcome.get("skipped_reason")
            progress_jobs.update_step(job_id, "b2_import", status="failed", detail=reason)
            progress_jobs.finish(
                job_id,
                message=(
                    f"CSV作成は完了しましたが、自動取込は完了しませんでした（{reason or '理由不明'}）。"
                    "開いているB2ブラウザで手動で取込・印刷してください。" + restore_note
                ),
                result={"mode": "auto_incomplete", **_b2_outcome_summary(outcome)},
            )

    job_id = progress_jobs.start(
        title=f"{profile.label} 一括実行",
        steps=steps,
        worker=worker,
        workflow=f"{profile.key}_run_full",
        metadata={
            "headed": headed,
            "slow_mo_ms": slow_mo_ms,
            "test_restore_print_wait": test_restore_print_wait,
            "verify_invoice_statuses": verify_invoice_statuses,
            "mode": profile.key,
        },
    )
    return {"job_id": job_id}


@app.post("/yamato/run-full", response_class=HTMLResponse)
async def yamato_run_full(request: Request):
    # JS無効時のフォールバック: 同じ一括ジョブを開始して元のカードへ戻る（進捗は /jobs で確認できる）。
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    await yamato_run_full_start(request)
    return RedirectResponse(url="/nekopos" if profile.key == "nekopos" else "/yamato", status_code=303)


@app.post("/yamato/b2-close", response_class=HTMLResponse)
async def yamato_b2_close(request: Request):
    # 印刷完了後にB2ブラウザを閉じる。
    form = await _read_form(request)
    profile = profile_for_mode(form.get("mode"))
    await run_in_threadpool(b2_chrome.close)
    return RedirectResponse(url="/nekopos" if profile.key == "nekopos" else "/yamato", status_code=303)


def _access_analytics_tab(request: Request) -> str:
    tab = request.query_params.get("tab", "rakuten")
    return tab if tab in {"rakuten", "yahoo"} else "rakuten"


def _billing_tab(request: Request) -> str:
    tab = request.query_params.get("tab", "yahoo")
    return tab if tab in {"yahoo", "rakuten"} else "yahoo"


def _default_target_date() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _default_target_month() -> str:
    previous_month = date.today().replace(day=1) - timedelta(days=1)
    return previous_month.strftime("%Y-%m")


def _parse_date_range(form: dict[str, str]) -> tuple[date, date]:
    default = _default_target_date()
    start_raw = form.get("period_start", "").strip() or default
    end_raw = form.get("period_end", "").strip() or default
    try:
        period_start = date.fromisoformat(start_raw)
        period_end = date.fromisoformat(end_raw)
    except ValueError as exc:
        raise ValueError("開始日・終了日は YYYY-MM-DD で指定してください。") from exc
    if period_start.isoformat() != start_raw or period_end.isoformat() != end_raw:
        raise ValueError("開始日・終了日は YYYY-MM-DD で指定してください。")
    if period_start > period_end:
        raise ValueError("開始日は終了日以前にしてください。")
    if (period_end - period_start).days > 365:
        raise ValueError("一度に指定できる期間は366日以内です。")
    return period_start, period_end


def _iter_dates(period_start: date, period_end: date):
    current = period_start
    while current <= period_end:
        yield current
        if current == period_end:
            break
        current += timedelta(days=1)


def _browser_mode(form: dict[str, str]) -> str:
    value = form.get("browser_mode", "background")
    return value if value in {"background", "front"} else "background"


def _mark_steps_failed(job_id: str, keys: tuple[str, ...]) -> None:
    for key in keys:
        progress_jobs.update_step(job_id, key, status="failed")


def _zip_artifacts(
    artifacts: list[tuple[str, Path]],
    *,
    download_name: str,
) -> Response:
    if not artifacts:
        return PlainTextResponse("一括ダウンロード対象が見つかりません。", status_code=404)
    output = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for requested_name, path in artifacts:
            name = Path(requested_name).name or path.name
            if name in used_names:
                name = f"{Path(name).stem}_{len(used_names) + 1}{Path(name).suffix}"
            used_names.add(name)
            archive.write(path, arcname=name)
    return Response(
        output.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/access-analytics", response_class=HTMLResponse)
def access_analytics_page(request: Request):
    active_tab = _access_analytics_tab(request)
    return templates.TemplateResponse(
        "access_analytics.html",
        {
            "request": request,
            "active_tab": active_tab,
            "default_target_date": _default_target_date(),
            "browser_mode": "background",
            "defer_preview": True,
            "result": None,
            "error": None,
        },
    )


@app.get("/access-analytics/preview", response_class=HTMLResponse)
def access_analytics_preview(request: Request):
    active_tab = _access_analytics_tab(request)
    try:
        result = read_access_analytics_preview(active_tab)
        preview_error = None
    except Exception as exc:
        result = None
        preview_error = str(exc)
    return templates.TemplateResponse(
        "_access_analytics_results.html",
        {
            "request": request,
            "active_tab": active_tab,
            "result": result,
            "preview_error": preview_error,
        },
    )


@app.post("/access-analytics/rakuten/start")
async def access_analytics_rakuten_start(request: Request):
    form = await _read_form(request)
    try:
        period_start, period_end = _parse_date_range(form)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    browser_mode = _browser_mode(form)
    headless = browser_mode != "front"
    slow_mo_ms = 150 if not headless else 0

    def worker(job_id: str) -> None:
        active_keys = ("login_check", "fetch")
        progress_jobs.update_step(job_id, "login_check", status="running")
        progress_jobs.update_step(job_id, "fetch", status="running")
        results = []
        try:
            for target in _iter_dates(period_start, period_end):
                if progress_jobs.is_cancel_requested(job_id):
                    raise JobCancelled()
                results.append(
                    download_rakuten_device_access_sync(
                        execute=True,
                        target_date=target,
                        include_all=False,
                        headless=headless,
                        slow_mo_ms=slow_mo_ms,
                    )
                )
                if progress_jobs.is_cancel_requested(job_id):
                    raise JobCancelled()
        except Exception:
            _mark_steps_failed(job_id, active_keys)
            raise
        progress_jobs.update_step(job_id, "login_check", status="completed")
        progress_jobs.update_step(job_id, "fetch", status="completed")
        progress_jobs.update_step(job_id, "validate_save", status="running")
        file_count = sum(len(result.csv_files) for result in results)
        row_count = sum(
            item.row_count for result in results for item in result.csv_files
        )
        try:
            record_access_analytics_batch(
                mall="rakuten",
                batch_id=f"job-{job_id}",
                target_label=f"{period_start.isoformat()}..{period_end.isoformat()}",
                artifacts=[
                    (item.downloaded_file.name, item.source_sha256)
                    for result in results
                    for item in result.csv_files
                ],
            )
        except Exception:
            progress_jobs.update_step(job_id, "validate_save", status="failed")
            raise
        sha8 = [
            item.source_sha256[:8]
            for result in results
            for item in result.csv_files
        ]
        progress_jobs.update_step(
            job_id,
            "validate_save",
            status="completed",
            detail=f"{file_count}件を検証・保存しました。",
        )
        progress_jobs.finish(
            job_id,
            message=f"楽天アクセス解析CSVを{file_count}件保存しました。",
            result={
                "file_count": file_count,
                "row_count": row_count,
                "sha8": sha8,
            },
        )

    job_id = progress_jobs.start(
        title="楽天市場 アクセス解析取得",
        steps=[
            ("login_check", "ログイン確認"),
            ("fetch", "3端末のCSV取得"),
            ("validate_save", "検証・保存"),
        ],
        worker=worker,
        workflow="access_analytics_rakuten",
        metadata={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "browser_mode": browser_mode,
        },
    )
    return {"job_id": job_id}


@app.post("/access-analytics/yahoo/start")
async def access_analytics_yahoo_start(request: Request):
    form = await _read_form(request)
    try:
        period_start, period_end = _parse_date_range(form)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    browser_mode = _browser_mode(form)
    headless = browser_mode != "front"
    slow_mo_ms = 150 if not headless else 0

    def worker(job_id: str) -> None:
        active_keys = ("login_check", "fetch_product", "fetch_overall")
        for key in active_keys:
            progress_jobs.update_step(job_id, key, status="running")
        try:
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
            result = download_yahoo_access_reports_sync(
                execute=True,
                period_start=period_start,
                period_end=period_end,
                headless=headless,
                slow_mo_ms=slow_mo_ms,
            )
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
        except Exception:
            _mark_steps_failed(job_id, active_keys)
            raise
        for key in active_keys:
            progress_jobs.update_step(job_id, key, status="completed")
        progress_jobs.update_step(job_id, "validate_save", status="running")
        all_files = ([result.product] if result.product else []) + list(result.overall)
        progress_jobs.update_step(
            job_id,
            "validate_save",
            status="completed",
            detail=f"{len(all_files)}件を検証・保存しました。",
        )
        progress_jobs.finish(
            job_id,
            message=f"Yahoo!アクセス解析CSVを{len(all_files)}件保存しました。",
            result={
                "file_count": len(all_files),
                "row_count": sum(item.row_count for item in all_files),
                "sha8": [item.source_sha256[:8] for item in all_files],
                "warnings": list(result.warnings),
            },
        )

    job_id = progress_jobs.start(
        title="Yahoo! アクセス解析取得",
        steps=[
            ("login_check", "ログイン確認"),
            ("fetch_product", "商品分析CSVの取得"),
            ("fetch_overall", "全体分析4件の取得"),
            ("validate_save", "検証・保存"),
        ],
        worker=worker,
        workflow="access_analytics_yahoo",
        metadata={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "browser_mode": browser_mode,
        },
    )
    return {"job_id": job_id}


@app.post("/access-analytics/rakuten")
async def access_analytics_rakuten_fallback(request: Request):
    started = await access_analytics_rakuten_start(request)
    if isinstance(started, Response):
        return started
    return RedirectResponse(url="/access-analytics", status_code=303)


@app.post("/access-analytics/yahoo")
async def access_analytics_yahoo_fallback(request: Request):
    started = await access_analytics_yahoo_start(request)
    if isinstance(started, Response):
        return started
    return RedirectResponse(url="/access-analytics?tab=yahoo", status_code=303)


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    active_tab = _billing_tab(request)
    return templates.TemplateResponse(
        "billing.html",
        {
            "request": request,
            "active_tab": active_tab,
            "default_target_month": _default_target_month(),
            "defer_preview": True,
            "result": None,
            "error": None,
        },
    )


@app.get("/billing/preview", response_class=HTMLResponse)
def billing_preview(request: Request):
    active_tab = _billing_tab(request)
    try:
        result = read_billing_preview(active_tab)
        preview_error = None
    except Exception as exc:
        result = None
        preview_error = str(exc)
    return templates.TemplateResponse(
        "_billing_results.html",
        {
            "request": request,
            "active_tab": active_tab,
            "result": result,
            "preview_error": preview_error,
        },
    )


@app.post("/billing/yahoo/start")
async def billing_yahoo_start(request: Request):
    values = await _read_form_values(request)
    form = {key: entries[-1] if entries else "" for key, entries in values.items()}
    target_month = form.get("target_month", "").strip() or _default_target_month()
    try:
        parsed_month = date.fromisoformat(target_month + "-01")
    except ValueError:
        return PlainTextResponse("対象年月は YYYY-MM で指定してください。", status_code=400)
    if parsed_month.strftime("%Y-%m") != target_month:
        return PlainTextResponse("対象年月は YYYY-MM で指定してください。", status_code=400)
    allowed_types = {"settlement", "billing", "receipt"}
    raw_types = [value for value in values.get("types", []) if value]
    invalid_types = sorted(set(raw_types) - allowed_types)
    if invalid_types:
        return PlainTextResponse("帳票種別の指定が不正です。", status_code=400)
    requested_types = tuple(
        value for value in raw_types if value in allowed_types
    )
    if not requested_types:
        requested_types = ("billing", "receipt", "settlement")
    final_only = _form_bool(form, "final_only")

    def worker(job_id: str) -> None:
        active_keys = ("login_check", "select_month", "fetch")
        for key in active_keys:
            progress_jobs.update_step(job_id, key, status="running")
        try:
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
            result = download_yahoo_statements_sync(
                execute=True,
                target_month=target_month,
                types=requested_types,
                final_only=final_only,
            )
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
        except Exception:
            _mark_steps_failed(job_id, active_keys)
            raise
        for key in active_keys:
            progress_jobs.update_step(job_id, key, status="completed")
        progress_jobs.update_step(job_id, "validate_save", status="running")
        progress_jobs.update_step(
            job_id,
            "validate_save",
            status="completed",
            detail=f"{len(result.files)}件を検証・保存しました。",
        )
        progress_jobs.finish(
            job_id,
            message=(
                f"Yahoo!請求関連CSVを{len(result.files)}件保存しました。"
                if result.files
                else "対象帳票は未確定またはデータなしのため保存していません。"
            ),
            result={
                "file_count": len(result.files),
                "row_count": sum(item.row_count for item in result.files),
                "states": sorted({item.statement_state for item in result.files}),
                "sha8": [item.source_sha256[:8] for item in result.files],
                "warnings": list(result.warnings),
            },
        )

    job_id = progress_jobs.start(
        title="Yahoo! 請求関連取得",
        steps=[
            ("login_check", "ログイン確認"),
            ("select_month", "対象月選択・確定状態判定"),
            ("fetch", "帳票ダウンロード"),
            ("validate_save", "検証・保存"),
        ],
        worker=worker,
        workflow="billing_statements_yahoo",
        metadata={
            "target_month": target_month,
            "types": list(requested_types),
            "final_only": final_only,
        },
    )
    return {"job_id": job_id}


@app.post("/billing/rakuten/start")
async def billing_rakuten_start(request: Request):
    form = await _read_form(request)
    screen = form.get("screen", "settlement_result")
    scope = form.get("scope", "latest")
    issue_date = form.get("issue_date", "").strip() or None
    if screen not in {"settlement_result", "billing_check"}:
        return PlainTextResponse("画面の指定が不正です。", status_code=400)
    if scope not in {"latest", "date", "all"}:
        return PlainTextResponse("取得範囲の指定が不正です。", status_code=400)
    if scope == "date" and not issue_date:
        return PlainTextResponse("発行日を指定してください。", status_code=400)
    if issue_date:
        try:
            parsed_issue_date = date.fromisoformat(issue_date)
        except ValueError:
            return PlainTextResponse("発行日は YYYY-MM-DD で指定してください。", status_code=400)
        if parsed_issue_date.isoformat() != issue_date:
            return PlainTextResponse("発行日は YYYY-MM-DD で指定してください。", status_code=400)

    def worker(job_id: str) -> None:
        active_keys = ("login_check", "list_settlements", "fetch")
        for key in active_keys:
            progress_jobs.update_step(job_id, key, status="running")
        try:
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
            result = download_billpay_settlement_sync(
                execute=True,
                screen=screen,
                scope=scope,
                issue_date=issue_date,
            )
            if progress_jobs.is_cancel_requested(job_id):
                raise JobCancelled()
        except Exception:
            _mark_steps_failed(job_id, active_keys)
            raise
        for key in active_keys:
            progress_jobs.update_step(job_id, key, status="completed")
        progress_jobs.update_step(job_id, "validate_save", status="running")
        progress_jobs.update_step(
            job_id,
            "validate_save",
            status="completed",
            detail=f"{len(result.documents)}件を検証・保存しました。",
        )
        progress_jobs.finish(
            job_id,
            message=f"楽天BillPay帳票を{len(result.documents)}件保存しました。",
            result={
                "document_count": len(result.documents),
                "document_types": sorted(
                    {item.document_type for item in result.documents}
                ),
                "validated_count": sum(
                    1 for item in result.documents if item.validated
                ),
                "sha8": [item.source_sha256[:8] for item in result.documents],
                "warnings": list(result.warnings),
            },
        )

    job_id = progress_jobs.start(
        title="楽天BillPay 請求関連取得",
        steps=[
            ("login_check", "ログイン確認"),
            ("list_settlements", "18か月表示・精算回列挙"),
            ("fetch", "帳票ダウンロード"),
            ("validate_save", "検証・保存"),
        ],
        worker=worker,
        workflow="billing_statements_rakuten",
        metadata={
            "screen": screen,
            "scope": scope,
            "issue_date": issue_date,
        },
    )
    return {"job_id": job_id}


@app.post("/billing/yahoo")
async def billing_yahoo_fallback(request: Request):
    started = await billing_yahoo_start(request)
    if isinstance(started, Response):
        return started
    return RedirectResponse(url="/billing", status_code=303)


@app.post("/billing/rakuten")
async def billing_rakuten_fallback(request: Request):
    started = await billing_rakuten_start(request)
    if isinstance(started, Response):
        return started
    return RedirectResponse(url="/billing?tab=rakuten", status_code=303)


@app.get("/access-analytics/download/{mall}/{artifact_id}")
def access_analytics_download(mall: str, artifact_id: str):
    if mall not in {"rakuten", "yahoo"}:
        return PlainTextResponse("取得先の指定が不正です。", status_code=404)
    if artifact_id == "all":
        return _zip_artifacts(
            latest_access_analytics_artifact_paths(mall),
            download_name=f"access_analytics_{mall}_latest.zip",
        )
    belongs = any(
        record.get("artifact_id") == artifact_id and record.get("mall") == mall
        for record in read_access_analytics_manifest()
    )
    path = resolve_access_analytics_artifact_path(artifact_id) if belongs else None
    if path is None:
        return PlainTextResponse("ファイルが見つかりません。", status_code=404)
    return FileResponse(path, filename=path.name)


@app.get("/billing/download/{mall}/{artifact_id}")
def billing_download(mall: str, artifact_id: str):
    if mall not in {"rakuten", "yahoo"}:
        return PlainTextResponse("取得先の指定が不正です。", status_code=404)
    if artifact_id == "all":
        return _zip_artifacts(
            latest_billing_artifact_paths(mall),
            download_name=f"billing_{mall}_latest.zip",
        )
    belongs = any(
        record.get("artifact_id") == artifact_id and record.get("mall") == mall
        for record in read_billing_manifest()
    )
    path = resolve_billing_artifact_path(artifact_id) if belongs else None
    if path is None:
        return PlainTextResponse("ファイルが見つかりません。", status_code=404)
    return FileResponse(path, filename=path.name)


def _log_relpath(raw: str | None) -> str | None:
    """絶対パスを logs/ 相対に変換する（/logs/view へのリンク用）。logs/ 外なら None。"""
    if not raw:
        return None
    try:
        return Path(raw).resolve().relative_to(LOGS_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return None


@app.get("/jobs", response_class=HTMLResponse)
def jobs_history(request: Request):
    # ジョブ実行履歴（logs/jobs/history.jsonl）を新しい順に一覧する。
    # ファイル無し・壊れ行は read_job_history が吸収して空/スキップになる。
    records = read_job_history(limit=200)
    rows = []
    for record in records:
        events_rel = _log_relpath(record.get("log_events_path"))  # type: ignore[arg-type]
        rows.append(
            {
                "finished_at": record.get("finished_at") or "-",
                "title": record.get("title") or "-",
                "workflow": record.get("workflow") or "-",
                "status": record.get("status") or "-",
                "duration_sec": record.get("duration_sec"),
                "error": record.get("error"),
                "events_url": f"/logs/view?path={quote(events_rel)}" if events_rel else None,
            }
        )
    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "rows": rows,
        },
    )


def _viewable_log_file(path: Path) -> bool:
    """/logs の「表示」対象にできるテキスト形式か。

    ローテーション世代（portal-run-<PC>.log.1 等、S1 で発生）も表示できるよう、
    拡張子セットに加えて「.log.<数字>」も許可する。
    """
    if path.suffix.lower() in LOG_VIEW_TEXT_SUFFIXES:
        return True
    suffixes = [suffix.lower() for suffix in path.suffixes]
    return (
        len(suffixes) >= 2
        and suffixes[-2] == ".log"
        and suffixes[-1].lstrip(".").isdigit()
    )


def _resolve_log_path(raw: str, source: str = "repo") -> Path | None:
    """?path= で受けた相対パスを許可ルート配下に正規化する。

    許可ルートは 2 つ（U2）: source=repo → リポジトリ内 logs/、
    source=shared → 共有ログフォルダ（RUNTIME_LOG_DIR、SharePoint 側）。
    resolve() 後にルート配下であることを検証し、
    `..%2F.env` のようなパストラバーサルや絶対パス指定を拒否する（None を返す）。
    """
    if not raw:
        return None
    root = RUNTIME_LOG_DIR if source == "shared" else LOGS_ROOT
    candidate = Path(raw)
    if candidate.is_absolute():
        return None
    try:
        root_resolved = root.resolve()
        resolved = (root / candidate).resolve()
    except OSError:
        return None
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        return None
    return resolved


def _shared_log_entries() -> list[dict[str, object]]:
    """共有ログ（RUNTIME_LOG_DIR。SharePoint 側の portal-run/error）の一覧を作る（U2）。

    fallback で RUNTIME_LOG_DIR がリポジトリ内 logs/ を指す場合は
    rglob 側と二重表示になるため出さない。一覧の失敗は握りつぶして空
    （共有フォルダの不調で /logs 自体を壊さない）。
    """
    entries: list[dict[str, object]] = []
    try:
        if not RUNTIME_LOG_DIR.is_dir() or RUNTIME_LOG_DIR.resolve() == LOGS_ROOT.resolve():
            return entries
        for path in RUNTIME_LOG_DIR.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "rel": path.name,
                    "scope": "共有",
                    "mtime": stat.st_mtime,
                    "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "size": stat.st_size,
                    "viewable": _viewable_log_file(path),
                    "view_url": f"/logs/view?path={quote(path.name)}&source=shared",
                }
            )
    except OSError:
        return entries
    return entries


@app.get("/logs", response_class=HTMLResponse)
def logs_index(request: Request):
    # logs/ 配下のファイルを更新日時の新しい順に一覧する（エラー調査の入口）。
    # 共有ログ（SharePoint 側の portal-run/error、U2）も「共有」ラベル付きで同じ一覧に出す。
    entries: list[dict[str, object]] = []
    if LOGS_ROOT.is_dir():
        for path in LOGS_ROOT.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(LOGS_ROOT).as_posix()
            entries.append(
                {
                    "rel": rel,
                    "scope": "アプリ内",
                    "mtime": stat.st_mtime,
                    "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "size": stat.st_size,
                    "viewable": _viewable_log_file(path),
                    "view_url": f"/logs/view?path={quote(rel)}",
                }
            )
    entries.extend(_shared_log_entries())
    entries.sort(key=lambda entry: entry["mtime"], reverse=True)
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "entries": entries[:200],
            "total_count": len(entries),
        },
    )


@app.get("/logs/view", response_class=HTMLResponse)
def logs_view(request: Request, path: str = "", source: str = "repo"):
    if source not in {"repo", "shared"}:
        source = "repo"
    resolved = _resolve_log_path(path, source)
    if resolved is None:
        return PlainTextResponse("不正なパスです。ログフォルダ配下の相対パスのみ指定できます。", status_code=400)
    if not resolved.is_file():
        return PlainTextResponse("ログファイルが見つかりません。", status_code=404)
    if not _viewable_log_file(resolved):
        return PlainTextResponse("テキスト形式のログのみ表示できます。", status_code=400)

    try:
        stat = resolved.stat()
        with resolved.open("rb") as handle:
            raw = handle.read(LOG_VIEW_MAX_BYTES)
    except OSError as exc:
        return PlainTextResponse(f"ログの読み込みに失敗しました: {exc}", status_code=500)

    truncated = stat.st_size > LOG_VIEW_MAX_BYTES
    if source == "shared":
        display_path = f"共有ログ/{resolved.relative_to(RUNTIME_LOG_DIR.resolve()).as_posix()}"
    else:
        display_path = f"logs/{resolved.relative_to(LOGS_ROOT.resolve()).as_posix()}"
    # 表示はテンプレート側の autoescape に任せる（HTMLログも生埋め込みしない）。
    content = raw.decode("utf-8", errors="replace")
    return templates.TemplateResponse(
        "log_view.html",
        {
            "request": request,
            "display_path": display_path,
            "content": content,
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "truncated": truncated,
            "max_bytes": LOG_VIEW_MAX_BYTES,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """設定画面（U6・閲覧のみ）: .env の見える化。

    README の設定キー群の設定状況（PASSWORD / LOGIN_ID 等の秘密は先頭2文字＋***で
    マスク）と、パス解決・ログ出力先・タイムアウトの実効値・稼働バージョンを表示する。
    doctor.py をコマンドラインで実行しなくても「動かない原因が設定かどうか」を
    画面だけで切り分けられるようにする。値の編集は従来どおり `.env`＋再起動。
    """
    # パス解決の結果（未解決でも 500 にせず、エラー文と候補を表示する）
    resolution: dict[str, object]
    try:
        paths = find_portal_paths()
        resolution = {
            "ok": True,
            "portal_root": str(paths.portal_root),
            "master_book": str(paths.master_book),
            "order_csv_dir": str(paths.order_csv_dir),
        }
    except Exception as exc:
        resolution = {
            "ok": False,
            "error": str(exc),
            "candidates": [str(path) for path in candidate_portal_roots()],
        }

    runtime = {
        "version": APP_VERSION,
        "started_at": APP_STARTED_AT,
        "log_dir": str(RUNTIME_LOG_DIR),
        "run_log_name": run_log_file_name(),
        "error_log_name": error_log_file_name(),
        "nav_timeout_ms": nav_timeout_ms(),
        "download_timeout_ms": download_timeout_ms(),
        "allowed_clients": ", ".join(allowed_client_rules()) or "未設定（すべて許可）",
    }
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "groups": settings_snapshot(),
            "resolution": resolution,
            "runtime": runtime,
        },
    )


@app.get("/health")
def health():
    # 死活監視＋稼働バージョン確認（S4）。version（起動時に確定）と head_on_disk
    # （毎回取得）の乖離は「update 済みだが再起動忘れ」を意味する。
    head_on_disk = _git_short_head()
    return {
        "status": "ok",
        "version": APP_VERSION,
        "started_at": APP_STARTED_AT,
        "head_on_disk": head_on_disk,
        "restart_required": (
            APP_VERSION != "unknown" and head_on_disk != "unknown" and APP_VERSION != head_on_disk
        ),
    }
