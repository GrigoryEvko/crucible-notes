# SM89 Ada

The SM89/90 backend occupies `0x100C000`--`0x11EA000` (1.9 MB, 978 functions) and is the shared code generation pipeline for Ada Lovelace (sm_89) and Hopper (sm_90/sm_90a) architectures within the embedded ptxas compiler. Despite covering two commercially distinct GPU families, the linker treats them as a single backend family that diverges only through runtime feature flags and per-architecture dispatch table entries. The ISA class string `"Ada"` is registered for sm_89 and `"Hopper"` for sm_90 in the profile descriptor table built by `sub_484F50` (line 468 / line 517 of the registration function). The SM version number is parsed numerically from the `--gpu-name` option and dispatched through a lookup table at `dword_1EED2E0`.

This page documents the complete backend: the instruction encoder table, the compilation driver, the option parser, the instruction selector (including the 226 KB mega-hub), the ELF/cubin output generator, the symbol resolver, and the instruction scheduler.

## Address Map

| Range | Size | Subsystem | Functions | Key Function |
|---|---|---|---|---|
| `0x100C000`--`0x10FFFFF` | 1.0 MB | Shared instruction encoders | ~750 | Template instances (`sub_100C110`, ...) |
| `0x1100000`--`0x1120000` | 128 KB | Backend driver | ~30 | `sub_1112F30` (65 KB, compilation driver) |
| `0x1120000`--`0x119BF40` | 496 KB | ISel pattern matchers | ~160 | Pattern-matching rules |
| `0x119BF40`--`0x11D4680` | 226 KB | ISel mega-hub | 1 | `sub_119BF40` (main dispatch) |
| `0x11D4680`--`0x11EA000` | 90 KB | Scheduler + emission | ~16 | `sub_11D6890` (13 KB, block scheduler) |

Total: 978 functions above 3 KB. The instruction encoder templates account for 792 of these.

## Architecture Classification

The option parser at `sub_1104950` calls `sub_15C3DD0` to convert `--gpu-name` (e.g., `sm_89`) into a numeric SM version, then indexes `dword_1EED2E0` to obtain an architecture family code. SM89 and SM90 share the same family code and therefore the same backend entry points.

Feature flag configuration at `sub_1100E50` reads the SM version from the codegen state (`v5[94]`) and enables per-generation features:

| Condition | Internal Version | Enables |
|---|---|---|
| SM >= 11 | sm_75 equivalent | Features v16, v17 (Turing baseline) |
| SM >= 14 | sm_89 equivalent | Feature v14 (Ada-specific) |
| SM == 29 or 30 | sm_89/90 | Feature 33 (conditional on debug flag 618) |
| SM >= 17 | sm_100 equivalent | Feature v19 (Blackwell) |

Feature 33 is the only flag gated to the sm_89/sm_90 pair specifically. It is disabled when flag 618 (device-debug) is set, suggesting it controls an optimization that interferes with debug info generation.

## Ada vs Hopper: Concrete Binary Differences

Although SM89 and SM90 share the same 1.9 MB backend, the binary distinguishes them at four levels: the profile descriptor, the dispatch table function pointers, the backend initialization constants, and ISA version validation. This section catalogs every difference found in the decompiled code.

### Profile Descriptor Flags

The profile registration function `sub_484F50` builds a 136-byte descriptor per SM target via `sub_484DB0`. After constructing the sm_89 descriptor, the code sets:

```c
v47->m128i_i8[3] = 1;   // byte[3] of the sm_89 profile descriptor
```

This flag is **unique to sm_89** — it is not set for sm_90, sm_90a, or any other SM target in the entire registration function. The corresponding byte[4] flag is reserved for "a" and "f" sub-variants (sm_90a, sm_100a, sm_100f, etc.) and is never set on sm_89.

**What byte[3] gates.** The profile descriptor byte[3] is propagated through the ELF section merger and instruction validation infrastructure (`sub_46EE00`, `sub_46C690`). Ada Lovelace is the only architecture that supports the full fixed-function graphics pipeline (tessellation, geometry shading, rasterization) at the SM level — Hopper (sm_90) is a datacenter-only GPU that drops fixed-function graphics hardware. Setting byte[3] = 1 on the sm_89 descriptor marks it as a **graphics-capable** architecture, gating tessellation-related code paths in the linker's section merging and instruction validation. When the linker processes shader sections that require graphics pipeline stages, this flag determines whether the target supports them.

### Dispatch Table: Seven Slots, All Functionally Distinct

The `sub_15C0CE0` registration function populates seven callback tables (`qword_2A644B8` through `qword_2A64488`) for each SM target. SM89 and SM90 have different function pointer addresses in all six callback slots (plus a different internal version constant in slot A0):

| Slot | Table | sm_89 | sm_90 / sm_90a | Functional Difference |
|---|---|---|---|---|
| B8 | Pre-compilation | `sub_15C2D40` | `sub_15C2CE0` | Identical behavior: both call `sub_166DA30(a2, 0)` and look up `"cpf_optx"` |
| B0 | Compilation | `sub_15C2C20` | `sub_15C2B30` | Identical behavior: both call `sub_166DA30(a2, 1)` and look up `"cpf_optx"` |
| A8 | Backend init | `sub_15C3740` | `sub_15C3520` | **Different** — see below |
| A0 | Internal version | `byte_2A5EE2C` | `asc_2A5EE28` | Different integer constant (sm_89 = internal 29, sm_90 = internal 30) |
| 90 | Perf-stats | `sub_15C1F90` | `sub_15C1ED0` | Identical behavior: both emit `dword_2A5EEF0` warning for `"sm_20"` / `"--perf-stats"` |
| 88 | Resource calc | `sub_15C2370` | `sub_15C2290` | Identical algorithm: same register file size / occupancy calculation |
| 98 | Cleanup | (via `qword_2A64498`) | (via `qword_2A64498`) | Not individually registered per-arch for sm_89/90 |

Despite having six distinct function addresses, only **one slot (A8)** has materially different behavior between sm_89 and sm_90. The remaining five are duplicated code (same algorithm, different object addresses) — a consequence of the template-based dispatch architecture where each SM target gets its own instantiation even when the logic is identical.

### Backend Initialization: sub_15C3740 (Ada) vs sub_15C3520 (Hopper)

The A8 slot handler initializes the per-function codegen context. Both functions call `sub_189F230(a3)` to allocate the context, then diverge in two places:

**1. Resource descriptor field at offset +348.**

```c
// sm_89 (Ada):
*(_DWORD *)(v6 + 348) = 28677;     // 0x7005

// sm_90 (Hopper):
*(_DWORD *)(v6 + 348) = 0x8000;    // 32768
```

This field encodes a hardware resource limit passed to the instruction scheduling and register allocation subsystems. The value `0x7005` for Ada encodes a composite: the low 12 bits (0x005 = 5) represent a thread-group granularity parameter, while the upper bits (0x7000) encode a shared memory bank configuration (bit field semantics inferred from the Hopper comparison). Hopper's clean power-of-two value `0x8000` (32768) reflects its larger shared memory capacity (228 KB vs Ada's 100 KB per SM) and simplified bank geometry.

**2. Hopper-only conditional at a2+355.**

```c
// sm_90 only (not present in sm_89):
if ( *(_BYTE *)(a2 + 355) )
    v7[107] = 0;
```

Offset 355 of the module options structure corresponds to the `--blocks-are-clusters` flag. When this flag is set on Hopper, the backend zeroes context field 107 (offset 428), which disables a per-block optimization that assumes independent thread blocks. This reflects Hopper's Thread Block Cluster feature, where multiple blocks form cooperative groups that share distributed shared memory. Ada does not support clusters, so this conditional is absent from its backend init.

### Feature 33: Tensor Core Extensions

The `sub_1100E50` feature flag configurator gates feature 33 on the exact condition:

```c
if ( (unsigned int)(v4 - 29) > 1 || *(_BYTE *)(a2 + 618) )
    sub_16E3AA0(v8, 33, 0);     // disabled
else
    sub_16E3AA0(v8, 33, 1);     // enabled
```

This enables feature 33 only when the internal SM version is 29 or 30 (sm_89 or sm_90) **and** flag 618 (`--device-debug`) is not set. Feature 33 is persisted as feature name index 28 via `sub_12B5EF0(table, 28)` and recorded as the key-value pair `"feature_28" = "true"` in the codegen metadata.

Both Ada and Hopper share this feature when debug mode is off. The debug-mode suppression indicates feature 33 controls an optimization (the specific optimization is not identified in the binary, but tensor core instruction scheduling or fusion is consistent with the sm_89/sm_90 gating) that produces SASS sequences incompatible with DWARF-based single-stepping.

### Internal SM Version Mapping

The `sub_15C3DD0` lookup function resolves target strings to internal version numbers via the `qword_2A644A0` hash table. The internal versions, inferred from the `sub_1100E50` threshold checks and `dword_1EED2E0` indexing (range check `version - 7 <= 0x1B`), follow this scheme:

| Internal Version | Real SM | Architecture | Notes |
|---|---|---|---|
| 7 | sm_35 | Kepler | Minimum supported (inferred from range base) |
| 8 | sm_37 | Kepler (GK210) | — |
| 9 | sm_50 | Maxwell | Feature: special load-cache behavior |
| 10 | sm_52/53 | Maxwell | Feature: special load-cache behavior |
| 11 | sm_60 | Pascal | Feature: v16, v17 enabled (>= 11) |
| 12 | sm_61/62 | Pascal | — |
| 13 | sm_70/72 | Volta | — |
| 14 | sm_75 | Turing | Feature: v14 enabled (>= 14) |
| ... | ... | ... | — |
| 29 | sm_89 | **Ada** | Feature 33 enabled |
| 30 | sm_90 | **Hopper** | Feature 33 enabled |
| ... | ... | ... | — |

The `dword_1EED2E0` array at index `(version - 7)` maps internal versions to architecture family codes used by the register allocator and instruction scheduler. SM89 and SM90 share the same family code, confirming they use identical scheduling and register allocation logic.

### ISA Version Gating

The instruction validation infrastructure (`sub_145EFB0`) checks ISA version compatibility when encountering architecture-specific instructions:

```c
// For sm_89-only instructions (e.g., Ada-specific shader instructions):
if ( sub_12B3090(*(_DWORD *)(a1 + 960)) ||
     *(_QWORD *)(a1 + 152) && sub_12A8360(*(_DWORD *)(a1 + 168), 89) )
{
    sprintf(v56, "%s on sm_89", s);
    // Check ISA version 8.1 requirement
    sub_1441FB0(a1, 8, 1, v125, a3);
}
```

Instructions gated to sm_90 or later use `sub_143E480(a1, 0x5A)` (where `0x5A` = 90) to test `target_SM <= 90`. When an instruction requires Hopper and the target is Ada, the validator emits an error referencing `"sm_90"` as the minimum required architecture.

The PTX ISA version requirements are:
- PTX ISA 8.1 for sm_89-specific instructions (Ada tessellation/graphics pipeline instructions)
- PTX ISA 7.8 for pre-Ada instructions
- PTX ISA 8.0+ for sm_90-specific instructions (Hopper cluster/DMA features)

### Compilation Driver SM Version Checks

The main compilation driver `sub_1112F30` contains three SM-version-dependent code paths that distinguish behavior above and below the sm_90 boundary:

| Line | Condition | Effect |
|---|---|---|
| 606 | `*(a1+376) <= 16` | Gates a pre-Blackwell codegen path (affects both Ada and Hopper identically) |
| 991 | `*(a1+376) > 26` | If internal version > 26 (sm_86+), emits `"sm_90"` in warp-synchronous behavior warning |
| 1150 | `*(a1+376) <= 26` | Gates tensor-memory-access-check; for sm_89 and sm_90 (both > 26), the check is inverted |
| 1303 | `*(a1+376) > 26` | Second tensor-memory-access gate for multi-function compilation mode |

None of these conditions distinguish sm_89 from sm_90 specifically — they both fall on the same side of every threshold (both have internal version > 26). The only per-architecture differentiation occurs through the feature flag configurator and the backend init constants documented above.

### Summary of Differences

| Aspect | SM89 Ada | SM90 Hopper |
|---|---|---|
| ISA class string | `"Ada"` | `"Hopper"` |
| Profile byte[3] | **1** (graphics-capable) | 0 |
| Profile byte[4] | 0 | 0 (1 for sm_90a) |
| Internal version | 29 | 30 |
| Backend init +348 | `0x7005` (28677) | `0x8000` (32768) |
| Cluster support | No (no byte+355 check) | Yes (`--blocks-are-clusters` gate) |
| Feature 33 | Enabled (unless debug) | Enabled (unless debug) |
| Shared memory limit | 100 KB/SM | 228 KB/SM |
| Thread Block Clusters | Not supported | Supported |
| Fixed-function graphics | Supported (tessellation, raster) | Not supported (datacenter only) |
| `-D__CUDA_ARCH__` | `890` | `900` |
| PTX ISA for new insns | 8.1 | 8.0+ |
| Dispatch table code | All 6 slots unique addresses | All 6 slots unique addresses |
| Dispatch table behavior | 5 of 6 functionally identical to sm_90 | 5 of 6 functionally identical to sm_89 |

## Instruction Encoder Templates (0x100C000–0x10FFFFF)

The 750 functions in this region are template-instantiated instruction encoding initializers. Each function initializes one SASS instruction variant — a specific opcode with a specific operand pattern. All share an identical structure:

```text
1. sub_4C28B0(a1, offset, fieldlen, value)   // 5-8 calls setting bitfield params
2. SSE load from xmmword_1F46xxx             // 16-byte instruction signature
3. memcpy loop: 3 parallel arrays (10 entries each)
   from .rodata into descriptor at a1+24..a1+140
4. sub_4C60F0(a1, a2, slot, offset, type)    // control code slot config
5. sub_4C5F90(a1, a2)                        // finalize descriptor
6. sub_50xxxx modifier calls                 // predicate, rounding, ftz, etc.
```

The only differences between functions are the constant table address (`xmmword_1F460E0` through `0x1F47400`), the operand field definitions, the opcode ID written to `a2+12`, and which modifier helpers are invoked.

### Size Clusters

| Size Range | Instruction Class | Example |
|---|---|---|
| 4,700–6,200 bytes | Simple (moves, branches, simple math) | Basic register-register operations |
| 7,400–7,700 bytes | Standard 3-source ALU | Most arithmetic/logic instructions |
| 7,800–8,100 bytes | ALU + extra modifiers | Rounding mode, saturate, FTZ |
| 8,300–8,500 bytes | Complex (texture, surface, atomics) | Memory + modifier combinations |

### Encoder Sub-Ranges by Opcode Type

| Sub-Range | Count | Type | Notes |
|---|---|---|---|
| `0x100C000`--`0x1030000` | ~100 | Arithmetic/Logic | Opcode 104, standard ALU variants |
| `0x1030000`--`0x1060000` | ~100 | Floating-point | FP modifiers via `sub_50E300`/`sub_50E320`/`sub_50E380` |
| `0x1060000`--`0x1090000` | ~100 | Memory/Load-Store | Cache modifiers, surface/texture, shared/global memory |
| `0x1090000`--`0x10D0000` | ~100 | Control flow / Special | Branch/call, barrier, warp-level, predicated ops |
| `0x10D0000`--`0x10FFFFF` | 148 | Transition-region (Ada/Hopper extensions) | Tensor-core, async copy, cluster-level, extended shared memory |

The transition-region encoders at `0x10D0000`--`0x10FFFFF` reference constant tables in the `0x1F472xx` range and represent SM89/90-specific instruction formats not present in earlier architectures. These cover tensor-core instructions (HMMA/DMMA), asynchronous copy (`cp.async`), cluster-level operations (distributed shared memory), and extended shared memory configurations.

### Rodata Constant Tables

Each encoder is parameterized by a read-only constant table in `.rodata` at addresses `0x1F460E0` through `0x1F47400`. Each table contains:

```text
Offset  Size    Content
  0     16B     SSE header: instruction signature
 16     400B    10 source-register-class entries (40-byte stride)
416     400B    10 destination-register-class entries (40-byte stride)
816     400B    10 control-code entries (40-byte stride)
```

The register-class entries encode which physical register files (R, UR, P, UP, SReg) are valid operands for each slot, with class 1023 serving as the "any" wildcard.

### Modifier Helpers

The `sub_50xxxx` family configures instruction modifiers:

| Function | Modifier |
|---|---|
| `sub_50C790` | Predicate conditioning |
| `sub_50A260` | Operand negation |
| `sub_50B570` | Absolute value |
| `sub_509800` | Type conversion |
| `sub_509930` | Saturation |
| `sub_50ABE0` | Rounding mode A |
| `sub_50AC20` | Rounding mode B |
| `sub_519D60` | Special modifier (FTZ-related) |
| `sub_50E120` | Comparison operator |
| `sub_50E180` | Boolean operation |
| `sub_50E300` | FP rounding mode |
| `sub_50E320` | FP flush-to-zero |
| `sub_50E340` | FP denormal mode |
| `sub_50E380` | Combined rounding + FTZ |

## Backend Driver (0x1100000–0x1120000)

This region contains 30 functions totaling 468 KB that implement the SM89/90 compilation pipeline from option parsing through codegen to ELF output.

### Compilation Driver: sub_1112F30 (65 KB, 2164 lines)

This is the main per-module compilation entry point within the SM89/90 backend. It orchestrates the entire PTX-to-SASS compilation flow.

**Phase 1: PTX header emission.** Writes `.version`, `.target`, and a dummy entry point (`__cuda_dummy_entry__`) to the output stream via `fprintf`.

**Phase 2: Mode selection.** Reads mode flags from the option parser and selects codegen callback function pointers:

| Mode | Callback | Description |
|---|---|---|
| `--compile-only` | `sub_110CD20` | Single-function compilation |
| Multi-function (standard) | `sub_110D110` | Iterates function list |
| `--compile-as-tools-patch` | `sub_110CBA0` | Texture/surface/sampler/constant patching |
| `--assyscall` | `sub_110D0B0` | System call compilation mode |
| Standard (default) | `sub_11089E0` / `sub_1109180` / `sub_1107F10` | Whole-program and multi-function variants |

**Phase 3: SM version validation.** Parses SM version from the target string via `sscanf(target, "%*[^0-9]%d")`. Validates `--legacy-bar-warp-wide-behavior` compatibility with sm_70+. Gates tensor-memory-access-check by target architecture.

**Phase 4: Per-function compilation.** Allocates per-function data structures (360 bytes for codegen state, 216 bytes for auxiliary data). Iterates the function list, calling `sub_110AA30` for codegen initialization per function and `sub_110D2A0` for output finalization.

**Phase 5: Parallel compilation.** Supports multi-threaded compilation via thread pool functions `sub_464AE0` (pool creation) and `sub_464C30` (work dispatch). Phase timing is tracked by `sub_45CCD0` / `sub_44EF30`.

### Option Parser: sub_1104950 (38 KB, 1208 lines)

Registers the complete embedded ptxas option set through a series of `sub_42E390` calls. Each call binds an option name to a destination field in the module structure. The full option catalog includes approximately 55 recognized options:

**Optimization options:** `opt-level`, `fast-compile`, `no-fastreg`, `disable-optimizer-constants`, `dont-merge-basicblocks`, `noFwdPrg`, `limit-fold-fp`, `optimize-float-atomics`, `sw4936628`.

**Register/resource options:** `maxrregcount`, `device-function-maxrregcount`, `register-usage-level` (validated range 0–10), `minnctapersm`, `maxntid`, `override-directive-values`.

**Debug options:** `device-debug`, `sp-bounds-check`, `device-stack-protector`, `device-stack-protector-frame-size-threshold`, `sanitize`, `lineinfo`.

**Code generation options:** `gpu-name`, `machine`, `abi-compile`, `compile-only`, `compile-as-tools-patch`, `extensible-whole-program`, `fast-compile`, `fastimul`, `position-independent-code`, `force-rela`, `blocks-are-clusters`, `enable-extended-smem`, `return-at-end`, `texmode_independent`.

**Warning/diagnostic options:** `warn-on-double-precision-use`, `suppress-double-demote-warning`, `warning-as-error`, `disable-warnings`, `suppress-stack-size-warning`, `suppress-async-bulk-multicast-advisory-warning`, `suppress-sparse-mma-advisory-info`, `make-errors-visible-at-exit`.

**Cache policy options:** `def-load-cache`, `force-load-cache`, `def-store-cache`, `force-store-cache`.

**Output/control options:** `output-file`, `link-info`, `okey`, `ptx-length`, `profile-options`, `opt-pointers`, `cimm`, `disable-smem-reservation`, `verbose-tkinfo`, `compiler-annotations`, `query-controls`, `apply-controls`, `nv-host`.

**Feature gating options:** `legacy-bar-warp-wide-behavior`, `assume-extern-functions-do-not-sync`, `disable-fast-video-emulation`, `g-tensor-memory-access-check`, `gno-tensor-memory-access-check`.

The parser validates option compatibility: `--nv-host` is incompatible with both `--extensible-whole-program` and `--compile-only`. Register-usage-level is bounds-checked to the range 0–10.

### Option Definition Table Builder: sub_1103030 (30 KB, 1249 lines)

Registers the user-facing option descriptions via `sub_42F130`. This is the "help text" layer that maps each option to its short name, type, and description string. Option types: 1=bool, 2=string, 4=int, 7=special. Representative short names: `"e"` for `--entry`, `"f"` for `--compile-functions`, `"k"` for `--key`, `"ok"` for `--okey`.

### Per-Function Codegen Init: sub_110AA30 (19 KB, 661 lines)

Called once per function to initialize the 360-byte codegen context. Key initialization:

| Context Offset | Value | Description |
|---|---|---|
| 24 | `sub_1100DE0` | Codegen callback |
| 64 | `"ptxocg.0.0"` | Producer string |
| 48 | `38156003` | Tool ID / magic number |
| 1200 | `"NVIDIA"` | Vendor string |
| 1544, 1568, 1584, 1600 | Various `sub_110270x` | Callback vtable entries |

SM version-conditional feature enablement:

- SM >= 14 (sm_89 equivalent): enables feature v14
- SM >= 11 (sm_75 equivalent): enables features v16, v17
- SM >= 17 (sm_100 equivalent): enables feature v19
- SM 9–10: special load-cache behavior

Configures optimization levels (`v5[133]` maps to 1/2/4), maxregcount, minnctapersm thresholds, register classes, scheduling slots, and opens output files.

### Per-Function Output Finalization: sub_110D2A0 (33 KB, 1113 lines)

Called after per-function codegen completes. Runs three low-level encoding passes (`sub_14075D0`, `sub_1407FC0`, `sub_14091C0`), generates register-pressure fatpoint statistics (`"CC"`, `"ADDR"`, `"REG"` variants for `--reg-fatpoint` mode), handles video emulation stub output, performs final register allocation accounting, and creates symbol table entries via `sub_1627F00`. Uses `setjmp`/`longjmp` for fatal compilation error recovery.

### Register Allocation and Launch Config: sub_110BC90 (18 KB, 763 lines)

Reads thread-block dimensions (`blockDim.x/y/z` from function node offsets 6–8), computes total threads as the product, and handles `maxntid`/`minnctapersm` overrides from both function-level directives and command-line options. Implements a complex register budget computation with multiple fallback paths based on SM version, optimization level, and launch configuration. Warns via `sub_467460` about `.local_maxnreg` on device functions in `--compile-only` mode.

### Feature Flag Configurator: sub_1100E50 (14 KB, 451 lines)

Configures approximately 30 boolean feature flags in the codegen feature table at `a1+1096`. Uses `sub_16E3AA0` to set individual flags and `sub_12B5EF0` to retrieve feature name strings for diagnostic output. Features are persisted as `"true"`/`"false"` key-value pairs via `sub_448E70`.

## ELF/Cubin Output: sub_1116890 (60 KB, 1998 lines)

The ELF output generator serves as the outer entry point for the SM89/90 backend. It receives `(argc, argv, module_list, ...)`, establishes `setjmp`/`longjmp` error recovery, creates the option parser via `sub_432020`, calls `sub_1104950` to populate the module structure, then invokes `sub_1112F30` (the compilation driver).

After compilation, it builds JSON metadata trees for the cubin output using `sub_1CFA200` (create JSON object), `sub_1CFA220` (add field), and `sub_1CFA2D0` (add child). The metadata includes `"version"`, `"metadata"`, `"type"`, `"min"`, `"max"` fields. Key-value pairs are iterated from the descriptor table at `off_1EEEFA0`. Final serialization uses `sub_1CF98D0`.

## Symbol Resolution: sub_110FA30 (55 KB, 1576 lines)

Processes symbol references within function bodies via recursive expression tree walking. The main switch on expression type handles three cases:

| Case | Expression Type | Handling |
|---|---|---|
| 0 | Binary expression | Recurse on left and right operands |
| 1 | Unary expression | Recurse on single operand |
| 4 | Symbol reference | Main logic: resolve name, check binding |

For case 4 (symbol reference), the resolver:

1. Checks if the symbol is a defined function (type 5).
2. Handles external references, weak symbols, and locally-defined symbols.
3. Manages `--extensible-whole-program` external reference warnings.
4. Builds per-function call graph sets (`a2[5]` for direct calls, `a2[6]` for transitive calls).
5. Uses `sub_4655C0` / `sub_465720` for set membership tests and set insertion.
6. Handles alias resolution via `sub_12B5620` / `sub_12B5640`.

Key state offsets on the module structure: 726 (compile-only), 889 (extensible-whole-program), 924 (ABI mode). The string `"<Internal/Macro>"` is used as source location for macro expansion diagnostics.

### Expression Tree Walkers

Two helper functions support the symbol resolver:

**sub_110EF30** (11 KB, 336 lines): Expression type analyzer. Switches on `(byte[0] & 0x3F)` to classify expression nodes. Case 0 handles binary expressions (recursive descent on both children), case 4 handles symbol/operand references with set-tracking via `sub_465720`. Copies 208 bytes (13x `__m128i`) of SSE state per scope for recursive processing.

**sub_110E7E0** (11 KB, 377 lines): Initializer expression walker. Processes constant initializer trees for global variables. Switch on `*a1`: type 0 = compound initializer (iterates children), type 1 = symbol reference (delegates to `sub_110EF30`), type 2 = list of initializers (sub-expression recursion). Expression node type encoded as `byte[0] & 0x3F` with up to 16 types.

### Function Symbol Table Builder: sub_1111DB0 (18 KB, 646 lines)

Processes individual function declarations. Distinguishes entry points vs. device functions vs. external declarations. Checks for CUDA builtins via `sub_4447B0`. Manages forward declarations (`v4+184` = "has body" flag). Integrates with the sanitizer via `"threadsteer"` and `"__cuda_sanitizer"` prefix checks. Tracks call graph edges by adding to `a2[56]` sets.

## Instruction Selection (0x1120000–0x11D4680)

### ISel Pattern Matchers (0x1120000–0x119BF40, ~160 functions)

Each pattern matcher is a predicate function with the signature:

```c
bool isel_match(match_ctx* ctx, ir_node* node, int* pattern_id, int* priority);
```

The function extracts IR node attributes via `sub_A49150(a1, a2, field_id)`, checks operand counts via `sub_530FD0`, retrieves operands via `sub_530FB0`, validates operand types through a family of type-check functions, and writes the matched pattern ID and priority if all constraints are satisfied.

**Operand type predicates:**

| Function | Predicate |
|---|---|
| `sub_1119410` | Decode register class from operand |
| `sub_1119420` | Is operand a register? |
| `sub_1119430` | Is operand a predicate register? |
| `sub_1119450` | Is operand a general register? |
| `sub_1119490` | Is operand an immediate? |
| `sub_11194A0` | Is operand a constant bank reference? |

**IR field IDs used in pattern matching:**

| Field ID | Decimal | Attribute |
|---|---|---|
| `0x1DF` | 479 | Primary opcode class |
| `0x1DE` | 478 | Secondary opcode |
| `0x1C4` | 452 | Operation variant |
| `0x1A3` | 419 | Data type |
| `0x20` | 32 | Addressing mode |
| `0xD0` | 208 | Memory space |
| `0x80` | 128 | Result type |
| `0x31` | 49 | Comparison operator |
| `0xDF` | 223 | Modifier flags |
| `0x7B` | 123 | Texture operation type |

Register class 1023 is the wildcard "any" class. Priority values (typically 16) determine preference when multiple patterns match the same IR node.

**Representative patterns:**

- **sub_1190050**: Matches complex 6-operand instructions (texture/surface, based on operand count). Field chain: `0x1DF=2468`, `0x1DE=2463`, `0x1C4=2357..2364`, `0x1A3=2182..2183`. Outputs `pattern_id=9`, `priority=16`.
- **sub_119B8F0**: Matches 3-operand predicated instructions. Field chain: `0x31=227..228`, `0xDF=1214`, `0x20=137..142`, `0xD0=1163..1169`. Result type 5.
- **sub_1140190**: Matches texture operations. Field `0x7B=536` with addressing mode and type constraints.

### ISel Mega-Hub: sub_119BF40 (226 KB)

The largest function in the SM89/90 backend and the fourth-largest in the entire binary. At 226 KB (~7,500+ lines estimated), it is too large for Hex-Rays decompilation and produces only a gap entry with a named label at `def_119BF78` (offset `0x119C120`).

This function is the main instruction selection dispatch. Its structure is a massive switch/jump-table on the IR primary opcode, with each case calling one or more of the ~160 pattern-matching helpers from the `0x1120000`--`0x119BF40` range. The ISel protocol is:

```c
for each pattern_matcher in pattern_table:
    matched = pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if matched && priority > best_priority:
        best_priority = priority
        best_id = pattern_id
emitter_table[best_id](ctx, ir_node)  // emit selected instruction
```

For trivial cases (register-register moves, simple immediates), the mega-hub contains inline pattern matching rather than dispatching to external helpers. The 226 KB size is consistent with covering the full SM89/90 SASS instruction set (~2,000+ instruction variants).

## Instruction Scheduling (0x11D4680–0x11EA000)

The final 16 functions form a cohesive list-scheduling subsystem for SASS instruction ordering. Five functions exceed 7 KB and share an identical data structure pattern.

### Shared Data Structure

All scheduling functions operate on a common per-basic-block data structure:

```text
State object layout (offsets from base):
  +832    pointer   Growable array of 184-byte per-BB records
  +840    uint32    Array capacity
  +848    pointer   Arena allocator for 192-byte scheduling contexts
  +864    pointer   Overflow chain (hash-table linked list)
```

Each 184-byte per-BB record contains:

```text
Offset  Size    Content
  +0     8B     Scheduling state pointer
  +4     4B     Instruction timing data
  +20    4B     Latency/priority value
  +25    1B     Hash bucket key (value & 7)
```

Overflow entries use a hash-table with linked-list chaining. The hash function is `key & 7` (3-bit hash, 8 buckets). Rehashing occurs when the table grows. Cleanup uses virtual method dispatch through a vtable at offset +32 of the arena allocator.

### Scheduling Functions

| Function | Size | Role |
|---|---|---|
| `sub_11D6890` | 13 KB | Main basic-block scheduler. Processes instruction lists, manages growing array, allocates scheduling contexts |
| `sub_11D6080` | 12 KB | Scheduling dependency query. Returns bool indicating whether a basic block has dependency hazards |
| `sub_11D5940` | 10 KB | Per-BB scheduling initialization. Populates initial scheduling data for a new basic block |
| `sub_11D4AF0` | 11 KB | Scheduling state update. Modifies scheduling state after instruction reordering. Two hash-table traversals with rehashing |
| `sub_11D52B0` | 9 KB | Scheduling state query. Checks for magic value 711 in `entry[1]`, possibly a phase-completion marker or specific opcode state |

The remaining 11 smaller functions (3–5 KB) at `0x11D7000`--`0x11EA000` are scheduling utilities: operand analysis, latency queries, resource conflict detection, instruction reordering, and a null stub (`nullsub_192` at `0x11D7250`).

This is a classic list-scheduling implementation handling:

- **Data dependency tracking**: RAW (read-after-write), WAR (write-after-read), WAW (write-after-write) hazards.
- **Instruction latency modeling**: Timing data at offsets +4 and +20 of the 184-byte records.
- **Register pressure management**: Integrated with the register allocation configuration from `sub_110BC90`.
- **Control-flow barrier insertion**: For synchronization points (barriers, warp-level ops).

## Whole-Program and Multi-Function Modes

Two alternative codegen orchestrators handle different compilation modes:

### Whole-Program Mode: sub_1109180 (13 KB, 387 lines)

Used when `--extensible-whole-program` is active. Iterates the function list building an ordered worklist. Creates 72-byte per-function register summaries. Tracks function attributes (has-local, has-surface, has-texture) via OR-accumulation of feature flags at offsets 36–63. Checks SM version (`v5+376 > 26`) for feature gating. Calls `sub_12AE300` to get the number of register classes and `sub_465020` to allocate per-class arrays.

### Multi-Function Mode: sub_1107F10 (9 KB, 251 lines)

Used in the default non-WPO compilation mode. Similar structure to `sub_1109180` but without whole-program analysis. Iterates the function list via `sub_449A80` lookups, creates 72-byte summary records, and manages function-level visited sets for call-graph traversal.

## Trace Integration: sub_1109EB0 (15 KB, 481 lines)

Implements the `--fdevice-time-trace` feature for generating Chrome trace format profiling output. Reads an input trace JSON file (`fopen(a1+472, "r")`), parses it via `sub_1CFA4D0`, and merges compilation phase timing data. The default trace name is `"ptxas application ptx input"`. Process identification uses `getpid()`. JSON output is built with the `sub_1CFA2xx` family (create/add-field/add-child) and serialized through `sub_1CF98D0`.

## Call Graph

The complete SM89/90 backend call graph, starting from the ELF output entry point:

```text
sub_1116890 (ELF output + metadata)
  +-- sub_432020 (create option parser)
  +-- sub_1104950 (parse options)
  |     +-- sub_42E390 (register option, ~55 calls)
  |     +-- sub_15C3DD0 (GPU name -> SM version)
  +-- sub_1112F30 (main compilation driver)
  |     +-- sub_110AA30 (per-function codegen init)
  |     |     +-- sub_1100E50 (feature flag setup)
  |     |     +-- sub_110BC90 (register/launch config)
  |     +-- sub_110D2A0 (per-function output finalization)
  |     |     +-- sub_14075D0 (encoding pass 1)
  |     |     +-- sub_1407FC0 (encoding pass 2)
  |     |     +-- sub_14091C0 (encoding pass 3)
  |     |     +-- sub_1627F00 (symbol table entry creation)
  |     +-- sub_1109180 (whole-program function processor)
  |     |     +-- sub_11078F0 (register analysis)
  |     +-- sub_1107F10 (multi-function codegen orchestrator)
  |     |     +-- sub_11078F0 (register analysis)
  |     +-- sub_1111DB0 (function symbol table builder)
  |     |     +-- sub_110FA30 (symbol resolution)
  |     |           +-- sub_110EF30 (expression type analyzer)
  |     |           +-- sub_110E7E0 (initializer expression walker)
  |     +-- sub_464AE0 (thread pool creation)
  |     +-- sub_464C30 (thread pool dispatch)
  +-- sub_1109EB0 (trace JSON integration)
  +-- sub_1103030 (option definition table builder)

sub_119BF40 (ISel mega-hub)
  +-- sub_1190050..sub_119B8F0 (~160 pattern matchers)
  +-- sub_100C110..sub_10FFFFF (~750 instruction encoders)
        +-- sub_4C28B0 (set bitfield)
        +-- sub_4C60F0 (control code slot config)
        +-- sub_4C5F90 (finalize descriptor)
        +-- sub_50xxxx (modifier helpers)

sub_11D6890 (instruction scheduler)
  +-- sub_11D6080 (dependency query)
  +-- sub_11D5940 (per-BB init)
  +-- sub_11D4AF0 (state update)
  +-- sub_11D52B0 (state query)
```

## Key Global Data

| Address | Type | Content |
|---|---|---|
| `dword_1EED2E0` | uint32[] | SM version to architecture family lookup table |
| `off_1EEEFA0` | descriptor[] | ELF metadata field descriptor table |
| `xmmword_1F460E0`--`0x1F47400` | const[] | Instruction encoding constant tables (~750 entries) |
| `dword_2A5D6xx`--`dword_2A5DCxx` | msg_desc[] | Error/warning message descriptors |

## Confidence Assessment

| Claim | Confidence | Verification |
|---|---|---|
| ISA class string "Ada" for sm_89 | CONFIRMED | Decompiled `sub_484F50` line 468: `"Ada"` as ISA class argument to `sub_484DB0` |
| `byte[3] = 1` unique to sm_89 (tessellation/graphics flag) | CONFIRMED | Decompiled `sub_484F50` line 511: `v47->m128i_i8[3] = 1;` after sm_89 block; no other arch sets this |
| SM89/90 shared backend at `0x100C000`--`0x11EA000` (1.9 MB) | HIGH | Address range consistent with function catalog; shared ISel mega-hub at `sub_119BF40` |
| ISel mega-hub `sub_119BF40` at 226 KB | HIGH | Too large for Hex-Rays decompilation; consistent with ~160 pattern matchers |
| Dispatch table: sm_89 B8=`sub_15C2D40`, B0=`sub_15C2C20`, A8=`sub_15C3740` | CONFIRMED | Decompiled `sub_15C0CE0` lines 103-109 match exactly |
| Dispatch table: sm_90 B8=`sub_15C2CE0`, B0=`sub_15C2B30`, A8=`sub_15C3520` | CONFIRMED | Decompiled `sub_15C0CE0` lines 110-116 match exactly |
| sm_90a uses same function pointers as sm_90 | CONFIRMED | Decompiled `sub_15C0CE0` lines 117-123: identical addresses for sm_90a |
| `__CUDA_ARCH__=890` / `__CUDA_ARCH__=900` | CONFIRMED | Strings at `0x1d40ab2` / `0x1d40adc`; decompiled lines 469/518 |
| `compute_89` string exists | CONFIRMED | String at `0x1d40aca` |
| Backend init +348: Ada=0x7005, Hopper=0x8000 | HIGH | Claimed from decompiled backend init functions (distinct A8 slot handlers) |
| Feature 33 gated on SM version 29/30 and debug flag 618 | HIGH | From decompiled `sub_1100E50` feature flag configurator |
| 750 instruction encoder templates at `0x100C000`--`0x10FFFFF` | HIGH | Count from systematic sweep of address range |
| ~160 ISel pattern matchers at `0x1120000`--`0x119BF40` | HIGH | Derived from function catalog of address range |
| Option parser `sub_1104950` registers ~55 options | HIGH | Decompiled function shows extensive `sub_42E390` call sequence |
| Compilation driver `sub_1112F30` at 65 KB, 2,164 lines | HIGH | Decompiled file exists at stated address |
| `--blocks-are-clusters` flag: Hopper-only (offset a2+355) | HIGH | Claimed from decompiled sm_90 backend init; absent from sm_89 |

For general Ada/Hopper architecture details, see the [ptxas wiki: Ada/Hopper](../../ptxas/targets/ada-hopper.html) and [cicc wiki: SM70-89](../../cicc/targets/sm70-89.html).

## Cross-References

### nvlink Internal
- [Embedded ptxas Overview](../ptxas/overview.md) — full address map including the SM89/90 backend at `0x100C000`--`0x11EA000`
- [Instruction Selection Hubs](../ptxas/isel-hubs.md) — the five mega-hub functions, including `sub_119BF40`
- [Architecture Dispatch](../ptxas/arch-dispatch.md) — SM89/SM90 vtable registration and callbacks
- [SM80 Ampere](sm80-ampere.md) — the preceding ISel backend at `0xCA0000`--`0xDA0000`
- [SM90 Hopper](sm90-hopper.md) — SM90 shares this same backend; Hopper-specific features gated by SM version
- [SM75 Turing](sm75-turing.md) — the preceding architecture backend at `0xF16000`--`0x100C000`
- [Architecture Profiles](arch-profiles.md) — SM89 profile metadata

### Sibling Wikis
- [ptxas: Ada/Hopper](../../ptxas/targets/ada-hopper.html) — standalone ptxas SM89/SM90 target documentation
- [cicc: SM70-89](../../cicc/targets/sm70-89.html) — cicc compiler SM89 Ada Lovelace target
