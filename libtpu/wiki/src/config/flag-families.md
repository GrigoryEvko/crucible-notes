# Flag Families

> *All addresses, symbols, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). Other versions differ.*

## Abstract

libtpu registers ~2048 `absl::Flag<T> FLAGS_<name>` globals, and every flag name carries a *prefix* that is not cosmetic: the prefix is the routing key that decides which config structure consumes the flag, which subsystem owns it, and — for the codename-prefixed families — which TPU generation the knob is scoped to. This page is the **prefix → owner / routing map**: a taxonomy of the ~18 prefix namespaces, what subsystem each belongs to, whether the prefix routes into `xla::DebugOptions`, into the `TpuCompilationEnvironment` (TCE), or into a standalone runtime flag, and — critically — which families are *TPU-live* versus *inherited-from-XLA-but-inert*.

The reference frame is XLA's own flag system. Upstream XLA shares one `DebugOptions` proto across CPU, GPU, and TPU backends, and flags register through two distinct sites: `MakeDebugOptionsFlags` (mangled `MakeDebugOptionsFlagsEPNS_`, present in the binary) wires the `xla_*` generic flags onto `DebugOptions` fields, while the TPU build adds a second, much larger surface — the `xla_tpu_*` / codename families — that lands in the TPU-private TCE and never touches `DebugOptions`. The TPU build *keeps* the GPU/CPU field names in the shared proto descriptor but **registers zero `xla_gpu_*` / `xla_cpu_*` flags** (byte-confirmed: zero `AbslFlagHelpGenForxla_gpu_*` symbols). A reimplementer who enumerates `DebugOptions` fields will see ~275 `xla_gpu_*` and ~64 `xla_cpu_*` strings with no flag behind them — inert metadata, not live knobs.

This page is the *taxonomy and routing map*. It does not re-list the per-flag catalog — that is the [flat atlas](xla-flag-atlas.md). It does not own the `TpuVersion`-aware prefix-strip/select mechanism — that is [flag-prefix-dispatch.md](flag-prefix-dispatch.md). It does not own the proto internals — those are [debugoptions-proto.md](debugoptions-proto.md) and [tpu-compilation-environment.md](tpu-compilation-environment.md). It owns the **prefix → owner classification and the live-vs-inert verdict per family**.

For reimplementation, the contract is:

- **The prefix → owner routing table** — for each of the ~18 prefixes, the registration site, the config struct it lands in (`DebugOptions` vs TCE vs standalone), and the subsystem that consumes it.
- **The codename family model** — that `jf` / `pf` / `vf` / `gf` are per-`TpuVersion` generation namespaces (Jellyfish / Pufferfish / Viperfish / 6acc60406), each carrying the *same* VMEM/MSA knob names scoped to a different generation, and that the catalogued `gl` codename is **not present** in this build.
- **The live-vs-inert classification** — which families have registered `AbslFlagHelpGenFor` symbols (live, settable through `LIBTPU_INIT_ARGS`) versus which survive only as proto descriptor strings (inert on TPU).

| | |
|---|---|
| **Authoritative count method** | `AbslFlagHelpGenFor<name>` mangled symbols — one per registered `absl::Flag` |
| **`xla_*` (generic) registration site** | `MakeDebugOptionsFlags` (`MakeDebugOptionsFlagsEPNS_`, 176 string refs) → `DebugOptions` |
| **TCE flag→field bridge** | `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` · `SetFieldFromFlagString @ 0x1d73fcc0` · `CreateDefaultTpuCompEnv @ 0x1d73dfa0` |
| **DebugOptions baseline** | `DefaultDebugOptionsIgnoringFlags @ 0x1e66a860` |
| **Total registered flags** | ~2048 `FLAGS_<name>` globals; 2107 distinct names incl. rodata-only aliases |
| **Inert (zero registered)** | `xla_gpu_*` (0 flags / 275 strings), `xla_cpu_*` (0 flags / 64 strings) |
| **Codename families present** | `jf` 148, `vf` 16, `gf` 14, `pf` 1 — `gl` **absent** |
| **Confidence** | CONFIRMED (registration symbols counted in-binary) unless a row says otherwise |

---

## 1. The Routing Question

### Purpose

A flag prefix answers three questions at once, and getting any of them wrong sends a reimplementer chasing a field that does not exist or shipping a knob that is silently inert. The three questions are: **(1) does this flag register at all on TPU?** **(2) which config struct stores its value?** **(3) which subsystem reads it?** This section frames the routing; §2–§3 give the per-family answers.

### The three sinks

Every registered flag's value lands in exactly one of three places. The prefix is the discriminator:

```text
PREFIX                         SINK                            REGISTRATION SITE
──────                         ────                            ─────────────────
xla_* (generic, non-tpu)   →   xla::DebugOptions field     →   MakeDebugOptionsFlags
                               (290-field DebugOptions)         (MakeDebugOptionsFlagsEPNS_)

xla_tpu_* / xla_jf_*       →   TpuCompilationEnvironment   →   per-flag FLAGS_<name> ctor,
xla_sc_* / xla_msa_*           field (1:1 flag↔field)           bridged by
xla_gf_* / xla_vf_* / …                                         OverrideTpuCompEnvByCmdLineFlags
barna_core_*                                                    + SetFieldFromFlagString

megascale_* / tpu_* / tf_* →   standalone absl::Flag       →   per-flag FLAGS_<name> ctor;
                               (no proto; read directly)        DCN runtime / driver / TF bridge
```

The fourth, non-sink case is the **inert** family: a name that exists in the binary's `.rodata` (as a `DebugOptions` proto field tag or an error string) but has *no* `FLAGS_<name>` global and *no* `AbslFlagHelpGenFor` symbol. `xla_gpu_*` and `xla_cpu_*` are the whole of this class.

> **GOTCHA —** the prefix `xla_` is *not* a single family. A flag named `xla_foo` (generic) is a `DebugOptions` field, but `xla_tpu_foo`, `xla_jf_foo`, `xla_sc_foo`, `xla_msa_foo`, `xla_gf_foo`, `xla_vf_foo`, `xla_hlo_foo`, `xla_llvm_foo`, `xla_mosaic_foo`, `xla_ior_foo`, `xla_llo_foo`, and `xla_gpu_foo` each route differently. A reimplementer that strips only the leading `xla_` and treats the remainder uniformly will route 1100+ TCE flags into the wrong proto. The routing key is the *second* token, not the first.

### Counting method — why these numbers are authoritative

The per-family counts on this page are **not** raw string greps (those carry a 1–2 digit concatenation-noise suffix from adjacent `.rodata` literals — e.g. `xla_jf_accumulation_reassociation8`). They are counts of the Itanium-mangled `_ZN<len>AbslFlagHelpGenFor<name>8NonConstEv` symbols, one of which is emitted per registered `absl::Flag<T>`. After peeling the trailing digit-noise, `AbslFlagHelpGenForxla_jf_*` yields exactly 148 distinct names — matching the catalogued de-duplicated count. The same method gives a clean, registration-true count per prefix; a prefix with **zero** such symbols is, by construction, not flag-wired in this build.

---

## 2. Core XLA Families (`DebugOptions`-routed and generic)

These prefixes are inherited from upstream XLA. Some are live on TPU; some survive only as shared-proto metadata.

### `xla_*` — generic XLA, the `DebugOptions` proto

The plain `xla_*` prefix (no recognized second token) is the generic XLA flag family: **112** distinct registered names (deduped `AbslFlagHelpGenForxla_*`, disjoint from the `tpu`/`jf`/`sc`/`gpu`/`cpu`/`msa`/`gf`/`vf`/`pf`/`ior`/`mosaic`/`llo`/`hlo`/`llvm` second-token families that get their own rows below). Each maps to a `DebugOptions` field by 1:1 name (`--xla_foo` ↔ field `xla_foo`), registered by `MakeDebugOptionsFlags`. Of the **290** `DebugOptions` wire-fields in libtpu's descriptor, only **2** are wired to a registered standalone `absl::Flag` (`xla_tpu_detect_nan` (135), `xla_tpu_detect_inf` (136)); the rest — including these generic `xla_*` fields — are reached through the PJRT `CompileOptions.debug_options` proto path, not the standalone flag surface (full breakdown on [debugoptions-proto.md](debugoptions-proto.md)). The all-default baseline is `DefaultDebugOptionsIgnoringFlags @ 0x1e66a860`. This family covers cross-backend concerns: the scheduler (`xla_latency_hiding_scheduler_*`), MSA (`xla_enable_cross_program_prefetch`), collectives (`xla_enable_async_all_reduce`), dump/trace (`xla_enable_hlo_trace`), and the `xla_backend_extra_options` string→string escape-hatch map. Owned in detail by [debugoptions-proto.md](debugoptions-proto.md).

### `xla_hlo_*` — HLO-level passes (split: live + proto-only)

`xla_hlo_*` is a *split* family. 5 names are live registered flags (byte-confirmed `AbslFlagHelpGenForxla_hlo_*` symbols): `xla_hlo_scheduling_brkga_{computation_limit,generation_limit,enable_as_fallback,compute_runtime_estimates}` (the BRKGA genetic-scheduler tuning sub-family) and `xla_hlo_parse_memory_schedule_from_file`. These register as standalone `absl::Flag` globals that the HLO scheduler reads, not as TCE fields. Separately, `xla_hlo_print_inline_stack_frames` exists as a *proto-only* `DebugOptions` field (no flag) — generic XLA dump plumbing the TPU backend does not wire.

> **NOTE —** `xla_hlo_*` is the clearest example of why "is the prefix in the proto?" and "is the prefix a flag?" are independent questions. The HLO scheduler knobs are live flags but *not* `DebugOptions` fields; `xla_hlo_print_inline_stack_frames` is a `DebugOptions` field but *not* a flag. The prefix alone does not decide the sink. (CONFIRMED — 5 registration symbols vs the proto-only field listed in [debugoptions-proto.md](debugoptions-proto.md).)

### `xla_llvm_*` — LLVM-backend passes (split: live + proto-only)

`xla_llvm_*` mirrors `xla_hlo_*`. 4 names are live registered flags: `xla_llvm_isa_emitter`, `xla_llvm_isa_emitter_bundles`, `xla_llvm_isa_emitter_force`, `xla_llvm_generate_xla_compatible_dwg` — TPU's LLVM/LLO ISA-emission path. The upstream-generic `xla_llvm_disable_expensive_passes` survives only as a proto-only `DebugOptions` field (no TPU flag). So the *live* `xla_llvm_*` flags are TPU-specific ISA emitter controls, not the generic LLVM pass gate the name suggests.

### `xla_gpu_*` and `xla_cpu_*` — inherited, INERT

These are the inert families. The binary contains ~275 `xla_gpu_*` and ~64 `xla_cpu_*` strings — but **zero** `AbslFlagHelpGenForxla_gpu_*` and **zero** `AbslFlagHelpGenForxla_cpu_*` registration symbols. The strings are the field tags of the shared `DebugOptions` proto descriptor (e.g. `xla_gpu_command_buffer_scheduling_mode`, `xla_gpu_enable_split_k_autotuning`, `xla_cpu_enable_platform_dependent_math`) plus the 22 nested-enum names the GPU/CPU backends use. On TPU they are dead weight: a `DebugOptions` populated by libtpu carries these fields at their proto defaults, but no flag can set them and no TPU consumer reads them.

| Family | Strings in binary | Registered flags | Verdict |
|---|---:|---:|---|
| `xla_gpu_*` | ~275 | **0** | INERT — `DebugOptions` field names only |
| `xla_cpu_*` | ~64 | **0** | INERT — `DebugOptions` field names only |

> **QUIRK —** the TPU build ships the *entire* GPU/CPU `DebugOptions` field set in its descriptor pool but strips all GPU/CPU flag wiring. This is upstream XLA's one-proto-for-all-backends design surviving into a TPU-only binary. A reimplementer enumerating the proto will find 17 proto-only fields (the GPU/CPU/generic subset enumerated on [debugoptions-proto.md](debugoptions-proto.md)); none are reachable from `LIBTPU_INIT_ARGS`. Do not implement `xla_gpu_*` parsing — there is nothing to parse against. (CONFIRMED — zero registration symbols.)

---

## 3. TPU-Private Families (TCE-routed)

These prefixes are TPU-specific. Every one is a registered flag whose value lands 1:1 in a `TpuCompilationEnvironment` field via the `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` bridge. None are `DebugOptions` fields.

### `xla_tpu_*` — the master TPU surface

The dominant family: **909 registered flags** (`AbslFlagHelpGenForxla_tpu_*` symbols; the catalogued 968-distinct union additionally counts rodata-only aliases and error-message-only references). Every `xla_tpu_*` flag is a TCE field by name. The family spans the entire TPU compiler and runtime: scheduler (LHS/ILP/BRKGA/Dozer/LEM), MSA (scoped VMEM/CMEM, prefetch), fusion (rwb/dot-dot/nested-dot/MRB), ICI collectives, numerics, layout, dot/conv, autotune/AutoFDO, and debug/dump. The subsystem keyword taxonomy and representative flags per group are on [xla-flag-atlas.md](xla-flag-atlas.md). ~330 of the TCE fields these flags back are `AutoProto` tri-state knobs (resolved through `AutoOr<T>`), detailed on [overview.md](overview.md) §4.

### Codename families — `jf` / `pf` / `vf` / `gf` (per-`TpuVersion`)

The most distinctive routing fact: four prefixes are **TPU-generation codenames**, each carrying a *parallel* set of knob names scoped to one `TpuVersion`. The codenames are confirmed in the binary as `JELLYFISH`, `PUFFERFISH`, `VIPERFISH`, `DRAGONFISH` tokens.

| Prefix | Codename | Registered | Role |
|---|---|---:|---|
| `xla_jf_*` | Jellyfish | 148 | The TPU XLA backend namespace (compiler core, all gens) |
| `xla_vf_*` | Viperfish | 16 | Per-generation VMEM / MSA overrides |
| `xla_gf_*` | (6acc60406 / v7x TPU7x, `gxc::gfc`) | 14 | Per-generation VMEM / MSA overrides |
| `xla_pf_*` | Pufferfish | 1 | `xla_pf_enable_nd_allreduce` (ND all-reduce gate) |

The `vf` / `gf` families are near-identical name-for-name — they are the *same* memory-subsystem knobs replicated per generation. Compare the byte-confirmed names:

```text
xla_gf_max_vmem_used_by_memory_space_assignment        ┐
xla_vf_max_vmem_used_by_memory_space_assignment        ├─ same knob, different gen
                                                       │
xla_gf_vmem_default_cross_program_prefetch_heuristic   ┐
xla_vf_vmem_default_cross_program_prefetch_heuristic   ├─ same knob, different gen
                                                       │
xla_gf_vmem_enable_cross_program_prefetch              ┐
xla_vf_vmem_enable_cross_program_prefetch              ┘
xla_vf_allow_split_vmem  ·  xla_vf_allow_replicated_vmem_writes  (vf-only)
```

This is the static face of the `TpuVersion`-aware overlay: the per-codename MSA overlay (`baseline ⊕ family-overlay`, v0/1→jf, v2→cmem, v3→vf, v4/5→gf) rewrites a family of TCE fields per generation. The prefix-strip/version-select dispatch that resolves *which* codename family applies at compile time is owned by [flag-prefix-dispatch.md](flag-prefix-dispatch.md).

> **CORRECTION (FAMILY-1) —** the flag catalog's family table lists `xla_gf_*` (14) as the only per-gen VMEM family. Registration-symbol counting shows a co-resident `xla_vf_*` family of 16 (Viperfish) and a 1-flag `xla_pf_*` (Pufferfish), both carrying the same VMEM/MSA knob names. The catalog under-counted the codename families to `gf` alone; `vf` and `pf` are real, byte-confirmed peers. (CONFIRMED — `AbslFlagHelpGenForxla_{vf,pf}_*` symbols.)

> **GOTCHA —** the task brief names a `gl` codename family. **No `gl` family exists in this build** — zero `xla_gl_*` strings and zero `AbslFlagHelpGenForxla_gl_*` symbols. A reimplementer must not reserve a routing arm for it. The live codename set is `{jf, pf, vf, gf}`. (CONFIRMED — empty grep.)

### `xla_sc_*` and `barna_core_*` — SparseCore / embedding

`xla_sc_*` (92 registered) is the SparseCore LLVM-backend compiler family: instruction fusion, latency-hiding scheduler, tile/SCS overlays, stack eliding, HBM optimization, and the `xla_sc_dump_*` debug surface. `barna_core_*` (61 registered) is the BarnaCore embedding-engine *runtime* family — HBM fraction budgets, row-sharding limits, partitioner objectives, profiler intervals. Both route into the TCE. Note `barna_core_*` carries no `xla_` prefix at all, yet is TCE-routed: the routing key is the recognized family token, not a leading `xla_`. The `xla_sc_*` codegen relationship is cross-referenced from [tpu-compilation-environment.md](tpu-compilation-environment.md).

### `xla_msa_*`, `xla_gf_*`, `xla_ior_*`, `xla_mosaic_*`, `xla_llo_*` — narrow TCE namespaces

| Prefix | Registered | Owner / role | Sink |
|---|---:|---|---|
| `xla_msa_*` | 22 | Memory-Space-Assignment (dedicated namespace: prefetch ratios, eviction/repack caps, IOR algorithm) | TCE |
| `xla_gf_*` | 14 | 6acc60406/v7x VMEM/MSA overrides (codename, see §3) | TCE |
| `xla_vf_*` | 16 | Viperfish VMEM/MSA overrides (codename, see §3) | TCE |
| `xla_ior_*` | 4 | "IOR" fast-mem round-trip MSA variant | TCE |
| `xla_mosaic_*` | 8 | Mosaic MLIR custom-kernel dialect controls | TCE |
| `xla_llo_*` | 1 | `xla_llo_annotation_lifecycle_strict_mode` (LLO annotation lifecycle) | TCE |

`xla_msa_*` is the dedicated MSA namespace that complements the `xla_tpu_*` MSA flags and the per-gen `xla_{gf,vf}_vmem_*` overrides; the three layers (`xla_msa_*` policy, `xla_tpu_*` scoped limits, per-gen overlay) compose at the consumer.

---

## 4. Runtime / Standalone Families (no proto)

These families register as plain `absl::Flag` globals read directly by the runtime — they land in neither `DebugOptions` nor the TCE.

### `megascale_*` — DCN collective runtime

150 registered flags. The Megascale data-center-network collective runtime: slice topology (`megascale_num_slices`, `megascale_slice_id`, `megascale_coordinator_address`), transport (`megascale_transport_type`, `megascale_grpc_num_channels`, `megascale_use_mtls_for_grpc`), watchdog/heartbeat (`megascale_enable_watchdog`, `megascale_heartbeat_{interval,timeout}_ms`), and abort policy (`megascale_error_reporter_abort_on_{error,hang}`). These are read by the DCN runtime at execution time, not the compiler; they are standalone flags with no proto backing.

### `tpu_*` — runtime / cache / driver

69 registered flags. The TPU runtime, compilation-cache, and driver surface: persistent compilation cache (`tpu_persistent_compilation_cache_location`, `tpu_program_cache_eviction_policy`), driver lifecycle (`tpu_deferred_deallocation`, `tpu_driver_callback_watchdog_timeout`, `tpu_link_up_check_timeout`), telemetry/coredump (`tpu_core_dump_directory`, `tpu_hbm_report_enable`), and the dangerous `DANGEROUS_tpu_runtime_abi_verification_disabled`. These are compile-time-irrelevant runtime knobs.

> **QUIRK —** `tpu_*` (runtime) and `xla_tpu_*` (compiler) are different families despite the shared `tpu` substring. `xla_tpu_foo` → TCE compiler field; `tpu_foo` → standalone runtime flag. The lowercase `libtpu_*` identifiers (`libtpu_init_utils`, `libtpu_lockfile`, `libtpu_version`, the `libtpu_lts_20260413_b_` build tag) are **not flags at all** — they are module / translation-unit names, and no `AbslFlagHelpGenForlibtpu_*` symbol exists for them. (CONFIRMED.)

### `tf_*` — TensorFlow-TPU bridge

20 registered flags (e.g. `FLAGS_tf_jf_*`). The legacy TensorFlow-TPU bridge surface, standalone flags read by the TF integration layer.

---

## 5. The Complete Routing Map

The full prefix → owner / routing / live-vs-inert table. Counts are registration-symbol-true (`AbslFlagHelpGenFor<prefix>`); INERT rows show string-count / zero-flags.

| Prefix | Registered | Subsystem owner | Sink | Live? |
|---|---:|---|---|---|
| `xla_tpu_*` | 909 | TPU compiler + runtime (master surface) | TCE | LIVE |
| `megascale_*` | 150 | Megascale DCN collective runtime | standalone | LIVE |
| `xla_jf_*` | 148 | Jellyfish — TPU XLA backend core | TCE | LIVE |
| `xla_*` (generic) | 112 | Generic XLA (scheduler/MSA/collective/dump) | `DebugOptions` | LIVE (290-field schema) |
| `xla_sc_*` | 92 | SparseCore LLVM compiler backend | TCE | LIVE |
| `tpu_*` | 69 | TPU runtime / cache / driver | standalone | LIVE |
| `barna_core_*` | 61 | BarnaCore embedding-engine runtime | TCE | LIVE |
| `xla_msa_*` | 22 | Memory-Space-Assignment namespace | TCE | LIVE |
| `tf_*` | 20 | TensorFlow-TPU bridge | standalone | LIVE |
| `xla_vf_*` | 16 | Viperfish per-gen VMEM/MSA | TCE | LIVE |
| `xla_gf_*` | 14 | 6acc60406/v7x per-gen VMEM/MSA | TCE | LIVE |
| `xla_mosaic_*` | 8 | Mosaic MLIR custom-kernel dialect | TCE | LIVE |
| `xla_hlo_*` | 5 | HLO scheduler (BRKGA) + schedule I/O | standalone* | LIVE (split) |
| `xla_ior_*` | 4 | IOR fast-mem round-trip MSA variant | TCE | LIVE |
| `xla_llvm_*` | 4 | LLVM/LLO ISA emitter controls | TCE/standalone | LIVE (split) |
| `xla_pf_*` | 1 | Pufferfish ND all-reduce | TCE | LIVE |
| `xla_llo_*` | 1 | LLO annotation lifecycle | TCE | LIVE |
| `xla_gpu_*` | **0** (~275 str) | (GPU backend — not on TPU) | `DebugOptions` proto-only | **INERT** |
| `xla_cpu_*` | **0** (~64 str) | (CPU backend — not on TPU) | `DebugOptions` proto-only | **INERT** |
| `xla_gl_*` | **0** (0 str) | — | — | **ABSENT** |

\* `xla_hlo_*` / `xla_llvm_*` are split: a registered-flag subset (standalone or TCE) plus a proto-only `DebugOptions` subset (see §2). The "Sink" cell names the live subset's sink.

> **NOTE —** the `(other / no std prefix)` 412-name bucket from the catalog (abseil / grpc / protobuf / OR-tools / cp_model library flags statically linked into libtpu) is intentionally omitted from this map — those are not XLA/TPU flags and route through their own libraries' registration, not the TPU config pipeline. They are settable through `LIBTPU_INIT_ARGS` only incidentally, as any absl flag is.

### Reading the map at a routing site

A reimplementation of the flag dispatcher tokenizes a `--name[=value]` argument and routes by the first recognized prefix token:

```c
function RouteFlag(name):                       // models the dispatch implied by the
    tok = SecondToken(name)                      // registration-site split (§1)
    if name starts "megascale_" or "tpu_"
       or name starts "tf_" or "barna_core_":
        return STANDALONE                        // FLAGS_<name>, read by runtime
    if name starts "xla_":
        switch tok:
          case "tpu","jf","sc","msa","gf","vf",  // codename + TPU namespaces
               "pf","ior","mosaic","llo":
              return TCE                          // OverrideTpuCompEnvByCmdLineFlags
          case "gpu","cpu":
              return INERT                        // proto field only, no flag — reject
          case "hlo","llvm":
              return SPLIT                         // some flags standalone/TCE, some proto-only
          default:                                 // plain xla_<concern>
              return DEBUG_OPTIONS                 // MakeDebugOptionsFlags field
    return STANDALONE                              // library flag (abseil/grpc/…)
```

This is the logical shape, not a single traced function: the actual binding is done generically by `absl::ParseCommandLine` against the pre-built `FLAGS_<name>` registry, and the proto routing happens after parse (the `DebugOptions` fields via the proto's own flag wiring, the TCE fields via `OverrideTpuCompEnvByCmdLineFlags`). The map above is what *determines* which `FLAGS_<name>` exists and what consumes it. (HIGH confidence on the routing classification; the dispatcher is reconstructed from the registration-site split, not a single byte-traced switch.)

---

## Related Components

| Component | Relationship |
|---|---|
| `MakeDebugOptionsFlags` (`…EPNS_`) | registers the generic `xla_*` flags onto `DebugOptions` |
| `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` | bridges TCE-routed families (`xla_tpu_*`, codenames, `xla_sc_*`, …) into the TCE |
| `SetFieldFromFlagString @ 0x1d73fcc0` | per-field writer used by the TCE bridge |
| `DefaultDebugOptionsIgnoringFlags @ 0x1e66a860` | the all-default baseline for the `xla_*` family |
| `AbslFlagHelpGenFor<name>` symbols | the authoritative per-family registration enumeration |

## Cross-References

- [overview.md](overview.md) — the three-layer pipeline; where this map sits (which proto each family lands in)
- [xla-flag-atlas.md](xla-flag-atlas.md) — the flat per-flag catalog; the full name list and subsystem keyword taxonomy this page indexes by prefix
- [flag-prefix-dispatch.md](flag-prefix-dispatch.md) — the `TpuVersion`-aware prefix-strip/select mechanism and the per-codename MSA overlay (`jf`/`vf`/`gf` resolution)
- [debugoptions-proto.md](debugoptions-proto.md) — `xla::DebugOptions`: the 290-field schema and the inert `xla_gpu_*`/`xla_cpu_*` carryover fields (only 2 fields are standalone-flag-wired; the earlier "94/111" split was superseded there)
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE master proto that the `xla_tpu_*` / codename / `xla_sc_*` / `xla_msa_*` families land in
- [registry-mediated-flags.md](registry-mediated-flags.md) — the reflection-mediated flag→field bridge (`TpuCompEnvReflection`) that serves all TCE families through one generic path
