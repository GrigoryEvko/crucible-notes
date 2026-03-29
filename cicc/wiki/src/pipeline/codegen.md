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
| Packed descriptor | `*(QWORD*)(*(QWORD*)(a1+16) + 16*a2 + 8)` |

Central PTX mma/wmma instruction builder. Reads a packed 64-bit descriptor via string queries ("mid", "shape", "ety", "aty", "bty", "al", "bl", "opc", "rnd", "satf", "rowcol").

### Complete Shape Inventory

| Enum | Shape | M dim | PTX String | Notes |
|---|---|---|---|---|
| `0x01` | m8n8k4 | 8 | `"m8n8k4"` | Original Volta HMMA |
| `0x02` | m8n8k16 | 8 | `"m8n8k16"` | Integer MMA (s8/u8) |
| `0x03` | m8n8k32 | 8 | `"m8n8k32"` | Sub-byte (s4/u4) |
| `0x04` | m8n8k64 | 8 | `"m8n8k64"` | Extended sub-byte |
| `0x05` | m8n8k128 | 8 | `"m8n8k128"` | Binary MMA (b1) |
| `0x10` | m16n8k4 | 16 | `"m16n8k4"` | f64 on Ampere |
| `0x11` | m16n8k8 | 16 | `"m16n8k8"` | Turing/Ampere HMMA |
| `0x12` | m16n8k16 | 16 | `"m16n8k16"` | Ampere (bf16, tf32) |
| `0x13` | m16n8k32 | 16 | `"m16n8k32"` | Ampere integer |
| `0x14` | m16n8k64 | 16 | `"m16n8k64"` | Sub-byte integer |
| `0x15` | m16n8k128 | 16 | `"m16n8k128"` | Extended sub-byte |
| `0x16` | m16n8k256 | 16 | `"m16n8k256"` | Largest — binary/sub-byte |
| `0x17` | m16n16k16 | 16 | `"m16n16k16"` | Square — Hopper+ |
| `0x18` | m32n8k16 | 32 | `"m32n8k16"` | Tall shape |
| `0x19` | m16n16k8 | 16 | `"m16n16k8"` | f16 WMMA path |

### Data Type Encoding

| Enum | Type | Bits | PTX |
|---|---|---|---|
| 1 | b1 | 1 | `"b1"` |
| 2 | s4 | 4 | `"s4"` |
| 3 | u4 | 4 | `"u4"` |
| 4 | s8 | 8 | `"s8"` |
| 5 | u8 | 8 | `"u8"` |
| 6 | f16 | 16 | `"f16"` |
| 7 | bf16 | 16 | `"bf16"` |
| 8 | tf32 | 19 | `"tf32"` |
| 9 | f64 | 64 | `"f64"` |
| 10 | f32 | 32 | `"f32"` |
| 11 | s32 | 32 | `"s32"` |

### Packed Descriptor Bit Layout

| Bits | Field | Values |
|---|---|---|
| [0] | rowcol | 0=row, 1=col |
| [2:1] | mid | 0=a, 1=b, 2=c, 3=d |
| [7:4] / [2:0] | opc / rnd | 0=none, 1=.and.popc, 2=.xor.popc / 1=.rn, 2=.rm, 3=.rp, 4=.rz |
| [15:8] | aty | A element type enum |
| [23:16] | bty | B element type enum |
| [25:24] | al | A layout (0=row, nonzero=col) |
| [27:26] | bl | B layout |
| [28] | satf | Saturation flag → `.satfinite` |
| [39:32] | shape | Shape enum (0x01–0x19) |

### Architecture Gates

| Family | Gate | Min SM | Features |
|---|---|---|---|
| HMMA | `*(target+252) > 0x45` | SM 70 | f16 only |
| IMMA | `*(target+252) > 0x47` | SM 72 | s8/u8 (restricted shapes at SM 72) |
| IMMA full | `*(target+252) > 0x48` | SM 75 | All IMMA shapes |
| BMMA | `*(target+252) > 0x48` | SM 75 | b1 (.and.popc, .xor.popc) |
| bf16/tf32/f64 | — | SM 80 | Ampere data types |
| tcgen05 | `+340 ≥ 0x3E8` | SM 100 | Blackwell tensor core |

SM 72 (Xavier) restriction: only basic IMMA shape (variant ≤ 1). SM 75+ supports all.

### Per-Family Functions

| Function | Family | Operation | Min SM |
|---|---|---|---|
| `sub_21E0360` | HMMA | load A/B (`hmmaldab`) | 70 |
| `sub_21E0630` | HMMA | load C (`hmmaldc`) | 70 |
| `sub_21DFBF0` | HMMA | store C (`hmmastc`) | 70 |
| `sub_21E0870` | HMMA | MMA (`hmmamma`) | 70 |
| `sub_21E1280` | IMMA | load A/B (`immaldab`) | 72 |
| `sub_21E15D0` | IMMA | load C (`immaldc`) | 72 |
| `sub_21E1830` | IMMA | store C | 72 |
| `sub_21E1D20` | IMMA | MMA w/ saturation (`immamma`) | 72 |
| `sub_21E2280` | BMMA | binary MMA (`bmmamma`) | 75 |
| `sub_21E8CD0` | tcgen05 | scaled MMA operands | 100 |

Each function exists in two copies: AsmPrinter side (`0x21Dxxxx`) and NVPTX backend side (`0x36Exxxx`).

### tcgen05 Blackwell — `sub_21E8CD0` / `sub_35F3E90`

Packed descriptor bits for Blackwell scaled MMA:

| Bit | Field | Values |
|---|---|---|
| 0 | scaleD | 0→"0", 1→"1" |
| 1 | negA | 0→"1", 1→"-1" |
| 2 | negB | 0→"1", 1→"-1" |
| 3 | transA | 0→"0", 1→"1" |
| 4 | transB | 0→"0", 1→"1" |

10 tcgen05.mma shape variants (opcodes 4905–4940), with modifiers: `block_scale`, `sparsity` (bit 5), `weight_stationary`, `scaleInputAccumulator`. Type encoding bits[8:6]: mxf4nvf4, i8, mxf8f6f4, f16, tf32, fp4, mxf4, bf16.

### Shape × Type × Architecture Matrix

| Shape | A/B Types | Acc Types | Min SM |
|---|---|---|---|
| m8n8k4 | f16 | f16,f32 | 70 |
| m16n8k4 | f64 | f64 | 80 |
| m16n8k8 | f16 | f16,f32 | 75 |
| m16n8k16 | f16,bf16,tf32 | f16,f32 | 80 |
| m16n16k16 | f16,bf16 | f16,f32 | 90 |
| m8n8k16 | s8,u8 | s32 | 72 |
| m16n8k32 | s8,u8 | s32 | 75 |
| m8n8k32 | s4,u4 | s32 | 75 |
| m16n8k128 | s4,u4 | s32 | 75 |
| m8n8k128 | b1 | s32 | 75 |
| m16n8k256 | b1 | s32 | 75 |
| tcgen05 (10 variants) | mxf8f6f4,mxf4,f16,bf16,tf32,i8,fp4 | varies | 100 |

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
