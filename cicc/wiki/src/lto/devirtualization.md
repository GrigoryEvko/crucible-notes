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
| **Helper range** | `sub_2700B00`--`sub_2708220` (branch funnel helpers, summary I/O) |

## The Closed-World GPU Advantage

Upstream LLVM's WholeProgramDevirt is designed primarily for LTO pipelines where some modules may not be visible (ThinLTO import/export split, shared libraries with hidden visibility). The pass must therefore be conservative: it can only devirtualize when `!type` metadata proves that the vtable set is complete. On GPU, this conservatism is unnecessary. All device code is statically linked into a single fatbinary -- there are no device-side shared libraries, no runtime code loading (the driver JIT compiles PTX, but does not add new device functions), and `__device__` virtual functions cannot escape to host code. The entire class hierarchy is visible.

CICC exploits this by running WPD in regular LTO mode (not ThinLTO export/import split), where the pass directly resolves virtual calls against the merged module. The `NVModuleSummary` builder records `type_test` metadata for all device vtables, and the pass consumes this metadata to build a complete picture of every virtual call site and every possible target. In practice, GPU programs rarely have deep polymorphic hierarchies in device code (the hardware penalties discourage it), so most virtual call sites resolve to a single implementation.

### The Formal Closed-World Argument

The closed-world guarantee on GPU rests on five architectural invariants, each of which eliminates a source of conservatism that forces upstream LLVM to leave calls indirect:

| # | Invariant | What upstream LLVM must worry about | Why GPU is immune |
|---|-----------|-------------------------------------|-------------------|
| 1 | **No device-side shared libraries** | A `.so` loaded at runtime could add a new vtable entry for a class. LTO must mark `!vcall_visibility` metadata `linkage-unit` to prove the vtable set is closed within the link unit. | The CUDA driver loads PTX/SASS as a monolithic blob. `cuModuleLoad` does not support incremental symbol addition. There is no `dl_iterate_phdr` on device. |
| 2 | **No `dlopen` on device** | Host-side `dlopen` can inject new implementations of virtual functions. Upstream must check `!vcall_visibility` for `translation-unit` scope. | Device code has no equivalent of `dlopen`. The only way to add device code is to recompile and reload the entire module. |
| 3 | **No device-side RTTI** | `dynamic_cast` and `typeid` on host can defeat devirtualization by requiring the vtable to contain RTTI pointers that reference external type_info objects. | CUDA explicitly prohibits `dynamic_cast` and `typeid` in `__device__` functions. Device vtables contain no RTTI pointers. The NVVM IR verifier (`sub_12DD660`) rejects code that attempts `dynamic_cast` in device context. |
| 4 | **No exceptions on device** | Virtual destructors in exception-handling code create additional vtable entries and `__cxa_throw` unwinding paths that must be considered. | CUDA does not support exceptions in device code. Virtual destructors are simple (no EH cleanup), and the compiler can see every destructor call site. |
| 5 | **Complete link-time visibility** | ThinLTO's import/export split means some modules may not be available during WPD. The pass must use summary-based resolution with `wholeprogramdevirt-summary-action=import/export`. | CICC uses `wholeprogramdevirt-summary-action=none` (direct resolution on the merged module). All device functions, including those from separate compilation units, are linked by nvlink into a single merged module before the LTO pipeline runs. |

The practical consequence: CICC sets `whole-program-visibility` effectively to true for all device code. The `!vcall_visibility` metadata that upstream uses to distinguish "linkage-unit" from "translation-unit" scope becomes irrelevant -- every device vtable is within a single, complete, closed translation unit.

### How NVModuleSummary Feeds WPD

The `NVModuleSummary` builder at `sub_D7D4E0` (2,571 decompiled lines, 74KB) produces the type metadata that WPD consumes. The interaction is:

1. **NVModuleSummary** walks every `GlobalValue` in the module (linked list at `Module+72`). For each function (opcode `0x3D`), it extracts attribute groups #34 (reference edges with type metadata) and #35 (direct call targets) via `sub_B91C10`.

2. For reference edges with type info (attribute #34), the builder decodes MDNode operands (lines 1193-1228 of the decompilation): each parameter position >= 2 yields a type node (opcode range 5-36), walked to a parent MDTuple (opcode 17) containing the type name string at offset 24 (indirect through pointer if length > 64).

3. These type-metadata edges are packed into the `FunctionSummary` record by `sub_D77220` as the `v378` (type-checked references) argument. The resulting metadata lands in the module as `llvm.type.test` / `type_test_assume` named metadata nodes.

4. WPD reads these nodes back via `sub_B6AC80(module, 0x166)` at its entry point, completing the producer-consumer chain.

### DevirtSCCRepeatedPass: The Outer Loop

WPD at the module level is one of two devirtualization mechanisms. The other operates at CGSCC granularity: `DevirtSCCRepeatedPass` at `sub_2284BC0` (16KB) wraps the CGSCC pipeline in a fixed-point iteration loop that re-runs until no new devirtualization opportunities are discovered or a maximum iteration count is reached. On reaching the limit, the pass emits `"Max devirtualization iterations reached"`. The `abort-on-max-devirt-iterations-reached` knob (registered at constructor 378) controls whether this is a fatal error or a warning. The iteration count at O1-O3 is 1; at tier 3 (maximum optimization) it is 5, giving the inliner and devirtualizer multiple rounds to discover indirect-to-direct call conversions that expose further inlining opportunities.

The two mechanisms are complementary: module-level WPD resolves virtual calls using global type hierarchy information (vtable metadata), while CGSCC-level devirtualization catches cases where inlining reveals new constant function pointers that can be resolved without type metadata.

## Algorithm

The pass executes in seven phases:

### Phase 1: Metadata Extraction (`0x2703170`--`0x27031CA`)

The entry point fetches four named metadata nodes from the module using `sub_B6AC80` (getNamedMetadata):

| Enum ID | Metadata Node | Purpose |
|---------|---------------|---------|
| `0x166` (358) | `llvm.type.test` / type_test_assume | Records of `@llvm.assume(@llvm.type.test(%ptr, %typeID))` intrinsic results |
| `0x164` (356) | `llvm.type.checked.load` | Call sites using type-checked vtable loads |
| `0x165` (357) | `llvm.type.checked.load.relative` | Relative vtable pointer variant (compact vtables) |
| `0x0B` (11) | Module-level type metadata | Type summaries describing vtable layouts |

If neither type_test_assume nor module-level type metadata are present, the pass checks for type_checked_load and type_checked_load_relative as fallbacks. If none exist, the pass returns 0 immediately.

The assembly sequence at the entry point:

```asm
; 0x2703170: entry
mov  esi, 0x166              ; enum ID = 358 (type_test_assume)
call sub_B6AC80              ; rbx = getNamedMetadata(module, 0x166)

mov  esi, 0x164              ; enum ID = 356 (type_checked_load)
call sub_B6AC80              ; r13 = getNamedMetadata(module, 0x164)

mov  esi, 0x165              ; enum ID = 357 (type_checked_load_relative)
call sub_B6AC80              ; [rbp-0x338] = result

mov  esi, 0x0B               ; enum ID = 11 (module-level type metadata)
call sub_B6AC80              ; r12 = result
```

### Phase 2: Type Test Record Iteration (`0x2703296`--`0x2703383`)

Type test records are stored in an array at offset `+0xA0` of the metadata state, with count at `+0xA8`. Each record is 144 bytes (`0x90`):

```c
struct TypeTestRecord {       // 0x90 = 144 bytes per record
    uint8_t *type_value;      // +0x00: pointer to type test value
    // ... call site references, metadata links ...
};

// Iteration pattern at 0x2703296:
TypeTestRecord *base = state->records;          // [state + 0xA0]
uint32_t count = state->record_count;           // [state + 0xA8]
TypeTestRecord *end = base + count;             // stride = 0x90
// Address computation in binary:
//   lea rax, [rax+rax*8]      ; count * 9
//   shl rax, 4                ; count * 144 = count * 0x90
//   add rax, rdx              ; end pointer

for (TypeTestRecord *rec = base; rec != end; rec++) {
    if (rec->type_value[0] != 0) continue;      // skip already-processed
    // ... look up type in hierarchy ...
}
```

For each record whose type byte is 0 (unprocessed), the pass computes a string hash of the type name via `sub_B91420` (get type name) and `sub_B2F650` (string hash), then looks up the type in a red-black tree rooted at offset `+0xE0` of the module state.

### Phase 3: Hash Table Construction (`0x2703589`--`0x2703AE2`)

Unique type test values are tracked in an open-addressed hash table with 56-byte entries. The hash function combines bit-shifted fields to reduce clustering:

```c
uint32_t hash(uint32_t val, uint32_t mask) {
    return ((val >> 4) ^ (val >> 9)) & mask;
}
```

The table uses power-of-2 sizing with LLVM-layer sentinels (empty = `0xFFFFFFFFE000`, deleted = `0xFFFFFFFFF000`). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the probing and growth policy.

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

Slot addressing uses the identity `slot_index * 7 * 8 = slot_index * 56`:

```asm
; At 0x27035A0:
lea  rdx, ds:0[rsi*8]    ; rsi = slot index, rdx = slot*8
sub  rdx, rsi             ; rdx = slot*8 - slot = slot*7
mov  rsi, [rdi+rdx*8]    ; load from table base + slot*56
```

Table growth is handled by `sub_2702540`, which reallocates and rehashes all entries using the same `(val >> 4) ^ (val >> 9)` function against the new mask. Entry initialization at `0x2703A33`:

```asm
; Insert new entry:
add  [rbp-0x2D0], 1      ; increment unique type count
call sub_2702540          ; grow table if needed (returns new entry ptr in rax)
mov  dword [rax+10h], 0  ; clear type info
mov  qword [rax+18h], 0  ; clear data
mov  [rax], rdx           ; store type test value
lea  rdx, [rax+10h]
mov  [rax+20h], rdx       ; self-referential link (RB tree node init)
mov  [rax+28h], rdx       ; self-referential link
mov  qword [rax+30h], 0  ; zero count
```

### Phase 4: Type Hierarchy Lookup via Red-Black Tree (`0x27032F7`--`0x2703362`, `0x2704183`--`0x2704267`)

For each unique type, the pass searches a red-black tree keyed by hashed type name. The tree is rooted at offset `+0xE0` of the module state, with the sentinel node at `+0xD8`. The search is a two-phase process with a three-field comparison:

#### Phase 4a: Compute Type Name Hash

```c
// At 0x27032F7:
char *name = sub_B91420(type_value);     // returns (name_ptr, name_len)
uint64_t hash = sub_B2F650(name, len);   // string hash

// Tree root and sentinel:
RBTreeNode *root = module_state[+0xE0];  // root pointer
RBTreeNode *sentinel = module_state + 0xD8; // sentinel node address
```

`sub_B2F650` (stringHash) is LLVM's standard `xxHash`-style string hasher. It produces a 64-bit hash that is stored at `node[+0x20]` for each type in the tree.

#### Phase 4b: Descend Tree by Hash

```c
// At 0x270330C:
RBTreeNode *current = root;
RBTreeNode *best = sentinel;    // rcx = sentinel initially

while (current != NULL) {
    uint64_t node_hash = current[+0x20];    // hash stored in node
    if (target_hash < node_hash) {
        best = current;                      // track nearest greater
        current = current[+0x10];            // left child
    } else if (target_hash > node_hash) {
        current = current[+0x18];            // right child
    } else {
        // hash matches -- proceed to Phase 4c
        break;
    }
}

if (current == NULL) goto not_found;
```

The binary encodes this as:

```asm
compare_node:
    cmp  rsi, [r15+20h]    ; compare target hash vs node hash
    ja   go_right           ; target > node -> right child
    jnb  hash_match         ; target == node -> verify
    mov  rcx, r15           ; track best (left-leaning)
    mov  r15, [r15+10h]    ; r15 = left child
    test r15, r15
    jnz  compare_node
    jmp  not_found

go_right:
    mov  r15, [r15+18h]    ; r15 = right child
    test r15, r15
    jnz  compare_node
    jmp  not_found
```

#### Phase 4c: Verify Full Match (Hash Collision Resolution)

On hash match, the pass performs a two-step verification to handle collisions:

```c
// At 0x2704200:
// Step 1: compare string lengths
if (current[+0x30] != target_length) {
    // Length mismatch -- this is a hash collision, not a real match.
    // Continue tree traversal to the next candidate.
    goto next_candidate;
}

// Step 2: compare actual type name strings
char *node_name = (char *)current[+0x28];    // node's type name data
char *target_name = target_string;            // from sub_B91420
int cmp = memcmp(node_name, target_name, target_length);
if (cmp != 0) goto next_candidate;

// Verified match -- read vtable data
```

The binary at `0x2704200`--`0x2704240`:

```asm
    cmp  r12, [r15+30h]    ; compare string length
    jnz  next_candidate    ; length mismatch

    mov  rdi, [r15+28h]    ; s1 = node's string data
    mov  rsi, [rbp-0x348]  ; s2 = target string data
    mov  rdx, r12           ; n = length
    call _memcmp
    test eax, eax
    jz   found_match
```

#### Phase 4d: Extract Vtable Data

After verifying the type match, the pass reads the vtable descriptor from the type node:

```c
// At 0x2704248:
void *vtable_start = current[+0x68];    // vtable start address
void *vtable_data  = current[+0x70];    // vtable data pointer (function pointers)

if (vtable_data == NULL) goto skip_type; // no vtable -> nothing to devirtualize
```

The vtable_data pointer leads to an array of function pointers representing the virtual method implementations for this type. The pass iterates this array comparing each entry against call site signatures to identify devirtualization candidates.

### Phase 5: Virtual Call Resolution (`0x2703974`--`0x27039BA`)

For each call site on a matched type, the pass calls `sub_26FEE10` (resolveVirtualCall):

```c
bool resolveVirtualCall(
    void *module_state,         // rdi: r15 (module/pass state)
    void *target_candidates,    // rsi: candidates vector from [rbp-0x230]
    void *hash_entry,           // rdx: r12 (pointer to hash table entry + 8)
    uint32_t candidate_count,   // rcx: from [rbp-0x228]
    void *call_site_info        // r8:  r13 (call site from [r15+0x28])
);
// Returns: al = 1 if unique resolution found, 0 otherwise
```

The resolution algorithm within `sub_26FEE10` works by comparing the vtable offset encoded in each call site's `llvm.type.test` / `llvm.type.checked.load` intrinsic against the vtable slot offsets of all candidate implementations. When exactly one candidate matches, the resolution succeeds with strategy 1 (direct call). When multiple candidates exist but all return the same constant or can be distinguished by a single offset, strategy 2 (unique member) is chosen. When multiple distinct targets exist, strategy 3 (branch funnel) is produced.

The resolution result is written to `hash_entry[+0x28]` as a strategy selector:

| Value | Strategy | Upstream LLVM counter |
|-------|----------|-----------------------|
| 1 | Direct call (single implementation) | `NumSingleImpl` |
| 2 | Unique member dispatch | `NumUniformRetVal` / `NumUniqueRetVal` |
| 3 | Branch funnel | `NumBranchFunnel` |

Before calling `sub_26FEE10`, the pass checks two preconditions:

```c
// At 0x2703974:
void *call_site_list = module_state[+0x28];   // r13 = [r15+0x28]
if (call_site_list == NULL) goto skip;

if (type_value[0] != 0) goto skip;            // byte check: direct type info only

void *existing = hash_entry[+0x28];
if (existing != 0) goto already_resolved;     // skip if previously resolved
```

### Phase 6: Strategy Application (`0x2703BA3`--`0x27046F0`)

#### Strategy 1 -- Direct Call Replacement (`0x27044DA`)

When only one class implements the virtual function (the common case on GPU), the indirect call is replaced with a direct call to the resolved function. This is handled by `sub_26F9AB0` (rewriteCallToDirectCall):

```c
// At 0x27044DA:
void rewriteCallToDirectCall(
    void *type_entry,           // rdi: r12
    void *call_site,            // rsi: [r15+0x38]
    uint64_t vtable_data,       // rdx: byte_3F871B3 (vtable offset data)
    uint32_t flags,             // ecx: 0
    void *resolved_function     // r8:  [rbx+0x40]
);
```

This is the simplest and most common optimization: the `call.reg` becomes `call.direct`, enabling downstream inlining. On GPU this is by far the dominant strategy. Consider a CUDA kernel with a virtual method call inside a loop:

```
; Before devirtualization (PTX):
ld.global.u64  %rd1, [%rd0];     // load vtable ptr
ld.global.u64  %rd2, [%rd1+16];  // load function ptr at vtable slot 2
call.uni       %rd2, (%args);    // indirect call -- full scheduling barrier

; After devirtualization (PTX):
call.uni       _ZN7DerivedN4workEv, (%args);  // direct call -- inlinable
```

The direct call then becomes an inlining candidate with CICC's 20,000-unit budget (89x the upstream LLVM default of 225), and the inliner typically eliminates it entirely, producing fully-inlined code with no call overhead.

#### Strategy 2 -- Unique Member Dispatch (`0x27045C9`)

When multiple classes exist but the call can be dispatched through a unique member offset, the pass rewrites via `sub_26F9080` (rewriteToUniqueMember), passing the diagnostic string `"unique_member"` (13 chars). The member offset is read from `hash_entry[+0x60]` and the base type from `hash_entry[+0x00]`.

```asm
; At 0x27045D9:
mov  r11, [r12]              ; type info (base type)
mov  rsi, [r12+60h]          ; member offset
lea  rax, "unique_member"    ; diagnostic string (13 chars)
call sub_26F9080             ; rewriteToUniqueMember
;   rdx = r14 (type test record)
;   rcx = r13 (call site)
;   r9  = rdi (vtable byte offset / 8)
;   "unique_member" + length 0x0D pushed on stack
```

After the initial rewrite, `sub_26FAF90` performs call-site-specific fixup, checking `[rbx+0x40]` to determine if additional adjustment is needed (e.g., adjusting `this` pointer offset for multiple inheritance).

Upstream LLVM's equivalent covers two sub-strategies: **uniform return value optimization** (all implementations return the same constant -- replace the call with that constant) and **unique return value optimization** (for `i1` returns, compare the vptr against the one vtable that returns a different value). Both are folded under the `"unique_member"` label in CICC's implementation.

#### Strategy 3 -- Branch Funnel (`0x27043B5`)

When multiple possible targets exist and cannot be reduced to a single dispatch, the pass creates a branch funnel -- a compact conditional dispatch sequence that checks the vtable pointer and branches to the correct target. This is handled by three functions:

1. `sub_26F78E0` -- create branch funnel metadata (with diagnostic string `"branch_funnel"`, 13 chars)
2. `sub_BCF480` -- build the conditional dispatch structure
3. `sub_BA8C10` -- emit the indirect branch sequence

```asm
; At 0x27043B5:
mov  r12, [rbx]               ; vtable pointer
mov  rdi, [r12]               ; function pointer from vtable
call sub_BCB120                ; get function declaration

; At 0x27043D3:
lea  rax, "branch_funnel"     ; 13 chars at 0x42BCB92
call sub_26F78E0               ; create branch funnel metadata
call sub_BCF480                ; build dispatch structure
call sub_BA8C10                ; emit indirect branch sequence
```

The branch funnel supports two dispatch granularities:

| Granularity | String | Function | Description |
|-------------|--------|----------|-------------|
| Byte | `"byte"` (4 chars, at `0x3F8C256`) | `sub_26F9120` | Check byte offset into vtable to select target |
| Bit | `"bit"` (3 chars, at `0x43ADFE0+0xE`) | `sub_26F9120` | Check bit offset for single-bit discrimination |

The emission sequence at `0x270450C`--`0x27045BF`:

```asm
; Byte-granularity dispatch:
lea  rcx, "byte"              ; at 0x3F8C256
mov  [rbp-0x318], 4           ; string length
call sub_26F9120               ; emit byte-offset check

; Bit-granularity dispatch:
lea  rbx, "bit"               ; at 0x43ADFE0 + 0xE
mov  [rbp-0x328], 3           ; string length
call sub_26F9120               ; emit bit-offset check

; Finalize:
call sub_26FB610               ; r8=byte_result, r9=bit_result
                               ; rdi=r12, rdx=byte_3F871B3
```

The finalization call `sub_26FB610` receives both byte and bit results and produces the final dispatch sequence. On GPU, branch funnels are rare because device code hierarchies are typically shallow, but the infrastructure exists for cases like thrust/CUB polymorphic iterators.

Upstream LLVM gates branch funnels behind the `wholeprogramdevirt-branch-funnel-threshold` knob (default: 10 targets per call site). CICC inherits this threshold.

### Phase 7: Cleanup (`0x2704144`--`0x270342E`)

After processing all types, the pass performs four cleanup operations:

1. **Function attribute cleanup** (`0x2704144`): iterates the module's function list (red-black tree at `[rax+10h]`), calling `sub_B98000` with parameter `0x1C` (attribute cleanup enum) on each function.
2. **Import list cleanup** (`0x270416C`): processes entries at `module[+0x110..+0x118]`, calling `sub_B43D60` to release function metadata for imported declarations.
3. **Type hierarchy destruction**: `sub_26F92C0` releases all type hierarchy data structures.
4. **Hash table deallocation** (`0x27033C3`): iterates all non-sentinel entries, calls `sub_26F75B0` to release per-entry resolution data, then `sub_C7D6A0` to free the table buffer. Type test result vectors (0x70-byte elements with sub-vectors at offsets `+0x10`, `+0x28`, `+0x40`, `+0x58`) are freed element by element.

Hash table cleanup detail:

```c
// At 0x27033C3:
uint32_t count = hash_table_entry_count;     // [rbp-0x2B8]
if (count == 0) goto skip_cleanup;

void *base = hash_table_base;                // [rbp-0x2C8]
void *end = base + count * 56;               // count * 7 * 8

for (void *entry = base; entry < end; entry += 56) {
    uint64_t key = *(uint64_t *)entry;
    if (key == 0xFFFFFFFFE000) continue;     // empty sentinel
    if (key == 0xFFFFFFFFF000) continue;     // deleted sentinel
    sub_26F75B0(entry[+0x18]);               // release resolution data
}
sub_C7D6A0(base);                            // free table buffer
```

## GPU-Specific Constraints

### Virtual Functions in Device Code

CUDA allows `__device__` virtual functions, but with restrictions that simplify devirtualization:

- **No RTTI on device.** There is no `typeid` or `dynamic_cast` on GPU. This means vtable layouts do not contain RTTI pointers, simplifying vtable reconstruction. The NVVM IR verifier rejects code that attempts `dynamic_cast` in device context.
- **No exceptions on device.** Virtual destructors do not need to handle `__cxa_throw` unwinding paths.
- **Closed world.** No device-side shared libraries, no `dlopen`, no runtime code generation. All virtual targets are known at compile time.
- **No separate compilation for virtual dispatch.** Device linking (nvlink) resolves all symbols before PTX emission, so the merged module always has complete type information.
- **Simplified vtable layout.** Without RTTI pointers and exception tables, device vtables are a flat array of function pointers at known offsets. This makes vtable slot arithmetic straightforward for the WPD pass.

### Cost of Unresolved Indirect Calls

If devirtualization fails, the PTX backend must emit a `call.uni` or `call` through a register. This has several penalties:

1. **No inlining.** The callee is unknown, so the [inliner](./inliner-cost.md) cannot evaluate it.
2. **Full `.param` marshaling.** Every argument must be written to `.param` space; no copy elision is possible. The call ABI (opcodes 510-513: `CallDirect`, `CallDirectNoProto`, `CallIndirect`, `CallIndirectNoProto`) forces `.param`-space round-tripping.
3. **Register pressure spike.** All live registers across the call must be spilled to [local memory](../gpu-execution-model.md#memory-hierarchy) (device DRAM, ~400 cycle latency on SM 70-90).
4. **Scheduling barrier.** The call is a full fence for instruction scheduling -- no operations can be reordered across it.
5. **Divergence hazard.** If different threads in a warp resolve the pointer to different functions, execution [serializes both paths](../gpu-execution-model.md#divergence). In the worst case (32 different targets), this is a 32x slowdown.
6. **Occupancy reduction.** The register spills increase per-thread local memory usage, reducing [occupancy](../gpu-execution-model.md#register-pressure-and-occupancy) and thus hiding less memory latency.

This is why CICC's default inlining budget of 20,000 (89x the upstream LLVM default) makes sense in combination with aggressive devirtualization: the pass converts expensive indirect calls into direct calls, and the inliner then eliminates them entirely.

## Relationship to LowerTypeTests

The `LowerTypeTests` pass (`sub_188C730`, 96,984 bytes at `0x188C730`; also `sub_2638ED0` at 70KB) is the other half of the type-test infrastructure. While WPD *consumes* type test metadata to resolve virtual calls, LowerTypeTests *produces* the runtime type-checking implementation. The interaction:

| Pass | Role | When |
|------|------|------|
| NVModuleSummary (`sub_D7D4E0`) | Produces type metadata in function summaries | During summary construction |
| WholeProgramDevirt (`sub_2703170`) | Consumes type metadata, resolves virtual calls | LTO phase, after summary, before GlobalDCE |
| LowerTypeTests (`sub_188C730`) | Lowers remaining `@llvm.type.test` intrinsics to runtime bit tests | After WPD, if CFI is active |

On GPU, LowerTypeTests is largely dead code -- CUDA does not use Control-Flow Integrity (CFI), and WPD resolves most type tests statically. The sweep at `0x1880000` confirms: "WPD/CFI/LowerTypeTests cluster is also upstream-only; CUDA does not use CFI or type-based devirtualization" in the sense of runtime CFI checks. The type metadata is consumed entirely by WPD's compile-time resolution.

LowerTypeTests validates its input with: `"Second argument of llvm.type.test must be metadata"` and `"Second argument of llvm.type.test must be a metadata string"`. These error paths are unreachable in normal CUDA compilation but exist because CICC links the full upstream LLVM IPO library.

## Optimization Remarks

When a call site is successfully devirtualized, the pass emits an optimization remark through the diagnostic handler. The remark is constructed at `0x2703EDA` using three components:

| Component | String | Address |
|-----------|--------|---------|
| Remark name | `"Devirtualized"` (13 chars) | `0x42BCBEe` |
| Pass name | `"wholeprogramdevirt"` (18 chars) | `0x42BC950` |
| Body prefix | `"devirtualized "` (14 chars) | `0x42BCBE2` |
| Attribute key | `"FunctionName"` (12 chars) | `0x42BC980` |

The remark construction sequence:

```c
// At 0x2703EDA:
sub_B17560(&remark, "Devirtualized", 13, "wholeprogramdevirt", 18);
sub_B18290(&remark, "devirtualized ", 14);        // append body
sub_B16430(&remark, "FunctionName", 12);          // create named attribute
sub_26F69E0(&remark, resolved_function);          // attach target name
sub_B180C0(&remark);                              // finalize
sub_1049740(diag_handler, &remark);               // publish to handler
```

The remark is visible via `-Rpass=wholeprogramdevirt` and includes the name of the resolved target function (obtained from the function's name metadata or via `sub_26F69E0` for unnamed functions).

After remark emission, extensive cleanup of small-string-optimized (SSO) `std::string` objects is performed -- each remark component checks if the string buffer was heap-allocated (compare pointer vs stack buffer address) and frees if necessary.

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
| `wholeprogramdevirt-cutoff` | signed | -1 | Maximum number of devirtualization actions to perform. -1 = unlimited. Useful for bisecting devirtualization-induced miscompiles. |
| `abort-on-max-devirt-iterations-reached` | bool | false | When `DevirtSCCRepeatedPass` at `sub_2284BC0` hits its iteration limit, abort instead of warning. Registered at constructor 378. |

## Complexity

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| Hash table insert/lookup | O(1) amortized, O(n) worst case | Linear probing with sentinel-based open addressing |
| Type hierarchy lookup | O(log n) | Red-black tree keyed by type name hash, with `memcmp` verification |
| Per-type call resolution | O(call_sites * candidates) | For each type, check every call site against every candidate target |
| Branch funnel emission | O(vtable_entries) per site | Linear in number of possible targets |
| String hash (`sub_B2F650`) | O(name_length) | One-pass hash of the type name string |
| Total pass | O(T * S * C * log T) | T = types, S = call sites per type, C = candidates. Typically sparse on GPU. |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `WholeProgramDevirtPass::run` | `sub_2703170` | 13,077 | Pass entry point |
| `buildTypeTestInfo` | `sub_2702830` | ~2,600 | Build type test records from metadata |
| `growHashTable` | `sub_2702540` | ~740 | Grow and rehash the type test hash table |
| `resolveVirtualCall` | `sub_26FEE10` | ~3,200 | Attempt single-target resolution for a call site |
| `rewriteCallToDirectCall` | `sub_26F9AB0` | ~1,600 | Strategy 1: replace indirect call with direct call |
| `rewriteToUniqueMember` | `sub_26F9080` | ~640 | Strategy 2: unique member dispatch rewrite |
| `finalizeUniqueMember` | `sub_26FAF90` | ~1,700 | Strategy 2: call-site-specific fixup |
| `createBranchFunnelMeta` | `sub_26F78E0` | ~1,100 | Strategy 3: create branch funnel metadata |
| `buildBranchFunnel` | `sub_BCF480` | ~6,400 | Strategy 3: build conditional dispatch structure |
| `emitIndirectBranch` | `sub_BA8C10` | ~8,200 | Strategy 3: emit indirect branch sequence |
| `emitDispatchCheck` | `sub_26F9120` | ~500 | Branch funnel byte/bit offset check |
| `finalizeBranchFunnel` | `sub_26FB610` | ~1,800 | Branch funnel finalization |
| `destroyTypeHierarchy` | `sub_26F92C0` | ~400 | Release type hierarchy data structures |
| `releaseResolutionData` | `sub_26F75B0` | ~300 | Free per-entry resolution data |
| `attachFunctionName` | `sub_26F69E0` | ~240 | Attach function name to optimization remark |
| `branchFunnelHelper` | `sub_2700B00` | ~9,800 | Branch funnel main helper (called from `sub_2703170`) |
| `summaryIO` | `sub_2706490` | ~7,600 | WPD summary read/write (`-wholeprogramdevirt-read-summary`) |
| `DevirtSCCRepeatedPass::run` | `sub_2284BC0` | 16,000 | CGSCC devirtualization iteration loop |
| `getNamedMetadata` | `sub_B6AC80` | ~200 | Fetch named metadata node from module |
| `getTypeInfoName` | `sub_B91420` | ~300 | Compute type info name string |
| `stringHash` | `sub_B2F650` | ~180 | Hash a type name string (xxHash-style) |
| `createRemarkHeader` | `sub_B17560` | ~250 | Create optimization remark header |
| `appendRemarkBody` | `sub_B18290` | ~200 | Append body text to remark |
| `createNamedAttribute` | `sub_B16430` | ~200 | Create named attribute for remark |
| `publishRemark` | `sub_1049740` | ~100 | Publish remark to diagnostic handler |

## Cross-References

- **[NVModuleSummary Builder](./module-summary.md)** -- produces the `type_test` metadata consumed by this pass; records devirtualization-relevant type GUIDs in per-function summaries via `sub_D7D4E0`.
- **[Inliner Cost Model](./inliner-cost.md)** -- devirtualized direct calls become inlining candidates with a 20,000-unit budget; the entire value of devirtualization on GPU depends on the inliner subsequently eliminating the call.
- **[ThinLTO Function Import](./thinlto-import.md)** -- in ThinLTO mode the pass would operate in export/import phases, but CICC primarily uses regular LTO for device code.
- **[Pipeline & Ordering](../llvm/pipeline.md)** -- WPD is registered at pipeline parser slot 121 as a module pass; it runs during the LTO phase after summary construction and before GlobalDCE.
- **[NVPTX Call ABI](../pipeline/irgen-functions.md)** -- describes the `.param`-space calling convention that makes indirect calls so expensive (opcodes 510-513: CallDirect, CallDirectNoProto, CallIndirect, CallIndirectNoProto).
- **[LazyCallGraph & CGSCC](../infra/lazycallgraph.md)** -- devirtualization converts ref edges to call edges in the call graph, triggering SCC re-computation via `switchInternalEdgeToCall`. The `DevirtSCCRepeatedPass` at `sub_2284BC0` wraps the CGSCC pipeline in a fixed-point loop.
- **[GPU Execution Model](../gpu-execution-model.md)** -- explains why indirect calls are so expensive on GPU (warp divergence, scheduling barriers, register spilling to local memory).
- **[Hash Infrastructure](../infra/hash-infrastructure.md)** -- the type test hash table uses the same sentinel-based open-addressing pattern as CICC's universal DenseMap infrastructure.
