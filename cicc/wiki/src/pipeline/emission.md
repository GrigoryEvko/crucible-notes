# PTX Emission

PTX assembly output, function headers, stack frames, register declarations, special registers, atomic instructions, barriers, debug info, and output modes. Address range `0x2140000`--`0x21FFFFF` for NVPTX-specific emission, `0x31E0000`--`0x3240000` for AsmPrinter.

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
| **Global variable ordering** | `sub_2157D50` (5.9KB, topological sort with circular dependency detection) |
| **Bitcode producer** | `"LLVM7.0.1"` (NVVM IR compat marker, despite LLVM 20.0.0) |

## Function Header Emission -- `sub_215A3C0`

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

## Kernel Attributes -- `sub_214DA90`

Reads NVVM metadata and emits performance-tuning directives. Attribute emission order:

| Order | Attribute | Source Metadata | Condition |
|---|---|---|---|
| 1 | `.blocksareclusters` | `nvvm.blocksareclusters` | Fatal if reqntid not set |
| 2 | `.reqntid X, Y, Z` | `nvvm.reqntid` + `sub_1C2EDB0` | Comma-separated strtol parse |
| 3 | `.maxntid X, Y, Z` | `sub_1C2EC00` / structured | Unspecified dims default to 1 |
| 4 | `.minnctapersm N` | `sub_1C2EF70` | -- |
| 5 | `.explicitcluster` | `nvvm.cluster_dim` | SM > 89 only |
| 6 | `.reqnctapercluster X, Y, Z` | Cluster dim readers | SM > 89 only |
| 7 | `.maxclusterrank N` | `sub_1C2EF50` | SM > 89 only |
| 8 | `.maxnreg N` | `sub_1C2EF90` | -- |

Cluster attributes (5--7) gated by `*(a1+232)->field_1212 > 0x59` (SM > 89, i.e., SM 90+).

## Stack Frame -- `sub_2158E80`

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

3. **Virtual register declarations** -- iterates register map at `*(a1+800)`, deduplicates via hash table at `a1+808`:
   ```ptx
   .reg .pred  %p<5>;
   .reg .b16   %rs<12>;
   .reg .b32   %r<47>;
   .reg .b64   %rd<8>;
   .reg .f32   %f<20>;
   .reg .f64   %fd<3>;
   ```

## Register Class Map -- Complete

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

## Special Registers -- `sub_21E86B0`

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
| `0x5E` | `^` | (dynamic) | Via `sub_3958DA0(0, ...)` -- %warpid/%laneid |
| `0x5F` | `_` | (dynamic) | Via `sub_3958DA0(1, ...)` |

### Cluster Registers -- `sub_21E9060` (SM 90+)

| Value | Register | Description |
|---|---|---|
| 0 | `%is_explicit_cluster` | Explicit cluster flag |
| 1 | `%cluster_ctarank` | CTA rank within cluster |
| 2 | `%cluster_nctarank` | CTAs in cluster |
| 3--5 | `%cluster_nctaid.{x,y,z}` | Cluster grid dimensions |
| 6--8 | `%cluster_ctaid.{x,y,z}` | CTA ID within cluster |
| 9--11 | `%nclusterid.{x,y,z}` | Number of clusters |
| 12--14 | `%clusterid.{x,y,z}` | Cluster ID |

Fatal: `"Unhandled cluster info operand"` on invalid value.

## Atomic Instruction Emission

### Operand Encoding

The atomic instruction word packs scope and operation into a single integer read from the operand array at `*(operand_array + 16*a2 + 8)`:

```
Bit layout:
  [3:0]   — reserved
  [7:4]   — scope: 0=gpu (implicit), 1=cta, 2=sys
  [15:8]  — reserved
  [23:16] — atomic opcode (BYTE2)
```

The scope field emits a prefix before the atomic suffix: scope 0 produces no prefix (implicit `.gpu`), scope 1 emits `".cta"`, scope 2 emits `".sys"`. The complete PTX instruction format is `atom[.scope].op.type`.

### Base Atomics -- `sub_21E5E70`

13-operation dispatch table. The switch on `BYTE2(v4)` selects both the operation suffix and its type class:

| Opcode | Suffix | Type Class | PTX Semantics |
|---|---|---|---|
| `0x00` | `.exch.b` | bitwise | Exchange -- atomically swap value |
| `0x01` | `.add.u` | unsigned | Unsigned integer addition |
| `0x03` | `.and.b` | bitwise | Bitwise AND |
| `0x05` | `.or.b` | bitwise | Bitwise OR |
| `0x06` | `.xor.b` | bitwise | Bitwise XOR |
| `0x07` | `.max.s` | signed | Signed integer maximum |
| `0x08` | `.min.s` | signed | Signed integer minimum |
| `0x09` | `.max.u` | unsigned | Unsigned integer maximum |
| `0x0A` | `.min.u` | unsigned | Unsigned integer minimum |
| `0x0B` | `.add.f` | float | Floating-point addition |
| `0x0C` | `.inc.u` | unsigned | Unsigned increment (wrapping) |
| `0x0D` | `.dec.u` | unsigned | Unsigned decrement (wrapping) |
| `0x0E` | `.cas.b` | bitwise | Compare-and-swap |

Opcodes `0x02` and `0x04` are intentionally absent -- the PTX ISA has no signed atomic add at that slot, and no bitwise operation occupies slot 4. The 13 operations exactly match the PTX `atom` instruction repertoire.

The type width suffix (`.b32`, `.b64`, `.u32`, `.u64`, `.s32`, `.s64`, `.f32`, `.f64`) is appended separately by the instruction printer after the operation suffix, based on the register class of the destination operand.

### L2 Cache-Hinted Atomics -- `sub_21E6420` (Ampere+)

A parallel emission function that inserts `L2::cache_hint` between the operation and type suffix to produce the extended format:

```
atom[.scope].op.L2::cache_hint.type
```

All 13 atomic operations are supported with L2 hints. The hint instructs the GPU L2 cache controller to retain (or evict) the target cache line after the atomic completes -- a data-locality optimization introduced with Ampere (SM 80).

The function uses SSE `xmmword` loads from precomputed string constants at addresses `xmmword_435F590` through `xmmword_435F620` to fast-copy 16-byte prefixes of each suffix string. This avoids per-character string construction: each atomic variant's complete suffix (e.g., `.exch.L2::cache_hint.b` at 22 bytes) is assembled from a 16-byte SSE load of the prefix plus a patched tail. The compiler optimized this into aligned vector moves rather than `memcpy` calls.

### Atomic Emission Pseudocode

```cpp
void emitAtomicOp(raw_ostream &OS, unsigned operand) {
    unsigned scope = (operand >> 4) & 0xF;
    unsigned opcode = (operand >> 16) & 0xFF;  // BYTE2

    OS << "atom";
    if (scope == 1) OS << ".cta";
    else if (scope == 2) OS << ".sys";
    // scope 0 = implicit .gpu, no suffix

    switch (opcode) {
    case 0x00: OS << ".exch.b"; break;
    case 0x01: OS << ".add.u";  break;
    // ... 0x02, 0x04 absent ...
    case 0x03: OS << ".and.b";  break;
    case 0x05: OS << ".or.b";   break;
    case 0x06: OS << ".xor.b";  break;
    case 0x07: OS << ".max.s";  break;
    case 0x08: OS << ".min.s";  break;
    case 0x09: OS << ".max.u";  break;
    case 0x0A: OS << ".min.u";  break;
    case 0x0B: OS << ".add.f";  break;
    case 0x0C: OS << ".inc.u";  break;
    case 0x0D: OS << ".dec.u";  break;
    case 0x0E: OS << ".cas.b";  break;
    }
    // Type width appended by caller
}
```

The L2-hinted variant (`sub_21E6420`) follows identical dispatch logic but emits `.op.L2::cache_hint.type` instead of `.op.type`.

## Memory Barriers -- `sub_21E94F0`

| Value | Instruction | Scope |
|---|---|---|
| 0 | `membar.gpu` | Device |
| 1 | `membar.cta` | Block |
| 2 | `membar.sys` | System |
| 4 | `fence.sc.cluster` | Cluster (SM 90+) |
| 3 | -- | Fatal: `"Bad membar op"` |

## Cluster Barriers -- `sub_21E8EA0` (SM 90+)

Encoding: bits[3:0] = operation (0=arrive, 1=wait), bits[7:4] = ordering (0=default, 1=relaxed).

| Instruction | Meaning |
|---|---|
| `barrier.cluster.arrive` | Signal arrival |
| `barrier.cluster.arrive.relaxed` | Relaxed-memory arrival |
| `barrier.cluster.wait` | Wait for all CTAs |
| `barrier.cluster.wait.relaxed` | Relaxed-memory wait |

## GenericToNVVM -- `sub_215DC20` / `sub_215E100`

### Pass Registration

| Field | Value |
|---|---|
| Pass name | `"generic-to-nvvm"` |
| Description | `"Ensure that the global variables are in the global address space"` |
| Pass ID | `unk_4FD155C` |
| Factory | `sub_215D530` (allocates 320-byte state) |
| Disable knob | `NVVMPassOptions[2200]` (bool) |
| Pipeline position | After InstructionSimplify, before LoopSimplify (position ~22 in optimizer) |

Registration uses a once-init pattern guarded by `dword_4FD1558`. The 80-byte pass descriptor stores the description at offset 0, pass kind `64` (ModulePass) at offset 8, the name string at offset 16, its length `15` at offset 24, the pass ID pointer at offset 32, flags `0` at offset 40, and the factory function pointer at offset 72. Registration dispatches through `sub_163A800` (the LLVM pass registration infrastructure).

A new-pass-manager version also exists: `GenericToNVVMPass`, registered at `sub_305ED20` / `sub_305E2C0` with CLI name `"generic-to-nvvm"`.

### Algorithm -- `sub_215E100` (36KB)

The pass body at `sub_215E100` is 36KB because it must rewrite every address-space-dependent use of every affected global. The factory function `sub_215D530` allocates a 320-byte state object containing two DenseMap-like hash tables:

| Table | Offset | Purpose | Initial Capacity |
|---|---|---|---|
| GVMap | +168 | Old GlobalVariable -> New GlobalVariable | 128 buckets, 48 bytes/bucket |
| ConstMap | +248 | Old Constant -> New Constant (for constant expressions) | 128 buckets, 48 bytes/bucket |

The algorithm proceeds in three phases:

**Phase 1 -- Clone globals.** Iterate over all `GlobalVariable` objects in the module. For each global in `addrspace(0)` (the LLVM generic address space):

1. Create a new `GlobalVariable` in `addrspace(1)` (NVPTX global memory) with identical initializer, linkage, alignment, and section attributes.
2. Store the old-to-new mapping in `GVMap`.

**Phase 2 -- Rewrite uses.** For each cloned global:

1. Create an `addrspacecast` instruction from the new global (`addrspace(1)*`) back to the original pointer type (`addrspace(0)*`). This preserves type compatibility with all existing uses.
2. Call `RAUW` (replaceAllUsesWith) on the original global, substituting the `addrspacecast` value. All instructions, constant expressions, and metadata references that pointed to the original global now point through the cast.
3. The `ConstMap` table handles the tricky case of constant expressions that embed a global reference: `ConstantExpr::getAddrSpaceCast`, `ConstantExpr::getGetElementPtr`, and similar must be reconstructed with the new global. This is the bulk of the 36KB function body -- a recursive walk over the constant expression tree, rebuilding each node.

**Phase 3 -- Erase originals.** Iterate `GVMap` and erase each original global from the module. The cleanup helper `sub_215D780` iterates the map, properly managing LLVM `Value` reference counts during deletion.

The destructor at `sub_215D1A0` / `sub_215CE20` frees both hash tables and all stored `Value` references.

```cpp
// Pseudocode for GenericToNVVM::runOnModule
bool runOnModule(Module &M) {
    for (GlobalVariable &GV : M.globals()) {
        if (GV.getAddressSpace() != 0) continue;  // skip non-generic
        if (GV.isDeclaration()) continue;

        // Phase 1: Clone to addrspace(1)
        GlobalVariable *NewGV = new GlobalVariable(
            M, GV.getValueType(), GV.isConstant(),
            GV.getLinkage(), GV.getInitializer(),
            GV.getName(), /*InsertBefore=*/nullptr,
            GV.getThreadLocalMode(), /*AddressSpace=*/1);
        NewGV->copyAttributesFrom(&GV);
        GVMap[&GV] = NewGV;
    }

    for (auto &[OldGV, NewGV] : GVMap) {
        // Phase 2: addrspacecast + RAUW
        Constant *Cast = ConstantExpr::getAddrSpaceCast(NewGV,
            OldGV->getType());
        OldGV->replaceAllUsesWith(Cast);
    }

    for (auto &[OldGV, NewGV] : GVMap) {
        // Phase 3: Erase originals
        OldGV->eraseFromParent();
    }
    return !GVMap.empty();
}
```

**Why this exists.** The CUDA frontend (EDG) generates globals in `addrspace(0)` (LLVM's generic/default address space). The NVPTX backend requires device globals to reside in `addrspace(1)` (GPU global memory) for correct PTX emission. GenericToNVVM bridges this mismatch. Upstream LLVM has an equivalent `NVPTXGenericToNVVM` pass, but cicc's version carries the additional `ConstMap` machinery for handling nested constant expression trees that reference relocated globals -- a case that upstream handles differently through its `GenericToNVVM` + `NVPTXAssignValidGlobalAddresses` split.

## Global Constructor Rejection -- `sub_215ACD0`

```c
if (lookup("llvm.global_ctors") && type_tag == ArrayType && count != 0)
    fatal("Module has a nontrivial global ctor, which NVPTX does not support.");
if (lookup("llvm.global_dtors") && type_tag == ArrayType && count != 0)
    fatal("Module has a nontrivial global dtor, which NVPTX does not support.");
```

GPU kernels have no "program startup" phase -- no `__crt_init` equivalent. Static initialization with non-trivial constructors is incompatible with the GPU execution model.

## Global Variable Emission -- `sub_2156420`

### Overview

The function `sub_2156420` (20KB, `printModuleLevelGV`) handles PTX emission for individual global variables. It processes each global in the module, categorizing it by type (texture reference, surface reference, sampler reference, or data variable) and emitting the appropriate PTX declaration.

Skipped globals: `"llvm.metadata"`, `"llvm.*"`, `"nvvm.*"`.

| Global Type | PTX Output |
|---|---|
| Texture reference | `.global .texref NAME;` |
| Surface reference | `.global .surfref NAME;` |
| Sampler reference | `.global .samplerref NAME = { ... }` |
| Managed memory | `.attribute(.managed)` |
| Demoted (addrspace 3) | `// NAME has been demoted` (comment only) |

### Sampler Reference Initializer

Sampler references receive a structured initializer block with addressing mode, filter mode, and normalization settings. The emission format:

```ptx
.global .samplerref my_sampler = {
    addr_mode_0 = clamp_to_edge,
    addr_mode_1 = wrap,
    addr_mode_2 = mirror,
    filter_mode = linear,
    force_unnormalized_coords = 1
};
```

The addressing mode values are selected from four string literals:

| Value | String |
|---|---|
| 0 | `"wrap"` |
| 1 | `"clamp_to_border"` |
| 2 | `"clamp_to_edge"` |
| 3 | `"mirror"` |

Filter mode selects between `"nearest"` and `"linear"`. The `force_unnormalized_coords` field is emitted only when the sampler uses unnormalized texture coordinates (integer addressing).

### Address Space Qualifiers

`sub_214FA80` maps NVPTX address space numbers to PTX qualifier strings:

| Address Space | PTX Qualifier |
|---|---|
| 0 | (generic, no qualifier) |
| 1 | `.global` |
| 3 | `.shared` |
| 4 | `.const` |
| 5+ | `.local` |

Additional attributes emitted by `sub_214FEE0`:
- `.attribute(.managed)` for CUDA managed memory globals
- `.attribute(.unified)` or `.attribute(.unified(N))` for unified addressing

### Data Type Emission

For aggregate or large types, the emitter uses `.b8 NAME[SIZE]` (byte array). For pointer types with initializers, it selects `.u32` or `.u64` arrays depending on the pointer width flag at `*(a1+232)->field_936`. Simple scalar types use the type from `sub_214FBF0` (`.u32`, `.u64`, `.f32`, `.f64`, etc.).

### Invalid Address Space Detection

If a global has an initializer in an address space that does not support static initialization:

```
fatal("initial value of 'NAME' is not allowed in addrspace(N)");
```

This diagnostic is emitted via `sub_1C3F040`.

## Global Variable Ordering -- `sub_2157D50` (Topological Sort)

### Problem

Global variables with initializers can reference other globals. If global `A`'s initializer contains a reference to global `B`, then `B` must be emitted before `A` in the PTX output. Circular dependencies are illegal and must be detected.

### Algorithm -- DFS Topological Sort

`sub_2157D50` (5.9KB) implements a depth-first topological sort over the global use-def chains. The algorithm:

1. **Build dependency graph.** For each global variable in the emission set, walk its initializer constant expression tree. Every `GlobalVariable` reference found in the initializer creates a directed edge from the referencing global to the referenced global.

2. **DFS with three-color marking.** Each global is in one of three states:
   - **White** (unvisited): not yet processed.
   - **Gray** (in progress): currently on the DFS stack -- its subtree is being explored.
   - **Black** (finished): all dependents have been emitted.

3. **Visit procedure.** For each white global, mark it gray and recurse into its dependencies. When all dependencies return, mark it black and push it onto the output ordering (post-order).

4. **Cycle detection.** If the DFS encounters a gray node, a back-edge has been found, which means a circular dependency. The pass emits the fatal diagnostic:

```
"Circular dependency found in global variable set"
```

This is a hard error -- cicc cannot emit globals with mutual references. The PTX format requires a linear declaration order, and there is no forward-declaration mechanism for global variable initializers.

### Pseudocode

```cpp
// sub_2157D50 — topological sort of globals for PTX emission
void orderGlobals(SmallVectorImpl<GlobalVariable *> &Ordered,
                  ArrayRef<GlobalVariable *> Globals) {
    enum Color { White, Gray, Black };
    DenseMap<GlobalVariable *, Color> color;

    for (GlobalVariable *GV : Globals)
        color[GV] = White;

    std::function<void(GlobalVariable *)> visit =
        [&](GlobalVariable *GV) {
        if (color[GV] == Black) return;
        if (color[GV] == Gray)
            fatal("Circular dependency found in global variable set");
        color[GV] = Gray;

        // Walk initializer for GlobalVariable references
        if (Constant *Init = GV->getInitializer())
            for (GlobalVariable *Dep : globalsReferencedBy(Init))
                if (color.count(Dep))
                    visit(Dep);

        color[GV] = Black;
        Ordered.push_back(GV);
    };

    for (GlobalVariable *GV : Globals)
        if (color[GV] == White)
            visit(GV);
}
```

### Interaction with Sampler References

Sampler reference globals can have structured initializers that reference other sampler state. These initializers are walked by the same DFS traversal. The topological sort ensures that any sampler whose initializer references another sampler or texture object appears after its dependencies in the PTX output.

### Call Context

`sub_2157D50` is called from the module-level emission entry (`sub_215ACD0` -> `sub_214F370`) after all globals have been collected but before any global PTX text is written. The ordered list is then iterated by `sub_2156420` to emit each global in dependency order.

## Output Mode Selection

Compilation output mode is controlled by a bitmask in the `a13` mode flags parameter, passed through the pipeline from the CLI flag parser (`sub_95C880`). The low bits encode the output format, while bits 8--9 encode the address width (32/64-bit).

### Mode Flag Bitmask

| Bits | Value | Mode | Description |
|---|---|---|---|
| `[2:0]` | `0x07` | Phase control | Default = 7 (all phases: lnk + opt + llc) |
| `[4]` | `0x10` | Debug | Debug compile or line-info enabled |
| `[5]` | `0x20` | LTO gen | LTO generation enabled |
| combined | `0x21` | gen-lto | Generate LTO bitcode for later linking |
| combined | `0x23` | full LTO | Complete LTO compilation (lnk + opt + lto) |
| combined | `0x26` | link-lto | Link-time LTO phase (consume LTO bitcode) |
| combined | `0x43` | OptiX IR | Emit `.optixir` format |
| `[7]` | `0x80` | gen-opt-lto | Lowering flag for LTO |
| `[8]` | `0x100` | nvvm-64 | 64-bit pointer mode |
| `[9]` | `0x200` | nvvm-32 | 32-bit pointer mode |

### CLI Flag to Mode Mapping

| CLI Flag | Mode Bits Set | Pipeline Effect |
|---|---|---|
| (default) | `0x07` | All phases run, PTX text output |
| `--emit-llvm-bc` | (EDG flag id=59) | Emit raw LLVM bitcode `.bc` after optimization |
| `--emit-optix-ir` | `(a13 & 0x300) \| 0x43` | Disables IP-MSP and LICM, emits `.optixir` |
| `-gen-lto` | `(a13 & 0x300) \| 0x21` | Generates LTO-compatible bitcode |
| `-gen-lto-and-llc` | `a13 \| 0x20` | LTO generation plus LLC codegen |
| `-link-lto` | `(a13 & 0x300) \| 0x26` | Consumes LTO bitcode for final compilation |
| `-lto` | `(a13 & 0x300) \| 0x23` | Full LTO mode (all phases) |
| `-split-compile=N` | (stored at offset+1480) | Per-function compilation, `F%d_B%d` output naming |

### OptiX IR Mode

The `--emit-optix-ir` flag is valid only when the compilation mode is CUDA (`a4 == 0xABBA`) or OpenCL (`a4 == 0xDEED`). It forces two optimizer passes to be disabled by routing `"-do-ip-msp=0"` and `"-do-licm=0"` to the opt phase. The output is an `.optixir` file containing NVVM IR in a format consumable by the OptiX ray-tracing runtime for JIT compilation. See [OptiX IR](optix-ir.md) for the full format details.

### Split Compilation

The `-split-compile=N` flag (stored at options offset +1480, with a sentinel at +1488 to detect double-definition) enables per-function or per-block compilation for large kernels. The pipeline assembler at `sub_12E54A0` generates output identifiers using the `"F%d_B%d"` format string (function index, block index). Each split unit is compiled independently and the results are linked back together. An extended variant `-split-compile-extended=N` sets the additional flag at offset +1644.

When split-compile is active, the optimization level is set to negative (typically -1), triggering special handling in `sub_12E1EF0`: each compiled function's bitcode is re-read via `sub_153BF40`, validated against the `"<split-module>"` identifier, and linked back through `sub_12F5610` with linkage attributes restored from a hash table.

### LTO Modes

Three LTO modes interact with emission:

1. **gen-lto** (`0x21`): Runs optimization but skips LLC. Output is optimized LLVM bitcode suitable for later link-time optimization. The `-gen-lto` string is forwarded to the LTO phase.

2. **link-lto** (`0x26`): Consumes bitcode produced by gen-lto. Runs the LTO linker and optimizer, then proceeds to LLC for final codegen. The `-link-lto` string is forwarded.

3. **full LTO** (`0x23`): Single-invocation LTO that runs all phases including linking and codegen.

## Bitcode Producer ID

The bitcode writer at `sub_1538EC0` (58KB, `writeModule`) stamps `"LLVM7.0.1"` as the producer identification string in the `IDENTIFICATION_BLOCK` of every output bitcode file. This is despite cicc being built on LLVM 20.0.0 internally.

### Dual-Constructor Mechanism

Two separate global constructors manage producer version strings, both reading the same environment variable but with different defaults:

| Constructor | Address | Default | Stored At | Purpose |
|---|---|---|---|---|
| `ctor_036` | `0x48CC90` | `"20.0.0"` | `qword_4F837E0` | True LLVM version (internal use) |
| `ctor_154` | `0x4CE640` | `"7.0.1"` | (separate global) | NVVM IR compatibility marker |

Both constructors execute this logic:

```c
char *result = getenv("LLVM_OVERRIDE_PRODUCER");
if (!result) result = default_string;  // "20.0.0" or "7.0.1"
producer_global = result;
```

The bitcode writer uses the `ctor_154` value, producing `"LLVM"` + `"7.0.1"` = `"LLVM7.0.1"` in the output. Setting `LLVM_OVERRIDE_PRODUCER` in the environment overrides both constructors to the same value.

### Why "LLVM7.0.1"

The `"LLVM7.0.1"` string is the **NVVM IR compatibility marker**. It signals that the bitcode format conforms to the NVVM IR specification originally based on LLVM 7.0.1's bitcode structure. Even though cicc's internal passes operate at LLVM 20.0.0 capability, the output bitcode format (record encoding, metadata layout, type table) is constrained to be readable by older NVVM toolchain components (libNVVM, nvdisasm, Nsight) that expect LLVM 7.x-era bitcode. The writer achieves this by:

1. Using the `IDENTIFICATION_BLOCK` producer string to declare compatibility.
2. Constraining the `MODULE_BLOCK` record types to the LLVM 7.x repertoire.
3. Enforcing `nvvmir.version` metadata with `major == 3, minor <= 2`.

The `disable-bitcode-version-upgrade` cl::opt (registered in `ctor_036`) controls whether the bitcode reader accepts version mismatches during ingestion.

### Related Environment Variable

`NVVM_IR_VER_CHK=0` bypasses the NVVM IR version validation at `sub_157E370` and `sub_12BFF60`, which normally enforces `major == 3, minor <= 2` and fatals with `"Broken module found, compilation aborted!"` on mismatch.

## Address Space Operations -- `sub_21E7FE0`

Multi-purpose helper for cvta, MMA operands, and address space qualifiers:

| Query | Values | Output |
|---|---|---|
| `"addsp"` | 0=generic, 1=.global, 3=.shared, 4+=.local | cvta address space suffix |
| `"ab"` | 0="a", 1="b" | cvta direction |
| `"rowcol"` | 0="row", 1="col" | MMA layout |
| `"mmarowcol"` | 0--3 | "row.row"/"row.col"/"col.row"/"col.col" |
| `"satf"` | 0=(none), 1=".satfinite" | MMA saturation |
| `"abtype"` | 0--6 | "u8"/"s8"/"u4"/"s4"/"b1"/"bf16"/"tf32" |
| `"trans"` | 0=(none), 1=".trans" | WGMMA transpose |

## Architecture-Gated Features

| Feature | Min Architecture | Evidence |
|---|---|---|
| Basic atomics (all 13 ops) | SM 20+ (all) | `sub_21E5E70`, no arch check |
| Atomic scopes (.cta/.sys) | SM 60+ (Pascal) | Scope bits in operand |
| L2 cache-hinted atomics | SM 80+ (Ampere) | `sub_21E6420` separate function |
| membar.cta/gpu/sys | SM 20+ (all) | `sub_21E94F0`, no arch check |
| fence.sc.cluster | SM 90+ (Hopper) | Opcode 4 in membar handler |
| barrier.cluster.arrive/wait | SM 90+ (Hopper) | `sub_21E8EA0` entire function |
| Cluster special registers (15) | SM 90+ (Hopper) | `sub_21E9060` entire function |
| MMA row/col layout | SM 70+ (Volta) | mmarowcol in `sub_21E7FE0` |
| MMA abtype: bf16/tf32 | SM 80+ (Ampere) | Ampere-class MMA formats |
| .trans modifier (WGMMA) | SM 90+ (Hopper) | WGMMA transpose |

## Key Global Variables

| Variable | Purpose |
|---|---|
| `byte_4FD17C0` | Pass configuration flag |
| `byte_4FD16E0` | ISel dump enable |
| `byte_4FD2160` | Extra ISel pass enable |
| `dword_4FD26A0` | Scheduling mode (1=simple, else=full pipeline) |
| `unk_4FD155C` | GenericToNVVM pass ID |
| `dword_4FD1558` | GenericToNVVM once-init guard |
| `qword_4F837E0` | True LLVM producer version ("20.0.0") |

## Cross-References

- [OptiX IR](optix-ir.md) -- OptiX IR output format details
- [Bitcode I/O](../infra/bitcode-io.md) -- Bitcode reader/writer and `"LLVM7.0.1"` producer
- [Register Classes](../reference/register-classes.md) -- Consolidated register class reference
- [Address Spaces](../reference/address-spaces.md) -- Consolidated address space reference
- [AsmPrinter](../infra/asmprinter.md) -- AsmPrinter infrastructure
- [nvcc Interface](nvcc-interface.md) -- CLI flag routing from nvcc to cicc
