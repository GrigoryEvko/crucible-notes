# Summary

[neuronx-distributed — NxD + NxD-Inference Reference](index.md)

---

# Architecture

- [Overview and Two-Wheel Layering](topics/overview.md)
- [Parallel State (7-Axis Mesh: TP/EP/PP/CP/DP + Token-Shuffle/KV-Shared/Spec-Draft)](topics/parallel-state.md)
- [SPMD Execution Model and SPMDBucketModelScript](topics/spmd.md)

# Training-Side (NxD)

- [Parallel Layers (ColumnParallelLinear, RowParallelLinear, VocabParallelEmbedding)](training/parallel-layers.md)
- [Pipeline Schedules (1F1B / Interleaved / Train / Inference)](training/pipeline-schedules.md)
- [Pipeline Model Partitioning and Shared Weights](training/pipeline-model.md)
- [NeuronZero1Optimizer and ZeRO-DCP Utils](training/zero1-optimizer.md)
- [Checkpointing (Sharded Save/Load, ZeRO Conversion)](training/checkpointing.md)
- [Trainer Integration and PyTorch Lightning Hook](training/trainer.md)
- [MoE (Experts, Routing, Token Shuffling)](training/moe.md)

# Compile / Trace Pipeline

- [ModelBuilder and ModelBuilderV2 Trace Flow](compile/model-builder.md)
- [HLO Utilities and Compile Cache](compile/trace-pipeline.md)

# Inference-Side (NxD-I)

- [Attention Module Family (Base, GQA, Sink, Sliding-Window)](inference/attention.md)
- [KV-Cache Managers (Standard, Block/Paged, DP, Multimodal)](inference/kv-cache.md)
- [Block KV Cache Detailed (Paged Attention)](inference/block-kv-cache.md)
- [Speculative Decoding (Eagle, Dynamic Token Tree)](inference/speculative.md)
- [LoRA Serving](inference/lora.md)
- [Model Catalogue (15 Supported Architectures)](inference/model-catalogue.md)

# Reference

- [Glossary](glossary.md)
