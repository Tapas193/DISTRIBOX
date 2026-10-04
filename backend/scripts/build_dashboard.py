"""Build the self-contained DISTRIBOX dashboard.

Reads the JSON payload emitted by ``scripts/verify.py`` and the HTML template,
and writes a single ``index.html`` with the data inlined. Inlining matters: a
static dashboard opened straight off the filesystem (``file://``) cannot use
``fetch()``, because browsers treat it as an opaque origin and refuse the
cross-origin read. Embedding the payload removes the need for a web server,
a build step, a bundler and any network access at all.

Usage::

    python -m scripts.build_dashboard
    python -m scripts.build_dashboard --check   # verify index.html is current
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
FRONTEND_DIR = REPO_DIR / "frontend"
TEMPLATE = FRONTEND_DIR / "index.template.html"
DATA = FRONTEND_DIR / "dashboard-data.json"
OUTPUT = FRONTEND_DIR / "index.html"

PLACEHOLDER = "__D6_DATA__"


def build(template: Path, data: Path, output: Path) -> Path:
    """Inline the JSON payload into the template and write ``output``."""
    html = template.read_text()
    occurrences = html.count(PLACEHOLDER)
    if occurrences != 1:
        # Must be exactly one. A second occurrence is almost always the token
        # quoted in the template's own header comment, and substituting into it
        # would inject the whole payload into a comment - silently doubling the
        # output size and mangling the documentation.
        raise SystemExit(
            f"{template} contains {occurrences} occurrences of {PLACEHOLDER}, "
            f"expected exactly 1. Do not mention the token in prose; rename the "
            f"placeholder or the prose."
        )

    payload = json.loads(data.read_text())

    # Compact: this file is read by a browser, not a human. Pretty-printing
    # would triple its size for no benefit.
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    # A literal "</script>" anywhere in the payload would close the tag early
    # and dump the rest of the document into the JSON parser. Escaping the
    # solidus is valid JSON and invisible to the parser.
    blob = blob.replace("</", "<\\/")

    output.write_text(html.replace(PLACEHOLDER, blob))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the DISTRIBOX dashboard")
    parser.add_argument("--template", default=str(TEMPLATE))
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the output is stale, without writing",
    )
    args = parser.parse_args()

    template, data, output = Path(args.template), Path(args.data), Path(args.output)
    for p in (template, data):
        if not p.exists():
            print(f"missing {p}. Run scripts/verify first to produce the payload.")
            return 1

    if args.check:
        if not output.exists():
            print(f"{output} does not exist")
            return 1
        build(template, data, output.parent / ".dashboard-check.html")
        fresh = (output.parent / ".dashboard-check.html").read_text()
        (output.parent / ".dashboard-check.html").unlink()
        current = output.read_text()
        if fresh != current:
            print(f"{output.name} is STALE relative to {data.name}")
            return 1
        print(f"{output.name} is up to date ({len(current):,} bytes)")
        return 0

    out = build(template, data, output)
    size = out.stat().st_size
    recs = len(json.loads(data.read_text()).get("recommendations", []))
    print(f"wrote {out} ({size:,} bytes, self-contained, {recs} recommendations)")
    print(f"open it directly: file://{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
