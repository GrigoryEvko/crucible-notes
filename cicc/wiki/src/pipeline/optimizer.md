# LLVM Optimizer

Pass pipeline assembly, two-phase compilation, NVVMPassOptions knob system, and New PM integration. Address range `0x12D0000`–`0x16FFFFF` (~4.2 MB of code).

| | |
|---|---|
| **Pipeline assembler** | `sub_12E54A0` (49.8KB, ~150 pass insertions) |
| **Phase orchestrator** | `sub_12E7E70` (9.4KB, Phase I / Phase II) |
| **Top-level entry** | `sub_12E1EF0` (51.3KB, jobserver + split-module) |
| **PassOptions init** | `sub_12D6300` (125KB, 222 knob slots) |
| **Target creation** | `sub_12EA530` (4.1KB, `"nvptx"` / `"nvptx64"`) |
| **LTO pipeline** | `sub_12F5F30` (37.8KB, dead kernel elimination) |
| **Pass count** | ~402 LLVM + 25+ NVIDIA custom |
| **Dual PM** | Both Legacy PM and New PM supported |
| **jemalloc** | 5.3.x statically linked (~400 functions at `0x12FC000`) |

## Architecture

```
sub_12E1EF0 (51KB, top-level entry)
  │  ├─ GNU Jobserver integration (parallel builds)
  │  ├─ Bitcode reading + verification (sub_153BF40)
  │  └─ Split-module support ("<split-module>")
  │
  ├─ sub_12E7E70 (9.4KB, two-phase orchestrator)
  │    ├─ Phase I → sub_12E54A0 (phase=1)
  │    ├─ Phase II → sub_12E54A0 (phase=2)
  │    └─ qword_4FBB3B0 phase counter (1→2→3)
  │
  └─ sub_12E54A0 (49.8KB, MASTER PIPELINE ASSEMBLY)
       ├─ sub_12EA530 (create "nvptx64" target machine)
       ├─ sub_14A04B0 (TargetLibraryInfo setup)
       ├─ sub_149CBC0 (SubtargetInfo setup)
       ├─ sub_1BFB9A0 (TargetTransformInfo setup)
       ├─ sub_12DE0B0 ×150+ (AddPass: insert each pass)
       ├─ sub_12DE8F0 (standard sub-pipelines, tiers 1-3)
       ├─ sub_12DE330 (conditional NVIDIA pass injection)
       ├─ sub_12DFE00 (extended pass injection)
       └─ sub_12EB010 (finalization + output generation)
```

## Pipeline Assembly — `sub_12E54A0`

| Field | Value |
|---|---|
| Address | `0x12E54A0` |
| Size | 49.8KB |
| Pass insertions | ~150 calls to `sub_12DE0B0` |

The critical function that assembles the entire LLVM pass pipeline. Called from `sub_12E7E70` for each phase.

### Target Machine Setup

1. Creates `"nvptx"` or `"nvptx64"` triple
2. Calls `sub_12EA530` — `TargetRegistry::lookupTarget` (error: `"Failed to locate nvptx target\n"`)
3. Creates target machine via vtable dispatch (+88)
4. Sets up `TargetLibraryInfo`, `SubtargetInfo`, `TargetTransformInfo`

### AddPass — `sub_12DE0B0`

| Field | Value |
|---|---|
| Address | `0x12DE0B0` |
| Size | 3.5KB |
| Signature | `(pipeline, pass_object, is_module_pass, is_required)` |

Called ~150 times from `sub_12E54A0`. Central function for inserting passes into the pipeline.

### Standard Sub-Pipelines — `sub_12DE8F0`

| Field | Value |
|---|---|
| Address | `0x12DE8F0` |
| Size | 17.9KB |

Builds sub-pipelines for each optimization tier. Known pass factory calls:

| Factory Address | Likely Pass |
|---|---|
| `sub_1CB4E40` | DeadCodeElimination |
| `sub_1A223D0` | SROA / Mem2Reg |
| `sub_1C98160` | InstructionCombiner |
| `sub_17060B0` | VerifierPass (with param) |
| `sub_12D4560` | NVIDIA custom pass |
| `sub_19B73C0` | LICM-like (7 params) |
| `sub_1C8E680` | Function pass |
| `sub_18FD350` | Late function pass |
| `sub_1A62BF0` | Loop pass (8 params, called multiple times) |
| `sub_195E880` | Loop pass |
| `sub_190BB10` | Loop pass |
| `sub_1952F90` | Loop pass |

References `qword_4FBB3B0` (phase counter) and `qword_4FBB370` (feature flags).

### Conditional NVIDIA Pass Injection — `sub_12DE330`

| Field | Value |
|---|---|
| Address | `0x12DE330` |
| Size | 4.8KB |

Reads NVIDIA pass options and conditionally inserts passes based on option flags and optimization level.

### Extended Pass Injection — `sub_12DFE00`

| Field | Value |
|---|---|
| Address | `0x12DFE00` |
| Size | 20.7KB |

Handles architecture-specific passes and special NVIDIA passes.

## Two-Phase Compilation — `sub_12E7E70`

| Field | Value |
|---|---|
| Address | `0x12E7E70` |
| Size | 9.4KB |
| Strings | `"Phase I"`, `"Phase II"`, `"Concurrent=Yes/No"` |

Implements an **iterative compilation model** not present in upstream LLVM:

1. Check thread count and concurrent compilation flag
2. **Phase I**: call `sub_12E54A0` with `phase=1`
3. **Phase II**: call `sub_12E54A0` with `phase=2`
4. Phase counter `qword_4FBB3B0`: values 1 → 2 → 3 (done)
5. If single-threaded, collapses to a single call

Worker function `sub_12E7B90` (3.0KB) dispatches to `sub_12E7E70` with concurrent mode enabled when thread count > 1.

## NVVMPassOptions — `sub_12D6300`

| Field | Value |
|---|---|
| Address | `0x12D6300` |
| Size | 125KB (largest function in the range) |
| Option slots | 222 (indices 1 through 0xDD) |
| Struct size | ~4,480 bytes (offsets up to a1+4464) |

NVIDIA's proprietary per-pass knob system. Table-driven architecture that controls all 222 optimization pass behaviors.

### Access Pattern

```c
for (index = 1; index <= 0xDD; index++) {
    sub_12D6170(base+120, index);    // fetch pass option descriptor
    sub_1691920(base+8, index);      // fetch pass option value
    sub_12D6090(a1+offset, ...);     // store string-typed option
    sub_12D6100(a1+offset, ...);     // store integer-typed option
    sub_12D6240(a1, index, "0");     // get option with default
}
```

### Helper Functions

| Function | Purpose |
|---|---|
| `sub_12D6170` | Pass option name lookup by index |
| `sub_12D6090` | Store string pass option |
| `sub_12D6100` | Store integer pass option |
| `sub_12D6240` | Get pass option with default value (`"0"` or `"1"`) |

## Optimization Levels

The `nvopt` pipeline builder supports multiple tiers:

| Level | Name | Description |
|---|---|---|
| O0 | `nvopt<O0>` | No optimization |
| O1 | `nvopt<O1>` | Basic optimization |
| O2 | `nvopt<O2>` | Standard optimization |
| O3 | `nvopt<O3>` | Aggressive optimization |
| Ofcmin | `nvopt<Ofcmin>` | Fast-compile minimum |
| Ofcmid | `nvopt<Ofcmid>` | Fast-compile medium |
| Ofcmax | `nvopt<Ofcmax>` | Fast-compile maximum |

These are custom pipeline names wrapping LLVM New PM infrastructure.

## New PassManager Integration

### NVIDIA Custom Module-Level Passes

| Pass Name | Pass Class | Purpose |
|---|---|---|
| `check-gep-index` | — | GEP index validation |
| `check-kernel-functions` | `NVPTXSetFunctionLinkagesPass` | Kernel function linkage |
| `cnp-launch-check` | `CNPLaunchCheckPass` | Cooperative launch validation |
| `ipmsp` | `IPMSPPass` | Inter-procedural memory space |
| `nv-early-inliner` | — | NVIDIA early inlining |
| `nv-inline-must` | `InlineMustPass` | Force-inline functions |
| `nvvm-pretreat` | `PretreatPass` | IR pre-treatment |
| `nvvm-verify` | `NVVMIRVerifierPass` | NVVM IR verification |
| `printf-lowering` | `PrintfLoweringPass` | Device printf → vprintf |
| `select-kernels` | `SelectKernelsPass` | Kernel selection |

### NVIDIA Custom Parameterized Module Passes

| Pass Name | Parameters |
|---|---|
| `set-global-array-alignment` | `modify-shared-mem`, `skip-shared-mem`, `modify-global-mem`, `skip-global-mem` |
| `memory-space-opt` | `first-time`, `second-time`, `no-warnings`, `warnings` |
| `lower-aggr-copies` | `lower-aggr-func-args` |
| `lower-struct-args` | `opt-byval` |
| `process-restrict` | `propagate-only` |

### NVIDIA Custom Function-Level Passes

| Pass Name | Purpose |
|---|---|
| `branch-dist` | Branch distribution |
| `nvvm-reflect-pp` | NVVM reflect preprocessor |
| `nvvm-peephole-optimizer` | NVVM-specific peephole |
| `remat` | IR-level rematerialization |
| `reuse-local-memory` | Local memory reuse |
| `set-local-array-alignment` | Local array alignment |
| `sinking2` | NVIDIA sinking pass |

### NVIDIA Custom Analyses

| Analysis Name | Purpose |
|---|---|
| `rpa` | Register Pressure Analysis |
| `merge-sets` | Merge set computation |

### NVIDIA Custom Loop Pass

| Pass Name | Purpose |
|---|---|
| `loop-index-split` | Loop index splitting |

## LTO Pipeline — `sub_12F5F30`

| Field | Value |
|---|---|
| Address | `0x12F5F30` |
| Size | 37.8KB |

Implements CUDA-specific Link-Time Optimization:

- Processes `"llvm.used"` and `"llvm.metadata"` sections
- Dead kernel elimination: `"no reference to kernel "`
- Dead variable elimination: `"no reference to variable "`
- Flags: `--device-c`, `--force-device-c`, `--trace`
- Host reference tracking: `-host-ref-ec=`, `-host-ref-eg=`, `-host-ref-ek=`, `-host-ref-ic=`, `-host-ref-ig=`, `-host-ref-ik=`
- `-optimize-unused-variables` flag

## NVPTX Target Machine — `sub_12F4060`

| Field | Value |
|---|---|
| Address | `0x12F4060` |
| Size | 15.7KB |

Creates NVPTX TargetMachine with NVIDIA-specific features:

| Feature | Description |
|---|---|
| `sharedmem32bitptr` | 32-bit shared memory pointers |
| `fma-level=` | FMA contraction level |
| `prec-divf32=` | Float32 division precision |
| `prec-sqrtf32=` | Float32 sqrt precision |
| `-llcO#` | Optimization level (errors on multiple specifications) |

## Dual Pass Manager Support

cicc v13.0 maintains **both Legacy PM and New PM** option registrations simultaneously:

| Aspect | Legacy PM | New PM |
|---|---|---|
| Name registration | `sub_C53080` / `sub_C53130` | `sub_16B8280` / `sub_16B88A0` |
| Error reporting | `sub_C53280` | `sub_16B1F90` |
| Base vtable | `unk_49DC150` | `unk_49EED30` |
| Bool vtable | `unk_49DC090` | `unk_49EEC70` |
| Unsigned vtable | `unk_49DBF10` | `unk_49EEAF0` |
| ID counter | `sub_C523C0` (atomic) | `unk_4FA0230` (atomic) |

Evidence: identical options (`force-summary-edges-cold`, `memdep-block-scan-limit`, `verify-scev`, etc.) registered twice with different infrastructure.

## jemalloc Allocator

~400 functions at `0x12FC000`–`0x131FFFF`. jemalloc 5.3.x statically linked, replacing the system allocator.

| Function | Size | Purpose |
|---|---|---|
| `sub_12FCDB0` | 132KB | `malloc_conf` parser (largest in cluster) |
| `sub_1307610` | 21KB | tcache allocation (large) |
| `sub_13010C0` | 16KB | tcache allocation (small) |
| `sub_1308BE0` | 8KB | tcache flush |
| `sub_13022D0` | 7KB | Arena initialization |

Configurable via `MALLOC_CONF` environment variable with dozens of options (abort, cache_oblivious, metadata_thp, trust_madvise, retain, dss, tcache, narenas, etc.).

## LLVM Analysis Infrastructure

Address range `0x1380000`–`0x14FFFFF` contains stock LLVM analysis passes with **no visible NVIDIA modifications**:

| Analysis | Key Function | Size |
|---|---|---|
| CFL-Andersen AA | `sub_138AAF0` | 75KB |
| CallGraph | `sub_1394520` | 24KB |
| DemandedBits | `sub_139F940` | 28KB |
| DependenceInfo | `sub_13B41E0` | 42KB |
| DominatorTree | `sub_13BFEC0` | 18KB |
| GlobalsAA | `sub_13C7380` | 36KB |
| LazyValueInfo | `sub_13EFEC0` | 44KB |
| LoopInfo | `sub_13FE280` | 10KB |
| ScalarEvolution | `sub_13D9330` | 54KB |

**Exception**: CUTLASS-specific alias analysis at `sub_1414D30` (9KB) — checks function names for `"cutlass"` substring and applies specialized memory ordering. Guarded by `byte_4F99740`.

## NVIDIA Constant Folding Extensions

| Function | Size | Purpose |
|---|---|---|
| `sub_14D1BC0` | 54KB | Constant-fold 40+ CUDA math functions (both float/double + `__finite` variants) |
| `sub_14D90D0` | 27KB | Constant-fold NVVM intrinsic opcodes |
| `sub_149E420` | 26KB | Vector math library mapping (scalar → `vceilf`, `vsqrtf`, `vexpf`, etc.) |
| `sub_14B5970` | 41KB | `"BadAssumption"` diagnostic — warns on conflicting `@llvm.assume()` calls |

## Key Global Variables

| Variable | Purpose |
|---|---|
| `qword_4FBB3B0` | Compilation phase counter (1=Phase I, 2=Phase II, 3=done) |
| `qword_4FBB370` | Feature flag register (NVIDIA-specific feature gates) |
| `qword_4FBB410` | Pass execution counter |
| `qword_4FBB430` | Optimization level store |
| `qword_4FBB510` | Debug/trace verbosity level |
| `byte_3F871B3` | NVIDIA global flag byte (checked in pipeline assembly) |
| `byte_4F99740` | CUTLASS optimization enable flag |
