# MAVERICK × DVE image (absorbs ACT)

This page is the **cross-generation diff** of the **MAVERICK × DVE** firmware image
(`NX_DVE`, `engine_idx=3`) against the committed
[MARIANA_PLUS × DVE](mariana-plus-dve.md) baseline. It is the **pivotal Maverick carve**:
DVE is the **head engine of the v5 NX block** — the engine that *absorbed* the amputated ACT.
It does **not** re-derive the DVE engine model (the SEQ dispatch machinery, the dual
HW-Decode/Sunda tables, the `engine_idx=3` boot); read [mariana-plus-dve.md](mariana-plus-dve.md)
(and, behind it, [mariana-dve.md](mariana-dve.md)) for that. Everything below is the *delta* —
and unlike the v4→v4+ step, the delta is **real and structural**.

> **THE HEADLINE — the ACT→DVE fold is REAL but it is NOT a handler merge.** The five
> ACT-specific dispatch handlers (`Activate`/`ActivateQuantize`/`ActivationTableLoad`/
> `ActivationReadAccumulator`/`Activate2`) are **ABSENT firmware-wide** on MAVERICK — **0 each**,
> region-wide over the entire `0x871300..EOF` MAVERICK block **and** 0 each in the DVE DEBUG DRAM
> specifically. The MAVERICK DVE handler set is **DVE-native** — **59 strict**, and the *only*
> strict difference vs MARIANA_PLUS DVE's **60** is `QuantizeMx` — the **`QuantizeMx` NAMED HANDLER is
> DROPPED** on MAVERICK (the 60→59 delta is a removal, **not** a migration: `QuantizeMx` has 0 string
> hits in the entire `0x871300+` MAVERICK region). The `0xe3 QUANTIZE_MX` **opcode stays DVE-bound**
> (armed only in the MAVERICK DVE PROF CAM `dbff2b84`); POOL's MX surface is the pre-existing
> **`0x7b TENSOR_DEQUANTIZE`** (EXTISA_0 idx16, funcVA `0x50ec`), **not** `0xe3`. The fold's
> **image-level footprint** is three concrete, byte-exact things on the DVE
> side: **(1)** the DVE PROF CAM newly **arms ACT opcodes `0x23` ACTIVATION_TABLE_LOAD + `0x25`
> ACTIVATE2** (absent from the MARIANA/MARIANA_PLUS DVE PROF); **(2)** the ACT read-accumulator
> survives **renamed** as the DVE-native `DveReadAccumulator` (the ACT op `0x24`, re-expressed
> `0x9b`-class); **(3)** the maverick address-map folds `ACT_CONTROL_TABLE` **under the DVE MMIO
> block** with no standalone ACT engine instance. So the fold is real at the **profiling/scheduling**
> level and the **datapath-rename** level — **not** as a merged ACT handler image. `[HIGH/OBSERVED —
> the absence, the PROF arming, the rename, and the map fold are all byte/string verified this
> session; the "fold" *causal* reading INFERRED-HIGH]`

> **WALL — MAVERICK (v5) is HEADER-OBSERVED only.** The carved `S:` rosters, the PROF CAM/TABLE
> bytes, the reset/boot trampoline (decoded with `ncore2gp`), the getter immediates, the carve
> sha256s, the shipped ISA-enum + opcode-deprecation comments, and the address-map fold are all
> **OBSERVED** this session. But **every claim about the v5 image *interior*** — the per-opcode→
> handler-body binding, the precise semantics of the migrated handlers, the runtime selection of
> v5 — is **INFERRED** and flagged inline. `v5 Q7_CC_TOP` is **FILE-ABSENT**; `arch_id 36` is
> **INFERRED** (never binary-observed). Treat the rosters/PROF/reset as ground truth and the
> interior reasoning as inference.

> **NOTE — provenance.** Every fact derives **solely** from static analysis of the shipped binaries
> with stock binutils (`nm`/`objdump`/`readelf`/`ar`/`dd`/`xxd`/`strings`/`sha256sum` + `python
> struct`) and the Cadence Xtensa toolchain (`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`, GNU
> Binutils 2.34.20200201 / Xtensa Tools 14.09). Handler naming derives from the DEBUG build's own
> embedded `S:` format strings; opcode naming from the shipped `neuron_maverick_arch_isa` /
> `neuron_mariana_arch_isa` OPCODE + NEURON_ENGINE enums. Lawful interoperability reverse
> engineering (DMCA 17 U.S.C. 1201(f)). Confidence/evidence tags follow the project
> [Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
> **OBSERVED/INFERRED/CARRIED**.

---

## 1. The delta at a glance — MARIANA_PLUS DVE → MAVERICK DVE

The full v4+→v5 step, leading with the fold thesis. Δ marks: **`F`** the fold footprint,
**`+`** genuinely added, **`−`** removed, **`R`** re-spec'd/re-authored, **`=`** invariant.

| property | MARIANA_PLUS DVE (baseline) | MAVERICK DVE (this page) | Δ |
|---|---|---|:---:|
| **5 ACT handlers** (`Activate`/`ActivateQuantize`/`ActivationTableLoad`/`ActivationReadAccumulator`/`Activate2`) | N/A — ACT is a **separate** image | **ABSENT firmware-wide — 0 each** (region-wide + DVE DEBUG DRAM) | **F** |
| **DVE PROF CAM arms `0x23`/`0x25`** (ACT opcodes) | **NOT armed** (`0x23`,`0x25` ∉ CAM) | **ARMED** — `{op=0x23,mask=0xff,en=1}`, `{op=0x25,mask=0xff,en=1}` | **F** |
| **ACT read-accumulator** | ACT-side handler `ActivationReadAccumulator` (op `0x24`) | re-expressed DVE-native **`DveReadAccumulator`** | **F** |
| **`ACT_CONTROL_TABLE`** (address-map) | standalone ACT engine instances | folded **under `…DVE_0_0_ACT_CONTROL_TABLE`**; **0** `amzn_tpb ACT_` MMIO instances | **F** |
| dispatch handler set (strict `S:` roster) | **60** | **59** | **−** (`−QuantizeMx`) |
| `QuantizeMx` *named handler* | present | **DROPPED** (0 hits region-wide; **not** migrated — `0xe3` stays DVE-bound, POOL's MX surface is `0x7b`) | **−** |
| PROF CAM armed-opcode set | `ca588683`, **47** armed | **`dbff2b84`, 53 armed** — `+10 / −3` | **R** |
| PROF\_TABLE | `d72b339f` (== MARIANA verbatim) | **`f349e417`** — re-authored | **R** |
| reset vector | `j 0x1f8` (`06 7d`, the `+0x1c` MARIANA shift) | **`j 0x1d8` (`06 75`)** — a new **−0x20** v5 shift | **R** |
| 2nd vector / `enter_run` / disp base | `j 0x204` / `@0x90` / `0x1250` | **`j 0x1e4` / `@0x94` / `0x1490`** | **R** |
| HW-Decode table base / default / real | `0x80800` / `0x31e3` / 82 real | **`0x80820` / `0x2f90` / 79 real** | **R** |
| opcode space (dispatch width) | 187 (`movi a3,186`, `addi −48`) | **187 (`movi a3,186`, `addi −48`)** — SAME | **=** |
| OPCODE *enum* cardinality (ISA) | 159 | **165** (`+6` new names) | **+** |
| DGE reshape fast-path | present (4 strings, dual-build) | **dropped — 0** (re-architected to HW DMA) | **−** |
| dtype named strings in NX | `UINT32/INT32/FP32` (+`QuantizeMx`) | `UINT32/INT32/FP32` only (no FP8/INT4/SFP8/MX) | **=** / **−** |
| `mariana-4062` DVE errata | present (RETAINED on v4+) | **dropped — 0** | **−** |
| engine self-name | `S: BEGIN on mariana_plus` | `S: BEGIN on maverick` | **R** |
| IRAM/DRAM size (every variant) | larger | **SMALLER** (`−0x4760..−0x8280`) | **−** |
| `.a` byte-reconcile | 8/8 `.so`==`.a` | **0 MAVERICK members in `.a`** — single-source | **R** |
| DRAM magic / SEQ model / `engine_idx=3` | `0x6099cb34`, runtime-computed | identical | **=** |

`[HIGH/OBSERVED — every Δ row re-verified this session; MAVERICK column carved fresh from
`libnrtucode_internal.so` (8/8 shas), MARIANA_PLUS column re-carved (8/8 anchors MATCH); per-row
anchors in §2–§7]`

> **NOTE — what a MARIANA_PLUS→MAVERICK DVE swap actually is.** This is the **opposite** of the
> v4→v4+ step. v4+ was a *pure recompile + DGE fast-path* (no functional dispatch change, PROF
> byte-identical). v5 is a **genuinely new generation**: an amputated ACT engine folded onto DVE
> (PROF-armed, not handler-merged), a new `−0x20` reset geometry, a re-authored PROF (CAM **and**
> TABLE), the DGE fast-path *dropped*, the `QuantizeMx` named handler *dropped* (the MX dtype
> *dequant* machinery lives in the Q7 POOL via `0x7b`, but `0xe3` itself stays DVE-bound), the DVE
> errata *dropped*, **smaller** in every variant, internal-twin-only, an independent build (6.1%
> 16-byte block similarity). The dispatch *mechanism* (187-entry `addi −48` table) survives; almost
> everything else moved. `[HIGH/OBSERVED]`

---

## 2. Carve provenance — single-source, 8/8 sha MATCH

MAVERICK is carried **only** by `libnrtucode_internal.so`
(`sha256 b7c67e89…632fc329b`, re-hashed this session = MATCH; ELF64 x86-64 DYN, not stripped,
identity-mapped — `.rodata` VA `0x46b0` == file offset `0x46b0`, so a blob's `.data` VA == its file
offset). `nm` lists exactly **14** `MAVERICK_NX_DVE_*_get` accessors (8 real + 6 zero-size SRAM/
EXTRAM boundary cursors); each is the canonical 4-instruction stub
`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`, disassembled 14/14:

```asm
9b55a0 <MAVERICK_NX_DVE_PERF_IRAM_get>:
  lea  -0x1442a7(%rip),%rax   # 0x871300  MAVERICK_NX_DVE_PERF_IRAM_get.data
  movq $0xec00,(%rsi)         ; size
9b5ca0 <MAVERICK_NX_DVE_PROF_CAM_get>:
  lea  -0x11a07(%rip),%rax    # 0x9a42a0  MAVERICK_NX_DVE_PROF_CAM_get.data
  movq $0x400,(%rsi)          ; size
```

`[HIGH/OBSERVED — 14/14 getters `nm`-resolved at `.text 0x9b55a0..0x9b5cc0`; img-ptr/size immediates
decoded; identity-map `.rodata` VA==fileoff confirmed via `readelf -SW`]`

The **8 real carves**, sliced at the getter `(img-ptr,size)` and sha256'd this session — **all 8
MATCH** the SX-IMG-18 anchors byte-for-byte:

| IMAGE | off | size | sha256[:16] |
|---|---|---:|---|
| PERF\_IRAM | `0x871300` | `0xec00` | `8da04026b247c478` |
| PERF\_DRAM | `0x87ff00` | `0x2740` | `bb3988910b45bebd` |
| TEST\_IRAM | `0x882640` | `0xf5c0` | `0254f7506f014cde` |
| TEST\_DRAM | `0x891c00` | `0x29c0` | `1b24f954c12acb7b` |
| DEBUG\_IRAM | `0x8945c0` | `0x19000` | `ffaefd3cb91b5d1b` |
| DEBUG\_DRAM | `0x8ad5c0` | `0x5f80` | `ed5f22e3a3787952` |
| PROF\_CAM | `0x9a42a0` | `0x400` | `dbff2b840c3f5082` |
| PROF\_TABLE | `0x9a46a0` | `0x2000` | `f349e417293e7b47` |

`[HIGH/OBSERVED — carved + sha256'd this session]`

> **GOTCHA — no `.a` byte-reconcile exists for MAVERICK.** `libnrtucode.a` (`sha 158dadc5`) carries
> **0 MAVERICK members** (**435** total = **420 image members** [CAYMAN 124 / MARIANA 124 /
> MARIANA_PLUS 124 / SUNDA 48 / MAVERICK 0] **+ 15 framework `.c.o`** objects, verified by `ar t`).
> Unlike the MARIANA_PLUS DVE carve (which reconciles 8/8 `.so`==`.a`), MAVERICK
> is **internal.so-EXCLUSIVE** — cross-validation is by the getter `(img-ptr,size)` parse + the sha256
> match to SX-IMG-18, **not** by an `.a` member. The shipped `.a` topping out at MARIANA_PLUS is
> itself a gen-step signature. `[HIGH/OBSERVED — `nm -D` MAVERICK = 0, `ar t` MAVERICK = 0]`

### 2a. DVE is the HEAD of the MAVERICK NX block (ACT amputated)

With ACT folded out, **DVE is the family head**. The lowest MAVERICK `.rodata` blob is
`MAVERICK_NX_DVE_PERF_IRAM @0x871300`; the 6 zero-size DVE cursors resolve to TEST_IRAM /
DEBUG_IRAM / `MAVERICK_NX_PE_PERF_IRAM` — engine order **DVE → PE → POOL**, *no ACT image preceding*.
The **gen boundary is byte-exact**: `MARIANA_PLUS_NX_POOL_PROF_TABLE @0x86f300` + `0x2000` =
`0x871300` == `MAVERICK_NX_DVE_PERF_IRAM @0x871300`. The two gens are contiguous; MAVERICK begins
exactly where MARIANA_PLUS ends, headed by DVE. `[HIGH/OBSERVED — nm `.data` sort + getter lea
arithmetic; DVE→PE adjacency from the cursor resolution]`

> **CORRECTION (vs the v4/v4+ ordering).** On MARIANA and MARIANA_PLUS the DVE image is placed
> `ACT → DVE → PE → POOL` (DVE second). On MAVERICK the order is `DVE → PE → POOL` (DVE **first**) —
> there is no ACT image to precede it. A reimplementer enumerating the v5 catalog must **not** expect
> an ACT blob ahead of DVE. `[HIGH/OBSERVED]`

---

## 3. The fold footprint #1 — the DVE PROF CAM arms the ACT opcodes

The DVE PROF CAM is a `0x400` table of 16-byte records `{opcode(u32 LE), mask(u32), enable(u32),
rsvd}`. Decoded byte-for-byte this session (records with `enable≠0`):

| PROF\_CAM | sha[:8] | armed records | the ACT opcodes 0x23/0x25 |
|---|---|---:|:---:|
| MARIANA_PLUS DVE | `ca588683` | 47 (== MARIANA verbatim) | **NOT armed** |
| MAVERICK DVE | `dbff2b84` | **53** | **ARMED** |

The smoking-gun arming, read directly from the MAVERICK DVE PROF CAM bytes:

```text
{op=0x23, mask=0xff, en=1}   ; ACTIVATION_TABLE_LOAD  <- ACT opcode, NOW profiled on DVE
{op=0x25, mask=0xff, en=1}   ; ACTIVATE2              <- ACT opcode, NOW profiled on DVE
```

Cross-checked against the MARIANA_PLUS DVE CAM (`ca588683`): **`0x23` and `0x25` are NOT in its
armed set** (positive control: `0x64`,`0x70`,`0x71` *are* armed on MPLUS and *removed* on MAVERICK,
see below). So the ACT opcodes are **newly armed on the DVE engine's PROF** — this is the
dispatch-level fold evidence: ACT-class opcodes are now scheduled/profiled on DVE.
`[HIGH/OBSERVED — both CAMs decoded byte-for-byte; 0x23/0x25 present on MAVERICK, absent on MPLUS;
the "fold" reading INFERRED-HIGH]`

### 3a. The full armed-opcode delta — `+10 / −3` (byte-exact)

Opcode→name from the shipped `neuron_maverick_arch_isa` OPCODE enum (`common.h`), with the enum's own
deprecation comments:

| ADDED (+10) | name | | REMOVED (−3) | name + enum comment |
|---|---|---|---|---|
| `0x23` | ACTIVATION_TABLE_LOAD ← **ACT fold** | | `0x64` | BATCH_NORM_PARAM_LOAD `// n, use BatchNormParamLoad2 instead` |
| `0x25` | ACTIVATE2 ← **ACT fold** | | `0x70` | TENSOR_SCALAR_IMM_LD_ARITH `// n, …not maintained/used` |
| `0x58` | MAX_POOL_SELECT | | `0x71` | TENSOR_SCALAR_IMM_LD_BITVEC `// n, …not maintained/used` |
| `0x61` | BATCH_NORM_STATS2 | | | |
| `0x62` | BATCH_NORM_AGGREGATE | | | |
| `0x6c` | MAX8 | | | |
| `0x6d` | MATCH_VALUE_LOAD | | | |
| `0x6e` | FIND_INDEX8 | | | |
| `0x6f` | MATCH_REPLACE8 | | | |
| `0x99` | CAST_PREDICATED | | | |

The `0x64`/`0x70`/`0x71` removals were **verified armed on the MPLUS DVE CAM** and **absent on
MAVERICK** — and each is marked `// n` (deprecated) in the shipped maverick enum, exactly. The
arming is a coherent v5 PROF re-spec: deprecate the old batch-norm-param-load + the two imm-load
variants, arm the new batch-norm-stats2/aggregate + the max8/match/find-index cluster +
cast-predicated, **and** arm the migrated ACT ops. `[HIGH/OBSERVED — both CAMs' armed sets diffed;
deprecation comments read from the shipped enum]`

> **REFINEMENT — `0x1e3` is a mask-width respec, not a 4th deprecation.** A raw masked-opcode diff
> surfaces an apparent 4th "removed" entry `0x1e3`. This is **not** an opcode removal. MARIANA_PLUS
> DVE PROF arms `0xe3` with a **9-bit mask** (`{op=0xe3,mask=0x1ff}`, yielding the `{0xe3, 0x1e3}`
> pair); MAVERICK DVE PROF arms `0xe3` with an **8-bit mask** (`{op=0xe3,mask=0xff}`) only. Both
> verified by reading the raw `mask` field. So the genuine deprecation delta is **`+10/−3`**; the
> `0x1e3` disappearance is the 9-bit→8-bit normalization of `0xe3`'s arming. `[HIGH/OBSERVED — both
> CAMs' raw masks decoded]`

> **CORRECTION — the PROF "+10" is NOT new opcode-space; it is a precision refinement to SX-IMG-18's
> framing.** All 10 added opcodes (`0x23/0x25/0x58/0x61/0x62/0x6c/0x6d/0x6e/0x6f/0x99`) **already
> exist** in the MARIANA ISA enum with identical values + names (verified against
> `neuron_mariana_arch_isa` `common.h`). So the PROF delta is **newly arming pre-existing opcodes on
> the DVE engine** — *not* opcode-space growth. The genuine OPCODE-enum growth (159→165) is a
> **separate** set of **6 different new names** — `ACTIVATE_MULTIPASS`, `COMPACT_CONTROL_INST`,
> `DMA_IMMEDIATE`, `DMA_MEMCPY2`, `TENSOR_SCALAR_INT_WIDE`, `TENSOR_TENSOR_INT_WIDE` (each present in
> the maverick enum, count 0 in the mariana enum). The two deltas are **RELATED** (both v5 re-spec)
> but **DISTINCT enum spaces**. `[HIGH/OBSERVED — both enums read; mariana=159, maverick=165 entries;
> the 6 new names confirmed maverick-only]`

### 3b. PROF — BOTH halves re-authored

Unlike the MARIANA_PLUS *verbatim* per-engine PROF reuse, MAVERICK re-authors **both** halves:
`PROF_CAM dbff2b84` **and** `PROF_TABLE f349e417` are each distinct from MARIANA_PLUS DVE
(`ca588683` / `d72b339f`, which == MARIANA verbatim). The HW-decode profiler config — CAM arming *and*
the profile table — is a v5 build artifact. `[HIGH/OBSERVED — all four shas computed; CAM/TABLE both
distinct]`

---

## 4. The fold footprint #2 — the handler roster is DVE-native, not a DVE+ACT union

### 4a. The 5 ACT handlers — GONE firmware-wide

Region-wide search over the entire MAVERICK block (`0x871300..EOF`, `0x15bac0` bytes) **and** the DVE
DEBUG DRAM specifically:

| ACT handler (`S:` string) | region-wide | DVE DEBUG DRAM |
|---|:---:|:---:|
| `Activate` | **0** | **0** |
| `ActivateQuantize` | **0** | **0** |
| `ActivationTableLoad` | **0** | **0** |
| `ActivationReadAccumulator` | **0** | **0** |
| `Activate2` | **0** | **0** |

The five ACT activation-engine-specific dispatch handlers are **absent as named SEQ handlers anywhere
in MAVERICK**, and in particular are **not** in the DVE image's handler set. **The fold is NOT "the
ACT handlers moved verbatim into DVE."** `[HIGH/OBSERVED — region-wide + DVE-specific = 0 each]`

### 4b. The MAVERICK DVE roster — 59 strict (vs MARIANA_PLUS DVE's 60)

Strict end-anchored single-token diff (the same `S: \K[A-Za-z][\w/-]*$` method the baseline uses,
run on both DEBUG DRAMs):

- **MAVERICK DVE = 59** vs **MARIANA_PLUS DVE = 60**.
- **ADDED in MAVERICK DVE: (none).** **REMOVED from MPLUS DVE: `QuantizeMx`.**

The **only** strict difference is `QuantizeMx` — confirmed **absent** on MAVERICK (`QuantizeMx` count
0 in the DVE DEBUG DRAM, **and 0 region-wide across `0x871300..EOF`**) and **present** on MPLUS. The
`QuantizeMx` **named handler is DROPPED** on v5 — this is a removal, **not** a migration. The MX
machinery is *not* in the NX sequencer on v5 (§6), but the opcode binding is unchanged: `0xe3
QUANTIZE_MX` **stays DVE-bound** (armed on the MAVERICK DVE PROF CAM `dbff2b84`), and POOL's only MX
surface is the pre-existing **`0x7b TENSOR_DEQUANTIZE`** (EXTISA_0 idx16, funcVA `0x50ec`), **never**
`0xe3` (proven absent from all four MAVERICK Q7 POOL KITs, [maverick-pool.md §2.2](maverick-pool.md)).
The MAVERICK DVE list is **DVE-native**
— it carries (count ≥1): the batch-norm cluster (`BatchNormalize{,BackProp,GradAccum,ParamLoad}`),
`MatchReplace`/`MatchValueLoad`/`FindIndex8`/`CastPredicated`/`RangeSelect`/`Exponential`/
`SparsityCompress`/`Rand2`/`DveReadAccumulator`/`DveReadIndices` + the shared SEQ control core
(AluOp/MOVE/WRITE/NOTIFY/BRANCH/NOP/Halt/ErrorHandler/dual-mode HW-Decode/Sunda) — **none** are added
ACT handlers. `[HIGH/OBSERVED — strict 59 vs 60; `QuantizeMx` 0 vs ≥1; DVE-native list present]`

> **NOTE — glue-stripped normalized agrees.** The glue-stripped normalized count is **MAVERICK 97 vs
> MPLUS 101**; the 4-token delta is `QuantizeMx` + `"DGE"` (the dropped DGE fast-path) + `"Applying"`
> + `"Setting"` (log fragments from the dropped `mariana-4062` patch + a dropped setting log) —
> **none** are added ACT handlers. Both methods agree: net `+0 added / −1 removed` on the named DVE
> set, **plus** the firmware-wide ACT-handler amputation. `[HIGH/OBSERVED]`

### 4c. The fold footprint #3 — the ACT read-accumulator survives RENAMED

The MAVERICK DVE handler set carries **`DveReadAccumulator`** (DVE-native, `0x9b`-class), which is
functionally the migrated `ActivationReadAccumulator` (the ACT-side op `0x24`) — but **renamed**, not
the ACT handler string. `DveReadAccumulator` is present (count 1 in the DVE DEBUG DRAM); the ACT
string `ActivationReadAccumulator` is absent (count 0). The read-accumulator datapath was re-expressed
as DVE-native rather than carried as the ACT handler. `[HIGH/OBSERVED — `DveReadAccumulator` 1,
`ActivationReadAccumulator` 0; the "re-expresses the ACT read-accumulator" functional reading
INFERRED-HIGH from the name + the FW-76 ACT-set context]`

### 4d. The fold footprint #4 — the address-map folds ACT under DVE

The shipped maverick address-map (`arch-headers/maverick/ext/al_address_map_db.json`, **OBSERVED**)
enumerates **192 `amzn_tpb…DVE_` MMIO instances** and **128 `…PE_`** — but **zero `…ACT_` engine
instances**. The only ACT residue is `ACT_CONTROL_TABLE`, namespaced **under the DVE block**:
`…TPB_0_DVE_0_0_ACT_CONTROL_TABLE`. The ACT control table is physically folded into the DVE engine's
MMIO region; there is no standalone ACT engine at the hardware-map level. This corroborates the
firmware-image absence with map-level evidence. `[HIGH/OBSERVED — engine-instance counts + the
`DVE_…_ACT_CONTROL_TABLE` namespacing read from the shipped map]`

---

## 5. The v5 reset / boot geometry — a new `−0x20` shift (NOT the MARIANA `+0x1c`)

The MAVERICK DVE PERF IRAM head bytes are `06 75 00 00 00 00 86 76 00`, decoded with the native
`ncore2gp` `xtensa-elf-objdump` (exit 0):

```asm
   0x000:  06 75 00   j 0x1d8   ; primary reset -> boot trampoline
   0x006:  86 76 00   j 0x1e4   ; secondary    -> halt
   0x1d8:  const16 a0, 0
   0x1db:  const16 a0, 148      ; (0x94) enter_run prologue VA
   0x1de:  jx      a0           ; -> enter_run @0x94
   0x1e4:  halt    0            ; 2nd vector = HALT trap
   0x1eb:  const16 a4, 0x1490   ; SEQ sub-table base
   0x1f7:  extui   a3, a3, 0, 4 ; addx4 a4,a3,a4 ; l32i a4,a4,0 ; jx a4   (SEQ dispatch)
```

vs **MARIANA_PLUS DVE** (`06 7d 00` = `j 0x1f8` / `86 7e 00` = `j 0x204` → `enter_run @0x90`, base
`0x1250`). **The v5 shift, all `−0x20`/`+4`:**

| | MARIANA_PLUS DVE | MAVERICK DVE | Δ |
|---|---|---|---|
| primary reset | `j 0x1f8` | `j 0x1d8` | **−0x20** |
| secondary | `j 0x204` | `j 0x1e4` | **−0x20** |
| `enter_run` | `@0x90` | `@0x94` | **+4** |
| dispatch base | `0x1250` | `0x1490` | (relocated) |

This is a **new boot-stub geometry**, **not** the MARIANA `+0x1c` carried forward (which MARIANA_PLUS
inherited unchanged). `[HIGH/OBSERVED — all bytes decoded with `ncore2gp`, exit 0; `j 0x1d8` / `j
0x1e4` / `const16 a0,148` / `const16 a4,0x1490` / the `extui;addx4;l32i;jx` dispatch read
instruction-exact]`

The DRAM head is unchanged: both MAVERICK DVE DEBUG/PERF DRAM lead `34 cb 99 60` = `0x6099cb34`, the
`.globstruct` dispatcher-state magic (unchanged since CAYMAN). `[HIGH/OBSERVED]`

### 5a. The dispatch structure — 187-entry, re-spec'd base/default/real-count

The top-level SEQ dispatch is gen-stable in *form* but relocated in *content* (decoded `ncore2gp`):

| property | MARIANA_PLUS DVE | MAVERICK DVE | Δ |
|---|---|---|:---:|
| normalization | `addi a2,a2,−48` (op − `0x30`) | `addi a2,a2,−48` | **=** |
| bound | `movi a3,186` (187 entries) | `movi a3,186` | **=** |
| HW-Decode table base | DRAM `0x80800` | DRAM `0x80820` | **R** |
| default arm | `0x31e3` | `0x2f90` | **R** |
| real targets | 82 | 79 | **R** |
| `S: Dispatch opcode` log | DRAM `0x80e58` | DRAM `0x80e74` | **R** |

The opcode-dispatch **structure** is gen-stable (187 entries, `addi −48` normalization), but the table
base, default arm, log offset, and real-target count (82→79) are all relocated/re-spec'd —
consistent with the independent v5 build + `QuantizeMx` dropped + the handler reshuffle.
`[HIGH/OBSERVED — dispatch normalization/bound decoded `ncore2gp`; the table base/default/real-count
read from the DRAM bytes]`

> **GOTCHA — the v5 image *interior* is the FLIX-desync frontier.** The 187-bound, the `addi −48`
> normalization, the table base/default/real-count, the reset/boot trampoline, the PROF arming, and
> the `S:` rosters are all HIGH/OBSERVED. But the **per-opcode → handler-*body* binding** (which
> opcode runs which handler body inside the v5 DVE image) is **not byte-resolved** — the PERF/TEST
> IRAM vector datapath desyncs under FLIX-VLIW bundling in the linear sweep (the documented SX-FW-00
> ceiling). Every interior-body claim is **INFERRED**, flagged inline. `[HIGH for the spine + the
> rosters/PROF; MED/INFERRED per-handler body]`

> **NOTE — the `movi a3,192` inner-bound sites are NOT the dispatch.** The DVE DEBUG IRAM has many
> `movi a3,192` sites paired with `const16` string-pointer loads + `movi a12,14` — these are
> per-handler argument-validation / inner bounds, **not** the top-level opcode jump table (which
> stays 187-entry). Do not mistake `movi a3,192` for a dispatch-width change. `[HIGH/OBSERVED that
> the top dispatch is 187-entry; the 192-site precise role MED/INFERRED]`

---

## 6. Dtype / DGE / errata — the rest of the v5 step

### 6a. Dtype — stays numeric in the NX sequencer; the MX machinery moved to the Q7 POOL

Across all 8 MAVERICK DVE images, the v5 dtype-superset named strings (`FP8_EXP2`/`INT4`/`SFP8`/
`MXTENSOR`/`MX_PERF`/`TILE_SIZE`/`FP4_EXP2`/`CPTC`/`QuantizeMx`) = **0 hits**; positive control: only
`NEURON_ISA_TPB_DTYPE_{FP32,INT32,UINT32}` numeric constants present. The v5 dtype superset
(`+FP8_EXP2(0x11)/INT4(0x12)/SFP8_E8..E5(0x13..0x16)` over MARIANA's FP4/CPTC) stays **numeric** in
the NX sequencer — exactly as MARIANA_PLUS DVE. The MX/sub-byte **dequant** machinery lives in the
MAVERICK **Q7 POOL** kernel (region-wide: `proc_4bit_mx_8`(1), `proc_4bit_non_mx`(1),
`dequant`(1)/`Dequant`(2)); both RNG algos present there (`XorwowRng`(1) + `LfsrSetSeeds`(1)). So the
v5 dtype additions are real at the image level — realized in the Q7 POOL dequant path, **not** as
NX-sequencer dtype strings. `[HIGH/OBSERVED — DVE-image dtype-string negative; Q7-POOL MX-dequant
positive]`

### 6b. DGE reshape fast-path — DROPPED on v5

The v4+ SEQ-side DGE fast-path is essentially gone:

| string | MAVERICK | MARIANA_PLUS DVE |
|---|:---:|:---:|
| `dge_decode_fast` | **0** | 2 |
| `dge_reshape_memcopy_transpose_fast` | **0** | 2 |
| `dge_backend_rtl` | **0** | 4 |
| `dge_reshape.cpp` | **0** | 2 |
| `wait_for_credit` | **0** | 2 |
| `analyze_tensor_reshape` | **0** | 2 |
| `tensor_reshape_transpose_sb2sb` | 1 | 2 |

Only a single `tensor_reshape_transpose_sb2sb` survives. Descriptor generation was **re-architected to
the HW DMA path** (the v4+ SEQ fast-path was not carried forward) — part of why MAVERICK DVE is
*smaller*. `[HIGH/OBSERVED counts; the "re-architected to HW DMA" reading INFERRED-HIGH from
ADDR-11/GEN-04]`

### 6c. The `mariana-4062` DVE errata — DROPPED

The DVE-specific silicon-errata workaround `"Applying patch for mariana-4062"` that MARIANA_PLUS DVE
**retained** (a DVE-only patch absent on MPLUS ACT) is **absent** on MAVERICK (0 region-wide vs 1 on
MPLUS DVE DEBUG DRAM). The v5 build did not carry the v4-gen DVE errata forward (different silicon, or
fixed-in-HW). No `maverick-<NNNN>` errata string observed; the self-name `"S: BEGIN on maverick"` is
present (1). `[HIGH/OBSERVED — region-wide count 0]`

### 6d. Cross-gen size — SMALLER in every variant; an independent build

| IMAGE | MAVERICK | MARIANA_PLUS | ΔSize |
|---|---|---|---:|
| PERF\_IRAM | `0xec00` | `0x16e80` | **−0x8280** |
| PERF\_DRAM | `0x2740` | `0x32c0` | −0xb80 |
| TEST\_IRAM | `0xf5c0` | `0x16be0` | **−0x7620** |
| TEST\_DRAM | `0x29c0` | `0x3660` | −0xca0 |
| DEBUG\_IRAM | `0x19000` | `0x1d760` | **−0x4760** |
| DEBUG\_DRAM | `0x5f80` | `0x7160` | −0x11e0 |
| PROF\_CAM | `0x400` | `0x400` | +0 (re-authored `dbff2b84`) |
| PROF\_TABLE | `0x2000` | `0x2000` | +0 (re-authored `f349e417`) |

MAVERICK DVE shrinks because the DGE fast-path bulk was dropped + ACT folded out — the **inverse** of
the MARIANA→MARIANA_PLUS IRAM *growth*. **Build independence:** MAVERICK DVE PERF IRAM diverges from
MARIANA_PLUS DVE PERF IRAM at **byte 1** (the reset immediate `75` vs `7d`) with only **6.1% positional
16-byte block similarity** (231/3776) — a fully independent v5 build, **not** a relocated recompile
(contrast the v4→v4+ step, which shared a `0x212`-byte prefix). The PERF IRAM is a genuine sequencer:
107 `entry` / 134 `retw` / 340 `call8` (decoded `ncore2gp`, exit 0). `[HIGH/OBSERVED — sizes from
getter `movq`; the byte-1 divergence + block-similarity computed this session]`

---

## 7. Engine-model confirmation — `engine_idx=3` (DVE), NC-v5, ACT=1 logical-only

The shipped ISA enum (re-read this session,
`neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h:142-149`):

```c
NEURON_ISA_TPB_NEURON_ENGINE { PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5 }
```

So **DVE = `engine_idx 3`** — the head engine that absorbed the folded ACT. **`ACT=1` still exists as
a *logical* ISA engine** (the enum is unchanged), but the logical `ACT=1` has **no firmware image**:
the ACT datapath executes/profiles on DVE (§3/§4). `NEURON_CORE_VERSION` tops at **V5** (`…V5 = 5`).
The DVE image carries the runtime engine-identity string `S: engine_base_addr=%llx tpb_base_addr=%llx
-> is_tpb=%u is_die_0=%u engine_idx=%u`, the DVE-specific `S: DVE perf mode support = %d`, the
per-fetch `S: Dispatch opcode=0x%x`, the dual `S: NX in HW Decode mode` / `S: NX in Sunda mode: HW
decode disabled`, and `S: ErrorHandler : Bad Opcode(0x%x)` — the **same** SEQ dispatch model,
`engine_idx` still boot-computed. Source tree still `cayman/seq/src/…` (the v5 build reuses the
`cayman/seq` label). `[HIGH/OBSERVED — enum/version/SEQ-model strings; `engine_idx` runtime-compute
INFERRED-HIGH from the string + the shared boot path]`

> **WALL — `coretype 37` is OBSERVED upstream, `arch_id 36` is INFERRED.** `coretype = 37`
> [HIGH/OBSERVED upstream — `maverick_libs @0x9b9050`; the ct=37 dispatch + bit37 in the
> `get_num_ext_isa_libs` mask `{13,21,29,37}`]. `arch_id = 36` [MED/INFERRED — **doubly**: it rests
> on `coretype = arch_id+1` across the four lower gens, **and** the `NX_TOPSP = arch_id` rule that
> grounds the other four gens *fails* for MAVERICK (enum slot 36 = `MAVERICK_NX__REMOVED__`
> placeholder; the real `MAVERICK_NX_TOPSP` is at index 54). **No NCFW v5 image** to confirm; never
> binary-observed — see [maverick-profile §7 W1](../generations/maverick-profile.md)]. The v5
> `Q7_CC_TOP` is **FILE-ABSENT**. `[CARRIED from generations/maverick-profile.md]`

---

## 8. Adversarial self-verify — the five strongest claims, re-challenged

1. **The 5 ACT handlers are absent firmware-wide.** Re-challenged: grepped `Activate$`/
   `ActivateQuantize`/`ActivationTableLoad`/`ActivationReadAccumulator`/`Activate2` over the
   `0x871300..EOF` MAVERICK region (`0x15bac0` bytes) **and** the DVE DEBUG DRAM carve — **0 each in
   both scopes**. `DveReadAccumulator` (1) is *not* a false hit on `ActivationReadAccumulator` (0).
   The baseline agent confirmed the same 5 are also 0 on MPLUS DVE (ACT was a *separate* image there).
   **HOLDS. `[HIGH/OBSERVED]`**

2. **The DVE roster is DVE-native 59 (vs 60), only `QuantizeMx` differs.** Re-challenged: strict
   `S: \K[A-Za-z][\w/-]*$` count on the MAVERICK DVE DEBUG DRAM = **59**; the baseline agent's same
   method on MPLUS DVE = **60** with `QuantizeMx` present; `QuantizeMx` count on MAVERICK = **0**. No
   ACT handler appears in the MAVERICK roster. **HOLDS. `[HIGH/OBSERVED]`**

3. **The DVE PROF CAM newly arms `0x23`/`0x25`.** Re-challenged: decoded both CAMs as 16-byte records.
   MAVERICK `dbff2b84` arms `{op=0x23,mask=0xff,en=1}` + `{op=0x25,mask=0xff,en=1}`; MARIANA_PLUS
   `ca588683` arms **neither** (positive control: `0x64/0x70/0x71` *are* on MPLUS, *removed* on
   MAVERICK, matching the `// n` deprecation comments; `0xe3` is a 9-bit→8-bit mask respec, not a 4th
   deprecation). The `+10` are all pre-existing in the mariana enum (re-armed, not new opcode-space).
   **HOLDS — with the two refinements. `[HIGH/OBSERVED]`**

4. **`DveReadAccumulator` is the renamed ACT read-accumulator.** Re-challenged: `DveReadAccumulator`
   present (1) on MAVERICK DVE; `ActivationReadAccumulator` (the ACT op `0x24`, confirmed `// Y` in the
   maverick enum) absent (0). The *rename* (string-level) is OBSERVED; the *functional equivalence* to
   the ACT op is **INFERRED-HIGH** (not byte-traced through the datapath). **HOLDS, with the
   equivalence flagged INFERRED. `[HIGH/OBSERVED rename; INFERRED-HIGH equivalence]`**

5. **The v5 reset geometry is `j 0x1d8` (`−0x20`), not the MARIANA `+0x1c`.** Re-challenged: `xxd` of
   the carve = `06 75 00 00 00 00 86 76`; `ncore2gp` decodes `j 0x1d8` / `j 0x1e4`, boot `const16
   a0,148(0x94); jx a0`, sub-table `const16 a4,0x1490`, dispatch `extui;addx4;l32i;jx`. MPLUS DVE =
   `06 7d` → `j 0x1f8` → `@0x90` / base `0x1250`. The `−0x20`/`+4` shift is exact. **HOLDS.
   `[HIGH/OBSERVED — decoded `ncore2gp`, exit 0]`**

All five hold against the binary; the only INFERRED residue is the *causal* fold reading, the
read-accumulator *functional* equivalence, and `arch_id 36` — each flagged.

---

## 9. Verdict — the fold is VERIFIED, as PROF-arming + datapath-rename

**VERDICT `[HIGH/OBSERVED]`: MAVERICK × DVE is a REAL new generation that VERIFIES the ACT→DVE fold at
the dispatch level — and the fold is a PROF-arming + datapath-rename event, NOT a handler-image
merge.** The five ACT handlers ceased to exist as firmware images (0 each); their opcodes (`0x23`/
`0x25`) are armed on the DVE PROF (absent from MARIANA/MPLUS DVE PROF); the read-accumulator is
re-expressed DVE-native (`DveReadAccumulator`); the address-map folds `ACT_CONTROL_TABLE` under the
DVE block. The DVE handler set is DVE-native 59 (the `QuantizeMx` named handler is **dropped**, not
migrated; `0xe3` stays DVE-bound, POOL's MX surface is `0x7b`). The v5
image-level deltas — a new `−0x20` reset geometry, a re-authored PROF (CAM + TABLE, `+10/−3` armed,
the `0xe3` mask respec), the DGE fast-path dropped, the `QuantizeMx` named handler dropped (MX
*dequant* lives in the Q7 POOL via `0x7b`; `0xe3` is unmoved),
the `mariana-4062` errata dropped, smaller in every variant, internal-twin-only, an independent build
(6.1% block-similarity) — are each OBSERVED. The only INFERRED items are the fold *causal* reading,
the per-handler-body binding (FLIX-desync frontier), and `arch_id 36`.

This page and [maverick-act.md](maverick-act.md) are the **two halves of the fold thesis** and are
kept consistent: the ACT image is absent, the ACT handlers are gone firmware-wide, and the fold =
PROF-arming (`0x23`/`0x25` on DVE) + `DveReadAccumulator` on DVE, **not** a DVE-handler merge.

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct disasm / byte read / shipped-header read this session):

- `internal.so b7c67e89` + `.a 158dadc5` re-hashed (MATCH). 14 `MAVERICK_NX_DVE` getters parsed
  instruction-exact (8 real + 6 zero cursors → TEST/DEBUG_IRAM + PE_PERF_IRAM, proving DVE→PE).
  **0 MAVERICK members** in `libnrtucode.a` (435 total = 420 image + 15 framework) — single-source carve. 8/8 carves sha256'd
  (MATCH SX-IMG-18). DVE-is-head; gen boundary byte-exact (`0x86f300+0x2000 == 0x871300`).
- Reset/boot decoded `ncore2gp` (exit 0): `j 0x1d8` / `j 0x1e4` → `enter_run @0x94`, base `0x1490`;
  the `−0x20`/`+4` v5 shift. DRAM magic `0x6099cb34`. Dispatch `extui;addx4;l32i;jx` @`0x1f7`.
- The 5 ACT handlers = **0** region-wide AND 0 in the DVE image. The DVE PROF arms ACT opcodes
  `0x23`/`0x25` (absent on MPLUS/MARIANA DVE PROF). `DveReadAccumulator` (1) renames the
  read-accumulator. Address-map: 192 DVE / 128 PE / **0 ACT** engine instances; `ACT_CONTROL_TABLE`
  under DVE.
- DVE roster strict **59 vs 60** (only `QuantizeMx` differs); glue-norm 97 vs 101. DVE-native list
  present.
- PROF CAM delta decoded byte-for-byte: `dbff2b84` 53-armed vs `ca588683` 47-armed; `+10` (incl.
  `0x23`/`0x25`) / `−3` (`0x64`/`0x70`/`0x71`, each `// n`) + the `0xe3` 9-bit→8-bit mask respec. The
  `+10` are pre-existing mariana opcodes (re-armed). Enum grows **159→165** (+6 different names:
  ACTIVATE_MULTIPASS/COMPACT_CONTROL_INST/DMA_IMMEDIATE/DMA_MEMCPY2/TENSOR_SCALAR_INT_WIDE/
  TENSOR_TENSOR_INT_WIDE). PROF_TABLE also re-authored (`f349e417`).
- Dispatch `addi −48` / `movi a3,186` (187-entry, SAME); base `0x800→0x820`, default `0x31e3→0x2f90`,
  real 82→79, log `0x80e58→0x80e74`.
- Dtype numeric-only in NX (no FP8/INT4/SFP8/MX/`QuantizeMx`); MX dequant + both RNG algos in the Q7
  POOL. DGE fast-path 0. `mariana-4062` dropped (0). Smaller in every variant; 6.1% block-similarity.
  `S: BEGIN on maverick`. ISA enum DVE=3, ACT=1 logical, NC-v5; `DVE perf mode support` string.

**MED / INFERRED:**

- The "ACT→DVE fold" as the **cause** of the missing ACT image (OBSERVED: image absent + `0x23`/`0x25`
  armed on DVE PROF + read-accumulator renamed + map fold; the *causal* reading corroborated by
  GEN-04/ADDR-11 + FW-76).
- `DveReadAccumulator` *functional* equivalence to the ACT read-accumulator (rename OBSERVED;
  equivalence INFERRED-HIGH).
- The exact per-opcode→handler-*body* binding on the v5 DVE dispatch (FLIX-desync frontier; PROF
  arming + handler list + table base/structure are OBSERVED, the per-row body is not byte-resolved —
  the SX-FW-00 ceiling).
- "DGE re-architected to HW DMA" (SEQ fast-path absent OBSERVED; the reading from ADDR-11/GEN-04).
- `engine_idx` computed at boot (=3 for DVE) — INFERRED from the string + the shared boot path.

**LOW / NOT CLAIMED:**

- `arch_id = 36` — `coretype=arch_id+1` extrapolation; no NCFW v5 image; never binary-observed.
- Whether the runtime ever **loads** MAVERICK (front lib = 0 MAVERICK refs; shipping vs pre-release
  out of scope).
- The exact `mariana-4062` errata semantics / why it was dropped (silicon vs fixed-in-HW).
- The `movi a3,192` inner-bound sites' precise role (per-handler arg-validation, not the dispatch).
- The NC-v5 wait-mode re-model's image effect (header-only; firmware does not name wait modes).

---

## Cross-references

- **[MARIANA_PLUS × DVE image](mariana-plus-dve.md)** — the committed v4+ baseline this page diffs
  against (the 60-handler roster, the 187-bound dispatch, the `+0x1c` reset, PROF `ca588683`/
  `d72b339f`, the DGE fast-path present).
- **[MAVERICK × ACT image](maverick-act.md)** — the **fold counterpart**: the ACT-side of the fold
  (NX_ACT FILE-ABSENT, the ACT opcodes `0x23`/`0x25` on the DVE PROF CAM, `DveReadAccumulator`
  re-expressing `ActivationReadAccumulator`). The two halves of the fold thesis.
- **[MARIANA × DVE image](mariana-dve.md)** — the deeper v4 baseline (the engine model, the 187-bound
  dispatch, the `+0x1c` reset).
- **[DVE Read-State kernel](../firmware/kernels/dve-read-state.md)** + **[Activate / PWL kernel](../firmware/kernels/activate-pwl.md)**
  — the DVE-native read kernels and the (now-folded) Activate PWL/LUT mechanism whose
  read-accumulator survives as `DveReadAccumulator`.
- **[MAVERICK profile](../generations/maverick-profile.md)** — the v5 generation profile (coretype 37,
  `arch_id 36`\* INFERRED, ACT-into-DVE, NC-v5, the dtype superset).
- **[Image Catalog Index](image-catalog-index.md)** — the full getter image catalog (where the
  `MAVERICK_NX_ACT_*` FILE-ABSENCE — the fold — was first found).
- **[The Confidence & Walls Model](../reference/confidence-model.md)** — the
  `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tags, the v5 HEADER-OBSERVED wall, and the FLIX-desync
  ceiling.
