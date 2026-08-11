# TENSOR4D / MEM_PATTERN4D — the Spill Descriptor

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; build-id `92b4d331` for `libwalrus.so`). The 4D encoder, the destination/source validator family, and the producer call sites all live in `neuronxcc/starfish/lib/libwalrus.so`; the `bir::AccessPattern` source object lives in `libBIR.so` (build-id `a9b1ea38`). For `.text`/`.rodata` the virtual address equals the file offset (`.text` base `0x62d660`, `.rodata` base `0x1c72000`). `libwalrus.so` retains its dynamic symbol table (`nm -DC`), so every symbol below is name-confirmed. Other wheels differ — treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

`TENSOR4D` is the **largest** member of the static-pattern descriptor family ([2.3](tensor-descriptors.md)) — the 20-byte slot that encodes an access pattern with **four** free dimensions. It exists for the same reason a fourth `{stride, count}` pair exists: a 16-byte `TENSOR3D` can describe only three free axes, and some operands' on-chip walk needs four. When an op-encoder reaches an operand whose access pattern cannot be expressed in three free dims, it packs the operand as a `TENSOR4D` (or its destination spelling `MEM_PATTERN4D`) instead of a `TENSOR3D`. This page recovers the 20-byte layout, the single byte-level difference from the 16-byte 3D case, the decision that picks 4D, and a census of *which* instructions actually emit it.

The 20-byte layout is the [4+4N rule](tensor-descriptors.md#the-44n-size-rule) at `N = 4`: `4 + 4·4 = 20`. It is byte-identical to `TENSOR3D` with **one extra `{stride, count}` pair** appended — and, because strides and counts live in two **separate** contiguous arrays, appending the fourth dimension does three things at once: it adds `step3` at the end of the stride array (`+0xA`), shifts the whole count array down by two bytes (the count base moves from `+0xA` in 3D to `+0xC` in 4D), then adds `num3` at `+0x12`. The "extra 4 bytes" are exactly `{step3 @+0xA, num3 @+0x12}`. There is no second descriptor, no continuation flag — the spill is the same array grown by one entry, which is why the slot is 20 bytes, not 16.

The one thing the naive expectation gets wrong: **`TENSOR4D` is the general maximal data-tensor descriptor, not a batch-norm / transpose-reduce special case.** Cross-referencing the two `assignAccess<TENSOR4D>` thunks against the symbol table resolves **22 distinct op-encoders** (18 CoreV2 + 4 CoreV3) that emit 4D — copy, shuffle, transpose, pool, reduce, max, gather, iota, memset, reciprocal, RNG-state, the BN-stats reduced inputs, and the gen3 sparse-matmul ifmap — plus a string-keyed generic selector that routes any name-tagged 4D operand. 4D is the **fallback** the encoder spills to when 3D cannot express the walk; BN/reduce/sparse are simply the operands that *most often* need the fourth axis, not the only ones.

For reimplementation, the contract is:

- The 20-byte byte map (`ADDR4 @+0`; `step0..3 @+4/+6/+8/+0xA`; `num0..3 @+0xC/+0xE/+0x10/+0x12`) — two separate `u16` arrays, the stride array first.
- The exact 16→20 spill delta: one appended `{step3 @+0xA, num3 @+0x12}` pair, with the count-array base relocating `+0xA → +0xC`.
- The spill *decision*: the descriptor type is a compile-time template choice per operand role; within the chosen 4D slot, the runtime active-dim count range-checks `active ≤ N+1 = 5`.
- The decoder family that consumes the 20 bytes — `mem4d_valid` (DST role, ×3 arches), `tensor4d_valid` (SRC role), `check_m4d_active_channels`, and the v4 PSUM / indirect gates — and the encoder↔decoder byte agreement.

| | |
|---|---|
| **Size** | **20 bytes** (`4 + 4N`, `N = 4`) — `.rodata` assert `"ISA mem pattern 4D must have 20 bytes to encode"` @ `0x1d6e990` |
| **Layout** | `[ADDR4 u32 @+0][step0..3 i16 @+4/+6/+8/+0xA][num0..3 u16 @+0xC/+0xE/+0x10/+0x12]` |
| **Spill delta vs 3D** | one extra pair `{step3 @+0xA, num3 @+0x12}`; count base `+0xA → +0xC` |
| **Dispatchers** | `assignAccess<core_v2::TENSOR4D>` @ `0x133e710` (size-20 guard); `<core_v3::TENSOR4D>` @ `0x1427170` |
| **Packer** | `assignStaticPattern<core_v2::TENSOR4D>` @ `0x1176df0`; `<core_v4::TENSOR4D>` @ `0x150cf60` (byte-identical math) |
| **Field writers** | step → `sub_116ca30` (i16, sign-checked); num → `sub_116d620` (u16, zero rejected) |
| **DST validators** | `core_v2::mem4d_valid` @ `0x127f660`; `core_v3::` @ `0x136ee40`; `core_v4::` @ `0x1446550` |
| **SRC validators** | `core_v3::tensor4d_valid` @ `0x136f600`; `core_v4::` @ `0x14477c0` |
| **Active-chan check** | `check_m4d_active_channels(MEM_PATTERN4D, u8)` — v2 `0x1280010` / v3 `0x1370570` / v4 `0x14494e0` |
| **Dim range check** | `cmp $0x5,%r13d ; setbe` @ `0x1177162` → `"[1, 4]D"` `.rodata` @ `0x1d53378` |
| **Producer census** | 18 CoreV2 + 4 CoreV3 op-encoders; 65 resolved thunk call sites (57 v2 + 8 v3) |

---

## The 20-byte layout

### Purpose

`TENSOR4D` is the descriptor an op-encoder hands the hardware address generator when an operand's on-chip walk spans four free dimensions. Like every member of the family it is a fixed-size POD slot embedded in the encoded instruction; the size is not a tunable, it is fixed by the descriptor *type*. The 4D slot is the largest the family defines — there is no `TENSOR5D`.

### The size assert

The size is pinned by the same `.rodata` mechanism as the smaller descriptors. The dispatcher (`assignAccess<TENSOR4D>`) checks that the slot it was handed is exactly `sizeof(NEURON_ISA_TPB_TENSOR4D)` = 20 bytes, and on mismatch raises the 4D string:

| Descriptor | `N` | Size | Assert string | `.rodata` | Referenced from | Confidence |
|---|---|---|---|---|---|---|
| `TENSOR4D` | 4 | **20 B** | `"ISA mem pattern 4D must have 20 bytes to encode"` | `0x1d6e990` | `0x133e9b1` (v2 dispatcher), `0x14273b6` (v3 dispatcher) | CERTAIN |
| `TENSOR3D` | 3 | 16 B | `"ISA mem pattern 3D must have 16 bytes to encode"` | `0x1d6e920` | 3D dispatchers (v2/v3/v4) | CERTAIN |

`20 = 4 + 4·4`. The 4D assert string is referenced from **exactly** the two 4D dispatchers — the `lea` that loads its address lives inside `assignAccess<core_v2::TENSOR4D>` at `0x133e9b1` and `assignAccess<core_v3::TENSOR4D>` at `0x14273b6` — so the binary's own size statement is unambiguous about which slot is 20 bytes.

> **NOTE —** there is no `assignAccess<core_v4::MEM_PATTERN4D>` symbol. The v4 destination dispatcher family stops at `MEM_PATTERN2D`/`MEM_PATTERN3D`; the v4 4D destination path reuses the `TENSOR4D` struct (packed by `assignStaticPattern<core_v4::TENSOR4D>` @ `0x150cf60`) and is validated by `core_v4::mem4d_valid`. So at v4 there is no separate `MEM_PATTERN4D` *encoder* — the 4D DST role rides the `TENSOR4D` wire struct. The symbol's absence is read directly from `nm -DC`, which `libwalrus.so` retains in full.

### The byte map

The 20 bytes are `[ADDR4][step0..step3][num0..num3]` — the stride array first, the count array second, **not** interleaved (the universal family invariant, [2.3](tensor-descriptors.md#the-byte-layout--two-separate-arrays)):

| Offset | Size | Field | Witness | Confidence |
|---|---|---|---|---|
| `+0x00` | 4 | `ADDR4` start-address word — partition dim folded in; byte-addr bits `0..28`, region/PSUM bits `25..28` (`0x1E000000`), bit-31 register-mode | `assignStartAddr<ADDR4>` head of packer @ `0x1176ff8`; decoder reads `+0` → `tensor_start_addr_valid` | CERTAIN |
| `+0x04` | 2 | `step0` (i16) — free-dim 0 stride | enc `lea [2·0+4]`; dec reads `+4` | CERTAIN |
| `+0x06` | 2 | `step1` (i16) — free-dim 1 stride | enc `lea [2·1+4]` | CERTAIN |
| `+0x08` | 2 | `step2` (i16) — free-dim 2 stride | enc `lea [2·2+4]`; dec `cmpw $0,+8` (PSUM path) | CERTAIN |
| `+0x0A` | 2 | `step3` (i16) — free-dim 3 stride — **the spill** | enc `lea [2·3+4]`; default-fill `+0xA` @ `0x1177397` | CERTAIN |
| `+0x0C` | 2 | `num0` (u16) — free-dim 0 count | enc `lea [2·0+0xC]`; dec `cmpw $0,+0xC` | CERTAIN |
| `+0x0E` | 2 | `num1` (u16) — free-dim 1 count | enc `lea [2·1+0xC]`; dec `cmpw $0,+0xE` | CERTAIN |
| `+0x10` | 2 | `num2` (u16) — free-dim 2 count | enc `lea [2·2+0xC]`; dec `cmpw $0,+0x10` | CERTAIN |
| `+0x12` | 2 | `num3` (u16) — free-dim 3 count — **the spill** | enc `lea [2·3+0xC]`; default-fill `+0x12` @ `0x117739b`; dec `cmpw $0,+0x12` | CERTAIN |

> **NOTE — the ADDR4 region/PSUM discriminator is bits `25..28`, and it is arch-universal.** The mask is `0x1E000000`, read off the decoder's `and ebx,1E000000h` and the PSUM-window base `0x2000000 = 1<<25` inside `tensor_start_addr_valid` ([2.2 ADDR4 §PSUM-window detection](addr4.md#psum-window-detection--region-class--bits-2528)). It is not a v3/v4 addition, and the discriminator is not at bits `21-22`. The `tensor4d_valid` source check below tests the same `25..28` quadrant bits (`& 0x1e000000`).

The encoder witnesses are byte-exact off the packer body (`assignStaticPattern<core_v2::TENSOR4D>` @ `0x1176df0`): the stride store computes its target as `desc + 2i + 4` (`lea r13,[rax+rax+4]` @ `0x11772b9`), the count store as `desc + (2i+4) + 8 = desc + 2i + 0xC` (`lea rdi,[rax+r13+8]` @ `0x11772f9`). The `step`/`num` word widths and signedness are unchanged from the smaller descriptors: `step` is a signed `i16` through `sub_116ca30` (sign-match overflow guard), `num` an unsigned `u16` through `sub_116d620` (zero-rejecting guard) — see [2.3 stride/count words](tensor-descriptors.md#the-stride-word--signed-i16-element-units).

---

## The 16→20 spill — the one extra pair

### Purpose

The whole reason `TENSOR4D` exists as a distinct 20-byte struct rather than a 16-byte one is the fourth free dimension. A reimplementer needs to know precisely what the four extra bytes are and where they land — because the count array does **not** simply grow at its old tail; its whole base relocates.

### The delta

```text
TENSOR3D (16 B):  [ addr32 ][ step0 step1 step2        ][ num0 num1 num2        ]
                    +0..+3    +4    +6    +8              +0xA  +0xC  +0xE
                                       (3 strides, base +4) (3 counts, base +0xA)

TENSOR4D (20 B):  [ addr32 ][ step0 step1 step2 step3  ][ num0 num1 num2 num3  ]
                    +0..+3    +4    +6    +8   +0xA       +0xC  +0xE  +0x10 +0x12
                                       (4 strides, base +4) (4 counts, base +0xC)
```

Because strides and counts are two **separate** arrays, adding the fourth dimension does three things, not one:

1. **appends `step3 @+0xA`** — the stride array grows from 3-wide (`+4..+9`) to 4-wide (`+4..+0xB`);
2. **shifts the entire count-array base down by 2 bytes** — `+0xA` (3D) → `+0xC` (4D) — because the count array starts immediately after the now-larger stride array (`base = 4 + 2N`, so `+0xA` at `N=3` becomes `+0xC` at `N=4`);
3. **appends `num3 @+0x12`** — the count array grows to 4-wide.

The "extra 4 bytes" are exactly `{step3 @+0xA, num3 @+0x12}`. The spill is **not** a second descriptor or a continuation record — it is the same `{step, num}` array geometry grown by one entry. This is why the wire struct is 20 bytes and not 16.

> **GOTCHA — the count base moves, it does not just extend.** A reimplementer who treats 4D as "3D plus a trailing `{step3, num3}` at the end of the 16-byte struct" writes `num0` at the 3D offset `+0xA` — but in a 4D slot `+0xA` is `step3`, and `num0` is at `+0xC`. The count array's *base* relocates by 2 bytes when the stride array gains an element. The pack offsets must be computed as `step @ +4+2i` and `num @ +(4+2N)+2i` with `N=4`, never by patching the 3D layout.

### Decoder proof of the boundary

The 16-vs-20 boundary is independently confirmed by the validators' count-nonzero sweep: each `num` word must be nonzero, and the number of words swept is exactly the descriptor's dimensionality.

| Validator | Address | `num` words checked | Count base | Confidence |
|---|---|---|---|---|
| `core_v4::mem3d_valid` | `0x14467d0` | `+0xA`, `+0xC`, `+0xE` (3 entries) | `+0xA` | CERTAIN |
| `core_v4::mem4d_valid` | `0x1446550` | `+0xC`, `+0xE`, `+0x10`, `+0x12` (4 entries) | `+0xC` | CERTAIN |

The 4D decoder validates exactly one more `num` word than the 3D decoder, **and** at a `+2`-shifted base — matching the encoder's count-array relocation byte-for-byte. The decoder validates precisely the bytes the encoder spills.

---

## The spill decision — when the encoder picks 4D

### Purpose

A reimplementer must reproduce not just the layout but the *choice*: given an operand, when does the encoder emit a 20-byte `TENSOR4D` rather than a 16-byte `TENSOR3D`? There are two layers — a compile-time template choice per operand role, and a runtime range check inside the chosen slot.

### The compile-time choice

The descriptor type is **not** computed from the access pattern's dimensionality at runtime. It is a compile-time template choice baked into each op-encoder's `visit*` method by operand **role**: a method calls `assignAccess<TENSOR3D>` for one operand and `assignAccess<TENSOR4D>` for another. For example, the matmul moving/PSUM operands are `TENSOR3D` ([2.3](tensor-descriptors.md#arch-invariance)), while a tensor-copy data tensor is `TENSOR4D`. The encoder "spills" to 4D in the sense that its author selected the 4D template for any operand whose walk might need up to four free dims; 3D is chosen where three always suffice. The template choice fixes the slot size; the access pattern then fills it.

### The runtime range check

Within the chosen 4D slot, the packer computes the number of *active* dimensions and range-checks that the pattern fits the descriptor's capacity. There is **no** explicit dim-count byte on the wire — the check is validity-only and writes nothing to the descriptor:

```c
// inside assignStaticPattern<core_v2::TENSOR4D> @0x1176df0
// active = partition dim (folded into ADDR4) + each non-unit free dim
active = 1 + count(d in 1 .. num_dims-1 : ap.Pattern[d].num != 1);
if (active > 5) {                       // cmp $0x5,%r13d ; setbe  @0x1177162
    raise("Illegal ISA memory access dimension: <k>, which must be in "
          "range of [1, 4]D tensor for ISA mem pattern");   // .rodata @0x1d53378
}
// the bound is N+1 = 5: four free (step,num) slots + the partition dim in ADDR4.
// contrast: the 3D packer raises "[1, 3]D" and checks cmp $0x4 (= 3+1).
```

The `5` is `N + 1`: the four `{step, num}` slots plus the partition dimension folded into the [ADDR4](addr4.md) word. The error string's `"[1, 4]D"` (vs the 3D packer's `"[1, 3]D"`) is the binary's own statement that this descriptor's capacity is four free dims.

The *filled* dim count is implicit in which `num` slots are not `1`: unused dims are default-filled with `{step = 1, num = 1}` (the no-advance / single-element identity, [2.3 unit-fill](tensor-descriptors.md#the-1-1-unit-fill-for-spare-dimensions)). The packer writes the `{1,1}` defaults at the 4D spill offsets directly — `mov WORD[rsi+0xa],1` (`step3 = 1`) @ `0x1177397` and `mov WORD[rsi+0x12],1` (`num3 = 1`) @ `0x117739b`. This is why the decoder's "`num != 0`" sweep accepts a unit-filled `1` but rejects a `0`.

> **NOTE —** the active *channel*/partition count is **not** a descriptor field. It is a separate `u8` argument passed to `check_m4d_active_channels(MEM_PATTERN4D, u8)` and validated against the ADDR4 word's partition band, not against any `{step, num}` slot. The 20-byte struct carries the walk geometry; the channel span is a caller-supplied count checked alongside it.

---

## How the four dims map to the slots

### Purpose

A reimplementer must know *which* logical axis lands in *which* slot — and, critically, that the partition axis is not a slot at all.

### The convention

The canonical AP axis order is `[W, Z, Y, X] = Pattern[0..3]`, with `Pattern[0] = W` the **outermost** dimension and the SBUF/PSUM partition axis ([2.3 partition folding](tensor-descriptors.md#partition-axis-folding)). The 4D packer:

- folds `Pattern[0]` (the partition axis) into the single 32-bit ADDR4 word `@+0` — it is **never** a `{step, num}` slot;
- iterates the free dims `Pattern[num_dims-1 .. 1]` (innermost first), **drops** degenerate (`num == 1`) axes, and **compacts** the survivors into slots `0,1,2,3` from the bottom;
- the fourth slot `{step3 @+0xA, num3 @+0x12}` holds the extra (fourth) surviving free dim. It does **not** relabel the partition axis or reorder the first three — it is the spill the 16-byte window could not hold.

So a `TENSOR4D` pattern spans up to **five** logical AP dims: `Pattern[0] → ADDR4`, four surviving free dims → the four `{step, num}` slots. The `AxisListType = XYZW` selector (value 3, "reduce all four free dims") is the reduce-side companion of this descriptor; the trailing-channel variants (`XYZWC` / `C`) are the five-axis collapse-all forms that ride a 4D descriptor with the partition axis as the reduction target. The axis mapping after `num==1` compaction is reconstructed from the `[W,Z,Y,X]` order plus the compaction loop; the per-op axis assignment itself is owned by the upstream AP lowering, not by the wire layout.

---

## The decoder family — consuming the 20 bytes

### Purpose

The 20-byte struct is read by a small family of hardware-model validators, split by operand role (destination vs source) and by arch generation. They confirm the layout independently: every offset the packer writes is an offset a validator reads. The struct is passed **by value** (a 20-byte POD spilled to a stack frame), so the frame mirrors the descriptor 1:1.

### `mem4d_valid` — the destination check (v2 `0x127f660` / v3 `0x136ee40` / v4 `0x1446550`)

```c
// core_v3::mem4d_valid(MEM_PATTERN4D /*by value*/, DTYPE, WRITE_TENSOR,
//                      ALLOWED_IN_PSUM, ALLOWED_IN_SBUF) -> bool   @0x136ee40
// struct base spilled at a frame offset (0x30(%rsp) in mem4d); fields mirror the wire struct.
bool mem4d_valid(desc, dtype, write, psum_ok, sbuf_ok) {
    uint8_t byte3  = addr32 >> 24;
    int     region = byte3 & 0x60;
    if (region == 0x20) {                                  // PSUM / 2nd-window region marker
        // two-window form: data window @+0, second window @+4
        if (!tensor_start_addr_valid(addr @+0, /*dtype=*/9)) return false;  // 1st window
        if (byte_at(+7) & 0xe0)                  ;          // testb $0xe0 : 2nd-window flags
        return tensor_start_addr_valid(word @+4, dtype);   // 2nd window
    } else {                                               // ordinary single ADDR4
        if (!tensor_start_addr_valid(addr @+0, dtype)) return false;
        if (step2(+8) == 0)                       return false;   // inner step nonzero
        return num0(+0xC) != 0 && num1(+0xE) != 0
            && num2(+0x10) != 0 && num3(+0x12) != 0;        // all four counts nonzero
    }
}
```

`mem4d_valid` is the wire-level consumer of exactly the bytes `assignStaticPattern<TENSOR4D>` wrote: the ADDR4 in a legal SB/PSUM window, the region marker, and every `num` word nonzero. The region byte `+3` `& 0x60 == 0x20` test reads the **same** `0x20` the encoder stamps via `or desc[+3],0x20` — encoder and decoder agree on the region bit. The `num`-sweep offsets `+0xC/+0xE/+0x10/+0x12` are byte-exact across all three arch copies.

There is no single 4D decoder. `mem4d_valid` exists in **three** arch copies — `core_v2::mem4d_valid @0x127f660`, `core_v3::mem4d_valid @0x136ee40`, and `core_v4::mem4d_valid @0x1446550` — alongside a separate source-role `tensor4d_valid` family and the `check_m4d_active_channels` channel checker. The 20-byte geometry is identical across all three copies; they differ only in the region/quadrant encoding.

### `tensor4d_valid` — the source check (v3 `0x136f600` / v4 `0x14477c0`)

The read-operand twin. Same 20-byte geometry, same `num0..num3 @+0xC/+0xE/+0x10/+0x12` nonzero sweep; it differs from `mem4d_valid` only in the region/quadrant encoding. The v4 body (`0x14477c0`) masks the ADDR4 word `& 0x1fffffff` (strip region), checks the byte address `≤ 0x3fffff` (bank-size, bits `0..21`), and requires the quadrant bits `25-28` (`& 0x1e000000`) clear in the plain (non-quadrant) case. This is the descriptor-level confirmation of the [2.3](tensor-descriptors.md) source-vs-destination rule: `src = TENSOR*` and `dst = MEM_PATTERN*` differ only in the wire-struct name and which validator runs — the field math is identical.

### The auxiliary checks

| Check | Address (v4 unless noted) | What it reads | Role | Confidence |
|---|---|---|---|---|
| `check_m4d_active_channels(MEM_PATTERN4D, u8)` | v2 `0x1280010` / v3 `0x1370570` / v4 `0x14494e0` | ADDR4 `@+0` partition band + 2nd-window byte `+7` / word `+4`; the `u8` = active channel count | validates the caller-supplied partition/channel span against the ADDR4, **not** a descriptor field | CERTAIN |
| `m4d_not_in_psum(MEM_PATTERN4D)` | `0x14463a0` | region byte `+3` (inverse of the `0x20` marker) | asserts a 4D operand is **not** PSUM-resident where 4D must be SBUF-only | CERTAIN |
| `indirect_quadrant_check_src4d(MEM_PATTERN4D)` | `0x14484a0` | region byte `+3 & 0xe0 == 0x20`; data addr `@+0` masked `& 0x1fffffff`, `≤ 0x3fffff`; 2nd word `@+4` same way | the gather/indirect-mode 4D check: two address windows in the 20 bytes (data `@+0`, index/scale `@+4`) | CERTAIN |

> **QUIRK — an indirect 4D descriptor reinterprets `+4` as a second address, not as `step0`.** In the gather/indirect form the region bits gate a dual-`ADDR4` reading of the 20 bytes: the data address at `+0` and an index/scale window at `+4`, the same dual-address shape as the v4 indirect 3D arm. The *plain* (non-indirect) 4D descriptor uses `+4` as `step0`. A reimplementer decoding a 4D slot must branch on the region bits before interpreting `+4`. (The decoder's region branch is read directly; the full indirect-4D *encoder* leaf was not unrolled.)

> **NOTE —** `v4::mem4d_valid` ends with `is_valid_enum` / `is_valid_dtype` calls (`0x1446663` / `0x14466a7`) operating on bytes "past `+0x12`". Those are the `DTYPE` / `ENUM_LIST` arguments passed by value alongside the struct, **not** descriptor bytes. The 20-byte struct itself is `+0..+0x13`.

---

## Producer census — which ops emit 4D

### Purpose

Is 4D materialized *only* on BN / transpose-reduce paths, or is it a general fallback? Resolving every call site of the two `assignAccess<TENSOR4D>` thunks against the symbol table settles it: **4D is the general maximal data-tensor descriptor.**

### Method

The two real packers are reached through `.plt` thunks: the CoreV2 thunk `@0x603ee0` (→ `0x133e710`) has **57** call sites; the CoreV3 thunk `@0x5f32d0` (→ `0x1427170`) has **8**. Resolving each call-site address to its enclosing `nm` symbol yields the distinct producers below — 65 sites, 22 distinct op-encoders.

### CoreV2 producers (18 distinct)

| Op-encoder | Operand that is `TENSOR4D` |
|---|---|
| `visitInstBNStats` | the reduced ofmap input (`BATCH_NORM_STATS2`) |
| `visitInstBNStatsAggregate` | the partial-stats input (`BATCH_NORM_AGGREGATE`) |
| `visitInstTensorReduce` | the reduced data (free-axis TR + `CROSS_LANE_REDUCE`) |
| `visitInstTensorCopy` | src + dst data tensors |
| `visitInstStreamTranspose` | the DVE 32×32 data descriptor |
| `visitInstStreamShuffle` | the partition-shuffle data descriptor |
| `visitInstPool` | MaxPool / AvgPool ofmap |
| `visitInstMax` | the DVE MAX8 data descriptor |
| `visitInstMaxIndex` | the 4D data half of the 2-bundle split |
| `generateInstMatchReplaceWithOptionalMaxIndex` | the MatchReplace8 data descriptor |
| `visitInstGather` | gather data / source descriptors |
| `visitInstIndirectCopy` | the indirect-copy data descriptor |
| `visitInstIota` | the iota output descriptor |
| `visitInstMemset` | the memset target descriptor |
| `visitInstReciprocal` | the DVE reciprocal data descriptor |
| `visitInstRng` / `visitInstGetRandState` | the RNG state struct (`D4_RAND_STRUCT = TENSOR4D`) |
| `CoreV2GenImpl::assignAccessToType` | the string-keyed generic selector (below) |

### CoreV3 producers (4 distinct)

| Op-encoder | Operand that is `TENSOR4D` |
|---|---|
| `generateMatMulSparse` | the `MatmultSparse` IFMAP (gen3-new; sparsity adds a tag/group axis) |
| `visitInstMaxIndexAndMatchReplace` | gen3 fused max-index + match-replace |
| `visitInstGetRandState` / `visitInstRandGetState` | gen3 RNG-state `TENSOR4D` |

### The string-keyed generic selector

`assignAccessToType @0x1237a50` is a non-template dispatcher that branches on the literal struct-type *name* and forwards to the matching `assignAccess<TENSOR_ND>`. The four keys are `.rodata` strings — `"NEURON_ISA_TPB_TENSOR1D"` @ `0x1c84c5b`, `…2D` @ `0x1c84c73`, `…3D` @ `0x1c84c8b`, `…4D` @ `0x1c84ca3` — dispatched to the `.plt` thunks `<1D>(0x627d50)` / `<2D>(0x620dc0)` / `<4D>(0x603ee0)` / `<3D>(0x61b4e0)`. Op-encoders that carry the descriptor type as a string (custom-op and generic paths) route 4D operands here. This is the mechanism behind the breadth of the producer set.

### The answer

`TENSOR4D` is the **general, maximal** data-tensor descriptor — the default an op-encoder reaches for any operand that may need up to four free dims (copy / shuffle / transpose / pool / reduce / max / gather / iota / memset / reciprocal / RNG-state), plus the BN-stats reduced inputs and the gen3 sparse-matmul ifmap. It is **not** a BN / transpose-reduce special case. BN, cross-lane reduce, and sparse-matmul are simply the operands that most often *need* the fourth axis:

- **BNStats / Aggregate** — the reduced ofmap spans the partition axis plus up to three free axes; the input AP maps `dim-0 → ADDR4 partition` and three free + the reduction axis → the four `{step, num}` slots.
- **`CROSS_LANE_REDUCE` TensorReduce** (`axis = C`) — the reduce adds a cross-partition axis on top of the free pattern → a fourth descriptor dim.
- **`MatmultSparse` ifmap** — structured sparsity adds a tag/group axis, so the ifmap is `TENSOR4D` where the dense ifmap is `TENSOR3D`.

The producer set is read from the 65 resolved call sites; the per-op explanation of *why* each needs a fourth axis is reconstructed from the operand roles.

---

## Arch invariance and the encoder↔decoder matrix

The 20-byte layout is invariant across CoreV2/V3/V4 — the same `+0xC` count base, the same `{step3 @+0xA, num3 @+0x12}` spill — proven three ways:

- **encoder** — `assignStaticPattern<core_v2::TENSOR4D>` @ `0x1176df0` and `<core_v4::TENSOR4D>` @ `0x150cf60` are byte-identical in the field math (`step @+4+2i`, `num @+0xC+2i`, `{1,1}` fill);
- **DST decoder** — `mem4d_valid` v2 `0x127f660` / v3 `0x136ee40` / v4 `0x1446550` all sweep `num @+0xC/+0xE/+0x10/+0x12`, region `@+3 = 0x20`, addr `@+0`;
- **SRC decoder** — `tensor4d_valid` v3 `0x136f600` / v4 `0x14477c0` use the same `num` sweep.

| Claim | Witness | Confidence |
|---|---|---|
| 20-byte size | `.rodata` `"4D must have 20 bytes"` @ `0x1d6e990`, ref'd by both 4D dispatchers (`0x133e9b1` v2, `0x14273b6` v3) | CERTAIN |
| `step @ +4/+6/+8/+0xA` | encoder `lea [2i+4]` @ `0x11772b9` + default `+0xA` @ `0x1177397` | CERTAIN |
| `num @ +0xC/+0xE/+0x10/+0x12` | encoder `lea [2i+0xC]` @ `0x11772f9` + default `+0x12` @ `0x117739b`; decoder `cmpw` at the same four offsets (all 3 `mem4d_valid`) | CERTAIN |
| 16→20 spill = +1 pair, base `+0xA→+0xC` | `mem3d_valid` sweeps `num` to `+0xE` (3); `mem4d_valid` to `+0x12` (4) | CERTAIN |
| dim-count = `active ≤ 5` (`= N+1`) | `cmp $0x5,%r13d ; setbe` @ `0x1177162` + `"[1, 4]D"` `.rodata` @ `0x1d53378` | CERTAIN |
| dim-0 → ADDR4 (no slot) | `assignStartAddr<ADDR4>` head of packer @ `0x1176ff8`; decoder reads `+0` → `tensor_start_addr_valid` | CERTAIN |
| indirect/mode marker — byte `+3` bit 5 = word bit 29 (`0x20`); nibble = byte3 `& 0x60` = word bits 29:30 | encoder `or desc[+3],0x20`; decoder `and $0x60 ; cmp $0x20` | CERTAIN |
| producer set = 18 v2 + 4 v3 | 65 resolved call sites of the two `TENSOR4D` thunks | CERTAIN |

---

## Evidence summary

| Claim | Grounding | Confidence |
|---|---|---|
| 20-byte layout, spill delta, active-dim range check, producer census | size pinned by the `.rodata` assert referenced from both 4D dispatchers; offsets byte-exact from the packer `lea` offsets and the validator `cmpw` offsets; the spill shows up both ways (3D `num`-sweep stops at `+0xE`, 4D reaches `+0x12`); producer set resolved from 65 thunk call sites | CERTAIN |
| Encoder ↔ decoder round-trip | every offset the packer writes is an offset a validator reads, for DST (`mem4d_valid` ×3) and SRC (`tensor4d_valid` ×2) | CERTAIN |
| ADDR4 `@+0` bit map | owned formally by [2.2 ADDR4](addr4.md); reconstructed here only as the descriptor's first word | HIGH |
| Axis-to-slot mapping after `num==1` compaction | follows the `[W,Z,Y,X]` order plus the high→low compaction loop; per-op axis assignment belongs to the upstream AP lowering | HIGH |
| Indirect-4D dual-address form | the decoder branches on region to read `+4` as a second address; the indirect-4D *encoder* leaf was not individually unrolled | HIGH |
| No `assignAccess<core_v4::MEM_PATTERN4D>` exists | absent from the retained dynamic symbol table; the v4 4D DST role reuses the `TENSOR4D` struct plus `mem4d_valid` | CERTAIN |

## Cross-References

- [2.3 TENSOR1D / 2D / 3D Descriptors — the 4+4N Rule](tensor-descriptors.md) — the 1/2/3D base this page extends: the `4+4N` size rule, the two-separate-arrays layout, the stride/count word encoding, the `{1,1}` unit-fill, and partition folding.
- [2.2 The ADDR4 Start-Address Word](addr4.md) — the 4-byte word at `+0`: partition-band bits, the `0x1fffffff` byte address, the region/quadrant bits the validators gate on, and the register-mode flag.
- [2.8 Access-Pattern Encoder Dispatch](ap-encoder-dispatch.md) — how an op-encoder picks *which* `assignAccess<TENSOR_ND>` template to call, fixing the compile-time choice that selects 4D over 3D *(planned)*.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the two on-chip memories these descriptors walk; the SBUF `(partition, byte)` space and the PSUM bank space the partition band resolves into.
- [BIR `AccessPattern` / `APPair`](../bir/) — the `bir::AccessPattern` source object the 4D packer reads (`Pattern @+0x50`, `num_dims @+0x58`, `APPair {step @+0, num @+8}`), and the `Pattern[0].step ≥ 0` partition constraint.
