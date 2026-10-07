"""Shared dark theme for the chart images: palette, fonts and layout helpers."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import to_rgb
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath

from market_pulse.fonts import cjk_font_paths

RED, GREEN = "#f0453a", "#22b573"
AMBER, INDIGO = "#e3a341", "#7c8cf0"

BG = "#171b24"
PANEL = "#232936"
TEXT = "#EDEFF2"
MUTED = "#8d94a1"
RULE = "#333a48"


def blend(base, tint, amount):
    base_rgb, tint_rgb = to_rgb(base), to_rgb(tint)
    return tuple(b + (t - b) * amount for b, t in zip(base_rgb, tint_rgb))


class Fonts:
    """font(size, bold=False) -> FontProperties in the shared typeface."""

    def __init__(self):
        self.regular, self.bold = cjk_font_paths()

    def __call__(self, size, bold=False):
        return FontProperties(fname=self.bold if bold else self.regular, size=size)


def tile(fig, rect, color=PANEL):
    """A flat coloured rectangle with a 0-1 coordinate system and no axes furniture."""
    ax = fig.add_axes(rect)
    ax.set_facecolor(color)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    return ax


# ------------------------------------------------------------------
# Inch canvas: for list-like cards whose height depends on the content.
# Coordinates are inches from the top-left corner.
# ------------------------------------------------------------------

def canvas(width, height):
    fig = plt.figure(figsize=(width, height))
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")
    return fig, ax


def box(ax, x, y, width, height, color=PANEL):
    ax.add_patch(mpatches.Rectangle((x, y), width, height, color=color, linewidth=0))


def plain(text):
    """Escape dollar signs: a pair of them would switch Matplotlib into math mode."""
    return str(text).replace("$", "\\$")


def text_width(text, font_properties):
    """Rendered width of the text in inches."""
    if not text:
        return 0.0
    return TextPath((0, 0), str(text), prop=font_properties).get_extents().width / 72


def ellipsize(text, font_properties, max_width):
    """Shorten the text with an ellipsis until it fits max_width inches."""
    text = str(text)
    if text_width(text, font_properties) <= max_width:
        return text
    while len(text) > 1 and text_width(text.rstrip() + "…", font_properties) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(text, font_properties, max_width):
    """Break the text into lines no wider than max_width inches, at spaces where possible."""
    lines, current = [], ""
    for word in str(text).split():
        candidate = f"{current} {word}" if current else word
        if text_width(candidate, font_properties) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        # A single word wider than the line is split mid-word rather than overflowing.
        while text_width(word, font_properties) > max_width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and text_width(word[:cut], font_properties) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def label(ax, x, y, text, font_properties, color=TEXT, ha="left", max_width=None):
    """Draw one line of text, vertically centred on y, optionally ellipsized."""
    if max_width is not None:
        text = ellipsize(text, font_properties, max_width)
    ax.text(x, y, plain(text), fontproperties=font_properties, color=color, ha=ha, va="center")


def save(fig, out_path, dpi=110):
    fig.savefig(out_path, dpi=dpi, facecolor=BG)
    plt.close(fig)
    return out_path
