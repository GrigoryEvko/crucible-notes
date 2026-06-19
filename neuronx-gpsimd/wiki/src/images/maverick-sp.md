# MAVERICK × SP image — the degenerate lower bound that completes the v5 matrix

This page **closes the MAVERICK (v5 / NC-v5) engine matrix** by carving the
MAVERICK Sync-Processor firmware image (`TPB_SP`, `engine_idx = 4`) and diffing it
against the committed, byte-true [MARIANA_PLUS × SP baseline](./mariana-plus-sp.md).
SP is the **leanest** NX sequencer — a pure scalar/sync control core with **no Q7
compute, no PROF, no EXTISA** — which makes it the *sharp test* of the v5 model, as
it was of the v4+ model. On MARIANA_PLUS, SP was the engine that proved the DGE
fast-path was **gen-wide ADDED**; on MAVERICK, SP is the mirror image — it proves
the fast-path is **gen-wide DROPPED**.

Everything below is read from `libnrtucode_internal.so`
(sha256 `b7c67e89…632fc329b`) via its **8** `MAVERICK_NX_SP_*_get` accessors,
carved from identity-mapped `.rodata`, with the shipped Cadence Vision-Q7
`ncore2gp` `xtensa-elf-objdump` decoding the flat blobs, plus the shipped
`neuron_maverick_arch_isa` clean ISA enum for the engine taxonomy.

> **THE EPISTEMIC WALL — read before every claim below.**
> **MAVERICK (v5) is HEADER-OBSERVED only.** Its accessors, getter sizes, carved
> blobs, reset/boot bytes, dispatch-encoding bytes, DRAM magic + init block, string
> presence/absence and block-similarity are all byte-grounded and tagged
> `OBSERVED` — and on SP the *carved reset bytes and the 8-getter count ARE
> OBSERVED*. But v5 **deep interior** — the exact per-opcode→trampoline row binding
> on the SP DRAM table, the inlined fetch/sync control-flow under the SRAM
> residence — is **not** byte-resolved (FLIX-VLIW desync frontier, SX-FW-00) and is
> tagged **`INFERRED`** wherever it appears. The 18-handler *retention* is
> `INFERRED-HIGH` (the only carrier of the named `S:` roster — the DEBUG image — is
> dropped). `arch_id 36` is `INFERRED` (`coretype = arch_id + 1`; no NCFW v5 image
> exists). Q7_CC_TOP is FILE-ABSENT on v5. Do **not** read any v5-interior
> statement as fact. [WALL]

> **THE HEADLINE — six OBSERVED v5 deltas + the decisive contrast.**
> MAVERICK SP is the **same** lean single-NX-core `cayman/seq` sync/control
> sequencer as MARIANA_PLUS SP — rebuilt for v5 as a **genuine new generation, not
> a recompile**. Six image-level deltas, **each OBSERVED**:
> 1. **SP runs from SRAM, not IRAM.** The IRAM getters return **size 0**; the code
>    lives in the SRAM blob (PERF `0xf580` / TEST `0xf6c0`). The SP/Q7 v5 SRAM
>    anomaly. `[HIGH/OBSERVED]`
> 2. **The DEBUG image is DROPPED.** **8** getters, not MARIANA_PLUS SP's 12 — no
>    `MAVERICK_NX_SP_DEBUG_*` (`nm` = 0), no PROF (`nm` = 0). The named `S:` handler
>    roster is **gone firmware-wide on SP** (`^S: ` count `0` across all 4 images vs
>    141 on the MPLUS SP DEBUG DRAM). `[HIGH/OBSERVED]`
> 3. **The DGE fast-path is DROPPED region-wide — the decisive contrast.** All four
>    `dge_decode_fast` strings = **0** (PRESENT, `1` each, on MARIANA_PLUS SP).
>    `[HIGH/OBSERVED]`
> 4. **v5 reset geometry.** SRAM head `06 78 / 86 79` → `j 0x1e4` / `j 0x1f0`, boot
>    → **`enter_run @0x94`** (the +4 v5 signature; MARIANA's was `@0x90`), via the
>    SP-specific **"Top-Sync" stub at +0xc** vs the DVE/PE/POOL `j 0x1d8`.
>    `[HIGH/OBSERVED]`
> 5. **≈62 % smaller.** 4 real images `0x23840` vs MARIANA_PLUS SP's 6 images
>    `0x5d9c0`. `[HIGH/OBSERVED]`
> 6. **An independent v5 build** — 5.9 % positional 16-byte block similarity to its
>    MARIANA_PLUS twin, diverging at **byte 1** (`78` vs `7d`). `[HIGH/OBSERVED]`
>
> **The dispatch MECHANISM is RETAINED byte-exact:** base-subtraction
> `sub a2,a2,a3` (enc **`3022c0`** — byte-identical to MARIANA_PLUS SP),
> `srli a2,a2,6` segmentation, table base file `0x800`, `.globstruct` magic
> `0x6099cb34` + init block byte-identical. `[HIGH/OBSERVED]`

`MAVERICK SP = the MARIANA_PLUS SP lean sync/control chassis, independently
rebuilt for v5: SRAM-resident, DEBUG-dropped, DGE-fast-path-dropped, ≈62 %
smaller, new reset geometry — with the dispatch mechanism, the lean handler
surface and numeric dtype ALL retained, and no new SP-side opcodes.` **The
degenerate lower bound of the v5 model.**

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. The engine *model* (the carve mechanics, the SP
sync/control role, the 18-handler 5-way intersection, the EVT_SEM/barrier
mechanism, the SP-vs-`TOP_SP` resolution) is derived once in
[mariana-plus-sp.md](./mariana-plus-sp.md) /
[mariana-sp.md](./mariana-sp.md) and **not** re-derived here — this is a **DIFF**
page.

Related pages: [MARIANA_PLUS × SP (the diff base)](./mariana-plus-sp.md) ·
[MAVERICK × ACT (matrix start, ACT→DVE fold)](./maverick-act.md) ·
[MAVERICK × DVE](./maverick-dve.md) · [MAVERICK × PE](./maverick-pe.md) ·
[MAVERICK × POOL](./maverick-pool.md) ·
[MAVERICK generation profile](../generations/maverick-profile.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md) ·
[Collective end-to-end (TOP_SP, engine 5)](../orientation/collective-end-to-end.md) ·
[Per-Engine Firmware Depth](../uarch/per-engine-depth.md).

> **NOTE — the objects used.** Container
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64
> x86-64 DYN, **not stripped**, re-hashed this session). First R `LOAD` is the
> identity map (`off 0x0 == vaddr 0x0`), so each `<NAME>.data` accessor address is
> simultaneously the `.rodata` VA and the file offset of its blob — carve =
> `so[ptr : ptr+size]`. **SRAM/IRAM segment-VA == device VA within that segment**
> (reset vector at byte 0 of the *resident* segment — on SP that is **SRAM**, since
> IRAM is empty); **DRAM string-file-offset == device DRAM VA − `0x80000`**; the SP
> boot stub + dispatch table resolve to **SRAM device base `0x04100000`** (boot
> `const16 a0,0x410`; trampolines `0x0410xxxx`). Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, Xtensa Tools 14.09;
> `--version` exit 0). **MAVERICK is internal.so-EXCLUSIVE:** `libnrtucode.a`
> (435 members = **420 image** [CAYMAN 124 / MARIANA 124 / MARIANA_PLUS 124 / SUNDA 48 / MAVERICK 0]
> **+ 15 framework `.c.o`**) carries **0** MAVERICK members,
> so unlike the MARIANA_PLUS SP 6/6 `.so`↔`.a` reconciliation there is **no**
> second-source byte-identity check — the carve is single-source. The shipped C
> ISA header `neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h` is cited for
> the engine enum. `[HIGH/OBSERVED]`

---

## 1. The delta table (MARIANA_PLUS SP baseline → MAVERICK SP)

The whole page in one table. `(==)` marks an invariant row; the **bold** rows are
the v5 deltas. Read the [MARIANA_PLUS SP page](./mariana-plus-sp.md) for the engine
*model* — this table documents the cross-gen delta, leading with the six OBSERVED
v5 deltas and the DGE-fast-path-dropped contrast. Every row re-verified this
session against fresh carves from `libnrtucode_internal.so`.
`[HIGH/OBSERVED unless tagged]`

| PROPERTY | MARIANA_PLUS SP ([baseline](./mariana-plus-sp.md)) | MAVERICK SP (this page) | Δ |
|---|---|---|---|
| getters (`nm`) | 12 (6 real + 6 cursor) | **8 (4 real + 4 cursor)** | **−4: no DEBUG getter, no PROF** |
| DEBUG image | present (`DEBUG_{IRAM,DRAM}`) | **DROPPED** (`MAVERICK_NX_SP_DEBUG_*` `nm` = 0) | **NEW v5 (matches PE/POOL)** |
| code residence | **IRAM** | **SRAM** (IRAM getters size 0) | **NEW v5 (the SP/Q7 SRAM anomaly)** |
| packaging | flat IRAM/DRAM (not ELF) | flat **SRAM**/DRAM (not ELF) | residence-shift |
| cores | single NX (no Q7) | single NX (no Q7) | `(==)` |
| reset vector (primary) | `06 7d` → `j 0x1f8` | **`06 78` → `j 0x1e4`** | **−0x14 (Top-Sync stub)** |
| secondary vector | `86 7e` → `j 0x204` | **`86 79` → `j 0x1f0`** | **−0x14** |
| boot → enter_run | `const16 a0,0x90` → `enter_run @0x90` | **`const16 a0,0x94`** → **`enter_run @0x94`** | **+4 (the v5 signature)** |
| boot SRAM base literal | n/a (IRAM offsets) | **`const16 a0,0x410`** (SRAM base `0x04100000`) | SRAM-residence |
| DRAM `.globstruct` magic | `0x6099cb34` | `0x6099cb34` | `(==)` |
| `.globstruct` init `[0x18:0x38]` | `4×0x1000` + `4×0xffffff` | **byte-identical** | `(==)` |
| dispatch flavor | seg/Sunda, `sub a2,a2,a3` (`3022c0`) | seg/Sunda, `sub a2,a2,a3` (`3022c0`) | `(==)` **enc byte-identical** |
| dispatch-head `sub` site | `@0x286c` (IRAM) | `@0xe326` (SRAM) | reloc (code IRAM→SRAM) |
| segmentation | `srli a2,a2,6` | `srli a2,a2,6` (`@0x233a`) | `(==)` |
| DRAM table base | file `0x800` | file `0x800` | `(==)` |
| table trampolines | IRAM offsets (`0xb0xx`/`0xd2xx`) | **SRAM-absolute `0x0410xxxx`** (40/40) | residence-reloc; default `0x0410857d` |
| size-class byte array | — | `@0x8a0`: `01 08 01 01 02 02 02 02 04 04 04 04 08 01 01 01` | observed |
| **handlers** | **18 (5-way intersection)** | **lean sync/control core RETAINED** (strings amputated) | `(==)` surface, `INFERRED-HIGH` |
| named `S:` roster | present (DEBUG DRAM, 141 lines) | **0** (no DEBUG image) | **amputated** |
| new v5 opcodes on SP | n/a | **NOT bound** (DMA/control ride compute engines) | `(==)` SP surface |
| opcode space | ~161 seg table | seg table, **STABLE** (no SP growth) | `(==)` |
| **DGE fast-path** (4 strings) | **PRESENT** (`1` each, DEBUG + TEST DRAM) | **DROPPED** (`0` each, region-wide) | **THE DECISIVE CONTRAST** |
| `dge_shape` (shared infra) | present | present (SP `2` / region `7`) | `(==)` |
| PROF | none (no SP instance) | none (no re-author possible) | `(==)` |
| Q7 / EXTISA | none | none (0 ELF magic) | `(==)` |
| dtype constants | `UINT32`/`INT32`/`FP32` | `UINT32`/`INT32`/`FP32` (`move.cpp:41`) | `(==)` |
| FP4/MX/CPTC/SFP8/INT4 strings | 0 | 0 | `(==)` |
| `translate_cayman+.hpp` | present | present (`grep` 1) | `(==)` |
| `mariana-4062` errata | absent (DVE-only) | absent (region-wide v5 errata drop) | `(==)` |
| engine self-name | `S: BEGIN on mariana_plus` | **none** (no DEBUG image) | amputated |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` (`grep` 6) | `(==)` |
| code size (PERF) | `0x1c300` (IRAM) | **`0xf580`** (SRAM) | **−0xcd80** |
| code size (TEST) | `0x1a8a0` (IRAM) | **`0xf6c0`** (SRAM) | **−0xb1e0** |
| total footprint | `0x5d9c0` (6 imgs) | **`0x23840`** (4 imgs) | **≈ −62 %** |
| build relation | recompile-relocated (0xa2 prefix) | **INDEPENDENT** (5.9 % blocks, byte-1 div.) | **genuine new gen** |
| `engine_idx` | 4 (`TPB_SP`, runtime) | 4 (`TPB_SP`, runtime) | `(==)` |

The diff reduces to **a genuine independent v5 rebuild** that moves the code to
SRAM, drops the DEBUG image, drops the DGE fast-path, re-laps the reset geometry to
`enter_run @0x94`, and shrinks ≈62 % — while keeping the base-subtraction dispatch
mechanism, the lean sync/control surface, the `.globstruct` state and numeric dtype
byte-stable. `[HIGH/OBSERVED]`

---

## 2. Carve + sha — 8 getters, 4 real, single-source

`nm` lists exactly **8** `MAVERICK_NX_SP_*_get` symbols (`nm | rg -c
'MAVERICK_NX_SP_.*_get$'` = 8) — **NO** `DEBUG`
(`…NX_SP.*DEBUG` = 0), **NO** `PROF` (`…NX_SP.*PROF` = 0). This is the **getter-shape
gen-step**: MARIANA_PLUS SP shipped 12 = PERF/TEST/**DEBUG** × {IRAM,DRAM,SRAM,EXTRAM}
(6 real); MAVERICK SP ships 8 = **PERF/TEST only** (4 real). As on MAVERICK PE/POOL
([pe](./maverick-pe.md), [pool](./maverick-pool.md)), the DEBUG image — the only
carrier of the named `S:` handler roster — is dropped. The eight `(img-ptr, size)`
stubs disassemble instruction-exact and match the catalog
([image-catalog-index.md](./image-catalog-index.md)) rows: `[HIGH/OBSERVED]`

| VARIANT | REGION | IMG-PTR (==fileoff) | SIZE | STATUS |
|---|---|---|---:|---|
| PERF | IRAM | `0x8eeae0` | `0x0` | **EMPTY** (IRAM unused — SP runs from SRAM) |
| PERF | DRAM | `0x8eeae0` | `0x24c0` | REAL (`.globstruct` + dispatch table) |
| PERF | SRAM | `0x8f0fa0` | `0xf580` | **REAL — the SEQ CODE (the anomaly)** |
| PERF | EXTRAM | `0x900520` | `0x0` | EMPTY (cursor → SP_TEST) |
| TEST | IRAM | `0x900520` | `0x0` | EMPTY |
| TEST | DRAM | `0x900520` | `0x2740` | REAL |
| TEST | SRAM | `0x902c60` | `0xf6c0` | **REAL — SEQ CODE** |
| TEST | EXTRAM | `0x912320` | `0x0` | EMPTY (cursor → Q7_POOL head) |

The four zero-size IRAM/EXTRAM getters all execute `movq $0x0,(%rsi)`; their `lea`
resolves to the contiguous-layout cursor. The four **real** carves (identity
`.rodata`), re-hashed this session — **all four match the backing carve and the
catalog**: `[HIGH/OBSERVED]`

| IMAGE | SIZE | sha256 (MAVERICK SP, this session) |
|---|---:|---|
| `MAV_SP_PERF_DRAM` | `0x24c0` | `d5d3ba2d80aa87b00faffac764716dd8c2e72326af9a8a47d390a0c7566db9d2` |
| `MAV_SP_PERF_SRAM` | `0xf580` | `08e3594546fce8c8f84503617015a5061313f4a03d74193283ae996b0d8b8d63` |
| `MAV_SP_TEST_DRAM` | `0x2740` | `c2b2436a4b231ff2929b80a630876314e82456a44a49a40bf96d3204fe2995f5` |
| `MAV_SP_TEST_SRAM` | `0xf6c0` | `27908ff6347e9135543b4f68364b0b502f4a128b7b100b0e58fa73d05bf3711c` |

The **MARIANA_PLUS SP baseline** (the diff target) was re-carved + re-hashed this
session at its IMG-17 offsets — **all 6 anchors MATCH** the committed
[mariana-plus-sp.md](./mariana-plus-sp.md): `PERF_IRAM 3a815569`,
`PERF_DRAM 79c2e2fa`, `TEST_IRAM 11527767`, `TEST_DRAM c9aaac9f`,
`DEBUG_IRAM 84ee1c05`, `DEBUG_DRAM 2958154e`. The diff below is against authentic
MARIANA_PLUS SP. `[HIGH/OBSERVED]`

> **GOTCHA — `nm | rg -c` double-counts getters 2×.** Each getter contributes two
> `nm` lines: the `.text` stub (`t MAVERICK_NX_SP_…_get`) **and** its blob symbol
> (`r MAVERICK_NX_SP_…_get.data`). A naïve `rg -c 'MAVERICK_NX_SP.*_get'` returns
> **16**; the count of distinct getters is the `_get$`-anchored sweep = **8**. The
> `.data` symbol addresses are the IMG-PTRs (`0x8eeae0`/`0x8f0fa0`/`0x900520`/
> `0x902c60`/`0x912320`). `[HIGH/OBSERVED]`

---

## 3. Engine ordering — SP is the terminal MAVERICK NX engine

The MAVERICK `.rodata` layout is **VARIANT-MAJOR, ENGINE-MINOR**, and with ACT
amputated ([maverick-act.md](./maverick-act.md)) the NX order is
**DVE → PE → POOL → SP**, the same terminal position SP held on MARIANA_PLUS. Read
from `nm` `.data` addresses, with 4/4 contiguity proofs (re-verified this session):
`[HIGH/OBSERVED]`

```text
… POOL_TEST_DRAM @0x8ec320  <  SP region @0x8eeae0        (POOL precedes SP)        ✓
SP_PERF_SRAM @0x8f0fa0  + 0xf580 = 0x900520 == SP_TEST start                        ✓
SP_TEST_SRAM @0x902c60  + 0xf6c0 = 0x912320 == SP_TEST_EXTRAM cursor                ✓
SP_TEST_EXTRAM cursor 0x912320 == MAVERICK_Q7_POOL_PERF_DRAM head @0x912320         ✓
```

SP's TEST block ends exactly at the `MAVERICK_Q7_POOL_PERF_DRAM` head — SP **precedes**
the Q7_POOL compute core; SP is the last NX sequencer, exactly as on MARIANA_PLUS.
`[HIGH/OBSERVED]`

---

## 4. The reset/boot diff — the v5 enter_run @0x94 + the SP "Top-Sync" stub

The SP **SRAM** is a flat device segment (no ELF magic), single-NX-core packaging;
the reset vector sits at byte 0 of the SRAM blob (the *resident* code segment, since
IRAM is empty). Decoded instruction-exact with `ncore2gp` (exit 0): `[HIGH/OBSERVED]`

```text
SP SRAM head (12 B):  06 78 00 00 00 00 86 79 00 00 00 00
0x000  06 78 00     j 0x1e4    ; primary reset → boot trampoline
0x006  86 79 00     j 0x1f0    ; secondary    → halt
0x1e4  041004 const16 a0,0x410 ; 049400 const16 a0,148(0x94) ; a00000 jx a0  → enter_run @0x94
0x1f0  005200 halt 0           ; secondary = HALT trap
enter_run @0x94: const16 a2,0x410 ; const16 a2,0xe086 ; beqz a2,0xa0 ; callx0 a2
                 ; builds the SRAM-absolute 0x0410e086 and calls it (real boot body)
DRAM head:  34 cb 99 60   (.globstruct magic 0x6099cb34, byte-identical both gens)
```

**The precise v5 reset map** (all three decoded with `ncore2gp` this session):
`[HIGH/OBSERVED]`

```text
ENGINE            reset bytes      primary    secondary  enter_run
MARIANA_PLUS SP   06 7d / 86 7e    j 0x1f8    j 0x204    @0x90
MAVERICK DVE/PE   06 75 / 86 76    j 0x1d8    j 0x1e4    @0x94   (−0x20 primary)
MAVERICK SP       06 78 / 86 79    j 0x1e4    j 0x1f0    @0x94   (−0x14 primary, +0xc vs DVE)
```

The unifying v5 marker is **`enter_run @0x94`** (the +4 over MARIANA's `@0x90`),
shared by every MAVERICK NX engine. SP does **not** share the DVE/PE/POOL
`j 0x1d8` / `−0x20` reset target: SP resets `j 0x1e4 / j 0x1f0` — **−0x14** from
MARIANA_PLUS SP (`0x1f8`/`0x204`) and **+0xc** from `j 0x1d8`, a distinct, shorter
**"Top-Sync"** boot stub. The DRAM `.globstruct` magic `0x6099cb34` and its init
block `[0x18:0x38]` (`4×0x00001000` + `4×0x00ffffff`) are **byte-identical** between
MAVERICK SP and MARIANA_PLUS SP — the shared dispatcher-state initialization, intact.
`[HIGH/OBSERVED]`

> **CORRECTION — the SP reset is NOT `j 0x1d8 / −0x20`.** A coarse "v5 = `−0x20`"
> reading (true for DVE/PE/POOL, per [maverick-act.md](./maverick-act.md)) does
> **not** carry to SP. SP's primary target is `0x1e4`, not `0x1d8`; its shift is
> `−0x14`, not `−0x20`; its boot stub is the shorter "Top-Sync" form (`+0xc` vs the
> compute engines). What SP *shares* is **`enter_run @0x94`**. This was predicted
> from the [MAVERICK × ACT](./maverick-act.md) cross-engine note ("SP resets
> `j 0x1e4 / j 0x1f0`, the Top-Sync stub, `+0xc`; SP runs from SRAM") and is here
> **confirmed by direct decode**. `[HIGH/OBSERVED — all bytes decoded with ncore2gp]`

The SP PERF SRAM decodes a genuine, separately-compiled, **smaller** `cayman/seq`
sequencer — census (native `ncore2gp` objdump, exit 0): **107 `entry` / 140 `retw`
/ 407 `call8` / 1943 `const16`** vs MARIANA_PLUS SP PERF IRAM **127 / 183 / 686 /
2204** — not a stub. The FLIX-vector datapath is partly bundle-interleaved by the
linear sweep (the SX-FW-00 limitation); the windowed-ABI control spine decodes
cleanly. `[HIGH/OBSERVED]`

---

## 5. The SP handler/opcode diff — done at the ISA/dispatch level

**Method shift.** MARIANA_PLUS SP's handler diff used single-token `S: <OpName>`
lines from its DEBUG DRAM. MAVERICK SP has **no DEBUG image**, so `^S: ` count = **0**
across all four images (vs 141 on the MPLUS SP DEBUG DRAM). The handler diff is
therefore done at the ISA-enum + decode-roster + dispatch-mechanism level — exactly
as [maverick-pe.md](./maverick-pe.md) handled the no-DEBUG PE. `[HIGH/OBSERVED]`

The MARIANA_PLUS SP baseline — the **18-handler 5-way-intersection sync/control core**:

```text
AluOp  BRANCH  BranchPrefetchHint  Event_Semaphore  EXT_BREAK  Halt  INS_BREAK  INS_FL
MOVE  NOP  NOTIFY  POLL_SEM  Redirect  SET_OM  STRONG_ORDER  TensorLoad  TensorStore  WRITE
```

**Evidence the sync/control core is RETAINED on MAVERICK SP** (the decode-source /
function roster read from the four SP images): `branch_prefetch_hint.cpp`,
`alu_op.cpp`, `move_shape.cpp`, `branch.cpp`, `error_handler.cpp`,
`signal_handler.cpp`, `exception_handler.hpp`; functions `fetch_cache_line` /
`sunda_fast_fetch` / `sunda_redirect` / `setup_enqueue_dispatch_descriptors` /
`interrupt_handler` / `regfile_read` / `get_window_addr` / `resolve_address` /
`move_shape` / `enter_run`. This is the lean sync/control decode set — the same
family as the 18-handler intersection — and the `cayman/seq` source tree is retained
(`exception_handler.hpp` at `…/cayman/seq/src/handlers/`; `move.cpp:41` at
`…/src/decode/move.cpp`). `[HIGH/OBSERVED roster + string absence; the **18-handler
RETENTION is INFERRED-HIGH** — the strict-string proof is not reproducible without
the dropped DEBUG image.]`

### 5.1 The dispatch — base-subtraction RETAINED byte-exact

SP uses the **segmented / Sunda-mode HW-decode** dispatch flavor: the register-base
subtraction `sub a2,a2,a3` normalization (the **sub-flavor**, not the `addi a2,a2,-65`
ASCII normalization of DVE/POOL nor the raw-compare chain of PE), feeding a
`srli a2,a2,6` 6-bit segmentation. The encoding is **byte-identical across gens**:
`[HIGH/OBSERVED]`

```text
MAVERICK SP @SRAM 0xe326:  sub a2,a2,a3  (enc 3022c0)   ; srli a2,a2,6 @0x233a
MARIANA_PLUS SP @IRAM 0x286c:  sub a2,a2,a3 (enc 3022c0) — SAME ENCODING
```

The address relocated (`0xe326` SRAM vs `0x286c` IRAM) because the code moved
IRAM→SRAM; the dispatch **mechanism** (base-subtraction + 6-bit segmentation) is
identical. The DRAM dispatch table sits at **file `0x800` on both gens**; on
MAVERICK its trampolines are **absolute SRAM addresses** (`0x0410xxxx`, base
`0x04100000`) rather than the MARIANA_PLUS IRAM offsets — the SRAM-residence
relocation of the same table architecture. The first-16 LE-32 trampolines and the
size-class array, read directly from the PERF DRAM blob: `[HIGH/OBSERVED]`

```text
@0x800: 0x4107fcd 0x4107fe6 0x4107fff 0x4108021 0x4108042 0x410806a 0x4108570 0x410808b
        0x4107bca 0x410809f 0x41080a5 0x41080ab 0x41080b7 0x41080c0 0x410857d 0x410857d
        (40/40 entries in the SRAM range 0x04100000–0x04110000; default = 0x0410857d, ×8)
@0x8a0: 01 08 01 01 02 02 02 02 04 04 04 04 08 01 01 01   (per-opcode size-class array)
```

The segmented `srli a2,a2,6` indexing means the table is the **segment-trampoline
table**, not a flat 1-per-opcode table — matching MARIANA_PLUS SP's segmented
dispatch. **No opcode-space growth on SP.** The dual-mode machinery survives as the
`sunda_fast_fetch` / `sunda_handle_surprises` / `sunda_redirect` symbols; the
`S:`-prefixed runtime log strings lived only in the dropped DEBUG image and are
amputated. `[HIGH/OBSERVED for the sub-enc byte-identity / srli / table base /
SRAM-abs trampolines / size-class array / sunda symbols; per-opcode→segment row
binding is the FLIX-desync frontier, MED/INFERRED.]`

### 5.2 The new v5 opcodes do NOT land on SP

The shipped `neuron_maverick_arch_isa` OPCODE enum defines all six v5 additions —
`ACTIVATE_MULTIPASS 0x26`, `COMPACT_CONTROL_INST 0xb6`, `DMA_MEMCPY2 0xb9`,
`DMA_IMMEDIATE 0xba`, `TENSOR_TENSOR_INT_WIDE 0xf3`, `TENSOR_SCALAR_INT_WIDE 0xf4`
(all `// Y`). Three (`COMPACT_CONTROL_INST` / `DMA_MEMCPY2` / `DMA_IMMEDIATE`) are
sync/control/DMA-adjacent — the natural SP candidates. **They do not bind SP
handlers:** (a) none of the six appear as handler/decode strings **anywhere** in the
MAVERICK region (including the DVE DEBUG DRAM — they are decoded *structurally*, not
as named handlers); (b) SP carries no `dma_immediate` / `dma_memcpy` /
`compact_control` decode source (its roster is `alu_op`/`branch`/`move`/`signal`/
`exception` only); (c) SP has no PROF to arm them on. So MAVERICK SP stays the lean
sync/control core; the new DMA/control opcodes ride the compute/DMA engines that own
the descriptor path. The SP opcode surface is **STABLE**. `[HIGH/OBSERVED enum +
firmware-wide handler-string absence + SP decode roster; "not bound to SP"
INFERRED-HIGH — SP has no PROF/DEBUG to read the binding directly.]`

---

## 6. The DGE fast-path test — DROPPED on v5 SP (the decisive contrast)

**The question (only SP can answer cleanly):** SP is the sync/control core with
**no** DGE/reshape dispatch handler. On MARIANA_PLUS, SP carried the DGE fast-path
anyway — the decisive proof the fast-path was **gen-wide ADDED**
([mariana-plus-sp.md §6](./mariana-plus-sp.md)). On v5, is the fast-path dropped on
SP too (the region-wide v5 drop OBSERVED on DVE/PE)?

**Answer — DROPPED. It is gen-wide-DROPPED on v5.** The four DGE fast-path strings —
**PRESENT** (`1` each) in the MARIANA_PLUS SP DEBUG DRAM — are **0** across the
MAVERICK SP region: `[HIGH/OBSERVED]`

| string | MARIANA_PLUS SP (DEBUG DRAM) | MAVERICK SP (4 imgs + region) |
|---|:---:|:---:|
| `dge_decode_fast` | **1** | **0** |
| `dge_reshape_memcopy_transpose_fast` | **1** | **0** |
| `tensor_reshape_transpose_sb2sb` | **1** | **0** (SP) / 1 region |
| `wait_for_credit` | **1** | **0** |
| `dge_backend_rtl` | — | **0** |
| `push REGWRITE` | (retired pre-v5) | **0** |
| `dge_shape` (shared infra) | present | **2** (SP) / 7 region — survives |

> **THE VERDICT — gen-wide DROPPED, the mirror of MARIANA_PLUS.** The v4+ SEQ-side
> DGE fast-path (`dge_decode_fast` + helpers + `wait_for_credit`) that
> [mariana-plus-sp.md](./mariana-plus-sp.md) found **PRESENT** on the lean SP — its
> single strongest proof the fast-path was gen-wide — is **GONE** on MAVERICK SP,
> region-wide and SP-specific 0. Only the shared `dge_shape` infra survives. On
> MARIANA_PLUS, SP proved the fast-path was gen-wide **added**; on MAVERICK, SP
> proves it is gen-wide **dropped** — re-architected to the HW DMA path per the v5
> re-model, consistent with [maverick-dve.md](./maverick-dve.md) /
> [maverick-pe.md](./maverick-pe.md). The shrink (§7) is consistent with the removed
> fast-path code. `[HIGH/OBSERVED counts; the "re-architected to HW DMA" reading
> INFERRED-HIGH.]`

---

## 7. dtype / PROF / size / build-independence

* **dtype — numeric-only, unchanged.** The only dtype constants in any MAVERICK SP
  image are `NEURON_ISA_TPB_DTYPE_{UINT32, INT32, FP32}` in the `move.cpp:41`
  assertion (*"highest priority is full-register moves. TODO other dtypes"*) —
  byte-identical to MARIANA_PLUS. `FP8_EXP2`/`INT4`/`SFP8`/`MXTENSOR`/`MX_PERF`/
  `TILE_SIZE`/`FP4`/`CPTC`/`QuantizeMx`/`proc_4bit`/`fp8_e`/`dequant` = **0** across
  all four SP images. The v5 MX/FP8/INT4/SFP8 dtype superset rides the Matmul
  in/out-dtype fields (PE) and the Q7 POOL dequant kernel — it leaves **no SP
  footprint**; the scalar/control core has no MX/dequant surface, exactly as on every
  prior gen. `[HIGH/OBSERVED]`

* **PROF — none, no re-author possible.** SP ships no `PROF_CAM`/`PROF_TABLE` on any
  gen (`nm | rg -c 'MAVERICK_NX_SP.*PROF'` = 0; 8 getters, 0 `hwdecode_` members —
  the only NX engine without PROF). So unlike the **v5 per-engine PROF re-authoring**
  the other three MAVERICK NX engines exhibit (DVE CAM `dbff2b84`/TABLE `f349e417`;
  PE CAM `85d857a7`/TABLE `e94d413a` — both halves distinct from MARIANA_PLUS, per
  [maverick-dve.md](./maverick-dve.md) / [maverick-pe.md](./maverick-pe.md)), SP has
  **no profiler to re-author or reuse**. The "v5 PROF re-authored per-engine" finding
  has no SP instance. `[HIGH/OBSERVED]`

* **Q7 / EXTISA — none.** Every carve is a flat device segment with no ELF magic
  (`head -c4` = the reset vector `06 78 …` for SRAM, the magic `34 cb 99 60` for
  DRAM — *not* `7f 45 4c 46`). No Q7 core, no EXTISA — those are Q7_POOL-only.
  `[HIGH/OBSERVED]`

* **size — ≈62 % smaller (the INVERSE of the v4→v4+ growth).** Where the
  MARIANA→MARIANA_PLUS SP transition **grew** IRAM (the inserted DGE fast-path),
  MARIANA_PLUS→MAVERICK SP **shrinks** in every dimension. Computed this session:
  total `0x23840` (4 imgs) vs `0x5d9c0` (6 imgs) = **−62.1 %**. `[HIGH/OBSERVED]`

  | role | MPLUS size | MAV size | dSize | note |
  |---|---:|---:|---:|---|
  | PERF code | `0x1c300` (IRAM) | `0xf580` (SRAM) | `−0xcd80` | IRAM→SRAM |
  | TEST code | `0x1a8a0` (IRAM) | `0xf6c0` (SRAM) | `−0xb1e0` | IRAM→SRAM |
  | PERF data | `0x3040` (DRAM) | `0x24c0` | `−0xb80` | |
  | TEST data | `0x33a0` (DRAM) | `0x2740` | `−0xc60` | |
  | DEBUG IRAM | `0x1a3e0` | — | `−0x1a3e0` | **image DROPPED** |
  | DEBUG DRAM | `0x6660` | — | `−0x6660` | **image DROPPED** |
  | **TOTAL** | `0x5d9c0` | `0x23840` | **≈ −62 %** | 6 imgs → 4 imgs |

  The shrink = the DGE-drop + the DEBUG-drop + the independent v5 build — the same
  direction as MAVERICK PE (≈64 % smaller, [maverick-pe.md](./maverick-pe.md)).

* **build independence.** MAVERICK SP PERF SRAM diverges from MARIANA_PLUS SP PERF
  IRAM at **byte 1** (reset `78` vs `7d`) with **5.9 %** positional 16-byte block
  similarity (231/3928 blocks) — a fully independent v5 build, **not** a relocated
  recompile (contrast MARIANA_PLUS SP, which shared a `0xa2`-byte prefix +
  byte-identical dispatch trampolines with MARIANA SP). This matches MAVERICK PE's
  ≈7.6 %. `[HIGH/OBSERVED]`

* **source / errata / self-name.** `translate_cayman+.hpp` present (`grep` 1, the
  gen-wide address-rerouting header); `cayman/seq` source tree retained (`grep` 6);
  `mariana-4062` errata **absent** (region-wide v5 errata drop); **no** `maverick`
  errata string; **no** `S: BEGIN on maverick` self-name on any SP image (it lives in
  the DVE DEBUG DRAM — SP's DEBUG image is dropped). `addr_bits` is not present as a
  string (the DEBUG-resident assertion strings are largely amputated — not a
  substantive delta). `[HIGH/OBSERVED; addr_bits MED — assertion-string absence.]`

* **engine_idx = 4 (TPB_SP), confirmed.** The shipped ISA enum
  `neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
  `NEURON_ISA_TPB_NEURON_ENGINE { PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5 }`
  fixes `TPB_SP=4`; `TPB_SP(4)` and `TOP_SP(5)` are **distinct** enumerators. The
  carved image is the per-NeuronCore `TPB_SP` (engine 4) — the scalar/sync engine
  inside one TPB alongside PE/POOL/DVE (ACT folded into DVE) — **not** the standalone
  `TOP_SP` (engine 5) collective sequencer (the
  [collective end-to-end](../orientation/collective-end-to-end.md) `engine_idx 5`
  target). `engine_idx` is **runtime-computed**: the region carries
  `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`
  — the late-bound identity that lets all NX engines share one reset/boot form.
  MAVERICK has its **own** ISA dir (`neuron_maverick_arch_isa`) — NC-v5. `arch_id 36`
  is `INFERRED` (`coretype = arch_id + 1`; no NCFW v5 image). `[HIGH/OBSERVED enum +
  identity string; arch_id 36 MED/INFERRED.]`

---

## 8. The MAVERICK matrix — COMPLETE (v5 vs MARIANA_PLUS, all engines)

With SP carved and diffed, **all five MAVERICK NX engine slots are now resolved**:
ACT (folded into DVE), DVE (head), PE, POOL, SP. The cross-engine MARIANA_PLUS → MAVERICK
divergence, each row anchored to its committed/in-flight page:
`[HIGH/OBSERVED for ACT/DVE/PE/SP; POOL MED-CARRIED]`

| engine | idx | image shape | PROF (v5) | DGE FP | the v5 change | page |
|---|---:|---|---|:---:|---|---|
| **ACT** | 1 | **ABSENT** (folded into DVE) | n/a | n/a | AMPUTATED: no `NX_ACT` image; ACT opcodes `0x23`/`0x25` armed on DVE PROF; read-accum → `DveReadAccumulator`. | [act](./maverick-act.md) |
| **DVE** | 3 | 14 getters, FULL DEBUG (the only one) | CAM `dbff2b84` + TABLE `f349e417` (re-authored) | DROPPED | HEAD engine: absorbed ACT; +6 new v5 opcodes; `0xe3 QuantizeMx` stays DVE-bound (DVE PROF CAM) but the **named handler is dropped** (60→59); PROF re-authored; DGE dropped; independent build. | [dve](./maverick-dve.md) |
| **PE** | 0 | 10 getters, **NO DEBUG** | CAM `85d857a7` + TABLE `e94d413a` (re-authored) | DROPPED | Matmul roster retained; MX unified into Matmul/Ldweights; +FP8/INT4/SFP8 dtypes; DEBUG dropped; PROF re-authored; DGE dropped; ≈64 % smaller; indep build. | [pe](./maverick-pe.md) |
| **POOL** | 2 | 10 getters NX, **NO DEBUG**; Q7_POOL keeps DEBUG+EXTISA | reused verbatim (disarmed) | DROPPED | dual-core; **NO MX expansion on POOL** (all 4 EXTISA KITs key-set == MARIANA_PLUS, `+0/−0`); POOL's only MX surface is the pre-existing `0x7b TENSOR_DEQUANTIZE` dequant (`0xe3` absent from every KIT); NX_POOL DEBUG dropped; Q7 → SRAM. | [pool](./maverick-pool.md) |
| **SP** | 4 | **8 getters, NO DEBUG, NO PROF** (4 real); **SRAM-resident** | **NONE** (no PROF) | **DROPPED** | LEAN sync/control core, **SRAM-resident** (the v5 anomaly); DEBUG dropped; DGE fast-path **DROPPED** (was PRESENT on MPLUS SP — the decisive contrast); `enter_run @0x94` (Top-Sync reset `j 0x1e4`); dispatch + handler surface + dtype RETAINED; no new SP opcodes; ≈62 % smaller; indep build. **The degenerate lower bound.** | **this page** |

### 8.1 The gen-wide v4+ → v5 invariants (across the MAVERICK NX engines)

* **GENUINE NEW GENERATION, NOT A RECOMPILE.** Every MAVERICK NX engine is an
  **independent** v5 build (≈6–8 % positional block-similarity to its MARIANA_PLUS
  twin: DVE/PE ≈7.6 %, **SP 5.9 %**), diverging at **byte 1** (reset byte
  `75`/`78` vs `7d`) — the sharp contrast with the MARIANA→MARIANA_PLUS
  recompile-relocation (shared prefixes, byte-identical dispatch trampolines).
* **THE v5 RESET / `enter_run @0x94`.** DVE/PE/POOL reset `j 0x1d8 / j 0x1e4`
  (`−0x20`); **SP** resets `j 0x1e4 / j 0x1f0` (the Top-Sync stub, `+0xc`). All share
  `enter_run @0x94` (the +4 v5 signature). The DRAM `.globstruct` magic `0x6099cb34`
  + init block is **unchanged** on every engine.
* **INTERNAL-TWIN-EXCLUSIVE.** MAVERICK ships **only** in
  `libnrtucode_internal.so`; **0** members in `libnrtucode.a` (435 total = 420 image + 15 framework). No `.a`
  byte-reconcile exists for any MAVERICK engine.
* **THE DGE FAST-PATH IS DROPPED GEN-WIDE.** The v4+ SEQ-side fast-path that was
  PRESENT on **every** MARIANA_PLUS NX engine (incl. the lean SP) is GONE region-wide
  — OBSERVED 0 on DVE/PE/**SP**. **SP is the decisive lean-engine confirmation.**
* **PROF RE-AUTHORED PER-ENGINE** (the opposite of MARIANA_PLUS's verbatim PROF
  reuse): DVE/PE/POOL ship new CAM + new TABLE (both halves distinct from
  MARIANA_PLUS); **SP has no PROF (n/a).**
* **THE DEBUG-IMAGE DROP.** Only NX_DVE keeps the full DEBUG+PERF+TEST set; PE/POOL/SP
  drop the DEBUG image (the only carrier of named `S:` handler strings). Handler diffs
  on PE/POOL/SP are done at the ISA/PROF/decode-roster level.
* **OPCODE/DTYPE EXPANSION IS ENGINE-LOCALIZED.** The 6 new v5 opcodes live in the
  maverick enum; the MX dtype superset (FP8/INT4/SFP8) rides the Matmul fields (PE) +
  the Q7 POOL dequant kernel; **the NX sequencer dtype surface stays numeric**
  (UINT32/INT32/FP32) on every NX engine including SP, and the new control/DMA opcodes
  do **not** bind SP handlers.
* **`mariana-4062` errata DROPPED gen-wide**; `cayman/seq` source tree retained; NC-v5
  own ISA dir; `coretype 37` / `arch_id 36` (INFERRED).

**No MAVERICK NX engine is a mere recompile of its MARIANA_PLUS twin — each is a
genuine new-generation independent build on a re-modeled chassis (new reset/enter_run,
re-authored PROF, dropped DGE fast-path, dropped DEBUG on PE/POOL/SP, opcode/MX/dtype
expansion localized to the compute engines + the Q7 POOL). ACT is folded into DVE. SP
is the degenerate lower bound that anchors the model: the lean sync/control chassis
with NOTHING added to dispatch and the v4+ DGE fast-path REMOVED — the decisive proof
the fast-path drop is gen-wide rather than engine-selective, the mirror image of SP's
role on the MARIANA_PLUS matrix. THE MAVERICK ENGINE MATRIX IS COMPLETE.** `[HIGH/OBSERVED
for ACT/DVE/PE/SP; the POOL line MED-CARRIED where not directly re-derived here.]`

---

## 9. Adversarial self-verify

The five strongest claims, re-challenged against the binary this session:

1. **SRAM-resident (IRAM size 0).** `nm` `.data`: `MAVERICK_NX_SP_PERF_IRAM_get.data`
   and `…PERF_DRAM_get.data` share VA `0x8eeae0` — the IRAM getter is the zero-size
   cursor (its stub `movq $0x0,(%rsi)`); the real code is `PERF_SRAM` (`0xf580`),
   `TEST_SRAM` (`0xf6c0`). The reset vector and boot trampoline decode from byte 0 of
   the **SRAM** blob and build SRAM-absolute addresses (`const16 a0,0x410` →
   `0x04100000`; trampolines `0x0410xxxx`, 40/40 in range). **HOLDS.** `[HIGH/OBSERVED]`
2. **DEBUG dropped, 8 getters, no PROF.** `nm | rg -c 'MAVERICK_NX_SP_.*_get$'` = 8;
   `…NX_SP.*DEBUG` = 0; `…NX_SP.*PROF` = 0. `^S: ` count = 0 across all four images
   (vs 141 on the MPLUS SP DEBUG DRAM); no `S: BEGIN on maverick`. **HOLDS.**
   `[HIGH/OBSERVED]`
3. **DGE-fast-path-dropped contrast.** All four fast-path strings (`dge_decode_fast` /
   `dge_reshape_memcopy_transpose_fast` / `tensor_reshape_transpose_sb2sb` /
   `wait_for_credit`) = **0** on MAVERICK SP (region-wide); = **1** each on the
   MARIANA_PLUS SP DEBUG DRAM (re-verified). Only shared `dge_shape` survives (SP 2).
   SP hosts no DGE handler → **gen-wide DROPPED. HOLDS.** `[HIGH/OBSERVED]`
4. **v5 reset geometry.** SRAM head `06 78 00 00 00 00 86 79` → `ncore2gp`:
   `j 0x1e4` / `j 0x1f0`; boot `const16 a0,0x410 ; const16 a0,148 ; jx a0` →
   `enter_run @0x94`; secondary `halt 0`. Cross-checked vs MARIANA_PLUS SP (`j 0x1f8`
   / `@0x90`) and the DVE/PE `j 0x1d8`. The brief's "SP = `j 0x1d8 / −0x20`" was
   **corrected** to the OBSERVED `0x1e4 / −0x14 / +0xc Top-Sync`. **HOLDS.**
   `[HIGH/OBSERVED]`
5. **≈62 % size reduction.** Recomputed from the carved getter sizes: MAV total
   `0x23840` (145472 B, 4 imgs) vs MPLUS total `0x5d9c0` (383424 B, 6 imgs) =
   **−62.1 %**. Block similarity 5.9 % (231/3928), byte-1 divergence. **HOLDS** (the
   brief's "≈61 %" is the same figure rounded). `[HIGH/OBSERVED]`

All five survive. The residual frontier is the exact per-opcode→segment-trampoline
row decode (FLIX-desync, SX-FW-00 — no DEBUG image to fall back on) and the
18-handler retention (INFERRED-HIGH from the decode roster + the unchanged
dispatch + the SP-is-the-substrate model, since the strict-string proof needs the
dropped DEBUG image).

---

## 10. Honesty ledger

**HIGH / OBSERVED (this session):**

- Container sha `b7c67e89…632fc329b` MATCH. 8 `MAVERICK_NX_SP_*_get` getters
  (4 real + 4 zero-size cursors); `nm | rg -c` DEBUG/PROF = 0/0. 4 real carves
  re-hashed (`d5d3ba2d` / `08e35945` / `c2b2436a` / `27908ff6`); single-source
  (0 `.a` members of 435). MARIANA_PLUS SP baseline 6/6 re-hashed, MATCH the
  committed page.
- Code residence SRAM: IRAM getters size 0; code in SRAM (`0xf580`/`0xf6c0`); SRAM
  base `0x04100000` (boot `const16 a0,0x410`; trampolines `0x0410xxxx`).
- Reset/boot decoded `ncore2gp` (exit 0): `06 78` → `j 0x1e4`; `86 79` → `j 0x1f0`;
  boot → `enter_run @0x94`; secondary `halt 0`; `enter_run @0x94` real code
  (`const16 a2,0x410 ; const16 a2,0xe086 ; beqz ; callx0 a2`). DRAM magic
  `0x6099cb34` + init block `[0x18:0x38]` byte-identical to MARIANA_PLUS SP.
- Engine ordering: SP last NX (DVE→PE→POOL→SP); `SP_TEST_SRAM` end `0x912320` ==
  `SP_TEST_EXTRAM` cursor == `MAVERICK_Q7_POOL_PERF_DRAM` head (4/4 contiguity).
- Dispatch: `sub a2,a2,a3` enc `3022c0` byte-identical to MARIANA_PLUS SP (relocated
  SRAM `0xe326`); `srli a2,a2,6` `@0x233a`; table `@0x800`; SRAM-abs trampolines
  (40/40, default `0x0410857d`); size-class array `@0x8a0`; `sunda_*` symbols.
- 0 `S:` strings on SP (DEBUG dropped); decode/function roster read (`alu_op`/
  `branch_prefetch_hint`/`move_shape`/`signal_handler`/`interrupt_handler`/`regfile`/
  `sunda_*`/`setup_enqueue_dispatch_descriptors`/`get_window_addr`/`exception_handler`).
- DGE fast-path 0 on SP + region-wide (4 strings); only `dge_shape` survives (SP 2 /
  region 7); MARIANA_PLUS SP DEBUG DRAM carried all four (1/1/1/1) — re-verified.
- dtype: only `UINT32`/`INT32`/`FP32` (`move.cpp:41`); 0 FP8/INT4/SFP8/MX/TILE_SIZE/
  CPTC/QuantizeMx/proc_4bit/dequant across all four SP images. PROF none. Q7/EXTISA
  none.
- Size: total `0x23840` (4 imgs) vs `0x5d9c0` (6 imgs) = −62.1 %; block similarity
  5.9 %, byte-1 divergence; code census 107/140/407/1943 (smaller genuine sequencer
  vs MPLUS 127/183/686/2204). `mariana-4062` absent; no maverick errata;
  `translate_cayman+` present; `cayman/seq` retained.
- ISA enum `TPB_SP=4` distinct from `TOP_SP=5`; runtime identity string present.

**MED / INFERRED:**

- The SP 18-handler **retention** — the named strings are amputated (no DEBUG image);
  INFERRED-HIGH from the decode roster + the unchanged base-subtraction dispatch +
  the SP-is-the-substrate model.
- "The new DMA/control opcodes (`0xb6`/`0xb9`/`0xba`) do **not** bind SP handlers" —
  INFERRED-HIGH from the firmware-wide handler-string absence + the SP decode roster +
  SP's lack of PROF/DEBUG.
- The exact per-opcode→segment-trampoline row binding on the SP DRAM table — the
  SX-FW-00 FLIX-desync frontier. Table base `0x800`, SRAM-abs trampolines, default
  `0x0410857d` and the size-class array are HIGH; per-row binding is the frontier.
- "DGE fast-path re-architected to HW DMA" — the SEQ-side absence is OBSERVED; the
  HW-DMA reading is INFERRED-HIGH.
- The matrix POOL line — CARRIED from the sibling-page model + the Q7-MX cross-ref
  (MED where not directly re-derived here).
- `arch_id 36` (`coretype = arch_id + 1`; no NCFW v5 image) — MED/INFERRED.

**LOW / NOT CLAIMED:**

- Whether the standalone `TOP_SP` (engine 5) block runs the same `cayman/seq` SP
  build (this library ships one NX SP image = `TPB_SP`).
- Which silicon/runtime selects PERF/TEST (no DEBUG ships); the exact SP-op → EVT_SEM
  APB-window binding (in the lowered instruction operands, not the firmware image).
- The exact DGE-removed control-flow / inlined fast-path body on SP (decoded only at
  the string/symbol level — the FLIX-desync frontier).
- Whether the runtime ever LOADS MAVERICK (out of scope).

---

## 11. Cross-references

- [MARIANA_PLUS × SP image](./mariana-plus-sp.md) — the v4+ baseline this page diffs
  against (the 12-getter shape, the 18-handler 5-way-intersection derivation, the
  +0x1c reset, and the DGE-fast-path PRESENCE this page inverts).
- [MAVERICK × ACT](./maverick-act.md) (the ACT→DVE fold, the v5 reset map + the SP
  Top-Sync prediction) · [MAVERICK × DVE](./maverick-dve.md) (the head engine, PROF
  re-author, DGE drop) · [MAVERICK × PE](./maverick-pe.md) (the no-DEBUG / MX-unified
  precedent) · [MAVERICK × POOL](./maverick-pool.md) (the Q7-MX expansion home).
- [MAVERICK generation profile](../generations/maverick-profile.md) — the v5
  new-generation model (independent build, re-modeled chassis, DGE drop).
- [Image Catalog Index](./image-catalog-index.md) — the full getter map (MAVERICK
  NX_SP rows).
- [Collective end-to-end](../orientation/collective-end-to-end.md) — the standalone
  `TOP_SP` (engine 5) collective sequencer kept distinct from this per-core `TPB_SP`
  (engine 4).
- [Per-Engine Firmware Depth](../uarch/per-engine-depth.md) — the companion
  `TPB_SP`/`TOP_SP` deep-dive, EVT_SEM aperture, and the no-dedicated-barrier-handler
  result.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
