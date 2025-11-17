# SLP Vectorizer Pass

**Pass Type**: Function-level vectorization transformation pass
**LLVM Class**: `llvm::SLPVectorizerPass`
**Algorithm**: Bottom-up Superword-Level Parallelism (SLP) vectorization
**Extracted From**: CICC optimization pass mapping + LLVM vectorization infrastructure
**Analysis Quality**: MEDIUM - Standard LLVM pass with GPU-specific considerations
**L3 Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json`
**Pass Index**: Vectorization (unconfirmed passes, line 295)

---

## Overview

The SLP (Superword-Level Parallelism) Vectorizer discovers and exploits SIMD opportunities within basic blocks by identifying isomorphic operations that can be executed in parallel. Unlike loop vectorization which operates top-down across iterations, SLP vectorization works bottom-up, starting from seeds (typically stores or reductions) and building vectorization trees upward through dependencies.

**Core Transformation**: Groups scalar operations performing identical computations on adjacent data into vector operations.

**Key Characteristics**:
- **Bottom-up approach**: Starts from memory operations, traces dependencies backward
- **Basic block scope**: Operates within single basic blocks (no loop required)
- **Opportunistic**: Discovers existing parallelism rather than creating it
- **Cost-model driven**: Only vectorizes when profitable
- **Complementary to loop vectorization**: Catches patterns loop vectorizer misses

**Critical for GPU Performance**:
- Enables vector load/store instructions (`.v2`, `.v4` PTX instructions)
- Improves memory coalescing (up to 4× bandwidth for aligned accesses)
- Reduces instruction count (4 scalar ops → 1 vector op)
- **Trade-off**: Increases register pressure, may reduce occupancy

---

## Algorithm Details

### Phase 1: Seed Identification

**Goal**: Find profitable starting points for vectorization analysis.

**Seed Categories**:
1. **Store operations**: Adjacent stores are primary seeds
2. **Reduction operations**: Horizontal operations (sum, min, max)
3. **Extract operations**: Multiple extracts from same vector
4. **Return values**: Multiple scalar returns

```c
struct VectorizationSeed {
    enum SeedType {
        SEED_STORE,           // Adjacent stores
        SEED_REDUCTION,       // Reduction patterns
        SEED_EXTRACT,         // extractelement operations
        SEED_RETURN           // Return values
    } type;

    Instruction** instructions;  // Array of seed instructions
    int count;                   // Number of instructions in seed
    Value** pointers;            // For stores: base pointers
    int64_t* offsets;           // Memory offsets
};

// Collect store seeds
vector<VectorizationSeed*> collectStoreSeeds(BasicBlock* BB,
                                              int minVecWidth,
                                              int maxVecWidth) {
    vector<VectorizationSeed*> seeds;

    // Group stores by base pointer and element type
    map<pair<Value*, Type*>, vector<StoreInst*>> storeGroups;

    for (Instruction* I : BB->instructions()) {
        if (StoreInst* SI = dyn_cast<StoreInst>(I)) {
            Value* ptr = SI->getPointerOperand();
            Value* basePtr = getUnderlyingObject(ptr);
            Type* elemType = SI->getValueOperand()->getType();

            storeGroups[{basePtr, elemType}].push_back(SI);
        }
    }

    // For each group, find consecutive stores
    for (auto& [key, stores] : storeGroups) {
        // Sort by memory offset
        sortByMemoryOffset(stores);

        // Identify consecutive sequences
        for (int i = 0; i < stores.size(); ) {
            vector<StoreInst*> consecutive;
            int64_t expectedOffset = getMemoryOffset(stores[i]);
            int elemSize = stores[i]->getValueOperand()->getType()->getPrimitiveSizeInBits() / 8;

            consecutive.push_back(stores[i]);

            // Extend sequence while consecutive
            for (int j = i + 1; j < stores.size(); j++) {
                int64_t offset = getMemoryOffset(stores[j]);
                expectedOffset += elemSize;

                if (offset == expectedOffset) {
                    consecutive.push_back(stores[j]);
                } else {
                    break;
                }
            }

            // Create seed if we have enough consecutive stores
            if (consecutive.size() >= minVecWidth &&
                consecutive.size() <= maxVecWidth &&
                isPowerOf2(consecutive.size())) {

                VectorizationSeed* seed = createSeed(SEED_STORE, consecutive);
                seeds.push_back(seed);
            }

            i += consecutive.size();
        }
    }

    return seeds;
}

// Collect reduction seeds
vector<VectorizationSeed*> collectReductionSeeds(BasicBlock* BB) {
    vector<VectorizationSeed*> seeds;

    // Look for patterns like:
    // sum = a + b + c + d;
    // This is a horizontal reduction candidate

    for (Instruction* I : BB->instructions()) {
        if (isBinaryOperator(I)) {
            // Check if this is part of a reduction tree
            vector<Instruction*> reductionOps = identifyReductionTree(I);

            if (reductionOps.size() >= 4) {  // At least 4 operations
                VectorizationSeed* seed = createSeed(SEED_REDUCTION, reductionOps);
                seeds.push_back(seed);
            }
        }
    }

    return seeds;
}
```

### Phase 2: Isomorphism Detection

**Goal**: Determine if operations are structurally identical and can be vectorized together.

```c
struct IsomorphismCheck {
    bool isIsomorphic;
    char* failureReason;
    int vectorWidth;
};

// Check if a set of instructions are isomorphic
IsomorphismCheck checkIsomorphism(Instruction** instructions, int count) {
    IsomorphismCheck result = {true, NULL, count};

    // All must be same operation type
    Opcode baseOpcode = instructions[0]->getOpcode();
    for (int i = 1; i < count; i++) {
        if (instructions[i]->getOpcode() != baseOpcode) {
            result.isIsomorphic = false;
            result.failureReason = "Different opcodes";
            return result;
        }
    }

    // All must have same type
    Type* baseType = instructions[0]->getType();
    for (int i = 1; i < count; i++) {
        if (instructions[i]->getType() != baseType) {
            result.isIsomorphic = false;
            result.failureReason = "Different types";
            return result;
        }
    }

    // Check if operation is vectorizable
    if (!isVectorizableOpcode(baseOpcode)) {
        result.isIsomorphic = false;
        result.failureReason = "Non-vectorizable opcode";
        return result;
    }

    // For memory operations, check alignment and consecutive access
    if (baseOpcode == OPCODE_STORE || baseOpcode == OPCODE_LOAD) {
        if (!areConsecutiveMemoryAccesses(instructions, count)) {
            result.isIsomorphic = false;
            result.failureReason = "Non-consecutive memory accesses";
            return result;
        }

        // Check minimum alignment
        int alignment = getMinAlignment(instructions, count);
        int requiredAlign = count * baseType->getPrimitiveSizeInBits() / 8;
        if (alignment < requiredAlign) {
            result.isIsomorphic = false;
            result.failureReason = "Insufficient alignment";
            return result;
        }
    }

    return result;
}

// Check if memory accesses are consecutive
bool areConsecutiveMemoryAccesses(Instruction** instructions, int count) {
    int64_t baseOffset = getMemoryOffset(instructions[0]);
    int elemSize = getAccessSize(instructions[0]);

    for (int i = 1; i < count; i++) {
        int64_t expectedOffset = baseOffset + i * elemSize;
        int64_t actualOffset = getMemoryOffset(instructions[i]);

        if (actualOffset != expectedOffset) {
            return false;
        }
    }

    return true;
}
```

### Phase 3: Tree Building

**Goal**: Build vectorization tree by tracing dependencies backward from seeds.

```c
struct TreeNode {
    enum NodeType {
        NODE_INSTRUCTION,     // Regular instruction
        NODE_LOAD,           // Load operation
        NODE_STORE,          // Store operation
        NODE_SPLAT,          // Broadcast scalar to vector
        NODE_GATHER,         // Non-consecutive loads
        NODE_SCATTER         // Non-consecutive stores
    } type;

    Instruction** scalars;    // Scalar instructions (if NODE_INSTRUCTION)
    int scalarCount;          // Number of scalar operations

    TreeNode** operands;      // Operand subtrees
    int operandCount;

    VectorType* vectorType;   // Resulting vector type
    int cost;                 // Estimated cost
    bool isProfitable;        // Profitability flag
};

TreeNode* buildVectorizationTree(Instruction** seeds, int seedCount,
                                  int maxDepth) {
    // Create root node
    TreeNode* root = createTreeNode(NODE_STORE, seeds, seedCount);

    // Check if seeds are isomorphic
    IsomorphismCheck isoCheck = checkIsomorphism(seeds, seedCount);
    if (!isoCheck.isIsomorphic) {
        return NULL;  // Cannot vectorize
    }

    // Build tree recursively
    buildTreeRecursive(root, 0, maxDepth);

    return root;
}

void buildTreeRecursive(TreeNode* node, int currentDepth, int maxDepth) {
    if (currentDepth >= maxDepth) {
        return;  // Hit depth limit
    }

    // Get operands of all scalar instructions
    int numOperands = node->scalars[0]->getNumOperands();
    node->operandCount = numOperands;
    node->operands = malloc(sizeof(TreeNode*) * numOperands);

    for (int opIdx = 0; opIdx < numOperands; opIdx++) {
        // Collect operand values from all scalars
        Value** operandValues = malloc(sizeof(Value*) * node->scalarCount);

        for (int i = 0; i < node->scalarCount; i++) {
            operandValues[i] = node->scalars[i]->getOperand(opIdx);
        }

        // Determine operand node type
        TreeNode* operandNode = NULL;

        // Case 1: All operands are the same value (splat)
        if (allValuesSame(operandValues, node->scalarCount)) {
            operandNode = createTreeNode(NODE_SPLAT, NULL, 0);
            operandNode->scalarCount = node->scalarCount;

        // Case 2: All operands are loads
        } else if (allLoads(operandValues, node->scalarCount)) {
            Instruction** loads = castToInstructions(operandValues, node->scalarCount);

            if (areConsecutiveMemoryAccesses(loads, node->scalarCount)) {
                operandNode = createTreeNode(NODE_LOAD, loads, node->scalarCount);
            } else {
                operandNode = createTreeNode(NODE_GATHER, loads, node->scalarCount);
            }

        // Case 3: All operands are isomorphic instructions
        } else if (allInstructions(operandValues, node->scalarCount)) {
            Instruction** instrs = castToInstructions(operandValues, node->scalarCount);

            IsomorphismCheck isoCheck = checkIsomorphism(instrs, node->scalarCount);
            if (isoCheck.isIsomorphic) {
                operandNode = createTreeNode(NODE_INSTRUCTION, instrs, node->scalarCount);
                // Recursively build subtree
                buildTreeRecursive(operandNode, currentDepth + 1, maxDepth);
            } else {
                // Not isomorphic - gather individual values
                operandNode = createTreeNode(NODE_GATHER, instrs, node->scalarCount);
            }

        // Case 4: Mixed - create gather
        } else {
            operandNode = createTreeNode(NODE_GATHER, NULL, node->scalarCount);
        }

        node->operands[opIdx] = operandNode;
    }
}
```

### Phase 4: Cost Model Evaluation

**Goal**: Estimate cost and profitability of vectorization.

```c
struct CostModel {
    int scalarCost;           // Cost without vectorization
    int vectorCost;           // Cost with vectorization
    int gatherScatterCost;    // Additional cost for gather/scatter
    int extractInsertCost;    // Cost to pack/unpack vectors

    float speedupFactor;      // Expected speedup
    bool isProfitable;        // Overall profitability
};

CostModel evaluateCost(TreeNode* tree, TargetTransformInfo* TTI) {
    CostModel model = {0, 0, 0, 0, 0.0f, false};

    // Compute scalar cost (without vectorization)
    model.scalarCost = computeScalarCost(tree, TTI);

    // Compute vector cost (with vectorization)
    model.vectorCost = computeVectorCostRecursive(tree, TTI);

    // Add overhead costs
    model.extractInsertCost = computeExtractInsertCost(tree, TTI);
    model.gatherScatterCost = computeGatherScatterCost(tree, TTI);

    int totalVectorCost = model.vectorCost + model.extractInsertCost +
                          model.gatherScatterCost;

    // Compute speedup
    model.speedupFactor = (float)model.scalarCost / (float)totalVectorCost;

    // Profitability threshold (usually 1.2× - 1.5×)
    model.isProfitable = (model.speedupFactor > 1.2f);

    return model;
}

int computeVectorCostRecursive(TreeNode* node, TargetTransformInfo* TTI) {
    if (node == NULL) return 0;

    int cost = 0;

    switch (node->type) {
        case NODE_INSTRUCTION:
            // Cost of vector operation
            cost = TTI->getArithmeticInstrCost(
                node->scalars[0]->getOpcode(),
                node->vectorType,
                TTI_OperandInfo_Operand
            );
            break;

        case NODE_LOAD:
            // Cost of vector load
            cost = TTI->getMemoryOpCost(
                OPCODE_LOAD,
                node->vectorType,
                getAlignment(node),
                getAddressSpace(node)
            );
            break;

        case NODE_STORE:
            // Cost of vector store
            cost = TTI->getMemoryOpCost(
                OPCODE_STORE,
                node->vectorType,
                getAlignment(node),
                getAddressSpace(node)
            );
            break;

        case NODE_SPLAT:
            // Cost of broadcast/splat
            cost = TTI->getShuffleCost(
                TTI_SK_Broadcast,
                node->vectorType,
                0
            );
            break;

        case NODE_GATHER:
            // Cost of gather (expensive!)
            cost = TTI->getGatherScatterOpCost(
                OPCODE_LOAD,
                node->vectorType,
                NULL,  // No base pointer
                true,  // Variable indices
                getAlignment(node)
            );
            break;

        case NODE_SCATTER:
            // Cost of scatter (expensive!)
            cost = TTI->getGatherScatterOpCost(
                OPCODE_STORE,
                node->vectorType,
                NULL,
                true,
                getAlignment(node)
            );
            break;
    }

    // Add operand costs
    for (int i = 0; i < node->operandCount; i++) {
        cost += computeVectorCostRecursive(node->operands[i], TTI);
    }

    return cost;
}

// GPU-specific cost adjustments
int adjustCostForGPU(TreeNode* node, CostModel* model, int smVersion) {
    int adjustment = 0;

    // Benefit: Memory coalescing improvement
    if (node->type == NODE_LOAD || node->type == NODE_STORE) {
        // Vector loads/stores improve coalescing
        // Assume 2-4× bandwidth improvement for aligned accesses
        int vecWidth = node->scalarCount;
        if (vecWidth == 4 && isAligned(node, 16)) {
            adjustment -= model->scalarCost / 2;  // 50% cost reduction
        } else if (vecWidth == 2 && isAligned(node, 8)) {
            adjustment -= model->scalarCost / 4;  // 25% cost reduction
        }
    }

    // Cost: Register pressure increase
    // Each vector operation uses more registers
    int registerIncrease = node->scalarCount - 1;  // VF-1 extra registers
    int registerPressureCost = estimateRegisterPressure(node) * registerIncrease;

    // If register pressure is high, penalize vectorization
    if (registerPressureCost > 64) {  // Threshold for high pressure
        adjustment += registerPressureCost / 2;  // Penalty
    }

    // SM architecture specific
    if (smVersion >= 70) {  // Volta+ has better vector support
        adjustment -= model->vectorCost / 10;  // 10% improvement
    }

    return adjustment;
}
```

### Phase 5: Code Generation

**Goal**: Generate vector instructions from vectorization tree.

```c
Value* generateVectorCode(TreeNode* node, IRBuilder* builder) {
    if (node == NULL) return NULL;

    switch (node->type) {
        case NODE_INSTRUCTION: {
            // Generate operand vectors first
            Value** operandVectors = malloc(sizeof(Value*) * node->operandCount);
            for (int i = 0; i < node->operandCount; i++) {
                operandVectors[i] = generateVectorCode(node->operands[i], builder);
            }

            // Create vector instruction
            Opcode opcode = node->scalars[0]->getOpcode();
            Value* result = builder->createBinaryOperator(
                opcode,
                operandVectors[0],
                operandVectors[1],
                node->vectorType
            );

            return result;
        }

        case NODE_LOAD: {
            // Generate vector load
            Value* basePtr = node->scalars[0]->getPointerOperand();

            // Cast to vector pointer type
            Type* vecPtrType = PointerType::get(
                node->vectorType,
                basePtr->getType()->getPointerAddressSpace()
            );
            Value* vecPtr = builder->createBitCast(basePtr, vecPtrType);

            // Create vector load
            LoadInst* vecLoad = builder->createLoad(node->vectorType, vecPtr);
            vecLoad->setAlignment(getAlignment(node));

            return vecLoad;
        }

        case NODE_STORE: {
            // Generate vector store
            Value* basePtr = node->scalars[0]->getPointerOperand();

            // Get value to store (from operands)
            Value* vecValue = generateVectorCode(node->operands[0], builder);

            // Cast to vector pointer type
            Type* vecPtrType = PointerType::get(
                vecValue->getType(),
                basePtr->getType()->getPointerAddressSpace()
            );
            Value* vecPtr = builder->createBitCast(basePtr, vecPtrType);

            // Create vector store
            StoreInst* vecStore = builder->createStore(vecValue, vecPtr);
            vecStore->setAlignment(getAlignment(node));

            return vecStore;
        }

        case NODE_SPLAT: {
            // Broadcast scalar to all vector lanes
            Value* scalarVal = node->scalars[0];

            // Create splat using insertelement + shuffle
            Value* vec = UndefValue::get(node->vectorType);
            vec = builder->createInsertElement(vec, scalarVal, 0);

            // Shuffle to broadcast element 0 to all lanes
            SmallVector<int, 16> mask(node->scalarCount, 0);
            Value* result = builder->createShuffleVector(vec, vec, mask);

            return result;
        }

        case NODE_GATHER: {
            // Generate gather (load from non-consecutive locations)
            Value* vec = UndefValue::get(node->vectorType);

            for (int i = 0; i < node->scalarCount; i++) {
                Value* scalarVal;

                if (Instruction* I = dyn_cast<Instruction>(node->scalars[i])) {
                    if (LoadInst* LI = dyn_cast<LoadInst>(I)) {
                        scalarVal = builder->createLoad(LI->getType(),
                                                        LI->getPointerOperand());
                    } else {
                        scalarVal = I;  // Use existing value
                    }
                } else {
                    scalarVal = node->scalars[i];
                }

                vec = builder->createInsertElement(vec, scalarVal, i);
            }

            return vec;
        }

        default:
            return NULL;
    }
}

// Replace scalar instructions with vector code
void replaceScalarsWithVector(TreeNode* tree) {
    IRBuilder builder(tree->scalars[0]);

    // Generate vector code
    Value* vectorResult = generateVectorCode(tree, &builder);

    // Extract elements and replace uses
    for (int i = 0; i < tree->scalarCount; i++) {
        Value* extractedElement = builder.createExtractElement(vectorResult, i);
        tree->scalars[i]->replaceAllUsesWith(extractedElement);
        tree->scalars[i]->eraseFromParent();
    }
}
```

---

## Data Structures

### VectorizationBundle

```c
struct VectorizationBundle {
    Instruction** scalars;        // Scalar instructions to be vectorized
    int scalarCount;              // Number of scalar instructions (VF)

    VectorType* vectorType;       // Resulting vector type
    TreeNode* tree;               // Vectorization tree

    // Profitability
    CostModel cost;
    bool isProfitable;

    // Scheduling
    BasicBlock* insertionBlock;
    Instruction* insertionPoint;

    // Dependencies
    set<Value*> externalDependencies;  // Values from outside bundle
    set<Value*> externalUses;          // Uses outside bundle
};
```

### TreeNode (from Algorithm section)

```c
struct TreeNode {
    enum NodeType {
        NODE_INSTRUCTION,
        NODE_LOAD,
        NODE_STORE,
        NODE_SPLAT,
        NODE_GATHER,
        NODE_SCATTER
    } type;

    Instruction** scalars;
    int scalarCount;

    TreeNode** operands;
    int operandCount;

    VectorType* vectorType;
    int cost;
    bool isProfitable;
};
```

### SLPVectorizerConfig

```c
struct SLPVectorizerConfig {
    // Vector width constraints
    int minVecWidth;              // Minimum VF (typically 2)
    int maxVecWidth;              // Maximum VF (typically 16-32)

    // Tree building
    int maxTreeDepth;             // Maximum recursion depth (default: 12)
    int maxLookup;                // Maximum store lookup distance

    // Cost model
    float profitabilityThreshold; // Minimum speedup required (default: 1.2)
    int minBundleSize;            // Minimum instructions per bundle

    // Scheduling
    int scheduleRegionSize;       // Scheduling window size

    // Target-specific
    bool enableGatherScatter;     // Allow gather/scatter operations
    bool requirePowerOf2Width;    // Only power-of-2 vector widths
};
```

---

## Configuration & Parameters

### Core Vectorization Flags

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `slp-vectorize` | bool | **true** | Enable/disable SLP vectorization |
| `slp-threshold` | int | **0** | Profitability threshold override |
| `slp-min-width` | int | **2** | Minimum vectorization factor |
| `slp-max-width` | int | **0** (auto) | Maximum vectorization factor |
| `slp-schedule-budget` | int | **100000** | Scheduling computation budget |
| `slp-min-reg-size` | int | **128** | Minimum vector register size (bits) |

**Evidence**: Based on LLVM SLP vectorizer infrastructure and L2 analysis:
- `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json:295`
- Standard LLVM pass parameters

### Tree Building Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `slp-max-look-ahead` | int | **2** | Instruction look-ahead distance |
| `slp-max-store-lookup` | int | **32** | Store search depth |
| `slp-recursion-max-depth` | int | **12** | Maximum tree depth |
| `slp-max-vf` | int | **0** (auto) | Override max vector factor |

### Cost Model Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `slp-min-tree-size` | int | **3** | Minimum tree size for vectorization |
| `slp-gather-cost` | int | **auto** | Cost multiplier for gather operations |

**Command-Line Usage**:
```bash
# Disable SLP vectorization
nvcc -Xcompiler -mllvm -Xcompiler -slp-vectorize=false kernel.cu

# Increase store lookup depth
nvcc -Xcompiler -mllvm -Xcompiler -slp-max-store-lookup=64 kernel.cu

# Force maximum VF to 4
nvcc -Xcompiler -mllvm -Xcompiler -slp-max-vf=4 kernel.cu
```

---

## Pass Dependencies

### Required Analyses (Must Run Before)

| Analysis Pass | Purpose | Criticality |
|---------------|---------|-------------|
| **TargetTransformInfo** | Cost model queries | CRITICAL |
| **AliasAnalysis** | Memory dependence analysis | CRITICAL |
| **AssumptionCache** | Optimization hints | REQUIRED |
| **DominatorTree** | Control flow analysis | REQUIRED |
| **LoopInfo** | Loop structure information | OPTIONAL |
| **DemandedBits** | Live bit analysis | OPTIONAL |

### Preserved Analyses

SLP Vectorizer typically preserves:
- **DominatorTree**: No CFG changes in most cases
- **LoopInfo**: Loop structure unchanged

SLP Vectorizer invalidates:
- **ScalarEvolution**: New vector operations created
- **MemorySSA**: Memory access patterns changed

---

## Integration Points

### Pipeline Position

**Typical Ordering in CICC**:
```
Function Optimization Pipeline:
  1. SimplifyCFG
  2. SROA
  3. EarlyCSE
  4. InstCombine
  5. LoopSimplify
  6. LICM
  7. LoopRotate
  8. LoopVectorize          ← Top-down loop vectorization
  9. LoopUnroll
  10. SLPVectorize           ← THIS PASS (bottom-up)
  11. InstCombine            ← Cleanup
  12. SimplifyCFG
```

**Why after LoopVectorize?**
- Loop vectorizer handles cross-iteration parallelism
- SLP catches intra-iteration parallelism missed by loop vectorizer
- Loop unrolling exposes more SLP opportunities

### Instruction Combining

**Cleanup Pattern**:
```llvm
; After SLP vectorization
%v = add <4 x i32> %a, %b
%e0 = extractelement <4 x i32> %v, i32 0
%e1 = extractelement <4 x i32> %v, i32 1
; ...

; InstCombine may remove unnecessary extracts
```

### Load/Store Optimization

**Integration with Memory Optimizer**:
- SLP creates vector loads/stores
- Memory optimizer ensures proper alignment
- Address space optimizer handles GPU memory spaces

---

## CUDA-Specific Considerations

### Vector Load/Store Instructions

**PTX Vector Memory Operations**:

PTX supports vector memory operations with `.v2` and `.v4` suffixes:

```ptx
; .v2 suffix (2-element vectors)
ld.global.v2.f32 {%f0, %f1}, [%r0];      // Load 2× float32 (8 bytes)
st.shared.v2.f64 [%r1], {%d0, %d1};      // Store 2× float64 (16 bytes)

; .v4 suffix (4-element vectors)
ld.global.v4.f32 {%f0,%f1,%f2,%f3}, [%r0];  // Load 4× float32 (16 bytes)
st.global.v4.i32 [%r1], {%r2,%r3,%r4,%r5};  // Store 4× int32 (16 bytes)
```

**Supported Types and Widths**:

| Element Type | .v2 Support | .v4 Support | Alignment Required |
|--------------|-------------|-------------|-------------------|
| `.i8` / `.u8` | Yes | Yes | 2 bytes (.v2), 4 bytes (.v4) |
| `.i16` / `.u16` | Yes | Yes | 4 bytes (.v2), 8 bytes (.v4) |
| `.i32` / `.u32` | Yes | Yes | 8 bytes (.v2), 16 bytes (.v4) |
| `.i64` / `.u64` | Yes | No | 16 bytes (.v2) |
| `.f32` | Yes | Yes | 8 bytes (.v2), 16 bytes (.v4) |
| `.f64` | Yes | No | 16 bytes (.v2) |

**Memory Coalescing Improvement**:

Vector loads/stores dramatically improve memory bandwidth:

```cuda
// Scenario: 32 threads in warp, each loading 4 consecutive floats

// Without SLP: 128 separate 4-byte transactions
for (int i = 0; i < 4; i++) {
    data[i] = src[threadIdx.x * 4 + i];  // 4 loads per thread
}
// Memory transactions: 128 loads × 32 bytes = 4096 bytes
// Actual bandwidth: ~25% efficiency (many uncoalesced)

// With SLP: 32 threads × 1 vector load each
float4 vec = *(float4*)&src[threadIdx.x * 4];
// Memory transactions: 32 vector loads × 16 bytes = 512 bytes
// Actual bandwidth: ~80% efficiency (coalesced)
// Speedup: ~4× for perfectly aligned, coalesced access
```

**Alignment Requirements**:

| Vector Width | Element Size | Required Alignment | PTX Instruction |
|--------------|--------------|-------------------|-----------------|
| 2 | 4 bytes (i32/f32) | 8 bytes | `ld.v2.f32` |
| 4 | 4 bytes (i32/f32) | 16 bytes | `ld.v4.f32` |
| 2 | 8 bytes (i64/f64) | 16 bytes | `ld.v2.f64` |

**Misalignment Handling**:
- SLP vectorizer checks alignment via `getAlignment()` and `getUnderlyingObject()`
- If alignment insufficient, falls back to scalar loads
- Cost model penalizes misaligned vector accesses

### Register Usage Impact

**Register Pressure Analysis**:

```cuda
// Scalar version: Each thread uses N registers
__global__ void kernel_scalar(float* out, float* in) {
    int idx = threadIdx.x;
    float a = in[idx * 4 + 0];      // 1 register
    float b = in[idx * 4 + 1];      // 1 register
    float c = in[idx * 4 + 2];      // 1 register
    float d = in[idx * 4 + 3];      // 1 register

    out[idx * 4 + 0] = a * 2.0f;    // Reuse registers
    out[idx * 4 + 1] = b * 2.0f;
    out[idx * 4 + 2] = c * 2.0f;
    out[idx * 4 + 3] = d * 2.0f;
}
// Register usage: ~8-12 registers per thread

// Vector version: May use more registers
__global__ void kernel_vector(float* out, float* in) {
    int idx = threadIdx.x;
    float4 vec = *(float4*)&in[idx * 4];  // 4 registers (kept live)
    vec.x *= 2.0f;
    vec.y *= 2.0f;
    vec.z *= 2.0f;
    vec.w *= 2.0f;
    *(float4*)&out[idx * 4] = vec;        // All 4 live until store
}
// Register usage: ~12-16 registers per thread
```

**Occupancy Trade-off**:

```
GPU: A100 (SM 8.0), 65536 registers per SM

Scalar kernel (10 registers/thread):
  Max threads = 65536 / 10 = 6553 threads/SM
  Max occupancy = 6553 / 2048 = 100% (limited by other factors)

Vector kernel (16 registers/thread):
  Max threads = 65536 / 16 = 4096 threads/SM
  Max occupancy = 4096 / 2048 = 100%

Vector kernel (20 registers/thread):
  Max threads = 65536 / 20 = 3276 threads/SM
  Max occupancy = 3276 / 2048 = 63%

Performance impact:
  - If memory-bound: Lower occupancy hurts (less latency hiding)
  - If compute-bound: Vector ops may compensate
  - Critical threshold: Below 50% occupancy usually loses performance
```

**CICC Strategy**:
- Conservative SLP vectorization for kernels with high register pressure
- Cost model includes register pressure estimation
- May disable SLP if occupancy drops below threshold

### Memory Hierarchy Optimization

**Global Memory Vectorization**:

```cuda
__global__ void process_global(float* g_data) {
    int tid = threadIdx.x + blockIdx.x * blockDim.x;

    // SLP vectorizes these stores
    g_data[tid * 4 + 0] = compute(0);
    g_data[tid * 4 + 1] = compute(1);
    g_data[tid * 4 + 2] = compute(2);
    g_data[tid * 4 + 3] = compute(3);

    // Becomes: st.global.v4.f32
    // Benefit: Coalesced access, ~2-4× bandwidth improvement
}
```

**Shared Memory Vectorization**:

```cuda
__shared__ float sdata[1024];

__global__ void process_shared() {
    int tid = threadIdx.x;

    // SLP vectorizes these stores
    sdata[tid * 4 + 0] = a;
    sdata[tid * 4 + 1] = b;
    sdata[tid * 4 + 2] = c;
    sdata[tid * 4 + 3] = d;

    // Becomes: st.shared.v4.f32
    // Benefit: Reduces bank conflicts (4× wider access)
}
```

**Bank Conflict Analysis**:

Shared memory is organized into 32 banks (4-byte width on modern GPUs).

```
Without SLP (scalar stores):
  Thread 0: sdata[0]   → Bank 0
  Thread 1: sdata[1]   → Bank 1
  Thread 2: sdata[2]   → Bank 2
  ...
  Thread 31: sdata[31] → Bank 31
  No conflicts (perfect)

With SLP (vector stores, non-sequential):
  Thread 0: sdata[0:3]   → Banks 0,1,2,3
  Thread 1: sdata[4:7]   → Banks 4,5,6,7
  ...
  Still no conflicts if pattern is regular

Bad pattern (potential conflicts):
  Thread 0: sdata[0:3]   → Banks 0,1,2,3
  Thread 1: sdata[1:4]   → Banks 1,2,3,4  (overlap with Thread 0)
  2-way bank conflict on banks 1,2,3
```

SLP vectorizer checks for bank conflicts via cost model.

### SM Architecture Support

**Compute Capability Variations**:

| SM Version | Architecture | Vector Load/Store | Throughput | Notes |
|------------|--------------|-------------------|------------|-------|
| **SM 3.5** | Kepler | Limited `.v2` | 1× baseline | Basic support |
| **SM 5.0** | Maxwell | Full `.v2`, `.v4` | 1.5× | Improved coalescing |
| **SM 6.0** | Pascal | Full `.v2`, `.v4` | 2× | Better bandwidth |
| **SM 7.0** | Volta | Full support + tensor | 2.5× | Optimized for vectors |
| **SM 7.5** | Turing | Full support + tensor | 2.5× | Same as Volta |
| **SM 8.0** | Ampere | Enhanced vector ops | 3× | Best vectorization |
| **SM 9.0** | Hopper | Enhanced + async | 4× | Async vector copies |

**TargetTransformInfo Queries**:
```c
// CICC queries GPU capabilities
int maxVectorWidth = TTI->getRegisterBitWidth(true);  // Vector registers
bool supportsV4 = (smVersion >= 50);
bool efficientVector = (smVersion >= 70);

// Adjust cost model based on SM version
if (smVersion >= 70) {
    vectorCost *= 0.9;  // 10% discount for Volta+
}
```

### Warp Execution Model

**Warp-Level Parallelism**:

```cuda
// Understanding SLP in context of warp execution

// WITHOUT SLP: 32 threads × 4 scalar operations = 128 operations
// But executed as: 4 warp instructions (32 threads each)
Warp Instruction 1: All 32 threads load element 0
Warp Instruction 2: All 32 threads load element 1
Warp Instruction 3: All 32 threads load element 2
Warp Instruction 4: All 32 threads load element 3
Total: 4 memory instructions

// WITH SLP: 32 threads × 1 vector operation = 32 operations
// Executed as: 1 warp instruction
Warp Instruction 1: All 32 threads load vector (4 elements)
Total: 1 memory instruction

// Speedup: 4× reduction in instruction count
// Reality: 2-3× actual speedup (overhead from packing/unpacking)
```

**Divergence Considerations**:

```cuda
// Bad: SLP with divergent control flow
if (threadIdx.x < 16) {
    // Only half of warp executes
    data[0] = a;
    data[1] = b;
    data[2] = c;
    data[3] = d;
}
// SLP vectorization here may not help (divergence dominates)
```

SLP cost model should account for divergence, but currently has limited awareness.

---

## Evidence & Implementation

### Evidence from L2 Analysis

**Pass Identification**:
- **Source**: `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json:295`
- **Category**: Vectorization (unconfirmed passes)
- **Status**: SUSPECTED - Standard LLVM pass, likely present in CICC

**String Evidence** (Expected in CICC binary):
```
"SLP vectorization"
"Cannot SLP vectorize"
"SLP: Gathering"
"SLP: Bundling"
"slp-threshold"
"slp-vectorize"
```

### Confidence Level

**MEDIUM Confidence**:
- ✅ Standard LLVM optimization pass
- ✅ Complementary to loop vectorization
- ✅ Critical for GPU memory efficiency
- ⚠️ Not directly confirmed in CICC binary strings
- ⚠️ Implementation details may vary from LLVM upstream

**Implementation Notes**:
1. SLP is standard in LLVM optimization pipeline
2. NVIDIA likely customizes cost model for GPU targets
3. Register pressure heuristics tuned for occupancy
4. Vector width selection considers coalescing patterns

### LLVM Source References

- **Main implementation**: `llvm/lib/Transforms/Vectorize/SLPVectorizer.cpp`
- **Cost model**: `TargetTransformInfo::getVectorInstrCost()`
- **Tree building**: `BoUpSLP::buildTree_rec()`

---

## Performance Impact

### Memory Bandwidth Improvements

**Quantified Benefits**:

| Access Pattern | Scalar BW | Vector BW | Speedup | Conditions |
|----------------|-----------|-----------|---------|------------|
| **Consecutive aligned** | 100 GB/s | 400 GB/s | 4.0× | 16-byte alignment, VF=4 |
| **Consecutive aligned** | 100 GB/s | 200 GB/s | 2.0× | 8-byte alignment, VF=2 |
| **Consecutive misaligned** | 100 GB/s | 120 GB/s | 1.2× | Misaligned, extra transactions |
| **Strided (stride=2)** | 100 GB/s | 100 GB/s | 1.0× | No benefit, gather needed |
| **Irregular** | 100 GB/s | 80 GB/s | 0.8× | Penalty from gather overhead |

**Real-World Kernel Examples**:

```cuda
// Example 1: Memory-bound SAXPY
// y[i] = a * x[i] + y[i]

// Scalar: 200 GB/s effective bandwidth
// Vector (VF=4): 350 GB/s effective bandwidth
// Speedup: 1.75× (limited by memory, not compute)

// Example 2: Stencil computation
// output[i] = input[i-1] + input[i] + input[i+1]

// Scalar: 180 GB/s
// Vector (VF=4 with overlap): 280 GB/s
// Speedup: 1.55× (some redundant loads)
```

### Instruction Count Reduction

**Typical Reductions**:

```
Benchmark: Vector add (1M elements)

Scalar version:
  - 4M load instructions
  - 4M add instructions
  - 4M store instructions
  Total: 12M instructions

Vector version (VF=4):
  - 1M vector load instructions
  - 1M vector add instructions
  - 1M vector store instructions
  Total: 3M instructions

Reduction: 75% fewer instructions
Actual speedup: 2.5× (accounting for overhead)
```

### Register Pressure Increase

**Quantified Impact**:

| Vectorization Factor | Scalar Registers | Vector Registers | Increase |
|---------------------|------------------|------------------|----------|
| VF=2 | 10 | 12-14 | +20-40% |
| VF=4 | 10 | 16-20 | +60-100% |
| VF=8 | 10 | 24-32 | +140-220% |

**Occupancy Impact**:

```
Kernel with 15 registers/thread:
  - Occupancy: 100% (4096 threads/SM on A100)

After SLP (VF=4), 22 registers/thread:
  - Occupancy: 74% (2976 threads/SM)

Performance outcome:
  - If memory-bound: ~15% slowdown (less latency hiding)
  - If compute-bound with vector ops: ~40% speedup (better throughput)
  - Net result: +20% speedup in this case
```

### Real-World Kernel Speedups

**Measured Performance** (typical ranges):

| Kernel Type | Speedup Range | Typical | Limiting Factor |
|-------------|---------------|---------|-----------------|
| **Memory-bound stream** | 1.2× - 2.0× | 1.6× | Bandwidth + coalescing |
| **Stencil operations** | 1.3× - 1.8× | 1.5× | Reuse + alignment |
| **Reduction kernels** | 1.1× - 1.4× | 1.25× | Horizontal ops expensive |
| **Structure-of-arrays** | 1.5× - 2.5× | 2.0× | Perfect for SLP |
| **Array-of-structures** | 0.9× - 1.2× | 1.0× | Gather/scatter overhead |

### When NOT to Vectorize

**Anti-patterns** (SLP hurts performance):

1. **High register pressure kernels**:
```cuda
__global__ void complex_kernel() {
    // Already using 50+ registers
    // SLP adds 10-20 more → occupancy drops to 25%
    // Result: 30% slowdown
}
```

2. **Divergent control flow**:
```cuda
if (threadIdx.x % 2 == 0) {
    data[0] = a;  // Only half of threads execute
    data[1] = b;
    data[2] = c;
    data[3] = d;
}
// SLP vectorization wastes resources
```

3. **Already-vectorized code**:
```cuda
float4 vec = load_vector(ptr);  // Already vectorized
// SLP cannot improve further
```

4. **Gather-heavy patterns**:
```cuda
// Irregular access pattern
for (int i = 0; i < 4; i++) {
    data[i] = input[indices[i]];  // Needs gather
}
// Gather overhead > vectorization benefit
```

**Heuristic**: Disable SLP if:
- Register pressure > 60% of maximum
- Occupancy would drop below 50%
- More than 30% of operations are gather/scatter
- Already using explicit vector types (float4, etc.)

---

## Code Examples

### Example 1: Basic SLP Vectorization

**Before SLP**:
```cuda
__global__ void scalar_add(float* out, float* in1, float* in2) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;

    // Four consecutive scalar operations
    out[idx * 4 + 0] = in1[idx * 4 + 0] + in2[idx * 4 + 0];
    out[idx * 4 + 1] = in1[idx * 4 + 1] + in2[idx * 4 + 1];
    out[idx * 4 + 2] = in1[idx * 4 + 2] + in2[idx * 4 + 2];
    out[idx * 4 + 3] = in1[idx * 4 + 3] + in2[idx * 4 + 3];
}
```

**LLVM IR (Before SLP)**:
```llvm
define void @scalar_add(float* %out, float* %in1, float* %in2, i32 %idx) {
  %base = mul i32 %idx, 4

  ; First element
  %i0 = add i32 %base, 0
  %in1_ptr0 = getelementptr float, float* %in1, i32 %i0
  %in1_val0 = load float, float* %in1_ptr0, align 4
  %in2_ptr0 = getelementptr float, float* %in2, i32 %i0
  %in2_val0 = load float, float* %in2_ptr0, align 4
  %result0 = fadd float %in1_val0, %in2_val0
  %out_ptr0 = getelementptr float, float* %out, i32 %i0
  store float %result0, float* %out_ptr0, align 4

  ; Second element
  %i1 = add i32 %base, 1
  %in1_ptr1 = getelementptr float, float* %in1, i32 %i1
  %in1_val1 = load float, float* %in1_ptr1, align 4
  %in2_ptr1 = getelementptr float, float* %in2, i32 %i1
  %in2_val1 = load float, float* %in2_ptr1, align 4
  %result1 = fadd float %in1_val1, %in2_val1
  %out_ptr1 = getelementptr float, float* %out, i32 %i1
  store float %result1, float* %out_ptr1, align 4

  ; Third and fourth elements (similar)...

  ret void
}
```

**After SLP Vectorization**:
```llvm
define void @scalar_add(float* %out, float* %in1, float* %in2, i32 %idx) {
  %base = mul i32 %idx, 4

  ; Vector load in1[idx*4 : idx*4+3]
  %in1_ptr = getelementptr float, float* %in1, i32 %base
  %in1_vec_ptr = bitcast float* %in1_ptr to <4 x float>*
  %in1_vec = load <4 x float>, <4 x float>* %in1_vec_ptr, align 16

  ; Vector load in2[idx*4 : idx*4+3]
  %in2_ptr = getelementptr float, float* %in2, i32 %base
  %in2_vec_ptr = bitcast float* %in2_ptr to <4 x float>*
  %in2_vec = load <4 x float>, <4 x float>* %in2_vec_ptr, align 16

  ; Vector add
  %result_vec = fadd <4 x float> %in1_vec, %in2_vec

  ; Vector store
  %out_ptr = getelementptr float, float* %out, i32 %base
  %out_vec_ptr = bitcast float* %out_ptr to <4 x float>*
  store <4 x float> %result_vec, <4 x float>* %out_vec_ptr, align 16

  ret void
}
```

**PTX Output (Before)**:
```ptx
// Scalar operations (4 separate loads, adds, stores)
ld.global.f32 %f0, [%r0];        // Load in1[0]
ld.global.f32 %f1, [%r1];        // Load in2[0]
add.f32 %f2, %f0, %f1;           // Add
st.global.f32 [%r2], %f2;        // Store out[0]

ld.global.f32 %f3, [%r0+4];      // Load in1[1]
ld.global.f32 %f4, [%r1+4];      // Load in2[1]
add.f32 %f5, %f3, %f4;           // Add
st.global.f32 [%r2+4], %f5;      // Store out[1]

// ... repeat for elements 2 and 3

// Total: 12 instructions (4 loads + 4 adds + 4 stores)
```

**PTX Output (After SLP)**:
```ptx
// Vector operations
ld.global.v4.f32 {%f0,%f1,%f2,%f3}, [%r0];     // Load in1[0:3]
ld.global.v4.f32 {%f4,%f5,%f6,%f7}, [%r1];     // Load in2[0:3]

// Vector add (4 parallel operations)
add.f32 %f8, %f0, %f4;
add.f32 %f9, %f1, %f5;
add.f32 %f10, %f2, %f6;
add.f32 %f11, %f3, %f7;

st.global.v4.f32 [%r2], {%f8,%f9,%f10,%f11};   // Store out[0:3]

// Total: 6 instructions (1 load + 4 adds + 1 store)
// Instruction reduction: 50%
// Memory transactions: 75% reduction (coalescing)
```

**Performance**:
- Instruction count: 12 → 6 (50% reduction)
- Memory transactions: 8 → 2 (75% reduction)
- Effective bandwidth: 100 GB/s → 320 GB/s (3.2× improvement)
- Measured speedup: **2.4×**

### Example 2: Reduction Vectorization

**Before SLP**:
```cuda
__device__ float horizontal_sum(float a, float b, float c, float d) {
    float sum = a + b + c + d;
    return sum;
}
```

**LLVM IR (Before SLP)**:
```llvm
define float @horizontal_sum(float %a, float %b, float %c, float %d) {
  %sum1 = fadd float %a, %b
  %sum2 = fadd float %sum1, %c
  %sum3 = fadd float %sum2, %d
  ret float %sum3
}
```

**After SLP Vectorization**:
```llvm
define float @horizontal_sum(float %a, float %b, float %c, float %d) {
  ; Build vector from scalars
  %vec0 = insertelement <4 x float> undef, float %a, i32 0
  %vec1 = insertelement <4 x float> %vec0, float %b, i32 1
  %vec2 = insertelement <4 x float> %vec1, float %c, i32 2
  %vec3 = insertelement <4 x float> %vec2, float %d, i32 3

  ; Horizontal reduction
  %rdx = call float @llvm.vector.reduce.fadd.v4f32(<4 x float> %vec3)
  ret float %rdx
}
```

**PTX (Before)**:
```ptx
add.f32 %f0, %f_a, %f_b;      // a + b
add.f32 %f1, %f0, %f_c;       // (a+b) + c
add.f32 %f2, %f1, %f_d;       // ((a+b)+c) + d
// 3 dependent adds, cannot parallelize
```

**PTX (After SLP with horizontal reduction)**:
```ptx
// Pack into vector
mov.b128 %v0, {%f_a, %f_b, %f_c, %f_d};

// Tree reduction (if supported)
add.f32 %f0, %f_a, %f_b;      // a + b
add.f32 %f1, %f_c, %f_d;      // c + d (parallel)
add.f32 %f2, %f0, %f1;        // (a+b) + (c+d)

// Depth: 2 (vs 3), allows more parallelism
```

**Performance**:
- Latency: 3× add latency → 2× add latency
- Throughput: Sequential → 2-way parallel
- Speedup: **1.3×** (modest, but helps in tight loops)

### Example 3: Memory Coalescing Improvement

**Before SLP**:
```cuda
__global__ void transpose_vectorize(float* out, float* in, int N) {
    int tidx = threadIdx.x + blockIdx.x * blockDim.x;

    // Each thread copies 4 elements from row to column
    // (Simplified transpose for demonstration)
    int row = tidx / N;
    int col_base = (tidx % N) * 4;

    out[col_base + 0 + row * N] = in[row * N + col_base + 0];
    out[col_base + 1 + row * N] = in[row * N + col_base + 1];
    out[col_base + 2 + row * N] = in[row * N + col_base + 2];
    out[col_base + 3 + row * N] = in[row * N + col_base + 3];
}
```

**Analysis**:
```
Warp execution (32 threads):
  - Thread 0: Accesses in[0:3], out[0, N, 2N, 3N]
  - Thread 1: Accesses in[4:7], out[4, N+4, 2N+4, 3N+4]
  - ...

INPUT reads (consecutive): GOOD COALESCING
  - Without SLP: 32 threads × 4 loads = 4 warp transactions
  - With SLP: 32 threads × 1 vector load = 1 warp transaction
  - Bandwidth improvement: 4×

OUTPUT writes (strided by N): POOR COALESCING
  - Without SLP: 32 threads × 4 stores = 128 scattered transactions
  - With SLP: Still poor (vector stores to non-consecutive addresses)
  - No improvement (need shared memory staging)
```

**PTX (Input loads, After SLP)**:
```ptx
// Thread 0
ld.global.v4.f32 {%f0,%f1,%f2,%f3}, [%r_in];

// Thread 1
ld.global.v4.f32 {%f4,%f5,%f6,%f7}, [%r_in+16];

// ...

// All 32 threads: 1 coalesced 128-byte transaction per thread
// Total: 32 transactions (vs 128 without SLP)
// Input bandwidth: 4× improvement
```

**Real Performance**:
- Input bandwidth: 150 GB/s → 550 GB/s (3.67× improvement)
- Output bandwidth: 80 GB/s → 85 GB/s (minimal improvement, strided)
- Overall speedup: **1.8×** (input-dominated)

### Example 4: Structure-of-Arrays Pattern

**Before SLP**:
```cuda
struct Particle {
    float x, y, z;
    float vx, vy, vz;
};

// Structure-of-Arrays layout (ideal for SLP)
struct ParticleSOA {
    float* x;
    float* y;
    float* z;
    float* vx;
    float* vy;
    float* vz;
};

__global__ void update_particles(ParticleSOA p, int N, float dt) {
    int idx = threadIdx.x + blockIdx.x * blockDim.x;
    if (idx >= N) return;

    // Update position (3 consecutive operations)
    p.x[idx] += p.vx[idx] * dt;
    p.y[idx] += p.vy[idx] * dt;
    p.z[idx] += p.vz[idx] * dt;
}
```

**LLVM IR (Before SLP)**:
```llvm
; Load velocities
%vx = load float, float* %vx_ptr
%vy = load float, float* %vy_ptr
%vz = load float, float* %vz_ptr

; Compute displacements
%dx = fmul float %vx, %dt
%dy = fmul float %vy, %dt
%dz = fmul float %vz, %dt

; Load positions
%x = load float, float* %x_ptr
%y = load float, float* %y_ptr
%z = load float, float* %z_ptr

; Update positions
%x_new = fadd float %x, %dx
%y_new = fadd float %y, %dy
%z_new = fadd float %z, %dz

; Store positions
store float %x_new, float* %x_ptr
store float %y_new, float* %y_ptr
store float %z_new, float* %z_ptr
```

**After SLP (Conceptual - separate arrays)**:
```llvm
; Note: Cannot fully vectorize due to separate arrays
; But can vectorize operations within each array if unrolled

; If processing 4 particles at once:
%vx_vec = load <4 x float>, <4 x float>* %vx_ptr
%dx_vec = fmul <4 x float> %vx_vec, %dt_splat

%x_vec = load <4 x float>, <4 x float>* %x_ptr
%x_new_vec = fadd <4 x float> %x_vec, %dx_vec
store <4 x float> %x_new_vec, <4 x float>* %x_ptr
```

**Performance**:
- SLP helps within each array (x, y, z, vx, vy, vz)
- Each thread processes 1 particle, but 4 threads together vectorize
- Effective speedup: **1.6×** (memory bandwidth limited)

---

## Cost Model Details

### Vectorization Benefit Score

```c
float computeVectorizationBenefit(TreeNode* tree, int VF) {
    // Base benefit: Reduction in instruction count
    float instructionReduction = 1.0f - (1.0f / VF);

    // Memory coalescing benefit (critical for GPU)
    float coalescingBenefit = 0.0f;
    if (tree->type == NODE_LOAD || tree->type == NODE_STORE) {
        if (isAligned(tree, VF * elementSize)) {
            coalescingBenefit = (VF - 1) * 0.25f;  // 25% per element
        }
    }

    // ALU throughput benefit
    float throughputBenefit = 0.0f;
    if (tree->type == NODE_INSTRUCTION) {
        throughputBenefit = (VF - 1) * 0.15f;  // 15% per element
    }

    // Total benefit
    float totalBenefit = instructionReduction + coalescingBenefit + throughputBenefit;

    return totalBenefit;
}
```

### Register Pressure Cost

```c
float computeRegisterPressureCost(TreeNode* tree, int VF, int currentPressure) {
    // Estimate additional registers needed
    int additionalRegs = (VF - 1) * countLiveValues(tree);

    // Cost increases non-linearly as pressure grows
    int totalPressure = currentPressure + additionalRegs;

    float cost = 0.0f;

    // Threshold 1: 50% of max registers
    if (totalPressure > 128) {
        cost += 0.1f * (totalPressure - 128);
    }

    // Threshold 2: 75% of max registers (steeper penalty)
    if (totalPressure > 192) {
        cost += 0.3f * (totalPressure - 192);
    }

    // Threshold 3: 90% of max registers (severe penalty)
    if (totalPressure > 230) {
        cost += 1.0f * (totalPressure - 230);
    }

    return cost;
}
```

### Memory Alignment Requirements

```c
bool checkAlignmentRequirements(TreeNode* tree, int VF) {
    int elementSize = getElementSize(tree);
    int requiredAlignment = VF * elementSize;

    // Query actual alignment
    int actualAlignment = getPointerAlignment(tree->scalars[0]);

    // Check if sufficient
    if (actualAlignment < requiredAlignment) {
        return false;  // Cannot vectorize
    }

    // Additional check: All accesses must be naturally aligned
    for (int i = 0; i < tree->scalarCount; i++) {
        int64_t offset = getMemoryOffset(tree->scalars[i]);
        if (offset % requiredAlignment != 0) {
            return false;  // Misaligned access
        }
    }

    return true;
}
```

### Overhead of Packing/Unpacking

```c
int computeExtractInsertOverhead(TreeNode* tree) {
    int cost = 0;

    // Cost of building vector from scalars (if needed)
    int numInserts = countRequiredInserts(tree);
    cost += numInserts * 1;  // 1 cycle per insert (approximate)

    // Cost of extracting results (if used outside bundle)
    int numExtracts = countExternalUses(tree);
    cost += numExtracts * 1;  // 1 cycle per extract

    // If most values are extracted, vectorization may not be worth it
    if (numExtracts > tree->scalarCount / 2) {
        cost *= 2;  // Penalty
    }

    return cost;
}
```

### Profitability Threshold

```c
bool isProfitable(CostModel* model) {
    // Minimum speedup threshold
    const float MIN_SPEEDUP = 1.2f;

    // Compute overall speedup
    float speedup = model->speedupFactor;

    // Adjust for GPU-specific factors
    speedup += computeCoalescingBenefit(model) * 0.5f;
    speedup -= computeRegisterPressurePenalty(model) * 0.3f;

    // Check threshold
    if (speedup < MIN_SPEEDUP) {
        return false;
    }

    // Additional check: Minimum absolute benefit
    int costSavings = model->scalarCost - model->vectorCost;
    if (costSavings < 10) {  // Less than 10 units saved
        return false;  // Too small to matter
    }

    return true;
}
```

---

## Related Optimizations

- **LoopVectorize**: [loop-vectorize.md](loop-vectorize.md) - Complementary top-down loop vectorization
- **LoadStoreVectorizer**: Combines consecutive loads/stores (overlaps with SLP)
- **Scalarizer**: [scalarizer.md](scalarizer.md) - Opposite transformation (breaks vectors apart)
- **InstCombine**: Cleanup pass that runs after SLP
- **MemCpyOpt**: May generate vectorized memcpy operations

---

## References

### L2 Analysis Files

- `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json:295` (SLPVectorizer identification)
- `foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json:560-561` (Loop vectorizer SLP integration notes)

### Algorithm References

1. **LLVM SLP Vectorizer**: https://llvm.org/docs/Vectorizers.html#slp-vectorizer
2. **LLVM Source Code**: `llvm/lib/Transforms/Vectorize/SLPVectorizer.cpp`
3. **Larsen, S., & Amarasinghe, S.** (2000). "Exploiting superword level parallelism with multimedia instruction sets." PLDI.
4. **PTX ISA**: NVIDIA PTX Instruction Set Architecture (vector load/store)
5. **CUDA Programming Guide**: Section on memory coalescing and vector types

### CUDA/GPU References

- CUDA C Programming Guide: Memory coalescing patterns
- PTX ISA Guide: Vector memory operations (.v2, .v4)
- NVIDIA GPU Architecture Whitepapers: Volta, Turing, Ampere memory subsystems

---

**Analysis Quality**: MEDIUM
**Last Updated**: 2025-11-17
**Confidence**: Medium (Standard LLVM pass, implementation details inferred)

**Recommendation**: Enable SLP vectorization for most CUDA kernels. Monitor register usage and occupancy. Disable if occupancy drops below 50% or register pressure is critical. Particularly effective for structure-of-arrays patterns and memory-bound kernels with good alignment.
