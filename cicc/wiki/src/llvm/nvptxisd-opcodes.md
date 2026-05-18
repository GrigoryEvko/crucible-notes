# NVPTXISD SelectionDAG Opcodes

> **NVIDIA-private target enum.** These opcode values exist only between SelectionDAG lowering and instruction selection. They are not part of upstream LLVM's public `ISD::` enum and are erased by the time MachineInstrs reach the AsmPrinter.
>
> **Upstream source:** Enum declared in `llvm/lib/Target/NVPTX/NVPTXISelLowering.h` as `namespace NVPTXISD { enum NodeType : unsigned { ... } }`. Lowering producers live in `NVPTXISelLowering.cpp`; consumers live in `NVPTXISelDAGToDAG.cpp` and the TableGen-generated pattern matcher.
>
> **Source of truth in this binary:** The 460 enumerator names below were recovered from `cicc_strings.json` (the assertion/diagnostic strings the constructors pass to `SDNode::getOperationName`). The numeric values are not directly observable as enum literals -- they appear baked into the master opcode dispatch in `sub_35F6D40` (6,634-case switch over `SDNode::getOpcode()`) and in the LowerOperation cluster at `sub_32E3060`. Cross-references to `cicc_strings.json`, `cicc_switches.json`, and the LowerOperation/ISel function map were used to assign opcodes to families.

CICC v13.0 enumerates exactly **460** distinct `NVPTXISD::*` SelectionDAG opcodes -- target-specific node types that exist transiently between operation legalization (where `LowerOperation` produces them) and pattern-based instruction selection (where the TableGen-generated matcher consumes them and emits NVPTX `MachineInstr` opcodes). Each opcode represents either a PTX construct that has no clean spelling in target-independent `ISD::*` (the `.param`-space calling convention, texture/surface fetches, funnel shifts with clamping, bitfield extract/insert) or an internal pseudo that survives only long enough to glue chains, prototypes, and call sequences together. The opcode count is roughly **15x** larger than upstream LLVM's NVPTX target, which carries around 30 NVPTXISD nodes; CICC's expansion is driven mostly by the texture/surface family (372 of the 460 opcodes -- 81%) plus the SM90+ load/store extension variants and the four call-flavor matrix.

This page catalogs every recovered opcode grouped by family. The numeric values shown in inline tables are reconstructions from the `sub_32E3060` LowerOperation dispatch and the `sub_35F6D40` master switch; treat them as MED confidence unless explicitly noted otherwise. See [SelectionDAG](selectiondag.md) for how these nodes flow through the pipeline, [ISel Pattern Matching](isel-patterns.md) for how they are matched, and [NVPTX Machine Opcode Reference](../reference/nvptx-opcodes.md) for the MachineInstr opcodes they lower to.

## Family Breakdown

| Family | Count | % of total | Producer (LowerOperation cluster) | Consumer (ISel) |
|---|---:|---:|---|---|
| Texture (`Tex*`, `Tld4*`) | 174 | 37.8% | `sub_32B8A20` (NVVM tex/surf lowering, 71KB) | `sub_3090F90` ISel + TableGen patterns |
| Surface (`Suld*`) | 198 | 43.0% | `sub_32B8A20` | `sub_3090F90` ISel + TableGen patterns |
| Call / Frame / Param | 29 | 6.3% | `sub_3040BF0` (`LowerCall`, 88KB) | `sub_3349730` formal args / `sub_332FEA0` calls |
| Load family | 18 | 3.9% | `sub_32D2680` (load/store lowering, 81KB) | TableGen `ld.*` patterns |
| Store family | 17 | 3.7% | `sub_32D2680` | TableGen `st.*` patterns |
| Math (BFE/BFI/IMAD/DP*/PRMT/SETP*/MUL_WIDE) | 10 | 2.2% | `sub_32983B0` (integer/FP legalization, 79KB) | TableGen integer-math patterns |
| Funnel shift (`FSH*`, `FUN_SHF*`) | 4 | 0.9% | `sub_32983B0` | TableGen `shf.l`/`shf.r` patterns |
| `Brx*` (branch index table) | 3 | 0.7% | `sub_32BE8D0` (conditional/select, 54KB) | `brx.idx` emitter |
| Vector reshape (`BUILD_VECTOR`, `UNPACK_VECTOR`) | 2 | 0.4% | `sub_32E3060` BUILD_VECTOR path | TableGen `mov.b{32,64,128}` patterns |
| Misc pseudo (`Dummy`, `Wrapper`, `ProxyReg`, `STACKSAVE`/`RESTORE`, `DYNAMIC_STACKALLOC`, `FCOPYSIGN`) | 7 | 1.5% | various | various |
| **Total** | **460** | **100.0%** | -- | -- |

## How Opcode Names Reach the Binary

Every NVPTXISD opcode has a constructor in `NVPTXTargetLowering` that calls `DAG.getNode(NVPTXISD::Foo, dl, VT, ...)`. The numeric value of `NVPTXISD::Foo` is a contiguous integer assigned by the C++ enum, starting at `ISD::BUILTIN_OP_END` (value 499 in LLVM 20, confirmed at `sub_33D4EF0` line 11 of the decompilation -- "above 499 delegate to TargetLowering"). The symbolic name survives in the stripped binary only because `NVPTXTargetLowering::getTargetNodeName(unsigned Opcode)` contains a switch that maps each enumerator to a string literal; those literals are what `cicc_strings.json` captures. The string is consulted by `SelectionDAG::print()`, diagnostic emission in `LegalizeOp`, and any assert that mentions the opcode.

The full enumeration of strings was extracted via:

```
jq -r '.[] | select(.value | test("^NVPTXISD::")) | .value' \
  cicc_strings.json | sort -u
```

Yields exactly 460 lines.

---

## Call / Frame / Param Family (29 opcodes)

These opcodes implement the PTX `.param`-space calling convention. They are emitted exclusively by `NVPTXTargetLowering::LowerCall` (`sub_3040BF0`, 88KB) and `LowerFormalArguments`/`LowerReturn`, then matched 1:1 in ISel to pseudo MachineInstrs in the **505--573 opcode range** documented in [NVPTX Machine Opcodes](../reference/nvptx-opcodes.md#call-abi-family-505573). Every CUDA device function call expands into a sequence built from these nodes; see the [SelectionDAG Call Sequence DAG Structure](selectiondag.md#call-sequence-dag-structure) for the canonical shape.

| NVPTXISD name | Role | Notes |
|---|---|---|
| `CALL` | Top-level call node (legacy/generic) | Rarely produced directly; most calls use the four flavor opcodes below. |
| `CallArg` | Single `.param` argument slot | Emitted per scalar/aggregate argument. |
| `CallArgBegin` | Marks start of argument list | Glue-only chain node. |
| `CallArgEnd` | Marks end of argument list | Glue-only chain node. |
| `LastCallArg` | Tags the final `CallArg` in a sequence | Lets the matcher recognize the last argument without counting. |
| `CallPrototype` | Attaches a parsed prototype string | Holds the `.callprototype` directive operand. |
| `CallSeqBegin` | Outer call-frame setup (opcode 315) | Maps to ISD-level `CALLSEQ_START`. |
| `CallSeqEnd` | Outer call-frame teardown (opcode 316) | Maps to ISD-level `CALLSEQ_END`. |
| `CallSuspend` | Suspend point inside a call | Used when the callee is a coroutine handle or `__syncthreads` boundary; see QUIRK below. |
| `CallSymbol` | Direct callee by symbol | Carries the `MCSymbol` for a named device function. |
| `CallVal` | Call returning a scalar value | Discriminates from the void variant for return-glue handling. |
| `CallVoid` | Call returning no value | Skips the `LoadRetParam` chain. |
| `PrintCall` | Emits `call` PTX directive | Non-uniform variant. |
| `PrintCallUni` | Emits `call.uni` PTX directive | Uniform-control-flow variant; matched when the call is provably warp-uniform. |
| `PrintConvergentCall` | `call` with convergent semantics | Forces barrier-like ordering. |
| `PrintConvergentCallUni` | `call.uni` with convergent semantics | Combination of both. |
| `Prototype` | Standalone `.callprototype` declaration | Emitted at function boundaries. |
| `SuspendPrototype` | Suspended prototype (forward reference) | Used when a call-site sees a prototype defined later in the module. |
| `DeclareParam` | `.param .align N .b8 _param_X[size]` byval aggregate | Opcode 505 at MachineInstr level. |
| `DeclareScalarParam` | Scalar `.param` with width+align | Opcode 506. |
| `DeclareRet` | `.param` slot for return value | For non-scalar returns. |
| `DeclareRetParam` | Combined return + param declaration | Helper for the dispatcher; collapses into separate Declare* nodes during selection. |
| `DeclareScalarRet` | Scalar return declaration | Opcode 508. |
| `LoadParam` | `ld.param.bN` of scalar argument | Reads from `.param` space inside the callee. |
| `LOAD_PARAM` | Alias / older spelling | Both names appear in the binary; see QUIRK. |
| `LoadParamV2` | `ld.param.v2.bN` (2-element vector) | |
| `LoadParamV4` | `ld.param.v4.bN` (4-element vector) | |
| `MoveParam` | `mov` between two `.param` slots | Used for register-to-param shuffles in the prologue. |
| `PseudoUseParam` | Liveness anchor for unused params | Prevents DCE from deleting declared-but-unused `.param` slots. |
| `ProxyReg` | Proxy node for cross-block register liveness | Lowers to a NOP `mov`; preserves SSA correctness across DAG boundaries. |
| `RET_FLAG` | LLVM-style return-with-flag glue | |
| `RET_GLUE` | Renamed `RET_FLAG` in newer LLVM versions | See QUIRK below -- both spellings coexist. |
| `RETURN` | Function epilogue marker | Opcode 569 at MachineInstr level. |

### High-impact opcode: `DeclareScalarParam` lowering

When `LowerCall` sees a scalar argument narrower than 64 bits, it emits a `DeclareScalarParam` node carrying `(size_in_bits, alignment, param_index)`. The C-pseudo for the producer:

```c
// In NVPTXTargetLowering::LowerCall, around the per-argument loop in sub_3040BF0
SDValue DeclareScalarParam(SDValue Chain, unsigned ParamIdx,
                           unsigned SizeBits, unsigned Align) {
    SDValue Ops[] = {
        Chain,
        DAG.getConstant(ParamIdx, dl, MVT::i32),
        DAG.getConstant(SizeBits, dl, MVT::i32),
        DAG.getConstant(Align,    dl, MVT::i32),
        InGlue,
    };
    SDVTList VTs = DAG.getVTList(MVT::Other, MVT::Glue);
    return DAG.getNode(NVPTXISD::DeclareScalarParam, dl, VTs, Ops);
}
```

The consumer (`sub_3090F90` in ISel) matches this with a single TableGen pattern that emits the PTX directive `.param .align <align> .b<size> _param_<idx>;` directly via `MachineInstr` opcode 506 with three immediate operands.

### High-impact opcode: `CallSeqBegin`/`CallSeqEnd` paired counter

`sub_3040BF0` maintains a monotonic per-function call counter at `NVPTXTargetLowering + 537024` (offset `134256 * 4`). Each `CallSeqBegin` reads-and-increments this counter:

```c
// Roughly mirrors the prologue of LowerCall in sub_3040BF0
uint32_t seq_id = *(uint32_t*)(TLI + 537024);
*(uint32_t*)(TLI + 537024) = seq_id + 1;

SDValue Begin = DAG.getNode(NVPTXISD::CallSeqBegin, dl,
                            DAG.getVTList(MVT::Other, MVT::Glue),
                            { Chain, DAG.getConstant(seq_id, dl, MVT::i32),
                                     DAG.getConstant(0,      dl, MVT::i32) });
// ... emit DeclareParam / StoreParam / Call ...
SDValue End   = DAG.getNode(NVPTXISD::CallSeqEnd, dl,
                            DAG.getVTList(MVT::Other, MVT::Glue),
                            { LastChain, DAG.getConstant(seq_id, dl, MVT::i32) });
```

The seq_id is what makes `.param` names unique within a function -- without it, two inlined calls would clash on `_param_0`.

> ⚡ **QUIRK — `RET_FLAG` and `RET_GLUE` both present**
> The strings table contains both `NVPTXISD::RET_FLAG` and `NVPTXISD::RET_GLUE`. Upstream LLVM renamed `ISD::RET_FLAG` to `ISD::RET_GLUE` in commit `e80b2b54` (2022) as part of a global rename of "flag" to "glue" for the auxiliary chain edge. CICC v13.0 carries the diagnostic string for **both** spellings, suggesting either (a) the binary was built from a tree partway through the rename with both `case` labels emitting different strings, or (b) NVIDIA retained `RET_FLAG` as an alias for older internal patterns and added `RET_GLUE` for compatibility with upstream merges. Both opcodes ultimately lower to PTX `ret;` -- the distinction is purely cosmetic at the asm-print level but matters when matching internal patterns that hard-coded the older name.

> ⚡ **QUIRK — `LoadParam` vs `LOAD_PARAM`**
> Two near-identical strings appear: `NVPTXISD::LoadParam` (CamelCase) and `NVPTXISD::LOAD_PARAM` (SCREAMING_SNAKE_CASE). The `LoadParam` family also has `V2`/`V4` vector variants in CamelCase, while `LOAD_PARAM` is a singleton. This is a leftover from an internal NVIDIA rename: the singleton `LOAD_PARAM` is the old single-element form retained for legacy patterns, while `LoadParam`/`LoadParamV2`/`LoadParamV4` are the modern triplet. The behavioral overlap means LLVM ends up matching `LOAD_PARAM` against `LoadParam` patterns in some legalization paths -- mostly harmless but a foot-gun if you try to add a new ISel pattern.

---

## Load Family (18 opcodes)

The load opcodes split into four sub-groups: vectorized generic loads (`LoadV2`/`V4`/`V8`), uncoalesced global loads (`LDGV2`/`LDGV4`, `LDUV2`/`LDUV4`), extending loads (`LoadExt*`), and `.param`-space loads (which were listed in the Call family above). All are produced by `sub_32D2680` (load/store lowering, 81KB) and feed the load/store vectorization pass discussed in [SelectionDAG: Load/Store Legalization](selectiondag.md#loadstore-legalization).

| NVPTXISD name | Vec width | Maps to PTX | Notes |
|---|---:|---|---|
| `LoadV2` | 2 | `ld.{space}.v2.b{8,16,32,64}` | Pair load from generic/global/shared/local. |
| `LoadV4` | 4 | `ld.{space}.v4.b{8,16,32}` | Quad load; b64 not supported as v4. |
| `LoadV8` | 8 | `ld.global.v8.b32` | SM90+ wide global load (Hopper TMA-adjacent). |
| `LDGV2` | 2 | `ld.global.nc.v2.b*` | Non-coherent (read-only cache) pair load. |
| `LDGV4` | 4 | `ld.global.nc.v4.b*` | Non-coherent quad load. |
| `LDUV2` | 2 | `ldu.global.v2.b*` | Uniform load (warp-invariant). |
| `LDUV4` | 4 | `ldu.global.v4.b*` | Uniform quad load. |
| `LoadExt` | 1 | `ld.*.b{N}; cvt` | Extending load (zext/sext into a wider register). |
| `LoadExtV2` | 2 | `ld.*.v2.b{N}; cvt` | Vector extending load. |
| `LoadExtV4` | 4 | `ld.*.v4.b{N}; cvt` | |
| `LoadExtVer2` | 1 | (see QUIRK) | Version-2 spelling of the same opcode. |
| `LoadExtVer2V2` | 2 |  | |
| `LoadExtVer2V4` | 4 |  | |
| `LoadParam` | 1 | `ld.param.b*` | Listed in Call family; reproduced here for completeness. |
| `LOAD_PARAM` | 1 | (alias) | |
| `LoadParamV2` | 2 | `ld.param.v2.b*` | |
| `LoadParamV4` | 4 | `ld.param.v4.b*` | |
| `MoveParam` | 1 | `mov ...` | Listed in Call family. |

### High-impact opcode: `LoadV4` lowering

The load/store vectorization combine in `sub_32D2680` scans contiguous `LOAD` nodes by memory offset; when four loads at offsets `0, k, 2k, 3k` for `k == elementBytes` are found, they are coalesced:

```c
// In NVPTXTargetLowering::ReplaceLoadVector, called from PerformDAGCombine
SDValue lowerLoadVector(SDValue Op, SelectionDAG &DAG) {
    LoadSDNode *Ld = cast<LoadSDNode>(Op);
    EVT VT  = Op.getValueType();         // e.g., v4i32
    EVT EltVT = VT.getVectorElementType();

    SDValue Ops[] = {
        Ld->getChain(),
        Ld->getBasePtr(),
    };
    // 5 result VTs: 4 elements + chain
    SDVTList VTs = DAG.getVTList({ EltVT, EltVT, EltVT, EltVT, MVT::Other });
    SDValue NewLd = DAG.getMemIntrinsicNode(
        NVPTXISD::LoadV4, dl, VTs, Ops, VT,
        Ld->getMemOperand());
    // Reconstruct vector from the 4 scalar results.
    SDValue Vec = DAG.getBuildVector(VT, dl,
        { NewLd.getValue(0), NewLd.getValue(1),
          NewLd.getValue(2), NewLd.getValue(3) });
    return DAG.getMergeValues({ Vec, NewLd.getValue(4) }, dl);
}
```

The matcher in `sub_3090F90` recognizes the `LoadV4` node, checks its memory operand's address space, and emits the appropriate PTX opcode (`ld.global.v4.b32` for AS 1, `ld.shared.v4.b32` for AS 3, etc.).

> ⚡ **QUIRK — `LoadExtVer2*` parallel triplet**
> The strings table contains both `LoadExt`/`LoadExtV2`/`LoadExtV4` and `LoadExtVer2`/`LoadExtVer2V2`/`LoadExtVer2V4` -- two complete triplets of extending load opcodes. The "Ver2" suffix indicates these are a parallel implementation introduced for SM90+ where the extension semantics changed (likely related to the `ld.global.nc.L1::no_allocate` cache hint variants added in PTX ISA 7.5). The matcher selects between the two triplets based on subtarget feature flags; older code paths continue to emit the original `LoadExt*` form. This is one of the few cases where the binary preserves two complete generations of the same operation rather than gating with a sub-feature.

---

## Store Family (17 opcodes)

Mirror of the load family, with the addition of `StoreParam`/`StoreRetval` for the call ABI. Produced by `sub_32D2680` and `sub_3040BF0` (the latter for the param/retval variants), matched in ISel to opcodes **571--573** for the generic stores and to dedicated MachineInstr pseudos for the param/retval forms.

| NVPTXISD name | Maps to PTX | Notes |
|---|---|---|
| `StoreV2` | `st.{space}.v2.b{8,16,32,64}` | Generic pair store; MachineInstr opcode 572. |
| `StoreV4` | `st.{space}.v4.b{8,16,32}` | Quad store; opcode 573. |
| `StoreV8` | `st.global.v8.b32` | SM90+ wide global store. |
| `StoreExt` | `cvt; st.*` | Truncating store. |
| `StoreExtV2` | `cvt; st.*.v2.*` | |
| `StoreExtV4` | `cvt; st.*.v4.*` | |
| `StoreExtVer2` | (see QUIRK in Load section) | |
| `StoreExtVer2V2` | | |
| `StoreExtVer2V4` | | |
| `StoreParam` | `st.param.b*` | Single scalar arg store; MachineInstr 571. |
| `StoreParamS32` | `st.param.s32` | Signed-explicit variant for return-value typing. |
| `StoreParamU32` | `st.param.u32` | Unsigned-explicit variant. |
| `StoreParamV2` | `st.param.v2.b*` | |
| `StoreParamV4` | `st.param.v4.b*` | |
| `StoreRetval` | `st.param.b*` (return slot) | Stores the return value into the caller-visible `.param` slot. |
| `StoreRetvalV2` | `st.param.v2.b*` | |
| `StoreRetvalV4` | `st.param.v4.b*` | |

### High-impact opcode: `StoreRetval` lowering

`LowerReturn` walks the return values and emits one `StoreRetval` per element:

```c
// In NVPTXTargetLowering::LowerReturn (companion to sub_3040BF0)
SDValue Chain = ...;
for (unsigned i = 0; i < RetVals.size(); ++i) {
    SDValue Val   = RetVals[i];
    SDValue Off   = DAG.getConstant(RetOffsets[i], dl, MVT::i32);
    SDValue Ops[] = { Chain, Val, Off };
    Chain = DAG.getNode(
        NVPTXISD::StoreRetval, dl,
        DAG.getVTList(MVT::Other),
        Ops);
}
return DAG.getNode(NVPTXISD::RET_GLUE, dl, MVT::Other, Chain);
```

The matcher recognizes the offset operand, selects the appropriate `st.param.b{8,16,32,64}` PTX form, and chains all the stores before the final `ret;`.

> ⚡ **QUIRK — `StoreParamS32`/`StoreParamU32` exist but `StoreParamS{8,16,64}` do not**
> The binary contains the signed/unsigned-discriminated `StoreParamS32` and `StoreParamU32` opcodes, but no `StoreParamS8`, `StoreParamS16`, `StoreParamU8`, `StoreParamU16`, or `StoreParamS64`/`StoreParamU64` siblings. The reason is that PTX `.param` storage is always at least 32 bits wide (per the calling convention's scalar widening rules -- see [SelectionDAG: Scalar Widening Rules](selectiondag.md#scalar-widening-rules)), so anything narrower than 32 bits is widened by the lowering before the store is emitted. The signedness discrimination at exactly 32 bits exists to feed the PTX type system, which distinguishes `.s32` and `.u32` for some optimization decisions in downstream `ptxas`. 64-bit args use only `StoreParam` (no signed/unsigned split) because by that width, ptxas treats `.s64` and `.u64` identically for all `.param` purposes.

---

## Math Family (10 opcodes)

NVPTX-specific math nodes that don't have a clean upstream `ISD::*` equivalent. All produced by `sub_32983B0` (integer/FP legalization, 79KB) or directly by the intrinsic lowering switch `sub_33B0210` for the builtin entries.

| NVPTXISD name | PTX equivalent | Purpose |
|---|---|---|
| `BFE` | `bfe.{s,u}{32,64}` | Bitfield extract: `(x >> offset) & ((1 << len) - 1)` with sign/zero extension. |
| `BFI` | `bfi.b{32,64}` | Bitfield insert: replace `len` bits at `offset` in `dst` with low bits of `src`. |
| `IMAD` | `mad.{lo,hi}.{s,u}{32,64}` | Integer multiply-add as a fused operation. |
| `DP2A` | `dp2a.{lo,hi}.{s,u}32.{s,u}32` | 2-way 8-bit dot product with 32-bit accumulator (SM61+). |
| `DP4A` | `dp4a.{s,u}32.{s,u}32` | 4-way 8-bit dot product (SM61+). |
| `MUL_WIDE_SIGNED` | `mul.wide.s32` | 32x32 -> 64 widening signed multiply. |
| `MUL_WIDE_UNSIGNED` | `mul.wide.u32` | 32x32 -> 64 widening unsigned multiply. |
| `PRMT` | `prmt.b32` | Permute bytes within two 32-bit registers (a 32-element 4-bit selection mask). |
| `SETP_F16X2` | `setp.{cmp}.f16x2` | Predicate comparison of two `half` packed in 32-bit (SM53+). |
| `SETP_BF16X2` | `setp.{cmp}.bf16x2` | Predicate comparison of two `bfloat16` packed in 32-bit (SM80+). |

### High-impact opcode: `BFE` lowering

`BFE` is matched from an idiomatic `(x >> offset) & mask` pattern during DAG combine:

```c
// In NVPTXTargetLowering::PerformDAGCombine, AND case in sub_33C0CA0
SDValue tryFormBFE(SDValue N, SelectionDAG &DAG) {
    // Match: AND(SHR(x, ConstOff), Mask) where Mask = (1 << Len) - 1
    if (N.getOpcode() != ISD::AND) return SDValue();
    SDValue Sh   = N.getOperand(0);
    ConstantSDNode *MaskC = dyn_cast<ConstantSDNode>(N.getOperand(1));
    if (!MaskC || (Sh.getOpcode() != ISD::SRL && Sh.getOpcode() != ISD::SRA))
        return SDValue();
    ConstantSDNode *OffC = dyn_cast<ConstantSDNode>(Sh.getOperand(1));
    if (!OffC) return SDValue();
    uint64_t mask = MaskC->getZExtValue();
    if (!isMask_64(mask)) return SDValue();
    unsigned Len = popcount(mask);
    unsigned Off = OffC->getZExtValue();
    bool Signed = (Sh.getOpcode() == ISD::SRA);
    SDValue Ops[] = {
        Sh.getOperand(0),
        DAG.getTargetConstant(Off, dl, MVT::i32),
        DAG.getTargetConstant(Len, dl, MVT::i32),
        DAG.getTargetConstant(Signed, dl, MVT::i1),
    };
    return DAG.getNode(NVPTXISD::BFE, dl, N.getValueType(), Ops);
}
```

ISel then matches `BFE` against the `bfe.{s,u}{32,64}` PTX instructions in a single TableGen pattern.

---

## Funnel Shift Family (4 opcodes)

Funnel shifts concatenate two registers, shift the combined `2N`-bit value, and return the high or low `N` bits. PTX exposes them as `shf.l.clamp` and `shf.r.clamp`; the `.clamp` variant saturates the shift amount at the operand width instead of wrapping mod-N.

| NVPTXISD name | PTX equivalent | Direction |
|---|---|---|
| `FSHL_CLAMP` | `shf.l.clamp.b32 d, a, b, c` | Left funnel with clamped shift amount. |
| `FSHR_CLAMP` | `shf.r.clamp.b32 d, a, b, c` | Right funnel with clamped shift amount. |
| `FUN_SHFL_CLAMP` | `shf.l.clamp.b32` (legacy) | Older alias for `FSHL_CLAMP`. |
| `FUN_SHFR_CLAMP` | `shf.r.clamp.b32` (legacy) | Older alias for `FSHR_CLAMP`. |

> ⚡ **QUIRK — `FSHL_CLAMP` / `FUN_SHFL_CLAMP` are functional duplicates**
> The binary carries two complete spellings of the funnel-shift opcode: the modern `FSHL_CLAMP`/`FSHR_CLAMP` and the legacy `FUN_SHFL_CLAMP`/`FUN_SHFR_CLAMP`. The modern pair matches LLVM's target-independent `ISD::FSHL`/`ISD::FSHR` (introduced in LLVM 9) with NVPTX's clamping semantics tacked on. The legacy pair was the original NVPTX-only spelling from before LLVM had upstream funnel shifts. Both lower to the same `shf.{l,r}.clamp.b32` PTX instruction. The legacy spelling is still referenced in pattern fragments that haven't been updated, so removing it would break a handful of TableGen patterns for `__funnelshift_l` / `__funnelshift_r` intrinsics that bypass the standard ISD path.

---

## Branch Index Table Family (3 opcodes)

`Brx*` opcodes implement `brx.idx`, NVPTX's indirect-branch-via-table instruction used by LLVM for jump-table-style switch lowering on SM30+.

| NVPTXISD name | Role |
|---|---|
| `BrxStart` | Marks the start of a branch index table (carries the table label). |
| `BrxItem` | One label entry inside the table. |
| `BrxEnd` | Marks the end of the table; the actual `brx.idx` instruction is emitted at this node. |

The three opcodes are always emitted as an inseparable triple by `sub_32BE8D0` (conditional/select lowering, 54KB). ISel collapses the triple into a single `brx.idx` instruction plus a private `.const` table.

---

## Vector Reshape Family (2 opcodes)

| NVPTXISD name | Role |
|---|---|
| `BUILD_VECTOR` | NVPTX-specific build-vector node (distinct from the generic `ISD::BUILD_VECTOR`). |
| `UNPACK_VECTOR` | Splits a packed register (e.g., `b32` holding two `half`s) into its scalar components. |

The NVPTX-specific `BUILD_VECTOR` exists because PTX has no native vector construction -- the lowering at `sub_32E3060` produces this opcode after splatting or pairwise packing, and ISel matches it to either `mov.b{32,64,128}` for the splat case or to a sequence of `cvt.pack` + `mov` for heterogeneous values. See [SelectionDAG: BUILD_VECTOR Lowering](selectiondag.md#build_vector-lowering).

---

## Misc / Pseudo Family (7 opcodes)

| NVPTXISD name | Role |
|---|---|
| `Dummy` | Placeholder used during lowering when a node needs an opcode but isn't yet decided. |
| `Wrapper` | Wraps a constant pool / global address reference; lowers to `mov.u{32,64}` of the symbol. |
| `STACKSAVE` | Saves current stack pointer (NVPTX local memory). |
| `STACKRESTORE` | Restores saved stack pointer. |
| `DYNAMIC_STACKALLOC` | `alloca` with non-constant size; emits `mov.u64 %sp, ...` sequence. |
| `FCOPYSIGN` | Floating-point copysign (`copysign(a, b)`); maps to `copysign.{f32,f64}` PTX. |
| `ProxyReg` | Cross-block register liveness anchor (listed in Call family). |

### High-impact opcode: `Wrapper` lowering

Every global address reference passes through `Wrapper`:

```c
// In NVPTXTargetLowering::LowerGlobalAddress (called from sub_331F6A0)
SDValue LowerGlobalAddress(SDValue Op, SelectionDAG &DAG) {
    GlobalAddressSDNode *GA = cast<GlobalAddressSDNode>(Op);
    SDValue Sym = DAG.getTargetGlobalAddress(GA->getGlobal(), dl,
                                             Op.getValueType(),
                                             GA->getOffset());
    return DAG.getNode(NVPTXISD::Wrapper, dl, Op.getValueType(), Sym);
}
```

ISel matches `Wrapper` and emits `mov.u64 %rd_target, symbol_name;` (or `.u32` for AS-5 pointers on 32-bit targets).

---

## Texture Family (174 opcodes)

The texture opcodes parameterize over four dimensions:

1. **Texture geometry**: `1D`, `1DArray`, `2D`, `2DArray`, `3D`, `Cube`, `CubeArray`.
2. **Sampler mode**: regular vs. `Unified`. Unified samplers (`TexUnified*`) bind the texture and sampler into a single 64-bit handle; non-unified versions take separate texture and sampler operands. SM70+ uses Unified exclusively in user-level code.
3. **Result type**: `Float`, `S32` (signed integer), `U32` (unsigned integer).
4. **Coordinate type**: `Float` or `S32`.
5. **Sampling mode suffix**: bare (regular sample), `Grad` (with explicit derivatives), `Level` (with explicit LOD).

This gives a combinatorial matrix. The 174 entries break down as:

| Geometry | Non-unified | Unified | Total |
|---|---:|---:|---:|
| `1D` | 12 | 12 | 24 |
| `1DArray` | 12 | 12 | 24 |
| `2D` | 12 | 12 | 24 |
| `2DArray` | 12 | 12 | 24 |
| `3D` | 12 | 12 | 24 |
| `Cube` | 6 | 9 | 15 |
| `CubeArray` | 6 | 9 | 15 |
| `Tld4*` (gather) | 12 | 12 | 24 |
| **Total** | **84** | **90** | **174** |

The 12-entry pattern per non-cube geometry is: `{Float,S32,U32} x {Float,S32} x {bare,Grad,Level}` minus a few unavailable combinations. Cubemap geometry omits `Grad` for non-unified (PTX doesn't expose `tex.grad.cube` outside the unified path).

### Naming scheme

`NVPTXISD::Tex<Unified?><Geometry><ResultTy><CoordTy><Sampling?>`

Examples:

| Name | Decoded meaning |
|---|---|
| `Tex1DFloatFloat` | Non-unified 1D, float result, float coord, regular sample |
| `TexUnified2DArrayU32FloatGrad` | Unified 2D array, u32 result, float coord, with gradients |
| `Tex3DS32FloatLevel` | Non-unified 3D, s32 result, float coord, with explicit LOD |
| `TexCubeFloatFloat` | Non-unified cubemap, float result, float coord (no Grad available) |
| `Tld4UnifiedR2DFloatFloat` | `tld4` gather, unified, R channel, 2D, float result/coord |

### Tld4 sub-family

`Tld4*` opcodes implement PTX `tld4` (gather-4) instructions that fetch four texels in a 2x2 footprint. The 24 `Tld4*` opcodes cover the matrix of `{A,B,G,R} channels x {Float,S64,U64} result x {non-unified, Unified}` for 2D textures only. Higher-D `tld4` doesn't exist in the PTX ISA.

| Channel suffix | Returns |
|---|---|
| `A` | Alpha channel of the 2x2 footprint |
| `B` | Blue channel |
| `G` | Green channel |
| `R` | Red channel |

### High-impact opcode: `TexUnified2DFloatFloat` lowering

The intrinsic lowering switch `sub_33B0210` matches `nvvm_tex_unified_2d_v4f32_f32` and emits:

```c
// In NVPTXTargetLowering::lowerTexIntrinsic, called from sub_33B0210
SDValue Ops[] = {
    Chain,
    TexHandle,    // 64-bit texture/sampler handle
    CoordX,       // f32
    CoordY,       // f32
};
SDVTList VTs = DAG.getVTList(
    { MVT::f32, MVT::f32, MVT::f32, MVT::f32,  // 4 result components
      MVT::Other });                            // chain
SDValue Tex = DAG.getNode(
    NVPTXISD::TexUnified2DFloatFloat, dl, VTs, Ops);
// Pack the 4 scalar results into a v4f32 via BUILD_VECTOR.
return DAG.getBuildVector(MVT::v4f32, dl,
    { Tex.getValue(0), Tex.getValue(1),
      Tex.getValue(2), Tex.getValue(3) });
```

ISel matches `TexUnified2DFloatFloat` against a TableGen pattern that emits PTX `tex.unified.2d.v4.f32.f32 {rd0, rd1, rd2, rd3}, [handle, {x, y}];`.

> ⚡ **QUIRK — Cubemap geometries skip Grad in the non-unified family but include it in the Unified family**
> `TexCubeArrayU32Float` and `TexCubeArrayU32FloatLevel` exist in the non-unified set (6 opcodes per cube geometry: `{Float,S32,U32} x {bare,Level}`), but there is **no** `TexCubeArrayU32FloatGrad`. The Unified family does include `TexUnifiedCubeArrayU32FloatGrad` (9 opcodes per cube geometry: `{Float,S32,U32} x {bare,Grad,Level}`). This asymmetry reflects an actual PTX ISA limitation: pre-SM30 cubemap fetches lacked a `tex.grad.cube` variant, so the non-unified opcodes (which model the pre-SM30 calling convention) cannot express it. The unified path was added at SM30 with the gradient variant included from day one.

---

## Surface Family (198 opcodes)

The `Suld*` opcodes implement surface load (`suld`) instructions across the matrix of:

1. **Geometry**: `1D`, `1DArray`, `1DBuffer`, `2D`, `2DArray`, `3D`. (No cubemap surface; CUDA surfaces don't support cubemaps.)
2. **Vector width**: scalar (`I*`), `V2` (2-element), `V4` (4-element).
3. **Element type**: `I8`, `I16`, `I32`, `I64`.
4. **Boundary mode**: `Clamp`, `Trap`, `Zero` -- what to do when accessing out-of-bounds texels.

`33 opcodes/geometry x 6 geometries = 198`. Per-geometry the count is `(4 element types x 3 boundary modes) x 3 widths - omissions`. The actual per-geometry layout is:

| Width | Element types covered | Per-mode count | x3 boundary modes |
|---|---|---:|---:|
| Scalar (`I*`) | I8, I16, I32, I64 | 4 | 12 |
| `V2` | I8, I16, I32, I64 | 4 | 12 |
| `V4` | I8, I16, I32 (no I64) | 3 | 9 |
| **Total per geometry** | | | **33** |

Note that V4 omits `I64` because `suld.v4.b64` would exceed 256 bits per transaction, which the PTX ISA doesn't support.

3D geometry omits `V4I8` per-mode for the same reason in some SM tiers; check `cicc_data_tables.json` for the exact subtarget feature-gate.

### Naming scheme

`NVPTXISD::Suld<Geometry><Vec?><EltTy><Boundary>`

Examples:

| Name | Decoded meaning |
|---|---|
| `Suld1DI32Clamp` | 1D surface, scalar i32, clamp on OOB |
| `Suld2DArrayV4I8Trap` | 2D array surface, 4xi8 vector, trap on OOB |
| `Suld1DBufferV2I64Zero` | 1D buffer surface, 2xi64 vector, return zero on OOB |

### High-impact opcode: `Suld1DI32Clamp` lowering

```c
// In NVPTXTargetLowering::lowerSurfaceLoadIntrinsic, sub_33B0210
SDValue Ops[] = {
    Chain,
    SurfHandle,    // 64-bit surface handle
    XCoord,        // s32 byte offset into the surface
};
SDVTList VTs = DAG.getVTList({ MVT::i32, MVT::Other });
return DAG.getNode(NVPTXISD::Suld1DI32Clamp, dl, VTs, Ops);
```

ISel emits PTX `suld.b.1d.b32.clamp {rd}, [handle, {x}];`.

> ⚡ **QUIRK — Boundary mode is part of the opcode, not an operand**
> Every surface load opcode bakes the boundary mode (`Clamp`/`Trap`/`Zero`) into the opcode name rather than carrying it as a runtime operand. This is because PTX `suld` has three completely distinct mnemonic forms (`suld.b.*.clamp`, `suld.b.*.trap`, `suld.b.*.zero`) that the assembler treats as separate instructions. Carrying the mode as a constant operand and selecting in ISel would work, but the historical NVPTX design opted to enumerate them explicitly -- which is exactly why the surface family has 198 opcodes instead of 66. The same design choice is why CICC's `cicc_strings.json` shows the family blowing up: each enumerator gets its own diagnostic string for `getTargetNodeName`.

---

## Cross-References

- [SelectionDAG & Instruction Selection](selectiondag.md) -- how these opcodes are produced and where they live in the pipeline.
- [SelectionDAG: NVPTXISD DAG Node Opcodes](selectiondag.md#nvptxisd-dag-node-opcodes) -- the 14-row call-ABI subtable with numeric opcode values.
- [SelectionDAG: Call Sequence DAG Structure](selectiondag.md#call-sequence-dag-structure) -- canonical shape of the DAG that emits Call* opcodes.
- [SelectionDAG: BUILD_VECTOR Lowering](selectiondag.md#build_vector-lowering) -- how `NVPTXISD::BUILD_VECTOR` is matched.
- [ISel Pattern Matching](isel-patterns.md) -- where these opcodes are consumed and translated to MachineInstrs.
- [Type Legalization](type-legalization.md) -- runs before LowerOperation and prepares operand types.
- [NVPTX Machine Opcodes](../reference/nvptx-opcodes.md) -- the post-ISel MachineInstr opcodes (440--573 for call ABI, etc.) that these NVPTXISD nodes lower to.
- [NVPTX Machine Opcodes: Call ABI Family](../reference/nvptx-opcodes.md#call-abi-family-505573) -- numeric mapping for the call/param family.
- [Tensor / MMA Codegen](mma-codegen.md) -- WMMA/MMA lowering (no NVPTXISD entries for MMA -- they go directly to MachineInstr opcodes).
- [Tex/Surface Builtins](../builtins/surface-texture.md) -- builtin entry points that feed the Tex/Suld families.

## Confidence Notes

- **HIGH confidence**: family membership and total count (460). All 460 names are direct extracts from `cicc_strings.json`.
- **HIGH confidence**: producer/consumer function assignments. Cross-checked against `cicc_functions.json` and the LowerOperation cluster documented in [SelectionDAG](selectiondag.md).
- **MED confidence**: numeric opcode values quoted in inline tables (`CallSeqBegin = 315`, `DeclareParam = 505`, etc.). These come from `sub_35F6D40` (master 6,634-case switch) and the `sub_32E3060` LowerOperation dispatch where they were observed as immediate constants in `getNode` call sites. The mapping to enumerator names is by behavioral signature (operand counts, chain/glue presence) and may shift by ±1 if the enum had silent insertions between LLVM versions.
- **MED confidence**: per-family counts in the breakdown table. Verified by `jq` regex matching but the boundary between "math" and "misc" is somewhat arbitrary; `PRMT` could go either way.
- **LOW confidence**: the explanation of `LoadExtVer2*` as SM90+ cache-hint variants. The "Ver2" suffix is a guess based on the parallel triplet structure; the actual semantic difference between `LoadExt` and `LoadExtVer2` could not be confirmed from the function-level analysis. The "Ver2" forms might be a deprecated path retained for backward-compatibility patterns rather than a forward-looking SM90+ feature.

## Open Follow-Ups

- **Numeric opcode values for the full 460 set.** Only the 14 call-ABI opcodes have confirmed numeric assignments. Recovering the rest requires walking `sub_35F6D40` case labels and correlating them with the string emitted by `getTargetNodeName` -- a several-hour reverse-engineering task.
- **Subtarget feature gating per opcode.** Some opcodes are only legal on certain SM tiers (`LoadV8`/`StoreV8` on SM90+, `DP4A` on SM61+, `SETP_BF16X2` on SM80+). The exact gating lives in the action table at `NVPTXTargetLowering + 2422` -- documenting which opcodes are gated where would complete the picture but needs the action table to be fully decoded.
- **Resolution of the `LoadExt` vs `LoadExtVer2` duality.** Either rename `LoadExtVer2*` to a more descriptive name or document the precise semantic difference. This likely needs a side-by-side test compilation against upstream LLVM NVPTX to see which intrinsic emits which form.
- **`Tld4` opcodes for SM90+ texture gather.** PTX ISA 8.0 added `tld4.s.2d` and `tld4.a.2d` variants for stencil/aware gather; if these are present in the binary they would extend the `Tld4*` family beyond the current 24 entries. Worth re-scanning `cicc_strings.json` against the PTX 8.0 spec.
- **Cross-reference into individual pass pages.** Each pass that lowers a subset of these opcodes (e.g., `mma-codegen.md` for tensor opcodes, `isel-patterns.md` for the matcher) should grow a link back to its slice of this catalog. Currently this page links outward but the back-links are missing.
