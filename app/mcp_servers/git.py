"""Thin compatibility entry point for explicit legacy stdio fixtures."""
from pathlib import Path
from services.git.__main__ import main

if __name__ == "__main__":
    main(["--stdio", "--repo", str(Path(__file__).resolve().parents[2])])
