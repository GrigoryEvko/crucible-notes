# InstCombine Peephole Passes (`NeuronHloInstCombine`)

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). They were read from the `neuronxcc/starfish/bin/hlo-opt` ELF, where VA == file-offset for `.text`/`.rodata`. Other wheels differ; treat every address as version-pinned.*

## Abstract

`NeuronHloInstCombine` is the HLO-level peephole combiner — pass `#62`, registry key `neuron-hlo-inst-comb`, class `xla::hilo::NeuronHloInstCombine`. It is the closest the Neuron HLO stack comes to a classic LLVM `InstCombine`: a single linear scan of every instruction in every computation, trying a small fixed set of structural rewrites at each one and tallying how many fired. Unlike a general combiner it carries **only two real patterns**, both narrow and both aimed at the same problem — graph shapes that XLA's framework lowering leaves behind when it expresses a tiled/strided operation as a fan of `slice`s feeding either an add-tree or a `concatenate`. The pass collapses each fan back into a single reshape-plus-(reduce|broadcast).

The two patterns live inside **one** `Run` body (`xla::hilo::NeuronHloInstCombine::Run` @ `0x1f55000`, 3891 B). `Run` does not delegate to a worklist or a matcher registry; it open-codes the per-instruction dispatch:

1. **Cascaded `slice→add` → strided-slice reduce.** `matchAndReplaceCascadedSliceAddPattern` (@ `0x1f53190`, 7670 B) recognises a root `slice` whose operand is an `add`-tree of `slice`s that together tile axis 0 contiguously, and rewrites the whole cascade to `reduce_add(reshape(source, {N, K}), dim={0})` over a freshly synthesized scalar-add computation named **`strided_slice_reduce`**.
2. **3-D `slice→concat` → `reshape(broadcast(constant))`.** `matchAndReplaceSliceConcatPattern3D` (@ `0x1f52810` `[clone .constprop.0]`, 1908 B) recognises a wide `concatenate` whose operands are `slice`s of a single broadcast-of-a-1-D-constant, and rebuilds it as `reshape(broadcast(broadcast(K)), concat.shape())`.

Both matchers return `bool` (changed?); `Run` increments a per-pattern counter on each `true` and, at the end of every computation, prints `"Optimized " <count> " of sliceAddPattern"` / `" of sliceConcatPattern"`. This page reconstructs both matchers, the contiguous-tiling legality test that gates the slice-add fusion, and the `Run` driver that hosts them.

| | |
|---|---|
| **Pass** | `#62` · key `neuron-hlo-inst-comb` (len 20) · class `xla::hilo::NeuronHloInstCombine` |
| **Run / name / factory** | Run @ `0x1f55000` · `name()` @ `0x1f4eda0` · factory `0x1e708e0` |
| **Source file (verbatim)** | `"hilo/hlo_passes/NeuronHloInstCombine.cc"` @ `.rodata 0x2e47f0` |
| **Pattern 1** | `matchAndReplaceCascadedSliceAddPattern` @ `0x1f53190` → `strided_slice_reduce` |
| **Pattern 2** | `matchAndReplaceSliceConcatPattern3D` @ `0x1f52810` `.constprop.0` |
| **Dispatch (in `Run`)** | P1 on every inst @ `0x1f55172`; P2 only on **non-`concatenate`** roots, gated @ `0x1f551d0`/called @ `0x1f551db` |
| **Opcode encoding** | opcode byte @ `HloInstruction+0x14`; operand count encoded `2*N` @ `+0x18` |
| **Tally strings** | `"Optimized "` @ `0x23847f` · `" of sliceAddPattern"` @ `0x243df3` · `" of sliceConcatPattern"` @ `0x25b007` |

> **NOTE —** The MLIR-dialect *twin* of this pass (`NeuronInstCombine`, operating on `mhlo` ops rather than HLO) is documented in **[4.36 — NeuronInstCombine (MLIR)](neuron-instcombine-mlir.md)**. The two are independently registered, run on different IRs, and share no code; do not conflate them. The concat-side rewrites here are adjacent to but distinct from the **[4.18 — Concatenation Optimizations](concat-optimizations.md)** family, which handles same-source slice→concat collapse via `GetOriginalSource` (see [§5](#5-the-run-driver-and-the-third-inline-rewrite)).

---

## 1. Reimplementation contract

A faithful reimplementation must, in one HLO pass, for each instruction `I` in document order within each computation:

1. **Try the slice-add fusion first** (`I` is the root). If it fires, count it and advance to the next instruction (the instruction vector may have been mutated, so re-anchor before continuing — see [§5](#5-the-run-driver-and-the-third-inline-rewrite)).
2. **Otherwise**, if `I` is *not* a `concatenate`, try the 3-D slice-concat rewrite. (The gate is inverted: P2's own root guard *requires* `concatenate`, yet `Run` only calls it on non-`concatenate` roots — see the [GOTCHA](#gotcha-the-inverted-concat-gate) below. The reimplementer must reproduce this exactly to match firing behaviour.)

The two rewrites are otherwise independent. The pass owns no cost model beyond the structural legality guards: once a slice-add chain fully tiles axis 0 with no gaps, or once a slice-concat matches and conserves element count, the rewrite fires unconditionally. The opcode dictionary, operand-list layout, and the XLA `match::` DSL evaluators below are the only shared infrastructure.

### Opcode dictionary (anchored to `HloOpcodeString` @ `0x96bb550`, 123-case jump table)

`HloInstruction` stores its opcode as a single byte at object offset `+0x14`; the operand list begins at `+0x40` and the operand **count** is encoded as `2*N` at `+0x18` (decode with `shr ,1`). The parent `HloComputation*` sits at `+0x48`.

| dec / hex | opcode | role in this pass |
|---|---|---|
| `1` | `add` (`kAdd`) | inner `match::Add` operand; rebuild `CreateBinary(…, kAdd, …)` in `strided_slice_reduce` |
| `21 / 0x15` | `broadcast` | P2 source pattern; `SkipNoOp` peels iff element-count preserved |
| `34 / 0x22` | `concatenate` | P2 root opcode; `Run` dispatch gate `[+0x14]==0x22` |
| `36 / 0x24` | `constant` | P2 leaf (1-D constant under the broadcast) |
| `91 / 0x5B` | `reshape` | `SkipNoOp` peels unconditionally; CreateReshape rebuild target |
| `110 / 0x6E` | `slice` | **root of P1** and every chain/inner leaf in both patterns |

> **CORRECTION —** `0x15` is `broadcast` and `0x5B` is `reshape`, not the reverse. A naive read of `SkipNoOpReshapesAndBroadcasts` (which touches both) can transpose them; the authoritative mapping is the `HloOpcodeString` switch slots above. This matters because `SkipNoOp` peels `reshape` (0x5B) *always* but `broadcast` (0x15) *only* when the element count is unchanged.

---

## 2. Pattern 1 — cascaded `slice → add` → strided-slice reduce

`matchAndReplaceCascadedSliceAddPattern(HloModule*, HloInstruction*)` @ `0x1f53190`. The candidate root arrives in `rsi`; the module in `rdi`. Returns `bool` in `r14b` (`true` ⇒ fused).

```c
// 0x1f53190 — matchAndReplaceCascadedSliceAddPattern
bool matchAndReplaceCascadedSliceAddPattern(HloModule* module, HloInstruction* inst) {

  // (A) ROOT GUARD — 0x1f531c2: 5-insn fast reject of any non-slice root.
  if (inst->opcode /*[inst+0x14]*/ != kSlice /*0x6E*/)        // cmp byte[rsi+14h],6Eh
      return false;

  // (B) STRUCTURAL MATCH — 0x1f532b6 .. 0x1f535b8.
  //   Two stack-built xla::match patterns evaluated by HloInstructionPattern::Match.
  //   P_root = Slice( Add( Slice(_), Slice(_) ) )    // opcodes 0x6E ⟶ kAdd=1 ⟶ 0x6E,0x6E
  //   The match::Add<> sub-pattern (built @0x1f4fb00) pins the kAdd operand opcode (0x1f535be: ecx=0x6E).
  if (!Match(inst, P_root))            return false;          // 0x1f53588
  if (!Match(inst, P_root /*2nd arm*/))return false;          // 0x1f535af (re-validates AllOf arm)

  // (C) CHAIN COLLECTION — inlined chainLinkSliceAddPattern; 0x1f53c1e .. 0x1f548d8.
  //   DFS over the Add-tree; every kSlice leaf is pushed into slices_in_chain.
  //   _M_realloc_insert pushes TWO entries per add node (lhs & rhs) at 0x1f548f8/0x1f5491d.
  std::vector<HloInstruction*> slices_in_chain;
  chainLinkSliceAddPattern(/*root*/inst, /*cursor*/inst, slices_in_chain);   // §3

  // (D) ORDER leaves ascending by axis-0 start offset — 0x1f54933 .. 0x1f549b3.
  std::sort(slices_in_chain.begin(), slices_in_chain.end(),
            [](const HloInstruction* a, const HloInstruction* b) {
                return a->slice_starts(0) < b->slice_starts(0);  // lambda #1, §3
            });
  //   libstdc++ introsort: __introsort_loop @0x1f4f240 → __insertion_sort @0x1f4eee0
  //   (tail __unguarded_linear_insert @0x1f4ee80) → heapsort fallback __adjust_heap @0x1f4ef90.

  // (E) CONTIGUOUS-TILING LEGALITY — 0x1f549b5 .. 0x1f54c0d. See §4.
  if (!IsContiguousFullTiling(slices_in_chain, source))   // gap-free + starts at 0 + covers dim 0
      return false;                                       // (returns the r14 set by earlier guards)

  // (F) REBUILD — 0x1f53d6b .. 0x1f54343. See §4. Replace Slice(Add(…)) cascade with
  //     reduce_add( reshape(source, {N,K}), init=0, dims={0}, "strided_slice_reduce" ).
  return /*fused*/ true;
}
```

> **GOTCHA —** The root is **`slice`** (0x6E), *not* `add`. The first five instructions reject any non-slice. The cascade is detected from the *outermost* slice inward; the `match::Add` sub-pattern only constrains that slice's single operand. A reimplementer who anchors the pattern on the `add` will never enter the matcher.

### Net effect

A height-`N` cascade `Σᵢ sliceᵢ(source)` — where each `sliceᵢ` is a contiguous, gap-free axis-0 window and the windows together tile the whole dimension — collapses to a single `reduce(reshape(source, {N, K}), add, dim 0)`: one reshape + one reduce in place of `N` slices and `N−1` adds. The synthesized reduction body is the named computation `strided_slice_reduce`, a scalar `p0 + p1`. The "strided" in the name reflects that the `{N, K}` reshape re-expresses the strided windowed sum as a contiguous reduce over the new leading axis.

---

## 3. Chain collection and the sort comparator

`chainLinkSliceAddPattern(HloInstruction*, HloInstruction*)` is a real `anonymous namespace` function but is **fully inlined** into `0x1f53190` — it has no standalone symbol. Its existence is witnessed by the demangled name of the sort-comparator lambda it owns (recovered verbatim from the `__unguarded_linear_insert` instantiation @ `0x1f4ee80`):

```
…chainLinkSliceAddPattern(HloInstruction*, HloInstruction*)::{lambda(HloInstruction const*, HloInstruction const*)#1}
```

The body is the match-DSL descent at `0x1f53c1e–0x1f548d8`:

```c
// inlined chainLinkSliceAddPattern — DFS chain collector
void chainLinkSliceAddPattern(HloInstruction* root, HloInstruction* cur,
                              std::vector<HloInstruction*>& chain) {
  // cur is a Slice whose operand is an Add (already verified by P_root).
  Add* add = cur->operand(0);
  for (arm in { add->operand(0), add->operand(1) }) {           // lhs, rhs
    if (Match(arm, Slice(_)))            chain.push_back(arm);   // leaf → record (0x1f548f8)
    else if (Match(arm, Add(Slice,Slice)) || Match(arm, Slice(Add(...))))
                                         recurse(arm);           // descend (branch @0x1f54720)
    else                                 reject();               // non-conforming arm ends/kills the chain
  }
  // Result: chain = every kSlice leaf feeding the Add-tree, in match order (pre-sort, arbitrary).
}
```

One leaf arm carries a rank constraint (`ShapePatternRankImpl`, a `match::Shape().WithRank(...)` guard) in the `0x1f546xx` region; the exact required rank value was not extracted from the on-stack pattern struct.

### Comparator — the cleanest witness (`__unguarded_linear_insert` @ `0x1f4ee80`)

```asm
0x1f4ee94  r13 = *iter                  ; value being inserted
0x1f4eea7  r15 = *(iter-1)              ; sorted-prefix predecessor
0x1f4eead  slice_starts(r13, 0) -> r12  ; val->slice_starts(0)
0x1f4eec4  slice_starts(r15, 0) -> rax  ; arr[j]->slice_starts(0)
0x1f4eec9  cmp r12, rax
0x1f4eecc  jl  ...                      ; shift while val < arr[j]
```

The comparator is strictly `a->slice_starts(0) < b->slice_starts(0)` — an ascending sort by the start offset of **slice dimension 0**. The `__insertion_sort` (`0x1f4eee0`) and `__adjust_heap` (`0x1f4ef90`) instantiations call `slice_starts(0)` on both operands with the same polarity. There is no secondary key; ties keep introsort's (unstable) order. **Axis 0 is constant** — every `slice_starts`/`slice_limits` call in the matcher passes `esi = 0`; there is no dynamically chosen slice axis.

---

## 4. The contiguous-tiling legality test (the fusion's only guard)

This is the gate that makes the rewrite sound: the chain of slices must *exactly* tile axis 0 — gap-free, starting at 0, and covering the full extent. Reconstructed from `0x1f549b5–0x1f54c0d`:

```c
// (E) contiguous-tiling legality — emits NEURON_LOG "First : ", "Last  : ", "IsContiguous = ".
bool IsContiguousFullTiling(const std::vector<HloInstruction*>& slices, HloInstruction* source) {
  const size_t N = slices.size();
  long firstStart = slices.front()->slice_starts(0);            // [rbp-0AE0h]

  for (size_t i = 0; i + 1 < N; ++i)                            // 0x1f549f0 loop
      if (slices[i]->slice_limits(0) != slices[i+1]->slice_starts(0))  // no gap / no overlap
          return false;                                         // goto loc_1F54D4A (bail)

  bool ok = (firstStart == 0);                                  // setz: chain must begin at offset 0
  if (ok) {                                                     // 0x1f54bd3
      long lastLimit = slices.back()->slice_limits(0);
      ok = (lastLimit == source->shape().dimensions[0]);        // full coverage of axis 0
  }
  return ok;                                                    // only a full, gap-free tiling fuses
}
```

Three conditions, all on axis 0: **(1)** adjacent slices abut (`limit[i] == start[i+1]`), **(2)** the first slice starts at `0`, **(3)** the last slice's limit equals the source's dim-0 extent. A strict sub-chain — one that tiles only part of the axis — is rejected by conditions (2)/(3). There is no size or count threshold: contiguity *is* the profitability test.

### Rebuild — `Slice(Add(…))` ⟶ `reduce_add(reshape(source,{N,K}),{0})`

Callee sequence, verbatim and in order (all addresses confirmed in the `0x1f53190` disasm):

```c
// (a) reshape source to add a leading "chain" dimension — 0x1f53e1c .. 0x1f53f06.
long K        = source0->shape().dimensions[0];                 // per-slice width  (r12)
long origDim0 = operand->shape().dimensions[0];                 // full sliced extent
long N        = origDim0 / K;                                   // idiv @0x1f53e73 → chain length
PrimitiveType et = root->shape().element_type();                // ebx
Shape rshape  = ShapeUtil::MakeValidatedShape(et, {N, K});      // ecx=2 (rank)   @0x1f53e84
auto  reshape = HloInstruction::CreateReshape(rshape, source, /*inferred_dim=*/-1); // @0x1f53eec (rcx=-1)
auto  new_reshape = comp->AddInstruction(reshape);              // @0x1f53f06; NEURON_LOG "new_reshape = "

// (b) build the scalar-add reduction body "strided_slice_reduce" — 0x1f53d6f .. 0x1f5418c.
HloComputation::Builder b("strided_slice_reduce");              // name str @0x26a75a / loaded @0x1f53d74
Shape scalar = ShapeUtil::MakeValidatedShape(et, {});           // rank-0          @0x1f53fd9
auto  p0  = b.AddInstruction(HloInstruction::CreateParameter(0, scalar, "p0"));  // @0x1f5403f
auto  p1  = b.AddInstruction(HloInstruction::CreateParameter(1, scalar, "p1"));  // @0x1f540b0
auto  sum = b.AddInstruction(HloInstruction::CreateBinary(scalar, kAdd /*1*/, p0, p1)); // @0x1f54124
auto  add_comp = module->AddEmbeddedComputation(b.Build(sum));  // @0x1f5418c

// (c) init = constant 0 of the element type — 0x1f541b9 .. 0x1f54202.
Literal zero = LiteralUtil::Zero(et);                           // @0x1f541c2
auto  init  = comp->AddInstruction(HloInstruction::CreateConstant(std::move(zero)));  // @0x1f541e8

// (d) reduce over the new leading dim ⇒ {K} — 0x1f54222 .. 0x1f542ec.
Shape outShape = ShapeUtil::DeleteDimension(/*dim=*/0, new_reshape->shape());     // @0x1f54298
auto  reduce   = HloInstruction::CreateReduce(outShape, new_reshape, init, /*dims=*/{0}, add_comp); // @0x1f542d0
auto  new_root = comp->AddInstruction(reduce);                                    // @0x1f542ec

// (e) splice in — 0x1f54319.
root->ReplaceAllUsesWith(new_root);                                               // @0x1f54319
```

> **QUIRK —** `N` is derived by integer division `origDim0 / K` (`idiv` @ `0x1f53e73`), not from `slices_in_chain.size()`. The contiguity guard guarantees the two agree (the chain tiles the axis with equal-width windows), so the division is the rebuild's source of truth for the reshape's leading dimension.

---

## 5. Pattern 2 — 3-D `slice → concat` → `reshape(broadcast(constant))`

`matchAndReplaceSliceConcatPattern3D(HloModule*, HloInstruction*) [clone .constprop.0]` @ `0x1f52810`. The concat candidate arrives in `rdi` (then `r12`); the module is effectively unused beyond reaching the parent computation. The "3D" names the *concat output* rank — the matched constant is 1-D and the rebuilt broadcast adds the concat-count axis.

```c
// 0x1f52810 — matchAndReplaceSliceConcatPattern3D
bool matchAndReplaceSliceConcatPattern3D(HloModule* /*m*/, HloInstruction* concat) {

  // (A) ROOT GUARD — 0x1f52833 .. 0x1f52839.
  if (concat->opcode != kConcatenate /*0x22*/)         return false;  // cmp byte[rdi+14h],22h
  if (concat->raw_operand_field /*[rdi+18h] = 2*N*/ <= 3) return false;// cmp qword[rdi+18h],3  → "wide" concat

  // (B) FIRST-HALF OPERAND-IDENTITY GUARD — 0x1f5284a .. 0x1f5287f.
  HloInstruction* op0 = concat->mutable_operand(0);                   // r13
  for (long b = 1; b < N/2; ++b)                                      // N = [r12+18h]>>1
      if (concat->operand(b) != op0)                  return false;   // operands 1..N/2 must all be op0

  // (C) OPERAND(0) IS ITSELF A CONCAT — 0x1f528b0 .. 0x1f528be.
  if (op0->opcode != kConcatenate /*0x22*/)            return false;  // cmp byte[r13+14h],22h
  if (op0->operand_count == 0)                         return false;  // [r13+18h]>>1 != 0

  // (D) INNER-CONCAT OPERANDS ARE slice(...) WITH ONE SHARED SOURCE — 0x1f528c0 .. 0x1f52b5b.
  //   P_slice = Slice( Slice(_) )  [opcodes 0x6E,0x6E with a 0x5B secondary leaf — see GAP]
  if (!Match(op0->operand(0), P_slice))                return false;  // 0x1f529de → Match @0x1f50ae0
  HloInstruction* src = SkipNoOpReshapesAndBroadcasts(captured0);     // 0x1f529f2 → @0x1f4f130
  for (long k = 1; k < op0->operand_count/2; ++k) {
      if (!Match(op0->operand(k), P_slice))            return false;  // 0x1f52b4e → @0x1f50ae0
      if (SkipNoOpReshapesAndBroadcasts(capturedK) != src) return false; // 0x1f52a27 — same source
  }

  // (E) ELEMENT-COUNT LEGALITY — 0x1f52b60 .. 0x1f52b8a.
  if (ExtentProduct(op0->shape()).first != ExtentProduct(src->shape()).first)
      return false;                                    // 0x1f52b6b / 0x1f52b82 → @0x1f4fd40
      // inner-concat total element count == shared source's: the slices+concat are a no-op reshuffle.

  // (F) SOURCE IS broadcast(constant), constant is 1-D — 0x1f52b90 .. 0x1f52c64.
  if (!P_bcast.Match(src))                             return false;  // 0x1f52bde → @0x1f50200 (opcode 0x15)
  HloInstruction* konst = SkipNoOpReshapesAndBroadcasts(src->operand(0)); // 0x1f52c16 → @0x1f4f130
  if (!Match(konst, Pattern<Opcode==kConstant /*0x24*/>)) return false;   // 0x1f52c43 → @0x1f4fdc0
  if (konst->shape().dimensions().size() != 1)         return false;  // 0x1f52c5b: rank must be 1

  // ===== REWRITE — 0x1f52c6a .. 0x1f52f1f =====
  HloComputation* comp = concat->parent_computation;   // r15 = [r12+0x48]

  // (R1) re-materialise broadcast(constant) fresh in comp (empty name).
  auto bdims = src->dimensions();                                     // vcall @0x1f52c79
  auto b0 = comp->AddInstruction(
        HloInstruction::CreateBroadcast(src->shape(), konst, bdims)); // 0x1f52ca1 / add 0x1f52cb7

  // (R2) intermediate shape  shape2 = MakeShape(elemTy, [N, constDim…]); broadcast-dims = [1..constRank].
  std::vector<long> newdims = { N };                                 // first elem = concat count N
  std::vector<long> bcast_dims;
  for (long i = 0; i < konst->shape().rank; ++i) {                   // konst is 1-D ⇒ one iter
      newdims.push_back(konst->shape().dimensions()[i]);             //   newdims = [N, constDim]
      bcast_dims.push_back(i + 1);                                   // 0x1f52ddb → [1]
  }
  Shape shape2 = ShapeUtil::MakeShape(b0->shape().element_type(), newdims); // 0x1f52e31 → @0x1f07a60

  // (R3) broadcast b0 up to shape2 along bcast_dims (adds the concat-count axis 0).
  auto b1 = comp->AddInstruction(
        HloInstruction::CreateBroadcast(shape2, b0, bcast_dims));    // 0x1f52e60 / add 0x1f52e76

  // (R4) reshape b1 to the original concat output shape (3-D).
  auto r = comp->AddInstruction(
        HloInstruction::CreateReshape(concat->shape(), b1, /*inferred=*/-1)); // 0x1f52ea3 / add 0x1f52eb9

  // (R5) splice in — 0x1f52edd.
  Status s = comp->ReplaceInstruction(concat, r);                    // @0x1f52edd
  return s.ok();                                                     // 0x1f52ee2 setz → al
}
```

> **GOTCHA — the inverted concat gate.** P2's own root guard *requires* `concatenate` (`cmp byte[rdi+14h],22h` @ `0x1f52833`), yet `Run` only calls P2 when the instruction is **not** a `concatenate` (`cmp byte[r12+14h],22h` @ `0x1f551d0`, with the call to P2 @ `0x1f551db` reached on the non-equal path). Read literally this would make P2 unreachable. The resolution is that `Run`'s gate tests the *current loop instruction*, while P2 re-validates the same pointer — i.e. P2 is *also* invoked from the inline same-source path on roots that are not themselves concats but whose operand chains contain one. Treat the two `0x22` tests as belonging to two different dispatch sites; a reimplementation must reproduce both rather than "optimise away" the apparent contradiction. *(Confidence: the two compares and the call edge are CONFIRMED in disasm; the precise reconciliation of which pointer each guards is INFERRED — MED.)*

### Net effect

Input (matched): `concat3D( S, S, …, op0=concat( slice(BC), slice(BC), … ), … )`, where every inner `slice` peels (through no-op reshapes/broadcasts) to the **same** `BC = broadcast(K)`, `K` a 1-D `constant`, with total element counts conserved. Output: `reshape( broadcast₂( broadcast(K) ), concat.shape() )`. The `N` slices and the inner concat are deleted; the constant is materialised once and shaped directly. All new instructions are added with an **empty** name `string_view`.

---

## 6. The `Run` driver and the third inline rewrite

`xla::hilo::NeuronHloInstCombine::Run` @ `0x1f55000` is the single host for both matchers. It scans each computation's instruction list, applies the rewrites in a fixed order, and tallies counters it prints per-computation.

```c
// 0x1f55000 — NeuronHloInstCombine::Run
StatusOr<bool> Run(HloModule* m, const flat_hash_set<string_view>& /*threads*/) {
  size_t sliceAddCount = 0, sliceConcatCount = 0;
  for (HloComputation* comp : m->computations()) {
    int n = (comp.instr_end - comp.instr_begin) >> 4;            // 0x10-byte vector entries
    for (int i = 0; i < n; ++i) {
      HloInstruction* inst = comp->instructions()[i];

      if (matchAndReplaceCascadedSliceAddPattern(m, inst)) {      // @0x1f55172
          ++sliceAddCount;                                       // @0x1f5517b
          /* re-fetch instr_begin (the rewrite may have reallocated the vector) and continue */
          continue;
      }
      if (inst->opcode != kConcatenate /*0x22*/) {               // @0x1f551d0 gate
          if (matchAndReplaceSliceConcatPattern3D(m, inst))      // @0x1f551db
              { ++sliceConcatCount; continue; }
      }
      // (inline) same-source slice→concat collapse on concat roots with >3 operands,
      //   all tracing to one GetOriginalSource(@0x1f50dc0) — a SEPARATE rewrite that also
      //   prints " of sliceConcatPattern" (its own counter). Belongs to 4.18; see NOTE.
    }
    // INFO logs (string-pool): "Optimized " <sliceConcatCount> " of sliceConcatPattern"
    //                          "Optimized " <sliceAddCount>    " of sliceAddPattern"  (@0x1f55b2e)
  }
  return (sliceAddCount + sliceConcatCount) > 0;                 // changed?
}
```

> **NOTE — two counters, one label.** `Run` maintains *two* counters that both print `" of sliceConcatPattern"`: one for `matchAndReplaceSliceConcatPattern3D` (this page) and one for the inline `GetOriginalSource`-based same-source collapse (documented in [4.18](concat-optimizations.md)). The `" of sliceConcatPattern"` string (`0x25b007`) is loaded at four sites in `Run` (`0x1f55bdc`, `0x1f55bf8`, `0x1f55c8f`, `0x1f55ca6`); a count printed by this pass may be the *sum* of two distinct rewrites. The `" of sliceAddPattern"` string (`0x243df3`) is loaded only at the slice-add sites (`0x1f55b2e`, `0x1f55b45`).

`GetOriginalSource` (`0x1f50dc0`) and `SkipNoOpReshapesAndBroadcasts` (`0x1f4f130`) are invoked from `Run`/P2, **not** from the slice-add matcher.

---

## 7. The XLA `match::` DSL evaluators (shared)

Both matchers express their structural predicates as stack-built `xla::match::detail` pattern objects evaluated by a handful of template instantiations. The opcode immediates are written into the on-stack pattern struct by the caller and read by the leaf `HloInstructionPatternOpcodeImpl::Match`.

| Addr | Demangled role | Used by | DSL meaning | Conf |
|---|---|---|---|---|
| `0x1f50ae0` | `Match<HloInstructionPattern<AllOf<Base,Opcode,Operand<AllOf<Base,Opcode,Operand<Base>>>>>>` | P2 (D) | `Slice( Slice(_) )` — 2-level opcode+operand chain | HIGH |
| `0x1f50200` | `HloInstructionPattern<AllOf<Base,Opcode,Operand<Base>>>::Match` | P2 (F) | `broadcast(*)` member matcher (op 0x15) | HIGH |
| `0x1f4fdc0` | `Match<HloInstructionPattern<AllOf<Base,Opcode>>>` | P2 (F) | opcode-only → `constant` (0x24) | CONFIRMED |
| `0x1f4f130` | `SkipNoOpReshapesAndBroadcasts(HloInstruction*)` | P2, `Run` | peel `reshape` (always) + no-op `broadcast` (iff count-preserving) | CONFIRMED |
| `0x1f4fd40` | `ShapeUtil::ExtentProduct<false>(Shape&) → pair<long,bool>` | P2 (E) | Π(dims) element count (+overflow flag, ignored) | CONFIRMED |
| `0x1f07a60` | `ShapeUtil::MakeShape(PrimitiveType, Span<long>)` | P2 (R2) | rebuild intermediate shape | CONFIRMED |
| `0x1f4fb00` | `match::Add<…>` builder (kAdd=1) | P1 (B) | pins the inner add operand opcode | CONFIRMED |
| `0x1f4ee80` | `__unguarded_linear_insert<…lambda#1>` | P1 (D) | comparator witness: ascending `slice_starts(0)` | CONFIRMED |

> **CORRECTION —** Earlier survey notes named P2's matchers as `0x1f50800` / `0x1f50f40` / `0x1f4fca0`. The `0x1f52810` body references **none** of those three (grep count 0); the matchers it actually calls are the five rows above plus `SkipNoOp`. The `0x1f508xx`/`0x1f50f40` addresses are deeper-nested `Match<…>` template siblings, and `xla::match::Broadcast<…>` @ `0x1f4fca0` is a pattern-builder helper belonging to the slice-add path's `match::Add` and the inline same-source rewrite.

---

## 8. Confidence, gaps, and verbatim evidence

**Adversarial re-verification of the five strongest claims** (each re-challenged against the `0x1f55000` cluster disasm):

1. **`Run` hosts both matchers** — CONFIRMED. `call …matchAndReplaceCascadedSliceAddPattern` @ `0x1f55172`; `call …matchAndReplaceSliceConcatPattern3D[.constprop.0]` @ `0x1f551db`.
2. **P1 root = `slice` (0x6E)** — CONFIRMED. `cmp byte[rsi+14h],6Eh` @ `0x1f531c2` (5-insn fast reject).
3. **P2 root = `concatenate` (0x22), wide gate** — CONFIRMED. `cmp byte[rdi+14h],22h` @ `0x1f52833`; `cmp qword[rdi+18h],3` @ `0x1f52839`.
4. **P1 rebuild = `strided_slice_reduce` synth** — CONFIRMED. `strided_slice_reduce` @ `0x26a75a` loaded @ `0x1f53d74`; `CreateReshape`@`0x1f53eec` → `CreateBinary(kAdd)`@`0x1f54124` → `AddEmbeddedComputation`@`0x1f5418c` → `DeleteDimension`@`0x1f54298` → `CreateReduce`@`0x1f542d0` → `ReplaceAllUsesWith`@`0x1f54319`.
5. **P2 rebuild = 2×`CreateBroadcast` + `MakeShape` + `CreateReshape` + `ReplaceInstruction`** — CONFIRMED. `CreateBroadcast`@`0x1f52ca1`,`0x1f52e60`; `MakeShape`@`0x1f52e31`; `CreateReshape`@`0x1f52ea3`; `ReplaceInstruction`@`0x1f52edd`.

**Gaps / lower confidence:**

- **P2 operand-count gate (raw form).** The instruction at `0x1f52839` compares the *encoded* `2*N` field (`[rdi+18h]`) against `3`, so the literal test is `2*N <= 3`, i.e. it rejects `N == 1`. The matcher then iterates `b ∈ [1, N/2)` (post-`shr` count). The clean reading "needs a wide concat" holds, but the exact threshold semantics of the raw `<= 3` on the doubled field vs. the post-shift `N/2` loop bound are reconstructed, not byte-proven beyond the two compares. **INFERRED — MED.**
- **`P_slice` nesting.** The three opcode immediates emitted into P2's on-stack slice pattern (`0x6E`, `0x6E`, `0x5B` at `0x1f528c7`/`0x1f528e8`/`0x1f528ee`) map to `Slice→Slice→…` with `0x5B` (reshape) as a secondary leaf, but the precise AllOf/Operand field binding is not byte-proven against the opaque struct layout. Outer opcode = `slice` is CONFIRMED; nesting is **INFERRED — MED.**
- **`CreateReshape` 3rd arg.** Passed as `-1` (`or rcx,-1` @ P1 `0x1f53ed7`, `rcx=-1` @ P2 `0x1f52ea3`); read as "no inferred-dimension index" per XLA convention. **CONFIRMED (value) / INFERRED (semantics).**
- **Bodies are disasm-only.** Every Hex-Rays body in this cluster is an `NVOPEN_IDA_SKIP_DECOMPILE` stub; all pseudocode is disassembly-derived. Control-flow confidence MED-HIGH (single-caller, linear match→rewrite).

**Verbatim string evidence (addresses confirmed in `_strings.json`):**

| String | Addr | Role |
|---|---|---|
| `"hilo/hlo_passes/NeuronHloInstCombine.cc"` | `0x2e47f0` | `NEURON_LOG` source file |
| `"strided_slice_reduce"` | `0x26a75a` | synthesized reduce computation name |
| `"chainLinkSliceAddPattern(inst) = "` | `0x2fbb58` | DEBUG cursor dump |
| `"chainLinkSliceAddPattern(rootInst) = "` | `0x3063d8` | DEBUG chain-root dump |
| `"slices_in_chain.size() = "` | `0x27e7e3` | DEBUG chain length |
| `"First : "` / `"IsContiguous = "` | `0x21cd17` / `0x266c9d` | legality block |
| `"new_reshape = "` | `0x243de4` | rebuilt-reshape dump |
| `"Optimized "` | `0x23847f` | tally prefix |
| `" of sliceAddPattern"` / `" of sliceConcatPattern"` | `0x243df3` / `0x25b007` | per-pattern tally |

---

## See also

- **[4.18 — Concatenation Optimizations](concat-optimizations.md)** — the `GetOriginalSource`-based same-source slice→concat collapse that shares `Run`'s `" of sliceConcatPattern"` label.
- **[4.37 — NeuronInstCombine (MLIR)](../mlir/inst-combine.md)** — the independently-registered Penguin/Tensorizer-dialect peephole combiner; the MLIR-level twin of this HLO pass.
- **[The hlo-opt Pass Registry](pass-registry.md)** — pass `#62` `neuron-hlo-inst-comb` in the `--passes` table.
