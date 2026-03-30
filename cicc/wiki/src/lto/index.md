# LTO & Module Optimization

CICC v13.0 implements Link-Time Optimization as a five-pass pipeline that exploits the GPU's closed-world compilation model for optimization opportunities unavailable to CPU compilers. In CPU LTO, the linker merges partially-optimized object files and runs a second round of optimization on the combined module. The fundamental constraint is that shared libraries, dynamic loading, and symbol interposition limit what the optimizer can assume about the complete program. On GPU, none of these constraints exist. Every `__device__` function that can execute on the hardware must be statically visible at compile time -- there is no device-side `dlopen`, no `.so` files, no PLT/GOT, no symbol preemption. This closed-world guarantee means the LTO pipeline can inline aggressively across translation units, devirtualize every virtual call site against a complete class hierarchy, and promote or split global variables with full knowledge that no external observer will access the original symbols.

The LTO pipeline runs after the main LLVM optimizer (tier 0-3 passes) has performed per-module optimization. It is triggered when cicc processes bitcode from separate compilation (`nvcc --device-c` / `-dc` mode), where each `.cu` file compiles to a relocatable device object containing LLVM bitcode in the [NVVM container](../structs/nvvm-container.md). The device linker (`nvlink`) merges these objects and reinvokes cicc in LTO mode, passing the combined bitcode through the LTO pipeline before final PTX emission. In whole-program compilation (the default), the pipeline is still partially active -- GlobalOpt and the inliner run regardless, but the summary-based import machinery is skipped because there is only one module.

| | |
|---|---|
| **LTO pipeline entry** | `sub_12F5F30` (`0x12F5F30`, 37.8 KB) |
| **NVModuleSummary driver** | `sub_D81040` (`0xD81040`, 56 KB) |
| **Summary builder** | `sub_D7D4E0` (`0xD7D4E0`, 74 KB) |
| **Address range (summary cluster)** | `0xD60000`--`0xD82000` |
| **Address range (import/inline cluster)** | `0x1850000`--`0x186CA00` |
| **NVVM container IRLevel for LTO** | `NVVM_IR_LEVEL_LTO` (value 1) |
| **Compile mode for separate compilation** | `NVVM_COMPILE_MODE_SEPARATE_ABI` (value 2) |
| **Module flags read** | `EnableSplitLTOUnit`, `UnifiedLTO`, `ThinLTO` |

## Why LTO Matters for GPU

Three properties of GPU execution make LTO dramatically more valuable than on CPU:

**Function calls are expensive.** Every GPU function call marshals arguments through the `.param` address space via `st.param` / `ld.param` instruction sequences. A function with 8 struct arguments can generate hundreds of cycles of marshaling overhead that inlining eliminates entirely. Cross-module inlining -- which requires LTO -- is the primary mechanism for removing this cost for functions defined in separate translation units. See the [inliner cost model](./inliner-cost.md) for the full cost analysis.

**Register pressure determines performance.** GPU occupancy (the number of concurrent warps per SM) is bounded by per-thread register usage. Call boundaries force the backend to save and restore registers across the call site, often spilling to local memory (device DRAM, 200-800 cycle latency). LTO enables cross-module inlining, which in turn enables cross-function register allocation -- the single most impactful optimization for GPU code.

**Indirect calls are catastrophic.** An indirect call in PTX (`call.uni` through a register) prevents backend inlining, forces full register spills, destroys instruction scheduling freedom, and creates warp-divergence hazards. Whole-program devirtualization, which requires LTO-level visibility of the complete type hierarchy, converts indirect calls to direct calls and enables all downstream optimizations.

## Regular LTO vs ThinLTO

CICC supports both regular (monolithic) LTO and ThinLTO. The LTO driver at `sub_D81040` reads three module flags via `sub_BA91D0` to determine which mode is active:

| Module Flag | Effect |
|---|---|
| `EnableSplitLTOUnit` | Enables the split LTO unit mechanism for type metadata |
| `UnifiedLTO` | Enables LLVM's unified LTO pipeline (combined thin+regular) |
| `ThinLTO` | Activates summary-based import and the two-phase declaration merge in `sub_D7D4E0` |

**Regular LTO** merges all translation units into a single LLVM module, then runs the full optimization pipeline on the merged result. This gives the optimizer complete visibility but has O(n) memory cost in the total program size and serializes compilation. For GPU programs this is often acceptable because device code is typically smaller than host code.

**ThinLTO** builds per-module summaries (via [NVModuleSummary](./module-summary.md)), uses the summaries to make import decisions without loading full bitcode, then imports selected functions and optimizes each module independently. The builder's `a8` parameter (thinlto_mode flag) activates Phase 2 of the summary builder, which performs a second walk over declarations to merge forward-declared and defined symbol tables. This mode enables parallel per-module optimization at the cost of less global visibility.

In practice, NVIDIA's toolchain (`nvcc` + `nvlink`) uses **regular LTO** as the default for device code, because the closed-world model and relatively small code size (compared to CPU programs) make the memory and compile-time cost acceptable. ThinLTO is available for large CUDA programs where compile time is a concern, activated by passing `-dlto` to `nvcc` (device LTO) or `-flto=thin` through the driver.

## LTO Pipeline

The LTO pipeline executes five major passes in a fixed order. Each pass consumes the output of its predecessor:

```
 ┌────────────────────────────────────────────────────────────────────────┐
 │                    NVVM Container (IRLevel=1)                         │
 │                    LLVM Bitcode + Module Flags                        │
 └────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  1. NVModuleSummary Builder  (sub_D7D4E0, 74 KB)              │
 │     Build per-function summaries with 4-level import priority, │
 │     complexity budget, CUDA attribute flags, call graph edges  │
 └────────────────────┬──────────────────────────────────────────-┘
                      │  ModuleSummaryIndex
                      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  2. ThinLTO Function Import  (sub_1854A20, 4.3 KB)            │
 │     Summary-guided cross-module import with floating-point     │
 │     threshold computation, priority-class multipliers,         │
 │     global import budget cap                                   │
 └────────────────────┬──────────────────────────────────────────-┘
                      │  Materialized functions + thinlto_src_module metadata
                      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  3. Inliner  (sub_1864060 + sub_2613930 + sub_38576C0)        │
 │     Four parallel cost models: NVIDIA custom (20K budget),     │
 │     LLVM standard (225), New PM CGSCC + ML, NVPTX target      │
 └────────────────────┬──────────────────────────────────────────-┘
                      │  Inlined module
                      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  4. GlobalOpt  (sub_18612A0, 65 KB)                            │
 │     Small-constant promotion (≤2047 bits), SRA for structs     │
 │     (≤16 fields), malloc/free elimination, address-space-aware │
 └────────────────────┬──────────────────────────────────────────-┘
                      │  Optimized globals
                      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  5. WholeProgramDevirtualization  (sub_2703170, 13 KB)         │
 │     Type-test metadata → vtable resolution → direct calls      │
 │     Red-black tree for type info lookup, 0x90-byte records     │
 └────────────────────┬──────────────────────────────────────────-┘
                      │
                      ▼
              Dead Kernel Elimination + GlobalDCE
              → Standard optimizer pipeline (tier 0-3)
              → Code generation + PTX emission
```

The LTO pipeline entry at `sub_12F5F30` (37.8 KB) orchestrates this sequence and also runs dead kernel elimination -- removing `__global__` functions that are never referenced by host-side kernel launches. This is a GPU-specific optimization: on CPU, the linker preserves all externally-visible entry points, but in GPU LTO the compiler knows the complete set of kernel launch sites from the host code.

## Separate Compilation and the NVVM Container

When `nvcc --device-c` compiles a `.cu` file, cicc produces an NVVM container with `CompileMode = NVVM_COMPILE_MODE_SEPARATE_ABI` (value 2) and `IRLevel = NVVM_IR_LEVEL_LTO` (value 1). This container wraps partially-optimized LLVM bitcode -- the per-module optimizer has run, but cross-module optimization has not. The bitcode is embedded in the ELF `.nv_fatbin` section of the relocatable object file.

At link time, `nvlink` extracts the bitcode sections from all input objects, concatenates them, and passes the result back to cicc in LTO mode. cicc deserializes each container, links the bitcode modules via LLVM's `Linker::linkModules`, and then runs the LTO pipeline described above on the merged module. The pipeline sees the complete device program for the first time at this point.

The IRLevel enum controls which optimizations have already been applied:

| IRLevel | Value | Meaning |
|---------|-------|---------|
| `NVVM_IR_LEVEL_UNIFIED_AFTER_DCI` | 0 | Default: fully optimized, no LTO needed |
| `NVVM_IR_LEVEL_LTO` | 1 | Partially optimized, awaiting LTO pipeline |
| `NVVM_IR_LEVEL_OPTIX` | 2 | OptiX pipeline IR (separate optimization model) |

## Pass Inventory

| Pass | Entry Point | Size | Pipeline Slot | Type | Sub-page |
|------|-------------|------|---------------|------|----------|
| NVModuleSummary Builder | `sub_D7D4E0` | 74 KB | N/A (called from driver) | Analysis | [module-summary.md](./module-summary.md) |
| NVModuleSummary Driver | `sub_D81040` | 56 KB | N/A (LTO entry) | Module | [module-summary.md](./module-summary.md) |
| ThinLTO Function Import | `sub_1854A20` | 4.3 KB | Slot 43 (`"function-import"`) | Module | [thinlto-import.md](./thinlto-import.md) |
| ThinLTO Threshold Engine | `sub_1853180` | 5.1 KB | N/A (called from import driver) | Utility | [thinlto-import.md](./thinlto-import.md) |
| NVIDIA Custom Inliner | `sub_1864060` | 75 KB | CGSCC pass | CGSCC | [inliner-cost.md](./inliner-cost.md) |
| LLVM Standard InlineCost | `sub_30DC7E0` | 51 KB | N/A (library) | Analysis | [inliner-cost.md](./inliner-cost.md) |
| New PM CGSCC Inliner | `sub_2613930` | 69 KB | CGSCC pass | CGSCC | [inliner-cost.md](./inliner-cost.md) |
| NVPTX Target Cost Modifier | `sub_38576C0` | 58 KB | N/A (target hook) | Target | [inliner-cost.md](./inliner-cost.md) |
| GlobalOpt | `sub_18612A0` | 65 KB | Slot 45 (`"globalopt"`) | Module | [globalopt.md](./globalopt.md) |
| WholeProgramDevirt | `sub_2703170` | 13 KB | Slot 121 (`"wholeprogramdevirt"`) | Module | [devirtualization.md](./devirtualization.md) |

## Key Differences from CPU LTO

| Aspect | CPU LTO | CICC GPU LTO |
|--------|---------|--------------|
| **Import threshold** | 100 instructions (default) | Priority-class multipliers, global budget at `dword_4FAB120` |
| **Cold import** | 0x multiplier (never import cold) | Imports cold functions if priority >= 2 |
| **Inline budget** | 225 (LLVM default) | 20,000 (NVIDIA custom), 89x larger |
| **Devirt conservatism** | Must handle DSOs, hidden visibility | Full type hierarchy always visible |
| **Code size concern** | Bloats `.text`, impacts cache/pages | No shared libs; size is secondary to register pressure |
| **Address spaces** | Trivial (flat memory model) | 5+ address spaces; GlobalOpt must preserve AS through splits |
| **Dead symbol elimination** | Linker GC sections | Dead kernel elimination in `sub_12F5F30` |
| **Threshold comparison** | Integer instruction count | Floating-point threshold with hotness/linkage/priority multipliers |
| **ML-guided inlining** | Available upstream | Integrated via InlineAdvisor at `sub_2609820` with model at `sub_29B2CD0` |

## LTO Knob Summary

### NVModuleSummary Knobs

| Knob | Default | Effect |
|------|---------|--------|
| `dword_4F87C60` (global override) | 0 | When nonzero, forces all symbols to importable; value 2 = conservative comdat handling |

### ThinLTO Import Knobs

Registered in `ctor_184_0` (`0x4DA920`) and `ctor_029` (`0x489C80`):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `import-instr-limit` | int | 100 | Base instruction count threshold for import |
| `import-hot-multiplier` | float | 10.0 | Multiplier applied to threshold for hot callsites |
| `import-cold-multiplier` | float | 0.0 | Multiplier for cold callsites (0 = never import cold on CPU) |
| `dword_4FAB120` | int | -1 | Global import budget; negative = unlimited |
| `dword_4FAA770` | int | 0 | Current import count (runtime accumulator) |
| `summary-file` | string | -- | Path to external summary file for ThinLTO |
| `function-import` | -- | -- | Pipeline registration string (slot 43) |
| `disable-thinlto-funcattrs` | bool | false | Disable ThinLTO function attribute propagation |
| `thinlto-workload-def` | string | -- | Workload definition file for priority-guided import |

### Inliner Knobs

Registered in `ctor_186_0` (`0x4DBEC0`):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `inline-budget` | int | 20,000 | Per-caller inlining cost budget (NVIDIA custom model) |
| `inline-total-budget` | int | -- | Global total budget across all callers |
| `inline-adj-budget1` | int | -- | Adjusted per-caller budget (secondary) |
| `nv-inline-all` | bool | off | Force inline every function call |
| `profuseinline` | bool | off | Verbose inlining diagnostic output |
| `inline-switchctrl` | int | -- | Heuristic tuning for switch statements |
| `inline-threshold` | int | 225 | LLVM standard model threshold (separate from NVIDIA's 20K) |
| `function-inline-cost-multiplier` | float | -- | New PM: penalty multiplier for recursive functions |

### GlobalOpt Knobs

No dedicated `cl::opt` flags. All thresholds are hardcoded:

| Parameter | Value | Description |
|-----------|-------|-------------|
| Max bits for promotion | 2,047 (`0x7FF`) | Globals exceeding this fall through to SRA |
| Max struct fields for SRA | 16 | Structs with >16 fields are not split |
| Hash table load factor | 75% | Triggers rehash of processed-globals table |
| Pipeline position | Step 30 (tier 2/3) | After GlobalDCE, before LoopVectorize |

### Devirtualization Knobs

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `wholeprogramdevirt` | -- | -- | Pipeline registration string (slot 121) |

The pass has no NVIDIA-specific tuning knobs. It relies entirely on the completeness of type_test metadata produced by the NVModuleSummary builder.

## Cross-References

- **[NVModuleSummary Builder](./module-summary.md)** -- 4-level import priority, complexity budget, CUDA attribute tracking
- **[ThinLTO Function Import](./thinlto-import.md)** -- threshold computation, priority-class multipliers, global budget
- **[Inliner Cost Model](./inliner-cost.md)** -- four parallel models, `.param` address space cost, ML advisory
- **[GlobalOpt for GPU](./globalopt.md)** -- address-space-aware SRA, small-constant promotion, malloc elimination
- **[Whole-Program Devirtualization](./devirtualization.md)** -- closed-world virtual call resolution, type test metadata
- **[NVVM Container Format](../structs/nvvm-container.md)** -- IRLevel enum, CompileMode, bitcode payload encoding
- **[LLVM Optimizer](../pipeline/optimizer.md)** -- LTO pipeline entry at `sub_12F5F30`, tier system
- **[LazyCallGraph & CGSCC](../infra/lazycallgraph.md)** -- call graph infrastructure used by the CGSCC inliner
- **[Entry Point & CLI](../pipeline/entry.md)** -- flag catalog routing to lto output vector, `-dc` mode
