"""失敗時の画面記録（page_diagnostics）と共有ログ強化のテスト。

2026-10-01 の障害調査で不足していた情報を残せることを検証する:
- 画面の URL・タイトル・本文冒頭・スクリーンショット・HTML が保存されること
- 画面が既に閉じていても例外にならないこと（ベストエフォート）
- 保存フォルダが上限件数で刈り込まれること
- job_created の metadata（対象画面・取得範囲）が共有ログへ出ること
- 例外の state と add_note した診断が共有エラーログの payload に入ること
実ブラウザ・実共有フォルダには触れない（FakePage と一時フォルダ）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import execution_logger, page_diagnostics


class FakePage:
    def __init__(self, *, closed: bool = False) -> None:
        self.url = "https://example.test/consent?token=abc#x"
        self._closed = closed

    def _check(self) -> None:
        if self._closed:
            raise RuntimeError("Target page, context or browser has been closed")

    async def title(self) -> str:
        self._check()
        return "利用規約の同意"

    async def evaluate(self, expression: str) -> str:
        self._check()
        return "利用規約が\n改定されました。  同意する"

    async def screenshot(self, *, path: str, full_page: bool = False) -> None:
        self._check()
        Path(path).write_bytes(b"png")

    async def content(self) -> str:
        self._check()
        return "<html><body>同意する</body></html>"


class _Logger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message, *args) -> None:
        self.lines.append(("info", message % args if args else message))

    def warning(self, message, *args) -> None:
        self.lines.append(("warning", message % args if args else message))

    def error(self, message, *args) -> None:
        self.lines.append(("error", message % args if args else message))


class CapturePageDiagnosticsTest(unittest.TestCase):
    def _capture(self, page, directory: Path, logger: _Logger) -> dict[str, str | None]:
        with (
            mock.patch.object(page_diagnostics, "diagnostics_dir", lambda: directory),
            mock.patch("portal_app.log_paths.get_portal_logger", lambda: logger),
        ):
            return asyncio.run(
                page_diagnostics.capture_page_diagnostics(
                    page, workflow="clickpost", label="login waiting"
                )
            )

    def test_saves_screen_and_logs_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = _Logger()
            result = self._capture(FakePage(), Path(tmp), logger)
            self.assertEqual(result["url"], "https://example.test/consent")  # クエリは落とす
            self.assertEqual(result["title"], "利用規約の同意")
            self.assertEqual(result["excerpt"], "利用規約が 改定されました。 同意する")
            self.assertTrue(Path(result["screenshot"]).is_file())
            self.assertTrue(Path(result["html"]).is_file())
            self.assertIn("clickpost_login_waiting", Path(result["screenshot"]).name)
            self.assertEqual(len(logger.lines), 1)
            level, line = logger.lines[0]
            self.assertEqual(level, "warning")
            self.assertIn("event=page_diagnostics", line)
            self.assertIn("title=利用規約の同意", line)

    def test_closed_page_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._capture(FakePage(closed=True), Path(tmp), _Logger())
            self.assertEqual(result["url"], "https://example.test/consent")
            self.assertIsNone(result["title"])
            self.assertIsNone(result["screenshot"])
            self.assertIsNone(result["html"])

    def test_none_page_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._capture(None, Path(tmp), _Logger())
            self.assertTrue(all(value is None for value in result.values()))

    def test_prunes_oldest_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(10):
                item = directory / f"old_{index}.png"
                item.write_bytes(b"x")
                os.utime(item, (1_000_000 + index, 1_000_000 + index))
            page_diagnostics._prune(directory, keep=4)
            self.assertEqual(
                sorted(item.name for item in directory.iterdir()),
                ["old_6.png", "old_7.png", "old_8.png", "old_9.png"],
            )


class SharedLogEnrichmentTest(unittest.TestCase):
    def test_job_created_metadata_is_mirrored(self) -> None:
        logger = _Logger()
        with mock.patch("portal_app.log_paths.get_portal_logger", lambda: logger):
            execution_logger._mirror_to_portal_log(
                {
                    "workflow": "billing_statements_rakuten",
                    "run_id": "r1",
                    "event": "job_created",
                    "status": "queued",
                    "level": "info",
                    "data": {"metadata": {"screen": "billing_check", "scope": "latest"}},
                }
            )
            execution_logger._mirror_to_portal_log(
                {
                    "workflow": "w",
                    "run_id": "r1",
                    "event": "job_started",
                    "level": "info",
                    "data": {},
                }
            )
        self.assertIn('metadata={"screen": "billing_check", "scope": "latest"}', logger.lines[0][1])
        self.assertNotIn("metadata=", logger.lines[1][1])

    def test_exception_payload_keeps_state_and_notes(self) -> None:
        class StateError(RuntimeError):
            state = "PAGE_CONTRACT_CHANGED"

        try:
            try:
                raise StateError("展開完了を確認できません。")
            except StateError as exc:
                exc.add_note("BillPay診断: screen=billing_check")
                raise
        except StateError as exc:
            payload = execution_logger.exception_payload(exc)
        self.assertEqual(payload["state"], "PAGE_CONTRACT_CHANGED")
        self.assertIn("BillPay診断: screen=billing_check", payload["traceback"])


if __name__ == "__main__":
    unittest.main()
