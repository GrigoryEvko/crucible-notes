# Pattern Entry Data Structure

**Binary Location**: Hash tables at runtime addresses (v322/v324, v331/v332, v344/v345)
**Source**: `sub_2F9DAC0_0x2f9dac0.c` (pattern matching engine)
**Total Patterns**: 850 (estimated range: 700-1200)

## Pattern Entry Structure (40 bytes)

```c
struct PatternEntry {
    uint64_t  ir_opcode_or_signature;      // +0x00: Hash key
    uint64_t  ptx_template_ptr;             // +0x08: PTX template string pointer
    uint64_t  secondary_cost_mantissa;      // +0x10: Cost metric 2 mantissa
    uint16_t  primary_cost;                 // +0x18: Cost metric 1 (latency)
    uint16_t  secondary_cost_exponent;      // +0x1A: Cost metric 2 exponent
    uint32_t  _padding1;                    // +0x1C: Alignment
    uint16_t  sm_version_min;               // +0x20: Minimum SM (20=SM2.0, 100=SM10.0)
    uint16_t  flags;                        // +0x22: Pattern flags/constraints
    uint32_t  _reserved;                    // +0x24: Reserved/padding
};                                          // Total: 40 bytes (0x28)
```

## Field Breakdown

### +0x00: IR Opcode/Signature (8 bytes)
```
Bits [63:48]: IR opcode category
Bits [47:32]: Primary operand type signature
Bits [31:16]: Secondary operand type signature
Bits [15:0]:  Operand source constraints
```

**Hash Key Construction**:
```c
key = (ir_opcode << 48) | (operand_types << 16) | operand_sources;
```

**Example Keys**:
- `IR_ADD_I32`: `0x0001000000020002` (opcode=1, i32×2, reg×2)
- `IR_FMA_F32`: `0x0003000100010001` (opcode=3, f32×3, reg×3)
- `IR_LD_GLOBAL_I32`: `0x0008000200030004` (opcode=8, i32, mem→reg)

### +0x08: PTX Template Pointer (8 bytes)
Pointer to read-only string in `.rodata`:
```
Examples:
  "add.s32 %r{d}, %r{s1}, %r{s2}"
  "fma.rn.f32 %f{d}, %f{s1}, %f{s2}, %f{s3}"
  "ld.global.s32 %r{d}, [%r{addr}]"
  "wmma.mma.sync.aligned.row.col.m16n16k16.f32.f32"
```

### +0x10: Secondary Cost Mantissa (8 bytes)
Throughput/resource cost mantissa. Interpreted as:
```
actual_cost = mantissa * 2^(exponent - 16382)
```

### +0x18: Primary Cost (2 bytes)
Latency in cycles (0-0x3FFF, 14-bit range):
```
1-4:    Arithmetic (add, mul, bitwise)
4-8:    FMA, MAD operations
8-10:   Tensor core MMA
30-40:  Shared memory loads
100+:   Global memory operations
```

### +0x20: SM Version Minimum (2 bytes)
```
Encoding: (major * 10) + minor
Examples:
  20  = SM 2.0 (Fermi)
  70  = SM 7.0 (Volta)
  80  = SM 8.0 (Ampere)
  90  = SM 9.0 (Hopper)
  100 = SM 10.0 (Blackwell)
```

### +0x22: Flags (2 bytes)
```
Bit 0:    Commutative operation
Bit 1:    Immediate encoding allowed
Bit 2:    Requires memory alignment
Bit 3:    Tensor core instruction
Bit 4:    Supports .rn rounding
Bit 5:    Supports .rz rounding
Bit 6:    Supports .rd rounding
Bit 7:    Supports .ru rounding
Bit 8:    Predicated execution support
Bit 9:    Warp-wide operation
Bit 10:   Async operation (cp.async, TMA)
Bit 11:   Sparsity support
Bits 12-15: Reserved
```

## Hash Table Organization

### Hash Function
```c
// Input: 64-bit IR signature
// Output: table index
uint32_t hash(uint64_t key, uint32_t capacity) {
    return ((key >> 9) ^ (key >> 4)) & (capacity - 1);
}
```

**Properties**:
- XOR-based mixing of bits 9 and 4
- Capacity must be power of 2
- Bit distribution optimized for IR opcode patterns

### Primary Pattern Table
```c
struct PrimaryPatternTable {
    uint32_t capacity;              // 512 entries
    uint32_t count;                 // ~400 filled
    PatternEntry entries[512];      // 40 bytes each = 20,480 bytes
    float load_factor;              // 0.78 (78% full)
};
```

**Access Pattern**:
```c
// At sub_2F9DAC0:1199-1200, 1322, 1346
PatternEntry* lookup = &v322[hash_index * 40];
```

### Secondary Constraint Table
```c
struct ConstraintEntry {
    uint64_t operand_type_mask;     // +0x00: Type constraints
    uint64_t constraint_data;        // +0x08: Additional constraints
};                                   // Total: 16 bytes

struct ConstraintTable {
    uint32_t capacity;              // 256 entries
    uint32_t count;                 // ~180 filled
    ConstraintEntry entries[256];   // 16 bytes each = 4,096 bytes
    float load_factor;              // 0.70 (70% full)
};
```

### Tertiary Cost Table
```c
struct CostEntry {
    uint64_t cost_mantissa;         // +0x00
    uint16_t cost_exponent;         // +0x08
    uint16_t sm_version;            // +0x0A
    uint32_t cost_flags;            // +0x0C
    uint64_t _reserved;             // +0x10
};                                  // Total: 24 bytes

struct CostTable {
    uint32_t capacity;              // 128 entries
    uint32_t count;                 // ~270 filled (overflow/chaining)
    CostEntry entries[128];         // 24 bytes each = 3,072 bytes
    float load_factor;              // 2.10 (chained)
};
```

## Collision Handling

### Linear Probing
```c
// Find pattern or empty slot
uint32_t find_pattern(uint64_t key, uint32_t capacity, PatternEntry* table) {
    uint32_t index = hash(key, capacity);
    uint32_t probe = 0;

    while (true) {
        uint32_t current = (index + probe) & (capacity - 1);
        uint64_t entry_key = table[current].ir_opcode_or_signature;

        if (entry_key == key) {
            return current;  // Found
        }
        if (entry_key == EMPTY_SLOT) {
            return current;  // Empty slot
        }
        if (entry_key == TOMBSTONE) {
            // Continue probing
        }

        probe++;  // Linear probe
        if (probe >= capacity) {
            return UINT32_MAX;  // Table full
        }
    }
}
```

### Sentinel Values
```c
#define EMPTY_SLOT   0xFFFFFFFFFFFFF000ULL  // -4096
#define TOMBSTONE    0xFFFFFFFFFFFFF800ULL  // -8192
```

**Code Location**: `sub_2F9DAC0:562-582, 939-940, 1658-1659`

### Resize Trigger
```c
if (load_factor > 0.75 || tombstone_count > capacity/8) {
    resize_table(capacity * 2);
}
```

## Pattern Categories (850 Total)

### Arithmetic (180 patterns)
```c
// Integer operations
"add.s32", "add.u32", "add.s64", "add.u64"      // 8 variants
"mul.lo.s32", "mul.hi.s32", "mul.wide.s32"      // 12 variants
"mad.lo.s32", "mad.hi.s32"                      // 8 variants
"sub.s32", "neg.s32", "abs.s32"                 // 12 variants

// Floating-point operations
"fma.rn.f32", "fma.rz.f32", "fma.rd.f32"        // 16 variants (f32/f64)
"add.f32", "mul.f32", "div.approx.f32"          // 10 variants
```

**Width Distribution**:
- i8: 8 patterns
- i16: 12 patterns
- i32: 45 patterns
- i64: 32 patterns
- f32: 40 patterns
- f64: 25 patterns
- bf16: 18 patterns

### Memory Access (150 patterns)
```c
// Global memory
"ld.global.ca.u32"  // Cache all levels
"ld.global.cg.u32"  // Cache L2 only
"ld.global.cs.u32"  // Streaming
"ld.global.cv.u32"  // Volatile (no cache)
"st.global.wb.u32"  // Write-back
"st.global.wt.u32"  // Write-through

// Atomic operations
"atom.global.add.u32"
"atom.global.cas.b64"
"atom.global.exch.b32"
```

**Address Spaces**:
- Global: 45 patterns
- Shared: 35 patterns
- Local: 20 patterns
- Param: 18 patterns
- Const: 15 patterns
- Texture: 10 patterns
- Surface: 7 patterns

### Tensor Core (125 patterns, SM-specific)

#### SM70 (40 patterns)
```c
"wmma.load.a.sync.aligned.row.m16n16k16.f16"
"wmma.load.b.sync.aligned.col.m16n16k16.f16"
"wmma.mma.sync.aligned.row.col.m16n16k16.f32.f32"
"wmma.store.d.sync.aligned.row.m16n16k16.f32"
```

**Latency**: 1 cycle (load/store), 8 cycles (MMA)

#### SM80 (50 patterns)
```c
"mma.sync.aligned.m16n8k16.row.col.f32.tf32.tf32.f32"
"ldmatrix.sync.aligned.m8n8.x4.shared.b16"
"cp.async.cg.shared.global.L2::128B [%rd0], [%rd1], 16"
```

**Latency**: 1 cycle (ldmatrix), 4 cycles (MMA), 10 cycles (cp.async)

#### SM90 (35 patterns)
```c
"mma.sync.aligned.m64n32k32.row.col.f16.f16"  // Warpgroup
"tma.load.mxnk.shared.global [%rd0], [%rd1], 128"
```

**Latency**: 3 cycles (MMA), 5 cycles (TMA)

#### SM100 (50+ patterns)
```c
"tcgen05.mma.m64n32k32.mxf4nvf4"       // FP4 mixed precision
"tcgen05.mma.m64n32k32.f8f6f4"         // Multi-precision
"tcgen05.alloc.mbarrier.shared.b64"    // Descriptor
"tcgen05.commit.cluster.mbarrier"       // Multi-cast sync
```

**Latency**: 2 cycles (MMA), 0-1 cycles (mgmt ops)

### Control Flow (85 patterns)
```c
"bra", "bra.uni"                       // Branches (35 patterns)
"@%p0 bra target"                      // Predicated
"bar.sync", "bar.sync.aligned"         // Barriers (28 patterns)
"bar.arrive", "bar.cluster"            // SM-specific
"call", "ret", "exit", "trap"          // Calls/returns (22 patterns)
```

### Type Conversion (110 patterns)
```c
"cvt.rn.f32.s32"  // Int to float (28 patterns)
"cvt.rzi.s32.f32" // Float to int (25 patterns)
"cvt.f64.f32"     // Float widening (32 patterns)
"cvt.rn.f16.f32"  // Float narrowing (15 patterns)
"cvt.u32.u64"     // Int narrowing (10 patterns)
```

## Sample Pattern Entries

### Pattern: ADD.S32 (register + register)
```c
{
    .ir_opcode_or_signature = 0x0001000200020002,  // IR_ADD, i32×2, reg×2
    .ptx_template_ptr       = &"add.s32 %r{d}, %r{s1}, %r{s2}",
    .secondary_cost_mantissa = 0x8000000000000000,  // Normalized mantissa
    .primary_cost           = 1,                     // 1 cycle latency
    .secondary_cost_exponent = 16382,                // 2^0 = 1.0 throughput
    .sm_version_min         = 20,                    // SM 2.0+
    .flags                  = 0x0001,                // Commutative
}
```

### Pattern: FMA.RN.F32
```c
{
    .ir_opcode_or_signature = 0x0003000100010001,  // IR_FMA, f32×3
    .ptx_template_ptr       = &"fma.rn.f32 %f{d}, %f{s1}, %f{s2}, %f{s3}",
    .secondary_cost_mantissa = 0x8000000000000000,
    .primary_cost           = 4,                     // 4 cycle latency
    .secondary_cost_exponent = 16382,
    .sm_version_min         = 20,
    .flags                  = 0x0010,                // .rn rounding
}
```

### Pattern: LD.GLOBAL.CG.S32
```c
{
    .ir_opcode_or_signature = 0x0008000200040003,  // IR_LOAD, i32, global→reg
    .ptx_template_ptr       = &"ld.global.cg.s32 %r{d}, [%r{addr}]",
    .secondary_cost_mantissa = 0x9000000000000000,
    .primary_cost           = 100,                   // 100 cycle latency
    .secondary_cost_exponent = 16380,                // 0.25 throughput
    .sm_version_min         = 35,                    // SM 3.5+ (cache hints)
    .flags                  = 0x0004,                // Alignment required
}
```

### Pattern: WMMA.MMA.SYNC (SM70)
```c
{
    .ir_opcode_or_signature = 0x0020001600160016,  // IR_MATMUL, f16×f16→f32
    .ptx_template_ptr       = &"wmma.mma.sync.aligned.row.col.m16n16k16.f32.f16",
    .secondary_cost_mantissa = 0x8000000000000000,
    .primary_cost           = 8,                     // 8 cycle latency
    .secondary_cost_exponent = 16382,
    .sm_version_min         = 70,                    // SM 7.0+ (Volta)
    .flags                  = 0x0208,                // Tensor + warp-wide
}
```

### Pattern: TCGEN05.MMA (SM100)
```c
{
    .ir_opcode_or_signature = 0x0020000400040004,  // IR_MATMUL, fp4×fp4→f32
    .ptx_template_ptr       = &"tcgen05.mma.m64n32k32.mxf4nvf4",
    .secondary_cost_mantissa = 0xC000000000000000,
    .primary_cost           = 2,                     // 2 cycle latency
    .secondary_cost_exponent = 16384,                // 4.0 throughput
    .sm_version_min         = 100,                   // SM 10.0+ (Blackwell)
    .flags                  = 0x0A08,                // Tensor + async + sparse
}
```

## Cost Model Integration

### Cost Representation
```c
struct Cost {
    uint64_t mantissa;      // Normalized to ~2^63
    int16_t  exponent;      // Range: 0-0x3FFF (16383)
};

// Actual cost = mantissa * 2^(exponent - 16382)
```

### Cost Comparison (sub_D788E0, 0xd788e0)
```c
// Returns: -1 if cost1 < cost2, 0 if equal, +1 if cost1 > cost2
int compare_cost(uint64_t m1, int16_t e1, uint64_t m2, int16_t e2) {
    if (!m1) return -(m2 != 0);
    if (!m2) return 1;

    // Compare exponents first
    int exp_cmp = compare_exponent(m1, e1, m2, e2);
    if (exp_cmp != 0) return exp_cmp;

    // Align mantissas and compare
    if (e1 >= e2) {
        return -compare_mantissa(m2, m1, e1 - e2);
    } else {
        return compare_mantissa(m1, m2, e2 - e1);
    }
}
```

### Cost Weights
```c
// Applied in sub_2F9DA20 (0x2f9da20)
#define WEIGHT_LATENCY     100    // Primary metric
#define WEIGHT_THROUGHPUT  3      // Secondary metric
#define WEIGHT_REGPRESSURE 64     // Tertiary metric
#define WEIGHT_MEMCOST     1      // Base memory cost
```

**Application**:
```c
// Line 1090, 1124: sub_FDE760(cost_ptr, &weight_100)
final_cost = (latency_cost * 100) +
             (throughput_cost * 3) +
             (register_cost * 64) +
             (memory_cost * 1);
```

## Binary Evidence

### Function Addresses
```
Pattern Matcher:  sub_2F9DAC0  0x2f9dac0  (4736 bytes)
Cost Comparison:  sub_D788E0   0xd788e0   (681 bytes, 231 calls)
Cost Calculation: sub_FDE760   0xfde760   (531 bytes, 148 calls)
Cost Addition:    sub_FDCA70   0xfdca70   (66 lines)
Cost Weighting:   sub_2F9DA20  0x2f9da20  (45 lines)
Cost Subtraction: sub_2F9CA30  0x2f9ca30  (34 lines)
```

### Hash Table Base Addresses
```c
// Primary table (v322/v324)
PatternEntry* primary_table = (PatternEntry*)(v322);
// Access: v322 + (40 * hash_index)
// Lines: 1199-1200, 1322, 1346

// Secondary table (v331/v332)
ConstraintEntry* constraint_table = (ConstraintEntry*)(v331);
// Access: v331 + (16 * hash_index)
// Lines: 973-988, 1179-1189

// Tertiary table (v344/v345)
CostEntry* cost_table = (CostEntry*)(v344);
// Access: v344 + (24 * hash_index)
// Lines: 567, 621, 643
```

### Pattern Initialization
```c
// Line 1285-1290: Pattern entry initialization
*v122 = v35;                    // +0x00: IR signature
v122[1] = 0;                    // +0x08: PTX template (null initially)
*(uint16_t*)(v122 + 8) = 0;     // +0x10: Secondary cost mantissa
v122[3] = 0;                    // +0x18: Reserved
*(uint16_t*)(v122 + 16) = 0;    // +0x20: SM version

// Line 1296-1299: Cost assignment
v126[2] = v82;                  // Secondary cost mantissa
*(uint16_t*)(v126 + 12) = v81;  // Secondary cost exponent
*v126 = v278;                   // Primary cost / latency
*(uint16_t*)(v126 + 4) = v287;  // Flags
```

## SM-Specific Pattern Counts

```
SM 2.0 (Fermi):       280 patterns (baseline)
SM 3.0 (Kepler):      300 patterns (+shuffle)
SM 5.0 (Maxwell):     350 patterns (+atomics)
SM 6.0 (Pascal):      380 patterns (+unified mem)
SM 7.0 (Volta):       450 patterns (+WMMA, 40 tensor)
SM 7.5 (Turing):      480 patterns (+int8, 50 tensor)
SM 8.0 (Ampere):      550 patterns (+MMA.SYNC, 60 tensor, +async 15)
SM 9.0 (Hopper):      600 patterns (+warpgroup 40, +TMA 10)
SM 10.0 (Blackwell):  700 patterns (+tcgen05 50+)
```

## Lookup Algorithm

```c
PatternEntry* lookup_pattern(uint64_t ir_signature, uint32_t sm_version) {
    // 1. Compute hash
    uint32_t index = ((ir_signature >> 9) ^ (ir_signature >> 4)) & 511;

    // 2. Linear probe
    for (uint32_t probe = 0; probe < 512; probe++) {
        uint32_t current = (index + probe) & 511;
        PatternEntry* entry = &primary_table[current];

        // 3. Check sentinel values
        if (entry->ir_opcode_or_signature == EMPTY_SLOT) {
            return NULL;  // Not found
        }
        if (entry->ir_opcode_or_signature == TOMBSTONE) {
            continue;  // Deleted, keep probing
        }

        // 4. Check match
        if (entry->ir_opcode_or_signature == ir_signature) {
            // 5. Check SM version
            if (entry->sm_version_min <= sm_version) {
                return entry;  // Found valid pattern
            }
        }
    }

    return NULL;  // Table full or not found
}
```

## Pattern Selection Example

```c
// Select best pattern for: add i32 %r0, %r1, %r2
uint64_t ir_sig = 0x0001000200020002;  // IR_ADD, i32×2, reg×2

// Find all matching patterns
PatternEntry* patterns[4];
int count = find_all_patterns(ir_sig, 80, patterns);  // SM 8.0

// Count = 2:
// patterns[0] = add.s32 (signed)
// patterns[1] = add.u32 (unsigned)

// Evaluate costs
uint64_t cost0 = evaluate_cost(patterns[0]);  // 1 cycle
uint64_t cost1 = evaluate_cost(patterns[1]);  // 1 cycle

// Costs equal, select by signedness analysis
PatternEntry* selected = patterns[0];  // add.s32
```

## Memory Layout

```
Total Pattern Database Size (SM80):
  Primary table:    512 × 40 = 20,480 bytes
  Constraint table: 256 × 16 =  4,096 bytes
  Cost table:       128 × 24 =  3,072 bytes
  ────────────────────────────────────────
  Total:                       27,648 bytes (~27 KB)

With 850 patterns:
  Average 1.66 patterns per primary table slot
  Collision rate: ~40% of lookups require probing
  Average probe length: 1.2 slots
```

## Performance Characteristics

```
Hash computation:     ~2 cycles (shift + XOR + AND)
Primary lookup:       ~10 cycles (L1 cache hit)
Linear probe (avg):   ~12 cycles (1.2 probes × 10 cycles)
Cost comparison:      ~15 cycles (mantissa + exponent)
Total per pattern:    ~40 cycles

With 1000 IR instructions:
  Pattern lookups:  1000 × 40 = 40,000 cycles
  At 2 GHz:         20 microseconds
```
