# Summary

[Neuron JAX / PJRT / Collectives Stack](index.md)

---

# Reference Apparatus

- [Methodology](methodology.md)
- [Glossary](glossary.md)
- [Extraction Status](reference/extraction-status.md)

# Part I — libneuronxla (PJRT Plugin)

- [Overview](libneuronxla/overview.md)
- [PJRT C-API Surface](libneuronxla/pjrt-api-surface.md)
- [Plugin Lifecycle and Version Negotiation](libneuronxla/lifecycle-and-versioning.md)
- [Client, Device, Memory Model](libneuronxla/client-device-memory.md)
- [Buffer and Host-Device Transfer](libneuronxla/buffer-transfers.md)
- [Compile Path (HLO → neuronx-cc → NEFF)](libneuronxla/compile-pipeline.md)
- [Execute Path (LoadedExecutable → libnrt)](libneuronxla/execute-pipeline.md)
- [Async Runtime and Event Model](libneuronxla/async-events.md)
- [Embedded XLA / LLVM / MLIR Footprint](libneuronxla/embedded-xla.md)
- [Neuron-Specific Extensions](libneuronxla/neuron-extensions.md)
- [Python Wrapper Layer](libneuronxla/python-wrapper.md)
- [JAX Plugin Registration](libneuronxla/jax-registration.md)

# Part II — neuronx-collectives

- [Overview](collectives/overview.md)
- [libnccom Architecture (NCCL Fork)](collectives/libnccom-architecture.md)
- [libnccom vs Upstream NCCL (Delta Audit)](collectives/libnccom-vs-nccl.md)
- [libnccom_static.a Archive (54 MB, 8910 symbols, 31 TUs)](collectives/libnccom-static-archive.md)
- [ncclRt* Device Runtime Shim (35 symbols)](collectives/ncclrt-shim.md)
- [Bootstrap Algorithms (5: Ring/Bruck/RecDouble/Hierarchical/SendRecv)](collectives/bootstrap.md)
- [Topology Builder (Kangaring, JBOG, MLA, findPathRec)](collectives/topology.md)
- [Collective Operations](collectives/collective-ops.md)
- [libnccom-net Plugin (OFI v4/v5/v6 Vtables)](collectives/libnccom-net-plugin.md)
- [libnccom-net vs aws-ofi-nccl (Fork Delta)](collectives/libnccom-net-vs-aws-ofi-nccl.md)
- [OFI Protocols (SENDRECV vs RDMA)](collectives/ofi-protocols.md)
- [AWS Platform Integration (EFA, NIC GUIDs, hwloc)](collectives/aws-platform.md)
- [Tuning Surface (36 ofi_nccl_* knobs)](collectives/tuning-knobs.md)

# Part III — Cross-Binary Boundaries

- [Stack Overview Diagram](boundaries/stack-diagram.md)
- [PJRT Plugin → libnrt ABI (NRT_2.0.0)](boundaries/pjrt-to-nrt.md)
- [PJRT Plugin → neuronx-cc Subprocess Contract](boundaries/pjrt-to-neuronx-cc.md)
- [libnrt → libnccom Collective Dispatch (nec_* + enc_*)](boundaries/nrt-to-nccom.md)
- [libnccom → libnccom-net (NCCL Net Plugin ABI Handshake)](boundaries/nccom-to-net.md)
- [Rendezvous (NEURON_RT_ROOT_COMM_ID)](boundaries/rendezvous.md)
