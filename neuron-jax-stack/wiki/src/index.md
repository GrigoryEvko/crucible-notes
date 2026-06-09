# Neuron JAX / PJRT / Collectives Stack

> **Status**: scaffolding · **Source packages**: `libneuronxla-2.2.16408.0+50c26cbd` (Python wheel, 490 MB libneuronpjrt.so) + `aws-neuronx-collectives_2.31.24.0` (libnccom.so + libnccom-net.so) · **Cross-stack orientation**: [`neuron-platform/wiki/`](../../neuron-platform/wiki/)

## What this wiki is

Combined reference for **two binary surfaces** that wave-2 N4 argued should be documented together: the **PJRT plugin** (libneuronpjrt.so) that connects JAX and PyTorch/XLA to the Neuron runtime, and the **collective-communications stack** (libnccom.so as NCCL fork + libnccom-net.so as aws-ofi-nccl rebuild). The two are tightly coupled — PJRT routes collective ops through libnrt into libnccom into libnccom-net into libfabric onto EFA NICs.

## Three-binary stack

```
        JAX (jax_plugins.neuron)      torch_xla
                  │                       │
                  └──────────┬────────────┘
                             │  PJRT C-API (v0.55..v0.75 multi-version)
                             ▼
              ┌────────────────────────────────┐
              │   libneuronpjrt.so  (490 MB)   │  ← PJRT plugin
              │   GetPjrtApi entry             │
              │   embeds full XLA + MLIR + LLVM│
              │   per-version trampolines      │
              │   (_0_70 / _0_75 variants)     │
              └─────────────┬──────────────────┘
                            │  NRT_2.0.0 (39 nrt_* imports)
                            ▼
              [ libnrt.so — see neuronx-runtime wiki ]
                            │
                  collective ops branch off to:
                            ▼
              ┌────────────────────────────────┐
              │   libnccom.so.2.31.24 (9.8 MB) │  ← collective engine
              │   NCCL fork (KaenaNCCL)        │
              │   data-plane API REMOVED       │
              │   only bootstrap + topology    │
              │     + ncclRt* device shim      │
              │   3 platform TUs (cayman/      │
              │     mariana/sunda)             │
              └─────────────┬──────────────────┘
                            │  ncclNet_v6_t vtable
                            │  via dlopen(libnccom-net.so)
                            ▼
              ┌────────────────────────────────┐
              │   libnccom-net.so (323 KB)     │  ← OFI transport
              │   aws-ofi-nccl rebuild         │
              │   SENDRECV + RDMA protocols    │
              │   3 plugin ABI vtables (v4/5/6)│
              └─────────────┬──────────────────┘
                            │  libfabric (FABRIC_1.0/1.1/1.8)
                            ▼
                       EFA NIC (PCI 0xefa0/1/2)
                            │
                            ▼
                       Peer host EFA NIC
```

## Three-layer ABI compatibility negotiation

| Layer | Mechanism | Versions seen in libneuronxla |
|---|---|---|
| PJRT C-API (client ↔ plugin) | per-version trampolines, env-var `NEURON_INTERNAL_PJRT_C_API_VERSION` | v0.55..v0.75 (10 jaxlib, 6 torch_xla variants) |
| NRT (plugin ↔ runtime) | ELF symbol versioning | `@@NRT_2.0.0` |
| NCCL net plugin (libnccom ↔ libnccom-net) | dlopen handshake | ncclNet_v4 / v5 / v6 |

## Fork-delta summary

| Binary | Upstream | Delta size |
|---|---|---|
| libneuronpjrt.so | OpenXLA PJRT C-API + StableHLO + XLA service | Embeds full XLA + LLVM (statically linked) |
| libnccom.so | NVIDIA NCCL (fork point ~2.13-2.16) | ~30-40%: data-plane removed, bootstrap +Bruck/RecursiveDoubling/Hierarchical, ncclRt* device shim added, kangaring topology, 3 platform TUs |
| libnccom-net.so | aws-ofi-nccl | <5%: identical except platform-registration tweaks |

## Where to start

1. **[Stack Overview Diagram](boundaries/stack-diagram.md)** — single canonical figure showing every binary edge
2. **[PJRT C-API Surface](libneuronxla/pjrt-api-surface.md)** — every PJRT_* function/struct in libneuronpjrt
3. **[libnccom vs Upstream NCCL](collectives/libnccom-vs-nccl.md)** — structured fork-delta audit
4. **[libnccom_static.a Archive](collectives/libnccom-static-archive.md)** — primary RE artifact (54 MB, 8910 symbols) for the otherwise-stripped libnccom.so
5. **[Bootstrap Algorithms](collectives/bootstrap.md)** — 5 algorithms (Ring / Bruck / Recursive-Doubling / Hierarchical / SendRecv)
6. **[Topology Builder](collectives/topology.md)** — Kangaring rings, JBOG/MLA hierarchy, findPathRec recursive DFS
7. **[OFI Protocols](collectives/ofi-protocols.md)** — SENDRECV vs RDMA paths inside libnccom-net

## Companion wikis

- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — libnrt.so that this plugin invokes
- [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) — subprocess invoked for HLO → NEFF compilation
- [`neuron-platform/wiki/`](../../neuron-platform/wiki/) — cross-stack flow diagrams including this stack
