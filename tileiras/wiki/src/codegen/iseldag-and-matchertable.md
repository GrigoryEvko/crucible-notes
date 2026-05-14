# ISelDAG and MatcherTable

## Abstract

NVIDIA's NVPTX `SelectionDAG` instruction selector turns legalized DAG nodes into NVPTX machine nodes and eventually into PTX instructions. The shape is mostly the familiar LLVM TableGen selector, with CUDA 13.1 additions for Blackwell tensor memory, tcgen05 MMA, block-scaled MMA, TMA, WGMMA, vector atomics, packed narrow-float conversion, and NVIDIA-specific validation.

For reimplementation, the C++ selector skeleton is not the main contract. The contract is the combination of:

- Intrinsic dispatch tables.
- Target feature gates.
- MatcherTable predicates and costs.
- NVPTX-specific DAG opcodes.
- Validator diagnostics for unsupported SM/PTX combinations.
- AsmWriter mnemonic and register-name tables.

## Selector Layers

The selector has three major layers:

| Layer | Role |
|---|---|
| Intrinsic-with-chain selector | Handles NVVM intrinsics that carry memory or control-flow chain effects. |
| Vector load/store selector | Handles vectorized memory operations, tensor-memory loads/stores, and packed lane patterns. |
| TableGen MatcherTable | Handles ordinary generated patterns, complex predicates, and recursive pattern scoring. |

The fast selectors try highly structured NVIDIA-specific cases first. When a case returns "not selected", the ordinary MatcherTable path gets a shot at the node. The asymmetry is important: unsupported or unrecognized cases fall back rather than hard-fail, unless the target explicitly diagnoses the operation.

```c
bool select_node(SDNode *node, SelectorState *st) {
    if (node->is_intrinsic_with_chain) {
        if (select_intrinsic_with_chain(node, st))
            return true;
    }

    if (node->is_vector_load_store) {
        if (select_vector_load_store(node, st))
            return true;
    }

    return match_tablegen_pattern(node, st);
}
```

## Intrinsic Dispatch

The intrinsic-with-chain selector is a dense switch over NVIDIA and upstream NVVM intrinsic IDs. Most case slots fall through to the MatcherTable on purpose; only cases with custom legality or custom machine-node construction carry bodies.

Important custom families include:

| Family | Behavior |
|---|---|
| `cvt_packfloat` | Validates FP8/FP6/FP4/UE8M0 format combinations and target support before emitting pack nodes. |
| `tcgen05.mma` | Emits Blackwell tensor-memory MMA nodes, gated by datacenter Blackwell target variants. |
| `nvvm.red` | Validates address space, type, noftz mode, and cache-hint legality. |
| `cp.async` and TMA bulk tensor ops | Constructs chain-aware async-copy and descriptor nodes. |
| WGMMA and MMA sync | Emits Hopper/Blackwell matrix instructions with architecture-specific feature checks. |
| Block-scaled MMA | Provides the consumer-Blackwell substitute path where tensor memory is not available. |
| FMA with FTZ override | Selects an FTZ or non-FTZ node based on a per-call `unsafe-fp-math` attribute. |

The selector should be modeled as a validator plus emitter:

```c
bool select_intrinsic_with_chain(SDNode *node, SelectorState *st) {
    IntrinsicID id = node->intrinsic_id;

    switch (classify_intrinsic(id)) {
    case INTR_CVT_PACKFLOAT:
        validate_packfloat(node, st->subtarget);
        emit_packfloat(node, st);
        return true;

    case INTR_TCGEN05_MMA:
        validate_tcgen05_target(node, st->subtarget);
        emit_tcgen05_mma(node, st);
        return true;

    case INTR_NVVM_RED:
        validate_nvvm_red(node, st->subtarget);
        emit_nvvm_red(node, st);
        return true;

    case INTR_TMA_OR_CP_ASYNC:
        emit_async_copy_or_tma(node, st);
        return true;

    case INTR_FMA:
        emit_fma_with_ftz_policy(node, st);
        return true;

    default:
        return false;
    }
}
```

## INTRINSIC_W_CHAIN Top-Level Dispatcher

In tileiras, `select_intrinsic_with_chain` materializes as `sub_1A854E0` (`NVPTXDAGToDAGISel::SelectIntrinsic_W_Chain`) — 6 213 B, 509 basic blocks, with a single jump table at instruction `0x1A8551B` driving the body. The dispatch key is not the LLVM intrinsic ID itself. It is the 32-bit overlay at `SDNode + 24`, packing the `NVPTXISD` opcode into the low 16 bits and the SDNode flag word into the high 16 bits. Intrinsic IDs enter only inside delegate handlers, which read `SDNode + 72`.

The switch declares 345 case slots across the dense range `[0x17, 0x172]`. Of those, 58 carry distinct per-class bodies; the other 287 share a single fallthrough target at `0x4135C4`, which returns zero so the surrounding `trySelectNode` (`sub_1AAD9D0`) can hand the node to the MatcherTable. The fallthrough is not an error path. It is the deliberate join that says "this `NVPTXISD` opcode is either handled by the generic TableGen patterns or reserved for an upstream LLVM opcode NVIDIA never customized in this selector." A reimplementation that treats fallthrough as a bug would over-diagnose 287 perfectly legal nodes.

Two cases short-circuit the search entirely. `0x17` and `0x18` correspond to the upstream `ISD::INTRINSIC_VOID` and `ISD::INTRINSIC_WO_CHAIN` opcodes, which a well-formed DAG should never route through the `_W_CHAIN` selector. Both join into the same body at `0x1A85817`, which simply returns zero. Return zero differs from fallthrough conceptually: it signals a routing error rather than a deferred decision. The calling convention forces the same outward behavior either way, and the MatcherTable gets a second chance to recognize the node before any diagnostic fires.

```c
unsigned __int64 select_intrinsic_w_chain(SelectorState *st, SDNode *node,
                                          ChainWrap *cw, SelectionDAG **dag,
                                          MachineFunction *mf,
                                          SDValue chain_in, SDValue glue) {
    uint32_t key = *(const uint32_t *)((const uint8_t *)node + 24);
    uint16_t isd_opcode = (uint16_t)key;

    switch (isd_opcode) {
    case 0x17: case 0x18:
        return 0;                                       /* routing error */

    case 0x2F:                                          /* cvt_packfloat */
        return select_cvt_packfloat(st, node, cw, dag, mf, chain_in);

    case 0x30:                                          /* tcgen05 + nvvm.red */
        return select_intrinsic_wo_chain_dispatch(st, node, cw, dag, mf, chain_in);

    case 0x31:                                          /* tcgen05.mma   -> opcode 0x211 */
        return select_tcgen05_mma_fastpath(st, node, cw, dag);

    case 0x32:                                          /* tcgen05.mma.ws -> opcode 0x212 */
        return select_tcgen05_mma_ws_fastpath(st, node, cw, dag);

    case 0x66:                                          /* FMA with FTZ probe */
        return select_fma_with_unsafe_fp_math_probe(st, node, cw, dag, mf, chain_in);

    /* ... 53 more cases ... */

    default:
        return 0;                                       /* fall through to MatcherTable */
    }
}
```

The body classes group into six dispatch families. Thirty-two cases delegate into a per-class NV emitter from the `sub_1A6x` pool — the cp.async, mbarrier, TMA, WGMMA, WMMA, and tcgen05 fast paths. Ten assemble SDNodes inline through the shared builder set `sub_2005A50` / `sub_2009D80` / `sub_2009DB0` / `sub_2004920` / `sub_200ABE0` / `sub_200B040`. Eight re-delegate into a smaller secondary dispatcher: `SelectIntrinsic_WO_Chain` (`sub_1A833C0`, a 21-case inner switch), the cvt_packfloat fan-out (`sub_1A85120`, six intrinsic IDs), the tcgen05.mma fan-out (`sub_1A80E40`, fourteen IDs), or the nvvm.red emitters (`sub_1A79EA0` and `sub_1A79DE0`). Two cases return zero unconditionally; two are pass-through identities.

## Per-Case Dispatch Table

The 58 non-default bodies cluster into a small set of structurally related families. The table below lists each named case in dispatch order. First column: the `NVPTXISD` opcode in hex. Second: the family the case belongs to. Third: the delegate that owns the actual emission, with sub_ADDR notation when the binary contains a dedicated function and "inline" when the body emits SDNodes directly. Fourth: the NVIDIA-specific delta against the upstream LLVM `SelectionDAGISel` template.

| Case | Family | Delegate | NVIDIA delta vs upstream |
|---:|---|---|---|
| `0x17` | unsupported | inline `return 0` | Upstream `ISD::INTRINSIC_VOID`; routing-error guard. |
| `0x18` | unsupported | inline `return 0` | Upstream `ISD::INTRINSIC_WO_CHAIN`; routing-error guard. |
| `0x2F` | cvt.packfloat fan-out | `sub_1A85120` | Six-way intrinsic-ID dispatch over FP8/FP6/FP4/UE8M0 conversion; not in upstream LLVM 18. |
| `0x30` | tcgen05 + nvvm.red | `sub_1A833C0` | Re-enters the 21-case `SelectIntrinsic_WO_Chain` dispatcher; carries Blackwell tensor-memory IDs. |
| `0x31` | tcgen05.mma dense | `sub_1A79180` | Bypasses ID dispatch and emits MI opcode `0x211` directly from a custom `NVPTXISD` opcode created during DAG legalization. |
| `0x32` | tcgen05.mma.ws | `sub_1A70690` | Warp-specialized variant, emits MI opcode `0x212`. |
| `0x37` | nvvm.red dense | `sub_1A79EA0` | Atomic reduction with NV-only `noftz`/scope/cache-hint validators. |
| `0x38` | nvvm.red noftz | `sub_1A79DE0` | f32/f64 reduction path with FTZ bit cleared; carries the `"noftz not support for other types for nvvm.red"` diagnostic. |
| `0x3A`-`0x3C` | vector legalisation | inline at `0x1A85520` | Joined body for `ld.vector.v{2,4}` chain variants; emits MI opcode `0x9E` wrapping per-lane `0xA0` extracts. |
| `0x3F`-`0x40` | vector legalisation | inline at `0x1A85520` | Same body, used for `st.vector.v{2,4}` chain variants. |
| `0x62`-`0x63` | f16/bf16 FMA | `sub_1A6DEE0` | f16x2 fused-multiply-add; emits MI opcode `0x65`. |
| `0x64` | f16/bf16 FMA | `sub_1A6DEE0` | bf16x2 fused-multiply-add, sm_80+. |
| `0x66` | FMAD with FTZ probe | inline | Reads `"unsafe-fp-math"` Function attribute via `sub_3FC6800` and picks between FTZ opcode `0x65` and non-FTZ wrapper opcode `0xF7`; per-call override not present in upstream. |
| `0x9A` | AS-marked load | `sub_1A6D350` | `ld.global` with address-space tag wrap. |
| `0x9E` | AS-marked load | `sub_1A6A910` | `ld.param` dispatcher (addrspace 101). |
| `0x9F` | AS-marked load | `sub_1A6C600` | `ld.const` dispatcher (addrspace 4). |
| `0xA0` | AS-marked load | `sub_1A6BF90` | `ld.global.nc` non-coherent load. |
| `0xA1` | cp.async commit/wait | `sub_1A6A3C0` | `cp.async.commit_group` / `cp.async.wait_group` wrap. |
| `0xA3` | pass-through | inline `return a2` | Already in canonical form; selector returns the incoming SDNode unchanged. |
| `0xA7` | AS-marked load cached | `sub_1A6C7F0` | `ld.param` with cache-modifier suffix. |
| `0xB6`-`0xB9` | vector legalisation | inline at `0x1A85520` | `BUILD_VECTOR` v2/v4 f16/bf16/i16/i8 variants. |
| `0xBF`-`0xC0` | vector legalisation | inline at `0x1A85520` | `BUILD_VECTOR` v8 chain variants. |
| `0xC3`-`0xC4` | wmma load dense | `sub_1A5F730` | Emits MI opcode 197 / 198 (`WMMA_LOAD_DENSE`, dense transposed). |
| `0xC5`-`0xC6` | wmma load sparse | `sub_1A5F730` | Same emitter, sparse-descriptor variant; sm_80+. |
| `0xC9`-`0xCA` | wmma store | inline | Wraps inner opcode `0xC9`/`0xCA` with MI opcode `0xD8` (`STORE_VECTOR_V2_MemRef`, 16-byte alignment). |
| `0xCF` | mma.sync / mma.sp | inline | Two paths: register-form (`0xBC`/`0xBD` load/store + 207/208 multiply-add) or `ADDRESSOF`-wrapped form (`0xDA` wrapper). |
| `0xD4` | cp.async.mbarrier | `sub_1A6CEF0` | `cp.async.mbarrier.arrive{.noinc}.shared.b64`. |
| `0xD5`-`0xD6` | cp.async shared.global | `sub_1A6CA70` | `cp.async.{ca,cg}.shared.global`; sm_80+. |
| `0xDE`-`0xDF` | TMA 1-5D | `sub_1A6E110` | `cp.async.bulk.tensor.{1..5}d.global.shared::cta`; intrinsic IDs 8941/8946. |
| `0xE4`-`0xE5` | TMA reduce | `sub_1A6E200` | 8-arm reduce family (intrinsic IDs 8974-9011). |
| `0xE8` | TMA shared::cluster | `sub_1A6E2F0` | `cp.async.bulk.tensor.Nd.shared::cluster.global.mbarrier` (ID 8951). |
| `0xEB` | TMA shared::cta | `sub_1A6E6C0` | `cp.async.bulk.tensor.Nd.shared::cta.global` (ID 8956). |
| `0xEC` | mbarrier family | `sub_1A6A6E0` | `mbarrier.{init,inval,arrive,arrive.noComplete}` inner dispatch. |
| `0xED` | st.bulk | `sub_1A6ED90` | `st.bulk.weak.shared::cta` (IDs `0x23A4`-`0x23AA`); sm_100. |
| `0x112` | TMA descriptor load | inline 2-way | Branches on MVT: `MVT::i32 (12)` -> `sub_1A6D560`; `MVT::i64 (13)` -> `sub_1A6DA40`; anything else falls through to `BUG`. |
| `0x12C` | wgmma dense | `sub_1A6FB40` | `wgmma.mma_async.sync.aligned.mNnNkN.type.layout` (ID `0x226A`); sm_90a. |
| `0x12D` | wgmma block variant | `sub_1A705A0` | 10-operand block variant (ID `0x245C`). |
| `0x12E` | wgmma control | `sub_1A69A70` | `wgmma.fence` / `commit_group` / `wait_group` (IDs `0x225D`-`0x225F`). |
| `0x131` | cluster control | `sub_1A6EB10` | `clusterlaunchcontrol.*` and `griddepcontrol.*` family. |
| `0x13B` | mbarrier try_wait | `sub_1A6A0F0` | `mbarrier.try_wait{.parity,.timelimit}.shared.b64`. |
| `0x13C` | cluster sync | `sub_1A69EE0` | `barrier.cluster.{arrive,arrive.relaxed,wait}`. |
| `0x13F` | prefetch.tensormap | `sub_1A6EF30` | TMA descriptor prefetch (`prefetch.tensormap`). |
| `0x142` | mma.block_scale | `sub_1A78E20` | `mma.block_scale.sync.aligned.mNnNkN` (ID `0x24B6`); sm_100a, consumer-Blackwell substitute for tensor-memory MMA. |
| `0x16F` | BUILD_VECTOR remap | inline 195-line body | Remaps source MVT to output MI opcode: `v4f32`/`f16x2`/`bf16x2` -> opcode 561; `v8f32` -> opcode 544; any other lane class falls through to `BUG`. |

The remaining 287 slots are the holes between named bodies. Their dense ranges collapse into a small set of intervals: `0x19`-`0x2E`, `0x33`-`0x36`, `0x39`, `0x3D`-`0x3E`, `0x41`-`0x61`, `0x65`, `0x67`-`0x99`, `0x9B`-`0x9D`, `0xA2`, `0xA4`-`0xA6`, `0xA8`-`0xB5`, `0xBA`-`0xBE`, `0xC1`-`0xC2`, `0xC7`-`0xC8`, `0xCB`-`0xCE`, `0xD0`-`0xD3`, `0xD7`-`0xDD`, `0xE0`-`0xE3`, `0xE6`-`0xE7`, `0xE9`-`0xEA`, `0xEE`-`0xEF`, `0xF0`-`0x111`, `0x113`-`0x12B`, `0x12F`-`0x130`, `0x132`-`0x13A`, `0x13D`-`0x13E`, `0x140`-`0x141`, and `0x143`-`0x16E`. The largest contiguous run (`0xF0`-`0x111`, 34 slots) covers the shfl, vote, match, redux, st, and atom families the MatcherTable handles via TableGen patterns; the second-largest (`0x143`-`0x16E`, 44 slots) is the post-`mma.block_scale` Blackwell opcode window.

## Delegate Map and Secondary Dispatchers

Three named cases fan out further before any opcode is emitted, and each secondary dispatcher carries its own dispatch key.

The first is the cvt.packfloat fan-out at `sub_1A85120`. It reads the LLVM intrinsic ID from `SDNode + 24` (a different field within the same word, distinct from the outer `NVPTXISD` opcode) and routes among six destinations. Intrinsics 8294 and 9123 are identity passes that return `*(_QWORD *)(SDNode + 40)` unchanged. IDs 8437-8440 form a four-arm block that emits MI opcode `0xEC` through `sub_200ABE0` followed by `0xA0`-style multiply-add wrap. ID 8627 delegates into `sub_1A84900`, the cvt_packfloat validator that emits `"cvt_packfloat intrinsic needs atleast SM90 and PTX >= 78"`. IDs 9531-9537 emit MI opcode `0x20C` (`cvt.rn.satfinite.*x2.f32`) through `sub_2005A50`, keyed by a seven-entry per-ID opcode table at `dword_4D0DE60`.

The second is `SelectIntrinsic_WO_Chain` at `sub_1A833C0`, a 5 435 B 21-case dispatcher reached from case `0x30`. The IDs it routes include 8376 (`tcgen05.alloc`), 8381 (`tcgen05.dealloc`), 9132 (128-bit atomic via `sub_1A80A40`), 9136 (`tcgen05.cp`), 9149 (`tcgen05.ld`), 9150 (`tcgen05.st`), 9399 (`tcgen05.wait`), the 9669-9671 commit triple, 9779 / 9811 (sparse texture), 9848 / 9853 (nvvm.red), 9856 (`tcgen05.mma` emitting MI opcode `0x211` through `sub_2015B50(..., 530, ...)`), 9857 (`tcgen05.mma.ws`), and the 10521-10530 tcgen05.mma block. The latter group is itself fan-routed by `sub_1A80E40`, the 230-basic-block `SelectTcgen05Mma` super-dispatcher that handles fourteen distinct intrinsic IDs.

The third is the cvt_packfloat-and-tcgen05 architecture gate at `sub_1A84900`, shared between the SM and PTX-version probes for case `0x2F`'s sm_90+ requirement, FP6/FP4 arch-conditional checks, and the UE8M0 path. Its diagnostic strings are part of the binary-test contract: paraphrase them in a reimplementation and NVIDIA's regression suite breaks.

The architecturally important consequence of this layering: the outer 345-case switch carries only 58 distinct bodies, secondary dispatchers add roughly 50 more intrinsic-ID-keyed arms, and the MatcherTable contributes the remaining ~200 NVVM IDs as TableGen patterns. The intrinsic-ID space dispatched by `sub_1A854E0` and its delegates spans `[8294, 10995]` and contains contiguous runs that mirror NVIDIA's PTX feature blocks: tcgen05 commits at 9669-9671, the tcgen05.mma block at 10521-10530, Hopper WGMMA singletons at `0x225D`-`0x225F`, `0x226A`, and `0x245C`, the cp.async.bulk.tensor block at 8919-8956 with the 8974-9011 reduce extension, and the FP8/FP6/FP4 conversion block at 8305-8308.

## Intrinsic-ID Range Map

All NVPTX selector paths (`SelectIntrinsic_W_Chain`, `SelectIntrinsic_WO_Chain`, the `cvt_packfloat` fan-out, the `tcgen05.mma` fan-out, and the MatcherTable cost scorer) dispatch on one 32-bit intrinsic ID stored at `*(uint32_t *)(SDNode + 72)`. The map below consolidates which ID range belongs to which family, which `sub_ADDR` delegate handles it, which PTX op family it emits, and which SM-and-PTX target gate guards the emission. It folds together the per-case dispatch table above and the secondary-dispatcher fan-outs so a reimplementation can look up a single intrinsic in one place rather than walking three nested switch statements.

| ID range | Family | Selector | PTX family | SM gate |
|---|---|---|---|---|
| 8294 | cvt_packfloat (FP6) | `sub_1A85120` | `cvt.rn.satfinite.fp6x2.f32` | sm_90 + PTX>=78 |
| 8305-8308 | FP8/FP6/FP4 conversion entry | inline | `cvt.{e4m3,e5m2,...}.fp32` | sm_89 |
| 8376 | tcgen05.alloc.shared | `sub_1A80E40` arm 0 | `tcgen05.alloc.shared::cta.b32` | sm_100 + tmem |
| 8381 | tcgen05.dealloc.shared | `sub_1A80E40` arm 1 | `tcgen05.dealloc.shared::cta` | sm_100 + tmem |
| 8422 | imma.stc | inline | `mma.sp.sync.aligned.m8n8k16` | sm_80 |
| 8437-8440 | cvt_packfloat (UE8M0x2) | `sub_1A85120` | `cvt.ue8m0x2.{fp8,fp16}` | sm_100a |
| 8481-8503 | cp.async.bulk.tensor G2S | inline | `cp.async.bulk.tensor.{shared, global}::cluster` | sm_90 |
| 8519-8582 | wmma | inline | `wmma.{load, store, mma.sync.aligned}` | sm_70+ |
| 8592-8596 | TMA store | inline | `cp.async.bulk.tensor.global.shared::cta` | sm_90 |
| 8627 | cvt_packfloat (FP4x2) | `sub_1A85120` | `cvt.fp4x2.{fp16,fp8}` | sm_100a |
| 8919-8956 | cp.async.bulk.tensor block | inline | `cp.async.bulk.tensor.*` | sm_90 |
| 8974-9011 | cp.async.bulk.tensor reduce | inline | `cp.async.bulk.tensor.reduce.*` | sm_90 |
| 9045 / 9059 / 9069 | mma type-A | inline | `mma.sync.aligned.{f16,f32}` | sm_80 |
| 9098 / 9106 / 9114 / 9122 | imma.stc | inline | `mma.sp.sync.aligned.{i8,i4}` | sm_80 |
| 9123 | cvt_packfloat (E8M0) | `sub_1A85120` | `cvt.{e8m0,bf16}` | sm_100a |
| 9132 | tcgen05 128-bit atom | `sub_1A80E40` arm 2 | `tcgen05.atom.b128` | sm_100 + tmem |
| 9136 | tcgen05.cp.shared | `sub_1A80E40` arm 3 | `tcgen05.cp.shared::cta` | sm_100 + tmem |
| 9149 / 9150 | tcgen05.ld / tcgen05.st | `sub_1A80E40` arms 4/5 | `tcgen05.{ld,st}` | sm_100 + tmem |
| 9153-9170 | ldmatrix | inline | `ldmatrix.sync.aligned.*` | sm_75 |
| 9271 / 9272 | shape-class | inline | (selector marker) | - |
| 9308 / 9398 | SM120 block-scaled | inline | `mma.block_scale.sync.aligned` | sm_120 |
| 9399 | tcgen05.wait | `sub_1A80E40` arm 6 | `tcgen05.wait::cta` | sm_100 + tmem |
| 9531-9537 | cvt_packfloat (E4M3) | `sub_1A85120` | `cvt.fp16x2.e4m3x2.*` | sm_100 |
| 9669-9671 | tcgen05.commit / arrive | `sub_1A80E40` arms 7-9 | `tcgen05.{commit,arrive}` | sm_100 + tmem |
| 9779 / 9811 | tcgen05 sparse texture | `sub_1A80E40` (bit-test `_bittest64(0x100000401, ID-9779)`) | `tcgen05.sp.*` | sm_100 + tmem |
| 9848 / 9853 | nvvm.red f32/f64 + integer | inline | `red.add.*` | sm_70+ |
| 9856 / 9857 | tcgen05.mma / .ws | `sub_1A80E40` arms 12/13 | `tcgen05.mma{,.ws}.sync` | sm_100 + tmem |
| 9858-9866 | stmatrix | inline | `stmatrix.sync.aligned.*` | sm_90 |
| 10379-10382 | alt stmatrix | inline | `stmatrix.{16, 32}.aligned.*` | sm_90 |
| 10521-10530 | tcgen05.mma family | `sub_1A80E40` arms 14+ | `tcgen05.mma.*{sp, block_scale, sp.block_scale}` | sm_100a + PTX>=7.7 |
| 10535-10571 | mbarrier/fence/barrier expand | inline | `mbarrier.*`, `fence.*` | sm_70+ |

The full intrinsic-ID space dispatched out of `sub_1A854E0` and its delegates is `[8294, 10995]`. Gaps between named rows above are reserved holes NVIDIA leaves for future PTX feature blocks. The MatcherTable absorbs them as TableGen patterns or routes them to the upstream `SelectCode` path; a reimplementation should leave the same holes rather than densifying the dispatch.

The bit-test on the sparse-texture row deserves a separate note. IDs 9779 and 9811 are 32 apart, which sits inside the 64-bit mask `0x100000401` (bit 0 for 9779, bit 10 for 9789 — the unused mid-slot — and bit 32 for 9811). The `sub_1A80E40` arm reads the literal mask, subtracts 9779 from the incoming ID, and uses `_bittest64` to select between two emission paths in a single instruction. A switch-based reimplementation must match the same two-arm fan-out even though the mask appears to admit a third bit.

Cross-references: tcgen05 commit/arrive layout and the WGMMA-side mbarrier wiring are documented in [tcgen05-wgmma-mbarrier-cluster.md](tcgen05-wgmma-mbarrier-cluster.md); the per-register-class vtables that back ldmatrix and stmatrix sit in [ldmatrix-stmatrix-and-register-class-vtables.md](ldmatrix-stmatrix-and-register-class-vtables.md); the TMA descriptor and `cp.async.bulk.tensor` IDs map to the descriptor encoders documented in [tma-tensormap-and-cp-async-bulk.md](tma-tensormap-and-cp-async-bulk.md).

## The unsafe-fp-math FTZ Probe in Case 0x66

Case `0x66` is the architecturally important inline body in `sub_1A854E0`. It is the clearest demonstration of how NVIDIA's selector differs from upstream TargetOption-layer FTZ control. Upstream LLVM picks FTZ-flavored FMA opcodes at module level: the `denormal-fp-math` and `nvptx-f32ftz` codegen options get read once when the `TargetMachine` is constructed, and every FMA in the module inherits the same FTZ semantics. The case-`0x66` body in tileiras probes the per-function attribute on each individual FMA selection and emits one of two different MI opcode sequences depending on the result.

The probe itself is a string-key lookup against the `LLVM::Function` attribute table. The selector takes the `Function *` from the surrounding `MachineFunction` (`a5` in the function ABI), passes it to `sub_3FC6800`, and asks for the value of the attribute named `"unsafe-fp-math"`. The 14-byte length argument is a verbatim `strlen("unsafe-fp-math")` consumed by the attribute lookup helper, which compares the key in length-prefixed form.

```c
bool select_fma_with_unsafe_fp_math_probe(SelectorState *st, SDNode *node,
                                          ChainWrap *cw, SelectionDAG **dag,
                                          MachineFunction *mf,
                                          SDValue chain_in) {
    LLVMFunction *func = machine_function_get_llvm_function(mf);
    bool unsafe = attribute_table_has(func, "unsafe-fp-math", 14);

    uint16_t flags = sdnode_flags(node);
    bool use_ftz_path = unsafe || (flags & 0x40) != 0;

    if (use_ftz_path) {
        SDValue mul   = emit_node(dag, 0x65,  chain_in, /* FMA */          ...);
        SDValue wrap  = emit_node(dag, 0x10F, mul,      /* FTZ_WRAP */     ...);
        SDValue inner = emit_node(dag, 0x64,  wrap,     /* FMAD, flags=512 */ ...);
        return emit_node(dag, 0x63, inner, /* FMAD, flags=512 */ ...);
    } else {
        SDValue alt   = emit_node(dag, 0xF7, chain_in, /* FTZ_ALTERNATE wrapper */ ...);
        SDValue addr  = emit_node(dag, 0xD2, alt,      /* INST_WRAPPER, ADDRESSOF-chain */ ...);
        SDValue copy  = emit_node(dag, 0x11, addr,     /* CopyToReg */     ...);
        uint16_t mvt  = sdnode_result_mvt(node);
        uint32_t mul_add_op = (mvt_is_f32(mvt)) ? 207 : 208;
        return emit_node(dag, mul_add_op, copy, ...);
    }
}
```

Two pieces of state can force the FTZ path. The first is the function attribute, a per-Function override any front-end can set on individual calls without touching global target options. The second is bit `0x40` of the SDNode's flag word, which the DAG legalizer sets when an earlier combine has already proved FTZ semantics safe (non-denormal constant operands, for instance). ORing the two sources together means a single FMA can take the FTZ path even when the surrounding function has no `"unsafe-fp-math"` set, and a function with the attribute always takes the FTZ path regardless of the flag word.

The FTZ path emits a four-instruction sequence ending in MI opcode `0x63` (`FMAD` inner with `NoFPExcept` flag bit `0x200` set). The non-FTZ path is the NVIDIA-patched wrapper sequence: MI opcode `0xF7`, an `FMA_NON_FTZ` wrapper absent from upstream LLVM's NVPTX tablegen output and unique to tileiras. From there it threads through opcode `0xD2` (`INST_WRAPPER`, used to keep the chain through an `ADDRESSOF` wrap), `0x11` (`CopyToReg`), and finally an MVT-keyed select between opcode 207 (`MUL_ADD_f32`) and 208 (`MUL_ADD_f64`). A reimplementation cannot just translate a single PTX FMA template — it must preserve the four-node wrapper chain on the non-FTZ path so downstream passes match the same operand layout.

The diagnostic-free nature of this case also deserves a note. Neither path produces an error string. FTZ is a semantic choice, not a target restriction, and the selector implements both. Resist the temptation to centralize FTZ handling at any single point above the selector: the per-call override is the contract.

## Inline Vector-Legalisation Joined Body

Eleven cases (`0x3A`-`0x3C`, `0x3F`-`0x40`, `0xB6`-`0xB9`, `0xBF`-`0xC0`) share one body at `0x1A85520`. The body is an inline vector-legalisation step that runs whenever the parent NVPTXISD opcode is a vector load, vector store, or `BUILD_VECTOR` whose lane MVT is the `v4f32` slot (NVPTX enum value 48). For any other lane MVT the body short-circuits to a single SDNode emission with MI opcode `0x9E`. For `v4f32` it walks the operand array, calls `sub_2007D50` to materialise each element index as a target constant, and emits a per-lane `EXTRACT_SUBVECTOR` (MI opcode `0xA0`) before re-emitting the parent opcode with the extracted operand list.

```c
SDValue select_vector_legalisation(SelectorState *st, SDNode *node,
                                   ChainWrap *cw, SelectionDAG **dag,
                                   SDValue chain_in) {
    DebugLoc *dl = sdnode_debug_loc(node);
    debug_loc_ref(dl);

    MVT result_mvt = sdnode_result_mvt_at(node, cw->index);

    if (result_mvt != MVT_V4F32_SLOT_48) {
        return emit_node(dag, 0x9E, chain_in, /* LEGAL_VECTOR_EXTRACT_COMBINE */
                         dl, node->operand_array, node->num_operands);
    }

    SDValue extracted[MAX_LANES];
    uint32_t count = 0;

    for (uint32_t i = cw->index; i < node->num_operands; ++i) {
        SDValue idx = make_target_constant(dag, i, dl);
        extracted[count++] = emit_node(dag, 0xA0, chain_in, /* EXTRACT_SUBVECTOR */
                                       dl, node->operand_array[i], idx);
    }

    SDValue reassembled = emit_node(dag, sdnode_isd_opcode(node), chain_in,
                                    dl, extracted, count);
    return emit_node(dag, 0x9E, chain_in, dl, reassembled);
}
```

The body preserves the parent opcode in the re-emit step rather than picking a fixed legalised opcode. Every joined case therefore lands at a different downstream pattern even though they all walk through the same inline code: the parent opcode at `SDNode + 24` gets read into a local at the top of the body and replayed when the legalised SDNode is emitted. Hard-coding the re-emit opcode in a reimplementation collapses all eleven cases into one and breaks the MatcherTable patterns that key on the original `NVPTXISD` opcode.

## BUILD_VECTOR Remap in Case 0x16F

Case `0x16F` is the longest inline body in `sub_1A854E0` and the most explicit example of MVT-driven MI opcode selection. It is reached from the Blackwell vector-load path where a tensor-memory unpack feeds a `BUILD_VECTOR` whose source MVT determines the output vector width and element type. The body reads the source operand's MVT slot, remaps it to one of two MI opcodes, then emits a `BUILD_VECTOR` with that opcode plus a per-element `EXTRACT_SUBVECTOR` chain.

```c
SDValue select_buildvec_remap_0x16F(SelectorState *st, SDNode *node,
                                    ChainWrap *cw, SelectionDAG **dag) {
    SDNode *src = sdnode_operand(node, 0);
    MVT src_mvt = sdnode_result_mvt_at(src, cw->index);

    uint32_t out_opcode;
    switch (src_mvt) {
    case MVT_V4F32_SLOT_158:
    case MVT_F16X2_SLOT_66:
    case MVT_BF16X2_SLOT_121:
        out_opcode = 561;             /* BUILD_VECTOR_V4 */
        break;
    case MVT_V8F32_SLOT_174:
        out_opcode = 544;             /* BUILD_VECTOR_V8 */
        break;
    default:
        unreachable("BUG: unsupported MVT for case 0x16F BUILD_VECTOR remap");
    }

    uint32_t elt_count = sdnode_num_operands(node);
    SDValue elts[MAX_LANES];

    for (uint32_t i = 0; i < elt_count; ++i) {
        SDValue idx = make_target_constant(dag, i, sdnode_debug_loc(node));
        elts[i] = emit_node(dag, 0xA0, /* chain */ cw->chain,
                            sdnode_debug_loc(node), sdnode_operand(node, i), idx);
    }

    return emit_buildvec_node(dag, out_opcode, /* DL */ sdnode_debug_loc(node),
                              elts, elt_count);
}
```

The MVT slot numbers in the switch are NVPTX-fork enum values, not upstream `MVT::SimpleValueType` values. Slot 158 is `v4f32`, slot 174 is `v8f32`, slot 66 is `f16x2`, slot 121 is `bf16x2`. The bottom and top guards in the binary (`v104 <= 0x9E` and `v104 > 0x9E and v104 != 174`) catch the entire upstream MVT space that does not correspond to one of the four legal Blackwell lane types and route to the same `BUG()` label as the default case. Preserve the bounds checks in a reimplementation: an out-of-range MVT here is a legalisation invariant violation, not a fallthrough to the MatcherTable.

## SDNode MI Opcode Index

The 58 real bodies collectively emit a small set of unique MI opcodes. The table below collates every opcode that appears as a constant argument to one of the builder functions, the builder it flows through, and its inferred purpose. Two opcodes are NVIDIA-only additions to NVPTX's MI namespace: `0xF7` (`FMA_NON_FTZ`, the case-`0x66` non-FTZ wrapper) and `0x10F` (`FTZ_WRAP`, the case-`0x66` FTZ wrapper). Both are absent from upstream LLVM 18's NVPTX TableGen output.

| MI opcode | Dec | Emitting builder | Purpose |
|---:|---:|---|---|
| `0x11` | 17 | `sub_1FF40D0` | `CopyToReg` |
| `0x63` | 99 | `sub_2008880` (flags=512) | `FMAD` inner, `NoFPExcept` set |
| `0x64` | 100 | `sub_2008880` (flags=512) | `FMAD` inner |
| `0x65` | 101 | `sub_2009D80` | `FMA` (FTZ path) |
| `0x9E` | 158 | `sub_2005A50` | `LEGAL_VECTOR_EXTRACT_COMBINE` |
| `0xA0` | 160 | `sub_2009D80` | `EXTRACT_SUBVECTOR` |
| `0xBC` | 188 | `sub_2009D80` | `MMA_LOAD` |
| `0xBD` | 189 | `sub_2009DB0` | `MMA_STORE` |
| `0xD2` | 210 | `sub_201CAC0` | `INST_WRAPPER` (FTZ path) |
| `0xD8` | 216 | `sub_2009E20` (align=16) | `STORE_VECTOR_WRAP` |
| `0xDA` | 218 | `sub_200ABE0` | `MMA_REG_WRAP` |
| `0xEC` | 236 | `sub_200ABE0` | `mbarrier.inval` wrapper |
| `0xF7` | 247 | `sub_200ABE0` | `FMA_NON_FTZ` (NV-patched) |
| `0x10F` | 271 | `sub_200ABE0` | `FTZ_WRAP` (NV-patched) |
| `0x20C` | 524 | `sub_2005A50` (in `sub_1A85120`) | `cvt.rn.satfinite.*x2.f32` |
| `0x211` | 529 | `sub_2005A50` (in `sub_1A833C0`) | `tcgen05.mma.sync` |
| `0x212` | 530 | `sub_2015B50` (in `sub_1A833C0`) | `tcgen05.mma.ws.sync` |
| 197 | 197 | `sub_1A5F730` | `WMMA_LOAD_DENSE` |
| 198 | 198 | `sub_1A5F730` | `WMMA_LOAD_DENSE_T` |
| 207 | 207 | `sub_2004920` | `MUL_ADD_f32` |
| 208 | 208 | `sub_2004920` | `MUL_ADD_f64` |
| 544 | 544 | `sub_1FF1090` | `BUILD_VECTOR_V8` |
| 561 | 561 | `sub_1FF1090` | `BUILD_VECTOR_V4` |

## Subtarget Probe Surface

A useful invariant for any reimplementation: `sub_1A854E0` and its delegates consult exactly three subtarget fields. The first is the feature byte at `unk_5BEBD51` (`HasTcgen05`); cases `0x30` (inner), `0x31`, `0x32`, and `0xED` (`st.bulk`) require this bit set. The second is the dword at `*(uint32_t *)(subtarget + 344)`, which encodes the SM major version times ten. The cvt_packfloat validator (`sub_1A84900`) and the tcgen05.mma block inside `sub_1A833C0` both consult it; it governs cases `0x2F`, `0x37`, `0x38`, `0x31`, `0x32`, `0xCF`, `0x112`, `0x12C`, `0x12D`, `0x142`, and `0x16F`. The third is the dword at `*(uint32_t *)(subtarget + 348)`, which encodes the PTX version times ten with the last decimal digit holding the architecture suffix (`.a` -> 2, `.f` -> 3). The 10521-10530 tcgen05.mma block in `sub_1A833C0` and the `mma.block_scale` path in case `0x142` both read it. No other subtarget field is read in this function. Fan subtarget probes through a broad feature-flag interface and the reimplementation diverges from the binary on test cases that vary other fields without changing these three.

## MatcherTable and Cost Scoring

The TableGen-generated MatcherTable path is the third selector layer, and it is not a single function. Two procedures collaborate: an upper-half dispatcher (`sub_1AAD9D0`, `trySelectNode`, 8 204 bytes, 61 case labels) decides whether a node has a fast path or must enter the generic matcher, and a recursive pattern-cost scorer (`sub_1AAFA40`, `SelectCodeCommon`, 12 724 bytes, 509 basic blocks, 119 case labels) walks the candidate pattern tree and returns an `int64_t` cost. The dispatcher delegates into the scorer through four call sites; the scorer self-recurses three times at lines 595, 1068, and 1202 of the decompilation. Five predicate helpers — `sub_1AAC4D0` `CostOperand` (299 LOC), `sub_1AACAB0` `CheckComplexPattern` (324 LOC), `sub_1AAD1E0` `CheckSame` / `CheckSameVT` (320 LOC), the 57-LOC shim `sub_1AAD880`, and `sub_1AAF9E0` `OPC_Scope` re-entry — implement the operand-check vocabulary the scorer consumes.

Every return path in the scorer and in each of the five helpers is saturating signed `int64`. The scorer never propagates an unchecked sum. Each arithmetic step performs an overflow probe (`__OFADD__` for addition, an `is_mul_ok` helper for multiplication) and clamps to `0x7FFFFFFFFFFFFFFF` on positive overflow or `0x8000000000000000` on negative overflow. The reason sits at line 405: `v14 = 9LL * (a3 != 2) + 1` injects a depth-dependent multiplier so root nodes weigh 1 and every nested node weighs 10. Inside a `tcgen05.mma` or `wgmma.mma_async` pattern tree with five operand levels and an inner vector-width multiplier of 16-32, an unchecked accumulator overflows `int64_t` before the match completes — and an overflowed cost would make a deep matrix pattern falsely appear cheaper than a shallow one. The saturating clamp is what keeps pattern selection deterministic on Blackwell tensor-memory trees.

```c
static int64_t sat_add_i64(int64_t a, int64_t b) {
    if (b > 0 && a > INT64_MAX - b) return INT64_MAX;
    if (b < 0 && a < INT64_MIN - b) return INT64_MIN;
    return a + b;
}

static int64_t sat_mul_i64(int64_t a, int64_t b) {
    if (!is_mul_ok(a, b)) {
        if ((a > 0) == (b > 0)) return INT64_MAX;   /* same sign -> +INF */
        return INT64_MIN;                            /* opposite sign -> -INF */
    }
    return a * b;
}
```

### Scorer entry and the 119-case opcode dispatch

The scorer is invoked as `sub_1AAFA40(NVPTXISelDAGToDAG *self, SDNode *N, unsigned Depth, __m128i ctx)`. It reads `Opcode = N->opcode` from offset `+16` and computes the depth amplifier first. The 119 case labels partition the NVPTX `ISD` enum into three contiguous ranges. Range 1 covers `0x01..0x5A` — upstream `ISD::CONSTANT_POOL`, `ISD::GlobalAddress`, `ISD::EntryToken`, `ISD::INLINEASM`, and other base kinds. Range 2 covers `0x6D..0xFD` — the NVPTX extensions: `NVPTXISD::LOAD`, `STORE`, `STORE_MASK`, `Intrinsic_W_Chain`, `Intrinsic_WO_Chain`, `FMA_FTZ`, and the `tcgen05` opcodes. Range 3 covers `0x120..0x17A` — the high-numbered NVPTX call-ABI and WGMMA descriptor opcodes such as `CallArg`, `CallPrototype`, `PseudoUseFP`, `SETP_*`, `StoreRetval`, `LoadParam`, `WgmmaDescriptor`.

Most cases collapse onto a shared tail at `LABEL_25`. They load a per-opcode integer constant into a local `v29` and fall through. The constant is the pattern-table row index used by the subtarget-feature predicate, between 78 and 291 in this build. A small number of cases return synthetic literals: `0x9A` returns the constant 4 (an `InvisibleReg`-style fixed cost); `0x08`, `0xD5`, `0xD6`, `0x127`, `0x148` return 0 unconditionally because they are pseudo-ops this layer never matches. Cases `0xE7` and `0xE9` short-circuit into `sub_1AA9FC0` and call it the only `EmitNode` they will ever issue. Cases `0xB1`, `0xB2`, `0xB3` walk vector loads and stores via two cost probes followed by an `is_mul_ok`-guarded multiply by vector width.

```c
int64_t SelectCodeCommon(NVPTXISelDAGToDAG *self, SDNode *N, unsigned Depth, __m128i ctx) {
    uint32_t Opcode = N->opcode;                   /* +16 in SDNode */
    int64_t  Mult   = 9LL * (Depth != 2) + 1;      /* 10x on every non-root step */

    switch (Opcode) {
    case 0x08: case 0xD5: case 0xD6: case 0x127: case 0x148:
        return 0;                                  /* pseudo-ops, no match */
    case 0x9A:
        return 4;                                  /* InvisibleReg fixed cost */
    case 0xE7:
        return sub_1AA9FC0(self, 32, N, sub_3F69B50(self->ctx, N), 1, 0, Depth, 0);
    /* ... 110+ further cases each setting v29 = <row> and goto LABEL_25 ... */
    default:
        goto LABEL_33;                             /* try fast-path emit primitives */
    }

LABEL_25:                                          /* shared CheckPatternPredicate tail */
    return check_predicate_and_emit(self, N, Depth, ctx, /*row=*/v29, Mult);
}
```

### The shared `LABEL_25` predicate tail and the 507-byte feature stride

`LABEL_25` is the single entry point that every range-1 and range-3 case folds into. It reads a byte from the subtarget-feature predicate matrix at the address `*(BYTE *)(v29 + v30 + 507 * v31 + 6544)`. Here `v30` is `self->subtarget` (read from `a1[3]`), `v29` is the per-opcode row constant from the dispatch, `v31` is the active SM-feature slot (`v376`, derived from the current PTX version and architecture bits), and `6544` is the base offset of the predicate matrix inside the `NVPTXSubtarget` object. An earlier reading interpreted the 507-byte stride as a flattened LLVM `FeatureBitset` (4 056 bits per slot); strand BD04 retracted that in favour of the TileAS modulo-scheduling pipeline-lattice transition matrix, which uses a 507-byte row to encode legal pipe-stage transitions per feature row. The matrix entry is consumed as a small enum: values `0` and `1` accept the pattern at base cost; value `4` doubles the cost (the multiplied path at `LABEL_257` that returns `sat_mul_i64(2, v32)`); other values reject by falling through to the fast-path emit attempt at `LABEL_33`. The 4-doubling path is what makes patterns that need a partial pipe-stage retraction cost twice as much as their plain form, biasing the scorer toward shapes the pipeline already supports.

```c
int64_t check_predicate_and_emit(NVPTXISelDAGToDAG *self, SDNode *N, unsigned Depth,
                                  __m128i ctx, int row, int64_t Mult) {
    uintptr_t st  = (uintptr_t)self->subtarget;     /* a1[3] */
    int       slot = self->active_feature_slot;     /* v376 */

    /* Pipeline-lattice transition matrix: 507 B per slot, base +6544. */
    uint8_t pipe = *(uint8_t *)(row + st + 507 * slot + 6544);

    if (pipe <= 1) {
        /* legal direct transition - return the running cost */
        return running_cost;
    }
    if (pipe == 4) {
        /* partial retraction - charge double */
        return sat_mul_i64(2, running_cost);
    }
    goto LABEL_33;                                  /* fall through to OPC_* emit */
}
```

### The five predicate helpers

The scorer leans on five helpers that mirror LLVM's `OPC_*` operand-check vocabulary. `sub_1AAC4D0` is `CostOperand`. It accepts an operand index, a flag word, and a depth, and returns the operand's contribution to the running cost. It fires when the matcher needs to charge for capturing a child node into a recorded slot. `sub_1AACAB0` is `CheckComplexPattern`. It dispatches into the per-target `ComplexPattern` matchers — `SelectAddrModeImm`, `SelectFrameIndex`, address-space classifiers for `tmem`/`shared`/`global` — and returns a cost reflecting how restrictive the pattern was. Impossible patterns return `INT64_MAX`. `sub_1AAD1E0` is `CheckSame` and `CheckSameVT`: pointer equality of two operand nodes (`OPC_CheckSame`) and value-type equality of two operand slots (`OPC_CheckSameVT`). One function services both because the implementation differs only in which byte of the recorded-slot descriptor it loads.

`sub_1AAD880` is a 57-line shim arbitrating between two interpretations of its flag argument. If the high byte of `a4` is zero or the low bit is set (`!BYTE4(a4) || (a4 & 1)`), control delegates directly to `sub_1AAD1E0`. Otherwise, if the recorded slot at `a3 + 8` holds value 18 (`ISD::UNDEF`), the shim returns 0 — undef costs nothing. The remaining path computes `v8 = sub_1AA64C0(...)` (an operand-cost accumulator) and `v9 = sub_1AA8940(...)` (per-operand cost), then performs `*(uint32_t *)(a3 + 32) * v9` with `is_mul_ok` guarding the multiply. The shim's dispatch shape is what keeps the scorer compact: a single recorded-slot descriptor can be checked as `OPC_CheckSame`, as `OPC_CheckSameVT`, or as a count-weighted operand cost depending on flag bits, without branching at the scorer's top level.

`sub_1AAF9E0` is the `OPC_Scope` re-entry — the recursive doorway that `LABEL_25` and the `0xB2`/`0xB3` vector-store cases use to enter a sub-pattern. Structurally it constructs a fresh `MatchContext` on the stack and recursively invokes `sub_1AAFA40` on the candidate sub-tree. The three self-recursion sites in the scorer (lines 595, 1068, 1202) plus the four `sub_1AAF9E0` calls form the mutual recursion that walks the full pattern tree.

```c
int64_t CostOperand(NVPTXISelDAGToDAG *self, int slot, SDNode *child,
                    uintptr_t flags, int *cost_state, ...);             /* sub_1AAC4D0 */

int64_t CheckComplexPattern(NVPTXISelDAGToDAG *self, SDNode *N,
                            const ComplexPatternFn *fn, ...);            /* sub_1AACAB0 */

int64_t CheckSame_or_SameVT(NVPTXISelDAGToDAG *self, int slot,
                            const RecordedSlot *rec, unsigned a5);       /* sub_1AAD1E0 */

int64_t CheckSame_shim(NVPTXISelDAGToDAG *self, int slot, uintptr_t rec_addr,
                       uintptr_t flag_word, unsigned a5) {               /* sub_1AAD880 */
    if (!BYTE4(flag_word) || (flag_word & 1))
        return CheckSame_or_SameVT(self, slot, (RecordedSlot *)rec_addr, a5);
    if (*(uint8_t *)(rec_addr + 8) == 18 /* ISD::UNDEF */) return 0;
    int64_t acc  = sub_1AA64C0(self, (int64_t *)rec_addr, 0, 1);
    int64_t per  = sub_1AA8940(self, slot, *(uintptr_t *)(rec_addr + 24), a5, 0, 0);
    int64_t prod = sat_mul_i64(*(uint32_t *)(rec_addr + 32), per);
    return sat_add_i64(prod, acc);
}

int64_t OPC_Scope_reenter(NVPTXISelDAGToDAG *self, SDNode *sub,
                           unsigned Depth, __m128i ctx, ...);            /* sub_1AAF9E0 */
```

### The 15-opcode `OPC_*` vocabulary

The TableGen primitives the scorer cross-dispatches form a compact 15-entry vocabulary. They are not consumed as a linear byte stream by `sub_1AAFA40` directly — the scorer calls the predicate helpers, and those helpers internalize the opcode semantics. The vocabulary still matches upstream LLVM's `SelectionDAGISel.h` enum byte-for-byte because the TableGen emitter produced both.

| Primitive | Backed by | Semantics |
|---|---|---|
| `OPC_Scope` | `sub_1AAF9E0` | Enter a fresh recursive match scope; on failure return to enclosing scope. |
| `OPC_RecordChild0..7` | `sub_1AAC4D0` | Capture operand `i` into recorded slot `r`. |
| `OPC_CheckPatternPredicate` | `LABEL_25` matrix probe | Test pipeline-lattice byte at `+6544 + 507·slot + row`. |
| `OPC_CheckOpcode` | dispatch `switch(v10)` | Test `N->opcode == expected`. |
| `OPC_CheckType` | `sub_1AAD1E0` | Test `N->valueType(i) == MVT::X`. |
| `OPC_CheckChild0Type` | `sub_1AAD1E0` | Same, applied to child 0. |
| `OPC_CheckSame` | `sub_1AAD880` shim | Test pointer equality of two recorded slots. |
| `OPC_CheckSameVT` | `sub_1AAD1E0` | Test value-type equality of two recorded slots. |
| `OPC_CheckComplexPat` | `sub_1AACAB0` | Invoke target-specific `ComplexPattern` matcher. |
| `OPC_SwitchOpcode` | dispatch tail at `LABEL_33` | Multi-way fast-path branch on `N->opcode`. |
| `OPC_SwitchType` | dispatch tail at `LABEL_33` | Multi-way fast-path branch on `N->valueType(0)`. |
| `OPC_EmitInteger` | `sub_1A9BF90` | Materialize a constant operand. |
| `OPC_EmitNode` | `sub_1A9C8F0` / `sub_1AA9FC0` | Build the output `MachineSDNode`. |
| `OPC_CompleteMatch` | `sub_1A9AB90` | Commit uses, return accepted cost. |
| `OPC_MoveParent` / `OPC_Reject` | scorer epilogue | Walk parent chain or return failure (cost = 0). |

The literal byte stream of these `OPC_*` codes — the actual data the TableGen emitter writes into a `static const unsigned char MatcherTable[]` — lives outside `sub_1AAFA40`. It sits in `.rodata`, addressed by `0x5B***` globals the scorer reads through the row constants in `v29`. Pattern-name strings paired with each row are plain ASCII in `.rodata` and fingerprint the NVIDIA data patch: `"setmaxregister"`, `"cp.async.bulk.tensor.group.shared.cluster"`, `"wgmma.mma_async.sync.aligned"`, `"wgmma.fence.sync.aligned"`, `"tcgen05.mma.sync"`, `"tcgen05.mma.ws.sync"`, `"mma.block_scaled.sync.aligned"`, `"mma.sp.sync.aligned.m8n8k16"`. These names do not live in the XOR-3 mnemonic pool. They are the TableGen-emitted pattern records, distinct from the lowered PTX mnemonics the AsmWriter prints.

### The upper-half dispatcher

`sub_1AAD9D0` is the first thing every node sees once the intrinsic and vector-memory selectors have declined. It reads `N->opcode` from offset `+16`, partitions on whether the opcode is `<= 0xD6` or `<= 0x17C`, and probes for a fast-path emit primitive through `sub_3FD9730` (`hasPatternFastpath`). On a fast-path hit the dispatcher returns 1 without entering the scorer. On a miss the dispatcher consults a smaller per-opcode pattern-presence table. Missing entries jump to `LABEL_15` and drop back to the caller; present entries call into the scorer through one of the four `sub_1AAF9E0` / `sub_1AAFA40` call sites and use the returned cost to commit or reject the match. The 61 case labels in this dispatcher are a strict subset of the scorer's 119 — every dispatched opcode has a scorer entry, but not every scorer entry has a fast path.

```c
bool trySelectNode(NVPTXISelDAGToDAG *self, SDNode *N, unsigned Depth, __m128i ctx) {
    uint32_t Opcode = N->opcode;

    if (Opcode <= 0xD6) {
        switch (Opcode) { /* 0 for unsupported pseudo-ops; fall through otherwise */ }
    } else if (Opcode <= 0x17C) {
        /* hasIntrinsic check for high-numbered ISD opcodes */
    }

    if (hasPatternFastpath(Opcode))
        return emit_fastpath(self, N);              /* sub_3FD9730 → emit_* */

    if (!hasMatcherEntry(Opcode))
        return false;                                /* LABEL_15 - no pattern */

    int64_t cost = SelectCodeCommon(self, N, Depth, ctx);   /* sub_1AAFA40 */
    return commit_if_profitable(self, N, cost);
}
```

The scorer's mutual recursion with this dispatcher is how a single top-level node produces a tree of `EmitNode` calls. Each successful scope commits one machine node; the scorer recurses through `sub_1AAF9E0` into the next sub-pattern; and so on. Reimplementations must preserve the order — fast-path probe first, scorer second — because some Blackwell intrinsics rely on the fast-path emitting a single machine node that the scorer would otherwise score apart into a less efficient `MOV` + `EmitInteger` pair.

### Reimplementation invariants for the scorer

Saturating arithmetic is mandatory. The depth amplifier `Mult = 9 * (Depth != 2) + 1` must be preserved exactly; substituting `Depth == 0 ? 1 : 10` is only correct if the caller never invokes the scorer at depth 2 as the initial scope. The `507·slot + 6544` pipeline-lattice probe must read a byte, not a bit, and must compare `<= 1` for accept and `== 4` for double-cost; other values fall through to `LABEL_33`. The five predicate helpers each return saturating `int64`. The `sub_1AAD880` shim's flag-byte dispatch (`!BYTE4(a4) || (a4 & 1)`) must come before the `ISD::UNDEF` zero-cost check, because reordering exposes a fast-path where an undef in a `CheckSameVT` slot would silently match. The upper-half dispatcher must consult the fast-path probe before the matcher-entry table; reverse the order and every Blackwell tensor-memory intrinsic ends up running the full 507-row predicate scan.

### Binary evidence

The scorer body lives at `sub_1AAFA40` (12 724 B, 119 cases, three self-recursion sites at lines 595, 1068, 1202 of the decompilation). The upper-half dispatcher lives at `sub_1AAD9D0` (8 204 B, 61 cases). The five predicate helpers are `sub_1AAC4D0` (`CostOperand`), `sub_1AACAB0` (`CheckComplexPattern`), `sub_1AAD1E0` (`CheckSame`/`CheckSameVT`), `sub_1AAD880` (the 57-line `CheckSame` shim), and `sub_1AAF9E0` (`OPC_Scope` re-entry). Pattern-name strings observed in `.rodata` (`"tcgen05.mma.sync"`, `"wgmma.mma_async.sync.aligned"`, `"mma.block_scaled.sync.aligned"`, and so on) fingerprint the NVIDIA pattern set against an upstream LLVM 18 NVPTX TableGen output. The 507-byte stride interpretation describes the TileAS modulo-scheduling pipeline-lattice transition matrix rather than a flattened LLVM `FeatureBitset`.

## Vector Load/Store Selection

Vector memory operations flow through a hierarchy of primary vector cases, NVIDIA extension cases, and scalar fallbacks. Tensor-memory (`tmem`) variants use an address-space marker outside upstream NVPTX's ordinary address-space range. The selector reads that marker plus the subtarget feature set to decide between tensor-memory loads/stores and the fallback path.

```c
bool select_vector_load_store(SDNode *node, SelectorState *st) {
    VectorClass cls = classify_vector_memory_node(node);

    if (cls.requires_tmem) {
        if (!st->subtarget.has_tensor_memory)
            return false;
        return emit_tmem_vector_memory(node, st);
    }

    if (cls.requires_bulk_tensor)
        return emit_tma_bulk_tensor(node, st);

    if (cls.is_predicated_global_store)
        return emit_predicated_vector_store(node, st);

    if (cls.can_be_merged_to_wide_vector)
        return emit_merged_vector_access(node, st);

    return false;
}
```

Wide-vector paths group scalar or smaller-vector operands into a single vector operation when lane count and memory class allow. Preserve the grouping rules in a reimplementation: they affect both emitted PTX shape and register pressure.

## SelectLoadVector / SelectStoreVector Dispatcher

In tileiras, `select_vector_load_store` realizes as `sub_1A874A0` (`NVPTXDAGToDAGISel::SelectLoadVector` / `SelectStoreVector`) — 9 857 B, 426 basic blocks, dominated by two jump tables and a short scalar tail. The primary jump table at `0x1A874EF` covers exactly 80 contiguous SDNode opcode values in `[158, 237]`; the secondary at `0x1A87526` covers 44 NVPTX-extension opcodes in `[524, 567]`. Eight short-circuit scalar branches sit before and after the jump tables and handle isolated opcodes `{58, 60, 98, 300, 301, -995, -5313, -5314}`, rounding the dispatch surface to 90 entries.

At entry the function reads a cached predicate that gates the kernel-parameter paths: `v10 = *(uint32_t *)(*(uintptr_t *)(a1 + 8) + 648)`. `a1 + 8` is the `NVPTXTargetMachine *` field on the selector; offset `648` is a boolean `hasVecLDST` derived during `runOnMachineFunction`. The boolean must be true before cases 58, 60, and 301 fire; when it is false the function falls through to the upstream `SelectCode` MatcherTable. The dispatch key itself is the SDNode opcode read from `*(uint32_t *)(a2 + 24)` — identical to the upper-half dispatcher's key in `sub_1A854E0`.

```c
unsigned __int64 select_vector_load_store(NVPTXISelDAGToDAG *self, SDNode *N,
                                          ChainWrap *cw, SelectionDAG **dag,
                                          MachineFunction *mf, ...) {
    bool hasVecLDST = *(uint32_t *)(*(uintptr_t *)((uint8_t *)self + 8) + 648);
    int  op        = *(int32_t *)((uint8_t *)N + 24);              /* SDNode->NodeType */

    if (op > 237) {
        if (op > 567)         return 0;                            /* MatcherTable fallback */
        if (op <= 523) {
            if (op == 300)    return SelectLoadParam(N, self);     /* sub_1A65F50 */
            if (op == 301 && self->vec_len > 2)
                              return SelectLoadParamV4(N, self);   /* sub_1A624D0 */
            return 0;
        }
        switch (op) {                                              /* sw2: [524, 567] */
        case 524:             return SelectStoreTmemV8Pred(N);     /* 0x1A87A78 */
        case 538: case 539: case 563:
                              return SelectLoadStoreV2(N, self);   /* sub_1A65F50 */
        case 543: case 544:   return SelectLoadStoreV4(N, self);   /* sub_1A624D0 */
        case 549: case 550: case 551:
        case 565: case 566: case 567:
                              return SelectV8_F16BF16Absorb(N);    /* 0x1A87870 / 0x1A87950 */
        default:              return 0;
        }
    }

    if (op > 157) {                                                /* sw1: [158, 237] */
        switch (op) {
        case 158:             return SelectLoadV4Tmem(N, self);
        case 160:             return SelectStoreV4Tmem_BuildVec(N, self);
        case 188:             return SelectLoadParamV4_TmemAware(N, self);
        case 192:             return SelectStoreParamV(N, self);   /* sub_1A65610 */
        case 208:             return SelectTMA_BulkTensor_V4(N, self);
        case 210:             return SelectTMA_BulkTensor_V2(N, self);
        case 215: case 216:   return SelectStoreV4_Predicated64(N, self);
        case 218:             return SelectStoreVectorByImm(N, self);
        case 236:             return SelectLoadV_Tmem_SubVec(N, self);
        case 237:             return SelectBitcastVectorCSE(N);
        default:              return 0;                            /* 69 fallthroughs */
        }
    }

    if (op == 98)             return SelectNonTemporalLoadV(N, self);
    if (op == 58 && hasVecLDST) return SelectBuildVectorI64(N, self);
    if (op == 60 && hasVecLDST) return SelectScalarToVectorI32(N, self);
    if (op == -995)           return SelectStoreVectorByImm(N, self);
    if ((unsigned)(-op - 5313) <= 1)
                              return SelectLoadV_Tmem(N, self,
                                       *(int32_t *)(self->subtarget + 352) - 50 <= 0x13);
    return 0;
}
```

The primary switch holds 80 case labels, but only 11 carry non-default bodies. Cases 158, 160, 188, 192, 208, 210, 215, 216, 218, 236, and 237 dispatch to NVIDIA-specific emission helpers; the remaining 69 labels (159, 161-187, 189-191, 193-207, 209, 211-214, 217, 219-235) join the shared `0x1A87820` tail and return zero so the outer trampoline at `sub_1AAD9D0` can hand the node to the MatcherTable. The negative aliases `-5313` and `-5314` are NVIDIA's post-LLVM-19 reservation for `NVPTXISD::LoadV2_Tmem` and `StoreV2_Tmem`; both route through the same `sub_1A86D30` emitter as case 236, but with the SM-minor predicate read from `subtarget + 352`. The predicate `cc - 50 <= 0x13` (raw SM minor in `[50, 69]`) is what distinguishes the Blackwell tmem path from the upstream tcgen05 emitter.

### Address-Space and MVT Probes

Five of the 11 active cases probe address space 255 — the NVPTX-internal `tmem` marker absent from upstream LLVM, where the highest defined address space is 103. Cases 158, 160, 188, 215/216, and 236 each test `*(uint32_t *)(memop + 96) + 32 == 255` to confirm the node operates on Blackwell tensor memory before dispatching into `sub_1A86D30`. Case 158 also accepts address space 16 (the NVPTX `param` AS) on the same handler when the inner MVT is `v16i32`, `v32i32`, `v8f32`, or `v16f32`; the BITCAST plus EXTRACT chain it emits is the routing flag the Blackwell emitter uses to disambiguate tmem from param.

Case 158 (`NVPTXISD::LoadV4Tmem`) gates on MVT values 48, 60, 130, and 142 — `v16i32`, `v32i32`, `v8f32`, `v16f32`. On a match it emits a chain that begins with MI opcode `0xD8` (`BITCAST`) and continues with one or more `0x9E` (`EXTRACT_VECTOR_ELT`) operations through `sub_200ACC0` and `sub_1A5FE60`. Case 208 (`NVPTXISD::TMA_BULK_TENSOR_V4`) is the Blackwell `cp.async.bulk.tensor` 4-lane store materialiser. Its body contains a `do { ... } while (v59 != 4)` loop that walks the operand array four times and emits MI opcodes `0xA0` (`BUILD_VECTOR_V4`), `0xCF` (`CP_ASYNC_BULK_TENSOR_V4_SHARED_CLUSTER`), and `0x9E` (`EXTRACT_VECTOR_ELT`) via `sub_201CAC0`. Cases 215 and 216 share one handler body at `0x1A879D0` that emits predicated v4 i64 stores. Case 215 emits MI opcode 519 (`STV_U32_GLOBAL`) when `(*(uint8_t *)(v47 + 28) & 2) != 0`; case 216 emits MI opcode 520 (`STV_U64_GLOBAL`) on the complementary odd-flag path `(*(uint8_t *)(v47 + 28) & 1) != 0`. Both paths verify that the inner operand MVT is `i64` (MVT 7) with a sub-element MVT of `i32` (MVT 6).

The secondary switch is structurally simpler. Six of its 44 cases dispatch to one of two emitters — `sub_1A65F50` for v2 patterns, `sub_1A624D0` for v4 patterns — while another six (the alt-encoded v8 loads at 549-551 and stores at 565-567) share a chain-absorb tail at `0x1A87870` / `0x1A87950`. The v8 tail reads 20 operands per group and uses the magic constant `0xCCCCCCCD * (x >> 2) >> 32` to perform a divide-by-five group-count computation; the result drives a merge of two `LOAD_VECTOR_V2` chains into a single `LOAD_VECTOR_V8` pattern the downstream scheduler can soak into a single MMA-feeding shared-memory transaction. Case 524 (`NVPTXISD::STV_PRED_V2_TMEM_V8`) is the only handler in the secondary switch that emits MI opcodes directly: it builds a 2-lane predicated store to tmem with an 8-wide stride encoding computed by `sub_1A5E450`.

### Named Case Bodies

The 11 named bodies on the primary switch, together with their MVT/AS gates and the MI opcodes they emit, are:

| Case | Handler addr | Semantic                                              | W       | Elt gate (MVT)         | AS gate     | MI opcodes emitted               |
|-----:|--------------|-------------------------------------------------------|---------|------------------------|-------------|----------------------------------|
|  158 | `0x1A880B0`  | `NVPTXISD::LoadV4Tmem` Blackwell tensor-memory load   | v4/v8/v16 | 48 / 60 / 130 / 142  | tmem (255), param (16) | `0xD8`, `0x9E` chain    |
|  160 | `0x1A88228`  | `BUILD_VECTOR_V4` tmem-store materialise              | v4      | i32 / v2i64 / bf16x2 / f16x2 | tmem (255) | `0xC1`, `0xD9`, `0xDA`, `0xEC`    |
|  188 | `0x1A87610`  | `LoadParamV4` tmem-routed kernel arg                  | v4      | `v20==12 \|\| ==36`    | param 101 -> tmem 255 | `0xD8`, delegate `sub_1EB1CC0` |
|  192 | `0x1A87E60`  | `StoreParam` / ret-value packer                       | v2/v4   | any pack <= i64        | param/local | from `sub_1A65610`               |
|  208 | `0x1A87B60`  | `TMA_BULK_TENSOR_V4` 4-lane bulk-tensor store         | v4      | f32 (MVT 38 gate)      | shared::cluster (7) | `0xA0`, `0xCF`, `0x9E`    |
|  210 | `0x1A87EC0`  | `TMA_BULK_TENSOR_V2` 2-lane TMA load                  | v2      | v8f32 (130) / v16f32 (142) | generic/global | `0x9E`, 521/522 via `sub_20159B0` |
| 215/216 | `0x1A879D0` | Predicated v4 i64 global store                       | v4      | i64 (MVT 7, sub-elt 6) | global      | 519 / 520                        |
|  218 | `0x1A87800`  | `StoreConstVector` immediate-offset store             | v2/v4   | any                    | const / global | from `sub_1A5E690`             |
|  236 | `0x1A87AE0`  | `LoadV_TmemSubVector` ext-load feeding v4/v8 tmem     | v4/v8   | i32 / bf16 / v8f32 / v16f32 | tmem (255) | delegate `sub_1A86D30`     |
|  237 | `0x1A87E88`  | `BUILD_BITCAST_VECTOR` identity CSE                   | v2      | identity fold          | n/a         | chain pass-through               |

Case 236 is the routing hinge for the Blackwell tmem extend-load path: when the inner SDNode's opcode is itself negative (the `LoadV2_Tmem`/`StoreV2_Tmem` band), the case delegates to `sub_1A86D30(N, TM, cc <= 0x13)` and lets the TMEM emitter resolve the final MI opcode. The SM-minor predicate matches the one used by the scalar-tail `-5313/-5314` paths and is what allows tileiras to fold an extending tmem load with a vector consumer in a single pattern match — a fold absent from upstream LLVM 18 NVPTX, because the entire negative-opcode band is NVIDIA-private.

Case 237 is the identity fold. The body returns the inner node's first operand verbatim when the inner SDNode also has opcode 237 and the result-VT word at `*(uint32_t *)(a2 + 100)` equals the inner node's `*(uint32_t *)(inner + 96)`. Consecutive `BUILD_BITCAST_VECTOR` nodes therefore collapse to a single chain link, which the downstream scheduler treats as a no-op for register-pressure accounting.

### Sub-helper Roster

Twelve sub-helpers carry the actual emission work for the named cases. Their sizes and inferred signatures are:

| Helper        | Size | Inferred signature                                                            | Role                                                                                       |
|---------------|------|-------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `sub_1A624D0` | 1374 | `SelectBaseMemVectorInst(SDNode*, TM*, imm, imm, ptr)`                        | 25-case inner switch on values 543-567; emits `ld.v[2\|4\|8].{global\|shared\|local\|const\|param}` MI ops |
| `sub_1A65F50` | 2319 | `SelectLoadParamV*(SDNode*, TM*)`                                             | Kernel-argument vector load; case 300 and the v2 secondary-switch arms (538, 539, 563)     |
| `sub_1A65610` | 2357 | `SelectStoreParamV*(SDNode*, TM*)`                                            | Case 192 ret-value packing; v2/v4 param-AS stores                                          |
| `sub_1A5E690` | 481  | `SelectStoreVectorByImm(SDNode*, TM*)`                                        | Case 218 and the scalar-tail opcode `-995` fallback; constant-offset addressing            |
| `sub_1A86D30` | 1895 | `SelectLoadStoreTMEM(SDNode*, TM*, hasSM70orNewer)`                           | tmem (tcgen05.ld/.st) plus ext-load variants; case 236 and the `-5313`/`-5314` band        |
| `sub_1A61760` | 779  | `SelectLoadVectorNonTemporal(SDNode*, TM*, ...)`                              | Case 98 (`ISD::NON_EXTLOAD` vector) when inner MVT is in `[12, 13]` (i64-packed)          |
| `sub_1A5F3B0` | 881  | `SelectLoadVectorPtr(SDNode*, TM*, imm, SDValue, SDValue)`                    | Case 58 (`ISD::BUILD_VECTOR`) when inner MVT is i64 (MVT 7); called twice for op0 and op1  |
| `sub_1A5F190` | 120  | `DecodeMemOperand(SDValue)`                                                   | Addressing-mode classifier; returns 0 for non-addressable operands                         |
| `sub_1A5F210` | 412  | `SelectMemInst2(SDValue, ...)`                                                | Case 60 (`ISD::SCALAR_TO_VECTOR`) 2-phase lowering; only invoked if `sub_1A5F190` succeeded |
| `sub_1A5FE60` | 282  | `SelectTmemAddr(SDValue, SDValue, imm, ..., ptr, TM*)`                        | Address-calc helper for case 158's tmem variant                                           |
| `sub_1A5E450` | 479  | `SelectVectorStride(SDValue, SDValue, int)`                                   | Stride-encoding helper for case 524 (TMA vector store)                                     |
| `sub_1A5D780` |  46  | `IsScalableVTLegal(MVT)`                                                      | Returns true for the small set of scalable vector types that NVPTX legalises directly      |

The emitters at the bottom of the call graph (`sub_2005A50`, `sub_2009D80`, `sub_200ACC0`, `sub_201CAC0`, `sub_200ABE0`) are the same SDNode-construction APIs `sub_1A854E0` uses for intrinsic-with-chain selection. The MI opcode is always passed as a literal integer in the call. Reimplementations cannot factor these calls into a generic `emit_node(opcode)` template without preserving the per-call flag-word and chain-operand layout: the downstream scheduler reads bits from those operands when it groups vector accesses into wide-vector transactions.

### SDNode and Memop Offset Map

The selector probes a small set of byte offsets inside the SDNode and the attached `MachineMemOperand`. The offsets are stable across the binary and form part of the in-memory ABI a reimplementation must reproduce when it wants to share the same MatcherTable byte stream.

| Object   | Offset | Field                            | Read in cases                                            |
|----------|-------:|----------------------------------|----------------------------------------------------------|
| SDNode   | `+24`  | `NodeType` (dispatch key)        | every case                                               |
| SDNode   | `+40`  | `OperandList` (`SDValue *`)      | 158, 160, 188, 192, 208, 210 (operand walk)              |
| SDNode   | `+48`+16i | result-VT table (16 B / slot, MVT word + type ptr) | 158, 160, 210, 236, 237                |
| SDNode   | `+64`  | `NumOperands` and flag word      | 208 (do-while group count), 210, 565-567                 |
| SDNode   | `+72`  | `MemoryVT` alignment / ordering  | 188, 192, 215, 216, 218                                  |
| SDNode   | `+80`  | `MachineMemOperand *` list head  | 158, 160, 188, 192, 215, 216, 218, 236                  |
| SDNode   | `+100` | result-VT word (CSE compare key) | 237                                                      |
| memop    | `+96+32` | `AddressSpace` (8-bit enum)    | 158, 160, 188, 215, 216, 236, 524                        |
| memop    | `+28`  | predicate / overlap flag word    | 215 (`& 2`), 216 (`& 1`)                                 |
| TM       | `+8`   | subtarget pointer (`a1 + 8`)     | entry preamble                                           |
| subtarget| `+352` | SM-minor dword                   | case 236, `-5313`/`-5314` scalar band                    |
| subtarget| `+648` | `hasVecLDST` boolean             | entry preamble; gates cases 58, 60, 301                  |

The address-space probe is the most diagnostic of the lot. Address space 255 is the NVPTX-internal `tmem` marker — present only in this binary and the tcgen05 emitters, with no upstream LLVM analogue because the highest upstream NVPTX address space is 103. The same field reaches case 158 with value 16 (NVPTX `param`) and value 255 (`tmem`), which is what lets a single handler route both Blackwell tmem loads and kernel-argument vector reads: the BITCAST-plus-EXTRACT chain is identical, and only the address-space tag tells `sub_1A86D30` whether to emit a `LD_V_TMEM_*` or a `LDV_PARAM_*` MI opcode.

### Delta Summary vs Upstream LLVM 18 NVPTX

All 11 non-default bodies are NVIDIA-added behaviours relative to a clean LLVM 18.1.4 NVPTX tree. The most visible deltas are the tmem fold (case 158 BITCAST-chain pre-pattern), the v4 tmem store materialiser (case 160), the predicated v4 i64 stores (cases 215/216), the immediate-embedded vector store (case 218), the `cp.async.bulk.tensor` 2- and 4-lane materialisers (cases 208 and 210), and the chain-pass-through CSE at case 237. The 69 default-slot cases stay unchanged from upstream; they reach `SelectCodeCommon` (`sub_1AAFA40`) via the outer trampoline at `sub_1AAD9D0`. Port the upstream NVPTX selector and add only the 11 NVIDIA bodies and the reimplementation matches tileiras on every test that does not depend on tmem-specific addressing — the Blackwell-specific surface is the only place the two need to converge byte-for-byte.

The cleanest invariant to preserve is the order of the two jump tables and the negative-opcode band. Primary switch first, secondary second: the secondary switch's v2 and v4 emitters fall through into the same `sub_1A65F50`/`sub_1A624D0` helpers the primary switch invokes through cases 188 and 192. Reverse the order and an alt-encoded `LoadV2` (opcode 538) reaches the v4 emitter through case 188's `LoadParamV4` path, emitting a spurious `ld.v4` for what should be a `ld.v2`. The negative-opcode band must be tested after both jump tables: the predicate `(unsigned)(-op - 5313) <= 1` is two-valued and any earlier test would have to special-case the wrap-around. The `hasVecLDST` boolean must be checked before cases 58, 60, and 301, because all three bodies emit MI opcodes the downstream scheduler cannot soak when the target lacks native vector LD/ST.

## Subtarget Feature Model

Tileiras recognizes a wide historical NVPTX CPU table, but the driver itself accepts only a narrow Blackwell target set. The backend feature table distinguishes ordinary Blackwell from arch-conditional and family-conditional variants. Tensor memory is present on datacenter Blackwell variants and absent on consumer Blackwell targets.

| Target family | Tensor memory | Notes |
|---|---:|---|
| Hopper base and older | No | WGMMA and TMA support depends on Hopper feature bits, not tensor memory. |
| Datacenter Blackwell arch/family variants | Yes | Required for `tcgen05` tensor-memory MMA paths. |
| Consumer Blackwell | No | Uses block-scaled MMA where tensor memory is unavailable. |

Target validation should be explicit and early:

```c
void validate_tcgen05_target(const SDNode *node, const Subtarget *st) {
    if (!st->has_tensor_memory)
        fatal("Not supported on this architecture");

    if (!st->is_blackwell_datacenter_variant)
        fatal("tcgen05.mma supported only on arch-conditional or family-conditional variants from SM100 onwards.");

    if (!ptx_version_supports_tcgen05(st->ptx_version))
        fatal("tcgen05.mma requires a newer PTX version");
}
```

## Packed Narrow-Float Conversion

The `cvt_packfloat` family validates both source and destination narrow-float formats. Source and destination get packed into small integer fields, then checked against SM and PTX feature gates.

```c
void validate_packfloat(const SDNode *node, const Subtarget *st) {
    PackedFloatMode mode = decode_packfloat_mode(node->immediate);

    if (!st->supports_sm90_or_newer || !st->supports_ptx_78_or_newer)
        fatal("cvt_packfloat intrinsic needs atleast SM90 and PTX >= 78");

    if (mode.uses_fp6_or_fp4 && !st->is_blackwell_arch_conditional)
        fatal("FP6/FP4 packed conversion requires Blackwell arch-conditional support");

    if (mode.uses_ue8m0x2 && !st->is_blackwell_arch_conditional)
        fatal("UE8M0x2 packed conversion requires Blackwell arch-conditional support");
}
```

The exact diagnostic spelling is part of compatibility for test suites that assert error text.

## AsmWriter String Tables

The NVPTX AsmWriter stows its opcode mnemonic pool and physical-register-name pool in an obfuscated data segment, then decodes them once before first use. The cipher is intentionally simple: byte `i` is XORed with `(3 * i) mod 256`.

```c
void xor3_decode(uint8_t *begin, uint8_t *end) {
    uint8_t key = 0;

    for (uint8_t *p = begin; p != end; ++p) {
        *p ^= key;
        key = (uint8_t)(key + 3);
    }
}
```

It is not a security boundary. The cipher prevents naive string extraction from surfacing every PTX mnemonic, but it is fully reversible and deterministic. A compatible open implementation can store mnemonic tables plainly unless binary-for-binary compatibility with NVIDIA's object layout is a goal.

## SDNode Layout Fingerprint

The selector repeatedly reads a 27-bit operand-count field from every `SDNode`. That is the standard LLVM `SDNode::getNumOperands()` layout: low 27 bits for the operand count, upper bits for status flags. Operands live in a contiguous `SDUse` array immediately before the node header.

```c
uint32_t sdnode_num_operands(const SDNode *node) {
    return node->operand_count_and_flags & ((1u << 27) - 1u);
}

SDUse *sdnode_operands(const SDNode *node) {
    uint32_t n = sdnode_num_operands(node);
    return (SDUse *)((uint8_t *)node - n * sizeof(SDUse));
}
```

For reverse engineering, this layout identifies SelectionDAG walkers in a stripped binary. For reimplementation, it matters only when reproducing the in-memory ABI of NVIDIA's LLVM fork — otherwise use the public LLVM APIs.

## NVIDIA-Specific ISel Patches

The tileiras NVPTX selector carries three byte-identifiable patches over upstream LLVM 21 SelectionDAG with no counterpart in any open-source NVPTX target. Each patch lives in a dedicated arm of one of the dispatchers documented earlier on this page, and each is fingerprintable from a stripped binary because the validator function addresses, intrinsic ID ranges, and diagnostic strings stay stable across builds.

The first patch lives at `sub_1A84900` (2 066 bytes) and is the `cvt_packfloat` 4-gate validator. It is reached from the intrinsic-ID range map for IDs `8294 / 8437-8440 / 8627 / 9123 / 9531-9537`, covering the FP6, FP4, and UE8M0x2 packed-conversion ops. The validator splits the encoded mode argument into two nibbles — `v10 = arg & 0xF` for the source narrow-float type and `v9 = arg >> 4` for the destination — then runs four cascaded gates. Gate one requires SM major at least `0x384` (sm_90) and PTX version at least `0x4D` (PTX 7.7); failure emits `"cvt_packfloat intrinsic needs atleast SM90 and PTX >= 78"` (typo preserved). Gate two fires when the destination nibble selects UE8M0x2 and requires SM major at least `0xA0` (sm_100a); failure emits `"cvt_packfloat to ue8m0x2 needs arch-conditional sm_100a or later"`. Gate three fires when the destination nibble selects fp6x2 or fp4x2 and applies the same SM major check; failure emits `"cvt_packfloat to {fp6,fp4}x2 needs arch-conditional sm_100a or later"`. Gate four fires when the destination nibble selects the family-conditional path and additionally requires `cc.minor == 0xF`, the sm_100f marker. Any failing gate makes the validator return a poison SDNode marked `IsErr`, which the dispatcher drops without falling through to the MatcherTable.

The second patch sits at case `0x66` of `SelectIntrinsic_W_Chain` and is a per-call FTZ override for fused multiply-add. Upstream LLVM handles FMA FTZ semantics only at the `TargetOption` layer — the `nvptx-f32ftz` codegen option is read once when the `TargetMachine` is constructed and every FMA in the module inherits the same FTZ flavor. Tileiras probes two per-instruction sources instead. It first tests the SDNode flag bit `0x40` (`NoFPExcept`); if set, FTZ opcode `0x65` is forced regardless of any function attribute. If the flag is clear it then calls `sub_3FC6800(F, "unsafe-fp-math", 0xE)` against the surrounding `LLVMFunction *`. Attribute set selects FTZ opcode `0x65` (`FMA_FTZ`); attribute unset selects non-FTZ opcode `0xF7` (`FMA`, wrapped). The `NoFPExcept` bit is the standard LLVM flag, but its NVIDIA-specific consequence — forcing FTZ rather than merely allowing it — is not upstream. Both paths are non-upstream.

The third patch lives at `sub_1A80A40` and gates the tcgen05 128-bit atom (intrinsic ID `9132`). The function tests `cc.major >= 0xA0 && hasFeature(80)`, where feature byte `80` is the `tmem` subtarget feature, checked against the byte at `unk_5BEBD51`. If either side fails the function emits `"128b atomics not supported on this architecture!"` (verbatim, exclamation point included) and returns a poison SDNode. The fingerprint is unusually clean: a single CMP+JL against `0xA0` followed by a CMP+JZ against the `tmem` feature byte, with no fallthrough to the MatcherTable. The patch is therefore trivially locatable in a stripped binary, and the diagnostic string is unique enough that grep over a binary dump lands directly on the function epilogue.

| Patch | Address / Site | Intrinsic ID(s) | Gate condition |
|---|---|---|---|
| cvt_packfloat 4-gate validator | `sub_1A84900` (2 066 B) | `8294`, `8437-8440`, `8627`, `9123`, `9531-9537` | SM major and PTX version floor, plus per-format arch-conditional sm_100a checks, plus family-conditional sm_100f check |
| FMAD FTZ split | case `0x66` of `SelectIntrinsic_W_Chain` | FMA path | SDNode flag bit `0x40` OR `"unsafe-fp-math"` function attribute selects FTZ opcode `0x65`; otherwise non-FTZ opcode `0xF7` |
| 128-bit atomic guard | `sub_1A80A40` | `9132` | `cc.major >= 0xA0 && hasFeature(80)` (`tmem` feature at `unk_5BEBD51`) |

```c
SDNode *handlePacketCvt(SDNode *n) {                                       /* patch 1 */
    uint8_t srcNib = n->intrId & 0xF, dstNib = (n->intrId >> 4) & 0xF;
    if (subtarget->major < 0x384 || subtarget->ptx < 0x4D)                 return error("...");
    if (isUE8M0x2(dstNib) && subtarget->major < 0xA0)                      return error("...");
    if (isFp6x2OrFp4x2(dstNib) && subtarget->major < 0xA0)                 return error("...");
    if (isFamilyCond(dstNib) && (subtarget->major < 0xA0 || subtarget->minor != 0xF))
                                                                           return error("...");
    return emitCvtPackFloat(n);
}

unsigned pickFmaOpcode(const Function *f, const SDNode *n) {              /* patch 2 */
    if (n->flags & 0x40)                                                  return 0x65;
    if (sub_3FC6800(f, "unsafe-fp-math", 0xE))                            return 0x65;
    return 0xF7;
}

SDNode *handle128bAtomic(SDNode *n) {                                     /* patch 3 */
    if (subtarget->major < 0xA0 || !subtarget->hasFeature(80))
        return error("128b atomics not supported on this architecture!");
    return emitTcgen05Atom128(n);
}
```

The three patches share a structural property worth calling out. Each sits at a single, well-defined dispatcher arm rather than scattering across the selector, and each returns a poison SDNode marked `IsErr` on failure rather than falling through to the MatcherTable. A reimplementation can drop these arms in or out independently without disturbing the rest of the selector, and a test suite can assert the exact diagnostic strings without worrying about ordering against unrelated cases. The `cvt_packfloat` validator reuses the same nibble-decode shape the case-`0x66` FMA selector uses for its flag-bit test, suggesting both patches were introduced through the same internal mechanism even though they live in different dispatcher layers. See [NVPTX Subtarget and Feature Matrix](nvptx-subtarget-and-feature-matrix.md) for the subtarget byte layout backing `cc.major`, `cc.minor`, and the `tmem` feature byte at `unk_5BEBD51`.

## Reimplementation Notes

The highest-risk compatibility points are:

- Preserve the fast-selector then MatcherTable fallback order.
- Treat unsupported architecture cases differently from ordinary non-matches: unsupported cases diagnose, ordinary non-matches fall back.
- Use saturating cost arithmetic in recursive matcher scoring.
- Keep tensor-memory selection behind explicit subtarget feature checks.
- Preserve `unsafe-fp-math` handling for FMA FTZ selection.
- Preserve diagnostic strings for target-gated intrinsics where tests depend on exact text.
- Decode or supply the AsmWriter mnemonic/register tables before emitting PTX.

## Appendix: NVPTXISD Opcode Map

Every `SDNode` carries a 16-bit `SDNode::NodeType` field whose numeric value selects between upstream LLVM `ISD::` opcodes and the NVPTX-private `NVPTXISD::` extensions. The three selectors in Tileiras — the `INTRINSIC_W_CHAIN` dispatcher, the load/store vector dispatcher, and the MatcherTable cost scorer — each consume a disjoint slice of this numeric space. Together they cover every opcode value the NVPTX backend can emit. The full upstream LLVM `ISD::*` enum names live in `llvm/include/llvm/CodeGen/ISDOpcodes.h`; the NVPTX-private additions live in `llvm/lib/Target/NVPTX/NVPTXISD.h`. Tileiras carries a fork of both headers, fingerprinted by the `LLVM21.0.0git` producer string.

### Dispatcher ranges

The three dispatchers split the opcode space cleanly. `SelectIntrinsic_W_Chain` at `sub_1A854E0` switches across `[0x17, 0x172]`, a 345-case window with 58 non-default bodies. `SelectLoadStoreVector` at `sub_1A874A0` uses two jump tables: a primary table at offset `0x1A874EF` covering `[158, 237]` with 80 cases (11 non-default), and a secondary at offset `0x1A87526` covering `[524, 567]` with 44 cases. The MatcherTable cost scorer at `sub_1AAFA40` consumes the remaining union `[0x01, 0x5A] ∪ [0x6D, 0xFD] ∪ [0x120, 0x17A]` — a 119-case dispatch that combines upstream `ISD::` opcodes with NVPTX-private opcodes inlined directly into the scorer.

| Dispatcher | Address | Numeric range | Cases |
|---|---|---|---|
| `SelectIntrinsic_W_Chain` | `sub_1A854E0` | `[0x17, 0x172]` | 345 (58 non-default) |
| `SelectLoadStoreVector` primary | `sub_1A874A0:0x1A874EF` | `[158, 237]` | 80 (11 non-default) |
| `SelectLoadStoreVector` secondary | `sub_1A874A0:0x1A87526` | `[524, 567]` | 44 |
| MatcherTable cost scorer | `sub_1AAFA40` | `[0x01, 0x5A] ∪ [0x6D, 0xFD] ∪ [0x120, 0x17A]` | 119 |

### Scalar branches outside the jump tables

Eight opcode values inside `SelectLoadStoreVector` are handled by isolated branches rather than by either jump table: `{58, 60, 98, 300, 301, -995, -5313, -5314}`. The first three are scalar parameter load/store opcodes the vector selector still has to recognise so it can route them to the scalar selector once `hasVecLDST` has decided against vectorisation. Opcodes `300` and `301` are the call-argument marshal and call-prototype emit opcodes. The three negative values are not arithmetic underflow: they are NVIDIA's post-LLVM-19 reservation slots for `LoadV2_Tmem` and `StoreV2_Tmem`, encoded as signed offsets from a private base so they cannot collide with upstream allocations.

### Named NVPTXISD opcodes

The following table samples opcodes that have explicit handlers in the three dispatchers. The notes column records what each handler keys on beyond the numeric opcode.

| Numeric | Name | Notes |
|---|---|---|
| `0x65` | `FMA_FTZ` | non-FTZ wrapper at case `0x66` of `INTRINSIC_W_CHAIN` |
| `0xF7` | `FMA` | non-FTZ form, gated by the `"unsafe-fp-math"` Function attribute |
| `58` | `LoadParam` | scalar param load; `hasVecLDST` gates whether the vector selector rejects |
| `60` | `StoreParam` | scalar param store; same gate |
| `98` | `StoreParamV2` | aligned-pair param store |
| `158` | `LoadV4Tmem` | NVPTX tensor-memory v4 load (address space `255`) |
| `160` | `LoadV4Tmem` (alt MVT) | TMEM v4 with alternate MVT operand |
| `188` | `StoreV4Tmem` | TMEM v4 store |
| `192` | `LoadV4Const` | constant-AS v4 load |
| `208` | `TMA_BULK_TENSOR_V4` | TMA bulk tensor v4 marshal |
| `210` | `TMA_BULK_TENSOR_V8` | TMA bulk tensor v8 marshal |
| `215` | `STV_U32_GLOBAL_V4` | global v4 u32 store |
| `216` | `STV_U64_GLOBAL_V4` | global v4 u64 store |
| `218` | `StoreRetval` | function return-value marshal |
| `236` | `LoadV4_Cluster` | cluster-shared v4 load |
| `300` | `CallArg` | call argument marshal (scalar branch) |
| `301` | `CallPrototype` | call prototype emit (scalar branch) |
| `524` | `STV_PRED_V2_TMEM_V8` | predicated v2 TMEM v8 store |
| `538` / `539` | `LoadV2` (alt) | alternate-encoded v2 load pair |
| `543` / `544` | `LoadV4` (alt) | alternate-encoded v4 load pair |
| `549`–`551` | `LoadV8` | v8 load family |
| `563` | `StoreV2` | v2 store |
| `565`–`567` | `StoreV8` | v8 store family |

### MatcherTable range opcodes

The MatcherTable cost scorer at `sub_1AAFA40` mixes upstream LLVM opcodes with NVPTX-private opcodes in the same numeric dispatch. Upstream values use their canonical `ISD::*` numbering and reach the scorer through the generic instruction-selection machinery. NVPTX-private values were inlined into the scorer so pattern cost calculations can fold target-specific knowledge without dispatching back into the generic layer.

| Numeric | Name | Notes |
|---|---|---|
| `0x01` | `ISD::LOAD` | upstream load matched by upstream patterns |
| `0x4A` | `ISD::STORE` | upstream store matched by upstream patterns |
| `0x65` | `NVPTXISD::FMA_FTZ` | inlined into cost scorer, same numeric as W_Chain case |
| `0x12C` | `NVPTXISD::WgmmaDescriptor` | wgmma operand marshal |
| `0x140` | `NVPTXISD::SETP_*` | predicate-set pattern family |
| `0x150` | `NVPTXISD::CallArg` | call-arg pattern family |
| `0x170` | `NVPTXISD::CallPrototype` | call-prototype pattern family |

### Reimplementation Invariants

- Keep the three dispatchers numerically disjoint; collisions between them produce silent miscompiles because the wrong handler will emit instructions for the wrong opcode shape.
- Preserve the scalar-branch list inside `SelectLoadStoreVector`; folding those opcodes back into the primary jump table changes their fallback behaviour when `hasVecLDST` is off.
- Treat the three negative opcode values as opaque reservation slots; do not renumber them without coordinating with the binary's `LoadV2_Tmem` / `StoreV2_Tmem` pattern entries.
- When extending the MatcherTable scorer, inline NVPTX-private opcodes rather than dispatching out, so cost arithmetic stays inside the saturating scorer.
