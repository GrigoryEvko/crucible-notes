# The KLR→BIR Codegen Enum-Translation Crosswalk

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`, GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`). The five translator helpers and the four `klr::to_string` name tables are `DYNSYM T` exports recovered with `nm -DC`; the bodies and backing `.rodata` tables are read straight off the ELF. For `.text` (`Addr==Off==0x62d660`) and `.rodata` (`Addr==Off==0x1c72000`), virtual address equals file offset, so `xxd -s <VA>` reads the right bytes. The cp311/cp312 builds carry the identical logic but their VAs drift; the ordinals, table contents, and mapping are version-stable.*

## Abstract

When the backend lowers a `klr` AST node into a `bir::Inst*`, it crosses an **enum boundary**: the `klr` front-end numbers its `Dtype`, `Engine`, `AluOp`, `ActivationFunc`, and `AccumCmd` enums one way (alphabetically, 1-based), and `libBIR` numbers the corresponding enums another way (by hardware/size class). The bridge is **five tiny `KlirToBirCodegen::codegen*` helpers** in `libwalrus.so`, each a `DYNSYM`-exported leaf that turns a `klr` ordinal into a BIR ordinal. They are the numeric glue every per-op codegen visitor calls before it can stamp an engine, a dtype, an ALU op, an activation func, or an accumulation command into a BIR instruction.

Four of the five are **value tables**: a `.rodata` `u32[]` indexed by the (already base-adjusted) `klr` ordinal, the cell *being* the BIR ordinal. The fifth, `codegenAccumCmd`, is an **inlined `switch`** realized as a relative-offset jump table that returns immediates — and it is the one with teeth, because it does not merely reorder: it **reroutes** `klr Accumulate` onto BIR `AddAccumulate(4)` (not `Accumulate(2)`) and **hard-rejects** `klr LoadAccumulate`.

This page reproduces all five mappings byte-exact, names each helper and backing table at its VA, gives the bound check and out-of-range policy for each, and cross-checks every mapping against the BIR enum rosters owned by the sibling pages. The single rule a reimplementer must not miss: **this is one of three enum id-spaces.** `codegen*` produces the *BIR-IR-level* ordinal; a second, downstream remap then turns that BIR ordinal into the *silicon wire byte* (the dtype wire-tag of [dtype-tables](dtype-tables.md), the `ALU_OP` byte of [aluop-modes](aluop-modes.md), the PWP-id of [op-family-enums](op-family-enums.md)). Reading a `codegen*` result as a silicon byte — or vice-versa — is a correctness bug.

For reimplementation, the contract is:

- The five helper VAs, their argument base-adjustment, bound, backing table, and out-of-range behaviour.
- The exact `klr → BIR` ordinal map for each of the five enums, dumped from `.rodata`.
- The three behaviourally significant special cases: the Dtype `e4m3` alias collapse, the AluOp comparison-family reorder, and the AccumCmd `Accumulate→AddAccumulate` reroute + `LoadAccumulate` rejection.
- The boundary discipline: `codegen*` is the L1(klr)→L1(BIR) step only; the L3 silicon remaps live on the sibling enum pages.

| | |
|---|---|
| **`codegenActivationFunc`** | `0xf14040` — table `0x1de9840` (25 × `u32`), bound `≤0x18`, OOR → BIR `30` (Unknown) |
| **`codegenDtype`** | `0xf14060` — table `0x1de97e0` (20 × `u32`), bound `≤0x13`, OOR → THROW |
| **`codegenAluOp`** | `0xf140c0` — table `0x1de9760` (29 × `u32`), bound `≤0x1c`, OOR → THROW |
| **`codegenEngine`** | `0xf140f0` — table `0x1de9740` (6 × `u32`), bound `≤0x05`, OOR → BIR `0` (Unassigned) |
| **`codegenAccumCmd`** | `0xf14110` — jump table `0x1de9580` (6 × `i32`), bound `≤0x05`, two arms THROW |
| **`klr::to_string` names** | Dtype `0xf7d9c0` · AluOp `0xf7f2f0` · ActivationFunc `0xf7f6e0` · AccumCmd `0xf7f630` · Engine `0xf7f0b0` (all `DYNSYM T`) |
| **BIR rosters** | Dtype/AluOp/EngineType/EngineAccumulationType/ActivationFunctionType — `libBIR.so`, cross-ref below |
| **`.rodata` remap band** | `0x1de9580`–`0x1de9858`, contiguous (AccumCmd jt first, then the four value tables) |

---

## The helper shape

### Two arithmetic forms

The five helpers split into two implementation shapes.

**Value-table form** (Dtype, Engine, AluOp, ActivationFunc). The body is three instructions: a bound `cmp` against the table's last legal index, a `ja` to either a `throw` stub or a soft-default fall-through, then `mov (table_VA,%rsi,4),%eax` — a 4-byte indexed load whose result is the BIR ordinal. The body receives the *already base-adjusted* index in `%esi`; the `klr−1` (Dtype/AluOp/ActivationFunc, 1-based) or `klr−2` (Engine, 2-based) subtraction is folded into the calling convention at the visitor, so what the body proves is the **bound and the table**, not the subtraction. `codegenDtype` is the canonical instance:

```c
// KlirToBirCodegen::codegenDtype @0xf14060  (objdump-exact)
//   esi = (u32)(klrDtype - 1)   ← decrement performed at the call site
int codegenDtype(KlirToBirCodegen* this, uint32_t esi) {
    if (esi > 0x13)                       // f14063: cmp $0x13,%esi
        sub_6D67DC();                     // f14066: ja  → THROW std::runtime_error
                                          //              @0x1c7ddbc "Unsupported dtype encountered"
    return dword_1DE97E0[esi];            // f1406c: lea 0x1de97e0(%rip),%rax
}                                         // f14073: mov (%rax,%rsi,4),%eax ; ret
```

`codegenEngine` and `codegenActivationFunc` are the same skeleton with a *soft default* instead of a throw: they pre-load the default into `%eax` (`xor %eax,%eax` → `0` for Engine; `mov $0x1e,%eax` → `30` for ActivationFunc) **before** the `cmp`, so an out-of-range index falls straight through to `ret` carrying the default. `codegenAluOp` mirrors `codegenDtype` and throws on out-of-range, with its own message `"Unsupported ALU operation"` at `0x1c7ddda`.

**Jump-table form** (AccumCmd). `codegenAccumCmd @0xf14110` is an inlined `switch(klr)` over a 6-entry `i32` table of *table-relative* offsets:

```c
// KlirToBirCodegen::codegenAccumCmd @0xf14110  (objdump-exact)
int codegenAccumCmd(KlirToBirCodegen* this, uint32_t esi) {
    if (esi > 5) goto throw_unrecognized;            // f14117: cmp $0x5,%esi ; ja 0x6d6902
    rdx = 0x1de9580;                                 // f14120: lea base
    rax = (int32_t)off_1DE9580[esi] + rdx;           // f14129: movslq (%rdx,%rsi,4),%rax ; add %rdx,%rax
    goto *rax;                                        // f14130: jmp *%rax
    //  esi=1 → 0xf14148: return 0   (Idle)
    //  esi=2 → 0xf14168: return 1   (Zero)
    //  esi=3 → 0xf14158: return 4   (AddAccumulate)   ◄ remap, not 2
    //  esi=4 → 0xf14138: return 3   (ZeroAccumulate)
    //  esi=0 → 0x6d6902: THROW "Unrecognized accumulation cmd encountered!" (@0x1d1cc98)
    //  esi=5 → 0x6d6934: THROW "LoadAccumulate is not supported"            (@0x1d1cc78)
}
```

`codegenAccumCmd` is dispatched on the raw `klr` ordinal (it is **1-based but not pre-decremented**: arm index `1` is `klr Idle`), which is why its table has six entries with arm `0` reserved for the catch-all throw.

> **GOTCHA — two address frames for the same five helpers.** Each helper's `lea` target VA equals its file offset (`.rodata Addr==Off==0x1c72000`), and the `klr::*` to_string symbols sit in `.text` (`Addr==Off==0x62d660`). All five `codegen*` symbols also appear as a low-band thunk twin in the IDA listing (e.g. the `codegenAccumCmd` twin near `0x627a20`), and IDA-internal labels such as `sub_4F2100` sit *below* `.text` entirely — those are rebased names for the same logic the compiler inlines at the activation/DVE call sites. Cite the high-band `0xf140xx` frame; that is the real out-of-line body.

### General finding

Every one of the five tables is a **pure ordinal reorder**, not a value-identity. The `klr` enums are alphabetical within their class (Dtype/AluOp/ActivationFunc) or in a private insertion order (Engine/AccumCmd); the BIR enums follow their own canonical order (size-class for Dtype, hardware-pipeline for Engine, etc.). The only coincidental identity ordinals are `klr rsqrt 28 → bir 28` (AluOp) and `klr uint32 14 → bir 14` (Dtype).

---

## (1) `codegenDtype` → `bir::Dtype`

Backing table `dword_1DE97E0` (`.rodata` off `0x1777e0`, 20 × `u32`), `xxd -s 0x1de97e0` byte-exact:

```
0c 03 04 07 0d 10 11 01 0b 13 0f 00 0a 0e 12 04 05 09 08 02
= [12, 3, 4, 7, 13, 16, 17, 1, 11, 19, 15, 0, 10, 14, 18, 4, 5, 9, 8, 2]
```

`klr::Dtype` (1-based, 20 members, names from `to_string @0xf7d9c0`) → `bir::Dtype` (`0..19`, roster owned by [dtype-tables](dtype-tables.md)):

| klr | `klr::Dtype` | → bir | `bir::Dtype` | note |
|----:|--------------|------:|--------------|------|
| 1 | `bfloat16` | 12 | `bfloat16` | |
| 2 | `float8e3` | 3 | `float8e3` | |
| 3 | `float8e4` | 4 | `float8e4` | |
| 4 | `float8e5` | 7 | `float8e5` | |
| 5 | `float16` | 13 | `float16` | |
| 6 | `float32` | 16 | `float32` | |
| 7 | `float32r` | 17 | `float32r` | |
| 8 | `int8` | 1 | `int8` | |
| 9 | `int16` | 11 | `int16` | |
| 10 | `int64` | 19 | `int64` | |
| 11 | `int32` | 15 | `int32` | |
| 12 | `uint8` | 0 | `uint8` | |
| 13 | `uint16` | 10 | `uint16` | |
| 14 | `uint32` | 14 | `uint32` | **identity ordinal** |
| 15 | `uint64` | 18 | `uint64` | |
| 16 | `float8_e4m3` | 4 | `float8e4` | **alias → same bir ord as klr 3** |
| 17 | `float8_e4m3fn` | 5 | `float8_e4m3fn` | |
| 18 | `float8_e5m2_x4` | 9 | `float8_e5m2_x4` | |
| 19 | `float8_e4m3fn_x4` | 8 | `float8_e4m3fn_x4` | |
| 20 | `float4_e2m1fn_x4` | 2 | `float4_e2m1fn_x4` | |

**Remap notes.**
- Both `klr 3 float8e4` and `klr 16 float8_e4m3` collapse to **bir 4** — `e4m3` is the modern spelling of the legacy `float8e4`; `klr 17 float8_e4m3fn` is the distinct `fn`-variant (→ bir 5). The collapse is visible in the table image itself: cells `[2]` and `[15]` both hold `4`.
- **bir ordinal 6 (`float8_e8m0fnu`) is unreachable** from any `klr::Dtype` member — it is absent from the whole table image. The OCP-MXFP shared-exponent E8M0 scale type has no `klr` source; it is synthesized internally on the QuantizeMx path and never carried as a front-end klr dtype.
- The returned bir ordinal is **not** the silicon wire-tag. To reach the `NEURON_ISA_TPB_DTYPE` byte, compose this result through the 20-byte `byte_1DFBAD0` wire-tag LUT — that *second* remap is owned by [dtype-tables](dtype-tables.md).

The mapping agrees with [dtype-tables](dtype-tables.md): `klr uint8 → bir 0`, `int8 → bir 1`, `float4_e2m1fn_x4 → bir 2`, `float8e3 → bir 3` match that page's `bir::Dtype` ordinals 0–3 cell for cell.

---

## (2) `codegenEngine` → `bir::EngineType`

Backing table `dword_1DE9740` (`.rodata` off `0x177740`, 6 × `u32`), `xxd` byte-exact:

```
02 00 00 00  04 00 00 00  05 00 00 00  03 00 00 00  01 00 00 00  06 00 00 00
= [2, 4, 5, 3, 1, 6]
```

`klr::Engine` (2-based for the table; `to_string @0xf7f0b0`) → `bir::EngineType` (`0..7`, roster owned by [structural-enums](structural-enums.md)):

| klr | `klr::Engine` | `esi=klr−2` | table cell | → bir | `bir::EngineType` |
|----:|---------------|:-----------:|:----------:|------:|-------------------|
| 2 | `act` | 0 | 2 | 2 | `Activation` |
| 3 | `dma` | 1 | 4 | 4 | `DMA` |
| 4 | `dve` | 2 | 5 | 5 | `DVE` |
| 5 | `pe` | 3 | 3 | 3 | `PE` |
| 6 | `pool` | 4 | 1 | 1 | `Pool` |
| 7 | `sp` | 5 | 6 | 6 | `SP` |
| 1 | `unassigned` / OOR | (`esi=0xFFFFFFFF`) | — | 0 | `Unassigned` (soft default) |

**Remap notes.**
- The `klr` roster is alphabetical (`act/dma/dve/pe/pool/sp`); BIR's is the hardware-pipeline order `Pool(1)/Activation(2)/PE(3)/DMA(4)/DVE(5)/SP(6)` — the table reorders.
- `klr 1 unassigned` (and any out-of-range value) yields `esi = klr−2 = 0xFFFFFFFF > 5`, which falls through to the pre-loaded `xor %eax,%eax` default at `0xf140f3` → **bir `0` (Unassigned)**, no throw.
- **bir EngineType 7 (`ALL`, the all-engine barrier pseudo) is unreachable** from this scalar field — barriers carry their engine set differently.
- The result is stamped into `Instruction+0x90`; the engine ordinals here agree with [structural-enums](structural-enums.md)'s `EngineType` (`Pool=1 … SP=6`).

---

## (3) `codegenAluOp` → `bir::AluOpType`

Backing table `dword_1DE9760` (`.rodata` off `0x177760`, 29 × `u32`), `xxd` byte-exact:

```
1d 04 02 03 18 0a 01 0b 0c 00 07 12 15 14 17 16 0d 0e 10 11 0f 08 09 1b 06 13 1a 1c 05
= [29,4,2,3,24,10,1,11,12,0,7,18,21,20,23,22,13,14,16,17,15,8,9,27,6,19,26,28,5]
```

`klr::AluOp` (1-based, 29 members; `to_string @0xf7f2f0`) → `bir::AluOpType` (`0..32`, roster owned by [aluop-modes](aluop-modes.md)):

| klr | `klr::AluOp` | → bir | klr | `klr::AluOp` | → bir |
|----:|--------------|------:|----:|--------------|------:|
| 1 | `abs` | 29 | 16 | `is_lt` | 22 |
| 2 | `add` | 4 | 17 | `logical_and` | 13 |
| 3 | `arith_shift_left` | 2 | 18 | `logical_or` | 14 |
| 4 | `arith_shift_right` | 3 | 19 | `logical_shift_left` | 16 |
| 5 | `average` | 24 | 20 | `logical_shift_right` | 17 |
| 6 | `bitwise_and` | 10 | 21 | `logical_xor` | 15 |
| 7 | `bitwise_not` | 1 | 22 | `max` | 8 |
| 8 | `bitwise_or` | 11 | 23 | `min` | 9 |
| 9 | `bitwise_xor` | 12 | 24 | `mod` | 27 |
| 10 | `bypass` | 0 | 25 | `mult` | 6 |
| 11 | `divide` | 7 | 26 | `not_equal` | 19 |
| 12 | `is_equal` | 18 | 27 | `pow` | 26 |
| 13 | `is_ge` | 21 | 28 | `rsqrt` | 28 (**identity**) |
| 14 | `is_gt` | 20 | 29 | `subtract` | 5 |
| 15 | `is_le` | 23 | | | |

**Remap notes.**
- The **comparison family** is the salient reorder. `klr` alphabetises the predicates `is_equal(12), is_ge(13), is_gt(14), is_le(15), is_lt(16), not_equal(26)`, while BIR orders them `is_equal(18), not_equal(19), is_gt(20), is_ge(21), is_lt(22), is_le(23)`. The table threads them correctly: `is_ge klr13→bir21`, `is_gt klr14→bir20`, `is_le klr15→bir23`, `is_lt klr16→bir22`, `not_equal klr26→bir19`.
- This is the **BIR-IR-level (L1) reorder only.** A *third*, separate reorder happens downstream in the CoreV4 `ALU_OP` silicon-nibble encoder (`sub_142E030`, jump table `0x1dfb5e8`), where the comparison predicates are renumbered again so an operand swap maps `gt↔lt`/`ge↔le` onto adjacent wire codes — owned by [aluop-modes](aluop-modes.md). Do not conflate the two; the `klr is_ge → bir 21` here is *not* the silicon byte.
- `klr::AluOp` (29) is a **subset** of BIR's 33. **bir ordinals 25 (`elemwise_mul`), 30 (`abs_max`), 31 (`abs_min`), 32 (`mod_int`) are unreachable** from this table — all four are absent from the table image. The front-end has only generic `mult` (→ bir 6); [aluop-modes](aluop-modes.md) shows `25 elemwise_mul` is a distinct BIR-internal op, and the remaining three are likewise minted on other lowering paths.
- `codegenAluOp` does **no** per-op engine filtering; all 29 ops are legal for the `TensorTensor`/`TensorScalar` `op` field. Engine-legality is a downstream verifier/sim concern.

---

## (4) `codegenActivationFunc` → `bir::ActivationFunctionType`

Backing table `dword_1DE9840` (`.rodata` off `0x177840`, 25 × `u32`), `xxd` byte-exact:

```
18 0e 17 0c 16 07 10 12 15 0b 11 19 02 0a 05 0f 1b 1c 0d 08 09 01 06 13 14
= [24,14,23,12,22,7,16,18,21,11,17,25,2,10,5,15,27,28,13,8,9,1,6,19,20]
```

`klr::ActivationFunc` (1-based, 25; `to_string @0xf7f6e0`) → `bir::ActivationFunctionType` (`0..30`, 31 members, roster owned by [op-family-enums](op-family-enums.md)):

| klr | `klr::ActivationFunc` | → bir | `bir` name | note |
|----:|-----------------------|------:|------------|------|
| 1 | `abs` | 24 | `Abs` | |
| 2 | `arctan` | 14 | `Arctan` | |
| 3 | `copy` | 23 | `Copy` | identity-passthrough source |
| 4 | `erf` | 12 | `Erf` | |
| 5 | `erf_dx` | 22 | `Derivative_Erf` | **alias `*_dx → Derivative_*`** |
| 6 | `exp` | 7 | `Exp` | |
| 7 | `gelu` | 16 | `Gelu` | |
| 8 | `gelu_apprx_tanh` | 18 | `Gelu_apprx_tanh` | |
| 9 | `gelu_dx` | 21 | `Derivative_Gelu` | **alias** |
| 10 | `log` | 11 | `Ln` | **alias `log ≡ Ln`** |
| 11 | `mish` | 17 | `Mish` | |
| 12 | `reciprocal` | 25 | `Reciprocal` | |
| 13 | `relu` | 2 | `Relu` | |
| 14 | `rsqrt` | 10 | `Rsqrt` | |
| 15 | `sigmoid` | 5 | `Sigmoid` | |
| 16 | `sign` | 15 | `Sign` | |
| 17 | `silu` | 27 | `Silu` | |
| 18 | `silu_dx` | 28 | `Derivative_silu` | **alias** |
| 19 | `sin` | 13 | `Sin` | |
| 20 | `softplus` | 8 | `Softplus` | |
| 21 | `sqrt` | 9 | `Sqrt` | |
| 22 | `square` | 1 | `Square` | |
| 23 | `tanh` | 6 | `Tanh` | |
| 24 | `gelu_apprx_sigmoid` | 19 | `Gelu_apprx_sigmoid` | |
| 25 | `gelu_apprx_sigmoid_dx` | 20 | `Derivative_Gelu_apprx_sigmoid` | **alias** |

**Remap notes.**
- `klr` spells derivatives `*_dx`; BIR spells them `Derivative_*`, and `log → Ln`. All 25 resolve to a semantically identical BIR func (bir names per the [op-family-enums](op-family-enums.md) roster).
- `klr::ActivationFunc` (25) is a **subset** of BIR's 31. **bir ordinals 0 (`Identity`), 3 (`Lrelu`), 4 (`Prelu`), 26 (`Abs_reciprocal_sqrt`), 29 (`Is_finite`), 30 (`Unknown`) are unreachable** from this table — all six are absent from the table image, though each is a real bir member. `Identity` is reached via `copy (→ Copy 23)`, leaky/parametric relu and `is_finite` arrive on other lowering paths, and `30 Unknown` serves only as the soft-default sentinel.
- Out-of-range silently yields **bir `30` (Unknown)** — the body pre-loads `mov $0x1e,%eax` at `0xf14043`, before the bound `cmp`.
- This produces the BIR-IR `func` ordinal only; the **silicon PWP opcode-id is a separate downstream remap** (the PWP-id column of [op-family-enums](op-family-enums.md)). Activation therefore has **three** id-spaces: klr `ActivationFunc(1..25)` → BIR `ActivationFunctionType(0..30)` (this table) → silicon PWP-id.

---

## (5) `codegenAccumCmd` → `bir::EngineAccumulationType`

**Not** a value table — an inlined `switch(klr)` over jump table `off_1DE9580` (`.rodata` off `0x177580`, 6 × `i32` table-relative offsets), `xxd` byte-exact:

```
fe8ed382 ff12abc8 ff12abe8 ff12abd8 ff12abb8 fe8ed3b4   (signed i32, relative to 0x1de9580)
```

Decoded targets (`base 0x1de9580 + rel`):

| idx (klr) | rel (i32) | target | effect |
|:---------:|----------:|:------:|--------|
| 0 | −24194174 | `0x6d6902` | THROW `"Unrecognized accumulation cmd encountered!"` (`@0x1d1cc98`) |
| 1 | −15553592 | `0xf14148` | `return 0` (Idle) |
| 2 | −15553560 | `0xf14168` | `return 1` (Zero) |
| 3 | −15553576 | `0xf14158` | `return 4` (AddAccumulate) |
| 4 | −15553608 | `0xf14138` | `return 3` (ZeroAccumulate) |
| 5 | −24194124 | `0x6d6934` | THROW `"LoadAccumulate is not supported"` (`@0x1d1cc78`) |

`klr::AccumCmd` (1-based, 5; `to_string @0xf7f630`) → `bir::EngineAccumulationType` (`0..5`, roster owned by [structural-enums](structural-enums.md)):

| klr | `klr::AccumCmd` | → bir | `bir::EngineAccumulationType` |
|----:|-----------------|------:|------------------------------|
| 1 | `Idle` | 0 | `Idle` |
| 2 | `Zero` | 1 | `Zero` |
| 3 | `Accumulate` | **4** | `AddAccumulate` ◄ **reroute, not bir `Accumulate(2)`** |
| 4 | `ZeroAccumulate` | 3 | `ZeroAccumulate` |
| 5 | `LoadAccumulate` | — | **THROW** `"LoadAccumulate is not supported"` |
| 0 / other | — | — | **THROW** `"Unrecognized accumulation cmd encountered!"` |

**Remap notes (the most behaviourally significant of the five).**
- Not identity, not a simple `−1` shift. `klr Accumulate(3)` is rerouted to **bir `AddAccumulate(4)`** — the explicit-add PSUM read-modify-write variant — *not* bir `Accumulate(2)`. So **bir `EngineAccumulationType 2` (`Accumulate`) is never produced by this codegen path**; the front-end's generic "Accumulate" always canonicalises to the explicit-add ordinal. Arm `idx3` returns via `mov $0x4` at `0xf1415c`.
- `klr LoadAccumulate(5)` is a **defined** klr member (it has a `to_string` name) but is **rejected at codegen with a hard `runtime_error`** (arm `idx5` → throw at `0x6d6934`) — load-into-accumulator is not lowerable on this backend even though the BIR enum reserves ordinal 5 for it. A genuine front-end/back-end capability gap, not a reorder.
- The result is the BIR-JSON `acc` / `reduce_cmd` field (`InstActivation.acc`, `InstTensorScalarCache.acc`, `InstExponential`/`RangeSelect.reduce_cmd`, `InstCopyPredicated.accumulator_cmd`). The compiler inlines a copy of this 5-case switch at the activation/DVE call sites; the exported `codegenAccumCmd @0xf14110` is the canonical reference.

---

## Consolidated crosswalk summary

| Enum | klr | bir | reorder kind | unreachable bir ordinals (no klr source) | bad-value policy |
|------|----:|----:|--------------|------------------------------------------|------------------|
| **Dtype** | 20 | 20 | full perm | `6` (`float8_e8m0fnu`) | THROW |
| **Engine** | 7 | 8 | full perm | `7` (`ALL`) | soft `0` (Unassigned) |
| **AluOp** | 29 | 33 | perm + cmp-family reorder | `25,30,31,32` (`elemwise_mul,abs_max,abs_min,mod_int`) | THROW |
| **ActivationFunc** | 25 | 31 | full perm + alias renames | `0,3,4,26,29,30` (`Identity,Lrelu,Prelu,Abs_reciprocal_sqrt,Is_finite,Unknown`) | soft `30` (Unknown) |
| **AccumCmd** | 5 | 6 | switch + value remap | `2` (`Accumulate`; klr `Accumulate→4`) | THROW (klr5 + bad: 2 distinct msgs) |

All five backing tables sit in one contiguous `.rodata` **codegen remap band**, `0x1de9580`–`0x1de9858`: the AccumCmd jump table starts it (`0x1de9580`), then the four value tables follow — Engine (`0x1de9740`), AluOp (`0x1de9760`), Dtype (`0x1de97e0`), ActivationFunc (`0x1de9840`) — adjacent to the other per-op `codegenImmediate*` encoders.

**Relation to the sibling pages.** This page is the *klr-codegen-time* L1(klr)→L1(BIR) step. The broad L1/L2/L3 model and the four downstream L1≠L3 silicon families are owned by [op-family-enums](op-family-enums.md); the dtype wire-tag LUT by [dtype-tables](dtype-tables.md); the `ALU_OP` silicon-byte reorder by [aluop-modes](aluop-modes.md); the `EngineType`/`EngineAccumulationType` rosters by [structural-enums](structural-enums.md). Each `codegen*` result is the *input* to those downstream remaps, never their output.

---

## Limits of this reading

The helper VAs, the bound checks, the four table images, the jump-table targets and their throw strings, and the agreement with the BIR rosters are all read directly off the cp310 ELF. Three things on this page are weaker:

- **The per-helper base adjustment (`klr−1` / `klr−2`).** [INFERRED] The bodies receive an already-decremented `%esi`, so the subtraction happens at the visitor call site, not in the helper. It is reconstructed from the enum base (1-based vs 2-based) plus the table semantics; the call-site arithmetic is not re-disassembled here.
- **The `klr` enum member names.** Taken from the `to_string` symbol set rather than by walking each `to_string` switch body.
- **Which downstream path mints an "unreachable" bir ordinal.** [INFERRED] That `elemwise_mul`, `abs_max`, `abs_min`, `mod_int`, `Lrelu`, `Prelu`, `Is_finite` and `float8_e8m0fnu` are absent from these tables is settled; *who* emits them is out of scope for this page.
