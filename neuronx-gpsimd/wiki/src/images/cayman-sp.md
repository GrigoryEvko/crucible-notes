# CAYMAN × SP image (+ SP-vs-TOP_SP)

The **CAYMAN × SP** firmware image is the program for the **Sync-Processor** (`TPB_SP`,
`engine_idx = 4`) — the **fifth and last** NX engine inside one NC-v3 TPB, alongside
[PE](./cayman-pe.md) (0), [ACT](./cayman-act.md) (1), [POOL](./cayman-pool.md) (2) and
[DVE](./cayman-dve.md) (3). It is one more compiled flavor of the *same* shared `cayman/seq/`
NX-class SEQ-dispatch chassis the other four engines run — byte-identical reset vector, boot
trampoline, `.globstruct` dispatcher-state magic and SEQ dispatch-table architecture — recompiled
with the **leanest possible handler subset**. This page carves all 12 image variants byte-exact
from `libnrtucode_internal.so`, proves SP's 18-handler roster is the **exact 5-way intersection**
of all five engines (zero engine-specific handlers), resolves the **SP-vs-TOP_SP** naming collision
via the shipped ISA enum, characterizes the SP DRAM (SEQ dispatch table, *no* PROF, *no* baked
tables) and the SP↔EVT_SEM/barrier interaction, and closes the **5-engine CAYMAN set**.

The decisive result is a *negative*: **SP adds nothing.** Where PE layers five matmul micro-ops, ACT
seven activation ops and POOL twenty-three compute ops onto the shared control core, SP's entire
handler set **is** that shared control core — `{AluOp, BRANCH, BranchPrefetchHint, Event_Semaphore,
EXT_BREAK, Halt, INS_BREAK, INS_FL, MOVE, NOP, NOTIFY, POLL_SEM, Redirect, SET_OM, STRONG_ORDER,
TensorLoad, TensorStore, WRITE}`, 18 names, the precise lower bound of the SEQ engine model. SP is
the pure control/sync substrate the other four engines are built **on top of**. The leanness is the
story.

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` (sha256 `b7c67e89…`) and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`; the EVT_SEM aperture geometry, the TOP_SP block facts and the barrier
pre-lowering are CARRIED from the engine/collective pages cited inline.

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**). First R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`), so
> each `<NAME>.data` accessor address is **simultaneously** the `.rodata` VA and the file offset of
> its blob — carve = `so[ptr : ptr+size]`. The SP image blobs are the device-side `.rodata` payloads
> of the `img_CAYMAN_NX_SP_*` members. **IRAM file-offset == device IRAM VA** (reset vector at byte
> 0); **DRAM string-file-offset == device DRAM VA − `0x80000`**. Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo,
> Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`, FLIX/VLIW). The clean C ISA header
> `aws_neuron_isa_tpb_common.h` (shipped redistributable, in the same `customop` package) is cited
> for the engine enum and the opcode values. All carve sha256, the reset vector, the dispatch table,
> the boot trampoline and the handler roster were reproduced this session (objdump exit 0, empty
> stderr). `[HIGH/OBSERVED]`

---

## 1. The headline

1. **CAYMAN × SP is the same `cayman/seq/` SEQ-dispatch engine as PE/ACT/POOL/DVE**, compiled with
   the minimal handler subset. Byte-decisive: the carved IRAM reset vector is the byte-identical
   `06 76 00 00` (`j 0x1dc`) shared by all five engines; the boot trampoline at `0x1dc`
   (`const16 a0,0 ; const16 a0,0x90 ; jx a0 → enter_run @0x90`) decodes *exactly* as POOL's; the
   DRAM header word is the shared `.globstruct` magic `0x6099cb34`; the DEBUG dispatch table base is
   the same DRAM `0x814`; the assertion paths are the same `/opt/workspace/NeuronUcode/cayman/seq/
   src/…`. `[HIGH/OBSERVED]`
2. **SP = the SYNC/CONTROL sequencer** (`TPB_SP`, `engine_idx = 4`, the 5th *execution* engine inside
   one TPB). Its 18 distinct handlers are **exactly** the shared SEQ control core — `AluOp`,
   `BRANCH`, `BranchPrefetchHint`, `Event_Semaphore`, `EXT_BREAK`, `Halt`, `INS_BREAK`, `INS_FL`,
   `MOVE`, `NOP`, `NOTIFY`, `POLL_SEM`, `Redirect`, `SET_OM`, `STRONG_ORDER`, `TensorLoad`,
   `TensorStore`, `WRITE` — **with zero engine-specific compute handlers**. `[HIGH/OBSERVED]`
3. **SP is the LEANEST of the five, and its set is the EXACT 5-way intersection.** Handler tally
   across the wave: DVE 53 \| POOL 41 \| ACT 26 \| PE 24 \| **SP 18**. SP's 18 names equal the
   intersection `SP ∩ ACT ∩ DVE ∩ PE ∩ POOL` byte-for-name (diff EMPTY), and SP is a **strict
   subset of every one** of the other four individually (SP-handlers-not-in-{ACT,DVE,PE,POOL} =
   `0/0/0/0`). SP has **zero** SP-only handlers. `[HIGH/OBSERVED]`
4. **12 image getters; 6 carry real bytes, 6 are zero-size boundary cursors** — DEBUG/PERF/TEST ×
   {IRAM, DRAM} = 6 flat firmware segments; DEBUG/PERF/TEST × {SRAM, EXTRAM} = 6 empty cursors (SP
   runs entirely out of IRAM+DRAM on CAYMAN). All 6 real carves are byte-identical (sha256) to the
   matching `libnrtucode.a` member `.rodata`. `[HIGH/OBSERVED]`
5. **SP ships NO PROF and has NO paired Q7.** Where ACT/DVE/PE/POOL each ship **14** NX getters
   (12 base + 2 PROF `{CAM, TABLE}`, byte-identical across the four), SP ships **12** = base only:
   `nm | rg -c CAYMAN_NX_SP_PROF` = **0**, `rg -c CAYMAN.*Q7_SP` = **0**. SP is the **only** CAYMAN NX
   engine without PROF, and (unlike POOL) carries no paired Q7 compute core or EXTISA ELF. It is a
   pure NX-only sequencer. `[HIGH/OBSERVED]`

> **CORRECTION — "SP hosts the barrier/semaphore handlers" is the wrong framing.** SP hosts **no
> SP-exclusive** sync/barrier/collective handler. The sync primitives — `Event_Semaphore` (0xa0),
> `POLL_SEM` (0xb3), `NOTIFY` (0xa6), `Event_Semaphore Rng Clr` (0xb0) — are part of the **shared
> 18-handler core present on all five engines** (each carries exactly one of each). What
> distinguishes SP is the *negative*: it is the engine whose **entire** handler set is this
> sync/control core and **nothing else**. The collective pseudo-ops (`CORE_BARRIER 0xd8`) are
> pre-lowered by the compiler into these same shared `EVENT_SEMAPHORE`/`POLL_SEM` HW ops — there is
> **no** dedicated `0xd8` handler on SP or any engine (§7). `[HIGH/OBSERVED]`

---

## 2. The 12 image getters (instruction-exact)

Each getter is the 4-instruction `(img-ptr, size)` stub
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`) disassembled this session
from `.text 0x9b3220..0x9b3520` (PERF/TEST) and `0x9b3720..0x9b37a0` (DEBUG). `CLS=NX, ENG=SP`. All
12 `.data` addresses (use plain `nm`, not `nm -D` — they are local `t` symbols) and sizes agree with
the catalog ([image-catalog-index.md](./image-catalog-index.md), CAYMAN NX_SP rows). `[HIGH/OBSERVED]`

| VARIANT | REGION | ACCESSOR (.text VA) | IMG-PTR (.rodata VA = file off) | SIZE | STATUS |
|---|---|---|---|---:|---|
| PERF | IRAM | `0x9b3220` | `0x0ba8a0` | `0x182c0` | REAL (SEQ code) |
| PERF | DRAM | `0x9b3240` | `0x0d2b60` | `0x02d40` | REAL (SEQ data, no `S:`) |
| PERF | SRAM | `0x9b3260` | `0x0d58a0` (=ACT_TEST_IRAM cursor) | `0` | EMPTY (boundary) |
| PERF | EXTRAM | `0x9b3280` | `0x0d58a0` (=ACT_TEST_IRAM cursor) | `0` | EMPTY (boundary) |
| TEST | IRAM | `0x9b34a0` | `0x136640` | `0x16ba0` | REAL (SEQ code) |
| TEST | DRAM | `0x9b34c0` | `0x14d1e0` | `0x03040` | REAL (SEQ data) |
| TEST | SRAM | `0x9b34e0` | `0x150220` (=ACT_DEBUG_IRAM cursor) | `0` | EMPTY (boundary) |
| TEST | EXTRAM | `0x9b3500` | `0x150220` (=ACT_DEBUG_IRAM cursor) | `0` | EMPTY (boundary) |
| DEBUG | IRAM | `0x9b3720` | `0x1d4b60` | `0x199a0` | REAL (SEQ code) |
| DEBUG | DRAM | `0x9b3740` | `0x1ee500` | `0x06360` | REAL (SEQ data + `S:` log) |
| DEBUG | SRAM | `0x9b3760` | `0x1f4860` (=Q7_POOL_PERF_IRAM cursor) | `0` | EMPTY (boundary) |
| DEBUG | EXTRAM | `0x9b3780` | `0x1f4860` (=Q7_POOL_PERF_IRAM cursor) | `0` | EMPTY (boundary) |

The six zero-size SRAM/EXTRAM getters all execute `movq $0x0,(%rsi)` and return their pointer at the
**contiguous-layout cursor** = the start of the *next* blob in the `.rodata` layout. `objdump`
therefore aliases the SP DEBUG SRAM/EXTRAM symbols to `CAYMAN_Q7_POOL_PERF_IRAM_get.data` (`0x1f4860`)
and the PERF/TEST SRAM/EXTRAM symbols to the next ACT blob — confirming `SP` is followed in the layout
by the POOL Q7 image. **SP uses no SRAM/EXTRAM on CAYMAN.** `[HIGH/OBSERVED]`

> **NOTE — why 12, not 14.** The four sequencer engines ACT/DVE/PE/POOL each ship **14** NX getters
> = 12 base + 2 PROF `{CAM, TABLE}`. SP ships **12** = base only. It has no HW-decode profiling
> CAM/table (`nm | rg -c CAYMAN_NX_SP_PROF` = 0 this session). SP is the only CAYMAN NX engine
> without PROF. `[HIGH/OBSERVED]`

### 2.1 Carve provenance + byte-identity

Carve rule (identity map): `blob = so[IMG-PTR : IMG-PTR+SIZE]` via `dd bs=1`. The 6 real carves and
their sha256 (reproduced this session; spot-reconciled **3/3 identical** to the `libnrtucode.a`
member `.rodata` via `ar p` + `objcopy -O binary --only-section=.rodata` + `cmp`): `[HIGH/OBSERVED]`

| IMAGE | FILE-OFF | SIZE | sha256 (full) |
|---|---|---:|---|
| `SP_PERF_IRAM` | `0xba8a0` | `0x182c0` | `5a6f6eaa7f6654a089b199c4c0ef2c48d6c7c79f28dc1c405161314b18ac1858` |
| `SP_PERF_DRAM` | `0xd2b60` | `0x2d40` | `9fe5e19db3d5e865052c5e2886ecd5a3c8c898531f0beb6bd61df42db0f67d04` |
| `SP_TEST_IRAM` | `0x136640` | `0x16ba0` | `b24ef299778ce0655545f6786b21997e6ac14e3de8292f499a8e9096c601b949` |
| `SP_TEST_DRAM` | `0x14d1e0` | `0x3040` | `deed216d810cf32fb094b38f54c3862ed808f5f1f89c92d56157f28960a7bf6b` |
| `SP_DEBUG_IRAM` | `0x1d4b60` | `0x199a0` | `6c3a6f79373cbe69038f794f6154e08bed312cb7ab8dd5ef1973dd5adaf9962d` |
| `SP_DEBUG_DRAM` | `0x1ee500` | `0x6360` | `5340ad8c6aa6a38868f76caf4d6dfbe6fd544b004bdccdbcf4a99ea35f778b76` |

The **SP PERF_IRAM `5a6f6eaa`** is the engine-distinguishing fingerprint in the cross-engine matrix
(§9); it reproduces the value the [PE](./cayman-pe.md)/[POOL](./cayman-pool.md) pages cited for SP
exactly. The archive ships the SP `img_*_contents.c.o` members (`PERF`/`TEST`/`DEBUG` × `{IRAM,DRAM}`)
and **no** `hwdecode_*_PROF_*` member; the `internal.so` getter blob equals the `.a` member `.rodata`
for the 3 spot-checked (`SP_PERF_IRAM`, `SP_DEBUG_IRAM`, `SP_DEBUG_DRAM`), byte-for-byte.
`[HIGH/OBSERVED]`

---

## 3. Flat-image geometry + boot

None of the 6 carves is an ELF (head ≠ `\x7fELF`) — they are **flat device-memory segments**, the
device-side `.rodata` payload of the `img_*` members (contrast POOL's Q7 EXTISA blobs, which *are*
`EM_XTENSA` ELFs; SP has none). Heads read this session: `[HIGH/OBSERVED]`

```text
IRAM (all 3 variants byte-identical):  06 76 00 00 00 00  86 77 00 00 00 00   ; j 0x1dc / j 0x1e8
DRAM (all 3 variants):                 34 cb 99 60                            ; header word 0x6099cb34
```

**Reset vector** (byte-identical across all 3 SP IRAM variants AND across all 5 engines):
`[HIGH/OBSERVED]`

```text
0x000:  06 76 00   j 0x1dc        ; primary reset vector -> boot path
0x006:  86 77 00   j 0x1e8        ; secondary vector -> halt trap
0x1dc:  04 00 00   const16 a0,0
0x1df:  04 90 00   const16 a0,144 ; (= 0x90)
        ...        jx a0          ; jump to C enter_run @ 0x90
0x1e8:  00 52 00   halt 0         ; 2nd vector = HALT trap
```

The boot `jx a0` lands on `enter_run @ 0x90`; the trampoline at `0x1dc` decodes **exactly** as
POOL's NX core. The same flat binary can be loaded on any engine slot: the DRAM carries
`S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u`, so the SP
image's `engine_idx` (= 4) is **derived at boot** from `engine_base_addr` vs `tpb_base_addr` — the
binary is **not** hard-wired to SP (§4c). `[HIGH/OBSERVED — string; runtime-compute INFERRED-HIGH
from the string + the shared reset/boot across all 5 engines]`

Disassembled with the shipped `ncore2gp` objdump (exit 0, empty stderr), `SP_DEBUG_IRAM` decodes a
**full Q7/NX windowed-ABI code body** — not a stub. Census this session: `[HIGH/OBSERVED]`

| metric | SP DEBUG IRAM |
|---|---:|
| `entry` (window-frame open) | 515 |
| `retw` (window return) | 728 |
| `call8` | 1550 |
| `callx8` | 71 |
| `const16` | 2044 |
| `jx` | 53 |

So SP carries the full windowed-ABI control spine + the generic SEQ fetch/DMA/cache infrastructure
— it is a genuine, separately-compiled `cayman/seq/` sequencer, not a thin trampoline. (The
FLIX-vector datapath is partly bundle-interleaved by the linear sweep — the documented SX-FW-00
limitation — but the control spine decodes cleanly.) `[HIGH/OBSERVED]`

---

## 4. The SP-vs-TOP_SP resolution

The brief's central question: is the IMG "SP" engine the per-NeuronCore **SP** (`engine_idx 4`), or
the standalone **TOP_SP** SoC-level sync block? The shipped ISA enum settles it decisively: **they
are two distinct enumerated engines, and the carved image is the per-core `TPB_SP`.**

### 4a. The ISA enum is authoritative — `TPB_SP(4) != TOP_SP(5)` `[HIGH/OBSERVED]`

`aws_neuron_isa_tpb_common.h:139-146` (read verbatim this session):

```c
typedef enum NEURON_ISA_TPB_NEURON_ENGINE {
    NEURON_ISA_TPB_NEURON_ENGINE_PE     = 0,
    NEURON_ISA_TPB_NEURON_ENGINE_ACT    = 1,
    NEURON_ISA_TPB_NEURON_ENGINE_POOL   = 2,
    NEURON_ISA_TPB_NEURON_ENGINE_DVE    = 3,
    NEURON_ISA_TPB_NEURON_ENGINE_TPB_SP = 4,   // <== the per-NeuronCore SP engine  (this image)
    NEURON_ISA_TPB_NEURON_ENGINE_TOP_SP = 5,   // <== the standalone top-level SP block
};
```

There are **six** engines, and `TPB_SP` and `TOP_SP` are **separate enumerators**. The image-catalog
`ENG = SP` row is the `TPB_SP = engine_idx 4` — the 5th *execution* engine inside one
TPB/NeuronCore (PE/ACT/POOL/DVE **+ SP**). This is exactly the project's
`PE=0 / ACT=1 / POOL=2 / DVE=3 / TPB_SP=4` model
([per-engine-depth.md §1](../uarch/per-engine-depth.md)). The standalone **TOP_SP** (engine 5) is the
separate SoC-level sequencer that hosts the **global** EVT_SEM array + the SoC time-sync tick and
walks the host-built collective program (the
[collective end-to-end](../orientation/collective-end-to-end.md) `engine_idx 5` target). They are
**not** the same block; the naming collision (both abbreviate to "SP") is the entire source of the
question. `[HIGH/OBSERVED — the enum is the authoritative shipped artifact.]`

### 4b. Both have an NX core — but this library ships exactly ONE SP image `[HIGH on the one-image fact]`

Both `TPB_SP` and `TOP_SP` embed an Xtensa NX core. The `gpsimd-customop` firmware library surfaces
**exactly one** NX SP image family, `CAYMAN_NX_SP` (`nm | rg -c CAYMAN.*TOP_SP` = **0** — there is
no `TOP_SP`-named getter). The carved `CAYMAN_NX_SP` blobs comfortably fit the `TPB_SP` NX core
geometry (`NX_IRAM 0x20000` = 128 KiB holds `SP_DEBUG_IRAM 0x199a0` ≈ 105 KiB / `SP_PERF_IRAM
0x182c0` ≈ 99 KiB). The binding "**this image runs on the `TPB_SP` (engine 4) NX core**" is
INFERRED-HIGH from the getter name + the engine enum + the geometry fit. **How (or whether) the
standalone `TOP_SP` is provisioned is out of this library's scope** — that is NCFW/runtime territory;
the data here neither confirms nor denies that the same `cayman/seq/` build serves both slots, and
this page does **not** fabricate an answer. `[HIGH on the one-image fact + the zero `TOP_SP` getter;
LOW / NOT-CLAIMED on the TOP_SP provisioning path.]`

### 4c. `engine_idx` is runtime-computed, not baked `[HIGH/OBSERVED]`

The SP DEBUG DRAM carries the **same** boot-identity string as every NX engine —
`S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` — a runtime
format string, not a constant. The engine identity (`engine_idx`, `is_tpb`, `is_die_0`) is **derived
at boot** from the `engine_base_addr` the core reads against `tpb_base_addr`. So the SP image
self-identifies as `engine_idx=4` **because it is placed at the `TPB_SP` IRAM base**, not because of
its bytes. This is the architectural reason all five NX engines can share the identical reset vector
and the identical boot trampoline: **one boot path, late-bound identity.** `[HIGH/OBSERVED — the
string + the shared boot; the late-binding mechanism INFERRED-HIGH from the runtime-formatted
identity string.]`

### 4d. Conclusion

The carved image is unambiguously the **per-NeuronCore `TPB_SP` (engine 4)** — the 5th execution
engine inside the TPB — **distinct** from the standalone `TOP_SP` (engine 5). The two blocks share an
*architecture* (each an NX-core sequencer with a semaphore/notification surface) and a *role family*
(the TPB-side sequencer-processor IP), but they are two enumerated, separately-addressed engines.
The `POLL_SEM` op the per-core SP carries is what lets it *participate* in global sync orchestrated
by the standalone `TOP_SP`; it is not evidence that the image *is* `TOP_SP`. `[HIGH/OBSERVED]`

---

## 5. The SP handler set = the EXACT 5-way intersection

Method (identical to the sibling pages): extract every single-token `S: <OpName>` from each engine's
CAYMAN DEBUG DRAM (regex `^S: [A-Za-z][\w/-]*$`), `sort -u`, set-diff. The SP DEBUG DRAM yields
exactly **18** distinct handler names this session. Per-engine counts (reproduced independently from
each engine's DEBUG DRAM carve this session): DVE 53 \| POOL 41 \| ACT 26 \| PE 24 \| **SP 18** — SP
is the **leanest** of the five. `[HIGH/OBSERVED]`

### 5a. SP's 18 handlers (the complete roster)

Read directly from the SP DEBUG DRAM's own `S:` logs (the 142 `S:` strings in the DEBUG build):

```text
AluOp  BRANCH  BranchPrefetchHint  Event_Semaphore  EXT_BREAK  Halt  INS_BREAK  INS_FL
MOVE  NOP  NOTIFY  POLL_SEM  Redirect  SET_OM  STRONG_ORDER  TensorLoad  TensorStore  WRITE
```

By function: `[HIGH for the names; the grouping is the analyst's, MED]`

| group | handlers |
|---|---|
| control flow / fetch | `BRANCH`, `BranchPrefetchHint`, `Redirect`, `Halt` |
| debug / break | `EXT_BREAK`, `INS_BREAK`, `INS_FL` |
| data move | `MOVE`, `TensorLoad`, `TensorStore`, `WRITE` |
| scalar ALU | `AluOp` |
| ordering | `SET_OM` (ordering mode), `STRONG_ORDER` |
| **sync / EVT_SEM** | `Event_Semaphore` (0xa0), `POLL_SEM` (0xb3), `NOTIFY` (0xa6) |
| no-op | `NOP` |

### 5b. SP == the intersection; a strict subset of every engine `[HIGH/OBSERVED]`

`comm -12` of all five engines' handler sets (`SP ∩ ACT ∩ DVE ∩ PE ∩ POOL`) yields **exactly** these
18 names — SP's set is byte-for-name **identical** to the 5-way intersection (diff EMPTY).
Pairwise-verified: SP-handlers not-in-ACT = `0`, not-in-DVE = `0`, not-in-PE = `0`, not-in-POOL = `0`.
**SP is a strict subset of every other engine; it has zero engine-exclusive handlers.** SP is the
shared control core that ACT/DVE/PE/POOL are each built on top of by *adding* their compute handlers.

| engine | total | = shared 18 core | + engine-specific (examples) |
|---|---:|---|---|
| **SP** | **18** | the 18 (= the whole set) | **none** |
| PE | 24 | the 18 | `EngineNop` + 5 matmul (`Ldweights`/`Matmul`/`MatmulSparse`/`LdTags`/`PeRegWrite`) |
| ACT | 26 | the 18 | activation set (`Activate`/`Cast`/`Copy`/`ActivationTableLoad`/…) |
| POOL | 41 | the 18 | pool/reduce/gather/sort/dequant + `ExtendedInst` (the Q7 bridge) + RNG |
| DVE | 53 | the 18 | the batch-norm/predicated/scan/dropout data-vector set |

So the engines are the same `cayman/seq/` firmware with **disjoint** compute subsets layered on the
shared 18-handler control core; SP's extension is the **empty** one — the precise lower bound. "Same
SEQ engine, different handler subset" is **CONFIRMED for SP, in its limiting/degenerate form.**
`[HIGH/OBSERVED]`

> **QUIRK — `EngineNop` is the cleanest discriminator between "control core" and "lean compute
> engine".** PE — the next-leanest at 24 — carries `EngineNop` (a barrier/serialization helper shared
> with POOL/DVE), but **SP does not.** SP's 18 names contain `NOP` (the scalar no-op) but **not**
> `EngineNop`. The 18-name intersection is therefore strictly smaller than "PE minus its 5 matmul
> ops": SP is the *only* engine with no member outside the all-five intersection. `[HIGH/OBSERVED]`

---

## 6. The SP DRAM — SEQ dispatch table, no PROF, no baked tables

The SP DRAM is the generic SEQ data segment: the `.globstruct` header + dispatcher-state init block
+ the SEQ dispatch table + the `S:` string pool. No weight/LUT/kernel table is baked in — SP is a
pure control sequencer.

**`.globstruct` header + dispatcher-state init (byte-identical to POOL).** DRAM head: header word
`0x6099cb34` @ `0x0`; the dispatcher-state init block — `4 × 0x00001000` @ `0x18` and `4 × 0x00ffffff`
@ `0x28` — is **byte-identical between SP and POOL** (re-read this session). The shared init block is
the common dispatcher-state initialization every flat NX DRAM carries. `[HIGH/OBSERVED]`

**The SEQ dispatch table @ DRAM `0x814` (file `0x814`).** SP carries the same indexed-jump
trampoline-pointer table POOL/PE/ACT use at the same base. Read this session (DEBUG, 4-byte-LE
trampoline pointers): `[HIGH location / MED per-opcode row decode]`

```text
0x814:  90 2a 00 00   c9 2a 00 00   c9 2a 00 00   c9 2a 00 00    ; 0x2a90, then 0x2ac9 (Bad-Opcode band)
0x824:  99 2a 00 00   88 2a 00 00   70 2a 00 00   78 2a 00 00    ; 0x2a99 0x2a88 0x2a70 0x2a78
```

Trampoline targets cluster in `0x2a70..0x4400` (the handler-trampoline region) with `0x2ac9` as the
repeated **Bad-Opcode** slot and `0x2fe4`-band defaults. The table is followed by the string pool at
`0xa98`, with the dispatch log `S: Dispatch opcode=0x%x` @ `0xaa8`. **SP's table simply binds fewer
real handlers** (most slots → default), matching its 18-handler set. The `ErrorHandler` arms
(`Bad Opcode(0x%x)` @ `0x2f81`, `Illegal Instruction(0x%x)`, `FP Error(%d)`, `Int Div Zero Error`)
are all present. In **PERF** the table relocates to file `0x218` (head `0x62d0 0x62d6 0x62e2 0x62eb`,
then a `0x98c3` repeating default band) — the same DEBUG-segmented-vs-PERF-clean split the sibling
engines show. `[HIGH/OBSERVED for the table location + default/trampoline bands; the exhaustive
per-opcode→handler decode is the FLIX-desync-limited frontier (SX-FW-00), not fully decoded here.]`

> **NOTE — no resident table in the SP firmware.** SP carries **no** weight/coefficient/kernel
> table. Its only "tables" are the dispatch table and the shared dispatcher-state block. (Contrast
> POOL, which additionally hosts a `kernel_info_table` in its Q7 EXTISA ELFs; SP has no Q7 and no
> EXTISA.) `[HIGH/OBSERVED]`

The SP DRAM otherwise carries the generic SEQ runtime infra — `S: IRAM cache init`,
`S: start_fill_siram`, `S: DramRingDMA::allocate`, **`sunda_fast_fetch`**, `S: Sunda seq Loop`, the
`DGE` (descriptor-generation-engine) family (13 `DGE` strings, incl. `DGE: Select backend Pool/RTL`),
and `S: dge_shape[].step`/`.num` — confirming the same cache/PC-bounds/DMA/shape machinery as the
other NX engines. `[HIGH/OBSERVED]`

---

## 7. The SP ↔ EVT_SEM / barrier interaction

SP's sync handlers map to the shipped ISA opcodes (read this session from the OPCODE enum in
`aws_neuron_isa_tpb_common.h`): `[HIGH/OBSERVED]`

| HW op | ISA opcode | enum | `S:` roster handler |
|---|---|---|---|
| Event-semaphore arrive/wait | `0xa0` | `…_OPCODE_EVENT_SEMAPHORE` | **`Event_Semaphore`** |
| Notify | `0xa6` | `…_OPCODE_NOTIFY` | **`NOTIFY`** |
| Event-semaphore range-clear | `0xb0` | `…_OPCODE_EVENT_SEMAPHORE_RANGE_CLEAR` | *(folded into `Event_Semaphore`)* |
| Poll / min-fold semaphore | `0xb3` | `…_OPCODE_POLL_SEM` | **`POLL_SEM`** |

> **GOTCHA — four EVT_SEM HW opcodes, three roster handlers.** The OPCODE enum has **four** EVT_SEM
> ops (`0xa0/0xa6/0xb0/0xb3`), but the SP `S:`-handler roster has only **three** sync names
> (`Event_Semaphore`, `NOTIFY`, `POLL_SEM`). The `0xb0` `EVENT_SEMAPHORE_RANGE_CLEAR` has **no
> separate roster entry on any engine** — it is covered by the single `Event_Semaphore` handler
> (range-clear is a mode of that handler, not its own dispatch arm). Verified this session across all
> five DEBUG DRAMs. `[HIGH/OBSERVED]`

These are the EVT_SEM HW-semaphore operations. They are **shared by all five engines** (each carries
exactly one of each `S:` name — verified) — **not** SP-exclusive. SP is simply the engine whose
*entire* handler set is this sync/control core.

**The barrier path (CARRIED from the collective + per-engine-depth pages).** The collective/barrier
pseudo-ops — `CORE_BARRIER 0xd8`, `SYNC_BARRIER 0xd5`, `DMA_BARRIER 0xc3` (all present in the ISA
OPCODE enum as `PSEUDO_*` opcodes) — are **pre-lowered by the compiler / NRT** into concrete
`EVENT_SEMAPHORE(0xa0)` arrive/wait + `POLL_SEM(0xb3)` HW instructions *before* the stream reaches
the engine sequencer. There is **no dedicated `0xd8` handler** on SP or any engine, and **zero**
`Barrier` `S:`-strings appear in any of the five DEBUG DRAMs — which is exactly why SP's 18-handler
set has `Event_Semaphore`/`POLL_SEM` but no "Barrier" handler. `[HIGH/OBSERVED — the absence of a
`0xd8` handler / any `Barrier` string on all 5 engines; the pre-lowering is the
[collective end-to-end](../orientation/collective-end-to-end.md) finding.]`

The HW substrate these ops operate on is the **EVT_SEM aperture**: 256 hardware EVENTS (1-bit
set/clr) + 256 hardware SEMAPHORES (32-bit counters), with distinct APB sub-windows aliasing the
array per operation (read/set/inc/dec). A `CORE_BARRIER` rendezvous keys on a semaphore handle; SP's
`Event_Semaphore`/`POLL_SEM` ops are the **arrive/wait** against that semaphore. The EVT_SEM geometry
is HIGH ([per-engine-depth.md §7](../uarch/per-engine-depth.md)); the exact SP-op→APB-window binding
is **MED** — the addressing lives in the *lowered instruction operands*, not in the firmware image.
`[HIGH EVT_SEM geometry / MED op-to-window binding]`

> **NOTE — "the TOP_SP poll accelerator" vs the per-core `POLL_SEM`.** The collective docs call
> `POLL_SEM` "the `TOP_SP` Min-fold poll accelerator" and label `TOP_SP = engine 5`. `POLL_SEM`
> (`0xb3`) is nonetheless present on the **per-core `TPB_SP` image (engine 4)** AND on all five
> engines — it is a *generic* SEQ sync op, not a `TOP_SP`-only accelerator. The "`TOP_SP` poll
> accelerator" phrasing reflects that the **standalone `TOP_SP` block (engine 5)** is the
> global-sync host; the per-core engines all carry the op to *participate*. `[HIGH that `POLL_SEM`
> is on all 5 per-core engines; the global-sync role of the standalone `TOP_SP` is the collective
> subsystem's territory.]`

### 7.1 Annotated SP dispatch + barrier/event-wait path

The SP sequencer reproduced as C pseudocode. Symbols are byte-pinned (CAYMAN opcodes / DRAM offsets
observed this session; the EVT_SEM aperture + barrier pre-lowering CARRIED from the cited pages):

```c
// ---------------------------------------------------------------------------
// CAYMAN SP (TPB_SP, engine_idx 4) — boot -> main fetch/dispatch -> handler.
// SP is PURE CONTROL/SYNC: its 18 handlers ARE the shared SEQ control core;
// it issues no compute. engine_idx is computed at boot, not baked.
//   Sync opcodes (shared by ALL 5 engines, observed):
//     EVENT_SEMAPHORE=0xa0  NOTIFY=0xa6  EVENT_SEM_RANGE_CLEAR=0xb0  POLL_SEM=0xb3
//   NO dedicated barrier handler: CORE_BARRIER(0xd8)/SYNC_BARRIER(0xd5)/
//   DMA_BARRIER(0xc3) are pre-lowered by the compiler into 0xa0/0xb3 ops.
// ---------------------------------------------------------------------------

void sp_enter_run(void) {                          // boot jx a0 lands @ IRAM 0x90
    sp_compute_engine_idx();                       // engine_base_addr vs tpb_base_addr -> 4 (TPB_SP)
                                                   // "S: engine_base_addr=%llx ... engine_idx=%u"
    bool hw_decode = nx_mode_select();             // dual-mode SEQ feature (not SP-specific)

    for (;;) {                                      // "S: Sunda seq Loop" main loop
        instr_t *ins = seq_fetch(hw_decode);       // I$-backed fetch (cache + PC-bounds + DGE/DMA infra)
        uint8_t  op  = ins->opcode;
        log("S: Dispatch opcode=0x%x", op);        // DRAM 0xaa8 (DEBUG only)

        // DEBUG: 4-byte-LE trampoline table @ DRAM 0x814 (most slots -> Bad-Opcode 0x2ac9).
        // PERF:  relocated addx4 table @ DRAM 0x218. SP binds ONLY the 18 control-core opcodes.
        switch (op) {
        // ---- the SYNC / EVT_SEM ops (shared by all 5 engines; SP's whole reason to exist) -----
        case 0xa0: h_event_semaphore(ins);  break; // "S: Event_Semaphore"  arrive/set/inc/dec; also
                                                   //   covers 0xb0 range-clear (a mode, not its own arm)
        case 0xb3: h_poll_sem(ins);         break; // "S: POLL_SEM"         wait / min-fold (<=16 reads)
        case 0xa6: h_notify(ins);           break; // "S: NOTIFY"           "S: sending notification"
        // ---- the rest of the shared 18-handler control core --------------------------------
        case 0x..: h_alu_op / h_branch / h_branch_prefetch_hint / h_redirect / h_move /
                   h_tensor_load / h_tensor_store / h_write / h_set_om / h_strong_order /
                   h_halt / h_nop / h_ext_break / h_ins_break / h_ins_fl;  break;
        // ---- there is NO compute case on SP, and NO 0xd8 barrier case on ANY engine --------
        default:   error_handler(op);              // "S: ErrorHandler : Bad Opcode(0x%x)" @0x2f81
        }
    }
}

// ---- the barrier / event-wait path the sync handlers drive ----
// A CORE_BARRIER(0xd8) the compiler emitted was ALREADY lowered into these two ops
// against a chosen semaphore handle in the 256-semaphore EVT_SEM array.
void h_event_semaphore(instr_t *ins) {             // 0xa0  the ARRIVE side
    uint32_t sema = ins->sema_handle;              // index into the 256-entry HW semaphore array
    // SET / INC / DEC / RANGE_CLEAR via the per-op APB sub-window (aliases of the array).
    evt_sem_apply(sema, ins->op_mode, ins->delta); // arrive: this core signals it reached the point
}

void h_poll_sem(instr_t *ins) {                    // 0xb3  the WAIT side (min-fold poll accelerator)
    uint32_t sema = ins->sema_handle;
    // poll up to n_read (<=16) semaphores, min-fold, block until the target count is met;
    // this is the rendezvous wait for the lowered CORE_BARRIER.
    while (evt_sem_min(sema, ins->n_read) < ins->target)
        ;                                          // HW-assisted poll; no busy CPU spin in the seq
    // proceed: all participating engines/cores have arrived.
}

void h_notify(instr_t *ins) {                      // 0xa6  notification emit
    // "S: sending notification" / "S: sending interrupt" — the per-core SEQ notify path.
    // (The standalone TOP_SP's 4-way SW-notification fabric is a SEPARATE block-level path.)
    seq_send_notification(ins->notify_target, ins->payload);
}
```

> **GOTCHA — the engine-numbering subtlety in the notify/interrupt path.** SP's
> `S: sending notification` / `S: sending interrupt` strings are the **per-core** `TPB_SP` (engine 4)
> notification path. The standalone `TOP_SP` (engine 5) block has its **own** 4-way SW-notification
> fabric (`sp_nx_nt` / `sp_explicit_nt` / `events_semaphores_nt` / `errors_nt`) — a *different*,
> block-level fabric. Do not conflate the per-core SP notify with the SoC `TOP_SP` fabric.
> `[HIGH that SP carries notify/interrupt dispatch; the `TOP_SP` fabric is a distinct block.]`

---

## 8. DEBUG vs PERF(release) vs TEST

| variant | IRAM size | DRAM size | `S:` strings | total DRAM strings |
|---|---:|---:|---:|---:|
| **DEBUG** | `0x199a0` | `0x6360` | **142** | 242 |
| **PERF** | `0x182c0` | `0x2d40` | 0 | 15 |
| **TEST** | `0x16ba0` | `0x3040` | 0 | 59 |

* **DEBUG** is the only build carrying the 142 `S:` runtime log strings — the RE substrate (every
  handler self-names, including the 18-handler roster). It is the largest variant.
* **PERF** (the production/release flavor) strips **all** `S:` logs: DRAM shrinks to `0x2d40`, only
  15 strings survive — assertion source-paths only (`…/exception_handler.hpp`, `…/decode/*.cpp`,
  `Assertion failure!`).
* **TEST** sits between: 0 `S:` logs but 59 strings — keeps function-name/file symbols for assert
  context.
* **The dispatch mechanism is invariant** across all three: same reset vector (`06 76 00 00`), same
  boot → `enter_run @0x90`, same SEQ table (DEBUG @ `0x814` / PERF @ `0x218`), same `ErrorHandler`/
  Bad-Opcode arm, same `.globstruct` magic. **A DEBUG→RELEASE(PERF) swap is a pure observability
  change**, not a functional/dispatch change. `[HIGH/OBSERVED]`

---

## 9. Cross-engine code-sharing

**Cross-engine PERF_IRAM matrix** (all 5 carved + hashed this session; all 5 distinct, all reproduce
the prior engine pages exactly): `[HIGH/OBSERVED]`

| engine | idx | PERF_IRAM sha256 | PERF_IRAM size | PROF |
|---|---:|---|---:|---|
| PE | 0 | `13ba3969…` | `0x159e0` | `8fd7e422` / `ce761f81` |
| ACT | 1 | `5ef2a351…` | `0x13dc0` | `8fd7e422` / `ce761f81` |
| POOL | 2 | `9049bf8c…` | `0x17280` | `8fd7e422` / `ce761f81` |
| DVE | 3 | `9fa066f4…` | `0x15c20` | `8fd7e422` / `ce761f81` |
| **SP** | **4** | **`5a6f6eaa…`** | **`0x182c0`** | **(no PROF)** |

* **CODE/DATA (IRAM/DRAM): SP shares NO bytes with PE/ACT/POOL/DVE.** Each engine's PERF_IRAM has a
  distinct sha256 — each is a separately-compiled `cayman/seq/` build with its own handler subset.
  The sharing is at the **source/structure** level (identical reset vector `06 76 00 00`, identical
  boot trampoline → `enter_run @0x90`, identical 18-handler control core, identical `.globstruct`
  magic `0x6099cb34` + init block, identical SEQ dispatch-table architecture, identical ErrorHandler
  arms, identical EVT_SEM op set) — **not** at the linked-byte level.
* **PROF: SP ships none.** The other four NX engines share byte-identical PROF_CAM (`8fd7e422`) /
  PROF_TABLE (`ce761f81`); SP has neither (`nm | rg -c CAYMAN_NX_SP_PROF` = 0). SP is the **only**
  CAYMAN NX engine without PROF. `[HIGH/OBSERVED]`
* **Q7: SP has none.** POOL is the only CAYMAN engine with a paired Q7 (and EXTISA ELFs). SP is a
  pure NX-only sequencer. `[HIGH/OBSERVED]`

> **QUIRK — SP's PERF_IRAM is the LARGEST of the five despite the FEWEST handlers.** SP_PERF_IRAM
> (`0x182c0` ≈ 99 KiB) is bigger than POOL's (`0x17280`), DVE's, PE's and ACT's PERF_IRAM — even
> though SP has the fewest handlers (18 vs POOL's 41). The reason: the build size is
> **infrastructure-dominated, not handler-count-dominated.** SP carries the full SEQ fetch / DMA /
> cache / sunda-fetch infrastructure (`DGE × 13`, `DramRingDMA`, `start_fill_siram`, IRAM cache,
> `sunda_fast_fetch`, the notification/interrupt dispatch) without any compute handlers to *amortize*
> that infra against — so the common spine is a larger fraction of a smaller total. Handler count is
> a poor proxy for image size. `[HIGH/OBSERVED]`

---

## 10. Engine-model classification — the 5-engine CAYMAN set is complete

SP is a **SEQ-style ASCII-opcode dispatch engine** (the NX-class sequencer model), the same
`cayman/seq/` firmware as PE/ACT/POOL/DVE, compiled with the **minimal/intersection** handler subset.
"Same SEQ engine, different handler subset" is **CONFIRMED for SP**, in its limiting form.

| property | SP (this image) | the other 4 NX engines |
|---|---|---|
| packaging | flat IRAM/DRAM segments | flat IRAM/DRAM |
| reset vector | `j 0x1dc` (`06 76 00 00`) | `j 0x1dc` (identical) |
| boot | `const16 a0,0x90 ; jx → enter_run @0x90` | identical |
| dispatch base | DRAM `0x814` (DEBUG) / `0x218` (PERF) | identical bases |
| `.globstruct` magic | `0x6099cb34` + shared init block | identical |
| miss policy | `ErrorHandler : Bad Opcode(0x%x)` | identical |
| source tree | `cayman/seq/src/…` | identical |
| distinct handlers | **18** (= the 5-way intersection) | 24 / 26 / 41 / 53 |
| compute subset | **none** | matmul / activation / pool / data-vector |
| PROF | **none** | byte-identical CAM+TABLE |
| paired Q7 | **none** | POOL only |

With SP carved + handler-diffed, the **five-engine CAYMAN set is complete**:

| ENGINE | idx | HANDLERS | ROLE | SPECIAL |
|---|---:|---:|---|---|
| [PE](./cayman-pe.md) | 0 | 24 | matmul / weights / tags | PE-only matmul set |
| [ACT](./cayman-act.md) | 1 | 26 | activation / cast / copy | ACT-only activation set |
| [POOL](./cayman-pool.md) | 2 | 41 | general compute (RICHEST) | dual-dispatch + Q7 + EXTISA |
| [DVE](./cayman-dve.md) | 3 | 53 | data/vector (most COUNT) | DVE-only bn/scan/dropout |
| **SP** (this page) | **4** | **18** | **SYNC / CONTROL (LEANEST)** | **== the 5-way intersection** |

All five: identical reset `06 76 00 00`, boot → `enter_run @0x90`, `.globstruct 0x6099cb34`, SEQ
dispatch table @ `0x814`, shared 18-handler control core. The four NX sequencers (PE/ACT/POOL/DVE)
share byte-identical PROF; SP ships none. POOL alone pairs a Q7. **SP alone is the pure control
core** — the differentiation is *entirely* the layered compute-handler subset on a common SEQ
chassis. `[HIGH/OBSERVED]`

---

## 11. Honesty ledger

**HIGH / OBSERVED (this session):**

- 12 `CAYMAN_NX_SP` getters indexed instruction-exact (6 real + 6 zero-size boundary cursors →
  next-blob ACT/Q7-POOL IRAM); 6 real carves byte-identical (sha256) to the `libnrtucode.a` member
  `.rodata` (3/3 spot-reconciled). SP PERF_IRAM = `5a6f6eaa`. **NO PROF** getter, **NO Q7_SP**,
  **NO TOP_SP** getter (`nm | rg -c` = 0/0/0).
- All carves FLAT (no ELF magic); reset vector `06 76 00 00` (`j 0x1dc`) identical across
  DEBUG/PERF/TEST and across all 5 engines; boot `const16 a0,0x90 ; jx → enter_run @0x90`; 2nd vector
  `j 0x1e8 → halt 0`. `SP_DEBUG_IRAM` census 515 entry / 728 retw / 1550 call8 / 71 callx8 /
  2044 const16 / 53 jx (native `ncore2gp` objdump, exit 0, empty stderr).
- DRAM head `0x6099cb34`; dispatcher-state init block (`4×0x1000` @ `0x18`, `4×0xffffff` @ `0x28`)
  byte-identical to POOL. SEQ dispatch table @ `0x814` (DEBUG, default-band `0x2ac9`/`0x2fe4`); PERF
  table @ `0x218`; `S: Dispatch opcode=0x%x` @ `0xaa8`; ErrorHandler arms @ `0x2f81`.
- **SP handler set = 18 = the EXACT 5-way intersection** (diff EMPTY); strict subset of every engine
  (pairwise not-in = 0/0/0/0). Counts reproduce the wave (DVE 53 \| POOL 41 \| ACT 26 \| PE 24 \|
  SP 18). `EngineNop` present on PE/POOL/DVE but **not** SP.
- The ISA enum `TPB_SP = 4` / `TOP_SP = 5` (`aws_neuron_isa_tpb_common.h:139-146`) — the
  authoritative SP-vs-TOP_SP disambiguation.
- EVT_SEM opcodes `EVENT_SEMAPHORE 0xa0` / `NOTIFY 0xa6` / `RANGE_CLEAR 0xb0` / `POLL_SEM 0xb3` read
  from the ISA OPCODE enum; the three roster handlers (`Event_Semaphore`/`NOTIFY`/`POLL_SEM`) present
  on SP AND all 5 engines; `0xb0` folded into `Event_Semaphore`. `CORE_BARRIER 0xd8` / `SYNC_BARRIER
  0xd5` exist as `PSEUDO_*` opcodes but have **no** engine handler (zero `Barrier` strings in any of
  the 5 DEBUG DRAMs).
- Cross-engine PERF_IRAM sha matrix (5 distinct, all reproduce prior pages). SP_PERF_IRAM is the
  largest despite the fewest handlers (infra-dominated). DEBUG 142 `S:` logs; PERF/TEST 0.
- SP-unique infra string `sunda_fast_fetch`; `DGE × 13`; `S: sending notification`/`interrupt`.

**MED / INFERRED:**

- "The `CAYMAN_NX_SP` image runs on the `TPB_SP` (engine 4) NX core" — INFERRED-HIGH from the getter
  name + ISA enum + the `NX_IRAM 0x20000` geometry fit; the image carries no self-baked `engine_idx`
  (it is runtime-computed from base addr).
- The `engine_idx` late-binding mechanism (one boot path, identity derived from `engine_base_addr`)
  — INFERRED-HIGH from the runtime-formatted identity string + the shared reset/boot across all 5.
- The SP-op → specific EVT_SEM APB-window binding — MED (the addressing is in the lowered instruction
  operands, not the firmware image; the EVT_SEM aperture geometry itself is HIGH/CARRIED).
- The exhaustive per-opcode SEQ dispatch-table row decode (FLIX/literal desync, SX-FW-00).

**LOW / NOT CLAIMED:**

- Whether the standalone `TOP_SP` (engine 5) runs the same `cayman/seq/` SP build or a different
  firmware path: this library ships only **one** NX SP image (named `SP = TPB_SP`); how/whether
  `TOP_SP` is provisioned is out of scope (NCFW/runtime territory). **Not fabricated.**
- Which silicon part / runtime selects DEBUG vs PERF vs TEST.
- SUNDA/MARIANA/MAVERICK SP variants (out of CAYMAN scope; see [mariana-sp.md](./mariana-sp.md) and
  the forward Part-6 pages).

---

## 12. Cross-references

- [Image Catalog Index](./image-catalog-index.md) — the full getter map (CAYMAN NX_SP rows).
- [Per-Engine Firmware Depth (PE / SP / TOP_SP / ACT)](../uarch/per-engine-depth.md) — §7 is the
  companion deep-dive on the `TPB_SP`/`TOP_SP` sync ops, the no-dedicated-barrier-handler result, the
  EVT_SEM aperture, and the per-gen SP evolution.
- [A Collective, End to End](../orientation/collective-end-to-end.md) — the `CORE_BARRIER 0xd8`
  pre-lowering and the `TOP_SP` (engine 5) collective-program execution this page references.
- [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md) — the shared SEQ table / trampoline /
  thunk dispatch mechanism SP runs (the 178-entry POOL table is the worked example; SP binds fewer).
- The sibling CAYMAN engine images: [× PE](./cayman-pe.md), [× ACT](./cayman-act.md),
  [× POOL](./cayman-pool.md), [× DVE](./cayman-dve.md).
- [MARIANA × SP image](./mariana-sp.md) — the v4 SP diff baseline against this page.
- [PROF_CAM / PROF_TABLE formats](./prof-cam-table-formats.md) — the generic HW-decode profiling
  CAM/table shared by the 4 NX engines that have PROF; SP has none.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
