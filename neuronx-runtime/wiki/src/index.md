# Neuron Runtime Internals

> *Reverse-engineering reference for the AWS Neuron runtime stack. Source packages: `aws-neuronx-dkms_2.27.4.0` (unstripped GPL-2.0 kernel source) · `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (libnrt.so + libnds.a with DWARF; libncfw.so + libnrtucode_extisa.so unstripped but symtab-only, no DWARF) · `aws-neuronx-collectives_2.31.24.0-1a31ba186` (libnccom.so with DWARF; libnccom-net.so symtab-only). Every address on a page pins to these builds.*

## What this wiki is

This book documents the **runtime side** of the AWS Neuron stack at reimplementation grade: the layer that takes a compiled model (a **NEFF**, produced by [`neuronx-cc`](../../neuronx-cc/wiki/)), loads it onto Trainium/Inferentia silicon, runs inference, and orchestrates multi-device collectives. It spans five binaries — a userspace runtime, a GPL kernel driver, two on-device firmware/microcode carriers, and a forked NCCL — reconstructed function-by-function from static analysis.

The bar is the house bar: a competent systems engineer should be able to **rebuild** each component from its page — the algorithm, the data layout, the wire format, the decision logic — and tell which parts are certain and which are inferred. See [How to Read This Book](front/how-to-read.md), which also covers the evidence and confidence conventions.

## The five binaries

| Binary | Package | Role |
|---|---|---|
| **libnrt.so** | runtime-lib | The userspace runtime: API, model load, execution, collectives glue, profiler |
| **kernel DKMS** | dkms (GPL source) | The `/dev/neuron*` char-device driver: ioctl, DMA, DHAL, NQ, reset, pod |
| **libncfw.so** | runtime-lib | Carrier for the on-device NCFW collective-sequencer firmware (Xtensa) |
| **libnrtucode_extisa.so** | runtime-lib | Provider for the GPSIMD/Q7 microcode (Tensilica Vision-Q7) |
| **libnccom.so / -net.so** | collectives | The NCCL fork (`2.31.24+nrt2.0`) for multi-node collectives over EFA |

`libnds.a` (the Neuron DataStore) is statically linked into libnrt.so and also shipped standalone.

## The layered stack

```text
  CONSUMERS    libtorchneuron.so / libneuronpjrt.so / JAX-PJRT
                       │  NRT_2.0.0 + NRT_3.0.0 ABI (149 exports)
                       ▼
  RUNTIME      libnrt.so  ── load NEFF, plan memory, submit, harvest, trace
                       │            │ dlopen
                       │            ├─► libnccom.so ── multi-node collectives (EFA)
                       │            └─► libnrtucode_extisa.so ── GPSIMD microcode
                       │  ioctl(/dev/neuronN, ...)
                       ▼
  KERNEL       aws-neuronx-dkms  ── ioctl, DMA rings, DHAL v2/v3/v4, NQ, reset, pod
                       │  PCIe BAR MMIO · DMA · MSI-X · FW-IO mailbox
                       ▼
  ON-DEVICE    NeuronCore engines (PE/ACT/POOL/DVE/SP) + NCFW sequencer (Xtensa)
               + GPSIMD/Q7 vector cores (Vision-Q7)
```

## How the data flows (the spine of this book)

A model's journey is the reading order: **silicon model** ([Part I](arch/overview.md)) → **kernel driver** ([Part III](kernel/overview.md)) → **runtime core** ([Part IV](runtime/overview.md)) → **NEFF parse, memory plan, resource build** ([Part V](neff/overview.md)) → **TPB instruction lowering and ISA validation** ([Part VI](isa/overview.md)) → **execute: submit and harvest** ([Part VII](exec/overview.md)) → **DMA descriptors** ([Part VIII](dma/overview.md)) → **on-device collectives** ([Part IX](collectives/overview.md)) → **collective firmware** ([Part X](firmware/overview.md)) and **GPSIMD microcode** ([Part XI](gpsimd/overview.md)) → **multi-node collectives** ([Part XII](nccom/overview.md)) → **trace and telemetry** ([Part XIII](trace/overview.md)). The cross-cutting apparatus — binary forensics ([Part II](forensics/overview.md)), the datastore ([Part XIV](datastore/overview.md)), security ([Part XV](security/overview.md)), and reference tables ([Part XVI](appendix/subsystem-matrix.md)) — wraps the spine. New readers should start with [An Inference, End to End](front/inference-walkthrough.md).

## Evidence basis

The kernel driver ships as unstripped GPL source, and every binary in scope retains at least a full symbol table — several also carry DWARF — so the great majority of claims here are symbol- (and where DWARF survives, type-) grounded rather than pattern-matched:

- The **DKMS C source is unstripped GPL-2.0** — kernel pages cite `file:line` directly.
- **libnrt.so, libnds.a, and libnccom.so preserve DWARF** — function names, struct field names, and enum values survive; pages cite addresses, struct offsets, and DWARF-recovered types. The two firmware carriers **libncfw.so and libnrtucode_extisa.so** (and **libnccom-net.so**) are **unstripped but symtab-only — no DWARF**; pages for those binaries cite named symbols and `.rodata` offsets, not DWARF types.
- The on-device firmware (NCFW Xtensa sequencer; GPSIMD Vision-Q7 microcode) is **fully disassemblable**: the Tensilica `.tie` config ships in the GPSIMD toolchain, so the custom vector ISA decodes exactly. See [Part XI](gpsimd/xtensa-vision-q7.md).

> **CORRECTION —** an earlier scaffold of this index claimed all four runtime-lib binaries plus both collectives binaries "preserve DWARF". That is overturned by `readelf -SW <bin> | grep -c '\.debug'`: only **libnrt.so** (9), **libnds.a** (835), and **libnccom.so** (8) carry `.debug_*` sections. **libncfw.so** (0), **libnrtucode_extisa.so** (0), and **libnccom-net.so** (0) are unstripped but **symtab-only — no DWARF**; this matches what all four firmware pages already state for libncfw.so. Pages for the symtab-only binaries are grounded on named symbols and `.rodata` offsets, not DWARF-recovered types.

> **CORRECTION —** an earlier scaffold of this wiki described the GPSIMD/Q7 cores as "ARM-derived" with a TIE config that could not be decoded. Both are wrong: the GPSIMD compute cores are **Tensilica Vision-Q7** (Xtensa LX with the IVP vector extension), and the `.tie` is shipped, so the 1065-op vector ISA is fully recovered ([GPSIMD ISA Catalog](gpsimd/ivp-isa-catalog.md)). The separate FW-IO management path is documented at [FW-IO MiscRAM Mailbox](kernel/fw-io.md).

## Companion wikis

- [`neuronx-cc`](../../neuronx-cc/wiki/) — the compiler that produces the NEFFs `nrt_load` consumes
- [`neuronx-gpsimd`](../../neuronx-gpsimd/wiki/) — the GPSIMD/Q7 custom-op toolchain (the producer side of [Part XI](gpsimd/overview.md))
- [`neuron-jax-stack`](../../neuron-jax-stack/wiki/) — the PJRT plugin that drives `nrt_*`
- [`neuronx-distributed`](../../neuronx-distributed/wiki/) — the distributed-training layer above the collectives
- [`neuronx-misc`](../../neuronx-misc/wiki/) — diagnostic tools (neuron-monitor, neuron-ls, neuron-profile)
