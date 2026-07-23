# GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover

> *All symbols and addresses on this page are read from the **cp310** wheel of `neuronx_cc` 2.24.5133.0+58f8de22. The backend wire encoder lives in `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, stripped; `.text`/`.rodata` VMA == file offset, `.data` is `+0x400000`); the IR-side handles (verify / `isCompatible` / JSON) live in `libBIR.so`; the reference datapath kernel lives in `libBIRSimulator.so`. cp311 shares this logic but not the BuildID; cp312 differs in size — re-confirm any raw address against the target wheel. See [Build & Version Provenance](../reference/versions.md). Treat every address as version-pinned.*

## Abstract

In this toolchain the name **"GPSIMD" is not a programmable SIMD core.** It is the **external alias of the internal Pool engine** — the backend prints `EngineType` `Pool(1)` to the outside world as the string `GPSIMD` — and it is the home of exactly **one** dedicated machine instruction: `InstGPSIMDSB2SB`, a fixed-function **cross-core State-Buffer → State-Buffer copy** that swaps an SBUF tile between the two physical cores of a 2-core Logical NeuronCore (LNC2). There is no GPSIMD `add`/`mul`/`custom-kernel` opcode in the BIR ISA; the engine's entire user-visible ISA surface is this single mover. The alias is recovered verbatim from the diagnostic string `ExternalEngineType used as EngineType. External: GPSIMD Internal: Pool` in `libBIR.so`, and from `InstGPSIMDSB2SB::getDefaultEngine() @0x3e2cf0`, which is literally `mov $0x1; ret` → `Pool(1)`.

The page reverse-engineers four things a reimplementer must reproduce. **(1) The one instruction** — `InstGPSIMDSB2SB`, opcode `0xBF`, a 64-byte `S3D3_COLLECTIVE` wire bundle emitted by the sole encoder `CoreV3GenImpl::visitInstGPSIMDSB2SB @0x1359840`. **(2) The cross-core addressing** — one bundle reads this core's SBUF (a full `TENSOR3D` access pattern) and writes the *peer* core's SBUF, addressing the peer by a **single scalar partition address** plus a one-bit "go cross-core" enable; there is **no core-index field and no core-count field**. **(3) The LNC2 hard-coding** — the op is legal only when the per-arch `cores-per-LNC` field (`arch+0x1A4`, the same field [1.07](lnc-memory-model.md) calls `lnc_size`) equals **2**; the encoder, the libBIR legality gate, and the simulator each compare it to the literal `2`. **(4) The GPSIMD-vs-DMA decision** — `lower_local_collectives` picks GPSIMD over DMA only for *small*, shape-compatible on-chip SendRecv swaps, under a hard `≤1024 bytes/partition` ceiling that lives in `libBIR`, not the encoder.

> **GOTCHA — three unrelated "GPSIMD"s ship in this wheel; this page documents exactly one.** (a) **This page's GPSIMD** = the backend-internal *external alias of the `Pool` engine* (`EngineType Pool(1)`), whose only op is the SB2SB mover. (b) A **separate Xtensa custom-op CPU cluster** — the `libbuiltincustomop_cpu0..7.stripped.so` libraries are compiled with `XtensaTools-14.09 clang version 10.0.1`; that is a programmable general-purpose processor array for user custom ops, documented in [Part 11 — Custom Ops & GPSIMD](../customop/), and has **nothing to do with `InstGPSIMDSB2SB` or the Pool engine.** (c) The **NKI front-door `nki.isa.engine.gpsimd = 3`** ("GpSimd Engine", "eight GpSimd cores ... 16 contiguous SBUF partitions") is a *third* namespace — the silicon programmable GP-SIMD array surfaced to kernel authors at engine index `3`, again **distinct** from this backend's `Pool(1)` alias. A reimplementer who conflates the Pool-alias SB2SB mover with the Xtensa cluster or the NKI engine-3 array will mis-model all three. Whenever this page says "GPSIMD" unqualified, it means **(a): the Pool alias.**

For reimplementation, the contract is:

- **The Pool-alias identity** — GPSIMD is the printed name of `EngineType Pool(1)`; the SB2SB op runs on the Pool sequencer, not a separate engine.
- **The one machine op** — `InstGPSIMDSB2SB` (InstructionType 33, opcode `0xBF`), its 64-byte `S3D3_COLLECTIVE` bundle, and the field walk that fills it.
- **The cross-core addressing model** — local src/dst `TENSOR3D` + one scalar peer partition address + a one-bit enable; *why* no core index exists (the LNC has exactly two cores).
- **The LNC2 gate** — `arch+0x1A4 == 2` at three byte sites, and the fact that CoreV4 *inherits* this encoder unchanged (it does **not** generalize the topology to >2 cores).
- **The decision** — minted by `lower_local_collectives` from on-chip `SendRecv` only, gated by `isCompatible` and the `≤1024 B/partition` ceiling; everything larger/incompatible falls to DMA.

| | |
|---|---|
| **What "GPSIMD" is** | external alias of `EngineType Pool(1)` — `External: GPSIMD Internal: Pool` |
| **The one machine op** | `bir::InstGPSIMDSB2SB` — InstructionType 33, opcode `0xBF` (191) |
| **Sole encoder** | `CoreV3GenImpl::visitInstGPSIMDSB2SB @0x1359840` (libwalrus; no CoreV2 body, CoreV4 inherits) |
| **Engine** | `getDefaultEngine() @0x3e2cf0` = `mov $0x1; ret` → `Pool(1)` |
| **Wire bundle** | `NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT` — 64 bytes, `fwrite 0x40` |
| **Cross-core addressing** | local src/dst `TENSOR3D` (`+0x10`/`+0x30`) + one scalar peer SB addr (`+0x21`) + enable (`+0x20`=1) |
| **LNC2 gate** | `cmpl $0x2,0x1a4(%rax)` @ `0x1359a67`/`0x1359da1`; `isCompatible cmp $0x2,%rdi` @ `0x2931f2`; sim `getNumCoresPerLNC()==2` @ `0x1f75ba` |
| **Hard size ceiling** | `InstGPSIMDSB2SB::MaxBytesPerPartition @0x784a00` = `0x400` = **1024** B/partition (libBIR) |
| **Pass knob** | `sendrecv-to-gpsimd-max-bpp` — "Bytes/partition under which local swapping SendRecvs will be mapped to GPSIMDSB2SB" |
| **ArithOps** | `ArithOps() @0x3e2ef0` → `ArithOpsZeroArithInst @0x49d490` — **zero** MACs (pure data move) |
| **Per-gen cost** | `Gen3Hwm::getInstGPSIMDSB2SBLatency @0x185ca10`, `CoreV4Hwm:: @0x1861ea0` |

---

## At a glance: what GPSIMD is, and is not

```text
   ┌───────────────────────── THREE THINGS NAMED "GPSIMD" ──────────────────────────┐
   │                                                                                │
   │  (a) THIS PAGE — the Pool-alias SB2SB mover                                     │
   │      EngineType Pool(1)  ──printed as──►  "GPSIMD"                              │
   │      one op: InstGPSIMDSB2SB (opcode 0xBF), a cross-core SBUF↔SBUF copy.        │
   │      String: "ExternalEngineType used as EngineType. External: GPSIMD          │
   │               Internal: Pool"  (libBIR)                                         │
   │                                                                                │
   │  (b) Xtensa custom-op CPU cluster  ──►  [Part 11 / customop]                    │
   │      libbuiltincustomop_cpu0..7.stripped.so, XtensaTools-14.09 clang.           │
   │      A programmable processor array for USER custom ops. NOT the Pool engine.   │
   │                                                                                │
   │  (c) NKI front-door engine  ──►  nki.isa.engine.gpsimd = 3  ("GpSimd Engine")   │
   │      The silicon programmable GP-SIMD array (8 cores × 16 partitions) exposed   │
   │      to kernel authors at engine index 3. A SEPARATE namespace from Pool(1).    │
   │                                                                                │
   └────────────────────────────────────────────────────────────────────────────────┘

   The SB2SB cross-core copy, drawn for LNC2 (the only legal topology):

        physical core 0                              physical core 1
        ┌──────────────────────┐                     ┌──────────────────────┐
        │ SBUF (private, full) │  one 0xBF bundle    │ SBUF (private, full) │
        │   src TENSOR3D ──────┼──► reads local src  │                      │
        │   @bundle+0x10       │    writes peer dst ─┼──► peer SB partition │
        │   dst TENSOR3D       │    addr @bundle+0x21│    (the scalar addr) │
        │   @bundle+0x30       │    enable @+0x20 = 1 │                      │
        └──────────────────────┘                     └──────────────────────┘
              "this core"                                  "the one peer"
        no core-index field — "the other core" is unambiguous when N == 2.
```

The mover is a **peer-to-peer point copy between two private SBUFs**, not a window into a shared address space — exactly the on-chip exception discussed in [1.07 The multi-core (LNC) memory model](lnc-memory-model.md). It does not make any SBUF address visible to another core's allocator; it copies bytes.

---

## The Pool-Alias Identity

### Purpose

Before any encoding detail matters, a reimplementer must internalize that **GPSIMD is a name, not an engine.** The backend has a single `Pool` sequencer ([1.09 Pool Engine](pool-engine.md)) — the windowed-reduction leg that also carries RNG and on-chip collective-compute ops — and it is *printed* to the outside world (artifacts, debug ASM filenames, ISA dumps) under the external name `GPSIMD`. The SB2SB mover runs on that same `Pool(1)` sequencer.

### The Two Names, One Engine

`bir::EngineType2ExternalName @0x47fca0` maps the internal `EngineType` enum to its external string. For `Pool(1)` the external string is `GPSIMD`. The libBIR string table carries the guard message that fires when the two are confused:

```text
ExternalEngineType used as EngineType. External: GPSIMD Internal: Pool
```

Both `Pool` and `GPSIMD` appear in the `EngineType` string pool (the internal `EngineType2string @0x47fa80` and external `EngineType2ExternalName @0x47fca0` tables), confirming they are two surface names for one engine slot. *Anchors: string verbatim in `libBIR.so`; `EngineType2ExternalName @0x47fca0` and `EngineType2string @0x47fa80` symbols present.*

### Proof the SB2SB Op Runs on Pool

`InstGPSIMDSB2SB::getDefaultEngine()` returns the engine the IR node binds to. Its body is two instructions:

```asm
; bir::InstGPSIMDSB2SB::getDefaultEngine() @0x3e2cf0  (libBIR)
3e2cf0:  b8 01 00 00 00       mov    $0x1,%eax        ; EngineType Pool(1)
3e2cf5:  c3                   ret
```

The constant `1` is `EngineType Pool`. Combined with the `External: GPSIMD Internal: Pool` alias, the chain is airtight: **the op named `GPSIMDSB2SB` defaults to the `Pool` engine, which is externally named `GPSIMD`.** *Anchors: `getDefaultEngine @0x3e2cf0` re-disassembled; `mov $0x1` = `Pool(1)`.*

### It Is a Pure Data Move

`InstGPSIMDSB2SB::ArithOps() @0x3e2ef0` routes to `StaticProfiler::ArithOpsZeroArithInst @0x49d490` — the zero-arithmetic classifier. The op counts **zero** MACs: it is a mover, not a compute op. The only transform it may carry is a per-side dtype reinterpret/cast (the `+0x0C`/`+0x0D` wire dtypes, and the simulator casts the moved `MemObj` to FP32); no reduction or ALU is applied by the op itself. *Anchors: `ArithOps @0x3e2ef0` → `ArithOpsZeroArithInst @0x49d490`.*

> **NOTE — GPSIMD is *not* a "run arbitrary code" instruction class.** The silicon GP-SIMD array (the NKI engine-3 array, eight cores × 16 partitions) is programmable, but the **compiler's exposed BIR ISA surface for the Pool-alias GPSIMD is exactly one fixed-function op**: the SB2SB copy. There is no GPSIMD `add`/`mul`/kernel opcode. User custom code targets `CustomOp`/`NKIKernel` instruction classes and (for CPU custom ops) the Xtensa cluster ([Part 11](../customop/)) — not a GPSIMD opcode. *Anchors (the ISA surface): exactly one `GenImpl::visitInstGPSIMDSB2SB` encoder symbol in `libwalrus.so`; the HW-programmability of the silicon array is out of binary scope.*

---

## The Instruction — `InstGPSIMDSB2SB` (IT 33, opcode 0xBF)

### Purpose

The single GPSIMD machine op. It copies an SBUF tile from this physical core into the peer core's SBUF in one 64-byte bundle. The reimplementer's job here is the wire layout and the field walk that fills it.

### The L1 IR Handle (libBIR)

`InstGPSIMDSB2SB` is a first-class `bir::Instruction` subclass with **named** operands (not a generic copy): `getIfmap()` reads the source AP at `inst+0xA8`, `getDst()` reads the output AP at `inst+0xC8`. The full handle set, all present as symbols:

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `getDefaultEngine()` | `0x3e2cf0` | `mov $0x1` → `Pool(1)` | CERTAIN |
| `ArithOps()` | `0x3e2ef0` | → `ArithOpsZeroArithInst @0x49d490` (zero MACs) | CERTAIN |
| `getValidEngines()` | `0x43f1f0` | loads `validEngines` table (runtime-populated; gen3+ only) | CERTAIN |
| `verify(arch, emitErr)` | (libBIR `verify`) | first call from the encoder; tail-calls `isCompatible` | CERTAIN |
| `isCompatible(ctx, mod, srcAP, dstAP, I)` | `0x2931d0` | the legality gate (ctx==2, ranks, SBUF, ≤1024 B) | CERTAIN |
| `getEffectiveBytesPerPartition(elems, dtype)` | `0x291a50` | dtype-padded byte budget feeding the ceiling | CERTAIN |
| `MaxBytesPerPartition` | `0x784a00` | `.rodata` constant = `0x400` = 1024 | CERTAIN |
| `hasSideEffect()` / `canDstBePartialAccess()` / `isSymbolic()` | weak | side-effect / partial-dst / symbolic predicates | CERTAIN |
| `createFromJson` / `evalFieldsInto` / `updateAffineExprs` | — | JSON round-trip + affine plumbing | CERTAIN |

*All symbols above are enumerated from `nm -DC libBIR.so`.*

### The 64-Byte Wire Bundle (`S3D3_COLLECTIVE`)

The encoder stamps the header (`opcode = 0xBF`, `inst_word_len = 16` dwords = 64 bytes), fills the sync-wait/update region (inherited verbatim from the CoreV2 sync skeleton), then writes the cross-core payload, and `fwrite()`s exactly `0x40` bytes. The sync region is named by the `setupSyncWait/Update<NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT>` template (both symbols present in `libwalrus.so`).

| Offset | Sz | Field | Source (encoder) | Anchor | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xBF` | `setupHeader` (`movb $0xbf`) | `@0x1359a15` | CERTAIN |
| `+0x01` | 1 | `inst_word_len` = 16 (⇒ 64 B) | `setupHeader` | — | CERTAIN |
| `+0x02` | 2 | reserved = 0 | `setupHeader` | — | CERTAIN |
| `+0x04` | 1 | wait[0] mode (wire LUT) | sync-wait helper | — | CERTAIN |
| `+0x05` | 1 | wait[0] semaphore index | `SyncRef::getId` | — | CERTAIN |
| `+0x06` | 1 | update mode (wire LUT) | sync-update helper | — | CERTAIN |
| `+0x07` | 1 | update semaphore index | `SyncRef::getId` | — | CERTAIN |
| `+0x08` | 4 | wait/update value | shared dword | — | CERTAIN |
| `+0x0C` | 1 | **src dtype** (this core) | dtype-tag(srcAP.Dtype) | `mov %al,0xc(%r13)` `@0x1359a3d` | CERTAIN |
| `+0x0D` | 1 | **dst dtype** (this core) | dtype-tag(dstAP.Dtype) | `mov %al,0xd(%r13)` `@0x1359a54` | CERTAIN |
| `+0x10` | 16 | **SRC `TENSOR3D`** (local source tile) | `assignAccess<TENSOR3D>(buf+0x10, srcAP)` | `lea 0x10(%r13)` | CERTAIN |
| `+0x20` | 1 | **cross-core enable = 1** (LNC2 marker) | const 1 | `movb $0x1,0x20(%r13)` `@0x1359a74` | CERTAIN |
| `+0x21` | 1 | **peer-core SB partition addr** | 2nd APPair element `*(*(srcAP+0x50)+8)` | `mov %al,0x21(%r13)` `@0x1359a9f` | CERTAIN |
| `+0x30` | 16 | **DST `TENSOR3D`** (destination tile) | `assignAccess<TENSOR3D>(buf+0x30, dstAP)` | `lea 0x30(%r13)` | CERTAIN |
| `+0x40` | — | end (`fwrite 0x40`) | `fwrite` | `@0x1359ada` | CERTAIN |

> **GOTCHA — the peer address must exist, or the encoder asserts.** The peer partition address is the **second** element of the source AP's `APPair` vector. The encoder reads `*(*(srcAP+0x50)+8)`; if the `APPair` count (`[srcAP+0x58]`) is zero, the underlying `SmallVector` fires an `idx < size()` assert. A reimplementer must ensure the upstream lowering (`lower_local_collectives`, below) has populated the second `APPair` element with the resolved peer address before the encoder runs. *Anchors: the 2nd-element read is the source of `bundle+0x21`.*

### Algorithm

```c
// CoreV3GenImpl::visitInstGPSIMDSB2SB @0x1359840  (libwalrus)
function visitInstGPSIMDSB2SB(InstGPSIMDSB2SB &I):
    arch = currentArchModel()
    // FIRST statement: legality gate — verify tail-calls isCompatible (libBIR).
    if !I.verify(arch->lnc_ctx /* = arch+0x1A4 */, /*emitError=*/1):   // @0x1359870/76
        return                                       // rejected before any byte is written

    setupHeader(bundle, /*opcode*/0xBF)              // movb $0xbf @0x1359a15; word len = 16
    setupSyncWait <S3D3_COLLECTIVE>(bundle, I)       // +0x04..+0x08, CoreV2 skeleton
    setupSyncUpdate<S3D3_COLLECTIVE>(bundle, I)

    bundle[+0x0C] = dtypeTag(I.getIfmap().Dtype)     // mov %al,0xc(%r13)  @0x1359a3d
    bundle[+0x0D] = dtypeTag(I.getDst().Dtype)       // mov %al,0xd(%r13)  @0x1359a54
    assignAccess<TENSOR3D>(bundle+0x10, I.getIfmap()) // local SBUF source tile

    // ---- THE LNC2 GATE (encoder, GENERATE path) ----
    if arch[+0x1A4] != 2:                            // cmpl $0x2,0x1a4(%rax) @0x1359a67
        NeuronAssertion(code=936,                    //   actual=<version>, supported="2"
                        "GPSIMDSB2SB.cpp:745")
    bundle[+0x20] = 1                                // cross-core enable; movb $0x1 @0x1359a74
    bundle[+0x21] = peerSBAddr(I)                    // 2nd APPair elem; mov %al @0x1359a9f

    assignAccess<TENSOR3D>(bundle+0x30, I.getDst())  // destination tile
    fwrite(bundle, 1, 0x40, bin)                     // @0x1359ada — exactly 64 bytes
```

A parallel **CHECK** path (`RUN_ISA_CHECKS`, no `fwrite`) repeats the same gate at `@0x1359da1` (`cmpl $0x2,0x1a4(%rax)`) and the same enable stamp, validating into stack scratch. *Anchors: both `cmpl $0x2,0x1a4` sites and the two `movb $0xbf` seeds disassembled.*

---

## Cross-Core Addressing — How the LNC Peer Is Named

### Purpose

This is the headline. The 2-core LNC addressing model uses **two addresses per copy**, asymmetric: a full access pattern for the *local* endpoints, but only a single scalar for the *remote* one.

### The Two-Address Model

```text
  LOCAL (this-core) endpoints — full TENSOR3D access patterns:
      source tile  = SRC TENSOR3D @bundle+0x10   (getIfmap, inst+0xA8)
      dest tile    = DST TENSOR3D @bundle+0x30   (getDst,   inst+0xC8)
    Each is a 16-byte TENSOR3D descriptor (partition base + per-dim stride/num),
    dtype-tagged by +0x0C / +0x0D. These name the SBUF footprint on the LOCAL core.

  REMOTE (peer-core) endpoint — a single scalar SB partition address:
      peer SB addr      = bundle+0x21  = *(*(srcAP+0x50)+8)  (2nd APPair element)
      cross-core enable = bundle+0x20  = 1   (always, once the LNC2 gate passes)
```

### Why One Scalar Peer Address, No Core Index

The LNC has **exactly two** physical cores (LNC2). From any core, "the other core" is unambiguous, so the hardware needs only (a) a one-bit "go cross-core" enable and (b) the peer's SBUF partition address. The compiler bakes the 2-core assumption in via the `+0x1A4 == 2` gate (next section). **There is no core-index field, no core-count field, and no multi-peer loop** anywhere in the encoder, the verifier, or the simulator. A 4-core LNC could not be expressed — there is no slot for a second peer. *Anchors: the encoder writes exactly one peer byte; no peer-index field exists.*

The peer's *symbolic* identity is resolved **upstream** by `lower_local_collectives`. `LowerLocalCollectives::getMemoryLocation @0x161ab30` mints a remote target named `<tensor>_remote_<core>` and marks it via `MemoryLocation::setRemoteLocalTarget`; `createRemoteAP @0x161cad0` builds the peer's `PhysicalAccessPattern`; the linker's `vnc_remote_addr_map`/`vnc_link` later resolve that target to the concrete peer-core SBUF partition that lands in `bundle+0x21`. *Anchors: `getMemoryLocation @0x161ab30`, `createRemoteAP @0x161cad0`, `setRemoteLocalTarget` / `getRemoteLocalTarget` symbols present.*

### Simulator Confirmation (functional semantics)

The reference model in `libBIRSimulator.so` (`InstVisitor::visitInstGPSIMDSB2SB @0x1f7590`) embodies the 2-core topology in its first statement and a `(coreId+1)&1` peer arithmetic:

```c
// birsim visitInstGPSIMDSB2SB @0x1f7590
assert NeuronCoresManager::getNumCoresPerLNC() == 2     // cmp $0x2,%rax; je @0x1f75ba
coreId  = this+0xE0
partner = (coreId + 1) & 1                              // the OTHER core in the pair
idx     = NCM::getCollectiveComputeInstIdx(mgr, coreId, I)     // @0x273e50
srcMO   = NCM::getCcOpInMemObjForCore(mgr, partner, idx, 0, 1) // @0x272aa0 — peer's CC output
dst     = getInOutPhysicalAP(I, 0, /*isOutput=*/1)
mem.write(dst) <- srcMO.cast(FP32)
```

The op pulls the **partner** core's collective-compute output `MemObj` into this core's State Buffer through the shared `NeuronCoresManager` `CcOp` map. The `(coreId+1)&1` arithmetic is the runtime embodiment of the compiled-in 2-core topology, valid **only** when `getNumCoresPerLNC() == 2`. *Anchors: `getNumCoresPerLNC()==2` gate at `@0x1f75ba`; `getCcOpInMemObjForCore @0x272aa0`, `getCollectiveComputeInstIdx @0x273e50` symbols present.*

---

## The LNC2 Hard-Coding

### Purpose

The 2-physical-core topology is not a parameter the encoder generalizes; it is **structural**, compiled in at three independent byte sites, all keyed on the per-arch `cores-per-LNC` field `arch+0x1A4`. This is the **same field** [1.07](lnc-memory-model.md) reads as `lnc_size` (`PassOptions+0x1A4`); the GPSIMD `arch+0x1A4` is the per-arch mirror of that cores-per-LNC value.

### The Three Sites

```text
  (1) ENCODER GATE   cmpl $0x2,0x1a4(%rax)   @0x1359a67 (GENERATE) / @0x1359da1 (CHECK)
                     mismatch ⇒ NeuronAssertion 936, args actual=<version> supported="2"
                     (GPSIMDSB2SB.cpp:745)
  (2) ENABLE BYTE    movb $0x1,0x20(%r13)     @0x1359a74  — the single cross-core marker,
                     stamped only after the gate passes.
  (3) libBIR GATE    isCompatible: FIRST predicate  cmp $0x2,%rdi   @0x2931f2
                     ctx (= cores-per-LNC, read from arch+0x1A4) must equal 2.
  (+) SIMULATOR      getNumCoresPerLNC() == 2  @0x1f75ba (functional gate, above).
```

```asm
; CoreV3GenImpl::visitInstGPSIMDSB2SB — the LNC2 gate, both paths
1359870:  8b b0 a4 01 00 00    mov    0x1a4(%rax),%esi   ; read arch ctx → verify arg
1359876:  e8 ..                call   InstGPSIMDSB2SB::verify   ; → isCompatible
1359a15:  c6 85 .. .. bf       movb   $0xbf,...          ; opcode 0xBF
1359a3d:  41 88 45 0c          mov    %al,0xc(%r13)      ; src dtype
1359a54:  41 88 45 0d          mov    %al,0xd(%r13)      ; dst dtype
1359a67:  83 b8 a4 01 00 00 02 cmpl   $0x2,0x1a4(%rax)   ; <<< LNC2 GATE (GENERATE)
1359a74:  41 c6 45 20 01       movb   $0x1,0x20(%r13)    ; cross-core enable = 1
1359a9f:  41 88 45 21          mov    %al,0x21(%r13)     ; peer SB partition addr
1359ada:  e8 ..                call   fwrite             ; emit 0x40 bytes
;  ... CHECK path repeats:
1359da1:  83 b8 a4 01 00 00 02 cmpl   $0x2,0x1a4(%rax)   ; <<< LNC2 GATE (CHECK)
```

```asm
; bir::InstGPSIMDSB2SB::isCompatible @0x2931d0 (libBIR) — first predicate
2931f2:  48 83 ff 02          cmp    $0x2,%rdi          ; ctx == 2 ? (cores-per-LNC)
2931fc:  4d 85 c0             test   %r8,%r8            ; both operands present ?
```

*Anchors: all four sites disassembled directly: `0x1359a67`, `0x1359da1`, `0x2931f2`, `0x1f75ba`.*

> **GOTCHA — `gpsimd_version` and `lnc_size` name the same field.** The value the encoder compares is `arch+0x1A4`, and it is described in different places as a "gpsimd version" and as `lnc_size` / `cores-per-LNC`. There is only one field. The gate is literally "this op is legal only when the LNC has exactly 2 cores." A 4-core LNC (a different `+0x1A4`) fails the encoder assert — there is no peer-index field to express more than one peer. This is the identical `arch+0x1A4 == 2` immediate cited by [1.07 The multi-core (LNC) memory model](lnc-memory-model.md); the two pages agree by construction. *Anchors: both call sites read `+0x1A4`; consistent with [1.07].*

### Does CoreV4 Generalize It? No.

`InstGPSIMDSB2SB` is a **gen3+** op (no CoreV2 encoder body exists), and there is **exactly one** `GenImpl::visitInstGPSIMDSB2SB` symbol in `libwalrus.so` — the CoreV3 one. **CoreV4 does not override the encoder**; it reuses the CoreV3 body verbatim, `+0x1A4 == 2` gate and all. The *only* per-gen difference is the cost model:

| Generation | Encoder | LNC2 gate | Cost model | Confidence |
|---|---|---|---|---|
| **CoreV2** (Sunda, single core) | none — op absent | n/a (single-core, no cross-core swap) | n/a | CERTAIN |
| **CoreV3** (Cayman / gen3, Trn2 LNC2) | `visitInstGPSIMDSB2SB @0x1359840` (sole body) | `cmpl $0x2,0x1a4` | `Gen3Hwm::getInstGPSIMDSB2SBLatency @0x185ca10` | CERTAIN |
| **CoreV4** | **inherits** the CoreV3 encoder | same `== 2` gate | `CoreV4Hwm::getInstGPSIMDSB2SBLatency @0x1861ea0` (distinct curve) | CERTAIN |

Both generations also carry a `dbg_is_valid_sb2sb_collective` validator (`neuronxcc::core_v3::` and `neuronxcc::core_v4::`), confirming the op survives into CoreV4 with the same wire layout. Gen4 did **not** widen the LNC beyond 2 for this op. *Anchors: single `CoreV3GenImpl` encoder symbol; both Hwm latency symbols; both `core_v3`/`core_v4` `dbg_is_valid_sb2sb_collective` symbols present.*

---

## Usage — GPSIMD vs DMA vs Collective (the compiler's decision)

### Purpose

`InstGPSIMDSB2SB` is never authored directly. It is **minted** by the `lower_local_collectives` pass when it lowers an on-chip `InstCollectiveCompute`, and only under specific conditions — small, shape-compatible swaps. Everything else falls to DMA or the cross-node collective library. A reimplementer must reproduce this decision exactly, or emit GPSIMD where DMA is required (and overflow the hard ceiling) or vice-versa.

### Provenance

`lnc_splitter` first clones the module into one `bir::Module` per physical core; `LowerLocalCollectives::run(vector<Module>&)` then groups the `InstCollectiveCompute` ops and dispatches on the collective kind. The kind dispatch:

```text
  lnc == 1   → early return (no cross-core exchange; arch+0x1A4 == 1)
  kind 0  SendRecv     → lowerSendRecv  @0x161cc70  → GPSIMDSB2SB *or* InstDMACopy
  kind 1  SendRecvCCE  → lowerSendRecvCCE @0x161f130 → InstDMACopy ONLY (CCE DMA)
  kind 2  AllReduce    → lowerAllReduce @0x1621a80  → DMA reduce-scatter + CoreBarrier
                                                       + all-gather  (NEVER GPSIMD)
  kind ≥3 (ReduceScatter/AllGather/AllToAll/Permute/…)
                       → FENCE-ONLY: CoreBarrier per remote core; op left INTACT =
                         the REMOTE / ICI cross-NODE path (runtime collective library).
```

So the on-chip (within-LNC) vs remote split is exactly which kinds get a local lowering — only `0`/`1`/`2`. **GPSIMD is reachable only on the `kind 0 SendRecv` arm.** *Anchors: `lowerSendRecv @0x161cc70`, `lowerSendRecvCCE @0x161f130`, `lowerAllReduce @0x1621a80` symbols present; dispatch structure per the kind handlers.*

### The Engine Pick (kind 0 SendRecv only)

```c
// LowerLocalCollectives::lowerSendRecv @0x161cc70  (the GPSIMD-vs-DMA picker)
function pickSendRecvEngine(srcAP, dstAP, I):
    compat = InstGPSIMDSB2SB::isCompatible(lnc /*=arch+0x1A4*/, module, srcAP, dstAP, I) // @0x2931d0
    if !compat:
        return InstDMACopy                          // shape/ctx/SBUF/size incompatible → DMA

    ebpp = getEffectiveBytesPerPartition(elemsPerPartition * DtypeSize, dtype)  // @0x291a50
    if ebpp > cl::opt("sendrecv-to-gpsimd-max-bpp"):
        return InstDMACopy                          // larger than the knob → DMA fallback
    return InstGPSIMDSB2SB                           // small + compatible → the GPSIMD fast path
```

GPSIMD is chosen for **small, shape-compatible** SendRecv swaps; everything larger or incompatible falls to DMA. The pass knob `sendrecv-to-gpsimd-max-bpp` ("Bytes/partition under which local swapping SendRecvs will be mapped to GPSIMDSB2SB", string verbatim in `libwalrus.so`) sits **under** the hard libBIR ceiling of 1024 B/partition. *Anchors: knob string verbatim; `getEffectiveBytesPerPartition @0x291a50`, `isCompatible @0x2931d0` symbols present.*

### The `isCompatible` Legality Gate

`bir::InstGPSIMDSB2SB::isCompatible @0x2931d0` is the GPSIMD legality predicate:

| Predicate | Anchor | Confidence |
|---|---|---|
| `ctx == 2` (cores-per-LNC == 2) | `cmp $0x2,%rdi` `@0x2931f2` | CERTAIN |
| both operands present (`r8 != 0`) | `test %r8,%r8` `@0x2931fc` | CERTAIN |
| partition base / model gate | `cmp ...,0x1d` (29) | HIGH |
| src/dst rank ∈ {2..5}, equal elems/partition, matching dtype, BOTH operands SBUF, physical AP | enumerated predicate list | HIGH |
| effective bytes/partition ≤ `MaxBytesPerPartition` = 1024 | `MaxBytesPerPartition @0x784a00` = `0x400` | CERTAIN |

The `≤1024 B/partition` ceiling lives in **libBIR**, not the encoder — the encoder's first statement is `verify(...)`, which tail-calls `isCompatible`, and the codegen body trusts that gate and never re-checks bytes. The budget is computed on **dtype-padded** bytes by `getEffectiveBytesPerPartition` *before* the comparison, so a 2-byte or 4-byte dtype consumes the budget faster than its element count alone implies. *Anchors: `MaxBytesPerPartition @0x784a00` byte-read `00 04 00 00` = 1024; `isCompatible` opcodes at `0x2931f2`/`0x2931fc`.*

### Sync / Completion

GPSIMDSB2SB swaps are fenced by `InstCoreBarrier` (CoreV3-only, `CoreV3GenImpl::visitInstCoreBarrier`) inserted by `lower_local_collectives`, and finalized by point-to-point completion tokens placed at the writers' common-postdominator block (the `_sb2sb` completion family). The `_remote_<core>` cross-core targets minted by `getMemoryLocation` are resolved by the linker's `vnc_remote_addr_map`/`vnc_link`. *Anchors: `InstCoreBarrier` encoder + `getRemoteLocalTarget` machinery present; the completion-token placement is the `lower_local_collectives` post-pass.*

### The Reimplementer's Decision Rule

```text
  LNC == 1                          → no cross-core op (everything stays local).
  kind 0 SendRecv, SBUF↔SBUF,
    rank 2..5, ebpp ≤ knob ≤ 1024   → InstGPSIMDSB2SB (Pool/GPSIMD engine, opcode 0xBF).
  kind 0 too big / incompatible     → InstDMACopy (core-to-core DMA).
  kind 1 SendRecvCCE                → InstDMACopy (CCE) always.
  kind 2 AllReduce                  → DMA reduce-scatter + CoreBarrier + all-gather.
  kind ≥3                           → CoreBarrier fence only; op survives → ICI / runtime lib.
```

GPSIMD is the narrow "small on-chip SBUF swap" fast path; DMA is the general on-chip mover; the ICI/collective library is the cross-node path. *Anchors: each arm anchored above.*

---

## Perf-Sim Cost (Gen3Hwm / CoreV4Hwm)

`Gen3Hwm::getInstGPSIMDSB2SBLatency @0x185ca10` models latency as piecewise-linear in transferred bytes with a breakpoint near 56 bytes — a cheap small-transfer regime and a steeper large-transfer slope. `CoreV4Hwm::getInstGPSIMDSB2SBLatency @0x1861ea0` carries its own distinct curve. `getLatencyExec` selects between them by `ArchLevel` (`gen3` vs `core_v4`). The slope/offset constants are hardware fits read from `.rodata`, not derivable from first principles; they are documented in the perf-sim cost-model page rather than here. *Anchors: both Hwm latency symbols present; the piecewise constants are HW fits.*

| Generation | Latency symbol | Address | Confidence |
|---|---|---|---|
| gen3 (CoreV3) | `Gen3Hwm::getInstGPSIMDSB2SBLatency` | `0x185ca10` | CERTAIN |
| gen4 (CoreV4) | `CoreV4Hwm::getInstGPSIMDSB2SBLatency` | `0x1861ea0` | CERTAIN |

---

## Confidence Ledger

| Claim | Tag | Basis (this page) |
|---|---|---|
| GPSIMD == external alias of `EngineType Pool(1)` | CERTAIN | string `External: GPSIMD Internal: Pool`; `EngineType2ExternalName @0x47fca0` |
| SB2SB op defaults to `Pool(1)` | CERTAIN | `getDefaultEngine @0x3e2cf0` = `mov $0x1; ret` |
| Exactly one GPSIMD machine op (`InstGPSIMDSB2SB`, opcode `0xBF`) | CERTAIN | sole `CoreV3GenImpl::visitInstGPSIMDSB2SB @0x1359840`; `movb $0xbf @0x1359a15` |
| Pure data move (zero MACs) | CERTAIN | `ArithOps @0x3e2ef0` → `ArithOpsZeroArithInst @0x49d490` |
| Cross-core: local src/dst `TENSOR3D` + one scalar peer addr + enable | CERTAIN | `+0x10`/`+0x30` `lea`, `+0x20` `movb $0x1` `@0x1359a74`, `+0x21` `mov %al` `@0x1359a9f` |
| LNC2 gate `arch+0x1A4 == 2` (encoder) | CERTAIN | `cmpl $0x2,0x1a4(%rax)` `@0x1359a67` / `@0x1359da1` |
| LNC2 gate (libBIR `isCompatible`) | CERTAIN | `cmp $0x2,%rdi` `@0x2931f2` |
| LNC2 gate (simulator) | CERTAIN | `getNumCoresPerLNC()==2` `@0x1f75ba`; `partner=(coreId+1)&1` |
| `+0x1A4` is the same field as `lnc_size` in [1.07] | CERTAIN | identical offset; both pages' call sites read `+0x1A4` |
| `≤1024 B/partition` ceiling in libBIR | CERTAIN | `MaxBytesPerPartition @0x784a00` = `00 04 00 00` = 1024 |
| CoreV4 inherits the CoreV3 encoder (no >2-core generalization) | CERTAIN | single encoder symbol; both Hwm latencies; `core_v3`/`core_v4` `dbg_is_valid_sb2sb_collective` |
| Minted by `lower_local_collectives` from kind-0 SendRecv only | CERTAIN | `lowerSendRecv @0x161cc70`; kind 1/2 → DMA; kind ≥3 → ICI |
| GPSIMD-vs-DMA pick under `sendrecv-to-gpsimd-max-bpp` | CERTAIN | knob string verbatim; `getEffectiveBytesPerPartition @0x291a50` |
| Two-/three-GPSIMD name collision (Pool-alias vs Xtensa vs NKI engine-3) | CERTAIN | `External: GPSIMD Internal: Pool`; `XtensaTools-14.09` in custom-op libs; `nki.isa.engine.gpsimd=3` |
| `isCompatible` rank/SBUF/dtype predicate list | HIGH | enumerated from the `isCompatible` body; opcodes byte-verified, full message decode beyond binary |

---

## Related Components

| Name | Relationship |
|---|---|
| [Pool Engine](pool-engine.md) | the engine GPSIMD aliases (`EngineType Pool(1)`) — the windowed-reduction leg that also hosts the SB2SB mover |
| Xtensa custom-op CPU cluster ([Part 11](../customop/)) | a **different** "GPSIMD" — a programmable processor array for user custom ops, `XtensaTools-14.09`; not the Pool engine |
| `nki.isa.engine.gpsimd` (engine 3) | the NKI front-door "GpSimd Engine" — the silicon programmable array; a third namespace, distinct from `Pool(1)` |
| `InstDMACopy` | the general on-chip core-to-core mover GPSIMD falls back to when a swap is too big or shape-incompatible |
| `InstCoreBarrier` (CoreV3) | the cross-core fence inserted around SB2SB swaps by `lower_local_collectives` |

## Cross-References

- [1.07 The multi-core (LNC) memory model](lnc-memory-model.md) — the shared-DRAM cross-core model, the "on-chip memory is private" rule, and the **same** `arch+0x1A4 == 2` gate; explains why SB2SB is a peer-to-peer copy, not a shared address space.
- [1.09 Pool Engine](pool-engine.md) — the engine this page's GPSIMD is the external alias of; the windowed pooling / reduce datapaths that share the `Pool(1)` sequencer with the SB2SB mover.
- [Part 11 — Custom Ops & GPSIMD](../customop/) — the Xtensa custom-op CPU cluster (the *other* "GPSIMD"), and the full reconciliation of the two-GPSIMD name collision.
- [Part 8 — The libwalrus Backend](../walrus/) — `lnc_splitter`, `lower_local_collectives` (the pass that mints `InstGPSIMDSB2SB`), `createRemoteAP`/`getMemoryLocation`, and the `vnc_remote_addr_map`/`vnc_link` cross-core address resolution that fills `bundle+0x21`.
