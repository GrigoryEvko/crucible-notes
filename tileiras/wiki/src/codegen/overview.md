# Codegen Overview

## Abstract

The backend half of `tileiras` starts where the MLIR pipeline ends: an
NVVM-ready `gpu.module` with a resolved `#nvvm.target`. The program is no
longer TileIR. It is an LLVM/NVVM module that must be linked against device
libraries, optimized, lowered through NVPTX target rules, selected into
machine instructions, and printed as PTX text for `ptxas`. This page states
the contracts and invariants each stage must preserve. Child pages document
the dispatchers, opcode tables, and modifier vocabularies that implement
those contracts.

The useful model is:

```text
MLIR llvm/nvvm dialect
    -> llvm::Module
    -> linked device-library module
    -> optimized LLVM module
    -> SelectionDAG and machine functions
    -> MCInst stream
    -> PTX assembly
```

Child pages document the detailed reverse-engineered subsystems. This
overview lays out the backend contracts that matter for users and
reimplementers.

## Backend Contract

| Stage | Responsibility | Public invariant |
|---|---|---|
| LLVM module handoff | Translate MLIR LLVM dialect to an `llvm::Module` and attach target triple, chip, features, and data layout. | The module is already ABI-ready; no high-level TileIR operations remain. |
| Device library linkage | Link embedded or external device bitcode used by math and NVVM helper calls. | Undefined device helper calls must be resolved before final codegen. |
| LLVM optimization | Run the LLVM optimization pipeline selected by the requested optimization level. | Optimizations preserve NVVM address spaces, kernel attributes, and libdevice semantics. |
| NVPTX target lowering | Lower calls, formal arguments, returns, intrinsics, address spaces, and custom target nodes. | Param-space values and kernel arguments are handled through NVPTX ABI rules, not generic pointer rules. |
| Instruction selection | Select custom NVPTX nodes first, then fall back to generated SelectionDAG matcher tables. | Feature-gated intrinsics are rejected or expanded before an illegal PTX instruction can be emitted. |
| Machine-function passes | Run target passes for argument lowering, image handles, scheduling, register allocation, and MIR cleanup. | Machine IR still carries enough target information for correct PTX emission. |
| PTX emission | Print PTX mnemonics, operands, modifiers, sections, directives, and target attributes. | Emitted PTX matches the resolved target feature set and is suitable for `ptxas`. |

## Target Initialization

The backend registers both 32-bit and 64-bit NVPTX targets, constructs
subtarget information from the target triple, CPU string, and feature string,
then builds or reuses a target machine for the compilation. The normal CUDA
device path is 64-bit and uses the `nvptx64-nvidia-cuda` triple.

Target initialization provides:

- target registry entries for `nvptx` and `nvptx64`;
- MC layer objects for registers, instruction descriptions, subtarget features,
  and asm output;
- an NVPTX target machine keyed by triple, chip, and feature set;
- a feature bitset used by target lowering and instruction selection.

The target feature set is the guardrail for newer instructions. Tensor memory,
TMA, WGMMA, tcgen05, block-scaled MMA, cluster operations, and related PTX
modifiers reach selection only when the subtarget says they are legal.

## MLIR-To-LLVM Handoff

`gpu.module` operations carrying the NVVM target attribute leave MLIR through a translator that maps each `nvvm.*` op to the matching `llvm.nvvm.*` intrinsic, then walks `llvm` dialect operations into the corresponding LLVM IR opcodes. The translator is a one-to-one mapping table: `nvvm.barrier0` becomes `@llvm.nvvm.barrier0`, `nvvm.mma.sync` becomes `@llvm.nvvm.mma.*`, `nvvm.wgmma.mma_async` becomes `@llvm.nvvm.wgmma.*`, and so on. There is no novel rewriting in this step. What matters is that NVPTX-specific information already encoded in the MLIR dialect — kernel attributes, address spaces, target metadata — must survive the translation unchanged.

The output is an `llvm::Module` with the `nvptx64-nvidia-cuda` triple set, the target chip and feature string attached to every kernel function, and the NVPTX data layout active. From here on the module is an ordinary LLVM IR module and the backend reads it the same way `clang` does.

## LLVM Optimization

After translation and device-library linkage, the module goes through the LLVM optimization pipeline selected by `O0`, `O1`, `O2`, `O3`, `Os`, or `Oz`. The pipeline is the standard `PassBuilder` shape — function simplification, CGSCC inlining, loop optimization, vectorization — followed by NVIDIA-private peephole and lowering passes the binary's PassRegistry table lists by name. The NVIDIA-private set covers NVPTX-specific patterns LLVM upstream does not optimize: lowering of `llvm.nvvm.barrier*` intrinsics, address-space inference and propagation, kernel-attribute preservation, libdevice math-helper specialization, and a final NVVM-aware GVN/DCE sweep.

NVVM-specific properties must survive ordinary LLVM optimization. Kernel functions retain `nvvm.kernel` metadata, NVVM intrinsics never get rewritten into target-illegal forms, NVPTX address spaces stay distinct, and libdevice calls keep the ABI the NVPTX backend expects. Any optimization pass that strips this metadata makes downstream selection fall back to a generic path that does not understand NVPTX `param`, `shared`, or `tmem` semantics.

## NVPTX ABI Lowering

NVPTX has a stricter ABI than ordinary LLVM IR suggests. Kernel parameters live in address-space 101 (`param`), device-function parameters use the by-value or by-pointer convention NVPTX defines, return values flow through the param space too, and `byval` aggregates need explicit unpacking into scalar or vector register-passing lowerings. Grid constants live in their own constant address space. None of this is the generic `pointer` lowering LLVM's IR-level legalizer would produce.

The NVPTX target lowering hook runs before SelectionDAG building and rewrites each formal argument, call, return, and address-space cast into the form the selector and the AsmPrinter both expect. Param-space values become `NVPTXISD::LoadParam` / `StoreParam` chains; kernel arguments become explicit `param`-space loads keyed by formal-arg index; by-value aggregates become a sequence of scalar param loads spelled out per field. Once this pass completes, no `inttoptr` or `addrspacecast` between mismatched NVPTX address spaces remains in the function. See [Lowering Formal Arguments](nvptx-target-lowering-call-and-args.md#lowering-formal-arguments) and [Lowering Calls](nvptx-target-lowering-call-and-args.md#lowering-calls) for the formal-arg shape lattice and the call-prototype layout.

## Instruction Selection

Selection runs in three layers. The intrinsic-with-chain selector handles NVVM intrinsics that carry memory or control-flow chains and routes most cases to per-family emitters or to a secondary intrinsic-ID dispatcher. The vector load/store selector handles the NVPTX-private vector memory opcodes (the v2/v4/v8 forms over global/shared/param/tmem) plus tensor-memory routing for Blackwell. Both fast selectors fall through to the generated MatcherTable on unrecognized cases, and the MatcherTable runs a saturating-int64 cost scorer over candidate TableGen patterns. The scorer reads a per-opcode predicate-matrix row to decide whether the pattern is legal on the active subtarget before any cost accumulates.

Feature-gated intrinsics — TMA, tensor-memory, WGMMA, tcgen05, `mma.block_scale`, cluster operations, special registers, async barriers — pass through validators that consult the subtarget feature bitmap and emit a diagnostic on failure rather than letting an illegal PTX instruction reach the printer. See [ISelDAG and MatcherTable — Selector Layers](iseldag-and-matchertable.md#selector-layers) for the dispatcher shape, [MatcherTable and Cost Scoring](iseldag-and-matchertable.md#matchertable-and-cost-scoring) for the 119-case scorer, and the operand-class vocabulary the predicate helpers consume.

## PTX Emission

The AsmPrinter is a single LTO-folded function with a 6,388-case dispatcher over MC opcodes. Each case selects one of 297 shared print-shape bodies; each body interleaves literal text, operand slots, and modifier-helper calls in the order `ptxas` requires. Mnemonic lookup goes through a parallel pair of `.rodata` offset tables keyed by MC opcode, returning a byte offset into an obfuscated mnemonic pool that is decrypted in place on first use via an `xor (3 * i) mod 256` walking cipher. Physical-register names use the same scheme on a smaller 586-byte pool.

Module-level emission produces the `.version` / `.target` / `.address_size` header, kernel directives (`.entry`, `.reqntid`, `.maxntid`, `.minnctapersm`, `.maxnreg`, cluster directives), global and managed-variable declarations, then per-function bodies. Each function emits its frame setup, the virtual-register declarations grouped by class, and the basic-block sequence of MC instructions. The printer performs no subtarget legality checks: by the time an opcode reaches this layer, the selector and the machine verifier have already proved it is legal for the chosen target.

See [AsmPrinter — MC Switch Shape Population Table](asm-printer-monster-and-windows.md#mc-switch-shape-population-table) for the dispatcher partition and [AsmWriter String Pools and the XOR-3 Walking Cipher](asm-printer-monster-and-windows.md#asmwriter-string-pools-and-the-xor-3-walking-cipher) for the mnemonic-pool layout, and [Per-SM Emission Templates](per-sm-emission-templates.md) for the actual PTX template strings emitted per SM tier.

## End-To-End Algorithm

The whole codegen path can be read as a sequence of structurally distinct stages, each with a published contract from the table above. From `gpu.module` to PTX text:

1. Translate the MLIR module to LLVM IR, mapping `nvvm.*` ops to `llvm.nvvm.*` intrinsics and preserving NVPTX address spaces and kernel attributes.
2. Link device libraries so libdevice math helpers and NVVM intrinsic implementations are resolved.
3. Resolve the NVPTX target — triple, chip, feature set — and reuse or construct the target machine keyed by that tuple.
4. Run the requested LLVM optimization pipeline (`PassBuilder` shape plus NVIDIA-private peepholes).
5. Per function: run NVPTX target lowering for arguments, calls, returns, address-space casts, and intrinsic legalization.
6. Per function: build the SelectionDAG, run the three-layer selector, build the MachineFunction, and run NVPTX-specific machine passes for argument lowering, scheduling, register allocation, and MIR cleanup.
7. Run the AsmPrinter to produce the final PTX text.

A reimplementation that keeps these seven stages and their published contracts can vary internal data structures freely without breaking any consumer downstream of the printer.

## Codegen Invariants

- The module has exactly one resolved NVPTX target before backend emission.
- Kernel functions retain `nvvm.kernel` and launch metadata through LLVM
  optimization.
- Address spaces remain semantic: global, shared, constant, local, parameter,
  and tensor memory are not interchangeable.
- Param-space values are lowered through NVPTX ABI code, not generic pointer
  lowering.
- Custom intrinsic selection validates subtarget support before emission.
- Generated matcher-table selection remains the default path for ordinary DAG
  nodes.
- Vector memory selection preserves lane grouping and address-space
  classification.
- TMA, WGMMA, tcgen05, tensor memory, cluster, and block-scaled MMA operations
  are subtarget-gated.
- PTX emission prints the instruction selected for the target, not a generic
  approximation.

## Cross-Links

- [NVPTX Bring-up and Target Init](nvptx-bring-up-and-target-init.md) covers target registration and target-machine construction.
- [NVPTX Subtarget and Feature Matrix](nvptx-subtarget-and-feature-matrix.md) covers per-SM feature bitsets and subtarget construction.
- [NVPTX Target Lowering — Calls and Arguments](nvptx-target-lowering-call-and-args.md) covers parameter, call, and custom-node lowering.
- [ISelDAG and MatcherTable](iseldag-and-matchertable.md) covers instruction selection.
- [AsmPrinter Monster and Windows](asm-printer-monster-and-windows.md) covers the MC dispatch shape and AsmWriter string pools.
- [Per-SM Emission Templates](per-sm-emission-templates.md) covers SM-specific opcode families.
- [Atomic, Warp, Sreg, and Fence Emission](atomic-warp-sreg-fence.md) covers atomic, warp-synchronous, special-register, and fence opcodes.
- [TMA + Tensormap + cp.async.bulk Emission](tma-tensormap-and-cp-async-bulk.md) covers TMA and tensor-map emission.
- [tcgen05 / WGMMA / mbarrier / Cluster Emission](tcgen05-wgmma-mbarrier-cluster.md) covers tensor memory, WGMMA, barriers, and cluster features.
- [ldmatrix / stmatrix and Register-Class Vtables](ldmatrix-stmatrix-and-register-class-vtables.md) covers matrix-load/store opcodes and the register-class vtable banks.
- [libdevice Overview](../libdevice/overview.md) covers device-library linkage and libdevice behavior.
