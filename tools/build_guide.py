"""Build guide.html from guide/template.html + guide/chapters/*.html.

Inside a chapter, write   {{file:path/to/file.py}}   and the build pastes
the REAL current contents of that project file into a code block. That is
how the guide's code always matches the project exactly.

If a file changes in a later chapter, the earlier chapter shows the version
from its own time instead:  {{file:path/to/file.py@m0}}  reads the frozen copy
guide/snapshots/m0/path/to/file.py. The newest chapter uses the live file.

Run:  uv run python tools/build_guide.py
"""

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE_DIR = ROOT / "guide"
OUT = ROOT / "guide.html"

FILE_TAG = re.compile(r"\{\{file:([^}@]+)(?:@([\w-]+))?\}\}")
SECTION_TAG = re.compile(r'<section[^>]*\bid="([^"]+)"[^>]*\bdata-title="([^"]+)"')


def code_block(rel_path: str, snapshot: str | None = None) -> str:
    rel_path = rel_path.strip()
    if snapshot:
        path = GUIDE_DIR / "snapshots" / snapshot / rel_path
    else:
        path = ROOT / rel_path
    if not path.exists():
        raise FileNotFoundError(f"guide references missing file: {path}")
    text = html.escape(path.read_text())
    return (
        '<div class="code"><div class="code-head">'
        f'<span class="path">{html.escape(rel_path)}</span></div>'
        f"<pre><code>{text}</code></pre></div>"
    )


def build() -> str:
    template = (GUIDE_DIR / "template.html").read_text()
    chapters = []
    toc = []
    for chapter_file in sorted((GUIDE_DIR / "chapters").glob("*.html")):
        body = chapter_file.read_text()
        body = FILE_TAG.sub(lambda m: code_block(m.group(1), m.group(2)), body)
        for sec_id, title in SECTION_TAG.findall(body):
            toc.append(f'<a href="#{sec_id}" data-sec="{sec_id}">{title}</a>')
        chapters.append(body)
    page = template.replace("{{TOC}}", "\n".join(toc))
    return page.replace("{{CHAPTERS}}", "\n".join(chapters))


if __name__ == "__main__":
    OUT.write_text(build())
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")
    sys.exit(0)
