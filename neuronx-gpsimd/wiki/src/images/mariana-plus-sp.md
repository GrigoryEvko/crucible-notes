# MARIANA_PLUS × SP image (cross-gen diff vs MARIANA — the matrix-completing engine)

This page **closes the MARIANA_PLUS (v4+) engine matrix** by diffing the
MARIANA_PLUS Sync-Processor firmware image (`TPB_SP`, `engine_idx = 4`) against
the committed, byte-true [MARIANA × SP baseline](./mariana-sp.md). Every size,
sha256, reset byte, opcode-table slot and string below is read directly from
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) via its 12
`MARIANA_PLUS_NX_SP_*_get` accessors, carved from identity-mapped `.rodata`, and
reconciled byte-for-byte against the matching `libnrtucode.a` members; the flat
blobs are decoded with the shipped Cadence Vision-Q7 `ncore2gp`
`xtensa-elf-objdump`.

SP is the **fifth and last** NX sequencer inside one MARIANA_PLUS TPB — after
PE (0), ACT (1), POOL (2), DVE (3) — and the leanest: a pure scalar/sync control
core with **no Q7 compute, no PROF, no EXTISA**. That degeneracy is precisely
what makes SP the *sharp test* of the v4+ model. Two questions only SP can answer
cleanly:

> **HEADLINE #1 — the null functional delta. MARIANA_PLUS SP is byte-for-name
> IDENTICAL to MARIANA SP at the handler/opcode/dtype layer.** The handler roster
> is `18 == 18` (`+0/−0`) — the exact 5-way-intersection sync/control core; the
> opcode-space dispatch is the same base-subtraction table at the **byte-identical
> address** `0x286c` with **byte-identical real-slot trampolines**; the reset is
> the **SAME +0x1c MARIANA NX shift with NO further shift** (`06 7d 00` → `j 0x1f8`,
> byte-identical heads on both gens); PROF is `none` (so cannot diverge). SP gained
> and lost nothing — across **both** generation transitions.
>
> **HEADLINE #2 — the DGE fast-path is GEN-WIDE, not engine-selective. It is
> PRESENT on SP.** The four DGE fast-path strings absent on MARIANA SP (`count 0`)
> are PRESENT on MARIANA_PLUS SP — in the DEBUG DRAM **and** the symbol-bearing
> TEST DRAM (compiled code, not stray text). SP hosts **no** DGE/reshape dispatch
> handler, so its carrying the fast-path code proves the optimization is a
> **SEQ-infrastructure recompile feature shipped on every NX label**, independent
> of each engine's handler subset. **The decisive resolution of the
> gen-wide-vs-engine-selective question.**
> `[HIGH/OBSERVED; functional reading INFERRED-HIGH]`

`MARIANA_PLUS SP = MARIANA SP-recompiled + the gen-wide DGE fast-path + a
register-map refresh` — **no model, ISA, handler, opcode or dtype change.**

MARIANA_PLUS shares the MARIANA ISA: there is no `neuron_mariana_plus_arch_isa`
dir (the four ISA dirs are `cayman`/`mariana`/`maverick`/`sunda`), and
MARIANA_PLUS carries only its own `arch-headers/mariana_plus/` *register-map* dir.
So the ISA/struct/dtype surface **is** MARIANA's; "v4+" is a register-map refresh
+ a recompile + a DGE optimization, not a new model (the ISA-dir listing is the
shipped artifact). The SP image inherits all of that.

The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit
tag, following the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. The engine *model* (the carve mechanics, the
12-getter shape, the 5-way-intersection derivation, the SP-vs-`TOP_SP`
resolution, the EVT_SEM/barrier mechanism) is derived once in
[mariana-sp.md](./mariana-sp.md) / [cayman-sp.md](./cayman-sp.md) and **not
re-derived here** — this is a **DIFF** page.

Related pages: [MARIANA × SP (the diff base)](./mariana-sp.md) ·
[MARIANA+ generation delta](../generations/mariana-plus-delta.md) ·
[MARIANA_PLUS × ACT (matrix start)](./mariana-plus-act.md) ·
[MARIANA_PLUS × DVE](./mariana-plus-dve.md) ·
[DGE Reshape Engine](../firmware/dge/dge-reshape.md) ·
[DGE 3-Backend Selector](../firmware/dge/dge-backend-selector.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md) ·
[Collective end-to-end (TOP_SP, engine 5)](../orientation/collective-end-to-end.md) ·
[Per-Engine Firmware Depth](../uarch/per-engine-depth.md).

> **NOTE — the objects used (identical to the baseline).** Container
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64
> x86-64 DYN, **not stripped**). First R `LOAD` is the identity map
> (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`), so each `<NAME>.data` accessor
> address is simultaneously the `.rodata` VA and the file offset of its blob —
> carve = `so[ptr : ptr+size]`. **IRAM file-offset == device IRAM VA** (reset
> vector at byte 0); **DRAM string-file-offset == device DRAM VA − `0x80000`**.
> Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`,
> uarch Cairo, `NX1.1.4`). The archive `libnrtucode.a`
> (sha256 `158dadc5…d7bd6130`) supplies the second carve source for the
> byte-identity reconciliation. The clean C ISA header
> `neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_common.h` is cited for the engine
> enum (MARIANA_PLUS shares the cayman/mariana ISA).

---

## 1. The delta table (MARIANA SP baseline → MARIANA_PLUS SP)

The whole page in one table. `(==)` marks an invariant row; the **bold** rows are
the *only* real changes. Read the [MARIANA SP page](./mariana-sp.md) for the
engine *model* — this table documents the cross-gen delta, leading with the null
functional delta and the DGE-fast-path verdict. Every row is grounded in a carve
from `libnrtucode_internal.so`.

| PROPERTY | MARIANA SP ([baseline](./mariana-sp.md)) | MARIANA_PLUS SP (this page) | Δ |
|---|---|---|---|
| getters (`nm`) | 12 (6 real + 6 cursor) | 12 (6 real + 6 cursor) | `(==)` shape |
| packaging | flat IRAM/DRAM (not ELF) | flat IRAM/DRAM (not ELF) | `(==)` |
| cores | single NX (no Q7) | single NX (no Q7) | `(==)` |
| reset vector (primary) | `06 7d 00 00` → `j 0x1f8` | `06 7d 00 00` → `j 0x1f8` | `(==)` **SAME +0x1c, no further shift** |
| secondary vector | `86 7e 00 00` → `j 0x204` | `86 7e 00 00` → `j 0x204` | `(==)` |
| IRAM head (12 B) | byte-identical to PLUS | byte-identical to MAR | `(==)` |
| boot trampoline | `const16 a0,0x90 ; jx → enter_run @0x90` | identical (decoded `0x1f8`) | `(==)` |
| DRAM `.globstruct` magic | `0x6099cb34` | `0x6099cb34` | `(==)` |
| `.globstruct` init `[0x18:0x38]` | `4×0x1000` + `4×0xffffff` | byte-identical | `(==)` |
| dispatch flavor | segmented/Sunda, `sub a2,a2,a3` (`3022c0`) | segmented/Sunda, `sub a2,a2,a3` (`3022c0`) | `(==)` |
| dispatch-head `sub` site | `@0x286c` | `@0x286c` | `(==)` **byte-identical addr** |
| DRAM table base (DEBUG) | file `0x800` | file `0x800` | `(==)` |
| real handler-slot trampolines | `293c 2945 2934 291c 2924 2965 2955 295d` | **byte-identical** | `(==)` |
| default/Bad-Opcode trampoline | `0x2975` | `0x298b` | reloc **+0x16** |
| `S: Dispatch opcode` string | `@0xa98` | `@0xb38` | reloc +0xa0 |
| opcode space | ~161-entry table | ~161-entry table | `(==)` **no growth** |
| **handlers** | **18 (5-way intersection)** | **18 (IDENTICAL set)** | `(==)` **+0 / −0** |
| `EngineNop` on SP? | no | no | `(==)` |
| **DGE fast-path** (4 strings) | **absent (`count 0`)** | **PRESENT (DEBUG + TEST DRAM)** | **NEW — gen-wide** |
| `push REGWRITE to DMA[%d]` | present | **retired** | dropped (folded) |
| PROF | none | none | `(==)` (no SP instance) |
| Q7 / EXTISA | none | none | `(==)` |
| dtype constants | `UINT32`/`INT32`/`FP32` | `UINT32`/`INT32`/`FP32` | `(==)` |
| FP4/MX/CPTC/SFP8 strings | 0 | 0 | `(==)` |
| `addr_bits.hpp` | present | present | `(==)` |
| `translate_cayman+.hpp` | present | present | `(==)` |
| `mariana-4062` errata | absent (DVE-only) | absent | `(==)` |
| `mariana_plus-NNNN` errata | n/a | none | — |
| engine self-name | `S: BEGIN on mariana` | `S: BEGIN on mariana_plus` | gen-name |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` | `(==)` |
| IRAM size (DEBUG) | `0x190e0` | `0x1a3e0` | **+0x1300 (GREW)** |
| `engine_idx` (runtime) | 4 (`TPB_SP`) | 4 (`TPB_SP`; identity string present) | `(==)` |

The diff reduces to **a full recompile, the new gen-wide DGE fast-path, and the
`BEGIN`-name** — the +0x1c reset, the table base, the real-slot trampolines, the
handler set, the opcode space, the dtype surface and the (absence of) PROF/Q7 are
all invariant.

---

## 2. Carve + sha + 6/6 byte-identity reconciliation

`nm` lists exactly **12** `MARIANA_PLUS_NX_SP_*_get` symbols (NO `PROF`, NO `Q7` —
`nm | rg -c 'MARIANA_PLUS_NX_SP.*PROF'` = 0; `…Q7_SP` = 0), the **same 12-getter
shape as MARIANA SP**. The four compute engines (ACT/DVE/PE/POOL) each ship 14 NX
getters (12 base + 2 PROF `{CAM, TABLE}`); SP ships 12 = base only. Each getter is
the `(img-ptr, size)` stub
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`); the 6
zero-size SRAM/EXTRAM getters all execute `movq $0x0,(%rsi)` and resolve to the
next-blob layout cursor — SP uses **no SRAM/EXTRAM** (runs entirely out of IRAM +
DRAM), exactly as MARIANA SP and CAYMAN SP.

Carve rule (identity map): `blob = so[IMG-PTR : IMG-PTR+SIZE]`. The 6 real
MARIANA_PLUS carves and their sha256 (**all 6** reconciled
byte-identical to the `libnrtucode.a` member `.rodata` via `ar x` +
`objcopy -O binary --only-section=.rodata` + `cmp -s` — full 6/6, not a
spot-check):

| IMAGE | FILE-OFF | SIZE | MPLUS sha256 (8) | `.a` member | MARIANA sha (8) |
|---|---|---:|---|:---:|---|
| `SP_PERF_IRAM` | `0x60be40` | `0x1c300` | `3a815569` | ✓ | `9499fee9` |
| `SP_PERF_DRAM` | `0x628140` | `0x3040` | `79c2e2fa` | ✓ | `fc097ffe` |
| `SP_TEST_IRAM` | `0x691ba0` | `0x1a8a0` | `11527767` | ✓ | `4d41211a` |
| `SP_TEST_DRAM` | `0x6ac440` | `0x33a0` | `c9aaac9f` | ✓ | `112914e4` |
| `SP_DEBUG_IRAM` | `0x738ba0` | `0x1a3e0` | `84ee1c05` | ✓ | `10dd1252` |
| `SP_DEBUG_DRAM` | `0x752f80` | `0x6660` | `2958154e` | ✓ | `8cabc82a` |

All 6 are **distinct** from their MARIANA counterparts (a full recompile, not a
patch — §7). All 12 getter `(img-ptr, size)` stubs match the catalog
([image-catalog-index.md](./image-catalog-index.md), MARIANA_PLUS NX_SP rows). The
MARIANA SP baseline is carved and hashed from the same container, and **all 6
anchors MATCH** the committed [mariana-sp.md](./mariana-sp.md) — the diff below is
against authentic MARIANA SP.

---

## 3. Engine ordering — SP is LAST of the five NX engines

The MARIANA_PLUS `.rodata` layout is **VARIANT-MAJOR, ENGINE-MINOR**
(ACT → DVE → PE → POOL → SP within each variant family), the **same order** as
MARIANA. Read directly from `nm` `.data` addresses, SP is the **terminal NX
sequencer** in every family, immediately before the POOL Q7 compute core:

```text
PERF : ACT 0x5a5480 → DVE 0x5bcd20 → PE 0x5d6e60 → POOL 0x5f0fe0 → SP_PERF_IRAM @0x60be40 (last)
       SP_PERF_DRAM end 0x62b180 == cursor target == ACT_TEST_IRAM @0x62b180
TEST : … → SP_TEST_*; cursor → ACT_DEBUG_IRAM @0x6af7e0 (next family head)
DEBUG: … → SP_DEBUG_DRAM @0x752f80; end 0x7595e0 == MARIANA_PLUS_Q7_POOL_PERF_IRAM @0x7595e0
```

Two contiguity anchors close the layout (both `nm`-verified):
**(a)** within PERF, `SP_PERF_IRAM` is the fifth and last NX head at `0x60be40`,
its DRAM ending exactly at the `ACT_TEST_IRAM @0x62b180` family head;
**(b)** `SP_DEBUG_DRAM` ends at `0x752f80 + 0x6660 = 0x7595e0` — exactly the VA of
`MARIANA_PLUS_Q7_POOL_PERF_IRAM_get.data` (`nm`: `7595e0 r …Q7_POOL_PERF_IRAM`).
SP's DEBUG block **precedes** the Q7_POOL compute core; SP is the last NX
sequencer.

---

## 4. The reset/boot diff — SAME +0x1c, NO further shift

The SP IRAM head is byte-identical across all three variants (DEBUG/PERF/TEST)
and **byte-identical between gens** — MARIANA_PLUS SP carries the MARIANA-gen NX
`+0x1c` reset shift with **no further relocation**:

```text
                       MARIANA SP            MARIANA_PLUS SP       Δ
IRAM head (12 B)   06 7d 00 00 00 00 86 7e…  06 7d 00 00 00 00 86 7e…  BYTE-IDENTICAL
0x000 primary      06 7d 00  j 0x1f8     →   06 7d 00  j 0x1f8        SAME (+0x1c carried)
0x006 secondary    86 7e 00  j 0x204     →   86 7e 00  j 0x204        SAME
boot @ 0x1f8       const16 a0,0 ; const16 a0,0x90 ; jx a0 → enter_run @0x90   UNCHANGED
0x204              halt 0  (secondary = HALT trap)                            UNCHANGED
DRAM head          34 cb 99 60  (magic 0x6099cb34)  ───── byte-identical both gens
```

The boot trampoline decoded **instruction-exact** with the shipped `ncore2gp`
objdump: `0x1f8 const16 a0,0 ; 0x1fb const16 a0,144 ; 0x1fe jx a0 →
enter_run @0x90 ; 0x204 halt 0`. The reset+boot stub is byte-for-byte the **same**
as MARIANA SP — the first MPLUS-vs-MARIANA divergence in PERF IRAM is at `@0xa2`
(`001640ec` vs `601540ec`, a literal-address constant, the recompile-relocation
point); the reset/boot/dispatch-trampoline prefix `0x000..0xa2` is identical. The
DRAM `.globstruct` magic `0x6099cb34` and its init block `[0x18:0x38]`
(`4×0x00001000` + `4×0x00ffffff`) are byte-identical CAY↔MAR↔MPLUS.

> **GOTCHA — the +0x1c is the MARIANA shift CARRIED, not a new v4+ shift.** Reading
> head bytes alone, the +0x1c relocation (`06 76`→`06 7d`) belongs to the
> CAYMAN→MARIANA transition; MARIANA→MARIANA_PLUS adds **zero** further shift —
> `j 0x1f8`/`j 0x204` on both gens, byte-identical. The boot target
> `→ enter_run @0x90` is unchanged across all three generations.

The DEBUG IRAM decodes a genuine, separately-compiled `cayman/seq/` sequencer —
not a stub. Census (native `ncore2gp` objdump): MARIANA_PLUS SP DEBUG IRAM
**524 `entry` / 743 `retw` / 1621 `call8` / 2167 `const16`** vs MARIANA
**532 / 735 / 1539 / 1977** — **larger** on MARIANA_PLUS (more `call8`/`const16`),
consistent with the bigger DEBUG_IRAM (`0x1a3e0` vs `0x190e0`) and the inlined DGE
fast-path bulk (§6). The FLIX-vector datapath is partly bundle-interleaved by the
linear sweep (the documented FLIX-desync limitation); the windowed-ABI control spine
decodes cleanly.

---

## 5. The SP handler diff — 18 == 18, the STABLE control core

Method (identical to the baseline and the sibling pages): extract every
single-token `S: <OpName>` from each DEBUG DRAM (regex
`^S: [A-Za-z][\w/-]*$`), `sort -u`, set-diff. SP's leaner string pool yields
**clean single-token** lines (no glued-prefix trap), so the regex isolates the 18
names with no false positives on either gen.

**RESULT — the headline:**

```text
MARIANA_PLUS SP = 18 handlers ;  MARIANA SP = 18 handlers
ADDED = 0 ;  REMOVED = 0 ;  comm -3 EMPTY ;  diff -q IDENTICAL
```

The 18 names, byte-for-name identical on both gens (the EXACT 5-way intersection):

```text
AluOp  BRANCH  BranchPrefetchHint  Event_Semaphore  EXT_BREAK  Halt  INS_BREAK  INS_FL
MOVE  NOP  NOTIFY  POLL_SEM  Redirect  SET_OM  STRONG_ORDER  TensorLoad  TensorStore  WRITE
```

By function: control-flow/fetch `{BRANCH, BranchPrefetchHint, Redirect, Halt}`;
debug/break `{EXT_BREAK, INS_BREAK, INS_FL}`; data-move
`{MOVE, TensorLoad, TensorStore, WRITE}`; scalar-ALU `{AluOp}`; ordering
`{SET_OM, STRONG_ORDER}`; **sync/EVT_SEM** `{Event_Semaphore, POLL_SEM, NOTIFY}`;
no-op `{NOP}`. `EngineNop` — the "control core vs lean compute engine"
discriminator — is **absent on SP on both gens** (it is present on PE/POOL/DVE);
SP's 18 contain `NOP` (scalar no-op) but never `EngineNop`, so SP remains the only
engine with no member outside the all-five intersection. The sync triple is the
**shared** EVT_SEM core present on all five engines; the full EVT_SEM/barrier
mechanism is derived in [mariana-sp.md §5](./mariana-sp.md) and is **unchanged**.
`[roster HIGH/OBSERVED; EVT_SEM mechanism CARRIED]`

> **QUIRK — SP is the engine that proves the "common chassis" model, twice.** The
> other four engines each *add* a compute/RNG subset onto the shared 18-handler
> core; on the CAYMAN→MARIANA step they each *diverged* (ACT +3, DVE +7, PE +4,
> POOL Q7 RNG body). SP's extension is the **empty set** on *every* gen — so SP's
> handler image is, by construction, gen-invariant. The chassis with nothing
> bolted on cannot diverge; SP's stability across *both* the v4 and v4+
> transitions is the strongest single piece of evidence that the MARIANA family
> engines are the *same* SEQ firmware recompiled, not a new model.

### 5.1 SEQ dispatch table — segmented/base-subtraction, opcode space STABLE

SP uses the **segmented / Sunda-mode HW-decode** dispatch flavor: a register-base
subtraction `sub a2,a2,a3` (encoding `3022c0`) feeding const16-base `addx4` jump
tables — **not** the `addi a2,a2,-65` ASCII normalization of DVE/POOL, nor the
raw-compare chain of PE/ACT. The dispatch head decodes instruction-exact and is
**byte-identical between gens** at the **byte-identical IRAM address** `0x286c`:

```text
MARIANA_PLUS SP @0x2862: const16 a2,8 ; const16 a2,0x4c14 ; l32i.n a2,a2,0 ;
                         l32i.n a3,a1,28 ; sub a2,a2,a3 (@0x286c, enc 3022c0) ;
                         srli a2,a2,6 ; …
MARIANA      SP        : IDENTICAL sub a2,a2,a3 (enc 3022c0) at the SAME @0x286c.
```

(The `const16 a2,0x4c14` base literal is a recompile-relocated literal-pool
constant; the dispatch *mechanism* is byte-identical.) The DRAM dispatch table
sits at **file `0x800` on both gens**; the first-16 LE-32 trampoline pointers:

```text
MPLUS  @0x800: 0x293c 0x298b 0x298b 0x298b 0x2945 0x2934 0x291c 0x2924
               0x2965 0x298b 0x2955 0x295d 0x298b 0x298b 0x298b 0x298b   ; default = 0x298b
MARIANA@0x800: 0x293c 0x2975 0x2975 0x2975 0x2945 0x2934 0x291c 0x2924
               0x2965 0x2975 0x2955 0x295d 0x2975 0x2975 0x2975 0x2975   ; default = 0x2975
```

The **real handler-slot trampolines are BYTE-IDENTICAL** between gens
(slots 0/4/5/6/7/8/10/11 = `0x293c, 0x2945, 0x2934, 0x291c, 0x2924, 0x2965,
0x2955, 0x295d` on both); **only** the default/Bad-Opcode trampoline relocated
**+0x16** (`0x2975` → `0x298b`, occupying the sparse default band — slots 1–3, 9,
12–15). The table base (`0x800`), the sparse default-band pattern (most slots →
default, matching the 18-handler sparse binding), and the real-slot byte-identity
are **gen-stable. No opcode-space growth** (contrast the v4 PE 25→29 and DVE
170→187 — **neither recurs here**). The `S: Dispatch opcode=0x%x` log relocated
`@0xa98` (MAR) → `@0xb38` (MPLUS, +0xa0 recompile shift). The dual-mode strings
(`S: NX in HW Decode mode` / `S: NX in Sunda mode: HW decode disabled` /
`S: Sunda seq Loop`), `sunda_fast_fetch`, and the `ErrorHandler` arms
(`Bad Opcode(0x%x)` / `Illegal Instruction` / `FP Error` / `Int Div Zero Error`,
source `cayman/seq/src/handlers/exception_handler.hpp`) are byte-for-name
identical both gens.
`[base/real-slot-identity/default-reloc HIGH; per-opcode row binding MED]`

> **NOTE — the "~161" count.** The dispatch table tail bleeds into a small
> adjacent jump table on both gens; `~161` is the clean trampoline run, identical
> in length on both. The default-only relocation does not affect the no-growth
> conclusion.

---

## 6. The DGE fast-path test — GEN-WIDE, the sharp finding

**The question (only SP can answer cleanly):** SP is the sync/control core with
**no** DGE/reshape dispatch handler. Does the v4+ DGE fast-path
(`dge_decode_fast` + helpers) appear on SP? If absent, the fast-path is
engine-selective (only the DGE-hosting engines). If present, it is gen-wide — a
SEQ-infrastructure recompile feature on every NX label. The sibling pages
INFERRED (ACT, #761) then OBSERVED (DVE, #762) it on the compute engines; SP is
the decisive case.

**Answer — PRESENT. The fast-path is TRULY GEN-WIDE.** The four DGE fast-path
strings, **absent on MARIANA SP (`count 0`)**, are **PRESENT on MARIANA_PLUS SP**
— in the DEBUG DRAM **and** the symbol-bearing TEST DRAM (×1 each — *compiled
code*, not stray text):

| string | MARIANA SP | MPLUS SP DEBUG DRAM | MPLUS SP TEST DRAM |
|---|:---:|:---:|:---:|
| `dge_decode_fast.cpp` | 0 | 1 | 1 |
| `dge_reshape_memcopy_transpose_fast` | 0 | 1 | 1 |
| `tensor_reshape_transpose_sb2sb` | 0 | 1 | 1 |
| `wait_for_credit` | 0 | 1 | 1 |
| `push REGWRITE to DMA[%d]` (reverse) | 1 | **0** | 0 |

The reverse delta — the retirement of `S: push REGWRITE to DMA[%d]` (present on
MARIANA SP, gone on MARIANA_PLUS SP) — is **identical to the ACT/DVE/PE/POOL
delta** (likely folded into the fast path). The shared DGE machinery present on
*both* gens (`dge_reshape.cpp`, `dge_shape`, `S: DGE: Select backend Pool/RTL`,
`S: DGE Reshape: Analyzed/Assessed tensor …`) confirms the four new strings
**refine an existing subsystem**, they are not a new dispatch handler (the handler
set is unchanged, §5).

> **THE VERDICT — gen-wide, not engine-selective.** The DGE fast-path landed on
> **all five** MARIANA_PLUS NX sequencers — ACT, DVE, PE, POOL, **and SP** — even
> the lean sync/control SP that hosts **no** DGE/reshape dispatch handler at all.
> It is therefore a **SEQ-infrastructure feature shipped gen-wide across every NX
> label** (the `cayman/seq` codebase recompiled for v4+), independent of each
> engine's handler subset — **not** compute-engine-only, **not** POOL-only. POOL
> remains the architectural *home* (the SW-DGE backend / `dge_backend_rtl` live
> there); but the fast-path translation unit is **linked into every NX image**, SP
> included. With SP — the engine that has no functional reason to carry it — the
> "recompile" is confirmed as the only gen-wide change and the fast-path as its
> single functional payload.
> `[HIGH/OBSERVED; the "throughput-optimization" reading INFERRED-HIGH]`

This DGE fast-path code is the **only** substantive functional change at the SP
image level, and it accounts for the IRAM *growth* (§7) — the opposite of the
CAYMAN→MARIANA SP shrink. SP's handler/opcode/dtype dispatch surface is otherwise
byte-for-name unchanged.

---

## 7. dtype / PROF / size — minimal, unchanged

* **dtype — minimal, unchanged.** SP carries **no**
  `FP4`/`CPTC`/`MXTENSOR`/`SFP8`/`QuantizeMx`/`proc_4bit`/`fp8_e` strings
  (`rg -ic` = 0 across all 6 images, both gens). The only dtype constants are
  `NEURON_ISA_TPB_DTYPE_{UINT32, INT32, FP32}` (the `move.cpp:41` assertion,
  *"highest priority is full-register moves. TODO other dtypes"*), byte-identical
  to MARIANA. SP is the scalar/control core with no MX/dequant surface, so the
  MARIANA_PLUS FP4/MX expansion — numeric and named only on the POOL Q7 core —
  leaves **no SP footprint**.

* **PROF — none, so no divergence OR reuse is possible.** SP ships no
  `PROF_CAM`/`PROF_TABLE` on either gen (`nm | rg -c 'MARIANA_PLUS_NX_SP.*PROF'`
  = 0; the archive has 12 `img_` members, **zero** `hwdecode_` members — the only
  NX engine without PROF). So the gen-wide per-engine PROF **byte-identical reuse**
  the other four engines exhibit (ACT `326bc0dd`, DVE `ca588683`, PE `43475cec`,
  POOL `0951b326`, all reused verbatim from MARIANA) simply **has no SP instance.**

* **Q7 / EXTISA — none.** Every carve is a flat device segment with no ELF magic
  (`head -c4` = the reset vector `06 7d 00 00` for IRAM, the magic `34 cb 99 60`
  for DRAM — *not* `7f 45 4c 46`). No Q7 core, no EXTISA — those are POOL-Q7-only.

* **size — 6/6 distinct, IRAM GREW (the v4+ direction).** Where the
  CAYMAN→MARIANA SP transition **shrank** IRAM, MARIANA→MARIANA_PLUS SP **grew**
  it in every variant — the same direction as MARIANA_PLUS ACT/DVE/PE/POOL, the
  bulk being the new DGE fast-path code (§6). Positional 16-byte-block similarity
  is low (IRAM 5–7%, DRAM 31–44%): a full recompile with relocated layout +
  inserted code, **not** a patch.

  | IMAGE | MAR size | MPLUS size | dSize | identical? |
  |---|---:|---:|---:|---|
  | `DEBUG_IRAM` | `0x190e0` | `0x1a3e0` | `+0x1300` | NO (recompile + DGE) |
  | `DEBUG_DRAM` | `0x6440` | `0x6660` | `+0x220` | NO |
  | `PERF_IRAM` | `0x153c0` | `0x1c300` | `+0x6f40` | NO |
  | `PERF_DRAM` | `0x2e60` | `0x3040` | `+0x1e0` | NO |
  | `TEST_IRAM` | `0x14000` | `0x1a8a0` | `+0x68a0` | NO |
  | `TEST_DRAM` | `0x3160` | `0x33a0` | `+0x240` | NO |

* **source/errata strings.** `addr_bits.hpp` is present on **both** gens
  (`grep` 1 each — the gen-wide MARIANA address-rerouting header);
  `translate_cayman+.hpp` present on both; `mariana-4062` errata is **absent**
  (DVE-only); there is **no** `mariana_plus-NNNN` errata string. The gen self-name
  is `S: BEGIN on mariana_plus` vs `S: BEGIN on mariana` (the **only** self-name
  change); the source tree is retained as `cayman/seq/src/…` on both gens (the
  `exception_handler.hpp` path is
  `/opt/workspace/NeuronUcode/cayman/seq/src/handlers/`, the `move.cpp` path
  `/opt/workspace/NeuronUcode/src/decode/move.cpp` on both — a build-string
  artifact across the generation).

* **engine_idx = 4 (TPB_SP), confirmed.** The shipped ISA enum
  `neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
  `NEURON_ISA_TPB_NEURON_ENGINE { PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5 }`
  fixes `TPB_SP=4`; `TPB_SP(4)` and `TOP_SP(5)` are **distinct** enumerators. The
  carved image is the per-NeuronCore `TPB_SP` (engine 4), NOT the standalone
  `TOP_SP` (engine 5) collective sequencer (the
  [collective end-to-end](../orientation/collective-end-to-end.md) `engine_idx 5`
  target). `engine_idx` is runtime-computed: the DRAM carries
  `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`
  — the same late-bound identity string as MARIANA, the architectural reason all
  five NX engines share the identical reset+boot stub. MARIANA_PLUS shares the
  cayman/mariana ISA (no `neuron_mariana_plus_arch_isa` dir), so the
  ISA/struct/dtype surface **is** MARIANA's.
  `[HIGH/OBSERVED; runtime-compute INFERRED-HIGH]`

> **NOTE — PERF/TEST strip the logs, mechanism invariant.** SP DEBUG DRAM = 141
> `S:` lines on MARIANA_PLUS (vs MARIANA 142 — the `push REGWRITE to DMA[%d]` log
> dropped, §6); SP PERF/TEST DRAM = 0. The dispatch mechanism (reset vector, table
> architecture, ErrorHandler/Dispatch arms) is invariant across builds. A
> DEBUG→PERF swap is a pure observability change.

---

## 8. The MARIANA_PLUS matrix — COMPLETE

With SP carved and diffed, **all five MARIANA_PLUS NX engines are now
cross-gen-diffed vs MARIANA.** The per-engine MARIANA→MARIANA_PLUS divergence,
each row anchored to its committed page:

| engine | idx | handlers M→M+ | opcode space | PROF (vs MARIANA) | DGE fast-path | the v4+ change | page |
|---|---:|---|---|---|:---:|---|---|
| **PE** | 0 | 29 == 29 (`+0/−0`) | stable (raw-compare) | `43475cec` byte-ident | PRESENT | recompile + DGE fast-path; PROF reused; IRAM grew. | [pe](./mariana-plus-pe.md) #763 |
| **ACT** | 1 | 29 == 29 (`+0/−0`) | stable (raw-compare) | `326bc0dd` byte-ident | PRESENT | recompile + DGE fast-path; PROF reused; +0x1c no-further; IRAM grew. | [act](./mariana-plus-act.md) #761 |
| **POOL** | 2 | 41 == 41 (`+0/−0`, NX) | stable (addi-0x41) | `0951b326` byte-ident | PRESENT (**HOME**) | recompile + DGE fast-path every variant; POOL = fast-path home (SW-DGE backend); PROF reused; IRAM grew. | [pool](./mariana-plus-pool.md) #764 |
| **DVE** | 3 | 53 == 53 (`+0/−0`) | stable (addi-0x41) | `ca588683` byte-ident | PRESENT | recompile + DGE fast-path; PROF reused; IRAM grew. | [dve](./mariana-plus-dve.md) #762 |
| **SP** | 4 | **18 == 18 (`+0/−0`)** | ~161 (seg/sub, real-slots byte-ident) | **NONE** (no PROF) | **PRESENT** | recompile + DGE fast-path (**even on the lean sync/control core**); +0x1c no-further; IRAM grew. The degenerate lower bound. | **this page** |

### 8.1 The gen-wide v4→v4+ invariants (all five engines)

* **RESET — the +0x1c MARIANA NX shift is RETAINED with NO further shift**
  (`j 0x1f8` / `j 0x204` on every engine; boot `→ enter_run @0x90` byte-identical);
  DRAM `.globstruct` magic `0x6099cb34` + init block unchanged.
* **HANDLER/OPCODE/DTYPE surfaces == MARIANA per engine** — **zero** handler adds
  gen-wide; opcode space stable per engine's MARIANA normalization style (ACT/PE
  raw-compare, DVE/POOL addi-0x41, **SP base-subtraction**); dtype
  `UINT32/INT32/FP32`, no named MX on any NX engine.
* **PROF — the four PROF-bearing engines REUSE MARIANA's per-engine PROF_CAM/TABLE
  BYTE-IDENTICAL** (a strong v4/v4+ kinship signal); **SP has no PROF.**
* **THE ONE FUNCTIONAL ADDITION — the DGE FAST-PATH**
  (`dge_decode_fast.cpp` + `dge_reshape_memcopy_transpose_fast` +
  `tensor_reshape_transpose_sb2sb` + `wait_for_credit`; `push REGWRITE to DMA[%d]`
  retired) — is present on **all five** NX engines, OBSERVED on ACT/DVE (committed)
  and **now SP** (this page): **truly gen-wide**, a SEQ-infra recompile feature,
  **not** engine-selective. POOL is the architectural home; the fast-path TU is
  linked into every NX image, including the lean SP with no DGE dispatch handler.
* **NX IRAM GREW gen-wide** (the DGE fast-path bulk — the opposite of the
  CAYMAN→MARIANA shrink), DRAM grew slightly; images recompile-distinct, PROF
  byte-identical.
* **The `cayman/seq` source tree retained** (`BEGIN on mariana` → `BEGIN on
  mariana_plus`); `addr_bits.hpp` gen-wide; no `mariana_plus` errata; MARIANA_PLUS
  shares the mariana ISA (own register-map dir only).

**No MARIANA_PLUS engine is a model change vs MARIANA — each is the same SEQ
engine recompiled with the gen-wide DGE fast-path + a register-map refresh on a
common chassis. SP is the degenerate lower bound that anchors the whole model: the
chassis with NOTHING added to dispatch — yet still carrying the gen-wide DGE
fast-path, the decisive proof that the fast-path is gen-wide rather than
engine-selective. THE MARIANA_PLUS ENGINE MATRIX IS COMPLETE.**

---

## 9. Adversarial self-verify

The five strongest claims, challenged against the binary:

1. **18-handler stability (`+0/−0`).** Single-token `S:` rosters extracted from
   both DEBUG DRAMs and set-diffed against the known 18: MPLUS = 18, MAR = 18,
   `ADDED = []`, `REMOVED = []`, full 18-set present in MPLUS, `EngineNop` absent
   on both. **HOLDS.**
2. **NO PROF/Q7/EXTISA.** `nm | rg -c 'MARIANA_PLUS_NX_SP.*PROF'` = 0; `…Q7_SP` =
   0; 12 getters total (no PROF pair). All carves flat (DEBUG_IRAM head `06 7d…`,
   DEBUG_DRAM head `34 cb 99 60` — no ELF magic). **HOLDS.**
3. **DGE-fast-path gen-wide verdict.** All 4 strings = 0 on MARIANA SP, = 1 on
   MARIANA_PLUS SP in *both* DEBUG and symbol-bearing TEST DRAM (compiled code);
   `push REGWRITE to DMA` retired (1→0). SP hosts no DGE handler → **gen-wide, not
   engine-selective. HOLDS.**
4. **SAME +0x1c (no further shift).** IRAM heads byte-identical across gens
   (`06 7d 00 00 00 00 86 7e 00 00…` both), boot `→ enter_run @0x90` unchanged.
   **HOLDS.**
5. **Matrix-rollup accuracy.** The four PROF hashes (`326bc0dd`/`ca588683`/
   `43475cec`/`0951b326`) are present in the committed
   `mariana-plus-act.md`; the engine-order tail anchor `SP_DEBUG_DRAM` end
   `0x7595e0` == `MARIANA_PLUS_Q7_POOL_PERF_IRAM_get.data @0x7595e0` (`nm`).
   **HOLDS.**

All five survive. The one residual frontier is the exact per-opcode→trampoline row
decode (FLIX-desync limited) — table base, real-slot byte-identity and
default-reloc are HIGH; per-row binding is the documented MED frontier.

---

## 10. Honesty ledger

**HIGH / OBSERVED:**

- Container sha `b7c67e89…632fc329b` MATCH; 12 MARIANA_PLUS NX_SP getters indexed
  (6 real + 6 zero-size cursors); `nm | rg -c` PROF/Q7 = 0/0. 6 real carves
  byte-identical (sha256) to the `libnrtucode.a` member `.rodata` — **full 6/6**
  (`3a815569`/`79c2e2fa`/`11527767`/`c9aaac9f`/`84ee1c05`/`2958154e`). MARIANA
  baseline 6/6 hashed, MATCH the committed page.
- Reset heads **byte-identical** both gens (`06 7d 00` `j 0x1f8`; `86 7e` `j
  0x204`) — SAME +0x1c, no further shift. Boot decoded native `ncore2gp`
  (`0x1f8 const16 a0,0 ; 0x1fb const16 a0,144 ; 0x1fe jx a0 → enter_run @0x90`).
  DRAM magic `0x6099cb34` + init block `[0x18:0x38]` byte-identical.
- **Handler diff: 18 == 18, +0/−0, byte-for-name identical** (the 5-way
  intersection); `EngineNop` absent both gens. SP DEBUG DRAM 141 `S:` (MAR 142);
  PERF/TEST 0.
- DEBUG IRAM census 524 `entry` / 743 `retw` / 1621 `call8` / 2167 `const16` (MAR
  532/735/1539/1977) — genuine `cayman/seq/` sequencer, larger on MARIANA_PLUS.
  Segmented `sub a2,a2,a3` (`3022c0`) at byte-identical `@0x286c` both gens.
- SEQ table base file `0x800` both; **real handler-slot trampolines BYTE-IDENTICAL**
  (`293c 2945 2934 291c 2924 2965 2955 295d`); **default trampoline reloc +0x16**
  (`0x2975`→`0x298b`); `S: Dispatch opcode` `@0xa98`→`@0xb38`; dual
  HW-Decode/Sunda + `sunda_fast_fetch`; ErrorHandler arms identical. **No
  opcode-space growth.**
- **DGE fast-path PRESENT on SP**: 4 strings `count 1` in DEBUG + TEST DRAM,
  `count 0` on MARIANA SP; `push REGWRITE to DMA[%d]` retired. **Gen-wide verdict.**
- dtype: only `UINT32/INT32/FP32`; FP4/MX/CPTC/SFP8/QuantizeMx/proc_4bit = 0 both
  gens. PROF none. Q7/EXTISA none (0 ELF magic). Size 6/6 distinct (IRAM GREW).
  `addr_bits.hpp` both; `mariana-4062` absent; no `mariana_plus` errata;
  `BEGIN on mariana_plus`.
- ISA enum `TPB_SP=4 / TOP_SP=5` distinct; runtime identity string present.
- Engine ordering: SP last NX in every variant family; `SP_DEBUG_DRAM` end
  `0x7595e0` == `MARIANA_PLUS_Q7_POOL_PERF_IRAM_get.data` (`nm`-verified); PERF
  cursor → `ACT_TEST_IRAM @0x62b180`, TEST cursor → `ACT_DEBUG_IRAM @0x6af7e0`.

**MED / INFERRED:**

- The exact per-opcode SEQ-table row decode — the FLIX-desync-limited frontier.
  Table base/real-slot-identity/default-reloc are HIGH; per-row binding
  is the documented frontier. `~161` is the clean trampoline run (tail bleeds into
  a small adjacent jump table on both gens).
- "The MARIANA_PLUS_NX_SP image runs on the `TPB_SP` (engine 4) NX core" —
  INFERRED-HIGH from the getter name + the ISA enum + the runtime identity string
  (the image carries no baked `engine_idx`).
- "MARIANA_PLUS SP adds the DGE fast-path *throughput optimization*" — the strings
  + absence + dual-build presence are OBSERVED; the functional reading is
  INFERRED-HIGH from the names + the shared DGE context.

**LOW / NOT CLAIMED:**

- Whether the standalone `TOP_SP` (engine 5) runs the same `cayman/seq/` SP build
  (this library ships one NX SP image = `TPB_SP`).
- Which silicon/runtime selects DEBUG/PERF/TEST; the exact SP-op → EVT_SEM
  APB-window binding (in the lowered instruction operands, not the firmware image).
- The exact DGE fast-path control-flow on SP (decoded only at the string/symbol
  level; the inlined fast-path body is the FLIX-desync frontier).

---

## 11. Cross-references

- [MARIANA × SP image](./mariana-sp.md) — the v4 baseline this page diffs against
  (the full carve, the 5-way-intersection derivation, the SP-vs-`TOP_SP`
  resolution, the EVT_SEM/barrier mechanism).
- [MARIANA_PLUS × ACT](./mariana-plus-act.md) (#761) /
  [MARIANA_PLUS × DVE](./mariana-plus-dve.md) (#762) — the committed v4+ sibling
  diffs feeding the §8 roll-up (ACT/DVE: recompile + DGE fast-path + PROF reuse).
- [MARIANA_PLUS × PE](./mariana-plus-pe.md) (#763) /
  [MARIANA_PLUS × POOL](./mariana-plus-pool.md) (#764) — the v4+ PE/POOL diffs
  (POOL = the DGE fast-path architectural home).
- [MARIANA+ generation delta](../generations/mariana-plus-delta.md) — the v4+
  register-map-refresh + recompile + DGE-fast-path generation model.
- [DGE Reshape Engine](../firmware/dge/dge-reshape.md) /
  [DGE 3-Backend Selector](../firmware/dge/dge-backend-selector.md) — the
  descriptor-generation subsystem the fast-path refines.
- [Image Catalog Index](./image-catalog-index.md) — the full getter map
  (MARIANA_PLUS NX_SP rows).
- [Collective end-to-end](../orientation/collective-end-to-end.md) — the standalone
  `TOP_SP` (engine 5) collective sequencer kept distinct from this per-core
  `TPB_SP` (engine 4).
- [Per-Engine Firmware Depth](../uarch/per-engine-depth.md) — the companion
  `TPB_SP`/`TOP_SP` deep-dive, EVT_SEM aperture, and the
  no-dedicated-barrier-handler result.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
