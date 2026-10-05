"""Repository-relative paths, so trackers work regardless of the working directory."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
ASSETS_DIR = ROOT / "assets"
