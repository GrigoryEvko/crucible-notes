# Allocator Drivers — ColoringAllocator Wiring and Loop-Aware Linearize

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds). Everything here lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; the `0x5e9020`–`0x62d650` range is `.plt` thunks, so every body address cited here is the real high body (`0x9x…`/`0xax…`/`0xbx…`), never its thunk. `.data` carries a +0x400000 delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

The walrus backend places every tensor with **graph coloring**, not linear scan, at default optimization. The colorer itself is Chaitin–Briggs — simplify, select, spill, repeat — but the *driver* that runs it is what this page documents: the wiring layer that decides which memory space is colored first, how spilled tensors cascade down the memory hierarchy, and how the loop-aware variant flattens loop nests so a value live across an iteration gets one allocation. This is the seam between the pass pipeline ([`pass-pipeline-optlevels`](pass-pipeline-optlevels.md)) and the per-space colorers (the `*_Allocator` classes, planned as separate pages).

There are two driver families. The first is `neuronxcc::backend::ColoringAllocator` (ctor `@0x9870c0`), **one class registered under eight pass names**. The registry collapse was established in [`backendpass-registry`](backendpass-registry.md): the eight names are the surplus of a single class whose constructor takes a `(Type, is_post_link, MemoryAddressSpace, MemoryAddressSpace)` discriminator tuple. This page enumerates *what each of the eight does* — which space it colors, whether it runs pre- or post-link, and the four constructor invariants the binary asserts. The default pre-codegen pipeline `sub_80A6E0` wires four of them into a strict cascade `PSUM → SB → DRAM → DRAM-shared`, each followed by its own DMA-optimization and address-rotation re-tuning; the low-level builder `sub_805870` wires the two `post_lnk` variants plus the REG allocator after `bir_linker`.

The second family is `ColoringAllocatorWithLoop` (ctor `@0xa84fc0`), a **distinct** class selected only at optlevel 6. Its `Rep::allocate` driver runs `PSUM → SB` itself over a `LinearizedFunction` produced by a loop-flattening linearize (`flattern_loop @0xa89a60`) that inlines loop bodies into one instruction-point stream. The page closes with the fact most likely to be misread from the binary's own output: the per-space `"best-of-n"` phrasing in the log strings is **not** a fixed-count best-of-N search — it is a **spill-iteration fixpoint** that re-colors until no tensor needs spilling. The SB colorer's restart loop shows the mechanism.

For reimplementation, the contract is:

- The eight `ColoringAllocator` registrations — the `(Type, post_lnk, space)` matrix and what each colors.
- The `PSUM → SB → DRAM → DRAM-shared` cascade order and *why* the hierarchy forces it.
- The loop-aware linearize: how `flattern_loop` expands `InstLoop` bodies inline so cross-iteration live ranges are visible to one interference graph.
- The spill-fixpoint loop — the correction to the "best-of-n" reading — and its termination condition.

| | |
|---|---|
| **Plain driver class** | `neuronxcc::backend::ColoringAllocator` ctor `0x9870c0`, `run` `0x985100` |
| **Registrations** | **8** factory `_M_invoke` lambdas, one class (see matrix) |
| **Loop-aware driver class** | `ColoringAllocatorWithLoop` ctor `0xa84fc0`, `run` `0xa84bb0`, driver `Rep::allocate` `0xa87030` |
| **Pre-codegen builder** | `sub_80A6E0` (`0x80a6e0`) — optlevel 1/2/3, cascade `PSUM→SB→DRAM→DRAM-shared` |
| **Loop-aware builder** | `sub_807EF0` (`0x807ef0`) — optlevel 6 |
| **Post-link builder** | `sub_805870` (`0x805870`) — `dram_post_lnk`, `dram_shared_post_lnk`, `reg` |
| **Cascade order** | PSUM (frozen first) → SB → DRAM (spill target) → DRAM-shared (cross-core) |
| **Linearize** | `Rep::linearize` `0xa89e70` → `flattern_loop` `0xa89a60` (opcode 105 = `InstLoop`) |
| **Coloring core** | per-space `*_Allocator::allocate`, Chaitin–Briggs + **spill fixpoint** (not best-of-N) |
| **Source path** | `neuronxcc/walrus/coloring_allocator/src/coloring_allocator.cpp` (asserts at :24/:25/:30/:31) |

---

## The Eight ColoringAllocator Registrations

### Purpose

The four pass names the pipeline emits most visibly — `coloring_allocator_{psum,sb,dram,dram_shared}` — are not four classes. They are four of **eight** registrations of one class. [`backendpass-registry`](backendpass-registry.md) proved the registry maps a pass-NAME string to a factory `std::function<unique_ptr<BackendPass>(PassOptions const&)>`; the eight `coloring_allocator_*` names (the ninth, `coloring_allocator_with_loop`, is the distinct loop-aware class) all resolve to the same `ColoringAllocator` constructor with different discriminator arguments. This section enumerates the matrix and what each row colors.

### Entry Point

Each pass name is built inline by the pipeline builder and resolved through the registry to a factory whose `_M_invoke` constructs the parameterized allocator:

```text
sub_80A6E0 / sub_807EF0 (pipeline builder)
  └─ build name string "coloring_allocator_<X>"        (helper sub_7FAE40)
       └─ BackendPassManager::addModParallelPass(name)  0x1748bd0
            └─ lookupGenerator(name)                     0x1744cb0
                 └─ GeneratorRegistration::getGenerator  0x1735740   → factory std::function
                      └─ factory _M_invoke               (per-name; table below)
                           ├─ tc_new(48)  BackendPass object
                           └─ ColoringAllocator::ctor    0x9870c0   (Type, post_lnk, space, space2)
```

An unregistered name aborts with `"Backend pass: <n> is not registered!"`.

*Anchors: the resolution chain matches [`backendpass-registry`](backendpass-registry.md); the ctor call site is read from each factory's disassembly.*

### The constructor matrix

The single point of parameterization is the constructor `@0x9870c0`:

```c
ColoringAllocator::ColoringAllocator(
    PassOptions const&,            // config bundle, shared by all factories
    Type        type,              // edx: 0=SB 1=PSUM 2=DRAM 3=REG
    bool        is_post_link,      // cl/ecx
    MemoryAddressSpace from_space, // r8d: 0=Local 1=Shared 2=Debug
    MemoryAddressSpace to_space);  // r9d: 0=Local in ALL eight registrations
```

Each factory `_M_invoke` `tc_new`s the `BackendPass`, then calls the ctor with constant immediates. The immediates below are the register loads preceding each factory's `call ColoringAllocatorC1…`:

| Pass name (registry) | Factory `_M_invoke` | `edx` Type | `ecx` post_lnk | `r8d` from_space | Colors |
|---|---|---|---|---|---|
| `coloring_allocator_sb` | `0x98a6f0` | `xor`→0 SB | `xor`→0 false | `xor`→0 Local | SBUF working tensors (`RepT<SB_Allocator>`) |
| `coloring_allocator_psum` | `0x98a490` | `mov 1` PSUM | `xor`→0 false | `xor`→0 Local | PSUM matmul accumulators (`RepT<PSUM_Allocator>`) |
| `coloring_allocator_dram` | `0x98a5c0` | `mov 2` DRAM | `xor`→0 false | `xor`→0 Local | per-core DRAM spill / large tensors (`RepT<DRAM_Allocator>`) |
| `coloring_allocator_reg` | `0x98a0f0` | `mov 3` REG | `xor`→0 false | `xor`→0 Local | engine registers (`RepT<REG_Allocator>`, the L33 register allocator) |
| `coloring_allocator_dram_shared` | `0x989e80` | `mov 2` DRAM | `xor`→0 false | `mov 1` Shared | cross-core shared-DRAM lifetimes |
| `coloring_allocator_dram_debug` | `0x98a360` | `mov 2` DRAM | `xor`→0 false | `mov 2` Debug | device-print / debug buffers |
| `coloring_allocator_dram_post_lnk` | `0x989fc0` | `mov 2` DRAM | `mov 1` TRUE | `xor`→0 Local | DRAM re-laid across the LINKED image |
| `coloring_allocator_dram_shared_post_lnk` | `0x98a220` | `mov 2` DRAM | `mov 1` TRUE | `mov 1` Shared | shared-DRAM re-laid across the LINKED image |

*Anchors: the `edx`/`ecx`/`r8d` immediates come from each factory's disassembly — e.g. psum @ `0x98a4ae` is `xor r8d,r8d; xor ecx,ecx; mov edx,1`, followed by the ctor call @ `0x98a4c1`.*

> **QUIRK —** the fourth constructor argument `to_space` (`r9d`) is `xor r9d,r9d` = 0/Local in **all eight** factories. It is a second `MemoryAddressSpace` the ctor accepts but the registered factories never exercise; its role (a secondary space, plausibly a spill source) is [INFERRED], not observable from these call sites.

### The four ctor-enforced invariants

The constructor dispatches on `Type` to install one of four `RepT<*_Allocator>` vtables into a `tc_new(696)` `Rep` object, and guards the `(post_lnk, space)` combination with four `NeuronAssertion`s. The assert strings and `coloring_allocator.cpp` line numbers were read verbatim from the ctor decompile `@0x9870c0`:

| Source line | Asserted condition | Meaning |
|---|---|---|
| `coloring_allocator.cpp:24` | `is_post_link == false` (PSUM path) | PSUM is never a post-link allocator |
| `coloring_allocator.cpp:25` | `from_addr_space == Local` (PSUM path) | PSUM is always core-local |
| `coloring_allocator.cpp:30` | `is_post_link == false` (SB path) | SB is never a post-link allocator |
| `coloring_allocator.cpp:31` | `from_addr_space == Local` (SB path) | SB is always core-local (SBUF) |

These match the matrix exactly: only DRAM-family rows carry `post_lnk=true` or a non-Local `from_space`.

*Anchors: four `logging::NeuronAssertion<ErrorCode>` sites at error codes 508–511 in the ctor decompile, each preceded by its `.cpp:NN` path string.*

> **NOTE —** the `Rep` object is `tc_new(696)` = `0x2B8` bytes; the `RepT<SB_Allocator>` / `RepT<PSUM_Allocator>` / `RepT<DRAM_Allocator>` / `RepT<REG_Allocator>` vtable is installed at `Rep+0`. The vptr stored is the `_ZTV` symbol **+ 0x10** (past the offset-to-top / typeinfo header), not the symbol itself.

---

## The PSUM → SB → DRAM → DRAM-Shared Cascade

### Purpose

At optlevel 1/2/3 (the default "coloring" plane) the builder `sub_80A6E0` emits the four base allocators in a fixed order interleaved with per-space re-tuning passes. The order is **not** arbitrary: it is dictated by the memory hierarchy and by the fact that one space's allocator produces constraints (or spill targets) the next space's allocator consumes.

### Entry Point

`sub_80A6E0` `@0x80a6e0` is the real builder body — it directly drives `BackendPassManager::addModParallelPass` / `addSubgraphParallelPass` / `addCoreParallelPass` (≈139 add-call sites, ≈127 named pass steps). The cascade and its inter-allocator passes:

```text
sub_80A6E0  (optlevel 1/2/3, --allocator != "lsa")
  … runtime_memory_reservation …
  ┌── PSUM block ────────────────────────────────────────────
  │  coloring_allocator_psum          Type=1, Local        *** colored first, frozen ***
  │  dma_optimization_psum            DMA tuned into the PSUM placement
  │  address_rotation_psum            tile-layout rotation on PSUM
  │  [memory_analysis_after_address_rotation_psum]
  ├── SB block ──────────────────────────────────────────────
  │  coloring_allocator_sb            Type=0, Local
  │  [memory_analysis_after_coloring_allocator_sb]
  │  dma_optimization_sb  /  address_rotation_sb  + memory_analysis recomputes
  │  non_ssa_legalization             *** POST PSUM+SB: re-legalize SSA-clones to fixed slots ***
  ├── DRAM block ────────────────────────────────────────────
  │  coloring_allocator_dram          Type=2, Local        gate: !--disable-dram-allocation
  │  address_rotation_dram            gate: !--disable-dram-rotation
  │  dynamic_dma_cleanup / build_fdeps / anti_dependency_analyzer
  └── DRAM-shared block ─────────────────────────────────────
     coloring_allocator_dram_shared   Type=2, Shared       (cross-core; gated paths)
     sync_shared_allocations / extend_shared_lifetimes / lower_local_collectives
     coloring_allocator_dram_debug    Type=2, Debug        (device-print buffers)
```

The relative ordering PSUM → SB → DRAM → DRAM-shared is exact; the realized step numbers drift because a `birverifier`/`lnc_verifier` is interposed after most passes when `enableVerifier` is set.

*Anchors: cascade order and inter-allocator passes read from the `sub_80A6E0` builder; gate predicates from the surrounding `if()` bodies.*

### Algorithm — the per-space re-tuning cycle

Every space allocator is immediately followed by its own DMA-optimization and address-rotation, because each allocator has just fixed *physical* addresses for that space and the DMA and tile layout must be re-tuned to that placement before the next space is colored:

```c
function ColorOneSpace(space):               // the PSUM/SB/DRAM repeat in sub_80A6E0
    emit "coloring_allocator_<space>"        // place: assign physical addresses for space
    emit "dma_optimization_<space>"          // DMAOptimization: re-route DMA into the placement
    emit "address_rotation_<space>"          // AddressRotation: rotate tile layout (i mod N)
    if "<…>" in --memory-analysis-after set:  // runtime membership test sub_7FC770
        emit "memory_analysis_after_<space>" // re-measure HWM / footprint for next space
```

> **GOTCHA —** the DRAM block has **no** `dma_optimization_dram`. DRAM placement is followed by `dynamic_dma_cleanup` / `dynamic_dma_scan` + `build_fdeps` + `remove_redundancies` instead — the DMA into DRAM is handled by the dynamic-DMA machinery, not the space-scoped `DMAOptimization` used for PSUM/SB. A reimplementer that mechanically appends `dma_optimization_<space>` after every allocator will emit a pass that does not exist for DRAM.

### Considerations — why this order

The cascade follows the memory hierarchy, and each step freezes constraints for the next:

- **PSUM first.** PSUM (`MemoryType=32`, the matmul **accumulator** space, a small bank-addressed window) is the most constrained and is RMW-pinned by accumulation groups. It is colored first and frozen so its banks become immovable constraints for everything downstream. `psum_legalization` precedes it.
- **SB second.** SBUF working tensors (`MemoryType=16`, partition-addressed) are colored with the PSUM accumulators already immovable. SB spill decisions target DRAM, so SB must precede DRAM. After SB+PSUM are placed, `non_ssa_legalization` re-legalizes the SSA-clones inserted before allocation to the now-fixed physical SB/PSUM slots (the `sb_allocated` attribute flips true) — see [`reorder-nonssa-presched`](reorder-nonssa-presched.md).
- **DRAM third.** DRAM (`MemoryType=8`) is the spill / large-tensor backing store. It must follow SB so the SB allocator's spill decisions have a DRAM target.
- **DRAM-shared last.** Cross-core shared-DRAM lifetimes need every per-core DRAM placement done first, then coordination across NeuronCores via `extend_shared_lifetimes` / `sync_shared_allocations` + `anti_dependency_analyzer_post_shared_dram`.

The geometry of PSUM banks and SB partition rectangles is detailed in [`sbuf-psum-geometry`](../arch/sbuf-psum-geometry.md); the tensor / memory-location model the colorers operate over is [`memory-location`](../bir/memory-location.md) and [`tensor-buffer-node`](../penguin/tensor-buffer-node.md).

---

## The Post-Link Allocator Wiring

### Purpose

The two `post_lnk` registrations and the `reg` allocator do not run in the pre-codegen pipeline. They run in the low-level builder `sub_805870` (`0x805870`), which always executes after codegen, right after `bir_linker` links the multi-LNC modules — so DRAM is re-allocated across the *linked* address space.

### Entry Point

```text
sub_805870  (low-level, always run, after bir_linker)
  report_stats
  coloring_allocator_dram_post_lnk          Type=2, Local,  post_lnk=1
  coloring_allocator_dram_shared_post_lnk   Type=2, Shared, post_lnk=1
  sync_shared_allocations
  coloring_allocator_dram_post_lnk
  anti_dependency_analyzer_post_lnk / separate_load_and_compute_post_ada
  … engine lowering …  lower_ap
  coloring_allocator_reg                    Type=3 REG  (the L33 register allocator)
  vnc_remote_addr_map …
```

That emission order is read directly from `sub_805870`. It is exactly why the constructor permits `post_lnk=true` *only* for the DRAM family (PSUM/SB asserted pre-link, §invariants): PSUM and SB are core-local and fixed pre-link, while DRAM — including cross-core Shared — must be re-laid across the linked image.

---

## The Loop-Aware Driver — ColoringAllocatorWithLoop

### Purpose

At optlevel 6 the builder `sub_807EF0` wires a **distinct** class, `ColoringAllocatorWithLoop`, registered as `coloring_allocator_with_loop`. It expresses the same Chaitin–Briggs algorithm as the plain family but adds a loop-flattening linearization so live ranges that cross loop bodies — including values live across the loop back-edge — are visible to a single interference graph. Its nested colorers operate on a `LinearizedFunction*`, not a `bir::Function*` + CFG.

### Entry Point

```text
coloring_allocator_with_loop  (optlevel 6 only, --allocator != "lsa")
  ColoringAllocatorWithLoop::run(bir::Module&)            0xa84bb0
    └─ Rep::allocate(Module&)                             0xa87f70   // per-Function loop
         └─ Rep::allocate(Module*, Function*)             0xa87030   *** THE DRIVER ***
              ├─ lf  = Rep::linearize(Function)           0xa89e70   // flattern_loop: bodies inlined
              ├─ PSUM_Allocator::allocate(lf)             0xad7140   *** PSUM FIRST, frozen ***
              ├─ SB_Allocator::allocate(lf')              0xa95310   // consumes PSUM-mutated lf
              ├─ Rep::indice_legalization(lf'', Function) 0xa8eb00   // AP index fixup post-place
              └─ Rep::free_linearized_function(lf)        0xa89b90
```

### Algorithm

```c
function Rep_allocate(module, function):              // sub_0xa87030
    lf  = Rep::linearize(function)                    // 0xa89e70 — flattern_loop, def/use check
    psum = PSUM_Allocator(opts, module, function)     // ctor 0xad9970
    lf  = psum.allocate(lf)                           // 0xad7140  *** PSUM colored first ***
    sb   = SB_Allocator(opts, module, function)       // ctor 0xa97750
    lf  = sb.allocate(lf)                             // 0xa95310  *** SB consumes PSUM-mutated lf ***
    Rep::indice_legalization(lf, function)            // 0xa8eb00  AP indices now physical
    Rep::free_linearized_function(lf)                 // 0xa89b90
    // DRAM, REG, shared-DRAM are downstream SEPARATE passes, NOT this driver
```

*Anchors: `Rep::allocate(Module*,Function*)` decompile — `linearize` (line 703) → `PSUM_Allocator::allocate` (705) → `SB_Allocator::allocate` (747) → `indice_legalization` (882) → `free_linearized_function` (883).*

The driver's scope is narrower than the plain cascade's: it colors **only PSUM and SB**, in that order, sharing one `LinearizedFunction`. DRAM is left to the downstream `coloring_allocator_{dram,…,dram_debug}` passes in plain-family style, and REG to `coloring_allocator_reg` in the low-level builder.

> **GOTCHA —** the with-loop translation unit *contains* sibling `DRAM_Allocator` colorers (`@0xafa350`), so a symbol listing suggests the driver handles DRAM too. `Rep::allocate` never invokes them — they are the loop-aware DRAM colorer reached only when the TU is driven by the standalone `bin/coloring_allocator_with_loop` harness.

### Considerations

PSUM is colored first and frozen for the same reason as the plain cascade (its RMW-pinned accumulator banks become immovable constraints); SB is colored second over the *same* `LinearizedFunction` that `PSUM_Allocator::allocate` mutated, so loop-crossing live ranges are consistent between the two passes. The `PassOptions` are snapshot-copied into the `Rep` by `Rep::Rep` (`@0xa886b0`); the `Rep` holds no persistent colorer members — `PSUM_Allocator` / `SB_Allocator` are stack-constructed per `Function`.

---

## The Loop-Flattening Linearize

### Purpose

`Rep::linearize` builds a `LinearizedFunction` — a chain of instruction-point nodes the colorer numbers for liveness — and `flattern_loop` is its loop-aware core. The plain-family `SB_Allocator::linearize` / `PSUM_Allocator::linearize` (`@0x9cefe0` / `@0xa30b10`) treat a loop as one opaque instruction: a tensor produced before a loop and consumed inside it has its live range conservatively span the whole loop as a black box. `flattern_loop` instead inlines the loop body's instruction-points into the single linear stream, giving the colorer the true intra-loop live ranges.

### Entry Point

```text
Rep::linearize(bir::Function*)                  0xa89e70
  ├─ per BasicBlock: tc_new(40) LinearizedBlock
  │    └─ flattern_loop(BasicBlock&, LinearizedBlock*, &tail)   0xa89a60
  ├─ LinearizedFunction = tc_new(40); first BB stored
  └─ "  linearize and check"  — static def/use validation over operand APs
```

Source: `walrus/walrus_loop_flow/coloring_allocator_with_loop/src/linearize_with_loop.cpp` (asserts cite this path).

### Algorithm

```c
function flattern_loop(bb, lin_block, &tail):        // sub_0xa89a60
    for inst in bb.instructions:                      // intrusive ilist
        node = tc_new(40)                             // LinearizedInstruction: +0 = bir::Instruction*
        append node to *tail                          // chain position IS the instruction-point;
                                                      //   dense id assigned later by renumber_locations
        if *(int*)(inst + 0x50) == 105:               // bir::InstLoop opcode (Loop=105)
            body = bir::BasicBlockHolder::blocks(inst - 96)
            for child_bb in body:                     // recurse INTO the loop body
                flattern_loop(child_bb, lin_block, &tail)   // splice body points INLINE, nested loops recurse
```

*Anchors: `flattern_loop` body @ `0xa89a60` — `tc_new(40)` (lines 22/32), the opcode test `*(_DWORD *)(v3 + 80) == 105` (line 38), `bir::BasicBlockHolder::blocks((…)(v3 - 96))` (line 40), and the recursive `flattern_loop(...)` calls (lines 48/55). The `llvm::isa<bir::InstLoop>` cast identifies the opcode-105 node as the loop instruction.*

> **QUIRK —** there is **no** explicit integer instruction-point index stored by `flattern_loop`. The `LinearizedInstruction` is 40 bytes: `+0` is the `bir::Instruction*`, `+8` is the doubly-linked chain pointer (with the `LinearizedBlock` back-pointer packed in the high lane). The dense integer instruction-point id is assigned **later**, by each `*_Allocator::renumber_locations`, by walking the chain in order. A reimplementer who expects `linearize` to hand back numbered points will not find them; the number is the chain position. The struct layout is derived from `flattern_loop`'s own accesses rather than recovered as a type.

### The static def/use check

In the same pass, `linearize` walks every instruction's input and output `AccessPattern` lists, resolves each to a `MemoryLocationSet`, and maintains a defined-set and a used-set (`llvm::DenseSet<MemoryLocationSet*>`). On a mismatch it emits `"Bad input file - failed static checks."`, `"<N> names were undefined at use point"`, `"<N> names were unused at definition point"`. Output tensors are skipped via `bir::isTensorKindOutput()`. This is the SSA-style use-before-def / dead-def validation the liveness analysis assumes.

*Anchors: the diagnostic strings and the `DenseSet` logic in `Rep::linearize`.*

---

## The Spill Fixpoint

### Purpose

Each per-space colorer runs a **spill-iteration fixpoint**: color the function, and if `select` reports any node that could not be placed, insert spill code and **re-color the spilled-and-reloaded function**, repeating until the spill set is empty. There is no fixed iteration count — termination is "no more spills."

> **GOTCHA —** the colorer logs `"      best-of-n loop, heuristic = "` on every attempt, which reads as a bounded best-of-N search (run N attempts, keep the best). It is a per-attempt heuristic label, not a count; see [§Why the loop is a fixpoint](#why-the-loop-is-a-fixpoint) below.

### Algorithm

Transcribed from `SB_Allocator::allocate(LinearizedFunction*)` `@0xa95310` (the with-loop colorer; the plain-family `@0x9d1b10` has the same structure):

```c
function SB_Allocator_allocate(lf):                  // sub_0xa95310
    log "  allocating SB"
    renumber_locations(lf)                            // dense id per MemoryLocationSet
    memloc_split(lf)                                  // live-range split of multi-range memlocs

  restart:                                            // LABEL_79 (line 798) — THE FIXPOINT HEAD
    renumber_locations(lf)
    find_partners(lf)                                 // coalescing / co-allocation candidates
    find_first_defs(lf); find_last_uses(lf); find_loads(lf)
    live_range(lf)                                    // liveness over instruction points
    build(lf)                                         // interference graph
    find_costs(lf)                                    // spill-cost annotation
    log "      best-of-n loop, heuristic = "          // <-- the misleading string
    stack    = simplify(lf)                           // Chaitin simplify  → NodeStack*
    placement= select(lf, stack, &score)             // 2-D place; sets need-spill flag at placement+8
    log "        SB score = " score

    if placement.need_spill == 0:                     // *(_DWORD*)(placement+2) == 0  → no spill
        log "      best SB heuristic = "
        commit(lf); return                            // LABEL_123 (line 1139): coloring succeeded

    // --- spill iteration: a tensor could not be placed ---
    create_eintervals(lf, spillset)                   // placement intervals for the spill set
    lf = insert_spill_code(lf, spillset)              // mint reload/store DMA memlocs → NEW lf
    goto restart                                       // re-color the spilled function (FIXPOINT)
```

*Anchors: `SB_Allocator::allocate` decompile — `simplify` (line 1032) → `select` (1033) → the branch `if (!*((_DWORD *)v213 + 2))` on the `select` result `v213` (line 1066, commit path `v101=1`, `LABEL_123`) versus the spill path `create_eintervals` (line 1083) → `insert_spill_code` returning a new `inserted` linearized function (line 1115). The fixpoint head `LABEL_79` sits at line 798; the only edge that re-enters it threads through the spill path, and no bounded counter gates it.*

### Why the loop is a fixpoint

Two structural facts distinguish it from a fixed-N search:

1. **The termination condition is data, not a counter.** The branch out of the loop is `if (select.need_spill == 0)` — i.e. the colorer leaves the loop the moment an attempt places every tensor. There is no `for (i = 0; i < N; i++)` and no "best score so far" register compared across a fixed number of trials.
2. **Each iteration operates on a strictly larger function.** `insert_spill_code` returns a **new** `LinearizedFunction` with reload/store memlocs minted in; the next iteration colors *that*, not a re-perturbed copy of the original. A best-of-N search re-runs the *same* problem with a different seed; this re-runs a *different, spill-augmented* problem each time. That is the definition of a spill fixpoint.

Two further strings settle it. The colorer logs `"Number of iterations in SB spills do loop: "` at the loop exit — it **counts** the iterations it took rather than bounding them, which is exactly what a fixpoint does and a best-of-N does not. And the only ceiling on the loop is a **wall-clock safety valve**, not an iteration count: if the colorer runs longer than ~2700 seconds it aborts with `"GCA is taking too long"` and `exit(2)`. A timeout is a watchdog, not a search budget.

The colorer reports the work it did: `"spilling from SB cost about <N> cycles"`, `"number of tensors spilled from SB = "`, `"total size of spilled tensors = <N> bytes/partition"`. Failure (a tensor that cannot be placed even after spilling) is `"couldn't allocate every tensor in SB and spilling can't help"`.

*Anchors: strings in the colorer body; the iteration-count log and the `exit(2)` timeout sit at the loop tail.*

> **NOTE —** the PSUM colorer `PSUM_Allocator::allocate` `@0xad7140` has the identical phase order, recovered from its disasm call targets: `"  allocating PSUM"` → `renumber_locations` → `live_range` → `build` → `find_costs` → `"      best-of-n loop, heuristic = "` → `simplify` (`@0xad7ce6`) → `select` (`@0xad7d09`) → `insert_spill_code` (`@0xad8a25`) → `"spilling from PSUM cost about "`. The two colorers differ only in geometry (PSUM = banks; SB = partition × byte rectangles) and a few special cases (PSUM accumulation banks; SB pinning).

---

## Allocator-Family Selection

Two independent selectors decide which allocator family is wired:

| Selector | Mechanism | Result |
|---|---|---|
| **optlevel → builder** | `run_backend_driver` reads optlevel, picks the builder | `sub_806F80` (opt0) → LSA only; `sub_80A6E0` (opt1/2/3) → plain coloring cascade; `sub_807EF0` (opt6) → `coloring_allocator_with_loop`; `sub_80D9D0` (opt7) → SMT allocation; `sub_809580` (opt8) → pure LSA |
| **`--allocator` string** | `std::string::compare("lsa")` `@0x80b089` inside the builder | `== 0` → wire `linear_scan_allocator`, skip the coloring sub-pipeline; `!= 0` (default / "coloring") → wire the four-space cascade |

The DRAM step has a further gate: even on the coloring path, `coloring_allocator_dram` is skipped under `--disable-dram-allocation`, and `address_rotation_dram` under `--disable-dram-rotation`. `--smt-allocation` forces optlevel 7. *Anchors: builder/optlevel mapping and the `compare` site; gate predicates from the surrounding `if()` bodies.* The detailed optlevel-plane mapping is in [`pass-pipeline-optlevels`](pass-pipeline-optlevels.md).

---

## Infrastructure Functions

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ColoringAllocator::ColoringAllocator(PassOptions&,Type,bool,AddrSpace,AddrSpace)` | `0x9870c0` | the 8-way parameterized ctor; 4 invariant asserts | CERTAIN |
| `ColoringAllocator::run(bir::Module&)` | `0x985100` | plain-family pass entry | CERTAIN |
| `ColoringAllocatorWithLoop` ctor / `run` | `0xa84fc0` / `0xa84bb0` | loop-aware class (distinct) | CERTAIN |
| `Rep::allocate(Module*,Function*)` | `0xa87030` | the loop-aware driver (PSUM→SB) | CERTAIN |
| `Rep::linearize(Function*)` | `0xa89e70` | builds `LinearizedFunction` + def/use check | CERTAIN |
| `flattern_loop(BasicBlock&,LinearizedBlock*,&tail)` | `0xa89a60` | opcode-105 loop-body inlining | CERTAIN |
| `Rep::indice_legalization(LinearizedFunction*,Function*)` | `0xa8eb00` | AP index fixup post-placement | CERTAIN |
| `SB_Allocator::allocate(LinearizedFunction*)` | `0xa95310` | with-loop SB colorer + spill fixpoint | CERTAIN |
| `PSUM_Allocator::allocate(LinearizedFunction*)` | `0xad7140` | with-loop PSUM colorer + spill fixpoint | CERTAIN |
| `SB_Allocator::insert_spill_code` | `0xac87d0` | mints reload/store memlocs → new `lf` | CERTAIN |
| pre-codegen builder `sub_80A6E0` | `0x80a6e0` | optlevel 1/2/3 cascade | CERTAIN |
| loop-aware builder `sub_807EF0` | `0x807ef0` | optlevel 6 | CERTAIN |
| post-link builder `sub_805870` | `0x805870` | `dram_post_lnk` / `dram_shared_post_lnk` / `reg` | CERTAIN |
| `BackendPassManager::lookupGenerator(string&)` | `0x1744cb0` | name → factory resolution | CERTAIN |
| factory `_M_invoke` (sb/psum/dram/reg/…) | see matrix | construct parameterized `ColoringAllocator` | CERTAIN |

> **Gaps.** The per-space colorer *bodies* (`live_range` / `build` / `find_costs` / `simplify` / `select` / `insert_spill_code` internals) are out of scope here — named and addressed, not transcribed. The geometric `select` search and the heuristic score formula are the colorer's internals. The `LinearizedFunction` / `LinearizedBlock` struct layouts are derived from `flattern_loop`'s field accesses rather than recovered as types. The opt7 SMT-allocation interleaving (`sub_80D9D0`) pins the allocator *set* but not the full step order. The fourth ctor argument `to_space`'s role is [INFERRED].

---

## Related Components

| Name | Relationship |
|---|---|
| `SB_Allocator` / `PSUM_Allocator` / `DRAM_Allocator` / `REG_Allocator` | the per-space Chaitin–Briggs colorers this driver runs (separate pages, planned) |
| `LinearScanAllocator` | the alternative family wired by `--allocator lsa` / optlevel 0/8 |
| `dma_optimization_<space>` / `address_rotation_<space>` | the per-space re-tuning passes interleaved into the cascade |
| `non_ssa_legalization` | re-legalizes SSA-clones to fixed SB/PSUM slots after PSUM+SB coloring |

## Cross-References

- [BackendPass Hierarchy and the Registry](backendpass-registry.md) — proves the 8-name → 1-class collapse this page enumerates
- [The walrus Pass Pipeline and Optlevel Planes](pass-pipeline-optlevels.md) — which builder runs at each optlevel, where the allocator gate sits
- [SSA Exit and Pre-Scheduling](reorder-nonssa-presched.md) — `non_ssa_legalization`, the post-PSUM+SB re-legalization fixed point
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the bank and partition addressing the colorers place into
- [Memory Location and Storage](../bir/memory-location.md) — the `MemoryLocationSet` model the colorers number
- [Penguin Tensor / Buffer Node](../penguin/tensor-buffer-node.md) — the tensor / memory-space placement model upstream of the backend
