"""ブラウザ自動操作が止まった・失敗した時点の画面を、共有ログフォルダへ残す。

2026-10-01 の障害調査（BillPay展開失敗・クリックポストのログイン停止）で、
「そのとき何の画面だったか」が共有ログから分からず原因特定に時間がかかった。
スクリーンショット・HTML はそれまで各端末のローカル logs/ にしか無く、
別の端末からは読めなかったため、共有ログと同じフォルダ配下へ保存する。

- 保存先: <共有ログフォルダ>/failure_screens-<PC名>/<日時>_<workflow>_<label>.{png,html}
- 共有ログ（portal-run-<PC名>.log）にも URL・タイトル・本文冒頭・保存先を1行残す
- すべてベストエフォート。ここでの失敗でジョブ本体を壊さない
  （閉じた画面・同期フォルダのロックなど、何が起きても呼び出し元の例外を上書きしない）
"""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path

# フォルダ内に残す最大ファイル数（png/html 合計）。超えた分は古い順に削除する。
MAX_FILES = 60
EXCERPT_CHARS = 300


def diagnostics_dir() -> Path:
    from portal_app.log_paths import log_file_suffix, resolve_log_dir

    return resolve_log_dir() / f"failure_screens-{log_file_suffix()}"


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "x"


def _prune(directory: Path, *, keep: int) -> None:
    try:
        files = sorted(
            (item for item in directory.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        with suppress(OSError):
            stale.unlink()


async def _save_files(page, result: dict[str, str | None], *, workflow: str, label: str) -> None:
    directory = diagnostics_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    stem = f"{stamp}_{_safe(workflow)}_{_safe(label)}"
    with suppress(Exception):
        screenshot = directory / f"{stem}.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        result["screenshot"] = str(screenshot)
    with suppress(Exception):
        html = directory / f"{stem}.html"
        html.write_text(await page.content(), encoding="utf-8")
        result["html"] = str(html)
    _prune(directory, keep=MAX_FILES)


def _log(result: dict[str, str | None], *, workflow: str, label: str) -> None:
    from portal_app.log_paths import get_portal_logger

    get_portal_logger().warning(
        "workflow=%s event=page_diagnostics label=%s %s",
        workflow,
        label,
        format_diagnostics(result),
    )


async def capture_page_diagnostics(page, *, workflow: str, label: str) -> dict[str, str | None]:
    """現在の画面の URL・タイトル・本文冒頭・スクリーンショット・HTML を記録して返す。"""

    result: dict[str, str | None] = {
        "url": None,
        "title": None,
        "excerpt": None,
        "screenshot": None,
        "html": None,
    }
    if page is None:
        return result
    with suppress(Exception):
        result["url"] = re.sub(r"[?#].*", "", str(page.url))
    with suppress(Exception):
        result["title"] = await page.title()
    with suppress(Exception):
        text = await page.evaluate("() => (document.body && document.body.innerText || '')")
        result["excerpt"] = re.sub(r"\s+", " ", str(text)).strip()[:EXCERPT_CHARS]
    with suppress(Exception):
        await _save_files(page, result, workflow=workflow, label=label)
    with suppress(Exception):
        _log(result, workflow=workflow, label=label)
    return result


def format_diagnostics(result: dict[str, str | None]) -> str:
    return " ".join(
        f"{key}={result.get(key)}" for key in ("url", "title", "screenshot", "html", "excerpt")
    )
