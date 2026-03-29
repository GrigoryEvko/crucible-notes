# PTX Emission

PTX assembly output, function headers, stack frames, register declarations, special registers, atomic instructions, barriers, debug info, and output modes. Address range `0x2140000`–`0x21FFFFF` for NVPTX-specific emission, `0x31E0000`–`0x3240000` for AsmPrinter.

| | |
|---|---|
| **AsmPrinter::emitFunctionBody** | `sub_31EC4F0` (72KB) |
| **Function header orchestrator** | `sub_215A3C0` (.entry/.func, .param, kernel attrs, .pragma) |
| **Kernel attribute emission** | `sub_214DA90` (.reqntid, .maxntid, .minnctapersm, cluster) |
| **Stack frame setup** | `sub_2158E80` (17KB, .local, .reg, `__local_depot`) |
| **Register class map** | `sub_2163730` + `sub_21638D0` (9 classes) |
| **GenericToNVVM** | `sub_215DC20` / `sub_215E100` (36KB, addrspace rewriting) |
| **Special registers** | `sub_21E86B0` (%tid, %ctaid, %ntid, %nctaid) |
| **Cluster registers** | `sub_21E9060` (15 registers, SM 90+) |
| **Atomic emission** | `sub_21E5E70` (13 opcodes) + `sub_21E6420` (L2 cache hints) |
| **Memory barriers** | `sub_21E94F0` (membar.cta/gpu/sys, fence.sc.cluster) |
| **Cluster barriers** | `sub_21E8EA0` (barrier.cluster.arrive/wait) |
| **Global variable emission** | `sub_2156420` (texref/surfref/samplerref/data) |
| **Bitcode producer** | `"LLVM7.0.1"` (NVVM IR compat marker, despite LLVM 20.0.0) |

## Function Header Emission — `sub_215A3C0`

Emits a complete PTX function prologue in this exact order:

| Step | Output | Condition |
|---|---|---|
| (a) | `.pragma "coroutine";\n` | Metadata node type `'N'` linked to current function |
| (b) | CUDA-specific attributes | `*(a1+232)->field_952 == 1` |
| (c) | `.entry ` or `.func ` | `sub_1C2F070` (isKernelFunction) |
| (d) | Return type spec | `.func` only, via `sub_214C940` |
| (e) | Mangled function name | `sub_214D1D0` |
| (f) | `.param` declarations | `sub_21502D0` (monotonic counter `_param_0`, `_param_1`, ...) |
| (g) | Kernel attributes | `.entry` only, via `sub_214DA90` |
| (h) | Additional attributes | `sub_214E300` |
| (i) | `.noreturn` | Non-kernel with noreturn attribute (metadata attr 29) |
| (j) | `{\n` | Open function body |
| (k) | Stack frame + registers | `sub_2158E80` |
| (l) | DWARF debug info | If enabled |

## Kernel Attributes — `sub_214DA90`

Reads NVVM metadata and emits performance-tuning directives. Attribute emission order:

| Order | Attribute | Source Metadata | Condition |
|---|---|---|---|
| 1 | `.blocksareclusters` | `nvvm.blocksareclusters` | Fatal if reqntid not set |
| 2 | `.reqntid X, Y, Z` | `nvvm.reqntid` + `sub_1C2EDB0` | Comma-separated strtol parse |
| 3 | `.maxntid X, Y, Z` | `sub_1C2EC00` / structured | Unspecified dims default to 1 |
| 4 | `.minnctapersm N` | `sub_1C2EF70` | — |
| 5 | `.explicitcluster` | `nvvm.cluster_dim` | SM > 89 only |
| 6 | `.reqnctapercluster X, Y, Z` | Cluster dim readers | SM > 89 only |
| 7 | `.maxclusterrank N` | `sub_1C2EF50` | SM > 89 only |
| 8 | `.maxnreg N` | `sub_1C2EF90` | — |

Cluster attributes (5–7) gated by `*(a1+232)->field_1212 > 0x59` (SM > 89, i.e., SM 90+).

## Stack Frame — `sub_2158E80`

| Field | Value |
|---|---|
| Address | `0x2158E80` |
| Size | 17KB |

### Emission Steps

1. **Local depot** (if `*(frame_info+48) != 0`):
   ```ptx
   .local .align 16 .b8 __local_depot0[256];
   ```
   Where alignment = `*(frame_info+60)`, index = function index, size = frame size.

2. **Stack pointer registers**:
   ```ptx
   .reg .b64 %SP;    // stack pointer
   .reg .b64 %SPL;   // stack pointer local
   ```
   Uses `.b32` in 32-bit mode (checked via `*(a2+8)->field_936`).

3. **Virtual register declarations** — iterates register map at `*(a1+800)`, deduplicates via hash table at `a1+808`:
   ```ptx
   .reg .pred  %p<5>;
   .reg .b16   %rs<12>;
   .reg .b32   %r<47>;
   .reg .b64   %rd<8>;
   .reg .f32   %f<20>;
   .reg .f64   %fd<3>;
   ```

## Register Class Map — Complete

9 register classes with vtable addresses, PTX type suffixes, register prefixes, and encoded IDs:

| Vtable | Class | PTX Type | Prefix | Encoded ID |
|---|---|---|---|---|
| `off_4A027A0` | Int1Regs | `.pred` | `%p` | `0x10000000` |
| `off_4A02720` | Int16Regs | `.b16` | `%rs` | `0x20000000` |
| `off_4A025A0` | Int32Regs | `.b32` | `%r` | `0x30000000` |
| `off_4A024A0` | Int64Regs | `.b64` | `%rd` | `0x40000000` |
| `off_4A02620` | Float32Regs | `.f32` | `%f` | `0x50000000` |
| `off_4A02520` | Float64Regs | `.f64` | `%fd` | `0x60000000` |
| `off_4A02760` | Int16HalfRegs | `.b16` | `%h` | `0x70000000` |
| `off_4A026A0` | Int32HalfRegs | `.b32` | `%hh` | `0x80000000` |
| `off_4A02460` | Int128Regs | `.b128` | `%rq` | `0x90000000` |

Encoding in `sub_21583D0`: `class_encoded_id | (register_index & 0x0FFFFFFF)`. Fatal `"Bad register class"` on unrecognized vtable.

## Special Registers — `sub_21E86B0`

Switch on operand value (ASCII-encoded):

| Opcode | Char | Register | Description |
|---|---|---|---|
| `0x26` | `&` | `%tid.x` | Thread ID, X |
| `0x27` | `'` | `%tid.y` | Thread ID, Y |
| `0x28` | `(` | `%tid.z` | Thread ID, Z |
| `0x29` | `)` | `%ntid.x` | Block dim, X |
| `0x2A` | `*` | `%ntid.y` | Block dim, Y |
| `0x2B` | `+` | `%ntid.z` | Block dim, Z |
| `0x2C` | `,` | `%ctaid.x` | Block ID, X |
| `0x2D` | `-` | `%ctaid.y` | Block ID, Y |
| `0x2E` | `.` | `%ctaid.z` | Block ID, Z |
| `0x2F` | `/` | `%nctaid.x` | Grid dim, X |
| `0x30` | `0` | `%nctaid.y` | Grid dim, Y |
| `0x31` | `1` | `%nctaid.z` | Grid dim, Z |
| `0x5E` | `^` | (dynamic) | Via `sub_3958DA0(0, ...)` — %warpid/%laneid |
| `0x5F` | `_` | (dynamic) | Via `sub_3958DA0(1, ...)` |

### Cluster Registers — `sub_21E9060` (SM 90+)

| Value | Register | Description |
|---|---|---|
| 0 | `%is_explicit_cluster` | Explicit cluster flag |
| 1 | `%cluster_ctarank` | CTA rank within cluster |
| 2 | `%cluster_nctarank` | CTAs in cluster |
| 3–5 | `%cluster_nctaid.{x,y,z}` | Cluster grid dimensions |
| 6–8 | `%cluster_ctaid.{x,y,z}` | CTA ID within cluster |
| 9–11 | `%nclusterid.{x,y,z}` | Number of clusters |
| 12–14 | `%clusterid.{x,y,z}` | Cluster ID |

Fatal: `"Unhandled cluster info operand"` on invalid value.

## Atomic Instruction Emission

### Base Atomics — `sub_21E5E70`

Operand encoding: bits[7:4] = scope (0=gpu, 1=cta, 2=sys), BYTE2 = atomic opcode.

| Opcode | Suffix | Type |
|---|---|---|
| `0x00` | `.exch.b` | Bitwise exchange |
| `0x01` | `.add.u` | Unsigned add |
| `0x03` | `.and.b` | Bitwise AND |
| `0x05` | `.or.b` | Bitwise OR |
| `0x06` | `.xor.b` | Bitwise XOR |
| `0x07` | `.max.s` | Signed max |
| `0x08` | `.min.s` | Signed min |
| `0x09` | `.max.u` | Unsigned max |
| `0x0A` | `.min.u` | Unsigned min |
| `0x0B` | `.add.f` | Float add |
| `0x0C` | `.inc.u` | Unsigned increment |
| `0x0D` | `.dec.u` | Unsigned decrement |
| `0x0E` | `.cas.b` | Compare-and-swap |

Opcodes 0x02 and 0x04 are intentionally absent — matches PTX ISA.

### L2 Cache-Hinted Atomics — `sub_21E6420` (Ampere+)

Parallel function inserting `L2::cache_hint` between operation and type: `atom[.scope].op.L2::cache_hint.type`. All 13 operations supported. Uses SSE `xmmword` loads from precomputed constants at `xmmword_435F590`–`xmmword_435F620`.

## Memory Barriers — `sub_21E94F0`

| Value | Instruction | Scope |
|---|---|---|
| 0 | `membar.gpu` | Device |
| 1 | `membar.cta` | Block |
| 2 | `membar.sys` | System |
| 4 | `fence.sc.cluster` | Cluster (SM 90+) |
| 3 | — | Fatal: `"Bad membar op"` |

## Cluster Barriers — `sub_21E8EA0` (SM 90+)

Encoding: bits[3:0] = operation (0=arrive, 1=wait), bits[7:4] = ordering (0=default, 1=relaxed).

| Instruction | Meaning |
|---|---|
| `barrier.cluster.arrive` | Signal arrival |
| `barrier.cluster.arrive.relaxed` | Relaxed-memory arrival |
| `barrier.cluster.wait` | Wait for all CTAs |
| `barrier.cluster.wait.relaxed` | Relaxed-memory wait |

## GenericToNVVM — `sub_215DC20`

| Field | Value |
|---|---|
| Pass name | `"generic-to-nvvm"` |
| Description | `"Ensure that the global variables are in the global address space"` |
| Pass ID | `unk_4FD155C` |
| Factory | `sub_215D530` (allocates 320-byte state with two DenseMaps) |

For each GlobalVariable in addrspace(0):
1. Clone to addrspace(1) (global memory)
2. Insert `addrspacecast` from new global back to original type
3. RAUW (replace all uses with) the cast
4. Erase original global

## Global Constructor Rejection — `sub_215ACD0`

```c
if (lookup("llvm.global_ctors") && type_tag == ArrayType && count != 0)
    fatal("Module has a nontrivial global ctor, which NVPTX does not support.");
if (lookup("llvm.global_dtors") && type_tag == ArrayType && count != 0)
    fatal("Module has a nontrivial global dtor, which NVPTX does not support.");
```

GPU kernels have no "program startup" phase — no `__crt_init` equivalent. Static initialization with non-trivial constructors is incompatible with the GPU execution model.

## Global Variable Emission — `sub_2156420`

Skipped globals: `"llvm.metadata"`, `"llvm.*"`, `"nvvm.*"`.

| Global Type | PTX Output |
|---|---|
| Texture reference | `.global .texref NAME;` |
| Surface reference | `.global .surfref NAME;` |
| Sampler reference | `.global .samplerref NAME = { addr_mode_0 = ..., filter_mode = ..., ... }` |
| Managed memory | `.attribute(.managed)` |
| Demoted (addrspace 3) | `// NAME has been demoted` (comment only) |

## Output Modes

| Mode | Flag | Output |
|---|---|---|
| PTX text | (default) | `.ptx` assembly file |
| LLVM bitcode | `--emit-llvm-bc` | `.bc` bitcode file |
| OptiX IR | `--emit-optix-ir` | `.optixir` file |
| LTO bitcode | `-gen-lto` / `-link-lto` | LTO-compatible `.bc` |
| Split compile | `-split-compile=N` | Multiple files (`F%d_B%d` naming) |

### Bitcode Producer ID

The bitcode writer (`sub_1538EC0`, 58KB) stamps `"LLVM7.0.1"` as the producer string despite being built on LLVM 20.0.0. This is the **NVVM IR compatibility marker**. Override: `LLVM_OVERRIDE_PRODUCER` env var (checked in `ctor_154` at `0x4CE640`).

## Address Space Operations — `sub_21E7FE0`

Multi-purpose helper for cvta, MMA operands, and address space qualifiers:

| Query | Values | Output |
|---|---|---|
| `"addsp"` | 0=generic, 1=.global, 3=.shared, 4+=.local | cvta address space suffix |
| `"ab"` | 0="a", 1="b" | cvta direction |
| `"rowcol"` | 0="row", 1="col" | MMA layout |
| `"mmarowcol"` | 0–3 | "row.row"/"row.col"/"col.row"/"col.col" |
| `"satf"` | 0=(none), 1=".satfinite" | MMA saturation |
| `"abtype"` | 0–6 | "u8"/"s8"/"u4"/"s4"/"b1"/"bf16"/"tf32" |
| `"trans"` | 0=(none), 1=".trans" | WGMMA transpose |

## Key Global Variables

| Variable | Purpose |
|---|---|
| `byte_4FD17C0` | Pass configuration flag |
| `byte_4FD16E0` | ISel dump enable |
| `byte_4FD2160` | Extra ISel pass enable |
| `dword_4FD26A0` | Scheduling mode (1=simple, else=full pipeline) |
| `unk_4FD155C` | GenericToNVVM pass ID |
