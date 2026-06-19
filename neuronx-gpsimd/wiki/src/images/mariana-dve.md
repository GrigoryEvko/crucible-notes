# MARIANA × DVE image

This page is the **cross-generation diff** of the **MARIANA × DVE** firmware image (NC-v4)
against the committed [CAYMAN × DVE](cayman-dve.md) baseline (NC-v3). It does **not**
re-derive the unchanged DVE engine model — that is the job of the CAYMAN page, which this
page treats as the reference. Read it first; everything below is the *delta*.

The headline verdict, proved instruction-exact: **MARIANA ships a separate, dedicated
`NX_DVE` image — the same `cayman/seq/` SEQ-style ASCII-opcode dispatch engine as CAYMAN
DVE, recompiled for v4.** Five facts carry the diff:

1. The reset vector shifted **`+0x1c`** (`j 0x1dc` → `j 0x1f8`), the *same* shift the
   [MARIANA × ACT](mariana-act.md) survey found — a whole-IRAM relocation, not a model change.
2. The handler roster gains **+7 / −0** (`Rand*` ×3, `Sparsity*` ×2, `QuantizeMx`,
   `Exponential`); all 28 CAYMAN DVE-only handlers are retained byte-for-name.
3. **Batch-norm is unchanged** at the handler/opcode layer — the 6-name set and its raw
   opcode bindings are gen-stable.
4. **No folded activation** — MARIANA DVE carries **zero** `Activate*` handlers; the
   ACT→DVE fold is a [MAVERICK](maverick-dve.md)-only event.
5. **No MARIANA DVE image is byte-identical to its CAYMAN counterpart** (8/8 shas differ) —
   a full per-generation recompile, re-armed profiler, and opcode-space growth `170 → 187`.

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md):
**HIGH/MED/LOW** × **OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a
carve re-derived this session from `libnrtucode_internal.so` and the CAYMAN baseline
re-carved + re-hashed for an apples-to-apples diff (all 5 CAYMAN anchor shas re-confirmed).
Disassembly uses the native `ncore2gp` `xtensa-elf-objdump`.

> **NOTE — provenance.** Every fact derives **solely** from static analysis of the shipped
> binaries with stock binutils (`nm`/`objdump`/`readelf`/`ar`/`objcopy`/`dd`/`xxd`/
> `strings`/`cmp`/`sha256sum`) and the Cadence Xtensa toolchain (`xtensa-elf-objdump`,
> `XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa Tools 14.09). All naming
> derives from the DEBUG build's own embedded `S:` format strings and the firmware's
> baked-in source-path assertion strings. Lawful interoperability reverse engineering
> (DMCA 17 U.S.C. 1201(f)).

---

## 1. The delta at a glance — CAYMAN DVE → MARIANA DVE

Everything that **changed** across the NC-v3 → NC-v4 step. Unchanged structure (the SEQ
dispatch model, the dual HW-Decode/Sunda tables, the ErrorHandler arms, the flat
IRAM/DRAM packaging, the `engine_idx=3` identity boot) is documented once on
[cayman-dve.md](cayman-dve.md) and not re-derived here.

| property | CAYMAN DVE (baseline) | MARIANA DVE (this page) | Δ |
|---|---|---|---|
| reset vector | `j 0x1dc` (`06 76 00 00`) | `j 0x1f8` (`06 7d 00 00`) | **+0x1c** |
| 2nd vector | `j 0x1e8` → `halt 0` (`86 77 00 00`) | `j 0x204` → `halt 0` (`86 7e 00 00`) | **+0x1c** |
| boot-stub body @`0xc` | `a0 71 69 80 …` | `a0 71 69 80 …` | identical (byte-for-byte = MARIANA ACT) |
| dispatch normalization | `addi a2,a2,-65` (opcode − `0x41` `'A'`) | `addi a2,a2,-48` (opcode − `0x30` `'0'`) | base `'A'`→`'0'` (extend down) |
| dispatch bound | `movi a3,169` → **170** entries | `movi a3,186` → **187** entries | **+17** |
| opcode range | `0x41..0xea` | `0x30..0xea` | low end −17 |
| table1 base (HW-Decode) | DRAM `0x80814` | DRAM `0x80800` | relocated |
| table2 base (Sunda) | DRAM `0x80abc` | DRAM `0x80aec` | relocated |
| real / default per table | 75 real / 95 default | 82 real / 105 default | **+7 real** |
| `S: Dispatch opcode` log | DRAM `0x80de0` | DRAM `0x80e58` | relocated |
| DVE-only handlers | 28 | 28 + **7 new** | +7 / −0 |
| folded activation | none | **none** (Maverick-only) | unchanged → see §6 |
| batch-norm handler set / bindings | 6 / FW-37 ops | 6 / **identical bindings** | unchanged → see §5c |
| dtype named strings | `UINT32/INT32/FP32` only | `UINT32/INT32/FP32` only **+ `QuantizeMx`** handler | +1 MX footprint |
| PROF_CAM | `8fd7e422` — shared 4/4 across NX engines | `ca588683` — **per-engine** (≠ ACT) | re-armed (47→48) |
| PROF_TABLE | `ce761f81` — header `0x00000201` | `d72b339f` — header **zeroed** | re-preallocated |
| PERF/TEST IRAM size | `0x15c20` / `0x15840` | `0x13540` / `0x13560` | **−0x26e0 / −0x22e0** (~−11%) |
| DEBUG IRAM / DRAM size | `0x1bcc0` / `0x6d60` | `0x1c560` / `0x7000` | +0x8a0 / +0x2a0 |
| boot-time errata log | — | `S: Applying patch for mariana-4062` | **new** |
| engine self-name | `S: BEGIN on cayman` | `S: BEGIN on mariana` | gen-renamed |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` | **unchanged** |

`[HIGH/OBSERVED — every Δ row re-confirmed this pass; CAYMAN column re-carved from the
baseline (5/5 anchor shas MATCH), MARIANA column carved fresh (8/8 shas); per-row anchors in
§2–§9]`

> **NOTE — what a CAYMAN→MARIANA DVE swap actually is.** A recompile + opcode-space growth
> (`+7` handlers / `+17` index slots) + a re-armed, now-engine-specific HW-decode profiler +
> a relocated code layout (reset vector and dispatch tables shifted). The dispatch
> **mechanism**, reset-vector **form**, ErrorHandler arms, `cayman/seq/` source tree, and
> SEQ infra are **invariant**. It is *not* a model change. `[HIGH/OBSERVED]`

---

## 2. Carve provenance and the cross-gen size/sha table

The 14 `MARIANA_NX_DVE_*_get` accessors resolve identically to CAYMAN's getter scheme
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`; 8 real + 6 zero-size
SRAM/EXTRAM boundary cursors aliasing the next-engine `NX_PE` IRAM). The container is the
same identity-mapped `libnrtucode_internal.so`
(`sha256 b7c67e89…632fc329b`, re-hashed this pass = MATCH), so carve = a plain `dd` at the
`.rodata` VA. The getter-body immediates were re-decoded this pass and agree with the carve
geometry — e.g. the DEBUG IRAM stub at `.text 0x9b4220`:

```asm
9b4220:  lea  -0x5ab267(%rip),%rax   # 0x408fc0  MARIANA_NX_DVE_DEBUG_IRAM_get.data
9b4227:  mov  %rax,(%rdi)
9b422a:  movq $0x1c560,(%rsi)        ; size
```

`[HIGH/OBSERVED — 14/14 getters `nm`-resolved at `.text 0x9b3d20..0x9b4880`; img-ptr/size
immediates decoded; the PERF_IRAM/PROF_CAM/PROF_TABLE stubs likewise decode to
`0x31f5c0/0x13540`, `0x59e880/0x400`, `0x59ec80/0x2000`]`

> **GOTCHA — these getters are local (`t`) symbols, not exported.** `nm -D` returns **zero**
> `MARIANA_NX_DVE_*` matches — the accessors are plain-`nm` local-text symbols. A
> reimplementer enumerating the image catalog must use `nm` (not `nm -D`/`objdump -T`).
> `[HIGH/OBSERVED — `nm -D` = 0, `nm` = 14]`

**Cross-gen sha256 table (DVE)** — the carve target each forward row is diffed against. The
CAYMAN column was re-carved + re-hashed this task (5/5 anchor shas re-confirmed vs the
committed baseline); the MARIANA column is fresh. **No row is identical.**

| IMAGE | CAYMAN DVE (16) | MARIANA DVE (16) | identical? |
|---|---|---|---|
| DEBUG_IRAM | `259769ff…` | `4c75ba8e…` | NO (recompile) |
| DEBUG_DRAM | `c106642d…` | `6f65a629…` | NO |
| PERF_IRAM | `9fa066f4…` | `dd14c1e3…` | NO |
| PERF_DRAM | `eb980f98…` | `79b7690b…` | NO |
| TEST_IRAM | `3e83b0f4…` | `78922240…` | NO |
| TEST_DRAM | `5efcb3eb…` | `59f60fbb…` | NO |
| PROF_CAM | `8fd7e422…` | `ca588683…` | NO (re-armed, §7) |
| PROF_TABLE | `ce761f81…` | `d72b339f…` | NO (re-preallocated, §7) |

`[HIGH/OBSERVED — all 8 MARIANA shas re-computed this pass; full digests e.g. DEBUG_IRAM
`4c75ba8ea14eb6a1161996f0e2d10a7b81b025ee07a901f59e03b0f7c3ff7e11`]`

Each MARIANA carve is byte-identical (sha256) to the matching `libnrtucode.a` member
`.rodata` (8/8) — `ar` lists exactly 14 `MARIANA_NX_DVE` members (12 `img_*`, 2
`hwdecode_*_PROF_{CAM,TABLE}`), and `objcopy --only-section=.rodata` reproduces each carve.
`[HIGH/OBSERVED — 8/8 carve↔archive reconcile; `ar t` member set enumerated]`

**Variant size/string delta.** Same DEBUG-vs-PERF-vs-TEST model as CAYMAN (only DEBUG
carries the `S:` runtime logs; PERF strips all; TEST keeps file/function symbols):

| GEN | VAR | IRAM | DRAM | `S:` strings | total strings | IVP-distinct (PERF/TEST) |
|---|---|---:|---:|---:|---:|---:|
| CAYMAN | DEBUG | `0x1bcc0` | `0x6d60` | 182 | 285 | — |
| CAYMAN | PERF | `0x15c20` | `0x2fc0` | 0 | ~16 | 317 |
| CAYMAN | TEST | `0x15840` | `0x32c0` | 0 | ~61 | 319 |
| MARIANA | DEBUG | `0x1c560` | `0x7000` | **190** | 296 | — |
| MARIANA | PERF | `0x13540` | `0x31a0` | 0 | 16 | 293 |
| MARIANA | TEST | `0x13560` | `0x34e0` | 0 | 62 | 290 |

`[HIGH/OBSERVED — sizes from getter immediates; string/IVP counts re-derived this pass]`

> **QUIRK — PERF/TEST IRAM got *smaller* despite +7 handlers.** MARIANA PERF IRAM is
> `0x13540` vs CAYMAN `0x15c20` (**−0x26e0, ~−11%**); TEST `0x13560` vs `0x15840`
> (**−0x22e0**). A tighter v4 compile/schedule more than absorbs the new handlers' code.
> DEBUG IRAM grows slightly (`+0x8a0`) and **all** DRAM grows (`+0x2a0/+0x1e0/+0x220`) — the
> `+8` new `S:` strings (the 7 handler logs + the `mariana-4062` errata log) account for the
> DEBUG-DRAM growth. MARIANA PERF_IRAM (`dd14c1e3`) is a *distinct byte-build* from CAYMAN
> (`9fa066f4`) — a full recompile, not a patch. `[HIGH/OBSERVED]`

---

## 3. The reset-vector shift (+0x1c) — the decisive cross-gen fingerprint

**First 12 bytes, byte-identical across all 3 MARIANA DVE IRAMs (DEBUG/PERF/TEST):**

```
00000000:  06 7d 00 00  00 00  86 7e 00 00  00 00
   0x000:  06 7d 00     j 0x1f8      ; primary reset vector -> boot path
   0x006:  86 7e 00     j 0x204      ; secondary vector -> halt trap
```

versus the CAYMAN DVE baseline (re-read this pass):

```
00000000:  06 76 00 00  00 00  86 77 00 00  00 00
   0x000:  06 76 00     j 0x1dc      ; CAYMAN primary
   0x006:  86 77 00     j 0x1e8      ; CAYMAN secondary
```

The `j` displacement bytes (byte 1 of each `j`) are the *only* difference: `76 → 7d` and
`77 → 7e`. Both boot targets moved **`+0x1c`** (`0x1f8 − 0x1dc = 0x1c`;
`0x204 − 0x1e8 = 0x1c`) — the **identical** `+0x1c` shift seen on MARIANA ACT. Decoded boot
path (MARIANA DVE DEBUG IRAM, `ncore2gp` objdump):

```asm
1f8:  const16 a0, 0          ┐ a0 = C enter_run prologue VA (0x90)
1fb:  const16 a0, 0x90       ┘
1fe:  jx      a0             ; jump to the enter_run prologue
…
204:  halt    0             ; 2nd vector = HALT trap
```

> **GOTCHA — the shift is a *relocation*, not a recoded boot scheme.** The boot-stub **body**
> at byte `0xc` (`a0 71 69 80 2a 29 a0 3a 29 90 e1 69 …`) is **byte-identical** between
> MARIANA DVE and MARIANA ACT (`cmp 0xc..0x40` IDENTICAL) **and** matches CAYMAN DVE's body
> byte at `0xc` — only the `j` displacement (byte 1) moved. A reimplementer must treat the
> reset vector as a *relocated entry into the same boot scheme shared across all MARIANA NX
> engines*, not a new boot path. The `const16 a0,0x90` `enter_run` target is itself unchanged;
> the `+0x1c` is the layout shift of the boot code that *precedes* it. `[HIGH/OBSERVED —
> reset vector read on all 3 DVE variants + the body `cmp` against MARIANA ACT]`

**Engine-identity boot is invariant.** The DRAM still carries
`S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`; the
same flat image loads on any engine slot and derives `engine_idx = 3` (DVE) at boot.
`[string HIGH/OBSERVED; runtime-compute INFERRED-HIGH — identical mechanism to cayman-dve.md §3]`

**Disassembly proof** (`ncore2gp` objdump, **exit 0, empty stderr, 46,162 lines** for DEBUG
IRAM): the MARIANA DVE IRAMs decode to real Q7/NX windowed-ABI + FLIX-VLIW + IVP vector code
— PERF carries **293** distinct IVP mnemonics (TEST 290; DEBUG's linear sweep recovers fewer
under FLIX desync, the same artifact CAYMAN noted). The DVE NX core still carries a **full
vector compute datapath**. `[HIGH/OBSERVED — exit-0 disasm; IVP counts re-derived]`

---

## 4. The dispatch delta — opcode space grew 170 → 187, base 0x41 → 0x30

MARIANA DVE uses the **same** SEQ dispatch model CAYMAN §4 decoded (opcode-byte
normalization → direct-indexed DRAM jump table, dual HW-Decode/Sunda sites). Only the
*constants* moved. Decoded instruction-exact at the two DEBUG-IRAM sites:

```c
/* MARIANA DVE dispatch core — DEBUG IRAM 0x2efe..0x2f20 (HW-Decode site).
 * Sunda-mode site at 0x3715..0x372c is identical bar the table base.
 * DELTA vs cayman-dve.md §4: normalize base 0x41->0x30, bound 169->186,
 *   tables 0x80814/0xabc -> 0x80800/0xaec, log 0x80de0 -> 0x80e58. */
static void dve_dispatch(uint32_t opcode_word /* [a1+8] */)
{
    dbg_log("S: Dispatch opcode=0x%x", opcode_word);  /* const16 a10,0xe58 ; call8 0x188a4 */

    int idx = opcode_word - 0x30;        /* addi a2,a2,-48   ('0'->0, was -65 / 'A') */
    if ((unsigned)idx > 186)             /* movi a3,186 ; bgeu a3,a2  -> bound 187 (was 169/170) */
        goto bad_opcode;                 /* j 0x31e3  (HW-decode default arm; CAYMAN 0x3207) */

    uint32_t target = dispatch_table[idx];   /* const16 a3,0x800 ; addx4 a2,a2,a3 ; l32i.n */
    goto *target;                            /* jx a2 -> trampoline -> handler */

bad_opcode:
    error_handler_bad_opcode(opcode_word);   /* "S: ErrorHandler : Bad Opcode(0x%x)" */
}
```

**SITE A** (HW-Decode, `0x2f09`): `addi a2,a2,-48 ; movi a3,186 ; bgeu a3,a2,0x2f15 ;
j 0x31e3 ; const16 a3,0x800 ; addx4 a2,a2,a3 ; l32i.n a2,[a2] ; jx a2`. **SITE B** (Sunda,
`0x3715`): identical except `const16 a3,0xaec` (second table) and `j 0x39fa` (Sunda default).
Byte-search cross-check: `movi a3,186` appears **2×** in DEBUG (the two sites);
`movi a3,169` appears **0×**. `[HIGH/OBSERVED — both sites disassembled instruction-exact;
`const16 a3,0x800` @0x2f18, `const16 a3,0xaec` @0x3724; the movi byte-search]`

**Per-opcode table delta** (decoded both DEBUG DRAM tables; *default* = out-of-range arm,
*real* = a distinct handler trampoline):

- **NEW real opcodes** (real in MARIANA, default in CAYMAN): **`0x30, 0x77, 0x78, 0xe0,
  0xe1, 0xe2, 0xe3`** — exactly **+7**.
- **REMOVED real opcodes**: **NONE (0)**.

The opcode space grew by **extending DOWN** from `'A'`(`0x41`) to `'0'`(`0x30`) (+17 index
slots) and **arming 7 new real entries**; the `'A'`+ block is gen-stable. The two parallel
tables (HW-Decode `0x800`, Sunda `0xaec`) both carry **82 real / 105 default**.
`[HIGH/OBSERVED]`

> **NOTE — three independent signals agree on +7.** The **+7 new handler names** (§5a) ==
> the **+7 new real dispatch opcodes** (`0x30/0x77/0x78/0xe0–0xe3`) == the **table real-count
> delta** (`82 − 75 = +7`). When three orthogonal measurements converge, the `+7/−0` handler
> delta is HIGH, not inferred. `[HIGH/OBSERVED — three agreeing signals]`

> **GOTCHA — the clean dispatch decode is in DEBUG only.** The PERF/TEST IRAM dispatch
> desyncs under FLIX-VLIW bundling in the linear sweep (the `movi a3,186` / `addi -48` don't
> byte-match there — the same artifact CAYMAN/MARIANA-ACT noted; note `addi a2,a2,-48` also
> appears once *outside* dispatch at `0x18c51`, so match the full site, not the lone `addi`).
> All HIGH facts above (187-bound, `0x30` normalization, dual table `0x800/0xaec`, `+7`
> opcodes, log `0x80e58`, ErrorHandler arms) come from the clean DEBUG two-site decode; the
> byte-exact PERF/TEST per-opcode rows are MED. `[HIGH DEBUG decode / MED PERF-TEST per-opcode]`

**Error path (engine-model confirm, unchanged).** The ErrorHandler arms are present
byte-for-byte in MARIANA DVE DRAM — `S: ErrorHandler : Bad Opcode(0x%x)`,
`… Illegal Instruction(0x%x)`, `… FP Error(%d)`, `… Int Div Zero Error`,
`S:/VS: Assertion failure! %s(%s:%u)` — with source paths still naming
`…/cayman/seq/src/handlers/exception_handler.hpp`. Identical fault classes to CAYMAN §4.
`[HIGH/OBSERVED]`

---

## 5. The handler roster diff — +7 / −0, batch-norm unchanged

**Method** is the CAYMAN/MARIANA-ACT glue-stripped set-diff: extract every
`<glue>S: <OpName>` from each DEBUG DRAM, strip the string-pool glued prefix (the 1–3 chars
before `S:` that abut the previous string with no NUL), normalize to the bare `OpName`,
`comm`-diff against the re-carved CAYMAN DVE DEBUG DRAM.

> **GOTCHA — the prefix-glue diff-trap is present on DVE.** A *naive* `^S:`-only grep falsely
> reports several glued handlers as "removed". Verified glued instances this pass:
> `RS: SparsityCompress`, `"TS: QuantizeMx`, `@S: DveReadAccumulator`,
> `FS: RandGetState`, `tS: BatchNormalizeGradAccum`, `VS: MatchReplace`. Match the prefix as
> `[A-Za-z@"]{0,3}S: ` and strip it before comparing. A second trap: that regex also catches
> the **non-handler** errata line `S: Applying patch for mariana-4062` (token `Applying`) —
> exclude it. The glue-stripped, errata-excluded diff gives the true **+7 / −0** delta.
> `[HIGH/OBSERVED — glue bytes read directly; `Applying` false-positive confirmed]`

### 5a. The 7 NEW MARIANA DVE handler tokens (CAYMAN DVE count = 0 for each)

| handler | role | note |
|---|---|---|
| `RandGetState` | RNG-state **read** | POOL-only on CAYMAN (its DVE-absent error blob `rand_algorithm(0x%x) not currently supported on POOL` ships alongside); the **same pair MARIANA ACT also gained** |
| `RandSetState` | RNG-state **write** | ditto |
| `Rand2` | second RNG handler variant | — |
| `SparsityCompress` | sparsity-compression apply | NEW data/vector capability |
| `SparsityCompressTag` | tag-emit companion of the above | — |
| `QuantizeMx` | **MX (microscaling block-format) quantize** | the firmware footprint of the FP4/MX dtype expansion; ACT has **no** `QuantizeMx` (see §7a) |
| `Exponential` | elementwise exponential math op | new source `exponential.cpp` (§5d) |

**REMOVED: NONE (0)** — every CAYMAN DVE handler is retained.
`[HIGH/OBSERVED — each new token byte-verified PRESENT in MARIANA DVE DRAM and ABSENT
(count 0) in the re-carved CAYMAN DVE DRAM]`

Each new handler is a **real** dispatch handler (not a stray string): its `S:` name sits in
the contiguous handler-name string-pool cluster, between established handlers
(`CopyPredicatedReduce`, `RangeSelect`, `DveReadIndices`, …). `RandSetState`'s handler load
was decoded instruction-exact (`const16 a3,0x2f83` at IRAM `0x11da6` — the low half of DRAM
VA `0x82f83`, the `RandSetState` string). `[HIGH names + HIGH opcode-table (§4); the
representative const16↔offset xref HIGH; the full per-handler xref MED under FLIX-desync]`

### 5b. The 28 CAYMAN DVE-only handlers — ALL RETAINED (identical counts)

Documented on [cayman-dve.md §6c](cayman-dve.md); re-confirmed present this pass with
identical counts. Grouped: batch-norm ×6, predicated copy/cast ×4
(`CastPredicated`/`CopyPredicated`/`CopyPredicatedReduce`/`CopyPredicatedScalar`),
DVE-native ×3 (`DveReadAccumulator`/`DveReadIndices`/`Dropout`), match/find/select ×5
(`FindIndex8`/`MatchReplace`/`MatchValueLoad`/`RangeSelect`/`TensorScalarSelect`),
scan/transpose/shuffle ×4, cached/imm tensor-scalar ×6. The shared SEQ control/move + basic
tensor-compute core (`AluOp`/`BRANCH`/`MOVE`/`NOP`/`TensorLoad`/`TensorStore`/`Pool`/
`Tensor-*`/…) is likewise unchanged. `[HIGH/OBSERVED — MARIANA count == CAYMAN count for all
28 + shared core]`

### 5c. Batch-norm change check — UNCHANGED at the handler/opcode layer

This is a **headline negative**: MARIANA did **not** change the DVE batch-norm kernels at
the dispatch layer.

- **Handler set unchanged**: all 6 names present on both gens (`BatchNormalize`,
  `…BackProp`, `…GradAccum`, `…GradAccum2`, `…ParamLoad`, `…ParamLoad2`).
- **Raw opcode→handler bindings unchanged**: both gens map the FW-37 batch-norm opcodes to
  the same handler. Only the trampoline *target* addresses shifted (whole-IRAM relocation):

| opcode | role | CAYMAN target | MARIANA target |
|---|---|---|---|
| `0x61`/`0x62` | Stats2 / Aggregate | `0x308e` | `0x3032` |
| `0x63` | GradAccum | `0x309e` | `0x3042` |
| `0x64` | ParamLoad | `0x30b6` | `0x305a` |
| `0x65` | BackProp | `0x30ae` | `0x3052` |
| `0x8e` | ParamLoad2 | `0x30be` | `0x3062` |
| `0x94` | GradAccum2 | `0x30a6` | `0x304a` |

The opcode *semantics* and the 6-name set are gen-stable; the targets differ only because
the whole IRAM relocated (the same `+0x1c`-class shift). The handler-name string-pool block
also shifted (`+0xE0`, from the new handler strings placed ahead of it).
`[HIGH/OBSERVED — handler/opcode stability]`

> **NOTE — batch-norm *internal math* was NOT re-decoded (MED).** The per-handler IVP body is
> out of this image-survey's scope. The distinct-IVP set differs between gens (MARIANA DVE
> PERF 293 vs CAYMAN 317) and some FW-37 div0/divnn/divn seed ops read 0 in the MARIANA PERF
> linear sweep — but that is **most likely a FLIX-desync artifact** of the linear
> disassembly (these seeds live inside batch-norm handler bodies the sweep desyncs
> differently per build), **not a proven removal**. New IVP mnemonics do appear
> (`ivp_dmulq2n8dxr8`/`ivp_mulqa2n8xr8`/`ivp_mulqan16xr16` dot-product-multiply +
> `ivp_ficeilnxf16`/`ivp_fifloornxf16t`/`ivp_firoundn_2xf32t` ceil/floor/round-to-int),
> consistent with the new `SparsityCompress`/`QuantizeMx`/`Exponential` compute. Per-handler
> batch-norm math equivalence is therefore **not established here** — it would need a
> per-handler CFG decode + ISS oracle as FW-37 did for CAYMAN.
> `[HIGH handler/opcode stability; MED internal-math (not decoded)]`

### 5d. Source-file diff (corroborating)

MARIANA DVE DRAM adds **two** source files absent on CAYMAN DVE: `exponential.cpp` (the
`Exponential` handler) and `addr_bits.hpp` (the **same** new pool/tpb address-rerouting
header MARIANA ACT also gained). All other source files
(`alu_op`/`branch`/`move`/`move_shape.cpp`, `dge_*.cpp`, `fetch`/`cache`/`fsm`/`uarch`/
`pool.hpp`, exception/signal/error handlers) are present on **both** gens.
`[HIGH/OBSERVED — both new source strings present in the SO; CAYMAN-absent]`

---

## 6. No folded activation — the ACT→DVE fold is Maverick-only

**The cross-gen ACT/DVE relationship, OBSERVED.** MARIANA DVE DRAM has **zero** activation
handlers: a grep of all 3 DRAMs (DEBUG/PERF/TEST) for `Activate`, `Activate2`,
`ActivationTableLoad`, `ActivationReadAccumulator`, `ActivateQuantize` returns **0 hits each**.
The standalone MARIANA `NX_ACT` image (carved this task for cross-check) carries **all** of
them. ACT is **not** folded into DVE on MARIANA.

This is consistent with two prior findings: MARIANA ships a *standalone* ACT image (see
[mariana-act.md](mariana-act.md)), and the ACT→DVE fold is a **MAVERICK**-generation event —
MARIANA is the **last** generation with separate ACT + DVE images, placed contiguous
**ACT-then-DVE-then-PE** in `.rodata` (verified by contiguity arithmetic: every DVE blob end
== the next DVE blob start; each DVE SRAM/EXTRAM cursor == the next-engine PE IRAM).

> **WALL — `Exponential` is a DVE math op, NOT activation-LUT folding.** The one new
> "exp-shaped" handler on MARIANA DVE (`Exponential`, source `exponential.cpp`) is an
> elementwise exponential **vector math** op on the DVE datapath — it is **not** the ACT
> activation-LUT path (`Activate`/`ActivationTableLoad`/`ActivationReadAccumulator` are all
> absent). Do not read `Exponential` as evidence of the fold. The fold (DVE absorbing ACT's
> LUT activation handlers) first appears on [MAVERICK](maverick-dve.md). `[HIGH/OBSERVED — 0
> `Activate*` in DVE; all present in the standalone MARIANA ACT image, cross-checked]`

> **NOTE — `PeManageSeed`/MX kernels ship MARIANA but are PE, not DVE.** The MARIANA-first
> PE kernels run on the PE engine; the DVE-visible v4 footprint is the `+7` handlers (§5),
> not anything ACT- or PE-side. `[CARRIED from the cross-gen kernel matrix]`

---

## 7. Dtype delta and the re-armed, now-engine-specific PROF tables

### 7a. Dtype — FP4/CPTC invisible as strings, but `QuantizeMx` is the MX footprint

FP4_EXP2 / CPTC do **not** appear as named strings in any MARIANA DVE blob
(`grep FP4|CPTC|MXTENSOR|SFP8|fp8_e = 0` across all 8). The only dtype constants are
`NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` (the `move.cpp:41` assertion, `… "TODO other
dtypes"`) — **byte-identical to CAYMAN**. The new dtype codes are numeric (handled by
integer comparison in the Cast/Quantize path), not named-string asserts — the same negative
MARIANA ACT showed.

**But** the FP4/MX expansion *does* leave a firmware-visible footprint on DVE that it does
**not** leave on ACT: the new handler **`QuantizeMx`** (MX = microscaling, the block-scaled
format family FP4/MXFP belongs to). The dtype expansion is invisible at the dtype-string
layer but **visible as a new MX-quantize handler/opcode** on the DVE engine.
`[HIGH/OBSERVED handler + the dtype-string negative; the `QuantizeMx`↔FP4/MX linkage
INFERRED-HIGH from the name + the v4 MX dtype context]`

### 7b. PROF_CAM — re-armed (47 → 48) AND now per-engine

```c
struct prof_cam_record {   /* 16 bytes, 64 slots; unchanged layout vs cayman-dve.md §7 */
    uint32_t opcode_id;    /* the profiled opcode */
    uint32_t mask;         /* 0xff (8-bit) — but see the 0x1ff 9-bit record below */
    uint32_t enable;       /* 1 for the populated slots */
    uint32_t reserved;     /* 0 */
};
```

- **sha**: MARIANA DVE `ca588683` ≠ CAYMAN DVE `8fd7e422` ≠ MARIANA ACT `326bc0dd`.
- MARIANA DVE arms **48** records (CAYMAN DVE 47), a **different** opcode set: it adds
  `0x30, 0x77, 0x78, 0x9f, 0xe0–0xe3` (overlapping the new dispatch opcodes) and a **9-bit**
  opcode record (`op=0x1e3 mask=0x1ff` — the larger opcode space), and drops the
  `0x01/02/03/06/07` + `0x21–0x24` + several mid-block opcodes CAYMAN profiled.

> **CORRECTION — PROF_CAM is no longer a generic shared NX CAM on MARIANA.** On CAYMAN,
> PROF_CAM was **byte-identical across all 4 NX engines** (one shared `8fd7e422`, see
> [cayman-dve.md §7](cayman-dve.md)). On MARIANA, **PROF_CAM is per-engine** (DVE `ca588683`
> ≠ ACT `326bc0dd`). The generic-shared-CAM property **did not survive the generation** — a
> reimplementer must arm the HW-decode CAM per-engine on v4, not once globally. The CAYMAN
> page's `CORRECTION` callout (the 47-record CAM is *generic* on v3) is **gen-scoped to
> CAYMAN**; it does not hold for MARIANA. `[HIGH/OBSERVED — sha DVE ≠ ACT on MARIANA]`

### 7c. PROF_TABLE — re-preallocated, header zeroed

MARIANA DVE `d72b339f` ≠ CAYMAN DVE `ce761f81` ≠ MARIANA ACT `8786dd11` — all three
distinct. CAYMAN carried header word `0x00000201` (+ `0x26000010` + descriptor), 150 nonzero
words; **MARIANA DVE zeroes the header**, 142 nonzero words (MARIANA ACT: zeroed header, 68
nonzero). Both tables keep the same size (`0x400` / `0x2000`). The `0x0201` header CAYMAN
carried is **gone across the generation**; exact field schema not decoded (out of scope).
`[HIGH/OBSERVED bytes / MED schema]`

---

## 8. New boot-time errata workaround

MARIANA DVE DEBUG DRAM carries a **new** boot-time log absent on CAYMAN DVE:

```
S: Applying patch for mariana-4062
```

It sits near `setup_enqueue_dispatch_descriptors` / the dispatch setup — a MARIANA-specific
silicon-errata workaround applied at boot. The exact errata semantics are not decoded.
`[HIGH/OBSERVED string; semantics NOT claimed]`

---

## 9. Engine-model confirmation — same `cayman/seq` SEQ engine, idx 3

MARIANA DVE is the **same** NX-class SEQ-style ASCII-opcode dispatch engine as CAYMAN DVE,
distinguished only by its data/vector handler subset + the `+7` new handlers + the enlarged
opcode space. The point-by-point invariants (packaging, dispatch model, `engine_idx=3`
identity, dual-mode tables, ErrorHandler arms, `cayman/seq/` source tree, DVE identity
strings `DveReadAccumulator`/`DveReadIndices` + `S: DVE perf mode support = %d`) are all
present and unchanged — see the §1 delta table for the changed rows and
[cayman-dve.md §10](cayman-dve.md) for the full unchanged model.

> **NOTE — the "same SEQ engine, different handler subset" hypothesis holds across the gen
> gap.** A CAYMAN↔MARIANA DVE swap is a recompile + opcode-space growth (`170→187`,
> `+7` handlers) + a re-armed per-engine HW-decode profiler + reset-vector/table relocation
> (`+0x1c`) + ~11% smaller PERF/TEST IRAM + a new `mariana-4062` errata patch. It is the
> Data/Vector Engine (`engine_idx=3`) — **not** folded with ACT (§6), **not** a POOL Q7
> engine, **not** a standalone vector core. `[HIGH/OBSERVED]`

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct disasm or byte read this pass):

- 14 `MARIANA_NX_DVE` getters (`nm` + getter-body `lea`/`movq` 14/14 exact at `.text
  0x9b3d20..0x9b4880`; `nm -D` = 0, they are local `t` symbols); 8 real + 6 zero-size
  boundary cursors (next-engine PE IRAM). Engine ordering ACT→DVE→PE proven by contiguity. 8
  real carves byte-identical (sha256) to `libnrtucode.a` member `.rodata`, 8/8.
- Reset vector `06 7d 00 00` (`j 0x1f8`) — **+0x1c** from CAYMAN's `06 76 00 00` (`j 0x1dc`),
  identical to MARIANA ACT; boot-stub body @`0xc` byte-identical to MARIANA ACT.
- Dispatch (two DEBUG-IRAM sites @0x2f09/0x3715): normalize `−0x30` (was `−0x41`), bound 187
  (`movi a3,186`; was 170/`movi a3,169`), tables `0x800/0xaec` (was `0x814/0xabc`), log
  `0x80e58`, dual HW-Decode/Sunda. Byte-search: `movi a3,186` ×2, `movi a3,169` ×0.
  Per-opcode diff: +7 new real (`0x30,0x77,0x78,0xe0–0xe3`), −0.
- Handler diff (glue-stripped, both DEBUG DRAMs, glue-trap + `Applying` false-positive
  documented): **+7** (`RandGetState`/`RandSetState`/`Rand2`/`SparsityCompress`/
  `SparsityCompressTag`/`QuantizeMx`/`Exponential`), **−0**; all 28 CAYMAN DVE-only + shared
  core retained. The +7 names == +7 new opcodes == +7 table real-count (three agreeing signals).
- **No folded activation**: 0 `Activate*` in DVE (all 3 variants); all present in the
  standalone MARIANA ACT image (cross-checked).
- Dtype: only `UINT32/INT32/FP32` named (== CAYMAN, `move.cpp:41`); FP4/CPTC absent as
  strings. `QuantizeMx` is the MX/FP4 footprint.
- Batch-norm: 6-handler set + raw opcode bindings (`0x61/62/63/64/65/8e/94`) gen-stable.
- PROF_CAM 47→48 (re-armed, 9-bit opcode `0x1e3`) + PROF_TABLE (zeroed header, 142 vs 150
  nonzero) both ≠ CAYMAN; PROF now **engine-specific** on MARIANA (DVE ≠ ACT).
- Source tree still `cayman/seq/src/…`; self-name `S: BEGIN on mariana`; new sources
  `exponential.cpp` + `addr_bits.hpp`; new errata log `Applying patch for mariana-4062`;
  disasm exit-0 / empty-stderr / 46,162 lines on DEBUG IRAM.
- Cross-gen sizes (PERF/TEST IRAM ~11% smaller, DRAM +8 `S:`); no MARIANA DVE image
  byte-identical to its CAYMAN counterpart (8/8 shas). CAYMAN baseline re-carved + re-hashed
  (5/5 anchors MATCH) — the diff is against the authentic CAYMAN images.

**MED / INFERRED:**

- Byte-exact PERF/TEST per-opcode dispatch rows (FLIX-desync; the *model* is HIGH from DEBUG).
- Full per-new-handler IRAM `const16`↔DRAM-offset xref (one representative, `RandSetState
  @0x11da6`, decoded HIGH; the rest rest on the name-cluster + opcode-table evidence).
- Batch-norm **internal math** equivalence: NOT re-decoded (distinct-IVP set differs, partly
  FLIX-desync; handler/opcode stability is HIGH, math-body equivalence undetermined).
- `QuantizeMx`↔FP4/MX dtype linkage: INFERRED-HIGH from the name + the v4 MX context.
- PROF_TABLE field schema: structure-level only.

**LOW / NOT CLAIMED:**

- Which silicon/runtime selects MARIANA vs MARIANA_PLUS, DEBUG vs PERF vs TEST.
- MARIANA_PLUS `NX_DVE` byte-comparison (catalog carries the getters; not carved here — see
  [mariana-plus-dve.md](mariana-plus-dve.md)).
- The exact `mariana-4062` errata semantics.
- The per-handler operand layout of the new DVE handlers (per-handler decode out of scope).

---

## Cross-references

- **[CAYMAN × DVE image](cayman-dve.md)** — the committed NC-v3 baseline this page diffs
  against (the 53-handler roster, the 170-bound dispatch, the unchanged engine model).
- **[MAVERICK × DVE image](maverick-dve.md)** — the NC-v5 step where the **ACT→DVE fold**
  finally happens (the absorption MARIANA does **not** do, §6).
- **[MARIANA × ACT image](mariana-act.md)** — the sibling v4 ACT image that shares the
  `+0x1c` reset shift, the `Rand*` pair, and the standalone (un-folded) packaging.
- **[MARIANA+ × DVE image](mariana-plus-dve.md)** — the v4+ sibling (catalog getters carved
  separately).
- **[Cross-Generation Kernel-Info Matrix](cross-gen-kernel-info-matrix.md)** — the
  per-generation handler-presence matrix (the `+7` MARIANA DVE handlers land here).
- **[Image Catalog Index](image-catalog-index.md)** — the full getter image catalog.
- **DVE kernel pages** (opcode-exact, gen-stable on MARIANA per §5c): batch-norm
  ([forward](../firmware/kernels/batchnorm-forward.md),
  [backprop](../firmware/kernels/batchnorm-backprop.md),
  [gradaccum](../firmware/kernels/batchnorm-gradaccum.md),
  [paramload](../firmware/kernels/batchnorm-paramload.md)),
  [search-cluster](../firmware/kernels/search-cluster.md),
  [stream-transpose](../firmware/kernels/stream-transpose.md).
- **[The Confidence & Walls Model](../reference/confidence-model.md)** — the
  `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tags and the FLIX-desync ceiling.
