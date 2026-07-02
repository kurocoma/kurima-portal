from __future__ import annotations

import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote

from portal_app.env import load_env_file

load_env_file()

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

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
from portal_app.services.ne02_order_details import download_ne02_order_details_sync
from portal_app.services.next_engine_downloader import (
    download_next_engine_order_details,
    download_next_engine_order_details_sync,
)
from portal_app.services.next_engine_order_status import restore_next_engine_print_wait_batch_sync
from portal_app.services.paths import candidate_portal_roots, find_portal_paths, latest_order_csv
from portal_app.services.progress_jobs import JobCancelled, progress_jobs, read_job_history
from portal_app.services.shipment_confirmation import confirm_next_engine_shipment_sync
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
from portal_app.services.yamato_conversion import (
    create_ne_to_yamato_csv,
    preview_ne_to_yamato_conversion,
)

APP_DIR = Path(__file__).resolve().parent
LOGS_ROOT = APP_DIR.parent / "logs"
LOG_VIEW_MAX_BYTES = 200_000
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
INVENTORY_TABS = {"normal", "takaesu"}


def _parse_order_numbers(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return tuple()
    return tuple(value for value in raw.replace(",", " ").split() if value)


async def _read_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


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
    if kind not in {"normal", "choice"}:
        return PlainTextResponse("kind must be normal or choice", status_code=400)

    result = analyze_latest_inventory()
    pdf_bytes = inventory_result_to_pdf(result, "normal" if kind == "normal" else "choice")
    filename_suffix = "normal" if kind == "normal" else "choice"
    filename = f"inventory_{filename_suffix}_{result.generated_at:%Y%m%d_%H%M%S}.pdf"
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
            "b2_csv_path": _b2_csv_path_text(),
            "restore_order_nos_text": "\n".join(
                _yamato_restore_order_numbers_from_prepare_result(prepare_result)
            ),
        },
        status_code=status_code,
    )


def _b2_csv_path_text() -> str | None:
    csv_path = resolve_default_b2_csv()
    return str(csv_path) if csv_path else None


@app.get("/yamato/preview", response_class=HTMLResponse)
def yamato_preview(request: Request):
    # GET /yamato から遅延ロードされる結果fragment。
    # 重いプレビュー（商品マスタ読込）はここで実行する。
    try:
        result = preview_ne_to_yamato_conversion(preview_limit=30)
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


@app.get("/shipment-confirmation")
def shipment_confirmation_status():
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
            restore_invoices_after_download=True,
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
                "message": "Next Engine から購入者データ、受注明細データ、納品書PDFを取得し、clickpostimport.csv と letterpack_addressbook.csv を作成しました。",
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
            restore_invoices_after_download=True,
            progress_callback=update_progress,
        )
        progress_jobs.finish(
            job_id,
            message="NE取得、納品書PDF取得、CSV作成が完了しました。",
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
    selected = _yamato_selected_steps(form)
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    csv_file_raw = form.get("csv_file", "").strip()
    headed = _form_headed(form)
    verify_invoice_statuses = _form_bool(form, "verify_invoice_statuses")
    confirm_import = _form_bool(form, "confirm_import") or form.get("import_mode", "execute") == "execute"
    slow_mo_ms = _form_int(form, "slow_mo_ms", 150)
    preview_limit = _form_int(form, "preview_limit", 30)

    prepare_result = None
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
            )
            current_result = prepare_result.conversion

        if selected["b2_login"] or selected["b2_import"]:
            b2_import_result = await import_yamato_b2_csv(
                csv_file=Path(csv_file_raw) if csv_file_raw else None,
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
            current_result = preview_ne_to_yamato_conversion(preview_limit=preview_limit)

        message = "選択した処理を実行しました。"
        if selected["b2_import"] and not confirm_import:
            message += " B2インポートは確認なしのためCSV選択までです。"

        return _yamato_response(
            request,
            result=current_result,
            prepare_result=prepare_result,
            b2_import_result=b2_import_result,
            message=message,
        )
    except Exception as exc:
        if current_result is None:
            try:
                current_result = preview_ne_to_yamato_conversion(preview_limit=30)
            except Exception:
                pass
        return _yamato_response(
            request,
            result=current_result,
            prepare_result=prepare_result,
            b2_import_result=b2_import_result,
            error=str(exc),
            status_code=500,
        )


@app.post("/yamato/run-selected/start")
async def yamato_run_selected_start(request: Request):
    # run-selected の非同期版。進捗をフローチップに同期表示するため、
    # 重い処理をバックグラウンドジョブ化し、フロントは /progress をポーリングする。
    # ステップは粗い2段階: prepare(受注取得〜CSV作成=チップ①〜⑤) / b2_import(B2取込=チップ⑥)。
    form = await _read_form(request)
    selected = _yamato_selected_steps(form)
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    csv_file_raw = form.get("csv_file", "").strip()
    headed = _form_headed(form)
    verify_invoice_statuses = _form_bool(form, "verify_invoice_statuses")
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
    if do_b2:
        steps.append(("b2_import", "ヤマトB2へ取込"))

    def worker(job_id: str) -> None:
        current_result = None
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
            )
            current_result = prepare_result.conversion

        b2_result = None
        if do_b2:
            progress_jobs.update_step(job_id, "b2_import", status="running")
            try:
                b2_result = import_yamato_b2_csv_sync(
                    csv_file=Path(csv_file_raw) if csv_file_raw else None,
                    check_login=selected["b2_login"] and not selected["b2_import"],
                    open_import_page=False,
                    select_file_dry_run=selected["b2_import"] and not confirm_import,
                    execute_import=selected["b2_import"] and confirm_import,
                    confirm_import=confirm_import,
                    headless=not headed,
                    slow_mo_ms=slow_mo_ms,
                    keep_browser_open=headed,
                )
            except Exception:
                progress_jobs.update_step(job_id, "b2_import", status="failed")
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

        result_summary: dict[str, object] = {}
        if current_result is not None:
            result_summary["output_csv"] = _path_text(getattr(current_result, "output_csv", None))
            result_summary["output_rows"] = getattr(current_result, "output_rows", None)
        if b2_result is not None:
            result_summary["b2_import_executed"] = bool(getattr(b2_result, "import_executed", False))
            result_summary["b2_skipped_reason"] = getattr(b2_result, "skipped_reason", None)
        progress_jobs.finish(job_id, message=message, result=result_summary)

    job_id = progress_jobs.start(
        title="ヤマト用CSV作成",
        steps=steps,
        worker=worker,
        workflow="yamato_run_selected",
        metadata={
            "headed": headed,
            "slow_mo_ms": slow_mo_ms,
            "confirm_import": confirm_import,
        },
    )
    return {"job_id": job_id}


@app.post("/yamato/restore-print-wait", response_class=HTMLResponse)
async def yamato_restore_print_wait(request: Request):
    form = await _read_form(request)
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
        current_result = preview_ne_to_yamato_conversion(preview_limit=30)
        return _yamato_response(
            request,
            result=current_result,
            restore_result=restore_result,
            message="納品書印刷待ちへの復旧を実行しました。" if execute else "復旧対象を確認しました。",
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
    automate = _form_bool(form, "automate")
    csv_file_raw = form.get("csv_file", "").strip()
    csv_path = Path(csv_file_raw) if csv_file_raw else resolve_default_b2_csv()
    if csv_path is None or not Path(csv_path).is_file():
        return PlainTextResponse("先にヤマト用CSVを作成してください。", status_code=400)
    csv_path = Path(csv_path)

    steps: list[tuple[str, str]] = [("open", "B2ブラウザを開く")]
    if automate:
        steps.append(("b2_import", "B2へ取込（自動を試行）"))

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
        workflow="yamato_b2_open",
        metadata={"automate": automate},
    )
    return {"job_id": job_id}


@app.post("/yamato/b2-open", response_class=HTMLResponse)
async def yamato_b2_open(request: Request):
    # JS無効時のフォールバック（モードA=手動のみ）。実Chromeを開いて /yamato へ戻る。
    form = await _read_form(request)
    csv_file_raw = form.get("csv_file", "").strip()
    csv_path = Path(csv_file_raw) if csv_file_raw else resolve_default_b2_csv()
    if csv_path is not None and Path(csv_path).is_file():
        try:
            await run_in_threadpool(b2_chrome.launch, csv_path=Path(csv_path), enable_cdp=False)
        except Exception:
            pass
    return RedirectResponse(url="/yamato", status_code=303)


@app.post("/yamato/b2-close", response_class=HTMLResponse)
async def yamato_b2_close(request: Request):
    # 印刷完了後にB2ブラウザを閉じる。
    await run_in_threadpool(b2_chrome.close)
    return RedirectResponse(url="/yamato", status_code=303)


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


def _resolve_log_path(raw: str) -> Path | None:
    """?path= で受けた相対パスを logs/ 配下に正規化する。

    resolve() 後に logs ルート配下であることを検証し、
    `..%2F.env` のようなパストラバーサルや絶対パス指定を拒否する（None を返す）。
    """
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return None
    try:
        logs_root = LOGS_ROOT.resolve()
        resolved = (LOGS_ROOT / candidate).resolve()
    except OSError:
        return None
    if resolved == logs_root or not resolved.is_relative_to(logs_root):
        return None
    return resolved


@app.get("/logs", response_class=HTMLResponse)
def logs_index(request: Request):
    # logs/ 配下のファイルを更新日時の新しい順に一覧する（エラー調査の入口）。
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
                    "mtime": stat.st_mtime,
                    "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "size": stat.st_size,
                    "viewable": path.suffix.lower() in LOG_VIEW_TEXT_SUFFIXES,
                    "view_url": f"/logs/view?path={quote(rel)}",
                }
            )
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
def logs_view(request: Request, path: str = ""):
    resolved = _resolve_log_path(path)
    if resolved is None:
        return PlainTextResponse("不正なパスです。logs/ 配下の相対パスのみ指定できます。", status_code=400)
    if not resolved.is_file():
        return PlainTextResponse("ログファイルが見つかりません。", status_code=404)
    if resolved.suffix.lower() not in LOG_VIEW_TEXT_SUFFIXES:
        return PlainTextResponse("テキスト形式のログのみ表示できます。", status_code=400)

    try:
        stat = resolved.stat()
        with resolved.open("rb") as handle:
            raw = handle.read(LOG_VIEW_MAX_BYTES)
    except OSError as exc:
        return PlainTextResponse(f"ログの読み込みに失敗しました: {exc}", status_code=500)

    truncated = stat.st_size > LOG_VIEW_MAX_BYTES
    # 表示はテンプレート側の autoescape に任せる（HTMLログも生埋め込みしない）。
    content = raw.decode("utf-8", errors="replace")
    return templates.TemplateResponse(
        "log_view.html",
        {
            "request": request,
            "rel_path": resolved.relative_to(LOGS_ROOT.resolve()).as_posix(),
            "content": content,
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "truncated": truncated,
            "max_bytes": LOG_VIEW_MAX_BYTES,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
