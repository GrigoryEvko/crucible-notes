# Error Handling

## Error Categories

Four error categories detected in CICC compilation pipeline:

1. **IR Validation Errors** (5+ instances)
2. **Register Allocation Errors** (8+ instances)
3. **Instruction Selection Errors** (3+ instances)
4. **CUDA-Specific Errors** (7+ instances)

---

## Error Messages (from binary)

### IR Validation

```
"Could not find leader" - GVN analysis failed due to malformed SSA
"Invalid opcode at line X"
"Use of undefined variable %v42"
"Definition does not dominate use"
"Operand count mismatch"
"Type mismatch: expected %s, got %s"
"Multiple definitions of variable"
"Operand not defined in predecessor"
"Non-SSA form detected"
```

### Register Allocation

```
"Bank conflict constraint violated"
"64-bit register not on even boundary"
"Register pair non-consecutive"
"Predicate exhaustion"
"Cannot allocate register"
"Register exceeds maximum"
"Spill register placement impossible due to constraints"
"Recursive spilling reaches resource limit"
"Register pressure exceeds architecture capacity"
```

### Instruction Selection

```
"No pattern match found for IR operation"
"Architecture Incompatibility: FP4 requires SM 100+"
"Operation unsupported for target SM version"
"Type combination has no implementation"
"Tensor core operation on SM <70"
"Warp specialization on SM <90"
"FP4 format on SM <100"
"TMA operations on SM <90"
"2:4 sparsity on SM <100"
```

### Divergence & Synchronization

```
"Cannot move store out of divergent region"
"Cannot use weight stationary with mxf8f6f4 and fp4 types"
"cta_group::2 is not supported with weight stationary"
"Warp Specialization Error: Invalid scale_vec configuration for tensor type"
"Cannot use 1X as scale_vec for mxf4nvf4"
"Cannot use 2X or 4X as scale_vec for mxf8f6f4"
"Cannot use 1X or 4X as scale_vec for mxf4"
"Memory operation in divergent region (would corrupt data)"
"Potential deadlock detected"
"Code motion out of divergent region (unsafe optimization)"
"Barrier mismatch between producer and consumer"
"Warp-level divergence without reconvergence point"
"2:4 sparsity pattern not satisfied"
```

---

## Validation Algorithms

### IR Node Validation

```c
typedef struct IRNode {
    uint8_t opcode;           // Offset 0
    uint8_t operand_count;    // Offset 1
    uint16_t flags;           // Offset 2
    void** operands;          // Offset 4-12 (system-dependent)
} IRNode;

bool validate_ir_node(IRNode* node) {
    // Check opcode range
    if (node->opcode > MAX_OPCODE) {
        error("Invalid IR opcode: %d at node %p",
              node->opcode, node);
        return false;
    }

    // Check operand count
    if (node->operand_count > MAX_OPERANDS) {
        error("Too many operands: %d for opcode %d",
              node->operand_count, node->opcode);
        return false;
    }

    // Validate each operand
    for (int i = 0; i < node->operand_count; i++) {
        if (!validate_operand_type(node->operands[i],
                                   expected_types[node->opcode][i])) {
            error("Operand type mismatch at position %d", i);
            return false;
        }
    }

    return true;
}
```

### SSA Validation Algorithm

```c
bool validate_ssa(Function* func) {
    // Phase 1: Check all uses have definitions
    for_each_instruction(instr, func) {
        for_each_use(use, instr) {
            Instruction* def = get_definition(use->value);
            if (!def) {
                error("Use of undefined variable %v%d at %p",
                      use->value->id, instr);
                return false;
            }

            // Check domination: definition must dominate use
            if (!dominates(def->block, instr->block)) {
                error("Definition does not dominate use");
                return false;
            }

            // Check type consistency
            if (!types_compatible(def->type, use->type)) {
                error("Type mismatch: expected %s, got %s",
                      type_name(def->type), type_name(use->type));
                return false;
            }
        }
    }

    // Phase 2: Validate phi nodes
    for_each_phi_node(phi, func) {
        // Operand count == predecessor block count
        if (phi->operand_count != phi->block->predecessor_count) {
            error("Phi node operand count mismatch: %d operands, %d predecessors",
                  phi->operand_count, phi->block->predecessor_count);
            return false;
        }

        // Each operand defined in corresponding predecessor
        for (int i = 0; i < phi->operand_count; i++) {
            BasicBlock* pred = phi->block->predecessors[i];
            if (!can_reach_definition(phi->operands[i], pred)) {
                error("Operand %d not defined in predecessor block", i);
                return false;
            }
        }
    }

    return true;
}
```

### Register Constraint Validation

```c
bool validate_register_allocation(RegisterAllocation* alloc,
                                   InterferenceGraph* ig) {
    // SM-specific register limits
    #define MAX_GPR         256  // General purpose registers
    #define MAX_PRED        8    // Predicate registers
    #define MAX_SPILLS      (SHARED_MEMORY_SIZE / 4)

    // Phase 1: Alignment and pairing constraints
    for_each_virtual_register(vr, alloc) {
        uint32_t physical = alloc->color[vr];
        RegisterClass rclass = vr->type->register_class;

        // 64-bit registers must be on even boundary
        if (rclass == GPR64) {
            if (physical % 2 != 0) {
                error("64-bit register %u not on even alignment boundary",
                      physical);
                return false;
            }
            if (physical + 1 >= MAX_GPR) {
                error("Register pair non-consecutive: R%u:R%u",
                      physical, physical + 1);
                return false;
            }
        }

        // Bank conflict check: multiple registers in same bank
        for_each_virtual_register(other_vr, alloc) {
            if (vr != other_vr && conflicts_with(vr, other_vr)) {
                uint32_t other_physical = alloc->color[other_vr];
                uint32_t bank1 = (physical % 128) / 4;
                uint32_t bank2 = (other_physical % 128) / 4;

                if (bank1 == bank2) {
                    error("Bank conflict: R%u (bank %u) conflicts with R%u (bank %u)",
                          physical, bank1, other_physical, bank2);
                    return false;
                }
            }
        }
    }

    // Phase 2: Predicate count
    uint32_t pred_count = 0;
    for_each_virtual_register(vr, alloc) {
        if (vr->type->register_class == PRED) {
            pred_count++;
        }
    }
    if (pred_count > 8) {
        error("Predicate exhaustion: %u predicates exceed limit of 8",
              pred_count);
        return false;
    }

    // Phase 3: Bounds check
    for_each_virtual_register(vr, alloc) {
        uint32_t physical = alloc->color[vr];
        if (physical >= MAX_GPR) {
            error("Register %u exceeds maximum %u", physical, MAX_GPR);
            return false;
        }
    }

    // Phase 4: Spill limit
    if (alloc->spill_count > MAX_SPILLS) {
        error("Spill count %u exceeds available shared memory",
              alloc->spill_count);
        return false;
    }

    return true;
}
```

### Bank Conflict Detection

```c
bool detect_bank_conflicts(SharedMemoryAccessPattern* pattern) {
    // Bank configuration constants
    #define BANKS_PER_SM        32
    #define BANK_WIDTH_BYTES    4
    #define CACHE_LINE_BYTES    128
    #define STRIDE_FACTOR       (CACHE_LINE_BYTES / BANK_WIDTH_BYTES)  // 32 banks

    // Formula: bank_index = (address % 128) / 4
    // Penalty: 32 cycles for full serialization

    for_each_memory_access(access, pattern) {
        uint32_t base_addr = extract_base_address(access);
        uint32_t stride = extract_stride(access);

        // Compute stride in banks
        uint32_t stride_banks = stride % STRIDE_FACTOR;

        // Conflict detection heuristics
        if (stride_banks == 0) {
            // All accesses hit same bank
            error("Bank conflict: stride %u hits same bank",
                  stride);
            pattern->conflict_penalty = 32;  // Full serialization
            return true;
        }

        if (stride_banks % (WARP_SIZE / THREADS_PER_BANK) > 1) {
            // Some threads access same bank
            error("Bank conflict: stride %u causes partial serialization",
                  stride);
            pattern->conflict_penalty = (WARP_SIZE / (stride_banks + 1)) * 4;
            return true;
        }
    }

    return false;
}
```

### Divergence Safety Validation

```c
// From sub_920430 @ 0x920430: divergence source classification
enum DivergenceClass {
    DIVERGENT_THREADIDX = 0,    // threadIdx.x/y/z
    UNIFORM_BLOCKDIM = 1,       // blockDim
    CONTEXT_BLOCKIDX = 2,       // blockIdx
    UNIFORM_GRIDDIM = 3,        // gridDim
    UNIFORM_WARPSIZE = 4        // warpSize
};

bool validate_divergence_safety(Function* func) {
    UniformityInfo* uniformity = compute_uniformity(func);

    for_each_block(block, func) {
        for_each_instruction(instr, block) {

            // Rule R1: Uniform Execution Requirement
            if (has_side_effects(instr)) {
                if (!all_threads_execute(instr, uniformity)) {
                    if (is_in_divergent_region(instr, uniformity)) {
                        error("Side effect not guaranteed on all threads: %p",
                              instr);
                        return false;
                    }
                }
            }

            // Rule R2: Memory Operation Preservation
            if (is_store(instr) || is_atomic(instr)) {
                if (is_in_divergent_region(instr, uniformity)) {
                    // Preserve store - cannot eliminate
                    mark_live(instr);
                }
            }

            // Rule R3: Control Dependence Safety
            if (is_control_dependent(instr)) {
                BasicBlock* ctrl_block = get_control_dependency(instr);
                if (!is_uniform_condition(ctrl_block, uniformity)) {
                    // Mark as alive - cannot speculate
                    mark_live(instr);
                }
            }

            // Rule R4: Convergent Operation Constraints
            if (is_convergent_operation(instr)) {
                // Cannot add control dependencies
                mark_convergent_boundary(instr);
            }

            // Rule R5: Speculative Execution Limits
            if (is_speculative_candidate(instr)) {
                if (depends_on_divergent_condition(instr, uniformity)) {
                    error("Cannot speculate instruction dependent on divergent condition");
                    return false;
                }
            }
        }
    }

    return true;
}
```

### Warp Specialization Constraint Validation (SM 90)

```c
// From sub_36E9630 @ 0x36e9630: warp specialization constraints
typedef struct {
    uint32_t weight_stationary : 2;    // bits 0-1
    uint32_t cta_group : 1;             // bit 1
    uint32_t scale_vec : 2;             // bits 2-3
    uint32_t reserved : 2;              // bits 4-5
    uint32_t kind : 3;                  // bits 6-8
} MMAttributes;

bool validate_warp_specialization(MMAttributes attrs) {

    // Constraint: cta_group::2 + weight_stationary incompatible
    if ((attrs.cta_group == 1) && (attrs.weight_stationary == 3)) {
        error("cta_group::2 is not supported with weight stationary");
        return false;
    }

    // Constraint: mxf8f6f4/fp4 cannot use weight stationary
    uint32_t kind_mxf8f6f4 = 2;  // bits [6:8] == 010
    uint32_t kind_fp4 = 7;        // bits [6:8] == 111

    if ((attrs.weight_stationary != 0) &&
        ((attrs.kind == kind_mxf8f6f4) || (attrs.kind == kind_fp4))) {
        error("Cannot use weight stationary with mxf8f6f4 and fp4 types");
        return false;
    }

    // Constraint: scale_vec incompatibilities by type
    enum KindType { MXFNVF4 = 0, F8F6F4 = 1, MXFXF6F4 = 2, F16 = 3,
                    I8 = 4, TF32 = 5, MXFX = 7 };

    switch(attrs.kind) {
        case MXFNVF4:  // mxf4nvf4
            // Cannot use scale_vec::1X
            if (attrs.scale_vec == 0) {
                error("Cannot use 1X as scale_vec for mxf4nvf4");
                return false;
            }
            break;

        case MXFXF6F4:  // mxf8f6f4
            // Cannot use scale_vec::2X or 4X
            if (attrs.scale_vec == 2 || attrs.scale_vec == 3) {
                error("Cannot use 2X or 4X as scale_vec for mxf8f6f4");
                return false;
            }
            break;

        case MXFX:  // mxf4
            // Cannot use scale_vec::1X or 4X
            if (attrs.scale_vec == 0 || attrs.scale_vec == 3) {
                error("Cannot use 1X or 4X as scale_vec for mxf4");
                return false;
            }
            break;

        case F16:
            // F16 is most flexible - no restrictions
            break;
    }

    return true;
}
```

### Divergence Source Detection Algorithm (sub_920430 @ 0x920430)

```c
typedef struct {
    const char* name;
    uint32_t class_code;
    bool is_divergent;
} DivergenceSource;

const DivergenceSource sources[] = {
    {"threadIdx.x", 0, true},
    {"threadIdx.y", 0, true},
    {"threadIdx.z", 0, true},
    {"blockDim.x", 1, false},
    {"blockDim.y", 1, false},
    {"blockDim.z", 1, false},
    {"blockIdx.x", 2, false},  // context-dependent
    {"blockIdx.y", 2, false},
    {"blockIdx.z", 2, false},
    {"gridDim.x", 3, false},
    {"gridDim.y", 3, false},
    {"gridDim.z", 3, false},
    {"warpSize", 4, false},
};

uint32_t classify_divergence_source(Instruction* instr) {
    // Extract operand attributes
    uint32_t attr = get_operand_attribute(instr);

    // Compare against known CUDA built-ins
    for (int i = 0; i < ARRAY_SIZE(sources); i++) {
        if (matches_builtin(attr, sources[i].name)) {
            return sources[i].class_code;
        }
    }

    return DIVERGENCE_CLASS_UNKNOWN;
}
```

### ADCE Integration with Divergence Constraints (sub_2ADCE40 @ 0x2adce40, sub_30ADAE0 @ 0x30adae0)

```c
// Aggressive Dead Code Elimination respecting divergence constraints
typedef struct {
    Instruction* instr;
    bool is_live;
    bool has_side_effect;
    bool is_divergent;
    bool depends_on_divergent_condition;
} LivenessInfo;

bool adce_safe_elimination(Instruction* instr, UniformityInfo* uniformity) {
    LivenessInfo info = {instr, false, false, false, false};

    // Phase 1: Identify side effects
    info.has_side_effect = has_side_effects(instr);

    // Phase 2: Determine divergence context
    info.is_divergent = is_in_divergent_region(instr, uniformity);
    info.depends_on_divergent_condition =
        is_data_dependent_on_divergent_value(instr, uniformity) ||
        is_control_dependent_on_divergent_branch(instr, uniformity);

    // Phase 3: Apply safety rules before elimination

    // Safety Rule 1: Cannot eliminate side effects in divergent code
    if (info.is_divergent && info.has_side_effect) {
        error("Cannot eliminate instruction with side effects in divergent region");
        info.is_live = true;
        return false;  // Mark as must-keep
    }

    // Safety Rule 2: Preserve memory operations in divergent regions
    if ((is_store(instr) || is_atomic(instr)) && info.is_divergent) {
        info.is_live = true;
        return false;
    }

    // Safety Rule 3: Control dependence safety
    if (info.depends_on_divergent_condition) {
        info.is_live = true;
        return false;
    }

    // Safety Rule 4: Convergent operation constraints
    if (is_convergent_operation(instr)) {
        // Check if adding control dependencies would violate semantics
        if (would_add_control_dependency(instr)) {
            info.is_live = true;
            return false;
        }
    }

    // Safe to eliminate
    return true;
}
```

---

## Validation Phases

### Phase 1: IR Parse and Construction
- Valid syntax validation
- Reference resolution (variables/functions exist)
- Type consistency checks
- Circular definition detection (abort on found)

### Phase 2: SSA Construction
- Phi insertion phase (iterative dominance frontier algorithm)
- Dominance property validation
- Variable renaming verification
- Phi node operand count validation

### Phase 3: Optimization Passes
- Pass dependency verification (circular dependency detection aborts)
- Analysis invalidation tracking
- IR preservation checks after each pass
- No undefined behavior introduction

### Phase 4: Register Allocation
- Virtual register allocation completion
- Constraint satisfaction (alignment, bank conflicts)
- Spill code validity
- Coloring conflict detection

### Phase 5: Instruction Selection
- All IR operations mapped to PTX instructions
- Target architecture support verification
- Type compatibility confirmation
- Precision requirements met

### Phase 6: Code Generation
- All instructions valid for target SM
- Memory access validity
- Synchronization correctness
- No undefined behavior

### Phase 7: Pre-Emission Validation
- PTX syntax validation (via ptxas)
- Register usage within limits
- Shared memory allocation validity
- Occupancy requirements
- Architecture constraints

---

## Validation Constraints Matrix

| Constraint | Detection Point | Severity | Recovery |
|-----------|-----------------|----------|----------|
| 64-bit register alignment | Register allocation | ERROR | Abort |
| Bank conflict (same 4-byte bank) | Register allocation constraints | WARNING | Skip optimization |
| Predicate count > 8 | Register allocation | ERROR | Reduce register pressure |
| SSA violation | SSA construction | ERROR | Abort |
| Type mismatch | IR validation | ERROR | Abort |
| Divergent store in dead code | ADCE + Divergence analysis | ERROR | Preserve code |
| cta_group::2 + weight stationary | Instruction selection | ERROR | Abort |
| FP4 on SM <100 | Architecture check | ERROR | Abort |
| TMA on SM <90 | Architecture check | ERROR | Abort |
| Undefined variable use | SSA construction | ERROR | Abort |

---

## Error Recovery Strategies

### Graceful Degradation (Non-Fatal)

**Bank Conflict Detection**:
1. Detect potential conflict during register allocation constraint phase
2. Skip optimization that would cause conflict
3. Proceed with default allocation
4. Report in verbose mode only

**Speculative Optimization Rejection**:
1. Attempt speculative transformation
2. Detect violation of divergence constraint
3. Skip transformation without reporting error
4. Continue compilation

### Hard Failures (Compilation Abort)

- SSA violation detected
- Type safety violation
- Undefined variable use
- Architectural impossibility (FP4 on SM 80)
- Register exhaustion (cannot allocate even with spilling)
- Infinite spill recursion

---

## Compiler Function Addresses

| Function | Address | Purpose |
|----------|---------|---------|
| Divergence source classification | 0x920430 | Classify threadIdx/blockIdx/blockDim |
| Thread index analysis | 0x6a49a0 | Complex divergence in conditionals |
| ADCE main driver | 0x2adce40 | Orchestrate dead code elimination |
| ADCE core algorithm | 0x30adae0 | Liveness and dependence analysis |
| Syncthreads registration | 0x90aee0 | Register __syncthreads as intrinsic |
| Syncthreads detection | 0xa91130 | Detect cuda.syncthreads in call chains |
| Uniformity pass requirement | 0x2310760 | Print uniformity analysis results |
| Uniformity pass driver | 0x2377300 | Uniformity pass orchestration |
| Warp group assignment | 0x35f3330 | Bit 1 extraction for cta_group decision |
| Barrier operation handling | 0x35f4e30 | Encode arrive/wait operations |
| TMA expect_tx operation | 0x35f4080 | Producer-specific barrier operation |
| cp.async.bulk.tensor pattern | 0xa8e250 | Async copy operation matching |
| Weight stationary validation | 0x36e9630 | Constraint enforcement (lines 169-175) |

---

## Shared Memory Bank Configuration

```
Bank Count:              32 independent banks per SM
Bank Width:              4 bytes per bank
Cache Line:              128 bytes (covers all 32 banks)
Address Formula:         bank_index = (address % 128) / 4
Serialization Penalty:   32 cycles (full serialization)
Broadcast Mechanism:     No penalty for uniform access
Stride Analysis:         stride_banks = stride % 32
```

---

## SM Version Compatibility

| Feature | SM 70 | SM 80 | SM 90 | SM 100 |
|---------|-------|-------|-------|--------|
| Tensor cores | YES | YES | YES | YES |
| Weight stationary MMA | NO | NO | YES | YES |
| TMA (Tensor Memory Accelerator) | NO | NO | YES | YES |
| Warp specialization (cta_group) | NO | NO | YES | YES |
| FP4 format | NO | NO | NO | YES |
| 2:4 sparsity | NO | NO | NO | YES |
| Cluster operations | NO | NO | YES | YES |
| mbarrier.expect_tx | NO | NO | YES | YES |

---

## Scale Vector Constraints by TMA Format

| Format | 1X | 2X | 4X | Constraint |
|--------|----|----|----|----|
| mxf4nvf4 | ERROR | VALID | VALID | Cannot use 1X |
| f8f6f4 | VALID | ERROR | ERROR | Cannot use 2X or 4X |
| mxf8f6f4 | VALID | ERROR | ERROR | Cannot use 2X or 4X |
| mxf4 | ERROR | VALID | ERROR | Cannot use 1X or 4X |
| f16 | VALID | VALID | VALID | Most flexible |

---

## Divergence Propagation Flow

```
ThreadIdx Value
      ↓
Mark as DIVERGENT (Phase 1)
      ↓
Forward Propagate to Users (Phase 2)
      ↓
Propagate Through Control Dependencies (Phase 3)
      ↓
Detect Convergence Points (Phase 4)
      ↓
Output Divergence Map for ADCE
```

**Convergence Point Types**:
- Explicit: __syncthreads() calls
- Structural: Post-dominator tree analysis
- Implicit: Block boundaries
- Functional: Return instructions
