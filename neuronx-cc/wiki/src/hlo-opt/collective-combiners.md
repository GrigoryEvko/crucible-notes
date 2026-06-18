# AllReduce / ReduceScatter / AllGather Combiners & Threshold Model

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel, `neuronxcc/starfish/bin/hlo-opt`, BuildID `93dd8bd9bd4c697b`, not stripped). For `.text` and `.rodata` in this binary VA == file offset; addresses resolve directly in `disasm/` and `*_strings.json`. Other versions will differ.*

## Abstract

Three passes in the `hlo-opt` pipeline fuse sibling collectives into one larger collective so the runtime issues fewer, bigger NCCL-style operations: pass **#79 `all-reduce-combiner`** (`xla::hilo::NeuronAllReduceCombiner`), pass **#78 `reduce-scatter-combiner`** (`xla::hilo::NeuronReduceScatterCombiner`), and pass **#77 `all-gather-combiner`** (`xla::hilo::NeuronAllGatherCombiner`). All three are **thin Neuron subclasses over the stock XLA combining engine** `xla::CombineInstructionsByKey<KeyT>`. The engine is unmodified upstream XLA — its source path string `./xla/service/collective_combiner_utils.h` (@0x393c30) and the base-key builder's `xla/service/all_reduce_key.cc` (@0x238599) are both under `xla/service/`, whereas the three combiners live in `hilo/hlo_passes/neuron_*_combiner.cc`.

This is the page's thesis, and it survives adversarial checking: **the Neuron delta is ONLY the combine key**. The grouping loop, the reachability guard, the byte/count accounting, the two stop conditions (`"Combined count threshold reached."` @0x2b8118, `"Combined size threshold exceeded."` @0x37bc88), and the fusion rewrite are stock. What each Neuron combiner contributes is (a) a per-op **key tuple** that decides which collectives are compatible, and (b) the wiring of two `llvm::cl::opt` thresholds out of the Neuron `HloPassOptions` singleton into the stock engine's two `long` tail arguments. The keys differ per collective along exactly one extra axis: all-reduce keys on a `"dtype="+<PrimitiveType>` **string** suffix; reduce-scatter keys on the **scatter_dimension** (gated by `combine_by_dim`); all-gather keys on the **all_gather_dimension** as an `optional<long>` (gated by `combine_by_dim`). Everything else in each key — opcode, element type, domain id, the two collective bool flags, and the replica groups — is produced by the same shared base routine, `xla::GetAllReduceKey` (@0x20154f0) for AR/RS, and an inline structured builder for AG.

The thresholds reconcile across all three combiners to a single pair of defaults read from one `HloPassOptions` block: **count = 256** (`collective-combine-threshold-count`) and **bytes = 1 GiB = 0x40000000** (`collective-combine-threshold-in-bytes`). Two backing reports disagreed on which flag carried which number; the binary settles it (see the [Threshold Model](#threshold-model-the-two-numbers) §). The `combine_by_dim` default is **TRUE**, driven by `--collective-combine-by-dim`; the while-loop skip is **FALSE**, driven (non-obviously) by `--neuron-fsdp`.

For reimplementation, the contract is:

- The **stock engine boundary**: which code is upstream `CombineInstructionsByKey` and which is the Neuron subclass — and the single point of divergence (the key functor).
- The **three key tuples**, byte-for-byte: their element order, the shared base fields, and the one Neuron-specific discriminator each adds.
- The **threshold model**: the two `cl::opt` defaults (256 / 1 GiB), their `HloPassOptions` offsets, how the factory reads them, and the signed `<= 0` disable gate.
- The **`combine_by_dim` semantics** and its `--neuron-fsdp` aliasing for while-loop skipping.

| | |
|---|---|
| **Passes** | #77 `all-gather-combiner`, #78 `reduce-scatter-combiner`, #79 `all-reduce-combiner` |
| **Stock engine** | `xla::CombineInstructionsByKey<KeyT>` — AR @0x2012330, RS @0x1fc2780, AG @0x1f887e0 (one specialization per `KeyT`) |
| **Stock base key (AR/RS)** | `xla::GetAllReduceKey` @0x20154f0 (`xla/service/all_reduce_key.cc`) |
| **Neuron source files** | `hilo/hlo_passes/neuron_{all_reduce,reduce_scatter,all_gather}_combiner.cc` |
| **`HloPassOptions` ctor** | `xla::hilo::HloPassOptions::HloPassOptions()` @0x1eb9140 — both `cl::opt` defaults set here |
| **Thresholds (default)** | count = **256** (0x100); bytes = **1 GiB** (0x40000000) |
| **`combine_by_dim` (default)** | **TRUE** (0x1ebab7a: `mov byte [r12+0B78h], 1`) — AG/RS only |
| **`skip_while_loops` (default)** | **FALSE** (`--neuron-fsdp`, +0xC30 = 0) — AG/RS only |

---

## The Stock-vs-Neuron Split

### Purpose

Every claim on this page hangs off one structural fact: the combining machinery is upstream XLA and only the key is Neuron. This section pins that boundary so a reimplementer knows precisely what to write fresh (a key functor and a flag-wiring factory) versus what to lift unchanged from XLA's `collective_combiner_utils.h`.

### Entry Point

The three passes share an identical shape — a thin `Run` (or `Run → RunWithKeyCombiner`) wrapper that builds a `FunctionRef` over the Neuron key functor and tail-calls the stock engine driver, which loops computations and calls the stock grouper:

```text
NeuronAllReduceCombiner::Run        @0x1f8f990  (39 B)   ── load key fn-ptr + InvokeFunction thunk
  └─ xla::AllReduceCombiner::RunWithKeyCombiner @0x2013eb0  ── STOCK driver: gates + per-comp loop
       └─ xla::CombineInstructionsByKey<KeyT_AR> @0x2012330 ── STOCK grouper (KeyT ends in std::string)

NeuronReduceScatterCombiner::Run    @0x1fc49c0  (187 B)  ── build FunctionRef{CombineKey, thunk}
  └─ …::RunWithKeyCombiner          @0x1fc4300            ── driver (Neuron-owned; logs RS strings)
       └─ xla::CombineInstructionsByKey<KeyT_RS> @0x1fc2780 ── STOCK grouper

NeuronAllGatherCombiner::Run        @0x1f8add0  (1960 B) ── gate + iterate + 2 lambdas
       └─ xla::CombineInstructionsByKey<KeyT_AG> @0x1f887e0 ── STOCK grouper (KeyT = 6-tuple, no string)
```

> **NOTE —** the all-reduce path reuses XLA's own driver `xla::AllReduceCombiner::RunWithKeyCombiner` verbatim (the Neuron contribution is purely the key fn-ptr passed in `r8`), whereas reduce-scatter and all-gather carry their own `RunWithKeyCombiner`/`Run` driver (it emits Neuron-specific log strings and reads the `neuron-fsdp` while-skip flag). In all three cases the *grouper* — `CombineInstructionsByKey` — is stock. The driver is a wrapper; the grouper is the engine.

### Algorithm — the divergence is one `r8`/`FunctionRef`

The 39-byte all-reduce `Run` is the cleanest proof. It does nothing but install the Neuron key functor and forward:

```c
StatusOr<bool> NeuronAllReduceCombiner::Run(module, threads):   // @0x1f8f990
    r8 = &NeuronAllReduceCombineKey;        // @0x1f8f991 — the ONLY Neuron-specific operand
    r9 = &InvokeFunction<optional<KeyT_AR>(HloInstruction*, HloDomainMap&)>;  // absl trampoline
    return xla::AllReduceCombiner::RunWithKeyCombiner(module, threads,
                                                      FunctionRef{r8, r9});   // @0x1f8f9a9 tailcall
```

The `KeyT_AR` demangled from the `call` operand at 0x1f8f9a9 is, verbatim:

```text
optional< tuple< tuple<HloOpcode, PrimitiveType, long, bool, bool, vector<vector<long>>>,
                 std::string > >
```

The trailing `std::string` is the dtype slot. Stock XLA's own `xla::AllReduceCombiner::CombineKey` (@0x2010a70) fills that slot with the **empty string** (`char const(&)[1]` = the NUL byte @0x234779); `NeuronAllReduceCombineKey` fills it with `"dtype="+<element_type>`. **That single string is the entire Neuron delta for all-reduce.** Everything else in the tuple — opcode, `PrimitiveType`, domain id, two bools, replica groups — comes from the *shared* `xla::GetAllReduceKey`, which is upstream.

The all-gather engine call (@0x1f8af6f) makes the same boundary visible from the type system. Its instantiation is `xla::CombineInstructionsByKey<std::tuple<PrimitiveType, optional<long>, long, bool, bool, vector<vector<long>>>>` — a *different* `KeyT` (a structured 6-tuple, no string) but the *same* template. One engine, three `KeyT` specializations, three key functors. Nothing else differs.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronAllReduceCombiner::Run` | 0x1f8f990 | 39 B | install key fn-ptr → stock `RunWithKeyCombiner` | CERTAIN |
| `NeuronAllReduceCombineKey[abi:cxx11]` | 0x1f8fce0 | 516 B | base key + `"dtype="+PrimitiveType` string | CERTAIN |
| `xla::AllReduceCombiner::RunWithKeyCombiner` | 0x2013eb0 | 1481 B | **stock** driver: threshold gate + per-comp loop | CERTAIN |
| `xla::AllReduceCombiner::CombineKey[abi:cxx11]` | 0x2010a70 | 380 B | **stock** key: base + empty string `""` | CERTAIN |
| `xla::CombineInstructionsByKey<KeyT_AR>` | 0x2012330 | 6780 B | **stock** grouper + both stop conditions | CERTAIN |
| `NeuronReduceScatterCombiner::Run` | 0x1fc49c0 | 187 B | build FunctionRef → `RunWithKeyCombiner` | CERTAIN |
| `NeuronReduceScatterCombiner::RunWithKeyCombiner` | 0x1fc4300 | 1702 B | driver: gates + while-skip + per-comp loop | CERTAIN |
| `NeuronReduceScatterCombiner::CombineKey[abi:cxx11]` | 0x1fc4ce0 | 939 B | base key + `scatter_dimension` (or −1) | CERTAIN |
| `xla::CombineInstructionsByKey<KeyT_RS>` | 0x1fc2780 | 6748 B | **stock** grouper | CERTAIN |
| `NeuronAllGatherCombiner::Run` | 0x1f8add0 | 1960 B | gate + iterate + 2 key/combine lambdas | CERTAIN |
| `InvokeObject<lambda#5>` (AG CombineKey) | 0x1f8a530 | 1863 B | builds the AG 6-tuple key | CERTAIN |
| `xla::CombineInstructionsByKey<KeyT_AG>` | 0x1f887e0 | 1451 B | **stock** grouper | CERTAIN |
| `xla::GetAllReduceKey` | 0x20154f0 | 1728 B | **stock** base key (opcode/dtype/domain/flags/groups); shared AR + RS | CERTAIN |
| `NeuronCombiner::CombineAllGathers(Span,bool)` | 0x1f8b750 | 4257 B | AG fusion rewrite + dim normalisation | CERTAIN |
| `NeuronCombiner::FindMostFrequentGatherDim(Span)` | 0x1f88080 | 450 B | histogram-argmax over AG dims | CERTAIN |
| `HloPassOptions::HloPassOptions()` | 0x1eb9140 | 12600 B | constructs every `cl::opt`, incl. both thresholds | CERTAIN |

---

## The Three Combine Keys

### Purpose

The key functor is the whole Neuron contribution, so it is the heart of the page. Each combiner's key is `optional<tuple<…>>`; returning a disengaged optional excludes the instruction from grouping entirely. The shared truth is the base group key; the per-collective truth is the one extra discriminator.

### The shared base key — `xla::GetAllReduceKey` @0x20154f0

Both AR and RS keys start here. It returns `optional<tuple<HloOpcode, PrimitiveType, long, bool, bool, vector<vector<long>>>` and is upstream XLA (`xla/service/all_reduce_key.cc` @0x238599). The returned struct (filled via hidden-ptr `r14`):

| Field | Off | Source | Meaning |
|---|---|---|---|
| replica_groups | +0x00 | `CollectiveDeviceList::replica_groups()` | `vector<vector<long>>`, flattened |
| bool0 | +0x18 | — | `use_global_device_ids` / `constrain_layout` (carried opaquely) |
| bool1 | +0x19 | — | the other collective flag |
| domain id | +0x20 | `HloDomainMap::GetDomainMetadataId(inst)` | domain / channel id (`long`) |
| HloOpcode | +0x28 | `inst->opcode()` | the opcode (`int`) |
| PrimitiveType | +0x2c | `inst->shape().element_type()` | element dtype (`byte`) |
| engaged | +0x30 | 1 | optional has_value flag (0 ⇒ excluded) |

Three guards return a disengaged optional (instruction not combined):

```c
optional<BaseKey> GetAllReduceKey(inst, domain_map, bool include_groups):  // @0x20154f0
    if inst->HasControlDependencies():            return {};   // @0x2015521
    op = inst->opcode();                          // byte [inst+0x14]
    if op != 0x07 /*kAllReduce*/ && op != 0x57 /*kReduceScatter*/: return {};  // @0x201552a
    if !is_trivial_reduction(inst->to_apply()):                                // @0x2015578
        VLOG("Skipping due to non-trivial reduction function: ") + comp.ToString();  // @0x2b8380
        return {};
    key.replica_groups = include_groups ? {} : inst->device_list().replica_groups();  // @0x201567b
    key.bool0/bool1/domain/opcode/element_type = …;
    return optional{key};
```

> **NOTE —** `GetAllReduceKey` is opcode-polymorphic: it accepts both `kAllReduce` (0x07) and `kReduceScatter` (0x57), which is why one upstream routine serves both Neuron combiners. The AR combiner feeds it all-reduces; the RS combiner feeds it reduce-scatters. The `include_groups` argument controls whether replica groups are *omitted* from the key (XLA's "combine across groups" mode); the Neuron callers pass `false`, so replica groups **are** part of the key (CERTAIN).

### All-reduce — `NeuronAllReduceCombineKey` @0x1f8fce0

The extra discriminator is a `"dtype="+<name>` string. The element-type name is the canonical XLA `PrimitiveType` enum name produced by **protobuf reflection at runtime** (`NameOfEnum`), so it is not a static literal in the binary:

```c
optional<KeyT_AR> NeuronAllReduceCombineKey(inst, domain_map):   // @0x1f8fce0
    base = GetAllReduceKey(inst, domain_map, /*include_groups=*/false);  // @0x1f8fce1 (ecx=0)
    if !base.has_value(): return {};                                     // @0x1f8fd?? → res+0x50 = 0
    et   = inst->shape().element_type();                                 // @0x1f8fd50  PrimitiveType
    name = NameOfEnum(PrimitiveType_descriptor(), et);                   // @0x1f8fd5e  protobuf reflection
    dtag = StrCat( AlphaNum("dtype=", /*len=*/6), AlphaNum(name) );      // @0x1f8fd9a; "dtype=" @0x2530b8
    return optional{ tuple{ base, dtag } };
```

The literal `"dtype="` lives at 0x2530b8 with length 6 hardcoded (`mov [..], 6` @0x1f8fd78). **The Neuron delta vs stock is exactly this string**: stock's `CombineKey` (@0x2010a70) emits `""` (no dtype discrimination), so the Neuron variant additionally refuses to fuse all-reduces of differing element types into one combined op.

### Reduce-scatter — `CombineKey` @0x1fc4ce0

The extra discriminator is the **scatter dimension**, gated by `combine_by_dim`. The trailing string slot stays **empty** (the dtype is already inside the base tuple via `PrimitiveType`):

```c
optional<KeyT_RS> CombineKey(inst, domain_map, bool combine_by_dim, vector<ReplicaGroup>* groups):  // @0x1fc4ce0
    if inst->opcode() != 0x57 /*kReduceScatter*/:  goto empty;    // @0x1fc4d0c
    base = GetAllReduceKey(inst, domain_map, /*include_groups=*/false);   // @0x1fc4d2a
    if !base.engaged:                              goto empty;    // @0x1fc4d2f
    if !MatchReductionComputation(inst->to_apply()).has_value():  goto empty;  // @0x1fc4d78
    if groups != null && !neuron::HasMatchingReplicaGroups(inst, *groups): goto empty;  // @0x1fc4d9a
    dim = combine_by_dim ? inst->scatter_dimension() /*[inst+0x258]*/ : -1;   // @0x1fc4ed0
    return optional{ tuple{ base, dim, /*string=*/"" } };          // @0x1fc4ee3
  empty:
    out[+0x58] = 0; return {};
```

`KeyT_RS` = `optional<tuple< tuple<HloOpcode,PrimitiveType,long,bool,bool,vector<vector<long>>>, long /*scatter_dim or −1*/, std::string /*empty*/ >>`. When `combine_by_dim` is true only reduce-scatters with equal scatter dim group together; when false the dim is `−1` and the key is dim-agnostic.

### All-gather — key `lambda#5` @0x1f8a530

All-gather builds a structured 6-tuple inline (no shared `GetAllReduceKey` call, no trailing string). The extra discriminator is the **all_gather_dimension** as an `optional<long>`, gated by `combine_by_dim`, plus a tensor-parallel replica-group filter:

```c
optional<KeyT_AG> GetKey(inst):   // InvokeObject<lambda#5> @0x1f8a530
    combine_by_dim = pass[+0x18];                            // @0x1f8a608
    if !neuron::HasMatchingReplicaGroups(inst, tp_groups):   // @0x1f8a756  TP-group filter
        return {};                                           // @0x1f8a629 out+0x40 = 0
    ag             = Cast<HloAllGatherInstruction>(inst);    // @0x1f8a7d8
    element_type   = ag->shape().element_type();             // @0x1f8a7ea
    replica_groups = flatten(ag->device_list().replica_groups());  // @0x1f8a82d
    all_gather_dim = combine_by_dim ? optional<long>(ag->all_gather_dimension())  // @0x1f8a99b
                                    : nullopt;
    domain_id      = domain_map->GetDomainMetadataId(ag);    // @0x1f8a9ca
    constrain_layout      = byte[ag+0x210];                  // @0x1f8a9da
    use_global_device_ids = byte[ag+0x260];                  // @0x1f8a9e5
    return optional{ tuple{ element_type, all_gather_dim, domain_id,
                            constrain_layout, use_global_device_ids, replica_groups } };
```

`KeyT_AG` = `optional<tuple< PrimitiveType, optional<long> /*dim*/, long /*domain*/, bool, bool, vector<vector<long>> >>`. With `combine_by_dim` off, the `optional<long>` is `nullopt` and all-gathers of *any* dimension (sharing dtype/group/domain) hash together — the fusion rewrite then normalises mismatched dims (see [AG rewrite](#all-gather-rewrite--dimension-normalisation)).

### Per-collective key table (the centerpiece)

| Collective (pass) | Key tuple | count thr | byte thr | `combine_by_dim` | Engine | Delta vs stock |
|---|---|---|---|---|---|---|
| **all-reduce** (#79) | `<GetAllReduceKey base>, "dtype="+PrimitiveType` | **256** | **1 GiB** | n/a (no dim) | STOCK `CombineInstructionsByKey` + STOCK driver | **+ dtype string** (stock uses `""`) |
| **reduce-scatter** (#78) | `<GetAllReduceKey base>, scatter_dim\|−1, ""` | **256** | **1 GiB** | **TRUE** | STOCK `CombineInstructionsByKey` | **+ scatter_dim** (gated) |
| **all-gather** (#77) | `PrimitiveType, optional<dim>, domain_id, bool, bool, replica_groups` | **256** | **1 GiB** | **TRUE** | STOCK `CombineInstructionsByKey` | **+ all_gather_dim** + TP-group filter |

> **GOTCHA —** the three keys look structurally different (string suffix vs middle `long` vs 6-tuple), but the engine is the *same template* `xla::CombineInstructionsByKey<KeyT>`, just instantiated on three `KeyT` types. A reimplementation that writes three separate grouping engines has misread the binary: write one generic grouper templated on `KeyT`, and three key functors. The compatibility predicate is `KeyT::operator==` via the hash set — nothing more.

---

## Threshold Model (the two numbers)

### Purpose

The thresholds are the only tunables. Both are `llvm::cl::opt<long>` fields of the Neuron `HloPassOptions` singleton, constructed in `HloPassOptions::HloPassOptions()` @0x1eb9140. The per-pass factory reads them out and passes them to the combiner ctor; the combiner forwards them to the engine's two `long` tail arguments. This section pins the defaults, their offsets, and — critically — which flag carries which number, because two backing reports disagreed.

### The default-value evidence (binary-resolved)

The decisive evidence is the adjacency of each `cl::opt` argstr to its initializing `mov` immediate inside `HloPassOptions::HloPassOptions()`:

```asm
0x1eba724  mov esi, offset aCollectiveCombineThresholdInBytes   ; "collective-combine-threshold-in-bytes"
0x1eba7d5  mov qword ptr [r12+9F8h], 40000000h                  ;  in-bytes default = 0x40000000 = 1 GiB
0x1eba8fe  mov esi, offset aCollectiveCombineThresholdCount     ; "collective-combine-threshold-count"
0x1eba9af  mov qword ptr [r12+0AB8h], 100h                      ;  count    default = 0x100 = 256
0x1ebab7a  mov byte  ptr [r12+0B78h], 1                         ;  combine-by-dim default = TRUE
0x1ebacbe  mov byte  ptr [r12+0C30h], 0                         ;  neuron-fsdp default = FALSE
```

So the offset → flag → default map is unambiguous:

| Offset | Flag (verbatim) | argstr @ | Type | Default | → pass field |
|---|---|---|---|---|---|
| +0x9F8 | `collective-combine-threshold-in-bytes` | 0x34bd38 | long | **0x40000000 = 1 GiB** | +0x08 |
| +0xAB8 | `collective-combine-threshold-count` | 0x3b7270 | long | **0x100 = 256** | +0x10 |
| +0xB78 | `collective-combine-by-dim` | 0x228b88 | bool | **1 (TRUE)** | +0x18 (AG/RS) |
| +0xC30 | `neuron-fsdp` | 0x21cc9e | bool | **0 (FALSE)** | +0x19 (AG/RS) |

> **CORRECTION (D-B10) —** the D-B10 backing report transposed the two threshold *labels*: it tabulated +0x9F8 as `collective-combine-threshold-count` = 1<<30 and +0xAB8 as `…-in-bytes` = 256, and accordingly named the all-gather ctor parameters `(count, bytes)`. The binary refutes the label swap: the `cl::opt` whose argstr is `"…-in-bytes"` (0x1eba724) is the one set to **0x40000000** at +0x9F8, and the argstr `"…-count"` (0x1eba8fe) is set to **0x100** at +0xAB8. The numeric values (1 GiB and 256) were correct in all three reports; only D-B10's flag↔number pairing was reversed. **in-bytes = 1 GiB at +0x9F8; count = 256 at +0xAB8** (CERTAIN). D-B08/D-B09 had the pairing right.

> **CORRECTION (D-B08) —** D-B08 reported `combine-by-dim` default = **false**, reading the option-storage flags word at +0xB98. The value byte is at +0xB78, and the binary sets it to **1** (`mov byte [r12+0B78h], 1` @0x1ebab7a). The default is **TRUE** (CERTAIN), matching D-B09 and D-B10. (D-B08 also did not observe the by-dim flag because the all-reduce combiner does not consume it — see the QUIRK below.)

### Factory wiring — reading the flags into the ctor

Each pass's registrar `_M_invoke` lambda reads the four fields out of `*[rsi]` (the `HloPassOptions` singleton) and passes them positionally. The all-gather factory (@0x1e70700) is representative; the all-reduce factory (@0x1e70600) loads only the two long thresholds (all-reduce has no dim/while-skip):

```asm
; RegisterNeuronAllGatherCombiner::_M_invoke @0x1e70700
0x1e7071c  movzx r8d, byte ptr [rax+0C30h]   ; arg4 = neuron-fsdp        (→ skip_while_loops, +0x19)
0x1e70724  mov   r14, [rax+9F8h]             ; arg1 = in-bytes threshold (1 GiB) → rsi → field +0x08
0x1e7072b  mov   r15, [rax+0AB8h]            ; arg2 = count threshold    (256)   → rdx → field +0x10
0x1e70732  movzx ebx, byte ptr [rax+0B78h]   ; arg3 = combine-by-dim     (TRUE)  → cl  → field +0x18
0x1e70754  call  NeuronAllGatherCombiner::NeuronAllGatherCombiner(long,long,bool,bool)
```

> **QUIRK —** the factory loads **`r14 = [+0x9F8]` (in-bytes) into `rsi` = ctor arg1**, and **`r15 = [+0xAB8]` (count) into `rdx` = ctor arg2**. Combined with the offset→flag map above, this means the combiner field **+0x08 holds the byte threshold (1 GiB)** and **+0x10 holds the count threshold (256)** — for *all three* combiners (the AR factory @0x1e70617/0x1e7061e does the same two loads). Mangled ctor signatures read `(long,long,bool,bool)` with the args generically named; do not infer "count first" from the parameter order — the binary's load order fixes byte-first. This is the root cause of the D-B10 transposition.

### Pass-object layout (all three identical)

Both Neuron ctors store an identical 5-field object (the AR object is the same minus the two bools — it uses the stock `AllReduceCombiner(long,long)` ctor for field init, then the factory swaps in the Neuron vtable):

```text
+0x00  vtable
+0x08  long  combine_threshold_in_bytes   = arg1 (rsi)  ← [opts+0x9F8] = 1 GiB
+0x10  long  combine_threshold_count      = arg2 (rdx)  ← [opts+0xAB8] = 256
+0x18  bool  combine_by_dim               = arg3 (cl)   ← [opts+0xB78] = TRUE   (AG/RS only)
+0x19  bool  skip_while_loops             = arg4 (r8b)  ← [opts+0xC30] = neuron-fsdp = FALSE  (AG/RS only)
+0x20  qword = 0  (empty cache/container field)
```

Disasm proof (AG ctor @0x1f85fc0, RS ctor @0x1fc0f10 are byte-identical bar the vtable immediate): `mov [rdi+8],rsi; mov [rdi+10h],rdx; mov [rdi+18h],cl; mov [rdi+19h],r8b; mov [rdi+20h],0`.

> **NOTE —** `skip_while_loops` is sourced from `--neuron-fsdp`, *not* a dedicated flag. While-body computations are skipped only when FSDP transform mode is enabled. This dual-use is the single most non-obvious wiring in the family: a reimplementer who expects a `--skip-while-loops` flag will not find one.

---

## The Stock Engine — `CombineInstructionsByKey<KeyT>`

### Purpose

This is the unmodified upstream grouper. A reimplementer writes it once and instantiates it on each `KeyT`. It is documented here only to the depth needed to reproduce its threshold gate and stop conditions — its body is stock and the per-op byte formula is not byte-traced (see gaps).

### Algorithm

```c
StatusOr<bool> CombineInstructionsByKey<KeyT>(comp, key_fn, combine_fn,
                                              long max_count, long max_bytes):   // engine
    groups = flat_hash_map<KeyT, set<HloInstruction*>>;
    for inst in comp.MakeInstructionPostOrder():
        key = key_fn(inst);                       // the Neuron key functor
        if !key.has_value(): continue;            // excluded (control deps / wrong opcode / TP filter / …)
        grp = groups[key];
        // VLOG "Considering HLO " <inst> " with current set size of " <n>
        if reachability_conflict(inst, grp):      // HloReachabilityMap — would create a cycle
            VLOG("Instruction is reachable."); flush(grp);   // start new set
        if grp.count + 1 > max_count:                        // signed compare
            VLOG("Combined count threshold reached."); flush(grp);   // @0x2b8118
        else if grp.bytes + ByteSizeOf(inst->shape()) > max_bytes:
            VLOG("Size " <sz> " above threshold.");                  // @0x23094f / @0x23fda1
            VLOG("Combined size threshold exceeded."); flush(grp);   // @0x37bc88
        grp.add(inst);    // VLOG "Adding instruction to set."
    for grp in groups: if grp.size() >= 2: combine_fn(grp);   // build one fused collective
```

The two stop strings — `"Combined count threshold reached."` (@0x2b8118) and `"Combined size threshold exceeded."` (@0x37bc88) — are **shared across all three** grouper instantiations (the same `.rodata` addresses appear in the AR, RS, and AG engine bodies), which is itself evidence the engine is one template, not three copies.

### The driver gate (shared shape)

Each driver gates on both thresholds being strictly positive (signed `<= 0`), then bails if the module contains a layout-constrained collective of the relevant opcode, then loops non-fusion computations:

```c
StatusOr<bool> RunWithKeyCombiner(module, threads, key_fn):
    if this->in_bytes (@+0x08) <= 0 || this->count (@+0x10) <= 0:    // AG: cmp [rbx+8],0 jle / cmp [rbx+10h],0 jg
        VLOG("Skip … because the threshold is zero"); return false;  // @0x1f8ae1b (AG)
    if hlo_query::ContainsLayoutConstrainedCollective(module, opcode):  // AR=0x07, RS=0x57, AG=0x04
        return false;
    for comp in module.MakeNonfusionComputations(threads):
        if this->skip_while_loops (@+0x19) && comp.GetUniqueCaller(kWhile=0x79):  // AG/RS only
            VLOG("Computation is a while body and skip_while_loops is true…"); continue;  // @0x34c020
        changed |= CombineInstructionsByKey<KeyT>(comp, key_fn, combine_fn,
                                                  /*count=*/ this->[+0x10],   // push [rbx+10h] @0x1f8af23
                                                  /*bytes=*/ this->[+0x08]);  // push [rbx+8]   @0x1f8af30
    return changed;
```

> **GOTCHA —** the gate is **signed `<= 0`**: setting *either* threshold to 0 or negative disables combining entirely (`jle`/`jg` at 0x1f8ae20/0x1f8ae27 in the AG Run). A reimplementation using unsigned comparisons would treat a 0 threshold as "combine nothing extra" rather than "do not run", silently diverging. Also note the engine's tail args are pushed `count` first (`push [rbx+10h]`) then `bytes` (`push [rbx+8]`), matching the engine signature `(…, long count, long bytes)`.

> **NOTE —** the all-gather `Run` logs `"Running AllGatherCombiner with threshold of " << [this+8] << " bytes"` — it prints the **byte** field (+0x08) and labels it "bytes", which is correct (the in-bytes threshold lives at +0x08). The cosmetic confusion is only in reports that mis-attributed +0x08 to count.

---

## All-Gather Rewrite & Dimension Normalisation

### Purpose

Only all-gather has a non-trivial rewrite beyond operand concatenation, because `combine_by_dim=false` lets all-gathers of *different* dimensions group together — they must be normalised to one dimension before fusion. This is Neuron-specific (`NeuronCombiner::CombineAllGathers` @0x1f8b750, source `hilo/hlo_passes/neuron_all_gather_combiner.cc`), but it is the *combine_fn*, not the engine.

### Algorithm

```c
Status CombineAllGathers(Span<HloInstruction*> group, bool combine_by_dim):   // @0x1f8b750
    if group.size() <= 1: return OkStatus();
    most_frequent_dim = FindMostFrequentGatherDim(group);     // @0x1f88080 histogram-argmax
    for ag in group:
        RET_CHECK ag->shape().IsArray();                      // "hlo->shape().IsArray()" @0x26a790
        RET_CHECK ag->opcode() == kAllGather (4);             // @0x3dad50
        RET_CHECK ag->operand_count() == 1;                   // "hlo->operand_count() == 1" @0x25309e
        operands.push_back(ag->operand(0));
        if combine_by_dim:
            RET_CHECK ag->all_gather_dimension() == most_frequent_dim;  // "@0x295820"
        else if ag->dim != most_frequent_dim:
            operand = Bitcast(PermuteDimensions(perm: ag_dim→most_frequent_dim, operand_shape));
    combined = CreateAllGather(MakeTupleShape(shapes), operands,
                               /*dim=*/most_frequent_dim, group[0]->replica_groups(),
                               /*constrain_layout=*/false, group[0]->channel_id(),
                               /*use_global_device_ids=*/ag0->[+0x260]);
    for i, ag in enumerate(group):
        gte = CreateGetTupleElement(combined, i);
        replacement = (ag needed a permute) ? Bitcast(PermuteDimensions(inverse_perm, gte)) : gte;
        comp->ReplaceInstruction(ag, replacement);
    return OkStatus();
```

`FindMostFrequentGatherDim` (@0x1f88080) is a histogram-argmax over the group's `all_gather_dimension` values, with a validity clamp: if the winning index is `>= min_rank` (the smallest tensor rank in the group) it resets to 0 (`cmovge` @0x1f881ef). The combined all-gather runs along that one dimension; mismatched members are transposed in (via `PermuteDimensions`+`Bitcast`) and transposed back out of the GTE.

> **QUIRK —** the `RET_CHECK !combine_by_dim || ag->all_gather_dimension() == most_frequent_dim` (@0x295820) fires **only on the `combine_by_dim==true` path**. With the default `combine_by_dim=true`, the key already partitions by dimension, so every member of a group shares `most_frequent_dim` and the check is a tautology guard. The transpose-normalisation branch is reachable only when `--collective-combine-by-dim` is explicitly disabled. (`combine_by_dim` semantics in the rewrite: MED on the exact permutation index math @0x1f8c1de; CERTAIN on the branch structure.)

---

## Adversarial Self-Verification

The five strongest claims, re-checked against the binary:

1. **count = 256, bytes = 1 GiB** — CERTAIN. `mov esi, "…-in-bytes"` (0x1eba724) precedes `mov qword [r12+9F8h], 0x40000000` (0x1eba7d5); `mov esi, "…-count"` (0x1eba8fe) precedes `mov qword [r12+0AB8h], 0x100` (0x1eba9af). The argstr-to-immediate adjacency is direct; not inferred.

2. **Stock engine / Neuron key split** — CERTAIN. The AR `Run` (@0x1f8f990, 39 bytes) loads only `&NeuronAllReduceCombineKey` into `r8` and tail-calls `xla::AllReduceCombiner::RunWithKeyCombiner` (an `xla::`, not `xla::hilo::`, symbol). The engine source path is `./xla/service/collective_combiner_utils.h`; the combiners' is `hilo/hlo_passes/neuron_*_combiner.cc`. The namespace and source-path split is observable, not assumed.

3. **The three key tuples** — CERTAIN. AR `KeyT` (demangled from the `call` operand @0x1f8f9a9) ends in `std::string`; AG `KeyT` (from the engine `call` @0x1f8af6f) is the 6-tuple `<PrimitiveType, optional<long>, long, bool, bool, vector<vector<long>>>` with **no** string; RS `KeyT` carries the middle `long` scatter dim. Three distinct demangled types confirm three key functors over one engine template.

4. **`combine_by_dim` default = TRUE** — CERTAIN. `mov byte [r12+0B78h], 1` @0x1ebab7a, after the `"collective-combine-by-dim"` argstr block (0x1ebaae2). This overturns D-B08's "false" reading.

5. **field +0x08 = bytes, +0x10 = count (byte-first)** — CERTAIN. Factory loads `r14 = [opts+0x9F8]` (the in-bytes flag) → `rsi` → ctor arg1 → `[this+8]`; `r15 = [opts+0xAB8]` (count) → `rdx` → arg2 → `[this+0x10]` (AG @0x1e70724/0x1e7072b, AR @0x1e70617/0x1e7061e). Cross-checked against the engine `push [rbx+8]` for the byte arg.

**Tagged INFERRED / not fully traced:** the per-op byte-size accumulator inside the engine is assumed dense `ShapeUtil::ByteSizeOf` (HIGH, not byte-traced); the AG transpose permutation index math @0x1f8c1de (MED); the exact `bool0`/`bool1` identities (`constrain_layout` vs `use_global_device_ids`) in `GetAllReduceKey` (MED — carried opaquely into the key either way); the `PrimitiveType` enum-name set appended after `"dtype="` (HIGH — produced by runtime protobuf reflection, not statically enumerable in this binary); and the stock `combine_fn` operand-concat rewrite for AR/RS (HIGH that it is unmodified upstream `CombineCollectives`; exact address a gap).

---

## Related Passes

| Pass | Name | Relationship |
|---|---|---|
| #77 | `all-gather-combiner` | this page — AG combiner; 6-tuple key + dim normalisation |
| #78 | `reduce-scatter-combiner` | this page — RS combiner; scatter-dim key |
| #79 | `all-reduce-combiner` | this page — AR combiner; dtype-string key |
| #86 | `collective-permute-to-all-gather` | reads a distinct `HloPassOptions` triple (+0xE90/+0xF48/+0xDA0); not a combiner |

---

## Cross-References

- [Collectives → CustomCall Lowering](collectives-to-customcall.md) — §4.3, how collectives are lowered after combining; the combined collective is what reaches the lowering
- [Flip-Collective OpExpander Family](flip-collective-opexpander.md) — §4.6, the flip that enables combining upstream of these passes
- [Distribution Bucketing](../distribution/bucketing.md) — Part 13, the runtime-side bucket model these byte/count thresholds feed
