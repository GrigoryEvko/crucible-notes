# NCFW DRAM Images + ctx_log Decoder

This page documents the **data-side** of the NCFW firmware — the four embedded
**DRAM data-images** (`v2`/`v3`/`v4`/`v4_plus`, the static-config companions of the
IRAM code-images) — and the host-side **`libncfw_ctx_log`** runtime-context
pretty-printer that walks the firmware's DRAM-resident structs and emits them as a
1 MiB JSON blob. It is the *data-and-decoder* companion to
[NCFW IRAM Images + Host Selector](ncfw-iram-images.md) (which owns the code-image
carve and the v5 wall) and to
[The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) (which owns the
ISA/decode/dispatch-spine story). **This page owns: the DRAM-image carve, the three
static dispatch/CSR tables in the DRAM header (the exc-cause vector @ `+0x000`, the
engine register-address table @ `+0x010`, and the 12-entry `algo_type`
engine-dispatch table @ `+0x0b0`), and how the host `ctx_log` chain reads the
firmware's runtime context.**

> **GOTCHA — two Xtensa cores, do not cross the wires.** The structs decoded here
> belong to the **scalar Xtensa-LX management core** (NCFW), *not* the Vision-Q7
> "Cairo" FLIX datapath that runs custom-op kernels. The DRAM image is the LX core's
> initialized `.data`; the `ctx_log` decoder is host x86-64 code that formats the LX
> core's *runtime* context. Neither involves the Q7 SIMD ucode (which ships
> separately in `libnrtucode_extisa.so`). See the
> [scalar-LX core page](../../uarch/ncfw-lx-core.md) for the full ISA evidence.

**Provenance & confidence.** Every fact below is read **this session** from the
shipped host library `libncfw.so` (sha256 `598920d7…`) with stock binutils
(`readelf` / `nm` / `objdump -M intel` / `sha256sum`) and a Python ELF/byte/struct
reader. Lawful interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no
vendor source snapshot consulted. Tags follow the
[Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte
/ size / symbol / disassembler output read from the binary **this pass**; `CARRIED`
= OBSERVED in a cited prior carve and reused; `INFERRED` = reasoned over those.
Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a reimplementation
trap), **NOTE** (orientation), **CORRECTION** (overturns a prior reading).

> **THE v5 / MAVERICK WALL.** `libncfw` ships **exactly four** NCFW generations
> (v2/v3/v4/v4_plus) on the DRAM side too — `nm` lists exactly four `v#_ncfw_dram_bin`
> symbols, and `libncfw_ctx_log` routes to exactly four codename decoders. **There is
> no MAVERICK (v5) DRAM image and no v5 `ctx_log` decoder in this binary.** Any claim
> about a v5 NCFW context-struct interior would be fabrication — the file contains no
> such image. v5 is named here only to mark the wall. See
> [§7](#7-the-v5--maverick-wall) and the
> [MAVERICK profile](../../generations/maverick-profile.md). `[WALL]`

---

## 0. Target binary identity (all anchors match this session)

| Field | Value | How verified `[HIGH/OBSERVED]` |
|---|---|---|
| Path | `…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libncfw.so` | `fd --no-ignore` |
| Size | **615640** bytes | `stat -c %s` |
| SHA-256 | `598920d743762c03b3007c089829c02d0095408bf431fa3533e508c5f0aa3e49` | `sha256sum` |
| ELF | ELF64 LSB DYN, x86-64, **not stripped** (HOST library) | `readelf` |
| `.rodata` | PROGBITS, **VMA == off == `0x65000`**, size `0x2c8e4` | `readelf -SW` |
| `.data` | PROGBITS, VMA `0x95020` ≠ off `0x94020` (**delta `0x1000`**) | `readelf -SW` |

The four DRAM blobs and all `ctx_log` rodata strings live in `.rodata`, where **VMA
equals file offset**, so each blob symbol's address *is* its file offset and the
carve is a direct `f[off : off+size]` slice — no `.data` VMA↔file delta correction is
needed. (That `0x1000` `.data` delta matters only for the two `.data`-resident
globals, e.g. `ncfw_log_buffer_full`; the DRAM image is `.rodata`.) `[HIGH/OBSERVED]`

---

## 1. The DRAM image inventory — four data-blobs

Each generation ships a **DRAM** (data) blob immediately preceding its IRAM blob in
`.rodata`, followed by a `u32 *_dram_bin_size` word that the
[host selector](ncfw-iram-images.md#2-the-host-selector-libncfw_get_image-text-0x1179)
loads. The **carved length, the `u32` size word, and the `nm -S` symbol size all
agree** for every blob — no padding slack. Re-carved + hashed with Python this pass
`[HIGH/OBSERVED]`:

| gen | codename | NC | file-off | size (B / hex) | `u32` szword (off) | nonzero | sha256 (carved) |
|---|---|---|---|---|---|---|---|
| v2 | SUNDA | NC-v2 | `0x66a60` | 14016 / `0x36c0` | `0x36c0` (`0x6a120`) | 147 (1.05%) | `ca01951124e505b6…` |
| v3 | CAYMAN | NC-v3 | `0x74a40` | 19968 / `0x4e00` | `0x4e00` (`0x79840`) | 243 (1.22%) | `2418ab0f6350ce93…` |
| v4 | MARIANA | NC-v4 | `0x7e440` | 19968 / `0x4e00` | `0x4e00` (`0x83240`) | 243 (1.22%) | `1c3ac5f445865844…` |
| v4+ | MARIANA_PLUS | NC-v4+ | `0x87ea0` | 19968 / `0x4e00` | `0x4e00` (`0x8cca0`) | 243 (1.22%) | `1c3ac5f445865844…` |

The codename↔NC binding is per the committed
[Codename ↔ Generation Map](../../generations/codename-generation-map.md):
SUNDA=NC-v2 (coretype 6), CAYMAN=NC-v3 (13), MARIANA=NC-v4 (21),
MARIANA_PLUS=NC-v4+ (29); `arch_id = coretype − 1`. The four sha256s match the
[IRAM-images carve](ncfw-iram-images.md#1-the-image-inventory--four-iram--four-dram-blobs)
exactly. `[HIGH/OBSERVED for the carve; codename↔NC HIGH/CARRIED]`

Three structural facts fall straight out `[HIGH/OBSERVED]`:

- **MARIANA and MARIANA_PLUS share a byte-identical DRAM** (both sha `1c3ac5f4…`;
  Python `v4 == v4plus` → `True`). MARIANA_PLUS is a *code-only* delta over MARIANA on
  a **shared data image** — confirming the IRAM-side conclusion from the data side.
- **SUNDA's DRAM is smaller** (`0x36c0` vs `0x4e00`) and structurally simpler
  ([§4](#4-per-arch-differences-v2-is-simpler-v3v4-are-relocated)) — the older,
  smaller-fabric generation.
- **Every DRAM image is ~99% zero.** Each is a **sparse raw memory image**: a small
  initialized header (the LX firmware's `.data`) followed by a large all-zero tail
  (its `.bss` / runtime working RAM). No ASCII, no ELF header, no version string.

> **NOTE — DRAM is a flat table image, not ELF.** `file(1)` reports "data" (v2) or a
> libmagic false-positive ("OpenPGP Public Key" on v3/v4 — the leading-byte pattern,
> not a real format). The **first bytes are a `u32` table, not instructions** (unlike
> the IRAM images, whose first bytes are live Xtensa `j` vectors). The init header is
> read as **data tables**; the host `ctx_log` decoders — which walk the *runtime*
> context those tables seed — are the disassemblable half. `[HIGH/OBSERVED]`

### 1.1 Init-data extent vs zero tail

| image | total | init-data extent (last nonzero) | zero tail | nonzero |
|---|---|---|---|---|
| v2 | `0x36c0` | `0x000`–`0x23c` (573 B) | `0x23d`–`0x36c0` | 1.05% |
| v3 | `0x4e00` | `0x000`–`0x3f4` (1013 B) | `0x3f5`–`0x4e00` | 1.22% |
| v4 | `0x4e00` | `0x000`–`0x3f4` (1013 B) | `0x3f5`–`0x4e00` | 1.22% |
| v4+ | `0x4e00` | *(== v4)* | *(== v4)* | 1.22% |

The init header is the LX firmware's initialized `.data` (the three static tables of
[§2](#2-the-dram-header--static-config-tables)); the zero tail is its `.bss` / runtime
working RAM — the region the live context (which `ctx_log` later dumps,
[§3](#3-the-host-ctx_log-decoder)) is built into at runtime. `[init/zero split
HIGH/OBSERVED; "tail = .bss working RAM" INFERRED HIGH — the only place the live ctx
can reside given an all-zero static image.]`

---

## 2. The DRAM header — static config tables

The init header carries **three** addressable tables that the LX firmware boots from,
all at fixed offsets from the DRAM base. Byte-decoded from the carved v4 image this
pass (`<I`/`<Q` struct unpack); v3 is the same shape with a constant `+0x18` offset
shift, and v2 is structurally simpler ([§4](#4-per-arch-differences-v2-is-simpler-v3v4-are-relocated)).

### 2.1 The exc-cause / boot vector @ `DRAM+0x000` (4 × u32)

The first four `u32` are IRAM code addresses — the firmware's exception-cause /
command-dispatch entry vector:

| gen | `+0x00` | `+0x04` | `+0x08` | `+0x0c` |
|---|---|---|---|---|
| v2 (SUNDA) | `0x1bb3` | `0x1bcf` | `0x1be3` | `0x1bb3` |
| v3 (CAYMAN) | `0x1399` | `0x13b1` | `0x13c5` | `0x1399` |
| v4 (MARIANA) | `0x1399` | `0x13b1` | `0x13c5` | `0x1399` |
| v4+ | *(== v4)* | | | |

All are small (`< 0x4c00`) IRAM code addresses. **Slot 3 == slot 0** in every
generation — a default/catchall (`{UserExc, KernelExc, DblExc, NMI→fallback}`,
cross-ref [main dispatch loop](main-dispatch-loop.md)). `[values HIGH/OBSERVED; the
exc-cause role HIGH/INFERRED from the slot-3==slot-0 catchall + the matching IRAM
handler bodies decoded in main-dispatch-loop.md.]`

### 2.2 The engine register-address table @ `DRAM+0x010` (20 × u64)

This is the SOC CSR / NC-event base-address table — the **literal `soc_addr`
integers** the `ctx_log` / barrier sub-decoders print as `"0x%016lX"`. Byte-decoded
from v4 (v3 byte-identical; see [§4](#4-per-arch-differences-v2-is-simpler-v3v4-are-relocated)):

| off | u64 | role `[HIGH/OBSERVED bytes; role HIGH/INFERRED]` |
|---|---|---|
| `+0x010` | `0x0000002802700000` | NC0 engine A |
| `+0x018` | `0x0000003802700000` | NC0 engine B |
| `+0x020` | `0x0000006802700000` | NC0 engine C |
| `+0x028` | `0x0000007802700000` | NC0 engine D |
| `+0x030` | `0x0000802802700000` | NC1 engine A *(bit[47] set)* |
| `+0x038` | `0x0000803802700000` | NC1 engine B |
| `+0x040` | `0x0000806802700000` | NC1 engine C |
| `+0x048` | `0x0000807802700000` | NC1 engine D |
| `+0x050`–`+0x068` | `…02000000`/`03000000`/`04000000`/`05000000` | NC0 ctl blocks |
| `+0x070`–`+0x088` | same four, `+0x8000` bank | NC1 ctl blocks |
| `+0x090` | `0x0000008006800000` | NC0 notif/DMA block 0 |
| `+0x098` | `0x0000008006900000` | NC0 notif/DMA block 1 |
| `+0x0a0`–`+0x0a8` | `…06800000`/`06900000`, `+0x8000` bank | NC1 notif/DMA |

Two byte-grounded properties pin the layout `[HIGH/OBSERVED]`:

- **The engine selector is bits [32:39]** of the low half: `0x28`/`0x38`/`0x68`/`0x78`
  = engines A/B/C/D, over a common base `0x02700000` (the **NC_EVENT region base**,
  matching the IRAM-side `reg-addr table base 0x2700000000` decoded in
  [main-dispatch-loop](main-dispatch-loop.md)).
- **The two banks differ in exactly bit [47].** `reg[0] ^ reg[4] =
  0x0000_8000_0000_0000` (bit 47), identical for every NC0↔NC1 pair (e.g.
  `reg[16] ^ reg[18]` also = bit 47). Bit [47] is the **die/NC-select bit** — i.e.
  the *same* CSR on die-0 vs die-1. So the table holds **per-die engine register
  apertures**, NC0 in the lower banks and NC1 (`+0x8000`) in the upper.

> **NOTE — this is where the `soc_addr` integers actually live.** Sibling barrier and
> ring decoders note that "the `soc_addr` integers live in the firmware DRAM image,
> not in `libncfw`." This table **is** that store: the DRAM `+0x010` per-die register
> apertures, printed by the host `ncfw_log_addr` helper (`@0x41c3`) through the
> `"%s: \"0x%016lX\""` format (`"soc_addr"` @`0x6511d`, `"0x%016lX"` @`0x6510a`). The
> ring `recv/send/post/dma_compl_sema`, the mesh event semaphores, and the barrier
> `barrier_sema`/`dma_sync_sema` are computed by the **LX firmware** from these bases
> plus per-entry offsets. `[base table HIGH/OBSERVED; the firmware-side per-entry
> arithmetic INFERRED — it runs in the un-disassemblable LX core.]`

### 2.3 The 12-entry `algo_type` engine-dispatch table @ `DRAM+0x0b0`

> **CORRECTION — `DRAM+0x0b0` is ONE contiguous 12-entry `algo_type` jump table, not
> an 8-slot table plus a separate 4-entry secondary table.** An earlier reading on
> this page split the `0xb0..0xe0` window by value range (the `0x3c..` cluster at
> `+0xb0` vs the `0x3e..` cluster at `+0xd0`) into two tables. **That split is
> overturned by the dispatch instruction**, re-verified byte-exact this pass at v3
> IRAM `0x3bf8`: `const16 a2,0xB0 (24 b0 00) ; addx4 a2,a3,a2 (20 23 a0) ; l32i.n
> a5,a2,0 (58 02)` — a **single** `const16` base literal (`24 b0 00` occurs **exactly
> once** in the v3 image, **zero** in v2), **one** `addx4` ×4-scale, and **one**
> `l32i.n`. A genuine 8+4 split would need a *second* base literal `const16 0xD0`
> (`24 d0 00`) and a *second* indexed load; the v3 image contains **neither**
> (`24 d0 00` count = 0). The `+0xd0` boundary is a layout coincidence — entries 8..11
> of the same table land at `0xB0 + 8·4 = 0xD0`. The capstone
> [`lx-isa-naming-archid-synthesis`](lx-isa-naming-archid-synthesis.md) §4.3 (which
> reads the `addx4/l32i` dispatch and bounds `a3 ∈ 0..11`), the
> [`main-dispatch-loop`](main-dispatch-loop.md) §4, and the
> [`ncfw-iram-images`](ncfw-iram-images.md) §1 NOTE all read this as the single
> 12-entry table; this page now agrees. `[HIGH/OBSERVED — the v3 `0x3bf8`
> `addx4/l32i` dispatch read is the decider; `<12I` unpack of the `0xb0..0xe0`
> window.]`

The table maps the dispatch `algo_type` index (0..11) to an IRAM case-label
entry-point inside the main-loop function (a staggered computed-goto, not separate
functions — see [main dispatch loop §4.2](main-dispatch-loop.md#42-the-12-entry-table-dram0xb0-carved-byte-exact)):

| | idx 0 | 1 | 2 | **3** | 4 | 5/6/7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|
| **v4** (`+0x0b0`) | `0x3c38` | `0x3c35` | `0x3c2e` | `0x48f0` | `0x3c27` | `0x3c20` (shared) | `0x3e9c` | `0x3e32` | `0x3e76` | `0x3e80` |
| **v3** (`+0x0b0`) | `0x3c20` | `0x3c1d` | `0x3c16` | `0x48c4` | `0x3c0f` | `0x3c08` (shared) | `0x3e84` | `0x3e1e` | `0x3e5e` | `0x3e68` |

Structure `[HIGH/OBSERVED]`:

- **12 entries, two case-clusters + an outlier.** idx `{0,1,2,4}` are distinct entries
  packed on a **7-byte stride** in the `0x3c..` cluster (`0x3c38`/`0x3c35`/`0x3c2e`/
  `0x3c27` — a computed-goto chain in the IRAM); **idx 3 is the structurally-distinct
  outlier** (`0x48f0` in v4, far from the cluster — the error/default leg into the
  `0x48e0` region); idx `5/6/7` all share the `0x3c20` catchall entry; idx `8..11` are
  the second case-cluster in the `0x3e..` region.
- **`DRAM+0x0b0` is all-zero in v2/SUNDA** — SUNDA has *no* `+0xB0` dispatch table
  (its simpler monolithic boot, [§4](#4-per-arch-differences-v2-is-simpler-v3v4-are-relocated)),
  consistent with the absence of any `const16 0xB0` (`24 b0 00`) instruction in v2.
- The handler bodies these entries point to are decoded on the
  [main dispatch loop](main-dispatch-loop.md) page; here the table only pins the
  *entry contract*.

### 2.4 The 40-byte descriptor table @ `DRAM+0x188` (v3/v4) / `+0x098` (v2)

A regular array of `0x28`-byte records — the firmware's static per-entry
config/channel descriptor table, loaded at boot. Field offsets byte-pinned from the
raw record bytes this pass (record `[0]` = `ff ff ff 00 | 00 00 00 00 | 00 00 00 80 |
00 00 00 01 | 00 00 00 00 | 00 00 00 00 | 01 00 00 00 | …`):

| off | type | field | v3/v4 values | v2 values |
|---|---|---|---|---|
| `+0x00` | u32 | tag / class | `0x00ffffff` | `0x00ffffff`; **last 2 recs `0x03ffffff`** |
| `+0x04` | u32 | reserved (0) | 0 | 0 |
| `+0x08` | u32 | id / CSR-offset | `0x80000000`, step `+0x04000000` (`0x80000000…0xbc000000`) | `0x01/04/05/06/07/08/09/0a/0b…000000`; `0x80/84000000` |
| `+0x0c` | u32 | count / width | `1` | `1` (`4` for the `0x03`-class recs) |
| `+0x10` | u32 | address | `0` | `0x00100100 + 8·i` progression |
| `+0x14` | u32 | sequential **index** | `0,1,…,N−1` | `0,1,…,N−1` |
| `+0x18` | u32 | bool/flag | alternating `1`/`0` | `0` |
| `+0x1c`–`+0x27` | — | zero | | |

Record count: **v3/v4 = 16; v2 = 11**. `[record shape + field offsets HIGH/OBSERVED;
per-field semantics INFERRED MED — no host decoder walks the DRAM directly, so naming
is structural.]`

> **QUIRK — the `+0x00` "class" lives in byte [3], the rest is a `0xffffff` marker.**
> The little-endian `u32` reads `0x00ffffff`, i.e. low three bytes `0xff ff ff` are a
> fixed marker and byte [3] is a small class field (`0x00`, or `0x03` for v2's last
> two records). Mis-reading the marker as the whole field, or the `+0x08` id as a
> single byte, is the easy trap; the structurally-correct read is byte [3] = class,
> `+0x08` u32 = id with a `0x04000000` stride. `[HIGH/OBSERVED]`

---

## 3. The host `ctx_log` decoder

`libncfw_ctx_log` is the **second public entry** of `libncfw` (the first is
`libncfw_get_image`). It pretty-prints the LX firmware's *runtime* context — the live
state built in the DRAM zero-tail — as a single 1 MiB JSON blob.

### 3.1 Public entry `libncfw_ctx_log` (`.text 0x1309`, 0xac B)

Fully disassembled this pass (`objdump -d 0x1309`). Signature recovered from the arg
spills; dispatch ladder is the **same four-key binary-search ladder** as
`libncfw_get_image` `[HIGH/OBSERVED]`:

```c
// @ libncfw.so .text 0x1309 (exported T). Args spilled at 0x1315–0x1321:
//   rdi=ctx -> [rbp-0x8] ; rsi=buf -> [rbp-0x10] ; rdx=model_name -> [rbp-0x18] ;
//   ecx=arch_id -> [rbp-0x1c]
int libncfw_ctx_log(void *ctx, char *buf, const char *model_name, uint32_t arch_id)
{
    // binary-search cmpl ladder on arch_id {0x1c, 0x14, 0x05, 0x0c}:
    switch (arch_id) {
      case 0x05: return sunda_ncfw_ctx_log        (ctx, buf, model_name); // call 0x1a12b
      case 0x0c: return cayman_ncfw_ctx_log        (ctx, buf, model_name); // call 0x32ed2
      case 0x14: return mariana_ncfw_ctx_log        (ctx, buf, model_name); // call 0x4bc79
      case 0x1c: return mariana_plus_ncfw_ctx_log (ctx, buf, model_name); // call 0x64a20
      default:   return 0x16;                    // 0x13ae: mov eax,0x16 -> EINVAL (22)
    }
}
```

Every leg forwards `(rdi=ctx, rsi=buf, rdx=model_name)` unchanged; only the per-arch
struct offsets ([§3.2](#32-buffer-setup-ncfw_ctx_log-text-0x19f01)) differ per copy.
**Out-of-range arch_id (incl. `0x24` = MAVERICK) returns EINVAL — no v5 leg, no
default image.** This is the second independent four-key anchor (the first is
`get_image`), so `arch_id ↔ codename` and the count-of-four are doubly pinned.
`[HIGH/OBSERVED]`

Each per-codename wrapper (e.g. `sunda_ncfw_ctx_log @0x1a12b`, 0x31 B) is a trivial
thunk: it re-spills the three args and tail-calls `ncfw_ctx_log` — verified at
`0x1a155: call 0x19f01`. The four wrappers exist only so the per-arch offsets are
baked in per copy. `[HIGH/OBSERVED]`

### 3.2 Buffer setup: `ncfw_ctx_log` (`.text 0x19f01`, 0x22a B)

```c
// @ libncfw.so .text 0x19f01. args at [rbp-0x48]=ctx, [rbp-0x50]=buf, [rbp-0x58]=model_name
int ncfw_ctx_log(void *ctx, char *buf, const char *model_name)
{
    if (!ctx || !buf || !model_name) return 0x16;          // 0x19f1a..0x19f2d: NULL-guard -> EINVAL
    memset(buf, 0, 0x100000);                              // 0x19f4a: the buffer is EXACTLY 1 MiB
    // open the outer object: "ncfw_ctx_top_sp": { "model_name": "<model_name>", ...
    //   key  "ncfw_ctx_top_sp" @ 0x65685 ; fmt "  \"model_name\": \"%s\",\n" @ 0x6566e
    // every snprintf is bounds-checked: snprintf(buf+strlen(buf), 0x100000 - strlen(buf), ...)
    //   on overflow -> sets the global byte ncfw_log_buffer_full (.bss 0x95029)
    void *configs  = (char*)ctx + 0x0000;                  // configs base
    void *neff_ctx = (char*)ctx + 0x3060;                  // sunda/v2 immediate (add rax,0x3060 @0x1a0cb)
    void *algo_ctx = (char*)ctx + 0x30C0;                  // sunda/v2 immediate (add rax,0x30c0 @0x1a0d9)
    ncfw_log_ctx(buf, indent, enable, neff_ctx, algo_ctx, configs); // call 0x19c0e
    return ncfw_log_buffer_full ? 0x1c : 0;                // 0x1c == overflow ; 0 == success
}
```

The 1 MiB `memset` (`mov edx,0x100000; call memset@plt`), the bounds-checked
`snprintf` pattern (`strlen(buf)` then `0x100000` cap), the two rodata strings, and
the three child-pointer computes are all byte-confirmed. The **per-arch offsets scale
with the configs size**: the cayman copy (`ncfw_ctx_log @0x32ca8`) uses `add
rax,0x4280` (neff_ctx) and `add rax,0x42e0` (algo_ctx) instead — verified at
`0x32e72`/`0x32e80`. `[HIGH/OBSERVED]`

| arch | neff_ctx offset | algo_ctx offset |
|---|---|---|
| sunda / v2 | `ctx+0x3060` | `ctx+0x30C0` |
| cayman / v3, mariana / v4, mariana_plus / v4+ | `ctx+0x4280` | `ctx+0x42E0` |

The `+0x3060→+0x4280` jump is the mesh-event-count delta (50 vs 108 events ×80 B),
carried from the algo-configs size — see
[§4](#4-per-arch-differences-v2-is-simpler-v3v4-are-relocated).

### 3.3 The 3-way fan-out: `ncfw_log_ctx` (`.text 0x19c0e`, 0x2f3 B)

`ncfw_log_ctx` reads the enable-flag byte and emits the **three top-level children**
in this order — `configs`, `neff`, `algo`:

```c
// @ libncfw.so .text 0x19c0e. enable gate: movzx eax,[rdx] @0x19cb3 (picks "<key>": { vs bare {)
void ncfw_log_ctx(char *buf, int indent, void *enable,
                  void *neff_ctx, void *algo_ctx, void *configs)
{
    // ...emit '"<key>": {' wrapper...
    ncfw_log_configs (buf, indent, configs);   // key "configs" @0x65666 -> call 0x19915  (STATIC config)
    ncfw_log_neff_ctx(buf, indent, neff_ctx);  // key "neff"    @0x6565d -> call 0x1653b  (RUNTIME neff)
    ncfw_log_algo_ctx(buf, indent, algo_ctx);  // key "algo"    @0x65658 -> call 0x18cd2  (RUNTIME algo)
}
```

All three call sites + the three rodata key strings (`"configs"` @`0x65666`,
`"neff"` @`0x6565d`, `"algo"` @`0x65658`) are byte-decoded. `[HIGH/OBSERVED]`

> **NOTE — `"configs"` is static, `"neff"`/`"algo"` are runtime; and there are *two*
> `"neff"` keys.** The top-level `"configs"` subtree is the **static** config (the
> data the DRAM header seeds); the top-level `"neff"` and `"algo"` are the **runtime**
> contexts (built in the DRAM zero-tail). Confusingly, *inside* `"configs"` there is
> **also** a `"neff"` sub-key (the NEFF config) — distinct from the top-level `"neff"`
> runtime ctx. Keep them apart. `[HIGH/OBSERVED]`

### 3.4 The `"configs"` subtree (`ncfw_log_configs @0x19915`)

Emits three sub-objects; the sub-struct pointers are `cfg + a per-arch offset`,
proven from the `lea`/`add` immediates before each call (sunda copy):

| key | sub-decoder | sunda offset | v3/v4/v4+ offset | leaf reference |
|---|---|---|---|---|
| `algo` | `ncfw_log_algo_configs` (`0x1961c`) | `cfg+0x0` | `cfg+0x0` | ring/mesh/hier configs ([ring](ring-kangaring.md) / [mesh](mesh-collective.md) / [hier](hierarchical-collective.md)) |
| `neff` | `ncfw_log_neff_configs` (`0x12864`) | `cfg+0x2228` | `cfg+0x3448` | NEFF config (children below) |
| `dev` | `ncfw_log_dev_configs` (`0xc371`) | `cfg+0x3030` | `cfg+0x4250` | tsync / tpb_id / dev_id / seng_id |

Verified at `0x19abf`/`0x19ac8`/`0x19ae3`/`0x19aec`/`0x19b07` (sunda `lea
[rax+0x2228]`/`[rax+0x3030]`). The **`neff` config block has three children**, decoded
from `ncfw_log_neff_configs @0x12864`'s call sites `[HIGH/OBSERVED]`:

| call site | child | key | leaf page |
|---|---|---|---|
| `0x12a8d` | `ncfw_log_configs_neff_dma_alloc_bitmap` (`0xce01`) | `dma_alloc_bitmap` @`0x654d1` | [dma-reprogram](dma-reprogram-apb-bcast.md) |
| `0x12aba` | `ncfw_log_neff_barrier_config` (`0x10f8b`) | `barrier_configs` @`0x654e2` | [device](neff-device-barrier.md) / [host](neff-host-barrier.md) barrier |
| `0x12ae7` | `ncfw_log_basic_block_configs` (`0x11cc7`) | `basic_block_configs` @`0x654f2` | spad_ctrl → cc_op_entry ([spad/cc_op](spad-ccop-tsync.md)) |

So the **device-barrier config is a child of `configs."neff"`** — the barrier struct
lives inside the NEFF config block. The `soc_addr` apertures from
[§2.2](#22-the-engine-register-address-table--dram0x010-20--u64) are printed through
`ncfw_log_addr @0x41c3` (also called from `ncfw_log_neff_configs`).

### 3.5 The runtime `"neff"` and `"algo"` subtrees

`ncfw_log_neff_ctx` (`0x1653b`, the top-level `"neff"` key) `[HIGH/OBSERVED]`:

```c
// @ libncfw.so .text 0x1653b
//   +0x5C  u8   barrier_completed   (movzx eax,[rax+0x5c] @0x167ca ; key "barrier_completed" @0x655ce)
//   +0x00  op          -> ncfw_log_op_ctx        (call 0x150a2 @0x168d5 ; key "op")
//   +0x20  basic_block -> ncfw_log_basic_block_ctx (lea [rax+0x20] @0x168de ; call 0x15ff1)
```

`ncfw_log_algo_ctx` (`0x18cd2`, the top-level `"algo"` key) — the runtime algorithm
scoreboard, three coexisting sub-ctxs `[HIGH/OBSERVED]`:

```c
// @ libncfw.so .text 0x18cd2
//   +0x000  ring         -> ncfw_log_algo_ring_ctx          (call 0x16a00 @0x18e7c) -> ring scoreboard
//   +0x200  hierarchical -> ncfw_log_algo_hierarchical_ctx (lea [rax+0x200] @0x18e85 ; call 0x183c6)
//   +0x204  mesh         -> ncfw_log_algo_mesh_ctx          (lea [rax+0x204] @0x18ea9 ; call 0x1884c)
```

The `+0x200`/`+0x204` offsets and the three call targets are byte-confirmed. The
`neff_ctx`→`algo_ctx` gap in the sunda layout is `0x30C0 − 0x3060 = 0x60`.

### 3.6 The full output shape

The complete `ctx_log` JSON (every key + nesting OBSERVED; leaf field detail from the
named sibling pages) `[HIGH/OBSERVED for structure]`:

```jsonc
{ "ncfw_ctx_top_sp": {
    "model_name": "<arg>",
    "configs": {                         // STATIC (ctx+0x0)  -- seeded by the DRAM header
       "algo": { "ring": {…}, "mesh": {…}, "hierarchical": {…} },   // ncfw_log_algo_configs
       "neff": { "dma_alloc_bitmap": …,
                 "barrier_configs": { "host_barrier": …, "device_barrier": …, … },
                 "basic_block_configs": [ … spad_ctrl / cc_op_entry … ] },
       "dev":  { "tsync": { "tpb_id":…, "dev_id":…, "seng_id":… } }
    },
    "neff": {                            // RUNTIME (ctx+0x3060 v2 / +0x4280 v3/v4)
       "barrier_completed": <u8>, "op": {…}, "basic_block": {…}
    },
    "algo": {                            // RUNTIME (ctx+0x30C0 v2 / +0x42E0 v3/v4)
       "ring": { "channels": [ 32 × {…} ] },
       "hierarchical": { "run_state": <u8> },
       "mesh": { "event_index": <u16> }
    }
} }
```

Buffer: a single 1 MiB (`0x100000`) text buffer, `memset`-zeroed, `snprintf`-appended
with a global overflow byte (`ncfw_log_buffer_full @0x95029`, `.bss`). Format is
pretty-printed JSON: `"%*s"` indent (`@0x65001`), `"\"%s\": {\n"`, `"%s: \"0x%016lX\""`
for `soc_addr`s. `[HIGH/OBSERVED]`

---

## 4. Per-arch differences (v2 is simpler; v3/v4 are relocated)

**v4 (MARIANA) DRAM == v4_plus (MARIANA_PLUS) DRAM — byte-identical** (Python `==` →
`True`; sha `1c3ac5f4…`). `[HIGH/OBSERVED]`

**v3 (CAYMAN) vs v4 (MARIANA) DRAM — differ in exactly 13 bytes**, all in the
dispatch-table region `[HIGH/OBSERVED]`:

| offsets | nature |
|---|---|
| `+0xb0`–`+0xdf` (12-entry `algo_type` table, idx 0..11) | each entry `+0x18` (e.g. idx0 `0x3c20`→`0x3c38`, idx3 `0x48c4`→`0x48f0`, idx8 `0x3e84`→`0x3e9c`, idx11 `0x3e68`→`0x3e80`) |
| `+0x12c` (one IRAM ptr) | `0x5c`→`0x88` byte |

Byte-diff this pass: differing offsets =
`{0xb0,0xb4,0xb8,0xbc,0xc0,0xc4,0xc8,0xcc,0xd0,0xd4,0xd8,0xdc,0x12c}` (13 total). The
delta is a **near-uniform `+0x18` IRAM-code relocation** — a small block of LX code was
inserted ahead of the `0x3c20` handler cluster, shifting those entry-points. **The
reg-addr table (`+0x10`–`+0xaf`) and the 40-byte descriptor table are byte-identical
between v3 and v4** — only IRAM-address fields moved, by a constant. CAYMAN→MARIANA is
a pure code-layout delta, not a config/topology change.

**v2 (SUNDA) DRAM — structurally simpler** `[HIGH/OBSERVED]`:

| image | size | reg-addr entries | 12-entry `algo_type` table | 40-B records |
|---|---|---|---|---|
| v2 | `0x36c0` | 4 (`0x0fff_xxxx` range) | **absent** (`+0xb0` all-zero) | 11 |
| v3 | `0x4e00` | 20 (`0x02xx_0270_0000`) | present | 16 |
| v4 | `0x4e00` | 20 | present (`+0x18` vs v3) | 16 |
| v4+ | `0x4e00` | == v4 | == v4 | == v4 |

SUNDA has only **4** real register apertures (`0x0fffc2700000`, `0x0fffc6700000`,
`0x0ffff0600000`, `0x0ffff0d00000`), **no `+0xB0` dispatch table** (`+0xb0` zeroed; its
boot block of `0xffffffff` sentinels + IRAM ptrs starts by `+0x30`), and **11**
descriptor records. The per-arch ×4 delta on the runtime context is therefore a
**size delta** (v2 smaller) + an **offset shift** of the neff/algo ctx regions — *not*
a schema change. The sub-struct layouts themselves (ring channel, mesh event, barrier
step, etc.) are schema-wide identical across all four copies. `[HIGH/OBSERVED]`

---

## 5. DRAM static config ↔ `ctx_log` live dump

The DRAM image is the **static substrate**; `ctx_log` dumps the **live context** that
the static tables seed. The bridge `[HIGH/OBSERVED where noted, else INFERRED]`:

- **`soc_addr` table** (DRAM `+0x010`) ↔ the `soc_addr` u64s `ctx_log` prints
  `"0x%016lX"`. The ring/mesh/barrier semaphore addresses are computed by the LX
  firmware from these per-die bases + per-entry offsets. `[base OBSERVED; address
  arithmetic INFERRED — runs in the LX core, no shipped disassembler config.]`
- **40-byte descriptor table** (DRAM `+0x188`) — its record count (v2=11, v3/v4=16)
  and `+0x14` index field (`0..N−1`) mirror the host ctx's per-channel / per-entry
  arrays; the static seed for the runtime scoreboards dumped under `"algo".ring` /
  `"neff".basic_block`. `[structural correspondence INFERRED MED.]`
- **DRAM zero tail ↔ runtime `"neff"`/`"algo"`.** The ~99%-zero region is the LX
  `.bss`/working RAM; the only place the live ctx can reside. `[INFERRED HIGH.]`
- **DRAM ≠ host rodata.** `libncfw`'s rodata holds the JSON key/format strings + the
  decoder code; the DRAM holds the raw `u32`/`u64` tables (zero ASCII). They are
  complementary halves of the same firmware contract. `[HIGH/OBSERVED — no string
  overlap.]`

---

## 6. Reimplementer's checklist (the data-asset + decoder obligations)

To rebuild a compatible NCFW DRAM image + host decoder:

1. **Ship one DRAM data-image per generation** in `.rodata` immediately before the
   IRAM image, each followed by its `u32 *_dram_bin_size` word at `start+size`; raw
   flat, no ELF/load-address header. `[HIGH]`
2. **Lay the header tables-first**: exc-cause vector @ `+0x000` (4×u32, slot 3 == slot
   0 catchall); per-die engine register-address table @ `+0x010` (`u64` apertures,
   engine selector in bits [32:39], NC bank in bit [47]); the **12-entry `algo_type`
   engine-dispatch table** @ `+0x0b0` (idx 0..11, ×4-indexed by a single `addx4` —
   one contiguous table, entries 8..11 land at `+0xd0`); 40-byte descriptor array @
   `+0x188`. Zero the rest (the runtime working region). `[HIGH]`
3. **Provide `ctx_log(ctx, buf, model_name, arch_id)`** with the *same four-key*
   `cmpl {0x1c,0x14,0x05,0x0c}` ladder as `get_image`, routing to per-codename thunks;
   EINVAL (22) on NULL or out-of-range arch_id. `[HIGH]`
4. **Use a 1 MiB output buffer**, `memset`-zeroed, with bounds-checked `snprintf`
   (`0x100000 − strlen(buf)`) and a global overflow flag; emit
   `"ncfw_ctx_top_sp" → {model_name, configs, neff, algo}`, with `configs` static and
   `neff`/`algo` the runtime contexts. `[HIGH]`
5. **Scale the runtime-ctx offsets to the configs size** — `neff_ctx`/`algo_ctx` at
   `+0x3060`/`+0x30C0` for the small (50-event) generation and `+0x4280`/`+0x42E0` for
   the larger (108-event) one. `[HIGH]`

---

## 7. The v5 / MAVERICK wall

**No MAVERICK (v5) NCFW DRAM image and no v5 `ctx_log` decoder ship in `libncfw.so`**
— evidence of absence, on independent byte-grounded lines `[HIGH/OBSERVED]`:

1. `nm` lists exactly four `v#_ncfw_dram_bin` symbols — `rg -i 'maverick|v5_'`
   returns nothing.
2. `libncfw_ctx_log` ([§3.1](#31-public-entry-libncfw_ctx_log-text-0x1309-0xac-b))
   routes to exactly **four** codename decoders; out-of-range arch_id (incl. `0x24`)
   → EINVAL, no fifth leg.
3. The four `.c` source strings are `sunda.c`/`cayman.c`/`mariana.c`/`mariana_plus.c`
   (@`0x954f9` < `0x95923` < `0x9592c` < `0x95936`) — no `maverick.c`.

> **NOTE — this matches the IRAM-side wall.** The DRAM count is four *identically* to
> the IRAM count. MAVERICK is a known codename elsewhere in the corpus, but **no
> shipped 2.31.x `libncfw` provisions a coretype-37 NCFW image or decoder**. Any v5
> NCFW context interior is therefore **ABSENT / fabrication** — never observed. See
> the [MAVERICK profile](../../generations/maverick-profile.md). `[HIGH/OBSERVED for
> the absence; CARRIED for the "driven differently" claim.]`

---

## 8. Cross-references

- [NCFW IRAM Images + Host Selector](ncfw-iram-images.md) *(Part 10)* — the code-image
  carve, the `libncfw_get_image` selector, the v4↔v4+ IRAM diff, and the v5 wall.
- [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) *(Part 6)* — the
  full ISA evidence (windowed XEA2 ABI, the `0x24` handler, the FLIX-mis-decode
  debunk). **The authoritative core characterization.**
- [NCFW Main Dispatch Loop](main-dispatch-loop.md) *(Part 10)* — decodes the IRAM
  handler bodies the DRAM exc-cause vector + 12-entry `algo_type` table point at, and
  the `waiti 15` idle loop.
- [NCFW DMA Reprogram + APB Broadcast + Alloc Bitmap](dma-reprogram-apb-bcast.md)
  *(Part 10)* — the `configs."neff".dma_alloc_bitmap` child + the DMA-engine / nc-event
  naming over the `+0x010` reg-addr base.
- [NCFW CUST3 DMA Doorbell Thunks](cust3-doorbell-thunks.md) *(Part 10)* — the
  per-channel doorbell thunks that consume the engine register apertures.
- [NCFW Ring Send/Wait + Config Schema + Host Command](ring-protocol-config-command.md)
  *(Part 10)* — the collective-config schema under `configs."algo"`.
- [Device Barrier](neff-device-barrier.md) / [Host Barrier](neff-host-barrier.md)
  *(Part 10)* — the `configs."neff".barrier_configs` children.
- [SPAD-ctrl / cc_op / tsync](spad-ccop-tsync.md) *(Part 10)* — the
  `basic_block_configs → cc_op_entry` leaf and `dev.tsync`.
- [Codename ↔ Generation Map](../../generations/codename-generation-map.md) *(Part 6)*
  — the authoritative `(arch_id, coretype, NC, codename)` join.

---

## 9. Verification ledger (grounded this session)

| # | claim | how grounded **this pass** | verdict |
|---|---|---|---|
| 1 | Four DRAM blobs; carved size == `u32` szword == `nm` size; v4 DRAM == v4+ DRAM | `nm -S` + Python carve + `<I` unpack + sha256: all four match; `v4==v4plus` True (sha `1c3ac5f4…`) | **HIGH/OBSERVED** |
| 2 | reg-addr table @ `DRAM+0x010` = 20×u64 per-die apertures, base `0x02700000`, engine selector bits [32:39], **NC bank = bit [47]** | `<20Q` unpack of v4 carve; `reg[0]^reg[4]=0x0000800000000000` (bit 47); base `0x02700000` common | **HIGH/OBSERVED** |
| 3 | **One 12-entry `algo_type` table** @ `DRAM+0x0b0` `{0x3c38,0x3c35,0x3c2e,0x48f0,0x3c27,0x3c20×3,0x3e9c,0x3e32,0x3e76,0x3e80}` (v3 = same `−0x18`; v2 absent) — single `addx4`-indexed table, NOT an 8+4 split | `<12I` unpack of v4/v3/v2 carves; v3 `0x3bf8` `const16 a2,0xB0; addx4; l32i.n` dispatch read (`24 b0 00` once, `24 d0 00` zero); v2 `+0xb0` all-zero | **HIGH/OBSERVED** |
| 4 | `libncfw_ctx_log @0x1309`: 4-key `cmpl {0x1c,0x14,0x05,0x0c}` ladder → `{sunda,cayman,mariana,mariana_plus}_ncfw_ctx_log`, EINVAL default | `objdump -d 0x1309`: cmp/je legs + `call 0x1a12b/0x32ed2/0x4bc79/0x64a20`, `mov eax,0x16` | **HIGH/OBSERVED** |
| 5 | ctx_log walk: 1 MiB `memset`, keys `ncfw_ctx_top_sp`/`model_name`/`configs`/`neff`/`algo`; fan-out configs@+0 / neff_ctx@+0x3060\|0x4280 / algo_ctx@+0x30C0\|0x42E0; ring@+0/hier@+0x200/mesh@+0x204; barrier_completed@+0x5c | `objdump -d 0x19f01/0x19c0e/0x18cd2/0x1653b/0x32ca8`; rodata strings @`0x65685`/`0x6566e`/`0x65666`/`0x6565d`/`0x65658` | **HIGH/OBSERVED** |

### Corrections recorded on this page

1. **`DRAM+0x0b0` is ONE contiguous 12-entry `algo_type` jump table** (idx 0..11),
   **not** an 8-slot table + a separate 4-entry secondary table (§2.3). An earlier
   reading on this page split the window by value range; the v3 `0x3bf8` dispatch
   instruction (`const16 a2,0xB0; addx4 a2,a3,a2; l32i.n a5,a2,0` — a single base
   literal + single ×4 index + single load, with no second `const16 0xD0`) overturns
   that split. Entries 8..11 land at `+0xd0` only as `0xB0 + 8·4`. Reconciled onto the
   capstone [`lx-isa-naming-archid-synthesis`](lx-isa-naming-archid-synthesis.md) §4.3
   and [`main-dispatch-loop`](main-dispatch-loop.md) §4.
2. **The reg-addr table at `DRAM+0x010` is the engine register-address table with the
   NC bank in bit [47]** (not an undifferentiated "soc_addr CSR table") — the engine
   selector is bits [32:39], the NC0/NC1 select is bit [47], byte-proven via the
   per-pair XOR (§2.2).
