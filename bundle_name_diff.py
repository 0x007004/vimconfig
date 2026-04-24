#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path
from typing import List, Optional, Set


def read_bundle_names(csv_path: Path) -> Set[str]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if "bundle_name" not in (reader.fieldnames or []):
            raise ValueError(f"{csv_path} 缺少表头 'bundle_name'")

        bundle_names = set()
        for row in reader:
            bundle_name = (row.get("bundle_name") or "").strip()
            if bundle_name:
                bundle_names.add(bundle_name)
        return bundle_names


def write_result(bundle_names: List[str], output_path: Optional[Path]) -> None:
    if output_path is None:
        for name in bundle_names:
            print(name)
        return

    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["bundle_name"])
        for name in bundle_names:
            writer.writerow([name])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="计算存在 GTE 不存在 GTA 中的 bundle_name"
    )
    parser.add_argument("--gta", required=True, help="GTA CSV 文件路径")
    parser.add_argument("--gte", required=True, help="GTE CSV 文件路径")
    parser.add_argument(
        "--output",
        help="可选：输出结果 CSV 文件路径；不传则直接打印到终端",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        gta_names = read_bundle_names(Path(args.gta))
        gte_names = read_bundle_names(Path(args.gte))
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    diff_names = sorted(gte_names - gta_names)
    write_result(diff_names, Path(args.output) if args.output else None)
    print(f"共找到 {len(diff_names)} 个只存在于 GTE 的 bundle_name", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
