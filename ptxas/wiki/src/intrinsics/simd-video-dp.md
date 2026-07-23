# SIMD Video & Dot-Product Lowering

> *Lowering observed by feeding PTX to ptxas v13.1 (CUDA 13.1.115) at `-arch sm_75/sm_89/sm_90a` and disassembling the resulting cubin with nvdisasm. Other versions will differ.*

This page documents how ptxas lowers the PTX SIMD video instruction family
(`vadd`/`vsub`/`vabsdiff`/`vmin`/`vmax`/`vshl`/`vshr`/`vmad`/`vset`, their
packed `.v2`/`.v4` forms, and `vavrg2`/`vavrg4`) and the integer dot-product
instructions (`dp4a`, `dp2a.lo`, `dp2a.hi`) into SASS. These are the
historically least-documented part of the lowering because, on every
architecture ptxas still targets (sm_75 Turing through sm_120 Blackwell), the
scalar video instructions have **no native SASS opcode** — they are open-coded
into integer-pipe sequences, and only one packed form survives as a hardware op.

The dispatch path is the classical-intrinsic opcode table (`sub_5D4190`, named
opcode hash at context `+808`): each video mnemonic resolves to a per-instruction
codegen handler, and `dp2a.lo`/`dp2a.hi`/`dp4a` resolve to `sub_56BA60` /
`sub_56C8D0` / `sub_577BA0`. See [Intrinsic Table](index.md) for the table.

## The two universal building blocks

Two SASS idioms recur in every video lowering. Recognizing them is enough to
read any of the sequences below.

### 1. The byte/half selector: `PRMT Rd, Ra, 0x3210, RZ`

PTX video ops carry a **lane selector** on each source (`.b0`/`.b1`/`.b2`/`.b3`
for byte lanes, `.h0`/`.h1` for half lanes, or a full-word `.b3210` default).
ptxas materializes that selection with the byte-permute instruction `PRMT`:

```
PRMT Rd, Ra, <sel>, Rb
```

`PRMT` builds `Rd` by picking, for each of the 4 output bytes, one byte from the
8-byte concatenation `{Rb, Ra}` according to the 4-nibble immediate selector.
The default full-word selection is `0x3210` (identity: byte *i* of `Ra`). Sign
extension of a selected sub-word lane is expressed by a selector nibble with the
high bit set (e.g. `0x7` replicates the sign of byte 0; `0x654` / `0x10` patterns
appear for half-word sign/zero extension). Every scalar video op begins by
running each input register through one such `PRMT` to extract and align the
operand lane before doing the arithmetic in the full 32-bit integer pipe.

### 2. The secondary-op merge (`.add` / `.min` / `.max`)

PTX video ops take an optional **secondary operation** that combines the primary
result with the third source `c`. The common case `.add` is lowered as a trailing
`IADD3` / `IMAD.IADD Rd, Rresult, 1, Rc`. For the one packed form that maps to a
native op (`vabsdiff4.u32`), the `.add` instead folds into the hardware
accumulator (`.ACC`, third source operand) of the SASS instruction itself, so no
extra add is emitted.

## Scalar (single-lane, 32-bit) video ops — fully emulated

On sm_75 and every later architecture the scalar `v*` ops (the `.s32`/`.u32`
single-lane forms with byte/half/word selectors) have **no native SASS opcode**.
ptxas open-codes each into a `PRMT`-prefixed integer sequence. The following are
the confirmed lowerings (selectors omitted; full-word `.b3210` shown).

| PTX (scalar) | SASS sequence (after the two `PRMT` selector ops) | Notes |
|---|---|---|
| `vadd.s32.s32.s32` | `IADD3 Rd, Ra', Rb', RZ` | plain add of the selected lanes |
| `vsub.s32.s32.s32` | `IADD3 Rd, Ra', -Rb', RZ` | negate second source |
| `vabsdiff.s32` | `IADD3 P, Ra', -Rb'` + sign-fixup `IADD3`/`SEL`, or `IABS` of the difference | absolute difference; signed needs sign-extended carry |
| `vmin.s32`/`vmax.s32` | `ISETP.{LT,GT}` + `SEL` (or `IMNMX`) | direction in the compare; signed uses `.EX` carry chain |
| `vshl.*.clamp` | `ISETP.LT.U32 P, Rb', 0x20` + `SEL Rsh, Rb', 0x20, P` + `SHF.L.U32 Rd, Ra', Rsh, RZ` | clamp shift amount to ≤ 32 before shifting |
| `vshl.*.wrap` | `LOP3.LUT Rsh, Rb', 0x1f, RZ, 0xc0` + `SHF.L.U32 Rd, Ra', Rsh, RZ` | wrap shift amount mod 32 (`&0x1f`) |
| `vshr.*.clamp` | clamp as above + `SHF.R.S64`/`SHF.R.U32` | arithmetic vs logical from `.s`/`.u` |
| `vset.<cmp>` | `ISETP.<cmp>` (with `.EX` for signed) + `SEL Rd, RZ, 0xffffffff, !P` then `SEL Rd, Rd, 1, P` | compare → 1/0 result in a data register |
| `vmad.s32` | `IMAD Rd, Ra', Rb', Rc` | multiply-add of selected lanes |
| `vmad.*.shrN` | `IMAD.WIDE Rt, Ra', Rb', Rc` + `SHF.R Rd, Rt, N` | wide multiply then shift-right by 7 or 15 |

**Secondary `.add`.** A scalar `v*.add` appends `IMAD.IADD Rd, Rprimary, 1, Rc`
(or `IADD3`) merging the third source after the primary op. A scalar `vset.<cmp>.add`
appends the same `IMAD.IADD` after the `SEL`.

**Saturate `.sat`.** `vadd.sat`/`vabsdiff.sat` etc. add a clamp to the signed
32-bit range: `ISETP` against `0x7fffffff`/`0x80000000` plus `SEL`, around the
primary op.

Worked example — `vabsdiff.s32.s32.s32` on sm_89:

```
PRMT  R0, R0, 0x3210, RZ            ; select lane of a
PRMT  R5, R2, 0x3210, RZ            ; select lane of b
IADD3 R8, P0, R0, -R5, RZ           ; a - b (with borrow)
SHF.R.S32.HI R2, RZ, 0x1f, R0       ; sign(a)
SHF.R.S32.HI R7, RZ, 0x1f, R5       ; sign(b)
IMAD.X R4, R2, 0x1, ~R7, P0         ; sign-extended high diff
IADD3 R11, P0, RZ, -R8, RZ          ; -(a-b)
ISETP.LT.AND P3, PT, R4, RZ, PT     ; diff < 0 ?
SEL   R11, R11, R8, P3              ; |a-b|
```

## Packed `.v2` / `.v4` video ops

The packed forms operate on two 16-bit lanes (`v*2`) or four 8-bit lanes
(`v*4`) inside one 32-bit register. Only **one** packed form is a native SASS
op on the architectures ptxas targets; the rest are per-lane `PRMT`-emulated.

| PTX (packed) | SASS | Native? |
|---|---|---|
| `vabsdiff4.u32.u32.u32[.add]` | `VABSDIFF4.U8[.ACC] Rd, Ra, Rb, Rc` | **native** (the only one) |
| `vabsdiff2.*`, `vabsdiff4.s32.*` | per-lane `PRMT` byte-split → `IADD3`/`IABS`/`SHF` → `PRMT` re-pack | emulated |
| `vmin4`/`vmax4` | 4× `PRMT` byte-extract → 4× `IMNMX` → `PRMT` re-pack | emulated |
| `vmin2`/`vmax2` | 2× `IMNMX` + `LOP3` lane masks | emulated |
| `vadd2`/`vadd4`/`vsub2`/`vsub4` | `IADD3` + `LOP3` lane-carry isolation, byte/half re-pack | emulated |
| `vavrg2`/`vavrg4` | per-lane `PRMT` extract → `IADD3` + rounding `IADD3`/`SHF.R` average → re-pack | emulated |

For `vabsdiff4.u32.u32.u32.add`, ptxas emits exactly:

```
VABSDIFF4.U8.ACC Rd, Ra, Rb, Rc     ; sum of |a_i - b_i| over 4 byte lanes, + Rc
```

The `.U8` sub-mode is the byte-lane width and `.ACC` is the accumulator that
absorbs the PTX `.add` secondary op (the third source). Without `.add` the same
`VABSDIFF4.U8` is emitted with `RZ` as the accumulator.

## Integer dot-product: `dp4a`, `dp2a.lo`, `dp2a.hi`

The dot-product instructions are a clean 1:1 lowering to the native SASS integer
dot-product op `IDP`, with the PTX accumulator `c` folded into the instruction's
third source (no separate add). The format suffixes encode the lane widths and
signedness of each source.

| PTX | SASS | Lane geometry |
|---|---|---|
| `dp4a.u32.u32 d, a, b, c` | `IDP.4A.U8.U8 Rd, Ra, Rb, Rc` | 4×(8-bit·8-bit), accumulate into 32-bit `c` |
| `dp4a.s32.s32 d, a, b, c` | `IDP.4A.S8.S8 Rd, Ra, Rb, Rc` | signed bytes |
| `dp2a.lo.u32.u32 d, a, b, c` | `IDP.2A.LO.U16.U8 Rd, Ra, Rb, Rc` | 2×(16-bit `a` lane · low-byte-pair of `b`) |
| `dp2a.hi.s32.s32 d, a, b, c` | `IDP.2A.HI.S16.S8 Rd, Ra, Rb, Rc` | high-byte-pair of `b`, signed |

The first format suffix is the `a`-source width (`U8`/`S8` for `dp4a`,
`U16`/`S16` for `dp2a` because `a` holds two 16-bit lanes), the second is the
`b`-source byte width (`U8`/`S8`), and `.LO`/`.HI` selects which byte pair of `b`
participates in the `dp2a` two-element dot product. Mixed-sign forms
(`dp4a.u32.s32`, etc.) produce the corresponding mixed suffix
(`IDP.4A.U8.S8`). `IDP` issues on the integer pipe; the third source `Rc` is the
running accumulator, so the PTX `d = c + dot(a, b)` is a single instruction.

> **Why dot-products are native but video ops are not.** The hardware retains a
> dedicated 4-lane byte multiply-accumulate (`IDP`) and a byte sum-of-absolute-
> differences (`VABSDIFF4`) because those two patterns are the inner loops of
> integer convolution / motion estimation. The broader scalar video ISA from the
> Fermi/Kepler era was dropped from the datapath; ptxas keeps the PTX surface
> syntax working by open-coding it on the general integer pipe.

## Architecture notes

- The scalar video lowerings are **identical** on sm_75 (Turing) and sm_89
  (Ada); both emulate. The native `VABSDIFF4.U8` and `IDP.{2A,4A}` ops are
  present across sm_75 → sm_90a.
- `vshl`/`vshr` require an explicit `.clamp` or `.wrap` modifier in PTX; ptxas
  rejects the bare form. `.clamp` produces the `ISETP.LT.U32 …0x20` + `SEL`
  shift-amount clamp; `.wrap` produces the `LOP3 …0x1f` mask.
- The SM100+ OCG builtin `viadd` / `viaddmax` / `vimnmx` family
  (see [OCG Intrinsic System](ocg.md)) is a *different* mechanism: those are
  Blackwell uniform/vector-integer-pipe builtins reached through the
  `__nv_ptx_builtin_ocg_*` path, not the PTX `v*` SIMD-video surface lowered
  here.

## Cross-References

- [Intrinsic Table](index.md) — the opcode hash (`sub_5D4190`) that routes each video / dp mnemonic to its codegen handler
- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) — `PRMT` is MercConverter opcode 24; the integer-pipe ops (`IADD3`, `IMAD`, `SHF`, `IMNMX`, `LOP3`, `ISETP`, `SEL`) are documented in the lowering-rules table
- [Convert (`cvt`) Lowering](cvt-lowering.md) — the `F2F`/`F2I`/`I2F`/`I2I` family
- [OCG Intrinsic System](ocg.md) — the Blackwell `viadd`/`vimnmx` OCG builtins
