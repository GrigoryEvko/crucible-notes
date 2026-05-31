# PTX-to-Ori Lowering

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The PTX-to-Ori lowering is the transition from parsed PTX assembly into the Ori internal representation -- the SASS-level, virtual-register IR that all subsequent optimization operates on. Unlike a traditional compiler where the parser builds an AST and a separate lowering pass consumes it, ptxas has **no materialized AST**: the Bison parser's reduction actions directly construct Ori IR nodes, basic blocks, and CFG edges inline. What the `--compiler-stats` timer calls "DAGgen-time" measures this inline construction phase. The result is a raw Ori IR that still uses PTX-derived opcodes and has unresolved architecture-dependent constructs. Fourteen "bridge phases" (pipeline indices 0--13) then transform this raw IR into the optimizer-ready form where every instruction carries its final SASS opcode, the CFG is fully annotated, and architecture-incompatible operations have been legalized.

The key architectural consequence of this design: there is no separate "lowering" function that you can point at and say "this converts PTX to Ori." The conversion is distributed across (1) the Bison parser's 443 reduction actions, (2) a 44 KB operand processing function, (3) the MercConverter instruction legalization pass, and (4) six additional bridge phases that handle FP16 promotion, control flow canonicalization, macro fusion, and recipe application.

| | |
|---|---|
| **DAGgen timer** | `"DAGgen-time           : %.3f ms (%.2f%%)\n"` (inline Bison -> Ori construction) |
| **Bison parser** | `sub_4CE6B0` (48 KB, 512 productions, 443 reductions, no AST) |
| **Operand processing** | `sub_6273E0` (44 KB, 6-bit operand type switch) |
| **MercConverter** | `sub_9F1A90` (35 KB, opcode-dispatched visitor) |
| **MercConverter orchestrator** | `sub_9F3340` (7 KB) |
| **Opcode dispatch** | `sub_9ED2D0` (25 KB, master switch on `*(instr+72) & 0xCF`) |
| **Post-conversion lowering** | `sub_9EF5E0` (27 KB, string `"CONVERTING"`) |
| **Bridge phases** | Phases 0--13 (14 phases, first group in the 159-phase pipeline) |
| **Diagnostic dump** | Phase 9: `ReportInitialRepresentation` (sub_A3A7E0 stats emitter) |
| **Intrinsic descriptors** | `sub_9EE390` (20 KB, `"IntrinsicDescrFile=%s"`) |

## Architecture

```text
PTX source text
     |
     v
[Flex scanner]  sub_720F00 (15.8KB, 552 rules)
     |  token stream
     v
[Bison parser]  sub_4CE6B0 (48KB, 512 productions)
     |  NO AST -- reduction actions build IR directly:
     |    - allocate instruction nodes from pool
     |    - set opcode field (instruction +72)
     |    - build operand array (instruction +84)
     |    - link into doubly-linked list per basic block
     |    - create basic block entries (40B each)
     |    - populate CFG hash maps (Code Object +648, +680)
     |
     v                                             "DAGgen-time"
[Operand processing]  sub_6273E0 (44KB)            boundary
     |  6-bit type switch (v12 & 0x3F)             ----------
     |  address computation, state space annotation
     v
+----------------------------------------------------------+
|  RAW ORI IR (PTX-derived opcodes, virtual registers)     |
|  Instructions: PTX-level names (add.f32, ld.global, etc) |
|  Registers: virtual R-file, typed descriptors             |
|  CFG: basic blocks + edge hash maps (partially formed)    |
+----------------------------------------------------------+
     |
     |  Phase 0: OriCheckInitialProgram (validate)
     |  Phase 1: ApplyNvOptRecipes      (configure opt levels)
     |  Phase 2: PromoteFP16            (FP16 -> FP32 where needed)
     |  Phase 3: AnalyzeControlFlow     (finalize CFG + RPO + backedges)
     |  Phase 4: AdvancedPhaseBeforeConvUnSup (arch hook, no-op default)
     |  Phase 5: ConvertUnsupportedOps  (MercConverter: PTX ops -> SASS ops)
     |  Phase 6: SetControlFlowOpLastInBB (CFG structural fixup)
     |  Phase 7: AdvancedPhaseAfterConvUnSup (arch hook, no-op default)
     |  Phase 8: OriCreateMacroInsts    (fuse instruction sequences)
     |  Phase 9: ReportInitialRepresentation (diagnostic dump)
     |  Phase 10: EarlyOriSimpleLiveDead (dead code elimination)
     |  Phase 11: ReplaceUniformsWithImm (fold known constants)
     |  Phase 12: OriSanitize           (validate post-bridge IR)
     |  Phase 13: GeneralOptimizeEarly  (bundled copy-prop + const-fold)
     v                                             "OCG-time"
+----------------------------------------------------------+               begins
|  OPTIMIZER-READY ORI IR                                  |
|  Instructions: SASS opcodes (FADD, IMAD, LDG, STG, ...) |
|  Registers: virtual R/UR/P/UP files                       |
|  CFG: complete with RPO, backedge map, loop headers       |
+----------------------------------------------------------+
     |
     v
[Phase 14+: main optimization pipeline]
```

## Inline IR Construction (Bison -> Ori)

The Bison parser at `sub_4CE6B0` has 512 grammar productions with 443 reduction-action cases. Each reduction action constructs IR directly -- no intermediate AST is ever materialized. The instruction table builder (`sub_46E000`, 93 KB, 1,141 per-opcode registration calls to `sub_46BED0`) runs during parser initialization and registers the legal type combinations for every PTX instruction. The instruction lookup subsystem (`sub_46C690` entry, `sub_46C6E0` matcher at 6.4 KB) classifies operands into 12 categories at parse time.

When the parser encounters a PTX instruction like `add.f32 %r1, %r2, %r3`, it:

1. Looks up `add.f32` in the opcode table to get the internal opcode index and validate the type qualifier `.f32`
2. Allocates an Ori instruction node from the memory pool
3. Writes the opcode into the instruction field at offset `+72`
4. Processes each operand through `sub_6273E0` to build the packed operand array at offset `+84`
5. Links the instruction into the current basic block's doubly-linked list
6. If the instruction is a branch/jump/return, creates a CFG edge in the successor hash map at Code Object `+648`

Special PTX registers (`%ntid`, `%laneid`, `%smid`, `%ctaid`, `%clock64`, etc.) are mapped to internal identifiers during parser initialization at `sub_451730`. The mapping table is built from the ROT13-encoded opcode table populated by `ctor_003` at `0x4095D0`.

## Operand Processing -- `sub_6273E0`

The 44 KB operand processing function handles all PTX operand forms. It reads `v12 = *(_BYTE *)a2` (the first byte of the PTX AST node) and switches on `v12 & 0x3F`. The 11 active case values and the default are:

| `v12 & 0x3F` | PTX AST node kind | PTX syntax example | Ori opcode emitted | Processing summary |
|---|---|---|---|---|
| 0 | Binary expression | `%r1 + %r2` | 131 (`HSETP2`, used as binary-op) or 162 (`TTUCCTL`, used as ternary-select) | Recursively processes both child operands via `sub_62A760`, compares widths via `sub_44B390`, and widens the narrower operand with `sub_620780`. Sub-switch on child list-head tag: 12 emits Ori 131, 13 emits Ori 162 |
| 2 | Register | `%r1`, `%f1`, `%p0` | 38 (type-resolved move) | Calls `sub_61C9B0` for type resolution via `dword_20249C0` lookup. Sub-switch on resolved type (9/10: int/fp immediate store; 11/13/15/20: flag constants; 26: const-bank redirect creating Ori 11 then `sub_620C50`; 36: 64-bit literal via `sub_616670`) |
| 3 | Register pair | `{%r1, %r2}` | 38 (same as case 2) | Shares handler with case 2. When type==3, the secondary sub-switch dispatches on sub_44E010/sub_44E040/sub_44E060/sub_44E070 extractors for int-32, int-64, float, and double values respectively |
| 4 | Variable reference | `var`, `arr[i]` | Depends on storage class | Calls `sub_457420` to resolve variable descriptor; `sub_6200A0` returns storage class. Inner switch on class: 2=register load (Ori 43/45), 3=state-space address (special-reg dispatch via `sub_627380` for `.nv.reservedSmem.*`), 4=array element (Ori 44/46), 5=constant (Ori 41), 6=surface/texture (Ori 42), 8=param (Ori 39), 9=extern (Ori 48), 10=global (Ori 24, link-time), 11=local (Ori 25, link-time), 12=shared (Ori 52) |
| 5 | Vector constructor | `{%r1, %r2}` (vec2) | 95 (`STS`, vec-pack) | If `a12` flag set: processes single child via `sub_62A760`. Otherwise: recursively processes both children, type-resolves via `sub_61C9B0`, emits Ori 95 two-operand vector pack instruction via `sub_A2F940` |
| 6 | Negation / unary op | `-%r1`, `~%r1` | 71 (`CALL`, unary-op) | Processes inner operand via `sub_62A760`, type-resolves, emits single-operand Ori 71 instruction via `sub_A2F7A0` with the operand type in the high word |
| 9 | Type cast / conversion | `.f32 %r1` (explicit cast) | Delegates recursively | Recursive call to `sub_6273E0` on child node (`a2+16`). If byte `a2+1` has bit 0 set, post-processes with `sub_A2FEB0` + `sub_6157C0` to apply rounding/saturation modifiers |
| 0xB (11) | Address expression | `[%rd1+16]`, `[arr+i]` | 131 (base+offset) or 144 (indexed array) | Sub-switch on inner node type: if inner==4 (variable), calls `sub_624DF0` for direct address; if inner==5 (vector), builds indexed address with Ori 144 via `sub_A2F940`, creates constant index operand from `dword_20246A0` lookup, and optionally emits a widening Ori 131 wrapper |
| 0xC (12) | Parenthesized expr | `(%r1)` | Passthrough | Unwraps one level: recursively calls `sub_62A760` on the inner child. If `a5` (destination context) flag is set and `a12` is zero, chains through `sub_624680` for destination address qualification |
| 0xE (14) | Operand list | `%r1, %r2, %r3` | Chained via `sub_620AF0` | If `a4` tag is 66 (predicated): checks type-qualifier size (4 or 8 bytes) via `sub_4574A0`/`sub_4574B0` family to set predicate flags, then delegates to `sub_629E40`. Otherwise: iterates the linked list (`*v56` walk), recursively calling `sub_6273E0` on each element and chaining results with `sub_620AF0(ctx, new, prev, index)` |
| 0x10 (16) | Predicate register | `%p0`, `@%p1` | Predicate-typed value | Calls `sub_61C9B0` for type resolution, then `sub_620320(ctx, typeCode, 1, &v283)` to create a predicate-width operand node |
| default | (unknown tag) | -- | Returns 0 | Unrecognized node tag; returns null |

String references in callees of `sub_6273E0` (not in the function itself, but in its direct callees `sub_627380`, `sub_623AB0`, `sub_4CE6B0`):
- `".nv.reservedSmem.{begin,end,cap,offset0,offset1}"` -- reserved shared memory region symbols resolved by `sub_627380` (called from case 4, storage class 3, state-space codes 'e'--'i')
- `"COARSEOFFSET"` -- coarse-grained offset label created by `sub_623AB0` for large address space indexing
- `"__$endLabel$__%s"` -- end-of-scope label synthesized by the Bison parser (`sub_4CE6B0`) for structured control flow

The function bridges PTX's explicitly-typed operand model (where `.u32`, `.f32`, `.b64` qualifiers are part of the syntax) to Ori's implicitly-typed model where the operand type is determined by the SASS opcode. Type resolution at `sub_61C9B0` maps PTX type qualifiers to a 5-entry size class via `dword_20249C0[min(typeClass, 4)]`, producing a size-category byte (255 for out-of-range) that is packed into the high word of the Ori operand descriptor.

## Bridge Phases (0--13)

### Phase 0: OriCheckInitialProgram -- Validation

Validates the raw Ori IR produced by the Bison parser for structural correctness: all basic blocks have valid entry/exit points, instruction operand counts match opcode requirements, register references are within bounds, and CFG edges are consistent. This is a pure validation pass that produces no IR transformations. It catches malformed IR early, before any optimization pass can amplify a structural error into a hard-to-diagnose miscompile.

### Phase 1: ApplyNvOptRecipes -- Optimization Level Configuration

Applies NvOptRecipe transformations controlled by option 391. When enabled, the PhaseManager's constructor (`sub_C62720`) allocates a 440-byte NvOptRecipe sub-manager at `PhaseManager+56`. This sub-manager configures per-phase behavior based on the NvOpt level (0--5), controlling which later phases are active and their aggressiveness:

| NvOpt level | Behavior |
|---|---|
| 0 | Minimal optimization (fast-compile path, many phases `isNoOp()`) |
| 1--2 | Standard optimization |
| 3--4 | Aggressive optimization (loop unrolling, speculative hoisting enabled) |
| 5 | Maximum optimization (may significantly increase compile time) |

The string `"Invalid nvopt level : %d."` in `sub_C173E0` confirms the valid range. The recipe data lives at `NvOptRecipe+312` with per-phase records at stride 584 bytes. The sub-manager maintains its own sorted array (`+376`) and hash table (`+400`..`+416`) for fast recipe lookup by phase index.

```text
NvOptRecipe Sub-Manager (440 bytes, at PhaseManager+56)
  +0      compilation_unit
  +8      phase_manager back-reference
  +16     ref_counted_list_1
  +312    recipe_data
  +336    allocator
  +344    timing_records (stride = 584 per entry)
  +376    sorted_array (for binary search by phase index)
  +400    hash_bucket_count
  +408    hash_buckets
  +432    shared_list_ptr (ref-counted)
```

### Phase 2: PromoteFP16 -- Half-Precision Type Promotion

Promotes half-precision (FP16) operations where hardware support is insufficient or promotion yields better throughput. The promotion strategy is architecture-dependent:

- **Pre-sm_53**: no native FP16 ALUs. All FP16 arithmetic is expanded to FP32 with narrowing conversions at stores.
- **sm_53+**: native FP16 support. Only operations that require expensive multi-instruction sequences in FP16 (certain transcendentals, complex comparisons) are promoted.
- **sm_89+ (Ada, Blackwell)**: wide FP16 tensor paths. Promotion is minimal; most FP16 stays native.

The phase walks the instruction linked list, inspects each instruction's type encoding at offset `+72`, and rewrites FP16 operations to FP32 equivalents by replacing the opcode and inserting conversion instructions (`F2F` in SASS terminology) at use/def boundaries.

### Phase 3: AnalyzeControlFlow -- CFG Finalization

Builds and finalizes the control flow graph data structures that the optimizer requires:

- **Successor edges**: populates the FNV-1a hash table at Code Object `+648`
- **Backedge map**: computes backedges and stores them at Code Object `+680`
- **RPO array**: builds the reverse post-order traversal at Code Object `+720`
- **Loop identification**: marks loop headers and backedge targets for later loop optimization passes (phases 18, 22, 24, 59)

The Bison parser constructs basic blocks and edges incrementally as it processes PTX instructions, but the CFG is not guaranteed to be fully consistent until this phase runs. For example, forward branch targets may reference blocks that were not yet created at parse time. This phase resolves all pending edges and ensures the CFG is complete.

### Phases 4 and 7: Architecture Hook Points

Phases 4 (`AdvancedPhaseBeforeConvUnSup`) and 7 (`AdvancedPhaseAfterConvUnSup`) are no-op-by-default hook points that bracket `ConvertUnsupportedOps`. Architecture backends override their vtables to inject target-specific processing:

- **Phase 4** (before): prepare target-specific state, mark instructions that need special handling on this architecture
- **Phase 7** (after): clean up after legalization, fix architecture-specific edge cases introduced by the generic lowering

These hooks are part of the 16 `AdvancedPhase` injection points distributed throughout the 159-phase pipeline. The architecture vtable factory at `sub_1CCEEE0` (17 KB, 244 callees) selects which overrides are active based on the `sm_version`.

### Phase 5: ConvertUnsupportedOps -- Instruction Legalization

The most substantial bridge phase. Lowers PTX operations that have no direct SASS equivalent for the target architecture. This phase runs the MercConverter engine (see next section) and handles:

- **64-bit integer arithmetic** on architectures with 32-bit ALUs: splits `add.s64`, `mul.lo.s64` into hi/lo 32-bit instruction pairs using carry chains
- **Complex addressing modes**: decomposes multi-component addresses into separate arithmetic instructions
- **PTX-specific operations**: converts PTX instructions that have no 1:1 SASS mapping (e.g., `bfe`, `bfi`, `prmt` variants not supported on all targets)
- **Architecture availability**: gates instructions by SM version (an instruction added in sm_80 is lowered to a multi-instruction sequence on sm_70)
- **Texture/surface operations**: legalizes texture sampling and surface access patterns (`sub_9E8B20`, 17 KB)
- **Memory operations**: legalizes load/store patterns, address register handling (`sub_9D76D0`/`sub_9D80E0`, 17--18 KB each)

After ConvertUnsupportedOps completes, every instruction in the IR has a valid SASS opcode for the target architecture.

The late phase 132 (`UpdateAfterConvertUnsupportedOps`) runs cleanup for edge cases introduced by this phase that are only detectable after optimization.

### Phase 6: SetControlFlowOpLastInBB -- CFG Structural Fixup

Enforces a critical structural invariant: **control flow operations must be the last instruction in their basic block.** If a branch, jump, return, or exit instruction is followed by other instructions in the same block (which can happen during lowering when a PTX instruction expands to a sequence ending in a branch), this phase splits the block at the control flow point.

The invariant is required by the scheduler (which assumes only the last instruction in a block can transfer control) and the register allocator (which computes live-out sets at block boundaries). The phase rewrites the instruction linked list and allocates new 40-byte basic block entries as needed.

### Phase 8: OriCreateMacroInsts -- Macro Fusion

Identifies and fuses instruction sequences into macro instructions for hardware efficiency. The phase scans the instruction linked list for patterns that the GPU hardware can execute as a single macro-op:

- **Compare + branch**: fused into a conditional branch macro instruction
- **Multiply + add**: fused into FMA where not already (different from PTX `fma` -- this catches `mul` followed by `add` on the same operands)
- **Address computation + memory access**: fused sequences for coalesced access patterns

The fused macro instructions carry composite semantics in a single IR node. They are expanded back into individual SASS instructions much later at phase 118 (`MercExpandInstructions`), after scheduling has determined the optimal placement. This late expansion allows the optimizer to treat the fused sequence as atomic, preventing passes from inserting unrelated instructions between the components.

### Phase 9: ReportInitialRepresentation -- Diagnostic Dump

Dumps the Ori IR state for debugging, active when `DUMPIR` or `--ftrace` diagnostics are enabled. The stats emitter at `sub_A3A7E0` prints a per-function profile:

```text
# 142 instructions, 24 R-regs
# [inst=142] [texInst=0] [tepid=0] [rregs=24]
# [est latency = 87] [LSpillB=0]
# [Occupancy = 0.750000]
# [issue thru=0.888889] [fp thru=0.000000]
# [worstcaseLat=87.000000]
# [avgcaseLat=52.500000]
# [SharedMem Alloc thru=0.000000]
# [instHint=0] [instPairs=0]
```

This snapshot provides the pre-optimization baseline. Comparing it against `ReportBeforeScheduling` (phase 96) and `ReportFinalMemoryUsage` (phase 126) shows the optimizer's impact on instruction count, register pressure, and estimated latency.

### Phases 10--13: Early Cleanup

| Phase | Name | Purpose |
|---|---|---|
| 10 | `EarlyOriSimpleLiveDead` | First dead code elimination pass. Removes instructions whose results are unused. Uses the SSE2-accelerated bitvector library (`sub_BDBA60`..`sub_BDDD40`) for liveness computation. |
| 11 | `ReplaceUniformsWithImm` | Folds known-constant uniform register loads into immediate operands. Important for kernel launch parameters passed through constant memory. |
| 12 | `OriSanitize` | Second structural validation after all bridge transformations. Catches errors introduced by phases 1--11 before the main optimizer begins. |
| 13 | `GeneralOptimizeEarly` | First compound optimization pass: copy propagation + constant folding + algebraic simplification in a single fixed-point iteration. Cleans up redundancies introduced by the bridge phases. |

## The MercConverter Engine

The MercConverter (`sub_9F1A90`, 35 KB) is the instruction conversion engine at the heart of `ConvertUnsupportedOps`. Despite its name referencing "Mercury" (NVIDIA's SASS encoding format), it operates purely at the IR level -- converting instruction semantics, not binary encodings.

### Call Chain

```text
sub_9F3340 (orchestrator, 7KB)
  |
  +-- sub_9F1A90 (MercConverter main pass, 35KB)
  |     |
  |     +-- sub_9ED2D0 (opcode dispatch, 25KB)
  |     |     |
  |     |     |  Large switch on (*(instr+72)) with byte-1 mask:
  |     |     |    BYTE1(opcode) &= 0xCF  -- strips modifier bits 4-5
  |     |     |
  |     |     +-- 130 explicit cases covering 181 opcode IDs
  |     |     +-- see "Opcode Dispatch Table" below for full map
  |     |     +-- default: emit_opcode(0xFFFF) -- unsupported marker
  |     |
  |     +-- sub_934630 (instruction creation utility, called N times)
  |
  +-- sub_9EF5E0 (post-conversion lowering, 27KB)
        |  string "CONVERTING"
        +-- sub_9EC160, sub_7C11F0, sub_7BFC30 (intrinsic expansion)
```

### Opcode Dispatch Table (sub_9ED2D0)

The dispatch reads `*(instr+72)`, masks byte 1 with `&= 0xCF` (strips modifier bits 4-5), then switches. `v[N]` = vtable call at byte offset `N*8` from `*(a1)`.

| Case value(s) | SASS mnemonic(s) | Handler | Type |
|---|---|---|---|
| 1 | `IMAD` | `sub_9DA5C0` | direct |
| 2,3,4,5,7 | `IMAD_WIDE`,`IADD3`,`BMSK`,`SGXT`,`ISETP` | `v[0]` | vtable |
| 6 | `LOP3` | `sub_9DA100` | direct |
| 8 | `IABS` | `sub_9D2440` | direct |
| 10,11,149,151,152,290,291 | `SHF`,`FFMA`,`UFLO`,`UIMAD`,`UMOV`,`MOV.104`,`UMOV.104` | `sub_9D80E0` | direct |
| 14,39,40,105,125,299,300,321 | `FMNMX`,`FLO`,`FCHK`,`ATOMS`,`DSETP`,`UFSETP`,`UI2I`,`LAST` | `v[7]` | vtable |
| 15,85 | `FSWZADD`,`TLD4` | `sub_9EC340` | direct |
| 16 | `FSET` | `sub_9E8B20` | direct |
| 17 | `FSEL` | `sub_9E7FB0` | direct |
| 18 | `FSETP` | `v[3]` | vtable |
| 22 | `R2P` | `sub_9D6DB0` | direct |
| 23 | `PLOP3` | `sub_9E58F0` | direct |
| 24 | `PRMT` | `sub_9D9F60` | direct |
| 26 | `VOTE` | `sub_9E54C0` | direct |
| 27 | `CS2R_32` | `sub_9E4BB0` | direct |
| 28 | `CS2R_64` | `sub_9D9E70` | direct |
| 31 | `VABSDIFF` | `v[4]` | vtable |
| 32,271 | `VABSDIFF4`,`TCLDSWS` | `sub_9E2440` | direct |
| 34 | `IDE` | `sub_9E55E0` | direct |
| 35 | `I2I` | `v[6]` | vtable |
| 36 | `I2IP` | `v[21]` | vtable |
| 38,59,106,180,182,192,194,215,221,242 | `POPC`,`R2B`,`QSPC`,`DMMA`,`HFMA2_MMA`,`REDUX`,`SM86_FIRST`,`DMMA.90`,`GMMA`,`UTMALDG` | `sub_9DA6B0` | direct |
| 41,284 | `IPA`,`IMNMX.104` | `sub_9D1DA0` | direct |
| 42,53,55,66 | `MUFU`,`BREV`,`BMOV_R`,`DEPBAR` | `sub_9D54B0` | direct |
| 43 | `F2F` | `v[9]` | vtable |
| 47 | `I2F` (cond) | `sub_9E74E0` | conditional |
| 50 | `FRND_X` | `v[12]` | vtable |
| 51 | `AL2P` | `sub_9E2F60` | direct |
| 52,54,72,97 | `AL2P_IDX`,`BMOV_B`,`RET`,`STG` | `sub_9D09C0` | direct (v8=1) |
| 57,101 | `S2R`,`ST` | `sub_9D6170` | direct |
| 58 | `B2R` | emit(162) | inline |
| 60,62,78,79 | `LEPC`,`BAR_IDX`,`RTT`,`BSYNC` | `sub_9E5EE0` | direct |
| 61,63,80 | `BAR`,`SETCTAID`,`MATCH` | `sub_9E6600` | direct |
| 65 | `GETLMEMBASE` | `v[22]` | vtable |
| 67 | `BRA` | `sub_9D9C30` | direct |
| 70 | `JMX` | `sub_9E3490` | direct |
| 73 | `BSSY` | `v[15]` | vtable |
| 74 | `BREAK` | `v[16]` | vtable |
| 75 | `BPT` | `sub_9E0C10` | direct |
| 77 | `EXIT` | `sub_9E4DF0` | direct |
| 81 | `NANOSLEEP` | `v[24]` | vtable |
| 83 | `TEX` | `sub_9D6AB0` | direct |
| 88,89 | `TXQ`,`LDC` | `sub_9D5990` | direct |
| 90 | `ALD` | `sub_9D2820` | direct |
| 91 | `AST` | `sub_9E7600` | direct |
| 92 | `OUT` | `sub_9E7890` | direct |
| 93,95 | `OUT_FINAL`,`STS` | `sub_9E1D40` | direct |
| 94 | `LDS` | `sub_9E1DF0` | direct |
| 96 | `LDG` | `sub_9D41C0` | direct |
| 98 | `LDL` | `sub_9D3230` | direct |
| 100 | `LD` | `sub_9D70E0` | direct |
| 102 | `ATOM` | `sub_9D9750` | direct |
| 103,104 | `ATOMG`,`RED` | `sub_9E31D0` | direct |
| 108 | `CCTL` | `sub_9D76D0` | direct |
| 110,111,112,114 | `CCTLT`,`MEMBAR`,`SULD`,`SUATOM` | `v[25]` | vtable |
| 118 | `ISBEWR` | `v[10]` | vtable |
| 119 | `SHFL` | `v[28]` | vtable |
| 120,121,126--128,280,281 | `WARPSYNC`,`YIELD`,`HADD2`,`HADD2_F32`,`HFMA2`,`SM100_LAST`,`SM104_FIRST` | `v[27]` | vtable |
| 122,123,310--312 | `DFMA`,`DADD`,`UF2FP.104`,`MXQMMA_SF`,`OMMA` | `v[26]` | vtable |
| 124 | `DMUL` | `sub_9E18B0` | direct |
| 130,169 | `HSET2`,`S2UR` | `v[29]` | vtable |
| 135 | `INTRINSIC` | `sub_9D6560` | direct |
| 139--141,143 | `UBMSK`,`UCLEA`,`UISETP`,`ULEA` | `sub_9D4C10` | direct |
| 145 | `ULOP3` | `sub_9D3020` | direct |
| 148 | `USGXT` | emit(45) | inline |
| 155,268 | `UPOPC`,`UTCSHIFT_2CTA` | `sub_9E5260` | direct |
| 156 | `USHF` | `sub_9D94B0` | direct |
| 157 | `SCATTER` | `v[84]` | vtable |
| 158,167 | `F2FP`,`LDTRAM` | `sub_9E4A00` | direct |
| 161 | `BMMA` | `sub_9D21D0` | direct |
| 162 | `TTUCCTL` | `sub_9D9660` | direct |
| 166 | `LDSM` | `sub_9E2100` | direct |
| 170 | `BRXU` | `sub_9E2DF0` | direct |
| 173,267 | `GATHER`,`UTCSHIFT_1CTA` | `sub_9EB5C0` | direct |
| 174 | `GENMETADATA` | `sub_9D9300` | direct |
| 176,177 | `BMMA_88128`,`BMMA_168128` | `v[34]` | vtable |
| 183,288 | `HMNMX2`,`ISETP.104` | `v[36]` | vtable |
| 184 | `IMMA_88` | `sub_9D2E70` | direct |
| 185 | `IMMA_SP_88` | `sub_9E32F0` | direct |
| 186 | `IMMA_16816` | `v[35]` | vtable |
| 188,190 | `IMMA_SP_16832`,`LDGDEPBAR` | `sub_9E2970` | direct |
| 195 | `F2IP` | `sub_9D2AB0` | direct |
| 196 | `UF2FP` | `sub_9D9080` | direct |
| 198 | `SUQUERY` | `sub_9D66F0` | direct |
| 201,202,204,285 | `QMMA_16816`,`QMMA_16832`,`QMMA_SP_12864`,`IMNMX.104v2` | `sub_9EAC30` | direct |
| 203 | `QMMA_SP_16832` | `sub_9D8E90` | direct |
| 205 | `SM89_LAST` | `sub_9E1260` | direct |
| 209 | `CGABAR_GET` | `sub_9E5740` | direct |
| 210,213,214 | `CGABAR_SET`,`CREATEPOLICY`,`CVTA` | `sub_9D8B30` | direct |
| 211 | `CGABAR_WAIT` | `v[39]` | vtable |
| 220 | `FMNMX.90` | `v[40]` | vtable |
| 223,238 | `LEPC.90`,`ULEPC` | `v[41]` | vtable |
| 228 | `SETMAXREG` | `v[42]` | vtable |
| 240 | `UTMACCTL` | `sub_9D6280` | direct |
| 241 | `UTMACMDFLUSH` | `sub_9E2CC0` | direct |
| 243 | `UTMAPF` | `v[43]` | vtable |
| 245 | `UTMALST` | `v[67]` | vtable |
| 246 | `VHMNMX` | `v[68]` | vtable |
| 247 | `VIADD` | `sub_9D0F70` | direct |
| 248 | `VIADDMNMX` | `sub_9D0DF0` | direct |
| 249 | `VIMNMX` | `v[72]` | vtable |
| 250 | `VIMNMX3` | `v[73]` | vtable |
| 251 | `WARPGROUP` | `v[74]` | vtable |
| 252 | `SM90_LAST` | `v[75]` | vtable |
| 253 | `SM100_FIRST` | `v[76]` | vtable |
| 257 | `FMNMX3` | `v[69]` | vtable |
| 262 | `UTCBAR_2CTA` | `sub_9E7440` | direct |
| 264 | `UTCCP_2CTA` | `sub_9D73F0` | direct |
| 265,266 | `UTCMMA_1CTA`,`UTCMMA_2CTA` | `v[93]` | vtable |
| 269 | `VIRTCOUNT` | emit(0xFFFF) | inline |
| 270 | `TCATOMSWS` | `v[77]` | vtable |
| 276 | `MEMSET` | `sub_9D5EC0` | direct |
| 277 | `ACQSHMINIT` (type==5 ? `v[65]` : `v[11]`) | conditional | vtable |
| 279 | `FENCE_T` | `v[81]` | vtable |
| 282 | `IADD` | `v[82]` | vtable |
| 283 | `UVIADD` | `v[83]` | vtable |
| 286,287 | `UIMNMX`,`UVIMNMX` | `v[87]` | vtable |
| 292 | `SEL.104` | `sub_9D0E90` | direct |
| 297 | `UFMUL` | `v[19]` | vtable |
| 298 | `UFSET` | `v[2]` | vtable |
| 301,319 | `UI2IP`,`QMMA_SF_SP` | `v[32]` | vtable |
| 302 | `UF2F` | `v[5]` | vtable |
| 303 | `UFRND` | `v[17]` | vtable |
| 304--306 | `UF2I`,`UF2IP`,`UI2F` | `v[8]` | vtable |
| 307 | `UI2FP` | `v[13]` | vtable |
| 308 | `UIABS` | `v[14]` | vtable |
| 309 | `CS2UR` | `v[18]` | vtable |
| 313 | `OMMA_SP` | `v[31]` | vtable |
| 314,324 | `QMMA_16816.104`,... | `v[1]` | vtable |
| 315 | `QMMA_16832.104` | `v[86]` | vtable |
| 316 | `QMMA_SP_16832.104` | `v[38]` | vtable |
| 317,318 | `QMMA_SP_12864.104`,`QMMA_SF` | `v[44]` | vtable |
| 322 | (sm_104) | `v[66]` | vtable |
| 323 | (sm_104) | `v[85]` | vtable |
| 325,326 | (sm_104) | `v[88]` | vtable |
| 327,328 | (sm_104) | `v[89]` | vtable |
| 329--331 | (sm_104) | `v[90]`,`v[30]`,`v[33]` | vtable |
| 332--348 | (sm_104, 17 opcodes) | `v[46..62]` | vtable 1:1 |
| 349--351 | (sm_104) | `v[78..80]` | vtable 1:1 |
| 352 | (sm_104) | `v[20]` | vtable |
| default | *all unmatched* | emit(0xFFFF) | unsupported |

**Legend.** `v[N]` = `*(*(a1)+N*8)(a1,a2)`. "emit(X)" = inline replacement opcode with no handler. Cases 15, 52, 54, 72, 97 set `v8=1`, skipping the default post-dispatch predication call to `sub_9CD420`. Case 47 (`I2F`) checks `*(*(instr+40)+174) & 1`; only calls `sub_9E74E0` if set, else falls to default. Case 277 (`ACQSHMINIT`) inspects operand type: `(operand & 7) == 5` selects `v[65]`, otherwise `v[11]`. Cases 58/148/269 emit replacement opcodes inline (162/45/0xFFFF).

### Per-Category Handlers

| Handler | Size | Category | Opcodes routed here |
|---|---|---|---|
| `sub_9D80E0` | 17 KB | Data movement legalization | 10,11,149,151,152,290,291 (SHF, FFMA, UFLO, UIMAD, UMOV, MOV/UMOV.104) |
| `sub_9DA6B0` | small | Misc ALU + MMA passthrough | 38,59,106,180,182,192,194,215,221,242 (POPC through UTMALDG) |
| `sub_9D54B0` | small | Bit/barrier manipulation | 42,53,55,66 (MUFU, BREV, BMOV_R, DEPBAR) |
| `sub_9D4C10` | small | Uniform integer ops | 139--141,143 (UBMSK, UCLEA, UISETP, ULEA) |
| `sub_9E6600` | 25 KB | Instruction expansion | 61,63,80 (BAR, SETCTAID, MATCH -- multi-instruction) |
| `sub_9E5EE0` | med | Control flow lowering | 60,62,78,79 (LEPC, BAR_INDEXED, RTT, BSYNC) |
| `sub_9D09C0` | small | Predicated lowering (v8=1) | 52,54,72,97 (AL2P_INDEXED, BMOV_B, RET, STG) |
| `sub_9EAC30` | med | Quarter-precision MMA | 201,202,204,285 (QMMA family) |
| `sub_9EC340` | 23 KB | Multi-operand legalization | 15,85 (FSWZADD, TLD4) |
| `sub_9E8B20` | 17 KB | Texture/surface lowering | 16 (FSET) |
| `sub_9D76D0` | 18 KB | Cache control legalization | 108 (CCTL) |

### Intrinsic Descriptor Loading

`sub_9EE390` (20 KB) loads architecture-specific instruction descriptions from a file (`"IntrinsicDescrFile=%s"`). This allows the MercConverter to query which intrinsic operations are natively supported on the target SM and which require multi-instruction expansion. The descriptor file is architecture-versioned and loaded once during the first compilation of a kernel targeting that architecture.

## The PTX-to-SASS Opcode Transition

The fundamental semantic transformation during lowering: PTX uses high-level, explicitly-typed opcodes; Ori uses SASS-level opcodes where the type is encoded in the mnemonic. All SASS opcode strings in the binary are ROT13-encoded.

### Lowering Rules Reference (95 common PTX instructions)

The table below lists the primary SASS opcode(s) each PTX instruction lowers to during the bridge phases. "1:1" means a single SASS instruction; "1:N" means a multi-instruction expansion whose size depends on type width, rounding mode, or target SM. The lowering phase column indicates where the conversion happens: **P** = parser inline (Bison reduction action writes the SASS opcode directly), **5** = Phase 5 ConvertUnsupportedOps / MercConverter, **45/78** = later legalization passes. Entries marked with an SM gate are only lowered on architectures that lack native hardware support.

| # | PTX instruction | SASS opcode(s) | Ratio | Phase | Notes |
|---|---|---|---|---|---|
| 1 | `add.s32` / `add.u32` | `IADD3 Rd, Ra, Rb, RZ` | 1:1 | P | Third source defaults to RZ (hardware zero register) |
| 2 | `add.s64` / `add.u64` | `IADD3 Rd, Ra, Rb, RZ` (sm_70+) | 1:1 | P | Native 64-bit ALU on Volta+; on older SM, splits to hi/lo pair |
| 3 | `add.f32` | `FADD Rd, Ra, Rb` | 1:1 | P | Rounding mode encoded in FADD modifier bits |
| 4 | `add.f64` | `DADD Rd, Ra, Rb` | 1:1 | P | Double-precision add |
| 5 | `sub.s32` / `sub.u32` | `IADD3 Rd, -Ra, Rb, RZ` | 1:1 | P | Subtraction via negated first source in IADD3 |
| 6 | `sub.f32` | `FADD Rd, Ra, -Rb` | 1:1 | P | Subtraction via negated second source |
| 7 | `sub.f64` | `DADD Rd, Ra, -Rb` | 1:1 | P | Double-precision subtraction |
| 8 | `mul.lo.s32` / `mul.lo.u32` | `IMAD Rd, Ra, Rb, RZ` | 1:1 | P | Multiply-add with zero addend |
| 9 | `mul.hi.s32` / `mul.hi.u32` | `IMAD.HI Rd, Ra, Rb, RZ` | 1:1 | P | High-half multiply |
| 10 | `mul.wide.s32` | `IMAD_WIDE Rd, Ra, Rb, RZ` | 1:1 | P | 32x32->64 widening multiply |
| 11 | `mul.f32` | `FMUL Rd, Ra, Rb` | 1:1 | P | FP32 multiply |
| 12 | `mul.f64` | `DMUL Rd, Ra, Rb` | 1:1 | P | FP64 multiply |
| 13 | `mad.lo.s32` / `mad.lo.u32` | `IMAD Rd, Ra, Rb, Rc` | 1:1 | P | Integer multiply-add |
| 14 | `mad.wide.s32` | `IMAD_WIDE Rd, Ra, Rb, Rc` | 1:1 | P | Widening multiply-add |
| 15 | `fma.rn.f32` | `FFMA Rd, Ra, Rb, Rc` | 1:1 | P | Fused multiply-add FP32 |
| 16 | `fma.rn.f64` | `DFMA Rd, Ra, Rb, Rc` | 1:1 | P | Fused multiply-add FP64 |
| 17 | `neg.s32` | `IADD3 Rd, -Ra, RZ, RZ` | 1:1 | 5 | Integer negate via IADD3 with negated source |
| 18 | `abs.s32` | `IABS Rd, Ra` | 1:1 | P | Integer absolute value |
| 19 | `min.s32` / `min.u32` | `IMNMX Rd, Ra, Rb` | 1:1 | P | Integer min; sign mode in modifier |
| 20 | `max.s32` / `max.u32` | `IMNMX Rd, Ra, Rb` | 1:1 | P | Same opcode as min, direction in modifier bit |
| 21 | `min.f32` | `FMNMX Rd, Ra, Rb, PT` | 1:1 | P | Predicate operand selects min vs max |
| 22 | `max.f32` | `FMNMX Rd, Ra, Rb, !PT` | 1:1 | P | Negated true-predicate selects max |
| 23 | `abs.f32` | modifier on source operand | 1:1 | P | Absolute-value modifier bit, no separate instruction |
| 24 | `neg.f32` | modifier on source operand | 1:1 | P | Negate modifier bit, no separate instruction |
| 25 | `div.approx.f32` | `MUFU.RCP` + `FMUL` | 1:2 | 5 | Approximate: reciprocal then multiply |
| 26 | `div.full.f32` | `MUFU.RCP` + `FFMA` x2 + `FMUL` | 1:4+ | 45/78 | Full-range with one Newton-Raphson refinement |
| 27 | `div.rn.f32` | N-R sequence (RCP + FFMA chain) | 1:~15 | 78 | IEEE rounding; may call `__cuda_sm20_div_rn_f32` |
| 28 | `div.rn.f64` | N-R sequence (DFMA chain) | 1:~30 | 78 | Inline template from `0x1700000` region |
| 29 | `div.s32` / `div.u32` | IMAD-based sequence | 1:~35 | 78 | Calls `__cuda_sm20_div_[su]32`; magic-number multiply |
| 30 | `div.s64` / `div.u64` | Multi-instruction sequence | 1:~60 | 78 | Calls `__cuda_sm20_div_[su]64`; no HW on any SM |
| 31 | `rem.s32` / `rem.u32` | div + IMAD back-multiply + IADD3 | 1:~38 | 78 | Remainder = a - (a/b)*b |
| 32 | `rem.s64` / `rem.u64` | Multi-instruction sequence | 1:~65 | 78 | Calls `__cuda_sm20_rem_[su]64` |
| 33 | `rcp.approx.f32` | `MUFU.RCP Rd, Ra` | 1:1 | P | SFU pipe, ~4 cycle latency |
| 34 | `rcp.rn.f32` | `MUFU.RCP` + FFMA refinement | 1:~8 | 78 | Newton-Raphson to IEEE precision |
| 35 | `rcp.approx.f64` | `MUFU.RCP64H` + widen | 1:2+ | 5 | Approximate double reciprocal |
| 36 | `rsqrt.approx.f32` | `MUFU.RSQ Rd, Ra` | 1:1 | P | SFU reciprocal square root |
| 37 | `sqrt.approx.f32` | `MUFU.RSQ` + `FMUL` (+ fixup) | 1:3+ | 5 | rsqrt then multiply by input |
| 38 | `sqrt.rn.f32` | N-R sequence | 1:~15 | 78 | May call `__cuda_sm20_sqrt_rn_f32` |
| 39 | `sqrt.rn.f64` | N-R sequence | 1:~25 | 78 | Inline DFMA template |
| 40 | `sin.approx.f32` | `MUFU.SIN Rd, Ra` | 1:1 | P | SFU pipe |
| 41 | `cos.approx.f32` | `MUFU.COS Rd, Ra` | 1:1 | P | SFU pipe |
| 42 | `lg2.approx.f32` | `MUFU.LG2 Rd, Ra` | 1:1 | P | SFU pipe |
| 43 | `ex2.approx.f32` | `MUFU.EX2 Rd, Ra` | 1:1 | P | SFU pipe |
| 44 | `tanh.approx.f32` | `MUFU.TANH Rd, Ra` | 1:1 | P | SFU pipe, sm_75+ only |
| 45 | `setp.lt.s32` (and variants) | `ISETP.LT Pd, PT, Ra, Rb` | 1:1 | P | Comparison op in ISETP modifier |
| 46 | `setp.lt.f32` (and variants) | `FSETP.LT Pd, PT, Ra, Rb` | 1:1 | P | Float comparison; NaN-aware via modifier |
| 47 | `setp.lt.f64` (and variants) | `DSETP.LT Pd, PT, Ra, Rb` | 1:1 | P | Double-precision comparison |
| 48 | `selp.b32` | `SEL Rd, Ra, Rb, Pp` | 1:1 | P | Predicate-driven select |
| 49 | `and.b32` | `LOP3 Rd, Ra, Rb, RZ, 0xC0` | 1:1 | 5 | 3-input LUT; AND = truth table 0xC0 |
| 50 | `or.b32` | `LOP3 Rd, Ra, Rb, RZ, 0xFC` | 1:1 | 5 | OR = truth table 0xFC |
| 51 | `xor.b32` | `LOP3 Rd, Ra, Rb, RZ, 0x3C` | 1:1 | 5 | XOR = truth table 0x3C |
| 52 | `not.b32` | `LOP3 Rd, Ra, RZ, RZ, 0x33` | 1:1 | 5 | NOT = truth table 0x33 (invert first input) |
| 53 | `and.pred` | `PLOP3 Pd, PT, Pa, Pb, PT, 0xC0` | 1:1 | 5 | Predicate-file version of LOP3 |
| 54 | `or.pred` | `PLOP3 Pd, PT, Pa, Pb, PT, 0xFC` | 1:1 | 5 | Predicate OR |
| 55 | `shl.b32` | `SHF.L Rd, RZ, Ra, Rb` | 1:1 | 5 | Funnel shift left; high input is zero |
| 56 | `shr.u32` | `SHF.R.U32 Rd, Ra, Rb, RZ` | 1:1 | 5 | Logical shift right |
| 57 | `shr.s32` | `SHF.R.S32 Rd, Ra, Rb, RZ` | 1:1 | 5 | Arithmetic shift right |
| 58 | `mov.b32` | `MOV Rd, Ra` | 1:1 | P | Register move |
| 59 | `mov.b64` | `MOV` pair (lo+hi) | 1:2 | P | Two 32-bit moves for 64-bit value |
| 60 | `cvt.f32.s32` | `I2F.F32.S32 Rd, Ra` | 1:1 | P | Integer-to-float conversion |
| 61 | `cvt.s32.f32` | `F2I.S32.F32 Rd, Ra` | 1:1 | P | Float-to-integer conversion |
| 62 | `cvt.f64.f32` | `F2F.F64.F32 Rd, Ra` | 1:1 | P | Float widening |
| 63 | `cvt.f32.f64` | `F2F.F32.F64 Rd, Ra` | 1:1 | P | Float narrowing |
| 64 | `cvt.f16.f32` | `F2F.F16.F32 Rd, Ra` | 1:1 | P | Float to half |
| 65 | `cvt.f32.f16` | `F2F.F32.F16 Rd, Ra` | 1:1 | P | Half to float |
| 66 | `cvt.rni.s32.f32` | `F2I.S32.F32.RNI Rd, Ra` | 1:1 | P | Round-to-nearest-int conversion |
| 67 | `ld.global.b32` | `LDG Rd, [Ra]` | 1:1 | P | Global load; cache mode in modifier |
| 68 | `ld.shared.b32` | `LDS Rd, [Ra]` | 1:1 | P | Shared memory load |
| 69 | `ld.local.b32` | `LDL Rd, [Ra]` | 1:1 | P | Local memory load (spill region) |
| 70 | `ld.const.b32` | `LDC Rd, c[bank][offset]` | 1:1 | P | Constant memory load |
| 71 | `ld.param.b32` | `LDC Rd, c[0][offset]` | 1:1 | P | Parameter load via constant bank 0 |
| 72 | `st.global.b32` | `STG [Ra], Rb` | 1:1 | P | Global memory store |
| 73 | `st.shared.b32` | `STS [Ra], Rb` | 1:1 | P | Shared memory store |
| 74 | `st.local.b32` | `STL [Ra], Rb` | 1:1 | P | Local memory store |
| 75 | `atom.global.add.s32` | `ATOMG.ADD Rd, [Ra], Rb` | 1:1 | P | Global atomic add |
| 76 | `atom.shared.add.s32` | `ATOMS.ADD Rd, [Ra], Rb` | 1:1 | P | Shared atomic add |
| 77 | `atom.global.cas.b32` | `ATOMG.CAS Rd, [Ra], Rb, Rc` | 1:1 | P | Global atomic compare-and-swap |
| 78 | `red.global.add.s32` | `RED.ADD [Ra], Rb` | 1:1 | P | Reduction (no return value) |
| 79 | `bra` | `BRA target` | 1:1 | P | Unconditional branch |
| 80 | `@%p0 bra` | `@P0 BRA target` | 1:1 | P | Predicated branch; predicate in P file |
| 81 | `call` | `CALL target` | 1:1 | P | Function call (ABI save/restore added later) |
| 82 | `ret` | `RET` | 1:1 | P | Return from function |
| 83 | `exit` | `EXIT` | 1:1 | P | Terminate thread |
| 84 | `bar.sync` | `BAR.SYNC` | 1:1 | P | CTA-level barrier |
| 85 | `membar.cta` | `MEMBAR.CTA` | 1:1 | P | Memory fence (CTA scope) |
| 86 | `membar.gl` | `MEMBAR.GL` | 1:1 | P | Memory fence (GPU scope) |
| 87 | `shfl.sync.bfly.b32` | `SHFL.BFLY Rd\|Pp, Ra, Rb, Rc` | 1:1 | P | Warp shuffle butterfly |
| 88 | `vote.sync.ballot.b32` | `VOTE.ALL Rd, Pp` | 1:1 | P | Warp vote ballot |
| 89 | `redux.sync.add.s32` | `REDUX.ADD Rd, Ra` | 1:1 | P | Warp reduction (sm_80+) |
| 90 | `popc.b32` | `POPC Rd, Ra` | 1:1 | P | Population count |
| 91 | `clz.b32` | `FLO Rd, Ra` (inverted) | 1:1 | 5 | Count leading zeros via find-leading-one |
| 92 | `brev.b32` | `BREV Rd, Ra` | 1:1 | P | Bit reversal |
| 93 | `bfe.u32` | `BMSK` / multi-instr | 1:1+ | 5 | Bit field extract; may use LEA+SHF on some SM |
| 94 | `bfi.b32` | `LOP3` + `SHF` sequence | 1:2+ | 5 | Bit field insert; no single SASS equivalent |
| 95 | `prmt.b32` | `PRMT Rd, Ra, Rb, Rc` | 1:1 | P | Byte permute |
| 96 | `fma.rn.f16` | `HFMA2 Rd, Ra, Rb, Rc` (sm_53+) | 1:1 | P | Native half FMA; promoted to FP32 pre-sm_53 |
| 97 | `add.f16x2` | `HADD2 Rd, Ra, Rb` (sm_53+) | 1:1 | 2 | Packed FP16x2; Phase 2 promotes if no HW |
| 98 | `cvta.to.global` | `CVTA Rd, Ra` | 1:1 | P | Address space conversion (generic to global) |
| 99 | `lop3.b32` | `LOP3 Rd, Ra, Rb, Rc, immLut` | 1:1 | P | Direct LUT-encoded 3-input logic |
| 100 | `shf.l.b32` | `SHF.L Rd, Ra, Rb, Rc` | 1:1 | P | Funnel shift left |
| 101 | `dp4a.u32.u32` | `IDP Rd, Ra, Rb, Rc` (sm_61+) | 1:1 | P | 4-element dot product accumulate |
| 102 | `nanosleep` | `NANOSLEEP Rd` | 1:1 | P | Thread sleep (sm_70+) |

Phase legend: **P** = parser (Bison reduction action, measured by DAGgen-time). **2** = Phase 2 PromoteFP16. **5** = Phase 5 ConvertUnsupportedOps (MercConverter). **45** = Phase 45 MidExpansion. **78** = Phase 78 LateExpansionUnsupportedOps. Entries marked 45/78 are kept as single Ori instructions through optimization and expanded late to preserve optimization opportunities (LICM, CSE, strength reduction can operate on the unexpanded form).

ROT13 encoding in the binary:
```text
SNQQ  = FADD       VZNQ  = IMAD       SSZN  = FFMA
VNQQ3 = IADD3      QZHY  = DMUL       YQT   = LDG
FGT   = STG        OEN   = BRA        RKVG  = EXIT
ERG   = RET        ONE   = BAR        FGF   = STS
```

Key semantic differences at the transition:

1. **Type moves into the opcode**: PTX `add.f32` becomes `FADD` (the "F" encodes float); PTX `add.s32` becomes `IADD3` (the "I" encodes integer). The type qualifier disappears from the instruction syntax.

2. **Register namespace unification**: PTX's typed virtual registers (`%r` for int, `%f` for float, `%rd` for 64-bit, `%p` for predicate) merge into Ori's four register files (R, UR, P, UP) with type tracked in the register descriptor at offset `+64`.

3. **Operand count changes**: SASS `IADD3` takes 3 source operands where PTX `add` takes 2 -- the third source defaults to `RZ` (the hardware zero register). This is handled by the expansion in `sub_9E6600`.

4. **Multi-instruction expansion**: Complex PTX operations expand to multiple SASS instructions. A PTX `div.f32` may become a Newton-Raphson sequence of `RCP` + `FMUL` + correction iterations.

5. **Predication mapping**: PTX `@%p0 instruction` maps to an Ori predicate operand in the P register file, attached to the instruction node's predicate slot.

### Mapping Tables

**Table 1 -- PTX type qualifier to SASS opcode prefix.** The type qualifier is consumed during MercConverter dispatch and encoded into the SASS mnemonic prefix. Peephole field 0x7E (slot 126) carries the data type qualifier internally (547 = `.f32`, 548 = `.f64`); the ISel field 22 encodes integer sign (99 = unsigned, 101 = signed). The `U` prefix denotes the uniform-register variant, not a type.

| PTX type qualifier | SASS prefix | Arithmetic | Compare | Conversion | Example |
|---|---|---|---|---|---|
| `.f16` / `.f16x2` | `H` | `HADD2`, `HFMA2`, `HMUL2` | `HSETP2`, `HSET2` | `F2F` (via width) | `add.f16` -> `HADD2` |
| `.bf16` / `.bf16x2` | `H` | `HADD2_F32` | `HSETP2` | `F2F` | `add.bf16` -> `HADD2` |
| `.f32` | `F` | `FADD`, `FFMA`, `FMUL` | `FSETP`, `FSET` | `F2I`, `F2F` | `add.f32` -> `FADD` |
| `.f64` | `D` | `DADD`, `DFMA`, `DMUL` | `DSETP` | `F2I`, `F2F` | `mul.f64` -> `DMUL` |
| `.s8`--`.s64` | `I` | `IADD3`, `IMAD`, `IMAD_WIDE` | `ISETP` | `I2I`, `I2F` | `add.s32` -> `IADD3` |
| `.u8`--`.u64` | `I` | `IADD3`, `IMAD` | `ISETP` | `I2I`, `I2F` | `mul.lo.u32` -> `IMAD` |
| `.b8`--`.b128` | (none) | `LOP3`, `SHF`, `PRMT` | `ISETP` | `I2I` | `and.b32` -> `LOP3` |
| `.pred` | `P` (in operand) | `PLOP3` | -- | `P2R`, `R2P` | `and.pred` -> `PLOP3` |

Signed vs. unsigned `.s`/`.u` distinction does not change the SASS mnemonic -- both map to `I`-prefixed instructions. The sign is encoded in a modifier bit (ISel field 22: value 99 for `.u`, 101 for `.s`), which the SASS printer emits as `.U32` vs no suffix on `ISETP`.

**Table 2 -- PTX state space to SASS memory instruction.** The state space qualifier on `ld`/`st` selects the SASS opcode suffix. Peephole field 0x119 (slot 281) carries the address space qualifier with enum values 1435--1440. Atomics follow the same suffix pattern (`ATOM`/`ATOMG`/`ATOMS`).

| PTX state space | SASS load | SASS store | SASS atomic | Field 281 value |
|---|---|---|---|---|
| `.global` | `LDG` | `STG` | `ATOMG` | 1436 |
| `.shared` | `LDS` | `STS` | `ATOMS` | 1437 |
| `.local` | `LDL` | `STL` | -- | 1438 |
| `.const` | `LDC` | -- | -- | 1439 |
| `.param` | `LDC` (bank 0) | -- | -- | 1440 |
| (generic) | `LD` | `ST` | `ATOM` | 1435 |

Additional memory instructions: `LDSM` (load shared matrix, sm_75+), `LDGSTS` (async global-to-shared, sm_80+), `LDGDEPBAR` (load with dependency barrier), `LDTRAM` (load TMEM, sm_100+).

**Table 3 -- PTX comparison/rounding modifiers to SASS encoding.** Modifiers that survive as SASS instruction suffixes are encoded through the modifier-encode functions in the 0x10B region. Field 345 (slot 0x159) selects rounding; field 150 (slot 0x096) selects comparison mode. The SASS printer emits these via `vtable[1760]` (rounding) and `vtable[1392]`/`vtable[3528]` (comparison).

| PTX modifier | Field ID | Enum value | SASS suffix | Encoding bits | Encoder |
|---|---|---|---|---|---|
| `.rn` (round nearest) | 345 | 1900 | `.RN` | field 300 = 1513 | `sub_10B6220` (3-bit) |
| `.rz` (round zero) | 345 | 1901 | `.RZ` | field 300 = 1515 | `sub_10B6220` |
| `.rm` (round minus-inf) | 345 | 1902 | `.RM` | field 300 = 1515 | `sub_10B6220` |
| `.rp` (round plus-inf) | 345 | 1903 | `.RP` | field 300 = 1516 | `sub_10B6220` |
| `.ftz` (flush to zero) | 301 | 1521 | `.FTZ` | 1-bit flag | `sub_10B6180` (1-bit) |
| `.sat` (saturate) | (stage) | -- | `.SAT` | 1-bit flag | `sub_10B6180` |
| `.eq .ne .lt .le .gt .ge` | 150 | 650, 651 | `.EQ .NE .LT .LE .GT .GE` | 4-bit, `(mod>>4)&0xF` | `sub_10B4650` (4-bit) |
| `.equ .neu .ltu .leu .gtu .geu` | 150 | 650, 651 | `.EQU .NEU ...` | 4-bit + unordered bit | `sub_10B4650` |
| `.num .nan` | 150 | 650, 651 | `.NUM .NAN` | 4-bit | `sub_10B4650` |

Rounding modes `.rz` and `.rm` share encoding value 1515 because the hardware distinguishes them by a secondary bit in field 301 (1520 vs 1521). The PTX comparison operators `.lo .ls .hi .hs` are unsigned aliases for `.lt .le .gt .ge` and produce identical SASS encoding.

## Error Detection During Lowering

The bridge phases include two error detection mechanisms:

**Internal compiler error assertion** (`sub_9EB990`, 1.4 KB): three references to `"Internal compiler error."`. Called when a bridge phase encounters an impossible IR state (e.g., an opcode value outside the known range in the MercConverter dispatch switch). Triggers `longjmp`-based fatal abort via `sub_42F590` back to the driver's error recovery point in `sub_446240`.

**Uninitialized register detector** (`sub_A0B5E0`, 7 KB): `"Found %d potentially uninitialized register(s) in function %s"`. Walks the instruction list per block, checks register descriptor flags at offset `+48` (bit 5 = "defined"). Reports registers that appear as sources without any prior definition. This detector fires after the bridge phases to catch conversion errors that leave registers undefined.

## Key Data Structures

### Instruction Node

```text
Instruction (variable size, linked list node)
  +0     prev_ptr           // doubly-linked list: previous instruction
  +8     next_ptr           // doubly-linked list: next instruction
  +16    child_ptr          // child/expanded instruction chain
  +32    control_word_ptr   // set later during scheduling (initially NULL)
  +72    opcode             // byte 0: primary opcode
                            // byte 1 bits 4-5: modifier (masked with 0xCF)
  +80    operand_count      // number of operands
  +84    operand_array      // packed operand descriptors
```

### Operand Encoding

```text
Each operand is a packed 32-bit value:
  Bits 28-30: operand kind ((value >> 28) & 7)
    1 = register operand
    5 = predicate register
    (other values for immediate, constant bank, label, etc.)

  Lower bits: operand-kind-specific payload (register ID, immediate value, etc.)
```

### Register Descriptor

```text
Register descriptor (accessed via *(ctx+88) + 8*regId)
  +12    register number (int)
  +48    flags (bit 5 = "defined", other bits for liveness state)
  +64    type (3=address, 6=GPR, 7=predicate)
```

## Timing Boundary

The lowering spans two `--compiler-stats` timer phases:

| Timer | Covers |
|---|---|
| `DAGgen-time` | Bison parser reduction actions -> Ori instruction nodes, operand processing (`sub_6273E0`), basic block / CFG construction |
| `OCG-time` | Phases 0--13 (bridge), then phases 14--158 (optimization + codegen) |

The boundary between "lowering" and "optimization" is therefore between phase 13 (`GeneralOptimizeEarly`, the last bridge phase) and phase 14 (`DoSwitchOptFirst`, the first pure optimization). After phase 13, the IR is in its final SASS-opcode form with validated structure, ready for the main optimization pipeline.

## Cross-References

- [PTX Parser](ptx-parser.md) -- Flex scanner + Bison LALR(1) parser (the source of raw Ori IR)
- [Ori IR](../ir/overview.md) -- IR design: Code Object, basic blocks, instruction format, register files
- [Optimization Pipeline](optimizer.md) -- 159-phase pipeline (phases 0--13 are the bridge)
- [Phase Manager](../passes/phase-manager.md) -- PhaseManager object, phase factory, dispatch loop
- [Optimization Levels](../config/opt-levels.md) -- NvOpt levels 0--5 and their effect on recipes
- [SASS Opcodes](../reference/sass-opcodes.md) -- target SASS instruction set after lowering

## Function Map

| Address | Size | Callers | Identity | Confidence |
|---|---|---|---|---|
| `0x451730` | 14 KB | 1 | Parser init, special register setup | HIGH |
| `0x46E000` | 93 KB | 1 | Opcode table builder (1,141 per-opcode calls) | HIGH |
| `0x4CE6B0` | 48 KB | 1 | Bison LALR(1) parser (512 productions) | HIGH |
| `0x6273E0` | 44 KB | N | Operand processing (6-bit type switch) | MEDIUM |
| `0x9D4380` | 7 KB | ~10 | Instruction builder / inserter into linked list | HIGH |
| `0x9D76D0` | 18 KB | 1 | Memory instruction legalization (load/store) | HIGH |
| `0x9D80E0` | 17 KB | 1 | Memory instruction legalization (variant) | HIGH |
| `0x9DA100` | 9 KB | 1 | Arithmetic operation handler (case 6) | HIGH |
| `0x9DE890` | 17 KB | 1 | Control flow legalization (branch/call) | MEDIUM |
| `0x9DDEE0` | 14 KB | 1 | Address computation legalization | MEDIUM |
| `0x9E6600` | 25 KB | 1 | Instruction expansion (64-bit split, etc.) | HIGH |
| `0x9E8B20` | 17 KB | 1 | Texture/surface lowering | MEDIUM |
| `0x9EB990` | 1.4 KB | 3 | Internal compiler error assertion | HIGH |
| `0x9EC340` | 23 KB | 1 | Multi-operand instruction legalization | MEDIUM |
| `0x9ED2D0` | 25 KB | 1 | Opcode dispatch (master switch, `& 0xCF` mask) | HIGH |
| `0x9EE390` | 20 KB | 1 | Intrinsic descriptor file loader | MEDIUM |
| `0x9EF5E0` | 27 KB | 1 | Post-MercConverter lowering (`"CONVERTING"`) | HIGH |
| `0x9F1A90` | 35 KB | 1 | MercConverter main instruction conversion pass | HIGH |
| `0x9F3340` | 7 KB | 1 | MercConverter orchestrator (`"After MercConverter"`) | HIGH |
| `0xA0B5E0` | 7 KB | N | Uninitialized register detector | HIGH |
| `0xA3A7E0` | 6 KB | N | Scheduling statistics printer (phase 9 output) | VERY HIGH |
