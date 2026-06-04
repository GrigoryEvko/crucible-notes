# GetReplicaPair3DOnTwistedTorus

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); `.rodata` VMA equals file offset (base `0x84a0000`). All addresses are VMA. Every symbol and `.rodata` table below is present in the full-symbol binary and cross-checked against the IDA decompile; the five route tables are byte-exact `.rodata` dumps.*

## Abstract

This page owns **two byte-level mechanisms** that turn an abstract collective ring step into a concrete chip and, separately, a concrete physical ICI link:

1. **`xla::jellyfish::GetReplicaPair3DOnTwistedTorus`** (`0x1c893400`, `group_utils.cc`) — the coordinate fold both replica-group builders call. Given the three loop indices of a Phase0/Phase1 group and the twisted-torus shape scalars `(2K, K, num_max_dims, orientation)`, it computes the physical coordinates `(cY, cX, cZ)` of the chip the twisted `2K` ring places at that step, then returns `map[cY][cX][cZ]` — the `{core0, core1}` megacore logical-device pair from `GetPhysicalToLogicalMapping3D`. The fold is gated by a fatal `CHECK("num_max_dims == 2")` on every two-doubled-axis branch.
2. **The n-hop Mapper hop-offset tables** the limited-ICI route consumer (`DmaDestinationRoutingTableEntryMapper`) reads to turn a `(src_chip, dst_chip)` pair into a physical DMA destination port. `kCaseHopsSignToOffsets` (`0xb8f0fb0`, 32-entry single-axis table, `std::lower_bound` keyed on `(routing_case, hop_len, sign)`) handles the one/two/four/eight-hop single-axis case; the four `y_routing`/`x_routing` diagonal tables (`0xb8f0e70`–`0xb8f0f70`) handle the two-axes-at-once case; `GetHopLength` (`0x1fc59c80`) snaps a coordinate delta to the `{1,2,4,8}` hop ladder.

The 3-nested-loop replica-group construction that *calls* this fold is on [2-Phase Replica-Group Construction](replica-group-2phase.md); the orientation/polarity enum the fold dispatches on is on [Twist Predicate & Orientation](twist-predicate-orientation.md). This page owns the **fold body + the `num_max_dims == 2` precondition + the hop-offset tables**.

For reimplementation, the contract is:

- **Pure function, no allocation.** `GetReplicaPair3DOnTwistedTorus` takes the `[Y][X][Z]` map by reference and the shape scalars + three loop indices by value; it returns a `pair<long,long>` and never mutates the map. Every leaf access is bounds-checked (`BUG()` on out-of-range).
- **The `num_max_dims == 2` gate is fatal, not recoverable.** Any orientation arm reached with `num_max_dims ∉ {1, 2}` calls `LogMessageFatal` with the verbatim `CHECK` string `"num_max_dims == 2"` (`group_utils.cc` lines 1558 / 1571 / 1584). This is the jellyfish-side enforcement of the same `K`/`2K`-only invariant `UpdateMinMaxDims` enforces upstream.
- **The hop-offset tables are read-only sorted statics.** `kCaseHopsSignToOffsets` is sorted by `(routing_case, hop_len, sign)` so the Mapper binary-searches it; the result `port_offset` is folded `(port_offset + base) mod 8` to the physical port. The diagonal tables are direct-indexed `[row][col]` with element stride 4 bytes.

| | |
|---|---|
| **Fold** | `xla::jellyfish::GetReplicaPair3DOnTwistedTorus` `0x1c893400` (`group_utils.cc`) |
| **Returns** | `pair<long,long>` = `map[cY][cX][cZ]` (the `{core0, core1}` megacore logical-device IDs) |
| **Map source** | `GetPhysicalToLogicalMapping3D` `0x1c88a280` — see [2-Phase Replica-Group Construction](replica-group-2phase.md) |
| **Precondition** | `CHECK("num_max_dims == 2")` — fatal on two-doubled-axis arm with `num_max_dims ≠ 2` (`group_utils.cc:1558/1571/1584`) |
| **Single-axis route table** | `kCaseHopsSignToOffsets` `0xb8f0fb0` (32 × 4 int32), `std::lower_bound` keyed `(routing_case, hop_len, sign)` |
| **Diagonal route tables** | `y_routing` `0xb8f0e70` · `y_routing_0` `0xb8f0f30` · `x_routing` `0xb8f0ef0` · `x_routing_0` `0xb8f0f70` |
| **Hop-ladder snap** | `GetHopLength` `0x1fc59c80` → `{1,2,4,8}` (`viperlite_pod/utils.cc:105`) |
| **Mapper dispatch** | `DmaDestinationRoutingTableEntryMapper::Map` `0x1fc584e0` — `RoutingScheme`: `2 ⇒` two-axes, `1 ⇒` n-hop, `0 ⇒` all-to-all (direct `target_port = dst_chip`), else fatal |
| **Confidence** | HIGH (fold dispatch + `num_max_dims == 2` CHECK decompile-verified; all five tables byte-exact `.rodata` dumps; lower_bound key build + mod-8 fold + diagonal dispatch decompile-verified) unless a row/callout says otherwise |

---

## 1. What the fold is for

The two twisted-torus replica-group builders (`GetPhase0ReplicaGroups`, `GetPhase1ReplicaGroups`) walk a 3-nested loop over `(i, j_or_m, k)` and, at each step, must answer: *which physical chip does the twisted `2K` ring place here, and which logical devices live on it?* That is the entire job of `GetReplicaPair3DOnTwistedTorus`. It is the **only** place the `+K`-mod-`2K` dateline twist enters the replica-group device lists.

The call protocol is fixed across both builders (decompile-verified push order at the `GetReplicaPair3D` call sites `0x137d395d` and `0x137d4231`):

```text
GetReplicaPair3DOnTwistedTorus(
    map,        // a1: const ref to the [Y][X][Z] vector<vector<vector<pair<long,long>>>>
    dims,       // a2: long* = &{Y, X, Z}  (the three physical axis extents)
    2K,         // a3: the long-axis (max) size, the "is-this-axis-the-2K-axis" sentinel
    K,          // a4: the short-axis (min) size, the modulus for the second K-segment
    num_max_dims, // a5: count of axes equal to 2K  (1 ⇒ K_K_2K, 2 ⇒ K_2K_2K) — CHECK'd == 2
    orientation,  // a6: which physical axis is the long axis / shard-phase selector (0,1,…)
    i,          // a7: outer loop index  (X-axis member, "i")
    j_or_m,     // a8: middle loop index (Y-axis member, "j" in Phase0 / "m" in Phase1)
    k)          // a9: inner loop index  (Z-axis member, "k")
  -> pair<long,long>   // {core0_logical_id, core1_logical_id}
```

> **GOTCHA —** the IDA prototype lists nine trailing `long` parameters; the meaningful ones are the four shape scalars `(2K, K, num_max_dims, orientation)` plus the three loop indices `(i, j_or_m, k)`. The `dims` pointer `a2` is dereferenced as `*a2 = Y`, `a2[1] = X`, `a2[2] = Z`; the fold compares each of these against `a3 (= 2K)` to decide *which* physical axis is the long axis. The loop-variable↔axis convention `Y↔j`, `X↔i`, `Z↔k` is the same one used throughout the collective half (confirmed from both builders' call-site argument order).

The function does **not** know about colors, phases, or megacore mode; it is a stateless geometric map. The builders supply the loop indices and consume the returned pair (`first` → core0's group, `second` → core1's group). The map's leaf type is the `{core0, core1}` pair that `GetPhysicalToLogicalMapping3D` deposited — so a single fold call yields *both* megacore cores of one physical chip.

---

## 2. The `num_max_dims == 2` precondition

The fold's outermost dispatch is a three-way branch on `a6` (the orientation / long-axis selector): `a6 == 1`, `a6 != 0` (the remaining nonzero case), and `a6 == 0` (the fall-through). **Within each of those three arms**, when the slice is the two-doubled-axis (`K_2K_2K`) case, the code asserts `a5 (num_max_dims) == 2` before taking the diagonal seam path. If `a5` is neither `1` (single-doubled-axis fast path) nor `2`, the arm fatal-errors:

```text
if ( a5 != 2 )
{
  absl::log_internal::MakeCheckOpString<long,long>(a5, 2, "num_max_dims == 2");
  // group_utils.cc line 1558 / 1571 / 1584 (one per orientation arm)
  LogMessageFatal(..., "platforms/xla/service/jellyfish/lowering/group_utils.cc", line, msg);
  Flush(); ~LogMessageFatal();   // process aborts
}
```

The decompile contains the string `"num_max_dims == 2"` **three times** — one per orientation arm — at source lines `1558`, `1571`, and `1584`. This is the per-call enforcement of the invariant that `TwistedTorusND::UpdateMinMaxDims` (`0x137d0260`) already established with its `max_dim == 2·min_dim` and `num_min_dims + num_max_dims == num_dims` CHECKs (see [Twisted Torus overview §3](overview.md)). The redundancy is deliberate: the fold is a free function in `group_utils.cc` reachable independently of the class, so it re-checks rather than trusting an object field.

| `num_max_dims` (`a5`) | meaning | fold path |
|---|---|---|
| `1` | single doubled axis (`K_K_2K`) | the closed-form single-seam fold (§3); no `CHECK` triggered |
| `2` | two doubled axes (`K_2K_2K`) | the diagonal `(+K, +K)` seam fold (§3); `CHECK` passes |
| any other | not a twisted torus | **fatal** `CHECK("num_max_dims == 2")` |

> **NOTE —** the precondition is on `num_max_dims` (the *count* of `2K` axes), not on the orientation `a6`. The orientation selects *which* physical axis carries the long ring (the `*a2 / a2[1] / a2[2]` comparisons against `2K`); `num_max_dims` selects *how many* axes are doubled. The `K_2K_NK` (`n > 2`) shape never reaches a `num_max_dims > 2` here because the collective half folds it through the same `num_max_dims ∈ {1,2}` machinery — the literal `nK` only matters to the routing-side `TwistedTorusTopology` (see [overview §2 GOTCHA](overview.md)).

---

## 3. The coordinate fold

After the dispatch and (where applicable) the `num_max_dims == 2` check, each arm computes the three physical coordinates `(cY, cX, cZ)` from the loop indices, applying the `+K`-mod-`2K` seam to whichever axis(es) the slice has doubled. The fold then returns `map[cY][cX][cZ]`.

### 3.1 Single doubled axis — `num_max_dims == 1` (`K_K_2K`)

This is the closed form (byte-exact from the `a6 == 0`/`a5 == 1` arm, lines `0x1c8932xx` and the fall-through block `0x1c893560`+). Let `j = a8` be the `2K`-ring member index, `i = a7`, `k = a9`, `K = a4`:

| output coord | value | meaning |
|---|---|---|
| `cY` | `(Y == 2K) ? j : (j mod K)` | if Y is the long axis, the plain `2K` ring; else walk the short K-axis |
| `cX` | `((X == 2K && j ≥ K) ? K : 0) + i` | second K-segment offset `+K` along X if X is long |
| `cZ` | `((Z == 2K && j ≥ K) ? K : 0) + k` | second K-segment offset `+K` along Z if Z is long |

The pattern: exactly one of `{Y, X, Z}` equals `2K` (the long axis). The ring member `j` runs `0..2K-1`. On the long axis, the first `K` steps (`j < K`) land in the lower half and the next `K` steps (`j ≥ K`) jump `+K` into the upper half — the dateline. On the other two (short) axes the coordinate is just the loop index, except that the long axis, when it is Y, threads `j` directly (`cY = j`) and folds `j mod K` onto the short axes' positions. This is exactly the `UpdateNeighborsKTo2K` seam in coordinate space (the doubled axis is two `K`-segments joined at the `+K` step).

```text
ring member j:    0  1  …  K-1 |  K  K+1  …  2K-1
long-axis coord:  0  1  …  K-1 |  K  K+1  …  2K-1   (Y==2K: plain 2K ring)
                  ───── lower ──┼──────── upper ────
short-axis fold:  j mod K runs 0..K-1 then repeats; +K applied to the long axis only
```

**Worked example** (`K = 4`, `2K = 8`, shape `K, K, 2K` with `Z == 2K`, so `Y = X = 4`, `Z = 8`; fix `i = 1`, `k = 2`). The `2K` ring walks `j = 0..7`:

| `j` | `cY = j mod K` | `cX = i = 1` | `cZ = ((Z==2K && j≥K)?K:0) + k` | landed chip `(Y,X,Z)` |
|---|---|---|---|---|
| 0 | 0 | 1 | `0 + 2 = 2` | `(0,1,2)` |
| 1 | 1 | 1 | `0 + 2 = 2` | `(1,1,2)` |
| 2 | 2 | 1 | `0 + 2 = 2` | `(2,1,2)` |
| 3 | 3 | 1 | `0 + 2 = 2` | `(3,1,2)` |
| 4 | 0 | 1 | `4 + 2 = 6` | `(0,1,6)` |
| 5 | 1 | 1 | `4 + 2 = 6` | `(1,1,6)` |
| 6 | 2 | 1 | `4 + 2 = 6` | `(2,1,6)` |
| 7 | 3 | 1 | `4 + 2 = 6` | `(3,1,6)` |

The ring walks the short `Y`-axis once at `Z = 2` (`j = 0..3`), then **jumps `+K = +4` along the long `Z`-axis** to `Z = 6` and walks `Y` again (`j = 4..7`). The two `K`-segments occupy the two halves of the doubled `Z`-axis; the seam (the `+K` jump at `j = K`) is the dateline. Each landed chip's `map[cY][cX][cZ]` pair contributes its `{core0, core1}` to the Phase0 group keyed `k·R + i`.

### 3.2 Two doubled axes — `num_max_dims == 2` (`K_2K_2K`)

Here both `2K` axes carry a `K`-segment, and the seam is the explicit `(coord + K) mod 2K` fold applied to the axis being threaded. The decompile computes, per orientation, a modular reduction of the form `(a4 + index) % a3` (i.e. `(K + member) mod 2K`) on the doubled axes, e.g. the `a6 != 0`/`a5 == 2` arm:

```text
// when both a2[1] (X) and a2[2] (Z) equal 2K (the dominant K_2K_2K orientation):
cX = ((X == 2K) ? base : … ) + (member folded (+K) mod 2K)
cZ = ((Z == 2K) ? base : … ) + (member folded (+K) mod 2K)
// modular reductions:  (a4 + a7) % a3   and   (a4 + a8) % a3   appear verbatim
```

The genuine two-axis twist is the diagonal `(+K, +K)`: each of the two `2K` axes carries one `K`-segment and the member walk folds *both* doubled axes via the same `(coord + K) mod 2K` seam, balancing the doubled-axis ICI bandwidth across both physical axes. The per-orientation closed form (which physical axis is the short `K`-axis in each of the three `a6` arms) was traced to its seam effect but not reduced to a single per-orientation formula — see §5.

### 3.3 The table return — bounds-checked

All arms converge on the same leaf access (`0x1c8938c5`+):

```text
// v12 = cY, v14 = cX, v10 = cZ  (the three folded coords)
if ( cY >= map.size() )                      BUG();   // outer (Y) bound
row    = map[cY];                                       // vector<vector<pair>>
if ( cX >= row.size() )                      BUG();   // middle (X) bound
inner  = row[cX];                                       // vector<pair<long,long>>
if ( cZ >= inner.size() )                    BUG();   // inner (Z) bound
return inner[cZ];                                       // pair{core0, core1}
```

The `24 * v12` / `24 * v14` strides are the 24-byte `std::vector` control block; the `16 * v10` stride is `sizeof(pair<long,long>)`. The returned `pair.first` is `core0`'s logical device ID, `pair.second` is `core1`'s — the megacore pair the builders split across groups.

> **GOTCHA —** the outer dispatch and the value-CHECK scalar are **two distinct arguments**, not one. The dispatch is on `a6` (the orientation / long-axis selector); the `CHECK("num_max_dims == 2")` is on `a5` (`num_max_dims`, the count of `2K` axes). The `K_K_2K` (`a5 == 1`) fast path never triggers the CHECK, and only the two-doubled-axis arms assert `a5 == 2`. Confidence: HIGH (three CHECK-string occurrences + the `a5 != 2` branch).

---

## 4. The n-hop Mapper hop-offset tables

Once the collective knows *who* it reduces with (the replica groups above), the route generator must decide *which physical ICI link* carries each `(src_chip, dst_chip)` transfer. On a limited-ICI (n-hop) topology that decision is a pure table lookup in `DmaDestinationRoutingTableEntryMapper`, dispatched by `Map` (`0x1fc584e0`) on the `RoutingScheme` argument (`a5`, decompile-verified at `Map`'s tail): `== 2 ⇒` `MapTwoAxesReachable` (diagonal), `== 1 ⇒` `MapOneTwoFourEightHopNeighborsReachable` (single-axis n-hop), `== 0 ⇒` all-to-all (the route's `target_port` is set directly to the destination chip ID, no table), anything else ⇒ fatal `"Unsupported routing scheme: %d"`. All five tables below are byte-exact `.rodata` dumps.

### 4.1 The single-axis table — `kCaseHopsSignToOffsets` (`0xb8f0fb0`)

`MapOneTwoFourEightHopNeighborsReachable` (`0x1fc588a0`) handles the case where source and destination differ on **one** axis. It builds a 3-tuple search key and `std::lower_bound`-searches the 32-entry sorted table (entry stride 16 bytes = 4 int32 `{routing_case, hop_len, sign, port_offset}`). The RetCheck string is verbatim: `"kCaseHopsSignToOffsets.contains( {routing_case, hop_len, hops > 0 ? POSITIVE : NEGATIVE})"`.

The key is built (decompile-verified `0x1fc58b00`+):

```text
routing_case = (coord_parity & 1) + (near ? 3 : 1)   // group 1..4
   // near = (the differing-axis hop count <= 4); coord_parity = source coord & 1
   // same-axis branch (v78 == v80): +3 if hop<=4 else +1; else (cross) +1
hop_len = GetHopLength(|delta|) in {1,2,4,8}          // 0x1fc59c80
sign    = (delta <= 0) + 1                            // 1 = POSITIVE (delta > 0), 2 = NEGATIVE (delta <= 0)
```

The matched entry's `port_offset` (4th int) is folded to the physical port (`0x1fc58c8e`+):

```text
base        = dma_axis_term + axis_factor * stride   // v79 + v16 * v17 in the decompile
target_port = (port_offset + base) mod 8             // (x + 7) & ~7 trick for the floor
```

`kCaseHopsSignToOffsets` (32 × 4 int32, byte-exact; `routing_case ∈ 1..4`, `hop_len ∈ {1,2,4,8}`, `sign ∈ {1,2}`, `port_offset ∈ 0..7`):

```text
idx  case hop sign -> off        idx  case hop sign -> off
[ 0]  1   1   1  ->  1           [16]  3   1   1  ->  4
[ 1]  1   1   2  ->  2           [17]  3   1   2  ->  2
[ 2]  1   2   1  ->  5           [18]  3   2   1  ->  5
[ 3]  1   2   2  ->  6           [19]  3   2   2  ->  1
[ 4]  1   4   1  ->  7           [20]  3   4   1  ->  7
[ 5]  1   4   2  ->  4           [21]  3   4   2  ->  6
[ 6]  1   8   1  ->  3           [22]  3   8   1  ->  3
[ 7]  1   8   2  ->  3           [23]  3   8   2  ->  3
[ 8]  2   1   1  ->  7           [24]  4   1   1  ->  7
[ 9]  2   1   2  ->  1           [25]  4   1   2  ->  5
[10]  2   2   1  ->  3           [26]  4   2   1  ->  4
[11]  2   2   2  ->  4           [27]  4   2   2  ->  0
[12]  2   4   1  ->  5           [28]  4   4   1  ->  1
[13]  2   4   2  ->  2           [29]  4   4   2  ->  6
[14]  2   8   1  ->  6           [30]  4   8   1  ->  2
[15]  2   8   2  ->  6           [31]  4   8   2  ->  2
```

> **NOTE —** the four `routing_case` groups encode the `(axis-class, coord-parity)` of the differing axis; the `{1,2,4,8}` ladder is the reachability the n-hop generator emits; `sign` is the SerDes direction (`+`/`-`). The geometric meaning of "near vs far" (the `+3` vs `+1` offset) was traced to the `hop_len <= 4` branch but not pinned to a named physical-axis identity — see §5.

### 4.2 The diagonal tables — `MapTwoAxesReachable` (`0x1fc58fa0`)

When source and destination differ on **both** axes simultaneously, the Mapper reads one of four direct-indexed tables. The dispatch is on the topology X-dimension `v53` (a topology getter; `cmp $0x4` / `cmp $0x8` on it at `0x1fc59102`/`0x1fc59107`) and on which axis the transfer runs along (`src.y == dst.y` ⇒ Y-axis table; `src.x == dst.x` ⇒ X-axis table; both equal ⇒ the same-src/dst error):

| X-dim (`v53`) | Y-axis transfer (`y == dst_y`) | X-axis transfer (`x == dst_x`) | row index | col index |
|---|---|---|---|---|
| `8` | `y_routing` (4×8) `0xb8f0e70` | `x_routing` (16 int32) `0xb8f0ef0` | Y: `src_y / 2` · X: `src_x mod 2` | other-axis hop |
| `4` | `y_routing_0` (2×8) `0xb8f0f30` | `x_routing_0` (4×4) `0xb8f0f70` | Y: `src_y / 2` · X: `src_x mod 4` | other-axis hop |

The leaf access (decompile-verified `0x1fc58fa0`+) is byte-offset `4*col + row_stride*row` into the chosen table:

- **`y_routing` / `y_routing_0`** (both 8 int32 per row): offset `4*col + 32*(src_y / 2)`.
- **`x_routing_0`** (`v53 == 4`, 4 int32 per row): offset `4*col + 16*(src_x mod 4)` — a genuine 4×4.
- **`x_routing`** (`v53 == 8`): offset `4*col + 32*(src_x mod 2)` with `col < 8` — the 16 int32 are indexed as a 2×8 block, **not** as the 4×4 the `.rodata` dump below prints. The dump rows below show the raw 16 int32 in storage order; the `v53 == 8` X-axis path walks them in 2×8 stride.

Invalid X-dim, or a same-axis transfer, errors: `"Two axes routing only supports slices of dimension X equal to 4 or 8"`, `"Two axes routing only supports transfers along X or Y axes"`, `"Mapper should not be called with same src/dst"` (all decompile-verified).

`y_routing` (4×8, `0xb8f0e70`):

```text
row0:  0  8  2 10  4 12  6 14
row1:  2 10  4 12  6 14  0  8
row2:  4 12  6 14  0  8  2 10
row3:  6 14  0  8  2 10  4 12
```

`y_routing_0` (2×8, `0xb8f0f30`):

```text
row0:  0  4  8 12  2  6 10 14
row1:  2  6 10 14  0  4  8 12
```

`x_routing` (4×4, `0xb8f0ef0`):

```text
row0:  9  1 11  3
row1: 13  5 15  7
row2:  1  9  3 11
row3:  5 13  7 15
```

`x_routing_0` (4×4, `0xb8f0f70`):

```text
row0:  5  1  7  3
row1:  1  5  3  7
row2: 13  9 15 11
row3:  9 13 11 15
```

The values `0..15` are the 4-bit physical ICI destination-port encoding, stored directly into `route_entry.target_port`. The `_0` variants are the smaller-pod (X-dim 4) topology versions. Which of the two variants applies per pod-size beyond the `X-dim == 4 vs 8` dispatch is open — see §5.

### 4.3 The hop-ladder snap — `GetHopLength` (`0x1fc59c80`)

`GetHopLength` is a `switch` on the **signed** delta that snaps `±1/±2/±4/±8` to the unsigned hop length `{1,2,4,8}` and sets a `StatusOr` success flag; any other value returns `MakeErrorImpl<3>("Invalid hops: %d")` at `viperlite_pod/utils.cc:105`:

| input delta | hop_len | input delta | hop_len |
|---|---|---|---|
| `±8` | `8` | `±2` | `2` |
| `±4` | `4` | `±1` | `1` |
| any other | error (`"Invalid hops: %d"`) | | |

So a coordinate delta on the n-hop ladder is always one of the four power-of-two reachability steps; the `sign` (the SerDes direction) is recovered separately by the caller (`delta <= 0`).

---

## 5. What was not resolved

- **The `K_2K_2K` per-orientation closed form.** §3.2 byte-confirmed the `(+K) mod 2K` seam and the dominant (X = Z = 2K) orientation, but the full per-orientation case table (which physical axis is the short `K`-axis in each of the three `a6` arms) was traced to its seam effect, not reduced to one closed formula. MEDIUM.
- **The `routing_case` "near vs far" axis-class.** §4.1 confirmed `routing_case = (parity & 1) + (near ? 3 : 1)` and that groups `{1,2}`/`{3,4}` are the near/far halves, but the exact physical meaning of "near vs far" (which axis-class / wrap-direction picks `+3` vs `+1`) was traced to the `hop_len <= 4` branch, not tied to a named axis. MEDIUM.
- **The `y_routing` vs `y_routing_0` pod-size selection.** Beyond the `X-dim == 8 vs 4` dispatch, the per-pod choice of variant was inferred from the dispatch immediates, not tied to a named `TpuChipConfig` pod-size field. MEDIUM.
- **The `RoutingScheme` enum value set.** The dispatch selector is byte-proven for `0` (all-to-all), `1` (n-hop), `2` (two-axes), with any other value hitting `"Unsupported routing scheme: %d"`; the symbolic enumerator *names* (which spelling maps to `0`/`1`/`2`) were not decoded. LOW.
- **The `≥2-phase` fold arm.** The `a6 == 1` orientation arm is a structurally distinct seam reached only if a future `>1`-phase twisted sharding is enabled; `GetPerColorShardIdTable`'s `1`-phase gate makes it unreachable in v0.0.40. Its collective semantic is unexercised. LOW. See [2-Phase Replica-Group Construction](replica-group-2phase.md).

---

## 6. Function & Table Map

| Symbol | Address | Role |
|---|---|---|
| `GetReplicaPair3DOnTwistedTorus` | `0x1c893400` | coord fold; returns `map[cY][cX][cZ]` pair |
| `CHECK("num_max_dims == 2")` | `group_utils.cc:1558/1571/1584` | per-orientation fatal precondition |
| `GetPhysicalToLogicalMapping3D` | `0x1c88a280` | builds the `[Y][X][Z] → {core0,core1}` map |
| `DmaDestinationRoutingTableEntryMapper::Map` | `0x1fc584e0` | `RoutingScheme` dispatch (2 ⇒ two-axes, 1 ⇒ n-hop, 0 ⇒ all-to-all, else fatal) |
| `MapOneTwoFourEightHopNeighborsReachable` | `0x1fc588a0` | single-axis n-hop lookup + mod-8 port fold |
| `MapTwoAxesReachable` | `0x1fc58fa0` | diagonal two-axes table lookup |
| `GetHopLength` | `0x1fc59c80` | `±{1,2,4,8} → {1,2,4,8}` snap (`utils.cc:105`) |
| `kCaseHopsSignToOffsets` | `0xb8f0fb0` | 32 × 4 int32 single-axis `(case,hop,sign)→offset` |
| `y_routing` | `0xb8f0e70` | 4 × 8 int32 diagonal port table (X-dim 8) |
| `y_routing_0` | `0xb8f0f30` | 2 × 8 int32 diagonal port table (X-dim 4) |
| `x_routing` | `0xb8f0ef0` | 4 × 4 int32 diagonal port table (X-dim 8) |
| `x_routing_0` | `0xb8f0f70` | 4 × 4 int32 diagonal port table (X-dim 4) |

---

## Cross-References

**Twist algorithms (this section)**
- [Twisted Torus — Section Map](overview.md) — the subsystem map; the `K`/`2K` shape gate and class family
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — the Phase0/Phase1 3-nested loops that *call* this fold; `GetPhysicalToLogicalMapping3D`
- [Twist Predicate & Orientation](twist-predicate-orientation.md) — the orientation enum the fold dispatches on; the `max == 2·min` predicate
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the per-color `UpdateNeighborsKTo2K` seam this fold mirrors in coordinate space
- [Get Tiebreak](get-tiebreak.md) — the routing-side equal-distance tiebreak rule on a twisted slice

**Sibling sections**
- [Get Static Path](../routing/get-static-path.md) — the routing-side path construction that consumes the per-link route table
- [Get Distances](../routing/get-distances.md) — the twist-adjusted torus-reduced distance vector
- [back to index](../index.md)
