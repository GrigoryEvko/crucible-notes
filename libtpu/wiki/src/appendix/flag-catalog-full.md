# Flag Catalog (Full)

> *All counts, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). Other versions differ.*

## Abstract

This appendix is the **exhaustive prefix index** of libtpu's flag surface: every `XLA_FLAGS` / `LIBTPU_INIT_ARGS` / `xla.DebugOptions` name string the binary registers, grouped by prefix, with per-group counts, a representative enumerated subset, the inferred type, and the byte-evidenced default where one survives. It is the machine-style companion to the grouped narrative in [xla-flag-atlas.md](../config/xla-flag-atlas.md): the atlas explains *what the high-signal knobs do*; this page is the *complete reference table a reader greps*. Where the two disagree on a count, this page wins — its numbers come directly from the binary.

The authoritative name census is the mangled helper-symbol set. Every `absl::Flag<T> FLAGS_<name>` global emits an `_ZN<len>AbslFlagHelpGenFor<name>8NonConstEv` helper symbol, so that symbol set is a 1:1 enumeration of registered flags; length-prefix parsing recovers each `<name>` exactly. The binary carries **2048** such distinct symbols — the registered-flag count. A further set of names appears only in `.rodata` (deprecated aliases, error-message-only references) that are not backed by a live `AbslFlagHelpGenFor` symbol; folding those in yields **2107** distinct flag *names*. The two numbers answer two different questions: 2048 is "how many flags can you set," 2107 is "how many flag names exist as strings in the binary." Both are used below, labelled explicitly per row.

Every flag is settable through one funnel: `LIBTPU_INIT_ARGS` (env string @ file `0x918c880`) is read by `GetLibTpuInitArguments @ 0x20ccca20`, split argv-style, and handed to `absl::ParseCommandLine` inside `RealInitGoogle @ 0x210ae860`. Because the parse is generic, the *entire* 2048-flag set is reachable through that one variable — there is no init-args-private subset. The `xla_*` (non-TPU) flags also bind to `xla::DebugOptions` fields via `MakeDebugOptionsFlags @ 0x1e66ce80`; the TPU-private families (`xla_tpu_*`, `xla_jf_*`, codenames, `megascale_*`, `barna_core_*`) are standalone globals that land in the `TpuCompilationEnvironment` via `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640`, not in DebugOptions. Which proto a name lands in is owned by [flag-families.md](../config/flag-families.md); this page owns the *grouped name index*.

This appendix is a pure-reference catalog: it carries no reimplementation contract of its own (the registration mechanism is reimplemented from [xla-flag-atlas.md](../config/xla-flag-atlas.md) and [registry-mediated-flags.md](../config/registry-mediated-flags.md)). It provides:

- **A per-prefix count table** — registered count and rodata-name count for each of the ~14 prefix namespaces, with scope and confidence.
- **Per-prefix sections** — for each prefix, the subsystem split (where applicable) and a substantial enumerated subset of names with inferred type, the highest-value TPU-specific flags spelled out in full.
- **The certainty boundary** — the 13 flags whose error-string `=value` remedy clauses survive (these spell the *non-default* direction, not the default), and the convention-inference caveat on the type column for the rest.

| | |
|---|---|
| **Registered flags** (`AbslFlagHelpGenFor` symbols) | **2048** |
| **Distinct rodata flag names** (registered + rodata-only) | **2107** |
| **Enumeration symbol** | `_ZN<len>AbslFlagHelpGenFor<name>8NonConstEv` (1 per registered flag) |
| **DebugOptions registrar** | `MakeDebugOptionsFlags @ 0x1e66ce80` (binds generic `xla_*` fields) |
| **TCE flag→field bridge** | `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` (TPU families) |
| **Funnel** | `LIBTPU_INIT_ARGS` (str @ file `0x918c880`) → `GetLibTpuInitArguments @ 0x20ccca20` → `absl::ParseCommandLine` (`RealInitGoogle @ 0x210ae860`) |
| **Type split** (all 2107 names) | bool 1431 (68%) · int 434 (21%) · string 93 · float 79 · enum/string 70 |
| **Error-string `=value` remedy clauses** | 13 (each spells the *non-default* direction, not the default); actual defaults live in `.text` initializers |
| **`xla_gpu_*` / `xla_cpu_*` registered flags** | **0** (GPU/CPU flag wiring stripped from this TPU build) |

> **NOTE —** two different `xla_tpu_*` counts measure two different things. The binary's `AbslFlagHelpGenFor` symbol set holds **909** *registered* `xla_tpu_*` flags — the settable surface. There are **968** distinct `xla_tpu_*` *name strings* in `.rodata` (909 registered + 59 rodata-only references), which is not the count of settable flags. This page splits the two columns per prefix so the distinction is explicit: use 909 for "how many `--xla_tpu_*=...` you can pass," 968 for "how many `xla_tpu_*` strings exist."

---

## Per-Prefix Index (at a glance)

Every prefix namespace in the catalog, with the registered-flag count (binary `AbslFlagHelpGenFor` census) and the rodata-name count (registered + rodata-only). The "rodata names" column matches the prior 2107-union tally; the "registered" column is what is actually settable. Confidence is `CERTAIN` for counts derived directly from the symbol census, `HIGH` where the rodata-name delta carries concatenation noise.

| Prefix | Registered | Rodata names | Scope | Owner proto |
|---|---:|---:|---|---|
| `xla_tpu_` | 909 | 968 | TPU-specific compiler + runtime knobs | TCE |
| `megascale_` | 150 | 150 | Megascale DCN collective runtime | standalone |
| `xla_jf_` | 148 | 148 | Jellyfish TPU XLA backend (all gens) | TCE |
| `xla_` (plain) | 121 | 138 | Generic XLA (scheduler / MSA / collective / dump) | DebugOptions |
| `xla_sc_` | 92 | 92 | SparseCore compiler (SCS/SCC LLVM backend) | TCE |
| `tpu_` | 69 | 69 | TPU runtime / compilation-cache / driver | standalone |
| `barna_core_` | 61 | 61 | BarnaCore embedding-engine runtime | standalone |
| `xla_msa_` | 22 | 22 | Memory-Space-Assignment (dedicated namespace) | TCE |
| `tf_` | 20 | 20 | TensorFlow-TPU bridge (`tf_jf_*` etc.) | standalone |
| `xla_vf_` | 16 | 16 | Gen-specific VMEM/MSA override mirror (`vf` codename) | TCE |
| `xla_gf_` | 14 | 14 | Gen-specific VMEM/MSA override mirror (`gf` codename) | TCE |
| `xla_mosaic_` | 8 | 8 | Mosaic MLIR custom-kernel dialect | TCE |
| `xla_ior_` | 4 | 4 | "IOR" fast-mem round-trip MSA variant | TCE |
| `xla_pf_` | 1 | 1 | Gen-specific ND-allreduce override (`pf` codename) | TCE |
| `xla_llo_` | 1 | 1 | LLO annotation lifecycle | TCE |
| `xla_gpu_` | 0 | — | GPU backend (proto-only, no flag) | DebugOptions |
| `xla_cpu_` | 0 | — | CPU backend (proto-only, no flag) | DebugOptions |
| `(other / no XLA prefix)` | 412 | 395 | abseil / grpc / protobuf / OR-tools library flags | standalone |
| **Total** | **2048** | **2107** | — | — |

> **NOTE —** "registered total 2048" and "rodata names 2107" are both byte-derived: the registered total is the count of distinct `AbslFlagHelpGenFor<name>8NonConstEv` symbols (2048, confirmed by `rg` over the names sidecar); the 2107 is that set unioned with the rodata-only references (deprecated aliases, error-string mentions). The "other / no XLA prefix" registered count is the residual (2048 − the 1636 XLA/TPU-prefixed registered flags = 412); those are statically-linked library flags (`alsologtostderr`, `alarm_on_failure`, OR-tools `cp_model_*`, grpc internals) of no compiler interest.

---

## `xla_tpu_` — TPU Compiler + Runtime Knobs (909 registered / 968 names)

The dominant family — the TPU-private compiler and runtime knob surface, registered as standalone `absl::Flag` globals that land in `TpuCompilationEnvironment`, **not** in `DebugOptions`. (The sole two exceptions wired to DebugOptions are `xla_tpu_detect_nan` and `xla_tpu_detect_inf` — see the `xla_` section.) Subsystem split, by keyword classification over all 968 `xla_tpu_*` name strings (sums to 968, not the 909 registered subset):

| Subsystem | Count | Keyword signature |
|---|---:|---|
| misc / uncategorized | 288 | (no dominant keyword) |
| ICI / collectives | 174 | `ici`, `all_reduce`, `all_gather`, `reduce_scatter`, `all_to_all`, `collective`, `sflag`, `dcn`, `barrier` |
| fusion | 101 | `fusion`, `fuse`, `rwb`, `dot_dot`, `nested_dot`, `multi_output`, `horizontal` |
| debug / dump / log / trace | 77 | `dump`, `debug`, `log`, `trace`, `verify`, `nan`, `recorder`, `assert` |
| MSA / prefetch / scoped mem | 55 | `msa`, `memory_space`, `prefetch`, `scoped_(v\|c)mem`, `async_copy`, `cmem`, `telamalloc` |
| SparseCore (TC-side) | 50 | `sparse_core`, `sparsecore`, `_sc_`, `embedding`, `minibatch`, `offload` |
| scheduler | 47 | `schedul`, `latency_hiding`, `lhs`, `ilp`, `brkga`, `critical_path` |
| auto-sharding / SPMD | 40 | `sharding`, `spmd`, `partition`, `shardonnay`, `propagat` |
| layout | 29 | `layout`, `minor_dim`, `2nd_minor`, `transpose`, `relayout`, `_x16/_x8/_x4` |
| memory / allocation | 27 | `allocat`, `hbm`, `vmem`, `spill`, `oom`, `defragment` |
| dot / conv | 24 | `dot`, `conv`, `matmul`, `mxu`, `gemm`, `einsum` |
| autotune / autofdo | 24 | `autotun`, `autofdo`, `flagnet` |
| numerics / precision | 21 | `accurate_`, `_exp`, `_log`, `precision`, `bf16`, `fp8`, `stochastic` |
| cost-model | 8 | `cost_model`, `cycle`, `learned_cost`, `roofline` |
| runtime | 3 | `runtime`, `init` |

### Scheduler — five engine gates

The scheduler family advertises five distinct engines, each behind its own gate. Defaults are unrecoverable from strings unless evidenced.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_tpu_enable_latency_hiding_scheduler` | bool | — | master LHS gate |
| `xla_tpu_enable_ilp_latency_hiding_scheduler` | bool | — | ILP-based LHS engine |
| `xla_tpu_enable_brkga_latency_hiding_scheduler` | bool | — | genetic (BRKGA) scheduler engine |
| `xla_tpu_brkga_latency_hiding_scheduler_generation_limit` | int | — | BRKGA generation cap |
| `xla_tpu_brkga_latency_hiding_scheduler_num_chromosomes` | int | — | BRKGA population size |
| `xla_tpu_brkga_latency_hiding_scheduler_num_top_heap_computations` | int | — | BRKGA elite-set size |
| `xla_tpu_brgka_latency_hiding_scheduler_no_progress_limit` | int | — | BRKGA stall cutoff (note `brgka` typo) |
| `xla_tpu_enable_dozer_latency_hiding_scheduler` | bool | — | "Dozer" scheduler variant |
| `xla_tpu_enable_lem_scheduler` | bool | — | LEM scheduler variant |
| `xla_tpu_consider_lp_llo_scheduler` | bool | — | LP-based LLO scheduler |
| `xla_tpu_enable_depth_memory_pressure_reduction` | bool | — | depth-based memory-pressure reduction |
| `xla_tpu_enable_cp_send_done_scheduling` | bool | — | collective-permute send/done sched |
| `xla_tpu_aggressive_flexible_annotation_scheduling` | bool | — | scheduling-annotation aggressiveness |
| `xla_tpu_scheduling_annotation_deannotate_unsupported_groups` | bool | false (errstr remedy `=true`) | deannotate annotation gaps |
| `xla_tpu_enable_all_experimental_scheduler_features` | bool | — | enable all experimental sched features |

> **QUIRK —** the name `xla_tpu_brgka_latency_hiding_scheduler_no_progress_limit` carries a transposed-letter typo (`brgka` vs the `brkga` used by its three siblings). It is a distinct registered flag string, not an alias — a reimplementer must register the misspelt name verbatim or this knob is unreachable.

### ICI / Collectives — largest subsystem (174)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_tpu_debug_sflag_wait_timeout_ms` | int | — | TC sflag-wait watchdog |
| `xla_tpu_debug_sc_sflag_wait_timeout_ms` | int | — | SparseCore sflag-wait watchdog |
| `xla_tpu_use_resilient_collective_emitter` | bool | — | fault-aware route table |
| `xla_tpu_collect_sflag_wait_hang_core` | bool | — | hang-attribution telemetry |
| `xla_tpu_collect_sflag_wait_hang_rate` | float | — | hang-rate statistic |
| `xla_tpu_force_startup_barrier_in_binomial_all_reduce` | bool | — | startup barrier injection |
| `xla_tpu_binomial_all_reduce_use_physical_core_ids` | bool | — | physical-core-id binomial AR |
| `xla_tpu_all_gather_collective_matmul_mode` | enum/string | — | collective-matmul AG mode |
| `xla_tpu_all_gather_step_count` | int | — | AG ring step count |
| `xla_tpu_all_reduce_vmem_contingency_kib` | int | — | AR VMEM reserve |
| `xla_tpu_all_to_all_max_rdma_size_kib` | int | — | A2A RDMA chunk cap |
| `xla_tpu_async_ragged_all_to_all_max_rdma_size_kib` | int | — | ragged A2A RDMA cap |
| `xla_tpu_add_barriers_around_aggregated_collectives` | bool | — | barrier wrapping |
| `xla_tpu_aggressive_opt_barrier_removal` | bool | — | opt-barrier removal |
| `xla_tpu_checksum_all_reduce_transfers` | bool | — | checksum AR transfers |
| `xla_tpu_1d_uni_direction_ring_min_input_size_chunks` | int | — | 1-D ring threshold |

The ICI-SDC test harness contributes a 10-flag sub-family: `xla_tpu_ici_sdc_test_{iterations, packet_size_chunks, buffer_size_chunks, delay_mask, pipeline_depth, max_distance}` (all int), `xla_tpu_ici_sdc_test_{emit_compact_code, run_on_program_start, inject_mismatch_for_testing_only}` (bool), `xla_tpu_ici_sdc_test_sflag_wait_timeout_ms` (int).

### Fusion (101)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_tpu_rwb_fusion` | bool | true (errstr remedy `=false`) | read-write-buffer fusion |
| `xla_tpu_dot_dot_fusion` | bool | true (errstr remedy `=false`) | dot→dot fusion |
| `xla_tpu_nested_dot_fusion` | bool | false (errstr remedy `=true`) | nested-dot (PartialReduce) fusion |
| `xla_tpu_accumulate_into_mrb` | bool | true (errstr remedy `=false`) | MRB accumulation fusion |
| `xla_tpu_allow_deeply_nested_fusion_numerical_diff` | bool | — | tolerate deep-fusion numerics |
| `xla_tpu_allow_input_fusion_in_certain_reduce_ops` | bool | — | reduce-op input fusion |
| `xla_tpu_allow_conv_input_fusion_with_downcast_convert` | bool | — | conv input fusion w/ downcast |
| `xla_tpu_async_collective_fusion_fuse_multiple_collectives` | bool | — | multi-collective async fusion |
| `xla_tpu_enable_async_collective_fusion_fuse_all_gather` | bool | — | AG async collective-fusion fuse |
| `xla_tpu_enable_async_collective_fusion_fuse_all_reduce` | bool | — | AR async collective-fusion fuse |
| `xla_tpu_copy_fusion_minimum_copy_size_in_bytes` | int | — | copy-fusion size floor |
| `xla_tpu_enable_experimental_fusion_cost_model` | bool | — | experimental fusion cost model |
| `xla_tpu_fusion_debugger_instrument_inputs` | bool | — | fusion-debugger input instrumentation |

### MSA / scoped memory (55)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_tpu_alternate_memory_benefit_scaling_factor_for_large_buffers` | float | — | MSA benefit scaling |
| `xla_tpu_async_copy_bandwidth_scaling_factor` | float | — | async-copy BW model |
| `xla_tpu_allocate_scoped_vmem_at_same_offset` | int | — | scoped VMEM offset reuse |
| `xla_tpu_allocate_scoped_cmem_at_same_offset` | int | — | scoped CMEM offset reuse |
| `xla_tpu_allow_in_cmem_copy` | bool | — | permit copies into CMEM |
| `xla_tpu_scoped_cmem_for_all_reduce` | bool | — | AR result in scoped CMEM |
| `xla_tpu_cmem_max_outstanding_prefetches` | int | — | CMEM prefetch cap |
| `xla_tpu_cmem_max_overlap_to_mem_size_async_copy_ratio` | float | — | CMEM overlap ratio |
| `xla_tpu_vmem_use_telamalloc` | bool | — | telamalloc VMEM allocator |
| `xla_tpu_scoped_vmem_limit_kib` | int | — | scoped VMEM byte limit (KiB) |
| `xla_tpu_autotune_memory_space_assignment` | bool | — | MSA autotune |

### SparseCore TC-side (50)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_tpu_enable_offloading_gather_to_sparsecore` | bool | — | gather offload to SC |
| `xla_tpu_enable_offloading_scatter_to_sparsecore` | bool | — | scatter offload to SC |
| `xla_tpu_enable_offloading_copy_to_sparsecore` | bool | — | copy offload to SC |
| `xla_tpu_enable_offloading_reduce_to_sparsecore` | bool | — | reduce offload to SC |
| `xla_tpu_enable_sparse_core_reduce_scatter_v2` | bool | false (errstr remedy `=true`) | SC ND reduce-scatter v2 |
| `xla_tpu_enable_sc_log_recorder` | bool | false (errstr remedy `=true`) | SC log recorder |
| `xla_tpu_enable_async_sc_call` | bool | — | async SC call |
| `xla_tpu_embedding_table_oblongness_threshold` | int | — (errstr remedy `=1`) | embedding-table oblongness cutoff |
| `xla_tpu_aggregate_data_dependent_sc_ops` | bool | — | data-dependent SC aggregation |

### Other high-signal `xla_tpu_` knobs

- **Numerics:** `xla_tpu_accurate_{exp, exp2, expm1, log1p, log2, logistic, sigshift}` (bool), `xla_tpu_bf16_emission_mode` (enum), `xla_tpu_auto_reduce_precision` (bool), `xla_tpu_experimental_enable_dynamic_int8_quantization` (bool).
- **Dot/conv:** `xla_tpu_enable_dot_strength_reduction`, `xla_tpu_enable_ragged_dot_kernel`, `xla_tpu_choose_faster_windowed_einsum_over_mem` (all bool), `xla_tpu_impure_contract_ragged_conv_with` (string).
- **Layout:** `xla_tpu_allow_layout_negotiation` (bool), `xla_tpu_enable_large_2nd_minor_layout`, `xla_tpu_allow_large_2nd_minor_layout_for_{x16, x8, x4}` (int).
- **Auto-sharding:** `xla_tpu_auto_spmd_partitioning_memory_budget_gb` (int), `xla_tpu_auto_spmd_partitioning_memory_budget_ratio` (float), `xla_tpu_auto_spmd_partitioning_solver_timeout_seconds` (int), `xla_tpu_auto_spmd_keep_all_user_shardings` (bool).
- **Cost-model:** `xla_tpu_emitter_learned_cost_model_options` (string/proto), `xla_tpu_enable_instruction_cycle_checking` (bool), `xla_tpu_hbm_initial_cycle_penalty` (int), `xla_tpu_impure_cost_model_logging_options` (string).
- **Debug/memory:** `xla_tpu_enable_tile_log_recorder` (bool; errstr remedy `=true`, so default false), `xla_tpu_impure_oom_fast_exit_threshold` (int; errstr remedy `=-1` for verbose logging), `xla_tpu_always_spill_to_default_memory` (bool).

---

## `xla_jf_` — Jellyfish XLA Backend (148)

The `jf` codename is the TPU XLA backend namespace (shared across generations). Lands in TCE. Subsystem split: misc 63, debug/dump 24, memory/alloc 15, fusion 10, MSA 9, ICI 8, dot/conv 7, sharding 4, scheduler 3, SparseCore 2, cost-model 2, numerics 1.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_jf_debug_level` | int | — (errstr remedy `=2` for stack traces) | JF backend debug verbosity |
| `xla_jf_run_verifier` | bool | — | run the JF HLO verifier |
| `xla_jf_vliw_scheduler` | bool | — | JF VLIW scheduler engine |
| `xla_jf_critical_path_scheduler` | bool | — | critical-path scheduler |
| `xla_jf_conv_{input,output,reshape}_fusion` | bool | — | conv fusion variants |
| `xla_jf_enable_multi_output_fusion` | bool | — | multi-output fusion |
| `xla_jf_fusion_max_vmem_mib` | int | — | fusion VMEM ceiling (MiB) |
| `xla_jf_conv_{full_precision,increased_precision}` | bool | — | conv precision controls |
| `xla_jf_auto_assign_mxu` | bool | — | auto MXU assignment |
| `xla_jf_use_cost_based_memory_coloring` | bool | — | cost-based memory coloring |
| `xla_jf_dump_{hlo_text,debug_info,llo_html}` | bool | — | JF dump variants |
| `xla_jf_dump_isa_program_proto` | string | — | ISA-program-proto dump path |
| `xla_jf_experimental_{cmem,vmem}_for_hlo_outputs` | bool | — | experimental output placement |
| `xla_jf_spmd_threshold_for_windowed_einsum_mib` | float | — | windowed-einsum SPMD threshold |

---

## `xla_` (plain) — Generic XLA / DebugOptions-Backed (121 registered / 138 names)

The non-codename `xla_*` flags. Unlike the TPU families, these bind to `xla::DebugOptions` fields via `MakeDebugOptionsFlags @ 0x1e66ce80`. Subsystem split over all 138 `xla_*` (plain) name strings (sums to 138, not the 121 registered subset): misc 32, ICI 23, scheduler 21, SparseCore 17, memory 15, MSA 14, debug 10, others 6.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `xla_enable_megacore_hbm_spill` | bool | false (errstr remedy `=true`, untested) | enable megacore HBM spill |
| `xla_enable_cross_program_prefetch` | bool | — | cross-program prefetch gate |
| `xla_default_cross_program_prefetch_heuristic` | bool | — | CPP heuristic default |
| `xla_enable_async_{all_gather,all_reduce,collective_permute}` | bool | — | async collective gates |
| `xla_enable_async_reduce_scatter_fusion` | bool | — | async RS fusion |
| `xla_{all_gather,all_reduce,all_to_all}_latency_bound_threshold_in_bytes` | float | — | latency-bound thresholds |
| `xla_all_gather_combiner_threshold_count` | float | — | AG combiner threshold |
| `xla_enable_all_gather_{2d,3d}_emitter` | bool | — | dimensional AG emitters |
| `xla_hlo_scheduling_brkga_{computation_limit,generation_limit}` | int | — | HLO BRKGA scheduler tuning |
| `xla_latency_hiding_scheduler_rerun` | int | — | LHS rerun count |
| `xla_hbm_logging_buffer_size_bytes` | int | — | HBM logging buffer size |
| `xla_enable_post_msa_sync_slice_fusion` | bool | — | post-MSA sync-slice fusion |
| `xla_hlo_parse_memory_schedule_from_file` | string | — | external memory schedule path |

> **GOTCHA —** the classic XLA dump/HLO knobs (`xla_dump_to`, `xla_hlo_profile`, `xla_dump_hlo_as_proto`, `xla_step_marker_location`, `xla_disable_hlo_passes`) appear as `xla.DebugOptions` *fields* and as `.rodata` strings, but they are **not** registered `absl::Flag` globals in this build. A direct cross-match of all 290 DebugOptions field names against the registered-flag set finds exactly **two** overlaps: `xla_tpu_detect_nan` (DebugOptions field 135) and `xla_tpu_detect_inf` (field 136). Every other dump/HLO knob is settable only through the PJRT `CompileOptions.debug_options` proto path, never through `LIBTPU_INIT_ARGS`. A reimplementer who exposes `--xla_dump_to=` as a libtpu command-line flag is wrong about this build.

---

## SparseCore & Embedding — `xla_sc_` (92), `barna_core_` (61)

`xla_sc_*` are the SparseCore-compiler LLVM-backend knobs (lands in TCE); `barna_core_*` are the BarnaCore embedding-engine runtime knobs (standalone).

### `xla_sc_` representative subset

| Flag | Type | Purpose |
|---|---|---|
| `xla_sc_enable_instruction_fusion` | bool | SC instruction fusion |
| `xla_sc_enable_latency_hiding_scheduler` | bool | SC LHS |
| `xla_sc_enable_scheduler_memory_pressure_tracking` | bool | SC mem-pressure tracking |
| `xla_sc_enable_tile_overlays` / `_scs_overlays` | bool | SC tile/SCS overlays |
| `xla_sc_enable_stack_eliding` | bool | SC stack eliding |
| `xla_sc_enable_hbm_optimization_mode` | bool | SC HBM optimization mode |
| `xla_sc_detect_nan` | bool | SC NaN detection |
| `xla_sc_assert_level` | enum | SC assert level |
| `xla_sc_compiler_backtrace_depth` | int | SC backtrace depth |
| `xla_sc_elementwise_shape_scaling_factor` | float | SC elementwise scaling |
| `xla_sc_async_wrapper_fusion_type` | enum | SC async-wrapper fusion type |
| `xla_sc_dump_{llvm_ir_to,mlir_to,bundles_to}` | string | SC IR/MLIR/bundle dump paths |
| `xla_sc_use_legacy_embeddings_loop_configs` | bool | legacy embedding loop configs |

### `barna_core_` representative subset

| Flag | Type | Purpose |
|---|---|---|
| `barna_core_max_hbm_fraction_for_embeddings` | int | HBM fraction cap for embeddings |
| `barna_core_hbm_savings_threshold_for_optimized_hbm_packing` | float | optimized-packing savings threshold |
| `barna_core_fraction_batches_to_process_locally` | bool | local-batch processing fraction |
| `barna_core_master_partitioner_thread_count` | int | partitioner thread count |
| `barna_core_hot_id_profiler_top_n_multiple` | float | hot-id profiler top-N multiple |
| `barna_core_enable_software_deduplication` | bool | software dedup |
| `barna_core_enable_software_row_sharding` | bool | software row sharding |
| `barna_core_file_operation_timeout` | int | file-op timeout |
| `barna_core_embedding_common_config_proto_path` | string | embedding-config proto path |
| `barna_core_partitioner_optimization_objective` | enum | partitioner objective |

---

## MSA Namespaces — `xla_msa_` (22), `xla_vf_` (16), `xla_gf_` (14), `xla_ior_` (4), `xla_pf_` (1), `xla_llo_` (1)

The dedicated memory-space-assignment namespaces. `xla_msa_*` is the generic MSA option set; `xla_vf_*` and `xla_gf_*` are gen-specific VMEM/MSA override sets (the `vf` / `gf` codename prefixes) carrying the *same* knob names scoped to that generation; `xla_ior_*` is the IOR fast-mem round-trip variant; `xla_pf_*` is a single ND-allreduce override; `xla_llo_*` is a single LLO-lifecycle flag.

### `xla_msa_` — full enumeration (22)

| Flag | Type | Purpose |
|---|---|---|
| `xla_msa_enable` | bool | MSA master gate |
| `xla_msa_max_cross_program_prefetches` | int | CPP prefetch cap |
| `xla_msa_max_outstanding_evictions` | int | eviction cap |
| `xla_msa_max_outstanding_prefetches` | int | prefetch cap |
| `xla_msa_max_repacks` | int | repack cap |
| `xla_msa_max_retries` | int | retry cap |
| `xla_msa_{min,preferred}_overlap_to_async_copy_ratio` | float | overlap-to-async-copy ratios |
| `xla_msa_max_overlap_to_mem_size_async_copy_ratio` | float | overlap-to-mem-size ratio |
| `xla_msa_enable_cross_program_prefetch_freeing` | bool | CPP freeing |
| `xla_msa_enable_sync_copy_replacement` | bool | sync-copy replacement |
| `xla_msa_enable_sync_slice_replacement` | bool | sync-slice replacement |
| `xla_msa_enable_while_redundant_eviction_elimination` | bool | redundant-eviction elimination |
| `xla_msa_enable_window_prefetch` | bool | window prefetch |
| `xla_msa_cross_program_prefetch_permissive_mode` | bool | permissive CPP mode |
| `xla_msa_default_cross_program_prefetch_heuristic` | bool | CPP heuristic default |
| `xla_msa_expanded_scoped_alternate_memory_mode` | enum | expanded scoped-AM mode |
| `xla_msa_use_bundle_aware_cost_model` | bool | bundle-aware cost model |
| `xla_msa_cost_model_options` | string | cost-model config |
| `xla_msa_experimental_ior_algorithm` | enum | experimental IOR algorithm |
| `xla_msa_experimental_use_telamalloc` | bool | experimental telamalloc |
| `xla_msa_allocate_scoped_memory_at_same_offset` | bool | scoped-mem offset reuse |

### `xla_vf_` (16), `xla_gf_` (14), `xla_ior_` (4), `xla_pf_` (1)

`xla_gf_vmem_{max_outstanding_evictions, max_repacks, max_retries}` (int), `xla_gf_vmem_use_ior_algorithm` (enum), `xla_gf_vmem_enable_while_redundant_eviction_elimination` (bool) — the gen-specific VMEM mirror of the `xla_msa_*` set; `xla_vf_*` carries the same `vmem_*` knob set (16 names, including `xla_vf_allow_replicated_vmem_writes` and `xla_vf_allow_split_vmem`). `xla_ior_{fast_mem_round_trip_production_msa, fast_mem_run_production_msa, stored_solution_path, use_stored_solution}` (4) carry the IOR fast-mem round-trip variant. `xla_pf_enable_nd_allreduce` (1, bool) is the lone `xla_pf_*` flag; `xla_llo_annotation_lifecycle_strict_mode` (1, enum) is the lone `xla_llo_*` flag.

---

## Runtime / Driver — `tpu_` (69), `megascale_` (150), `tf_` (20), `xla_mosaic_` (8)

### `tpu_` — runtime / compilation-cache / driver (69)

Standalone runtime flags (not compiler knobs). Representative subset:

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `tpu_use_tfrt` | bool | — (errstr deprecates `=false`) | use TFRT runtime path |
| `tpu_compilation_cache_disable_coordination_service` | bool | — | disable cache coordination |
| `tpu_persistent_compilation_cache_location` | string | — | persistent cache path |
| `tpu_persistent_compilation_cache_ttl_secs` | int | — | cache TTL |
| `tpu_local_compilation_cache_size_bytes` | int | — | local cache size |
| `tpu_program_cache_eviction_policy` | enum | — | cache eviction policy |
| `tpu_program_proto_compression` | bool | — | proto compression |
| `tpu_link_up_check_timeout` | int | — | link-up check timeout |
| `tpu_driver_callback_watchdog_timeout` | int | — | driver-callback watchdog |
| `tpu_core_dump_directory` | string | — | core-dump directory |
| `tpu_hbm_report_enable` | bool | — | HBM report toggle |
| `tpu_log_allocations_on_oom` | bool | — | log allocations on OOM |
| `tpu_hlo_breakpoint_debugger_server_port` | int | — | HLO breakpoint debugger port |
| `DANGEROUS_tpu_runtime_abi_verification_disabled` | bool | — | disable ABI verification (dangerous) |

> **NOTE —** the lowercase `libtpu_*` identifiers (`libtpu_init_utils`, `libtpu_lockfile`, `libtpu_sdk_*`, `libtpu_telemetry_*`, `libtpu_version`, the `libtpu_lts_20260413_b_` build tag) are **not flags** — they are translation-unit / module name strings in `.rodata`. They are excluded from the 2107 catalog.

### `megascale_` (150) — DCN collective runtime

Top knobs: `megascale_num_slices` (int), `megascale_slice_id` (int), `megascale_coordinator_address` (string), `megascale_transport_type` (enum), `megascale_enable_tpu_premapping` (bool), `megascale_enable_watchdog` (bool), `megascale_graph_hang_threshold` (int), `megascale_heartbeat_{interval,timeout}_ms` (int), `megascale_error_reporter_abort_on_{error,hang}` (bool), `megascale_use_heartbeat` (bool), `megascale_grpc_num_channels` (int), `megascale_use_mtls_for_grpc` (bool), `megascale_verify_checksums` (bool), `megascale_use_numa_aware_threadpool` (bool; errstr remedy `=false`, so default true).

### `tf_` (20) and `xla_mosaic_` (8)

`tf_*` are the TensorFlow-TPU bridge flags (`tf_jf_*` and similar). `xla_mosaic_*` are the Mosaic MLIR custom-kernel dialect flags, including the legacy `xla_mosaic_deprecated_allow_implicit_single_buffering`.

---

## Defaults — the Certainty Boundary

proto3 carries no descriptor-level defaults, and the per-flag defaults live in `xla::DefaultDebugOptions()` and the `FLAGS_<name>` static initializers — both in `.text`, **not recoverable from strings**. What *does* survive is a set of help/error strings that spell a `--flag=value` clause. Critically, every such surviving clause is a *remedy* — the value the message tells the user to set when something goes wrong ("use `--flag=false` in the meantime", "set `=true` to enable") — so the spelled value is the **non-default**, and the implied actual default is its opposite. **13** flags carry such a `=value` remedy clause; the remaining flags' defaults leave no string at all.

| Flag | Type | Remedy `=value` (errstr) | Implied default |
|---|---|---|---|
| `xla_tpu_accumulate_into_mrb` | bool | `=false` ("in the meantime") | true |
| `xla_tpu_rwb_fusion` | bool | `=false` (reverted-on-fallback) | true |
| `xla_tpu_dot_dot_fusion` | bool | `=false` (if failure persists) | true |
| `xla_tpu_nested_dot_fusion` | bool | `=true` ("did you forget to set") | false |
| `xla_tpu_scheduling_annotation_deannotate_unsupported_groups` | bool | `=true` (to deannotate gaps) | false |
| `xla_tpu_enable_tile_log_recorder` | bool | `=true` (to enable logging) | false |
| `xla_tpu_enable_sc_log_recorder` | bool | `=true` (to enable logging) | false |
| `xla_tpu_enable_sparse_core_reduce_scatter_v2` | bool | `=true` (SC ND RS needs) | false |
| `xla_tpu_impure_oom_fast_exit_threshold` | int | `=-1` (more detailed logging) | not string-recoverable |
| `xla_tpu_embedding_table_oblongness_threshold` | int | `=1` (avoid tiled layout) | not string-recoverable |
| `xla_enable_megacore_hbm_spill` | bool | `=true` (to activate, untested) | false |
| `xla_jf_debug_level` | int | `=2` (enable stack traces) | not string-recoverable |
| `megascale_use_numa_aware_threadpool` | bool | `=false` (to disable) | true |

> **GOTCHA —** none of these are byte-confirmed *defaults*. Each `=value` is the value the message tells the user to set (the remedy), which is the **non-default**; the "Implied default" column is the inferred opposite for the booleans, and is genuinely unrecoverable for the int-valued knobs (where the remedy is a specific tuning value, not a sentinel-vs-default flip). Do not read the remedy value as the default — it is the opposite. The authoritative defaults for every flag require disassembling `DefaultDebugOptions()` and the `FLAGS_*` ctors in `.text`. Four further flags sometimes cited with byte-defaults (`xla_tpu_allow_deeply_nested_fusion_numerical_diff`, `xla_tpu_enable_offloading_{gather,scatter}_to_sparsecore`, `xla_tpu_fusion_debugger_instrument_inputs`) carry **no** `=value` string at all and have no string-derivable default.

For the type column: the 13 types above are byte-corroborated from `=value` evidence; the rest are **convention-inferred** from the flag-name suffix (`enable_/use_/allow_` ⇒ bool; `_ms/_kib/_count/_size/_n` ⇒ int; `_ratio/_factor/_fraction` ⇒ float; `_file/_path/_dir/_proto` ⇒ string; `_mode/_type/_level` ⇒ enum). This is XLA's own registration convention, so it is reliable but not per-flag byte-confirmed. A `_threshold` suffix may be int or float; `_mode`/`_level` may be int-enum or string — those are `HIGH`, not `CERTAIN`.

---

## Cross-References

- [xla_* Flag Atlas](../config/xla-flag-atlas.md) — the curated sibling: grouped narrative + per-subsystem deep-dive into the ~100 highest-signal knobs (point here for *what a flag does*)
- [Flag Families](../config/flag-families.md) — prefix → owner routing: which proto (DebugOptions vs TCE vs standalone) each prefix lands in, live-vs-inert verdict per family
- [DebugOptions Proto](../config/debugoptions-proto.md) — the `xla.DebugOptions` message (290 fields, 17 nested enums), the 2-field flag-wiring overlap (`xla_tpu_detect_nan/inf`)
- [Default DebugOptions](../config/default-debugoptions.md) — where the per-flag defaults live (`DefaultDebugOptions()` + `FLAGS_*` static initializers in `.text`)
- [Registry-Mediated Flags](../config/registry-mediated-flags.md) — the `AbslFlagHelpGenFor` registration mechanism and the `MakeDebugOptionsFlags` / `OverrideTpuCompEnvByCmdLineFlags` bind sites
- [Flag Prefix Dispatch](../config/flag-prefix-dispatch.md) — the `TpuVersion`-aware prefix-strip/select mechanism for the codename families
- [Environment Variables](../config/env-vars.md) — `LIBTPU_INIT_ARGS`, `LIBTPU_ON_GCE`, `TPU_LOAD_LIBRARY`, the parse funnel into `absl::ParseCommandLine`
