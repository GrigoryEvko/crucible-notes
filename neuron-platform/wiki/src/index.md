# AWS Neuron Platform — Cross-Stack Reference

> **Status**: scaffolding · **Scope**: orientation across all AWS Neuron binaries · **Companions**: per-component wikis under `neuronx-cc/`, `neuronx-runtime/`, `neuron-jax-stack/`, `neuronx-gpsimd/`, `neuronx-distributed/`, `neuronx-misc/`

## What this wiki is

The AWS Neuron stack is composed of **~30 binaries across 7 source packages**. A single user-facing call like `model.forward(input)` traverses Python → torch_neuronx → libtorchneuron.so → libnrt.so → DKMS kernel module → NeuronCore firmware. A multi-host collective adds libnccom.so → libnccom-net.so → libfabric → EFA NIC to that path. The per-component wikis document each binary's internals; **this wiki is the orientation layer**: cross-binary contracts, end-to-end flows, ABI versioning, file formats that cross component boundaries, and the codename matrices that the per-component wikis hang off.

## Stack at a glance

```
              ┌──────────────────────────────────────────────────────┐
COMPILE-TIME  │   neuronx-cc (hlo2penguin + walrus + libBIR.so)      │  ← Compiler
              │   neuronx-cc-stubs                                   │
              └──────────────────────────────────────────────────────┘
              ┌──────────────────────────────────────────────────────┐
RUNTIME       │   libneuronxla (libneuronpjrt.so) — PJRT plugin      │  ← JAX entry
              │   torch_neuronx (libtorchneuron.so)  — PyTorch entry │
              │   tensorflow_neuronx                  — TF entry      │
              ├──────────────────────────────────────────────────────┤
              │   neuronx-runtime (libnrt.so + libncfw.so + DKMS)    │  ← Runtime
              ├──────────────────────────────────────────────────────┤
              │   neuronx-collectives (libnccom.so + libnccom-net)   │  ← Collectives
              ├──────────────────────────────────────────────────────┤
              │   neuronx-gpsimd  (Xtensa Q7 custom-op SDK)          │  ← Custom ops
              │   neuronx-distributed  (NxD + NxD-Inference)         │  ← Parallelism
              │   neuronx-misc  (tools + libndbg + k8s + OCI)        │  ← Tooling
              └──────────────────────────────────────────────────────┘
```

## Three firmware-running CPUs

A single NeuronCore device runs **three distinct CPUs** with three distinct firmware-load paths:

| CPU | Codename | Firmware source | ISA | Programmed by |
|---|---|---|---|---|
| NeuronCore TPB | (per silicon: cayman/mariana/sunda) | libncfw.so payloads (8 blobs: 4 iram + 4 dram), loaded by `libnrt.so:encd_ncfw_init` | **Tensilica Xtensa LX** with custom TIE extensions | walrus backend → BIR → KELF → NEFF |
| Q7 management coprocessor | (Annapurna AL) | Q7 ucode, separate load path | ARM-derived (Annapurna Q7) | Firmware ships with kernel driver; speaks FW_IO protocol |
| GPSIMD custom-op engine | Xtensa Q7 | User-built per custom op (`xt-clang++` → `lsp_fll_load_cpuN.so`) | **Tensilica Xtensa LX** (separate config from NeuronCore) | gpsimd custom-op SDK |

This was a key wave-2 finding that **wave-1 conflated the three CPUs**. See [Three Firmware-Running CPUs](architecture/three-cpus.md).

## Codename naming layers

Three orthogonal naming axes that the wiki must keep distinct:

| Axis | Names | Used by |
|---|---|---|
| **Silicon codename** | Tonga / Sunda / Cayman / Mariana | `xla::hilo::*` cost-model classes, `libndbg` arch backends, `libncfw` firmware C files |
| **ISA generation** | CoreV1 / CoreV2 / CoreV3 / CoreV4 / CoreV5, or `gen2`/`gen3`/`gen4`/`gen5` alias, or DKMS `NEURON_ARCH_V{2,3,4}` | `libwalrus` backend codegen classes, DKMS source subdirs (`v2/`, `v3/`, `v4/`) |
| **Product alias** | inf1 / trn1 / inf2 / trn2 / trn3 / trn3pre | User-facing NKI target argument, AWS instance type names |

These map 1-to-1 (not layered), per wave-2 N2.1 reconciliation. The chronological order — established by Python source comments and binary string evidence — is:

```
Tonga ──── Sunda ──── Cayman ──── Mariana
(Inf1)    (Trn1/Inf2) (Trn2)     (Trn3)
CoreV1    CoreV2      CoreV3     CoreV4
                                  CoreV5 (trn3pre variant)
```

**Open**: an N2.5 vs N2.1 contradiction on which silicon codename maps to which firmware-payload generation. See task #1044.

## Where to start

1. **[End-to-End Cross-Stack Flow](architecture/cross-stack-flow.md)** — the 4 canonical ASCII diagrams (compile / execute / collective / error)
2. **[Codename Decoder](architecture/codename-decoder.md)** — the canonical mapping table (binary-anchored)
3. **[ABI Versioning Matrix](architecture/abi-versioning.md)** — every versioned ABI boundary across the stack
4. **[NEFF File Format](formats/neff.md)** — the compiler→runtime binary artifact
5. **[BIR JSON Schema](formats/bir-json.md)** — the compiler-internal IR wire format
6. **[IOCTL → libnrt → User-API Traceability](topics/ioctl-userapi-traceability.md)** — 50-row matrix from Python down to kernel syscall

## Per-component wiki index

| Component | Wiki | Scope |
|---|---|---|
| Compiler | [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) | hlo2penguin, walrus, BIR, NKI Python frontend |
| Runtime + Driver + FW | [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) | libnrt.so, libncfw.so, aws-neuronx-dkms |
| JAX/PJRT + Collectives | [`neuron-jax-stack/wiki/`](../../neuron-jax-stack/wiki/) | libneuronpjrt.so + libnccom.so + libnccom-net.so |
| Custom-Op SDK | [`neuronx-gpsimd/wiki/`](../../neuronx-gpsimd/wiki/) | Xtensa Q7 ATen integration |
| Distributed Training/Inference | [`neuronx-distributed/wiki/`](../../neuronx-distributed/wiki/) | NxD parallel layers, NxD-I 15 model families |
| Tools + Bindings + K8s | [`neuronx-misc/wiki/`](../../neuronx-misc/wiki/) | neuron-* CLIs, framework bindings, k8s, OCI |
