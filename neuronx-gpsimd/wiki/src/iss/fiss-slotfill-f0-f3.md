# fiss Slotfill — FLIX Formats F0–F3

> **Part 14, the operand-decode layer.** The gateway
> ([libcas-core Surface + ISS Plugin ABI](./cas-core-surface.md)) established the
> division of labor: `libcas-core.so` is a *timing/hazard* oracle and
> `libfiss-base.so` is the *value* oracle. This page documents the **front door**
> of that value oracle — the `slotfill__` operand decoders — for the four 16-byte
> wide FLIX formats **F0, F1, F2, F3**. A `slotfill__` function is the functional
> ISS's **bitfield-decode complement** to the libisa-core *encode* thunks: where
> `Opcode_<m>_Slot_<s>_encode` deposits a selector template and `field_set` lays
> operand bits *into* a bundle, the slotfill reads operand fields *back out* of a
> live bundle word. This page proves the two are exact inverses.

All facts below are derived from static analysis of the two shipped binaries only,
by ABSOLUTE path, with `nm -D`, `objdump -d/-s`, `readelf -SW`, and a small x86
field-emulator that replicates (or drives) the real shift/mask sequences:

```
FISS = ncore2gp/config/libfiss-base.so   (12,330,016 B; ELF64 x86-64 SYSV, not stripped)
ISA  = ncore2gp/config/libisa-core.so    ( 9,690,712 B; ELF64 x86-64 SYSV, not stripped)
```

Each claim is tagged **confidence** × **provenance**: `OBSERVED` = read directly
off the binary; `INFERRED` = strong deduction from observed evidence;
`CARRIED` = taken from a sibling report (`ISS-09`/`ISS-13`, the fiss surface
and datapath analyses). `.text` and `.rodata` are
VMA==file-offset in both libraries; the writable sections carry the ncore2gp
`0x200000` delta — confirmed for FISS below (§6) and matching the gateway's
finding for libcas-core.

---

## 1. Where this page sits in the fiss surface

`libfiss-base.so` exports **20,368** defined `T` functions (`nm -D --defined-only |
rg -c ' T '`); `nm -D` lists **20,384** dynamic symbols including version/abs
entries. They partition by the seven `libfiss.h` stage prefixes plus `slotfill__`
and `opcode__`. The headline census, consistent with the
gateway:

| fiss-base prefix | count | role |
|---|--:|---|
| `slotfill__` | **12,569** | operand bitfield decode (**this page**) |
| `opcode__` | 1,708 | per-lane datapath stage-5 callers |
| `stateload__` / `regload__` / `writeback__` | 1,534 each | arch-state / gather / scatter |
| `opcode_complete__` | 214 | commit hooks |
| `memload__` | 110 | memory-load value path |
| `memstore__` / `memstore_check__` | 106 each | memory-store value path |
| `module__` | **864** | the value leaves (the *xdref* oracle) |

> **The 864 / 12,569 / ~20,379 framing.** The **864** `module__xdref_*` leaves are
> the value computation ([The fiss Datapath Oracle](./fiss-datapath-oracle.md)); the
> **12,569** `slotfill__` functions are the *operand-decode* layer this page covers;
> the ~20.4k total exports are the full per-(mnemonic×stage) value-kernel matrix.
> The slotfill count **12,569** equals the libisa-core `Opcode_*_encode` thunk count
> exactly (both `nm \| rg -c` = 12,569) — one decode function per encode thunk. The
> decode layer is the encode layer's structural mirror.
### 1.1 Census — the F0–F3 slice (reconciled to the unit)

`nm -D --defined-only FISS \| rg 'slotfill__F[0123]__'` selects **6,288** of the
12,569 — the F0–F3 slice. The per-format and per-(format×slot×unit) totals
are exact:

| format | slots | per-cell counts | total |
|---|--:|---|--:|
| **F0** | 4 | `S0_LdSt` 348 · `S1_Ld` 260 · `S2_Mul` 322 · `S3_ALU` 564 | **1,494** |
| **F1** | 4 | `S0_LdStALU` 542 · `S1_Ld` 260 · `S2_Mul` 449 · `S3_ALU` 558 | **1,809** |
| **F2** | 4 | `S0_LdSt` 213 · `S1_Ld` 257 · `S2_Mul` 535 · `S3_ALU` 544 | **1,549** |
| **F3** | **5** | `S0_LdSt` 342 · `S1_Ld` 256 · `S2_Mul` 244 · `S3_ALU` 503 · **`S4_ALU` 91** | **1,436** |

`1,494 + 1,809 + 1,549 + 1,436 = 6,288`. All 17 cells enumerated; every column sum
equals the per-format `nm` count. **F3 is the 5-slot format** (dual ALU `S3`+`S4`),
matching the libisa `formats[]` roster
([FLIX VLIW Encoding §2](../isa/core/flix-encoding.md): F3 idx 7 carries
`LdSt·Ld·Mul·ALU·ALU`). The placement census on the *encode* side
([FLIX VLIW Encoding §6.3](../isa/core/flix-encoding.md)) carries the **identical**
per-cell totals (F3 `S0_LdSt 342 … S4_ALU 91` etc.) — the per-(format×slot)
opcode population is the same matrix viewed from either end.
> **CORRECTION — the "role-tag" suffix census is not a clean partition; the trailing
> token is mostly an IVP datatype suffix.** A backing report tabulated F0–F3 by a
> per-slot encoding tag (`_H 455 / _S 442 / _A 259 / _W15 243 / _N 23 / _WB 3`).
> Those figures do **not** survive a byte-exact recount. The trailing `_<TOKEN>` on a
> slotfill symbol is part of the *mnemonic spelling*, dominated by IVP datatype/mode
> suffixes, not a separable role tag: counting the actual final token over the 6,288
> F0–F3 names gives `_2X32` 379, `_XP` 294, `_H` 289, `_S` 287, `_IP` 286,
> `_2XF32` 270, `_2XF32T` 256, `_I` 227, `_X` 191, … with `_W15` 108, `_A` 108,
> and **`_WB` = 0** (no F0–F3 symbol ends in `_WB`). The earlier `_S 442`/`_WB 3`
> counts came from substring greps that also matched `_slot` and mnemonic interiors.
> Use the **per-format/per-cell** counts (§1.1) — those are exact; treat any
> standalone "tag distribution" with suspicion.

There are ~1,533 distinct base-mnemonic spellings across the F0–F3 slotfill set
(`nm -D \| rg -o 'slot[0-9]__[A-Z].*$' \| sort -u \| wc -l`), but they collapse into
a **small number of decode *forms*** — the entire 6,288-function slice is generated
from roughly four field-extraction templates (§3), re-anchored per slot. The
distinct counts differ because the opcode *population* per slot differs, not the
field math. *(HIGH · OBSERVED for the spelling count; INFERRED for "four forms".)*

---

## 2. The slotfill ABI — the per-op context (`%rdi`)

Every slotfill is a leaf with one argument (`%rdi` → per-op context), no stack
frame in the simple bodies, and `xor eax,eax ; ret` on success. It reads the raw
bundle from the head of the context and writes decoded operand **indices**
(register numbers) / **immediates** into fixed operand-latch offsets of the *same*
context. It performs **no arithmetic on operand values** — only index math,
sign/zero-extension, and sub-opcode selection.

```
ctx (%rdi) layout TOUCHED by the F0–F3 slotfill:
  +0x00  word0   bundle bits [31:0]     (op0 nibble in [3:0])
  +0x04  word1   bundle bits [63:32]
  +0x08  word2   bundle bits [95:64]
  +0x0c  word3   bundle bits [127:96]
  +0x2c  windowed-mode predicate        (test; je  -> selects AR-rotation path)
  +0x30  window-base                     (shl $3 => x8, added into each AR index)
  ---- operand-INDEX latches (written by slotfill, read by regload/writeback) ----
  SCALAR (base-Xtensa AR form):
    +0x24  dst AR index      +0x48  src1 AR index      +0x50  src2 AR idx | imm
  VECTOR (IVP 3-reg ALU form):
    +0x50  vs index   +0x94  vt index   +0xd8  vr index    (ALU canonical triple)
    +0x28  sub-op / alt-vt  +0x6c  alt-vs  +0xb0  alt-vr   (Mul / dual-slot set)
    +0xec / +0x130           extra vreg fields (MULNX16 sub-op case)
    +0x34 / +0xac            parallel bool-guard / lane-mask latches (predicate path)
  ---- operand-VALUE blocks (written later by regload, NOT by slotfill) ----
    +0x54.. / +0x98.. / +0xdc..   16-dword (512-bit NX16) gathered vector values
```

> **QUIRK — the bundle is the first 16 bytes of the context.** `word0..word3` live at
> `ctx+0x00..0x0c` and the slotfill `mov (%rdi)` / `mov 0x4(%rdi)` / `mov 0x8(%rdi)`
> reads them as four little-endian dwords. A reimplementation must place the live
> bundle word at the head of the op-context *before* invoking the slot decoder; the
> decoder never receives the bundle as a separate argument.
**Index→value pairing (the regload handoff).** The slotfill is the *only* producer
of operand indices; `regload__<m>` converts each index to a 512-bit value. Read off
`regload__add_h` @`0x7eb160`:

```text
mov 0x94(%rdi),%edx ; shl $0x4,%edx              ; vt index x 16  (NX16 vreg = 16 dwords)
mov 0x8(%rsi),%rax  ; mov 0x10(%rax),%rax         ; rax = vector regfile base
mov (%rax,%rcx,4),%ecx ; mov %ecx,0x98(%rdi)      ; gather lane 0 -> ctx+0x98
... lanes 1..15 -> 0x9c,0xa0,...,0xd4             ; 16 dwords = one NX16 vreg value
```

The three indices `regload__add_h` dereferences are **exactly** `0x50, 0x94, 0xd8`
— the three latches the `F0_S3_ALU` slotfill wrote (§3.2). The regfile base is
`*(*(%rsi+0x8)+0x10)`; the `x16` stride and 5-bit index imply a **32-entry × 16-dword
(512-bit NX16) vector register file**, consistent with `opnd_sem_vec_addr`'s
`& 0x1f` mask in the gateway. *(HIGH · OBSERVED for the latch addresses and stride;
INFERRED for the 32-entry size — it is read from the gather stride and the 5-bit
index width, not a declared bound.)*

---

## 3. Per-cell bitfield decode — all 17 F0–F3 cells

Two decode families coexist in every wide slot. A **scalar** base-Xtensa form
decodes classic AR-register fields with window rotation; a **vector** IVP form
decodes 5-bit `vr/vs/vt` indices scattered across the 128-bit bundle. The same
field math is reused by every mnemonic of the same form in a cell (spot-verified on
ADD/SUB/MIN/ABS samples). "Bundle bit `b`" = absolute bit in the 128-bit word
(`word_i` bit `k` = bundle bit `32*i + k`). All immediates below were read from the
disassembled bodies; the vector positions are cross-checked against the libisa-core
`get_fn ∘ field_get` composition (§4).

### 3.1 Scalar base-Xtensa AR form

All four formats share **one** scalar S0 decoder, re-anchored to the slot's bundle
bit-offset. Each AR field is **4 bits**: a 3-bit low triple at the slot's nominal
offset **plus a 4th high "bank" bit** (XOR-inverted) that selects the upper register
window/bank; a window-base value (`ctx+0x30 × 8`) is added and the result masked to
**6 bits (`& 0x3f` → 64 physical AR registers)**.

Annotated C reproducing `slotfill__F0__F0_S0_LdSt_slot0__ADD` @`0x29d220`
(byte-for-byte the same body as `slotfill__x24__Inst_slot0__ADD` @`0x296320` — see
§4d), naming the real shift/mask immediates:

```c
/* one AR field = 3 low index bits + 1 XOR-inverted high bank bit + window-base,
 * masked to 6 bits (64 AR). lo3_sh / bank_sh are the per-field shifts read from
 * the body. The je @0x29d22f selects the windowed path when ctx+0x2c != 0. */
static uint32_t ar_field(uint32_t word0, uint32_t winbase8,
                         unsigned lo3_sh, unsigned bank_sh) {
    uint32_t idx  = (word0 >> lo3_sh) & 0x7;             /* shr lo3_sh ; and $0x7  */
    uint32_t bank = ((word0 >> bank_sh) ^ 1) & 0x1;      /* shr bank_sh; xor $1; and $1 */
    idx |= bank << 3;                                    /* lea (,bank,8)          */
    idx += winbase8;                                     /* add window-base (<<3)  */
    return idx & 0x3f;                                   /* and $0x3f  -> 64 AR    */
}

int slotfill__F0_S0_LdSt__ADD(op_ctx *c) {              /* @0x29d220              */
    uint32_t w0 = c->word0;                              /* mov (%rdi)             */
    uint32_t wb = c->window_base << 3;                   /* mov 0x30(%rdi); shl $3 */
    /* test ctx+0x2c -> windowed path: */
    c->dst  = ar_field(w0, wb, 0xc, 0xf);   /* dst  -> +0x24 : bits {12,13,14}+bank15 */
    c->src1 = ar_field(w0, wb, 0x8, 0xb);   /* src1 -> +0x48 : bits { 8, 9,10}+bank11 */
    c->src2 = ar_field(w0, wb, 0x4, 0x7);   /* src2 -> +0x50 : bits { 4, 5, 6}+bank 7 */
    return 0;                                            /* xor eax,eax ; ret      */
}
```

The slot bit-offsets across the scalar cells (low triple shown; the bank bit sits
one nibble above each, e.g. dst bank at bit 15/63/etc.):

| cell | addr | rep | slot bitoff | dst (→0x24) | src1 (→0x48) | src2 (→0x50) | conf |
|---|---|---|--:|---|---|---|---|
| `F0_S0_LdSt` | `0x29d220` | ADD | 4 | `{12,13,14}` | `{8,9,10}` | `{4,5,6}` | HIGH · OBSERVED |
| `F1_S0_LdStALU` | `0x7533d0` | ADD | 4 | `{12,13,14}` | `{8,9,10}` | `{4,5,6}` | HIGH · OBSERVED |
| `F2_S0_LdSt` | `0x3c6990` | ADD | 4 | `{12,13,14}` | `{8,9,10}` | `{4,5,6}` | HIGH · OBSERVED |
| `F3_S0_LdSt` | `0x4ae910` | ADD | 4 | `{12,13,14}` | `{8,9,10}` | `{4,5,6}` | HIGH · OBSERVED |
| `F0_S1_Ld` | `0x46f1e0` | ADD | 16 | `{60,61,62}` | `{16,17,18}` | `{20,21,22}` | HIGH · OBSERVED |
| `F1_S1_Ld` | `0x3794b0` | ADD | 16 | `{60,61,62}` | `{16,17,18}` | `{20,21,22}` | HIGH · OBSERVED |
| `F2_S1_Ld` | `0x59fb40` | ADD | 16 | `{60,61,62}` | `{16,17,18}` | `{20,21,22}` | HIGH · OBSERVED |
| `F3_S1_Ld` | `0x42b7f0` | ADD | 16 | `{26,29,30}` | `{16,17,18}` | `{20,21,22}` | HIGH · OBSERVED |
| `F0_S2_Mul` | `0x767f20` | ADD | 28 | `{45,67,68}` | `{64,65,70}` | `{41,42,43}` | HIGH · OBSERVED |
| `F3_S2_Mul` | `0x434130` | ADD | 28 | `{45,67,68}` | `{64,65,70}` | `{41,42,43}` | HIGH · OBSERVED |

* S0 starts at **bundle bit 4** in every format — bits `[3:0]` are the `op0` format
  selector, which the operand decode never touches (the minimum operand bit is 4,
  matching [FLIX VLIW Encoding §2.1](../isa/core/flix-encoding.md)).
* S1 (`Ld`) starts at **bundle bit 16**, matching the libisa `slots[]` `F*_S1_Ld`
  `bitoff = 16`.
* **`F3_S1` is the narrow 22-bit `Ld` slot** (libisa `slots[]` width 22); its dst
  field is packed at `{26,29,30}` rather than `{60,61,62}` — the
  `shr $0x1c & 6 \| shr $0x1a & 1 \| shr $0x1f` chain at `0x42b801..0x42b821` builds
  dst from word0/word1 boundary bits.

> **GOTCHA — the AR field is 4 bits, not 3.** Naming a scalar operand by its low
> triple alone (e.g. "dst = `{12,13,14}`") silently drops the **XOR-inverted high
> bank bit** at bit 15 (`shr $0xf ; xor $1 ; and $1 ; << 3`). A reimplementation that
> decodes only the low 3 bits caps the AR file at 8 and mis-routes every register
> ≥ 8. The full field is `{12,13,14}` *plus* inverted-bit-15, window-base added,
> masked `& 0x3f`. The same applies to src1 (bank bit 11) and src2 (bank bit 7).
> *(HIGH · OBSERVED — the `xor $1` is at `0x29d234`/`0x29d252`/`0x29d277`.)*

### 3.2 Vector IVP 3-register form

The vector slots (`ADD_S`, `MUL_S`, `IVP_*`) decode three **5-bit** vreg indices
scattered across the bundle. Each field is built as `(low3) \| (high2)` from two
bundle positions. The canonical ALU triple latches to `vt→0x94, vs→0x50, vr→0xd8`;
the dual-slot (`F3_S4`) and Mul forms use the parallel set `0x28/0x6c/0xb0`.

Annotated C reproducing `slotfill__F0__F0_S3_ALU_slot3__ADD_S` @`0x770e10`, with the
exact shifts read from the body (the slotfill reads `word1` @`ctx+0x04` and `word2`
@`ctx+0x08`):

```c
int slotfill__F0_S3_ALU__ADD_S(op_ctx *c) {            /* @0x770e10              */
    uint32_t w1 = c->word1, w2 = c->word2;
    /* vs -> +0x50 : low2 from word1, high3 from word2                          */
    c->vs = ((w1 >> 0x7) & 0x3) | ((w2 >> 0x9) & 0x1c);          /* {39,40} | {75,76,77} */
    /* vt -> +0x94 : low3 | high2  ==  ((sw<<28)>>29) | ((sw>>5)&0x18)          */
    c->vt = ((w1 >> 0x4) & 0x7) | ((w1 >> 0xf) & 0x18);          /* {36,37,38} | {50,51} */
    /* vr -> +0xd8 : 1 bit | 4 bits ==  (sw&1) | ((sw>>3)&0x1e)                 */
    c->vr = ((w1 >> 0x18) & 0x1) | ((w1 >> 0xd) & 0x1e);         /* {56} | {45,46,47,48} */
    /* + parallel bool-guard / lane-mask latches at +0x28/+0xac (predicate path) */
    return 0;
}
```

| cell | addr | rep | vt (→0x94) | vs (→0x50) | vr (→0xd8) | conf |
|---|---|---|---|---|---|---|
| `F0_S3_ALU` | `0x770e10` | ADD_S | `{36,37,38,50,51}` | `{39,40,75,76,77}` | `{56,45,46,47,48}` | HIGH · OBSERVED |
| `F1_S3_ALU` | `0x1c5730` | ADD_S | `{31,33,34,50,51}` | `{36,37,38,39,40}` | `{56,46,47,48,49}` | HIGH · OBSERVED |
| `F2_S3_ALU` | `0x4a4ad0` | ADD_S | `{31,33,34,50,51}` | `{36,37,38,39,40}` | `{56,46,47,48,49}` | HIGH · OBSERVED |
| `F3_S3_ALU` | `0x195290` | ADD_S | `0x94={79,33,52,50,53}` | `0x50={56,46,47,48,49}` | `0xd8={36,37,38,39,40}` | HIGH · OBSERVED |
| `F3_S4_ALU` | `0x1a0790` | IVP_ADDNX16 | `0x28={81,82,83,97,98}` | `0x6c={60,61,62,63,72}` | `0xb0={54,55,57,59}` | HIGH · OBSERVED |
| `F1_S2_Mul` | `0x1bf520` | ADD_S | `{70,71,72,73,74}` | `{41,42,43,44,45}` | `{64,65,66,67,68}` | HIGH · OBSERVED |
| `F2_S2_Mul` | `0x5a9ca0` | ADD_S | `{87,88,89,90,..}` | `{70,71,..}` | `{64,65,66,67,68}` | MED · INFERRED |
| `F0_S2_Mul` | `0x76c420` | IVP_MULNX16 | sub-op `0x28={28,29}` + vregs → `0xec`/`0x130` |  |  | HIGH · OBSERVED |

* Each vreg field is **5 bits → 32 IVP vector registers** (matching
  `opnd_sem_vec_addr & 0x1f`). The low 3 bits sit at the slot's nominal offset; the
  top 2 spill elsewhere in the bundle — FLIX operands are **scattered, not
  contiguous** ([FLIX VLIW Encoding §5](../isa/core/flix-encoding.md)).
* `F3` permutes which latch holds which field: the *latches* `vt@0x94`, `vs@0x50`,
  `vr@0xd8` are fixed, but in F3_S3 the `vr` field lands in `0xd8`, the `vs` field in
  `0x50`, the `vt` field in `0x94` — the **role↔latch mapping is format-local**, not
  fixed.
* The `_S` body also writes secondary bool-guard / lane-mask latches
  (`0x28/0x34/0x6c/0xac/0xb0`) duplicating the operand value — the per-lane `vbool`
  predicate path (the `_t` predicated variants named by the datapath analysis). The
  **primary** register latches are `0x50/0x94/0xd8` (ALU) and `0x28/0x6c/0xb0`
  (Mul/dual). *(Bit-extraction OBSERVED; predicate **purpose** is MED · INFERRED.)*

> **CORRECTION — `F0_S3_ALU` `vr` is `{56,45,46,47,48}`, not `{56,46,47,48,49}`.** A
> backing report listed `vr = {56,46,47,48,49}`. The body at `0x770e4a`–`0x770e55`
> reads `(word1 >> 0xd) & 0x1e` for the four high bits, i.e. `word1` bits
> `{13,14,15,16}` → bundle bits `{45,46,47,48}`, plus `(word1 >> 0x18) & 1` =
> `word1` bit 24 → bundle bit 56. So `vr = {56, 45, 46, 47, 48}` — the low four are
> 45–48, an off-by-one from the report. The latch (`0xd8`), the `(sw&1)\|((sw>>3)&0x1e)`
> *shape*, and the 5-bit width are all correct; only the absolute bit list shifts by
> one.

### 3.3 The sub-opcode selector case (`IVP_MULNX16`)

`slotfill__F0__F0_S2_Mul_slot2__IVP_MULNX16` @`0x76c420` is the requested
sub-opcode case. It decodes a **2-bit sub-opcode selector** at bundle bits
`{28,29}` (= `word0 >> 28 & 3`) — sitting exactly at the Mul slot's bit-offset 28 —
then two scattered 5-bit vreg fields into `ctx+0xec` and `ctx+0x130`:

```c
int slotfill__F0_S2_Mul__IVP_MULNX16(op_ctx *c) {        /* @0x76c420            */
    uint32_t w0 = c->word0, w1 = c->word1, w2 = c->word2;
    c->subop = (w0 >> 0x1c) & 0x3;                        /* {28,29} -> +0x28     */
    /* two 5-bit vreg fields, each low|high scattered across word1/word2:        */
    uint32_t a = ((w1 >> 0xa) & 0x7) | ((w2 << 0x2) & 0x18);  /* -> +0x130        */
    uint32_t b = ((w1 >> 0x9) & 0x1) | ((w1 >> 0xc) & ...);   /* -> +0xec         */
    c->vreg_a = a;
    c->vreg_b = b;
    return 0;
}
```

The `shr $0x1c ; and $0x3 ; mov %ecx,0x28(%rdi)` at `0x76c428` is the sub-opcode
extraction; the two vreg assemblies write `0x130(%rdi)` and `0xec(%rdi)`. The 2-bit
selector chooses among the `MULNX16` packing variants without a separate opcode
entry. *(HIGH · OBSERVED for the selector and latches; the exact high-bit assembly
of `vreg_b` is MED · INFERRED.)*

### 3.4 Immediate operands — sign-extension vs zero-extension

Operand **kind** is encoded by **extension mode**. Register indices are
*zero*-extended (`shr ; and`); signed immediates are *sign*-extended (`shl ; sar`).
From `slotfill__x24__Inst_slot0__ADDI` @`0x2963f0` (the RRI8 form; the same body is
the immediate template re-anchored in every `*_ALU` cell's `ADDI`/`ADDMI`):

```c
int slotfill__Inst__ADDI(op_ctx *c) {                    /* @0x2963f0            */
    uint32_t w0 = c->word0;
    c->dst  = ar_field(w0, c->window_base<<3, 0xc, 0xf); /* r -> +0x24 (zero-ext)*/
    c->src1 = ar_field(w0, c->window_base<<3, 0x8, 0xb); /* s -> +0x48 (zero-ext)*/
    /* t-position imm8 -> +0x50, SIGN-extended:                                  */
    int32_t imm = (int32_t)(w0 << 0x8) >> 0x18;          /* shl $0x8 ; sar $0x18 */
    c->src2 = imm;                                        /* mov %eax,0x50(%rdi)  */
    return 0;
}
```

`shl $8` places imm bit 7 in bit 31; `sar $0x18` is an **arithmetic** right shift of
24, sign-extending the 8-bit field to 32 bits (Xtensa `ADDI imm8`, range −128..127).
The exact bytes are `c1 e0 08` (`shl $0x8,%eax`), `c1 f8 18` (`sar $0x18,%eax`),
`89 47 50` (`mov %eax,0x50(%rdi)`). The sibling `ADDMI` uses `sar $0x10` (16-bit
shift → `imm << 8` form). The slotfill stores the architecturally-meaningful value,
never raw bits.
---

## 4. Encode/decode inverse cross-validation (slotfill ⇄ libisa-core)

The libisa-core disassembler exposes, per slot, a `get_fn` (scatter the slot's bits
out of the bundle into a normalized slotword) plus per-operand `Field_*_get`/`_set`
accessors (read/write a named field inside that slotword). The libfiss slotfill
**fuses** `get_fn ∘ field_get` into a single direct bundle→index pass. If the RE is
correct: (a) composing the libisa `get_fn` with the field accessor must reproduce
the slotfill's bundle bit positions, and (b) each field `_set` must **invert** its
`_get` — the `set == get⁻¹` property.

### 4a. Field-level inverse (the vector ALU registers, F0_S3_ALU)

The libisa field accessors for `Slot_f0_s3_alu`, read from machine code:

```c
/* Field_fld_ivp_sem_vec_alu_vr_Slot_f0_s3_alu_get @0x32bf60 */
uint32_t vec_alu_vr_get(uint32_t sw) { return  (sw & 1) | ((sw >> 3) & 0x1e); }
/* _vt_get @0x32c470 */
uint32_t vec_alu_vt_get(uint32_t sw) { return ((sw << 28) >> 29) | ((sw >> 5) & 0x18); }
/* _vs_get @0x331c70 */
uint32_t vec_alu_vs_get(uint32_t sw) { return  (sw << 17) >> 27; }
```

Composed with the slot `get_fn` `Slot_f0_Format_f0_s3_alu_36_get` @`0x3b0c70` (a
multi-lane scatter over bundle dwords `+4/+8/+0xc` — confirmed by disassembly; the
ALU slot is anchored above bit 32, so it does *not* read `word0`), the
bundle-absolute field bits come out:

| field | libisa `get_fn ∘ field_get` | slotfill latch | match |
|---|---|---|---|
| `vt` | `{36,37,38,50,51}` | `0x94` `{36,37,38,50,51}` | **CONFIRMED** |
| `vs` | `{39,40,75,76,77}` | `0x50` `{39,40,75,76,77}` | **CONFIRMED** |
| `vr` | `{56,45,46,47,48}` | `0xd8` `{56,45,46,47,48}` | **CONFIRMED** |

The `_set` accessors (`vr` @`0x32bf70`, `vt` @`0x32c490`, `vs` @`0x331c80`) were
emulated against their `_get`s: **`get(set(v)) == v` for all `v ∈ 0..31`**, for all
three fields. Worked with the byte-grounded `get` formulas:

```text
$ python3  (immediates read from libisa disasm; full 5-bit sweep)
  vr: get(set(v))==v for all 32 values -> True      # set: (v&1) | (((v>>1)&0xf)<<4)
  vt: get(set(v))==v for all 32 values -> True      # set: ((v&7)<<1) | (((v>>3)&3)<<8)
  vs: get(set(v))==v for all 32 values -> True      # set: (v&0x1f)<<10
```

Each field's `set` is the exact bit-inverse of its `get` — the decode (slotfill)
losslessly inverts the encode (`field_set`). *(HIGH · OBSERVED for the `get`
formulas read from the binary; the inverse is proven by exhaustive 32-value
emulation.)*

> **NOTE — `vr_set` is byte-confirmed, not just emulated.** `vec_alu_vr_set`
> @`0x32bf70` is `lea (,%rsi,8),%eax ; and $1,%esi ; and $0xf0,%eax ; and $0xe,%dl ;
> or ; or ; mov %esi,(%rdi)`: it writes `v&1` → slotword bit 0 and `(v>>1)&0xf` →
> slotword bits `[7:4]`, masking the target with `0x0e`/`0xf0`. The `get`
> (`(sw&1)\|((sw>>3)&0x1e)`) reads precisely those positions back. The round-trip is
> structural, not coincidental.
### 4b. Per-slot cross-checks (libisa composed vs slotfill latch)

| cell | libisa `get_fn ∘ field` | slotfill latches | verdict |
|---|---|---|---|
| `F1_S2_Mul` | vt`{70-74}`/vs`{41-45}`/vr`{64-68}` | `0x94`/`0x50`/`0xd8` | CONFIRMED |
| `F1_S3_ALU` | vs`{36-40}`/vt`{31,33,34,50,51}`/vr`{46-49,+}` | `0x50`/`0x94`/`0xd8` | CONFIRMED |
| `F2_S3_ALU` | identical layout to `F1_S3_ALU` | same | CONFIRMED |
| `F3_S3_ALU` | vr`{36-40}`/vt`{79,33,52,50,53}`/vs`{56,46-49}` | `0xd8`/`0x94`/`0x50` | CONFIRMED |
| `F3_S4_ALU` | vt`{81-83,97,98}`/vs`{60-63,72}`/vr`{54,55,57,59}` | `0x28`/`0x6c`/`0xb0` | CONFIRMED |

A residual `±1` appears only where the emulator's `get_fn` slotword model drops a
high-bank bit on a couple of Mul-slot vector fields (e.g. `F2_S2_Mul`); the operand
*body* and the primary 4 of 5 register bits agree in every case. *(HIGH · OBSERVED
for the bodies; MED on the `±1` Mul cells.)*

### 4c. Format-framing inverse (encode template ⇄ decode trigger)

`Format_F0_encode` @`0x3b5810` loads the 16-byte template
`0f 00 00 01 00 …` from `.rodata` — `byte0 = 0x0F` (op0 = 0xF), `byte3 = 0x01`
(b3lo = 1). This is exactly the F0 decode trigger
([FLIX VLIW Encoding §3](../isa/core/flix-encoding.md): F0 = op0 F, b3lo ∈ {1,5}).
The encode side fixes bits `[3:0] = 0xF` as the format selector; the slotfill
operand decode reads only bits `≥ 4` — so the selector nibble and the operand
fields are **disjoint**, and encode/decode framing is mutually consistent.
**CONFIRMED.**

### 4d. Scalar inverse (byte-identity)

`slotfill__F0__F0_S0_LdSt_slot0__ADD` @`0x29d220` is **byte-identical** to
`slotfill__x24__Inst_slot0__ADD` @`0x296320` — verified by `objdump -s` over the
full 0x88-byte body (`diff` empty). The base-ISA scalar decoder is reused verbatim
at slot bit-offset 4; the AR fields `{4,5,6}`/`{8,9,10}`/`{12,13,14}` are the
standard Xtensa RRR `t`/`s`/`r` positions, confirming the slot framing (S0 @ bit 4)
inverts the `x24` base encoding. **CONFIRMED.**

> **GOTCHA — only the plain `ADD` clones are byte-identical.** `x24 ADDI` @`0x2963f0`
> is *not* byte-identical to `F0_S0 ADDI` (same algorithm, different register
> allocation/ordering). The byte-identity holds for the simplest RRR bodies; do not
> assume it for every mnemonic. The *field math* is shared; the exact instruction
> scheduling is per-symbol.
No mismatch was found in any cross-check above except the documented `±1` Mul-slot
high-bank residual (§4b) and the corrected `F0_S3_ALU vr` bit list (§3.2). The
field bodies and the primary register bits agree everywhere.

---

## 5. Operand-struct handoff — how slotfill output feeds the pipeline

The five per-mnemonic stages share one context, keyed by the indices the slotfill
wrote. End-to-end for the vector `add_h` (F0_S3_ALU class):

```text
slotfill__...ADD_S  @0x770e10   bundle -> index latches  vt->0x94 vs->0x50 vr->0xd8   (DECODE)
stateload__add_h    @0x7eb0f0   config/window arch-state -> context                   (STATE)
regload__add_h      @0x7eb160   for L in {0x94,0x50,0xd8}:                             (GATHER)
                                  idx=ctx[L]; base=*(*(ctx_machine+0x8)+0x10);
                                  for lane 0..15: ctx[L+4 + 4*lane] = base[idx*16+lane]
opcode__add_h__stage_5 @0x7eaf90 reads value blocks; calls module__xdref_add_...@plt   (EXECUTE)
writeback__add_h    @0x7eb300   dstidx=ctx[0x50]; for lane: base[dstidx*16+lane]=...   (SCATTER)
```

So the slotfill is the **only** producer of operand indices; `regload` turns indices
into 512-bit values; `opcode__*_stage_5` calls the `module__xdref_*` leaf per lane
([The fiss Datapath Oracle](./fiss-datapath-oracle.md)); `writeback` commits the
result lanes back into `regfile[index]`. **The slotfill is the decode root of the
entire per-instruction pipeline.**

> **QUIRK — vector dst overlaps a source latch.** For the IVP vector ALU form the
> destination index is re-read from `ctx+0x50` (the `vs` latch) by `writeback`: the
> encoding overlaps dst with the `vt/vs` field group. The scalar analogue writes its
> result through `ctx+0x24` (dst). A reimplementation must not assume a separate dst
> latch for the vector forms.
---

## 6. Section map — the `.data` offset hazard (FISS)

`.text` and `.rodata` are VMA==file-offset, so `objdump`/`xxd` on the slotfill code
and `.rodata` templates works directly. The writable sections carry the ncore2gp
`0x200000` delta — **not** libtpu's `0x400000`. Confirmed `readelf -SW`:

| Section | VMA | File off | VMA − fileoff |
|---|---|---|---|
| `.text` | `0x00190430` | `0x00190430` | 0 |
| `.rodata` | `0x0088ff00` | `0x0088ff00` | 0 |
| `.data.rel.ro` | `0x00c17e80` | `0x00a17e80` | **`0x200000`** |
| `.got.plt` | `0x00c8d000` | `0x00a8d000` | **`0x200000`** |
| `.data` | `0x00c8eb68` | `0x00a8eb68` | **`0x200000`** |

Every slotfill address quoted on this page is `.text` (≥ `0x190430`), so it is read
directly; only if you walk a `.data.rel.ro` dispatch pointer must you subtract
`0x200000` to reach the file bytes.
---

## 7. Confidence ledger

**HIGH · OBSERVED** — read from disassembly / `nm` / emulation:

* the 6,288 F0–F3 slotfill census and every per-(format×slot×unit) count (§1.1);
* the slotfill ABI (bundle dwords @`0x0–0xc`; scalar latches `0x24/0x48/0x50`;
  vector latches `0x50/0x94/0xd8` + `0x28/0x6c/0xb0`);
* the 4-bit scalar AR field (3 index + XOR-inverted bank bit, ×8 window-base,
  `& 0x3f` → 64 AR) for all S0/S1/S2 scalar cells; the F3_S1 packed-dst special case;
* the immediate sign-extension (`shl 8 ; sar 0x18`) vs register zero-extension;
* the vector `vr/vs/vt` 5-bit scatter for `F0/F1/F2/F3 S3`, `F3_S4`, `F1_S2_Mul`,
  and the `MULNX16` 2-bit sub-op selector at `{28,29}`;
* the field-level `set == get⁻¹` inverse (`vr/vt/vs`, all 32 values);
* the libisa-composed bundle-bit cross-checks (CONFIRMED, no mismatch beyond the
  documented `±1`);
* the byte-identity of `F0_S0` scalar ADD with `x24` ADD; the F0 encode-template
  framing inverse.

**MED · INFERRED** — flagged:

* the exact high-bank (5th) bit of a couple of Mul-slot vector fields
  (`F2_S2_Mul`), where the emulator's `get_fn` slotword model is lossy, and the
  high-bit assembly of the `MULNX16` second vreg field;
* the bool-guard / lane-mask secondary latches (`0x28/0x34/0xac`) are the per-lane
  `vbool` predicate path — extraction OBSERVED, **purpose** INFERRED;
* the 32-entry × 16-dword vector regfile size — inferred from the ×16 gather stride
  and 5-bit index width, not a declared bound.

The cross-validation against the reference cores (`libfiss-ref-base.so`) is on
[ref-vs-production diff](./ref-vs-production-diff.md); the runnable-oracle wiring is on
[Runnable ISS Infrastructure](./runnable-iss-infra.md). The sibling slotfill pages cover the remaining formats:
[F4/F6/F7/F11](./fiss-slotfill-f4-f11.md) and the
[narrow N0/N1/N2](./fiss-slotfill-n0-n2.md) forms; the fiss surface and exception
model is on [libfiss-base Surface + Exception Model](./fiss-surface-exceptions.md);
the encode-side codec ABI is on
[The libisa Table Schema & Codec ABI](../isa/core/libisa-table-schema.md).
