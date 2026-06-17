# TENSOR1D / 2D / 3D Descriptors — the 4+4N Rule

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; build-id `92b4d331` for `libwalrus.so`, `a9b1ea38` for `libBIR.so`). The descriptor encoders and validators live in `neuronxcc/starfish/lib/libwalrus.so`; the `bir::AccessPattern` source object lives in `libBIR.so`. For `.text`/`.rodata` the virtual address equals the file offset (`.text` base `0x62d660`, `.rodata` base `0x1c72000`). Other wheels differ — treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

When an engine reads or writes a tensor that lives in SBUF or PSUM, the instruction does not carry a shape — it carries an **access pattern**: a start address plus a small set of `{stride, count}` pairs that tell the engine's address generator how to *walk* the on-chip memory. The Tonga ISA encodes that walk in a family of fixed-size **static-pattern descriptors** — `TENSOR1D`, `TENSOR2D`, `TENSOR3D` (and `TENSOR4D`, [2.4](tensor4d-mempattern4d.md)) — and this page recovers their exact on-wire byte layout. The bar is that a reimplementer, given an access pattern, can pack the correct bytes for the 1D/2D/3D case and have the hardware validator accept them.

Every descriptor in the family obeys one geometry, the **4+4N rule**: a descriptor that encodes `N` free dimensions is exactly `4 + 4N` bytes — `8` for 1D, `12` for 2D, `16` for 3D. The leading 4 bytes are the [ADDR4](addr4.md) start-address word; each free dimension contributes one signed 16-bit **stride** word and one unsigned 16-bit **count** word, for 4 bytes per dimension. The single structural surprise — and the thing that makes "interleaved" intuitions wrong — is that the strides and counts do **not** alternate: a descriptor is `[ADDR4][stride0 stride1 …][num0 num1 …]`, two separate contiguous arrays, the stride array first.

The descriptor encodes only the *free* axes. The partition axis (the outermost AP dimension, `Pattern[0]`) is folded into the ADDR4 word as a partition-band term and is never a stride/count word. A lower-rank pattern placed into a higher-rank descriptor unit-fills the spare dimensions with `{stride=1, num=1}` — never zero, because the validator rejects a zero count. There is no dim-count field on the wire at all: the dimensionality is fixed by *which* descriptor type the op-encoder chose at compile time.

For reimplementation, the contract is:

- The `4 + 4N` size rule and the per-type byte map (1D = 8, 2D = 12, 3D = 16).
- The two-array layout: all `N` strides at `+4`, then all `N` counts at `+(4 + 2N)`.
- Stride = signed `int16` element-units; count = unsigned `uint16`, `0` illegal.
- The `{1, 1}` unit-fill convention for spare dimensions and the absence of an on-wire dim count.
- Partition-axis folding into ADDR4, so the descriptor walks only `Pattern[1..N]`.

| | |
|---|---|
| **Size rule** | `slot_size(N) = 4 + 4N` bytes (1D=8, 2D=12, 3D=16, 4D=20) |
| **Layout** | `[ADDR4 u32 @+0][N × i16 stride @+4][N × u16 num @+(4+2N)]` — two separate arrays |
| **Stride word** | signed `int16`, two's-complement, `[-32768, +32767]`, **element units** (not bytes) |
| **Count word** | unsigned `uint16`, `[1, 65535]` (width allows `0`; validator rejects it) |
| **Spare-dim fill** | `{stride = 1, num = 1}` (never zero) |
| **Dim count** | none on wire — implicit in the descriptor *type* chosen by the op-encoder |
| **Encoders** | `assignStaticPattern<core_v2::TENSOR{1,2,3}D>` @ `0x11755d0`/`0x1175dd0`/`0x11765d0`; `core_v4` @ `0x150ad20`/`0x150b7e0`/`0x150c390` |
| **Validators** | `core_v4::tensor2d_valid` @ `0x14468c0`, `tensor3d_valid` @ `0x14467d0` |
| **Field writers** | stride → `sub_116ca30` (i16, sign-checked); count → `sub_116d620` (u16) |
| **Size asserts** | `.rodata` @ `0x1d6e810`/`0x1d6e8b0`/`0x1d6e920`/`0x1d6e990` |

---

## The 4+4N size rule

### Purpose

The descriptor is a fixed-size POD slot inside an encoded instruction. Before the packer fills it, the dispatcher (`assignAccess<T>`) asserts that the slot it was handed is exactly `sizeof(descriptor)` — `4 + 4N` bytes for an `N`-dimensional pattern. The `+4` is the ADDR4 word common to every descriptor; each free dimension adds one stride word and one count word, 4 bytes, split across the two arrays. The size is therefore not a tunable — it is fixed by the descriptor *type*, and the type is fixed by the op-encoder.

### The size asserts

Four `.rodata` strings pin the four sizes. They fire only if the encoded slot length disagrees with `sizeof`, so they are the encoder's own statement of the rule (raw bytes, `.rodata`):

| Descriptor | `N` | Size | Assert string | `.rodata` address | Confidence |
|---|---|---|---|---|---|
| `TENSOR1D` | 1 | **8 B** | `"ISA mem pattern 1D must have 8 bytes to encode"` | `0x1d6e810` | CONFIRMED |
| `TENSOR2D` | 2 | **12 B** | `"ISA mem pattern 2D must have 12 bytes to encode"` | `0x1d6e8b0` | CONFIRMED |
| `TENSOR3D` | 3 | **16 B** | `"ISA mem pattern 3D must have 16 bytes to encode"` | `0x1d6e920` | CONFIRMED |
| `TENSOR4D` | 4 | 20 B | `"ISA mem pattern 4D must have 20 bytes to encode"` | `0x1d6e990` | CONFIRMED |

`8 = 4 + 4·1`, `12 = 4 + 4·2`, `16 = 4 + 4·3`, `20 = 4 + 4·4` — the rule holds exactly. The `count`-array base is the other half of the rule: it starts at `4 + 2N` (past the ADDR4 word and all `N` stride words), i.e. `+6` (1D), `+8` (2D), `+0xA` (3D), `+0xC` (4D).

> **NOTE —** the `core_v4` destination spelling of these descriptors, `MEM_PATTERN2D` / `MEM_PATTERN3D` ([2.4](tensor4d-mempattern4d.md)), is **the same size** (12 / 16 B) and the same byte layout as the `TENSOR` source form. Source (`TENSOR*`) and destination (`MEM_PATTERN*`) differ only in the wire-struct *name* and which validator runs — never in the bytes. A reimplementer encodes both identically.

---

## The byte layout — two separate arrays

### Purpose

The single fact that distinguishes this layout from a naive guess: the stride words and the count words live in **two separate contiguous arrays**, not interleaved. The ADDR4 word comes first; immediately after it, all `N` strides; immediately after them, all `N` counts. For 1D the two arrays are one element each, so a 1D descriptor *looks* interleaved — but that is a degenerate coincidence, not a different layout (see the **CORRECTION** below).

### Algorithm

The encoder writes the slot in three steps. ADDR4 first (`assignStartAddr<ADDR4>`, [2.2](addr4.md)), then the stride array, then the count array:

```c
// neuronxcc::backend::Generator::assignStaticPattern<core_v?::TENSOR_ND>
// reconstructed from the real bodies (CoreV2 @0x11755d0/0x1175dd0/0x11765d0,
// CoreV4 @0x150ad20/0x150b7e0/0x150c390 — byte-identical field math).
void assignStaticPattern_ND(uint8_t *slot, const bir::AccessPattern &ap) {
    // [1] ADDR4 start word @+0  (partition dim folded in here — see below)
    assignStartAddr_ADDR4(slot + 0, ap);                  // call @0x11757d5 (1D), @0x11767d8 (3D)

    // [2] compact the free dims: drop degenerate (num==1) axes, keep order,
    //     iterate Pattern[num_dims-1 .. 1] (innermost first), SKIP index 0 (partition).
    int out = 0;
    for (int d = ap.num_dims - 1; d >= 1; --d) {          // 0x1175a1f: lea -0x1(r13),ebx
        if (ap.Pattern[d].num == 1) continue;             // num==1 = degenerate / broadcast
        write_stride_i16(slot + 4 + 2*out, ap.Pattern[d].step);   // sub_116ca30, @+4+2i
        write_num_u16   (slot + (4 + 2*N) + 2*out, ap.Pattern[d].num); // sub_116d620, @+(4+2N)+2i
        ++out;
    }

    // [3] back-fill spare dims up to the descriptor's capacity N with {1,1}
    for (int i = out; i < N; ++i) {
        write_stride_i16(slot + 4 + 2*i, 1);              // 0x00010001 immediate when paired
        write_num_u16   (slot + (4 + 2*N) + 2*i, 1);
    }
    // [validate] active = 1 + #{free dims kept}; require active <= N+1  (cmp $0x4 @0x1176942 for 3D)
}
```

The `stride` array base is `+4` and the `count` array base is `4 + 2N`, on both the encoder side and the validator side (verified below). `2N` is the byte span of the stride array (`N` × `i16`).

### Per-type byte map

| Descriptor | Total | ADDR4 | stride words | count words | Confidence |
|---|---|---|---|---|---|
| `TENSOR1D` (8 B) | `+0..+7` | `addr32 @+0` | `stride0 @+4` | `num0 @+6` | CONFIRMED |
| `TENSOR2D` (12 B) | `+0..+0xB` | `addr32 @+0` | `stride0 @+4`, `stride1 @+6` | `num0 @+8`, `num1 @+0xA` | CONFIRMED |
| `TENSOR3D` (16 B) | `+0..+0xF` | `addr32 @+0` | `stride0 @+4`, `stride1 @+6`, `stride2 @+8` | `num0 @+0xA`, `num1 @+0xC`, `num2 @+0xE` | CONFIRMED |
| `TENSOR4D` (20 B) | `+0..+0x13` | `addr32 @+0` | `+4`/`+6`/`+8`/`+0xA` | `+0xC`/`+0xE`/`+0x10`/`+0x12` | CONFIRMED |

Worked offsets for the count-array base: `+6 = 4 + 2·1`, `+8 = 4 + 2·2`, `+0xA = 4 + 2·3`, `+0xC = 4 + 2·4`. These are byte-exact from the writer `lea` offsets (`lea 0x2(rax,r13)` for 1D, `lea 0x4(rax,r13)` for 2D, `lea 0x6(rax,r13)` for 3D — each = `base + (2i+4) + 2N_offset`) and from the validator `cmpw` offsets (next section).

> **CORRECTION (supersedes an earlier `TENSOR2D` decode) —** an earlier study described the 2D slot as the *interleaved* layout `[addr32 @+0][stride0 @+4][num0 @+6][stride1 @+8][num1 @+0xA]`. That is **wrong**. The verified 2D layout is the *separate-array* form `[addr32 @+0][stride0 @+4][stride1 @+6][num0 @+8][num1 @+0xA]`. The proof is symmetric: the encoder writes `stride @+4+2i` (`sub_116ca30`) and `num @+8+2i` (`sub_116d620`, `lea 0x4(rax,r13)` @ `0x11762d9`), and the validator `tensor2d_valid` reads `num0 @+8` and `num1 @+0xA` (next section). The two-separate-arrays rule is universal across 1D/2D/3D/4D; the apparent 1D "interleave" is the degenerate case where each array holds one element.

---

## The stride word — signed i16, element units

### Purpose

A stride word tells the address generator how far to advance along one free axis between consecutive elements. It is the per-axis *step* of the walk. Strides may be negative — a descending or reverse-iteration walk is a first-class pattern — so the word is a **signed** 16-bit integer.

### Encoding

The stride writer is `sub_116ca30 @0x116ca30`, called once per active free dimension to write an `i16` at `slot + 4 + 2*i`. Its overflow guard is the canonical *signed* narrowing check: it sign-extends the low 16 bits (`movswq %ax,%rcx` @ `0x116ca4c`) and compares the sign of the full 64-bit AP step against the sign of the truncated `i16` (`setg`/`setg`), raising an error only if truncation flipped the sign. The store is a plain 16-bit word write (`mov %ax,(%rdi)` @ `0x116cfa9`).

| Property | Value | Evidence | Confidence |
|---|---|---|---|
| Width | 16 bits | `mov %ax,(%rdi)` store | CONFIRMED |
| Signedness | **signed** two's-complement | `movswq` sign-extend + sign-match guard | CONFIRMED |
| Range | `[-32768, +32767]` | `i16` two's-complement | CONFIRMED |
| Units | **element units**, not bytes | dtype passed to writer only as error-message context, never as a scale | CONFIRMED |
| Sign bit | bit 15 of the word | i16 layout | CONFIRMED |

> **QUIRK —** the stride is in **element** units. The compiler writes the logical element step verbatim; the dtype byte-size multiply happens in hardware at walk time, not in the descriptor. (The one place a `×4` scale appears in the encoder is the MX-only `MXMEM_PATTERN1D` variant — *not* plain `TENSOR1D/2D/3D`.) A reimplementer must **not** pre-multiply strides by `dtypeBytes`.

> **GOTCHA (PSUM only) —** the validators apply a tighter-than-`i16` bound on `stride1` in the **PSUM-destination** path: `testb $0xe0,0x7(%rsp)` — byte `+7` is the high byte of `stride1 @+6`, and its top 3 bits must be clear, restricting `stride1` to roughly a 13-bit magnitude window for PSUM writes. The general SBUF-source path does **not** apply this mask. (Role inferred from the region gate — INFERRED.)

---

## The count word — unsigned u16, zero illegal

### Purpose

A count word is the element count along one free axis — how many elements the walk visits on that axis before the next-outer axis advances. It is an **unsigned** 16-bit integer.

### Encoding

The count writer is `sub_116d620 @0x116d620`, called once per active free dimension to write a `u16` at `slot + (4 + 2N) + 2*i`. Its overflow guard is the classic *unsigned* narrowing check (`and $0xffff,esi` @ `0x116d63f`, then a `setne`/`setne` comparing "full value nonzero" against "low-16 nonzero" — no sign extension). The store is `mov %ax,(%rdi)` @ `0x116da3f`.

| Property | Value | Evidence | Confidence |
|---|---|---|---|
| Width | 16 bits | `mov %ax,(%rdi)` store | CONFIRMED |
| Signedness | **unsigned** | `and $0xffff` guard, no sign-extend | CONFIRMED |
| Width range | `[0, 65535]` | u16 | CONFIRMED |
| Effective range | **`[1, 65535]`** | validator rejects `num == 0` (below) | CONFIRMED |
| Units | element count (no dtype scale) | logical count written verbatim | CONFIRMED |

> **GOTCHA —** although the *width* admits `0`, a zero count is **illegal** for an SBUF-resident descriptor: the validator rejects any descriptor with a zero count word (`setne` chain, next section). This is the decode-side counterpart of the encoder's `{1,1}` unit-fill — a never-written count word would be `0` and would be rejected, which is precisely *why* the encoder must unit-fill spare dimensions rather than leave them zero.

---

## The validators — encoder ↔ decoder agreement

### Purpose

The validators are the hardware-model decode-side check. They confirm the byte layout independently: every offset the encoder *writes* is an offset the validator *reads*. The 16-byte 3D descriptor passes by value in two registers (SysV ABI: a 16-byte POD → `rdi` = bytes `+0..+7`, `rsi` = bytes `+8..+0xF`), and the validator spills them so the stack frame mirrors the descriptor 1:1.

### `tensor3d_valid` @ `0x14467d0`

```c
// neuronxcc::core_v4::tensor3d_valid(NEURON_ISA_TPB_TENSOR3D, dtype)  @0x14467d0
// rdi -> (rsp)+0 = bytes +0..+7 ;  rsi -> 0x8(rsp) = bytes +8..+0xF
//   (rsp)+0 = addr32(+0..3)    0x4(rsp) = stride0/stride1 (+4..+7)
//   0x8(rsp) = stride2 (+8)    0xa(rsp) = num0   0xc(rsp) = num1   0xe(rsp) = num2
bool tensor3d_valid(desc, dtype) {
    uint8_t byte3  = addr32 >> 24;           // high byte of ADDR4 word
    int     region = byte3 & 0x60;
    if (region == 0x20) {                                  // PSUM-destination path
        if (stride2(+8) == 0)             return false;    // cmpw $0,0x8(rsp)
        if (!tensor_start_addr_valid(addr32)) return false;
        if (byte_at(+7) & 0xe0)           return false;    // testb $0xe0,0x7(rsp): stride1 hi
        return tensor_start_addr_valid(word @+4);          // tail re-validate
    } else {                                               // SBUF path
        if (!tensor_start_addr_valid(addr32)) return false;
        if ((byte3 & 0x70) == 0x40)       return false;    // SB sub-region reject
        return num0(+0xA) != 0 && num1(+0xC) != 0 && num2(+0xE) != 0;  // setne chain
    }
}
```

The SBUF path reads `num0 @+0xA`, `num1 @+0xC`, `num2 @+0xE` — exactly where `assignStaticPattern<TENSOR3D>` writes them — and requires all three nonzero. **Layout verified both ways.**

### `tensor2d_valid` @ `0x14468c0`

The 12-byte 2D descriptor passes as `rdi` (bytes `+0..+7`) + `esi` (bytes `+8..+0xB`, 32-bit). The body spills `mov rdi,(rsp)`, `mov esi,0x8(rsp)`, with `r15w` = bytes `+8..+9` = `num0`. The SBUF path requires `num0 @+8 != 0` (`test %r15w`) **and** `num1 @+0xA != 0` (`cmpw $0,0xa(rsp)`); the PSUM path applies the same `testb $0xe0,0x7(rsp)` `stride1`-high mask. This confirms the 2D separate-array layout — `stride0 @+4`, `stride1 @+6`, `num0 @+8`, `num1 @+0xA` — not interleaved.

### Agreement matrix

| Field | Encoder writes | Validator reads | Confidence |
|---|---|---|---|
| ADDR4 `@+0` | `assignStartAddr<ADDR4>(slot+0)` | `tensor_start_addr_valid(word)` | CONFIRMED |
| `stride0 @+4` | `sub_116ca30(slot+4)` i16 | part of ADDR4-style word `@+4` | CONFIRMED |
| `stride1 @+6` | `sub_116ca30(slot+6)` i16 | byte `+7` hi (`testb 0xe0`, PSUM) | CONFIRMED |
| `stride2 @+8` | `sub_116ca30(slot+8)` i16 (3D) | `cmpw $0,0x8(rsp)` ≠ 0 (PSUM, 3D) | CONFIRMED |
| `num0 @+8`/`+0xA` | `sub_116d620(base+0)` u16 | 2D: `test r15w@+8` / 3D: `cmpw +0xA` | CONFIRMED |
| `num1 @+0xA`/`+0xC` | `sub_116d620(base+2)` u16 | 2D: `cmpw +0xA` / 3D: `cmpw +0xC` | CONFIRMED |
| `num2 @+0xE` | `sub_116d620(base+4)` u16 (3D) | `cmpw $0,0xe(rsp)` ≠ 0 (3D) | CONFIRMED |
| partition | folded → ADDR4 band bits | addr band bits | CONFIRMED |
| dim count | (implicit type) + `active ≤ N+1` | (none on wire) | CONFIRMED |

> **NOTE —** there is no standalone `tensor1d_valid` for the *plain-tensor* 1D source path in the validator family that round-trips the count word (`mxmem1d_valid @0x1447190` validates the distinct MX 1D variant). 1D encoder↔validator agreement is therefore inferred from the identical writer offsets (`stride0 @+4`, `num0 @+6`) plus the shared `tensor_start_addr_valid` ADDR4 check — STRONG, not byte-round-tripped.

---

## The `{1, 1}` unit-fill for spare dimensions

### Purpose

A descriptor type has a fixed capacity (`N` slots). An access pattern with **fewer** active free dimensions than the capacity must still fill every slot — the validator rejects zero counts. The convention is to back-fill spare slots with the unit pattern `{stride = 1, num = 1}`: a no-advance, single-element axis — the identity that lets a lower-rank pattern legally occupy a higher-rank descriptor.

### How it is written

The packer emits only the non-degenerate (`num != 1`) free dims, compacted from output index 0, then back-fills the remainder to the cap. A `num == 1` axis is treated as degenerate/broadcast — dropped from the compacted prefix, re-materialized at the tail as a unit dim.

- **`TENSOR1D`** — the 1D-AP fast path writes the little-endian 32-bit immediate `0x00010001` at `slot+4` in one store (`movl $0x10001,0x4(rax)` @ `0x1175c17`): `stride0 = 1 @+4` and `num0 = 1 @+6` together. This is the *only* case where one 32-bit store fills a whole descriptor's free part — because 1D's stride array and count array are one element each and adjacent.
- **`TENSOR2D`** — per-slot fill `mov $1,0x4(rsi,rax,2)` (`stride @+4+2i`) + `mov $1,0x8(rsi,rax,2)` (`num @+8+2i`) @ `0x1176317`; tail sets `stride1 = 1 @+6`, `num1 = 1 @+0xA` @ `0x1176339`.
- **`TENSOR3D`** — per-slot fill `stride = 1 @+4+2i`, `num = 1 @+0xA+2i` @ `0x1176b17`; the 1D-into-3D fast path writes `0x00010001` at `+4` (stride0=stride1=1) **and** at `+0xA` (num0=num1=1) @ `0x1176c6e`/`0x1176c75`, then unit-fills slot 2 → all three pairs `{1,1}`.

> **QUIRK — "does a 1D descriptor zero/one-fill dims 2–3?"** It has none. A `TENSOR1D` descriptor is 8 bytes — one `{stride, num}` slot — so there are no dims 2–3 to fill. The `{1,1}` fill applies *within* a descriptor when the pattern has fewer active free dims than the descriptor's **capacity**: a 1-extent pattern placed into a 3D descriptor unit-fills slots 1 and 2; a 1D pattern in a 1D descriptor unit-fills its single slot. The fill is always **one** (`{1,1}`), never zero — a zero count is rejected by the validator.

### No dim-count field on the wire

The descriptor carries no explicit dim-count byte. The dimensionality is **implicit in the wire-struct type** (`TENSOR1D`/`2D`/`3D`), chosen at compile time by the caller op-encoder picking which `assignAccess<TENSOR_ND>` template to call (matmul always picks `TENSOR3D`, for example). At runtime the packer computes `active = 1 + #{free dims d ∈ 1..num_dims-1 : Pattern[d].num != 1}` and range-checks `active ≤ N+1` (3D: `cmp $0x4,%r13d` @ `0x1176942`; the `+1` accounts for the partition dim folded into ADDR4). On overflow it raises `"Illegal ISA memory access dimension <k>, which must be in range of [1, 3]D tensor for ISA mem pattern"`. This is a validity check only — it writes no count to the descriptor.

---

## Partition-axis folding

### Purpose

A reimplementer must know which AP dimension maps to the SBUF/PSUM partition axis and how it is encoded — because it is **not** one of the stride/count words. The descriptor's stride/count arrays describe only the *free* axes; the partition axis is folded into the ADDR4 word.

### The convention

The canonical AP axis order is `[W, Z, Y, X] = Pattern[0..3]`, where `Pattern[0] = W` is the **outermost** dimension and **is** the SBUF/PSUM partition axis. The packer iterates `Pattern[num_dims-1 .. 1]` (innermost first) and **skips index 0** — so a "3D" tensor pattern spans four logical AP dims: `Pattern[0] → ADDR4 address`, `Pattern[1..3] → the three {stride, num} slots`.

The partition dim is encoded implicitly, folded into the [ADDR4](addr4.md) `@+0` start-address word as the partition-band term:

```text
basePartition = MemLoc.homePartition + (dtypeBytes * offset) / partitionStride
addr_word     = (basePartition << 18)   + inPartitionByteAddr    // SBUF band
              = (basePartition << 15) + … + inPartitionByteAddr   // PSUM band
```

then band-shifted into the high bits of the ADDR4 word. The partition dim is never a stride or count word.

> **GOTCHA — partition step sign.** Unlike the free axes (which may have negative `i16` strides), the partition stride `Pattern[0].step` is asserted `≥ 0` upstream: `bir::AccessPattern::getStepBytesPerHighestDim` ([libBIR](../bir/) @ `0x203170`) reads `Pattern[0]` and `__assert_fail`s on `"Pattern[0].getStep() >= 0 && \"Negative step in Pattern[0] not supported\\n\""`. The partition axis never walks backward; only the free axes may. (CONFIRMED — the assert is read directly off the `libBIR` body, which also confirms the `bir::APPair` element layout `{step @+0, num @+8}` and the 20-entry `Dtype` table, dtype index `> 0x13` → `"Unknown dtype"`.)

---

## Arch invariance

The descriptor byte layout is **arch-invariant** across CoreV2/V3/V4. The CoreV2 packers (`0x11755d0`/`0x1175dd0`/`0x11765d0`) and CoreV4 packers (`0x150ad20`/`0x150b7e0`/`0x150c390`) are byte-identical in the field math — same `stride @+4+2i`, same `num @+(4+2N)+2i`, same `{1,1}` fill, same `0x00010001` immediates. The only difference is which ADDR4 leaf is called (`assignStartAddr<core_v2::ADDR4>` @ `0x1172e10` vs `<core_v4::ADDR4>` @ `0x1508df0`) and, on the v4 3D path, a second `assignStartAddr` call (@ `0x150cde3`) for the inline indirect/gather arm — *not* part of the plain `TENSOR3D` layout ([2.4](tensor4d-mempattern4d.md)).

> **NOTE —** CoreV3 (gen3/Cayman) has no standalone `assignStaticPattern` symbols; `assignAccess<core_v3::TENSOR3D>` @ `0x1425850` routes through `vtbl[+0x30]` (`mov 0x30(rax),rax ; jmp *rax`) to the shared `CoreV3GenImpl` leaf, reusing the same template body. The byte layout is identical — the v4 byte-match proves the geometry is arch-invariant. (STRONG — the v3 leaf was not individually disassembled.)

---

## Worked example: encode a 2D strided pattern

A reimplementer encoding an access pattern that walks an SBUF tensor along two free axes — outer stride 64 elements × 16 counts, inner stride 1 element × 32 counts, starting at some partition-folded ADDR4 word `A` — packs a `TENSOR2D` (12 B) as:

| Offset | Bytes | Field | Value |
|---|---|---|---|
| `+0x00` | 4 | ADDR4 word | `A` (partition dim folded into the band bits) |
| `+0x04` | 2 | `stride0` (i16) | `64` → `0x0040` |
| `+0x06` | 2 | `stride1` (i16) | `1` → `0x0001` |
| `+0x08` | 2 | `num0` (u16) | `16` → `0x0010` |
| `+0x0A` | 2 | `num1` (u16) | `32` → `0x0020` |

Little-endian on the wire (after the ADDR4 word): `40 00 01 00 10 00 20 00`. The strides come first as a block, then the counts — never interleaved. A reverse inner walk is `stride1 = -1` → `0xFFFF` (two's-complement i16). A 1D pattern dropped into this 2D slot would unit-fill the spare pair: `stride1 = 1`, `num1 = 1`.

---

## Gaps and Confidence

- **Byte offsets, sizes, signedness, `{1,1}` fill, partition folding — CONFIRMED.** The three sizes (8/12/16) are pinned by four `.rodata` asserts; the offsets are byte-exact from writer `lea` offsets and validator `cmpw`/`testb` offsets; stride signedness is the `movswq` sign-match guard, count unsignedness the `and $0xffff` guard; the `0x00010001` immediates pin the unit-fill; the `Pattern[0].step ≥ 0` partition constraint is read directly off the `libBIR` `getStepBytesPerHighestDim` body.
- **Encoder ↔ validator round-trip — CONFIRMED for 2D and 3D** (`tensor2d_valid` @ `0x14468c0`, `tensor3d_valid` @ `0x14467d0` read exactly the offsets the packers write); **STRONG for 1D** (no count-round-tripping `tensor1d_valid`; inferred from shared writer offsets + ADDR4 validator).
- **ADDR4 `@+0` bit map — STRONG.** The partition-band decomposition and band bits are owned formally by [2.2 ADDR4](addr4.md); reconstructed here only as the descriptor's first word.
- **CoreV3 layout — STRONG.** No distinct v3 static-pattern symbols; the vtable-reuse leaf was not individually field-walked, but the v2↔v4 byte-match makes the geometry arch-invariant.
- **PSUM `stride1` `0xe0`-mask role — INFERRED.** Read off the validator; its role as a PSUM-write magnitude bound (vs a general source constraint) is inferred from the region gate.

## Cross-References

- [2.2 The ADDR4 Start-Address Word](addr4.md) — the 4-byte word every descriptor embeds at `+0`: the partition-band bits, the `0x1fffffff` byte address, the `0x80000000` register-mode flag, and the `RegId < 0x40` constraint.
- [2.4 TENSOR4D / MEM_PATTERN4D & the 4D Spill](tensor4d-mempattern4d.md) — the 20-byte 4D case and the `MEM_PATTERN*` destination spelling; the indirect/gather arm reached via the v4 3D second `assignStartAddr`.
- [2.8 Access-Pattern Encoder Dispatch](ap-encoder-dispatch.md) — how the op-encoder picks *which* `assignAccess<TENSOR_ND>` template to call, fixing the implicit dim count.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the two memories these descriptors walk: the SBUF `(partition, byte)` 2-D space and the PSUM 1-D bank space the partition-band term resolves into.
- [BIR `AccessPattern` / `APPair` (Part 7, codegenAccess)](../bir/) — the `bir::AccessPattern` source object (`Pattern @+0x50`, `num_dims @+0x58`, `APPair {step @+0, num @+8}`) the encoder reads, and the `getStepBytesPerHighestDim` partition-step assert.
