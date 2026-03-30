# Type Translation, Globals & Special Variables

The type translation subsystem is one of the most algorithmically complex parts of NVVM IR generation. It converts the Edison Design Group (EDG) intermediate language type graph --- which can contain arbitrary mutual recursion, template-dependent types, and CUDA address-space qualifiers --- into a well-formed LLVM type system. The same IR generation phase also handles global variable materialization (with CUDA memory-space assignment), kernel metadata emission, and the translation of CUDA built-in variables (`threadIdx`, `blockIdx`, etc.) into LLVM intrinsic calls.

| | |
|---|---|
| **Type translation entry** | `sub_91AED0` (640 bytes) |
| **Fixed-point driver** | `sub_91AB30` (896 bytes) |
| **Topological sort** | `sub_919CD0` (896 bytes, 10-level BFS) |
| **Type-kind dispatch** | `sub_918E50` (2,400 bytes, 11+ categories) |
| **Type-pair comparator** | `sub_911D10` (1,024 bytes) |
| **Global var creation** | `sub_915C40` (2,018 bytes) |
| **Address space logic** | `sub_916430` (482 bytes) |
| **Annotation emitter** | `sub_914410` (3,524 bytes) |
| **Kernel metadata** | `sub_93AE30` (~5,600 bytes) |
| **Special var classifier** | `sub_920430` (old) / `sub_127F7A0` (new) |
| **Special var codegen** | `sub_922290` (old) / `sub_1285550` (new) |

## EDG-to-LLVM Type Translation

### The Problem

EDG represents C++ types as a graph of IL nodes linked through child/parent pointers, member chains, and scope references. This graph can be arbitrarily cyclic: consider `struct A { B* b; }; struct B { A* a; };` where translating `A` requires translating the pointee type `B`, which requires translating the pointee type `A`. Template instantiations add another dimension --- a template class body may reference types that cannot be resolved until the template arguments themselves are translated. The type translator must produce valid LLVM types from this graph without infinite recursion or stale mappings.

NVIDIA solves this with a **fixed-point iteration scheme**: translate every type, detect whether any translation changed a previously-emitted LLVM type, and if so, repeat the entire pass. The iteration terminates when a full pass produces no changes.

### Context Object Layout

The type translation pass operates on a context structure initialized by `sub_91AB30` and threaded through every function in the subsystem:

| Offset | Size | Field |
|---|---|---|
| `+0x000` | 8 | `debug_logger` --- nullable, enables trace output when non-null |
| `+0x008` | 8 | `pass_list_ptr` --- vector of `(vtable_ptr, pass_instance)` pairs |
| `+0x010` | 8 | `target_info` |
| `+0x018` | 8 | `address_space_map` --- qualifier-to-LLVM-AS translation table |
| `+0x020` | 8 | `llvm_context` --- the `LLVMContext*` |
| `+0x028` | 8 | `module_ptr` |
| `+0x038` | 8 | `edg_node_map` --- hash table: EDG nodes to LLVM values |
| `+0x038` | 16 | `visited_set` --- open-addressed hash set for dedup (at `+0x38`..`+0x48`) |
| `+0x050` | 4 | `iteration_counter` |
| `+0x060` | 12 | `visited_set` control (count, capacity, bucket_count) |
| `+0x078` | 8 | `processed_list` --- vector of completed types |
| `+0x090` | 16 | `type_cache` --- hash table: EDG type pointer to LLVM `Type*` |
| `+0x0A0` | 8 | `remap_list` --- vector of type-remapping entries |
| `+0x150` | 8 | `alignment_table` --- target-specific alignment data |
| `+0x168` | 4 | `threshold` --- type index below which scope lookups are attempted |
| `+0x2A0` | 16 | `pending_replacements` --- vector of `(old_type, new_type)` pairs |
| `+0x310` | 1 | `flags` --- bit-packed control flags |

### Fixed-Point Iteration Algorithm

The entry point `sub_91AED0` recovers pass infrastructure objects by iterating a `vector<pair<void*, void*>>` at `context+8`. Each element is 16 bytes: a vtable pointer identifying the pass, and a pass instance pointer. The function compares vtable pointers against 8 known globals to extract the data layout, reflect pass, target transform info, module context, dominator tree, and alias analysis results. It then calls `sub_91AB30`, the actual iteration driver.

```
// sub_91AB30: TypeTranslationPass driver
fn translate_all_types(ctx: &mut TypeTransCtx, module: &EDGModule) {
    // Optional pre-processing (gated by byte_3C34E60)
    if PRE_PROCESS_FLAG {
        pre_process_types(ctx, module);  // sub_90F800
    }

    // Gather initial flags from all module members
    for member in module.members() {       // linked list from module+80
        gather_initial_flags(member);      // sub_AA3700
    }

    // MAIN FIXED-POINT LOOP
    loop {
        let changed = single_iteration(ctx, module);  // sub_91AA50
        if !changed { break; }
    }

    // Optional late fixup pass (gated by byte_3C35480)
    if OPTIMIZATION_FLAG {
        finalize_late_types(ctx, module);  // sub_90F750
        loop {
            let changed = late_fixup(ctx, module);  // sub_917E30
            if !changed { break; }
        }
    }

    // Optional cleanup (gated by dword_3C351E0)
    if CLEANUP_FLAG {
        cleanup_stale_types(ctx);  // sub_90EB40
    }

    flush_and_finalize(ctx);  // sub_909590
}
```

Each single iteration (`sub_91AA50`) performs three steps:

1. **Topological sort** (`sub_919CD0`): Build a dependency ordering of all EDG type nodes reachable from the module root.
2. **Invalidate** (`sub_913880` for each type in reverse order): Remove stale cache entries for types whose dependencies have changed.
3. **Process** (`sub_9197C0` for each type in reverse order): Translate each type, returning whether any LLVM type was modified.

The iteration returns the logical OR of all `sub_9197C0` results. If any type replacement occurred, the outer loop repeats.

### 10-Level Topological Sort

The function `sub_919CD0` produces a dependency-ordered list of EDG types. Rather than a standard DFS-based topological sort, it uses a **10-level iterative BFS** implemented with sorted sets at each level. This unusual depth accommodates deeply nested C++ class hierarchies with multiple inheritance, where types at depth N must be resolved before types at depth N+1 can be translated.

Each level maintains a sorted set (vector-backed, managed by `sub_6CDA50` for initialization and `sub_6CDC80` for merge/sort). Starting from the module's member list, the algorithm:

1. Inserts root-level type declarations into level 0.
2. For each level 0..9, discovers type dependencies and inserts them into the next level.
3. After all 10 levels, concatenates the sets in reverse (leaf types first, composite types last).

The output is a vector of EDG type node pointers ordered so that leaf types precede the composite types that reference them.

### EDG Type Kind Dispatch

The core dispatcher `sub_918E50` (2,400 bytes) reads the type-kind byte at `edg_node+16` and routes to specialized handlers:

| Kind Byte | Value | Handler | Description |
|---|---|---|---|
| `0x00`--`0x10` | 0--16 | Primitive dispatch | `void`, `bool`, `char`, `int`, `float`, `double`, etc. |
| `0x11` | 17 | Void special | Void type with swap handling in comparator |
| `0x05` | 5 | `sub_5FFE90` | Qualified type (`const`/`volatile`/`restrict`) --- carries address-space info |
| `0x0D` | 13 | Enum path | Enum type bridging C/C++ enum constants to LLVM integers |
| `0x0E` | 14 | Function path | Function type with parameter chain traversal |
| `0x1A` | 26 | `sub_915850` | Array type (subscript form with enumeration base) |
| `0x1B` | 27 | Inline handler | Compound type (struct/union/class) --- multi-child with dedup hash |
| `0x32`--`0x33` | 50--51 | Union variants | Union type (two internal representations) |
| `0x36` | 54 | `sub_918C40` | Typedef / using declaration --- chains through EDG resolution |
| `0x37` | 55 | Using variant | Using declaration variant |
| `0x4B`--`0x4C` | 75--76 | Pointer/ref | Pointer and reference types --- carry qualifier words for address spaces |
| `0x4D` | 77 | Member pointer | Pointer-to-member type |
| `0x4E` | 78 | `sub_914070` | Dependent/nested type --- requires scope resolution |

For types with kind > 23 that are not special-cased, a default handler applies a bitmask test: `0x100000100003FF >> (kind - 25)`. If the low bit is set, the type requires scope tracking (kinds 25--34 selectively, plus kinds 57 and 73). The handler then looks up any existing LLVM type for this EDG type via the scope table, and if the mapping has changed, triggers a replacement plus metadata propagation.

### Compound Type (Struct/Class) Translation

When kind `0x1B` (27) is encountered, the dispatcher uses an inline handler that:

1. Reads the child count from `node+20 & 0xFFFFFFF` and divides by 2 (children come in pairs: type descriptor + offset/alignment info).
2. Builds a reference-counting hash table to detect shared sub-types. If a child type appears exactly once, it can be translated independently. If it appears multiple times, it indicates a shared base class or diamond inheritance pattern.
3. For unique children, calls `sub_911D10` (the type-pair comparator) with the parent scope to translate.

Diamond inheritance is detected by the reference count exceeding 1, which prevents the comparator from making conflicting replacements for the same sub-type.

### Type-Pair Comparison Engine

The function `sub_911D10` is the core workhorse for comparing and replacing type pairs. It takes `(context, type_a, type_b, scope_pair, is_recursive_flag)` and maintains a local worklist of `(type_a, type_b)` pairs:

```
fn compare_and_replace(ctx, type_a, type_b, scope, is_recursive) {
    let mut worklist = vec![(type_a, type_b)];

    while let Some((a, b)) = worklist.pop() {
        if a == b { continue; }

        // Normalize: larger type index = v15, smaller = v14
        let (v14, v15) = if type_index(a) < type_index(b) { (a, b) } else { (b, a) };

        // Primitive vs compound: record scope mapping
        if v14.kind <= 0x17 && v15.kind > 0x17 {
            record_scope_mapping(ctx, v14, v15);
        }

        // Check for UINT_MAX sentinel (incomplete type) -> swap
        if scope_table_lookup(v15) == UINT_MAX {
            swap(&mut v14, &mut v15);
        }

        // Perform actual replacement
        replace_type(ctx, v14, v15, is_recursive);

        // For pointer/reference types: propagate through children
        if v15.kind == 75 || v15.kind == 76 {
            let qualifier = v15.qualifier_word & 0x7FFF;
            // Address space qualifiers trigger child propagation
            if qualifier == 1 || qualifier == 32 || qualifier == 33 || qualifier == 14 {
                worklist.push((v14.child, v15.child));
            }
        }

        // For union types: push all variant children
        if v15.kind == 50 || v15.kind == 51 {
            for child in v15.children() { worklist.push((v14, child)); }
        }
    }
}
```

This worklist-based approach avoids stack overflow on deeply nested types while correctly propagating address-space information through pointer chains.

### CUDA Address Space Propagation

CUDA memory-space qualifiers flow through the EDG type system via a 15-bit **qualifier word** stored at `edg_node+18`. The low 15 bits encode the qualifier ID; bit 15 is a negation flag. During type translation, when the type-pair comparator encounters pointer or reference types (kinds 75/76), it reads the qualifier word and maps it to an LLVM address space:

| EDG Qualifier | Value | LLVM Address Space | CUDA Meaning |
|---|---|---|---|
| Generic | 0 | 0 | Generic (default) |
| Global | 1 | 1 | `__device__` / global memory |
| Function | 14 | --- | Method qualifier (not an address space) |
| Array context A | 26 | --- | Array subscript qualifier A |
| Array context B | 27 | --- | Array subscript qualifier B |
| Shared | 32 | 3 | `__shared__` memory |
| Constant | 33 | 4 | `__constant__` memory |

The conversion is performed by `sub_5FFE90` (qualifier to LLVM address space number) and `sub_5A3140` (creates the appropriately qualified LLVM pointer type). The function `sub_911CB0` combines the conversion with a type-index computation: it takes `(type_kind - 24)` as a base and combines it with the qualifier to produce a unique index for the scope table.

Address-space propagation is transitive: if `struct S` contains a `__shared__ int*` field, the shared qualifier must be reflected in the LLVM type of the pointer field within `S`. The type-pair comparator achieves this by pushing child pairs onto its worklist whenever a pointer/reference type carries a non-zero qualifier.

### Five Caching Layers

To avoid redundant work, the translator maintains five distinct caches:

| Cache | Location | Key | Value | Purpose |
|---|---|---|---|---|
| **Visited set** | `ctx+0x38`..`+0x48` | EDG node ptr | (presence only) | Prevents re-processing the same declaration |
| **Type cache** | `ctx+0x70`..`+0x94` | EDG decl ptr | child type ptr | Tracks which LLVM type a declaration was previously translated to |
| **Type-value map** | Per-call in `sub_913E90` | EDG type ptr | LLVM `Type*` | Caches enum/struct translations; supports inline mode (up to 4 entries) |
| **Scope table** | `ctx+0x10`, hash at `+8/+24` | scope ID | type info | Maps scope identifiers to type information for type-pair comparison |
| **Type index table** | `ctx+0x98`+ | compound key | monotonic index | Linear ordering of processed types; Jenkins-like hash for compound keys |

All hash tables use the standard DenseMap infrastructure with NVVM-layer sentinels (-8 / -16). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing strategy, and growth policy.

Cache invalidation is handled by `sub_913880`, which walks a type's member list and removes stale entries. Invalidation cascades: if a struct type is invalidated, all member types that are non-trivial (not kind 54/55 typedef/using) are also removed from the cache.

### Template Specialization

Template types are handled by `sub_918790` (struct/class type translation with template instantiation support):

1. `sub_41F0F0` extracts template argument descriptions from the EDG IL into a 1,536-byte stack buffer (heap fallback for > 50 arguments).
2. `sub_908040` performs syntactic template argument substitution, producing two lists: substituted types and original types.
3. If both lists are non-empty **and** the optimization flags `byte_3C35480` + `byte_3C353A0` are both set, `sub_910920` performs semantic type matching using the full optimization infrastructure.
4. Otherwise, `sub_906590` creates the LLVM type directly from the substitution result.

The two-pass approach (syntactic substitution then semantic matching) handles cases like `template<typename T> struct Wrapper { T* data; }` where `Wrapper<__shared__ int>` must produce a pointer in address space 3 --- the syntactic pass substitutes `T = __shared__ int`, and the semantic pass verifies the LLVM type is correct.

Template specialization support is entirely optional and gated behind configuration flags, allowing it to be disabled for faster compilation when not needed.

### Primitive Type Translation Table

The dispatcher `sub_918E50` handles kinds `0x00`--`0x10` (values 0--16) as primitive/scalar types. These map directly from EDG internal type representation to LLVM IR types. The correspondence between the three type-tag namespaces used across cicc is:

| EDG Type Kind | EDG Printer `type_kind` | Cast Codegen Tag (`*(type+8)`) | LLVM IR Type | Width |
|---|---|---|---|---|
| `0x00` | `0x00` error | --- | `<error>` | --- |
| `0x01` | `0x01` void | 3 | `void` | 0 |
| `0x02` | `0x02` scalar/integer | 17 | `iN` | N bits |
| `0x03` | `0x03` float | 1 (half), 2 (float), 3 (double), 4 (fp80), 5 (fp128), 6 (bf16) | see FP table | varies |
| `0x04` | `0x04` imaginary | --- | emulated | varies |
| `0x05` | `0x05` complex | --- | `{ fN, fN }` struct | 2x float |
| `0x06` | `0x06` pointer/ref | 18 | `ptr` (opaque) or `ptr addrspace(N)` | 32/64 |
| `0x07` | `0x07` function | 15 (function), 16 (ptr-to-fn) | function type | --- |
| `0x08` | `0x08` array | 20 | `[N x elem]` | N * elem |
| `0x09`--`0x0B` | `0x09`--`0x0B` class/struct/union/enum | 21 (struct) | `%struct.Name = type { ... }` | layout |
| `0x0C` | `0x0C` elaborated/typedef | --- | resolved target | --- |
| `0x0D` | `0x0D` pointer-to-member | --- | `{ ptr, i64 }` or `i64` | 64/128 |
| `0x0E` | `0x0E` template param | --- | deduced | --- |
| `0x0F` | `0x0F` vector | 16 | `<N x elem>` | N * elem |
| `0x10` | `0x10` scalable vector | 16 | `<vscale x N x elem>` | runtime |

The integer type (EDG kind `0x02`) carries its bit-width in the upper bytes of the type word. The cast codegen subsystem (`sub_128A450`) classifies types by the tag byte at `*(type+8)`: tags 1--6 are floating-point (see next section), tag 11 is integer, tag 15 is pointer, and tag 16 is vector/aggregate. The key dispatch idiom `(tag - 1) > 5u` tests "is NOT a float"; `(tag & 0xFD) != 0xB` tests "is NOT integer-like".

### Floating-Point Type Encoding

Floating-point types use a sub-kind byte stored in the EDG type node at `v3[10].m128i_i8[0]` (type printer) or equivalently the cast codegen tag at `*(type+8)`. The complete mapping including all NVIDIA-extended formats:

| Cast Tag | EDG FP Sub-kind | Mangling | C++ Type | LLVM Type | Width | SM Minimum |
|---|---|---|---|---|---|---|
| 1 | 0 / 0xA | `DF16_` | `_Float16` / `__half` | `half` | 16 | SM 53 (scalar), SM 70 (packed) |
| 1 | 1 | `Dh` | `__fp16` | `half` | 16 | SM 53 |
| 2 | 2 | `f` | `float` | `float` | 32 | all |
| --- | 3 | `DF32x` | `_Float32x` | `double` (promoted) | 64 | all |
| 3 | 4 | `d` | `double` | `double` | 64 | all |
| --- | 5 | `DF64x` | `_Float64x` | `fp128` (emulated) | 128 | all |
| --- | 6 | (single) | `long double` | platform-dependent | arch | --- |
| --- | 7 | `u7float80` | `float80` | `x86_fp80` | 80 | N/A on GPU |
| --- | 8 | `g` | `__float128` | `fp128` | 128 | emulated |
| 6 | 9 | `u6__bf16` or `DF16b` | `__bf16` / `__nv_bfloat16` | `bfloat` | 16 | SM 80 |
| --- | 0xB | `DF32_` | `_Float32` | `float` | 32 | all |
| --- | 0xC | `DF64_` | `_Float64` | `double` | 64 | all |
| --- | 0xD | `DF128_` | `_Float128` | `fp128` | 128 | emulated |

The `bf16` mangling has a three-way ABI gate controlled by `qword_4F077B4` (low 32 = `use_new_bf16_mangling`, high 32 = `bf16_abi_version`) and `qword_4F06A78` (secondary selector). Old ABI emits `u6__bf16` (Itanium vendor-extended); C++23 ABI emits `DF16b` (P1467 standard). The `__nv_bool` type (EDG printer case `0x02`, bit 4 of `+162`) is a CUDA-specific boolean that emits `"__nv_bool"` when `sub_5D76E0` (CUDA mode check) returns true, or `"_Bool"` / `"bool"` otherwise.

Two additional NVIDIA-specific types have dedicated mangling:

| EDG Type Code | Mangling | C++ Type | Purpose |
|---|---|---|---|
| 17 | `u11__SVCount_t` | `__SVCount_t` | ARM SVE predicate count |
| 18 | `u6__mfp8` | `__mfp8` | 8-bit minifloat (FP8 E4M3/E5M2 base) |

On the LLVM side, the `__mfp8` type maps to `i8` storage with metadata annotations indicating the floating-point interpretation.

### CUDA FP8/FP6/FP4 Extended Type Keywords

CUDA 12.x+ introduces narrow floating-point types for transformer inference and tensor core operations. The EDG parser (`sub_691320`) recognizes these as token values 236 and 339--354, all resolved through `sub_6911B0` (CUDA type-token resolver):

| Token | Keyword | Format | Width | Packed Variant | SM Requirement |
|---|---|---|---|---|---|
| 236 | `__nv_fp8_e4m3` | E4M3 (4-bit exponent, 3-bit mantissa) | 8 | --- | SM 89 |
| 339 | `__nv_fp8_e5m2` | E5M2 (5-bit exponent, 2-bit mantissa) | 8 | --- | SM 89 |
| 340 | `__nv_fp8x2_e4m3` | E4M3 packed pair | 16 | 2 elements | SM 89 |
| 341 | `__nv_fp8x2_e5m2` | E5M2 packed pair | 16 | 2 elements | SM 89 |
| 342 | `__nv_fp8x4_e4m3` | E4M3 packed quad | 32 | 4 elements | SM 89 |
| 343 | `__nv_fp8x4_e5m2` | E5M2 packed quad | 32 | 4 elements | SM 89 |
| 344 | `__nv_fp6_e2m3` | E2M3 (2-bit exponent, 3-bit mantissa) | 6 | --- | SM 100 |
| 345 | `__nv_fp6_e3m2` | E3M2 (3-bit exponent, 2-bit mantissa) | 6 | --- | SM 100 |
| 346 | `__nv_fp6x2_e2m3` | E2M3 packed pair | 12 | 2 elements | SM 100 |
| 347 | `__nv_fp6x2_e3m2` | E3M2 packed pair | 12 | 2 elements | SM 100 |
| 348 | `__nv_mxfp8_e4m3` | MX-format E4M3 | 8 | --- | SM 100 |
| 349 | `__nv_mxfp8_e5m2` | MX-format E5M2 | 8 | --- | SM 100 |
| 350 | `__nv_mxfp6_e2m3` | MX-format E2M3 | 6 | --- | SM 100 |
| 351 | `__nv_mxfp6_e3m2` | MX-format E3M2 | 6 | --- | SM 100 |
| 352 | `__nv_mxfp4_e2m1` | MX-format E2M1 (FP4) | 4 | --- | SM 100 |
| 353 | `__nv_satfinite` | Saturation-to-finite modifier | --- | --- | SM 89 |
| 354 | `__nv_e8m0` | E8M0 exponent-only scale format | 8 | --- | SM 100 |

The resolver `sub_6911B0` follows the `field_140 == 12` (qualified/elaborated type) chain to find the base type node, then sets `v325 = 20` (typename). At the LLVM level, these narrow types are lowered to integer storage types (`i8`, `i16`, `i32`) with type metadata or intrinsic-based interpretation. The `cvt_packfloat` intrinsic family handles conversion to and from these formats with explicit format specifiers:

| cvt_packfloat Case | PTX Suffix | Format |
|---|---|---|
| 2 | `.e4m3x2` | FP8 E4M3 pair |
| 3 | `.e5m2x2` | FP8 E5M2 pair |
| 4 | `.bf16x2` | BFloat16 pair |
| 5 | `.e2m1x2` | FP4 E2M1 pair (SM 100+) |
| 6 | `.e2m3x2` | FP6 E2M3 pair (SM 100+) |
| 7 | `.e3m2x2` | FP6 E3M2 pair (SM 100+) |
| 8 | `.ue8m0x2` | UE8M0 scale pair (SM 100+) |

### Address Space Annotations on Types

CUDA memory-space qualifiers propagate through the EDG type system via a 15-bit **qualifier word** at `edg_node+18`. The low 15 bits encode a qualifier ID; bit 15 is a negation flag. The qualifier word is the single mechanism through which `__device__`, `__shared__`, `__constant__`, and `__managed__` semantics reach the LLVM type system.

**EDG qualifier word to LLVM address space mapping** (performed by `sub_5FFE90`):

| Qualifier Word (`node+18 & 0x7FFF`) | LLVM Address Space | CUDA Source | Notes |
|---|---|---|---|
| 0 | 0 | (default/generic) | Unqualified pointers |
| 1 | 1 | `__device__` / global | Explicit global annotation |
| 9 | 0 (with flag check via `sub_5F3280`) | (generic variant) | Conditional on context |
| 14 | --- | `__host__` / method qualifier | Not an address space --- function qualifier |
| 26 | --- | (array subscript context A) | Internal, not an address space |
| 27 | --- | (array subscript context B) | Internal, not an address space |
| 32 | 3 | `__shared__` | Per-block shared memory |
| 33 | 4 | `__constant__` | Read-only constant memory |

The function `sub_5A3140` creates the appropriately address-space-qualified LLVM pointer type given the qualifier output from `sub_5FFE90`. The helper `sub_911CB0` combines address space information with the type kind to produce a unique scope-table index: it computes `(type_kind - 24)` as a base and combines it with the qualifier to produce a monotonic key.

**EDG frontend encoding** (from `sub_691320` parser, tokens 133--136, and `sub_667B60`):

| Parser Token | CUDA Keyword | `v305` Value | EDG `memory_space_code` | Target AS |
|---|---|---|---|---|
| 133 | `__shared__` | 4 | 2 | 3 |
| 134 | `__device__` | 5 | 1 | 1 |
| 135 | `__constant__` | 6 | 3 | 4 |
| 136 | `__managed__` | 7 | (special) | 0 + `"managed"` annotation |
| 273 | `__global__` (addr-space attr) | --- | 0 | 0 |
| 274 | `__shared__` (addr-space attr) | --- | 2 | 3 |
| 275 | `__constant__` (addr-space attr) | --- | 3 | 4 |
| 276 | `__generic__` (addr-space attr) | --- | (parsed) | (parsed) |

Address-space propagation through types is **transitive**: if `struct S` contains a `__shared__ int*` field, the shared qualifier flows through the pointer type and is preserved in the LLVM `ptr addrspace(3)` type of that field. The type-pair comparator `sub_911D10` achieves this by pushing child pairs onto its worklist whenever a pointer/reference type (kinds 75/76) carries a non-zero qualifier. The qualifier-word masks 1, 14, 32, and 33 are the four values that trigger this child propagation.

For a full cross-reference of all 10 address spaces (including AS 5 local, AS 6 tensor memory, AS 7 shared cluster, AS 25 internal device, AS 53 MemorySpaceOpt annotation, AS 101 param), see [Address Spaces](../reference/address-spaces.md).

### Vector Type Handling

NVPTX has a highly constrained vector type model. Only four vector types are legal --- all packed into 32-bit `Int32HalfRegs` (`%hh` prefix in PTX):

| Legal Vector Type | LLVM MVT | PTX Register Class | PTX Suffix | SM Minimum |
|---|---|---|---|---|
| `v2f16` | `v2f16` | `Int32HalfRegs` | `.f16x2` | SM 70 (arith), SM 53 (ld/st) |
| `v2bf16` | `v2bf16` | `Int32HalfRegs` | `.bf16x2` | SM 80 |
| `v2i16` | `v2i16` | `Int32HalfRegs` | `.s16x2` | SM 70 |
| `v4i8` | `v4i8` | `Int32HalfRegs` | (packed bytes) | SM 70 |

All wider vector types are illegal and undergo recursive split/scalarize during [type legalization](../llvm/type-legalization.md). The split depth for common CUDA vector types:

| CUDA Type | LLVM Type | Split Chain | Final Form |
|---|---|---|---|
| `float4` | `v4f32` | v4f32 -> 2x v2f32 -> 4x f32 | 4 scalar `float` ops |
| `float2` | `v2f32` | v2f32 -> 2x f32 | 2 scalar `float` ops |
| `int4` | `v4i32` | v4i32 -> 2x v2i32 -> 4x i32 | 4 scalar `i32` ops |
| `double2` | `v2f64` | v2f64 -> 2x f64 | 2 scalar `double` ops |
| `half2` | `v2f16` | **legal** (no split) | single `.f16x2` packed op |
| `__nv_bfloat162` | `v2bf16` | **legal** (no split, SM 80+) | single `.bf16x2` packed op |
| `short2` | `v2i16` | **legal** (no split) | single `.s16x2` packed op |
| `char4` / `uchar4` | `v4i8` | **legal** (no split) | single packed-byte op |
| `half` (4 elements) | `v4f16` | v4f16 -> 2x v2f16 | 2 packed `.f16x2` ops |
| `half` (8 elements) | `v8f16` | v8f16 -> v4f16 -> 2x v2f16 | 4 packed `.f16x2` ops |

The critical architectural insight: `v2f32` is NOT legal on NVPTX (no 64-bit packed float register class exists), so `float4` always fully scalarizes to four independent `f32` operations. In contrast, `half2` stays packed throughout the pipeline, delivering 2x throughput via `add.f16x2`, `mul.f16x2`, and `fma.rn.f16x2` PTX instructions.

SM-version gating affects which types are legal at which pipeline stage:

- **SM < 53**: No legal vector types; `v2f16` must be scalarized, and scalar `f16` is promoted to `f32`.
- **SM 53--69**: Scalar `f16` is legal; `v2f16` is legal for load/store but packed arithmetic may be `Custom` or `Expand`.
- **SM 70+**: `v2f16` fully legal with packed arithmetic. `i128` scalar register class added.
- **SM 80+**: `v2bf16` added as legal vector type.
- **SM 100+**: Additional packed FP types for `cvt_packfloat` --- `e2m1x2`, `e2m3x2`, `e3m2x2`, `ue8m0x2`.

Tensor core matrix fragments bypass vector legalization entirely. WMMA and WGMMA intrinsics represent matrix data as individual scalar registers or `{f16, f16, ...}` struct aggregates, not as LLVM vector types. See [MMA Codegen](../llvm/mma-codegen.md) for the tensor-core lowering path.

### Cast Codegen Type Tags

The cast emission function `sub_128A450` uses a distinct type-tag namespace at `*(type+8)`. This tag drives all cast instruction selection and must be clearly distinguished from the EDG type-kind byte at `edg_node+16`:

| Tag | LLVM Type | Cast Behavior |
|---|---|---|
| 1 | `half` (f16) | Float family; float-to-float casts use `fpext`/`fptrunc` |
| 2 | `float` (f32) | Float family |
| 3 | `double` (f64) | Float family |
| 4 | `x86_fp80` | Float family (not used on GPU) |
| 5 | `fp128` | Float family; triggers standard LLVM cast path (no `__nv_*_rz` intrinsic) |
| 6 | `bfloat` (bf16) | Float family |
| 11 | `iN` (integer) | Integer family; width at `*(type+8) >> 8` |
| 15 | `ptr` | Pointer family |
| 16 | `<N x elem>` (vector) | Vector/aggregate; address-space extraction via `sub_16463B0` |

Integer-to-float conversions (tags 11 -> 1..6) default to `sitofp`/`uitofp` but can route through NVIDIA-specific `__nv_*_rz` round-to-zero intrinsics when `unk_4D04630` is clear. These intrinsics (`__nv_float2int_rz`, `__nv_double2ll_rz`, etc.) are emitted as plain function calls and later pattern-matched by the PTX backend to `cvt.rz.*` instructions. The `fp128` path always uses standard LLVM casts because 128-bit floating point is emulated via [FP128/I128 library calls](../passes/fp128-emulation.md).

### SelectionDAG SimpleVT Encoding

After IR generation, types enter the SelectionDAG type system where they are encoded as single-byte `SimpleVT` values for the legality table lookup at `NVPTXTargetLowering + 2422`:

| SimpleVT | LLVM Type | Bitwidth |
|---|---|---|
| 0 | extended/custom | computed via `sub_1F58D40` |
| 1 | `i1` | 1 |
| 2 | `i2` | 2 |
| 3 | `i8` | 8 |
| 4 | `i16` | 16 |
| 5 | `i32` | 32 |
| 6 | `i64` | 64 |
| 7 | `i128` | 128 |
| 8 | `f16` / `bf16` | 16 |
| 9 | `f32` | 32 |
| 10 | `f64` | 64 |
| 14--55 | fixed-width vector types | vector of above |
| 56--109 | scalable vector types | scalable vector of above |

The bitwidth-to-SimpleVT conversion pattern appears 11 times in the 348KB `DAGTypeLegalizer::run` monolith (`sub_20019C0`), and the vector-to-scalar-element switch table (cases 14--109 mapping back to scalar VT 2--10) appears 6 times. This redundancy is an artifact of the monolithic inlining --- upstream LLVM factors these into per-category files (`LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, etc.).

## Global Variable Code Generation

### Module-Level Driver

Global variable codegen is driven by `sub_915990` (~2,700 bytes), which iterates all EDG IL global declarations and categorizes them into sorted sets:

- Regular device globals
- `__constant__` globals
- `__shared__` globals
- `__managed__` globals
- Texture references
- Surface references
- Grid constants

After categorization, a topological sort (using the same `sub_3FEBB0`/`sub_3FED60` graph primitives as the type translator) determines the order in which globals must be materialized. If global `A`'s initializer references global `B`, then `B` must be code-generated first. The transitive dependency discovery is performed by `sub_914960`, a BFS that walks EDG IL linkage chains, filtering nodes with kind byte in range `[25..34]` (variable, function, and template declarations).

### Address Space Determination

The function `sub_916430` (482 bytes) examines EDG IL node attribute bytes to determine the NVPTX address space for a global variable:

```
fn determine_address_space(edg_node: &EDGNode) -> u32 {
    let storage_class = edg_node[0x88];
    let flags_9c      = edg_node[0x9C];
    let flags_b0      = edg_node[0xB0];
    let flags_ae      = edg_node[0xAE];
    let flags_a8      = edg_node[0xA8] as u64;

    // __constant__: storage class 2
    if storage_class == 2 {
        return 4;  // constant address space
    }

    // __shared__: bit 7 of flags_9c
    if flags_9c & 0x80 != 0 {
        if flags_ae & 1 != 0 {
            return 3;  // extern __shared__
        }
        if flags_b0 & 0x20 != 0 {
            return 5;  // local memory (stack-local shared variant)
        }
        return 3;  // __shared__
    }

    // Bit 6 of flags_9c: device-side memory
    if flags_9c & 0x40 != 0 {
        if edg_node[0xF0] != 0 {
            return 3;  // template-instantiated shared variable
        }
        return 0;  // generic device
    }

    // Extended attribute flags
    if flags_a8 & 0x2000100000 != 0 {
        return 3;  // shared-like semantics
    }

    if storage_class > 2 {
        emit_diagnostic("unsupported storage class!");
    }

    return 0;  // default: generic device memory
}
```

### NVPTX Address Space Assignment

See [Address Spaces](../reference/address-spaces.md) for the complete master table mapping LLVM AS numbers to PTX qualifiers, hardware, and pointer widths.

In the IR generation context: address space 0 (generic) is the default for `__device__` variables. Address space 1 (global) appears in pointer types when the global qualifier is explicit in the type annotation (as opposed to being inferred from the variable declaration). `__managed__` variables use address space 0 (same as regular device globals) but receive a `"managed"` annotation in `nvvm.annotations` that the runtime uses to set up Unified Virtual Memory mappings.

### GlobalVariable Object Creation

The function `sub_915C40` (2,018 bytes) materializes an LLVM `GlobalVariable`:

1. **Hash table lookup**: Checks whether the EDG node has already been materialized. The table at `ctx+0x178..0x190` maps EDG node pointers to `GlobalVariable*`. If found with a different type, calls `GlobalVariable::mutateType` to reconcile.

2. **Allocation**: Allocates 88 bytes (`0x58`) via `operator new`, then calls the `GlobalVariable` constructor with module, type, `isConstant` flag, linkage, initializer (null for declarations), name, and address space.

3. **Alignment**: Computes alignment via `sub_91CB50` (a DataLayout wrapper), then converts to log2 via BSR (bit-scan-reverse) for LLVM's `MaybeAlign` representation. Always explicitly set, even for naturally-aligned types.

4. **Initializer**: If `edg_node[0xB0] & 0x20` is set and the variable is not extern (`edg_node[0x88] != 1`), calls `sub_916690` to generate the initializer IR. The initializer handler dispatches on a variant byte: variant 0/3 for constant expressions, variant 1/2 for aggregate initializers.

5. **`__managed__` annotation**: If `edg_node[0x9D] & 1` is set, emits `("managed", 1)` to the annotation list via `sub_913680`.

6. **Texture/surface detection**: If the mode flag at `ctx+0x168` has bit 0 set, calls `sub_91C2A0` (isTextureType) and `sub_91C2D0` (isSurfaceType). Matching variables get `"texture"` or `"surface"` annotations and are inserted into a red-black tree at `ctx+0x200` for ordered tracking during annotation emission.

7. **Registration**: The new `GlobalVariable*` is stored into the hash table for future lookups.

### Finalization: Metadata and `@llvm.used`

After all globals are materialized, `sub_915400` calls four finalization functions in sequence:

**`sub_9151E0` --- emit `nvvmir.version`**: Creates a named metadata node `"nvvmir.version"` containing version operands as `ConstantInt` values wrapped in `ConstantAsMetadata`. When debug info is present (`ctx+0x170` non-null), the tuple has 4 operands including address-space-qualified indices; otherwise 2 operands.

**`sub_914410` --- emit `nvvm.annotations`**: Iterates the annotation list at `ctx+0x1B0..0x1B8` and creates `MDTuple` entries under the named metadata `"nvvm.annotations"`. Each annotation record produces a `{GlobalValue*, MDString-key, ConstantInt-value}` triple. Three annotation categories receive special batching: `"grid_constant"`, `"preserve_n_data"`, and `"preserve_reg_abi"` --- these are collected into compound MDTuples rather than emitting one per parameter, reducing metadata size in kernels with many annotated parameters.

**`sub_90A560` --- emit `@llvm.used`**: Builds the `@llvm.used` global array that prevents LLVM from dead-stripping texture references, surface references, and managed variables. The function iterates the registered global triples at `ctx+0x198..0x1A0` (24-byte records, hence the `0xAAAAAAAAAAAAAAAB` magic divisor for dividing by 3), bitcasts each `GlobalValue*` to `i8*`, constructs a `ConstantArray` of type `[N x i8*]`, and creates a global with name `"llvm.used"`, appending linkage, and section `"llvm.metadata"`.

**Conditional**: If debug info is present, emits a `"Debug Info Version"` module flag with value 3 via `Module::addModuleFlag`. If enabled, also emits `"llvm.ident"` metadata identifying the compiler.

## Kernel Metadata

### Annotation Emitter (`sub_93AE30`)

After a kernel's function body has been code-generated, `sub_93AE30` translates EDG-level kernel attributes (`__launch_bounds__`, `__cluster_dims__`) into LLVM named metadata under `"nvvm.annotations"`. The function signature:

```c++
void emitKernelAnnotationMetadata(
    NVVMContext *ctx,       // ctx->module at offset +344
    FuncDecl    *funcDecl,  // EDG function declaration, params at +16, count at +8
    LaunchAttr  *launch,    // __launch_bounds__/cluster attrs, NULL if none
    MDNodeVec   *out        // output vector of metadata nodes
);
```

### Parameter Metadata

For each function parameter (stride 40 bytes, iterated from `funcDecl+16`):

1. **Visibility check**: If launch attributes exist and bit `0x20` of `launch+198` is clear, or `param+32 != 0`, emits opcode 22 (hidden/implicit parameter). If `dword_4D04628` is set and the launch bit is set, calls `sub_8D2E30` to check for special types and emits opcode 40.

2. **Type dispatch**:
   - Type `1` (pointer): Checks `sub_91B6F0` for read-only image/sampler (opcode 54) and `sub_91B730` for surface reference (opcode 79).
   - Type `2` (value): Computes alignment metadata via `sub_91A390`, then log2 via BSR, emits packed `(log2, hasValue)` pair. Checks for alignment attribute tag 92 via `sub_A74D20`.

3. **MDNode creation**: `sub_A7B020(module, paramIndex, &attrAccum)` creates the MDNode for each parameter.

### Cluster Metadata

Triggered when `launch` is non-null and `*(launch+328)` points to a valid cluster config. The cluster config struct:

| Offset | Field | Used As |
|---|---|---|
| `+20` | `[5]` | `reqntid.x` (cluster) |
| `+24` | `[6]` | `reqntid.y` (cluster) |
| `+28` | `[7]` | `reqntid.z` (cluster) |
| `+40` | `[10]` | `cluster_dim.z` (also presence flag: > 0 triggers emission) |
| `+44` | `[11]` | `cluster_dim.y` |
| `+48` | `[12]` | `cluster_dim.x` |

When `cluster_config[10] > 0`, three metadata entries are emitted in order:

1. **`nvvm.blocksareclusters`** --- boolean flag, no value string. Emitted unconditionally.
2. **`nvvm.reqntid`** --- the three cluster dimension fields `[12],[11],[10]` are converted to decimal strings and concatenated with commas: `"{x},{y},{z}"`. Uses SSO `std::string` objects with a two-digit lookup table (`"00","01",...,"99"`) for fast integer-to-string conversion. A `0x3FFFFFFFFFFFFFFF` sentinel triggers a fatal `"basic_string::append"` error on overflow.
3. **`nvvm.cluster_dim`** --- the three fields `[7],[6],[5]` are similarly concatenated.

### Function-Level Metadata Node

After all per-parameter and cluster metadata is accumulated, if the accumulator is non-empty, `sub_A7B020(module, 0xFFFFFFFF, &attrAccum)` creates a function-level MDNode with parameter index `-1` (sentinel). This node carries all function-level annotations combined.

### Annotation Reader (`sub_A84F90`)

The inverse of the emitter. Reads `"nvvm.annotations"` named metadata from an LLVM Module and populates internal structures. For each `{function_ref, key_string, value}` operand tuple, the key is matched via raw integer comparisons (not `strcmp`):

| Key String | Match Method | Handler |
|---|---|---|
| `"kernel"` | 6-byte `i32+i16` compare | `sub_CE8040`: set/clear `nvvm.kernel` flag |
| `"maxntidx/y/z"` | 7-byte prefix + suffix char | `sub_A7C1C0` with `"nvvm.maxntid"` |
| `"reqntidx/y/z"` | 7-byte prefix + suffix char | `sub_A7C1C0` with `"nvvm.reqntid"` |
| `"cluster_dimx/y/z"` | 12-byte qword+i32 + suffix | `sub_A7C1C0` with `"nvvm.cluster_dim"` |
| `"maxnreg"` | 7-byte qword + byte `'g'` | `sub_B2CD60` with `"nvvm.maxnreg"` |
| `"minctasm"` | 8-byte single qword compare | `sub_B2CD60` with `"nvvm.minctasm"` |
| `"maxclusterrank"` | 14-byte multi-width compare | `sub_B2CD60` with `"nvvm.maxclusterrank"` |
| `"cluster_max_blocks"` | 18 bytes | Same handler as `maxclusterrank` |
| `"align"` | 5 bytes | `sub_B2CCF0`: BSR-based log2 alignment |

The raw integer comparison technique avoids `strcmp` overhead by loading the key bytes as `i32`/`i64` values and comparing in a single instruction. For example, `"kernel"` is checked as two loads: `*(uint32_t*)key == 0x6E72656B` and `*(uint16_t*)(key+4) == 0x6C65`.

### Complete Metadata String Catalog

**Module-level named metadata:**

| Key | Purpose |
|---|---|
| `nvvm.annotations` | Container for all kernel and global annotations |
| `nvvm.annotations_transplanted` | Flag: annotations already migrated to function-level |
| `nvvm.reflection` | Compile-time reflection constants |
| `nvvmir.version` | NVVM IR version (2 or 4 operands) |
| `llvm.used` | Array preventing dead-stripping of annotated globals |
| `llvm.ident` | Compiler identification string |

**Function-level metadata keys:**

| Key | Value Format | Source |
|---|---|---|
| `nvvm.kernel` | (boolean presence) | `__global__` qualifier or calling convention `0x47` |
| `nvvm.maxntid` | `"x,y,z"` | `__launch_bounds__(maxThreads)` |
| `nvvm.reqntid` | `"x,y,z"` | `__launch_bounds__` or cluster config |
| `nvvm.maxnreg` | decimal string | `__launch_bounds__(..., ..., maxRegs)` |
| `nvvm.minctasm` | decimal string | `__launch_bounds__(..., minCTAs)` |
| `nvvm.maxclusterrank` | decimal string | SM >= 90 cluster rank limit |
| `nvvm.blocksareclusters` | (boolean presence) | `__cluster_dims__` present |
| `nvvm.cluster_dim` | `"x,y,z"` | `__cluster_dims__(x,y,z)` |

**Global variable annotations** (emitted as `{GlobalValue*, MDString, i32}` triples in `nvvm.annotations`):

| Annotation | Value | Trigger |
|---|---|---|
| `"managed"` | 1 | `__managed__` qualifier |
| `"texture"` | 1 | Texture reference type detected |
| `"surface"` | 1 | Surface reference type detected |
| `"grid_constant"` | (batched) | `__grid_constant__` parameter attribute |
| `"preserve_n_data"` | (batched) | NVIDIA-internal preservation hint |
| `"preserve_reg_abi"` | (batched) | NVIDIA-internal register ABI hint |

### Metadata Accessor Functions

The backend reads metadata through typed accessor functions in the `0xCE7xxx`--`0xCE9xxx` range:

| Address | Reconstructed Name | Returns |
|---|---|---|
| `sub_CE9220` | `isKernel(func)` | `true` if linkage == `0x47` OR `nvvm.kernel` present |
| `sub_CE8D40` | `getMaxNtid(out, func)` | Parses `"nvvm.maxntid"` as `(x,y,z)` triple |
| `sub_CE8DF0` | `getReqNtid(out, func)` | Parses `"nvvm.reqntid"` as `(x,y,z)` triple |
| `sub_CE8EA0` | `getClusterDim(out, func)` | Parses `"nvvm.cluster_dim"` as `(x,y,z)` triple |
| `sub_CE9030` | `getMaxClusterRank(func)` | Checks `"cluster_max_blocks"` then `"nvvm.maxclusterrank"` |
| `sub_CE90E0` | `getMinCtaSM(func)` | Checks `"minctasm"` then `"nvvm.minctasm"` |
| `sub_CE9180` | `getMaxNReg(func)` | Checks `"maxnreg"` then `"nvvm.maxnreg"` |

Each accessor first checks the function-level metadata (post-transplant), then falls back to the raw `nvvm.annotations` tuples (pre-transplant). The `isKernel` check is especially important: it recognizes kernels either by calling convention `0x47` or by the `nvvm.kernel` metadata presence, ensuring compatibility with both the EDG frontend path and bitcode loaded through LibNVVM.

### Metadata Lifecycle

The complete flow from CUDA source to PTX directives:

```
CUDA:  __global__ void kern() __launch_bounds__(256, 2) __cluster_dims__(2, 1, 1)

EDG:   LaunchAttr { cluster_config[12]=256, [11]=1, [10]=1, [7]=1, [6]=1, [5]=2 }

sub_93AE30:
  -> nvvm.blocksareclusters (presence flag)
  -> nvvm.reqntid = "256,1,1"
  -> nvvm.cluster_dim = "2,1,1"
  -> function-level MDNode (index -1)

sub_A84F90:  reads back on bitcode load

Backend accessors (CE8xxx): typed access

PTX emitter (sub_3022E70):
  .blocksareclusters
  .reqntid 256, 1, 1
  .reqnctapercluster 2, 1, 1
```

## Special Variables: threadIdx, blockIdx, blockDim, gridDim, warpSize

### Recognition Pipeline

CUDA built-in variables (`threadIdx`, `blockIdx`, `blockDim`, `gridDim`, `warpSize`) are not stored in memory --- they map directly to PTX special registers accessed via LLVM intrinsics. Two parallel codegen paths exist: an older one in the `0x920xxx` range and a newer one in the `0x1285xxx` range. Both share the same logic structure.

The classifier function `isSpecialRegisterVar` (`sub_920430` / `sub_127F7A0`) checks five preconditions before recognizing a variable:

1. **Inside kernel**: `(ctx->flags_at_360 & 1) != 0` --- only valid in `__global__` function context.
2. **Not extern**: `(sym->byte_89 & 1) == 0`.
3. **Not template-dependent**: `*(signed char*)(sym+169) >= 0`.
4. **Element count == 1**: `sym->elem_count_at_136 == 1`.
5. **Name non-null**: `sym->name_at_8 != NULL`.

If all pass, the name is compared via `strcmp` against the five known strings. The output category:

| Category | Name | Type |
|---|---|---|
| 0 | `threadIdx` | `dim3` (3-component struct) |
| 1 | `blockDim` | `dim3` |
| 2 | `blockIdx` | `dim3` |
| 3 | `gridDim` | `dim3` |
| 4 | `warpSize` | scalar `int` |

### Intrinsic ID Table

A static 2D array `int intrinsicIDs[5][3]` maps `(category, component)` to LLVM intrinsic IDs:

| CUDA Variable | .x | .y | .z |
|---|---|---|---|
| `threadIdx` | `@llvm.nvvm.read.ptx.sreg.tid.x` | `.tid.y` | `.tid.z` |
| `blockDim` | `@llvm.nvvm.read.ptx.sreg.ntid.x` | `.ntid.y` | `.ntid.z` |
| `blockIdx` | `@llvm.nvvm.read.ptx.sreg.ctaid.x` | `.ctaid.y` | `.ctaid.z` |
| `gridDim` | `@llvm.nvvm.read.ptx.sreg.nctaid.x` | `.nctaid.y` | `.nctaid.z` |
| `warpSize` | `@llvm.nvvm.read.ptx.sreg.warpsize` | --- | --- |

Each intrinsic is a zero-argument call returning `i32`. The old codegen path uses intrinsic ID 9374 for `warpSize`; the new path uses 4348.

### dim3 Member Access Codegen

Two functions handle the code generation, depending on whether the access is a full `dim3` struct or a single component:

**Full struct access** (`sub_922290` / `sub_1285550`): For `threadIdx` as a whole (all three components), loops 3 times:

```c++
for (component = 0; component < 3; component++) {
    intrinsicID = intrinsicIDs[category][component];
    decl = Module::getOrInsertIntrinsic(intrinsicID);
    callInst = CallInst::Create(decl);  // zero-arg, returns i32
    // Insert into struct via InsertValue
}
```

The three call results are composed into the struct type via `CreateInsertValue`. The IR value is named `"predef_tmp"`.

**Single component access** (`sub_9268C0` / `sub_1286E40`): For `threadIdx.x` specifically, the member name's first character is extracted from `member_symbol+56+8`:

- `'x'` (`0x78`) with null terminator `'\0'` at next byte -> component 0
- `'y'` (`0x79`) -> component 1
- `'z'` (`0x7A`) -> component 2

The null-terminator check prevents false matches on member names like `"xy"`. A single intrinsic call is emitted, named `"predef_tmp_comp"`:

```llvm
%predef_tmp_comp = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
```

Both paths compute alignment from the return type's bit-width via BSR and handle sign extension: if the type tag byte at `+140` satisfies `(tag & 0xFB) == 8` (signed int), the result is marked as signed.

### PTX Backend Mapping

The NVPTX backend (`sub_21E86B0`) maps internal register encodings (single-byte case labels using ASCII character codes) to PTX special register names:

| Code | ASCII | PTX Register |
|---|---|---|
| `0x26` | `&` | `%tid.x` |
| `0x27` | `'` | `%tid.y` |
| `0x28` | `(` | `%tid.z` |
| `0x29` | `)` | `%ntid.x` |
| `0x2A` | `*` | `%ntid.y` |
| `0x2B` | `+` | `%ntid.z` |
| `0x2C` | `,` | `%ctaid.x` |
| `0x2D` | `-` | `%ctaid.y` |
| `0x2E` | `.` | `%ctaid.z` |
| `0x2F` | `/` | `%nctaid.x` |
| `0x30` | `0` | `%nctaid.y` |
| `0x31` | `1` | `%nctaid.z` |

Codes `0x5E` (`^`) and `0x5F` (`_`) are delegated to `sub_3958DA0` for cluster and warp-level registers. Any unhandled code triggers a fatal `"Unhandled special register"` error. Register names are written via optimized `memcpy` of 6--9 bytes directly to the output stream.

### ISel Lowering

The instruction selector (`sub_36E4040`) validates that the intrinsic declaration returns `i32` (type code 7 at offset `+48` of the overload descriptor). If the type does not match, it emits a fatal error: `"Unsupported overloaded declaration of llvm.nvvm.read.sreg intrinsic"`. It then creates a `MachineSDNode` with NVPTX target opcode 3457.

### EDG Frontend Diagnostic

The EDG frontend includes a diagnostic at `sub_6A49A0` that detects writes to predefined read-only variables. When a store target matches any of the five built-in names, it emits diagnostic `0xDD0`:

```
error: cannot assign to variable 'threadIdx' with predefined meaning in CUDA
```

This diagnostic fires during semantic analysis, long before IR generation. It ensures that CUDA programs cannot accidentally (or intentionally) write to hardware register proxies.
