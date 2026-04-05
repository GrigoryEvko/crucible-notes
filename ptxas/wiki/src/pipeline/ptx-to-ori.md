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

```
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

The 44 KB operand processing function handles all PTX operand forms. It switches on a 6-bit type encoding extracted as `v12 & 0x3F`:

| Type bits | Operand kind | PTX syntax | Processing |
|---|---|---|---|
| Register | Direct register reference | `%r1`, `%rd1`, `%f1` | Look up register descriptor via `*(ctx+88) + 8*regId` |
| Register pair | 64-bit register pair | `%rd1` (on 32-bit ALU) | Allocate paired descriptors, link hi/lo |
| Immediate | Integer constant | `42`, `0xFF` | Pack into operand field |
| Float immediate | Floating-point constant | `0F3F800000` | Encode IEEE 754 bits |
| Address | Base + offset | `[%rd1+16]` | Compute effective address, annotate state space |
| Constant bank | Constant memory ref | `c[2][0x100]` | Bank index + offset encoding |
| Label | Branch target | `$L__BB0_1` | Resolve to basic block index |
| Special register | Built-in register | `%ntid.x`, `%laneid` | Map to internal ID from `sub_451730` table |

String evidence in `sub_6273E0`:
- `".nv.reservedSmem.offset0"` -- reserved shared memory region handling
- `"COARSEOFFSET"` -- coarse-grained offset computation for large address spaces
- `"__$endLabel$__%s"` -- label generation for structured control flow expansion

The function bridges PTX's explicitly-typed operand model (where `.u32`, `.f32`, `.b64` qualifiers are part of the syntax) to Ori's implicitly-typed model where the operand type is determined by the SASS opcode.

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

```
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

```
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
| 10 | `EarlyOriSimpleLiveDead` | First dead code elimination pass. Removes instructions whose results are unused. Uses the SIMD-accelerated bitvector library (`sub_BDBA60`..`sub_BDE150`) for liveness computation. |
| 11 | `ReplaceUniformsWithImm` | Folds known-constant uniform register loads into immediate operands. Important for kernel launch parameters passed through constant memory. |
| 12 | `OriSanitize` | Second structural validation after all bridge transformations. Catches errors introduced by phases 1--11 before the main optimizer begins. |
| 13 | `GeneralOptimizeEarly` | First compound optimization pass: copy propagation + constant folding + algebraic simplification in a single fixed-point iteration. Cleans up redundancies introduced by the bridge phases. |

## The MercConverter Engine

The MercConverter (`sub_9F1A90`, 35 KB) is the instruction conversion engine at the heart of `ConvertUnsupportedOps`. Despite its name referencing "Mercury" (NVIDIA's SASS encoding format), it operates purely at the IR level -- converting instruction semantics, not binary encodings.

### Call Chain

```
sub_9F3340 (orchestrator, 7KB)
  |
  +-- sub_9F1A90 (MercConverter main pass, 35KB)
  |     |
  |     +-- sub_9ED2D0 (opcode dispatch, 25KB)
  |     |     |
  |     |     |  Large switch on (*(instr+72)) with byte-1 mask:
  |     |     |    BYTE1(opcode) &= 0xCF  -- strips modifier bits 4-5
  |     |     |
  |     |     +-- case 1:  sub_9DA5C0 (2KB)   -- opcode class 1
  |     |     +-- case 6:  sub_9DA100 (9KB)   -- arithmetic operations
  |     |     +-- case 8:  sub_9D2440         -- specific class
  |     |     +-- case 10,11,149,151,152,290,291:
  |     |     |            sub_9D80E0 (17KB)  -- memory load/store
  |     |     +-- default: vfunc[0](a1, a2)   -- vtable dispatch
  |     |
  |     +-- sub_934630 (instruction creation utility, called N times)
  |
  +-- sub_9EF5E0 (post-conversion lowering, 27KB)
        |  string "CONVERTING"
        +-- sub_9EC160, sub_7C11F0, sub_7BFC30 (intrinsic expansion)
```

### Per-Category Handlers

| Handler | Size | Category | Key behavior |
|---|---|---|---|
| `sub_9D76D0` | 18 KB | Memory legalization (load/store) | Register type dispatch: 6=GPR, 7=predicate, 3=address. Uses `sub_9D4380` (instruction builder) and `sub_9CD420` (predication). |
| `sub_9D80E0` | 17 KB | Memory legalization (variant) | Same opcode set as `sub_9D76D0`, alternate code path for different operand patterns. |
| `sub_9EC340` | 23 KB | Multi-operand legalization | Operand type test: `(v >> 28) & 7 == 1` means register. Register class query via `sub_7BE7B0`. Creates new instructions via `sub_7DEAD0`. |
| `sub_9E6600` | 25 KB | Instruction expansion | Splits instructions into multiple SASS equivalents (e.g., 64-bit ops on 32-bit ALU). Uses `sub_9D4380` ~10 times. |
| `sub_9E8B20` | 17 KB | Texture/surface lowering | Register type 6 = GPR. Manipulates bitmask at register descriptor offset `+48`. |
| `sub_9DA100` | 9 KB | Arithmetic operations | Handles opcode case 6 -- standard ALU instruction legalization. |
| `sub_9DE890` | 17 KB | Control flow legalization | Branch/call instruction patterns. Calls `sub_9D4380` (builder) 5 times. |
| `sub_9DDEE0` | 14 KB | Address computation | Address arithmetic lowering, complex addressing mode decomposition. |

### Intrinsic Descriptor Loading

`sub_9EE390` (20 KB) loads architecture-specific instruction descriptions from a file (`"IntrinsicDescrFile=%s"`). This allows the MercConverter to query which intrinsic operations are natively supported on the target SM and which require multi-instruction expansion. The descriptor file is architecture-versioned and loaded once during the first compilation of a kernel targeting that architecture.

## The PTX-to-SASS Opcode Transition

The fundamental semantic transformation during lowering: PTX uses high-level, explicitly-typed opcodes; Ori uses SASS-level opcodes where the type is encoded in the mnemonic. All SASS opcode strings in the binary are ROT13-encoded.

```
PTX source (typed virtual ISA)          Ori IR (SASS machine-level)
---------------------------------       ---------------------------------
add.f32  %r1, %r2, %r3           -->   FADD  R1, R2, R3
add.s32  %r4, %r5, %r6           -->   IADD3 R4, R5, R6, RZ
mul.f64  %d1, %d2, %d3           -->   DMUL  D1, D2, D3
mad.lo.s32 %r7, %r8, %r9, %r10  -->   IMAD  R7, R8, R9, R10
ld.global.f32 %r11, [%rd1]       -->   LDG   R11, [R1]
st.shared.f32 [%rd2], %r12       -->   STS   [R2], R12
bra  $L__BB0_1                   -->   BRA   bix1
@%p0 bra $L__BB0_2               -->   @P0 BRA bix2
exit                              -->   EXIT
bar.sync 0                        -->   BAR
```

ROT13 encoding in the binary:
```
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

## Error Detection During Lowering

The bridge phases include two error detection mechanisms:

**Internal compiler error assertion** (`sub_9EB990`, 1.4 KB): three references to `"Internal compiler error."`. Called when a bridge phase encounters an impossible IR state (e.g., an opcode value outside the known range in the MercConverter dispatch switch). Triggers `longjmp`-based fatal abort via `sub_42F590` back to the driver's error recovery point in `sub_446240`.

**Uninitialized register detector** (`sub_A0B5E0`, 7 KB): `"Found %d potentially uninitialized register(s) in function %s"`. Walks the instruction list per block, checks register descriptor flags at offset `+48` (bit 5 = "defined"). Reports registers that appear as sources without any prior definition. This detector fires after the bridge phases to catch conversion errors that leave registers undefined.

## Key Data Structures

### Instruction Node

```
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

```
Each operand is a packed 32-bit value:
  Bits 28-30: operand kind ((value >> 28) & 7)
    1 = register operand
    5 = predicate register
    (other values for immediate, constant bank, label, etc.)

  Lower bits: operand-kind-specific payload (register ID, immediate value, etc.)
```

### Register Descriptor

```
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
