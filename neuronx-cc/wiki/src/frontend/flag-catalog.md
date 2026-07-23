# CompileCommand Flag Catalog — the 147-Flag Surface

> *All flag literals, help strings, defaults, and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython module `neuronxcc/driver/commands/CompileCommand.cpython-310-x86_64-linux-gnu.so`). Other wheels differ; treat every address as version-pinned. Offsets in `[...]` are `.rodata` byte offsets read from the IDA string table / `_rodata.bin`.*

## Abstract

This page is the **complete user-facing option reference** for the `neuronx-cc compile` subcommand: every `--flag` the parser recognizes, with its argument type, default value, visibility class, and a concise (byte-verbatim where present) help string, in **one master catalog table**. It is the authoritative grammar — a reader who wants to know "does `--X` exist, what does it take, and will `--help` show it" should be able to answer that here without touching the binary.

The flag surface is registered in exactly one Cython function — `CompileCommand.__init__` (`__pyx_pw_..._CompileCommand_1__init__` @ file-off `0x36290`, ~30 564 decompiled lines, 717 `add_argument`/`--` references) — and its pipeline effects are assembled in `CompileCommand.buildPipeline` (`0x619d0`, ~16 616 lines), which constructs the `tensorizer_options` string and the Job list. Every `add_argument` call is intercepted by `_AddArgumentCallInterceptor` (in `driver/Arguments.cpython-310-...so`) and recorded in `_ArgumentRegistry` keyed by an **ArgKind** (`PUBLIC`/`INTERNAL`/`HIDDEN`/`EARG`/`Harg`); that ArgKind decides which `--help*` surface a flag appears on (see [3.9](flag-visibility-argkind.md) for the registry mechanics, [3.2](two-parser-architecture.md) for the two-parser architecture).

Extracting every `.rodata` string beginning `--` from the binary's string table yields **147 distinct `--` literals**. That is the "147-flag surface." Note what the count *includes*: a handful of flags appear twice — once as the bare flag and once as a defaulted `=value` form (e.g. `--internal-dma-qos-class-count` and `--internal-dma-qos-class-count=`) — and four `--no-*` negation companions are separate literals. So **147 ≠ 147 user-distinct features**; the *base-name* count (`=value`/`--no-`/short companions folded into their parent) is **~140**. Both numbers are reported, and the table below collapses the duplicate `=value` literals into one row while keeping the `--no-*` companions visible.

| | |
|---|---|
| **Registration site** | `CompileCommand.__init__` @ `0x36290` (one function, 717 `--`/`add_argument` refs) |
| **Pipeline effects** | `CompileCommand.buildPipeline` @ `0x619d0`; `validateArgs` @ `0x2d690` |
| **Visibility model** | `_ArgumentRegistry` keyed by `ArgKind {PUBLIC, INTERNAL, HIDDEN, EARG, Harg}` (`driver/Arguments.so`) |
| **Distinct `--` literals** | **147** (includes `=value` dup forms + `--no-*` companions) |
| **Base-name count** | ~140 (dup/`=value`/negation/short companions folded) |
| **Shared driver flags** | `--logfile` · `--logfile-verbose` · `--verbose` · `--version`/`-V` (in `CommandDriver.so`, on every subcommand) |
| **Short aliases** | `-O` → `--optlevel`, `-o` → `--output`, `-V` → `--version` (Cython `string_tab[111]/[613]`) |
| **Help surfaces** | `--help` (PUBLIC) · `--help-hidden` (+HIDDEN/EARG) · `--help-hidden-list` (+INTERNAL, dumps defaults) |

> **GOTCHA —** the "layers per module" knob is `--meta-module`, whose *metavar* is `M`. There is no `--M` flag; no such literal exists in `CompileCommand.so`. Tools that scrape metavars out of a usage line can manufacture one.

---

## How to read the catalog

**Columns.** `flag` (byte-verbatim literal) · `argtype` · `default` · `Vis` (visibility / ArgKind) · `help` (concise; quoted text is byte-verbatim `.rodata`, offset in `[]`).

**argtype legend.** `bool` = `store_true`/`store_false`; `ED` = `EnableDisableArgumentAction` pair (auto-registers a `--no-`/`--disable` companion); `NO` = `NoableArgumentAction` pair (auto `--no-<flag>`); `DEP` = `DeprecatedArgumentAction` (still parsed, warns); `int`/`float`/`str`/`list`; `enum(...)` = fixed choice set; `=N`/`=value` = literal stored *with* its default token (Cython idiom for a defaulted opt).

**Vis legend.** `PUB` = shown by `compile --help`. `HID` = shown only by `--help-hidden` (`HelpHiddenAction`). `INT` = shown only by `--help-hidden-list` (`HelpHiddenListAction`); by convention every `--internal-*` / `--enable-internal-*` flag. `EARG` = experimental ArgKind (the `--enable-experimental-*` / `--experimental-*` family; help-suppressed from `--help`, listed by `--help-hidden`). `DEP` = deprecated. Where the page writes `PUB(/HID)` the ArgKind byte was not resolvable to a single constant from the deeply-nested `__init__` decompilation (see honesty note).

**Cf (confidence).** `CERTAIN` = literal + help/dest both in `.rodata` and consistent, or a decompiled equality/SetAttr pins the default/effect. `HIGH` = literal + dest present; default/effect inferred from the adjacent dest plus its `buildPipeline`/`validateArgs` use, with no literal default constant. `MEDIUM` = literal present; type/default/visibility reasoned from naming convention and Action class alone. Cells whose value is computed at runtime from `--arch`/`--target` are tagged `(per-target)` and are **not** a static token.

> **NOTE — defaults honesty.** `(per-target)` / `(none)` defaults are *computed* in `validateArgs`/`buildPipeline` from `--arch`/`--target`; they are not single rodata constants and are never guessed here. Plain `bool` `store_true` flags default `False`. `ED`/`NO` flags expose a `--no-`/`--disable` companion.

---

## Master catalog — A–Z

### Shared top-level driver flags
Registered in `driver/CommandDriver.cpython-310-...so`, **not** in `CompileCommand` — they appear on *every* subcommand including `compile`. Verbatim from the embedded usage block (`0x37c02–0x37d4f`) + help strings (`0x37840–0x37940`).

| flag | argtype | default | Vis | help | Cf |
|---|---|---|---|---|---|
| `--logfile <logfile>` | str | `log-neuron-cc.txt` | PUB | *"Name of the logfile. (Default: %(default)s)"* — file sink for `neuronxlogger` via `setup_logfile_logging()` | CERTAIN |
| `--logfile-verbose` | enum(debug,info,warning,user) | `info` | PUB | *"Specify the verbosity level of the logfile. (Default: %(default)s)"* | CERTAIN |
| `--verbose` | enum(debug,info,warning,user) | `user` | PUB | *"Specify the verbosity level of console output. (Default: %(default)s)"* → stdout handler level | CERTAIN |
| `--version`, `-V` | flag | n/a | PUB | *"Print version information and exit"* — `VersionAction` → `getVersionString()`; prints `NeuronX Compiler version <...>`, exits | CERTAIN |
| `--fork-subcommand` | int | (internal) | HID | (no help / SUPPRESS) — re-exec helper for the `--fork` worker model | HIGH |

### CompileCommand flags A–F

| flag | argtype | default | Vis | help | Cf |
|---|---|---|---|---|---|
| `--allocator <value>` | enum(coloring, linear_scan) | `coloring` | HID | (no help) — selects walrus register/memory allocator; bad value raises *"Unsupported allocator= %s"* `[0x88c30]` | HIGH |
| `--arch <value>` | enum(inf1,inf2,trn1,trn1n,trn2,trn2n,trn3,sunda) | (from `--target`) | PUB | *"Target MLA architecture"* `[0x88c50]` — sets NeuronCore generation; gates LNC default (`arch!="sunda" or lnc==1` `[0x871a0]`) | CERTAIN |
| `--auto-cast <cast mode>` | enum(none, matmult, all) | `none` | PUB | *"Automatically cast FP32 operators to a lower-precision type for increased performance. (Default: %(default)s)"* `[0x866e0]`; → fp32-cast policy in `tensorizer_options` ([3.14](precision-flag-marshalling.md)) | CERTAIN |
| `--auto-cast-type <data-type>` | enum(fp16, bf16, tf32, fp8_e4m3) | `bf16` | PUB | *"When auto-cast mode is enabled, set the lower-precision data type to which the selected FP32 operations are cast. (Default: %(default)s)"* `[0x85280]` (`fp8_e4m3` canonicalizes to `fp8e4` in `buildPipeline`; `fp32r` reachable only via `--fast-math` macros) | CERTAIN |
| `--auto-compare-mismatch` | bool | False | HID | (no help) — golden-compare debug toggle; feeds `XLAInferGoldens` numerical-compare path | HIGH |
| `--cc-pipeline-tiling-factor=2` | int(`=N`) | `2` | HID | (no help; literal carries default `=2` `[0x88550]`) — collective-compute pipeline tiling factor | HIGH |
| `--cnn-training-model` | bool | False | HID | (no help) `[0x89510]` — marks graph as CNN-training; cousin of `--model-type cnn-training` | HIGH |
| `--cpu-arch <value>` | enum(skylake-avx512, skylake) | (host/native) | HID | *"Target CPU architecture"* `[0x88c70]` — CPU/simulator target for BIRSim/qemu host execution | CERTAIN |
| `--disable-concat-delinearizer` | bool | False | HID | (no help) `[0x88530]` — turns OFF the concat-delinearizer penguin pass | HIGH |
| `--disable-degraded-fusion` | bool | False | HID | (no help) `[0x88bd0]` — disables the "degraded fusion" heuristic | HIGH |
| `--disable-prefer-par-on-non-broadcast` | bool | False | HID | (no help) `[0x87120]` — disables the "prefer parallel on non-broadcast" layout/tiling heuristic (dest in BP use-list) | HIGH |
| `--disable-stream-shuffle` | bool | False | HID | (no help) `[0x88e10]` — disables stream-shuffle DMA optimization | HIGH |
| `--disable-stream-transpose-small` | bool | False | HID | (no help) `[0x87fa0]` — disables stream-transpose for small tensors | HIGH |
| `--disable-tiling-of-non-overlapping-mem-access` | bool | False | HID | (no help) `[0x870e0]` — disables tiling for non-overlapping memory accesses | HIGH |
| `--distribution-strategy <name>` | str/enum(fsdp, nemo, llm-training) | (none) | INT | *"Enable compiler optimizations for best performance with specific distribution strategy"* `[0x84b80]` — BP branches to FSDP/NeMo/LLM-training sharding ([Part 2.27](../arch/)) | CERTAIN |
| `--distribution-type-llm-training` | bool | False | INT | (no help) `[0x87f60]` — shorthand that sets `distribution_strategy=="llm-training"` (BP compares the literal) | CERTAIN |
| `--enable-arch-legalize` | bool | False | PUB(/HID) | (no help) `[0x89170]` — public face of `--enable-internal-arch-legalize` | HIGH |
| `--enable-bir-vnsplitter` | bool | False | HID | (no help) `[0x89010]`; dest `enable_bir_vnsplitter` `[0x88b90]` — enables BIR virtual-node splitter | HIGH |
| `--enable-ccop-compute-overlap` | bool | False | HID | (no help) `[0x88510]` — overlaps collective-compute (CCOP) with compute | HIGH |
| `--enable-dge` | ED | ON for levels `{io, scalar_dynamic_offset}` | PUB | *"Enable DGE [levels enabled by default: io, scalar_dynamic_offset]"* `[0x86240]` — Dynamic Generation Engine descriptors; fine-tune via `--internal-enable/disable-dge-levels` | CERTAIN |
| `--enable-explicit-bwd-convs-padding` | bool | False | PUB(/HID) | (no help) `[0x86f60]` — emits explicit padding for backward convolutions | HIGH |
| `--enable-fast-context-switch` | bool | False | PUB | *"Optimize for faster model switching rather than execution latency."* `[0x85660]` | CERTAIN |
| `--enable-fast-loading-neuron-binaries` | bool | False | PUB | *"This option will defer loading some weight constants until the start of model execution. This results in overall faster system performance when your application switches between models frequently on the same neuron core (or set of cores)."* `[0x856a4]` — conflicts with `--static-weights` `[0x85620]` | CERTAIN |
| `--enable-native-kernel` | bool | False | PUB(/HID) | (no help) `[0x89150]` — enables native-kernel lowering path | HIGH |
| `--enable-native-kernel-select-and-scatter` | bool | False | HID | (no help) `[0x86cc0]` — routes `select_and_scatter` through the native-kernel impl | HIGH |
| `--enable-parallel-queues` | bool | False | PUB | *"Enable parallel DMA queues to improve memory bandwidth utilization"* `[0x86140]` | CERTAIN |
| `--enable-p-to-pp-broadcast` | bool | False | HID | (no help) `[0x88970]` — partition→partition-pipeline broadcast optimization (dest in BP use-list) | HIGH |
| `--expand-batch-norm-training` | bool | False | HID | (no help) `[0x88650]` — expands BatchNorm-training ops into primitives in the Frontend | HIGH |
| `--expand-batch-norm-training-and-grad` | bool | False | HID | (no help) `[0x87e80]` — as above plus the gradient computation | HIGH |
| `--experimental-convolution-kernel-match` | bool/str | False | HID(EARG) | (no help) `[0x86c80]` — experimental conv kernel-matching | HIGH |
| `--experimental-unsafe-fp8e4m3fn-as-fp8e4m3` | bool | False | HID(EARG) | *"Convert fp8e4m3fn types to fp8e4m3"* `[0x864a0]` — unsafe; internal twin `--internal-experimental-unsafe-fp8e4m3fn-as-fp8e4m3` | CERTAIN |
| `--fast-math <cast-method>` | list (`nargs='+'`, 16-token macro set) | `[]` (empty) | INT | *"Controls how the compiler makes tradeoffs between performance and accuracy for fp32 operations. (Default: %(default)s)"* `[0x87380]` — master precision/relayout knob; macro set → `tensorizer_options` ([3.14](precision-flag-marshalling.md)) | CERTAIN |
| `--fork` | bool/int | False/0 | HID | (no help) — run compilation in a forked worker model (distinct from `--enable-internal-fork-backend`, which forks only the backend) | MEDIUM |
| `--framework <value>` | enum (XLA) | `XLA` (auto: *"XLA detected"* `[0x89e58]`) | PUB | *"Framework used to generate training model."* `[0x85d80]` — selects input-graph frontend (`XLAInterface`); HLO vs other ingest | CERTAIN |

**The precision-flag defaults.** The registered `argparse` defaults are the *off* settings: `--auto-cast` builds `default=` from the interned `none` token, and `--fast-math` builds `default=` from `PyList_New(0)` — an empty list. So the shipped behaviour is no casting until the user asks for it. `--fast-math` additionally carries `kind=ArgKind.INTERNAL` and `nargs='+'`.

> **GOTCHA —** `matmult` and `fp32-cast-matmult` describe the *behaviour selected once casting is enabled*, not the defaults. Documentation that leads with "auto-cast defaults to matmult" is describing the enabled mode.

*Anchors: `CompileCommand.__init__` @ `0x36290` — `--auto-cast` `default=none` at `0x39a7b`/`0x39a82`, `--fast-math` `PyList_New(0)` at `0x3a6ec`/`0x3a702`, `ArgKind.INTERNAL` fetch at `0x3a833`.*

**`--auto-cast-type` has four choices.** `fp8_e4m3` is a genuine direct choice alongside `fp16`/`bf16`/`tf32`, not a macro-only spelling: the literal is interned as `__pyx_n_u_fp8_e4m3` at `[0x8a560]`, and its per-choice doc line *"fp8_e4m3: floating point with 4 bits exponent and 3 bits mantissa"* `[0x8536e]` sits contiguous with the `fp16:`/`bf16:`/`tf32:` doc lines at `[0x85320]`/`[0x85346]`/`[0x85357]`, forming one four-choice description block. `buildPipeline` canonicalizes the user spelling `fp8_e4m3 → fp8e4` ([3.14](precision-flag-marshalling.md) §4.1). Only `fp32r` (the `tf32` canonical form) and the relayout-dtype variants are macro-only.

### CompileCommand flags H–M

| flag | argtype | default | Vis | help | Cf |
|---|---|---|---|---|---|
| `--help` | flag | n/a | PUB | *"Show this help message and exit"* `[0x87300]` — `HelpAction`; prints PUBLIC flags, exits | CERTAIN |
| `--help-hidden` | flag | n/a | HID | *"Print help for internal options"* `[0x87320]` — `HelpHiddenAction`; prints PUBLIC + HIDDEN (+EARG) | CERTAIN |
| `--help-hidden-list` | flag | n/a | HID | *"List internal options and default values"* `[0x85cc0]` — `HelpHiddenListAction`; dumps full registry (INTERNAL flags + defaults) | CERTAIN |
| `--images` | list[str]/positional | (none) | PUB | (no help) `[0x8a800]` — input image/graph file(s) to compile (fileGlobs ingest) | HIGH |
| `--inst-count-limit=100000000` | int(`=N`) | `100000000` | HID | (no help; literal carries default `=100000000` `[0x88630]`) — caps total instruction count (codegen safety) | HIGH |
| `--io-config <value>` | str (path/json) | (none) | PUB | (no help) `[0x8a370]` — supplies IO tensor layout/dtype config to the Frontend / StaticIOTranspose ([3.15](static-io-transpose.md)) | HIGH |
| `--jit-infergoldens` | bool | False | HID | (no help) `[0x89880]` — JIT-infer golden reference outputs (InferGoldens/XLAInferGoldens); cf. `--auto-compare-mismatch` | HIGH |
| `--jobs <N>` | int | (cpu_count-derived) | PUB | *"Limit number of simultaneous of processes and threads"* `[0x87340]` — distinct from `--num-parallel-jobs` | CERTAIN |
| `--layer-unroll-factor <N>` | int | (none / model-driven) | HID | (no help) `[0x892b0]`; `layer_unroll_factor_Used` `[0x88890]` tracks user-set — loop/layer unroll factor | HIGH |
| `--layout-transform-heuristic=mcts` | enum(`=value`) | `mcts` | HID | (no help; literal carries `=mcts` `[0x86880]`) — layout-transform search heuristic (Monte-Carlo Tree Search) | HIGH |
| `--lnc <N>` | int | (per-target: 2 on Trn2, else 1) | PUB | *"Number of NeuronCores per Logical Neuron Core. This option is only valid for Trn2 targets. On Trn2, the default is 2."* `[0x85be0]` — canonical short form of `--logical-nc-config`; deprecated alias `--num-neuroncores` warns *"DEPRECATED. Prefer --lnc instead."* `[0x88140]` | CERTAIN |
| `--logical-nc-config <N>` | int | (per-target: 2 on Trn2, else 1) | PUB | *"…On Trn2, the default is 2."* `[0x85be0]` — long form of `--lnc`; default-failure path *"Unrecognized architecture, unsure how to default --logical-nc-config"* `[0x85440]` | CERTAIN |
| `--meta-module <M>` | int | `0` (0 ⇒ whole graph = single module) | HID | *"Number of layers to be clustered into a module (partition). Set this to zero to treat the entire graph as a single module."* `[0x85b60]` — module size for partitioner / Modular Flow; `meta_module` read in BP (~15539) | CERTAIN |
| `--metrics-timestamp <value>` | str | (utcnow isoformat) | HID | (no help) `[0x896a0]` — timestamp stamped into `global_metric_store.json` `[0x88950]` | HIGH |
| `--model-specific-opt=unet` | enum(`=value`) | (unset) | HID | (no help; literal carries `=unet` `[0x88af0]`) — model-specific optimization presets (UNet/diffusion) | HIGH |
| `--model-type <str>` | enum(transformer, transformer-inference, cnn-training, unet-inference, vae-inference, operator-fusion, generic) | (auto / generic) | INT | *"Enable compiler optimizations for best performance with specific model types"* `[0x861a0]` — model-type optimization preset; `model_type` compared in BP | CERTAIN |

### CompileCommand flags N–Z

| flag | argtype | default | Vis | help | Cf |
|---|---|---|---|---|---|
| `--native-kernel-auto-cast=<value>` | str/enum(`=value`) | (empty) | HID | (no help; literal carries trailing `=` `[0x88ad0]`) — per-native-kernel auto-cast override | HIGH |
| `--native-to-custom-softmax` | bool | False | HID | (no help) `[0x88870]` — rewrites native softmax ops to the custom-softmax kernel | HIGH |
| `--neuroncore-pipeline-cores <N>` | int | (per-target) | PUB | *"Number of neuron cores to be used in NeuronCore Pipeline mode. (Default: %(default)s)"* `[0x859e0]` + disclaimer `[0x85a3d]` (not for data-parallel deployment) | CERTAIN |
| `--nn-executor <value>` | str/enum | (none) | HID | (no help) `[0x89f58]` — selects NN/input-graph executor backend (companion of `--tfpb`) | HIGH |
| `--no-enable-tritium-loopfusion` | bool (`NO` negation) | (positive default ON) | HID | (no help) `[0x881f0]` — `--no-` companion that DISABLES Tritium loop-fusion penguin pass | HIGH |
| `--no-internal-hlo-remat` | NO (negation) | (positive off) | INT | *"Do graph-level rematerialization in the Frontend"* `[0x862e0]` (shared) — negation of `--internal-hlo-remat`; sorts into N | CERTAIN |
| `--no-keep-remat-dma-transpose` | bool (`NO` negation) | (positive default) | HID | (no help) `[0x88430]` — disables keeping the rematerialized DMA-transpose | HIGH |
| `--non-local-tensors=<list>` | list/str(`=value`) | (empty) | HID | (no help; literal carries `=` `[0x89680]`) — names tensors to treat as non-local (cross-partition/core) | HIGH |
| `--no-run-pg-layout-and-tiling` | bool (ED/`NO` negation) | (positive default) | HID | (no help) `[0x88410]` — disables the PG (parallel-group) layout-and-tiling pass | HIGH |
| `--num-neuroncores <N>` | int (DEP) | (per-target) | DEP | *"DEPRECATED. Prefer --lnc instead."* `[0x88140]` — legacy spelling, still parsed (`DeprecatedArgumentAction`), maps to LNC | CERTAIN |
| `--num-parallel-jobs <N>` | int | (cpu-count-derived) | PUB | *"Number of parallel jobs during compilation"* `[0x859a0]`; `num_parallel_jobs_Used` `[0x88d10]` tracks user-set — distinct from `--jobs` | CERTAIN |
| `--option-preset <value>` | str/enum | (none) | HID | (no help) `[0x89c50]` — named bundle of pre-set options expanded into underlying flags | HIGH |
| `--optlevel <0\|1\|2\|3>` (`-O`) | enum("0","1","2","3") | `2` | PUB | *"Optimization level. The default setting -O2 provides the best balance between performance and compile time. -O1 enables the core performance optimizations…, while also aiming to minimize compile-time. -O3 can provide additional performance in some cases but may incur higher compile time and memory usage…"* `[0x857a0]`; extra -O3 note `[0x871e0]` | CERTAIN |
| `--output <file>` (`-o`) | str (path) | `file.neff` | PUB | *"Filename where compilation output (NEFF archive) will be recorded. (Default: %(default)s)"* `[0x85e80]` | CERTAIN |
| `--partitioner-opts=<opts>` | str(`=value`) | `--transformer` | HID | (no help; literal carries `=--transformer` `[0x881c0]`) — free-form options forwarded to the partitioner | HIGH |
| `--partitions-per-bank=<N>` | int(`=N`) | `64` | HID | (no help; literal carries `=64` `[0x88cf0]`) — SB partitions packed per bank (matches 128-partition / 2-bank geometry, [Part 2.x](../arch/sbuf-psum-geometry.md)) | HIGH |
| `--pipeline <stage[,stage...]>` | list[str] | (full pipeline) | HID | (no help) `[0x8a520]` — PARTIAL-PIPELINE selector; membership-tested in BP (~8789), gates pass-blocks; use with `--state` | HIGH |
| `--policy <value>` | str/enum | (none) | HID | (no help in CompileCommand; penguin `Options.so` carries *"Compatible mode for the old scheduler"*) — scheduler/optimization policy forwarded to penguin | HIGH |
| `--query-compute-placement` | bool | False | HID | *"Determine which compute engine to execute certain op/subgraph, by returning different return code"* `[0x86360]` — QUERY MODE; public face of `--internal-compute-engine-placement` | CERTAIN |
| `--rtol=<float>` | float(`=value`) | (test-harness default) | HID | (no help; literal carries `=` `[0x8a9d8]`; metavar `tolerance`) — relative tolerance for the golden-compare path (pairs with `--atol=`) | HIGH |
| `--run-id=<value>` | str(`=value`) | (generated) | HID | (no help) `[0x8a4e5]` — run identifier stamped into `global_metric_store.json`; pairs with `--submit-metrics` | HIGH |
| `--run-pg-layout-and-tiling` | ED | (off) | HID | (no help) `[0x88810]` — runs the PG layout-and-tiling pass; negation `--no-run-pg-layout-and-tiling` | HIGH |
| `--run-simulator-after=<stage>` | str/enum(`=value`) | `BirCodeGenLoop` | HID | (no help; literal carries `=BirCodeGenLoop` `[0x86820]`) — runs BIRSim immediately after the named stage | HIGH |
| `--scheduler <value>` | enum(none, experimental-loop-shift-left, experimental-loop-lsa) | `none` | HID | (no help; VA raises *"Unsupported scheduler= %s"* `[0x88c10]`) — selects experimental loop scheduler in penguin scheduling | CERTAIN |
| `--skip-pass=<PassName>` | list/str(`=value`, repeatable) | (none) | HID | (no help) — removes named pass(es); two baked-in literals `--skip-pass=MaskPropagation` `[0x88750]`, `--skip-pass=VDNSPrep` `[0x89430]` | HIGH |
| `--softmax-division-delay` | bool | False | HID | (no help) `[0x88cd0]` — delays the softmax division (numerator/denominator split) | HIGH |
| `--softmax-epsilon=<float>` | float(`=value`) | `-100000000.0` | HID | (no help; literal carries `=-100000000.0` `[0x883f0]`) — large-negative masking value (-1e8) for numerically-stable softmax | HIGH |
| `--spmd` | bool | False | HID | (no help; cf. *"enable spmd mode"* `[0x898e0]` for the experimental twin) — SPMD compilation mode; cf. `--enable-experimental-spmd` | HIGH |
| `--state in:<S>:out:<S>` | str | (full: in=initial, out=final) | HID | (no help) — START/END pipeline STATE so the compiler runs a sub-range; parsed by `getInStateFromCmdline` `[0x84040]`; works with `--pipeline` | CERTAIN |
| `--static-weights` | bool | False | PUB(/HID) | (no positive help; conflict *"Option %s cannot be used with --static-weights."* `[0x85620]`) — bakes weights into the NEFF; mutually exclusive with `--enable-fast-loading-neuron-binaries` | CERTAIN |
| `--submit-metrics` | bool | False | HID | (no help) — submits compile metrics to `global_metric_store.json` `[0x88950]`; pairs with `--run-id`/`--metrics-timestamp` | HIGH |
| `--target <instance-family>` | enum(trn1,trn1n,trn2,trn2n,trn3,inf1,inf2,sunda) | (required / from instance) | PUB | *"Name of Neuron instance family where the compiled model will be run."* `[0x85c60]` — primary target; `instanceToFamily` `[0x84730]` resolves family → MLA arch | CERTAIN |
| `--temp-dir <dir>` | str (path) | (generated working temp dir) | PUB(/HID) | (no dedicated help; log *"Intermediate files stored in %s, output in %s"* `[0x85d40]`) — directory for intermediate artifacts (SaveTemps, getWorkingDir) | HIGH |
| `--tensorizer-parallel-compile` | bool | False | HID | (no help) `[0x883d0]` — multi-process tensorizer compilation; complements `--num-parallel-jobs`/`--fork` | HIGH |
| `--tfpb <file>` | str (path) | (none) | HID | (no help) `[0x8a9a0]` — alternate input-graph format (tensorizer/TF protobuf) instead of HLO; pairs with `--nn-executor` | HIGH |
| `--upcast-all-to-fp32` | bool | False | PUB(/HID) | *"Upcast all ops to FP32"* `[0x88e50]` — public face of internal `--internal-upcast-all-to-FP32` | CERTAIN |
| `--vectorize-strided-dma` | bool | False | HID | (no help) `[0x88eb0]` — vectorizes strided DMA descriptors (DMA-codegen optimization) | HIGH |

> **NOTE — no W/X/Y/Z CompileCommand flags.** No `--flag` literal whose *base-name* begins W, X, Y, or Z exists in `CompileCommand.so` (cross-checked over the full literal set). `--verbose`/`--version` begin V but are registered in `CommandDriver`, catalogued above.

---

## The `--internal-*` family (auto-INTERNAL, surfaced by `--help-hidden-list`)

These flags acquire `ArgKind.INTERNAL` **automatically** because their dest starts with `"internal-"` — `_AddArgumentCallInterceptor.add_argument` (`Arguments.so` @ `0x19210`) calls `name.startswith("internal-")` (compared literal `__pyx_kp_u_internal` = `"internal-"` `[0x23d08]`); on match → `kind = ArgKind.INTERNAL`. They show only under `--help-hidden-list`, which dumps each INTERNAL flag *with its default*. **Default `False`** unless a value-arg.

| flag | argtype | default | help | Cf |
|---|---|---|---|---|
| `--internal-autotune` | bool | False | *"Internal flag used by autotune"* `[0x87fd0]` | CERTAIN |
| `--internal-autotune-config <path>` | str | (none) | *"Specify autotune config file path"* `[0x85520]` | CERTAIN |
| `--internal-autotune-subprocess` | bool | False | (no help) `[0x882d0]` — marks this process an autotune child | HIGH |
| `--internal-backend-options <str>` | str | (none) | (no help) `[0x888f0]` — free-form string forwarded to BIR/walrus backend | HIGH |
| `--internal-build-with-users` | bool | False | (no help) `[0x887d0]` — retain def-use ("with users") info | HIGH |
| `--internal-compiler-debug-mode` | bool/str | False | (no flag help; banner *"WARNING: Debug mode enabled via --internal-compiler-debug-mode"* `[0x853c0]`) | CERTAIN |
| `--internal-compiler-debug-mode-numerical` | bool | False | *"compile saves and logs information useful for debugging,and also for numerical correctness debugging"* `[0x84b00]` | CERTAIN |
| `--internal-compute-engine-placement` | bool/str | (off) | *"Determine which compute engine to execute certain op/subgraph, by returning different return code"* `[0x86360]` (twin of `--query-compute-placement`) | CERTAIN |
| `--internal-disable-birsim-validation` | bool | False | (no help) `[0x86b80]` — disables BIRSim validation pass | HIGH |
| `--internal-disable-birverifier-validation` | bool | False | (no help) `[0x86b40]` — disables the BIRVerifier validation job | HIGH |
| `--internal-disable-dge-levels <list>` | list/str | (none) | *"Specify different DGE levels to be turned OFF for dynamic DMA"* `[0x872c0]` | CERTAIN |
| `--internal-disable-fma-on-ios` | bool | False | *"Disable generating FMA operations on input or output tensors"* `[0x86320]` | CERTAIN |
| `--internal-disable-layer-det` | bool | False | *"Force partitioner to disable layer detection heuristic"* `[0x85e40]` | CERTAIN |
| `--internal-dma-qos-class-count[=N]` | int(`=N`) | (per-target) | *"Number of DMA QoS classes"* `[0x887f0]` — two literals: bare `[0x88290]` + `=`-form `[0x87e20]` | CERTAIN |
| `--internal-dma-qos-for-spill-code[=N]` | int/str(`=N`) | (none) | *"Specify the QoS class to assign to spill code"* `[0x87280]` — two literals: bare `[0x86b00]` + `=`-form `[0x84c20]` | CERTAIN |
| `--internal-dynamic-dma-scratch-size-per-partition <N>` | int | (per-target) | *"SB scratch size per partition for Dynamic DMA"* `[0x85560]` | CERTAIN |
| `--internal-enable-bir-debugger-launch-configuration` | bool | False | (no help) `[0x86a80]` — emits a BIR debugger launch config | HIGH |
| `--internal-enable-dge-levels <list>` | list/str | (adds to `default_dge_on_levels`) | *"Specify different DGE levels to be turned ON for dynamic DMA"* `[0x84c80]` | CERTAIN |
| `--internal-enable-dma-qos-labeling` | bool | False | *"Enable penguin to assign QoS classes to DMA instructions"* `[0x86100]` | CERTAIN |
| `--internal-experimental-unsafe-fp8e4m3fn-as-fp8e4m3` | bool | False | *"Convert fp8e4m3fn types to fp8e4m3"* `[0x864a0]` (internal alias of the public EARG twin) | CERTAIN |
| `--internal-force-all-uniq` | bool | False | *"Force partitioner to treat all modules as uniq."* `[0x85e00]` — disables module-dedup | CERTAIN |
| `--internal-hlo-analyze-schedule` | bool | False | (no help) `[0x88080]` — runs HLO schedule analysis | HIGH |
| `--internal-hlo-remat` (+ `--no-internal-hlo-remat`) | NO | (off) | *"Do graph-level rematerialization in the Frontend"* `[0x862e0]` | CERTAIN |
| `--internal-inject-error` | bool/str | (off) | *"Deliberately trigger error to as part of negative testing"* `[0x86420]` | CERTAIN |
| `--internal-interleave-kind <value>` | enum(dst_reduce, first_mm) | (none) | (no help) `[0x888d0]` — op-interleaving strategy in scheduling | HIGH |
| `--internal-log-additional-qor-metrics` | bool | False | (no help) `[0x86960]` — logs extra QoR metrics | HIGH |
| `--internal-num-neuroncores-per-sengine <N>` | int | (per-target) | (no help) `[0x86920]` — NeuronCores per sequencer-engine | HIGH |
| `--internal-num-semaphores-per-dma-queue <N>` | int | (per-target) | *"Number of semaphores allocated to a DMA queue"* `[0x85960]` | CERTAIN |
| `--internal-relaxed-order` *(via `--enable-internal-relaxed-order`)* | bool | False | (no help) — relaxed instruction ordering (see enable-internal family) | HIGH |
| `--internal-suppress-error` | bool/str | (off) | (no help) `[0x88b10]` — suppresses a named error (triage) | HIGH |
| `--internal-tensorizer-opt-level <N>` | int | (tracks `--optlevel`) | (no help) `[0x88060]` — overrides tensorizer opt level independent of `-O` ([3.10](opt-level-planes.md)) | HIGH |
| `--internal-upcast-all-to-FP32` | bool | False | *"Upcast all ops to FP32"* `[0x88e50]` — internal twin of `--upcast-all-to-fp32` | CERTAIN |

## The `--enable-internal-*` family (feature toggles; INTERNAL)

Same `"internal-"`-substring rule ⇒ `ArgKind.INTERNAL` (surfaced by `--help-hidden-list`). These are **opt-in switches for in-development backend/flow features**; off by default; more "productised" than raw `--internal-*` knobs. All `bool`, default `False`.

| flag | help | Cf |
|---|---|---|
| `--enable-internal-anti-dependence-reduction` `[0x86ee0]` | (no help) — anti-dependence reduction (dest `anti_dependency_analyzer`) | HIGH |
| `--enable-internal-arch-legalize` `[0x88120]` | (no help) — architecture-legalization pass | HIGH |
| `--enable-internal-bir-e2e-compilation` `[0x86e80]` | (no help) — end-to-end BIR compilation | HIGH |
| `--enable-internal-bir-linker` `[0x88690]` | (no help) — BIR linker flow | HIGH |
| `--enable-internal-call-graph` `[0x88670]` | *"Link modules using call-graph for Modular Flow"* `[0x85d00]` | CERTAIN |
| `--enable-internal-data-race-checker` `[0x86e40]` | *"Enable data race checker"* `[0x88a30]` | CERTAIN |
| `--enable-internal-dma-desc-reuse` `[0x87ee0]` | *"Reuse DMA descriptors for Modular Flow"* `[0x855e0]` | CERTAIN |
| `--enable-internal-fork-backend` `[0x88330]` | *"Fork backend in a separate process"* `[0x85dc0]` | CERTAIN |
| `--enable-internal-io-dge` `[0x88dd0]` | *"Enable IO Dynamic DMA"* `[0x890b0]` | CERTAIN |
| `--enable-internal-lnc-single-graph-compilation` `[0x86e00]` | *"Enabling this option will lead Penguin to generate a single BIR graph corresponding to the computation for all LNC cores, rather than the default approach of generating multiple graphs, one per each LNC core. This is currently an experimental feature which is turned off by default."* `[0x85ee0]` | CERTAIN |
| `--enable-internal-memory-analysis` `[0x86dc0]` | (no help) — memory analysis | HIGH |
| `--enable-internal-micro-modular-compilation` `[0x86d80]` | *"Enables very fast micro-module based compilation"* `[0x86000]` | CERTAIN |
| `--enable-internal-modular-compilation` `[0x86d40]` | *"Enables detection of modules and fast modular compilation"* `[0x860c0]` (referenced in BP) | CERTAIN |
| `--enable-internal-neff-wrapper` `[0x88310]` | (no help) — gates the `NeffWrapper` job ([Part 12](../formats/)) | HIGH |
| `--enable-internal-new-backend` `[0x884d0]` | *"Run new BIR codegen passes"* `[0x886f0]` (referenced in BP) | CERTAIN |
| `--enable-internal-relaxed-order` `[0x880e0]` | (no help) — relaxed instruction ordering | HIGH |
| `--enable-internal-walrus-replay-script` `[0x86d00]` | (no help) — emit a walrus replay script | HIGH |

## The `--enable-experimental-*` family (EARG; surfaced by `--help-hidden`)

Tagged **`EARG`** explicitly at the `add_argument` call (no `"internal-"` substring, so *not* auto-INTERNAL). Help-suppressed from `--help`; listed by `--help-hidden`. **Least stable** — bleeding-edge paths. All `bool`, default `False`.

| flag | help / note | Cf |
|---|---|---|
| `--enable-experimental-O1` `[0x88df0]` | experimental -O1 path (help shared with optlevel block) | HIGH |
| `--enable-experimental-bir-backend` `[0x870a0]` | experimental BIR backend | HIGH |
| `--enable-experimental-bir-partitioner` `[0x87060]` | dest `enable_experimental_bir_partitioner` `[0x85160]` | HIGH |
| `--enable-experimental-fallback-scheduler` `[0x87020]` | experimental fallback scheduler | HIGH |
| `--enable-experimental-legacy-partitioner` `[0x86fe0]` | dest `enable_experimental_legacy_partitioner` `[0x85120]` | HIGH |
| `--enable-experimental-no-split-dram` `[0x86fa0]` | *"Do not perform vnsplitting if it is from DRAM to DRAM"* `[0x862a0]` | HIGH |
| `--enable-experimental-sb-xfer` `[0x884f0]` | *"Use SB to SB transfers for inter-TPB tensor IO"* `[0x85400]` | CERTAIN |
| `--enable-experimental-spmd` `[0x889d0]` | *"enable spmd mode"* `[0x898e0]`; dest `enable_experimental_spmd` `[0x886d0]` | CERTAIN |
| `--enable-experimental-tensorized-scheduler` `[0x87f20]` | dest `enable_experimental_tensorized_scheduler` `[0x850a0]` | HIGH |

> **GOTCHA — `--no-enable-tritium-loopfusion` implies a positive default-ON flag.** The literal set contains the `--no-` companion but not a separate `--enable-tritium-loopfusion` literal in the A–Z table; the positive `--enable-tritium-loopfusion` defaults **ON**, and only its `NoableArgumentAction` negation `--no-enable-tritium-loopfusion` `[0x881f0]` is a distinct rodata literal. A reimplementer must register the positive flag (default `True`) plus the auto-generated negation.

---

## Visibility / ArgKind in one paragraph

A flag's help surface is decided by its **ArgKind**, set once at `add_argument` time and stored in `_ArgumentRegistry` as the tuple `"argument,kind,job,default,"`. The interceptor assigns `INTERNAL` automatically iff the dest starts `"internal-"`; otherwise `PUBLIC`, **unless** the call passes an explicit `kind=` kwarg (`HIDDEN`, `EARG`, `Harg`). `--help` shows PUBLIC only; `--help-hidden` adds HIDDEN + the experimental EARG/Harg args; `--help-hidden-list` filters to `kind == INTERNAL` and dumps each with its default (the registry dump). Stability ordering, most→least stable: **PUBLIC > HIDDEN (hand-tagged debug) > `--enable-internal-*` (feature toggles) > `--internal-*` (raw dev knobs) > `--enable-experimental-*`/EARG (bleeding edge) > `*unsafe*` experimental**; `Harg`-tagged args never appear on any help surface. Full mechanics: [3.9 — flag-visibility & ArgKind](flag-visibility-argkind.md).

## Choice / enum appendix (verbatim)

```text
--auto-cast        : none (DEFAULT) | matmult | all   (matmult is the behaviour when enabled, not the argparse default)
--auto-cast-type   : fp16 | bf16 (DEFAULT) | tf32 | fp8_e4m3   (fp8_e4m3→fp8e4 canonicalized; fp32r only via --fast-math)
--fast-math macros : all | none |       (argparse default = [] empty list; no macro is the default)
                     fp32-cast-all | fp32-cast-all-fp16 | fp32-cast-all-fp32r |
                     fp32-cast-all-fp8e4 | fp32-cast-matmult |
                     fp32-cast-matmult-bf16 | fp32-cast-matmult-fp16 |
                     fp32-cast-matmult-fp32r | fp32-cast-matmult-fp8e4 | fp32-cast-none |
                     fast-relayout | fast-relayout-fp32 | fast-relayout-fp32r |
                     fast-relayout-fp8e4 | no-fast-relayout                 (semantics: 3.14)
--arch / --target  : inf1 | inf2 | trn1 | trn1n | trn2 | trn2n | trn3 | sunda
                     (aliases inferentia/trainium; internal core_v4/core_v5; trn3pre pre-silicon)
--cpu-arch         : skylake-avx512 | skylake
--distribution-strategy : fsdp | nemo | llm-training
--model-type       : transformer | transformer-inference | cnn-training |
                     unet-inference | vae-inference | operator-fusion | generic
--allocator        : coloring (DEFAULT) | linear_scan
--scheduler        : none (DEFAULT) | experimental-loop-shift-left | experimental-loop-lsa
--internal-interleave-kind : dst_reduce | first_mm
--optlevel / -O    : "0" (-O0) | "1" (-O1) | "2" (-O2, DEFAULT) | "3" (-O3)   — NO 4..9
--logfile-verbose / --verbose : debug | info | warning | user
--pipeline/--state stages : Frontend | WalrusDriver | Backend | BIRSim  (+ BirCodeGenLoop)
```

### What `-O3` actually does

The `--optlevel` choice set is exactly the four Cython string-table tokens `"0"`, `"1"`, `"2"`, `"3"` (`string_tab[1..4]`); no `-O4`..`-O9` constants exist anywhere in `.rodata`, and the help text names only `-O1`/`-O2`/`-O3`. `-O` is the short alias (`string_tab[111] = ("-O", &__pyx_kp_u_O)`), with `-O0`/`-O1`/`-O2`/`-O3` as its argparse short spellings.

`-O3` is then **unconditionally rewritten to `-O2`**. The only guard is the equality test `_Pyx_PyUnicode_Equals(kp_u_3, optlevel)` (`buildPipeline` L15261); when it holds, `buildPipeline` sets `optlevel="2"` (L15279), sets `layer_unroll_factor=0` (L15288) and `layer_unroll_factor_Used=True`, and logs the USER advisory `kp_u_The_O3_setting_is_not_optimized`. There is no precondition and no further guarding `if`.

> **GOTCHA —** `-O3` is not a "degrade only if preconditions are unmet" path. Passing `-O3` always yields an `-O2` compile plus a zeroed layer-unroll factor. The level→knob map is in [3.10 — opt-level planes](opt-level-planes.md) §2.4.

---

## Evidence summary — the five strongest claims

Each is grounded in `CompileCommand.cpython-310-…so`'s string table and `.rodata`:

1. **147 distinct `--` literals.** The count includes the `=value` defaulted forms (`--internal-dma-qos-class-count=`, `--internal-dma-qos-for-spill-code=`) and four `--no-*` companions; the de-duplicated base-name count is ~140.
2. **No `--M`; `--meta-module` is the real flag.** No `--M` literal exists in the 147-set. `--meta-module` is present with the byte-verbatim help *"Number of layers to be clustered into a module…"*.
3. **`--optlevel` is 0–3, default 2, no 4–9.** The help string names only -O1/-O2/-O3, and no `4`–`9` choice tokens exist.
4. **Default-carrying literals are byte-exact.** `--partitions-per-bank=64`, `--softmax-epsilon=-100000000.0`, `--partitioner-opts='--transformer'`, `--run-simulator-after=BirCodeGenLoop`, `--inst-count-limit=100000000`, `--cc-pipeline-tiling-factor=2`, `--layout-transform-heuristic=mcts`, `--model-specific-opt=unet` — all present verbatim in the literal set. `--output`'s default `file.neff` is a rodata token; `--fast-math`'s default is the empty list built by `PyList_New(0)` @ `0x3a6ec`, not a macro token (the `fp32-cast-matmult` macro string exists in the choice set but is not the default).
5. **`--scheduler` choice set + validation.** `none | experimental-loop-shift-left | experimental-loop-lsa`, plus the `validateArgs` raise string *"Unsupported scheduler= %s"*, are all in `.rodata`.

## Cross-references

- [3.9 — flag visibility & ArgKind](flag-visibility-argkind.md) — the `_ArgumentRegistry`, `ArgKind` enum, `--help`/`--help-hidden`/`--help-hidden-list` mechanics, the `"internal-"`-prefix rule.
- [3.10 — optimization levels](opt-level-planes.md) — deep `-O0..-O3` → per-pass / `--walrus-passes` knob mapping and the `-O3→-O2` fallback.
- [3.14 — precision flags](precision-flag-marshalling.md) — `--fast-math` / `--auto-cast` / `--auto-cast-type` macro *semantics* (this page lists only the choice surface).
- [3.2 — the two-parser frontend](two-parser-architecture.md) — `InterceptingArgumentParser` vs the second parser and how the surface is split.
- [0.2 — the compile pipeline](../front/pipeline.md) — how `buildPipeline` turns these flags into the Job list / `tensorizer_options`.
- Part 14 flag appendix — the consolidated cross-tool flag index (walrus_driver `cl::opt` flags are a *distinct* surface).
