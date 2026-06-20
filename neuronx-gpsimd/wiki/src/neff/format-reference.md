# The NEFF Format Reference

> **Scope.** This is the **consolidated, publishable specification** of the *Neuron
> Executable File Format* (NEFF) — the model container produced by `neuronx-cc` and loaded
> by `libnrt.so`'s `nrt_load` path. It is the document a reimplementer uses to **read or
> write** a NEFF, and to understand how a **GPSIMD custom op** — its per-engine sequencer
> microcode, its weights, its relocation/patch tables, its version gates, and its
> Vision-Q7 device kernel — is **packed, validated, relocated, and placed on-device**. It
> synthesizes the per-topic sibling pages of this Part: the
> [container byte format](./container-byte-format.md), the
> [metaneff I/O ABI](./metaneff-io-abi.md), the
> [sequencer microcode](./seq-microcode.md), the
> [relocation / weight subsystem](./relocation-weights.md), the
> [assembly pipeline](./assembly-pipeline.md), the
> [version / compatibility model](./version-compat.md), the byte-by-byte
> [concrete carve](./concrete-carve.md), the
> [NEFF ↔ device-ELF relationship](./neff-elf-relationship.md), the
> [ntff trace + parse-state](./ntff-trace-parse-state.md) page, and the
> [container capstone](./container-capstone.md). Every cited anchor here is
> **re-grounded against the binary** in this pass; nothing is taken on a sibling's word
> alone.

**Tag legend.** Each claim carries `[CONF × PROV]`: confidence `HIGH/MED/LOW` × provenance
`OBSERVED` (bytes/struct/string/enum read this pass from a binary, an IDA sidecar, a
`STATIC_ASSERT`ed shipped header, or the carved fixture), `INFERRED` (decompiled
control/data flow), `CARRIED` (consolidated from a sibling page, re-grounded here). **v2–v4
(SUNDA/CAYMAN/MARIANA) facts are byte-grounded**; **v5 / MAVERICK is header-OBSERVED only**
and every v5 *interior* claim is flagged `INFERRED`.

**Binaries of record.**

| binary | build | role |
|:---|:---|:---|
| `libnrt.so.2.31.24.0` | `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, x86-64, RTTI+DWARF, 122 956 336 B (`0x7542A30`) | the **host loader** (`nrt_load` / `neff_parse` / `kelf_load_from_neff`); also carries the one embedded NEFF fixture |
| `libnrtucode_internal.so` | `aws-neuronx-gpsimd-customop-lib 0.21.2.0`, x86-64, not stripped, 10 276 288 B | the **GPSIMD device-code** layer: the host prelinker + the 16 resident Vision-Q7 ELF blobs |
| `libtorchneuron.so` | `torch_neuronx-2.9.0.2.13.24727`, RTTI | the **metaneff** host key-ring (`Model`/`SPMDModel`) |

In `libnrt.so` this build has **no VMA↔file-offset delta on any section** (`readelf -SW`:
`.text`@`0x3dbc0`, `.rodata`@`0x7cf000`, `.data`@`0xc07e00` all satisfy VMA==fileoffset), so
every address below is a direct seek. The host `libnrtucode_internal.so` has per-section
deltas (`.text` Δ`0x1000`, `.data` Δ`0x3000`) — its `.rodata` blob offsets are host file
offsets; the **device** ELF inside it has its own VMA scheme (`.text`@`0x01000000`,
data@`0x02000000`, dynamic@`0x03000000`). Never mix the three address worlds.

---

## 0. The NEFF at a glance — three artifacts, two-level container  `[HIGH × OBSERVED]`

A loadable model is **three** artifacts, only the first of which is the NEFF:

1. **The NEFF** — a flat **1024-byte C-struct header** glued to an inner archive. It carries
   the device program (per-engine sequencer microcode), the **device-side** var/`mem_ref`
   I/O table, the DMA rings, the relocation seeds, the weight/constant payloads, and (for a
   custom op) the GPSIMD device-code library. *This document.*
2. **The metaneff** — a **separate protobuf string**, the **host-side** I/O key-ring (which
   `at::Tensor` fills which slot, dtype/shape, output→input aliasing). It is **not** inside
   the NEFF tar; it is handed alongside the NEFF bytes to `torch.classes.neuron.{Model,
   SPMDModel}` ([metaneff-io-abi](./metaneff-io-abi.md)).
3. **The weight bins** — constant tensors. These **do** ride inside the NEFF tar, named by
   the var table ([relocation-weights §7](./relocation-weights.md)). metaneff carries **no**
   weights.

The container is two regions, identified **structurally — there is no literal magic
number** ([version-compat §1](./version-compat.md)):

```
file off 0x000   [ 1024-byte neff_header_t ]
file off 0x400   [ data_size bytes of inner archive (begins at header_size offset) ]
```

> **The single fixture.** One real `pkg2` NEFF exists in the corpus — embedded in
> `libnrt.so`'s `.data` (a built-in default/test model). Header at file off **`0xC07E20`**,
> gzip inner stream at **`0xC08220`** (`= 0xC07E20 + 0x400`). It is a **minimal toy model**:
> one all-reduce of a 32-element fp32 gradient — **no constant weights, no custom-op ucode
> library**. So the weight-payload and ucode-lib sections of the format are carved
> *elsewhere* (the ELF blobs in `libnrtucode_internal.so`, §6), not in this instance. The
> byte-by-byte carve of the fixture is [concrete-carve](./concrete-carve.md); this page
> abstracts it. `[HIGH × OBSERVED]`

---

## 1. The container — the 1024-byte `neff_header_t` (byte-layout map)  `[HIGH × OBSERVED]`

IDA ordinal **5896**, `size == 1024` (verified `structures.json`). Every offset below is
exact; the `[bracketed]` values are the embedded fixture's bytes, re-read this pass at
`0xC07E20` (`xxd -s 0xC07E20`):

| off | C type | field | meaning · fixture value |
|---:|:---|:---|:---|
| `+0x000` | `u64` | `pkg_version` | `1` = raw-tar + SHA-256 ; `2` = gzip-tar + MD5 · **[=2]** |
| `+0x008` | `u64` | `header_size` | `== 0x400` in practice; validated `< file_size` · **[=0x400]** |
| `+0x010` | `u64` | `data_size` | inner-archive **compressed** byte count; `<= file_size − 0x400` · **[=0x6AD]** |
| `+0x018` | `u64` | `neff_version_major` | hard ceiling **2** (`> 2` ⇒ refuse) · **[=2]** |
| `+0x020` | `u64` | `neff_version_minor` | **not** range-checked (informational) · [=0] |
| `+0x028` | `u8[128]` | `neff_build_version` | ASCII label, never parsed · **[="2.0.21884.0%kaena-tools/develop@6c66f4b"]** |
| `+0x0A8` | `u32` | `unused_0` | producer "data is gzip" flag (`1` = gzip) · **[=1]** |
| `+0x0AC` | `u8[32]` | `hash` | `pkg2`: 16-B MD5 in `[0..15]` ; `pkg1`: 32-B SHA-256 (all 32) · **[=MD5…]** |
| `+0x0CC` | `u8[16]` | `uuid` | RFC-4122 v4 model UUID · **[=561826fb-125f-4bb8-991c-29e085c8949a]** |
| `+0x0DC` | `char[256]` | `name` | model/test name · **[="x"]** |
| `+0x1DC` | `u32` | `requested_tpb_count` | TPBs requested (`1` = single core) · **[=1]** |
| `+0x1E0` | `u8[64]` | `tpb_per_node` | per-NUMA-node TPB counts · [all 0] |
| `+0x220` | `u64` | `feature_bits` | forward-compat capability mask (§7.1) · **[=0]** |
| `+0x228` | `u32` | `vnc_size` | virtual-NeuronCore size (`1`=single, `2`=dual-fuse) · **[=1]** |
| `+0x22C` | `u8[468]` | `pad` | reserved |
| `+0x400` | `u8[data_size]` | `data` | inner archive payload (gzip stream begins here for `pkg2`) |

Leading-qword re-read this pass (`xxd -s 0xC07E20`):

```
00c07e20: 0200…00  pkg_version=2     00c07e28: 0004…00  header_size=0x400
00c07e30: ad06…00  data_size=0x6AD   00c07e38: 0200…00  ver_major=2
00c07ecc: 61b3cab2 4369b326 a2b3ee40 a744c746   hash[0:16] = MD5
00c07edc: 3030…30  hash[16:31] = ASCII "0000…"   (see QUIRK)
00c07eec: 561826fb 125f4bb8 991c29e0 85c8949a   uuid
00c08040: 0000…00  feature_bits=0    00c08048: 0100…00  vnc_size=1
00c08220: 1f8b0800…  inner gzip stream  (== 0xC07E20 + 0x400)
```

> **QUIRK — `hash[16..31]` is the ASCII string `"0000…"`, not zero padding.** The `pkg2`
> digest is a 16-byte MD5 in `hash[0..15]`. The producer fills the 16 trailing bytes with
> the ASCII digit `'0'` (`0x30`), not `0x00`. A reader must take **exactly `hash[0..15]`**;
> a full-32-byte compare against a zero-extended MD5 fails. `[HIGH × OBSERVED]`

> **NOTE — `neff_build_version` is your most precise build fingerprint.** It is free-form
> `"<ver>%<branch>@<git-sha>"` (`"2.0.21884.0%kaena-tools/develop@6c66f4b"`) and is never
> parsed by the loader. Prefer it over the coarse `(major,minor)` compat pair for identity.
> `neff_version_minor` (`+0x20`) is accepted at **any** value — there is no minor gate;
> forward compatibility is delegated entirely to `feature_bits` (§7.1).
> `[HIGH × OBSERVED, version-compat §1]`

### 1.1 Structural front gate — `neff_get_header_from_buffer @0x4ca2c0`  `[HIGH × OBSERVED]`

The cheap pre-gate, returning the header pointer or `NULL` (re-grounded `addr=0x4ca2c0`,
IDA `functions.json`):

```c
// 0x4ca2c0  neff_header_t* neff_get_header_from_buffer(void* neff, size_t size)
if (neff == NULL)              return NULL;  // "Invalid NEFF buffer" @0x8497b6
if (size <= 0x3FF)             return NULL;  // a header is 0x400 B  "Invalid number of NEFF bytes…" @0x82d2d0
if (hdr->header_size /*+8*/ >= size) return NULL; // "Invalid NEFF file size hdr sz:…" @0x82d300
return (neff_header_t*)neff;                 // does NOT read any version field
```

A `NULL` return makes `neff_parse` bail with code **2** (`NRT_INVALID`). All three error
strings log under subsystem tag `"NEFF"`.

### 1.2 The two inner-archive formats (the `pkg_version` gate)  `[HIGH × OBSERVED]`

| property | `pkg_version 1` (legacy) | `pkg_version 2` (current) |
|:---|:---|:---|
| inner archive | raw **GNU ustar** tar | **gzip**-compressed **GNU ustar** tar |
| integrity digest | SHA-256 (32 B, all of `hash[]`) | MD5 (16 B, first 16 of `hash[]`) |
| `__assert_fail` line | `neff.cpp:0x34` (`sha256`) | `neff.cpp:0x40` (`md5`) |
| unsupported value | → `NRT_UNSUPPORTED_NEFF_VERSION (10)` "Unsupported NEFF packager" | |

> **CORRECTION (vs the inner-archive "POSIX-pax" label).** The carved fixture's member-0
> tar header is **GNU `ustar`**, *not* POSIX-pax: magic `ustar ` + two trailing **spaces**
> (`75 73 74 61 72 20 20` at member off `+0x100`, version ` \0`), GNU **base-256**
> `uid`/`gid` (high bit `0x80` set), and **zero** PAX/GNU-longname extension records
> (no `typeflag 'x'/'g'/'L'/'K'` anywhere). Re-confirmed this pass — inflating the fixture
> gzip yields a 20 480-B tar whose first member is `kelf-a.json` and whose `+0x100` reads
> `ustar  `. The `libarchive` reader (`archive_read_support_format_tar`) accepts the GNU
> dialect transparently, so the *parser* is dialect-agnostic; a NEFF **writer** should emit
> GNU `ustar` to match the producer byte-for-byte.
> `[HIGH × OBSERVED — concrete-carve §3]`

The gzip filter (`archive_read_support_filter_gzip`) is registered **unconditionally**; on
`pkg1` it is inert (no gzip member). The integrity digest is **unkeyed** (plain MD5/SHA-256
over the **compressed** payload — corruption detection, *not* authentication: a re-hashed
modified NEFF passes), and is computed **only** when `nrt_load`'s verify flag (`a3`) is set.

> **CORRECTION (re-grounded) — the `pkg2` MD5 is over the COMPRESSED gzip bytes.** Verified
> this pass by recomputation: `md5(data[0xC08220 : +0x6AD])` =
> **`61b3cab24369b326a2b3ee40a744c746`** = `header.hash[0:16]` (exact). The MD5 of the
> *inflated* 20 480-B tar is `0a677f86…` and does **not** match. So the digest input is the
> on-disk gzip member (`header.data` for `data_size` bytes), confirmed in the decompiled
> `neff_parse` (`MD5_Update(v12->data, v12->data_size)`). `[HIGH × OBSERVED — concrete-carve §2]`

---

## 2. The inner archive — `neff_parse → neff_t::files`  `[HIGH × OBSERVED]`

`neff_parse @0x4ca3f0` (re-grounded `addr=0x4ca3f0`) builds the in-memory filesystem with
**no temp files**:

```c
// 0x4ca3f0  neff_parse, after the version/feature/integrity gates (§7)
a = archive_read_new();
archive_read_support_format_tar(a);
archive_read_support_filter_gzip(a);                      // gzip for pkg2; inert for pkg1
archive_read_open_memory(a, hdr + 0x400, hdr->data_size); // the compressed window
while (archive_read_next_header(a, &e) != ARCHIVE_EOF) {
    if (archive_entry_filetype(e) == 0x4000) continue;    // AE_IFDIR: skip dir entries
    name = archive_entry_pathname(e);
    if (name.rfind("./") == 0) name.erase(0, 2);          // strip leading "./"
    if (name endswith 18-B "wavegraph-bin.json")          // memcmp vs 0x84986c → SKIP
        { archive_read_data_skip(a); continue; }
    sz = archive_entry_size(e); if (sz < 0) error;
    buf = malloc(sz); archive_read_data(a, buf, sz);       // private copy out of the stream
    files[name] = { buf, sz };                             // std::map<string, pair<void*,long>>
}
```

`neff_t` (ordinal 5894, size 48) is `{ std::map<string, pair<void*,long>> files; }`. The
`O(log n)` lookup every later "load file X" goes through is **`neff_get_file_content
@0x4cb670`**, returning `{buffer, size}` from the map node (payload at `node+0x40` /
`+0x48`).

> **CORRECTION (vs DX-NEFF-07 §2's "checksum" gloss).** The 18-byte suffix the loader skips
> is **`wavegraph-bin.json`** (the per-graph debug wavegraph IR — `memcmp` against the byte
> string at `0x84986c`), **not** `"…checksum"`. `def.json` still *references*
> `wavegraph-bin.json` under `debug_info.wavegraph`; in production builds the file is simply
> absent, and a reader must tolerate that dangling reference. `[HIGH × OBSERVED — container §3]`

**The inner-tar member catalogue** (paths OBSERVED in the fixture; roles per the parsers):

| member | role |
|:---|:---|
| `kelf-a.json` | top-level kelf descriptor: `graphs[]`, `version`, `target` (§3) |
| `neff.json` | TVM/NNVM host-graph manifest (one fused `tvm_op` / `__kelf` node) |
| `info.json` / `hlo_metrics.json` / `hlo_stats.json` | optional build/profiler metadata |
| `sgNN/` *(dir)* | sub-graph marker, one per NeuronCore program — **skipped** (`AE_IFDIR`) |
| `sgNN/def.json` | the **section catalog** (var table, dma_queue, engines, ucode_lib…) — §3 |
| `sgNN/{pe,act,pool,dve,sp}.{json,bin,asm}` | per-engine program triples (§4) |
| `sgNN/act_info.json` | optional activation/PWL side table (referenced by `act.json`) |
| `sgNN/wavegraph-bin.json` | optional pre-lowering IR (debug; **stripped** in prod) |
| `ucode_lib.json` | optional GPSIMD custom-op library manifest (§5) |
| `*-simout.npy` / weight bins | weight/constant payloads named by the var table (§6) |

`kelf-a.json` (fixture, verbatim): `{ "graphs":[{"definition":"sg00/def.json","name":"sg00"}],
"version":"0.5", "target":"*" }`. `target "*"` = architecture-neutral (already lowered).

---

## 3. The section catalog — `def.json` parsed in fixed key order  `[HIGH × OBSERVED]`

For each `graph` in `kelf-a.json` the runtime calls **`kelf_load_from_neff @0x4c0870`**
(re-grounded), loads `sgNN/def.json` via **simdjson** (haswell backend), and consumes its
keys **in this exact order** — the canonical section→parser map every sibling builds on:

| `def.json` key | `libnrt` parser / sink @ addr | doc |
|:---|:---|:---|
| `name` | graph-name string | — |
| `var { }` | **`parse_one_variable @0x4b36b0`** → `mem_ref[]` | §3.1 |
| *(post-`var`)* dense-`var_id` check | `max(var_id)+1 == map.size` invariant ("Unexpected var_ids set!") | §3.1 |
| *(post-`var`)* `num_outputs > 0` | "no Outputs" reject | — |
| `dma_queue { }` | **`parse_one_dma_ring @0x4b5f80`** → typed rings | §3.3 |
| `replica_groups [ ]` | `parse_replica_groups` (collectives topology) | — |
| `src_target_pairs [ ]` | `parse_src_target_pairs` (collectives routing) | — |
| *(if `arch_type == 2`)* default Q7 lib | **`ucode_get_q7_lib @0x2265a0`** → `ext_isa_ucode_lib_def` | §5 |
| `ucode_lib` *(→ file)* | **`parse_one_ucode_lib @0x4b1610`** | §5 |
| `runtime_statebuffer_reservation [ ]` | `parse_sb_carveouts` (EVTACCEL) | §7.3 |
| `cc_stream` / `num_streams` | `CCSTMI.num_streams` | — |
| *(fp8 block)* | `parse_fp8_conversion_config` | — |
| per-engine `<eng>.json: dma[]` | **`parse_one_dma_block @0x4bc620`** | §8 |
| per-engine `<eng>.json: instr→.bin` | **`parse_one_engine_instr @0x4b7e30`** → `load_bin_file @0x4ae500` | §4 |
| *(engine absent)* | empty placeholder `instr_set` + WARN "Engine %s not found … empty placeholder" @`0x82d050` | §4 |

The five engine names come from **`al_hal_tpb_get_tpb_eng_names @0x44bd00`** (which returns
`&NEURON_ENG_NAMES` @`0xc09600`), indexed by `al_hal_tpb_eng_type`: **PE=0, ACT=1, POOL=2,
DVE=3, SP=4 (MAX=5)** — these are exactly the `sg00/{pe,act,pool,dve,sp}` filename stems.

> **GOTCHA — two distinct `arch` numberings.** The `arch_type` tested above is the
> **software/HAL ordinal** `al_hal_tpb_arch_type` (`INVALID=0, _1=1, SUNDA=2, CAYMAN=3,
> MARIANA=4, NUM=5` — re-grounded from `enums.json`), **not** the hardware `arch_id`
> codename byte (`0x05`/`0x0c`/`0x14`/`0x1c`). `arch_type == 2` ⇒ inject the default Q7
> ExtISA lib; `arch_type == 3` (CAYMAN) is the EVTACCEL/legacy-NEFF arm. Do not conflate the
> scales. `[HIGH × OBSERVED — version-compat §5, container §5]`

### 3.1 The `var` table — the **device-side** I/O ABI (`parse_one_variable`)  `[HIGH × OBSERVED]`

`var{}` binds each named tensor to a dense `var_id` and a device `mem_ref`. JSON keys
include `var_id` (u32), `#transfer-type` (`"input"`/`"output"`/`"tmp-buf"`/`"state-buffer"`/
`"virtual"`/`"buffer"`/…), `dtype`, `shape`/`internal_shape`/`framework_shape`, `size`,
`alignment`, `backing_buf`/`backing_variable_off` (aliasing), `referenced_var_id`,
`file`/`file_name`/`prefix` (weight payload), `load_weight_bin`. The dense-`var_id`
invariant (`max var_id + 1 == map size`) is enforced after the loop.

`#transfer-type → kbin_mr_type` (re-grounded `enums.json`, 12 values):

```
MR_INVALID=0  MR_SB=1  MR_BUFFER_STAGED=2  MR_BUFFER=3  MR_TMP_BUF=4  MR_INPUT=5
MR_OUTPUT=6   MR_PTR=7  MR_VIRTUAL_TMP_BUF=8  MR_LIST=9  MR_PTR_TABLE=10  MR_REMOTE=11
```

Fixture `sg00/def.json` (OBSERVED): `input{input, var_id 0, size 128, elem 4, [1,1,1,32]}`;
`SB{state-buffer, var_id 1}`; `output{output, var_id 2, #file-name
"value_nn__output:0-simout.npy"}`; `gradient_in{tmp-buf, var_id 3}`; `gradient_out{tmp-buf,
var_id 4}`. The dense `0..4` set passes `check_var_ids`; `num_outputs == 1` passes the "no
Outputs" reject.

### 3.2 The metaneff — the **host-side** I/O key-ring  `[HIGH × OBSERVED, metaneff-io-abi]`

The metaneff is a **separate proto3 string** (package `metaneff`), **not** in the NEFF tar,
handed to `torch.classes.neuron.{Model,SPMDModel}(neff_bytes, metaneff_bytes, …)`. Schema
(re-confirmed from the native `_InternalParse`/`_InternalSerialize` bodies in
`libtorchneuron.so`, not a `.proto` file):

```
message MetaTensor  1 name(bytes)  2 shape(rep int64)  3 data_type(enum DataType)
                    4 content(bytes)  5 allow_dynamic_batch_size(bool)
                    6 checkpoint_key(bytes, proto3-opt)  7 type(enum Type)
                    8 user_input_key(bytes, proto3-opt)
  DataType: UNDEFINED=0 FLOAT=1 INT32=2 BYTE=3 STRING=4 BOOL=5 UINT8=6 INT8=7
            UINT16=8 INT16=9 INT64=10  <11 reserved>  FLOAT16=12 DOUBLE=13
            BFLOAT16=14 F8E4M3FN=15 F8E5M2=16
  Type:     UNDEFINED_TYPE=0 USER_INPUT=1 INPUT_STATE=2 INPUT_WEIGHT=3
message ModelConfig 1 num_infer  2 timeout  3 optimal_ncg_size  4 async_load(bool@+0x28)
                    5 lazy_load(bool@+0x29)  6 return_aliases(bool@+0x2a)
message MetaNeff    1 input_tensors(rep MetaTensor)  2 output_tensors(rep MetaTensor)
                    3 model_config  4 serialized_graph_def(HLO bytes)  5 name
                    6 output_aliases_to(map<i64,i64>)  7 num_user_inputs
                    8 num_states  9 num_weights
```

**The binding contract** `[HIGH]`: `MetaTensor` index `i` (inputs then outputs) **==** NEFF
`var_id i` **==** nrt tensor-set ordinal `i` **==** device `mem_ref[var_id i]`. The
`MetaTensor.name` `"input{i}"`/`"output{i}"` is the runtime `set_input`/`set_output` lookup
key; the dense `0..N-1` invariant is the **same** one the kelf var-table enforces.
`MetaTensor.type {USER_INPUT, INPUT_STATE, INPUT_WEIGHT}` partitions which device slots the
host fills from user args vs internal checkpoint state/weights; `output_aliases_to`
(`out_idx → in_idx`) is donated/in-place buffer reuse. **metaneff = host key-ring; var
table = device key-ring; the nrt-tensor ordinal is the shared index space.**

### 3.3 `dma_queue → parse_one_dma_ring` (typed rings)  `[HIGH × OBSERVED]`

Each named queue carries `owner` (engine name; `==5` invalid-sentinel reject),
`semaphore`/`semaphore_set`, and a string-switched `type`. `kbin_dma_ring_type` (ordinal
6028): `ENG=0 H2T=1 H2T_SERVICE=2 H2T_COPY=3 COLLECTIVES=4 MODEL=5 DATA=6 IN=7 OUT=8
INDIRECT_MEMCPY=9 ACT_TBL=10 DYNAMIC_ACT_TBL=11 DVE_TBL=12 IOQ_SWITCHER=13 TOPSP_INIT=14
EMBEDDING_UPDATE=15 CUSTOM_OP=16 GENERIC=17 LAST=18`. The string switch maps:
`"in"→IN(7)`, `"out"→OUT(8)`, `"data"→DATA(6)` or `INDIRECT_MEMCPY(9)`,
`"act_load"→DYNAMIC_ACT_TBL(11)`, `"embedding_update"→EMBEDDING_UPDATE(15)`; a missing
`"type"` rejects.

> **GOTCHA — `CUSTOM_OP(16)` is NOT produced by the string switch.** The GPSIMD custom-op
> transfer ring is plumbed by the ucode custom-op path (the `EMBEDDING_UPDATE` /
> `LOAD_POOL_ARGUMENT` pseudo lowering), not by a `"type"` arm. The materialized ring is
> `kbin_dma_ring_instance_t` (264 B: `name[256]@0`, `ndesc u32@256`, `desc[]@264`).
> `[HIGH × OBSERVED — version-compat §8]`

Fixture: `q_gradient_in{owner pool, sem 10, in}`; `q_gradient_out{owner pool, sem 11, out}`.

---

## 4. The engine `.bin` — 64-byte TPB-sequencer microcode  `[HIGH × OBSERVED]`

Each engine's program is named by the `<engine>.json` `"instr"` key and loaded by
`parse_one_engine_instr @0x4b7e30` → `load_bin_file @0x4ae500` (a `malloc`+`memcpy` private
copy) into `instr_set { uint8_t* buffer; uint32_t size; }` (size 16) keyed by engine name.
**The `.bin` is NOT Vision-Q7 / Xtensa code** — it is a **TPB *sequencer* instruction
stream of fixed 64-byte slots** (the byte-level ISA is [seq-microcode](./seq-microcode.md)).

### 4.1 The 64-byte slot  `[HIGH × OBSERVED — STATIC_ASSERTed shipped header]`

`aws_tonga_isa_tpb_common.h` pins `TONGA_ISA_TPB_INST_NBYTES=64`, `INST_NWORDS=16`,
`RT_MAX_NAME=32`, `NUM_SEMAPHORES=32` (all re-read this pass). The common 4-byte header
(`TONGA_ISA_TPB_INST_HEADER`, `STATIC_ASSERT(sizeof==4)`):

| off | sz | field | sample | meaning |
|:---|:---|:---|:---|:---|
| `+0x00` | 1 | `opcode` | varies | 1-byte `TONGA_ISA_TPB_OPCODE` = `base \| (engine<<5)` (§4.2) |
| `+0x01` | 1 | `inst_word_len` | **`0x10`** | `== NWORDS == 16` (×4 B = 64 B) — constant for **every** slot |
| `+0x02` | 1 | `debug_cmd` | `0` | bits[1:0]=`TRACE_MODE`, bit[7]=`halt_en` |
| `+0x03` | 1 | `debug_hint` | `0` | software trace hint |

After the header, an **events block** in one of two dialects: **(a)** legacy TONGA 4-B
(`wait_event_mode/idx`, `set_event_mode/idx`) or **(b)** the **NEURON_ISA 8-B semaphore
form** the fixture carries — `wait_mode(1) wait_idx(1) update_mode(1) update_idx(1)
semaphore_value(u32)`, where `wait_mode 0x04` = "wait $S[n] ≥ value" and `update_mode 0x13`
= increment-on-complete. Data-moving ops then use `TONGA_ISA_TPB_MEM_ACCESS_{1,2,3,4}D`
(8/12/16/20 B; up to 4 dims of `{tpb_addr start; int16 step_elem[N]; uint16 num_elem[N]}`).

> **CORRECTION — the "16-bit opcode" is not a 16-bit opcode.** The LE lead word (`0x10C8`
> in `pe.bin`, `0x10C1`/`0x10A0` in `pool.bin`) is `{ byte0 = opcode, byte1 =
> inst_word_len = 0x10 }`. The `0x10` high byte is the **constant 64-byte slot-length
> marker**, identical for every instruction — *not* part of the opcode. Pin `byte0` as the
> 1-byte `TONGA_ISA_TPB_OPCODE`. `[HIGH × OBSERVED — seq-microcode §0]`

### 4.2 The opcode space — `opcode = base | (engine<<5)`  `[HIGH × OBSERVED — shipped enum]`

`TONGA_ISA_TPB_ENGINE` (re-read verbatim): **PE=0x0, ACT=0x1, POOL=0x2, ALL=0x3, RT=0x6,
SIM=0x7** (INVALID=0xFF). The shipped enum **literally** defines opcodes as
`base | (ENGINE<<5)` — e.g. `TENSOR_TENSOR_ARITH = 0x01 | (POOL<<5) = 0x41`,
`PSEUDO_DMA_TRIGGER = 0x01 | (RT<<5) = 0xC1`. So `engine = opcode >> 5`, `base = opcode &
0x1F`. Selected catalog:

| eng | opcode · mnemonic |
|:---|:---|
| PE(0) | `0x01 LDWEIGHTS` · `0x02 MATMUL` |
| ACT(1) | `0x21 ACTIVATE` · `0x22 ACTIVATE_QUANTIZE` |
| POOL(2) | `0x41 TENSOR_TENSOR_ARITH` · `0x42 TENSOR_REDUCE_ARITH` · `0x43 TENSOR_SCALAR` · `0x45 POOL` · `0x46 COPY` · `0x47 CAST` · `0x49 MEMSET` · `0x4D RNG` · `0x7B DEQUANT` · `0x7C/0x7D CROSS_LANE_REDUCE` · `0x7E IOTA` · `0xF0 EXTENDED_INST` · `0xF2 NONZERO_WITH_COUNT` |
| ALL(3) | `0x61 EVENT_WAIT` · `0x62 EVENT_SET` · `0x68 NOP` · `0x69 WRITE` · `0x6A NOTIFY` |
| (class 5) | `0xA0 EVENT_SEMAPHORE` — SP/SEQ control (not a TPB engine, §4.5) |
| RT(6) | `0xC1 PSEUDO_DMA_TRIGGER` · `0xC4 PSEUDO_DMA_MEMCPY` · `0xC8` (gen-dependent, see QUIRK) · `0xCA PSEUDO_EMBEDDING_UPDATE` |
| SIM(7) | `0xEB.. SIM_*` (simulator-only) · `0xFF INVALID` |

> **QUIRK — `0xC8` mnemonic is generation-dependent; the encoding is stable.** The shipped
> ulib-0.21.2 header names RT base `0x08` = `PSEUDO_READ_VAR_ADDR`; the fixture's `pe.asm`
> prints `PSEUDO_TRIGGER_COLLECTIVE` and the 64-byte payload decodes byte-exact as a
> collective record. RT base `0x08` was **reassigned across generations**; what is stable is
> `0x08 | (RT<<5) = 0xC8`. **Pin the encoding, not the mnemonic.** `[HIGH × OBSERVED]`

### 4.3 RT-class pseudos — runtime-patched, never hardware-executed  `[HIGH × OBSERVED]`

The shipped header is explicit (verbatim): RT-class are *"placeholders for the runtime to
add appropriate WRITE based on DMA queue allocation. NEVER executed by the H/W."* and
`pseudo_dma_trigger.h`: *"replaced by Write, by KRT."* So the engine `.bin` is a
**pre-relocation** stream; at `nrt_load` the assembly/patch passes (§9–§10) rewrite each
pseudo into the concrete `WRITE`(`ALL` opcode `0x69`)/semaphore/descriptor-trigger bound to
the model's allocated DMA queue + IP space. Consequences: `0xC1`/`0xC8`/`0xCA` **never
appear in any device-side decode table** (the SEQ never sees them); `PSEUDO_DMA_TRIGGER`
carries its **queue name** (32-B ASCII) which the loader resolves to the physical ring.

### 4.4 Two key pseudo records (re-confirmed byte-exact in the fixture)

`PSEUDO_DMA_TRIGGER` (`pool.bin` slot, `STATIC_ASSERT(sizeof==64)`): `+0x00` header
`{0xC1, 0x10, …}`, `+0x04` events, **`dma_queue_name[32]`** (non-empty asserted),
`use_raw_count u32`, `union {block_id|count} u32`, `reserved[16]`. The shipped struct
declares 4-B events placing the name at `+0x08`; **the fixture's 8-B NEURON_ISA events
dialect shifts the name to `+0x0C`** — proven because the queue name (`"q_gradient_in"`)
decodes cleanly **only** at `+0x0C` (an empty string at `+0x08` would trip the
non-empty-name assert).

`PSEUDO_TRIGGER_COLLECTIVE` (`pe.bin` slot, `sizeof==64`): `+0x0C` `op`(ALU_OP `ADD=0x04`),
`+0x0D` `dtype`(`FP32=0x0A`), `+0x0E` `group_id`(u16), `+0x10` `input_tensor_id`(u32),
`+0x14` `output_tensor_id`(u32), `+0x18` `num_elements`(u64), `+0x20` `ctype`(`ALL_REDUCE=0x01`),
`+0x30`/`+0x38` `src/dst_offset_elems`(u64). The fixture's `pe.bin` (one slot) decodes to
`$S[10]>0 $S[22]++@complete ctype=ALL_REDUCE in=3 out=4 num_elements=32 dtype=fp32 op=ADD
group_id=0` — exactly its shipped `pe.asm`.

### 4.5 The second ISA — the SP sequencer (`sp.bin`)  `[HIGH × OBSERVED]`

Do **not** conflate with the 64-B TPB ISA. SP slots are **8 bytes** (`aws_tonga_isa_sp_common.h`:
`NBYTES=8, NWORDS=2`); the header is one byte `{opcode:7, phase:1}` with its own opcode set
(`WRITE8_IND 0x04`, `NOTIFY_HALT 0x10`, `WAIT_CYCLES 0x20`, `BRANCH_U 0x30`, `SET_CNTR 0x40`,
`SET_AR 0x60`, …). SP is the per-NeuronCore **sync/control sequencer**; the four data engines
(PE/ACT/POOL/DVE) carry the 64-B stream. `sp.bin` is 0 B in the fixture. (`0xA0
EVENT_SEMAPHORE` in `pool.bin` is a class-5 SEQ-control op, *not* a TPB engine opcode.)

### 4.6 The POOL → Vision-Q7 custom-op bridge (`EXTENDED_INST 0xF0`)  `[HIGH × OBSERVED+CARRIED]`

The **POOL engine is the GPSIMD attach point** (8-core Vision-Q7 `ncore2gp` cluster). Two
opcode dispatches in series: **Stage A** (host/NEFF) — the POOL `.bin` SEQ front-end hands
each opcode byte to a Q7 core. **Stage B** (device/Q7) — each core linear-scans its in-DRAM
`kernel_info_table` `{u8 0; u8 0; u8 spec; u8 opcode; u32 funcVA}` (8-B rows) for the packed
`(spec, opcode)` key and `callx8`s the matched `funcVA`. **The 1-byte opcode column IS the
`TONGA_ISA_TPB_OPCODE` low byte — no translation layer.** `EXTENDED_INST 0xF0` is the
custom-op entry: **five** `kernel_info_table` rows differing only by the `spec` byte
(`{0,1,2,4,3}`) — a two-level `(spec, 0xF0)` dispatch. Within the custom-op family, opcode
`0x85 (= -123)` selects a built-in op; any other selects a user ExtISA op (§5). Re-verified
byte-exact in §6.3.

---

## 5. GPSIMD custom-op / ucode library — device-code embedding  `[HIGH × OBSERVED]`

GPSIMD custom-op device code rides in the NEFF as a **ucode library**. **Two injection
paths**, both ending in the same `ucode_lib` struct:

**(A) Default Q7 library — baked into the runtime, *not* the NEFF.** On `arch_type == 2`,
`kelf_load_from_neff` calls `ucode_get_q7_lib @0x2265a0` (re-grounded) →
`ucode_lib_get_ext_isa(CSWTCH.113[arch_type−2])` where `CSWTCH.113 @0x86ada8 = {6, 13, 21}`
(a **third** numbering — ExtISA library identifiers, distinct from `arch_type` 2/3/4 and the
hardware `arch_id`). The default ExtISA library lives in the sibling
`libnrtucode_extisa.so`, registered under JSON key `ext_isa_ucode_lib_def` with `flags = 6`,
capped at **18** ExtISA ops.

**(B) NEFF-supplied library.** `def.json` `"ucode_lib"` names a JSON manifest inside the tar
(e.g. `ucode_lib.json`); each element → `parse_one_ucode_lib @0x4b1610` (re-grounded). Its
keys/gates (re-confirmed):

```
library    (string) path of the device-code BIN in the tar → load_bin_file (private copy)
ulib_to_ucode_version (string semver)  MAJOR-equality vs runtime "1.21.1.0" (@.rodata 0x84fe68);
           parse_version_string splits at first '.'; stoi(major)==1 required;
           mismatch → "Mismatch between GPSIMD lib version and NRT version!"
opcode     (i64)  -123 (==0x85) → flags=0 (built-in); else → flags=6 (user ExtISA op)
cpu_id     (u64)  MUST be 0  (assert "cpu_id == 0"  kelf2kbin.cpp:0x691)
total_cpus (u64)  in {0,1,8}: 1 = one Vision-Q7 core, 8 = the full 8-core cluster
functions[] each → parse_one_ucode_lib_function @0x4b1180 (keys name/opcode/sub_opcode)
duplicate name → "UCode Library %s has already been added"
```

> **CORRECTION — the version JSON key is `ulib_to_ucode_version`, not `version`.** The
> 21-char key is assembled at runtime (`_mm_load_si128(&"ulib_to_ucode_ve")` then overwrite
> from byte 13 with `"_version"`). The `cpu_id == 0`, `ulib_to_ucode_version`, and
> `"1.21.1.0"` strings were all re-confirmed present in `libnrt.so` this pass.
> `[HIGH × OBSERVED — version-compat §6]`

**On-device placement** `[MED × INFERRED]`: the library bytes are a **Vision-Q7 (Cairo NX,
core `ncore2gp`) device program** — in fact a full **Xtensa PI ELF32** (§6).
`nrtucode_get_memory_image` builds the device memory image;
`nrtucode_ll_get_load_sequence` emits the load sequence that DMAs the image into the Q7
**IMEM**. The cores attach to the **POOL** engine; the runtime drives them through Pool
stdio queues. The POOL **preamble** is what `INSERT`s the load slots —
`insert_pool_ulib_config_v2` + `ucode_ll_get_load_sequence` (in `ib_create_one_block`, §10).
So: NEFF tar `library` bytes → `ucode_lib.content` (host) → nrtucode memory image →
load-sequence DMA into Q7 IMEM → dispatched by opcode via the POOL `EXTENDED_INST 0xF0` /
`kernel_info_table` bridge (§4.6, §6).

### 5.1 The ucode-lib struct family — three forms  `[HIGH × OBSERVED]`

The library appears in distinct representations as it crosses host → device:

| form | size | key fields |
|:---|---:|:---|
| `ucode_lib` (runtime / simdjson twin) | 80 | `+0x00 std::string name`, `+0x20 void* content`, `+0x28 u64 content_size`, `+0x30 u32 flags`, `+0x34 u8 cpu_id`, `+0x35 u8 total_cpus`, `+0x38 std::vector functions` |
| `ucode_lib::function_entry` | 40 | `+0x00 std::string name`, `+0x20 u8 opcode`, `+0x21 u8 sub_opcode` |
| `kbin_ucode_lib_function` (**on-disk**, ≡ `ucode_func_info`) | **258** | `+0x000 char name[256]`, `+0x100 u8 opcode`, `+0x101 u8 sub_opcode` |
| `SUNDA_…_LIBRARY_TABLE_ENTRY` (**device-baked** resolver row) | **280** | `+0x00 valid`, `+0x01 opcode`, `+0x02 sub_opcode`, `+0x03 cpu_id`, `+0x04 total_cpus`, `+0x0C flags`(`= kbin flags & 7`), `+0x10 u64 stripped_lib_addr`, `+0x18 char func_name[256]` |

> **GOTCHA — two distinct per-function strides.** The on-disk/staged record is **258 B**
> (`name[256] + opcode + sub_opcode`); the device-baked resolver row is **280 B** (a
> `valid/opcode/cpu/total/reserved/flags/addr` header *then* `func_name[256]`). The producer
> `ucode_stage_libs_table @0x3108e0` reads source at 258-stride, writes dest at 280-stride
> (`strncpy(dst, src, 0xFF)` then `opcode/sub_opcode = u16 @ src+0x100`). **Staging caps**:
> global table **≤ 97** functions ("too many ucode functions in this table"), per-function
> name **≤ 64** ("too long"), per-core **≤ 28** ("per core exceeded the maximum"); one DMA
> queue per Q7 core (8). `[HIGH × OBSERVED — ntff §5]`

---

## 6. The device kernel — NEFF `library` is an Xtensa PI ELF32  `[HIGH × OBSERVED]`

The GPSIMD device ucode is **not** a flat blob: it is a complete **32-bit LE Xtensa
Position-Independent ELF executable** (`ELFCLASS32`, `EI_DATA=LSB`, `ET_EXEC`,
`e_machine=0x5E=94` Tensilica, `e_flags=0x300`, 4 program headers). Re-confirmed this pass
by `xxd` of the ground-truth blob at host file off **`0x2EF7E0`** in
`libnrtucode_internal.so`:

```
002ef7e0: 7f454c46 01010100 00000000 00000000   ELFCLASS32 / LSB / EV_CURRENT
002ef7f0: 0200 5e00 01000000 10560001 34000000   ET_EXEC ; e_machine 0x5E ; e_entry 0x01005610
```

**Segment → device region map** (native `ncore2gp xtensa-elf-readelf -lW`, re-run this pass):

| Phdr | Type | VAddr | FileSz/MemSz | Flg | → device region |
|:---|:---|:---|:---|:---|:---|
| [0] | LOAD | `0x01000000` | `0x6F1E`/`0x6F1E` | `R E` | **IRAM** (`.text` — Q7 FLIX) |
| [1] | LOAD | `0x02000000` | `0x0450`/`0x048C` | `RWE` | **DRAM** (`.rodata`/`.data`/`kernel_info_table`/`.globstruct`/`.bss`) |
| [2] | LOAD | `0x03000000` | `0x0C08` | `W E` | **host-only** (`.dynamic` + `.rela.got`) — never staged |
| [3] | DYNAMIC | `0x03000000` | `0x00A4` | `RW` | host-only (reloc descriptor) |

seg1's `MemSz − FileSz = 0x048C − 0x450 = 0x3C` is the `.bss` the loader zero-pads.

> **GOTCHA — MAVERICK (v5) has a distinct device map.** All v2–v4 (CAYMAN/MARIANA/
> MARIANA_PLUS) blobs use code LOAD `0x01000000`; MAVERICK's code LOAD `VirtAddr = 0x0`
> (OBSERVED), and its device aperture is **INFERRED** only. A reimplementer must read the
> code base from each blob's own `Phdr`, never assume `0x1000000`. `[HIGH × OBSERVED for
> vaddr; v5 interior INFERRED — neff-elf §2]`

### 6.1 The `.rela.got` relocation table  `[HIGH × OBSERVED]`

`readelf -SW`: `[33] .rela.got RELA 030000c8 0075c8 000b40 0c` — VAddr `0x030000C8`,
device-ELF file off **`0x75C8`**, size `0xB40 / 0x0C = 240` `Elf32_Rela` `{u32 r_offset;
u32 r_info; u32 r_addend}`. Re-read this pass at host file off `0x2EF7E0 + 0x75C8 =
0x2F6DA8`:

```
entry0  r_offset=0x0100001B  r_info=0x23 (SLOT0_ALT, type 35)  r_addend=0x0200025C
entry1  r_offset=0x0100001E  r_info=0x14 (SLOT0_OP,  type 20)  r_addend=0x0200025C
entry2,3  all-zero (R_XTENSA_NONE padders)
```

Native `ncore2gp xtensa-elf-readelf -rW` type histogram (re-run this pass): **8 NONE / 30
RELATIVE / 101 SLOT0_OP / 101 SLOT0_ALT = 240**.

> **CORRECTION — reloc type 5 = `R_XTENSA_RELATIVE`, not `R_XTENSA_32`.** The native
> ncore2gp readelf prints `00000005 R_XTENSA_RELATIVE` (30 of them) — confirmed this pass.
> Per the Xtensa psABI, type **1** = `R_XTENSA_32` and type **5** = `R_XTENSA_RELATIVE`; the
> 30 data-word fixups in this table are type 5. The shared reloc-type-set
> `{0=NONE, 5=RELATIVE, 20–34=SLOT*_OP, 35–49=SLOT*_ALT}` holds; pin type 5 to the name
> `R_XTENSA_RELATIVE`. `[HIGH × OBSERVED — neff-elf §5]`

The consumer is the **host** prelinker `prelink_relocate_lib @0x9b6160`: SLOT_OP/ALT
(20–49) funnel to the per-FLIX-slot immediate writer `relocate_op @0x9b6660`; type 5 patches
a 32-bit data word. A failed reloc **aborts** the install (correctness-critical) — the
opposite of the NEFF `kbin_patch` table (warn-only, §9). The `.rela.got` is the **forward,
load-time, address-exact** relocator; `kbin_patch` is the **reverse, trace-time,
slot-index** one — they share only the word "relocation".

### 6.2 The UCPL prelinked form (in flight)  `[HIGH × OBSERVED + CARRIED]`

`prelink @0x9b5d60` flattens the ELF into `{iram_buf, dram_buf}` (seg0 copied+relocated,
seg1 copied+zero-padded(`.bss`)+relocated) and prepends a **32-byte UCPL header**. Magic
`"UCPL "` re-confirmed this pass at host file off **`0x9B5BE0`** (decimal `10178080`):

```c
typedef struct {                  // 0x20 bytes, written to device offset 0
  char     magic[8];     // +0x00  "UCPL " + 3 NUL  (qword 0x204C504355)
  uint32_t code_seg_len; // +0x08  align4(code p_memsz)
  uint32_t data_seg_off; // +0x0c  0x20 + align32(code_seg_len)
  uint32_t data_seg_len; // +0x10  align4(data p_memsz)
  uint32_t init_fn;      // +0x14  = DT_INIT 0x1000000
  uint32_t fini_fn;      // +0x18  = DT_FINI 0x1006F10
  uint32_t start_sym;    // +0x1c  device entry point
} ucpl_header_t;
```

`nrtucode_ll_create @0x9b1a90` then `mem_write_buf`s three regions (UCPL hdr, `iram_buf`→IRAM,
`dram_buf`→DRAM) **broadcast to all 8 Q7 cores**. The `kernel_info_table` is now resident in
device DRAM. The NEFF-supplied custom-op `library` BIN is the **same** ELF32-PI format
(`[HIGH × INFERRED]`: `validate_dynamic_load @0x9b71f0` requires the 4-phdr Xtensa-PI shape;
no NEFF-supplied BIN is byte-present in this corpus subset).

### 6.3 The in-ELF `kernel_info_table` — the device dispatch ABI  `[HIGH × OBSERVED]`

`readelf -SW`: `[7] kernel_info_table PROGBITS 02000380 007400 000088` — VAddr `0x02000380`,
size `0x88 = 17 rows × 8 B`. Re-read this pass at host file off `0x2EF7E0 + 0x7400 =
0x2F6BE0`:

```
row0  00 00 00 7e  80 00 00 01   {spec 0, opcode 0x7E IOTA,              funcVA 0x01000080}
row1  00 00 00 7c  f8 03 00 01   {spec 0, opcode 0x7C CROSS_LANE_REDUCE, 0x010003F8}
row5  00 00 00 41  1c 0f 00 01   {spec 0, opcode 0x41 TENSOR_TENSOR_ARITH, 0x01000F1C}
rows6-10  opcode 0xF0, spec {0,1,2,4,3}  → the five EXTENDED_INST (custom-op) entries
```

Every `funcVA` lands inside `.text`/IRAM `[0x1000000, 0x1006F1E)` (internally consistent).
The 1-byte opcode column **is** the `TONGA_ISA_TPB_OPCODE` low byte — the same number the
compiler emits in the POOL `.bin`. The five `0xF0` rows differ only by `spec` → the two-level
`(spec, 0xF0)` dispatch into `dispatch_extended_inst`. This is the device-resident end of the
I/O ABI; the NEFF var table is the host end.

---

## 7. The version / compatibility gates — three tiers  `[HIGH × OBSERVED]`

A NEFF carries version/compat at **three independent tiers**, each gated by a different
function, in load order ([version-compat](./version-compat.md)).

### 7.1 Tier 1 — container (`neff_parse @0x4ca3f0`)

- **`neff_version_major` ≤ 2** (hard ceiling; else code **10** `NRT_UNSUPPORTED_NEFF_VERSION`,
  "accepted version range: 0.x-2.x").
- **`feature_bits` forward-compat trapdoor** (armed only when `major == 2`). Re-grounded this
  pass at `0x4ca790`: `movabs $0x7ffffffff8000000,%rax ; and 0x220(%r15),%rax ; push
  $0x7ffffff`. If `(feature_bits & 0x7FFFFFFFF8000000) != 0` → code **10** "compiled by a
  newer version of Neuron compiler … supported by this Neuron Runtime: 0x7FFFFFF". The
  arithmetic: mask = bits **27..62** (the trapdoor), supported = `0x7FFFFFF` = bits **0..26**
  (what the runtime understands); `mask & supported = 0`, `mask | supported = bits 0..62`
  (clean partition); bit 63 untested. A future feature opts in by setting a high bit; every
  predating runtime **deterministically refuses** rather than mis-executing. **No override.**
  (`major 0/1` predate the bitmap and bypass the test.)
- **`pkg_version ∈ {1,2}`** (else code 10 "Unsupported NEFF packager"); **data-fit**
  `file_size − 1024 ≥ data_size` (else code **2** `NRT_INVALID`); **integrity** (when `a3`):
  `pkg1` SHA-256 / `pkg2` MD5 over the compressed window; mismatch → code 2.

### 7.2 Tier 2 — architecture target (`kelf::parse_target @0x497c10` + `kelf::load @0x497dc0`)

`kelf-a.json` `target` → `al_hal_tpb_arch_type`: `"sunda"/"v2"→SUNDA(2)`,
`"cayman"/"v3"→CAYMAN(3)`, `"mariana"/"v4"→MARIANA(4)`, `"*"→current instance arch`,
`""→SUNDA(2)` default; else "Invalid target '%s'" → `NRT_INVALID`. `kelf::load` compares the
resolved arch (stored at `kelf+184`) against the **live instance** arch
(`al_hal_tpb_get_arch_type`); a mismatch is a **hard reject** (cause `NEFF_ARCH_INCOMPAT`,
`--target` hint) **unless** `NEURON_RT_ALLOW_LEGACY_NEFF=1` (then WARN + proceed). `target
"*"` always matches. The `kelf-a.json` `"version"` ("0.5") is parse-only, not a gate.

### 7.3 Tier 3 — per-section

- **GPSIMD ucode-lib**: MAJOR-equality semver vs `"1.21.1.0"` (major 1); `cpu_id == 0`;
  `total_cpus ∈ {1,8}`; dup-name reject; staging caps 97/64/28 (§5.1). **Not overridable.**
- **SB carveout (EVTACCEL, `kbl_model_add @0x3058e0`, CAYMAN only)**: strict **value**
  contract `{start_partition 0, num_partitions 128, fixed offset, size 8}`; off-contract →
  `NRT_INVALID`; absent → allowed. `kbin_sb_carveout_type`: `INVALID=0, EVTACCEL=1`;
  `sb_carveout` (40 B): `type@0, offset@8, size@16, start_partition@24, num_partitions@32`.
- **Legacy-SB guard**: missing/corrupt `runtime_statebuffer_reservation` on a requiring arch
  → reject unless `NEURON_RT_ALLOW_LEGACY_NEFF=1`.

### 7.4 Skew policy

Precedence **container → arch → section**, with distinct codes (`NRT_INVALID=2`,
`NRT_UNSUPPORTED_NEFF_VERSION=10`) and cause (`NEFF_ARCH_INCOMPAT`) so the failing tier is
diagnosable. Forward-incompat is **always** a clean refusal. The **only** override surface is
`NEURON_RT_ALLOW_LEGACY_NEFF=1`, which relaxes **only** the arch gate and the legacy-SB guard
— never the `feature_bits` trapdoor or the ucode semver gate.

---

## 8. The DMA subsystem — typed rings + descriptor blocks  `[HIGH × OBSERVED]`

Two distinct DMA structures: **(a)** `def.json` `dma_queue` → `parse_one_dma_ring` → typed
`kbin_dma_ring_type` rings (§3.3; `CUSTOM_OP(16)` is the GPSIMD custom-op ring); **(b)**
`<engine>.json` `"dma"` array → `parse_one_dma_block @0x4bc620` → per-block descriptor lists.

`kbin_dma_desc` (5456 B on-disk): `+0x0000 desc_type` (`DATA=0 EVENT=1 INC_SEMA=2`), `+0x0004
op` (`INVALID=-1 COPY=0 FMA=1 ADD=2 MIN=3 MAX=4 TRANSPOSE=5`), `+0x0008` union (5424 B):
DATA variant = `kbin_dma_desc_info_t to`(160) + `from[32]`(5120) + `num_tiling_dims`(4) +
`transpose_info|cce_info`(140); `+0x1538 block_id`, `+0x153C block_end_desc`, `+0x1540
function_name*`, `+0x1548 priority_class`. `kbin_dma_desc_info_t` (160 B): `idx`/`off`/
`num_dims`/`steps[8]`(signed; negative = transpose)/`sizes[8]`/`dtype`. The `cce_info` (140 B)
carries `num_sources/num_dests` + fma `scale[32]` + `min_max` — the collective-compute (CCE)
packet (`add_dma_packet_cce @0x2307d0` maps `op{FMA/ADD/MIN/MAX}` → `SDMA_CCETYPE`).

The `PSEUDO_DMA_TRIGGER` `block_id` (§4.4) selects which descriptor block to transfer;
`use_raw_count` distinguishes a block ref from a raw descriptor count. Weight staging
(§11) is a **load-time bulk copy**; per-inference operand movement is the descriptor rings
(SBUF↔HBM).

---

## 9. The relocation / patch tables — IP-space relocation  `[HIGH × OBSERVED]`

Engine streams are compiled against a *device-IP* space; at load the kbin-construction passes
`INSERT/MODIFY/DELETE` 64-byte slots while expanding the compiler's pseudos, and record a
per-engine patch list — the **NEFF analog of ELF dynamic relocations** (a debug/trace
PC→source map, *not* required for execution correctness).

`kbin_patch_location_t` (16 B, ordinal 8667, re-grounded `size=16`):

| off | sz | field | meaning |
|:---|:---|:---|:---|
| `+0x0` | 4 | `offset` | byte offset of the patch site in the **translated** (kbin) buffer |
| `+0x4` | 4 | `count` | **slot** count (`byte_count >> 6`, i.e. number of 64-byte slots) |
| `+0x8` | 4 | `type` | `kbin_patch_type_t`: `INSERT=0, MODIFY=1, DELETE=2` |
| `+0xC` | 4 | `section` | `kbin_patch_section_t`: `PREAMBLE=0, POSTAMBLE=1, MAIN=2, FUNCATION=3` |

> **CORRECTION / QUIRK — the section-3 symbol is spelled `FUNCATION`.** Re-confirmed this
> pass against `enums.json`: the enum table contains `KBIN_PATCH_SECTION_FUNCATION` exactly
> once (every other "FUNCTION" occurrence in the binary is the correct spelling). The
> *value* is 3 either way; reproduce the producer's typo for symbol parity. `[HIGH × OBSERVED]`

Containers: `kbin_eng_patch_t` (16 B: `count`, `array_count`, `kbin_patch_location_t*
locations`); `kbin_patch_info_t` (80 B: `eng_patch[5]`, indexed `{PE,ACT,POOL,DVE,SP}`),
resident at `model_t + 0x18A0` (so `kbin_patch_device_ip_to_neff_ip` computes the per-section
table base as `mod + (eng + 0x18A)*16`).

**Building the list** (`translate_pseudo_instrs_partial_v2 @0x2763d0`, a *side effect* of
assembly): per 64-B source slot, `translate_one_pseudo_instr_v2` emits N real slots and the
delta is recorded — `N>1 → INSERT count=N−1`; `N==0 → DELETE count=1` (`0x40 >> 6`); `N==1 →
passthrough (no record)`. `MODIFY` = a 1→1 in-place branch fixup (the `0xD3` call / `0xDF`
exit resolved to a relative branch once offsets are known), carried through
`ib_add_one_function_to_final_buf @0x27c4f0`. `patch_section = MAIN(2)` for `func_id 0`,
`FUNCATION(3)` otherwise. Append failure only **WARN**s.

**The relocation walk** (`get_neff_ip @0x2faee0` / `kbin_patch_device_ip_to_neff_ip
@0x2fb3a0`, re-grounded): translates a runtime device PC back to the source-stream slot index.
Per matching entry — `MODIFY +0x40 addr & +1 ip`; `DELETE +count` both; `INSERT +count<<6
addr only` (synthetic slots have no source index); tail = `(device_ip − addr) >> 6`. The
`0x40`/`<<6` stride everywhere **confirms 64-byte slots**. A PC inside a FUNCTION block
accumulates two `get_neff_ip` calls. On miss → "Failed to find section %d in the patch list".

---

## 10. Per-engine image assembly — the `ib_create` pipeline  `[HIGH × OBSERVED]`

The `def.json` `.bin` is **only the MAIN-body source**; the runtime **assembles** a larger
device image around it by buffer concatenation, in this fixed physical order, then DMA-stages
it to HBM as one `inst_block`. The assembler is **`ib_create_one_block @0x2f7e50`**
(re-grounded), driven by `sequencer_setup_instr @0x4483d0` (which asserts the last engine is
SP).

```
[ PREAMBLE ( MODEL_SWITCH inside ) ][ MAIN ][ POSTAMBLE ][ FUNCTION* ]
```

| region | builder(s) | patch record |
|:---|:---|:---|
| **PREAMBLE** | runtime-synthesized notify/barrier/DGE-config/feature-flags/collectives-init. For **POOL**: `insert_pool_ulib_config_v2` + `ucode_ll_get_load_sequence` — the **GPSIMD IMEM load**, the §5/§6 bridge. | whole-region `INSERT/PREAMBLE` at offset 0 |
| **MAIN** | `ib_add_one_function_to_final_buf(function[0])` appends the pseudo-EXPANDED `def.json` stream; `kbin_patch_extend` splices the MAIN patches. | per-instruction `INSERT/DELETE/MODIFY` × MAIN |
| **POSTAMBLE** | `ib_insert_common_postamble @0x27b190` (sema-reset / DMA-bundle rearm / done-notify) + the execution-reset branch (`0xDF` exit → `offset_0`, POSTAMBLE start). | whole-region `INSERT/POSTAMBLE` |
| **FUNCTION\*** | extra callable functions (`func_id != 0`); a branch-fixup pass resolves `0xD3 PSEUDO_FUNCTION_CALL → br_rel(fn entry)` (the `MODIFY` producer). | `INSERT/MODIFY/DELETE` × FUNCATION |

**The five-stage `def.json → image` chain:**

| stage | entry @ addr | produces |
|:---|:---|:---|
| 1 LOAD | `parse_one_engine_instr @0x4b7e30 → parse_instr @0x4aefe0` | `instr_set{buffer,size}` (raw `<eng>.bin`) |
| 2 IDENTIFY | `itf_identify_functions` | functions[]/blocks[] (`func_id`, `kbin_begin/end_offset`) |
| 3 TRANSLATE | `itf_setup_functions_one_eng @0x2798c0 → translate_pseudo_instrs_partial_v2 @0x2763d0` | expanded slots + per-block patch_info |
| 4 ASSEMBLE | `ib_create_eib @0x323b70 → ib_create_one_block @0x2f7e50` | one `ib_code_one_eng_t` (eib), model patch_info, HBM `inst_block` |
| 5 RESOLVE | `ib_fill_debug_info @0x323c90 → ib_fill_debug_info_impl @0x2f69d0` | `ib_addrs_one_eng` (device base + ranges) |

**Struct outputs** (host eib → device-resolved): `ib_code_one_eng_t` (56 B: `+0 inst_block*
code`, `+8 preamble`, `+0x18 model_switch`, `+0x28 core` — each `ib_code_range_t {offset,
size}`); `ib_addrs_one_eng` (56 B, `model_t.ib_addrs[5] @0x1988`: `+0 dmem_t* base`, `+8
one_offset`, `+0x18 model_switch_offset`, `+0x28 core_offset`); `inst_block` (96-B header +
buffer: `name[32]`, `buf_len`, `inst_len`, `pcore`, `allocator`, `eng`, `dmem`, `start_addr =
dmem._pa + dmem.align_offset`, `gc_tracker`, `buf[]`). `model_t.ib_functions[5] @0x19B0` holds
the FUNCTION range. The HBM stage (`ib_transfer_block_to_tdram @0x27ad80`) uses
`dmem_alloc_aligned(TONGA_DRAM, align = max(0x8000, per-eng size), pow2)` + `dmem_buf_copyin`
— the **same DMA path as weights** (§11). SP (eng 4) is assembled **last** (driver assert
`sequencer_sunda.c:0x239`).

---

## 11. Weight / constant layout — tar member → mem_ref → HBM/SBUF  `[HIGH × OBSERVED]`

Constant tensors are **separate tar members** named by the var table (a `"file"` var names
the payload via `file_name` + the sub-graph `prefix`). `parse_one_variable @0x4b36b0`, `"file"`
branch: `.npy` → `numpy_load @0x4cb810` (verify magic `u16 == 0x4e93`; `header_len = u16 at
npy off 8`; `data = header + (hdr_len + 10)` — into the tar buffer, zero-copy, **v1 npy headers
only**); else (raw `.bin`) → `load_bin_file` (private copy). Either way → an `MR_BUFFER(3)`
mem_ref whose buffer points at the constant bytes. **metaneff carries no weights — they live
only here.**

`kbin_mem_ref` (152 B on-disk, re-grounded `size=152`): `+0x00 mr_type`, `+0x08 name`, `+0x10
size`, `+0x18 alignment`, `+0x20 var_id`, `+0x24 dtype[16]`, `+0x38 shape[8]`, `+0x78
dtensor_md*`, `+0x80 buffer*` (constant payload ptr; NULL for non-constant MR types), `+0x88
union {ptr | virtual_scratchpad | list | remote}`.

**Staging** (`mem_ref_copy_and_stage_mr @0x2fb780`, re-grounded): only MR types
`{SB(1), BUFFER(3), TMP_BUF(4), VIRTUAL_TMP_BUF(8)}` are bulk-staged. `MR_BUFFER`/`MR_TMP_BUF`
→ HBM via `dmem_alloc_aligned(TONGA_DRAM, hbm_idx, usage = DMA_MEM_USAGE_TYPE_WEIGHT(6) for
weights / SCRATCHPAD_NOT_SHARED(16) for types 4/8)`; if `mr.buffer != NULL` → `dmem_buf_copyin`
(the DMA); resolved PA = `dmem._pa + dmem.align_offset`. `MR_SB` → the **32 MiB** on-chip
State-Buffer AXI aperture (no copy). `dmem_buf_copyin @0x229820 → dmem_device_copy @0x228090
→ ndl_memory_copy_as` (async copy-buffer DMA). **The same stage DMA carries the per-engine
sequencer image (§10) — weights and code use one HBM-stage path.**

---

## 12. The complete load sequence — one flow  `[HIGH]`

```
nrt_load(neff_bytes, [metaneff_bytes])
 │
 ├─ neff_get_header_from_buffer @0x4ca2c0 : structural validate (NULL / size / header_size)
 ├─ neff_parse @0x4ca3f0
 │     TIER-1 gates: major≤2 ; feature_bits trapdoor ; pkg{1,2} ; data fit ; (a3) MD5/SHA256
 │     tar walk (gzip filter for pkg2) → neff_t::files map (in memory, GNU ustar)
 │
 ├─ kelf::load (per kelf-a.json graph)
 │     TIER-2 gate: parse_target → arch ; vs instance arch (NEFF_ARCH_INCOMPAT)
 │     kelf_load_from_neff @0x4c0870, def.json keys IN ORDER:
 │       parse_one_variable @0x4b36b0  → var table → mem_refs (file vars = MR_BUFFER §11)
 │       check_var_ids (dense 0..N-1) ; num_outputs>0
 │       parse_one_dma_ring @0x4b5f80  → typed rings (CUSTOM_OP=16 for GPSIMD)
 │       parse_replica_groups / src_target_pairs (collectives)
 │       ucode_get_q7_lib @0x2265a0 (default) / parse_one_ucode_lib @0x4b1610 (NEFF)
 │                                        — TIER-3 semver, cpu_id==0, total_cpus{1,8} §5
 │       parse_sb_carveouts (EVTACCEL value-check §7.3) ; parse_fp8_conversion_config
 │       per engine PE/ACT/POOL/DVE/SP:
 │         parse_one_dma_block @0x4bc620   → descriptor blocks (§8)
 │         parse_one_engine_instr @0x4b7e30 → load <eng>.bin (64-B slots §4)
 │
 ├─ per-engine ASSEMBLY (sequencer_setup_instr @0x4483d0, §10):
 │     itf_identify_functions → itf_setup_functions_one_eng → translate_pseudo_instrs_partial_v2
 │       (expand pseudos, BUILD patch_info §9)
 │     ib_create_one_block: PREAMBLE / MODEL_SWITCH / MAIN / POSTAMBLE / FUNCTION*
 │       (POOL: INSERT the Q7-library LOAD SEQUENCE — the GPSIMD IMEM bridge §5/§6)
 │     ib_transfer_block_to_tdram @0x27ad80 → HBM-resident inst_block per engine
 │     ib_fill_debug_info → ib_addrs_one_eng (device base + code ranges)
 │
 ├─ STAGE constants/weights (mem_ref_copy_and_stage_mr @0x2fb780, §11):
 │     MR_BUFFER → HBM (dmem_alloc_aligned + dmem_buf_copyin) ; MR_SB → SBUF AXI
 │
 ├─ STAGE GPSIMD ucode libraries (§5/§6): prelink the Xtensa PI ELF (240 .rela.got entries)
 │     → emit UCPL header → mem_write_buf ×3 (UCPL/IRAM/DRAM) broadcast to 8 Q7 cores
 ▼
nrt_model* ready. (host) metaneff binds at::Tensors → nrt_tensor[i] → var_id i (§3.2).
nrt_execute() triggers the engines ; the SEQ hits EXTENDED_INST(0xF0) → kernel_info_table
(spec,0xF0) → Q7 funcVA callx8 (§6.3). (trace time) exec_print_engine_instruction_pointer →
kbin_patch_device_ip_to_neff_ip → get_neff_ip maps the live HW PC back to the .asm line (§9).
```

The execution trace itself is captured as **ntff** — a separate protobuf with two
arena-allocated roots (`ntff::ntff_info` = static "what will run", `ntff::ntrace_info` =
runtime event stream), where GPSIMD/Q7 work appears under `engine_instruction_info` rows with
`nc_engine_type_ == POOL(2)` ([ntff-trace-parse-state](./ntff-trace-parse-state.md)).

---

## 13. The struct catalog (field-exact, one table)  `[HIGH × OBSERVED]`

| struct | size | owner page | key fields |
|:---|---:|:---|:---|
| `neff_header_t` | 1024 | container | pkg_version/header_size/data_size/version/hash/uuid/feature_bits §1 |
| `neff_t` | 48 | container | `std::map<string,(buf,size)> files` |
| `kbin_mem_ref` | 152 | relocation-weights | mr_type/name/size/var_id/dtype/shape[8]/buffer@+0x80/union §11 |
| `mem_ref` (runtime C++) | 208 | relocation-weights | resolved record (PA/dmem); buffer@+0x58, dtype@+0x80 |
| `MetaTensor` (host proto) | — | metaneff-io-abi | name/shape/data_type/type §3.2 |
| `MetaNeff` (host proto) | — | metaneff-io-abi | input/output_tensors/model_config/output_aliases_to §3.2 |
| `TONGA_ISA_TPB_INST_HEADER` | 4 | seq-microcode | opcode/inst_word_len(0x10)/debug_cmd/hint |
| 64-B TPB slot | 64 | seq-microcode | header4 + events(4/8) + payload §4.1 |
| SP slot | 8 | seq-microcode | `{opcode:7, phase:1}` + payload §4.5 |
| `PSEUDO_DMA_TRIGGER` | 64 | seq-microcode | name[32]@+0x0C(8-B dialect)/use_raw_count/blk §4.4 |
| `PSEUDO_TRIGGER_COLLECTIVE` | 64 | seq-microcode | op/dtype/group/in/out/num/ctype §4.4 |
| `kernel_info_table` row | 8 | neff-elf | `{0,0,spec,opcode,u32 funcVA}`; 17 rows §6.3 |
| `ucode_lib` (runtime) | 80 | container | name/content/flags/cpu_id/total_cpus/functions §5.1 |
| `ucode_lib::function_entry` | 40 | ntff | name/opcode/sub_opcode §5.1 |
| `kbin_ucode_lib_function` (on-disk) | 258 | ntff | name[256]/opcode/sub_opcode (258-stride) §5.1 |
| `SUNDA_…_LIBRARY_TABLE_ENTRY` (device) | 280 | ntff | valid/opcode/sub/cpu/total/flags/addr/func_name[256] §5.1 |
| `kbin_dma_desc` | 5456 | DMA | type/op/to/from[32]/cce/block §8 |
| `kbin_dma_desc_info_t` | 160 | DMA | idx/off/num_dims/steps[8]/sizes[8]/dtype §8 |
| `kbin_dma_ring_instance_t` | 264 | version-compat | name[256]/ndesc/desc[] §3.3 |
| `kbin_patch_location_t` | 16 | relocation-weights | offset/count(slots)/type/section(FUNCATION) §9 |
| `kbin_eng_patch_t` | 16 | relocation-weights | count/array_count/locations |
| `kbin_patch_info_t` | 80 | relocation-weights | eng_patch[5] @ model_t+0x18A0 §9 |
| `ib_code_one_eng_t` (eib) | 56 | assembly | code/preamble/model_switch/core §10 |
| `ib_addrs_one_eng` | 56 | assembly | base/one/model_switch/core_offset §10 |
| `inst_block` | 96+ | assembly | name/buf_len/inst_len/dmem/start_addr/buf[] §10 |
| `sb_carveout` | 40 | version-compat | type/offset/size/start_part/nparts §7.3 |
| `Elf32_Ehdr` (device) | 52 | neff-elf | class32/LSB/ET_EXEC/mach 0x5E/flags 0x300/phnum 4 §6 |
| `Elf32_Rela` | 12 | neff-elf | `{r_offset, r_info, r_addend}`; types 0/5/20/35 §6.1 |
| `ucpl_header_t` | 32 | neff-elf | magic 0x204C504355/seg lens/init/fini/start_sym §6.2 |

**Key enums** (re-grounded `enums.json` this pass):

```
kbin_mr_type           INVALID0 SB1 BUFFER_STAGED2 BUFFER3 TMP_BUF4 INPUT5 OUTPUT6 PTR7
                       VIRTUAL_TMP_BUF8 LIST9 PTR_TABLE10 REMOTE11
al_hal_tpb_eng_type    PE0 ACT1 POOL2 DVE3 SP4 (MAX5)            [file-name / HAL engine scale]
TONGA_ISA_TPB_ENGINE   PE0 ACT1 POOL2 ALL3 RT6 SIM7             [opcode-class scale, opcode=base|(eng<<5)]
al_hal_tpb_arch_type   INVALID0 _1 SUNDA2 CAYMAN3 MARIANA4 (NUM5)
kbin_dma_ring_type     ENG0…CUSTOM_OP16…LAST18
kbin_dma_desc_type     DATA0 EVENT1 INC_SEMA2
kbin_dma_desc_op       INVALID-1 COPY0 FMA1 ADD2 MIN3 MAX4 TRANSPOSE5
kbin_patch_type        INSERT0 MODIFY1 DELETE2
kbin_patch_section     PREAMBLE0 POSTAMBLE1 MAIN2 FUNCATION3     [producer typo preserved]
kbin_sb_carveout_type  INVALID0 EVTACCEL1
R_XTENSA (device)      NONE0 RELATIVE5 SLOT{0..14}_OP20..34 SLOT{0..14}_ALT35..49
NRT_STATUS             NRT_INVALID2 NRT_UNSUPPORTED_NEFF_VERSION10
```

---

## 14. What a reimplementer must do

**TO READ a NEFF:**

1. Read 1024 B; interpret `neff_header_t` (§1). Verify `header_size == 0x400`,
   `version_major ≤ 2`, `pkg_version ∈ {1,2}`; optionally verify the hash (MD5 over the
   **compressed** `data[data_size]` for `pkg2`; take **only** `hash[0..15]`).
2. Take `data[0..data_size]`; if `pkg2` gunzip it; walk the **GNU ustar** tar; skip dirs and
   the `wavegraph-bin.json` debug side-file; build `name → (buf, size)`.
3. Parse `kelf-a.json` (`graphs`/`target`); per graph parse `sgNN/def.json` keys **in order**
   (§3): `var` (the device ABI), `dma_queue`, `replica_groups`, `ucode_lib`, `sb_reserv`,
   per-engine `{dma[], instr→.bin}`.
4. Decode each `<engine>.bin` as **64-byte slots** (8-byte SP slots for `sp.bin`): `byte0 =
   opcode = base|(engine<<5)`, `byte1 = 0x10` word_len, then events (4/8 B) + payload (§4).
   RT-class (`0xC1/0xC8/0xCA`) are PSEUDO placeholders.
5. Resolve constant vars (`file_name → tar member`; `.npy` header at magic `0x4e93`, data at
   `hdr + hlen + 10`) → `MR_BUFFER` (§11).
6. A custom op's `library` is an **Xtensa PI ELF32** (§6): `R+X` LOAD → IRAM, `R+W` LOAD
   (incl. `kernel_info_table`) → DRAM, `.rela.got` (240 entries, types 0/5/20/35) consumed
   host-side by prelink. The `kernel_info_table` `(spec, opcode)` rows are the device dispatch
   ABI.
7. The metaneff (separate) gives the host I/O ordering (§3.2).

**TO WRITE a NEFF:**

1. Emit each engine's pseudo `.bin` (64-B slots; RT pseudos carrying queue names / tensor
   ids; SP `.bin` as 8-B slots).
2. Emit `def.json` (var table with dense `var_id 0..N-1` and `num_outputs > 0`, `dma_queue`,
   `ucode_lib` manifest if a custom op with key `ulib_to_ucode_version`/`opcode`/`cpu_id`(=0)/
   `total_cpus`∈{1,8}, `sb_reservation` if EVTACCEL on CAYMAN).
3. For a custom op, emit the Q7 ext-ISA **ELF32-PI** kernel (`e_machine=0x5E`, the 4-phdr
   `{R+X, R+W, R+W dynamic, PT_DYNAMIC}` shape) with its `kernel_info_table` `(spec,opcode)`
   rows and `.rela.got`, and name it via `def.json`/`ucode_lib.json` `"library"`.
4. Pack the tar (`kelf-a.json` + `neff.json` + `sgNN/*`) as **GNU ustar**; gzip it (`pkg2`).
5. Fill `neff_header_t`: `pkg_version=2`, `header_size=0x400`, `data_size`, version `2.minor`,
   `feature_bits` = OR of features used (**keep within bits 0..26** for the target runtime),
   `uuid`, `name`, `vnc_size`; compute `MD5(compressed data) → hash[0..15]` (fill `[16..31]`
   with ASCII `'0'`).
6. Concatenate `[1024-B header][gzip data]`. Ship the metaneff **alongside** (not in the tar)
   for the torch host path.

---

## 15. Open items  `[LOW]`

- The per-bit names within `feature_bits 0..26` are producer-side (only the boundary at bit
  27 and the supported mask `0x7FFFFFF` are OBSERVED).
- `translate_one_pseudo_instr_v2`'s exact per-pseudo expansion factor `N` is data/queue-
  dependent at runtime; the INSERT/DELETE *structure* is OBSERVED, the count `N` is not in the
  static `.bin`.
- The 8-B NEURON_ISA event-mode numeric enum names (`wait 0x04` / `update 0x13` decoded
  operationally; the 4-B TONGA event-mode enum *is* shipped).
- A worked POOL-compute slot's 4-D `MEM_ACCESS` payload (the fixture's POOL stream is only DMA
  triggers + a semaphore gate; the pattern is carried from the struct anchor).
- A byte-exact NEFF-supplied `ucode_lib` ELF tar member (absent from this corpus subset; the
  format identity is INFERRED from `validate_dynamic_load`).
- `numpy_load` handles only **v1** npy headers (`u16` hlen); `v2/v3` (`u32` hlen) large-weight
  bins are unproven.
- v5 / MAVERICK device interiors (code-vaddr `0x0` is OBSERVED; the device aperture mapping is
  INFERRED).

---

## 16. Cross-references

This page consolidates the NEFF Part; each topic's authoritative byte-level page:

- [NEFF Container Byte Format](./container-byte-format.md) — the 1024-B header decompile, the
  tar walk, the `feature_bits` trapdoor, the section→parser map.
- [metaneff Protobuf + var/mem_ref Device I/O ABI](./metaneff-io-abi.md) — the host key-ring.
- [Per-Engine TPB-Sequencer Microcode](./seq-microcode.md) — the 64-byte word ISA.
- [Relocation / Patch Subsystem + Weight Layout](./relocation-weights.md) — patch tables + weight DMA.
- [Per-Engine Instruction-Block Assembly Pipeline](./assembly-pipeline.md) — the `ib_create` chain.
- [NEFF Version / Compatibility Model](./version-compat.md) — the three-tier gates.
- [A Concrete NEFF, Carved Byte-by-Byte](./concrete-carve.md) — the single fixture, instance of this spec.
- [The NEFF ↔ ELF Relationship](./neff-elf-relationship.md) — the device Xtensa PI ELF, prelink, UCPL.
- [The NEFF Byte-Level Container Capstone](./container-capstone.md) — the consolidated container view.
- [ntff Trace Protobuf + simdjson NEFF Parse State](./ntff-trace-parse-state.md) — the execution-trace record.

---

## Guard restatement

Every byte-level claim is grounded in shipped/extracted static-analysis artifacts: the
embedded NEFF fixture (re-read this pass at `libnrt.so` off `0xC07E20`/`0xC08220`; `pkg2` MD5
`61b3cab2…` recomputed over the compressed gzip window; inner archive confirmed GNU `ustar`),
the `STATIC_ASSERT`ed arch-isa headers (`aws_tonga_isa_tpb_common.h`: NBYTES 64 / NWORDS 16 /
ENGINE PE0..SIM7 / `opcode = base|(ENGINE<<5)`), the device Xtensa PI ELF carved at
`libnrtucode_internal.so` off `0x2EF7E0` (e_machine 0x5E re-read; `.rela.got` 240 entries and
the type-5 = `R_XTENSA_RELATIVE` histogram re-run via the native `ncore2gp` readelf;
`kernel_info_table` 17 rows re-read; UCPL magic at off `0x9B5BE0`), and the IDA-typed
structs/enums of `libnrt.so` (load-spine addresses, `kbin_*` sizes, the `FUNCATION` typo, the
`kbin_mr_type`/patch enums all re-grounded). Host control-flow facts are from the `libnrt.so`
decompiled bodies (host x86); device-side dispatch facts are carried from the cited sibling
pages and re-grounded against the binary. Every fact reads as derived from static analysis of
shipped/extracted artifacts alone.
