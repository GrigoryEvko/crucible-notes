#!/usr/bin/env python3
"""ROT13 decoder for ptxas obfuscated knob/opcode strings.

ptxas v13.0 stores ~2,000 knob names, ~900 PTX opcode mnemonics, and 322 SASS
opcode mnemonics ROT13-encoded in .rodata. The cipher is rotate-13 on letters
only -- digits, underscores, and punctuation pass through unchanged. The
runtime decoder is inlined inside GetKnobIndex (sub_79B240 / sub_6F0820) and
performs the rotation and case-insensitive compare in a single loop.

This script reads ptxas_strings.json (produced by analyze_ptxas.py) and prints
every string that decodes to a recognizable identifier. It is the inverse of
the inline decoder shown in wiki/src/config/knobs.md:66-86.

Heuristic for "looks ROT13-encoded": pure [A-Za-z_][A-Za-z0-9_]* identifier,
length 6..80, at least two uppercase letters (filters single-word lowercase
strings), and decodes to a token containing a known knob/opcode prefix
(Mercury/Scav/URF/Convert/Disable/Enable/Force/Speculatively/Schedule/...).

Usage:
    python3 decode_rot13_knobs.py [--all] [--prefix PREFIX] [path/to/ptxas_strings.json]

    --all       Print every candidate, not just recognized-prefix matches.
    --prefix    Filter decoded output by prefix (e.g. --prefix Mercury).

Output columns: address, ROT13 form, decoded form.
"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import sys
from pathlib import Path

# Known plaintext prefixes after decoding -- these come from manual triage of
# the 1,294 knob registry and the 322 SASS opcode table. Adding entries here
# expands the recognized set without changing the cipher itself.
_KNOWN_PREFIXES: tuple[str, ...] = (
    "Mercury", "Scav", "URF", "Convert", "Disable", "Enable", "Force",
    "Speculatively", "Speculative", "Schedule", "Sched", "Loop", "Hoist",
    "Promote", "Bar", "Tex", "Sb", "Trace", "Dump", "Use", "Track",
    "Insert", "Merge", "Assume", "Compacted", "Consume", "Aggressive",
    "Conservative", "LongIntArith", "XBlock", "War", "Forward", "Reverse",
    "PostFix", "PreFix", "Generate", "Lower", "Expand", "Eliminate",
    "Cta", "Reconfig", "Optimize", "Pred", "Mov", "Linear",
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def looks_rot13_candidate(s: str) -> bool:
    """Cheap pre-filter for identifier-shaped strings.

    Filters out version strings, paths, format specifiers, and short tokens
    that would generate false positives during the prefix scan.
    """
    if not (6 <= len(s) <= 80):
        return False
    if not _IDENT_RE.match(s):
        return False
    if sum(1 for c in s if c.isupper()) < 2:
        return False
    return True


def rot13(s: str) -> str:
    """Apply ROT13 to letters, pass everything else through.

    Mirrors the inline decode in sub_79B240: `(c & 0xDF) - 65 <= 12` adds 13;
    `(c & 0xDF) - 78 < 13` subtracts 13. Non-alphabetic bytes are untouched,
    which is why `SchedNumBB_Limit` decodes correctly despite the underscore
    and digits.
    """
    return codecs.decode(s, "rot_13")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default="ptxas_strings.json",
                    help="Path to ptxas_strings.json (default: ./ptxas_strings.json)")
    ap.add_argument("--all", action="store_true",
                    help="Print every identifier-shaped candidate, not just known-prefix decodes.")
    ap.add_argument("--prefix", default=None,
                    help="Only print decoded names starting with this prefix (case-sensitive).")
    args = ap.parse_args()

    src = Path(args.path)
    if not src.is_file():
        print(f"error: {src} not found", file=sys.stderr)
        return 1

    with src.open() as f:
        raw = json.load(f)
    strings = raw if isinstance(raw, list) else raw.get("strings", [])

    rows: list[tuple[str, str, str]] = []
    for entry in strings:
        if not isinstance(entry, dict):
            continue
        v = entry.get("value", "")
        if not looks_rot13_candidate(v):
            continue
        d = rot13(v)
        if not args.all and not any(d.startswith(p) for p in _KNOWN_PREFIXES):
            continue
        if args.prefix and not d.startswith(args.prefix):
            continue
        addr = entry.get("ea") or entry.get("address") or ""
        rows.append((f"{addr:>10}" if isinstance(addr, str) else f"0x{addr:08x}", v, d))

    rows.sort(key=lambda r: r[2])  # alphabetical by decoded name
    for addr, enc, dec in rows:
        print(f"{addr}  {enc:<55s} -> {dec}")
    print(f"\n# {len(rows)} strings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
