# MatmulMode and Modifiers

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol is a demangled C++ name. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset.*

## Abstract

A matrix-multiply on the TPU MXU is not one operation — it is a *sequence* of feed passes, one per significand slice of the operands. A bf16 matmul at full precision is three int8-emulated passes; an int8 matmul is up to eight byte-plane passes; an int4 matmul is four nibble-plane passes. `xla::jellyfish::MatmulMode` is the **16-ordinal enum that names each pass-role** — the per-slice feed that the lowering picks for one operand, and that the cost model prices distinctly. This page documents that enum, the way operand dtype maps to a candidate mode list, and the `MatmulModifier` / `MatpushModifier` keys that bind a mode (and its data format) to a reservation row in the [`MxuLatencyTable`](mxu-latency-overview.md).

The reference frame is a software-emulated wide multiply: to multiply two bf16 values on hardware that only supports a narrower mantissa, you split each operand into significand slices, multiply the slices, and recombine with shifts — exactly the "bf16x3" / "int8x8" trick. `MatmulMode` enumerates the slices: `{Round, High, Low, Soft Middle Eight, Soft Low_eight}` are the bf16/fp32 precision passes (ordinals 0–4); `{Soft Byte k, Soft Signed Byte k}` are the int8 byte planes (ordinals 5–11); `{Nibble k, Signed Nibble k}` are the int4 nibble planes (12–15). Each ordinal carries a **weight** in a 16-entry table; the lowering forms the cross-product of the LHS and RHS candidate lists and `stable_sort`s the pairs by summed weight, so the cheapest precision pair is consumed first.

The second half of the page is the binding into the cost model. `MatmulDataFormat` is the *data-path width* code (bf16-packed, int8/x8, int4, fp8 variants) that a matmul or latch carries; `GetMatmulDataFormat` derives it from the operand dtype and the convolution lowering strategy. The `MatmulModifier` (matmul family) and `MatpushModifier` (latch / matprep family) are the keys the `MxuLatencyTable` lookup builds from that format, and they pick which reservation group — `{2,1,1}` bf16, `{4,3,2}` transposed, `{8,7,6}` x8 — prices the op. The two secondary tables — the matmul-format key list and the latch/vxpose format ordinals — are how the format byte is laid into the key.

For reimplementation, the contract is:

- The 16 `MatmulMode` ordinals, their display strings, and the 16-entry weight table that orders mode pairs.
- `GetMatmulModes(operand)`: the per-`PrimitiveType` candidate mode list, and the precision-driven default.
- `MatmulDataFormat`: the format codes and the `GetMatmulDataFormat` dtype/strategy dispatch that produces them.
- The `MatmulModifier`/`MatpushModifier` key bytes and the format → reservation-group binding.

| | |
|---|---|
| **MatmulMode enum** | `xla::jellyfish::MatmulMode` — 16 ordinals; printed by `operator<<` `@0x1d6294e0` |
| **Weight table** | `@0xae0f480` = `[5,4,3,2,1,40,40,30,30,20,10,10,40,40,40,40]` |
| **Pair comparator** | `ConvMatmulModes::operator<` `@0x130e12a0` — `W[lhs]+W[rhs]` |
| **Per-dtype mode list** | `SpatialMajorConvolution::GetMatmulModes(operand)` `@0x130dfbe0` (dtype jt `@0xae0f26c`) |
| **Cross-product + sort** | `GetMatmulModes()` `@0x130df600` — skip `{Low,Low}`, `stable_sort` by summed weight |
| **Data-format derivation** | `convolution_util::GetMatmulDataFormat` `@0x1307be40` (dtype jt `@0xae0d6f4`) |
| **Modifier key types** | `MatmulModifier` (8-byte key), `MatpushModifier` (4-byte key) |

---

## The 16 MatmulMode Ordinals

### Purpose

`MatmulMode` names the feed-role of one pass over one operand. The lowering picks a list of modes per operand from its dtype; the cost model prices the matmul by the chosen mode pair and the resulting `MatmulDataFormat`.

### The enum

`xla::jellyfish::operator<<(ostream&, MatmulMode)` `@0x1d6294e0` is a `jmp *jt[ord]` dispatcher over a 16-entry jump table (`@0xb53c6e4`); each case builds its display string inline. Decoded byte-exact and bound 1:1 to the 16-entry weight table `@0xae0f480`:

| Ord | MatmulMode | Weight | Group / feed role | Confidence |
|---:|---|---:|---|---|
| 0 | Round | 5 | bf16/fp32 — round-to-nearest, single pass | HIGH |
| 1 | High | 4 | bf16/fp32 — high-significand pass | HIGH |
| 2 | Low | 3 | bf16/fp32 — low-significand pass | HIGH |
| 3 | Soft Middle Eight | 2 | bf16 3-pass split — middle 8 bits | HIGH |
| 4 | Soft Low_eight | 1 | bf16 3-pass split — low 8 bits | HIGH |
| 5 | Soft Byte 2 | 40 | int8 ×8 — byte plane 2 | HIGH |
| 6 | Soft Signed Byte 0 | 40 | int8 ×8 signed — byte plane 0 | HIGH |
| 7 | Soft Byte 1 | 30 | int8 ×8 — byte plane 1 | HIGH |
| 8 | Soft Signed Byte 1 | 30 | int8 ×8 signed — byte plane 1 | HIGH |
| 9 | Soft Byte 0 | 20 | int8 ×8 — byte plane 0 | HIGH |
| 10 | Soft Byte 3 | 10 | int8 ×8 — byte plane 3 (top) | HIGH |
| 11 | Soft Signed Byte 3 | 10 | int8 ×8 signed — byte plane 3 | HIGH |
| 12 | Nibble 0 | 40 | int4 ×4 — nibble plane 0 | HIGH |
| 13 | Signed Nibble 0 | 40 | int4 ×4 signed — nibble plane 0 | HIGH |
| 14 | Nibble 1 | 40 | int4 ×4 — nibble plane 1 | HIGH |
| 15 | Signed Nibble 1 | 40 | int4 ×4 signed — nibble plane 1 | HIGH |

> **NOTE —** ordinal 4's display string is the literal `"Soft Low_eight"` (length 14): a source-level `"Soft Low" "_eight"` literal join written at overlapping stack offsets, so the space-then-underscore is in the shipping binary, not a decode artefact. Do not "correct" it to `Soft Low Eight`.

### The three semantic groups

- **{0–4} bf16/fp32 precision passes** (weights 5,4,3,2,1). `Round` is the single-pass; `High`/`Low` are the 2-pass significand split; `Soft Middle Eight` / `Soft Low_eight` complete the 3-pass int8-emulated bf16 (the high-accuracy bf16×3).
- **{5,7,9,10,11,6,8} int8 ×8 byte planes** — `Soft Byte k` unsigned, `Soft Signed Byte k` signed; ×8 = 4 byte planes 0–3 latched separately.
- **{12,13,14,15} int4 ×4 nibble planes** — `Nibble k` / `Signed Nibble k`.

> **QUIRK —** the weights do **not** track ordinal order. The bf16 group is cheap (1–5), the byte planes are mid-to-expensive (10–40), and the int4 nibbles plus signed-byte-0 are pinned at 40. This is deliberate: the comparator sums two weights and sorts ascending, so a bf16 pair always sorts before an int8 pair, and the lowering consumes the cheapest precision combination first.

### The pair comparator

`ConvMatmulModes::operator<` `@0x130e12a0` (re-verified byte-exact) compares two mode pairs by their summed operand weights:

```c
function ConvMatmulModes::operator<(a, b):    // @0x130e12a0, W[] @0xae0f480
    return W[a.lhs] + W[a.rhs] < W[b.lhs] + W[b.rhs];   // setb on int sums
```

The secondary key is `std::stable_sort` insertion order (LHS-outer × RHS-inner from `GetMatmulModes()`), so ties resolve to the order the cross-product produced them — confirmed by the `__stable_sort` / `__inplace_merge` instantiations over `ConvMatmulModes*` (`@0x130dfe40`, `@0x130e0760`).

---

## Per-Dtype Mode Lists — GetMatmulModes

### Purpose

`GetMatmulModes(operand)` produces the candidate `MatmulMode` list for one operand from its element type. The no-arg `GetMatmulModes()` then cross-products the LHS and RHS lists into `ConvMatmulModes` pairs.

### Algorithm

`SpatialMajorConvolution::GetMatmulModes(long operand)` `@0x130dfbe0` (byte-confirmed in the decompile) first short-circuits the depthwise cases, then dispatches on `element_type`:

```c
function GetMatmulModes(operand):                     // @0x130dfbe0
    if strategy.is_depthwise[+504] or is_batch_group_depthwise[+505]:
        return {Round}                                // mode 0
    switch operand.shape.element_type():              // dtype jump table @0xae0f26c
        case 1,6,22,27,31:        return {Soft Byte 2}              // mode 5     (byte=0x05)
        case 2,21,26,30:          return {Soft Signed Byte 0}       // mode 6     (byte=0x06)
        case 3  (S16):            return {Soft Byte 2, Soft Signed Byte 1}  // {5,8} (word=0x0805)
        case 4  (S32):            return {Soft Byte 2, Soft Byte 1, Soft Byte 0, Soft Signed Byte 3} // {5,7,9,11}
        case 7  (U16):            return {Soft Byte 2, Soft Byte 1}         // {5,7} (word=0x0705)
        case 8  (U32):            return {Soft Byte 2, Soft Byte 1, Soft Byte 0, Soft Byte 3} // {5,7,9,10}
        default:  // bf16 / fp16 / fp32 / fp8 — drive off the convolution precision
            switch GetConvPrecision(operand):         // @0x131916e0 → 0 / 1 / 2
                case 2: return {Soft Low_eight, Soft Middle Eight, High}   // {4,3,1} bf16×3 hi-acc
                case 1: return {Low, High}                                 // {2,1}   bf16×2
                else:   return {Round}                                     // {0}     bf16×1
```

The per-case packed bytes are read directly out of the decompile: case 3 writes the 16-bit immediate `2053` = `0x0805` → modes `{5,8}`; case 4 writes `0x0B090705` → `{5,7,9,11}`; case 8 writes `0x0A090705` → `{5,7,9,10}`; the precision-2 default writes `0x0304` plus a third byte `1` → `{4,3,1}`. The dtype jump table `@0xae0f26c` has 31 entries (`element_type − 1` index); the default arm covers every floating type.

The no-arg `GetMatmulModes()` `@0x130df600` cross-products the LHS × RHS lists, **skips the degenerate `{Low,Low}` = `{2,2}` pair**, and `stable_sort`s by `operator<` above. The result is the ordered list of `(MatmulMode lhs, MatmulMode rhs)` pairs the emitter walks.

> **GOTCHA —** the `{Low,Low}` skip is not optional. A 2-pass bf16 operand contributes `{Low, High}`; the cross-product would otherwise emit `{Low,Low}`, which produces no usable significand product. A reimplementation that keeps it emits a redundant pass and miscosts the matmul.

---

## MatmulDataFormat and the Modifier Keys

### Purpose

`MatmulDataFormat` is the data-path width code carried by a matmul or latch. It is distinct from `MatmulMode`: a mode is a per-significand-slice feed role; a format is the packing of the operand lane. The `MxuLatencyTable` modifier key is built from the **format**, not the mode — so the format byte is what selects the reservation group.

### The format codes

`GetMatmulDataFormat` (`convolution_util::GetMatmulDataFormat` `@0x1307be40`) derives the format from the operand `PrimitiveType` and the convolution lowering strategy (a two-stage dispatch: strategy packing bits short-circuit to format 1/2; otherwise a 22-entry dtype jump table `@0xae0d6f4`). The format codes (byte-confirmed targets):

| Format | Meaning | Reservation group |
|---:|---|---|
| 1 | bf16 packed | `{2,1,1}` (single bf16) |
| 2 | bf16 packed, alternate path | `{2,1,1}` |
| 3 / 9 | F8E4M3Fn (native / converted) | fp8 |
| 4 | F32 | wide |
| 5 / 7 | F8E5M2 (native / variant) | fp8 |
| 6 | int8 / x8 | `{8,7,6}` (x8 quad) |
| 8 | int4 unsigned | x4 |
| 10 | fp8 fnuz variant | fp8 |

The stage-2 dtype targets: `PrimitiveType 2 → 6`, `6 → 5`, `19 → 3 / 9` (native vs converted, gated by a `Target` vtable slot), `20 → 10`, `21 → 6 / 8` (int4 signed vs unsigned, `±2` on the strategy x4/x8 bits), `22 → 5 / 7`, `23 → 4`; every other type in 3..18 takes the `FATAL` arm.

### The modifier keys

`MxuLatencyTable::GetResourceUsage` builds one of two key structs depending on the op family (the lookup itself is documented on [`mxu-latency-overview`](mxu-latency-overview.md)):

| Key | Family | Width | Byte layout | Source helpers |
|---|---|---:|---|---|
| `MatmulModifier` | matmul | 8 B | byte[0] = format (1 / 2 / 6 in the VF matmul cases 212 / 218 / 230); byte[1..] = 0 | inline, format-key list `@0x84a2644` |
| `MatpushModifier` | latch / matprep | 4 B | byte[0] = `GainLatchModeToMatmulDataFormat(latch_mode)`; byte[1..2] = `LatchModeIsTranspose(latch_mode)`; byte[3] = `LatchOpcodeToMsr(0x8F)` | helpers below |

The matpush key is the format → reservation binding in action. `GainLatchModeToMatmulDataFormat` `@0x1d629260` maps the LLO `GainLatchMode` attribute (the per-pass latch role of [`slot-matprep-iar-latch`](../isa/slot-matprep-iar-latch.md)) to a `MatmulDataFormat` code, which becomes byte[0] of the key; `LatchModeIsTranspose` `@0x1d628ea0` sets the transpose bytes; `LatchOpcodeToMsr(0x8F)` `@0x1c8a1300` selects the matrix-staging register. The opcode that selects the matpush variant also pre-transforms the latch mode — `^0xB` for the xpose pass (VF opcode 271), `|0x14` for the wide pass (VF opcode 277) — routing the same physical latch into the transposed `{4,3,2}` or wide `{8,7,6}` reservation group.

### Format → reservation-group binding

The reservation triplet scales with the format's data-path width — the byte planes a single matpush must feed:

```text
format 1/2 (bf16 single)      → {res0:2, MSR_a:1, MSR_b:1}    = {2,1,1}
format (bf16 transposed/dbl)  → {res0:4, MSR_a:3, MSR_b:2}    = {4,3,2}
format 6 (int8 x8 / wide)     → {res0:8, MSR_a:7, MSR_b:6}    = {8,7,6}
```

So a bf16 matmul (`GetMatmulDataFormat → 1`) draws the `{2,1,1}` group, an int8/x8 matmul (`→ 6`) draws `{8,7,6}` — the 4× hold reflecting the 4-byte-plane x8 latch sequence. The actual per-`(format × MSR)` integers are on the per-gen pages.

---

## Secondary Tables

### Matmul-format key list

The matmul-family modifier key byte[0] is drawn from a small format-key list `@0x84a2644` (entries `01, 02, 03, 04, …`, loop bound `cmp $7`). The VF matmul cases pin three of these: opcode 212 → format 1, 218 → format 2, 230 → format 6 — the same three byte values the matmul map at `MxuLatencyTable this+0x20` is keyed on. The matmul-modifier map policy (a `pair<MatmulDataFormat,int>` array) is at `@0x21c20650`.

### Latch / vxpose format ordinals

The latch (matpush) and vector-transpose ops feed the format byte through `GainLatchMode`, not the matmul format-key list. `GainLatchModeToMatmulDataFormat` `@0x1d629260` is the jump table (`GainLatchMode` ordinal → `MatmulDataFormat`) that performs that conversion; the transpose flag is the separate `LatchModeIsTranspose` `@0x1d628ea0`. The exact `GainLatchMode` → format mapping for every latch ordinal is on [`slot-matprep-iar-latch`](../isa/slot-matprep-iar-latch.md).

> **NOTE —** there is a *second, distinct* `MatmulMode` in the binary: the 5-value `mlir::llo::MatmulMode` rounding-mode attribute (`round`, `high`, `low`, `soft_middle_eight`, `soft_low_eight`; `MatmulModeAttr::print` `@0x13e53d20`). It is the MLIR-attribute counterpart of the bf16 precision group (jellyfish ordinals 0–4) only — the int8/int4 feed modes (5–15) do **not** appear in the MLIR attribute, which carries them via `MatmulDataFormat` + `GainLatchMode` instead. Do not conflate the two enums.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `operator<<(ostream&, MatmulMode)` | `0x1d6294e0` | 16-case display dispatcher (jt `@0xb53c6e4`) | HIGH |
| `ConvMatmulModes::operator<` | `0x130e12a0` | pair comparator — `W[lhs]+W[rhs]` | CERTAIN |
| `SpatialMajorConvolution::GetMatmulModes(long)` | `0x130dfbe0` | per-dtype candidate mode list | CERTAIN |
| `SpatialMajorConvolution::GetMatmulModes()` | `0x130df600` | cross-product, skip `{Low,Low}`, `stable_sort` | HIGH |
| `convolution_util::GetConvPrecision` | `0x131916e0` | bf16 precision 0 / 1 / 2 selector | HIGH |
| `convolution_util::GetMatmulDataFormat` | `0x1307be40` | dtype + strategy → format code (jt `@0xae0d6f4`) | CERTAIN |
| `GainLatchModeToMatmulDataFormat` | `0x1d629260` | latch mode → format byte (matpush key byte[0]) | CERTAIN |
| `LatchModeIsTranspose` | `0x1d628ea0` | matpush key transpose bytes | HIGH |
| `LatchOpcodeToMsr` | `0x1c8a1300` | matpush key staging-register byte | HIGH |
| weight table | `0xae0f480` | `[5,4,3,2,1,40,40,30,30,20,10,10,40,40,40,40]` | CERTAIN |
| matmul-format key list | `0x84a2644` | `01,02,03,04,…` — matmul key byte[0] source | HIGH |

---

## Worked Example — bf16 matmul mode selection

`Dot(lhs[B=512,K=256], rhs[K=256,N=128])`, bf16, on Ghostlite:

```text
1. GetMatmulModes(lhs): bf16, GetConvPrecision==1 → {Low(2), High(1)}.
   GetMatmulModes(rhs): bf16                       → {Low(2), High(1)}.
2. Cross-product, skip {Low,Low}={2,2}; summed weights:
      {High,High}=4+4=8 ; {High,Low}=4+3=7 ; {Low,High}=3+4=7.
   stable_sort ascending → {Low,High}(7), {High,Low}(7), {High,High}(8).
   First-consumed pair = {Low,High}  (lhs=Low ord 2, rhs=High ord 1).
3. GetMatmulDataFormat(bf16, strategy): no x8/x4 pack bits → format 1 (bf16 packed).
4. MatmulModifier{format=1} → reservation group {2,1,1}: res0 held 2 cy, the two
   matrix-staging registers held 1 cy each. Back-to-back bf16 latches pipeline at
   ~1-cy issue while the multi-hundred-cycle systolic latency hides across array depth.
5. For int8 (S32 operand): GetMatmulModes → {Soft Byte 2, Soft Byte 1, Soft Byte 0,
   Soft Signed Byte 3} = modes {5,7,9,11}; GetMatmulDataFormat → format 6; the matpush
   reservation jumps to {8,7,6} — 4× the bf16 hold, the 4-byte-plane x8 latch sequence.
```

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-overview` | the model that consumes these modifier keys to index reservation rows |
| `mxu-latency-vf` / `-gl` / `-gf` / `-pf` | the per-gen integer matrices the format byte selects |
| `slot-mxu` | the MXU instruction slot whose matmul ops carry a `MatmulMode` pair |
| `slot-matprep-iar-latch` | the latch / matprep ops and the `GainLatchMode` → format mapping |

---

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuLatencyTable` model that the `MatmulModifier`/`MatpushModifier` keys index
- [MXU Latency: VF](mxu-latency-vf.md) — Viperfish `array<int,19>` reservation integers for each format group
- [MXU Latency: GL (Ghostlite)](mxu-latency-gl.md) — Ghostlite `array<int,11>` reservation integers
- [MXU Latency: GF (6acc60406)](mxu-latency-gf.md) — TPU7x reservation integers
- [MXU Latency: PF](mxu-latency-pf.md) — Pufferfish reservation integers
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU slot; matmul ops carrying a `MatmulMode` pair
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the latch ops and the full `GainLatchMode` → `MatmulDataFormat` jump table
- [Resource Enum (23-slot)](resource-enum.md) — the higher-level `Resource` vector (distinct from the MXU-internal `MxuResource`)
- [MxuOpHoldIssues Stall Recurrence](mxu-opholdissues-stall.md) — how the reservation group becomes an issue stall
