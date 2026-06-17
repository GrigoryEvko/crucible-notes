# Extraction Status

> **Binaries pinned on this page** — `libnrt.so` → `libnrt.so.2.31.24.0` (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, BuildID `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, ELF64 DYN x86-64, **not stripped**, full DWARF v4); `libnccom.so.2.31.24` (`aws-neuronx-collectives 2.31.24.0-1a31ba186`, BuildID `9c00176c081788c9435d27d11bb40e92495463f0`, not stripped, `.debug_info`); `neuron.ko` (`aws-neuronx-dkms 2.27.4.0`, GPL-2.0 C source authoritative, binary stripped); `libncfw.so` (SONAME `libncfw.so.2.31.1.0.cf13a49f`, BuildID `a98f8e1ca2294582835310c3a1092e0a5e500db5`, **no DWARF**); `libnrtucode_extisa.so` (BuildID `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, **stripped, no DWARF**); plus the static archive `libnds.a` (6 TUs, 810,674 B, the same code linked into `libnrt.so`).
>
> **Part 0 — Reference Apparatus** / REFERENCE · **Evidence grade:** this page grades *other* pages; its own claims are the per-page status tags carried in each cited page's own header blockquote, plus the DWARF coverage facts from the source-map and heavy-frame census re-run against these binaries · [back to index](../index.md)

## Abstract

This is the book's honest self-assessment. Every other page asserts a confidence in its own header — "Reimplementation-grade", "byte-anchored", "boundary edge" — but a reader landing on one page cannot see how the whole stack lines up: which subsystems are decoded to the byte and which are an opaque blob with a name stuck on it. This page is that ledger. It assigns one of three evidence grades to every shipped page and to every subsystem cluster, names the principal gap that keeps each cell below the top grade, and routes the genuinely-opaque items to the [Phase-3 Deep-Dive Backlog](../appendix/deep-dive-backlog.md). It is a catalogue: its quality is the completeness and the honesty of the map, not new derivation.

The three grades are deliberately coarse. **Byte-level decoded (reimplementation-grade)** means a competent engineer could rebuild the artifact — wire format, ISA encoding, struct layout, algorithm — from the page alone, because the bytes were read and the decision logic recovered. **Surface-mapped** means the role, call surface, and inputs/outputs are pinned but at least one interior is not decoded to the byte: a config TU with no recovered static caller, a serializer with no DWARF, an upload path summarized rather than re-derived. **Not-traced** means the page is honest about an opaque region — a sequencer-internal microcode op, a per-arch leaf body left at a name, an interior math kernel that was sized but not walked. The grade is the *floor* of the page: a page that is byte-level on its main artifact but leaves one helper opaque is graded by what a reimplementer still cannot rebuild. Two of the five binaries — `libncfw.so` and `libnrtucode_extisa.so` — carry **no DWARF and (for extisa) no symbols at all**, which sets a hard ceiling on every page that depends on them; that ceiling is named explicitly in [§5](#5-the-no-dwarf-ceiling).

> **NOTE —** "surface-mapped" is not a failure grade. For vendored OSS (protobuf, Abseil, simdjson, libarchive, zlib, the Rust crates) surface-mapping is the *correct* terminal state — a reimplementer links the pinned version rather than reversing the arena allocator. Those rows are graded by whether the *boundary* (which symbols, which version, what they back) is decoded, not the library interior. The version pins live in the [vendored SBOM](../forensics/vendored-sbom.md); this page does not re-grade them.

## Grade legend

| Grade | Meaning | What it implies for a reimplementer |
|---|---|---|
| **byte-level** | Wire format / ISA / struct / algorithm decoded to the byte; decision logic recovered | Rebuild from the page alone; cross-check against the cited address/offset |
| **surface-mapped** | Role, call surface, inputs/outputs pinned; ≥1 interior not byte-decoded | Reproduce the boundary; the interior needs the binary (or, for OSS, link upstream) |
| **not-traced** | An opaque region named honestly — sized but not walked, or no DWARF/symbols | Treat as a black box at the named boundary; routed to the deep-dive backlog |

## 1. Per-Part coverage table

One row per shipped page, grouped by Part; the SUMMARY path is exact. **Grade** is the page floor per the legend. **Status** is a one-phrase state; **Principal gap** names the single thing that keeps the cell below byte-level (— if none). **Conf** grades *this assessment* of the page, not the page's own internal claims.

### Part 0 — Reference Apparatus

| Page (`src/…`) | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `front/how-to-read.md` | byte-level | conventions fixed | — | HIGH |
| `front/five-binaries.md` | surface-mapped | binary identity map | per-binary depth lives downstream | HIGH |
| `front/inference-walkthrough.md` | byte-level | worked example | — | HIGH |
| `methodology.md` | byte-level | method fixed | — | HIGH |
| `front/codename-cheatsheet.md` | byte-level | codename↔arch pinned | — | HIGH |
| `front/source-tree.md` | byte-level | 331 DWARF CU tree | 18 TUs name-gapped (fn-covered) | HIGH |
| `glossary.md` | byte-level | terms fixed | — | HIGH |
| `front/bibliography.md` | surface-mapped | external refs | — | HIGH |
| `reference/binary-layout.md` | byte-level | section/segment map | — | HIGH |
| `reference/extraction-status.md` | — | this page | — | — |

### Part I — Silicon & Architecture Model

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `arch/overview.md` | surface-mapped | runtime-lens model | — | HIGH |
| `arch/generations-enum.md` | byte-level | V2/V3/V4 enum decoded | — | HIGH |
| `arch/pci-device-ids.md` | byte-level | device-ID→arch table | — | HIGH |
| `arch/hw-geometry.md` | byte-level | per-gen geometry consts | — | HIGH |
| `arch/coretype-numbering.md` | byte-level | the +1 off-by-one resolved | — | HIGH |
| `arch/memory-hierarchy.md` | byte-level | BAR/SBUF layout | — | HIGH |

### Part II — Binary Anatomy & Forensics

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `forensics/overview.md` | byte-level | heavy-frame census | — | HIGH |
| `forensics/elf-anatomy.md` | byte-level | ELF tables read | — | HIGH |
| `forensics/vendored-sbom.md` | byte-level | versions pinned; library interiors intentionally not reversed | OSS interiors out of scope by design | HIGH |
| `forensics/static-init.md` | byte-level | 77 ctors mapped | — | HIGH |
| `forensics/globals-atlas.md` | byte-level | singleton atlas | — | HIGH |
| `forensics/dispatch-tables.md` | byte-level | dispatch taxonomy | — | HIGH |
| `forensics/rtti-class-hierarchy.md` | byte-level | RTTI graph | — | HIGH |
| `forensics/string-domain.md` | byte-level | string surface | — | HIGH |
| `forensics/crt-plt.md` | byte-level | PLT/loader surface | — | HIGH |

### Part III — Kernel Driver

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `kernel/overview.md` … `kernel/ioctl-pod.md` (probe, cdev, IOCTL dispatch+catalog, mem/dma/nq/pod handlers) | byte-level | GPL C source read directly | line-anchored, not RE | HIGH |
| `kernel/mempool-handles.md`, `dma-op-layer.md`, `dma-rings.md` | byte-level | DMA/handle model | — | HIGH |
| `kernel/udma-main.md`, `udma-m2m.md`, `udma-iofic.md` | byte-level | UDMA fork (GPL C) | — | HIGH |
| `kernel/dhal-core.md`, `dhal-v2.md`, `dhal-v3.md`, `dhal-v4.md` | byte-level | DHAL v2/v3/v4 bodies decoded | — | HIGH |
| `kernel/notification-queues.md`, `topsp.md`, `reset.md`, `crwl.md`, `pod-election.md` | byte-level | NQ/reset/lock/election | — | HIGH |
| `kernel/fw-io.md`, `dmabuf-p2p.md`, `datastore.md`, `metrics.md`, `sysfs.md`, `power.md`, `misc.md` | byte-level | mailbox/p2p/telemetry | — | HIGH |

### Part IV — Userspace Runtime Core

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `runtime/overview.md`, `api-lifecycle.md`, `api-device-config.md`, `api-tensors.md`, `api-async-collectives.md` | byte-level | public C API surface | thunks forward to impl TUs | HIGH |
| `runtime/config-structs.md`, `env-vars.md` | byte-level | nrt_config + NEURON_RT_* table | — | HIGH |
| `runtime/error-codes.md` | byte-level | priority classifier + error catalogue | — | HIGH |
| `runtime/interned-strings.md` | byte-level | string DB | — | HIGH |
| `runtime/tdrv-lifecycle.md`, `tdrv-dmem.md`, `tdrv-dma-rings.md`, `tdrv-scratchpad.md`, `tdrv-tensor.md`, `tdrv-arch-ops.md` | byte-level | TDRV core | — | HIGH |
| `runtime/arch-geometry.md` | surface-mapped | per-arch geometry | `sbuf_base_for_core` leaf bodies MED | HIGH |
| `runtime/arch-csr-offsets.md`, `arch-notification.md`, `arch-sdma.md`, `arch-stpb.md` | byte-level | per-arch CSR/INTC/SDMA/STPB | — | HIGH |
| `runtime/hal-adapter.md`, `hal-tpb-shims.md`, `hal-registers.md` | byte-level | KaenaHal adapter/shims | — | HIGH |
| `runtime/hal-udma-iofic.md` | surface-mapped | HAL UDMA build + IOFIC | 17/24 `al_hal_udma_config` fns no static caller | HIGH |
| `runtime/logging.md`, `device-book.md`, `ndl.md` | byte-level | nlog / db / NDL shim | — | HIGH |

### Part V — Model Format & Loading

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `neff/overview.md`, `container.md`, `metadata-schema.md`, `section-taxonomy.md`, `dtype-system.md` | byte-level | NEFF=gzip-tar + JSON schema | — | HIGH |
| `neff/kelf2kbin.md`, `kbin-structs.md`, `memory-planning.md`, `compute-resource-build.md`, `load-pipeline.md` | byte-level | KBIN lowering + KBL build | huge per-fn parse leaves (sized) | HIGH |

### Part VI — TPB Instruction Set

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `isa/overview.md` | byte-level | engine instruction model | — | HIGH |
| `isa/pseudo-instruction-lowering.md` | byte-level | SP/TopSP builders | — | HIGH |
| `isa/instruction-record.md` | byte-level | 64-byte record format | — | HIGH |
| `isa/fp8-dtype-encoding.md` | byte-level | FP8/dtype encoding | — | HIGH |
| `isa/validator-architecture.md` | byte-level | validator entry tree | — | HIGH |
| `isa/validators-per-arch.md` | byte-level | per-arch validator deltas | some `encd` interior math not walked | HIGH |

### Part VII — Execution Engine

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `exec/overview.md`, `kmgr-facade.md`, `xu-workers.md`, `submit-path.md`, `completion-engine.md`, `xu-queue-abi.md` | byte-level | submit/harvest path + ABI | — | HIGH |

### Part VIII — DMA & Descriptor Engine

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `dma/overview.md` | byte-level | DMA engine map | — | HIGH |
| `dma/descriptor-format.md` | byte-level | 16-byte descriptor decoded | — | HIGH |
| `dma/meta-ctrl-overlays.md` | byte-level | SDMA/CCE/TDG overlays | — | HIGH |
| `dma/ring-cycle.md`, `virtual-rings.md`, `iofic.md` | byte-level | ring/vring/IOFIC | — | HIGH |

### Part IX — On-Device Collectives

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `collectives/overview.md`, `engine-core.md`, `comm-context.md`, `nccl-boundary.md` | byte-level | enc context + libnccom boundary | — | HIGH |
| `collectives/algorithm-taxonomy.md`, `mesh-composer.md`, `ring-scheduling.md`, `hierarchical-rdh.md`, `enc-primitives.md` | byte-level | mesh/ring/hier/RDH composers | composer methods are line sinks (sized) | HIGH |
| `collectives/switch-broadcast-barrier.md` | surface-mapped | switch-platform events | 13 per-event composer TUs name-gapped (fn-covered) | MEDIUM |
| `collectives/topology-partition.md` | byte-level | union-find partitioner | — | HIGH |
| `collectives/proxy-driver.md`, `send-recv.md` | byte-level | bananaphone IPC + async P2P | — | HIGH |
| `collectives/cc-op-isa.md` | byte-level | cc_op_entry on-device ISA | — | HIGH |
| `collectives/channel-descriptor.md` | byte-level | 148-byte channel descriptor | — | HIGH |
| `collectives/encd-overview.md`, `encd-dma-devmem.md`, `encd-sema-topsp.md`, `encd-arch-ops.md` | byte-level | encd emitter (per-arch) | — | HIGH |

### Part X — Collectives Firmware (libncfw)

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `firmware/overview.md`, `carrier-library.md` | surface-mapped | carrier map | no DWARF (see [§5](#5-the-no-dwarf-ceiling)) | HIGH |
| `firmware/embedded-payloads.md` | byte-level | 8 Xtensa blobs carved + fingerprinted | — | HIGH |
| `firmware/serializer-families.md` | surface-mapped | per-arch CC-context dumpers | serializer interiors — no DWARF | MEDIUM |
| `firmware/ncfw-sequencer.md` | surface-mapped | Xtensa-LX disassembly | sequencer-internal ops not fully traced | MEDIUM |
| `firmware/upload-path.md` | surface-mapped | DKMS→DRAM upload | DMA-descriptor algebra summarized, not re-derived | HIGH |

### Part XI — GPSIMD / Q7 Microcode & ISA

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `gpsimd/overview.md`, `xtensa-vision-q7.md`, `xtensa-toolchain.md` | byte-level | Q7 identification + TIE config | — | HIGH |
| `gpsimd/extisa-provider.md`, `dispatch-tables.md`, `ucode-facade.md` | byte-level | nrtucode_* API + dispatch + dlopen facade | extisa binary stripped (boundary only) | HIGH |
| `gpsimd/microcode-loader.md` | byte-level | RELA+FLIX UCPL loader | — | HIGH |
| `gpsimd/q7-blobs.md` | byte-level | 13 Q7 blobs carved | sequencer-internal TIE ops in microcode not-traced | HIGH |
| `gpsimd/ivp-isa-catalog.md` | byte-level | 1065 ivp + 469 scalar = 1534 mnemonics, 12569 placements | — | HIGH |

### Part XII — Multi-Node Collectives (libnccom)

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `nccom/overview.md`, `comm-init.md`, `topology.md`, `algorithm-ring.md`, `algorithm-tree.md`, `send-recv-prims.md`, `proxy-engine.md`, `tuning.md`, `transport-intra.md`, `transport-efa.md`, `net-plugin.md`, `abi.md` | surface-mapped | NCCL-fork map (DWARF-bearing) | inherited NCCL bodies mapped by delta, not byte-walked | HIGH |

### Part XIII — Profiling, Trace & Telemetry

| Page cluster | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `trace/overview.md`, `inspect-profile-api.md`, `system-monitor.md` | byte-level | three trace producers | — | HIGH |
| `trace/ntff-format.md`, `ntff-wire-tables.md` | byte-level | ntff.proto wire tables decoded | — | HIGH |
| `trace/event-taxonomy.md` | byte-level | 46 SysTraceEventType variants | — | HIGH |
| `trace/rust-capture.md`, `rust-serde.md`, `rust-ffi.md` | byte-level | neuron_rustime capture + serde + FFI | Rust generics mapped via cgu, not source | HIGH |
| `trace/telemetry-errors.md` | byte-level | metrics + error reporting | — | HIGH |

### Part XIV — Neuron DataStore

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `datastore/overview.md`, `kernel-side.md`, `userspace-libnds.md` | byte-level | counter plane + libnds.a | — | HIGH |
| `datastore/wire-format.md` | byte-level | NDS wire format decoded | — | HIGH |

### Part XV — Security & Attack Surface

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `security/overview.md`, `ioctl-attack-surface.md`, `fw-io-trust.md`, `hardening.md` | byte-level | privilege-gate model + 14 findings | — | HIGH |

### Part XVI — Appendices

| Page | Grade | Status | Principal remaining gap | Conf |
|---|---|---|---|---|
| `appendix/subsystem-matrix.md` | surface-mapped | subsystem↔binary↔TU matrix | — | HIGH |
| `appendix/symbol-versions.md` | byte-level | symbol-version manifest | — | HIGH |
| `appendix/globals-index.md` | byte-level | global address index | — | HIGH |
| `appendix/known-bugs.md` | surface-mapped | anomalies catalog | — | HIGH |
| `appendix/deep-dive-backlog.md` | surface-mapped | backlog (receives [§4](#4-what-remains-opaque)) | — | HIGH |

## 2. What is byte-level decoded

The artifacts a reimplementer can rebuild from the page alone, each with its owning page. These are the load-tested cores of the book — wire formats read field-by-field, ISA catalogues sized against their own operand tables, struct layouts confirmed against multiple sources.

- **The 16-byte DMA descriptor** — every bit-field decoded → [`dma/descriptor-format.md`](../dma/descriptor-format.md).
- **The `cc_op_entry` on-device collective ISA** and **the 148-byte ring channel descriptor** → [`collectives/cc-op-isa.md`](../collectives/cc-op-isa.md), [`collectives/channel-descriptor.md`](../collectives/channel-descriptor.md).
- **The `ntff.proto` trace wire tables** — `TcParseTable`-decoded → [`trace/ntff-wire-tables.md`](../trace/ntff-wire-tables.md).
- **The Q7 IVP ISA catalogue** — 1065 ivp + 469 scalar = 1534 mnemonics, 12569 placements, sized against `libisa-core.so`'s operand tables → [`gpsimd/ivp-isa-catalog.md`](../gpsimd/ivp-isa-catalog.md).
- **The DHAL v2 / v3 / v4 vtable bodies** — decoded from the GPL kernel source → [`kernel/dhal-v2.md`](../kernel/dhal-v2.md), [`kernel/dhal-v3.md`](../kernel/dhal-v3.md), [`kernel/dhal-v4.md`](../kernel/dhal-v4.md).
- **The NDS wire format** → [`datastore/wire-format.md`](../datastore/wire-format.md).
- **The union-find topology partitioner** → [`collectives/topology-partition.md`](../collectives/topology-partition.md).
- **The priority classifier + error catalogue** (`NRT_STATUS`) → [`runtime/error-codes.md`](../runtime/error-codes.md).

## 3. What is surface-mapped (boundary pinned, interior not byte-decoded)

These pages pin the role and call surface but stop short of a byte-level interior — correctly, because the interior either lacks a recovered caller, lacks DWARF, or is upstream OSS a reimplementer should link rather than reverse.

- **Some HAL config TUs** — 17 of 24 `al_hal_udma_config` functions have no recovered static caller in libnrt; their role is known, their invocation context is not → [`runtime/hal-udma-iofic.md`](../runtime/hal-udma-iofic.md).
- **The libncfw serializer interiors** — per-arch CC-context dumpers with no DWARF → [`firmware/serializer-families.md`](../firmware/serializer-families.md).
- **Some firmware upload paths** — the DMA-descriptor algebra of the DKMS→DRAM upload is summarized, not re-derived → [`firmware/upload-path.md`](../firmware/upload-path.md).
- **The libnccom inherited NCCL bodies** — mapped by *delta* from upstream NCCL `2.31.24+nrt2.0`, not byte-walked → [`nccom/overview.md`](../nccom/overview.md).

## 4. What remains opaque (not-traced)

The honest black boxes. Each is named at its boundary and routed to the [Phase-3 Deep-Dive Backlog](../appendix/deep-dive-backlog.md); none is claimed as covered.

| Opaque region | Where it surfaces | Why it is opaque | Backlog route |
|---|---|---|---|
| Sequencer-internal TIE ops in the Q7 microcode | [`gpsimd/q7-blobs.md`](../gpsimd/q7-blobs.md) | custom Tensilica TIE ops; no public iclass decode | deep-dive-backlog (Q7 microcode) |
| The NCFW Xtensa sequencer's internal handler ops | [`firmware/ncfw-sequencer.md`](../firmware/ncfw-sequencer.md) | no DWARF; disassembly mapped, internals not fully walked | deep-dive-backlog (NCFW sequencer) |
| Per-arch SBUF leaf bodies (`sbuf_base_for_core` MED) | [`runtime/arch-geometry.md`](../runtime/arch-geometry.md) | window size byte-pinned; per-core base derivation MED | deep-dive-backlog (arch leaves) |
| Some `encd` interior math | [`isa/validators-per-arch.md`](../isa/validators-per-arch.md), [`collectives/encd-overview.md`](../collectives/encd-overview.md) | huge per-fn bodies sized but not line-walked | deep-dive-backlog (encd math) |
| 13 switch-platform per-event composer TUs | [`collectives/switch-broadcast-barrier.md`](../collectives/switch-broadcast-barrier.md) | template/STL-inlined; folded into `enc.cc` by address, never name-cited | deep-dive-backlog (switch-platform events) |

> **GOTCHA —** "not-traced" never means "absent from the book." Each opaque region has a page that names its boundary, its inputs, and its callers; what is missing is the byte-level interior. A reimplementer treats the boundary as a contract and the interior as a build-against-the-binary task — not as a hole the book pretends is filled.

## 5. The no-DWARF ceiling

Two of the five binaries set a hard ceiling that no amount of analysis lifts to byte-level on *internal names*:

- **`libncfw.so`** (BuildID `a98f8e1ca…`) carries **no DWARF** and self-versions in its SONAME (`libncfw.so.2.31.1.0.cf13a49f`) rather than in `.git_hash`/`.nrt_brazil_version` sections. Its public boundary (the `libncfw_get_image_func` get-image entry, the embedded blob set, the v2 reset prologue) is byte-anchored; its internal serializer and handler *names* are not recoverable, so those pages are graded surface-to-MED by construction.
- **`libnrtucode_extisa.so`** (BuildID `7bb03bc4…`) is **stripped** — no DWARF *and* no symbol table. It is reached only as a `dlopen` target whose 30-entry `dlsym` binding table names its API surface; everything below that surface is decoded by raw disassembly against the carved blobs, not by symbol.

> **NOTE —** the byte-identity QUIRK that links these two carriers (the SUNDA v2 sequencer boot scaffold ships byte-identically in both, per [`firmware/upload-path.md`](../firmware/upload-path.md)) is itself a byte-level fact recovered *despite* the no-DWARF ceiling — proof that the ceiling caps internal *naming*, not byte-level structural claims. The grade floor of a no-DWARF page is therefore surface on names but can be byte-level on bytes; the table above grades the names a reimplementer would need, which is the binding constraint.

## Cross-References

- [Phase-3 Deep-Dive Backlog](../appendix/deep-dive-backlog.md) — receives every not-traced item in [§4](#4-what-remains-opaque); the work queue this page feeds
- [Subsystem ↔ Binary ↔ Source-TU Matrix](../appendix/subsystem-matrix.md) — the orthogonal view: which TU in which binary implements each subsystem (this page grades *coverage*; that page maps *ownership*)
- [Overview and Heavy-Frame Census](../forensics/overview.md) — the DWARF-CU partition and byte-budget split that underpins every grade here
- [Binary Layout](binary-layout.md) — the section/segment map of the binaries this page pins in its header
