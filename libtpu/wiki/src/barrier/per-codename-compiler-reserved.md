# Per-Codename `compiler_reserved` SFLAG Integers

> *All addresses, file offsets, and integers on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; 781,691,048 B; ships with full C++ symbols). `.text`/`.lrodata`/`.rodata` VMA == file offset; `.data.rel.ro` uses a `−0x200000` file-offset delta and `filewrapper_toc` a `−0x400000` delta. Other versions will differ.*

## Abstract

The two integers every TensorCore barrier formula is parametric in — `base` (`Target+0x8c0`) and `count` (`Target+0x8c4`) — are not constants in `.text`. They are read at target construction from a per-core-type **`compiler_reserved`** repeated-int32 range carried in the chip-config proto, one `(codename, deployment)` blob per silicon generation. The general formulas that consume `base`/`count` live on [Barrier → SFLAG Binding](barrier-to-sflag-binding.md); the proto carrier, its `FromProto` sink, and the `EnumMap` element layout live on [SpecialPurposeSyncFlags](special-purpose-sync-flags.md); the normaliser that pins `REPLICA` to `count − 1` lives on [InferBarrierConfig](infer-barrier-config.md). **This page owns the literal per-codename integers** — the actual `compiler_reserved` `{base, count}` numbers per generation (jellyfish/dragonfish/pufferfish/viperfish/ghostlite/6acc60406), where those numbers are physically stored in the binary, and how `Target::Init` / `SparseCoreTarget::Init` resolve them into the two `int32` struct scalars.

The sibling barrier pages mark these integers **LOW** ("an embedded-memfile dependency / not statically extractable"). **They are in fact statically extractable.** The chip-config blobs are *compiled into* `.lrodata`/`.rodata`; the so-called "memfiles" are not runtime-loaded — they are populated by load-time `R_X86_64_RELATIVE` relocations that point at compiled-in data. Every per-gen integer below is carved directly from the embedded blob bytes and cross-checked against the writer disassembly. The numbers are CONFIRMED, byte-exact.

For reimplementation, the contract is:

- **The TC carve is gen-shaped, not gen-keyed.** `compiler_reserved(kTensorCore)` is an ascending-contiguous block that **always starts at `base = 8`**; only the length varies — `17` ints on JF/DF (`count = 12`), `43` ints on PF/VF/GL/GF (`count = 38`). The `−5` that produces `count` is a hard-coded literal, gen-independent.
- **The SC carve is constant where present.** SparseCore first carries a `SpecialPurposeSyncFlags` at Viperfish; from VF onward `compiler_reserved(kSparseCore)` is `[7055..7154]` (`base = 7055`, `count = 100`, **no `−5`**), with four named scalars pinned immediately above (`7155`/`7156`/`7157`/`7167`).
- **The TC `sequencer_overlay` scalar is the only per-gen *value* that steps:** `254` → `511` → `4095` (`0xFE`/`0x1FF`/`0xFFF`, the `2^n − 1` form with `n = 8/9/12`).
- **The storage is `.lrodata`/`.rodata` blobs + `.data.rel.ro` `FileWrapper` descriptors**, reachable through the 9 `tpu_chip_config_memfile_<deployment>_embed_internal_create` stubs (filewrapper_toc slots 39–47) and a superset of 47 descriptors at `.data.rel.ro 0x2200e350`.

| | |
|---|---|
| **Owns** | the literal per-codename `compiler_reserved` `{base, count}` integers + their static storage + the `Init` resolution |
| **TC carve** | `Target::Init` @`0x1d60fc20` → `base = CR_TC[0]` @`Target+0x8c0`, `count = \|CR_TC\| − 5` @`Target+0x8c4` |
| **SC carve** | `SparseCoreTarget::Init` @`0x1d612b20` → `base = CR_SC[0]` @`+0x1d0`, `count = \|CR_SC\|` @`+0x1d4` (no `−5`) |
| **TC base (all gens)** | `8` |
| **TC count** | `12` (JF/DF), `38` (PF/VF/GL/GF) |
| **SC base / count** | `7055` / `100` (VF/GL/GF; absent JF/DF/PF) |
| **TC `sequencer_overlay`** | `254` (JF/DF) / `511` (PF/VF/GL) / `4095` (GF) |
| **Proto carrier** | `TpuChipConfigProto.special_purpose_sync_flags` (field 13) → `compiler_reserved` (field 3, repeated int32) |
| **Storage** | `*_chip_configs.binarypb` blobs in `.lrodata`/`.rodata`; `FileWrapper` descriptors @`.data.rel.ro 0x2200e350` |
| **Evidence grade** | CONFIRMED (carved from embedded blob bytes + writer disassembly; corrects sibling-page LOW) |

---

## 1. The per-generation SFLAG memory map

This is the authoritative table. Every cell was decoded from the embedded blob bytes (§4) and is consistent with the `Target::Init` / `SparseCoreTarget::Init` writers (§3). The `TpuVersion` proto-wire value (field 1 of each blob) names the generation; the runtime enum is wire `− 1` (`TpuVersionFromProto`).

| Gen | Codename | Proto ver | TC `cr` range (raw) | TC `base` | TC `count` (`−5`) | TC usable window `[base, base+count)` | TC reserved top-5 (mega/gap/AR1/AR2/global) | TC `sequencer_overlay` | SC `cr` range | SC `base` | SC `count` | SC local/global/seq/tile |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| JF | jellyfish | 1 | `[8..24]` (17) | `8` | `12` | `[8..19]` | `20 / 21 / 22 / 23 / 24` | `254` (`0xFE`) | — (no SC) | — | — | — |
| DF | dragonfish | 2 | `[8..24]` (17) | `8` | `12` | `[8..19]` | `20 / 21 / 22 / 23 / 24` | `254` (`0xFE`) | — (no SC) | — | — | — |
| PF | pufferfish | 3 | `[8..50]` (43) | `8` | `38` | `[8..45]` | `46 / 47 / 48 / 49 / 50` | `511` (`0x1FF`) | — (no SC) | — | — | — |
| VF | viperfish | 4 | `[8..50]` (43) | `8` | `38` | `[8..45]` | `46 / 47 / 48 / 49 / 50` | `511` (`0x1FF`) | `[7055..7154]` (100) | `7055` | `100` | `7155 / 7156 / 7157 / 7167` |
| GL | ghostlite | 5 | `[8..50]` (43) | `8` | `38` | `[8..45]` | `46 / 47 / 48 / 49 / 50` | `511` (`0x1FF`) | `[7055..7154]` (100) | `7055` | `100` | `7155 / 7156 / 7157 / 7167` |
| GF | 6acc60406 | 6 | `[8..50]` (43) | `8` | `38` | `[8..45]` | `46 / 47 / 48 / 49 / 50` | `4095` (`0xFFF`) | `[7055..7154]` (100) | `7055` | `100` | `7155 / 7156 / 7157 / 7167` |

Reads a reimplementer must carry from this:

- **TC `base` is `8` for every generation.** The `compiler_reserved(kTensorCore)` range is ascending-contiguous and always anchored at `8`; the per-gen variable is the *length* (`17` → `43` at the JF/DF → PF boundary). The `−5` carve (§3) is the same literal across all gens.
- **The GlobalBarrier slot equals the last `cr` element exactly.** `base + count + 4` = JF/DF `24`, PF/VF/GL/GF `50` — which is precisely `CR_TC[-1]`. The five named top slots ARE the top five ints of the contiguous range; the usable per-id window is the bottom `count`. The [SFLAG binding](barrier-to-sflag-binding.md) `base+count+{0,2,3,4}` formulas now resolve to concrete numbers (e.g. GF global barrier `= 50`, megacore `= 46`, AR(1) `= 48`, AR(2) `= 49`; JF/DF global barrier `= 24`, megacore `= 20`).
- **`sequencer_overlay` is a separate SFLAG scalar above the TC range, and the only value that steps per gen** (`254`/`511`/`4095`). It is a single SFLAG *index*, not a bitmask — see [SpecialPurposeSyncFlags §5.3](special-purpose-sync-flags.md). The `2^n − 1` form (`n = 8/9/12`) tracks the per-gen SFLAG-number encoding width, not a mask.
- **SparseCore `compiler_reserved` first appears at Viperfish (v4)** and is constant `[7055..7154]` thereafter. The SC `count` is taken FULL (no `−5`); the SC global/local barriers are the named scalars `7156`/`7155`, not a top-of-range reservation. No generation carries a BarnaCore (`core_type = 2`) `SpecialPurposeSyncFlags` entry.

> **NOTE — this page is authoritative for the integers.** [Barrier → SFLAG Binding](barrier-to-sflag-binding.md) and [SpecialPurposeSyncFlags](special-purpose-sync-flags.md) mark these literals LOW because they document only the *window geometry* (the carve, the `−5`, the five-slot map — all gen-independent and CONFIRMED there) and treat the per-`(codename, deployment)` integers as a memfile dependency. Those blobs are statically compiled in (§2) and the integers are CONFIRMED byte-exact here (§4), so use this page for the concrete numbers.

---

## 2. Where the integers are stored — statically embedded, not runtime-loaded

The integers live inside `*_chip_configs.binarypb` blobs that are compiled into the read-only data segments. The "memfile" abstraction is a static-embed wrapper, not a filesystem or network path.

### 2.1 The 9 deployment-name memfile stubs → `filewrapper_toc` slots 39–47

Each `tpu_chip_config_memfile_<deployment>_embed_internal_create()` is a two-instruction stub (`mov rax,[rip+disp]; ret`) that returns a `toc_ptr` global inside `filewrapper_toc` (VMA `0x224bf798`). The decompiled `default` stub is literally:

```c
// tpu_chip_config_memfile_default_embed_internal_create @0x20b18fa0
char **tpu_chip_config_memfile_default_embed_internal_create() {
    return toc_ptr[0];          // a filewrapper_toc slot pointer
}
```

The slot index is `(toc_ptr − 0x224bf798) / 8`; the slot descriptor carries the blob's data pointer (`desc+8`, a relative reloc) and size (`desc+0x10`, a raw `u64`). The deployment → slot → blob → codename map for this build:

| Memfile deployment | `create` @ | `toc_ptr` | slot | blob `*_chip_configs_*` | ver | size |
|---|---|---|---|---|---|---|
| `default` | `0x20b18fa0` | `0x224bf8d0` | 39 | `6acc60406_tensornode_chip_configs_default` | 6 | 1309 |
| `inference` | `0x20b18fc0` | `0x224bf8d8` | 40 | `6acc60406_chip_configs_inference` | 6 | 1481 |
| `legacy` | `0x20b18fe0` | `0x224bf8e0` | 41 | `6acc60406_tensornode_chip_configs_legacy` | 6 | 1190 |
| `legacy_dense` | `0x20b19000` | `0x224bf8e8` | 42 | `pufferfish_chip_configs_legacy_dense` | 3 | 870 |
| `megacore` | `0x20b19020` | `0x224bf8f0` | 43 | `viperfish_glp_emulation_chip_configs_megacore` | 4 | 985 |
| `megacore_dense` | `0x20b19040` | `0x224bf8f8` | 44 | `pufferfish_chip_configs_megacore_dense` | 3 | 819 |
| `megacore_inference` | `0x20b19060` | `0x224bf900` | 45 | `pufferfish_chip_configs_megacore_inference` | 3 | 786 |
| `megachip` | `0x20b19080` | `0x224bf908` | 46 | `viperfish_chip_configs_megacore` | 4 | 1458 |
| `legacy_sparse_core` | `0x20b190a0` | `0x224bf910` | 47 | `6acc60406_tensornode_chip_configs_legacy_sparse_core` | 6 | 1174 |

The memfile name is the *deployment* name; the blob basename embeds the *codename*. In this build the 9 memfiles bind `{6acc60406 ×4, pufferfish ×3, viperfish ×2}` — JF/DF/GL deployments are not surfaced through these stubs.

### 2.2 The full 47-descriptor / 35-unique-blob registry

The 9-memfile TOC is only a subset. A superset of **47 `FileWrapper` descriptors** (40-byte stride `{name@+0, data@+8, size@+0x10, fp@+0x18}`) is packed contiguously at `.data.rel.ro 0x2200e350..0x2200ec00`, with `data`/`name` as `R_X86_64_RELATIVE` relocs into the chip-config data clusters (`.lrodata 0x5f01400..`, `.rodata 0xbdc6000../0xbded800..`) and `size`/`fp` as raw bytes in the `.data.rel.ro` file image. Walking it yields **35 unique `(codename, deployment)` blobs**, distributed JF 2 / DF 2 / PF 9 / VF 11 / GL 4 / GF 7. So **every runtime `TpuVersion` has at least one embedded blob** — the literal SFLAG range is statically present for all six gens, not just the three surfaced via memfile stubs. (`47 > 35` is descriptor aliasing: several deployments alias the same data blob, e.g. `megachip` aliases the viperfish `megacore` blob.)

| Gen | Representative blob | `FileWrapper` desc | data VA (== file off) | size | `fp[:8]` |
|---|---|---|---|---|---|
| JF | `jellyfish_chip_configs_default` | `0x2200e4b8` | `0xbdee4c0` | 848 | `95d58454…` |
| DF | `dragonfish_chip_configs_default` | `0x2200e4e0` | `0xbdee820` | 918 | `ac2ac3da…` |
| PF | `pufferfish_chip_configs_legacy` | `0x2200e490` | `0xbdee0d0` | 996 | `7ff2f7f9…` |
| VF | `viperfish_chip_configs_megacore` | `0x2200ea10` | `0x5f05b70` | 1458 | `6306c936…` |
| GL | `ghostlite_chip_configs_inference` | `0x2200e558` | `0x5f03570` | 1263 | `3ce606ef…` |
| GF | `6acc60406_tensornode_chip_configs_default` | `0x2200e350` | `0x5f01460` | 1309 | `30c55c21…` |

> **NOTE — not runtime-loaded.** There is no `fopen`/`mmap`/embed-FS path for these blobs. They are compiled into `.lrodata`/`.rodata`; the descriptors are compiled into `.data.rel.ro`; the only "loading" is the load-time `R_X86_64_RELATIVE` relocation that fills the `name`/`data` pointers and the `toc_ptr` globals. `FLAGS_deepsea_chip_config_name` @`0x224714b0` selects the *deployment* name, and a `(TpuVersion, name, TpuCoreType)` flat-hash-map selects which embedded blob feeds `TpuChipConfig::FromProto` @`0x20aea100`. No default-fallback constant is needed — the literals **are** the embedded bytes.

---

## 3. How `Init` resolves the integers into `{base, count}`

Both carves read one `SpecialPurposeSyncFlags` element (via `GetSpecialPurposeSyncFlags(core)` — the parsing/access path is owned by [SpecialPurposeSyncFlags](special-purpose-sync-flags.md)) and copy two `int32`s onto the target object. The TC side subtracts five; the SC side does not.

### 3.1 TensorCore — `Target::Init` (`size − 5`)

```c
// Target::Init @0x1d60fc20 — TC compiler_reserved carve (lines 1969-2068, byte-exact)
SpecialPurposeSyncFlags = GetSpecialPurposeSyncFlags(kTensorCore);   // line 1969
if (!SpecialPurposeSyncFlags)
    DieBecauseNull("chip_config.GetSpecialPurposeSyncFlags("
                   "::tpu::TpuCoreType::kTensorCore)");               // line 1971 — TC entry mandatory

size = element->compiler_reserved.size();                            // v284
data = element->compiler_reserved.data();                            // v286
for (i = 1; i < size; ++i)                                           // contiguity assertion
    CHECK(data[i] == data[i-1] + 1,
          "compiler_reserved_tensor_core_sync_flags[i] =="
          " compiler_reserved_tensor_core_sync_flags[i - 1] + 1");   // line 2018

*((_DWORD *)target + 560) = data[0];   // base  = CR_TC[0]   @Target+0x8c0   line 2067
*((_DWORD *)target + 561) = size - 5;  // count = |CR_TC|-5  @Target+0x8c4   line 2068
```

`data[0]` is `8` for every gen, so `base = 8` always. `size − 5` is `12` (JF/DF, `17 − 5`) or `38` (PF+, `43 − 5`). The contiguity CHECK guarantees `base + index` *is* the SFLAG number, which is why the [binding formulas](barrier-to-sflag-binding.md) are pure arithmetic.

### 3.2 SparseCore — `SparseCoreTarget::Init` (no `−5`)

```c
// SparseCoreTarget::Init @0x1d612b20 — SC compiler_reserved carve (lines 546-547, byte-exact)
SpecialPurposeSyncFlags = GetSpecialPurposeSyncFlags(kSparseCore);          // line 454
*(_DWORD *)(sc_target + 464) = *(_DWORD *)SpecialPurposeSyncFlags[1];       // base  = CR_SC[0]   @+0x1d0  line 546
*(_DWORD *)(sc_target + 468) = *((_DWORD *)SpecialPurposeSyncFlags + 4);    // count = |CR_SC|    @+0x1d4  line 547
// then the four named scalars, each gated by & 0x100000000 (presence bit-32):
//   tile_overlay  -> +488 (0x1e8) ;  sequencer_overlay -> +512 (0x200)
//   global_barrier-> +516 (0x204) ;  local_barrier     -> +520 (0x208)
```

`SC base = 7055`, `SC count = 100` — taken FULL. The four scalars (`7167`/`7157`/`7156`/`7155`) are stored into `SparseCoreTarget` fields only when bit 32 of the packed element qword is set; their consumers are SparseCore-only (see [SpecialPurposeSyncFlags §5.2](special-purpose-sync-flags.md)).

> **GOTCHA —** the `−5` lives only on the TC path. A reimplementation that applies `−5` to the SC range would shrink the usable SC barrier-id window by five and corrupt the SC tree-barrier id space; the SC global/local barriers are the named scalars (`7156`/`7155`), not a top-of-range reservation.

---

## 4. Confirming the integers against the embedded bytes

Each per-gen integer was carved directly from the blob at its file offset (`.lrodata`/`.rodata` VMA == file offset). The TC `compiler_reserved` is a packed-int32 field-3 (each value `< 128` is a single varint byte, so `[8..24]`/`[8..50]` appears as the literal byte sequence `08 09 … 18` / `08 … 32`); `sequencer_overlay` is field 4 (tag `0x20`) as a LE varint; the SC range `[7055..7154]` is packed two-byte LE varints. Findings, per gen:

| Gen | Blob @ file off | TC `cr` byte run | `seq_overlay` tag bytes | SC `cr` packed run |
|---|---|---|---|---|
| JF | `0xbdee4c0` (848) | `08..18` (`[8..24]`) present | `20 FE 01` (`254`) present | — |
| DF | `0xbdee820` (918) | `08..18` (`[8..24]`) present | `20 FE 01` (`254`) present | — |
| PF | `0xbdee0d0` (996) | `08..32` (`[8..50]`) present | `20 FF 03` (`511`) present | — |
| VF | `0x5f05b70` (1458) | `08..32` (`[8..50]`) present | `20 FF 03` (`511`) present | `[7055..7154]` present |
| GL | `0x5f03570` (1263) | `08..32` (`[8..50]`) present | `20 FF 03` (`511`) present | `[7055..7154]` present |
| GF | `0x5f01460` (1309) | `08..32` (`[8..50]`) present | `20 FF 1F` (`4095`) present | `[7055..7154]` present; scalars `F3 37`/`F4 37`/`F5 37`/`FF 37` (`7155`/`7156`/`7157`/`7167`) all present |

The carved ranges are exactly the inputs the `Init` writers (§3) copy. Three independent decoders agree: a hand-written packed-varint decoder, `protoc --decode_raw` of each blob (field 13 visible with packed `compiler_reserved` + scalars), and the writer disassembly. The blob top-level is `TpuChipConfigProto` (field 12 `memory_reservations`, field 13 `special_purpose_sync_flags` present); the `SpecialPurposeSyncFlags` field schema (`f1 core_type` enum `.tpu.TpuCoreTypeProto`, `f3 compiler_reserved` repeated int32, `f4`–`f7` scalars) is re-decoded from the embedded descriptor and matches [SpecialPurposeSyncFlags §1](special-purpose-sync-flags.md).

---

## 5. Deployment-level variance and edge cases

Across all 35 unique blobs the TC `cr` range and TC `sequencer_overlay` depend ONLY on the generation, not on the deployment name (`default`/`inference`/`legacy`/`legacy_dense`/`megacore`/`megacore_dense`/`megacore_inference`/`megachip`/`legacy_sparse_core`/`lite`). Two deployment-level facts a reimplementer should not over-generalise away:

- **The `viperfish_glp_emulation_chip_configs_*` blobs carry an empty-SC edge case.** Five GLP-emulation blobs (`legacy`/`legacy_sparse_core`/`megachip`/`megachip_tccontrol`/`megacore`) carry an SC `SpecialPurposeSyncFlags` with **empty `compiler_reserved` (`count = 0`)** but the four named scalars STILL set (`7155`/`7156`/`7157`/`7167`). The GLP-emulation megacore SC therefore has no usable barrier-id range (`SparseCoreTarget+0x1d4 == 0`) yet retains its overlay/barrier scalar numbers.
- **`pufferfish_lite` / `viperfish_lite` reuse the parent-gen TC range** (lite is a PF-/VF-version with no SC). `megachip` (deployment) aliases the `viperfish_chip_configs_megacore` blob, so `megachip` on this build is a VF-megacore config with the full SC `[7055..7154]`.

> **QUIRK —** because all 35 embedded blobs of a given gen share the same TC `cr` + `seq_overlay` (and SC where present), the per-gen integers are deployment-invariant *within this build*. A future libtpu could in principle ship a deployment with a different `cr` length — that is not testable from this binary alone — but nothing in this build varies the SFLAG range by deployment.

---

## 6. What remains LOW / open

> The per-gen `compiler_reserved` `{base, count}` integers (TC `base = 8` all gens, `count = 12`/`38`; SC `base = 7055`, `count = 100`), the TC `sequencer_overlay` values (`254`/`511`/`4095`), the SC named scalars (`7155`/`7156`/`7157`/`7167`), the static-embed storage (`.lrodata`/`.rodata` blobs + `.data.rel.ro` descriptors + memfile-stub TOC slots 39–47), and the `Target::Init` (`size − 5`) / `SparseCoreTarget::Init` (no `−5`) resolution are CONFIRMED — carved from the embedded blob bytes and matched against the writer disassembly. These are the authoritative integers (the sibling pages mark them LOW only because they do not carve the blobs).

> **[LOW] Open items, scoped out of this page:**
> - **The runtime placement of the four named scalars** inside the `EnumMap` element (`+0x20..+0x3c`) and whether `FromProto` consumes any on the spot — owned by [SpecialPurposeSyncFlags](special-purpose-sync-flags.md); the proto *values* are CONFIRMED here, the element-offset reads there.
> - **Why `sequencer_overlay` steps `254 → 511 → 4095`.** The `2^n − 1` form (`n = 8/9/12`) is attributed to the per-gen SFLAG-number encoding width, not read as a single binary literal; the index-not-bitmask conclusion is CONFIRMED ([SpecialPurposeSyncFlags §5.3](special-purpose-sync-flags.md)), the width inferred.
> - **Cross-build reachability:** this build embeds 35 blobs but surfaces only 9 via memfiles. Which `(codename, deployment, core)` tuples a running compiler can actually select (i.e. whether the JF/DF blobs are live or dead) depends on the `(TpuVersion, name, TpuCoreType)` map population, not traced here.

---

## Cross-References

**Barrier algorithms (this section)**
- [Barriers and Sync-Flags — Section Map](overview.md) — the subsystem map; §5 summarises the SFLAG window and these integers
- [Barrier → SFLAG Binding](barrier-to-sflag-binding.md) — the `base+count+{0,2,3,4}` formulas these integers parameterise; resolves to concrete numbers with the §1 table
- [SpecialPurposeSyncFlags](special-purpose-sync-flags.md) — the proto carrier, the `FromProto` sink, the `EnumMap` element, and the four named scalars' element offsets
- [InferBarrierConfig](infer-barrier-config.md) — the normaliser that pins `REPLICA` to `id = count − 1` (top usable TC id) and `GLOBAL` to `id = −1`
- [Barrier Coloring](barrier-coloring.md) — the engine that assigns the per-key `CUSTOM` ids that index *into* `[base, base+count)`
- [Global-Barrier Window](global-barrier-window.md) — the Mosaic func-level TC tree barrier; the one "global" path that does **not** read `base+count+4`
- [Per-Gen Remote-SFLAG Encoders](remote-sflag-encoders.md) — the cross-chip side of the SFLAG number space

**Sibling subsystems**
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the on-chip atomic-counter substrate every SFLAG number indexes
- [back to index](../index.md)
