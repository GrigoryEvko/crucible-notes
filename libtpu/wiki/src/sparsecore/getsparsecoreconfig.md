# GetSparseCoreConfig — The Offload Op-Type Enum Source

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.text`/`.rodata` addresses are virtual; for this binary `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset `0x84a0000`, and `.data.rel.ro` VMA − `0x200000` == file offset.*

## Abstract

`GetSparseCoreConfig` is the one resolver that every SparseCore-offload classification site funnels through: given an async HLO instruction, it parses the instruction's `BackendConfig` and hands back a fully-materialized `xla::jellyfish::SparseCoreConfig` proto (a copy, never a borrowed pointer). The single field the scheduler cares about is **field 2 `offload`**, a `TYPE_ENUM` of type `.xla.jellyfish.Offload`, stored in the C++ object at `+0x24` with its presence has-bit at `+0x10` mask `0x4`. That one enum decides whether an offloaded async op consumes a SparseCore gather / scatter / data-formatting / kernel / sort resource lane, recurses into a wrapped collective, or falls through to the per-core general SparseCore resource — i.e. *which physical SparseCore engine class the latency-hiding scheduler throttles the op against*.

This page recovers three byte-exact things. First, the `GetSparseCoreConfig` resolver itself (`0x1c868d20`): its thread-name guard (`kSparseCoreThread` / `kSparseCoreOffloadCandidateThread`), its copy-construction of `SparseCoreConfig` with a globals fallback, and the consumer-side read of `offload` at `+0x24` behind has-bit `+0x10 & 4`. Second, the full `xla::jellyfish::Offload` enum (`OFFLOAD_UNSPECIFIED` 0 .. `OFFLOAD_COMPUTE` 8) and exactly which enumerator maps to which scheduler resource arm (`enum − 2` indexing in the scheduler, `enum − 1` in the reservation map). Third, the SC-offload **gate bits** — `Target[+0x628] & 4` (an SC-offload-capability has-bit) and `Target[+0x540]` (a `platform_type == 2` bool) — traced to where `jellyfish::Target::Init` sets them, plus the per-generation default basis (`TpuVersion == 5`).

For reimplementation, the contract is:

- `GetSparseCoreConfig(async_start)` → `SparseCoreConfig`; the consumer reads `offload` (field 2, object `+0x24`) only when has-bit `+0x10 & 4` is set, else the op classifies as no SC resource.
- The `offload` enumerator selects the SC resource lane: `GATHER`/`SCATTER`/`DATA_FORMATTING`/`KERNEL`/`SORT` → `kSparseCore{Gather,Scatter,DataFormatting,Kernel,Sort}` (scheduler ids 23/24/25/26/27); `COLLECTIVE` → recurse into the async-wrapped instruction; `UNSPECIFIED`/`EMBEDDING`/`COMPUTE` → no `{23..27}` lane (the general id-22 path).
- The SC-offload scheduler sub-pass runs only when the gate holds: `Megachip ∧ CoresPerChip(SC) > 0 ∧ (Target[+0x628] & 4 ∨ Target[+0x540]) ∧ ModuleContainsLEMSparseCoreInstruction ∧ FLAGS_xla_sc_enable_latency_hiding_scheduler`.
- On real hardware the gate bit `Target[+0x628] & 4` is set per-generation; the SC-offload-concurrency defaults key on `TpuVersion == 5` (the newest generation), overridable by a TCE `AutoOr<bool>` flag.

| | |
|---|---|
| **Resolver** | `xla::jellyfish::backend_config_util::GetSparseCoreConfig` @ `0x1c868d20` |
| **Returns** | `xla::jellyfish::SparseCoreConfig` (copy; ctor-copy @ `0x1d6df7c0`) |
| **Classifier field** | `offload` (field 2, enum `.xla.jellyfish.Offload`), object `+0x24`, has-bit `+0x10 & 4` |
| **Offload enum** | 9 enumerators, `OFFLOAD_UNSPECIFIED` 0 .. `OFFLOAD_COMPUTE` 8 |
| **Scheduler consumer** | `TpuAsyncTracker::MayAddSparseCoreResource` @ `0x11000480` (index `enum − 2`) |
| **Reservation consumer** | `(anon)::GetSparseCoreResources` @ `0x10fdc0a0` (index `enum − 1`) |
| **Gate bits** | `Target[+0x628] & 4` (SC-offload-capability) ∨ `Target[+0x540] != 0` (`platform_type == 2`) |
| **Gate site** | `SparseCoreCompiler::RunHloScheduler` @ `0x1306f820` |
| **Bits set in** | `jellyfish::Target::Init` @ `0x1d60fc20` |
| **Per-gen default** | `TpuVersion == 5` (`ShouldEnableConcurrentSparseCoreOffloading` @ `0x1d6b6f80`) |
| **Source file** | `platforms/xla/service/jellyfish/lowering/backend_config_util.cc` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Resolver — `GetSparseCoreConfig` @ `0x1c868d20`

The resolver takes the async-start HLO and returns a `SparseCoreConfig` by value (caller-provided sret buffer). It is *not* a pointer accessor: it copy-constructs a fresh proto, so the consumer's `offload`/`has-bit` reads are on a private copy that outlives the instruction's backend-config arena.

```c
// backend_config_util::GetSparseCoreConfig(this_sret, async_start)  @ 0x1c868d20
SparseCoreConfig *GetSparseCoreConfig(SparseCoreConfig *this, const HloInstruction *a2) {
    if (!a2) { SparseCoreConfig::SparseCoreConfig(this, /*arena=*/0); return this; }  // null → default
    CHECK(a2->opcode() == kAsyncStart);                          // [a2+0xc] == 17  (src line 472)
    // thread-name guard (src line 475):
    auto t = a2->async_execution_thread();
    CHECK(t == "sparsecore"                                      // len 10 "sparsecore"
       || t == kSparseCoreOffloadCandidateThread);               // len 28
    auto cfg = a2->backend_config<jellyfish::BackendConfig>();    // StatusOr<BackendConfig>
    if (cfg.ok()) {
        const SparseCoreConfig *src = cfg->sparse_core_config();
        if (!src) src = &SparseCoreConfig_globals_;               // default-instance fallback
        SparseCoreConfig::SparseCoreConfig(this, /*arena=*/0, src);// copy-construct
    } else {
        SparseCoreConfig::SparseCoreConfig(this, /*arena=*/0);     // parse failed → default
    }
    /* unref the StatusOr's status rep, destroy the BackendConfig temp */
    return this;
}
```

Two guards bracket the parse:

- **Opcode guard.** The instruction must be `kAsyncStart` (opcode `17` = `0x11`). The `CHECK` message is `"async_start->opcode() == HloOpcode::kAsyncStart"` (src line 472).
- **Thread-name guard.** The async-execution thread must be `"sparsecore"` (the 10-byte `kSparseCoreThread`, compared via the immediate `0x6F63657372617073` = `"sparsec"` + `0x6572` = `"or"` low bytes) **or** the 28-byte `kSparseCoreOffloadCandidateThread`. This is the SIMD `vptest` compare in the disassembly; the `CHECK` message names both threads (src line 475).

> **NOTE — the resolver returns a default, never a null pointer.** On a null instruction, a non-`kAsyncStart` opcode (in the `MayAdd*` callers it is pre-filtered, not `CHECK`ed), a parse failure, or a `BackendConfig` with no `sparse_core_config` sub-message, the result is a default-constructed `SparseCoreConfig` — all has-bits clear. The consumer's `has_offload()` test (`+0x10 & 4`) is therefore the real gate: a default proto has the bit clear and classifies as "no SC resource". A reimplementation must not treat the resolver as fallible at the call site; the fallibility is folded into the cleared has-bits.

---

## The `SparseCoreConfig` Proto — Field Map

`GetSparseCoreConfig` returns the full `SparseCoreConfig`; only `offload` (field 2) is read by the scheduler/reservation classifiers, but the complete field map (from `SparseCoreConfig::_InternalSerialize` @ `0x1d6dfae0`) fixes the object layout and confirms `offload` lives at object `+0x24` behind has-bit `+0x10 & 4`.

| field | name | object off | has-bit (`obj+0x10`) | proto type | Confidence |
|---|---|---|---|---|---|
| 1 | `tiling` | `+0x20` (i32) | `0x2` | enum `.xla.jellyfish.Tiling` | CONFIRMED |
| 2 | **`offload`** ← the SC op-type enum | `+0x24` (i32) | `0x4` | enum `.xla.jellyfish.Offload` | CONFIRMED |
| 3 | `comp_env` | `+0x18` (msg) | `0x1` | msg `ScCompilationEnvironment` | CONFIRMED |
| 4 | `enable_megacore` | `+0x2c` (bool) | `0x10` | bool | CONFIRMED |
| 5 | `hbm_bandwidth_adjustment_factor` | `+0x28` (f32) | `0x8` | float (fixed32) | CONFIRMED |
| 6 | `function_mode` | `+0x2d` (byte) | `0x20` | enum/bool | CONFIRMED |
| 7 | `dedup_id` | `+0x30` (i64) | `0x80` | int64 | CONFIRMED |
| 8 | `enable_program_barrier` | `+0x2e` (bool) | `0x40` | bool | CONFIRMED |
| 9 | `load_dat` | `+0x38` (bool) | `0x100` | bool | CONFIRMED |

The `offload` `FieldDescriptorProto` is carved byte-exact: tag `0a07` (`"offload"`, 7 bytes), `1802` (number = 2), `2001` (label = optional), `280e` (type = `TYPE_ENUM` = 14), `3216` (`.xla.jellyfish.Offload`, 0x16 bytes). The descriptor type strings `.xla.jellyfish.Offload`, `.xla.jellyfish.Tiling`, and the field names `enable_megacore` / `hbm_bandwidth_adjustment_factor` / `function_mode` / `dedup_id` / `enable_program_barrier` / `load_dat` are all present in `.rodata`.

> **GOTCHA — `offload` is a backend-config enum, not a custom-call target or MLIR op kind.** The op-type classification keyed by `GetSparseCoreConfig` is a *proto field on the instruction's backend config*. This is a different mechanism from the plain `SparseCoreAsyncTracker`, which keys on the custom-call *target string* (`"AllToAllDynamic"`) via a separate enum (`SparseCoreOperationType`) — see [The Plain Tracker Keys on a Target Name, Not `offload`](#the-plain-tracker-keys-on-a-target-name-not-offload). Do not conflate the two: `offload` (this page) and `SparseCoreOperationType` are distinct enums with distinct value spaces and distinct consumers.

---

## The `xla::jellyfish::Offload` Enum

Decoded byte-exact from the `EnumDescriptorProto` (each value is a `10 NN` `EnumValueDescriptorProto`). All nine enumerator name strings (`OFFLOAD_UNSPECIFIED` .. `OFFLOAD_COMPUTE`) are present in `.rodata`.

| value | enumerator | semantic | Confidence |
|---|---|---|---|
| 0 | `OFFLOAD_UNSPECIFIED` | unset / default | CONFIRMED |
| 1 | `OFFLOAD_EMBEDDING` | embedding lookup/update offload | CONFIRMED |
| 2 | `OFFLOAD_GATHER` | gather op-class | CONFIRMED |
| 3 | `OFFLOAD_SCATTER` | scatter op-class | CONFIRMED |
| 4 | `OFFLOAD_COLLECTIVE` | collective (recurse into wrapped op) | CONFIRMED |
| 5 | `OFFLOAD_DATA_FORMATTING` | data-formatting op-class | CONFIRMED |
| 6 | `OFFLOAD_KERNEL` | generic SC kernel op-class | CONFIRMED |
| 7 | `OFFLOAD_SORT` | sort op-class | CONFIRMED |
| 8 | `OFFLOAD_COMPUTE` | compute offload | CONFIRMED |

---

## Scheduler Consumer — `MayAddSparseCoreResource` @ `0x11000480`

`TpuAsyncTracker::MayAddSparseCoreResource` is the producer that turns an offloaded async op into scheduler resource ids. It first re-applies the same thread guard (async + thread `"sparsecore"` / `kSparseCoreOffloadCandidateThread`, else return), calls `GetSparseCoreConfig`, then switches on `offload` **only if the has-bit is set**. The switch index is `offload` directly (the `add 0xfffffffe` / jump table covers values `{2..7}`, i.e. `enum − 2` after the table base).

```c
// MayAddSparseCoreResource(this, async_start, &out)  @ 0x11000480  (condensed)
SparseCoreConfig cfg; GetSparseCoreConfig(&cfg, async_chain_start(async_start));
int usage = /* 2 = kResourceOccupy on start, 1 = kResourceRelease on done */;
if (cfg.has_bits & 0x4) {                       // v55 & 4  → has_offload()
    switch (cfg.offload) {                      // v56 = [&cfg + 0x24]
        case 2: out.push_back({23, usage}); break;  // OFFLOAD_GATHER          → kSparseCoreGather
        case 3: out.push_back({24, usage}); break;  // OFFLOAD_SCATTER         → kSparseCoreScatter
        case 4: MayAddSparseCoreResource$_0(&out,    // OFFLOAD_COLLECTIVE      → recurse on
                  async_wrapped_instruction(async_start), usage); break;  //    async-wrapped op
        case 5: out.push_back({25, usage}); break;  // OFFLOAD_DATA_FORMATTING → kSparseCoreDataFormatting
        case 6: out.push_back({26, usage}); break;  // OFFLOAD_KERNEL          → kSparseCoreKernel
        case 7: out.push_back({27, usage}); break;  // OFFLOAD_SORT            → kSparseCoreSort
        default: break;                             // 0/1/8 → no {23..27} arm
    }
}
// independent of the offload switch — the general per-core SparseCore resource (id 22):
if (this[+0x13b] == 1) {                         // a1+315 — "per-core" gate
    for (i = 0; i < GetNumSparseCoresUsed(async_chain_start(async_start)); i++)
        out.push_back({22, usage});              // id 22 once per used SC core
} else {
    out.push_back({22, usage});                  // id 22 once
}
```

| `offload` | enumerator | scheduler arm (`MayAddSparseCoreResource`, idx `enum − 2`) | Confidence |
|---|---|---|---|
| 0 | `OFFLOAD_UNSPECIFIED` | no arm — id 22 path only | CONFIRMED |
| 1 | `OFFLOAD_EMBEDDING` | no arm — id 22 path only | CONFIRMED |
| 2 | `OFFLOAD_GATHER` | id 23 `kSparseCoreGather` | CONFIRMED |
| 3 | `OFFLOAD_SCATTER` | id 24 `kSparseCoreScatter` | CONFIRMED |
| 4 | `OFFLOAD_COLLECTIVE` | recurse into `async_wrapped_instruction` (`$_0` @ `0x110008e0`) | CONFIRMED |
| 5 | `OFFLOAD_DATA_FORMATTING` | id 25 `kSparseCoreDataFormatting` | CONFIRMED |
| 6 | `OFFLOAD_KERNEL` | id 26 `kSparseCoreKernel` | CONFIRMED |
| 7 | `OFFLOAD_SORT` | id 27 `kSparseCoreSort` | CONFIRMED |
| 8 | `OFFLOAD_COMPUTE` | no arm — id 22 path only | CONFIRMED |

The resource ids `{22..27}` are the SparseCore engine classes in the 47-id scheduler `ResourceType` enum; see [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) for their concurrency caps and hazard classes.

> **QUIRK — `OFFLOAD_COLLECTIVE` recurses, it does not emit a lane.** Value 4 is the one arm that does not push a `kSparseCore*` id. Instead it re-enters `MayAddSparseCoreResource` (the `$_0` lambda at `0x110008e0`) on the *async-wrapped* instruction, so a collective wrapped inside an SC-offload async op is classified by the wrapped op's own `offload` field. A reimplementation that maps value 4 to a resource id will double-count or mis-throttle wrapped collectives.

> **NOTE — the id-22 general path is independent of `offload`.** The per-core `kSparseCore` (id 22) emission runs *after* the `offload` switch and is gated by a separate byte (`this+0x13b == 1`), which selects "one id-22 per used SC core" (`GetNumSparseCoresUsed`) vs "one id-22". So even `OFFLOAD_UNSPECIFIED`/`EMBEDDING`/`COMPUTE` ops — which hit no `{23..27}` arm — still consume the general SparseCore resource. The `offload` enum refines *which* sub-engine; id 22 is the always-present per-core occupancy.

---

## Reservation Consumer — `GetSparseCoreResources` @ `0x10fdc0a0`

The reservation-map twin reads the **same** `SparseCoreConfig.offload` field (same `+0x24` value, same `+0x10 & 4` has-bit), but indexes with `enum − 1` (a `dec` then `cmp 6`), so its live range is `{1..7}` — it additionally covers `OFFLOAD_EMBEDDING` (value 1) in its own arm.

```c
// (anon)::GetSparseCoreResources(async_start)  @ 0x10fdc0a0  (condensed)
SparseCoreConfig cfg; GetSparseCoreConfig(&cfg, async_start);
if ((cfg.has_bits & 4) != 0) {                  // v29[16] & 4  → has_offload()
    switch (cfg.offload) {                      // index = enum, table base enum−1, range {1..7}
        case 1: /* OFFLOAD_EMBEDDING → embedding/general reservation arm */ ...
        case 2: /* OFFLOAD_GATHER          → kSparseCoreGather   */ ...
        case 3: /* OFFLOAD_SCATTER         → kSparseCoreScatter  */ ...
        case 4: /* OFFLOAD_COLLECTIVE      → collective arm      */ ...
        case 5: /* OFFLOAD_DATA_FORMATTING → kSparseCoreDataFmt  */ ...
        case 6: /* OFFLOAD_KERNEL          → kSparseCoreKernel   */ ...
        case 7: /* OFFLOAD_SORT            → kSparseCoreSort     */ ...
    }
}
```

> **GOTCHA — scheduler indexes `enum − 2`, reservation indexes `enum − 1`.** The two consumers read the identical proto field at the identical offset but with a one-off difference in jump-table base. The scheduler (`MayAddSparseCoreResource`) starts its dense table at value 2 (`OFFLOAD_GATHER`), so `OFFLOAD_EMBEDDING` (1) hits no scheduler arm; the reservation map (`GetSparseCoreResources`) starts at value 1, so it does reserve for embedding. A reimplementer must keep both index bases: do not assume the two classifiers share a switch.

See [SC Queue Assignment & Reservation](sc-queue-assignment-reservation.md) for the reservation-map's resource→limit structure.

---

## The SC-Offload Gate Bits

The SparseCore-offload scheduler sub-pass runs only when `SparseCoreCompiler::RunHloScheduler` (@ `0x1306f820`) finds the gate predicate true. Two of its conjuncts are `Target` bitfield reads:

```c
// SparseCoreCompiler::RunHloScheduler gate  @ 0x1306f820  (object offsets in bytes)
runSC =  TpuChipConfig::Megachip( Target[+0x3b8][+0x18] )                 // @0x1306f84c
      && *(int*)( Target[+0x3b8] + 0x94 ) > 0       // CoresPerChip(kSparseCore) > 0  @0x1306f863
      && ( (Target[+0x628] & 4) != 0  ||  Target[+0x540] != 0 )           // the two gate bits
      && offloader_util::ModuleContainsLEMSparseCoreInstruction(M)        // @0x1306fbc8
      && FLAGS_xla_sc_enable_latency_hiding_scheduler;                    // @0x1306fc04
```

In the decompile this is `(*((_BYTE*)this + 1576) & 4) != 0 || *((_BYTE*)this + 1344)` — byte `1576 = 0x628`, byte `1344 = 0x540`. The `*(int*)(... + 148) > 0` is the `0x94` CoresPerChip(SC) read. The whole predicate appears twice (the eager check and the SC-path re-test).

### Where the bits are set — `Target::Init` @ `0x1d60fc20`

Both fields are written inside `jellyfish::Target::Init`. The register `r12`/`v342` is the `Target*` being initialized; `v98`/`*v98` is the first scalar of the `TpuTopology` (the platform-type enum).

```c
// jellyfish::Target::Init  @ 0x1d60fc20  (relevant writes)
Target[+0x540] = (TpuTopology[+0] == 2);     // platform_type == 2   (decompile: _R12+1344)
Target[+0x541] = (TpuTopology[+0] == 1);     // platform_type == 1   (decompile: _R12+1345)
// inside the predicate-gated config-append loop:
Target[+0x628] |= 1;                         // bit-0  (config sub-field A has-bit)  @0x1d611d52
Target[+0x628] |= 4;                         // bit-2  (SC-offload-capability has-bit) @0x1d612121
```

- **`Target[+0x540]`** is a `bool` = `(TpuTopology[+0] == 2)`. The first `TpuTopology` scalar is the internal platform-type enum; value `2` is the `iss` (simulator) path (`platform_type == 1` lands in the sibling byte `Target[+0x541]`). So `Target[+0x540] != 0` force-takes the SC path on the simulator regardless of the capability bit.
- **`Target[+0x628]`** is a `_has_bits_`-style qword (decompile `*((_QWORD*)_R12 + 197)` — qword index 197 = byte `0x628`). Bit-2 (mask `0x4`) is OR'd in inside an unrolled config-append loop that is itself gated by the SC-offload feature-detect; it is the **SC-offload-capability has-bit**, set for the eligible (newest-gen) part. Bit-0 (mask `0x1`) is OR'd earlier in the same loop for a sibling config sub-field. The gate predicate `(Target[+0x628] & 4) == 0 → read Target[+0x540]` is replayed verbatim inside `Target::Init` itself (combined with `Megachip ∧ CoresPerChip(SC) > 0`) — the SC-offload feature-detect.

| gate bit | object off | meaning | set in `Target::Init` | Confidence |
|---|---|---|---|---|
| `Target[+0x628] & 4` (bit-2) | `+0x628` qword | SC-offload-capability has-bit (per-gen, predicate-gated) | OR'd `|= 4` @ `0x1d612121` | CONFIRMED |
| `Target[+0x540]` | `+0x540` byte | `platform_type == 2` (iss/simulator) | `= (TpuTopology[+0] == 2)` @ `0x1d610b1b` | CONFIRMED |
| `Target[+0x541]` | `+0x541` byte | `platform_type == 1` (sibling, not in gate) | `= (TpuTopology[+0] == 1)` @ `0x1d610b29` | CONFIRMED |
| `Target[+0x628] & 1` (bit-0) | `+0x628` qword | sibling config-append has-bit (not in gate) | OR'd `|= 1` @ `0x1d611d52` | CONFIRMED |

> **NOTE — the exact proto sub-field naming bit-2 was not isolated.** The bit-set site, mask, and value are byte-exact, and the bit sits in the predicate-gated config-append loop alongside bit-0 and two SSO strings (object `+0x580`/`+0x5f0`). But the single descriptor entry that names this SC-offload-capability sub-field (a nested config field copied from the chip's `vector_isa`/`TpuSequencerParts`) was not pinned to one descriptor. The *role* — an SC-offload-capability flag the scheduler gate reads — is byte-exact regardless. **Confidence: bit position CONFIRMED; sub-field proto name INFERRED.**

> **GOTCHA — `platform_type` enum order is descriptor-string order.** `TpuTopology[+0]` is the topology's first scalar = the internal `TpuPlatformType` enum (per the `ValidateArgs(TpuPlatformType, …)` signature and the proven `TpuTopology[+0x8] = TpuChipParts*` layout). The gate compares it `== 2`; the value→name pairing `{0 hardware, 1 grm, 2 iss}` is taken from descriptor-string order (`TpuPlatformTypeToProto = type + 1`), not a separately decoded `platform_type()` getter. **Confidence: the `== 2` comparison and its gate role CONFIRMED; the enum value→name pairing INFERRED.**

---

## Per-Generation Default Basis — `TpuVersion == 5`

The two SC-offload-concurrency knobs that feed the scheduler default to *enabled* on exactly one chip generation. Both compute their hardware default as `TpuChipParts[+0] == 5` (i.e. `tpu::TpuVersion == 5`), then let a `TpuCompilationEnvironment` `AutoOr<bool>` flag override via the `0x100` "is-set" bit.

```c
// ShouldEnableConcurrentSparseCoreOffloading(tce_view, topo, b)  @ 0x1d6b6f80
hw_default = (TpuChipParts[+0] == 5) & ~b;          // *(_DWORD*)(topo+8) == 5  → TpuVersion 5
flag = tce[+0x458] ? tce[+0x458] : &AutoProto_globals_;   // a1 + 1112 = 0x458
v = AutoOr<bool>::FromProtoOrDie(flag);
return (v & 0x100) ? /*flag set → use flag value*/ : hw_default;

// EnableSparseCoreOffloadQueuingInLhs(tce_view, topo)  @ 0x1d6b81e0
hw_default = (TpuChipParts[+0] == 5);               // *(_DWORD*)(topo+8) == 5
flag = tce[+0x730] ? tce[+0x730] : &AutoProto_globals_;   // a1 + 1840 = 0x730
... same AutoOr<bool> 0x100 override ...
```

`TpuChipParts[+0]` is the `TpuVersion` (the 0-based chip-generation enum); `TpuChipParts::ToProto` → `TpuVersionToProto(v) = v + 1` (confirmed: the decompiled body is literally `return v + 1`). The codename table from `TpuVersionFromString` (@ `0x20b3a5a0`, init-list @ `0x220117b0`):

| TpuVersion (internal) | codename | proto value (`= internal + 1`) | Confidence |
|---|---|---|---|
| 0 | `jellyfish` | 1 | CONFIRMED |
| 1 | `dragonfish` | 2 | CONFIRMED |
| 2 | `pufferfish` | 3 | CONFIRMED |
| 3 | `viperfish` | 4 | CONFIRMED |
| 4 | `ghostlite` | 5 | CONFIRMED |
| 5 | (codename obfuscated in v0.0.40) | 6 ← SC-offload concurrency default ON | CONFIRMED |

The five readable codenames are present in `.rodata`; the value-5 codename is obfuscated in this build. See [TPU Version Codename Matrix](../targets/tpu-version-codename-matrix.md) for the full generation map.

> **NOTE — the override flag field numbers were not decoded.** Both knobs read an `AutoOr<bool>` at TCE `_impl_` offsets `0x458` (concurrency) / `0x730` (offload-queuing-in-LHS), falling back to `AutoProto_globals_` when unset; the offsets and the `0x100`-bit override are byte-exact, but the two proto **field numbers** (the `_InternalSerialize` tags at those offsets) were not isolated. **Confidence: offsets + override mechanism CONFIRMED; field numbers PARTIAL.** See [TpuCompilationEnvironment](../config/tpu-compilation-environment.md) and [TCE Field Offsets & Defaults](../config/tce-field-offsets-defaults.md).

---

## The Plain Tracker Keys on a Target Name, Not `offload`

Not every SparseCore scheduler keys on `GetSparseCoreConfig`. When the gate holds, the SC-offload schedule is produced by a *plain* `SparseCoreAsyncTracker` first, and that tracker classifies async-schedulable ops by **opcode + custom-call target name**, not the `offload` backend enum. This is the cleanest way to see that `offload` is one of two distinct SC classification mechanisms in the binary.

```c
// SparseCoreAsyncTracker::IsSupportedAsyncStart(hlo)  @ 0x134964c0
bool IsSupportedAsyncStart(const HloInstruction *h) {
    int op = h->opcode();                          // [h+0xc]
    if (op == 12) return true;                     // 0xc  all-to-all
    if (op == 17) return true;                     // 0x11 async-start
    if (op == 49)                                  // 0x31 custom-call
        return SparseCoreOperationTypeFromString(h->custom_call_target()) == 8;  // "AllToAllDynamic"
    return false;
}
// IsSupportedAsyncDone @ 0x13496520 is identical except op 16 (0x10 async-done) replaces 17.
```

`SparseCoreOperationTypeFromString` (@ `0x14b7f060`) is a chained `EqualsIgnoreCase` mapper over a *separate* enum, `SparseCoreOperationType`, whose first eight values are confirmed in order: `1 SparseMap`, `2 CooToCsr`, `3 CooToEll`, `4 SparseMapRow`, `5 SortLexicographic`, `6 ReduceDuplicates`, `7 EllToCsr`, `8 AllToAllDynamic` (then `9 ScSendToTc`, `10 ScReceiveFromTc`, … continuing well past 8). The plain tracker gates only on `== 8` (`"AllToAllDynamic"`) — it overlaps SC all-to-all ops, and (via `PostProcessScheduleGraph` → `FindNearestAllToAlls` @ `0x13496600`) biases the schedule toward them.

| classifier | keyed on | enum | values used | Confidence |
|---|---|---|---|---|
| `MayAddSparseCoreResource` (this page) | `SparseCoreConfig.offload` (field 2) | `xla::jellyfish::Offload` | `{2..7}` → ids `{23..27}` + recurse | CONFIRMED |
| `GetSparseCoreResources` (reservation) | `SparseCoreConfig.offload` (field 2) | `xla::jellyfish::Offload` | `{1..7}` | CONFIRMED |
| `SparseCoreAsyncTracker::IsSupportedAsync{Start,Done}` | custom-call target string | `SparseCoreOperationType` | `== 8` (`AllToAllDynamic`) | CONFIRMED |

> **QUIRK — two enums, one word "SparseCore op type".** `xla::jellyfish::Offload` (a backend-config enum, 9 values, drives resource lanes) and `SparseCoreOperationType` (a custom-call target-name enum, ≥ 8 values, drives async-schedulability) are easy to conflate because both describe "what kind of SparseCore op this is". They are wholly separate: different namespaces, different value spaces, different read paths, different consumers. `GetSparseCoreConfig` resolves the first; `custom_call_target()` + `SparseCoreOperationTypeFromString` the second.

---

## Worked Example — An `OFFLOAD_SCATTER` Async Op on a Newest-Gen Part

A SparseCore-offloaded scatter, on a megacore newest-generation (`TpuVersion == 5`) part:

```text
%sc = async-start(%coo), execution_thread="sparsecore",
        backend_config = { sparse_core_config { offload: OFFLOAD_SCATTER } }
%sc.d = async-done(%sc)
```

Walking the resolver and the classifiers:

- **Gate.** `Target::Init` set `Target[+0x628] |= 4` for the newest-gen part, so `(Target[+0x628] & 4) != 0` is true; with `Megachip`, `CoresPerChip(SC) > 0`, an LEM SparseCore instruction in the module, and the LHS flag on, `SparseCoreCompiler::RunHloScheduler` enters the SC-offload sub-passes.
- **Resolve.** For `%sc`, `MayAddSparseCoreResource` re-checks the thread (`"sparsecore"`, len 10 — passes), calls `GetSparseCoreConfig`, which copy-constructs the `SparseCoreConfig`. `has_offload()` (`+0x10 & 4`) is set; `offload` (`+0x24`) reads `3` (`OFFLOAD_SCATTER`).
- **Classify.** The switch on `3` emits `{24, kResourceOccupy}` — scheduler resource id 24 (`kSparseCoreScatter`) — on the async-start, and the matching `{24, kResourceRelease}` on `%sc.d`. Independently, the id-22 path emits the general per-core `kSparseCore` occupancy.
- **Throttle.** The scheduler caps concurrent id-24 ops at `GetNumAvailableResources(24)` (the `..._scatter_overlap_limit` TCE knob) and treats id 24 as unsharable (hazard 0) — see [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md).
- **Default-instance contrast.** Had the backend config omitted `sparse_core_config`, `GetSparseCoreConfig` would return the default instance, `has_offload()` would be clear, and `%sc` would consume only the general id-22 lane — no scatter-specific throttle.

This is exactly why `offload` is the SC op-type classifier: it is the one proto field that refines a generic SparseCore async op into a specific engine-class resource the scheduler can model.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `GetSparseCoreConfig` returns a copy-constructed `SparseCoreConfig` (globals fallback) | `0x1c868d20`: `SparseCoreConfig::SparseCoreConfig(this, 0, src)`, `SparseCoreConfig_globals_` | CONFIRMED |
| Thread guard: `"sparsecore"` (len 10) ∨ `kSparseCoreOffloadCandidateThread` (len 28); opcode `kAsyncStart` | `0x1c868d20` SIMD compare + `CHECK` strings (src 472/475) | CONFIRMED |
| `offload` = field 2, enum `.xla.jellyfish.Offload`, object `+0x24`, has-bit `+0x10 & 4` | `_InternalSerialize` @ `0x1d6dfae0`; `FieldDescriptorProto` carve; consumer reads | CONFIRMED |
| Offload enum `OFFLOAD_UNSPECIFIED` 0 .. `OFFLOAD_COMPUTE` 8 (9 values) | `EnumDescriptorProto` @ `0xbfa1f9f`; all 9 strings in `.rodata` | CONFIRMED |
| Scheduler arm map `{2→23, 3→24, 4→recurse, 5→25, 6→26, 7→27}`, idx `enum − 2` | `MayAddSparseCoreResource` @ `0x11000480` switch (decompiled) | CONFIRMED |
| Reservation map reads same field, idx `enum − 1`, range `{1..7}` (covers `EMBEDDING`) | `GetSparseCoreResources` @ `0x10fdc0a0`: `v29[16] & 4`, `case 1..7` | CONFIRMED |
| id-22 (`kSparseCore`) path is independent of `offload`, gated `this+0x13b == 1` | `0x11000480`: post-switch loop on `GetNumSparseCoresUsed` | CONFIRMED |
| Gate predicate `(Target[+0x628] & 4) ∨ Target[+0x540]` plus Megachip/CoresPerChip(SC)/LEM/flag | `RunHloScheduler` @ `0x1306f820`: `this+1576 & 4`, `this+1344`, `+148 > 0` | CONFIRMED |
| `Target[+0x540] = (TpuTopology[+0] == 2)`; `Target[+0x541] = (== 1)` | `Target::Init` @ `0x1d60fc20` lines `_R12+1344/+1345 = *v98 == 2/1` | CONFIRMED |
| `Target[+0x628] |= 4` (bit-2) and `|= 1` (bit-0) in predicate-gated config loop | `Target::Init`: `v342+197 = … | 4` / `| 1` (qword 197 = `0x628`) | CONFIRMED |
| The proto sub-field that names bit-2 (SC-offload-capability) | bit-set site byte-exact; descriptor entry not isolated | INFERRED |
| `platform_type` value→name pairing `{0 hardware, 1 grm, 2 iss}` | descriptor-string order + `ToProto = type+1`; `== 2` comparison byte-exact | INFERRED (pairing) / CONFIRMED (`== 2`) |
| Per-gen default `TpuVersion == 5`; `AutoOr<bool>` 0x100 override at TCE `+0x458`/`+0x730` | `0x1d6b6f80` (`*(topo+8)==5`, `a1+1112`); `0x1d6b81e0` (`a1+1840`) | CONFIRMED |
| TCE override field numbers for the two SC-offload knobs | offsets + override byte-exact; field numbers not decoded | PARTIAL |
| TpuVersion 0..5 = jellyfish/dragonfish/pufferfish/viperfish/ghostlite/(obfuscated); proto = +1 | `TpuVersionFromString` init-list @ `0x220117b0`; `TpuVersionToProto` body `v+1` | CONFIRMED |
| Plain `SparseCoreAsyncTracker` keys on opcode `{0xc, 0x11/0x10, 0x31}` + `SparseCoreOperationType == 8` | `IsSupportedAsyncStart/Done` @ `0x134964c0`/`0x13496520`; `FromString` @ `0x14b7f060` | CONFIRMED |
| `SparseCoreOperationType` values 1..8 = SparseMap..AllToAllDynamic | `0x14b7f060` chained `EqualsIgnoreCase` (decompiled, in order) | CONFIRMED |

---

## Cross-References

- [SparseCore Overview](overview.md) — the SCS/TAC/TEC engine model the offloaded op-classes target.
- [SparseCore Architecture](architecture.md) — the hardware engines behind the `kSparseCore{Gather,Scatter,DataFormatting,Kernel,Sort}` resource lanes.
- [SC Back-End Pipeline](sc-backend-pipeline.md) — where SC-offload scheduling sits in the SparseCore compile flow.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the gather/scatter datapath that `OFFLOAD_GATHER` / `OFFLOAD_SCATTER` route to.
- [SC Queue Assignment & Reservation](sc-queue-assignment-reservation.md) — the reservation-map (`GetSparseCoreResources`) twin of this classifier.
- [SparseCore vs Neuron MatMultSparse](sparsecore-vs-neuron-matmultsparse.md) — cross-architecture contrast of the sparse offload model.
- [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) — the 47-id scheduler enum; the caps/hazards for ids 22..27 this page emits, and the three-tracker selection gate.
- [LatencyHidingScheduler Core](../sched/latency-hiding-scheduler-core.md) — the list scheduler that consumes the resource ids.
- [TpuCompilationEnvironment](../config/tpu-compilation-environment.md) — the TCE that carries the `AutoOr<bool>` SC-offload override flags (`+0x458` / `+0x730`).
- [TCE Field Offsets & Defaults](../config/tce-field-offsets-defaults.md) — TCE `_impl_` offset table.
- [TPU Version Codename Matrix](../targets/tpu-version-codename-matrix.md) — the `TpuVersion` 0..5 generation map (default-on basis = version 5).
- [TPU Topology Struct](../targets/tpu-topology-struct.md) — the `TpuTopology` whose first scalar (`platform_type`) sets `Target[+0x540]`.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore back-end — [back to index](../index.md)
