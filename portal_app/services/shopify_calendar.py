"""Shopify 営業日カレンダーの更新〜反映。

運用（2026-10 決定）:
- ストアには「今月」と「来月」の2枚を出す（PC=サイドバー2か所 / SP=トップページの営業日カレンダー2か所）。
- テーマは固定名ファイル calendar_current.jpg（今月）/ calendar_next.jpg（来月）を参照する
  （初回だけテーマエディタで付け替え）。以後このモジュールはファイルの中身だけを差し替え、
  テーマには書き込まない。
- 生成画像と入力した臨時休業日は SharePoint 同期フォルダ「Shopifyカレンダー」に残す。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from portal_app.services import shopify_admin
from portal_app.services.calendar_image import (
    CalendarMonth,
    current_and_next,
    format_closed_days,
    render_calendar,
)
from portal_app.services.execution_logger import APP_ROOT
from portal_app.services.paths import candidate_portal_roots

CURRENT_FILENAME = "calendar_current.jpg"
NEXT_FILENAME = "calendar_next.jpg"
FOLDER_NAME = "Shopifyカレンダー"
CLOSED_DAYS_FILE = "臨時休業日.json"
DEFAULT_STOREFRONT_URL = "https://shop.kurima.co.jp/"

_SHOP_IMAGE_RE = re.compile(r"shopify://shop_images/([^\"'\s]+)")
_MONTH_FILE_RE = re.compile(r"^\d{4}_\d{2}\.(jpg|jpeg|png|webp)$", re.IGNORECASE)


# --- 保存先 -------------------------------------------------------------------


def calendar_dir() -> Path:
    """生成画像の保存先。KURIMA_SHOPIFY_CALENDAR_DIR > ポータル同期フォルダ > data/ の順。"""
    explicit = os.environ.get("KURIMA_SHOPIFY_CALENDAR_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for root in candidate_portal_roots():
        if root.is_dir():
            return root / FOLDER_NAME
    return APP_ROOT / "data" / "shopify_calendar"


def image_path(target: CalendarMonth, folder: Path | None = None) -> Path:
    return (folder or calendar_dir()) / f"{target.key}.jpg"


def load_closed_days(folder: Path | None = None) -> dict[str, str]:
    """保存済みの臨時休業日（{"2026_12": "29-31"}）。無い・壊れているときは空。"""
    path = (folder or calendar_dir()) / CLOSED_DAYS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_closed_days(entries: dict[str, tuple[int, ...]], folder: Path | None = None) -> None:
    """入力した臨時休業日を月ごとに上書き保存する（他の月の記録は残す）。"""
    base = folder or calendar_dir()
    base.mkdir(parents=True, exist_ok=True)
    data = load_closed_days(base)
    for key, days in entries.items():
        data[key] = format_closed_days(days)
    (base / CLOSED_DAYS_FILE).write_text(
        json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --- 生成 ---------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarPlan:
    """今月・来月それぞれの (対象月, 臨時休業日, Shopify 上のファイル名)。"""

    items: tuple[tuple[CalendarMonth, tuple[int, ...], str], ...]


def build_plan(
    today: date, closed: dict[str, tuple[int, ...]]
) -> CalendarPlan:
    current, upcoming = current_and_next(today)
    return CalendarPlan(
        items=(
            (current, closed.get(current.key, ()), CURRENT_FILENAME),
            (upcoming, closed.get(upcoming.key, ()), NEXT_FILENAME),
        )
    )


def generate_images(plan: CalendarPlan, folder: Path | None = None) -> list[Path]:
    """画像を作って保存し、臨時休業日も記録する。保存したパスを返す。"""
    base = folder or calendar_dir()
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for target, days, _ in plan.items:
        path = image_path(target, base)
        path.write_bytes(render_calendar(target, days))
        paths.append(path)
    save_closed_days({target.key: days for target, days, _ in plan.items}, base)
    return paths


# --- テーマ参照の確認（読み取りのみ） -------------------------------------------


@dataclass
class ThemeCheck:
    theme_name: str = ""
    current_refs: list[str] = field(default_factory=list)
    next_refs: list[str] = field(default_factory=list)
    month_refs: list[str] = field(default_factory=list)
    width_mismatch: list[str] = field(default_factory=list)

    @property
    def setup_done(self) -> bool:
        """初回の付け替えが済んでいるか（今月・来月とも2か所＝PCサイドバー＋SPトップ）。"""
        return len(self.current_refs) >= 2 and len(self.next_refs) >= 2 and not self.month_refs


def _strip_json_comment(text: str) -> str:
    """Shopify の settings_data.json 先頭の /* ... */ コメントを取り除く。"""
    stripped = text.lstrip("﻿").lstrip()
    if stripped.startswith("/*"):
        end = stripped.find("*/")
        if end != -1:
            stripped = stripped[end + 2 :]
    return stripped


def analyze_theme_settings(content: str, theme_name: str = "") -> ThemeCheck:
    """有効なブロックのうち、カレンダー画像を参照している場所を調べる。"""
    data = json.loads(_strip_json_comment(content))
    current = data.get("current")
    if isinstance(current, str):  # プリセット名で参照している古い形式
        current = (data.get("presets") or {}).get(current, {})
    sections = (current or {}).get("sections") or {}
    check = ThemeCheck(theme_name=theme_name)

    for section_key, section in sections.items():
        if not isinstance(section, dict) or section.get("disabled"):
            continue
        section_title = (section.get("settings") or {}).get("title") or section.get("type", "")
        widths: dict[str, str] = {}
        for block_key, block in (section.get("blocks") or {}).items():
            if not isinstance(block, dict) or block.get("disabled"):
                continue
            settings = block.get("settings") or {}
            image = settings.get("image")
            if not isinstance(image, str):
                continue
            match = _SHOP_IMAGE_RE.fullmatch(image.strip())
            if not match:
                continue
            filename = match.group(1)
            where = f"{section_title}（{section.get('type', section_key)}）"
            if filename == CURRENT_FILENAME:
                check.current_refs.append(where)
            elif filename == NEXT_FILENAME:
                check.next_refs.append(where)
            elif _MONTH_FILE_RE.match(filename):
                check.month_refs.append(f"{where}: {filename}")
            else:
                continue
            if settings.get("width"):
                widths[block_key] = str(settings["width"])
        if len(set(widths.values())) > 1:
            check.width_mismatch.append(
                f"{section_title}: " + " / ".join(sorted(set(widths.values())))
            )
    return check


def check_theme(cfg: shopify_admin.ShopifyConfig) -> ThemeCheck:
    name, content = shopify_admin.read_main_theme_settings(cfg)
    return analyze_theme_settings(content, name)


def check_storefront(url: str | None = None) -> dict[str, int]:
    """公開ストアのトップHTMLに固定名ファイルが何回出てくるか数える（PC/SP 両方の要素が入る）。"""
    target = url or os.environ.get("KURIMA_SHOPIFY_STOREFRONT_URL") or DEFAULT_STOREFRONT_URL
    request = urllib.request.Request(target, headers={"User-Agent": "kurima-portal/calendar-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", "replace")
    stems = {name: name.rsplit(".", 1)[0] for name in (CURRENT_FILENAME, NEXT_FILENAME)}
    return {name: html.count(stem) for name, stem in stems.items()}


# --- 状態表示 -----------------------------------------------------------------


@dataclass
class CalendarStatus:
    configured: bool
    admin_url: str | None = None
    scopes: tuple[str, ...] = ()
    files: dict[str, shopify_admin.StoreFile | None] = field(default_factory=dict)
    theme: ThemeCheck | None = None
    theme_error: str | None = None
    error: str | None = None

    @property
    def can_write_files(self) -> bool:
        return "write_files" in self.scopes


def read_status() -> CalendarStatus:
    cfg = shopify_admin.shopify_config()
    if cfg is None:
        return CalendarStatus(configured=False)
    status = CalendarStatus(configured=True, admin_url=cfg.admin_url)
    try:
        _, status.scopes = shopify_admin.access_token(cfg)
        for name in (CURRENT_FILENAME, NEXT_FILENAME):
            status.files[name] = shopify_admin.find_file(cfg, name)
    except shopify_admin.ShopifyError as exc:
        status.error = str(exc)
        return status
    try:
        status.theme = check_theme(cfg)
    except shopify_admin.ShopifyAccessDenied:
        status.theme_error = "テーマの確認には read_themes 権限が必要です（反映自体には不要）。"
    except (shopify_admin.ShopifyError, ValueError) as exc:
        status.theme_error = f"テーマを確認できませんでした（{exc}）。"
    return status


# --- 反映 ---------------------------------------------------------------------


@dataclass
class PublishResult:
    uploaded: list[tuple[str, str, str | None]] = field(default_factory=list)  # (ファイル名, 月, URL)
    saved: list[Path] = field(default_factory=list)
    theme: ThemeCheck | None = None
    storefront: dict[str, int] | None = None
    warnings: list[str] = field(default_factory=list)


StepCallback = Callable[[str, str, str | None], None]


def publish(
    plan: CalendarPlan,
    *,
    step: StepCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    folder: Path | None = None,
) -> PublishResult:
    """画像を生成・保存し、Shopify の固定名ファイルを差し替えて確認する。

    step(key, status, detail) で進捗を通知する。key は
    generate / upload_current / upload_next / verify。
    """
    notify = step or (lambda *_: None)
    cancelled = cancel_check or (lambda: False)
    cfg = shopify_admin.shopify_config()
    if cfg is None:
        raise shopify_admin.ShopifyError(
            "Shopify の接続設定がありません（SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET）。"
        )
    result = PublishResult()

    notify("generate", "running", None)
    _, scopes = shopify_admin.access_token(cfg)
    if "write_files" not in scopes:
        raise shopify_admin.ShopifyAccessDenied(
            "Shopify アプリに write_files 権限がありません。Dev Dashboard でスコープを追加してください。"
        )
    result.saved = generate_images(plan, folder)
    notify("generate", "completed", "、".join(p.name for p in result.saved) + " を保存しました。")

    for (target, _, filename), key in zip(plan.items, ("upload_current", "upload_next")):
        if cancelled():
            raise InterruptedError("中止しました。")
        notify(key, "running", f"{target.label} → {filename}")
        jpeg = image_path(target, folder).read_bytes()
        file_id = shopify_admin.replace_image_file(
            cfg, filename=filename, jpeg=jpeg, alt=f"{target.label}の営業日カレンダー"
        )
        ready = shopify_admin.wait_until_ready(cfg, file_id, filename)
        result.uploaded.append((filename, target.label, ready.url))
        notify(key, "completed", f"{target.label}を {filename} に反映しました。")

    notify("verify", "running", None)
    try:
        result.theme = check_theme(cfg)
        if not result.theme.setup_done:
            result.warnings.append(
                "テーマの初回設定が終わっていません。画面の「初回設定」の手順で、"
                "4か所の画像を calendar_current.jpg / calendar_next.jpg に付け替えてください。"
            )
        if result.theme.width_mismatch:
            result.warnings.append(
                "トップページのカレンダー画像の幅が揃っていません（"
                + "、".join(result.theme.width_mismatch)
                + "）。"
            )
    except shopify_admin.ShopifyAccessDenied:
        result.warnings.append("read_themes 権限が無いため、テーマの参照先は確認していません。")
    except (shopify_admin.ShopifyError, ValueError) as exc:
        result.warnings.append(f"テーマを確認できませんでした（{exc}）。")
    try:
        result.storefront = check_storefront()
        missing = [name for name, count in result.storefront.items() if count == 0]
        if missing:
            result.warnings.append(
                "ストアのトップページに " + "・".join(missing) + " が見つかりませんでした。"
                "テーマの初回設定を確認してください。"
            )
    except OSError as exc:
        result.warnings.append(f"ストアの表示を確認できませんでした（{exc}）。")
    notify(
        "verify",
        "completed",
        "確認しました。" if not result.warnings else f"注意 {len(result.warnings)} 件",
    )
    return result


def describe_updated_at(value: str) -> str:
    """Shopify の updatedAt（UTC ISO）を日本時間の表示にする。"""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")
