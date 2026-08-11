# Confidence Ledger

> *This page indexes claims and gaps across the `neuronx_cc` 2.24.5133.0+58f8de22 wiki (cp310/cp311/cp312 wheels, static analysis only). Every entry below mirrors the confidence grade carried by the source page; the addresses and symbols belong to the cited page and are pinned there. When a source page is re-graded, this ledger must follow — it is a mirror, not an independent claim.*

## Abstract

Every page in this book carries the four-tier confidence ladder from [§0.3](../methodology.md): **CERTAIN** (directly observed), **HIGH** (multiple signals agree, decisive line not read end-to-end), **MEDIUM** (deduced from structure or from a directly-read sibling), **LOW** (a plausible unproven reading). Most of the book is CERTAIN or HIGH. This appendix is the opposite-facing index: it consolidates, in one place, the book's **weakest-evidence claims and its genuine gaps**, so a reader can see exactly what is *not* solidly nailed down without re-reading 200 pages to find the caveats.

The honesty argument is the whole point. A wiki that hides its soft spots is more dangerous than one that has none, because a reader cannot tell a confident claim from a hopeful one. [§0.3](../methodology.md) already lists the *categories* of thing that are provably not recoverable from the shipped binaries (Xtensa instruction bodies, the `SUNDA_APB_BASE` numeric, identifier strings that were never compiled in). This page is the *instance-level* counterpart: the specific pages, the specific claims, the specific reason each is soft, and — for each — the evidence that *would* settle it.

Entries are grouped by **why** they are weak, not by topic, because the reason is what a reader needs in order to weigh the claim:

1. **Structure-only** — no code bodies were recoverable; the claim rests on ELF headers, byte-diffs, and strings (the GPSIMD Xtensa images, [§11.1](../custom-ops/gpsimd-xtensa-layout.md)).
2. **Cython-obscured call-order** — the symbol or string is confirmed, but the body lives in a compiled `.so` whose control flow was not walked instruction-by-instruction (much of [Part 6](../nki/production-kernel-inventory.md), `parallel_state`, `CollectiveOp.so`).
3. **DWARF-reconstructed** — the body *was* read, but via the `KernelBuilder` debug line table rather than disassembly; the line→method map is the evidence, not the opcodes.
4. **Cross-library / PLT-thunk** — the call site is pinned but the callee body is in a different binary (`libBIR.so`, `parallel_state`) not walked here.
5. **Genuine gaps** — a value, struct, or path that is simply not in the corpus: a withheld source, a stripped packer, an Xtensa immediate, a config-supplied default.
6. **Flagged seams** — a claim graded **HIGH** rather than CERTAIN on its own page because the decisive plumbing crosses a boundary (the front↔back LNC-size equality; the `info.json` ctor seed).

The §-numbers below are this book's part/chapter scheme; the link is the canonical page. Where a page grades a claim, that grade is reproduced here so the ledger can be audited against its source.

| | |
|---|---|
| **Scope** | The wiki's own confidence tags, consolidated |
| **Confidence model** | [§0.3 Methodology & Confidence](../methodology.md) |
| **Strongest single gap** | GPSIMD Xtensa code — *no disassembler in corpus* ([§11.1](../custom-ops/gpsimd-xtensa-layout.md)) |
| **Most-cited soft seam** | front-end `logical_nc_config` == backend `lnc_size` — **HIGH**, not CERTAIN |
| **Tag vocabulary** | CERTAIN / HIGH / MEDIUM / LOW / GAP |
| **Maintenance rule** | mirror only — re-grade the source page first, then this row |

---

## 1. Structure-only — claims with no recovered code body

These pages document hardware or images whose **instruction bodies could not be disassembled at all**. Every behavioural claim is deduced from ELF structure, a cross-image byte-diff, and `.rodata`/`.data` strings; only the layout claims are directly observed. The pages say so plainly and never present a string-derived behaviour as observed code.

| Claim | Page | Tag on the page | Why structure-only | What would resolve it |
|---|---|---|---|---|
| GPSIMD CPUs are 8 Tensilica Xtensa ELF32 images, one per core, re-linked at `0x84000000 + id·0x200000` | [§11.1](../custom-ops/gpsimd-xtensa-layout.md) | **CERTAIN** (layout) | `file`/`readelf` + byte-diff prove it without code | already settled |
| cpu_id derivation reads `MEM_WINDOW0_LO` UREG and checks `SUNDA_APB_BASE` | [§11.1](../custom-ops/gpsimd-xtensa-layout.md) | **HIGH** (string + byte-diff); `SUNDA_APB_BASE` numeric **LOW** | the deriving code is Xtensa, not disassembled; the immediate is an Xtensa literal | an Xtensa disassembler, or a NEFF/trace fixture showing the resolved base |
| The 6 `.ctors` register ATen/c10 statics + op-name table | [§11.1](../custom-ops/gpsimd-xtensa-layout.md) | count **CERTAIN**; binding **HIGH** (from `.rodata` strings) | ctor *bodies* are Xtensa, not read | Xtensa disasm of the ctor entries |
| Op dispatch is a flat function table keyed by FunctionId, not a c10 Dispatcher | [§11.1](../custom-ops/gpsimd-xtensa-layout.md) | **HIGH** (string surface; Dispatcher symbols absent) | absence-of-symbol argument, not a read selector | Xtensa disasm of the entry trampoline |
| Headroom (~1.37 MiB/core) holds stack/heap/DMA scratch | [§11.1](../custom-ops/gpsimd-xtensa-layout.md) | span **CERTAIN** (arithmetic); *use* **MEDIUM** | no allocator code disassembled | Xtensa disasm of the runtime allocator |

> **HARD LIMIT (from [§11.1](../custom-ops/gpsimd-xtensa-layout.md)) —** host binutils has no Xtensa backend (`objdump -i | rg xtensa` → empty) and IDA recovered `total_functions=2, decompiled=0, flirt=null`. Any future page claiming an Xtensa *instruction sequence* must ship the disassembler that produced it. The companion reconciliation page [GPSIMD reconciliation](../custom-ops/two-gpsimd-reconciliation.md) is *not* in this row — its compiler/IR side (`libwalrus`/`libBIR`) *is* disassembled; only its cp312 anchor `0x13597a0` is asserted rather than read, because cp312 `libwalrus` is not in the indexed corpus.

---

## 2. Cython-obscured — symbol confirmed, body not walked

The NKI front-end ([Part 6](../nki/production-kernel-inventory.md)) and the distribution layer ([Part 13](../distribution/)) lean heavily on compiled Cython `.so` modules. Their **string surface** (class rosters, method names, `pyx_n_s_` identifiers, asserts) is binary evidence and is read directly. What is soft is the **control flow inside** those modules — the exact call order, the default geometry, the field offsets — when the body was not single-stepped.

| Claim | Page | Tag on the page | Why soft | What would resolve it |
|---|---|---|---|---|
| Default worker-group geometry behind the 5 group collectives | [§6.5.x NeuronCodegen Collectives](../nki/neuroncodegen-collectives.md) | **MEDIUM** (group geometry) | `gen_all_worker_group` body lives in `parallel_state`, a module not walked here | disasm of `parallel_state.gen_all_worker_group` |
| `CollectiveKind` numeric resolution from Op class | [§6.5.x](../nki/neuroncodegen-collectives.md) / [§6.5.13](../nki/bircodegen-collective.md) | roster **CERTAIN**; the resolve step **HIGH** | the `CollectiveOp.so` name pool is read; the Op→kind mapping body is downstream | disasm of `CollectiveOp.so` resolver |
| The Shardy export `(dims, reshape_dims, transpose_perm)` perm arithmetic | [§13.x Shardy↔HloSharding bridge](../distribution/shardy-hlosharding-bridge.md) | **MEDIUM** — the call order is read, the perm vector is not traced bb-by-bb | the 279-bb `convertToHloSharding` was not traced block-by-block | bb-by-bb trace of `convertToHloSharding @0x2bc58f0` |

> **NOTE —** the one Cython module that is *not* obscured is `KernelBuilder.cpython-3xx.so`, which ships with full DWARF debug info ([§0.3 QUIRK](../methodology.md)). Pages that lean on it are in §3 below, not here, because there the body *was* read — through the line table.

---

## 3. DWARF-reconstructed — body read via the debug line table

`KernelBuilder.cpython-3xx.so` is the most readable binary in the stack: its DWARF line table maps every method to `KernelBuilder.py` line ranges. The NKI codegen pages ([Part 6](../nki/production-kernel-inventory.md)) use this to recover method *sequence* and call sites. This is strong evidence — stronger than string-only — but it is worth flagging that the underlying anchor is a **`<file>:<line>` map, not a disassembled opcode stream**: a `:NNN` source line cannot be re-verified against a stripped peer `.so`, and the line table reflects the source structure, not necessarily the emitted control flow after optimization.

| Claim class | Pages | Tag posture | The seam |
|---|---|---|---|
| `KernelBuilder` method call orders (`KB.py:NNNN` anchors throughout) | [§6.5.x NeuronCodegen* pages](../nki/neuroncodegen-collectives.md) | **CERTAIN** at method granularity via DWARF | the `:NNNN` line is from the debug table; trust the *method*, spot-check the *order* |
| `info.json` pre-seed in `NeffFileWriter` ctor | [§12.x NEFF header writer](../formats/neff-header-bom-writer.md) | **HIGH / MEDIUM**; the source-line anchor `0x1543eb0:228` is not used | a `:NNN` line cannot be verified against a stripped `.so`; the seed is corroborated (the string *is* loaded in the ctor) but not single-stepped to the insert call |

---

## 4. Cross-library / PLT-thunk — call site pinned, callee elsewhere

A recurring soft spot: the wiki pins a *call site* in `libwalrus.so` but the *callee body* lives in `libBIR.so` (imported as a PLT thunk) and was not traced. The *result use* is certain; the internal decode is taken on faith from a named model.

| Claim | Page | Tag on the page | Callee location | What would resolve it |
|---|---|---|---|---|
| `EngineAccumulationType` enum→bool decode (`getCalcStart`/`getCalcAccu`) | [§7.x Sim MatMul-MX](../bir/sim-matmul-mx.md) | **GAP** — result-use certain, bit-decode taken on faith | `libBIR` PLT thunks `@0x22b0a80` / `@0x22b1618` | disasm of the `libBIR` accessors |
| Per-function `key 19` ("auto psum accumulate") *authoring* pass | [§7.x](../bir/sim-matmul-mx.md) | **GAP** — the simulator *reads* it; the setter is a `libwalrus`/HLO concern | upstream authoring pass, not traced | trace the pass that writes key 19 |
| `DynamicAPINFO` setter bodies | [§5.x Symbolic AP register-ALU](../penguin/symbolic-ap-register-alu.md) | **GAP** | defined in `libBIR`, imported here | disasm of the `libBIR` setters |

---

## 5. Genuine gaps — values and paths simply not in the corpus

These are not "soft readings" — they are **absences**. The value, struct, or source does not exist in the shipped artifacts in any readable form. A reimplementer should treat each as a hole to fill from a fixture, a header, or hardware, not from this wiki.

### 5.1 Source-withheld NKI production leaves

The deepest gap in the book. [§6.6.4 Production Kernel Inventory](../nki/production-kernel-inventory.md) documents the three-tree topology: `nkilib` (open `.py`), `_pre_prod_kernels` (readable glue), and **`_private_kernels/` — 34 compiled Cython `.so`, zero `.py`, source withheld**.

| Gap | Page | Tag on the page | What would resolve it |
|---|---|---|---|
| Algorithm bodies of `attention`, `qkv`, `mlp`, `expert_mlps`, `router_topk`, `conv`, `collective_matmul`, `fused_linear`, `prefix_caching_attention`, `hw_ubench` | [§6.6.4](../nki/production-kernel-inventory.md) | entry-point symbols recoverable; algorithm bodies **not** — there is no readable twin short of decompiling the Cython `.so` | Cython-`.so` decompilation, or the upstream `.py` |
| (`blockwise_mm`, `llama3_transformer` are *not* in this gap — their production copy *is* the readable `_pre_prod` `.py`, merely Cython-compiled) | [§6.6.4](../nki/production-kernel-inventory.md) | recoverable — do not list as withheld | n/a |

### 5.2 AllToAll device lowering

The SPMD emitter draws an explicit boundary: it emits *stock* GSPMD `kAllToAll` HLOs, and the device-side rewrite/lowering "is where this page hands off" ([§13.x SPMD collective emission](../distribution/spmd-collective-emission.md)). The `AlltoAllOp` class and the `AllToAll` `CollectiveKind` name are both present in `CollectiveOp.so`, and the assert `Illegal AlltoAll without {split,concat}_dimension` is read ([§6.5.x](../nki/neuroncodegen-collectives.md)) — but the **device lowering body** (split/concat-dimension realization to BIR DMA) sits in downstream `xla::hilo` rewrite passes documented elsewhere, and is the genuine seam between emission and codegen.

| Gap | Page | Tag posture | What would resolve it |
|---|---|---|---|
| AllToAll → device DMA realization (split/concat dim handling) | [§13.x emission boundary](../distribution/spmd-collective-emission.md) + [§6.5.x](../nki/neuroncodegen-collectives.md) | class/kind **CERTAIN**; lowering body handed off, not traced on these pages | trace the `xla::hilo` AllToAll rewrite + the BIR DMA emit |

### 5.3 Pipeline-parallel / MPMD partition

Boundary markers for the per-layer pipeline cut are read directly as paired `Start`/`End` `kCustomCall` (`0x2B`) sentinels carrying `boundaryCount=<N>` ([§4.x boundary markers](../hlo-opt/boundary-markers-layer-cut.md)). What is *not* in the corpus is an end-to-end **MPMD / pipeline-parallel partition driver** that consumes those cuts into separate device programs — the markers are structural metadata "stripped before codegen", and the multi-program scheduler that would act on them is not a traced pass here.

| Gap | Page | Tag posture | What would resolve it |
|---|---|---|---|
| Pipeline-parallel / MPMD multi-program partitioner | [§4.x](../hlo-opt/boundary-markers-layer-cut.md) | marker mechanism **CERTAIN**; the consuming MPMD driver not traced, and possibly absent | a pass reading `boundaryCount` to cut programs, if one ships |

### 5.4 Stripped-packer and config-supplied values

| Gap | Page | Tag on the page | What would resolve it |
|---|---|---|---|
| `generateInstLoadActFuncSet` IT6 wire packing `(size<<16)\|((23−size)<<11)\|base` | [§9.x BKT-ctrl blob](../activation/bkt-ctrl-blob.md) | **GAP** — proven from bytes + consumer, not the packer (lib stripped) | the producing lib, un-stripped |
| `MaxCceDmaSource` numeric (per-arch `EngineInfo`/`Target` ctor, not JSON) | [§8.x DMA legalization](../walrus/dma-legalization.md) | **GAP** — only the deref path is pinned | disasm of the per-arch `Target` ctor |
| `DMAQueueAttribute` enumerator names (`num_queues`/`sync_type`/`priority_class`) | [§8.x DMA queues](../walrus/dma-queues.md) | **GAP** — written structurally; names unrecoverable | a `def.json` attribute table or un-stripped enum |
| MX 8-partition scale-block intra-quadrant packing | [§ MX matmul legality](../numerics/mx-matmul-legality.md) | **MEDIUM** — deduced from two rules, never checked against a NEFF fixture | a NEFF MX fixture |
| `sendrecv-to-gpsimd-max-bpp` knob *default* | [§8.x local collectives](../walrus/local-collectives.md) | **GAP / LOW** (the default specifically) | the knob's per-arch default ctor |

---

## 6. Flagged seams — well-supported but not fully traced

These claims are *well-supported* yet stop short of CERTAIN on their own page, because the decisive plumbing crosses a binary or strand boundary and was never traced through a single symbol. They are the most important rows to know about, because they read as solid and are *almost* solid.

| Seam | Page(s) | Grade on the page | The boundary |
|---|---|---|---|
| front-end `logical_nc_config` == backend `lnc_size` is one quantity *N* | [§13.8 LNC sharding constraint](../distribution/lnc-sharding-constraint.md), [§1.x LNC memory model](../arch/lnc-memory-model.md) | **HIGH** | the path from the CLI attribute to `PassOptions+0x1A4` spans the driver→penguin boundary and is not traced through a single symbol |
| ADDR4 bit-31 register-mode flag re-purposes byte 0 as an 8-bit register id | [§2.x ADDR4](../isa/addr4.md) | **CERTAIN** (the flag + packing); mode-nibble names at bits 29–30 **MEDIUM** | the register-mode flag itself is read from the packing store `mov byte[rbx],r15b @0x1508fca`; the soft part is the *naming* of the mode nibble. Bit 30 (ACTIVE) is only ever consumed — the encoder never sets it |
| `info.json` ctor pre-seed (also §3) | [§12.x NEFF header writer](../formats/neff-header-bom-writer.md) | **HIGH / MEDIUM** | corroborated by the string load in the ctor, not single-stepped to the insert call |
| `lnc_size` front-end default (2 on Trn2/`sunda`, else 1) seeds the SPMD mesh | [§1.x LNC memory model](../arch/lnc-memory-model.md) | **HIGH** | the CLI→`PassOptions` plumbing is not traced through one symbol on that page |
| `DependenceEdgeT` member layout | [§5.x backend dependence distance](../penguin/backend-dependence-distance.md) | **MEDIUM / GAP** | only the 40-byte size and the stored `Instruction*` are recovered |

---

## 7. Maintenance discipline

This ledger is a mirror. It must not present a directly-observed claim as weak, nor a deduction as observed — in either direction the reader loses the ability to weigh what they are reading. Three rules keep it honest:

- **Re-grade the source page first, then this row.** A grade that appears here and nowhere else is a bug in the ledger, not a finding.
- **Reproduce the substance, not just the tier.** Every row above names *why* the claim is soft and *what evidence would settle it*; a bare tier letter is not auditable.
- **Rows track the current state of the page.** Where a page's grade has moved — the NEFF writer's `info.json` pre-seed moved down from CERTAIN once the insert call proved untraced, and the ADDR4 mode-nibble naming separated from the register-mode flag it sits beside — this ledger carries the grade that stands now.

---

## See also

- [§0.3 Methodology & the Confidence Model](../methodology.md) — the four-tier ladder this page mirrors, and the category-level "not recoverable" catalog.
- [§11.1 The GPSIMD CPUs: 8-core Xtensa ELF Layout](../custom-ops/gpsimd-xtensa-layout.md) — the canonical structure-only page (§1 here).
- [§6.6.4 Production Kernel Inventory: the Three-Tree Story](../nki/production-kernel-inventory.md) — the source-withheld leaves (§5.1 here).
