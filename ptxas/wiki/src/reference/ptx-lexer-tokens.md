# PTX Lexer Tokens

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). `.rodata` VMA = file offset + `0x400000`.*

ptxas's PTX front-end uses a flex scanner (`ptxlex`, built with `lex -Cf` full tables)
feeding a bison grammar. The complete token vocabulary was reconstructed **from the
binary alone** by walking the embedded DFA: **527 distinct literal keywords across 547
flex actions**, mapping to **162 bison token ids**, of which **45 are modifier-group
tokens** (one id shared by a family of keywords) and 15 are no-token rules (comments,
whitespace, macros, `#line`, EOF).

## Scanner table form

The full-table flex form stores transitions as a flat array of 8-byte records:

```c
struct yy_trans_info { int32_t yy_verify; int32_t yy_nxt; };
```

- `yy_transition` is at VMA `0x203C048` (~191,600 records, ending ≈ `0x21B2ED8`).
- A DFA state is a record index `S`. The edge on input byte `c`: read record `S+c`; if
  `yy_verify == c`, the next state is `S' = S + yy_nxt` — **`yy_nxt` is a relative
  offset**, not an absolute index.
- The accept/action number for state `S` is the `int32` at byte `S*8 − 4` (i.e. the
  `yy_nxt` field of record `S−1`).
- The INITIAL start state is `yy_state_list[1]` (= record index 2); `yy_state_list` is
  at VMA `0x203C020`.
- Actions dispatch through a 552-entry jump table at `0x203A5A8`
  (`jmp *0x203A5A8(,%rax,8)`); each action block loads its bison token id (and a
  `ptxlval` modifier sub-code) before returning.

## Token id scheme

Bison terminals run **258–422 (0x102–0x1A6)**. A token id is either:

- **single** — one keyword (e.g. `0x153` = `.version`, `0x12D` = `.reg`), or
- **modifier-group** — one id covering many related keywords, disambiguated by the
  `ptxlval` sub-code (e.g. all MMA shapes share `0x182`).

A few ids in the range (263, 321, 398) are **parser-only terminals** — no lexer rule
emits them.

## Notable modifier groups

| Token id | Members | Group |
|---|---|---|
| `0x113` | 38 | type names — incl. modern FP8/FP6/FP4 (`.e4m3`, `.e5m2`, `.e2m1x2`, `.e3m2x2`, `.ue8m0x2`, …) |
| `0x182` | 178 | MMA/WGMMA shapes (`.mMnNkK`) + tensor tiles (`.RxCb`) |
| `0x140` | 18 | comparison operators |
| `0x120` | 10 | rounding modes (now incl. `.rna`, `.rs`) |
| `0x18D` | 10 | cache-eviction policies (`.L1::evict_*`, `.L2::evict_*`) |
| `0x1A1` | 11 | TMA tensor-descriptor fields (`.global_address`, `.rank`, `.box_dim`, `.swizzle_mode`, …) |
| `0x169` | 6 | memory-ordering (`.relaxed`, `.acquire`, `.release`, `.sc`, …) |
| `0x191` | 8 | vector widths `.x1` … `.x128` |

## redplait's unknown indices — resolved

The six indices left unlabeled by prior chain-walking are resolvable under both
readings, from the binary:

**As flex action/rule numbers** — all part of the dense WGMMA `.m64nXk8` / `.m64nXk64`
shape sweep (token `0x182`):

| action | keyword |
|---|---|
| 398 | `.m64n144k8` |
| 399 | `.m64n152k8` |
| 408 | `.m64n224k8` |
| 409 | `.m64n232k8` |
| 413 | `.m64n8k64` |
| 414 | `.m64n16k64` |

**As bison token ids:**

| token id | keyword(s) |
|---|---|
| `0x18E` (398) | *(parser-only — no lexer rule)* |
| `0x18F` (399) | `.mbarrier_init` |
| `0x198` (408) | `.seq` |
| `0x199` (409) | `.1g .2g .4g` |
| `0x19D` (413) | `.L2::cache_hint` |
| `0x19E` (414) | `.multicast::cluster`, `.multicast::cluster::all` |

## Reproduce

The full token table is in the repo: `decoded/ptxas-tokens/ptx_tokens.tsv`
(token id → keyword(s)/group-members, kind) and `ptxas_action_to_token.tsv` (the
552-action map: action, token id, sub-code, jump target). Regenerate with
`decoded/tools/ptxas_flex_tokens.py` (parameterized by table VMA / start state; it
re-derives all 547 actions). A handful of high-volume regex rules (identifiers, number
and string literals) are placeholders rather than enumerated paths; the literal-keyword
tokens are exact.

## Cross-References

- [PTX Parser (Flex + Bison)](../pipeline/ptx-parser.md) — the grammar and instruction registration.
- [Operand-Type Signatures & Attributes](operand-types-attributes.md) — what the parsed instructions carry.
- [PTX Instruction Table](ptx-instructions.md).
