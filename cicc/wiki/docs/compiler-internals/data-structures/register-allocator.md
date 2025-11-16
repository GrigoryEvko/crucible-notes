# Register Allocator Data Structures

## Interference Graph Node

```c
// Size: 40 bytes (0x28), SSE register layout
struct IGNode {
    void*       node_data;        // +0x00: Pointer to register metadata
    uint64_t    degree;           // +0x08: Neighbor count (m128i_u64[1])
    uint32_t    vreg_id;          // +0x10: Virtual register ID
    float       spill_cost;       // +0x14: Computed spill cost
    uint8_t     reg_class;        // +0x18: 0=GPR32, 1=GPR64, 2=PRED, 3=H16
    uint8_t     flags;            // +0x19: 0x01=precolored, 0x02=spilled
    uint16_t    color;            // +0x1A: Assigned physical register (0-14)
    uint32_t    neighbor_count;   // +0x1C: Edge list size
    IGNode**    neighbors;        // +0x20: Adjacency list pointer
};
```

**Storage**: Nodes stored in `__m128i` arrays for SIMD processing (decompiled evidence: `v62->m128i_u64[1]` at 0x1090bd0:1039).

**Allocation**: Bump allocator, 40-byte aligned blocks.

## Spill Cost Computation

### Formula

```c
spill_cost = (def_freq * use_freq * mem_latency * loop_depth_mult)
```

### Coefficients

```c
#define MEM_LATENCY_L1      4.0f     // cycles
#define MEM_LATENCY_L2     10.0f     // cycles
#define MEM_LATENCY_L3     40.0f     // cycles
#define MEM_LATENCY_MAIN  100.0f     // cycles
#define LOOP_DEPTH_BASE     1.5f     // exponential base
```

### Computation

```c
float compute_spill_cost(uint32_t defs, uint32_t uses, uint32_t loop_depth) {
    float def_freq = (float)defs;
    float use_freq = (float)uses;
    float mem_lat  = estimate_memory_latency();  // 4.0-100.0
    float loop_mul = powf(LOOP_DEPTH_BASE, (float)loop_depth);
    return def_freq * use_freq * mem_lat * loop_mul;
}
```

**Address**: 0xb612d0 (sub_B612D0, 39329 bytes)

## Graph Coloring Priority

### Constants

```c
#define K_REGISTERS        15        // Physical register count
#define K_THRESHOLD        14        // K-1 for Briggs criterion
#define COALESCE_FACTOR    0.8f      // 0xCCCCCCCCCCCCCCCD / 2^64
```

### Briggs Criterion

```c
bool is_briggs_safe(IGNode* node) {
    uint32_t low_degree_count = 0;
    for (uint32_t i = 0; i < node->neighbor_count; i++) {
        if (node->neighbors[i]->degree < K_REGISTERS) {
            low_degree_count++;
        }
    }
    return low_degree_count >= K_REGISTERS;
}
```

**Evidence**: `v64 > 0xE` check at 0x1090bd0:1039, 1060, 1066

### Priority Calculation

```c
float compute_priority(IGNode* node) {
    if (is_briggs_safe(node)) {
        return INFINITY;  // Infinite priority
    }
    float effective_degree = (float)node->degree * COALESCE_FACTOR;
    return node->spill_cost / effective_degree;
}
```

**Formula**: `priority = spill_cost / (degree * 0.8)`

**Code location**: 0x1081400:1076

## Worklist Structures

```c
// Simplify worklist: Priority queue (max-heap)
struct SimplifyWorklist {
    IGNode**    nodes;            // +0x00: Heap array
    uint32_t    size;             // +0x08: Current size
    uint32_t    capacity;         // +0x0C: Allocated capacity
};

// Freeze worklist: FIFO queue
struct FreezeWorklist {
    IGNode**    queue;            // +0x00: Circular buffer
    uint32_t    head;             // +0x08: Read index
    uint32_t    tail;             // +0x0C: Write index
    uint32_t    capacity;         // +0x10: Buffer size
};

// Spill worklist: Priority queue (min-heap by cost)
struct SpillWorklist {
    IGNode**    nodes;            // +0x00: Heap array
    uint32_t    size;             // +0x08: Current size
    uint32_t    capacity;         // +0x0C: Allocated capacity
};
```

**Simplify**: Max-heap ordered by `compute_priority()`.

**Freeze**: FIFO for move-related nodes.

**Spill**: Min-heap ordered by spill cost.

## Coloring Structures

### Color Assignment Map

```c
struct ColorMap {
    uint16_t*   vreg_to_color;    // +0x00: [vreg_id] -> color (0-14)
    uint32_t    vreg_count;       // +0x08: Virtual register count
    uint64_t*   color_avail;      // +0x10: Bitset per register class
};
```

### Available Colors Per Class

```c
// K=15 registers: R0-R14
#define COLOR_MASK_ALL   0x7FFF    // 15 bits set

uint64_t available_colors[4] = {
    0x7FFF,  // GPR32: R0-R14
    0x5555,  // GPR64: R0,R2,R4,R6,R8,R10,R12,R14 (even only)
    0x00FF,  // PRED:  P0-P7
    0x7FFF   // H16:   H0-H14
};
```

### Precolored Nodes

```c
struct PrecoloredNode {
    uint32_t    vreg_id;          // +0x00: Virtual register ID
    uint16_t    color;            // +0x04: Fixed physical register
    uint8_t     reg_class;        // +0x06: Register class
    uint8_t     reserved;         // +0x07: Reserved for alignment
};

// Calling convention: R0-R7 for arguments, R0 for return
PrecoloredNode calling_conv_nodes[] = {
    {.vreg_id = ARG0_VREG, .color = 0, .reg_class = GPR32},
    {.vreg_id = ARG1_VREG, .color = 1, .reg_class = GPR32},
    // ... R2-R7 for remaining arguments
    {.vreg_id = RETVAL_VREG, .color = 0, .reg_class = GPR32}
};
```

## Lazy Reload Structures

### Spill Slot Tracking

```c
struct SpillSlot {
    int32_t     slot_offset;      // +0x00: Stack offset (-1 = unallocated)
    uint32_t    vreg_id;          // +0x04: Virtual register ID
    uint32_t    size_bytes;       // +0x08: Slot size (4, 8, 16)
    uint8_t     reg_class;        // +0x0C: Register class
    uint8_t     alignment;        // +0x0D: Required alignment (1, 2, 4)
    uint16_t    reserved;         // +0x0E: Padding
};

struct SpillSlotMap {
    SpillSlot*  slots;            // +0x00: Array of spill slots
    uint32_t    slot_count;       // +0x08: Number of slots
    int32_t     stack_size;       // +0x0C: Total stack frame size
};
```

### Live Range Information

```c
struct LiveRange {
    uint32_t    start_pc;         // +0x00: First definition point
    uint32_t    end_pc;           // +0x04: Last use point
    uint32_t*   use_points;       // +0x08: Array of use instruction PCs
    uint32_t    use_count;        // +0x10: Number of uses
    uint32_t    loop_depth;       // +0x14: Innermost loop depth
};
```

### Reload Point Tracking

```c
struct ReloadPoint {
    uint32_t    pc;               // +0x00: Instruction address
    uint32_t    vreg_id;          // +0x04: Virtual register to reload
    int32_t     slot_offset;      // +0x08: Source spill slot
    uint8_t     reg_class;        // +0x0C: Register class
    uint8_t     size_bytes;       // +0x0D: Reload size (4, 8, 16)
    uint16_t    flags;            // +0x0E: 0x01=redundant, 0x02=critical
};
```

### Reload Placement Algorithm

```c
void place_reloads(SpillSlotMap* slots, LiveRange* ranges, uint32_t vreg_count) {
    for (uint32_t v = 0; v < vreg_count; v++) {
        if (slots->slots[v].slot_offset == -1) continue;  // Not spilled

        LiveRange* lr = &ranges[v];
        for (uint32_t u = 0; u < lr->use_count; u++) {
            uint32_t use_pc = lr->use_points[u];

            // Check if already loaded on this path
            if (!is_value_available(v, use_pc)) {
                // Insert reload immediately before use
                insert_reload_instruction(use_pc, v, slots->slots[v].slot_offset);
            }
        }
    }
}
```

**Addresses**:
- sub_B612D0 (0xb612d0): Main entry
- sub_A78010 (0xa78010): Reload emission (lines 77-82)

## Memory Layout

### Graph Node Allocation

```c
struct NodeAllocator {
    IGNode*     node_pool;        // +0x00: Preallocated node array
    uint32_t    pool_size;        // +0x08: Capacity
    uint32_t    next_index;       // +0x0C: Next free node
    void*       backing_memory;   // +0x10: Raw memory block
};

// Allocation: 40 bytes per node, 16-byte aligned
#define NODE_SIZE          40
#define NODE_ALIGNMENT     16
```

### Edge List Representation

```c
// Adjacency list: variable-length arrays
struct EdgeList {
    IGNode**    edges;            // +0x00: Dynamic array of neighbor pointers
    uint32_t    capacity;         // +0x08: Allocated size
    uint32_t    count;            // +0x0C: Current edge count
};

// Edge addition: amortized O(1) with doubling
void add_edge(IGNode* from, IGNode* to) {
    if (from->neighbor_count >= from->neighbors capacity) {
        // Reallocate with 2x capacity
        uint32_t new_cap = from->neighbors_capacity * 2;
        IGNode** new_list = realloc(from->neighbors, new_cap * sizeof(IGNode*));
        from->neighbors = new_list;
        from->neighbors_capacity = new_cap;
    }
    from->neighbors[from->neighbor_count++] = to;
    from->degree++;
}
```

### Hash Maps for Fast Lookup

```c
// Open addressing with linear probing
#define HASH_LOAD_FACTOR  0.75f
#define HASH_INITIAL_SIZE 1024

struct VRegHashMap {
    uint32_t*   keys;             // +0x00: Virtual register IDs
    IGNode**    values;           // +0x08: Node pointers
    uint32_t    capacity;         // +0x10: Hash table size
    uint32_t    size;             // +0x14: Number of entries
};

uint32_t hash_vreg(uint32_t vreg_id, uint32_t capacity) {
    // Multiplicative hashing: 0x9e3779b9 = phi * 2^32
    return (vreg_id * 0x9e3779b9u) % capacity;
}

IGNode* lookup_node(VRegHashMap* map, uint32_t vreg_id) {
    uint32_t idx = hash_vreg(vreg_id, map->capacity);
    while (map->keys[idx] != EMPTY_KEY) {
        if (map->keys[idx] == vreg_id) {
            return map->values[idx];
        }
        idx = (idx + 1) % map->capacity;  // Linear probing
    }
    return NULL;
}
```

## Register Class Constraints

### Class Definitions

```c
enum RegisterClass {
    GPR32 = 0,    // General purpose 32-bit: R0-R254 (K=15 physical)
    GPR64 = 1,    // General purpose 64-bit: R0:R1, R2:R3, ... (K=7 physical)
    PRED  = 2,    // Predicate registers: P0-P7 (K=1 physical)
    H16   = 3     // Half-precision 16-bit: H0-H254 (K=15 physical)
};
```

### Alignment Requirements

```c
struct RegisterConstraints {
    uint8_t     reg_class;        // +0x00: RegisterClass enum
    uint8_t     alignment;        // +0x01: 1, 2, or 4 registers
    uint16_t    color_mask;       // +0x02: Available colors bitset
    uint32_t    reserved;         // +0x04: Padding
};

RegisterConstraints class_constraints[] = {
    {.reg_class = GPR32, .alignment = 1, .color_mask = 0x7FFF},  // Any R0-R14
    {.reg_class = GPR64, .alignment = 2, .color_mask = 0x5555},  // Even only
    {.reg_class = PRED,  .alignment = 1, .color_mask = 0x00FF},  // P0-P7
    {.reg_class = H16,   .alignment = 1, .color_mask = 0x7FFF}   // H0-H14
};
```

### Incompatibility Edges

```c
// Add implicit edge if registers alias or violate constraints
void add_constraint_edges(IGNode* node_a, IGNode* node_b) {
    // 64-bit uses pair of 32-bit registers: incompatible
    if (node_a->reg_class == GPR64 && node_b->reg_class == GPR32) {
        if (registers_overlap(node_a->vreg_id, node_b->vreg_id)) {
            add_edge(node_a, node_b);
            add_edge(node_b, node_a);
        }
    }

    // Bank conflict constraint: same bank = incompatible
    if (same_memory_bank(node_a->vreg_id, node_b->vreg_id)) {
        add_edge(node_a, node_b);
        add_edge(node_b, node_a);
    }
}
```

### Bank Conflict Constraints

```c
#define BANK_COUNT         32
#define BANK_WIDTH_BYTES    4
#define BANK_PENALTY_CYCLES 32

uint32_t get_bank_index(uint32_t address) {
    return ((address % 128) / BANK_WIDTH_BYTES) % BANK_COUNT;
}

bool same_memory_bank(uint32_t vreg_a, uint32_t vreg_b) {
    // Compute hypothetical addresses for register accesses
    uint32_t addr_a = vreg_a * 4;  // 4 bytes per 32-bit register
    uint32_t addr_b = vreg_b * 4;
    return get_bank_index(addr_a) == get_bank_index(addr_b);
}
```

## Binary Evidence

### Function Addresses

```c
// Register allocation core functions
#define FN_SIMPLIFY_AND_COLOR    0x1081400   // Main coloring loop
#define FN_SELECT_NODE_REMOVAL   0x1090bd0   // Briggs criterion
#define FN_ASSIGN_COLORS         0x12e1ef0   // Color assignment
#define FN_GRAPH_CONSTRUCTION    0xb612d0    // Build interference graph
#define FN_COMPUTE_SPILL_COST    0xb612d0    // Cost calculation (same entry)

// Helper functions
#define FN_ALLOC_OPERAND_SPEC    0xa778c0    // Allocate operand spec
#define FN_PROCESS_CONSTRAINTS   0xa79c90    // Process operand list
#define FN_ASSIGN_PHYSICAL_REG   0xb5ba00    // Physical register assignment
#define FN_EMIT_INSTRUCTION      0xa78010    // Emit with reloads
#define FN_CONSTRAINT_ENCODING   0xa77ab0    // Register class masks
```

### Code Patterns

```asm
; Briggs criterion check at 0x1090bd0:1039
mov     v64, [v62 + 8]           ; Load degree (m128i_u64[1])
cmp     v64, 0xE                 ; Compare with K_THRESHOLD (14)
jg      briggs_safe              ; If degree > 14, Briggs safe

; Coalesce factor at 0x1090bd0:603
mov     rax, 0xCCCCCCCCCCCCCCCD  ; Magic constant = 4/5
mul     rdx                      ; Multiply degree by 0.8
shr     rdx, 3                   ; Fixed-point division

; Priority calculation at 0x1081400:1076
mov     eax, [v6 + 16]           ; Load base priority
imul    eax, weight_factor       ; Multiply by conditional weight
```

### Data Structure Sizes

```c
sizeof(IGNode)              = 40 bytes (0x28)
sizeof(SpillSlot)           = 16 bytes (0x10)
sizeof(LiveRange)           = 24 bytes (0x18)
sizeof(ReloadPoint)         = 16 bytes (0x10)
sizeof(ColorMap)            = 24 bytes (0x18)
sizeof(SimplifyWorklist)    = 16 bytes (0x10)
sizeof(FreezeWorklist)      = 20 bytes (0x14)
sizeof(SpillWorklist)       = 16 bytes (0x10)
sizeof(RegisterConstraints) = 8 bytes  (0x08)
```

## Algorithm Pseudocode

### Graph Coloring with Briggs Optimization

```c
void simplify_and_color(InterferenceGraph* graph) {
    SimplifyWorklist simplify = {0};
    FreezeWorklist freeze = {0};
    SpillWorklist spill = {0};

    // Initial worklist population
    for (uint32_t i = 0; i < graph->node_count; i++) {
        IGNode* node = &graph->nodes[i];
        if (node->flags & PRECOLORED) continue;

        if (compute_priority(node) == INFINITY) {
            push_heap(&simplify, node);
        } else if (is_move_related(node)) {
            enqueue(&freeze, node);
        } else {
            push_heap(&spill, node);
        }
    }

    // Main loop: simplify until stack empty
    while (simplify.size > 0 || freeze.size > 0 || spill.size > 0) {
        IGNode* node = NULL;

        // Priority: Simplify > Freeze > Spill
        if (simplify.size > 0) {
            node = pop_heap(&simplify);  // Max priority
        } else if (freeze.size > 0) {
            node = dequeue(&freeze);
        } else if (spill.size > 0) {
            node = pop_heap(&spill);     // Min cost
        }

        // Remove from graph, push to stack
        remove_node(graph, node);
        push_stack(&coloring_stack, node);

        // Update neighbor degrees
        for (uint32_t i = 0; i < node->neighbor_count; i++) {
            node->neighbors[i]->degree--;
        }
    }

    // Pop stack and assign colors
    while (coloring_stack.size > 0) {
        IGNode* node = pop_stack(&coloring_stack);
        assign_color(graph, node);
    }
}
```

### Color Assignment

```c
bool assign_color(InterferenceGraph* graph, IGNode* node) {
    RegisterConstraints* rc = &class_constraints[node->reg_class];
    uint16_t available = rc->color_mask;

    // Remove colors used by neighbors
    for (uint32_t i = 0; i < node->neighbor_count; i++) {
        IGNode* neighbor = node->neighbors[i];
        if (neighbor->color != UNCOLORED) {
            available &= ~(1u << neighbor->color);
        }
    }

    // Check alignment constraints for 64-bit
    if (rc->alignment == 2) {
        available &= 0x5555;  // Even registers only: R0,R2,R4,...
    }

    // Assign first available color
    if (available == 0) {
        mark_for_spill(node);
        return false;
    }

    node->color = __builtin_ctz(available);  // First set bit
    return true;
}
```

### Spill Cost with Loop Depth

```c
float compute_spill_cost_with_loops(uint32_t vreg_id,
                                     uint32_t* def_blocks,
                                     uint32_t* use_blocks,
                                     uint32_t def_count,
                                     uint32_t use_count) {
    float total_cost = 0.0f;

    for (uint32_t d = 0; d < def_count; d++) {
        uint32_t def_block = def_blocks[d];
        uint32_t def_depth = get_loop_depth(def_block);
        float def_weight = powf(LOOP_DEPTH_BASE, (float)def_depth);

        for (uint32_t u = 0; u < use_count; u++) {
            uint32_t use_block = use_blocks[u];
            uint32_t use_depth = get_loop_depth(use_block);
            float use_weight = powf(LOOP_DEPTH_BASE, (float)use_depth);

            float mem_latency = estimate_cache_latency(def_block, use_block);
            total_cost += def_weight * use_weight * mem_latency;
        }
    }

    return total_cost;
}
```
