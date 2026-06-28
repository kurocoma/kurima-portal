from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from portal_app.env import load_env_file  # noqa: E402
from portal_app.services.next_engine_downloader import _chromium_launch_options  # noqa: E402
from portal_app.services.yamato_b2_import import (  # noqa: E402
    LOGIN_ID_ENV,
    PASSWORD_ENV,
    _enter_b2_cloud,
    _login_to_b2,
    _storage_state_path,
)


async def main() -> None:
    load_env_file()
    login_id = os.environ.get(LOGIN_ID_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "").strip()
    if not login_id or not password:
        raise RuntimeError("YAMATO_B2_LOGIN_ID/YAMATO_B2_PASSWORD is not configured.")

    warnings: list[str] = []
    storage_state = _storage_state_path()
    storage_state.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_chromium_launch_options(False, 150))
        context_kwargs: dict[str, object] = {
            "accept_downloads": True,
            "locale": "ja-JP",
            "viewport": {"width": 1366, "height": 900},
        }
        if storage_state.exists():
            context_kwargs["storage_state"] = str(storage_state)

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        try:
            await _login_to_b2(page, login_id=login_id, password=password, warnings=warnings)
            page = await _enter_b2_cloud(page, warnings=warnings)
            await page.bring_to_front()
            await context.storage_state(path=str(storage_state))

            print(f"READY title={await page.title()}", flush=True)
            print(f"READY url={page.url}", flush=True)
            if warnings:
                print("WARN " + " | ".join(warnings), flush=True)

            await page.wait_for_timeout(60 * 60 * 1000)
        except Exception as exc:
            print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
            try:
                print(f"ERROR url={page.url}", flush=True)
                print(f"ERROR title={await page.title()}", flush=True)
            except Exception:
                pass
            await page.wait_for_timeout(10 * 60 * 1000)
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
