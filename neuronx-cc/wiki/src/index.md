# neuronx-cc Internals — AWS Neuron Compiler

> **Status**: scaffolding · **Source packages**: `neuronx_cc-2.24.5133.0` (cp310/cp311/cp312 wheels) + stubs · **Cross-stack orientation**: [`neuron-platform/wiki/`](../../neuron-platform/wiki/)

## What this wiki is

The AWS Neuron compiler. Takes XLA HLO (from JAX or PyTorch-XLA via libneuronpjrt) or pure-Python NKI kernels, and emits **NEFF** (Neuron Executable File Format) binaries that the runtime (`libnrt.so`) loads onto NeuronCore devices.

## Compile pipeline at a glance

```
User Python (PyTorch / JAX / NKI / pure-Python kernel)
            │
            ▼
    XLA HLO  (HloModule protobuf)
            │
            ▼
    MHLO / StableHLO / CHLO   (MLIR dialects)
            │
            │  hlo2penguin (227 MB binary)
            │  - CanonicalizeForTensorizer
            │  - TensorizerLegalizationPass
            │  - NeuronOpFusion / NeuronInstCombine
            │  - PenguinizeFunctions
            ▼
    Penguin Python IR  (.py emission)
            │
            │  walrus_driver + libwalrus.so
            │  60+ register_generator_* passes
            │  - Inlining (BIR / NKI kernels)
            │  - Memory analysis + coloring allocators
            │  - DMA pipeline + LNC barriercheck
            │  - Scheduling + lower_dma
            ▼
    BIR  (Backend IR, bir::Module*)
            │
            │  Backend (per-target codegen)
            │  TongaISel / SundaISel / Cayman / CoreV4Gen
            ▼
    Per-Engine ISA (PE / ACT / DVE / SP / POOL)
            │
            │  Kelper → NeffWrapper (hlo-neff-wrapper)
            ▼
    NEFF on disk
```

## Codename surface

| Compiler axis | Names | Hardware target |
|---|---|---|
| `xla::hilo::*` cost-model | Tonga / Sunda / Cayman / Mariana | Per silicon (4 chronological generations) |
| `libwalrus.so` codegen | CoreV2Gen / CoreV3Gen / CoreV4Gen | Per ISA generation |
| NKI Python `Target` | Cayman / Tonga / Sunda / CoreV4 | User-facing target argument |

See [arch/codename-decoder.md](arch/codename-decoder.md) for the full mapping (gated on cross-stack resolution).

## Where to start

1. **[Compiler Pipeline Overview](arch/overview.md)** — the IR descent diagram with binary anchors
2. **[BIR Instruction Hierarchy](bir/inst-hierarchy.md)** — the 110 opcodes that every walrus pass operates on
3. **[Walrus Pass Inventory](walrus/pass-inventory.md)** — 60+ passes grouped by phase
4. **[NKI nl.* Language Surface](nki/nl-language-surface.md)** — the 97 user-visible Python ops
5. **[NKI nisa.* ISA Surface](nki/nisa-isa-surface.md)** — the 41 hardware-near intrinsics
6. **[NEFF File Format](formats/neff.md)** — the compile-output binary artifact (1024B header + gzip-pax-tar)
7. **[BIR JSON Schema](formats/bir-json.md)** — the inter-process wire format with `bir_roundtrip`

## Companion wikis

- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — the runtime side that consumes NEFFs
- [`neuron-jax-stack/wiki/`](../../neuron-jax-stack/wiki/) — libneuronpjrt → neuronx-cc subprocess contract
- [`neuronx-misc/wiki/`](../../neuronx-misc/wiki/) — torch_neuronx / jax_neuronx framework bindings that drive compilation
