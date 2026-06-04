# MXU Latency: VF

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset.*

## Abstract

This page is the Viperfish (v5/v5e) concretization of the [`MxuLatencyTable`](mxu-latency-overview.md) reservation model: the actual integer cycle-hold bodies that price how an MXU op *occupies* the systolic-array sub-units. Where the [overview](mxu-latency-overview.md) documents the gen-invariant index scheme — four op families, four `flat_hash_map`s, a `find → array[resource]` read with a per-gen default fallback — this page dumps the value rows for Viperfish, the widest-resource generation. Every VF reservation row is a fixed-length `std::array<int,19>` (the symbol is literally `std::__u::array<int,19ul>`, and the lookup bounds-checks `resource >= 0x13`), one int per `MxuResource` micro-pipeline port.

The reference frame remains an LLVM `WriteRes`/`ProcResource` table specialized to one functional unit: each MXU op reserves a set of the 19 MXU sub-ports for a set of cycles, and the back-to-back issue stall is gated by the busiest still-held port. The Viperfish table is built once by a ~27 KB constructor (`MxuLatencyTable::MxuLatencyTable` `@0x1c8a52c0`) that fills four maps — matpush (latch/matprep), matmul, matres (result read), and vlxmr (vector-latch-into-MRB) — by densifying small sparse `{MxuResource → cycles}` sets through `SetReservations<Modifier>`. The matpush map is the richest: 32 keys = 8 `MatmulDataFormat` dtypes × {NoXpose, Xpose} × {Msr0, Msr1}, almost all priced uniformly except the half-cost f32-non-transposed case.

The page covers the four-family value matrix value-by-value, the 19-column `MxuResource` band assignment (anchored by the consumer accessors that pin specific columns), the `GetResourceUsage` dispatch and its per-resource default-key set (res3→15, res11→0), and the `AddOverrunCheckReservations` augmentation — which the decompile reveals to be a four-step `{5,13,21,29}` ramp on eight named `kMsr{A,B}OverrunCheck{0..3}` columns, not a single value. The page closes with the MXU1 twin relationship to the second physical issue slot.

For reimplementation, the contract is:

- The four `flat_hash_map<Modifier, array<int,19>>` and the modifier-key construction `GetResourceUsage` builds for each family.
- The 32 matpush reservation rows (the `{[0]:4,[push]:3,[latch]:2}` form, the f32-NoXpose `{[0]:2,...:1,...:1}` half-cost exception), the matres `{[18]:8|4}` tail, the vlxmr `{2,6,14,22,30}` load-feed ramp, and the matmul row's target columns.
- The 19-column `MxuResource` band: which port each index reserves, anchored by `GetResourceUsage` (res11→col0, res3→col15), `GetXluPathReservation` (res14), and the named overrun-check enumerators (cols 2..9).
- The per-resource default fallback (res3→15, res11→0) and the overrun-check `{5,13,21,29}` ramp on `kMsr{A,B}OverrunCheck{0..3}`.

| | |
|---|---|
| **Class** | `xla::viperfish::MxuLatencyTable` |
| **VF ctor** | `xla::viperfish::MxuLatencyTable::MxuLatencyTable` `@0x1c8a52c0` (~27 KB) |
| **VF lookup** | `xla::viperfish::MxuLatencyTable::GetResourceUsage` `@0x1c8ae5c0` |
| **Reservation array** | `std::array<int,19>` — bound `resource >= 0x13` → `BUG()` |
| **`MxuResource` count** | 19 (`MxuResource::kNumMxuResources`, CHECK-anchored, `mxu_latency_table_vf.cc:58`) |
| **Map offsets** | matpush @ `this+0x00` (key+4 array) · matmul @ `this+0x20` (key+8 array) |
| **Default-key set** | `res3 → 15`, `res11 → 0`; any other res → `InvalidArgument` |
| **Row builders** | `SetReservations<{Matpush `@0x1c8abde0`, Vlxmr `@0x1c8accc0`, Matres `@0x1c8acea0`}>` |
| **Overrun aug.** | `AddOverrunCheckReservations` `@0x1c8abfe0` — `{5,13,21,29}` on `kMsr{A,B}OverrunCheck{0..3}` |
| **MXU1 twin** | second `VectorExtended` issue slot, −20-bit control region; shares one reservation backend |

---

## The Reservation Array Shape

### Purpose

Every VF reservation row is a dense 19-int vector indexed by `MxuResource`. The op's reservation is sparse at build time (a 3–6 entry `flat_hash_map<MxuResource,int>`), but it is stored dense: `SetReservations` zero-inits the array and writes only the named cells, so a resource the op never touches holds it for 0 cycles.

### Structure

The four maps sit at fixed offsets in the `MxuLatencyTable` object, distinguished by the key *type* (the C++ template argument), not a discriminator field:

```text
MxuLatencyTable (heap, reachable at owning LatencyTable + 0x1d8)
  this + 0x00   flat_hash_map<MatpushModifier, array<int,19>>   ── latch / matprep  (key 4 B → array at key+4)
  this + 0x20   flat_hash_map<MatmulModifier,  array<int,19>>   ── matmul           (key 6 B → array at key+8)
  this + ...    flat_hash_map<MatresModifier,  array<int,19>>   ── matrix-result read (key 1 B)
  this + ...    flat_hash_map<VlxmrModifier,   array<int,19>>   ── vector-latch-into-MRB (key 4 B)
```

`GetResourceUsage` `@0x1c8ae5c0` reads the matmul map by passing `a2 + 32` (`this+0x20`) to `find<MatmulModifier>` and the matpush map by passing the bare `a2` (`this+0x00`) to `find<MatpushModifier>` — both byte-confirmed. The value array is copied out of the hash bucket with three `vmovups ymm` loads (19 ints = 76 bytes ≈ three 32-byte loads) onto the stack before indexing. The copy offset is key-width-dependent: `vmovups [rdx+8]` for the 8-byte-aligned `MatmulModifier` bucket, `vmovups [rdx+4]` for the 4-byte `MatpushModifier` bucket.

### How a row is built — `SetReservations`

`SetReservations<MatpushModifier>` `@0x1c8abde0` (verified) densifies a sparse `{MxuResource → cycles}` map into the 19-int array and `try_emplace`s it:

```c
function SetReservations<Modifier>(packed_key, resource_to_cycles_map, target_map):  // VF @0x1c8abde0
    array<int,19> res_vector = {0};                  // vxorps + vmovups zero-init, 19 slots
    for (resource_index, cycles) in resource_to_cycles_map:
        if resource_index >= 19:                      // mxu_latency_table_vf.cc:58
            LogFatal(MakeCheckOpString(resource_index, 19,
                     "resource_index < to_underlying(MxuResource::kNumMxuResources)"))
        res_vector[resource_index] = cycles
    if not target_map->try_emplace(packed_key, res_vector).second:   // mxu_latency_table_vf.cc:71
        LogFatal("resource_map->try_emplace(key, res_vector).second")   // keys must be unique
```

The store of the 19-int array into the new bucket is itself three `vmovups` writes (`[rdx+4]`, `[rdx+0x24]`, `[rdx+0x30]`) — the array lands at bucket offset 4, after the 4-byte `MatpushModifier` key. `SetReservations<VlxmrModifier>` `@0x1c8accc0` and `SetReservations<MatresModifier>` `@0x1c8acea0` are byte-identical in the bound (`>= 0x13`) and the two CHECK strings (`vf.cc:58`, `vf.cc:71`), differing only in the key type. All three CHECKs are byte-confirmed in the decompile.

> **NOTE —** the array width is the **resource count**, not a row count. `MxuResource::kNumMxuResources = 19` on Viperfish, vs 11 on Ghostlite / TPU7x. The two generations therefore index *different physical sub-units by the same integer*, and a reimplementation must keep one `MxuResource` enum per gen. The 19 is byte-anchored by the bound `resource >= 0x13` in both `SetReservations` (write side) and `GetResourceUsage` (read side).

---

## The Lookup and the Default-Key Set

### Algorithm

`GetResourceUsage` `@0x1c8ae5c0` is byte-confirmed:

```c
function MxuLatencyTable::GetResourceUsage(this, instr, resource, is_throughput):  // VF @0x1c8ae5c0
    // 1. Per-resource default fallback, taken before any table lookup.
    if resource == 3:    default_cycle = 15           // VF latch/issue-slot seed
    elif resource == 11: default_cycle = 0            // VF matmul-issue slot seed
    else:                                             // status path, vf.cc
        return InvalidArgument("Unsupported kind of resource")
    if instr > 0x10A:  goto matpush_dispatch          // opcodes above the matmul band

    // 2. Matmul family — key byte[0] = MatmulDataFormat, byte[1..] = 0; map @ this+0x20.
    switch (instr):
        case 212: key = MatmulModifier{format=1};  if find(this+0x20, key): read array[resource]
        case 218: key = MatmulModifier{format=2};  ...
        case 230: key = MatmulModifier{format=6};  ...
        default:  goto matpush_dispatch

  matpush_dispatch:
    // 3. Matpush family — key from the latch mode, map @ this+0x00.
    switch (instr):
        case 267: key = MatpushKey(latch_mode)            // plain matpush
        case 271: key = MatpushKey(latch_mode ^ 0xB)      // xpose pass
        case 277: key = MatpushKey(latch_mode | 0x14)     // wide pass
        default:  LogFatal("Unsupported opcode")          // vf.cc:578
    if not find(this+0x00, key): throw out_of_range       // raw_hash_map::at

    // 4. Read.
    array<int,19> res_vector = entry.value                // vmovups copy ([rdx+8] matmul / [rdx+4] matpush)
    if resource >= 19: BUG()                              // bound 0x13
    return res_vector[resource]
```

The matpush key is assembled from three helpers: `byte[0] = GainLatchModeToMatmulDataFormat(latch_mode)` (`@0x1d629260`), `byte[1..2] = LatchModeIsTranspose(latch_mode)` (`@0x1d628ea0`), `byte[3] = LatchOpcodeToMsr(0x8F)` (`@0x1c8a1300`). The opcode-specific pre-transform — `^0xB` for opcode 271, `|0x14` for opcode 277 — routes the same physical latch into the transposed or wide bucket.

> **NOTE —** The per-resource default keys are **per-generation**. On Viperfish `resource==3 → 15` and `resource==11 → 0` (`a4==3 → v9=15`, `a4==11 → v9=0` at `@0x1c8ae5c0`); on Ghostlite/TPU7x they shift to `resource==4 → 3` / `resource==9 → 9`. A reimplementation must read the default-resource keys from the per-gen lookup, not carry the VF pair across gens. Any `resource` other than 3 or 11 returns an `InvalidArgument` Status ("Unsupported kind of resource"), not an out-of-bounds read.

> **GOTCHA —** the matmul opcode is **not** stable across gens. VF matches matmul on opcodes 212/218/230 (formats 1/2/6) and matpush on 267/271/277; Ghostlite matches a different opcode set. Bind the family by the per-gen opcode list. The matmul opcode 230 = `0xE6` is the `s8` matmul ordinal (format 6 = int8/x8); 212 = `0xD4` is f32 (format 1); 218 = `0xDA` is bf16 (format 2).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `viperfish::MxuLatencyTable::MxuLatencyTable` | `0x1c8a52c0` | VF ctor — fills all four maps (~27 KB) | CERTAIN |
| `viperfish::MxuLatencyTable::GetResourceUsage` | `0x1c8ae5c0` | VF lookup — family dispatch + `find` + `array[resource]` | CERTAIN |
| `viperfish::SetReservations<MatpushModifier>` | `0x1c8abde0` | densify `{res→cyc}` → `array<int,19>`, `try_emplace` | CERTAIN |
| `viperfish::SetReservations<VlxmrModifier>` | `0x1c8accc0` | vlxmr row builder (bound 0x13) | CERTAIN |
| `viperfish::SetReservations<MatresModifier>` | `0x1c8acea0` | matres row builder (bound 0x13) | CERTAIN |
| `viperfish::AddOverrunCheckReservations` | `0x1c8abfe0` | inserts `kMsr{A,B}OverrunCheck{0..3}` → `{5,13,21,29}` | CERTAIN |
| `GainLatchModeToMatmulDataFormat` | `0x1d629260` | matpush key byte[0] | CERTAIN |
| `LatchModeIsTranspose` | `0x1d628ea0` | matpush key byte[1..2] | HIGH |
| `LatchOpcodeToMsr` | `0x1c8a1300` | matpush key byte[3] | HIGH |
| `viperfish::MxuLatencyTable::MxuOpResourceReservations` | `0x1c8ad080` | accumulate per-resource reservations over a window | HIGH |
| `viperfish::MxuLatencyTable::MxuOpHoldIssues` | `0x1c8ad3a0` | back-to-back issue stall recurrence | HIGH |
| `LatencyTableViperfish::GetXluPathReservation` | `0x1c8a3200` | reads Xlu deposit via `ViperfishPerformance` res 0x0e (anchors `MxuResource` col 14) | CERTAIN |

---

## The 19 MxuResource Columns

### Purpose

The 19 `MxuResource` indices are the MXU micro-pipeline reservation ports. The enum (`xla::viperfish::MxuResource`) has no `ToString` in the binary, so most columns are named functionally by their writer-modifier role. Several columns are pinned by direct accessor or `try_emplace` CHECK-string evidence and are CERTAIN-grade.

### The columns

| col | name | anchor | written by | Confidence |
|---|---|---|---|---|
| 0 | MXU issue / dispatch port | `GetResourceUsage` `ViperfishPerformance::Resource 0xb → col0` | matpush, matmul | CERTAIN |
| 1 | vlxmr load-feed stage 0 | ctor vlxmr map | vlxmr (No + Xpose) | HIGH |
| 2 | `kMsrAOverrunCheck0` | `AddOverrunCheckReservations` CHECK `vf.cc:78` | overrun (MSR-A), matmul | CERTAIN |
| 3 | `kMsrAOverrunCheck1` | overrun CHECK `vf.cc:80` | overrun (MSR-A) | CERTAIN |
| 4 | `kMsrAOverrunCheck2` | overrun CHECK `vf.cc:82` | overrun (MSR-A), matmul | CERTAIN |
| 5 | `kMsrAOverrunCheck3` | overrun CHECK `vf.cc:84` | overrun (MSR-A), matmul | CERTAIN |
| 6 | `kMsrBOverrunCheck0` | overrun CHECK `vf.cc:87` | overrun (MSR-B) | CERTAIN |
| 7 | `kMsrBOverrunCheck1` | overrun CHECK `vf.cc:89` | overrun (MSR-B) | CERTAIN |
| 8 | `kMsrBOverrunCheck2` | overrun CHECK `vf.cc:91` | overrun (MSR-B), matmul | CERTAIN |
| 9 | `kMsrBOverrunCheck3` | overrun CHECK `vf.cc:93` | overrun (MSR-B) | CERTAIN |
| 10 | matpush PUSH port — MSR bank 0 | ctor `Msr0` push pair | matpush(Msr0), matmul | CERTAIN |
| 11 | matpush PUSH port — MSR bank 1 | ctor `Msr1` push pair | matpush(Msr1) | CERTAIN |
| 12 | matpush LATCH port — MSR bank 0 | ctor `Msr0` latch pair | matpush(Msr0), matmul | CERTAIN |
| 13 | matpush LATCH port — MSR bank 1 | ctor `Msr1` latch pair | matpush(Msr1), matmul | CERTAIN |
| 14 | Xlu / matrix-result deposit | `GetXluPathReservation` → `ViperfishPerformance` res 0xe | vlxmr(Xpose) | CERTAIN |
| 15 | MXU-result port (matmul throughput `{8,16,32}`) | `GetResourceUsage` `Resource 3 → col15`; ctor key `15 → {8,16,32}` (fmt 1/2/6) | matmul | CERTAIN |
| 16 | MXU-result sub-stage A | ctor matmul map | matmul | MEDIUM |
| 17 | MXU-result sub-stage B | ctor matmul map | matmul | MEDIUM |
| 18 | matmul-result-feed / accumulate tail | matres row `[18] = 8 \| 4` | matres | CERTAIN |

> **NOTE —** Cols 2..9 are the `OverrunCheck` reservations, installed by `AddOverrunCheckReservations` `@0x1c8abfe0`: the named enumerators `kMsrAOverrunCheck{0,1,2,3}` (cols 2,3,4,5) and `kMsrBOverrunCheck{0,1,2,3}` (cols 6,7,8,9), each carrying a **graduated ramp `{5,13,21,29}`** (not a flat value), behind a distinct `try_emplace(...).second` CHECK string at `vf.cc:78..93`. The MSR-A set is taken on the `a1 != 1` (MSR0) path and the MSR-B set on `a1 == 1` (MSR1); `a1 ∉ {0,1}` is a `LogFatal("Invalid MSR.")` at `vf.cc:95`. The vlxmr feed occupies col 1 in its own rows, but the overrun augmentation is what gives cols 2..9 their names.

### The MSR bank selection

The matpush PUSH/LATCH ports follow the `Msr` byte of the key: `Msr0 → {push col 10, latch col 12}`, `Msr1 → {push col 11, latch col 13}`. Viperfish has two matrix-staging-register banks (`ViperfishTarget::MatrixStagingRegisterCount` `@0x1d49ace0` = 2), so the 1-bit `Target` field on the latch (decoded at bundle abs 58, see [`slot-mxu`](../isa/slot-mxu.md)) picks which bank, and that bank index becomes key byte[3] = `LatchOpcodeToMsr(0x8F)`.

---

## The Matpush Reservation Rows

### Purpose

The matpush (latch/matprep) family is the moving-operand stage; its reservation is what gates back-to-back latch issue. The VF ctor builds 32 keys: an outer loop over `kMsrs = {0, 1}` (`@0xb43b1df`, loop base `+0x21`) and an inner loop over the format table `{2,3,4,5,6,7,8}` (`@0xb43b1d8`, bf16/f8e5m2.bf16/f8e4m3b11.bf16/u8/s8/u4/s4), plus the f32 keys built directly — each crossed with {NoXpose, Xpose}.

### The value matrix

The 32 rows reduce to one rule plus one exception. Every key gets `{[0]:4, [push]:3, [latch]:2}` **except** `{f32, NoXpose, *}` which gets the half-cost `{[0]:2, [push]:1, [latch]:1}`:

```text
array(fmt, xpose, Msr) = (fmt == 1 (f32) && !xpose)
    ? { [0]:2, [10+Msr]:1, [12+Msr]:1 }     // f32 NoXpose — HALF cost
    : { [0]:4, [10+Msr]:3, [12+Msr]:2 };    // everything else
where push-col = 10+Msr, latch-col = 12+Msr  (Msr ∈ {0,1})
```

| key (`{fmt,xpose,0,Msr}`) | fmt | xpose | Msr | array<19> non-zero cells |
|---|---|---|---|---|
| `0x00000001` | 1 (f32) | no | 0 | `[0]=2 [10]=1 [12]=1` (half) |
| `0x01000001` | 1 (f32) | no | 1 | `[0]=2 [11]=1 [13]=1` (half) |
| `0x00000101` | 1 (f32) | yes | 0 | `[0]=4 [10]=3 [12]=2` |
| `0x01000101` | 1 (f32) | yes | 1 | `[0]=4 [11]=3 [13]=2` |
| `0x000000NN` | 2..8 | no | 0 | `[0]=4 [10]=3 [12]=2` |
| `0x010000NN` | 2..8 | no | 1 | `[0]=4 [11]=3 [13]=2` |
| `0x000001NN` | 2..8 | yes | 0 | `[0]=4 [10]=3 [12]=2` |
| `0x010001NN` | 2..8 | yes | 1 | `[0]=4 [11]=3 [13]=2` |

The 32 keys span `fmt ∈ {1..8}` (`NN = 02..08`). The format codes are `MatmulDataFormat`: 1 f32, 2 bf16, 3 f8e5m2.bf16, 4 f8e4m3b11.bf16, 5 u8, 6 s8, 7 u4, 8 s4. `fmt 9` (f8e5m2) and `fmt 10` (f8e4m3fn) have **no** VF matpush key — the ctor format table tops out at 8; those packed FP8 formats are Ghostlite/TPU7x-only.

> **QUIRK —** the per-dtype throughput delta the [overview](mxu-latency-overview.md) describes does **not** live in the VF *matpush* reservation row. On Viperfish every matpush key but f32-NoXpose pays the flat `{[0]:4, [push]:3, [latch]:2}` (the f32-NoXpose half-cost is `{[0]:2, [push]:1, [latch]:1}`); the matpush dtype only forks on f32-vs-rest × transpose, so `array[0]` (`MxuResource 0`, the issue/dispatch port) is a flat **4** (or **2** for f32-NoXpose) — **not** a `{2,4,8}` per-format ramp (`*(_BYTE *)v998 = 0; LODWORD(v998[1]) = {2|4}` at ctor `@0x1c8a52c0` lines 650/683). The per-format throughput magnitude `{8,16,32}` (format 1/2/6 → 8/16/32) instead lands in the *matmul* reservation array at `array[15]` (`MxuResource 15`, the MXU-result port), byte-anchored **here** in this ctor: `*(_BYTE *)v145 = 15; *((_DWORD *)v145 + 1) = 8` (line 1466/1467, fmt 1), `15 → 16` (line 1946/1947, fmt 2), `15 → 32` (line 2855/2856 and 3287/3288, fmt 6). It is mirrored in the `ViperfishPerformance` grid col r3 (see [`performance-vf`](performance-vf.md)). A reimplementation that puts the per-format scaling in the *matpush* reservation will over-cost integer latches on VF — the per-format scaling belongs in the *matmul* `array[15]` instead.

---

## The Sibling Families — Matres, Vlxmr, Matmul

### Matres — the result-read tail

`MatresModifier` is a 1-byte `{fmt}` key (the array is read at key+4). The whole row is a single tail cell at col 18, dtype-forked:

| key / fmt | dtype | array<19> |
|---|---|---|
| `0x01..0x04` | f32 / bf16 / f8e5m2.bf16 / f8e4m3b11.bf16 | `[18] = 8` |
| `0x05..0x08` | u8 / s8 / u4 / s4 | `[18] = 4` |

The matmul-result tail holds the result-feed port (col 18) for 8 cycles on the wide/decompose dtypes and 4 on the integer dtypes — the integer accumulate retires twice as fast.

### Vlxmr — the vector-latch-into-MRB feed

`VlxmrModifier` is a 4-byte `{fmt, xpose, 0, Msr}` key (same shape as matpush; array at key+4). The VF ctor builds only two keys — a coarser dimension than the 32-key matpush family — and the row is a monotone load-feed ramp:

| key | fmt | xpose | Msr | array<19> |
|---|---|---|---|---|
| `0x00000000` | 0 | no | 0 | `[1]=2 [2]=6 [3]=14 [4]=22 [5]=30` |
| `0x00000101` | 1 | yes | 0 | `[1]=2 [6]=6 [7]=14 [8]=22 [9]=30 [14]=33` |

The NoXpose feed fills cols 1..5 with the `{2,6,14,22,30}` (step-8 from 6) ramp; the Xpose variant shifts the ramp to cols 1,6..9 and adds `[14]=33`, the Xlu/matrix-result deposit (col 14 — the same port `GetXluPathReservation` reads). The Xpose vlxmr is the only family member that touches the Xlu cross-lane port.

> **NOTE —** the overrun-check insertion (`{5,13,21,29}` on cols 2..9) overlaps the same `MxuResource` columns the NoXpose-vlxmr feed (cols 2..5) and Xpose-vlxmr feed (cols 6..9) write into. These are distinct map entries (vlxmr vs matmul-augmented), so they never collide in one row; the column reuse reflects that the MSR overrun-check ports and the vlxmr feed stages are the same physical sub-pipeline at different occupancy depths.

### Matmul — the pure-matmul reservation

`MatmulModifier` is a 6-byte `{fmt, ..., gains}` packed key — wider than the 4-byte matpush/vlxmr and 1-byte matres keys — so its array is read at key+**8** (the `vmovups [rdx+8]` in the lookup), vs key+4 for the others. `GetResourceUsage` builds the lookup key with only byte[0]=fmt set (1/2/6 for opcodes 212/218/230); the ctor builds 12 keys with `b2 ∈ {0,1}` and the gains bit `b5 ∈ {0,1}`. The pure-matmul row targets cols `{0, 2, 4, 5, 8, 10, 12, 13, 15, 16, 17}` — the issue port (0), the overrun-augmented MSR ports (2,4,5,8), the push/latch banks (10,12,13), and the three mxu-result ports (15,16,17). Col 15 carries the byte-anchored per-format throughput magnitude `array[15] = {8,16,32}` for formats 1/2/6 (ctor `@0x1c8a52c0:1466/1947/2856/3288`), the cell `GetResourceUsage` returns on the `resource==3 → 15` default path.

> **GOTCHA (matmul values — partly pinned) —** the matmul `array[15]` throughput cell is **byte-anchored CERTAIN**: the ctor writes key `MxuResource 15 → {8,16,32}` for formats 1/2/6 (`*(_BYTE *)v145 = 15; *((_DWORD *)v145 + 1) = 8` at `@0x1c8a52c0:1466`, `= 16` at `:1947`, `= 32` at `:2856`/`:3288`), and `GetResourceUsage` reads exactly this cell on the `resource==3 → 15` default path. The *remaining* MatmulModifier cells (the push/latch banks 10/12/13, the MSR overrun ports 2/4/5/8, mxu-result sub-stages 16/17) are built inline by `find_or_prepare_insert_large<MatmulModifier>` (`@0x1c8af3e0`) copying the `AddOverrunCheck`-augmented input map, a path that resists full static value extraction — the KEY structure (6-byte, array at +8), the target column set, and the overrun augmentation (`{5,13,21,29}` on cols 2..9 per MSR bank) are CONFIRMED, but those non-`[15]` cell integers are MEDIUM and a reimplementer should re-derive them from the ctor double loop. The matmul *latency* (the systolic depth, not the occupancy) lives in the [`ViperfishPerformance`](performance-vf.md) grid (bf16 base 121/131).

---

## The Overrun-Check Augmentation

`AddOverrunCheckReservations` `@0x1c8abfe0` is called once per MSR bank before each matmul-map insert, augmenting the input `{MxuResource → int}` map with the structural-hazard cost of a matpush overrunning the staging register. The decompile shows it is **not** a single value — it is a four-step ramp inserted under named enumerators:

```c
function AddOverrunCheckReservations(msr, reservation_map):   // VF @0x1c8abfe0
    if msr == 1:                                               // MSR-B bank
        reservation_map.try_emplace(kMsrBOverrunCheck0,  5)    // vf.cc:87, col 6
        reservation_map.try_emplace(kMsrBOverrunCheck1, 13)    // vf.cc:89, col 7
        reservation_map.try_emplace(kMsrBOverrunCheck2, 21)    // vf.cc:91, col 8
        reservation_map.try_emplace(kMsrBOverrunCheck3, 29)    // vf.cc:93, col 9
    elif msr == 0:                                             // MSR-A bank
        reservation_map.try_emplace(kMsrAOverrunCheck0,  5)    // vf.cc:78, col 2
        reservation_map.try_emplace(kMsrAOverrunCheck1, 13)    // vf.cc:80, col 3
        reservation_map.try_emplace(kMsrAOverrunCheck2, 21)    // vf.cc:82, col 4
        reservation_map.try_emplace(kMsrAOverrunCheck3, 29)    // vf.cc:84, col 5
    else:
        LogFatal("Invalid MSR.")                               // vf.cc:95
```

The `{5,13,21,29}` ramp (step 8) is the per-K-tile overrun cost: a matpush that overruns the staging register holds the overrun-check port progressively longer (5 → 13 → 21 → 29 cycles) for deeper overrun depths 0..3. Each `try_emplace` is guarded by its own `.second` CHECK so the same overrun-check resource is never inserted twice into one matmul row. This is the structural-hazard half of the matmul reservation, folded into the matmul map so the consumer sees it as part of the normal array.

---

## The MXU1 Twin Relationship

Viperfish issues MXU ops through **two** `VectorExtended` bundle slots (MXU0 / MXU1), a fixed −20-bit control-region twin over one shared 8×6-bit operand pool ([`slot-mxu`](../isa/slot-mxu.md), [`decode-side-vf-gxc`](../isa/decode-side-vf-gxc.md)). The cost model does not duplicate the reservation table per slot: the `MxuLatencyTable` is keyed on the `MatmulDataFormat`/`GainLatchMode` modifier, not on which physical slot or which of the four physical MXU arrays issued the op. Both MXU1 and MXU0 ops resolve to the same `find → array[resource]` read against the same four maps.

The slot-vs-array orthogonality (two control slots, `mxu_count = 4` physical arrays) is the bundle encoder's concern; the reservation model collapses it to the modifier key. `MxuOpResourceReservations` `@0x1c8ad080` accumulates the per-`MxuResource` reservation an instruction window imposes across both slots, and `MxuOpHoldIssues` `@0x1c8ad3a0` computes how many cycles the next MXU op stalls given the resources still held — the stall recurrence the [overview](mxu-latency-overview.md) and [`mxu-opholdissues-stall`](mxu-opholdissues-stall.md) detail.

> **NOTE —** because matpush holds its push/latch ports for only 1–3 cycles (the `{2,1,1}`/`{4,3,2}` rows) while the systolic matmul latency is multi-hundred-cycle, a back-to-back bf16 latch stream on VF pipelines at near-issue-rate; the int8 path's reservation is the same `{4,3,2}` on VF (the dtype scaling lives in the `ViperfishPerformance` grid), so the matpush *occupancy* alone does not throttle int8 relative to bf16 on this generation — a VF-specific quirk vs the `{8,7,6}` scaling the [overview](mxu-latency-overview.md) describes for the modifier model in general.

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-overview` | the gen-invariant index scheme this page's integers fill |
| `matmul-mode-modifiers` | the `MatmulDataFormat`/`MatpushModifier` key bytes and format → group binding |
| `performance-vf` | the separate `ViperfishPerformance` grid (28 cols) — the per-format `{8,16,32}` matmul throughput cells distinct from this `array<int,19>` |
| `resource-enum` | the higher-level 23-slot `ResourceVector` — distinct from `MxuResource` |
| `mxu-latency-gl` / `-gf` / `-pf` | the `array<int,11>` (GL/GF) and PF sibling reservation matrices |
| `mxu-opholdissues-stall` | the issue-stall recurrence that consumes these arrays |

---

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuLatencyTable` model, the four families, the `find → array[resource]` read this page concretizes for VF
- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the `MatmulModifier`/`MatpushModifier` key bytes, `MatmulDataFormat` codes, and format → reservation-group binding
- [MXU Latency: PF](mxu-latency-pf.md) — the Pufferfish reservation matrix (single-MSR, no Target field)
- [MXU Latency: GL (Ghostlite)](mxu-latency-gl.md) — the `array<int,11>` reservation matrix with shifted defaults (res4→3, res9→9)
- [MXU Latency: GF (TPU7x)](mxu-latency-gf.md) — the TPU7x `array<int,11>` reservation matrix
- [Performance: VF](performance-vf.md) — the 28-column `ViperfishPerformance` grid, the matmul throughput cells, and res 0x0e Xlu deposit that co-exist with this reservation table
- [Resource Enum (23-slot)](resource-enum.md) — the per-bundle `ResourceVector`, distinct from the MXU-internal `MxuResource`
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot whose latch/matmul ops this table prices; the Transpose/Target latch bits feeding the matpush key
- [Decode-Side: VF / GXC](../isa/decode-side-vf-gxc.md) — the MXU1 −20 twin and the abs 57/58 Transpose/Target fields the matpush key reads
- [MxuOpHoldIssues Stall Recurrence](mxu-opholdissues-stall.md) — how `MxuOpResourceReservations`/`MxuOpHoldIssues` turn these arrays into issue stalls
