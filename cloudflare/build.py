"""
Build the static Cloudflare copy of the portfolio page.

templates/index.html is already plain HTML + JS; the only difference is a flag
that switches it to snapshot mode (read /api/snapshot, decrypt in the browser).
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC  = HERE.parent / "templates" / "index.html"
OUT  = HERE / "public" / "index.html"

html = SRC.read_text(encoding="utf-8")
if "<head>" not in html:
    raise SystemExit(f"{SRC}: no <head> tag to inject into")

html = html.replace("<head>", "<head>\n  <script>window.SNAPSHOT_MODE = true;</script>", 1)
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(html, encoding="utf-8")
print(f"wrote {OUT.relative_to(HERE.parent)}")
