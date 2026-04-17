#!/usr/bin/env python3
"""SEO postprocess for the nvopen-tools GitHub Pages site.

Responsibilities
----------------
1. For every content HTML page (not print.html / toc.html / 404.html):
   - Inject <link rel="canonical" href="..."> pointing at the .html form of
     the URL (matching what the sitemap advertises). This deduplicates
     /foo/ vs /foo/index.html in Google's index.
2. For print.html and toc.html:
   - Ensure <meta name="robots" content="noindex"> is present and is the
     only robots directive (mdbook emits it by default on these pages;
     we just make sure no other robots tag overrides it).
3. Emit _site/sitemap.xml:
   - URL-encodes '+' -> '%2B' in path segments so cudafe++ works as
     cudafe%2B%2B in the XML.
   - Adds <lastmod> from file mtime.
   - Skips 404.html / print.html / toc.html.

Usage: python3 seo_postprocess.py <site-dir>
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import urllib.parse
from pathlib import Path

BASE_URL = "https://grigoryevko.github.io/nvopen-tools"

# Files inside each wiki that must NOT appear in the sitemap and must be
# marked noindex.
EXCLUDE_NAMES = {"404.html", "print.html", "toc.html"}

# Files we don't include in the sitemap but also don't rewrite (no canonical
# needed; mdbook noindexes them anyway).
NOINDEX_NAMES = {"print.html", "toc.html"}


def url_for(rel_path: str) -> str:
    """Convert a site-relative POSIX path to an absolute https URL.

    Each path segment is percent-encoded independently so '+' becomes
    '%2B' and other reserved characters are escaped, while the '/'
    separators are preserved.
    """
    segments = rel_path.split("/")
    encoded = "/".join(urllib.parse.quote(seg, safe="") for seg in segments)
    return f"{BASE_URL}/{encoded}"


_CANONICAL_RE = re.compile(
    r'<link\s+rel=["\']canonical["\'][^>]*>',
    flags=re.IGNORECASE,
)
_HEAD_OPEN_RE = re.compile(r"<head[^>]*>", flags=re.IGNORECASE)
_ROBOTS_INDEX_RE = re.compile(
    r'<meta\s+name=["\']robots["\']\s+content=["\']index[^"\']*["\']\s*/?>',
    flags=re.IGNORECASE,
)


def inject_canonical(html: str, canonical_url: str) -> str:
    """Insert or replace <link rel="canonical"> in a document's <head>."""
    if _CANONICAL_RE.search(html):
        return _CANONICAL_RE.sub(
            f'<link rel="canonical" href="{canonical_url}">',
            html,
            count=1,
        )
    tag = f'\n        <link rel="canonical" href="{canonical_url}">'
    m = _HEAD_OPEN_RE.search(html)
    if not m:
        return html
    return html[: m.end()] + tag + html[m.end() :]


def strip_stray_index_follow(html: str) -> str:
    """Remove any <meta name="robots" content="index,..."> tags.

    mdbook emits its own noindex on print/toc pages. Historical head.hbs
    used to inject an overriding 'index, follow' directive; this strips
    any residue so the noindex stays authoritative.
    """
    return _ROBOTS_INDEX_RE.sub("", html)


def process_html_file(
    path: Path,
    site_root: Path,
    indexable_pages: list[tuple[str, float]],
) -> None:
    rel = path.relative_to(site_root).as_posix()
    name = path.name

    text = path.read_text(encoding="utf-8", errors="replace")
    original = text

    if name in NOINDEX_NAMES:
        # These should not have conflicting robots directives, and
        # should not carry a canonical (they're non-canonical variants).
        text = strip_stray_index_follow(text)
    elif name == "404.html":
        # Leave as-is; GitHub Pages will serve it.
        pass
    else:
        canonical = url_for(rel)
        text = inject_canonical(text, canonical)
        indexable_pages.append((rel, path.stat().st_mtime))

    if text != original:
        path.write_text(text, encoding="utf-8")


def write_sitemap(site_root: Path, pages: list[tuple[str, float]]) -> None:
    pages.sort(key=lambda t: t[0])
    out = site_root / "sitemap.xml"
    with out.open("w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for rel, mtime in pages:
            loc = url_for(rel)
            lastmod = _dt.datetime.fromtimestamp(
                mtime, tz=_dt.timezone.utc
            ).strftime("%Y-%m-%d")
            fh.write(
                f"  <url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>\n"
            )
        fh.write("</urlset>\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: seo_postprocess.py <site-dir>", file=sys.stderr)
        return 2
    site_root = Path(argv[1]).resolve()
    if not site_root.is_dir():
        print(f"not a directory: {site_root}", file=sys.stderr)
        return 2

    indexable: list[tuple[str, float]] = []
    for root, _, files in os.walk(site_root):
        for fname in files:
            if not fname.endswith(".html"):
                continue
            process_html_file(
                Path(root) / fname,
                site_root,
                indexable,
            )

    write_sitemap(site_root, indexable)
    print(
        f"seo_postprocess: wrote sitemap with {len(indexable)} URLs "
        f"to {site_root / 'sitemap.xml'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
