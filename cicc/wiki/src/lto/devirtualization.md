# Whole-Program Devirtualization

CICC v13.0 includes LLVM's `WholeProgramDevirtPass` at `sub_2703170` (13,077 bytes), which replaces indirect virtual calls with direct calls using whole-program type information. On GPU this optimization is far more consequential than on CPU: an indirect call in PTX compiles to a `call.uni` through a register, which prevents the backend from inlining the callee, forces all live registers across the call boundary into local memory spills, destroys instruction scheduling freedom, and creates a warp-divergence hazard if threads in the same warp resolve the function pointer to different targets. A single devirtualized call site in a hot kernel loop can therefore improve performance by an order of magnitude -- the direct call enables inlining by the [inliner cost model](./inliner-cost.md), which in turn eliminates `.param`-space marshaling, enables cross-boundary register allocation, and restores the instruction scheduler's ability to interleave memory and arithmetic operations.

CICC's devirtualization operates in a privileged position: GPU compilation is inherently a closed-world model. Every function that can be called on the device must be visible at link time -- there is no dynamic loading, no shared libraries, and no `dlopen` on GPU. This means the set of possible implementations for any virtual function is fully known, making single-implementation devirtualization almost always profitable and branch funnels rare. The pass runs as a module-level pass (pipeline parser slot 121, registered as `"wholeprogramdevirt"`) during the LTO phase, after the [NVModuleSummary builder](./module-summary.md) has computed type test metadata and before GlobalDCE eliminates dead virtual methods.

| | |
|---|---|
| **Entry point** | `sub_2703170` (`0x2703170`, 13,077 bytes) |
| **Address range** | `0x2703170`--`0x2706485` |
| **Stack frame** | 856 bytes (`0x358`) |
| **Pass name** | `"wholeprogramdevirt"` (pipeline slot 121) |
| **Pass type** | Module pass |
| **Callee-saved** | `r15`, `r14`, `r13`, `r12`, `rbx` |
| **Return value** | 1 = module modified, 0 = no changes |
| **Remark category** | `"wholeprogramdevirt"` / `"Devirtualized"` |

## The Closed-World GPU Advantage

Upstream LLVM's WholeProgramDevirt is designed primarily for LTO pipelines where some modules may not be visible (ThinLTO import/export split, shared libraries with hidden visibility). The pass must therefore be conservative: it can only devirtualize when `!type` metadata proves that the vtable set is complete. On GPU, this conservatism is unnecessary. All device code is statically linked into a single fatbinary -- there are no device-side shared libraries, no runtime code loading (the driver JIT compiles PTX, but does not add new device functions), and `__device__` virtual functions cannot escape to host code. The entire class hierarchy is visible.

CICC exploits this by running WPD in regular LTO mode (not ThinLTO export/import split), where the pass directly resolves virtual calls against the merged module. The `NVModuleSummary` builder records `type_test` metadata for all device vtables, and the pass consumes this metadata to build a complete picture of every virtual call site and every possible target. In practice, GPU programs rarely have deep polymorphic hierarchies in device code (the hardware penalties discourage it), so most virtual call sites resolve to a single implementation.

## Algorithm

The pass executes in seven phases:

### Phase 1: Metadata Extraction

The entry point fetches four named metadata nodes from the module using `sub_B6AC80` (getNamedMetadata):

| Enum ID | Metadata Node | Purpose |
|---------|---------------|---------|
| `0x166` (358) | `llvm.type.test` / type_test_assume | Records of `@llvm.assume(@llvm.type.test(%ptr, %typeID))` intrinsic results |
| `0x164` (356) | `llvm.type.checked.load` | Call sites using type-checked vtable loads |
| `0x165` (357) | `llvm.type.checked.load.relative` | Relative vtable pointer variant (compact vtables) |
| `0x0B` (11) | Module-level type metadata | Type summaries describing vtable layouts |

If neither type_test_assume nor module-level type metadata are present, the pass checks for type_checked_load and type_checked_load_relative as fallbacks. If none exist, the pass returns 0 immediately.

### Phase 2: Type Test Record Iteration

Type test records are stored in an array at offset `+0xA0` of the metadata state, with count at `+0xA8`. Each record is 144 bytes (`0x90`):

```c
struct TypeTestRecord {       // 0x90 = 144 bytes per record
    uint8_t *type_value;      // +0x00: pointer to type test value
    // ... call site references, metadata links ...
};

// Iteration pattern:
TypeTestRecord *base = state->records;          // [state + 0xA0]
uint32_t count = state->record_count;           // [state + 0xA8]
TypeTestRecord *end = base + count;             // stride = 0x90
for (TypeTestRecord *rec = base; rec != end; rec++) {
    if (rec->type_value[0] != 0) continue;      // skip already-processed
    // ... look up type in hierarchy ...
}
```

For each record whose type byte is 0 (unprocessed), the pass computes a string hash of the type name via `sub_B91420` (get type name) and `sub_B2F650` (string hash), then looks up the type in a red-black tree rooted at offset `+0xE0` of the module state.

### Phase 3: Hash Table Construction

Unique type test values are tracked in an open-addressed hash table with 56-byte entries. The hash function combines bit-shifted fields to reduce clustering:

```c
uint32_t hash(uint32_t val, uint32_t mask) {
    return ((val >> 4) ^ (val >> 9)) & mask;
}
```

The table uses power-of-2 sizing with two sentinel values for slot states:

| Sentinel | Value | Meaning |
|----------|-------|---------|
| Empty | `0xFFFFFFFFE000` | Slot never occupied |
| Deleted | `0xFFFFFFFFF000` | Slot was occupied then erased |

Each 56-byte hash table entry stores:

| Offset | Size | Field |
|--------|------|-------|
| `+0x00` | 8 | Type test value (key) |
| `+0x08` | 8 | Flags / padding |
| `+0x10` | 8 | Type info pointer |
| `+0x18` | 8 | Associated data (resolution result) |
| `+0x20` | 8 | Red-black tree node (self-referential on init) |
| `+0x28` | 8 | Link pointer |
| `+0x30` | 8 | Count / size |

Table growth is handled by `sub_2702540`, which reallocates and rehashes all entries using the same `(val >> 4) ^ (val >> 9)` function against the new mask.

### Phase 4: Type Hierarchy Lookup

For each unique type, the pass searches a red-black tree keyed by hashed type name. The search is a standard two-phase process:

1. **Hash comparison**: descend left/right comparing the target hash against `node[+0x20]`.
2. **Full verification**: on hash match, compare string lengths (`node[+0x30]` vs target length), then call `memcmp` on the actual type name strings (`node[+0x28]` vs target string).

After finding the type node, the pass reads the vtable data at `node[+0x68]` (vtable start) and `node[+0x70]` (vtable data pointer). If the vtable pointer is null, the type is skipped.

### Phase 5: Virtual Call Resolution

For each call site on a matched type, the pass calls `sub_26FEE10` (resolveVirtualCall):

```c
bool resolveVirtualCall(
    void *module_state,         // rdi: r15
    void *target_candidates,    // rsi: candidates vector
    void *hash_entry,           // rdx: r12
    uint32_t candidate_count,   // rcx: from [rbp-0x228]
    void *call_site_info        // r8:  r13
);
// Returns: al = 1 if unique resolution found, 0 otherwise
```

The resolution result is written to `hash_entry[+0x28]` as a strategy selector:

| Value | Strategy |
|-------|----------|
| 1 | Direct call (single implementation) |
| 2 | Unique member dispatch |
| 3 | Branch funnel |

### Phase 6: Strategy Application

#### Strategy 1 -- Direct Call Replacement

When only one class implements the virtual function (the common case on GPU), the indirect call is replaced with a direct call to the resolved function. This is handled by `sub_26F9AB0` (rewriteCallToDirectCall):

```c
// At 0x27044DA
void rewriteCallToDirectCall(
    void *type_entry,           // rdi: r12
    void *call_site,            // rsi: [r15+0x38]
    uint64_t vtable_data,       // rdx: byte_3F871B3 (vtable offset data)
    uint32_t flags,             // ecx: 0
    void *resolved_function     // r8:  [rbx+0x40]
);
```

This is the simplest and most common optimization: the `call.reg` becomes `call.direct`, enabling downstream inlining. Upstream LLVM calls this "single implementation devirtualization" and tracks it with `NumSingleImpl`.

#### Strategy 2 -- Unique Member Dispatch

When multiple classes exist but the call can be dispatched through a unique member offset, the pass rewrites via `sub_26F9080` (rewriteToUniqueMember), passing the diagnostic string `"unique_member"` (13 chars). The member offset is read from `hash_entry[+0x60]` and the base type from `hash_entry[+0x00]`.

After the initial rewrite, `sub_26FAF90` performs call-site-specific fixup, checking `[rbx+0x40]` to determine if additional adjustment is needed (e.g., adjusting `this` pointer offset for multiple inheritance).

Upstream LLVM's equivalent covers two sub-strategies: **uniform return value optimization** (all implementations return the same constant -- replace the call with that constant) and **unique return value optimization** (for `i1` returns, compare the vptr against the one vtable that returns a different value). Both are folded under the `"unique_member"` label in CICC's implementation.

#### Strategy 3 -- Branch Funnel

When multiple possible targets exist and cannot be reduced to a single dispatch, the pass creates a branch funnel -- a compact conditional dispatch sequence that checks the vtable pointer and branches to the correct target. This is handled by three functions:

1. `sub_26F78E0` -- create branch funnel metadata (with diagnostic string `"branch_funnel"`, 13 chars)
2. `sub_BCF480` -- build the conditional dispatch structure
3. `sub_BA8C10` -- emit the indirect branch sequence

The branch funnel supports two dispatch granularities:

| Granularity | String | Function | Description |
|-------------|--------|----------|-------------|
| Byte | `"byte"` (4 chars) | `sub_26F9120` | Check byte offset into vtable to select target |
| Bit | `"bit"` (3 chars) | `sub_26F9120` | Check bit offset for single-bit discrimination |

The finalization call `sub_26FB610` receives both byte and bit results and produces the final dispatch sequence. On GPU, branch funnels are rare because device code hierarchies are typically shallow, but the infrastructure exists for cases like thrust/CUB polymorphic iterators.

Upstream LLVM gates branch funnels behind the `wholeprogramdevirt-branch-funnel-threshold` knob (default: 10 targets per call site). CICC inherits this threshold.

### Phase 7: Cleanup

After processing all types, the pass performs three cleanup operations:

1. **Function attribute cleanup**: iterates the module's function list, calling `sub_B98000` with parameter `0x1C` (attribute cleanup enum) on each function.
2. **Import list cleanup**: processes entries at `module[+0x110..+0x118]`, calling `sub_B43D60` to release function metadata for imported declarations.
3. **Type hierarchy destruction**: `sub_26F92C0` releases all type hierarchy data structures.
4. **Hash table deallocation**: iterates all non-sentinel entries, calls `sub_26F75B0` to release per-entry resolution data, then `sub_C7D6A0` to free the table buffer. Type test result vectors (0x70-byte elements with sub-vectors at offsets `+0x10`, `+0x28`, `+0x40`, `+0x58`) are freed element by element.

## GPU-Specific Constraints

### Virtual Functions in Device Code

CUDA allows `__device__` virtual functions, but with restrictions that simplify devirtualization:

- **No RTTI on device.** There is no `typeid` or `dynamic_cast` on GPU. This means vtable layouts do not contain RTTI pointers, simplifying vtable reconstruction.
- **No exceptions on device.** Virtual destructors do not need to handle `__cxa_throw` unwinding paths.
- **Closed world.** No device-side shared libraries, no `dlopen`, no runtime code generation. All virtual targets are known at compile time.
- **No separate compilation for virtual dispatch.** Device linking (nvlink) resolves all symbols before PTX emission, so the merged module always has complete type information.

### Cost of Unresolved Indirect Calls

If devirtualization fails, the PTX backend must emit a `call.uni` or `call` through a register. This has several penalties:

1. **No inlining.** The callee is unknown, so the [inliner](./inliner-cost.md) cannot evaluate it.
2. **Full `.param` marshaling.** Every argument must be written to `.param` space; no copy elision is possible.
3. **Register pressure spike.** All live registers across the call must be spilled to local memory (device DRAM, ~400 cycle latency).
4. **Scheduling barrier.** The call is a full fence for instruction scheduling -- no operations can be reordered across it.
5. **Divergence hazard.** If different threads in a warp resolve the pointer to different functions, execution serializes into multiple passes.

This is why CICC's default inlining budget of 20,000 (89x the upstream LLVM default) makes sense in combination with aggressive devirtualization: the pass converts expensive indirect calls into direct calls, and the inliner then eliminates them entirely.

## Optimization Remarks

When a call site is successfully devirtualized, the pass emits an optimization remark through the diagnostic handler. The remark is constructed at `0x2703EDA` using three components:

| Component | String | Address |
|-----------|--------|---------|
| Remark name | `"Devirtualized"` (13 chars) | `0x42BCBEe` |
| Pass name | `"wholeprogramdevirt"` (18 chars) | `0x42BC950` |
| Body prefix | `"devirtualized "` (14 chars) | `0x42BCBE2` |
| Attribute key | `"FunctionName"` (12 chars) | `0x42BC980` |

The remark is visible via `-Rpass=wholeprogramdevirt` and includes the name of the resolved target function (obtained from the function's name metadata or via `sub_26F69E0` for unnamed functions).

## Knobs

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `wholeprogramdevirt-branch-funnel-threshold` | unsigned | 10 | Maximum number of call targets per call site for branch funnel emission. Beyond this threshold, the call site is left indirect. |
| `whole-program-visibility` | bool | false | Force enable whole-program visibility even without `!vcall_visibility` metadata. On GPU this is effectively always true. |
| `disable-whole-program-visibility` | bool | false | Force disable whole-program visibility for debugging. |
| `wholeprogramdevirt-summary-action` | enum | none | Controls summary interaction: `none`, `import`, `export`. CICC uses `none` (direct resolution on merged module). |
| `wholeprogramdevirt-read-summary` | string | empty | Read type resolutions from a bitcode/YAML file. |
| `wholeprogramdevirt-write-summary` | string | empty | Write type resolutions to a bitcode/YAML file. |
| `wholeprogramdevirt-skip` | string list | empty | Comma-separated list of function names to exclude from devirtualization. |
| `wholeprogramdevirt-check` | enum | none | Runtime checking mode: `none`, `trap` (abort on incorrect devirt), `fallback` (fall back to indirect call). |
| `wholeprogramdevirt-keep-unreachable-function` | bool | true | Keep unreachable functions as possible devirt targets (conservative default). |
| `wholeprogramdevirt-print-index-based` | bool | false | Print index-based devirtualization messages for debugging. |
| `devirtualize-speculatively` | bool | false | Enable speculative devirtualization without whole-program visibility. Not useful on GPU (full visibility is always available). |

## Complexity

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| Hash table insert/lookup | O(1) amortized, O(n) worst case | Linear probing with sentinel-based open addressing |
| Type hierarchy lookup | O(log n) | Red-black tree keyed by type name hash |
| Per-type call resolution | O(call_sites * candidates) | For each type, check every call site against every candidate target |
| Branch funnel emission | O(vtable_entries) per site | Linear in number of possible targets |
| Total pass | O(T * S * C * log T) | T = types, S = call sites per type, C = candidates. Typically sparse. |

## Function Map

| Address | Identity | Role |
|---------|----------|------|
| `sub_2703170` | `WholeProgramDevirtPass::run` | Pass entry point (13,077 bytes) |
| `sub_2702830` | `buildTypeTestInfo` | Build type test records from metadata |
| `sub_2702540` | `growHashTable` | Grow and rehash the type test hash table |
| `sub_26FEE10` | `resolveVirtualCall` | Attempt single-target resolution for a call site |
| `sub_26F9AB0` | `rewriteCallToDirectCall` | Strategy 1: replace indirect call with direct call |
| `sub_26F9080` | `rewriteToUniqueMember` | Strategy 2: unique member dispatch rewrite |
| `sub_26FAF90` | `finalizeUniqueMember` | Strategy 2: call-site-specific fixup |
| `sub_26F78E0` | `createBranchFunnelMeta` | Strategy 3: create branch funnel metadata |
| `sub_BCF480` | `buildBranchFunnel` | Strategy 3: build conditional dispatch structure |
| `sub_BA8C10` | `emitIndirectBranch` | Strategy 3: emit indirect branch sequence |
| `sub_26F9120` | `emitDispatchCheck` | Branch funnel byte/bit offset check |
| `sub_26FB610` | `finalizeBranchFunnel` | Branch funnel finalization |
| `sub_26F92C0` | `destroyTypeHierarchy` | Release type hierarchy data structures |
| `sub_26F75B0` | `releaseResolutionData` | Free per-entry resolution data |
| `sub_26F69E0` | `attachFunctionName` | Attach function name to optimization remark |
| `sub_B6AC80` | `getNamedMetadata` | Fetch named metadata node from module |
| `sub_B91420` | `getTypeInfoName` | Compute type info name string |
| `sub_B2F650` | `stringHash` | Hash a type name string |
| `sub_B17560` | `createRemarkHeader` | Create optimization remark header |
| `sub_B18290` | `appendRemarkBody` | Append body text to remark |
| `sub_B16430` | `createNamedAttribute` | Create named attribute for remark |
| `sub_1049740` | `publishRemark` | Publish remark to diagnostic handler |

## Cross-References

- **[NVModuleSummary Builder](./module-summary.md)** -- produces the `type_test` metadata consumed by this pass; records devirtualization-relevant type GUIDs in per-function summaries.
- **[Inliner Cost Model](./inliner-cost.md)** -- devirtualized direct calls become inlining candidates with a 20,000-unit budget; the entire value of devirtualization on GPU depends on the inliner subsequently eliminating the call.
- **[ThinLTO Function Import](./thinlto-import.md)** -- in ThinLTO mode the pass would operate in export/import phases, but CICC primarily uses regular LTO for device code.
- **[Pipeline & Ordering](../llvm/pipeline.md)** -- WPD is registered at pipeline parser slot 121 as a module pass; it runs during the LTO phase after summary construction and before GlobalDCE.
- **[NVPTX Call ABI](../pipeline/irgen-functions.md)** -- describes the `.param`-space calling convention that makes indirect calls so expensive (opcodes 510-513: CallDirect, CallDirectNoProto, CallIndirect, CallIndirectNoProto).
- **[LazyCallGraph & CGSCC](../infra/lazycallgraph.md)** -- devirtualization converts ref edges to call edges in the call graph, triggering SCC re-computation via `switchInternalEdgeToCall`.
