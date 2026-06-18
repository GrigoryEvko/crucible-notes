# Distribution-strategy seeding (`--distribution-strategy`)

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The flag branching lives in the Cython driver module `neuronxcc/driver/commands/CompileCommand.cpython-310-*.so`; the front-end sharding it seeds lives in `neuronxcc/starfish/bin/hlo2penguin` and `hlo-opt`. For `hlo2penguin`/`hlo-opt` `.text`, VA = file-off + `0x201000` and `.rodata` VA = file-off + `0x200000` (NOT VA == file-off). The cp311/cp312 wheels share this code. Other wheels differ — treat every address as version-pinned. Provenance D-AB08, D-AF01.*

## Abstract

`--distribution-strategy` is the **user-facing entry point that seeds Neuron's SPMD
sharding**. The user names a parallelism *preset* — `fsdp`, `nemo`, or `llm-training` —
and the driver, in one decompiled Cython function (`CompileCommand.buildPipeline`
@ `0x619d0`), translates that preset into a set of front-end compiler flags and
attribute mutations. Those flags then drive a *stock* OpenXLA Shardy (`mlir::sdy`)
mesh pipeline inside `hlo2penguin`, which sizes the device mesh's named axes
(data / tensor / pipeline parallel) and lowers them into the replica groups carried
by the emitted collectives. This page documents that translation: the CLI surface,
the per-strategy branch in `buildPipeline`, the `--spmd` / `enable_internal_spmd_opt`
gating, and how the seeded mesh becomes replica groups.

The split of responsibility is the thing to keep straight. The **branching is Neuron**:
the three strategy names, the `if distribution_strategy == "fsdp": layer_unroll_factor = 4`
logic, the `--distribution-type-llm-training` forwarding, the `--spmd`→`enable_internal_spmd_opt`
gate — all of that is Neuron driver code in `CompileCommand.so`. The **mesh machinery
downstream is stock**: once the flags reach `hlo2penguin`, the sharding is expressed
in `mlir::sdy` (`MeshAxisAttr`, `getReplicaGroups`, `convertToHloSharding`) exactly
as upstream OpenXLA defines it. Neuron contributes the *seeding* — which preset turns
on which flags, and the LNC mesh cardinality (`--lnc`) the mesh is sized against — not
the sharding algebra itself.

This page is deliberately the CLI→sharding *seed* page. The physical-LNC pinning op
`AwsNeuronLNCShardingConstraint`, which rides the stock `CustomCallPartitioner`
registry, is the subject of [13.9 (LNC sharding constraint)](lnc-sharding-constraint.md)
and is not re-derived here. The Shardy attr↔`HloSharding` change-of-basis is
[13.4](shardy-hlosharding-bridge.md); the factor algebra that produces the attrs is
[13.3](sharding-algebra.md); the full 147-flag CLI catalog is
[3.8 (flag catalog)](../frontend/flag-catalog.md).

For reimplementation, the contract is:

- The **CLI surface**: `--distribution-strategy {fsdp,nemo,llm-training}`,
  `--distribution-type-llm-training`, `--spmd` (`--enable-experimental-spmd`),
  `--lnc` / `--logical-nc-config` — their dests, defaults, and visibility.
- The **`buildPipeline` strategy-branch dispatch** — the equality ladder on
  `self.distribution_strategy` and what each branch mutates.
- The **`--spmd` gate** — when `"spmd"` is forwarded and when `enable_internal_spmd_opt`
  flips, keyed on the LNC count.
- The **flag → mesh-axes → replica-groups** path the seed drives in `hlo2penguin`.

| | |
|---|---|
| **Branch function** | `CompileCommand.buildPipeline` @ `0x619d0` (Cython, decompiled) |
| **Strategy dest** | `self.distribution_strategy` (`__pyx_n_s_distribution_strategy_2`) — `--distribution-strategy` literal @ `.rodata`, dest token @ `0x88bb0` |
| **Strategy values** | `fsdp` (`__pyx_n_u_fsdp`) · `nemo` (`__pyx_n_u_nemo`) · `llm-training` (`__pyx_kp_u_llm_training`) |
| **Comparison op** | `_Pyx_PyUnicode_Equals` per value (string-equality ladder) |
| **Forwarded-flags accumulator** | `self.tensorizer_options` (list appended per branch) |
| **LNC mesh cardinality** | `self.logical_nc_config` (`--lnc` / `--logical-nc-config`); Trn2 default 2, else 1 |
| **SPMD toggle** | `self.enable_experimental_spmd` (`--spmd`) → appends `"spmd"`, gates `enable_internal_spmd_opt` |
| **Downstream mesh** | stock `mlir::sdy` — `MeshAxisAttr`, `getReplicaGroups`, `convertToHloSharding` (in `hlo2penguin`) |
| **Provenance** | branching = **Neuron** (D-AB08 §3); mesh = **stock OpenXLA** (D-AB08 §3.3) |

> **NOTE —** the branch logic in §2 is from the *decompiled* Cython extension
> (real pseudocode, not skip-decompile). Tags: **CONFIRMED** = the flag literal +
> dest + the `_Pyx_PyUnicode_Equals` / `_Pyx_PyObject_Append` call site were read in
> the decompiled body; **STRONG** = disasm-context inference; **INFERRED** =
> cross-component reasoning. The mesh-machinery symbols (§3) are CONFIRMED present in
> `hlo2penguin`; the per-axis *degree* binding is INFERRED (not in one decodable struct).

---

## §1 — The CLI surface

### Purpose

A distribution strategy is named once, on the `neuronx_cc compile` command line, and
must be turned into concrete compiler behavior before any HLO is touched. The driver
exposes a tiny cluster of flags for this — one enum-valued preset selector, one boolean
shorthand, the SPMD master switch, and the LNC width that sizes the mesh. Everything
the rest of this page does is downstream of these four.

### The flags

The flag surface is owned by the [3.8 catalog](../frontend/flag-catalog.md)
(provenance D-AF01); reproduced here only as the input to the seeding logic. Every
literal and dest below is a byte-verbatim `.rodata` string in `CompileCommand.so`.

| Flag | Dest | Type / values | Default | Vis | Role in seeding |
|---|---|---|---|---|---|
| `--distribution-strategy` | `distribution_strategy` | enum `{fsdp,nemo,llm-training}` | none | INTERNAL | preset selector; branched in `buildPipeline` |
| `--distribution-type-llm-training` | (sets `distribution_strategy`) | bool | False | INTERNAL | shorthand that forces `distribution_strategy == "llm-training"` |
| `--spmd` (`--enable-experimental-spmd`) | `enable_experimental_spmd` | bool | False | HID (EARG) | help `"enable spmd mode"`; turns SPMD on |
| `--lnc` / `--logical-nc-config` | `logical_nc_config` | int | 2 on Trn2, else 1 | PUBLIC | LNC mesh cardinality (mesh size N) |

CONFIRMED facts (cross-checked against D-AF01):

- `--distribution-strategy` help string is verbatim *"Enable compiler optimizations
  for best performance with specific distribution strategy"*; dest token at `0x88bb0`;
  ArgKind INTERNAL (it is a tuning preset, surfaced only via `--help-hidden-list`).
- The three values `fsdp` / `nemo` / `llm-training` are the only ones compared in
  `buildPipeline` (the equality ladder in §2). They appear as the interned constants
  `__pyx_n_u_fsdp`, `__pyx_n_u_nemo`, `__pyx_kp_u_llm_training` (the `kp_u` prefix on
  `llm-training` marks it a *keyword-payload* constant, i.e. a string literal carrying
  a `-`, vs the `n_u` identifier-name constants).
- `--distribution-type-llm-training` (literal @ `0x87f60`) is *not* a separate strategy:
  it is a boolean shorthand whose effect is to set `distribution_strategy == "llm-training"`,
  so it folds into branch (C) below.
- `--spmd` is the public alias of `--enable-experimental-spmd` (literal @ `0x889d0`,
  help `"enable spmd mode"` @ `0x898e0`, dest `enable_experimental_spmd` @ `0x886d0`).
- `--lnc == --logical-nc-config` (help *"Number of NeuronCores per Logical Neuron Core.
  … On Trn2, the default is 2."* @ `0x85be0`), validated by the assert
  `args.arch != "sunda" or args.logical_nc_config == 1` @ `0x871a0` — `sunda`/Trn2 is
  the only arch that may carry LNC > 1.

> **CORRECTION — `--distribution-strategy` is NOT public.** A naive reading of the
> *"Enable compiler optimizations for best performance…"* help text (which reads like a
> user-facing knob) suggests a PUBLIC flag. It is registered INTERNAL: it is shown only
> by `--help-hidden-list`, not the default `compile --help`. The 3.8 catalog tags it
> INTERNAL on the dest evidence; this page does not contradict that. (CONFIRMED via
> D-AF01 row `--distribution-strategy`.)

---

## §2 — The strategy branch in `buildPipeline`

### Purpose

`CompileCommand.buildPipeline` (`0x619d0`) is the single place the driver assembles the
front-end argument list `self.tensorizer_options` (the flags forwarded to `hlo2penguin`
/ `hlo-opt`). Among the dozens of attr-gated appends it performs, a short ladder reads
`self.distribution_strategy` and, per matched preset, mutates `tensorizer_options` and/or
sibling attrs. This ladder is the entire translation from the named strategy to compiler
behavior; there is no second dispatch elsewhere.

### The dispatch ladder

The branches are independent equality tests (not an if/elif chain in the decompiled
body — each is its own `_Pyx_PyUnicode_Equals` site), but because `distribution_strategy`
holds exactly one value at most one fires. The pseudocode names the real call sites.

```c
// CompileCommand.buildPipeline @ 0x619d0  (decompiled Cython; CONFIRMED call sites)
// self.tensorizer_options is the forwarded front-end flag list (the accumulator).
// self.distribution_strategy is the --distribution-strategy value (or "" if unset).

void seed_distribution_strategy(CompileCommand *self) {

  PyObject *ds = self->distribution_strategy;        // GetAttr distribution_strategy

  // ---- branch (A): FSDP ------------------------------------------------------
  // @ ~py-line 3826 / 14013.  CONFIRMED.
  if (_Pyx_PyUnicode_Equals(ds, __pyx_n_u_fsdp)) {
      if (!self->layer_unroll_factor_Used)           // sentinel: user did not set --layer-unroll-factor
          self->layer_unroll_factor = 4;             // SetAttr int 4
      // FSDP itself is realized later by the neuron-fsdp / coalesce-fsdp HLO passes
      // (see §3); this branch only seeds the while-loop unroll factor.
  }

  // ---- branch (B): NeMo-Megatron --------------------------------------------
  // @ ~py-line 14092.  CONFIRMED (Append site present).
  else_if (_Pyx_PyUnicode_Equals(ds, __pyx_n_u_nemo)) {
      // mutates self->tensorizer_options (GetAttr tensorizer_options + Append):
      // a distinct preset list of front-end flags for NeMo-style parallelism.
      // The exact appended token(s) are an Append into tensorizer_options; the
      // specific string was not isolated to one decodable constant -> STRONG.
  }

  // ---- branch (C): LLM-training ---------------------------------------------
  // @ ~py-line 13255 / 4148.  CONFIRMED.
  else_if (_Pyx_PyUnicode_Equals(ds, __pyx_kp_u_llm_training)) {
      _Pyx_PyObject_Append(self->tensorizer_options,
                           "distribution-type-llm-training");   // forward the flag
      // i.e. --distribution-type-llm-training reaches hlo2penguin, which selects the
      // LLM-training mesh seeding (data/tensor/pipeline-parallel axis assignment).
  }
}
```

> **GOTCHA — the strategy name and the forwarded flag are not the same string.** The
> user types `--distribution-strategy llm-training`; the driver forwards
> `distribution-type-llm-training` (no leading `--` — `tensorizer_options` carries
> bare option names). The boolean `--distribution-type-llm-training` flag is the *same*
> seed reached the other way: it sets `distribution_strategy == "llm-training"` so branch
> (C) fires. Two CLI spellings, one front-end behavior. (CONFIRMED — D-AB08 §3.1/§3.2C.)

> **NOTE — FSDP seeds an unroll factor, not a mesh.** Branch (A) does *not* append a mesh
> or sharding flag. It sets `layer_unroll_factor = 4` (matching the HloPassOptions
> `while-loop-unroll-factor` knob, *"Specify the unroll factor for the while-loop"*),
> which shapes the loop the FSDP all-gather/reduce-scatter passes coalesce against. The
> FSDP transform proper is the `neuron-fsdp` / `coalesce-fsdp` HLO passes (§3), enabled
> through the same forwarded path, not in this branch.

### The `--spmd` gate

`--spmd` is a *separate* attr (`enable_experimental_spmd`) handled by its own block in
`buildPipeline`, independent of `distribution_strategy`. It is the explicit tie between
the LNC mesh size and SPMD activation.

```c
// CompileCommand.buildPipeline @ 0x619d0, ~py-line 14605.  CONFIRMED.
if (self->enable_experimental_spmd) {                    // --spmd / --enable-experimental-spmd
    _Pyx_PyObject_Append(self->tensorizer_options, "spmd");   // turn SPMD mode on (front-end)

    if (lnc_predicate(self->logical_nc_config)) {        // a comparison on the LNC count
        self->enable_internal_spmd_opt = true;           // SetAttr True  -> internal SPMD opt
    }
}
```

- `"spmd"` is appended to `tensorizer_options` unconditionally when `--spmd` is set —
  this is what activates the stock SPMD partitioner inside `hlo2penguin`. CONFIRMED.
- `enable_internal_spmd_opt` flips only when the LNC-count predicate holds.
  `logical_nc_config` (the `--lnc` width, §1) is the mesh cardinality N; the internal
  SPMD optimization is gated on it because SPMD partitioning is only meaningful when
  N > 1 (a single LNC core has nothing to shard across). The exact comparison operand
  is the `logical_nc_config` attr read at this site; the precise threshold is **STRONG**
  (the predicate exists and reads `logical_nc_config`; the literal it compares to was
  not isolated to a single constant).

> **QUIRK — two SPMD switches, different layers.** `--spmd` forwards `"spmd"` (the
> *front-end* SPMD mode) and *also* sets `enable_internal_spmd_opt` (a Neuron *internal*
> optimization toggle). A reimplementer must not collapse them: one is a forwarded
> front-end flag, the other a sibling driver attr read by later `buildPipeline` blocks.
> Both originate from the one `if self.enable_experimental_spmd:` test. (CONFIRMED.)

### The full seeding context

The distribution / SPMD branches are three appends among many; the same `buildPipeline`
pushes other front-end flags into `tensorizer_options` under their own attr predicates
(e.g. `internal-disable-fma-on-ios`, `disable-concat-delinearizer`). `tensorizer_options`
**is** the accumulator of forwarded front-end flags; the distribution-strategy branches
simply push their seeding tokens into it. This is why the seed is invisible after the
driver: by the time `hlo2penguin` runs, the strategy has already been reduced to a flat
list of front-end flags. (CONFIRMED — `_Pyx_PyObject_Append` on `tensorizer_options` at
each site.)

---

## §3 — From seed to mesh to replica groups

### Purpose

The seed produced in §2 is a set of front-end flags. This section follows them into
`hlo2penguin`, where they configure the Neuron HLO pass options and, ultimately, the
stock Shardy mesh that turns named parallelism axes into the replica groups carried by
the emitted collectives. The boundary is sharp: everything in §2 is Neuron driver code;
everything here is stock OpenXLA `mlir::sdy` *driven by* a Neuron flag set.

### The Neuron pass options the seed sets

The forwarded flags become `cl::opt` values on the Neuron HLO pass-options object
(`HloPassOptions` ctor @ `0x1f93480`, CONFIRMED `cl::opt` strings in `hlo2penguin`):

| Pass-option string | Help (verbatim) | Seeded by |
|---|---|---|
| `neuron-fsdp` | *"Transform according to Neuron implementation of FSDP."* | `--distribution-strategy fsdp` path |
| `coalesce-fsdp` | *"Coalesce FSDP."* | (same) — coalesces ag/rs |
| `run-collective-matmul` | *"Run the Neuron Collective Matmul"* | LLM-training / SPMD paths |
| `enable-partition-gather` | *"Replaces kGather op with params."* | partition path |
| `override_core_count` | *"Manually set core count"* / *"Number of logical Neuron cores"* | `--lnc` width |

The FSDP collectives are emitted and coalesced in `hlo-opt`, **not** `hlo2penguin`:
the strings `fsdp_all_gather` / `fsdp_reduce_scatter` (and *"Adding fsdp ag/rs to
schedule"*, *"coalesce_fsdp_all_gathers/reduce_scatters: new AG/RS"*) are present in
`hlo-opt` only (CONFIRMED — the same scheduling strings are absent from `hlo2penguin`).
So the FSDP path straddles two binaries: `hlo2penguin` carries the `neuron-fsdp` /
`coalesce-fsdp` *pass-option* declarations, while the actual ag/rs emission and
schedule-insertion happen in the `hlo-opt`-backed `Frontend` job. The unroll-factor
branch (A) seeded earlier shapes the while-loop these passes coalesce against.

### The stock Shardy mesh pipeline

The sharding itself is expressed via the stock XLA Shardy (`mlir::sdy`) mesh pipeline.
The whole symbol set is CONFIRMED present in `hlo2penguin`:

```text
  --distribution-strategy / --spmd  (driver, §2)
        │  seeds tensorizer_options
        ▼
  hlo2penguin HloPassOptions  (neuron-fsdp, override_core_count, spmd, ...)
        │
        ▼  STOCK mlir::sdy mesh pipeline
  "Sharding" custom-call ──ImportSdyShardings──► sdy::ShardingConstraintOp
        │   ("Converts a CustomCall with target name Sharding into a
        │    ShardingConstraintOp and ... ShardingGroup into a ShardingGroupOp.")
        ▼
  sdy MeshAttr / MeshAxisAttr   ← named axes: data / tensor / pipeline parallel
        │   axis sizes = parallelism degrees; product = LNC count N (override_core_count)
        ▼
  sdy::getReplicaGroups(AxisRefListAttr, MeshAttr)
        │   the mesh AXES → collective REPLICA GROUPS lowering
        ▼
  all-gather / reduce-scatter / all-to-all  (one replica group per mesh-axis slice)
        │   ApplyShardingConstraintsPass → ShardingConstraintToReshardPass
        ▼  ("Converts ShardingConstraintOp into ReshardOp.")
  xla::sdy::convertToHloSharding(TensorShardingAttr, MeshAttr) → xla::HloSharding
        (→ stock SPMD partitioner; the change-of-basis is owned by 13.4)
```

The mesh-axis → replica-group lowering is the answer to "how does a strategy flag become
replica groups": the named mesh axes (`data` / `tensor` / `pipeline`) are sized by the
parallelism degrees, their product equals the LNC count N (surfaced as
`override_core_count`), and `sdy::getReplicaGroups(AxisRefListAttr, MeshAttr)` lowers a
chosen axis (or axis subset) into the replica groups of each emitted collective. This is
**stock OpenXLA** machinery; the Neuron contribution is only *seeding* the mesh shape
(via the strategy preset + `--lnc`) and the FSDP/collective-matmul passes that emit the
collectives.

CONFIRMED supporting `mlir::sdy` symbols in `hlo2penguin` (twins present in `hlo-opt`):
`xla::sdy::convertToHloSharding(TensorShardingAttr, MeshAttr)` @ `0x2bc58f0`,
`xla::sdy::ShardyXLA::Run` @ `0x2d2a220` (hlo-opt twin `0x2b346b0`),
`sdy::getReplicaGroups(AxisRefListAttr, MeshAttr)`, `MeshAxisAttr`, `getOrderedAxisRefs`,
`createFullyManualComputation`, `mlir::sdy::createInsertExplicitReshardsPass`,
`mlir::sdy::createReshardToCollectivesPass`. The mesh *shape* itself is the stock
`auto_spmd_partitioning_mesh_shape` knob (CONFIRMED string in both binaries).

> **INFERRED — the axis-name → degree binding table.** The mesh *machinery* is CONFIRMED
> (the full `sdy` symbol set above). The exact per-strategy mapping from
> `data`/`tensor`/`pipeline` axis names to their degrees was **not** found in a single
> decodable struct — it is assembled from the forwarded flags and the parallelism
> configuration at runtime. A reimplementer should treat the *axis-to-replica-group
> lowering* as firm and the *specific degree assignment per preset* as the one piece to
> recover from runtime config, not from a static table.

### Where the Neuron-specific pin enters

The stock pipeline above emits stock `"Sharding"` custom-calls →
`sdy::ShardingConstraintOp`. Neuron additionally emits `AwsNeuronLNCShardingConstraint`,
a custom-call that pins an `HloSharding` to the *physical* LNC topology rather than the
abstract mesh. That op — its penguin printer, its ride on the stock
`CustomCallPartitioner` registry, and the front-end↔backend `lnc_splitter` contract — is
the subject of **[13.9 (LNC sharding constraint)](lnc-sharding-constraint.md)** and is
not re-derived here. The relationship is: this page's seed sets up the *mesh and the
stock sharding constraints*; 13.9 covers the Neuron op that *additionally pins* a
constraint to a specific LNC core.

---

## §4 — Provenance ledger

### Neuron vs stock

| Surface | Neuron or stock | Evidence |
|---|---|---|
| `--distribution-strategy` flag + values | **Neuron** | `CompileCommand.so` `.rodata` literals; D-AF01 |
| `buildPipeline` strategy-branch ladder | **Neuron** | decompiled `_Pyx_PyUnicode_Equals` sites; D-AB08 §3.2 |
| `--spmd` → `enable_internal_spmd_opt` gate | **Neuron** | decompiled `if self.enable_experimental_spmd`; D-AB08 §3.2D |
| `--lnc` LNC mesh cardinality | **Neuron** | `logical_nc_config` dest + `sunda` assert; D-AF01 |
| `neuron-fsdp` / `coalesce-fsdp` / `run-collective-matmul` passes | **Neuron** | `HloPassOptions` `cl::opt` strings @ `0x1f93480` |
| `fsdp_all_gather` / `fsdp_reduce_scatter` scheduling | **Neuron** | `hlo-opt` strings |
| `mlir::sdy` mesh pipeline (`MeshAxisAttr`, `getReplicaGroups`, `convertToHloSharding`) | **stock OpenXLA** | full `sdy` symbol set in `hlo2penguin`; D-AB06/13.4 |
| stock SPMD partitioner + reshard machinery | **stock OpenXLA** | D-AB01/02/04; 13.1–13.5 |

### Confidence summary

**CONFIRMED**
- `--distribution-strategy ∈ {fsdp,nemo,llm-training}`, all three compared via
  `_Pyx_PyUnicode_Equals` in `buildPipeline`.
- Branch (A) FSDP → `layer_unroll_factor = 4` if `!layer_unroll_factor_Used`.
- Branch (C) LLM-training → `_Pyx_PyObject_Append(tensorizer_options,
  "distribution-type-llm-training")`; the `--distribution-type-llm-training` boolean
  reaches the same branch.
- `--spmd` → append `"spmd"` + (LNC-count-gated) `enable_internal_spmd_opt = True`.
- `--lnc == --logical-nc-config`, Trn2/`sunda` default 2 (else 1),
  `args.arch != "sunda" or args.logical_nc_config == 1`.
- The `HloPassOptions` `cl::opt` set (`neuron-fsdp`, `coalesce-fsdp`,
  `run-collective-matmul`, `override_core_count`) and the full `mlir::sdy` symbol set
  in `hlo2penguin`.

**STRONG**
- Branch (B) NeMo mutates `tensorizer_options` (Append present); the specific token(s)
  appended were not isolated to one decodable constant.
- The `--spmd` LNC-count predicate reads `logical_nc_config`; the literal threshold it
  compares to was not isolated.

**INFERRED**
- The per-strategy mapping from `data`/`tensor`/`pipeline` axis *names* to their
  *degrees* — the mesh machinery is confirmed; the exact degree table is assembled at
  runtime, not held in one static struct.

**NOT TRACED / OPEN**
- The exact NeMo preset flag list (branch B body Append target).
- cp311/cp312 share the cp310 code; only cp310 was decoded.

---

## Cross-references

- **[3.8 — CompileCommand flag catalog](../frontend/flag-catalog.md)** — the
  full 147-flag CLI surface; the authoritative source for `--distribution-strategy` /
  `--lnc` / `--spmd` flag rows (provenance D-AF01). This page does not contradict it.
- **[13.3 — Sharding algebra](sharding-algebra.md)** — the `mlir::sdy` factor algebra
  (`OpShardingRule`, `ShardingProjection`) that produces the `TensorShardingAttr` the
  seeded mesh is filled with.
- **[13.4 — Shardy ↔ HloSharding bridge](shardy-hlosharding-bridge.md)** — the
  `convertToHloSharding` change-of-basis that turns the seeded sdy sharding into the
  `xla::HloSharding` the stock SPMD partitioner consumes.
- **[13.9 — LNC sharding constraint](lnc-sharding-constraint.md)** — the Neuron
  `AwsNeuronLNCShardingConstraint` custom-call that pins a constraint to the physical
  LNC topology; the front-end↔backend `lnc_splitter` contract.
