# Open-Frontier Register

> *All addresses, counts, and sidecar figures on this page apply to `libtpu.so` v0.103 from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

Every page in this book asserts things about a stripped 745 MB binary, and a reconstruction is only auditable if it states its own edges as precisely as its interior. This appendix is that edge: the honest catalog of what is **not** yet fully resolved, kept as a live register rather than a disclaimer. It is the inverse of the rest of the wiki — instead of "here is the algorithm," each entry is "here is the question, here is exactly what evidence would close it, and here is the page that would own the answer."

The frontier sorts into five categories, and the distinction matters because they close by different means. **Decompilation walls** are functions Hex-Rays declined to lift — most are import stubs with no body to recover, a residual ~21 are genuine code that a manual disassembly pass would close. **Hardware-dependent facts** are values that static analysis structurally cannot confirm because they only exist on a powered TPU: runtime-populated framework vtable slots, live telemetry counters, flag defaults that resolve against device state. **Per-gen data gaps** are constants for older codenames that ship in one proto form but not another. **Inferred-link items** are edges the wiki traced by name-family agreement but did not byte-confirm at the leaf. And the **named open questions** were five specific tasks (`#1092`, `#1096`, `#1171`, and the `P-3-478..482` SparseCore/DMA cluster); a later recovery pass closed the `#1092`/`#1096`/`#1171` trio directly from the decompile — their deep pages are now full, two of them surfacing `CORRECTION`s — leaving only the `P-3-478..482` cluster genuinely open.

A register is only credible if it is also a graveyard for closed items. Several once-open claims were not just left open — they were *overturned* by later analysis, and those `CORRECTION`s are recorded here as CLOSED-by-correction to prove the register reflects current state, not the initial scratch hypotheses. The trailing-zstd blob, the "walrus" pass driver, and the naive demangle-rate count are the worked examples.

For an auditor, the contract of this page is:

- **The exact failure floors** — the 516 decompilation refusals and the 7,915 analysis problems, broken to the structural cause, so a reader knows which are knowledge gaps and which are noise.
- **The closeability grade per item** — a Confidence column that, uniquely on this page, grades *how confidently the gap can be closed* and by what evidence, not how confident the current claim is.
- **The owning page per item** — every open question routes to the page that would absorb its resolution, so closing the frontier is a navigable task list, not a wish.

| | |
|---|---|
| **Decompilation failures** | **516** (no `cfunc`) — owned by [methodology-deep](methodology-deep.md) |
| **— import/data stubs** | 486 (`0x22860108`–`0x228611xx` thunk band; not a knowledge gap) |
| **— hand-written assembly** | 9 (BoringSSL bignum/MD5, dnnl JIT kernels; no C source exists) |
| **— template/codegen giants** | ~21 (the genuine residual wall) |
| **Analysis problems** | **7,915** (6 types; `final` 4188 dominant) |
| **Named open tasks** | `P-3-478..482` SC/DMA cluster — the `#1092`/`#1096`/`#1171` trio was recovered in a later pass (now CLOSED) |
| **CLOSED-by-correction** | trailing-zstd blob · "walrus" · demangle-rate · per-gen geometry source |
| **Confidence column semantics** | *closeability of the gap*, not certainty of a present claim |

---

## The Frontier Register

The table below is the master index. Each row is one open item; the **Confidence** column grades how confidently the gap can be *closed* given the evidence named in the blocking-evidence column — `HIGH` means a bounded manual pass over identified addresses closes it, `LOW` means closing it needs evidence the binary does not contain (a powered device, a newer build). Rows that read `CLOSED` are kept to show the register is live; their detail is in the [§CLOSED-by-Correction](#closed-by-correction-the-graveyard) section.

| Open item | Category | Blocking evidence to close it | Owning page | Confidence (closeable) |
|---|---|---|---|---|
| ~21 template/codegen functions with no `cfunc` | Decompile wall | Manual disasm of named addresses; raise lift budget | [methodology-deep](methodology-deep.md) | HIGH |
| dnnl JIT + BoringSSL asm stubs unrecoverable as C | Decompile wall | None — assembly with no C source; read the disasm | [embedded-library-atlas](../forensics/embedded-library-atlas.md) | CERTAIN (won't improve) |
| PJRT vtable slots populated by framework at `Create` | HW-dependent | A live `PJRT_Client_Create` trace on a TPU | [client-and-device](../pjrt/client-and-device.md) | LOW |
| `FLAGS_enable_runtime_uptime_telemetry` live values | HW-dependent | On-device runtime; telemetry is read, not stored | [stream-executor-pjrt-adapter](../pjrt/stream-executor-pjrt-adapter.md) | LOW |
| Flag defaults that resolve against device state | HW-dependent | Device-resident config; static default may be a sentinel | [flag-catalog-full](flag-catalog-full.md) | LOW |
| `chip_config` (driver-side) vs `chip_parts` (geometry) split | Per-gen gap | xref `kChipConfigAliases` consumers per gen | [per-gen-comparison-matrix](per-gen-comparison-matrix.md) | MEDIUM |
| `issue_latency_cycle_count` absent in every embedded blob | Per-gen gap | A build whose `chip_parts` populates field 4 | [per-gen-comparison-matrix](per-gen-comparison-matrix.md) | LOW |
| 834 stream-op per-leaf `(pattern,verb,dtype,space)` opcode | Inferred link | Byte-dump the ISel matcher arm per leaf | [llvmtpu-intrinsic-table](llvmtpu-intrinsic-table.md) | MEDIUM |
| 890 default-builder ops' exact arity + result predicate | Inferred link | Decode each `verifyInvariantsImpl` body | [llvmtpu-intrinsic-table](llvmtpu-intrinsic-table.md) | HIGH |
| Per-intrinsic LLVM `IntrProperties` bits | Inferred link | Decode the `Intrinsic::getAttributes` table | [llvmtpu-intrinsic-table](llvmtpu-intrinsic-table.md) | MEDIUM |
| `#1092` structured-sparsity slot encoding | CLOSED (recovered) | Recovered: there is **no** dedicated sparsity bundle slot — sparsity rides in the packed MXU operand layout (`SparsityConfig`, 1:N restriction, SME outer-product gate) | [slot-sparsity-v5plus](../isa/slot-sparsity-v5plus.md) | CLOSED |
| `#1096` per-gen NOP canonical templates | CLOSED (recovered) | Recovered: two orthogonal no-ops — empty-slot predicate `kNeverExecute=31` fill + opcode-space all-ones NOP (`CORRECTION NOP-1`: the default bundle halts) | [nop-canonical](../isa/nop-canonical.md) | CLOSED |
| `#1171` `TpuVersion`-aware flag-prefix dispatch | CLOSED (recovered) | Recovered: codename-prefixed flags are registered unconditionally and applied gen-blind (`CORRECTION DISPATCH-1`); the active gen selects only data/codec, not flag gating | [flag-prefix-dispatch](../config/flag-prefix-dispatch.md) | CLOSED |
| `P-3-478` `InitializeOnScs` lookup-callback edge | Named open | Resolve the SC barrier callback target | [tensorcore-barrier](../barrier/tensorcore-barrier.md) | MEDIUM |
| `P-3-480` `LatencyTable::Create(TpuVersion)` factory tail | Named open | Finish the per-gen factory dispatch decode | [cycletable-family](../cost/cycletable-family.md) | HIGH |
| `P-3-481` `SetLatchIndices` per-gen overrun handshake | Named open | Decode the per-gen overrun branch | [latch-assignment-overrun](../sched/latch-assignment-overrun.md) | MEDIUM |
| `P-3-482` cmem-load / sparsity DMA edges | Named open | Trace the v4 cmem-load → sparsity DMA path | [slot-cmem-load-pf](../isa/slot-cmem-load-pf.md) | MEDIUM |
| trailing-zstd blob → per-codename constants | CLOSED | — overturned: no blob exists | [trailing-zstd-blob](../forensics/trailing-zstd-blob.md) | CLOSED |
| "walrus" pass-pipeline driver | CLOSED | — overturned: zero occurrences in binary | [glossary](../glossary.md) | CLOSED |
| naive `_Z`-prefix demangle rate (98%) | CLOSED | — overturned: field-backed rate is 93.0% | [methodology-deep](methodology-deep.md) | CLOSED |

> **NOTE —** the Confidence column on this page is deliberately *not* the four-level behavioral scale that [evidence-conventions](../front/evidence-conventions.md) defines for the rest of the book. Here it grades the **tractability of closing the gap**: `HIGH`/`MEDIUM` items are bounded static work this corpus already contains the inputs for; `LOW` items need a powered device or a different build; `CERTAIN (won't improve)` items are at their permanent floor.

---

## Decompilation Walls

### Purpose

The headline floor is **516 functions for which Hex-Rays returned no `cfunc`** — 0.058% of the 884,832 recovered functions. Read naively, "516 functions did not decompile" sounds like a 516-function knowledge gap. It is not. The [methodology-deep](methodology-deep.md) gap audit owns the full taxonomy; this section's job is to state which of the 516 are *frontier* (worth re-attacking) and which are permanently at the floor.

### The Three Structural Causes

A breakdown of the 516 by address band and demangled name puts every refusal into one of three buckets:

```text
516 decompilation refusals (no cfunc)
├─ 486  import / data stubs        0x22860108–0x228611xx (one shard)
│        strlen · free · getenv · abort · __cxa_finalize · __tls_get_addr
│        MallocExtension_Internal_* · sched_getcpu · eventfd
│        → PLT/GOT-style thunks; no local body exists to lift. NOT a gap.
├─   9  hand-written assembly      0x206ee040–0x2071e720 · 0x1b012c00 …
│        bn_sqr8x_mont · bn_power5_nohw · bn_mulx4x_mont_gather5 · md5_sha1_final
│        dnnl jit_avx512/jit_uni convolution kernels
│        → assembly with no C source; read the disasm, never the cfunc. Floor.
└─ ~21  template / codegen giants  the genuine residual wall
         mlir::Dialect::addOperations<…1000+ TF ops…>  @ 0xfedc180
         xla::jellyfish::ReduceEmitter::EmitReduction  @ 0x13e16240
         llvm::LiveIntervals::computeVirtRegInterval   @ 0x18e601e0
         llvm::X86II::getMemoryOperandNo · AtomicExpandImpl::run · RegAllocEvictModel
         xla::{viperfish,ghostlite}::*Performance ctors · DummyAlias*Printer
         → real code Hex-Rays declined on lift budget; a manual pass closes these.
```

> **GOTCHA —** the dominant 486 are import thunks in a single contiguous shard at `0x2286xxxx`. A reader who treats "516 failures" as 516 missing functions over-states the gap by ~24×. The actual frontier is the ~21 template/codegen functions; the 486 thunks resolve trivially by their symbol name and the 9 asm stubs are at their permanent floor. Cite the **~21**, not the 516, as the recoverable wall.

### What Closes Each Bucket

| Bucket | Count | Closeable by | Confidence (closeable) |
|---|---|---|---|
| Import / data stubs | 486 | Already closed by symbol name; no decompilation owed | CERTAIN |
| Hand-written assembly | 9 | Reading the disassembly; the `cfunc` will never exist | CERTAIN (floor) |
| Template / codegen giants | ~21 | Manual disasm pass over the named addresses; some lift with a raised budget | HIGH |

> **QUIRK —** the largest single refusal, `mlir::Dialect::addOperations<…>` at `0xfedc180`, is one C++ call that registers the entire TensorFlow MLIR op set — over a thousand op classes as template arguments in a single statement. It is not algorithmically interesting; recovering it yields a flat registration list, not logic. It sits on the frontier only because the decompiler refused it, not because a reimplementer needs its body. The `jellyfish::ReduceEmitter::EmitReduction` refusal (`0x13e16240`) is the inverse — genuinely interesting reduction-emission logic behind a deeply nested `btree_map` signature — and is the one template-explosion refusal worth a targeted manual pass.

### The 7,915 Analysis Problems

The second published floor is the `problems` sidecar: **7,915** IDA analysis problems, distinct from decompilation refusals and clustering by type, not by subsystem.

| Problem type | Count | What it is | Frontier? |
|---|---|---|---|
| `final` | 4,188 | Address finalized without full flow resolution | No — analysis bookkeeping |
| `rolled` | 1,659 | Instruction rolled into a prior analysis unit | No — bookkeeping |
| `disasm_problem` | 942 | A byte span IDA could not cleanly disassemble | Marginal — data-in-code edges |
| `bad_stack` | 574 | Stack-pointer delta unresolved at a point | Marginal — affects frame recovery |
| `head_problem` | 545 | Instruction-head boundary ambiguity | No — bookkeeping |
| `illegal_addr` | 7 | Reference to an address outside any segment | Yes — 7 anomalies worth a look |

> **NOTE —** the 7,915 problems are overwhelmingly analysis bookkeeping (`final` + `rolled` + `head_problem` = 6,392, 81%), not knowledge gaps. The only rows a frontier auditor should chase are the **7 `illegal_addr`** anomalies — references that point outside every defined segment, which usually mean either a relocation the loader resolves at runtime or a genuine analysis miss. They are the single most tractable problems-floor item, and they have no owning deep page yet.

---

## Hardware-Dependent Facts

### Purpose

A second class of frontier item cannot be closed by *any* amount of static work, because the fact does not exist in the binary — it only exists on a powered TPU. These items are flagged here precisely so no future page treats a static placeholder as the real value. The closeability Confidence for every item in this category is `LOW`: closing them needs a live device, which is outside the static-RE method this book is built on.

### Runtime-Populated Framework Vtable Slots

`libtpu.so` is a PJRT plugin exporting one C symbol, `GetPjrtApi`, returning a 140-slot `PJRT_Api` vtable ([API & vtable Reconstruction](../pjrt/api-vtable-reconstruction.md)). Of those slots, five are TPU-overridden and statically resolvable — slot 15 points at `tpu_plugin::PJRT_Client_Create` (`0xE6A8840`), confirmed in the binary. But the *contents* of the `PJRT_Client` object that `Create` builds — the per-device handles, the `tpu::System*` shared pointer, the wired device/memory/topology slots — are populated at runtime by probing hardware. The static binary shows the *construction code* ([client-and-device](../pjrt/client-and-device.md)); it cannot show the *resulting object graph*, because that depends on how many cores the device enumerates and what `tpu::System::Initialize` (`0x1D0AE420`) discovers.

> **NOTE —** the wiki traces `PJRT_Client_Create` → `GetTpuPjRtClient` (`0xF8008C0`) → `xla::TpuClient` construction fully as *code*. What it cannot trace is the post-construction state: how many `xla::TpuDevice` objects exist, what each `TpuCoreLocation` resolves to, what the throttle `Semaphore` permits. Those are runtime facts. A reimplementer rebuilding the client from the code path will get the *shape* right; the *population* is a live-trace question.

### Live Telemetry Values

The construction path reads `FLAGS_enable_runtime_uptime_telemetry` and, when set, merges uptime telemetry into the client config. The flag's existence and its gate are static; the telemetry *values* it streams are runtime counters with no static representation. Any page describing telemetry content is describing a wire schema, not observed values.

### Flag Defaults That Resolve On-Device

The [flag catalog](flag-catalog-full.md) recovers each flag's static default from the binary. A subset of flags carry a static *sentinel* default that the runtime overrides against device state at boot (e.g. a `-1`/`0` placeholder that means "ask the hardware"). For those, the static default is not the effective default. The frontier item is: which flags carry sentinels, and what device-state rule resolves each. This is closeable only with a device, hence `LOW`.

> **GOTCHA —** a reimplementer who reads a flag's static default as its effective default will mis-configure any flag whose real value is device-derived. The static catalog is correct about *what the binary stores*; it is silent about *what the runtime substitutes*. Treat any default that looks like a sentinel as unresolved until confirmed on hardware.

---

## Per-Gen Data Gaps

### Purpose

Older-codename constants are a frequently-assumed gap that turns out to be **mostly closed** — and the register's job is to say so precisely rather than leave a vague "older gens may be incomplete." The genuine residual is narrow.

### What Is Already Closed

The hinted gap — "older codenames ship only as `chip_configs`, not `chip_parts`" — does not hold for this build. The [per-gen comparison matrix](per-gen-comparison-matrix.md) confirms that **all nine `<codename>_chip_parts.binarypb` blobs (v2 through v7) are embedded** contiguously in `.lrodata` at `0xbdf29a0..`, parsed by `TpuChipParts::DefaultsForVersion` (`0x20b1b040`). The older-gen lane/sublane/MXU/memory constants are therefore proto-sourced and materializable, **not inferred**. The HBM/VMEM/SMEM/SFLAG bytes, the MXU `VectorIsa`, and per-gen frequencies all decode straight from the wire bytes.

> **CORRECTION (PGM-1, summarized) —** the per-gen geometry is **not** written by `tpu::TpuChipConfig::Create` (`0x20ae98e0`), as an early framing assumed. `TpuChipConfig::Create` builds the *driver-side* memory/queue layout via the `kChipConfigAliases` flat-map (`0x2200b8b0`); the lane/sublane geometry is a `TpuChipParts`/`TpuTopology` property. This correction is owned in full by [per-gen-comparison-matrix](per-gen-comparison-matrix.md); it is logged here as a CLOSED per-gen item so this register reflects the resolved state.

### The Genuine Residual

| Residual gap | Why it is open | Closeable by | Confidence |
|---|---|---|---|
| `chip_config` (driver) vs `chip_parts` (geometry) consumer split | The two proto families serve different layers; which gen reads which `kChipConfigAliases` entry is not fully xref'd | xref `kChipConfigAliases` consumers per `TpuVersion` | MEDIUM |
| `issue_latency_cycle_count` (`VectorIsa` field 4) | **Absent (proto default 0) in every embedded blob** — real per-gen issue latency lives in the cost-model `Performance` grids, not `chip_parts` | A build whose `chip_parts` populates field 4 — may never exist | LOW |

> **GOTCHA —** `issue_latency_cycle_count` is the trap. The field exists in the `chip_parts` schema but is never populated in this build (it reads as proto default 0 for all gens). A reimplementer must **not** read MXU/VPU issue latency from `chip_parts`; the live value is queried through `CycleTable::GetCyclesForThroughput` (per-gen vtable slot `+0x10`). The `chip_parts` zero is not the answer — it is the absence of an answer.

---

## Inferred-Link Items

### Purpose

The fourth category is edges the wiki traced by *name-family agreement* — confirming the class→engine mapping and locating the lowering pass — but stopping short of byte-dumping the leaf encoding. These are graded `INFERRED` or `I` on their owning pages; they are genuine frontier because the conclusion is trustworthy while the exact leaf is not yet re-verified.

### The Intrinsic→ISel Leaf Gaps

The [LLVMTPU intrinsic table](llvmtpu-intrinsic-table.md) recovers all **1356 distinct `llvm.tpu.*` intrinsics** two independent ways. Three leaf-level facts remain inferred:

| Inferred link | What is known | What is not yet byte-confirmed | Confidence (closeable) |
|---|---|---|---|
| 834 stream ops | class→engine mapping confirmed; lowering pass located | the per-`(pattern,verb,dtype,memspace)` numeric stream-engine command value | MEDIUM |
| 890 default-builder ops | name-family arity recovered | exact operand count and result `TypeConstraint` per op (in each `verifyInvariantsImpl`) | HIGH |
| Per-intrinsic `IntrProperties` | OpInterface presence known (`MemoryEffect`, `AliasAnalysis`, `AccessGroup`, `Bytecode`) | the `IntrNoMem`/`IntrArgMemOnly`/`IntrWillReturn` bits each carries | MEDIUM |

> **QUIRK —** the 834-way stream-op explosion *is* the encoding — there is no single parameterized `llvm.tpu.stream` op. The frontier here is not "find the parameterization" (there is none); it is "byte-dump the matcher arm for each of the 834 leaves." That is bounded work, but 834 leaves of it, which is why the closeability is `MEDIUM` rather than `HIGH` despite the path being known.

### A Byte-Confirmed Counter-Example

Not every intrinsic→ISel link stayed inferred. The SparseCore `addrspacecast` family (16 ops) was traced end-to-end and *corrected* a common assumption:

> **CORRECTION (INTR-2, summarized) —** the SparseCore `addrspacecast` intrinsics do **not** lower to `ISD::ADDRSPACECAST` (`0xf4`) nodes; they survive as LLVM-IR intrinsic calls absorbed by the consuming SC load-store ISel. A whole-`.text` xref placed every `0xf4` constructor caller in generic LLVM, none in the TPU/SC bands. This is owned by [addrspacecast-isel](../sparsecore/addrspacecast-isel.md); it is cited here as the worked example of an inferred link that closure *upgraded into a correction* — the frontier is not only "fill gaps," it is "re-verify and overturn."

---

## Named Open Questions

### Purpose

This band was originally five `O`-graded tasks whose deep pages existed as stubs with no completed raw-findings file. A later recovery pass closed the `#1092`/`#1096`/`#1171` trio directly from the decompile — their deep pages are now full, and two of them surfaced `CORRECTION`s in the process. Only the `P-3-478..482` SparseCore/DMA cluster remains genuinely open. The table below records all five for the audit trail.

### The Five Tasks (three now recovered)

| Task | Topic | Owning page (current state) | What closed / closes it | Confidence |
|---|---|---|---|---|
| `#1092` | Structured-sparsity slot encoding (v5+) | [slot-sparsity-v5plus](../isa/slot-sparsity-v5plus.md) — RECOVERED | Closed: no dedicated slot; sparsity lives in the packed MXU operand layout | CLOSED |
| `#1096` | Per-gen NOP canonical templates | [nop-canonical](../isa/nop-canonical.md) — RECOVERED | Closed: predicate `kNeverExecute=31` fill + all-ones opcode NOP (`CORRECTION NOP-1`) | CLOSED |
| `#1171` | `TpuVersion`-aware flag-prefix dispatch | [flag-prefix-dispatch](../config/flag-prefix-dispatch.md) — RECOVERED | Closed: flags registered gen-blind (`CORRECTION DISPATCH-1`) | CLOSED |
| `P-3-478..482` | SparseCore / DMA edges (cluster) | barrier / cost / sched / isa pages | Resolve the five SC/DMA callback and DMA-edge traces | MEDIUM |

The `P-3-478..482` cluster is a band of related SparseCore and DMA edges that feed completed pages but are not themselves fully recovered: `P-3-478` is the `InitializeOnScs` lookup-callback target ([tensorcore-barrier](../barrier/tensorcore-barrier.md)); `P-3-480` is the tail of `LatencyTable::Create(TpuVersion)` factory dispatch ([cycletable-family](../cost/cycletable-family.md)); `P-3-481` is the `SetLatchIndices` per-gen overrun handshake ([latch-assignment-overrun](../sched/latch-assignment-overrun.md)); and `P-3-482` carries the cmem-load/sparsity DMA edges ([slot-cmem-load-pf](../isa/slot-cmem-load-pf.md)).

> **NOTE —** `#1171` is now CLOSED — `flag-prefix-dispatch` was recovered and its `OPEN` banner replaced with a full decode (`CORRECTION DISPATCH-1`: codename-prefixed flags are registered gen-blind; the active gen drives only data/codec selection). The row is retained here as a closed-by-recovery audit entry, the same way the trailing-zstd and walrus items are kept in the graveyard below.

---

## CLOSED-by-Correction (the Graveyard)

A register that only ever grows is a wish-list, not an audit. These items were open in early analysis and are now **closed by correction** — later evidence overturned the original claim. They are retained, not deleted, so a reader can see the register reflects current truth rather than initial guesses.

| Closed item | Original claim | What overturned it | Owning correction |
|---|---|---|---|
| Trailing zstd blob | ~4.1 MB zstd-dictionary blob appended past EOF at `0x20F99BEF`, decoding to per-codename HW constants | The offset is `~2.6 MB` *inside* `.text`; the bytes are an x86-64 `mov` immediate; no dictionary, no frame, no payload | `CORRECTION (ZSTD-01)` — [trailing-zstd-blob](../forensics/trailing-zstd-blob.md) |
| "walrus" pass driver | An IR/compiler pass-pipeline driver named "walrus" | Case-insensitive search of name **and** string tables returns **zero** occurrences; the real driver is the HLO pass registry, ungated by any "walrus" symbol | `CORRECTION (GLOSS-1)` — [glossary](../glossary.md) |
| Demangle rate (98%) | 98% of functions carry a demangled name (from the `_Z`-prefix count) | `_Z`-prefix overshoots: ~48,500 of the 871,370 `_Z` names fail to demangle or demangle to themselves; the field-backed rate is **822,847 / 884,832 = 93.0%** | `CORRECTION (METH-D1)` — [methodology-deep](methodology-deep.md) |
| Per-gen geometry source | Lane/sublane geometry written by `tpu::TpuChipConfig::Create` | `TpuChipConfig::Create` builds only the driver-side layout; geometry is a `TpuTopology` property from the MXU `VectorIsa` | `CORRECTION (PGM-1)` — [per-gen-comparison-matrix](per-gen-comparison-matrix.md) |

> **QUIRK —** the trailing-zstd closure is the strongest argument for keeping a register at all. The original claim had a file offset, a size, a compression format, and a decode target — it was specific enough to *sound* recovered. It was wrong on every load-bearing fact. The defense was a single cross-check: resolve the anchor offset against the symbol map before carving. Every CLOSED row here is a reminder that a precise-sounding claim with one missing cross-check is exactly where a reconstruction goes wrong.

---

## Cross-References

- [Extraction Methodology, Deep](methodology-deep.md) — owns the 516 + 7,915 failure taxonomy and the `METH-D1` demangle correction; the parent of every count on this page
- [Evidence & Confidence Conventions](../front/evidence-conventions.md) — the four-level behavioral confidence scale this page deliberately diverges from (here Confidence grades closeability)
- [Trailing zstd Blob](../forensics/trailing-zstd-blob.md) — the `ZSTD-01` correction; the worked example of a closed-by-overturn frontier item
- [Per-Gen Comparison Matrix](per-gen-comparison-matrix.md) — the per-gen `chip_parts` decode and the `PGM-1` geometry-source correction
- [LLVMTPU Intrinsic Table](llvmtpu-intrinsic-table.md) — the inferred stream/builder/`IntrProperties` leaf gaps and the `INTR-2` addrspacecast correction
- [Client, Device & Topology](../pjrt/client-and-device.md) — the `PJRT_Client` construction path whose runtime population is hardware-dependent
- [StreamExecutor → PJRT Adapter](../pjrt/stream-executor-pjrt-adapter.md) — the `TpuClient` bridge and the runtime telemetry gate
- [Flag Catalog, Full](flag-catalog-full.md) — the static flag defaults whose on-device sentinels are unresolved
- [Slot Sparsity v5+](../isa/slot-sparsity-v5plus.md) · [NOP Canonical](../isa/nop-canonical.md) · [Flag-Prefix Dispatch](../config/flag-prefix-dispatch.md) — the `#1092`/`#1096`/`#1171` trio, recovered in a later pass and retained here as closed-by-recovery
- [Embedded Library Atlas](../forensics/embedded-library-atlas.md) — the BoringSSL/dnnl statically-linked code whose hand-written assembly is at its permanent decompilation floor
