"""Markup mistakes in the templates that browsers "repair" into a broken page."""
import glob
import os
import re

TEMPLATES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "web", "templates", "**", "*.html"), recursive=True))
# a tag that is still open when the next one starts: <details class="x"<div ...>
TAG_IN_TAG = re.compile(r'<[a-zA-Z][^<>]*="[^"<>]*"<[a-zA-Z/]')


def test_no_tag_starts_inside_another_tag():
    found = [f"{os.path.basename(path)}: {match.group(0)}" for path in TEMPLATES
             for match in TAG_IN_TAG.finditer(open(path, encoding="utf-8").read())]
    assert found == []
