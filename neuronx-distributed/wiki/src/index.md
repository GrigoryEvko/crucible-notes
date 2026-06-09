# neuronx-distributed — NxD + NxD-Inference Reference

> **Status**: scaffolding · **Source packages**: `neuronx_distributed-0.18.27753+1cafd54f` (training core) + `neuronx_distributed_inference-0.9.17334+ced6ae4e` (inference, 15 model families)

## What this wiki is

The AWS Neuron **distributed-training and distributed-inference** libraries, all pure-Python over PyTorch-XLA. NxD provides the parallelism primitives; NxD-Inference adds attention, KV-cache, LoRA, and a catalog of 15 supported model architectures. This wiki documents the algorithmic implementations (pipeline schedules, ZeRO-1 optimizer, MoE token-shuffling, Eagle speculative decoding, paged KV-cache) rather than just listing class names.

## 7-axis parallel state mesh

Wave-2 N2.7 confirmed NxD uses a **richer parallel state than Megatron-LM's TP/PP/DP triple**:

| Axis | Group constructor | Use |
|---|---|---|
| **TP** (tensor parallel) | `get_tensor_model_parallel_replica_groups` | Megatron-style intra-layer sharding |
| **EP-model** (expert model parallel) | `get_expert_model_parallel_replica_groups` | MoE expert sharding |
| **PP** (pipeline parallel) | `get_pipeline_model_parallel_replica_groups` | Multi-stage forward+backward pipeline |
| **CP** (context parallel) | `get_context_parallel_replica_groups` | Long-context sequence sharding |
| **DP** (data parallel) | `get_data_parallel_replica_groups` | Standard replica grouping |
| **EP-data** (expert data parallel) | `get_expert_data_parallel_replica_groups` | DP subgroup excluding EP partners |
| **Token-Shuffle** | `get_token_shuffle_replica_groups` | DP subdivision for MoE pre-routing |
| **KV-Shared** | `get_kv_shared_replica_groups` | KV-cache sharing group |
| **Speculative-Draft** | `get_speculative_draft_replica_groups` | Eagle draft model subdivision of TP |

## Four pipeline schedules

| Schedule | Total steps | Memory peak | Use |
|---|---|---|---|
| `InferenceSchedule` | `MB` (microbatches) | 1 mb activations | Inference; no backward |
| `Train1F1BSchedule` | `2·MB + 1` | `(stages-stage_id-1)` activations | Megatron 1F1B steady state |
| `TrainInterleavedSchedule` | varies | highest | Interleaved chunks (requires `MB % stages == 0`) |
| `TrainSchedule` | `2·(MB+stages-1)` | mid | GPipe with bubbles (deprecated, kept as reference) |

## 15 NxD-Inference model families

dbrx, deepseek, diffusers (flux/clip/t5/vae), gemma3, gpt_oss, llama, llama4, mistral, mixtral, mllama, pixtral, qwen2, qwen2_vl, qwen3, qwen3_moe, qwen3_vl, whisper.

Notable variants:
- **DeepSeek**: Multi-head Latent Attention (q_lora_rank, kv_lora_rank, low-rank KV compression)
- **gpt_oss**: native MXFP4 compute with `mx_layout_transform.py` shuffling
- **qwen3_moe**: FP8 dequant on load via `_scale_inv` per-tile scales
- **llama4**: mandatory chunked attention (per-layer `attention_chunk_size`)
- **whisper**: encoder-decoder with separate cross-attention KV (encoded once, reused across decode steps)
- **mllama** / **qwen2_vl** / **qwen3_vl** / **pixtral**: multimodal with vision encoder + decoder text model

## Where to start

1. **[Parallel State Construction](topics/parallel-state.md)** — how the 7-axis mesh is built from a flat rank list
2. **[Pipeline Schedules](training/pipeline-schedules.md)** — full algorithmic comparison of the 4 schedulers
3. **[NeuronZero1Optimizer](training/zero1-optimizer.md)** — TP-aware ZeRO-1 with EP variant
4. **[MoE Token Shuffling](training/moe.md)** — BASE-Layers all-to-all protocol
5. **[Block KV Cache](inference/block-kv-cache.md)** — paged-attention page-table layout
6. **[Speculative Decoding](inference/speculative.md)** — Eagle with static and dynamic token trees
7. **[Model Catalogue](inference/model-catalogue.md)** — per-model sharding / KV-cache / spec-dec / quantization deltas

## Companion wikis

- [`neuron-jax-stack/wiki/`](../../neuron-jax-stack/wiki/) — the underlying PJRT and collectives stack
- [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) — neuronx-cc subprocess invoked during ModelBuilder trace
- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — runtime that executes the compiled models
