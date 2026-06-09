# neuronx-misc — Tools, Bindings, K8s, OCI

> **Status**: scaffolding · **Source packages**: `aws-neuronx-tools_2.29.18.0` + `aws-neuronx-k8-plugin_2.29.147.0` + `aws-neuronx-k8-scheduler_2.29.147.0` + `aws-neuronx-oci-hook_2.15.13.0` + `nki-0.3.0` + `torch_neuronx-2.9.0` + `jax_neuronx-0.7.0` + `tensorflow_neuronx-*` + `tensorboard_plugin_neuronx-2.0.918.0`

## What this wiki is

The **supporting binaries** for the AWS Neuron stack: user-facing CLIs, debug libraries, Kubernetes integration, container-runtime OCI hook, and the per-framework Python bindings that connect PyTorch / JAX / TensorFlow to libneuronpjrt. None of these are core compiler or runtime — they're tooling, integration, and convenience layers — but the user-facing entry points for almost everyone live here.

## Component inventory

```
DIAGNOSTIC TOOLS (Go binaries, link libnrt + libndl + libndbg)
├── neuron-ls       — device enumeration (analog of nvidia-smi)
├── neuron-monitor  — streaming JSON telemetry daemon
├── neuron-top      — Python-script terminal dashboard
├── neuron-bench    — benchmark harness (130 MB, embeds HTTP)
├── neuron-profile  — NEFF execution profiler
├── neuron-explorer — interactive profile browser
├── neuron-dbg      — debugger CLI (wraps libndbg.so)
├── neuron-dump     — SDK state snapshot (shell + Python)
└── nccom-test      — collectives correctness/perf test

DEBUG LIBRARY
└── libndbg.so      — 3 per-arch backends (cayman / mariana / sunda)
                     CSR resolution + engine state introspection

KUBERNETES (Go binaries)
├── k8s-neuron-device-plugin   — kubelet device-plugin v1beta1 gRPC
└── k8s-neuron-scheduler        — kube-scheduler extender HTTP

CONTAINER RUNTIME (C++ binary, nlohmann::json)
└── oci-neuron-hook  — OCI prestart hook; injects /dev/neuron* + cgroup perms

FRAMEWORK BINDINGS (Python wheels)
├── torch_neuronx-2.9.0  — libtorchneuron.so + xla_impl trace pipeline
├── jax_neuronx-0.7.0    — thin PJRT plugin registration
├── tensorflow_neuronx   — 3 TF-version wheels (2.8 / 2.9 / 2.10)
└── tensorboard_plugin_neuronx

NKI STANDALONE
├── nki-0.3.0         — pure NKI wheel (libNkiPythonCAPI.so + Cython front-ends)
└── neuron_dtypes     — bf16, fp4, fp8, fp32r custom NumPy dtypes
```

## Where to start

1. **[Package Inventory](inventory.md)** — full table with SHA256s, sizes, function counts
2. **[Shared Dependencies](shared-deps.md)** — map of which tools link which `lib*.so`
3. **[neuron-ls](tools/neuron-ls.md)** — most-used user CLI
4. **[neuron-monitor](tools/neuron-monitor.md)** — JSON telemetry schema
5. **[libndbg Arch Backends](libndbg/arch-backends.md)** — cayman/mariana/sunda backends (confirms codename triple)
6. **[K8s Device Plugin Overview](k8s/device-plugin-overview.md)** — kubelet gRPC integration
7. **[OCI Hook Overview](oci-hook/overview.md)** — container runtime integration
8. **[torch_neuronx Overview](bindings/torch-neuronx-overview.md)** — PyTorch entry path
9. **[NKI Wheel vs neuronx-cc Bundled](nki/wheel-vs-cc.md)** — relationship between the standalone NKI wheel and the NKI inside neuronx-cc

## Companion wikis

- [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) — compiler; the NKI Python frontend is mirrored here
- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — the NRT_2.0.0 ABI these tools consume
- [`neuron-platform/wiki/`](../../neuron-platform/wiki/) — cross-stack flow including these tools' role
