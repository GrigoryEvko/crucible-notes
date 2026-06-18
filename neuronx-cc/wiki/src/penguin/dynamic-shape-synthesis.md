# Dynamic-Shape System Synthesis — the End-to-End Thread

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core). The front half (HLO/MHLO dynamic passes) lives in the native tools `hlo2penguin` / `hlo-opt`; the runtime-value primitives `bir::BirIntRuntimeValue`, `bir::InstDynamicForLoop`, `bir::DynamicForLoopAxis`, the `bir::Hwm` latency stubs, and `pelican::AffineIdx` live in `libBIR.so` and are exercised by the simulator binary `nki_klr_sim`; the AP-lowering, DGE-routing, and descriptor passes live in `neuronxcc/starfish/lib/libwalrus.so` (`.text` base `0x62d660`, `.rodata` base `0x1c72000`, VA == file-offset); the ADDR4 encoder also lives in `libwalrus.so`. Other wheels differ; treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*
>
> *Provenance: `libwalrus.so` and `libBIR.so` are present in the corpus — as per-symbol decompiled/disasm sidecars and as full IDA databases under `ida/`. The `nm -DC` body addresses (`assembleDynamicInfo @0xf20230`, `rewireDynamicAPRegisters @0xef91d0`, …) are the `libwalrus.so`-proper VA frame recovered from the `ida/…libwalrus.so/` database; the per-symbol sidecars carry the same bodies in a distinct internal VA frame. The cited bodies are disassembled, not merely declared — the addresses are CONFIRMED. The same provenance holds on [Symbolic-AP Register-ALU](symbolic-ap-register-alu.md), [DGE Level Selection](dge-level-dynamic-dma.md), [the Dynamic For-Loop](dynamic-for-loop.md), and [Backend Dependence-Distance](backend-dependence-distance.md).*

## Abstract

This is the **capstone of the dynamic-shape thread**. The four preceding Part-5 pages each reverse one organ of the system in isolation: [5.23 DGE level selection](dge-level-dynamic-dma.md) (the route predicate and the three `dynamic_dma_*` passes), [5.24 TensorCopyDynamic generators](tensorcopy-dynamic-generators.md) (the runtime-offset IR nodes), [5.25 symbolic-AP register-ALU](symbolic-ap-register-alu.md) (the address arithmetic), and [5.26 the dynamic for-loop](dynamic-for-loop.md) (the runtime trip count). This page does **not** re-derive them. It **threads** them into one traceable narrative — a single transformer-decode step followed from the front-end `S32` size scalar all the way to the ADDR4 hardware descriptor — and then names the **structural facts** that make the whole system cohere: one shared register-handle offset, one honest negative result, and one bit-numbering seam that the dynamic-shape reports left open but the [ADDR4](../isa/addr4.md) encoder page closes.

Three things to take away before the trace:

- **There is no shape bucketing inside the compiler.** neuronx-cc 2.24 has no "compile N static shape buckets" mechanism. Bucketing is produced *above* the compiler (the framework traces the model once per shape); the compiler sees **one program, one bound, per invocation**. Everything dynamic the compiler materializes is a runtime **offset** or a runtime **trip count**, not a runtime **shape**.
- **One offset is the spine of the system.** Every runtime value — whether it steers a DMA's base address or a loop's iteration count — bottoms out in a `bir::Register` referenced by `bir::BirIntRuntimeValue` at **`+0x40`**. This page witnesses that offset in **four independent function bodies** across two binaries.
- **A dynamic offset never shrinks a buffer.** SBUF is allocated for the **max** (the compile-time bound), because by the time the allocator runs there are no shape-dynamic tensors left — only bounded, padded, max-sized ones whose *addresses* (not sizes) the runtime steers.

For reimplementation, the synthesis contract is:

- The **two genuinely-dynamic artifacts** the compiler emits (runtime OFFSET via [TensorCopyDynamic](tensorcopy-dynamic-generators.md) → DGE/indirect DMA; runtime TRIP COUNT via [`InstDynamicForLoop`](dynamic-for-loop.md)) and the **single primitive** both reduce to.
- The **seven hand-off seams** between the per-organ pages, each anchored to a shared offset/symbol witnessed in both producer and consumer.
- The **honest gaps**: the cross-binary front→back hand-off (synthesis, not a co-verified offset) and — now resolved on this page — the ADDR4 register-mode bit number.

| | |
|---|---|
| **The spine** | `bir::BirIntRuntimeValue` register handle @ `+0x40` (witnessed in 4 bodies) |
| **Runtime offset path** | `TensorCopyDynamicDst/SrcGen` → `DynamicAPINFO` → register-ALU → DGE → descriptor → ADDR4 |
| **Runtime trip path** | `InstDynamicForLoop` (op 106), axis ub `= coef·reg + const` |
| **Offset producer (regref)** | `KlirToBirCodegen::assembleDynamicInfo @0xf20230` (`*(rv+0x40) = Register`) |
| **Offset rewriter (regref)** | `rewireDynamicAPRegisters @0xef91d0` → callback `sub_EFA700` (`*(v+0x40)` read+write) |
| **Trip reader (regref)** | simulator's upper-bound eval (`nki_klr_sim`) via `BirIntRuntimeValue::getNameStr @0x513f30` (`*(rv+0x40)`) |
| **DGE route gate** | `AssignHWDGEEngineImpl::assignEngine @0x1180360` — `if (DGEType@+0xF8 != 2) return;` |
| **Compiler max-bound** | `pelican::AffineIdx::getMaxTripcount @0xb45430` (virtual, vtable+0x140) |
| **ADDR4 modes** | bit 29 indirect-gather · bit 30 active · **bit 31 register-mode** ([ADDR4](../isa/addr4.md)) |

> **GOTCHA — "dynamic shape" is a misnomer in the backend.** Three different things wear the word "dynamic": a dynamic *shape* (a tensor whose extent is a runtime value), a dynamic *offset* (a static-shape tensor accessed at a runtime base address), and a dynamic *trip count* (a loop run a runtime number of times). The first **does not survive** into Penguin — `xla::DynamicPadder` pads it to the bound and masks it. Only the second and third reach the backend, and they are what this whole thread reverses. Conflating "dynamic shape" with "dynamic offset" is the single most common reading error here.

---

## Section 0 — The one structural fact: no compile-N-buckets

The deepest fact, established by the front-end survey ([4.31 hlo-opt dynamic front](../front/) family) and threaded through every backend organ:

> **neuronx-cc 2.24 has no "compile-N-static-shape-buckets" mechanism.** Shape bucketing is produced *above* the compiler — the framework (torch-neuronx / transformers-neuronx) traces the model once per shape, emitting one fully-static HLO module per bucket; the runtime (libnrt) dispatches to the smallest fitting NEFF and pads the input up to it. The compiler sees one program per invocation, and inside it there is no bucket choice. *(CONFIRMED — by exhaustive absence plus the positive bounded-dynamic mechanism.)*

**Adversarial verification of the negative.** A negative result is only as strong as the search behind it. Sweeping the compiler binaries for every `bucket` token resolves each one to something **unrelated to shapes**:

- `tbb::…::init_bucket` / `rehash_bucket` / `advance_to_next_bucket` — Intel TBB concurrent-hash-map internals.
- `llvm::DenseMapBase<…>::LookupBucketFor` / `moveFromOldBuckets` — LLVM open-addressing probe (this is the *same* helper family the `axisRegs` map uses; see seam S2).
- `bir::isCoalescedCCOp` / `birverifier::checkCoalescedCCOps` / `InstCollectiveCompute` coalescing — **collective-op** byte-size bucketing (the `--internal-ccop-*-bucketing` flags), which groups small AllGather/AllReduce/ReduceScatter into fewer large collectives. This is a distributed-SPMD performance fusion, **orthogonal** to dynamic shapes.

There is **no** pass, class, flag, or string that compiles one dynamic shape into N static shape variants, and **no** runtime bucket-selection loop in the AOT compiler binaries. *(CONFIRMED — `bucket` token sweep over `libwalrus.so` / `libBIR.so`; every hit is TBB, LLVM DenseMap, or CCop coalescing.)*

> **NOTE — why this is the deepest reason there is no bucketing.** By the time the SBUF allocator runs, `xla::DynamicPadder` has already padded every bounded-dynamic dim to its compile-time bound `N` and select-masked the valid region. The only sizes the allocator ever sees are the bounds. "Allocate for the max" is therefore not a heuristic — it is structural: **one bound = one buffer**. There is nothing to bucket. (See Section 4.)

**CONSEQUENCE.** Two — and only two — genuinely-dynamic artifacts survive into the backend, and they are what the whole thread reverses:

- **(A) Runtime OFFSET** — a static-shape tensor whose access *base address* is a runtime register: a `DynamicSlice` read at a runtime position (gather), a `DynamicUpdateSlice` write (scatter, e.g. a KV-cache write index), MoE expert routing. This is the DGE / indirect-DMA path. Owner pages: [5.24](tensorcopy-dynamic-generators.md) (the IR node), [5.25](symbolic-ap-register-alu.md) (the AP/register lowering), [5.23](dge-level-dynamic-dma.md) (the DGE route), [ADDR4](../isa/addr4.md) (the encoding).
- **(B) Runtime TRIP COUNT** — a data-dependent loop whose iteration count is a runtime register: `bir::InstDynamicForLoop`, opcode 106. Owner page: [5.26](dynamic-for-loop.md).

Both (A) and (B) bottom out in the **same** hardware primitive: a value materialized into a `bir::Register` and referenced by `BirIntRuntimeValue` at `+0x40`. That offset is the spine (Section 2, seam S1).

---

## Section 1 — The end-to-end thread (one worked example, traced to ADDR4)

The example exercises **both** dynamic flavours: a transformer decode step that **(i)** writes the new K/V at a runtime position (a `DynamicUpdateSlice` = a `TensorCopyDynamicDst` scatter), and **(ii)** loops over a runtime sequence length (an `InstDynamicForLoop`). Each stage names the **real symbol** that drives it. **⭐** marks a seam (a hand-off between owner pages).

### Stage 0 — Front end: the `S32` size scalar  *(hlo2penguin / hlo-opt, stock XLA + thin Neuron glue)*

The model is traced at a **bounded** shape: the sequence dim has bound `N`, `is_dynamic = true`; the actual length arrives as an **`S32` size scalar**. `xla::DynamicParameterBinding::Bind` seeds `xla::DynamicDimensionInference` with the side table `(HloInstruction, ShapeIndex, dim) → S32 size HLO`. The KV write index is an `S32` scalar operand of a `DynamicUpdateSlice`.

`xla::DynamicPadder::Run` then makes the program fully static: `PadWithScalar` writes a padding identity into `[size : bound)` of each dynamic dim using an `iota ≥ size` mask + select; `DynamicIndexSplitter::Run` guarantees each dynamic index is a **scalar** `S32` (the reject string is *"Dynamic indexing instruction with non-scalar index is not supported. Make sure that 'dynamic-index-splitter' pass was exectuted to canonicalize the indices:"*); and the Neuron MHLO cleanup `CanonicalizeForTensorizer::replaceGetDimensionSize` const-folds any `GetDimensionSize` on a statically-known dim. What survives into the Neuron path is exactly two things:

- a **static-shape** `DynamicUpdateSlice` whose **destination offset is a runtime `S32` scalar**, and
- a runtime **size/count scalar** (the sequence length) destined to become a loop bound.

> **⭐ HAND-OFF 0→1 (seam S6, STRONG / synthesis).** `TensorizerLegalizationPass` → `xla::hilo` builds Penguin IR. The runtime scalar becomes an opaque `S32` Penguin value; the dynamic-DMA SBUF scratch is sized by `xla::hilo::SetDynamicDMASbufBytes` and the `dynamic-dma-sbuf-bytes` / `dynamic-dma-scratch-size-per-partition` flags. **No registers exist yet.** This hand-off crosses a binary boundary (front-end `hlo2penguin` vs back-end `libwalrus`/`libBIR`); it is documented structurally on both sides but is **not** a single co-disassembled body — it is the seam most reliant on synthesis (S6).

### Stage 1 — Penguin IR node  *([5.24 TensorCopyDynamic generators](tensorcopy-dynamic-generators.md))*

The runtime-offset write is emitted as a `TensorCopyDynamicDstGen` (scatter): `src` = the data (`AccessMode.LOAD`), `generic_addrs` = the runtime address/index tensor, `generic_dim` = the indexed dim, `offset_scale` = the index→offset stride, `addr_free_indices` = the dynamic-AP indices. Its `rhs_str` is `"dynamic_copy_dst %s"`. (A `DynamicSlice` *read* would be a `TensorCopyDynamicSrcGen` / gather, `rhs_str "dynamic_copy_src …"`.) `serialize()` emits the on-wire dynamic-copy descriptor exactly in the fields the DMA lowering consumes, and `enumerate_ap_indices` threads `addr_free_indices` as **first-class AP indices** alongside the normal free/partition indices — so the runtime-address registers go through AP register allocation just like static ones.

The runtime loop bound (the sequence-length scalar) is emitted as a `birpy.BirDynamicForAxis` whose upper bound is the runtime value → it becomes an `InstDynamicForLoop` (Stage 1b).

> **⭐ HAND-OFF 1→2.** The `NeuronEngine` class attribute on the `Src`/`Dst` generator is the engine hint the DGE pass ([5.23](dge-level-dynamic-dma.md)) reads; `addr_free_indices` + `generic_addrs` are what the register-ALU pass ([5.25](symbolic-ap-register-alu.md)) lowers.

### Stage 1b — Dynamic loop structure  *([5.26 dynamic for-loop](dynamic-for-loop.md))*

The runtime-trip loop is `bir::InstDynamicForLoop` (opcode 106), carrying one `bir::DynamicForLoopAxis*`. The axis holds three `QuasiAffineExpr`: `lb`, `ub`, `step` (set together by `InstDynamicForLoop::setAxis(string, QuasiAffineExpr, QuasiAffineExpr, QuasiAffineExpr)` — CONFIRMED symbol in `libBIR.so`). The dynamic discriminator vs the static `LoopAxis` is `getTripcount() → 0` paired with `hasRuntimeValue() → 1`. The verifier asserts `lb`/`step` are constants and `ub` is **exactly** `coef·<runtime-register> + const` with a non-null register. The trip count is therefore `const + coef·reg`, where `reg` is the value the `axisRegs` map produced (seam S2).

> **⭐ HAND-OFF 1b→spine (seam S1).** The `ub` `QuasiAffineExpr`'s single non-constant term is a `bir::BirIntRuntimeValue`; **its register lives at `+0x40`** — the same register handle as the offset path. This is *why* a runtime offset and a runtime trip count are the same primitive.

### Stage 2 — Dynamic-AP companion  *(`KlirToBirCodegen::assembleDynamicInfo @0xf20230`, libwalrus)*

Codegen produces the static (kind-1) `PhysicalAccessPattern` — the resolved, bound-sized geometry — and `assembleDynamicInfo` packages the runtime-offset half into a `bir::DynamicAPINFO` hung off `PhysicalAccessPattern + 0x1D8`. The full demangled signature is `KlirToBirCodegen::assembleDynamicInfo(unique_ptr<bir::DynamicAPINFO>&, shared_ptr<klr::TensorRef>, bir::InstArg&)`. It distinguishes:

- a **scalar** offset (KV write index, the common case) → a pelican `bir::BirIntRuntimeValue` (kind 7) whose `regref @ +0x40` is the `Register` looked up by `getRegisterByName` (the failure string is *"… for scalar offset"*). **⭐ regref@+0x40 — producer end of seam S1.**
- a **vector** offset (gather index / MoE routing) → a pelican `IndirectArgExpr` (kind 3) with `arg_id`, `indirect_dim == 0`, `indirect_dim_max_index = index-tensor element count`.

The net runtime address is `c + Σ coef_axis · expr_axis`, and `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided` enforces the XOR (exactly one of scalar/vector — seam S7). At this stage the AP is still **symbolic** (kind-2): the addresses are pelican `Expr`s over loop axes, not yet registers.

> **⭐ HAND-OFF 2→3 (seam S5).** The kind-2 `SymbolicAccessPattern`, and any residual `DynamicAPINFO @ +0x1D8` on a kind-1 `PhysicalAccessPattern`, is what the register-ALU pass materializes.

### Stage 3 — Register-ALU materialization  *([5.25 symbolic-AP register-ALU](symbolic-ap-register-alu.md), `LowerAP::convertSymAP @0x11b7f80`)*

Two cooperating sub-paths run inside the `lower_ap` / `expand_inst_late` passes:

**(3a) `convertSymAP`** turns a kind-2 `SymbolicAccessPattern` into a kind-3 `RegisterAccessPattern`. It guards against DRAM (*"cannot be DRAM"* — only SB/PSUM symbolic APs lower), mints up to seven virtual registers on the **consuming inst's engine** (`RegAPOffset`, `RegSubTensorId`, `RegTemp`, `RegDramAddr0`, `RegDramAddr1`, `RegTemp1`, `RegAPAddr`), and Horner-walks each `QuasiAffineExpr` into register-ALU ops: `RegMov dst = c`; then per loop axis `tmp = axisReg·coef` (`AluOp 6 = mult`); `dst += tmp` (`AluOp 4 = add`). It then **delinearizes** the partition/free offset into the engine's 2-D SBUF/PSUM space: `RegAPAddr = base + (off/step)·part_stride + (off%step)·dtype_bytes`, with `part_stride = 0x40000` for SBUF, `0x8000` for a PSUM bank, via `divide(7) / mult(6) / mod(0x1B) / add(4)`. Finally it builds the kind-3 `RegisterAccessPattern` (`regref @ +0xE8 = RegAPAddr`, `reg_ap_offset @ +0xF0 = RegAPOffset`, `reg_sub_tensor_id @ +0xF8 = RegSubTensorId`) and splices it in.

> **⭐ The per-axis `axisReg` (seam S2)** is the loop-induction register that the loop-lowering pass minted — looked up, **not** minted here, via the `axisRegs` map. If absent, `convertSymAP` errors *"loop axis hasn't been assigned register for induction variable!"* — proving it **consumes** what the loop side ([5.26](dynamic-for-loop.md)) **produced**.

**(3b) `ExpandDynamicAPInfo @0xcd4dc0` + `ExpandDynamicAPInfoImpl @0xccd030`** handle a residual `DynamicAPINFO` left on a kind-1 `PhysicalAccessPattern`: emit `InstRegisterAlu mult(6)/add(4)` for `addr = c + Σ coef_i·index_i` (Expr→Reg memoized by `ExprRefHasher` — a CSE), densify the indirect `arg_id`, guard *"Only DynamicDMA can have DynamicAP"*. Then `rewireDynamicAPRegisters @0xef91d0` walks the AP (guarding on `+0x1D8`, via `PhysicalAccessPattern::forEachDynamicExpr`) and rebinds each `BirIntRuntimeValue.regref @ +0x40` to the re-named register through a name→name map. **⭐ regref@+0x40 — rewriter end of seam S1.**

> **⭐ HAND-OFF 3→4.** Regalloc (`coloring_allocator_with_loop`, the `L33` coloring allocator) colors the minted virtual registers; the kind-3 `RegisterAccessPattern` (or the DynamicDMA's resolved offset registers) is what the DGE pass and the encoder consume.

### Stage 4 — DGE route selection  *([5.23 DGE level selection](dge-level-dynamic-dma.md), `getDGEInstructionType @0x1097330`)*

`dynamic_dma_setup` (pre-allocator) inserts the SB-space scratch `MemoryLocation "DynamicDMAScratchLoc"` used to stage generated descriptors. `DynamicDMAScan::run @0xd03c40` then visits every DMA-family inst; if `DGEType @ InstDMA+0xF8 == 3` (Unassigned), `getDGEInstructionType` decides the route and stamps it:

- `DGEType 0 None` — static DMA, no descriptor generation;
- `DGEType 1 SWDGE` — software descriptor program (the **scalar dynamic-offset** flavour — our scatter);
- `DGEType 2 HWDGE` — hardware descriptor generation (vector dynamic offsets / xbar transpose), **gated to `ArchLevel > 29`**.

The arch gate is binary-witnessed: `getDGEInstructionType` reads `v9 = *(getModule() + 172)` (`ArchLevel @ Module+0xAC`) and the HWDGE candidate is only added inside `if (v9 > 29)` (gen3 = 30 / core_v4 = 40 / core_v5 = 50). Our scatter (scalar dynamic offset) routes to `SWDGE(1)`, with `HWDGE(2)` offered as an arch-gated alternative on gen3+.

> **⭐ HAND-OFF 4→4b (seam S3).** `DGEType @ +0xF8` is the **sole gate** into the HWDGE engine binder.

### Stage 4b — HWDGE engine binding  *(`AssignHWDGEEngineImpl::assignEngine @0x1180360`)*

The binder's **first line** is binary-witnessed: `if (a2[62] != 2) return;` — `a2[62]` is the `_DWORD` at `+0xF8`, so this is a **no-op for None / SWDGE / Unassigned** and binds an engine **only for HWDGE(2)**. `ValidEngines = {SP(6), Activation(2)}` on `arch ≤ 49` (a larger set on core_v5); the chosen engine is written to `InstDMA + 0x90`. A SWDGE DMA stays on its triggering compute engine. `dynamic_dma_cleanup` then re-asserts the stamped route is in the eligible set and that any HWDGE DMA's engine is in `{SP, Activation}`.

### Stage 5 — Descriptor lowering  *(`lower_dynamic_dma`, `LowerDMAImpl::createDescForReadVarAddr @0x1198860`)*

`lower_dynamic_dma` (registered as `register_generator_lower_dynamic_dma`) materializes the descriptor program. For our single-runtime-offset static-shape copy, `createDescForReadVarAddr` synthesizes a `MemoryLocation` referencing the runtime address (`FunctionArgumentMap::getMapping` → `addMemoryLocation` → `createDescs<InstDMADescriptorCopy>` → `setReferenceMemLoc`), producing an `InstDMABlock` whose descriptor `PhysicalAccessPattern` address field is **patched at runtime** from the variable. `SplitLargeDMAs` splits over-budget descriptor blocks; `convertDMA2Trigger` replaces the `InstDMACopy` in place with an `InstDMATrigger`, and `InstDMABlock::setTrigger` binds the block to its trigger (the pairing the simulator follows: a trigger fires → iterate the descriptor list).

> **⭐ HAND-OFF 5→6.** The descriptor's `PhysicalAccessPattern` — carrying its register/reference address — is handed to the wire encoder.

### Stage 6 — ADDR4 hardware descriptor  *([2.2 ADDR4](../isa/addr4.md), `CoreV{2,3,4}GenImpl`)*

The encoder stamps the kind-3 `RegisterAccessPattern` (or the resolved `actual_ap`) into the 32-bit ADDR4 word:

- **register-mode** (the start address comes from a register, not an immediate): **ADDR4 bit 31** (`0x80000000`) → byte 0 becomes an 8-bit colored-register id (`regid < 0x40`), bits `8..30` forced to zero. The runtime reads that register for the base address.
- **indirect** (gather/scatter via an index tensor): **ADDR4 bit 29** (`0x20000000`) on the index slot, plus an `INDIRECT16B` / `INDIRECT20B` / `MXINDIRECT16B` record (the gather index tensor).

The runtime then reads the register / index, computes the address (the value the register-ALU pass pre-computed into `RegAPAddr`, or the runtime-patched reference memloc), and the DMA moves the **bound-sized** tile to/from the runtime position.

> **⭐ END OF THREAD.** front-end runtime offset (`S32` scalar) → `TensorCopyDynamicDstGen` → `DynamicAPINFO` symbolic exprs → register-ALU + kind-3 `RegisterAccessPattern` → `DGEType` route + HWDGE engine → SW/HW descriptor → ADDR4 register/indirect word. The runtime **trip count** runs the parallel loop path (`InstDynamicForLoop` `ub = coef·reg + const`) and **shares the same `regref @ +0x40`** register handle and the same `axisRegs` map. A reader can now trace a dynamic shape from the front end to ADDR4.

---

## Section 2 — The seams (each hand-off, witnessed in producer and consumer)

A seam is **CONFIRMED** when the same offset/symbol is witnessed in *both* the producing and the consuming body. Below, each verdict is grounded directly in the binary.

### ⭐ Seam S1 — `BirIntRuntimeValue.regref @ +0x40` — the system spine  *(CONFIRMED, 4 bodies)*

The runtime value's backing `Register*` lives at `BirIntRuntimeValue + 0x40` (= `+64`). This **one** offset is shared by four independent producers/consumers, binary-witnessed here:

1. **Producer — `assembleDynamicInfo @0xf20230` (libwalrus):** the scalar branch does `getRegisterByName` then `*(rv + 64) = RegisterByName` (decompiled line 178), with the failure string *" for scalar offset"* (line 224). *Mints* the scalar runtime offset's register handle into `+0x40`.
2. **Rewriter — `rewireDynamicAPRegisters @0xef91d0` callback `sub_EFA700` (libwalrus):** the body `dyn_cast<bir::BirIntRuntimeValue>`-asserts, reads `v5 = *(v2 + 64)`, looks the name up at `v5 + 296` (`reg.name @ +0x128`), calls `getRegisterByName`, and writes `*(v2 + 64) = result`. **Reads *and* rewrites** `+0x40` to the re-named register.
3. **Reader — `BirIntRuntimeValue::getNameStr @0x513f30` (nki_klr_sim / libBIR):** `v2 = *(a2 + 64)` then formats `*(v2 + 296)` (the register's name). The simulator's upper-bound evaluation reaches the trip-count register through exactly this path.
4. **Reader — `BirIntRuntimeValue::str @0x514a10` (nki_klr_sim / libBIR):** reads `a2[8]` (= `+0x40`) and, when the printer is `getNameStr`, formats `*(v4 + 296)` — a *second* independent body in the simulator binary using `+0x40` → `+296`.

**VERDICT: CONFIRMED.** `regref @ +0x40` is identical across the offset path (`assembleDynamicInfo` / `rewireDynamicAPRegisters`) and the trip-count path (the simulator's runtime-value printers), and the `reg.name @ +296` key is shared by both. This is the strongest seam in the system — it is *why* a runtime offset and a runtime trip count reduce to the same primitive.

> **CORRECTION (D-Z07 §S1 / Evidence Anchors).** The capstone backing report cited the trip-count read in the simulator's `evaluateUpperBoundExpr` as `RegState::read(…, *(bir::Register**)(v38 + 64), 0)` — i.e. it inferred the `+0x40` read inside the (decompilation-failed) upper-bound evaluator. The mechanism is **right** and the offset is **right**, but the *cleanest* binary witnesses for `+0x40` in the simulator binary are the two `BirIntRuntimeValue` name printers (`getNameStr @0x513f30`, `str @0x514a10`), whose pseudocode *did* decompile and which unambiguously dereference `+0x40 → +296`. Anchor the simulator end of S1 to those two bodies, not to the failed-decompile evaluator. *(The four `bir::Hwm` latency methods for `InstDynamicForLoop` are likewise present as symbols in `nki_klr_sim` but did not produce pseudocode — see Section 5.)*

### ⭐ Seam S2 — `axisRegs : DenseMap<DynamicForLoopAxis*, Register*>` — loop ↔ register  *(CONFIRMED map + consumer; producer INFERRED)*

The loop's induction-variable register is bound into a `DenseMap<bir::DynamicForLoopAxis*, bir::Register*>`; `convertSymAP`'s `lowerAffineExpr` **looks up** (does not mint) that per-axis register when lowering a symbolic AP over the axis. The map helpers (`operator[]`, `LookupBucketFor`, `grow`) are present in `libwalrus.so`, and the consumer's error path (*"loop axis hasn't been assigned register for induction variable!"*) proves the producer/consumer contract.

**VERDICT: CONFIRMED** (map helpers present; consumer contract co-cited). **NOTE:** the exact *population site* — the codegen body that fills the map (mints the induction register and inserts it) — was not isolated; it is **INFERRED** to be the loop-structuring/unroll pass.

### ⭐ Seam S3 — `DGEType @ InstDMA+0xF8` — scan ↔ engine binder  *(CONFIRMED, both ends)*

**Producer:** `DynamicDMAScan` stamps `I.DGEType @ +0xF8 = getDGEInstructionType().first` (`{0 None / 1 SWDGE / 2 HWDGE}`, HWDGE only if `ArchLevel > 29`). **Consumer:** `assignEngine @0x1180360`'s first line is `if (a2[62] != 2) return;` — `a2[62]` is the `_DWORD` at `+0xF8`. Both the arch gate (`getDGEInstructionType` line 164: `*(getModule() + 172)`, line 573/629/704: `if (v9 > 29)`) and the consumer gate are binary-witnessed. **VERDICT: CONFIRMED.**

### ⭐ Seam S4 — kind-3 `RegisterAP` → ADDR4 register/indirect mode  *(RESOLVED on this page)*

**Producer:** `convertSymAP` writes `regref @ +0xE8`, `reg_ap_offset @ +0xF0`, `reg_sub_tensor_id @ +0xF8` on the kind-3 `RegisterAccessPattern`. **Consumer:** `CoreV{2,3,4}GenImpl` stamps the ADDR4 word. The dynamic-shape reports flagged the *register-mode bit number* as `SEAM-UNCERTAIN` (one input said bit-31, another listed bit-30 "active" without naming a register-mode bit). **This page resolves it** — see Section 3.

### ⭐ Seam S5 — `DynamicAPINFO @ PhysicalAP+0x1D8` — produce ↔ consume  *(CONFIRMED)*

**Producer:** `assembleDynamicInfo` hangs the `DynamicAPINFO` off `+0x1D8`. **Consumer:** `rewireDynamicAPRegisters @0xef91d0` guards on `*(a1 + 472)` (= `+0x1D8`) before walking it with `PhysicalAccessPattern::forEachDynamicExpr` (CONFIRMED at `0x3fd7178`). Same offset, same struct. **VERDICT: CONFIRMED.**

### ⭐ Seam S6 — front-end `S32` scalar → Penguin value  *(STRONG / synthesis)*

**Producer:** the front end leaves an `S32` runtime size/offset scalar (the survivor of `DynamicPadder` + `DynamicIndexSplitter`). **Consumer:** [5.24](tensorcopy-dynamic-generators.md) wires it as `generic_addrs`/`addr_free_indices` (offset) or as a `BirDynamicForAxis` upper bound (trip count). **VERDICT: STRONG, not a single co-verified offset.** The front end (`hlo2penguin`) and the backend (`libwalrus`/`libBIR`) are **different binaries**; the `xla::hilo → Penguin` bridge is documented structurally (`SetDynamicDMASbufBytes`, `BirDynamicForAxis`) but the exact Penguin-value → `BirIntRuntimeValue` construction is **not** traced end-to-end in one body. This is the seam most reliant on reasoning. *(Flagged.)*

### ⭐ Seam S7 — DGE flavour (scalar/vector) ↔ AP offset kind (k7/k3)  *(STRONG)*

`getDGEInstructionType` dispatches on `isScalarDynamicOffsetAP` (vfn+88, witnessed at line 562) vs `isVectorDynamicOffsetAP` (vfn+72); `assembleDynamicInfo` builds a scalar `BirIntRuntimeValue` (k7) or a vector `IndirectArgExpr` (k3) and XOR-validates via the **same** `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided`. Scalar AP → SWDGE; vector AP → vector-offsets/HWDGE. **VERDICT: STRONG** (shared validator co-cited; the predicate *bodies* are PLT-only in the DGE pass, so semantics come from call sites).

### Seam-agreement table

| Seam | Producer | Consumer | Shared symbol / offset | Status |
|---|---|---|---|---|
| S1 | `assembleDynamicInfo @0xf20230` | sim printers / `rewireDynamicAPRegisters` | `BirIntRuntimeValue.regref @ +0x40` | **CONFIRMED** (4 bodies) |
| S2 | loop-lowering (population site) | `convertSymAP` `lowerAffineExpr` | `DenseMap<DynamicForLoopAxis*,Register*>` | CONFIRMED map; producer INFERRED |
| S3 | `DynamicDMAScan` | `assignEngine @0x1180360` | `DGEType @ InstDMA+0xF8` | **CONFIRMED** |
| S4 | `convertSymAP` kind-3 RegisterAP | `CoreV*GenImpl` ADDR4 | bits 29 indirect / 31 register-mode | **RESOLVED** (Section 3) |
| S5 | `assembleDynamicInfo` | `rewireDynamicAPRegisters @0xef91d0` | `DynamicAPINFO @ PhysicalAP+0x1D8` | **CONFIRMED** |
| S6 | front-end `S32` scalar | Penguin node | `xla::hilo → Penguin` (cross-binary) | STRONG / synthesis |
| S7 | `getDGEInstructionType` | `assembleDynamicInfo` | `_validateOnlyOneOf…Offset` XOR | STRONG |

---

## Section 3 — ⭐ The ADDR4 register-mode bit (seam S4 resolution)

The dynamic-shape reports left this open as `SEAM-UNCERTAIN`: one input recorded register-mode as ADDR4 **bit 31**, another listed the high nibble as bits 29(indirect)/30(active) and did **not** name a bit-31 register-mode — and neither disassembled the `CoreV*Gen` ADDR4 bit-set. The [ADDR4 encoding page](../isa/addr4.md) **does** disassemble that emitter, and it closes the seam unambiguously. The 32-bit word packs:

| Field | Bits | Mask | Meaning |
|---|---|---|---|
| Byte address | `0..28` | `0x1FFFFFFF` | 29-bit on-chip byte address (SBUF or PSUM) |
| Mode bit 0 | `29` | `0x20000000` | INDIRECT-gather (the index/offset slot) |
| Mode bit 1 | `30` | `0x40000000` | ACTIVE / dynamic-AP marker |
| Register-mode | `31` | `0x80000000` | RM = 1 ⇒ byte 0 is a colored-register id, bits `8..30` zero |

The encoder bytes are witnessed in `libwalrus.so` disassembly: `or byte [rax+3], 0x20` stamps **bit 29** (tensor-indirect, mode `0b01`); `test edi, 0x40000000` tests **bit 30** (active/dynamic); and the `0x80000000` register-mode flag (bit 31) collapses the word to `0x80000000 | (regid & 0xFF)` with `regid < 0x40`.

> **CORRECTION (D-Z07 §U1 / §S4).** The seam was a **granularity gap, not a conflict**, and the two readings are now reconciled:
> - **"register-mode = ADDR4 bit 31"** is **CORRECT.** Bit 31 (`0x80000000`) is the register-mode flag; setting it re-purposes byte 0 as the colored register id that supplies the runtime base address — exactly the kind-3 `RegisterAccessPattern → register-mode descriptor` hand-off the register-ALU pass feeds.
> - **"bit 30 active"** is a **different bit**, not a competing register-mode claim. Bit 30 (`0x40000000`) is the ACTIVE / dynamic-AP marker; it is *not* the register-mode flag. The second report was describing the mode **nibble** (bits 30:29) and never named bit 31, which made it *look* like a disagreement.
> - **"bit 29 indirect"** was already consistent across both inputs and is CONFIRMED-cross.
>
> **NET:** ADDR4 bit 29 = indirect-gather, bit 30 = active/dynamic, **bit 31 = register-mode** — all three CONFIRMED from the encoder disassembly on the [ADDR4](../isa/addr4.md) page. The dynamic-shape thread's `SEAM-UNCERTAIN` on the register-mode bit number is **resolved: bit 31.**

---

## Section 4 — SBUF interaction: a dynamic offset never shrinks the buffer

> **Question.** Does a dynamic AP force a worst-case / max-bound SBUF allocation?
> **Answer.** Yes — for a *structural* reason, not a heuristic one.

**4.1 The shape is already bounded + padded before SBUF allocation.** By the time the backend allocator runs, there are no shape-dynamic tensors left: `xla::DynamicPadder` padded every bounded-dynamic dim to its bound `N` and select-masked the valid region (Section 0). The allocator sees a tensor of **compile-time size = the bound**. There is no "dynamic-sized SBUF buffer" to allocate; the buffer is statically max-sized. A dynamic **offset** does not change the buffer's size — it only changes *which element* of the max-sized buffer the runtime addresses. *(CONFIRMED — DynamicPadder removes shape-dynamic ops; only static + runtime-offset slices survive into Penguin.)*

**4.2 The compiler-side max-bound path for trip counts.** Any static sizing/cost analysis that must run **without** a live register uses `pelican::AffineIdx::getMaxTripcount @0xb45430` — a **virtual** call through `vtable + 0x140` (slot 320): `return (*(this->vtable[0x140]))(this);`. A register-backed `AffineIdx`'s subclass override returns a **conservative upper bound** (the max), because the concrete count is unknown at compile time. This is the explicit "use the max for sizing" hook. **CONTRAST:** the simulator uses the **concrete** register (it executes the actual runtime count). So the tension the task named resolves cleanly: **compiler = max (`getMaxTripcount`), simulator = concrete (register read)**. *(CONFIRMED — `getMaxTripcount` is a single-instruction virtual dispatch through vtable+0x140; the override value/derivation is INFERRED.)*

**4.3 The dynamic-DMA SB scratch reservation.** Independently of the data buffers, the DGE/indirect path reserves a fixed SB-space scratch: `dynamic_dma_setup` inserts one `MemoryLocation "DynamicDMAScratchLoc"` (memory-space tag 4 = SB) to stage the dynamically-generated descriptors, and its size is governed by the front-end flags `dynamic-dma-sbuf-bytes` (`xla::hilo::SetDynamicDMASbufBytes`) and `dynamic-dma-scratch-size-per-partition` — a fixed, max-bounded per-partition reservation, not data-dependent. *(CONFIRMED — scratch memloc + sizing flags.)*

**4.4 Net SBUF model** *(synthesis)*:

- Dynamic **shape** → no effect beyond using the bound (already the only size).
- Dynamic **offset** → no buffer-size change; the buffer is max-sized; the offset re-addresses within it; plus a fixed DGE scratch.
- Dynamic **trip** → loop-body buffers sized by `getMaxTripcount` (the max), reused across iterations.

The compiler **always** allocates SBUF for the worst case. Runtime values only steer *addresses* and *iteration counts* within max-sized, statically-allocated space. *(STRONG — each piece CONFIRMED individually; the unified "always max" conclusion is synthesis across the three mechanisms, since no single allocator body reads the bound, `getMaxTripcount`, and the scratch together.)*

---

## Section 5 — Scheduling interaction

**5.1 The dynamic loop has no static per-instruction latency.** All four `bir::Hwm` latency methods for `InstDynamicForLoop` — `getLatency @0x99e608`, `getLatencyExec @0x99eed8`, `getLatencyReadInit @0x99e330`, `getLatencyWriteDrain @0x99f4f0` (addresses in `nki_klr_sim`) — are present as symbols but **do not produce pseudocode** (Hex-Rays decompilation fails on each), consistent with the reported "not implemented" stubs. *(CONFIRMED present as symbols; bodies are decompilation-failed — see the S1 CORRECTION.)* So `InstDynamicForLoop` is a pure **control container**: its cost is obtained by **recursing the loop body** (as for a static `InstLoop`) with the trip count being an *estimate* (`getMaxTripcount` or a profile estimate), since the real count is runtime. The body's per-instruction latencies are well-defined; the loop multiplies them by the estimated trip count.

**5.2 The data-dependent cost estimators** (registered through `neuronxcc.starfish.penguin.Statistics`):

- `DynamicInstEstimator` (**coarse**): `est_inst_count += num_dynamic_instances // 65536` (load/store/contract, 2¹⁶ elems/inst) and `// 8388608` (TensorContract, 2²³); also sets `has_copy`/`has_reduce` shape flags. The floor-division is deliberately an estimate.
- `DetailDynInst` (**precise**): masked/predicated iteration domain (`domain_size ∩ active masks`) × `compute_flops` → `Total_flops`, × `dtype_size` → `Total_bytes_moved`. The loop trip count determines the domain **size**; `DetailDynInst` is where that estimate becomes a FLOP/byte cost.

These feed the scheduler/tiling heuristics where an exact count is impossible. *(CONFIRMED/STRONG — emitted as stats; the exact consuming scheduler module is not in these binaries.)*

**5.3 Net scheduling model** *(synthesis)*: a static region is exact (`Hwm` per-instruction latency); a dynamic **loop** is `body-recursion × estimated trip`; a dynamic **offset** DMA is scheduled like a normal DMA on its (SWDGE-trigger / HWDGE-SP/Act) engine, and the *extra* `InstRegisterAlu` address-compute ops the register-ALU pass emitted **do** have static latency. So a dynamic **offset** adds a few statically-costed ALU ops; only the dynamic **trip count** is genuinely estimate-costed.

> **NOTE — "Bucketize" in the scheduler is not shape bucketing.** `BucketizeCCOp` is collective-op **coalescing** (group small same-`(group_id, factor)` collectives into one tuple-I/O collective), driven by the `--internal-ccop-*-bucketing` flags. It is orthogonal to dynamic shapes — mentioned here only to close the loop with the Section-0 negative result. *(CONFIRMED — the CCop coalescing symbols `isCoalescedCCOp` / `checkCoalescedCCOps` are the only "bucketize"-adjacent code in the backend.)*

---

## Section 6 — Flagged gaps (honest uncertainties)

| # | Gap | Status |
|---|---|---|
| U1 | **ADDR4 register-mode bit number** — was `SEAM-UNCERTAIN` in the dynamic-shape reports. | **RESOLVED: bit 31** (Section 3, from the [ADDR4](../isa/addr4.md) encoder disassembly). |
| U2 | **Front-end `S32` scalar → backend `BirIntRuntimeValue`** (seam S6). Different binaries; the `xla::hilo → Penguin` bridge is documented structurally but not co-disassembled in one body. | STRONG / synthesis. |
| U3 | **`axisRegs` population site** (seam S2). Map helpers + consumer CONFIRMED; the producer pass that mints+inserts the induction register was not isolated. | Consumer CONFIRMED; producer INFERRED (loop-structuring/unroll). |
| U4 | **`getMaxTripcount` override value.** The virtual (vtable+0x140) returning a conservative max is CONFIRMED; the exact override body / how the max is derived (ub `coef·reg+const` with `reg` → its declared range?) was not read. | Path CONFIRMED; value INFERRED. |
| U5 | **Bounded-dynamic (single-NEFF) path use in production.** The compiler *can* compile a single NEFF with runtime size scalars (the XLA `PadToStatic`/`SliceToDynamic` path), but production stacks predominantly trace-per-bucket; which path a given NEFF uses is decided above the compiler. | Both paths CONFIRMED to exist; relative use INFERRED. |

---

## Cross-references

- [4.31 hlo-opt dynamic front-end](../front/) — the stock XLA `DynamicPadder` / `DynamicIndexSplitter` / `replaceGetDimensionSize` passes that produce the surviving `S32` scalar (Stage 0).
- [5.23 DGE level selection](dge-level-dynamic-dma.md) — `getDGEInstructionType`, the `DGEType @ +0xF8` route, the arch gate, and the engine binder (Stages 4 / 4b).
- [5.24 TensorCopyDynamic generators](tensorcopy-dynamic-generators.md) — the runtime-offset IR nodes `TensorCopyDynamicSrc/DstGen` (Stage 1).
- [5.25 symbolic-AP register-ALU](symbolic-ap-register-alu.md) — `convertSymAP`, the register mint + Horner-walk + delinearization (Stage 3).
- [5.26 the dynamic for-loop](dynamic-for-loop.md) — `InstDynamicForLoop` (op 106), the axis ub `= coef·reg + const`, and `axisRegs` (Stages 1b / S2).
- [Part 8 — DGE / `walrus/`](../walrus/) — the descriptor-generation engine the dynamic-DMA path drives.
- [2.2 ADDR4](../isa/addr4.md) — the 32-bit descriptor word; the authority on the register-mode (bit 31) / indirect (bit 29) / active (bit 30) bits (Stage 6, Section 3).
- [Worked example — matmul](../front/worked-example-matmul.md) / [flash attention](../front/worked-example-flash-attention.md) — the static end-to-end traces this page parallels.
