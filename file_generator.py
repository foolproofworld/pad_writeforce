import argparse
from pathlib import Path
from typing import List


DEFAULT_ISO_PATH = Path.home() / "Desktop" / "pad_test.iso"
DEFAULT_SMALL_DIR = Path.home() / "Desktop" / "pad_small_files"


def generate_blank_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size_bytes)


def generate_iso(path: Path, size_gb: int = 2) -> Path:
    size_bytes = size_gb * 1024 * 1024 * 1024
    generate_blank_file(path, size_bytes)
    return path


def generate_small_files(output_dir: Path, count: int = 1_000, size_kb: int = 100) -> List[Path]:
    generated: List[Path] = []
    size_bytes = size_kb * 1024
    output_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(1, count + 1):
        filename = f"doc_{idx:04d}.dat"
        path = output_dir / filename
        generate_blank_file(path, size_bytes)
        generated.append(path)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "生成 pad_test.iso（约 2GB）以及 1000 个 100KB 的小文件，用于存储压力测试。"
            "默认输出到桌面，可通过参数自定义。"
        )
    )
    parser.add_argument(
        "--mode",
        choices=["all", "iso", "small"],
        default="all",
        help="选择生成内容：all=全部（默认），iso=仅生成大文件，small=仅生成小文件包",
    )
    parser.add_argument(
        "--iso-path",
        type=Path,
        default=DEFAULT_ISO_PATH,
        help=f"大文件输出路径，默认 {DEFAULT_ISO_PATH}",
    )
    parser.add_argument(
        "--iso-size-gb",
        type=int,
        default=2,
        help="大文件大小（GB），默认 2GB",
    )
    parser.add_argument(
        "--small-dir",
        type=Path,
        default=DEFAULT_SMALL_DIR,
        help=f"小文件输出目录，默认 {DEFAULT_SMALL_DIR}",
    )
    parser.add_argument(
        "--small-count",
        type=int,
        default=1_000,
        help="小文件数量，默认 1000",
    )
    parser.add_argument(
        "--small-size-kb",
        type=int,
        default=100,
        help="小文件大小（KB），默认 100KB",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.mode in {"all", "iso"}:
        iso_path = generate_iso(args.iso_path, args.iso_size_gb)
        size_gb = iso_path.stat().st_size / 1024 / 1024 / 1024
        print(f"生成大文件: {iso_path} （约 {size_gb:.2f} GB）")

    if args.mode in {"all", "small"}:
        files = generate_small_files(args.small_dir, args.small_count, args.small_size_kb)
        print(f"生成小文件包: {len(files)} 个文件，目录 {args.small_dir}")


if __name__ == "__main__":
    main()
