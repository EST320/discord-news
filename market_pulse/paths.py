"""Repository-relative paths, so trackers work regardless of the working directory."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "assets"

# Not tracked on main: the workflows check the `state` branch out into this
# directory. Created on demand so a fresh local clone starts with empty state.
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
