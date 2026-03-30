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

## LTO Pipeline Entry -- `sub_12F5F30` Algorithm

`sub_12F5F30` (`0x12F5F30`, 37,797 bytes) is the top-level LTO orchestrator. It is called after the CLI parser (`sub_12F7D90`) has resolved the compilation mode bitmask and the LTO argument vector has been populated from the `-Xlto` forwarding meta-flag. The function operates in three distinct modes determined by the mode bitmask in `a13`:

| Mode | Bitmask | CLI Flag | Behavior |
|------|---------|----------|----------|
| **gen-lto** | `0x21` | `-gen-lto` | Emit partially-optimized bitcode for later linking. No dead-kernel pass. |
| **full LTO** | `0x23` | `-lto` | Full merge + optimize + dead-kernel elimination + emit PTX. |
| **link-lto** | `0x26` | `-link-lto` | Link pre-existing LTO bitcode modules, run full pipeline. |

The function's argument list is reconstructed from the LTO output vector `v330` (the fourth CLI routing vector, populated by `-Xlto` and the six `-host-ref-*` flags). It receives the merged LLVM module, the host reference tables, and the compilation options struct.

### Pseudocode: `sub_12F5F30` Top-Level

```
function sub_12F5F30(module, lto_args, options, error_cb):
    # ---- Phase A: Parse LTO-specific arguments ----
    mode = NONE
    trace_enabled = false
    optimize_unused_vars = false
    host_refs = HostRefTable{}      # 6-field table: ek, ik, ec, ic, eg, ig
    force_device_c = false

    for arg in lto_args:
        switch arg:
            case "-gen-lto":       mode = GEN_LTO
            case "-link-lto":      mode = LINK_LTO
            case "-olto":          lto_opt_level = next_arg()
            case "--device-c":     device_c = true
            case "--force-device-c": force_device_c = true
            case "--trace":        trace_enabled = true
            case "-optimize-unused-variables": optimize_unused_vars = true
            case "-has-global-host-info":      has_host_info = true
            case "-host-ref-ek=*": host_refs.ek = parse_symbol_list(value)
            case "-host-ref-ik=*": host_refs.ik = parse_symbol_list(value)
            case "-host-ref-ec=*": host_refs.ec = parse_symbol_list(value)
            case "-host-ref-ic=*": host_refs.ic = parse_symbol_list(value)
            case "-host-ref-eg=*": host_refs.eg = parse_symbol_list(value)
            case "-host-ref-ig=*": host_refs.ig = parse_symbol_list(value)

    # ---- Phase B: Build preserved-symbol sets ----
    # Collect symbols from llvm.used and llvm.metadata named metadata
    used_set = collect_named_metadata(module, "llvm.used")
    metadata_set = collect_named_metadata(module, "llvm.metadata")

    # Merge host reference tables into a unified "referenced from host" set.
    # The 6 host-ref flags encode three entity types x two reference modes:
    #   e = explicit reference (symbol name appears in host launch site)
    #   i = implicit reference (symbol address taken on host side)
    #   k = kernel (__global__),  c = constant (__constant__),  g = global (__device__)
    host_referenced_kernels  = host_refs.ek  UNION  host_refs.ik
    host_referenced_constants = host_refs.ec  UNION  host_refs.ic
    host_referenced_globals   = host_refs.eg  UNION  host_refs.ig

    # ---- Phase C: Decide what to preserve ----
    preserved = used_set  UNION  metadata_set  UNION  host_referenced_kernels

    if NOT optimize_unused_vars:
        preserved = preserved  UNION  host_referenced_constants
                               UNION  host_referenced_globals

    # ---- Phase D: Dead kernel/variable elimination ----
    if mode == GEN_LTO:
        # gen-lto: emit bitcode only, skip elimination
        return emit_lto_bitcode(module)

    if has_host_info:
        dead_kernel_elimination(module, preserved, trace_enabled)

        if optimize_unused_vars:
            dead_variable_elimination(module, preserved,
                                     host_referenced_constants,
                                     host_referenced_globals,
                                     trace_enabled)

    # ---- Phase E: Run the 5-pass LTO pipeline ----
    if mode == LINK_LTO or mode == FULL_LTO:
        run_module_summary_builder(module)      # sub_D7D4E0 via sub_D81040
        run_thinlto_import(module)              # sub_1854A20  (if ThinLTO)
        run_inliner(module)                     # sub_1864060 + sub_2613930
        run_globalopt(module)                   # sub_18612A0
        run_whole_program_devirt(module)        # sub_2703170
        run_global_dce(module)                  # final GlobalDCE sweep

    # ---- Phase F: Hand off to optimizer pipeline ----
    return module    # returned to sub_12E7E70 for tier 0-3 passes
```

### Host Reference Flag Encoding

The six `-host-ref-*` flags are the mechanism by which `nvlink` communicates host-side symbol usage to cicc's LTO pass. `nvlink` inspects the host-side relocatable objects and emits a semicolon-separated list of device symbol names for each flag. The two-letter suffix encodes:

| Suffix | Entity Type | Reference Kind |
|--------|-------------|----------------|
| `-host-ref-ek` | `__global__` kernel | Explicit (launch site in host code) |
| `-host-ref-ik` | `__global__` kernel | Implicit (address taken, e.g. `&myKernel`) |
| `-host-ref-ec` | `__constant__` variable | Explicit (`cudaMemcpyToSymbol` target) |
| `-host-ref-ic` | `__constant__` variable | Implicit (address taken) |
| `-host-ref-eg` | `__device__` global variable | Explicit (`cudaMemcpyToSymbol` target) |
| `-host-ref-ig` | `__device__` global variable | Implicit (address taken) |

The `-has-global-host-info` flag signals that `nvlink` has provided complete host reference information. When this flag is absent, `sub_12F5F30` conservatively preserves all externally-visible symbols -- the dead kernel/variable elimination pass is skipped entirely.

### Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| `sub_12F5F30` | `0x12F5F30` | 37.8 KB | LTO pipeline entry and dead-symbol orchestrator |
| `sub_12F5610` | `0x12F5610` | 7.3 KB | LLVM module linker wrapper (`Linker::linkModules`) |
| `sub_12F7D90` | `0x12F7D90` | 14.3 KB | CLI argument parser (architecture, opt level, flags) |
| `sub_12F4060` | `0x12F4060` | 15.7 KB | TargetMachine creation with NVIDIA options |
| `sub_1C13840` | `0x1C13840` | -- | Global/function iterator used for dead-code sweep |
| `sub_12F1650` | `0x12F1650` | 5.2 KB | Bitcode reader variant A |
| `sub_12F11C0` | `0x12F11C0` | 5.2 KB | Bitcode reader variant B |

## Dead Kernel Elimination Algorithm

Dead kernel elimination is the most impactful GPU-specific optimization in the LTO pipeline. It exploits the closed-world model: every `__global__` function that will ever execute must have a corresponding `<<<>>>` launch site (or `cudaLaunchKernel` call) in the host code that `nvlink` has already seen. Any kernel not in the host reference set is dead.

This pass cannot exist on CPU. A CPU linker must preserve all non-hidden external functions because shared libraries loaded at runtime via `dlopen` could call them. On GPU there is no `dlopen`, no dynamic symbol resolution, no PLT. The set of reachable kernels is completely determined at link time.

### Pseudocode: `dead_kernel_elimination`

```
function dead_kernel_elimination(module, preserved_set, trace):
    # Walk all functions in the module via sub_1C13840 iterator
    worklist = []

    for func in module.functions():
        if func.isDeclaration():
            continue

        cc = func.getCallingConv()

        # PTX calling convention 71 = __global__ (kernel entry point)
        # PTX calling convention 72 = __device__ (device function)
        # PTX calling convention 95 = CUDA internal (managed init)
        if cc != 71:
            continue    # only eliminate kernels, not device functions

        name = func.getName()

        if name in preserved_set:
            continue    # referenced from host, or in llvm.used -- keep it

        # This kernel has no host launch site.
        if trace:
            emit_diagnostic("no reference to kernel " + name)

        worklist.append(func)

    # ---- Remove dead kernels ----
    for func in worklist:
        # Before erasing, check if any device-side indirect references exist.
        # On GPU, device-side function pointers (callback patterns) can reference
        # kernels via address-of. Check use_empty():
        if NOT func.use_empty():
            # Has device-side users -- cannot safely remove.
            # (This is rare: kernels are almost never called from device code.)
            continue

        func.replaceAllUsesWith(UndefValue)
        func.eraseFromParent()

    return len(worklist)
```

### Pseudocode: `dead_variable_elimination`

When `-optimize-unused-variables` is enabled, the same logic extends to `__device__` and `__constant__` global variables:

```
function dead_variable_elimination(module, preserved_set,
                                   host_constants, host_globals, trace):
    worklist = []

    for gv in module.globals():
        if gv.isDeclaration():
            continue

        name = gv.getName()

        if name in preserved_set:
            continue

        as = gv.getAddressSpace()

        # Address space 1 = global, address space 4 = constant
        if as == 4 and name NOT in host_constants:
            if trace:
                emit_diagnostic("no reference to variable " + name)
            worklist.append(gv)
        elif as == 1 and name NOT in host_globals:
            if trace:
                emit_diagnostic("no reference to variable " + name)
            worklist.append(gv)

    for gv in worklist:
        if NOT gv.use_empty():
            continue    # still referenced from device code
        gv.eraseFromParent()

    return len(worklist)
```

The `--trace-lto` CLI flag (which maps to `--trace` in the LTO argument vector via the flag catalog at line 2394) enables the diagnostic messages. When active, cicc prints one line per eliminated symbol to stderr, enabling build-system integration and debugging of unexpected kernel removal.

## Module Merge Process

Before `sub_12F5F30` can perform dead-kernel elimination or any LTO optimization, the separate-compilation bitcode modules must be merged into a single LLVM module. This merge happens in two layers: the NVIDIA module linker wrapper `sub_12F5610` (7.3 KB) and the underlying LLVM `IRLinker` at `sub_16786A0` (61 KB).

### Two-Level Linking Architecture

```
nvlink extracts .nv_fatbin bitcode sections
         |
         v
┌─────────────────────────────────────────────────────────────┐
│  NVIDIA Module Loader  (sub_12C06E0, 63 KB)                │
│  - Validates LLVM bitcode magic (0xDEC0170B or 0x4243C0DE) │
│  - Checks IR version via sub_12BFF60                        │
│  - Validates target triple (must be "nvptx64-*")            │
│  - Single-module fast path: return directly if N=1          │
│  - Multi-module: normalize triples, set matching DataLayout │
└─────────────────────┬───────────────────────────────────────┘
                      │  N validated modules
                      v
┌─────────────────────────────────────────────────────────────┐
│  NVIDIA Module Linker Wrapper  (sub_12F5610, 7.3 KB)       │
│  - Selects primary module (typically the largest)           │
│  - For each secondary module:                               │
│      Copy triple from primary → secondary                   │
│      Call IRLinker to merge secondary into primary           │
│  - Post-link: restore linkage attributes from hash table    │
│      Values 7-8: external linkage (low 6 bits)              │
│      Other: set low 4 bits + visibility from bits 4-5       │
│      Set dso_local flag (byte+33 |= 0x40)                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      v
┌─────────────────────────────────────────────────────────────┐
│  LLVM IRLinker::run  (sub_16786A0, 61 KB)                   │
│  - Allocates 0x2000-byte DenseMap for symbol resolution     │
│  - Hash function: (addr >> 9) ^ (addr >> 4)                 │
│  - Resolves COMDAT groups (sub_167DAB0, 39 KB)              │
│  - Links global value prototypes (sub_1675980, 37 KB)       │
│  - Links function bodies (sub_143B970, 14 KB)               │
│  - Merges named metadata (llvm.dbg.cu, llvm.used, etc.)     │
│  - Resolves llvm.global_ctors / llvm.global_dtors ordering  │
│  - Maps values across modules via DenseMap<Value*, Value*>  │
│  - Tombstone sentinels: empty=-8, deleted=-16               │
└─────────────────────┬───────────────────────────────────────┘
                      │  single merged module
                      v
              sub_12F5F30 (LTO pipeline entry)
```

### Pseudocode: Module Merge (`sub_12F5610` + `sub_12C06E0`)

```
function module_merge(module_list, llvm_ctx, options):
    # ---- Step 1: Load and validate all modules (sub_12C06E0) ----
    modules = []
    for entry in module_list:
        buf = open_buffer(entry.data, entry.length, entry.name)

        # Validate bitcode magic
        magic = read_u32(buf, 0)
        if magic != 0x0B17C0DE and magic != 0xDEC04342:
            error("invalid bitcode: " + entry.name)
            return NULL

        module = parse_bitcode(buf, llvm_ctx)  # sub_15099C0

        # Check IR version compatibility (sub_12BFF60)
        if ir_version_check(module_list, module, flags) != 0:
            error(entry.name + ": error: incompatible IR detected. "
                  "Possible mix of compiler/IR from different releases.")
            return NULL

        # Validate target triple
        triple = module.getTargetTriple()
        if NOT triple.startswith("nvptx64-"):
            error("Module does not contain a triple, "
                  "should be 'nvptx64-'")
            return NULL

        modules.append(module)

    # ---- Step 2: Single-module fast path ----
    if len(modules) == 1:
        return modules[0]

    # ---- Step 3: Multi-module linking (sub_12F5610) ----
    # Save linkage attributes before linking (they get modified)
    linkage_map = DenseMap<StringRef, u8>{}
    for module in modules:
        for func in module.functions():
            linkage_map[func.getName()] = func.getLinkage()
        for gv in module.globals():
            linkage_map[gv.getName()] = gv.getLinkage()

    # Select primary module and link secondaries into it
    primary = modules[0]
    for i in range(1, len(modules)):
        secondary = modules[i]

        # Normalize: copy DataLayout from primary to secondary
        secondary.setDataLayout(primary.getDataLayout())
        secondary.setTargetTriple(primary.getTargetTriple())

        # IRLinker::run (sub_16786A0)
        # Resolves COMDATs, links globals, maps values, merges metadata
        err = Linker::linkModules(primary, secondary)
        if err:
            error("<module_name>: link error: <details>")
            return NULL

    # ---- Step 4: Restore linkage attributes ----
    # During linking, LLVM may promote linkage (e.g., internal -> external)
    # to resolve cross-module references. Restore the original linkage
    # where possible, preserving the correct visibility for PTX emission.
    for func in primary.functions():
        name = func.getName()
        if name in linkage_map:
            original = linkage_map[name]
            if original in [7, 8]:       # external linkage variants
                func.setLinkage(original & 0x3F)
            else:
                func.setLinkage(original & 0x0F)
                if (original & 0x30) != 0:
                    func.setVisibility(original >> 4)
            func.setDSOLocal(true)       # byte+33 |= 0x40

    for gv in primary.globals():
        # same linkage restoration logic
        ...

    return primary
```

### Key Data Structures in the Merge

| Structure | Location | Details |
|-----------|----------|---------|
| Value map DenseMap | Allocated in `sub_16786A0` | 0x2000 bytes (8192), hash: `(addr >> 9) ^ (addr >> 4)`, quadratic probing |
| Linkage hash table | Stack-allocated in `sub_12E1EF0` (v362) | Maps `StringRef` name to original linkage byte |
| Function-to-module map | Stack-allocated in `sub_12E1EF0` (v359) | Maps `StringRef` name to function pointer for split-module dispatch |
| COMDAT group map | Internal to `sub_167DAB0` | Tracks COMDAT selection kinds: any / exact-match / largest / no-dup / same-size |
| Named metadata merge list | Internal to `sub_1671B40` | Special handling for `llvm.dbg.cu`, `llvm.used`, `llvm.compiler.used`, `llvm.global_ctors`, `llvm.global_dtors`, `llvm.global.annotations` |
| Module config flag | `dword_4F99BC0` | Controls linker behavior variant |

### Split-Module Compilation and Re-Linking

When concurrent compilation is active (thread count > 1 and multiple defined functions), the optimization pipeline uses a split-module strategy: each function is extracted into its own bitcode module, optimized independently in a thread pool, and then re-linked. The split/re-link cycle uses the same `sub_12F5610` linker wrapper:

1. **Split** (`sub_1AB9F40`): extracts per-function bitcode using a filter callback (`sub_12D4BD0`) that selects a single function by name from the function-to-module hash table.
2. **Optimize** (thread pool via `sub_16D5230`): each worker runs `sub_12E86C0` (Phase II optimizer) with `qword_4FBB3B0 = 2`.
3. **Re-link** (`sub_12F5610`): merges all per-function bitcode modules back into a single module.
4. **Restore linkage** (v362 hash table): the saved linkage attributes from step 0 are written back to prevent linkage promotion artifacts.

This cycle is orchestrated by `sub_12E1EF0` (51 KB, the top-level concurrent compilation entry). The GNU Jobserver integration (`sub_16832F0`) throttles thread pool size to match the build system's `-j` level when cicc is invoked from `make`.

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
