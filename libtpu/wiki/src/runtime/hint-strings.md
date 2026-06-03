# User-Facing Hint Strings

> *All offsets on this page are **file offsets** (hex) into `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). In this build `.rodata` VMA == file offset (1:1, base `0x84a0000`), so a file offset is also the load address. Other wheels will differ.*

## Abstract

This page catalogs libtpu's **hint strings**: the advisory prose the runtime and the XLA:TPU compiler emit *alongside* an error to tell the operator or JAX user what to **do** about it. They are the actionable companions to the raw error/status *templates* catalogued on [`error-templates.md`](error-templates.md). Where an error template states the fact ("`X` must have rank >= N: got %s"), a hint appends the remedy ("Please use `--xla_tpu_rwb_fusion=false`", "Reduce TPU memory usage", "Please remove the hosts from the fleet"). The two live adjacent in `.rodata` and are joined at a `.text` `absl::StrCat`/`StrFormat` callsite; this page owns the hint half and its trigger/knob mapping, not the templates.

The hint surface is signposted by **five marker tokens**, and the choice of token is itself a taxonomy of *who* fixes the problem. `--flag=value`, "Reduce … memory", and "Please remove the hosts …" are **operator-actionable** — the user has a self-service remedy. `b/<id>`, `go/<link>`, and "please file a bug" point the user *back at the XLA/TPU team* — they signal an internal limitation or a known issue, not a knob the user can turn. This split (operator-actionable vs file-it-upstream) is the page's primary axis, refined into seven **actionability classes**: flag-suggestion, doc-link, bug-report, capacity/OOM, deprecation, perf-tuning, and operator-action.

This is a **reference catalog**, not an algorithm. Each section groups hints by trigger and, where the binary shows it, pairs the hint with the condition that fires it and the flag/knob it names. Flag names are byte-confirmed real `absl::Flag` globals (each has an `AbslFlagHelpGenFor<name>` symbol); their *help-text bodies* live in the config section and are out of scope here. The page does **not** claim to know which error template each hint is concatenated onto — that link is a `.text` callsite not yet traced (see [Not Traced](#not-traced)).

The catalog contract:

- **The five marker tokens** and how they partition operator-actionable from file-it-upstream.
- **The seven actionability classes**, each with its hints grouped by subsystem.
- **For each hint:** the verbatim string, its file offset, and the flag/knob or tracker coordinate it references.
- **The direction-of-default caveat:** whether the advised flag value is the workaround or the on-switch — directional, not byte-confirmed.

| | |
|---|---|
| **Section** | `.rodata` of `libtpu.so` (base `0x84a0000`, file-offset == VMA) |
| **Marker tokens** | `--<flag>=<val>` · `go/<link>` · `b/<id>` · "file a bug" · "Reduce … memory" |
| **Distinct `go/` links** | 54 (514 occurrences are per-gen ISA-counter docs, not remedies) |
| **Distinct `b/<id>` ids** | 31 (all with recovered context) |
| **Flag-suggestion hints** | ~28 strings; 10/10 spot-checked flags confirmed as registered `absl::Flag` |
| **Concatenation callsite** | `.text` `absl::StrCat`/`StrFormat` — **not traced** (hint ↔ template link open) |

At a glance, by trigger:

| Actionability class | Trigger (what fires the hint) | Primary token | Count (TPU/XLA subset) |
|---|---|---|---|
| flag-suggestion | a knob is unset / mis-set, or a fallback path was hit | `--<flag>=<val>` | ~28 strings |
| doc-link (action) | a failure with a documented resolution playbook | `go/<link>` (non-counter) | ~14 |
| doc-link (ISA) | reading the ISA spec for a perf counter | `go/<gen>-isa#anchor` | 514 occ / ~16 distinct |
| bug-report | an internal invariant broke / unimplemented path | "file a bug" · `b/<id>` | ~40 prose + 31 `b/<id>` |
| capacity / OOM | HBM/VMEM/Smem pressure, allocation failure | "Reduce … memory" · OOM | ~15 |
| deprecation | a removed/migrating flag or feature is used | "deprecated" · "no longer" | ~7 |
| perf-tuning | a suboptimal-but-correct code path was taken | "suboptimal" · "inefficient" | ~6 |
| operator-action | fleet/topology/sequencing remedy, no bug-id | "Please …" · "Ensure …" | ~25 |

---

## The Five Marker Tokens

The hint surface was located by `rg` over the string table for five token families. They are not just search anchors — the token a hint carries is a reliable predictor of its actionability class, because libtpu's authors use each token consistently.

```text
--<flag>=<value>     OPERATOR-ACTIONABLE  → set this knob and retry
"Reduce … memory"    OPERATOR-ACTIONABLE  → free HBM / raise a limit
"Please remove …"    OPERATOR-ACTIONABLE  → fleet/topology remedy
go/<link>            FILE-IT-UPSTREAM     → read the team's doc/playbook
b/<id>               FILE-IT-UPSTREAM     → known issue, comment/track
"please file a bug"  FILE-IT-UPSTREAM     → internal invariant broke
```

> **NOTE —** the split is a heuristic, not a hard partition: a single hint can carry both ("…suboptimal MXU throughput … Please file a bug with XLA:TPU compiler team, and use `--xla_tpu_accumulate_into_mrb=false` in the meantime." at `0xa083064` is *both* a bug-report and a flag-suggestion). When a hint mixes tokens it is catalogued under its primary remedy and cross-noted.

> **GOTCHA —** the `go/` and `b/` tokens are dominated by *non-actionable* uses. 514 of ~640 `go/` occurrences are per-gen ISA-spec links embedded in hardware-perf-counter description strings (`[counter](http://go/glc-sc-isa#anchor)`), not error remedies. A reimplementer scanning for "actionable hints" by grepping `go/` alone will drown in counter documentation; filter to the non-counter set ([Doc-Link Hints](#doc-link-hints-golink)).

---

## Flag-Suggestion Hints

The self-service remedy class: an error or log line names a specific `--xla_*` / `--megascale_*` / `--tpu_*` / `--deepsea_*` flag and the value to set. Every flag named below was confirmed to be a registered `absl::Flag` global by the presence of its `AbslFlagHelpGenFor<name>` symbol (10/10 spot-checked resolved 1:1; flag help-text bodies belong to the config section).

### Fusion / MXU

| Offset | Hint string (verbatim) | Flag referenced | Confidence |
|---|---|---|---|
| `0x858bbcf` | "(1) Please use --xla_tpu_rwb_fusion=false (and --xla_tpu_dot_dot_fusion=false if failure persists), Reason: found fallback window config while lowering fusion." | `xla_tpu_rwb_fusion`, `xla_tpu_dot_dot_fusion` | CERTAIN |
| `0xa2b7034` | (duplicate of `0x858bbcf` — same rwb/dot_dot fusion remedy) | (same) | CERTAIN |
| `0x96c35ed` | "PartialReduce is designed to be used with fusion. Did you forget to set \`--xla_tpu_nested_dot_fusion=true\`?" | `xla_tpu_nested_dot_fusion` | CERTAIN |
| `0xa083064` | "…suboptimal MXU throughput on this HLO. Please file a bug with XLA:TPU compiler team, and use --xla_tpu_accumulate_into_mrb=false in the meantime." | `xla_tpu_accumulate_into_mrb` | HIGH |

> **CORRECTION (HINT-1) —** the raw extraction listed the rwb/dot_dot duplicate at `0x2b7034`. The strings sidecar places it at `0xa2b7034` (the leading `a` was dropped); both copies of the string are byte-identical. Offsets on this page use the sidecar value.

### SparseCore / embedding

| Offset | Hint string (verbatim) | Flag referenced | Confidence |
|---|---|---|---|
| `0x9fe2e58` | "If you are seeing this error when attempting to compile a distributed embedding model try running with --xla_tpu_embedding_table_oblongness_threshold=1 to ensure the embedding table is not given a tiled layout." | `xla_tpu_embedding_table_oblongness_threshold` | HIGH |
| `0xa073044` | "Skipping reduce-scatter decomposition as SC ND RS needs --xla_tpu_enable_sparse_core_reduce_scatter_v2=true." | `xla_tpu_enable_sparse_core_reduce_scatter_v2` | HIGH |
| `0xa07b05e` | "Number of sparse cores for scatter offloading should not be 0. To disable, please set xla_tpu_enable_offloading_scatter_to_sparsecore to false." | `xla_tpu_enable_offloading_scatter_to_sparsecore` | HIGH |
| `0xa07b0ee` | "Number of SparseCores for gather offloading should not be 0. To disable, please set xla_tpu_enable_offloading_gather_to_sparsecore to false." | `xla_tpu_enable_offloading_gather_to_sparsecore` | HIGH |
| `0x85c0718` | "Unsupported: computing max_nz_per_row requires --xla_sc_fused_scatter_in_compute_loop" | `xla_sc_fused_scatter_in_compute_loop` | HIGH |
| `0x85c076e` | "Unsupported: computing max_unique_nz_per_row requires --xla_sc_fused_scatter_in_compute_loop" | `xla_sc_fused_scatter_in_compute_loop` | HIGH |
| `0x85c07cb` | "Unsupported: computing max_unique_nz_per_row requires --xla_sc_fused_gather_in_compute_loop" | `xla_sc_fused_gather_in_compute_loop` | HIGH |
| `0x86678da` | "…To avoid this failure, try to set a larger number to '--fdo_config_sparsecore_allowed_dropped_id_count_per_epoch' flag" | `fdo_config_sparsecore_allowed_dropped_id_count_per_epoch` | HIGH |

### Scheduler / collective / infeed

| Offset | Hint string (verbatim) | Flag referenced | Confidence |
|---|---|---|---|
| `0xa008ce4` | "…has gaps on the way from that operand to itself. You can use --xla_tpu_scheduling_annotation_deannotate_unsupported_groups=true to deannotate the unsupported groups." | `xla_tpu_scheduling_annotation_deannotate_unsupported_groups` | HIGH |
| `0xa060e03` | "Attempted to use profile-guided latency estimator without the real cost model. Either enable --xla_tpu_scheduler_using_real_cost_model or unset --xla_tpu_impure_latency_hiding_scheduler_profile_path." | `xla_tpu_scheduler_using_real_cost_model`, `xla_tpu_impure_latency_hiding_scheduler_profile_path` | HIGH |
| `0xa04a55e` | "Sinking DCN collectives requires --xla_tpu_use_megascale_host_reduction." | `xla_tpu_use_megascale_host_reduction` | HIGH |
| `0xa05844c` | "Sharded infeed with non-uniform layouts is not supported. Try turning off the infeed layout optimization (--transpose_tpu_infeed=false) and report to XLA team." | `transpose_tpu_infeed` | HIGH |

### Memory / OOM / runtime / debug

| Offset | Hint string (verbatim) | Flag referenced | Confidence |
|---|---|---|---|
| `0x9e6e39e` | "…Aborting compilation early because it's unlikely to have enough device memory. Requires %s, has %s available. If more detailed logging is desired, set --xla_tpu_impure_oom_fast_exit_threshold=-1" | `xla_tpu_impure_oom_fast_exit_threshold` | CERTAIN |
| `0xa074100` | ". Reduce TPU memory usage or set --jellyfish_executor_max_wait_time_for_releasing_memory_on_oom to a larger value." | `jellyfish_executor_max_wait_time_for_releasing_memory_on_oom` | CERTAIN |
| `0xa01af59` | "Spilling sregs to HBM on megacore is implemented but untested; use --xla_enable_megacore_hbm_spill=true to activate, and please update b/177274769 with findings." | `xla_enable_megacore_hbm_spill` | CERTAIN |
| `0xa07a6ba` | ". Restart the job or disable set --megascale_use_numa_aware_threadpool=false." | `megascale_use_numa_aware_threadpool` | HIGH |
| `0x96deddc` | "…bytes. Pass --xla_hbm_logging_buffer_size_bytes=<n>" | `xla_hbm_logging_buffer_size_bytes` | HIGH |
| `0xa06c059` | "Unable to log from the tile because tile log is NOT enabled. Use --xla_tpu_enable_tile_log_recorder=true to enable logging." | `xla_tpu_enable_tile_log_recorder` | HIGH |
| `0xa06c0d5` | "…was invoked but logging was not enabled. Use --xla_tpu_enable_sc_log_recorder=true to enable logging." | `xla_tpu_enable_sc_log_recorder` | HIGH |
| `0xb52d9a0` | "You need to set --deepsea_chips_per_host_bounds, --deepsea_host_bounds to match your setup. Contact tfrt-devs@ if you have any questions." | `deepsea_chips_per_host_bounds`, `deepsea_host_bounds` | HIGH |
| `0x96c2015` | "…the threadpool for tensorflow operations is too small.  Try increasing --45eac_num_operation_threads and please notify barna-core-devs@" | `_45eac_num_operation_threads` (BarnaCore) | HIGH |
| `0x9fff409` | "Very slow compile? If you want to file a bug, run with envvar XLA_FLAGS=--xla_dump_to=/tmp/foo and attach the results." | `xla_dump_to` | HIGH |
| `0xa00da29` | "…if you would like to reliably retrieve the error message, try running it again with --notpu_use_continuations." | `tpu_use_continuations` (negated) | HIGH |
| `0x87fb08b` | "[enable stack trace via --xla_jf_collect_llo_stack_trace]" | `xla_jf_collect_llo_stack_trace` | HIGH |

### Direction-of-default caveat

> **GOTCHA —** the value a hint tells you to set is not necessarily the *opposite* of the default; read the verb. The "use `--flag=false` … in the meantime" / "Please use `--flag=false`" hints (`rwb_fusion`, `dot_dot_fusion`, `accumulate_into_mrb`) name the **workaround** (a non-default toggle to escape a broken path). The "use `--flag=true` to enable" hints (`tile_log_recorder`, `sc_log_recorder`, `sparse_core_reduce_scatter_v2`, `nested_dot_fusion`) name a feature that is **off by default**, where `=true` is the on-switch. Treat these as direction-of-default; only the ~13 rodata-evidenced defaults are byte-confirmed (config section). Do not assume the advised value equals "the default".

---

## Doc-Link Hints (`go/<link>`)

The `go/` shortlink token splits cleanly into a small **actionable playbook** set and a large **ISA-counter-documentation** family. Only the first is a remedy; the second is reference material the profiler attaches to perf counters.

### Actionable / playbook links (TPU/XLA-relevant)

| `go/` link | Hint string (offset) | Subsystem | Confidence |
|---|---|---|---|
| `go/scoped-vmem` | `0xa011573` ". See go/scoped-vmem for more details." | MSA / scoped VMEM | HIGH |
| `go/sc-dynamic-bounded-slice` | `0xa050247` "DynamicBoundedSlice lowering is only supported inside of custom fusions, see go/sc-dynamic-bounded-slice for more information." | SparseCore lowering | HIGH |
| `go/xla_compile_runtime_flag_error` | `0xa047216` "Runtime flags … must be set consistently between compile options and ABSL flags. … See go/xla_compile_runtime_flag_error for resolution." | compile-env flag consistency | HIGH |
| `go/megascale-debug-playbook` | `0xa21a318` "Debug dumping triggered. Refer to go/megascale-debug-playbook for further debugging. megascale_debug_dir = " | megascale runtime | HIGH |
| `go/jfc-errata` | `0x8644f10` / `0xa0d46d9` / `0xa0ee662` "…See errata entry go/jfc-errata#<anchor>" (counter-errata caveats) | JXC ISA errata | HIGH |
| `go/llvm-crash-bug` | `0xa033687` "invalid profile created. Please file a bug at: http://go/llvm-crash-bug and include the profraw files that caused this error." | PGO / LLVM crash report | HIGH |

> **NOTE —** these are the only `go/` links a user can act on to *resolve* a TPU/XLA failure. A second cluster (`go/protection-key-fault`, `go/general-protection-fault`, `go/stacktraces`, `go/cppstackoverflow`, `go/lsan`, `go/prod-naming-1-5`, `go/grpc-without-init-google-exemption`, `go/streamz-force-collection`, `go/no_file_or_rpc_during_init`, `go/redact-debug-string`) comes from statically-linked Google library prose (signal handlers, sanitizers, gRPC, telemetry), not from TPU-specific code paths. They are present in the binary but are not libtpu remedies; treat them as library noise (MEDIUM relevance).

### Per-gen ISA-counter doc links (the bulk: 514 occurrences)

The dominant `go/` use is **not** a remedy. Each hardware performance counter the profiler exposes carries a Markdown link to its ISA-spec section inside the counter's *description* string, e.g. `0xa0dcfac` "This counter counts hold-scalar-issue cycles for `[yN_reserved](http://go/vfc-sc-isaa#yn-reserved)`." The link prefix encodes the TPU generation:

```text
go/jfc-isa, go/jfc-errata                       JXC  (Jellyfish / Dragonfish)
go/pfc-isa, go/pfc-sc-isa, go/pfc-bc-isa,
  go/pfc-memory-system                          PXC  (Pufferfish)
go/vfc-sc-isa (94+ occ), go/vfc-mem             VXC vector-fetch-core (Viperfish)
go/vxc-isa, go/vxc-sc-isa                        VXC ISA (Viperfish / Ghostlite vector)
go/glc-isa, go/glc-sc-isa (94 occ)              GXC general-load-core (Ghostlite)
go/gfc-isa, go/gfc-sc-isa (97 occ), go/gfc-power GXC general-fetch-core
go/core (70 occ)                                generic core-doc anchor
```

> **QUIRK —** one of these links is misspelled in the shipped binary: `go/vfc-sc-isaa` (confirmed at `0xa0dcfac`, double `a`). A reimplementer reproducing the counter-description corpus should preserve the typo to byte-match this build, not "fix" it. The full counter→anchor table is a separate large extraction and is **not** enumerated here.

---

## Bug-Report Hints

The file-it-upstream class. These prose strings point the user at a tracker, a team, or a `b/<id>` known issue. They signal that the user has hit an internal limitation, not a knob to turn.

### "file a bug" prose with tracker coordinates

| Offset | Hint string (verbatim, abbreviated) | Destination | Confidence |
|---|---|---|---|
| `0x9f1d09c` | "DeepseaPlatform and --tpu_use_tfrt=false are now deprecated. … please file a bug at: https://b.stripped_domain/issues/new?component=670280" | tracker component 670280 (TFRT) | CERTAIN |
| `0xa039ed8` | "RoutingTableAnalyzer detects a potential deadlock! File a bug against SliceBuilder (https://b.stripped_domain/issues/new?component=503036). Please attach core dumps retrieved from Coroner." | tracker component 503036 (SliceBuilder) | HIGH |
| `0xaf0bf90` | "Please report a bug at: https://github.com/google/jax/issues/new?assignees=apaszke" | github.com/google/jax (public) | CERTAIN |
| `0xab9e630` (also `0xaba4a30`/`0xaba4b70`/`0xaba4c50`) | "Please file a bug under Platforms > Performance > BarnaCore > Software with repro instructions" | BarnaCore component path (×4) | CERTAIN |
| `0x858c562` | "Kernel body fingerprint collision detected for key: %016x%016x. Please file a bug with the XLA team and provide the colliding kernel bodies." | XLA team | HIGH |
| `0x8a2941d` | "Fatal error in creation of RWB Fusion. Please file a bug with XLA-TPU" | XLA-TPU | HIGH |
| `0x96c1211` | "XLA has not implemented dynamic sized slice with non-trival stride yet. Please file a bug against XLA" | XLA | HIGH |
| `0x96c12d4` | "Unimplemented reduce-window in fusion cost modeling. Please file a bug with XLA" | XLA | HIGH |
| `0x96c1fd3` / `0xa0c9b76` | "Allocated address is not aligned, please file a bug to tfrt-devs@" / "Duplicate allocation found, please file a bug to tfrt-devs@." | tfrt-devs@ | HIGH |
| `0xa036ccd` | "… is an unsupported memory space in TpuCustomCallScopedVmemAdjuster. Please file a P2 *feature request*, against the XLA-TPU team …" | XLA-TPU (P2 FR) | HIGH |
| `0xa03eb45` / `0xa03eb8b` | "Manual sub-axis isn't supported. Please file a bug with a reproducer." / "Non-divisible sharding with unreduced axes isn't supported. …" | XLA (Shardy) | HIGH |
| `0xa1b0507` | "The auto-sharding solver could not find a valid solution within the given time limit. Please report this as a bug!" | XLA (auto-sharding) | HIGH |

### The TPU-lowering invariant block (`0xa0c8b1e..0xa0c9758`)

A contiguous run of "should not happen" templates, one per HLO op the TPU backend expects to have been legalised away before lowering. These are pure internal-bug markers — there is no user remedy.

```text
Encountered <Op> op during TPU lowering that should have been eliminated during
an earlier phase of compilation. This should not happen - please file a bug against XLA.
   <Op> ∈ { Dot, Call, BatchNormTraining, BatchNormInference, BatchNormGrad,
            Pad, Reverse, select-and-scatter, custom-fusion, OutputFusion, … }   (~14 ops)
```

> **CORRECTION (HINT-2) —** the raw notes summarised this block as a uniform "Encountered `<Op>`" template family. The first string in the run, at `0xa0c8b1e`, is actually a sibling variant — "Encountered mismatched layouts for select-and-scatter. This should not happen - please file a bug against XLA." — phrased about *layouts* rather than an un-eliminated op. The "Encountered `<Op>` op during TPU lowering…" template begins at `0xa0c8b8d` (Dot). The block is contiguous but not strictly homogeneous.

### `b/<id>` known-issue / TODO references (31 distinct)

These pin a limitation to a tracked bug. They are not actionable beyond "comment/track" — the most a user does is leave a note on the bug. Representative rows; the full set of 31 is in the raw extraction.

| `b/<id>` | Context (offset) | Class | Confidence |
|---|---|---|---|
| `b/167392593` | `0xa00f6f4` (×4) "TODO(b/167392593): Support {bitcasts merging dims, sliced prefetches, …}." | MSA/prefetch TODO | HIGH |
| `b/177274769` | `0xa01af59` "…use --xla_enable_megacore_hbm_spill=true … please update b/177274769 with findings." | megacore HBM spill | CERTAIN |
| `b/433785288` | `0xa041d37` "…fixed by Shardy partitioner in the future, tracked in b/433785288. Contact Shardy or XLA team for help." | Shardy partitioner | HIGH |
| `b/36072659` | `0xa0af6ba` "Infeed buffer size … bug in the infeed operation (b/36072659). See the bug for a workaround …" | infeed deadlock | HIGH |
| `b/147787375` | `0x99e1405` / `0x99e144d` "Close() appears to be hanging, this might be a deadlock see b/147787375" / "Disable() appears to be hanging, …" | runtime deadlock | HIGH |
| `b/282055166` | `0x9929df3` "Error may indicate firmware queue fullness: b/282055166" | runtime/firmware | HIGH |
| `b/30481585` | `0xa12ba16` "Device reset is not yet supported on this platform (b/30481585)" | runtime/device-reset | HIGH |
| `b/488336614` | `0xa0a0fb0` "b/488336614 Only single-SC element scatter add is supported." | SparseCore scatter | HIGH |
| `b/422762004` | `0xa12c03b` "Gather with implicit convert is not supported. (b/422762004)" | gather lowering | HIGH |
| `b/343490729` | `0x87050df` "Not implemented: TODO(b/343490729): stores packed int2 are not supported" | int2 lowering | HIGH |

> **NOTE —** `b/<id>` ids that point a user at a *workaround* (`b/36072659` infeed, `b/494604538` cancellables) are the closest this class gets to actionable; the rest are progress-tracking only. The 6-digit-minimum `rg` threshold means shorter `b/<id>` or `cl/<id>` changelist references may exist below it (LOW confidence on completeness).

---

## Capacity / OOM Hints

The operator-actionable memory-pressure class: free memory, raise a limit, or wait for compaction. Grouped by where the pressure is detected.

### Compile-time and runtime OOM

| Offset | Hint string (verbatim, abbreviated) | Remedy | Confidence |
|---|---|---|---|
| `0x9e6e39e` | "Aborting compilation early because it's unlikely to have enough device memory. Requires %s, has %s available. … set --xla_tpu_impure_oom_fast_exit_threshold=-1" | compile-time OOM abort + logging knob | CERTAIN |
| `0xa074100` | ". Reduce TPU memory usage or set --jellyfish_executor_max_wait_time_for_releasing_memory_on_oom to a larger value." | runtime OOM → free memory or wait longer | CERTAIN |
| `0xa07b361` | "Not enough HBM spill stack available, please increase." | increase HBM spill stack | HIGH |
| `0xa04bde4` | "Allocation fails. Try again after compaction. Please note that compaction can be slow. If you want to achieve better performance, please manage TPU buffers carefully to avoid this compaction." | retry-after-compaction (capacity + perf) | CERTAIN |

### Embedding capacity

| Offset | Hint string (verbatim, abbreviated) | Trigger | Confidence |
|---|---|---|---|
| `0xa01cc4f` | "TPU embedding: out of memory allocating %lld bytes." | embedding HBM allocation | HIGH |
| `0xa030f0a` | "The current embedding configuration does not fit on the TPU due to HBM space constraints or more optimization algorithms than supported: aggregate_min_resource_count (%d) > total_resource_count (%d)." | embedding config exceeds HBM/optimizer resources | HIGH |
| `0xa056bd4` | "The number of unique optimizers in the TPU embedding configuration exceeds the capacity of the TPU system." | too many distinct optimizers | HIGH |
| `0xa07d172` | "Scatter operand has %d elements, which exceeds the 32-bit limit. Unsupported on SparseCore." | SparseCore 32-bit index limit | HIGH |

### Fusion / VMEM / Smem capacity guards

| Offset | Hint string (verbatim) | Limit | Confidence |
|---|---|---|---|
| `0x84b1b50` / `0x84b1b7d` / `0x9fd4955` | "Nested dot fusion would exceed vmem capacity" / "Custom Fusion would exceed vmem capacity" / "' exceeds VMEM capacity." | VMEM-capacity fusion guard | HIGH |
| `0x84b1a2b` / `0xa1b5992` | "Smem high-water mark exceeds memory capacity" / "SCS high-Smem usage exceeds Smem capacity" | Smem capacity | HIGH |
| `0x9ff331b` | "The input tensor is not on HBM/VMEM and it exceeds the HBM logging buffer limit." | HBM logging-buffer cap (paired with `--xla_hbm_logging_buffer_size_bytes`) | HIGH |
| `0xa1eed4d` | "Too many buffers are colored in the alternate memory. Could not reserve alternate memory for colored output of instruction " | alternate-memory coloring | HIGH |
| `0xa122204` / `0xa122248` | "The size of next-hop routing table (%d) exceeds the chip limit (%d)" / "The size of routing table (%d) exceeds the chip limit (%d)" | ICI routing-table cap | HIGH |

> **NOTE —** the VMEM/Smem/routing-table guards are *capacity rejections*, not remedies — they tell the user the limit was hit but name no flag. The actionable OOM hints (`xla_tpu_impure_oom_fast_exit_threshold`, `jellyfish_executor_max_wait_time_for_releasing_memory_on_oom`) are also flag-suggestions and cross-appear above.

---

## Deprecation Notices

Sparse, and mostly flag-migration. Library "constexprs are no longer supported"-style noise is excluded; only TPU/XLA notices are listed.

| Offset | Notice (verbatim, abbreviated) | Migration | Confidence |
|---|---|---|---|
| `0x9f1d09c` | "DeepseaPlatform and --tpu_use_tfrt=false are now deprecated. This flag is slated for removal. …" | remove; file bug if it was needed | CERTAIN |
| `0x9feecc8` | "--xla_tpu_impure_enable_packed_bf16_math_ops is deprecated. Please use --xla_tpu_bf16_emission_mode in TpuCompilationEnvironment." | → `xla_tpu_bf16_emission_mode` | CERTAIN |
| `0x8a293e0` | "Non-SPMD model parallelism is no longer supported by XLA:TPU" | use SPMD | HIGH |
| `0xa26350a` | "[DEPRECATED_XLA_TPU_FLAG_USE] Deprecated TpuCompilationEnvironment flags were overridden: " | runtime marker (flags overridden) | HIGH |
| `0xa2ad13b` | "[DEPRECATED_XLA_TPU_FLAG_USE] Deprecated TpuCompilationEnvironment flags were present and not matching their default values:" | runtime marker (non-default deprecated flag) | HIGH |
| `0xa0b19dd` (also `0xa0b1a9a`) | "The SegmentShardingHelperDivSimple class can only be used when … BarnaCores per row per task is equal to 1 … Use the SegmentShardingHelperDiv class instead." (also `…ModSimple` → `…Mod`) | BarnaCore class migration | HIGH |
| `0xa0b1c7c` | "DescriptionForDevice.* is not supported for TPU. Please use one of the GetExecutor methods instead." | → `GetExecutor` methods | HIGH |

> **NOTE —** the two `[DEPRECATED_XLA_TPU_FLAG_USE]` strings are *runtime markers* the runtime prints when a deprecated `TpuCompilationEnvironment` flag is set; they prefix the offending flag list rather than naming a single flag. The deprecated-flag *names* themselves belong to the config section.

---

## Perf-Tuning Suggestions

"Suboptimal but correct" notices: the code path works, but the user can do better.

| Offset | Suggestion (verbatim, abbreviated) | Confidence |
|---|---|---|
| `0xa083064` | "…suboptimal MXU throughput on this HLO. … use --xla_tpu_accumulate_into_mrb=false in the meantime." (also a flag-suggestion + bug-report) | HIGH |
| `0x9fd3a5e` | ". Switch to allocate_output to avoid performance penalty." | HIGH |
| `0x9ff1304` | "Concatenate fusion is inefficient." | HIGH |
| `0xa041d37` | "…SPMD will replicate the tensor and then partition it … which is inefficient. … tracked in b/433785288." | HIGH |
| `0xa29fd14` | "Layout inefficient dot whose output shape has small lane/sublane dimensions" | HIGH |
| `0xa04bde4` | "…If you want to achieve better performance, please manage TPU buffers carefully to avoid this compaction." (also a capacity hint) | CERTAIN |

> **NOTE —** the MSA knob `xla_tpu_msa_inefficient_use_to_copy_ratio` (a proto field at `0xbfc053e`) is the heuristic *behind* the "inefficient use-to-copy" perf path; it is named in the config layer, not in a hint string (LOW confidence that any user-facing hint prints it directly).

---

## Operator-Action Prose

Self-service remedies that carry no bug-id: fleet/topology actions, op-sequencing protocols, init/lifecycle guidance, and team pointers.

### Megascale hang digest (8 cause-branch remedies)

Each "Megascale detects a hang that is likely caused by `<cause>`. `<remedy>`." string names a different operator action keyed to the detected cause. The cause→remedy *dispatch* (which `Cause` enum selects which string) is a `.text` switch and is **not traced**.

| Offset | Cause → remedy (verbatim, abbreviated) | Confidence |
|---|---|---|
| `0xa058553` | bad TPU **sparse core** chips → "Please remove the hosts from the fleet and restart the workload. If problem persists please contact Megascale XLA team." | HIGH |
| `0xa05862f` | bad TPU **tensor core** chips → "Please remove the hosts from the fleet and restart the workload. …" | HIGH |
| `0x9ffc2f2` | networking issue → "Please examine the underlying networking stack for the following hosts." | CERTAIN |
| `0x9fd7519` | data-input stall → "Please check the workers to make sure the data input pipeline is working properly." | CERTAIN |
| `0xa06d7d2` | different modules on different devices → "Please confirm that all workers is running the exact same program. …" | HIGH |
| `0xa079562` | inconsistent HLO module compilation → "This is likely a bug in JAX tracing or XLA compiler. Please inspect the HLO dumps …" | HIGH |
| `0xa0d7cec` | worker not queuing programs → "Check if your application is blocked/crashing and preventing JAX to queue the next TPU program (jitted functions)." | HIGH |
| `0x9fe02ae` | unknown cause → "Megascale detects a hang but cannot determine the root cause. Please inspect the full digest below." | CERTAIN |

Associated aborts: `0xa045c93` "Aborting the coordinator as an unrecoverable error is reported …"; `0xa045d37` "Aborting the coordinator after collecting errors … as megascale_error_reporter_abort_on_hang is set to true …".

> **QUIRK —** the bad-SC-chips (`0xa058553`) and bad-TC-chips (`0xa05862f`) remedies are near-identical English with a one-word grammatical drift ("If problem persists" vs "If the problem persists"). They are two distinct rodata strings, one per cause-branch, not a single shared template — a reimplementer building the digest must emit the matching variant per cause, not deduplicate them.

### TPU embedding op-sequencing protocol (`0xa009da1..0xa04698b`)

~10 "Ensure that … before …" hints enforce the embedding-parameter op ordering (`load_` → `retrieve_` → `enqueue_`/infeed). They describe a strict op-protocol the JAX/TF-TPU user must follow; the underlying state machine is not traced here.

```text
"Cannot retrieve embedding parameters … until the previous retrieve/load operation
 has completed. Ensure that the previous … ops have completed before …"

"TPU embedding infeeds cannot be run in the middle of retrieving/loading the embedding
 parameters. Please quiesce all infeeds before retrieving/loading parameters and restart
 the infeeds after … is complete. Ensure that the retrieve_/load_tpu_embedding_*_parameters
 ops have completed before the enqueue_tpu_embedding_*_batch ops are run."   (0xa046624 / 0xa046853)

"Embedding parameters can be loaded … only after the TPU system has been first initialized.
 Ensure that the load_… ops are run only after tpu.initialize_system() is complete."
```

### Init / driver-lifecycle hints

| Offset | Hint (verbatim, abbreviated) | Confidence |
|---|---|---|
| `0x9fe0d47` | "Failed to get tpu system. Please call tf.tpu.experimental.initialize_tpu_system() before running any ops on tpu." | HIGH |
| `0x9fd4688` | "PjRtClient already exists for TPU. This probably means you have already implicitly initialized PJRT. … call tf.tpu.experimental.initialize_tpu_system() explicitly …" | HIGH |
| `0xa094bb2` | "No TPU_SYSTEM device found. Please ensure that you're connected to a host with a TPU_SYSTEM device." | HIGH |
| `0x9fe65a1` | "Failed to initialize TPU system, please contact Cloud TPU Support." | HIGH |
| `0xa07188f` | "WARNING: could not determine %s, please set env var \`%s\` manually, otherwise libtpu.so may not properly initialize." | HIGH |
| `0xa1e3f95` | "Are you using the right LibTPU version? This LibTPU is built for <…>" | HIGH |
| `0xa1a96ba` | "executable is built for device %s of type \"%s\"; cannot run it on device %s of type \"%s\"" | HIGH |
| `0xa0012c5` | "… DMA buffers were still outstanding when the driver was destroyed. … please ensure all buffers are destroyed before destroying driver objects." | HIGH |
| `0xa079d96` | "TPU driver close was incomplete; kernel reported %d device handles outstanding. … Ensure that all userspace access to the device has ended before invoking driver close." | HIGH |
| `0x9ffb075` | "Lost connection to the SliceBuilder controller task (normally worker task 0). Please check controller task status." | HIGH |
| `0xa1fd28c` | "Please ensure that you have only created one TPUEmbedding" | HIGH |

### Contact-team pointers

```text
tfrt-devs@         alignment / allocation / topology-bounds bugs
barna-core-devs@   embedding hot-id-replication threadpool sizing
Shardy team / XLA  manual-axes / unreduced-axes sharding
Megascale XLA team persistent hang after host removal
Cloud TPU Support  TPU-system init failure (the one external channel)
```

---

## The CPU-Feature Startup Guard (`go/sigill-fail-fast`)

A fatal pre-main guard that aborts if the host CPU lacks an ISA feature the build requires. Twelve variants at `0xbe7d460+`, one per feature:

```text
FATAL ERROR: This binary was compiled with <isa> enabled, but this feature is not
available on this processor (go/sigill-fail-fast).
   <isa> ∈ { aes, avx, mmx, pclmul, popcnt, sse, sse2, sse3,
             sse4.1, sse4.2, ssse3, cmpxchg16b }                 (12 variants)
```

> **NOTE —** this is the **absl** CPU-feature startup guard from statically-linked Google code, not a TPU-specific path — it tells the operator their host CPU is too old for the build. It is included here because it is user-facing fatal prose with a `go/` remedy link, but it is not a libtpu remedy. The earlier error-template catalog named 11 of the 12 variants; the twelfth, `cmpxchg16b`, is confirmed present at `0xbe7d460+` (CERTAIN).

---

## Not Traced

This page is the hint half of the diagnostic surface; several links to the rest of the surface are deliberately left open because they require disassembly this catalog did not do.

- **Hint ↔ error-template link.** Hints sit adjacent to their parent error template in `.rodata`, but the concatenation is a `.text` `absl::StrCat`/`StrFormat` callsite. Which of the error-template surface each hint is appended to — and the resulting `absl::StatusCode` — is **not traced**.
- **Direction-of-default.** Whether a flag-suggestion's advised value is the default or the workaround is a `DefaultDebugOptions()` / `FLAGS_*` ctor question, not byte-confirmed for every flag (only ~13 rodata-evidenced defaults are). The verb-based heuristic above is directional, not authoritative.
- **Log severity.** The severity each hint is emitted at (WARNING / ERROR / FATAL) is set at the callsite, except where the prose itself says "FATAL" / "Aborting" / "WARNING".
- **Megascale cause→remedy dispatch.** The 8 hang-digest strings are recovered, but the `Cause`-enum → string switch is not traced.
- **`go/<link>` content** resolves to internal Google URLs not present in the binary; only the link tokens and referencing prose are recovered.
- **`go/<gen>-isa#anchor` → counter map.** The 514-occurrence ISA-doc-link family is summarised by gen-codename mapping only; the per-counter anchor table is a separate large extraction.

---

## Cross-References

- [Error Templates](error-templates.md) — the raw error/status *templates* these hints are appended to; the fact, where the hint is the remedy.
- [Runtime Overview](overview.md) — where in the runtime the diagnostic surface sits.
- [Internal Pass Names](internal-pass-names.md) — the pass/lowering names that appear in many "should not happen during TPU lowering" bug-report hints.
