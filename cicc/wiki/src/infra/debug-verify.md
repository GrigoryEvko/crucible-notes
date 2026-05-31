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
| **Stack frame** | `0x4B8` bytes (eight tracking structures) |
| **Upstream origin** | `llvm/lib/Transforms/Utils/Debugify.cpp` -- `CheckDebugInfoPass` |

## Three Verification Modes

cicc supports three independent verification protocols, each activated by a different set of knobs. Understanding which protocol is active determines what diagnostic output to expect and how much overhead the verification adds.

### Mode 1: Post-Pass Debug Info Verification (`verify-each`)

The default verification mode, activated by the `verify-each` (or its alias `verify-after-all`) LLVM knob. The pipeline runner invokes `sub_29C8000` as a sandwich around each optimization pass:

```c
// Pseudocode for the pipeline runner's verification protocol
// (entry: 0x29C8000, stack: 0x4B8 bytes)
snapshot_debug_metadata(M);
run_optimization_pass(M, "instcombine");
sub_29C8000(M, errs(), dbgCU, hashMap, "instcombine", 11, file, fileLen, jsonOut);
```

The pass name argument identifies which optimization just ran, so the JSON report can attribute any debug info degradation to the specific pass responsible. The verifier checks the full metadata inventory: subprograms, scopes, variables, types, labels, imported entities, and retained nodes. It produces `ERROR` diagnostics for dropped subprograms and `WARNING` diagnostics for dropped debug variable intrinsics.

**Activation:** `-Xcicc -verify-each` or `-Xcicc -verify-after-all`
**Overhead:** One full metadata snapshot + eight hash table constructions + per-function variable scan per optimization pass. Substantial for large modules.

### Mode 2: Debugify Synthetic Injection + Verification (`debugify-each`)

The full Debugify cycle injects synthetic debug metadata before each pass, runs the pass, then verifies the synthetic metadata survived. This mode is more aggressive than Mode 1 because it tests every pass even on code compiled without `-g`.

```c
// Debugify cycle pseudocode
sub_29C1CB0(M, "llvm.debugify");   // inject synthetic debug info
run_optimization_pass(M, "instcombine");
sub_29C8000(M, errs(), dbgCU, hashMap, "instcombine", ...);  // verify
strip_debugify_metadata(M, "llvm.debugify");  // cleanup
```

The injector (`sub_29C1CB0`) creates `"llvm.debugify"` / `"llvm.mir.debugify"` named metadata nodes that serve as watermarks. The checker looks for these watermarks to distinguish synthetic from genuine debug info.

**Activation:** `-Xcicc -debugify-each`
**Sub-knobs:** `debugify-level` (`locations` or `location+variables`), `debugify-quiet`, `debugify-func-limit`, `debugify-export`

### Mode 3: Debug Info Preservation Checking (`verify-debuginfo-preserve`)

A lighter-weight mode that checks only whether existing debug info survives optimization, without injecting synthetic metadata. This mode is available through the New Pass Manager infrastructure and can export results via `verify-di-preserve-export`.

**Activation:** `-Xcicc -verify-debuginfo-preserve`
**Sub-knobs:** `verify-each-debuginfo-preserve`, `verify-di-preserve-export`

### Mode Selection Matrix

| Knob | Scope | Injects synthetic? | Checks variables? | JSON output? |
|---|---|---|---|---|
| `verify-each` | All passes | No | Yes (if `-g`) | If jsonOutput != NULL |
| `debugify-each` | All passes | Yes | Configurable via `debugify-level` | Via `debugify-export` |
| `verify-debuginfo-preserve` | All passes | No | Yes | Via `verify-di-preserve-export` |
| (none, `-g` active) | -- | No | No per-pass check | No |

## Pipeline Integration

The verifier operates as an interleaved "check" pass. The New Pass Manager registers it via two wrappers in the pipeline construction code at `0x2270000`--`0x227FFFF`:

| Address | Registration string | Role |
|---|---|---|
| `sub_22702B0` | `"NewPMCheckDebugifyPass]"` | Verification after each pass |
| `sub_2270390` | `"NewPMDebugifyPass]"` | Synthetic injection before each pass |
| `sub_2270470` | `"VerifierPass]"` | Standard IR verifier (separate) |

The pipeline text parser (`sub_2272BE0`, 14KB) recognizes these as named module passes:

| Slot | Pipeline name | Class | Level |
|---|---|---|---|
| #26 | `"check-debugify"` | `NewPMCheckDebugifyPass` | Module |
| #35 | `"debugify"` | `NewPMDebugifyPass` | Module |

When `debugify-each` is active, the pipeline builder (`sub_2277440`, 60KB -- `buildDefaultPipeline()` equivalent) wraps every optimization pass in a debugify/check-debugify pair. When `verify-each` is active, only the check-debugify wrapper is inserted.

## Verification Function Signature

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

### Phase 1: Module-Level Guard (`0x29C8000` -- `0x29C807A`)

Looks up the `"llvm.dbg.cu"` named metadata node via `sub_BA8DC0` (`Module::getNamedMetadata`). If absent or empty, prints `": Skipping module without debug info\n"` and returns 0. This is the fast path for modules compiled without `-g`.

### Phase 2: Pre-Pass Metadata Snapshot (`0x29C8080` -- `0x29C8AE5`)

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

This is the standard DenseMap pointer hash with LLVM-layer sentinels. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the complete specification.

### Phase 3: DISubprogram Iteration (`0x29C82BE` -- `0x29C84C8`)

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

### Phase 4: Hash Table Construction (`0x29C8508` -- `0x29C8AC2`)

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

The MDNode operand access pattern used during population:

```c
// MDNode internal layout decoding (0x29C8508+)
byte flags = *(ptr - 0x10);
if (flags & 0x02) {          // distinct metadata
    operands = *(ptr - 0x20);  // operand array is before the node
} else {
    int count = (flags >> 2) & 0x0F;
    operands = ptr - 0x10 - (count * 8);  // inline operands
}
```

### Phase 5: Per-Function Debug Variable Checking (`0x29C8B3B` -- `0x29C9060`)

Iterates every function in the module. For each, looks up its `DISubprogram` in the hash table and cross-references `dbg.value()` / `dbg.declare()` intrinsics against the pre-snapshot. Two diagnostic levels:

**ERROR** (pass dropped a subprogram entirely):
```text
ERROR: <pass> dropped DISubprogram of <function> from <file>
ERROR: <pass> did not generate DISubprogram for <function> from <file>
```

**WARNING** (pass dropped individual variable tracking):
```text
WARNING: <pass> drops dbg.value()/dbg.declare() for <var> from function <func> (file <file>)
```

The distinction between "dropped" and "did not generate" is significant: "dropped" means metadata existed before the pass and was deleted; "not-generate" means the pass created new IR (e.g., from inlining or outlining) without attaching corresponding debug metadata. This taxonomy is important for GPU compilation because kernel outlining and device function inlining frequently create new IR nodes.

The variable name is resolved by:
1. Getting DISubprogram from the metadata ref
2. Calling `sub_AF34D0` (`DIScope::getScope()`) to walk the scope chain upward
3. Getting the file via operand `[10h]` of the scope's file ref
4. Calling `sub_B91420` (`MDString::getString()`) to convert MDString to StringRef

### Phase 6: Per-Instruction Location Verification (`0x29C8D42` -- `0x29C8D85`)

Delegated to `sub_29C3AB0` (5,592 bytes), which performs detailed checks:

- Every instruction with a `DebugLoc` has a valid `DILocation`
- `DILocation` scope chains resolve to a valid `DISubprogram`
- No orphaned debug locations reference deleted subprograms
- BB-level consistency: all instructions in a basic block share compatible scopes
- Dropped location tracking: emits `"dropped DILocation"` diagnostics

The JSON output from this sub-pass uses structured field names: `"DILocation"`, `"bb-name"`, `"fn-name"`, `"action"` (with values `"drop"` or `"not-generate"`).

### Phase 7: JSON Structured Output (`0x29C90BC` -- `0x29C94E2`)

When a non-null JSON output stream is provided (the `jsonOutput` parameter), the pass serializes a structured report via `sub_2241E40` (YAML/JSON serializer):

```json
{"file":"kernel.cu", "pass":"instcombine", "bugs": [
  {"metadata":"DISubprogram", "name":"_Z6kernelPf", "fn-name":"_Z6kernelPf", "action":"drop"},
  {"metadata":"dbg-var-intrinsic", "name":"idx", "fn-name":"_Z6kernelPf", "action":"not-generate"}
]}
```

This JSON reporting mechanism is an NVIDIA extension with no upstream LLVM equivalent. It feeds into NVIDIA's internal CI infrastructure to track debug info quality regressions across compiler versions. The `"no-name"` string serves as fallback when the pass name pointer is NULL.

The serialization calls `sub_CB7060` (YAML::IO constructor) and proceeds through `sub_C6D380` (object emission), `sub_C6C710` (array emission), and `sub_C6B0E0` (key writer). After serialization, the stream is flushed via `sub_CB7080` and freed via `sub_CB5B00`. If the file descriptor is valid (`fd != -1`), it is closed via `sub_C837B0` (`close(fd)`).

### Phase 8: Result Reporting and Metadata Reconstruction (`0x29C94E2` -- `0x29C9A27`)

Prints the summary line (`"<pass>: PASS\n"` or `"<pass>: FAIL\n"`), then reconstructs the module's metadata tables from the verified versions -- reallocating subprogram, type, variable, label, and global variable arrays and copying verified metadata back into the compile unit structures.

The result is a 3-way outcome in bit flags (combined at `0x29C9073`--`0x29C9080` via AND):
- Bit 0: any verification failure (determines PASS/FAIL)
- Bit 1: JSON report was requested and successfully written

The final result is PASS only if all sub-checks passed AND the JSON report (if requested) was successfully written.

Cleanup frees all eight temporary hash tables (each via `sub_C7D6A0` -- sized dealloc with alignment 8), linked list nodes via `j_j___libc_free_0`, and SmallVector inline buffers are detected by pointer comparison (if `ptr == stack_addr`, skip free).

### Phase 9: Return (`0x29C9A12` -- `0x29C9A27`)

Returns `var_420` (bool) in the `al` register. Standard epilog restores `rbx`, `r12`--`r15`, `rbp`.

## Complete Diagnostic Code Table

Every diagnostic string emitted by the debug verification subsystem, with exact provenance and trigger conditions.

### Verification Pass Diagnostics (`sub_29C8000`)

| # | Severity | Diagnostic string | Trigger condition | Address range |
|---|---|---|---|---|
| D01 | INFO | `": Skipping module without debug info\n"` | `"llvm.dbg.cu"` absent or empty | `0x29C8000`--`0x29C807A` |
| D02 | ERROR | `"ERROR: <pass> dropped DISubprogram of <func> from <file>\n"` | DISubprogram existed pre-pass, absent post-pass | `0x29C8C08`--`0x29C8D2E` |
| D03 | ERROR | `"ERROR: <pass> did not generate DISubprogram for <func> from <file>\n"` | New function has no DISubprogram | `0x29C8C08`--`0x29C8D2E` |
| D04 | WARNING | `"WARNING: <pass> drops dbg.value()/dbg.declare() for <var> from function <func> (file <file>)\n"` | Variable intrinsics lost for a tracked variable | `0x29C8E4E`--`0x29C9060` |
| D05 | SUMMARY | `"<pass>: PASS\n"` | All checks passed | `0x29C94E2`+ |
| D06 | SUMMARY | `"<pass>: FAIL\n"` | Any check failed | `0x29C94E2`+ |
| D07 | ERROR | `"Could not open file: <path>\n"` | JSON report file I/O failure | `0x29C90BC`--`0x29C94E2` |

### Per-Instruction Verifier Diagnostics (`sub_29C3AB0`)

| # | Severity | Diagnostic string | Trigger condition |
|---|---|---|---|
| D08 | ERROR | `"<pass> dropped DILocation"` | Instruction had DILocation pre-pass, absent post-pass |
| D09 | ERROR | `"<pass> did not generate DISubprogram"` | DILocation references nonexistent subprogram |
| D10 | ERROR | (scope chain invalid) | DILocation scope chain does not resolve to a valid DISubprogram |
| D11 | WARNING | (BB inconsistency) | Instructions within a basic block reference incompatible scopes |

### JSON Report Field Schema

| # | Field key | Type | Values | Context |
|---|---|---|---|---|
| J01 | `"file"` | string | Source filename | Top-level report |
| J02 | `"pass"` | string | Pass name, or `"no-name"` if NULL | Top-level report |
| J03 | `"bugs"` | array | Array of bug objects | Top-level report |
| J04 | `"metadata"` | string | `"DISubprogram"`, `"dbg-var-intrinsic"`, `"DILocation"` | Per-bug object |
| J05 | `"name"` | string | Entity name (function or variable) | Per-bug object |
| J06 | `"fn-name"` | string | Containing function name | Per-bug object |
| J07 | `"bb-name"` | string | Basic block name | Per-bug object (location bugs) |
| J08 | `"action"` | string | `"drop"` or `"not-generate"` | Per-bug object |

### Action Value Taxonomy

| Action | Meaning | Common cause in GPU compilation |
|---|---|---|
| `"drop"` | Pass explicitly or inadvertently deleted existing debug metadata | Dead code elimination removing a function with debug info |
| `"not-generate"` | Pass created new IR without attaching corresponding debug metadata | Kernel outlining, device function inlining, or loop transformation creating new BBs |

### String Encoding Details

Several diagnostic strings are constructed inline using immediate `mov` instructions rather than string table references:

| String | Encoding | Instruction |
|---|---|---|
| `"ERRO"` | `0x4F525245` | `mov dword [rsp+X], 0x4F525245` |
| `"R:"` | `0x3A52` | `mov word [rsp+X+4], 0x3A52` |
| `"WARNING:"` | `0x3A474E494E524157` | `mov qword [rsp+X], 0x3A474E494E524157` |

These inline immediate constructions avoid string table lookups and are a common LLVM raw_ostream optimization for short fixed strings.

## Compile Unit Descriptor Layout

The verification pass reads and reconstructs a per-CU descriptor object (referenced at `[rbp+var_440]`) with the following layout:

| Offset | Type | Contents | Copy helper |
|---|---|---|---|
| `+08h` | `void**` | Subprogram array data pointer | -- |
| `+10h` | `void**` | Subprogram array end pointer | -- |
| `+18h` | `size_t` | Subprogram count | -- |
| `+20h` | `void*` | Scope chain data | -- |
| `+28h` | `size_t` | Scope chain count | -- |
| `+38h` | `void**` | Global variable array data | -- |
| `+40h` | `void**` | Global variable array end | -- |
| `+48h` | `size_t` | Global variable count | -- |
| `+50h` | `void*` | Local variable list head | -- |
| `+58h` | `size_t` | Local variable count | -- |
| `+68h` | `void**` | Type array data | -- |
| `+70h` | `void**` | Type array end | -- |
| `+78h` | `size_t` | Type count | -- |
| `+80h` | `void*` | Imported entities list | `sub_29C2230` (32-byte node deep copy) |
| `+88h` | `size_t` | Imported entities count | -- |
| `+98h` | `void**` | Label array data | -- |
| `+A0h` | `void**` | Label array end | -- |
| `+A8h` | `size_t` | Label count | -- |
| `+B0h` | `void*` | Retained nodes list | `sub_29C0F30` |
| `+B8h` | `size_t` | Retained nodes count | -- |

## DISubprogram Node Layout

Accessed during Phase 3 scope chain validation:

| Offset | Type | Contents |
|---|---|---|
| `[node-38h]` | `void*` | Pointer to compile unit / parent scope |
| `[node-18h]` | `byte` | Metadata tag byte (DWARF tag discriminator) |
| `[node-14h]` | `uint32` | Flags field (lower 27 bits = operand index) |
| `[node+08h]` | `void*` | Next pointer in linked list |
| `[node+18h]` | `void*` | Linked list head for child scopes |
| `[node+20h]` | `void*` | Linked list tail for child scopes |
| `[node+28h]` | `void*` | Variable attachment (DIVariable list) |
| `[node+38h]` | `void*` | Additional metadata ref |
| `[node+48h]` | `void*` | Subprogram scope list head |
| `[node+50h]` | `void*` | Subprogram scope list tail |

## Debugify Injector (`sub_29C1CB0`)

The Debugify injector creates synthetic debug metadata to test whether optimization passes preserve debug info correctly. It is the counterpart to the verifier -- the injector sets up the watermarks, and the verifier checks them.

**Named metadata markers:**
- `"llvm.debugify"` -- marks the module as containing synthetic debug info (standard Debugify)
- `"llvm.mir.debugify"` -- marks MIR-level synthetic debug info

**Behavior controlled by `debugify-level`:**
- `locations` -- inject only `DILocation` on every instruction (cheaper, tests location preservation)
- `location+variables` -- inject `DILocation` plus synthetic `dbg.value()`/`dbg.declare()` for every SSA value (full coverage, higher overhead)

The injector assigns monotonically increasing line numbers to every instruction and creates one `DILocalVariable` per SSA value that produces a result. The variable names follow the pattern `"dbg_var_N"` where N is the SSA value index. After injection, the module has guaranteed 100% debug coverage, making any coverage loss attributable to the subsequent optimization pass.

## Verbosity Control

Two global flags provide fine-grained control over verification output:

### `qword_5008FC8` -- Verbose Diagnostic Output Enable

Boolean flag (byte). Controls the output stream selection:
- When `0`: uses `sub_CB72A0` (null/discard stream constructor) -- diagnostics silently discarded
- When non-zero: uses `sub_CB7330` (stderr stream accessor) -- diagnostics printed to stderr

This flag gates the ERROR and WARNING messages. The JSON structured output is controlled separately by the `jsonOutput` parameter. Setting `qword_5008FC8 = 0` suppresses text diagnostics while still producing JSON output.

### `qword_5008C88` -- Metadata Depth Threshold

Signed 32-bit integer, read at `0x29C8371`. Controls how deep the scope chain walk goes:
- When `<= 0`: the deep scope chain walk is skipped for non-subprogram metadata. Only top-level DISubprogram validation runs.
- When `> 0`: full scope chain traversal validates every DILexicalBlock, DILexicalBlockFile, and DINamespace in the hierarchy.

This allows production builds to run lightweight verification (subprogram-only) while development builds run exhaustive scope chain checking.

### Debugify-Specific Knobs

| Knob | Type | Default | Registration | Effect |
|---|---|---|---|---|
| `debugify-quiet` | bool | off | `ctor_493` at `0x556960` | Suppress all debugify text output |
| `debugify-func-limit` | int | unlimited | `ctor_493` at `0x556960` | Max functions to inject synthetic debug info into |
| `debugify-level` | enum | `location+variables` | `ctor_493` at `0x556960` | `locations` or `location+variables` |
| `debugify-function` | string | -- | `ctor_493` at `0x556960` | Restrict debugify to a single named function |
| `check-debugify-function` | string | -- | `ctor_493` at `0x556960` | Restrict check-debugify to a single named function |
| `debugify-each` | bool | off | `ctor_377` at `0x516190` | Wrap every pass in debugify/check-debugify |
| `debugify-export` | string | -- | `ctor_377` at `0x516190` | Export debugify results to file |

## GPU Debug Info: What PTX Needs

DWARF for PTX differs fundamentally from DWARF for x86. PTX is a virtual ISA -- there are no physical registers, no real stack, and no fixed instruction encoding. The debug metadata cicc emits serves two consumers: cuda-gdb (which maps PTX locations back to source) and ptxas (which carries debug info forward into SASS/ELF for the hardware debugger).

### The .loc Directive

The AsmPrinter (`sub_31D55F0`) emits DWARF `.loc` directives before each PTX instruction that has a valid `DebugLoc`:

```ptx
.loc 1 42 0          // file 1, line 42, column 0
ld.param.u64 %rd1, [_Z6kernelPf_param_0];
.loc 1 43 5
mul.wide.u32 %rd2, %r1, 4;
```

The `.file` directives (`sub_31E4280`) establish the file table, and `sub_31E6100` maintains a file/line-to-MCSymbol mapping for line table construction.

The `dwarf-extended-loc` knob (enum: `Default`/`Enable`/`Disable`, registered at `0x490000` range) controls whether extended flags appear in `.loc` directives. When disabled, cicc emits bare `.loc file line column` without the `is_stmt`, `prologue_end`, or `discriminator` extensions. This is relevant because older ptxas versions do not parse extended `.loc` flags.

### The line-info-inlined-at Extension

The `-line-info-inlined-at` LLVM knob (registered at `ctor_043` / `0x48D7F0`, exposed as `-no-lineinfo-inlined-at` in the cicc CLI, which sets `-line-info-inlined-at=0` on the backend) controls whether inlined-at chains are preserved in PTX line info. When enabled (the default), every `.loc` directive for inlined code carries the full inlining chain so cuda-gdb can reconstruct the call stack at any point in the inlined code. When disabled, only the immediate source location is emitted, losing the inlining context but producing smaller PTX.

### The -show-src / nvptx-emit-src Feature

The `-show-src` CLI flag (stored at flag struct offset `+808`, routed to the backend as `-nvptx-emit-src`) enables source line interleaving in PTX output. When active, the AsmPrinter annotates each `.loc` directive with the corresponding source line as a PTX comment:

```ptx
// kernel.cu:42    float val = input[idx];
.loc 1 42 0
ld.global.f32 %f1, [%rd2];
// kernel.cu:43    val = val * val;
.loc 1 43 0
mul.f32 %f2, %f1, %f1;
```

This is purely a readability feature for developers inspecting PTX output. It has no effect on cuda-gdb or debug quality -- the source text is embedded as comments that ptxas ignores.

### NvvmDebugVersion

The NVVM container format includes a debug version field (`NvvmDebugVersion`, packed as `{Major:uint16, Minor:uint16}` at container offset `0x08`--`0x09`). The current version is `Major=3, Minor<=2`. The reader (`sub_CD41B0`) validates that `Major` equals 3 and warns if `Minor` exceeds 2. If absent, the default `{3, 2}` is assumed. This version tracks the debug metadata schema independently of the NVVM IR version, allowing debug format evolution without breaking IR compatibility.

The standalone pipeline (`sub_12BFF60`) performs a consistency check: if the container declares `debug_info_present` (bit 4 of flags) AND the debug mode flag is set AND the debug version has not been validated, it returns error code 3 (incompatible).

### DbgRecord Format (LLVM 20)

cicc v13.0 uses LLVM 20's `DbgRecord` format by default (`write-experimental-debuginfo = true`, registered at `ctor_025`). This replaces traditional `dbg.value()`/`dbg.declare()` intrinsics with non-intrinsic debug records attached directly to instructions. Related knobs:

| Knob | Default | Registration | Effect |
|---|---|---|---|
| `write-experimental-debuginfo` | `true` | `ctor_025` | Use DbgRecord format for new debug info |
| `write-experimental-debuginfo-iterators-to-bitcode` | `true` | `ctor_018` | Serialize DbgRecords to bitcode |
| `preserve-input-debuginfo-format` | `false` | `ctor_018` | When true, preserve whichever format the input uses |

The verifier handles both formats: it checks for `dbg.value()`/`dbg.declare()` intrinsics AND for DbgRecord attachments.

## Debug Info Stripping Passes

cicc includes five stripping passes registered in the pipeline parser (at `sub_12C6910` and related):

| Pipeline name | Slot | LLVM pass | Effect |
|---|---|---|---|
| `"strip-dead-debug-info"` | #110 | `StripDeadDebugInfoPass` | Remove debug info for dead functions/globals |
| `"strip-debug-declare"` | #112 | `StripDebugDeclarePass` | Remove `dbg.declare()` intrinsics only |
| `"strip-nondebug"` | #113 | `StripNonDebugSymbolsPass` | Remove non-debug symbols (keep debug) |
| `"strip-nonlinetable-debuginfo"` | #114 | `StripNonLineTableDebugInfoPass` | Strip everything except line tables |

The `strip-nonlinetable-debuginfo` pass is the key one for the `-generate-line-info` mode: it strips all debug metadata except `.loc` / `.file` directives, producing line-number-only debug info without variable locations, type descriptions, or scope trees. This is what nvcc's `--generate-line-info` flag triggers -- enough for profiler source correlation but not enough for stepping through code in cuda-gdb.

The core debug info stripping implementation lives at `0xAE0000` (Zone 3 of the type system module), which calls `stripDebugInfo()` to remove all `llvm.dbg.*` intrinsics from the module.

## Debug Compilation Modes

cicc supports three debug info levels, controlled by CLI flags that route through the flag dispatch table:

| CLI flag | Flag offset | Backend routing | Debug level |
|---|---|---|---|
| `-g` | `+296` | `-debug-compile` to both linker and optimizer | Full debug info (FullDebug emission kind) |
| `-generate-line-info` | `+328` | `-generate-line-info` to optimizer only | Line tables only (LineTablesOnly emission kind) |
| (neither) | -- | -- | No debug info (NoDebug) |

When `-g` is active, cicc emits `DICompileUnit` with full emission kind, preserves all `DISubprogram`, `DILocalVariable`, `DIType`, and scope metadata through the pipeline, and the backend emits complete DWARF sections. The verifier runs at full depth.

When `-generate-line-info` is active, the `StripNonLineTableDebugInfoPass` runs early in the pipeline, leaving only line table metadata. The verifier still runs but only checks `DILocation` / `DISubprogram` consistency (variable checks are skipped because the variable metadata was intentionally stripped).

**Key routing difference:** `-g` routes to BOTH the linker (`-debug-compile`) and optimizer (`-debug-compile`), because libdevice linking needs the debug flag to preserve user debug info during merging. `-generate-line-info` routes to the optimizer only.

The frontend uses two independent guard mechanisms for debug emission:
- `dword_4D046B4` -- global flag checked at statement/parameter level by `sub_9433F0` (per-param debug), `sub_943430` (per-global debug)
- `[ctx+0x170]` -- compile unit pointer checked at module finalization level by `sub_915400`

The NVVM container carries a dedicated `DebugInfo` enum (3 values: `NONE`, `LINE_INFO`, `DWARF`) at deserialized struct offset `+12`, separate from the module metadata.

## Complete Knob Reference

| Knob | Type | Default | Registration | Effect |
|---|---|---|---|---|
| `-g` / `-debug-compile` | bool | off | `ctor_043` at `0x48D7F0` | Full debug compilation |
| `-generate-line-info` | bool | off | `ctor_043` at `0x48D7F0` | Line tables only |
| `-no-lineinfo-inlined-at` | bool | off | CLI flag dispatch | Disable inlined-at tracking (sets `-line-info-inlined-at=0`) |
| `-show-src` / `-nvptx-emit-src` | bool | off | Flag offset `+808` | Interleave source in PTX comments |
| `dwarf-extended-loc` | enum | Default | `0x490000` range | `Default`/`Enable`/`Disable` extended `.loc` flags |
| `dwarf-version` | unsigned | (platform) | LLVM default | DWARF version for debug sections |
| `debugify-each` | bool | off | `ctor_377` at `0x516190` | Run Debugify+CheckDebugify around every pass |
| `debugify-level` | enum | location+variables | `ctor_493` at `0x556960` | `locations` or `location+variables` |
| `debugify-quiet` | bool | off | `ctor_493` at `0x556960` | Suppress debugify diagnostics |
| `debugify-func-limit` | int | unlimited | `ctor_493` at `0x556960` | Max functions to debugify |
| `debugify-function` | string | -- | `ctor_493` at `0x556960` | Restrict debugify to named function |
| `check-debugify-function` | string | -- | `ctor_493` at `0x556960` | Restrict check-debugify to named function |
| `debugify-export` | string | -- | `ctor_377` at `0x516190` | Export debugify results to file |
| `verify-each` | bool | off | `ctor_043` at `0x48D7F0` | Run IR verifier after every pass |
| `verify-after-all` | alias | -- | `ctor_043` at `0x48D7F0` | Alias for `verify-each` |
| `verify-debuginfo-preserve` | bool | off | `ctor_376` at `0x512DF0` | Enable debug info preservation checking |
| `verify-each-debuginfo-preserve` | bool | off | `ctor_377` at `0x516190` | Per-pass debug info preservation |
| `verify-di-preserve-export` | string | -- | `ctor_377` at `0x516190` | Export preservation results to file |
| `no-inline-line-tables` | bool | off | `sub_29E2B40` | Prevent inlining from merging line tables |
| `write-experimental-debuginfo` | bool | true | `ctor_025` | Use DbgRecord format |
| `preserve-input-debuginfo-format` | bool/default | false | `ctor_018` | Preserve input debug format |
| `qword_5008FC8` | bool | off | -- | Verbose diagnostic output enable |
| `qword_5008C88` | int32 | >0 | -- | Metadata depth threshold (<=0 skips deep scope walk) |
| `CAN_FINALIZE_DEBUG` | env var | -- | `sub_60F290` et al. | Debug finalization control |
| `NVVM_IR_VER_CHK` | env var | enabled | `sub_12BFF60` | Override debug version checking (set "0" to disable) |

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

| Function | Address | Size | Role |
|---|---|---|---|
| `"llvm.global_ctors"` utility | `sub_29C00F0` | -- | -- |
| `errs()` diagnostic output stream accessor | `sub_29C0AE0` | -- | -- |
| PassManager / PassAdaptor infrastructure (`"PassManager"`, `"PassAdaptor"`) | `sub_29C0DC0` | -- | -- |
| Copy retained-nodes list (SmallVector deep copy) | `sub_29C0F30` | -- | -- |
| Copy local-variable list | `sub_29C1060` | -- | -- |
| Copy scope-chain list | `sub_29C1190` | -- | -- |
| Validate scope chain connectivity | `sub_29C12C0` | -- | -- |
| Debugify synthetic debug info injector (`"llvm.debugify"`, `"llvm.mir.debugify"`) | `sub_29C1CB0` | -- | -- |
| Merge/update tracking sets after verification | `sub_29C1F00` | -- | -- |
| Serialize verification result to stream | `sub_29C20D0` | -- | -- |
| Copy imported-entities list (32-byte node deep copy) | `sub_29C2230` | -- | -- |
| Per-instruction `DILocation` verifier | `sub_29C3AB0` | 5,592B | -- |
| `DenseMap::FindAndConstruct` for tracking map | `sub_29C5270` | -- | -- |
| Set insert with metadata key normalization | `sub_29C6AD0` | -- | -- |
| Set insert variant (different key extraction) | `sub_29C6DE0` | -- | -- |
| Debug info verification pass (main entry) | `sub_29C8000` | 12,480B | -- |
| `no-inline-line-tables` flag handler | `sub_29E2B40` | -- | -- |
| `NewPMCheckDebugifyPass` wrapper | `sub_22702B0` | -- | -- |
| `NewPMDebugifyPass` wrapper | `sub_2270390` | -- | -- |
| `VerifierPass` wrapper (standard IR verifier) | `sub_2270470` | -- | -- |
| Pass pipeline text parser | `sub_2272BE0` | 14KB | -- |
| `buildDefaultPipeline()` equivalent | `sub_2277440` | 60KB | -- |
| Flag filter (checks `-debug-compile`, `-g`, `-generate-line-info`) | `sub_12C6910` | -- | -- |
| Emit per-instruction `.loc` DWARF directive | `sub_31D55F0` | -- | -- |
| Emit `.file`/`.loc` directives (function scope) | `sub_31E4280` | -- | -- |
| `insertDebugLocEntry` (file/line to symbol mapping) | `sub_31E6100` | -- | -- |
| `DwarfDebug::beginModule()` | `sub_399B1E0` | 29KB | -- |
| `.debug_aranges` emission | `sub_3997B50` | 33KB | -- |
| Module-level emission entry / NVPTX Debug Info Emission | `sub_215ACD0` | 8.1KB | -- |
| NVVM IR version + debug version validator | `sub_12BFF60` | ~9KB | -- |
| NVVM container debug version check | `sub_CD41B0` | -- | -- |
| Emit `DILocalVariable` for parameter (frontend) | `sub_9433F0` | -- | -- |
| Emit debug info for GlobalVariable (frontend) | `sub_943430` | -- | -- |
| Set `DebugLoc` from EDG source position (frontend) | `sub_941230` | -- | -- |
| Finalize: `"Debug Info Version"` = 3 (frontend) | `sub_915400` | -- | -- |

### LLVM Infrastructure Functions Used

| Address | Identity | Called from |
|---|---|---|
| `sub_BA8DC0` | `Module::getNamedMetadata(StringRef)` | Phase 1 |
| `sub_B2FC80` | `isa<DISubprogram>` or similar MDNode type check | Phase 3 |
| `sub_B2FC00` | MDNode type check (different metadata kind) | Phase 3 |
| `sub_B92180` | `MDNode::getContext()` | Phase 4 |
| `sub_B91420` | `MDString::getString()` | Phase 5 |
| `sub_B91A10` | `MDNode::getOperand(unsigned)` | Phase 4 |
| `sub_B14240` | MDNode operand range iterator | Phase 4 |
| `sub_AF34D0` | `DIScope::getScope()` -- walk scope chain upward | Phase 5 |
| `sub_AF4500` | `DISubprogram::describes(Function)` | Phase 5 |
| `sub_B58DC0` | `DenseSet::insert` | Phase 2 |
| `sub_B96E90` | `DenseMap::insert_or_assign` | Phase 4 |
| `sub_B91220` | `DenseMap::erase` | Phase 8 |
| `sub_C7D670` | `aligned_alloc(size, alignment=8)` | Phase 4 |
| `sub_C7D6A0` | `aligned_free_sized(ptr, size, alignment=8)` | Phase 8 |
| `sub_CB7330` | `errs()` -- get stderr `raw_ostream` | Phase 5 |
| `sub_CB72A0` | `nulls()` -- get null/discard `raw_ostream` | Phase 5 (quiet mode) |
| `sub_CB6200` | `raw_ostream::write(const char*, size_t)` | Phase 5, 7 |
| `sub_CB5D20` | `raw_ostream::write(char)` | Phase 5 |
| `sub_CB5B00` | `raw_ostream` destructor / free | Phase 7 |
| `sub_CB7060` | YAML::IO output constructor | Phase 7 |
| `sub_CB7080` | `raw_ostream::flush()` | Phase 7 |

## NVIDIA Modifications vs Stock LLVM

The key differences from upstream LLVM's `CheckDebugInfoPass`:

1. **JSON structured output** -- Upstream only prints text diagnostics. NVIDIA added a YAML/JSON serializer (`sub_2241E40`, `sub_CB7060`) that produces machine-parseable bug reports with `"file"`, `"pass"`, `"bugs"` fields and per-bug `"action"` classification (`"drop"` vs `"not-generate"`).

2. **Verbosity control** -- Two global flags (`qword_5008FC8` for output enable, `qword_5008C88` for depth threshold) allow fine-grained control over verification overhead. Upstream has only the `debugify-quiet` knob.

3. **Eight-table metadata tracking** -- Upstream `CheckDebugInfoPass` tracks DISubprograms and debug variable intrinsics. NVIDIA's version maintains eight separate hash tables covering subprograms, scopes, global variables, local variables, types, imported entities, labels, and retained nodes -- a much more comprehensive snapshot.

4. **Metadata reconstruction** -- After verification, NVIDIA's pass reconstructs the module's metadata tables from the verified versions (Phase 8), which upstream does not do. This means the verifier can also serve as a "repair" pass that normalizes metadata after an optimization pass corrupts it.

5. **No kernel-specific handling** -- The verifier treats `__global__` and `__device__` functions identically. CUDA-specific debug info (address space annotations, shared memory debug, warp-level location info) is validated elsewhere, likely during NVPTX backend emission.

6. **DbgRecord format support** -- cicc v13.0 defaults to the LLVM 20 DbgRecord format (`write-experimental-debuginfo = true`), so the verifier handles both intrinsic-based and record-based debug info transparently.

## Cross-References

- [AsmPrinter & PTX Body Emission](./asmprinter.md) -- `.loc`/`.file` directive emission, per-instruction debug annotation
- [PTX Emission](../pipeline/emission.md) -- module-level emission entry, DWARF debug writer lookup
- [Debug Info Pipeline](../pipeline/debug-info-pipeline.md) -- end-to-end debug info flow from frontend to backend
- [CLI Flags](../config/cli-flags.md) -- `-g`, `-generate-line-info`, `-show-src` flag routing
- [LLVM Knobs](../config/knobs.md) -- `debugify-*`, `verify-each`, `dwarf-*` knobs
- [Pipeline & Ordering](../llvm/pipeline.md) -- where debug verification fits in the pass pipeline
- [Hash Infrastructure](./hash-infrastructure.md) -- DenseMap/DenseSet implementation used by tracking tables
- [Diagnostics](./diagnostics.md) -- broader diagnostic and remark system
- [NVVM Container](../structs/nvvm-container.md) -- NvvmDebugVersion field
