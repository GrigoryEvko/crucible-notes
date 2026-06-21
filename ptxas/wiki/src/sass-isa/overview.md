# SASS ISA & Hardware Model

> *Recovered from the decoded per-architecture SASS instruction tables shipped in
> nvdisasm V13.1.115 (CUDA 13.1), cross-validated against the ptxas v13.0.88
> scheduling tables. Facts and models only — the verbatim NVIDIA tables are not
> redistributed.*

ptxas's final output is SASS — the native machine code the GPU executes. nvdisasm
ships an encrypted, per-architecture **instruction table** describing that ISA in
full: every instruction class, its assembly format, its binary encoding, its
legality conditions, and its scheduling properties. Decrypting those tables (the
stream-cipher + LZ4 scheme documented in `decoded/MANIFEST.md`) and parsing all
thirteen targets recovers a complete, machine-level model of the hardware — what the
GPU can decode, how it encodes instructions, how it schedules them, and how each
generation's capabilities differ.

This section builds that model along three axes:

- **[Instruction Encoding](instruction-encoding.md)** — the 128-bit instruction word:
  opcode, guard predicate, operand/register-file encoding, constant-bank addressing,
  operand modifiers, the scheduling-control word, and the 116-entry relocator ABI.
- **[Execution Model](execution-model.md)** — the dynamic view: coupled vs decoupled
  pipes, the functional-unit virtual queues, the scoreboard/barrier dependency
  mechanism, and the per-warp issue rule.
- **[Legality & Capabilities](legality-and-capabilities.md)** — the constraint model:
  register alignment, out-of-range and address checks, the shader-type capability
  tiers, and the dual predicate register files.
- **[Architecture Evolution](architecture-evolution.md)** — the generation-by-generation
  ISA diff (SM75 → SM121): what each GPU added, removed, or gated, and the two decisive
  capability splits (Hopper wgmma vs Blackwell tcgen05; datacenter vs consumer Blackwell).

## Why this matters

The tables are the ground truth for *theoretically modelling a GPU*: a cycle-accurate
or capability model can be built directly from the encoding (what bits mean), the
execution descriptors (how the hardware issues and retires), and the legality rules
(what is allowed). Three findings frame the rest of the section:

1. **Nine decoders cover thirteen targets.** `SM86≡87≡88`, `SM90≡90a`, and `SM120≡121`
   are byte-identical decoders. The `a` suffix and the consumer/pro/Jetson splits are
   ptxas codegen-legality gates, **not** hardware-decoder differences.
2. **The control word is the scheduling contract.** Bits 102–124 of every instruction
   carry the stall count, yield, operand reuse, two scoreboard ids, and a 6-bit wait
   mask — the entire per-instruction schedule ptxas computes lives here.
3. **The hardware evolves by datapath, not by tweak.** The matrix unit was redesigned
   twice in three generations, and Blackwell splits into a tensor-memory datacenter
   part and a uniform-ALU consumer part with disjoint instruction sets.

## Provenance

The decoded tables are NVIDIA's compiled data, decrypted for interoperability and
research under DMCA 17 U.S.C. § 1201(f); only the recovered **facts** — bit positions,
counts, type taxonomies, the evolution diff, and our parser/extractor tooling — are
published. The verbatim tables are not redistributed (see `decoded/MANIFEST.md`).
