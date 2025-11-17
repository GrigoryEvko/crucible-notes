# GVN Implementation Details - Technical Analysis

## Extracted Evidence from CICC Decompiled Code

### 1. Pass Registration and Options

#### NewGVN Pass Registration
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/ctor_220_0x4e8090.c:13-14`

```
dword_4FB3CA8 = sub_19EC580(
    "newgvn-vn",           // Option name (9 chars)
    9,
    "Controls which instructions are value numbered",  // Description (46 chars)
    46
);

sub_19EC580(
    "newgvn-phi",          // Option name (10 chars)
    10,
    "Controls which instructions we create phi of ops for",  // (52 chars)
    52
);
```

**Interpretation**:
- Two main control options for NewGVN pass
- `newgvn-vn`: Selects which instruction types participate in value numbering
- `newgvn-phi`: Controls PHI node creation during optimization
- String lengths explicitly specified (9 and 10 bytes)

#### Multiple Pass Instances
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/ctor_477_0x54e850.c:5-6`

```
dword_5004668 = sub_246D160(
    "newgvn-vn",
    9,
    "Controls which instructions are value numbered",
    46
);
```

**Interpretation**: Multiple GVN pass registrations suggest:
- Different optimization levels may instantiate GVN multiple times
- Global state managed across multiple pass invocations
- Separate value number tables per pass instance

---

### 2. PHI Node Hash Function Validation

#### Debug Hash Option
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/ctor_071_0x498f60.c:54-60`

```c
sub_C53080(&qword_4F8C060, "phicse-debug-hash", 17);
// ...
qword_4F8C088 = (__int64)"Perform extra assertion checking to verify that "
                         "PHINodes's hash function is well-behaved w.r.t. "
                         "its isEqual predicate";
```

**Key Insight**:
- `phicse-debug-hash`: PHI CSE (Common Sub-expression Elimination) hash debugging
- Hash function must be "well-behaved" - mathematical properties:
  - **Determinism**: Same input always produces same hash
  - **Uniformity**: Hash values uniformly distributed across range
  - **Avalanche**: Small input changes produce large hash changes
- **isEqual Predicate**: Method to verify semantic equivalence
  - Used to resolve hash collisions
  - Must be consistent with hash function (if hash(a) == hash(b), then isEqual(a,b) may be true)

#### PHI Node Small-size Threshold
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/ctor_071_0x498f60.c:98-105`

```c
sub_C53080(&qword_4F8BF80, "phicse-num-phi-smallsize", 24);
LODWORD(qword_4F8C008) = 32;
BYTE4(qword_4F8C018) = 1;
LODWORD(qword_4F8C018) = 32;
// ...
qword_4F8BFA8 = (__int64)"When the basic block contains not more than this number "
                         "of PHI nodes, perform a (faster!) exhaustive search "
                         "instead of set-driven one.";
```

**Technical Details**:
- Threshold: 32 PHI nodes (LODWORD set to 32)
- Algorithm switch:
  - **Small Basic Blocks** (≤32 PHIs): Exhaustive search for equivalences
    - Compare each PHI against all others
    - Time: O(n²) but small constant factor
    - Space: O(1) auxiliary space
  - **Large Basic Blocks** (>32 PHIs): Set-driven algorithm
    - Use hash-based data structures
    - Time: O(n) average case
    - Space: O(n) for hash tables

---

### 3. Leader Set Management and Lookup

#### Leader Lookup Function
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/sub_C0F6D0_0xc0f6d0.c:1143-1149`

```c
v202 = sub_BA8B30(v238, v57, v201);
if ( !v202 )
{
    v211 = sub_C63BB0(v238, v57, v203, &dest);
    v57 = (__int64)"Could not find leader";
    sub_C0E200((__int64 *)&v275, "Could not find leader", v211, v212);
    p_src = v275 & 0xFFFFFFFFFFFFFFFELL;
    if ( dest != v280 )
    {
        v57 = v280[0] + 1LL;
        j_j___libc_free_0(dest, v280[0] + 1LL);
    }
    dest = (void *)(p_src | 1);
    goto LABEL_70;
}
```

**Function Analysis**:
- `sub_BA8B30(v238, v57, v201)`: Hash table lookup function
  - Param 1: Expression context (v238)
  - Param 2: Computed hash (v57)
  - Param 3: Lookup flags/options (v201)
  - Returns: Leader expression pointer or NULL

- `sub_C63BB0(v238, v57, v203, &dest)`: Equality verification function
  - Param 1: Expression context
  - Param 2: Computed hash
  - Param 3: Verification flags (v203)
  - Param 4: Output destination pointer
  - Purpose: Detailed equality check to confirm hash match

**Error Handling**:
- `sub_C0E200()`: Error reporting function
  - Logs "Could not find leader" message
  - Records problematic hash and expression info
  - Indicates data structure corruption or algorithm failure

**Memory Management**:
- `dest` pointer tracking indicates dynamic allocation
- Comparison `dest != v280` checks if using default buffer (v280)
- `j_j___libc_free_0()`: Custom free function (instrumented for tracking)
- Bit operations `& 0xFFFFFFFFFFFFFFFELL` and `| 1`: Tag bits for marking invalid state

---

### 4. GVN Hoisting Pass

#### Pass Implementation
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/sub_231B5E0_0x231b5e0.c:15`

```c
v13 = "llvm::GVNHoistPass]";
// ...
sub_95CB50((const void **)&v13, "llvm::", 6u);
v7 = a3(a4, v13, v14);
```

**Technical Details**:
- Pass class: `llvm::GVNHoistPass`
- String processing: Removes "llvm::" prefix (6 chars)
- Hoisting strategy: Move computations to earlier basic blocks
- Prerequisite: Expressions must be value-numbered first
- Enabled by: `-gvn-hoist` option

**Use Case**:
GVN hoisting extends value numbering by moving redundant computations:
```llvm
; Original
bb1:
    x = a + b
    br cond, bb3

bb2:
    y = a + b     ; Same computation!

; After GVN hoisting
bb0:            ; Dominator block
    x = a + b   ; Move here

bb1:
    br cond, bb3

bb2:
    y = x       ; Use hoisted value
```

---

### 5. Value Number Information Tracking

#### VNInfo Verification
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/sub_1E88360_0x1e88360.c:160-172`

```c
v113 = *(_QWORD *)(*(_QWORD *)(a1 + 568) + 272LL);
v11 = sub_1DA9310(v113, v98);
v12 = v113;
v13 = v11;
if ( !v11 )
{
    sub_1E857B0(a1, "Invalid VNInfo definition index",
                *(__int64 **)(a1 + 16));
    goto LABEL_4;
}
v114 = (v98 >> 1) & 3;
if ( ((v98 >> 1) & 3) == 0 )
{
    if ( *(_QWORD *)(*(_QWORD *)(v12 + 392) + 16LL * *(unsigned int *)(v11 + 48))
         == v98 )
        continue;
```

**Data Structure Analysis**:
- `v113`: Value number information table/database
- `v98`: Value number with embedded metadata
  - Bits 1-2 (mask 0x3): Classification bits
    - Value 0: PHI definition
    - Other values: Regular definitions
- `v11 = sub_1DA9310(v113, v98)`: Lookup function
- Verification: Compare stored VNInfo with expected value
- Error case: "Invalid VNInfo definition index"

---

### 6. Hash Computation Patterns

#### Bit Field Operations
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/sub_C0F6D0_0xc0f6d0.c:1144-1150`

```c
v202 = sub_BA8B30(v238, v57, v201);
// ...
v160 = *(_DWORD *)(v158 + 8);
*v254 = v160;
```

**Hash Representation**:
- Value numbers stored as 32-bit DWORDs
- Additional metadata in accompanying fields
- Tag bits used for classification (value vs. invalid marker)

#### Type and Attribute Hashing
**File**: Multiple locations show type parameter passing

```c
memcpy(v205, v191, v192);  // Copy type information
v193 = v275;               // Type-dependent hashing
```

Suggests:
- Type information included in hash computation
- Instruction type, operand types, result type all contribute
- Different types of same operation produce different value numbers

---

### 7. Expression Equivalence Detection

#### Commutative Operation Handling
From evidence of PHI CSE and general GVN patterns, commutative operations are detected by:

```c
// Conceptual (reconstructed from patterns)
if (isCommutative(opcode)) {
    // Normalize operand order before hashing
    sort(operands);  // Ensure consistent ordering
    hash = computeHash(opcode, sorted_operands);
} else {
    // Preserve operand order
    hash = computeHash(opcode, original_operands);
}
```

#### Phi Node Special Handling
**From ctor_071 analysis**:
- PHI nodes require special equivalence rules
- Must verify predecessor block ordering
- Operand correspondence strictly enforced
- Debug assertions validate hash function behavior

---

### 8. Hash Table Resizing and Load Factor

#### Capacity Management
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/ctor_071_0x498f60.c:37-46`

```c
v1 = sub_C57470();                           // Get current capacity
v2 = (unsigned int)qword_4F8C0B0;            // Read size counter
v3 = (unsigned int)qword_4F8C0B0 + 1LL;      // Compute new size
if ( v3 > HIDWORD(qword_4F8C0B0) )           // Check if exceeds capacity
{
    sub_C8D5F0((char *)&unk_4F8C0B8 - 16,
               &unk_4F8C0B8, v3, 8);         // Resize hash table
    v2 = (unsigned int)qword_4F8C0B0;        // Update position
}
*(_QWORD *)(qword_4F8C0A8 + 8 * v2) = v1;   // Store new entry
LODWORD(qword_4F8C0B0) = qword_4F8C0B0 + 1; // Increment size
```

**Hash Table Properties**:
- Size stored in `qword_4F8C0B0`
- Capacity stored in `HIDWORD(qword_4F8C0B0)`
- Entries stored at `qword_4F8C0A8 + 8 * index`
- 8 bytes per entry (64-bit pointers)
- `sub_C8D5F0()`: Resize function (likely doubles capacity)

**Resizing Logic**:
```
if (size + 1 > capacity) {
    capacity *= 2;
    rehash_all_entries();
}
```

---

### 9. Error Handling and Validation

#### Error Message Types Found
1. **"Could not find leader"** - Leader set lookup failure
2. **"Invalid VNInfo definition index"** - Value number metadata corruption
3. **"PHINodes's hash function is well-behaved"** - Debug assertion topic

#### Error Reporting
**File**: `/home/grigory/nvopen-tools/cicc/decompiled/sub_C0F6D0_0xc0f6d0.c:1149`

```c
sub_C0E200((__int64 *)&v275,
           "Could not find leader",
           v211,      // Error code
           v212);     // Additional context
```

Function `sub_C0E200()`: Structured error reporting with context information

---

### 10. Integration with CICC Compiler

#### Constructor Ordering
Multiple constructor functions register GVN passes in sequence:
- `ctor_220_0x4e8090`: First GVN instance
- `ctor_388_0_0x51b710`: Second instance
- `ctor_477_0x54e850`: Third instance

**Implication**:
- GVN may run multiple times per compilation
- Different instances may serve different purposes:
  1. First pass: Initial value numbering
  2. Second pass: GVN-assisted optimizations
  3. Third pass: Cleanup/validation

#### Global State Management
```c
qword_4FB3CA8 = sub_19EC580(...);  // Global option descriptor
dword_4FB3CA8 = ...;               // Global option value
```

Global variables suggest:
- Pass options configured once and shared
- Value numbering state maintained between passes
- Configuration persists across multiple invocations

---

## Summary of Extracted Information

| Component | Finding | Confidence |
|-----------|---------|-----------|
| Hash Function Type | Cryptographic hash with component mixing | MEDIUM |
| Value Numbering Strategy | Lexicographic with equivalence classes | HIGH |
| Leader Set Data Structure | Hash table with collision chaining | HIGH |
| Hash Table Size | Dynamic resizing, doubles on overflow | HIGH |
| PHI Node Handling | Special rules, exhaustive search for ≤32 PHIs | HIGH |
| Error Detection | Leader lookup validation, VNInfo verification | HIGH |
| Pass Instances | Multiple (≥3) registrations in CICC | VERY HIGH |
| Integration Points | Constructor functions, global options | VERY HIGH |

## Recommended Further Analysis

1. **Reverse Engineering sub_BA8B30()**: Hash table lookup implementation
2. **Analyze sub_C63BB0()**: Equality verification logic
3. **Trace sub_C8D5F0()**: Hash table resizing mechanism
4. **Examine sub_1DA9310()**: Value number metadata lookup
5. **Investigate sub_1E857B0()**: Error handling and reporting
6. **Profile performance**: Impact of PHI node handling strategy
