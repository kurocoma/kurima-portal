from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


COMPANY_FOLDER = "株式会社しまのや"
LIBRARY_FOLDER = "くりまポータル - ドキュメント"
MASTER_FILE_NAME = "商品管理シート.xlsm"
ORDER_RELATIVE_PARTS = ("ネクストエンジン", "発注関連", "受注明細一覧")
TOOL_RELATIVE_PARTS = ("ネクストエンジン", "発注関連")


@dataclass(frozen=True)
class PortalPaths:
    portal_root: Path
    master_book: Path
    order_csv_dir: Path
    tool_dir: Path


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def candidate_portal_roots() -> list[Path]:
    home = Path.home()
    one_drive = _path_from_env("OneDriveCommercial") or _path_from_env("OneDrive")
    explicit = _path_from_env("PORTAL_ROOT")

    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)

    candidates.extend(
        [
            home / COMPANY_FOLDER / LIBRARY_FOLDER,
            home / f"OneDrive - {COMPANY_FOLDER}" / "kurimaportal" / "Shared Documents",
            home / f"OneDrive - {COMPANY_FOLDER}" / LIBRARY_FOLDER,
        ]
    )

    if one_drive:
        candidates.extend(
            [
                one_drive / "kurimaportal" / "Shared Documents",
                one_drive / LIBRARY_FOLDER,
            ]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def is_portal_root(path: Path) -> bool:
    return (
        path.exists()
        and (path / MASTER_FILE_NAME).is_file()
        and path.joinpath(*ORDER_RELATIVE_PARTS).is_dir()
    )


def find_portal_paths() -> PortalPaths:
    for root in candidate_portal_roots():
        if is_portal_root(root):
            return PortalPaths(
                portal_root=root,
                master_book=root / MASTER_FILE_NAME,
                order_csv_dir=root.joinpath(*ORDER_RELATIVE_PARTS),
                tool_dir=root.joinpath(*TOOL_RELATIVE_PARTS),
            )

    checked = "\n".join(str(path) for path in candidate_portal_roots())
    raise FileNotFoundError(
        "くりまポータルの同期フォルダを検出できませんでした。"
        "PORTAL_ROOT を設定するか、SharePoint ライブラリを同期してください。\n"
        f"確認した候補:\n{checked}"
    )


def latest_order_csv(order_csv_dir: Path) -> Path:
    files = [
        path
        for path in order_csv_dir.iterdir()
        if path.is_file() and path.name.lower().startswith("data")
    ]
    if not files:
        raise FileNotFoundError(f"data で始まる受注明細 CSV が見つかりません: {order_csv_dir}")
    return max(files, key=lambda path: path.stat().st_mtime)
