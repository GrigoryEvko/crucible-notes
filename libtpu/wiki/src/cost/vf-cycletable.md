# VfCycleTable

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ. The binary is **not** stripped — every symbol below is a demangled C++ name. `.text` VMA == file offset; `.rodata` VMA == file offset (section `0x84a0000`); `.data.rel.ro` VMA − 0x200000 == file offset.*

## Abstract

`xla::jellyfish::VfCycleTable` is the **throughput half** of the cost model for Viperfish (`TpuVersion 3`, v5e). It is the first `CycleTable` subclass where the family changes shape: where [`JfCycleTable`](jf-cycletable.md) reads its cycle numbers through a flat byte-offset LUT into one `Performance` grid, `VfCycleTable` routes the MXU classes through a *second* object — a `viperfish::MxuLatencyTable` — and only the vector/matrix-result classes touch the `ViperfishPerformance` grid. The single `GetCyclesForThroughput` body is therefore not a LUT read but a 32-arm `switch` that *adapts* each cycle class into one of three call shapes against two collaborating sub-tables. This page is the byte-level transcription of that switch: the full 32-entry cycle-class → (instr, resource, transpose) table, how each arm wraps `MxuLatencyTable::GetResourceUsage` or `ViperfishPerformance::GetResourceUsage`, and the `StatusOr` unwrap that surfaces the cycle integer.

The design fact to carry away: a `VfCycleTable` holds **two** sub-table pointers, not one. Its constructor (`@0x1c89e120`) allocates a `ViperfishPerformance` (`new 0x30`) at `+0x10` and a `MxuLatencyTable` (`new 0xA0`) at `+0x18`, both owned. The matmul/matpush classes (`0x00`–`0x10`) call `MxuLatencyTable::GetResourceUsage(instr, res, transpose)` at `+0x18`; the matrix-result / cross-lane classes (`0x17`, `0x1b`–`0x1f`) call `ViperfishPerformance::GetResourceUsage(instr, 0xe)+1` at `+0x10`. This is the structural inversion versus JF: there the matmul cycles came out of the flat `Performance` grid; here they come out of the per-`(MatmulModifier × MxuResource)` reservation matrix, and the throughput integer is *the same number* the [MXU-latency](mxu-latency-vf.md) reservation model exposes — read through a `res → array-index` remap rather than as a flat cell.

The contract a reimplementer must honor:

- **`GetCyclesForThroughput(cls)` is a `switch`, default return `1`.** `cls >= 0x20` and every unpriced arm return the default `1` cycle; only the matmul/matpush and matrix-result arms read a sub-table.
- **The matmul/matpush "resource" argument is a resource-index *remap*, not a cycle seed.** `MxuLatencyTable::GetResourceUsage` maps `res 3 → array index 15` (`MatmulAccA`) and `res 0xb → array index 0` (`MatpushPushPort`); any other `res` is `InvalidArgument`. The returned cycle is `array[remapped_index]` of the per-modifier `std::array<int,19>` reservation row.
- **The throughput integer is `array[15]` (matmul) or `array[0]` (matpush) of the matched [MXU-latency](mxu-latency-vf.md) reservation row.** The *dispatch* (`res 3 → array[15]`, `res 0xb → array[0]`) is byte-confirmed; the concrete cell magnitudes per `MatmulDataFormat` are owned by the [MXU-latency](mxu-latency-vf.md) / [Performance: VF](performance-vf.md) pages (see CORRECTION (VF-CT-1) below).
- **`GetResource(cls)` is *not* overridden by VF.** It is the gen-invariant `CycleTable::GetResource` (`@0x1c89ce20`, flat LUT `@0xb438aec`) shared with [JF](jf-cycletable.md) and all other gens.
- **Transcendentals bypass the switch** via scalar virtual overrides: `EstimateSinCosCost = 154`, `EstimateTanCost = 170`.

| | |
|---|---|
| **Class** | `xla::jellyfish::VfCycleTable` — serves `TpuVersion 3` (Viperfish v5e) |
| **Throughput reader** | `VfCycleTable::GetCyclesForThroughput(Instruction)` `@0x1c89e2c0` (vtable slot `+0x10`) |
| **Dispatch** | 32-arm `switch` (jump table `@0xb438490`, 32 × i32 self-relative); `cls >= 0x20` → `1` |
| **`ViperfishPerformance*`** | held at `VfCycleTable+0x10` (`new 0x30`); read by mxres classes |
| **`MxuLatencyTable*`** | held at `VfCycleTable+0x18` (`new 0xA0`); read by matmul/matpush classes |
| **MXU read** | `viperfish::MxuLatencyTable::GetResourceUsage(instr, res, transpose)` `@0x1c8ae5c0` → `StatusOr<int>` |
| **mxres read** | `viperfish::ViperfishPerformance::GetResourceUsage(instr, 0xe)` `@0x1c8cbc40`, **`+1`** |
| **res→index remap** | `res 3 → array[15]` (`MatmulAccA`), `res 0xb → array[0]` (`MatpushPushPort`); else `InvalidArgument` |
| **Reservation array** | `std::array<int,19>` — bound `index >= 0x13` → `BUG()` |
| **Resource reader** | `CycleTable::GetResource(Instruction)` `@0x1c89ce20` (gen-invariant; **no** VF override) |
| **Transcendentals** | `EstimateSinCosCost` `@0x1c89e480` = 154; `EstimateTanCost` `@0x1c89e4a0` = 170 |
| **Confidence** | CONFIRMED (byte-anchored, decompile-verified) unless a row says otherwise |

---

## Object Layout — Two Owned Sub-Tables

The `VfCycleTable` constructor (`@0x1c89e120`) is the clearest statement of the v4→v5 shape change. It stores the vtable and the `Target*`, then `new`s and owns two distinct sub-tables:

```c
// xla::jellyfish::VfCycleTable::VfCycleTable(const Target&)  @0x1c89e120  (decompiled, exact)
void VfCycleTable(VfCycleTable *this, const Target *target) {
    *((_QWORD *)this + 1) = target;            // this+0x08 = Target*
    *(_QWORD *)this       = off_21C200D8;       // this+0x00 = vtable
    memset(this + 0x10, 0, 0x10);               // zero the two sub-table ptrs

    auto *perf = (ViperfishPerformance *)operator new(0x30);
    ViperfishPerformance::ViperfishPerformance(perf);
    swap_and_free(this + 0x10, perf);           // this+0x10 = ViperfishPerformance*

    auto *mxu = (MxuLatencyTable *)operator new(0xA0);
    MxuLatencyTable::MxuLatencyTable(mxu);
    swap_and_free(this + 0x18, mxu);            // this+0x18 = MxuLatencyTable*
}
```

| Offset | Field | Owner of | Read by |
|--------|-------|----------|---------|
| `+0x00` | vtable `off_21C200D8` | — | virtual dispatch |
| `+0x08` | `const Target*` | borrowed | device-detail accessors |
| `+0x10` | `ViperfishPerformance*` (`new 0x30`) | owned (`default_delete`) | the mxres classes `0x17`, `0x1b`–`0x1f` |
| `+0x18` | `MxuLatencyTable*` (`new 0xA0`) | owned (`~MxuLatencyTable` + `free`) | the matmul/matpush classes `0x00`–`0x10` |

> **QUIRK — the matmul cycle no longer lives in the `Performance` grid.** A reimplementer porting from [JF](jf-cycletable.md) will look for the MXU throughput cells inside `ViperfishPerformance` and not find them as throughput. On VF the matmul/matpush throughput is the `array[15]`/`array[0]` *reservation* cell of the separate `MxuLatencyTable` at `+0x18`. The `ViperfishPerformance` grid at `+0x10` is consulted only for the matrix-result / cross-lane (Xlu) classes. Two objects, two read strategies, one `GetCyclesForThroughput`.

---

## The Throughput Read Path — `GetCyclesForThroughput`

`VfCycleTable::GetCyclesForThroughput` (`@0x1c89e2c0`) is a `switch` over the cycle-class ordinal (`cls < 0x20` via the jump table `@0xb438490`; anything else falls to `default` and returns `1`). Each priced arm dispatches into one of three shapes:

- **(a) MXU** — `MxuLatencyTable::GetResourceUsage(instr, res, transpose)` at `+0x18`. Returns an `absl::StatusOr<int>`; the cycle is the payload.
- **(b) mxres / Xlu** — `ViperfishPerformance::GetResourceUsage(instr, 0xe)` at `+0x10`, **plus 1**.
- **(c) default / fatal** — `return 1`, or `LogMessageFatal("Unsupported PushGainsS4.")` for classes `0x0a` / `0x10`.

The decompiled body, normalized to the three shapes (hex class ordinals; the decompile prints them decimal):

```c
// xla::jellyfish::VfCycleTable::GetCyclesForThroughput(Instruction cls)  @0x1c89e2c0
//   (decompiled, exact dispatch; instr/res shown in hex)
int64 GetCyclesForThroughput(VfCycleTable *this, int cls) {
    switch (cls) {
        // ---- (a) matmul: MxuLatencyTable @ this+0x18, res 3 -> array[15] = MatmulAccA ----
        case 0x00: return mxu_thru(this, 0xd4, /*res*/3, /*xpose*/0);   // matmul'  rate = THRU(CT0)
        case 0x01: return mxu_thru(this, 0xda, 3, 0);                   // matmul'' rate
        case 0x04: return mxu_thru(this, 0xe6, 3, 0);                   // matmul   base rate
        // ---- (a) matpush: MxuLatencyTable @ this+0x18, res 0xb -> array[0] = MatpushPushPort ----
        case 0x05: return mxu_thru(this, 0x10b, /*res*/0xb, 0);         // matpush  rate
        case 0x06: return mxu_thru(this, 0x10f, 0xb, 0);               // matpush' rate
        case 0x09: return mxu_thru(this, 0x115, 0xb, 0);               // matpush'' rate
        case 0x0b: return mxu_thru(this, 0x10b, 0xb, /*xpose*/1);      // matpush  rate (transposed)
        case 0x0c: return mxu_thru(this, 0x10f, 0xb, 1);              // matpush' rate (transposed)
        case 0x0f: return mxu_thru(this, 0x115, 0xb, 1);             // matpush'' rate (transposed)
        // ---- (c) fatal ----
        case 0x0a:
        case 0x10:
            LogFatal("Unsupported PushGainsS4.", /*cycle_table.cc:682*/);
        // ---- (b) mxres / Xlu: ViperfishPerformance @ this+0x10, res 0xe, +1 ----
        case 0x17: return VfPerf_GetResourceUsage(this, 0x128, 0xe) + 1;   // mxres-class
        case 0x1b: return VfPerf_GetResourceUsage(this, 0x12e, 0xe) + 1;   // reduce-window Xlu
        case 0x1c: return VfPerf_GetResourceUsage(this, 0x11f, 0xe) + 1;   // THE conv MXRES/Xlu
        case 0x1d: return VfPerf_GetResourceUsage(this, 0x123, 0xe) + 1;
        case 0x1f: return VfPerf_GetResourceUsage(this, 0x12a, 0xe) + 1;
        // ---- (c) default ----
        default:   return 1;        // 0x02,0x03,0x07,0x08,0x0d,0x0e,0x11-0x16,0x18-0x1a,0x1e, cls>=0x20
    }
}
```

`mxu_thru` wraps the `StatusOr` unwrap that follows each `MxuLatencyTable::GetResourceUsage` call in the decompile: the call writes a `{status_qword, int_payload}` pair into a stack temporary; the reader tests `status != 1` (status `1` is the OK sentinel) and on a bad status sets the code `55` and calls `absl::internal_statusor::ThrowBadStatusOrAccess`. On the OK path it returns the int payload.

```c
// the StatusOr<int> unwrap repeated after every MxuLatencyTable::GetResourceUsage  (decompiled, exact)
int mxu_thru(VfCycleTable *this, uint16_t instr, int res, bool xpose) {
    StatusOrPair tmp;                                         // {qword status, dword int}
    MxuLatencyTable::GetResourceUsage(&tmp, *(MxuLatencyTable**)(this + 0x18), instr, res, xpose);
    if (tmp.status != 1)                                      // status 1 == OK
        ThrowBadStatusOrAccess(/*code*/55);                   // bad-status path
    return tmp.int_payload;
}
```

> **NOTE — status sentinel `1` is OK, not failure.** The `if (v12 != 1)` branch in the decompile is the *bad-status* branch, because `GetResourceUsage` writes `*(_QWORD*)result = 1` to mark a valid `StatusOr` payload. A reimplementer who reads `!= 1` as "ok" inverts the unwrap. In practice the bad path is unreachable for the priced classes (the modifier maps always contain the keys these arms build); the `throw` exists to surface a missing reservation as a hard failure rather than a silent zero.

> **GOTCHA — the `+1` is on the mxres arms only.** The matrix-result / cross-lane classes (`0x17`, `0x1b`–`0x1f`) return `ViperfishPerformance::GetResourceUsage(instr, 0xe) + 1` — the grid cell *plus one*. The matmul/matpush arms have no `+1`; they return the reservation cell verbatim. A reimplementation that factors the `+1` out to a common tail will over-count every MXU class by one cycle. (The `+1` is the issue-slot overhead the mxres path folds into the throughput; the MXU reservation already includes its own occupancy.)

---

## The 32-Entry Cycle-Class Table

The full jump table (`@0xb438490`, 32 × i32 self-relative) decoded to `(handler, call, role)`. Class ordinals are `CycleTable::Instruction`, the dense `0x00..0x20` enum shared across all gens (see [CycleTable Family](cycletable-family.md)); the role labels are the gen-stable class roles. Every row below is verified against the decompile at `@0x1c89e2c0`.

| CT | handler | dispatch | role / value | Confidence |
|---:|---------|----------|--------------|------------|
| `0x00` | `1c89e2e5` | `MxuLat.GetResourceUsage(0xd4, res3, false)` | matmul' rate = `array[15]` = **THRU(CT0)** | CONFIRMED |
| `0x01` | `1c89e380` | `MxuLat.GetResourceUsage(0xda, res3, false)` | matmul'' rate = `array[15]` | CONFIRMED |
| `0x02` | `1c89e310` | `return 1` | matprep default | CONFIRMED |
| `0x03` | `1c89e310` | `return 1` | matprep default | CONFIRMED |
| `0x04` | `1c89e363` | `MxuLat.GetResourceUsage(0xe6, res3, false)` | matmul base rate = `array[15]` | CONFIRMED |
| `0x05` | `1c89e394` | `MxuLat.GetResourceUsage(0x10b, res0xb, false)` | matpush rate = `array[0]` | CONFIRMED |
| `0x06` | `1c89e3a3` | `MxuLat.GetResourceUsage(0x10f, res0xb, false)` | matpush' rate = `array[0]` | CONFIRMED |
| `0x07` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x08` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x09` | `1c89e325` | `MxuLat.GetResourceUsage(0x115, res0xb, false)` | matpush'' rate = `array[0]` | CONFIRMED |
| `0x0a` | `1c89e42c` | `LogMessageFatal` (`cycle_table.cc:682`) | unsupported (`PushGainsS4` abort) | CONFIRMED |
| `0x0b` | `1c89e354` | `MxuLat.GetResourceUsage(0x10b, res0xb, TRUE)` | matpush rate (transposed) | CONFIRMED |
| `0x0c` | `1c89e3d1` | `MxuLat.GetResourceUsage(0x10f, res0xb, TRUE)` | matpush' rate (transposed) | CONFIRMED |
| `0x0d` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x0e` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x0f` | `1c89e334` | `MxuLat.GetResourceUsage(0x115, res0xb, TRUE)` | matpush'' rate (transposed) | CONFIRMED |
| `0x10` | `1c89e42c` | `LogMessageFatal` | unsupported (`PushGainsS4t` abort) | CONFIRMED |
| `0x11` | `1c89e310` | `return 1` | vector µop default | CONFIRMED |
| `0x12` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x13` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x14` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x15` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x16` | `1c89e310` | `return 1` | default (the reduce-window `R[5]` CT 0x16) | CONFIRMED |
| `0x17` | `1c89e346` | `VfPerf.GetResourceUsage(0x128, res0xe) + 1` | mxres-class throughput | CONFIRMED |
| `0x18` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x19` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x1a` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x1b` | `1c89e410` | `VfPerf.GetResourceUsage(0x12e, res0xe) + 1` | reduce-window Xlu (CT 0x1b) | CONFIRMED |
| `0x1c` | `1c89e317` | `VfPerf.GetResourceUsage(0x11f, res0xe) + 1` | **THE conv MXRES/Xlu (CT 0x1c)** | CONFIRMED |
| `0x1d` | `1c89e405` | `VfPerf.GetResourceUsage(0x123, res0xe) + 1` | mxres-class | CONFIRMED |
| `0x1e` | `1c89e310` | `return 1` | default | CONFIRMED |
| `0x1f` | `1c89e372` | `VfPerf.GetResourceUsage(0x12a, res0xe) + 1` | mxres-class | CONFIRMED |

> **QUIRK — `0x0a`/`0x10` are fatal on VF but not on later gens.** The `PushGainsS4` / `PushGainsS4t` classes hit `LogMessageFatal` ("Unsupported PushGainsS4.", `cycle_table.cc:682`) on Viperfish. The Ghostlite (`GlcCycleTable`) helper maps the same ordinals to a fourth matpush variant (`instr 0x166`) and the `6acc60406` (`GfcCycleTable`) helper turns them into a recoverable error string (`"Unsupported Matrix Operand type:"`) instead of a hard abort. A reimplementation that ports the VF fatal verbatim to a v6 table will crash on a class those gens legitimately price.

The 18 priced arms cover three contiguous bands: matmul (`0x00`/`0x01`/`0x04`), matpush forward + transposed (`0x05`/`0x06`/`0x09`/`0x0b`/`0x0c`/`0x0f`), and matrix-result/Xlu (`0x17`/`0x1b`/`0x1c`/`0x1d`/`0x1f`). Everything else — including the entire vector-µop band `0x11`–`0x16` and `0x18`–`0x1a` — returns the default `1`. The conv cost emitter pulls exactly three of these: `thru(CT 0)` (the matmul rate), `thru(CT 5)` (the matpush rate), and `thru(CT 0x1c)` (the Xlu / matrix-result-read rate).

---

## The Matmul/Matpush Bridge — `MxuLatencyTable::GetResourceUsage`

The matmul/matpush arms are the architectural heart of the page: they show that the VF throughput number is *the same integer* the [MXU-latency reservation model](mxu-latency-vf.md) exposes. `viperfish::MxuLatencyTable::GetResourceUsage(instr, res, transpose)` (`@0x1c8ae5c0`) is **not** a flat lookup keyed on `res`. The `res` argument is a *seed* for which cell of the per-modifier reservation array to return:

```c
// xla::viperfish::MxuLatencyTable::GetResourceUsage(Instruction instr, Resource res, bool xpose)
//   @0x1c8ae5c0  (decompiled, exact dispatch)
StatusOr<int> GetResourceUsage(MxuLatencyTable *this, uint16_t instr, int res, uint8_t xpose) {
    uint8_t array_index;
    if (res == 3)        array_index = 15;       // res 3  -> MatmulAccA
    else if (res == 0xb) array_index = 0;        // res 0xb -> MatpushPushPort
    else return InvalidArgument("Unsupported kind of resource",  // mxu_latency_table_vf.cc
                                /*line 547*/);

    Modifier key;
    switch (instr) {
        // --- matmul: MatmulModifier{[0] = format}, find in map @ this+0x20 ---
        case 0xd4: key = MatmulModifier{ .fmt = 1 }; break;     // bf16
        case 0xda: key = MatmulModifier{ .fmt = 2 }; break;
        case 0xe6: key = MatmulModifier{ .fmt = 6 }; break;     // int8 / x8
        // --- matpush: MatpushModifier via GLM transform, find in map @ this+0x00 ---
        case 0x10b: key = MatpushModifier(GLMToFormat(xpose),       LatchModeIsTranspose(xpose),
                                          LatchOpcodeToMsr(0x8F)); break;       // direct GLM
        case 0x10f: key = MatpushModifier(GLMToFormat(xpose ^ 0xB), ...);   break;   // XOR 0xb
        case 0x115: key = MatpushModifier(GLMToFormat(xpose | 0x14), ...);  break;   // OR  0x14
        default: LogFatal("Unsupported opcode", /*mxu_latency_table_vf.cc:578*/);
    }

    array<int,19> row;
    if (!map.find(key, &row)) ThrowStdOutOfRange("raw_hash_map<>::at");
    if (array_index >= 0x13) BUG();              // array<int,19> bound
    return StatusOr<int>{ ok, row[array_index] };  // <-- the throughput cycle
}
```

The matmul map lives at `this+0x20`, keyed by `MatmulModifier{format}`; the matpush map at `this+0x00`, keyed by `MatpushModifier` after the per-instr `GainLatchMode` transform (`0x10b` direct, `0x10f` XOR `0xb`, `0x115` OR `0x14`) and `LatchOpcodeToMsr(0x8F)`. The returned cycle is `row[array_index]` — `row[15]` (`MatmulAccA`) for matmul, `row[0]` (`MatpushPushPort`) for matpush.

### The throughput integers — the same numbers as the reservation matrix

The *route* is byte-confirmed: `GetResourceUsage` returns `row[15]` (matmul, `MatmulAccA`) or `row[0]` (matpush, `MatpushPushPort`) of the per-`MatmulModifier` / per-`MatpushModifier` reservation `array<int,19>` it finds in the [MXU-latency](mxu-latency-vf.md) maps. The *value* in each cell is per-`MatmulDataFormat`:

| `thru(CT 0)` = matmul rate (`array[15] = MatmulAccA`) | value | Confidence |
|------------------------------------------------------|------:|------------|
| bf16 (`instr 0xd4`, fmt 1) | 8 | UNVERIFIED |
| fmt 2 (`instr 0xda`) | 16 | UNVERIFIED |
| int8 / x8 (`instr 0xe6`, fmt 6) | 32 | UNVERIFIED |

| `thru(CT 5)` = matpush rate (`array[0] = MatpushPushPort`) | value | Confidence |
|-----------------------------------------------------------|------:|------------|
| bf16 narrow (fmt 1) | 2 | UNVERIFIED |
| bf16 mid | 4 | UNVERIFIED |
| int8 / x8 wide (fmt 6) | 8 | UNVERIFIED |

> **CORRECTION (VF-CT-1) —** the *value* tables above are UNVERIFIED and partly contradict the sibling [MXU-latency: VF](mxu-latency-vf.md) page. The reservation rows are not a flat rodata table: they are `{Modifier, array<int,19>}` pairs `insert_range`'d by the `MxuLatencyTable` constructor (`@0x1c8a52c0`) from initializer data near `@0xb43b1d8`, and the in-map record is reassembled (`rdx+8`, `rdx+0x28`, `rdx+0x34`) before indexing — so a clean `array[15] = {8,16,32}` triple at a single address could **not** be byte-anchored here. The MXU-latency page states the per-format `{8,16,32}` matmul scaling lives in the separate `ViperfishPerformance` grid (column r3) and that the VF matpush *reservation* row is a flat `{4,3,2}` (the `{2,1,1}/{4,3,2}/{8,7,6}` per-dtype delta is the generic modifier model, not the VF reservation array). The byte-confirmed fact this page owns is the *dispatch*: `thru(CT 0)` reads `row[15]` and `thru(CT 5)` reads `row[0]` of the matched modifier row. The concrete cell magnitudes belong to and must be re-derived from [MXU-latency: VF](mxu-latency-vf.md) and [Performance: VF](performance-vf.md).

> **GOTCHA — `res 3` does not index slot 3.** The `res` argument names the *concept* (matmul-accumulate vs matpush-push-port), and `GetResourceUsage` translates it to the concrete sub-port column (`3 → 15`, `0xb → 0`). A reimplementation that uses `res` as a direct array index reads the wrong column (`row[3]` and `row[11]` instead of `row[15]`/`row[0]`) and silently returns a different reservation cell. The only two legal `res` values into this function are `3` and `0xb`; anything else is `InvalidArgument`.

---

## The Resource Side — `CycleTable::GetResource` (Gen-Invariant)

`VfCycleTable` does **not** override `GetResource`. The decompile contains no `VfCycleTable::GetResource` symbol; the resource lane for a class comes from the gen-invariant `CycleTable::GetResource` (`@0x1c89ce20`), a flat LUT identical to the one [JF](jf-cycletable.md#the-reslut--cycletablegetresource--dword_b438aec-33--int32-byte-exact) uses:

```c
// xla::jellyfish::CycleTable::GetResource(Instruction cls)  @0x1c89ce20  (decompiled, exact, shared)
int GetResource(CycleTable *this, int cls) {
    return dword_B438AEC[cls];      // resLUT @0xb438aec, 33 × int32, values 0..6
}
```

The cost lambda then deposits `ResourceVector[GetResource(cls)] += (double)GetCyclesForThroughput(cls)` — exactly as on JF. The `resLUT` maps the matmul band `0x00`–`0x04` to `R[1] Matmul`, the latch band `0x05`–`0x10` to `R[0] Matpush`, and the matrix-result / cross-lane classes (`0x17`, `0x1b`–`0x1f`) to `R[2] Xlu`. So the VF switch *prices* the cycle differently from JF (two sub-tables instead of one flat grid), but it *places* the cycle into the identical 23-slot [`ResourceVector`](resource-enum.md) lane. The throughput-pricing logic is gen-private; the resource-conflict bookkeeping is shared.

> **NOTE — the `MxuLatencyTable` `array<int,19>` and the `ResourceVector` (23 slots) are different vectors.** The 19-int reservation row indexed by `MatmulAccA`/`MatpushPushPort` is the [MXU-latency](mxu-latency-vf.md) intra-op micro-port array (one int per MXU sub-pipeline port). The 23-slot [`ResourceVector`](resource-enum.md) is the bundle-issue accumulator the scheduler reduces with `MaxResourceCycles`. `GetResourceUsage` reads the former to produce a number; `GetResource` names a slot in the latter to deposit that number. Do not conflate the two index spaces.

---

## Transcendentals — Scalar Virtual Overrides

Transcendental cost on VF bypasses the switch entirely. The `VfCycleTable` vtable carries two scalar `const` virtual overrides returning a fixed estimate independent of operand size:

```c
int64 VfCycleTable::EstimateSinCosCost(...) @0x1c89e480 { return 154; }   // sin / cos
int64 VfCycleTable::EstimateTanCost(...)    @0x1c89e4a0 { return 170; }   // tan
```

These are the Viperfish entries in the cross-gen transcendental table (JF/PF = 198/219, **VF = 154/170**, GL/GF = 142/151) — the XLU pipeline speeding up across generations. The full per-gen table is on [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md).

---

## VF vs JF — The Shape Change

The two tables answer the same two questions (`GetCyclesForThroughput`, `GetResource`) and feed the same [`ResourceVector`](resource-enum.md), but they read entirely differently:

| Axis | [`JfCycleTable`](jf-cycletable.md) (`TpuVersion` 0/1) | `VfCycleTable` (`TpuVersion` 3) |
|------|------------------------------------------------------|---------------------------------|
| Sub-tables held | one `Performance*` at `+0x10` | `ViperfishPerformance*` at `+0x10` **and** `MxuLatencyTable*` at `+0x18` |
| `GetCyclesForThroughput` form | flat `offsetLUT[cls]` byte-offset read | 32-arm `switch` over three call shapes |
| MXU matmul/matpush cycle | a flat cell in the `Performance` grid (8/1) | `MxuLatencyTable::GetResourceUsage` reservation cell via `res→index` remap |
| matrix-result / Xlu cycle | a flat cell in the `Performance` grid | `ViperfishPerformance::GetResourceUsage(instr, 0xe) + 1` |
| priced-class selector | a literal 33-bit mask `0x19FFC0821` | the `switch` arms themselves |
| unsupported class | falls to default `1` | `0x0a`/`0x10` are `LogMessageFatal` |
| `GetResource` | shared `CycleTable::GetResource` | shared `CycleTable::GetResource` (no override) |
| transcendentals | 198 / 219 | 154 / 170 |
| matmul rate source | flat cell in `Performance` grid | `array[15]` of the matched `MxuLatencyTable` reservation row |

What changed is the *route* — VF moved the matmul throughput out of the flat grid and into the per-`MatmulModifier` reservation matrix (read at `array[15]`) so it can vary by `MatmulDataFormat` without a separate flat cell per format. The concrete per-format magnitudes are owned by [MXU-latency: VF](mxu-latency-vf.md) (see CORRECTION (VF-CT-1)); this page fixes only the read route. Pufferfish (`TpuVersion 2`) is the intermediate form that first introduced the `switch`-over-`GetResourceUsage` shape; VF refines it with the `MxuLatencyTable` split. The Ghostlite (`GlcCycleTable`, `res4→3`/`res9→9`) and `6acc60406` (`GfcCycleTable`, `res3→3`/`res8→8`, distinct opcode set `0x121..0x147`) helpers reuse the VF structure with per-gen remaps and opcode bases.

---

## Reimplementation Recipe

A faithful `VfCycleTable` needs the two sub-tables plus the switch:

```c
int GetCyclesForThroughput(const VfCycleTable *t, uint32_t cls) {
    switch (cls) {
        // matmul (res 3 -> MatmulAccA), matpush (res 0xb -> MatpushPushPort)
        case 0x00: return mxu(t->mxu /*+0x18*/, 0xd4,  3,   0);
        case 0x01: return mxu(t->mxu, 0xda,  3,   0);
        case 0x04: return mxu(t->mxu, 0xe6,  3,   0);
        case 0x05: return mxu(t->mxu, 0x10b, 0xb, 0);
        case 0x06: return mxu(t->mxu, 0x10f, 0xb, 0);
        case 0x09: return mxu(t->mxu, 0x115, 0xb, 0);
        case 0x0b: return mxu(t->mxu, 0x10b, 0xb, 1);
        case 0x0c: return mxu(t->mxu, 0x10f, 0xb, 1);
        case 0x0f: return mxu(t->mxu, 0x115, 0xb, 1);
        case 0x0a: case 0x10: fatal("Unsupported PushGainsS4.");      // cycle_table.cc:682
        // mxres / Xlu (ViperfishPerformance @ +0x10, res 0xe, +1)
        case 0x17: return vfperf(t->perf /*+0x10*/, 0x128, 0xe) + 1;
        case 0x1b: return vfperf(t->perf, 0x12e, 0xe) + 1;
        case 0x1c: return vfperf(t->perf, 0x11f, 0xe) + 1;            // conv Xlu
        case 0x1d: return vfperf(t->perf, 0x123, 0xe) + 1;
        case 0x1f: return vfperf(t->perf, 0x12a, 0xe) + 1;
        default:   return 1;                                          // everything else
    }
}
// mxu(): MxuLatencyTable::GetResourceUsage — remap res (3->15, 0xb->0), find modifier, return row[idx].
// GetResource is the gen-invariant CycleTable::GetResource (resLUT @0xb438aec) — no VF override.
// transcendentals bypass the switch:  sin/cos -> 154,  tan -> 170.
```

The data to embed: the [`MxuLatencyTable`](mxu-latency-vf.md) reservation matrix (the four `flat_hash_map<Modifier, array<int,19>>` maps and the `res→index` remap), the [`ViperfishPerformance`](performance-vf.md) `Instruction × Resource` grid (read at column `0xe`), and the gen-invariant `resLUT` (`@0xb438aec`). With those three tables and the switch above, the VF throughput half is complete.

---

## Cross-References

- [CycleTable Family](cycletable-family.md) — the abstract base, the six-factory registry, the per-version dispatch, and the shared 33-value cycle-class enum.
- [JfCycleTable](jf-cycletable.md) — the flat offset-LUT predecessor; the page this one is the MxuLatencyTable-routed analog of, and the source of the gen-invariant `GetResource` / `resLUT` framing.
- [MXU Latency: VF](mxu-latency-vf.md) — the `viperfish::MxuLatencyTable` reservation matrix this page reads through `GetResourceUsage`: the four modifier maps, the `array<int,19>` rows, the `res→index` remap, and the matmul/matpush cycle values.
- [Performance: VF](performance-vf.md) — the `ViperfishPerformance` `Instruction × Resource` grid the mxres classes (`0x17`, `0x1b`–`0x1f`) read at column `0xe`, plus the `GetResourceUsage` read path.
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` whose head `R[0]..R[6]` the gen-invariant `resLUT` emits, and the `MaxResourceCycles` overlap reduction.
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the cross-gen throughput integers and the transcendental scalar table (VF = 154/170).
- [Performance: VF](performance-vf.md) and [MXU Latency: VF](mxu-latency-vf.md) together hold the two sub-tables a `VfCycleTable` owns at `+0x10` and `+0x18`.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VII — Cost & Latency Model / CycleTable — [back to index](../index.md)
</content>
</invoke>
