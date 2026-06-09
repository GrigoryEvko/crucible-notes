# neuronx-runtime Internals — Driver, Runtime, Firmware

> **Status**: scaffolding · **Source packages**: `aws-neuronx-dkms_2.27.4.0_all` (unstripped GPL-2.0 source!) + `aws-neuronx-runtime-lib_2.31.24.0` · **Cross-stack orientation**: [`neuron-platform/wiki/`](../../neuron-platform/wiki/)

## What this wiki is

The **runtime side** of the AWS Neuron stack: the userspace library (libnrt.so), the on-device firmware carrier (libncfw.so), and the Linux kernel driver (DKMS module). This is the layer that consumes NEFFs produced by [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) and dispatches work onto physical NeuronCore hardware.

## Four-layer stack

```
                ┌────────────────────────────────────────┐
   USERSPACE    │   libtorchneuron.so / libneuronpjrt.so │  ← consumers
                └────────────────┬───────────────────────┘
                                 │  NRT_2.0.0 + NRT_3.0.0 ABI
                                 ▼
                ┌────────────────────────────────────────┐
                │   libnrt.so.2.31.24.0 (122 MB)         │  ← runtime library
                │   142 nrt_* + 8 nrta_* exports         │
                │   embeds libndl (IOCTL portal)         │
                └────────────────┬───────────────────────┘
                                 │  ioctl(/dev/neuron*, ...)
                                 ▼
   KERNEL       ┌────────────────────────────────────────┐
                │   aws-neuronx-dkms (GPL-2.0, ~20.6k LOC)│  ← kernel driver
                │   70+ IOCTLs under magic 'N'           │
                │   v2/v3/v4/vc per-gen DHAL vtables     │
                └────────────────┬───────────────────────┘
                                 │  PCIe BAR0 MMIO, DMA, MSI-X
                                 ▼
   ON-DEVICE    ┌────────────────────────────────────────┐
                │   NeuronCore TPB (Xtensa LX)            │  ← firmware
                │   firmware loaded from libncfw.so       │
                │   Q7 management coprocessor (separate)  │
                │   GPSIMD subcores                       │
                └────────────────────────────────────────┘
```

## Three firmware-running CPUs

Per wave-2 N2.5 reconciliation, a single NeuronCore device runs **three distinct on-device CPUs**, each with its own firmware-load path:

- **NeuronCore TPB sequencer** — Tensilica Xtensa LX, firmware shipped as `v{2,3,4,4_plus}_ncfw_iram_bin` payloads inside libncfw.so, uploaded by `libnrt.so:encd_ncfw_init` via H2T DMA into device IRAM. This is what `nrt_execute` ultimately dispatches work onto.
- **Q7 management coprocessor** — separate ARM-derived (Annapurna AL) CPU running Q7 ucode (loaded via a different path that wave-2 did not fully trace; "Failed to get Q7 ucode iram" strings confirm separate path). This is what speaks the **FW_IO protocol** (BAR0 MISC RAM register-poll loop). Wave-1 incorrectly conflated this with the Xtensa NCFW.
- **GPSIMD subcores** — also Tensilica Xtensa LX (different TIE config), programmed per-custom-op via the [neuronx-gpsimd](../../neuronx-gpsimd/wiki/) toolchain (xt-clang++ → 8 per-core ELFs).

## Easy wins from unstripped sources

- **DKMS C source is unstripped GPL-2.0** at `/extracted/aws-neuronx-dkms_2.27.4.0_all/usr/src/aws-neuronx-2.27.4.0/`. Most kernel-driver pages are mechanical transcription, not RE.
- **libnrt.so preserves debug_info** (verified by N3 wave-1). Symbol names and types survive — almost all libnrt pages are symbol-lookup, not pattern-matching.

## Where to start

1. **[IOCTL Catalog](topics/ioctl-catalog.md)** — 70+ IOCTLs directly transcribed from `neuron_ioctl.h:656-874`
2. **[FW I/O Protocol](topics/fw-io-protocol.md)** — the BAR0 MISC RAM register protocol (Q7 path, not Xtensa)
3. **[PCI Probe and Device IDs](kernel-driver/pci-probe.md)** — 5-entry PCI ID table + BAR mapping
4. **[DHAL Vtable](kernel-driver/dhal-vtable.md)** — 17-substruct hardware abstraction, per-generation v2/v3/v4/vc
5. **[libnrt API Surface](runtime/libnrt/api-surface.md)** — 142 NRT_2.0.0 + 8 NRT_3.0.0 (async) symbols
6. **[libncfw Payloads](firmware/libncfw/payloads.md)** — 8 embedded firmware blobs (4 iram + 4 dram)
7. **[Xtensa LX ISA Identification](firmware/libncfw/isa-identification.md)** — vector-table fingerprint, L32R idiom, Annapurna lineage

## Companion wikis

- [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) — produces NEFFs that `nrt_load` consumes
- [`neuron-jax-stack/wiki/`](../../neuron-jax-stack/wiki/) — PJRT plugin that drives nrt_* calls
- [`neuronx-misc/wiki/`](../../neuronx-misc/wiki/) — diagnostic tools (neuron-monitor, neuron-ls, neuron-profile, neuron-dbg)
