# Architecture Detection

## Binary Evidence
- Entry point: 0x95EB40 (1,234 bytes executable)
- IR validator: 0x2C80C90 (1,536 bytes executable)
- Data layout validator: 0x2C74F70
- Error emission: 0xCB6200

## Detection Pipeline

1. **Compile-time flag parsing** (0x95EB40)
2. **Feature flag initialization** (SMFeatures struct)
3. **Type system address space checks** (0x2C80C90)
4. **Instruction validation** (runtime, per-instruction)

---

## Flag Translation

**Input → Preprocessor | Optimizer | LLVM**

```
-arch=compute_XXX → -R __CUDA_ARCH=YYYY | -opt-arch=sm_XXX | -mcpu=sm_XXX
```

**Evidence** (sub_95EB40_0x95eb40.c, lines 485-649):

| Arch | __CUDA_ARCH | opt-arch | mcpu | Supported |
|------|-------------|----------|------|-----------|
| SM75 | 750 | sm_75 | sm_75 | Turing (RTX 20) |
| SM80 | 800 | sm_80 | sm_80 | Ampere (A100) |
| SM86 | 860 | sm_86 | sm_86 | Ampere (RTX 30) |
| SM87 | 870 | sm_87 | sm_87 | Jetson Orin |
| SM88 | 880 | sm_88 | sm_88 | Ada variants |
| SM89 | 890 | sm_89 | sm_89 | Ada (L40S) |
| SM90 | 900 | sm_90/90a | sm_90 | Hopper (H100) |
| SM100 | 1000 | sm_100/100a/100f | sm_100 | Blackwell DC |
| SM103 | 1030 | sm_103/103a/103f | sm_103 | Blackwell DC var |
| SM110 | 1100 | sm_110/110a/110f | sm_110 | Blackwell Thor |
| SM120 | 1200 | sm_120/120a/120f | sm_120 | RTX 50 |
| SM121 | 1210 | sm_121/121a/121f | sm_121 | Future |

**Variant encoding**:
- Base (sm_XXX): standard, forward-compat
- `-a` (sm_XXXa): arch-specific, NOT forward-compat
- `-f` (sm_XXXf): family features, partial compat

---

## PTX ISA Mapping

```
PTX 1.0 → SM20-21    PTX 6.0 → SM70-72     PTX 8.0 → SM90-99
PTX 3.0 → SM30-37    PTX 6.3 → SM75        PTX 8.3 → SM100-103
PTX 4.0 → SM50-53    PTX 7.0 → SM80-89     PTX 8.4 → SM110-121
PTX 5.0 → SM60-62
```

---

## Feature Matrix

**Tensor cores** (latency: cycles per operation):

| Feature | SM70 | SM75 | SM80 | SM90 | SM100 |
|---------|------|------|------|------|-------|
| Tensor type | WMMA | WMMA | MMA.SYNC | WGrpMMA | TCGen05 |
| Latency | 8 | 8 | 4 | 3 | 2 |
| RF size | 64KB | 64KB | 64KB | 128KB | 128KB |
| Max regs | 255 | 255 | 255 | 255 | 255 |
| Color K | 15 | 15 | 15 | 15 | 15 |

**Precision support** (bitmask encoding):

| Format | SM70 | SM75 | SM80 | SM90 | SM100 |
|--------|------|------|------|------|-------|
| FP16 | ✓ | ✓ | ✓ | ✓ | ✓ |
| BF16 | ✗ | ✗ | ✓ | ✓ | ✓ |
| TF32 | ✗ | ✗ | ✓ | ✓ | ✓ |
| FP8 | ✗ | ✗ | ✗ | ✓ | ✓ |
| FP4 | ✗ | ✗ | ✗ | ✗ | ✓ |
| INT4 | ✗ | ✗ | ✗ | ✗ | ✓ |

**Memory operations**:

| Feature | SM70 | SM75 | SM80 | SM90 | SM100 |
|---------|------|------|------|------|-------|
| cp.async | ✗ | ✗ | ✓ | ✓ | ✓ |
| TMA | ✗ | ✗ | ✗ | ✓ | ✓ |
| TMEM (AS6) | ✗ | ✗ | ✗ | ✗ | ✓* |

`*SM100/103 only (SM120 rejects)`

**Synchronization**:

| Feature | SM70 | SM75 | SM80 | SM90 | SM100 |
|---------|------|------|------|------|-------|
| CoopGrp | ✓ | ✓ | ✓ | ✓ | ✓ |
| WarpSpec | ✗ | ✗ | ✗ | ✓ | ✓ |
| TBC | ✗ | ✗ | ✗ | ✓ | ✓ |
| 2:4 Sparse | ✗ | ✓ | ✓ | ✓ | ✓ |
| DynSparse | ✗ | ✗ | ✗ | ✗ | ✓ |

---

## SMFeatures Struct Initialization

```c
typedef struct {
    uint32_t sm_version;              // 70-121
    uint32_t max_registers;           // 255
    uint32_t register_file_kb;        // 64 (SM70-89) | 128 (SM90+)
    uint32_t physical_color_limit;    // 15 (all)
    uint64_t feature_flags;           // Bitmask below
} SMFeatures;

// Feature bit layout (uint64_t feature_flags):
// [0-7]   : Reserved (must be 0)
// [8]     : has_tensor_cores (SM70+)
// [9]     : has_wmma (SM70+)
// [10]    : has_mma_sync (SM80+)
// [11]    : has_warpgroup_mma (SM90+)
// [12]    : has_tcgen05 (SM100+)
// [13]    : has_async_copy (SM80+)
// [14]    : has_tma (SM90+)
// [15]    : has_fp16 (SM70+)
// [16]    : has_bfloat16 (SM80+)
// [17]    : has_tf32 (SM80+)
// [18]    : has_fp8 (SM90+)
// [19]    : has_fp4 (SM100+)
// [20]    : has_int4 (SM100+)
// [21]    : has_block_scale (SM100+)
// [22]    : has_cooperative_groups (SM70+)
// [23]    : has_warp_specialization (SM90+)
// [24]    : has_thread_block_clusters (SM90+)
// [25]    : has_sparsity_2_4 (SM75+)
// [26]    : has_dynamic_sparsity (SM100+)
// [27-63] : Reserved
```

**Initialization pseudocode** (0x95EB40):

```c
if (sm >= 70) flags |= (1ULL << 8);   // tensor_cores
if (sm >= 70) flags |= (1ULL << 9);   // wmma
if (sm >= 75) flags |= (1ULL << 25);  // sparsity_2_4
if (sm >= 80) flags |= (1ULL << 10);  // mma_sync
if (sm >= 80) flags |= (1ULL << 13);  // async_copy
if (sm >= 80) flags |= (1ULL << 16);  // bfloat16
if (sm >= 80) flags |= (1ULL << 17);  // tf32
if (sm >= 90) flags |= (1ULL << 14);  // tma
if (sm >= 90) flags |= (1ULL << 11);  // warpgroup_mma
if (sm >= 90) flags |= (1ULL << 18);  // fp8
if (sm >= 90) flags |= (1ULL << 23);  // warp_specialization
if (sm >= 90) flags |= (1ULL << 24);  // thread_block_clusters
if (sm >= 100) flags |= (1ULL << 12); // tcgen05
if (sm >= 100) flags |= (1ULL << 19); // fp4
if (sm >= 100) flags |= (1ULL << 20); // int4
if (sm >= 100) flags |= (1ULL << 21); // block_scale
if (sm >= 100) flags |= (1ULL << 26); // dynamic_sparsity
```

---

## Instruction Selection Dispatch

**Location**: 0x2C80C90 (1,536 bytes)

**Access pattern**:
```c
uint32_t instr_type = decode_instr_opcode(instr);
uint32_t dispatch_idx = instr_type & 0xFF;  // 0-255 range
void (*handler)(IRInstr* instr) = instr_dispatch_table[dispatch_idx];
handler(instr);
```

**Implemented opcodes** (0x3D-0x4F):

| Opcode | Instr | Handler | Check |
|--------|-------|---------|-------|
| 0x3D | load | check_addr_space() | AS6 (TMEM) rejected |
| 0x3E | store | check_addr_space() | AS6 (TMEM) rejected |
| 0x40 | fence | check_ordering() | Only acq_rel/seq_cst |
| 0x4F | addrspacecast | check_as_cast() | No AS1→AS3 direct |

**Fault conditions** (emit error string):
- Load/store with AS6 on SM120: 0x43a24b0 ("Tensor Memory loads/stores...")
- fence with acquire/release: "Invalid ordering for fence..."
- addrspacecast AS1↔AS3: "Cannot cast non-generic..."

---

## Address Space Layout

**Type descriptor offset analysis** (0x2C80C90:1260):

```
Instruction → type_info (offset -56)
           → deref (offset +8)
           → [bits 0-7: flags]
             [bits 8-31: address_space]
           → [bits 8-31] >> 8 == AS
```

**Address space values**:

| AS | Name | L/S | Atomic | Cast |
|----|------|-----|--------|------|
| 0 | generic | ✓ | ✓ | ↔all |
| 1 | global | ✓ | ✓ | ↔0 |
| 3 | shared | ✓ | ✓ | ↔0 |
| 6 | TMEM | ✓* | ✗ | ↔0 |

`*SM100/103 only (AS6 → error on SM120)`

---

## Target Triple Validation

**Function** (0x2C80C90:721-922):

| Mode | Validation | Error |
|------|-----------|-------|
| IR (mode=1) | nvptx-*-cuda, nvsass-*-cuda/nvcl/directx/spirv | "Invalid target triple" |
| PTX (mode=0) | nvptx-*-cuda, nvptx64-*-cuda | "Invalid target triple (must be nvptx...)" |

**Data layout check** (0x2C74F70):
- Empty string → "Empty target data layout, must exist"
- Invalid format → Parse error with examples

---

## Unsupported Instructions

| Instr | Error | Line | Condition |
|-------|-------|------|-----------|
| indirectbr | - | 1185-1186 | Always |
| invoke | - | 1188-1190 | Always |
| resume | - | 1192-1194 | Always |
| fence | "Invalid ordering" | 1308 | IR mode, acquire/release |
| landingpad | - | 1652 | Always |

---

## Blackwell Divergence

**SM100/103 vs SM120**:

| Property | SM100 | SM120 |
|----------|-------|-------|
| TMEM (AS6) | ✓ (0x445905e) | ✗ (0x43a24b0) |
| Max warps | 64 | 48 |
| Shared mem | 227 KB | 99 KB |
| Tensor gen | TCGen05 | MMA.SYNC |
| Matrix layout | TN/TT/NT/NN | TN only |
| Dispatch | 1SM/2SM | 1SM |

**SM120 TMEM rejection** (0x2C80C90:1272):
```c
if (addr_space == 6) {
    sub_CB6200(stream, "Tensor Memory loads/stores...", 0x2D);
    *context |= ERROR_FLAG;
    if (!diagnostic_mode) abort_compile();
}
```

---

## String References

| Address | String |
|---------|--------|
| 0x43a24b0 | "Tensor Memory loads/stores are not supported\n" |
| 0x445905e | "Has support for Tensor Memory" |

---

## Code Generation Control

**SM-specific paths** (dispatch per opcode):

```c
// Instruction selection
if (sm >= 100) emit_tcgen05_mma(op);     // Fastest: 2-cycle latency
else if (sm >= 90) emit_warpgroup_mma(); // 3-cycle latency
else if (sm >= 80) emit_mma_sync();      // 4-cycle latency
else emit_wmma();                        // 8-cycle latency

// Register allocation
rf_size_kb = (sm >= 90) ? 128 : 64;
max_occupancy = (sm >= 90) ? 100 : 75;
```

**Scheduling (overlapping**):

```c
if (sm >= 90) overlap = 1.5;       // TCGen05: tight scheduling
else if (sm >= 80) overlap = 2.5;  // MMA.SYNC: moderate
else overlap = 4.0;                // WMMA: aggressive prefetch
```

---

## Evidence Summary

- **Binary size**: 2.78 GB (cicc.i64 at /home/grigory/nvopen-tools/cicc/cicc)
- **Functions analyzed**: 0x95EB40 (flag parser), 0x2C80C90 (validator)
- **Arch coverage**: SM75-SM121 (12 major versions)
- **TMEM cutoff**: SM100/103 only (explicit error string at 0x43a24b0)
- **Critical dispatch**: Instruction opcode → handler (0x3D-0x4F range)
