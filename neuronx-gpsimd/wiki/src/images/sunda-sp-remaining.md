# SUNDA × SP + the remaining NX engines (ACT / DVE / PE, RELEASE variant)

The **SUNDA** firmware set is the **NC-v2** floor — `arch_id 5 / coretype 6`, the **oldest** generation in
the image matrix, and the only one shipped **RELEASE-only** (no DEBUG/PERF/TEST flavor surfaced as a
getter). This page carves the **four remaining SUNDA NX engines** — `SP` (`TPB_SP`, `engine_idx 4`),
`ACT` (1), `DVE` (3), `PE` (0) — byte-exact from `libnrtucode_internal.so`, reconciles every carve against
the matching `libnrtucode.a` RELEASE member, and **diffs each engine against its committed CAYMAN (v3)
baseline**: [× SP](./cayman-sp.md), [× ACT](./cayman-act.md), [× DVE](./cayman-dve.md),
[× PE](./cayman-pe.md). Together with the [SUNDA × POOL image](./sunda-pool.md) it **completes the SUNDA
engine set** and establishes the **v2 baseline floor across all five NX engines + the paired POOL Q7**.

The decisive result is a **two-axis verdict**:

1. **SUNDA is NOT engine-count-reduced.** It ships the **same** five NX engines (PE/ACT/POOL/DVE/SP) **+ the
   paired POOL Q7** as CAYMAN — `nm` lists exactly **24** `*_get` accessors across six engine-classes. The
   "reduction" is strictly **per-engine** (single flavor, no PROF, no DKL, no in-library EXTISA, half the
   image size, total observability strip) — never a missing engine.
2. **The v2 floor is a *strict subset* of everything CAYMAN adds.** The CAYMAN extension layer —
   `PeManageSeed`/`LdweightsMX`/`MatmulMX` (MARIANA-first), `ConvLutLoad 0xe4` (CAYMAN-first), RNG/dequant/MX,
   the on-chip SB2SB collective, the `ExtendedInst 0xf0` bridge, sequence-bounds, **and the PROF HW-decode
   profiler** — is **absent** at the SUNDA floor on every engine. The SUNDA activation PWP header even
   **diverges** from cayman+ (sha `dbdca26b…` vs `8f6f5f49…`).

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. SUNDA is a **v2 byte-grounded** target — **not** the v5 inference wall — so the
carved getters, carve sha256, reset bytes, dispatch signature, `.a` member split, and the enum/header reads
below are all **OBSERVED**. Where the v2 **observability floor** (zero handler self-naming, even in DEBUG)
makes a per-handler *name* claim unre-derivable, this page anchors to the shared `sunda/seq/` **module spine**,
the **absent compute strings/opcodes**, and the byte-distinct IRAM datapaths — and says so explicitly.

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not stripped**,
> 10,276,288 B — re-hashed this session, MATCH). First R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`),
> so each `<NAME>.data` accessor address is **simultaneously** the `.rodata` VA and the file offset of its
> blob — carve = `so[ptr : ptr+size]`. **IRAM file-offset == device IRAM VA** (reset vector at byte 0);
> **DRAM string-file-offset == device DRAM VA − `0x80000`**. The byte-identity reconciliation uses
> `…/custom_op/c10/lib/libnrtucode.a` (**435** members; **48** SUNDA). Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo, Xtensa24,
> RI-2022.9, `TargetHWVersion=NX1.1.4`, FLIX/VLIW 32B). The clean C ISA headers
> (`neuron_<gen>_arch_isa/tpb/…`, shipped redistributable) are cited for the engine enum, opcode values and
> the activation/PWP struct layouts. All carve sha256, reset vectors, the boot trampoline, the dispatch
> signature and the `.a` split were reproduced this session (objdump exit 0, empty stderr). `[HIGH/OBSERVED]`

---

## 1. The headline

1. **SUNDA ships ALL five NX engines + POOL Q7 — the full CAYMAN engine set.** `nm libnrtucode_internal.so`
   lists exactly **24** `SUNDA_*_get` accessors = **6** engine-classes (`NX_ACT`, `NX_DVE`, `NX_PE`,
   `NX_POOL`, `NX_SP`, `Q7_POOL`) × **4** regions (`IRAM`/`DRAM`/`SRAM`/`EXTRAM`), **RELEASE-only**, plus the
   two weak-undef `Q7_POOL_RELEASE_EXTISA_0_{JSON,SO}` symbols (the POOL container, see [× POOL](./sunda-pool.md)).
   `[HIGH/OBSERVED]`
2. **4 getters/engine, vs CAYMAN's 12–14.** Each SUNDA non-POOL engine surfaces exactly **4** getters
   (RELEASE × `{IRAM, DRAM, SRAM, EXTRAM}`); `IRAM`/`DRAM` carry real bytes, `SRAM`/`EXTRAM` are **zero-size
   boundary cursors** (`movq $0x0,(%rsi)`, lea → next blob). CAYMAN ships **14** for ACT/DVE/PE (12 base + 2
   PROF) and **12** for SP (no PROF). SUNDA has **NO PROF getter, NO DKL, NO in-library EXTISA** on any engine.
   `[HIGH/OBSERVED]`
3. **All 8 non-POOL carves are byte-identical (sha256) to the `libnrtucode.a` RELEASE member `.rodata`** —
   `8/8 IDENTICAL` (independent 2-source proof: the `internal.so` getter blob == the archive member). The 48
   SUNDA `.a` members = 6 engine-classes × 8 (DEBUG+RELEASE × 4 regions) = **24 DEBUG + 24 RELEASE**; the
   DEBUG superset (no getters in `internal.so`) is what enables the module-spine enumeration below. `[HIGH/OBSERVED]`
4. **Uniform reset/boot across all five NX engines, == SUNDA POOL, == CAYMAN in the reset *value*.** Every
   IRAM head is `06 76 00 00 00 00 86 77 00 00 00 00` (`j 0x1dc` / `j 0x1e8`); the `0x1dc` trampoline decodes
   **exactly** `const16 a0,0 ; const16 a0,148(0x94) ; jx a0 → enter_run @0x94`. The **only** boot delta vs
   CAYMAN is the **+0x4** entry-point shift (CAYMAN `enter_run @0x90`; SUNDA `@0x94`). DRAM head =
   `0x6099cb34` `.globstruct` magic (== CAYMAN) + the byte-identical `4 × 0x00001000 @0x18` dispatcher-state
   init block. `[HIGH/OBSERVED]`
5. **The NX dispatch is the SUNDA base-subtraction segmented "Sunda-mode" flavor** — `sub a2,a2,a3 = 1`,
   `addi a2,a2,-65 = 0`, `movi a3,177 = 0` on **every** engine. It is **NOT** the `addi-0x41`-ASCII-normalized
   indexed table CAYMAN DVE/POOL use, and **NOT** a 177-entry table. This is the **origin** of the dispatch
   that [MARIANA SP](./mariana-sp.md) and MAVERICK SP inherit byte-exact (the flavor *named after SUNDA*). `[HIGH/OBSERVED]`
6. **The observability floor is TOTAL — on every engine, even in DEBUG.** Zero `S:` handler-name logs, zero
   `"Dispatch opcode=0x%x"`, zero compute-name self-strings (`Matmul`/`Activate`/`Dropout`/…). The CAYMAN
   `S:`-log handler set-diff method ([× ACT](./cayman-act.md)/[DVE](./cayman-dve.md)/[PE](./cayman-pe.md)/[SP](./cayman-sp.md)
   §5) is **INAPPLICABLE** on SUNDA for every engine. The per-engine CAYMAN diff is therefore expressed at the
   **string/opcode-ABSENCE** level, anchored to the shared `sunda/seq/` module spine — never fabricated as a
   SUNDA handler-name count. `[HIGH/OBSERVED]`

> **GOTCHA — "SUNDA is a reduced-engine baseline" is the wrong framing.** The reduction is **per-engine**, not
> per-set. SUNDA has the **same six engine-classes** as CAYMAN (five NX + the POOL Q7). What it lacks is
> *inside* each engine: the DEBUG/PERF/TEST flavor split, the PROF CAM/TABLE, the DKL family, the EXTISA
> container, and the CAYMAN compute-extension opcodes. A reimplementer must provision **all five NX engines**
> for a v2-compatible part — just with the lean per-engine handler floor. `[HIGH/OBSERVED]`

---

## 2. Inventory + carve — the 8 non-POOL SUNDA images

### 2.1 The getters (instruction-exact)

Each getter is the 4-instruction `(img-ptr, size)` stub
(`lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`) disassembled this session from
`.text 0x9b2d20..0x9b2f80`. The `.data` VAs (use plain `nm` — they are local `r`/`t` symbols) and the
`movq $<size>` immediates were both re-read; they match the SX-IMG-02 catalog rows 547–570 exactly. `[HIGH/OBSERVED]`

| ENGINE | REGION | ACCESSOR (.text VA) | IMG-PTR (= file off) | SIZE | STATUS |
|---|---|---|---|---:|---|
| ACT | IRAM | `0x9b2d20` | `0x055f0` | `0x8f20` | REAL (SEQ code; reset `j 0x1dc`) |
| ACT | DRAM | `0x9b2d40` | `0x0e510` | `0x2120` | REAL (SEQ data; no `S:` logs) |
| ACT | SRAM | `0x9b2d60` | `0x10630` | `0` | EMPTY (cursor → DVE IRAM) |
| ACT | EXTRAM | `0x9b2d80` | `0x10630` | `0` | EMPTY (cursor → DVE IRAM) |
| DVE | IRAM | `0x9b2da0` | `0x10630` | `0xbab0` | REAL |
| DVE | DRAM | `0x9b2dc0` | `0x1c0e0` | `0x2660` | REAL |
| DVE | SRAM | `0x9b2de0` | `0x1e740` | `0` | EMPTY (cursor → PE IRAM) |
| DVE | EXTRAM | `0x9b2e00` | `0x1e740` | `0` | EMPTY (cursor → PE IRAM) |
| PE | IRAM | `0x9b2e20` | `0x1e740` | `0xb3d0` | REAL |
| PE | DRAM | `0x9b2e40` | `0x29b10` | `0x2300` | REAL |
| PE | SRAM | `0x9b2e60` | `0x2be10` | `0` | EMPTY (cursor → POOL IRAM) |
| PE | EXTRAM | `0x9b2e80` | `0x2be10` | `0` | EMPTY (cursor → POOL IRAM) |
| SP | IRAM | `0x9b2f20` | `0x3b580` | `0xb450` | REAL |
| SP | DRAM | `0x9b2f40` | `0x469d0` | `0x2220` | REAL |
| SP | SRAM | `0x9b2f60` | `0x48bf0` | `0` | EMPTY (cursor → Q7_POOL IRAM) |
| SP | EXTRAM | `0x9b2f80` | `0x48bf0` | `0` | EMPTY (cursor → Q7_POOL IRAM) |

The POOL NX (idx 2) + Q7_POOL getters sit between PE and SP in the layout (`0x9b2ea0..0x9b3000`) — see
[× POOL](./sunda-pool.md). **No SUNDA non-POOL engine carries a non-zero SRAM/EXTRAM**; the eight zero-size
stubs all `movq $0x0,(%rsi)` and `lea` to the contiguous-layout cursor (the *next* engine's IRAM). `[HIGH/OBSERVED]`

> **GOTCHA — the SP getter `.data` VA is `0x3b580`, not its catalog row index.** SUNDA SP is `engine_idx 4`
> (`TPB_SP`) per the ISA enum (§4); the `.data` VA `0x3b580` is just its file offset in the contiguous
> `.rodata` layout (POOL's NX+Q7 blobs precede it). Don't confuse the layout offset with the engine index.
> `[HIGH/OBSERVED]`

### 2.2 Carve provenance + the 2-source byte-identity reconciliation

Carve rule (identity map): `blob = so[IMG-PTR : IMG-PTR+SIZE]` (python slice). The 8 real non-POOL images
carved this session; sha256 (full) **reproduces SX-IMG-24 exactly**, and **each carve is byte-identical
(sha256) to the matching `libnrtucode.a` RELEASE member `.rodata`** (`ar x` + `objcopy --only-section=.rodata`):
`[HIGH/OBSERVED]`

| IMAGE | FILE-OFF | SIZE | sha256 (full) | `.a` RELEASE member |
|---|---|---:|---|---|
| `SUNDA_NX_ACT_IRAM` | `0x055f0` | `0x8f20` | `b7e0a63a53540f427f73bc20b0f1fd658e61661085b684d8538239f466412bb0` | IDENTICAL |
| `SUNDA_NX_ACT_DRAM` | `0x0e510` | `0x2120` | `065eff7be7a750d8e1bf3f51ad560e697d894df1f1de673c0bce39dfed27e632` | IDENTICAL |
| `SUNDA_NX_DVE_IRAM` | `0x10630` | `0xbab0` | `817e35709f96a5b3ac38c353f1b997182f7c84f9ddfaf319f3cd65ba644bbbdd` | IDENTICAL |
| `SUNDA_NX_DVE_DRAM` | `0x1c0e0` | `0x2660` | `1947b5faa1cd646231a18359c1d4ff6baa79942b05f55c9f823ea777de0f7b3f` | IDENTICAL |
| `SUNDA_NX_PE_IRAM` | `0x1e740` | `0xb3d0` | `6d390ba876c4e281b433d65cac47b1ca7cd10e816f0736a909a40ca8b6ddebad` | IDENTICAL |
| `SUNDA_NX_PE_DRAM` | `0x29b10` | `0x2300` | `3660e8547c5cf94fc1905c3abf915225eb1d99d0459f49b656b230a387a822ed` | IDENTICAL |
| `SUNDA_NX_SP_IRAM` | `0x3b580` | `0xb450` | `5feb17aac1a8c3d1eaa552f04732c6002b70c7beaab41b708d6ca65927a5799c` | IDENTICAL |
| `SUNDA_NX_SP_DRAM` | `0x469d0` | `0x2220` | `0eb88c5ba2fb33e4ef0bf2b87dfec4772aac5e3da762c9ab0054b0d432c97c1c` | IDENTICAL |

The archive **also** holds the SUNDA **DEBUG** members per engine (`img_SUNDA_NX_{ACT,DVE,PE,SP}_DEBUG_{IRAM,DRAM,SRAM,EXTRAM}`)
that `internal.so` does **not** surface as getters — used below (`ar x` + `.rodata`-strip) for the module-set
enumeration, since the RELEASE flavor strips all assertion/log strings. SUNDA DEBUG **DRAM** sizes (distinct
from RELEASE): ACT `0x3300`, DVE `0x3800`, PE `0x3360`, SP `0x3390`. `[HIGH/OBSERVED]`

### 2.3 The `.a` member split — where the 48 land

`ar t libnrtucode.a` = **435** members; the per-generation split is byte-clean: `[HIGH/OBSERVED]`

| Generation | `.a` image members | Notes |
|---|---:|---|
| **SUNDA (v2)** | **48** | 6 engine-classes × 8 = (DEBUG + RELEASE) × `{IRAM, DRAM, SRAM, EXTRAM}` = **24 DEBUG + 24 RELEASE** |
| CAYMAN (v3) | 124 | DEBUG/PERF/TEST × 4 regions + PROF CAM/TABLE + DKL + EXTISA |
| MARIANA (v4) | 124 | |
| MARIANA_PLUS (v4+) | 124 | |
| MAVERICK (v5) | **0** | no `.a` members (carved from `internal.so` only) |
| *(runtime, non-image)* | 15 | `common.c.o`, `nrtucode*.c.o`, `prelink*.c.o`, `pi_library_load.c.o` |

`48 + 124×3 + 0 + 15 = 435`. The **48** SUNDA members cover **all six engine-classes** in both DEBUG and
RELEASE — i.e. SUNDA's `.a` footprint is one-third of CAYMAN's *not because engines are missing*, but because
SUNDA ships **2 flavors** (DEBUG+RELEASE) where CAYMAN ships **3** (DEBUG/PERF/TEST) **plus** the PROF, DKL
and EXTISA member families SUNDA omits entirely. `[HIGH/OBSERVED]`

---

## 3. Reset / boot geometry — uniform, and the +0x4 shift vs CAYMAN

All four non-POOL engines are **flat device segments** (no ELF magic) — identical packaging to SUNDA POOL NX
and CAYMAN. The reset/boot is **byte-identical across ACT/DVE/PE/SP** (and == SUNDA POOL). `[HIGH/OBSERVED]`

```text
IRAM head (all four engines, xxd -l12):  06 76 00 00 00 00  86 77 00 00 00 00
  0x000:  06 76 00   j 0x1dc        ; primary reset vector -> boot
  0x006:  86 77 00   j 0x1e8        ; secondary vector -> halt trap
  0x1dc:  04 00 00   const16 a0,0
  0x1df:  04 94 00   const16 a0,148    ; (= 0x94)   <-- SUNDA: 0x94.  CAYMAN here: 0x90.
  0x1e2:  a0 00 00   jx a0          ; jump to C enter_run @ 0x94
  0x1e8:  00 52 00   halt 0         ; 2nd vector = HALT trap
DRAM head (all four):  34 cb 99 60                ; .globstruct magic 0x6099cb34  (== CAYMAN)
DRAM @0x18 (all four):  4 × 0x00001000             ; dispatcher-state init  (== CAYMAN / SUNDA POOL)
```

The trampoline at `0x1dc` was decoded **byte-for-byte on each of the four engines** (`const16 a0,148 ; jx a0`)
— and the **CAYMAN** SP image at the same `0x1dc` decodes `const16 a0,144(0x90) ; jx a0`. So the reset
**value** (`j 0x1dc`, the `{v2,v3}` family) is shared, the trampoline **shape** is shared, and the **only**
boot difference is the **+0x4 `enter_run` entry-point shift**: `[HIGH/OBSERVED]`

| engine | SUNDA reset | CAYMAN reset | SUNDA `enter_run` | CAYMAN `enter_run` | delta |
|---|---|---|---|---|---|
| ACT | `j 0x1dc` | `j 0x1dc` | `@0x94` | `@0x90` | **+0x4** |
| DVE | `j 0x1dc` | `j 0x1dc` | `@0x94` | `@0x90` | **+0x4** |
| PE | `j 0x1dc` | `j 0x1dc` | `@0x94` | `@0x90` | **+0x4** |
| SP | `j 0x1dc` | `j 0x1dc` | `@0x94` | `@0x90` | **+0x4** |
| *(POOL NX `j 0x1dc` / Q7 `j 0x220`, also `@0x94` — see [× POOL](./sunda-pool.md))* | | | | | |

> **NOTE — the +0x4 is a relocated trampoline, not a different boot.** CAYMAN (v3, NC-v3) carries the
> `06 76`→`06 7d` `+0x1c` NX-only reset-shift family elsewhere in the gen ladder; SUNDA (v2) is a *prior*
> point — same `06 76` reset value, just `enter_run` at `0x94` instead of `0x90`. The v2 boot is a **relocated
> variant of the same trampoline shape**, not a new boot path. `engine_idx` is the same late-bound,
> base-addr-derived value the CAYMAN pages describe ([× SP §4c](./cayman-sp.md)) — which is **why** all five
> SUNDA NX engines share one reset + one trampoline. `[HIGH/OBSERVED reset/boot; the late-binding INFERRED-HIGH
> from the shared boot across all 5 engines.]`

### 3.1 Engine layout — fully contiguous

The full SUNDA engine order is **byte-contiguous** (each blob's end == the next blob's start), verified from
the getter `.data` VAs + sizes: `[HIGH/OBSERVED]`

```text
ACT_IRAM 0x055f0 → ACT_DRAM 0x0e510 → DVE_IRAM 0x10630 → DVE_DRAM 0x1c0e0 →
PE_IRAM  0x1e740 → PE_DRAM  0x29b10 → POOL(NX) 0x2be10 → … → SP_IRAM 0x3b580 →
SP_DRAM  0x469d0 → Q7_POOL_IRAM 0x48bf0 → …
```

Every adjacency closes exactly (`0x055f0+0x8f20 = 0x0e510`, `0x469d0+0x2220 = 0x48bf0`, …). The order is
`ACT → DVE → PE → NX_POOL → NX_SP → Q7_POOL`; **NX_SP is interleaved between the two POOL cores**, and there
are no gaps. `[HIGH/OBSERVED]`

### 3.2 Decode census — full windowed-ABI + IVP datapath, exit 0

The shipped `ncore2gp` objdump decodes all four RELEASE IRAMs to real Xtensa windowed-ABI + FLIX/VLIW + IVP
vector code (exit 0, empty stderr). The window-frame + call spine reproduces **byte-exact** this session; the
distinct-IVP figures are SX-IMG-24's full FLIX-bundle decode: `[HIGH/OBSERVED spine; IVP per SX-IMG-24]`

| ENGINE | `entry` | `retw` | `call8` | const16 | distinct-IVP | total-IVP |
|---|---:|---:|---:|---:|---:|---:|
| ACT | 256 | 84 | 194 | 1717 | 212 | 463 |
| DVE | 274 | 86 | 221 | 2219 | **232** | 666 |
| PE | 336 | 86 | 236 | 2213 | 222 | 588 |
| SP | 406 | 81 | 245 | 2181 | 209 | 663 |

DVE carries the **highest** distinct-IVP count (232 — the data/vector engine), then PE (222), ACT (212), SP
(209). All four are genuine separately-compiled `sunda/seq/` sequencers, not stubs.

> **GOTCHA — the FLIX literal-pool desync caps the IVP linearization.** A naïve linear sweep with stock
> tooling under-counts IVP ops because the `sub a2,a2,a3` dispatch hub and the IVP datapath decode with
> `.byte` bundle-boundary markers (the documented SX-FW-00 frontier). The `entry`/`retw`/`call8` window spine
> reproduces exactly (256/274/336/406 entry, 84/86/86/81 retw, 194/221/236/245 call8 — re-verified this
> session); the distinct-IVP figures rest on SX-IMG-24's bundle-aware decode. No desync touches the carve
> shas, reset bytes, `.a` split or the enum/header reads this page depends on. `[HIGH for the spine; the IVP
> counts CARRIED from SX-IMG-24's bundle-aware decode.]`

---

## 4. The NX dispatch — the SUNDA base-subtraction segmented flavor

Re-checked per engine in the RELEASE IRAM with the `ncore2gp` objdump (counts this session): `[HIGH/OBSERVED]`

| engine | `sub a2,a2,a3` | `addi a2,a2,-65` | `movi a3,177` | `addx4` | `jx` |
|---|---:|---:|---:|---:|---:|
| ACT | **1** | 0 | 0 | 41 | 12 |
| DVE | **1** | 0 | 0 | 28 | 13 |
| PE | **1** | 0 | 0 | 29 | 12 |
| SP | **1** | 0 | 0 | 28 | 12 |
| POOL | **1** | 0 | 0 | *(see [× POOL](./sunda-pool.md))* | |

On **every** SUNDA NX engine: exactly **one** `sub a2,a2,a3` (the register-base subtraction) feeding
`const16`-base `addx4` indexed jumps; **zero** `addi a2,a2,-65` (the `'A'`-`0x41` ASCII normalization CAYMAN
DVE/POOL use); **zero** `movi a3,177` (the 177-entry table bound). This is the **segmented base-subtraction**
dispatch — identical *in kind* to the "Sunda-mode" flavor [MARIANA SP](./mariana-sp.md) and MAVERICK SP carry
byte-exact (and which those pages explicitly name *after* SUNDA). SUNDA is the **origin**; the later SPs keep
it byte-exact. `[HIGH/OBSERVED for the sub/addi/movi counts on all 5 engines.]`

```c
// ---------------------------------------------------------------------------
// SUNDA NX dispatch (all 5 engines) — base-subtraction SEGMENTED "Sunda-mode".
//   sub a2,a2,a3 : a2 = opcode - segment_base   (the register-base subtraction)
//   addx4 idx    : const16-base indexed jump table, addressed by the normalized index
//   raw-compare leaves : per-segment beqi a2,N arms (e.g. SP beqi a2,64; PE beqi a2,5)
// NOT the CAYMAN addi-a2,a2,-65 (ASCII '0x41') normalization; NOT a 177-entry table.
// The exact per-opcode hub is the FLIX-desync frontier (SX-FW-00): the sub-a2,a2,a3
// site decodes with .byte bundle markers, so it is not fully linearizable.
// ---------------------------------------------------------------------------
static handler_t seq_dispatch(uint8_t opcode) {
    // a3 is loaded with a const16 SEGMENT BASE; a2 = opcode; then:
    uint8_t idx = opcode - seg_base;              // sub a2, a2, a3
    if (idx < seg_count)                          // segment in-range?
        return jump_table[idx];                   // addx4-indexed const16-base jump (the segmented hub)
    // ... per-segment raw-compare leaves (beqi a2,N) for the sparse opcodes ...
    return error_handler;                         // Bad Opcode -> "S: ErrorHandler" (DEBUG only; stripped on RELEASE)
}
```

> **NOTE — reconciling the "raw-compare" reading.** SX-IMG-23 (the SUNDA POOL page) reported POOL as a
> "raw-compare chain, no `0x41`-normalization" and observed `movi a3,184 ; beq` sites. Both readings are
> consistent: the SUNDA dispatch combines a **base-subtraction segmented table** (`sub a2,a2,a3` + `addx4`
> indexed hub) **with** per-segment **raw-compare leaves** (the `beqi a2,N` arms). The unified, precise
> reading — across all five engines + the re-checked POOL — is the **segmented base-subtraction flavor with
> raw-compare leaves**. `[HIGH for the sub/addi/movi counts; the unified-flavor synthesis INFERRED-HIGH from
> the counts + the MARIANA/MAVERICK "Sunda-mode" precedent.]`

---

## 5. The observability floor + the per-engine handler diff vs CAYMAN

### 5.1 The floor is TOTAL — on every engine, even in DEBUG

The CAYMAN `S:`-log handler set-diff method ([× ACT](./cayman-act.md)/[DVE](./cayman-dve.md)/[PE](./cayman-pe.md)/[SP](./cayman-sp.md)
§5) **cannot run on SUNDA for any engine**. SUNDA's DEBUG DRAMs carry **zero** handler self-naming: `[HIGH/OBSERVED]`

| engine | SUNDA DEBUG `S:` logs | CAYMAN `S:` logs (the diff baseline) |
|---|---:|---|
| ACT | **0** | ~41 (incl. `Activate`/`Cast`/`Copy`/`TensorScalar`) |
| DVE | **0** | 53-name set (incl. the 28 bn/scan/dropout) |
| PE | **0** | 24-name set (incl. `Matmul`/`Ldweights`/`LdTags`) |
| SP | **0** | 142 logs / 18-handler set |

Zero `"Dispatch opcode=0x%x"`, zero per-handler names, zero compute-name strings (`Matmul`/`Activate`/`relu`/
`gelu`/`Dropout`/`Ldweights`/`TensorScalar`/… = **0** word-boundary grep hits on every engine's DEBUG IRAM+DRAM).
Set-diffing the four DEBUG DRAMs: **62 strings are common to all four**, and the **per-engine-unique count is
ACT=0, PE=0, SP=0, DVE=1** — the **sole** engine-distinguishing string in the entire DRAM data segment is DVE's
`/opt/workspace/NeuronUcode/sunda/seq/src/uarch.hpp:161` `0 && "not a supported tscr op"` (the tensor-scalar
decode arm — DVE is the tensor-scalar-rich engine; ACT/PE/SP have byte-identical string sets). The engines are
byte-distinct **only** in their IRAM code bodies and dispatch tables. `[HIGH/OBSERVED]`

> **GOTCHA — `Cast` survives the absence scan as RTTI, not as a handler.** The raw substring `Cast` returns 2
> hits per engine — but both are `std::bad_cast` / `St8bad_cast` (C++ typeinfo), **not** the compute op; the
> word-boundary `\bcast\b` count is **0** on every engine. A reimplementer grepping bare `Cast` will draw a
> false positive. The compute-name floor (no `Activate`/`Cast`/`Copy`/`Matmul`/… handler self-string) holds.
> `[HIGH/OBSERVED]`

The shared `sunda/seq/` **module spine** (recovered from the DEBUG DRAM source-path assertions — the only
handler-level evidence available) is **exactly 16 distinct `.cpp`/`.hpp` basenames, byte-identical across
ACT/PE/SP** (DVE adds `uarch.hpp` as a 17th, the `tscr` arm above):

```text
alu_op.cpp  branch.cpp  branch_prefetch_hint.cpp  cache.hpp  error_handler.cpp
error_notifications.cpp  evsem_block.hpp  exception_handler.hpp  fetch.hpp  fsm.hpp
interrupt_handler.hpp  legacy_dma.hpp  move.cpp  move_shape.cpp  signal_handler.cpp  soc_window_manager.hpp
```

Only **6** of the 16 carry full `/opt/workspace/NeuronUcode/…` paths with `line:expr` assert text —
`src/decode/{alu_op,branch,move}.cpp`, `src/handlers/signal_handler.cpp`, `sunda/seq/src/handlers/
exception_handler.hpp`, and (DVE-only) `sunda/seq/src/uarch.hpp`; the other 10 are stored as bare
null-terminated basenames (a `__FILE__`-basename table + a parallel offset array), not full paths. So all five
SUNDA NX engines are the **same `sunda/seq/` SEQ codebase** — the fetch/cache/fsm/branch control spine +
move/alu_op decode + legacy_dma + soc_window translation + exception/interrupt/signal handlers — but **without**
the per-handler `S:` self-naming and **without** the CAYMAN extended layer. `[HIGH/OBSERVED]`

### 5.2 The per-engine diff vs CAYMAN (anchored to absences, not `S:` names)

Because the `S:` names are gone, the CAYMAN handler-**name** diff is expressed at the **string/opcode-absence**
level. The CAYMAN handler counts are carried as *context* (never re-derived as SUNDA observations):

#### ACT — SUNDA × ACT vs [CAYMAN × ACT](./cayman-act.md) (26)

| CAYMAN ACT (26 = 18 core + 7 ACT-only + `EngineNop`) | SUNDA ACT |
|---|---|
| **Shared 18-handler control core** (`AluOp`/`BRANCH`/`MOVE`/`Event_Semaphore`/`POLL_SEM`/…) | **PRESENT** (module spine; un-named) |
| **`Activate` (0x21)** | datapath present (LUT-driven activation math, 212 distinct IVP); **un-named** |
| **`ActivateQuantize` (0x22)** | present in datapath; un-named |
| **`ActivationTableLoad` (0x23)** | present (the LUT-load arm); un-named |
| **`ActivationReadAccumulator` (0x24)** | present; un-named |
| **`Cast` / `Copy` / `TensorScalar`** | present; un-named |
| **`EngineNop`** | present (shared serialization helper) |

ACT was already the lean activation engine on CAYMAN (no big extension layer to lose). The
`ActivationTableLoad`+`Activate` model is **structurally present** (the `sunda/seq/` spine + the LUT-driven IVP
datapath) but unverifiable by name. **The decisive ACT-specific SUNDA delta is the activation PWP header
divergence (`dbdca26b`, §6).** `[HIGH/OBSERVED for the absences + IVP count; the per-handler-name mapping
INAPPLICABLE.]`

#### DVE — SUNDA × DVE vs [CAYMAN × DVE](./cayman-dve.md) (53, the richest)

| CAYMAN DVE (53 = 18 core + 28 DVE-only + …) | SUNDA DVE |
|---|---|
| **Shared 18-handler control core** | **PRESENT** (module spine) |
| **6 batch-norm** (`BatchNormalize{,BackProp,GradAccum,GradAccum2,ParamLoad,ParamLoad2}`) | datapath present (highest distinct-IVP = 232); **un-named** |
| **4 predicated** (`CastPredicated`/`CopyPredicated{,Reduce,Scalar}`) | present; un-named |
| **`Dropout`** (uses `xorwow` RNG on CAYMAN) | **RNG ABSENT** — 0 `xorwow`/`lfsr`/`rand` strings; SUNDA DVE either lacks Dropout-RNG or computes it un-named (the RNG handler/algo is at the floor) |
| **Match/find/select (5)**, **Scan/transpose/shuffle (4)** | datapath present; un-named |
| **`tscr` decode arm** | **uniquely named** — `uarch.hpp:161` `"not a supported tscr op"` (the only engine-distinguishing string) |

DVE is the only engine with an engine-distinguishing string at the SUNDA floor (the `tscr` assertion). The
CAYMAN-rich bn/predicated/scan/dropout set exists as un-named IRAM code; **the RNG layer is the cleanest
verifiable absence.** `[HIGH for the absences + IVP + the tscr string; the 28-DVE-only-name mapping
INAPPLICABLE.]`

#### PE — SUNDA × PE vs [CAYMAN × PE](./cayman-pe.md) (24)

| CAYMAN PE (24 = 18 core + `EngineNop` + 5 PE-only) | SUNDA PE |
|---|---|
| **Shared 18-handler control core + `EngineNop`** | **PRESENT** (module spine) |
| **`Ldweights` / `Matmul` / `LdTags` / `PeRegWrite`** | dense matmul-array micro-op datapath present (222 distinct IVP); **un-named** |
| **`MatmulSparse`** (CAYMAN binds the `S4D3_MM` 4D/sparse struct) | **sparse variant is a candidate absence** — un-verifiable by name; the dense path is present |
| **`PeManageSeed 0x08` / `LdweightsMX 0x09` / `MatmulMX 0x0A`** (MARIANA-first) | **ABSENT at the floor** — not in the SUNDA opcode enum (§5.3); the SUNDA PE roster is the **floor below CAYMAN** |
| **`ConvLutLoad 0xe4`** (enum-defined at CAYMAN) | **ABSENT** from the SUNDA opcode enum entirely |

The SUNDA PE roster is the v2 floor: the dense matmul path present and un-named; the sparse variant a candidate
absence; the MX/seed extensions not even enum-defined. `[HIGH for the absences + IVP; the 5-PE-only-name
mapping INAPPLICABLE.]`

#### SP — SUNDA × SP vs [CAYMAN × SP](./cayman-sp.md) (18 = the exact 5-way intersection)

| CAYMAN SP (18, the pure sync/control core) | SUNDA SP |
|---|---|
| **The exact 5-way intersection** (`AluOp`/`BRANCH`/`BranchPrefetchHint`/`Event_Semaphore`/`EXT_BREAK`/`Halt`/`INS_BREAK`/`INS_FL`/`MOVE`/`NOP`/`NOTIFY`/`POLL_SEM`/`Redirect`/`SET_OM`/`STRONG_ORDER`/`TensorLoad`/`TensorStore`/`WRITE`) | **PRESENT** as modules — `evsem_block.hpp` + `interrupt_handler.hpp`/`signal_handler.cpp` + the `sunda/seq/` spine; byte-for-name identical to ACT/DVE/PE → **no SP-only module** |
| **No PROF, no paired Q7** | **same** (SP is NX-only on SUNDA too) |
| **18 distinct handler count** | **un-verifiable by name** (0 `S:` logs) — CARRIED from CAYMAN; the strict-subset control-spine model upheld |

SP remains the **leanest** engine — its module spine (evsem_block, fetch, fsm, branch, cache, legacy_dma,
soc_window, interrupt) is **byte-for-name identical** to ACT/DVE/PE, confirming SP carries **no SP-only
module**: it is the strict-subset control core, exactly as on CAYMAN. The exact count of **18** cannot be
re-derived without the logs (the honest caveat). `[HIGH for the modules + no-SP-only-module; the count-18 is
CARRIED from CAYMAN, INAPPLICABLE to re-verify on SUNDA.]`

### 5.3 The v2-floor opcode characterization — what is ABSENT, pinned to the enum

Read directly from the shipped per-gen `aws_neuron_isa_tpb_common.h` opcode enum (the `// Y`/`// n` maintenance
flags are the header's own): `[HIGH/OBSERVED]`

| opcode | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | MAVERICK (v5) | first-ship |
|---|---|---|---|---|---|
| `PE_MANAGE_SEED 0x08` | **absent** | absent | `:163` `// Y` | present | **MARIANA** |
| `LDWEIGHTS_MX 0x09` | **absent** | absent | `:164` `// Y` | present | **MARIANA** |
| `MATMUL_MX 0x0A` | **absent** | absent | `:165` `// Y` | present | **MARIANA** |
| `CONV_LUT_LOAD 0xe4` | **absent** | `:294` `// Y` | `:304` `// Y` | present | **CAYMAN** |
| `TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH 0x87` | `:221` `// n` | dropped | dropped | dropped | **SUNDA-only** (retired NC-v3+) |
| `TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC 0x88` | `:222` `// n` | dropped | dropped | dropped | **SUNDA-only** (disabled even on SUNDA) |

So at the **enum** level, SUNDA is the floor below:
- **`PeManageSeed`/`LdweightsMX`/`MatmulMX`** — MARIANA-first (CAYMAN has **0** hits too; SUNDA inherits the
  absence). This matches the committed PeManageSeed boundary (CAYMAN 0 / MARIANA first-ship at `0x08`/`0x09`/`0x0a`).
- **`ConvLutLoad 0xe4`** — enum-defined starting at **CAYMAN**; absent from the SUNDA enum entirely.
- And SUNDA is **above** the `0x87`/`0x88` DUAL: those two `TensorScalarPtrMultiDual{Arith,Bitvec}` opcodes are
  **SUNDA-only** (defined `// n` at `common.h:221`/`:222`, dropped on cayman+). See §7.

> **NOTE — "absent from PE on CAYMAN" (handler) vs "enum-defined on CAYMAN" (opcode) for `ConvLutLoad`.** The
> committed [× PE](./cayman-pe.md) page states `ConvLutLoad 0xe4` "does not exist in any CAYMAN PE variant" —
> that is about the firmware *handler* (no CAYMAN PE `S:` log / no PE dispatch arm for `0xe4` until MARIANA),
> not the opcode enum (where `0xe4` is `// Y`-defined at CAYMAN). For **SUNDA** there is no ambiguity:
> `0xe4` is absent at *both* levels — no enum entry **and** zero firmware footprint (§5.4). SUNDA is the
> unambiguous ConvLut floor. `[HIGH/OBSERVED — both the SUNDA enum absence and the firmware absence.]`

### 5.4 The compute-layer absence ledger (string-level, all four engines)

Full DEBUG IRAM+DRAM string scan, all four engines — **0 hits each** (extends the SUNDA POOL absence ledger to
ACT/DVE/PE/SP): `[HIGH/OBSERVED]`

| token | meaning | SUNDA |
|---|---|---|
| `xorwow` / `lfsr` / `rand` | RNG (the `Dropout` randomizer) | **0** — no RNG layer |
| `dequant` / `cptc` / `mx` | TensorDequantize / cptc / MX dequant | **0** — no dequant/MX layer |
| `sb2sb` / `collective` | on-chip SB2SB `0xBF` collective | **0** — no on-chip collective |
| `extendedinst` | the `0xf0` ExtendedInst bridge (POOL-side) | **0** — no `0xf0` bridge |
| `convlut` | `ConvLutLoad` | **0** — no conv-LUT |
| `nonzero` / `sequence_bound` | `NonzeroWithCount` / `GetSequenceBounds` (`0xf2`) | **0** — no sequence-bounds |
| `PeManageSeed` / `LdweightsMX` / `MatmulMX` | the MARIANA PE seed/MX layer | **0** — floor below MARIANA |

The base per-engine compute (the activation math on ACT, the bn/scan/select on DVE, the matmul micro-ops on
PE, the sync/control core on SP) exists as un-named IRAM code; the **CAYMAN extension layer** (RNG, dequant/MX,
on-chip collective, conv-LUT, sequence-bounds, the `0xf0` bridge) is **absent across the SUNDA engine set**.
`[HIGH/OBSERVED for each absence at the string level; the per-handler-name mapping INAPPLICABLE.]`

---

## 6. dtype / PROF / activation-header deltas

### 6.1 dtype — 16-base UINT32/INT32/FP32 floor, == CAYMAN, on all four

The `move.cpp:41` dtype assertion is **byte-identical** across ACT/DVE/PE/SP and to CAYMAN (read verbatim from
the SUNDA ACT DEBUG DRAM this session): `[HIGH/OBSERVED]`

```text
/opt/workspace/NeuronUcode/src/decode/move.cpp:41
  ((ins.dtype == NEURON_ISA_TPB_DTYPE_UINT32) || (ins.dtype == NEURON_ISA_TPB_DTYPE_INT32)
   || (ins.dtype == NEURON_ISA_TPB_DTYPE_FP32))
  && "highest priority is full-register moves. TODO other dtypes"
```

No FP4/CPTC/MXTENSOR/SFP8 named dtype strings (those codes arrive at MARIANA). The SUNDA base is the **16-code
floor**, == CAYMAN, on every engine. SUNDA is the **BF16 bracket end** of the gen-extremes ladder — the v2
dtype floor with the wide-format extensions (the FP4/MX dtype codes) all added by later gens. `[HIGH/OBSERVED
dtype assertion; the BF16-bracket framing CARRIED from the gen-extremes page.]`

### 6.2 PROF — absent on every engine (the no-HW-decode-profiling floor)

SUNDA ships **no** PROF_CAM/PROF_TABLE getters and **no** `hwdecode_` `.a` members for any engine
(`nm | rg -ci 'SUNDA.*PROF' = 0`; `ar t | rg -ci 'sunda.*(prof|hwdecode)' = 0`). For contrast, CAYMAN ships
**8** PROF getters (`CAYMAN_NX_{ACT,DVE,PE,POOL}_PROF_{CAM,TABLE}_get`), where the four NX engines share the
**byte-identical** PROF_CAM (`8fd7e422`) / PROF_TABLE (`ce761f81`). SUNDA = the no-HW-decode-profiling floor
across the set. `[HIGH/OBSERVED]`

> **NOTE — PROF_CAM ≠ the activation CAM.** The **16-byte opcode-only PROF_CAM** is the HW
> instruction-decode profiler (keyed on `opcode`, 16B stride). It is **distinct** from the **32-byte**
> `aws_hal_stpb_act_cam_entry_t` activation-LUT CAM (keyed `(opcode, func_id)`, 32B stride). SUNDA omits the
> PROF_CAM **entirely** (no getter, no member); the activation CAM is part of the ACT PWP machine (next), and
> it is *that* CAM/PWP header that **diverges** on SUNDA — not the PROF profiler. See
> [PROF_CAM / PROF_TABLE formats](./prof-cam-table-formats.md). `[HIGH/OBSERVED]`

### 6.3 The SUNDA activation PWP header DIVERGES — sha `dbdca26b…`

The ACT piecewise-cubic (PWP) machine is described by four shipped header structs —
`aws_hal_stpb_act_{cam,profile,control,bucket}_entry_t` (CAM 32B / PROFILE 128B / CONTROL 32B / BUCKET 32B).
For cayman/mariana/mariana_plus/maverick the combined header is **byte-stable** (sha256 `8f6f5f49…`). **SUNDA
diverges — sha256 `dbdca26b…`** — a known SUNDA-specific de-share. The diff is **three fields, all within the
same 32B/128B/32B/32B sizes** (compile-verified equal-size to cayman): `[HIGH/OBSERVED]`

| # | field | SUNDA (v2) | cayman+ |
|---|---|---|---|
| (i) | CAM mask byte-order | `{func_id_mask@39:32, opcode_mask@47:40}` | **swapped** to `{opcode_mask@39:32, func_id_mask@47:40}` |
| (ii) | PROFILE FMA-bypass | `rsvd33:5 @439:435` (no FMA-bypass bit) | cayman+ **ADDS** `bias_scale_fma_bypass:1 @435` (+ `rsvd33:4 @439:436`) |
| (iii) | PROFILE batch-norm | `unused1:14 @767:754` (no bn-accum-read bit) | cayman+ **ADDS** `batchnorm_accumulator_rd:1 @754` (+ `unused1:13 @767:755`) |

So the SUNDA→cayman PWP step is a **minor config-bit growth** (FMA-bypass + batch-norm-accumulator-read) **plus
a CAM mask byte-order swap**; the **core PWL machine is unchanged** — the `CONTROL` index-extract
(`act_tbl_base:11`, `extract_lsb:5`, `extract_size:4`), the `BUCKET` cubic (`d0..d3, x0`; `d2`/`d3` present ⇒
piecewise-cubic, *not* linear) and the region/symmetry config are byte-stable v2→v5. `[HIGH/OBSERVED — header
read + sha both gens.]`

> **GOTCHA — the `dbdca26b` divergence is the *de-share* signal, not a different algorithm.** SUNDA is the
> *only* gen whose activation PWP header is not byte-identical to the cayman+ block; the divergence is **two
> added config bits + one mask byte-swap**, so a v2-compatible reimplementation must decode the **SUNDA** CAM
> mask order and **omit** the FMA-bypass / bn-accum-read control bits — but otherwise runs the **same cubic
> PWL** as the later gens. A reimplementer keying off the cayman+ `8f6f5f49` layout will mis-read the SUNDA
> CAM opcode/func_id masks. `[HIGH/OBSERVED]`

---

## 7. A SUNDA-specific algorithm — the retired DUAL `TensorScalarPtrMultiDual` (0x87 / 0x88)

SUNDA carries **two opcodes no later gen defines**: `TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH = 0x87`
(`common.h:221`) and `…_DUAL_BITVEC = 0x88` (`:222`), both flagged `// n, ucode/kaenadve exists, not
maintained/used`. They are **dropped** entirely on cayman/mariana/maverick (the enum no longer defines them).
This is the only opcode pair where **SUNDA is the *upper* extreme** — the v2-only fast-path retired after
NC-v2. See [SUNDA-only Dual / Deprecated TensorScalarPtr](../firmware/kernels/sunda-dual-tensorscalarptr.md)
for the full lineage. `[HIGH/OBSERVED]`

The DUAL merges **up to four** regular `TensorScalarPtr` instructions into one 4-D kernel, applying a
**dual** ALU op per slice — with the second op **config-fixed to multiply in `DVE_config`** (not a struct
field). It binds the **same 64-byte `NEURON_ISA_TPB_S4D4_TSM_STRUCT`** as the singular `PtrMulti 0x4F`/`0x5F`
(there is *no* DUAL-specific struct; the single `op`@36 is `op0`, and the absent `op1` is the config constant).
The `BITVEC` form (`0x88`) is **header-disabled** ("no sensible default 2nd op for bitvec to hard-code … tight
on uop table space in DVE"). Reproduced as annotated C pseudocode (the names are the real header identifiers
from `sunda/.../aws_neuron_isa_tpb_s4d4_tsm.h`): `[HIGH/OBSERVED — SUNDA header verbatim; the on-device datapath
bind MED/INFERRED — no DUAL firmware string exists (§7.1).]`

```c
/* TensorScalarPtrMultiDual{Arith,Bitvec} (0x87 / 0x88) — SUNDA ONLY.
 * Validity gate : is_valid_tensor_scalar_ptr_multi_dual()      (s4d4_tsm.h:183-189)
 *                 && has_tensor_scalar_ptr_multi_dual_opcode()  (:221-223, ARITH-only)
 *                 && has_valid_src_slices_tsm_dual()            (:260-263, W in 1..4)
 * op0 = i->s4d4_tsm.op (struct @36);  op1 = AluOp::Multiply  (DVE_config, NOT a struct field, :52-53)
 * The W scalar PAIRS were preloaded by a preceding TensorScalarImmLd (0x70/0x71).
 * Retired on cayman+ (enum name dropped). 0x88 BITVEC is header-disabled (:289-291). */
void tensor_scalar_ptr_multi_dual(const S4D4_TSM *i,
                                  const scalar_pair_t pairs[/*W*/],   /* from ImmLd flops */
                                  AluOp op1_fixed /* == AluOp::Multiply (0x06), DVE_config */)
{
    const uint8_t W   = i->src_mem_pattern.num_elem[3];  /* 1..4 scalar PAIRS (half the singular cap of 8) */
    const AluOp   op0 = i->op;                            /* @36; arith op-set gate (or bitvec, disabled)   */

    for (uint8_t w = 0; w < W; ++w) {                    /* outer W-slice; the pair swaps once per XYZ round */
        for_each_zyx_element(i->src_mem_pattern, w, /*[z,y,x]*/) {
            T src = load(i->in_dtype, &SRC[w][z][y][x]);
            /* dst = (src op0 pair[w][0]) op1 pair[w][1],  op1 == Multiply (config-fixed)                    */
            T t0  = alu_apply(op0,       src, pairs[w].s0, i->reverse_operands);  /* TensorScalar #1         */
            T out = alu_apply(op1_fixed, t0,  pairs[w].s1, i->reverse_operands);  /* * pair[w][1]            */
            store(i->out_dtype, &DST[w][z][y][x], out);
        }
    }
}
```

### 7.1 The firmware contributes a NEGATIVE result for the DUAL

There is **no identifiable DUAL worker** anywhere in the container: **zero** `"Dual"` strings blob-wide, and
SUNDA's only DVE image is the **name-stripped RELEASE** build (the carve here, `0x10630`/`0xbab0`) — a strings
scan of its `.rodata` window yields only packed binary, no self-name pool. So the DUAL decode ceiling is
**header-only** (`common.h` + `s4d4_tsm.h` + the stale `struct2opcode` JSON). The firmware contributes a
**negative** (no Dual worker is identifiable), not a positive carve — exactly the v2 observability floor at
work. The *singular* `PtrMulti 0x4F`/`0x5F` and the deprecated `TensorScalarPtr 0x44`/`0x54` *do* survive on
later gens; the DUAL `0x87`/`0x88` is the **hard-retired** end. `[HIGH/OBSERVED — `nm -S` + strings region scan;
the on-device DUAL datapath is MED/INFERRED — never fabricated.]`

> **NOTE — the `0x87` value was never "reused".** SUNDA already carried `0x87` in **two** distinct enums at
> once — `OPCODE_*_DUAL_ARITH = 0x87` (`common.h:221`) **and** `UPDATE_MODE_SEM_SUB_REG_READ = 0x87`
> (`:350`). On cayman+ only the **OPCODE** name was dropped; the `UPDATE_MODE` constant is gen-stable. Opcode
> dispatch and update-mode selection are separate decode contexts, so the shared value is unambiguous in
> either. `[HIGH/OBSERVED]`

---

## 8. The v2-floor cross-engine synthesis (with the [SUNDA × POOL](./sunda-pool.md) page — COMPLETE)

SUNDA (`arch_id 5 / coretype 6 / NC-v2`) is the **v2 BASELINE**. With the POOL page and this one, the SUNDA
engine set is **complete** and the v2 floor is characterized across **all** engines: `[HIGH per row OBSERVED;
synthesis INFERRED-HIGH]`

| axis | SUNDA v2 (the floor) | CAYMAN v3 (the reference) |
|---|---|---|
| engine set | **ALL 5 NX (PE/ACT/POOL/DVE/SP) + POOL Q7** | same 5 NX + POOL Q7 (SUNDA **not** engine-count-reduced) |
| getters / engine | **4** (RELEASE × 4 regions) | 14 (ACT/DVE/PE) / 12 (SP) |
| flavors | **RELEASE only** | DEBUG / PERF / TEST (+ DKL) |
| PROF (CAM/TABLE) | **NONE** (any engine) | 2/engine (shared `8fd7e422`/`ce761f81`; SP=none) |
| DKL | **NONE** | full DKL family |
| in-library EXTISA | **NONE** | POOL EXTISA container |
| reset (all NX engines) | `j 0x1dc` (== CAYMAN) | `j 0x1dc` |
| `enter_run` | **`@0x94`** | `@0x90` (**+0x4**) |
| DRAM `.globstruct` magic | `0x6099cb34` (== CAYMAN) | `0x6099cb34` |
| NX dispatch | **base-subtraction segmented** (`sub a2,a2,a3`; "Sunda-mode") | `addi-0x41`-normalized table (DVE/POOL) / raw-compare (PE) |
| NX `S:` handler logs | **ABSENT** (every engine, even DEBUG) | present (41 ACT / 142 SP / …) |
| `"Dispatch opcode"` log | **ABSENT** | present |
| compute self-naming | **ABSENT** (0 `Matmul`/`Activate`/…) | present (per-handler `S:`) |
| dtype | **16-base** UINT32/INT32/FP32 | 16-base (== SUNDA) |
| activation PWP header | **`dbdca26b…` (DIVERGES)** — CAM mask swap + 2 fewer config bits | `8f6f5f49…` (stable v3→v5) |
| `PeManageSeed`/`MX` | **NONE** (MARIANA-first) | NONE (also; MARIANA-first) |
| `ConvLutLoad 0xe4` | **NONE** (not enum-defined) | enum-defined (`:294`); PE handler MARIANA-first |
| `0x87`/`0x88` DUAL | **DEFINED** (`// n`, SUNDA-only) | dropped |
| RNG / dequant / MX / SB2SB / ExtendedInst | **NONE** (every engine) | present (POOL/DVE) |
| IRAM size (RELEASE vs PERF) | ~half CAYMAN (ACT `0x8f20`, SP `0xb450`) | ACT `0x13dc0`, SP `0x182c0` |
| source tree | `sunda/seq/` + `sunda/pool/` | `cayman/seq/` + `dispatch.hpp` |
| engine order (contiguity) | `ACT→DVE→PE→POOL→SP→Q7` | per-gen |

**The v2-floor thesis.** SUNDA is the **same five-NX-engine + POOL-Q7 structure** as CAYMAN — every engine the
same `sunda/seq/` SEQ codebase (shared reset `j 0x1dc`, `enter_run @0x94`, `.globstruct 0x6099cb34`, the
fetch/cache/fsm/branch/move/alu_op/evsem/soc_window/exception module spine) — but the **per-engine v2 floor**:
(1) **packaging** — single RELEASE flavor, no PROF/DKL/EXTISA, 4 getters/engine; (2) **observability** — zero
`S:`/Dispatch/compute self-naming on every engine even in DEBUG; (3) **dispatch** — the base-subtraction
segmented "Sunda-mode" table, the origin MARIANA/MAVERICK SP inherit; (4) **compute** — dtype-16, no RNG/dequant/
MX/SB2SB/ExtendedInst/ConvLut/sequence-bounds, plus the `dbdca26b` activation-header divergence; (5)
**completeness** — SUNDA is **not** engine-count-reduced; the reduction is strictly per-engine. SUNDA opens the
bottom of the image matrix as the v2 baseline; every later gen (CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK) is a
**strict superset** over this floor. `[synthesis INFERRED-HIGH; every constituent fact OBSERVED.]`

---

## 9. Honesty ledger

**HIGH / OBSERVED (this session):**

- 24 SUNDA `*_get` accessors `nm`-listed (6 engine-classes × 4 regions, RELEASE-only) + 2 weak-undef EXTISA;
  the 8 non-POOL getters parsed instruction-exact (img-ptr + `movq $size` immediate); 8 real + 8 zero-size
  cursors. No DEBUG/PERF/TEST/PROF/DKL getter for any SUNDA engine.
- 8 real carves; sha256 reproduces SX-IMG-24 **exactly**; **8/8 byte-identical** (sha256) to the
  `libnrtucode.a` RELEASE member `.rodata` (independent 2-source proof).
- `.a` split: **435** members = SUNDA **48** + CAYMAN/MARIANA/MARIANA_PLUS **124** each + MAVERICK **0** + 15
  runtime; SUNDA 48 = 24 DEBUG + 24 RELEASE.
- All 4 non-POOL IRAM heads byte-identical `06 76 00 00 …` (`j 0x1dc`); the `0x1dc` trampoline decodes
  `const16 a0,148(0x94) ; jx a0 → enter_run @0x94` on each; CAYMAN at the same site = `const16 a0,144(0x90)`
  → **+0x4**. DRAM head `0x6099cb34` + init block `4×0x1000 @0x18` byte-identical on each.
- Dispatch: `sub a2,a2,a3 = 1`, `addi-65 = 0`, `movi-177 = 0` on all engines (the base-subtraction
  "Sunda-mode" flavor); `addx4` {41/28/29/28}.
- Observability floor: 0 `S:` logs on every DEBUG DRAM (sizes ACT `0x3300`/DVE `0x3800`/PE `0x3360`/SP
  `0x3390`); 0 `"Dispatch opcode"`; 0 word-boundary compute-name strings (`Cast` raw=2/engine is `std::bad_cast`
  RTTI, `\bcast\b`=0). 62 strings common to all four DEBUG DRAMs; per-engine-unique ACT/PE/SP=0, DVE=1 (the sole
  distinguishing string = DVE's `uarch.hpp:161` `"not a supported tscr op"`). Module spine = exactly 16
  basenames byte-identical across ACT/PE/SP, DVE +1. The CAYMAN SP-page infra strings (`sunda_fast_fetch`,
  `Sunda seq Loop`, `DGE`) do **not** appear on the SUNDA non-POOL engines (a bare `fast_fetch` + `dge_shape`
  do, uniformly across all four DEBUG DRAMs).
- `move.cpp:41` dtype assertion (UINT32/INT32/FP32) byte-identical across the four + to CAYMAN.
- PROF absence: no SUNDA PROF getter/member (0/0); CAYMAN ships 8 PROF getters. Activation PWP header
  diverges: SUNDA `dbdca26b…` vs cayman+ `8f6f5f49…` (3-field diff: CAM mask swap + 2 cayman+-added PROFILE
  config bits).
- Opcode-enum floor: `PeManageSeed 0x08`/`LdweightsMX 0x09`/`MatmulMX 0x0A` MARIANA-first (absent SUNDA+CAYMAN);
  `ConvLutLoad 0xe4` CAYMAN-first (absent SUNDA enum); `0x87`/`0x88` DUAL SUNDA-only (`common.h:221`/`:222`).
- Engine order `ACT→DVE→PE→NX_POOL→NX_SP→Q7_POOL` fully contiguous (no gaps).
- ISA enum `PE=0/ACT=1/POOL=2/DVE=3/TPB_SP=4/TOP_SP=5` (`aws_neuron_isa_tpb_common.h:139-146`); SUNDA SP =
  `TPB_SP` (engine_idx 4).

**MED / INFERRED:**

- The per-engine compute role (ACT=activation-LUT / DVE=bn-scan / PE=matmul-array / SP=sync-control):
  INFERRED-HIGH from the CAYMAN baselines + the IVP datapaths + the module spine; **not** re-verifiable by
  handler name on SUNDA (no logs).
- The unified "base-subtraction segmented + raw-compare-leaves" dispatch reading reconciling SX-IMG-23's
  "raw-compare" label: the sub/addi/movi **counts** are OBSERVED; the precise per-opcode hub is FLIX-desynced
  (SX-FW-00). The synthesis is INFERRED-HIGH from the counts + the MARIANA/MAVERICK precedent.
- The distinct-IVP figures (209–232) are CARRIED from SX-IMG-24's bundle-aware FLIX decode; the
  `entry`/`retw`/`call8` window spine reproduced byte-exact this session.
- The DUAL `0x87`/`0x88` on-device datapath — header-only (no firmware string; SUNDA DVE RELEASE is
  name-stripped). The fold (op0 then config-fixed multiply, scalar PAIR swap per W) is reported from the SUNDA
  **header** (HIGH for the header contract); its on-device bind is family-level INFERRED.
- `engine_idx` late-binding (runtime-computed from base addr) — INFERRED-HIGH from the shared reset/boot across
  all five NX engines.

**LOW / NOT CLAIMED:**

- The exact SUNDA per-engine handler **count** (the `S:` names are absent; the CAYMAN 26/53/24/18 counts are
  **not** re-derived on SUNDA — INAPPLICABLE by name, carried as context, never fabricated).
- The exact per-opcode SUNDA dispatch chain (FLIX-desync; not linearly recovered).
- Whether the SUNDA DVE `Dropout` retains an un-named RNG path or omits it (RNG strings = 0; datapath presence
  un-named).
- Which silicon part / runtime loads RELEASE vs the (`.a`-present) DEBUG build.

> **CORRECTION applied to SX-IMG-24 framing — none required for the substance.** SX-IMG-24's facts reproduce
> exactly against the binary (carve shas, `.a` split, reset/boot, dispatch, observability, dtype, the enum
> floor). One *refinement* this page adds (not a correction): SX-IMG-24 lists `ConvLut` only as a string-level
> absence (§5b/§7d); at the **enum** level `CONV_LUT_LOAD 0xe4` is **CAYMAN-first** (`common.h:294`), so SUNDA
> is the floor below it at *both* the enum and firmware levels — consistent with, and sharper than, the report.
> The `dbdca26b` activation-header divergence (cross-referenced from the ACT PWP analysis) is added here
> because it is the single most ACT-engine-specific SUNDA delta and was outside SX-IMG-24's engine-image scope.

---

## 10. Cross-references

- [SUNDA × POOL image (the v2 baseline)](./sunda-pool.md) — the POOL/Q7 half of the SUNDA set; the
  base-subtraction dispatch anchor and the EXTISA container.
- [SUNDA v2 Baseline Topology](../generations/sunda-v2-baseline.md) — the gen-level v2-floor synthesis this
  page grounds.
- The committed CAYMAN engine baselines this page diffs against: [× SP](./cayman-sp.md) (the SP-vs-TOP_SP page),
  [× ACT](./cayman-act.md), [× DVE](./cayman-dve.md), [× PE](./cayman-pe.md).
- [SUNDA-only Dual / Deprecated TensorScalarPtr](../firmware/kernels/sunda-dual-tensorscalarptr.md) — the full
  `0x87`/`0x88` DUAL lineage (§7) and the deprecation spectrum.
- [PROF_CAM / PROF_TABLE formats](./prof-cam-table-formats.md) — the 16-byte HW-decode profiler SUNDA omits
  (distinct from the 32-byte activation CAM that diverges, §6).
- [MARIANA × SP image](./mariana-sp.md) — the v4 SP that inherits the SUNDA base-subtraction dispatch byte-exact.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
