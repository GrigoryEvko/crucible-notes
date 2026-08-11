# MARIANA × POOL image (dual-core)

The **MARIANA × POOL** firmware image is the NC-v4 generation of the **central general-compute
engine** (`engine_idx = 2`) — the **only** one of the five NX engines that ships **two distinct
firmware cores**. This page is a **cross-generation DIFF** against the committed
[CAYMAN × POOL](./cayman-pool.md) baseline (NC-v3); it does **not** re-derive the dual-dispatch
two-core model from scratch — that model, the 178-slot SEQ hub, the `kernel_info_table` back-end,
the `0xF0` reconciliation, and the 41-handler roster mechanics are all established on the CAYMAN
page and are diffed here, not rebuilt. Read CAYMAN × POOL first; this page records **only what
changed** across the v3 → v4 gap, for **both** cores:

1. **`NX_POOL`** — the NX-class SEQ sequencer (`'S:'` dialect). On MARIANA its reset vector shifts
   **+0x1c** (`06 76 00 00` → `06 7d 00 00`), but its handler set, opcode space, and dispatch form
   are **unchanged** — POOL already shipped the richest SEQ handler set on CAYMAN, so there was
   nothing to add at the SEQ layer.
2. **`Q7_POOL`** — the per-pool-core compute engine (`'P%i:'` dialect, `kernel_info_table` linear
   scan). Its reset vector is **byte-identical** to CAYMAN (no +0x1c shift), and its
   `(opcode, spec)` key set is **byte-for-key identical**. The one substantive v4 change lives
   **inside** the existing RNG kernel body.

**The headline is the RNG arrival.** The CAYMAN Q7_POOL ships the Marsaglia Xorwow **software**
path only (`Xorwow(SW)`). MARIANA Q7_POOL replaces it with the `Xorwow(TIE)` build variant **and**
adds a **second algorithm — LFSR** (`LfsrGetSeeds` / `LfsrSetSeeds`), selected by a `rand_algo`
fork *inside* the kernel body — **without** a new SEQ handler, a new opcode, or a new
`kernel_info_table` row. This is the firmware-image realization of the SW → TIE+LFSR RNG boundary
that [RNG — Xorwow TIE Path](../firmware/kernels/rng-xorwow-tie.md),
[RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md), and
[RNG Seed-State Ops](../firmware/kernels/rng-seed-state-ops.md) reconstruct at the kernel level.

The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag,
following the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`.

> **NOTE — the objects used + the baseline carve.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**). The first R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`),
> so each `_get.data` accessor address is simultaneously the `.rodata` VA and the file offset:
> carve = `so[ptr : ptr+size]`. All 46 MARIANA POOL blob VAs (`0x34b720…0x5a3480`) fall inside
> this R `LOAD`. Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo,
> Xtensa24, RI-2022.9, `NX1.1.4`, FLIX/VLIW 32B). **The CAYMAN POOL baseline is carved and
> hashed from the same container for an apples-to-apples diff — all 8 anchor sha256 MATCH the
> committed CAYMAN × POOL page** (NX_DEBUG_IRAM `8e4412b9`, NX_DEBUG_DRAM `7bdf6ed7`, NX_PERF_IRAM
> `9049bf8c`, NX_PROF_CAM `8fd7e422`, NX_PROF_TABLE `ce761f81`, Q7_DEBUG_IRAM `513a8a22`,
> EXTISA_0_SO `910d41c3`, EXTISA_3_SO `052ac31c`) — so the diff is against the authentic CAYMAN
> image, not a paraphrase of it.

> **GOTCHA — carve the blobs, not the stubs.** The getter accessor lives in `.text` (e.g.
> `MARIANA_NX_POOL_DEBUG_IRAM_get` at `.text` VA `0x9b4320`), and `.text` carries the `0x2000`
> VA − file-offset delta. The blob VAs in `.rodata` are identity-mapped, so the *carved blobs* are
> correct as long as you carve at the `IMG-PTR` (`.data` accessor address), never the stub.

---

## 1. The cross-gen verdict (per core)

| Axis | `NX_POOL` (SEQ) | `Q7_POOL` (compute) |
| --- | --- | --- |
| reset vector | **SHIFTED +0x1c** (`06 76 00 00`→`06 7d 00 00`, `j 0x1dc`→`j 0x1f8`) | **UNCHANGED** (`06 7f 00 00`, `j 0x200`) |
| dispatch form | `addi a2,a2,-65` + `movi a3,177` — **unchanged** (base `0x41`, bound 177) | `kernel_info_table` linear scan — **unchanged** |
| opcode space | 177 entries (54 real / 123 default) — **unchanged, NO growth** | 17/1/2/9 KIT entries — **key set byte-for-key identical** |
| handler / key set | 41 handlers, **+0 / −0** (byte-for-name identical) | `(opcode,spec)` keys **identical**; funcVAs pure relocation `+0x0..+0x44` |
| `0xF0` bridge | slot `0x3190`→`0x306d` (real, ≠ default) — **intact** | five `0xf0+spec` rows — **byte-for-key on both gens** |
| **kernel bodies** | recompile only (no new logic) | **`Xorwow(SW)` → `Xorwow(TIE)` + new `LFSR` — the v4 RNG arrival** |
| PROF | re-preallocated + **disarmed** + now **per-engine** (`8fd7e422`→`0951b326`) | (no PROF) |
| size delta | IRAM **shrank** (DEBUG −0x7a0, PERF −0x2d60, TEST −0x27c0) | IRAM **grew** (DEBUG +0x300 — the new RNG body) |
| new source | `addr_bits.hpp` (gen-wide MARIANA address rerouting) | (none new; `dispatch.hpp` unchanged) |

The two cores **diverge in how they absorb the generation**: the NX SEQ front-end is recompiled
and reset-shifted but otherwise frozen; the Q7 compute core grows the RNG kernel. **No MARIANA POOL
image is byte-identical to its CAYMAN counterpart** (20/20 distinct — a full recompile, not a
patch), yet the *dispatch mechanism, reset-vector form, `0xF0` bridge, KIT key set, ErrorHandler
arms, `cayman/seq` + `dispatch.hpp` source trees, and the dual-core split* are all **invariant**.
A CAYMAN ↔ MARIANA POOL swap is **a recompile + the NX +0x1c reset shift + `addr_bits.hpp` + the Q7
`Xorwow(SW)→TIE+LFSR` RNG body + re-preallocated/disarmed PROF**, not a model change.

> **NOTE — POOL is the RNG origin, not a recipient.** The cross-engine RNG migration the
> [MARIANA × ACT](./mariana-act.md) / [MARIANA × DVE](./mariana-dve.md) pages document
> (`RandGetState`/`RandSetState` appearing on ACT/DVE this gen) runs the **other** direction: POOL
> shipped `RandGetState`/`RandSetState` as POOL-only handlers on CAYMAN; v4 keeps POOL's handlers
> and gives DVE/ACT copies. POOL is the **origin engine** for the RNG handler pair.

---

## 2. The dual-core inventory + carve (46 getters, the SAME shape as CAYMAN)

`nm libnrtucode_internal.so | rg -c 'MARIANA_(NX|Q7)_POOL_.*_get$'` (excluding `PLUS`) = **46**:
**14 `NX_POOL`** + **32 `Q7_POOL`**, identical split to CAYMAN POOL (28 real + 18 zero-size
boundary cursors). Each getter is the canonical 4-instruction `(img-ptr, size)` stub.

### 2a. `NX_POOL` (CLS = NX) — 14 getters (the SEQ sequencer)

| VARIANT | REGION | IMG-PTR (.rodata = file off) | SIZE | STATUS |
| --- | --- | --- | --- | --- |
| PERF | IRAM | `0x34b720` | `0x14520` | REAL (SEQ code) |
| PERF | DRAM | `0x35fc40` | `0x03180` | REAL (SEQ data, no `S:`) |
| PERF | SRAM/EXTRAM | `0x362dc0` | `0` | EMPTY (boundary cursor) |
| TEST | IRAM | `0x3bb840` | `0x14240` | REAL |
| TEST | DRAM | `0x3cfa80` | `0x03480` | REAL |
| TEST | SRAM/EXTRAM | `0x3d2f00` | `0` | EMPTY |
| **DEBUG** | **IRAM** | `0x44b540` | `0x1c080` | REAL (code; sha `41b6c798`) |
| **DEBUG** | **DRAM** | `0x4675c0` | `0x07000` | REAL (data + 177 `S:` logs; sha `ec067304`) |
| DEBUG | SRAM/EXTRAM | `0x46e5c0` | `0` | EMPTY |
| **PROF** | **CAM** | `0x5a3080` | `0x00400` | REAL (sha `0951b326` — disarmed) |
| **PROF** | **TABLE** | `0x5a3480` | `0x02000` | REAL (sha `534f2239` — re-prealloc) |

### 2b. `Q7_POOL` (CLS = Q7) — 32 getters (compute + DKL + EXTISA)

| VARIANT | REGION | IMG-PTR | SIZE | STATUS |
| --- | --- | --- | --- | --- |
| PERF | IRAM | `0x48dae0` | `0x164e0` | REAL (Q7 compute code) |
| PERF | DRAM | `0x4a3fc0` | `0x13180` | REAL (Q7 data) |
| TEST | IRAM | `0x4b7140` | `0x17e80` | REAL |
| TEST | DRAM | `0x4cefc0` | `0x13480` | REAL |
| **DEBUG** | **IRAM** | `0x4e2440` | `0x1ed40` | REAL (code; sha `47f76629`) |
| **DEBUG** | **DRAM** | `0x501180` | `0x15d80` | REAL (data + 158 `P%i:` logs; sha `02cacff0`) |
| DKL_PERF | IRAM | `0x516f00` | `0x101c0` | REAL (dyn-kernel-load build; sha `8a8c927a`) |
| DKL_PERF | DRAM | `0x5270c0` | `0x13c80` | REAL (sha `0aaa01a7`) |
| **DKL_DEBUG** | **IRAM** | `0x53ad40` | `0x14140` | REAL (+ `P%i:` logs; sha `22790dbe`) |
| **DKL_DEBUG** | **DRAM** | `0x54ee80` | `0x16700` | REAL (sha `a01f5d43`) |
| DKL_TEST | IRAM | `0x565580` | `0x101c0` | REAL (**== DKL_PERF**, §7) |
| DKL_TEST | DRAM | `0x575740` | `0x13c80` | REAL (**== DKL_PERF**) |
| PERF_EXTISA_0 | SO | `0x5893c0` | `0x0a260` | REAL (`EM_XTENSA` ELF; 17-entry KIT; sha `9f2ce049`) |
| PERF_EXTISA_1 | SO | `0x593640` | `0x00f5c` | REAL (ELF; 1-entry KIT) |
| PERF_EXTISA_2 | SO | `0x5945c0` | `0x01500` | REAL (ELF; 2-entry KIT) |
| PERF_EXTISA_3 | SO | `0x595ae0` | `0x06974` | REAL (ELF; 9-entry KIT; cptc family; sha `8477ff26`) |
| PERF_EXTISA_{0–3} | JSON | (4 blobs) | `0x20` | REAL (dummy `{"dummy_message":"hello world"}`) |

28 real carves + 12/12 spot-reconciled byte-identical (sha256 + `cmp -s`) to the matching
`libnrtucode.a` member `.rodata` across NX/Q7/EXTISA/PROF/DKL/JSON; the internal-`.so` getter blob
== the `.a` member `.rodata`.

> **NOTE — engine ordering confirms POOL is LAST.** PE PERF_DRAM ends `@0x34b720` == MARIANA
> `NX_POOL` PERF_IRAM start — exactly the contiguity cursor the
> [MARIANA × PE](./mariana-pe.md) carve predicted. The ACT→DVE→PE→POOL layout adjacency holds; the
> last POOL blob (`EXTISA_3_JSON` end `0x59c480`) precedes the PROF tables `@0x5a3080`.

> **QUIRK — `DKL_TEST` is `DKL_PERF` (still).** `cmp` confirms MARIANA `DKL_PERF_IRAM ==
> DKL_TEST_IRAM` (`8a8c927a`) and `DKL_PERF_DRAM == DKL_TEST_DRAM` (`0aaa01a7`) — the same
> "DKL has only a DEBUG-vs-release split, no separate TEST flavor" property as CAYMAN.

---

## 3. The reset/boot diff — the +0x1c NX-only shift

The byte-level signature that `NX_POOL` and `Q7_POOL` are **two separate cores even on MARIANA**,
and that they absorbed the generation **differently**: the NX core's reset trampoline shifted, the
Q7 core's did not.

**(A) `NX_POOL` — flat, reset SHIFTED +0x1c (the MARIANA gen change):**

```text
IRAM head (all 3 variants byte-identical): 06 7d 00 00 00 00  86 7e 00 00 00 00
  0x000: 06 7d 00   j 0x1f8       ; primary reset -> boot
  0x006: 86 7e 00   j 0x204       ; secondary    -> halt
  0x1f8: const16 a0,0 ; const16 a0,0x90 ; jx a0   -> enter_run @0x90
  0x204: halt 0
DRAM head = 34 cb 99 60                              (header word 0x6099cb34, unchanged)
```

CAYMAN `NX_POOL` was `06 76 00` (`j 0x1dc`) / `86 77 00` (`j 0x1e8`). The boot targets each moved
**+0x1c** (`0x1f8 − 0x1dc = 0x1c`; `0x204 − 0x1e8 = 0x1c`) — the **identical +0x1c shift** the
MARIANA ACT/DVE/PE carves carry.

**(B) `Q7_POOL` (incl. DKL) — flat, reset UNCHANGED:**

```text
IRAM head (all variants): 06 7f 00 00 00 00  86 80 00 00 00 00
  0x000: 06 7f 00   j 0x200       ; primary reset -> boot   (== CAYMAN, byte-identical)
  0x006: 86 80 00   j 0x20c       ; secondary    -> halt
  0x200: const16 a0,0 ; const16 a0,0x90 ; jx a0   -> enter_run @0x90
  0x20c: halt 0
```

CAYMAN `Q7_POOL` was **also** `06 7f 00` (`j 0x200`) / `86 80 00` (`j 0x20c`). The Q7 compute
core's reset vector is **byte-identical** across CAYMAN and MARIANA — the +0x1c MARIANA shift
applies **only** to the NX SEQ core. Both trampolines still converge on `enter_run @0x90`.

**(C) `EXTISA_0..3` SO — real `EM_XTENSA` ELFs**, section geometry == CAYMAN; only `.text` grew
**+0x48** (`EXTISA_0` `.text` `0x6f1e`→`0x6f66`), absorbed within the same blob `0xa260` — that
`+0x48` is the new RNG-fork body. `kernel_info_table` `@0x02000380` size `0x88` (17 entries),
`.globstruct @0x02000408`, `.bss @0x02000450` — all == CAYMAN.

Disassembly proof (shipped `ncore2gp` objdump): `NX_POOL DEBUG IRAM` decodes 697 `entry` /
908 `retw`; `NX_POOL PERF IRAM` 140 `entry` / 213 `retw` / 285 distinct IVP ops; `Q7_POOL DEBUG
IRAM` 432 `entry` / 577 `retw` — both cores carry a full FLIX vector compute datapath (same
direction as CAYMAN's 150/430).

> **GOTCHA — the FLIX desync is a disassembler limit, not a finding.** The flat DEBUG IRAM carries
> no `.xt.prop` FLIX property table, so densely-scheduled vector bundles desync under the linear
> sweep. The `entry`/`retw` and reset-vector reads are robust; per-bundle micro-op recovery inside
> the RNG body is MED and so flagged.

---

## 4. The `NX_POOL` handler + opcode diff — STABLE (no growth)

MARIANA `NX_POOL` uses the **same** SEQ dispatch as CAYMAN — **`addi`-normalization** (the DVE
form), not the raw-compare chain of PE. Both DEBUG dispatch sites decode instruction-exact and
agree:

```text
SITE A @0x2d0f:  addi a2,a2,-65 ; movi a3,177 ; bgeu a3,a2,0x2d1b ; j 0x3075 (default) ;
                 const16 a3,8 ; const16 a3,0x800 ; addx4 a2,a2,a3 ; ...
SITE B @0x35a5:  addi a2,a2,-65 ; movi a3,177 ; bgeu a3,a2,0x35b1 ; j 0x3911 (default) ;
                 const16 a3,8 ; const16 a3,0xac8 ; addx4 a2,a2,a3 ; ...
CAYMAN  @0x2e5f:  addi a2,a2,-65 ; movi a3,177 ; bgeu a3,a2,0x2e6b ; j 0x3198 (default) ;
                 const16 a3,8 ; const16 a3,0x814 ; ...
```

- **Normalization base `0x41` (`'A'`) — UNCHANGED** (`addi a2,a2,-65` both gens). Unlike DVE
  (which shifted its base `0x41`→`0x30` this gen), POOL keeps `'A'`-based normalization; the
  opcode space did **not** extend downward.
- **Bound `movi a3,177` — UNCHANGED** → 177-entry table, indices `0..176`. **NO growth** —
  contrast PE (25→29) and DVE (opcode bound 170→187).
- **Table base relocated:** CAYMAN `DRAM file 0x814`; MARIANA SITE A `@0x800`, SITE B `@0xac8`
  (the DEBUG-segmented two-table layout). Default trampoline `0x3198`→`0x3075`. The dispatch log
  `"S: Dispatch opcode=0x%x"` moved `@0x80e38`→`@0x80e28` (−0x10).

**Real-vs-default count: 54 real / 123 default on BOTH gens** (MARIANA default `0x3075` ×123;
CAYMAN default `0x3198` ×123). Every real slot relocated, but the count and pattern are invariant
(the 178-bound is `0xf2 − 0x41 + 1`; 177 = the index count `0..176`, 54 = the non-default slots).

**The handler diff (the structural claim):**

> **Method (CAYMAN-page method, applied here).** Extract every single-token `"<glue>S: <OpName>"`
> from each DEBUG DRAM, **strip the string-pool glued-prefix byte(s)**, keep single-token names,
> `sort -u`, `comm`-diff. The glue-trap is present and was handled — observed glued prefixes:
> `"PS: EmbeddingUpdate"`, `"PS: EngineNop"`, `"RS: TensorScalarAddr"`,
> `"RS: TensorScalarAffineSelect"`, `"@S: RandGetState"`, `"@S: Rng"`, `"TS: GetSequenceBounds"`,
> `"TS: NonzeroWithCount"`, `"VS: TensorGather"`. A naive `^S:`-only diff is wrong; the strict
> glue-stripped diff gives a clean +0/−0.

**RESULT: MARIANA `NX_POOL` = 41 handlers; CAYMAN `NX_POOL` = 41 handlers; ADDED = 0; REMOVED = 0;
the 41-handler set is byte-for-name IDENTICAL.**

```text
AluOp BRANCH BranchPrefetchHint ConvLutLoad CrossLaneReduce EXT_BREAK EmbeddingUpdate EngineNop
Event_Semaphore ExtendedInst GetSequenceBounds Halt INS_BREAK INS_FL Iota LoadPoolArgument
MEMSET/RNG MOVE ModifyPoolConfig NOP NOTIFY POLL_SEM Pool RandGetState RandSetState Redirect
SB2SB_Collective SET_OM STRONG_ORDER Sort Tensor-Reduce Tensor-Scalar Tensor-Scalar-PTR
Tensor-Tensor TensorDequantize TensorGather TensorLoad TensorScalarAddr TensorScalarAffineSelect
TensorStore WRITE
```

= 18 shared-all-5 SEQ control core + 7 shared-with-DVE compute primitives + 16 POOL-only handlers
(incl. `ExtendedInst` = the `0xf0` bridge, and `RandGetState`/`RandSetState` = **the RNG handlers
the new TIE/LFSR kernel routes into**). See [CAYMAN × POOL §5](./cayman-pool.md) for the per-token
5-way roster derivation.

> **The key cross-gen finding for the NX core.** POOL's SEQ handler set is the **richest of the
> five engines and already shipped on CAYMAN with ALL** the general-compute + RNG + `ExtendedInst`
> handlers. There was nothing to add at the SEQ layer this gen — so unlike PE
> (+`PeManageSeed`/MX) and DVE (+`RandGet/SetState`/`Rand2`/`Sparsity`/`QuantizeMx`/`Exponential`),
> the `NX_POOL` handler set is **unchanged**. The v4 RNG expansion lands on the Q7 **compute** core
> (§5), reached via the **pre-existing** `RandGetState`/`RandSetState` handlers.

Both gens also carry the dual-mode SEQ feature (`"S: NX in HW Decode mode"` / `"S: NX in Sunda
mode: HW decode disabled"`), the ErrorHandler arms (`"S: ErrorHandler : Bad Opcode(0x%x)"`,
`cayman/seq/src/handlers/exception_handler.hpp`), and `"S: BEGIN on mariana"` (vs CAYMAN's
`"BEGIN on cayman"`). The new source header `addr_bits.hpp` appears on MARIANA `NX_POOL` (absent on
CAYMAN — the gen-wide MARIANA address-rerouting header also on ACT/DVE/PE); `translate_cayman+.hpp`
is on both gens. **No `mariana-4062` errata on either POOL core** (that patch is DVE-specific).

---

## 5. The `Q7_POOL` `kernel_info_table` diff + the MARIANA RNG arrival (the headline)

### 5a. The KIT key set — BYTE-FOR-KEY IDENTICAL across gens

Entry format (unchanged): 8-byte stride `{ u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4) }`;
native-LE `u32` key = `(opcode<<24)|(spec<<16)`. Entry counts identical: `EXTISA_0=17`,
`EXTISA_1=1`, `EXTISA_2=2`, `EXTISA_3=9` on both gens; the `(op,spec)` key set is **equal in every
EXTISA** (`added=[] removed=[]` for all four). The full MARIANA `EXTISA_0` table — funcVAs are
**pure monotonic relocation** (`+0x0` at low addresses growing to `+0x44` at high, the `+0x48`
`.text` growth distributed across the function layout); **no re-routing, no new entry**:

| idx | opcode | spec | funcVA (MAR) | Δ vs CAY | routing target |
| --- | --- | --- | --- | --- | --- |
| 0 | `0x7e` | 0 | `0x01000080` | `+0x0` | `pool_iota` |
| 1 | `0x7c` | 0 | `0x010003f8` | `+0x0` | `pool_cross_lane_reduce_arith` |
| 2 | `0x7d` | 0 | `0x01000410` | `+0x0` | `pool_cross_lane_reduce_bitvec` |
| 3 | `0x45` | 0 | `0x01000b90` | `+0x0` | `decode_pool` (Pool) |
| 4 | `0x51` | 0 | `0x01001068` | `+0xc` | (tensor primitive, `Q`) |
| 5 | `0x41` | 0 | `0x01000f1c` | `+0x0` | `decode_tensor_tensor_arith` |
| **6** | **`0xf0`** | **0** | `0x01003390` | `+0x20` | `ExtendedInst` spec0 (EngineNop) |
| **7** | **`0xf0`** | **1** | `0x010033a0` | `+0x20` | `pool_extended_inst_copy` |
| **8** | **`0xf0`** | **2** | `0x010034a4` | `+0x20` | `decode_extended_inst_tensor_tensor_arith` |
| **9** | **`0xf0`** | **4** | `0x010037d8` | `+0x30` | **Rand band → `decode_pool`** |
| **10** | **`0xf0`** | **3** | `0x01003a90` | `+0x30` | **Rand band → `decode_pool`** |
| 11 | `0x52` | 0 | `0x01003b80` | `+0x40` | (tensor primitive, `R`) |
| 12 | `0x46` | 0 | `0x01004100` | `+0x40` | `pool_copy` |
| 13 | `0x47` | 0 | `0x010041a0` | `+0x40` | (tensor primitive, `G`) |
| 14 | `0xbe` | 0 | `0x01004244` | `+0x40` | (tensor primitive) |
| 15 | `0xf2` | 0 | `0x01004890` | `+0x44` | `get_sequence_bounds` / `nonzero_with_count` |
| 16 | `0x7b` | 0 | `0x01004e04` | `+0x40` | `decode_tensor_dequantize` |

`EXTISA_3` (the cptc/MX family, 9 entries): keys == CAYMAN, funcVAs moved; idx7 `0xe4`/0
`@0x01002260` (the cptc dispatcher), idx8 `0xf0`/spec7 `@0x01003b74` (cptc extended path). The
`cptc_decode_impl<1..6>` DTYPE-selected family lives here, byte-for-name identical on both gens.
`EXTISA_1` = 1 entry (`0x7e` iota); `EXTISA_2` = 2 entries (`0x7c`, `0x7d`).

### 5b. The kernel-body diff — the RNG (the SOLE substantive Q7 change)

`Q7_POOL DEBUG DRAM` `'P%i:'` broad-token set-diff (CAYMAN vs MARIANA):

```text
ADDED on MARIANA  : Xorwow(TIE) (Init), XorwowRng(TIE), XorwowGetSeeds(TIE),
                    XorwowSetSeeds(TIE), LfsrGetSeeds, LfsrSetSeeds
REMOVED on MARIANA: Xorwow(SW) (Init), XorwowRng(SW), XorwowGetSeeds(SW), XorwowSetSeeds(SW)
```

Both gens carry `"P%i: RandGetState : num_chans = %0d : rand_algo = 0x%x"` and the matching
`RandSetState`; `"P%i: Decode : ExtendedInstRandGetState"` / `"…RandSetState"`;
`"P%i: ExtendedInstRand{Get,Set}State : num_tensor_elements = %d"`. The `'Decode :'`
kernel-dispatch list (8 names: `ExtendedInstCopy`, `ExtendedInstCptcDecode`,
`ExtendedInstRandGetState`, `ExtendedInstRandSetState`, `ExtendedInstTensorTensorArith`,
`GetSequenceBounds`, `SB2SB_Collective`, `Sbuf2Sbuf`) is **identical** — `ADDED=0, REMOVED=0` at
the dispatch-name layer. **RNG is the only substantive Q7 change.**

> **The arrival, reconciled.** On CAYMAN POOL the only RNG is the Marsaglia Xorwow **software**
> path (`Xorwow(SW)`). On MARIANA POOL that same Xorwow became the **`(TIE)` build variant** AND a
> **second algorithm — LFSR — was added** (`LfsrGetSeeds`/`LfsrSetSeeds`). Both are reached through
> the **same** `RandGetState`/`RandSetState` handlers (NX SEQ side) and the **same** `0xf0`-spec3/4
> `kernel_info_table` rows (Q7 side) — the algorithm is selected by the `rand_algo` fork **inside**
> the kernel body, **not** by a new opcode or a new table entry. This is exactly why the KIT key
> set is unchanged (5a) yet the Q7 IRAM grew **+0x300** (DEBUG): the new LFSR-fork + TIE-variant
> body was compiled into the **existing** `RandGetState`/`RandSetState` kernel.
> `[HIGH/OBSERVED for the string boundary]`

> **CORRECTION — the additions are an internal fork, NOT new table rows.** A first-pass hypothesis
> expected the RNG arrival to show up as **new `opcode→funcVA` rows**. The observed reality is the
> opposite: the KIT `(op,spec)` key set is byte-for-key identical across gens; the additions live
> **inside** the existing kernel body behind the `rand_algo` selector. The image-level boundary is
> therefore "new kernel **body**", not "new dispatch row".

### 5c. The `rand_algo` fork, as annotated C (the NEW RNG dispatch)

The fork the new MARIANA kernel body adds is reconstructed in
[RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md): the full ISA enum is
`NEURON_ISA_TPB_RAND_ALGORITHM { LFSR=0, PCG32=1, PHILOX=2, XORWOW=3 }`; POOL wires exactly **two**
of the four (`LFSR(0)` and `XORWOW(3)`); `PCG32(1)`/`PHILOX(2)` hit the SEQ
`"rand_algorithm(0x%x) not currently supported on POOL"` arm. The fork is a single `bbci` in the
**shared** `SetSeeds` body, driven by a `saltu(rand_algo, 1)` comparison; there is **no separate
`LfsrRng` function and no LFSR opcode** — the per-draw advance is **shared** with the Xorwow(TIE)
driver `@0xbc78`; only the seed init differs (`LfsrSetSeeds @0xb700` vs `XorwowSetSeeds(TIE)
@0xb744`). The C below models that fork — **this is the new logic the MARIANA Q7 body contains**;
the surrounding `RandGetState`/`RandSetState` decode path is unchanged from CAYMAN:

```c
/* MARIANA Q7_POOL — the NEW rand_algo fork inside the RandSetState kernel body.
 * Reached via the UNCHANGED 0xf0-spec3/4 kernel_info_table rows (§5a) and the
 * pre-existing RandSetState handler (§4). The fork is the v4 RNG arrival: a SECOND
 * algorithm (LFSR) and the (SW)->(TIE) Xorwow build variant. ISA enum:
 *   NEURON_ISA_TPB_RAND_ALGORITHM { LFSR=0, PCG32=1, PHILOX=2, XORWOW=3 }.       */

typedef enum { RAND_LFSR = 0, RAND_PCG32 = 1, RAND_PHILOX = 2, RAND_XORWOW = 3 } rand_algo_t;

void pool_rand_set_seeds(rand_algo_t rand_algo, const rng_seed_t *seed, rng_state_t *st) {
    LOG("P%i: RandSetState : num_chans = %0d : rand_algo = 0x%x", cpu_id, num_chans, rand_algo);

    /* POOL wires ONLY LFSR(0) and XORWOW(3). PCG32/PHILOX are ISA-defined but rejected
     * at the SEQ front-end ("rand_algorithm(0x%x) not currently supported on POOL").    */
    if (rand_algo != RAND_LFSR && rand_algo != RAND_XORWOW)
        return reject_unsupported_algo(rand_algo);          /* SEQ-side arm */

    /* The fork: a single bbci on the rand_algo bit (saltu(rand_algo,1) resolves the
     * 4-value enum down to the two POOL wires). NEW on MARIANA; absent on CAYMAN.        */
    if (rand_algo == RAND_LFSR) {
        /* NEW second algorithm. One u32 word per lane (vs Xorwow's 5-6 words):
         * no memset, no multi-word chain, no Weyl const — frame 192, one FLIX bundle.   */
        lfsr_set_seeds(seed, st);                            /* @0xb700 — NEW on MARIANA */
    } else { /* RAND_XORWOW */
        /* The (SW)->(TIE) build variant: same 5 Marsaglia seeds, byte-identical Weyl
         * const, same DRAM-scratch state model — recompiled under the (TIE) label.       */
        xorwow_set_seeds_tie(seed, st);                      /* @0xb744 — (TIE) on MARIANA */
    }
    /* Per-draw advance is SHARED by both algorithms (only seed init differs).            */
    /* ... advance driver @0xbc78 (XorwowRng(TIE) body) ...                               */
}
```

> **NOTE — `(TIE)` is a build label, not a HW instruction.** Per
> [RNG — Xorwow TIE Path](../firmware/kernels/rng-xorwow-tie.md), the shipped `ncore2gp` ISA decode
> tables contain **zero** rng/xorwow/lfsr opcode and **zero** RNG state register; the `(TIE)`
> suffix is a generation/build variant label on the *same* software Xorwow kernel. This page
> establishes only the firmware-image **presence** boundary: `Xorwow(TIE)` + `LfsrGet/SetSeeds`
> **first appear** on the MARIANA Q7_POOL image. The internal fork register is MED, cited from the
> LFSR-dispatch page. `[presence HIGH/OBSERVED; exact fork slot MED]`

The seed-state opcodes themselves are gen-stable: `0x77 RAND_GET_STATE` / `0x78 RAND_SET_STATE` are
flagged maintained (`// Y`) in **every** gen's `aws_neuron_isa_tpb_common.h` (mariana L215/216 vs
cayman L210/211), each a 64-byte operand struct (`D4_RAND` get / `S1_RAND` set, `dtype == UINT32`)
— see [RNG Seed-State Ops](../firmware/kernels/rng-seed-state-ops.md). The opcodes did not change;
the **kernel they advance** gained the second algorithm. `[HIGH/CARRIED from the RNG pages]`

The Q7 dispatcher infra is invariant (`"P%i: Entering/Exiting Dispatch"`,
`"P%i: In dispatch, CPU ID…got opcode 0x%x"`, `"P%i: UNKNOWN OPCODE=0x%x"` /
`"…UNKNOWN EXTENDED OPCODE=%d"`, source `dispatch.hpp`): the linear scan by packed `(spec,opcode)`
key, partitioning by `get_cpu_id()` across pool channels — see
[CAYMAN × POOL §4b](./cayman-pool.md).

---

## 6. The `0xF0` ExtendedInst bridge — UNCHANGED + the dtype/MX footprint

**The `0xF0` bridge registers across both cores on MARIANA, unchanged.**

- **SEQ side (`NX_POOL`):** index `0xf0 − 0x41 = 0xaf = 175`; slot `@table + 175·4` reads
  `0x306d` (MARIANA) / `0x3190` (CAYMAN) — **both REAL** handlers, distinct from the default
  trampoline (MAR `0x3075`, CAY `0x3198`). `"S: ExtendedInst"` present in `NX_POOL` DRAM both gens
  (`@DRAM file 0x26e5` MARIANA) — **POOL-exclusive** (the only engine with the bridge).
- **Q7 side (`EXTISA_0`):** the five `0xf0+spec` rows (specs `0,1,2,4,3`) present **byte-for-key on
  both gens** (idx 6–10; funcVA relocated `+0x20`/`+0x30`). The two-level `(opcode<<24)|(spec<<16)`
  escape is **structurally invariant**; the spec byte sub-selects exactly one of the five rows;
  there is no third dispatch level.

POOL remains the **only** engine with **both** the SEQ `0xf0` bridge **and** a Q7 compute core —
which is exactly why only POOL has the dual-dispatch. The `0xF0` reconciliation is identical to
CAYMAN; see [CAYMAN × POOL §4c](./cayman-pool.md) and
[POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md).

**dtype / MX footprint:**

- **`NX_POOL`:** `FP4`/`CPTC`/`MXTENSOR`/`SFP8`/`fp8_e` do **not** appear as named strings
  (`grep = 0`). The only dtype constants are `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}`
  (`move.cpp` assertion) — byte-identical to CAYMAN. New dtype codes are numeric in the decode
  path, not named strings (same negative as ACT/DVE/PE).
- **`Q7_POOL`:** the MX dequant footprint **is** present (`proc_4bit_mx_8`, `proc_4bit_non_mx`,
  `proc_6bit_non_mx`, `"Unimplemented dequant format"`,
  `"unsupported in_dtype/out_dtype for cptc_decode"`, `cptc_decode_impl<1..6>`) — but it is
  **byte-for-name IDENTICAL on both gens**. The MX/CPTC dequant path is **pre-existing on CAYMAN
  POOL, not a MARIANA addition**. POOL has **no** `QuantizeMx` handler (that is a DVE handler,
  gained by DVE this gen); POOL's MX lives in `TensorDequantize`/cptc, present since CAYMAN. So the
  MARIANA FP4/MX expansion leaves **no new POOL firmware footprint**.

---

## 7. DKL, PROF, and the dtype/size deltas

**DKL — structurally invariant.** MARIANA DKL DEBUG DRAM carries the same symbols as CAYMAN:
`dynamic_kernel_dispatch`, `dynamic_extended_op_kernel_dispatch`, `.kernel_info_table`,
`dispatch_wrapper.hpp`, `"P%i: Corrupted prelink library; NULL start symbol"`, and
`"P%i: CustomOps not supported on Cayman"`. The DKL build also carries the MARIANA TIE+LFSR RNG
(`LfsrGet/SetSeeds`, `Xorwow(TIE)`). `DKL_PERF == DKL_TEST` byte-identical (§2b). See
[External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md).

> **QUIRK — `"CustomOps not supported on Cayman"` survived into MARIANA.** The CAYMAN-named source
> string was **not** updated for MARIANA — a build-string artifact; the dynamic custom-op path
> remains gated off.

**PROF — re-preallocated + DISARMED + now per-engine.** On CAYMAN all four NX engines shared **one**
47-record CAM (`8fd7e422`). On MARIANA POOL the CAM is **per-engine and essentially zeroed**:

| Resource | CAYMAN POOL | MARIANA POOL | cross-engine (MARIANA) |
| --- | --- | --- | --- |
| `PROF_CAM` sha | `8fd7e422` (shared 4/4) | `0951b326` (**disarmed**) | ≠ PE `43475cec` ≠ DVE `ca588683` ≠ ACT `326bc0dd` |
| `PROF_CAM` armed opcode-capture records | 46 real (`enable==1`, `mask=0xff`) + 1 null sentinel | **0** real (only the 1 null sentinel survives) | PE re-armed 22 |
| `PROF_TABLE` sha | `ce761f81` (`0x0201` hdr + 150 nz words) | `534f2239` (**zeroed hdr + 1 nz word**) | per-engine |

The CAYMAN shared-CAM property **did not survive the generation**: PROF is now per-engine, and POOL
specifically got a **disarmed** CAM (contrast MARIANA PE's re-armed 22 PE-specific records).
`Q7_POOL` ships **no** PROF. (PROF_TABLE field schema not decoded — MED.) See
[PROF CAM/TABLE Formats](./prof-cam-table-formats.md).

> **CORRECTION — "disarmed" is precise, "0 records" is not.** CAYMAN POOL's shared CAM holds **46**
> real opcode-capture records (`enable==1`, `mask=0xff`) plus one trailing null-opcode sentinel
> (`enable==1`, `opcode=0`, `mask=0`) → a strict `enable==1` count of 47. MARIANA POOL's CAM keeps
> **only that same null sentinel** (1 strict `enable==1`, `mask=0`) and **zero real opcode-capture
> records** (`mask≠0`). So the literal `enable==1` count is **1, not 0** — the CAM is "disarmed"
> because **no real capture record is armed**, not because the byte is wholly zero.

**Size / sha deltas — 20/20 distinct (full recompile).** No MARIANA POOL image is byte-identical to
its CAYMAN counterpart. The directional split is the signature of the diff: **NX_POOL IRAM SHRANK**
(the tighter v4 compile), **Q7_POOL IRAM GREW** (the new TIE/LFSR RNG body):

| IMAGE | CAY-sz / sha | MAR-sz / sha | ΔSize | cause |
| --- | --- | --- | --- | --- |
| NX DEBUG_IRAM | `0x1c820` / `8e4412b9` | `0x1c080` / `41b6c798` | **−0x7a0** | tighter v4 compile |
| NX PERF_IRAM | `0x17280` / `9049bf8c` | `0x14520` / `22991181` | **−0x2d60** | tighter v4 compile |
| NX TEST_IRAM | `0x16a00` / `57b52caa` | `0x14240` / `a505cbaa` | **−0x27c0** | tighter v4 compile |
| NX DEBUG_DRAM | `0x6f20` / `7bdf6ed7` | `0x7000` / `ec067304` | +0xe0 | recompile |
| Q7 DEBUG_IRAM | `0x1ea40` / `513a8a22` | `0x1ed40` / `47f76629` | **+0x300** | **the TIE/LFSR RNG body** |
| Q7 PERF_IRAM | `0x16360` / `b06ceff1` | `0x164e0` / `0c761dba` | **+0x180** | the RNG body |
| Q7 TEST_IRAM | `0x17d60` / `93cfcaaf` | `0x17e80` / `e8a32b3f` | **+0x120** | the RNG body |
| Q7 DEBUG_DRAM | `0x15d00` / `226f4254` | `0x15d80` / `02cacff0` | +0x80 | recompile |
| DKL_DEBUG_IRAM | `0x13fc0` / `7e5e39ac` | `0x14140` / `22790dbe` | +0x180 | the RNG body |
| EXTISA_0_SO | `0xa260` / `910d41c3` | `0xa260` / `9f2ce049` | +0x0 | `.text +0x48` inside |
| EXTISA_3_SO | `0x6974` / `052ac31c` | `0x6974` / `8477ff26` | +0x0 | inside |
| PROF_CAM | `0x400` / `8fd7e422` | `0x400` / `0951b326` | +0x0 | disarmed |

The DEBUG/PERF/TEST observability split is invariant (only DEBUG carries the runtime logs: NX 177
`S:` / Q7 158 `P%i:`; PERF/TEST strip them); the dispatch mechanism is unchanged across builds.

---

## 8. Adversarial self-verify — the 5 strongest claims

1. **The RNG arrival (the headline).** *Challenge:* is `Xorwow(TIE)`/`LFSR` really NEW vs CAYMAN,
   not a string already present on CAYMAN under a different glue? *Evidence:* `Q7_POOL DEBUG DRAM`
   `'P%i:'` set-diff (broad token, both carves) — MARIANA ADDS `Xorwow(TIE)`,
   `XorwowRng(TIE)`, `Xorwow{Get,Set}Seeds(TIE)`, `LfsrGetSeeds`, `LfsrSetSeeds`; REMOVES the four
   `(SW)` variants. CAYMAN `Q7_POOL` carries `Xorwow(SW)` only and **zero** `Lfsr`/`(TIE)` tokens.
   Corroborated structurally by Q7 IRAM growth **+0x300** (DEBUG) and the EXTISA_0 `.text` **+0x48**
   — the new fork body has a byte cost. **PASS.**
2. **The +0x1c reset shift.** *Challenge:* is `06 7d` vs `06 76` a real `+0x1c` boot-target shift,
   not a coincidental opcode byte? *Evidence:* `j 0x1f8` (MARIANA) − `j 0x1dc` (CAYMAN) = `0x1c`;
   secondary `j 0x204` − `j 0x1e8` = `0x1c`; both still land `enter_run @0x90`; the +0x1c matches
   the MARIANA ACT/DVE/PE carves. **PASS.**
3. **Per-core handler/table deltas.** *Challenge:* is the NX 41==41 a glue-trap artifact, and is the
   Q7 key set really unchanged? *Evidence:* the glue-stripped diff (10 documented glued prefixes
   `PS:`/`RS:`/`@S:`/`TS:`/`VS:`) gives +0/−0; the KIT `(op,spec)` key set is `added=[] removed=[]`
   for all four EXTISA, funcVAs pure monotonic relocation `+0x0..+0x44`. **PASS.**
4. **The `0xF0` bridge across both cores.** *Challenge:* does the SEQ `0xf0` slot still point to a
   REAL handler (not the default), and do the five Q7 rows survive? *Evidence:* SEQ slot
   `0x306d ≠ default 0x3075`; `"S: ExtendedInst"` present; Q7 five `0xf0+spec{0,1,2,4,3}` rows
   byte-for-key. The two-level escape is structurally invariant. **PASS.**
5. **The size/sha deltas.** *Challenge:* is the diff a recompile or a patch — and is the baseline
   authentic? *Evidence:* 20/20 images distinct from CAYMAN; the CAYMAN baseline carves and
   8/8 anchor sha256 MATCH the committed page; the directional split (NX shrank / Q7 grew) is
   internally consistent with the RNG-body cause. **PASS.**

---

## 9. Honesty ledger

**HIGH / OBSERVED:**

- 46 getters parsed instruction-exact (14 NX + 32 Q7; 28 real + 18 cursors); 12/12 spot-reconciled
  to `libnrtucode.a` member `.rodata`; CAYMAN baseline 8/8 anchors MATCH the committed page.
- Both reset vectors: NX `06 7d 00` (`j 0x1f8`, +0x1c) / Q7 `06 7f 00` (`j 0x200`, unchanged); both
  → `enter_run @0x90`. EXTISA `.text +0x48`.
- NX dispatch: `addi a2,a2,-65` (base `0x41`, no shift) + `movi a3,177` (no growth) both sites;
  54 real / 123 default both gens; default tramp `0x3198`→`0x3075`; table base `0x814`→`0x800/0xac8`.
- NX handler diff 41==41 (+0/−0, glue-trap documented); Q7 KIT key set byte-for-key identical
  (17/1/2/9; funcVA pure relocation); `0xf0` bridge intact (SEQ slot real both gens; Q7 five rows).
- Q7 RNG diff: ADDED `Xorwow(TIE)`+`LfsrGet/SetSeeds`, REMOVED `Xorwow(SW)`; `rand_algo` selector
  both gens; the SOLE substantive Q7 change.
- MX/CPTC footprint identical both gens (pre-existing on CAYMAN); NX dtype only UINT32/INT32/FP32.
- PROF disarmed + per-engine (`8fd7e422`→`0951b326`, 0 armed); DKL invariant, `DKL_PERF==DKL_TEST`.
- Size 20/20 distinct (NX IRAM shrank, Q7 IRAM grew).

**MED / INFERRED:**

- The exact internal `rand_algo` fork register/slot selecting LFSR vs Xorwow (the fork EXISTS —
  `rand_algo` string both gens; the additions are internal, not new rows). Direction HIGH; exact
  slot CARRIED from [RNG — LFSR Dispatch](../firmware/kernels/rng-lfsr-dispatch.md).
- The `0xf0` specs 3/4 → {RandGetState, RandSetState} binding (specs 0/1/2 HIGH; 3/4 route via
  shared `decode_pool`, MED).
- PROF "disarmed" = 0 `enable==1` records (record-shape INFERRED-HIGH); PROF_TABLE field schema.
- `engine_idx = 2` computed at boot (from the boot-identity string + corpus CSR enum POOL=2).

**LOW / NOT CLAIMED:** which silicon/runtime selects MARIANA vs MARIANA_PLUS (reported
byte-identical POOL ucode — a gen-label distinction; not carved here, see
[MARIANA+ × POOL](./mariana-plus-pool.md)); DEBUG/PERF/TEST/DKL selection; the exact per-kernel
operand layout of the new TIE/LFSR RNG body (per-kernel scope; the RNG pages cover the algorithm).

---

## 10. Cross-references

- [CAYMAN × POOL](./cayman-pool.md) — **the diff baseline** (the dual-core model, the 178-slot SEQ
  hub, the 41-handler derivation, the KIT format, the `0xF0` reconciliation).
- [MARIANA+ × POOL](./mariana-plus-pool.md) — the v4+ POOL image (reported byte-identical ucode).
- [Image Catalog Index](./image-catalog-index.md) — the getter map; the MARIANA POOL rows 327–384.
- [RNG — Xorwow TIE Path](../firmware/kernels/rng-xorwow-tie.md) — the `(TIE)` build variant (the
  headline RNG arrival).
- [RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md) — the new second
  algorithm + the `rand_algo` fork (`{LFSR=0, PCG32=1, PHILOX=2, XORWOW=3}`).
- [RNG Seed-State Ops](../firmware/kernels/rng-seed-state-ops.md) — `0x77`/`0x78` get/set state.
- [kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md) — the 8-byte record /
  packed-key format.
- [POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the spec sub-dispatch.
- [External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md) — the DKL layer.
- [PROF CAM/TABLE Formats](./prof-cam-table-formats.md) — the now-per-engine profiling resource.
- Sibling MARIANA engines: [× ACT](./mariana-act.md), [× DVE](./mariana-dve.md), [× PE](./mariana-pe.md).
