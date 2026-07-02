"""商品マスタなど、更新頻度の低いファイルの読み込み結果を mtime ベースでキャッシュする。

ページ表示のたびに数MBのExcel(.xlsm)をopenpyxlでフルパースしていた処理を、
ファイルの最終更新時刻が変わらない限り再利用することで高速化する。
変換ロジックそのものは変更しない（読み込み結果をメモするだけ）。
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Callable, TypeVar

T = TypeVar("T")

_CACHE: dict[tuple[str, str], tuple[int, object]] = {}
_LOCK = Lock()


def cached_by_mtime(path: Path, key: str, loader: Callable[[], T]) -> T:
    """``path`` の最終更新時刻が変わらない限り ``loader()`` の結果を再利用する。

    - ファイルが取得できない(stat不可)場合はキャッシュせず毎回 ``loader()`` を実行する。
    - ``key`` は同一ファイル内の別テーブル/別シートを区別するための識別子。
    - 返り値は呼び出し側で破壊的に変更しない前提（読み取り専用）で共有する。
      可変オブジェクト(DataFrame等)を返す場合は呼び出し側でコピーすること。
    """
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return loader()

    cache_key = (str(path), key)
    with _LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None and hit[0] == stamp:
            return hit[1]  # type: ignore[return-value]

    # 重い読み込みはロックの外で実行（同時アクセス時に二重読みは許容し、デッドロック/長時間ロックを避ける）
    value = loader()

    with _LOCK:
        _CACHE[cache_key] = (stamp, value)
    return value


def clear_master_cache() -> None:
    """キャッシュを全消去する（テスト・明示的リフレッシュ用）。"""
    with _LOCK:
        _CACHE.clear()
