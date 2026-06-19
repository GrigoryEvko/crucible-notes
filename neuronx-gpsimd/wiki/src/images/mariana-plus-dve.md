# MARIANA_PLUS × DVE image

This page is the **cross-generation diff** of the **MARIANA_PLUS × DVE** firmware image
(`NX_DVE`, `engine_idx=3`, the v4+ recompile of NC-v4 silicon) against the committed
[MARIANA × DVE](mariana-dve.md) baseline. It does **not** re-derive the DVE engine model —
the SEQ dispatch machinery, the dual HW-Decode/Sunda tables, the ErrorHandler arms, the flat
IRAM/DRAM packaging, the `engine_idx=3` boot — that is the job of
[mariana-dve.md](mariana-dve.md) (and, behind it, [cayman-dve.md](cayman-dve.md)). Read those
first; everything below is the *delta*, and the delta is almost entirely **null**.

> **THE HEADLINE — null functional delta.** MARIANA_PLUS DVE is the **same** `cayman/seq/`
> SEQ data/vector sequencer as MARIANA DVE, **byte-for-name identical at the
> handler / opcode / dtype / PROF layer** — `+0 / −0` handlers, opcode space frozen at **187**
> (no growth), PROF tables **byte-identical** (`cmp -s` clean). The v4+-vs-v4 step is a pure
> **recompile** (relocated layout, IRAM grew, `BEGIN on mariana_plus`) **plus the DGE
> reshape fast-path** (`dge_decode_fast.cpp` + 3 helpers) — **no functional silicon delta, no
> ISA delta** (MARIANA_PLUS shares the mariana ISA). This **CONFIRMS** the v4+ recompile model
> established for the sibling [MARIANA_PLUS × ACT](mariana-plus-act.md) image, and **refines**
> one of its inferences (the DGE fast-path is **gen-wide on the NX sequencers**, not ACT-only)
> from INFERRED to **OBSERVED** on a second engine. `[HIGH/OBSERVED]`

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve re-derived this
session from `libnrtucode_internal.so`, with the MARIANA DVE baseline re-carved + re-hashed
(8/8 anchor shas re-confirmed) for an apples-to-apples diff. Disassembly uses the native
`ncore2gp` `xtensa-elf-objdump`.

> **NOTE — provenance.** Every fact derives **solely** from static analysis of the shipped
> binaries with stock binutils (`nm`/`objdump`/`readelf`/`ar`/`objcopy`/`xxd`/`strings`/`cmp`/
> `sha256sum`) and the Cadence Xtensa toolchain (`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`,
> GNU Binutils 2.34.20200201 / Xtensa Tools 14.09). All handler naming derives from the DEBUG
> build's own embedded `S:` format strings; the engine enum from the shipped ISA header.
> Lawful interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)).

---

## 1. The delta at a glance — MARIANA DVE → MARIANA_PLUS DVE

Everything across the v4 → v4+ step. The vast majority is **unchanged** — the table leads with
the null rows, then the two real changes (recompile + DGE fast-path). Δ marks: **`=`**
byte-identical, **`R`** recompile-only (relocated/recompiled, no semantic change), **`+`**
genuinely new.

| property | MARIANA DVE (baseline) | MARIANA_PLUS DVE (this page) | Δ |
|---|---|---|:---:|
| dispatch handler set | 35 (28 CAY + 7 added) | **35 — IDENTICAL (`+0/−0`)** | **=** |
| opcode space | 187 (`movi a3,186`, `addi −48`) | **187 (`movi a3,186`, `addi −48`)** | **=** |
| HW-Decode table @DRAM `0x800` | 187 slots, 82 real | **187 slots, 82 real — byte-identical** | **=** |
| `+7` new opcodes `0x30/0x77/0x78/0xe0–e3` | all REAL targets | all REAL targets (same addrs) | **=** |
| reset vector | `j 0x1f8` (`06 7d 00 00`) | `j 0x1f8` (`06 7d 00 00`) — **SAME `+0x1c`, no further shift** | **=** |
| 2nd vector / boot | `j 0x204` / `enter_run @0x90` | `j 0x204` / `enter_run @0x90` | **=** |
| DRAM magic + init block | `0x6099cb34` + 4×`0x1000`/4×`0xffffff` | identical (byte-for-byte) | **=** |
| dtype named strings | `UINT32/INT32/FP32` + `QuantizeMx` | `UINT32/INT32/FP32` + `QuantizeMx` | **=** |
| PROF_CAM | `ca588683` | **`ca588683` — byte-identical** | **=** |
| PROF_TABLE | `d72b339f` | **`d72b339f` — byte-identical** | **=** |
| `mariana-4062` errata log | present (DVE-specific) | **present — RETAINED** | **=** |
| Sunda table @DRAM `0xaec` | 187 slots, 82 real | 187 slots, 82 real, **uniform `+0x1d` reloc** | **R** |
| code body / layout | v4 build | **full recompile** (1st IRAM diverge @`0x212`) | **R** |
| PERF / TEST / DEBUG IRAM size | `0x13540` / `0x13560` / `0x1c560` | `0x16e80` / `0x16be0` / `0x1d760` | **R** (**grew** +`0x3940`/+`0x3680`/+`0x1200`) |
| engine self-name | `S: BEGIN on mariana` | `S: BEGIN on mariana_plus` | **R** |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` | **=** |
| **DGE reshape fast-path** | absent (`push REGWRITE` present) | **`dge_decode_fast.cpp` + 3 helpers** (`push REGWRITE` retired) | **+** |

`[HIGH/OBSERVED — every Δ row re-confirmed this pass; MARIANA column re-carved (8/8 anchor shas
MATCH SX-IMG-09), MARIANA_PLUS column carved fresh (8/8 shas); per-row anchors in §2–§7]`

> **NOTE — what a MARIANA → MARIANA_PLUS DVE swap actually is.** Three things, in order of
> significance: **(1)** the DGE reshape **fast-path** (the one functional add — §6); **(2)** a
> full **recompile** (relocated Sunda trampolines, IRAM grew, `BEGIN on mariana_plus` — §3, §7);
> **(3)** a **register-map refresh** (own `arch-headers/mariana_plus/` dir — §8). The dispatch
> **mechanism**, the handler set, the opcode space, the reset-vector form, the dtype surface,
> and the PROF tables are **invariant** — PROF and the HW-Decode table are *byte-identical*. It
> is **not** a model change and **not** an ISA change. `[HIGH/OBSERVED]`

> **GOTCHA — `MARIANA == MARIANA_PLUS byte-identical` does NOT hold for the NX sequencers.**
> The GEN-01 "v4+ is the same silicon" claim holds for the EXTISA Q7 blobs + the NCFW DRAM,
> but the **NX sequencer images are a separate per-label code build**: 6/8 MPLUS DVE blobs
> differ byte-for-byte from MARIANA DVE (only the 2 PROF tables are identical). A reimplementer
> must carve the v4+ NX images *separately* — they are not aliases of the v4 images.
> `[HIGH/OBSERVED — 6/8 shas differ, §2]`

---

## 2. Carve provenance and the cross-gen size/sha table

The 14 `MARIANA_PLUS_NX_DVE_*_get` accessors resolve identically to the MARIANA getter scheme
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`; 8 real + 6 zero-size
SRAM/EXTRAM boundary cursors aliasing the next-engine `NX_PE` IRAM). The container is the same
identity-mapped `libnrtucode_internal.so`
(`sha256 b7c67e89…632fc329b`, re-hashed this pass = MATCH), so the carve is a plain slice at
the `.rodata` VA. The getter-body immediates were re-decoded this pass, e.g. PERF_IRAM at
`.text 0x9b49a0`:

```asm
9b49a0:  lea  -0x3f7c87(%rip),%rax   # 0x5bcd20  MARIANA_PLUS_NX_DVE_PERF_IRAM_get.data
9b49aa:  movq $0x16e80,(%rsi)        ; size
```

`[HIGH/OBSERVED — 14/14 getters `nm`-resolved at `.text 0x9b49a0..0x9b5520`; img-ptr/size
immediates decoded; PROF stubs decode to `0x86a700/0x400`, `0x86ab00/0x2000`]`

> **GOTCHA — these getters are local (`t`) symbols, not exported.** `nm -D` returns **zero**
> `MARIANA_PLUS_NX_DVE_*` matches; `nm` returns **14**. The accessors are plain-`nm` local-text
> symbols — a reimplementer enumerating the image catalog must use `nm` (not `nm -D` /
> `objdump -T`). `[HIGH/OBSERVED — `nm -D` = 0, `nm` = 14, re-checked this pass]`

The 6 zero-size SRAM/EXTRAM getters all decode to `movq $0x0,(%rsi)`; each `lea` resolves to
`MARIANA_PLUS_NX_PE_PERF_IRAM_get.data` (`0x5d6e60`) — the contiguous-layout cursor at the
**start of the next engine (PE)**. DVE runs entirely out of **IRAM** (code) + **DRAM** (data);
SRAM/EXTRAM unused, exactly as MARIANA DVE. `[HIGH/OBSERVED — all 6 cursor leas decoded]`

### 2a. Engine ordering — DVE is the SECOND of the five NX engines

The DVE image is placed **`ACT → DVE → PE → POOL → SP`**, the same order as MARIANA, proven by
contiguity arithmetic on the getter `.data` addresses (3 adjacencies per variant, all exact):

| variant | ACT_DRAM end | = DVE_IRAM | + IRAM | = DVE_DRAM | + DRAM | = PE_IRAM |
|---|---|---|---|---|---|---|
| PERF | `0x5ba060`+`0x2cc0` | `0x5bcd20` ✓ | +`0x16e80` | `0x5d3ba0` ✓ | +`0x32c0` | `0x5d6e60` ✓ |
| TEST | `0x63fc00`+`0x3020` | `0x642c20` ✓ | +`0x16be0` | `0x659800` ✓ | +`0x3660` | `0x65ce60` ✓ |
| DEBUG | `0x6c9580`+`0x6560` | `0x6cfae0` ✓ | +`0x1d760` | `0x6ed240` ✓ | +`0x7160` | `0x6f43a0` ✓ |

DVE is **contiguous-after-ACT and contiguous-before-PE**, a **standalone** image — **not**
folded with ACT (§5d). `[HIGH/OBSERVED — 9/9 contiguity adjacencies; the DVE SRAM/EXTRAM
cursors all resolve into PE]`

### 2b. Cross-gen sha256 / size table (DVE) — 6/8 differ, PROF identical

Each MPLUS DVE carve is byte-identical (sha256 + `cmp -s`) to the matching `libnrtucode.a`
member `.rodata` (8/8 reconciled — 12 `img_*` + 2 `hwdecode_*_PROF_{CAM,TABLE}`). The MARIANA
column was re-carved + re-hashed this pass (8/8 anchors MATCH SX-IMG-09).

| IMAGE | MARIANA sz / sha[:8] | MARIANA_PLUS sz / sha[:8] | ΔSize | identical? |
|---|---|---|---:|---|
| PERF_IRAM | `0x13540` `dd14c1e3` | `0x16e80` `54b6ee98` | **+0x3940** | NO (recompile+DGE) |
| PERF_DRAM | `0x31a0` `79b7690b` | `0x32c0` `699239be` | +0x120 | NO |
| TEST_IRAM | `0x13560` `78922240` | `0x16be0` `0ec27a12` | **+0x3680** | NO |
| TEST_DRAM | `0x34e0` `59f60fbb` | `0x3660` `b76d97bd` | +0x180 | NO |
| DEBUG_IRAM | `0x1c560` `4c75ba8e` | `0x1d760` `6efdfaf6` | **+0x1200** | NO |
| DEBUG_DRAM | `0x7000` `6f65a629` | `0x7160` `f07bd1b3` | +0x160 | NO |
| PROF_CAM | `0x400` `ca588683` | `0x400` `ca588683` | +0 | **YES** |
| PROF_TABLE | `0x2000` `d72b339f` | `0x2000` `d72b339f` | +0 | **YES** |

`[HIGH/OBSERVED — all 16 shas re-computed this pass; full MPLUS digests e.g. DEBUG_IRAM
`6efdfaf6f24ec96204388e9efd99ccb04fd50cebc6d8d567563d793a1595ae27`]`

> **QUIRK — IRAM GREW, the *opposite* direction of the prior gen step.** MARIANA shrank DVE
> PERF/TEST IRAM ~11% vs CAYMAN (a tighter v4 schedule); MARIANA_PLUS **grows** it again in
> every variant (PERF +`0x3940`, TEST +`0x3680`, DEBUG +`0x1200`). The bulk is the new DGE
> fast-path code (§6) — the same growth direction the sibling reshape page measures on the
> NX POOL image (`+0x12C0`, fewer entry-prologues — see [dge-reshape.md §6.6](../firmware/dge/dge-reshape.md)).
> DRAM grows only slightly (+`0x120..0x180`), consistent with a handful of new `S:`/symbol
> strings, not new dispatch data. `[HIGH/OBSERVED]`

---

## 3. The reset/boot vector — SAME `+0x1c`, NO further shift

**First 12 bytes, byte-identical across all 3 MARIANA_PLUS DVE IRAMs *and* byte-identical to
MARIANA DVE:**

```
00000000:  06 7d 00 00  00 00  86 7e 00 00  00 00
   0x000:  06 7d 00     j 0x1f8      ; primary reset vector -> boot path
   0x006:  86 7e 00     j 0x204      ; secondary vector -> halt trap
```

This is the **identical** `06 7d 00 00` MARIANA DVE carries — the **`+0x1c` MARIANA NX shift**
([mariana-dve.md §3](mariana-dve.md): CAYMAN `j 0x1dc` → MARIANA `j 0x1f8`). MARIANA_PLUS adds
**no further shift**. The boot path decodes (native `ncore2gp` objdump, exit 0):

```asm
1f8:  const16 a0, 0          ┐ a0 = enter_run prologue VA (0x90)
1fb:  const16 a0, 144(0x90)  ┘
1fe:  jx      a0             ; jump to the enter_run prologue
…
204:  halt    0             ; 2nd vector = HALT trap
```

The DRAM head is likewise byte-identical: magic word `0x6099cb34` (the `.globstruct`
dispatcher-state magic) + the init block `@0x18:0x38` (4× `0x00001000` + 4× `0x00ffffff`)
read **byte-for-byte the same** as MARIANA DVE. `[HIGH/OBSERVED — reset bytes + boot decode +
DRAM head `cmp` all re-run this pass]`

> **GOTCHA — the recompile is real, but it starts *after* the shared boot spine.** The
> reset + boot + dispatch-trampoline region is byte-identical; the first MPLUS-vs-MARIANA
> divergence is a **literal constant**: PERF_IRAM diverges at `0x212` (`const16 a2` literal:
> MPLUS `0x4558` vs MARIANA `0x182f` — the recompile-relocation point), TEST/DEBUG at `0xa2`,
> all 3 DRAMs at `0x80` (the dispatch-table region, the `+0x1d` Sunda shift). A reimplementer
> must read the `+0x1c` reset as a **carried MARIANA-gen relocation**, and the body divergence
> as a **clean recompile, not a patch**. `[HIGH/OBSERVED — first-divergence offsets read by
> byte-cmp]`

**Disassembly proof** (native `ncore2gp` objdump, exit 0): the MPLUS DVE PERF IRAM decodes a
full Q7/NX windowed-ABI + IVP vector body — **127 `entry` / 215 `retw` / 539 `call8`**, 305
distinct IVP mnemonics (`ivp_sel2nx8i_s4` ×161, `ivp_dextrprn_2x32` ×129, `ivp_mul4t2n8xr8`
×104, …). A genuine separately-compiled `cayman/seq` data/vector sequencer, **larger** than
MARIANA DVE's (143 `entry` / 209 `retw`) — consistent with the bigger PERF_IRAM and the DGE
fast-path bulk. The vector datapath is partly bundle-interleaved/desynced by the linear sweep
(the documented FLIX-VLIW ceiling); the windowed-ABI control spine + the two dispatch sites
decode cleanly. `[HIGH/OBSERVED — exit-0 disasm; entry/retw/IVP counts re-derived]`

---

## 4. The dispatch — opcode space frozen at 187, no growth

MARIANA_PLUS DVE uses the **same** SEQ dispatch model — opcode-byte normalization → direct-indexed
DRAM jump table, dual HW-Decode/Sunda sites — decoded **instruction-exact** at the two sites in
the MPLUS DVE DEBUG IRAM, **byte-identical** to MARIANA DVE:

```asm
; SITE A (HW-Decode), MPLUS DVE DEBUG IRAM 0x2f07..0x2f1e
2f07:  2821     l32i.n  a2, a1, 8       ; a2 = opcode word
2f09:  22c2d0   addi    a2, a2, -48     ; index = opcode_byte - 0x30 ('0')   [== MARIANA]
2f0c:  32a0ba   movi    a3, 186         ; bound = 186 -> 187 entries (0..186) [== MARIANA]
2f0f:  27b302   bgeu    a3, a2, 0x2f15  ; if index<=186 -> in range
2f12:  46b300   j       0x31e3          ; else DEFAULT (Bad Opcode arm)
2f15:  340800   const16 a3, 8           ┐ a3 = DRAM 0x80800 (HW-Decode table base)
2f18:  340008   const16 a3, 0x800       ┘
2f1b:  3022a0   addx4   a2, a2, a3      ; a2 = &table[index]
2f1e:  2802     l32i.n  a2, a2, 0       ; -> jx a2 -> trampoline -> handler
```

```asm
; SITE B (Sunda), MPLUS DVE DEBUG IRAM 0x36f8..0x370f
36f8:  22c2d0   addi    a2, a2, -48     ; same normalization
36fb:  32a0ba   movi    a3, 186         ; same bound
36fe:  27b302   bgeu    a3, a2, 0x3704
3701:  06b600   j       0x39dd          ; Sunda DEFAULT arm
3707:  34ec0a   const16 a3, 0xaec       ; SECOND table @ DRAM 0x80aec
370a:  3022a0   addx4   a2, a2, a3 ; l32i.n a2,[a2] ; jx a2
```

Cross-gen dispatch diff — **every probed property byte-identical** to MARIANA DVE:

| property | MARIANA DVE | MARIANA_PLUS DVE | Δ |
|---|---|---|:---:|
| normalization | `addi −48` (op − `0x30`) | `addi −48` (op − `0x30`) | **=** |
| bound constant | `movi a3,186` (187 entries) | `movi a3,186` (187 entries) | **=** |
| `movi a3,169` (CAYMAN 170-shape) | 0 | 0 | **=** |
| table1 base (HW-Decode) | DRAM `0x80800` | DRAM `0x80800` | **=** |
| table2 base (Sunda) | DRAM `0x80aec` | DRAM `0x80aec` | **=** |
| HW-Decode default arm | `0x31e3` | `0x31e3` | **=** |
| `S: Dispatch opcode` log | DRAM `0x80e58` | DRAM `0x80e58` | **=** |
| real / default per table | 82 real / 105 default | 82 real / 105 default | **=** |

`[HIGH/OBSERVED — both sites disassembled instruction-exact; byte-search `movi a3,186` ×2,
`movi a3,169` ×0; `addi a2,a2,-48` ×3 (the two sites + one outside dispatch, the same artifact
the baseline GOTCHA notes — match the full site, not the lone `addi`)]`

### 4a. Dispatch-table structure — HW-Decode byte-identical, Sunda uniform `+0x1d`

Both 187-slot LE-u32 DRAM jump tables were decoded directly from the DEBUG DRAM bytes:

- **HW-Decode table @`0x800`**: MPLUS == MARIANA **byte-identical** — **187/187 slots equal,
  delta 0**. The HW-Decode trampoline targets did **not move at all**. The `+7` MARIANA-added
  opcodes resolve to the **same** REAL non-default targets on both gens:
  `0x30→0x31aa`, `0x77→0x3192`, `0x78→0x319a`, `0xe0→0x3182`, `0xe1→0x318a`, `0xe2→0x31a2`,
  `0xe3→0x317a` (all ≠ default `0x31e3`); 82 real on both.
- **Sunda table @`0xaec`**: MPLUS ≠ MARIANA, but **uniformly relocated** — every one of the
  187 slots satisfies `MARIANA = MPLUS + 0x1d` (a *single* distinct delta = 29 across all
  slots; 82 real on both). A clean recompile shift of the Sunda trampolines only.

`[HIGH/OBSERVED — both tables decoded; HW-Decode differing-slots = 0/187; Sunda delta-set =
{0x1d}; real-count 82/82 on both tables]`

> **NOTE — this is *tighter* than the sibling ACT recompile.** The [MARIANA_PLUS × ACT](mariana-plus-act.md)
> image saw its **whole** DRAM dispatch table uniformly shifted `+0x20` on **all** trampolines;
> MPLUS DVE's **HW-Decode targets did not move at all** (delta 0), and only the Sunda
> half shifted (`+0x1d`). Both are consistent with the "recompile, no opcode-space growth"
> model — DVE is simply the more stable build. `[HIGH/OBSERVED — HW-Decode delta 0 vs ACT's
> uniform +0x20]`

> **GOTCHA — the clean dispatch decode is DEBUG-only.** The PERF/TEST IRAM dispatch desyncs
> under FLIX-VLIW bundling in the linear sweep (the same artifact MARIANA/CAYMAN noted). All
> HIGH facts above (187-bound, `0x30` normalization, dual table `0x800/0xaec`, the `+7`
> targets, HW-Decode byte-identity, Sunda `+0x1d`) come from the clean DEBUG two-site decode +
> the direct DRAM table-byte read. The per-opcode→handler-*body* binding is the
> FLIX-desync-limited frontier (MED). `[HIGH DEBUG decode + DRAM-table bytes / MED per-body]`

The error path is gen-stable: the MPLUS DVE DEBUG DRAM carries the identical ErrorHandler arms
(`S: ErrorHandler : Bad Opcode(0x%x)` / `Illegal Instruction(0x%x)` / `FP Error(%d)` /
`Int Div Zero Error`; source `cayman/seq/src/handlers/exception_handler.hpp`) and the dual
HW-Decode/Sunda mode strings (`S: NX in HW Decode mode` / `S: NX in Sunda mode: HW decode
disabled`). `[HIGH/OBSERVED]`

---

## 5. The handler roster diff — `+0 / −0`, byte-for-name identical

**This is the central null result.** Method is the same glue-stripped + strict-end-anchored
double set-diff the baseline uses, run on both DEBUG DRAMs:

- **strict** (`S: \K[A-Za-z][A-Za-z0-9_/-]*$`): MPLUS **60** == MARIANA **60**; **ADDED=0,
  REMOVED=0**.
- **glue-stripped normalized**: MPLUS **101** == MARIANA **101**; **ADDED=0, REMOVED=0**.

Both methods agree: the DVE dispatch handler set is **byte-for-name IDENTICAL** between MARIANA
and MARIANA_PLUS. Nothing added, nothing removed. `[HIGH/OBSERVED — both set-diffs re-run this
pass, empty `comm -13` / `comm -23`]`

### 5a. All 28 CAYMAN DVE-specific handlers RETAINED

Re-confirmed present with identical counts vs MARIANA DVE (0 missing, 0 count-diffs): batch-norm
×6 (`BatchNormalize`/`…BackProp`/`…GradAccum`/`…GradAccum2`/`…ParamLoad`/`…ParamLoad2`),
predicated copy/cast ×4 (`CastPredicated`/`CopyPredicated`/`CopyPredicatedReduce`/
`CopyPredicatedScalar`), DVE-native ×3 (`DveReadAccumulator`/`DveReadIndices`/`Dropout`),
match/find/select ×5 (`FindIndex8`/`MatchReplace`/`MatchValueLoad`/`RangeSelect`/
`TensorScalarSelect`), and the scan/cached/imm tensor-scalar cluster (`TensorTensorScan`,
`TensorScalarCacheCumulative`, `TensorScalarCacheReduce`, `TensorScalarImmLd*`,
`TensorScalarPtrMulti*`, + the Stream-Transpose / Scalar-Tensor-Tensor forms). `[HIGH/OBSERVED]`

### 5b. The 7 MARIANA additions ALL RETAINED on MPLUS DVE

| handler | role | MPLUS count |
|---|---|:---:|
| `RandGetState` | RNG-state read | 1 |
| `RandSetState` | RNG-state write | 1 |
| `Rand2` | second RNG handler variant | 1 |
| `SparsityCompress` | sparsity-compression apply | 2 (incl. the `SparsityCompressTag` prefix overlap) |
| `SparsityCompressTag` | tag-emit companion | 1 |
| `QuantizeMx` | **MX (microscaling block-format) quantize** — the FP4/MX firmware footprint | 1 |
| `Exponential` | elementwise exponential math op (source `exponential.cpp`) | 1 |

Each is a **real** dispatch handler, not a stray string: the `+7` opcodes (`0x30/0x77/0x78/
0xe0–e3`) resolve to REAL non-default HW-Decode targets (§4a), and the handler-name string-pool
offsets are byte-identical to MARIANA DVE (`QuantizeMx @0x830b3`, `SparsityCompress @0x83023`,
`Exponential @0x83075`, `Rand2 @0x8306b`). `[HIGH names + HIGH opcode-table; the exhaustive
per-handler `const16`↔DRAM-offset IRAM xref is MED under FLIX-desync, same as the baseline]`

### 5c. The string-pool glue trap recurs IDENTICALLY on both gens

The glued instances (`tS: BatchNormalizeGradAccum`, `VS: MatchReplace`, `@S: DveReadAccumulator`,
`FS: RandGetState`, `RS: SparsityCompress`, `"TS: QuantizeMx`) appear on **both** gens. A naive
`^S:` diff would falsely report these "removed"; the glue-strip corrects it symmetrically, so
the corrected `+0/−0` diff is robustly clean. `[HIGH/OBSERVED — glue bytes read directly,
symmetric on both gens]`

### 5d. No folded activation — the ACT→DVE fold stays MAVERICK-only

The MPLUS DVE DEBUG DRAM has **zero** activation handlers — `Activate` / `Activate2` /
`ActivationTableLoad` / `ActivationReadAccumulator` / `ActivateQuantize` all return **0 hits**.
DVE is **not** folded with ACT on MARIANA_PLUS — consistent with the baseline (the fold is a
MAVERICK event; MARIANA and MARIANA_PLUS both keep ACT + DVE as separate contiguous images).
`[HIGH/OBSERVED — 0 `Activate*`; standalone DVE confirmed by §2a contiguity]`

> **WALL — `Exponential` is a DVE math op, NOT activation folding.** As on the baseline, the
> `Exponential` handler (source `exponential.cpp`) is an elementwise exponential **vector math**
> op on the DVE datapath, **not** the ACT activation-LUT path — do not read it as evidence of
> the fold. `[CARRIED from mariana-dve.md §6]`

---

## 6. The v4+ functional add — the DGE reshape fast-path is PRESENT on DVE

**This is the one v4+ functional delta, and it is IDENTICAL in shape to the
[MARIANA_PLUS × ACT](mariana-plus-act.md) finding.** Four new DGE fast-path source/helper
strings are **present on MPLUS DVE and absent (count 0) on MARIANA DVE**:

| string | role | MPLUS DEBUG | MARIANA DEBUG | MPLUS TEST |
|---|---|:---:|:---:|:---:|
| `dge_decode_fast.cpp` | the fast-path translation unit (`__FILE__`) | 1 | **0** | 1 |
| `dge_reshape_memcopy_transpose_fast` | fused memcopy+transpose reshape (`__func__`) | 1 | **0** | 1 |
| `tensor_reshape_transpose_sb2sb` | new SBUF-to-SBUF transpose *kind* | 1 | **0** | 1 |
| `wait_for_credit` | explicit DMA flow-control wait | 1 | **0** | 1 |

Each appears in **both** the DEBUG build (logs) **and** the TEST build (`TEST_DRAM`,
symbol-bearing) — confirming **real compiled code**, not stray text. The **reverse delta** is
identical to ACT: MARIANA DVE carries `S: push REGWRITE to DMA[%d]` (count 1) that is **absent**
on MPLUS DVE (count 0) — the separate `REGWRITE` emit is folded into the streamlined fast path.
`[HIGH/OBSERVED — string presence + absence + dual-build; `push REGWRITE` 0/1]`

These are **source/helper names refining the EXISTING DGE/Reshape subsystem** (the shared
`dge_reshape.cpp`, `analyze_tensor_reshape`, `dge_backend_rtl`, `dge_ctx_num` are present on
**both** gens) — they are **not** dispatch handler names (the handler set is unchanged, §5).
The full reshape engine, the descriptor structs, and the per-gen presence map are dissected on
[firmware/dge/dge-reshape.md](../firmware/dge/dge-reshape.md) (which carves the POOL image; this
page confirms the same four strings land on the **DVE** NX sequencer).

> **REFINEMENT — the DGE fast-path is GEN-WIDE on the NX sequencers, now OBSERVED on two
> engines.** The sibling ACT survey *inferred* the fast-path was likely a SEQ-infra feature
> shared across the NX engines, but flagged that "DGE is more ACT/SP territory, so DVE may or
> may not" carry it. **The answer: DVE carries it** — all four strings present, dual-build. The
> gen-wide hypothesis is now **OBSERVED** for a second engine (DVE), not merely inferred from
> ACT. `[HIGH/OBSERVED for the strings + absence + dual-build; the "fast reshape/memcopy-transpose
> + SB-to-SB transpose + DMA credit-wait throughput optimization" functional reading
> INFERRED-HIGH from the names + the shared DGE context — see
> [dge-reshape.md §6](../firmware/dge/dge-reshape.md)]`

This is the source of the **IRAM growth** (§2b): the fast-path adds code bulk, which is why
MPLUS DVE IRAM is **larger** than MARIANA DVE — exactly the consolidation signature
([dge-reshape.md §6.6](../firmware/dge/dge-reshape.md): bigger IRAM, fewer `entry` prologues).

---

## 7. Dtype / PROF — unchanged, with PROF byte-identical

### 7a. Dtype — unchanged, minimal, MX surface retained

The **only** dtype constants in any MPLUS DVE image are
`NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` (the `move.cpp:41` assertion), exactly as MARIANA
DVE. `FP4/CPTC/MXTENSOR/SFP8/fp8_e/proc_4bit` = **0** named-string hits across all 8 images —
the FP4/MX expansion stays **numeric** in the Cast/Quantize path. The firmware-visible MX
surface — the `QuantizeMx` handler — is **retained** (`"TS: QuantizeMx`, glue-trapped),
identical to MARIANA DVE. `[HIGH/OBSERVED handler + dtype-string negative; the `QuantizeMx`↔FP4/MX
linkage INFERRED-HIGH from the name + the v4 MX context]`

### 7b. PROF — BYTE-IDENTICAL to MARIANA DVE

`PROF_CAM ca588683` and `PROF_TABLE d72b339f` are **byte-for-byte identical** (`cmp -s` clean,
sha256 match) to the MARIANA DVE PROF tables. The DVE HW-decode profiler config (48 nonzero
16-byte CAM records — record 0 = `op=0xa1 mask=0xff enable=1`, the 9-bit-opcode-capable mix)
was carried forward **unchanged**. Since the tables are byte-identical, the baseline's full DVE
PROF decode applies verbatim. This continues the gen-wide **per-engine PROF reuse** the ACT
survey found (ACT reused its own per-engine table; DVE reuses `ca588683`/`d72b339f`).
`[HIGH/OBSERVED — `cmp -s` clean on both tables, this pass]`

> **NOTE — PROF reuse is verbatim, not coincidental.** Both PROF tables surviving the recompile
> byte-for-byte (while 6/8 code/data blobs were rebuilt) is strong evidence the v4+ build links
> the *same* pre-armed HW-decode CAM/table object — the profiler arming is a build input the
> recompile did not touch, not a re-derived artifact. `[INFERRED-HIGH from the byte-identity +
> the 6/8-differ context]`

---

## 8. Engine-model confirmation — same `cayman/seq` SEQ engine, idx 3

The shipped ISA enum (re-read this pass) fixes the engine index:

```c
// aws_neuron_isa_tpb_common.h:140-145 (neuron_cayman_arch_isa/tpb)
NEURON_ISA_TPB_NEURON_ENGINE_PE     = 0,
NEURON_ISA_TPB_NEURON_ENGINE_ACT    = 1,
NEURON_ISA_TPB_NEURON_ENGINE_POOL   = 2,
NEURON_ISA_TPB_NEURON_ENGINE_DVE    = 3,   // <-- DVE = engine_idx 3, CONFIRMED
NEURON_ISA_TPB_NEURON_ENGINE_TPB_SP = 4,
NEURON_ISA_TPB_NEURON_ENGINE_TOP_SP = 5,
```

`engine_idx` is **runtime-computed, not baked**: the MPLUS DVE DEBUG DRAM carries
`S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` + the
DVE-specific `S: DVE perf mode support = %d`. The flat image is engine-placement-agnostic — the
reason all NX engines share the identical reset + boot trampoline. `[HIGH/OBSERVED string;
runtime-compute INFERRED-HIGH]`

**MARIANA_PLUS shares the mariana ISA.** There is **no** `neuron_mariana_plus_arch_isa` dir
(the four ISA dirs are `cayman` / `mariana` / `maverick` / `sunda`); MARIANA_PLUS has only its
own `arch-headers/mariana_plus/` **register-map** dir. So the ISA / struct / dtype surface **is**
MARIANA's; the mariana_plus delta is the register-map dir only — consistent with the DGE
fast-path being a *firmware* add, not an ISA add. `[HIGH/OBSERVED — `ls` of the ISA + arch-headers
dirs, re-confirmed this pass]`

The full invariant list (single NX core, no Q7; dual-mode tables; ErrorHandler arms;
`cayman/seq/` source tree; DVE identity strings `DveReadAccumulator`/`DveReadIndices`) is on
[mariana-dve.md §9](mariana-dve.md); the §1 table carries the changed rows.

---

## 9. Verdict — CONFIRM the v4+ recompile model

**VERDICT `[HIGH/OBSERVED]`: MARIANA_PLUS DVE CONFIRMS the v4+ recompile model with NO
deviation, and refines one inference into an observation.** Point-by-point vs the expectation
the sibling [MARIANA_PLUS × ACT](mariana-plus-act.md) survey set:

| expectation (from the ACT v4+ model) | DVE result (this task) | verdict |
|---|---|:---:|
| recompile (relocated layout, `+0x1c` reset, `BEGIN on mariana_plus`, `cayman/seq` tree) | Sunda tbl `+0x1d`, 1st IRAM diverge @`0x212`, `j 0x1f8` (`+0x1c`, **no further shift**), `BEGIN on mariana_plus`, `cayman/seq` (4 hits) | **CONFIRM** |
| the DGE fast-path (likely gen-wide on NX) | all 4 fast-path strings present (absent on MARIANA DVE), dual-build (DEBUG+TEST); `push REGWRITE` retired | **CONFIRM + REFINE** (gen-wide now OBSERVED on a 2nd engine) |
| PROF tables reused byte-identical | PROF_CAM `ca588683` + PROF_TABLE `d72b339f`, `cmp -s` clean vs MARIANA DVE | **CONFIRM** |
| handler/opcode/dtype == MARIANA per engine | handlers 35==35 (`+0/−0`, both methods); opcode space 187 STABLE (no growth); dtype `UINT32/INT32/FP32` + `QuantizeMx` retained | **CONFIRM** |
| register-map refresh (own `mariana_plus` dir) | `arch-headers/mariana_plus/` exists; no own ISA dir | **CONFIRM** |
| the v4+ delta is handler/opcode-NEUTRAL (recompile + DGE + register-map) | exactly that — **NO** functional dispatch/handler/opcode/dtype change | **CONFIRM** |

Two DVE-specific observations (vs ACT, **not** deviations from the model): **(1)** the
`mariana-4062` errata is **RETAINED** on MPLUS DVE (it was *absent* on MPLUS ACT — it is a
DVE-specific silicon-errata patch, carried forward into v4+); **(2)** the HW-Decode dispatch
table @`0x800` is **byte-identical** on DVE (delta 0; only the Sunda table shifted `+0x1d`),
whereas MPLUS ACT had its whole DRAM table uniformly `+0x20` — an even tighter recompile.

**No CORRECTION to the v4+ model.** MARIANA_PLUS DVE is a pure **recompile + DGE fast-path**;
there is **no functional silicon delta** and **no ISA delta**. `engine_idx=3` (DVE) confirmed
via the ISA enum + the runtime-identity + `DVE perf mode support` strings. `[HIGH/OBSERVED]`

> **CORRECTION — minor backing-report address typo (not a model deviation).** The Sunda-mode
> **default arm** is `j 0x39dd` (decoded instruction-exact at MPLUS DVE DEBUG IRAM `0x3701`,
> and identically on MARIANA), not the `0x39fa` cited in one backing-report prose line. The
> handler/opcode/table facts are unaffected; the default-arm *target* is `0x39dd`.
> `[HIGH/OBSERVED — re-decoded at `0x3701` this pass]`

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct disasm, byte read, or shipped-header read this pass):

- 14 `MARIANA_PLUS_NX_DVE` getters (`nm` = 14, `nm -D` = 0; img-ptr/size immediates decoded
  14/14 at `.text 0x9b49a0..0x9b5520`); 8 real + 6 zero-size cursors → `NX_PE` IRAM. Engine
  order `ACT→DVE→PE` proven by 9/9 contiguity adjacencies. 8/8 carves byte-identical (sha256 +
  `cmp -s`) to `libnrtucode.a` member `.rodata`.
- Reset `06 7d 00 00` (`j 0x1f8`) — **SAME `+0x1c`, no further shift**; reset+boot stub +
  DRAM magic `0x6099cb34` + init block byte-identical to MARIANA DVE; boot decodes `const16
  a0,0; a0,144; jx a0` → `enter_run @0x90`.
- Dispatch (two DEBUG-IRAM sites @`0x2f09`/`0x36f8`): `addi −48`, `movi a3,186` (×2; `169` ×0),
  tables `0x800/0xaec`, log `0x80e58`, default `0x31e3` — byte-identical to MARIANA. HW-Decode
  table 187/187 byte-identical (delta 0, 82 real); Sunda table uniform `+0x1d` (82 real); `+7`
  opcodes `0x30/0x77/0x78/0xe0–e3` all REAL.
- Handler diff: strict 60==60 / glue 101==101, **`+0/−0`**; all 28 CAYMAN + all 7 MARIANA
  additions retained; glue trap documented + symmetric. 0 `Activate*` (no fold).
- DGE fast-path: 4 strings present on MPLUS (absent on MARIANA, count 0), dual-build
  (DEBUG+TEST); `push REGWRITE` 0/1 (retired); `BEGIN on mariana_plus`; `cayman/seq` ×4.
- PROF_CAM `ca588683` + PROF_TABLE `d72b339f` **byte-identical** (`cmp -s` clean) to MARIANA DVE.
- Dtype only `UINT32/INT32/FP32` (`move.cpp:41`); `QuantizeMx` retained; no FP4/MX strings.
- `mariana-4062` errata RETAINED (DVE-specific); ISA enum DVE=3; `arch-headers/mariana_plus/`
  exists, no own ISA dir; runtime-identity + `DVE perf mode support` strings present.
- Size table 6/8 distinct (IRAM **grew** in every variant — DGE fast-path bulk), 2/8 PROF
  byte-identical. MARIANA baseline re-carved + re-hashed (8/8 anchors MATCH).

**MED / INFERRED:**

- The per-opcode→handler-*body* binding (which opcode runs which handler body) — the
  FLIX-desync-limited frontier; table base/structure/real-count/`+0x1d`-Sunda-reloc are HIGH.
- Full per-new-handler IRAM `const16`↔DRAM-offset xref (FLIX-VLIW bundling absorbs the
  `const16`s); the handler-name cluster + the opcode-table real-target evidence is the primary
  proof (HIGH).
- The DGE fast-path *functional* reading (fused memcopy+transpose, SB-to-SB transpose, DMA
  credit-wait) — INFERRED-HIGH from the names + the shared DGE context (see dge-reshape.md).
- `engine_idx` computed at boot (=3 for DVE) — INFERRED from the string + the shared boot path.
- The PROF byte-identity is HIGH; the per-opcode CAM decode is carried from the baseline (the
  tables are byte-identical, so it applies verbatim).

**LOW / NOT CLAIMED:**

- Which silicon/runtime selects MARIANA vs MARIANA_PLUS, DEBUG vs PERF vs TEST.
- The exact DGE fast-path control-flow (decoded only at the string/symbol level).
- The exact `mariana-4062` errata semantics.
- Batch-norm internal-math equivalence (out of image-survey scope; handler/opcode stability is
  HIGH, math-body equivalence undetermined — same as the baseline).
- The per-handler operand layout of the DVE handlers.

---

## Cross-references

- **[MARIANA × DVE image](mariana-dve.md)** — the committed v4 baseline this page diffs against
  (the 35-handler roster, the 187-bound dispatch, the `+0x1c` reset, the unchanged engine model).
- **[MARIANA_PLUS × ACT image](mariana-plus-act.md)** — the sibling v4+ ACT image that set the
  recompile + DGE-fast-path model this page confirms (and refines to gen-wide).
- **[MAVERICK × DVE image](maverick-dve.md)** — the v5 step where the **ACT→DVE fold** finally
  happens (the absorption MARIANA_PLUS does **not** do, §5d).
- **[DGE Reshape Engine](../firmware/dge/dge-reshape.md)** — the full reshape/fast-path
  subsystem (`dge_decode_fast.cpp`, `memcopy_transpose_fast`, `sb2sb`, `wait_for_credit`) the
  §6 strings belong to, carved on the POOL image.
- **[MARIANA_PLUS Delta](../generations/mariana-plus-delta.md)** — the broader v4+ generation diff.
- **[Image Catalog Index](image-catalog-index.md)** — the full getter image catalog.
- **[The Confidence & Walls Model](../reference/confidence-model.md)** — the
  `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tags and the FLIX-desync ceiling.
