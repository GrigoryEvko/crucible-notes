# Pass Manager Data Structures

## PASS DESCRIPTOR

```c
struct PassDescriptor {
    void*       pass_fn;        // +0x00: Pass function pointer (QWORD)
    uint32_t    pass_count;     // +0x08: Pass instance count (DWORD)
    uint32_t    opt_level;      // +0x0C: Optimization level (0-3)
    uint32_t    flags;          // +0x10: Analysis/Transform flags
    uint32_t    reserved;       // +0x14: Padding
};  // Size: 24 bytes per entry
```

## PASS REGISTRY ENTRY

```c
struct PassRegistryEntry {
    uint8_t     metadata[16];   // +0x00: Pass metadata/IDs
    void*       pass_object;    // +0x10: Pointer to Pass instance
    uint8_t     state[16];      // +0x20: Pass flags/state
    void*       analysis_reqs;  // +0x30: Analysis requirements (offset +40)
    void**      fn_array;       // +0x38: Function pointer array (offset +48)
    uint32_t    array_flag;     // +0x40: Array presence flag (offset +56)
    uint8_t     padding[4];     // +0x44: Alignment
};  // Size: 64 bytes per entry, stride: 64
```

## PASS MANAGER STRUCTURE

```c
struct PassManager {
    uint32_t    opt_level;      // +0x00: Optimization level (from a2+112)
    uint32_t    padding1;       // +0x04
    void*       config_ptr;     // +0x08: Pointer to PassManagerConfig
    PassDescriptor passes[212]; // +0x10: Array of 212 passes
};  // Total size: 5104 bytes (16 + 212*24)
```

## PASS MANAGER CONFIG

```c
struct PassManagerConfig {
    uint8_t     header[112];        // +0x00: Unknown header fields
    uint32_t    optimization_level; // +0x70: O0/O1/O2/O3
    uint32_t    padding;            // +0x74
    void*       pass_registry;      // +0x78: PassRegistryEntry* (offset +120)
};  // Minimum size: 128 bytes
```

## BINARY LAYOUT

### Pass Manager Function
- **Address**: `0x12D6300`
- **Size**: 4786 bytes (0x12AB)
- **Range**: `0x12D6300 - 0x12D6B9A`
- **Decompiled Size**: 122 KB

### Handler Functions
- **Metadata Handler**: `0x12D6170` (113 passes, even indices)
- **Boolean Handler**: `0x12D6240` (99 passes, odd indices)
- **Store Helper**: `0x12D6090` (pass metadata storage)
- **Registry Lookup**: `0x1691920` (64-byte stride indexing)
- **Registry Search**: `0x168FA50` (pass ID search)
- **Pass ID Match**: `0x1690410` (ID verification)

### Pass Registry
- **Base**: `PassManagerConfig + 120` (a2+120)
- **Entry Size**: 64 bytes
- **Total Slots**: 222 (indices 0-221)
- **Active Slots**: 212 (indices 10-221)
- **Unused Slots**: 10 (indices 0-9)
- **Access Pattern**: `base + ((index - 1) << 6)`

## PASS HIERARCHY

### Module Passes (Indices 10-50, ~41 passes)
- **Scope**: Entire compilation unit
- **Method**: `runOnModule(Module&)`
- **Frequency**: Once per module
- **Examples**: GlobalOpt, Internalization, DeadArgumentElim

### Function Passes (Indices 50-200, ~139 passes)
- **Scope**: Individual functions
- **Method**: `runOnFunction(Function&)`
- **Frequency**: Once per function
- **Examples**: InstCombine, SimplifyCFG, DSE, GVN, JumpThreading

### Loop Passes (Indices 160-180, ~21 passes)
- **Scope**: Individual loops
- **Method**: `runOnLoop(Loop&)`
- **Frequency**: Once per loop
- **Examples**: LICM, LoopUnroll, LoopVersioning, LoopVectorize

### Backend Passes (Indices 210-221, ~12 passes)
- **Scope**: Code generation
- **Method**: `runOnMachineFunction()`
- **Frequency**: Once per function (backend)
- **Examples**: Vectorization, CodeGenPrepare, PostRA

## PASS REGISTRATION

### Static Registration
- **Constructor Count**: 206 files (`ctor_*.c`)
- **Pattern**: `RegisterPass<T>` template instantiation
- **Timing**: Compile-time static initialization
- **Registry**: Singleton PassRegistry with lazy instantiation

### Pass ID Assignment
- **Range**: 10-221 (0x0A-0xDD)
- **Type**: `uint32_t`
- **Unused**: 0-9 (reserved/unused)
- **Total Active**: 212

### Handler Distribution
- **Even Indices (113)**: Metadata handler (sub_12D6170)
- **Odd Indices (99)**: Boolean handler (sub_12D6240)
- **Total**: 212 passes

## EXECUTION ENGINE

### Initialization Phase
```c
// PassManager constructor (0x12D6300)
v5 = *(_DWORD *)(a2 + 112);           // Read opt level
*(_QWORD *)(a1 + 8) = a2;             // Store config ptr
*(_DWORD *)a1 = v5;                    // Store opt level
```

### Pass Iteration
```c
// Sequential pass processing (unrolled loop)
for (index = 10; index <= 221; index++) {
    if (index % 2 == 0) {
        // Even: metadata handler
        metadata = sub_12D6170(a2 + 120, index);
        if (metadata) {
            fn = **(_QWORD **)(metadata + 48);
            count = *(_DWORD *)(metadata + 40);
        }
    } else {
        // Odd: boolean handler
        result = sub_12D6240(a2, index, "0");
        enabled = (uint32_t)result;
        count = (uint32_t)(result >> 32);
    }
    // Store at a1 + 16 + (index-10)*24
    sub_12D6090(output_ptr, fn, count, analysis, opt_level);
}
```

### Analysis Invalidation
```c
// Line 1674 in sub_12D6300
v50 = *(_BYTE *)(v48 + 36) == 0;  // Check preservation flag
// If true: analyses invalidated
// If false: analyses preserved
```

## PASS TABLE (212 Entries)

| ID  | Name                      | Address    | Level    | Type      | Handler |
|-----|---------------------------|------------|----------|-----------|---------|
| 10  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 11  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 12  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 13  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 14  | OndemandMdsLoading        | -          | MODULE   | ANALYSIS  | Meta    |
| 15  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 16  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 17  | AutoUpgradeDebugInfo      | -          | MODULE   | TRANSFORM | Bool    |
| 18  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 19  | (Transform) [DEFAULT=1]   | -          | MODULE   | TRANSFORM | Bool    |
| 20  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 21  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 22  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 23  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 24  | IpoDerefinement           | -          | MODULE   | ANALYSIS  | Meta    |
| 25  | (Transform) [DEFAULT=1]   | -          | MODULE   | TRANSFORM | Bool    |
| 26  | I2PP2IOpt                 | -          | MODULE   | ANALYSIS  | Meta    |
| 27  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 28  | Passno                    | 0x489160   | MODULE   | ANALYSIS  | Meta    |
| 29  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 30  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 31  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 32  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 33  | (Transform)               | 0x48AFF0   | MODULE   | TRANSFORM | Bool    |
| 34  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 35  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 36  | BitcodeVersionUpgrade     | -          | MODULE   | ANALYSIS  | Meta    |
| 37  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 38  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 39  | (Transform)               | -          | MODULE   | TRANSFORM | Bool    |
| 40  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 41  | AttribTransplant          | -          | MODULE   | TRANSFORM | Bool    |
| 42  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 43  | (Transform)               | 0x48D7F0   | MODULE   | TRANSFORM | Bool    |
| 44  | (Analysis)                | -          | MODULE   | ANALYSIS  | Meta    |
| 45  | BasicAa                   | -          | FUNCTION | TRANSFORM | Bool    |
| 46  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 47  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 48  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 49  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 50  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 51  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 52  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 53  | (Transform)               | 0x490B90   | FUNCTION | TRANSFORM | Bool    |
| 54  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 55  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 56  | (Analysis)                | 0x492190   | FUNCTION | ANALYSIS  | Meta    |
| 57  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 58  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 59  | (Transform)               | 0x493700   | FUNCTION | TRANSFORM | Bool    |
| 60  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 61  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 62  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 63  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 64  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 65  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 66  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 67  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 68  | AddToOr                   | 0x4971A0   | FUNCTION | ANALYSIS  | Meta    |
| 69  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 70  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 71  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 72  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 73  | JumpThreading             | 0x499980   | FUNCTION | TRANSFORM | Bool    |
| 74  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 75  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 76  | (Analysis)                | 0x49B6D0   | FUNCTION | ANALYSIS  | Meta    |
| 77  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 78  | (Analysis)                | 0x49C8E0   | FUNCTION | ANALYSIS  | Meta    |
| 79  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 80  | LastRunTracking           | -          | FUNCTION | ANALYSIS  | Meta    |
| 81  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 82  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 83  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 84  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 85  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 86  | (Analysis)                | 0x4A0170   | FUNCTION | ANALYSIS  | Meta    |
| 87  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 88  | ConvertingI32             | -          | FUNCTION | ANALYSIS  | Meta    |
| 89  | InstCombine               | -          | FUNCTION | TRANSFORM | Bool    |
| 90  | InstCombine               | -          | FUNCTION | ANALYSIS  | Meta    |
| 91  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 92  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 93  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 94  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 95  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 96  | (Analysis)                | 0x4A2E30   | FUNCTION | ANALYSIS  | Meta    |
| 97  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 98  | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 99  | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 100 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 101 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 102 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 103 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 104 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 105 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 106 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 107 | FpElim                    | 0x4A64D0   | FUNCTION | TRANSFORM | Bool    |
| 108 | Allopts                   | -          | FUNCTION | ANALYSIS  | Meta    |
| 109 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 110 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 111 | Basicaa                   | -          | FUNCTION | TRANSFORM | Bool    |
| 112 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 113 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 114 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 115 | (Transform)               | 0x4AB910   | FUNCTION | TRANSFORM | Bool    |
| 116 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 117 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 118 | (Analysis)                | 0x4AC770   | FUNCTION | ANALYSIS  | Meta    |
| 119 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 120 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 121 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 122 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 123 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 124 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 125 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 126 | (Analysis)                | 0x4ADE70   | FUNCTION | ANALYSIS  | Meta    |
| 127 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 128 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 129 | (Transform)               | 0x4AEC50   | FUNCTION | TRANSFORM | Bool    |
| 130 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 131 | (Transform)               | 0x4AF290   | FUNCTION | TRANSFORM | Bool    |
| 132 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 133 | (Transform)               | 0x4B0180   | FUNCTION | TRANSFORM | Bool    |
| 134 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 135 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 136 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 137 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 138 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 139 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 140 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 141 | OndemandMdsLoading        | -          | FUNCTION | TRANSFORM | Bool    |
| 142 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 143 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 144 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 145 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 146 | IpoDerefinement           | -          | FUNCTION | ANALYSIS  | Meta    |
| 147 | Passno                    | 0x4CC760   | FUNCTION | TRANSFORM | Bool    |
| 148 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 149 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 150 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 151 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 152 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 153 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 154 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 155 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 156 | (Analysis)                | 0x4CEB50   | FUNCTION | ANALYSIS  | Meta    |
| 157 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 158 | (Analysis)                | 0x16BD370  | FUNCTION | ANALYSIS  | Meta    |
| 159 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 160 | (Analysis)                | -          | LOOP     | ANALYSIS  | Meta    |
| 161 | Symbolication             | -          | LOOP     | ANALYSIS  | Meta    |
| 162 | (Analysis)                | -          | LOOP     | ANALYSIS  | Meta    |
| 163 | (Transform)               | -          | LOOP     | TRANSFORM | Bool    |
| 164 | (Analysis)                | -          | LOOP     | ANALYSIS  | Meta    |
| 165 | FpCastOpt                 | 0x4D0500   | LOOP     | TRANSFORM | Bool    |
| 166 | (Analysis)                | -          | LOOP     | ANALYSIS  | Meta    |
| 167 | LoadSelectTransform       | -          | LOOP     | TRANSFORM | Bool    |
| 168 | (Analysis)                | -          | LOOP     | ANALYSIS  | Meta    |
| 169 | (Transform)               | -          | LOOP     | TRANSFORM | Bool    |
| 170 | (Analysis)                | -          | LOOP     | ANALYSIS  | Meta    |
| 171 | Icp                       | -          | LOOP     | TRANSFORM | Bool    |
| 172 | (Analysis)                | 0x4D2700   | LOOP     | ANALYSIS  | Meta    |
| 173 | (Transform)               | 0x4D3950   | LOOP     | TRANSFORM | Bool    |
| 174 | Vp                        | 0x4D4490   | LOOP     | ANALYSIS  | Meta    |
| 175 | MemopOpt                  | -          | LOOP     | TRANSFORM | Bool    |
| 176 | (Analysis)                | 0x4D5CC0   | LOOP     | ANALYSIS  | Meta    |
| 177 | (Transform)               | -          | LOOP     | TRANSFORM | Bool    |
| 178 | Inline                    | 0x4D6A20   | FUNCTION | ANALYSIS  | Meta    |
| 179 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 180 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 181 | (Transform)               | 0x4D9680   | FUNCTION | TRANSFORM | Bool    |
| 182 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 183 | NounwindInference         | -          | FUNCTION | TRANSFORM | Bool    |
| 184 | (Analysis)                | 0x4DA920   | FUNCTION | ANALYSIS  | Meta    |
| 185 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 186 | InlinedAllocaMerging      | 0x4DBEC0   | FUNCTION | ANALYSIS  | Meta    |
| 187 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 188 | (Analysis)                | 0x4DD2E0   | FUNCTION | ANALYSIS  | Meta    |
| 189 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 190 | PartialInlining           | 0x4DDC60   | FUNCTION | ANALYSIS  | Meta    |
| 191 | (Transform)               | -          | FUNCTION | TRANSFORM | Meta    |
| 192 | (Analysis)                | 0x4DF2E0   | FUNCTION | TRANSFORM | Bool    |
| 193 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 194 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 195 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 196 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 197 | (Transform)               | -          | FUNCTION | ANALYSIS  | Meta    |
| 198 | DSE                       | -          | FUNCTION | ANALYSIS  | Meta    |
| 199 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 200 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 201 | GVN                       | -          | FUNCTION | TRANSFORM | Bool    |
| 202 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 203 | Lftr                      | 0x4E1CD0   | FUNCTION | ANALYSIS  | Meta    |
| 204 | (Transform)               | -          | FUNCTION | ANALYSIS  | Meta    |
| 205 | SelectUnfolding           | -          | FUNCTION | ANALYSIS  | Meta    |
| 206 | LicmPromotion             | -          | FUNCTION | ANALYSIS  | Meta    |
| 207 | (Transform)               | -          | FUNCTION | ANALYSIS  | Meta    |
| 208 | (Analysis)                | -          | FUNCTION | ANALYSIS  | Meta    |
| 209 | (Transform)               | -          | FUNCTION | TRANSFORM | Bool    |
| 210 | (Analysis)                | -          | BACKEND  | ANALYSIS  | Meta    |
| 211 | (Transform) [DEFAULT=1]   | -          | BACKEND  | TRANSFORM | Bool    |
| 212 | (Analysis)                | -          | BACKEND  | ANALYSIS  | Meta    |
| 213 | (Transform)               | -          | BACKEND  | TRANSFORM | Bool    |
| 214 | UnknownTripLsr            | 0x4E4B00   | BACKEND  | ANALYSIS  | Meta    |
| 215 | (Transform)               | 0x4E5C30   | BACKEND  | ANALYSIS  | Meta    |
| 216 | (Analysis)                | -          | BACKEND  | ANALYSIS  | Meta    |
| 217 | (Transform) [DEFAULT=1]   | -          | BACKEND  | TRANSFORM | Bool    |
| 218 | LICM                      | -          | BACKEND  | ANALYSIS  | Meta    |
| 219 | (Transform)               | -          | BACKEND  | TRANSFORM | Bool    |
| 220 | (Analysis)                | -          | BACKEND  | ANALYSIS  | Meta    |
| 221 | (Transform)               | -          | BACKEND  | TRANSFORM | Bool    |

## IDENTIFIED PASSES (82/212)

### Scalar Optimizations
- **InstCombine**: ID 89/90
- **SimplifyCFG**: ctor_073 @ 0x499980
- **AddToOr**: ID 68 @ 0x4971A0
- **JumpThreading**: ID 73 @ 0x499980, ctor_243 @ 0x4ED0C0

### Dead Code Elimination
- **DCE**: ctor_267 @ 0x4F54D0, ctor_515 @ 0x55ED10, ctor_676 @ 0x5A3430
- **DSE**: ID 198, ctor_444 @ 0x53EB00
- **DeadArgumentElim**: (inferred)

### Loop Optimizations
- **LICM**: ID 218, ctor_206 @ 0x4E33A0
- **LicmPromotion**: ID 206, ctor_457 @ 0x544C40
- **LoopUnroll**: ctor_472 @ 0x54B6B0
- **Lftr**: ID 203 @ 0x4E1CD0, ctor_452 @ 0x541C20
- **UnknownTripLsr**: ID 214 @ 0x4E4B00, ctor_470 @ 0x54A080

### Value Numbering
- **GVN**: ID 201
- **CSE**: ctor_564 @ 0x572AC0

### Interprocedural
- **Inline**: ID 178 @ 0x4D6A20, ctor_392 @ 0x51E600, ctor_425 @ 0x5345F0, ctor_629 @ 0x58FAD0
- **PartialInlining**: ID 190 @ 0x4DDC60, ctor_431 @ 0x537BA0
- **Preinline**: ctor_388 @ 0x51B710, ctor_723 @ 0x5C1130
- **Internalization**: ctor_430 @ 0x536F50

### Analysis Passes
- **BasicAa**: ID 45
- **TargetTransformInfo**: ctor_620 @ 0x58B6C0
- **ComplexBranchDist**: ctor_262 @ 0x4F2830, ctor_525 @ 0x563730

### NVIDIA-Specific
- **NvptxLoadStoreVectorizer**: ctor_358 @ 0x50E8D0, ctor_609 @ 0x585D30
- **FpElim**: ID 107 @ 0x4A64D0
- **FpCastOpt**: ID 165 @ 0x4D0500
- **LdstUpsizing**: ctor_516 @ 0x5605F0

### Backend/CodeGen
- **Vectorization**: ctor_642
- **CgpBranchOpts**: ctor_288 @ 0x4FA950, ctor_544 @ 0x56C190
- **SchedCycles**: ctor_282 @ 0x4F8F80, ctor_652 @ 0x599EF0
- **PostRa**: ctor_600 @ 0x57F210
- **Peephole**: ctor_314, ctor_577

### Miscellaneous
- **Passno**: ID 28 @ 0x489160, ID 147 @ 0x4CC760
- **Allopts**: ID 108, ctor_335 @ 0x507310
- **Checks**: ctor_402 @ 0x526D20
- **DebugInfoPrint**: ctor_729 @ 0x5C4BB0

## OPTIMIZATION LEVELS

### O0 (Minimal)
- **Passes**: ~15-20
- **Enabled**: Correctness-critical only
- **Examples**: AlwaysInliner, NVVMReflect, MandatoryInlining

### O1 (Basic)
- **Passes**: ~50-60
- **Enabled**: Quick optimizations
- **Examples**: SimplifyCFG, InstCombine, DSE, EarlyCSE

### O2 (Standard)
- **Passes**: ~150-170
- **Enabled**: All major optimizations
- **Examples**: LICM, GVN, MemCpyOpt, Inlining, GlobalOpt

### O3 (Aggressive)
- **Passes**: ~200-212
- **Enabled**: All passes + aggressive variants
- **Examples**: LoopUnroll, LoopVectorize, SLPVectorize, BBVectorize
- **Special**: IDs 19, 25, 217 default-enabled

## PASS DEPENDENCIES

### Common Requirements
- **DominatorTree**: Required by 80+ passes
- **LoopInfo**: Required by 30+ loop passes
- **CallGraph**: Required by inlining passes
- **ScalarEvolution**: Required by loop analysis
- **TargetLibraryInfo**: Required by optimization passes

### Invalidation Patterns
- **SimplifyCFG**: Invalidates DominatorTree, LoopInfo
- **LoopUnroll**: Invalidates LoopInfo, DominatorTree
- **Inlining**: Invalidates CallGraph, all CFG analyses
- **LICM**: Invalidates LoopInfo

## MEMORY OVERHEAD

- **PassManager**: 5104 bytes
- **PassRegistry**: 14208 bytes (222 × 64)
- **PassDescriptor Array**: 5088 bytes (212 × 24)
- **Total**: ~24 KB per compilation unit

## CONSTRUCTOR ANALYSIS

### Total Constructors: 206
### Mapped Addresses: 133
### Unique Names: 82
### Pass Variants:
- **DCE**: 6 instances
- **Inline**: 4 instances
- **CSE**: 4 instances
- **LICM**: 3 instances
- **InstCombine**: 2 instances
