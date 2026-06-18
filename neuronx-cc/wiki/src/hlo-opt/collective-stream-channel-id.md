# Collective Stream-ID & Channel-ID Family

> *All addresses on this page are virtual addresses (VMA) for neuronx_cc 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt`; resolve via `objdump --start-address` or the VMA-keyed `disasm/` sidecars. VA ≠ raw file offset: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000 (section headers). Other builds will differ.*

> **CORRECTION (audit #815) —** The "two collective predicates" reconstruction below conflates two *distinct* opcode-bitmask routines with *different* masks. `neuron::IsCollective` @0x1f7e010 (called by the enforcer #82) uses masks **`0x20000410`** (low band, indexed by `op` directly) + **`0x80011`** (high band, indexed by `op − 0x53`) plus `op == 7`, selecting opcodes **{0x4, 0x7, 0xa, 0x1d, 0x53, 0x57, 0x66}** — verified by `objdump` (`mov $0x20000410`, `mov $0x80011`, `sub $0x53`, `cmp $0x1d`). The masks `0x650` / `0x810000000000001` quoted in the pseudocode belong to the *stock* `hlo_query::NextChannelId` @0x8ab1ac0 (and the inlined checker #58 low band), where the `bt` is indexed by `op` **directly** (after `and $0xfffffffd`), not by `op − 0x1C`; the "bits {0,52,59} → opcodes {0x1c,0x50,0x57}" mapping is therefore incorrect. The "Shared substrate / two predicates", "high-band `−0x1C` rebase" GOTCHA, and self-verification item #5 need re-derivation against these three functions separately.*

## Abstract

Three HLO passes in `hlo-opt` assign the two identity fields that the Neuron runtime communicator keys on when it schedules collectives: the **`stream_id`** frontend attribute (which hardware collective stream a collective is issued on) and the **`channel_id`** (the globally-unique handle that pairs collective participants across replicas). They are small, adjacent, and Neuron-authored — none of the three exists in stock XLA. `CollectiveStreamIdChecker` (registration order #58) runs early and is a read-only presence detector; `NeuronCollectiveStreamIdInjector` (#81) and `NeuronUniqueChannelIdEnforcer` (#82) run late and adjacent, the injector stamping a `stream_id` digit on every collective and the enforcer immediately repairing any `channel_id` collisions it leaves behind.

The two transforms are not the monotonic-counter allocators their names suggest. The injector partitions collectives into exactly **two** streams — `"0"` and `"1"` — by **replica-group equivalence** against two exemplar groups it discovers from `collective_type` frontend attributes (tensor-parallel collectives → group 0, FSDP/data-parallel collectives → group 1). The enforcer assigns fresh `channel_id`s only on collision, drawing them from `xla::hlo_query::NextChannelId(module)` (one past the current global maximum) and inserting each new id back into a SwissTable so two collisions never alias. The checker mutates nothing; its sole output is a single boolean reported through the second `Run` argument: "does some collective already carry a `stream_id`?"

This page reconstructs all three `Run` bodies, the shared collective predicates (a typed `DynCast` route in the injector, an inline `HloOpcode` bitmask route in the enforcer and checker), the `stream_id` 2-way partition, and the channel-id renumbering loop. The HLO opcode and replica-group concepts are owned by [Part 13](../part13/channel-id-replica-group.md); the collective lowering that consumes `stream_id` downstream is [§4.3](collectives-to-customcall.md).

For reimplementation, the contract is:

- The **collective predicates**: the typed `xla::DynCast<HloCollectiveInstruction>` test (injector) and the three-band inline `HloOpcode` bitmask test (`op==7`, mask `0x650`, mask `0x810000000000001`) used by the enforcer, checker, and `NextChannelId`.
- The **`stream_id` 2-way partition**: discover TP and FSDP exemplar replica-groups from `collective_type`, then stamp `'0'`/`'1'` on every collective by `ReplicaGroupsEqual`.
- The **channel-id uniqueness algorithm**: first-seen-wins via `flat_hash_set<optional<long>>`, collisions renumbered monotonically from `NextChannelId(module)`.
- The **checker's read-only contract**: always returns `{OK, false}`, reports presence through `*(*(arg2)+8)`.

| | |
|---|---|
| **Checker `Run`** | `0x1e8c800` — `xla::CollectiveStreamIdChecker::Run` (1052 B, 66 bb) |
| **Injector `Run`** | `0x1f951a0` — `xla::hilo::NeuronCollectiveStreamIdInjector::Run` (4889 B, 219 bb) |
| **Enforcer `Run`** | `0x1fecb40` — `xla::hilo::NeuronUniqueChannelIdEnforcer::Run` (1223 B, 55 bb) |
| **Reg. order / name keys** | #58 `collective-stream-id-checker` · #81 `aws_neuron_collective_stream_id_injector` · #82 `neuron_unique_channel_id_enforcer` |
| **IR level** | HLO (HiLo `xla::HloModule`); passes are `xla::HloPassInterface` subclasses |
| **Shared infra** | `neuron::IsCollective` `0x1f7e010` · `hlo_query::NextChannelId` `0x8ab1ac0` · `ReplicaGroupsEqual` `0x9163ca0` |
| **Stock vs Neuron** | All three **Neuron-authored** (no stock-XLA analogue) |

---

## Shared Substrate

All three passes derive `xla::HloPassInterface`; `name()` is vtable slot `vptr+0x10`, `Run` is `vptr+0x18`. Each `name()` is the canonical 11-byte stub `mov eax,len; mov edx,offset str; ret`, and all three name literals were read verbatim from rodata.

### The pass identities (CERTAIN)

| Pass | Class symbol | `name()` key (len) | Run |
|---|---|---|---|
| #58 | `xla::CollectiveStreamIdChecker` | `collective-stream-id-checker` (28) | `0x1e8c800` |
| #81 | `xla::hilo::NeuronCollectiveStreamIdInjector` | `aws_neuron_collective_stream_id_injector` (40) | `0x1f951a0` |
| #82 | `xla::hilo::NeuronUniqueChannelIdEnforcer` | `neuron_unique_channel_id_enforcer` (33) | `0x1fecb40` |

> **NOTE —** the checker lives in namespace `xla::`, not `xla::hilo::` like the two transforms. The mangled symbols (`_ZN3xla25CollectiveStreamIdChecker3Run…` vs `_ZN3xla4hilo32NeuronCollectiveStreamIdInjector3Run…`) confirm this. The "checker" predates the Neuron `hilo` collective-coalescing cluster; the injector/enforcer are the newer rewrite half of the family.

### The two collective predicates

A "collective" must be recognised before any field is touched. Two distinct implementations exist, equivalent in intent:

**Typed route — injector only.** `xla::DynCast<HloCollectiveInstruction>(inst)`; non-null ⇒ collective. This is class-based (RTTI-style cross-cast inside the HLO hierarchy) and therefore the stronger test — it admits exactly the instructions that subclass `HloCollectiveInstruction`.

**Inline bitmask route — enforcer, checker, and `NextChannelId`.** Reads `opcode = *(u8*)(inst+0x14)` (the `HloInstruction::opcode_` byte) and tests three opcode bands. Decoded from the inline masks (CERTAIN mask values; opcode→name mapping MED, see [Part 13](../part13/channel-id-replica-group.md)):

```c
bool IsCollectiveOpcode(uint8_t op):       // neuron::IsCollective @0x1f7e010, inlined in #82/#58/NextChannelId
    if op == 7:                            // kAllReduce — always a collective
        return true
    if op > 3 && op <= 0x0A:               // low band: bt 0x650, op
        return (0x650 >> op) & 1            // bits {4,6,9,10} set (all-gather / all-to-all / permute / broadcast class)
    if op >= 0x1C && op <= 0x57:           // high band: bt 0x810000000000001, (op - 0x1C)
        return (0x810000000000001 >> (op - 0x1C)) & 1   // bits {0,52,59} set (async-collective / reduce-scatter / send-recv band)
    return false
```

> **GOTCHA —** the high-band mask is indexed by `op − 0x1C`, not `op`. The set bits `{0, 52, 59}` correspond to opcodes `0x1C`, `0x50`, `0x57`. A reimplementation that tests `bt 0x810000000000001, op` directly (forgetting the `−0x1C` rebase) will misclassify every high-band opcode. (Bit positions re-derived from the immediate `580964351930793985` = `0x810000000000001`; CERTAIN on the mask, MED on each opcode's name.)

### The `stream_id` frontend attribute

`"stream_id"` is the verbatim key both transforms key on. The injector emplaces it; the checker looks it up. It lives in each collective's `FrontendAttributes` proto map, reached at `inst+0x30` (the metadata/frontend-attributes block). The literal is read from rodata `0x2768c6` in the checker (referenced 5 times) and built from rodata `0x411858` in the injector (referenced once, at `0x1f9592a`).

### Shared HloInstruction layout (CERTAIN, cross-checked across all three passes)

| Field | Offset | Type | Used by |
|---|---|---|---|
| `opcode_` | `+0x14` | `u8` | enforcer, checker (bitmask predicate) |
| `frontend_attributes` | `+0x30` | `FrontendAttributes*` (0x108-byte owned block) | injector (emplace), checker (lookup) |
| `channel_id` value | `+0x208` | `long` | enforcer, `NextChannelId` |
| `channel_id` engaged | `+0x210` | `u8` (optional flag) | enforcer, `NextChannelId` |

`HloComputation` holds its instruction pointer vector at `+0x40..+0x48` (begin..end, 16-byte stride per element); `HloModule` holds its computation pointer vector at `+0x40..+0x48` (8-byte stride).

---

## #81 — NeuronCollectiveStreamIdInjector

### Purpose

Stamp a `stream_id` frontend attribute on **every** collective in the module, partitioning them into two backend collective streams. Tensor-parallel (TP) collectives and any un-classified collective land on stream `"0"`; FSDP / data-parallel collectives land on stream `"1"`. This gives the backend exactly two independent collective queues so the two families can be issued and overlapped on distinct hardware streams. **Neuron-authored** — stock XLA has no stream-id concept.

### Algorithm

The injector runs three sequential scans over `module.computations()`. Scans 1 and 2 *discover* the two exemplar replica-groups; scan 3 *stamps* the digit.

```c
function Injector_Run(module):                          // 0x1f951a0
    vector<ReplicaGroup> group0, group1;                // TP exemplar, FSDP exemplar
    bool changed = false;

    // --- SCAN 1: capture replica_groups of a TP collective into group0 ---
    for comp in module.computations():
        for inst in comp.MakeInstructionPostOrder():     // @0x9634ab0
            if DynCast<HloCollectiveInstruction>(inst) == null: continue   // @0x1f94fb0
            if inst.shape().AreAllLeavesIntegers(): continue               // skip integer-typed collectives
            if HasFAKV(inst, "collective_type", "tp_reduce_scatter")
               || HasFAKV(inst, "collective_type", "tp_all_gather"):       // @0x1f94d00 (isra helper)
                group0 = inst.replica_groups();          // @0x965e7e0; vector copy-assign
                changed = true;
                break    // first TP match in this computation wins; later computations overwrite

    // --- SCAN 2: capture replica_groups of an FSDP collective into group1 ---
    for comp in module.computations():
        for inst in comp.MakeInstructionPostOrder():
            if DynCast<HloCollectiveInstruction>(inst) == null: continue
            if inst.shape().AreAllLeavesIntegers(): continue
            if HasFAKV(inst, "collective_type", "fsdp_reduce_scatter")
               || HasFAKV(inst, "collective_type", "fsdp_all_gather"):
                group1 = inst.replica_groups();
                changed = true;
                break

    // --- SCAN 3: stamp a stream_id digit on EVERY collective ---
    for comp in module.computations():
        for inst in comp.instructions():                 // RAW instruction list, NOT post-order
            if DynCast<HloCollectiveInstruction>(inst) == null: continue
            if ReplicaGroupsEqual(inst.replica_groups(), group0):   // @0x9163ca0
                digit = 0                                // 0x1f95a60: r15d := 0
            else:
                digit = ReplicaGroupsEqual(inst.replica_groups(), group1) ? 1 : 0   // 0x1f958c8

            fa = inst.frontend_attributes();             // create fresh 0x108-byte block if null (see below)
            fa.SyncMapWithRepeatedField(); fa.SetMapDirty();
            fa.map["stream_id"] = string(1, '0' + digit);   // TryEmplaceInternal<char[10]>("stream_id")

    return StatusOr<bool>{ OK, changed };                // result[+8]=changed, result[+0]=0
```

### The stream-id space — binary {"0","1"} (CERTAIN)

The injected value is a **single ASCII digit**, built by `std::string::_M_construct(1, c)` then `*p = (uint8)r15d + 0x30` (the `+0x30` ASCII offset at `0x1f95900`/`0x1f9590c`). `r15d ∈ {0,1}` is the boolean result of the `ReplicaGroupsEqual` comparisons. Therefore:

- `stream_id = "1"` ⟺ the collective's replica-groups equal the FSDP exemplar (`group1`).
- `stream_id = "0"` ⟺ the collective's replica-groups equal the TP exemplar (`group0`), **or match neither**.

> **QUIRK —** despite the "id injector" name this is **not** a monotonic counter. The stream space is fixed at size 2, and the partition is an equivalence relation over replica-groups, not an enumeration. The collective's *own* `collective_type` is irrelevant during scan 3 — only its replica-group shape decides the digit. A reimplementation that allocates incrementing ids, or that keys off each collective's own `collective_type`, is wrong. The `collective_type` annotations are consulted **only** in scans 1/2 to pick the two exemplars.

### The `collective_type` vocabulary (CERTAIN — stack-built literals)

The four recognised `collective_type` values are built on the stack from immediate fragments, decoded directly from the injector's `constants_used`:

| Literal | Built from (verified) |
|---|---|
| `"collective_type"` (key) | `0x6968…` → `"collecti"` + `"ve_t"` + `"yp"` + `"e"` |
| `"tp_all_gather"` | `"tp_all_g"` (`0x6755…`) + `"athe"` + `'r'` |
| `"fsdp_all_gather"` | `"fsdp_all"` (`0x6C6C…`) + `"_gat"` + `"he"` + `'r'` |
| `"tp_reduce_scatter"` | xmmword + `'r'` (paired family) |
| `"fsdp_reduce_scatter"` | xmmword + `"te"` + `'r'` (paired family) |

The `coalesce_fsdp_all_gathers` / `coalesce_fsdp_reduce_scatters` HiLo functions (visible in the symbol table) are the upstream producers that tag collectives with these `collective_type` values — confirming the vocabulary is shared, not invented by the injector.

> **NOTE —** which pass *writes* `collective_type` is upstream of `hlo-opt` (the penguin/frontend collective-tagging path) and was not traced here. The injector is a pure consumer of the attribute.

### Fresh-FrontendAttributes construction

When `inst[+0x30] == null` the injector allocates a new metadata block: `operator new(0x108)`, then in-place construct `FrontendAttributes(arena=0, owned=true)`, `StatisticsViz` (sub-object at `+0xB0`), `ResultAccuracy` (at `+0xE8`), store at `inst[+0x30]`, and tear down the prior (empty) object. The `0x108`-byte size and the `{FrontendAttributes, StatisticsViz@+0xB0, ResultAccuracy@+0xE8}` layout are the on-binary layout of the Neuron metadata block — all three ctors (`FrontendAttributesC1`, `StatisticsVizC1`, `ResultAccuracyC1`) appear in the injector's callee list.

### `HasFrontendAttributesKeyValue` helper (`0x1f94d00`, `.isra.0`)

```c
function HasFAKV(inst, key, value):                     // 0x1f94d00
    m = inst.frontend_attributes();                     // *(inst+0x30), or kEmptyRare if null
    m.SyncMapWithRepeatedField();
    it = m.FindHelper(key);                              // protobuf InnerMap probe
    CHECK(it != end());                                 // aborts on "key not found" (map.cc) — corrupted-map guard
    return (it != end()) && memcmp(it->value, value);   // string-value compare
```

> **GOTCHA —** the embedded `CHECK failed: it != end(): key not found:` literal (`0x1f94de0`) is the **only** abort site in this entire pass family. It can only fire if `FindHelper` first reports the key present and then the protobuf map mutates it away mid-call — i.e. a corrupted/concurrently-modified map. Under normal single-threaded operation a missing key returns `end()` cleanly and the helper returns `false`.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `…NeuronCollectiveStreamIdInjector::Run` | `0x1f951a0` | three-scan driver | CERTAIN |
| `…::HasFrontendAttributesKeyValue.isra.0` | `0x1f94d00` | key/value match on frontend attrs | CERTAIN |
| `DynCast<HloCollectiveInstruction>` | `0x1f94fb0` | typed collective predicate | CERTAIN |
| `HloComputation::MakeInstructionPostOrder` | `0x9634ab0` | scan-1/2 traversal | CERTAIN |
| `HloInstruction::replica_groups` | `0x965e7e0` | exemplar / per-inst groups | CERTAIN |
| `ReplicaGroupsEqual` | `0x9163ca0` | the 2-way partition test | CERTAIN |
| `Map…InnerMap::TryEmplaceInternal<char[10]>` | (callee) | emplace `"stream_id"` | CERTAIN |

### Considerations

- **Scan-1/2 "last-match-wins."** Each discovery scan breaks on the first matching collective *per computation* and overwrites `group0`/`group1`, so the captured exemplar is the *last* matching computation's *first* matching collective. With homogeneous TP/FSDP groups (the expected case) the result is well-defined; with heterogeneous groups the captured exemplar is the final one. (CERTAIN from the `break`/overwrite structure.)
- **Scan-3 traverses the raw instruction list, not post-order** — a deliberate asymmetry with scans 1/2. The stamping order is irrelevant since each collective's digit depends only on its own replica-groups, but a reimplementer must not assume post-order here.
- **Integer-typed collectives are skipped in discovery** (`AreAllLeavesIntegers`) but **not in stamping** — scan 3 stamps every collective regardless of element type.

---

## #82 — NeuronUniqueChannelIdEnforcer

### Purpose

Guarantee that every collective's `channel_id` is globally unique within the module. The runtime pairs collective participants by `channel_id`; a duplicate id silently cross-wires two unrelated collectives. The injector and the upstream coalescing passes can leave colliding ids behind, so this pass runs immediately after the injector to repair them. **Neuron-authored.**

### Algorithm

```c
function Enforcer_Run(module):                          // 0x1fecb40
    long nextId = hlo_query::NextChannelId(module);     // @0x8ab1ac0 → one past current global max, floor 1
    flat_hash_set<optional<long>> seen;                 // SwissTable, element = optional<long> (engaged-bit + value)
    bool changed = false;

    for comp in module.computations():                  // module[+0x40..+0x48], 8B stride
        for inst in comp.instructions():                // comp[+0x40..+0x48], 16B stride — RAW order
            if !neuron::IsCollective(inst): continue     // @0x1f7e010
            optional<long> cid = inst.channel_id();      // @0x965e9b0 → {value@+0x208, engaged@+0x210}
            if seen.contains(cid):                       // SwissTable probe (MixingHashState)
                inst.set_channel_id(optional<long>{nextId});   // @0x965e9d0
                seen.insert(optional<long>{nextId});     // prepare_insert + store {nextId, engaged}
                nextId += 1;
                changed = true;
            else:
                seen.insert(cid);                        // first-seen path keeps the id

    return StatusOr<bool>{ OK, changed };                // result[+8]=changed, result[+0]=0
```

### The next-id source: `hlo_query::NextChannelId` (`0x8ab1ac0`, CERTAIN)

`NextChannelId` walks every instruction of every computation; for each collective-class opcode whose `channel_id` is engaged (`*(u8*)(inst+0x210) != 0`, value at `inst[+0x208]`) it tracks `r8 = max(r8, channel_id + 1)`, starting from `r8 = 1`. It returns `max(existing channel_id) + 1` with floor 1. It uses the *same* inline opcode bitmasks as the predicate above — its `constants_used` table contains `1616` (`0x650`) and `580964351930793985` (`0x810000000000001`), confirming the shared mask values. So the new-id space is `[NextChannelId, ∞)`: strictly above every pre-existing id.

### Why it is correct (CERTAIN)

- **The uniqueness key is the full `optional<long>`** — both the engaged bit and the value. The set policy is `FlatHashSetPolicy<optional<long>>` (confirmed in the enforcer's callee list), hashed by `HashStateBase<MixingHashState>::combine<optional<long>>` (`0x1fec7c0`) with `prepare_insert` (`0x1fec970`). A *disengaged* channel_id is itself a distinct set element, so two no-channel collectives still count as a collision after the first.
- **First-seen wins, every later collision renumbers.** The first collective carrying a given id keeps it; subsequent carriers of the same id get `nextId`, `nextId+1`, … Because each assigned id is inserted back into `seen`, two collisions can never receive the same new id, and because they all start above the global maximum they can never alias a pre-existing id.
- **Order-dependent but deterministic.** Iteration is module order × raw instruction-list order (NOT post-order — contrast the injector's scans 1/2). The same module always renumbers identically.

> **QUIRK —** the disengaged (`nullopt`) channel_id participates in the set as a real value. If two collectives both lack a channel, the second is treated as a collision and gets an *engaged* id from `nextId`, materialising a channel where there was none. This is intentional: every collective the runtime must pair needs an id, and the enforcer is the place that backfills them.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `…NeuronUniqueChannelIdEnforcer::Run` | `0x1fecb40` | first-seen-wins renumbering loop | CERTAIN |
| `hlo_query::NextChannelId` | `0x8ab1ac0` | `max(channel_id)+1`, floor 1 | CERTAIN |
| `neuron::IsCollective` | `0x1f7e010` | bitmask collective predicate | CERTAIN |
| `HloInstruction::channel_id` | `0x965e9b0` | getter (optional) | CERTAIN |
| `HloInstruction::set_channel_id` | `0x965e9d0` | setter (optional) | CERTAIN |
| `HashStateBase<MixingHashState>::combine<optional<long>>` | `0x1fec7c0` | SwissTable hash | CERTAIN |
| `raw_hash_set<…optional<long>>::prepare_insert` | `0x1fec970` | SwissTable insert | CERTAIN |

### Considerations

- The no-computations early exit (`module[+0x40] == module[+0x48]`) returns `{OK, false}` (changed = false) without touching the set.
- The pass never *lowers* the maximum or compacts ids; it only adds ids strictly above the existing max, so ids stay sparse after renumbering.

---

## #58 — CollectiveStreamIdChecker

### Purpose

Report whether any collective in the module already carries a `stream_id` frontend attribute. It is a **read-only query/guard pass**, not a transform — despite the "checker" name it performs no consistency check *between* collectives; it is a presence detector. The plausible driver use is to gate whether the injector still needs to run. **Neuron-authored.**

### Algorithm

```c
function Checker_Run(module, arg2):                     // 0x1e8c800
    bool* found = arg2 ? *(bool**)(arg2 + 8) : null;    // out-param derived from 2nd Run arg (see below)
    for comp in module.computations():
        for inst in comp.instructions():
            if !IsCollectiveOpcode(inst.opcode_): continue          // inline bitmask (op==7 / 0x650 / 0x81…1)
            map = inst[+0x30];                           // frontend_attributes; null ⇒ use kEmptyRare, skip
            map.SyncMapWithRepeatedField();
            if map.size == 0: continue                   // empty map ⇒ skip
            // hash "stream_id" (_Hash_bytes @0x9a1eb00) + probe InnerMap; memcmp key vs literal "stream_id"
            if map.contains_key("stream_id"):
                if found: *found = 1                     // @0x1e8c9e0 — report "a stream_id exists"
                // no rewrite, no further work for this inst
    return StatusOr<bool>{ OK, false };                  // @0x1e8c9e3 — ALWAYS {0, false}
```

### Read-only contract (CERTAIN on mechanism)

The checker's callee list is the proof of its read-only nature: `SyncMapWithRepeatedField`, `_Hash_bytes` (`0x9a1eb00`), and `memcmp` (×4) — **no** `set_channel_id`, **no** map emplace, **no** allocation. It never mutates the module and its `StatusOr<bool>` is **always `{OK, false}`** (`result[+8]=0`, `result[+0]=0` at `0x1e8c9e3`). The only literal it embeds is `"stream_id"` (rodata `0x2768c6`, referenced 5 times); it carries no `Check failed:` strings of its own — the lone abort site in this whole family is the injector's `HasFAKV` corrupted-map guard.

### The second `Run` argument (MED — caller contract not directly observable)

The stock `HloPassInterface::Run` second parameter is `flat_hash_set<string_view> const& execution_threads`. This `Run` instead treats it as a pointer-to-struct and writes a byte through `*(*(arg2)+8)` on the first positive hit. (The injector uses the same arg purely as a harmless scratch out-param for protobuf `FindHelper`.) Because every `Run` invocation reaches the pass indirectly through the vtable (`vptr+0x18`), the exact caller-side struct could not be confirmed from this binary. The most consistent reading: the checker is invoked with a small `{…, bool* found}` context and reports "stream_id present" into `found`.

> **CORRECTION (D-B15) —** an earlier pass survey listed #58 only by name with no role, implying a stream-id *consistency* validator. The binary shows it is a presence detector that always returns "unchanged" and never compares stream-ids across collectives. The mechanism is CERTAIN; the semantic of the `bool* found` out-param is MED because the caller is reached indirectly.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `CollectiveStreamIdChecker::Run` | `0x1e8c800` | read-only `stream_id`-presence scan | CERTAIN |
| `MapFieldBase::SyncMapWithRepeatedField` | `0x9983e80` | proto map sync before probe | CERTAIN |
| `_Hash_bytes` | `0x9a1eb00` | hash `"stream_id"` for the InnerMap probe | CERTAIN |
| `memcmp` | `0x9a1e710` | key string-compare | CERTAIN |

---

## Adversarial Self-Verification

The five strongest claims, re-checked against the binary:

1. **Stream space is exactly {"0","1"}** — VERIFIED. Injector stamps a 1-char string via `_M_construct(1,c)` with `*p = r15d + 0x30`, `r15d` ∈ {0,1} from two `ReplicaGroupsEqual` calls. The only `"stream_id"` reference in the injector is the single emplace at `0x1f9592a`. CERTAIN.
2. **Channel-ids are renumbered from `NextChannelId(module)`** — VERIFIED. Enforcer's callee list contains `hlo_query::NextChannelId` (`0x8ab1ac0`) as its first call, plus `FlatHashSetPolicy<optional<long>>::prepare_insert` and `set_channel_id`. NextChannelId's `constants_used` includes `1` (the floor) and the opcode masks. CERTAIN.
3. **The checker is read-only and always returns {OK,false}** — VERIFIED. Checker callees are only sync/hash/memcmp/stack-check; no setter or allocator; return site `0x1e8c9e3` writes 0/0. CERTAIN.
4. **The `collective_type` vocabulary is exactly the four TP/FSDP literals** — VERIFIED. Stack-built immediates decode to `collective_type`, `tp_all_gather`, `fsdp_all_gather` (and the paired `*_reduce_scatter` family); the `coalesce_fsdp_*` producer symbols confirm the same vocabulary. CERTAIN on the four values.
5. **Opcode bitmasks `0x650` / `0x810000000000001`** — VERIFIED as immediates (`1616` and `580964351930793985`) in both `IsCollective`-using passes and `NextChannelId`. The high-band `−0x1C` rebase and bit positions {0,52,59} are recomputed directly. CERTAIN on masks; the opcode→collective-name mapping for the non-`kAllReduce` bits is **INFERRED** (MED) — the `HloOpcode` enum table was not transcribed on this page; see [Part 13](../part13/channel-id-replica-group.md).

**INFERRED / not traced:** the `collective_type` *producer* pass (upstream of hlo-opt); the checker's exact `arg2` struct type (indirect caller); the precise opcode names behind mask bits other than `op==7` = `kAllReduce`.

---

## Related Passes

| Order | Name | Relationship |
|---|---|---|
| #58 | `CollectiveStreamIdChecker` | runs early; gates whether the injector is needed |
| #81 | `NeuronCollectiveStreamIdInjector` | stamps the 2-way `stream_id` partition |
| #82 | `NeuronUniqueChannelIdEnforcer` | runs immediately after #81; repairs `channel_id` collisions |

> **NOTE —** the registration order (#58 ≪ #81 → #82) is CERTAIN, but the actual driver run-list is assembled in the HLO-to-Tensorizer Python/driver layer, not in `hlo-opt`. The "checker gates injector, enforcer cleans up after injector" reading is the most consistent with the ordering but is not directly observable in this binary.

## Cross-References

- [Channel-ID & Replica-Group](../part13/channel-id-replica-group.md) — Part 13: the `channel_id`/`replica_group` HLO concepts and the `HloOpcode` enum these masks index
- [Collectives to CustomCall](collectives-to-customcall.md) — §4.3: the downstream lowering that consumes `stream_id` to place collectives on backend streams
