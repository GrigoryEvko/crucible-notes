# xla_* Flag Atlas

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes). Other versions differ.*

## Abstract

libtpu registers exactly **2048 `absl::Flag<T>` globals** (the `AbslFlagHelpGenFor<name>` symbol count). Every one of them is settable through a single funnel — `LIBTPU_INIT_ARGS` parsed by `absl::ParseCommandLine` — so the flag surface is, in effect, libtpu's entire command line. This page is the **grouped atlas** of that surface: not a flat 2048-row dump (that would be the anti-pattern this wiki exists to avoid) but a per-family taxonomy with per-subsystem deep-dives into the ~100 highest-signal knobs, each tagged with its inferred type, the proto field it backs where known, and a confidence label.

The reference frame is XLA's own flag system. The non-TPU `xla_*` flags are fields of `xla::DebugOptions`, registered by `xla::MakeDebugOptionsFlags @ 0x1e66ce80` (confirmed: it takes a `vector<tsl::Flag>*` and a `DebugOptions*` and binds each field to a `--xla_foo` flag). The TPU-private families (`xla_tpu_*`, `xla_jf_*`, `xla_sc_*`, `megascale_*`, `barna_core_*`) are standalone `absl::Flag` globals whose values land in `TpuCompilationEnvironment` (TCE) via `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640`, *not* in DebugOptions. Which proto a name lands in is the single most consequential structural fact for a reimplementer; that taxonomy is owned by [flag-families.md](flag-families.md) and the protos by [debugoptions-proto.md](debugoptions-proto.md) and [tpu-compilation-environment.md](tpu-compilation-environment.md). This page owns the **catalog**: the grouped name space and per-flag type/effect.

The authoritative name enumeration is the mangled helper-symbol set: every `absl::Flag<T> FLAGS_<name>` emits an `_ZN<len>AbslFlagHelpGenFor<name>8NonConstEv` symbol, so that symbol set is a 1:1 census of registered flags (length-prefix parsing recovers each `<name>` exactly); `nm | rg -o 'AbslFlagHelpGenFor...8NonConstEv' | sort -u | wc -l` returns 2048. Every catalogued name on this page resolves to an `AbslFlagHelpGenFor<name>` symbol in the binary. **Types are convention-inferred** for ~99% of flags (the `enable_/use_/allow_` ⇒ bool, `_ms/_kib/_count` ⇒ int, `_ratio/_factor` ⇒ float, `_file/_path` ⇒ string, `_mode/_level` ⇒ enum heuristic XLA itself uses to register them); only a handful of defaults and types are byte-evidenced — most from `=value` clauses in error strings, plus `xla_tpu_embedding_table_oblongness_threshold` recovered from its `AbslFlagDefaultGenFor` initializer. Treat every type and default below as `HIGH` unless a row says `CERTAIN` (byte-evidenced) or `LOW` (ambiguous suffix).

For navigation, the contract is:

- **The family taxonomy** — prefix → owning proto → count, so a reader knows where a name's field lives before chasing it.
- **The per-subsystem high-signal catalog** — the ~100 flags a reimplementer of the TPU pipeline actually needs, grouped by scheduler / fusion / MSA / collectives / SparseCore / layout / numerics / autotune / debug / runtime.
- **The certainty boundary** — which rows are byte-confirmed, and the err-string direction-of-default trap on the rest.

| | |
|---|---|
| **Name census** | 2048 registered `absl::Flag` (`AbslFlagHelpGenFor*` symbols, `sort -u`) |
| **Enumeration symbol** | `_ZN<len>AbslFlagHelpGenFor<name>8NonConstEv` (1 per flag) |
| **DebugOptions registrar** | `xla::MakeDebugOptionsFlags @ 0x1e66ce80` (binds `xla_*` fields) |
| **TCE flag→field bridge** | `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` (TPU families) |
| **Funnel** | `LIBTPU_INIT_ARGS` (str @ file `0x918c880`) → `absl::ParseCommandLine` |
| **Type split (inferred, all 2048)** | ≈ bool 68% · int 21% · string · float · enum (suffix-convention, not byte-typed) |
| **Byte-confirmed types/defaults** | ~18 (most from `=value` error strings; oblongness from `AbslFlagDefaultGenFor`) |
| **Confidence** | HIGH (convention-inferred) unless a row says CERTAIN (byte-evidenced) or LOW |

---

## 1. Family Taxonomy — At a Glance

The prefix is the routing key: it decides which proto consumes the flag and which compiler/runtime subsystem owns its semantics. The counts below are per-prefix `AbslFlagHelpGenFor*` symbol counts and sum to the 2048 registered total. The `Lands in` column is the central distinction — `xla_tpu_*` are **not** DebugOptions fields, a trap [overview.md](overview.md) §3 flags as the GOOD/BAD divide.

| Family | Count | Type-dominant | Lands in | Subsystem owner |
|---|---:|---|---|---|
| `xla_tpu_*` | 909 | bool / int / float | TCE (standalone) | TPU compiler + runtime |
| `(other + xla_vf_/xla_pf_)` | 429 | mixed | n/a (vendored libs) | absl / grpc / protobuf / OR-tools |
| `megascale_*` | 150 | bool 73 / int 47 / str 14 | standalone `absl::Flag` | DCN collective runtime |
| `xla_jf_*` | 148 | bool 109 / int 23 | TCE | Jellyfish XLA backend |
| `xla_*` (plain) | 121 | bool / int / enum | `xla::DebugOptions` | generic XLA |
| `xla_sc_*` | 92 | bool 73 / int 13 | TCE | SparseCore LLVM backend |
| `tpu_*` | 69 | bool / int / str | runtime/cache/driver | TPU runtime |
| `barna_core_*` | 61 | float / int / duration | standalone `absl::Flag` | BarnaCore embedding HW |
| `xla_msa_*` | 22 | bool / int / float | TCE + DebugOptions mix | Memory-Space Assignment |
| `tf_*` | 20 | bool | runtime | TF-TPU bridge |
| `xla_gf_*` | 14 | bool / int / enum | TCE | 6acc60406/v7x VMEM/MSA |
| `xla_mosaic_*` | 8 | bool / enum | TCE | Mosaic MLIR dialect |
| `xla_ior_*` | 4 | bool / enum | TCE | "IOR" fast-mem MSA variant |
| `xla_llo_*` | 1 | enum | TCE | LLO annotation lifecycle |

> **GOTCHA —** the 429 `(other)` registered flags are almost all not TPU flags — 412 are `absl`, gRPC, protobuf, OR-tools, and `cp_model` library flags statically linked into the 745 MB binary; the remaining 17 are the tiny gen-codename mirrors (`xla_vf_*` 16, `xla_pf_*` 1) folded in here rather than given their own rows. (The owner partition on [flag-catalog-full.md](../appendix/flag-catalog-full.md) breaks `xla_vf_` out separately and reports the pure vendored-lib bucket as 412.) A reimplementer enumerating `AbslFlagHelpGenFor*` symbols must filter to the `xla*` / `tpu*` / `megascale*` / `barna_core*` prefixes or pull in OR-tools' entire flag surface (`absl_flags_*`, `cp_model_*`). They are still settable through `LIBTPU_INIT_ARGS`, but they configure the vendored solvers, not the TPU compiler.

> **NOTE —** there are **zero `xla_gpu_*` flags** registered (no `AbslFlagHelpGenForxla_gpu_*` symbol exists), yet 17 GPU/CPU fields survive in the *shared* `DebugOptions` descriptor as proto-only, flag-less fields. The TPU build strips the GPU flag wiring but keeps the GPU fields in the proto. The proto-only set is enumerated on [debugoptions-proto.md](debugoptions-proto.md).

The `xla_tpu_*` family — the bulk of the surface — itself splits across the subsystems the rest of this page deep-dives:

| `xla_tpu_*` subsystem | Count | Deep-dive |
|---|---:|---|
| misc / uncategorized | 229 | (long tail; not catalogued individually) |
| ICI / collectives | 174 | [§5](#5-collectives--ici-174-xla_tpu_) |
| fusion | 101 | [§3](#3-fusion-101-xla_tpu_) |
| debug / dump / log | 77 | [§9](#9-debug--dump--log--trace) |
| MSA / memory-space | 55 | [§4](#4-memory-space-assignment-msa) |
| SparseCore | 50 | [§6](#6-sparsecore--barnacore-embedding) |
| scheduler | 47 | [§2](#2-scheduler-47-xla_tpu_) |
| auto-sharding / SPMD | 40 | [§7](#7-layout--auto-sharding) |
| layout | 29 | [§7](#7-layout--auto-sharding) |
| memory / allocation | 27 | [§8](#8-memory--allocation--runtime) |
| dot / conv | 24 | (representative rows in [§3](#3-fusion-101-xla_tpu_)) |
| autotune / AutoFDO | 24 | [§10](#10-autotune--autofdo) |
| numerics / precision | 21 | [§3](#3-fusion-101-xla_tpu_) |
| cost-model | 8 | [§2](#2-scheduler-47-xla_tpu_) |
| runtime | 3 | [§8](#8-memory--allocation--runtime) |

---

## 2. Scheduler (47 `xla_tpu_`)

### Purpose

libtpu advertises **five distinct latency-hiding scheduler engines** behind separate gates — a reimplementer who assumes a single scheduler will mis-model the pipeline. The master gate is `xla_tpu_enable_latency_hiding_scheduler`; the four alternatives (`ilp`, `brkga`, `dozer`, `lem`) are independent variants. The BRKGA engine (Biased Random-Key Genetic Algorithm) carries its own population-tuning sub-family. The generic `xla_*` and `xla_jf_*` siblings (76 in the full scheduler group) carry the LHS resource model and the BRKGA fallback knobs.

### Catalog — TPU scheduler gates

| Flag | Type | Default | Effect |
|---|---|---|---|
| `xla_tpu_enable_latency_hiding_scheduler` | bool | (unrec) | master LHS gate |
| `xla_tpu_enable_ilp_latency_hiding_scheduler` | bool | (unrec) | ILP-formulated LHS |
| `xla_tpu_enable_brkga_latency_hiding_scheduler` | bool | (unrec) | genetic (BRKGA) scheduler |
| `xla_tpu_enable_dozer_latency_hiding_scheduler` | bool | (unrec) | "Dozer" variant |
| `xla_tpu_enable_lem_scheduler` | bool | (unrec) | LEM variant |
| `xla_tpu_consider_lp_llo_scheduler` | bool | (unrec) | LP-based LLO scheduler |
| `xla_tpu_enable_latency_hiding_layer_scheduler` | bool | (unrec) | per-layer LHS |
| `xla_tpu_enable_multi_compute_overlap_in_layer_scheduler` | bool | (unrec) | multi-compute overlap |
| `xla_tpu_aggressive_flexible_annotation_scheduling` | bool | (unrec) | annotation aggressiveness |
| `xla_tpu_scheduling_annotation_deannotate_unsupported_groups` | AutoOr&lt;bool&gt; | **false** (AUTO→off) | deannotate annotation gaps |
| `xla_tpu_enable_all_experimental_scheduler_features` | bool | (unrec) | turns on all experimental sched features |

### Catalog — BRKGA tuning + generic LHS

| Flag | Type | Effect |
|---|---|---|
| `xla_tpu_brkga_latency_hiding_scheduler_generation_limit` | int | BRKGA generations |
| `xla_tpu_brkga_latency_hiding_scheduler_num_chromosomes` | int | BRKGA population |
| `xla_tpu_brkga_latency_hiding_scheduler_num_top_heap_computations` | int | BRKGA elite set |
| `xla_tpu_brgka_latency_hiding_scheduler_no_progress_limit` | int | BRKGA stall cutoff (note `brgka` typo) |
| `xla_hlo_scheduling_brkga_generation_limit` | int | generic BRKGA generations |
| `xla_hlo_scheduling_brkga_enable_as_fallback` | bool | use BRKGA only as fallback |
| `xla_latency_hiding_scheduler_rerun` | bool | re-run LHS pass |
| `xla_latency_hiding_scheduler_resource_serializing` | bool | serialize resource use |
| `xla_latency_hiding_scheduler_enable_selective_resources` | bool | selective resource tracking |
| `xla_lhs_prioritize_async_depth_over_stall` | bool | async-depth priority |
| `xla_lhs_make_all_gather_selective` | bool | selective AG overlap |
| `xla_lhs_threshold_for_applying_output_fusion_latency_multiplier` | float | output-fusion latency mult. threshold |
| `xla_jf_vliw_scheduler` | bool | Jellyfish VLIW post-scheduler |
| `xla_jf_critical_path_scheduler` | bool | critical-path scheduler |
| `xla_hlo_parse_memory_schedule_from_file` | string | replay a fixed schedule |

The 8 cost-model flags feed the scheduler's latency estimates: `xla_tpu_emitter_learned_cost_model_options` (string/proto — a learned-cost proto with no shipped ML client), `xla_tpu_enable_instruction_cycle_checking` (bool), `xla_tpu_hbm_initial_cycle_penalty` (int), `xla_tpu_break_of_accum_cost_heuristic` (bool), plus the generic `xla_jf_random_latency` and `xla_jf_use_cost_based_memory_coloring`.

> **QUIRK —** the `brgka` spelling in `xla_tpu_brgka_latency_hiding_scheduler_no_progress_limit` is a typo in the flag name itself, distinct from the correctly-spelled `brkga` knobs. A reimplementer copying the BRKGA family by pattern will silently drop this knob unless they match both spellings. The typo is in the registered `AbslFlagHelpGenFor` symbol — it is the real flag name, not an extraction artifact.

---

## 3. Fusion (101 `xla_tpu_`)

### Purpose

Fusion is the second-largest `xla_tpu_` subsystem and carries the only cluster of byte-evidenced defaults on the whole surface — four of the five `=value` error strings live here. The gates control read-write-buffer (RWB) fusion, dot→dot chaining, nested-dot (PartialReduce) fusion, MRB accumulation, and the numerical tolerances for deep fusion. The generic `xla_jf_*` conv/multi-output fusion knobs and the SparseCore fusion gate round out the group.

### Catalog — fusion gates (byte-evidenced cluster)

| Flag | Type | Default | Effect |
|---|---|---|---|
| `xla_tpu_rwb_fusion` | bool | **true** | read-write-buffer fusion |
| `xla_tpu_dot_dot_fusion` | bool | **true** | dot→dot fusion |
| `xla_tpu_nested_dot_fusion` | bool | **true** | nested-dot (PartialReduce) fusion |
| `xla_tpu_accumulate_into_mrb` | bool | **true** | MRB accumulation fusion |
| `xla_tpu_allow_deeply_nested_fusion_numerical_diff` | bool | **true** | tolerate deep-fusion numerics |
| `xla_tpu_fusion_debugger_instrument_inputs` | AutoOr&lt;bool&gt; | **false** (`Gen` `movw $0`→AUTO; off if consumer AUTO→off) | fusion-debugger input instrumentation |
| `xla_tpu_allow_input_fusion_in_certain_reduce_ops` | bool | (unrec) | reduce-op input fusion |
| `xla_tpu_allow_conv_input_fusion_with_downcast_convert` | bool | (unrec) | conv input fusion w/ downcast |
| `xla_tpu_wrap_fusion_lowerable_hlos_in_loop_fusion` | bool | (unrec) | wrap lowerable HLOs |
| `xla_tpu_enable_experimental_fusion_cost_model` | bool | (unrec) | experimental fusion cost model |

### Catalog — generic fusion + dot/conv + numerics

| Flag | Type | Effect |
|---|---|---|
| `xla_jf_enable_multi_output_fusion` | bool | multi-output fusion |
| `xla_jf_enable_producer_consumer_multi_output_fusion` | bool | producer/consumer MOF |
| `xla_jf_fusion_max_vmem_mib` | int | per-fusion VMEM cap (MiB) |
| `xla_sc_enable_instruction_fusion` | bool | SparseCore instruction fusion |
| `xla_tpu_enable_dot_strength_reduction` | bool | dot → cheaper op |
| `xla_tpu_enable_ragged_dot_kernel` | bool | ragged-dot kernel |
| `xla_tpu_choose_faster_windowed_einsum_over_mem` | bool | windowed-einsum speed/mem tradeoff |
| `xla_jf_conv_full_precision` | bool | full-precision conv |
| `xla_jf_auto_assign_mxu` | bool | auto MXU assignment |
| `xla_tpu_accurate_exp` / `_log1p` / `_logistic` | bool | accurate transcendental family |
| `xla_tpu_bf16_emission_mode` | enum | bf16 emission policy |
| `xla_tpu_experimental_enable_dynamic_int8_quantization` | bool | dynamic int8 quant (experimental) |

> **GOTCHA —** the help/error-string `=value` clause is the value the *message tells you to set* — **not** the registered default. The byte-authoritative default is the `FLAGS_<name>` inline literal at `FlagImpl+0x48`, and for this cluster it is `01 00 00 00` = **true** in every case: `rwb_fusion`, `dot_dot_fusion`, `accumulate_into_mrb`, `nested_dot_fusion`, and `allow_deeply_nested_fusion_numerical_diff` are **all `true` by default**. The error strings (e.g. in `PartialReduceEmitter::ValidateShapes @ 0x10eaa120`, `AssignMrbEntriesToChains @ 0x10f4ac60`) offer `=false`/`=true` as a *workaround to flip an on-by-default knob*, so reading the suggested value as the default inverts it. Trust the `+0x48` union, never the prose; see [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md).

---

## 4. Memory-Space Assignment (MSA)

### Purpose

MSA controls where buffers live (VMEM / CMEM / HBM), how async copies prefetch across the memory hierarchy, and how the scoped-memory allocator (`telamalloc`) packs them. The knobs split three ways: the `xla_tpu_*` MSA family (55), the dedicated `xla_msa_*` namespace (22), and the per-generation `xla_gf_vmem_*` (6acc60406) / `xla_ior_fast_mem_*` overlays. Many MSA fields resolve through the AUTO tri-state rather than carrying a flat default — see [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md).

### Catalog — `xla_tpu_*` MSA

| Flag | Type | Effect |
|---|---|---|
| `xla_tpu_alternate_memory_benefit_scaling_factor_for_large_buffers` | float | MSA benefit scaling |
| `xla_tpu_async_copy_bandwidth_scaling_factor` | float | async-copy BW model |
| `xla_tpu_allocate_scoped_vmem_at_same_offset` | bool | scoped VMEM offset reuse |
| `xla_tpu_allocate_scoped_cmem_at_same_offset` | bool | scoped CMEM offset reuse |
| `xla_tpu_allow_in_cmem_copy` | bool | permit copies into CMEM |
| `xla_tpu_scoped_cmem_for_all_reduce` | bool | scoped CMEM for all-reduce |
| `xla_tpu_vmem_scavenging_mode` | enum | VMEM scavenger policy |
| `xla_tpu_vmem_use_telamalloc` | bool | telamalloc VMEM allocator |
| `xla_tpu_scoped_vmem_limit_kib` | int | scoped-VMEM byte budget (KiB) |

### Catalog — `xla_msa_*` namespace (22)

| Flag | Type | Effect |
|---|---|---|
| `xla_msa_enable` | bool | MSA master gate |
| `xla_msa_max_outstanding_prefetches` | int | prefetch concurrency cap |
| `xla_msa_max_outstanding_evictions` | int | eviction concurrency cap |
| `xla_msa_max_cross_program_prefetches` | int | XPP count cap |
| `xla_msa_max_repacks` / `_max_retries` | int | repack / retry budgets |
| `xla_msa_min_overlap_to_async_copy_ratio` | float | min overlap ratio |
| `xla_msa_preferred_overlap_to_async_copy_ratio` | float | preferred overlap ratio |
| `xla_msa_max_overlap_to_mem_size_async_copy_ratio` | float | overlap-vs-memsize ratio |
| `xla_msa_enable_window_prefetch` | bool | window prefetch |
| `xla_msa_enable_sync_copy_replacement` | bool | sync→async copy replacement |
| `xla_msa_expanded_scoped_alternate_memory_mode` | enum | scoped-alt-mem mode |
| `xla_msa_experimental_ior_algorithm` | enum | "IOR" eviction algorithm (experimental) |
| `xla_msa_use_bundle_aware_cost_model` | bool | bundle-aware cost model |
| `xla_msa_cost_model_options` | string | cost-model config string |

Per-generation overlays: `xla_gf_vmem_max_outstanding_evictions` / `_max_repacks` / `_max_retries` (int, 6acc60406), `xla_gf_vmem_use_ior_algorithm` (enum), `xla_ior_fast_mem_*` (4 flags, the fast-mem round-trip MSA variant). The generic `xla_enable_cross_program_prefetch` and `xla_default_cross_program_prefetch_heuristic` gate XPP at the DebugOptions level.

---

## 5. Collectives / ICI (174 `xla_tpu_`)

### Purpose

The largest `xla_tpu_` subsystem. It covers the inter-chip-interconnect (ICI) collective emitters (all-reduce, all-gather, reduce-scatter, all-to-all), the resilient/fault-aware route selection, the `sflag` (sync-flag) wait watchdogs and hang-attribution telemetry, and the ICI-SDC (silent-data-corruption) test harness. The `megascale_*` family (§ separate) is the DCN runtime layer above these.

### Catalog — collective emitters + sflag watchdogs

| Flag | Type | Default | Effect |
|---|---|---|---|
| `xla_tpu_enable_sparse_core_reduce_scatter_v2` | AutoOr&lt;bool&gt; | **true** (AUTO→on, but `TpuVersion`+second-field composite at `EnableSparseCoreReduceScatterV2 @ 0x1d6b8660`) | SC ND reduce-scatter v2 |
| `xla_tpu_all_gather_collective_matmul_mode` | enum | (unrec) | collective-matmul AG mode |
| `xla_tpu_all_gather_step_count` | int | (unrec) | AG ring step count |
| `xla_tpu_all_reduce_vmem_contingency_kib` | int | (unrec) | AR VMEM reserve (KiB) |
| `xla_tpu_all_to_all_max_rdma_size_kib` | int | (unrec) | A2A RDMA chunk cap (KiB) |
| `xla_tpu_1d_uni_direction_ring_min_input_size_chunks` | int | (unrec) | 1-D ring threshold |
| `xla_tpu_use_resilient_collective_emitter` | bool | (unrec) | fault-aware route table |
| `xla_tpu_add_barriers_around_aggregated_collectives` | bool | (unrec) | barrier wrapping |
| `xla_tpu_force_startup_barrier_in_binomial_all_reduce` | bool | (unrec) | startup barrier |
| `xla_tpu_combine_quantized_all_reduce_operands` | bool | (unrec) | quantized-AR operand combine |
| `xla_tpu_checksum_all_reduce_transfers` | bool | (unrec) | AR transfer checksum |
| `xla_tpu_debug_sflag_wait_timeout_ms` | int | (unrec) | TC sflag-wait watchdog |
| `xla_tpu_debug_sc_sflag_wait_timeout_ms` | int | (unrec) | SC sflag-wait watchdog |
| `xla_tpu_collect_sflag_wait_stats` | bool | (unrec) | sflag-wait stats master |
| `xla_tpu_collect_sflag_wait_hang_core` | bool | (unrec) | hang-attribution: core |
| `xla_tpu_collect_sflag_wait_hang_rate` | float | (unrec) | hang-rate stat |

### Catalog — generic collectives + ICI-SDC harness

| Flag | Type | Effect |
|---|---|---|
| `xla_enable_async_all_gather` | bool | async AG (DebugOptions) |
| `xla_enable_async_all_reduce` | bool | async AR (DebugOptions) |
| `xla_enable_async_reduce_scatter_fusion` | bool | async RS fusion |
| `xla_all_gather_combiner_threshold_count` | float | AG combiner threshold |
| `xla_all_reduce_latency_bound_threshold_in_bytes` | float | AR latency-bound threshold |
| `xla_enable_all_gather_2d_emitter` / `_3d_emitter` | bool | 2D/3D AG emitter |
| `xla_tpu_ici_sdc_test_iterations` | int | ICI-SDC test iterations |
| `xla_tpu_ici_sdc_test_packet_size_chunks` | int | ICI-SDC packet size |
| `xla_tpu_ici_sdc_test_inject_mismatch_for_testing_only` | bool | inject ICI mismatch (testonly) |
| `xla_tpu_ici_sdc_test_run_on_program_start` | bool | run harness at program start |

The ICI-SDC test sub-family has 10 members (`_iterations`, `_packet_size_chunks`, `_buffer_size_chunks`, `_delay_mask`, `_pipeline_depth`, `_max_distance`, `_emit_compact_code`, `_run_on_program_start`, `_inject_mismatch_for_testing_only`, `_sflag_wait_timeout_ms`) — a self-test harness, not production tuning.

---

## 6. SparseCore + BarnaCore Embedding

### Purpose

Two families serve the SparseCore (SC) embedding path: `xla_sc_*` (92) are the SparseCore LLVM-backend compiler/codegen knobs, and `barna_core_*` (61) are the BarnaCore HW embedding-accelerator runtime tunables. The `xla_tpu_*` side (50) carries the SC offload gates and the SC SDC checker. SC compiler flags land in TCE; BarnaCore flags are standalone runtime `absl::Flag` globals.

### Catalog — `xla_tpu_*` SC offload + `xla_sc_*` compiler

| Flag | Type | Default | Effect |
|---|---|---|---|
| `xla_tpu_enable_offloading_gather_to_sparsecore` | bool | **false** | gather offload to SC |
| `xla_tpu_enable_offloading_scatter_to_sparsecore` | enum (Tristate) | **ENABLED** (`Gen` `movb $2`) | scatter offload to SC |
| `xla_tpu_enable_sc_log_recorder` | AutoOr&lt;bool&gt; | **false** (AUTO→off) | SC log recorder |
| `xla_tpu_embedding_table_oblongness_threshold` | float | **50.0** | embedding-table oblongness cutoff |
| `xla_tpu_enable_sc_sdc_checker` | bool | (unrec) | SparseCore SDC checker |
| `xla_tpu_aggregate_data_dependent_sc_ops` | bool | (unrec) | data-dependent SC aggregation |
| `xla_sc_enable_instruction_fusion` | bool | (unrec) | SC instruction fusion |
| `xla_sc_enable_latency_hiding_scheduler` | bool | (unrec) | SC LHS |
| `xla_sc_enable_tile_overlays` / `_scs_overlays` | bool | (unrec) | tile / SCS overlays |
| `xla_sc_enable_stack_eliding` | bool | (unrec) | stack eliding |
| `xla_sc_enable_hbm_optimization_mode` | enum | (unrec) | SC HBM optimization mode |
| `xla_sc_detect_nan` | bool | (unrec) | SC NaN detection |
| `xla_sc_assert_level` | enum | (unrec) | SC assertion level |
| `xla_sc_dump_llvm_ir_to` | string | (unrec) | dump SC LLVM IR |
| `xla_sc_use_legacy_embeddings_loop_configs` | bool | (unrec) | legacy embeddings loop configs |

### Catalog — `barna_core_*` embedding runtime (61)

| Flag | Type | Effect |
|---|---|---|
| `barna_core_max_hbm_fraction_for_embeddings` | float | HBM fraction cap for embeddings |
| `barna_core_override_tpu_table_limit_fraction` | float | per-table limit override |
| `barna_core_software_row_sharding_hbm_usage_fraction_limit` | float | SW row-sharding HBM cap |
| `barna_core_master_partitioner_thread_count` | int | partitioner threads |
| `barna_core_hot_id_profiler_top_n_multiple` | int | hot-ID profiler top-N |
| `barna_core_file_operation_timeout` | duration/int | file-op timeout |
| `barna_core_embedding_common_config_proto_path` | string | embedding config proto path |
| `barna_core_partitioner_optimization_objective` | enum | partitioner objective |

---

## 7. Layout + Auto-Sharding

### Purpose

Layout knobs (29 `xla_tpu_`) control tiling, the "large 2nd-minor" layout per element width (x16/x8/x4), relayout, and layout negotiation. Auto-sharding / SPMD (40 `xla_tpu_` + 8 generic) controls the auto-SPMD partitioner's memory budget and solver, plus user-sharding preservation.

### Catalog — layout + sharding

| Flag | Type | Effect |
|---|---|---|
| `xla_tpu_allow_layout_negotiation` | bool | layout negotiation gate |
| `xla_tpu_enable_large_2nd_minor_layout` | int | large 2nd-minor layout master |
| `xla_tpu_allow_large_2nd_minor_layout_for_x16` | int | per-x16 variant |
| `xla_tpu_allow_large_2nd_minor_layout_for_x8` | int | per-x8 variant |
| `xla_tpu_allow_large_2nd_minor_layout_for_x4` | int | per-x4 variant |
| `xla_tpu_allow_sharding_on_minor_dim` | int | minor-dim sharding |
| `xla_tpu_auto_spmd_partitioning_memory_budget_gb` | int | auto-SPMD memory budget (GB) |
| `xla_tpu_auto_spmd_partitioning_memory_budget_ratio` | float | budget ratio |
| `xla_tpu_auto_spmd_partitioning_solver_timeout_seconds` | int | solver wall-clock cap |
| `xla_tpu_auto_spmd_keep_all_user_shardings` | bool | preserve user shardings |
| `xla_tpu_auto_spmd_remove_all_user_shardings` | bool | strip user shardings |
| `xla_tpu_autotune_shardings` | bool | sharding autotune |
| `xla_jf_spmd_threshold_for_windowed_einsum_mib` | float | windowed-einsum SPMD threshold (MiB) |
| `xla_jf_bf16_propagation` | bool | bf16 propagation |

> **GOTCHA —** `xla_tpu_allow_large_2nd_minor_layout_for_x16` and its `_x8` / `_x4` siblings are typed `int`, not `bool`, despite the `allow_` prefix that elsewhere signals a boolean. The `_for_x16` suffix implies a tri-state-or-count integer, not an on/off gate (`LOW` confidence — the type was inferred from suffix, not byte-confirmed). A reimplementer must not assume every `allow_*` flag is boolean.

---

## 8. Memory / Allocation + Runtime

### Purpose

Allocation knobs (27 `xla_tpu_` + generic) control OOM handling, HBM/VMEM/SMEM spilling, defragmentation, and allocation backtraces. The runtime/cache/driver family (`tpu_*`, 69) controls the compilation cache, driver watchdogs, and core-dump behavior — these are runtime, not compile-time, knobs.

### Catalog — allocation + runtime/cache

| Flag | Type | Default | Effect |
|---|---|---|---|
| `xla_tpu_impure_oom_fast_exit_threshold` | int | **10** (`+0x48`=`0x0a`) | OOM fast-exit threshold |
| `xla_enable_megacore_hbm_spill` | bool | **true** | megacore HBM spill |
| `xla_tpu_always_spill_to_default_memory` | bool | (unrec) | always spill to default mem (proto field) |
| `xla_jf_poison_vmem_allocations` | bool | (unrec) | poison VMEM allocs (debug) |
| `xla_jf_memory_allocator_include_backtrace` | bool | (unrec) | alloc backtraces |
| `xla_jf_lsra_v2_spill_reporter_threshold` | int | (unrec) | LSRA spill-report threshold |
| `xla_hbm_logging_buffer_size_bytes` | int | (unrec) | HBM log buffer size |
| `tpu_compilation_cache_persists_in_riegeli` | bool | (unrec) | cache persistence format |
| `tpu_persistent_compilation_cache_location` | string | (unrec) | cache location path |
| `tpu_persistent_compilation_cache_ttl_secs` | int | (unrec) | cache TTL |
| `tpu_driver_callback_watchdog_timeout` | int | (unrec) | driver watchdog timeout |
| `tpu_core_dump_directory` | string | (unrec) | core-dump directory |
| `tpu_log_allocations_on_oom` | bool | (unrec) | log allocations on OOM |
| `DANGEROUS_tpu_runtime_abi_verification_disabled` | bool | (unrec) | **disables ABI verification** |

> **QUIRK —** `xla_tpu_impure_oom_fast_exit_threshold` defaults to **10** (byte-evidenced: inline `FlagImpl+0x48` = `0x0a`, no `Gen` reloc) — a positive count, not a `-1` "disabled" sentinel. The `impure_` prefix is a libtpu naming convention marking ~30 non-deterministic / logging / side-effecting knobs (`impure_cost_model_logging_options`, `impure_llo_lifecycle_log_mode`, `impure_probability_of_host_offloading`). A reimplementer should treat `impure_` flags as runtime-observable side channels, not pure compile decisions.

---

## 9. Debug / Dump / Log / Trace

### Purpose

77 `xla_tpu_` debug/dump knobs plus the generic `xla_jf_dump_*` and `xla_enable_*_trace` families (181 in the full group). These control HLO/LLO/MLIR dumps, tracing, NaN/SDC checking, and the log recorders. The `xla_jf_dump_*` family is the Jellyfish-backend dump surface; `xla_sc_dump_*` is the SparseCore equivalent.

### Catalog — dump / trace / verify

| Flag | Type | Default | Effect |
|---|---|---|---|
| `xla_tpu_enable_tile_log_recorder` | bool | **false** | tile log recorder |
| `xla_jf_debug_level` | int | **1** | Jellyfish debug verbosity |
| `xla_jf_run_verifier` | bool | **false** | run HLO verifier |
| `xla_jf_dump_to` | string | (unrec) | Jellyfish dump directory |
| `xla_jf_dump_hlo_text` | bool | (unrec) | dump HLO text |
| `xla_jf_dump_llo_html` | bool | (unrec) | dump LLO HTML |
| `xla_jf_dump_isa_program_proto` | string | (unrec) | dump ISA program proto |
| `xla_jf_dump_extended_fingerprint` | string | (unrec) | extended fingerprint dump |
| `xla_jf_collect_llo_stack_trace` | bool | (unrec) | collect LLO stack trace |
| `xla_sc_dump_llvm_ir_to` | string | (unrec) | dump SC LLVM IR |
| `xla_sc_dump_mlir_to` | string | (unrec) | dump SC MLIR |
| `xla_enable_hlo_trace` | bool | (unrec) | HLO trace |
| `xla_enable_mxu_trace` | bool | (unrec) | MXU trace |
| `xla_enable_transpose_trace` | bool | (unrec) | transpose trace |
| `xla_dump_hlo_memory_schedule_info` | bool | (unrec) | dump memory schedule info |

### Catalog — LLVM-emitter dumps (`xla_llvm_*`, 4)

| Flag | Type | Effect |
|---|---|---|
| `xla_llvm_isa_emitter` | bool | enable LLVM→ISA emitter |
| `xla_llvm_isa_emitter_bundles` | bool | emit instruction bundles |
| `xla_llvm_isa_emitter_force` | bool | force the LLVM ISA emitter |
| `xla_llvm_generate_xla_compatible_dwg` | bool | XLA-compatible debug-with-graph |

---

## 10. Autotune / AutoFDO

### Purpose

23 `xla_tpu_autofdo_*` flags drive profile-guided optimization: fingerprint-keyed loading of pre-tuned flags, layouts, schedules, and shardings, plus the FlagNet predictor. AutoFDO is a fingerprint→tuning cache: a module's fingerprint keys a stored set of decisions that bypass the live cost models.

### Catalog — AutoFDO

| Flag | Type | Effect |
|---|---|---|
| `xla_tpu_autofdo` | bool | AutoFDO master gate |
| `xla_tpu_autofdo_profile_file` | string | profile file path |
| `xla_tpu_autofdo_load_module_layout_fingerprint` | string | per-module layout fingerprint |
| `xla_tpu_autofdo_load_module_flag_fingerprint` | string | per-module flag fingerprint |
| `xla_tpu_autofdo_module_flags` / `_module_layouts` | bool | apply flag / layout tunings |
| `xla_tpu_autofdo_flagnet` | enum | FlagNet predictor mode |
| `xla_tpu_autofdo_flagnet_confidence_threshold` | int | FlagNet confidence cutoff |
| `xla_tpu_autofdo_hlo_module_size_threshold` | int | size threshold for AutoFDO |
| `xla_tpu_autotune_layouts` / `_schedules` / `_shardings` | bool | autotune layouts/schedules/shardings |
| `xla_tpu_autofdo_proposed_layout_file` | string | proposed-layout file |

---

## 11. The Certainty Boundary

The entire catalog above rests on two extraction methods with different trust levels, and a reimplementer must respect the seam.

**Names — `CERTAIN`.** The 2048 registered names come from the `AbslFlagHelpGenFor<name>` mangled-symbol set, which is a 1:1 enumeration of `absl::Flag` globals (`sort -u | wc -l` = 2048). Every name catalogued on this page resolves to such a symbol. Additional flag-like strings appear in `.rodata` (deprecated aliases / error-message references) but are not registered flags; they are not counted in the 2048 and are out of scope here.

**Types — `HIGH`, mostly inferred.** Only the suffix convention (XLA's own registration convention) types ~99% of flags. The ambiguous suffixes (`_threshold` int-or-float, `_mode`/`_level` int-enum-or-string) are marked `LOW` per row. Byte-confirming a type needs the `absl::Flag<T>` template argument from the `FLAGS_<name>` symbol's RTTI — not done here.

**Defaults — only 18 are `CERTAIN`.** Most come from `=value` clauses in help/error strings; `xla_tpu_embedding_table_oblongness_threshold` is recovered directly from its `AbslFlagDefaultGenFor` initializer (`movl $0x42480000` = `50.0f` @ `0x1d7068c0`), which overrides the `=1` workaround value its error string suggests. Everything else lives in `.text` initializers (`xla::DefaultDebugOptions()` and the per-flag `FLAGS_*` static ctors) not recoverable from strings. The full byte-evidenced set:

| Flag | Default |
|---|---|
| `xla_tpu_accumulate_into_mrb` | true (`+0x48`=`01`) |
| `xla_tpu_rwb_fusion` | true (`+0x48`=`01`) |
| `xla_tpu_dot_dot_fusion` | true (`+0x48`=`01`) |
| `xla_tpu_nested_dot_fusion` | true |
| `xla_tpu_allow_deeply_nested_fusion_numerical_diff` | true |
| `xla_tpu_fusion_debugger_instrument_inputs` | AUTO (`Gen` `movw $0`) → off |
| `xla_tpu_scheduling_annotation_deannotate_unsupported_groups` | false (AutoOr, AUTO→off) |
| `xla_tpu_enable_tile_log_recorder` | false (`+0x48`=`00`) |
| `xla_tpu_enable_sc_log_recorder` | false (AutoOr, AUTO→off) |
| `xla_tpu_enable_sparse_core_reduce_scatter_v2` | true (AutoOr AUTO→on; version composite) |
| `xla_tpu_enable_offloading_gather_to_sparsecore` | false |
| `xla_tpu_enable_offloading_scatter_to_sparsecore` | ENABLED (`Gen` `movb $2`) |
| `xla_tpu_impure_oom_fast_exit_threshold` | 10 (`+0x48`=`0x0a`) |
| `xla_tpu_embedding_table_oblongness_threshold` | 50.0 (float) |
| `xla_enable_megacore_hbm_spill` | true |
| `xla_jf_debug_level` | 1 |
| `xla_jf_run_verifier` | false |
| `megascale_use_numa_aware_threadpool` | true (`+0x48`=`01`) |

> **NOTE —** for the ~330 TCE fields that are `AutoProto` oneofs, "default" is not even a flat value — it is an AUTO-resolution polarity baked into each consumer, optionally rewritten by a per-`TpuVersion` MSA overlay. The effective value is `flag-default ⊕ AUTO-polarity ⊕ per-version-overlay`. That resolution is owned by [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md); this atlas catalogs the flag *names and inferred types*, not their resolved values.

---

## Related Components

| Component | Relationship |
|---|---|
| `AbslFlagHelpGenFor<name> @ symtab` | the 1:1 name-enumeration symbol per flag |
| `xla::MakeDebugOptionsFlags @ 0x1e66ce80` | registers the `xla_*` DebugOptions flags |
| `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` | binds the TPU families into TCE |
| `GetLibTpuInitArguments @ 0x20ccca20` | the `LIBTPU_INIT_ARGS` funnel for all flags |
| `PartialReduceEmitter::ValidateShapes @ 0x10eaa120` | hosts the `nested_dot_fusion=true` evidence string |

## Cross-References

- [overview.md](overview.md) — the four-stage flag→DebugOptions→TCE→effective-value pipeline this atlas sits inside
- [flag-families.md](flag-families.md) — the prefix→owner taxonomy in full; which proto each family lands in
- [env-vars.md](env-vars.md) — `LIBTPU_INIT_ARGS` and the env-var roster that feeds the parse
- [debugoptions-proto.md](debugoptions-proto.md) — `xla::DebugOptions`: the 290-field schema the plain `xla_*` flags back (full descriptor decode; the earlier "111 wire-fields / 94 flag-wired" figure was a partial sample, superseded there)
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the 1121-field TCE proto the `xla_tpu_*` / `xla_jf_*` / `xla_sc_*` flags land in
- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the AUTO tri-state that makes "default" a resolution rule for ~330 fields
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field→offset→default reference where the non-evidenced defaults are recovered
