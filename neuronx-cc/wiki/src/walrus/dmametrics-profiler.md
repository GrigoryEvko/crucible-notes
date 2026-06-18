# DMAMetrics, PerformanceProfiler and the per-load `load_profile` Map

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). Every body lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (0x62d660…) and `.rodata` (0x1c72000…) the virtual address equals the file offset; `.data` carries a +0x400000 delta (so a `.data` VA `0x3ded4d8` is file offset `0x39ed4d8`). The `0x5e…/0x5f…/0x6…` band is `.plt` thunks — every body cited here is a real `.text` frame, cross-checked against the retained `.dynsym` (`nm -DC`). Other wheels differ — treat every address as version-pinned.*

## Abstract

Three independent compile-time diagnostics share one theme — *how much data moves, and how efficiently* — but compute completely different numbers and live in different classes. This page documents all three and pins the one quirk in each that a reimplementer is most likely to invent wrong.

1. **`DMAMetricsImpl`** is the `dma_metrics` `BackendPass`. It walks the `main` function of every module and sorts each DMA-family instruction into a `{InstType × ShapeType × MemType}` bucket — **counting instances, not summing bytes** — then `print`s a `libfort` ASCII grid to the compiler log at INFO. This is the operator-facing diagnostic; nothing in the NEFF reads it.
2. **`PerformanceProfiler::runProfile`** is a *separate, coarser* static memory-roofline estimator. It sums `IOTensorBytes` and `DDRTransferBytes` over the function's allocations and emits one scalar, **`MemEffcy = DDRTransferBytes / IOTensorBytes`**. It is **not** the discrete-event PerfSim cost engine (in-flight sibling 8.45) and shares no code with it.
3. The per-instruction **`load_profile`** map is **not** a profiler field at all — it belongs to **`LoadCloning`** (the load-clustering pass that feeds `vn_splitter`, [8.23](vnsplitter-shrink.md)). Each entry distills a load to its **arithmetic intensity `aif = downstream_arith_ops / bytes_moved`**, the sort key that drives load cloning.

None of the three is the MetricStore (in-flight sibling 8.47); none calls `addMetric` directly. They are the human-readable / decision-input layer that sits *beside* the metric-emitting passes.

| | |
|---|---|
| **Pass class / driver** | `DMAMetrics::run(std::vector<unique_ptr<Module>>&)` @0x16c7fc0 (real); `DMAMetrics::run(bir::Module&)` @0x16c6c90 (stub, status 2) |
| **The pass body** | `DMAMetricsImpl::DMAMetricsImpl(vector<Module>&, Logger&, set<DGELevels> const&)` @0x16ccf30 — **the ctor *is* the pass** |
| **Per-inst entry** | `DMAMetricsImpl::enterInstruction(bir::Instruction&)` @0x16cacb0 |
| **The bucketing quirk** | `DMAMetricsImpl::updateCopyCastDMA(bir::InstDMA&)` @0x16cb740 |
| **Table dump** | `DMAMetricsImpl::print(logging::Logger&)` @0x16c9170 |
| **Tuple-key map op** | `std::map<tuple<InstType,ShapeType,MemType>,DMACount>::operator[]` @0x16cac00 (PLT 0x5fdb10) |
| **Roofline profiler** | `PerformanceProfiler::runProfile()` @0xd6b080; `PerformanceProfiler::run(Module&)` @0xd6d0d0 (delegating stub) |
| **`load_profile` producer** | `LoadCloning::profile_load_aif(bir::Function&)` @0xc7d770 (map @`LoadCloning+0x170`) |
| **`load_profile` consumer** | `LoadCloning::cluster_load_uses()` @0xc821f0 |
| **aif numerator / denominator** | `LoadCloning::get_arithmetic_ops(Instruction*)` @0xc7d200 / `LoadCloning::get_num_bytes(Instruction*)` @0xc79380 |

All symbols verified present at these exact addresses via `nm -DC libwalrus.so`. **CONFIRMED.**

---

## Part A — `DMAMetrics` / `DMAMetricsImpl`

### A.1 The pass shape — the ctor is the body

`DMAMetrics` is a `BackendPass`. Two `run` overloads exist, but only the vector-of-modules form does work:

```c
// DMAMetrics::run(vector<unique_ptr<Module>>&) @0x16c7fc0  — CONFIRMED
Status DMAMetrics::run(vector<unique_ptr<Module>> &modules) {
    if (modules.begin() == modules.end())
        return {code: 2, msg: "No modules to analyze!"};   // empty-set guard
    DMAMetricsImpl impl(modules,
                        ctx.logger,                         // a2+40
                        *(ctx + 96) + 432);                 // const set<DGELevels>&
    // ^ the ENTIRE pass runs inside the ctor; impl's dtor then frees the
    //   two 0x200-byte + one 0xD8 SmallVector worklist arenas.
    return {code: 0, msg: ""};
}
```

`DMAMetrics::run(bir::Module&)` @0x16c6c90 is a one-line stub returning `{2, ""}` — the single-module overload is never the real metric path. **CONFIRMED.**

The third ctor argument `*(ctx+96)+432` is a `const set<DGELevels>&` (the *enabled* DGE-levels config) pulled from the backend `PassOptions`. **The exact `PassOptions` accessor is not pinned — INFERRED** that it is `+432` inside the context object at `ctx+96`; the *type* (`set<DGELevels>&`) is CONFIRMED from the demangled ctor signature.

The ctor (read from disasm — Hex-Rays declined this frame) allocates three worklist arenas, zeroes the scalar counters (`this+0xC0`, `this+0xC4`, `this+0xC8`) and the RB-tree headers, stashes `(Logger*, set<DGELevels>*)`, then runs the per-module loop:

```c
// DMAMetricsImpl::DMAMetricsImpl body @0x16ccf30..0x16cd3d5 — STRONG (offsets CONFIRMED)
for (Module &m : modules) {
    name = bir::Module::getName(m);                   // 0x16cd20e
    logger.attributes.insert("mod_name", name);       // 0x16cd2cd — tags every record
    Function *fn = m.getFunctionByName("main");        // 0x16cd35b — "main" string CONFIRMED
    if (fn)
        IRVisitor<DMAMetricsImpl>::visit(fn.bb_begin, fn.bb_end);   // 0x16cd391
    logger.attributes.erase("mod_name");              // 0x16cd3ac
}
this->print(this->logger);                            // 0x16cd3d5 — ONCE, after all modules
```

> **Reimplementer note.** Only the function named exactly `"main"` in each module is analyzed; everything else is silently skipped. Accumulator state lives in `impl` and is dumped exactly once at the end.

### A.2 `enterInstruction` — the DMA-family gate and the `Total` pair

`enterInstruction` runs first for **every** instruction (before the subtype switch). It reads the opcode at `inst+0x58` and maintains the two headline scalars that `print`'s percentage divides:

```c
// DMAMetricsImpl::enterInstruction @0x16cacb0 — CONFIRMED
void enterInstruction(Instruction &inst) {
    op = inst.opcode;                                  // inst+0x58
    // (1) DMA-FAMILY GATE  — 0x16cacc4
    if (op == 18 || ((unsigned)(op - 19) <= 0x30 && byte_1E03860[op - 19]))
        ++this->dge_inst_count;                        // this+0xC0  (add [rdi+0xC0],1)
    // byte_1E03860 (0x31 bytes @0x1E03860, xxd-verified) flags op-19 for
    // {19,22,32,41,42,43,44,45,46,67}; plus explicit op==18. So the counted
    // DMA family is {18,19,22,32,41-46,67}.

    // (2) DGE-LEVEL LOOKUP + "Total/other" PAIR  — 0x16cace5
    node = lower_bound(std::map@this+0x108, dge_level_ordinal);   // RB-tree, keyed by DGEType
    if (tree_empty || best.key > 3 || no_node_key_le_3)
        this->total_pair += {1, 1};                    // paddd [rdi+0xC4], {1,1}
        //  ^ bumps this+0xC4 (lo) AND this+0xC8 (hi) together — see "dead twin" below

    // (3) INDIRECT SAVE/LOAD GUARD  — 0x16cad78
    if (found_key <= 3 && (unsigned)(op - 0x2A) <= 3)  // op in {42,43,44,45}
        NeuronAssertion(1878,
            "Invalid instruction in BIR, Indirect Saves and Loads should be lowered",
            "!(IsInvalidInstruction)", "dma_metrics.cpp:161");   // FATAL, throw_with_nested
}
```

The `byte_1E03860` membership `{18,19,22,32,41-46,67}` and the `paddd [rdi+0xC4],{1,1}` are CONFIRMED in disasm; the assertion string and `dma_metrics.cpp:161` source anchor are CONFIRMED in `.rodata`. The `DGELevels` ordinals (`0 spill_reload · 1 scalar_dynamic_offset · 2 vector_dynamic_offsets · 3 dynamic_size · 4 dst_reduce · 5 transpose`) come from sibling D-Z02; the `<= 3` boundary selects the first four (the descriptor-gen DGE flavours) — **STRONG**.

> **CORRECTION (vs. backing report A.5/print).** The report's print section says the *slow* counter is `this+0xC4`. Both `updateCopyCastDMA` (A.3) **and** `enterInstruction` bump `this+0xC4`; the percentage in `print` divides `this+0xC4` by `this+0xC0`. These are the same two scalars from both code paths — there is no separate "slow" field. The `+0xC8` twin set by the `paddd` is **never read** by `print` for `this`; it is a write-only side effect of bumping a packed qword. **RESOLVED — print reads only `+0xC0` and `+0xC4`** (disasm 0x16c9743/0x16c9755, below).

### A.3 ⭐ `updateCopyCastDMA` — instance bucketing, NOT byte summing

This is the headline quirk. Reached for `InstLoad(19)`, `InstSave(22)`, and the `isCopyOrCastDMA` fall-through. It classifies each copy/cast DMA into a `{Copy|Cast} × {Fast|Slow} × {SR|IO}` bucket and **increments that bucket's count by one** — it never accumulates a byte total:

```c
// DMAMetricsImpl::updateCopyCastDMA @0x16cb740 — CONFIRMED
void updateCopyCastDMA(InstDMA &inst) {
    bool isCopyDMA = bir::isCopyDMA(&inst);            // call @0x16cb76c
    bool isIODMA   = bir::isIODMA(&inst);              // call @0x16cb778

    // (1) DESCRIPTOR COUNT — number of PhysicalAccessPattern operands across IN+OUT.
    //     Used ONLY as a >0 guard, not as a size.
    int desc_count = 0;                                // r14
    for (op in [list_IN @inst+0xA0/0xA8, list_OUT @inst+0xB0/0xB8]) {
        if (llvm::isa<PhysicalAccessPattern, bir::Argument>(*(op - 8)))  // call @0x16cb7ab/0x16cb7eb
            ++desc_count;                              // add $0x1,%r14  @0x16cb806
    }
    if (desc_count == 0) return;                       // no physical descriptor => skip

    // (2) SHAPE — from AP[0]'s innermost element count
    ap0 = inst.getArgument<PhysicalAccessPattern>(0);  // call @0x16cb863
    num_elems = (ap0.apvec[0].field@+8 if ap0.apvec /*+0x1D8*/ nonempty
                 else ap0+0x50-chain.field@+8);
    bool slow_shape = ((num_elems & 0xF) != 0);        // and $0xf,%edx  @0x16cb89b

    // (3) KEY + COUNT BUMP — the bucket is keyed, then incremented by ONE
    key = { InstType  : isCopyDMA ? Copy(0) : Cast(1),
            ShapeType : slow_shape ? Slow(1) : Fast(0),
            MemType   : isIODMA   ? IO(0)   : SR(1) };
    DMACount &c = map@this+0xD0 [key];                 // operator[] call @0x16cb8cd
    ++c.total;                                         // addl $0x1,(%rax)        @0x16cb8d7
    if (inst.flag@+0xF8 != 0) {                        //  "is slow / oversized"  @0x16cb8da
        ++this->slow_total;                            // addl $0x1,0xC4(%rbx)    @0x16cb8e8
        ++c.slow;                                      // addl $0x1,0x4(%rax)     @0x16cb8ef
    }
}
```

**Verified directly in disasm** (`objdump -d --start-address=0x16cb740`):

- `isCopyDMA`/`isIODMA`/`isa<PhysicalAccessPattern,Argument>` PLT calls — present.
- `and $0xf,%edx` @0x16cb89b — the `num_elems & 0xF` slow-shape test (innermost element count not a multiple of 16 ⇒ a sub-16-element / unaligned transfer).
- the `std::map<tuple<InstType,ShapeType,MemType>,DMACount>::operator[]` PLT @0x16cb8cd.
- the three increments are `addl $0x1, ...` — **literal `+1`, on a `DMACount` field, not an `add %reg` of a byte length.**

> **This is the quirk to pin.** `DMAMetrics` answers *"how many Copy-IO-Slow DMA instructions are there"*, **not** *"how many bytes did they move."* There is no byte-accumulator anywhere in this body; `desc_count` is a presence guard, `num_elems` feeds only the Fast/Slow boolean, and every count bump is `+1`. A reimplementer who sums transfer sizes here is reproducing the wrong pass. **CONFIRMED.** (Byte-summing lives in `PerformanceProfiler`, Part B, and `LoadCloning::get_num_bytes`, Part C — both genuinely sum `partitions × bytes_per_partition`.)

The tuple key memory order is libstdc++-reversed (`[+0]=MemType, [+4]=ShapeType, [+8]=InstType`). Enum values (CONFIRMED from `print`'s label switch strings): `InstType {0 Copy, 1 Cast, 2 Transpose, 3 CCE, 4 Replicate, 5 DstReduce, 6 ReadVarAddr}`, `ShapeType {0 Fast, 1 Slow, 2 N/A}`, `MemType {0 IO, 1 SR, 2 N/A}`. The IRVisitor switch (@0x16ccbc0, 110 cases) routes `Transpose`/`CCE`/`Replicate`/`DstReduce`/`ReadVarAddr` to other bucket keys directly; only the `Copy`/`Cast` path runs the shape-classification above.

### A.4 `print` — the `libfort` table and the headline percentage

`print` builds a `libfort` table (`ft_create_table` → `ft_set_cell_prop`/`ft_ln`/`ft_add_separator`/`ft_nwrite` → `ft_to_string` → `ft_destroy_table`) and logs the whole grid at severity 20 (INFO), bracketed `"DMA Metrics Start\n"` … `"\nDMA Metrics End"` (both strings CONFIRMED in `.rodata` @0x1c898bf).

Six columns (header strings CONFIRMED): `Instruction Type | Shape Type | Memory Type | DGE Instructions (BIR) | Total Instructions (BIR) | Percentage (%)`. Body rows iterate `map@this+0xD0`; a cosmetic relabel rewrites a `"Fast Shape"` cell to `"Good Shape"`/`"Bad Shape"` (strings `Good Shape`/`Slow Shape` @0x1c8980b/0x1c8988d). Consecutive rows with the same `InstType`/`ShapeType` use libfort cell-span so the label prints once per group.

The bottom **`Total`** row carries the one headline number:

```c
// print @0x16c9743..0x16c9779 — CONFIRMED in disasm
denom = this->dge_inst_count;        // mov 0xC0(%rbx),%eax   @0x16c9743
numer = this->slow_total;            // mov 0xC4(%rbx),%edx   @0x16c9755
pct   = (float)numer / (float)denom; // cvtsi2ss ×2; divss %xmm1,%xmm0  @0x16c9763..76d
pct  *= 100.0f;                      // mulss 0x1dd8bfc(=100.0)         @0x16c9771
// => Percentage = 100 * (this+0xC4) / (this+0xC0)
```

So the headline metric is **`100 × (this+0xC4) / (this+0xC0)`** — the fraction of DMA-family instructions classified DGE/slow. Verified: `mov 0xc0`/`mov 0xc4`, two `cvtsi2ss`, `divss`, `mulss 0x1dd8bfc`. The constant `0x1dd8bfc` holds `100.0f`. If `dge_inst_count == 0` the cell is `-1.0`. **CONFIRMED.**

> **OUTPUT SINK.** The grid goes to the `logging::Logger` at INFO — not JSON, not the NEFF. It is purely a human-readable diagnostic; the machine-consumable DMA stats that reach the NEFF come from the separate DMAReport / HBMUsage report passes and the MetricStore (in-flight sibling 8.47), of which this `print` is the operator-facing mirror. **STRONG.**

---

## Part B — `PerformanceProfiler` (the separate coarse memory roofline)

`PerformanceProfiler::run(Module&)` @0xd6d0d0 is the `BackendPass` override (status return) delegating to `runProfile()` @0xd6b080. There is **no `register_generator_*` lambda** for it — it is not a CLI-named pipeline pass; it is invoked from the Tensorizer/CoreGen phase (xrefs surface only PLT thunks; `CoreV2GenImpl::generateDynamicDMA` shares the `0xd6d0d0` thunk region). **STRONG.**

`runProfile` is a *single linear pass* over the module's `main` function — not a timeline simulator. It calls `Module::getDMAProfile(mod)` (PLT @0xd6b0b1, CONFIRMED) and runs two phases:

```c
// PerformanceProfiler::runProfile @0xd6b080 — CONFIRMED
void runProfile() {
    Function *main = this->module->getMain();          // hard precondition; BUG() if null
    DMAProfile *prof = Module::getDMAProfile(module);   // call @0xd6b0b1

    // PHASE 1 — non-local-tensor reload/spill count (lines 219-269)
    int v4 = 0;
    for (Instruction &i : main) {
        if (i.class_tag /*+80*/ == 3 /*InstAbstractCopy*/) {
            ml = cast<MemoryLocation>(getArgument<PhysicalAccessPattern>(i,0).storage + 56);
            if (ml.storage_kind /*+272*/ == 4 && *(ml+216) == 8 /*SBUF/HBM tensor*/)
                if (!isTensorKindInput(ml) && *(ml+224) != 6)   // NOT a graph input
                    ++v4;                                       // non-local reload/spill
        }
    }
    log(INFO, "number of tensorizer non-local-tensor caused reload left %d", v4);
    log(INFO, "number of tensorizer non-local-tensor caused spill  left %d", v4);

    // PHASE 2 — memory-efficiency roofline (lines 354-1135)
    long IOTensorBytes = 0, DDRTransferBytes = 0;
    for (MemoryLocation &ml : main.allocs /*Storage class==1*/) {
        if (ml.size /*+16*/ == 0 || ml.partitions /*+56*/ == 0) continue;
        long b = ml.bytes_per_partition /*+264*/ * ml.num_partitions /*+256*/;  // = get_num_bytes
        IOTensorBytes += b;
        if (alloc_moves_HBM_SBUF)
            _InterlockedAdd64(&DDRTransferBytes_stat /*@0x3ded4d8*/, b);  // lock add @0xd6b819
        log(DEBUG /*10*/, "DEBUG (PerformanceProfiler) visiting mem loc ...");
    }
    // FINAL — the one scalar this pass exists to produce
    float MemEffcy = (float)DDRTransferBytes / (float)IOTensorBytes;   // divss @0xd6cc63
    *(prof + 112) = MemEffcy;                                          // written BACK onto the profile
    log(DEBUG, "DEBUG (PerformanceProfiler) Memory Effcy (Tensorizer Way) = %f; "
               "IOTensorBytes = %ld; DDRTransferBytes = %ld.\n", MemEffcy, ...);
}
```

**Verified directly in disasm/`.rodata`:**

- `Module::getDMAProfile` PLT @0xd6b0b1.
- `lock add %rax, 0x3ded4d8(%rip)` @0xd6b819 — the atomic `DDRTransferBytes` accumulator. `0x3ded4d8` is a `.data` VA; with the +0x400000 delta it is file offset `0x39ed4d8`. It is registered as an `llvm::TrackingStatistic` (`RegisterStatistic` PLT call @0xd6cb79) and so is surfaced by `-stats`.
- `divss %xmm1,%xmm0` @0xd6cc63 — the `MemEffcy` ratio.
- strings `"; DDRTransferBytes = "` @0x1c79ecf and `"DEBUG (PerformanceProfiler) Memory Effcy (Tensorizer Way) = "` @0x1d0f558.

> **The formula to pin: `MemEffcy = DDRTransferBytes / IOTensorBytes`** — DDR (HBM↔SBUF) traffic over total tensor footprint, a roofline data-movement-efficiency ratio. **CONFIRMED.**

> **How it differs from PerfSim (in-flight sibling 8.45).** PerfSim is a discrete-event timeline (per-engine, per-DMA-queue, `bir::Hwm` latency oracle) producing a *time* estimate. `PerformanceProfiler` does *one pass over MemoryLocations*, summing two byte totals into a single ratio plus a DDR-bytes Statistic — no timeline, no per-engine model, no Hwm oracle. They share no code; neither calls the other. They are the explicit *distinction* to keep straight: PerfSim measures cycles, this measures HBM traffic efficiency. **STRONG.** `runProfile` reads `getDMAProfile()` and writes `MemEffcy` back onto it (`+112`) — it augments the DMA profile object that later serialization passes read; it is not a standalone report. **STRONG.**

---

## Part C — the per-load `load_profile` map (`LoadCloning`, not the profiler)

### C.0 Ownership correction

> **CORRECTION (the AD04 ownership note, re-confirmed here).** `DenseMap<bir::Instruction*, load_profile>` is **not** a `PerformanceProfiler` field. It is a `LoadCloning` member at `LoadCloning+0x170` (=368). Producer `LoadCloning::profile_load_aif(Function&)` @0xc7d770; consumer `LoadCloning::cluster_load_uses()` @0xc821f0. Bucket stride = 104 bytes (8-byte `Instruction*` key + 96-byte `load_profile` value). All four symbols CONFIRMED via `nm -DC`. The map keys per-instruction load decisions for the load-clustering / cloning pass that feeds `vn_splitter` ([8.23](vnsplitter-shrink.md)).

### C.1 The `load_profile` value (96 bytes)

Each value holds (containers CONFIRMED from the producer's insert sites; **field offsets within the 96-byte value are INFERRED** — the three containers are pinned, their byte order is not):

- a nested `DenseMap<MemoryLocation*, long>` — downstream op-count attributed to each input tensor;
- a `DenseSet<MemoryLocation*>` — the source memlocs this load feeds (cluster-overlap / Jaccard quality);
- a `std::map<float, Instruction*>` + a `float aif` — the arithmetic-intensity sort key.

### C.2 ⭐ `profile_load_aif` — the `aif = arith_ops / bytes` formula

```c
// LoadCloning::profile_load_aif @0xc7d770 — CONFIRMED
void profile_load_aif(Function &fn) {
    for (Instruction *L : fn) {
        // qualifying load: opcode==19 && !dependentsEmpty && !is_static_weights
        //                  && !load_has_superset_user            (lines 328-340)
        if (L.opcode /*+88*/ != 19 || !qualifies(L)) continue;

        long total_ops = 0;
        for (User *U : def_use_chain(L)) {
            long a = LoadCloning::get_arithmetic_ops(U);   // U's compute work — C.3
            load_profile.per_memloc[outMLoc(U)] += a;       // op[] @line 411
            total_ops += a;                                 // running total -> v125[0]
        }
        // ⭐ THE FORMULA — divss of (total downstream ops) by (bytes L moves)
        long bytes = LoadCloning::get_num_bytes(L);         // call @0xc7ddd0 — C.4
        float aif  = (float)total_ops / (float)bytes;        // cvtsi2ss ×2; divss @0xc7ddef
        if (aif >= this->threshold /*+86*/)
            clone_candidates@this+24[aif] = L;               // std::map<float,Instruction*>
        load_profile_map@this+368[L] = load_profile;         // insert (lines 1034-1048)
        log(DEBUG /*10*/, "[LOAD] %s\tarithmetic intensity = %f\n", L, aif);
    }
}
```

**Verified directly in disasm** (`objdump -d --start-address=0xc7d770`): `get_num_bytes` PLT call @0xc7ddd0, then `cvtsi2ss %rax,%xmm0` (bytes), `cvtsi2ssq 0x20(%rsp),%xmm1` (op total), `divss %xmm0,%xmm1` @0xc7ddef. The operand order is `divss %xmm0(bytes),%xmm1(ops)` ⇒ `xmm1 = ops / bytes` — i.e. **`aif = downstream_arith_ops / bytes_moved`**. The string `"arithmetic intensity = "` is CONFIRMED in `.rodata` @0x1c77b88. **CONFIRMED.**

### C.3 `get_arithmetic_ops` — the per-op compute / engine model

```c
// LoadCloning::get_arithmetic_ops @0xc7d200 — CONFIRMED
long get_arithmetic_ops(Instruction *U) {
    switch (U.opcode /*+88*/) {
        case 8:  return 2 * get_matmult_macs(U);   // MatMult / PE  (1 MAC = 2 flops)
        case 20: return get_reduce_ops(U);          // reduce-class
        case 23: return get_tensor_copy_ops(U);     // TensorCopy / DVE
        case 27: return get_reduce_ops(U);          // Reduce / Pool
        case 29: return get_tensor_scalar_ops(U);   // TensorScalar / Act
        default: if (bitset 0x481400 has op) return 0;  // pure data movement
                 else return get_partition_num(U) * get_elements_per_partition(U,0);
    }
}
```

The opcode→engine routing (PE/DVE/Act/Pool) is the per-instruction compute-load model; `aif` pairs that compute estimate against the DMA bytes from C.4. **CONFIRMED.**

### C.4 `get_num_bytes` — the per-load DMA-bytes model

```c
// LoadCloning::get_num_bytes @0xc79380 — CONFIRMED
long get_num_bytes(Instruction *L) {
    out = (L.opcode == 22 /*Save*/) ? getArgument(L,0) : getOutput(L,0);
    ml  = cast<MemoryLocation>(out.storage);            // storage_kind +272 == 4
    return ml.num_partitions /*+264*/ * ml.bytes_per_partition /*+256*/;
}
```

This is the *same* `partitions × bytes_per_partition` model `PerformanceProfiler` uses for `IOTensorBytes` (B, Phase 2) — the genuine byte-sum the `DMAMetrics` bucket count (A.3) deliberately does **not** compute. **CONFIRMED.**

### C.5 How it is consumed

`cluster_load_uses` @0xc821f0 reads the `load_profile` map (LookupBucketFor @0xc8232a), builds `all_clusters` of `a_cluster` nodes, then graph-partition refinement: `get_quality_simple()` (cohesion = memloc overlap) and `remove_max_intra_cluster_edge(i)` (iteratively split the worst edge), reporting `"refine_cluster <id> size:.. min:.. max:.. aif:.."` at DEBUG. Clusters group loads that share source memlocs *and* have similar arithmetic intensity, so a hot load is cloned once per cluster (`clone_load` @0xc81330) rather than globally — minimizing redundant DMA while keeping compute local. The pipeline order (`LoadCloning::run` @0xc83920): `getHWM → index_memlocs → index_instruction → profile_load_aif (BUILD) → cluster_load_uses (CONSUME) → index_instruction → check_live_range → clone_load`. **CONFIRMED.**

---

## Part D — where the three numbers go

None of the three bodies calls `MetricStore::addMetric` directly (their disasm is clean of `addMetric`/`MetricStore`). Their outputs:

1. **`DMAMetricsImpl::print`** → a `libfort` ASCII grid to the Logger at INFO. Purely human-readable; the machine-consumable DMA counters that reach the NEFF live in the separate DMAReport / HBMUsage passes and the MetricStore (in-flight sibling 8.47). `print` is the operator-facing mirror of those buckets. **STRONG.**
2. **`PerformanceProfiler::runProfile`** → two INFO reload/spill lines, the `DDRTransferBytes` `llvm::TrackingStatistic` (`-stats`), and the `MemEffcy` float written back onto `Module::getDMAProfile()+112` — carried on the DMA-profile object later serialization passes read. **STRONG.**
3. **`LoadCloning` `load_profile`/`aif`** → not a metric; an internal decision input that drives load cloning + clustering inside the `separate_load_and_compute` family. It mutates the IR (clones loads), which then changes the inst-counts the report passes and PerfSim subsequently measure. **CONFIRMED/STRONG.**

Net loop: `LoadCloning(aif)` shapes the IR → PerfSim + DMAReport + `PerformanceProfiler` measure the result → MetricStore collects the scalars → the PGA autotuner re-tunes. `DMAMetrics::print` and the `PerformanceProfiler` DEBUG records are the human-readable observability layer over that loop. **STRONG.**

---

## Reimplementation checklist

- **`DMAMetrics` counts instances, not bytes.** Bucket each Copy/Cast DMA into `{Copy|Cast} × {Fast|Slow} × {SR|IO}` and increment by `+1`; slow-shape = `(AP[0].num_elems & 0xF) != 0`; descriptor count is a `>0` presence guard. No byte accumulator exists in `updateCopyCastDMA`.
- The headline percentage is `100 × (DGE-or-slow count `+0xC4`) / (DGE-family count `+0xC0`)`, `-1.0` if the denominator is zero. The `+0xC8` twin is a dead write.
- Only the `"main"` function of each module is visited; the whole pass runs in the `DMAMetricsImpl` ctor and `print`s once.
- **`PerformanceProfiler` is the byte-summing roofline:** `MemEffcy = DDRTransferBytes / IOTensorBytes`, both via `partitions × bytes_per_partition`. It is distinct from PerfSim (cycles vs. traffic) and writes `MemEffcy` back to `getDMAProfile()+112`.
- **`load_profile` is a `LoadCloning` member, not a profiler field.** `aif = sum(get_arithmetic_ops over users) / get_num_bytes(load)`; `get_num_bytes` is the same `partitions × bytes_per_partition` model. Loads above the `aif` threshold become clone candidates clustered by shared memlocs.
