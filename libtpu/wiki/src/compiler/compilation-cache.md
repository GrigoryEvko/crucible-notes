# Compilation Cache

> *Addresses, build-id, and symbol names apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ; treat every VA as version-pinned.*

## Abstract

Compiling a TPU program is expensive — the full HLO→MLIR→LLO descent, MSA, scheduling, and bundle packing run end to end (see [compile-phases.md](compile-phases.md)). libtpu therefore never recompiles a program it has already built if it can recognize the request as identical. Recognition is a **cache-key** problem: the runtime must reduce the entire compilation request — the program text, the compiler flags, and the physical target — to a short comparable token, look that token up, and on a miss compile once and store the result under it. This page owns that machinery: **what hashes into the key, the lookup/store paths, and the cache-entry lifecycle.** The serialized executable that ends up *inside* a hit entry — the `TpuExecutable` / `TpuProgram` wire format — is owned by [tpu-program-serialization.md](tpu-program-serialization.md) and is treated here as an opaque payload.

There are **three distinct cache layers**, and conflating them is the first reimplementation trap. The outermost is the XLA JIT cache, `tensorflow::DeviceCompilationCache<T>` (from `device_compilation_cache.h`): an in-process `flat_hash_map` keyed by a `DeviceCompilationClusterSignature` — a function name plus the *canonicalized argument shapes/values* — that decides whether an XLA cluster needs (re)compilation at all. Beneath it sits the **TPU C-API key**, `tensorflow::tpu::TpuCompilationCacheKey`, built by `TpuCompile_CreateCompilationCacheKey` (0xf6a2080) in `tpu_util_c_api.cc`: a `Fingerprint2011` digest over a `StrCat`-assembled string of thirteen named inputs (HLO/MLIR fingerprints, replica count, topology bounds, guaranteed-constants size, shapes prefix, embedding-partitions fingerprint) plus a conditional device-assignment tail. The innermost is the TFRT group cache, `tensorflow::tfrt_tpu::TpuCompilationCache` (`tpu_compilation_cache.cc`): the layer that actually *holds* compiled programs, resolves a key through a three-tier lookup (in-memory → coordination service → on-disk persistent cache), and manages entry refcounts and eviction.

The page is organized by layer, outermost first. Each layer gets the same `### Purpose / ### Cache Key / ### Lookup & Store / ### Algorithm / ### Function Map` grammar, so a reader can compare the three keys side by side. The closing units cover the persistent on-disk path format and the entry lifecycle (refcount, eviction, restore-from-serialized).

For reimplementation, the contract is:

- **The TPU cache key is a `Fingerprint2011` over a deterministic, ordered string** of thirteen named fields plus a conditional device-assignment tail. Reproduce the field set, the *concatenation* order (which differs from the `dump_vars` label order — `shapes_prefix` is appended last), the separators (`:` and `,`), and the two-stage hash (fingerprint the embedding-partitions proto first, then fingerprint the whole prefix). Any field omitted or reordered silently changes the key and causes spurious misses.
- **The XLA-side key is a *signature*, not a hash** — `DeviceCompilationClusterSignature` stores the function name plus a per-argument canonical form (constant tensors carried by value, non-constants carried as `(dtype, shape)`), compared by a custom `Hash`/`==`. It gates *whether* `TpuCompile_CreateCompilationCacheKey` is even called.
- **Lookup is three-tiered.** `TpuCompilationCache::LookUpInternal` (0xf7a72c0) tries the in-memory map first, then the coordination service, then the persistent on-disk cache; a miss at every tier is the only path that triggers a real compile.
- **Entries are reference-counted and LRU-evictable.** The cache holds `CompiledSubgraph`/`TpuCompilationCacheEntry` objects with explicit `Release`/`DiscardEntryRef`/`MarkOldestEntryForEviction`; an executing program pins its entry.
- **The persistent path is a filesystem prefix**, not a hash directory: `JoinPath(dir, "CL" + guaranteed_const_fp + "_" + base_key)`, built by `ConstructCacheEntryFilepathPrefix` (0xe8cab00).

| | |
|---|---|
| **TPU key builder** | `TpuCompile_CreateCompilationCacheKey` — 0xf6a2080 (`tpu_util_c_api.cc:149-193`) |
| **Key digest** | `Fingerprint2011` (0x20d6cd40) → decimal string; two-stage |
| **XLA signature** | `tensorflow::DeviceCompilationClusterSignature::Build` — 0xfb01f40 |
| **XLA in-mem cache** | `DeviceCompilationCache<T>::{LookupOrCreate,Store}` — 0xe9932e0 / 0xe994a60 |
| **TFRT group cache** | `tensorflow::tfrt_tpu::TpuCompilationCache` (`tpu_compilation_cache.cc`) |
| **Three-tier lookup** | `TpuCompilationCache::LookUpInternal` — 0xf7a72c0 |
| **Persistent path** | `ConstructCacheEntryFilepathPrefix` — 0xe8cab00 (`"CL" + fp + "_" + key`) |
| **Cache entry type** | `tfrt::tpu::TpuCompilationCacheEntry` (ctor 0xf7bd100) |
| **Source files** | `tpu_util_c_api.cc`, `device_compilation_cache.h`, `tpu_compilation_cache.cc` |

---

## Layer 1 — XLA Cluster Signature (`DeviceCompilationCache`)

### Purpose

This is the outermost gate, and the one a JAX/XLA caller hits first. When an XLA cluster (an HLO computation derived from one `NameAttrList` plus its argument list) is about to be compiled, `DeviceCompilationCache<T>` decides whether an equivalent cluster has already been compiled *in this process*. It is templated on the executable type — both `xla::PjRtLoadedExecutable` (0xe9932e0) and `xla::LocalExecutable` (0xe986a20) instantiations are present in the binary, sharing identical `LookupOrCreate`/`Store` code. It is a pure in-memory cache from `third_party/tensorflow/compiler/jit/device_compilation_cache.h` (the `Store` log site cites `device_compilation_cache.h:259`); it has no persistence of its own and delegates the actual compile to `DeviceCompiler<T>::CompileStrict`/`CompileAsynchronous`.

### Cache Key

The key is a `tensorflow::DeviceCompilationClusterSignature`, **built — not hashed — into a structured value** by `DeviceCompilationClusterSignature::Build` (0xfb01f40):

```c
// DeviceCompilationClusterSignature::Build  (sub_0xfb01f40)
// canonical_function : a DeviceCompilationCanonicalFunction (name + attrs)
// args               : Span<const XlaArgument>
function Build(canonical_function, args):
    sig.name = canonical_function.name                  // copied string, +0..+15
    AppendArguments(sig, args)                           // sub_0xfb01a80
    return sig
```

`AppendArguments` (0xfb01a80) walks each `XlaArgument` and appends a `std::variant`: a **compile-time-constant** argument is captured **by value** as a `tensorflow::Tensor` (so two calls with different constant values get different signatures), while a non-constant argument is captured as a `pair<DataType, InlinedVector<int64,4>>` — its dtype and shape only. The signature is compared by `DeviceCompilationClusterSignature::Hash` and equality, which is why the map below is a `FlatHashMapPolicy<DeviceCompilationClusterSignature, unique_ptr<Entry>>, Hash>` rather than a string-keyed map.

> **GOTCHA —** the signature is *value-sensitive for constants and shape-sensitive for variables*. A reimplementation that keys only on shapes will incorrectly share entries across calls whose constant arguments differ (e.g. a `slice` start index folded as a guaranteed constant). Capturing the full `Tensor` for constants is deliberate and load order matters: `AppendArguments` preserves argument order.

### Lookup & Store

`LookupOrCreate` (0xe9932e0) and `Store` (0xe994a60) both run under a single `absl::Mutex` guarding the map. `LookupOrCreate` emplaces a fresh empty `Entry` (allocated `operator new(0x30)`, 48 bytes) only if the signature is absent, then returns a snapshot of the entry's current state. `Store` re-finds the entry by signature and writes back whichever of the four optional fields the compiler produced.

```c
// Entry layout (48 bytes, operator new(0x30); offsets from the Entry base, byte-confirmed in Store)
//   +0   : absl::Mutex          mu              (per-entry lock; Store locks Entry+0)
//   +8   : DeviceCompileState   compile_state   (DWORD; kUncompiled / kCompiling / kCompiled)
//   +16  : int64                request_count   (bumped on each LookupOrCreate; init 0)
//   +24  : absl::Status         compilation_status  (StatusRep*, refcounted; init 1 = inline OK)
//   +32  : XlaCompilationResult* compilation_result  (owned; deleted on overwrite)
//   +40  : T*                   executable      (owned; vtable-deleted on overwrite)
```

```c
// DeviceCompilationCache<T>::LookupOrCreate  (sub_0xe9932e0)
function LookupOrCreate(signature) -> EntrySnapshot:
    lock(mutex_)
    entry = map_.try_emplace(signature, new Entry{})    // EmplaceDecomposable
    entry.request_count += 1                            // +16
    snapshot.compile_state = entry.compile_state        // +8
    snapshot.status        = entry.compilation_status   // +24, Ref()'d
    snapshot.request_count = entry.request_count
    snapshot.{result,exe}  = entry.{result,exe}         // +32,+40 (aliased, not owned)
    unlock(mutex_)
    return snapshot

// DeviceCompilationCache<T>::Store  (sub_0xe994a60)
function Store(signature, opt_state, opt_status, opt_result, opt_exe):
    lock(mutex_)
    entry = map_.try_emplace(signature, new Entry{})    // re-find or create
    unlock(mutex_)
    lock(entry.mutex)                                   // per-entry mutex at Entry+0
    if opt_state.has_value():   entry.compile_state = *opt_state
    if opt_status.has_value():  entry.compilation_status = *opt_status  // swap+Unref old
    if opt_result.has_value():  delete entry.result; entry.result = release(*opt_result)
    if opt_exe.has_value():     vtable_delete entry.exe; entry.exe = release(*opt_exe)
    unlock(entry.mutex)
    VLOG(4) << "Added/updated cache entry: key=" << sig.HumanString()
                                 << ", entry=" << entry.DebugString()
```

> **QUIRK —** `Store` takes four `std::optional` fields and updates only the ones present. The compiler first stores `compile_state = kCompiling` (claiming the slot), and only later stores `compilation_result` + `executable` + `compile_state = kCompiled`. A concurrent `LookupOrCreate` that observes `kCompiling` is expected to wait on the status future rather than recompile — the cache itself does not block, it surfaces the state.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `DeviceCompilationClusterSignature::Build` | `0xfb01f40` | Build name+args signature | HIGH |
| `AppendArguments` | `0xfb01a80` | Append per-arg variant (const→Tensor, var→dtype/shape) | HIGH |
| `DeviceCompilationCache<PjRt…>::LookupOrCreate` | `0xe9932e0` | Emplace + snapshot under mutex | CERTAIN |
| `DeviceCompilationCache<PjRt…>::Store` | `0xe994a60` | Write-back optional fields | CERTAIN |
| `DeviceCompilationCache<LocalExe>::LookupOrCreate` | `0xe986a20` | Same, `LocalExecutable` instantiation | HIGH |
| `DeviceCompilationCache<LocalExe>::Store` | `0xe988d00` | Same, `LocalExecutable` instantiation | HIGH |
| `DeviceCompiler<…>::CompileStrict` | `0xe993860` / `0xe986fa0` | Miss path: do the real compile | HIGH |
| `DeviceCompiler<…>::CompileAsynchronous` | `0xe993420` / `0xe986b20` | Async miss path | MEDIUM |

---

## Layer 2 — TPU Compilation Cache Key

### Purpose

When Layer 1 misses and the compiler reaches the TPU backend, the request is reduced to a `tensorflow::tpu::TpuCompilationCacheKey` — the token under which the TFRT group cache (Layer 3) and the persistent on-disk cache are indexed. The single builder is `TpuCompile_CreateCompilationCacheKey` (0xf6a2080), exported through the C-ABI shim layer in `learning/45eac/tfrc/executor/stream_executor/tpu_util_c_api.cc` (the `LOG(FATAL)` and `VLOG` sites cite lines 149, 152, 178, 193). It is the most important function on this page, because its field set *is* the cache's notion of program identity.

### Cache Key

The key is a decimal string: the `Fingerprint2011` of a `StrCat`-assembled **prefix** string, itself a `to_string` of that 64-bit fingerprint. The builder carries a 13-entry `dump_vars` label table for its `VLOG` dump (`tpu_util_c_api.cc`, the `$_1` lambda, `dump_vars<13ul, 13ul, …>`); those thirteen labels name the program-identity inputs, and a fourteenth (`device_assignment`) is appended conditionally but is **not** in the dump table. The table below lists the inputs by name; the **#** column is the dump-table label order, which is *not* identical to the concatenation order (see the note after the pseudocode).

| # | Field (dump_vars label) | Source | Confidence |
|---|---|---|---|
| 1 | `function_name` | `std::string(property.function_name)` | HIGH |
| 2 | `function_library_fingerprint` | `property.function_library_fingerprint` | HIGH |
| 3 | `mlir_module_fingerprint` | `property.mlir_module_fingerprint` | HIGH |
| 4 | `num_replicas` | `property.num_replicas` | HIGH |
| 5–7 | `chip_bounds.{x,y,z}` | `topology.chip_bounds().{x,y,z}` (topo+0x58 region) | HIGH |
| 8–10 | `wrap.{x,y,z}` | `topology.wrap().{x,y,z}` (topo+0xA0/+0xA2) | HIGH |
| 11 | `shapes_prefix` | `std::string(property.shapes_prefix)` | HIGH |
| 12 | `guaranteed_constants_size` | `property.guaranteed_constants_size` | HIGH |
| 13 | `embedding_partitions_fingerprint` | `Fingerprint2011(embedding_partitions_proto)` | HIGH |
| — | `device_assignment` (conditional, not in dump table) | `:device_assignment:` + joined ids, **or** `:default_device_assignment` | HIGH |

```c
// TpuCompile_CreateCompilationCacheKey  (sub_0xf6a2080, tpu_util_c_api.cc:149)
// property : compilation request (function name/lib fp, mlir fp, replicas, shapes, …)
// mesh     : TpuMeshCommonState* (carries topology + embedding partitions)
function CreateCompilationCacheKey(property, mesh) -> TpuCompilationCacheKey:
    CHECK(mesh != nullptr)                                       // line 149
    // (a) fingerprint the embedding-partitions proto first
    eb_str = mesh.embedding_partitions_proto().SerializeToString() // CHECK ok, line 152
    eb_fp  = Fingerprint2011(eb_str)
    topo   = mesh.tpu_topology()
    // (b) assemble the ordered prefix string
    //     (StrCat<...,int,...,int,...,int,...,int,...,bool,...,bool,...,bool,
    //             ...,char const*,...,string> in the decompile)
    prefix = StrCat(property.function_name,                     // field 1 (string, v103/a10)
                ":", property.function_library_fingerprint,    // 2  (FastIntToBuffer)
                ":", property.mlir_module_fingerprint,         // 3  (FastIntToBuffer)
                ":", property.num_replicas,                    // 4  (int, a16)
                ":", topo.chip_bounds.x, ",", .y, ",", .z,     // 5-7  (3×int, topo+0x58 region)
                ",", topo.wrap.x, ",", .y, ",", .z,            // 8-10 (3×bool, topo+0xA0/+0xA2)
                ":", property.guaranteed_constants_size,       // 12 (int, a9)
                ":", to_string(eb_fp))                         // 13 (embedding fp, v87)
    // (c) device-assignment tail (field 14): explicit ids vs. default
    if device_grid_matches_replicas(property):                 // line ~220 shape check
        if property.device_assignment == nullptr:
            StrAppend(prefix, ":default_device_assignment")
        else:
            StrAppend(prefix, ":device_assignment:", Join(ids, ","))
    StrAppend(prefix, property.shapes_prefix)                  // field 11 (string, a8) — appended last
    VLOG(1) << "prefix_raw = " << prefix                       // line 178
    // (d) the key proper is the fingerprint of the whole prefix
    key.fingerprint  = to_string(Fingerprint2011(prefix))
    key.prefix       = prefix
    VLOG(1) << "CompilationCacheKey:" << key                   // line 193
    return key                                                  // {fingerprint, prefix} pair
```

> **CONCATENATION ORDER —** the `dump_vars` label table is the builder's *display* order, which is not the *concatenation* order. From the decompile, the main `StrCat` runs `function_name`, then `:`-joined `function_library_fingerprint`, `mlir_module_fingerprint`, `num_replicas`, then `,`-joined `chip_bounds.{x,y,z}` and `wrap.{x,y,z}`, then `:`-joined `guaranteed_constants_size` and the embedding-partitions fingerprint. The device-assignment tail is `StrAppend`ed next, and `shapes_prefix` is `StrAppend`ed **last** of all (it is the final `StrAppend` after the device-assignment block). A reimplementation must match this concatenation order, not the dump-label order, or the fingerprint diverges.
>
> The exact int-vs-bool rendering of individual `chip_bounds`/`wrap` components (the decompiled `StrCat` template instantiates four `int` and three `bool` AlphaNum slots) is a Hex-Rays artifact and was not pinned per-component; the field *names* and the overall order above are byte-anchored to the `dump_vars` labels and the `StrCat`/`StrAppend` control flow.

The guaranteed-constants contribution is itself a fingerprint, computed separately by `TpuCompile_CreateGuaranteedConstFingerprint` (0xf6a2040) and combined with `FingerprintCat2011`:

```c
// TpuCompile_CreateGuaranteedConstFingerprint  (sub_0xf6a2040)
function CreateGuaranteedConstFingerprint(running_fp, const_bytes) -> uint64:
    return FingerprintCat2011(running_fp, Fingerprint2011(const_bytes))
```

> **GOTCHA —** the key folds *both* a content fingerprint (HLO/MLIR module fp, field 3; constants size, field 12; embedding-partitions fp, field 13) *and* the physical target (topology chip bounds + wrap, fields 5–10; the device-assignment tail). Two byte-identical programs compiled for different topologies or device assignments get different keys — correct, because the compiled `TpuProgram` is target-specific. A reimplementation that keys on program text alone will serve a v4-2x2x1 binary to a v5e-2x2 request and crash at load.

> **QUIRK —** the device-assignment tail is gated by a check at decompile line 220. The tail is **appended** when `(replicas.lo × replicas.hi == num_cores) || (replicas.lo ∉ {1, num_cores})`, where `num_cores` is read from `topology+0x80` and the two `replicas` halves come from the 64-bit replica field; otherwise the explicit/default-assignment field is **omitted entirely**, not defaulted — so the key length is variable. Reproduce the predicate exactly, or single-replica and multi-replica runs of the same program will collide or diverge unexpectedly. (The grid/replica field identities here are inferred from the arithmetic shape of the guard, not from labeled symbols — MEDIUM confidence on which field is which; the predicate structure itself is byte-anchored.)

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuCompile_CreateCompilationCacheKey` | `0xf6a2080` | Assemble prefix + fingerprint → key | CERTAIN |
| `TpuCompile_CreateGuaranteedConstFingerprint` | `0xf6a2040` | `FingerprintCat2011(fp, Fingerprint2011(const))` | CERTAIN |
| `TpuCompile_DestroyCompilationCacheKey` | `0xf6a2e60` | Free the key struct | HIGH |
| `Fingerprint2011` | `0x20d6cd40` | 64-bit non-crypto digest of a `string_view` | CERTAIN |
| `FingerprintCat2011` | `0x20d6d0e0` | Combine two 64-bit fingerprints | CERTAIN |
| `TpuProgram_GetFingerprint` | `0xe8bed60` | Read fingerprint off a compiled `TpuProgram` (+160→+648) | HIGH |
| `TpuExecutable_Fingerprint` | `0xeabea40` | Fingerprint of a serialized executable | MEDIUM |

> **NOTE —** `Fingerprint2011` is the Farmhash-derived TF fingerprint, *not* a cryptographic hash. The cache trusts it for identity; an adversary could in principle force a collision, but the threat model is recompilation-avoidance, not integrity. `TpuProgram_GetFingerprint` reads the fingerprint stored *inside* an already-compiled program (the inline-string-optimized field at `program+160 → +648`), used to validate a deserialized program against its key on load.

---

## Layer 3 — TFRT Group Cache (`tfrt_tpu::TpuCompilationCache`)

### Purpose

This is the layer that actually *stores compiled programs* and decides compile-vs-reuse at runtime. `tensorflow::tfrt_tpu::TpuCompilationCache` (`learning/45eac/tfrt/tf_tpu/tpu_compilation_cache.cc`) keys on the Layer-2 key, holds `tfrt::tpu::TpuCompilationCacheEntry` objects grouped by program group, and resolves a lookup through three tiers in priority order. A "group" is a set of related entries (e.g. the per-core programs of one sharded computation) inserted and evicted together; `EmplaceGroupEntry`/`RemoveGroup`/`RemoveGroupEntries` operate at group granularity, while individual entries carry their own refcounts.

### Cache Key

Layer 3 indexes on the Layer-2 fingerprint string, but first normalizes it with `ConvertToBaseKey` (0xf7bf760), which strips the per-core suffix so all cores of a group share one base key. The persistent on-disk index uses a further-derived path prefix (see *Persistent On-Disk Cache* below).

### Lookup & Store

The hit path is `LookUpInternal` (0xf7a72c0), a three-tier resolver:

```c
// TpuCompilationCache::LookUpInternal  (sub_0xf7a72c0, tpu_compilation_cache.cc:~1097)
function LookUpInternal(base_key) -> AsyncValueRef<Entry>:
    // TIER 1 — in-memory map (shared lock; walk tombstone chain)
    lock_shared(cache_mutex_)
    node = entries_head_; while node.flags & 3: node = node.next   // skip dead slots
    result = GetTpuCompilationCacheEntry(node)                     // hit if present
    unlock_shared(cache_mutex_)
    if result.is_present: return result

    // TIER 2 — coordination service (multi-host: peer may already hold it)
    if coord_service_ && coord_service_.is_available():
        return LookUpWithCoordService(base_key)

    // TIER 3 — persistent on-disk cache
    if persistent_cache_enabled_:                                  // entries_[20]
        st = LoadGroupFromPersistentCache()                        // sub: read RecordReader
        if st == kOk:
            node = entries_head_; while node.flags & 3: node = node.next
            return GetTpuCompilationCacheEntry(node)               // now in-memory
        else:
            return MakeErrorAsyncValueRef(st)                      // propagate load error

    // total miss with no persistence → hard error (the compile path runs elsewhere)
    return MakeErrorAsyncValueRef(
        Internal("Cannot access the coordination service or persistent "
                 "cache to fetch the compilation cache entry."))   // line 1097
```

The store/compile path is `CompileGroup` (0xf7a4f60) → `MaybeCompileGroup` (0xf7a6200) → `EmplaceGroupEntry` (0xf7a3a00). `MaybeCompileGroup` is the dedup point: it re-checks the cache under an exclusive lock before launching a compile, so two concurrent first-time requests for the same key compile once and share the result. `RestoreFromSerialized` (0xf7a2ca0) is the persistent-cache loader's counterpart — it reconstructs entries from a serialized blob (`TpuCompilationEntrySource`) rather than compiling.

> **QUIRK —** the in-memory tier walks an intrusive node chain (`node = node.next` at +16) and skips any node whose low two flag bits at +4 are set. Those bits mark tombstoned / being-evicted entries; a naive `flat_hash_map::find` reimplementation will return an entry that is mid-eviction. The shared-lock walk plus flag check is the correct read protocol.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuCompilationCache::LookUpInternal` | `0xf7a72c0` | Three-tier lookup (mem→coord→disk) | CERTAIN |
| `TpuCompilationCache::LookUpWithCoordService` | `0xf7a74a0` | Tier-2 multi-host lookup | HIGH |
| `TpuCompilationCache::LoadGroupFromPersistentCache` | `0xf7a7de0` | Tier-3 on-disk read | HIGH |
| `TpuCompilationCache::CompileGroup` | `0xf7a4f60` | Miss path: compile a group | HIGH |
| `TpuCompilationCache::MaybeCompileGroup` | `0xf7a6200` | Re-check under lock, dedup compiles | HIGH |
| `TpuCompilationCache::EmplaceGroupEntry` | `0xf7a3a00` | Insert compiled group into map | HIGH |
| `TpuCompilationCache::GetEntriesFrom` | `0xf7a1f80` | Resolve group → per-core entries | MEDIUM |
| `TpuCompilationCache::RemoveGroup` | `0x21389f60` | Drop a group | HIGH |
| `TpuCompilationCache::RemoveGroupEntries` | `0xf7a85c0` | Drop N entries of a group | MEDIUM |
| `TpuCompilationCache::RestoreFromSerialized` | `0xf7a2ca0` | Rebuild entries from serialized blob | HIGH |
| `TpuCompilationCache::ConvertToBaseKey` | `0xf7bf760` | Strip per-core suffix → group base key | HIGH |
| `tfrt::tpu::TpuCompilationCacheEntry::Program` ctor | `0xf7bc500` | Build the `Program` payload of an entry | HIGH |
| `tfrt::tpu::TpuCompilationCacheEntry` ctor | `0xf7bd100` | Build a full cache entry | HIGH |

---

## Persistent On-Disk Cache

The persistent cache turns a `TpuCompilationCacheKey` into a **filesystem path prefix**, not a hash-bucket directory. `ConstructCacheEntryFilepathPrefix` (0xe8cab00, in an anonymous namespace) builds it:

```c
// ConstructCacheEntryFilepathPrefix  (sub_0xe8cab00)
// dir            : persistent cache directory (string_view)
// key            : TpuCompilationCacheKey
// topology       : tpu::TpuTopology
// guaranteed_fp  : int64 guaranteed-const fingerprint
function ConstructCacheEntryFilepathPrefix(dir, key, topology, guaranteed_fp) -> path:
    base = ConstructPersistentCompilationCacheKey(key, topology)   // derived stable key
    file = JoinStrings({ "CL", to_string(guaranteed_fp), base }, "_")  // "CL<fp>_<base>"
    return JoinPath(dir, file)                                     // tsl::io::JoinPathImpl
```

The on-disk record itself is read/written by `tfrt_tpu::RecordReader` (ctor 0xf7beda0) and `RecordWriter` (ctor 0xf7be740) — the TFRecord-style framed-blob format also used for the serialized `TpuProgram` payload. The cache directory and its read-only / read-write mode are governed by absl flags (the binary carries a `tfrt_tpu_pjrt_client_*` flag family); the exact flag names for the persistent directory were not individually traced (LOW confidence on flag identity), but the path-prefix construction above is byte-confirmed.

> **NOTE —** the `"CL"` literal is the persistent-cache record-type tag (the file is a *compiled* program). The guaranteed-const fingerprint is hoisted into the *filename* — separate from the in-key field 12 (size) — so that a change in constant *values* (not just their byte size) produces a distinct on-disk file even when the in-memory key would otherwise look stable. This is the on-disk analogue of Layer 1 capturing constant `Tensor`s by value.

---

## Entry Lifecycle

A cache entry is reference-counted across its whole life: created on a miss, pinned while a program executes, and evicted under LRU pressure. The TFRT layer's lifecycle primitives live on `TpuCompilationCacheInterface`:

```text
 LookUpInternal miss
        │
        ▼
 CompileGroup ──► MaybeCompileGroup (dedup) ──► compile ──► EmplaceGroupEntry
        │                                                        │
        │  (or)  RestoreFromSerialized / LoadGroupFromPersistentCache
        ▼                                                        ▼
   InsertEntry ───────────────────────────────────────► entry in map (refcount=1)
        │
        ▼
   execution pins entry  ──►  DoTpuExecute holds AsyncValueRef<TpuCompilationCacheEntry>
        │
        ▼
   Release(uid) / DiscardEntryRef  ──►  refcount-- ; if 0 and LRU-oldest:
        │
        ▼
   MarkOldestEntryForEviction  ──►  flag node (+4 bits) ; RemoveEntry frees payload
```

| Operation | Address | Role | Confidence |
|---|---|---|---|
| `TpuCompilationCacheInterface::InsertEntry` | `0xeaacde0` | Add a `CompiledSubgraph` to the cache | HIGH |
| `TpuCompilationCacheInterface::RemoveEntry` | `0xeaac840` | Remove by key | HIGH |
| `TpuCompilationCacheInterface::Release` | `0xeaabde0` | Drop a held reference by uid | HIGH |
| `TpuCompilationCacheInterface::DiscardEntryRef` | `0xeaac500` | Decrement entry refcount | HIGH |
| `TpuCompilationCacheInterface::MarkOldestEntryForEviction` | `0xeaac340` | LRU eviction candidate | HIGH |
| `TpuCompilationCacheExternal::InitializeEntry` | `0xe977c40` | Populate an entry via compile callback | HIGH |
| `DoTpuExecute` (consumes entry) | `0xe6fa5a0` | Holds `AsyncValueRef<TpuCompilationCacheEntry>` during run | HIGH |

A `tfrt::tpu::TpuCompilationCacheEntry` (ctor 0xf7bd100) wraps an `AsyncValueRef<Program>` plus the metadata an executor needs without touching the program bytes: the `HloInputOutputAliasConfig`, the input/output `xla::Shape` vectors, the `CompilerMetadata`, the `HostTransferProto` list, an optional `DeviceAssignment`, the FDO config, and the `PjrtExecutableContext`. The `Program` itself (ctor 0xf7bc500) is the `shared_ptr<const tpu::TpuCoreProgram>` payload — the object that [tpu-program-serialization.md](tpu-program-serialization.md) documents.

> **GOTCHA —** the entry holds the program as a `shared_ptr<const TpuCoreProgram>`, and `DoTpuExecute` takes an `AsyncValueRef` to the *entry*, not to the program. Eviction therefore cannot free a program that is mid-execution: the executing reference keeps the entry (and transitively the program) alive even after `MarkOldestEntryForEviction` flags it. A reimplementation that frees the program on eviction-flag rather than on last-ref will use-after-free under concurrent execute+evict.

---

## How the Layers Compose

A single `PJRT_Client_Compile` call (the PJRT entry point; see [client-and-device.md](../pjrt/client-and-device.md) and the phased compile in [ext-compile-phasecompile.md](../pjrt/ext-compile-phasecompile.md)) threads through all three layers:

```text
PJRT_Client_Compile(program, options)
   │
   ├─ Layer 1: DeviceCompilationClusterSignature::Build(fn, args)   // 0xfb01f40
   │            LookupOrCreate(sig)  ─ hit? ─► return cached executable
   │                                  │ miss → compile_state=kCompiling, fall through
   │            ▼
   ├─ Layer 2: TpuCompile_CreateCompilationCacheKey(property, mesh) // 0xf6a2080
   │            key = to_string(Fingerprint2011(prefix_of_13_fields + device_assignment))
   │            ▼
   ├─ Layer 3: TpuCompilationCache::LookUpInternal(ConvertToBaseKey(key))  // 0xf7a72c0
   │            tier1 in-mem ─ tier2 coord-svc ─ tier3 persistent("CL<fp>_<base>")
   │              │ all miss
   │              ▼
   │            CompileGroup → MaybeCompileGroup (re-dedup) → compile → EmplaceGroupEntry
   │            (optionally write persistent record via RecordWriter)
   │            ▼
   └─ Layer 1: Store(sig, kCompiled, status, compilation_result, executable)  // 0xe994a60
                return PjRtLoadedExecutable
```

The redundancy is intentional: Layer 1 catches re-compiles of the *same XLA cluster* cheaply (no fingerprinting), Layer 2 produces the *target-specific* identity, and Layer 3 catches cross-process and cross-run reuse via coordination service and disk. Each layer's miss is the next layer's lookup.

---

## Related Components

| Name | Relationship |
|---|---|
| `TpuExecutable` / `TpuProgram` serialization | The opaque payload stored in a hit entry; wire format |
| `DeviceCompiler<T>::CompileStrict` | The miss path Layer 1 delegates the real compile to |
| `Fingerprint2011` / `FingerprintCat2011` | The hash primitives every key digest is built on |
| Coordination service | Tier-2 multi-host cache sharing in `LookUpWithCoordService` |
| PJRT client compile | The public entry point that drives all three layers |

## Cross-References

- [TpuProgram Serialization](tpu-program-serialization.md) — the `TpuExecutable`/`TpuProgram` wire format stored inside a cache hit; this page treats it as an opaque payload
- [Compile Phases](compile-phases.md) — the expensive end-to-end pipeline a cache hit lets you skip
- [Compiler Overview](overview.md) — the IR-layer stack the cached program is the output of
- [Custom-Call Lowering](custom-call-lowering.md) — `tpu_custom_call` carries its own `MosaicMlirCacheEntry` sub-cache, distinct from the program cache
- [PJRT Client and Device](../pjrt/client-and-device.md) — the `PJRT_Client_Compile` entry point that consults Layer 1
- [PJRT Phased Compile Extension](../pjrt/ext-compile-phasecompile.md) — the multi-phase compile API that re-serializes between phase boundaries
- [Executable Execution](../pjrt/executable-execution.md) — `DoTpuExecute` pins a cache entry for the duration of a run
