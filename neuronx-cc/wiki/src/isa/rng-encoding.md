# RNG-Family Encoding

> *All symbols, addresses, offsets and opcode constants on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core logic). The instruction encoders, validators and `<op>_info.so` Cython doc-strings live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331…`). The `bir::RandomAlgorithmKind` / `RandomDistributionKind` enums live in `libBIR.so` (`a9b1ea38`); the functional PRNG kernels — the algorithm semantics this page cites — are in `libBIRSimulator.so` (`f1c6885f`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the random-number op family the TPB emits on the wire: **`Rng`** (the primitive raw-bits generator), **`Rand2`** (the modern uniform-distribution generator), and the two engine-state ops **`RandGetState`** / **`RandSetState`** that snapshot and seed the per-lane PRNG. It also fixes the **algorithm ordinals** (`LFSR` / `PCG32` / `PHILOX_1` and the gen-2 `XORWOW`), the **distribution** field, the **dropout-threshold** band that the related `Dropout` op carries, and several **container assignments** that earlier passes got wrong.

The single most important structural fact: **the on-wire RNG family is split across two hardware generations, and they do not share a state representation.** The gen-1 `Rng` path (a `CoreV2`-era opcode) writes 32 raw PRNG bits per element and selects from a *three-way* algorithm switch — `LFSR`/`PCG32`/`PHILOX_1` — but the algorithm is **engine-global state**, not a bundle field. The gen-2 `Rand2` path (a `CoreV4` override, a different container) folds raw-generate + range-normalise into one op over a fixed `XORWOW` generator and carries an FP32 `[min,max)` range. The two state ops thread between them, discriminated by an `EngineType` (gen-1 = 6 `uint32`/lane, gen-2 = 24 `uint32`/lane). This generational split is exactly where the prior container assignments went wrong, and the corrections are called out in place.

Every bundle is a `std::array<unsigned char, 64>`: emplaced into a `SmallVector`, blanket-zeroed (so any unwritten byte is a hard `0x00`), header-stamped, field-filled, ISA-checked, and `fwrite(buf, 1, 0x40, bin)`-ed — the lifecycle shared by the whole TPB ISA ([2.5 the bundle](instruction-bundle.md)). The bar for this page: a reader can **byte-encode each RNG-family instruction by hand and select the algorithm/distribution**, knowing for each control byte its offset, width, semantic, the value source, and the disassembly store-site that pins it. Every field row and ordinal carries a confidence tag (CONFIRMED = exact store/validator-load disassembled; STRONG = name/enum-corroborated; INFERRED = zero-init implied; SPECULATIVE). No ordinal or field name is fabricated.

For reimplementation, the contract is:

- The four wire containers and their opcode→struct map: `Rng 0x4D → d4_mr` (shared with `Memset`), `Rand2 0xE2 → d3_rand`, `RandGetState 0x77 → d4_rand`, `RandSetState 0x78 → s1_rand`.
- The algorithm-selection model: the difference between the gen-1 `_RAND_ALGORITHM` ordinal namespace and the gen-2 `Rand2` enum-list slot, and why the on-wire `Rand2` algorithm byte is `3` but is **not** an `_RAND_ALGORITHM` ordinal.
- The distribution field, the FP32 `[min,max)` range encoding, and the dropout-threshold polarity byte.
- The legacy-vs-modern opcode aliasing (`0xD0`↔`0x78`, `0x76`↔`0x77`) and the `PseudoSetRngSeed` reconciliation.

## At a glance

The four ops share the TPB bundle skeleton — byte `0x00` is the opcode, byte `0x01` is `inst_word_len = 0x10` (16 dwords = 64 bytes), the access-pattern sub-bands sit at `+0x10` (source/seeds) and `+0x30`/`+0x2c` (dest), and `0x0C..0x2F` is the instruction-specific control band this page maps.

| Opcode | Op | Generator (libwalrus) | Wire struct | Engine state | Algorithm field |
|---|---|---|---|---|---|
| `0x4D` (77) | `Rng` | `CoreV2GenImpl::visitInstRng @0x12376a0` | `d4_mr_struct` (shared w/ `Memset`) | engine-global (gen-1) | **none in bundle** |
| `0xE2` (226) | `Rand2` | `CoreV4GenImpl::visitInstRand2 @0x143aca0` | `d3_rand_struct` | gen-2 `XORWOW` | `+0x0C` = `3` (enum-list 0x1d) |
| `0x77` (119) | `RandGetState` | `CoreV3GenImpl::visitInstRandGetState @0x13571e0` | `d4_rand_struct` (shared w/ `0x76`) | gen-1 / gen-2 (by `EngineType`) | `+0x0C` = `3` (packed) |
| `0x78` (120) | `RandSetState` | `CoreV3GenImpl::visitInstRandSetState @0x1357ee0` | `s1_rand_struct` | gen-1 / gen-2 (by `EngineType`) | `+0x0C` = `3` |

**Related consumers (not RNG generators, but tied to the engine state):**

| Opcode | Op | Generator | Role |
|---|---|---|---|
| `0x49` (73) | `Memset` | `CoreV2GenImpl::visitInstMemset @0x125b320` | shares the `d4_mr` container with `Rng`; `+0x28` = fill imm vs `Rng`'s reserved-zero |
| `0x7F` (127) | `Dropout` | `CoreV2GenImpl::visitInstDropout @0x123e510` | own `D3_DROPOUT` struct; consumes the engine PRNG, carries the threshold band |
| `0xD0` (208) | `PseudoSetRngSeed` | (pseudo, lowered upstream) | the higher-level seed-set op; the legacy `0xD0` wire spelling of `RandSetState` |

> **NOTE — opcode-to-ordinal grounding.** The decimal ordinals are the authoritative `NEURON_ISA_TPB` opcode-enum values: `Memset 0x49(73)`, `Rng 0x4D(77)`, `RandGetState 0x77(119)`, `RandSetState 0x78(120)`, `Dropout 0x7F(127)`, `PseudoSetRngSeed 0xD0(208)`, `Rand2 0xE2(226)`. The full enum table is owned by [2.23 ISA Enum Ordinals](isa-enum-ordinals.md); this page cites only the RNG-family slots and the algorithm/distribution enums it needs.

---

## The two-generation model (read this first)

The RNG family is **not** one generator with a selectable algorithm field on every op. It is two physically distinct PRNG generations, and the bundle layout depends on which generation an op belongs to.

```text
GEN-1  ("Rand" family — the CoreV2-era path)
  Rng 0x4D ─────────────► writes 32 RAW PRNG bits / element into the dst tensor
  GetRandState 0x77/0x76 ► snapshots the engine state to SBUF
  SetRandState 0x78/0xD0 ► seeds the engine from a source tensor
  algorithm = engine-global _RAND_ALGORITHM  {LFSR=0, PCG32=1, PHILOX_1=2}
  per-lane state = 6 uint32  (PCG: word0|word1 = uint64 LCG state;
                              LFSR: word0; Philox: key0/key1 + 4× counter)

GEN-2  ("Rand2" family — the CoreV4 override)
  Rand2 0xE2 ───────────► generate + normalise to FP32 [min,max) in one op
  RandGetState 0x77 ─────► snapshots gen-2 state (4-lane Xorwow)
  RandSetState 0x78 ─────► restores gen-2 state
  algorithm = fixed XORWOW (cuRAND), no per-instance choice
  per-lane state = 24 uint32  (4× XorwowState, each 6 uint32)
```

The discriminator that tells a state op which generation it touches is the **`EngineType`** field on the BIR instruction (`Instruction+0x90` in the simulator): `EngineType == 1` → gen-1 6-`uint32` state; `EngineType == 5` → gen-2 24-`uint32` `XORWOW` state. CONFIRMED — `mov eax,[rbx+90h]; cmp 5; cmp 1` in the simulator's `visitInstSetRandState`. The `Rng` op (gen-1) and `Rand2` op (gen-2) consume their generation's running state; the algorithm is fixed per generation, not chosen per bundle. This is the single fact a reimplementer must internalise before reading the field maps.

> **CORRECTION — `Xorwow` is the gen-2 algorithm only.** An earlier reading labelled the *whole* RNG family `XORWOW`. The binary is authoritative: only `Rand2` / `RandGetState` / `RandSetState` (gen-2) run `XORWOW`; `Rng` / `GetRandState` / `SetRandState` (gen-1) run the three-way `LFSR`/`PCG32`/`PHILOX_1` switch keyed by the engine-global `_RAND_ALGORITHM` ordinal. The two are separate state vectors (6 vs 24 `uint32`/lane) and separate algorithm namespaces.

---

## The algorithm and distribution ordinal tables

Two enum namespaces are in play, and conflating them is the classic trap. The first is the **gen-1 `_RAND_ALGORITHM`** ordinal (`bir::RandomAlgorithmKind`), the engine-global generator selector; the second is the **`Rand2` on-wire algorithm field** (`enum-list 0x1d`), which the `CoreV4` encoder stamps to a fixed constant.

### `_RAND_ALGORITHM` — gen-1 generator selector (`bir::RandomAlgorithmKind`)

Ground truth: `bir::RandomAlgorithmKind2string @0x401de0` (libBIR) and the inverse `string2 @0x40ef50`, round-trip-verified; the simulator's `visitInstRand @0x1d6290` dispatches on this ordinal (`mov esi,[rdi+0F0h]` = `Instruction+240`).

| Ord | Name | Generator (simulator-confirmed) | Confidence |
|---|---|---|---|
| `0` | `LFSR` | 32-bit Fibonacci LFSR, taps `{32,22,2,1}` → poly `x³²+x²²+x²+x+1` | CONFIRMED |
| `1` | `PCG32` | PCG-XSH-RR 64/32, mult `0x5851F42D4C957F2D`, inc `0x14057B7EF767814F` | CONFIRMED |
| `2` | `PHILOX_1` | Philox-4×32-10, `M0=0xD2511F53`, `M1=0xCD9E8D57`, `W0=0x9E3779B9`, `W1=0xBB67AE85` | CONFIRMED |
| — | (other) | `default → FATAL "Supported PRNGs are LFSR/PCG32/PHILOX_1: "` | CONFIRMED |

> **CORRECTION — `LFSR=0`, `PCG32=1`, `PHILOX_1=2`.** An earlier note implied `PCG32=0`. The libBIR `2string`/`string2` tables emit them in the order `LFSR / PCG32 / PHILOX_1`; the simulator switch (`==1 → PCG32 @0x1d71b4`, `==2 → Philox @0x1d6ac5`, `==0 → LFSR @0x1d6cdf`) confirms the ordinals. The wire form on the gen-1 `Rand` path is the ordinal itself — **no remap** — but note these ordinals do **not** appear in the `Rng 0x4D` bundle (see the `Rng` field map: the algorithm is engine-global, not a bundle field).

### `RandomDistributionKind` — gen-1 distribution selector

Ground truth: `bir::RandomDistributionKind` `@0x401ee0` (libBIR), the `InstRand` "distribution" field (`Instruction+288`), identity wire form.

| Ord | Name | Materialised in libBIRSimulator? | Confidence |
|---|---|---|---|
| `0` | `Raw` | **yes** — the only distribution the sim implements; raw `uint32` words straight to dst | CONFIRMED |
| `1` | `Uniform` | only via `Rand2`'s `xorwowStep` affine `min+(max-min)·u`, not on the gen-1 path | CONFIRMED |
| `2` | `Normal` | **not found** — no Box-Muller, no inverse-CDF in any RNG kernel | CONFIRMED (absence) |
| `3` | `Binomial` | **not found** — no binomial/BTPE loop | CONFIRMED (absence) |

> **GOTCHA — gen-1 `Rng`/`Rand` produces only `Raw` bits.** The simulator's `visitInstRand` reads **only** `Instruction+240` (algorithm); an exhaustive field-offset grep shows it never reads `+288` (distribution) or `+296` (params). The kernel models the hardware RNG bit-stream; `Uniform`/`Normal`/`Binomial` shaping is done either upstream in lowering or by the gen-2 `Rand2` op. A reimplementer who expects `Rng` to honour a `distribution` field will be wrong — `Rng` truncates each 32-bit raw draw to the dst dtype and stops there.

### `Rand2` on-wire algorithm field — `enum-list 0x1d`, fixed `3`

The `d3_rand` `rand_algorithm` byte at `+0x0C` is validated by `is_valid_enum(list 0x1d)` (check name `strict_rand2_algorithm`) and is stamped to the **constant `0x3`** by the `CoreV4` encoder. This `3` is the `XORWOW` slot of *enum-list 0x1d* — it is **not** an `_RAND_ALGORITHM` ordinal (that namespace tops out at `PHILOX_1 = 2`; there is no ordinal `3`).

> **CORRECTION — `Rand2`'s `+0x0C == 3` is NOT `_RAND_ALGORITHM` ordinal 3.** Do not cross-decode the `Rand2` algorithm byte through the `{LFSR,PCG32,PHILOX_1}` table. The two are different enum-lists: the gen-1 field carries the `RandomAlgorithmKind` ordinal `0..2`; the gen-2 `Rand2` field carries enum-list-0x1d value `3` (the cuRAND `XORWOW` slot, the only algorithm `Rand2` supports). On the wire, treat the `Rand2` algorithm byte as the literal `3`; do not equate it to `PHILOX_1` or any other gen-1 ordinal. CONFIRMED — the `CoreV4` encoder stores the constant `3` (`@0x143b07e`) and the validator gates `enum-list 0x1d`.

---

## `Rng` (opcode `0x4D`) — the primitive raw-bits generator

### Purpose

`Rng` is the gen-1 primitive: it writes 32 pseudo-random bits per element, truncating each 32-bit draw to the dst data-type, drawing from the engine's current gen-1 PRNG stream. It carries **no** algorithm, seed, or distribution field — those are engine-global state established by `SetRandState`. `Rng` is a `CoreV2`-era opcode; there is **no** `CoreV3`/`CoreV4` `visitInstRng` (the gen-4 replacement is `Rand2`).

### Container — the shared `d4_mr_struct`

`Rng` does **not** have a private wire struct. Its `rng_info` Cython doc-string states verbatim *"ISAInstructionInfo for d4_mr_struct - Rng"*. The `d4_mr` ("4-D match-replace") 64-byte container is **shared** by opcodes `0x49` (`Memset`), `0x4B`, `0xE9`, and `0x4D` (`Rng`) — proved by `core_v4::is_valid_d4_mr` dispatching on the opcode byte (`cmp 0x49`, `cmp 0x4b`, `cmp 0xe9`, `cmp 0x4d` at successive sites in `@0x146b020`).

> **CORRECTION — `Rng` reuses `d4_mr`, it has no `rng_struct`.** A prior assignment gave `Rng` a dedicated container. The `rng_info` doc-string and the `is_valid_d4_mr` opcode dispatch both pin `Rng` to the `d4_mr` family. The *only* on-wire difference between `Memset 0x49` and `Rng 0x4D` in this shared container is byte `+0x28` (see the `Memset` shared-container map below): `Memset` writes the fill immediate there, `Rng` pins it to reserved-zero.

### Field map (validator `core_v4::is_valid_d4_mr`, `Rng` branch; emit `CoreV2GenImpl::visitInstRng @0x12376a0`)

| Off | W | Field | Constraint / enum | Value source | Tag |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode = `0x4D` | `is_valid_enum(list 0x04)` | `mov BYTE[...],0x4d @0x12377fb`/`@0x123797e` | CONFIRMED |
| `+0x01` | u8 | inst_word_len `0x10` / event hdr | `has_valid_neuron_events()` | header constant | STRONG |
| `+0x1C` | u32 | dst_element_count | elements/partition | `getNumElementsPerPartition()` → `[r12+0x1c]` | CONFIRMED |
| `+0x20` | u8 | dst dtype (`d4_mr_dtype`) | `is_valid_dtype(ALLOW_FP32)` | helper `@0x120e650` → `[r12+0x20]` | CONFIRMED |
| `+0x22` | u8 | num_active_channels | `start_addr_active_channels` (≥1) | `AP[+0x8]` low byte → `[r12+0x22]` | CONFIRMED |
| `+0x24` | u8 | reserved_zero | must == 0 (`test r15b`) | `[r12+0x24]=0` | CONFIRMED |
| `+0x28` | u32 | reserved_zero (`Rng`) | must == 0 (`test r14d`) | `[r12+0x28]=0` | CONFIRMED |
| `+0x2C` | T4D | dst tensor4d AP | `tensor4d_valid(WRITE, PSUM/SBUF)` | `assignAccess<TENSOR4D>(dstAP)` | CONFIRMED |
| — | — | reserved nibble-masks | `(rsp+0xc8\|rsp+0xbc)==0`, `(rsp+0xd0 & 0xffffff00ff00ff00)==0` | AP-dim high bytes reserved-zero (`d4_mr_reserved_z`) | CONFIRMED |

> **NOTE — the two "unused" `d4_mr` selectors.** `rng_info` documents two fields the `d4_mr` container carries for `Memset`/match-replace that `Rng` pins to constants: *"Must be 0 for Rng (unused)"* and *"Must be Serial for Rng (unused)"* — a perf-mode / accumulate selector that `Rng` zeroes. They are not RNG fields; they are inherited container slots. STRONG.

### Annotated encode

```c
// libwalrus CoreV2GenImpl::visitInstRng @0x12376a0  (bundle base = r12)
u8 *b = emplace_zeroed_array64();             // blanket-zeroed std::array<u8,64>
b[0x00] = 0x4D;                               // opcode (also record key @0x1237788)
b[0x1c] = getNumElementsPerPartition();       // dst element count (u32)
b[0x20] = dtype_wire_tag(dstAP.dtype);        // dst dtype, helper @0x120e650
b[0x22] = dstAP.num_active_channels;          // AP[+0x8] low byte
b[0x24] = 0;                                  // reserved
*(u32*)&b[0x28] = 0;                          // reserved (Memset would put set_value here)
assignAccess_TENSOR4D(&b[0x2c], dstAP, /*dst*/true);
runISACheck(b); fwrite(b, 1, 0x40, bin);
// NO algorithm / seed / distribution byte — gen-1 RNG state is engine-global.
```

---

## `Rand2` (opcode `0xE2`) — uniform-distribution generator

### Purpose

`Rand2` is the gen-2 modern replacement for `Rng`: it folds raw-generate and range-normalise into one op. `rand2_info` documents *"Generate pseudo random numbers with uniform distribution using the Vector Engine … FP32 random values with uniform distribution within the specified `[min,max]` range … Uses XORWOW PRNG."* It is a **`CoreV4`-only** override — there is no `CoreV2`/`CoreV3` `visitInstRand2`.

### Container — `d3_rand_struct`

The wire struct is `d3_rand_struct`; the operand list (`rand2_info`) is `{dst, dst_mem_pattern, dtype, num_active_channels, post_process, min, min_src, max, max_src, rand_algorithm}`. The validator is `core_v4::is_valid_rand2 @0x14680d0` (the clean field map; `dbg_is_valid_rand2 @0x1468270` names each check).

### Field map (emit `CoreV4GenImpl::visitInstRand2 @0x143aca0`, struct base `r13`)

| Off | W | Field | Constraint / enum | Value source | Tag |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode = `0xE2` | `is_valid_enum(0x04)`; check `rand2_opcode` | `mov WORD[r13],si @0x143ae25` (byte0 = `0xE2`) | CONFIRMED |
| `+0x01` | u8 | inst_word_len / event hdr | `has_valid_neuron_events()` | header | STRONG |
| `+0x0C` | u8 | rand_algorithm = `3` | `is_valid_enum(list 0x1d)`; `strict_rand2_algorithm`; **XORWOW slot** | `mov BYTE[r13+0x0c],3 @0x143b07e` | CONFIRMED |
| `+0x0E` | u8 | post_process = `1` | `is_valid_enum(list 0x1e)`; `RandPostProc.UniformInRange` | `mov BYTE[r13+0x0e],1 @0x143b083` | CONFIRMED |
| `+0x20` | u8 | dtype | `is_valid_dtype(ALLOW_FP32)`; FP32/FP16/BF16/FP8\*; `valid_rand2_dtype` | helper → `[r13+0x20]` | CONFIRMED |
| `+0x22` | u8 | num_active_channels | `start_addr_active_channels` (≥1) | `[r13+0x22]` | CONFIRMED |
| `+0x26` | u8 | min_src (value-kind) | `is_valid_enum(list 0x37)` scalar/vector selector | operand encoder `@0x142e370` over `getArgument(0)` → `[r13+0x26]` | CONFIRMED |
| `+0x27` | u8 | max_src (value-kind) | `is_valid_enum(list 0x37)`; `valid_rand2_post_processing` | operand encoder over `getArgument(1)` → `[r13+0x27]` | CONFIRMED |
| imm | f32 | min | FP32 lower bound (scalar imm slot or vector AP) | `ImmValInstField` via operand encoder | STRONG |
| imm | f32 | max | FP32 upper bound; `min < max` enforced | `ImmValInstField` | STRONG |
| `+0x30` | T3D | dst tensor3d AP | `tensor3d_valid(WRITE, PSUM/SBUF)` | `assignAccess<TENSOR3D>(dstAP)` | CONFIRMED |
| — | — | reserved nibble-masks | `[rsp+0x6c..]` high nibbles `& 0xff00ff00 == 0` | AP-dim reserved-zero (`d3_rand reserved`) | CONFIRMED |

> **QUIRK — `min`/`max` are operands, not the `RandomDistributionKind` params.** The FP32 `[min,max)` range comes from the two `ImmValInstField` operands (`getArgument(0)`/`(1)`, default `0.0`/`1.0`), encoded by the generic operand encoder `@0x142e370`. For a scalar source the FP32 immediate is written into the bundle's immediate slot and `*_src` selects "scalar"; for a vector source an AP is written and `*_src` selects "vector". This is *not* the `Instruction+296` `params` SmallVector of the gen-1 distribution path. The simulator's `xorwowStep @0x1aa1c0` confirms the affine: `u = bitcast((bits & 0x7FFFFF) | 0x3F800000) - 1.0f` (a `[0,1)` draw), then `min + (max-min)·u`.

### Annotated encode

```c
// libwalrus CoreV4GenImpl::visitInstRand2 @0x143aca0  (struct base = r13)
u8 *b = emplace_zeroed_array64();
*(u16*)&b[0x00] = (flag<<8) | 0xE2;           // opcode byte0 = 0xE2 + flag byte1
b[0x0c] = 3;                                   // rand_algorithm = XORWOW slot (FIXED)
b[0x0e] = 1;                                   // post_process = UniformInRange (FIXED)
b[0x20] = dtype_wire_tag(dstAP.dtype);         // FP32 / FP16 / BF16 / FP8*
b[0x22] = dstAP.num_active_channels;
b[0x26] = encode_value_src(&b, getArgument(0)); // min: scalar imm or vector AP + kind
b[0x27] = encode_value_src(&b, getArgument(1)); // max: scalar imm or vector AP + kind
assignAccess_TENSOR3D(&b[0x30], dstAP, /*dst*/true);
runISACheck(b); fwrite(b, 1, 0x40, bin);
```

---

## `RandGetState` (opcode `0x77`) — snapshot engine PRNG state to SBUF

### Purpose

`RandGetState` stores the current per-lane PRNG state from the engine into an SBUF tensor: *"Each partition in the output tensor holds the PRNG states for the corresponding compute lane."* (`rand_get_state_info`). For `XORWOW` the dst element-count must be **6** (GpSimd) or **24** (Vector). Python fields: `rand_algorithm(XORWOW)`, `rand_generator(RandSrc.RNG_XORWOW)`, `rand_num_steps`, `rand_post_proc(RawU32)`.

### Container — `d4_rand_struct` (shared with `0x76`)

`d4_rand_struct` is shared by two opcodes — `0x76` (the legacy state op) and `0x77` (`RandGetState`): `core_v4::is_valid_d4_rand @0x14720b0` dispatches `cmp r13b,0x76` and `cmp bl,0x77`. Both the modern `CoreV3GenImpl::visitInstRandGetState @0x13571e0` and the legacy `CoreV3GenImpl::visitInstGetRandState @0x1357560` emit opcode `0x77` and produce the identical `d4_rand` bundle — they are codegen aliases.

### The packed `+0x0C` algorithm/generator/post-proc DWORD

The `CoreV3` encoder writes a single 32-bit literal `0x01000003` at `+0x0C` (`@0x13573a1`), which little-endian unpacks to four control bytes:

```text
 DWORD[+0x0C] = 0x01000003   (little-endian)
   byte +0x0C = 0x03  rand_algorithm   (XORWOW slot)
   byte +0x0D = 0x00  rand_generator   (RandSrc.RNG_XORWOW)
   byte +0x0E = 0x00  reserved / post-proc-lo
   byte +0x0F = 0x01  post_process / valid  (RandPostProc.RawU32)
```

### Field map (validator `core_v4::is_valid_d4_rand @0x14720b0`, `0x77` branch; emit `@0x13571e0`, base `r12`)

| Off | W | Field | Constraint / enum | Value source | Tag |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode = `0x77` | `is_valid_enum(0x04)` (also accepts `0x76`) | `mov BYTE[...],0x77` | CONFIRMED |
| `+0x0C` | u8 | rand_algorithm = `3` | enum-list; XORWOW slot | low byte of `0x01000003` | CONFIRMED |
| `+0x0D` | u8 | rand_generator = `0` | `RandSrc.RNG_XORWOW` | packed | STRONG |
| `+0x0E` | u8 | post-proc-lo / reserved = `0` | — | packed | STRONG |
| `+0x0F` | u8 | post_process / valid = `1` | `RandPostProc.RawU32` | packed | STRONG |
| `+0x20` | u8 | dtype | validator `cmp == 0x09` (u32 PRNG-word); `rand_get_state_valid_dtype` | helper → `[r12+0x20]` | CONFIRMED |
| `+0x22` | u8 | num_active_channels | `start_addr_active_channels` (≥1); `valid_rand_state_channels` | `[r12+0x22]` | CONFIRMED |
| `~+0x2x` | u8/u32 | rand_num_steps | present in container; `0` for plain GetState (zero-init) | not written by `CoreV3` GetState | STRONG |
| `+0x2C` | T4D | dst tensor4d AP | `tensor4d_valid(WRITE, PSUM/SBUF)`; `valid_rand_get_state_dst_tensor` | `assignAccess<TENSOR4D>` | CONFIRMED |
| — | — | reserved | `[rsp+0x9c]&0xffff00`, `[rsp+0xb0]&0xff...ff00ff00`, `[rsp+0xb8]==0` | AP high-byte reserved-zero | CONFIRMED |
| — | gate | legal-combo | `rand_get_state_legal_combinations(union, engine)` couples `{algo, generator, dtype, engine}` | branch on `[rsp+0x4c] ∈ {1,2,3}` `@0x14723c0` | CONFIRMED |

> **NOTE — the `dtype == 0x09` pin and the 6-vs-24 element count.** The state tensor's wire dtype is forced to the `uint32` tag (`0x09`); the per-partition element count is gated by `rand_get_state_legal_combinations` against the engine: GpSimd → **6** state words, Vector → **24** (4 `XORWOW` lanes × 6). `rand_num_steps` is a real container field used by a seed-advance variant, but a plain `GetState` advances `0` steps (the byte is left at zero-init). STRONG.

---

## `RandSetState` (opcode `0x78`, legacy `0xD0`) — seed the engine PRNG

### Purpose

`RandSetState` seeds the per-lane PRNG inside the engine from a source tensor: *"Each partition in the source tensor seeds the PRNG states for the corresponding compute lane."* (`rand_set_state_info`). `src_seeds` is a 2-D source tensor; for `XORWOW` the per-lane element count is **6** (GpSimd) or **24** (Vector). Python fields: `rand_algorithm(XORWOW)`, `rand_generator(RNG_XORWOW)`, `src_seeds`, `update_state(RandStateUpdate.SrcTensor)`.

### Container — `s1_rand_struct`, and the `0xD0`↔`0x78` opcode duality

The same `s1_rand` container is emitted under two opcodes across generations:

- **`0x78`** — the modern `RandSetState` (`CoreV3GenImpl::visitInstRandSetState @0x1357ee0`; record key `mov DWORD[rbp-0xb0],0x78 @0x1357fc8`, bundle byte `@0x135803b`). The validator `core_v2::is_valid_rand_set_state @0x12b4510` accepts **only** `0x78` (`cmp bl,0x78 @0x12b45b1`).
- **`0xD0`** — the legacy `CoreV2GenImpl::visitInstSetRandState @0x1224690` (record key `mov DWORD[rbp-0xb0],0xd0 @0x12247ff`, bundle byte `@0x1224871`).

> **CORRECTION — `0xD0` is the legacy `SetRandState` spelling; the modern opcode enum reuses slot `0xD0` for `PseudoSetRngSeed`.** This is the subtlest of the container corrections. In the current `NEURON_ISA_TPB` opcode enum, `0xD0 (208) = PseudoSetRngSeed` — a *pseudo*-instruction (the high-level "set rng seed" op, lowered upstream), **not** a raw wire opcode. The `CoreV2` `visitInstSetRandState` historically stamped bundle byte `0xD0`, but the modern validator rejects `0xD0` and canonicalises to `0x78`. So: real `RandSetState` wire bundles use **`0x78`**; `0xD0` survives only as the deprecated pre-`CoreV3` spelling and as the opcode-enum slot now owned by the `PseudoSetRngSeed` pseudo-op. A reimplementer must emit `0x78`, and must not read `0xD0` as `PseudoSetRngSeed` semantics when it appears in a legacy `CoreV2` bundle. CONFIRMED (the `0x78`-only validator gate; the `0xD0` enum slot).

### Field map (validator `core_v2::is_valid_rand_set_state @0x12b4510`, base `rsp+0xd0`; emit `@0x1357ee0`, base `rbp-0xb0`)

| Off | W | Field | Constraint / enum | Value source | Tag |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode = `0x78` | `is_valid_enum`; check `s1_rand opcode` (legacy `0xD0`) | `mov BYTE[...],0x78` | CONFIRMED |
| `+0x01` | u8 | header = `0x10` | `cmp == 0x10` (fixed mem/event hdr) | header | CONFIRMED |
| `+0x0C` | u8 | rand_algorithm = `3` | `is_valid_enum`; XORWOW slot | `[off 0x0c] = 0x03 @0x1358223` | CONFIRMED |
| `+0x0D` | u8 | rand_generator | `RandSrc.RNG_XORWOW` | `[off 0x0e]=0` family | STRONG |
| `+0x0F` | u8 | proc / reserved | `is_valid_enum` | — | STRONG |
| `+0x10` | u8 | update_state | `RandStateUpdate.SrcTensor`; emit `0`/`1` | `@0x1358543`/`@0x1358576` | CONFIRMED |
| `+0x16` | u16 | count / par hdr | WORD `@[rsp+0xe6]` | — | STRONG |
| `+0x18` | u8 | seed-source valid / mode | `cmp == 0x2`; emit `1` | `@0x135822a` | CONFIRMED |
| `+0x20` | T1D | src_seeds tensor AP | `tensor_start_addr_valid(WRITE, PSUM/SBUF)`; 1-D seed vector (6 GpSimd / 24 Vector) | `assignAccess<TENSOR1D>(getArgument(1))` `@0x1358075` | CONFIRMED |
| — | — | reserved | `s1_rand_reserved` band; `cmp eax,0x9010002`/`0x9000002` enumerate legal `{dtype,dims}` | `valid_rand_state_channels` | CONFIRMED |
| — | gate | legal-combo | `rand_set_state_legal_combinations(union, engine)` gates `{algorithm, generator, engine, element-count}` | `@0x1482e50` | CONFIRMED |

> **NOTE — `RandSetState` emits a two-bundle pair.** `visitInstRandSetState` issues two `fwrite(.,1,0x40,.)` calls (`@0x13580dc`/`@0x135816b`): the seed-write bundle plus a state-commit bundle. The source seed vector is bound as a `TENSOR1D` AP (a second `TENSOR1D` arg `@0x13581d8` — the 6/24-element seed vector). On restore, the simulator applies a per-lane zero-guard: if a lane's first five words are all zero it forces the first word to `1`, avoiding a dead all-zero `XORWOW` state. CONFIRMED.

---

## `Memset` (opcode `0x49`) — the shared `d4_mr` container

`Memset` and `Rng` emit the **same** 64-byte `d4_mr` bundle; the only control-band difference is `+0x28`. This is the "Memset-Random" coupling: a `Memset` fills `+0x28` with a literal value, while the `Rng` variant zeroes it and instead pulls 32 PRNG bits per element from the engine stream.

| Off | W | `Memset` (`0x49`) | `Rng` (`0x4D`) | Tag |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0x49` | opcode `0x4D` | CONFIRMED |
| `+0x1C` | u32 | dst_element_count | dst_element_count | CONFIRMED |
| `+0x20` | u8 | dtype | dtype | CONFIRMED |
| `+0x22` | u8 | num_active_channels | num_active_channels | CONFIRMED |
| `+0x24` | u8 | reserved_zero | reserved_zero | CONFIRMED |
| `+0x28` | u32 | **set_value (fill imm)** | **reserved_zero (= 0)** | CONFIRMED |
| `+0x2C` | T4D | dst tensor4d AP | dst tensor4d AP | CONFIRMED |

`CoreV2GenImpl::visitInstMemset @0x125b320` (base `r13`): `[r13+0x28] = [r12+0xf8]` (the fill immediate, DWORD `@0x125b455`), gated by a `[r12+0x118]` flag (`0` = immediate-fill, non-zero = vector path/error). pybind check names confirm the field family: `memset_set_value_type`, `memset_opcode`, and the `d4_mr` family `d4_mr_reserved_z` / `d4_mr_le16k_src` / `d4_mr_same_src_dst` / `match_replace_dtype`. STRONG.

---

## `Dropout` (opcode `0x7F`) — the engine-PRNG consumer + threshold band

`Dropout` is a separate opcode with its **own** `D3_DROPOUT` wire struct — it is **not** a `d4_mr`/`d3_rand` RNG generator. It is the consumer of the engine PRNG stream: it draws from the same gen-1 PRNG (seeded out-of-band by `PseudoSetRngSeed`/`RandSetState`, state observable via `RandGetState`) and compares each draw against a per-element threshold, dropping or keeping by the threshold polarity. `CoreV2GenImpl::visitInstDropout @0x123e510` (base `r13`).

### Field map (validator `dbg_is_valid_dropout @0x12dbef0`; emit `@0x123e510`)

| Off | W | Field | Constraint / name | Value source | Tag |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode = `0x7F` | `s_dropout_opcode` | `movb 0x7f @0x123e676` | CONFIRMED |
| `+0x01` | u8 | header = `0x10` | shared | header | INFERRED |
| `+0x20` | u8 | in_dtype | `d3_dropout_same_src_dst_type` (in == out) | `sub_120E650(src) @0x123e6c7` | CONFIRMED |
| `+0x21` | u8 | out_dtype | same-type assert | `sub_120E650(dst) @0x123e6d3` | CONFIRMED |
| `+0x22` | u8 | active / lane | `d3_dropout_src_dst_count_check` | `src.AP[0x50][0x8]` low `@0x123e701` | CONFIRMED |
| `+0x23` | u8 | **threshold_imm_ptr** | the KEEP/DROP threshold scalar-operand slot | boost::variant converter `sub_12051E0` → slot index `@0x123e740` | CONFIRMED (store); STRONG (name) |
| `+0x24` | u8 | **threshold_dtype** | `d3_dropout_threshold_dtype_check` | `sub_120E650(converter) @0x123e848` | CONFIRMED |
| `+0x2B` | u8 | **rng_mode / dropout_variant** | which RNG mode/variant the dropout uses | `*(this+0xF0)` (RNG-config byte) `@0x123e737` | CONFIRMED (store); INFERRED (semantic) |
| `+0x10` | T3D | src AP | `assignAccess<TENSOR3D>(src) @0x123e856` | — | CONFIRMED |
| `+0x30` | T3D | dst AP | `assignAccess<TENSOR3D>(dst) @0x123e864` | — | CONFIRMED |

> **CORRECTION — the dropout threshold is a 1-byte operand-slot pointer at `+0x23`, not an inline FP32.** A prior reading described the dropout bundle as embedding `{threshold value, DropoutThresholdType, dst AP}` inline. The disassembly is precise: `+0x23` is `threshold_imm_ptr`, a 1-byte index into a scalar-operand slot (resolved from a `boost::variant` by `sub_12051E0`), **not** an inline `f32`; the threshold's own dtype is the separate byte `+0x24`. The threshold *value* lives in the bundle's operand slot the pointer selects. The RNG-mode selector is yet a third byte, `+0x2B`.

### The `DropoutThresholdType` polarity (`klr::DropoutThresholdType`)

The threshold-type selects whether the threshold is read as the probability of **dropping** or **keeping** an element. Ground truth: `klr::to_string(klr::DropoutThresholdType&) @0xf7f5b0` (`cmp eax,1`/`cmp eax,2`), table `@0x1c7ef10` (`"DropRate"`,`"KeepRate"`).

| Ord | Name | Meaning | Confidence |
|---|---|---|---|
| `1` | `DropRate` | threshold = probability to **drop** | CONFIRMED |
| `2` | `KeepRate` | threshold = probability to **keep** | CONFIRMED |
| `0` | (unset/other) | not a valid configured polarity | CONFIRMED |

> **NOTE — `Dropout` has no seed/state field in its bundle.** The RNG seed is established out-of-band by the separate `PseudoSetRngSeed` pseudo-instruction (`s_rng_opcode`, the `0xD0`-slot pseudo-op); `Dropout` consumes the engine's current stream. The polarity byte (`DropoutThresholdType`, carried by the BIR instruction, not the threshold-dtype byte) selects the comparison direction. The full `DropoutThresholdType` enum is owned by [2.23 ISA Enum Ordinals](isa-enum-ordinals.md).

---

## The container-assignment corrections, consolidated

The prior-pass container map had four assignments that the binary overturns. They are stated in place above; collected here for a reimplementer auditing an older map:

| Op | Prior assignment | Correct (binary-grounded) |
|---|---|---|
| `Rng 0x4D` | private `rng_struct` | shares `d4_mr_struct` with `Memset 0x49` (`rng_info` doc-string; `is_valid_d4_mr` dispatch) |
| `Rand2 0xE2` algo byte | `_RAND_ALGORITHM` ordinal `3` | enum-list-0x1d value `3` (XORWOW slot); `_RAND_ALGORITHM` has no ordinal `3` |
| `RandSetState` opcode | `0xD0` | canonical `0x78`; `0xD0` is the legacy spelling, and the opcode enum now labels slot `0xD0` as `PseudoSetRngSeed` |
| `Dropout 0x7F` threshold | inline FP32 value in the bundle | 1-byte `threshold_imm_ptr@+0x23` (operand-slot index) + `threshold_dtype@+0x24` + `rng_mode@+0x2B` |
| whole family algorithm | `XORWOW` everywhere | `XORWOW` is gen-2 (`Rand2`) only; gen-1 (`Rng`) is `LFSR`/`PCG32`/`PHILOX_1` engine-global |

---

## Cross-References

- [2.23 ISA Enum Ordinals](isa-enum-ordinals.md) — *(planned)* — owns the full `RandomAlgorithmKind`, `RandomDistributionKind` and `DropoutThresholdType` enum tables; this page cites only the RNG-family slots.
- [2.5 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the shared bundle lifecycle (emplace → zero → header → fields → ISA-check → `fwrite 0x40`) every RNG op runs.
- [2.16 Pool / TensorReduce / Reciprocal / Iota Encoding](pool-reduce-encoding.md) — sibling control-band page; the same `assignAccess<TENSORnD>` operand machinery.
- [1.11 The DVE Engine](../arch/dve-engine.md) — the Vector/DVE engine that runs the `Rand2` `XORWOW` lanes and the gen-2 24-`uint32` state.
- [Part 7 — BIR Simulator RNG kernels (F10) & Codegen RNG (I14)](../bir/) — the functional `LFSR`/`PCG32`/`PHILOX_1`/`XORWOW` constants, the `EngineType` discriminator, and the upstream `InstRand`/`InstRand2` that feed these encoders.
- [Build & Version Provenance](../reference/versions.md) — the single pinned build all addresses are read from.
