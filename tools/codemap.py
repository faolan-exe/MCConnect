"""Regenerates the route tables of docs/CODEMAP.md from the Flask URL map (no database needed).

    python tools/codemap.py          # rewrites the part between the ROUTES markers
    python tools/codemap.py --check  # exit 1 if the file is out of date (for CI or before a commit)
"""
import inspect
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CODEMAP = os.path.join(ROOT, "docs", "CODEMAP.md")
START, END = "<!-- ROUTES:START (tools/codemap.py) -->", "<!-- ROUTES:END -->"


class _NoDB:
    """create_app only stores the manager; the URL map needs no database."""


def routes():
    import logging
    logging.disable(logging.INFO)
    from web.main import create_app
    app = create_app(_NoDB(), {"TESTING": True, "SECRET_KEY": "codemap", "SERVER_NAME": "mc.test"})
    rows = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        view = app.view_functions[rule.endpoint]
        while hasattr(view, "__wrapped__"):
            view = view.__wrapped__
        body = inspect.getsource(view)
        templates = re.findall(r'render(?:_template|_mod)\(\s*(?:"[a-z]*",\s*)?"([^"]+\.html)"', body)
        methods = ", ".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        rows.append(("server" if rule.subdomain else "main", rule.rule, methods, view.__name__,
                     ", ".join(dict.fromkeys(templates))))
    return sorted(rows)


def table(rows, area, api):
    lines = ["| Path | Method | View (web/main.py) | Template |", "|---|---|---|---|"]
    for where, path, methods, view, templates in rows:
        if where == area and path.startswith(("/api/", "/mc/", "/icons/", "/resourcepack/", "/download/", "/uploads/")) == api:
            lines.append(f"| `{path}` | {methods} | `{view}` | {templates} |")
    return "\n".join(lines)


def generated():
    rows = routes()
    return "\n\n".join([
        START,
        "### Server pages (`<subdomain>.`)", table(rows, "server", False),
        "### Server APIs and files", table(rows, "server", True),
        "### Main domain pages", table(rows, "main", False),
        "### Main domain APIs and files", table(rows, "main", True),
        END])


def main():
    text = open(CODEMAP, encoding="utf-8").read()
    new = text[:text.index(START)] + generated() + text[text.index(END) + len(END):]
    if "--check" in sys.argv:
        if new != text:
            print("docs/CODEMAP.md is out of date: python tools/codemap.py")
            return 1
        return 0
    with open(CODEMAP, "w", encoding="utf-8") as file:
        file.write(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
