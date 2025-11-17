# Loop Detection

## Overview

Loop detection in CICC identifies natural loops in control flow graphs (CFGs) using dominance-based analysis. A natural loop L is characterized by: (1) a header block h with multiple incoming edges, one of which is a back edge; (2) all blocks in L can reach the back edge source x without traversing h; (3) h dominates all blocks in L.

**Complexity Analysis**: O(α(V) × (V + E))
- V: number of basic blocks (vertices)
- E: number of CFG edges
- α: inverse Ackermann function ≈ 4 for all practical inputs
- Bottleneck: Lengauer-Tarjan dominator tree construction

---

## Algorithm: Dominator-Based Natural Loop Detection

### Phase 1: Dominator Tree Construction

**Algorithm**: Lengauer-Tarjan dominance algorithm

**Time Complexity**: O(α(n) × (n + m)) where n = blocks, m = edges

**Space Complexity**: O(n) for idom pointers and auxiliary structures

```c
// Lengauer-Tarjan algorithm skeleton
void computeDominators(CFG* cfg) {
    BasicBlock** postOrder = new BasicBlock*[cfg->numBlocks];
    int* postOrderNum = new int[cfg->numBlocks];
    int postOrderCount = 0;

    // Phase 1: DFS post-order numbering
    // Traverse CFG in reverse: count = reverse post-order index
    dfsPostOrder(cfg->entry, postOrder, postOrderNum, &postOrderCount);

    // Phase 2: Initialize structures
    BasicBlock** idom = new BasicBlock*[cfg->numBlocks];
    BasicBlock** semi = new BasicBlock*[cfg->numBlocks];
    BasicBlock** parent = new BasicBlock*[cfg->numBlocks];
    int* semiNum = new int[cfg->numBlocks];

    // Initialize: idom[entry] = entry, others = NULL
    idom[cfg->entry->id] = cfg->entry;
    for (int i = 0; i < cfg->numBlocks; i++) {
        semi[i] = NULL;
        parent[i] = NULL;
    }

    // Phase 3: First pass - compute semidominator
    for (int i = postOrderCount - 2; i >= 0; i--) {
        BasicBlock* w = postOrder[i];

        // semi[w] = minimum of:
        // - pred(w) where pred is processed (postOrderNum[pred] < postOrderNum[w])
        // - semi[v] where v is ancestor in auxiliary tree with postOrderNum[v] < postOrderNum[w]

        BasicBlock* minSemi = w;

        for (int j = 0; j < w->numPredecessors; j++) {
            BasicBlock* pred = w->predecessors[j];
            BasicBlock* candidate = NULL;

            if (postOrderNum[pred->id] < postOrderNum[w->id]) {
                candidate = pred;
            } else {
                // Find semidominator ancestor
                candidate = findAncestorWithLowestSemi(pred, postOrderNum[w->id]);
            }

            if (postOrderNum[candidate->id] < postOrderNum[minSemi->id]) {
                minSemi = candidate;
            }
        }

        semi[w->id] = minSemi;
        semiNum[w->id] = postOrderNum[minSemi->id];
    }

    // Phase 4: Second pass - compute immediate dominators
    for (int i = 0; i < postOrderCount - 1; i++) {
        BasicBlock* w = postOrder[i];
        BasicBlock* u = NULL;
        int minPostOrder = INT_MAX;

        // Find ancestor of w with minimum semidominator post-order
        // If semidom[u] == semi[w], then idom[w] = u, else idom[w] = idom[u]

        if (i > 0) {
            // Search predecessors for candidates
            for (int j = 0; j < w->numPredecessors; j++) {
                BasicBlock* pred = w->predecessors[j];
                BasicBlock* candidate = findAncestorWithLowestSemi(pred, semiNum[w->id]);

                if (candidate != NULL && postOrderNum[semi[candidate->id]->id] < minPostOrder) {
                    minPostOrder = postOrderNum[semi[candidate->id]->id];
                    u = candidate;
                }
            }

            if (u != NULL && semi[u->id] == semi[w->id]) {
                idom[w->id] = semi[w->id];
            } else {
                idom[w->id] = idom[u->id];
            }
        }
    }

    // Phase 5: Store results in BasicBlock structure
    for (int i = 0; i < cfg->numBlocks; i++) {
        cfg->blocks[i]->idom = idom[cfg->blocks[i]->id];
    }
}

void dfsPostOrder(BasicBlock* block, BasicBlock** postOrder,
                  int* postOrderNum, int* count) {
    block->visited = 1;

    for (int i = 0; i < block->numSuccessors; i++) {
        BasicBlock* succ = block->successors[i];
        if (!succ->visited) {
            dfsPostOrder(succ, postOrder, postOrderNum, count);
        }
    }

    postOrder[*count] = block;
    postOrderNum[block->id] = *count;
    (*count)++;
}
```

---

## Back Edge Identification

### Definition and Classification

**Back Edge**: An edge (u, v) in CFG where v dominates u. Mathematically: `dominates(v, u) == true`

Back edges are fundamental markers of loop existence. A back edge identifies its target (v) as a loop header.

### DFS-Based Edge Classification Algorithm

```c
struct EdgeClassification {
    enum Type { TREE_EDGE = 0, BACK_EDGE = 1, FORWARD_EDGE = 2, CROSS_EDGE = 3 };
    Type type;
    int sourceDiscoveryTime;
    int targetDiscoveryTime;
    int sourceFinishTime;
    int targetFinishTime;
};

// Global DFS state
int dfsTime = 0;
int* discoveryTime;  // discovery[block->id]
int* finishTime;     // finish[block->id]
enum State { WHITE = 0, GRAY = 1, BLACK = 2 };
State* blockState;

void identifyBackEdges(CFG* cfg, DominatorTree* domTree) {
    // Initialize
    discoveryTime = new int[cfg->numBlocks];
    finishTime = new int[cfg->numBlocks];
    blockState = new State[cfg->numBlocks];
    dfsTime = 0;

    for (int i = 0; i < cfg->numBlocks; i++) {
        blockState[i] = WHITE;
        discoveryTime[i] = -1;
        finishTime[i] = -1;
    }

    // Phase 1: DFS traversal for time stamps
    dfsTraversal(cfg->entry);

    // Phase 2: Edge classification and back edge verification
    for (int i = 0; i < cfg->numBlocks; i++) {
        BasicBlock* source = cfg->blocks[i];

        for (int j = 0; j < source->numSuccessors; j++) {
            BasicBlock* target = source->successors[j];

            EdgeClassification edge = {0};
            edge.sourceDiscoveryTime = discoveryTime[source->id];
            edge.targetDiscoveryTime = discoveryTime[target->id];
            edge.sourceFinishTime = finishTime[source->id];
            edge.targetFinishTime = finishTime[target->id];

            // Classification rules:
            if (discoveryTime[target->id] == discoveryTime[source->id] + 1 &&
                blockState[target->id] == GRAY) {
                // Target is direct successor and still being processed
                edge.type = TREE_EDGE;
            } else if (discoveryTime[target->id] < discoveryTime[source->id] &&
                       finishTime[target->id] > finishTime[source->id]) {
                // Target ancestor in DFS tree: target discovered before, finishes after
                edge.type = BACK_EDGE;
            } else if (discoveryTime[target->id] > discoveryTime[source->id] &&
                       finishTime[target->id] < finishTime[source->id]) {
                // Target descendant but not tree edge: forward edge
                edge.type = FORWARD_EDGE;
            } else if (discoveryTime[target->id] > finishTime[source->id]) {
                // No ancestor-descendant relationship: cross edge
                edge.type = CROSS_EDGE;
            } else {
                // Unclassified (shouldn't occur)
                edge.type = CROSS_EDGE;
            }

            // Phase 3: Dominator verification for potential back edges
            if (edge.type == BACK_EDGE) {
                // Verify: target must dominate source for natural loop
                if (dominates(domTree, target, source)) {
                    // Confirmed back edge: target is loop header
                    addBackEdge(cfg, source, target);
                } else {
                    // Dominance check failed: not a natural loop
                    // Mark as irreducible control flow
                    markIrreducible(cfg, source, target);
                }
            }
        }
    }
}

void dfsTraversal(BasicBlock* block) {
    blockState[block->id] = GRAY;
    discoveryTime[block->id] = dfsTime++;

    for (int i = 0; i < block->numSuccessors; i++) {
        BasicBlock* succ = block->successors[i];
        if (blockState[succ->id] == WHITE) {
            dfsTraversal(succ);
        }
    }

    blockState[block->id] = BLACK;
    finishTime[block->id] = dfsTime++;
}

int dominates(DominatorTree* tree, BasicBlock* dominator, BasicBlock* block) {
    BasicBlock* current = block;

    while (current != NULL) {
        if (current == dominator) {
            return 1;
        }
        current = current->idom;
    }

    return 0;
}
```

---

## Loop Construction from Back Edges

### Algorithm: Natural Loop Computation

**Input**: Back edge (x → h) where h = loop header, x = latch block

**Output**: L = set of all basic blocks in natural loop

```c
struct Loop {
    BasicBlock* header;
    vector<BasicBlock*> blocks;
    vector<BasicBlock*> exitBlocks;
    struct Loop* parentLoop;
    vector<struct Loop*> subLoops;
    int nestingDepth;
};

Loop* constructLoop(BasicBlock* header, BasicBlock* latchBlock) {
    Loop* loop = malloc(sizeof(Loop));
    loop->header = header;
    loop->parentLoop = NULL;

    // Initialize vectors
    initVector(&loop->blocks, 64);
    initVector(&loop->exitBlocks, 16);
    initVector(&loop->subLoops, 4);

    // Step 1: Initialize loop set L = {h, x}
    addBlockToLoop(loop, header);
    addBlockToLoop(loop, latchBlock);

    // Step 2: Initialize work queue W with predecessors of x
    Queue* workQueue = createQueue(256);

    for (int i = 0; i < latchBlock->numPredecessors; i++) {
        BasicBlock* pred = latchBlock->predecessors[i];
        if (pred != header && !isInLoop(loop, pred)) {
            enqueue(workQueue, pred);
        }
    }

    // Step 3: Backward traversal - process work queue
    while (!isQueueEmpty(workQueue)) {
        BasicBlock* m = dequeue(workQueue);

        // Condition: if m != h and m not in L
        if (m != header && !isInLoop(loop, m)) {
            // Add m to loop
            addBlockToLoop(loop, m);

            // Add all predecessors of m to work queue
            for (int i = 0; i < m->numPredecessors; i++) {
                BasicBlock* pred = m->predecessors[i];
                if (!isInLoop(loop, pred)) {
                    enqueue(workQueue, pred);
                }
            }
        }
    }

    // Step 4: Identify exit blocks (blocks with successors outside loop)
    for (int i = 0; i < loop->blocks.size; i++) {
        BasicBlock* block = loop->blocks.elements[i];

        for (int j = 0; j < block->numSuccessors; j++) {
            BasicBlock* succ = block->successors[j];
            if (!isInLoop(loop, succ)) {
                // succ is outside loop: block is exit block
                if (!isExitBlock(loop, block)) {
                    addExitBlock(loop, block);
                }
                break;
            }
        }
    }

    freeQueue(workQueue);
    return loop;
}

void addBlockToLoop(Loop* loop, BasicBlock* block) {
    if (!isInLoop(loop, block)) {
        vectorPush(&loop->blocks, block);
        block->loop = loop;
    }
}

int isInLoop(Loop* loop, BasicBlock* block) {
    for (int i = 0; i < loop->blocks.size; i++) {
        if (loop->blocks.elements[i] == block) {
            return 1;
        }
    }
    return 0;
}

void addExitBlock(Loop* loop, BasicBlock* block) {
    if (!isExitBlock(loop, block)) {
        vectorPush(&loop->exitBlocks, block);
    }
}

int isExitBlock(Loop* loop, BasicBlock* block) {
    for (int i = 0; i < loop->exitBlocks.size; i++) {
        if (loop->exitBlocks.elements[i] == block) {
            return 1;
        }
    }
    return 0;
}
```

---

## Loop Header Identification

### Properties and Definition

A loop header h must satisfy:

1. **Dominance**: h dominates all blocks in the loop L
2. **Back Edge Target**: All back edges targeting the loop have h as target
3. **Single Entry**: h is the unique entry point to L (natural loop property)
4. **External Edges**: Only h receives edges from outside L (guaranteed by natural loop definition)

### Identification Method

The target block of a back edge (x → h) is automatically identified as loop header. No additional computation required beyond back edge identification.

### Verification in C

```c
int isValidLoopHeader(Loop* loop, BasicBlock* header) {
    // Verify header dominates all blocks in loop
    for (int i = 0; i < loop->blocks.size; i++) {
        BasicBlock* block = loop->blocks.elements[i];
        if (block != header) {
            if (!dominates(header, block)) {
                return 0;  // Invalid: header doesn't dominate all blocks
            }
        }
    }

    // Verify single entry: count external edges to header
    int externalEntries = 0;
    for (int i = 0; i < header->numPredecessors; i++) {
        BasicBlock* pred = header->predecessors[i];
        if (!isInLoop(loop, pred)) {
            externalEntries++;
        }
    }

    if (externalEntries == 1) {
        return 1;  // Valid: single external entry
    } else if (externalEntries == 0) {
        // Self-loop: no external entry required
        return 1;
    } else {
        return 0;  // Invalid: multiple external entries (should be handled by LoopSimplify)
    }
}
```

---

## Nesting Depth Calculation

### Formula and Definition

**Base Case**: Outermost loop has nesting depth = 1

**Recursive Case**: depth(nested_loop) = depth(parent_loop) + 1

**Block Depth**: depth(block) = depth(innermost_containing_loop) or 0 if block not in loop

### Loop Tree Construction and Depth Assignment

```c
struct LoopTree {
    Loop** rootLoops;        // Top-level loops not contained in others
    int numRootLoops;
    Loop** allLoops;         // All loops in function
    int numLoops;
    int maxDepth;
};

LoopTree* buildLoopTree(vector<Loop*>* backEdges, CFG* cfg) {
    LoopTree* tree = malloc(sizeof(LoopTree));
    tree->allLoops = malloc(sizeof(Loop*) * backEdges->size);
    tree->numLoops = 0;
    tree->maxDepth = 0;

    // Step 1: Construct all loops from back edges
    for (int i = 0; i < backEdges->size; i++) {
        BackEdge* edge = backEdges->elements[i];
        Loop* loop = constructLoop(edge->header, edge->latchBlock);
        tree->allLoops[tree->numLoops++] = loop;
    }

    // Step 2: Build containment relationships
    // Loop A contains Loop B if all blocks of B are in A
    for (int i = 0; i < tree->numLoops; i++) {
        for (int j = 0; j < tree->numLoops; j++) {
            if (i != j) {
                Loop* outer = tree->allLoops[i];
                Loop* inner = tree->allLoops[j];

                int innerFullyContained = 1;
                for (int k = 0; k < inner->blocks.size; k++) {
                    if (!isInLoop(outer, inner->blocks.elements[k])) {
                        innerFullyContained = 0;
                        break;
                    }
                }

                if (innerFullyContained) {
                    // Check if immediate parent (no loop between outer and inner)
                    int isImmediateParent = 1;
                    for (int k = 0; k < tree->numLoops; k++) {
                        if (k != i && k != j) {
                            Loop* candidate = tree->allLoops[k];

                            // Check if candidate contains inner and is contained in outer
                            int candidateContainsInner = 1;
                            for (int m = 0; m < inner->blocks.size; m++) {
                                if (!isInLoop(candidate, inner->blocks.elements[m])) {
                                    candidateContainsInner = 0;
                                    break;
                                }
                            }

                            int outerContainsCandidate = 1;
                            for (int m = 0; m < candidate->blocks.size; m++) {
                                if (!isInLoop(outer, candidate->blocks.elements[m])) {
                                    outerContainsCandidate = 0;
                                    break;
                                }
                            }

                            if (candidateContainsInner && outerContainsCandidate) {
                                isImmediateParent = 0;
                                break;
                            }
                        }
                    }

                    if (isImmediateParent && inner->parentLoop == NULL) {
                        inner->parentLoop = outer;
                        vectorPush(&outer->subLoops, inner);
                    }
                }
            }
        }
    }

    // Step 3: Identify root loops (no parent)
    tree->rootLoops = malloc(sizeof(Loop*) * tree->numLoops);
    tree->numRootLoops = 0;

    for (int i = 0; i < tree->numLoops; i++) {
        if (tree->allLoops[i]->parentLoop == NULL) {
            tree->rootLoops[tree->numRootLoops++] = tree->allLoops[i];
        }
    }

    // Step 4: Assign depths using post-order traversal
    for (int i = 0; i < tree->numRootLoops; i++) {
        assignDepth(tree->rootLoops[i], 1, &tree->maxDepth);
    }

    // Step 5: Assign block depths
    for (int i = 0; i < cfg->numBlocks; i++) {
        BasicBlock* block = cfg->blocks[i];

        // Find innermost loop containing block
        Loop* innermostLoop = NULL;
        for (int j = 0; j < tree->numLoops; j++) {
            if (isInLoop(tree->allLoops[j], block)) {
                if (innermostLoop == NULL ||
                    tree->allLoops[j]->nestingDepth > innermostLoop->nestingDepth) {
                    innermostLoop = tree->allLoops[j];
                }
            }
        }

        if (innermostLoop != NULL) {
            block->nestingDepth = innermostLoop->nestingDepth;
        } else {
            block->nestingDepth = 0;  // Not in any loop
        }
    }

    return tree;
}

void assignDepth(Loop* loop, int parentDepth, int* maxDepth) {
    // Recursive DFS: depth(loop) = parentDepth
    loop->nestingDepth = parentDepth;

    if (parentDepth > *maxDepth) {
        *maxDepth = parentDepth;
    }

    // Process sub-loops: depth = loop->depth + 1
    for (int i = 0; i < loop->subLoops.size; i++) {
        Loop* subLoop = loop->subLoops.elements[i];
        assignDepth(subLoop, parentDepth + 1, maxDepth);
    }
}

int getLoopDepth(Loop* loop) {
    // Returns nesting depth: outermost = 1
    return loop->nestingDepth;
}

int getBlockDepth(BasicBlock* block) {
    // Returns nesting depth of innermost containing loop
    return block->nestingDepth;
}
```

---

## Dominator Tree Data Structure

### Representation

```c
struct BasicBlock {
    int id;

    // Dominance information
    struct BasicBlock* idom;              // Immediate dominator
    vector<struct BasicBlock*> idomChildren;  // Blocks this block immediately dominates

    // CFG structure
    vector<struct BasicBlock*> successors;
    vector<struct BasicBlock*> predecessors;
    int numSuccessors;
    int numPredecessors;

    // Instruction and analysis data
    Instruction** instructions;
    int numInstructions;

    // Loop information
    struct Loop* loop;                    // Innermost containing loop
    int nestingDepth;                     // Nesting depth in loop tree

    // Additional fields (PHI nodes, etc.)
};

struct DominatorTree {
    BasicBlock** blocks;
    int numBlocks;
    BasicBlock* entry;

    // Reverse dominance queries
    int (*dominates)(struct DominatorTree*, BasicBlock*, BasicBlock*);
    int (*strictlyDominates)(struct DominatorTree*, BasicBlock*, BasicBlock*);
    BasicBlock* (*getImmediateDominator)(struct DominatorTree*, BasicBlock*);
    vector<BasicBlock*> (*getDominatedBlocks)(struct DominatorTree*, BasicBlock*);
};
```

### Query Operations

```c
int dominates(DominatorTree* tree, BasicBlock* dominator, BasicBlock* block) {
    // Check if dominator dominates block
    BasicBlock* current = block;

    while (current != NULL) {
        if (current == dominator) {
            return 1;  // dominator found in chain to entry
        }
        current = current->idom;
    }

    return 0;  // dominator not in dominance chain
}

int strictlyDominates(DominatorTree* tree, BasicBlock* dominator, BasicBlock* block) {
    // strictDom(d, b) = dominates(d, b) AND d != b
    return dominator != block && dominates(tree, dominator, block);
}

BasicBlock* getImmediateDominator(DominatorTree* tree, BasicBlock* block) {
    return block->idom;
}

void getDominatedBlocks(DominatorTree* tree, BasicBlock* block,
                        vector<BasicBlock*>* result) {
    // Collect all blocks immediately dominated by this block
    clearVector(result);

    for (int i = 0; i < block->idomChildren.size; i++) {
        vectorPush(result, block->idomChildren.elements[i]);
    }
}
```

---

## LoopInfo Analysis Data Structure

### Complete Structure Definition

```c
struct LoopInfo {
    // Tree of loops
    vector<Loop*> topLevelLoops;  // Root loops (nesting depth 1)
    vector<Loop*> allLoops;       // All loops in function

    // Query structures for fast lookup
    Loop** blockToLoop;           // blockToLoop[block_id] = innermost loop containing block
    int blockToLoopSize;

    // Maximum nesting depth encountered
    int maxNestingDepth;

    // Function context
    CFG* cfg;
    DominatorTree* domTree;
};

struct Loop {
    // Loop identity and structure
    BasicBlock* header;
    struct Loop* parentLoop;
    vector<struct Loop*> subLoops;  // Immediately nested loops

    // Loop membership
    vector<BasicBlock*> blocks;
    vector<BasicBlock*> exitBlocks;

    // Loop properties
    int nestingDepth;              // Depth = parent_depth + 1

    // LoopSimplify normalized form (if applied)
    BasicBlock* preheader;         // Guaranteed single entry block
    BasicBlock* latchBlock;        // Block containing back edge to header

    // Analysis annotations
    int isSingleEntryLoop;         // 1 if header has single external predecessor
    int isSingleExitLoop;          // 1 if single block exits to outside
    int isCountableLoop;           // 1 if iteration count computable

    // Loop bounds (if known)
    enum BoundType { UNBOUNDED = 0, FINITE = 1 } boundType;
    int64_t tripCount;             // For FINITE loops with constant trip count
};

struct LoopTreeNode {
    Loop* loop;
    vector<struct LoopTreeNode*> children;
    struct LoopTreeNode* parent;
};
```

### Query Interface

```c
// Get innermost loop containing block
Loop* getLoopFor(LoopInfo* info, BasicBlock* block) {
    if (block->id < info->blockToLoopSize) {
        return info->blockToLoop[block->id];
    }
    return NULL;
}

// Get nesting depth of block (0 if not in loop)
int getLoopDepthOf(LoopInfo* info, BasicBlock* block) {
    Loop* loop = getLoopFor(info, block);
    if (loop != NULL) {
        return loop->nestingDepth;
    }
    return 0;
}

// Check if block is in loop
int isBlockInLoop(Loop* loop, BasicBlock* block) {
    for (int i = 0; i < loop->blocks.size; i++) {
        if (loop->blocks.elements[i] == block) {
            return 1;
        }
    }
    return 0;
}

// Check containment: does loop1 contain loop2?
int loopContains(Loop* loop1, Loop* loop2) {
    if (loop1 == loop2) return 0;

    Loop* parent = loop2->parentLoop;
    while (parent != NULL) {
        if (parent == loop1) return 1;
        parent = parent->parentLoop;
    }

    return 0;
}

// Get immediate child loops
vector<Loop*>* getImmediateSubLoops(Loop* loop) {
    return &loop->subLoops;
}

// Get all blocks in loop
vector<BasicBlock*>* getLoopBlocks(Loop* loop) {
    return &loop->blocks;
}

// Get exit blocks
vector<BasicBlock*>* getExitBlocks(Loop* loop) {
    return &loop->exitBlocks;
}

// Get back edges (edges from latchBlock to header)
void getBackEdges(Loop* loop, vector<Edge*>* result) {
    clearVector(result);

    // Iterate through latchBlock successors
    if (loop->latchBlock != NULL) {
        for (int i = 0; i < loop->latchBlock->numSuccessors; i++) {
            if (loop->latchBlock->successors[i] == loop->header) {
                Edge* edge = malloc(sizeof(Edge));
                edge->source = loop->latchBlock;
                edge->target = loop->header;
                vectorPush(result, edge);
            }
        }
    }
}
```

---

## Integration with Loop Optimization Passes

CICC implements 7 primary loop optimization passes integrated with loop detection:

### Pass 1: Loop Invariant Code Motion (LICM)

**Dependency**: LoopInfo (loop structure and nesting depth)

**Operation**: Identifies values invariant within loop body and hoists computation to preheader

**Complexity**: O(V + E) per loop

```c
void runLICM(Loop* loop, LoopInfo* loopInfo, DominatorTree* domTree) {
    // For each instruction in loop
    for (int i = 0; i < loop->blocks.size; i++) {
        BasicBlock* block = loop->blocks.elements[i];

        for (int j = 0; j < block->numInstructions; j++) {
            Instruction* instr = block->instructions[j];

            // Check invariance: all operands defined outside loop
            int isInvariant = 1;
            for (int k = 0; k < instr->numOperands; k++) {
                Value* operand = instr->operands[k];

                // If operand defined in loop, not invariant
                if (operand->definingBlock != NULL &&
                    isBlockInLoop(loop, operand->definingBlock)) {
                    isInvariant = 0;
                    break;
                }
            }

            // If invariant and not dependent on loop, hoist to preheader
            if (isInvariant && loop->preheader != NULL) {
                moveInstructionToBlock(instr, loop->preheader);
            }
        }
    }
}
```

### Pass 2: Loop Unroll

**Dependency**: LoopInfo (nesting depth for factor selection, trip count analysis)

**Operation**: Replicates loop body N times to reduce branch overhead

**Unroll Factor**: Decreases with nesting depth: `factor = baseUnrollFactor / (2 ^ (depth - 1))`

```c
void runLoopUnroll(Loop* loop, LoopInfo* loopInfo) {
    // Determine unroll factor based on nesting depth
    int baseUnrollFactor = 4;  // Default: 4× unrolling
    int adjustedFactor = baseUnrollFactor >> (loop->nestingDepth - 1);

    // Limit minimum factor
    if (adjustedFactor < 1) {
        adjustedFactor = 1;  // No unrolling if too nested
    }

    // Unroll: replicate loop body adjustedFactor times
    for (int unrollCount = 1; unrollCount < adjustedFactor; unrollCount++) {
        // Clone all blocks in loop body with unique instruction names
        // Update induction variable increments
    }
}
```

### Pass 3: Loop Vectorize

**Dependency**: LoopInfo (loop structure, data dependence analysis within loops)

**Operation**: Vectorizes innermost loops with SIMD/Tensor Core operations

**Application**: Particularly relevant for CUDA target (tensor cores)

### Pass 4: Loop Rotate

**Dependency**: LoopInfo (preheader and latch structure from LoopSimplify)

**Operation**: Transforms while-loop to do-while structure for better branch prediction

### Pass 5: Loop Fusion

**Dependency**: LoopInfo (containment relationships and adjacency)

**Operation**: Combines consecutive loops with compatible structure to improve cache locality

**Requirement**: Loops must be adjacent, non-nested, same depth, compatible iteration bounds

### Pass 6: Loop Interchange

**Dependency**: LoopInfo (nesting relationships, data dependence within nested loops)

**Operation**: Reorders nested loop iteration to improve data locality

**Example**: Exchange outer and inner loop to make inner loop vectorizable

### Pass 7: Loop Deletion

**Dependency**: LoopInfo (loop body analysis, definition-use chains)

**Operation**: Removes loops proven dead (no side effects, unused results)

**Condition**: Loop has no external side effects, results unused outside loop

---

## LoopSimplify Prerequisite Pass

Many optimization passes require loops in canonical (simplified) form:

```c
struct LoopSimplifyRequirements {
    // Single preheader: unique block with edge to header, executed once per loop iteration
    // Ensures: loop->preheader != NULL
    BasicBlock* preheader;

    // Single latch: unique block containing back edge to header
    // Ensures: loop->latchBlock != NULL
    BasicBlock* latchBlock;

    // All back edges from latch to header only
    // Invariant: all successors of latch that are in loop target header

    // All loop entries from preheader only
    // Invariant: only external edge to header is from preheader
};

void runLoopSimplify(Loop* loop) {
    // Insert preheader if needed
    if (!hasPreheader(loop)) {
        loop->preheader = insertPreheader(loop);
    }

    // Merge multiple back edge sources into single latch
    if (countBackEdgeSources(loop) > 1) {
        loop->latchBlock = mergeBackEdgeSources(loop);
    }

    // Normalize exit block structure
    normalizeExitBlocks(loop);
}
```

---

## Complexity Analysis Summary

| Operation | Time Complexity | Space Complexity | Notes |
|-----------|-----------------|------------------|-------|
| Dominator tree construction (Lengauer-Tarjan) | O(α(V) × (V + E)) | O(V) | α ≈ 4 for all practical inputs |
| Back edge identification (DFS classification) | O(V + E) | O(V) | Post-order numbering and edge traversal |
| Natural loop construction (backward CFG traversal) | O(V + E) per back edge | O(V) | Work queue processes each block ≤ 1× |
| Nesting depth calculation | O(L) where L = number of loops | O(L) | Post-order tree traversal |
| Loop membership queries (block in loop) | O(1) with hash table, O(V) without | O(V) with table | blockToLoop hash table recommended |
| Loop containment queries (loop A ⊆ loop B) | O(depth) | O(1) | Traversal of parentLoop chain |
| Full LoopInfo computation | O(α(V) × (V + E)) | O(V + L) | Dominated by dominator tree construction |

**Overall Complexity for Complete Loop Analysis**: O(α(V) × (V + E)) where:
- V = number of basic blocks
- E = number of CFG edges
- α = inverse Ackermann function ≈ 4

---

## Key Properties

1. **Natural Loop Invariants**:
   - Single entry point (loop header)
   - All blocks reachable from header through loop edges
   - All blocks can reach back edge source without passing through header

2. **Dominator Relationship**:
   - Header dominates all loop blocks
   - Each block dominates all its successors within loop

3. **Nesting Formula**:
   - depth(outermost) = 1
   - depth(nested) = depth(parent) + 1
   - Calculated recursively: O(L) for L loops

4. **Back Edge Property**:
   - Every natural loop has ≥ 1 back edge
   - Multiple back edges indicate multiple entries (before LoopSimplify)
   - Back edge target always equals loop header

---

## CUDA-Specific Applications

Loop detection integrates with GPU code generation:

- **Thread Block Mapping**: Outermost loops map to thread blocks; innermost to thread iterations
- **Warp Divergence**: Loop nesting depth affects branch prediction; deep nesting increases divergence
- **Register Pressure**: Nesting depth scales register usage; critical for occupancy calculation
- **Shared Memory**: Loop structure determines synchronization point placement
- **Tensor Core Vectorization**: Innermost loops targeted for tensor core operations
- **Global Memory Coalescing**: Loop structure guides memory access pattern analysis

---

## References

1. Lengauer, T., & Tarjan, R. E. (1979). "A fast algorithm for finding dominators in a flowgraph." ACM Transactions on Programming Languages and Systems.

2. Muchnick, S. S. (1997). "Advanced Compiler Design and Implementation." Morgan Kaufmann.

3. Aho, A. V., Lam, M. S., Sethi, R., & Ullman, J. D. (2006). "Compilers: Principles, Techniques, and Tools." Pearson.

4. LLVM Loop Terminology Documentation: https://llvm.org/docs/LoopTerminology/

5. LLVM LoopInfo Analysis: https://llvm.org/doxygen/classllvm_1_1LoopInfo.html
