import argparse
from pathlib import Path
from typing import Iterable, List


PRESET_SIZES_MB = [128, 512, 1024, 2048]


def generate_blank_file(path: Path, size_mb: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size_mb * 1024 * 1024)


def generate_preset_files(output_dir: Path) -> List[Path]:
    generated: List[Path] = []
    for size in PRESET_SIZES_MB:
        path = output_dir / f"payload_{size}mb.bin"
        generate_blank_file(path, size)
        generated.append(path)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成指定大小的空白文件，或一次性生成 128/512/1024/2048 MB 的预设文件"
    )
    parser.add_argument(
        "destination",
        type=Path,
        nargs="?",
        help="输出文件路径；若未指定则使用 --preset-dir 生成预设文件",
    )
    parser.add_argument("size", type=int, nargs="?", help="文件大小（MB）")
    parser.add_argument(
        "--preset-dir",
        type=Path,
        default=Path("payloads"),
        help="使用预设尺寸时的输出目录，默认 payloads",
    )
    parser.add_argument(
        "--preset",
        action="store_true",
        help="一次性生成 128/512/1024/2048 MB 的四个文件",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.preset or (args.destination is None and args.size is None):
        output_dir = args.preset_dir
        paths = generate_preset_files(output_dir)
        for path in paths:
            print(f"Generated {path} ({path.stat().st_size // 1024 // 1024} MB)")
        return

    if args.destination is None or args.size is None:
        raise SystemExit("必须提供文件路径和大小，或者使用 --preset 生成预设文件")

    generate_blank_file(args.destination, args.size)
    print(f"Generated {args.destination} ({args.size} MB)")


if __name__ == "__main__":
    main()
