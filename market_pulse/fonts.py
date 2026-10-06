"""Locate a Chinese-capable font pair (regular, bold) for chart text."""

import tempfile
from functools import lru_cache
from pathlib import Path

# (regular, bold, face to extract or None). Checked in order.
CANDIDATES = [
    # Ubuntu, after `apt-get install fonts-noto-cjk`. The collection's first
    # face is the Japanese one, so the Simplified Chinese face is pulled out.
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "CJK SC"),
    # Windows (Microsoft YaHei) and macOS (PingFang), for local runs.
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", None),
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc", None),
]


def extract_face(collection_path, family_hint):
    """Save the face whose family name contains family_hint as a standalone font.

    Falls back to the collection itself (whose first face Matplotlib will use)
    if the face cannot be extracted: slightly different glyph shapes beat no
    chart at all.
    """
    try:
        from fontTools.ttLib import TTCollection

        out_path = Path(tempfile.gettempdir()) / f"{Path(collection_path).stem}-{family_hint.replace(' ', '')}.otf"
        if out_path.exists():
            return str(out_path)

        for font in TTCollection(collection_path).fonts:
            if family_hint in (font["name"].getDebugName(1) or ""):
                font.save(str(out_path))
                return str(out_path)
    except Exception as exc:
        print(f"Could not extract the {family_hint} face from {collection_path}: {exc!r}")
    return collection_path


@lru_cache(maxsize=1)
def cjk_font_paths():
    for regular, bold, face in CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            if face:
                return extract_face(regular, face), extract_face(bold, face)
            return regular, bold
    raise RuntimeError(
        "No Chinese font found. On Ubuntu install one with: sudo apt-get install -y fonts-noto-cjk"
    )
