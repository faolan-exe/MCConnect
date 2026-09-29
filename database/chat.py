"""Tooltips in chat lines for the plugin: ⟦label⇝line¶line⟧ shows the lines when hovering the label (plugin 3.20).

Plugins without the "hover" feature get the label only (without_hover, applied by the socket server). The label may
carry "&" colors; tooltip lines too ("&7" gray is the usual one, "&f" for values). Keep them short: 2-4 lines.
"""
import re

OPEN, HOVER, CLOSE, BREAK = "⟦", "⇝", "⟧", "¶"
HOVER_RE = re.compile(f"{OPEN}([^⇒{HOVER}{CLOSE}]*){HOVER}([^{CLOSE}]*){CLOSE}")
_MARKERS = re.compile(f"[{OPEN}{CLOSE}⇒{HOVER}{BREAK}|~\\n]")


def hover(label, *lines):
    """The label with a tooltip of the given lines (empty lines are left out; markers are removed from them)."""
    lines = [_MARKERS.sub("", str(line)) for line in lines if line]
    if not lines:
        return label
    return f"{OPEN}{_MARKERS.sub('', label)}{HOVER}{BREAK.join(lines)}{CLOSE}"


def without_hover(text):
    """The labels only (for plugins before 3.20)."""
    return HOVER_RE.sub(lambda m: m.group(1), text)
