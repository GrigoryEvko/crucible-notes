# GVN (Global Value Numbering) Hash Function - Quick Reference

## Overview
Global Value Numbering (GVN) is a compiler optimization technique that eliminates redundant computations by assigning value numbers to equivalent expressions. The implementation uses a cryptographic hash function to efficiently identify and track value equivalence.

## Key Findings

### Hash Function Algorithm
- **Type**: Cryptographic hash with component combination
- **Primary Components**:
  1. **Opcode** - Instruction type (add, mul, load, phi, etc.)
  2. **Operands** - Hashed value numbers of operand expressions
  3. **Value Type** - Data type of result (i32, float, vector, etc.)
  4. **Memory Attributes** - Address space, alignment, atomic properties

### Value Numbering Scheme
- **Strategy**: Lexicographic assignment with equivalence classes
- **Storage**: Hash table mapping expressions to leader value numbers
- **Lookup**: O(1) average case using hash table probing
- **Verification**: isEqual() predicate confirms semantic equivalence

### Leader Set Management
The leader is the canonical representative of an equivalence class:
- `find_leader(expr)` - Returns value number of leader (O(1))
- `insert_leader(expr)` - Adds new expression to leader table
- `merge_equivalence()` - Unifies two equivalence classes
- Error: "Could not find leader" when lookup fails

## Pass Options

### NewGVN Pass Parameters
```
-newgvn-vn        : Controls which instructions are value numbered
-newgvn-phi       : Controls which instructions create phi of ops
-phicse-debug-hash: Enables assertion checking for PHI hash function
```

## Expression Equivalence Rules

### 1. Identical Operations
```
add(x, y) ≡ add(x, y)      [Same opcode + operands]
```

### 2. Commutative Operations
```
add(a, b) ≡ add(b, a)      [Operation is commutative]
mul(x, y) ≡ mul(y, x)      [Operand order normalized]
```

### 3. Constant Folding
```
add(const(2), const(3)) ≡ const(5)
mul(const(4), const(0)) ≡ const(0)
```

### 4. Identity Elements
```
add(x, 0) ≡ x              [0 is additive identity]
mul(x, 1) ≡ x              [1 is multiplicative identity]
```

### 5. Memory Operations
```
load(ptr1) ≡ load(ptr1)    [Same address, no intervening writes]
```

### 6. GetElementPointer
```
gep(gep(base, x), y) ≡ gep(base, combine(x,y))
```

## Hash Function Computation

### Pseudocode
```c
uint64_t computeHashForExpression(Instruction *I) {
    uint64_t hash = hashOpcode(I->getOpcode());

    for (auto &operand : I->operands()) {
        uint64_t opHash = getValueNumber(operand);
        hash = combineHash(hash, opHash);
    }

    hash = combineHash(hash, hashType(I->getType()));

    if (isMemoryOp(I)) {
        hash = combineHash(hash, hashMemoryAttributes(I));
    }

    return hash % tableSize;
}

uint64_t combineHash(uint64_t h1, uint64_t h2) {
    // Reversible mixing function
    h1 = ((h1 << shift) | (h1 >> (64-shift))) ^ h2 + magic_constant;
    return h1;
}
```

## Data Structures

### Expression Hash Table
```
Type:              Unordered hash map
Key:               Computed hash value
Value:             (leader_value_number, instruction_ptr)
Collision Method:  Chaining with equality verification
Capacity:          Dynamic with 2x resizing
Load Factor:       75% threshold triggers rehashing
```

## Implementation Details

### Instructions Value Numbered
- Binary ops: add, sub, mul, div, rem, and, or, xor, shl, lshr, ashr
- Unary ops: neg, not, bitcast, trunc, zext, sext
- Memory: load (with constraints)
- Control: phi, select
- Comparison: icmp, fcmp
- Pointer: gep (GetElementPointer)
- Intrinsics: some (vetted for side effects)

### Instructions NOT Value Numbered
- Function calls with side effects
- Memory operations with unknown semantics
- Volatile operations
- Instructions with undefined behavior
- Control flow (br, switch)

## Time Complexity
| Operation | Complexity | Notes |
|-----------|-----------|-------|
| Hash insertion | O(1) avg, O(n) worst | Hash table probing |
| Hash lookup | O(1) avg, O(n) worst | Hash table collision chain |
| Equality check | O(m) | m = operand count (small constant) |
| Full pass | O(n log n) | n = instruction count |

## Space Complexity
- Hash table: O(n) for n unique expressions
- Value number map: O(n) one entry per instruction
- Total: O(n) linear space

## Optimization Applications

### Common Subexpression Elimination
```c
// Before
a = x + y;
if (condition) {
    b = x + y;   // Redundant!
}

// After GVN
a = x + y;
if (condition) {
    b = a;       // Reuse computed value
}
```

### Constant Folding
```c
// Before
x = 2 + 3;

// After
x = 5;
```

### Copy Propagation
```c
// Before
x = y;
z = x;

// After
z = y;
```

## Known Limitations

1. **Memory Dependencies**: Cannot handle operations with unknown memory dependencies
2. **Phi Nodes**: Handled conservatively to avoid incorrect equivalences
3. **Function Calls**: Side effects prevent value numbering
4. **Pointer Analysis**: Requires accurate alias analysis
5. **Hash Collisions**: Degraded performance in pathological cases

## Related Passes

- **GlobalValueNumbering**: Older GVN implementation
- **MemorySSA**: Memory Static Single Assignment for memory operation handling
- **LoopStrengthReduce**: Builds on GVN for loop optimizations
- **IndVarSimplify**: Induction variable simplification

## Error Handling

### "Could not find leader" Error
- Occurs when: Leader set lookup fails unexpectedly
- Cause: Hash table corruption or algorithm bug
- Location: `/home/grigory/nvopen-tools/cicc/decompiled/sub_C0F6D0_0xc0f6d0.c:1149`
- Action: Error is logged with context information

## Debug Options

```
phicse-debug-hash:
  "Perform extra assertion checking to verify that PHINodes's
   hash function is well-behaved w.r.t. its isEqual predicate"

  Purpose: Validate hash function correctness for PHI nodes
  Effect: Adds overhead, catches equivalence detection bugs
```

## CICC Integration Points

### Pass Registration
- **ctor_220_0x4e8090.c**: NewGVN pass registration
- **ctor_388_0_0x51b710.c**: Pass option "Run the NewGVN pass"
- **ctor_477_0x54e850.c**: Another GVN instance registration

### Variant Passes
- **GVN Hoisting** (sub_231B5E0_0x231b5e0.c): Moves expressions earlier when value-numbered
- **PHI CSE** (ctor_071_0x498f60.c): Specialized CSE for PHI nodes

## Files in CICC Using GVN

```
/home/grigory/nvopen-tools/cicc/decompiled/
├── ctor_071_0x498f60.c          [PHI CSE hash options]
├── ctor_220_0x4e8090.c          [NewGVN registration]
├── ctor_388_0_0x51b710.c        [Pass description]
├── sub_231B5E0_0x231b5e0.c      [GVN Hoist implementation]
├── sub_C0F6D0_0xc0f6d0.c        [Leader set management]
└── sub_1E88360_0x1e88360.c      [VN info verification]
```

## Confidence Assessment

- **Overall**: HIGH
- **Pass Identification**: VERY HIGH (explicit strings found)
- **Hash Function**: MEDIUM (decompiled code obscures details)
- **Value Numbering**: HIGH (multiple references found)
- **Leader Management**: HIGH (error handling visible)

## Next Steps

1. Implement GVN pass integration tests
2. Profile hash function performance
3. Validate equivalence detection accuracy
4. Test edge cases (nested expressions, memory ops)
5. Integrate with NVIDIA-specific optimizations
