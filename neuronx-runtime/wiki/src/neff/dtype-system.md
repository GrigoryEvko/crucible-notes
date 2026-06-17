# The dtype System

> *All addresses, offsets, struct ordinals, enum values, and table dumps on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). `.text`/`.rodata` VMA equals file offset for the cited ranges, so every `0x…` is an analysis VMA. Enum values are read from DWARF (`enums.json`); struct offsets from DWARF (`structures.json`); the `str_to_dtype` / `str_to_dtype_map` matchers from decoded LE-ASCII string-compare constants and the `__static_init` map-builder body; the `CSWTCH.19` / `CSWTCH.21` / `data_type_sizes.0` backing tables from `objdump -s` byte dumps. Source TUs: `/opt/workspace/KaenaRuntime/{kelf/kelf2kbin.cpp, tdrv/helper.c}`. Other versions will differ.*
> *Evidence grade: **Confirmed (table- and callsite-anchored)** — every map entry is a decoded `.rodata` byte dump or a decompiled map-builder line; the translators are full decompiled bodies. · Part V — Model Format & Loading · [back to index](../index.md)*

## Abstract

There is no single "data type" in this runtime. A tensor's element type is re-encoded **three times** as it travels from the model file to the silicon, by three independently-numbered enums that happen to name the same physical formats: `nrt_dtype_t` (the NEFF I/O-tensor JSON layer, where `FLOAT32=10` and `BFLOAT16=6`), `kbin_dtype` (the `def.json` / KBIN container layer, where the same formats renumber to `FP32=5`, `BF16=7`, and FP8 enters as `FP8_E3/E4/E5 = 1/2/3`), and `SDMA_DTYPE` — the DMA-engine `al_sdma_m2m_meta_ctrl_data_type` enum, which shares the **ISA-word numbering** where `FP8 = 13/14/15` and `UINT64 = 1`. None of the three maps is an identity onto another. The dtype byte you read at one layer is *meaningless* at the next without a table lookup, and the runtime carries those tables as `.rodata` switch-arrays compiled from a single `std::map`.

This page owns the **higher-level dtype system**: the value spaces of the three enums, the string→enum matchers that admit a dtype at each ingest point, the canonical **dtype→size** table, and — the load-bearing artifacts for a reimplementer — the two **translation maps** the DMA descriptor builder runs at staging time: `kbin_dtype_to_sdma_dtype` (`@0x266460`, the kbin→SDMA dtype remap referred to elsewhere as `translate_dma_dtype`) and `kbin_cce_op_to_sdma_cce_op` (`@0x2664c0`, the collective-compute op remap). The companion page [FP8 and dtype Encoding](../isa/fp8-dtype-encoding.md) owns the *other* end of the same system: the bit-level layout of the `NEURON_ISA_TPB_DTYPE` field inside the 64-byte instruction word, the FP8 format table (E3M4 / E4M3 / E5M2), and the `insert_set_fp8_conv_config` enablement encoder. The two pages share one fact and must never contradict it: **the FP8 instruction-word / SDMA numbering (`13/14/15`) is not the KBIN/NRT numbering (`1/2/3`)** — and the remap between them is the table this page dumps.

The reason three numberings coexist is historical layering, not redundancy. `nrt_dtype_t` is the public NRT-API / TVM-relay surface (`nrt_get_model_tensor_info`); `kbin_dtype` is a compact 4-bit container enum built for the JSON ingest of the per-engine programs; `SDMA_DTYPE` is the Alpine/AnnapurnaLabs UDMA hardware enum, fixed by silicon, that the compute path and the DMA path both speak on the wire. The runtime never unifies them — it translates at each boundary. A reimplementer who assumes `dtype == dtype` across the stack will mis-size buffers, mis-program SDMA copy-engine descriptors, and silently corrupt FP8/FP32R tensors, whose values *move* between layers (`FP32R`: `nrt`=11, `kbin`=6, `SDMA`=11).

For reimplementation, the contract is:

- **The three enum value spaces** — `nrt_dtype_t`, `kbin_dtype`, `SDMA_DTYPE` — and the fact that each is a distinct numbering of the same format set, *not* a re-tagging.
- **The two string-matchers** — `str_to_dtype` (inlined in `extract_tensor_info`, the NEFF I/O layer, a *smaller* accepted set) and the `str_to_dtype_map` `std::map` (`@bss 0xcb04a0`, the `def.json` layer, which adds FP8/FP4 and the packed `_x4` container types).
- **The dtype→size lookup** — `kbin_get_dtype_size` (`@0x266520`) composes kbin→SDMA→bytes through `data_type_sizes.0` (`@0x9d6e60`); the canonical sizes are `{fp8/int8/uint8=1, fp16/bf16/int16/uint16=2, fp32/fp32r/int32/uint32=4, int64/uint64=8}`.
- **The two translation maps** — `kbin_dtype_to_sdma_dtype` (`@0x266460`, `CSWTCH.19`) and `kbin_cce_op_to_sdma_cce_op` (`@0x2664c0`, `CSWTCH.21`) — as annotated C, the maps a DMA/CCE descriptor builder must reproduce verbatim.
- **The packed `_x4` types** — `float8_e4m3fn_x4`/`float8_e5m2_x4`/`float4_e2m1fn_x4` map to *unsigned-int container* dtypes (`UINT32`/`UINT16`), not to FP8/FP4 — the trap where a packed format hides inside an integer code.

| | |
|---|---|
| **NEFF I/O enum** | `nrt_dtype_t` (DWARF, 4 B) — `FLOAT32=10`, `FLOAT16=7`, `BFLOAT16=6`, FP8 reserved `13/14/15`, FP32R=`11` |
| **KBIN container enum** | `kbin_dtype` (ordinal 615, 4 B) — `0=INVALID … FP8_E3/E4/E5=1/2/3 … INT64=15` |
| **SDMA / ISA enum** | `SDMA_DTYPE` = `al_sdma_m2m_meta_ctrl_data_type` — shares `NEURON_ISA_TPB_DTYPE` numbering (FP8 = `13/14/15`) |
| **NEFF str→dtype** | `str_to_dtype` (inlined in `extract_tensor_info` `@0xe1710`) — LE-ASCII int compares; *smaller* set |
| **def.json str→dtype** | `str_to_dtype_map` `std::map<string,kbin_dtype>` `@bss 0xcb04a0` — 19 entries, built by `__static_init` `@0x78d80` |
| **kbin→SDMA dtype remap** | `kbin_dtype_to_sdma_dtype` `@0x266460` (96 B) — `CSWTCH.19` `@0x9d6e20` (15×u32) |
| **kbin→SDMA CCE-op remap** | `kbin_cce_op_to_sdma_cce_op` `@0x2664c0` (96 B) — `CSWTCH.21` `@0x9d6e00` (4×u32) |
| **dtype→size** | `kbin_get_dtype_size` `@0x266520` (73 B) → `data_type_sizes.0` `@0x9d6e60` (16×u32) |
| **FP8 cast config** | `kbin_fp8_conv_cfg_t` (16 B) — bit-encoding owned by [FP8 Encoding](../isa/fp8-dtype-encoding.md) |
| **Source TUs** | `kelf/kelf2kbin.cpp` (map builder, consumers); `tdrv/helper.c` (translators, size lookup) |

---

## 1. Three Coexisting dtype Encodings

### Purpose

A reimplementer's first job is to stop thinking of "dtype" as a single quantity. The runtime defines three separate enums, each authoritative at its own layer, and moves a tensor's type across them by table lookup. This section pins the three value spaces side by side so the boundaries are explicit; §2 gives the two string matchers that *enter* the system, §3 the size lookup, and §4 the translators that bridge it.

### Encoding — the three enums

The same physical format set carries different integer codes at each layer. The "SDMA / ISA word" column is owned in bit detail by [FP8 and dtype Encoding](../isa/fp8-dtype-encoding.md); it is reproduced here only to show the cross-layer divergence.

| Format | `nrt_dtype_t` (NEFF I/O) | `kbin_dtype` (KBIN / def.json) | `SDMA_DTYPE` / ISA word | Size (bytes) | Confidence |
|---|---|---|---|---|---|
| FP8 E3M4 | `13` (reserved) | `FP8_E3 = 1` | `13` | 1 | HIGH |
| FP8 E4M3 | `14` (reserved) | `FP8_E4 = 2` | `14` | 1 | HIGH |
| FP8 E5M2 | `15` (reserved) | `FP8_E5 = 3` | `15` | 1 | HIGH |
| FP16 | `FLOAT16 = 7` | `FP16 = 4` | `7` | 2 | HIGH |
| FP32 | `FLOAT32 = 10` | `FP32 = 5` | `10` | 4 | HIGH |
| FP32R | `FP32R = 11` | `FP32r = 6` | `11` | 4 | HIGH |
| BF16 | `BFLOAT16 = 6` | `BF16 = 7` | `6` | 2 | HIGH |
| UINT8 | `UINT8 = 3` | `UINT8 = 8` | `3` | 1 | HIGH |
| UINT16 | `UINT16 = 5` | `UINT16 = 9` | `5` | 2 | HIGH |
| UINT32 | `UINT32 = 9` | `UINT32 = 10` | `9` | 4 | HIGH |
| UINT64 | `UINT64 = 1` | `UINT64 = 11` | `1` | 8 | HIGH |
| INT8 | `INT8 = 2` | `INT8 = 12` | `2` | 1 | HIGH |
| INT16 | `INT16 = 4` | `INT16 = 13` | `4` | 2 | HIGH |
| INT32 | `INT32 = 8` | `INT32 = 14` | `8` | 4 | HIGH |
| INT64 | `INT64 = 12` | `INT64 = 15` | `12` | 8 | HIGH |
| (unknown) | `UNKNOWN = 0` | `INVALID = 0` | `0` (rejected) | 1 (`nrt` default) | HIGH |

The `nrt_dtype_t` values are decoded from the LE-ASCII integer compares inside `extract_tensor_info` (`@0xe1710`); the `kbin_dtype` values from the DWARF enum (ordinal 615) and the `str_to_dtype_map` builder body (`@0x78d80`); the `SDMA_DTYPE` column from the `CSWTCH.19` byte dump (`@0x9d6e20`, §4) cross-checked against the ISA `NEURON_ISA_TPB_DTYPE` enum on the [FP8 page](../isa/fp8-dtype-encoding.md).

> **QUIRK —** the three numberings agree on **nothing** systematic. `FP32` is `10 / 5 / 10` across the three layers; `BF16` is `6 / 7 / 6`; `FP32R` is `11 / 6 / 11`. The NEFF and SDMA columns coincide for several integer types (`UINT64=1`, `INT8=2`) but diverge for floats (`FLOAT32`: nrt `10`, kbin `5`). There is no arithmetic relation — each is a hand-assigned table. A reimplementer must carry three literal maps, not a formula.

> **GOTCHA —** `nrt_dtype_t` and `SDMA_DTYPE` look tantalizingly similar (both have `UINT64=1`, `INT8=2`, `FP32=10`) but **differ on the FP8 codes and on the float ordering** — and, more importantly, nothing in the runtime ever converts `nrt`→`SDMA` directly. The NEFF I/O enum is consumed only to build the public tensor-info descriptors; the wire path is `kbin → SDMA`. Do not "optimize" a reimplementation by treating the NEFF byte as an SDMA byte; the only legal bridges are the two in §4, and their domain is `kbin_dtype`, not `nrt_dtype_t`.

### The KBIN dtype value space (the spine)

`kbin_dtype` (DWARF ordinal 615, `uint32`) is the enum the rest of the lowering pivots on — it is the type of every `def.json` `dtype`/`_dtype` field after matching, the index into the size table, and the domain of the SDMA translator. Its 16 values are dense and contiguous:

```text
0 INVALID   1 FP8_E3   2 FP8_E4   3 FP8_E5   4 FP16   5 FP32   6 FP32r   7 BF16
8 UINT8     9 UINT16  10 UINT32  11 UINT64  12 INT8   13 INT16 14 INT32  15 INT64
```

> **NOTE —** unlike the ISA `NEURON_ISA_TPB_DTYPE` enum (which packs FP8 at the *top* of its range, `13/14/15`), `kbin_dtype` packs FP8 at the *bottom* (`1/2/3`) — this is precisely the inversion the kbin→SDMA translator (§4) exists to undo. The two enums are the same 16 formats in two different orders, and the FP4 / `CPTC` extension codes (`16`, `25..31`) the [ISA page](../isa/fp8-dtype-encoding.md) documents have **no** `kbin_dtype` member; the packed `_x4` types (§2) are how FP4/packed-FP8 enter the KBIN layer instead, riding inside integer container codes.

---

## 2. The Two String Matchers

### Purpose

A dtype enters the system as a **string** in JSON (`"float32"`, `"bfloat16"`, `"float8_e4m3fn"`). Two different matchers turn those strings into enum values, at two different tiers, and they accept **different sets**. Pinning both — and the gap between them — is what lets a reimplementer parse a NEFF without silently dropping a format.

### The NEFF I/O matcher — `str_to_dtype`

The TVM-relay I/O tensor layer (`neff.json` `attrs.dltype[]`) matches dtype strings inside `extract_tensor_info` (`@0xe1710`), where `str_to_dtype` is *inlined* as a sequence of length-keyed LE-ASCII integer compares (no `std::map`). It accepts the **smaller** set — the formats a model exposes as graph I/O — and defaults anything else to `UNKNOWN(0)` at 1 byte/element:

```c
// Inlined str_to_dtype, inside extract_tensor_info @0xe1710.
// Matches dltype STRING -> nrt_dtype_t via length + LE-ASCII word compares.
nrt_dtype_t str_to_dtype(const char *s, size_t len):
    switch (len):
      case 4: if eq(s,"int8")     return INT8;     // 0x38746e69
      case 5: if eq(s,"int16")    return INT16;    if eq(s,"int32") return INT32;
              if eq(s,"int64")    return INT64;    if eq(s,"uint8") return UINT8;
      case 6: if eq(s,"uint16")   return UINT16;   if eq(s,"uint32") return UINT32;
              if eq(s,"uint64")   return UINT64;
      case 7: if eq(s,"float32")  return FLOAT32;  if eq(s,"float16") return FLOAT16;
      case 8: if eq(s,"bfloat16") return BFLOAT16; // 0x363174616F6C6662
      default:
        log("Unknown dtype: %s, defaulting to UNKNOWN");
        return UNKNOWN;     // 0, sized at 1 byte/elem by calc_tensor_size
```

> **QUIRK —** `str_to_dtype` does **not** match any FP8/FP4 string. The NEFF I/O layer exposes only the "classic" 11 formats; FP8 tensors cross the I/O boundary disguised as their byte-equivalent integer/half code or are internal to the subgraph. The FP8/FP4 strings are recognized only by the `def.json` matcher below. `enum nrt_dtype_t` *defines* `FP32R=11` and FP8 `13/14/15`, but `str_to_dtype` has no case that emits them — the enumerators exist for the descriptor struct, not for I/O-string ingest.

### The def.json matcher — `str_to_dtype_map`

The KBIN-ingest layer (per-engine `def.json`, the `var` `dtype` field and the CCE descriptor `_dtype` field) matches through a real `std::map<std::string, kbin_dtype>` at `bss 0xcb04a0`, built once at image init by `__static_initialization_and_destruction_0` (`@0x78d80`) and consumed via `_Rb_tree::find` (helper `@0x4c8390`) at the two call sites inside `parse_one_dma_block` (`@0x4bc168`, `@0x4bf10d`). It is the **larger** set — 19 entries, adding FP8, `float32r`, and the packed `_x4` container types. The full map, byte-exact from the builder body:

| dtype string | `kbin_dtype` | Notes | Confidence |
|---|---|---|---|
| `"float8e3"` | `1` (FP8_E3) | | HIGH |
| `"float8e4"` | `2` (FP8_E4) | | HIGH |
| `"float8e5"` | `3` (FP8_E5) | | HIGH |
| `"float16"` | `4` (FP16) | | HIGH |
| `"float32"` | `5` (FP32) | | HIGH |
| `"float32r"` | `6` (FP32r) | reduced/round FP32 variant | HIGH |
| `"bfloat16"` | `7` (BF16) | | HIGH |
| `"uint8"` | `8` | | HIGH |
| `"uint16"` | `9` | | HIGH |
| `"uint32"` | `10` | | HIGH |
| `"uint64"` | `11` | | HIGH |
| `"int8"` | `12` | | HIGH |
| `"int16"` | `13` | | HIGH |
| `"int32"` | `14` | | HIGH |
| `"int64"` | `15` | | HIGH |
| `"float8_e4m3fn"` | `2` | **alias** of FP8_E4 | HIGH |
| `"float8_e4m3fn_x4"` | `10` (UINT32) | packed-4 FP8 E4M3 → UINT32 container | HIGH |
| `"float8_e5m2_x4"` | `10` (UINT32) | packed-4 FP8 E5M2 → UINT32 container | HIGH |
| `"float4_e2m1fn_x4"` | `9` (UINT16) | packed-4 FP4 E2M1 → UINT16 container | HIGH |

The `_dtype` key suffix is appended by `parse_one_desc_ap` before the lookup (the access-pattern name `+ "_dtype"`), so the CCE descriptor dtype is matched through the same map as the variable dtype.

> **GOTCHA —** the packed `_x4` types are the sharpest trap in the whole dtype system: **`float8_e4m3fn_x4` and `float8_e5m2_x4` resolve to `kbin_dtype 10 (UINT32)`, and `float4_e2m1fn_x4` to `9 (UINT16)` — not to any FP8/FP4 code.** Four packed FP8 sub-bytes ride inside one 32-bit unsigned container; four packed FP4 nibbles ride inside one 16-bit container. Every downstream stage (size lookup, SDMA translation, descriptor emission) then treats them as plain `UINT32`/`UINT16`, which is correct for *transport* (the bytes move opaquely) but means a reimplementer cannot recover "this is packed FP8" from the `kbin_dtype` alone — that information is consumed at match time and discarded. The separate validator `is_hw_supported_type` (`@0x4ae170`) is the only place the `_x4` strings are checked as a gate before the container-code remap.

> **NOTE —** `"float8_e4m3fn"` (without `_x4`) is a plain **alias** of `FP8_E4 = 2`; there is no `"float8_e5m2"` (non-`_x4`) entry in the map, nor a non-`x4` FP4. So an *unpacked* E5M2 must arrive as `"float8e5"` (→ `3`), and unpacked FP4 cannot enter the KBIN layer through this matcher at all. The 19 entries above are the complete accepted vocabulary of the `def.json` dtype fields.

---

## 3. The dtype → Size Lookup

### Purpose

Buffer sizing, tensor-byte accounting (`calc_tensor_size`), and descriptor length computation all need bytes-per-element. The runtime never stores a size on the dtype; it composes one through the translator and a 16-entry size table. A reimplementer reproduces this two-step exactly, because the table is keyed by **SDMA** dtype, not by `kbin_dtype` — so the size lookup *forces* a translation first.

### Algorithm — `kbin_get_dtype_size`

```c
// Models kbin_get_dtype_size @0x266520 (73 B), TU tdrv/helper.c.
// Composes: kbin_dtype -> SDMA dtype (CSWTCH.19) -> byte size (data_type_sizes.0).
uint32_t kbin_get_dtype_size(kbin_dtype dt):
    if dt == INVALID || dt > INT64:                  // out of 1..15
        al_hal_log(ERROR, "Invalid dtype %u passed"); // tag "dtype_sizeof"
        return 0
    sdma = kbin_dtype_to_sdma_dtype(dt)              // 0x266460, the §4 translator
    return data_type_sizes_0[sdma]                   // 0x9d6e60, 16 x u32, indexed by SDMA code
```

### Data table — `data_type_sizes.0`

The backing table at `@0x9d6e60`, indexed by **SDMA** dtype code (`0..15`), 16 × u32, byte-exact:

| SDMA code | dtype | size | SDMA code | dtype | size |
|---|---|---|---|---|---|
| 0 | (invalid) | 0 | 8 | INT32 | 4 |
| 1 | UINT64 | 8 | 9 | UINT32 | 4 |
| 2 | INT8 | 1 | 10 | FP32 | 4 |
| 3 | UINT8 | 1 | 11 | FP32r | 4 |
| 4 | INT16 | 2 | 12 | INT64 | 8 |
| 5 | UINT16 | 2 | 13 | FP8_E3 | 1 |
| 6 | BF16 | 2 | 14 | FP8_E4 | 1 |
| 7 | FP16 | 2 | 15 | FP8_E5 | 1 |

Folded back to `kbin_dtype`, the derived sizes are: FP8 (`1/2/3`) = 1; FP16/BF16 (`4/7`) = 2; FP32/FP32r (`5/6`) = 4; UINT8/INT8 (`8/12`) = 1; UINT16/INT16 (`9/13`) = 2; UINT32/INT32 (`10/14`) = 4; UINT64/INT64 (`11/15`) = 8.

> **GOTCHA —** the size table is indexed by **SDMA** code, so `data_type_sizes_0[dt]` with a raw `kbin_dtype` is **wrong**: at index `1` the table holds `8` (UINT64's size), but `kbin_dtype 1` is `FP8_E3`, whose true size is `1`. The composition through `kbin_dtype_to_sdma_dtype` is mandatory — skip it and every FP8/FP16 tensor is mis-sized by a factor of up to 8×. The NEFF I/O layer's `calc_tensor_size` uses the *nrt* dtype's own inline size path and is a separate code line; only the KBIN/DMA path goes through this SDMA-keyed table.

---

## 4. The Translation Maps

### Purpose

These are the load-bearing artifacts of the page. When the DMA/CCE descriptor builder lowers a `def.json` collective-compute descriptor to an Alpine SDMA m2m packet, it must convert both the **dtype** (`kbin_dtype` → `SDMA_DTYPE`) and the **CCE operation** (`kbin_dma_desc_op` → `SDMA_CCETYPE`). Two leaf translators in `tdrv/helper.c` do exactly that, each a single switch backed by a `.rodata` `CSWTCH` array. They are called from `vring_add_dma_packet_cce_int` / `dma_util_vring_append_descs` (the descriptor-emission path; see [Descriptor Format](../dma/descriptor-format.md)). A reimplementation must reproduce both arrays verbatim — they are the wire-encoding contract between the runtime and the SDMA engine.

### Entry Point

```text
vring_add_dma_packet_cce_int / dma_util_vring_append_descs   ── CCE descriptor emit
  ├─ kbin_dtype_to_sdma_dtype (0x266460, 96 B)    ── dtype remap   → CSWTCH.19 @0x9d6e20
  └─ kbin_cce_op_to_sdma_cce_op (0x2664c0, 96 B)  ── CCE-op remap  → CSWTCH.21 @0x9d6e00
dma_util_expand_desc_complete_cce
  └─ kbin_get_dtype_size (0x266520)               ── §3, composes the dtype remap with data_type_sizes.0
```

### Algorithm — `kbin_dtype_to_sdma_dtype` (the `translate_dma_dtype` remap)

```c
// Models kbin_dtype_to_sdma_dtype @0x266460 (96 B), TU tdrv/helper.c.
// kbin_dtype (1..15) -> al_sdma_m2m_meta_ctrl_data_type (the SDMA / ISA-word numbering).
// This is the kbin->SDMA remap; ZERO and out-of-range hard-fail.
al_sdma_m2m_meta_ctrl_data_type kbin_dtype_to_sdma_dtype(kbin_dtype dt):
    if dt < FP8_E3 /*1*/ || dt > INT64 /*15*/:
        nlog_write(ERROR, "Unsupported data type!: %u", dt);
        __assert_fail(... "helper.c", 0x1AF);        // hard fail on bad dtype
    return CSWTCH_19[dt - 1];                          // 0x9d6e20, 15 x u32, indexed (dt-1)
```

`CSWTCH.19` (`@0x9d6e20`, 15 × u32, indexed by `kbin_dtype - 1`), decoded byte-exact:

| `kbin_dtype` | → `SDMA_DTYPE` | | `kbin_dtype` | → `SDMA_DTYPE` |
|---|---|---|---|---|
| `FP8_E3 (1)` | `13` (`0x0d`) | | `UINT8 (8)` | `3` |
| `FP8_E4 (2)` | `14` (`0x0e`) | | `UINT16 (9)` | `5` |
| `FP8_E5 (3)` | `15` (`0x0f`) | | `UINT32 (10)` | `9` |
| `FP16 (4)` | `7` | | `UINT64 (11)` | `1` |
| `FP32 (5)` | `10` (`0x0a`) | | `INT8 (12)` | `2` |
| `FP32r (6)` | `11` (`0x0b`) | | `INT16 (13)` | `4` |
| `BF16 (7)` | `6` | | `INT32 (14)` | `8` |
| | | | `INT64 (15)` | `12` (`0x0c`) |

> **QUIRK —** this map is exactly the FP8-at-bottom → FP8-at-top inversion. `kbin_dtype 1/2/3 (FP8)` become SDMA `13/14/15`; `kbin_dtype 11 (UINT64)` becomes SDMA `1`. The output column is identical to the `NEURON_ISA_TPB_DTYPE` ISA-word numbering on the [FP8 page](../isa/fp8-dtype-encoding.md) — the compute path (instruction word) and the DMA path (SDMA descriptor) speak the **same** dtype dialect, and only the KBIN/NRT container layers use the divergent `1/2/3` form. So there is one remap at NEFF build/stage time (kbin→SDMA-or-ISA), not two independent ones.

### Algorithm — `kbin_cce_op_to_sdma_cce_op`

The collective-compute operation carried by a CCE descriptor (the `def.json` desc `"op"`: `fma`/`add`/`min`/`max`) renumbers the same way. `kbin_dma_desc_op` codes the op `1..4`; the SDMA engine's `SDMA_CCETYPE` uses a different ordering:

```c
// Models kbin_cce_op_to_sdma_cce_op @0x2664c0 (96 B), TU tdrv/helper.c.
SDMA_CCETYPE kbin_cce_op_to_sdma_cce_op(kbin_dma_desc_op op):
    if op < FMA /*1*/ || op > MAX /*4*/:
        nlog_write(ERROR, "Unsupported CCE Op! %u", op);
        __assert_fail(... "helper.c", 0x1C0);
    return CSWTCH_21[op - 1];                          // 0x9d6e00, 4 x u32, indexed (op-1)
```

`CSWTCH.21` (`@0x9d6e00`, 4 × u32, indexed by `kbin_dma_desc_op - 1`):

| `kbin_dma_desc_op` | def.json `"op"` | → `SDMA_CCETYPE` | Confidence |
|---|---|---|---|
| `FMA (1)` | `"fma"` | `1` | HIGH |
| `ADD (2)` | `"add"` | `0` | HIGH |
| `MIN (3)` | `"min"` | `3` | HIGH |
| `MAX (4)` | `"max"` | `2` | HIGH |

> **NOTE —** the CCE-op remap is *not* identity either: `ADD(2)` maps to SDMA `0`, and `MIN(3)`/`MAX(4)` swap to `3`/`2`. The `COPY(0)` / `TRANSPOSE(5)` ops from the full `kbin_dma_desc_op` set (documented on [Descriptor Format](../dma/descriptor-format.md)) are out of this translator's `1..4` domain — they are not CCE arithmetic ops and take a different descriptor path, which is why the array is only 4 wide and `__assert_fail`s on `0` or `5`.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `kbin_dtype_to_sdma_dtype` | `0x266460` | 96 B | kbin→SDMA dtype remap (`translate_dma_dtype`); `CSWTCH.19` | HIGH |
| `kbin_cce_op_to_sdma_cce_op` | `0x2664c0` | 96 B | kbin→SDMA CCE-op remap; `CSWTCH.21` | HIGH |
| `kbin_get_dtype_size` | `0x266520` | 73 B | dtype→size via translator + `data_type_sizes.0` | HIGH |
| `str_to_dtype` (inlined) | in `0xe1710` | — | NEFF I/O dtype string → `nrt_dtype_t` (smaller set) | HIGH |
| `str_to_dtype_map` (builder) | `0x78d80` | — | def.json dtype string → `kbin_dtype` (19-entry `std::map`) | HIGH |
| `is_hw_supported_type` | `0x4ae170` | — | gate for packed `_x4` dtype strings before container remap | MEDIUM |
| `CSWTCH.19` | `0x9d6e20` | 60 B | kbin→SDMA dtype table (15×u32) | HIGH |
| `CSWTCH.21` | `0x9d6e00` | 16 B | kbin→SDMA CCE-op table (4×u32) | HIGH |
| `data_type_sizes.0` | `0x9d6e60` | 64 B | SDMA-dtype→byte-size table (16×u32) | HIGH |

---

## 5. The FP8 Cast Config (Boundary)

### Purpose

FP8 is the one dtype family whose *numeric behavior* is not fixed by its enum value: the dynamic range (`emax`) and overflow policy (saturation) are programmed separately. The KBIN layer carries that policy as `kbin_fp8_conv_cfg_t`, parsed from the `def.json` `fp8_conversion_config` object. This page owns the dtype-system view of *where the config lives*; the bit-level encoder that turns it into device CSR writes is owned by the [FP8 Encoding](../isa/fp8-dtype-encoding.md) page and is not re-derived here.

### Encoding

`kbin_fp8_conv_cfg_t` (16 B, DWARF-confirmed) is parsed by `parse_fp8_conversion_config` into `mla_resources.FP8_CONV_CFG` (the 20-byte `fp8_conversion_info` wrapper `{initialized@0, cfg@4}`), then lowered into `kbin+400`:

| Field | Offset | Type | def.json key | Env override |
|---|---|---|---|---|
| `fp8_range_extension` | `+0` | `bool` | `"fp8_range_extension"` | `NEURON_RT_DBG_FP8_RANGE_EXT` |
| `emax_fp8_e4m3` | `+4` | `uint32` | `"emax_fp8_e4m3"` | `NEURON_RT_DBG_FP8_EMAX_E4M3` |
| `emax_fp8_e5m2` | `+8` | `uint32` | `"emax_fp8_e5m2"` | `NEURON_RT_DBG_FP8_EMAX_E5M2` |
| `ocp_sat` | `+12` | `bool` | `"ocp_sat"` | `NEURON_RT_DBG_OCP_FP8_SAT` |
| `sis` | `+13` | `bool` | `"fp8_sis"` | `NEURON_RT_DBG_FP8_SIS` |
| `sns` | `+14` | `bool` | `"fp8_sns"` | `NEURON_RT_DBG_FP8_SNS` |

> **NOTE —** the per-format `emax` values exist only for E4M3 and E5M2; E3M4 (`FP8_E3`) has no dedicated range field and follows the global `fp8_range_extension`/`ocp_sat` policy. This matches the encoder's behavior on the [FP8 page](../isa/fp8-dtype-encoding.md), where only the DVE engine consumes the two `emax` fields. The `initialized@0` flag in the 20-byte wrapper is set to `0` at `mla_resources` init and flipped when the config is parsed, so an absent `fp8_conversion_config` leaves FP8 conversion un-programmed (default policy).

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_dtype_t` | the NEFF I/O-tensor dtype enum; consumed to build public `nrt_get_model_tensor_info` descriptors |
| `kbin_dtype` | the KBIN/`def.json` container enum — the spine this page's translators pivot on |
| `SDMA_DTYPE` / `NEURON_ISA_TPB_DTYPE` | the wire dtype the compute and DMA paths share (FP8 = `13/14/15`) |
| `kbin_dtype_to_sdma_dtype` / `kbin_cce_op_to_sdma_cce_op` | the two staging-time translators a DMA/CCE builder must reproduce |
| `kbin_fp8_conv_cfg_t` | the 16-byte FP8 cast-policy struct, parsed here and encoded by the ISA-layer FP8 encoder |

## Cross-References

- [FP8 and dtype Encoding](../isa/fp8-dtype-encoding.md) — the instruction-field bit detail: the `NEURON_ISA_TPB_DTYPE` field inside the 64-byte word, the FP8 format table (E3M4/E4M3/E5M2), and the `insert_set_fp8_conv_config` enablement encoder. This page owns the higher-level enums and the kbin↔SDMA/ISA remap; that page owns the bits.
- [NEFF Metadata Schema (simdjson-DOM Consumed Keys)](metadata-schema.md) — the `def.json` `var`/`desc` keys whose `dtype`/`_dtype` strings feed `str_to_dtype_map`, and the `fp8_conversion_config` object that fills `kbin_fp8_conv_cfg_t`
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the SDMA m2m descriptor whose `meta_ctrl` word carries the translated `SDMA_DTYPE` and `SDMA_CCETYPE`, and the full `kbin_dma_desc_op` set (`COPY`/`TRANSPOSE`) the CCE-op remap excludes
