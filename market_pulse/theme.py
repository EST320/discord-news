"""Shared dark theme for the chart images: palette, fonts and tile helper."""

from matplotlib.colors import to_rgb
from matplotlib.font_manager import FontProperties

from market_pulse.fonts import cjk_font_paths

RED, GREEN = "#f0453a", "#22b573"

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
