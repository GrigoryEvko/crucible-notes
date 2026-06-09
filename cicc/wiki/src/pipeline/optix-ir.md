# OptiX IR Generation

When cicc receives the `--emit-optix-ir` flag, it activates an alternate compilation path that produces **OptiX IR** instead of PTX. OptiX IR is the intermediate representation consumed by NVIDIA's OptiX ray tracing engine, which uses a continuation-based execution model fundamentally different from the standard CUDA kernel launch model. Rather than compiling all the way down to PTX machine code, the OPTIXIR pipeline stage serializes the optimized LLVM module in a form that the OptiX runtime can later JIT-compile, link with ray tracing shaders, and schedule across the RT cores' hardware intersection pipeline.

The OptiX path is the third of four stages in cicc's internal pipeline (`LNK -> OPT -> OPTIXIR -> LLC`), but it is mutually exclusive with LLC in practice: when OptiX mode is active, the pipeline bitmask enables OPTIXIR (`0x40`) and disables certain optimizations that would be incorrect for continuation-based code. The flag also forces the EDG frontend to emit lifetime intrinsics (`--emit-lifetime-intrinsics`, EDG option id 132), which mark the live ranges of local variables — essential information for the OptiX runtime's continuation frame layout.

| | |
|---|---|
| **Pipeline stage** | OPTIXIR (stage 3 of 4) |
| **Stage bit** | Bit 6 (`0x40`) in pipeline bitmask |
| **Mode bitmask** | `0x43` = `(a13 & 0x300) \| 0x43` |
| **Core function** | `sub_12F9270` (~6 KB) |
| **Timer name** | `"OPTIXIR"` / `"LibNVVM Optix IR step."` |
| **Container IR level** | `NVVM_IR_LEVEL_OPTIX` (value 2) |
| **CLI flag** | `--emit-optix-ir` (15 bytes, inline-matched) |
| **Input extension** | `.optixir` (recognized at `0x8FC001`) |
| **Callback slot** | `CompilationState+144` (function), `+152` (user data) |
| **Availability** | CUDA (`0xABBA`) and OpenCL (`0xDEED`) modes only |

## Flag Processing

### `--emit-optix-ir` in Real Main (`sub_8F9C90`)

In the standalone entry point, `--emit-optix-ir` is matched at `0x8FAD00` by a 15-byte inline comparison (split across three immediate compares: `"--emit-o"` + `"ptix"` + `"-ir"`). When matched, it performs three actions:

1. **Pushes three strings** to the `v266` pass-through vector:
   - `"--emit-optix-ir"` (literal, 15 bytes via explicit `strcpy`)
   - An 18-byte target string from `xmmword_3C23B30` + `"28"` (likely target-related configuration)
   - A 20-byte GPU name string from `xmmword_3C23B40` + `"t128"` (likely target capability)

2. **Sets `v243 = 1`** (the OptiX IR mode flag)

3. **Sets `v258 = 1`** (the NVC flag, also set by `-nvc`)

### `--emit-optix-ir` in Flag Catalog (`sub_9624D0`)

In the 3-column flag fan-out system, `--emit-optix-ir` is processed at line 2415 of the decompiled flag catalog. Its behavior:

```c
// Only valid when a4 == 0xDEED (OpenCL) or a4 == 0xABBA (CUDA)
if (a4 == 0xDEED || a4 == 0xABBA) {
    // Route to optimizer: disable IP-MSP and LICM
    append_to_opt_vector("-do-ip-msp=0");
    append_to_opt_vector("-do-licm=0");

    // Set mode bitmask: preserve 64/32-bit mode bits, set OptiX mode
    a13 = (a13 & 0x300) | 0x43;
}
```

The `0x43` value decomposes to:
- Bits `[1:0]` = `0x03` — all standard phases enabled (LNK + LLC)
- Bit 6 = `0x40` — OPTIXIR stage enabled

### 3-Column Fan-Out

The flag translation table maps `--emit-optix-ir` across all three compilation columns:

| Column | Forwarded As |
|--------|-------------|
| **nvcc -> EDG** | `--emit-lifetime-intrinsics` |
| **nvcc -> cicc (optimizer)** | `--emit-optix-ir` + `-do-ip-msp=0` + `-do-licm=0` |
| **cicc internal** | Mode bitmask `0x43` |

This is notable because a single user-facing flag triggers a *different* flag in the EDG frontend (`--emit-lifetime-intrinsics`, EDG option id 132) while also routing the OptiX flag itself to the cicc optimizer. The EDG side-effect ensures that lifetime markers (`llvm.lifetime.start` / `llvm.lifetime.end`) are present in the generated LLVM IR, which the OptiX runtime needs to compute continuation frame sizes.

## Pipeline Stage

### Bitmask and Gating

The pipeline orchestrator `sub_12C35D0` (41 KB, the `nvvmCompileProgram` internal) reads the pipeline stage bitmask from `sub_12D2AA0` during initialization. This function parses the architecture code and options into four stage descriptors:

| Stage | Descriptor Pair | Bitmask Bit |
|-------|----------------|-------------|
| LNK | `(&v195, &v200)` | Bit 0 (`0x01`) |
| OPT | `(&v196, &v201)` | Bit 7 (`0x80`) |
| OPTIXIR | `(&v197, &v202)` | Bit 6 (`0x40`) |
| LLC | `(&v198, &v203)` | Bit 2 (`0x04`) |

The OPTIXIR stage executes at lines 1093–1150 of the decompiled orchestrator, after OPT and before LLC:

```c
// STAGE 3 -- OPTIXIR
if (v87 & 0x40) {
    // Start timer
    sub_16D8B50(timer_ctx, "OPTIXIR", 7,
                "LibNVVM Optix IR step.", 22, ...);

    // Generate OptiX IR from the optimized LLVM module
    err = sub_12F9270(arch_code,      // a3: SM architecture code
                      llvm_ctx,       // a4: LLVM context
                      module,         // current LLVM Module*
                      state + 6,      // output buffer for OptiX IR
                      &error_str);    // error string out

    if (err) {
        // Append error to state[10] error log
        ...
    }

    // Close timer
    sub_16D7950(timer_ctx);
}
```

### Callback Mechanism

Like the other three stages, OPTIXIR has a callback slot in the `CompilationState` structure:

| Offset | Field |
|--------|-------|
| `+112` | LNK callback function pointer |
| `+120` | LNK callback user data |
| `+128` | OPT callback function pointer |
| `+136` | OPT callback user data |
| **`+144`** | **OPTIXIR callback function pointer** |
| **`+152`** | **OPTIXIR callback user data** |
| `+160` | LLC callback function pointer |
| `+168` | LLC callback user data |

In the standalone pipeline entry (`sub_1265970`), the OPTIXIR callback is registered when both verbose and keep-temps modes are active (the logical AND of `-v` and `-keep`, which requires wizard mode). The callback ID is `64222`, registered via `sub_1268040` through `sub_12BC0F0`.

## `sub_12F9270` — OptiX IR Generator

| Field | Value |
|-------|-------|
| Address | `0x12F9270` |
| Size | ~6 KB |
| Parameters | `(uint arch_code, LLVMContext *ctx, Module *module, OutputBuffer *out, char **error_str)` |
| Return | `unsigned int` (0 = success) |

This function takes the fully optimized LLVM module and serializes it into OptiX IR format. The output goes into the `state+6` output buffer in the `CompilationState`, not into the PTX output buffer at `state+80`. The architecture code and LLVM context are passed through from the pipeline orchestrator's arguments.

The function is relatively small (~6 KB) compared to the LLC stage (`sub_12F5100`, ~12 KB), consistent with it being primarily a serialization step rather than a full code generation pipeline. It does not run SelectionDAG, register allocation, or instruction scheduling — those are the domain of the LLC stage, which is typically skipped when OptiX mode is active.

## IR Level and Container Marking

When the NVVM container format wraps an OptiX IR payload, the `IRLevel` field in the binary header is set to `NVVM_IR_LEVEL_OPTIX` (value 2):

| IRLevel Value | Enum Name | Meaning |
|---------------|-----------|---------|
| 0 | `NVVM_IR_LEVEL_UNIFIED_AFTER_DCI` | Default: IR after Device-Code-Interface unification |
| 1 | `NVVM_IR_LEVEL_LTO` | Link-Time Optimization IR (partially optimized) |
| **2** | **`NVVM_IR_LEVEL_OPTIX`** | **OptiX pipeline IR** |

In the binary header, this is stored as a `uint16_t` at offset `0x0C`:

```text
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  IRLevel = 0x0002 (OPTIX)    |   0x0C in NvvmContainerBinaryHeader
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

In the XML serialization path (used for debugging), this appears as the `"IRLevel"` element with the symbolic name `"NVVM_IR_LEVEL_OPTIX"`.

The `.optixir` file extension is recognized as an input format by cicc's argument parser (matched at `0x8FC001` by comparing the last 8 characters of the filename). This allows round-tripping: cicc can both produce and consume OptiX IR files.

## Optimization Pipeline Differences

When OptiX mode is active, the flag catalog forces two critical optimizer changes via the pass-through vector to the OPT stage:

### LICM Disabled (`-do-licm=0`)

Loop Invariant Code Motion is **completely disabled** when compiling for OptiX. The `do-licm` NVVMPassOption (at a known offset in the 4,512-byte options struct) gates the LICM pass insertion in the pipeline assembler `sub_12E54A0`. When set to 0, the `sub_195E880(0)` LICM pass at position 22 of the Tier 0 pipeline is skipped entirely.

The rationale is that OptiX uses a continuation-based execution model where functions can be suspended and resumed at hardware-defined continuation points (ray-surface intersection, any-hit shader invocation, etc.). LICM hoisting moves computations out of loops and into dominating blocks, which can move them across implicit continuation boundaries. If a hoisted value is live across a continuation point, the OptiX runtime must save it to the continuation frame — potentially increasing frame size and reducing performance. Worse, the hoisting may move side-effecting operations across points where the program could be suspended, violating the continuation semantics. Disabling LICM avoids these correctness and performance hazards entirely.

### IP-MSP Disabled (`-do-ip-msp=0`)

Interprocedural Memory Space Propagation is also disabled. IP-MSP (`sub_12E6160`, the NVVMMemorySpacePropagation pass) propagates memory space annotations (generic -> shared/local/global) across function boundaries. This optimization is meaningless for OptiX IR because the OptiX runtime performs its own memory space analysis during JIT compilation, and the intermediate representation must remain generic to allow runtime binding of hit attributes, payload data, and SBT (Shader Binding Table) records to their final memory spaces.

### Forced Inlining (`nv-inline-all`)

The `nv-inline-all` knob (registered at constructor `ctor_186_0` at `0x4DBEC0` in the NVIDIA custom inliner) bypasses cost analysis entirely and forces inlining of every call. This mode is used for OptiX compilation where the entire call graph must be flattened for the hardware intersection pipeline. The OptiX runtime requires monolithic shader functions because the RT core hardware executes individual ray tracing programs as atomic units — there is no call stack during hardware intersection traversal.

From the inliner cost model (`sub_1864060`, 75 KB):

> The `nv-inline-all` knob bypasses cost analysis entirely and forces inlining of every call. This is used for specific compilation modes (e.g., OptiX ray tracing where the entire call graph must be flattened for the hardware intersection pipeline).

The standard `inline-budget` (default 20,000) and `inline-total-budget` are irrelevant when `nv-inline-all` is active — every call site is inlined unconditionally regardless of cost.

## Continuation-Based Execution Model

OptiX IR exists because NVIDIA's ray tracing hardware uses a fundamentally different execution model than standard CUDA kernels. Understanding this model explains every design decision in the OPTIXIR pipeline stage.

### Standard CUDA vs. OptiX Execution

In standard CUDA, a kernel is a single function that runs to completion on an SM. The compiler produces PTX, which ptxas assembles into SASS machine code. The entire call graph is resolved at compile time, and the GPU executes instructions sequentially (modulo warp divergence and memory latency hiding).

In OptiX, a ray tracing pipeline consists of multiple **programs** (ray generation, closest-hit, any-hit, miss, intersection, callable) that are compiled separately and linked at runtime by the OptiX driver. When a ray-surface intersection occurs, the hardware suspends the current program, saves its live state to a **continuation frame** in device memory, and launches the appropriate hit shader. When the hit shader completes, execution resumes from the continuation point.

This model has several consequences for compilation:

1. **No cross-function calls during intersection.** The RT core hardware does not support a general call stack. All function calls within a single program must be fully inlined before the OptiX runtime receives the IR — hence `nv-inline-all`.

2. **Lifetime intrinsics are critical.** The OptiX runtime uses `llvm.lifetime.start` / `llvm.lifetime.end` markers to determine which local variables are live at each potential continuation point. Variables that are provably dead at a continuation point do not need to be saved to the continuation frame. Without these markers, the runtime must conservatively assume all locals are live, inflating frame sizes and reducing performance.

3. **LICM is unsafe.** Hoisting computations out of loops can move them across implicit continuation points, creating live ranges that span suspension/resumption boundaries. The OptiX runtime cannot reconstruct the hoisted value after resumption unless it is saved, but the compiler does not know where the continuation points will be (they are determined at runtime by the ray tracing pipeline topology).

4. **Memory space must remain generic.** OptiX IR is JIT-compiled at runtime with knowledge of the full pipeline configuration. Memory space decisions that depend on the pipeline topology (shared memory for hit attributes, global memory for payload) cannot be made at cicc compile time.

5. **The output is IR, not machine code.** Unlike the LLC stage which produces PTX text, the OPTIXIR stage serializes the LLVM module in a form suitable for the OptiX JIT. This is why `sub_12F9270` is only ~6 KB — it is a serializer, not a code generator.

## Configuration

### CLI Activation

```bash
# Standard OptiX compilation via nvcc
nvcc --emit-optix-ir -arch=sm_89 -o kernel.optixir kernel.cu

# Direct cicc invocation
cicc --emit-optix-ir -arch sm_89 -o kernel.optixir kernel.bc

# The flag also accepts .optixir input files for round-tripping
cicc -arch sm_89 -o kernel.ptx kernel.optixir
```

### Effective Configuration When Active

When `--emit-optix-ir` is specified, the following configuration is implicitly applied:

| Setting | Value | Source |
|---------|-------|--------|
| `v243` (OptiX flag) | 1 | Real main `sub_8F9C90` |
| `v258` (NVC flag) | 1 | Real main `sub_8F9C90` |
| Pipeline bitmask | `0x43` | Flag catalog `sub_9624D0` |
| `do-licm` | 0 | Flag catalog, routed to OPT |
| `do-ip-msp` | 0 | Flag catalog, routed to OPT |
| EDG: `emit-lifetime-intrinsics` (id 132) | enabled | 3-column fan-out |
| Container `IRLevel` | 2 (`NVVM_IR_LEVEL_OPTIX`) | Container serializer |
| `nv-inline-all` | true | OptiX mode forces all inlining |

### Bitmask Decomposition

The `0x43` mode value preserves the 64/32-bit mode bits (mask `0x300`) from any previously-set `a13` value:

```text
a13 = (a13 & 0x300) | 0x43

Bit field:
  [9:8] = preserved (0x100 = 64-bit, 0x200 = 32-bit)
  [7]   = 0  (OPT stage -- controlled separately)
  [6]   = 1  (OPTIXIR stage enabled)
  [5:3] = 0  (no LTO, no verification override)
  [2]   = 0  (LLC stage -- typically not run in OptiX mode)
  [1:0] = 11 (LNK + base phase control)
```

Note that bit 2 (LLC) is 0 in the `0x43` bitmask, confirming that the LLC stage is not activated when OptiX mode is the primary output. The pipeline runs `LNK -> OPT -> OPTIXIR` and stops.

## Diagnostic Strings

| String | Length | Context |
|--------|--------|---------|
| `"OPTIXIR"` | 7 | Timer phase name (passed to `sub_16D8B50`) |
| `"LibNVVM Optix IR step."` | 22 | Timer description string |
| `"--emit-optix-ir"` | 15 | CLI flag literal (inline-matched in real main) |
| `"--emit-lifetime-intrinsics"` | 27 | EDG flag routed from `--emit-optix-ir` |
| `".optixir"` | 8 | Input file extension (matched at `0x8FC001`) |
| `"-do-ip-msp=0"` | 13 | Optimizer option routed when OptiX active |
| `"-do-licm=0"` | 12 | Optimizer option routed when OptiX active |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| OptiX IR generator (core OPTIXIR stage) | `sub_12F9270` | ~6 KB | — |
| Pipeline orchestrator (`nvvmCompileProgram` internal) | `sub_12C35D0` | ~41 KB | — |
| Bitmask / stage descriptor parser | `sub_12D2AA0` | — | — |
| Flag catalog (routes `--emit-optix-ir`) | `sub_9624D0` | ~75 KB | — |
| Real main (matches `--emit-optix-ir` at `0x8FAD00`) | `sub_8F9C90` | ~10 KB | — |
| OPTIXIR callback registration (callback ID `64222`) | `sub_1268040` | — | — |
| Pipeline callback dispatcher | `sub_12BC0F0` | — | — |
| Inliner cost model (`nv-inline-all` bypass) | `sub_1864060` | ~75 KB | — |
| CGSCC inliner core (`inlineCallsImpl`) | `sub_186CA00` | ~61 KB | — |
| Timer start (receives `"OPTIXIR"` phase name) | `sub_16D8B50` | — | — |
| Timer close | `sub_16D7950` | — | — |
| Pipeline assembler (skips LICM when `do-licm=0`) | `sub_12E54A0` | ~49.8 KB | — |

## Cross-References

- [Entry Point & CLI](./entry.md) — `--emit-optix-ir` flag parsing and `v243` variable
- [LLVM Optimizer](./optimizer.md) — `do-licm` and `do-ip-msp` NVVMPassOptions, pipeline assembler
- [NVVM Container Binary Format](../structs/nvvm-container.md) — `NVVM_IR_LEVEL_OPTIX` (value 2) in IRLevel enum
- [EDG 6.6 Frontend](./edg.md) — `--emit-lifetime-intrinsics` (EDG option id 132)
- [Code Generation](./codegen.md) — LLC stage that is skipped in OptiX mode
- [LICM](../llvm/licm-real.md) — the pass disabled by OptiX mode
