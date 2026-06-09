# Summary

[neuronx-runtime Internals](index.md)

---

# Reference Apparatus

- [Methodology](methodology.md)
- [Glossary](glossary.md)
- [Binary Layout](reference/binary-layout.md)
- [Extraction Status](reference/extraction-status.md)

# Kernel Driver (DKMS, Unstripped Source)

- [Module Layout and Build](kernel-driver/module-layout.md)
- [PCI Probe and Device IDs (5 PCI IDs)](kernel-driver/pci-probe.md)
- [DHAL Vtable (17 substructs, v2/v3/v4/vc)](kernel-driver/dhal-vtable.md)
- [Char Device and mmap](kernel-driver/cdev-mmap.md)
- [IOCTL Dispatch (70+ IOCTLs)](kernel-driver/ioctl-dispatch.md)
- [Memory Chunks and Handle Table](kernel-driver/mempool-handles.md)
- [DMA Engines and udma Fork](kernel-driver/dma-udma.md)
- [DMA Rings and H2T Queues](kernel-driver/dma-rings.md)
- [Cooperative RW Lock (CRWL)](kernel-driver/crwl.md)
- [Neuron DataStore](kernel-driver/datastore.md)
- [Notification Queues](kernel-driver/notification-queues.md)
- [Reset State Machine](kernel-driver/reset.md)
- [Sysfs Metrics Tree](kernel-driver/sysfs.md)
- [Pod Election (v3 Ultraserver)](kernel-driver/pod-election.md)
- [DMA-buf Export and P2P](kernel-driver/dmabuf-p2p.md)

# Runtime Library (libnrt.so)

- [Public API Surface (142 NRT_2.0.0 + 8 NRT_3.0.0)](runtime/libnrt/api-surface.md)
- [nrt_init and nrt_close Lifecycle](runtime/libnrt/lifecycle.md)
- [NEFF Loading](runtime/libnrt/neff-load.md)
- [Tensor and TensorSet Model](runtime/libnrt/tensors.md)
- [Execute and Async Exec](runtime/libnrt/execute.md)
- [Collective Comms Glue (enc_*, nec_*)](runtime/libnrt/collectives.md)
- [Inspect / Trace](runtime/libnrt/inspect.md)
- [libnds and libnrtucode_extisa](runtime/libnrt/companions.md)

# On-Device Firmware (libncfw)

- [Carrier Library (libncfw.so)](firmware/libncfw/carrier.md)
- [Embedded Payloads (8 blobs: 4 iram + 4 dram)](firmware/libncfw/payloads.md)
- [ISA Identification (Tensilica Xtensa LX)](firmware/libncfw/isa-identification.md)
- [Upload Path (DKMS → device DRAM)](firmware/libncfw/upload-path.md)
- [Collectives Scheduler Model](firmware/libncfw/collectives-scheduler.md)

# Cross-Cutting Topics

- [IOCTL Catalog](topics/ioctl-catalog.md)
- [mmap Resource Discovery](topics/mmap-resources.md)
- [FW I/O Protocol (Q7 Management Coprocessor)](topics/fw-io-protocol.md)
- [Memory Hierarchy (BAR0/BAR2/BAR4 + HBM + SRAM)](topics/memory-hierarchy.md)
- [Telemetry and CloudWatch Posting](topics/telemetry.md)
- [Error Reporting and ECC](topics/error-reporting.md)
- [Q7 vs Xtensa Dual-Firmware Model](topics/q7-vs-xtensa-firmware.md)
- [RE Methodology Notes](topics/re-methodology.md)
