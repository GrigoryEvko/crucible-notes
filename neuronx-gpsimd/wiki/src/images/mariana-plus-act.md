# MARIANA_PLUS × ACT image (cross-gen diff vs MARIANA)

This page **starts the MARIANA_PLUS (v4+) engine matrix** by diffing the
MARIANA_PLUS Activation-engine firmware image against the committed, byte-true
[MARIANA × ACT baseline](./mariana-act.md). Every size, sha256, opcode, reset
byte, and string below is read directly from `libnrtucode_internal.so`
(sha256 `b7c67e89…632fc329b`) via its 14 `MARIANA_PLUS_NX_ACT_*_get` accessors,
carved from identity-mapped `.rodata`, with the shipped Cadence Vision-Q7
`ncore2gp` `xtensa-elf-objdump` decoding the flat blobs. The page default is
`[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

> **The one headline, up front — and it is counterintuitive, so this page proves
> it hard.**
> **MARIANA_PLUS ACT is BYTE-FOR-NAME IDENTICAL to MARIANA ACT at the
> handler/opcode/dtype/PROF layer.** The handler roster is `29 == 29` (`+0/−0`),
> the opcode-space compare-chain is byte-identical *at byte-identical addresses*,
> the PROF tables are byte-for-byte identical, and the dtype surface is unchanged.
> The v4+-vs-v4 delta is **a RECOMPILE + ONE functional addition — a DGE reshape
> fast-path** — with **NO new silicon**: no new dispatch handler, no new opcode,
> no new dtype. `MARIANA_PLUS = MARIANA-recompiled + the DGE fast-path`.

This is the *expected* result. MARIANA_PLUS shares the MARIANA ISA — there is no
`neuron_mariana_plus_arch_isa` dir; the four ISA dirs are
`cayman`/`mariana`/`maverick`/`sunda`, and MARIANA_PLUS carries only its own
`arch-headers/mariana_plus/` *register-map* dir. So the ISA/struct/dtype surface
**is** MARIANA's. The "v4+" label is a register-map refresh + a recompile + a DGE
optimization, not a model or ISA change. [HIGH/OBSERVED — ISA-dir listing is the
shipped artifact]

Related pages: [MARIANA × ACT (the diff base)](./mariana-act.md) ·
[MARIANA+ generation delta](../generations/mariana-plus-delta.md) ·
[DGE Reshape Engine](../firmware/dge/dge-reshape.md) ·
[DGE 3-Backend Selector](../firmware/dge/dge-backend-selector.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md) ·
[MAVERICK × ACT — the DVE fold](./maverick-act.md).

---

## 1. The delta table (MARIANA baseline → MARIANA_PLUS)

The whole page in one table. `(==)` marks invariant rows; the **bold** rows are
the *only* real changes. Read the [MARIANA page](./mariana-act.md) for the engine
*model* — this table documents the cross-gen delta, leading with the null
functional delta.

| PROPERTY | MARIANA ACT (baseline) | MARIANA_PLUS ACT (this page) | Δ |
|---|---|---|---|
| getters (`nm` / IDA sidecar) | 14 / 14 | 14 / 14 | `(==)` shape |
| real / empty images | 8 real + 6 zero-size cursors | 8 real + 6 zero-size cursors | `(==)` |
| packaging | flat IRAM/DRAM (not ELF) | flat IRAM/DRAM (not ELF) | `(==)` |
| reset vector | `06 7d 00 00` → `j 0x1f8` | `06 7d 00 00` → `j 0x1f8` | **`(==)` SAME +0x1c, no further shift** |
| 2nd vector | `86 7e 00 00` → `j 0x204` → `halt 0` | `86 7e 00 00` → `j 0x204` → `halt 0` | `(==)` byte-identical |
| boot path | `const16 a0,0x90 ; jx a0` → `enter_run@0x90` | same | `(==)` byte-identical |
| DRAM `.globstruct` magic | `0x6099cb34` + init block | `0x6099cb34` + init block | `(==)` byte-identical |
| dispatch model | raw-compare chain + `addx4` DRAM table | raw-compare chain + `addx4` DRAM table | `(==)` SEQ |
| compare-chain consts | `movi a3,{167…189}` @`0x2b06…` | **identical consts AT identical addrs** | `(==)` byte-exact |
| **HANDLER ROSTER** | 29 (incl. `Activate2`/`RandGet`/`RandSet`) | **29 IDENTICAL** | **`+0/−0`** |
| opcode space | (stable) | **NO growth** (DRAM table reloc only) | **`(==)`** |
| dtype string consts | `{UINT32,INT32,FP32}` | `{UINT32,INT32,FP32}` | `(==)` |
| FP4/CPTC as strings | absent | absent (0 hits ×8 carves) | `(==)` neg. |
| **PROF_CAM** | `326bc0dd` (24 armed records) | **`326bc0dd`** | **byte-IDENTICAL** |
| **PROF_TABLE** | `8786dd11` | **`8786dd11`** | **byte-IDENTICAL** |
| source tree | `…/cayman/seq/src/…` | `…/cayman/seq/src/…` | `(==)` dir name |
| engine self-name | `S: BEGIN on mariana` | `S: BEGIN on mariana_plus` | **rename** |
| **NEW vs prior gen** | `+Activate2/+Rand*` (vs CAYMAN) | **`+DGE fast-path code`** (vs MARIANA) | **new code** |
| **IRAM size (PERF)** | `0x11180` | `0x14be0` | **`+0x3a60` GREW** |
| DRAM size (PERF) | `0x2ba0` | `0x2cc0` | `+0x120` larger |
| DVE fold | standalone (contiguous-before-DVE) | standalone (contiguous-before-DVE) | `(==)` NO fold |

> **CORRECTION carried from [MARIANA ACT §7](./mariana-act.md#7-variant--cross-gen-size-table).**
> The MARIANA page's footnote ("MARIANA_PLUS ACT is **not** byte-identical to
> MARIANA ACT") is exactly right at the *blob* level — 6/8 images differ (full
> recompile). But that is **not** a functional delta: this page proves the
> dispatch/handler/opcode/dtype/PROF *surface* is unchanged. The blobs differ
> because they were recompiled and a DGE fast-path was inserted; the *engine* did
> not change. [HIGH/OBSERVED — resolves the IMG-08 §7 note in full]

---

## 2. The 14 MARIANA_PLUS × ACT getters + carve provenance

`CLS=NX`, `ENG=ACT`, `engine_idx=1`. `nm | rg -c 'MARIANA_PLUS_NX_ACT_[A-Z]+_[A-Z]+_get$'`
→ **14**; the IDA `_names.json` sidecar independently lists **14**. 8 carry real
bytes; 6 SRAM/EXTRAM getters emit `movq $0x0,(%rsi)` and alias the
contiguous-layout DVE cursor — ACT runs entirely from IRAM (code) + DRAM (data),
exactly as MARIANA. Each getter disassembles to the
`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret` stub.

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA == file off) | SIZE | sha256 |
|---|---|---|---|---|---|
| DEBUG | IRAM | `0x9b4e20` | `0x6af7e0` | `0x19da0` | `4df3ea4e…a8cabbd0` |
| DEBUG | DRAM | `0x9b4e40` | `0x6c9580` | `0x06560` | `4e3f02b0…2cdd84d2` |
| DEBUG | SRAM/EXTRAM | `0x9b4e60`/`0x9b4e80` | `0x6cfae0` (DVE cursor) | `0x0` | EMPTY |
| PERF | IRAM | `0x9b4920` | `0x5a5480` | `0x14be0` | `1569932e…5d2c7c97` |
| PERF | DRAM | `0x9b4940` | `0x5ba060` | `0x02cc0` | `0d1cfb62…9c6da711` |
| PERF | SRAM/EXTRAM | `0x9b4960`/`0x9b4980` | `0x5bcd20` (DVE cursor) | `0x0` | EMPTY |
| TEST | IRAM | `0x9b4ba0` | `0x62b180` | `0x14a80` | `8bb6db59…d23b5ce7` |
| TEST | DRAM | `0x9b4bc0` | `0x63fc00` | `0x03020` | `2161801c…91dfa7ff` |
| TEST | SRAM/EXTRAM | `0x9b4be0`/`0x9b4c00` | `0x642c20` (DVE cursor) | `0x0` | EMPTY |
| PROF | CAM | `0x9b54a0` | `0x868300` | `0x00400` | `326bc0dd…0d937e9a54` |
| PROF | TABLE | `0x9b54c0` | `0x868700` | `0x02000` | `8786dd11…1c7d233d` |

All 8 real carves (`internal.so[IMG-PTR : IMG-PTR+SIZE]`) reproduce these sha256
exactly, and each is **byte-identical** (sha256 + `cmp -s` clean) to the matching
`libnrtucode.a` member `.rodata` (`ar x` → `objcopy --only-section=.rodata`): the
archive ships exactly 14 `MARIANA_PLUS_NX_ACT` members (12 `img_*_contents.c.o` +
2 `hwdecode_*_PROF_{CAM,TABLE}_contents.c.o`), **8/8 reconciled** (not a
spot-check). One firmware corpus, two packaging views.

> **NOTE — ACT is the HEAD of the MARIANA_PLUS NX block.** The layout is
> variant-major, engine-minor (`ACT → DVE → PE → POOL → SP`), the same order as
> MARIANA. ACT is the family head: ACT `PERF_DRAM` ends at
> `0x5ba060 + 0x2cc0 = 0x5bcd20`, **exactly** where `MARIANA_PLUS_NX_DVE_PERF_IRAM.data`
> begins (and the same adjacency holds for TEST and DEBUG, 3/3). The whole
> MARIANA_PLUS block begins right after the last MARIANA blob:
> `MARIANA_NX_POOL_PROF_TABLE.data` (`0x5a3480`) is immediately followed by
> `MARIANA_PLUS_NX_ACT_PERF_IRAM.data` (`0x5a5480`) — the two generations are laid
> out contiguously (MARIANA fully, then MARIANA_PLUS). ACT is a **standalone**
> image, contiguous-before-DVE, **not folded** — same fold-disproof as MARIANA
> (the fold is MAVERICK-only).

---

## 3. The reset/boot vector — SAME +0x1c MARIANA shift, NO further shift

The reset region is **byte-identical across all 3 MARIANA_PLUS IRAM variants AND
byte-identical to MARIANA ACT** (`od -An -tx1 -N16`):

```
MARIANA      IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
MARIANA_PLUS IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
                    └ j 0x1f8 ┘         └ j 0x204 ┘            └ shared stub ┘
```

The `J` opcode byte-1 is `0x7d` on **both** generations (the MARIANA `+0x1c`
forward shift from CAYMAN's `0x76`), so MARIANA_PLUS inherits MARIANA's relocated
boot entry with **no further shift**. Decoded with the shipped `ncore2gp`
`xtensa-elf-objdump` off the MARIANA_PLUS PERF IRAM carve:

```text
0x000:  06 7d 00     j        0x1f8      ; primary reset vector → boot path
0x006:  86 7e 00     j        0x204      ; secondary vector → halt trap
0x1f8:  04 00 00     const16  a0, 0
0x1fb:  04 90 00     const16  a0, 144    ; a0 = 0x90  (C enter_run prologue)
0x1fe:  a0 00 00     jx       a0         ; jump into the C boot prologue
0x204:  00 52 00     halt     0          ; 2nd vector is a HALT trap
```

The DRAM head is `34 cb 99 60` = header word `0x6099cb34`, the `.globstruct`
dispatcher-state magic shared with every flat NX DRAM, **byte-identical** to
MARIANA across all 3 variants, with the same `@0x18:0x38` init block
(`4× 0x00001000 + 4× 0x00ffffff`). The reset + boot stub + DRAM init are a
recompile of the *same* boot scheme, not a re-relocation.

> **GOTCHA — the recompile signature is a literal constant, not a vector move.**
> The MARIANA_PLUS-vs-MARIANA PERF IRAM **shared prefix is `0x212` bytes** — the
> reset, boot trampoline, and the `addx4` dispatch trampoline are byte-identical —
> and the **first divergence is at `0x212`**, a `const16` literal-address constant
> (`0xc8` MPLUS vs `0x68` MARIANA). That is the recompile-relocation point: the
> boot/dispatch *scheme* is shared verbatim and the code only diverges where a
> relocated literal address appears. A binary patch would have left the literals
> alone; a recompile shifts them.

---

## 4. THE HEADLINE — handler roster is byte-for-name IDENTICAL (`+0/−0`)

The single most counterintuitive claim on the page, proven two independent ways
against both DEBUG DRAMs. Both methods agree: **nothing added, nothing removed.**

```c
// METHOD 1 (glue-stripped normalized): extract every "<glue>S: <OpName>",
//   strip the 0-3 string-pool glue chars (VS:/TS:/@S:), bare OpName, sort -u.
//   strings -n3 DRAM | rg -oP '[A-Za-z@]{0,3}S: [A-Z][A-Za-z_0-9]+'
//     | rg -oP 'S: \K[A-Z][A-Za-z_0-9]+' | sort -u
//   →  MARIANA_PLUS 70 tokens  ==  MARIANA 70 tokens   (ADDED=0, REMOVED=0)
//
// METHOD 2 (strict end-anchored single-token): the clean dispatch roster.
//   strings DRAM | rg -oP 'S: \K[A-Za-z][A-Za-z0-9_/-]*$' | sort -u
//   →  MARIANA_PLUS 29  ==  MARIANA 29                  (diff = EMPTY)
```

The 29-handler clean dispatch set — **identical on both generations**:

- **ACTIVATION-ENGINE-SPECIFIC:** `Activate`, `Activate2`, `ActivateQuantize`,
  `ActivationTableLoad`, `ActivationReadAccumulator`, `Cast`, `Copy`,
  `TensorScalar`
- **RNG (arrived on MARIANA, RETAINED on MARIANA_PLUS):** `RandGetState`,
  `RandSetState`
- **SHARED SEQ control/move core:** `AluOp`, `MOVE`, `WRITE`, `NOTIFY`, `BRANCH`,
  `BranchPrefetchHint`, `SET_OM`, `POLL_SEM`, `NOP`, `EngineNop`,
  `Event_Semaphore`, `INS_FL`, `INS_BREAK`, `EXT_BREAK`, `Halt`, `Redirect`,
  `STRONG_ORDER`, `TensorLoad`, `TensorStore`

MARIANA's three additions over CAYMAN (`Activate2`/`RandGetState`/`RandSetState`)
are **retained**, each confirmed a *real* dispatch handler by its string-load
entry site in MARIANA_PLUS DEBUG IRAM (the same self-naming pattern as MARIANA, at
near-identical addresses):

| Handler | DRAM string (file off) | IRAM entry (`const16 a10,…`) | MARIANA entry |
|---|---|---|---|
| `Activate2` | `S: Activate2` @`0x24a0` | `const16 a10,0x24a0` @`0xc0c0` | @`0xc0bd` (same neighborhood) |
| `RandGetState` | `S: RandGetState` @`0x2400` | `const16 a10,0x2400` @`0x1f77`/`0x2d4c`/`0x6993`/`0x6c0f` | @`0x1f77`/`0x2d4c`/`0x69b3`/`0x6c2f` |
| `RandSetState` | `S: RandSetState` @`0x23d0` | `const16 a10,0x23d0` @`0xbe60` | @`0xbe60` (byte-exact) |

> **GOTCHA — the string-pool prefix-glue diff-trap recurs identically.** A naive
> `^S:` match FALSELY reports `Cast`/`Copy`/`TensorScalar`/`ActivateQuantize`/
> `ActivationReadAccumulator`/`Activate2` as "removed": their DRAM log strings
> picked up a string-pool glued prefix with no intervening NUL — `VS: Cast`,
> `VS: Copy`, `TS: TensorScalar`, `TS: ActivateQuantize`,
> `@S: ActivationReadAccumulator`, `RS: Activate2`. **Both** generations exhibit
> the *same* glue, so the glue-stripped diff is robustly clean. Any Part-6 cross-gen
> handler diff MUST glue-strip.

---

## 5. The opcode space — gen-stable, NO growth

MARIANA_PLUS ACT uses the **same** dispatch model as MARIANA ACT: an
`addx4`-indexed jump-through-DRAM-table trampoline plus a 16-entry sub-table, with
the **raw-compare** normalization flavor (`movi a3,N ; bne a2,a3`) for the DEBUG
segment — **not** the `addi a2,a2,-65` ASCII style (DVE) nor the `sub a2,a2,a3`
base-subtraction style (SP). The sole `addi a2,a2,-65` in the image is at `0x165bd`,
far from dispatch (an unrelated inline char-decode).

The opcode-space stability is proven at the byte level. The DEBUG segmented
compare-chain constants are **byte-identical AND at byte-identical addresses** on
both generations (`ncore2gp` disasm, region `[0x2b06:0x2b8d]` `cmp`-clean):

```text
0x2b06:  movi a3,167   0x2b12:  movi a3,168   0x2b1e:  movi a3,169
0x2b2a:  movi a3,170   0x2b36:  movi a3,171   0x2b42:  movi a3,176
0x2b4e:  movi a3,177   0x2b5a:  movi a3,178   0x2b66:  movi a3,179
0x2b72:  movi a3,181   0x2b7e:  movi a3,184   0x2b8a:  movi a3,189
   ── identical opcodes at identical addresses on MARIANA and MARIANA_PLUS ──
```

The `addx4` trampoline (`extui a3,a3,0,4 ; addx4 a4,a3,a4 ; l32i a4,a4,0 ; jx a4`,
sub-table base `const16 a4,0x1250`) is byte-identical in structure, and the
`S: Dispatch opcode=0x%x` per-fetch log is at DRAM file offset `0x858` on **both**
generations.

> **GOTCHA — the DRAM dispatch-table "+0x20 reloc" is a recompile shift, not a
> handler change.** The IRAM-target trampolines reached through the DRAM table are
> uniformly relocated `MARIANA = MARIANA_PLUS + 0x20` — a clean recompile shift, so
> there is **no opcode-space growth** (contrast the genuine PE `25→29` and DVE
> `170→187` growth on other engines). The table base, entry structure, and the
> slot-by-slot `+0x20` relocation are gen-stable; the *per-opcode→handler row
> binding* is the FLIX-desync-limited frontier carried from
> [MARIANA ACT §5](./mariana-act.md#5-dispatch--dtype-deltas) (FW-00 bundle
> desync). [HIGH model/base; MED per-row binding]

The SEQ infrastructure confirms the engine model is unchanged: the identical
`ErrorHandler` arms (`Bad Opcode` / `Illegal Instruction` / `FP Error` /
`Int Div Zero`, sourced from `…/cayman/seq/src/handlers/exception_handler.hpp`),
the dual `S: NX in HW Decode mode` / `S: NX in Sunda mode` strings, the
`sunda_fetch`/`sunda_redirect`/`sunda_handle_surprises` fetch machinery, and the
`S: enter_run: start/done` markers are all byte-for-name present.

---

## 6. dtype + PROF deltas — minimal dtype, PROF byte-IDENTICAL

**dtype: unchanged, minimal.** The only dtype constants in any MARIANA_PLUS ACT
image are `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` in the byte-identical
`move.cpp:41` assertion (*"highest priority is full-register moves. TODO other
dtypes"*) — the same three MARIANA carries. `FP4`/`CPTC`/`MXTENSOR`/`SFP8` →
**0 hits across all 8 MARIANA_PLUS ACT carves**. The MARIANA dtype expansion
(`FP4_EXP2`, `CPTC1..7`) is numeric — handled by integer comparison in the
`Cast`/activation-table path, never as named-string asserts — so it stays invisible
at the ACT-image layer on both v4 and v4+. [HIGH/OBSERVED-negative]

**PROF: byte-for-byte IDENTICAL to MARIANA.** Both profiling blobs are `cmp -s`
clean against the MARIANA ACT tables:

- **PROF_CAM** `326bc0dd…d937e9a54` (`0x400`) — **24 armed 16-byte records**
  `{opcode(u32), mask=0xff, enable=1, rsvd}`, opcode set
  `{0x21,0x22,0x23,0x24,0x25,0x43,0x44,0x46,0x47,0x77,0x78,0x9f,0xa0,0xa1,0xa2,0xa3,0xa5,0xa7,0xa8,0xa9,0xaa,0xab,0xb1,0xb2}`
  — the *same* MARIANA-re-armed set, carried forward verbatim.
- **PROF_TABLE** `8786dd11…1c7d233d` (`0x2000`) — identical preallocation.

This is gen-wide: all four MARIANA_PLUS PROF-bearing engines reuse MARIANA's
per-engine PROF_CAM byte-identical (DVE `ca588683`, PE `43475cec`, POOL `0951b326`,
ACT `326bc0dd`). MARIANA_PLUS **reuses MARIANA's HW-decode profiler config
unchanged** — a strong v4/v4+ kinship signal, and confirmation that the recompile
did not re-arm anything.

---

## 7. The ONE functional delta — a DGE reshape fast-path

The only substantive image-level change distinguishing MARIANA_PLUS from MARIANA is
**a new DGE (Descriptor-Generation Engine) fast-path**, evidenced by 4 new
source/helper strings present on MARIANA_PLUS ACT and **absent on MARIANA ACT**
(`comm -13`, both DEBUG DRAMs):

| String (MARIANA_PLUS-only) | kind |
|---|---|
| `dge_decode_fast.cpp` | new source file |
| `dge_reshape_memcopy_transpose_fast` | new DGE reshape helper |
| `tensor_reshape_transpose_sb2sb` | new SB→SB transpose reshape kind |
| `wait_for_credit` | new DMA credit-flow primitive |

These are **source/helper names, NOT dispatch handler names** — the handler set is
unchanged (§4). They *refine* the existing DGE/Reshape subsystem, which is present
on **both** generations (shared strings `dge_backend_rtl`, `dge_reshape.cpp`,
`addr_bits.hpp`, `transform_addr_for_pool_tpb_rerouting`, the
`S: DGE Reshape: …` / `S: DGE: Select backend Pool/RTL` logs all confirmed present
on both). They also appear in the symbol-bearing MARIANA_PLUS TEST build, so they
correspond to real compiled code, not stray text. [HIGH/OBSERVED strings + absence]

The reverse delta is trivial: MARIANA carries `S: push REGWRITE to DMA[%d]`
(absent on MARIANA_PLUS, likely folded into the fast path), which is why the DEBUG
DRAM `S:` line count drops `153 → 152`. There is **no `mariana_plus` errata
string** (the `mariana-4062` errata is DVE-specific and absent here too).

```c
// The v4+ change, in one line of pseudocode (INFERRED-HIGH from the names +
// the shared DGE context — see firmware/dge/dge-reshape.md):
//
//   if (dge_decode_fast(req) && is_sb2sb_transpose(req)) {
//       tensor_reshape_transpose_sb2sb(req);   // NEW: a fast reshape/transpose
//       wait_for_credit(dma_ring);             // NEW: explicit DMA credit-wait
//   } else {
//       analyze_tensor_reshape(req);           // the SHARED slow path (both gens)
//   }
```

This is an **internal throughput optimization** to the descriptor-generation
machinery — a "fast" reshape/memcopy-transpose path with an SB-to-SB transpose kind
and an explicit DMA credit-wait. It is *not* a new dispatch handler, *not* a new
opcode, *not* a new dtype. It accounts for the IRAM **growth** (§8) — the bulk of
the `+0x3a60` PERF-IRAM expansion. The exact control-flow is decoded only at the
string/symbol level. [strings OBSERVED; "DGE fast-path optimization" INFERRED-HIGH]

---

## 8. The recompile evidence — size table + directional growth

| IMAGE | MARIANA sz / sha | MARIANA_PLUS sz / sha | ΔSize | identical? |
|---|---|---|---|---|
| DEBUG_IRAM | `0x18ba0` / `f27afe6b` | `0x19da0` / `4df3ea4e` | `+0x1200` | NO (recompile+DGE) |
| DEBUG_DRAM | `0x63c0` / `28b6bbe9` | `0x6560` / `4e3f02b0` | `+0x1a0` | NO |
| PERF_IRAM | `0x11180` / `78608c91` | `0x14be0` / `1569932e` | `+0x3a60` | NO |
| PERF_DRAM | `0x2ba0` / `05a5b8d9` | `0x2cc0` / `0d1cfb62` | `+0x120` | NO |
| TEST_IRAM | `0x11240` / `c5c5adce` | `0x14a80` / `8bb6db59` | `+0x3840` | NO |
| TEST_DRAM | `0x2e60` / `b3bcccc3` | `0x3020` / `2161801c` | `+0x1c0` | NO |
| **PROF_CAM** | `0x400` / `326bc0dd` | `0x400` / `326bc0dd` | `+0x0` | **YES** |
| **PROF_TABLE** | `0x2000` / `8786dd11` | `0x2000` / `8786dd11` | `+0x0` | **YES** |

**6/8 distinct (full recompile); 2/8 (PROF) byte-identical.** The directional
signature is telling: **IRAM GREW in every variant** (PERF `+0x3a60`, TEST
`+0x3840`, DEBUG `+0x1200`) — the **opposite** direction from the CAYMAN→MARIANA
shrink ([MARIANA ACT §7](./mariana-act.md#7-variant--cross-gen-size-table)). The
growth is the new DGE fast-path code (§7). Positional 16-byte-block similarity is
low (PERF 6 %, TEST 5 %, DEBUG 14 %) — a fresh build with relocated layout +
inserted code, **not** a patch. The PERF IRAM decodes a genuine, *larger*
`cayman/seq` sequencer (`123 entry / 188 retw / 500 call8 / 193 distinct
mnemonics`, vs MARIANA's `138 entry / 185 retw`).

> **NOTE — `engine_idx` is still runtime-computed (= 1, ACT).** The shipped ISA
> enum `NEURON_ISA_TPB_NEURON_ENGINE { PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4,
> TOP_SP=5 }` (`aws_neuron_isa_tpb_common.h:139-146`) fixes ACT = 1. MARIANA_PLUS
> ACT carries the runtime-identity string `S: engine_base_addr=%llx
> tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` exactly as MARIANA —
> the same flat image is loaded at the ACT base and self-locates `engine_idx` at
> boot, which is why all NX engines share the identical reset + boot trampoline.
> [HIGH/OBSERVED string; runtime-compute INFERRED]

---

## 9. What this sets up for the MARIANA_PLUS matrix (IMG-14…17)

The ACT instance establishes the v4+-vs-v4 pattern: **recompile (relocated layout,
the SAME `+0x1c` reset, `BEGIN on mariana_plus`, `cayman/seq` source tree) + the DGE
fast-path + PROF tables reused byte-identical from MARIANA + a register-map
refresh** (the `arch-headers/mariana_plus/` dir), with **handler/opcode/dtype
surfaces == MARIANA per engine**. The v4+ delta is image/handler-level-NEUTRAL: it
is an optimization + a register-map refresh, **not** a functional engine change.
The expectation for the remaining MARIANA_PLUS engines is that each will show the
same null functional delta plus the DGE fast-path (a SEQ-infra feature, likely
gen-wide). [INFERRED-HIGH from the ACT instance — to be VERIFIED per-engine]

---

## 10. Adversarial self-verification

Five strongest claims, challenged against the binary:

1. **Handler roster byte-for-name identical (`+0/−0`).** *Challenge:* a glue
   artifact could mask a real add/drop. *Re-verify:* two independent methods —
   glue-stripped `70 == 70`, strict end-anchored `29 == 29`, `comm`/`diff` both
   EMPTY in both directions; the 29-token roster matches the
   [MARIANA baseline §4](./mariana-act.md#4-the-handler-delta-the-cross-gen-headline)
   exactly. **HOLDS.**
2. **No opcode-space growth.** *Challenge:* the DRAM table could hide new entries.
   *Re-verify:* DEBUG compare-chain region `[0x2b06:0x2b8d]` is `cmp`-clean between
   gens (same `movi a3,{167…189}` at the same addresses); the DRAM trampolines are a
   uniform `+0x20` recompile shift, no count growth (contrast PE `25→29`). **HOLDS**
   (per-row binding remains the documented MED frontier).
3. **SAME +0x1c reset, no further shift.** *Challenge:* a second relocation could
   hide in a later variant. *Re-verify:* all 3 IRAM variants head `06 7d 00 00 …
   86 7e` — byte-identical to MARIANA; `ncore2gp` decodes `j 0x1f8`/`j 0x204`;
   PERF-IRAM shared prefix `0x212`, first divergence a relocated `const16` literal.
   **HOLDS.**
4. **PROF byte-identical to MARIANA.** *Challenge:* same size could mask different
   content. *Re-verify:* `cmp -s` clean for both CAM (`326bc0dd`) and TABLE
   (`8786dd11`); the 24 armed records match the MARIANA set. **HOLDS.**
5. **DGE fast-path is the one functional addition.** *Challenge:* the 4 strings
   could be stray text, or present on MARIANA too. *Re-verify:* all 4
   (`dge_decode_fast.cpp`, `dge_reshape_memcopy_transpose_fast`,
   `tensor_reshape_transpose_sb2sb`, `wait_for_credit`) → MARIANA_PLUS=1,
   MARIANA=0; the shared DGE strings are present on both; the new names recur in the
   symbol-bearing TEST build. **HOLDS** (strings OBSERVED; optimization reading
   INFERRED-HIGH).

**Honesty ledger.** *HIGH/OBSERVED:* 14 getters (`nm` + IDA sidecar = 14); 8 carves
sha-reproduced + `cmp`-reconciled 8/8 to `libnrtucode.a`; MARIANA baseline 8/8
MATCH; reset/boot/DRAM-magic byte-identical to MARIANA; handler
`29==29`/`70==70`; compare-chain byte-identical at identical addrs; PROF
`cmp`-clean; dtype `{UINT32,INT32,FP32}` only, FP4/CPTC 0 hits ×8; 4 DGE strings
present-vs-absent; IRAM grew, PROF identical; `BEGIN on mariana_plus`; ISA enum
ACT=1. *MED/INFERRED:* per-opcode→handler DRAM row binding (FLIX-desync frontier);
"DGE fast-path optimization" semantics (string/symbol level); "DGE fast-path is
gen-wide" (from the ACT instance). *LOW/NOT CLAIMED:* `Activate2` vs `Activate`
semantics; which runtime selects MARIANA vs MARIANA_PLUS; activation FUNCTION
coefficients (host-supplied LUT); the exact DGE fast-path control-flow.
