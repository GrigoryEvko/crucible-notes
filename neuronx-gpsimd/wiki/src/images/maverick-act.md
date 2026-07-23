# MAVERICK × ACT image — the ACT→DVE fold (cross-gen diff vs MARIANA_PLUS)

This page **starts the MAVERICK (v5 / NC-v5) engine matrix** — and it starts with
a structural wall. Where the [MARIANA_PLUS × ACT page](./mariana-plus-act.md)
diffed a recompiled-but-functionally-identical standalone ACT image, **MAVERICK
ships no standalone `NX_ACT` firmware image at all.** The Activation datapath is
*folded* (ACT-into-DVE per the SoC address map): the ACT-class opcodes
`ACTIVATION_TABLE_LOAD` (`0x23`) and `ACTIVATE2` (`0x25`) appear instead on the
MAVERICK **DVE** engine's PROF CAM, and ACT's read-accumulator is re-expressed as
the DVE-native `DveReadAccumulator` (`0x9b`). So a "MAVERICK × ACT image carve"
resolves to: *there is no MAVERICK ACT image*; this page carves what **replaces**
it — the MAVERICK DVE image, the head of the MAVERICK NX block — and diffs that
merged surface against the committed `MARIANA_PLUS` ACT baseline.

Everything below is read from `libnrtucode_internal.so`
(sha256 `b7c67e89…632fc329b`) via its 62 `MAVERICK_*_get` accessors, carved from
identity-mapped `.rodata`, with the shipped Cadence Vision-Q7 `ncore2gp`
`xtensa-elf-objdump` decoding the flat blobs, plus the shipped
`neuron_maverick_arch_isa` clean ISA enum for opcode→name.

> **THE EPISTEMIC WALL — read before every claim below.**
> **MAVERICK (v5) is HEADER-OBSERVED only.** Its image *heads*, accessors,
> `.rodata` blobs, reset bytes, PROF CAMs, and roster strings are byte-grounded and
> tagged `OBSERVED`. But v5 *interior* mechanism — the per-opcode→handler row
> binding on the DVE dispatch table, the activation control-flow under the fold,
> the wait-mode re-model — is **not** byte-resolved (FLIX-VLIW desync frontier)
> and is tagged **`INFERRED`** wherever it appears. `arch_id 36` is
> `INFERRED` (`coretype = arch_id + 1`; no NCFW v5 image exists). Do **not** read
> any v5-interior statement on this page as fact. [WALL]

> **THE ONE HEADLINE, up front.**
> 1. **`nm libnrtucode_internal.so | rg -c 'MAVERICK.*NX_ACT'` = `0`** — three
>    independent zero-counts (symbols, case-insensitive `maverick`+`act`, `.rodata`
>    strings). There is **no MAVERICK ACT sequencer binary.** The
>    [image-catalog-index](./image-catalog-index.md) found `MAVERICK_NX_ACT_*`
>    FILE-ABSENT. [HIGH/OBSERVED]
> 2. **The ACT opcodes moved to DVE.** The MAVERICK DVE PROF CAM (`dbff2b84`) arms
>    `0x23 ACTIVATION_TABLE_LOAD` + `0x25 ACTIVATE2` — which MARIANA DVE PROF
>    (`ca588683`) does **not** arm. ACT-class opcodes are now scheduled/profiled on
>    the DVE engine. [HIGH/OBSERVED]
> 3. **ACT→DVE is a RENAME / re-expression, NOT a functional merge.** The
>    activation datapath is re-expressed under DVE dispatch (`DveReadAccumulator`
>    replaces `ActivationReadAccumulator`); the MAVERICK DVE handler roster is
>    **DVE-native, not a DVE+ACT union** (59 handlers; no `Activate*` anywhere
>    firmware-wide). The fold does not collapse ACT into a vector op. [HIGH/OBSERVED
>    roster; the "rename-not-merge" framing CARRIED from the MARIANA fold-disproof]

Contrast the rest of the lineage: SUNDA/CAYMAN/MARIANA/MARIANA_PLUS each ship a
**standalone** `NX_ACT` image (the MARIANA pages proved **NO fold** in v4/v4+).
**This is the page that proves the fold.** It is a MAVERICK-only event.

Related pages: [MAVERICK × DVE — the DVE-absorbs-ACT counterpart](./maverick-dve.md) ·
[MARIANA_PLUS × ACT (the diff base)](./mariana-plus-act.md) ·
[MARIANA × ACT (the standalone-ACT baseline)](./mariana-act.md) ·
[Activate + PWL Application Mechanism](../firmware/kernels/activate-pwl.md) ·
[DveReadAccumulator / DVE state read-back](../firmware/kernels/dve-read-state.md) ·
[MAVERICK generation profile](../generations/maverick-profile.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md).

---

## 1. The delta table (MARIANA_PLUS ACT → MAVERICK) — STRUCTURAL fold first

The whole page in one table. The **bold** rows lead with the *structural* delta
(an ACT image that ceased to exist). `(==)` marks the few invariants that survive
the gen-step. Unlike the MARIANA_PLUS page — whose headline was a *null* functional
delta — this table's headline is the **opposite of `+0/−0`**: an entire engine
image removed and its opcodes relocated.

| PROPERTY | MARIANA_PLUS ACT (baseline) | MAVERICK "ACT" (this page) | Δ |
|---|---|---|---|
| **standalone `NX_ACT` image** | **YES** — 14-getter image, head of NX block | **ABSENT** — 0 `MAVERICK_NX_ACT_*` symbols | **🧱 FOLD: image removed** |
| **ACT-class handlers (firmware-wide)** | `Activate`/`Activate2`/`ActivateQuantize`/`ActivationTableLoad`/`ActivationReadAccumulator` present | **0 hits, any MAVERICK blob** | **ALL REMOVED as SEQ handlers** |
| **ACT opcodes `0x23`/`0x25`** | armed on the ACT PROF CAM (`326bc0dd`) | **armed on the DVE PROF CAM** (`dbff2b84`) | **MOVED onto DVE** |
| **ACT read-accumulator** | `ActivationReadAccumulator` (ACT handler) | **`DveReadAccumulator` (`0x9b`)** — DVE-native | **RE-EXPRESSED, renamed** |
| ACT engine `engine_idx` | `1` (ISA enum + image) | `1` ISA enum only — **no image** | enum holds, image gone |
| getters (`nm` / IDA sidecar) | 14 / 14 (`NX_ACT`) | **0 / 0** (`NX_ACT`); 62 total MAVERICK | structural |
| what replaces it (carved here) | n/a | **MAVERICK DVE image**, 14 getters, head of NX block | DVE = new head |
| reset vector | `06 7d 00 00` → `j 0x1f8` | **`06 75 00 00` → `j 0x1d8`** | **−0x20 (NEW v5 geom)** |
| 2nd vector | `86 7e 00 00` → `j 0x204` | **`86 76 00 00` → `j 0x1e4`** | **−0x20** |
| boot path | `const16 a0,0x90 ; jx a0` → `enter_run@0x90` | `const16 a0,0x94 ; jx a0` → `enter_run@0x94` | **+0x4** |
| dispatch sub-table base | `const16 a4,0x1250` | `const16 a4,0x1490` | relocated |
| DRAM `.globstruct` magic | `0x6099cb34` | `0x6099cb34` | `(==)` byte-identical |
| dispatch model | `addx4`-indexed DRAM table + per-fetch `S: Dispatch opcode` | same | `(==)` SEQ model |
| **PROF CAM (the fold's footprint)** | ACT `326bc0dd` (29-handler) reused MARIANA verbatim | **DVE `dbff2b84` re-authored** (+10/−3 vs MARIANA DVE) | **RE-SPEC'd** |
| DGE reshape fast-path | **present** (the v4+ functional addition) | **DROPPED** (`dge_decode_fast` 0 vs 10) | removed |
| MX / sub-byte dtype machinery | (numeric; not in ACT image) | in the **Q7_POOL** dequant kernel, not the NX seq | relocated |
| DEBUG variant | ACT has DEBUG IRAM/DRAM | DVE keeps DEBUG; **PE/POOL/SP drop it** | thinned |
| static archive (`libnrtucode.a`) | 124 `MARIANA_PLUS` members | **0 MAVERICK members** | internal-twin-only |
| engine self-name | `S: BEGIN on mariana_plus` | `S: BEGIN on maverick` (on the DVE image) | **rename** |
| build relationship | recompile of MARIANA (`0x212`-byte shared prefix) | **independent v5 build** (6.1% block-sim, diverge byte 1) | real gen-step |

> **🧱 GOTCHA — this is NOT a carve gap.** The absence of a MAVERICK ACT image is
> not "we failed to find the getters." It is **three independent zero-counts** over
> the only binary that carries MAVERICK at all (§2), plus the IDA `_names.json`
> sidecar listing **0** `MAVERICK_NX_ACT_*`. The five-NX-engine MARIANA roster
> (`ACT/DVE/PE/POOL/SP`) is a **four**-NX-engine MAVERICK roster
> (`DVE/PE/POOL/SP`), with ACT folded into DVE. [HIGH/OBSERVED]

> **NOTE — what "fold" does and does not mean here.** OBSERVED: the ACT image is
> gone; ACT opcodes are armed on DVE's PROF; the read-accumulator is renamed to
> `DveReadAccumulator`. The causal reading — *that the ACT datapath now executes
> under DVE dispatch* — is `INFERRED-HIGH`, corroborated by the
> [MAVERICK address-map fold node](../generations/maverick-profile.md) (ACT folded
> into DVE, header/map level, OBSERVED there). The fold is a **dispatch
> re-homing**, not a collapse of ACT into a vector instruction. [INFERRED-HIGH]

---

## 2. The "ACT is absent" proof — getter inventory, three zero-counts

**SOURCE (the ONLY binary carrying MAVERICK):**
`…/custom_op/c10/lib/libnrtucode_internal.so`, sha256
`b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (MATCH).
ELF64 x86-64 DYN, not stripped; first `R` LOAD maps file `0x0` → VA
`0x0` (identity map → blob file-offset == `.rodata` VA). [HIGH/OBSERVED]

```text
nm libnrtucode_internal.so | rg -c 'MAVERICK.*NX_ACT'       →  0
nm libnrtucode_internal.so | rg -ci 'maverick.*act'         →  0   (case-insensitive)
strings .rodata          | rg -ci 'maverick.*act'           →  0   (string layer)
ida …_names.json         | rg -c  'MAVERICK_NX_ACT'         →  0   (IDA v3 sidecar)
```

**THERE IS NO MAVERICK ACT IMAGE** — four independent zeros. [HIGH/OBSERVED]

The **62** MAVERICK getters (each the canonical 4-instruction accessor stub
`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`,
e.g. `MAVERICK_NX_DVE_PERF_IRAM_get` @`.text 0x9b55a0` → `.rodata 0x871300`,
`movq $0xec00,(%rsi)`) distribute as:

| ENGINE | `_get` accessors | variants present | notes |
|---|---|---|---|
| **`NX_ACT`** | **0** | **(NONE)** | **🧱 ABSENT — the fold** |
| `NX_DVE` | **14** | DEBUG(4) PERF(4) TEST(4) PROF(CAM,TABLE) | **full set — head of NX block** |
| `NX_PE` | 10 | PERF(4) TEST(4) PROF(CAM,TABLE) | NO DEBUG |
| `NX_POOL` | 10 | PERF(4) TEST(4) PROF(CAM,TABLE) | NO DEBUG |
| `NX_SP` | 8 | PERF(4) TEST(4) | NO DEBUG, NO PROF; runs from SRAM |
| `Q7_POOL` | 20 | DEBUG(4) PERF(4+8 EXTISA) TEST(4) | the MX-dequant home (§6) |
| **TOTAL** | **62** | — | matches the catalog index |

Contrast: `nm … | rg -c 'MARIANA_PLUS_NX_ACT_.*_get'` → **14**. The standalone ACT
image exists on v4+ and is gone on v5. [HIGH/OBSERVED]

> **GOTCHA — MAVERICK is internal-twin-EXCLUSIVE; the carve is single-source.**
> `libnrtucode.a` (sha256 `158dadc5…`, MATCH) ships **0** MAVERICK members
> (**435** total = **420 image** [CAYMAN 124 / MARIANA 124 / MARIANA_PLUS 124 / SUNDA 48 /
> MAVERICK 0] **+ 15 framework `.c.o`**). MAVERICK lives
> *only* in the clang-15 `internal.so` twin. So unlike the MARIANA_PLUS carve —
> reconciled 8/8 against the matching `.a` member `.rodata` — **there is no
> `.a` member to byte-reconcile a MAVERICK carve against.** Every MAVERICK blob
> below is single-source by necessity (the getter `(img-ptr,size)` in
> `internal.so .rodata`). This is itself a gen-step signature: the shipped static
> archive tops out at MARIANA_PLUS. [HIGH/OBSERVED]

---

## 3. What replaces ACT — the MAVERICK DVE image head (carve + reset)

With ACT amputated, **DVE is the head of the MAVERICK NX block.** The lowest
MAVERICK `.rodata` blob is `MAVERICK_NX_DVE_PERF_IRAM` @`0x871300`. The
**gen boundary is byte-exact**: `MARIANA_PLUS_NX_POOL_PROF_TABLE.data` @`0x86f300`
+ `0x2000` = `0x871300` == `MAVERICK_NX_DVE_PERF_IRAM.data` @`0x871300`. The two
generations are laid out contiguously (MARIANA_PLUS fully, then MAVERICK), and DVE
is the first MAVERICK image — **no ACT image in between**, the byte-level
fold-disproof inverted. [HIGH/OBSERVED — `nm` `.data` sort + getter `lea`]

The 8 real MAVERICK DVE blobs (single-source carve, sha256):

| IMAGE | off (`.rodata` VA == file off) | size | sha256 |
|---|---|---|---|
| DVE PERF_IRAM | `0x871300` | `0x0ec00` | `8da04026…08f7819a5` |
| DVE PERF_DRAM | `0x87ff00` | `0x02740` | `bb398891…06b18959` |
| DVE TEST_IRAM | `0x882640` | `0x0f5c0` | `0254f750…baac7031` |
| DVE TEST_DRAM | `0x891c00` | `0x029c0` | `1b24f954…6661ab4c` |
| DVE DEBUG_IRAM | `0x8945c0` | `0x19000` | `ffaefd3c…e8b6b254` |
| DVE DEBUG_DRAM | `0x8ad5c0` | `0x05f80` | `ed5f22e3…09fb6cf29` |
| DVE PROF_CAM | `0x9a42a0` | `0x00400` | `dbff2b84…23eacd19` |
| DVE PROF_TABLE | `0x9a46a0` | `0x02000` | `f349e417…f9a5cacef` |

[HIGH/OBSERVED — getter `movq` sizes + carve sha256.] The PERF/TEST/DEBUG IRAMs are
genuine SEQ code, not stubs (§7); SRAM/EXTRAM getters are zero-size boundary
cursors, as on every gen. `S: BEGIN on maverick` is carried on the DVE DEBUG DRAM
(the v5 self-name; there is no ACT image to carry one). [HIGH/OBSERVED]

### 3a. The reset/boot vector — a NEW v5 geometry, NOT the MARIANA `+0x1c`

MAVERICK DVE PERF IRAM head bytes are `06 75 00 00 00 00 86 76 00 00 00 00`, with
the shared boot stub `a0 71 69 80` at `[12..15]` byte-identical to every prior gen.
Decoded with the shipped `ncore2gp` `xtensa-elf-objdump`:

```text
0x000:  06 75 00     j        0x1d8      ; primary reset vector → boot trampoline   (MPLUS: j 0x1f8)
0x006:  86 76 00     j        0x1e4      ; secondary vector → halt trap             (MPLUS: j 0x204)
0x1d8:  const16  a0, 0 ; const16  a0, 148(0x94) ; jx a0   →  enter_run @0x94        (MPLUS: @0x90)
0x1e4:  halt     0                                          ; 2nd vector is a HALT trap
       (SEQ dispatch trampoline:  extui a3,a3,0,4 ; addx4 a4,a3,a4 ; l32i a4,a4,0 ; jx a4)
       (dispatch sub-table base:  const16 a4, 0x1490)                                (MPLUS: 0x1250)
```

The **v5 shift**: primary `0x1f8 → 0x1d8` (`−0x20`), secondary `0x204 → 0x1e4`
(`−0x20`), `enter_run 0x90 → 0x94` (`+0x4`), dispatch base `0x1250 → 0x1490`. This
is a **new boot-stub geometry**, not the MARIANA `+0x1c` forward shift. The shift
is uniform: DVE/PE/POOL all reset `j 0x1d8 / j 0x1e4`; SP resets `j 0x1e4 / j 0x1f0`
(the Top-Sync stub, `+0xc`; SP runs from SRAM, not IRAM). The DRAM head is
`34 cb 99 60` = `0x6099cb34`, the `.globstruct` dispatcher-state magic **unchanged**
on v5. [HIGH/OBSERVED — all reset/boot bytes decoded with `ncore2gp`]

> **NOTE — `engine_idx` is still boot-computed (`= 1` for the logical ACT).** The
> shipped enum `NEURON_ISA_TPB_NEURON_ENGINE { PE=0, ACT=1, POOL=2, DVE=3,
> TPB_SP=4, TOP_SP=5 }` is byte-for-byte CAYMAN's — `ACT=1` remains a **logical**
> ISA engine index on v5. The DVE image carries the same runtime-identity string
> `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u
> engine_idx=%u` + `S: Dispatch opcode=0x%x` + the `0x6099cb34` magic — the same
> flat-image-loaded-at-engine-base SEQ model. So **`engine_idx=1=ACT` holds at the
> ISA-enum level but NOT at the firmware-image level** (no ACT sequencer binary; the
> datapath runs on DVE). [HIGH/OBSERVED enum/string; the fold reading INFERRED-HIGH]

---

## 4. The fold's image-level footprint — the DVE PROF CAM (ACT opcodes on DVE)

The PROF CAM is a `0x400` table of 16-byte armed records
`{opcode(u32 LE), mask=0xff, enable, rsvd…}`. The **smoking gun** is differential:
the ACT opcodes `0x23`/`0x25` are armed on the MAVERICK DVE CAM but **absent** from
the MARIANA DVE CAM — decoded byte-for-byte:

| PROF CAM | sha256(8-hex) | armed-opcode-set | `0x23` `ACTIVATION_TABLE_LOAD`? | `0x25` `ACTIVATE2`? |
|---|---|---|---|---|
| MARIANA DVE | `ca588683` | 46 | **NO** | **NO** |
| MARIANA_PLUS DVE | `ca588683` | 46 | **NO** | **NO** *(byte-identical to MARIANA — verbatim reuse)* |
| **MAVERICK DVE** | **`dbff2b84`** | **53** | **YES** | **YES** |

[HIGH/OBSERVED — all three CAMs carved + decoded; opcode→name from the shipped
`neuron_maverick_arch_isa` `aws_neuron_isa_tpb_common.h` OPCODE enum.] MARIANA DVE
arming `{0x30, 0x41…0x53, 0x5e, 0x64, 0x65, 0x6a, 0x6b, 0x70, 0x71, 0x72, 0x77,
0x78, 0x83, 0x84, 0x8e, 0x93, 0x98, 0x9f, 0xa0…0xab, 0xb1, 0xb2, 0xe0…0xe3}` — note
**no `0x23`/`0x25`**.

The exact MAVERICK-DVE-vs-MARIANA-DVE armed delta (`+10/−3`):

```text
ADDED (+10):
  0x23 ACTIVATION_TABLE_LOAD   ← an ACT opcode, now profiled on DVE  ★ the fold
  0x25 ACTIVATE2               ← an ACT opcode, now profiled on DVE  ★ the fold
  0x58 MAX_POOL_SELECT
  0x61 BATCH_NORM_STATS2       (replaces deprecated 0x60 BATCH_NORM_STATS)
  0x62 BATCH_NORM_AGGREGATE
  0x6c MAX8
  0x6d MATCH_VALUE_LOAD
  0x6e FIND_INDEX8
  0x6f MATCH_REPLACE8
  0x99 CAST_PREDICATED
REMOVED (−3, deprecated on v5 per the enum comments):
  0x64 BATCH_NORM_PARAM_LOAD       ("use BatchNormParamLoad2 instead")
  0x70 TENSOR_SCALAR_IMM_LD_ARITH  ("ucode exists, not maintained/used")
  0x71 TENSOR_SCALAR_IMM_LD_BITVEC
```

The presence of `0x23 ACTIVATION_TABLE_LOAD` + `0x25 ACTIVATE2` on the DVE CAM
(absent from MARIANA DVE) is the **image-level smoking gun for the ACT-into-DVE
fold**: ACT-class opcodes are now armed/profiled on the DVE engine, even though
they no longer have a separate named SEQ handler image. The rest of the delta is a
coherent v5 re-spec — deprecate old batch-norm/imm-load variants, profile the new
batch-norm-stats2/aggregate + the max8/match/find-index cluster + cast-predicated.
This is the image shadow of the v5 OPCODE-roster growth (`159→165`). [opcode names +
arming OBSERVED; the "fold" reading INFERRED-HIGH, corroborated by the address map]

> **GOTCHA — do NOT conflate the PROF `0x61` with the nonblocking-cmd `0x61`.** The
> DVE PROF arms `0x61 BATCH_NORM_STATS2`. The v5 NC-v5 wait-mode re-model uses a
> *separate* enum space (`READ_SEMAPHORE=0x60`/`READ_EVENT=0x61` as
> nonblocking-command codes — a header fact, not the OPCODE space). The PROF CAM
> indexes OPCODE; these are different number lines that happen to overlap. [NOTE]

> **CORRECTION — the 16-byte PROF CAM is NOT the 32-byte activation-LUT CAM.** The
> PROF `0x400` opcode-arming table here is the **HW-decode profiler** config, a
> distinct artifact from the 32-byte activation-LUT bucket table that the ACT
> control path used. The fold relocates *opcode profiling* onto DVE; it does not
> imply the LUT machinery is a DVE sub-table at this layer. [CORRECTION, carried
> from the PROF-format reference]

---

## 5. The ACT handler diff vs MARIANA_PLUS — the key question, answered

There is no MAVERICK ACT image to diff. The meaningful question is *"where did the
MARIANA_PLUS ACT 29-handler set GO on v5?"* — and the answer is **NOT** "merged
verbatim into DVE." Three findings:

**5a. ACT-specific handlers — GONE firmware-wide.** Region-wide string search over
the entire MAVERICK blob range (`0x871300..0x9aaea0`), glue-tolerant:

```text
S: Activate                 0      S: ActivateQuantize          0
S: Activate2                0      S: ActivationTableLoad       0
S: ActivationReadAccumulator 0
```

The MARIANA_PLUS ACT activation-engine-specific handlers
(`Activate`/`Activate2`/`ActivateQuantize`/`ActivationTableLoad`/
`ActivationReadAccumulator`) are **absent as named SEQ handlers anywhere in
MAVERICK** — the exact opposite of MARIANA_PLUS's `+0/−0` ACT diff. [HIGH/OBSERVED]

**5b. The MAVERICK DVE handler roster is DVE-NATIVE, not a DVE+ACT union.** Strict
end-anchored extraction (`rg -oP 'S: \K[A-Za-z][A-Za-z0-9_/-]*$' | sort -u`, the
MARIANA_PLUS method-2): **MAVERICK DVE = 59** vs **MARIANA_PLUS DVE = 60**. The only
strict difference is `QuantizeMx` — the **named handler is DROPPED** on MAVERICK (0 hits region-wide),
**not** migrated; the `0xe3 QUANTIZE_MX` opcode stays DVE-bound (armed on the MAVERICK DVE PROF CAM),
and POOL's only MX surface is the pre-existing `0x7b TENSOR_DEQUANTIZE` dequant path (§6,
[maverick-pool.md §2.2](maverick-pool.md)). The roster is DVE-native:
`BatchNormalize{,BackProp,GradAccum,GradAccum2,ParamLoad,ParamLoad2}`, `Dropout`,
`SparsityCompress{,Tag}`, `MatchReplace`, `MatchValueLoad`, `FindIndex8`,
`RangeSelect`, `Exponential`, `CastPredicated`, `CopyPredicated{,Reduce,Scalar}`,
**`DveReadAccumulator`**, `DveReadIndices`, the `TensorScalar*`/`TensorTensor*`
family, `Rand2/RandGetState/RandSetState`, plus the shared SEQ control core
(`AluOp/MOVE/WRITE/NOTIFY/BRANCH/SET_OM/POLL_SEM/NOP/EngineNop/Event_Semaphore/
INS_*/EXT_BREAK/Halt/Redirect/STRONG_ORDER/TensorLoad/TensorStore`). [HIGH/OBSERVED]

**5c. The ACT read-accumulator survives as `DveReadAccumulator` (`0x9b`) — the
rename, not the ACT handler.** The MAVERICK region carries `@S: DveReadAccumulator`
(string-pool glue prefix `@S:`, as the baselines documented), and the shipped enum
defines `NEURON_ISA_TPB_OPCODE_DVE_READ_ACCUMULATOR = 0x9b`. The functional
"read the activation accumulator" capability is **re-expressed under DVE dispatch
with a DVE-native name** — it is *the fold realized as a rename*, NOT the ACT
`ActivationReadAccumulator` handler (which appears 0× firmware-wide, §5a). This is
the precise sense in which **ACT→DVE is rename / re-expression, not a functional
merge**: the datapath is re-homed under DVE's roster, not collapsed into a vector
op. [HIGH/OBSERVED string + enum; the re-expression reading INFERRED-HIGH]

**Net ACT handler diff vs MARIANA_PLUS ACT:** REMOVED = the entire ACT-specific set
(absent firmware-wide); ADDED-as-DVE = none (DVE is its own engine; the `DveRead*`
handlers are DVE-native, not ACT handlers renamed *into* the DVE roster). This is
the EXACT OPPOSITE of MARIANA_PLUS's `+0/−0`. The "diff" is structural: the ACT
engine ceased to exist as a firmware image; its opcodes moved onto DVE
(PROF-confirmed §4). [HIGH/OBSERVED]

---

## 6. Per-subsystem deltas — dtype, DGE, PROF, wait-mode

**6a. dtype — numeric in the NX sequencer; the MX machinery is in the Q7 POOL.**
`FP8_EXP2 / INT4 / SFP8 / MXTENSOR / MX_PERF / TILE_SIZE / FP4_EXP2 / CPTC /
QuantizeMx` (named strings) = **0** across the whole MAVERICK NX region
(DVE/PE/POOL/SP) — the v5 dtype superset stays numeric in the sequencer, exactly as
MARIANA_PLUS. The MX / sub-byte **dequant** machinery lives in the MAVERICK
`Q7_POOL` GPSIMD kernel (`P%i:` prefix): `proc_4bit_non_mx` / `proc_4bit_mx_8` /
`dequant`/`Dequant` (the `group_size==8 → MX micro-scaling` model). Both RNG algos
(`Xorwow*` + `Lfsr*`) are present in the Q7 kernel. So the v5 dtype additions are
real at the image level, realized in the Q7 POOL dequant path — **not** as
NX-sequencer dtype strings. [HIGH/OBSERVED]

**6b. DGE fast-path — DROPPED, not retained from v4+.** The MARIANA_PLUS DGE
reshape fast-path (`dge_decode_fast` ×10, `dge_reshape_memcopy_transpose_fast` ×10,
`dge_backend_rtl` ×20, `wait_for_credit` ×10, `analyze_tensor_reshape` ×10) occurs
**0×** in MAVERICK (only a single `tensor_reshape_transpose_sb2sb` survives;
`dge_shape` logging remains). The v4+ SEQ-side DGE fast-path subsystem was *not*
carried forward — consistent with the address-map's MAVERICK DGE = 512 KiB stub +
the DDMA/CDMA/UDMA hardware-DMA rename. This is part of why MAVERICK DVE is
**smaller** than MARIANA/MPLUS DVE (§7). [HIGH/OBSERVED counts; "re-architected to
HW DMA" reading INFERRED-HIGH from the address map]

**6c. PROF — re-authored, not reused (§4).** Where MARIANA_PLUS reused MARIANA's
per-engine PROF CAM byte-identical, MAVERICK DVE PROF (`dbff2b84`, 53 armed) is
distinct from MARIANA/MPLUS DVE (`ca588683`, 46 armed). The HW-decode profiler
config was **re-authored** for v5 — a direct gen-step indicator. [HIGH/OBSERVED]

**6d. wait-mode re-model — no ACT-visible firmware-log evidence (expected).** The
MAVERICK NX images carry only `STRONG_ORDER` (shared all gens); no `UNORDERED /
nonblocking / REG_OFFSET / WAIT_MODE / UPDATE_MODE / EVT_SEM` log strings. The
NC-v5 wait-mode divergence is an **ISA-header** fact; firmware does not name its
wait modes, so the image layer is silent — neither confirming nor refuting. This is
correctly out of the image-string scope. [HIGH/OBSERVED absence]

---

## 7. Code-body evidence + cross-gen size — a genuine v5 build, *smaller* than v4+

The MAVERICK DVE PERF IRAM is a **genuine** Q7/NX windowed-ABI SEQ sequencer
(decoded with `ncore2gp`): `107 entry / 134 retw / 340 call8 / 153 call0 /
2077 const16 / 1641 l32r` — a real separately-compiled sequencer, not a stub. The
vector datapath is partly FLIX-bundle-desynced by the linear sweep (the FLIX-desync
limitation); the windowed-ABI control spine decodes cleanly. [HIGH/OBSERVED]

Cross-gen DVE size — MAVERICK is **smaller** (the DGE-drop shrink, *opposite* the
v4→v4+ growth):

| IMAGE | MARIANA | MARIANA_PLUS | MAVERICK | Δ (MAV vs MARIANA) |
|---|---|---|---|---|
| DVE PERF_IRAM | `0x13540` | (recompiled) | `0x0ec00` | `−0x4940` |
| DVE DEBUG_IRAM | `0x1c560` | `0x1d760` | `0x19000` | `−0x3560` |
| DVE PERF_DRAM | `0x031a0` | (recompiled) | `0x02740` | `−0xa60` |
| DVE DEBUG_DRAM | `0x07000` | `0x07160` | `0x05f80` | `−0x1080` |

[HIGH/OBSERVED — getter `movq` + `nm`.] The shrink is the dropped DGE fast-path bulk
(§6b) + ACT folded out as a separate image — the **inverse** of MARIANA_PLUS's IRAM
*growth*. And it is an **independent v5 build**, not a recompile of MPLUS: MAVERICK
DVE PERF IRAM vs MARIANA_PLUS DVE PERF IRAM share a **1-byte** prefix (diverge at
the reset immediate `75` vs `7d`) with only **6.1%** positional 16-byte block
similarity — contrast MARIANA_PLUS-vs-MARIANA ACT's `0x212`-byte shared prefix +
identical reset (a recompile-relocation). [HIGH/OBSERVED]

> **NOTE — `coretype 37` / `arch_id 36`.** `coretype = 37` is OBSERVED
> (`maverick_libs` @`0x9b9050` via `nm`; the `ct=37` dispatch + bit 37 in the
> `get_num_ext_isa_libs` mask `0x2020202000 = {13,21,29,37}`). `NEURON_CORE_VERSION`
> tops at `V5`. `arch_id = 36` is **INFERRED** (`coretype = arch_id + 1` across the 4
> lower gens; **no NCFW v5 image exists** to confirm — never binary-observed; mark
> it `36*`). [coretype/version HIGH/OBSERVED; `arch_id 36*` MED/INFERRED — WALL]

---

## 8. What this sets up for the MAVERICK matrix

MAVERICK is a **real new generation** at the image level, not a recompile. Where
MARIANA_PLUS-vs-MARIANA was "recompile + a DGE fast-path + a register-map refresh,
`+0/−0` handler/opcode," MAVERICK-vs-MARIANA_PLUS is structurally and functionally
divergent — the v5 image deltas (each OBSERVED): (1) **no standalone ACT image**
(the ACT→DVE fold, the biggest delta); (2) a **new reset geometry** (`j 0x1d8`,
`enter_run@0x94`, base `0x1490`, `−0x20`); (3) a **re-authored PROF** (`+10/−3`);
(4) the **DGE fast-path dropped**; (5) the **MX dtype machinery relocated** to the
Q7 POOL; (6) **DEBUG dropped** for PE/POOL/SP; (7) **internal-twin-exclusive**
(0 `.a` members); (8) an **independent v5 build** (6.1% block-sim).

**Expectation for the rest of the MAVERICK matrix** (DVE deep-dive / PE / POOL /
SP / Q7): each MAVERICK engine should be a genuinely new v5 build (`j 0x1d8` reset,
`BEGIN on maverick`, re-authored PROF, no DGE fast-path, PERF/TEST-only for
PE/POOL/SP, DEBUG only on DVE+Q7), NOT a MARIANA_PLUS recompile; opcode space grown;
MX dtype on the Q7 POOL. **All v5-interior generalizations here are `INFERRED`
from the DVE instance — to be VERIFIED per-engine.** [INFERRED-HIGH — WALL]

---

## 9. Adversarial self-verification

The five strongest claims, challenged against the binary:

1. **No standalone MAVERICK `NX_ACT` image.** *Challenge:* a carve gap could fake an
   absence. *Evidence:* `nm | rg -c 'MAVERICK.*NX_ACT'` = **0**; case-insensitive
   `maverick`+`act` symbols = **0**; `.rodata` strings = **0**; the IDA `_names.json`
   sidecar lists **0** `MAVERICK_NX_ACT_*` (independent of `nm`). Four zeros across
   two tools. The 62 getters resolve to DVE/PE/POOL/SP/Q7 with no ACT family.
   **HOLDS.** [HIGH/OBSERVED]
2. **The ACT opcodes are armed on the DVE PROF CAM.** *Challenge:* `0x23`/`0x25`
   might be generic DVE opcodes, not the fold. *Evidence:* the comparison is
   **differential** — `0x23`/`0x25` are armed on MAVERICK DVE (`dbff2b84`) and
   **absent** from both MARIANA DVE and MARIANA_PLUS DVE (`ca588683`, byte-identical
   to each other). The shipped enum names them `ACTIVATION_TABLE_LOAD`/`ACTIVATE2` —
   ACT opcodes. Their *appearance* on DVE PROF is the fold. **HOLDS.** [HIGH/OBSERVED]
3. **The read-accumulator is re-expressed as `DveReadAccumulator` (`0x9b`).**
   *Challenge:* maybe `ActivationReadAccumulator` is just glue-hidden, not renamed.
   *Evidence:* glue-tolerant search → `ActivationReadAccumulator` = **0**
   firmware-wide, while `@S: DveReadAccumulator` **is** present, and the shipped enum
   defines `DVE_READ_ACCUMULATOR = 0x9b`. The capability survives under a DVE-native
   name. **HOLDS.** [HIGH/OBSERVED string + enum]
4. **ACT→DVE is rename / re-expression, NOT a functional merge.** *Challenge:* the
   fold could mean ACT collapsed into a DVE vector op. *Evidence:* the MAVERICK DVE
   strict roster is **59** DVE-native handlers (vs MARIANA_PLUS DVE 60; only
   `QuantizeMx` differs) — `BatchNorm*`/`Dropout`/`FindIndex8`/`MatchReplace`/
   `Exponential`/`DveRead*`, **not** a DVE+ACT union; no `Activate*` anywhere. The
   datapath is re-homed under DVE dispatch + renamed, not merged into one op.
   **HOLDS** (roster OBSERVED; framing CARRIED + corroborated). [HIGH]
5. **MAVERICK (v5) interiors are HEADER-OBSERVED only.** *Challenge:* did any
   v5-interior claim get stated as fact? *Evidence:* the file-absence, the
   ACT-opcodes-on-DVE-PROF arming, the `DveReadAccumulator` re-expression, the
   reset bytes, the gen-boundary, and the size/build deltas are all byte-grounded
   `OBSERVED`. The per-opcode→handler row binding, the activation control-flow under
   the fold, and `arch_id 36*` are tagged **`INFERRED`**; the wait-mode re-model is
   declared out of image scope. **HOLDS — the wall is respected.** [WALL]

**Honesty ledger.** *HIGH/OBSERVED:* `internal.so b7c67e89` + `.a 158dadc5`
hashes (MATCH); 0 `MAVERICK_NX_ACT` symbols (×4 zeros incl. IDA sidecar); 0
MAVERICK `.a` members (435 = 124×3 + 48 + 0 = **420 image** + **15 framework** = 435); 62 getters (DVE 14 / PE 10 / POOL 10 /
SP 8 / Q7 20); 8 DVE blobs carved + sha256'd; gen-boundary byte-exact
(`0x86f300+0x2000 == 0x871300`); reset `j 0x1d8 / j 0x1e4` decoded with `ncore2gp`,
`enter_run@0x94`, base `0x1490`, `−0x20` shift; DRAM magic `0x6099cb34`
unchanged; DVE PROF CAM `dbff2b84` (53 armed) vs MARIANA/MPLUS `ca588683` (46),
`+10/−3`, `0x23`/`0x25` armed on DVE & absent from MARIANA DVE; ACT-specific
handlers 0× firmware-wide; `DveReadAccumulator`/`0x9b` present in enum + roster; DVE
strict roster 59 vs 60 (only `QuantizeMx`); MX dequant + both RNG algos in Q7_POOL;
DGE fast-path 0 vs 10–20; DVE smaller; 6.1% block-sim (independent build);
`BEGIN on maverick`; ISA enum `ACT=1` logical; `NEURON_CORE_VERSION V5`; `coretype
37`. *MED/INFERRED:* `arch_id 36*` (no NCFW v5); the "ACT-into-DVE fold" as *cause*
of the absent image (image absent + ACT opcodes on DVE PROF OBSERVED; the fold
reading corroborated by the address map); "DGE re-architected to HW DMA"; the
per-engine v5 generalizations for the rest of the matrix; the per-opcode→handler
row binding on the v5 DVE dispatch (FLIX-desync frontier). *LOW / NOT CLAIMED:*
whether the runtime ever LOADS MAVERICK; the NC-v5 wait-mode image effect
(header-only); the exact activation control-flow under the fold; `Activate2` vs
`Activate` semantics. *N/A:* nothing fabricated; no vendor source snapshot
referenced — all facts derive from the shipped binaries + the shipped `ncore2gp`
toolchain + the shipped clean ISA headers (DMCA 1201(f) interoperability).
