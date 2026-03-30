# Debug Info Pipeline

Debug information in cicc follows a four-stage lifecycle: generation in the EDG/IR-generation frontend, preservation and selective stripping in the optimizer, verification after each pass, and emission as `.loc`/`.file` directives in the PTX backend. This page traces the full journey of debug metadata from CUDA source to PTX output, covering the three compilation modes (`-g`, `-generate-line-info`, neither), the five stripping passes, the NVIDIA-custom verification infrastructure, and the backend emission format with its non-standard inlined-at extension. Understanding this flow is essential for anyone reimplementing cicc's debug info contract, because the NVPTX target's debug model is fundamentally different from x86 DWARF: PTX is a virtual ISA with no physical registers, no real stack, and no fixed instruction encoding, so the debug metadata cicc emits is consumed by ptxas rather than directly by a debugger.

| | |
|---|---|
| **Debug info generation** | `sub_9433F0` (per-parameter), `sub_943430` (per-global), `sub_941230` (source location) |
| **Debug version module flag** | `sub_915400` -- emits `"Debug Info Version"` = 3 |
| **Flag filter** | `sub_12C6910` -- checks `-debug-compile`, `-g`, `-generate-line-info` |
| **Verification pass** | `sub_29C8000` (12,480B, 434 BBs) -- runs after each optimization pass |
| **Per-instruction verifier** | `sub_29C3AB0` (5,592B) |
| **Debugify injector** | `sub_29C1CB0` |
| **Stripping passes** | `#110`--`#114` in the pipeline parser |
| **`.loc` emission** | `sub_31D55F0` (per-instruction), `sub_31E4280` (function-scope `.file`/`.loc`) |
| **DWARF section emission** | `sub_399B1E0` (29KB, `DwarfDebug::beginModule`) |
| **NVVM container field** | `DebugInfo` at container offset +12 (enum: NONE/LINE_INFO/DWARF) |
| **cl::opt registration** | `ctor_043` at `0x48D7F0` -- `debug-compile`, `generate-line-info`, `line-info-inlined-at` |

## Three Compilation Modes

cicc supports three debug info levels. The mode is selected at the CLI layer and propagated through the flag dispatch table into both the optimizer and the backend. The flag filter function `sub_12C6910` reads the CLI flags and routes them to the appropriate pipeline stages.

| CLI flag | Flag struct offset | Routing | NVVM container `DebugInfo` | DICompileUnit emission kind |
|---|---|---|---|---|
| `-g` | `+296` | `-debug-compile` to LNK and OPT stages | `NVVM_DEBUG_INFO_DWARF` (2) | `FullDebug` |
| `-generate-line-info` | `+328` | `-generate-line-info` to OPT stage only | `NVVM_DEBUG_INFO_LINE_INFO` (1) | `LineTablesOnly` |
| (neither) | -- | -- | `NVVM_DEBUG_INFO_NONE` (0) | `NoDebug` |

The distinction between `-g` and `-generate-line-info` is critical and non-obvious:

- **`-g`** routes as `-debug-compile` to **both** the linker (LNK) and optimizer (OPT) stages. The linker stage needs the flag because libdevice linking must preserve debug info from the user module when merging with the stripped libdevice bitcode. The optimizer preserves all metadata: `DICompileUnit`, `DISubprogram`, `DILocalVariable`, `DIType`, scope chains, `dbg.value()`/`dbg.declare()` intrinsics -- everything. The backend emits complete DWARF sections. cuda-gdb can step through source, inspect variables, and reconstruct inlined call stacks.

- **`-generate-line-info`** routes only to the OPT stage (not the linker). Early in the optimizer, `StripNonLineTableDebugInfoPass` strips all metadata except `DILocation` / `DISubprogram` / `DICompileUnit` with `LineTablesOnly` emission kind. This is enough for profiler source correlation (Nsight Compute maps `.loc` directives back to source lines) but not enough for variable inspection or source-level debugging in cuda-gdb.

- **Neither flag**: no debug metadata is generated. The IR-generation frontend skips all debug calls (the `dword_4D046B4` / `[ctx+0x170]` guards prevent emission), and the module has no `llvm.dbg.cu` named metadata. The verification pass detects this in Phase 1 and returns immediately.

## Stage 1: Frontend Debug Metadata Generation

### EDG IL-to-IR Layer

The IR generation frontend creates debug metadata when the debug info flag is active. Two independent guards control this:

- **`dword_4D046B4`**: a global flag checked at parameter and statement codegen entry points. When set, the function prolog emitter (`sub_938240` / Path B equivalent) calls `sub_9433F0` to emit `DILocalVariable` metadata for each parameter, and the statement emitter (`sub_9363D0`) calls `sub_941230` to set the IR builder's debug location from the EDG source position.

- **`[ctx+0x170]`**: a pointer to the `DICompileUnit` object in the codegen context. When non-null, the global variable emitter (`sub_916430` and friends) calls `sub_943430` to attach debug metadata to each `GlobalVariable`, and the module finalizer (`sub_915400`) emits the `"Debug Info Version"` module flag with value 3.

The metadata hierarchy created during IR generation:

```
DICompileUnit
  [ctx+0x170], emission kind: FullDebug or LineTablesOnly
  ├── DIFile (per source file)
  ├── DISubprogram (per __global__ / __device__ function)
  │     ├── DILocalVariable (per parameter, via sub_9433F0)
  │     │     arg: 1-based index from v10 in the parameter iteration loop
  │     │     scope: parent DISubprogram
  │     │     file, line, type: from EDG declaration node
  │     ├── DILocalVariable (per auto variable, via statement codegen)
  │     └── DILocation (per instruction, via sub_941230)
  │           line, column: from EDG source position
  │           scope: nearest enclosing DILexicalBlock or DISubprogram
  └── DIGlobalVariable (per device-side global, via sub_943430)
        [gv+0xAD] < 0 indicates debug info present on the GlobalVariable
```

The module finalizer `sub_915400` runs after all globals and functions have been code-generated. Its debug-relevant actions:

1. Calls `sub_9151E0` to emit `nvvmir.version` metadata. When `[ctx+0x170]` is non-null, the version tuple has 4 operands instead of 2, including address-space-qualified indices.
2. Calls `sub_914410` to emit `nvvm.annotations` metadata.
3. If `[ctx+0x170] != 0`: calls `sub_BA93D0` (`Module::addModuleFlag`) with `("Debug Info Version", 3)`. This module flag is mandatory -- without it, LLVM's DWARF backend refuses to emit debug sections.

### DIBuilder Infrastructure

The actual metadata node creation uses LLVM's `DIBuilder` infrastructure at `0xAD0000`--`0xAF0000` (Zone 2 of the type system module). This includes `DIBasicType` / `DIDerivedType` / `DICompositeType` uniquing, scope chain construction, and the standard LLVM `!dbg` attachment API. cicc uses the standard LLVM `DIBuilder` without modifications -- the NVIDIA-specific aspects are in the calling patterns (which EDG nodes map to which DI metadata), not in the metadata creation API itself.

## Stage 2: Optimizer Preservation and Stripping

### The StripNonLineTableDebugInfoPass

When `-generate-line-info` is active (but not `-g`), the optimizer runs `StripNonLineTableDebugInfoPass` (`"strip-nonlinetable-debuginfo"`, pipeline parser slot #114) early in the pipeline. This pass:

1. Strips all `DILocalVariable` and `DIGlobalVariable` metadata
2. Removes all `dbg.value()` and `dbg.declare()` intrinsics
3. Strips `DIType` nodes, imported entities, and retained nodes
4. Downgrades `DICompileUnit` emission kind from `FullDebug` to `LineTablesOnly`
5. Preserves `DISubprogram`, `DILocation`, `DIFile`, and `DICompileUnit` (the minimum needed for `.loc` directives)

After this pass, the module has enough metadata for line-table-based profiling but not for source-level debugging.

### The Five Stripping Passes

cicc registers five debug stripping passes in the pipeline parser, all standard LLVM passes:

| Pipeline name | Slot | LLVM pass class | What it strips | What survives |
|---|---|---|---|---|
| `"strip-dead-debug-info"` | #110 | `StripDeadDebugInfoPass` | Debug info for dead functions/globals | Everything for live code |
| `"strip-debug-declare"` | #112 | `StripDebugDeclarePass` | `dbg.declare()` intrinsics only | `dbg.value()`, all metadata |
| `"strip-nondebug"` | #113 | `StripNonDebugSymbolsPass` | Non-debug symbols | All debug metadata |
| `"strip-nonlinetable-debuginfo"` | #114 | `StripNonLineTableDebugInfoPass` | Everything except line tables | `DILocation`, `DISubprogram`, `DIFile` |
| (core stripping at `0xAE0000`) | -- | `stripDebugInfo()` | All `llvm.dbg.*` intrinsics | Nothing |

The core debug stripping implementation at `0xAE0000` (Zone 3 of the type system module) is the nuclear option -- it calls `stripDebugInfo()` to remove everything. The four named passes provide finer granularity.

### Optimizer Pass Behavior with Debug Info

Every standard LLVM optimization pass is expected to preserve debug metadata it does not intentionally modify. In practice, some passes degrade debug info quality:

**Passes that preserve debug info well:**
- InstCombine: updates `dbg.value()` when simplifying instructions, uses `replaceAllDbgUsesWith`
- SROA: splits `dbg.declare()` into multiple `dbg.value()` fragments when decomposing allocas
- GVN: preserves debug locations on replacement instructions
- SimplifyCFG: maintains `DILocation` through block merging

**Passes that commonly degrade debug info:**
- Inlining: creates new `DISubprogram` for inlined functions, must maintain inlined-at chains. Failure to do so triggers the verifier's `"did not generate DISubprogram"` diagnostic.
- LoopUnroll: duplicates instructions without always duplicating `DILocation` scope context
- LICM: moves instructions out of loops, potentially detaching them from their original scope
- Dead code elimination: removes instructions along with their `dbg.value()` references
- Tail merging / BranchFolding: merges basic blocks from different source scopes

The verification pass (`sub_29C8000`) runs after each optimization pass and tracks exactly which passes degrade debug info. When the `debugify-each` knob is active, the full Debugify-then-CheckDebugify cycle runs around every pass, injecting synthetic debug metadata before the pass and verifying it survived afterward.

## Stage 3: Debug Info Verification

The verification pass `sub_29C8000` is documented in detail on the [Debug Info Verification](../infra/debug-verify.md) page. Here we summarize its role in the pipeline.

### Pipeline Integration Protocol

The pipeline runner invokes the verifier as a sandwich around each optimization pass:

```c
// Pseudocode for the verification protocol
snapshot_debug_metadata(M);          // Phase 2 of sub_29C8000: 8 hash tables
run_optimization_pass(M, "instcombine");
sub_29C8000(M, errs(), dbgCU, hashMap, "instcombine", 11, file, fileLen, jsonOut);
// Returns: true = PASS, false = FAIL (debug info degraded)
```

The pass name argument lets the JSON report attribute degradation to the specific pass responsible. The eight-table metadata snapshot captures `DISubprogram`, `DIScope`, `DIGlobalVariable`, `DILocalVariable`, `DIType`, `DIImportedEntity`, `DILabel`, and retained nodes -- far more comprehensive than upstream LLVM's `CheckDebugInfoPass`, which only tracks subprograms and debug variable intrinsics.

### Verification Modes

Three modes of debug verification exist, controlled by LLVM knobs:

| Mode | Knob | What runs |
|---|---|---|
| Standard | `verify-each` or `verify-after-all` | `sub_29C8000` after every pass |
| Debugify | `debugify-each` | `sub_29C1CB0` (inject) + pass + `sub_29C8000` (check) |
| Selective | `verify-debuginfo-preserve` | Lighter-weight preservation checking |

The Debugify mode is especially powerful: it first injects synthetic debug metadata via `sub_29C1CB0` (ensuring every instruction has a `DILocation` and every variable has `dbg.value()`), then runs the optimization pass, then checks whether the synthetic metadata survived. This detects passes that drop debug info even when the original module had sparse or no debug metadata.

### Behavior in `-generate-line-info` Mode

When the module is in `LineTablesOnly` mode (after `StripNonLineTableDebugInfoPass` has run), the verifier still executes but its scope is narrower. Phase 5 (per-function debug variable checking) skips variable intrinsic validation because `dbg.value()`/`dbg.declare()` were intentionally stripped. Only Phase 6 (per-instruction `DILocation` verification via `sub_29C3AB0`) remains fully active, checking that:

- Every instruction with a `DebugLoc` has a valid `DILocation`
- `DILocation` scope chains resolve to a valid `DISubprogram`
- No orphaned debug locations reference deleted subprograms
- BB-level consistency is maintained

## Stage 4: Backend Emission

### The `.loc` Directive

The AsmPrinter emits DWARF `.loc` directives as inline annotations in the PTX instruction stream. The per-instruction emitter `sub_31D55F0` runs after each real (non-meta) instruction when `HasDebugInfo` (`r15+0x1E8`) is set. It reads the `DebugLoc` attached to each `MachineInstr` and emits:

```ptx
.loc 1 42 0
ld.param.u64 %rd1, [_Z6kernelPf_param_0];
.loc 1 43 5
mul.wide.u32 %rd2, %r1, 4;
```

The function-scope emitter `sub_31E4280` handles `.file` directives that establish the file index table, and `sub_31E6100` (`insertDebugLocEntry`) maintains a file/line-to-MCSymbol mapping for MBB boundaries used in DWARF line table construction.

### The NVIDIA Inlined-At Extension

Standard LLVM `.loc` emits only `file line column`. cicc extends `.loc` with `function_name` and `inlined_at` attributes that encode the full inlining chain:

```ptx
.loc 1 42 0, function_name _Z6kernelPf, inlined_at 2 15 3
```

This allows ptxas to reconstruct the complete call stack at any point in inlined code, so cuda-gdb can show the user which function was inlined and where. The implementation in the AsmPrinter:

1. Reads the `DebugLoc` from the `MachineInstr`
2. Walks the inlined-at chain via `DebugLoc::getInlinedAt()`
3. Builds a work list (`SmallVector<DebugLoc, 8>`) of the full chain
4. Emits in reverse order (outer locations before inner) so ptxas sees the outermost caller first
5. Tracks already-emitted inlined-at locations in an `InlinedAtLocs` set to prevent duplicates

The `line-info-inlined-at` LLVM knob (registered at `0x48D7F0`, `cl::opt<bool>`) controls whether this extension is active. The CLI flag `-no-lineinfo-inlined-at` disables it by setting `-line-info-inlined-at=0` on the backend command line. When disabled, only the immediate source location is emitted, losing inlining context but producing smaller PTX.

### The `dwarf-extended-loc` Knob

The `dwarf-extended-loc` knob (enum: `Default`/`Enable`/`Disable`, registered at `0x490000` area) controls whether extended flags appear in `.loc` directives:

| Value | Effect |
|---|---|
| `Default` (0) | Platform-dependent behavior |
| `Enable` (1) | Emit `is_stmt`, `prologue_end`, `discriminator` extensions |
| `Disable` (2) | Bare `.loc file line column` only |

The `Disable` mode exists for compatibility with older ptxas versions that do not parse extended `.loc` flags. When enabled, the extended flags allow cuda-gdb to identify statement boundaries (`is_stmt`), function entry points (`prologue_end`), and distinguish between multiple code paths at the same source line (`discriminator`).

### Source Interleaving

The `-show-src` CLI flag (flag struct offset `+808`, routed to the backend as `-nvptx-emit-src`) enables the `InterleaveSrcInPtx` mode. When active, the AsmPrinter reads source file lines and emits them as comments interleaved with the PTX:

```ptx
// kernel.cu:42    float val = input[idx];
.loc 1 42 0
ld.global.f32 %f1, [%rd2];
// kernel.cu:43    val = val * val;
.loc 1 43 0
mul.f32 %f2, %f1, %f1;
```

This is purely a readability feature -- the comments are ignored by ptxas and have no effect on debug quality. The `nvptx-emit-src` LLVM knob description string is `"Emit source line in ptx file"`.

### `.file` Directive Emission

The `.file` directives are emitted by `emitDwarfFileEntries` during `doFinalization` (`sub_3972F10`, 24KB). They map source filenames to numeric file indices referenced by `.loc`:

```ptx
.file 1 "/path/to/kernel.cu"
.file 2 "/usr/local/cuda/include/cuda_runtime.h"
```

The file table is built incrementally as `.loc` directives reference new files during instruction emission. The DWARF line section symbols are created via `sub_E808D0` (`createTempSymbol` for DwarfLineSection) and bound via `sub_E81A00` (`emitDwarfLineSection`).

### DWARF Section Emission

When full debug info (`-g`) is active, a separate DWARF emission module at `0x3990000`--`0x39DF000` generates complete DWARF debug sections. This is standard LLVM DWARF emission with no significant NVIDIA modifications to the section format:

| Address | Size | Function |
|---|---|---|
| `sub_399B1E0` | 29KB | `DwarfDebug::beginModule()` -- initializes from `llvm.dbg.cu`, strings: `"DWARF Debug Writer"`, `"DWARF Emission"` |
| `sub_3997B50` | 33KB | `.debug_aranges` emission -- address range tables |
| `sub_399D1D0` | 12KB | Range list emission (`DW_RLE_base_address`, `DW_RLE_offset_pair`, `DW_RLE_start_length`) |
| `sub_399EB70` | 12KB | Register location expressions -- strings: `"no DWARF register encoding"`, `"sub-register"` |
| `sub_39BDF60` | 38KB | `.debug_names` accelerator table -- bucket count, name count, augmentation string |
| `sub_39B6390` | 33KB | DWARF form size calculator -- switch on `DW_FORM_*` codes |
| `sub_215ACD0` | 8.1KB | Module-level emission entry (NVPTX Debug Info Emission) |

The module-level entry `sub_215ACD0` checks `*(a1+240)->field_344` to determine if DWARF is enabled, then looks up the `"NVPTX DWARF Debug Writer"` / `"NVPTX Debug Info Emission"` pass info. The NVPTX backend does not emit physical register locations -- GPUs have no DWARF register numbering scheme that maps to hardware. Instead, it emits virtual register references that ptxas resolves through SASS-level debug info.

The DWARF string/enum tables at `0xE00000`--`0xE0FFFF` (tag-to-string conversion, attribute-to-string, operation encoding) are stock LLVM 20 `BinaryFormat/Dwarf.cpp` utilities with no visible NVIDIA modifications.

### `.target` Debug Suffix

The header emission function `sub_214F370` appends `, debug` to the `.target` directive when `MCAsmInfo::doesSupportDebugInformation()` returns true:

```ptx
.target sm_90, texmode_independent, debug
```

This suffix tells ptxas that the PTX contains debug information and should be processed accordingly. Without it, ptxas ignores `.loc` and `.file` directives.

## NvvmDebugVersion

The NVVM container format includes a debug version field at header bytes `0x08`--`0x09`:

| Offset | Size | Field |
|---|---|---|
| `0x08` | 1 byte | `NvvmDebugVersion.Major` |
| `0x09` | 1 byte | `NvvmDebugVersion.Minor` |

Current version: `Major=3, Minor<=2`. The version check logic in `sub_CD41B0`:

- `Major` must equal 3 (hard fail on mismatch: `"not compatible"` error, returns NULL)
- `Minor > 2`: warning printed, parse continues
- If absent: default `{3, 2}` is assumed

This version tracks the debug metadata schema independently of the NVVM IR version (`NvvmIRVersion` at `0x06`--`0x07`, current `Major=2, Minor<=0x62`). The separation allows debug format evolution without breaking IR compatibility -- NVIDIA can add new debug metadata fields (e.g., for new SM features) without requiring a full IR version bump.

The container's `DebugInfo` field (at deserialized struct offset +12) also encodes the debug level as an enum that must be consistent with the module metadata:

```c
enum NvvmDebugInfo {
    NVVM_DEBUG_INFO_NONE      = 0,  // no debug info
    NVVM_DEBUG_INFO_LINE_INFO = 1,  // -generate-line-info
    NVVM_DEBUG_INFO_DWARF     = 2   // -g
};
```

The standalone pipeline validates this at IR intake: if `debug_info_present AND debug_mode_flag AND NOT debug_version_validated`, the function returns error code 3 (incompatible).

## Debug Records Format

cicc v13.0 inherits LLVM 20's support for the new debug records format (`DbgRecord`) as an alternative to the traditional `dbg.value()` / `dbg.declare()` intrinsics. Three knobs control this:

| Knob | Type | Default | Effect |
|---|---|---|---|
| `write-experimental-debuginfo` | bool | true | Write debug info in new non-intrinsic format |
| `write-experimental-debuginfo-iterators-to-bitcode` | bool | true | Serialize debug records to bitcode |
| `preserve-input-debuginfo-format` | boolOrDefault | false | When true, preserve whatever format the input uses |

The `write-experimental-debuginfo` default of `true` means cicc v13.0 uses the new `DbgRecord` format internally by default. This is an LLVM 20 feature where debug info is stored as `DbgVariableRecord` and `DbgLabelRecord` objects attached directly to instructions rather than as separate `dbg.value()` intrinsic calls. The format change is transparent to the optimizer and backend -- the verification pass and AsmPrinter handle both formats identically.

## End-to-End Flow Diagram

```
CUDA Source (.cu / .cup)
    │
    ▼
EDG 6.6 Frontend (IL tree)
    │  dword_4D046B4 / [ctx+0x170] guards debug emission
    │  sub_9433F0: per-parameter DILocalVariable
    │  sub_943430: per-global DIGlobalVariable
    │  sub_941230: per-instruction DILocation
    │  sub_915400: "Debug Info Version" = 3 module flag
    ▼
LLVM Module with debug metadata
    │  llvm.dbg.cu → DICompileUnit → DISubprogram → ...
    │
    ├─ If -generate-line-info:
    │    StripNonLineTableDebugInfoPass (#114)
    │    strips variables, types, scopes; keeps DILocation/DISubprogram
    │
    ▼
LLVM Optimizer (sub_12E54A0)
    │  ┌─────────────────────────────────────────────┐
    │  │  For each pass:                              │
    │  │    snapshot = sub_29C8000 Phase 2 (8 tables) │
    │  │    run_pass(M);                              │
    │  │    sub_29C8000(M, ..., passName, ...);       │
    │  │    if FAIL: JSON report + diagnostic         │
    │  └─────────────────────────────────────────────┘
    ▼
Optimized LLVM Module
    │
    ▼
NVPTX Backend (SelectionDAG → MachineInstr)
    │  DebugLoc attached to each MachineInstr
    │
    ▼
AsmPrinter (sub_31EC4F0)
    │  sub_31D55F0: per-instruction .loc emission
    │  sub_31E4280: .file/.loc at function scope
    │  inlined-at chain walking → function_name, inlined_at extensions
    │  InterleaveSrcInPtx: source line comments
    │
    ├─ If -g:
    │    sub_399B1E0: DwarfDebug::beginModule()
    │    sub_3997B50: .debug_aranges
    │    sub_39BDF60: .debug_names
    │
    ▼
PTX Output
    .target sm_90, texmode_independent, debug
    .file 1 "kernel.cu"
    .loc 1 42 0, function_name _Z6kernelPf
    ld.param.u64 %rd1, [_Z6kernelPf_param_0];
```

## Knobs Reference

| Knob | Type | Default | Scope | Effect |
|---|---|---|---|---|
| `-g` / `-debug-compile` | bool | off | CLI | Full debug compilation (`FullDebug` emission) |
| `-generate-line-info` | bool | off | CLI | Line tables only (`LineTablesOnly` emission) |
| `-no-lineinfo-inlined-at` | bool | off | CLI | Disable inlined-at tracking (sets `-line-info-inlined-at=0`) |
| `-show-src` / `-nvptx-emit-src` | bool | off | CLI | Interleave source lines as PTX comments |
| `dwarf-extended-loc` | enum | Default | LLVM | `Default`/`Enable`/`Disable` extended `.loc` flags |
| `dwarf-version` | unsigned | (platform) | LLVM | DWARF version for debug sections |
| `line-info-inlined-at` | bool | true | LLVM | Emit inlined-at chains in `.loc` directives |
| `debugify-each` | bool | off | LLVM | Debugify + CheckDebugify around every pass |
| `debugify-level` | enum | location+variables | LLVM | `locations` or `location+variables` |
| `debugify-quiet` | bool | off | LLVM | Suppress debugify diagnostics |
| `debugify-func-limit` | int | unlimited | LLVM | Max functions to debugify |
| `debugify-export` | string | -- | LLVM | Export debugify results to file |
| `verify-each` | bool | off | LLVM | Run IR verifier after every pass |
| `verify-debuginfo-preserve` | bool | off | LLVM | Enable debug info preservation checking |
| `no-inline-line-tables` | bool | off | LLVM | Prevent inlining from merging line tables |
| `write-experimental-debuginfo` | bool | true | LLVM | Use DbgRecord format instead of intrinsics |
| `preserve-input-debuginfo-format` | boolOrDefault | false | LLVM | Preserve input debug info format as-is |
| `NvvmDebugVersion` | {u8,u8} | {3,2} | Container | Debug metadata schema version |
| `qword_5008FC8` | bool | off | Global | Verbose diagnostic output enable |
| `qword_5008C88` | int32 | >0 | Global | Metadata depth threshold (<=0 skips deep scope walk) |

## NVIDIA Modifications vs Stock LLVM

1. **Inlined-at `.loc` extension.** Upstream LLVM's NVPTX AsmPrinter emits standard `.loc file line column`. cicc appends `function_name` and `inlined_at` attributes that encode the full inlining chain for cuda-gdb call stack reconstruction.

2. **Eight-table verification.** Upstream `CheckDebugInfoPass` tracks `DISubprogram` and debug variable intrinsics. NVIDIA's `sub_29C8000` maintains eight separate hash tables covering subprograms, scopes, global variables, local variables, types, imported entities, labels, and retained nodes.

3. **JSON structured reporting.** NVIDIA added a YAML/JSON serializer to the verification pass that produces machine-parseable bug reports with per-pass attribution -- no upstream equivalent.

4. **Metadata reconstruction.** After verification, NVIDIA's pass reconstructs the module's metadata tables from verified versions (Phase 8), effectively serving as a "repair" pass that normalizes metadata after corruption.

5. **Container debug versioning.** The `NvvmDebugVersion` field in the NVVM container header tracks the debug metadata schema independently of the IR version -- a concept that does not exist in upstream LLVM.

6. **Three-level debug info enum.** The `NVVM_DEBUG_INFO_NONE` / `LINE_INFO` / `DWARF` enum in the container provides a compile-unit-level debug mode indicator that ptxas and libNVVM can check without parsing the full module metadata.

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_9433F0` | -- | Emit `DILocalVariable` for function parameter |
| `sub_943430` | -- | Emit debug info for `GlobalVariable` (conditional on `[ctx+0x170]`) |
| `sub_941230` | -- | Set IR builder `DebugLoc` from EDG source position |
| `sub_915400` | 133B | Module finalizer: emit `"Debug Info Version" = 3` module flag |
| `sub_12C6910` | -- | Flag filter: checks `-debug-compile`, `-g`, `-generate-line-info` |
| `sub_29C8000` | 12,480B | Debug info verification pass (main entry) |
| `sub_29C3AB0` | 5,592B | Per-instruction `DILocation` verifier |
| `sub_29C1CB0` | -- | Debugify synthetic debug info injector |
| `sub_22702B0` | -- | `NewPMCheckDebugifyPass` wrapper |
| `sub_2270390` | -- | `NewPMDebugifyPass` wrapper |
| `sub_31D55F0` | -- | Per-instruction `.loc` emission |
| `sub_31E4280` | -- | Function-scope `.file`/`.loc` emission |
| `sub_31E6100` | -- | `insertDebugLocEntry` (file/line to MCSymbol mapping) |
| `sub_31D89B0` | -- | Instruction-level debug comment emission |
| `sub_214F370` | 7.2KB | `emitHeader` (`.version`, `.target ... , debug`) |
| `sub_215ACD0` | 8.1KB | Module-level emission entry / NVPTX Debug Info Emission |
| `sub_399B1E0` | 29KB | `DwarfDebug::beginModule()` |
| `sub_3997B50` | 33KB | `.debug_aranges` emission |
| `sub_399D1D0` | 12KB | Range list emission (`DW_RLE_*`) |
| `sub_399EB70` | 12KB | Register location expressions |
| `sub_39BDF60` | 38KB | `.debug_names` accelerator table |
| `sub_39B6390` | 33KB | DWARF form size calculator |
| `sub_ADCDB0` | -- | `DIBuilder` / debug metadata helper |
| `sub_48D7F0` | -- | `cl::opt` registration: `debug-compile`, `generate-line-info`, `line-info-inlined-at` |
| `sub_CD41B0` | -- | NVVM container version check (validates `NvvmDebugVersion.Major == 3`) |

## Cross-References

- [Debug Info Verification](../infra/debug-verify.md) -- detailed `sub_29C8000` algorithm, 9-phase walk, JSON output format
- [AsmPrinter & PTX Body Emission](../infra/asmprinter.md) -- `.loc`/`.file` directive emission, per-instruction debug annotation, inlined-at chain
- [PTX Emission](./emission.md) -- module-level emission, `.target ... , debug` suffix
- [Entry Point & CLI](./entry.md) -- `-g`, `-generate-line-info` flag parsing in `sub_8F9C90`
- [NVVM IR Generation](./ir-generation.md) -- dual-path architecture, codegen context
- [CLI Flags](../config/cli-flags.md) -- flag routing through the 3-column dispatch table
- [LLVM Knobs](../config/knobs.md) -- `debugify-*`, `verify-each`, `dwarf-*` knobs
- [Pipeline & Ordering](../llvm/pipeline.md) -- where debug verification fits in the pass ordering
- [NVVM Container](../structs/nvvm-container.md) -- `NvvmDebugVersion` field in the binary header
- [Inliner Cost Model](../lto/inliner-cost.md) -- inlining decisions that create the inlined-at chains
