# Convert (`cvt`) Lowering — Rounding & Type Matrix

> *Lowering observed by feeding PTX to ptxas v13.1 (CUDA 13.1.115) at `-arch sm_89` (and `sm_90a` for tf32/fp8 forms) and disassembling with nvdisasm. Other versions will differ.*

PTX `cvt` collapses the full Cartesian product of source type × destination type
× float-round mode (`.rn`/`.rz`/`.rm`/`.rp`) × int-round mode
(`.rni`/`.rzi`/`.rmi`/`.rpi`) × `.ftz` × `.sat` into four SASS conversion
opcodes plus their packed variants:

| SASS op | Direction | Packed variant |
|---|---|---|
| `F2F` | float → float (width change) | `F2FP` (narrow / packed-pair / fp8 / tf32) |
| `FRND` | float → integral float (same type, round-to-int) | — |
| `F2I` | float → integer | `F2IP` (small / packed integer dest) |
| `I2F` | integer → float (64-bit operand) | `I2FP` (32-bit operand) |
| `I2I` | integer → integer (with `.sat`) | — (most int↔int folds away) |

Rather than a giant table, the lowering is best expressed as a small set of
rules. The opcode is chosen by the **type-pair direction**; everything else
(round mode, ftz, sat, signedness, lane width) becomes a **modifier suffix** on
that opcode.

The `cvt` mnemonic resolves through the classical-intrinsic opcode table
(`sub_5D4190`, handler `sub_59F630`); the conversion's Ori opcode is one of
`F2F`/`F2I`/`I2F`/`I2I`, and the round/ftz/sat modifiers are carried in subop
fields (`F2F_SRC_SIZE` = 16/32/64, a source-unsigned flag for int sources,
`F2F_F16_SRC`/`F2F_F16_DST` half-width flags) that the SASS printer renders as
the suffixes below.

## Rule 1 — opcode is selected by type-pair direction

```
dst float, src float, different width         -> F2F   (F2FP if narrowing to a packed/sub-32b float)
dst float, src float, SAME type, int round    -> FRND  (round float to integral float; Rule 7)
dst integer, src float                        -> F2I   (F2IP if dst < 32 bits or packed)
dst float, src integer, 64-bit operand        -> I2F
dst float, src integer, 32-bit operands        -> I2FP
dst integer, src integer                      -> I2I (only when .sat present); otherwise folded away (Rule 6)
```

The selection depends **only on the float-ness of the source and destination
types**, never on the `cvt` mnemonic spelling. The float-side type appears in the opcode operand suffix
(`F2F.F32.F64`, `F2I.S8.F64`, `I2F.U64`, `I2FP.F32.S32`). The convention is
`OP.<dst-fmt>.<src-fmt>`; the destination format is omitted only when it is the
"natural" 32-bit form (e.g. `I2FP.F32.S32` carries both; `F2I.TRUNC.NTZ` for
`s32` omits the `.S32` because s32 is the default int dest).

## Rule 2 — float round mode → opcode suffix (default omitted)

For float destinations (`F2F`, `I2F`, `I2FP`):

| PTX | Suffix | Notes |
|---|---|---|
| `.rn` | *(none)* | round-to-nearest-even is the hardware default; never printed |
| `.rz` | `.RZ` | toward zero |
| `.rm` | `.RM` | toward −∞ |
| `.rp` | `.RP` | toward +∞ |

```
cvt.rn.f32.f64  -> F2F.F32.F64        (no suffix)
cvt.rz.f32.f64  -> F2F.F32.F64.RZ
cvt.rm.f32.f64  -> F2F.F32.F64.RM
cvt.rp.f32.f64  -> F2F.F32.F64.RP
cvt.rz.f32.s32  -> I2FP.F32.S32.RZ
```

**Float widening (`f32→f64`, `f16→f32`, …) carries no round mode** — the result
is exact, so `cvt.f64.f32` → `F2F.F64.F32` regardless of any (illegal-anyway)
round qualifier.

## Rule 3 — int round mode → opcode suffix (`.rni` default omitted)

For integer destinations (`F2I`, `F2IP`), the PTX integer-round suffix maps to:

| PTX | Suffix | Rounds |
|---|---|---|
| `.rni` | *(none)* | to nearest-even integer (default) |
| `.rzi` | `.TRUNC` | toward zero (truncate) |
| `.rmi` | `.FLOOR` | toward −∞ |
| `.rpi` | `.CEIL` | toward +∞ |

```
cvt.rni.s32.f32 -> F2I.NTZ
cvt.rzi.s32.f32 -> F2I.TRUNC.NTZ
cvt.rmi.s32.f32 -> F2I.FLOOR.NTZ
cvt.rpi.s32.f32 -> F2I.CEIL.NTZ
```

`F2I` always carries `.NTZ` ("no-trap-on-NaN-to-zero" — NaN maps to 0) on the
32-bit forms. The destination signedness/width is a suffix: `F2I.U32.TRUNC.NTZ`,
`F2I.S64.F64.TRUNC`, `F2I.S8.TRUNC.NTZ`. The 64-bit-source form spells the source
explicitly (`F2I.S64.F64.TRUNC`).

## Rule 4 — `.ftz` and `.sat` are independent modifier bits

- **`.ftz`** (flush subnormal inputs/outputs to zero) is a 1-bit flag, rendered
  as the `.FTZ` suffix. It is only meaningful when an f32 operand participates;
  on an f64-only conversion the bit is dropped.
  `cvt.rzi.ftz.s32.f32` → `F2I.FTZ.TRUNC.NTZ`.
- **`.sat`** (clamp to the destination range) is a 1-bit flag:
  - On `F2I`, saturation to the integer destination range is **implicit** in the
    op (any out-of-range float clamps to dest min/max), so `cvt.rzi.sat.s8.f32` →
    `F2I.S8.TRUNC.NTZ` — the `.S8` width *is* the clamp.
  - On `F2F` to `f32` (`cvt.sat.f32.f32`, clamp to `[0.0, 1.0]`) ptxas emits
    `FADD.SAT Rd, RZ, Ra` — the saturating add-with-zero idiom rather than an
    `F2F`.
  - On `I2I`, `.sat` is what *forces* an actual `I2I` to exist (Rule 6).

## Rule 5 — half / bf16 / tf32 / fp8 narrowing uses `F2FP`; half widening uses the half ALU

Narrowing a float to a sub-32-bit float type goes through the packing op `F2FP`:

```
cvt.rn.f16.f32          -> F2FP.F16.F32.PACK
cvt.rn.bf16.f32         -> F2FP.BF16.F32.PACK
cvt.rn.tf32.f32         -> F2FP.TF32.F32.PACK_B          (sm_90+)
cvt.rn.satfinite.e4m3x2.f32 -> F2FP.SATFINITE.E4M3.F32.PACK_AB_MERGE_C   (sm_90+, packs two f32 → fp8 pair)
cvt.rn.satfinite.e5m2x2.f32 -> F2FP.SATFINITE.E5M2.F32.PACK_AB_MERGE_C   (sm_90+)
cvt.rn.relu.f16x2.f32   -> F2FP.RELU.F16.F32.PACK_AB     (.relu clamps negatives to 0)
cvt.rn.f16x2.e4m3x2     -> F2FP.F16.E4M3.UNPACK_B        (fp8 → half, the inverse)
```

The `PACK`/`PACK_AB`/`PACK_AB_MERGE_C`/`UNPACK_B` sub-modes select how the
narrowed lanes are written into the (possibly two-lane) destination register.
`.relu` and `.satfinite` are extra suffixes. Exception — direct `f64`↔`f16`
**does** use `F2F` (no f32 hop) on current hardware:
`cvt.rn.f16.f64` → `F2F.F16.F64`, `cvt.f64.f16` → `F2F.F64.F16`.

Half **widening** to f32 (`cvt.f32.f16`) is not an `F2F`: ptxas uses the FP16
ALU, `HADD2.F32 Rd, -RZ, Ra.H0_H0` (add-with-zero on the half pipe selecting lane
`H0`), because the FP16→FP32 widen is free on the half datapath.

`cvt.rna.*` (round-to-nearest-away) has **no native suffix**: on tf32 it is
emulated — `HFMA2.MMA` bias add + `FSETP.GEU |x|,+INF` guard + conditional
`IMAD.IADD` + `LOP3 …0xffffe000` mantissa mask. (`.rna` is rejected for bf16 on
sm_89; needs sm_90.)

## Rule 6 — same-pipe int↔int is free (folded into load/store/consumer)

Integer-to-integer conversions whose value fits the register file (sign-extend,
zero-extend, truncate) usually emit **no compute instruction at all**. ptxas
folds the width change into:

- the **load** width/sign (`cvt.u32.u16` of a loaded value → `LDG.E.U16`
  zero-extends in the load; signed → sign-extends),
- the **store** width (`cvt.s16.s32` before a store → `STG.E.U16` truncates),
- or the consuming arithmetic's operand modifiers.

`cvt.s32.s16` of a register value materializes as a `PRMT` (byte/sign select)
only when no load/store/consumer can absorb it. The **only** int↔int form that
forces a real op is `.sat`:

```
cvt.sat.s8.s32  -> I2I.S8.S32.SAT   (+ PRMT byte-pack)
cvt.sat.u8.s32  -> I2I.U8.S32.SAT
```

## Rule 7 — round a float to an integral float → `FRND`, not `F2F`/`F2I`

When the source and destination float types are **identical** but an integer
round mode (`.rni`/`.rzi`/`.rmi`/`.rpi`) is applied, the result stays a float
whose value is the rounded integer. ptxas emits the dedicated round op `FRND`
with the **integer-rounding spelling** of the mode:

```
cvt.rni.f32.f32 -> FRND              (round-to-nearest-even, default omitted)
cvt.rzi.f32.f32 -> FRND.TRUNC
cvt.rmi.f32.f32 -> FRND.FLOOR
cvt.rpi.f32.f32 -> FRND.CEIL
cvt.rzi.f64.f64 -> FRND.F64.TRUNC
```

This is the key reason the same four PTX round tokens render two different ways
in SASS: when a conversion **changes type** the float spelling
(`.RN`/`.RZ`/`.RM`/`.RP`) prints; when it **rounds to an integral value in the
same type** (`FRND`) the integer spelling (default / `.TRUNC` / `.FLOOR` /
`.CEIL`) prints. The underlying round-mode encoding is one enum
(nearest=0, floor=1, ceil=2, trunc=3); only the printed suffix differs.

## Compact summary

```
F2F   = float<->float width change, exact when widening; narrow carries .RN/.RZ/.RM/.RP
FRND  = round float to integral float, SAME type; (none|TRUNC|FLOOR|CEIL)
F2FP  = float narrow to f16/bf16/tf32/fp8 (PACK/UNPACK/relu/satfinite), 2-lane
F2I   = float->int; round = (none|TRUNC|FLOOR|CEIL); dest width/sign = suffix;
        .NTZ always; .FTZ optional; saturation implicit in dest width
F2IP  = F2I to <32b / packed int dest
I2F   = int->float when a 64b operand is involved; .RN default
I2FP  = int->float for 32b operands; .RN default
I2I   = int->int ONLY with .sat; otherwise the conversion folds into ld/st/consumer
HADD2.F32 / FADD.SAT  = special idioms (f16->f32 widen; f32 saturate-to-[0,1])
```

## Cross-References

- [Math Intrinsics](math.md) — the `div`/`rcp`/`sqrt` software sequences that *use* these conversions internally
- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) — the round/ftz/sat modifier encoding (fields 345/301, encoders in the `0x10B` region) and the `cvt.f32.s32`→`I2F`, `cvt.s32.f32`→`F2I` rows of the lowering-rules table
- [SIMD Video & Dot-Product Lowering](simd-video-dp.md) — the other historically-undocumented family
- [Instruction Selection](../codegen/isel.md) — `F2F`/`F2I`/`I2F`/`I2I` opcode IDs in the MercConverter dispatch
