# NEFF Feature Flags

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The producer lives in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset). The `ModuleAttribute` enum is defined in `libBIR.so`. Every address was re-walked against the cp310 `libwalrus.so` this session via `nm -DC`, `objdump -d`, and `grep -aob`; treat each as version-pinned (the cp311/cp312 wheels shift by a small per-wheel delta).*

## Abstract

A NEFF declares the runtime capabilities it requires through a **64-bit feature bitmask** stored at `neff_header+192` (the loader's `feature_flags` field — see [Part 12.2](neff-header-bom-writer.md)). The bitmask is the forward/backward-compatibility contract between a compiled NEFF and the Neuron runtime that loads it: a NEFF with bit *X* set requires a runtime that implements feature *X*, and a runtime too old to recognize a set bit must refuse the NEFF. Because the bits are an append-only, fixed-constant encoding, an old runtime can detect a too-new NEFF simply by testing for bits it does not know.

Both the header mask and a parallel human-readable `"neff_features"` string array in each per-core `def.json` ([Part 12.3](neff-json-sidecars.md), in-flight) are produced by **one** function: `NeffPackager::writeNEFFFeatures` (`0x15294b0`). This function is a pure reader/serializer — it sets no attribute. It assembles the mask from three sources: (A) eighteen bits read 1:1 from `bir::ModuleAttribute` enum ordinals via `Module::getAttribute`, each set upstream by the codegen visitor that emitted the feature-bearing instruction; (B) two procedural bits computed inline (`neff_feature_vnc` from the vNC count, `neff_feature_remote_sem` from an IR scan); and (C) one unnamed arch-gated bit (DVE perf mode). The `ModuleAttribute` value is a `boost::variant` whose bool arm engaged means "feature present"; the per-attribute discriminator byte being non-zero is what `writeNEFFFeatures` tests.

The page is structured as: the [11-flag catalog](#the-11-flag-catalog) (the named subset that reaches `def.json`, the deliverable); the [full bit map](#the-full-writenefffeatures-bit-map) of all ~21 header bits with their ordinals and OR constants; the [`ModuleAttribute` encoding](#the-moduleattribute-encoding) (enum, storage type, on-wire surfaces); and the [setting mechanism and the compat-reject contract](#setting-mechanism-and-the-compat-reject-contract). The single most counter-intuitive fact — that `neff_feature_vnc` and `neff_feature_remote_sem` have a header bit and a `def.json` name but are **not** `ModuleAttribute` enum members in this build — is flagged in place.

For reimplementation, the contract is:

- The `writeNEFFFeatures` assembly order: which ordinal feeds which fixed bit constant, the two procedural bits, the arch gates, and the `mov %r14,0xc0(%rax)` store into the header.
- The 11-flag named catalog: bit, ordinal, meaning, the codegen visitor that sets it, and the runtime effect — plus which bits push a `def.json` name and which set only a header bit.
- The `ModuleAttribute` `boost::variant` storage and the two on-wire surfaces (header mask vs. `def.json` array), and the schema-version-bump compat lever.

| | |
|---|---|
| **Producer** | `NeffPackager::writeNEFFFeatures` @ `0x15294b0` (`neff_packager.cpp`; 18 `getAttribute` calls) |
| **Header store** | `mov %r14,0xc0(%rax)` @ `0x1529971` → `NeffFileWriter+192` → `neff_header+192` `feature_flags` (u64) |
| **`def.json` key** | `"neff_features"` (plural) @ `.rodata 0x1c86984`; `operator[]` @ `0x1529a88` |
| **Attribute reader** | `bir::Module::getAttribute@plt` @ `0x600360` (`boost::variant` return) |
| **Attribute setter** | `bir::Module::setAttribute@plt` @ `0x61d170` (36 call sites in `libwalrus`) |
| **Enum source** | `bir::ModuleAttribute` (`libBIR.so`, 23 members `0..22`) |
| **Named flags** | 11 (push a `def.json` name); header bitmask carries up to ~21 bits |
| **Procedural bits** | `vnc` (0x400, `options.vnc_nc_count>1`), `remote_sem` (0x4000, `InstDMABlock` scan) |
| **Schema lever** | `NeffFileWriter+200` bumped to `max(cur,2)` when `large_tensor_support` set or `arch>39` |

---

## The 11-Flag Catalog

These eleven flags are the **named** subset: each has a `.rodata` string in `libwalrus`, each pushes its name into the `def.json` `"neff_features"` array, and (for the nine attribute-backed ones) each maps to a `ModuleAttribute` ordinal. The other ~10 header bits set only a mask bit and have no `def.json` name (catalogued in the [full bit map](#the-full-writenefffeatures-bit-map)).

The "set by" column names the codegen routine that records the attribute via `setAttribute(<ord>, true)` at the cited call site; the `%esi` immediate at that site **is** the ordinal and is byte-confirmed. (The enclosing function names come from the cp310 IDA decompile; the shipped `.so` carries no local symbols, so the function-entry addresses are tagged accordingly — the call-site `%esi` immediates are directly observable.)

| Flag (`def.json` name) | Bit | Ord | Meaning / gated capability | Set by (`setAttribute` site) | Runtime effect | Conf |
|---|---|---|---|---|---|---|
| `collective_has_offset` | `0x000002` | 2 | Collective ops carry a CC-group offset | `visitInstCollectiveSend` `0x1272440` / `Recv` `0x1272ab0` / `Compute` (`esi=2`, 5 sites) | Collective engine applies the per-op offset | CONFIRMED |
| `neff_feature_custom_ops` | `0x000004` | 3 | NEFF carries GPSIMD/Xtensa custom-op (µcode-library) kernels: `ucode_lib.json` manifest + `cpu.so`/`cpu.params` present | `visitInstCustomOp` `0x12613c0`, site `0x12619b0` (`esi=3`) | Provision GPSIMD/Xtensa engine; load µcode lib; `dlopen` `cpu.so` | CONFIRMED |
| `neff_coalesced_ccops` | `0x000008` | 4 | Collective-compute ops coalesced across channels (`coalesce_multichannel_cc_ops` ran) | `visitInstCollectiveCompute` `0x126c380`, sites `0x126e31d`/`0x126e9ad` (`esi=4`) | Collective engine expects coalesced multi-channel CC descriptors | CONFIRMED |
| `neff_queue_set_instances` | `0x000010` | 5 | NEFF uses DMA queue-set instances (parameterised group of DMA queue instances) | `visitInstSwitchQueueInstance` `0x1264240`, site `0x1264274` (`esi=5`, unconditional) | Loader allocates the queue-set instances declared in `def.json` | CONFIRMED |
| `neff_has_functions` | `0x000020` | 6 | Multi-function NEFF: module carries callable `bir::Function` definitions (function/call table) | `visitInstCall` `0x12633e0`, site `0x126343f` (`esi=6`) | Loader builds a function dispatch/call table; supports intra-NEFF calls | CONFIRMED |
| `neff_feature_dynamic_pwp` | `0x000040` | 7 | Runtime-programmable Pool-Window-Program (dynamic, vs compile-time-baked, PWP path) | `LowerPWPImpl::LowerPWPImpl` ctor `0x115d130`, site `0x115da0a` (`esi=7`) | Loader programs the PWP table at load/exec from NEFF data (INFERRED) | CONFIRMED |
| `neff_feature_indirect_memcpy_32b_sem_wait` | `0x000080` | 8 | Indirect-DMA (gather/scatter memcpy) uses a 32-bit semaphore-wait value | `generateIndirectLoadSave` `0x1268c00`, sites `0x12695ef`/`0x1269dbf` (`esi=8`) | DMA queue/semaphore engine treats the wait value as 32-bit | CONFIRMED |
| `neff_feature_indirect_memcpy_bound_check` | `0x000100` | 9 | Indirect-DMA gather/scatter has HW bounds-checking on the index | `generateIndirectLoadSave` `0x1268c00` (`0x1268e6e`) / `visitInstStreamTranspose` `0x1266df0` (`0x1266ef6`) (`esi=9`) | DMA engine bounds-checks the index (faults / clamps) | CONFIRMED |
| `neff_feature_DMA_desc_higher_dim` | `0x000200` | 10 | NEFF uses 3D/4D (higher-than-2D) DMA descriptors | `DescGen::dumpToFile` `0x11e4760`, site `0x11e4de7` (`esi=0xa`) | DMA descriptor decoder must support the higher-dim layout | CONFIRMED |
| `neff_feature_vnc` | `0x000400` | — | Multi-LNC / virtual-NeuronCore NEFF: more than one virtual core per physical core | **Procedural** in `writeNEFFFeatures`: `cmpl $0x1,0x1a4(%rax); ja` @ `0x152964f` (`options.vnc_nc_count > 1`) | Loader instantiates N virtual cores; maps each subgraph to a vNC | CONFIRMED |
| `neff_feature_remote_sem` | `0x004000` | — | NEFF issues cross-core remote semaphore updates (one core signals another's semaphore) | **Procedural** in `writeNEFFFeatures`: scan every block for an `InstDMABlock` (`classof@plt 0x625200`) where `isRemoteUpdateInstruction@plt 0x61ced0` is true; first match sets bit | CONFIRMED |

> **QUIRK — `vnc` and `remote_sem` are NOT `ModuleAttribute` enum members.** They have a header bit (`0x400`/`0x4000`) and a `def.json` name (`.rodata 0x1c86973`/`0x1c86992`), but **no** enum ordinal, **no** `getAttribute` read, and **no** `setAttribute` write. They are computed at package time inside `writeNEFFFeatures` itself — `vnc` from `options.vnc_nc_count` (`options+0x1a4`), `remote_sem` from an `InstDMABlock` IR scan. A roster that assigns either an enum ordinal is wrong for 2.24.5133. This is the report's flag-level correction C-1, re-affirmed below.

> **NOTE — the named array lists ≤ 11; the header mask carries up to ~21.** Only these eleven flags push a string into `def.json["neff_features"]`. Every ordinal ≥ 11 (the DGE / large-tensor cluster) and the two pure arch-version bits set **only** a header bit — no name. A reimplementer reading `def.json` to recover the capability set will under-report; the authoritative surface is the `neff_header+192` mask.

---

## The Full `writeNEFFFeatures` Bit Map

`writeNEFFFeatures` accumulates the mask in `%r14`, OR-ing a fixed bit constant for each engaged attribute, then stores `%r14` to `NeffFileWriter+0xC0` (=192). The bit positions are wire constants independent of read order. The function reads ordinals `2..10` first, then the two procedural bits, then the `0xb/0xc/0xf` cluster, then `0xd/0xe/0x11/0x14/0x15/0x16`. The `%edx` immediate at each `getAttribute` PLT call is the ordinal; the OR constant is the wire bit.

| Bit | Ord | `def.json` name pushed | How set | Conf |
|---|---|---|---|---|
| `0x0000002` | 2 | `collective_has_offset` | `getAttribute(2)` @ `0x15294f8` | CONFIRMED |
| `0x0000004` | 3 | `neff_feature_custom_ops` | `getAttribute(3)` @ `0x1529523` | CONFIRMED |
| `0x0000008` | 4 | `neff_coalesced_ccops` | `getAttribute(4)` @ `0x1529549` | CONFIRMED |
| `0x0000010` | 5 | `neff_queue_set_instances` | `getAttribute(5)` @ `0x152956f` | CONFIRMED |
| `0x0000020` | 6 | `neff_has_functions` | `getAttribute(6)` @ `0x1529595` | CONFIRMED |
| `0x0000040` | 7 | `neff_feature_dynamic_pwp` | `getAttribute(7)` @ `0x15295bb` | CONFIRMED |
| `0x0000080` | 8 | `neff_feature_indirect_memcpy_32b_sem_wait` | `getAttribute(8)` @ `0x15295e1` | CONFIRMED |
| `0x0000100` | 9 | `neff_feature_indirect_memcpy_bound_check` | `getAttribute(9)` @ `0x1529607` | CONFIRMED |
| `0x0000200` | 10 (`0xa`) | `neff_feature_DMA_desc_higher_dim` | `getAttribute(0xa)` @ `0x152962d` | CONFIRMED |
| `0x0000400` | — | `neff_feature_vnc` | `cmpl $0x1,0x1a4(%rax); ja` @ `0x152964f` | CONFIRMED |
| `0x0000800` | — | *(no name — DVE perf mode)* | `cmpl $0x14,0xac(%r13)` @ `0x152965c` (arch>20) `&& !disableDVEPerfMode` | CONFIRMED |
| `0x0001000` | 11 (`0xb`) | *(bit only)* `neff_feature_DGE` | `getAttribute(0xb)` @ `0x152967f`; `or $0x1000,%r14` @ `0x15296a4` | CONFIRMED |
| `0x0002000` | 12 (`0xc`) | *(bit only)* `neff_feature_POOL_RSQRT` | `getAttribute(0xc)` @ `0x15296b8`; `or $0x2000,%r14` @ `0x15296dd` | CONFIRMED |
| `0x0020000` | 15 (`0xf`) | *(bit only)* `neff_feature_embedding_stride32` | `getAttribute(0xf)` @ `0x15296f1`; `or $0x20000,%r14` @ `0x1529716` | CONFIRMED |
| `0x0004000` | — | `neff_feature_remote_sem` | `InstDMABlock` `isRemoteUpdateInstruction` scan (`0x625200`/`0x61ced0`) | CONFIRMED |
| `0x0008000` | 13 (`0xd`) | *(bit only)* `neff_feature_DGE_cast` | `getAttribute(0xd)` @ `0x1529800`; `or $0x8000,%r14` @ `0x1529825` | CONFIRMED |
| `0x0010000` | 14 (`0xe`) | *(bit only)* `neff_feature_vector_DGE` | `getAttribute(0xe)` @ `0x1529839`; `or $0x10000,%r14` @ `0x152985e` | CONFIRMED |
| `0x0200000` | 17 (`0x11`) | *(bit only)* `neff_feature_hardware_DGE` | `getAttribute(0x11)` @ `0x1529872`; `or $0x200000,%r14` @ `0x1529897` | CONFIRMED |
| `0x0100000` | 20 (`0x14`) | *(bit only)* `neff_feature_SQI_no_rearm` | `getAttribute(0x14)` @ `0x15298ab`; `or $0x100000,%r14` @ `0x15298d0` | CONFIRMED |
| `0x0400000` | 21 (`0x15`) | *(bit only)* `neff_feature_partial_sb2sb_ccop` | `getAttribute(0x15)` @ `0x15298e4`; `or $0x400000,%r14` @ `0x1529909` | CONFIRMED |
| `0x2000000` | 22 (`0x16`) | *(bit only)* `neff_feature_large_tensor_support` | `getAttribute(0x16)` @ `0x152991d`; `or $0x2000000,%r14` @ `0x1529eb9` | CONFIRMED |
| `0x1000000` | 22+ | *(bit only — no ordinal)* | `or $0x1000000,%r14` @ `0x1529ecf`, only when `arch>39` (`cmpl $0x27,0xac(%r13); jg` @ `0x152993b`) | CONFIRMED |

> **QUIRK — bit order ≠ ordinal order, and two bits have no ordinal at all.** The function reads `getAttribute` ordinals out of numeric order (`…10, [vnc], [DVE], 11, 12, 15, [remote_sem], 13, 14, 17, 20, 21, 22`), but each OR constant is a fixed wire bit, so the *encoding* is stable regardless of read order. Two bits — `0x800` (DVE perf) and `0x1000000` (the arch>39 companion) — are pure arch-version triggers with **no** `ModuleAttribute` and **no** name. `arch` here is read from `Module+0xac` (172); `0x14`=ArchLevel 20 and `0x27`=ArchLevel 39 are ArchLevel ordinals.

### The store and the schema bump

```c
// writeNEFFFeatures @0x15294b0 — tail; r14 = accumulated u64 mask
// a4=Module const&, a3=NeffFileWriter&; options = *(a1+96); arch = *(int*)(Module+0xac)
function writeNEFFFeatures(json &def, NeffFileWriter &w, Module const &m):
    mask = 0;                                       // xor %r14d,%r14d @0x152950a
    // ... 0x2..0x200 attribute reads + vnc/DVE/remote_sem + 0xb/0xc/0xf/0xd/0xe/0x11/0x14/0x15/0x16 ...

    if (large_tensor_support present):              // ord 0x16 variant byte @0x388(rsp) @0x152992d
        mask |= 0x2000000;                          // @0x1529eb9
        destroy_content(variant);                   // boost::variant cleanup @0x1529ec0

    if (arch > 39):                                 // cmpl $0x27,0xac(%r13); jg @0x152993b
        mask |= 0x1000000;                          // @0x1529ecf  (no ordinal — arch trigger)
    if (mask != 0 || large_tensor_support):         // test r14,r14 @0x1529949
        // schema/min-version lever: w.schema_min = max(w.schema_min, 2)
        if (w.schema_min /* +0xc8 */ < 2)           // mov 0xc8(%rcx),%rax; cmp $2 @0x1529ee0
            w.schema_min = 2;                        // cmovb %rdx,%rax; mov %rax,0xc8(%rcx) @0x1529eee

    *(u64*)(w + 0xC0) = mask;                        // mov %r14,0xc0(%rax) @0x1529971 -> header+192
    def["neff_features"] = name_array;              // operator[]("neff_features" @0x1c86984) @0x1529a88
```

> **NOTE — the `0xc8` (writer+200) field is the schema/min-version lever.** When `large_tensor_support` is present or the `arch>39` path is taken with a non-zero mask, `writeNEFFFeatures` raises `NeffFileWriter+200` to `max(current, 2)` via a `cmovb`-based clamp at `0x1529ee0..0x1529eee`. `initializeNeffHeader` later copies this into `neff_header` as the coarse minimum-runtime-version gate ([Part 12.2](neff-header-bom-writer.md), header `schema_min` at `+200`). Do not confuse the `0xc8`/+200 schema field with the `0xc0`/+192 feature-mask store — they are adjacent quadwords with different roles.

---

## The `ModuleAttribute` Encoding

### The enum

`bir::ModuleAttribute` (defined in `libBIR.so`, `ModuleAttributes.cpp`) has 23 members `0..22`. Ordinals `0`/`1` are pipeline bookkeeping (`previous_pass`, `pass_sequence_number`); the feature family occupies `2..22` with three non-feature members interleaved (`16 neuron_core_id`, `18 loops_on_chip`, `19 loops_in_backend`):

```text
 0 previous_pass               1 pass_sequence_number
 2 neff_collective_has_offset
 3 neff_feature_custom_ops      4 neff_coalesced_ccops
 5 neff_queue_set_instances     6 neff_has_functions
 7 neff_feature_dynamic_pwp
 8 neff_feature_indirect_memcpy_32b_sem_wait
 9 neff_feature_indirect_memcpy_bound_check
10 neff_feature_DMA_desc_higher_dim
11 neff_feature_DGE            12 neff_feature_POOL_RSQRT
13 neff_feature_DGE_cast       14 neff_feature_vector_DGE
15 neff_feature_embedding_stride32
16 neuron_core_id  (NOT a feature bit)   17 neff_feature_hardware_DGE
18 loops_on_chip   19 loops_in_backend
20 neff_feature_SQI_no_rearm
21 neff_feature_partial_sb2sb_ccop
22 neff_feature_large_tensor_support
```

> **CORRECTION (D-S04 C-1, re-affirms D-D12 §6) —** `neff_feature_vnc` and `neff_feature_remote_sem` are **not** in this enum. They exist only as (a) `writeNEFFFeatures` procedural bits and (b) `def.json` name strings in `libwalrus` `.rodata` (`0x1c86973` `"neff_feature_vnc"`, `0x1c86992` `"neff_feature_remote_sem"`). Ordinal `16` (`neuron_core_id`) is a `ModuleAttribute` but is **not** a feature bit — it is set by the vNC concretizers (`test_vnc` `0x110f030`, `BirLinker::initModule` `0x15d3b30`, `LncSplitter::concretizeModule` `0x16d3bf0`) to stamp each module's core index, and `writeNEFFFeatures` never reads it.

### Storage type — the `boost::variant` bool arm

`bir::Module::setAttribute(ModuleAttribute, value)` / `getAttribute(ModuleAttribute)` store into an `unordered_map<ModuleAttribute, variant>` on the `Module`. The value type (from the `nm -DC` mangled signature) is:

```c
boost::variant< recursive_flag<bool>, long, unsigned long, std::string,
                std::vector<recursive>, std::unordered_map<string, recursive> >;
```

A feature flag is the **bool arm set to `true`**. In `writeNEFFFeatures` the per-attribute local is the variant **discriminator byte**: non-zero ⇒ the attribute is engaged ⇒ push the name / OR the bit, then `boost::variant::destroy_content` frees the returned variant (the `destroy_content@plt` call observed at `0x1529ec0` is one such cleanup). So "feature present" precisely means "the `ModuleAttribute` is set to a `true` bool in the BIR module's attribute map."

> **GOTCHA — "present" is the discriminator byte, not a stored boolean value.** `writeNEFFFeatures` does not read a `bool` out of the variant and test it; it tests whether the variant's **active arm** is the bool arm at all (discriminator non-zero). A reimplementer who models the attribute map as `map<ord,bool>` and writes `false` entries would produce a different result from the binary, which keys "present" off arm engagement, not stored truth. Codegen only ever calls `setAttribute(ord, true)`, so in practice the two models coincide — but the wire semantics are "engaged," not "true."

### The two on-wire surfaces

```text
(i)  def.json  "neff_features": [ "<name>", … ]   — SmallVector<string> of the named subset (≤ 11).
                                                     Per-core (each module's def.json).
(ii) neff_header+192  feature_flags : u64          — the full §"full bit map" mask (ALL bits).
                                                     Written via NeffFileWriter+192 ->
                                                     initializeNeffHeader copies to header+192.
                                                     This is the runtime-read surface.
```

The `string2ModuleAttribute` reverse map (`libBIR`) lets the BIR-JSON loader round-trip attribute keys when a `Module` is (de)serialized to BIR JSON, so the attributes also appear as keys in the serialized BIR module — not only in the NEFF. The `def.json` array is the human/diagnostic mirror; the header u64 is the machine gate.

---

## Setting Mechanism and the Compat-Reject Contract

### Setting — upstream passes call `setAttribute`

There are 36 `setAttribute` call sites in `libwalrus`. Each feature-using codegen routine detects it exercised a capability and records it as a `Module` attribute (the `boost::variant` bool=`true` arm). Nearly all live in `CoreV2GenImpl`, the per-instruction codegen visitor: the flag is set the moment the feature-bearing instruction is lowered to ISA. A few belong to dedicated lowering passes (`LowerPWP` for `dynamic_pwp`, `DescGen` for `DMA_desc_higher_dim`). `writeNEFFFeatures` itself sets **no** attribute; `vnc` and `remote_sem` are the exceptions to the attribute model entirely — computed at package time from `options`/IR, never stored.

The DGE cluster (ords 11/13/14/17) is set by **one** routine, `CoreV2GenImpl::generateDynamicDMA` (`0x1276b10`): ord 11 (`DGE`) on any dynamic DMA, ord 13 (`DGE_cast`) when a cast is fused, ord 17 (`hardware_DGE`) on the HW-DGE path, ord 14 (`vector_DGE`) when `!isDstReduceDGE`. `embedding_stride32` (ord 15) is set by `visitInstIndirectSaveAccumulate` (`0x1269f00`); `large_tensor_support` (ord 22) by `assign64bitAddr` (`0x1260290`). These ordinal-≥11 setters write only a header bit; their names live in `libBIR ModuleAttribute2string`, not in `libwalrus` `.rodata`.

> **NOTE — a downstream pass *reads* the attribute too.** The `.rodata` assert string at `0x1d6b670` — `I.getModule()->getAttribute(ModuleAttribute::neff_feature_SQI_no_rearm)` — is direct evidence that a lowering pass branches on `getAttribute(SQI_no_rearm)` to choose its SQI lowering, not only that `writeNEFFFeatures` serializes it. The attribute is thus both a compile-time pass signal and a wire capability bit. (`SQI_no_rearm`, ord 20, is set conditionally by `visitInstSwitchQueueInstance` `0x1264240` at `0x126444f`, after the same visitor unconditionally sets `queue_set_instances` ord 5 at `0x1264274`.)

### Runtime read and the compat-reject contract

The Neuron runtime (the NEFF loader) extracts the PAX tar, reads `neff_header+192 feature_flags` (u64) **before** wiring engines, and uses the bits two ways: (a) gate which HW/firmware capabilities to provision (GPSIMD for `custom_ops`, the higher-dim DMA decoder, the remote-sem fabric, vNC partitioning, queue sets, the function call table), and (b) reject a NEFF whose bits it does not recognize. The `def.json` `"neff_features"` array is the human-readable mirror. *(The runtime side is in `libnrt`, outside this corpus; the read-and-gate semantics are STRONG/INFERRED from the producer contract and the append-only encoding.)*

The bitmask **is** the forward/backward-compat mechanism:

```c
// Runtime-side capability gate (libnrt; reconstructed from the producer contract)
// header_mask = *(u64*)(neff_header + 192);
if (header_mask & ~RUNTIME_SUPPORTED_MASK)        // any bit this runtime does not implement
    reject_neff("requires unsupported feature");  // newer-NEFF / older-runtime -> refuse
if (neff_header.schema_min > RUNTIME_MAX_SCHEMA)  // coarse min-version gate (header+200, set to >=2)
    reject_neff("schema too new");
provision_engines(header_mask);                   // per-feature provisioning of the recognized bits
```

> **GOTCHA — the bits are append-only, fixed constants; never renumber them.** The whole reject contract depends on a fixed bit ⇒ fixed feature mapping. A NEFF with bit `X` set requires a runtime that implements `X`; an old runtime detects a too-new NEFF by testing for any set bit outside its supported mask. If a reimplementation renumbers or reuses a bit, every previously compiled NEFF and every deployed runtime silently mis-negotiates. This is why ord ≥ 11 flags still consume a stable, dedicated wire bit even though they push no `def.json` name. The `neff_header+200` schema-min field (raised to `≥ 2` for `large_tensor_support` / `arch>39`) is a coarser min-runtime-version gate layered **on top of** the per-feature bits, not a replacement for them.

### Adjacent resource keys — not feature flags

Right after the `neff_features` array, `writeDefJson` emits a `.rodata` string cluster at `0x1c869b9..` — `"runtime_event_count"` (xref `0x152a7e4`), `"runtime_semaphore_count"`, `"runtime_statebuffer_reservation"` (xref `0x152c043`). These are per-core **resource** declarations the runtime reads to provision events / semaphores / state-buffer bytes before exec. They are complementary to the capability bits — the bits say *which* features, these say *how much* resource — and are **not** part of the feature mask. *(Keys CONFIRMED; provisioning semantics STRONG.)*

---

## Verification and Re-Verify Ceiling

The five strongest claims were re-walked against the cp310 `libwalrus.so` this session:

| Claim | Evidence | Result |
|---|---|---|
| Mask stored to header+192 | `mov %r14,0xc0(%rax)` @ `0x1529971`; `writeNEFFFeatures` exported `T` @ `0x15294b0` with the 4-arg `(json&, NeffFileWriter&, Module const&)` signature | CONFIRMED |
| Every bit↔ordinal pairing | `getAttribute(ord)` `%edx` imms `2..0xa, 0xb, 0xc, 0xf, 0xd, 0xe, 0x11, 0x14, 0x15, 0x16` with matching `or $bit,%r14` constants, all `@0x15294f8..0x1529928` | CONFIRMED |
| `vnc`/`remote_sem` are procedural | `cmpl $0x1,0x1a4(%rax); ja` @ `0x152964f`; `InstDMABlock::classof@plt 0x625200` + `isRemoteUpdateInstruction@plt 0x61ced0` @ `0x1529787`/`0x1529793` — no ordinal | CONFIRMED |
| `def.json` key is `"neff_features"` (plural) | `lea # 1c86984` @ `0x1529a88`; string @ file offset `29911428` = `0x1c86984` | CONFIRMED |
| Schema bump = `max(schema_min,2)` for arch>39 / large-tensor | `cmpl $0x27,0xac(%r13); jg` @ `0x152993b`; `cmovb`-clamp on `0xc8(%rcx)` @ `0x1529ee0..0x1529eee`; `or $0x1000000` @ `0x1529ecf` | CONFIRMED |

> **NOTE — re-verify ceiling.** The producer (`writeNEFFFeatures`) was disassembled byte-for-byte this session: the store, every `getAttribute` ordinal/bit pair, both procedural bits, the arch gates, the schema clamp, and the `"neff_features"` key are all directly observed in the cp310 `.so`. What was **not** independently re-walked: (1) the *setter* function-entry addresses (`visitInstCustomOp` `0x12613c0`, `generateDynamicDMA` `0x1276b10`, etc.) — the shipped `.so` has no local symbols, so these come from the cp310 IDA decompile, though the `setAttribute` call-site `%esi` ordinals (`esi=3` @ `0x12619b0`, `esi=6` @ `0x126343f`, …) were spot-checked and confirmed. (2) The runtime-side reject logic lives in `libnrt`, outside this corpus, so §"Runtime read" is the contract reconstruction (STRONG/INFERRED), not a disassembly. (3) The `ModuleAttribute` enum ordinal-to-name mapping is `libBIR`-defined and cross-referenced from D-D12; each ordinal's *bit* is confirmed in `libwalrus`, but the *name* for the unnamed ordinal-≥11 bits is from the `libBIR` string table, not the `libwalrus` `.rodata`.

---

## Related Components

| Name | Relationship |
|---|---|
| `NeffPackager::writeNEFFFeatures` (`0x15294b0`) | The producer; OR-assembles the mask, stores to writer+192, pushes the `def.json` name array |
| `NeffFileWriter::initializeNeffHeader` (`0x1540a00`) | Copies writer+192 into `neff_header+192`; copies writer+200 into the header schema-min field |
| `bir::Module::getAttribute` (`@plt 0x600360`) | Reads the `boost::variant` attribute per ordinal |
| `bir::Module::setAttribute` (`@plt 0x61d170`) | The 36 upstream codegen setters that record each feature |
| `CoreV2GenImpl` visitors | Per-instruction codegen; set most feature attributes at ISA-lowering time |

## Cross-References

- [The `neff_header` POD, the In-Memory BOM, and the NeffPackager Writer](neff-header-bom-writer.md) — Part 12.2; the `feature_flags` field at `neff_header+192` this system fills, and the `schema_min` field at `+200` the schema lever bumps.
- **Part 12.3 — NEFF JSON Sidecars** *(in-flight)*: the `def.json` that carries the mirror `"neff_features"` name array (and the adjacent `runtime_*` resource keys).
- **Part 11 — Custom Ops / µcode Library** *(planned)*: the `custom_ops` (`0x4`) and `ucode_lib` machinery whose presence sets ord 3.
- **Part 13 — Collective Coalescing** *(planned)*: the `coalesced_ccops` (`0x8`, ord 4) transform that sets the coalesced-multichannel bit.
