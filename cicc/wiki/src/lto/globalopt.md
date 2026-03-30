# GlobalOpt for GPU

CICC implements a custom GlobalOpt pass (`sub_18612A0`, 65 KB, 2179 decompiled lines) that replaces LLVM's stock `GlobalOptPass` with GPU-aware global variable transformations. The pass operates on NVIDIA's internal IR representation rather than LLVM IR directly, and adds address-space-aware logic that stock LLVM lacks entirely: it extracts the CUDA address space from the global's flags byte (`(flags >> 2) & 7`), preserves address space through all generated replacement globals, and applies promotion thresholds calibrated for GPU memory hierarchy. The pass runs at pipeline position 30 in the tier-2 and tier-3 optimization sequences (via wrapper `sub_196A2B0`), immediately after GlobalDCE / ConstantProp (`sub_1968390`) and before LoopVectorize. It runs at `-O2` and above; tier-1 does not include it. The inliner cost model at `sub_18612A0` also calls into GlobalOpt as a subroutine when evaluating whether a callee's globals can be folded after inlining, creating a tight coupling between inlining decisions and global optimization.

The pass implements four transformation strategies with decreasing priority: small-constant promotion for globals under 2047 bits, scalar replacement of aggregates (SRA) for struct globals with up to 16 fields, malloc/free elimination for heap-allocated globals with single-unit access, and a hash-table-driven deduplication cleanup pass. Each strategy preserves the original global's NVPTX address space, which is critical -- a `__device__` global in address space 1 must remain in AS 1 after splitting, not silently migrate to AS 0 (generic). The generated IR uses distinctive suffixes (`.body`, `.init`, `.val`, `.notinit`, `.f0`..`.f15`, `.isneg`, `.isnull`) that survive through to PTX emission and are visible in `cuobjdump` output.

| | |
|---|---|
| **Core transform** | `sub_18612A0` (`0x18612A0`, 65 KB, 2179 lines) |
| **Pipeline wrapper** | `sub_196A2B0` (`0x196A2B0`) |
| **Recursive re-application** | `sub_185B1D0` (`0x185B1D0`) |
| **Pre-SRA setup** | `sub_185B7E0` (`0x185B7E0`) |
| **Hash table rehash** | `sub_1860410` (`0x1860410`) |
| **Per-user SRA rewrite** | `sub_1860BE0` (`0x1860BE0`) |
| **Pipeline position** | Step 30 (tier 2/3), after GlobalDCE, before LoopVectorize |
| **Minimum opt level** | `-O2` (tier 2) |
| **Pass registration** | `"globalopt"` in pipeline parser at slot 45 |
| **IR node allocation** | 88 bytes per global, 64 bytes per basic block, 56 bytes per instruction |

## Address Space Handling

Every transformation in this pass must respect CUDA address spaces. The global's address space is extracted at line 577 of the decompilation:

```c
uint8_t addr_space = (*(uint8_t*)(global + 33) >> 2) & 7;
```

The NVPTX address spaces relevant here are 0 (generic), 1 (global/`__device__`), 3 (shared/`__shared__`), 4 (constant/`__constant__`), and 5 (local). See [Address Spaces](../reference/address-spaces.md) for the complete table with hardware mapping, pointer widths, and latency numbers.

When `sub_18612A0` creates replacement globals via `sub_15E51E0`, it passes the extracted address space to the constructor. The created global inherits the same address space, linkage (always internal, linkage code 7), and metadata (copied via `sub_15E6480`). This is the key delta from stock LLVM: upstream `GlobalOpt` does not consider address space when splitting globals because host-side address spaces are trivial. On GPU, promoting a `__shared__` struct global to per-field `__shared__` globals preserves the 10x latency advantage over DRAM, while accidentally demoting to generic would force the hardware to resolve address space at runtime via the generic-to-specific address resolution unit.

## Entry Guard: Type Filtering

Before attempting any transformation, the pass filters on the global's type tag (byte at `type + 8`). The acceptance bitmask is `0x8A7E`:

```c
// Bits set: 1,2,3,4,5,9,11,13,15
uint16_t bitmask = 0x8A7E;
if ((1 << type_tag) & bitmask) {
    // accepted: i16, i32, i64, i80, float, double, arbitrary-int, struct, opaque-ptr
}
```

Additionally, struct (tag 13), vector (tag 14), and array (tag 16) types are accepted if `sub_16435F0(type, 0)` returns true -- this is the `isAnalyzableType` predicate that recursively checks whether the type's leaf elements are all scalars or pointers.

After type filtering, the pass walks the global's use-list. Every user must be either a store (opcode tag 54) or a load (opcode tag 55). If any user is an arithmetic instruction (tag <= 23), a GEP used in a non-trivial way, or any other instruction kind, the global is rejected -- it cannot be optimized because its address escapes or is used in a way the pass cannot model.

## Path A: Small-Constant Promotion

When the global's initializer is a struct constant and its total bit-size (including alignment padding) fits within 2047 bits (0x7FF), the pass promotes it into a function-local value with a separate initializer function. This threshold is NVIDIA-specific -- upstream LLVM uses different heuristics based on `TargetData` layout considerations. The 2047-bit ceiling corresponds roughly to 64 32-bit registers, aligning with the per-thread register budget on most SM architectures where promoting beyond that limit would spill to local memory and negate the benefit.

### Size Computation

The pass walks the type tree recursively to compute total bit-size. The implementation at lines 499-570 of the decompilation uses a `switch` on the type tag byte at `type + 8`:

| Type tag | Type | Bits |
|----------|------|------|
| 0x1 | i16 / half | 16 |
| 0x2 | i32 / float | 32 |
| 0x3 | i64 | 64 |
| 0x4 | x86_fp80 | 80 |
| 0x5 | i128 | 128 |
| 0x6 | fp128 / ppc_fp128 | 128 |
| 0x7 | pointer | `sub_15A9520(target, 0) * 8` |
| 0x9 | double | 64 |
| 0xB | iN (custom width) | from type word >> 8 |
| 0xD | struct | 8 * field_count (via `sub_15A9930`) |
| 0xE | vector | 8 * alignment * num_elements * padded_size |
| 0xF | opaque ptr | `sub_15A9520(target, addr_space) * 8` |
| 0x0, 0x8, 0xA, 0xC, 0x10 | array variants | element_size * array_length (recursive) |

Note that opaque pointers (tag 0xF) use `getPointerSizeInBits(target, addr_space)` -- the pointer size varies by address space on NVPTX (64-bit for AS 0/1, potentially 32-bit for AS 3/5 on some targets). Tags 0x0, 0x8 (label/token), 0xA (metadata), and 0xC (bfloat) all fall into the array-multiplier path -- they extract an element count and recurse, which handles the case where these type wrappers contain inner array types.

The pseudocode for the size computation:

```c
// sub_18612A0, lines 499-570
uint64_t compute_total_bits(Type *type, TargetInfo *target, uint8_t addr_space) {
    uint8_t tag = *(uint8_t *)(type + 8);
    switch (tag) {
    case 0x1:  return 16;                                     // i16 / half
    case 0x2:  return 32;                                     // i32 / float
    case 0x3:  return 64;                                     // i64
    case 0x4:  return 80;                                     // x86_fp80
    case 0x5:  return 128;                                    // i128
    case 0x6:  return 128;                                    // fp128 / ppc_fp128
    case 0x7:  return sub_15A9520(target, 0) * 8;             // generic pointer
    case 0x9:  return 64;                                     // double
    case 0xB:  return *(uint32_t *)(type + 8) >> 8;           // iN custom-width
    case 0xD: {                                               // struct
        uint64_t layout = sub_15A9930(target, type);          // getStructLayout
        return 8 * *(uint32_t *)(layout + 12);                // 8 * element_count
    }
    case 0xE: {                                               // vector
        uint64_t align = sub_15A9FE0(target, type);           // getAlignment
        uint64_t n_elts = *(uint32_t *)(type + 12);
        uint64_t elem_bits = compute_total_bits(
            sub_16463B0(type, 0), target, addr_space);        // getArrayElementType
        return 8 * align * n_elts * ((elem_bits + align - 1) / align);
    }
    case 0xF:  return sub_15A9520(target, addr_space) * 8;    // opaque ptr (AS-aware)
    default: {                                                 // 0x0,0x8,0xA,0xC,0x10: array
        uint64_t n_elts = *(uint32_t *)(type + 12);
        Type *elem = sub_16463B0(type, 0);                    // getArrayElementType
        return n_elts * compute_total_bits(elem, target, addr_space);
    }
    }
}
```

The acceptance check at line 570:

```c
if (total_elements * alignment * ceil_div(total_bits, alignment) > 0x7FF)
    goto path_b;  // too large, try SRA instead
```

### Generated IR Pattern

For a qualifying global, the pass generates three components:

```llvm
; Original: @my_global = addrspace(1) global { i32, i32 } { i32 42, i32 7 }

; After promotion:
@my_global.body = internal addrspace(1) global { i32, i32 } { i32 42, i32 7 }

define internal void @my_global.init() {
  store { i32, i32 } { i32 42, i32 7 }, ptr addrspace(1) @my_global.body
  ret void
}

; All loads of @my_global replaced with: load ptr addrspace(1) @my_global.body
; ExtractValue users get ".val" accessors
; Uninitialized code paths get "notinit" sentinel via sub_15FB630
```

The `.body` global is created via `sub_15E51E0` with the same address space and internal linkage (code 7). The `.init` function is created via `sub_15E5070`. The pass then walks all users of the original global: loads (tag 55) get redirected to the `.body` global, GEPs (tag 71) get RAUW'd via `sub_164D160`, and `extractvalue` instructions (tag 75) get specialized `.val` accessors. Sub-opcodes on the `extractvalue` determine further handling: codes 0x20/0x25/0x29 produce `notinit` sentinels, 0x24/0x28 extract terminal types via `sub_159C540`, and 0x21-0x23/0x26-0x27 pass through unchanged.

The full promotion pseudocode covering body creation, init creation, and use rewriting:

```c
// sub_18612A0, lines 577-805 — Path A: small-constant promotion
void promote_small_constant(Global *global, Module *module, Value *init_val,
                            Type *type, TargetInfo *target) {
    // --- Extract address space from global flags ---
    uint8_t addr_space = (*(uint8_t *)(global + 33) >> 2) & 7;

    // --- Create ".body" global in same address space ---
    void *node = sub_1648A60(88, 1);                           // IRBuilder::create
    Global *body_gv = sub_15E51E0(
        get_scope(module), type, /*init=*/0, /*linkage=*/7,
        concat_name(global, ".body"), addr_space);             // createGlobalVar
    sub_15E6480(global, body_gv);                              // copyMetadata

    // --- Rewrite all users of original global ---
    Use *use = *(Use **)(global + 8);                          // use-list head
    while (use != NULL) {
        Instruction *inst = sub_1648700(use);                  // getInstruction
        uint8_t opcode = *(uint8_t *)(inst + 16);

        if (opcode == 71) {                                    // GEP
            // If GEP references old global, RAUW to body
            sub_164D160(inst, body_gv);                        // RAUW
            sub_15F20C0(inst);                                 // eraseFromParent
        } else {
            // Create local variable referencing body
            Value *local = sub_15FD590(inst, get_scope(module),
                                       "newgv", module);       // createLocalVar
            sub_1648780(use, local);                           // replaceUseWith
        }
        use = *(Use **)(use + 8);                              // next use
    }

    // --- Create ".init" function ---
    Function *init_fn = sub_15E5070(
        get_scope(module), type, /*linkage=*/7,
        init_val, concat_name(global, ".init"));               // createFunction
    int init_user_count = 0;

    // Walk users again for extractvalue and load rewriting
    use = *(Use **)(body_gv + 8);
    while (use != NULL) {
        Instruction *inst = sub_1648700(use);
        uint8_t opcode = *(uint8_t *)(inst + 16);

        if (opcode == 55) {                                    // load
            sub_15F9480(init_val, init_fn);                    // createStoreInit
            init_user_count++;
        } else if (opcode == 75) {                             // extractvalue
            Value *val_acc = sub_15F8F80(inst, type, init_fn,
                concat_name(global, ".val"));                  // createExtractValue
            uint8_t sub_opcode = *(uint8_t *)(inst + 24);
            switch (sub_opcode) {
            case 0x20: case 0x25: case 0x29:
                // Uninitialized path: create "notinit" sentinel
                sub_15FB630(val_acc, "notinit", inst);         // createNotInit
                break;
            case 0x24: case 0x28:
                // Terminal type extraction
                sub_159C540(val_acc);                          // getTerminalType
                break;
            default:                                           // 0x21-0x23, 0x26-0x27
                break;                                         // pass-through
            }
            sub_164D160(inst, val_acc);                        // RAUW
            sub_15F20C0(inst);                                 // eraseFromParent
            init_user_count++;
        }
        use = *(Use **)(use + 8);
    }

    // --- Finalize ---
    if (init_user_count > 0) {
        sub_1631BE0(module_fn_list, init_fn);                  // insertIntoFnList
        // Patch metadata chain at global+56
        *(void **)(global + 56) = init_fn;
    } else {
        // Dead init function: destroy
        sub_15E5530(init_fn);                                  // destroyFunctionBody
        sub_159D9E0(init_fn);                                  // destroyFunction
        sub_164BE60(init_fn);                                  // dropAllReferences
        sub_1648B90(init_fn);                                  // markDead (flags |= 1)
    }

    sub_15E55B0(global);                                       // erase original global
    sub_15F20C0(module_entry);                                 // erase module-level ref

    // --- Recursive re-application to newly created .body ---
    sub_185B1D0(body_gv, target);                              // recursiveGlobalOpt
}
```

After rewriting all uses, if the `.init` function has users, it is linked into the module's function list via `sub_1631BE0`. If it has zero users (the initializer was never needed), the function body is destroyed and marked dead. The original global is erased via `sub_15E55B0`. Finally, `sub_185B1D0` recursively re-applies GlobalOpt to the newly created `.body` global, enabling cascaded optimizations.

## Path B: Scalar Replacement of Aggregates (SRA)

When a global is too large for constant promotion, the pass attempts SRA -- exploding a struct global into per-field scalar globals. This path has stricter preconditions:

1. The caller's `flag` parameter (a4) must be zero -- when set, SRA is disabled.
2. The initializer must be the unique initializer for this global (verified via `sub_15A0680`).
3. The type must be a struct (tag 13) with 1 to 16 fields: `field_count - 1 <= 0xF`.
4. Every user must reference only this global -- no cross-global pointer arithmetic.

The 16-field limit is a hardcoded constant at line 822 of the decompilation. It prevents combinatorial explosion in the null-check and free chains that follow: each field generates one `icmp eq` (null check), one `or`, one conditional branch, one `free_it` block, and one `next` block. Beyond 16 fields the cost of the generated guard code would exceed the benefit of splitting.

### Use Analysis: Store Value Collection

Before field explosion, the pass collects all stored values into a hash set to determine which initializers are live. For each store (tag 54) user of the global, `sub_185CAF0` inserts the stored value into a hash/set structure at `v432`. The scratch buffer starts with capacity 32 and grows via `sub_16CC920` when full. This collection serves two purposes: it validates that all stores write analyzable values (no opaque function pointers or computed addresses), and it builds the value set used later to initialize the per-field globals.

```c
// sub_18612A0, lines 823-868 — Store value collection for SRA
void collect_store_values(Global *global, Module *module,
                          HashSet *store_set, Buffer *scratch) {
    Use *use = *(Use **)(global + 8);
    int store_count = 0;

    while (use != NULL) {
        Instruction *inst = sub_1648700(use);                  // getInstruction
        uint8_t opcode = *(uint8_t *)(inst + 16);

        if (opcode == 54) {                                    // store
            sub_185CAF0(use, store_set, scratch);              // collectStoredValue
            store_count++;

            // Grow scratch if full
            if (scratch->size >= scratch->capacity) {
                if (scratch->capacity < 64)
                    memset(scratch->data, 0xFF, scratch->capacity * 8);
                else
                    sub_16CC920(scratch);                      // growScratchBuffer
            }
        }
        use = *(Use **)(use + 8);
    }
}
```

### Global-Only-Use Validation

After collection, lines 878-1017 validate that every user of every collected global references **only** the target global -- no cross-global pointer arithmetic is allowed. The validation walks the use chain of each collected global. For each operand slot (24-byte stride, count from `*(uint32_t *)(global + 20) & 0xFFFFFFF`):

- If the operand is the module itself: accepted.
- If the opcode tag is <= 0x17 (arithmetic/comparison): rejected -- the global's address is used in computation.
- If the opcode is 77 (GEP): the pass calls `sub_16CC9F0` (find in sorted set) to verify the GEP's base pointer is the same global being split.
- If the opcode is 54 (store): the pass checks that the store's parent basic block (at offset -24 from the operand) belongs to the global being analyzed.

If any operand fails validation, a flag `v17` is set to zero and the entire SRA path is abandoned for this global.

### Field Explosion

For each field index 0 through `field_count - 1`, the pass creates a replacement global variable in the same address space with internal linkage. The full pseudocode at lines 1084-1476:

```c
// sub_18612A0, lines 1084-1476 — SRA field explosion
typedef struct {
    Global **data;
    uint64_t size;
    uint64_t capacity;
} FieldVec;

void sra_explode_fields(Global *global, Module *module, Type *struct_type,
                        Value *init_val, TargetInfo *target, FieldVec *fields) {
    uint8_t addr_space = (*(uint8_t *)(global + 33) >> 2) & 7;
    const char *global_name = sub_1649960(global);             // getName
    uint32_t field_count = *(uint32_t *)(struct_type + 12);
    uint64_t ptr_bits = sub_15A9520(target, addr_space);       // getPointerSizeInBits

    for (uint32_t i = 0; i < field_count; i++) {
        // --- Extract field type and offset ---
        Type *field_type = sub_1646BA0(struct_type, ptr_bits); // getStructFieldType
        uint64_t field_offset = sub_15A06D0(struct_type, i);   // computeFieldOffset

        // --- Generate name: "my_global.f0", "my_global.f1", ... ---
        char name[256];
        snprintf(name, sizeof(name), "%s.f%d", global_name, i);

        // --- Extract field initializer from parent init ---
        Value *field_init = sub_15FEBE0(module, init_val, field_type); // createBitcast/GEP

        // --- Create field global in same address space, internal linkage ---
        Global *field_gv = sub_15E51E0(
            get_scope(module), field_type, field_init,
            /*linkage=*/7, name, addr_space);                  // createGlobalVar

        // --- Copy metadata from parent to field global ---
        sub_15E6480(global, field_gv);                         // copyMetadata

        // --- Store into dynamically-grown field vector ---
        if (fields->size >= fields->capacity) {
            // Realloc growth: double capacity (lines 1161-1220)
            uint64_t new_cap = fields->capacity * 2;
            if (new_cap < 8) new_cap = 8;
            fields->data = realloc(fields->data, new_cap * sizeof(Global *));
            fields->capacity = new_cap;
        }
        fields->data[fields->size++] = field_gv;

        // --- Compute field bit-size (same type switch as Path A) ---
        uint64_t field_bits = compute_total_bits(field_type, target, addr_space);
        uint64_t alignment;
        if (*(uint8_t *)(field_type + 8) == 0xD) {            // struct
            uint64_t layout = sub_15A9930(target, field_type);
            alignment = *(uint64_t *)(layout + 8);
        } else {
            alignment = sub_15A9FE0(target, field_type);       // getAlignment
        }
        uint64_t padded = alignment * ((field_bits + alignment - 1) / alignment);

        // --- Create GEP replacement and store initializer ---
        Value *gep = sub_15FEBE0(module, field_gv, field_type); // createBitcast/GEP
        sub_15F9660(field_offset, field_gv, global);            // createFieldStore
    }
}
```

The field globals are stored in a dynamically-grown `std::vector` with realloc growth strategy (lines 1161-1220 of the decompilation). The growth factor is 2x with a minimum initial capacity of 8 entries.

### Null/Negative Guards

After field explosion, the pass generates safety checks for the original global's pointer value. This pattern handles the case where the global was heap-allocated via malloc -- the original pointer might be null or negative (indicating allocation failure on some platforms). The guard chain is constructed at lines 1478-1535:

```c
// sub_18612A0, lines 1478-1535 — Null/negative guard chain generation
Value *build_guard_chain(Global *global, FieldVec *fields,
                         Module *module, TargetInfo *target) {
    // --- Create %isneg = icmp slt <ptr>, 0 ---
    // Opcode 51 = ICmp, predicate 40 = SLT (signed less than zero)
    Value *isneg = sub_15FEC10(
        /*dest=*/NULL, /*type_id=*/1, /*opcode=*/51, /*pred=*/40,
        get_module_sym(module), /*offset=*/0,
        concat_name(global, ".isneg"), get_current_bb(module)); // createICmp

    Value *chain = isneg;

    // --- For each field: %isnullI = icmp eq <field_ptr>, null ---
    for (uint64_t i = 0; i < fields->size; i++) {
        Global *field_gv = fields->data[i];
        uint64_t field_offset = sub_15A06D0(
            get_type(global), i);                              // computeFieldOffset

        // Predicate 32 = EQ (equal to null)
        Value *isnull = sub_15FEC10(
            /*dest=*/NULL, /*type_id=*/1, /*opcode=*/51, /*pred=*/32,
            field_gv, field_offset,
            concat_name(global, ".isnull"), get_current_bb(module));

        // Chain with OR: %tmpI = or i1 %chain, %isnullI
        // Opcode 27 = OR
        char tmp_name[16];
        snprintf(tmp_name, sizeof(tmp_name), "tmp%lu", i);
        chain = sub_15FB440(/*opcode=*/27, chain, isnull,
                            tmp_name, module);                 // createBinOp(OR)
    }

    return chain;  // final chained predicate
}
```

The generated IR for a 3-field struct:

```llvm
%isneg  = icmp slt ptr @original_global, null    ; predicate 40 = SLT
%isnull0 = icmp eq ptr @my_global.f0, null        ; predicate 32 = EQ
%tmp0   = or i1 %isneg, %isnull0
%isnull1 = icmp eq ptr @my_global.f1, null
%tmp1   = or i1 %tmp0, %isnull1
%isnull2 = icmp eq ptr @my_global.f2, null
%tmp2   = or i1 %tmp1, %isnull2
br i1 %tmp2, label %malloc_ret_null, label %malloc_cont
```

The `.isneg` guard is created by `sub_15FEC10` with opcode 51 (ICmp), predicate 40 (SLT with zero). Per-field `.isnull` guards use predicate 32 (EQ with null). The guards are chained with OR instructions (opcode 27) via `sub_15FB440`. The chain evaluation is linear in the number of fields -- for the maximum 16 fields, this produces 17 `icmp` instructions and 16 `or` instructions, plus one terminal conditional branch.

### Malloc/Free Decomposition Algorithm

This is the core of NVIDIA's per-field malloc/free elimination, covering lines 1537-1640 of the decompilation. When the chained null check indicates a valid allocation, the pass generates a multi-block control flow that replaces the original single malloc/free pair with per-field conditional frees. This is the key divergence from upstream LLVM: stock `tryToOptimizeStoreOfMallocToGlobal` treats the malloc/free as an atomic pair, replacing it with a single static allocation. NVIDIA decomposes to per-field granularity, generating N+2 basic blocks (one `malloc_ret_null`, one `malloc_cont`, and for each field one `free_it` plus one `next` block).

The complete pseudocode:

```c
// sub_18612A0, lines 1537-1640 — Malloc/free decomposition
void decompose_malloc_free(Global *global, Module *module, Function *fn,
                           FieldVec *fields, Value *guard_chain,
                           TargetInfo *target) {
    uint8_t addr_space = (*(uint8_t *)(global + 33) >> 2) & 7;

    // === Step 1: Create control flow skeleton ===

    // "malloc_cont" — continuation after successful allocation check
    BasicBlock *malloc_cont_bb = sub_157FBF0(
        fn, get_global_chain(module), "malloc_cont");          // createBB

    // "malloc_ret_null" — failure path returning null
    BasicBlock *ret_null_body = sub_157E9C0(fn);               // createReturnBB
    BasicBlock *malloc_ret_null_bb = sub_157FB60(
        NULL, ret_null_body, "malloc_ret_null", NULL);         // createBBWithPred

    // === Step 2: Emit conditional branch on guard chain ===
    // br i1 %guard_chain, label %malloc_ret_null, label %malloc_cont
    sub_15F8650(
        get_terminator(fn),                                    // insertion point
        malloc_ret_null_bb,                                    // true target (fail)
        malloc_cont_bb,                                        // false target (success)
        guard_chain,                                           // condition (isneg|isnull)
        fn);                                                   // createCondBr

    // === Step 3: Per-field conditional free and reinitialization ===
    BasicBlock *current_bb = malloc_cont_bb;

    for (uint64_t i = 0; i < fields->size; i++) {
        Global *field_gv = fields->data[i];
        uint64_t field_offset = sub_15A06D0(get_type(global), i);
        Type *field_type = sub_1646BA0(get_type(global),
                                       sub_15A9520(target, addr_space));

        // 3a. Create "tmp" alloca in current block
        Value *tmp_alloca = sub_15F9330(
            NULL, field_type, "tmp", current_bb);              // createAlloca

        // 3b. Create non-null check: %condI = icmp ne <field_ptr>, null
        // Opcode 51 = ICmp, predicate 33 = NE (not equal to null)
        char cond_name[64];
        snprintf(cond_name, sizeof(cond_name), "%s.f%lu.nonnull",
                 sub_1649960(global), i);
        Value *cond = sub_15FED60(
            NULL, /*type_id=*/1, /*opcode=*/51, /*pred=*/33,
            field_gv, field_offset, cond_name, current_bb);    // createICmpNE

        // 3c. Create "free_it" block — frees this field if non-null
        char free_name[64];
        snprintf(free_name, sizeof(free_name), "free_it%lu", i);
        BasicBlock *free_it_bb = sub_157FB60(
            NULL, NULL, free_name, NULL);                      // createBBWithPred

        // 3d. Create "next" block — fallthrough after conditional free
        char next_name[64];
        snprintf(next_name, sizeof(next_name), "next%lu", i);
        BasicBlock *next_bb = sub_157FB60(
            NULL, NULL, next_name, NULL);                      // createBBWithPred

        // 3e. Conditional branch: non-null → free, null → skip
        // br i1 %condI, label %free_itI, label %nextI
        sub_15F8650(
            get_terminator_of(current_bb),
            free_it_bb,                                        // true: free
            next_bb,                                           // false: skip
            cond, fn);                                         // createCondBr

        // 3f. In free_it block: wire field into use-def chain, then branch to next
        sub_15FDB00(field_gv, get_use_chain(free_it_bb),
                    i, free_it_bb);                            // wireDef

        // Unconditional branch: free_it → next
        sub_15F8590(NULL, next_bb, free_it_bb);                // createBr

        // 3g. In next block: store field initializer into the new field global
        sub_15F9850(field_offset, tmp_alloca, next_bb);        // createStoreToField

        current_bb = next_bb;
    }

    // === Step 4: Wire entry into malloc_cont, erase original ===
    // Unconditional branch from entry into malloc_cont
    sub_15F8590(NULL, malloc_cont_bb, get_entry_bb(fn));       // createBr

    // Erase the original global
    sub_15F20C0(get_module_entry(module));                      // eraseFromParent
}
```

The generated CFG for a 2-field struct `{ i32, float }`:

```
entry:
  br i1 %tmp1, label %malloc_ret_null, label %malloc_cont

malloc_ret_null:
  ret null

malloc_cont:
  %cond0 = icmp ne ptr @g.f0, null
  br i1 %cond0, label %free_it0, label %next0

free_it0:
  ; free(@g.f0)   — conditional per-field deallocation
  br label %next0

next0:
  store i32 <init0>, ptr addrspace(1) @g.f0
  %cond1 = icmp ne ptr @g.f1, null
  br i1 %cond1, label %free_it1, label %next1

free_it1:
  ; free(@g.f1)
  br label %next1

next1:
  store float <init1>, ptr addrspace(1) @g.f1
  ; ... continuation
```

Each `free_it` block is conditionally entered only when the field pointer is non-null, preventing double-free on fields that were never successfully allocated. The `next` blocks store the field initializer after the conditional free, ensuring the field global is properly initialized regardless of whether freeing occurred. This per-field decomposition enables a critical optimization that upstream LLVM cannot perform: if a later pass (dead store elimination, constant propagation) determines that only some fields of the struct are actually used, the unused field globals and their associated `free_it`/`next` blocks become dead code and are trivially eliminated by GlobalDCE.

### Address-Space-Aware Splitting

The address space preservation logic is woven throughout both the field explosion and the malloc/free decomposition. Every call to `sub_15E51E0` (createGlobalVar) passes the extracted address space from the parent global. The extraction point is always the same: `(*(uint8_t *)(global + 33) >> 2) & 7`. This is critical for three reasons:

1. **Shared memory splitting**: A `__shared__` struct global (AS 3) split into per-field globals must keep each field in AS 3. If any field migrated to AS 0 (generic), the hardware would resolve the address at runtime via the generic-to-specific resolution unit, adding 10-20 cycles of latency per access and defeating the purpose of placing data in shared memory.

2. **Constant memory splitting**: A `__constant__` struct (AS 4) split into fields must remain in AS 4 to benefit from the constant cache's broadcast capability. A single warp reading the same constant field hits the cache once and broadcasts to all 32 threads. In AS 0 (generic), this broadcast would not occur.

3. **Pointer size consistency**: On some NVPTX targets, pointers in AS 3 (shared) and AS 5 (local) are 32-bit, while AS 0 and AS 1 pointers are 64-bit. The size computation for opaque pointers (tag 0xF) calls `sub_15A9520(target, addr_space)` -- if the address space were lost during splitting, the pointer size calculation would be wrong, producing incorrect field offsets and corrupted stores.

The per-field null checks in the guard chain also respect address space: the `icmp eq` with null uses a null pointer of the correct address space width. A 32-bit null in AS 3 is not the same bit pattern as a 64-bit null in AS 1.

### Hash Table for Processed Globals

After field explosion and malloc rewrite, the pass uses a custom hash table (open addressing, 32-byte entries) to track which globals and their transitive users have been processed. This is an instance of the NVIDIA-original hash table variant (sentinel pair -8/-16) as documented in the [hash infrastructure](../infra/hash-infrastructure.md) page.

| Offset | Field | Description |
|--------|-------|-------------|
| +0 | key | Pointer to global (sentinel: -8 = empty, -16 = tombstone) |
| +8 | data | Pointer to field-global vector |
| +16 | size | Current vector size |
| +24 | cap | Vector capacity |

Hash function, quadratic probing with triangular numbers, and 75% load factor / 12.5% tombstone compaction thresholds all follow the standard DenseMap infrastructure; see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for details.

The processing loop (lines 1710-1812) iterates remaining users of the original global and rewrites them to reference the new field globals:

```c
// sub_18612A0, lines 1710-1812 — Post-SRA user rewriting via hash table
void rewrite_remaining_users(Global *global, FieldVec *fields,
                             HashTable *table, Module *module,
                             TargetInfo *target) {
    Use *use = *(Use **)(global + 8);

    while (use != NULL) {
        Use *next_use = *(Use **)(use + 8);
        Instruction *inst = sub_1648700(use);
        uint8_t opcode = *(uint8_t *)(inst + 16);

        if (opcode == 54) {                                    // store
            // Walk the store's own use-chain
            Use *store_use = *(Use **)(inst + 8);
            while (store_use != NULL) {
                Use *next_su = *(Use **)(store_use + 8);

                // Per-user SRA rewrite: replaces GEP+store/load sequences
                // with direct accesses to the appropriate field global
                sub_1860BE0(store_use, table, fields, target); // rewriteUserForSRA

                store_use = next_su;
            }

            // If store has no remaining uses, erase it
            if (*(Use **)(inst + 8) == NULL) {
                sub_15F20C0(inst);                             // eraseFromParent
                // Remove from hash table (mark as tombstone)
                HashEntry *entry = sub_1860630(
                    inst, 0, table, NULL);                     // lookupInTable
                if (entry != NULL)
                    entry->key = (void *)(-16);                // tombstone
            }
        } else {
            // For non-store users (loads, etc.): create direct stores
            // to the appropriate field global
            for (uint64_t i = 0; i < fields->size; i++) {
                uint64_t offset = sub_15A06D0(
                    get_type(global), i);                      // computeFieldOffset
                sub_15F9660(offset, fields->data[i], inst);    // createFieldStore
            }
        }

        use = next_use;
    }
}
```

After all users are rewritten, cleanup proceeds in two phases: first, operand lists of dead GEP (tag 77) and store (tag 54) instructions are unlinked from the use chain (nulling out 24-byte-stride operand slots at lines 2004-2079); second, the dead instructions are erased via `sub_15F20C0` at lines 2081-2117. Finally, the original global declaration is erased via `sub_15E55B0`, and all temporary data structures (hash table backing array, field vectors, scratch buffers) are freed at lines 2119-2161.

## Top-Level Driver: sub_18612A0

The complete control flow of the core transform function, integrating all four strategies. This pseudocode corresponds to the entire 2179-line decompilation:

```c
// sub_18612A0 — Core GlobalOpt transform for a single global variable
// Returns: 1 if transformed, 0 if no transformation applied
int globalopt_transform(Global *global, Module *module, Type *type,
                        int flag, TargetInfo *target, TargetInfo *target2) {
    // === Phase 1: Type filter (lines 444-451) ===
    uint8_t type_tag = *(uint8_t *)(type + 8);
    uint16_t bitmask = 0x8A7E;  // bits: 1,2,3,4,5,9,11,13,15
    if (!((1 << type_tag) & bitmask)) {
        // Additional acceptance for struct(13), vector(14), array(16)
        if (type_tag == 13 || type_tag == 14 || type_tag == 16) {
            if (!sub_16435F0(type, 0))                         // isAnalyzableType
                return 0;
        } else {
            return 0;
        }
    }

    // === Phase 2: Use validation — all users must be store/load (lines 452-481) ===
    Buffer scratch = { .data = alloca(8 * sizeof(void *)), .size = 0, .capacity = 8 };
    Use *use = *(Use **)(global + 8);
    while (use != NULL) {
        Instruction *inst = sub_1648700(use);                  // getInstruction
        uint8_t opcode = *(uint8_t *)(inst + 16);
        if (opcode <= 0x17) return 0;                          // arithmetic: reject
        if (opcode == 54) {                                    // store
            if (!sub_185C920(inst, &scratch))                  // analyzeStore
                return 0;
        } else if (opcode != 55) {                             // not load either
            return 0;
        }
        use = *(Use **)(use + 8);
    }

    // === Phase 3: Collect store values and evaluate initializer (lines 482-493) ===
    Buffer store_buf = { .data = calloc(32, sizeof(void *)), .size = 0, .capacity = 32 };
    sub_185C560(module, global, &store_buf);                   // collectStoreValues
    Value *init_val = sub_140B2F0(module, target, global, 1);  // evaluateInitializer

    // === Phase 4: Try Path A — small-constant promotion (lines 494-805) ===
    uint8_t init_tag = *(uint8_t *)(init_val + 16);
    if (init_tag == 13) {                                      // struct constant
        uint8_t addr_space = (*(uint8_t *)(global + 33) >> 2) & 7;
        uint64_t total_bits = compute_total_bits(type, target, addr_space);
        uint64_t alignment = sub_15A9FE0(target, type);
        uint64_t padded = alignment * ((total_bits + alignment - 1) / alignment);

        if (padded <= 0x7FF) {                                 // <= 2047 bits
            promote_small_constant(global, module, init_val, type, target);
            free(store_buf.data);
            return 1;
        }
    }

    // === Phase 5: Try Path B — SRA of struct globals (lines 807-2177) ===
    if (flag != 0) { free(store_buf.data); return 0; }        // SRA disabled by caller

    // Verify unique initializer
    if (init_val != sub_15A0680(get_module_sym(module), 1, 0)) {
        free(store_buf.data); return 0;
    }

    // Check struct with 1-16 fields
    if (type_tag == 14) type = unwrap_vector(type);            // vector peeling
    if (*(uint8_t *)(type + 8) != 13) { free(store_buf.data); return 0; }
    uint32_t field_count = *(uint32_t *)(type + 12);
    if (field_count - 1 > 0xF) { free(store_buf.data); return 0; }  // > 16 fields

    // Collect stored values into hash set (lines 823-868)
    HashSet store_set;
    init_hashset(&store_set);
    collect_store_values(global, module, &store_set, &scratch);

    // Validate all users reference only this global (lines 878-1017)
    if (!validate_global_only_uses(global, &store_set)) {
        free(store_buf.data); return 0;
    }

    // Optional vector type peeling (lines 1026-1083)
    if (*(uint8_t *)(type + 8) == 14) {
        peel_vector_type(global, module, type, target);
    }

    // Field explosion (lines 1084-1476)
    FieldVec fields = { .data = NULL, .size = 0, .capacity = 0 };
    sra_explode_fields(global, module, type, init_val, target, &fields);

    // Null/negative guard chain (lines 1478-1535)
    Value *guard = build_guard_chain(global, &fields, module, target);

    // Malloc/free decomposition (lines 1537-1640)
    Function *fn = get_parent_function(global);
    decompose_malloc_free(global, module, fn, &fields, guard, target);

    // Hash-table-driven user rewriting (lines 1642-2161)
    HashTable processed;
    init_hashtable(&processed);
    rewrite_remaining_users(global, &fields, &processed, module, target);

    // Cleanup: unlink dead operands, erase dead instructions
    cleanup_dead_instructions(&processed);                     // lines 2004-2117

    // Erase original global and free temporaries
    sub_15E55B0(global);                                       // lines 2119-2161
    free(fields.data);
    free(store_buf.data);
    destroy_hashtable(&processed);
    destroy_hashset(&store_set);

    return 1;
}
```

## LTO Interaction

GlobalOpt benefits significantly from LTO's whole-program visibility. In single-compilation mode, a `__device__` global with external linkage cannot be optimized because the compiler cannot prove it is unused by other translation units. With ThinLTO, the [NVModuleSummary](./module-summary.md) builder records per-global reference edges, and the [ThinLTO importer](./thinlto-import.md) pulls definitions across module boundaries. After import, GlobalOpt can see all users of a global across the entire program and make decisions that are impossible in per-module compilation:

- **Internalization**: A global referenced only within one module (after import) can be marked internal (linkage 7), enabling all four transformation paths.
- **Dead global elimination**: A global with zero users after import is trivially dead and erased. The [NVModuleSummary](./module-summary.md) builder's address-space tracking ensures that `__device__` globals referenced by kernels are not prematurely killed -- a kernel's reference counts as a use even when no host-side code touches the global.
- **Cross-module constant propagation**: After import, if a `__device__` global is stored exactly once (from a host-side `cudaMemcpyToSymbol`) and loaded many times across multiple device functions, the single-store can be propagated as a constant, unlocking Path A's small-constant promotion.

The pass wrapper `sub_196A2B0` is also called from the inliner cost model (`sub_18612A0` address shared by both -- the inliner calls the GlobalOpt transform function to evaluate whether post-inline global folding would pay for the inline cost). This creates a feedback loop: inlining a caller that references a global may expose the global for optimization, which reduces code size, which makes further inlining cheaper.

## Recursion

After completing either Path A or Path B, the pass recursively calls `sub_185B1D0` on the newly created replacement globals. This handles cascading opportunities: splitting a struct global into fields may expose one of the field globals for further small-constant promotion (if a field is a small struct itself), or for dead elimination (if one field is never used). The recursion terminates when no further transformations apply -- each recursive call runs the same type filter and use validation, so it will return 0 for leaf scalars or globals with non-store/load users.

## Knobs and Thresholds

| Threshold | Value | Source | Effect |
|-----------|-------|--------|--------|
| Max bits for Path A | 2047 (0x7FF) | Hardcoded | Globals exceeding this fall through to SRA |
| Max struct fields for SRA | 16 | Hardcoded | Structs with >16 fields are not split |
| Hash table load factor | 75% (3/4) | Hardcoded | Triggers rehash of processed-globals table |
| Tombstone threshold | 12.5% (1/8) | Hardcoded | Triggers compacting rehash |
| Initial scratch buffer | 8 entries | Hardcoded | For use analysis; grows via `sub_16CC920` |
| Store collection buffer | 32 entries | Hardcoded | For store value collection; grows dynamically |
| SRA disable flag (a4) | Caller-set | Runtime | When set, Path B is bypassed entirely |
| Pipeline gate | opts[1440] | Config array | When set, the `sub_196A2B0` wrapper is skipped |
| Optimization tier | >= 2 | Pipeline config | GlobalOpt not run at tier 1 |

The pipeline parser registers `"globalopt"` at slot 45 in the pass name table, mapping to `llvm::GlobalOptPass`. The NVIDIA wrapper `sub_196A2B0` is gated by the config array at offset 1440 -- when `opts[1440]` is set, the wrapper skips the pass entirely. At tier 2, GlobalOpt runs unconditionally at pipeline position 30. At tier 3, it runs with the same parameters but benefits from more aggressive SCCP and GlobalDCE having run upstream.

There are no user-facing CLI flags that directly control the 2047-bit threshold or the 16-field SRA limit. These are compile-time constants in the binary. The only external control is the tier-level gate and the `opts[1440]` kill switch.

## Function Map

| Address | Symbol | Role |
|---------|--------|------|
| `0x18612A0` | `sub_18612A0` | Core transform: type filter, Path A, Path B |
| `0x196A2B0` | `sub_196A2B0` | Pipeline wrapper (calls core after GlobalDCE) |
| `0x185B1D0` | `sub_185B1D0` | Recursive re-application to split globals |
| `0x185B7E0` | `sub_185B7E0` | Pre-SRA setup |
| `0x1860410` | `sub_1860410` | Hash table rehash |
| `0x1860630` | `sub_1860630` | Hash table lookup |
| `0x1860BE0` | `sub_1860BE0` | Per-user SRA rewrite |
| `0x185C560` | `sub_185C560` | Collect all store values for a global |
| `0x185C920` | `sub_185C920` | Analyze single store for optimizability |
| `0x185CAF0` | `sub_185CAF0` | Collect stored value into hash set |
| `0x15E51E0` | `sub_15E51E0` | Create global variable (88 bytes, with AS) |
| `0x15E5070` | `sub_15E5070` | Create init function |
| `0x164D160` | `sub_164D160` | RAUW (Replace All Uses With) |
| `0x15F20C0` | `sub_15F20C0` | Erase instruction from parent |
| `0x15E55B0` | `sub_15E55B0` | Erase global declaration |
| `0x15A9520` | `sub_15A9520` | `getPointerSizeInBits(target, addr_space)` |
| `0x15A9930` | `sub_15A9930` | `getStructLayout` (field offsets) |
| `0x15A06D0` | `sub_15A06D0` | `computeFieldOffset` |
| `0x1646BA0` | `sub_1646BA0` | `getStructFieldType` |
| `0x16435F0` | `sub_16435F0` | `isAnalyzableType(type, depth)` |
| `0x140B2F0` | `sub_140B2F0` | `evaluateInitializer(module, target, ..., 1)` |
| `0x15FB630` | `sub_15FB630` | Create `notinit` sentinel |
| `0x15FB440` | `sub_15FB440` | Create binary OR (opcode 27) |
| `0x15FEC10` | `sub_15FEC10` | Create ICmp instruction |
| `0x15F8650` | `sub_15F8650` | Create conditional branch |
| `0x15F8590` | `sub_15F8590` | Create unconditional branch |
| `0x157FBF0` | `sub_157FBF0` | Create basic block |
| `0x15FED60` | `sub_15FED60` | Create ICmp NE (opcode 51, predicate 33) |
| `0x15F9330` | `sub_15F9330` | Create alloca (`"tmp"` variable in block) |
| `0x15FDB00` | `sub_15FDB00` | Wire def into use-def chain |
| `0x15F9850` | `sub_15F9850` | Create store-to-field-global |
| `0x157E9C0` | `sub_157E9C0` | Create return basic block (null-return) |
| `0x157FB60` | `sub_157FB60` | Create basic block with predecessor |
| `0x15F55D0` | `sub_15F55D0` | Grow operand list |
| `0x1648700` | `sub_1648700` | `getInstruction(use)` from use-chain |
| `0x1649960` | `sub_1649960` | `getName(global/fn)` returns C string |
| `0x1648A60` | `sub_1648A60` | `IRBuilder::create(size, kind)` allocates IR node |
| `0x15E5530` | `sub_15E5530` | Destroy function body |
| `0x159D9E0` | `sub_159D9E0` | Destroy function |
| `0x164BE60` | `sub_164BE60` | Drop all references |
| `0x1648B90` | `sub_1648B90` | Mark dead (flags or-equals 1) |
| `0x1631BE0` | `sub_1631BE0` | Insert into function list |
| `0x15A9FE0` | `sub_15A9FE0` | `getAlignment(target, type)` ABI alignment |
| `0x15A0680` | `sub_15A0680` | `lookupSymbol(module_sym, idx, flags)` |
| `0x16463B0` | `sub_16463B0` | `getArrayElementType(ptr, idx)` |
| `0x159C540` | `sub_159C540` | `getTerminalType(type)` |
| `0x1752100` | `sub_1752100` | Collect use-def chain |
| `0x15E6480` | `sub_15E6480` | Copy metadata from global to global |
| `0x15F8F80` | `sub_15F8F80` | Create extractvalue instruction |
| `0x15F9480` | `sub_15F9480` | Create store-init (initializer store) |
| `0x15F9660` | `sub_15F9660` | Create field store (offset + field global) |
| `0x15FD590` | `sub_15FD590` | Create local variable (`"newgv"`) |
| `0x15FEBE0` | `sub_15FEBE0` | Create bitcast/GEP for field extraction |
| `0x1648780` | `sub_1648780` | Replace use with value |
| `0x16CC920` | `sub_16CC920` | Grow scratch buffer |
| `0x16CC9F0` | `sub_16CC9F0` | Find in sorted set |
| `0x1968390` | `sub_1968390` | GlobalDCE / ConstantProp (runs before GlobalOpt) |

## Differences from Upstream LLVM GlobalOpt

Stock LLVM's `GlobalOptPass` (in `lib/Transforms/IPO/GlobalOpt.cpp`) performs similar high-level transformations: SRA of globals, shrink-to-bool, constant marking, dead global elimination, malloc/free removal, static constructor evaluation, calling convention optimization (fastcc), and alias resolution. The NVIDIA implementation diverges in these concrete ways:

1. **Internal IR, not LLVM IR.** The pass operates on NVIDIA's custom IR node format with 88-byte global nodes, 24-byte operand stride, and type tags at offset +8/+16 of type/instruction nodes. A reimplementation targeting upstream LLVM would use `GlobalVariable`, `StoreInst`, `LoadInst`, and `GetElementPtrInst` directly.

2. **2047-bit constant promotion threshold.** LLVM does not have a single bit-count gate for constant promotion. NVIDIA's threshold likely targets the GPU register file: 2047 bits is approximately 64 32-bit registers, close to the per-thread register budget on many SM architectures.

3. **Per-field malloc decomposition.** Stock LLVM's `tryToOptimizeStoreOfMallocToGlobal` handles malloc/free as a single pair. NVIDIA generates per-field null checks, conditional frees, and continuation blocks -- a more aggressive decomposition.

4. **Custom hash table.** LLVM uses `DenseMap`/`SmallPtrSet`. NVIDIA uses a hand-rolled open-addressing hash table with 32-byte entries (see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function and sentinel values).

5. **Address-space preservation.** Every created global explicitly receives the source global's address space. Stock LLVM does not special-case address spaces in GlobalOpt.

6. **Recursive re-application.** After splitting, NVIDIA calls `sub_185B1D0` to re-run GlobalOpt on the results. Upstream LLVM relies on the pass manager to schedule re-runs via its invalidation mechanism.

7. **Inliner integration.** The inliner cost model at the same address range calls into GlobalOpt to evaluate post-inline global folding benefit. This tight coupling does not exist in upstream LLVM where inlining and GlobalOpt are independent passes.

## Cross-References

- [NVModuleSummary Builder](./module-summary.md) -- builds the global reference edges that determine which globals are live across modules
- [Inliner Cost Model](./inliner-cost.md) -- calls GlobalOpt's transform function to evaluate post-inline global optimization benefit
- [ThinLTO Function Import](./thinlto-import.md) -- imports functions across module boundaries, exposing globals for cross-module optimization
- [Alias Analysis & NVVM AA](../infra/alias-analysis.md) -- address-space-aware alias analysis that informs which memory operations can alias globals in different address spaces
- [MemorySpaceOpt](../passes/memory-space-opt.md) -- resolves generic pointers to specific address spaces; runs before GlobalOpt and may expose globals that were previously behind generic pointers
- [Pipeline & Ordering](../llvm/pipeline.md) -- full pass ordering showing GlobalOpt's position at step 30
- [Type Translation, Globals & Special Vars](../pipeline/irgen-types.md) -- how EDG frontend assigns address spaces to global variables during IR generation
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- hash function, sentinel values, and probing strategy used by the processed-globals table
- [Struct Splitting](../passes/struct-splitting.md) -- the NewPM `lower-aggr-copies` pass that handles similar aggregate decomposition at a different pipeline stage
- [Address Spaces](../reference/address-spaces.md) -- complete NVPTX address space reference including pointer sizes and latency characteristics
