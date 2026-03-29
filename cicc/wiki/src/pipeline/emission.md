# PTX Emission

PTX assembly output, function headers, stack frames, special registers, atomic instructions, debug info, and output modes. Address range `0x2140000`–`0x21FFFFF` for NVPTX-specific emission, `0x31E0000`–`0x3240000` for AsmPrinter.

| | |
|---|---|
| **AsmPrinter::emitFunctionBody** | `sub_31EC4F0` (72KB) |
| **PTX function header** | `sub_214DA90` (`.entry` / `.func`, `.param`, kernel attributes) |
| **Stack frame setup** | `sub_2158E80` (`.local .align`, `.reg`, `__local_depot`) |
| **GenericToNVVM** | `sub_215DC20` / `sub_215E100` (36KB, address space rewriting) |
| **Special registers** | `sub_21E86B0` (`%tid`, `%ctaid`, `%ntid`, `%nctaid`) |
| **Atomic emission** | `sub_21E5E70` (`.exch`, `.add`, `.cas`, L2 cache hints) |
| **Bitcode producer** | `"LLVM7.0.1"` (compatibility marker, despite LLVM 20.0.0 internals) |

## Architecture

```
MachineFunction
  │
  ├─ sub_31EC4F0 (AsmPrinter::emitFunctionBody, 72KB)
  │    ├─ Iterate MachineInstrs
  │    ├─ Emit assembly text
  │    ├─ Handle debug info / DWARF
  │    └─ Instruction count / mix reporting
  │
  ├─ sub_214DA90 (PTX function header)
  │    ├─ .entry / .func declaration
  │    ├─ .param declarations
  │    └─ Kernel attributes (.maxntid, .reqntid, .minnctapersm, etc.)
  │
  ├─ sub_2158E80 (Stack frame)
  │    ├─ .local .align N .b8 __local_depotX[SIZE]
  │    ├─ .reg .b64 %SP / %SPL
  │    └─ Register declarations (.reg .bN %rN<count>)
  │
  └─ MCStreamer → PTX text output
```

## PTX Function Headers — `sub_214DA90`

### Kernel vs Device Function

| Directive | Meaning |
|---|---|
| `.entry` | Kernel function (callable from host) |
| `.func` | Device function (callable from device only) |

### Parameter Declarations

Format: `.param .align N .b8 name[SIZE]`

Parameter name generation uses monotonic counter (`a1[134256]`), producing `_param_0`, `_param_1`, etc.

### Kernel Attributes

| Attribute | Purpose | Source Metadata |
|---|---|---|
| `.maxntid` | Max threads per block | `nvvm.maxntid` (`__launch_bounds__`) |
| `.reqntid` | Required threads per block | `nvvm.reqntid` |
| `.minnctapersm` | Min CTAs per SM | `nvvm.minctasm` |
| `.maxnreg` | Max register count | `nvvm.maxnreg` |
| `.cluster_dim` | Cluster dimensions | `nvvm.cluster_dim` (Hopper+) |
| `.maxclusterrank` | Max cluster rank | `nvvm.maxclusterrank` (Hopper+) |
| `.reqnctapercluster` | Required CTAs per cluster | Hopper+ |
| `.explicitcluster` | Explicit cluster launch | Hopper+ |
| `.blocksareclusters` | Blocks are clusters | `nvvm.blocksareclusters` (Hopper+) |
| `.noreturn` | Function does not return | |

Pragma emission: `"\t.pragma "` via `sub_215A3C0` / `sub_215AC60`.

## Stack Frame Emission — `sub_2158E80`

| Field | Value |
|---|---|
| Address | `0x2158E80` |
| Size | 17KB |

```ptx
.local .align 16 .b8 __local_depot0[256];   // stack frame
.reg .b64 %SP;                               // stack pointer
.reg .b64 %SPL;                              // stack pointer low
.reg .b32 %r<128>;                           // general registers
.reg .pred %p<16>;                           // predicate registers
```

Register declarations emit all register classes with their counts.

## Instruction Emission

| PTX Instruction | Emitter | Notes |
|---|---|---|
| `bra.uni` | `sub_215BB80` | Unconditional branch |
| `.pragma` | `sub_2158BD0` / `sub_215AC60` | Per-function pragmas |
| Inline ASM | `sub_21BC460` | `" begin inline asm"` / `" end inline asm"` comments |

Error register: `"%ERROR"` emitted by `sub_215BA10` / `sub_215BB50` for invalid register references.

`"Bad register class"` at `sub_21583D0`, `"Unsupported FP type"` at `sub_2158820`.

## Address Space Operations — `sub_21E7FE0`

| PTX Instruction | Meaning |
|---|---|
| `cvta.to.shared` | Convert to shared address space |
| `cvta.to.local` | Convert to local address space |
| `cvta.to.global` | Convert to global address space |
| `cvta.to.param` | Convert to parameter address space |
| `addsp` | Stack pointer offset computation |

### GenericToNVVM — `sub_215DC20`

| Field | Value |
|---|---|
| Address | `0x215DC20` |
| Pass name | `"generic-to-nvvm"` |
| Description | `"Ensure that the global variables are in the global address space"` |

`sub_215E100` (36KB): Main pass body — rewrites all address-space-cast operations for every global variable.

### Redundant cvta Removal — `sub_21DA810`

`"NVPTX optimize redundant cvta.to.local instruction"` — removes redundant conversions to local address space.

## Special Registers — `sub_21E86B0`

| PTX Register | Meaning |
|---|---|
| `%tid.x` / `%tid.y` / `%tid.z` | Thread ID within block |
| `%ntid.x` / `%ntid.y` / `%ntid.z` | Block dimensions |
| `%ctaid.x` / `%ctaid.y` / `%ctaid.z` | Block ID within grid |
| `%nctaid.x` / `%nctaid.y` / `%nctaid.z` | Grid dimensions |

### Hopper Cluster Registers — `sub_21E9060`

| PTX Register | Meaning |
|---|---|
| `%is_explicit_cluster` | Explicit cluster flag |
| `%cluster_ctarank` | CTA rank within cluster |
| `%cluster_nctarank` | Number of CTAs in cluster |
| `%cluster_ctaid.x/y/z` | CTA ID within cluster |
| `%clusterid.x/y/z` | Cluster ID |
| `%cluster_nctaid.x/y/z` | Cluster grid dimensions |
| `%nclusterid.x/y/z` | Number of clusters |

## Atomic Instruction Emission — `sub_21E5E70`

### Standard Atomics

| Suffix | Operation |
|---|---|
| `.exch.b` | Exchange (bitwise) |
| `.add.u` | Add (unsigned) |
| `.and.b` | AND (bitwise) |
| `.or.b` | OR (bitwise) |
| `.xor.b` | XOR (bitwise) |
| `.max.u` | Maximum (unsigned) |
| `.min.u` | Minimum (unsigned) |
| `.cas.b` | Compare-and-swap (bitwise) |
| `.inc.u` | Increment (unsigned) |
| `.dec.u` | Decrement (unsigned) |

### L2 Cache-Hinted Atomics (Ampere+) — `sub_21E6420`

| Suffix | Operation |
|---|---|
| `.exch.L2::cache_hint.b` | Exchange with L2 hint |
| `.add.L2::cache_hint.u` | Add with L2 hint |
| `.and.L2::cache_hint.b` | AND with L2 hint |
| `.cas.L2::cache_hint.b` | CAS with L2 hint |

## Memory Barriers — `sub_21E94F0`

| PTX Instruction | Scope |
|---|---|
| `membar.cta` | Block-level fence |
| `membar.gpu` | Device-level fence |
| `membar.sys` | System-level fence |
| `fence.sc.cluster` | Cluster-scope fence (Hopper+) |

## Cluster Barriers — `sub_21E8EA0`

| PTX Instruction | Meaning |
|---|---|
| `barrier.cluster.arrive` | Arrive at cluster barrier |
| `barrier.cluster.wait` | Wait at cluster barrier |
| `.relaxed` | Relaxed memory ordering modifier |

## Global Constructor Check — `sub_215ACD0`

Checks for `"llvm.global_ctors"` / `"llvm.global_dtors"`. Emits error: `"Module has a nontrivial global ctor, which NVPTX does not support."` NVPTX does not support global constructors/destructors natively.

Also handles `"NVPTX Debug Info Emission"` / `"NVPTX DWARF Debug Writer"`.

## Debug Info

| Function | Purpose |
|---|---|
| `sub_216EF30` | `"Function too large, generated debug information may not be accurate."` |
| `sub_215ACD0` | DWARF debug writer initialization |
| DWARF emission cluster | `0x3990000`–`0x39BF000` (accel tables, form sizes, ranges) |

## Register Pressure Reporting — `sub_21E9A60`

Custom NVIDIA diagnostic: `"Max Live RRegs: "`, `"\tPRegs: "`, `"Function Size: "`. Machine function extra info printer registered as `"extra-machineinstr-printer"`.

## Output Modes

| Mode | Flag | Output |
|---|---|---|
| PTX text | (default) | `.ptx` assembly file |
| LLVM bitcode | `--emit-llvm-bc` | `.bc` bitcode file |
| OptiX IR | `--emit-optix-ir` | `.optixir` file |
| LTO bitcode | `-gen-lto` / `-link-lto` | LTO-compatible `.bc` |
| Split compile | `-split-compile=N` | Multiple output files (`F%d_B%d` naming) |

### Bitcode Producer ID

The bitcode writer (`sub_1538EC0`, 58KB) writes `"LLVM7.0.1"` as the producer identification string, despite being built on LLVM 20.0.0 internally. This is the **NVVM IR compatibility marker** — ensures the bitcode format conforms to NVVM IR spec based on LLVM 7.0.1 structure.

The `LLVM_OVERRIDE_PRODUCER` environment variable can override this (checked in `ctor_154` at `0x4CE640`).

## Utility Passes

| Pass | Address | Pass ID | Purpose |
|---|---|---|---|
| Alloca Hoisting | `sub_21BC7D0` | `alloca-hoisting` | Move all allocas to entry block (PTX requirement) |
| Valid Global Names | `sub_21BCD80` | `nvptx-assign-valid-global-names` | Sanitize names to valid PTX identifiers |
| Image Optimizer | `sub_21BCF10` | — | Optimize texture/surface access patterns |
| Peephole | `sub_21DB090` | `nvptx-peephole` | NVPTX-specific peephole optimization |
| Prolog/Epilog | `sub_21DB5F0` | — | Custom frame management (no traditional prolog/epilog) |
| Replace Image Handles | `sub_21DBEA0` | — | Replace IR-level image handles with PTX references |
| NVVMIntrRange | `sub_216F4B0` | `nvvm-intr-range` | Add `!range` metadata to NVVM intrinsics |
| setmaxnreg | `sub_21EA5F0` | — | Dynamic register limit (Hopper+) |
| Address Space Validation | `sub_21BEE70` | — | `"Bad address space in addrspacecast"` |

## Key Global Variables

| Variable | Purpose |
|---|---|
| `byte_4FD17C0` | Pass configuration flag |
| `byte_4FD16E0` | ISel dump enable |
| `byte_4FD2160` | Extra ISel pass enable |
| `dword_4FD26A0` | Scheduling mode (1=simple, else=full pipeline) |
