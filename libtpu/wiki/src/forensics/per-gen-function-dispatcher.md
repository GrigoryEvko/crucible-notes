# Per-Generation Function Dispatcher

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 745 MB, not stripped). VMA == file offset in `.text`/`.rodata`/`.lrodata`. Other builds will differ.*

## Abstract

A TPU compiler that ships one binary for six silicon generations needs a way to pick the right per-generation implementation — the right target descriptor, cost model, codec, ISA emitter, HAL factory — at run time, keyed on the device it is compiling for. The **per-generation function dispatcher** is the structural class that does this selection. It is not, as a naive reading of a stripped binary might suggest, one giant `switch (version)`. The dominant mechanism is `util_registration::FunctionRegistry<Key, Signature>` — a mutex-guarded abseil swiss-table (`flat_hash_map`) of type-erased `std::function` *factory functors*, keyed on the `tpu::TpuVersion` enum (or, for the ISA emitter, on the pair `(TpuVersion, TpuSequencerType)`). Lookup is a real swiss-table `find()`; on a hit, the recovered functor is *invoked* to construct the per-generation object.

This is the same shape an LLVM engineer knows from `TargetRegistry` (the `RegisterTarget`/`lookupTarget` pattern that lets one `llc` binary emit for every backend), but built on Google's `util_registration` library and abseil containers rather than LLVM's intrusive linked list. Registration happens at static-init time: each generation contributes a tiny three-instruction `GoogleInitializer` thunk (`lea functor, %rsi; mov $version, %edi; jmp Register…`) that inserts one factory under one version key. Selection is then a `lock_shared` → `find` → invoke-functor sequence with no branch ladder anywhere in the hot path.

Two *other* per-generation selection mechanisms coexist with the registry and are documented here for contrast: a small set of LLVM-lowered `switch (version)` jump tables (the codec factory `tpu::TpuCodec::Create`, the low-level mnemonic encoder factory, the enum-string utilities), and one static 2-D array indexed `[PlatformType][TpuVersion]` (the HAL factory store `g_hal_factories_by_type`). Once any of the three has *selected and constructed* the per-generation object, all subsequent per-generation behavior dispatches through that object's **vtable** — the parallel per-codename vtable families (`Target` 266 slots, `CycleTable` 5 slots, `TpuCodec` 6 slots, `IsaEmitter` 152 slots). The dispatcher *selects* the family; the vtable *runs* it. This page is one class within the broader [dispatch-table taxonomy](dispatch-table-taxonomy.md); it covers the per-generation registry and its two sibling mechanisms in depth.

For reimplementation, the contract is:

- **The registry object** — a 40-byte `{absl::Mutex, raw_hash_set CommonFields}` Meyers singleton, lazily constructed via `__cxa_guard_acquire` + `operator new(0x28)` + zero-init.
- **The lookup** — a swiss-table `find` over a 4-byte (or 8-byte pair) enum key, under a shared lock, returning a type-erased `std::function`; the empty-functor sentinel on miss.
- **The `TpuVersion` ordinal → codename map** that indexes every per-generation table, and where each registry reads its key from.
- **The registration path** — the `GoogleInitializer` thunks that populate the registries, one per codename (and per sequencer type for the ISA emitter).
- **The miss policy per registry** — graceful `InvalidArgument` for `Target`, hard `LogMessageFatal` for `CycleTable`, empty-functor for the LLO/emitter registries.

| | |
|---|---|
| **Dispatcher class** | `util_registration::FunctionRegistry<Key, Signature>` |
| **Registry object size** | 40 bytes (`operator new(0x28)`): `{Mutex @+0x00, CommonFields @+0x08..+0x27}` |
| **Lookup engine** | `absl::container_internal::raw_hash_set<…>::find` (swiss-table, SOO threshold `0x20000`) |
| **Key type (per-gen)** | `tpu::TpuVersion` (4-byte enum) or `pair<TpuVersion, TpuSequencerType>` (8-byte) |
| **Per-gen registries** | 5 × `TpuVersion`-keyed + 1 × pair-keyed (Target, CycleTable, 3 LLO ops, IsaEmitter) |
| **Sibling mechanisms** | M2 `switch (version)` jump tables; M3 static `[PlatformType][TpuVersion]` array |
| **Canonical `Get`** | `xla::jellyfish::target::GetForVersion` @ `0x1d49f500` → `FunctionRegistry::Get` @ `0x1d49f580` |
| **Codename→ordinal map** | `DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0`; `TpuVersionToString` @ `0x20b3a480` |

---

## The Per-Generation Dispatch Families At A Glance

Six distinct families select a per-generation implementation. Five are `TpuVersion`-keyed registries; one is keyed on the `(version, sequencer-type)` pair. Each row names the selector entry point, where it reads its version key, and what happens on a lookup miss — the miss policy is deliberately *different* per family and is the most important reimplementation detail in the table.

| # | Family (constructed object) | Selector `Get` @ | `Register` @ | Key source | Miss policy |
|---|---|---|---|---|---|
| 1 | `Target` (per-gen descriptor, 266-slot vtable) | `0x1d49f580` | `0x1d49f760` | `TpuTopology+0x8` | `InvalidArgument` (`StatusOr` error) |
| 2 | `CycleTable` (cost model, 5-slot vtable) | `0x1c89cd20` | `0x1c89d400` | `Target+0x398` | `LogMessageFatal` (hard crash) |
| 3 | LLO ATR op (`LloValue*` factory) | `0x1d545280` | `0x1d5ae3c0` | `TpuVersion` | empty functor |
| 4 | LLO DMA op (`LloValue*` factory) | `0x1d546600` | `0x1d5aebe0` | `TpuVersion` | empty functor |
| 5 | LLO route op (`LloValue*` factory) | `0x1d54e020` | `0x1d5aa7a0` | `TpuVersion` | empty functor |
| 6 | `IsaEmitter` (152-slot vtable) | `0x140af4e0` | `0x140c2360` | `(version, seqtype)` pair | empty functor |

The same `FunctionRegistry` machinery also backs eight *non*-per-generation registries keyed on op-name strings (custom-call `ShouldFuse`, `OperandData` lowering, `HloCostAnalysis`, SPMD partitioning, the system profiler, the DMA-descriptor-state factory). They share the identical `Register`/`Get`/`find` swiss-table code but key on strings, not versions — they are the custom-call / op-emitter registries, not per-generation dispatchers, and are out of scope here.

> **NOTE —** the dispatcher is the *selector*, not the *behavior*. Families 1, 2, and 6 each construct an object whose vtable then carries all per-generation runtime logic. The 266-slot `Target` vtable, the 5-slot `CycleTable` vtable, and the 152-slot `IsaEmitter` vtable are documented in the [RTTI ↔ vtable census](rtti-vtable-census.md). This page stops at construction; the vtable page picks up where it leaves off.

---

## Dispatch Mechanism

### Purpose

Map a runtime `TpuVersion` (and sometimes a `TpuSequencerType`) to a function that constructs the correct per-generation object, with no compile-time knowledge of which generations are linked in. The registry inverts control: each generation *registers itself* at static-init, and the selector queries by key. Adding a generation means adding a registration thunk, not editing a `switch`.

### The registry object

Every per-generation registry is a Meyers singleton — a function-local `static` whose construction is guarded by `__cxa_guard_acquire`/`__cxa_guard_release`. The object is exactly 40 bytes (`operator new(0x28)`), zero-initialized, with this layout:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `mutex` | `+0x00` | `absl::Mutex` (8 B) | Shared on read (`Get`), exclusive on write (`Register`) |
| `set` | `+0x08` | `raw_hash_set` `CommonFields` (32 B) | `{control-ptr/H2 bytes, slots-ptr, size, capacity}` of `flat_hash_map<Key, shared_ptr<MapValue>>` |

`MapValue` is the type-erased payload: a `std::function<Signature>` plus an `absl::SourceLocation {file, line}` recorded at registration for duplicate-key diagnostics. It is heap-allocated (`operator new(0x48)` = 72 B for the `Target` registry) and held by `shared_ptr` so a concurrent `Get` can ref-count it out from under the lock.

### Algorithm — selection

```c
function GetForVersion(version):                       // sub_1D49F500 (Target)
    if !guard(target_registry):                        // __cxa_guard_acquire
        r = operator new(0x28)                          // 40 bytes
        memzero(r, 0x28)                                // vxorps + vmovups ymm0
        target_registry = r                             // Meyers singleton
        release_guard()
    return FunctionRegistry::Get(target_registry, &version)   // sub_1D49F580

function FunctionRegistry::Get(this, key):             // sub_1D49F580
    Mutex::lock_shared(this + 0x00)
    it = raw_hash_set::find(this + 0x08, key)          // sub_1D49FAC0 — swiss-table
    if it:                                              // HIT
        mv = *it.shared_ptr                             // MapValue
        atomic_inc(mv.refcount)                         // _InterlockedIncrement64
        if mv.function not empty:
            out = copy of mv's __policy_func            // FunctionWrapper trampoline
        else:
            out = __empty_func                          // null-construct sentinel
    else:                                               // MISS
        out = __empty_func                              // empty std::function
    Mutex::unlock_shared(this + 0x00)
    return out
```

The `find` is a genuine abseil swiss-table probe, not a linear scan. For the 4-byte `TpuVersion` key: hash the key, SSE group-match the H2 control bytes (`vpcmpeqb %xmm1,%xmm0; vpmovmskb`), `tzcnt` to pick the matching slot, then a single 4-byte key compare against the stored enum int. Capacity is checked against the small-object-optimization threshold `0x20000`. The caller then *invokes* the returned `std::function` to construct the object; an empty functor means "no implementation for this generation."

> **QUIRK —** the selector never branches on the version value itself. There is no `if (version == kJellyfish) …` anywhere on the hot path — the only comparison is the swiss-table's key-int compare deep inside `find`. A reimplementer who models this as a `switch` will get correct *behavior* but the wrong *structure*, and will miss that generations are pluggable at link time.

### Algorithm — registration

```c
// One GoogleInitializer module per codename, emitted as a 3-instruction thunk:
//     lea  <$_0::__invoke>, %rsi        ; raw factory fn-ptr (e.g. → GhostliteTarget::Create)
//     mov  $<version>,      %edi        ; the TpuVersion key (xor %edi,%edi for v0)
//     jmp  RegisterTargetCreationFunctor ; tail-call

function RegisterTargetCreationFunctor(version, fn_ptr):   // sub_1D49F680
    ensure target_registry exists                          // same Meyers guard
    fn = wrap(fn_ptr)                                       // std::function via __policy_func + FunctionWrapper
    FunctionRegistry::Register(target_registry, &version, fn,
                               SourceLocation{"target_registry.cc", 25})  // sub_1D49F760

function FunctionRegistry::Register(this, key, fn, loc):   // sub_1D49F760
    Mutex::lock(this + 0x00)                                // EXCLUSIVE
    mv = operator new(0x48)                                 // 72-byte MapValue
    move fn into mv ; store loc at mv+0x38 / mv+0x40
    (it, inserted) = find_or_prepare_insert(this+0x08, key) // sub_1D49FC80; stores key int32
    if not inserted:
        StrCat duplicate-key error with version + SourceLocation
    Mutex::unlock(this + 0x00)
```

The factory functor is a raw function pointer wrapped in a `std::function` via libc++'s `__policy_func` type-erasure. The wrapper class is `FunctionRegistry<…>::FunctionWrapper`; its `__call_func`/`__create`/`__empty_func` policy slots appear by name in the decompiled `Get` body, which is how the type erasure is recovered without source.

### Function map

| Function | Address | Role |
|---|---|---|
| `target::GetForVersion` | `0x1d49f500` | Lazy-init + selector entry for the `Target` registry |
| `FunctionRegistry<TpuVersion,…Target>::Get` | `0x1d49f580` | Shared-locked swiss-table lookup → `std::function` |
| `raw_hash_set<…>::find` (Target) | `0x1d49fac0` | SSE group-match + `tzcnt` + int32 key compare |
| `target::RegisterTargetCreationFunctor` | `0x1d49f680` | Static-init registration entry (one per codename) |
| `FunctionRegistry<…>::Register` | `0x1d49f760` | Exclusive-locked `find_or_prepare_insert` |
| `find_or_prepare_insert_large` | `0x1d49fc80` | Slot allocation + key-int store for `Register` |

---

## TpuVersion Index

### The ordinal → codename map

Every per-generation table is indexed by the `tpu::TpuVersion` enum. The ordinals are dense, 0..5, and map to codenames as follows. The mapping is recovered from the codec `switch` cases (which are literal `case 0..5`), the `Target` registration thunks (each `mov $N,%edi`), and the codename in each leaf factory's symbol.

| Ordinal | Codename | Short tag |
|---|---|---|
| 0 | Jellyfish | jxc (legacy) |
| 1 | Dragonfish | — (shares Jf cost model) |
| 2 | Pufferfish | pxc / plc |
| 3 | Viperfish | vfc / vlc |
| 4 | Ghostlite | glc |
| 5 | (anon, `6acc60406` / TPU7x) | gfc (mktg "Ironwood") |

> **GOTCHA —** the `TpuVersion` enum used to *index the per-generation tables* is **not** the same axis as the `DeviceType`/`DeviceIdentifiers` ordinals returned by `DeviceTypeFromDeviceIdentifiers` (`0xf6993a0`). That function maps external device identifiers onto a *different* numbering (one axis places Jellyfish at 3 and Ghostlite at 13). The registries are keyed on the dense internal `TpuVersion` (0..5), reached after the device-identifier layer has normalized to it. Conflating the two will index the wrong table. The internal `TpuVersion` is the canonical index for everything on this page; treat `DeviceTypeFromDeviceIdentifiers` as an upstream normalizer, not the table index.

### Where each registry reads its key

The key is read from a different source per family, because each runs at a different point in the pipeline where a different object holds the authoritative version:

- **`Target` registry** — reads `*(u32*)(topology + 8)`, the version stored on the `TpuTopology`, at `target::CreateFromTopology` (`0x1d48e520`). The topology is the earliest carrier of the version.
- **`CycleTable` registry** — reads `*((u32*)target + 230)` = `Target+0x398` (`Target::tpu_version()`), inside `CycleTable::Create` (`0x1c89cc00`). By cost-model time the `Target` exists and is authoritative.
- **`IsaEmitter` registry** — reads an 8-byte `{version, seqtype}` pair; the version half is the same internal `TpuVersion`, the second half a `TpuSequencerType`.

In each case the 4-byte (or 8-byte) key is copied to a stack temporary and passed by `const&` to `Get`.

> **NOTE —** `Target+0x398` (the `CycleTable` key) and `Target+0x3fc` (a chip-parts version written in `CreateFromTopology`) are two distinct version-like `u32` fields. Their precise distinction — internal `TpuVersion` vs. a `TpuVersionProto` vs. a variant discriminator — was not fully reconciled from the binary; `+0x398` is the one the cost-model lookup keys on (LOW confidence on the exact semantics of `+0x3fc`).

---

## Representative Dispatchers By Address

### The Target registry — the canonical example

`Target` selection is the cleanest instance of the mechanism and the one most fully byte-traced. The flow from topology to constructed `Target`:

```text
CreateFromTopology(topology, l, l)          0x1d48e520
  version = *(u32*)(topology + 8)           ── key from topology
  fn = target::GetForVersion(version)       0x1d49f500  ── registry lookup
  if fn.empty():
      return InvalidArgument("No Target registered for ", version)
  Target* t = fn(topology, l, l)            ── INVOKE the per-gen factory functor
  t[+0x3fc] = chip_parts_version            ── post-construct field writes
  t[+0x928] = config
  return StatusOr{t}
```

There are **six** `Target` factories registered — versions 0..5. Five are the named `…Target::Create` modules (jellyfish..ghostlite); the sixth is a v5 (`gfc`/TPU7x) registration thunk that lives in the `google_init_cold` section with no own module symbol. Its `__invoke` (`0x1d49f100`) does not tail-call any of the five named `…Target::Create` functions — it runs an outlined factory body (`0x1d49c9c0`) that `new`s and `Target::Init`s a **`ViperfishSparseCoreTarget`** (typeinfo `_ZTIN…25ViperfishSparseCoreTargetE`, `0x21cc9080`), constructed via `Target::Init(…, unique_ptr<SparseCoreTarget>, …)` (`0x1d60fc20`). So v5 has its own (anonymous, SparseCore-derived) `Target`, not a reuse of the v4 Ghostlite descriptor — the same full-population as `CycleTable` (`GfcCycleTable`) and the codec's distinct v5 case.

| `TpuVersion` | Codename | `…Target::Create` @ | `GoogleInitializer` module @ |
|---|---|---|---|
| 0 | jellyfish | `0x1d492040` | `xla_target_jellyfish` `0x213eeea0` (edi=0, via `xor`) |
| 1 | dragonfish | `0x1d48f0a0` | `xla_target_dragonfish` `0x213eee80` (edi=1) |
| 2 | pufferfish | `0x1d493620` | `xla_target_pufferfish` `0x213eeec0` (edi=2) |
| 3 | viperfish | `0x1d4995a0` | `xla_target_viperfish` `0x213eef00` (edi=3) |
| 4 | ghostlite | `0x1d496640` | `xla_target_ghostlite` `0x213eeee0` (edi=4) |
| 5 | (anon, `gfc`/TPU7x) | `ViperfishSparseCoreTarget` via `0x1d49f100`→`0x1d49c9c0` | thunk in `google_init_cold` `0x213eef20` (edi=5) |

Each module's `$_0::__invoke` thunk (e.g. ghostlite `__invoke` `0x1d499040`) `lea`s itself into `%rsi`, sets the version in `%edi`, and tail-calls `RegisterTargetCreationFunctor`; `__invoke` in turn tail-calls the named `…Target::Create`. The `Target` registry's singleton lives at `target_registry` (`0x2257e170`, guard `0x2257e178`), and a miss produces the graceful string `"No Target registered for "` (`0xa1e75db`).

### The CycleTable registry — fatal on miss

The cost-model registry is structurally identical but its miss policy is the opposite extreme. `CycleTable::Create` (`0x1c89cc00`) keys on `Target+0x398`, looks up the registry singleton (`GetCycleTableRegistry::r`, `0x225799e8`), and if the returned functor is the empty sentinel it calls `LogMessageFatal` and **crashes the process**:

```c
function CycleTable::Create(target):                   // sub_1C89CC00
    ensure GetCycleTableRegistry::r                     // Meyers guard, operator new(0x28)
    key = *((u32*)target + 230)                         // Target+0x398
    fn = FunctionRegistry::Get(r, &key)                 // sub_1C89CD20
    if fn.is_empty():                                    // *(byte*)(it+16) == 1
        LogMessageFatal("cycle_table.cc", 960,           // HARD CRASH
                        "cycle_table_creator != nullptr")
            << "No cycle table registered for platform: " << key
    return fn(target)                                    // construct the per-gen CycleTable
```

The rationale: any reachable generation *must* have a cost model, so a missing registration is a build/link error worth crashing on, not a recoverable condition like a missing target descriptor.

The `CycleTable` registry differs from `Target` in one structural way: it has **six** leaves, 0..5 — the cost model is per-generation all the way down to v5, even though the v5 `Target` is shared. The named per-codename `CycleTable` vtables:

| `TpuVersion` | Leaf | `_ZTV` vtable symbol @ | First-slot address (+0x10) |
|---|---|---|---|
| 0 / 1 | `JfCycleTable` | `0x21c1ffb8` | `0x21c1ffc8` |
| 2 | `PfCycleTable` | `0x21c20048` | `0x21c20058` |
| 3 | `VfCycleTable` | `0x21c200c8` | `0x21c200d8` |
| 4 | `GlcCycleTable` | `0x21c20148` | `0x21c20158` |
| 5 | `GfcCycleTable` | `0x21c201c8` | `0x21c201d8` |

> **Note (vptr convention):** the two columns above are the two correct ways to cite a vtable. The `_ZTV` symbol address (`0x21c1ffb8` …) is the group base, which begins with the 16-byte `{offset-to-top, typeinfo-ptr}` header; the first-slot address (`0x21c1ffc8` …) is `_ZTV+0x10`, the value an object's vtable pointer actually holds. When matching a `call *0xN(%rax)` site against a symbol, the slot index is measured from `_ZTV+0x10`, not from the `_ZTV` symbol. The named-leaf stride is `0x80` bytes (the base `CycleTable` vtable sits between Jf and Pf, widening that one gap to `0x90`).

### The IsaEmitter registry — the widest, pair-keyed

The ISA-emitter registry is the broadest per-generation dispatcher and the only one keyed on a *pair*: `pair<TpuVersion, TpuSequencerType>`. Its `find` (`0x140af5e0`) hashes the two int32 key halves with `_mm_crc32_u64`, group-matches the H2 control bytes (`vpcmpeqb`/`vpmovmskb`), `tzcnt`s the slot, then compares **both halves**:

```c
// raw_hash_set<…pair<TpuVersion,TpuSequencerType>…>::find — sub_140AF5E0
if *(u32*)(slot)     == version &&        // first int32: TpuVersion
   *(u32*)(slot + 4) == seqtype:          // second int32: TpuSequencerType
    return slot                            // pair hit
```

`Get` is `0x140af4e0`, `Register` is `0x140c2360`. The registry is populated by ~28 `google_init_module_*_emitter` static-init modules. Some are per-codename-per-sequencer (the `MakeIsaEmitter<…>` lambdas for `jellyfish`, `pufferfish` tensor-core and barna-core, `viperfish` tensor-core, `ghostlite` tensor-core, the barna-core address-handler); the rest are op-specific (cholesky, qr, lu, eigh, topk, resize, padding, alloc, window-prefetch, async-collective start/done) that register their emitter under specific sequencer-type cells. The full `(version × sequencer-type) → IsaEmitter-leaf` cell census was not exhaustively enumerated (it requires decoding each module's `__invoke` thunk and its embedded pair); the registry *mechanism* and signature are CERTAIN, the per-cell matrix is not traced.

> **GOTCHA —** the `IsaEmitter` key is a *pair*, and the SOO (small-object) path compares the 8-byte key as a single `vpcmpeqd` against the inline slot. A reimplementer who keys this registry on `TpuVersion` alone will collide every sequencer type within a generation onto one slot and silently return the wrong emitter. The sequencer-type half is part of the selection key, not a tie-breaker.

### Sibling mechanism M2 — the codec switch-jump

Not every per-generation selection is a registry. `tpu::TpuCodec::Create` (`0x1e835fa0`) is a literal `switch (version)` — an LLVM-lowered jump table — over cases 0..5:

```c
function TpuCodec::Create(out, version):               // sub_1E835FA0
    switch version:
        case 0: codec = CreateTpuCodecJellyfish()       // 0x1e840ac0
        case 1: codec = CreateTpuCodecDragonfish()      // 0x1e8360e0
        case 2: codec = CreateTpuCodecPufferfish()      // 0x1e841fa0
        case 3: codec = CreateTpuCodecViperfish()       // 0x1e843f00
        case 4: codec = CreateTpuCodecGhostlite()       // 0x1e83bce0
        case 5: codec = sub_1E838380()                  // anon v5 codec
    out[1] = codec ; out[0] = 1                          // StatusOr{codec}
```

The codec path keeps a *named* v5 case (`sub_1E838380`, the anonymous v5 codec) even though the v5 `Target` is shared — the same v5-asymmetry as `CycleTable`. The low-level mnemonic-encoder factory `jfc::mnemonics::factory::CreateEncoder(version, seqtype)` (`0x1d1e3820`) is a similar `cmp $N,%esi` ladder, and the enum-string utilities (`TpuVersionToString` `0x20b3a480`, `TpuVersionToExternalName` `0x20b3a500`) are more switches. These are the literal-`switch` members of the per-generation dispatch family — fewer than ten, and confined to the codec/encoder/enum-string layer.

### Sibling mechanism M3 — the HAL factory 2-D array

The HAL factory framework uses neither a registry nor a switch but a static 2-D array, `tpu::(anon)::g_hal_factories_by_type` (`0x22583be0`, mutex `0x22583bd8`). It is indexed `[PlatformType]` (0x30-byte stride, via `lea (rcx,rcx,2); shl $4`) × `[TpuVersion]` (8-byte `unique_ptr<TpuHalFactory>` slots, bounds-checked against `0x6`), with a registered-platform bitmask at `+0x90`. `TpuHalFactory::Get` (`0x1fbb19c0`) keys on `(TpuVersion, optional<PlatformType>)`; `Register` (`0x1fbb16a0`) sets the platform bit and writes the `unique_ptr`. A miss yields `"No TPU platform registered for "` (`0xa1e75f5`) or `"No TPU platforms registered; check deps."` (`0x860012b`). This is the third per-generation selection shape; see the [dispatch-table taxonomy](dispatch-table-taxonomy.md) for how all three are cataloged.

---

## Related Components

| Component | Relationship |
|---|---|
| `Target` 266-slot vtable family | The object family selected by registry #1; runtime per-gen behavior |
| `CycleTable` 5-slot vtable family | Selected by registry #2; the per-gen cost model |
| `IsaEmitter` 152-slot vtable family | Selected by pair-keyed registry #6; the per-gen ISA encoders |
| `TpuCodec` 6-slot vtable family | Selected by the M2 codec switch-jump |
| Optimization-rewrite dispatcher | A *separate* class: MLIR `PatternApplicator` benefit-ordered dispatch, not version-keyed |

The optimization-rewrite dispatcher (the MLIR `PatternApplicator` / `OperationLegalizer` engine) is frequently confused with this one because both are "dispatchers," but it selects a *rewrite rule* by op-kind and benefit, not a per-generation implementation by version. It is documented separately.

## Cross-References

- [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md) — the parent taxonomy; this page is the per-generation registry class examined in depth, alongside the M2/M3 siblings
- [RTTI ↔ Vtable Census](rtti-vtable-census.md) — the 266/5/152-slot vtable families this dispatcher selects and constructs
- [Polymorphic Dispatch Entry Points](polymorphic-entry-points.md) — the indirect-call sites and thunk tables that the constructed per-gen objects expose
- [Forensics Overview](overview.md) — where the per-generation dispatcher sits in the binary anatomy
- [ISA Emitter Registry](../isa/isa-emitter-registry.md) — the pair-keyed `IsaEmitter` registry and its per-sequencer cells, from the ISA side
- [Sequencer Ops Per Generation](../isa/sequencer-ops-per-gen.md) — the `TpuSequencerType` axis that forms the second half of the `IsaEmitter` key
