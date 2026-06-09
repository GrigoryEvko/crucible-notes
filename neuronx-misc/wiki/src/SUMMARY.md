# Summary

[neuronx-misc — Tools, Bindings, K8s, OCI](index.md)

---

# Architecture and Inventory

- [Package Inventory and Artifact Map](inventory.md)
- [Build Provenance and Version Matrix](build-provenance.md)
- [Shared Dependencies (libnrt, libndl, libnccom, libndbg)](shared-deps.md)
- [Go Toolchain Fingerprint](go-toolchain.md)

# Observability and Diagnostic Tools

- [neuron-ls (Device Enumeration)](tools/neuron-ls.md)
- [neuron-monitor (Streaming JSON Telemetry)](tools/neuron-monitor.md)
- [neuron-monitor Schema Reference](tools/neuron-monitor-schema.md)
- [neuron-monitor Sidecar Scripts](tools/neuron-monitor-sidecars.md)
- [neuron-top (Terminal Dashboard)](tools/neuron-top.md)
- [neuron-bench (Benchmark Harness)](tools/neuron-bench.md)
- [neuron-profile (NEFF Execution Profiler)](tools/neuron-profile.md)
- [neuron-explorer (Profile Browser)](tools/neuron-explorer.md)
- [neuron-dbg (Debugger CLI)](tools/neuron-dbg.md)
- [neuron-dump (SDK State Snapshot)](tools/neuron-dump.md)
- [nccom-test (Collectives Correctness)](tools/nccom-test.md)
- [PodResources gRPC Client Integration](tools/podresources-grpc.md)

# libndbg Debug Library

- [Overview and ABI](libndbg/overview.md)
- [Per-Arch Backends (cayman / mariana / sunda)](libndbg/arch-backends.md)
- [CSR Block Discovery and Symbolic Resolution](libndbg/csr-blocks.md)
- [Engine State Introspection (PC, Runstate, Start Addr)](libndbg/engine-state.md)
- [Debug Info Loader](libndbg/debug-info.md)

# Kubernetes Integration

- [Device Plugin Overview](k8s/device-plugin-overview.md)
- [Device Plugin gRPC Surface](k8s/device-plugin-grpc.md)
- [Scheduler Extender API](k8s/scheduler.md)
- [Topology-Aware Placement](k8s/topology.md)
- [Container Images and RBAC](k8s/deployment.md)

# OCI Runtime Hook

- [Overview and Prestart Flow](oci-hook/overview.md)
- [Device Discovery via libndl](oci-hook/device-discovery.md)
- [Bind-Mount and Runtime Spec Mutation](oci-hook/mount-spec.md)
- [Runtime Integration (docker, containerd, podman)](oci-hook/runtime-integration.md)

# Framework Bindings

- [torch_neuronx Overview](bindings/torch-neuronx-overview.md)
- [torch_neuronx.trace and xla_impl Pipeline](bindings/torch-neuronx-trace.md)
- [torch_neuronx parallel_compile, FSDP-MICS, distributed](bindings/torch-neuronx-distributed.md)
- [torch_neuronx PyHLO and XLA HLO](bindings/torch-neuronx-pyhlo.md)
- [jax_neuronx (PJRT Wrapper)](bindings/jax-neuronx.md)
- [tensorflow_neuronx (2.8/2.9/2.10 Variants)](bindings/tensorflow-neuronx.md)
- [tensorboard_plugin_neuronx](bindings/tensorboard-plugin.md)

# NKI Standalone Wheel

- [Wheel vs neuronx-cc-Bundled Relationship](nki/wheel-vs-cc.md)
- [Python Frontend](nki/python-frontend.md)
- [MLIR Dialect Surface](nki/mlir-dialect.md)
- [Backend Selection](nki/backends.md)
- [neuron_dtypes](nki/dtypes.md)

# Cross-Cutting

- [Shared Profile / Monitor YAML Schemas](cross/profile-schema.md)
- [NRT Linkage Surface Used by Tools](cross/nrt-linkage.md)
- [Glossary](cross/glossary.md)
- [Extraction Gaps](cross/gaps.md)
