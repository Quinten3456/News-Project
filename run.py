"""
run.py — Interactive entry point for the weekly AI Intelligence Brief.

Usage:
    python run.py
"""

import os
import subprocess
import sys


def main():
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-cache", action="store_true")
    args, _ = parser.parse_known_args()

    print("\n=== AI Intelligence Brief ===\n")

    url = input("AI Report YouTube URL (press Enter to skip): ").strip()

    cmd = [sys.executable, "scripts/generate_brief.py", "--verbose"]
    if url:
        cmd += ["--podcast-url", url]
    if args.no_cache:
        cmd += ["--no-cache"]

    print()
    subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    main()
