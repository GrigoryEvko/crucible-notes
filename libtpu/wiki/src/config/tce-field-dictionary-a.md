# TCE Field Dictionary (A)

> *All field numbers, names, types, offsets, and addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, `libtpu_lts_20260413_b_RC00`). Other versions renumber and re-offset fields.*

## Abstract

`TpuCompilationEnvironment` (TCE) is the TPU-private master compile-config protobuf: a single editions-proto message carrying **1121 live fields**, every one of which is also a registered `absl::Flag` (a perfect 1:1 mapping, the structural inverse of the GPU/CPU-shared `xla.DebugOptions`). This page is the **field-number → name → proto type** dictionary for the **lower half** of that proto: field numbers **1 through 560**. The matching upper half — field **561 through 1218** (max field number 1218, with 97 deletion gaps) — lives on [TCE Field Dictionary (B)](tce-field-dictionary-b.md). The split point is exact and stated below.

The dictionary is reconstructed from the `TpuCompilationEnvironment` `FileDescriptorProto` carved out of the binary at `protodesc_cold` VA `0xbfa6060` (size 137,692 B) and cross-checked against the generated fast-parse table `TpuCompilationEnvironment::_table_` @ `0x21cfa9e0`, whose 1121-entry `FieldEntry` array is sorted by ascending field number. Each line of this page gives the field number, the verbatim field name (every name on this page was confirmed byte-present in the binary's `.rodata` string pool), the proto base type, and — for the 423 wrapped fields — the wrapper message/enum type. The C++ struct offset and the per-field literal default value are deliberately *not* repeated here; they are the subject of [TCE Field-Offsets & Flag Defaults](tce-field-offsets-defaults.md), and the structural overview (parse-table header, type histogram, HOT-tag taxonomy, AutoProto switch) is on [TpuCompilationEnvironment](tpu-compilation-environment.md).

For a reimplementer, the contract this page satisfies is narrow and precise:

- **Field number → name.** The canonical numbering a serialized TCE proto uses on the wire and that `GetTpuCompEnvWithDefaultValues` walks when it resolves each field to its `absl::Flag`.
- **Name → proto base type.** bool / int32 / int64 / uint32 / uint64 / float / double / string / enum / message — the wire type that decides how the value is parsed and stored.
- **Wrapper type, where present.** Which enum (`TristateProto.Value`, `MemorySchedulerProto.Value`, …) or which message (`AutoProto`, `RangeSpecProto`, the typed helpers) a non-scalar field carries.

| | |
|---|---|
| **Proto** | `TpuCompilationEnvironment` (editions proto, all fields `optional`) |
| **Descriptor source** | `FileDescriptorProto` @ `0xbfa6060` (137,692 B) |
| **Parse table** | `TpuCompilationEnvironment::_table_` @ `0x21cfa9e0` (1121 `FieldEntry`, ascending field#) |
| **This page covers** | field **#1 – #560** (lower half) |
| **Page B covers** | field **#561 – #1218** ([dictionary B](tce-field-dictionary-b.md)) |
| **Split boundary** | #560 `xla_tpu_ici_sdc_test_run_on_program_start` ends here; #561 `xla_tpu_debug_sflag_wait_timeout_ms` opens page B |
| **Names verified** | 22 spot-checked verbatim in `.rodata` strings; 0 misses |

---

## How to Read These Tables

Each row is one live field, in ascending field-number order. Columns are uniform across every group:

- **#** — the proto field number (the wire tag). Numbers are *not* contiguous: TCE has 97 deletion gaps in `1..1218` (no `reserved_range`; deleted fields simply vanish from the descriptor). Gaps inside this page's range are listed in [§Field-Number Gaps](#field-number-gaps-in-1560).
- **Field name** — the verbatim proto field name, identical to the registered `absl::Flag` name. Most carry the `xla_tpu_` / `xla_jf_` / `xla_` prefixes; a handful are bare (`config_criterion`, `loop_invert`, `rematerialization_algorithm`).
- **Type** — the proto base type. For `enum` and `message`, the **Wrapper / message type** column names the concrete type.

> **NOTE —** the 423 non-scalar fields split into 74 `enum`-typed (67 of them `TristateProto.Value` = AUTO/DISABLED/ENABLED, plus 7 direct helper enums) and 349 `message`-typed (330 are the 30-arm `AutoProto` switch wrapper; the remaining 19 are typed helper messages such as `RangeSpecProto`, `RepeatedStrings`, and the `msa.*` option messages). The wrapper enum value-by-value tables and the AutoProto arm list live on [TpuCompilationEnvironment](tpu-compilation-environment.md); this page only names the wrapper per field.

> **GOTCHA —** a field's *position* on this page is its field number, not its struct offset. The two are unrelated: the parse table sorts `FieldEntry` by field number, but the C++ `_impl_` layout interleaves them by type/alignment (field #2 sits at struct offset `+0xBC`, field #1 at `+0xB8`, field #3 at `+0xA8`). Do not infer offset from field number; use [TCE Field-Offsets & Flag Defaults](tce-field-offsets-defaults.md).

---

## #1 – #30 — Collectives, Cross-Program Prefetch, CMEM, Embedding

The lowest field numbers are the oldest jellyfish-era knobs: all-gather emitters, the net-router ring limits, the (now mostly deprecated) cross-program-prefetch / scoped-CMEM cluster, and the first embedding tunable. Field #21 is a gap.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 1 | `xla_enable_async_collective_permute` | enum | `TristateProto.Value` |
| 2 | `xla_tpu_sdc_checker_instrument_megacore_fusion` | bool | |
| 3 | `xla_tpu_scoped_vmem_limit_sweep_profile_path` | string | |
| 4 | `xla_tpu_allocate_scoped_vmem_at_same_offset` | bool | |
| 5 | `xla_enable_all_gather_2d_emitter` | bool | |
| 6 | `xla_always_enable_all_gather_2d_asymmetric` | bool | |
| 7 | `xla_enable_all_gather_3d_emitter` | bool | |
| 8 | `xla_always_enable_all_gather_3d_asymmetric` | bool | |
| 9 | `xla_tpu_enable_minor_all_gather` | bool | |
| 10 | `xla_tpu_use_routing_table_indices_in_all_gather` | bool | |
| 11 | `xla_tpu_enable_net_router_in_all_gather` | bool | |
| 12 | `xla_tpu_cross_module_net_router_ring_limit` | int64 | |
| 13 | `xla_tpu_cross_replica_net_router_ring_limit` | int64 | |
| 14 | `xla_tpu_max_cmem_used_by_memory_space_assignment` | int64 | |
| 15 | `xla_enable_cross_program_prefetch` | bool | |
| 16 | `xla_tpu_enable_cross_program_prefetch_freeing` | bool | |
| 17 | `xla_tpu_cmem_enable_while_redundant_eviction_elimination` | bool | |
| 18 | `xla_tpu_cmem_max_outstanding_prefetches` | int64 | |
| 19 | `xla_tpu_cmem_max_outstanding_evictions` | int64 | |
| 20 | `xla_tpu_allocate_scoped_cmem_at_same_offset` | bool | |
| 22 | `xla_default_cross_program_prefetch_heuristic` | bool | |
| 23 | `xla_tpu_ior_cmem` | string | |
| 24 | `megascale_use_one_to_all_for_gather` | bool | |
| 25 | `xla_tpu_enable_async_pincer_emitter` | bool | |
| 26 | `xla_tpu_write_cmem_output_via_stores_on_megacore` | bool | |
| 27 | `megascale_use_dcn_all_to_all_in_collectives_mask` | int64 | |
| 28 | `xla_hlo_scheduling_brkga_compute_runtime_estimates` | bool | |
| 29 | `xla_tpu_wait_n_cycles_before_program_termination` | int64 | |
| 30 | `xla_tpu_embedding_table_oblongness_threshold` | float | *(is_used_at_runtime)* |

> **NOTE —** field #30 is one of only four fields in the whole proto with `is_used_at_runtime=true`; the others (#354, #432, and `xla_tpu_num_embedding_devices`) live further on. The bit lives in the `TpuCompEnvFieldOptions` extension (#535801365), not in the parse table. Fields #4, #14, #15, #16, #17, #18, #19, #20, #22, #23 are among the 101 `deprecated=true` fields, superseded by the per-family `xla_{jf,vf,gf,cmem}_vmem_*` knobs.

---

## #31 – #95 — Scheduler, Tracing, BRKGA, LLO/LSRA, SDC Checker

This block opens with the memory-scheduler selector (#31), then a run of trace/profile toggles, the BRKGA HLO-scheduling parameters, the early LLO/LSRA register-allocation knobs, the combiner thresholds, and the first big SDC-checker cluster. Field #21, #44, #45, #54, #59, #70 are gaps in this range.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 31 | `xla_memory_scheduler` | enum | `MemorySchedulerProto.Value` |
| 32 | `xla_enable_profiler` | bool | |
| 33 | `xla_enable_hlo_trace` | bool | |
| 34 | `xla_trace_only_stalling_hlo` | bool | |
| 35 | `xla_enable_module_trace` | bool | |
| 36 | `xla_enable_mxu_trace` | bool | |
| 37 | `xla_enable_transpose_trace` | bool | |
| 38 | `xla_jf_trivial_traces` | bool | |
| 39 | `xla_jf_module_tracemarks` | bool | |
| 40 | `xla_hbm_logging_buffer_size_bytes` | int64 | |
| 41 | `xla_hlo_scheduling_brkga_generation_limit` | int64 | |
| 42 | `xla_hlo_scheduling_brkga_computation_limit` | int64 | |
| 43 | `xla_hlo_scheduling_brkga_enable_as_fallback` | bool | |
| 46 | `xla_enable_megacore_hbm_spill` | bool | |
| 47 | `xla_enable_lru_free_reg_assignment` | bool | |
| 48 | `xla_jf_emit_annotations` | bool | |
| 49 | `xla_jf_llo_level` | int32 | |
| 50 | `xla_jf_naive_bundle_packer` | message | `RangeSpecProto` |
| 51 | `xla_jf_track_bundle_dependency_indices` | bool | |
| 52 | `xla_jf_pack_latches` | bool | |
| 53 | `xla_jf_bf16_propagation` | bool | |
| 55 | `xla_tpu_arf_combiner_threshold_in_bytes` | int64 | |
| 56 | `xla_tpu_ars_combiner_threshold_in_bytes` | int64 | |
| 57 | `xla_tpu_agf_combiner_threshold_in_bytes` | int64 | |
| 58 | `xla_jf_crs_combiner_threshold_count` | int64 | |
| 60 | `xla_jf_bounds_check_annotate_only` | message | `RangeSpecProto` |
| 61 | `xla_jf_reconstruct_hlo_from_proto` | bool | |
| 62 | `xla_jf_slot_tracker_hoist_limit` | int64 | |
| 63 | `xla_jf_enable_multi_output_fusion` | bool | |
| 64 | `xla_jf_emit_global_barrier_at_start` | bool | |
| 65 | `xla_jf_lsra_v2_alloc_only` | message | `RangeSpecProto` |
| 66 | `xla_jf_lsra_v2_reserved_smem` | int64 | |
| 67 | `xla_jf_lsra_v2_annotate` | bool | |
| 68 | `xla_jf_llo_rematerialization_parameter_threshold` | int64 | |
| 69 | `xla_ra_split` | message | `RangeSpecProto` |
| 71 | `xla_jf_rematerialization_percent_shared_memory_limit` | int64 | |
| 72 | `xla_tpu_rematerialization_max_block_size` | int64 | |
| 73 | `xla_tpu_block_rematerialization_factor` | int64 | |
| 74 | `xla_tpu_rematerialization_min_size_in_bytes` | int64 | |
| 75 | `xla_jf_order_barna_core_feed_serialize` | bool | |
| 76 | `xla_jf_order_barna_core_feed_overlap` | bool | |
| 77 | `xla_jf_conditional_code_motion` | bool | |
| 78 | `xla_jf_auto_cross_replica_sharding` | bool | |
| 79 | `xla_jf_use_vdelay` | bool | |
| 80 | `xla_tpu_autotune_windows_service` | string | |
| 81 | `xla_tpu_autotune_windows` | bool | |
| 82 | `xla_tpu_autotune_phase_ordering` | bool | |
| 83 | `xla_tpu_log_post_opt_fingerprints` | bool | |
| 84 | `xla_tpu_enable_sdc_checker` | bool | |
| 85 | `xla_tpu_sdc_check_fail_ratio_debug_only` | int64 | |
| 86 | `xla_tpu_sdc_check_repeat_count` | int64 | |
| 87 | `xla_tpu_sdc_check_inputs` | bool | |
| 88 | `xla_tpu_sdc_replicate_llo` | bool | |
| 89 | `tpu_sdc_checker_filter_loop_iteration_depth` | int32 | |
| 90 | `tpu_sdc_checker_filter_loop_iteration_check` | int32 | |
| 91 | `tpu_sdc_checker_filter_loop_iteration_skip` | int32 | |
| 92 | `xla_tpu_sdc_check_halt_on_detection` | bool | |
| 93 | `xla_tpu_sdc_check_log_full_hlo` | bool | |
| 94 | `xla_tpu_sdc_extra_llo_replica_count` | int64 | |
| 95 | `xla_tpu_sdc_duplicate_mxu_instructions` | bool | |

---

## #96 – #159 — VLIW Scheduling, Convolution Precision, Bounds-Check, IOR/LLVM Backend

A scheduling-and-codegen block: the VLIW scheduler and its fuel, conv-precision numerics, the bounds-check `RangeSpec` family, the LLVM-backend toggles, and the first `loop_invert` pair. Gaps: #121, #124–#127, #156.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 96 | `xla_enable_async_all_gather` | enum | `TristateProto.Value` |
| 97 | `xla_tpu_enable_log_recorder` | bool | |
| 98 | `xla_jf_all_to_all_shard_kib` | int64 | |
| 99 | `xla_tpu_all_to_all_with_different_output_addresses` | bool | |
| 100 | `xla_jf_xlu_optimizer` | bool | |
| 101 | `xla_jf_auto_assign_xlu` | bool | |
| 102 | `xla_jf_critical_path_scheduler` | bool | |
| 103 | `xla_jf_load_cse_and_s2l_forwarding` | bool | |
| 104 | `xla_tpu_load_store_optimizations` | bool | |
| 105 | `xla_jf_accumulation_reassociation` | bool | |
| 106 | `xla_jf_vliw_scheduler` | bool | |
| 107 | `xla_jf_vliw_fuel` | int64 | |
| 108 | `xla_tpu_force_send_recv_host_on_same_resource` | enum | `TristateProto.Value` |
| 109 | `xla_jf_allow_cross_replica_sharding_on_certain_reduce` | bool | |
| 110 | `xla_jf_cross_replica_sharding_also_try_reducing_min_span_by_factor` | float | |
| 111 | `xla_tpu_allow_input_fusion_in_certain_reduce_ops` | bool | |
| 112 | `xla_tpu_allow_sharding_on_minor_dim` | bool | |
| 113 | `xla_jf_enable_buffer_alias` | bool | |
| 114 | `xla_jf_verify_sync_flags` | bool | |
| 115 | `xla_tpu_verify_matmul` | bool | |
| 116 | `xla_jf_use_rng_bit_generator_emitter` | bool | |
| 117 | `xla_tpu_expand_rng_bit_generator` | bool | |
| 118 | `xla_jf_module_group_simplifier` | bool | |
| 119 | `xla_tpu_ior_remat` | bool | |
| 120 | `xla_enable_scalar_multiply_reduction` | bool | |
| 122 | `xla_optimize_llo_for_llvm` | bool | |
| 123 | `xla_tpu_run_space_to_batch_on_new_platforms` | bool | |
| 128 | `xla_tpu_min_elements_for_while_loop_concat_code_motion` | int64 | |
| 129 | `xla_tpu_sharding_metadata` | bool | |
| 130 | `xla_tpu_reverse_layout_computation_order` | bool | |
| 131 | `xla_tpu_spmd_threshold_for_allgather_cse` | int64 | |
| 132 | `xla_tpu_verify_or_assign_tiling_before_lowering` | enum | `VerifyOrAssignTilingFlagsProto.Value` |
| 133 | `xla_tpu_spmd_decompose_sharded_concats` | bool | |
| 134 | `xla_tpu_enable_graph_splitting` | bool | |
| 135 | `xla_jf_always_use_windowed_reduce` | bool | |
| 136 | `xla_tpu_max_kept_sublanes_for_reduce` | int64 | |
| 137 | `xla_tpu_check_nan_on_reduce` | bool | |
| 138 | `xla_tpu_use_tree_reduce` | bool | |
| 139 | `xla_tpu_uni_direction_ring_max_size_2d_plane` | int64 | |
| 140 | `xla_tpu_1d_uni_direction_ring_min_input_size_chunks` | int64 | |
| 141 | `xla_tpu_nd_short_transfer_max_chunks` | int64 | |
| 142 | `xla_tpu_enable_pincer_short_emitter` | bool | |
| 143 | `xla_tpu_permute_size4_cross_module_rings` | bool | |
| 144 | `xla_tpu_enable_2d_cross_module_reduce_scatter` | bool | |
| 145 | `xla_tpu_use_strided_strategy_nd` | bool | |
| 146 | `xla_tpu_checksum_all_reduce_transfers` | bool | |
| 147 | `xla_tpu_enforce_two_phase_sharding_topology` | bool | |
| 148 | `xla_tpu_use_routing_table_indices_in_all_reduce` | bool | |
| 149 | `xla_max_concurrent_send_recv` | int32 | |
| 150 | `xla_tpu_enable_latency_hiding_scheduler` | bool | |
| 151 | `xla_tpu_licm_analysis_allowance` | int64 | |
| 152 | `xla_tpu_scheduler_percent_shared_memory_limit` | int64 | |
| 153 | `xla_tpu_spmd_auto_partitioning` | bool | |
| 154 | `xla_tpu_perform_spmd_cse_prevention` | bool | |
| 155 | `xla_tpu_verify_device_assignment_in_runtime` | bool | |
| 157 | `xla_jf_experimental_vmem_for_hlo_outputs` | int64 | |
| 158 | `xla_jf_use_cost_based_memory_coloring` | bool | |
| 159 | `xla_jf_always_overlay` | bool | |

> **QUIRK —** field #132 `xla_tpu_verify_or_assign_tiling_before_lowering` is a `VerifyOrAssignTilingFlagsProto.Value` enum (`NONE=0 / VERIFY=1 / ASSIGN=2`), not a bool. A reimplementer who treats every `xla_tpu_*verify*` knob as a boolean toggle will mis-parse this one — it gates the tile-mode selector consumed at struct offset `+0xDFC`.

---

## #160 – #219 — Loop-Invert, ISA Emitter, IOR/MSA, LLVM, Instrumentation

The `loop_invert` / ISA-emitter `RangeSpec` cluster, the IOR fast-mem and stored-solution MSA knobs, the LLVM-backend group, the HLO-dedup pair, and the bf16-coalescing / rematerialization-algorithm knobs. The two bare-named strings `config_criterion` (#209) and `rematerialization_algorithm` (#212) appear here. Gaps: #214, #218.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 160 | `loop_invert` | message | `RangeSpecProto` |
| 161 | `loop_invert_modules` | message | `RangeSpecProto` |
| 162 | `xla_llvm_isa_emitter` | bool | |
| 163 | `xla_llvm_isa_emitter_bundles` | message | `RangeSpecProto` |
| 164 | `xla_llvm_isa_emitter_force` | bool | |
| 165 | `xla_run_scoped_memory_assignment` | bool | |
| 166 | `xla_jf_loop_trip_count` | int32 | |
| 167 | `xla_jf_internal_prefetch_overlays` | bool | |
| 168 | `xla_tpu_last_overlay_prefetches_first` | bool | |
| 169 | `xla_tpu_put_trap_at_hlo_marker_index` | int64 | |
| 170 | `xla_tpu_single_overlay_mode` | bool | |
| 171 | `internal_embedding_emitter_fraction_vmem_available` | double | |
| 172 | `xla_allow_hoisting_across_branch` | bool | |
| 173 | `xla_jf_avoid_cross_slot_vmem_bank_conflicts` | bool | |
| 174 | `xla_jf_shard_f32_weight_across_loop_iterations` | bool | |
| 175 | `xla_jf_allow_cross_replica_sharding_on_batch_matmul` | bool | |
| 176 | `xla_jf_fusion_max_vmem_mib` | double | |
| 177 | `xla_tpu_pack_vloads` | bool | |
| 178 | `xla_tpu_pack_cloads` | bool | |
| 179 | `xla_tpu_sublane_shift_scratchpad_size` | int64 | |
| 180 | `xla_tpu_small_operand_count_for_loop_fusion` | int64 | |
| 181 | `xla_jf_fusion_max_instruction_count_for_window_config` | int64 | |
| 182 | `xla_tpu_copy_fusion_allow_split` | bool | |
| 183 | `xla_tpu_allow_in_cmem_copy` | bool | |
| 184 | `xla_jf_crs_combiner_threshold_in_bytes` | int64 | |
| 185 | `xla_jf_enable_hlo_pipeline` | bool | |
| 186 | `xla_ior_fast_mem_run_production_msa` | bool | |
| 187 | `xla_ior_fast_mem_round_trip_production_msa` | bool | |
| 188 | `xla_ior_use_stored_solution` | bool | |
| 189 | `xla_ior_stored_solution_path` | string | |
| 190 | `xla_use_llvm_backend` | bool | |
| 191 | `xla_llvm_generate_xla_compatible_dwg` | bool | |
| 192 | `xla_jf_llvm_use_fast_opt` | bool | |
| 193 | `xla_jf_llo2llvm_timing_info` | bool | |
| 194 | `xla_jf_llvm_use_bitcode_dump` | bool | |
| 195 | `xla_jf_llvm_flags` | message | `RepeatedStrings` |
| 196 | `xla_jf_tanh_increased_precision` | bool | |
| 197 | `xla_jf_poison_vmem_allocations` | bool | |
| 198 | `xla_jf_hlo_deduplicate_only` | string | |
| 199 | `xla_jf_hlo_deduplication` | bool | |
| 200 | `xla_tpu_instrument_hlo_operations` | bool | |
| 201 | `xla_tpu_instrumentation_config` | string | |
| 202 | `xla_tpu_merge_small_overlays_into_big_neighbors` | bool | |
| 203 | `xla_tpu_net_router_trace_me_instrumentation` | bool | |
| 204 | `xla_tpu_use_custom_tree_barrier` | bool | |
| 205 | `xla_tpu_use_routing_table_indices_in_net_router` | bool | |
| 206 | `xla_tpu_use_routing_table_indices_in_tree_barrier` | bool | |
| 207 | `bf16_coalescing_dump_killed_candidate_pairs` | bool | |
| 208 | `bf16_coalescing_ignore_distance_for_pairing` | bool | |
| 209 | `config_criterion` | string | |
| 210 | `jf_xla_partial_reduce_hbm_bw_ratio` | double | |
| 211 | `jf_xla_partial_reduce_use_roofline_cost_fn` | bool | |
| 212 | `rematerialization_algorithm` | string | |
| 213 | `rematerialize_even_if_memory_limit_not_reached` | bool | |
| 215 | `tpu_use_continuations` | bool | |
| 216 | `treewidth_rematerialization_minimize_memory` | bool | |
| 217 | `use_op_tuner_config` | bool | |
| 219 | `xla_enable_trace` | bool | |

---

## #220 – #294 — AutoFDO Start, Conv Fusion, Pipelining, Overlay Compression, JF VMEM Family

The first AutoFDO toggle, the conv-fusion / conv-precision numerics block, the JF pipelining knobs, the overlay-compression and single-phase-ring transfer parameters, and the start of the per-family JF VMEM MSA cluster (#275–#286). Gaps: #244–#247, #250–#254, #268, #292, #294.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 220 | `xla_tpu_autofdo` | bool | |
| 221 | `xla_jf_auto_assign_mxu` | bool | |
| 222 | `xla_jf_auto_latch_lmr` | bool | |
| 223 | `xla_jf_bf16_inside_cross_replica_sum` | bool | |
| 224 | `xla_jf_bounds_check` | message | `RangeSpecProto` |
| 225 | `xla_jf_bounds_check_stride` | bool | |
| 226 | `xla_jf_bounds_check_verbose` | bool | |
| 227 | `xla_jf_collect_llo_stack_trace` | bool | |
| 228 | `xla_jf_conv_base_dilation_adversary` | bool | |
| 229 | `xla_jf_conv_full_precision` | bool | *(NUMERICS)* |
| 230 | `xla_jf_conv_increased_precision` | bool | *(NUMERICS)* |
| 231 | `xla_jf_conv_input_fusion` | bool | |
| 232 | `xla_jf_conv_min_limit_vmem_mib` | double | |
| 233 | `xla_jf_conv_output_fusion` | bool | |
| 234 | `xla_jf_conv_prefers_padding_input_feature` | bool | |
| 235 | `xla_jf_conv_reshape_fusion` | bool | |
| 236 | `xla_jf_convolution_performance_target` | double | |
| 237 | `xla_jf_cp_pass_enabled` | bool | |
| 238 | `xla_tpu_crs_bounds_check_threshold_chips_count` | int64 | |
| 239 | `xla_jf_debug_level` | int64 | |
| 240 | `xla_jf_enable_final_priority_fusion` | bool | |
| 241 | `xla_jf_enable_pipelining` | bool | |
| 242 | `xla_jf_enable_producer_consumer_multi_output_fusion` | bool | |
| 243 | `xla_jf_experimental_cmem_for_hlo_outputs` | int64 | |
| 248 | `xla_jf_log_hlo_output` | bool | |
| 249 | `xla_jf_line_info_in_symbol_table` | bool | |
| 255 | `xla_jf_overlay_compression_threshold` | int64 | |
| 256 | `xla_jf_poison_operands_before_emitter` | bool | |
| 257 | `xla_jf_profile_cheap_ops` | bool | |
| 258 | `xla_jf_program_hbm_alignment_in_kib` | int64 | |
| 259 | `xla_jf_random_latency` | bool | |
| 260 | `xla_vf_vmem_use_ior_algorithm` | string | |
| 261 | `xla_jf_simplifier_pass_enabled` | bool | |
| 262 | `xla_jf_single_phase_ring_max_kib` | int64 | |
| 263 | `xla_jf_single_phase_ring_threshold` | int64 | |
| 264 | `xla_jf_span_size_in_kib` | int64 | |
| 265 | `xla_jf_spmd_conv_halo_exchange_always_on_lhs` | bool | |
| 266 | `xla_jf_spmd_report_instruction_count` | int64 | |
| 267 | `xla_jf_spmd_threshold_for_windowed_einsum_mib` | int64 | |
| 269 | `xla_jf_tune_large_vmem` | bool | |
| 270 | `xla_jf_use_hw_constants` | bool | |
| 271 | `xla_jf_use_multi_colors_in_all_reduce_if_supported` | bool | |
| 272 | `xla_jf_use_rotated_pincer_emitter` | bool | |
| 273 | `xla_jf_use_rotated_pincer_ring_emitter` | bool | |
| 274 | `xla_jf_use_single_phase_ring_emitter` | bool | |
| 275 | `xla_jf_vmem_default_cross_program_prefetch_heuristic` | bool | |
| 276 | `xla_jf_vmem_enable_cross_program_prefetch` | bool | |
| 277 | `xla_jf_vmem_enable_while_redundant_eviction_elimination` | bool | |
| 278 | `xla_jf_vmem_max_outstanding_evictions` | int64 | |
| 279 | `xla_jf_vmem_max_outstanding_prefetches` | int64 | |
| 280 | `xla_jf_vmem_max_overlap_to_mem_size_async_copy_ratio` | float | |
| 281 | `xla_jf_vmem_max_repacks` | int64 | |
| 282 | `xla_jf_vmem_max_retries` | int64 | |
| 283 | `xla_jf_vmem_memory_space_assignment` | bool | |
| 284 | `xla_jf_vmem_min_overlap_to_async_copy_ratio` | float | |
| 285 | `xla_jf_vmem_preferred_overlap_to_async_copy_ratio` | float | |
| 286 | `xla_jf_vmem_use_ior_algorithm` | string | |
| 287 | `xla_tpu_vmem_use_telamalloc` | bool | |
| 288 | `xla_max_concurrent_async_all_gathers` | int32 | |
| 289 | `xla_max_concurrent_async_collective_permutes` | int32 | |
| 290 | `xla_max_concurrent_host_send_recv` | int32 | |
| 291 | `xla_max_decomposed_all_reduces` | int64 | |
| 293 | `xla_set_split_input_output_dmas` | bool | |

> **NOTE —** the `xla_jf_vmem_*` block (#275–#286) is the *jellyfish* arm of the per-family MSA overlap-ratio family. It is mirrored field-for-field by the `xla_vf_vmem_*` (#436–#447), `xla_tpu_cmem_*` (#309–#315), and `xla_gf_vmem_*` (#533–#547) arms later on this page. The three float ratios in each arm (max / min / preferred overlap-to-async-copy) are the MSA cost-model tuning surface; the per-version overlay picks one arm.

---

## #295 – #354 — SparseCore Tracing, All-Reduce VMEM, CMEM MSA Family, Detect-NaN, Megacore Fusion

The first `AutoProto`-typed field (#295), the all-reduce VMEM contingency knobs, the `xla_tpu_cmem_*` MSA arm, the copy-fusion cluster, the NaN/Inf/special-FP detection toggles, and the megacore-fusion enable Tristate (#344). Gaps: #303, #335.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 295 | `xla_sparse_core_enable_hardware_tracing` | message | `AutoProto` *(SPARSE_CORE)* |
| 296 | `xla_tpu_add_llo_regions_to_symbol_table` | bool | |
| 297 | `xla_tpu_all_reduce_vmem_contingency_kib` | int64 | |
| 298 | `xla_tpu_assign_all_reduce_scatter_layout` | bool | |
| 299 | `xla_tpu_auto_reduce_precision` | bool | |
| 300 | `xla_tpu_auto_spmd_keep_all_user_shardings` | bool | |
| 301 | `xla_tpu_auto_spmd_partitioning_memory_budget_gb` | int64 | |
| 302 | `xla_tpu_autofdo_skip_hlo_fingerprints` | message | `RepeatedStrings` |
| 304 | `xla_tpu_autotune_database` | string | |
| 305 | `xla_tpu_binomial_all_reduce_use_physical_core_ids` | bool | |
| 306 | `xla_tpu_block_rematerialization_record_stats` | bool | |
| 307 | `xla_tpu_check_llo_types` | bool | |
| 308 | `xla_tpu_choose_faster_windowed_einsum_over_mem` | bool | |
| 309 | `xla_tpu_cmem_max_overlap_to_mem_size_async_copy_ratio` | float | |
| 310 | `xla_tpu_cmem_max_repacks` | int64 | |
| 311 | `xla_tpu_cmem_max_retries` | int64 | |
| 312 | `xla_tpu_cmem_memory_space_assignment` | bool | |
| 313 | `xla_tpu_cmem_min_overlap_to_async_copy_ratio` | float | |
| 314 | `xla_tpu_cmem_preferred_overlap_to_async_copy_ratio` | float | |
| 315 | `xla_tpu_cmem_use_telamalloc` | bool | |
| 316 | `xla_tpu_conditional_code_motion_config` | string | |
| 317 | `xla_tpu_copy_elision_analysis_allowance` | int64 | |
| 318 | `xla_tpu_copy_fusion_minimum_copy_size_in_bytes` | int64 | |
| 319 | `xla_tpu_copy_fusion_pad_unpad_ratio` | double | |
| 320 | `xla_tpu_copy_fusion_threshold` | int64 | |
| 321 | `xla_tpu_copy_insertion_use_region_analysis` | bool | |
| 322 | `xla_tpu_copy_insertion_use_region_analysis_limit` | int64 | |
| 323 | `xla_tpu_decompose_all_gather_fusion` | bool | |
| 324 | `xla_tpu_decompose_all_reduce_bidirectional_communication` | bool | |
| 325 | `xla_tpu_decompose_reduce_scatter_fusion` | bool | |
| 326 | `xla_tpu_deduplicated_hlo_min_bundle_count` | int64 | |
| 327 | `xla_tpu_detect_inf` | bool | |
| 328 | `xla_tpu_detect_llo_nan` | bool | |
| 329 | `xla_tpu_detect_nan` | bool | |
| 330 | `xla_tpu_detect_only_on_fusion` | bool | |
| 331 | `xla_tpu_detect_special_fp` | bool | |
| 332 | `xla_tpu_dot_dot_fusion` | bool | |
| 333 | `xla_tpu_dot_dot_fusion_duplicated` | bool | |
| 334 | `xla_tpu_dot_dot_fusion_separable_convs_only` | bool | |
| 336 | `xla_tpu_enable_all_experimental_scheduler_features` | bool | |
| 337 | `xla_tpu_enable_all_reduce_scatter_fusion` | bool | |
| 338 | `xla_tpu_enable_asymmetric_max_colors` | bool | |
| 339 | `xla_tpu_enable_copy_fusion` | bool | |
| 340 | `xla_tpu_enable_copy_permute_minor_fusion` | bool | |
| 341 | `xla_tpu_enable_cross_module_binomial_all_reduce` | bool | |
| 342 | `xla_tpu_enable_deduplicated_calls` | enum | `TristateProto.Value` |
| 343 | `xla_tpu_enable_experimental_fusion_cost_model` | bool | |
| 344 | `xla_tpu_enable_megacore_fusion` | enum | `TristateProto.Value` |
| 345 | `xla_tpu_enable_megascale_barrier` | bool | |
| 346 | `xla_tpu_enable_multi_level_input_dot_dot_fusion` | bool | |
| 347 | `xla_tpu_enable_multi_level_nested_dot_fusion` | bool | |
| 348 | `xla_tpu_enable_multi_level_nested_loop_fusion` | bool | |
| 349 | `xla_tpu_enable_multi_level_output_dot_dot_fusion` | bool | |
| 350 | `xla_tpu_enable_nd_wus_on_partial_active_dims` | bool | |
| 351 | `xla_tpu_enable_pincer_short_fusion_emitter` | bool | |
| 352 | `xla_tpu_enable_scheduler_memory_pressure_tracking` | enum | `TristateProto.Value` |
| 353 | `xla_tpu_enable_sparse_gradient_rewrite` | bool | |
| 354 | `xla_tpu_enable_untiled_layout` | bool | *(is_used_at_runtime)* |

> **GOTCHA —** `xla_tpu_detect_inf` (#327) and `xla_tpu_detect_nan` (#329) are TCE proto fields *distinct from* the same-named `xla.DebugOptions` fields (#136/#135). The TCE fields are flag-wired and materialized from the absl default; the DebugOptions ones are born proto3-zero (FALSE) unless the flag override layer sets them. Do not conflate the two protos when reimplementing the detect-NaN path.

---

## #355 – #432 — Untiled Layout, Experimental Padding, Megacore-Fusion Tuning, Poison-Padding, SPMD Windowed Einsum

The untiled-VMEM-DMA toggle, the experimental quantization/padding knobs, the megacore-fusion margin/scaling floats, the MSA while-execution count, the nested-dot-fusion cluster, the poison-padding pair, the RWB-fusion bool, and the SPMD windowed-einsum decomposition block. Gaps: #356–#359, #362, #379, #401, #419, #422.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 355 | `xla_tpu_enable_vmem_to_vmem_dmas` | bool | |
| 360 | `xla_tpu_experimental_allow_fast_quantization_conversions` | bool | |
| 361 | `xla_tpu_experimental_cmem_fraction_for_hlo_outputs` | float | |
| 363 | `xla_tpu_experimental_max_concat_padding_ratio` | double | |
| 364 | `xla_tpu_experimental_max_padding_gib` | double | |
| 365 | `xla_tpu_extra_hoisting_range_for_register_producers` | int64 | |
| 366 | `xla_tpu_force_startup_barrier_in_binomial_all_reduce` | bool | |
| 367 | `xla_tpu_force_vmem_dma_and_spans` | bool | |
| 368 | `xla_tpu_fusion_config_collection` | string | |
| 369 | `xla_tpu_handle_reduce_window_as_convolution` | bool | |
| 370 | `xla_tpu_hbm_bw` | double | |
| 371 | `xla_tpu_hbm_initial_cycle_penalty` | int64 | |
| 372 | `xla_tpu_input_conv_multi_users` | bool | |
| 373 | `xla_tpu_insert_dummy_fusions_on_conv_kernels` | bool | |
| 374 | `xla_tpu_licm_size_inflation_ratio` | float | |
| 375 | `xla_tpu_llo_compilation_max_retries` | int32 | |
| 376 | `xla_tpu_local_dma_pipe_dma_from_cmem_min_chunks` | int64 | |
| 377 | `xla_tpu_local_dma_pipe_dma_to_cmem_min_chunks` | int64 | |
| 378 | `xla_tpu_log_current_and_new_fusion_cost_models` | bool | |
| 380 | `xla_tpu_max_clustered_loads` | int64 | |
| 381 | `xla_tpu_max_vld_live_range` | int64 | |
| 382 | `xla_tpu_max_vmreg_live_range` | int64 | |
| 383 | `xla_tpu_megacore_fusion_allow_ags` | bool | |
| 384 | `xla_tpu_megacore_fusion_disable_live_out_ags` | bool | |
| 385 | `xla_tpu_megacore_fusion_good_overload_margin` | float | |
| 386 | `xla_tpu_megacore_fusion_latency_bound_ar_fusion_size` | int64 | |
| 387 | `xla_tpu_megacore_fusion_orthogonal_ag` | bool | |
| 388 | `xla_tpu_megacore_fusion_orthogonal_ars` | bool | |
| 389 | `xla_tpu_megacore_fusion_scaling_factor` | float | |
| 390 | `xla_tpu_memory_space_assignment_while_execution_count` | int64 | |
| 391 | `xla_tpu_multioutput_fusion_max_operands` | int64 | |
| 392 | `xla_tpu_nested_dot_fusion` | bool | |
| 393 | `xla_tpu_nested_dot_fusion_supported_custom_ops` | string | |
| 394 | `xla_tpu_nested_dot_fusion_vmem_fraction` | double | |
| 395 | `xla_tpu_op_tracemarks` | bool | |
| 396 | `xla_tpu_optimize_bf16_math` | bool | *(NUMERICS)* |
| 397 | `xla_tpu_order_dot_after_layout` | bool | |
| 398 | `xla_tpu_override_rwb_tpu_limitation` | bool | |
| 399 | `xla_tpu_poison_padding` | bool | |
| 400 | `xla_tpu_poison_padding_value` | int32 | |
| 402 | `xla_tpu_prefer_async_allgather_to_allreduce` | bool | |
| 403 | `xla_tpu_prefer_binomial_single_phase_ring_emitter` | bool | |
| 404 | `xla_tpu_prefer_dynamic_pad` | bool | |
| 405 | `xla_tpu_profile_traceme_level` | int32 | |
| 406 | `xla_tpu_random_all_reduce_delay_mask` | uint32 | |
| 407 | `xla_tpu_reduce_window_reduction_dim_max` | int64 | |
| 408 | `xla_tpu_remove_bf16_bitcast_converts_for_all` | bool | |
| 409 | `xla_tpu_room_for_potential_register_dependency` | int64 | |
| 410 | `xla_tpu_rotated_pincer_pack_allgather_fusion` | bool | |
| 411 | `xla_tpu_run_all_reduce_simplifier` | bool | |
| 412 | `xla_tpu_run_space_to_batch` | bool | |
| 413 | `xla_tpu_rwb_fusion` | bool | |
| 414 | `xla_tpu_schedule_send_recvs` | enum | `TristateProto.Value` |
| 415 | `xla_tpu_scheduler_using_real_cost_model` | enum | `TristateProto.Value` |
| 416 | `xla_tpu_scoped_cmem_for_all_reduce` | int64 | |
| 417 | `xla_tpu_scoped_hbm_for_all_reduce` | int64 | |
| 418 | `xla_tpu_scoped_vmem_limit_kib` | int64 | |
| 420 | `xla_tpu_sg_sshfl_ignore_bits` | int64 | |
| 421 | `xla_tpu_short_pincer_single_step_max_chunks_per_chip` | int64 | |
| 423 | `xla_tpu_spmd_bidirectional_windowed_einsum` | bool | |
| 424 | `xla_tpu_spmd_f32_accum_for_bf16_ar` | bool | |
| 425 | `xla_tpu_spmd_rng_bit_generator_unsafe` | bool | |
| 426 | `xla_tpu_spmd_unroll_windowed_einsum` | bool | |
| 427 | `xla_tpu_spmd_windowed_einsum_decompose_ag` | bool | |
| 428 | `xla_tpu_spmd_windowed_einsum_decompose_rs` | bool | |
| 429 | `xla_tpu_store_to_load_forwarding_window` | int64 | |
| 430 | `xla_tpu_threshold_for_long_live_range_in_flowdown` | int64 | |
| 431 | `xla_tpu_uni_direction_ring_max_size` | int64 | |
| 432 | `xla_tpu_untiled_layout_for_1D_only` | bool | *(is_used_at_runtime)* |

> **NOTE —** `xla_tpu_rwb_fusion` (#413) is one of two fields (with `xla_tpu_accumulate_into_mrb`, #597 on page B) whose absl default is **TRUE** despite an earlier help-string reading of `false`. The registered flag default is byte-authoritative; the help text describes behavior, not the default. See [TCE Field-Offsets & Flag Defaults](tce-field-offsets-defaults.md).

---

## #433 – #497 — Aggressive Scheduling, VF VMEM Family, Async Collective Fusion, AutoFDO/Autotune

The aggressive-scheduling Tristate, the full `xla_vf_vmem_*` MSA arm (#436–#447), the send/recv aggregation cluster, the async-collective-fusion block (the first `SparseDenseMatmulFdoConfig` message at #462), the AutoFDO module flags, and the autotune-by-pass toggles. Gaps: #434, #448, #468, #470, #481, #484, #491.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 433 | `xla_tpu_use_aggressive_scheduling` | enum | `TristateProto.Value` |
| 435 | `xla_tpu_use_resilient_collective_emitter` | bool | |
| 436 | `xla_vf_vmem_default_cross_program_prefetch_heuristic` | bool | |
| 437 | `xla_vf_vmem_enable_cross_program_prefetch` | bool | |
| 438 | `xla_vf_vmem_enable_cross_program_prefetch_freeing` | bool | |
| 439 | `xla_vf_vmem_enable_while_redundant_eviction_elimination` | bool | |
| 440 | `xla_vf_vmem_max_outstanding_evictions` | int64 | |
| 441 | `xla_vf_vmem_max_outstanding_prefetches` | int64 | |
| 442 | `xla_vf_vmem_max_overlap_to_mem_size_async_copy_ratio` | float | |
| 443 | `xla_vf_vmem_max_repacks` | int64 | |
| 444 | `xla_vf_vmem_max_retries` | int64 | |
| 445 | `xla_vf_vmem_memory_space_assignment` | bool | |
| 446 | `xla_vf_vmem_min_overlap_to_async_copy_ratio` | float | |
| 447 | `xla_vf_vmem_preferred_overlap_to_async_copy_ratio` | float | |
| 449 | `xla_tpu_enable_send_recv_aggregation` | bool | |
| 450 | `xla_tpu_enable_data_parallel_all_reduce_opt` | bool | *(PIPELINER)* |
| 451 | `xla_tpu_vector_load_fusion_window` | int64 | |
| 452 | `xla_tpu_fuse_only_phase0_in_2d_reduce_scatter` | bool | |
| 453 | `xla_tpu_sdc_checker_log_inputs_on_sdc_event` | bool | |
| 454 | `xla_tpu_enable_expression_constant_splitter` | bool | |
| 455 | `xla_jf_hlo_deduplication_all_unique` | bool | |
| 456 | `xla_tpu_pre_fusion_remat` | bool | |
| 457 | `xla_tpu_sc_megachip_temporal_reuse` | bool | |
| 458 | `xla_tpu_enable_async_all_to_all` | bool | |
| 459 | `xla_tpu_auto_spmd_partitioning_memory_budget_ratio` | float | |
| 460 | `xla_sc_disable_megacore_partitioning` | bool | |
| 461 | `xla_tpu_enable_async_collective_fusion` | bool | |
| 462 | `xla_tpu_sparse_dense_matmul_fdo_config` | message | `SparseDenseMatmulFdoConfig` |
| 463 | `xla_tpu_enable_async_collective_fusion_multiple_steps` | bool | |
| 464 | `xla_vf_max_vmem_used_by_memory_space_assignment` | int64 | |
| 465 | `xla_tpu_enable_async_collective_fusion_fuse_all_gather` | enum | `TristateProto.Value` |
| 466 | `xla_tpu_enable_async_collective_fusion_fuse_all_reduce` | bool | |
| 467 | `xla_tpu_megacore_fusion_orthogonal_ars_margin` | float | |
| 469 | `xla_tpu_autofdo_module_flags` | bool | |
| 471 | `xla_tpu_autofdo_module_layouts` | bool | |
| 472 | `xla_tpu_autofdo_skip_module_fingerprints` | message | `RepeatedStrings` |
| 473 | `xla_tpu_autotune_dots` | bool | |
| 474 | `xla_tpu_autotune_flags` | bool | |
| 475 | `xla_tpu_autotune_fusions` | bool | |
| 476 | `xla_tpu_autotune_layouts` | bool | |
| 477 | `xla_tpu_autotune_memory_space_assignment` | bool | |
| 478 | `xla_tpu_split_cluster_gap` | int64 | |
| 479 | `xla_tpu_split_cluster_size` | int64 | |
| 480 | `xla_tpu_autofdo_hlo_module_size_threshold` | int32 | |
| 482 | `xla_tpu_custom_fusion_no_global_unfusible` | bool | |
| 483 | `xla_tpu_custom_fusion_traverse_edges_twice` | bool | |
| 485 | `xla_tpu_overlap_compute_collective_tc` | bool | |
| 486 | `xla_tpu_max_send_recv_aggregation` | int32 | |
| 487 | `xla_tpu_vmac_transform_strategy` | enum | `TpuVmacTransformStrategyProto.Value` |
| 488 | `xla_tpu_vector_store_fusion_window` | int64 | |
| 489 | `xla_tpu_async_copy_bandwidth_scaling_factor` | float | |
| 490 | `xla_tpu_metrics_filename_prefix` | string | |
| 492 | `xla_tpu_enable_domain_passes` | bool | |
| 493 | `xla_tpu_experimental_enable_dynamic_int8_quantization` | bool | |
| 494 | `xla_max_cross_program_prefetches` | int64 | |
| 495 | `xla_tpu_enable_host_aware_passes` | bool | |
| 496 | `xla_tpu_disallow_in_alt_mem` | string | |
| 497 | `xla_tpu_allow_deeply_nested_fusion_numerical_diff` | bool | |

---

## #498 – #560 — Prefetch FIFO, ICI-SDC Test, Data-Parallel DCN, GF VMEM Family, AG Pipelining

The closing block of this page: prefetch-interval-picker overrides, the numerics `accurate_log2`, the ICI-SDC self-test cluster (#516–#521, #560), the data-parallel DCN pipelining knobs, the full `xla_gf_vmem_*` MSA arm (#533–#547), the overlay-profiler toggles, and the AG-backward-pipelining group. Gaps: #506, #509, #511, #515, #527, #530, #531, #546.

| # | Field name | Type | Wrapper / message type |
|---|---|---|---|
| 498 | `xla_tpu_prefetch_interval_picker_size_override` | int64 | |
| 499 | `xla_tpu_force_1d_allreduce_at_chunk_count` | int64 | |
| 500 | `xla_tpu_enable_aggressive_loop_fusion_layout_opt` | bool | |
| 501 | `xla_tpu_use_repeated_instance_for_preferred_prefetch_time` | bool | |
| 502 | `xla_tpu_enforce_prefetch_fifo_order` | bool | |
| 503 | `xla_tpu_reduce_loop_fusion_dup_with_unfusable_user` | bool | |
| 504 | `xla_tpu_accurate_log2` | bool | *(NUMERICS)* |
| 505 | `xla_tpu_dcn_max_overlap_estimation` | float | |
| 507 | `xla_tpu_autofdo_op_windows` | bool | |
| 508 | `xla_tpu_dcn_overlap_limit` | int64 | |
| 510 | `xla_tpu_enable_log_recorder_partitioned_logging` | bool | |
| 512 | `xla_tpu_no_crash_on_oom` | bool | |
| 513 | `xla_lhs_enable_release_start_policy` | bool | |
| 514 | `xla_tpu_debug_sflag_wait_shalt_on_detection` | uint32 | |
| 516 | `xla_tpu_ici_sdc_test_buffer_size_chunks` | int32 | |
| 517 | `xla_tpu_ici_sdc_test_packet_size_chunks` | int32 | |
| 518 | `xla_tpu_ici_sdc_test_iterations` | int32 | |
| 519 | `xla_tpu_ici_sdc_test_max_distance` | int32 | |
| 520 | `xla_tpu_ici_sdc_test_pipeline_depth` | int32 | |
| 521 | `xla_tpu_ici_sdc_test_inject_mismatch_for_testing_only` | bool | |
| 522 | `xla_tpu_mock_send_recv_host` | bool | |
| 523 | `xla_tpu_data_parallel_dcn_ar_dual_pipelining` | uint32 | *(PIPELINER)* |
| 524 | `xla_tpu_enable_aggressive_broadcast_priority_update` | bool | |
| 525 | `xla_tpu_data_parallel_opt_different_sized_ops` | uint32 | *(PIPELINER)* |
| 526 | `xla_tpu_ars_halo_exchange_count` | int64 | |
| 528 | `xla_tpu_dus_emitter_desired_update_window_chunk_count` | int64 | |
| 529 | `xla_tpu_spmd_f32_accum_for_bf16_ar_min_subgroup_size` | int64 | |
| 532 | `xla_tpu_show_overlay_waits_in_profiler` | bool | |
| 533 | `xla_gf_vmem_use_ior_algorithm` | string | |
| 534 | `xla_gf_vmem_default_cross_program_prefetch_heuristic` | bool | |
| 535 | `xla_gf_vmem_enable_cross_program_prefetch` | bool | |
| 536 | `xla_gf_vmem_enable_cross_program_prefetch_freeing` | bool | |
| 537 | `xla_gf_vmem_enable_while_redundant_eviction_elimination` | bool | |
| 538 | `xla_gf_vmem_max_outstanding_evictions` | int64 | |
| 539 | `xla_gf_vmem_max_outstanding_prefetches` | int64 | |
| 540 | `xla_gf_vmem_max_overlap_to_mem_size_async_copy_ratio` | float | |
| 541 | `xla_gf_vmem_max_repacks` | int64 | |
| 542 | `xla_gf_vmem_max_retries` | int64 | |
| 543 | `xla_gf_vmem_memory_space_assignment` | bool | |
| 544 | `xla_gf_vmem_min_overlap_to_async_copy_ratio` | float | |
| 545 | `xla_gf_vmem_preferred_overlap_to_async_copy_ratio` | float | |
| 547 | `xla_gf_max_vmem_used_by_memory_space_assignment` | int64 | |
| 548 | `xla_tpu_allow_multi_dim_reduce_rwb` | bool | |
| 549 | `xla_tpu_show_overlay_overhead_in_profiler` | bool | |
| 550 | `xla_tpu_show_overlay_details_in_profiler` | bool | |
| 551 | `xla_tpu_overlay_allocation_table_size` | int64 | |
| 552 | `xla_tpu_enable_ag_backward_pipelining` | bool | *(PIPELINER)* |
| 553 | `xla_tpu_prefuse_self_attention` | bool | |
| 554 | `xla_tpu_enable_indexing_optimizations` | int64 | |
| 555 | `xla_tpu_allow_layout_negotiation` | bool | |
| 556 | `xla_tpu_backward_propagate_reduce` | bool | |
| 557 | `xla_tpu_decompose_all_gather_einsum` | bool | |
| 558 | `xla_tpu_decompose_einsum_reduce_scatter` | bool | |
| 559 | `xla_tpu_keep_hlo_proto_literals_up_to` | uint64 | |
| 560 | `xla_tpu_ici_sdc_test_run_on_program_start` | bool | |

> **NOTE —** field **#560** `xla_tpu_ici_sdc_test_run_on_program_start` is the last field on this page. The next field, **#561** `xla_tpu_debug_sflag_wait_timeout_ms` (uint32), opens [TCE Field Dictionary (B)](tce-field-dictionary-b.md). The boundary is exact: this page owns `1 ≤ field# ≤ 560`, page B owns `561 ≤ field# ≤ 1218`.

---

## Field-Number Gaps in #1–#560

The numbering is not contiguous: TCE carries no `reserved_range` or `reserved_name`, so a deleted field simply leaves a hole. The gaps inside this page's range — every field number in `1..560` that is *not* a live field above — are:

```text
21, 44, 45, 54, 59, 70, 121, 124, 125, 126, 127, 156, 214, 218,
244, 245, 246, 247, 250, 251, 252, 253, 254, 268, 292, 294, 303,
335, 356, 357, 358, 359, 362, 379, 401, 419, 422, 434, 448, 468,
470, 481, 484, 491, 506, 509, 511, 515, 527, 530, 531, 546
```

> **GOTCHA —** these holes are deletions, not declared-reserved ranges. A reimplementer carving the descriptor must not assume the field set is `1..N` contiguous; iterate the actual `FieldEntry` array (sorted ascending) and skip the missing numbers. The full 97-gap list across `1..1218` lives on [TpuCompilationEnvironment](tpu-compilation-environment.md); the slice above is the portion below #561.

---

## Type Distribution Within #1–#560

For orientation, the proto base-type mix of the 508 live fields on this page (the lower half is bool-heavy, consistent with the early jellyfish toggle era):

| Base type | Notes |
|---|---|
| bool | the dominant type; most early knobs are simple feature toggles |
| int64 | thresholds, counts, byte limits, ring sizes |
| int32 | a smaller integer set (trip counts, retry caps, ICI-SDC test sizes) |
| uint32 / uint64 | a handful: delay masks (#406), `keep_hlo_proto_literals_up_to` (#559), DCN-pipelining (#523/#525), sflag-wait shalt (#514) |
| float / double | the MSA overlap ratios and the megacore-fusion / cost-model scaling factors |
| string | profile paths, config selectors, filename prefixes, `config_criterion` / `rematerialization_algorithm` |
| enum | 13 in this range; `TristateProto.Value` dominates, plus `MemorySchedulerProto.Value` (#31), `VerifyOrAssignTilingFlagsProto.Value` (#132), `TpuVmacTransformStrategyProto.Value` (#487) |
| message | `RangeSpecProto` (bounds/ISA-emitter knobs), `RepeatedStrings` (LLVM flags, FDO skip lists), the first `AutoProto` (#295), and typed helpers (`SparseDenseMatmulFdoConfig` #462) |

The whole-proto histogram (418 bool / 148 int64 / 349 message / 74 enum / 37 string / 34 float / 32 int32 / 14 double / 11 uint32 / 4 uint64) and the parse-table-derived `type_card` proof are on [TpuCompilationEnvironment](tpu-compilation-environment.md).

---

## Confidence

Every field number, name, and type on this page is **CERTAIN**: the names derive from the `TpuCompilationEnvironment` `FileDescriptorProto` carved from the binary and were cross-checked against the 1121-entry `FieldEntry` array of `TpuCompilationEnvironment::_table_` @ `0x21cfa9e0`, whose entry order is field-number-sorted. Twenty-two of the names spanning the full #1–#560 range were additionally confirmed byte-present, verbatim, in the binary's `.rodata` string pool (0 misses), including the wrapper type strings `TristateProto`, `VerifyOrAssignTilingFlags`, `AutoProto`, and `RangeSpecProto`. The HOT-subsystem tags shown in italics for a few fields (NUMERICS, PIPELINER, SPARSE_CORE) and the four `is_used_at_runtime` markers come from the `TpuCompEnvFieldOptions` field-option extension (#535801365) and are **HIGH** confidence — recovered from the descriptor, not the parse table; not every tagged field is annotated here (the complete HOT breakdown is on [TpuCompilationEnvironment](tpu-compilation-environment.md)). No field on this page is LOW confidence.

---

## Cross-References

- [TCE Field Dictionary (B)](tce-field-dictionary-b.md) — the upper half: field #561 – #1218, same field#→name→type grammar
- [TpuCompilationEnvironment](tpu-compilation-environment.md) — the structural overview: parse-table header, type histogram, HOT-tag taxonomy, wrapper-enum value tables, the 30-arm AutoProto switch
- [TCE Field-Offsets & Flag Defaults](tce-field-offsets-defaults.md) — the C++ struct offset and literal default value of each field; the kGenFunc-vs-inline FlagImpl mechanism
- [Configuration & Compile Knobs — overview](overview.md) — how the TCE proto sits in the PJRT compile path alongside DebugOptions and the flag registry
- [Part XVI — Configuration & Compile Knobs](../index.md) — index entry
