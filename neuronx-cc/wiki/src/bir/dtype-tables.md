# The BIR Dtype Enum and its Wire-Tag / Stride / Alignment LUTs

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310). The enum body lives in `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`); the codegen LUTs and the alignment predicate live in `neuronxcc/starfish/lib/libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`). For both binaries VA == file offset in `.text` and `.rodata` (libwalrus `.rodata` `Addr==Offset==0x1c72000` per `readelf -S`), so `xxd -s <VA>` reads the right bytes. cp311/cp312 VAs drift; the enum ordinals, LUT contents, and field offsets are stable across them.*

## Abstract

BIR has exactly **20** element data types, ordinals `0..19`, defined by the `bir::Dtype` enum in `libBIR.so` and byte-verified from the dense switch body of `bir::Dtype2string`. This is the *front-end's* type alphabet — the one a `from_json` loader parses and a `MemoryLocation` carries. It is **not** the on-wire `NEURON_ISA_TPB_DTYPE` alphabet that the hardware bundle encodes: the two are different enums with different orderings, and the bridge between them is a 20-byte lookup table in `libwalrus.so`.

This page reconstructs three things a reimplementer needs to emit a correct TPB instruction from a BIR `Dtype`:

- **The dtype → wire-tag remap** `byte_1DFBAD0` (20 × `u8`). The CoreV4 encoder indexes it by the BIR Dtype int to get the `NEURON_ISA_TPB_DTYPE` value that goes into the 64-byte bundle. The ordering is by container-size class, not by Dtype ordinal, so the remap is non-trivial.
- **The dtype → element byte-stride** `qword_1DFC040` (20 × `u64`). The matmul/ldweight encoder uses it as `start_addr = (numElem − 1) · stride[dtype] + base`. For the x4-packed MX types the stride is the **container** size (2 or 4 bytes), not the logical FP4/FP8 element size.
- **The per-dtype byte-address alignment**, which is *not* a third table but a small predicate `core_v3::addr_aligned_dtype` keyed on the **wire-tag** (not the Dtype). To get the alignment for a BIR Dtype you compose `wire_tag = byte_1DFBAD0[dtype]` then feed it to the predicate.

The canonical Dtype-cast machinery (`CastToNewDType`, the FP8 conversion config) belongs to [Numerics §9.3](../numerics/dtype-catalog.md); this page owns the enum roster and the three backend LUTs only. For the on-wire ISA ordinals see [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md); for how Python-facing NKI maps onto these types see the [nki/dtype Façade](../nki/dtype-facade.md).

| | |
|---|---|
| **Enum body** | `bir::Dtype2string` @0x2641e0 (libBIR `.text`) — dense switch, 20 cases |
| **Reverse map** | `bir::string2Dtype` @0x265fb0 (libBIR) — returns 2..19, default→assert |
| **Wire-tag LUT** | `byte_1DFBAD0` @0x1DFBAD0 (libwalrus `.rodata`, 20 × `u8`) |
| **Stride LUT** | `qword_1DFC040` @0x1DFC040 (libwalrus `.rodata`, 20 × `u64`) |
| **Wire-tag reader** | `sub_14347C0` @0x14347c0 (libwalrus) — the dtype→wire-tag converter |
| **Stride reader** | `CoreV4GenImpl::generateLdweightMx` @0x143e350 (libwalrus) |
| **Alignment predicate** | `core_v3::addr_aligned_dtype` @0x136ed10 (libwalrus, 87 B) |
| **x4 expand factor** | 4 (for Dtype ∈ {2, 8, 9}) |
| **Member count** | exactly 20 — `cmp …,0x13; ja default` bounds every consumer |

---

## The master table

Every column below is read straight out of one of the two binaries. `Name` is the verbatim string from the `Dtype2string` switch case; `WireTag` is `byte_1DFBAD0[Int]`; `Stride` is `qword_1DFC040[Int]` in bytes; `Align` is the byte-address modulus from `addr_aligned_dtype` composed through the wire-tag; `x4` is the lane-container expand factor.

| Int | Name | WireTag | Stride | Align | x4 | Confidence |
|----:|------|--------:|-------:|------:|:--:|:----------:|
| 0 | `uint8` | 3 (`0x03`) | 1 | 1 | — | CERTAIN |
| 1 | `int8` | 2 (`0x02`) | 1 | 1 | — | CERTAIN |
| 2 | `float4_e2m1fn_x4` | 16 (`0x10`) | 2 | (*) | 4 | CERTAIN |
| 3 | `float8e3` | 13 (`0x0d`) | 1 | 1 | — | CERTAIN |
| 4 | `float8e4` | 14 (`0x0e`) | 1 | 1 | — | CERTAIN |
| 5 | `float8_e4m3fn` | 14 (`0x0e`) | 1 | 1 | — | CERTAIN |
| 6 | `float8_e8m0fnu` | 3 (`0x03`) | 1 | 1 | — | CERTAIN |
| 7 | `float8e5` | 15 (`0x0f`) | 1 | 1 | — | CERTAIN |
| 8 | `float8_e4m3fn_x4` | 14 (`0x0e`) | 4 | 1 | 4 | CERTAIN |
| 9 | `float8_e5m2_x4` | 15 (`0x0f`) | 4 | 1 | 4 | CERTAIN |
| 10 | `uint16` | 5 (`0x05`) | 2 | 2 | — | CERTAIN |
| 11 | `int16` | 4 (`0x04`) | 2 | 2 | — | CERTAIN |
| 12 | `bfloat16` | 6 (`0x06`) | 2 | 2 | — | CERTAIN |
| 13 | `float16` | 7 (`0x07`) | 2 | 2 | — | CERTAIN |
| 14 | `uint32` | 9 (`0x09`) | 4 | 4 | — | CERTAIN |
| 15 | `int32` | 8 (`0x08`) | 4 | 4 | — | CERTAIN |
| 16 | `float32` | 10 (`0x0a`) | 4 | 4 | — | CERTAIN |
| 17 | `float32r` | 11 (`0x0b`) | 4 | 4 | — | CERTAIN |
| 18 | `uint64` | 1 (`0x01`) | 8 | 8 | — | CERTAIN |
| 19 | `int64` | 12 (`0x0c`) | 8 | 8 | — | CERTAIN |

(*) **FP4-x4 (Int 2, wire-tag 16) has no defined `core_v3` alignment.** The `core_v3::addr_aligned_dtype` predicate falls through to its default `return false` for any wire-tag outside `{1,2,3,4..15}`, and wire-tag 16 is outside that set — so on `core_v3` FP4-x4 is "never aligned". It is a CoreV4-only type; the `core_v4` copy of the predicate @0x1446420 widens its always-aligned group to include wire-tag 16, making FP4-x4 align 1 on CoreV4. See the [alignment](#per-dtype-alignment-a-predicate-not-a-table) and [arch-family](#three-copies-one-per-arch-family) sections.

> **QUIRK —** the wire-tag column is a *permutation*, not an offset. `int8`→2 and `uint8`→3, but `bfloat16`→6 and `float16`→7 while `uint32`→9 and `int32`→8 (signed/unsigned swap inside the 32-bit class). A reimplementer who assumes `wire_tag == dtype` or `wire_tag == dtype + k` will emit the wrong type into every bundle. The remap exists because the on-wire `NEURON_ISA_TPB_DTYPE` enum orders types by container-size class (8b → 16b → 32b → 64b → FP8 → FP4), an ordering unrelated to the BIR Dtype ordinals.

---

## The enum body: `Dtype2string` @0x2641e0

The enum has no `.rodata` member-name table of its own; its definitive form is the switch in `bir::Dtype2string` (libBIR `.text`). The decompiled body is a dense `switch (dtype)` with cases `0..19`, each returning a single string literal, and a `default` that builds a `NeuronAssertion` against the source path `…/neuronxcc/walrus/ir/lib/IR/Dtype.cpp:39`:

```c
// bir::Dtype2string @0x2641e0  (libBIR .text) — reconstructed
std::string bir::Dtype2string(bir::Dtype dtype) {
  switch (dtype) {
    case 0:  return "uint8";
    case 1:  return "int8";
    case 2:  return "float4_e2m1fn_x4";
    case 3:  return "float8e3";
    case 4:  return "float8e4";
    case 5:  return "float8_e4m3fn";
    case 6:  return "float8_e8m0fnu";
    case 7:  return "float8e5";
    case 8:  return "float8_e4m3fn_x4";
    case 9:  return "float8_e5m2_x4";
    case 10: return "uint16";
    case 11: return "int16";
    case 12: return "bfloat16";
    case 13: return "float16";
    case 14: return "uint32";
    case 15: return "int32";
    case 16: return "float32";
    case 17: return "float32r";
    case 18: return "uint64";
    case 19: return "int64";
    default:                                   // Dtype.cpp:39
      NeuronAssertion(ErrorCode::71, "…/IR/Dtype.cpp:39");  // no case ≥ 20
  }
}
```

There is **no case ≥ 20**: the switch is dense `0..19` and any other value traps. The reverse map `bir::string2Dtype` @0x265fb0 corroborates the bound from the other side — it returns ordinals `2..19` (cases 0/1 are pre-filtered by length before the dispatch) and its own `default` asserts at `Dtype.cpp:49`. All 20 name strings are independently present as standalone `.rodata` literals in libBIR (`strings -a` finds exactly the 20-name set, no extras). [CONFIRMED — decompiled switch bodies + `.rodata` string set.]

> **CORRECTION (D-D04, re S2-04 §3.6) —** an earlier transcription annotated the Dtype roster "0 uint8 … 19 int64 (+more)". The "(+more)" is wrong. The enum is **exactly** `0..19`; both `Dtype2string` (cases 0..19, default→assert) and `string2Dtype` (returns 2..19, default→assert) independently fix the density at 20 members. Upgrade the roster to CERTAIN.

---

## The wire-tag LUT: `byte_1DFBAD0` @0x1DFBAD0

This is the bridge from the front-end `Dtype` to the on-wire `NEURON_ISA_TPB_DTYPE`. It is a flat 20-byte array in libwalrus `.rodata`. Dumped at its VA (== file offset):

```text
$ xxd -s 0x1DFBAD0 -l 24 libwalrus.so
01dfbad0: 03 02 10 0d 0e 0e 03 0f  0e 0f 05 04 06 07 09 08   wire-tag[0..15]
01dfbae0: 0a 0b 01 0c  | 11 15 17 13                          wire-tag[16..19] | adjacent LUT
```

The first 20 bytes are the table; the four bytes after (`11 15 17 13`) are a *different* adjacent lookup table, not part of `byte_1DFBAD0` — the bounds check `cmp …,0x13` (=19) is what keeps reads inside the first 20. [CONFIRMED — direct `xxd`; byte-identical to D-D04 and to S2-06.]

### The reader: `sub_14347C0` @0x14347c0

This is *the* dtype→wire-tag converter, named for its source assert string `CoreV4GenImpl.cpp:111`. Every CoreV4 `visitInst*` / `generate*Mx` encoder funnels through it. The hot path is four instructions:

```text
0x14347d7   cmp   eax, 13h                 ; dtype ≤ 19 ?
0x14347da   ja    loc_1434800             ;   else → NeuronAssertion(CoreV4GenImpl.cpp:111)
0x14347dc   lea   rdx, unk_1DFBAD0        ; rip-rel → 0x1DFBAD0  (0x14347dc+7+0x9c72ed)
0x14347e3   movzx eax, byte ptr [rdx+rax] ; wire_tag = byte_1DFBAD0[dtype]
```

```c
// sub_14347C0 @0x14347c0 — the dtype→wire-tag converter
uint8_t bir_dtype_to_wire_tag(uint32_t dtype) {        // NEURON_ISA_TPB_DTYPE
  if (dtype > 19)                                       // 0x13
    NeuronAssertion("…/codegen/src/CoreV4GenImpl.cpp:111");
  return ((const uint8_t *)&byte_1DFBAD0)[dtype];
}
```

The rip-relative `lea` resolves to exactly `0x1DFBAD0`, confirming the symbol the table-dump targets. Observed callers (CoreV4 encoders): `visitInstExponential` @0x1439d30, `visitInstQuantizeMx` @0x143dc60, `visitInstRand2` @0x143aca0, `visitInstActivation` @0x143bbb0, `generateMatmultMx` @0x143ebd0, `generateLdweightMx` @0x143e350. [CONFIRMED — disasm of `sub_14347C0` + caller list.]

> **GOTCHA —** the x4-packed FP8 types share the wire-tag of their unpacked base: `float8_e4m3fn_x4` (Int 8) → 14, identical to `float8_e4m3fn` (Int 5) → 14; `float8_e5m2_x4` (Int 9) → 15, like `float8e5` (Int 7) → 15. The "×4-ness" is **not** carried in the wire-tag — it is carried by the access pattern (the `num_elem_per_partition ×4` on the MX path) and by the stride. Two distinct Dtypes collapsing onto one wire-tag is by design; do not try to recover the Dtype from the wire-tag, the map is many-to-one.

---

## The stride LUT: `qword_1DFC040` @0x1DFC040

A flat 20-entry `u64` array giving the **element byte-stride** used for address arithmetic. Dumped and parsed (little-endian):

```text
$ xxd -s 0x1DFC040 -l 160 libwalrus.so   (parsed as 20 × u64)
  1 1 2 1 1 1 1 1 4 4 2 2 2 2 4 4 4 4 8 8
```

Entry 20 onward is a mangled symbol string (`*ZN9neur…`), which pins the table length at exactly 20 qwords — there is no 21st stride. [CONFIRMED — `xxd` + `struct.unpack('<20Q', …)`; byte-identical to D-D04/S2-06.]

### The reader: `generateLdweightMx` @0x143e350

The single in-binary consumer indexes the table with `imul rax*8` and folds the stride into the start address. The InstMatmultMx Dtype field sits at instruction offset `+0x30`:

```text
0x143e643   mov   eax, [rbx+30h]          ; dtype = InstMatmultMx.dtype  (field @ +0x30)
0x143e64e   cmp   eax, 13h                 ; ≤ 19 bounds
0x143e651   ja    …                        ;   else assert
0x143e657   lea   rcx, unk_1DFC040        ; rip-rel → 0x1DFC040
0x143e66b   imul  rbx, [rcx+rax*8]         ; rbx = (numElem−1) · stride[dtype]
0x143e67d   add   rax, base                ; → instr.src_mem_pattern.t.start_addr.addr
```

```c
// CoreV4GenImpl::generateLdweightMx @0x143e350 — stride fold (excerpt)
uint32_t dtype = inst->dtype;                    // InstMatmultMx + 0x30
if (dtype > 19) NeuronAssertion(...);
uint64_t stride   = ((const uint64_t *)&qword_1DFC040)[dtype];
uint64_t span     = (numElem - 1) * stride;      // imul rbx, stride[dtype]
start_addr.addr   = base + span;
```

`generateMatmultMx` @0x143ebd0 uses the same `+0x30` field and the same converter chain. [CONFIRMED — disasm of `generateLdweightMx`; second `lea unk_1DFC040` site at 0x143e9ea in the same function.]

> **QUIRK —** the stride is the **container** size, not the logical element size, and that is the whole point of the table for packed types. `float4_e2m1fn_x4` (Int 2) has stride **2** — a 2-byte container holding four 4-bit values. `float8_e4m3fn_x4` / `float8_e5m2_x4` (Int 8, 9) have stride **4** — a `u32` container holding four FP8 bytes — which is why their stride (4) differs from plain FP8's stride (1). For every standard-width scalar type the stride equals the byte-width (8b→1, 16b→2, 32b→4, 64b→8) and matches the alignment exactly; the packed types are the only place container ≠ logical width.

---

## Per-dtype alignment: a predicate, not a table

There is no third `.rodata` table for alignment. The byte-address modulus is computed by `neuronxcc::core_v3::addr_aligned_dtype` @0x136ed10 — an 87-byte `bool(uint addr, NEURON_ISA_TPB_DTYPE wt)` that returns `addr & (align−1) == 0`. Crucially it is keyed on the **wire-tag**, not the BIR Dtype, so the per-Dtype alignment is a two-step composition: `wt = byte_1DFBAD0[dtype]`, then `addr_aligned_dtype(addr, wt)`.

The body is a cascade of small range tests. Decompiled and cross-read against the disasm:

```c
// core_v3::addr_aligned_dtype @0x136ed10 (libwalrus, 87 bytes)
// a1 = address (edi), a2 = wire-tag (esi). returns (addr & (align-1)) == 0.
bool addr_aligned_dtype(uint addr, NEURON_ISA_TPB_DTYPE wt) {
  if ((uint8_t)(wt - 13) <= 2 ||                 // wt ∈ {13,14,15}  → align 1
      (uint8_t)(wt -  2) <= 1)                    // wt ∈ {2,3}       → align 1
    return true;                                  //   no alignment requirement
  if ((uint8_t)(wt - 4) <= 3)                     // wt ∈ {4,5,6,7}   → align 2
    return (~addr) & 1;                           //   ⇔ addr & 1 == 0
  if ((uint8_t)(wt - 8) <= 3)                     // wt ∈ {8,9,10,11} → align 4
    return (addr & 3) == 0;
  if (wt == 12 || wt == 1)                        // wt ∈ {1,12}      → align 8
    return (addr & 7) == 0;
  return false;                                   // wt ∈ {0,16,…}    → no defined align
}
```

Mapping the wire-tag groups back through the Dtype→wire-tag remap gives the per-Dtype `Align` column of the master table:

| Wire-tag group | Align | BIR Dtypes hitting it |
|---|---:|---|
| {2, 3, 13, 14, 15} | 1 | int8, uint8, float8_e8m0fnu, all FP8 (e3/e4/e4m3fn/e5/e4m3fn_x4/e5m2_x4) |
| {4, 5, 6, 7} | 2 | int16, uint16, bfloat16, float16 |
| {8, 9, 10, 11} | 4 | int32, uint32, float32, float32r |
| {1, 12} | 8 | uint64, int64 |
| {0, 16, …} | — (false) | float4_e2m1fn_x4 (wire-tag 16) — see below |

This grouping is byte-for-byte the same partition as `core_v3::get_type_size` @0x136e5b0 uses to assign element byte-sizes — i.e. on `core_v3` a wire-tag's alignment always equals its element size. [CONFIRMED — decompile + disasm of `addr_aligned_dtype`; partition matches S2-08 §3 and `get_type_size`.]

> **GOTCHA —** FP4-x4 (Dtype 2 → wire-tag 16) reaches the predicate's `return false` default on `core_v3`. That does **not** mean "any address" — it means the predicate reports *not aligned* for every address, so a `core_v3` backend will never accept an FP4-x4 access. FP4-x4 is genuinely a CoreV4-only type here; only the `core_v4` predicate copy admits wire-tag 16. A reimplementer targeting an older arch must reject FP4-x4 upstream, not paper over it with align 1.

---

## x4 packing: the expand factor is 4

Three Dtypes are MX "x4-packed" lane containers: `float4_e2m1fn_x4` (2), `float8_e4m3fn_x4` (8), `float8_e5m2_x4` (9). Each packs **four** logical FP4/FP8 elements into one on-wire container, and the MX matmul path multiplies the K-extent (`num_elem_per_partition`) by 4 to recover the logical element count.

The factor is observable directly in `generateMatmultMx` @0x143ebd0, which carries the assert string and drives the `MXMEM_PATTERN1D` assignment:

```c
// generateMatmultMx @0x143ebd0 (excerpt) — the x4-packed gate
LOBYTE(bundle) = sub_14347C0(lhs_ap->dtype);   // wire-tag for the packed datum
…
reportError("MX dtype must be packed with 4 elements into 1");   // x4 invariant
…
assignAccessForMX<NEURON_ISA_TPB_MXMEM_PATTERN1D>();             // ×4 num_elem path
```

The packing manifests on two axes, both already in the master table: the **access pattern** carries `num_elem_per_partition × 4` (so K = partitionDim × 4 on the MX matmul), and the **stride** is the container size (2 for FP4-x4, 4 for FP8-x4) rather than the logical element width. The wire-tag is *not* a third signal — the packed types reuse their unpacked base's wire-tag (8/9 → 14/15, as above). [CONFIRMED — `generateMatmultMx` assert string + `assignAccessForMX`; the {2,8,9} membership matches S2-08 §2.4 and D-B04.]

> **GOTCHA —** an earlier note recorded "fp8_x4 wire tag 9 = u32". That `9` is **not** a value from `byte_1DFBAD0` (where Int 8/9 → wire-tags 14/15). It is the ADDR4 **container** tag emitted by the `QuantizeMx` encoder from a *different* 20-entry LUT, where the FP8-x4 datum is addressed as a `uint32` container (wire-tag 9 = uint32). Both facts are true and non-contradictory: the *element* wire-tag is the FP8 tag (14/15); the *container* is addressed as u32. Do not conflate the two LUTs.

---

## Notes on the non-obvious members

- **`float32r` (Int 17)** is a first-class Dtype, not a flag on `float32`. It carries its own wire-tag 11, but is sized and aligned identically to `float32` (stride 4, align 4). It is the round / reduced-precision fp32 PE-accumulation format, gated by a distinct validator `NEURON_ISA_TPB_DTYPE_ALLOW_FP32R` in `core_v3::is_valid_dtype`. It is not a packed type.

- **`float8_e8m0fnu` (Int 6)** is the OCP-MXFP shared-exponent scale type — an 8-bit value with wire-tag 3 (same class as `uint8`), stride 1, align 1. It is the E8M0 scale paired with the x4-packed data in the second ADDR4 of `MXMEM_PATTERN1D`, not a compute datum.

- **`float8e3` / `float8e4` / `float8e5` (Int 3, 4, 7)** are legacy short-name FP8 variants kept alongside the canonical `float8_e4m3fn` (5) and `float8_e5m2_x4` (9) family. `float8e4` (wire-tag 14) shares its tag with `float8_e4m3fn` (14) — both are e4m3; `float8e5` (15) maps like e5m2. The legacy names are still reachable through `string2Dtype`, so a JSON loader must accept them.

---

## Three copies, one per arch family

`addr_aligned_dtype` is instantiated once per arch family: `core_v2` @0x609d10, `core_v3` @0x136ed10, `core_v4` @0x1446420. They are not identical. The `core_v4` copy widens its always-aligned group from `{13,14,15}` to `{13,14,15,16}` (a `cmp dl,3` against `wt − 13` instead of `cmp dl,2`), pulling FP4-x4 (wire-tag 16) into "always aligned", and it adds a `sub_142DE70` sub-check for the `{4..7}` x4 range. The task scope is the **`core_v3`** copy @0x136ed10; the `core_v4` FP4 delta is noted because it is the reason FP4-x4 has no `core_v3` alignment but align 1 on `core_v4`. A full v2/v3/v4 alignment crosswalk is out of scope. [STRONG — `core_v3` fully decoded; `core_v4` delta read from its disasm but not exhaustively traced.]

> **NOTE —** both LUTs (`byte_1DFBAD0`, `qword_1DFC040`) have exactly one in-binary consumer each, and both consumers are CoreV4 (`sub_14347C0`, `generateLdweightMx`). So the tables appear **CoreV4-scoped**; earlier arches likely compute stride via `core_vN::get_type_size` on the wire-tag domain rather than reading these arrays. This is inferred from the single-consumer fact, not traced through every arch. [INFERRED.]

---

## Open questions

- **Wire-enum names.** The `NEURON_ISA_TPB_DTYPE` *integer* values (1..16) are CERTAIN — they come straight out of `byte_1DFBAD0` and the alignment/size groupings. The symbolic *names* of those wire-tags (e.g. whether 14 is spelled `DTYPE_FP8_E4M3`) are **not** recovered as a symbol table in libwalrus; an authoritative wire-enum name list would need the ISA datamodel reflection layer or the pybind decoder. The values are pinned; the names are inferred. [INFERRED — names only.]

- **Intra-x4 lane order.** Which of the four logical FP4/FP8 elements occupies which sub-field of the container is a hardware micro-op detail not encoded in any compiler table examined here. [SPECULATIVE — not in the binary.]

---

## Reimplementation checklist

To emit a TPB bundle from a BIR `Dtype`:

1. Validate `0 ≤ dtype ≤ 19`; trap otherwise (this is what every consumer's `cmp …,0x13; ja` enforces).
2. `wire_tag = byte_1DFBAD0[dtype]` — the value that goes into the bundle's dtype field. Remember it is a permutation and many-to-one for the x4 FP8 pair.
3. `stride = qword_1DFC040[dtype]` — for address arithmetic `(numElem − 1) · stride + base`. It is the container size; do not substitute logical element width for packed types.
4. `aligned = addr_aligned_dtype(addr, wire_tag)` — keyed on the **wire-tag**, not the Dtype. On `core_v3`, FP4-x4 (wire-tag 16) is never aligned; reject it or target `core_v4`.
5. If `dtype ∈ {2, 8, 9}`, multiply `num_elem_per_partition` by 4 on the MX matmul path (the x4 expand) and treat the datum as a packed container.
