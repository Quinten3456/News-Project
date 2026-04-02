"""
run.py — Interactive entry point for the weekly AI Intelligence Brief.

Usage:
    python run.py
"""

import os
import subprocess
import sys


def main():
    print("\n=== AI Intelligence Brief ===\n")

    url = input("AI Report YouTube URL (press Enter to skip): ").strip()

    cmd = [sys.executable, "scripts/generate_brief.py", "--verbose"]
    if url:
        cmd += ["--podcast-url", url]

    print()
    subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    main()
