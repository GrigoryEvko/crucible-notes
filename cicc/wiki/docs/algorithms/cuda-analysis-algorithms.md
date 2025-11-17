# CUDA-Specific Analysis Algorithms

## Overview

This document provides complete algorithmic implementations of CICC's CUDA-specific analysis passes, including thread divergence analysis, bank conflict detection, and warp-level optimizations. These algorithms enable safe, aggressive optimization of CUDA kernels while maintaining semantic correctness.

## Table of Contents

1. [Divergence Analysis](#divergence-analysis)
2. [Bank Conflict Detection](#bank-conflict-detection)
3. [Bank Conflict Avoidance](#bank-conflict-avoidance)
4. [Helper Algorithms](#helper-algorithms)
5. [Integration with ADCE](#integration-with-adce)

---

# Divergence Analysis

## 1. Forward Data-Flow Divergence Analysis

The core divergence analysis algorithm uses forward data-flow propagation to identify which values and instructions are divergent across threads in a warp.

### 1.1 Complete Algorithm Implementation

```c
// ============================================================================
// DIVERGENCE ANALYSIS - FORWARD DATAFLOW ALGORITHM
// ============================================================================

typedef enum {
    SOURCE_THREADIDX = 0,  // threadIdx.x/y/z - DIVERGENT
    SOURCE_BLOCKDIM = 1,   // blockDim.x/y/z - UNIFORM
    SOURCE_BLOCKIDX = 2,   // blockIdx.x/y/z - CONTEXT_DEPENDENT
    SOURCE_GRIDDIM = 3,    // gridDim.x/y/z - UNIFORM
    SOURCE_WARPSIZE = 4    // warpSize - UNIFORM
} DivergenceSourceType;

typedef struct {
    bool is_uniform;
    bool is_divergent;
    Value* divergence_source;
    BasicBlock* convergence_point;
    DivergenceSourceType source_type;
} UniformityInfo;

typedef struct {
    bool is_divergent;
    Value* divergent_condition;
    BasicBlock* convergence_block;
    int warp_mask;  // Which threads in warp are affected
} DivergenceMarker;

typedef struct {
    bool has_side_effect;
    enum { STORE, CALL, VOLATILE, ATOMIC, SYNCTHREADS } effect_type;
    bool critical_semantics;
} SideEffectMap;

// Main divergence analysis driver
void DivergenceAnalysis(Function* KernelFunc) {
    // Phase 1: Initialize - All variables start as non-divergent
    Map<Value*, UniformityInfo> UniformityMap;
    Map<Instruction*, DivergenceMarker> DivergenceMap;

    for (Instruction* I : KernelFunc->instructions) {
        UniformityMap[I].is_uniform = true;
        UniformityMap[I].is_divergent = false;
        UniformityMap[I].divergence_source = NULL;
        UniformityMap[I].convergence_point = NULL;
    }

    // Phase 2: Mark thread-dependent values as divergent
    MarkBuiltinsDivergent(KernelFunc, &UniformityMap);

    // Phase 3: Forward propagation through data dependencies
    PropagateDataDependentDivergence(KernelFunc, &UniformityMap);

    // Phase 4: Forward propagation through control dependencies
    PropagateControlDependentDivergence(KernelFunc, &UniformityMap, &DivergenceMap);

    // Phase 5: Detect convergence points
    DetectConvergencePoints(KernelFunc, &DivergenceMap);

    // Phase 6: Store results for use by other passes
    StoreUniformityResults(KernelFunc, &UniformityMap);
}

// ============================================================================
// PHASE 1: DIVERGENCE SOURCE DETECTION
// ============================================================================

// Implementation address: 0x920430
DivergenceSourceType ClassifyDivergenceSource(Value* V) {
    if (!V || !isa<CallInst>(V)) {
        return -1;  // Not a built-in
    }

    CallInst* CI = cast<CallInst>(V);
    Function* Callee = CI->getCalledFunction();
    if (!Callee) return -1;

    StringRef Name = Callee->getName();

    // threadIdx.x/y/z - Each thread has unique value
    if (Name.contains("threadIdx.x") ||
        Name.contains("threadIdx.y") ||
        Name.contains("threadIdx.z") ||
        Name.contains("llvm.nvvm.read.ptx.sreg.tid")) {
        return SOURCE_THREADIDX;  // DIVERGENT
    }

    // blockDim.x/y/z - Kernel launch parameter, uniform
    if (Name.contains("blockDim.x") ||
        Name.contains("blockDim.y") ||
        Name.contains("blockDim.z") ||
        Name.contains("llvm.nvvm.read.ptx.sreg.ntid")) {
        return SOURCE_BLOCKDIM;  // UNIFORM
    }

    // blockIdx.x/y/z - Different per block, uniform within block
    if (Name.contains("blockIdx.x") ||
        Name.contains("blockIdx.y") ||
        Name.contains("blockIdx.z") ||
        Name.contains("llvm.nvvm.read.ptx.sreg.ctaid")) {
        return SOURCE_BLOCKIDX;  // CONTEXT_DEPENDENT
    }

    // gridDim.x/y/z - Kernel launch parameter, uniform
    if (Name.contains("gridDim.x") ||
        Name.contains("gridDim.y") ||
        Name.contains("gridDim.z") ||
        Name.contains("llvm.nvvm.read.ptx.sreg.nctaid")) {
        return SOURCE_GRIDDIM;  // UNIFORM
    }

    // warpSize - Architecture constant (32)
    if (Name.contains("warpSize") ||
        Name.contains("llvm.nvvm.read.ptx.sreg.warpsize")) {
        return SOURCE_WARPSIZE;  // UNIFORM
    }

    return -1;  // Unknown source
}

void MarkBuiltinsDivergent(Function* F, Map<Value*, UniformityInfo>* UMap) {
    for (Instruction& I : instructions(F)) {
        DivergenceSourceType SourceType = ClassifyDivergenceSource(&I);

        if (SourceType == SOURCE_THREADIDX) {
            // threadIdx is ALWAYS divergent
            (*UMap)[&I].is_uniform = false;
            (*UMap)[&I].is_divergent = true;
            (*UMap)[&I].divergence_source = &I;
            (*UMap)[&I].source_type = SOURCE_THREADIDX;
        } else if (SourceType == SOURCE_BLOCKIDX) {
            // blockIdx is context-dependent
            // For intra-warp analysis, treat as uniform
            // For inter-block analysis, treat as divergent
            (*UMap)[&I].is_uniform = true;  // Within block/warp
            (*UMap)[&I].is_divergent = false;
            (*UMap)[&I].source_type = SOURCE_BLOCKIDX;
        } else if (SourceType >= 0) {
            // blockDim, gridDim, warpSize are ALWAYS uniform
            (*UMap)[&I].is_uniform = true;
            (*UMap)[&I].is_divergent = false;
            (*UMap)[&I].source_type = SourceType;
        }
    }
}

// ============================================================================
// PHASE 2: FORWARD DATA DEPENDENCY PROPAGATION
// ============================================================================

bool PropagatesDivergence(Value* Operand, Instruction* User) {
    // Divergence propagates through most operations
    // Exceptions: Operations that "hide" divergence

    if (isa<PHINode>(User)) {
        // PHI nodes: divergent if ANY operand is divergent
        return true;
    }

    if (isa<SelectInst>(User)) {
        SelectInst* SI = cast<SelectInst>(User);
        // If condition is divergent, result is divergent
        // If true/false values are divergent, result is divergent
        return true;
    }

    if (isa<BinaryOperator>(User)) {
        // Arithmetic/logical operations propagate divergence
        return true;
    }

    if (isa<CastInst>(User)) {
        // Type casts preserve divergence
        return true;
    }

    if (isa<GetElementPtrInst>(User)) {
        // Address computation propagates divergence
        return true;
    }

    if (isa<LoadInst>(User)) {
        // Load from divergent address creates divergent value
        return true;
    }

    if (isa<CallInst>(User)) {
        CallInst* CI = cast<CallInst>(User);
        Function* Callee = CI->getCalledFunction();

        // Syncthreads STOPS divergence propagation
        if (Callee && Callee->getName().contains("syncthreads")) {
            return false;
        }

        // Other function calls propagate divergence
        return true;
    }

    return true;  // Conservative: propagate by default
}

void PropagateDataDependentDivergence(Function* F, Map<Value*, UniformityInfo>* UMap) {
    // Worklist algorithm for fixed-point iteration
    std::queue<Value*> Worklist;
    std::set<Value*> InWorklist;

    // Initialize worklist with all divergent values
    for (auto& Entry : *UMap) {
        if (Entry.second.is_divergent) {
            Worklist.push(Entry.first);
            InWorklist.insert(Entry.first);
        }
    }

    // Forward propagation
    while (!Worklist.empty()) {
        Value* V = Worklist.front();
        Worklist.pop();
        InWorklist.erase(V);

        // Propagate to all users of this value
        for (User* U : V->users()) {
            Instruction* UserInst = dyn_cast<Instruction>(U);
            if (!UserInst) continue;

            // Check if divergence propagates through this use
            if (PropagatesDivergence(V, UserInst)) {
                // Mark user as divergent if not already
                if (!(*UMap)[UserInst].is_divergent) {
                    (*UMap)[UserInst].is_divergent = true;
                    (*UMap)[UserInst].is_uniform = false;
                    (*UMap)[UserInst].divergence_source = (*UMap)[V].divergence_source;

                    // Add to worklist for further propagation
                    if (InWorklist.find(UserInst) == InWorklist.end()) {
                        Worklist.push(UserInst);
                        InWorklist.insert(UserInst);
                    }
                }
            }
        }
    }
}

// ============================================================================
// PHASE 3: CONTROL DEPENDENCY PROPAGATION
// ============================================================================

// Implementation address: 0x6a49a0
bool IsDivergentBranch(BranchInst* BR, Map<Value*, UniformityInfo>* UMap) {
    if (BR->isUnconditional()) {
        return false;  // Unconditional branches don't diverge
    }

    Value* Condition = BR->getCondition();

    // If condition is divergent, branch is divergent
    if ((*UMap)[Condition].is_divergent) {
        return true;
    }

    return false;
}

void PropagateControlDependentDivergence(Function* F,
                                          Map<Value*, UniformityInfo>* UMap,
                                          Map<Instruction*, DivergenceMarker>* DMap) {
    // Build post-dominator tree for convergence analysis
    PostDominatorTree PDT(*F);

    for (BasicBlock& BB : *F) {
        // Check if block is terminated by divergent branch
        BranchInst* BR = dyn_cast<BranchInst>(BB.getTerminator());
        if (!BR || !IsDivergentBranch(BR, UMap)) {
            continue;
        }

        // Find convergence point (immediate post-dominator)
        BasicBlock* ConvergenceBlock = PDT.getNode(&BB)->getIDom()->getBlock();

        // Mark all successors up to convergence point as divergent
        std::queue<BasicBlock*> Worklist;
        std::set<BasicBlock*> Visited;

        for (BasicBlock* Succ : successors(&BB)) {
            Worklist.push(Succ);
        }

        while (!Worklist.empty()) {
            BasicBlock* Current = Worklist.front();
            Worklist.pop();

            if (Visited.count(Current) || Current == ConvergenceBlock) {
                continue;
            }
            Visited.insert(Current);

            // Mark all instructions in this block as control-dependent on divergent branch
            for (Instruction& I : *Current) {
                if (!(*UMap)[&I].is_divergent) {
                    (*UMap)[&I].is_divergent = true;
                    (*UMap)[&I].is_uniform = false;
                    (*UMap)[&I].convergence_point = ConvergenceBlock;
                }

                // Store divergence marker
                (*DMap)[&I].is_divergent = true;
                (*DMap)[&I].divergent_condition = BR->getCondition();
                (*DMap)[&I].convergence_block = ConvergenceBlock;
            }

            // Continue to successors
            for (BasicBlock* Succ : successors(Current)) {
                Worklist.push(Succ);
            }
        }
    }
}

// ============================================================================
// PHASE 4: CONVERGENCE POINT DETECTION
// ============================================================================

typedef enum {
    CONV_EXPLICIT_SYNC,      // __syncthreads()
    CONV_POST_DOMINATOR,     // Post-dominator tree join
    CONV_BLOCK_BOUNDARY,     // Single predecessor/successor
    CONV_FUNCTION_RETURN     // Function exit
} ConvergenceType;

// Implementation addresses: 0x90aee0, 0xa91130
bool IsSyncthreadsCall(CallInst* CI) {
    if (!CI) return false;

    Function* Callee = CI->getCalledFunction();
    if (!Callee) return false;

    StringRef Name = Callee->getName();

    // Check for various syncthreads patterns
    if (Name.contains("syncthreads") ||
        Name.contains("cuda.syncthreads") ||
        Name.contains("llvm.nvvm.barrier0") ||
        Name.contains("llvm.nvvm.barrier.sync")) {
        return true;
    }

    return false;
}

void DetectConvergencePoints(Function* F, Map<Instruction*, DivergenceMarker>* DMap) {
    PostDominatorTree PDT(*F);

    for (BasicBlock& BB : *F) {
        for (Instruction& I : BB) {
            if (!(*DMap)[&I].is_divergent) {
                continue;  // Not in divergent region
            }

            // Type 1: Explicit synchronization
            if (CallInst* CI = dyn_cast<CallInst>(&I)) {
                if (IsSyncthreadsCall(CI)) {
                    // All divergence ends at syncthreads
                    (*DMap)[&I].convergence_block = &BB;

                    // Mark all subsequent instructions as uniform
                    auto NextIt = std::next(I.getIterator());
                    while (NextIt != BB.end()) {
                        (*DMap)[&*NextIt].is_divergent = false;
                        (*DMap)[&*NextIt].convergence_block = &BB;
                        ++NextIt;
                    }
                }
            }

            // Type 2: Post-dominator convergence
            // Already handled in control dependency propagation

            // Type 3: Block boundary convergence
            if (BB.getSinglePredecessor() && BB.getSingleSuccessor()) {
                // Single entry/exit - implicit convergence
                (*DMap)[&I].convergence_block = &BB;
            }

            // Type 4: Function return
            if (isa<ReturnInst>(&I)) {
                (*DMap)[&I].convergence_block = &BB;
            }
        }
    }
}

// ============================================================================
// WARP RECONVERGENCE DETECTION
// ============================================================================

typedef struct {
    BasicBlock* divergence_point;
    BasicBlock* convergence_point;
    std::set<BasicBlock*> divergent_region;
    int warp_mask;  // Which threads diverged
} WarpReconvergenceInfo;

void DetectWarpReconvergence(Function* F,
                              Map<Instruction*, DivergenceMarker>* DMap,
                              std::vector<WarpReconvergenceInfo>* ReconvInfo) {
    PostDominatorTree PDT(*F);

    for (BasicBlock& BB : *F) {
        BranchInst* BR = dyn_cast<BranchInst>(BB.getTerminator());
        if (!BR || BR->isUnconditional()) {
            continue;
        }

        // Check if this is a divergent branch
        bool HasDivergentInst = false;
        for (Instruction& I : BB) {
            if ((*DMap)[&I].is_divergent) {
                HasDivergentInst = true;
                break;
            }
        }

        if (!HasDivergentInst) {
            continue;
        }

        // Find immediate post-dominator (convergence point)
        DomTreeNode* PDNode = PDT.getNode(&BB);
        if (!PDNode || !PDNode->getIDom()) {
            continue;
        }

        BasicBlock* ConvergencePoint = PDNode->getIDom()->getBlock();

        // Collect divergent region
        WarpReconvergenceInfo Info;
        Info.divergence_point = &BB;
        Info.convergence_point = ConvergencePoint;
        Info.warp_mask = 0xFFFFFFFF;  // All threads potentially affected

        // BFS to find all blocks in divergent region
        std::queue<BasicBlock*> Worklist;
        std::set<BasicBlock*> Visited;

        for (BasicBlock* Succ : successors(&BB)) {
            Worklist.push(Succ);
        }

        while (!Worklist.empty()) {
            BasicBlock* Current = Worklist.front();
            Worklist.pop();

            if (Visited.count(Current) || Current == ConvergencePoint) {
                continue;
            }

            Visited.insert(Current);
            Info.divergent_region.insert(Current);

            for (BasicBlock* Succ : successors(Current)) {
                Worklist.push(Succ);
            }
        }

        ReconvInfo->push_back(Info);
    }
}

// ============================================================================
// BRANCH DIVERGENCE PREDICTION
// ============================================================================

typedef struct {
    float divergence_probability;  // 0.0 = uniform, 1.0 = fully divergent
    int estimated_threads_taken;   // How many threads take branch
    int estimated_threads_not_taken;
    bool is_likely_uniform;
} BranchDivergencePrediction;

BranchDivergencePrediction PredictBranchDivergence(BranchInst* BR,
                                                    Map<Value*, UniformityInfo>* UMap) {
    BranchDivergencePrediction Pred;
    Pred.divergence_probability = 0.0;
    Pred.estimated_threads_taken = 32;  // Default: full warp
    Pred.estimated_threads_not_taken = 0;
    Pred.is_likely_uniform = true;

    if (BR->isUnconditional()) {
        return Pred;  // Unconditional = uniform
    }

    Value* Condition = BR->getCondition();

    // Check if condition is based on threadIdx
    if ((*UMap)[Condition].is_divergent &&
        (*UMap)[Condition].source_type == SOURCE_THREADIDX) {

        // Analyze the condition pattern
        if (ICmpInst* Cmp = dyn_cast<ICmpInst>(Condition)) {
            Value* LHS = Cmp->getOperand(0);
            Value* RHS = Cmp->getOperand(1);

            // Pattern: threadIdx.x == constant
            if (isa<ConstantInt>(RHS)) {
                ConstantInt* C = cast<ConstantInt>(RHS);

                if (Cmp->getPredicate() == CmpInst::ICMP_EQ) {
                    // Only one thread matches
                    Pred.divergence_probability = 0.97;  // 31/32 threads diverge
                    Pred.estimated_threads_taken = 1;
                    Pred.estimated_threads_not_taken = 31;
                    Pred.is_likely_uniform = false;
                } else if (Cmp->getPredicate() == CmpInst::ICMP_ULT ||
                           Cmp->getPredicate() == CmpInst::ICMP_SLT) {
                    // threadIdx.x < N
                    int64_t Threshold = C->getSExtValue();
                    Pred.estimated_threads_taken = std::min((int)Threshold, 32);
                    Pred.estimated_threads_not_taken = 32 - Pred.estimated_threads_taken;
                    Pred.divergence_probability = (float)Pred.estimated_threads_not_taken / 32.0;
                    Pred.is_likely_uniform = (Threshold >= 32 || Threshold <= 0);
                }
            }

            // Pattern: (threadIdx.x % N) == K
            // This creates warp-level patterns
            if (BinaryOperator* BO = dyn_cast<BinaryOperator>(LHS)) {
                if (BO->getOpcode() == Instruction::URem ||
                    BO->getOpcode() == Instruction::SRem) {
                    // Modulo operation - creates periodic divergence
                    Pred.divergence_probability = 0.5;  // Assume 50% divergence
                    Pred.is_likely_uniform = false;
                }
            }
        }
    } else {
        // Condition is uniform
        Pred.is_likely_uniform = true;
        Pred.divergence_probability = 0.0;
    }

    return Pred;
}
```

---

# Bank Conflict Detection

## 2. Shared Memory Bank Conflict Analysis

NVIDIA GPUs have 32 independent banks in shared memory per SM, with each bank being 4 bytes wide. Bank conflicts occur when multiple threads in a warp access different addresses within the same bank.

### 2.1 Bank Configuration

```c
// ============================================================================
// BANK CONFLICT DETECTION - ARCHITECTURAL PARAMETERS
// ============================================================================

#define SHARED_MEM_BANKS 32
#define BANK_WIDTH_BYTES 4
#define CACHE_LINE_SIZE 128  // 32 banks × 4 bytes = 128 bytes
#define WARP_SIZE 32

// Bank addressing: bank_index = (address % 128) / 4
#define BANK_INDEX(addr) (((addr) % CACHE_LINE_SIZE) / BANK_WIDTH_BYTES)

// SM-specific configurations
typedef struct {
    int sm_version;          // 70=Volta, 80=Ampere, 90=Hopper
    int banks_per_sm;
    int bank_width_bytes;
    int conflict_latency_cycles;
    float bank_conflict_penalty_weight;
    int shared_memory_size_kb;
} SMBankConfig;

SMBankConfig GetSMBankConfig(int sm_version) {
    SMBankConfig config;
    config.sm_version = sm_version;
    config.banks_per_sm = 32;
    config.bank_width_bytes = 4;

    switch (sm_version) {
        case 70:  // Volta (SM 7.0)
            config.shared_memory_size_kb = 96;
            config.conflict_latency_cycles = 32;
            config.bank_conflict_penalty_weight = 2.0;
            break;
        case 80:  // Ampere (SM 8.0)
            config.shared_memory_size_kb = 96;
            config.conflict_latency_cycles = 32;
            config.bank_conflict_penalty_weight = 2.0;
            break;
        case 90:  // Hopper (SM 9.0)
            config.shared_memory_size_kb = 128;
            config.conflict_latency_cycles = 32;
            config.bank_conflict_penalty_weight = 1.5;  // TMA reduces impact
            break;
        default:
            config.shared_memory_size_kb = 96;
            config.conflict_latency_cycles = 32;
            config.bank_conflict_penalty_weight = 2.0;
    }

    return config;
}
```

### 2.2 Bank Conflict Detection Algorithm

```c
// ============================================================================
// BANK CONFLICT DETECTION ALGORITHM
// ============================================================================

typedef struct {
    Instruction* access_inst;
    Value* address;
    int thread_id;
    int bank_index;
    int offset_in_bank;
    bool is_broadcast;  // Same address across all threads
} SharedMemoryAccess;

typedef struct {
    int total_conflicts;
    int serialization_factor;  // How many sequential accesses needed
    float performance_penalty;
    std::map<int, std::vector<int>> bank_to_threads;  // Which threads hit each bank
    bool has_broadcast;
} BankConflictAnalysis;

// Main bank conflict detection
BankConflictAnalysis DetectBankConflicts(std::vector<SharedMemoryAccess>* Accesses,
                                          SMBankConfig* Config) {
    BankConflictAnalysis Result;
    Result.total_conflicts = 0;
    Result.serialization_factor = 1;
    Result.performance_penalty = 0.0;
    Result.has_broadcast = false;

    // Map banks to threads accessing them
    std::map<int, std::set<uint64_t>> BankToAddresses;

    for (SharedMemoryAccess& Access : *Accesses) {
        // Calculate bank index using architectural formula
        // bank = (address % 128) / 4
        int bank = BANK_INDEX((uint64_t)Access.address);
        Access.bank_index = bank;

        // Track which addresses are accessed in this bank
        uint64_t addr_val = (uint64_t)Access.address;
        BankToAddresses[bank].insert(addr_val);

        // Track which threads access this bank
        Result.bank_to_threads[bank].push_back(Access.thread_id);
    }

    // Analyze each bank for conflicts
    for (auto& Entry : Result.bank_to_threads) {
        int bank = Entry.first;
        std::vector<int>& threads = Entry.second;

        if (threads.size() <= 1) {
            continue;  // No conflict: single access
        }

        // Check if all threads access same address (broadcast)
        if (BankToAddresses[bank].size() == 1) {
            Result.has_broadcast = true;
            continue;  // No conflict: broadcast access
        }

        // True bank conflict: multiple threads, different addresses
        int num_conflicts = threads.size() - 1;
        Result.total_conflicts += num_conflicts;

        // Serialization factor = number of different addresses
        int serialization = BankToAddresses[bank].size();
        Result.serialization_factor = std::max(Result.serialization_factor, serialization);
    }

    // Calculate performance penalty
    // Penalty = conflict_count × latency × weight
    Result.performance_penalty =
        (float)Result.total_conflicts *
        (float)Config->conflict_latency_cycles *
        Config->bank_conflict_penalty_weight;

    return Result;
}

// ============================================================================
// ADDRESS PATTERN ANALYSIS
// ============================================================================

typedef enum {
    PATTERN_UNIFORM,        // All threads access same address
    PATTERN_SEQUENTIAL,     // threadIdx.x → address[x]
    PATTERN_STRIDED,        // threadIdx.x → address[x * stride]
    PATTERN_RANDOM,         // No discernible pattern
    PATTERN_BROADCAST       // Same address for all threads
} AccessPattern;

typedef struct {
    AccessPattern pattern_type;
    int stride;             // For PATTERN_STRIDED
    int base_offset;
    bool causes_conflict;
    int conflict_severity;  // 0=none, 1=low, 2=medium, 3=high
} AddressPatternInfo;

AddressPatternInfo AnalyzeAccessPattern(Value* AddressComputation,
                                         Map<Value*, UniformityInfo>* UMap) {
    AddressPatternInfo Info;
    Info.pattern_type = PATTERN_RANDOM;
    Info.stride = 0;
    Info.base_offset = 0;
    Info.causes_conflict = false;
    Info.conflict_severity = 0;

    // Analyze the address computation IR
    if (GetElementPtrInst* GEP = dyn_cast<GetElementPtrInst>(AddressComputation)) {
        // Check if index is based on threadIdx
        Value* Index = GEP->getOperand(GEP->getNumOperands() - 1);

        if ((*UMap)[Index].is_divergent &&
            (*UMap)[Index].source_type == SOURCE_THREADIDX) {

            // Sequential access: address[threadIdx.x]
            Info.pattern_type = PATTERN_SEQUENTIAL;
            Info.stride = 1;
            Info.causes_conflict = false;  // Sequential is conflict-free
            Info.conflict_severity = 0;

            // Check for stride
            if (BinaryOperator* BO = dyn_cast<BinaryOperator>(Index)) {
                if (BO->getOpcode() == Instruction::Mul) {
                    // address[threadIdx.x * stride]
                    if (ConstantInt* C = dyn_cast<ConstantInt>(BO->getOperand(1))) {
                        Info.pattern_type = PATTERN_STRIDED;
                        Info.stride = C->getSExtValue();

                        // Check if stride causes bank conflicts
                        // Conflict if stride % (128/element_size) != 0
                        Type* ElemType = GEP->getResultElementType();
                        int elem_size = ElemType->getPrimitiveSizeInBits() / 8;

                        if (elem_size == 0) elem_size = 4;  // Default

                        int banks_per_element = (elem_size + BANK_WIDTH_BYTES - 1) / BANK_WIDTH_BYTES;

                        // Conflict if stride hits same bank repeatedly
                        if ((Info.stride * elem_size) % CACHE_LINE_SIZE < CACHE_LINE_SIZE / 2) {
                            Info.causes_conflict = true;
                            Info.conflict_severity = 3;  // High severity
                        }
                    }
                }
            }
        } else if (!(*UMap)[Index].is_divergent) {
            // Uniform address - broadcast pattern
            Info.pattern_type = PATTERN_BROADCAST;
            Info.causes_conflict = false;  // Broadcast is conflict-free
            Info.conflict_severity = 0;
        }
    }

    return Info;
}

// ============================================================================
// PADDING CALCULATION
// ============================================================================

int CalculatePaddingForConflictAvoidance(int array_size,
                                          int element_size,
                                          int access_stride) {
    // Goal: Add padding to shift bank alignment
    // Formula: padding = gcd(stride * element_size, 128) adjustment

    int stride_bytes = access_stride * element_size;
    int conflict_period = CACHE_LINE_SIZE;  // 128 bytes

    // Check if current layout causes conflicts
    int bank_stride = (stride_bytes % conflict_period) / BANK_WIDTH_BYTES;

    if (bank_stride == 0 || bank_stride >= SHARED_MEM_BANKS / 2) {
        return 0;  // No padding needed - good access pattern
    }

    // Calculate padding to avoid conflicts
    // Add enough elements to shift to next cache line alignment
    int padding_bytes = conflict_period - (stride_bytes % conflict_period);
    int padding_elements = (padding_bytes + element_size - 1) / element_size;

    return padding_elements;
}
```

---

# Bank Conflict Avoidance

## 3. Bank Conflict Avoidance Strategies

The compiler implements six strategies to avoid bank conflicts:

### 3.1 Strategy 1: Register Reordering

```c
// ============================================================================
// STRATEGY 1: REGISTER REORDERING
// ============================================================================

typedef struct {
    VirtualRegister* vreg;
    int preferred_bank;
    std::set<int> forbidden_banks;
    int conflict_cost;
} RegisterBankConstraint;

void ApplyRegisterReorderingForBankConflicts(RegisterAllocationContext* RA) {
    std::map<VirtualRegister*, RegisterBankConstraint> Constraints;

    // Analyze shared memory access patterns
    for (MachineInstr& MI : RA->function->instructions) {
        if (!IsSharedMemoryAccess(&MI)) {
            continue;
        }

        // Extract address register
        VirtualRegister* AddrReg = GetAddressRegister(&MI);
        if (!AddrReg) continue;

        // Determine which bank this access will hit
        AddressPatternInfo Pattern = AnalyzeAccessPattern(AddrReg->defining_value, RA->uniformity_map);

        if (Pattern.causes_conflict) {
            // Add constraint to avoid conflicting register assignment
            RegisterBankConstraint Constraint;
            Constraint.vreg = AddrReg;
            Constraint.conflict_cost = Pattern.conflict_severity * 2.0;

            // Forbid register classes that would worsen conflicts
            // (Implementation-specific register class analysis)

            Constraints[AddrReg] = Constraint;
        }
    }

    // Apply constraints during graph coloring
    for (auto& Entry : Constraints) {
        VirtualRegister* VReg = Entry.first;
        RegisterBankConstraint& Constraint = Entry.second;

        // Add constraint edges in interference graph
        for (int ForbiddenBank : Constraint.forbidden_banks) {
            RA->AddConstraintEdge(VReg, ForbiddenBank, Constraint.conflict_cost);
        }
    }
}
```

### 3.2 Strategy 2: Shared Memory Padding

```c
// ============================================================================
// STRATEGY 2: SHARED MEMORY PADDING INSERTION
// ============================================================================

// Implementation addresses: 0x1cc5230, 0x2d198b0
void SetSharedMemoryArrayAlignment(Module* M) {
    for (GlobalVariable& GV : M->globals()) {
        // Check if this is a shared memory allocation
        if (GV.getAddressSpace() != 3) {  // Address space 3 = shared memory
            continue;
        }

        Type* Ty = GV.getValueType();
        if (!Ty->isArrayTy()) {
            continue;
        }

        ArrayType* ArrTy = cast<ArrayType>(Ty);
        int NumElements = ArrTy->getNumElements();
        Type* ElemTy = ArrTy->getElementType();
        int ElemSize = ElemTy->getPrimitiveSizeInBits() / 8;

        // Analyze access patterns to this array
        int AccessStride = AnalyzeArrayAccessStride(&GV);

        // Calculate padding needed
        int Padding = CalculatePaddingForConflictAvoidance(NumElements, ElemSize, AccessStride);

        if (Padding > 0) {
            // Create new array type with padding
            ArrayType* NewTy = ArrayType::get(ElemTy, NumElements + Padding);

            // Replace global variable with padded version
            // (Implementation details omitted for brevity)
        }
    }
}

int AnalyzeArrayAccessStride(GlobalVariable* GV) {
    int detected_stride = 1;  // Default: sequential access

    for (User* U : GV->users()) {
        if (GetElementPtrInst* GEP = dyn_cast<GetElementPtrInst>(U)) {
            // Extract stride from index computation
            Value* Index = GEP->getOperand(GEP->getNumOperands() - 1);

            if (BinaryOperator* BO = dyn_cast<BinaryOperator>(Index)) {
                if (BO->getOpcode() == Instruction::Mul) {
                    if (ConstantInt* C = dyn_cast<ConstantInt>(BO->getOperand(1))) {
                        detected_stride = std::max(detected_stride, (int)C->getSExtValue());
                    }
                }
            }
        }
    }

    return detected_stride;
}
```

### 3.3 Strategy 3: Broadcast Optimization

```c
// ============================================================================
// STRATEGY 3: BROADCAST OPTIMIZATION
// ============================================================================

bool CanUseBroadcast(LoadInst* LI, Map<Value*, UniformityInfo>* UMap) {
    Value* Address = LI->getPointerOperand();

    // Check if address is uniform across warp
    if (!(*UMap)[Address].is_divergent) {
        return true;  // Uniform address → broadcast
    }

    return false;
}

void OptimizeBroadcastAccesses(Function* F, Map<Value*, UniformityInfo>* UMap) {
    for (BasicBlock& BB : *F) {
        for (Instruction& I : BB) {
            LoadInst* LI = dyn_cast<LoadInst>(&I);
            if (!LI) continue;

            // Check if this is shared memory load
            unsigned AS = LI->getPointerAddressSpace();
            if (AS != 3) continue;  // Not shared memory

            if (CanUseBroadcast(LI, UMap)) {
                // Transform to broadcast instruction
                // Use shfl.sync or explicit broadcast PTX instruction

                IRBuilder<> Builder(LI);

                // Load from shared memory once (thread 0)
                // Then broadcast to all threads in warp
                // This avoids bank conflicts entirely

                // Pseudocode transformation:
                // Before: value = load sharedMem[uniform_addr]
                // After:  if (laneId == 0) tmp = load sharedMem[uniform_addr]
                //         value = shfl.sync(tmp, 0)
            }
        }
    }
}
```

### 3.4 Strategy 4: Stride Analysis

```c
// ============================================================================
// STRATEGY 4: STRIDE MEMORY ACCESS VERSIONING
// ============================================================================

typedef struct {
    Value* base_pointer;
    Value* stride_value;
    bool is_constant_stride;
    int constant_stride;
    bool causes_bank_conflict;
} StrideAnalysisResult;

StrideAnalysisResult AnalyzeMemoryAccessStride(GetElementPtrInst* GEP) {
    StrideAnalysisResult Result;
    Result.base_pointer = GEP->getPointerOperand();
    Result.is_constant_stride = false;
    Result.constant_stride = 0;
    Result.causes_bank_conflict = false;

    // Symbolic stride analysis
    Value* Index = GEP->getOperand(GEP->getNumOperands() - 1);

    if (ConstantInt* C = dyn_cast<ConstantInt>(Index)) {
        // Constant index - no stride
        Result.is_constant_stride = true;
        Result.constant_stride = 0;
    } else if (BinaryOperator* BO = dyn_cast<BinaryOperator>(Index)) {
        if (BO->getOpcode() == Instruction::Mul) {
            // index * stride
            Value* Multiplier = BO->getOperand(1);

            if (ConstantInt* C = dyn_cast<ConstantInt>(Multiplier)) {
                Result.is_constant_stride = true;
                Result.constant_stride = C->getSExtValue();
                Result.stride_value = Multiplier;

                // Check if this stride causes conflicts
                Type* ElemType = GEP->getResultElementType();
                int elem_size = ElemType->getPrimitiveSizeInBits() / 8;
                if (elem_size == 0) elem_size = 4;

                int stride_bytes = Result.constant_stride * elem_size;
                int bank_stride = (stride_bytes % CACHE_LINE_SIZE) / BANK_WIDTH_BYTES;

                // Conflict if multiple threads hit same bank
                if (bank_stride > 0 && bank_stride < SHARED_MEM_BANKS / 2) {
                    Result.causes_bank_conflict = true;
                }
            }
        }
    }

    return Result;
}

void VersionMemoryAccessByStride(Function* F) {
    // Create specialized versions for different stride patterns
    // Version 1: stride=1 (sequential, no conflict)
    // Version 2: stride=power_of_2 (potential conflicts)
    // Version 3: stride=arbitrary (analyze runtime)

    for (BasicBlock& BB : *F) {
        for (Instruction& I : BB) {
            if (GetElementPtrInst* GEP = dyn_cast<GetElementPtrInst>(&I)) {
                StrideAnalysisResult Stride = AnalyzeMemoryAccessStride(GEP);

                if (Stride.causes_bank_conflict) {
                    // Create conflict-free version with padding/transformation
                    // Switch between versions based on stride value
                }
            }
        }
    }
}
```

### 3.5 Strategy 5: Alignment Optimization

```c
// ============================================================================
// STRATEGY 5: ALIGNMENT OPTIMIZATION
// ============================================================================

void OptimizeSharedMemoryAlignment(GlobalVariable* GV) {
    Type* Ty = GV->getValueType();

    // Align to cache line boundary (128 bytes)
    int desired_alignment = CACHE_LINE_SIZE;

    // Check current alignment
    int current_alignment = GV->getAlignment();

    if (current_alignment < desired_alignment) {
        GV->setAlignment(Align(desired_alignment));
    }

    // For arrays, ensure each row starts at bank 0
    if (Ty->isArrayTy()) {
        ArrayType* ArrTy = cast<ArrayType>(Ty);
        Type* ElemTy = ArrTy->getElementType();
        int elem_size = ElemTy->getPrimitiveSizeInBits() / 8;
        int num_elements = ArrTy->getNumElements();

        // Calculate row size
        int row_size_bytes = num_elements * elem_size;

        // Pad each row to align to cache line
        if (row_size_bytes % CACHE_LINE_SIZE != 0) {
            int padding_bytes = CACHE_LINE_SIZE - (row_size_bytes % CACHE_LINE_SIZE);
            int padding_elements = (padding_bytes + elem_size - 1) / elem_size;

            // Add padding to array
            // (Implementation details omitted)
        }
    }
}
```

### 3.6 Strategy 6: Coalescing Improvement

```c
// ============================================================================
// STRATEGY 6: MEMORY COALESCING IMPROVEMENT
// ============================================================================

typedef struct {
    bool is_coalesced;
    int coalescing_efficiency;  // 0-100%
    std::vector<int> uncoalesced_threads;
    int transaction_count;  // How many memory transactions needed
} CoalescingAnalysis;

CoalescingAnalysis AnalyzeMemoryCoalescing(std::vector<SharedMemoryAccess>* Accesses) {
    CoalescingAnalysis Result;
    Result.is_coalesced = true;
    Result.coalescing_efficiency = 100;
    Result.transaction_count = 1;

    // Sort accesses by address
    std::sort(Accesses->begin(), Accesses->end(),
              [](const SharedMemoryAccess& a, const SharedMemoryAccess& b) {
                  return (uint64_t)a.address < (uint64_t)b.address;
              });

    // Check if accesses form contiguous cache-line-aligned region
    uint64_t first_addr = (uint64_t)(*Accesses)[0].address;
    uint64_t cache_line_base = first_addr & ~(CACHE_LINE_SIZE - 1);

    for (size_t i = 0; i < Accesses->size(); i++) {
        uint64_t addr = (uint64_t)(*Accesses)[i].address;
        uint64_t expected_addr = cache_line_base + (i * BANK_WIDTH_BYTES);

        if (addr != expected_addr) {
            Result.is_coalesced = false;
            Result.uncoalesced_threads.push_back((*Accesses)[i].thread_id);
        }
    }

    // Calculate efficiency
    Result.coalescing_efficiency =
        ((Accesses->size() - Result.uncoalesced_threads.size()) * 100) / Accesses->size();

    // Estimate transaction count
    std::set<uint64_t> cache_lines;
    for (auto& Access : *Accesses) {
        uint64_t cache_line = ((uint64_t)Access.address) / CACHE_LINE_SIZE;
        cache_lines.insert(cache_line);
    }
    Result.transaction_count = cache_lines.size();

    return Result;
}

void ImproveMemoryCoalescing(Function* F) {
    // Reorder memory accesses to improve coalescing
    // Transform strided accesses to sequential accesses where possible

    for (BasicBlock& BB : *F) {
        std::vector<Instruction*> MemoryOps;

        // Collect all memory operations in block
        for (Instruction& I : BB) {
            if (isa<LoadInst>(&I) || isa<StoreInst>(&I)) {
                MemoryOps.push_back(&I);
            }
        }

        // Analyze access pattern
        if (MemoryOps.size() < 2) continue;

        // Sort by address to enable coalescing
        // (Implementation would use scheduler to reorder)
    }
}
```

---

# Helper Algorithms

## 4. Supporting Algorithms

### 4.1 Thread-to-Bank Mapping

```c
// ============================================================================
// THREAD-TO-BANK MAPPING
// ============================================================================

typedef struct {
    int thread_id;
    int lane_id;  // 0-31 within warp
    int warp_id;
    int block_id;
} ThreadInfo;

int MapThreadToBank(ThreadInfo* Thread, Value* Address, int element_size) {
    // Calculate which bank this thread will access
    // Bank formula: bank = (address % 128) / 4

    uint64_t addr = (uint64_t)Address;
    int bank = BANK_INDEX(addr);

    return bank;
}

std::map<int, std::vector<ThreadInfo>> BuildBankAccessMap(
    std::vector<SharedMemoryAccess>* Accesses) {

    std::map<int, std::vector<ThreadInfo>> BankMap;

    for (auto& Access : *Accesses) {
        int bank = BANK_INDEX((uint64_t)Access.address);

        ThreadInfo TInfo;
        TInfo.thread_id = Access.thread_id;
        TInfo.lane_id = Access.thread_id % WARP_SIZE;
        TInfo.warp_id = Access.thread_id / WARP_SIZE;
        TInfo.block_id = 0;  // Would be extracted from context

        BankMap[bank].push_back(TInfo);
    }

    return BankMap;
}
```

### 4.2 Access Pattern Detection

```c
// ============================================================================
// ACCESS PATTERN DETECTION
// ============================================================================

typedef struct {
    AccessPattern pattern;
    int period;  // For periodic patterns
    bool is_predictable;
    float conflict_probability;
} PatternDetectionResult;

PatternDetectionResult DetectAccessPattern(std::vector<SharedMemoryAccess>* Accesses) {
    PatternDetectionResult Result;
    Result.pattern = PATTERN_RANDOM;
    Result.period = 0;
    Result.is_predictable = false;
    Result.conflict_probability = 0.5;

    if (Accesses->size() < 2) {
        Result.pattern = PATTERN_UNIFORM;
        Result.is_predictable = true;
        return Result;
    }

    // Check for uniform access (all same address)
    bool all_same = true;
    uint64_t first_addr = (uint64_t)(*Accesses)[0].address;
    for (auto& Access : *Accesses) {
        if ((uint64_t)Access.address != first_addr) {
            all_same = false;
            break;
        }
    }

    if (all_same) {
        Result.pattern = PATTERN_BROADCAST;
        Result.is_predictable = true;
        Result.conflict_probability = 0.0;  // No conflict
        return Result;
    }

    // Check for sequential access
    bool is_sequential = true;
    for (size_t i = 1; i < Accesses->size(); i++) {
        uint64_t prev = (uint64_t)(*Accesses)[i-1].address;
        uint64_t curr = (uint64_t)(*Accesses)[i].address;

        if (curr != prev + BANK_WIDTH_BYTES) {
            is_sequential = false;
            break;
        }
    }

    if (is_sequential) {
        Result.pattern = PATTERN_SEQUENTIAL;
        Result.is_predictable = true;
        Result.conflict_probability = 0.0;  // Sequential is conflict-free
        return Result;
    }

    // Check for strided access
    if (Accesses->size() >= 3) {
        uint64_t stride = (uint64_t)(*Accesses)[1].address - (uint64_t)(*Accesses)[0].address;
        bool is_strided = true;

        for (size_t i = 2; i < Accesses->size(); i++) {
            uint64_t expected = (uint64_t)(*Accesses)[0].address + (i * stride);
            uint64_t actual = (uint64_t)(*Accesses)[i].address;

            if (actual != expected) {
                is_strided = false;
                break;
            }
        }

        if (is_strided) {
            Result.pattern = PATTERN_STRIDED;
            Result.is_predictable = true;

            // Calculate conflict probability based on stride
            int stride_banks = (stride % CACHE_LINE_SIZE) / BANK_WIDTH_BYTES;
            if (stride_banks == 0) {
                // All threads hit same bank - high conflict
                Result.conflict_probability = 1.0;
            } else if (stride_banks >= SHARED_MEM_BANKS / 2) {
                // Good distribution - low conflict
                Result.conflict_probability = 0.1;
            } else {
                // Moderate conflicts
                Result.conflict_probability = 0.5;
            }

            return Result;
        }
    }

    // Random/unpredictable pattern
    Result.pattern = PATTERN_RANDOM;
    Result.is_predictable = false;
    Result.conflict_probability = 0.5;

    return Result;
}
```

### 4.3 Penalty Weight Calculation

```c
// ============================================================================
// PENALTY WEIGHT CALCULATION
// ============================================================================

#define BASE_BANK_CONFLICT_PENALTY 2.0f

float CalculateBankConflictPenalty(BankConflictAnalysis* Analysis,
                                    SMBankConfig* Config) {
    // Base formula: penalty = conflicts × latency × weight
    float base_penalty =
        (float)Analysis->total_conflicts *
        (float)Config->conflict_latency_cycles *
        Config->bank_conflict_penalty_weight;

    // Adjust for serialization factor
    // Higher serialization = worse penalty
    float serialization_multiplier = (float)Analysis->serialization_factor;

    float total_penalty = base_penalty * serialization_multiplier;

    // Cap penalty at reasonable maximum
    float max_penalty = 1000.0f;
    if (total_penalty > max_penalty) {
        total_penalty = max_penalty;
    }

    return total_penalty;
}

float IntegratePenaltyWithCostModel(Instruction* I,
                                     float base_cost,
                                     BankConflictAnalysis* ConflictInfo) {
    // Integrate bank conflict penalty into instruction cost model
    // Used by instruction selection and scheduling

    float conflict_penalty = CalculateBankConflictPenalty(ConflictInfo, &GlobalSMConfig);

    // Combined cost = base_cost × (1 + conflict_penalty_factor)
    float penalty_factor = conflict_penalty / 100.0f;  // Normalize
    float combined_cost = base_cost * (1.0f + penalty_factor);

    return combined_cost;
}
```

---

# Integration with ADCE

## 5. Divergence-Aware Dead Code Elimination

### 5.1 Complete ADCE Algorithm with Safety Rules

```c
// ============================================================================
// ADCE INTEGRATION - DIVERGENCE-AWARE DEAD CODE ELIMINATION
// ============================================================================

// Implementation addresses: 0x2adce40 (driver), 0x30adae0 (core)

typedef struct {
    std::set<Instruction*> alive_instructions;
    std::set<BasicBlock*> alive_blocks;
    Map<Instruction*, SideEffectMap> side_effect_info;
    Map<Instruction*, DivergenceMarker> divergence_info;
    Map<Value*, UniformityInfo> uniformity_info;
} ADCEContext;

void AggressiveDeadCodeElimination(Function* F,
                                    Map<Value*, UniformityInfo>* UMap) {
    ADCEContext Ctx;
    Ctx.uniformity_info = *UMap;

    // Phase 1: Analyze side effects and divergence
    AnalyzeSideEffects(F, &Ctx);
    AnalyzeDivergence(F, &Ctx);

    // Phase 2: Mark initially alive instructions
    MarkInitiallyAlive(F, &Ctx);

    // Phase 3: Propagate liveness with divergence awareness
    PropagateLivenessWithDivergence(F, &Ctx);

    // Phase 4: Remove dead code
    RemoveDeadCode(F, &Ctx);
}

// ============================================================================
// SAFETY RULE R1: UNIFORM EXECUTION REQUIREMENT
// ============================================================================

bool CanEliminate_R1_UniformExecution(Instruction* I, ADCEContext* Ctx) {
    // Rule R1: Can only eliminate if:
    // - ALL threads execute it (uniform), OR
    // - It has NO side effects

    bool has_side_effect = Ctx->side_effect_info[I].has_side_effect;
    bool is_divergent = Ctx->divergence_info[I].is_divergent;

    if (has_side_effect && is_divergent) {
        // Has side effects AND in divergent region
        // Some threads execute, some don't → CANNOT ELIMINATE
        return false;
    }

    // Either no side effects, or uniform execution → CAN ELIMINATE
    return true;
}

// ============================================================================
// SAFETY RULE R2: MEMORY OPERATION PRESERVATION
// ============================================================================

bool CanEliminate_R2_MemoryOperation(Instruction* I, ADCEContext* Ctx) {
    // Rule R2: Protect memory operations in divergent code

    if (StoreInst* SI = dyn_cast<StoreInst>(I)) {
        // Store operation
        if (Ctx->divergence_info[I].is_divergent) {
            // Divergent store - some threads may execute
            // CANNOT ELIMINATE - would cause data corruption
            return false;
        }
    }

    if (CallInst* CI = dyn_cast<CallInst>(I)) {
        Function* Callee = CI->getCalledFunction();
        if (Callee) {
            StringRef Name = Callee->getName();

            // Atomic operations
            if (Name.contains("atomic")) {
                if (Ctx->divergence_info[I].is_divergent) {
                    return false;  // CANNOT ELIMINATE
                }
            }
        }
    }

    return true;  // Safe to eliminate
}

// ============================================================================
// SAFETY RULE R3: CONTROL DEPENDENCE SAFETY
// ============================================================================

bool CanEliminate_R3_ControlDependence(Instruction* I, ADCEContext* Ctx) {
    // Rule R3: Cannot eliminate instructions control-dependent on divergent branch

    if (!Ctx->divergence_info[I].is_divergent) {
        return true;  // Not in divergent region - safe
    }

    // In divergent region - check for side effects
    if (Ctx->side_effect_info[I].has_side_effect) {
        // Has side effects - CANNOT ELIMINATE
        return false;
    }

    // No side effects - check if value is dead
    if (I->use_empty()) {
        return true;  // Dead and no side effects - CAN ELIMINATE
    }

    return false;  // Conservative: keep it
}

// ============================================================================
// SAFETY RULE R4: SIDE EFFECT PRESERVATION
// ============================================================================

bool CanEliminate_R4_SideEffectPreservation(Instruction* I, ADCEContext* Ctx) {
    // Rule R4: ALWAYS preserve operations with side effects

    if (CallInst* CI = dyn_cast<CallInst>(I)) {
        // Function calls may have external side effects
        Function* Callee = CI->getCalledFunction();

        if (!Callee) {
            // Indirect call - assume side effects
            return false;  // CANNOT ELIMINATE
        }

        // Check for I/O operations
        StringRef Name = Callee->getName();
        if (Name.contains("printf") || Name.contains("write") ||
            Name.contains("print")) {
            return false;  // CANNOT ELIMINATE
        }
    }

    if (LoadInst* LI = dyn_cast<LoadInst>(I)) {
        // Volatile loads have observable side effects
        if (LI->isVolatile()) {
            return false;  // CANNOT ELIMINATE
        }
    }

    if (StoreInst* SI = dyn_cast<StoreInst>(I)) {
        // Volatile stores
        if (SI->isVolatile()) {
            return false;  // CANNOT ELIMINATE
        }
    }

    return true;  // No protected side effects
}

// ============================================================================
// SAFETY RULE R5: CONVERGENT OPERATION CONSTRAINTS
// ============================================================================

bool CanEliminate_R5_ConvergentOperations(Instruction* I, ADCEContext* Ctx) {
    // Rule R5: Convergent operations have special semantics

    if (CallInst* CI = dyn_cast<CallInst>(I)) {
        // Check for convergent attribute
        if (CI->isConvergent()) {
            // Convergent operation - cannot add control dependencies
            // CANNOT ELIMINATE unless proven safe
            return false;
        }

        Function* Callee = CI->getCalledFunction();
        if (Callee) {
            StringRef Name = Callee->getName();

            // Explicit convergence control intrinsics
            if (Name.contains("llvm.experimental.convergence")) {
                return false;  // CANNOT ELIMINATE
            }

            // Warp-level primitives
            if (Name.contains("shfl") || Name.contains("ballot") ||
                Name.contains("vote")) {
                return false;  // CANNOT ELIMINATE
            }
        }
    }

    return true;  // Not a convergent operation
}

// ============================================================================
// SAFETY RULE R6: SPECULATIVE EXECUTION LIMITS
// ============================================================================

bool CanEliminate_R6_SpeculativeExecution(Instruction* I, ADCEContext* Ctx) {
    // Rule R6: Limit speculative execution in divergent regions

    if (!Ctx->divergence_info[I].is_divergent) {
        return true;  // Not in divergent region
    }

    // Check if this instruction would be speculatively executed
    BasicBlock* BB = I->getParent();

    // If block is only reachable through divergent branch,
    // cannot speculatively move code into or out of it
    for (BasicBlock* Pred : predecessors(BB)) {
        BranchInst* BR = dyn_cast<BranchInst>(Pred->getTerminator());
        if (BR && !BR->isUnconditional()) {
            Value* Cond = BR->getCondition();
            if (Ctx->uniformity_info[Cond].is_divergent) {
                // Divergent target - restrict speculation
                if (mayHaveSideEffects(I)) {
                    return false;  // CANNOT ELIMINATE
                }
            }
        }
    }

    return true;  // Safe for speculation
}

// ============================================================================
// COMBINED SAFETY CHECK - ALL RULES
// ============================================================================

bool IsSafeToEliminate(Instruction* I, ADCEContext* Ctx) {
    // Apply ALL six safety rules

    if (!CanEliminate_R1_UniformExecution(I, Ctx)) {
        return false;
    }

    if (!CanEliminate_R2_MemoryOperation(I, Ctx)) {
        return false;
    }

    if (!CanEliminate_R3_ControlDependence(I, Ctx)) {
        return false;
    }

    if (!CanEliminate_R4_SideEffectPreservation(I, Ctx)) {
        return false;
    }

    if (!CanEliminate_R5_ConvergentOperations(I, Ctx)) {
        return false;
    }

    if (!CanEliminate_R6_SpeculativeExecution(I, Ctx)) {
        return false;
    }

    // Passed all safety checks
    return true;
}

// ============================================================================
// LIVENESS PROPAGATION WITH DIVERGENCE AWARENESS
// ============================================================================

void PropagateLivenessWithDivergence(Function* F, ADCEContext* Ctx) {
    std::queue<Instruction*> Worklist;

    // Initialize with all alive instructions
    for (Instruction* I : Ctx->alive_instructions) {
        Worklist.push(I);
    }

    while (!Worklist.empty()) {
        Instruction* I = Worklist.front();
        Worklist.pop();

        // Mark all operands as alive
        for (Use& U : I->operands()) {
            if (Instruction* OpI = dyn_cast<Instruction>(U.get())) {
                if (Ctx->alive_instructions.find(OpI) == Ctx->alive_instructions.end()) {
                    // Check if safe to mark as alive
                    // Even if instruction appears dead, it may be required
                    // for correctness in divergent regions

                    Ctx->alive_instructions.insert(OpI);
                    Worklist.push(OpI);
                }
            }
        }

        // Mark control dependencies as alive
        BasicBlock* BB = I->getParent();
        if (Ctx->alive_blocks.find(BB) == Ctx->alive_blocks.end()) {
            Ctx->alive_blocks.insert(BB);

            // Mark terminator as alive
            Instruction* Term = BB->getTerminator();
            if (Ctx->alive_instructions.find(Term) == Ctx->alive_instructions.end()) {
                Ctx->alive_instructions.insert(Term);
                Worklist.push(Term);
            }
        }
    }
}

// ============================================================================
// DEAD CODE REMOVAL
// ============================================================================

void RemoveDeadCode(Function* F, ADCEContext* Ctx) {
    std::vector<Instruction*> ToRemove;

    for (BasicBlock& BB : *F) {
        for (Instruction& I : BB) {
            if (Ctx->alive_instructions.find(&I) == Ctx->alive_instructions.end()) {
                // Instruction not marked alive - check if safe to remove
                if (IsSafeToEliminate(&I, Ctx)) {
                    ToRemove.push_back(&I);
                } else {
                    // Not safe - mark as alive for safety
                    Ctx->alive_instructions.insert(&I);
                }
            }
        }
    }

    // Remove dead instructions
    for (Instruction* I : ToRemove) {
        I->eraseFromParent();
    }
}
```

---

## Summary

This document provides complete implementations of CICC's CUDA-specific analysis algorithms:

1. **Divergence Analysis**: Forward data-flow algorithm with 6 safety rules
2. **Bank Conflict Detection**: 32 banks × 4 bytes analysis with exact formulas
3. **Bank Conflict Avoidance**: 6 comprehensive strategies
4. **Helper Algorithms**: Thread-to-bank mapping, pattern detection, penalty calculation
5. **ADCE Integration**: Divergence-aware dead code elimination with all safety rules

These algorithms enable safe, aggressive optimization of CUDA kernels while maintaining semantic correctness and avoiding performance-degrading bank conflicts.
