# MXU Latency: GL (Ghostlite)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset.*

## Abstract

`xla::ghostlite::MxuLatencyTable` is the Ghostlite (TpuVer 4, v6e) instance of the per-generation MXU reservation model framed in [`mxu-latency-overview`](mxu-latency-overview.md). Where the base `GhostlitePerformance` grid ([`performance-gl-ghperf`](performance-gl-ghperf.md)) supplies an MXU op's *total* pipeline latency, this table supplies its *occupancy* — for each `(opcode-modifier, MxuResource)` pair, how many cycles that one internal MXU sub-unit (gain array, the matrix-staging registers, the matrix-result buffer read port) is held while the op is in flight. The value cell is read by the per-bundle stall recurrence to decide how long the next MXU op must wait before it can issue.

Ghostlite's resource set is narrower than Viperfish's: the reservation vector is `std::array<int,11>`, against Viperfish's `array<int,19>`. The bounds-check literal is correspondingly `11` (`MxuResource::kNumMxuResources`) rather than `19`. The lookup `GetResourceUsage @0x1c8b7560` keys two families — `MatmulModifier` (the matmul map at `this+0x20`) and `MatpushModifier` (the latch/matprep map at `this+0x00`) — by the Ghostlite-specific opcode set, builds the modifier key from the three shared `GainLatchMode` helpers, `find`s the row, and returns `array[resource]`. Two per-resource defaults short-circuit the lookup: `resource==4 → 3` and `resource==9 → 9` seed the latch/matmul-issue slots before any map read.

The defining structural fact of this page is the **GL-vs-GF constructor split**. Ghostlite (v6e) and `6acc60406` (the GF generation) share the `array<int,11>` *shape*, the four modifier *types*, and the three `GainLatchMode` key helpers — but they are two distinct objects built by two distinct constructors with two distinct base-latency value sets. GL's ctor `@0x1c8b2920` is **data-driven**: it fills the maps with templated `SetReservations<Modifier>` calls that densify a sparse `flat_hash_map<MxuResource,int>` into the `array<int,11>`. GF's ctor `@0x1c8bb1c0` is **inline**: it writes `array[res & 0xf] = cy` directly and builds no `Matres` map at all. The base matmul latencies differ — GL prices bf16/F32 at **192** and fp8 at **182**; GF at **211/204** — so the two generations are *not* a shared instance. This page documents GL; the GF twin is on [`mxu-latency-gf`](mxu-latency-gf.md).

For reimplementation, the contract is:

- The Ghostlite `MxuLatencyTable` object: `flat_hash_map<MatpushModifier, array<int,11>>` at `this+0x00`, `flat_hash_map<MatmulModifier, array<int,11>>` at `this+0x20`, plus the `Vlxmr`/`Matres` maps at later offsets, and the per-resource default keys `res4→3`, `res9→9`.
- The reservation bodies: the matpush `{2,1,1}/{4,3,2}/{8,7,6}` width-scaled triplets, the `Vlxmr`/`Matres` rodata sets, and how `SetReservations` densifies them into the dense vector.
- The `GetResourceUsage(instr, resource)` lookup: GL opcode → family → modifier key → `find` → bounds-check `resource < 11` → `array[resource]`.
- The GL/GF ctor split: data-driven `SetReservations` (GL) vs inline direct-write (GF), and the divergent `192/182` vs `211/204` base latencies, so a reimplementation keeps one ctor per silicon generation.

| | |
|---|---|
| **Class** | `xla::ghostlite::MxuLatencyTable` |
| **GL ctor** | `MxuLatencyTable::MxuLatencyTable` `@0x1c8b2920` — data-driven `SetReservations`; `operator new 0xA0` |
| **GL lookup** | `MxuLatencyTable::GetResourceUsage` `@0x1c8b7560` — defaults `res4→3`, `res9→9` |
| **Reservation vector** | `std::array<int,11>` (`MxuResource::kNumMxuResources` = 11) |
| **Owning CycleTable** | `GlcCycleTable` `@0x1c89e7e0` — MXU table at `this+0x18` (`new 0xA0`) |
| **MXU twin** | `MxuLatencyTable1` (MXU1, second instance) — same class, distinct map set |
| **GF counterpart** | GF (`6acc60406`) ctor `@0x1c8bb1c0`, lookup `@0x1c8bdb20` — inline build, `211/204` |
| **Source file** | `…/target/ghostlite/mxu_latency_table_gl.cc` (CHECK/LogFatal anchors) |

---

## Object Layout

### Purpose

The table is built once at `GlcCycleTable` construction and holds only the per-instance hash maps the lookup reads. Each map keys a small modifier struct to a dense `array<int,11>` of per-resource hold cycles.

### Structure

`GlcCycleTable::GlcCycleTable @0x1c89e7e0` allocates the table with `operator new(0xA0)` and stores it at `this+0x18`, alongside a `GhostlitePerformance` (`new 0x30`) at `this+0x10`:

```c
function GlcCycleTable_ctor(this, target):              // @0x1c89e7e0
    this.vtable = off_21C20158
    this.target = target                                // [this+0x08]
    this.perf = new GhostlitePerformance()              // [this+0x10] — new 0x30; @0x1c8cbc80
    this.mxu  = new MxuLatencyTable()                   // [this+0x18] — new 0xA0; @0x1c8b2920
```

The `0xA0`-byte `MxuLatencyTable` body is a set of Abseil flat hash maps at fixed offsets, distinguished by the C++ key *type* the lookup passes to `find`, not by a discriminator field:

```text
MxuLatencyTable (0xA0 bytes, at GlcCycleTable + 0x18)
  this + 0x00   flat_hash_map<MatpushModifier, array<int,11>>   ── latch / matprep ops
  this + 0x20   flat_hash_map<MatmulModifier,  array<int,11>>   ── matmul ops
  this + 0x40   flat_hash_map<VlxmrModifier,   array<int,11>>   ── vector-latch-into-MRB ops
  this + 0x60   flat_hash_map<MatresModifier,  array<int,11>>   ── matrix-result-read ops
  this + 0x80   matmul_latencies_ (MatmulDataFormat → total-latency int)
```

The matpush map address `this+0x00` is read directly as the bare `a2`/`this` in `find<MatpushModifier>`; the matmul map address `this+0x20` is `find<MatmulModifier>(a2 + 32, …)` — both byte-confirmed in `GetResourceUsage @0x1c8b7560`. The `matmul_latencies_` map at `this+0x80` is the per-`MatmulDataFormat` *total* latency, distinct from the per-resource occupancy vectors (the `192/182` values below).

> **QUIRK —** the value `array<int,11>` is read out of the bucket with raw `vmovups` loads, copying all 44 bytes onto the stack before indexing. The matmul read is `vmovups [rdx+8]` (8-byte `MatmulModifier` key precedes the value); the matpush read is `vmovups [rdx+4]` (4-byte `MatpushModifier` key). A reimplementation that assumes a uniform key width reads the array at the wrong offset for one of the two families. This is identical to the Viperfish convention; only `N` shrinks from 19 to 11.

### How a row is built — `SetReservations`

GL populates every map with the templated `SetReservations<Modifier>` (`MatpushModifier @0x1c8b5d80`, `VlxmrModifier @0x1c8b5f60`, `MatresModifier @0x1c8b6140`). Each takes the modifier key plus a sparse `flat_hash_map<MxuResource,int>` (the "this op holds resource k for c cycles" set), zero-inits a dense `array<int,11>`, copies the sparse cells in, and `try_emplace`s the result:

```c
function SetReservations<Modifier>(key, resource_to_cycles):   // GL @0x1c8b5d80 (matpush)
    array<int,11> result_vector = {0}                  // vxorps-zeroed 44-byte vector
    for (resource_index, cycles) in resource_to_cycles:        // SOO-iterated flat_hash_map
        CHECK(resource_index < 11)                     // "resource_index < to_underlying(
                                                       //  MxuResource::kNumMxuResources)" — gl.cc:60
        result_vector[resource_index & 0xF] = cycles
    CHECK(resource_map->try_emplace(key, result_vector).second)   // gl.cc:63 — keys are unique
```

Both CHECKs are byte-confirmed in `SetReservations<MatpushModifier> @0x1c8b5d80`: the bound `*v7 > 0xA` raises `MakeCheckOpString(idx, 11, "resource_index < to_underlying(MxuResource::kNumMxuResources)")` at `gl.cc:60`, and the unique-key CHECK `try_emplace(...).second` fires at `gl.cc:63`. The zero-init means **any resource the op does not name holds it for 0 cycles** — it imposes no stall. The ctor `@0x1c8b2920` issues six `SetReservations<MatpushModifier>` calls (latch-mode key variants `+1`, `+257`, `+2`, `+258`, base, `|0x100`), two `SetReservations<VlxmrModifier>` (keys `0`, `257`), and eight `SetReservations<MatresModifier>` (keys `1..8`).

---

## The Reservation Bodies

### Purpose

The integers in the `array<int,11>` are the throughput model: how many cycles each MXU sub-resource is held per op. They scale with the data-path width, so back-to-back issue of a narrow-format push pipelines at near-issue-rate while a wide x8 push throttles the stream.

### Matpush (latch / matprep) — the width-scaled triplet

The matpush body is a three-resource reservation `{R_0, R_a, R_b}` whose magnitudes scale with the `MatmulDataFormat` width. The value-set is the same `{2,1,1} → {4,3,2} → {8,7,6}` progression seen on Viperfish — GL stages these triplets at `[rbp-0x7c]/[rbp-0x74]/[rbp-0x6c]` in the ctor (`@0x1c8b2a72/2f9a/39fa`) before feeding them to `SetReservations`:

| matpush format / pass | reservation triplet | Confidence |
|---|---|---|
| bf16 single (format 1) | `{2, 1, 1}` | CERTAIN |
| bf16 transposed / doubled | `{4, 3, 2}` | CERTAIN |
| int8 x8 (4-byte-plane quad) | `{8, 7, 6}` | CERTAIN |

The leading element is the gain-array hold; the other two are the matrix-staging-register A/B holds. The `{2,1,1}` bf16 set means a bf16 matpush holds its staging registers for only 1 cycle, so a bf16 latch stream pipelines at ~1-cycle issue while the ~192-cycle latency is hidden across the systolic depth. The `{8,7,6}` int8-x8 set — four times the bf16 hold — reflects the four byte-plane latch sequence and throttles an x8 stream to roughly a quarter of the bf16 issue rate.

### Vlxmr and Matres — the rodata sets

The `Vlxmr` (vector-latch-into-MRB) and `Matres` (matrix-result-read) families take their reservations from `.rodata` pair arrays:

| Family | key(s) | reservation | rodata | Confidence |
|---|---|---|---|---|
| `Vlxmr` | `0` | `{res0: 2}` | `@0xb43bfc0` | CERTAIN |
| `Vlxmr` | `257` (MSR `0x101`) | `{res0: 2, res1: 49}` | `@0xb43bfc8` | CERTAIN |
| `Matres` | `1,2,3,4` | `{res4: 2}` | `@0xb43bfd8` | CERTAIN |
| `Matres` | `5,6,7,8` | `{res4: 1}` | `@0xb43bfe0` | CERTAIN |

The MSR-driven matpush walk reads the GL `kMsrs` list `{1,2,3,3,4,5,6,7,8}` at `@0xb43bfb4`; the inline matpush value-set bytes are `{0,0,1,1,9,0,3,7,a,8,a,4,3,3,3,3,1,1,2,2,3,3}` at `@0xb43bfe8`. These are the v6e-specific MxuResource indices — the `array<int,11>` resource numbering differs from Viperfish's `array<int,19>`, so the *physical* sub-unit at a given index is not shared between the two generations.

> **NOTE —** the MxuResource enum has **no** `ToString` in the binary; the indices `0..10` are used numerically throughout. They are the MXU-internal sub-units (gain array, staging registers A/B, MRB read port, matmul-issue slot), distinct from the higher-level 23-slot `ResourceVector` of [`resource-enum`](resource-enum.md). Index-to-physical-unit binding for GL is by position only (MEDIUM).

---

## The Lookup

### Algorithm

`GetResourceUsage @0x1c8b7560` carries the clean symbol `xla::ghostlite::MxuLatencyTable::GetResourceUsage(GhostlitePerformance::Instruction, Resource, bool)`. It takes a default before any map read, dispatches family by the GL opcode, builds the key, and reads the cell:

```c
function MxuLatencyTable::GetResourceUsage(this, instr, opcode, resource, latch_mode):  // GL @0x1c8b7560
    // 1. Per-resource default fallback, before the table lookup.
    if resource == 4:  default_cycle = 3              // GL latch/issue-slot seed
    elif resource == 9: default_cycle = 9             // GL matmul-issue slot seed
    else: return InvalidArgument("Unsupported kind of resource")   // gl.cc:442

    // 2. Select family + build key from the GL opcode.
    switch (opcode):
        case 292: key = MatmulModifier{format=1};  map = this+0x20      // matmul
        case 298: key = MatmulModifier{format=2};  map = this+0x20
        case 310: key = MatmulModifier{format=6};  map = this+0x20
        case 347: key = MatpushKey(latch_mode);          map = this+0x00  // matpush base
        case 349: key = MatpushKey(latch_mode ^ 0xB);    map = this+0x00  // transposed path
        case 356: key = MatpushKey(latch_mode | 0x14);   map = this+0x00  // wide x8 path
        case 358: key = MatpushKey(latch_mode | 0x18);   map = this+0x00  // wide x8 (alt)
        default:  LogFatal("Unsupported opcode")         // gl.cc:478

    // 3. find + bounds-check + read.
    entry = map.find(key)
    if entry not found: throw out_of_range            // raw_hash_map::at
    array<int,11> res_vector = entry.value            // vmovups copy, key-width-dependent offset
    CHECK(resource < 11)                              // v9 >= 0xB → BUG()
    return res_vector[resource]
```

The `MatpushKey` is `{ byte[0]=GainLatchModeToMatmulDataFormat(mode), byte[1]=LatchModeIsTranspose(mode), byte[2]=3, byte[3]=LatchOpcodeToMsr(0x95) }`, assembled from the three shared helpers `GainLatchModeToMatmulDataFormat @0x1d629260`, `LatchModeIsTranspose @0x1d628ea0`, and `LatchOpcodeToMsr @0x1c8a1300`. The opcode-specific pre-transform of the latch mode (`^0xB`, `|0x14`, `|0x18`) routes the same physical latch into the transposed or wide reservation bucket.

> **GOTCHA —** the GL opcodes are **not** the Viperfish opcodes. Where VF matches matmul on opcode `230` (cases `212/218`) and matpush on `267/271/277`, GL matches matmul on `292/298/310` and matpush on `347/349/356/358` — and GL adds a *fourth* matpush case (`358`, `mode | 0x18`) that Viperfish lacks. GL's `LatchOpcodeToMsr` argument is `0x95` (VF: `0x8F`), and the key byte[2] is `3` (VF byte[3] holds the MSR). Bind the family by the per-gen opcode list, never by a hardcoded constant.

> **NOTE —** The per-resource default keys are per-generation: Viperfish defaults are `res3→15` / `res11→0`; Ghostlite's are `res4→3` / `res9→9`, byte-confirmed at `@0x1c8b7560` (`a4==4 → v9=3`, `a4==9 → v9=9`). A reimplementation must read the GL default-resource keys from the GL lookup; the GL integer matrix reflects the 11-resource indexing.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ghostlite::MxuLatencyTable::MxuLatencyTable` | `0x1c8b2920` | GL ctor — data-driven `SetReservations`; `matmul_latencies_` 192/182 | CERTAIN |
| `ghostlite::MxuLatencyTable::GetResourceUsage` | `0x1c8b7560` | GL lookup — defaults `res4→3`, `res9→9`; bound `< 11` | CERTAIN |
| `ghostlite::SetReservations<MatpushModifier>` | `0x1c8b5d80` | densify `{res→cy}` → `array<int,11>`; CHECK `< 11` (gl.cc:60), `try_emplace` (gl.cc:63) | CERTAIN |
| `ghostlite::SetReservations<VlxmrModifier>` | `0x1c8b5f60` | vlxmr family row builder | CERTAIN |
| `ghostlite::SetReservations<MatresModifier>` | `0x1c8b6140` | matres family row builder | CERTAIN |
| `GlcCycleTable::GlcCycleTable` | `0x1c89e7e0` | owning CycleTable — wires GL perf (`+0x10`) + MXU table (`+0x18`) | CERTAIN |
| `GainLatchModeToMatmulDataFormat` | `0x1d629260` | matpush key byte[0] — shared with VF/GF | CERTAIN |
| `LatchModeIsTranspose` | `0x1d628ea0` | matpush key byte[1] — shared | HIGH |
| `LatchOpcodeToMsr` | `0x1c8a1300` | matpush key byte[3] — GL arg `0x95` | HIGH |
| GF (`6acc60406`) ctor | `0x1c8bb1c0` | the divergent twin — inline build, 211/204 | CERTAIN |
| GF (`6acc60406`) `GetResourceUsage` | `0x1c8bdb20` | GF lookup — `mxu_latency_table_gf.cc`, no res4/res9 remap | CERTAIN |

---

## The GL-vs-GF Constructor Split

### Purpose

Ghostlite (v6e) and `6acc60406` (the GF generation) look like one family — same `array<int,11>` shape, same four modifier types, same `GainLatchMode` key helpers — but they are two distinct objects built by two distinct constructors with two distinct value sets. A reimplementation that treats them as one shared instance prices every `6acc60406` matmul wrong.

### What they share, what diverges

```text
                       GL (Ghostlite, v6e)          GF (`6acc60406`)
  owning CycleTable    GlcCycleTable @0x1c89e7e0     GfcCycleTable @0x1c89eec0
  MxuLatency ctor      @0x1c8b2920                   @0x1c8bb1c0
  GetResourceUsage     @0x1c8b7560                   @0x1c8bdb20
  ─────────────────────────────────────────────────────────────────────────────
  SHARED  : object size 0xA0; array<int,11>; MatmulModifier/MatpushModifier/
            VlxmrModifier/MatresModifier; GainLatchModeToMatmulDataFormat /
            LatchModeIsTranspose / LatchOpcodeToMsr; matpush {2,1,1}/{4,3,2}/{8,7,6}.
  DIVERGE : ctor mechanism, base op-latency, opcode set, default-resource handling.
```

The divergences are byte-confirmed:

- **Build mechanism.** GL's ctor `@0x1c8b2920` is data-driven: `SetReservations<Modifier>` densifies a `flat_hash_map<MxuResource,int>` into the vector (6× matpush, 2× vlxmr, 8× matres). GF's ctor `@0x1c8bb1c0` is inline: per-modifier helpers write `array[res & 0xf] = cy` directly (`cmp al,0xa` bound), and it builds **no** `Matres` map — `SetReservations` does not appear in the GF ctor at all.
- **Base op-latency** (`matmul_latencies_`, the per-`MatmulDataFormat` total latency at `this+0x80`). GL's ctor emits a loop-keyed `matmul_latencies_.try_emplace(format, 192)` and `try_emplace(format, 182)` (`@0x1c8b3092/3205`). GF's ctor names the formats explicitly — `try_emplace(MatmulDataFormat::kF32, 211)`, `(kBf16, 211)`, `(kF8E5M2, 204)`, `(kF8E4M3Fn, 204)` (`@0x1c8bb486/592/699/804`). So GL prices bf16/F32 at **192** and fp8 at **182**; GF at **211/204**.
- **Opcode set + default handling.** GL keys off opcodes `292/298/310` (matmul) and `347/349/356/358` (matpush), seeds `res4→3`/`res9→9` before the lookup, and reads `array[resource]` (with the seed-default short-circuit). GF keys off `289/295/301/307` (matmul, formats `1/2/9/10`) and `324..327` (matpush, with pre-transforms `|0x32`, `^0xB`, `|0x30`), CHECK-bounds `mxu_resource_idx < 11` up front (`gf.cc:415`), and reads `array[a4]` directly with **no** res4/res9 seed remap.

| | GL (Ghostlite) | GF (`6acc60406`) |
|---|---|---|
| bf16 / F32 base latency | 192 | 211 |
| fp8 (E5M2 / E4M3Fn) base latency | 182 | 204 |
| matmul opcodes | 292, 298, 310 | 289, 295, 301, 307 |
| matpush opcodes | 347, 349, 356, 358 | 324, 325, 326, 327 |
| matpush key byte[2] / `LatchOpcodeToMsr` arg | `3` / `0x95` | `1` / `0x91` |
| build | `SetReservations` (data-driven) | inline `array[res]=cy` |
| `Matres` map | yes (8 keys) | none |
| res4/res9 default seed | yes | no (direct index) |
| source file | `mxu_latency_table_gl.cc` | `mxu_latency_table_gf.cc` |

> **NOTE —** the format ordinals the latencies key are now named outright by the source-embedded CHECK strings: GF binds `kF32`/`kBf16` to 211 and `kF8E5M2`/`kF8E4M3Fn` to 204. The parallel GL pair (192/182) is loop-keyed over the same `MatmulDataFormat` key list (`@0x84a2644 = {1,2,3,4}`), so GL bf16/F32 → 192, fp8 → 182 by the same mapping. The two silicon generations price the same matmul op at different total latencies — the strongest single proof that the two MXU tables are not one instance.

---

## The MXU1 Twin

Ghostlite carries a second `MxuLatencyTable` instance — the MXU1 twin — for the second matrix unit. It is the same `xla::ghostlite::MxuLatencyTable` class with the same `array<int,11>` layout and the same `GetResourceUsage` body; only its populated map set differs, reflecting the second MXU's distinct gain-array geometry. The lookup, key construction, default-resource seeds, and bounds-check are identical, so the description above covers both instances. A reimplementation instantiates the class twice (one per MXU) and fills each from its own reservation set; it does not need a second code path. (MEDIUM — the twin is confirmed as a second instance of the same class; its per-cell values were not separately transcribed.)

---

## How the Scheduler Consumes the Reservation

The reservation arrays are consumed by the same two methods described on the overview: `MxuOpResourceReservations` accumulates the per-`MxuResource` hold for an instruction window, and `MxuOpHoldIssues` computes how many cycles the next MXU op must wait given the resources still held by ops in flight — the stall recurrence of [`mxu-opholdissues-stall`](mxu-opholdissues-stall.md). The division of labour mirrors an LLVM `MCSchedModel`: the `GhostlitePerformance` grid supplies the latency (when the result is ready), and this table supplies the occupancy (when the unit is free for the next op). For a back-to-back matmul stream the throughput is gated by the smallest free resource — a bf16 push holding its staging register `{2,1,1}` pipelines at near-issue-rate while the 192-cycle latency hides across the array depth, whereas an int8-x8 push holding `{8,7,6}` throttles the stream to a quarter of that rate.

The same throughput integers surface in the base grid: the `GhostlitePerformance` EUP-prep column carries the `{4 bf16 / 8 fp8}` matmul magnitude, so the two cost tables agree on the per-dtype throughput — see [`performance-gl-ghperf`](performance-gl-ghperf.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-overview` | the shared model, modifier-key construction, and the issue-stall consumer |
| `mxu-latency-gf` | the `6acc60406` (GF) twin — inline build, `211/204` base latencies |
| `mxu-latency-vf` | the Viperfish (v5p) `array<int,19>` instance with the `res3→15`/`res11→0` defaults |
| `performance-gl-ghperf` | the 476×31 base grid whose EUP-prep column agrees on the throughput integers |
| `matmul-mode-modifiers` | the `MatmulMode` ordinals, `MatmulDataFormat` codes, and family→reservation bindings |
| `mxu-opholdissues-stall` | the issue-stall recurrence that consumes the `array<int,11>` rows |

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the shared per-gen reservation model, the four modifier families, the `find → array[resource]` read
- [MXU Latency: GF (6acc60406)](mxu-latency-gf.md) — the `6acc60406` twin; the inline ctor and `211/204` divergence
- [MXU Latency: VF](mxu-latency-vf.md) — the Viperfish `array<int,19>` instance and the `res3→15`/`res11→0` defaults
- [Performance: GL (GhPerf 476×31)](performance-gl-ghperf.md) — the Ghostlite base grid; Xlu deposit res 0x0f; EUP-prep throughput agreement
- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the `MatmulMode` ordinals, `MatmulDataFormat` codes, modifier arrays
- [MxuOpHoldIssues Stall Recurrence](mxu-opholdissues-stall.md) — the consumer that turns reservations into issue stalls
- [Resource Enum (23-slot)](resource-enum.md) — the higher-level `ResourceVector`, distinct from the MXU-internal `MxuResource`
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot whose ops this table prices
