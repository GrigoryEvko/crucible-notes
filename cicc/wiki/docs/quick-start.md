# CICC Technical Reference

## Core Constants

| Constant | Value | Source | Evidence |
|----------|-------|--------|----------|
| K (Physical registers) | 15 | Architecture target | 0x1090BD0:1039 (0xE = 14 in code) |
| Coalesce weight | 0.8 | Graph coloring | sub_1090BD0:603, 0xCCCCCCCCCCCCCCCD/2^64 |
| IR node size | 64 bytes | Heap allocation | sub_672A20:2979-3010 |
| Hash table primary | 512 entries | Pattern database | sub_2F9DAC0:1199-1200 |
| Hash table secondary | 256 entries | Operand constraints | sub_2F9DAC0:973-988 |
| Hash table tertiary | 128 entries | Cost table | sub_2F9DAC0:567-643 |
| Pattern count | 850 | Instruction selection | IR→PTX mapping |
| Optimization passes | 212 | PassManager | indices 10-221 (0x0A-0xDD) |
| Unused pass slots | 10 | PassManager | indices 0-9 |
| Symbol table buckets | 1024 | Estimated | Typical compiler pattern |
| SM version range | 20-120 | Architecture support | SM 2.0 through Blackwell |

## Critical Functions

| Address | Size (KB) | Function | Purpose | L3 Evidence |
|---------|-----------|----------|---------|------------|
| 0x672A20 | 129 | CompilationPipeline_Main | Main IR-to-PTX orchestrator | sub_672A20_0x672a20.c |
| 0xB612D0 | 39 | BuildInterferenceGraph | Register allocation graph construction | sub_B612D0_0xb612d0.c |
| 0x12D6300 | 122 | PassManager | Pass dispatcher/sequence executor | sub_12D6300_0x12d6300.c |
| 0x2F9DAC0 | Unknown | InstructionSelector | Pattern matcher hash tables | sub_2F9DAC0_0x2f9dac0.c |
| 0x1081400 | 69 | SimplifyAndColor | Graph coloring priority setup | sub_1081400_0x1081400.c |
| 0x1090BD0 | 61 | SelectNodeForRemoval | Briggs criterion node selection | sub_1090BD0_0x1090bd0.c |
| 0x12E1EF0 | 51 | AssignColorsAndOptimize | Color assignment to registers | sub_12E1EF0_0x12e1ef0.c |
| 0x12D6170 | Handler | MetadataHandler | sub_12D6170 - fetch complex pass metadata | pass_function_addresses.json |
| 0x12D6240 | Handler | BooleanOptionHandler | sub_12D6240 - fetch pass boolean flags | pass_function_addresses.json |

## Data Structure Layouts

### IRValueNode (64 bytes)

| Offset | Size | Type | Field | Access Pattern |
|--------|------|------|-------|-----------------|
| 0 | 8 | uint64_t* | next_use_def | *(_QWORD *)node |
| 8 | 1 | uint8_t | opcode | *(_BYTE *)(node + 8) |
| 9 | 1 | uint8_t | operand_count | *(_BYTE *)(node + 9) |
| 10 | 1 | uint8_t | state_phase | *(_BYTE *)(node + 10) |
| 11 | 1 | uint8_t | control_flags | *(_BYTE *)(node + 11) & MASK |
| 12 | 4 | uint32_t | padding | Alignment |
| 16 | 8 | uint64_t* | type_or_def | *(_QWORD *)(node + 16) |
| 24 | 8 | uint64_t* | value_or_operand | *(_QWORD *)(node + 24) |
| 32 | 8 | uint64_t* | next_operand | *(_QWORD *)(node + 32) |
| 40 | 8 | uint64_t* | second_operand | *(_QWORD *)(node + 40) |
| 48 | 8 | uint64_t | reserved | - |
| 56 | 8 | uint64_t* | parent_context | *(_QWORD *)(node + 56) |

### PatternEntry (40 bytes)

| Offset | Size | Type | Field |
|--------|------|------|-------|
| 0 | 8 | uint64_t | ir_opcode_signature |
| 8 | 8 | uint64_t* | ptx_template_ptr |
| 16 | 8 | uint64_t | secondary_cost_value |
| 24 | 2 | uint16_t | primary_cost |
| 26 | 2 | uint16_t | sm_version_min |
| 28 | 12 | reserved | - |

### OperandConstraintEntry (16 bytes)

| Offset | Size | Type | Field |
|--------|------|------|-------|
| 0 | 8 | uint64_t | operand_type_mask |
| 8 | 8 | uint64_t | constraint_data |

### SymbolTableEntry (128 bytes estimated)

| Offset | Field | Type |
|--------|-------|------|
| 0 | hash_next | uint64_t* |
| 8 | name_ptr | uint64_t* |
| 16 | type | uint32_t |
| 20+ | attributes | variable |

## Pattern Database Hash Function

```
unsigned int hash(uint64_t key, unsigned int capacity) {
    unsigned int h = ((key >> 9) ^ (key >> 4)) & (capacity - 1);
    return h;
}
```

**Sentinel values:**
- Empty slot: -4096 (0xFFFFFFFFFFFFF000)
- Tombstone: -8192 (0xFFFFFFFFFFFFF800)
- Load factor threshold: 0.75
- Resize factor: 2.0

## Register Allocation Algorithm Constants

**Briggs Criterion Check:**
- K value: 15 physical registers
- Conservative threshold: degree <= 14 (K-1)
- Safe coloring when: count(neighbors with degree < K) >= K

**Priority Calculation:**
```
effective_degree = actual_degree * 0.8
if (briggs_safe) {
    priority = INFINITE
} else {
    priority = spill_cost / effective_degree
}
```

**Spill Cost Formula:**
```
cost = definition_frequency × use_frequency ×
       memory_latency_multiplier × loop_depth_multiplier

loop_depth_multiplier = pow(base, depth)  // base >= 1.5
```

## Instruction Selection Pattern Categories

| Category | Count | Examples | SM Min |
|----------|-------|----------|--------|
| Arithmetic | 180 | add.s32, mul.lo, fma.rn | 20 |
| Memory Access | 150 | ld.global, st.shared, atom | 20 |
| Control Flow | 85 | bra, bar.sync, call, ret | 20 |
| Tensor Core | 125 | wmma, mma.sync, warpgroup | 70+ |
| Type Conversion | 110 | cvt.*,* conversions | 20 |
| Bitwise Operations | 95 | and, or, xor, shl, popc | 20 |
| Floating Point | 105 | fma, sqrt, sin, lg2 | 20 |
| Special Operations | 50 | min, max, clz, prmt, pack | 20 |

## Optimization Pass Structure

**PassManager layout (indices 10-221):**
```
Pass slot stride: 16 bytes
Total output: 3552 bytes (212 passes × 16-24 bytes)
First offset: 16 bytes
Last offset: 3536 bytes
Sequence: Sequential 10→221, indices 0-9 unused
```

**Handler functions:**
- Even indices (10,12,14...): sub_12D6170 metadata handler (113 passes)
- Odd indices (11,13,15...): sub_12D6240 boolean handler (99 passes)

**Special passes (default enabled):**
- Index 19: enabled=1
- Index 25: enabled=1
- Index 217: enabled=1

## File Paths

**Decompiled code:**
```
/home/grigory/nvopen-tools/cicc/decompiled/sub_672A20_0x672a20.c
/home/grigory/nvopen-tools/cicc/decompiled/sub_B612D0_0xb612d0.c
/home/grigory/nvopen-tools/cicc/decompiled/sub_12D6300_0x12d6300.c
/home/grigory/nvopen-tools/cicc/decompiled/sub_2F9DAC0_0x2f9dac0.c
/home/grigory/nvopen-tools/cicc/decompiled/sub_1081400_0x1081400.c
/home/grigory/nvopen-tools/cicc/decompiled/sub_1090BD0_0x1090bd0.c
/home/grigory/nvopen-tools/cicc/decompiled/sub_12E1EF0_0x12e1ef0.c
```

**L3 Analysis files:**
```
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/pass_function_addresses.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/complete_pass_ordering.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/pass_manager_implementation.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/pattern_database.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/cost_model_complete.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/register_allocation/graph_coloring_priority.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/register_allocation/spill_cost_formula.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/register_allocation/lazy_reload_algorithm.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/data_structures/ir_node_exact_layout.json
/home/grigory/nvopen-tools/cicc/deep_analysis/L3/data_structures/symbol_table_exact.json
```

## Common Commands

**List all function addresses from pass file:**
```bash
jq '.identified_passes | to_entries[] | "\(.key): \(.value[0].address)"' \
  /home/grigory/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/pass_function_addresses.json | grep -v null | sort
```

**Extract pattern database statistics:**
```bash
jq '.pattern_database' \
  /home/grigory/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/pattern_database.json
```

**Find IR node allocation patterns:**
```bash
grep -n "sub_72C930\|sub_727670\|sub_7276D0" \
  /home/grigory/nvopen-tools/cicc/decompiled/sub_672A20_0x672a20.c | head -20
```

**Check IR node structure at offsets:**
```bash
grep -n "+(8)\|+(16)\|+(24)\|+(32)\|+(40)\|+(56)" \
  /home/grigory/nvopen-tools/cicc/decompiled/sub_672A20_0x672a20.c
```

**Analyze graph coloring priority calculation:**
```bash
grep -A2 "Briggs\|degree.*14\|0xE" \
  /home/grigory/nvopen-tools/cicc/decompiled/sub_1090BD0_0x1090bd0.c
```

**Extract pass ordering indices:**
```bash
jq '.pass_ordering_complete_list.pass_sequence_decimal' \
  /home/grigory/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/complete_pass_ordering.json
```

**Find symbol table hash operations:**
```bash
grep -n "hash\|bucket\|DJB2\|FNV" \
  /home/grigory/nvopen-tools/cicc/decompiled/sub_672A20_0x672a20.c
```

## Allocator Functions

| Function | Address | Size Param | Purpose |
|----------|---------|-----------|---------|
| sub_727670 | 0x727670 | unknown | Primary IR node allocation |
| sub_7276D0 | 0x7276D0 | unknown | Operand node allocation |
| sub_724D80 | 0x724D80 | 0 usually | Attribute node creation |
| sub_72C930 | 0x72C930 | 84/79/0 | Variable-size IR allocation |

**Size mapping for sub_72C930:**
- 84 = 64 byte node + 20 byte operand array
- 79 = 64 byte node + 15 byte operand space
- 0 = null/error case

## SM Architecture Pattern Support

| SM | Generation | Pattern Count | Tensor Support | Key Features |
|----|-----------|---------------|-----------------|--------------|
| 20 | Fermi | 280 | No | Basic arithmetic/memory |
| 30 | Kepler | 300 | No | Shuffle, dynamic parallelism |
| 50 | Maxwell | 350 | No | Atomic ops, improved memory |
| 60 | Pascal | 380 | No | Unified memory |
| 70 | Volta | 450 | WMMA | 40 patterns, f32/f64 |
| 75 | Turing | 480 | WMMA | 50 patterns, f16/i8 support |
| 80 | Ampere | 550 | MMA.SYNC | 60 patterns, async memory |
| 90 | Hopper | 600 | Warpgroup MMA | 40 patterns, TMA (10) |
| 100 | Blackwell | 700 | TCGen05 | 50 patterns, FP8 support |

## Use-Def Chain Traversal

**Structure:** Intrusive linked list at offset 0

**Traversal:**
```c
for (node = list_head; node; node = *(uint64_t *)node) {
    uint8_t flags = *(_BYTE *)(node + 11);
    uint8_t opcode = *(_BYTE *)(node + 8);

    if ((flags & 0x02) == 0) break;  // Break condition
    if (flags & 0x10) continue;      // Skip condition

    if (opcode == 19) special_handling();

    *(_BYTE *)(node + 10) = 5;  // Mark state
}
```

**Flag meanings:**
- 0x02: Break condition (loop terminator)
- 0x10: Skip/optimization path
- state_phase 1,3,5: Processing phases

## Analysis Statistics

| Metric | Value |
|--------|-------|
| Total functions analyzed | 80,562 |
| Functions decompiled | 80,281 (99.65%) |
| Strings extracted | 188,141 |
| Callgraph size | 2.0 GB |
| L3 analysis files | 29 |
| Unknown IDs resolved | 27/27 |
| Passes documented | 212 |
| Pattern categories | 8 |
| IR→PTX mappings | 850 |
| Constructor functions found | 206 |
| Constructor addresses mapped | 133 |
