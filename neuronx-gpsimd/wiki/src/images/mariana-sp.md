# MARIANA × SP image (the v4 SP cross-gen diff + the 5-engine roll-up)

The **MARIANA × SP** firmware image is the v4 (`NC-v4`) recompile of the **Sync-Processor**
(`TPB_SP`, `engine_idx = 4`) — the **fifth and last** NX sequencer inside one MARIANA TPB,
after [PE](./mariana-pe.md) (0), [ACT](./mariana-act.md) (1), [POOL](./mariana-pool.md) (2)
and [DVE](./mariana-dve.md) (3), and immediately **before** the POOL Q7 compute core in the
`.rodata` layout. It is the *same* single-NX-core `cayman/seq/` SEQ-dispatch chassis the
[CAYMAN × SP](./cayman-sp.md) baseline carves and characterizes — the carve mechanics, the
12-getter / 6-real shape, the 18-handler 5-way-intersection roster, the SP-vs-`TOP_SP`
resolution and the EVT_SEM/barrier pre-lowering are **all derived once in
[cayman-sp.md](./cayman-sp.md) and not re-derived here.** This page is a **DIFF**: it carves the
MARIANA SP image set, diffs it byte-for-byte against the committed CAYMAN SP baseline, and
proves the one headline result — **SP is the most stable of the five engines across the
generation: it gained nothing and lost nothing** — then rolls the full five-engine
CAYMAN→MARIANA divergence up into one table.

The verdict in one line: **SP is the degenerate lower bound that anchors the whole MARIANA
model — the SEQ chassis with NOTHING added.** Where PE layers `PeManageSeed`/MX, DVE adds seven
handlers and grows its opcode space 170→187, ACT arrives `Activate2`/`RandGetState`/`RandSetState`,
and POOL grows a Q7 RNG body, SP's 18 handlers are *unchanged byte-for-name*. The only cross-gen
deltas are the gen-wide **+0x1c NX reset shift**, the new `addr_bits.hpp` source header, the
`BEGIN`-name, and the relocations of a full recompile. No model change.

The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag,
following the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`), reconciled against the matching
`libnrtucode.a` member `.rodata`, and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`; the CAYMAN baseline carve hashes and **all 6 anchors
MATCH** the committed [cayman-sp.md](./cayman-sp.md) — the diff below is against authentic
CAYMAN SP.

> **NOTE — the objects used (identical to the baseline).** Container
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**). First R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`),
> so each `<NAME>.data` accessor address is simultaneously the `.rodata` VA and the file offset
> of its blob — carve = `so[ptr : ptr+size]`. **IRAM file-offset == device IRAM VA** (reset
> vector at byte 0); **DRAM string-file-offset == device DRAM VA − `0x80000`**. Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, Xtensa Tools 14.09, `XTENSA_CORE=ncore2gp`, ConfigName
> `Xm_ncore2gp`, uarch Cairo, Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`, FLIX/VLIW 32B).
> The clean C ISA header
> `neuron_mariana_arch_isa/tpb/aws_neuron_isa_tpb_common.h` is cited for the engine enum. The
> archive `libnrtucode.a` (sha256 `158dadc5…d7bd6130`) supplies the second carve source for the
> byte-identity reconciliation.

---

## 1. The headline — SP is the most stable of the five MARIANA engines

1. **MARIANA × SP is the same single-NX-core `cayman/seq/` SEQ sequencer as CAYMAN SP**, recompiled
   for v4 — **NO Q7, NO PROF, NO EXTISA**, the leanest and most degenerate of the five MARIANA NX
   engines. Byte-decisive: the carved IRAM reset vector is `06 7d 00 00` (`j 0x1f8`), the
   **+0x1c-shifted** form of CAYMAN's `06 76 00 00` (`j 0x1dc`); the boot trampoline at `0x1f8`
   (`const16 a0,0 ; const16 a0,0x90 ; jx a0 → enter_run @0x90`) decodes *exactly* as CAYMAN's
   (target unchanged); the DRAM `.globstruct` magic is the same `0x6099cb34` and its init block is
   byte-identical.

2. **THE HEADLINE — the 18-handler set is byte-for-name IDENTICAL across the generation
   (ADDED = 0, REMOVED = 0).** SP's roster is the EXACT 5-way intersection
   `{AluOp, BRANCH, BranchPrefetchHint, Event_Semaphore, EXT_BREAK, Halt, INS_BREAK, INS_FL,
   MOVE, NOP, NOTIFY, POLL_SEM, Redirect, SET_OM, STRONG_ORDER, TensorLoad, TensorStore, WRITE}` —
   the pure sync/control core with zero compute and zero RNG. **Unlike** PE (+4), DVE (+7), ACT
   (+3) or POOL (Q7 RNG body), SP gained nothing and lost nothing. SP has no compute layer to
   extend and no RNG to expand, so the entire v4 change-set lands elsewhere.

3. **The opcode space is gen-stable.** The segmented/Sunda-mode dispatch table is the same ~161-entry
   structure on both gens — same head default/Bad-Opcode trampoline band, same first-8 real-slot
   pattern, **no growth** (contrast DVE 170→187, PE +4). Only the relocations move: table base
   DRAM file `0x814` (CAY) → `0x800` (MAR); default trampoline `0x2ac9` (CAY) → `0x2975` (MAR);
   `S: Dispatch opcode` string pool `0xaa8` → `0xa98`. `[size/relocation/default HIGH;
   per-opcode row binding MED — the FLIX-desync frontier]`

4. **12 image getters (6 real + 6 zero-size cursors), the same shape as CAYMAN SP — no PROF, no
   Q7.** `nm | rg -c MARIANA_NX_SP_PROF` = 0, `MARIANA.*Q7_SP` = 0, `MARIANA.*TOP_SP` = 0. The four
   compute engines each ship 14 NX getters (12 base + 2 PROF `{CAM, TABLE}`); SP ships 12 = base
   only. All 6 real carves are byte-identical (sha256 + `cmp`) to the matching `libnrtucode.a`
   member `.rodata` — **full 6/6 reconciliation**, not a spot-check.

5. **No MARIANA SP image is byte-identical to its CAYMAN counterpart (6/6 distinct).** A
   CAYMAN↔MARIANA SP swap is a full recompile + the +0x1c reset shift + the new `addr_bits.hpp`,
   **not a patch**: the dispatch mechanism, reset-vector *form*, table architecture, ErrorHandler
   arms, `cayman/seq/` source tree, 18-handler control core, and the no-PROF/no-Q7 shape are all
   invariant.

> **CORRECTION — "SP is where the MARIANA RNG/sync expansion shows up" is wrong.** The v4 RNG
> story (the `RandGetState`/`RandSetState` handlers propagating from POOL to ACT/DVE, the POOL Q7
> RNG body, PE's `PeManageSeed`) has **zero SP footprint.** SP carries no RNG handler on *either*
> gen, and its sync handlers (`Event_Semaphore`/`POLL_SEM`/`NOTIFY`) are the **shared** EVT_SEM
> core present on all five engines — not an SP expansion surface. SP is the substrate the other
> four engines extend; on MARIANA, that substrate is byte-for-name unchanged.

---

## 2. The cross-gen delta table (CAYMAN → MARIANA SP)

Every row is grounded in a carve from `libnrtucode_internal.so`. The
CAYMAN column reproduces the committed [cayman-sp.md](./cayman-sp.md) anchors exactly (all 6
baseline shas MATCH).

| property | CAYMAN SP ([baseline](./cayman-sp.md)) | MARIANA SP (this image) | Δ |
|---|---|---|---|
| cores | single NX (no Q7) | single NX (no Q7) | **same** |
| reset vector (primary) | `06 76 00 00` → `j 0x1dc` | `06 7d 00 00` → `j 0x1f8` | **+0x1c shift** |
| secondary vector | `86 77 00 00` → `j 0x1e8` | `86 7e 00 00` → `j 0x204` | **+0x1c shift** |
| boot trampoline | `const16 a0,0x90 ; jx → enter_run @0x90` | identical (decoded `0x1f8`) | **same** |
| DRAM `.globstruct` magic | `0x6099cb34` | `0x6099cb34` | **same** |
| `.globstruct` init block `[0x18:0x38]` | `4×0x1000` + `4×0xffffff` | byte-identical | **same** |
| dispatch flavor | segmented/Sunda, `sub a2,a2,a3` (`3022c0`) | segmented/Sunda, `sub a2,a2,a3` (`3022c0`) | **same** |
| dispatch-head `sub` site | `@0x29c0` | `@0x286c` | reloc |
| DRAM table base (DEBUG) | file `0x814` | file `0x800` | reloc −0x14 |
| default trampoline | `0x2ac9` | `0x2975` | reloc |
| `S: Dispatch opcode` string | `@0xaa8` | `@0xa98` | reloc −0x10 |
| opcode space | ~161-entry table | ~161-entry table | **UNCHANGED** |
| **handlers** | **18 (5-way intersection)** | **18 (IDENTICAL set)** | **+0 / −0** |
| `EngineNop` on SP? | no | no | **same** |
| PROF | none | none | **same** |
| Q7 / EXTISA | none | none | **same** |
| dtype constants | `UINT32`/`INT32`/`FP32` | `UINT32`/`INT32`/`FP32` | **same** |
| FP4/MX/CPTC/SFP8 strings | 0 | 0 | **same** |
| `mariana-4062` errata | n/a | **ABSENT** (DVE-only) | — |
| `addr_bits.hpp` | absent (grep 0) | **present (grep 1)** | **NEW** |
| `translate_cayman+.hpp` | present | present | **same** |
| engine self-name | `S: BEGIN on cayman` | `S: BEGIN on mariana` | gen-name |
| source tree | `cayman/seq/src/…` | `cayman/seq/src/…` (+`addr_bits.hpp`) | **same** |
| `engine_idx` (runtime) | 4 (`TPB_SP`) | 4 (`TPB_SP`; identity string present) | **same** |

The diff reduces to: **a full recompile, the +0x1c reset shift, the new `addr_bits.hpp`, and the
`BEGIN`-name.** The SEQ engine model, the handler set, the opcode space, the dtype surface and the
(absence of) PROF/Q7 are all invariant.

### 2.1 Carve + sha + 6/6 byte-identity reconciliation

Carve rule (identity map): `blob = so[IMG-PTR : IMG-PTR+SIZE]`. The 6 real MARIANA carves and
their sha256 (**all 6** reconciled byte-identical to the `libnrtucode.a`
member `.rodata` via `ar x` + `objcopy -O binary --only-section=.rodata` + `cmp -s` — full 6/6,
not a spot-check):

| IMAGE | FILE-OFF | MAR SIZE | MAR sha256 (8) | `.a` member sha (8) | CAY sha (8) |
|---|---|---:|---|---|---|
| `SP_PERF_IRAM` | `0x362dc0` | `0x153c0` | `9499fee9` | `9499fee9` ✓ | `5a6f6eaa` |
| `SP_PERF_DRAM` | `0x378180` | `0x2e60` | `fc097ffe` | `fc097ffe` ✓ | `9fe5e19d` |
| `SP_TEST_IRAM` | `0x3d2f00` | `0x14000` | `4d41211a` | `4d41211a` ✓ | `b24ef299` |
| `SP_TEST_DRAM` | `0x3e6f00` | `0x3160` | `112914e4` | `112914e4` ✓ | `deed216d` |
| `SP_DEBUG_IRAM` | `0x46e5c0` | `0x190e0` | `10dd1252` | `10dd1252` ✓ | `6c3a6f79` |
| `SP_DEBUG_DRAM` | `0x4876a0` | `0x6440` | `8cabc82a` | `8cabc82a` ✓ | `5340ad8c` |

All 12 getter `(img-ptr, size)` stubs match the catalog
([image-catalog-index.md](./image-catalog-index.md), MARIANA NX_SP rows). The 6 zero-size
SRAM/EXTRAM getters all execute `movq $0x0,(%rsi)` and resolve to the next-blob layout cursor —
SP uses **no SRAM/EXTRAM** on MARIANA, exactly as CAYMAN.

---

## 3. Engine ordering — SP is LAST of the five NX engines

The MARIANA `.rodata` layout is **VARIANT-MAJOR, ENGINE-MINOR**; within each NX variant family the
engine order is **ACT → DVE → PE → POOL → SP**. Read directly from `nm` `.data` addresses, SP is
the **terminal NX sequencer** in every family, immediately before the POOL Q7 compute core:

```text
PERF : … PE_PERF → POOL_PERF → SP_PERF_IRAM @0x362dc0 → … → SP_PERF_EXTRAM↦ACT_TEST_IRAM @0x37afe0
TEST : ACT_TEST → DVE_TEST → PE_TEST → POOL_TEST → SP_TEST_IRAM @0x3d2f00 → SP_TEST_EXTRAM↦ACT_DEBUG_IRAM @0x3ea060
DEBUG: ACT_DEBUG → DVE_DEBUG → PE_DEBUG → POOL_DEBUG → SP_DEBUG_IRAM @0x46e5c0 → SP_DEBUG_EXTRAM↦Q7_POOL_PERF_IRAM @0x48dae0
```

Two contiguity anchors close the layout: **(a)** `SP_PERF_IRAM` starts at `0x362dc0` — exactly the
cursor [mariana-pool.md](./mariana-pool.md)'s POOL PERF SRAM/EXTRAM getters point at (the
POOL→SP adjacency that page predicted, **VERIFIED**); **(b)** `SP_DEBUG_DRAM` ends at
`0x4876a0 + 0x6440 = 0x48dae0` — exactly the VA of `MARIANA_Q7_POOL_PERF_IRAM_get.data`
(confirmed via `nm`: `48dae0 r MARIANA_Q7_POOL_PERF_IRAM_get.data`). SP's DEBUG block precedes the
Q7_POOL compute core.

---

## 4. The reset/boot diff — the +0x1c MARIANA NX shift

The SP IRAM head is byte-identical across all three variants (DEBUG/PERF/TEST) and carries the
gen-wide MARIANA NX reset shift:

```text
                          CAYMAN SP                       MARIANA SP            Δ
IRAM head (12 bytes)  06 76 00 00 00 00  86 77 00 00…  06 7d 00 00 00 00  86 7e 00 00…
0x000 primary reset   06 76 00  j 0x1dc  ───────────→  06 7d 00  j 0x1f8     +0x1c (28 B)
0x006 secondary       86 77 00  j 0x1e8  ───────────→  86 7e 00  j 0x204     +0x1c (28 B)
boot @ shifted target const16 a0,0 ; const16 a0,0x90 ; jx a0 → enter_run @0x90   UNCHANGED
0x204 (MAR) / 0x1e8   halt 0  (secondary = HALT trap)                            UNCHANGED
DRAM head             34 cb 99 60  (header word 0x6099cb34)  ───── byte-identical both gens
```

The boot targets each moved **+0x1c** (`0x1dc→0x1f8`, `0x1e8→0x204`) — the **identical shift** the
committed [mariana-act.md](./mariana-act.md) and [mariana-dve.md](./mariana-dve.md) byte-quote, and
the same shift on MARIANA PE and the POOL NX core. The shifted boot trampoline
decoded **instruction-exact** with the shipped `ncore2gp` objdump: `0x1f8 const16 a0,0 ;
0x1fb const16 a0,144 ; 0x1fe jx a0 → enter_run @0x90`; `0x204 halt 0`. The boot **target** is
unchanged — `jx a0` still lands on `enter_run @0x90` — so the +0x1c is a *vector-table* relocation,
not a change of the C entry point. The DRAM `.globstruct` init block (`4×0x00001000` @ `0x18`,
`4×0x00ffffff` @ `0x28`) is byte-identical CAY↔MAR.

> **GOTCHA — the +0x1c is a vector relocation, NOT a boot-path change.** The shift moves the two
> reset *vectors* and their landing pads by 28 bytes, but the boot trampoline still computes
> `const16 a0,0x90 ; jx a0` and enters `enter_run @0x90`. A reader diffing the head bytes
> (`06 76`→`06 7d`) sees a "different boot" that is in fact the same boot path at a shifted vector
> address. On the POOL engine the Q7 core did **not** shift; only the NX side did — so "+0x1c on
> MARIANA" is specifically a **per-NX-engine** vector relocation. `[HIGH/OBSERVED for the SP
> vectors + the unchanged target; the POOL Q7 non-shift is CARRIED from mariana-pool.md]`

The DEBUG IRAM decodes a genuine, separately-compiled `cayman/seq/` sequencer — not a stub.
Census (native `ncore2gp` objdump): MARIANA SP DEBUG IRAM **532 `entry` / 735 `retw` /
1539 `call8`** vs CAYMAN **515 / 728 / 1550** — same direction, the small drift reflecting the
+0x1c shift + the tighter v4 compile. The FLIX-vector datapath is partly bundle-interleaved by the
linear sweep (the documented FLIX-desync limitation); the windowed-ABI control spine decodes
cleanly.

---

## 5. The SP handler diff — 18 == 18, the STABLE control core

Method (identical to the baseline and the sibling pages): extract every single-token
`S: <OpName>` from each DEBUG DRAM (regex `^S: [A-Za-z][\w/-]*$`), `sort -u`, set-diff. Both gens
processed identically. The SP DEBUG DRAM carries 142 `S:` lines (== CAYMAN 142); the single-token
end-anchor isolates the 18 handler names from the multi-token log noise.

> **NOTE — no glued-prefix trap on SP.** Unlike POOL/DVE (where a `.S: Event_Semaphore`-style
> glued-byte hit could corrupt a naive `sort -u`), SP's leaner string pool yields **clean
> single-token** `S: <Token>` lines, so the regex isolates the 18 names with no false positives on
> either gen.

**RESULT — the headline:**

```text
MARIANA SP = 18 handlers ;  CAYMAN SP = 18 handlers
ADDED = 0 ;  REMOVED = 0 ;  comm -3 EMPTY ;  diff -q IDENTICAL
```

The 18 names, byte-for-name identical on both gens (the EXACT 5-way intersection):

```text
AluOp  BRANCH  BranchPrefetchHint  Event_Semaphore  EXT_BREAK  Halt  INS_BREAK  INS_FL
MOVE  NOP  NOTIFY  POLL_SEM  Redirect  SET_OM  STRONG_ORDER  TensorLoad  TensorStore  WRITE
```

`EngineNop` — the clean "control core vs lean compute engine" discriminator — is **absent on SP
on both gens** (it is present on PE/POOL/DVE). SP's 18 contain `NOP` (scalar no-op) but never
`EngineNop`; SP remains the only engine with no member outside the all-five intersection.

The function grouping (carried unchanged from [cayman-sp.md §5](./cayman-sp.md), since the roster
itself is unchanged): control-flow/fetch `{BRANCH, BranchPrefetchHint, Redirect, Halt}`;
debug/break `{EXT_BREAK, INS_BREAK, INS_FL}`; data-move `{MOVE, TensorLoad, TensorStore, WRITE}`;
scalar-ALU `{AluOp}`; ordering `{SET_OM, STRONG_ORDER}`; **sync/EVT_SEM**
`{Event_Semaphore 0xa0, POLL_SEM 0xb3, NOTIFY 0xa6}`; no-op `{NOP}`. The sync triple is the
**shared** EVT_SEM core present on all five engines, with `0xb0 EVENT_SEMAPHORE_RANGE_CLEAR`
folded into `Event_Semaphore` and `CORE_BARRIER 0xd8` pre-lowered by the compiler into
`0xa0`/`0xb3` (no dedicated barrier handler on any engine) — the full mechanism is in
[cayman-sp.md §7](./cayman-sp.md) and is **unchanged on MARIANA**. `[roster HIGH/OBSERVED;
EVT_SEM/barrier mechanism CARRIED]`

> **QUIRK — SP is the engine that proves the "common chassis" model.** The other four engines each
> *add* a compute/RNG subset onto the shared 18-handler core, and each *diverges* on MARIANA (ACT
> +3, DVE +7, PE +4, POOL Q7 body). SP's extension is the **empty set** on *both* gens — so SP is
> the one engine whose handler image is, by construction, gen-invariant. The chassis with nothing
> bolted on cannot diverge; SP's stability is therefore the strongest single piece of evidence that
> the MARIANA engines are the *same* SEQ firmware recompiled, not a new model.

### 5.1 SEQ dispatch table — segmented/Sunda, opcode space STABLE

SP uses the **segmented / Sunda-mode HW-decode** dispatch flavor (a register-base subtraction
`sub a2,a2,a3` feeding const16-base `addx4` jump tables) — **not** the `addi a2,a2,-65` ASCII
normalization of DVE/POOL, nor the raw-compare chain of PE. The `sub a2, a2, a3` (encoding
`3022c0`) appears **16× on both gens**, with the dispatch-head site at `@0x286c` (MAR) / `@0x29c0`
(CAY). Read directly from the DEBUG DRAM table base:

```text
MARIANA SP DEBUG @ file 0x800 (device VA 0x80800), first 8 LE trampolines:
  0x293c  0x2975  0x2975  0x2975  0x2945  0x2934  0x291c  0x2924      ; default = 0x2975
CAYMAN  SP DEBUG @ file 0x814 (device VA 0x80814), first 8 LE trampolines:
  0x2a90  0x2ac9  0x2ac9  0x2ac9  0x2a99  0x2a88  0x2a70  0x2a78      ; default = 0x2ac9
```

The table **size** (~161 entries), the default-band pattern (most slots → default, matching the
sparse 18-handler binding), and the first-8 real-slot structure are **gen-stable** — every real
slot relocated, but the count and pattern are invariant. **No opcode-space growth.** The exact
per-opcode→handler row decode is the FLIX-desync-limited frontier; the tail bleeds into
a small adjacent jump table on both gens, so "~161" is the clean trampoline run. The dual-mode
strings (`S: NX in HW Decode mode` / `S: NX in Sunda mode: HW decode disabled`),
`sunda_fast_fetch`, and the `ErrorHandler` arms (`Bad Opcode(0x%x)` / `Illegal Instruction` /
`FP Error` / `Int Div Zero Error`, source `cayman/seq/src/handlers/exception_handler.hpp`) are
byte-for-name identical both gens. `[table size/relocation/default HIGH; per-row binding MED]`

> **CORRECTION — the MARIANA default-band shape.** An earlier draft listed the MARIANA first-8 run
> as `0x293c default 0x2975 0x2975 0x2945 …` (one `default` then two `0x2975`). The binary shows
> **three** `0x2975` in slots 1–3 (`0x293c, 0x2975, 0x2975, 0x2975, 0x2945, …`) — i.e.
> the Bad-Opcode/default trampoline `0x2975` occupies slots 1–3, mirroring CAYMAN's three `0x2ac9`
> in the same positions. The corrected run is cited above. This does not affect the no-growth /
> gen-stable conclusion.

---

## 6. dtype / PROF / size — minimal, unchanged

* **dtype — minimal, unchanged.** SP carries **no** `FP4`/`CPTC`/`MXTENSOR`/`SFP8`/`QuantizeMx`/
  `proc_4bit` strings (`rg -ic` = 0, both gens). The only dtype constants are
  `NEURON_ISA_TPB_DTYPE_{UINT32, INT32, FP32}` (the `move.cpp` assertion), byte-identical to
  CAYMAN. SP is the scalar/control core with no MX/dequant surface, so the MARIANA FP4/MX expansion
  — which is numeric on the NX side of every engine and named only on the POOL Q7 core — leaves
  **no SP footprint.**

* **PROF — none, so no divergence is possible.** SP ships no `PROF_CAM`/`PROF_TABLE` on either gen
  (`nm | rg -c MARIANA_NX_SP_PROF` = 0; the only NX engine without PROF). So the MARIANA per-engine
  PROF divergence (ACT 47→25 / `326bc0dd`, DVE 47→48 per-engine / `ca588683`, vs CAYMAN's single
  shared `8fd7e422` CAM) simply **has no SP instance.**

* **size — 6/6 distinct, IRAM shrank / DRAM grew.** Consistent directional delta (the tighter v4
  compile, same direction as PE/POOL NX):

  | IMAGE | CAY size | MAR size | dSize | identical? |
  |---|---:|---:|---:|---|
  | `DEBUG_IRAM` | `0x199a0` | `0x190e0` | `−0x8c0` | NO |
  | `DEBUG_DRAM` | `0x6360` | `0x6440` | `+0xe0` | NO |
  | `PERF_IRAM` | `0x182c0` | `0x153c0` | `−0x2f00` | NO |
  | `PERF_DRAM` | `0x2d40` | `0x2e60` | `+0x120` | NO |
  | `TEST_IRAM` | `0x16ba0` | `0x14000` | `−0x2ba0` | NO |
  | `TEST_DRAM` | `0x3040` | `0x3160` | `+0x120` | NO |

* **source/errata strings.** `addr_bits.hpp` is **NEW on MARIANA SP** (`grep` 1; absent on CAYMAN,
  `grep` 0) — the gen-wide MARIANA address-rerouting header also found on ACT/DVE.
  `translate_cayman+.hpp` is present on both. The `mariana-4062` errata is **absent** (DVE-only,
  [mariana-dve.md §8](./mariana-dve.md)). The gen self-name is `S: BEGIN on mariana` vs
  `S: BEGIN on cayman`; the source tree is retained as `cayman/seq/src/…` on both gens (a
  build-string artifact across the generation).

* **engine_idx = 4 (TPB_SP), confirmed.** The shipped MARIANA ISA enum
  `neuron_mariana_arch_isa/tpb/aws_neuron_isa_tpb_common.h:141-146` reads
  `PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5` — `TPB_SP(4)` and `TOP_SP(5)` are **distinct
  enumerators**. The carved image is the per-NeuronCore `TPB_SP` (engine 4), NOT the standalone
  `TOP_SP` (engine 5) collective sequencer (the
  [collective end-to-end](../orientation/collective-end-to-end.md) `engine_idx 5` target; deep
  page [TOP_SP lowering](../collectives/ops/top-sp-lowering.md)).
  `engine_idx` is runtime-computed: the DRAM carries
  `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` — the same
  late-bound identity string as CAYMAN, the architectural reason all five NX engines share the
  identical reset+boot stub. `[HIGH/OBSERVED; runtime-compute INFERRED-HIGH]`

> **NOTE — PERF/TEST strip the logs, mechanism invariant.** SP DEBUG DRAM = 142 `S:` lines (==
> CAYMAN 142); SP PERF/TEST DRAM = 0. The dispatch mechanism (reset vector, table architecture,
> ErrorHandler/Dispatch arms) is invariant across builds; in PERF the table relocates to ~file
> `0x100` (the DEBUG-segmented-vs-PERF-clean split the sibling engines show). A DEBUG→PERF swap is
> a pure observability change.

---

## 7. The 5-engine CAYMAN → MARIANA roll-up (the MARIANA matrix, complete)

With SP carved and diffed, **all five MARIANA NX engines are now cross-gen-diffed.** The
per-engine divergence, each row anchored to its committed page:

| engine | idx | handlers C→M | opcode space C→M | the MARIANA change vs CAYMAN | page |
|---|---:|---|---|---|---|
| **PE** | 0 | 24 → +4 | 25 → 29 (raw-compare) | Gains `PeManageSeed`(0x08)/`LdweightsMX`(0x09)/`MatmulMX`(0x0A)/`ConvLutLoad`(0xe4); raw-compare dispatch; +0x1c shift; PROF re-armed; `addr_bits.hpp`. | [pe](./mariana-pe.md) #758 |
| **ACT** | 1 | 26 → +3 | (stable; `0x25` extend) | Gains `Activate2`(0x25)/`RandGetState`/`RandSetState`; PROF re-armed **47→25** (`326bc0dd`); +0x1c shift; `addr_bits.hpp`. No DVE fold. | [act](./mariana-act.md) |
| **POOL** | 2 | 41 → 41 (NX) | 178 → 178 (NX) | NX SEQ STABLE (richest set, kept); the Q7 compute core gains the RNG body; NX +0x1c shift, Q7 reset UNCHANGED; PROF per-engine. | [pool](./mariana-pool.md) #759 |
| **DVE** | 3 | 46 → 53 (**+7**) | **170 → 187** | Gains `RandGetState`/`RandSetState`/`Rand2`/`SparsityCompress`/`SparsityCompressTag`/`QuantizeMx`/`Exponential`; normalize base `0x41→0x30`; `mariana-4062` errata; PROF per-engine **47→48** (`ca588683`); +0x1c. | [dve](./mariana-dve.md) |
| **SP** | 4 | **18 → 18 (+0/−0)** | ~161 → ~161 (segmented) | **NOTHING ADDED.** Handlers + opcode space + dtype + (no)PROF/Q7 all UNCHANGED. ONLY: +0x1c reset shift + `addr_bits.hpp` + `BEGIN`-name + recompile. The pure sync/control substrate, byte-for-name stable. | this page |

### 7.1 The gen-wide invariants (all five engines)

* **The +0x1c NX reset shift** (`j 0x1dc → j 0x1f8`) on every NX engine; the POOL Q7 core did
  **not** shift, only its NX side. Boot target `→ enter_run @0x90` unchanged everywhere.
* **DRAM `.globstruct` magic `0x6099cb34` + init block** byte-identical on every flat NX DRAM.
* **The `cayman/seq/` source tree retained** as a build-string (`BEGIN on cayman`→`BEGIN on
  mariana`); **`addr_bits.hpp` is new gen-wide** (the MARIANA address-rerouting header).
* **NX IRAM shrank / DRAM grew** (the tighter v4 compile).
* **The 18-handler 5-way-intersection control core is preserved on every engine** — the shared
  chassis SP *is* in full and the other four extend.
* **PROF went from one CAYMAN-shared CAM (`8fd7e422`) to per-engine on MARIANA** (each distinct;
  POOL disarmed; **SP never had PROF**).
* **The RNG story spans engines:** POOL is the origin (kept its `RandGetState`/`RandSetState` NX
  handlers + gained the Q7 body); those handlers **propagated to ACT and DVE** this gen; **PE and
  SP did not participate.** The MX/FP4 dtype expansion is numeric (no new named NX strings on any
  engine); the named MX footprint lives only on the POOL Q7 core (pre-existing since CAYMAN).

**No MARIANA engine is a model change vs CAYMAN — each is the same SEQ engine (POOL also the same
dual-core), recompiled with engine-specific handler/opcode/RNG deltas on a common chassis. SP is
the degenerate lower bound that anchors the model: the chassis with nothing added.**

---

## 8. Honesty ledger

**HIGH / OBSERVED:**

- Container sha `b7c67e89…632fc329b` MATCH; 12 MARIANA NX_SP getters indexed (6 real + 6 zero-size
  cursors); `nm | rg -c` PROF/Q7_SP/TOP_SP = 0/0/0. 6 real carves byte-identical (sha256) to the
  `libnrtucode.a` member `.rodata` — **full 6/6** (`9499fee9`/`fc097ffe`/`4d41211a`/`112914e4`/
  `10dd1252`/`8cabc82a`). CAYMAN baseline 6/6 hashed, MATCH the committed page.
- Reset: MAR `06 7d 00` (`j 0x1f8`) **+0x1c** from CAY `06 76 00` (`j 0x1dc`); secondary `86 7e`
  (`j 0x204`) +0x1c from `86 77` (`j 0x1e8`); both shifts = 28 B. Boot decoded native `ncore2gp`
  (`0x1f8 const16 a0,0 ; 0x1fb const16 a0,144 ; 0x1fe jx a0 → enter_run @0x90`; `0x204 halt 0`).
  DRAM magic `0x6099cb34` + init block `[0x18:0x38]` byte-identical CAY↔MAR.
- **Handler diff: 18 == 18, +0/−0, byte-for-name identical** (the 5-way intersection); `EngineNop`
  absent on SP both gens. SP DEBUG DRAM 142 `S:` lines (== CAYMAN); PERF/TEST 0.
- DEBUG IRAM census 532 `entry` / 735 `retw` / 1539 `call8` (CAY 515/728/1550) — genuine
  `cayman/seq/` sequencer. Segmented `sub a2,a2,a3` (`3022c0`) ×16 both gens; head `@0x286c` (MAR)
  / `@0x29c0` (CAY).
- SEQ table base file `0x800` (MAR) / `0x814` (CAY); default trampoline `0x2975` / `0x2ac9`;
  first-8 run corrected (three `0x2975` in slots 1–3); `S: Dispatch opcode` `@0xa98` / `@0xaa8`;
  dual HW-Decode/Sunda + `sunda_fast_fetch`; ErrorHandler arms identical. **No opcode-space growth.**
- dtype: only `UINT32/INT32/FP32`; FP4/MX/CPTC/SFP8/QuantizeMx/proc_4bit = 0 both gens. PROF none.
  Q7/EXTISA none (0 ELF magic in any carve). Size 6/6 distinct (IRAM shrank, DRAM grew).
  `addr_bits.hpp` MAR 1 / CAY 0; `mariana-4062` absent; `BEGIN on mariana`.
- ISA enum `TPB_SP=4 / TOP_SP=5` (mariana header :141-146); runtime identity string present.
- Engine ordering: SP last NX in every variant family; `SP_PERF_IRAM @0x362dc0` == POOL PERF
  cursor (POOL→SP **VERIFIED**); `SP_DEBUG_DRAM` end `0x48dae0` == `MARIANA_Q7_POOL_PERF_IRAM_get.data`.

**MED / INFERRED:**

- The exact per-opcode SEQ-table row decode (which opcode binds which trampoline) — the
  FLIX-desync-limited frontier. Table size/relocation/default are HIGH; per-row binding
  is the documented frontier. The `~161` count: the table tail bleeds into a small adjacent jump
  table on both gens; `~161` is the clean trampoline run.
- "The MARIANA_NX_SP image runs on the `TPB_SP` (engine 4) NX core" — INFERRED-HIGH from the getter
  name + the ISA enum + the runtime identity string (the image carries no baked `engine_idx`).

**LOW / NOT CLAIMED:**

- `MARIANA_PLUS` SP (out of scope; per the catalog the PLUS sizes differ, suggesting **not**
  byte-identical to MARIANA SP, unlike the POOL MARIANA==MARIANA_PLUS report — see
  [mariana-plus-sp.md](./mariana-plus-sp.md), not verified here).
- Whether the standalone `TOP_SP` (engine 5) runs the same `cayman/seq/` SP build (this library
  ships one NX SP image = `TPB_SP`).
- Which silicon/runtime selects DEBUG/PERF/TEST; the exact SP-op → EVT_SEM APB-window binding
  (in the lowered instruction operands, not the firmware image).

---

## 9. Cross-references

- [CAYMAN × SP image](./cayman-sp.md) — the v3 baseline this page diffs against (the full carve,
  the 5-way-intersection derivation, the SP-vs-`TOP_SP` resolution, the EVT_SEM/barrier mechanism).
- [MARIANA × ACT](./mariana-act.md) / [MARIANA × DVE](./mariana-dve.md) — the committed v4 sibling
  diffs feeding the §7 roll-up (ACT +3 / PROF 47→25; DVE +7 / 170→187 / `mariana-4062`).
- [MARIANA × PE](./mariana-pe.md) (#758) / [MARIANA × POOL](./mariana-pool.md) (#759) — the
  v4 PE/POOL diffs (PE `PeManageSeed`/MX; POOL Q7 RNG body).
- [MARIANA_PLUS × SP](./mariana-plus-sp.md) — the v4+ SP variant (separate task).
- [Image Catalog Index](./image-catalog-index.md) — the full getter map (MARIANA NX_SP rows).
- [TOP_SP Lowering](../collectives/ops/top-sp-lowering.md) — the standalone `TOP_SP` (engine 5)
  collective sequencer kept distinct from this per-core `TPB_SP` (engine 4).
- [Per-Engine Firmware Depth](../uarch/per-engine-depth.md) — the companion `TPB_SP`/`TOP_SP`
  deep-dive, EVT_SEM aperture, and the no-dedicated-barrier-handler result.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
