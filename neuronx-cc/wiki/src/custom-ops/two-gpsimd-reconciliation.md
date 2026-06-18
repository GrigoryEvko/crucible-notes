# The Two-GPSIMD Reconciliation — Pool-Engine Alias vs Xtensa Cluster

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. Two binaries carry a unit named "GPSIMD" and they are **different hardware**. The compiler/IR side lives in `neuronxcc/starfish/lib/libwalrus.so` + `libBIR.so` (cp310 frame: `.text`/`.rodata` VMA == file offset, `.data` is `+0x400000`; the `_0x5e9xxx` `.c` sidecars are PLT thunks, real bodies disassemble from `.text 0x135xxxx` — the two-VA-frame artifact). The custom-op CPU side lives in `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` — eight Tensilica Xtensa ELF32 executables. Disasm offsets quoted from the encoder body are grounded on the cp310 IDA tree (`CoreV3GenImpl::visitInstGPSIMDSB2SB` @ `0x1359840`, CONFIRMED); the cp312 anchor `0x13597a0` is ASSERTED, not verified — cp312 `libwalrus` is not in the indexed corpus (only the cp312 `walrus_driver` binaries are), so treat the cp312 delta as a noted prediction. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

The name **"GPSIMD"** appears twice in this toolchain, attached to two physically and architecturally distinct units, and a reverse-engineer who conflates them will build a memory model that cannot exist. This page is the decisive reconciliation: the two "GPSIMD" are a **name collision across two abstraction layers**, settled empirically by binary evidence, not by argument.

The first "GPSIMD" — call it the **Pool-alias GPSIMD** ([1.13](../arch/gpsimd-engine.md)) — is the *external display name of the internal Pool sequencer*. The `libBIR` engine table literally renames `EngineType Pool(1)` to the string `"GPSIMD"`, and its entire user-visible ISA surface is one fixed-function machine instruction, `InstGPSIMDSB2SB` (InstructionType 33, opcode `0xBF`), a cross-core State-Buffer→State-Buffer copy that swaps SBUF tiles between the two physical cores of a 2-core LNC. It is a *datapath the compiler emits and a hardware engine executes*. The second "GPSIMD" — the **Xtensa custom-op CPU cluster** ([11.1](gpsimd-xtensa-layout.md)) — is a software op-runtime: eight Tensilica Xtensa cores, each running a statically-linked freestanding image (`libbuiltincustomop_cpu{0..7}.stripped.so`) that embeds a PyTorch-c10 subset and the custom-op SDK. It is reached by a *completely different* BIR instruction, `InstCustomOp` (InstructionType 53), and has its own DRAM-windowed memory model that the compiler never sees.

The proof that these are two things and not one thing seen from two angles is threefold and **CONFIRMED** at the binary level: (i) the verbatim `libBIR` string `"ExternalEngineType used as EngineType. External: GPSIMD Internal: Pool"`; (ii) two distinct `InstructionType`s with two distinct encoders on two distinct engines; (iii) a *disjoint-namespace test* — the Xtensa `cpu0.so` has **zero** mentions of SBUF/SB2SB/Pool/partition/GPSIMD, and `libwalrus`/`libBIR` have **zero** mentions of `SUNDA_APB_BASE`/`MEM_WINDOW0`/`data_scratch_map`. Neither binary knows the other's memory model. The rest of this page builds the unified four-region address map that keeps them as adjacent-but-disjoint regions owned by different agents, identifies the single DMA bridge where they touch the same bytes, and ledgers every claim by confidence — flagging honestly that the address-map *unification* (whether the Xtensa DRAM window physically aliases SBUF) is the one genuinely-open question.

> **GOTCHA —** "GPSIMD" with no qualifier is a landmine in this codebase. It has already misled one analysis pass. The two correct, disambiguated names are **"GPSIMD(=Pool) engine"** (the SB2SB datapath, [1.13](../arch/gpsimd-engine.md)) and **"GPSIMD custom-op CPU"** (the 8 Xtensa cores, [11.1](gpsimd-xtensa-layout.md)). Never write bare "GPSIMD" in memory-model prose.

For reimplementation, the contract is:

- The **name-collision result**: two `InstructionType`s (`IT33` Pool-engine `GPSIMDSB2SB` vs `IT53` Xtensa `CustomOp`), why they share a name, and the single string that settles it.
- The **disjoint-namespace test**: the two `rg`-negative grounds that prove neither binary models the other's memory.
- The **unified four-region address map** — per-CPU private window, shared DRAM scratch, the APB aperture, and the SBUF substrate — with each region's single owner.
- The **DMA bridge** (the only contact point) and the one **SPECULATIVE** gap it leaves open.

| | |
|---|---|
| **Pool-alias GPSIMD** | `EngineType Pool(1)` → external `"GPSIMD"`; one op `InstGPSIMDSB2SB` (IT33, opcode `0xBF`) |
| **Xtensa GPSIMD** | 8× Tensilica Xtensa ELF32; reached by `InstCustomOp` (IT53), cpu_id ∈ [0,8) |
| **Decisive string** | `libBIR` verbatim: `"External: GPSIMD Internal: Pool"` |
| **Sole SB2SB encoder** | `CoreV3GenImpl::visitInstGPSIMDSB2SB` @ `0x1359840` (cp310, CONFIRMED) / `0x13597a0` (cp312, ASSERTED — cp312 `libwalrus` not in corpus) |
| **LNC2 gate** | `cmpl $0x2,0x1a4(%rax)` (cores-per-LNC == 2) @ encoder `+0x227`/`+0x561` |
| **Per-CPU window** | `0x84000000 + cpu_id·0x200000` (2 MiB stride) — read off ELF `LOAD` vaddrs |
| **Shared DRAM scratch** | `[0x80000, 0x90000)` (64 KiB), `data_transfer.cpp:160` |
| **APB aperture** | `READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE`, `data_transfer.cpp:240` |
| **Disjointness** | `cpu0.so` ∌ {sbuf,sb2sb,pool,partition,gpsimd}; `libwalrus`/`libBIR` ∌ {SUNDA_APB_BASE,MEM_WINDOW0} |
| **Backing** | D-M11 (Pool/SB2SB ISA), D-AC07 (memory-model reconciliation) |

---

## The Two Views, Stated Precisely

### Purpose

Before reconciling, each view must be stated so precisely that the collision becomes obvious rather than confusing. The two units agree on exactly one thing — the four ASCII letters of their name — and disagree on everything that matters: instruction, engine, address space, and abstraction level.

### View A — GPSIMD as the Pool engine doing SB2SB

The thing [1.13](../arch/gpsimd-engine.md) and D-M11 call "GPSIMD" is the **external name of `EngineType Pool(1)`**. Its entire user-visible ISA surface is one fixed-function machine instruction:

```text
InstGPSIMDSB2SB        InstructionType 33, opcode 0xBF (wire word 0x10BF)
  engine              = EngineType Pool(1), externally displayed as "GPSIMD"
  wire struct         = NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT (64-byte bundle)
  semantics           = cross-core SBUF tile ↔ SBUF tile copy (a pure data move)
  ArithOps            = ArithOpsZeroArithInst (ZERO MACs — not a compute op)
  operands            = BOTH src and dst are SBUF (MemoryLocationType 16); never PSUM
  topology gate       = arch+0x1A4 (cores-per-LNC) must == 2 (LNC2)
  size ceiling        = ≤1024 effective bytes/partition (MaxBytesPerPartition = 0x400)
```

This is a **datapath the compiler emits and hardware executes**. There is no C++ runtime, no `cpu_id`, no DRAM window, no Xtensa core anywhere in this view. The cross-core peer is named by a single scalar SB partition address at `bundle+0x21` (the second `APPair` element of the source access pattern), with a one-bit cross-core-enable at `bundle+0x20` hard-stamped `1`; there is no core-index and no core-count field, because in a 2-core LNC "the other core" is unambiguous. The full instruction reference is [1.13](../arch/gpsimd-engine.md); this page only needs that it is `IT33`, runs on Pool, and lives entirely in SBUF.

### View B — GPSIMD as an Xtensa custom-op CPU

The thing [11.1](gpsimd-xtensa-layout.md) calls "the GPSIMD CPU" is a cluster of **eight Tensilica Xtensa cores**, each running a statically-linked freestanding image that embeds a PyTorch-c10 subset plus the custom-op SDK. It is a *software op-runtime* (a CPU executing C++), not a BIR-ISA datapath:

```text
GPSIMD custom-op CPU cluster                reached by InstCustomOp (IT53)
  images              = libbuiltincustomop_cpu{0..7}.stripped.so (8 ELF32 EXEC)
  machine             = Tensilica Xtensa (readelf: "Tensilica Xtensa Processor")
  private window      = .text/.data/.bss at 0x84000000 + cpu_id*0x200000 (2 MiB stride)
  shared scratch      = DRAM [0x80000, 0x90000) (64 KiB cross-CPU mailbox)
  window aperture     = firmware programs MEM_WINDOW0 to map SUNDA_APB_BASE
  identity            = cpu_id ∈ [0, 8)
```

This view has **no concept of SBUF, partitions, or the Pool engine**. Its memory model is `cpu_id`-keyed private windows plus a shared DRAM mailbox, reached through an APB hardware window. The compiler that schedules an `InstCustomOp` constrains the op's tensors to "SBUF or HBM" but never sees this DRAM-window machinery — it is a sealed runtime/firmware contract below the wire. The full ELF layout is [11.1](gpsimd-xtensa-layout.md).

### Considerations

The two views are at **different abstraction levels for different units**. View A is hardware the compiler models directly, down to the 64-byte bundle. View B is software firmware whose memory model the compiler cannot observe. The "two memory models" never needed to be unified into one address space, because they belong to two different things.

---

## The Decision — Two Different Things

### Purpose

The single question this page exists to answer: *same hardware seen twice, or two different units?* The answer is **two different units**, and it is settled by three binary grounds, each independently sufficient.

### Algorithm

```c
function ReconcileGPSIMD():                       // D-AC07 §1c
    // Ground (i): the EngineType external-alias string in libBIR
    s = rg(libBIR, "External: GPSIMD Internal: Pool")
    if s present:                                 // CONFIRMED — verbatim, 1 hit
        // "GPSIMD" in the BIR/compiler world is DEFINED to be the display
        // name of the internal Pool sequencer. View-A GPSIMD == Pool engine.
        viewA_is_pool = true

    // Ground (ii): two distinct InstructionTypes / encoders / engines
    if InstGPSIMDSB2SB(IT33) != InstCustomOp(IT53):   // distinct IR classes
        // CoreV3GenImpl::visitInstGPSIMDSB2SB (Pool/SB2SB) vs the IT53
        // custom-op path that ships the 8 Xtensa cpu*.so images.
        two_distinct_ops = true                   // NOT one op seen twice

    // Ground (iii): the disjoint-namespace test
    cpu0_knows_sbuf   = rg(cpu0.so, "sbuf|sb2sb|pool|partition|gpsimd")  // → 0 hits
    walrus_knows_apb  = rg(libwalrus, "SUNDA_APB_BASE|MEM_WINDOW0|data_scratch_map")  // → 0
    bir_knows_apb     = rg(libBIR,    "SUNDA_APB_BASE|MEM_WINDOW0")      // → 0 hits
    if !cpu0_knows_sbuf && !walrus_knows_apb && !bir_knows_apb:
        sealed_namespaces = true                  // neither binary models the other

    assert viewA_is_pool && two_distinct_ops && sealed_namespaces
    return "NAME COLLISION — two different units, not one"   // CONFIRMED
```

### The three grounds, re-verified

Every ground below was re-run on the wheel binaries (cp312 layout; the cp310 frame is byte-equivalent for these strings):

```text
(i)  libBIR.so   "ExternalEngineType used as EngineType. External: GPSIMD Internal: Pool"
       → 1 hit (verbatim). The single most decisive fact.                  [CONFIRMED]

(ii) two InstructionTypes / encoders:
       InstGPSIMDSB2SB (IT33) — CoreV3GenImpl::visitInstGPSIMDSB2SB
         nm -DC libwalrus → "neuronxcc::backend::CoreV3GenImpl::visitInstGPSIMDSB2SB"
       InstCustomOp   (IT53) — the path that ships libbuiltincustomop_cpu{0..7}.so
       Two distinct InstructionTypes, two encoders, two engines.           [CONFIRMED]

(iii) disjoint namespaces (rg -a -c):
       cpu0.so      ∋ sbuf|sb2sb|gpsimd|partition          → 0  (CONFIRMED disjoint)
       libwalrus.so ∋ SUNDA_APB_BASE|MEM_WINDOW0|data_scratch_map → 0  (CONFIRMED)
       libBIR.so    ∋ SUNDA_APB_BASE|MEM_WINDOW0           → 0  (CONFIRMED)
```

> **QUIRK —** both names are *locally correct*. The Xtensa cluster is named "GPSIMD" because on Trainium the programmable scalar/SIMD custom-op cores are marketed as "GP-SIMD" / "Penguin". The Pool engine is named "GPSIMD" because the compiler's `EngineType` table literally renames `Pool → GPSIMD`. The apparent conflict is a vocabulary clash inside two sealed namespaces, not a factual one — which is exactly why the disjoint-namespace test (ground iii) resolves it cleanly.

### Considerations

There is no third reading. The `getDefaultEngine` accessor for `InstGPSIMDSB2SB` returns `EngineType 1` (Pool) — `mov eax,1` per D-M11 — and `ArithOps` routes to `ArithOpsZeroArithInst`, confirming View A is a pure mover on the Pool sequencer, never a "run arbitrary SIMD code" surface. User custom code targets the kernel-class ops (IT53–55: `CustomOp`=IT53, `BIRKernel`=IT54, `NKIKernel`=IT55), i.e. View B, never a GPSIMD opcode. The two paths cannot be merged because they are scheduled as separate instructions on separate engines.

---

## The Unified Four-Region Address Map

### Purpose

Because the two "GPSIMD" are different units, the unified map is **not a merge of two overlapping views** — it is a *layered* map in which each region is owned by exactly one agent. Four regions: three on the Xtensa side plus the SBUF substrate that belongs only to the Pool engine.

### Data Tables

| Region | Range / Key | Owner / Accessor | Width | Confidence |
|---|---|---|---|---|
| **(A) per-CPU private window** | `0x84000000 + cpu_id·0x200000` (2 MiB stride; 8 windows tile `0x84000000..0x85000000` = 16 MiB) | the Xtensa custom-op CPU `cpu_id` — its own `.text/.data/.bss/heap/stack`, non-PIC, absolute-linked | ELF32 vaddr | CONFIRMED |
| **(B) shared DRAM scratch / mailbox** | `[0x80000, 0x90000)` (64 KiB), DRAM-relative offset | ALL 8 Xtensa CPUs (cross-CPU mailbox); `data_scratch_map_t` + the `doSyncPhysicalCores` barrier live here | DRAM offset | CONFIRMED |
| **(C) the APB window aperture** | `MEM_WINDOW0_LO == SUNDA_APB_BASE` (numeric `SUNDA_APB_BASE` NOT recoverable) | the Xtensa CPU's DMA/window hardware, programmed by firmware, verified by the SDK | 64-bit UREG | CONFIRMED (predicate) |
| **(D) SBUF (State Buffer)** | partition-addressed (NOT a flat `0x…` addr; per-partition tiles, ≤1024 B/partition) | the **Pool engine** (= View-A GPSIMD) via `InstGPSIMDSB2SB` `bundle+0x21` peer SB partition addr; the Xtensa CPU NEVER addresses SBUF directly | partition × byte | CONFIRMED disjoint |

### Region (A) — the per-CPU private window (CONFIRMED, read off the ELF)

The 2 MiB stride is not inferred — it is read directly off the eight ELF `LOAD` segment virtual addresses:

```text
readelf -l libbuiltincustomop_cpu{0,1,2}.stripped.so  (first LOAD segment):
  cpu0:  LOAD  0x84000000  RWE  filesz 0x8d448 (~578 KiB)
  cpu1:  LOAD  0x84200000  RWE                              Δ = 0x200000 = 2 MiB
  cpu2:  LOAD  0x84400000  RWE                              Δ = 0x200000 = 2 MiB
```

Each image is ELF32 `EXEC`, machine "Tensilica Xtensa Processor", fully absolute-linked at its own base. The eight windows tile a contiguous 16 MiB aperture (`0x84000000`–`0x85000000`); with ~578 KiB used per core, each leaves ~1.4 MiB headroom for heap/stack. This region is **private**: a core sees only its own image. (Full layout: [11.1](gpsimd-xtensa-layout.md).)

### Region (B)/(C) — the shared scratch and its aperture (CONFIRMED asserts)

The cross-CPU coordination region is a 64 KiB DRAM window, and the firmware-programmed aperture onto the APB is asserted by the SDK before it DMAs. Three verbatim `data_transfer.cpp` asserts from `cpu0.so`:

```text
data_transfer.cpp:160   dram_addr >= 0x80000 && dram_addr < 0x90000     // region (B), 64 KiB
data_transfer.cpp:171   cpu_id < 8                                       // 8 Xtensa CPUs
data_transfer.cpp:240   READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE  // region (C) seam
```

Region (A) (`0x84xxxxxx` 32-bit code aperture) and region (B) (a low `0x8xxxx` DRAM-relative offset) are **disjoint address ranges** — a core's own image versus the shared mailbox. The disjointness is arithmetic on two CONFIRMED literals, so it is **STRONG** even though no single string asserts "(A) and (B) do not overlap".

### Region (D) — the SBUF substrate (CONFIRMED disjoint)

SBUF is a **separate namespace entirely**, owned by the Pool engine. It is *partition-addressed*, not byte-flat: an SBUF address is a `(partition, byte-offset)` pair, not a `0x…` immediate. It does **not appear in the Xtensa map at all** — `cpu0.so` has zero SBUF/partition strings (ground iii). View-B's eight CPUs never address SBUF directly; only the Pool engine does, via the `InstGPSIMDSB2SB` peer-partition field. SBUF/PSUM geometry itself (partition count, per-partition byte budget) is owned by [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md), not this page.

> **NOTE —** the map is *layered*, not *flat*. (A), (B), and (D) live in three different addressing schemes (ELF32 vaddr, DRAM-relative offset, partition-tuple). They are not three slices of one linear space — trying to draw them on a single number line is the error this reconciliation prevents.

---

## The Bridge — Where the Two Views Touch

### Purpose

The two units contact the same bytes at exactly **one seam**: a custom-op's tensors. The headline reconciliation sub-question is *"does the GPSIMD access SBUF directly, or only via DMA to its DRAM window?"* — and the answer depends on **which** GPSIMD.

### Algorithm

```c
function CustomOpDataPath():                      // D-AC07 §3
    // COMPILER LEVEL (libBIR verifier — verbatim strings, this build)
    assert tensor.home in {SBUF, HBM}             // "All args to a customop must be
                                                  //  located in SBUF or HBM"
                                                  // "All of a customop's outputs ... SBUF or HBM"
    // ⇒ the compiler constrains every custom-op arg/output to a normal BIR
    //   tensor home. It NEVER sees SUNDA_APB_BASE / MEM_WINDOW0 (ground iii).

    // RUNTIME LEVEL (cpu0.so SDK — verbatim asserts)
    assert MEM_WINDOW0_LO == SUNDA_APB_BASE        // data_transfer.cpp:240
    dma_in(dram_addr in [0x80000,0x90000))         // data_transfer.cpp:160
    t = wrap_as_at_Tensor(windowed_bytes)          // CPU-LOCAL copy
    compute_on_cpu(t)                              // bitonic sort/merge, etc.
    dma_out(t)                                      // back through the same window

    // The Pool/SB2SB GPSIMD takes a DIFFERENT path entirely:
    InstGPSIMDSB2SB: read/write SBUF tiles NATIVELY (both operands MemLocType 16);
                     never touches the DRAM window or SUNDA_APB_BASE.
```

### The seam, stated once

```text
View-A (Pool/SB2SB GPSIMD):   SBUF tile ──Pool engine──▶ peer-core SBUF tile
                              (direct, SBUF-native; no DRAM window, no APB)

View-B (Xtensa GPSIMD):       SBUF/HBM  ──DMA via MEM_WINDOW0 / SUNDA_APB_BASE──▶
                              DRAM staging [0x80000,0x90000) ──▶ Xtensa-local
                              at::Tensor copy ──compute──▶ back out the window
```

The Pool engine **is** an SBUF-native datapath (View A, region D). The Xtensa CPU reaches its tensors only *transitively*, through the DMA window (View B, regions B/C). The compiler-level "SBUF or HBM" constraint and the runtime-level "DRAM window" assert are the **two ends of the same seam** — the compiler names the tensor's home, the firmware DMAs it into the CPU-local window. The two ends never see each other's vocabulary, which is why the bridge is the *only* contact point and not a shared address space.

> **GOTCHA —** the bridge is **one-directional in concept**: it is the custom-op CPU *pulling* SBUF/HBM data into its own window. It is **not** the Pool engine and the Xtensa CPU sharing an address. A reimplementer who models "GPSIMD reads SBUF" must ask *which* GPSIMD: the Pool engine reads SBUF natively; the Xtensa CPU reads a DMA'd copy in a DRAM window. They are different mechanisms.

### Considerations

This seam is the one place the two views touch the same *bytes*, and it is also the only place the reconciliation stays **open** — see the ledger row R16 below.

---

## Confirmed-vs-Speculative Ledger

### Purpose

Per-claim grounding so a reimplementer knows which rows to trust verbatim and which to re-derive. `B-string` = verbatim string in the named binary; `B-symbol` = nm/demangled symbol; `B-byte` = readelf/objdump/xxd. The headline result (R1–R5) is uniformly **CONFIRMED**; the *unification* of the address map is where the **SPECULATIVE** rows sit.

| # | Claim | Grounding | Tag |
|---|---|---|---|
| R1 | View-A GPSIMD == external alias of internal Pool engine | `libBIR` B-string `"External: GPSIMD Internal: Pool"` | **CONFIRMED** |
| R2 | View-B GPSIMD == the 8-core Xtensa custom-op cluster (IT53, `cpu*.so`, `cpu_id`) | `libwalrus` B-symbol + `cpu*.so` B-byte | **CONFIRMED** |
| R3 | The two are DIFFERENT units (name collision), not one HW seen twice | R1 + R2 + disjoint-namespace test | **CONFIRMED** |
| R4 | `cpu0.so` has NO sbuf/sb2sb/pool/partition/gpsimd strings | `cpu0.so` `rg` → 0 | **CONFIRMED** |
| R5 | `libwalrus`/`libBIR` have NO `SUNDA_APB_BASE`/`MEM_WINDOW0` strings | `rg` → 0 | **CONFIRMED** |
| R6 | per-CPU window = `0x84000000 + cpu_id·0x200000` (2 MiB stride) | `readelf -l` cpu0/1/2 `LOAD` vaddrs (Δ=0x200000) | **CONFIRMED** |
| R7 | shared DRAM scratch `[0x80000, 0x90000)` (64 KiB) | `cpu0.so` B-string `data_transfer.cpp:160` | **CONFIRMED** |
| R8 | `cpu_id < 8` (8 Xtensa CPUs) | `cpu0.so` B-string `data_transfer.cpp:171` + 8 image files | **CONFIRMED** |
| R9 | `MEM_WINDOW0_LO == SUNDA_APB_BASE` aperture | `cpu0.so` B-string `data_transfer.cpp:240` | **CONFIRMED** (predicate) |
| R10 | `InstGPSIMDSB2SB` operands BOTH in SBUF (MemLocType 16) | `libBIR`/`libwalrus` (D-M11) | **CONFIRMED** |
| R11 | `InstGPSIMDSB2SB` LNC2 gate `arch+0x1A4 == 2` | `libwalrus` disasm `cmpl $0x2,0x1a4(%rax)` (encoder body) | **CONFIRMED** |
| R12 | custom-op tensors constrained to SBUF or HBM (compiler) | `libwalrus` B-string `"All args to a customop must be located in SBUF or HBM"` | **CONFIRMED** |
| R13 | Xtensa CPU reaches tensors via DMA through `MEM_WINDOW0` into the DRAM window; computes on local copy | R7 + R9 + R12 synthesis | **STRONG** |
| R14 | region (A) and region (B) are disjoint address spaces | arithmetic on R6 / R7 literals | **STRONG** |
| R15 | numeric value of `SUNDA_APB_BASE` | not recoverable (Xtensa L32R literal pool) | **SPECULATIVE** |
| R16 | whether DRAM window `[0x80000,0x90000)` is HBM / dedicated DRAM / an alias of SBUF backing | no string ties the window to SBUF or HBM | **SPECULATIVE** |
| R17 | the per-CPU DMA-engine identity (SW-DGE vs DMA ring) on the Xtensa side | not string-resident on the cpu side | **INFERRED** |
| R18 | `data_scratch_map_t` exact field layout | no field strings | **INFERRED** |

### The contradiction, explicitly resolved

```text
APPARENT CONTRADICTION (the reason this page exists):
  View-B says  "GPSIMD = an Xtensa CPU with an APB-mapped DRAM window."
  View-A says  "GPSIMD = a fixed-function Pool engine doing SBUF↔SBUF copies."
  Read naively, these describe incompatible hardware with incompatible memory.

RESOLUTION (binary-grounded):
  They are not in contradiction because they name TWO DIFFERENT UNITS.
  "GPSIMD" is overloaded: View-B = the product/firmware name for the custom-op
  scalar-CPU cluster; View-A = the COMPILER's EngineType external alias (Pool →
  "GPSIMD", R1). The libBIR string + two distinct InstructionTypes (IT53 vs IT33)
  + the disjoint-namespace test (R4/R5) prove the two memory models belong to two
  different things and never needed to be unified into one address space.
```

> **NOTE —** the **residual tension** is row R16, and only R16. The SBUF/HBM ↔ DRAM-window physical relationship (§"The Bridge") is the one place the two views touch the same bytes, and these binaries do not say whether that is a cross-memory DMA or an address re-view of one store. The STRONG reading — from the "SBUF or HBM" compiler constraint plus a distinct APB-windowed DRAM at runtime — is that the custom-op CPU has its own DMA-fed DRAM staging region distinct from SBUF, and the DMA crosses memories. But "the window aliases SBUF" is not *excluded* by these binaries. Closing R16 needs an HBM/DRAM address-map task or the Sunda APB register docs; it is correctly left SPECULATIVE rather than guessed.

---

## Related Components

| Name | Relationship |
|---|---|
| [GPSIMD Engine — Pool-Alias SB2SB Mover](../arch/gpsimd-engine.md) | View-A: the full `InstGPSIMDSB2SB` (IT33) ISA — wire bundle, LNC2 gate, cross-core addressing |
| [The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md) | View-B: the 8 Xtensa images, the `0x84000000+id·0x200000` aperture, the DRAM scratch |
| [Lowering On-Chip Collectives](../walrus/local-collectives.md) | mints `InstGPSIMDSB2SB` from on-chip `SendRecv`; the `isCompatible`/`ebpp` engine pick |
| [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) | owns region (D)'s partition geometry — the SBUF address space the Pool engine drives |

## Cross-References

- [GPSIMD Engine](../arch/gpsimd-engine.md) — the Pool-alias SB2SB datapath in full; this page's View A
- [GPSIMD CPUs Xtensa Layout](gpsimd-xtensa-layout.md) — the 8-core Xtensa cluster; this page's View B
- [Lowering On-Chip Collectives](../walrus/local-collectives.md) — where `GPSIMDSB2SB` is born and the GPSIMD-vs-DMA decision is made
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — region (D) geometry, owned elsewhere
- [Custom-Op Wire Byte-Layout (0x85 / 0x86)](customop-wire-layout.md) — the `InstCustomOp` (IT53) wire that reaches View B
- [FindCustomOpData Staging](findcustomopdata-staging.md) — how the 8 Xtensa `cpu*.so` images are staged and bound
