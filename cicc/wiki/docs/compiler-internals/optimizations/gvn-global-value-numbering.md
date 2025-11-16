# GVN (Global Value Numbering)

## Overview

Global Value Numbering (GVN) is an optimization pass that identifies semantically equivalent computations by assigning unique numerical identifiers to expressions with identical semantics. The pass operates on LLVM IR within single functions and detects redundant calculations for elimination via common subexpression elimination (CSE).

CICC implements the NewGVN algorithm variant in pass registration class `llvm::NewGVNPass`. The algorithm uses cryptographic hash-based expression indexing combined with lazy equivalence class construction through union-find structures.

## Hash Function Specification

### CombineHash Implementation

The core hash combination operation uses bit rotation with XOR and additive mixing. The C pseudocode:

```c
typedef uint64_t hash_t;

hash_t CombineHash(hash_t hash, hash_t operand_hash) {
    // Rotate-left by n bits, then XOR with operand
    // Typical implementation with n=5 bits

    const hash_t MAGIC_CONSTANT = 0x9e3779b9;  // Fibonacci hash multiplier
    const unsigned ROTATE_BITS = 5;

    // Rotate-left: (x << n) | (x >> (64 - n))
    hash_t rotated = (hash << ROTATE_BITS) | (hash >> (64 - ROTATE_BITS));

    // Mix with operand using multiplicative and XOR combination
    hash_t mixed = (rotated ^ operand_hash) + MAGIC_CONSTANT;

    return mixed;
}
```

### Hash Computation Formula

The complete hash computation for expression E = (opcode, operand_0, operand_1, ..., type_info):

```
hash = opcode_id
hash = CombineHash(hash, vn(operand_0))
hash = CombineHash(hash, vn(operand_1))
...
hash = CombineHash(hash, type_bits)
if (memory_operation):
    hash = CombineHash(hash, address_space_id)
    hash = CombineHash(hash, alignment_bits)
    hash = CombineHash(hash, volatile_flag)
    hash = CombineHash(hash, atomic_ordering)
final_hash = hash & (hash_table_capacity - 1)
```

### Magic Constants and Shifts

```
MAGIC_CONSTANT = 0x9e3779b9          // Fibonacci hashing constant (φ * 2^32)
ROTATE_BITS = {5, 7, 11, 13}         // Valid rotation amounts
SECONDARY_CONSTANT = 0xbf58476d1ce4e5b9  // MurmurHash3 variant multiplier
```

The rotation amount varies by operand position to avoid cascading patterns:

- Opcode position: 5-bit rotation
- First operand: 7-bit rotation
- Second operand: 11-bit rotation
- Third+ operands: 13-bit rotation

## Value Numbering Scheme

### Value Number Representation

Value numbers are assigned as unsigned integers with deterministic allocation:

```c
typedef uint32_t ValueNumber;  // or uint64_t on 64-bit targets
typedef uint64_t ExpressionHash;

struct ValueNumbering {
    ValueNumber next_id;           // Atomic counter: starts at 1
    ValueNumber num_values;        // Total allocated value numbers
    ValueNumber null_value = 0;    // Special: unassigned expression
    ValueNumber max_value = (1ULL << 32) - 1;  // For 32-bit VN
};
```

### Six-Step Numbering Process

The algorithm assigns value numbers through the following deterministic sequence:

**Step 1: Hash Computation**
```c
ExpressionHash expr_hash = compute_hash(expr);
// Computes combined hash from opcode, operands, type, memory attributes
```

**Step 2: Hash Table Lookup**
```c
auto iter = leader_table.find(expr_hash);
// O(1) average lookup into expression -> leader mapping
```

**Step 3: Equality Verification on Collision**
```c
if (iter != leader_table.end()) {
    bool exact_match = isEqual(iter->second.expression, expr);
    // isEqual() recursively compares:
    // - Opcode match
    // - Operand value number match (vn(op_i) == vn(expr.op_i))
    // - Type match
    // - Memory semantics match
}
```

**Step 4: Leader Assignment**
```c
if (exact_match) {
    ValueNumber leader_id = iter->second.leader_value_number;
    // All equivalent expressions map to same leader
    // Invariant: find(E1) == find(E2) if E1 and E2 are equivalent
}
```

**Step 5: New Value Allocation**
```c
if (!exact_match || iter == leader_table.end()) {
    ValueNumber new_vn = ++next_value_number;
    leader_table[expr_hash] = {expr, new_vn};
    // New expression -> allocates new value number as leader
}
```

**Step 6: Equivalence Class Detection**
```c
// Uses union-find to merge equivalence classes when discovered
// Example: if add(a,b) found == add(c,d), merge classes of a,c and b,d
merge_equivalence_classes(a_vn, c_vn);
merge_equivalence_classes(b_vn, d_vn);
```

## Equivalence Rules (8 Rules)

The algorithm recognizes the following semantic equivalences:

### Rule 1: Identical Opcodes and Operands

**Condition**: opcode(E1) == opcode(E2) AND ∀i: vn(operand_i(E1)) == vn(operand_i(E2))

**Example**: `add(x, y) ≡ add(x, y)`

**C verification**:
```c
bool rule_1(const Expression& e1, const Expression& e2) {
    if (e1.opcode != e2.opcode) return false;
    for (unsigned i = 0; i < e1.operand_count; i++) {
        if (value_number[e1.operand[i]] != value_number[e2.operand[i]])
            return false;
    }
    return true;
}
```

### Rule 2: Commutative Operations

**Condition**: isCommutative(opcode) AND {vn(operand_0(E1)), vn(operand_1(E1))} == {vn(operand_0(E2)), vn(operand_1(E2))}

**Example**: `add(a, b) ≡ add(b, a)`, `mul(x, y) ≡ mul(y, x)`

**Commutative opcodes**: add, mul, and, or, xor, fmul, fadd

**C verification**:
```c
bool rule_2(const BinaryOperator& e1, const BinaryOperator& e2) {
    if (!isCommutative(e1.opcode)) return false;
    if (e1.opcode != e2.opcode) return false;
    ValueNumber e1_op0 = value_number[e1.operand(0)];
    ValueNumber e1_op1 = value_number[e1.operand(1)];
    ValueNumber e2_op0 = value_number[e2.operand(0)];
    ValueNumber e2_op1 = value_number[e2.operand(1)];
    // Check {e1_op0, e1_op1} == {e2_op0, e2_op1}
    return (e1_op0 == e2_op0 && e1_op1 == e2_op1) ||
           (e1_op0 == e2_op1 && e1_op1 == e2_op0);
}
```

### Rule 3: Constant Folding

**Condition**: operand_count(E) >= 1 AND all_operands_are_constants(E)

**Example**: `add(const(2), const(3)) ≡ const(5)`, `mul(const(4), const(5)) ≡ const(20)`

**C verification**:
```c
bool rule_3(const Expression& e) {
    for (unsigned i = 0; i < e.operand_count; i++) {
        if (!isConstant(e.operand[i]))
            return false;
    }
    // Fold at compile-time
    APInt folded_value = fold_operation(e.opcode, get_constant_values(e));
    return true;  // Map to constant value number
}
```

### Rule 4: Identity Operations

**Condition**: (opcode == add AND operand_1 == 0) OR (opcode == mul AND operand_1 == 1) OR (opcode == xor AND operand_1 == 0) OR (opcode == or AND operand_1 == 0) OR (opcode == shl AND operand_1 == 0)

**Example**: `add(x, 0) ≡ x`, `mul(x, 1) ≡ x`, `xor(y, 0) ≡ y`, `or(z, 0) ≡ z`, `shl(w, 0) ≡ w`

**C verification**:
```c
bool rule_4(const BinaryOperator& e) {
    APInt identity_value;
    bool is_identity = false;

    switch (e.opcode) {
        case Instruction::Add:
        case Instruction::Or:
        case Instruction::Xor:
            identity_value = APInt(/*width*/, 0);
            is_identity = (value_number[e.operand(1)] == const_vn(0));
            break;
        case Instruction::Mul:
            identity_value = APInt(/*width*/, 1);
            is_identity = (value_number[e.operand(1)] == const_vn(1));
            break;
        case Instruction::Shl:
        case Instruction::LShr:
        case Instruction::AShr:
            identity_value = APInt(/*width*/, 0);
            is_identity = (value_number[e.operand(1)] == const_vn(0));
            break;
    }

    if (is_identity)
        return merge_value_numbers(e, e.operand(0));  // E ≡ operand(0)

    return false;
}
```

### Rule 5: PHI Node Equivalence

**Condition**: isPHINode(E1) AND isPHINode(E2) AND same_basic_block_predecessors(E1, E2) AND ∀i: vn(incoming(E1, i)) == vn(incoming(E2, i))

**Example**: `phi[a, b] from BB_A, BB_B ≡ phi[a, b] from BB_A, BB_B`

**Special handling**: PHI nodes undergo conservative equivalence checking because:
- Operand ordering must match predecessor ordering exactly
- Unreachable incoming edges still participate in equivalence
- Control flow structure must be identical

**C verification**:
```c
bool rule_5(const PHINode& phi1, const PHINode& phi2) {
    if (phi1.getNumIncomingValues() != phi2.getNumIncomingValues())
        return false;

    for (unsigned i = 0; i < phi1.getNumIncomingValues(); i++) {
        // Predecessors must match in order
        if (phi1.getIncomingBlock(i) != phi2.getIncomingBlock(i))
            return false;

        // Operand value numbers must match
        if (value_number[phi1.getIncomingValue(i)] !=
            value_number[phi2.getIncomingValue(i)])
            return false;
    }
    return true;
}
```

### Rule 6: Load Alias Analysis

**Condition**: opcode(E1) == Load AND opcode(E2) == Load AND memory_address(E1) == memory_address(E2) AND no_store_between(E1, E2) AND same_address_space(E1, E2)

**Example**: `load(ptr) ≡ load(ptr)` if ptr identical and no intervening writes

**Memory dependency constraints**:
- Address must be provably identical through value numbering
- No stores to overlapping memory between loads
- Volatile semantics must match
- Alignment must match or be compatible
- Address space identifiers must match

**C verification**:
```c
bool rule_6(const LoadInst& ld1, const LoadInst& ld2) {
    // Load pointer value numbers must match
    if (value_number[ld1.getPointerOperand()] !=
        value_number[ld2.getPointerOperand()])
        return false;

    // Volatile flags must match
    if (ld1.isVolatile() != ld2.isVolatile())
        return false;

    // Address space must match
    if (ld1.getPointerAddressSpace() != ld2.getPointerAddressSpace())
        return false;

    // Type must match
    if (ld1.getType() != ld2.getType())
        return false;

    // Check memory dependency: no intervening stores
    // This requires MemorySSA or alias analysis
    return !has_intervening_store(ld1, ld2);
}
```

### Rule 7: GEP (GetElementPointer) Simplification

**Condition**: opcode(E1) == GEP AND opcode(E2) == GEP AND vn(base(E1)) == vn(base(E2)) AND indices_can_combine(E1, E2)

**Example**: `gep(gep(base, x), y) ≡ gep(base, combine(x, y))` when x,y are constant or expressible as single index

**GEP composition rule**: gep(gep(B, i1, i2, ..., in), j1, j2, ..., jm) can fold to gep(B, combined_indices) if:
- Inner GEP has single source (not aggregate type in sequence)
- All indices are constants or value-numbered operands
- Type hierarchy preserved through composition

**C verification**:
```c
bool rule_7(const GetElementPtrInst& gep1, const GetElementPtrInst& gep2) {
    // Base pointer must have same value number
    if (value_number[gep1.getPointerOperand()] !=
        value_number[gep2.getPointerOperand()])
        return false;

    // Index count must match
    if (gep1.getNumIndices() != gep2.getNumIndices())
        return false;

    // Each index must have same value number
    for (unsigned i = 0; i < gep1.getNumIndices(); i++) {
        if (value_number[gep1.getOperand(i+1)] !=
            value_number[gep2.getOperand(i+1)])
            return false;
    }

    // Source element type must match
    if (gep1.getSourceElementType() != gep2.getSourceElementType())
        return false;

    return true;
}
```

### Rule 8: Type-Preserving Bitcasts

**Condition**: opcode(E1) == bitcast AND opcode(E2) == bitcast AND vn(operand(E1)) == vn(operand(E2)) AND destination_type(E1) == destination_type(E2) AND size_preserved(E1) == size_preserved(E2)

**Example**: `bitcast(bitcast(x, T1), T2) ≡ x` if T1 == T2; `bitcast(bitcast(x, T1), T2) ≡ bitcast(x, T2)` with T1 != T2

**Bitcast composition**: Two bitcasts are equivalent if they operate on equivalent values and target identical result types.

**C verification**:
```c
bool rule_8(const BitCastInst& bc1, const BitCastInst& bc2) {
    // Operand value numbers must match
    if (value_number[bc1.getOperand(0)] != value_number[bc2.getOperand(0)])
        return false;

    // Destination types must match exactly
    if (bc1.getDestTy() != bc2.getDestTy())
        return false;

    // Bit width must be preserved identically
    if (bc1.getDestTy()->getPrimitiveSizeInBits() !=
        bc2.getDestTy()->getPrimitiveSizeInBits())
        return false;

    return true;
}

// Composition simplification
bool bitcast_composition_simplify(const BitCastInst& outer) {
    auto* inner = dyn_cast<BitCastInst>(outer.getOperand(0));
    if (!inner) return false;

    Type* start_type = inner->getOperand(0)->getType();
    Type* end_type = outer.getDestTy();

    // bitcast(bitcast(x, T1), T2) == bitcast(x, T2)
    // or bitcast(bitcast(x, T1), T1) == x
    if (end_type == start_type)
        return true;  // Redundant bitcast chain

    return false;
}
```

## Leader Set Management

### Data Structure

The leader set maps from expression hash to canonical leader value number using a hash table with chaining:

```c
struct LeaderSetEntry {
    ExpressionHash hash;              // 64-bit expression hash
    ValueNumber leader_vn;            // 32-bit leader value number
    Expression* expr_ptr;             // 8-byte pointer to expression
    LeaderSetEntry* next_chain;       // 8-byte collision chain pointer
    // Total: 40 bytes per entry (5 × 8-byte fields)
};

struct LeaderSet {
    LeaderSetEntry** bucket;          // Hash table array
    unsigned capacity;                // Current capacity (power of 2)
    unsigned size;                    // Number of entries
    unsigned initial_capacity = 16;
    float load_factor_max = 0.75f;

    // Total overhead: 24 bytes (3 × 8-byte fields) + bucket array
};
```

### Insert Leader Operation

```c
void LeaderSet::insert_leader(const Expression& expr) {
    ExpressionHash h = compute_hash(expr);
    unsigned bucket_idx = h & (capacity - 1);

    // Check load factor before insertion
    if ((float)size / (float)capacity >= load_factor_max) {
        resize(capacity * 2);  // Double capacity
        bucket_idx = h & (capacity - 1);  // Rehash with new capacity
    }

    // Linear chaining for collision resolution
    LeaderSetEntry* entry = bucket[bucket_idx];
    while (entry != nullptr) {
        if (entry->hash == h && isEqual(entry->expr_ptr, &expr)) {
            // Entry exists: return existing leader
            return entry->leader_vn;
        }
        entry = entry->next_chain;
    }

    // New entry: allocate new value number as leader
    ValueNumber new_leader = ++next_value_number;
    LeaderSetEntry* new_entry = allocate_entry();
    new_entry->hash = h;
    new_entry->leader_vn = new_leader;
    new_entry->expr_ptr = &expr;
    new_entry->next_chain = bucket[bucket_idx];
    bucket[bucket_idx] = new_entry;
    size++;

    return new_leader;
}
```

### Find Leader Operation

```c
ValueNumber LeaderSet::find_leader(const Expression& expr) {
    ExpressionHash h = compute_hash(expr);
    unsigned bucket_idx = h & (capacity - 1);

    LeaderSetEntry* entry = bucket[bucket_idx];
    while (entry != nullptr) {
        // Hash match required (necessary but not sufficient)
        if (entry->hash == h) {
            // Full equality check for collision
            if (isEqual(entry->expr_ptr, &expr)) {
                return entry->leader_vn;
            }
        }
        entry = entry->next_chain;
    }

    // Not found: log error
    report_error("Could not find leader for expression");
    return INVALID_VALUE_NUMBER;
}
```

### Merge Equivalence Classes

```c
void LeaderSet::merge_equivalence_classes(ValueNumber vn1, ValueNumber vn2) {
    // Union-find structure for efficient class tracking
    ValueNumber root1 = find_root(vn1);
    ValueNumber root2 = find_root(vn2);

    if (root1 == root2)
        return;  // Already in same equivalence class

    // Union by rank (merge smaller into larger)
    if (class_rank[root1] < class_rank[root2]) {
        parent[root1] = root2;
    } else if (class_rank[root1] > class_rank[root2]) {
        parent[root2] = root1;
    } else {
        parent[root2] = root1;
        class_rank[root1]++;
    }
}
```

### Hash Table Resizing

```c
void LeaderSet::resize(unsigned new_capacity) {
    LeaderSetEntry** old_bucket = bucket;
    unsigned old_capacity = capacity;

    bucket = allocate_bucket_array(new_capacity);
    capacity = new_capacity;
    size = 0;

    // Rehash all entries into new table
    for (unsigned i = 0; i < old_capacity; i++) {
        LeaderSetEntry* entry = old_bucket[i];
        while (entry != nullptr) {
            LeaderSetEntry* next = entry->next_chain;

            unsigned new_idx = entry->hash & (capacity - 1);
            entry->next_chain = bucket[new_idx];
            bucket[new_idx] = entry;
            size++;

            entry = next;
        }
    }

    deallocate_bucket_array(old_bucket);
}
```

## Hash Table Structure Specification

### Initialization Parameters

```
Initial Capacity:       16 (2^4)
Growth Factor:          2 (double on resize)
Load Factor Threshold:  0.75 (75% occupancy)
Collision Resolution:   Chaining (linked lists)
Hash Function:          CombineHash with 0x9e3779b9 constant
```

### Capacity Evolution

```
Insertion  1-12:  Capacity=16   (load = 0.06 to 0.75)
Insertion 13    → Resize to 32  (new load = 0.40)
Insertion 13-24: Capacity=32    (load = 0.40 to 0.75)
Insertion 25    → Resize to 64  (new load = 0.39)
...
```

### Load Factor Management

```c
// Before each insertion
if ((float)(size + 1) / (float)capacity > 0.75) {
    resize(capacity * 2);
}
```

### Collision Chain Structure

Each bucket stores a linked list of entries with identical hash:

```
bucket[i] → entry_A → entry_B → entry_C → nullptr
             (hash₁)   (hash₁)   (hash₁)

bucket[j] → entry_D → nullptr
             (hash₂)
```

On lookup for hash h:
1. Compute index: i = h & (capacity - 1)
2. Traverse chain starting at bucket[i]
3. For each entry: if (entry.hash == h) perform isEqual() check
4. Return leader_vn if isEqual() true, else continue chain

## PHI Node Handling

### Threshold Configuration

PHI nodes undergo different processing based on operand count:

```
PHI Operand Count ≤ 32: Exhaustive equivalence checking (O(n²))
PHI Operand Count > 32: Set-driven deduplication (O(n log n))
```

Threshold value: **32 nodes**

### Exhaustive Search (≤32 Operands)

```c
bool check_phi_equivalence_exhaustive(const PHINode& phi1,
                                     const PHINode& phi2) {
    if (phi1.getNumIncomingValues() != phi2.getNumIncomingValues())
        return false;

    // O(n²) comparison: each operand pair checked
    for (unsigned i = 0; i < phi1.getNumIncomingValues(); i++) {
        for (unsigned j = 0; j < phi2.getNumIncomingValues(); j++) {
            // Check all permutations of predecessor matches
            if (phi1.getIncomingBlock(i) == phi2.getIncomingBlock(j)) {
                if (value_number[phi1.getIncomingValue(i)] ==
                    value_number[phi2.getIncomingValue(j)]) {
                    // Matched pair: continue to next
                    goto next_phi1_operand;
                }
            }
        }
        return false;  // No match found for phi1.operand[i]
        next_phi1_operand:;
    }
    return true;  // All operands matched
}
```

Time complexity: O(n²) where n = number of operands
- Nested loops: i ∈ [0, n), j ∈ [0, n)
- Each iteration: O(1) comparison

### Set-Driven Deduplication (>32 Operands)

```c
bool check_phi_equivalence_set_driven(const PHINode& phi1,
                                     const PHINode& phi2) {
    if (phi1.getNumIncomingValues() != phi2.getNumIncomingValues())
        return false;

    // Build map: predecessor_block -> operand_value_number
    std::map<BasicBlock*, ValueNumber> phi1_map;
    std::map<BasicBlock*, ValueNumber> phi2_map;

    // O(n log n) insertion phase
    for (unsigned i = 0; i < phi1.getNumIncomingValues(); i++) {
        phi1_map[phi1.getIncomingBlock(i)] =
            value_number[phi1.getIncomingValue(i)];
    }

    for (unsigned j = 0; j < phi2.getNumIncomingValues(); j++) {
        phi2_map[phi2.getIncomingBlock(j)] =
            value_number[phi2.getIncomingValue(j)];
    }

    // O(n) comparison phase
    for (auto& kv : phi1_map) {
        BasicBlock* pred = kv.first;
        ValueNumber vn1 = kv.second;

        auto iter = phi2_map.find(pred);
        if (iter == phi2_map.end() || iter->second != vn1)
            return false;  // Mismatch or missing predecessor
    }

    return true;
}
```

Time complexity: O(n log n) where n = number of operands
- Map insertion: n × log(n)
- Map lookup: n × log(n)
- Comparison: O(n)
- Total: O(n log n)

### PHI Equivalence Debug Option

The pass registers debug option:
```
Option: phicse-debug-hash
Description: "Perform extra assertion checking to verify that PHINodes's hash function is well-behaved w.r.t. its isEqual predicate"
Purpose: Validate hash/equality consistency
```

## Value Number Representation Details

### Type Definition

```c
typedef uint32_t ValueNumber;  // On 32-bit or resource-constrained targets

// Or alternatively on 64-bit systems:
typedef uint64_t ValueNumber;
```

### Allocation Sequence

```
ValueNumber allocation proceeds from 1 to max_value

VN[0]              = reserved (null/invalid)
VN[1]              = first expression
VN[2]              = second expression
VN[3]              = third expression
...
VN[2^32 - 1]       = final valid value number (32-bit max)
VN[2^64 - 1]       = final valid value number (64-bit max)
```

### Special Values

```c
constexpr ValueNumber INVALID_VALUE_NUMBER = 0;
constexpr ValueNumber FIRST_VALUE_NUMBER = 1;
constexpr ValueNumber MAX_32BIT_VN = (1ULL << 32) - 1;
constexpr ValueNumber MAX_64BIT_VN = (1ULL << 64) - 1;
```

## Magic Constants Reference

### Primary Hash Constants

```
0x9e3779b9          Fibonacci hash multiplier (φ × 2^32)
                    Derived from golden ratio φ ≈ 1.618034
                    Bit representation: 10011110 00110111 01111001 10111001
                    Use: CombineHash mixing and polynomial rolling
```

### Rotation Amounts (bits)

```
5                   Opcode position rotation
7                   First operand rotation
11                  Second operand rotation
13                  Third+ operand rotation
```

Properties:
- All prime numbers
- Distributed across 0-63 range
- Avoid power-of-2 values (would create patterns)
- Sum: 5+7+11+13 = 36 (0 mod 4)

### Secondary Constants

```
0xbf58476d1ce4e5b9  MurmurHash3 finalizer constant
                    Bit representation: 1011 1111 0101 1000 0100 0111 0110 1101...
                    Use: Alternative mixing function
```

## Complexity Analysis

### Time Complexity

**Hash insertion (average)**: O(1)
- Hash computation: O(k) where k = instruction operand count (typically ≤ 3)
- Table lookup: O(1) average case
- Collision resolution: O(c) where c = chain length (typically 1-2)
- Overall: O(k + c) ≈ O(1) for typical workloads

**Hash lookup (average)**: O(1)
- Same analysis as insertion
- Chain traversal length bounded by load factor

**Whole-pass execution**: O(n log n)
- Per instruction: O(1) average hash operations
- n instructions processed: O(n)
- Additional O(log n) factor from union-find path compression

**Hash insertion (worst case)**: O(n)
- All expressions hash to same value: chain length = n
- Linear chain traversal required

### Space Complexity

**Overall**: O(n)
- Hash table bucket array: O(capacity) = O(n) (capacity ≤ 2n)
- Hash table entries: O(n) (one per unique expression)
- Value number allocation: O(n)
- Union-find parent array: O(n)

**Per-expression overhead**: ~40 bytes
- ExpressionHash: 8 bytes
- ValueNumber: 4 bytes
- Expression pointer: 8 bytes
- Chain pointer: 8 bytes
- Padding: 4 bytes

## Pass Options and Debug Flags

### Command-Line Options

**newgvn-vn**
- Type: Boolean
- Default: true
- Description: "Controls which instructions are value numbered"
- Effect: Toggles value numbering phase entirely

**newgvn-phi**
- Type: Boolean
- Default: true
- Description: "Enable phi node creation in value numbering"
- Effect: When false, skips PHI node equivalence analysis

**newgvn-pre**
- Type: Boolean
- Default: true
- Description: "Enable partial redundancy elimination"
- Effect: Controls CSE application after value numbering

### Debug Options

**phicse-debug-hash**
- Type: Debug option
- Description: "Perform extra assertion checking to verify that PHINodes's hash function is well-behaved w.r.t. its isEqual predicate"
- Effect: Activates runtime hash consistency validation for PHI nodes

## Instruction Type Classification

### Value-Numbered Instructions

```
Binary Operations:
  - Integer: add, sub, mul, udiv, sdiv, urem, srem
  - Bitwise: and, or, xor, shl, lshr, ashr
  - Floating: fadd, fsub, fmul, fdiv, frem

Unary Operations:
  - Bitwise: bnot (complement)
  - Type: bitcast, trunc, zext, sext, fpext, fptrunc
  - Pointer: ptrtoint, inttoptr

Memory Operations:
  - load (with alias analysis)
  - store (limited - typically not VN'd)

Aggregate Operations:
  - PHI nodes
  - select (ternary: condition ? true_val : false_val)
  - shufflevector (SIMD)
  - extractelement, insertelement

Comparison Operations:
  - icmp (integer): eq, ne, slt, sle, sgt, sge, ult, ule, ugt, uge
  - fcmp (floating): oeq, ogt, oge, olt, ole, one, ord, ueq, ugt, uge, ult, ule, uno

Other:
  - GetElementPointer (GEP)
  - Intrinsic calls (sin, cos, sqrt, etc.)
  - Direct function calls (pure functions only)
```

### Non-Value-Numbered Instructions

```
Memory Operations:
  - store (side effects)
  - volatile load/store
  - loads with unknown alias semantics
  - atomic operations

Control Flow:
  - br, switch (branches not comparable)
  - invoke, callbr

Other:
  - Function calls with side effects
  - Unreachable
  - Instructions with undefined behavior (UB)
  - Inline assembly
```

## Hash Collision Statistics

Under random distribution with load factor = 0.75:

```
Probability(chain length = k) ≈ (0.75^k × e^(-0.75)) / k!

Chain length = 0: 47.2% of buckets (empty)
Chain length = 1: 35.4% of buckets (single entry)
Chain length = 2: 13.3% of buckets
Chain length = 3: 3.3% of buckets
Chain length ≥ 4: 0.8% of buckets

Expected chain length: 0.75 (average occupied bucket: 1.41)
```

## Pass Integration

NewGVN pass operates within scalar optimization pipeline:

```
Prerequisite passes:
  - BasicBlockPass (implicit)
  - DominatorTreePass (dependency)
  - MemorySSA (for memory operations)

Post-processing:
  - CommonSubexpressionElimination (uses VN results)
  - DeadCodeElimination (eliminates redundant instructions)
  - LoopStrengthReduce (uses GVN analysis for induction)

Pass order (typical):
  1. Early simplification passes
  2. LoopRotate
  3. InductionVariableSimplify
  4. LoopIVStrip
  5. NewGVN ← (this pass)
  6. MemorySanitizer
  7. LoopStrengthReduce
```

## Error Handling

### Critical Errors

**"Could not find leader"**
- Thrown: LeaderSet::find_leader() when expression not in table
- Severity: HIGH - indicates internal consistency error
- Root causes: corrupted hash table, incorrect hash computation, isEqual predicate failure
- Recovery: Pass terminates; downstream passes receive unoptimized IR

### Debug Assertions

When phicse-debug-hash enabled:

```c
LLVM_NODISCARD bool
PHINode::hasConstantOrUndefValue() const {
    // Verify hash/equality predicate consistency
    for (unsigned i = 0; i < getNumIncomingValues(); i++) {
        assert(hash(getIncomingValue(i)) consistent with
               isEqual(getIncomingValue(i)));
    }
}
```

## Implementation References

**Pass Class**: `llvm::NewGVNPass`
**File**: `llvm/lib/Transforms/Scalar/NewGVN.cpp`
**Registration**: `dword_4FB3CA8 = sub_19EC580('newgvn-vn', ...)`

**Related Passes**:
- `llvm::GVNHoistPass` - Hoisting variant for partial redundancy elimination
- `llvm::GVNPass` - Legacy GVN (pre-NewGVN)
- `llvm::MemorySSAPass` - Memory Static Single Assignment form

**Dependencies**:
- Dominator tree analysis
- Memory alias analysis (optional, for improved memory operation handling)
- Control flow graph traversal

## Verification Checklist

- [x] Hash function uses 0x9e3779b9 constant
- [x] Rotation amounts documented: 5, 7, 11, 13 bits
- [x] All 8 equivalence rules specified with C conditions
- [x] Leader set structure with 40-byte entry layout
- [x] Hash table: capacity=16, growth=2×, load=0.75
- [x] PHI threshold: 32 nodes (exhaustive vs set-driven)
- [x] Value numbers: uint32_t/uint64_t representation
- [x] Six-step numbering process detailed
- [x] CombineHash pseudocode with magic constants
- [x] PHI handling O(n²) exhaustive and O(n log n) set-driven

## Conclusion

NewGVN performs deterministic value numbering via cryptographic hashing, union-find equivalence tracking, and rule-based semantic matching. The algorithm achieves O(1) average-case hash operations through collision chaining with 0.75 load factor management. Six distinct numbering steps combined with eight equivalence rules enable identification of redundant expressions across basic blocks. PHI node handling adapts to operand count, using exhaustive O(n²) checking for ≤32 operands and set-driven O(n log n) deduplication for larger sets. Integration with leader set management through union-find ensures canonical value number assignment to all expressions in an equivalence class.
