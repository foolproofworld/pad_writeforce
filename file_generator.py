import argparse
import os
from pathlib import Path


def generate_blank_file(path: Path, size_mb: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size_mb * 1024 * 1024)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate blank files of a given size for storage stress testing")
    parser.add_argument("destination", type=Path, help="Output file path")
    parser.add_argument("size", type=int, help="File size in MB")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate_blank_file(args.destination, args.size)
    print(f"Generated {args.destination} ({args.size} MB)")


if __name__ == "__main__":
    main()
