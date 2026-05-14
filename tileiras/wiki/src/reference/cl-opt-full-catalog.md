# cl::opt Full Catalog

Every command-line option the `tileiras` binary registers — through LLVM `cl::opt` / `cl::list` / `cl::alias`, the NVIDIA-private dialect-bag, and MLIR PassOption registrars that share its textual surface. An option counts if a static-storage symbol calls `llvm::cl::Option::setArgStr(name, len)` (`sub_4534CC0`, 1174 B) at static-construction time, if a tileiras dialect-bag helper (`sub_5FE350` / `sub_5FE910` / `sub_5FED40`) runs from a per-invocation builder, or if `mlir::detail::PassOptions::Option<T>` is constructed from `sub_6D3460`. The binary contains 689 distinct caller addresses for `sub_4534CC0`; this catalog covers the 77 user-visible options across registrars 1–7. The 478-row PassBuilder name registry is summarized in `pipeline/passbuilder-mega-registry.md`.

## Reading guide

Option families surface to the user through different CLI prefixes. The first stop depends on which prefix appears on the command line:

| If you see... | Find it in... | Examples |
|---|---|---|
| bare `--opt-level`, `-O`, `-g` | Layer 1 (Driver Globals) | `--opt-level`, `--gpu-name`, `--sanitize` |
| bare `--compute-capability`, `--debuginfo-level` | Layer 2 (Dialect Options Bag) | targets pre-driver loop |
| `--pass-pipeline="tileir{key=value ...}"` | Layer 3 (TileIR PassOptions) | `compute-capability=sm_90`, `num-warps=8`, `opt-level=2` |
| `-mllvm -nvptx-...` | Layer 4 (NVPTX Backend) | upstream LLVM NVPTX target options |
| bare `-Om`, `-Osize`, `-w`, `-Werror` | Layer 4b (NVPTX-CL Options) | host-driver-mode compatibility flags |
| `-mllvm -nvvm-reflect-*` | Layer 5 (NVVM Reflect) | `nvvm-reflect-add KEY=VAL`, `-R KEY=VAL` |
| `--mlir-...` | Layer 6 (MLIR Framework) | `--mlir-print-ir-after-all`, `--mlir-timing` |
| `--passes="..."` pass-name strings | PassBuilder mega-registry | not user-visible cl::opts; see [`pipeline/passbuilder-mega-registry`](../pipeline/passbuilder-mega-registry.md) |

When the same name appears in two layers (the documented `--compute-capability` collision between Layer 2 and Layer 3 is the canonical example), the driver propagates the Layer-2 value down into Layer 3; the Layer-3 default fires only when the MLIR pass library is loaded outside the tileiras driver.

## Registrar Tiers

The static-init-time CLI surface partitions into five disjoint registrars plus two LLVM-inherited bulk registrars. Each tier owns its builder body, its storage scheme, and its help-text rodata cluster.

| Tier | Count | Registrar function | Storage scheme | End-user prefix |
|------|------:|--------------------|----------------|-----------------|
| Driver-tier (Layer 1)        |  5  | `sub_579270` (3834 B), called by thunk `sub_57A170` from `main` | Heap-allocated 864-byte `TileirasDriverCLOpts` aggregate | bare (`--opt-level`, `-O`, `--lineinfo`, `--device-debug`, `-g`) |
| Dialect-bag (Layer 2)        |  3  | `sub_602440` (1599 B), called from `sub_602A80` per invocation | Heap-allocated ~1488-byte dialect options bag | bare (parsed alongside Layer 1) |
| TileIR PassOptions (Layer 3) | 20  | `sub_6D3460` (13726 B); helpers `sub_6D3140` (int), `sub_6D2E20` (uint/size_t), `sub_5FED40` (bool), `sub_4534CC0` (string/enum), `sub_44E10F0` (string default setter) | Caller-owned `TileIRPipelineOptions` struct, ≥5616 B | `--pass-pipeline="tileir{...}"` |
| NVPTX backend (Layer 4)      | 26  | LLVM static ctors, per TU within `NVPTXTargetMachine` | Per-TU BSS globals, 160–200 B per option | `-mllvm -nvptx-*` |
| NVVM reflect (Layer 5)       |  4  | `ctor_238` (`0x463A70`); cl::list backing at `0x5B4F380` | Static `cl::opt<bool>` at `0x5B4F400`, `cl::list<string>` at `0x5B4F300` | `-mllvm -nvvm-reflect-*` |
| MLIR framework (Layer 6)     | 18  | MLIR support-library static ctors (AsmPrinter / Diagnostics / MLIRContext / PassTiming) | Per-TU BSS globals | `--mlir-*` |
| NVPTX-CL options (Layer 4b)  | 13  | `sub_45BA4C0` (8524 B), ManagedStatic-guarded; `__cxa_atexit` per opt | Static globals, 13 `__cxa_atexit` registrations | bare (no `-mllvm` prefix) |
| Misc LLVM (Layer 7)          |  1  | LLVM static ctor in `ValueTracking.cpp` | Per-TU BSS global | `-mllvm` |
| **PassBuilder mega-registry** | 478 | `sub_1CCB7D0` (35948 B) | StringMap<PassInfo> at `*(this+8)` | name-registry only; not a CLI option |
| **Driver-tier total (1+2)**  |  8  | | | |
| **Pass-options surface (3)** | 47  | (counts include the helper-distinguished int/uint/bool/string/enum sub-types) | | |
| **Total user-visible**       | 77  | | | |
| **Total `setArgStr` xrefs**  | 689 | (LLVM/MLIR/Target full link graph) | | |

Static-init order follows ELF `.init_array`; each ctor invokes `setArgStr(opt, name, len)` then `done()` (`sub_4534420`, 199 B), inserting into `cl::GlobalParser` (`sub_4530050`). Categories attach via `sub_452D690` (`OptionCategory::addOption`, 455 B). The atomic counter `NumOccurrences` (`sub_452D580`, 88 B) uses `InterlockedExchangeAdd64`. Standard cl::alias diagnostics live at `0x45E7218`/`0x45E7258`/`0x45E7288`/`0x45E72C0` (verbatim upstream) and route through `sub_452D9F0` → `sub_459CCA0`; the duplicate-registration error "` registered more than once!`" is emitted from `setArgStr` itself.

## Master Table

Columns: `option | type | default | help text (verbatim) | storage / agg offset | defining pass / TU | wiki page`. Sorted alphabetically within each registrar. Where the per-option BSS storage was not individually extracted, the rodata address of the name string is given as `name@<addr>`.

### Layer 1 — Tileiras Driver Globals (registrar `sub_579270`)

Aggregate: heap-allocated 864 B `TileirasDriverCLOpts`, owner pointer returned to `main` via `sub_57A170`. cl::opt slots are 192 B; the two cl::alias slots are 136 B. Applicator trampolines are `sub_578C40` (int) and `sub_578C50` (bool), paired with `nullsub_10` / `nullsub_11`.

| option | type | default | help text (verbatim) | storage | defining pass | wiki |
|--------|------|---------|----------------------|---------|---------------|------|
| `--device-debug` | `cl::opt<bool>` | `false` | `Generate debug information (if present in the input bytecode)` | `name@(len 12)`, agg+`0x218..0x2D7` | `sub_579270` | [driver/cli-options](../driver/cli-options.md) |
| `-g` | `cl::alias` → `device-debug` | — | `Alias for --device-debug` | `name@0x45E74F9 (len 1)`, agg+`0x2D8..0x35F` | `sub_579270` | [driver/cli-options](../driver/cli-options.md) |
| `--lineinfo` | `cl::opt<bool>` | `false` | `Generate line-number information (if present in the input bytecode)` | `name@0x45E74F0 (len 8)`, agg+`0x158..0x217` | `sub_579270` | [driver/cli-options](../driver/cli-options.md) |
| `-O` | `cl::alias` → `opt-level` | — | `Alias for --opt-level` | `name (len 1)`, agg+`0x0D0..0x157` | `sub_579270` | [driver/cli-options](../driver/cli-options.md) |
| `--opt-level` | `cl::opt<int>` | `3` | `Specify optimization level. Default Value: 3.` | `name@0x45E74xx (len 9)`, agg+`0x000..0x0C7` | `sub_579270` | [driver/cli-options](../driver/cli-options.md) |

`--opt-level` `ValueStr` metavar = `"N"` (renders as `--opt-level=<N>`). Aggregate heap-allocated by `sub_44A8C20(0x360)`.

#### Layer 1 — `cl::ValuesClass` int32 enum options

Four additional Layer-1 options are wired by byte-equivalent template instantiations of `cl::opt<cl::ValuesClass>::opt`, each differing only in its string-pair table, parser vtable, and the int32 target slot it writes into the aggregate. The parsed result is always a single int32; downstream code consults the integer, never the string.

| option | builder | parser vtable | default | int32 codes |
| --- | --- | --- | ---: | --- |
| `--gpu-name` | `sub_577620` (5-pair table) | `&unk_59A7378` | `100` | `"sm_100"`=100, `"sm_103"`=103, `"sm_110"`=110, `"sm_120"`=120, `"sm_121"`=121 |
| `--host-arch` | `sub_577950` (3-pair table) | `&unk_59A7468` | `0` | `"x86_64"`=0, `"aarch64"`=1, `"arm64ec"`=2 |
| `--host-os` | `sub_577C80` (2-pair table) | `&unk_59A7558` | `0` | `"linux"`=0, `"windows"`=1 |
| `--sanitize` | `sub_577FB0` (1-pair table) | `&unk_59A7648` | `0` | (unset)=0, `"memcheck"`=1 |

GPU-name codes correspond to compute-capability families: 100 is Datacenter Blackwell (default), 103 the Blackwell variant, 110 Jetson Thor, 120 Consumer RTX 50** / Pro, 121 DGX Spark. `--sanitize` is the toggle that activates the `-sanitize=memcheck -g-tmem-access-check` nvdisasm tail.

Host-triple resolution reads these ints downstream. `sub_40FD330` keys off the `host-arch` int with stride 39 for `x86_64` (code 0), stride 36 for `aarch64` (code 1), and stride 36 for `arm64ec` (code 2 — which uses a sub-entry of the aarch64 record); this is the only place `arm64ec` diverges from `aarch64`. `sub_40FD7E0` keys off the `host-os` int with OS-index 7 for `linux` (code 0) and OS-index 15 for `windows` (code 1).

The four parser vtables `&unk_59A7378` / `&unk_59A7468` / `&unk_59A7558` / `&unk_59A7648` share an 8-slot layout: vtable+0 typeinfo helper, +8 destructor, +16 `parse` (string → int32 map probe), +24 `print` (int32 → string lookup), +32 `valuesDefault` (initialise from a `cl::values(...)` builder), +40 reserved, +48 reserved, +56 reserved. The `parse` slot is the only operation invoked at command-line-parse time; the `print` slot fires only when `--help` is requested.

### Layer 2 — Tileiras Dialect Options Bag (registrar `sub_602440`)

Aggregate: ~1488 B heap-allocated dialect bag attached to a per-invocation `mlir::DialectRegistry`. Not visible through the global LLVM parser; consumed by tileiras's pre-driver loop and forwarded to Layer 3. Helpers: `sub_5FE350` (enum), `sub_5FE910` (string), `sub_5FED40` (bool).

| option | type | default | help text (verbatim) | storage | defining pass | wiki |
|--------|------|---------|----------------------|---------|---------------|------|
| `--compute-capability` | `cl::opt<string>` | `sm_100` | (metavar `compute capability`, no description body) | `name@0x4Exxxxx (len 18)`, bag+`0x4D8..0x5C7`; default literal `"sm_100"`@`0x45E7185` | `sub_602440` | [driver/cli-options](../driver/cli-options.md) |
| `--debuginfo-level` | `cl::opt<enum>` (4-value) | `none` (=0) | `The level of debug info to emit.` | `name@0x45EF0xx (len 15)`, bag+`0x070..0x1EF` | `sub_602440` | [lowering/target-and-debuginfo](../lowering/target-and-debuginfo.md) |
| `--is-optimized` | `cl::opt<bool>` | `false` | `Encode in the debug info whether the program is optimized or not.` | `name@0x45EF0xx (len 12)`, bag+`0x400..0x4D7` | `sub_602440` | [lowering/target-and-debuginfo](../lowering/target-and-debuginfo.md) |

Enum table for `--debuginfo-level` (constructed inline by `sub_602440`):

| value | string | help (verbatim) |
|------:|--------|-----------------|
| 0 | `none` | `None.` |
| 1 | `full` | `Full.` |
| 2 | `line-tables` | `Line Tables Only.` |
| 3 | `debug-directives` | `Debug Directives Only.` |

Note: `--compute-capability` collides with the Layer-3 PassOption of the same name (Layer 2 defaults to `sm_100`, Layer 3 to `sm_80`). The driver propagates Layer 2 into Layer 3; the Layer-3 default fires only when the MLIR passes load without the tileiras driver.

### Layer 3 — TileIR MLIR PassOptions (registrar `sub_6D3460`)

20 `mlir::Pass::Option<T>` registrations on a single `TileIRPipelineOptions` (≥5616 B). Not in the global LLVM cl::opt registry; parsed from `--pass-pipeline="tileir{key=value ...}"`. `reg+N` is the registration slot (208 B stride); `val+N` is the resolved-value offset in the `a2` value-struct read by `sub_6D6A00`. For per-option consumer mapping see `pipeline/pass-options-mapping.md`.

| option | type | default | help text (verbatim) | storage / agg+slot | consuming pass(es) | wiki |
|--------|------|---------|----------------------|--------------------|--------------------|------|
| `approx` | `bool` | `0` | `Approximate calculation.` | reg+`2104`, val~+`?` | NVVMReflect path (sub_14FE980) | [libdevice/nvvm-reflect-mechanism](../libdevice/nvvm-reflect-mechanism.md) |
| `compute-capability` | `string` | `sm_80` | `compute capability` | reg+`600`, val+`728/736` | sub_738810 (Frontend→TileAA), sub_6D0E90 | [driver/cli-options](../driver/cli-options.md) |
| `dump-host` | `string` | `""` | `Print the generated host code to the provided path.` | reg+`4912`, val+`5040/5048`, presence+`5072` | sub_879B50 (EmitHostWrapper) | [driver/tileir-callbacks-abi](../driver/tileir-callbacks-abi.md) |
| `dynamic-persistent` | `bool` | `0` | `Enable dynamic persistent transformation` | reg+`2936`, val+`3064` | TileASDynamicPersistent (driver gate) | [passes/tileas/cta-cluster-family](../passes/tileas/cta-cluster-family.md) |
| `emit-line-info` | `enum` (5-value) | `none` | `Emit debug line info from existing or snapshot IR (snapshot saved to ./snapshot.mlir).` | reg+`3408`, val+`3536` | driver (snapshot insertion); SynthesizeDebugInfoScopes | [lowering/target-and-debuginfo](../lowering/target-and-debuginfo.md) |
| `enable-debug-logging` | `bool` | `0` | `Enable debug logging in TileIR host callbacks.` | reg+`4024`, val+`4152` | sub_879B50 EmitHostWrapper | [driver/tileir-callbacks-abi](../driver/tileir-callbacks-abi.md) |
| `enable-random-delay` | `bool` | `0` | `enable random delay` | reg+`2520` | TileAS scheduler family (LOW conf) | [scheduler/overview](../scheduler/overview.md) |
| `ftz` | `bool` | `0` | `Flush denormal to zero.` | reg+`2312`, val+`2232` | NVVMReflect path (`nvvm-reflect-ftz` ModuleFlag) | [libdevice/nvvm-reflect-mechanism](../libdevice/nvvm-reflect-mechanism.md) |
| `host-triple` | `string` | `native` | `Specify the target triple for TileIR host callbacks.` | reg+`4232`, val+`4360` | sub_879B50 EmitHostWrapper | [driver/tileir-callbacks-abi](../driver/tileir-callbacks-abi.md) |
| `index-bitwidth` | `int` | `32` | `Bitwidth of the index type, 0 to use size of machine word.` | reg+`1688` | ConvertTileASToLLVM, TileAS→NVGPU, ConvertToLLVM, ConvertMemRefToLLVM, ConvertControlFlowToLLVM, UnspecializedPipeline | [lowering/tileas-to-llvm](../lowering/tileas-to-llvm.md) |
| `max-constraint-iterations` | `uint` | `10` | `Maximum number of iterations for resource constraint generation. Higher values allow more optimization attempts but increase compilation time. Lower values may result in fallback to serial execution when resource constraints are tight.` | reg+`4704`, val+`4832` | sub_8A25E0 TileASPrepareForScheduling | [passes/tileas/cta-cluster-family](../passes/tileas/cta-cluster-family.md) |
| `num-ctas` | `int` | `1` | `number of ctas in a cga` | reg+`392`, val+`520` | sub_738810 (Frontend→TileAA) | [lowering/cuda-tile-to-tileaa](../lowering/cuda-tile-to-tileaa.md) |
| `num-warps` | `int` | `4` | `number of warps` | reg+`184`, val+`312` | sub_738810 (Frontend→TileAA) | [lowering/cuda-tile-to-tileaa](../lowering/cuda-tile-to-tileaa.md) |
| `opt-level` | `int` | `2` | `Optimization level for NVVM compilation. Please notice that the default value is 2 and can be set from 0 to 3.` | reg+`864`, val+`992` | driver `sub_6D6A00` shape switch; ConvertTargetToNVVM (sub_14FE980) | [pipeline/driver-and-opt-levels](../pipeline/driver-and-opt-levels.md) |
| `pipeline-strategy` | `enum` (3-value) | `none` | `Select the strategy of pipelining optimization.` | reg+`1072`, val+`1200` | driver `sub_6D6A00` / `sub_6D0E90` / `sub_6D18D0` (warp-specialize selector); SpecializeAgents | [passes/tileas/async-pipeline-family](../passes/tileas/async-pipeline-family.md) |
| `rrt-size-threshold` | `uint` | `4096` | `RRT size threshold for quantization (in time slots). Applies quantization when RRT exceeds this size to reduce compilation time at the cost of schedule accuracy. Smaller thresholds enable more compression for faster compilation but with reduced scheduling precision. If threshold is 0, then no quantization will be applied.` | reg+`4496`, val+`4624` | sub_8A25E0 TileASPrepareForScheduling | [passes/tileas/cta-cluster-family](../passes/tileas/cta-cluster-family.md) |
| `schedule-trace-file` | `string` | `""` | `Generate a chrome timeline trace if not empty for the visualizationof the scheduling result for TileASv2` | reg+`3144`, val+`3272` | sub_825050 TileASScheduleRewriteEnable | [scheduler/overview](../scheduler/overview.md) |
| `unspecialized-pipeline-num-stages` | `int` | `4` | `numStages for unspecialized pipeline pass.` | reg+`1896`, val+`1816` | UnspecializedPipeline (sub_1A24770), ConvertTileASToLLVM, TileAS→NVGPU | [passes/tileas/async-pipeline-family](../passes/tileas/async-pipeline-family.md) |
| `use-nvgpucomp-libnvvm` | `bool` | `0` | `Use NVGpuComp to compile NVVM IR. If false, use default libnvvm path.` | reg+`5488`, val+`5616` | ConvertTargetToNVVM (sub_14FE980) | [lowering/nvgpu-and-gpu-to-nvvm](../lowering/nvgpu-and-gpu-to-nvvm.md) |
| `v2-opt-level` | `int` | `0` | `Optimization level for tile_ir V2 pass pipeline.` | reg+`2728`, val+`2856` | driver `sub_6D6A00` second-axis shape gate | [pipeline/driver-and-opt-levels](../pipeline/driver-and-opt-levels.md) |

Enum tables for Layer-3 enums:

| option | value | string | help (verbatim) |
|--------|------:|--------|-----------------|
| `pipeline-strategy` | 0 | `none` | `no pipelining optimization` |
| `pipeline-strategy` | 1 | `unspecialize` | `do pipelining for unspecialized flow` |
| `pipeline-strategy` | 2 | `warp-specialize` | `do pipelining for warp specialized flow` |
| `emit-line-info` | 0 | `none` | `Do not emit line info.` |
| `emit-line-info` | 1 | `inputIR` | `Emit line info from the existing input IR.` |
| `emit-line-info` | 2 | `tileaa` | `Emit line info from TileAA IR snapshot before lowering to TileAS.` |
| `emit-line-info` | 3 | `tileas` | `Emit line info from TileAS IR snapshot before lowering to LLVM.` |
| `emit-line-info` | 4 | `post-tileas` | `Emit line info from Cute and LLVM IR snapshot.` |

### Layer 4 — NVPTX Backend cl::opt (LLVM static ctors)

Per-TU global ctors invoking `sub_4534CC0(&optObj, name, len)` at static-init. Eight rows below are `INITIALIZE_PASS` markers (pass-name registrations, not cl::opt). The set is verbatim upstream LLVM `NVPTXTargetMachine`; default for `nvptx-force-min-byval-param-align` is patched to `false` (upstream = `true`).

| option | type | default | help text (verbatim) | storage (string addr) | defining pass | wiki |
|--------|------|---------|----------------------|-----------------------|---------------|------|
| `alloca-hoisting` | INITIALIZE_PASS | — | `NVPTX specific alloca hoisting` | `name@0x4D12164` | NVPTXAllocaHoisting | [nvptx-passes/overview](../nvptx-passes/overview.md) |
| `disable-nvptx-load-store-vectorizer` | `cl::opt<bool>` | `false` | `Disable load/store vectorizer` | `name@0x4D0EF60` | LSV gate | [nvptx-passes/overview](../nvptx-passes/overview.md) |
| `disable-nvptx-require-structured-cfg` | `cl::opt<bool>` | `false` | `Transitional flag to turn off NVPTX's requirement on preserving structured CFG. The requirement should be disabled only when unexpected regressions happen.` | `name@0x4D0EF88` | NVPTXTargetMachine | [codegen/nvptx-bring-up-and-target-init](../codegen/nvptx-bring-up-and-target-init.md) |
| `nvptx-aa-wrapper` | INITIALIZE_PASS | — | `NVPTX Address space based Alias Analysis Wrapper` | `name@0x4D11FB1` | NVPTXAliasAnalysisWrapper | [nvptx-passes/memory-space-opt-and-process-restrict](../nvptx-passes/memory-space-opt-and-process-restrict.md) |
| `nvptx-approx-log2f32` | `cl::opt<bool>` | `false` | `NVPTX Specific: whether to use lg2.approx for log2` | `name@0x4D0DA2D` | NVPTXISelLowering | [libdevice/math-pass-pipeline-and-crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) |
| `nvptx-asm-printer` | INITIALIZE_PASS | — | `NVPTX Assembly Printer` | `name@0x4D07A97` | NVPTXAsmPrinter | [codegen/asm-printer-monster-and-windows](../codegen/asm-printer-monster-and-windows.md) |
| `nvptx-assign-valid-global-names` | INITIALIZE_PASS | — | `Assign valid PTX names to globals` | `name@0x4D121A0` | NVPTXAssignValidGlobalNames | [nvptx-passes/overview](../nvptx-passes/overview.md) |
| `nvptx-atomic-lower` | INITIALIZE_PASS | — | `NVPTX lower atomics of local memory` | `name@0x4D1221C` | NVPTXAtomicLower | [codegen/atomic-warp-sreg-fence](../codegen/atomic-warp-sreg-fence.md) |
| `nvptx-early-byval-copy` | `cl::opt<bool>` | `false` | `Create a copy of byval function arguments early.` | `name@0x4D0F141` | NVPTXLowerArgs | [nvptx-passes/lower-args-and-aggr-and-struct](../nvptx-passes/lower-args-and-aggr-and-struct.md) |
| `nvptx-emit-init-fini-kernel` | `cl::opt<bool>` | `false` | `Emit kernels to call ctor/dtor globals.` | `name@0x4D1262C` | NVPTXCtorDtorLowering | [nvptx-passes/kernel-cdp-inline-pretreat](../nvptx-passes/kernel-cdp-inline-pretreat.md) |
| `nvptx-exit-on-unreachable` | `cl::opt<bool>` | `false` | `Lower 'unreachable' as 'exit' instruction.` | `name@0x4D0F127` | NVPTXISelLowering | [codegen/nvptx-target-lowering-call-and-args](../codegen/nvptx-target-lowering-call-and-args.md) |
| `nvptx-fma-level` | `cl::opt<uint>` | (default per LLVM) | `NVPTX Specific: FMA contraction (0: don't do it 1: do it  2: do it aggressively` | `name@0x4D0D9DC` | NVPTXTargetMachine | [libdevice/math-pass-pipeline-and-crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) |
| `nvptx-force-min-byval-param-align` | `cl::opt<bool>` | `false` (NVIDIA-patched; upstream default = `true`) | `NVPTX Specific: force 4-byte minimal alignment for byval params of device functions.` | `name@0x4D0DBF0` | NVPTXLowerArgs | [nvptx-passes/lower-args-and-aggr-and-struct](../nvptx-passes/lower-args-and-aggr-and-struct.md) |
| `nvptx-forward-params` | INITIALIZE_PASS | — | `NVPTX Forward Params` | `name@0x4D12715` | NVPTXForwardParams | [nvptx-passes/overview](../nvptx-passes/overview.md) |
| `nvptx-isel` | INITIALIZE_PASS | — | `NVPTX DAG->DAG Pattern Instruction Selection` | `name@0x4D1293D` | NVPTXISelDAGToDAG | [codegen/iseldag-and-matchertable](../codegen/iseldag-and-matchertable.md) |
| `nvptx-libcall-callee` | `cl::opt<bool>` | (default per LLVM) | (controls direct libcall lowering; help colocated near `0x4D08070`) | `name@0x4D0805A` | NVPTXTargetLowering | [codegen/nvptx-target-lowering-call-and-args](../codegen/nvptx-target-lowering-call-and-args.md) |
| `nvptx-lower-global-ctor-dtor` | `cl::opt<bool>` | `false` | `Lower GPU ctor / dtors to globals on the device.` | `name@0x4D083A1` | NVPTXCtorDtorLowering | [nvptx-passes/kernel-cdp-inline-pretreat](../nvptx-passes/kernel-cdp-inline-pretreat.md) |
| `nvptx-lower-global-ctor-dtor-id` | `cl::opt<string>` | `""` | `Override unique ID of ctor/dtor globals.` | `name@0x4D12678` | NVPTXCtorDtorLowering | [nvptx-passes/kernel-cdp-inline-pretreat](../nvptx-passes/kernel-cdp-inline-pretreat.md) |
| `nvptx-no-f16-math` | `cl::opt<bool>` | `false` | `NVPTX Specific: Disable generation of f16 math ops.` | `name@0x4D0E6C4` | NVPTXISelLowering | [libdevice/math-pass-pipeline-and-crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) |
| `nvptx-prec-divf32` | `cl::opt<uint>` | (default per LLVM) | `NVPTX Specific: Override the precision of the lowering for f32 fdiv` | `name@0x4D0DA08` | NVPTXISelLowering (also reads `__CUDA_PREC_DIV` reflect key) | [libdevice/math-pass-pipeline-and-crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) |
| `nvptx-prec-sqrtf32` | `cl::opt<bool>` | `false` | `NVPTX Specific: 0 use sqrt.approx, 1 use sqrt.rn.` | `name@0x4D0DA1A` | NVPTXISelLowering (also reads `__CUDA_PREC_SQRT` reflect key) | [libdevice/math-pass-pipeline-and-crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) |
| `nvptx-rsqrt-approx-opt` | `cl::opt<bool>` | `false` | `Enable reciprocal sqrt optimization` | `name@0x4D15BF5` | NVPTXTargetLowering | [libdevice/math-pass-pipeline-and-crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) |
| `nvptx-sched4reg` | `cl::opt<bool>` | `false` | `NVPTX Specific: schedule for register pressue` | `name@0x4D0D9CC` | NVPTXSubtarget scheduler choice | [codegen/nvptx-subtarget-and-feature-matrix](../codegen/nvptx-subtarget-and-feature-matrix.md) |
| `nvptx-short-ptr` | `cl::opt<bool>` | `false` | `Use 32-bit pointers for accessing const/local/shared address spaces.` | `name@0x4D0F117` | NVPTXTargetMachine | [codegen/nvptx-subtarget-and-feature-matrix](../codegen/nvptx-subtarget-and-feature-matrix.md) |
| `nvptx-traverse-address-aliasing-limit` | `cl::opt<uint>` | (default per LLVM) | `Depth limit for finding address space through traversal` | `name@0x4D12070` | NVPTXAA | [nvptx-passes/memory-space-opt-and-process-restrict](../nvptx-passes/memory-space-opt-and-process-restrict.md) |
| `nvptx-use-max-local-array-alignment` | `cl::opt<bool>` | `false` | `Use maximum alignment for local memory` | `name@0x4D11F00` | NVPTXLowerArgs | [nvptx-passes/lower-args-and-aggr-and-struct](../nvptx-passes/lower-args-and-aggr-and-struct.md) |

Layer 4 also carries `nvptx-prec-divf32` enum value-strings: `Use div.approx`, `Use div.full`, `Use IEEE Compliant F32 div.rnd if available (default)`, `Use IEEE Compliant F32 div.rnd if available, no FTZ`.

### Layer 4b — NVPTX-CL Options Registrar (`sub_45BA4C0`)

A ManagedStatic-guarded static initializer at `sub_45BA4C0` (8524 B) registers exactly 13 `llvm::cl::opt` instances against the global registry. Each branch ends with `__cxa_atexit(dtor, &opt, &__dso_handle)`. These appear bare on the CLI (no `-mllvm` prefix).

| option | type | default | help text (verbatim) | storage / line | defining pass | wiki |
|--------|------|---------|----------------------|----------------|---------------|------|
| `debug-compile` | flag | `false` | `Compile for debugging` | `sub_45BA4C0:708` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `generate-line-info` | flag | `false` | `Emit line info even without -G` | `sub_45BA4C0:774` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `ignore-bad-fp` | flag | `false` | `Workaround Gdb problem in dumping floating-point constants` | `sub_45BA4C0:390` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `line-info-inlined-at` | flag | `false` | `Emit line with inlined-at enhancement` | `sub_45BA4C0:840` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `maxreg` | int (with `cl::value_desc`) | (none) | `max regcount` | `sub_45BA4C0:583` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `nvptx-f32ftz` | flag | `false` | (no description) | `sub_45BA4C0:198` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `nvptx-nan` | flag | `false` | (no description) | `sub_45BA4C0:134` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `Om` | flag | `false` | `Perform maximum optimization` | `sub_45BA4C0:518` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `Osize` | flag | `false` | `Optimize for code size` | `sub_45BA4C0:454` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `register-usage-level` | int | (none) | (no description) | `sub_45BA4C0:902` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `value-tracking-max-depth` | int | (none) | (no description) | `sub_45BA4C0:646` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `w` | `cl::alias` | — | `disable warnings` | `sub_45BA4C0:262` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |
| `Werror` | flag | `false` | `Treat all warnings as errors` | `sub_45BA4C0:326` | tileiras CLI | [driver/cli-options](../driver/cli-options.md) |

### Layer 5 — NVVM Reflect cl::opt (`ctor_238` at `0x463A70`)

Three registrations bundled into one TU ctor: a `cl::opt<bool>`, a `cl::list<std::string>`, and a `cl::alias`. List backing `std::vector<std::string>` at `0x5B4F380` (begin/end) + `0x5B4F390` (capacity); 32 B/entry. Atexit dtors: `sub_9C31D0` (opt), `sub_9C3B10` (list), `sub_9C3120` (alias).

| option | type | default | help text (verbatim) | storage | defining pass | wiki |
|--------|------|---------|----------------------|---------|---------------|------|
| `nvvm-reflect-add` | `cl::list<string>` | (empty) | `A key=value pair. Replace __nvvm_reflect(name) with value.` | obj@`0x5B4F300`, name@`0x4D3C77A`; metavar `name=<int>`@`0x4D3C78B`; list backing@`0x5B4F380`/`0x5B4F390` | NVVMReflectPass (`sub_1BD0910` / `sub_1BD0C50` / `sub_1BD1280`) | [libdevice/nvvm-reflect-mechanism](../libdevice/nvvm-reflect-mechanism.md) |
| `nvvm-reflect-enable` | `cl::opt<bool>` | `true` | `NVVM reflection, enabled by default` | obj@`0x5B4F400`, name@`0x4D3C766`, help@`0x5B4F428` (len 35) | NVVMReflectPass | [libdevice/nvvm-reflect-mechanism](../libdevice/nvvm-reflect-mechanism.md) |
| `R` | `cl::alias` → `nvvm-reflect-add` | — | (alias) | obj@`0x5B4F260`, name@(len 1); standard 4-check cl::alias validation | NVVMReflectPass | [libdevice/nvvm-reflect-mechanism](../libdevice/nvvm-reflect-mechanism.md) |

Two pass-name strings co-locate in Layer 5 but register via `INITIALIZE_PASS`, not cl::opt:

| pass-arg | help (verbatim) | storage |
|----------|-----------------|---------|
| `nvvm-intr-range` | `Add !range metadata to NVVM intrinsics.` | `name@0x4D0ED8E`, help@`0x4D3C3B0` |
| `nvvm-reflect` | `Replace occurrences of __nvvm_reflect() calls with 0/1` | `name@0x4D0ED5D`, help@`0x4D3C518` |

The legacy-PM create-fn for `nvvm-reflect` (`sub_1BD0880`, 21 B) is a `report_fatal_error("target-specific codegen-only pass")` stub; the real pass body reaches through the new-PM path in `sub_1A92780`. Three soft-CLI parser errors live at `0x4D3C6B8` / `0x4D3C6E0` / `0x4D3C710` (`Empty name`, `Missing value`, `integer value expected in nvvm-reflect-add option '`).

The 13 B string `nvvm-reflect-` at `0x4D3C6A0` has `ftz\0` at offset +13 (`0x4D3C6AD`), so a 16 B StringRef into `Module::getModuleFlag` materializes the key `"nvvm-reflect-ftz"` from concatenated rodata. The same `ftz` substring is shared with the Layer-3 `ftz` PassOption.

### Layer 6 — MLIR Framework cl::opt (AsmPrinter / Diagnostics / PassManager / Timing)

18 static-ctor cl::opts in MLIR support libs (`lib/IR/AsmPrinter.cpp`, `lib/IR/Diagnostics.cpp`, `lib/IR/MLIRContext.cpp`, `lib/Pass/PassTiming.cpp`). Exposed unmodified through the global LLVM cl::opt parser.

| option | type | default | help text (verbatim) | storage | defining pass | wiki |
|--------|------|---------|----------------------|---------|---------------|------|
| `mlir-disable-threading` | `cl::opt<bool>` | `false` | `Disable multi-threading within MLIR, overrides any further call to MLIRContext::enableMultiThreading()` | `name@0x502E5E2`, help@`0x502E618` | MLIRContext | [infra/threading-and-synchronization](../infra/threading-and-synchronization.md) |
| `mlir-elide-elementsattrs-if-larger` | `cl::opt<uint>` | (per MLIR) | `Elide ElementsAttrs with "..." that have more elements than the given upper limit` | `name@0x502CB20` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-elide-resource-strings-if-larger` | `cl::opt<uint>` | (per MLIR) | `Elide printing value of resources if string is too long in chars.` | `name@0x502CBA0` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-output-format` | `cl::opt<enum>` | `text` | `Display method for timing data` | `name@0x502F5BB`, help@`0x502F5D0` | PassTiming | [mlir-infra/overview](../mlir-infra/overview.md) |
| `mlir-pretty-debuginfo` | `cl::opt<bool>` | `false` | `Prints out debug info using the pretty forms ignoring raw loc forms` | `name@0x502CDAE` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-assume-verified` | `cl::opt<bool>` | `false` | `Skip op verification when using custom printers` | `name@0x502CDDA`, help@`0x502CC10` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-debuginfo` | `cl::opt<bool>` | `false` | `Print debug info in pretty form` | `name@0x502CD99` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-elementsattrs-with-hex-if-larger` | `cl::opt<int>` | `-1` (disabled) | `Print DenseElementsAttrs with a hex string that have more elements than the given upper limit (use -1 to disable)` | `name@0x502CA78` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-local-scope` | `cl::opt<bool>` | `false` | `Print with local scope and inline information (eliding aliases for attributes, types, and locations)` | `name@0x502CDF5`, help@`0x502CC40` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-op-generic` | `cl::opt<bool>` | `false` | `Print all operations using the generic assembly form` | `name@0x502CDC4` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-op-on-diagnostic` | `cl::opt<bool>` | `true` | `When a diagnostic is emitted on an operation, also print the operation as an attached note` | `name@0x502E5F9`, help@`0x502E680` | Diagnostics | [mlir-infra/diagnostic-abi-and-helpers](../mlir-infra/diagnostic-abi-and-helpers.md) |
| `mlir-print-skip-regions` | `cl::opt<bool>` | `false` | `Skip regions when printing ops.` | `name@0x502CE0C`, help@`0x502CCA8` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-stacktrace-on-diagnostic` | `cl::opt<bool>` | `false` | `When a diagnostic is emitted, also print the stack trace as an attached note` | `name@0x502E6E0`, help@`0x502E708` | Diagnostics | [mlir-infra/diagnostic-abi-and-helpers](../mlir-infra/diagnostic-abi-and-helpers.md) |
| `mlir-print-unique-ssa-ids` | `cl::opt<bool>` | `false` | `Print unique SSA ID numbers for values, block arguments and naming conflicts across all regions` | `name@0x502CE3B`, help@`0x502CD10` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-print-value-users` | `cl::opt<bool>` | `false` | `Print users of operation results and block arguments as a comment` | `name@0x502CE24`, help@`0x502CCC8` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |
| `mlir-timing` | `cl::opt<bool>` | `false` | `Display execution times` | `name@0x502F565`, help@`0x502F571` | PassTiming | [mlir-infra/overview](../mlir-infra/overview.md) |
| `mlir-timing-display` | `cl::opt<enum>` | `list` | `Output format for timing data` | `name@0x502F589`, help@`0x502F59D` | PassTiming | [mlir-infra/overview](../mlir-infra/overview.md) |
| `mlir-use-nameloc-as-prefix` | `cl::opt<bool>` | `false` | `Print SSA IDs using NameLocs as prefixes` | `name@0x502CE55`, help@`0x502CD70` | AsmPrinter | [bytecode/asm-printer-status](../bytecode/asm-printer-status.md) |

Enum values — `mlir-timing-display`: `list` = `display the results in a list sorted by total time` (`0x502F5F0`); `tree` = `display the results ina with a nested tree view` (`0x502F628`, verbatim typo `ina` preserved from upstream). `mlir-output-format`: `text` = `display the results in text format` (`0x502F658`); `json` = `display the results in JSON format` (`0x502F680`).

### Layer 7 — Misc LLVM cl::opt

| option | type | default | help text (verbatim) | storage | defining pass | wiki |
|--------|------|---------|----------------------|---------|---------------|------|
| `disable-i2p-p2i-opt` | `cl::opt<bool>` | `false` | `Disables inttoptr/ptrtoint roundtrip optimization` | `name@0x4FF26BA`, help@`0x4FF2688` | `llvm/Analysis/ValueTracking.cpp` | upstream LLVM ValueTracking |

## PassBuilder Mega-Registry Note

- The 478 pretty-name-keyed (`"llvm::TPass]"`) entries + 66 naked-class entries + 7 special-form entries (551 total) inserted into the PassBuilder `StringMap<PassInfo>` by `sub_1CCB7D0` (35948 B) are downstream LLVM. Twenty NVIDIA-private interleavings (`check-gep-index`, `check-kernel-functions`, `cnp-launch-check`, `ipmsp`, `nv-early-inliner`, `nv-inline-must`, `nvvm-pretreat`, `nvvm-verify`, `printf-lowering`, `select-kernels`, `nvvm-aa`, `kernel-info`, `nvvm-reflect-pp`, `nvvm-peephole-optimizer`, `propagate-alignment`, `reuse-local-memory`, `memory-space-opt`, `lower-aggr-copies`, `lower-struct-args`, `process-restrict`) are pipeline-text-parser keys for `--passes=...`, not freestanding cl::opts; factory functors live in `parseModulePass` / `parseCGSCCPass` / `parseFunctionPass` / `parseLoopPass` / `parseMachinePass`. Full table: [pipeline/passbuilder-mega-registry](../pipeline/passbuilder-mega-registry.md).

