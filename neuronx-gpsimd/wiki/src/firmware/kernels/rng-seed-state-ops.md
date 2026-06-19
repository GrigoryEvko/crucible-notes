# RNG Seed-State Opcodes (`0x77` RandGetState / `0x78` RandSetState)

This page documents the two opcodes that **save and restore** the GPSIMD datapath RNG
state — the per-lane PRNG words that the [Xorwow](rng-xorwow-sw.md), [TIE-Xorwow](rng-xorwow-tie.md),
[LFSR](rng-lfsr-dispatch.md), and Philox generators advance. `0x77` **`RAND_GET_STATE`**
serializes the live generator state **out** to a destination tensor; `0x78`
**`RAND_SET_STATE`** deserializes a generator state **in** from a source tensor (or an
inline immediate). Together they form a clean checkpoint pair: a model can dump the RNG
state to memory at a kernel boundary and reload it later to reproduce or migrate the
draw sequence. These two ops **close the datapath-RNG (GpSimd) opcode surface** — every
generator the previous pages decoded now has a save/restore path, and the draw/seed/checkpoint
roles are fully accounted for.

These ops are *not* number generators. They do not draw; they only walk the generator's
state words between an addressable tensor and the per-lane state buffer in DRAM/SBUF
scratch. They are also **architecturally distinct** from the PE engine's
[`PeManageSeed`](pe-matmul.md) (`0x08`), which manages the PSUM stochastic-rounding seeds —
a different opcode, a different engine, a different struct, a different RNG domain. See §7.

This is the **Cadence Vision-Q7 GPSIMD compute core's** own firmware — windowed-ABI Xtensa
code on the `ncore2gp` (Cairo) core — together with the **NX/SEQ sequencer** front-end that
validates the operand before it reaches the compute core. Every device fact below is
byte-pinned to a carve re-derived this session from `libnrtucode_internal.so`; every host-ISA
fact is read out of the public `aws_neuron_isa_tpb_*.h` headers shipped in the same
customop-lib package and **gcc-compiled this session** for offset/sizeof ground truth.
Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

> **NOTE — what was used, and the exact objects.** The firmware container is
> `…/custom_op/c10/lib/libnrtucode_internal.so` (`10,276,288 B`, sha256 `b7c67e898a116454…`
> — the FW-26/FW-27/FW-28/FW-41 anchor, re-verified in-task). The host ISA headers live under
> `…/custom_op/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/`
> (= NeuronCore v2/v3/v4/v5). The two operand structs `S1_RAND` (set) and `D4_RAND` (get) were
> compiled with `gcc` against the **mariana**, **sunda** and **maverick** headers; all reported
> `sizeof`/`offsetof` values below are emitted by that compile, not transcribed. The string
> sweep counts are `strings -a … \| rg -F -c` over the binary. `[HIGH/OBSERVED]`

---

## 1. The headline

1. **The two opcodes are named verbatim and maintained on all four generations.** The
   `NEURON_ISA_TPB_OPCODE` enum gives
   `RAND_GET_STATE = 0x77` and `RAND_SET_STATE = 0x78`, each flagged `// Y` (maintained) in
   **every** gen's `aws_neuron_isa_tpb_common.h`
   (sunda L207/208, cayman L210/211, mariana L215/216, maverick L218/219). `[HIGH/OBSERVED]`
2. **`0x77` is GET (save), `0x78` is SET (restore) — not swapped.** The enum order is explicit
   (`RAND` 0x76 → `RAND_GET_STATE` 0x77 → `RAND_SET_STATE` 0x78), and the struct binding
   confirms the polarity: GET takes the **destination-tensor** struct, SET takes the
   **source-tensor** struct (§3). `[HIGH/OBSERVED]`
3. **The operand structs are compile-verified 64 B.** `0x78 RAND_SET_STATE → S1_RAND_STRUCT`
   (`sizeof==64`, `src_mem_pattern@16` SBUF-only); `0x77 RAND_GET_STATE → D4_RAND_STRUCT`
   (`sizeof==64`, `dst_mem_pattern@44` SBUF+PSUM, shared with the legacy `RAND` 0x76). Both
   pin `dtype == UINT32` (`0x9`). `[HIGH/OBSERVED — gcc compile]`
4. **The saved state is the per-lane PRNG state buffer in memory**, not a hardware register.
   The firmware logs *"Initializing XORWOW state in DRAM scratch"*; the ISA register roster has
   **no** RNG/seed/xorwow state SR/UR. The Get/Set interface exists precisely because the state
   is addressable memory. `[HIGH/OBSERVED]`
5. **The state width is a function of `(engine, generator, algorithm)`** and is fixed by the
   host validators' `legal_combinations` table: Pool keeps Xorwow=6 / LFSR=1 / Philox=7 u32
   words; the wider DVE lanes keep Xorwow=24 / LFSR=4; Act keeps LFSR=2. The `dtype` is `UINT32`
   on every arm. `[HIGH/OBSERVED — s1_rand.h / d4_rand.h verbatim]`
6. **The dispatch is `SEQ → 0xf0 ExtendedInst → per-engine body`.** The NX/SEQ front-end logs
   `S: RandGetState`/`S: RandSetState`; the `0xf0` extended-instruction escape registers
   `ExtendedInstRandGetState`/`ExtendedInstRandSetState`; the per-engine body forks on
   `rand_algorithm` into the shared `Set`/`GetSeeds` bodies (the **same** fork the draw path
   uses). `[HIGH/OBSERVED]`
7. **Per-gen handler presence trails the opcode.** The opcode is `// Y` everywhere, but the
   working algo bodies arrive incrementally: SUNDA defines the op but wires **only** Pool+Philox
   (its `rand_algo` enum has no XORWOW); CAYMAN adds software Xorwow; MARIANA adds LFSR and the
   multi-engine (Pool/DVE/Act) surface; **MAVERICK keeps Pool+DVE but drops the Act arm** (§6).
   `[HIGH/OBSERVED]`
8. **These two close the datapath-RNG opcode surface** (§8) and are kept distinct from the PE
   stochastic-rounding seed op `0x08 PeManageSeed` (§7) and the one-shot `SET_RNG_SEED` pseudo
   `0xd0` (§7.3). `[HIGH/OBSERVED]`

---

## 2. The names + per-gen flag — the 4-gen opcode enum

Source: `aws_neuron_isa_tpb_common.h`, `NEURON_ISA_TPB_OPCODE` enum, per-arch dir
`neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (= NC-v2/v3/v4/v5).

| gen | `common.h` line (get / set) | value | flag |
|---|---|---|---|
| SUNDA (v2) | 207 / 208 | `0x77` / `0x78` | `// Y` / `// Y` |
| CAYMAN (v3) | 210 / 211 | `0x77` / `0x78` | `// Y` / `// Y` |
| MARIANA (v4) | 215 / 216 | `0x77` / `0x78` | `// Y` / `// Y` |
| MAVERICK (v5) | 218 / 219 | `0x77` / `0x78` | `// Y` / `// Y` |

So `RAND_GET_STATE = 0x77`, `RAND_SET_STATE = 0x78`, **both `// Y` (maintained) in all four
gens.** `[HIGH/OBSERVED]`

**Neighbour anchors (mariana `common.h`):** `0x4D RNG // Y` (the POOL Xorwow draw handler,
[Xorwow](rng-xorwow-sw.md)); `0x76 RAND // n, ucode exists, not maintained/used` (the legacy
general-HW draw, dormant); `0x7f DROPOUT // Y` (the inline-RNG consumer, [Dropout](dropout.md));
`0xe2 RAND2 // Y` (the modern DVE draw, [Rand2](rand2.md)). The seed-state pair `0x77/0x78`
stays **maintained across the `RAND → RAND2` generational handoff** — it serves whichever
generator the kernel selects. `[HIGH/OBSERVED — common.h L163/187/214/223/302]`

> **GOTCHA — do not read `RAND` (0x76, `// n`) as a synonym for the seed-state ops.** `0x76` is
> the legacy *draw* op (dormant); `0x77`/`0x78` are the *checkpoint* ops (maintained). They
> share the `D4_RAND` struct on the GET side (§3) but are different opcodes with different
> semantics. `[HIGH/OBSERVED]`

---

## 3. The operand structs — compile-verified

The authoritative struct↔opcode binding is the shipped
`neuron_<gen>_arch_isa/tpb/instruction_mapping.json`:

```jsonc
// neuron_mariana_arch_isa/tpb/instruction_mapping.json
"NEURON_ISA_TPB_D3_RAND_STRUCT": [ "NEURON_ISA_TPB_OPCODE_RAND2" ],            // L64-65
"NEURON_ISA_TPB_D4_RAND_STRUCT": [ "NEURON_ISA_TPB_OPCODE_RAND",              // L76-78
                                   "NEURON_ISA_TPB_OPCODE_RAND_GET_STATE" ],
"NEURON_ISA_TPB_S1_RAND_STRUCT": [ "NEURON_ISA_TPB_OPCODE_RAND_SET_STATE" ],  // L205-206
```

So `0x78 RAND_SET_STATE → S1_RAND`, `0x77 RAND_GET_STATE → D4_RAND` (shared with `0x76 RAND`),
`0xe2 RAND2 → D3_RAND`. `[HIGH/OBSERVED]`

### 3a. `0x78 RAND_SET_STATE` → `S1_RAND_STRUCT` (the SRC / restore variant, 64 B)

Compile-verified offsets (`gcc … ; offsetof`; `ISA_STATIC_ASSERT(sizeof==64)`):

| off | field | type / size | role |
|---|---|---|---|
| 0  | `header` | `NEURON_ISA_TPB_HEADER` (4) | opcode byte @0 = `0x78` |
| 4  | `events` | `NEURON_ISA_TPB_EVENTS` (8) | sync events |
| 12 | `rand_algorithm` | `RAND_ALGORITHM` (1) | `{LFSR=0,PCG32=1,PHILOX=2,XORWOW=3}` |
| 13 | `num_active_channels` | `uint8_t` (1) | live-lane bound (≤ `POOLING_NUM_CHANNELS`) |
| 14 | `update_state` | `RAND_STATE_UPDATE` (1) | `{SRC_TENSOR=0, IMM_SINGLE=1, IMM_LANE=2}` |
| 15 | `in_dtype` | `DTYPE` (1) | pinned `UINT32` (`0x9`) |
| 16 | `src_mem_pattern` | `TENSOR1D` (8) | **the STATE INPUT, SBUF-only** |
| 24 | `rand_generator` | `RAND_SRC` (1) | `{RNG_LFSR=0,RNG_XORWOW=1,RNG_PHILOX=2,OUTPUT_CVT_LFSR=3}` |
| 25 | `reserved[7]` | `uint8_t[7]` (7) | must be 0 (`has_s1_rand_reserved_zero`, checks `[0..6]`) |
| 32 | `imm_state[8]` | `uint32_t[8]` (32) | the 8-word inline immediate state |

`sizeof(NEURON_ISA_TPB_S1_RAND_STRUCT) == 64` `[HIGH/OBSERVED — gcc compile, all gens]`.

Two state-input modes, encoded in `update_state`:

* `update_state == SRC_TENSOR (0)`: the state is read from `src_mem_pattern` (an SBUF
  `TENSOR1D` whose `num_elem[0]` must equal the algo's word count — `rand_set_state_elem_cnt`),
  and **all 8 `imm_state[]` words must be 0** (`rand_set_state_imm_zero`). This is the normal
  *restore from a checkpoint tensor* path.
* `update_state != SRC_TENSOR` (`IMM_SINGLE`/`IMM_LANE`): the state comes inline from
  `imm_state[]`. The only inline-legal generator is Philox, and even then `imm_state[7] == 0`
  is required (`has_valid_rand_set_state_imm`, `s1_rand.h` L162-175). `[HIGH/OBSERVED]`

The `src_mem_pattern` is validated `tensor1d_valid(…, WriteTensor::False, AllowedInPSUM::False,
AllowedInSBUF::True)` — i.e. a **read-only SBUF source** (`s1_rand.h` L128-131). `[HIGH/OBSERVED]`

### 3b. `0x77 RAND_GET_STATE` → `D4_RAND_STRUCT` (the DST / save variant, 64 B, shared with `0x76 RAND`)

Compile-verified offsets (`gcc … ; offsetof`; `ISA_STATIC_ASSERT(sizeof==64)`):

| off | field | type / size | role on GET |
|---|---|---|---|
| 0  | `header` | `HEADER` (4) | opcode byte @0 = `0x77` |
| 4  | `events` | `EVENTS` (8) | sync events |
| 12 | `rand_algorithm` | `RAND_ALGORITHM` (1) | which generator's state to dump |
| 13 | `rand_num_steps` | `uint8_t` (1) | **forced 0** on GET (`rand_get_state_unused_field_zero`) |
| 14 | `post_process` | `RAND_POST_PROC` (1) | **forced 0** on GET |
| 15 | `rand_generator` | `RAND_SRC` (1) | coupled with `rand_algorithm` |
| 16 | `params[4]` | `RAND_PARAM` (16) | **forced 0** on GET |
| 32 | `dtype` | `DTYPE` (1) | pinned `UINT32` (`0x9`, `rand_get_state_valid_dtype`) |
| 33 | `reserved1[1]` | `uint8_t` (1) | must be 0 |
| 34 | `num_active_channels` | `uint8_t` (1) | live-lane bound |
| 35 | `reserved2[9]` | `uint8_t[9]` (9) | must be 0 |
| 44 | `dst_mem_pattern` | `TENSOR4D` (20) | **the STATE OUTPUT, SBUF+PSUM** |

`sizeof(NEURON_ISA_TPB_D4_RAND_STRUCT) == 64` `[HIGH/OBSERVED — gcc compile, all gens]`.

GET reuses the `RAND` op's 4D struct because it **writes** the state out to a 4D destination.
The Rand-op-specific fields (`rand_num_steps`, `post_process`, `params`) are all forced to 0 by
`rand_get_state_unused_field_zero` (`d4_rand.h` L187-194), so on the GET path the struct
degenerates to *(algorithm, generator, dtype=UINT32, num_active_channels, dst_mem_pattern)*. The
`dst_mem_pattern` is validated `tensor4d_valid(…, WriteTensor::True, AllowedInPSUM::True,
AllowedInSBUF::True)` — a **writable** tensor allowed in PSUM **or** SBUF (`d4_rand.h` L213-216).
`[HIGH/OBSERVED]`

> **QUIRK — the source/destination asymmetry is the whole design.** SET reads from an SBUF-only
> `TENSOR1D` source (`@16`, read-only); GET writes to an SBUF-or-PSUM `TENSOR4D` destination
> (`@44`, writable). GET is broader (PSUM allowed) because dumping state to a PSUM bank is cheap;
> SET is narrower (SBUF-only) because the restore source is a model-supplied tensor. The two are
> a deliberate checkpoint pair, not symmetric mirror ops. `[HIGH/OBSERVED]`

### 3c. The element-count contract (the state width is encoded in the tensor shape)

The number of u32 state words is **not** a struct field — it is the element count of the operand
tensor, checked by `rand_set_state_elem_cnt(i, N)` (`src_mem_pattern.num_elem[0] == N`,
`s1_rand.h` L133-135) and `rand_get_state_elem_cnt(i, N)`
(`t4d_element_count(dst_mem_pattern) == N`, `d4_rand.h` L249-251). `N` is fixed per
`(engine, generator, algorithm)` by the `legal_combinations` validators (§4). `[HIGH/OBSERVED]`

---

## 4. The semantics — which state, what width, per engine × algo

The host validators `rand_set_state_legal_combinations` / `rand_get_state_legal_combinations`
(mariana `s1_rand.h` L63-126 / `d4_rand.h` L218-247, read verbatim) enumerate the **exact**
u32 word count per `(engine, generator, algorithm)`. GET mirrors SET arm-for-arm, with one
structural difference: SET splits the Philox arm into two (`IMM_SINGLE` + `SRC_TENSOR`), GET
has a single Philox arm — so **SET has 8 legal arms, GET has 7** on mariana.

| engine | `rand_generator` | `rand_algorithm` | state words (u32) | note |
|---|---|---|---|---|
| Pool | `RNG_PHILOX (2)` | `PHILOX (2)` | **7** | `imm_state[7]==0`; `SRC` or `IMM_SINGLE` (set has both arms) |
| Pool | `RNG_XORWOW (1)` | `XORWOW (3)` | **6** | the 6-word per-lane Xorwow state |
| Pool | `OUTPUT_CVT_LFSR (3)` | `LFSR (0)` | **1** | the 1-word LFSR state |
| DVE | `RNG_XORWOW (1)` | `XORWOW (3)` | **24** | the wide DVE-lane Xorwow state |
| DVE | `RNG_LFSR (0)` | `LFSR (0)` | **4** | DVE LFSR |
| DVE | `OUTPUT_CVT_LFSR (3)` | `LFSR (0)` | **4** | DVE output-convert LFSR |
| Act | `OUTPUT_CVT_LFSR (3)` | `LFSR (0)` | **2** | mariana-only (dropped on maverick, §6) |

`[HIGH/OBSERVED — the eight s1_rand.h arms L64-125 / seven d4_rand.h arms L219-247; every arm
pins `in_dtype/dtype == UINT32`.]`

> **NOTE — `update_state == SRC_TENSOR` on every restore arm.** All eight SET arms except the
> first Philox arm (`IMM_SINGLE`) require `update_state == SRC_TENSOR`. The lone `IMM_SINGLE`
> arm is the inline-Philox seed/restore path (`s1_rand.h` L64-69). Every restore from a
> *checkpoint tensor* uses `SRC_TENSOR`. `[HIGH/OBSERVED]`

### 4a. Which RNG state (the answer)

* **The state save/restored is the per-lane PRNG state buffer held in DRAM/SBUF scratch — not a
  hardware state register.** The firmware string `Xorwow(SW)/Xorwow(TIE) : Initializing XORWOW
  state in DRAM scratch` is observed in the ucode container; the ISA register roster has **zero**
  rng/seed/xorwow state SR/UR. Get/Set read/write the state from/to the operand mem-pattern
  precisely because the state lives in addressable memory. `[HIGH/OBSERVED]`
* **Which generator is selected by the coupled `(rand_generator, rand_algorithm)` pair**, each
  `legal_combinations` arm fixing the coupling (`RNG_XORWOW`↔`XORWOW`,
  `RNG_LFSR`/`OUTPUT_CVT_LFSR`↔`LFSR`, `RNG_PHILOX`↔`PHILOX`). So `0x77`/`0x78` are the
  **generic** checkpoint op for whichever `rand_algo` the kernel uses. `[HIGH/OBSERVED]`

**State formats (cross-anchored to the algo kernels):**

* **XORWOW** — the 6-word per-lane state `(x, y, z, w, v` = 5 xorshift words `+ d` = Weyl
  counter`)`. The [Xorwow SW path](rng-xorwow-sw.md) recovers the Marsaglia default seed vector
  byte-exact (`x=0x075BCD15, y=0x159A55E5, z=0x1F123BB5, w=0x05491333, v=0x00583F19`) with Weyl
  increment `d += 362437 (0x000587C5)`. On Pool the checkpoint width is **6**; on the wider DVE
  lanes it is **24** (the DVE Xorwow keeps a 4×-wider per-lane state). `[HIGH/OBSERVED — width
  from validator; seed/Weyl CARRIED from the Xorwow page]`
* **LFSR** — **one** u32 word per lane. `d4_rand.h` states it verbatim: *"LFSR treats each of
  the 128 lanes as an independent random number generator. The lowest u32 word of the rand_state
  (rand_state[0]) for each lane is used as the initial state."* Pool checkpoint width **1**;
  DVE **4**; Act **2**. `[HIGH/OBSERVED — d4_rand.h L52-60 + validator]`
* **PCG32** — two u32 words (`rand_state[0..1]`); ISA-defined (`d4_rand.h` L62-71) but
  **POOL-unsupported** (§5b). `[HIGH — header text; no live arm]`
* **PHILOX** — counter-based: a 16-byte counter start, an 8-byte key, and a 4-byte counter
  offset, "saved/restored unmodified for model checkpointing" (`d4_rand.h` L73-83). Pool
  checkpoint width **7** (the 7 immediate/state words with the `imm_state[7]==0` constraint).
  `[HIGH/OBSERVED — the Philox arm + header prose]`
* `num_active_channels` (`S1_RAND@13` / `D4_RAND@34`) bounds the checkpoint to the live lane
  subset (validated against `POOLING_NUM_CHANNELS`); `has_valid_rand_state_channels` couples the
  channel count to the algorithm. `[HIGH/OBSERVED — validators]`

### 4b. How it ties to the `rand_algo` fork

The `rand_algorithm` field **is** the `rand_algo` enum
`{LFSR=0, PCG32=1, PHILOX=2, XORWOW=3}` (mariana `common.h` L987-992). The **same** enum that
forks the draw path in the shared `SetSeeds` body forks the checkpoint here. From the
instruction-exact [LFSR dispatch tree](rng-lfsr-dispatch.md):

```c
// Pool/Q7 RandSetState (MARIANA TIE), all addresses byte-pinned in rng-lfsr-dispatch.md
//
// SEQ front-end → 0xf0 ExtendedInstRandSetState (decode fn 0x87c0, log DRAM 0x1aa1)
//   → Q7 RandSetState dispatcher 0xb5f4   (entry 48; log 0x1e80 "RandSetState: num_chans / rand_algo")
//       reads rand_algorithm = i.s1_rand.rand_algorithm   // [a1+12]
//   → dispatcher 0xb640: algo_select = saltu(rand_algo, 1) // == (rand_algo == 0)  [b67a]
//   → shared SetSeeds 0xb6dc  (THE FORK):
//       b6e5: bbci a2, 0, 0xb6f5         // bit 0 of the stored algo flag at [a1+12]
//         rand_algo == 0 (LFSR)   → fall through → LfsrSetSeeds        0xb700 (entry a1,192)
//         rand_algo == 3 (XORWOW) → branch        → XorwowSetSeeds(TIE) 0xb744 (entry a1,0x400)
//
// RandGetState (0x77) mirrors it:
//   SEQ → 0xf0 ExtendedInstRandGetState (decode 0x8688, log 0x1a3f)
//   → Q7 RandGetState dispatcher 0xb930 (log 0x1ee5)
//   → shared GetSeeds 0xb9ec (fork bbci a2,0,0xba05):
//         LFSR   → LfsrGetSeeds                    (log 0x1f1d)
//         XORWOW → XorwowGetSeeds(TIE) 0xba58 (entry a1,0x580, log 0x1f30)
```

So `0x77`/`0x78` are **algo-parametric**: the firmware reads `rand_algorithm@12`, runs the
`{0,3}` POOL gate (§5b), forks to the Xorwow-vs-LFSR seed body, and that body's per-lane state
geometry **is** the checkpoint payload. The frame sizes at each arm entry
(`LfsrSetSeeds entry a1,192`; `XorwowSetSeeds(TIE) entry a1,0x400`;
`XorwowGetSeeds(TIE) entry a1,0x580`) are the inner per-lane buffers; the operationally relevant
*checkpoint* width is the validator element count (§4). `[HIGH/OBSERVED — fork instructions,
entries, logs byte-pinned in rng-lfsr-dispatch.md / rng-xorwow-tie.md; CARRIED here]`

> **CORRECTION — `LfsrSetSeeds 0xb700` / `XorwowSetSeeds(TIE) 0xb744` are documented on the
> [LFSR dispatch page](rng-lfsr-dispatch.md), not the TIE page.** The [TIE-Xorwow page](rng-xorwow-tie.md)
> maps both SET arms to the *shared* entry `0xb6dc` and lists only the arm log xrefs
> (`0xb716` LFSR / `0xb75a` Xorwow). Cite the LFSR page for the `0xb700`/`0xb744` arm entries.
> `[HIGH/OBSERVED]`

---

## 5. The dispatch surface — SEQ → `0xf0` ExtendedInst → per-engine body

### 5a. The front-end is the NX/SEQ sequencer

The dispatch surface is **SEQ** (the NX sequencer front-end), which validates the operand and
then reaches the per-engine kernel body via the `0xf0` extended-instruction escape — the
seed-state ops are **not** in the Q7KIT EXTISA table directly. The string sweep over the ucode
container confirms the chain (counts are `strings -a … \| rg -F -c` over `libnrtucode_internal.so`):

| string | count | role |
|---|---|---|
| `S: RandGetState` / `S: RandSetState` | 11 / 11 | the SEQ front-end (present on **every** gen incl. SUNDA) |
| `ExtendedInstRandGetState` / `ExtendedInstRandSetState` | 8 / 8 | the `0xf0` escape registration on the per-engine `kernel_info` table |
| `RandGetState` / `RandSetState` | 26 / 26 | total (front-end + decode + dispatcher logs + bodies) |
| `XorwowGetSeeds` / `XorwowSetSeeds` | 7 / 7 | the Xorwow seed bodies (`(SW)` cayman + `(TIE)` mariana+) |
| `LfsrGetSeeds` / `LfsrSetSeeds` | 5 / 5 | the LFSR seed bodies (mariana+ only — **zero on sunda/cayman**) |
| `not currently supported on POOL` | 6 | the `{0,3}` algo gate (§5b) |

`[HIGH/OBSERVED — string sweep this task]`

The observed dispatcher log formats are verbatim:

```
P%i: Decode : ExtendedInstRandGetState
P%i: ExtendedInstRandGetState : num_tensor_elements = %d
P%i: RandGetState : num_chans = %0d : rand_algo = 0x%x
P%i: XorwowGetSeeds(SW)
P%i: XorwowGetSeeds(TIE)
P%i: LfsrGetSeeds
P%i: Xorwow(TIE) : Initializing XORWOW state in DRAM scratch
```

`[HIGH/OBSERVED — strings -a libnrtucode_internal.so]`

### 5b. The `{0, 3}`-algo POOL gate

The SEQ front-end validates `rand_algo ∈ {0=LFSR, 3=XORWOW}`; the other two enum values
(`1=PCG32`, `2=PHILOX`) hit the gate string (observed verbatim, 6 occurrences):

```
S: RandGetState : rand_algorithm(0x%x) not currently supported on POOL
S: RandSetState : rand_algorithm(0x%x) not currently supported on POOL
```

So on **POOL** only LFSR + Xorwow state save/restore is wired; PCG32/Philox are ISA-defined but
POOL-unsupported. `[HIGH/OBSERVED — gate string + the LFSR-dispatch `{0,3}` gate at `0x301b`]`

> **GOTCHA — the host validator is broader than the wired POOL handler.** Philox *is* a legal
> arm in both `legal_combinations` validators (the 7-word Pool/Philox arm), yet the POOL firmware
> gate rejects `rand_algo==2` at runtime with the "not currently supported on POOL" string. The
> compile-time legal set (host ISA validator) and the runtime-wired handler set (POOL firmware)
> are **not identical** — Philox is admissible to the validator but dead on the POOL body.
> `[HIGH/OBSERVED]`

### 5c. The same opcode has two engine bodies

The engine discriminator is the host validator's `NeuronEngine` argument, which selects the legal
`(algo, state-width)` set. The same opcode `0x78` has a **Pool body** (`0xb5f4`, §4b) **and** a
**DVE body** (`0xebe0`); the engine field of the instruction picks which. The DVE
`RandSetState` body is registered in the DVE `kernel_info` table immediately alongside Rand2 and
Exponential — confirmed in [Rand2](rand2.md):

```c
// DVE kernel_info registration stubs (consecutive), from rand2.md §5a:
230e: const16 a2, 0xebe0   // handler 0xebe0 = "S: RandSetState"   (the DVE RandSetState body)
232a: const16 a2, 0xf2cc   // handler 0xf2cc = "S: Rand2"
2346: const16 a2, 0xf698   // handler 0xf698 = "S: Exponential"
```

`[HIGH/OBSERVED — the DVE RandSetState stub + name string; the opcode→funcVA descriptor literal
itself is runtime-bound / out-of-carve, MED for the exact byte edge.]`

> **NOTE — the device-side VAs are device VAs, not host symbols.** The dispatcher/seed VAs
> (`0xb5f4`, `0xb930`, `0xb6dc`, `0xb9ec`, `0xb744`, `0xba58`, `0xb700`, `0xebe0`) are
> **Q7/DVE ucode** addresses inside the embedded blob, recovered with the native
> `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) on the committed Xorwow/LFSR pages. They do **not**
> appear in the host-x86 IDA function DB of `libnrtucode_internal.so` (host text spans
> `0x9b01a0–0x9bb600`) — the *strings* for these routines are in `.rodata` (re-confirmed this
> task), the *VAs* are CARRIED from the device disassembly on the sibling pages. `[VAs CARRIED;
> strings HIGH/OBSERVED]`

---

## 6. Per-generation presence — opcode vs handler vs validator surface

The opcode is `// Y` on all four gens, but two finer surfaces lag it: the **host validator's
legal-combination set** (which engines/algos are admissible) and the **runtime handler body**
(which algo seed save/restore code actually ships). Both were re-derived this session — header
arm-counts + the ucode string sweep agree.

| gen | opcode | validator engine(s) | legal arms (set / get) | `rand_algo` enum | RNG handler bodies present |
|---|---|---|---|---|---|
| SUNDA (v2) | `// Y` | Pool only (fixed arg) | 2 / 1 | LFSR, PCG32, PHILOX (**no XORWOW**) | **none wired** — SEQ string + `0xf0` decode exist, no algo body |
| CAYMAN (v3) | `// Y` | Pool only (fixed arg) | 3 / 2 | + XORWOW | `XorwowSet/GetSeeds(SW)` ([SW path](rng-xorwow-sw.md)); **no LFSR** |
| MARIANA (v4) | `// Y` | Pool + DVE + Act (`e` arg) | 8 / 7 | full 4-value | `XorwowSet/GetSeeds(TIE)` + `LfsrSet/GetSeeds` + DVE `RandSetState 0xebe0` |
| MAVERICK (v5) | `// Y` | **Pool + DVE only** (`e` arg) | **7 / 6** | full 4-value | same TIE + LFSR; **Act arm dropped** |

`[HIGH/OBSERVED — per-gen common.h + s1_rand.h/d4_rand.h arm counts (verbatim this session) +
ucode string sweep]`

Two byte-grounded structural facts make this table exact:

1. **SUNDA's `RAND_ALGORITHM` enum is truncated.** `neuron_sunda_arch_isa/tpb/common.h` L875-877
   defines only `LFSR=0, PCG32=1, PHILOX=2` — **no `XORWOW=3`** — and the `RAND_SRC` enum has only
   `RNG_LFSR=0, RNG_PHILOX=2` (no `RNG_XORWOW`, no `OUTPUT_CVT_LFSR`). SUNDA's only legal
   combination is **Pool + Philox** (`d4_rand.h` GET elem_cnt 7; `s1_rand.h` SET 2 Philox arms),
   and there is no wired Xorwow/LFSR POOL body. So on SUNDA the op exists at the ISA/decode layer
   but has no working save/restore body. `[HIGH/OBSERVED — sunda common.h enums]`
2. **The validator call signature encodes the engine surface.** SUNDA/CAYMAN call the validator
   with a *fixed* `NeuronEngine::Pool` argument (`s1_rand.h` L54 / `d4_rand.h` L147); MARIANA/
   MAVERICK take the engine `e` as a parameter (`rand_set_state_legal_combinations(i, e)`), and
   their arms branch on `e == Pool | DVE | Act`. That is the concrete encoding of "Pool-only on
   sunda/cayman, multi-engine on mariana+." `[HIGH/OBSERVED]`

> **CORRECTION — MAVERICK is Pool+DVE, not the full Pool+DVE+Act.** Header arm-count
> (`s1_rand.h` 7 set / `d4_rand.h` 6 get) is exactly one fewer than MARIANA's (8/7). The missing
> arm is the **Act-engine LFSR** arm (`e == NeuronEngine::Act`, `OUTPUT_CVT_LFSR`, 2 words):
> present on MARIANA (`s1_rand.h` L118, `d4_rand.h` L243), **absent on MAVERICK** (zero
> `NeuronEngine::Act` matches in either MAVERICK header). The Act LFSR state-checkpoint arm is a
> MARIANA-only feature. `[HIGH/OBSERVED — arm grep across all four gens this session]`

**Tonga (NeuronCore-v1) note.** The V1 (`tonga`) header set ships **no** `s1_rand.h`/`d4_rand.h`
and **no** `RAND_GET_STATE`/`RAND_SET_STATE` opcodes — this op family begins at SUNDA (v2).
`[HIGH/OBSERVED — arch-isa/tpb/ absence]`

---

## 7. The two RNG-state domains — GpSimd-RNG vs PE-SR-RNG

There are **two physically and architecturally distinct** RNG state domains. `0x77`/`0x78`
belong to the first; `0x08 PeManageSeed` to the second. They share the word "seed/state" only.

### 7.1 Domain A — GpSimd / datapath RNG (`0x77` / `0x78`, this page)

* **Engine:** Pool / DVE / Act vector datapath.
* **State:** the per-lane PRNG state in DRAM/SBUF scratch — Xorwow 6w \| LFSR 1w \| PCG32 2w \|
  Philox 7w on Pool; wider on DVE (Xorwow 24, LFSR 4); Act LFSR 2.
* **Algorithm:** `rand_algo`-selected `{LFSR, PCG32, PHILOX, XORWOW}`.
* **Op family:** `RAND (0x76)` / `RAND2 (0xe2)` advance; `RAND_GET_STATE (0x77)` /
  `RAND_SET_STATE (0x78)` **checkpoint**; `SET_RNG_SEED` pseudo (§7.3) seeds.

### 7.2 Domain B — PE stochastic-rounding RNG (`0x08 PeManageSeed`)

From [PE Matrix-Multiply Path](pe-matmul.md):

* **Opcode:** `NEURON_ISA_TPB_OPCODE_PE_MANAGE_SEED = 0x08` (`// Y`, `common.h` L163); **v4+ only.**
* **Engine:** PE systolic array (TensorE).
* **State:** the PSUM fp32→bf16 stochastic-rounding seeds — *not* a per-cell PRNG. (NC-v4 keeps
  thousands of 32-bit seeds across the PSUM banks.)
* **Struct:** `NEURON_ISA_TPB_S2S1D2_PE_SEED_STRUCT` (`sizeof==64`, compile-verified).
* **Modes:** `NEURON_ISA_TPB_PE_SEED_MODE { NONE = 0, LOAD_SEED = 1, SAVE_SEED = 2 }`
  (`common.h` L1132-1136).
* **Consumer:** matmul PSUM drain to BF16. `[HIGH/OBSERVED — pe-matmul.md, common.h]`

> **CORRECTION — use the enum, not the prose, for `PE_SEED_MODE`.** The `s2s1d2_pe_seed.h` field
> comment reads "LoadSeed (value of 0) / SaveSeed (value of 1)", but the authoritative C enum is
> `NONE=0, LOAD_SEED=1, SAVE_SEED=2`. The prose comment is off by one; trust the enum.
> `[HIGH/OBSERVED]`

**The decisive distinction:** different opcode (`0x08` vs `0x77`/`0x78`), different engine (PE vs
Pool/DVE/Act), different struct (`S2S1D2_PE_SEED` vs `D4_RAND`/`S1_RAND`), different state (PSUM
SR seeds vs the algo PRNG state), different purpose (deterministic stochastic-rounding dither vs
user-RNG reproducibility). **Do not conflate them.** `[HIGH/OBSERVED]`

### 7.3 The lesser cousin — `SET_RNG_SEED` (pseudo `0xd0`)

`SET_RNG_SEED` is a **pseudo** opcode (`NEURON_ISA_TPB_OPCODE_PSEUDO_SET_RNG_SEED = 0xd0`,
mariana `common.h` L284; sunda L277), NRT-translated, struct
`NEURON_ISA_TPB_PSEUDO_SET_RNG_SEED_STRUCT` (`sizeof==64`, compile-verified this session:
`seed_src@12`, `seed@16`). Its operand is `seed_src` and a **single 4-byte `seed@16`**
(`IMM_VAL`); the header comment reads *"sets RNG seed for engine; Only DVE supported for now."* It
**seeds** domain A (one-shot init) and is lowered by NRT into a real op — it is **not** a state
checkpoint and **not** `0x77`/`0x78`. `[HIGH/OBSERVED — aws_neuron_isa_tpb_pseudo_set_rng_seed.h +
gcc compile (seed@16, seed_src@12, sizeof 64)]`

---

## 8. The closed RNG opcode surface

With `0x77`/`0x78` decoded, the datapath-RNG (domain A) opcode surface is **closed**:

| op | name | struct | flag | role | page |
|---|---|---|---|---|---|
| `0x4D` | `RNG` | (POOL) | `YYYY` | POOL Rng(XORWOW) draw handler | [Xorwow SW](rng-xorwow-sw.md) / [TIE](rng-xorwow-tie.md) |
| `0x76` | `RAND` | `D4_RAND` | `nnnn` | legacy general-HW draw (dormant) | [Rand2](rand2.md) |
| `0x77` | `RAND_GET_STATE` | `D4_RAND` | `YYYY` | **SAVE** generator state | *this page* |
| `0x78` | `RAND_SET_STATE` | `S1_RAND` | `YYYY` | **RESTORE** generator state | *this page* |
| `0xE2` | `RAND2` | `D3_RAND` | `YYYY` | modern DVE draw (Uniform) | [Rand2](rand2.md) |
| `0x7F` | `DROPOUT` | `S3D3_DROP` | `YYYY` | inline-RNG consumer (LFSR arm) | [Dropout](dropout.md) |
| pseudo `0xd0` | `SET_RNG_SEED` | `PSEUDO_*` | — | one-shot seed (NRT-lowered) | §7.3 |

The `rand_algo` enum `{LFSR=0, PCG32=1, PHILOX=2, XORWOW=3}` (the [dispatch fork](rng-lfsr-dispatch.md)),
the draw kernels (`0x4D`/`0x76`/`0xe2` + Dropout), the seed-init pseudo (`0xd0`), and now the
state-checkpoint pair (`0x77`/`0x78`) account for **every** datapath-RNG opcode in the four-gen
ISA. The PE stochastic-rounding seed op (`0x08`, domain B) is a separate subsystem
([PeManageSeed](pe-matmul.md)). `[HIGH/OBSERVED]`

---

## 9. Walls and residual uncertainty

* **The handler bodies' inner state-write/advance micro-ops live in FLIX-desync'd Q7/DVE bundles.**
  The flat DEBUG IRAM has no `.xt.prop` property table (the project-wide FLIX-desync wall the
  Xorwow/LFSR pages hit). The dispatcher **entries**, the **fork instructions** (`bbci`,
  `saltu`), the **log strings**, the **frame sizes**, and the **call edges** are byte-pinned
  (HIGH); the per-word shift/xor inside the seed bodies is **not** byte-recoverable (LFSR taps
  LOW/UNRECOVERED). Decoding the checkpoint *dispatch + struct + semantics* does **not** require
  the desync'd inner bundles: the state **width/format** come from the host validators (HIGH), the
  dispatch from the pinned entries (HIGH). `[desync flagged; not needed for this page]`
* **The DVE `opcode→funcVA` descriptor literal** (the byte binding `0x78 → 0xebe0`) is in the
  runtime-bound kernel-descriptor region. The funcVA `0xebe0` body + `"S: RandSetState"` name
  string are HIGH/OBSERVED; the table→funcVA byte edge is MED/out-of-carve. `[flagged]`
* **The exact TIE Xorwow inner state-buffer byte size on POOL** (the `entry a1,0x400` /
  `0x580` frames) is MED (obscured by desync; inherited `0x180`=384-byte geometry from the
  [SW path](rng-xorwow-sw.md)). The operationally relevant **checkpoint** width is fixed by the
  validator at **6 words / Pool** (HIGH), which overrides the MED inner-buffer figure for the
  `0x77`/`0x78` operand. `[reconciled: validator width HIGH]`
* **The Xorwow Marsaglia seed constants and Weyl increment** are CARRIED from the
  [SW path](rng-xorwow-sw.md); they are *referenced* here, not re-derived. `[CARRIED]`

---

## 10. Confidence summary

| area | tag |
|---|---|
| The names (`0x77 GET` / `0x78 SET`), `// Y` all four gens, enum order | HIGH / OBSERVED |
| Struct bindings (`instruction_mapping.json`) + `S1_RAND`/`D4_RAND` compile-verify (sizeof 64, offsets) | HIGH / OBSERVED |
| Per-`(engine, generator, algorithm)` state-width table (`s1_rand.h`/`d4_rand.h` arms) | HIGH / OBSERVED |
| Dispatch chain (`SEQ → 0xf0 ExtendedInst → Pool/DVE body`), fork, logs, string-sweep counts | HIGH / OBSERVED |
| `{0,3}` POOL gate; `UINT32` (`0x9`) dtype | HIGH / OBSERVED |
| Per-gen presence (SUNDA-none / CAYMAN-XORWOW / MARIANA full / MAVERICK Pool+DVE) | HIGH / OBSERVED |
| Two-domain (GpSimd-RNG vs PE-SR-RNG `0x08`) + pseudo `0xd0` distinction | HIGH / OBSERVED |
| Device-side dispatcher/seed VAs (`0xb5f4`…`0xebe0`) | CARRIED (device disasm, sibling pages) |
| DVE `opcode→funcVA` descriptor byte; exact TIE inner state-buffer size | MED / out-of-carve |
| LFSR feedback taps + inner per-word state-advance micro-ops (FLIX-desync) | LOW / UNRECOVERED |
