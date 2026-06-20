#!/usr/bin/env python3
"""
ptxas_flex_tokens.py -- recover the PTX lexer token table from the ptxas binary.

ptxas (CUDA 13.0.88) embeds a flex scanner (function ptxlex) built with the
flex *full table* form (lex -Cf): the DFA lives in one flat array
`yy_transition`, an array of 8-byte records {int32 yy_verify; int32 yy_nxt}.

DFA semantics recovered from the ptxlex disassembly:
  * A DFA *state* is identified by a record index S into yy_transition.
  * To take the edge for input byte c from state S, read record (S + c).
  * If that record's yy_verify == c the edge is valid; the next state is
        S' = S + record.yy_nxt          (yy_nxt is a RELATIVE record offset)
  * The accept/action number for a state S is the int32 stored 4 bytes before
    record S, i.e. the yy_nxt field of record (S-1).  0 == non-accepting.
  * The initial (INITIAL) state is yy_state_list[1], record index 2.

The recovered "action" is a flex rule number (0..551).  Each action is
dispatched through yy_action_jumptable (VMA 0x203a5a8); the rule body returns a
bison token id (258..422) and may first store a modifier sub-code into ptxlval.
Many keywords share one token id -- those are the PTX "modifier-group" tokens
(rounding modes, comparisons, cache-eviction policies, MMA shapes,
tensor-descriptor fields, ...).  The action->token map is read separately from
the jump-table targets (see decoded/ptxas-tokens/ptxas_action_to_token.tsv);
this script recovers the keyword <-> action half directly from the DFA.

Addresses are virtual (VMA); for .rodata in this build VMA = fileoffset+0x400000.

Pinned values for ptxas 13.0.88 (override with flags for other builds):
  yy_transition[0]  VMA 0x203c048   (record index 0)
  yy_state_list     VMA 0x203c020   (8-byte ptrs; [1] -> 0x203c058 = record 2)
  INITIAL start     record index 2
"""
import argparse
import struct
import sys
from collections import defaultdict

VMA_DELTA = 0x400000  # .text/.rodata: VMA = fileoffset + delta
TABLE_VMA = 0x203c048
START_STATE = 2

# flex rule numbers whose body is a regex/identifier/number/comment rule
# rather than a fixed keyword (for the 13.0.88 build; informational).
REGEX_ACTIONS = {528, 529, 530, 531, 532, 533, 534, 535, 536, 540, 548}


def load(path):
    with open(path, "rb") as fh:
        return fh.read()


def rec(blob, table_off, idx):
    """(yy_verify, yy_nxt) of record `idx`."""
    return struct.unpack_from("<ii", blob, table_off + idx * 8)


def accept(blob, table_off, state):
    """Action number for a state (int32 four bytes before record `state`)."""
    if state <= 0:
        return 0
    return struct.unpack_from("<i", blob, table_off + state * 8 - 4)[0]


def walk(blob, table_off, start, n_records, max_len=48):
    """DFS the DFA; return {action -> set(keyword strings)}."""
    syms = [9, 10, 13] + list(range(32, 127))
    results = defaultdict(set)
    stack = [(start, "")]
    seen = set()  # (state, prefix) guard against identifier self-loops
    while stack:
        S, pre = stack.pop()
        if len(pre) > max_len:
            continue
        act = accept(blob, table_off, S)
        if act and pre:
            results[act].add(pre)
        for c in syms:
            j = S + c
            if not (0 <= j < n_records):
                continue
            v, n = rec(blob, table_off, j)
            if v != c:
                continue
            nS = S + n
            if nS == S or not (0 < nS < n_records):
                continue
            key = (nS, pre)
            if key in seen:
                continue
            seen.add(key)
            stack.append((nS, pre + chr(c)))
    return results


def canonical(action, keywords):
    """Pick the canonical literal keyword for an action (shortest clean one)."""
    if action in REGEX_ACTIONS:
        rep = min(keywords, key=lambda s: (len(s), s))
        return "<regex:%s...>" % rep
    clean = [k for k in keywords if "\t" not in k and "\n" not in k and k]
    pool = clean or [k for k in keywords if k] or list(keywords)
    return min(pool, key=lambda s: (len(s), s)) if pool else ""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary")
    ap.add_argument("--table-vma", type=lambda x: int(x, 0), default=TABLE_VMA,
                    help="VMA of yy_transition[0]")
    ap.add_argument("--start", type=int, default=START_STATE,
                    help="INITIAL start-state record index")
    ap.add_argument("--records", type=int, default=192000,
                    help="record-count bound for the DFA walk")
    ap.add_argument("--max-len", type=int, default=48)
    ap.add_argument("--tsv", help="write action/keyword TSV here")
    ap.add_argument("--all-variants", action="store_true",
                    help="emit every matched literal, not just canonical")
    args = ap.parse_args()

    blob = load(args.binary)
    table_off = args.table_vma - VMA_DELTA
    res = walk(blob, table_off, args.start, args.records, args.max_len)

    out = open(args.tsv, "w") if args.tsv else sys.stdout
    out.write("action_dec\taction_hex\tkeyword(s)\tn_variants\n")
    for a in sorted(res):
        kws = sorted(res[a])
        if args.all_variants and a not in REGEX_ACTIONS:
            lits = sorted(k for k in kws if "\t" not in k and "\n" not in k and k)
            kw = "|".join(lits) if lits else canonical(a, kws)
        else:
            kw = canonical(a, kws)
        out.write(f"{a}\t{a:#x}\t{kw}\t{len(kws)}\n")
    if args.tsv:
        out.close()
        print(f"wrote {len(res)} actions -> {args.tsv}", file=sys.stderr)


if __name__ == "__main__":
    main()
