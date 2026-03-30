# Debug Info Verification

cicc includes a custom debug info verification pass (`sub_29C8000`) that validates DWARF-like debug metadata after each optimization pass in the pipeline. This is not the upstream LLVM IR Verifier (`llvm::Verifier::verify(Module)`); it is an NVIDIA-specific implementation derived from LLVM's `CheckDebugInfoPass` (in `Debugify.cpp`) with two significant extensions: a structured JSON reporting mechanism that tracks exactly which optimization passes degrade debug info quality, and a configurable verbosity system that allows the verification overhead to be tuned from silent to exhaustive. The pass lives in a self-contained module of approximately 93 functions in the `0x29C0000`--`0x29FFFFF` address range, alongside the Debugify synthetic debug info injector and general pass infrastructure utilities. Its purpose is to ensure that when a developer compiles with `-g` or `-generate-line-info`, the debug metadata that cuda-gdb and Nsight Compute rely on survives the aggressive optimization pipeline intact.

| | |
|---|---|
| **Primary function** | `sub_29C8000` (12,480 bytes, 434 basic blocks) |
| **Address range** | `0x29C8000` -- `0x29CB0C0` |
| **Per-instruction verifier** | `sub_29C3AB0` (5,592 bytes) |
| **Debugify injector** | `sub_29C1CB0` |
| **NewPM wrappers** | `sub_22702B0` (`NewPMCheckDebugifyPass`), `sub_2270390` (`NewPMDebugifyPass`) |
| **Pipeline parser names** | `"check-debugify"` (pass #26), `"debugify"` (pass #35) |
| **Verbose output flag** | `qword_5008FC8` (bool) |
| **Depth threshold** | `qword_5008C88` (int32) |
| **Upstream origin** | `llvm/lib/Transforms/Utils/Debugify.cpp` -- `CheckDebugInfoPass` |

## Pipeline Integration

The verifier operates as an interleaved "check" pass. The pipeline runner invokes it as a sandwich around each optimization pass:

```c
// Pseudocode for the pipeline runner's verification protocol
snapshot_debug_metadata(M);
run_optimization_pass(M, "instcombine");
sub_29C8000(M, errs(), dbgCU, hashMap, "instcombine", 11, file, fileLen, jsonOut);
```

The pass name argument identifies which optimization just ran, so the JSON report can attribute any debug info degradation to the specific pass responsible. When the `verify-each` / `verify-after-all` LLVM knob is active, the verifier runs after every pass. When `debugify-each` is active, the full Debugify-then-CheckDebugify cycle runs instead, which first injects synthetic debug metadata (via `sub_29C1CB0`), runs the pass, then verifies the synthetic metadata survived.

The function signature reconstructed from the binary:

```c
bool sub_29C8000(
    Module*       module,       // rdi
    raw_ostream&  output,       // rsi -- diagnostic stream
    NamedMDNode*  dbgCU,        // rdx -- "llvm.dbg.cu" metadata
    DenseMap*     hashMap,      // rcx -- metadata identity table
    const char*   passName,     // r8
    size_t        passNameLen,  // stack+0x00
    const char*   fileName,     // stack+0x08
    size_t        fileNameLen,  // stack+0x10
    raw_ostream*  jsonOutput,   // stack+0x18 -- NULL if no JSON report
    ...
);
// Returns: true = all checks passed, false = any violation detected
```

## Verification Algorithm

The pass proceeds through nine sequential phases within a single function call. The 0x4B8-byte stack frame holds eight separate tracking data structures.

### Phase 1: Module-Level Guard

Looks up the `"llvm.dbg.cu"` named metadata node via `sub_BA8DC0` (`Module::getNamedMetadata`). If absent or empty, prints `": Skipping module without debug info\n"` and returns 0. This is the fast path for modules compiled without `-g`.

### Phase 2: Pre-Pass Metadata Snapshot

Initializes eight `SmallVector`/`DenseMap` structures on the stack and walks the compile unit metadata tree:

| Stack offset | Purpose | Copy helper |
|---|---|---|
| `var_1F0` | DISubprogram tracking set | `sub_29C6AD0` |
| `var_1D0` | Scope chain working set | `sub_29C1190` |
| `var_1A0` | DIVariable tracking | `sub_29C1060` |
| `var_170` | Scope-to-function mapping | -- |
| `var_140` | DICompileUnit refs | -- |
| `var_130` | Primary metadata node buffer | -- |

For each `DICompileUnit` operand, the pass walks the subprogram list and retained types, recording every metadata node in hash tables for O(1) identity comparison. The hash function is:

```c
uint64_t hash = ((ptr >> 4) ^ (ptr >> 9)) & (bucket_count - 1);
```

This matches LLVM's `DenseMap` hash with linear probing, empty sentinel `0x0`, tombstone `0xFFFFFFFFF000`, and secondary sentinel `0xFFFFFFFFE000`.

### Phase 3: DISubprogram Iteration

Walks the subprogram list attached to each compile unit via linked-list traversal (`[node+8]` = next pointer). For each subprogram, reads the metadata tag byte at `[node-18h]`:

| Tag byte | DWARF tag | Action |
|---|---|---|
| `0x54` (`'T'`) | `DW_TAG_template_parameter` | Skip |
| `0x55` (`'U'`) | Compile unit / subprogram variant | Special handling |
| `0x44` (`'D'`) | `DW_TAG_subprogram` | Validate |
| `0x45` (`'E'`) | `DW_TAG_lexical_block` | Validate scope chain |
| `0x46` (`'F'`) | `DW_TAG_lexical_block_file` | Validate scope chain |
| `0x47` (`'G'`) | `DW_TAG_namespace` | Validate scope chain |

The flag byte at `[rdx+21h] & 0x20` tests the "definition" bit (only defined, non-declaration subprograms are tracked). Values outside `0x44`--`0x47` are flagged as invalid scope types.

### Phase 4: Hash Table Construction

Allocates and populates eight sorted hash tables via `sub_C7D670` (aligned_alloc, alignment=8), each holding 16-byte entries `[pointer, secondary_key]`:

| Object offset | Table contents | Purpose |
|---|---|---|
| `+18h` | DISubprogram | Function-level metadata |
| `+28h` | DIScope | Scope hierarchy |
| `+48h` | DIGlobalVariable | Module-level variables |
| `+58h` | DILocalVariable | Function-local variables |
| `+78h` | DIType | Type descriptions |
| `+88h` | DIImportedEntity | `using` declarations |
| `+A8h` | DILabel | Label metadata |
| `+B8h` | Retained nodes | Misc retained metadata |

### Phase 5: Per-Function Debug Variable Checking

Iterates every function in the module. For each, looks up its `DISubprogram` in the hash table and cross-references `dbg.value()` / `dbg.declare()` intrinsics against the pre-snapshot. Two diagnostic levels:

**ERROR** (pass dropped a subprogram entirely):
```
ERROR: <pass> dropped DISubprogram of <function> from <file>
ERROR: <pass> did not generate DISubprogram for <function> from <file>
```

**WARNING** (pass dropped individual variable tracking):
```
WARNING: <pass> drops dbg.value()/dbg.declare() for <var> from function <func> (file <file>)
```

The distinction between "dropped" and "did not generate" is significant: "dropped" means metadata existed before the pass and was deleted; "not-generate" means the pass created new IR (e.g., from inlining or outlining) without attaching corresponding debug metadata. This taxonomy is important for GPU compilation because kernel outlining and device function inlining frequently create new IR nodes.

### Phase 6: Per-Instruction Location Verification

Delegated to `sub_29C3AB0` (5,592 bytes), which performs detailed checks:

- Every instruction with a `DebugLoc` has a valid `DILocation`
- `DILocation` scope chains resolve to a valid `DISubprogram`
- No orphaned debug locations reference deleted subprograms
- BB-level consistency: all instructions in a basic block share compatible scopes
- Dropped location tracking: emits `"dropped DILocation"` diagnostics

The JSON output from this sub-pass uses structured field names: `"DILocation"`, `"bb-name"`, `"fn-name"`, `"action"` (with values `"drop"` or `"not-generate"`).

### Phase 7: JSON Structured Output

When a non-null JSON output stream is provided (the `jsonOutput` parameter), the pass serializes a structured report via `sub_2241E40` (YAML/JSON serializer):

```json
{"file":"kernel.cu", "pass":"instcombine", "bugs": [
  {"metadata":"DISubprogram", "name":"_Z6kernelPf", "fn-name":"_Z6kernelPf", "action":"drop"},
  {"metadata":"dbg-var-intrinsic", "name":"idx", "fn-name":"_Z6kernelPf", "action":"not-generate"}
]}
```

This JSON reporting mechanism is an NVIDIA extension with no upstream LLVM equivalent. It feeds into NVIDIA's internal CI infrastructure to track debug info quality regressions across compiler versions. The `"no-name"` string serves as fallback when the pass name pointer is NULL.

### Phase 8: Result Reporting and Metadata Reconstruction

Prints the summary line (`"<pass>: PASS\n"` or `"<pass>: FAIL\n"`), then reconstructs the module's metadata tables from the verified versions -- reallocating subprogram, type, variable, label, and global variable arrays and copying verified metadata back into the compile unit structures.

The result is a 3-way outcome in bit flags:
- Bit 0: any verification failure (determines PASS/FAIL)
- Bit 1: JSON report was requested and successfully written

The final result is PASS only if all sub-checks passed AND the JSON report (if requested) was successfully written.

## GPU Debug Info: What PTX Needs

DWARF for PTX differs fundamentally from DWARF for x86. PTX is a virtual ISA -- there are no physical registers, no real stack, and no fixed instruction encoding. The debug metadata cicc emits serves two consumers: cuda-gdb (which maps PTX locations back to source) and ptxas (which carries debug info forward into SASS/ELF for the hardware debugger).

### The .loc Directive

The AsmPrinter (`sub_31D55F0`) emits DWARF `.loc` directives before each PTX instruction that has a valid `DebugLoc`:

```
.loc 1 42 0          // file 1, line 42, column 0
ld.param.u64 %rd1, [_Z6kernelPf_param_0];
.loc 1 43 5
mul.wide.u32 %rd2, %r1, 4;
```

The `.file` directives (`sub_31E4280`) establish the file table, and `sub_31E6100` maintains a file/line-to-MCSymbol mapping for line table construction.

The `dwarf-extended-loc` knob (enum: `Default`/`Enable`/`Disable`) controls whether extended flags appear in `.loc` directives. When disabled, cicc emits bare `.loc file line column` without the `is_stmt`, `prologue_end`, or `discriminator` extensions. This is relevant because older ptxas versions do not parse extended `.loc` flags.

### The line-info-inlined-at Extension

The `-line-info-inlined-at` LLVM knob (exposed as `-no-lineinfo-inlined-at` in the cicc CLI, which sets `-line-info-inlined-at=0` on the backend) controls whether inlined-at chains are preserved in PTX line info. When enabled (the default), every `.loc` directive for inlined code carries the full inlining chain so cuda-gdb can reconstruct the call stack at any point in the inlined code. When disabled, only the immediate source location is emitted, losing the inlining context but producing smaller PTX.

### The -show-src / nvptx-emit-src Feature

The `-show-src` CLI flag (stored at flag struct offset `+808`, routed to the backend as `-nvptx-emit-src`) enables source line interleaving in PTX output. When active, the AsmPrinter annotates each `.loc` directive with the corresponding source line as a PTX comment:

```
// kernel.cu:42    float val = input[idx];
.loc 1 42 0
ld.global.f32 %f1, [%rd2];
// kernel.cu:43    val = val * val;
.loc 1 43 0
mul.f32 %f2, %f1, %f1;
```

This is purely a readability feature for developers inspecting PTX output. It has no effect on cuda-gdb or debug quality -- the source text is embedded as comments that ptxas ignores.

### NvvmDebugVersion

The NVVM container format includes a debug version field (`NvvmDebugVersion`, packed as `{Major:uint16, Minor:uint16}` at container offset `0x08`--`0x09`). The current version is `Major=3, Minor<=2`. The reader validates that `Major` equals 3 and warns if `Minor` exceeds 2. If absent, the default `{3, 2}` is assumed. This version tracks the debug metadata schema independently of the NVVM IR version, allowing debug format evolution without breaking IR compatibility.

## Debug Info Stripping Passes

cicc includes five stripping passes registered in the pipeline parser (at `sub_12C6910` and related):

| Pipeline name | LLVM pass | Effect |
|---|---|---|
| `"strip-dead-debug-info"` (#110) | `StripDeadDebugInfoPass` | Remove debug info for dead functions/globals |
| `"strip-debug-declare"` (#112) | `StripDebugDeclarePass` | Remove `dbg.declare()` intrinsics only |
| `"strip-nondebug"` (#113) | `StripNonDebugSymbolsPass` | Remove non-debug symbols (keep debug) |
| `"strip-nonlinetable-debuginfo"` (#114) | `StripNonLineTableDebugInfoPass` | Strip everything except line tables |

The `strip-nonlinetable-debuginfo` pass is the key one for the `-generate-line-info` mode: it strips all debug metadata except `.loc` / `.file` directives, producing line-number-only debug info without variable locations, type descriptions, or scope trees. This is what nvcc's `--generate-line-info` flag triggers -- enough for profiler source correlation but not enough for stepping through code in cuda-gdb.

The core debug info stripping implementation lives at `0xAE0000` (Zone 3 of the type system module), which calls `stripDebugInfo()` to remove all `llvm.dbg.*` intrinsics from the module.

## Debug Compilation Modes

cicc supports three debug info levels, controlled by CLI flags that route through the flag dispatch table:

| CLI flag | Flag offset | Backend routing | Debug level |
|---|---|---|---|
| `-g` | `+296` | `-debug-compile` to both linker and optimizer | Full debug info (FullDebug emission kind) |
| `-generate-line-info` | `+328` | `-generate-line-info` to optimizer | Line tables only (LineTablesOnly emission kind) |
| (neither) | -- | -- | No debug info (NoDebug) |

When `-g` is active, cicc emits `DICompileUnit` with full emission kind, preserves all `DISubprogram`, `DILocalVariable`, `DIType`, and scope metadata through the pipeline, and the backend emits complete DWARF sections. The verifier runs at full depth.

When `-generate-line-info` is active, the `StripNonLineTableDebugInfoPass` runs early in the pipeline, leaving only line table metadata. The verifier still runs but only checks `DILocation` / `DISubprogram` consistency (variable checks are skipped because the variable metadata was intentionally stripped).

## Knobs and Configuration

| Knob | Type | Default | Effect |
|---|---|---|---|
| `-g` / `-debug-compile` | bool | off | Full debug compilation |
| `-generate-line-info` | bool | off | Line tables only |
| `-no-lineinfo-inlined-at` | bool | off | Disable inlined-at tracking (sets `-line-info-inlined-at=0`) |
| `-show-src` / `-nvptx-emit-src` | bool | off | Interleave source in PTX comments |
| `dwarf-extended-loc` | enum | Default | `Default`/`Enable`/`Disable` extended `.loc` flags |
| `dwarf-version` | unsigned | (platform) | DWARF version for debug sections |
| `debugify-each` | bool | off | Run Debugify+CheckDebugify around every pass |
| `debugify-level` | enum | location+variables | `locations` or `location+variables` |
| `debugify-quiet` | bool | off | Suppress debugify diagnostics |
| `debugify-func-limit` | int | unlimited | Max functions to debugify |
| `debugify-export` | string | -- | Export debugify results to file |
| `verify-each` | bool | off | Run IR verifier after every pass |
| `verify-debuginfo-preserve` | bool | off | Enable debug info preservation checking |
| `no-inline-line-tables` | bool | off | Prevent inlining from merging line tables |
| `qword_5008FC8` | bool | off | Verbose diagnostic output enable |
| `qword_5008C88` | int32 | >0 | Metadata depth threshold (<=0 skips deep scope walk) |

## DWARF Emission Backend

The actual DWARF section emission lives in a separate module at `0x3990000`--`0x39DF000`:

| Address | Size | Function |
|---|---|---|
| `sub_399B1E0` | 29KB | `DwarfDebug::beginModule()` -- initializes from `llvm.dbg.cu` |
| `sub_3997B50` | 33KB | `.debug_aranges` emission |
| `sub_399D1D0` | 12KB | Range list emission (`DW_RLE_*`) |
| `sub_399EB70` | 12KB | Register location expressions |
| `sub_39BDF60` | 38KB | `.debug_names` accelerator table |
| `sub_39B6390` | 33KB | DWARF form size calculator |
| `sub_215ACD0` | 8.1KB | Module-level emission entry (NVPTX Debug Info Emission) |

The module-level entry `sub_215ACD0` checks `*(a1+240)->field_344` to determine if DWARF is enabled, then looks up the `"NVPTX DWARF Debug Writer"` / `"NVPTX Debug Info Emission"` pass info. The NVPTX backend does not emit physical register locations (GPUs have no DWARF register numbering scheme that maps to hardware); instead, it emits virtual register references that cuda-gdb resolves through ptxas's SASS-level debug info.

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_29C8000` | 12,480B | Debug info verification pass (main entry) |
| `sub_29C3AB0` | 5,592B | Per-instruction `DILocation` verifier |
| `sub_29C1CB0` | -- | Debugify synthetic debug info injector |
| `sub_29C0AE0` | -- | `errs()` diagnostic output stream accessor |
| `sub_29C0F30` | -- | Copy retained-nodes list (deep copy) |
| `sub_29C1060` | -- | Copy local-variable list |
| `sub_29C1190` | -- | Copy scope-chain list |
| `sub_29C12C0` | -- | Validate scope chain connectivity |
| `sub_29C1F00` | -- | Merge/update tracking sets after verification |
| `sub_29C20D0` | -- | Serialize verification result to stream |
| `sub_29C2230` | -- | Copy imported-entities list (32-byte node deep copy) |
| `sub_29C5270` | -- | `DenseMap::FindAndConstruct` for tracking map |
| `sub_29C6AD0` | -- | Set insert with metadata key normalization |
| `sub_29C6DE0` | -- | Set insert variant (different key extraction) |
| `sub_29E2B40` | -- | `no-inline-line-tables` flag handler |
| `sub_22702B0` | -- | `NewPMCheckDebugifyPass` wrapper |
| `sub_2270390` | -- | `NewPMDebugifyPass` wrapper |
| `sub_12C6910` | -- | Flag filter (checks `-debug-compile`, `-g`, `-generate-line-info`) |
| `sub_31D55F0` | -- | Emit per-instruction `.loc` DWARF directive |
| `sub_31E4280` | -- | Emit `.file`/`.loc` directives (function scope) |
| `sub_31E6100` | -- | `insertDebugLocEntry` (file/line to symbol mapping) |
| `sub_399B1E0` | 29KB | `DwarfDebug::beginModule()` |
| `sub_3997B50` | 33KB | `.debug_aranges` emission |
| `sub_215ACD0` | 8.1KB | Module-level emission entry / NVPTX Debug Info Emission |

## NVIDIA Modifications vs Stock LLVM

The key differences from upstream LLVM's `CheckDebugInfoPass`:

1. **JSON structured output** -- Upstream only prints text diagnostics. NVIDIA added a YAML/JSON serializer (`sub_2241E40`, `sub_CB7060`) that produces machine-parseable bug reports with `"file"`, `"pass"`, `"bugs"` fields and per-bug `"action"` classification (`"drop"` vs `"not-generate"`).

2. **Verbosity control** -- Two global flags (`qword_5008FC8` for output enable, `qword_5008C88` for depth threshold) allow fine-grained control over verification overhead. Upstream has only the `debugify-quiet` knob.

3. **Eight-table metadata tracking** -- Upstream `CheckDebugInfoPass` tracks DISubprograms and debug variable intrinsics. NVIDIA's version maintains eight separate hash tables covering subprograms, scopes, global variables, local variables, types, imported entities, labels, and retained nodes -- a much more comprehensive snapshot.

4. **Metadata reconstruction** -- After verification, NVIDIA's pass reconstructs the module's metadata tables from the verified versions (Phase 8), which upstream does not do. This means the verifier can also serve as a "repair" pass that normalizes metadata after an optimization pass corrupts it.

5. **No kernel-specific handling** -- The verifier treats `__global__` and `__device__` functions identically. CUDA-specific debug info (address space annotations, shared memory debug, warp-level location info) is validated elsewhere, likely during NVPTX backend emission.

## Cross-References

- [AsmPrinter & PTX Body Emission](./asmprinter.md) -- `.loc`/`.file` directive emission, per-instruction debug annotation
- [PTX Emission](../pipeline/emission.md) -- module-level emission entry, DWARF debug writer lookup
- [CLI Flags](../config/cli-flags.md) -- `-g`, `-generate-line-info`, `-show-src` flag routing
- [LLVM Knobs](../config/knobs.md) -- `debugify-*`, `verify-each`, `dwarf-*` knobs
- [Pipeline & Ordering](../llvm/pipeline.md) -- where debug verification fits in the pass pipeline
