from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, quote

from portal_app.env import load_env_file

load_env_file()

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
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
from portal_app.services.inventory_pdf import inventory_result_to_pdf
from portal_app.services.letterpack_pdf import create_letterpack_label_pdf
from portal_app.services.ne02_order_details import download_ne02_order_details_sync
from portal_app.services.next_engine_downloader import download_next_engine_order_details
from portal_app.services.next_engine_order_status import restore_next_engine_print_wait_batch_sync
from portal_app.services.paths import candidate_portal_roots, find_portal_paths
from portal_app.services.progress_jobs import progress_jobs
from portal_app.services.shipment_confirmation import confirm_next_engine_shipment_sync
from portal_app.services.takaesu_orders import prepare_takaesu_order_workflow_sync
from portal_app.services.yamato_b2_import import import_yamato_b2_csv
from portal_app.services.yamato_b2_workflow import prepare_yamato_b2_sync
from portal_app.services.yamato_conversion import (
    create_ne_to_yamato_csv,
    preview_ne_to_yamato_conversion,
)

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="くりまポータルツール")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


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


def _form_int(form: dict[str, str], key: str, default: int) -> int:
    try:
        return int(form.get(key, "") or default)
    except ValueError:
        return default


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
    conversion = getattr(result, "conversion", None)
    letterpack = getattr(result, "letterpack", None)
    return {
        "buyer_file": _path_text(getattr(buyer, "downloaded_file", None)),
        "buyer_count": getattr(getattr(buyer, "snapshot", None), "count", None),
        "product_file": _path_text(getattr(product, "downloaded_file", None)),
        "product_count": getattr(getattr(product, "snapshot", None), "count", None),
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
    try:
        paths = find_portal_paths()
        status = {
            "ok": True,
            "portal_root": paths.portal_root,
            "master_book": paths.master_book,
            "order_csv_dir": paths.order_csv_dir,
        }
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
        },
    )


@app.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request):
    try:
        result = analyze_latest_inventory()
        return templates.TemplateResponse(
            "inventory.html",
            {
                "request": request,
                "result": result,
                "error": None,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "inventory.html",
            {
                "request": request,
                "result": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.post("/inventory/fetch-next-engine", response_class=HTMLResponse)
async def inventory_fetch_next_engine(request: Request):
    try:
        download_result = await download_next_engine_order_details()
        result = analyze_latest_inventory()
        return templates.TemplateResponse(
            "inventory.html",
            {
                "request": request,
                "result": result,
                "download_result": download_result,
                "error": None,
            },
        )
    except Exception as exc:
        current_result = None
        try:
            current_result = analyze_latest_inventory()
        except Exception:
            pass
        return templates.TemplateResponse(
            "inventory.html",
            {
                "request": request,
                "result": current_result,
                "download_result": None,
                "error": str(exc),
            },
            status_code=500,
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
    try:
        result = preview_ne_to_yamato_conversion(preview_limit=30)
        return _yamato_response(request, result=result)
    except Exception as exc:
        return _yamato_response(request, result=None, error=str(exc), status_code=500)


def _yamato_response(
    request: Request,
    *,
    result,
    message: str | None = None,
    error: str | None = None,
    prepare_result=None,
    restore_result=None,
    b2_import_result=None,
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
            "restore_order_nos_text": "\n".join(
                _yamato_restore_order_numbers_from_prepare_result(prepare_result)
            ),
        },
        status_code=status_code,
    )


@app.get("/clickpost", response_class=HTMLResponse)
def clickpost_delivery(request: Request):
    try:
        result = preview_clickpost_csv(preview_limit=30)
        return templates.TemplateResponse(
            "clickpost.html",
            {
                "request": request,
                "result": result,
                "prepare_result": None,
                "upload_result": None,
                "letterpack_pdf_result": None,
                "browser_mode": "background",
                "slow_mo_ms": 150,
                "message": None,
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
                "browser_mode": "background",
                "slow_mo_ms": 150,
                "message": None,
                "error": str(exc),
            },
            status_code=500,
        )


@app.get("/progress/{job_id}")
def progress_status(job_id: str):
    snapshot = progress_jobs.snapshot(job_id)
    if snapshot is None:
        return Response(status_code=404)
    return snapshot


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
                "message": "Next Engine から購入者データと受注明細データを取得し、clickpostimport.csv と letterpack_addressbook.csv を作成しました。",
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
            progress_callback=update_progress,
        )
        progress_jobs.finish(
            job_id,
            message="NE取得とCSV作成が完了しました。",
            result={"prepare": _clickpost_prepare_summary(prepare_result)},
        )

    job_id = progress_jobs.start(
        title="クリックポスト NE取得＋CSV作成",
        steps=[
            ("buyer_download", "NE購入者データ取得"),
            ("product_download", "NE受注明細データ取得"),
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
            "NE取得、レターパックPDF作成、クリックポスト本番処理が完了しました。"
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
    headed = _form_bool(form, "headed")
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


@app.post("/yamato/restore-print-wait", response_class=HTMLResponse)
async def yamato_restore_print_wait(request: Request):
    form = await _read_form(request)
    order_numbers = _parse_order_numbers(form.get("order_nos"))
    headed = _form_bool(form, "headed")
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


@app.get("/health")
def health():
    return {"status": "ok"}
