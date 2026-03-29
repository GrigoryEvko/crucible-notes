# Code Generation

NVPTX backend: SelectionDAG lowering, instruction selection, register allocation, and machine-level passes. Address range `0x1700000`–`0x35EFFFF` (~37 MB of code). This is the **largest address range** in the binary.

| | |
|---|---|
| **NVPTXTargetLowering** | `0x330xxxx`–`0x33Bxxxx` (~2.3MB combined) |
| **Intrinsic lowering switch** | `sub_33B0210` (343KB — one of the largest single functions) |
| **NVPTXDAGToDAGISel** | `0x33D0000`–`0x348FFFF` (~1.7MB) |
| **ISel entry** | `sub_3090F90` (91KB) |
| **Greedy RegAlloc** | `0x34A0000`–`0x350FFFF` |
| **StructurizeCFG** | `sub_35CC920` (95KB, mandatory for PTX) |
| **MRPA** | `0x1DF0000`–`0x1E00000` (NVIDIA-custom pressure tracking) |
| **LegalizeTypes** | `sub_20019C0` (348KB — largest function in SelectionDAG range) |

## Architecture

```
LLVM IR
  │
  ├─ CodeGenPrepare (IR-level backend prep)
  │    sub_1D70000-1D7FFFF: sunkaddr, sunk_phi, block splitting
  │
  ├─ SelectionDAG Build
  │    sub_2065D30 (visit dispatcher)
  │    sub_2056920 (major worker, 69KB)
  │    sub_2077400 (NVVM tex/surf handle lowering) ★ NVIDIA
  │    sub_2072590 (NVPTX argument passing, 38KB) ★ NVIDIA
  │
  ├─ LegalizeTypes
  │    sub_20019C0 (348KB main loop)
  │    sub_201E5F0 (opcode dispatch, 81KB)
  │    sub_201BB90 (expand integer, 75KB)
  │
  ├─ LegalizeOp
  │    sub_1FFB890 (169KB, type action dispatch)
  │    sub_1FF6F70 (43KB, atomic target-specific lowering) ★ NVIDIA
  │
  ├─ DAG Combining
  │    sub_F681E0 (65KB, top-level orchestrator)
  │    sub_F20C20 (64KB, visitNode main)
  │
  ├─ Instruction Selection
  │    sub_3090F90 (91KB, NVPTXDAGToDAGISel::Select) ★ NVIDIA
  │    sub_33D4EF0 (complex addressing, calls sub_969240 399×)
  │
  ├─ Instruction Scheduling
  │    sub_355F610 (64KB, ScheduleDAGMILive post-RA)
  │    sub_3563190 (58KB, MachinePipeliner)
  │
  ├─ Register Allocation
  │    sub_2F49070 (82KB, RAGreedy::selectOrSplit)
  │    sub_2F2D9F0 (93KB, LiveRangeSplitter)
  │
  ├─ Machine-Level Passes
  │    MRPA, Block Remat, Mem2Reg, LDG, Peephole, etc.
  │
  └─ StructurizeCFG
       sub_35CC920 (95KB, mandatory for PTX structured control flow)
```

## SelectionDAG Infrastructure

### SDNode Layout

Observed from pervasive patterns across the DAG codebase:

| Offset | Size | Field |
|---|---|---|
| +0 | 8 | First operand / use chain |
| +4 | 4 | Packed: `NumOperands` (bits 0–26) \| `Flags` (bits 27–31) |
| +7 | 1 | Extra flags (bit 6 = has operand pointer at -8) |
| +8 | 8 | ValueType / MVT |
| +16 | 8 | Use chain (next user pointer) |
| +24 | 8 | Operand value / constant |
| +26 | 2 | Memory flags (bits 7–9 = address space: global=1, shared=3, local=5) |

Operand stride: **32 bytes** per operand. Access pattern: `node - 32 * (node+4 & 0x7FFFFFF)`.

### DAG Combining — `sub_F681E0`

| Field | Value |
|---|---|
| Address | `0xF681E0` |
| Size | 65KB (largest in SelectionDAG infrastructure range) |
| Role | Top-level DAG combining orchestrator |

Manages worklist, iterates SDNode linked lists, creates new nodes. Opcodes: 55=TokenFactor, creates `"ind"` and `"merge"` nodes. Global `byte_4F8F8E8` = verbose/debug flag.

### Known Bits for DAG — `sub_F5A610`

Self-recursive with depth limit (a4==48 → early return). Handles ISD::Constant (opcode 17), ISD::BUILD_VECTOR (54/55). Uses `sub_F4F140`/`sub_F4F1E0`/`sub_F4F8E0` for KnownBits operations.

## NVPTX Target Lowering

The NVPTXTargetLowering cluster at `0x330xxxx`–`0x33Bxxxx` (~2.3MB) is the most NVIDIA-modified area in the binary.

### LowerOperation Dispatcher — `sub_32E3060`

| Field | Value |
|---|---|
| Address | `0x32E3060` |
| Size | 111KB |
| Role | Main ISD opcode → custom lowering dispatch |

### Key Lowering Functions

| Function | Size | Purpose |
|---|---|---|
| `sub_3040BF0` | 88KB | `LowerCall` — .param space argument passing ABI |
| `sub_3048C30` | 86KB | Atomic operation lowering (scope-aware: CTA/GPU/SYS) |
| `sub_3349730` | — | `LowerFormalArguments` |
| `sub_331C5B0` | — | Formal arguments helper |
| `sub_332FEA0` | — | Call lowering (`__sync_synchronize`, `abort()` handling) |
| `sub_2072590` | 38KB | NVPTX-custom argument passing / type coercion |
| `sub_2077400` | 20KB | NVVM texture/surface handle lowering |

### NVVM Texture/Surface Lowering — `sub_2077400`

String: `"nvvm_texsurf_handle op0 must be metadata wrapping a GlobalVariable"`. Handles NVVM texture/surface intrinsic lowering — checks metadata node type == 19 (MDNode), validates GlobalVariable type.

### Atomic Lowering

`sub_1FF6F70` (43KB): Target-specific atomic lowering. Switch on opcode (54–58 = atomic operations), inner switch on element size (3=i16, 4=i32, 5=i64, 6=i128, 7=f16). Checks `*(_BYTE*)(*a1 + 792)` for SM feature flags (bit mask 0xC). Uses target instruction table at offset 74096.

NVPTX-specific atomic opcodes at `sub_20BED60`:
- Opcodes 294–297: `atom.add` (f32/f64/i32/i64)
- Opcodes 302–305: `atom.min` (s32/s64/u32/u64)
- Opcodes 314–317: `atom.max` (s32/s64/u32/u64)
- Opcode 462: `atom.cas` (generic)

## Instruction Selection

### NVPTXDAGToDAGISel::Select — `sub_3090F90`

| Field | Value |
|---|---|
| Address | `0x3090F90` |
| Size | 91KB |
| Role | Main ISel entry point |

### Intrinsic Lowering Switch — `sub_33B0210`

| Field | Value |
|---|---|
| Address | `0x33B0210` |
| Size | 343KB |
| Role | NVVM intrinsic → PTX instruction mapping |

Handles hundreds of NVVM intrinsics: tex, surf, atom, shfl, vote, wmma, mma, tensor, barrier, etc.

### Compressed Legality Table

The instruction selector uses a compressed table at `base+6414`:

```c
legality = *(byte*)(base + 500*arch_variant + opcode + 6414);
// Values: 0=illegal, 1=custom lower, 2=legal
```

Secondary table at `base+521536`: 4-bit packed bitfield indexed by `(opcode_class >> 3) + 36*arch_id - arch_id`.

### Legalize Action Table

At object offset 72760 (`0x11C58`): Per-type legalize action array. 4 bits per entry, stride `4 * (type_bits + 15 * opcode + 18112) + 12`. Actions: 0=Legal, 1=Promote, 5=Custom, 9=ExpandInt, 13=ExpandFP, 14=SplitVec.

## MMA / Tensor Core Codegen

### MMA Instruction Builder — `sub_21E74C0`

| Field | Value |
|---|---|
| Address | `0x21E74C0` |
| Size | 17KB |

Central PTX mma/wmma instruction builder covering all generations:

| Shape | Data Types | Features |
|---|---|---|
| `m8n8k4` | f16, f32, f64 | Volta (sm_70) |
| `m8n8k16/32/64/128` | s32 (int MMA) | Turing (sm_75) |
| `m16n8k4/8/16/32/64/128/256` | f16, bf16, tf32, s32 | Ampere/Hopper/Blackwell |
| `m16n16k16/8` | f16, s32 | Legacy shapes |
| `m32n8k16` | s32 | Legacy shapes |

Rounding modes: `.rm`, `.rn`, `.rp`, `.rz`. Saturation: `.satfinite`. Binary operations: `.and.popc`, `.xor.popc`.

### Per-Type Lowering Functions

| Function | Size | Type |
|---|---|---|
| `sub_21DFBF0` | 5KB | HMMA store-C (`hmmastc`) |
| `sub_21E0360` | 3KB | HMMA load-A/B (`hmmaldab`) |
| `sub_21E0630` | 3KB | HMMA load-C (`hmmaldc`) |
| `sub_21E0870` | 4KB | HMMA multiply-accumulate (`hmmamma`) |
| `sub_21E1280` | 4KB | IMMA load-A/B (`immaldab`) |
| `sub_21E15D0` | 3KB | IMMA load-C (`immaldc`) |
| `sub_21E1830` | 5KB | IMMA store-C |
| `sub_21E1D20` | 6KB | IMMA multiply-accumulate (saturation control) |
| `sub_21E2280` | 6KB | Binary MMA (`bmma`, 1-bit XOR/AND popcount) |
| `sub_21E8CD0` | 2KB | tcgen05 scaled MMA (Blackwell: `scaleD`, `transA`, `negA`, `negB`) |

## Register Allocation

### Greedy RA — `sub_2F49070`

| Field | Value |
|---|---|
| Address | `0x2F49070` |
| Size | 82KB |
| Role | `RAGreedy::selectOrSplit` inner loop |

Standard LLVM greedy register allocator. "Allocation failed" fallback at `sub_34ED530`.

### Live Range Splitting — `sub_2F2D9F0`

| Field | Value |
|---|---|
| Address | `0x2F2D9F0` |
| Size | 93KB |
| Role | Split live ranges to reduce spilling |

### NVPTX Register Classes

`Int1Regs`, `Int16Regs`, `Int32Regs`, `Int64Regs`, `Float32Regs`, `Float64Regs`. Register class IDs from instruction constraints: 14, 24, 27, 29, 32, 36, 39, 40, 41, 50, 51, 67, 72, 76, 78.

## NVIDIA Machine-Level Passes

### MRPA — Machine Register Pressure Analysis

| Field | Value |
|---|---|
| Address | `sub_2E5A4E0` (48KB) + `0x1DF0000`–`0x1E00000` cluster |
| Pass ID | `machine-rpa` |
| Evidence | `"Incorrect RP info from incremental MRPA update"` |

Custom incremental register pressure tracking not in upstream LLVM. Integrated with scheduler.

### Block Rematerialization — `sub_2186D90`

| Field | Value |
|---|---|
| Address | `0x2186D90` |
| Size | 47KB |
| Pass ID | `nvptx-remat-block` |
| Algorithm | Two-phase: candidate selection + iterative "pull-in" |

Strings: `"Max-Live-Function("`, `"Reducing %d live-ins"`, `"Really Final Pull-in:"`. Uses `sub_2181870` (19KB) for two-phase candidate selection with "second-chance" heuristic. Configurable iteration limit: `dword_4FD3740`.

### Machine Mem2Reg — `sub_21F9920`

| Pass ID | `nvptx-mem2reg` |
|---|---|
| Purpose | Promotes `__local_depot` stack objects back to registers post-regalloc |

### LDG Transform — `sub_21F2780`

| Pass ID | `ldgxform` |
|---|---|
| Purpose | Transforms global loads to `ldg.*` (texture cache) for read-only data |

Creates `.ldgsplit`, `.load`, `.ldgsplitinsert` suffixed values.

### Vector Splitting — `sub_21F3A20`

| Field | Value |
|---|---|
| Address | `0x21F3A20` |
| Size | 44KB |
| Purpose | Split wide vectors to match PTX register sizes |

Strings: `"bitCast"`, `"vecBitCast"`, `"splitVec"`, `"extractSplitVec"`, `"insertSplitVec"`, `"splitVecGEP"`.

### RLMCAST — `sub_2D13E90`

| Field | Value |
|---|---|
| Address | `0x2D13E90` |
| Size | 67KB |
| Purpose | Register-level multicast instruction lowering |

NVIDIA-proprietary instruction — broadcasts a value to multiple register destinations.

### Texture Group Merge (.Tgm)

`sub_2DDE8C0`: `.Tgm` suffix marks texture group operations in scheduling. Groups texture loads to hide latency using function pointer table (3 predicates).

## StructurizeCFG — `sub_35CC920`

| Field | Value |
|---|---|
| Address | `0x35CC920` |
| Size | 95KB |
| Required | **Mandatory** for NVPTX (PTX demands structured control flow) |

Explicitly rejects EH funclets and irreducible CFGs: `"Irreducible CFGs are not supported yet"`, `"EH Funclets are not supported yet"`.

## NVPTX Subtarget

### Feature Flag Offsets (NVPTXSubtarget)

| Offset | Purpose |
|---|---|
| +2498 | Type legality flags (per MVT, 259-byte stride) |
| +2584 | Float legality flags |
| +2843 | Integer type support flag |
| +2870–2871 | Branch distance / jump table eligibility |

### SM Processor Table — `qword_502A920`

45 entries with stride-2 layout: `[2*i+0]` = SM name, `[2*i+1]` = PTX version code. PTX versions: 5 (legacy through sm_90), 6 (sm_90a+), 7 (a/f variants).

## Key Global Variables

| Variable | Purpose |
|---|---|
| `byte_4FD2E80` | Remat debug trace flag |
| `dword_4FD3740` | Remat iteration limit |
| `byte_4FD1980` / `byte_4FD18A0` / `byte_4FD1A60` | LICM/CSE/Sinking enables |
| `byte_4FD25C0` | nvptx-mem2reg enable |
| `dword_4FD26A0` | Scheduling mode (1=simple, else=full pipeline) |
| `byte_4F8F8E8` | DAG combining verbose/debug flag |
| `qword_4F8BF28` | Known-bits recursive expansion threshold |
