from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portal_app.services.paths import find_portal_paths  # noqa: E402
from portal_app.services.yamato_conversion import (  # noqa: E402
    YAMATO_OUTPUT_HEADERS,
    load_item_name_map,
    read_yamato_source_csv,
    transform_ne_to_yamato,
    write_quoted_cp932_csv,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_files(paths: list[Path]) -> str:
    return sha256_text(":".join(sha256_file(path) for path in paths))


def resolve_path(value: object) -> Path:
    # `~` と `%USERPROFILE%` 等の環境変数を展開する（PC 依存のユーザー名を config に直書きしないため）。
    path = Path(os.path.expandvars(str(value))).expanduser()
    return path if path.is_absolute() else ROOT / path


def parse_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_list: str | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current_list is None:
                raise ValueError(f"List item without key in {path}: {raw}")
            data.setdefault(current_list, []).append(_clean_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            raise ValueError(f"Unsupported YAML line in {path}: {raw}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            data[key] = []
            current_list = key
        else:
            data[key] = _clean_scalar(value)
            current_list = None
    return data


def _clean_scalar(value: str) -> str | bool | int:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.isdigit():
        return int(value)
    return value


def read_csv_shape(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="cp932", newline="") as handle:
        rows = list(csv.reader(handle))
    header = rows[0] if rows else []
    data = rows[1:] if rows else []
    return {
        "header": header,
        "row_count": len(data),
        "column_count": len(header),
        "column_counts": sorted({len(row) for row in rows}),
    }


def first_byte_difference(left: bytes, right: bytes) -> int | None:
    for index, (left_byte, right_byte) in enumerate(zip(left, right)):
        if left_byte != right_byte:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def first_cell_difference(generated: Path, reference: Path) -> dict[str, int] | None:
    with generated.open("r", encoding="cp932", newline="") as generated_handle:
        generated_rows = list(csv.reader(generated_handle))
    with reference.open("r", encoding="cp932", newline="") as reference_handle:
        reference_rows = list(csv.reader(reference_handle))
    for row_index, (generated_row, reference_row) in enumerate(zip(generated_rows, reference_rows), start=1):
        for column_index, (generated_cell, reference_cell) in enumerate(zip(generated_row, reference_row), start=1):
            if generated_cell != reference_cell:
                return {"row": row_index, "column": column_index}
        if len(generated_row) != len(reference_row):
            return {"row": row_index, "column": min(len(generated_row), len(reference_row)) + 1}
    if len(generated_rows) != len(reference_rows):
        return {"row": min(len(generated_rows), len(reference_rows)) + 1, "column": 1}
    return None


def validate_cp932(path: Path) -> tuple[bool, str | None]:
    try:
        path.read_text(encoding="cp932")
        return True, None
    except UnicodeDecodeError as exc:
        return False, str(exc)


def validate_crlf(path: Path) -> bool:
    data = path.read_bytes()
    return b"\n" not in data.replace(b"\r\n", b"")


def build_generated_csv(source_csv: Path, output_path: Path) -> dict[str, Any]:
    paths = find_portal_paths()
    item_map, item_master_rows, item_warnings = load_item_name_map(paths)
    source_df = read_yamato_source_csv(source_csv)
    converted_df, transform_warnings, address_reviews, address_adjusted_rows = transform_ne_to_yamato(
        source_df,
        item_map,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_quoted_cp932_csv(converted_df, output_path)
    warnings = list(item_warnings + transform_warnings)
    return {
        "source_rows": len(source_df),
        "output_rows": len(converted_df),
        "duplicate_rows_removed": len(source_df) - len(converted_df),
        "item_master_rows": item_master_rows,
        "address_adjusted_rows": address_adjusted_rows,
        "address_review_rows": sum(1 for item in address_reviews if item.requires_review),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def run(config_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    config = parse_simple_yaml(config_path)
    source_csv = resolve_path(config["source_csv"])
    reference_csv = resolve_path(config["reference_csv"])
    run_root = resolve_path(config.get("run_root", "validation/runs"))
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    run_dir = output_dir or run_root / f"yamato-b2-equivalence-{run_id}"
    generated_csv = run_dir / str(config.get("generated_name", "generated-ne-to-yamato.csv"))

    generation = build_generated_csv(source_csv, generated_csv)
    generated_bytes = generated_csv.read_bytes()
    reference_bytes = reference_csv.read_bytes()
    generated_shape = read_csv_shape(generated_csv)
    reference_shape = read_csv_shape(reference_csv)
    generated_cp932, generated_cp932_error = validate_cp932(generated_csv)
    reference_cp932, reference_cp932_error = validate_cp932(reference_csv)

    differences: list[dict[str, Any]] = []
    if generated_bytes != reference_bytes:
        differences.append(
            {
                "kind": "byte_content",
                "first_byte_offset": first_byte_difference(generated_bytes, reference_bytes),
                "first_cell_mismatch": first_cell_difference(generated_csv, reference_csv),
            }
        )
    if generated_shape["header"] != reference_shape["header"]:
        differences.append({"kind": "header"})
    if generated_shape["row_count"] != reference_shape["row_count"]:
        differences.append(
            {
                "kind": "row_count",
                "generated": generated_shape["row_count"],
                "reference": reference_shape["row_count"],
            }
        )
    if generated_shape["column_count"] != reference_shape["column_count"]:
        differences.append(
            {
                "kind": "column_count",
                "generated": generated_shape["column_count"],
                "reference": reference_shape["column_count"],
            }
        )
    if generated_shape["header"] != list(YAMATO_OUTPUT_HEADERS):
        differences.append({"kind": "expected_output_headers"})
    if generated_shape["column_count"] != 42:
        differences.append({"kind": "expected_column_count", "expected": 42, "generated": generated_shape["column_count"]})
    if not generated_cp932:
        differences.append({"kind": "generated_cp932", "error": generated_cp932_error})
    if not reference_cp932:
        differences.append({"kind": "reference_cp932", "error": reference_cp932_error})
    if not validate_crlf(generated_csv):
        differences.append({"kind": "generated_crlf"})
    if not validate_crlf(reference_csv):
        differences.append({"kind": "reference_crlf"})

    paths = find_portal_paths()
    source_csv_hash = sha256_file(source_csv)
    master_book_hash = sha256_file(paths.master_book)
    reference_hash = sha256_file(reference_csv)
    generated_hash = sha256_file(generated_csv)
    implementation_hash = sha256_files(
        [
            ROOT / "portal_app" / "services" / "yamato_conversion.py",
            ROOT / "portal_app" / "services" / "paths.py",
        ]
    )
    report = {
        "name": "yamato_b2_csv_equivalence",
        "kind": "yamato_b2_csv_equivalence",
        "run_id": run_id,
        "passed": not differences,
        "source_csv": str(source_csv),
        "master_book": str(paths.master_book),
        "reference_csv": str(reference_csv),
        "generated_csv": str(generated_csv),
        "report_file": str(run_dir / "report.json"),
        "generation": generation,
        "generated_shape": {key: value for key, value in generated_shape.items() if key != "header"},
        "reference_shape": {key: value for key, value in reference_shape.items() if key != "header"},
        "differences": differences,
        "hashes": {
            "source_csv": source_csv_hash,
            "master_book": master_book_hash,
            "source_hash": sha256_text(f"{source_csv_hash}:{master_book_hash}"),
            "reference_csv": reference_hash,
            "generated_csv": generated_hash,
            "baseline_hash": reference_hash,
            "generated_hash": generated_hash,
            "implementation_hash": implementation_hash,
            "validator_hash": sha256_text(
                (ROOT / "scripts" / "validate_equivalence.py").read_text(encoding="utf-8-sig")
                + config_path.read_text(encoding="utf-8-sig")
            ),
            "criteria_hash": sha256_file(ROOT / ".loop" / "current" / "criteria.yaml"),
            "task_hash": sha256_file(ROOT / ".loop" / "current" / "task.md"),
            "eval_schema_hash": sha256_file(ROOT / ".loop" / "current" / "eval-schema.json"),
        },
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("validation/equivalence.yaml"))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)

    report = run(args.config, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
