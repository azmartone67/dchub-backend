#!/usr/bin/env python3
"""Render prompt.md with the claimed row's brief, fenced as JSON data.

The brief is serialised by json.dumps and nothing else, so no field in it can
close the fence or add prose outside the data block: a ``` inside a string
becomes \\u0060\\u0060\\u0060 before it reaches the prompt.

Usage: render_prompt.py brief.json > prompt.md
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def render(brief: dict, template: str | None = None) -> str:
    template = template if template is not None else \
        (HERE / "prompt.md").read_text(encoding="utf-8")
    data = json.dumps(brief, indent=2, ensure_ascii=True, default=str)
    data = data.replace("`", "\\u0060")
    return template.replace("{{BRIEF}}", data, 1)


if __name__ == "__main__":
    brief = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.stdout.write(render(brief))
