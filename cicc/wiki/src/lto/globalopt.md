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

The NVPTX address space encoding used by CICC:

| AS | CUDA qualifier | Memory | Latency | Scope |
|----|---------------|--------|---------|-------|
| 0 | (generic) | Generic pointer, resolved at runtime | Varies | Thread |
| 1 | `__device__` | Global device memory (DRAM) | 200-800 cycles | Grid |
| 3 | `__shared__` | Per-block SRAM (configurable L1) | 20-30 cycles | Block |
| 4 | `__constant__` | Constant memory (cached, broadcast) | 4 cycles (hit) | Grid |
| 5 | (local) | Per-thread local memory (spilled to DRAM) | 200-800 cycles | Thread |

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

When the global's initializer is a struct constant and its total bit-size (including alignment padding) fits within 2047 bits (0x7FF), the pass promotes it into a function-local value with a separate initializer function. This threshold is NVIDIA-specific -- upstream LLVM uses different heuristics based on `TargetData` layout considerations.

### Size Computation

The pass walks the type tree recursively to compute total bit-size:

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
| 0x10 | array | element_size * array_length (recursive) |

Note that opaque pointers (tag 0xF) use `getPointerSizeInBits(target, addr_space)` -- the pointer size varies by address space on NVPTX (64-bit for AS 0/1, potentially 32-bit for AS 3/5 on some targets).

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

After rewriting all uses, if the `.init` function has users, it is linked into the module's function list via `sub_1631BE0`. If it has zero users (the initializer was never needed), the function body is destroyed and marked dead. The original global is erased via `sub_15E55B0`. Finally, `sub_185B1D0` recursively re-applies GlobalOpt to the newly created `.body` global, enabling cascaded optimizations.

## Path B: Scalar Replacement of Aggregates (SRA)

When a global is too large for constant promotion, the pass attempts SRA -- exploding a struct global into per-field scalar globals. This path has stricter preconditions:

1. The caller's `flag` parameter (a4) must be zero -- when set, SRA is disabled.
2. The initializer must be the unique initializer for this global (verified via `sub_15A0680`).
3. The type must be a struct (tag 13) with 1 to 16 fields: `field_count - 1 <= 0xF`.
4. Every user must reference only this global -- no cross-global pointer arithmetic.

### Field Explosion

For each field index 0 through `field_count - 1`:

```c
for (int i = 0; i < field_count; i++) {
    Type *field_type = getStructFieldType(struct_type, ptr_bits);  // sub_1646BA0
    uint64_t field_offset = computeFieldOffset(type_info, bits);   // sub_15A06D0

    // Generate name: "my_global.f0", "my_global.f1", ...
    char name[256];
    snprintf(name, sizeof(name), "%s.f%d", global_name, i);

    // Create field global in same address space, internal linkage
    GlobalVariable *field_gv = createGlobalVar(
        scope, field_type, field_init, /*linkage=*/7, name, addr_space
    );  // sub_15E51E0

    // Copy metadata from parent
    copyMetadata(global, field_gv);  // sub_15E6480

    // Create GEP replacement and store initializer
    createBitcastGEP(module, type_info, src_type);  // sub_15FEBE0
    createFieldStore(offset, field_gv, parent);      // sub_15F9660
}
```

The field globals are stored in a dynamically-grown `std::vector` with realloc growth strategy (lines 1161-1220 of the decompilation).

### Null/Negative Guards

After field explosion, the pass generates safety checks for the original global's pointer value. This pattern handles the case where the global was heap-allocated via malloc -- the original pointer might be null or negative (indicating allocation failure on some platforms):

```llvm
%isneg  = icmp slt ptr @original_global, null    ; predicate 40 = SLT
%isnull0 = icmp eq ptr @my_global.f0, null        ; predicate 32 = EQ
%tmp0   = or i1 %isneg, %isnull0
%isnull1 = icmp eq ptr @my_global.f1, null
%tmp1   = or i1 %tmp0, %isnull1
; ... chain for all fields
br i1 %tmpN, label %malloc_ret_null, label %malloc_cont
```

The `.isneg` guard is created by `sub_15FEC10` with opcode 51 (ICmp), predicate 40 (SLT with zero). Per-field `.isnull` guards use predicate 32 (EQ with null). The guards are chained with OR instructions (opcode 27) via `sub_15FB440`.

### Malloc/Free Replacement

When the chained null check indicates a valid allocation, the pass generates a multi-block control flow that replaces the original single malloc/free pair with per-field conditional frees:

```llvm
malloc_ret_null:
  ret null

malloc_cont:
  ; For each field:
  %cond0 = icmp ne ptr @my_global.f0, null       ; predicate 33 = NE
  br i1 %cond0, label %free_it0, label %next0

free_it0:
  ; free the individual field allocation
  br label %next0

next0:
  store <field0_init>, ptr addrspace(N) @my_global.f0
  ; ... repeat for each field
```

This is more aggressive than upstream LLVM's malloc/free removal, which replaces a single malloc/free pair as an atomic unit. NVIDIA's version decomposes to per-field granularity, enabling partial-allocation scenarios where some fields are stack-promoted and others remain heap-allocated.

### Hash Table for Processed Globals

After field explosion and malloc rewrite, the pass uses a custom hash table (open addressing, 32-byte entries) to track which globals and their transitive users have been processed:

| Offset | Field | Description |
|--------|-------|-------------|
| +0 | key | Pointer to global (sentinel: -8 = empty, -16 = tombstone) |
| +8 | data | Pointer to field-global vector |
| +16 | size | Current vector size |
| +24 | cap | Vector capacity |

Hash function: `bucket = (capacity - 1) & ((uint64_t(global) >> 9) ^ (uint64_t(global) >> 4))`.
Collision resolution: linear probing.
Rehash trigger: `4 * (count + 1) >= 3 * capacity` (75% load factor), or when tombstone count exceeds `capacity / 8`.

The processing loop (lines 1710-1812) iterates remaining users of the original global. For stores (tag 54), it calls `sub_1860BE0` to rewrite each user's GEP+store/load sequences to reference the new field globals. For other users (typically loads), it creates direct stores to the appropriate field global using the computed field offset.

After all users are rewritten, cleanup proceeds in two phases: first, operand lists of dead GEP (tag 77) and store (tag 54) instructions are unlinked from the use chain (nulling out 24-byte-stride operand slots); second, the dead instructions are erased via `sub_15F20C0`. Finally, the original global declaration is erased, and all temporary data structures (hash table backing array, field vectors, scratch buffers) are freed.

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
| `0x16CC920` | `sub_16CC920` | Grow scratch buffer |
| `0x16CC9F0` | `sub_16CC9F0` | Find in sorted set |
| `0x1968390` | `sub_1968390` | GlobalDCE / ConstantProp (runs before GlobalOpt) |

## Differences from Upstream LLVM GlobalOpt

Stock LLVM's `GlobalOptPass` (in `lib/Transforms/IPO/GlobalOpt.cpp`) performs similar high-level transformations: SRA of globals, shrink-to-bool, constant marking, dead global elimination, malloc/free removal, static constructor evaluation, calling convention optimization (fastcc), and alias resolution. The NVIDIA implementation diverges in these concrete ways:

1. **Internal IR, not LLVM IR.** The pass operates on NVIDIA's custom IR node format with 88-byte global nodes, 24-byte operand stride, and type tags at offset +8/+16 of type/instruction nodes. A reimplementation targeting upstream LLVM would use `GlobalVariable`, `StoreInst`, `LoadInst`, and `GetElementPtrInst` directly.

2. **2047-bit constant promotion threshold.** LLVM does not have a single bit-count gate for constant promotion. NVIDIA's threshold likely targets the GPU register file: 2047 bits is approximately 64 32-bit registers, close to the per-thread register budget on many SM architectures.

3. **Per-field malloc decomposition.** Stock LLVM's `tryToOptimizeStoreOfMallocToGlobal` handles malloc/free as a single pair. NVIDIA generates per-field null checks, conditional frees, and continuation blocks -- a more aggressive decomposition.

4. **Custom hash table.** LLVM uses `DenseMap`/`SmallPtrSet`. NVIDIA uses a hand-rolled open-addressing hash table with 32-byte entries and a specific hash function (`(ptr >> 9) ^ (ptr >> 4)`).

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
