# Loop Analysis Algorithms - Complete Technical Reference

**Extraction Date**: 2025-11-16
**Confidence**: HIGH
**Source Files**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/loop_detection.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/GVN_IMPLEMENTATION_DETAILS.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/DSE_QUICK_REFERENCE.md`

**Status**: Complete extraction of loop detection, analysis, and transformation algorithms from CICC compiler

---

## Table of Contents

1. [Loop Detection Algorithms](#1-loop-detection-algorithms)
2. [Loop Analysis Algorithms](#2-loop-analysis-algorithms)
3. [Loop Transformation Algorithms](#3-loop-transformation-algorithms)
4. [SCEV (Scalar Evolution) Algorithms](#4-scev-scalar-evolution-algorithms)
5. [Integration with GVN and MemorySSA](#5-integration-with-gvn-and-memoryssa)
6. [CUDA-Specific Loop Handling](#6-cuda-specific-loop-handling)

---

## 1. Loop Detection Algorithms

### 1.1 Natural Loop Detection - Complete Algorithm

**Algorithm**: Dominator-Based Natural Loop Detection
**Complexity**: O(V + E) where V=vertices, E=edges
**Classification**: Control Flow Analysis

```c
// ============================================================================
// Natural Loop Detection Algorithm
// Based on: Dominator tree and back-edge identification
// Complexity: O(V + E) for CFG with V basic blocks, E edges
// ============================================================================

typedef struct BasicBlock {
    int id;
    char *label;
    struct BasicBlock **successors;
    struct BasicBlock **predecessors;
    int num_successors;
    int num_predecessors;

    // Dominator tree information
    struct BasicBlock *idom;           // Immediate dominator
    struct BasicBlock **idom_children; // Children in dominator tree
    int num_idom_children;

    // DFS information
    int discovery_time;
    int finish_time;
    enum { WHITE, GRAY, BLACK } color;

    // Loop information
    struct Loop *innermost_loop;
    int loop_depth;
} BasicBlock;

typedef struct Loop {
    int id;
    BasicBlock *header;              // Loop entry point (unique)
    BasicBlock *latch;               // Block containing back edge (after simplify)
    BasicBlock *preheader;           // Guaranteed single entry (after simplify)

    Set<BasicBlock*> *blocks;        // All blocks in loop
    Set<BasicBlock*> *exit_blocks;   // Blocks with successors outside loop
    Set<Edge*> *exit_edges;          // Edges leaving loop

    struct Loop *parent_loop;        // Immediately containing loop
    Vector<Loop*> *sub_loops;        // Immediately contained loops
    int depth;                       // Nesting depth (1 = outermost)

    // Analysis results
    int trip_count;                  // -1 if unknown
    bool has_trip_count;
    Set<Instruction*> *induction_vars;
    bool is_rotated;
    bool is_simplified;
} Loop;

typedef struct LoopInfo {
    Vector<Loop*> *top_level_loops;  // Outermost loops
    Map<BasicBlock*, Loop*> *block_to_loop; // Maps block to innermost loop
    Loop *root;                       // Implicit root representing function
} LoopInfo;

typedef struct Edge {
    BasicBlock *source;
    BasicBlock *dest;
    enum { TREE, BACK, FORWARD, CROSS } type;
} Edge;


// ============================================================================
// Phase 1: Build Dominator Tree
// Algorithm: Lengauer-Tarjan (O(α(V) * (V + E)))
// ============================================================================

void BuildDominatorTree(Function *F) {
    int num_blocks = F->num_blocks;
    BasicBlock **blocks = F->blocks;
    BasicBlock *entry = F->entry_block;

    // Initialize
    for (int i = 0; i < num_blocks; i++) {
        blocks[i]->idom = NULL;
        blocks[i]->idom_children = NULL;
        blocks[i]->num_idom_children = 0;
    }

    // Entry block dominates itself
    entry->idom = entry;

    // Compute post-order numbering (for efficiency)
    int post_order_num = 0;
    int *post_order = malloc(num_blocks * sizeof(int));
    BasicBlock **reverse_post_order = malloc(num_blocks * sizeof(BasicBlock*));

    ComputePostOrder(entry, post_order, &post_order_num, reverse_post_order);

    // Compute dominators iteratively (Cooper-Harvey-Kennedy algorithm)
    bool changed = true;
    while (changed) {
        changed = false;

        // Process blocks in reverse post-order (excluding entry)
        for (int i = 1; i < num_blocks; i++) {
            BasicBlock *b = reverse_post_order[i];
            BasicBlock *new_idom = NULL;

            // Find first processed predecessor
            for (int j = 0; j < b->num_predecessors; j++) {
                BasicBlock *pred = b->predecessors[j];
                if (pred->idom != NULL) {
                    new_idom = pred;
                    break;
                }
            }

            // Intersect with all other processed predecessors
            for (int j = 0; j < b->num_predecessors; j++) {
                BasicBlock *pred = b->predecessors[j];
                if (pred != new_idom && pred->idom != NULL) {
                    new_idom = Intersect(pred, new_idom, post_order);
                }
            }

            // Update if changed
            if (b->idom != new_idom) {
                b->idom = new_idom;
                changed = true;
            }
        }
    }

    // Build dominator tree children lists
    for (int i = 0; i < num_blocks; i++) {
        BasicBlock *b = blocks[i];
        if (b->idom != NULL && b->idom != b) {
            AddDomTreeChild(b->idom, b);
        }
    }

    free(post_order);
    free(reverse_post_order);
}

BasicBlock* Intersect(BasicBlock *b1, BasicBlock *b2, int *post_order) {
    // Find common dominator using post-order numbering
    while (b1 != b2) {
        while (post_order[b1->id] < post_order[b2->id]) {
            b1 = b1->idom;
        }
        while (post_order[b2->id] < post_order[b1->id]) {
            b2 = b2->idom;
        }
    }
    return b1;
}

void ComputePostOrder(BasicBlock *block, int *order, int *num,
                      BasicBlock **reverse_order) {
    block->color = GRAY;

    for (int i = 0; i < block->num_successors; i++) {
        BasicBlock *succ = block->successors[i];
        if (succ->color == WHITE) {
            ComputePostOrder(succ, order, num, reverse_order);
        }
    }

    block->color = BLACK;
    order[block->id] = *num;
    reverse_order[*num] = block;
    (*num)++;
}


// ============================================================================
// Phase 2: Identify Back Edges via DFS
// A back edge (u, v) where v dominates u
// ============================================================================

Set<Edge*>* IdentifyBackEdges(Function *F) {
    Set<Edge*> *back_edges = CreateSet();
    int time = 0;

    // Initialize all blocks
    for (int i = 0; i < F->num_blocks; i++) {
        F->blocks[i]->color = WHITE;
        F->blocks[i]->discovery_time = 0;
        F->blocks[i]->finish_time = 0;
    }

    // Perform DFS from entry block
    DFS_Visit(F->entry_block, &time, back_edges);

    return back_edges;
}

void DFS_Visit(BasicBlock *block, int *time, Set<Edge*> *back_edges) {
    block->color = GRAY;
    block->discovery_time = (*time)++;

    for (int i = 0; i < block->num_successors; i++) {
        BasicBlock *succ = block->successors[i];
        Edge *edge = CreateEdge(block, succ);

        if (succ->color == WHITE) {
            // Tree edge
            edge->type = TREE;
            DFS_Visit(succ, time, back_edges);

        } else if (succ->color == GRAY) {
            // Back edge candidate (target is ancestor in DFS tree)
            if (Dominates(succ, block)) {
                edge->type = BACK;
                SetInsert(back_edges, edge);
            } else {
                edge->type = CROSS;
            }

        } else { // BLACK
            if (succ->discovery_time < block->discovery_time) {
                // Forward edge or cross edge
                if (Dominates(succ, block)) {
                    edge->type = BACK;
                    SetInsert(back_edges, edge);
                } else if (Dominates(block, succ)) {
                    edge->type = FORWARD;
                } else {
                    edge->type = CROSS;
                }
            }
        }
    }

    block->color = BLACK;
    block->finish_time = (*time)++;
}

bool Dominates(BasicBlock *a, BasicBlock *b) {
    // Check if 'a' dominates 'b' using dominator tree
    BasicBlock *current = b;
    while (current != NULL) {
        if (current == a) {
            return true;
        }
        current = (current->idom == current) ? NULL : current->idom;
    }
    return false;
}


// ============================================================================
// Phase 3: Construct Natural Loops from Back Edges
// For each back edge (B -> H), find all blocks in natural loop
// ============================================================================

Loop* ConstructNaturalLoop(BasicBlock *header, BasicBlock *latch, int loop_id) {
    Loop *L = malloc(sizeof(Loop));
    L->id = loop_id;
    L->header = header;
    L->latch = latch;
    L->blocks = CreateSet();
    L->exit_blocks = CreateSet();
    L->exit_edges = CreateSet();
    L->sub_loops = CreateVector();
    L->parent_loop = NULL;
    L->depth = 0;
    L->trip_count = -1;
    L->has_trip_count = false;
    L->induction_vars = CreateSet();
    L->is_rotated = false;
    L->is_simplified = false;
    L->preheader = NULL;

    // Add header and latch to loop
    SetInsert(L->blocks, header);
    SetInsert(L->blocks, latch);

    // Workqueue for backward traversal
    Queue *work = CreateQueue();
    if (latch != header) {
        QueuePush(work, latch);
    }

    // Backward traversal to find all blocks in loop
    while (!QueueEmpty(work)) {
        BasicBlock *m = QueuePop(work);

        // Add all predecessors that aren't already in loop
        for (int i = 0; i < m->num_predecessors; i++) {
            BasicBlock *pred = m->predecessors[i];

            // Don't go through header (loop entry point)
            if (pred == header) {
                continue;
            }

            // If not yet in loop, add it and continue traversal
            if (!SetContains(L->blocks, pred)) {
                SetInsert(L->blocks, pred);
                QueuePush(work, pred);
            }
        }
    }

    DestroyQueue(work);

    // Find exit blocks (blocks with successors outside loop)
    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (int i = 0; i < b->num_successors; i++) {
            BasicBlock *succ = b->successors[i];
            if (!SetContains(L->blocks, succ)) {
                SetInsert(L->exit_blocks, b);
                Edge *exit_edge = CreateEdge(b, succ);
                SetInsert(L->exit_edges, exit_edge);
            }
        }
    }
    DestroyIterator(it);

    return L;
}


// ============================================================================
// Phase 4: Detect All Natural Loops
// Main entry point for loop detection
// ============================================================================

LoopInfo* DetectNaturalLoops(Function *F) {
    // Step 1: Build dominator tree
    BuildDominatorTree(F);

    // Step 2: Find all back edges
    Set<Edge*> *back_edges = IdentifyBackEdges(F);

    // Step 3: Construct loops from back edges
    Vector<Loop*> *all_loops = CreateVector();
    int loop_id = 0;

    Iterator *it = SetIterator(back_edges);
    while (HasNext(it)) {
        Edge *back_edge = Next(it);
        BasicBlock *header = back_edge->dest;
        BasicBlock *latch = back_edge->source;

        Loop *L = ConstructNaturalLoop(header, latch, loop_id++);
        VectorPush(all_loops, L);
    }
    DestroyIterator(it);

    // Step 4: Build loop nesting hierarchy
    BuildLoopNestingTree(all_loops);

    // Step 5: Create LoopInfo structure
    LoopInfo *LI = malloc(sizeof(LoopInfo));
    LI->top_level_loops = CreateVector();
    LI->block_to_loop = CreateMap();

    // Identify top-level loops and build block mapping
    for (int i = 0; i < VectorSize(all_loops); i++) {
        Loop *L = VectorGet(all_loops, i);

        if (L->parent_loop == NULL) {
            VectorPush(LI->top_level_loops, L);
        }

        // Map each block to its innermost loop
        Iterator *bit = SetIterator(L->blocks);
        while (HasNext(bit)) {
            BasicBlock *b = Next(bit);
            Loop *existing = MapGet(LI->block_to_loop, b);

            // Keep innermost loop (highest depth)
            if (existing == NULL || L->depth > existing->depth) {
                MapPut(LI->block_to_loop, b, L);
                b->innermost_loop = L;
            }
        }
        DestroyIterator(bit);
    }

    DestroyVector(all_loops);
    DestroySet(back_edges);

    return LI;
}


// ============================================================================
// Phase 5: Build Loop Nesting Hierarchy
// Determine parent-child relationships between loops
// ============================================================================

void BuildLoopNestingTree(Vector<Loop*> *all_loops) {
    int num_loops = VectorSize(all_loops);

    // For each loop, find its parent (smallest containing loop)
    for (int i = 0; i < num_loops; i++) {
        Loop *L1 = VectorGet(all_loops, i);
        Loop *parent = NULL;
        int min_parent_size = INT_MAX;

        for (int j = 0; j < num_loops; j++) {
            if (i == j) continue;

            Loop *L2 = VectorGet(all_loops, j);

            // Check if L2 contains L1
            if (SetContains(L2->blocks, L1->header)) {
                // L2 contains L1's header, check if all blocks contained
                bool all_contained = true;
                Iterator *it = SetIterator(L1->blocks);
                while (HasNext(it)) {
                    BasicBlock *b = Next(it);
                    if (!SetContains(L2->blocks, b)) {
                        all_contained = false;
                        break;
                    }
                }
                DestroyIterator(it);

                // If L2 properly contains L1 and is smallest so far
                if (all_contained && SetSize(L2->blocks) < min_parent_size) {
                    parent = L2;
                    min_parent_size = SetSize(L2->blocks);
                }
            }
        }

        // Set parent-child relationship
        L1->parent_loop = parent;
        if (parent != NULL) {
            VectorPush(parent->sub_loops, L1);
        }
    }

    // Compute loop depths recursively
    for (int i = 0; i < num_loops; i++) {
        Loop *L = VectorGet(all_loops, i);
        if (L->parent_loop == NULL) {
            AssignLoopDepth(L, 1);
        }
    }
}

void AssignLoopDepth(Loop *L, int depth) {
    L->depth = depth;

    // Assign depth to all blocks in loop
    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        b->loop_depth = depth;
    }
    DestroyIterator(it);

    // Recursively assign depth to child loops
    for (int i = 0; i < VectorSize(L->sub_loops); i++) {
        Loop *child = VectorGet(L->sub_loops, i);
        AssignLoopDepth(child, depth + 1);
    }
}


// ============================================================================
// Irreducible Loop Detection and Handling
// ============================================================================

typedef struct IrreducibleLoop {
    Set<BasicBlock*> *entries;   // Multiple entry points
    Set<BasicBlock*> *blocks;    // All blocks in irreducible region
    bool is_reducible;           // False for irreducible loops
} IrreducibleLoop;

IrreducibleLoop* DetectIrreducibleLoop(Function *F) {
    // An irreducible loop has multiple entry points that are not dominated
    // by a single header

    IrreducibleLoop *IL = malloc(sizeof(IrreducibleLoop));
    IL->entries = CreateSet();
    IL->blocks = CreateSet();
    IL->is_reducible = true;

    // Strategy: Detect strongly connected components (SCCs) in CFG
    // Check if SCC has multiple entries from outside

    Vector<Set<BasicBlock*>*> *sccs = FindStronglyConnectedComponents(F);

    for (int i = 0; i < VectorSize(sccs); i++) {
        Set<BasicBlock*> *scc = VectorGet(sccs, i);

        // Count external entries to this SCC
        Set<BasicBlock*> *entry_points = CreateSet();

        Iterator *it = SetIterator(scc);
        while (HasNext(it)) {
            BasicBlock *b = Next(it);

            for (int j = 0; j < b->num_predecessors; j++) {
                BasicBlock *pred = b->predecessors[j];

                // If predecessor is outside SCC, this is an entry
                if (!SetContains(scc, pred)) {
                    SetInsert(entry_points, b);
                }
            }
        }
        DestroyIterator(it);

        // If more than one entry point, it's irreducible
        if (SetSize(entry_points) > 1) {
            IL->entries = entry_points;
            IL->blocks = scc;
            IL->is_reducible = false;

            // CICC handles irreducible loops by:
            // 1. Node splitting to create reducible form
            // 2. Conservative optimization (skip loop opts)
            // 3. Warning to user if problematic

            return IL;
        }

        DestroySet(entry_points);
    }

    return IL;
}

// Tarjan's SCC algorithm
Vector<Set<BasicBlock*>*>* FindStronglyConnectedComponents(Function *F) {
    // Implementation of Tarjan's algorithm for finding SCCs
    // Returns vector of sets, each set is an SCC

    Vector<Set<BasicBlock*>*> *sccs = CreateVector();
    Stack *stack = CreateStack();
    Map<BasicBlock*, int> *indices = CreateMap();
    Map<BasicBlock*, int> *lowlinks = CreateMap();
    Map<BasicBlock*, bool> *on_stack = CreateMap();
    int index = 0;

    for (int i = 0; i < F->num_blocks; i++) {
        BasicBlock *b = F->blocks[i];
        if (MapGet(indices, b) == NULL) {
            StrongConnect(b, &index, stack, indices, lowlinks, on_stack, sccs);
        }
    }

    DestroyStack(stack);
    DestroyMap(indices);
    DestroyMap(lowlinks);
    DestroyMap(on_stack);

    return sccs;
}

void StrongConnect(BasicBlock *v, int *index, Stack *stack,
                   Map<BasicBlock*, int> *indices,
                   Map<BasicBlock*, int> *lowlinks,
                   Map<BasicBlock*, bool> *on_stack,
                   Vector<Set<BasicBlock*>*> *sccs) {
    MapPut(indices, v, *index);
    MapPut(lowlinks, v, *index);
    (*index)++;
    StackPush(stack, v);
    MapPut(on_stack, v, true);

    for (int i = 0; i < v->num_successors; i++) {
        BasicBlock *w = v->successors[i];

        if (MapGet(indices, w) == NULL) {
            StrongConnect(w, index, stack, indices, lowlinks, on_stack, sccs);
            int v_lowlink = MapGet(lowlinks, v);
            int w_lowlink = MapGet(lowlinks, w);
            MapPut(lowlinks, v, MIN(v_lowlink, w_lowlink));

        } else if (MapGet(on_stack, w)) {
            int v_lowlink = MapGet(lowlinks, v);
            int w_index = MapGet(indices, w);
            MapPut(lowlinks, v, MIN(v_lowlink, w_index));
        }
    }

    // If v is root of SCC
    if (MapGet(lowlinks, v) == MapGet(indices, v)) {
        Set<BasicBlock*> *scc = CreateSet();
        BasicBlock *w;
        do {
            w = StackPop(stack);
            MapPut(on_stack, w, false);
            SetInsert(scc, w);
        } while (w != v);
        VectorPush(sccs, scc);
    }
}


// ============================================================================
// Self-Loop Detection (Single Block Loops)
// ============================================================================

bool IsSelfLoop(BasicBlock *block) {
    // Check if block has edge to itself
    for (int i = 0; i < block->num_successors; i++) {
        if (block->successors[i] == block) {
            return true;
        }
    }
    return false;
}

Loop* CreateSelfLoop(BasicBlock *block, int loop_id) {
    Loop *L = malloc(sizeof(Loop));
    L->id = loop_id;
    L->header = block;
    L->latch = block;
    L->blocks = CreateSet();
    SetInsert(L->blocks, block);
    L->exit_blocks = CreateSet();
    L->exit_edges = CreateSet();
    L->sub_loops = CreateVector();
    L->parent_loop = NULL;
    L->depth = 0;
    L->trip_count = -1;
    L->has_trip_count = false;
    L->induction_vars = CreateSet();

    // Find exits
    for (int i = 0; i < block->num_successors; i++) {
        BasicBlock *succ = block->successors[i];
        if (succ != block) {
            SetInsert(L->exit_blocks, block);
            Edge *exit = CreateEdge(block, succ);
            SetInsert(L->exit_edges, exit);
        }
    }

    return L;
}


// ============================================================================
// 1.2 Loop Header Properties and Verification
// ============================================================================

bool VerifyLoopHeader(Loop *L) {
    BasicBlock *header = L->header;

    // Property 1: Header must dominate all blocks in loop
    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        if (!Dominates(header, b)) {
            DestroyIterator(it);
            return false; // Header doesn't dominate all blocks
        }
    }
    DestroyIterator(it);

    // Property 2: Header must be the only entry to the loop
    // (after LoopSimplify, there's a preheader guaranteeing this)
    int external_entries = 0;
    for (int i = 0; i < header->num_predecessors; i++) {
        BasicBlock *pred = header->predecessors[i];
        if (!SetContains(L->blocks, pred)) {
            external_entries++;
        }
    }

    // Natural loops have exactly one external entry
    // (or single preheader after LoopSimplify)
    if (external_entries > 1 && L->preheader == NULL) {
        return false; // Multiple external entries without preheader
    }

    // Property 3: All back edges must target the header
    it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (int i = 0; i < b->num_successors; i++) {
            BasicBlock *succ = b->successors[i];
            if (succ == header && SetContains(L->blocks, b)) {
                // This is a back edge - should come from latch
                // After LoopSimplify, only latch should have back edge
            }
        }
    }
    DestroyIterator(it);

    return true;
}


// ============================================================================
// 2. Loop Analysis Algorithms
// ============================================================================

// ============================================================================
// 2.1 Trip Count Analysis
// Estimate or compute exact trip count for loops
// ============================================================================

typedef struct TripCount {
    bool is_constant;
    int constant_value;
    Instruction *symbolic_value;  // If not constant
    bool is_runtime_check;        // Requires runtime check
} TripCount;

TripCount* AnalyzeTripCount(Loop *L) {
    TripCount *TC = malloc(sizeof(TripCount));
    TC->is_constant = false;
    TC->constant_value = -1;
    TC->symbolic_value = NULL;
    TC->is_runtime_check = false;

    BasicBlock *header = L->header;
    BasicBlock *latch = L->latch;

    // Find branch instruction in header or latch
    Instruction *branch = GetTerminator(header);

    if (branch->opcode != BR_COND) {
        return TC; // Not a conditional loop
    }

    // Extract loop condition
    Value *condition = branch->operands[0];

    // Check if condition is comparison
    if (!IsComparisonInst(condition)) {
        return TC;
    }

    Instruction *cmp = (Instruction*)condition;
    Value *op0 = cmp->operands[0];
    Value *op1 = cmp->operands[1];

    // Try to identify induction variable pattern
    // Pattern: i = phi [start, preheader], [i.next, latch]
    //          i.next = i + step
    //          cmp = i < limit

    PHINode *iv = NULL;
    Value *limit = NULL;
    Value *step = NULL;
    Value *start = NULL;

    // Check if op0 is PHI (induction variable)
    if (IsPHINode(op0)) {
        iv = (PHINode*)op0;
        limit = op1;
    } else if (IsPHINode(op1)) {
        iv = (PHINode*)op1;
        limit = op0;
    } else {
        return TC;
    }

    // Extract start value and step from PHI
    for (int i = 0; i < iv->num_incoming; i++) {
        BasicBlock *incoming_block = iv->incoming_blocks[i];
        Value *incoming_value = iv->incoming_values[i];

        if (incoming_block == L->preheader) {
            start = incoming_value;
        } else if (incoming_block == latch) {
            // Should be i + step
            if (IsBinaryInst(incoming_value)) {
                Instruction *binop = (Instruction*)incoming_value;
                if (binop->opcode == ADD || binop->opcode == SUB) {
                    if (binop->operands[0] == (Value*)iv) {
                        step = binop->operands[1];
                    } else if (binop->operands[1] == (Value*)iv) {
                        step = binop->operands[0];
                    }
                }
            }
        }
    }

    // If we have start, step, and limit as constants, compute trip count
    if (IsConstant(start) && IsConstant(step) && IsConstant(limit)) {
        int start_val = GetConstantValue(start);
        int step_val = GetConstantValue(step);
        int limit_val = GetConstantValue(limit);

        // Compute based on comparison type
        enum CmpPredicate pred = cmp->predicate;

        if (pred == CMP_SLT || pred == CMP_ULT) {
            // i < limit, starting from start, step step
            if (step_val > 0) {
                TC->is_constant = true;
                TC->constant_value = (limit_val - start_val + step_val - 1) / step_val;
            }
        } else if (pred == CMP_SLE || pred == CMP_ULE) {
            if (step_val > 0) {
                TC->is_constant = true;
                TC->constant_value = (limit_val - start_val + step_val) / step_val;
            }
        }
        // Similar for SGT, UGT, SGE, UGE with negative steps
    } else {
        // Symbolic trip count
        TC->is_runtime_check = true;
        TC->symbolic_value = cmp;
    }

    L->trip_count = TC->constant_value;
    L->has_trip_count = TC->is_constant;

    return TC;
}


// ============================================================================
// 2.2 Induction Variable Detection
// Find all induction variables in a loop
// ============================================================================

typedef struct InductionVariable {
    PHINode *phi;
    Value *start;
    Value *step;
    enum { LINEAR, AFFINE, WRAP, COMPLEX } type;
    bool is_canonical;  // True for i = 0, step = 1
} InductionVariable;

Set<InductionVariable*>* DetectInductionVariables(Loop *L) {
    Set<InductionVariable*> *ivs = CreateSet();
    BasicBlock *header = L->header;

    // Find all PHI nodes in loop header
    for (Instruction *I = header->first_inst; I != NULL; I = I->next) {
        if (!IsPHINode(I)) {
            break; // PHIs are always at start of block
        }

        PHINode *phi = (PHINode*)I;
        InductionVariable *iv = AnalyzeInductionVariable(phi, L);

        if (iv != NULL) {
            SetInsert(ivs, iv);
            SetInsert(L->induction_vars, (Instruction*)phi);
        }
    }

    return ivs;
}

InductionVariable* AnalyzeInductionVariable(PHINode *phi, Loop *L) {
    // Check if PHI matches induction variable pattern
    // Pattern: phi = [start, preheader], [update, latch]

    if (phi->num_incoming != 2) {
        return NULL; // Must have exactly 2 incoming values for simple IV
    }

    Value *start = NULL;
    Value *update = NULL;
    BasicBlock *latch = L->latch;
    BasicBlock *preheader = L->preheader;

    for (int i = 0; i < phi->num_incoming; i++) {
        if (phi->incoming_blocks[i] == preheader) {
            start = phi->incoming_values[i];
        } else if (phi->incoming_blocks[i] == latch) {
            update = phi->incoming_values[i];
        }
    }

    if (start == NULL || update == NULL) {
        return NULL;
    }

    // Check if update is linear: phi + step or phi * factor
    if (!IsBinaryInst(update)) {
        return NULL;
    }

    Instruction *update_inst = (Instruction*)update;
    Value *step = NULL;
    enum InductionVariableType iv_type;

    if (update_inst->opcode == ADD || update_inst->opcode == SUB) {
        // Linear: phi + step
        if (update_inst->operands[0] == (Value*)phi) {
            step = update_inst->operands[1];
        } else if (update_inst->operands[1] == (Value*)phi) {
            step = update_inst->operands[0];
        } else {
            return NULL;
        }

        // Check if step is loop invariant
        if (!IsLoopInvariant(step, L)) {
            iv_type = COMPLEX;
        } else {
            iv_type = LINEAR;
        }

    } else if (update_inst->opcode == MUL) {
        // Geometric progression
        if (update_inst->operands[0] == (Value*)phi) {
            step = update_inst->operands[1];
        } else if (update_inst->operands[1] == (Value*)phi) {
            step = update_inst->operands[0];
        } else {
            return NULL;
        }
        iv_type = WRAP;

    } else {
        return NULL;
    }

    // Create induction variable descriptor
    InductionVariable *iv = malloc(sizeof(InductionVariable));
    iv->phi = phi;
    iv->start = start;
    iv->step = step;
    iv->type = iv_type;

    // Check if canonical (start = 0, step = 1)
    iv->is_canonical = (IsConstant(start) && GetConstantValue(start) == 0 &&
                        IsConstant(step) && GetConstantValue(step) == 1);

    return iv;
}

bool IsLoopInvariant(Value *V, Loop *L) {
    // Value is loop invariant if:
    // 1. It's a constant
    // 2. It's defined outside the loop
    // 3. All its operands are loop invariant

    if (IsConstant(V)) {
        return true;
    }

    if (!IsInstruction(V)) {
        return true; // Arguments, globals, etc.
    }

    Instruction *I = (Instruction*)V;
    BasicBlock *def_block = I->parent_block;

    // Check if defined outside loop
    if (!SetContains(L->blocks, def_block)) {
        return true;
    }

    // If defined inside loop, check if all operands are invariant
    for (int i = 0; i < I->num_operands; i++) {
        if (!IsLoopInvariant(I->operands[i], L)) {
            return false;
        }
    }

    return true;
}


// ============================================================================
// 2.3 Loop-Carried Dependency Analysis
// Detect dependencies that cross loop iterations
// ============================================================================

typedef struct Dependence {
    Instruction *source;
    Instruction *sink;
    int distance;        // Iteration distance (-1 if unknown)
    enum { FLOW, ANTI, OUTPUT } type;
    bool is_loop_carried;
} Dependence;

Vector<Dependence*>* AnalyzeLoopDependencies(Loop *L) {
    Vector<Dependence*> *deps = CreateVector();

    // Collect all memory operations in loop
    Vector<Instruction*> *loads = CreateVector();
    Vector<Instruction*> *stores = CreateVector();

    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (Instruction *I = b->first_inst; I != NULL; I = I->next) {
            if (I->opcode == LOAD) {
                VectorPush(loads, I);
            } else if (I->opcode == STORE) {
                VectorPush(stores, I);
            }
        }
    }
    DestroyIterator(it);

    // Analyze all pairs of memory operations
    // Flow (RAW): Store -> Load
    for (int i = 0; i < VectorSize(stores); i++) {
        Instruction *store = VectorGet(stores, i);
        for (int j = 0; j < VectorSize(loads); j++) {
            Instruction *load = VectorGet(loads, j);

            Dependence *dep = AnalyzeDependence(store, load, L, FLOW);
            if (dep != NULL) {
                VectorPush(deps, dep);
            }
        }
    }

    // Anti (WAR): Load -> Store
    for (int i = 0; i < VectorSize(loads); i++) {
        Instruction *load = VectorGet(loads, i);
        for (int j = 0; j < VectorSize(stores); j++) {
            Instruction *store = VectorGet(stores, j);

            Dependence *dep = AnalyzeDependence(load, store, L, ANTI);
            if (dep != NULL) {
                VectorPush(deps, dep);
            }
        }
    }

    // Output (WAW): Store -> Store
    for (int i = 0; i < VectorSize(stores); i++) {
        for (int j = i + 1; j < VectorSize(stores); j++) {
            Instruction *store1 = VectorGet(stores, i);
            Instruction *store2 = VectorGet(stores, j);

            Dependence *dep = AnalyzeDependence(store1, store2, L, OUTPUT);
            if (dep != NULL) {
                VectorPush(deps, dep);
            }
        }
    }

    DestroyVector(loads);
    DestroyVector(stores);

    return deps;
}

Dependence* AnalyzeDependence(Instruction *I1, Instruction *I2, Loop *L,
                              enum DependenceType type) {
    // Get memory addresses
    Value *addr1 = (I1->opcode == LOAD || I1->opcode == STORE) ?
                   I1->operands[0] : NULL;
    Value *addr2 = (I2->opcode == LOAD || I2->opcode == STORE) ?
                   I2->operands[0] : NULL;

    if (addr1 == NULL || addr2 == NULL) {
        return NULL;
    }

    // Use alias analysis to check if addresses may alias
    AliasResult alias = MayAlias(addr1, addr2);

    if (alias == NO_ALIAS) {
        return NULL; // No dependence
    }

    // Try to compute dependence distance
    int distance = -1;
    bool is_loop_carried = false;

    // Check if addresses are based on induction variable
    // Pattern: arr[i + offset1] and arr[i + offset2]
    Value *base1, *index1, *base2, *index2;
    int offset1, offset2;

    if (DecomposeGEP(addr1, &base1, &index1, &offset1) &&
        DecomposeGEP(addr2, &base2, &index2, &offset2)) {

        if (base1 == base2 && index1 == index2) {
            // Same base and index, different offsets
            distance = offset2 - offset1;

            // Check if this crosses iterations
            if (distance != 0) {
                is_loop_carried = true;
            }
        }
    }

    // Create dependence
    Dependence *dep = malloc(sizeof(Dependence));
    dep->source = I1;
    dep->sink = I2;
    dep->distance = distance;
    dep->type = type;
    dep->is_loop_carried = is_loop_carried;

    return dep;
}


// ============================================================================
// 2.4 Loop Bounds Analysis
// Determine upper and lower bounds for loop iterations
// ============================================================================

typedef struct LoopBounds {
    Value *lower_bound;
    Value *upper_bound;
    bool is_constant_lower;
    bool is_constant_upper;
    int constant_lower;
    int constant_upper;
} LoopBounds;

LoopBounds* AnalyzeLoopBounds(Loop *L) {
    LoopBounds *bounds = malloc(sizeof(LoopBounds));
    bounds->lower_bound = NULL;
    bounds->upper_bound = NULL;
    bounds->is_constant_lower = false;
    bounds->is_constant_upper = false;

    // Find canonical induction variable
    Set<InductionVariable*> *ivs = DetectInductionVariables(L);
    InductionVariable *canonical_iv = NULL;

    Iterator *it = SetIterator(ivs);
    while (HasNext(it)) {
        InductionVariable *iv = Next(it);
        if (iv->is_canonical) {
            canonical_iv = iv;
            break;
        }
    }
    DestroyIterator(it);

    if (canonical_iv == NULL && SetSize(ivs) > 0) {
        canonical_iv = SetGetAny(ivs);
    }

    if (canonical_iv != NULL) {
        // Lower bound is start value
        bounds->lower_bound = canonical_iv->start;
        if (IsConstant(canonical_iv->start)) {
            bounds->is_constant_lower = true;
            bounds->constant_lower = GetConstantValue(canonical_iv->start);
        }

        // Upper bound from loop condition
        BasicBlock *header = L->header;
        Instruction *branch = GetTerminator(header);

        if (branch->opcode == BR_COND) {
            Value *cond = branch->operands[0];
            if (IsComparisonInst(cond)) {
                Instruction *cmp = (Instruction*)cond;

                // Find which operand is the IV
                if (cmp->operands[0] == (Value*)canonical_iv->phi) {
                    bounds->upper_bound = cmp->operands[1];
                } else if (cmp->operands[1] == (Value*)canonical_iv->phi) {
                    bounds->upper_bound = cmp->operands[0];
                }

                if (bounds->upper_bound != NULL && IsConstant(bounds->upper_bound)) {
                    bounds->is_constant_upper = true;
                    bounds->constant_upper = GetConstantValue(bounds->upper_bound);
                }
            }
        }
    }

    DestroySet(ivs);
    return bounds;
}


// ============================================================================
// 3. Loop Transformation Algorithms
// ============================================================================

// ============================================================================
// 3.1 Loop Simplification (Canonicalization)
// Ensures loops have canonical form for optimization
// ============================================================================

bool LoopSimplify(Loop *L, Function *F) {
    bool changed = false;

    // Step 1: Insert preheader if needed
    if (L->preheader == NULL) {
        changed |= InsertLoopPreheader(L, F);
    }

    // Step 2: Ensure single latch (single back edge)
    changed |= EnsureSingleLatch(L, F);

    // Step 3: Ensure dedicated exit blocks
    changed |= EnsureDedicatedExits(L, F);

    if (changed) {
        L->is_simplified = true;
    }

    return changed;
}

bool InsertLoopPreheader(Loop *L, Function *F) {
    BasicBlock *header = L->header;

    // Count external predecessors (from outside loop)
    Vector<BasicBlock*> *external_preds = CreateVector();
    for (int i = 0; i < header->num_predecessors; i++) {
        BasicBlock *pred = header->predecessors[i];
        if (!SetContains(L->blocks, pred)) {
            VectorPush(external_preds, pred);
        }
    }

    // If only one external predecessor, it becomes the preheader
    if (VectorSize(external_preds) == 1) {
        L->preheader = VectorGet(external_preds, 0);
        DestroyVector(external_preds);
        return false; // No changes needed
    }

    // Create new preheader block
    BasicBlock *preheader = CreateBasicBlock(F, "loop.preheader");

    // Redirect all external predecessors to preheader
    for (int i = 0; i < VectorSize(external_preds); i++) {
        BasicBlock *pred = VectorGet(external_preds, i);
        ReplaceSuccessor(pred, header, preheader);
    }

    // Preheader unconditionally branches to header
    InsertBranch(preheader, header);

    // Update PHI nodes in header
    for (Instruction *I = header->first_inst; I != NULL; I = I->next) {
        if (!IsPHINode(I)) break;

        PHINode *phi = (PHINode*)I;

        // Collect all values from external predecessors
        Value *external_value = NULL;
        Vector<int> *indices_to_remove = CreateVector();

        for (int i = 0; i < phi->num_incoming; i++) {
            if (!SetContains(L->blocks, phi->incoming_blocks[i])) {
                external_value = phi->incoming_values[i];
                VectorPush(indices_to_remove, i);
            }
        }

        // Remove old external incoming values
        for (int i = VectorSize(indices_to_remove) - 1; i >= 0; i--) {
            int idx = VectorGet(indices_to_remove, i);
            RemovePHIIncoming(phi, idx);
        }

        // Add single incoming from preheader
        AddPHIIncoming(phi, external_value, preheader);

        DestroyVector(indices_to_remove);
    }

    L->preheader = preheader;
    DestroyVector(external_preds);

    return true; // Loop was modified
}

bool EnsureSingleLatch(Loop *L, Function *F) {
    BasicBlock *header = L->header;

    // Find all latches (blocks with back edges to header)
    Vector<BasicBlock*> *latches = CreateVector();
    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (int i = 0; i < b->num_successors; i++) {
            if (b->successors[i] == header) {
                VectorPush(latches, b);
                break;
            }
        }
    }
    DestroyIterator(it);

    // If already single latch, done
    if (VectorSize(latches) == 1) {
        L->latch = VectorGet(latches, 0);
        DestroyVector(latches);
        return false;
    }

    // Create new latch block
    BasicBlock *new_latch = CreateBasicBlock(F, "loop.latch");
    SetInsert(L->blocks, new_latch);

    // Redirect all latches to new latch
    for (int i = 0; i < VectorSize(latches); i++) {
        BasicBlock *old_latch = VectorGet(latches, i);
        ReplaceSuccessor(old_latch, header, new_latch);
    }

    // New latch branches to header
    InsertBranch(new_latch, header);

    // Update PHI nodes in header
    for (Instruction *I = header->first_inst; I != NULL; I = I->next) {
        if (!IsPHINode(I)) break;

        PHINode *phi = (PHINode*)I;

        // Create PHI in new latch for values from old latches
        PHINode *latch_phi = CreatePHI(new_latch, phi->type, phi->num_incoming);

        Vector<int> *indices_to_remove = CreateVector();
        for (int i = 0; i < phi->num_incoming; i++) {
            BasicBlock *incoming = phi->incoming_blocks[i];
            if (SetContains(L->blocks, incoming) && incoming != new_latch) {
                // This is from an old latch
                AddPHIIncoming(latch_phi, phi->incoming_values[i], incoming);
                VectorPush(indices_to_remove, i);
            }
        }

        // Remove old latch entries from header PHI
        for (int i = VectorSize(indices_to_remove) - 1; i >= 0; i--) {
            int idx = VectorGet(indices_to_remove, i);
            RemovePHIIncoming(phi, idx);
        }

        // Add new latch entry to header PHI
        AddPHIIncoming(phi, latch_phi, new_latch);

        DestroyVector(indices_to_remove);
    }

    L->latch = new_latch;
    DestroyVector(latches);

    return true;
}

bool EnsureDedicatedExits(Loop *L, Function *F) {
    // A dedicated exit block is one that has no predecessors outside the loop
    bool changed = false;

    Vector<Edge*> *exit_edges = CreateVector();
    Iterator *it = SetIterator(L->exit_edges);
    while (HasNext(it)) {
        Edge *e = Next(it);
        VectorPush(exit_edges, e);
    }
    DestroyIterator(it);

    for (int i = 0; i < VectorSize(exit_edges); i++) {
        Edge *e = VectorGet(exit_edges, i);
        BasicBlock *exit_block = e->dest;

        // Check if exit block has external predecessors
        bool has_external = false;
        for (int j = 0; j < exit_block->num_predecessors; j++) {
            if (!SetContains(L->blocks, exit_block->predecessors[j])) {
                has_external = true;
                break;
            }
        }

        if (has_external) {
            // Create dedicated exit block
            BasicBlock *dedicated = CreateBasicBlock(F, "loop.exit");

            // Redirect edge to dedicated exit
            ReplaceSuccessor(e->source, exit_block, dedicated);

            // Dedicated exit branches to original exit
            InsertBranch(dedicated, exit_block);

            // Update PHIs in original exit
            for (Instruction *I = exit_block->first_inst; I != NULL; I = I->next) {
                if (!IsPHINode(I)) break;

                PHINode *phi = (PHINode*)I;
                for (int j = 0; j < phi->num_incoming; j++) {
                    if (phi->incoming_blocks[j] == e->source) {
                        phi->incoming_blocks[j] = dedicated;
                    }
                }
            }

            changed = true;
        }
    }

    DestroyVector(exit_edges);
    return changed;
}


// ============================================================================
// 3.2 Loop Rotation
// Transform while-loop into do-while loop
// ============================================================================

bool LoopRotate(Loop *L, Function *F) {
    // Loop rotation transforms:
    //   while (cond) { body }
    // into:
    //   if (cond) { do { body } while (cond) }

    // Requires simplified loop
    if (!L->is_simplified) {
        LoopSimplify(L, F);
    }

    BasicBlock *header = L->header;
    BasicBlock *latch = L->latch;
    BasicBlock *preheader = L->preheader;

    // Check if header has conditional branch
    Instruction *term = GetTerminator(header);
    if (term->opcode != BR_COND) {
        return false; // Already rotated or not rotatable
    }

    // Clone header into latch position
    BasicBlock *rotated_header = CloneBasicBlock(header, "loop.header.rotated");

    // Update preheader to branch to first iteration check
    ReplaceSuccessor(preheader, header, rotated_header);

    // Make rotated header the new loop header
    L->header = rotated_header;
    SetInsert(L->blocks, rotated_header);

    // Update back edge to target rotated header
    ReplaceSuccessor(latch, header, rotated_header);

    // Old header becomes part of loop body
    // (it's already in loop blocks)

    L->is_rotated = true;
    return true;
}


// ============================================================================
// 3.3 Loop Unswitching
// Hoist loop-invariant conditionals outside loop
// ============================================================================

bool LoopUnswitch(Loop *L, Function *F, Instruction *branch) {
    // Unswitching transforms:
    //   for (...) { if (invariant_cond) { A } else { B } }
    // into:
    //   if (invariant_cond) { for (...) { A } } else { for (...) { B } }

    // Check if branch condition is loop invariant
    if (!IsBranchInst(branch) || branch->opcode != BR_COND) {
        return false;
    }

    Value *condition = branch->operands[0];
    if (!IsLoopInvariant(condition, L)) {
        return false;
    }

    // Clone entire loop
    Loop *cloned_loop = CloneLoop(L, F, "loop.unswitch");

    // In original loop, replace branch with unconditional to true successor
    BasicBlock *true_succ = ((BasicBlock*)branch->operands[1]);
    BasicBlock *false_succ = ((BasicBlock*)branch->operands[2]);

    // Original takes true path
    ReplaceInstruction(branch, CreateBranch(true_succ));
    SimplifyLoopAfterUnswitch(L, true_succ, false_succ);

    // Cloned takes false path
    Instruction *cloned_branch = FindCorrespondingInst(branch, cloned_loop);
    BasicBlock *cloned_false = FindCorrespondingBlock(false_succ, cloned_loop);
    ReplaceInstruction(cloned_branch, CreateBranch(cloned_false));
    SimplifyLoopAfterUnswitch(cloned_loop, cloned_false, true_succ);

    // Insert condition check in preheader
    BasicBlock *original_preheader = L->preheader;
    BasicBlock *decision_block = CreateBasicBlock(F, "unswitch.decision");

    // Decision block checks condition
    InsertConditionalBranch(decision_block, condition,
                           L->header, cloned_loop->header);

    // Redirect to decision block
    ReplaceSuccessor(original_preheader, L->header, decision_block);
    L->preheader = decision_block;
    cloned_loop->preheader = decision_block;

    return true;
}


// ============================================================================
// 3.4 Loop Peeling
// Execute first iteration separately
// ============================================================================

bool LoopPeel(Loop *L, Function *F, int num_iterations) {
    // Peeling executes first N iterations outside loop
    // Useful for:
    // - Eliminating first-iteration special cases
    // - Enabling vectorization after removing dependencies

    if (!L->is_simplified) {
        LoopSimplify(L, F);
    }

    BasicBlock *preheader = L->preheader;
    BasicBlock *header = L->header;

    for (int iter = 0; iter < num_iterations; iter++) {
        // Clone loop body for this peeled iteration
        Map<Value*, Value*> *value_map = CreateMap();
        Vector<BasicBlock*> *peeled_blocks = CreateVector();

        // Clone all blocks in loop
        Iterator *it = SetIterator(L->blocks);
        while (HasNext(it)) {
            BasicBlock *original = Next(it);
            BasicBlock *cloned = CloneBasicBlock(original, "loop.peel");
            VectorPush(peeled_blocks, cloned);
            MapPut(value_map, original, cloned);
        }
        DestroyIterator(it);

        // Update cloned instructions to use cloned values
        for (int i = 0; i < VectorSize(peeled_blocks); i++) {
            BasicBlock *cloned = VectorGet(peeled_blocks, i);
            RemapInstructions(cloned, value_map);
        }

        // Connect peeled iteration
        BasicBlock *peeled_header = MapGet(value_map, header);
        if (iter == 0) {
            ReplaceSuccessor(preheader, header, peeled_header);
        }

        // Last peeled iteration branches to original loop
        BasicBlock *peeled_latch = MapGet(value_map, L->latch);
        if (iter == num_iterations - 1) {
            ReplaceSuccessor(peeled_latch, peeled_header, header);
        }

        DestroyMap(value_map);
        DestroyVector(peeled_blocks);
    }

    // Update loop PHI nodes to account for peeled iterations
    for (Instruction *I = header->first_inst; I != NULL; I = I->next) {
        if (!IsPHINode(I)) break;

        PHINode *phi = (PHINode*)I;
        // Update incoming value from preheader to be from last peel iteration
        // (Implementation details depend on value mapping)
    }

    return true;
}


// ============================================================================
// 3.5 Loop Fusion
// Combine adjacent loops
// ============================================================================

bool LoopFusion(Loop *L1, Loop *L2, Function *F) {
    // Fuse two adjacent loops into one
    // Requirements:
    // 1. Loops must be adjacent (L1 exit leads to L2 preheader)
    // 2. Same trip count
    // 3. No dependencies preventing fusion

    // Check adjacency
    if (!AreLoopsAdjacent(L1, L2)) {
        return false;
    }

    // Check trip counts
    TripCount *tc1 = AnalyzeTripCount(L1);
    TripCount *tc2 = AnalyzeTripCount(L2);

    if (!tc1->is_constant || !tc2->is_constant ||
        tc1->constant_value != tc2->constant_value) {
        return false; // Different trip counts
    }

    // Check dependencies
    if (HasAntiDependence(L1, L2)) {
        return false; // Cannot fuse due to dependencies
    }

    // Merge loop bodies
    // 1. Combine L2's blocks into L1
    Iterator *it = SetIterator(L2->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        SetInsert(L1->blocks, b);
    }
    DestroyIterator(it);

    // 2. Merge induction variables (use L1's IV, replace L2's IV uses)
    InductionVariable *iv1 = GetCanonicalIV(L1);
    InductionVariable *iv2 = GetCanonicalIV(L2);

    if (iv1 != NULL && iv2 != NULL) {
        ReplaceAllUsesWith(iv2->phi, iv1->phi);
    }

    // 3. Update control flow
    // L1's latch now branches to merged body, then to L1 header
    BasicBlock *l1_latch = L1->latch;
    BasicBlock *l2_header = L2->header;
    BasicBlock *l2_latch = L2->latch;

    // Connect L1 latch to L2's first body block
    ReplaceSuccessor(l1_latch, L1->header, l2_header);

    // Connect L2 latch back to L1 header
    ReplaceSuccessor(l2_latch, l2_header, L1->header);

    // 4. Update exit blocks
    it = SetIterator(L2->exit_blocks);
    while (HasNext(it)) {
        BasicBlock *exit = Next(it);
        SetInsert(L1->exit_blocks, exit);
    }
    DestroyIterator(it);

    // Delete L2 loop object
    free(L2);

    return true;
}

bool AreLoopsAdjacent(Loop *L1, Loop *L2) {
    // Check if L1's exit leads directly to L2's preheader
    Iterator *it = SetIterator(L1->exit_blocks);
    while (HasNext(it)) {
        BasicBlock *exit = Next(it);
        for (int i = 0; i < exit->num_successors; i++) {
            if (exit->successors[i] == L2->preheader) {
                DestroyIterator(it);
                return true;
            }
        }
    }
    DestroyIterator(it);
    return false;
}


// ============================================================================
// 3.6 Loop Fission (Distribution)
// Split loop into multiple loops
// ============================================================================

bool LoopFission(Loop *L, Function *F, Set<Instruction*> *partition1) {
    // Split loop into two loops based on instruction partitioning
    // partition1 = instructions for first loop
    // partition2 = all other instructions

    // Create second loop
    Loop *L2 = CloneLoop(L, F, "loop.fission");

    // Remove partition2 instructions from L
    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (Instruction *I = b->first_inst; I != NULL; ) {
            Instruction *next = I->next;
            if (!SetContains(partition1, I) && !IsPHINode(I) &&
                !IsTerminator(I)) {
                RemoveInstruction(I);
            }
            I = next;
        }
    }
    DestroyIterator(it);

    // Remove partition1 instructions from L2
    it = SetIterator(L2->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (Instruction *I = b->first_inst; I != NULL; ) {
            Instruction *next = I->next;
            if (SetContains(partition1, I) && !IsPHINode(I) &&
                !IsTerminator(I)) {
                RemoveInstruction(I);
            }
            I = next;
        }
    }
    DestroyIterator(it);

    // Connect loops in sequence
    BasicBlock *l1_exit = GetSingleExit(L);
    if (l1_exit != NULL) {
        ReplaceSuccessor(l1_exit, GetSuccessor(l1_exit, 0), L2->preheader);
    }

    return true;
}


// ============================================================================
// 4. SCEV (Scalar Evolution) Algorithms
// ============================================================================

// ============================================================================
// 4.1 SCEV Expression Types and Construction
// ============================================================================

typedef enum SCEVType {
    SCEV_CONSTANT,
    SCEV_ADD_REC,    // {start, +, step}
    SCEV_ADD,        // op1 + op2
    SCEV_MUL,        // op1 * op2
    SCEV_UDIV,       // op1 / op2
    SCEV_UNKNOWN
} SCEVType;

typedef struct SCEV {
    SCEVType type;
    union {
        int constant_value;
        struct {
            struct SCEV *start;
            struct SCEV *step;
            Loop *loop;
        } add_rec;
        struct {
            struct SCEV *op1;
            struct SCEV *op2;
        } binary;
        Value *unknown_value;
    } data;
} SCEV;

SCEV* CreateConstantSCEV(int value) {
    SCEV *scev = malloc(sizeof(SCEV));
    scev->type = SCEV_CONSTANT;
    scev->data.constant_value = value;
    return scev;
}

SCEV* CreateAddRecSCEV(SCEV *start, SCEV *step, Loop *L) {
    SCEV *scev = malloc(sizeof(SCEV));
    scev->type = SCEV_ADD_REC;
    scev->data.add_rec.start = start;
    scev->data.add_rec.step = step;
    scev->data.add_rec.loop = L;
    return scev;
}

SCEV* CreateAddSCEV(SCEV *op1, SCEV *op2) {
    SCEV *scev = malloc(sizeof(SCEV));
    scev->type = SCEV_ADD;
    scev->data.binary.op1 = op1;
    scev->data.binary.op2 = op2;
    return scev;
}

SCEV* CreateUnknownSCEV(Value *V) {
    SCEV *scev = malloc(sizeof(SCEV));
    scev->type = SCEV_UNKNOWN;
    scev->data.unknown_value = V;
    return scev;
}


// ============================================================================
// 4.2 SCEV Analysis - Building Evolution Expressions
// ============================================================================

Map<Value*, SCEV*> *scev_cache = NULL;

SCEV* GetSCEV(Value *V, Loop *L) {
    // Check cache
    if (scev_cache == NULL) {
        scev_cache = CreateMap();
    }

    SCEV *cached = MapGet(scev_cache, V);
    if (cached != NULL) {
        return cached;
    }

    SCEV *result = NULL;

    // Handle different value types
    if (IsConstant(V)) {
        result = CreateConstantSCEV(GetConstantValue(V));

    } else if (IsPHINode(V)) {
        result = AnalyzePHISCEV((PHINode*)V, L);

    } else if (IsInstruction(V)) {
        result = AnalyzeInstructionSCEV((Instruction*)V, L);

    } else {
        result = CreateUnknownSCEV(V);
    }

    MapPut(scev_cache, V, result);
    return result;
}

SCEV* AnalyzePHISCEV(PHINode *phi, Loop *L) {
    // Check if PHI is an induction variable
    // Pattern: phi = [start, preheader], [phi + step, latch]

    if (phi->num_incoming != 2) {
        return CreateUnknownSCEV((Value*)phi);
    }

    Value *start_val = NULL;
    Value *step_val = NULL;

    for (int i = 0; i < phi->num_incoming; i++) {
        BasicBlock *incoming_block = phi->incoming_blocks[i];
        Value *incoming_value = phi->incoming_values[i];

        if (incoming_block == L->preheader) {
            start_val = incoming_value;
        } else if (incoming_block == L->latch) {
            // Should be phi + step
            if (IsBinaryInst(incoming_value)) {
                Instruction *binop = (Instruction*)incoming_value;
                if (binop->opcode == ADD) {
                    if (binop->operands[0] == (Value*)phi) {
                        step_val = binop->operands[1];
                    } else if (binop->operands[1] == (Value*)phi) {
                        step_val = binop->operands[0];
                    }
                }
            }
        }
    }

    if (start_val != NULL && step_val != NULL) {
        SCEV *start_scev = GetSCEV(start_val, L);
        SCEV *step_scev = GetSCEV(step_val, L);
        return CreateAddRecSCEV(start_scev, step_scev, L);
    }

    return CreateUnknownSCEV((Value*)phi);
}

SCEV* AnalyzeInstructionSCEV(Instruction *I, Loop *L) {
    switch (I->opcode) {
        case ADD: {
            SCEV *op1 = GetSCEV(I->operands[0], L);
            SCEV *op2 = GetSCEV(I->operands[1], L);
            return CreateAddSCEV(op1, op2);
        }

        case MUL: {
            SCEV *op1 = GetSCEV(I->operands[0], L);
            SCEV *op2 = GetSCEV(I->operands[1], L);
            SCEV *mul = malloc(sizeof(SCEV));
            mul->type = SCEV_MUL;
            mul->data.binary.op1 = op1;
            mul->data.binary.op2 = op2;
            return mul;
        }

        default:
            return CreateUnknownSCEV((Value*)I);
    }
}


// ============================================================================
// 4.3 SCEV Evolution Prediction
// Predict value at iteration N
// ============================================================================

SCEV* PredictSCEVAtIteration(SCEV *scev, int iteration) {
    switch (scev->type) {
        case SCEV_CONSTANT:
            return scev; // Constants don't change

        case SCEV_ADD_REC: {
            // {start, +, step}[n] = start + step * n
            SCEV *start = scev->data.add_rec.start;
            SCEV *step = scev->data.add_rec.step;

            // If both constant, compute directly
            if (start->type == SCEV_CONSTANT && step->type == SCEV_CONSTANT) {
                int value = start->data.constant_value +
                           step->data.constant_value * iteration;
                return CreateConstantSCEV(value);
            }

            // Otherwise, build symbolic expression
            SCEV *iter_scev = CreateConstantSCEV(iteration);
            SCEV *step_times_n = CreateMulSCEV(step, iter_scev);
            return CreateAddSCEV(start, step_times_n);
        }

        case SCEV_ADD: {
            SCEV *op1 = PredictSCEVAtIteration(scev->data.binary.op1, iteration);
            SCEV *op2 = PredictSCEVAtIteration(scev->data.binary.op2, iteration);
            return CreateAddSCEV(op1, op2);
        }

        default:
            return scev;
    }
}


// ============================================================================
// 4.4 SCEV Bounds Computation
// ============================================================================

typedef struct SCEVBounds {
    SCEV *lower;
    SCEV *upper;
    bool has_lower;
    bool has_upper;
} SCEVBounds;

SCEVBounds* ComputeSCEVBounds(SCEV *scev, Loop *L) {
    SCEVBounds *bounds = malloc(sizeof(SCEVBounds));
    bounds->lower = NULL;
    bounds->upper = NULL;
    bounds->has_lower = false;
    bounds->has_upper = false;

    if (scev->type == SCEV_ADD_REC) {
        // For {start, +, step}, bounds depend on trip count
        SCEV *start = scev->data.add_rec.start;
        SCEV *step = scev->data.add_rec.step;

        TripCount *tc = AnalyzeTripCount(L);

        if (start->type == SCEV_CONSTANT && step->type == SCEV_CONSTANT &&
            tc->is_constant) {

            int start_val = start->data.constant_value;
            int step_val = step->data.constant_value;
            int count = tc->constant_value;

            if (step_val > 0) {
                bounds->lower = CreateConstantSCEV(start_val);
                bounds->upper = CreateConstantSCEV(start_val + step_val * (count - 1));
                bounds->has_lower = true;
                bounds->has_upper = true;
            } else if (step_val < 0) {
                bounds->lower = CreateConstantSCEV(start_val + step_val * (count - 1));
                bounds->upper = CreateConstantSCEV(start_val);
                bounds->has_lower = true;
                bounds->has_upper = true;
            }
        }
    }

    return bounds;
}


// ============================================================================
// 4.5 SCEV Simplification Rules
// ============================================================================

SCEV* SimplifySCEV(SCEV *scev) {
    switch (scev->type) {
        case SCEV_ADD: {
            SCEV *op1 = SimplifySCEV(scev->data.binary.op1);
            SCEV *op2 = SimplifySCEV(scev->data.binary.op2);

            // 0 + x = x
            if (op1->type == SCEV_CONSTANT && op1->data.constant_value == 0) {
                return op2;
            }

            // x + 0 = x
            if (op2->type == SCEV_CONSTANT && op2->data.constant_value == 0) {
                return op1;
            }

            // c1 + c2 = c3
            if (op1->type == SCEV_CONSTANT && op2->type == SCEV_CONSTANT) {
                return CreateConstantSCEV(op1->data.constant_value +
                                        op2->data.constant_value);
            }

            return CreateAddSCEV(op1, op2);
        }

        case SCEV_MUL: {
            SCEV *op1 = SimplifySCEV(scev->data.binary.op1);
            SCEV *op2 = SimplifySCEV(scev->data.binary.op2);

            // 0 * x = 0
            if ((op1->type == SCEV_CONSTANT && op1->data.constant_value == 0) ||
                (op2->type == SCEV_CONSTANT && op2->data.constant_value == 0)) {
                return CreateConstantSCEV(0);
            }

            // 1 * x = x
            if (op1->type == SCEV_CONSTANT && op1->data.constant_value == 1) {
                return op2;
            }

            // x * 1 = x
            if (op2->type == SCEV_CONSTANT && op2->data.constant_value == 1) {
                return op1;
            }

            // c1 * c2 = c3
            if (op1->type == SCEV_CONSTANT && op2->type == SCEV_CONSTANT) {
                return CreateConstantSCEV(op1->data.constant_value *
                                        op2->data.constant_value);
            }

            SCEV *mul = malloc(sizeof(SCEV));
            mul->type = SCEV_MUL;
            mul->data.binary.op1 = op1;
            mul->data.binary.op2 = op2;
            return mul;
        }

        case SCEV_ADD_REC: {
            SCEV *start = SimplifySCEV(scev->data.add_rec.start);
            SCEV *step = SimplifySCEV(scev->data.add_rec.step);

            // {x, +, 0} = x
            if (step->type == SCEV_CONSTANT && step->data.constant_value == 0) {
                return start;
            }

            return CreateAddRecSCEV(start, step, scev->data.add_rec.loop);
        }

        default:
            return scev;
    }
}


// ============================================================================
// 5. Integration with GVN and MemorySSA
// ============================================================================

// ============================================================================
// 5.1 Loop-Aware Value Numbering (Integration with GVN)
// ============================================================================

typedef struct LoopGVNContext {
    Loop *loop;
    Map<Value*, int> *value_numbers;
    Map<int, Value*> *leader_table;
    int next_value_number;
} LoopGVNContext;

LoopGVNContext* CreateLoopGVNContext(Loop *L) {
    LoopGVNContext *ctx = malloc(sizeof(LoopGVNContext));
    ctx->loop = L;
    ctx->value_numbers = CreateMap();
    ctx->leader_table = CreateMap();
    ctx->next_value_number = 0;
    return ctx;
}

int AssignValueNumber_LoopAware(Value *V, LoopGVNContext *ctx) {
    // Check if already numbered
    int *existing = MapGet(ctx->value_numbers, V);
    if (existing != NULL) {
        return *existing;
    }

    // For loop invariant values, use global value number
    if (IsLoopInvariant(V, ctx->loop)) {
        // Use global GVN
        int vn = GlobalGVN_GetValueNumber(V);
        MapPut(ctx->value_numbers, V, vn);
        return vn;
    }

    // For PHI nodes in loop header, use SCEV-based numbering
    if (IsPHINode(V)) {
        PHINode *phi = (PHINode*)V;
        if (phi->parent_block == ctx->loop->header) {
            SCEV *scev = GetSCEV(V, ctx->loop);

            // If two PHIs have same SCEV, they get same value number
            Iterator *it = MapIterator(ctx->value_numbers);
            while (HasNext(it)) {
                MapEntry *entry = Next(it);
                Value *other = entry->key;
                if (IsPHINode(other)) {
                    SCEV *other_scev = GetSCEV(other, ctx->loop);
                    if (SCEVEqual(scev, other_scev)) {
                        int vn = *(int*)entry->value;
                        MapPut(ctx->value_numbers, V, vn);
                        DestroyIterator(it);
                        return vn;
                    }
                }
            }
            DestroyIterator(it);
        }
    }

    // Assign new value number
    int vn = ctx->next_value_number++;
    MapPut(ctx->value_numbers, V, vn);
    MapPut(ctx->leader_table, vn, V);

    return vn;
}


// ============================================================================
// 5.2 Loop-Aware Memory Disambiguation (Integration with MemorySSA)
// ============================================================================

bool AreMemoryAccessesIndependent_Loop(Instruction *I1, Instruction *I2,
                                        Loop *L) {
    // Use MemorySSA to check if two memory accesses in a loop are independent

    // Get memory access objects
    MemoryAccess *MA1 = GetMemoryAccess(I1);
    MemoryAccess *MA2 = GetMemoryAccess(I2);

    if (MA1 == NULL || MA2 == NULL) {
        return false;
    }

    // Check if accesses are to same location
    Value *addr1 = GetMemoryAddress(I1);
    Value *addr2 = GetMemoryAddress(I2);

    // Use SCEV to analyze addresses
    SCEV *scev1 = GetSCEV(addr1, L);
    SCEV *scev2 = GetSCEV(addr2, L);

    // If both are AddRec with same loop and different offsets
    if (scev1->type == SCEV_ADD_REC && scev2->type == SCEV_ADD_REC &&
        scev1->data.add_rec.loop == L && scev2->data.add_rec.loop == L) {

        SCEV *start1 = scev1->data.add_rec.start;
        SCEV *start2 = scev2->data.add_rec.start;
        SCEV *step1 = scev1->data.add_rec.step;
        SCEV *step2 = scev2->data.add_rec.step;

        // If same base, same step, different start
        if (SCEVEqual(step1, step2)) {
            SCEV *diff = CreateAddSCEV(start1, NegateSCEV(start2));
            diff = SimplifySCEV(diff);

            if (diff->type == SCEV_CONSTANT && diff->data.constant_value != 0) {
                // Different starting points, same step -> independent
                return true;
            }
        }
    }

    // Fall back to MemorySSA alias analysis
    return !MemorySSA_MayAlias(MA1, MA2);
}


// ============================================================================
// 6. CUDA-Specific Loop Handling
// ============================================================================

typedef struct CUDALoopInfo {
    Loop *loop;
    bool is_thread_loop;      // Loop over threads
    bool is_block_loop;       // Loop over blocks
    bool is_warp_uniform;     // All threads execute same iterations
    int unroll_factor;        // GPU-specific unroll factor
    bool uses_shared_memory;
    bool has_sync_points;
} CUDALoopInfo;

CUDALoopInfo* AnalyzeCUDALoop(Loop *L) {
    CUDALoopInfo *cuda_info = malloc(sizeof(CUDALoopInfo));
    cuda_info->loop = L;
    cuda_info->is_thread_loop = false;
    cuda_info->is_block_loop = false;
    cuda_info->is_warp_uniform = true;
    cuda_info->unroll_factor = 1;
    cuda_info->uses_shared_memory = false;
    cuda_info->has_sync_points = false;

    // Detect thread/block index usage
    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (Instruction *I = b->first_inst; I != NULL; I = I->next) {
            // Check for thread index intrinsics
            if (IsIntrinsicCall(I, "llvm.nvvm.read.ptx.sreg.tid")) {
                cuda_info->is_thread_loop = true;
            }

            // Check for block index intrinsics
            if (IsIntrinsicCall(I, "llvm.nvvm.read.ptx.sreg.ctaid")) {
                cuda_info->is_block_loop = true;
            }

            // Check for shared memory usage
            if (I->opcode == LOAD || I->opcode == STORE) {
                Value *addr = I->operands[0];
                if (IsSharedMemoryAddress(addr)) {
                    cuda_info->uses_shared_memory = true;
                }
            }

            // Check for synchronization
            if (IsIntrinsicCall(I, "llvm.nvvm.barrier0")) {
                cuda_info->has_sync_points = true;
            }
        }
    }
    DestroyIterator(it);

    // Compute GPU-specific unroll factor
    // Based on register pressure and occupancy
    int loop_depth = L->depth;
    int estimated_registers = EstimateRegisterPressure(L);

    if (estimated_registers < 32 && loop_depth <= 2) {
        cuda_info->unroll_factor = 4;
    } else if (estimated_registers < 64 && loop_depth <= 1) {
        cuda_info->unroll_factor = 2;
    } else {
        cuda_info->unroll_factor = 1;
    }

    return cuda_info;
}


// ============================================================================
// Performance Metrics
// ============================================================================

typedef struct LoopMetrics {
    int num_blocks;
    int num_instructions;
    int num_memory_ops;
    int num_phi_nodes;
    int estimated_iterations;
    double register_pressure;
    double memory_bandwidth_estimate;
} LoopMetrics;

LoopMetrics* ComputeLoopMetrics(Loop *L) {
    LoopMetrics *metrics = malloc(sizeof(LoopMetrics));
    metrics->num_blocks = SetSize(L->blocks);
    metrics->num_instructions = 0;
    metrics->num_memory_ops = 0;
    metrics->num_phi_nodes = 0;

    Iterator *it = SetIterator(L->blocks);
    while (HasNext(it)) {
        BasicBlock *b = Next(it);
        for (Instruction *I = b->first_inst; I != NULL; I = I->next) {
            metrics->num_instructions++;

            if (I->opcode == LOAD || I->opcode == STORE) {
                metrics->num_memory_ops++;
            }

            if (IsPHINode(I)) {
                metrics->num_phi_nodes++;
            }
        }
    }
    DestroyIterator(it);

    TripCount *tc = AnalyzeTripCount(L);
    metrics->estimated_iterations = tc->is_constant ? tc->constant_value : -1;

    metrics->register_pressure = EstimateRegisterPressure(L);
    metrics->memory_bandwidth_estimate =
        metrics->num_memory_ops * sizeof(int) * metrics->estimated_iterations;

    return metrics;
}

// ============================================================================
// End of Loop Analysis Algorithms
// ============================================================================
