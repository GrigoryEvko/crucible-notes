# DSE (Dead Store Elimination)

## Overview

Dead Store Elimination removes store instructions that write to memory locations where the written values are subsequently overwritten before being read. Pass ID: DSE. Category: Dead Code Elimination. Execution scope: Function-level. Complexity: O(N) where N = store instruction count. MemorySSA integration yields O(1) reachability queries versus O(N) with traditional MemoryDependenceAnalysis.

Prerequisites: MemorySSA analysis, MemoryDependenceAnalysis, AliasAnalysis, AAResults.

## Algorithm

```c
struct StoreTracker {
    Instruction* store_instr;
    uint64_t mem_address;
    uint32_t store_size_bytes;
    BitVector* overwrite_mask;
    MemoryDef* memory_def;
    bool is_dead;
    bool is_partial_live;
};

struct DSEContext {
    Function* func;
    MemorySSA* mem_ssa;
    AliasAnalysis* alias_analysis;
    StoreTracker* stores;
    uint32_t store_count;
    uint32_t scanlimit;
    uint32_t walklimit;
    bool enable_partial_tracking;
};

void runDSEPass(Function* func, MemorySSA* mem_ssa, AliasAnalysis* aa) {
    // STEP 1: Build MemorySSA
    mem_ssa->run(func);

    // STEP 2: Scan for store instructions
    DSEContext ctx = {0};
    ctx.func = func;
    ctx.mem_ssa = mem_ssa;
    ctx.alias_analysis = aa;
    ctx.store_count = 0;
    ctx.enable_partial_tracking = getParameterBool("enable-dse-partial-overwrite-tracking");
    ctx.scanlimit = getParameterInt("dse-memoryssa-scanlimit");
    ctx.walklimit = getParameterInt("dse-memoryssa-walklimit");

    for (BasicBlock* bb : func->blocks()) {
        uint32_t bb_scan_count = 0;
        for (Instruction* inst : bb->instructions()) {
            if (isStoreInstruction(inst) && bb_scan_count < ctx.scanlimit) {
                bb_scan_count++;
                StoreTracker* tracker = allocateStoreTracker();
                tracker->store_instr = inst;
                tracker->mem_address = extractMemoryAddress(inst);
                tracker->store_size_bytes = extractStoreSize(inst);
                tracker->memory_def = mem_ssa->getMemoryDefForStore(inst);
                tracker->overwrite_mask = allocateBitVector(tracker->store_size_bytes * 8);
                setBitVectorAll(tracker->overwrite_mask);
                ctx.stores[ctx.store_count++] = tracker;

                if (ctx.store_count >= getParameterInt("dse-memoryssa-partial-store-limit")) {
                    ctx.enable_partial_tracking = false;
                }
            }
        }
    }

    // STEP 3: For each store, find reaching definitions
    for (uint32_t i = 0; i < ctx.store_count; i++) {
        StoreTracker* store = ctx.stores[i];
        MemoryDef* def = store->memory_def;

        bool has_reaching_use = false;
        for (MemoryUse* use : def->uses()) {
            if (walkMemorySSA(use, ctx.walklimit)) {
                has_reaching_use = true;
                break;
            }
        }

        if (!has_reaching_use) {
            store->is_dead = true;
        }
    }

    // STEP 4: Check for subsequent stores (partial overwrite tracking)
    if (ctx.enable_partial_tracking) {
        for (uint32_t i = 0; i < ctx.store_count; i++) {
            StoreTracker* store_i = ctx.stores[i];
            clearBitVector(store_i->overwrite_mask);

            for (uint32_t j = i + 1; j < ctx.store_count; j++) {
                StoreTracker* store_j = ctx.stores[j];
                uint32_t overlap_offset = 0;
                uint32_t overlap_size = 0;

                if (computeMemoryOverlap(store_i->mem_address,
                                        store_i->store_size_bytes,
                                        store_j->mem_address,
                                        store_j->store_size_bytes,
                                        &overlap_offset,
                                        &overlap_size)) {

                    for (uint32_t byte_idx = 0; byte_idx < overlap_size; byte_idx++) {
                        uint32_t bit_pos = (overlap_offset + byte_idx) * 8;
                        setBitRange(store_i->overwrite_mask, bit_pos, 8);
                    }
                }
            }

            if (isBitVectorFull(store_i->overwrite_mask, store_i->store_size_bytes * 8)) {
                store_i->is_dead = true;
            } else if (isBitVectorPartial(store_i->overwrite_mask)) {
                store_i->is_partial_live = true;
            }
        }
    }

    // STEP 5: Handle store merging
    if (getParameterBool("enable-dse-partial-store-merging")) {
        performStoreMerging(&ctx);
    }

    // STEP 6: Verify no aliases invalidate analysis
    for (uint32_t i = 0; i < ctx.store_count; i++) {
        StoreTracker* store = ctx.stores[i];
        BasicBlock* store_bb = store->store_instr->getParent();

        for (Instruction* inst : store_bb->instructions()) {
            if (isCallInstruction(inst) && !isKnownMemoryReadOnly(inst)) {
                store->is_dead = false;
            }

            if (isIndirectMemoryOp(inst)) {
                if (!canProveDereferencePrecision(inst, aa)) {
                    store->is_dead = false;
                }
            }
        }

        if (isVolatileAccess(store->store_instr)) {
            store->is_dead = false;
        }
    }

    // STEP 7: Eliminate dead stores
    for (uint32_t i = 0; i < ctx.store_count; i++) {
        StoreTracker* store = ctx.stores[i];
        if (store->is_dead && !store->is_partial_live) {
            store->store_instr->eraseFromParent();
            mem_ssa->removeMemoryAccess(store->memory_def);
            mem_ssa->invalidatePhis();
        }
    }
}

bool walkMemorySSA(MemoryUse* use, uint32_t walklimit) {
    uint32_t visit_count = 0;
    MemoryAccess* access = use->getDefiningAccess();

    while (access && visit_count < walklimit) {
        visit_count++;

        if (isa<MemoryPhi>(access)) {
            MemoryPhi* phi = cast<MemoryPhi>(access);
            for (Value* incoming : phi->getIncomingValues()) {
                if (walkMemorySSA(cast<MemoryUse>(incoming), walklimit - visit_count)) {
                    return true;
                }
            }
        } else if (isa<MemoryUse>(access)) {
            return true;
        }

        access = access->getDefiningAccess();
    }

    return false;
}

bool computeMemoryOverlap(uint64_t addr1, uint32_t size1,
                         uint64_t addr2, uint32_t size2,
                         uint32_t* out_offset, uint32_t* out_size) {
    uint64_t addr1_end = addr1 + size1;
    uint64_t addr2_end = addr2 + size2;

    uint64_t overlap_start = (addr1 > addr2) ? addr1 : addr2;
    uint64_t overlap_end = (addr1_end < addr2_end) ? addr1_end : addr2_end;

    if (overlap_start >= overlap_end) {
        return false;
    }

    *out_offset = overlap_start - addr1;
    *out_size = overlap_end - overlap_start;
    return true;
}

void setBitRange(BitVector* bv, uint32_t start_bit, uint32_t num_bits) {
    for (uint32_t i = 0; i < num_bits; i++) {
        bv->set(start_bit + i);
    }
}

bool isBitVectorFull(BitVector* bv, uint32_t num_bits) {
    for (uint32_t i = 0; i < num_bits; i++) {
        if (!bv->test(i)) {
            return false;
        }
    }
    return true;
}

bool isBitVectorPartial(BitVector* bv) {
    bool has_set = false;
    bool has_unset = false;

    for (uint32_t i = 0; i < bv->size(); i++) {
        if (bv->test(i)) {
            has_set = true;
        } else {
            has_unset = true;
        }
    }

    return has_set && has_unset;
}
```

## MemorySSA Data Structures

```c
struct MemoryAccess {
    unsigned access_type;
    Instruction* inst;
    BasicBlock* block;
    MemoryAccess* defining_access;
    unsigned volatile_order;
    unsigned block_order;
};

struct MemoryUse {
    MemoryAccess base;
    SmallVector<MemoryAccess*> clobbering_accesses;
    MemoryAccess* optimized_def;
};

struct MemoryDef {
    MemoryAccess base;
    SmallVector<MemoryUse*> uses;
    bool is_volatile;
};

struct MemoryPhi {
    MemoryAccess base;
    SmallVector<MemoryAccess*> incoming_defs;
    unsigned num_incoming;
};

struct MemorySSA {
    Function* func;
    DominatorTree* dom_tree;
    PostDominatorTree* post_dom_tree;
    SmallVector<MemoryDef*> memory_defs;
    SmallVector<MemoryPhi*> memory_phis;
    DenseMap<Instruction*, MemoryAccess*> inst_to_access;
    DenseMap<BasicBlock*, MemoryPhi*> phi_map;
    unsigned next_id;
};

struct MemorySSAWalker {
    MemorySSA* m_ssa;
    DenseMap<MemoryAccess*, MemoryAccess*> cache;
    unsigned walklimit;
};

MemoryDef* MemorySSA_getMemoryDef(MemorySSA* ssa, Instruction* inst) {
    MemoryAccess* access = ssa->inst_to_access[inst];
    if (access && isa<MemoryDef>(access)) {
        return cast<MemoryDef>(access);
    }
    return NULL;
}

MemoryUse* MemorySSA_getMemoryUse(MemorySSA* ssa, Instruction* inst) {
    MemoryAccess* access = ssa->inst_to_access[inst];
    if (access && isa<MemoryUse>(access)) {
        return cast<MemoryUse>(access);
    }
    return NULL;
}

MemoryAccess* MemorySSAWalker_getClobberingMemoryAccess(MemorySSAWalker* walker,
                                                       MemoryUse* use) {
    unsigned visits = 0;
    MemoryAccess* access = use->defining_access;

    while (visits < walker->walklimit) {
        visits++;

        if (isa<MemoryPhi>(access)) {
            MemoryPhi* phi = cast<MemoryPhi>(access);
            unsigned first_incoming = 0;
            for (unsigned i = 0; i < phi->num_incoming; i++) {
                if (phi->incoming_defs[i]) {
                    first_incoming = i;
                    break;
                }
            }
            access = phi->incoming_defs[first_incoming];
        } else if (isa<MemoryDef>(access)) {
            return access;
        } else {
            return access;
        }
    }

    return access;
}
```

## Partial Overwrite Tracking Mechanism

```c
struct ByteMask {
    uint8_t* mask;
    uint32_t size_bytes;
    uint32_t alloc_size;
};

struct OverwriteTracker {
    DenseMap<uint64_t, ByteMask*> address_to_mask;
    SmallVector<StoreInfo*> store_list;
    uint32_t partial_store_limit;
};

void initializeByteMask(ByteMask* mask, uint32_t size) {
    mask->size_bytes = size;
    mask->alloc_size = (size + 7) / 8;
    mask->mask = allocate(mask->alloc_size);
    memset(mask->mask, 0xFF, mask->alloc_size);
}

void setByteMask(ByteMask* mask, uint32_t byte_offset, uint32_t num_bytes) {
    for (uint32_t i = 0; i < num_bytes; i++) {
        uint32_t byte_idx = (byte_offset + i) / 8;
        uint32_t bit_idx = (byte_offset + i) % 8;
        if (byte_idx < mask->alloc_size) {
            mask->mask[byte_idx] |= (1 << bit_idx);
        }
    }
}

void clearByteMask(ByteMask* mask, uint32_t byte_offset, uint32_t num_bytes) {
    for (uint32_t i = 0; i < num_bytes; i++) {
        uint32_t byte_idx = (byte_offset + i) / 8;
        uint32_t bit_idx = (byte_offset + i) % 8;
        if (byte_idx < mask->alloc_size) {
            mask->mask[byte_idx] &= ~(1 << bit_idx);
        }
    }
}

bool isByteMaskFull(ByteMask* mask) {
    for (uint32_t i = 0; i < mask->size_bytes; i++) {
        if (!((mask->mask[i / 8] >> (i % 8)) & 1)) {
            return false;
        }
    }
    return true;
}

uint32_t countLiveBytes(ByteMask* mask) {
    uint32_t count = 0;
    for (uint32_t i = 0; i < mask->size_bytes; i++) {
        if ((mask->mask[i / 8] >> (i % 8)) & 1) {
            count++;
        }
    }
    return count;
}

void analyzePartialOverwrites(OverwriteTracker* tracker,
                             StoreTracker* stores[],
                             uint32_t store_count) {
    for (uint32_t i = 0; i < store_count; i++) {
        StoreTracker* store_i = stores[i];
        ByteMask* mask = allocate(sizeof(ByteMask));
        initializeByteMask(mask, store_i->store_size_bytes);

        bool store_is_dead = true;

        for (uint32_t j = i + 1; j < store_count; j++) {
            StoreTracker* store_j = stores[j];

            uint64_t store_i_start = store_i->mem_address;
            uint64_t store_i_end = store_i_start + store_i->store_size_bytes;
            uint64_t store_j_start = store_j->mem_address;
            uint64_t store_j_end = store_j_start + store_j->store_size_bytes;

            if (store_j_start < store_i_end && store_j_end > store_i_start) {
                uint64_t overlap_start = (store_i_start > store_j_start) ? store_i_start : store_j_start;
                uint64_t overlap_end = (store_i_end < store_j_end) ? store_i_end : store_j_end;

                uint32_t mask_offset = overlap_start - store_i_start;
                uint32_t mask_len = overlap_end - overlap_start;

                setByteMask(mask, mask_offset, mask_len);
            }

            // Check if any load occurs between store_i and store_j
            for (BasicBlock* bb : store_j->store_instr->getParent()->getPredecessors()) {
                for (Instruction* inst : bb->instructions()) {
                    if (isLoadInstruction(inst)) {
                        uint64_t load_addr = extractMemoryAddress(inst);
                        uint32_t load_size = extractLoadSize(inst);

                        if (load_addr >= store_i_start && load_addr < store_i_end) {
                            store_is_dead = false;
                            break;
                        }
                    }
                }
            }
        }

        if (store_is_dead && isByteMaskFull(mask)) {
            store_i->is_dead = true;
        }
    }
}
```

## Store Merging Algorithm

```c
struct StoreMergeCandidate {
    uint32_t store_idx_a;
    uint32_t store_idx_b;
    uint32_t merge_cost;
    uint32_t merge_benefit;
    bool is_profitable;
};

struct MergedStore {
    uint64_t merged_address;
    uint32_t merged_size;
    Value* merged_value;
    Instruction* merge_point;
    SmallVector<uint32_t> component_indices;
};

void performStoreMerging(DSEContext* ctx) {
    uint32_t max_merges = getParameterInt("dse-memoryssa-partial-store-limit");
    uint32_t merge_count = 0;

    for (uint32_t i = 0; i < ctx->store_count && merge_count < max_merges; i++) {
        StoreTracker* store_i = ctx->stores[i];

        if (store_i->is_dead) continue;

        for (uint32_t j = i + 1; j < ctx->store_count; j++) {
            StoreTracker* store_j = ctx->stores[j];

            if (store_j->is_dead) continue;

            if (!isAdjacent(store_i->mem_address, store_i->store_size_bytes,
                           store_j->mem_address, store_j->store_size_bytes)) {
                continue;
            }

            BasicBlock* bb_i = store_i->store_instr->getParent();
            BasicBlock* bb_j = store_j->store_instr->getParent();

            if (bb_i != bb_j) {
                if (!isConsecutiveBlock(bb_i, bb_j)) {
                    continue;
                }
            }

            uint32_t inter_defs = countInterveningDefs(store_i, store_j, ctx);
            if (inter_defs > 0) continue;

            uint32_t merged_size = computeMergedSize(store_i->mem_address,
                                                    store_i->store_size_bytes,
                                                    store_j->mem_address,
                                                    store_j->store_size_bytes);

            uint32_t merge_cost = computeMergeCost(merged_size);
            uint32_t merge_benefit = computeMergeBenefit(store_i, store_j);

            if (merge_benefit > merge_cost) {
                MergedStore* merged = allocateMergedStore();
                merged->merged_address = computeMergedAddress(store_i->mem_address,
                                                             store_j->mem_address);
                merged->merged_size = merged_size;
                merged->component_indices[0] = i;
                merged->component_indices[1] = j;

                Value* combined_value = combineBitPatterns(
                    extractLoadValue(store_i->store_instr),
                    extractLoadValue(store_j->store_instr),
                    store_i->mem_address - merged->merged_address,
                    store_j->mem_address - merged->merged_address);

                merged->merged_value = combined_value;
                merged->merge_point = store_i->store_instr;

                performMerge(ctx, merged);
                merge_count++;
            }
        }
    }
}

bool isAdjacent(uint64_t addr1, uint32_t size1, uint64_t addr2, uint32_t size2) {
    return (addr1 + size1 == addr2) || (addr2 + size2 == addr1);
}

uint32_t computeMergedSize(uint64_t addr1, uint32_t size1,
                          uint64_t addr2, uint32_t size2) {
    uint64_t start = (addr1 < addr2) ? addr1 : addr2;
    uint64_t end = (addr1 + size1 > addr2 + size2) ? (addr1 + size1) : (addr2 + size2);
    return end - start;
}

uint32_t computeMergeCost(uint32_t merged_size) {
    uint32_t cost = 0;

    // Cost for wider store instruction
    if (merged_size <= 4) {
        cost = 1;
    } else if (merged_size <= 8) {
        cost = 2;
    } else if (merged_size <= 16) {
        cost = 3;
    } else {
        cost = 4;
    }

    // Cost for register combining operations
    cost += (merged_size > 8) ? 2 : 0;

    return cost;
}

uint32_t computeMergeBenefit(StoreTracker* store1, StoreTracker* store2) {
    uint32_t benefit = 0;

    // Benefit from eliminating one store instruction
    benefit += 1;

    // Benefit from improved cache locality
    benefit += 1;

    // Benefit from reduced register pressure
    if (store1->store_size_bytes > 0 && store2->store_size_bytes > 0) {
        benefit += 1;
    }

    return benefit;
}

void performMerge(DSEContext* ctx, MergedStore* merged) {
    uint32_t idx_a = merged->component_indices[0];
    uint32_t idx_b = merged->component_indices[1];

    StoreTracker* store_a = ctx->stores[idx_a];
    StoreTracker* store_b = ctx->stores[idx_b];

    // Create new merged store instruction
    IRBuilder<> builder(store_a->store_instr);
    Instruction* merged_inst = builder.CreateStore(
        merged->merged_value,
        builder.CreateIntToPtr(
            builder.getInt64(merged->merged_address),
            PointerType::getUnqual(IntegerType::get(ctx->func->getContext(),
                                                    merged->merged_size * 8))));

    // Update MemorySSA for merged store
    MemoryDef* merged_def = ctx->mem_ssa->createMemoryDef(merged_inst);

    // Mark originals as redundant
    store_a->store_instr->eraseFromParent();
    store_b->store_instr->eraseFromParent();

    // Update tracking
    store_a->is_dead = true;
    store_b->is_dead = true;
}
```

## Configuration Parameters

| Parameter | Type | Default | Range | Impact | Rationale |
|-----------|------|---------|-------|--------|-----------|
| `enable-dse-partial-overwrite-tracking` | bool | true | {true, false} | HIGH - byte-level overwrite detection | Permits elimination of stores with partial overwrites; disables tracking when store_count exceeds limit |
| `enable-dse-partial-store-merging` | bool | true | {true, false} | MEDIUM - adjacent store fusion | Reduces store instruction count; improves memory locality; increases register usage if beneficial |
| `dse-memoryssa-partial-store-limit` | int | 100 | [1, ∞) | Threshold for disabling partial tracking | Prevents exponential analysis growth; conservative fallback when store count exceeds threshold |
| `dse-memoryssa-defs-per-block-limit` | int | Unknown | [1, ∞) | Limits MemorySSA growth per block | Controls memory overhead for blocks with many stores; prevents pathological MemoryPhi creation |
| `dse-memoryssa-path-check-limit` | int | Unknown | [1, ∞) | Maximum reachability check paths | Limits analysis cost in divergent control flow; bounds walklimit for path traversal |
| `dse-memoryssa-scanlimit` | int | 150 | [1, ∞) | Forward scan distance for loads | Maximum instructions scanned after store; bounded linear search for load dependencies |
| `dse-memoryssa-walklimit` | int | Unknown | [1, ∞) | MemorySSA walker traversal depth | Controls MemorySSA phi-traversal recursion depth; prevents stack overflow in cyclic memory patterns |
| `dse-memoryssa-samebb-cost` | int | Unknown | [1, ∞) | Same-block reachability cost | Heuristic weight for same-block store sequences; affects store merging profitability |
| `dse-optimize-memoryssa` | bool | true | {true, false} | MEDIUM - walker caching | Activates MemorySSA walker result caching; O(1) cached queries vs O(walklimit) uncached |
| `dse-memoryssa-otherbb-cost` | int | Unknown | [1, ∞) | Cross-block reachability cost | Heuristic weight for cross-block store sequences; affects merge profitability across blocks |

## Complexity Analysis

O(N) overall where N = store instruction count within function scope.

Per-store operations:
- MemorySSA reachability query: O(1) with walker caching; O(walklimit) uncached traversal
- Partial overwrite tracking: O(K × M) where K = stores examined, M = overlapping store count
- Byte-mask computation: O(store_size_bytes / 8) bit operations
- Store merging decision: O(log N) candidate search with early termination

MemorySSA construction: O(N) where N = instructions in function.

MemoryPhi creation: O(blocks) at control flow joins; bounded by number of basic blocks.

## CUDA Memory Space Handling

```c
enum CUDAMemorySpace {
    CUDA_SPACE_GLOBAL = 1,
    CUDA_SPACE_SHARED = 2,
    CUDA_SPACE_LOCAL = 3,
    CUDA_SPACE_CONSTANT = 4,
    CUDA_SPACE_GENERIC = 5
};

struct CUDAMemoryConstraints {
    CUDAMemorySpace space;
    bool requires_sync_before_dse;
    bool has_atomic_semantics;
    bool visible_to_all_threads;
    uint32_t sync_scope_id;
};

void analyzeCUDAMemorySpace(Instruction* store_inst,
                           CUDAMemoryConstraints* constraints) {
    uint32_t addr_space = getAddressSpace(store_inst);

    switch (addr_space) {
        case CUDA_SPACE_GLOBAL:
            constraints->space = CUDA_SPACE_GLOBAL;
            constraints->visible_to_all_threads = true;

            // Check for synchronization dependencies
            if (hasMemoryBarrier(store_inst)) {
                constraints->requires_sync_before_dse = true;
                constraints->sync_scope_id = getBarrierScopeId(store_inst);
            }

            // Check for atomic semantics
            if (isAtomicStore(store_inst)) {
                constraints->has_atomic_semantics = true;
            }
            break;

        case CUDA_SPACE_SHARED:
            constraints->space = CUDA_SPACE_SHARED;
            constraints->visible_to_all_threads = true;

            // Shared memory writes must respect __syncthreads()
            if (!hasGuardingSync(store_inst)) {
                constraints->requires_sync_before_dse = true;
            }

            // Check for inter-thread communication patterns
            if (mightCommunicateBetweenThreads(store_inst)) {
                constraints->requires_sync_before_dse = true;
            }
            break;

        case CUDA_SPACE_LOCAL:
            constraints->space = CUDA_SPACE_LOCAL;
            constraints->visible_to_all_threads = false;
            constraints->requires_sync_before_dse = false;

            // Local memory is per-thread; no synchronization needed
            break;

        case CUDA_SPACE_CONSTANT:
            constraints->space = CUDA_SPACE_CONSTANT;
            constraints->visible_to_all_threads = true;
            constraints->requires_sync_before_dse = false;

            // Constant memory is read-only; stores may be compile errors
            break;
    }
}

bool canElimateStoreInCUDAKernel(Instruction* store_inst,
                               CUDAMemoryConstraints* constraints) {

    // Disallow elimination of atomic stores
    if (constraints->has_atomic_semantics) {
        return false;
    }

    // Disallow elimination of global memory stores without sync analysis
    if (constraints->space == CUDA_SPACE_GLOBAL &&
        constraints->visible_to_all_threads &&
        !constraints->requires_sync_before_dse) {
        return false;
    }

    // Disallow elimination of shared memory stores without guarantee
    // that no other thread reads the value
    if (constraints->space == CUDA_SPACE_SHARED) {
        return false;
    }

    // Local memory stores can be safely eliminated if dead
    if (constraints->space == CUDA_SPACE_LOCAL) {
        return true;
    }

    return true;
}

struct AtomicStoreInfo {
    Instruction* store_inst;
    uint32_t memory_order;
    uint32_t sync_scope;
    bool is_release;
    bool is_acq_rel;
};

uint32_t extractAtomicOrdering(Instruction* atomic_inst) {
    // Extract AtomicOrdering enum from instruction metadata
    // Possible values: Unordered, Monotonic, Acquire, Release, AcqRel, SeqCst

    Metadata* md = atomic_inst->getMetadata("atomic.order");
    if (md) {
        return extractIntFromMetadata(md);
    }

    return 0; // Unordered (default)
}

bool isAtomicStore(Instruction* inst) {
    if (!isa<StoreInst>(inst)) {
        return false;
    }

    StoreInst* store = cast<StoreInst>(inst);
    return store->isAtomic();
}

bool hasMemoryBarrier(Instruction* inst) {
    BasicBlock* bb = inst->getParent();
    Function* func = bb->getParent();

    for (Instruction* barrier : func->getInstructions()) {
        if (isMemoryBarrierInstruction(barrier)) {
            uint32_t barrier_scope = getBarrierScopeId(barrier);
            if (barrier_scope == CUDA_SCOPE_ALL_THREADS ||
                barrier_scope == CUDA_SCOPE_BLOCK) {
                return true;
            }
        }
    }

    return false;
}

bool mightCommunicateBetweenThreads(Instruction* store_inst) {
    BasicBlock* bb = store_inst->getParent();

    // Check if any load from shared memory follows this store
    for (Instruction* inst : bb->getInstructions()) {
        if (isLoadInstruction(inst) &&
            getAddressSpace(inst) == CUDA_SPACE_SHARED) {

            // Check if load address overlaps with store address
            uint64_t store_addr = extractMemoryAddress(store_inst);
            uint64_t load_addr = extractMemoryAddress(inst);

            if (load_addr == store_addr ||
                addressesOverlap(store_addr, extractStoreSize(store_inst),
                                load_addr, extractLoadSize(inst))) {
                return true;
            }
        }
    }

    return false;
}
```

## Reachability Query Functions

```c
bool queryMemoryReachable(MemorySSA* ssa,
                         MemoryDef* def,
                         Instruction* query_inst,
                         uint32_t walklimit) {
    MemoryAccess* access = ssa->getMemoryAccess(query_inst);

    if (!access) {
        return false;
    }

    MemoryAccess* clobbering = ssa->getWalker()->getClobberingMemoryAccess(
        cast<MemoryUse>(access),
        walklimit);

    return clobbering == def;
}

MemoryDef* findDominatingMemoryDef(MemorySSA* ssa,
                                  BasicBlock* bb,
                                  uint32_t offset,
                                  DominatorTree* dom_tree) {

    for (Instruction* inst : bb->instructions()) {
        if (isStoreInstruction(inst)) {
            MemoryDef* def = ssa->getMemoryDef(inst);
            if (def && dom_tree->dominates(inst->getParent(), bb)) {
                return def;
            }
        }
    }

    return NULL;
}

bool canProveNoAlias(AliasAnalysis* aa,
                    Instruction* store_inst,
                    Instruction* load_inst) {

    MemoryLocation store_loc = getMemoryLocation(store_inst);
    MemoryLocation load_loc = getMemoryLocation(load_inst);

    AliasResult result = aa->alias(store_loc, load_loc);

    return result == NoAlias;
}

SmallVector<MemoryUse*> findAllReachingUses(MemorySSA* ssa,
                                           MemoryDef* def) {
    SmallVector<MemoryUse*> uses;

    for (MemoryUse* use : def->uses()) {
        uses.push_back(use);
    }

    return uses;
}
```

## Instruction Classification

```c
enum StoreType {
    STORE_SCALAR_8BIT = 1,
    STORE_SCALAR_16BIT = 2,
    STORE_SCALAR_32BIT = 3,
    STORE_SCALAR_64BIT = 4,
    STORE_VECTOR_128BIT = 5,
    STORE_VECTOR_256BIT = 6,
    STORE_VECTOR_512BIT = 7,
    STORE_MEMSET_PATTERN = 8,
    STORE_MEMCPY_PATTERN = 9
};

bool isStoreInstruction(Instruction* inst) {
    return isa<StoreInst>(inst);
}

bool isLoadInstruction(Instruction* inst) {
    return isa<LoadInst>(inst);
}

bool isCallInstruction(Instruction* inst) {
    return isa<CallInst>(inst) || isa<InvokeInst>(inst);
}

bool isMemoryBarrierInstruction(Instruction* inst) {
    return inst->getOpcode() == Instruction::Fence ||
           inst->getOpcode() == Instruction::AtomicCmpXchg ||
           inst->getOpcode() == Instruction::AtomicRMW;
}

bool isIndirectMemoryOp(Instruction* inst) {
    if (isLoadInstruction(inst)) {
        LoadInst* load = cast<LoadInst>(inst);
        return !isa<GlobalVariable>(load->getPointerOperand());
    }

    if (isStoreInstruction(inst)) {
        StoreInst* store = cast<StoreInst>(inst);
        return !isa<GlobalVariable>(store->getPointerOperand());
    }

    return false;
}

bool isVolatileAccess(Instruction* inst) {
    if (isLoadInstruction(inst)) {
        return cast<LoadInst>(inst)->isVolatile();
    }

    if (isStoreInstruction(inst)) {
        return cast<StoreInst>(inst)->isVolatile();
    }

    return false;
}

uint32_t extractStoreSize(Instruction* store_inst) {
    StoreInst* store = cast<StoreInst>(store_inst);
    Type* store_type = store->getValueOperand()->getType();

    uint32_t bits = store_type->getPrimitiveSizeInBits();
    return (bits + 7) / 8;
}

uint32_t extractLoadSize(Instruction* load_inst) {
    LoadInst* load = cast<LoadInst>(load_inst);
    Type* load_type = load->getType();

    uint32_t bits = load_type->getPrimitiveSizeInBits();
    return (bits + 7) / 8;
}

uint64_t extractMemoryAddress(Instruction* memory_inst) {
    if (isStoreInstruction(memory_inst)) {
        StoreInst* store = cast<StoreInst>(memory_inst);
        return extractAddressValue(store->getPointerOperand());
    }

    if (isLoadInstruction(memory_inst)) {
        LoadInst* load = cast<LoadInst>(memory_inst);
        return extractAddressValue(load->getPointerOperand());
    }

    return 0;
}

Value* extractLoadValue(Instruction* store_inst) {
    if (isStoreInstruction(store_inst)) {
        StoreInst* store = cast<StoreInst>(store_inst);
        return store->getValueOperand();
    }
    return NULL;
}

bool addressesOverlap(uint64_t addr1, uint32_t size1,
                     uint64_t addr2, uint32_t size2) {
    return !(addr1 + size1 <= addr2 || addr2 + size2 <= addr1);
}

uint32_t getAddressSpace(Instruction* inst) {
    Type* ptr_type = NULL;

    if (isStoreInstruction(inst)) {
        StoreInst* store = cast<StoreInst>(inst);
        ptr_type = store->getPointerOperand()->getType();
    } else if (isLoadInstruction(inst)) {
        LoadInst* load = cast<LoadInst>(inst);
        ptr_type = load->getPointerOperand()->getType();
    }

    if (ptr_type && isa<PointerType>(ptr_type)) {
        return cast<PointerType>(ptr_type)->getAddressSpace();
    }

    return 0;
}
```

## Memory Dependency Integration

Pass runs after: Early CSE, GVN passes that remove trivial dead stores.

Pass runs before: Late-stage loop optimizations, vectorization passes.

Invalidates: MemorySSA (must be recomputed by dependent passes).

Preserves: DominatorTree, control flow semantics, exception handling.

## Analysis Termination Conditions

Eliminates all dead stores where:

1. Store S writes bytes [addr, addr+N)
2. For all loads L reading [addr, addr+N), no path exists from S to L without intermediate store S' overwriting all of [addr, addr+N)
3. No call instructions between S and all reaching loads (unless call is verified read-only)
4. No volatile accesses to overlapping addresses
5. No atomic memory ordering constraints violated
6. No CUDA synchronization dependencies violated

Conservative early termination when:

- Store count exceeds dse-memoryssa-partial-store-limit: disable partial tracking
- MemorySSA traversal depth exceeds walklimit: use cached result or return unknown
- Basic block contains >dse-memoryssa-defs-per-block-limit memory ops: limit MemoryPhi creation
- Path count in control flow exceeds dse-memoryssa-path-check-limit: use approximation

## Known Limitations

1. Requires MemorySSA; does not work with raw MemoryDependenceAnalysis
2. Byte-level tracking incurs overhead proportional to store_size_bytes; optimal for 1-8 byte stores
3. Does not track control-flow-dependent dead stores (requires post-dominance analysis)
4. Conservative with function calls; requires function side-effect analysis for precision
5. Walklimit prevents deep MemorySSA traversal; may miss distant dead stores
6. CUDA handling requires explicit address space metadata; generic pointers assume worst-case
7. Store merging heuristic uses fixed cost model; does not adapt to target architecture
