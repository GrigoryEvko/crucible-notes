# The NEFF Byte-Level Container Capstone

This is the single publishable specification of the **Neuron Executable File
Format (NEFF)** as consumed by the AWS Trainium/Inferentia runtime: the complete
byte layout, the compression model, the device-ELF↔NEFF mapping with the UCPL
prelink/relocation walk, the host/device I/O ABI, the version/feature-bits
compatibility model, and a reimplementer's read/write checklist. It sits *above*
the parallel synthesis [`format-reference.md`](./format-reference.md) (the
abstract spec) and the nine concrete lane pages, consolidating them and folding
in the runtime load path, the host prelinker, and the compiler packager seam.

Every fact is anchored to an address/offset/symbol/enum/opcode/string and tagged
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`. `CARRIED` claims are re-grounded
against the binary in this pass; a claim that could not be re-pinned is flagged.

**Binaries of record (both re-verified this pass):**

| binary | role | path |
|--------|------|------|
| `libnrt.so.2.31.24.0` | the NEFF **reader**/loader (122,956,336 B) | `neuronx-runtime/extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so.2.31.24.0` |
| `libnrtucode_internal.so` | the device-ELF prelinker/relocator | `neuronx-gpsimd/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so` |

The ground-truth fixture used throughout is a **real decompressed pkg2 NEFF
embedded in `libnrt.so`**: the 1024-B header at file offset `0xC07E20`, its gzip
inner archive at `0xC08220`. `libnrt.so`'s `.text`/`.rodata`/`.data` are all
`VMA == fileoffset` (zero delta), so every quoted libnrt offset is both.

**Lane cross-links** (all re-read in full for this capstone):
[`container-byte-format.md`](./container-byte-format.md) ·
[`metaneff-io-abi.md`](./metaneff-io-abi.md) ·
[`seq-microcode.md`](./seq-microcode.md) ·
[`relocation-weights.md`](./relocation-weights.md) ·
[`assembly-pipeline.md`](./assembly-pipeline.md) ·
[`version-compat.md`](./version-compat.md) ·
[`concrete-carve.md`](./concrete-carve.md) ·
[`neff-elf-relationship.md`](./neff-elf-relationship.md) ·
[`ntff-trace-parse-state.md`](./ntff-trace-parse-state.md) ·
[`format-reference.md`](./format-reference.md).
Runtime path: [`../runtime/callgraph-spine.md`](../runtime/callgraph-spine.md),
[`../runtime/libnrt-surface.md`](../runtime/libnrt-surface.md). Host prelinker:
[`../runtime/prelinker-ucpl.md`](../runtime/prelinker-ucpl.md),
[`../runtime/ucode-relocation-consumer.md`](../runtime/ucode-relocation-consumer.md).
The compiler producer seam is Part-12; cite it as the inline path
`compiler/neff-packager.md` (stub may not exist yet — not linked, to avoid a
broken-link warning). The device ext-ISA the relocated kernel runs is the
device-ISA lane (`compiler/...` / images lane), out of scope here.

---

## 0. The NEFF at a glance — two levels, three artifacts, one codec

A NEFF is a **flat 1024-byte C-struct header immediately followed by an inner
archive** (an in-memory tar filesystem). There is **no literal magic number**;
the header is identified *structurally*.

```
 file off 0x000                         file off 0x400 (== header_size)
 +-------------------------------+-------------------------------------------+
 | 1024-byte neff_header_t       | data_size bytes of inner archive          |
 | (pkg_version, sizes, hash,    | pkg2: ONE gzip stream over a GNU-ustar tar|
 |  uuid, feature_bits, vnc)     | pkg1: a raw GNU-ustar tar (no gzip)       |
 +-------------------------------+-------------------------------------------+
```

**Three artifacts together form a loadable model** `[HIGH·CARRIED — §1/§4]`:

1. **the NEFF** — the device program + the *device*-side var table (this doc).
2. **the metaneff** — a *separate* proto3 string, the *host*-side I/O key-ring,
   **NOT inside the NEFF tar** (handed to the torch host path alongside the NEFF; §4).
3. **the weight bins** — constant tensors, which **do** ride inside the NEFF tar,
   named by the var table (§1.6).

A **fourth** artifact lives in the *runtime*, not the NEFF: the default Vision-Q7
ext-ISA library (a 32-bit Xtensa PI ELF32, §3), baked into
`libnrtucode_internal.so`. A NEFF-supplied custom-op kernel (`def.json`
`"ucode_lib"`) overrides it; either way the relocated kernel is what the eight Q7
pool cores run (§3).

> **GOTCHA — there is no NEFF magic.** Detection is structural (§1.1): a sane
> `header_size`/`data_size`, `neff_version_major ≤ 2`, `pkg_version ∈ {1,2}`, and
> (for pkg2) a `1f 8b` gzip stream at offset `0x400`. The only sub-magics in the
> whole container are the gzip `1f 8b 08`, the npy `0x4e93`, the device ELF
> `7f 45 4c 46`, the UCPL `"UCPL "`, and a library-index slot marker `0x1095`.
> `[HIGH·OBSERVED]`

The load spine, byte-verified this pass (every address re-resolved from
`libnrt.so` IDA `functions.json`, every caller edge confirmed):

```
nrt_load  →  nrt_load_util  →  kmgr_load_nn_nc  (callers of neff_parse @0xde4c4,0xde6ca)
  → neff_parse @0x4ca3f0
      → neff_get_header_from_buffer @0x4ca2c0     (structural gate)
      → archive_read_support_filter_gzip @0x4ca5a5 → gzip_filter_read @0x4d1970
            → inflateInit2_ @0x5020d0 / inflate @0x502260 / inflateEnd @0x504490
      → per-member tar walk → neff_t::files (std::map<name,(buf,size)>)
  → kelf_load_from_neff @0x4c0870
      → parse_one_variable @0x4b36b0        (the device var table, §1.6/§4)
      → parse_one_dma_ring @0x4b5f80        (typed DMA rings, §1.8)
      → parse_one_ucode_lib @0x4b1610       (GPSIMD custom-op kernel, §3)
      → kelf::parse_target @0x497c10 / kelf::load @0x497dc0   (arch gate, §5.3)
```

---

## 1. The byte-level container layout

### 1.1 The 1024-byte `neff_header_t` (IDA ordinal 5896, size 1024)

Offsets are exact; `[=…]` is the embedded-fixture byte, re-verified this pass by
`xxd -s 0xC07E20 -l 1024`. `[HIGH·OBSERVED]`

| off | type | field | meaning | fixture |
|-----|------|-------|---------|---------|
| `+0x000` | u64 | `pkg_version` | 1 = raw-tar + SHA256 ; 2 = gzip-tar + MD5 | `=2` |
| `+0x008` | u64 | `header_size` | `== 0x400` in practice; `< file_size` | `=0x400` |
| `+0x010` | u64 | `data_size` | inner-archive byte count (**COMPRESSED** for pkg2); `≤ file_size-0x400` | `=0x6AD` (1709) |
| `+0x018` | u64 | `neff_version_major` | accepted `0..2` (hard ceiling) | `=2` |
| `+0x020` | u64 | `neff_version_minor` | **not** range-checked (informational) | `=0` |
| `+0x028` | char[128] | `neff_build_version` | ASCII label, never parsed | `="2.0.21884.0%kaena-tools/develop@6c66f4b"` |
| `+0x0A8` | u32 | `unused_0` | producer compression flag (1 = gzip) | `=1` |
| `+0x0AC` | u8[32] | `hash` | pkg1: 32-B SHA256 ; pkg2: 16-B MD5 in `[0..15]` | `=61b3cab2 4369b326 a2b3ee40 a744c746` |
| `+0x0CC` | u8[16] | `uuid` | RFC-4122 v4 model UUID | `=561826fb-125f-4bb8-991c-29e085c8949a` |
| `+0x0DC` | char[256] | `name` | model/test name | `="x"` |
| `+0x1DC` | u32 | `requested_tpb_count` | TPBs requested (1 = single core) | `=1` |
| `+0x1E0` | u8[64] | `tpb_per_node` | per-NUMA-node TPB counts | `=0…` |
| `+0x220` | u64 | `feature_bits` | forward-compat capability mask (§5.2) | `=0` |
| `+0x228` | u32 | `vnc_size` | virtual-NeuronCore size (1=single, 2=dual-fuse) | `=1` |
| `+0x22C` | u8[468] | `pad` | reserved | — |
| `+0x400` | u8[`data_size`] | `data` | inner archive payload (tar; gzipped if pkg2) | — |

**Structural detector** `neff_get_header_from_buffer @0x4ca2c0` `[HIGH·OBSERVED]`:
reject `neff == NULL` (`"Invalid NEFF buffer"` @`0x8497b6`); reject
`neff_size ≤ 0x3FF` (`"Invalid number of NEFF bytes received"` @`0x82d2d0`);
reject `header_size(+8) ≥ neff_size`. It returns the header pointer; it does
**not** read the version (that is `neff_parse`'s job).

> **GOTCHA — `data_size` is the COMPRESSED count.** The fixture's `data_size` is
> `0x6AD` = 1709 bytes (the gzip stream length), which inflates to 20480 B. A
> reimplementer must size the gzip member from `data_size`, then inflate to
> learn the tar length, not the reverse. `[HIGH·OBSERVED — confirmed: 1709-B gzip → 20480-B tar]`

> **QUIRK — `hash[16..31]` is not zero padding in the fixture.** For pkg2 only
> `hash[0..15]` (the MD5) is meaningful; the fixture fills `hash[16..31]` with
> the ASCII string `"0000000000000000"` (`0x30` bytes), not NUL. A reader must
> take **exactly** `hash[0..15]` for pkg2 and the full 32 bytes for pkg1.
> `[HIGH·OBSERVED — concrete-carve §1]`

### 1.2 The inner-archive format (the `pkg_version` gate) `[HIGH·OBSERVED]`

| property | pkg_version 1 (legacy) | pkg_version 2 (current) |
|----------|------------------------|--------------------------|
| inner archive | raw GNU-ustar tar | gzip-compressed GNU-ustar tar |
| integrity hash | SHA-256 (32 B, full) | MD5 (16 B, first 16 of `hash[]`) |
| hash domain | over `data[data_size]` | over `data[data_size]` (the **compressed** bytes — see §2) |
| assert line | `neff.cpp:0x34` (`sha256`) | `neff.cpp:0x40` (`md5`) |
| unsupported value | → `NRT_UNSUPPORTED_NEFF_VERSION(10)` `"Unsupported NEFF packager"` | same |

The gzip filter is registered **unconditionally**; on pkg1 it bids 0 (no gzip
member) and `inflate` is never reached. The hash is **unkeyed** — integrity, not
authentication (§5.5) — and verified only when `nrt_load` passes the verify flag
(`a3`).

> **CORRECTION — the inner archive is GNU ustar, not POSIX-pax.** Earlier lane
> prose called it "POSIX-pax tar". The fixture's member-0 header carries the GNU
> ustar signature `magic[6]="ustar "` + `version[2]=" \0"` at `+0x100`/`+0x106`,
> GNU base-256 uid/gid (high bit `0x80`), and **zero** PAX/GNU-longname extension
> records (no typeflag `x`/`g`/`L`/`K`). A reimplementer can emit plain GNU ustar.
> `[HIGH·OBSERVED — concrete-carve §3; re-confirmed: ustar magic at tar+0x100]`

### 1.3 The tar walk — `neff_parse @0x4ca3f0` → `neff_t::files` `[HIGH·OBSERVED]`

```c
archive_read_new();
archive_read_support_format_tar(a);
archive_read_support_filter_gzip(a);                  // @0x4ca5a5 — the only caller
archive_read_open_memory(a, hdr + 0x400, data_size);  // in-memory; no temp files
while (archive_read_next_header(a, &entry) == OK) {
    if (filetype(entry) == 0x4000 /*AE_IFDIR*/) continue;           // skip dirs
    name = pathname(entry);
    if (starts_with(name, "./")) name += 2;                          // strip prefix
    if (ends_with(name, "wavegraph-bin.json"|18-B checksum suffix))  // producer side-file
        { archive_read_data_skip(a); continue; }                     // @0x4ca756
    if (size < 0) reject;
    buf = malloc(size); archive_read_data(a, buf, size);
    files[name] = {buf, size};                                       // std::map insert
}
```

`neff_t` is IDA ordinal 5894, size 48 = `{ std::map<string, pair<void*,long>> files }`.
`neff_get_file_content @0x4cb670` is the O(log n) name lookup
(`value.first = buf @node+0x40`, `value.second = size @node+0x48`) used by every
later "load file X".

> **CORRECTION — the skipped side-file suffix is `wavegraph-bin.json`, not
> "checksum".** The 18-byte suffix compared at the skip site (string @`0x84986c`,
> terminator `byte_84987E`) is literally `"wavegraph-bin.json"`. The DX-NEFF-10
> backing report called this a generic `…checksum` skip; the binary names the
> exact debug side-file. `[HIGH·OBSERVED — container-byte-format §3, re-grounded]`

### 1.4 The inner-tar member taxonomy (five section classes)

`[paths OBSERVED in the fixture; roles per the libnrt parsers]`

| member | role | parser/sink |
|--------|------|-------------|
| `kelf-a.json` | top-level kelf descriptor (`graphs[]`, `version`, `target`) | `kelf::load @0x497dc0` (§5.3) |
| `neff.json` | TVM/NNVM-style host graph manifest | (host metadata; ignored by loader) |
| `info.json`/`hlo_metrics.json` | build/profiler metadata | (ignored) |
| `sgNN/` | directory marker, one per sub-graph | `AE_IFDIR`; skipped |
| `sgNN/def.json` | sub-graph manifest (var/dma_queue/ucode_lib/sb…) | `kelf_load_from_neff @0x4c0870` (§1.5) |
| `sgNN/{pe,act,pool,dve}.json` | per-engine manifest (`dma[]`, `instr`) | `parse_one_dma_block @0x4bc620` / `parse_one_engine_instr @0x4b7e30` |
| `sgNN/{pe,act,pool,dve}.bin` | per-engine 64-B-slot TPB sequencer program (§1.7) | `load_bin_file @0x4ae500` → `instr_set{buf,size}` |
| `sgNN/sp.{json,bin}` | SP sync sequencer (8-B-slot ISA, §1.7) | `parse_one_engine_instr` |
| `sgNN/{…}.asm` | human-readable disassembly (DEBUG only) | (not loaded) |
| `ucode_lib.json` | optional GPSIMD custom-op library manifest (§3) | `parse_one_ucode_lib @0x4b1610` |
| `<library>.bin`/`.so` | the device-code (Xtensa PI ELF32) ucode kernel (§3) | `load_bin_file` → `ucode_lib.content` |
| `*-simout.npy` / weight `.bin` | weight/constant payloads named by the var table (§1.6) | `numpy_load @0x4cb810` / `neff_get_file_content` |

The classes: **(a)** JSON manifests (structure); **(b)** engine `.bin`
sequencer microcode (the TPB program, §1.7); **(c)** the device-ELF ucode kernel
(the Q7 ext-ISA program, §3); **(d)** weight/constant payloads (§1.6); **(e)**
debug side-files (ignored). **No section is individually compressed** — the whole
archive is one gzip stream for pkg2 (§2).

### 1.5 The `def.json` section catalog — parsed in fixed key order

For each graph in `kelf-a.json`, `kelf_load_from_neff @0x4c0870` loads
`sgNN/def.json` (simdjson) and consumes its keys in this order. Keys are looked
up by name (an out-of-order producer still works), but the *sinks* fire in this
sequence. `[HIGH·OBSERVED — container-byte-format §5 / concrete-carve §4]`

| `def.json` key | libnrt parser / sink | doc |
|----------------|----------------------|-----|
| `name` | graph name string | §1.5 |
| `var { }` | `parse_one_variable @0x4b36b0` | §1.6/§4 |
| (post-var) `check_var_ids` | dense `var_id 0..N-1` invariant (`"Unexpected var_ids set!"`) | §4 |
| (post-var) `num_outputs > 0` | `"no Outputs"` reject | §4 |
| `dma_queue { }` | `parse_one_dma_ring @0x4b5f80` | §1.8 |
| `replica_groups [ ]` | `parse_replica_groups` (collectives) | §1.8 |
| `src_target_pairs [ ]` | `parse_src_target_pairs` (CC routing) | §1.8 |
| (`arch_type==2`) default Q7 lib | `ucode_get_q7_lib @0x2265a0` | §3 |
| `ucode_lib` (string → file) | `parse_one_ucode_lib @0x4b1610` | §3 |
| `runtime_statebuffer_reservation[]` | `parse_sb_carveouts` (EVTACCEL) | §5.4 |
| `cc_stream` / `num_streams` | CC stream meta | §1.8 |
| `<fp8 config block>` (`n_config`) | `parse_fp8_conversion_config` | §1.8 |
| per-engine loop (PE/ACT/POOL/DVE/SP): `<engine>.json dma[]` | `parse_one_dma_block @0x4bc620` | §1.8 |
| per-engine: `<engine>.json instr → .bin` | `parse_one_engine_instr @0x4b7e30` | §1.7 |
| missing engine | empty placeholder + WARN `"Engine %s not found … empty placeholder"` | — |

Engine names (`al_hal_tpb_get_tpb_eng_names @0x44bd00`, enum
`al_hal_tpb_eng_type`): **PE=0, ACT=1, POOL=2, DVE=3, SP=4** (MAX=5).

### 1.6 The var table + weight/constant tar layout `[HIGH·OBSERVED]`

`var{}` binds each named tensor to a **dense** `var_id` and a device
memory-reference (`mem_ref`). JSON keys: `var_id`, `type`/`#transfer-type`
(`input`/`output`/`tmp-buf`/`state-buffer`/`virtual`/`remote`/`pointer`/`shared`/
`sharded`/`pipe`), `dtype`, `shape`/`internal_shape`/`framework_shape`, `size`,
`alignment`, `backing_buf`/`backing_variable_off` (aliasing), `referenced_var_id`,
`file`/`file_name`/`prefix` (weight payload), `load_weight_bin`, `dge-table`,
`debug_tensor_md`. The dense-`var_id` invariant (`max var_id+1 == map size`) is
enforced after the loop.

`#transfer-type → kbin_mr_type` (12 values, enum from `enums.json`):

```
MR_INVALID=0  MR_SB=1  MR_BUFFER_STAGED=2  MR_BUFFER=3  MR_TMP_BUF=4
MR_INPUT=5  MR_OUTPUT=6  MR_PTR=7  MR_VIRTUAL_TMP_BUF=8  MR_LIST=9
MR_PTR_TABLE=10  MR_REMOTE=11
```

The on-disk record is `kbin_mem_ref` (structures.json, size **152**):
`+0x00 mr_type`, `+0x08 const char* name`, `+0x10 size`, `+0x18 alignment`,
`+0x20 var_id`, `+0x24 char dtype[16]`, `+0x38 u64 shape[8]`,
`+0x80 u8* buffer` (non-NULL only for `MR_BUFFER`), `+0x88 union[16]`.

> **GOTCHA — two different `mem_ref` structs.** The on-disk `kbin_mem_ref` (152 B,
> `buffer @+0x80`) is distinct from the *runtime* C++ `mem_ref` that
> `parse_one_variable` builds with `operator new(0x88)` (`buffer @+0x58`,
> `size @+0x48`, `alignment @+0x50`, `dtype_id @+0x80`). Do not conflate the two
> offset families. `[HIGH·OBSERVED — relocation-weights §7.3]`

**Weights/constants** (`parse_one_variable` "file" branch): `path = prefix + "/" +
file_name`. `.npy` → `numpy_load @0x4cb810` (verify magic `u16 == 0x4e93`
`'\x93N'`; `header_len = u16 @ npy+8`; data ptr = `header + (hlen+10)`, a pointer
**into the tar buffer**; v1 npy headers only). A raw `.bin` →
`neff_get_file_content` directly. Either way create an `MR_BUFFER(3)` `mem_ref`.
**The metaneff does NOT carry weights — they live only here in the NEFF tar.**

**Staging into DRAM/SBUF** (`mem_ref_copy_and_stage_mr @0x2fb780`): the stage
predicate is `if ((unsigned)mr_type > 8 || _bittest64(&-283, mr_type)) skip` —
`-283 = 0xFFFFFFFFFFFFFEE5`, whose **clear** bits `{1,3,4,8}` are the staged
types (MR_SB, MR_BUFFER, MR_TMP_BUF, MR_VIRTUAL_TMP_BUF). `MR_BUFFER`/`MR_TMP_BUF`
→ HBM: `dmem_alloc_aligned(TONGA_DRAM=2, hbm_idx, usage=DMA_MEM_USAGE_TYPE_WEIGHT=6
for weights)`; if `mr.buffer != NULL` → `dmem_buf_copyin @0x229820` (async
copy-buffer DMA); resolved `PA = dmem._pa + dmem.align_offset`. `MR_SB` → the
`0x2000000` (32 MiB) on-chip State-Buffer AXI aperture (`aws_hal_stpb_get_axi_offset`;
no copy). **The same stage DMA carries the per-engine sequencer image (§1.9) —
weights and code use one HBM-stage path.**

### 1.7 The engine `.bin` — 64-byte TPB-sequencer microcode `[HIGH·OBSERVED]`

Each engine's program is named from `<engine>.json` by the `instr` key and loaded
by `parse_one_engine_instr @0x4b7e30` → `load_bin_file` into
`instr_set{uint8_t* buffer; u32 size}`. **Critical: the `.bin` is NOT
Xtensa/Vision-Q7 code; it is a TPB *sequencer* instruction stream of fixed 64-byte
slots.** (The Q7 code is the §3 ucode kernel.)

**The 64-B slot** (shipped `aws_tonga_isa_tpb_common.h`, STATIC_ASSERTed:
`INST_NBYTES=64`, `NWORDS=16`). Common 4-byte header `TONGA_ISA_TPB_INST_HEADER`:
`+0 opcode (1-B packed enum)`, `+1 inst_word_len = 0x10` (every 64-B slot, in
**4-byte words** = 16 = 64 B), `+2 debug_cmd`, `+3 debug_hint`. The events block
has two dialects: legacy TONGA 4 B vs NEURON_ISA semaphore 8 B (the fixture's
dialect — payload lands at `+0x0C`).

> **GOTCHA — `byte1 = 0x10` is the slot-length word count, not half a 16-bit
> opcode.** An older reading treated the LE lead halfword (`0x10C8`) as a 16-bit
> opcode; the correct decode is `byte0 = opcode` (1 byte), `byte1 = inst_word_len
> = 0x10` = 16 four-byte words = 64 B. Reading `byte1` as a byte count (expecting
> `0x40`) rejects every slot. `[HIGH·OBSERVED — seq-microcode §1.1 / concrete-carve §5]`

**The opcode space — `opcode = base | (engine << 5)`** (`TONGA_ISA_TPB_ENGINE`:
PE=0x0 ACT=0x1 POOL=0x2 ALL=0x3 RT=0x6 SIM=0x7). `bits[7:5]` = engine class,
`bits[4:0]` = per-engine base. Selected catalog:

```
PE(0)   0x01 LDWEIGHTS  0x02 MATMUL
ACT(1)  0x21 ACTIVATE   0x22 ACTIVATE_QUANTIZE
POOL(2) 0x41 TENSOR_TENSOR_ARITH  0x42 TENSOR_REDUCE_ARITH  0x45 POOL  0x46 COPY
        0x47 CAST  0x49 MEMSET  0x4A REG_LOAD  0x4B REG_STORE  0x7B DEQUANT
        0x7E IOTA  0xF0 EXTENDED_INST  0xF2 NONZERO_WITH_COUNT
ALL(3)  0x61 EVENT_WAIT  0x62 EVENT_SET  0x68 NOP  0x69 WRITE  0x6A NOTIFY
        0xA0 EVENT_SEMAPHORE   (base 0x00 | class-5<<5; class 5 is NOT a TPB engine)
RT(6)   0xC1 PSEUDO_DMA_TRIGGER  0xC4 PSEUDO_DMA_MEMCPY  0xC8 PSEUDO_TRIGGER_COLLECTIVE
SIM(7)  0xEB.. simulator-only      0xFF INVALID
```

RT-class are **PSEUDO placeholders** (`"RUNTIME modifiable … NEVER executed by the
H/W"`); the loader's `translate_pseudo` / `kbin_patch` passes (§1.9) rewrite each
into a concrete `WRITE`/semaphore/descriptor-trigger bound to the model's
allocated DMA queue + IP space. **So the `.bin` is a pre-relocation stream.**

> **GOTCHA — RT base `0x08`'s mnemonic is generation-dependent.** `0xC8` =
> `0x08 | (RT<<5)`. The shipped `ulib 0.21.2` header names base `0x08`
> `PSEUDO_READ_VAR_ADDR`; the fixture's `pe.asm` prints `PSEUDO_TRIGGER_COLLECTIVE`
> for the same byte. The *encoding* is stable; the *name* is not. Likewise the
> custom-op source pseudo is named `EXTENDED_INST (0xF0)` in
> [`seq-microcode.md`](./seq-microcode.md), `PSEUDO_EMBEDDING_UPDATE` in
> [`relocation-weights.md`](./relocation-weights.md), and `0xCA` in
> [`assembly-pipeline.md`](./assembly-pipeline.md) — three different layers
> (device dispatch key vs source pseudo vs lowering path), not a contradiction.
> `[HIGH·OBSERVED]`

**The SP second ISA (`sp.bin`)**: SP slots are 8 bytes (NBYTES=8, NWORDS=2);
header `{opcode:7, phase:1}` in one byte; its own opcode set (`WRITE8_IND 0x04`,
`NOTIFY_HALT 0x10`, `WAIT_CYCLES 0x20`, `BRANCH_U 0x30`, `SET_CNTR 0x40`,
`SET_AR 0x60`…). SP is the per-NeuronCore sync sequencer; the four data engines
carry the 64-B stream.

### 1.8 DMA rings + descriptor blocks `[HIGH·OBSERVED]`

`def.json "dma_queue"` → `parse_one_dma_ring @0x4b5f80`: named typed rings.
`kbin_dma_ring_type` (ordinal 6028):

```
ENG=0 H2T=1 H2T_SERVICE=2 H2T_COPY=3 COLLECTIVES=4 MODEL=5 DATA=6 IN=7 OUT=8
INDIRECT_MEMCPY=9 ACT_TBL=10 DYNAMIC_ACT_TBL=11 DVE_TBL=12 IOQ_SWITCHER=13
TOPSP_INIT=14 EMBEDDING_UPDATE=15 CUSTOM_OP=16 GENERIC=17 LAST=18
```

String switch: `"in"→IN(7)`, `"out"→OUT(8)`, `"data"→DATA(6)`/`INDIRECT_MEMCPY(9)`,
`"act_load"→DYNAMIC_ACT_TBL(11)`, `"embedding_update"→EMBEDDING_UPDATE(15)`;
missing `"type"` → reject. `CUSTOM_OP(16)` is the GPSIMD custom-op ring (plumbed by
the ucode custom-op path, **not** the string switch). Ring
`kbin_dma_ring_instance_t` (264 B): `name[256]@0`, `ndesc u32@256`, `desc[]@264`.

`<engine>.json "dma"` → `parse_one_dma_block @0x4bc620`: per-block descriptor
lists. `kbin_dma_desc` (5456 B): `+0x0 desc_type` (DATA0 EVENT1 INC_SEMA2),
`+0x4 op` (INVALID-1 COPY0 FMA1 ADD2 MIN3 MAX4 TRANSPOSE5), `+0x8 union`,
`+0x1538 block_id`, `+0x1540 function_name*`. The `PSEUDO_DMA_TRIGGER block_id`
selects which descriptor block to transfer. `replica_groups`/`src_target_pairs`/
`cc_stream` feed the collective topology+routing; `n_config` (`fp8_range_extension`,
`emax_fp8_e4m3/e5m2`, `ocp_sat`…) drives FP8 cast semantics.

### 1.9 Per-engine image assembly + the NEFF relocation (`kbin_patch`)

The `def.json` `.bin` is **only the MAIN body**; the runtime *assembles* a larger
device image (`ib_create_one_block @0x2f7e50`) by buffer concatenation in this
fixed order, then DMA-stages it to HBM as one `inst_block`:

```
[ PREAMBLE ][ (MODEL_SWITCH) ][ MAIN ][ POSTAMBLE ][ FUNCTION* ]
```

- **PREAMBLE**: runtime-synthesized notify/barrier/DGE-config/feature-flags/
  collectives-init. For POOL it INSERTs `insert_pool_ulib_config_v2` +
  `ucode_ll_get_load_sequence` — the GPSIMD IMEM load (the §3 bridge), which is
  why the POOL image is larger than `pool.bin`.
- **MAIN**: `ib_add_one_function_to_final_buf(function[0])` appends the
  pseudo-expanded `def.json` stream; `kbin_patch_extend` splices the MAIN patches.
- **POSTAMBLE**: `ib_insert_common_postamble` (sema-reset/DMA-rearm/done-notify).
- **FUNCTION\***: extra callable functions; a branch-fixup pass resolves `0xD3`
  PSEUDO_FUNCTION_CALL → `br_rel` and `0xDF` exit → `br_rel` (MODIFY). The HBM
  stage (`ib_transfer_block_to_tdram @0x27ad80`) uses `dmem_alloc_aligned` +
  `dmem_buf_copyin` (align ≥ 0x8000) — the **same DMA as weights**. SP assembled
  last (`sequencer_setup_instr @0x4483d0` asserts the last engine is SP=4).

**The NEFF-side patch table** is `kbin_patch_location_t` (16 B, ordinal 8667):
`+0x0 offset` (in the translated kbin buffer), `+0x4 count` (SLOT count =
`byte_count >> 6`), `+0x8 type` (INSERT0 MODIFY1 DELETE2), `+0xC section`
(PREAMBLE0 POSTAMBLE1 MAIN2 **FUNCATION3** — the misspelling is in the binary).
`kbin_eng_patch_t` (16 B); `kbin_patch_info_t` (80 B) = `eng_patch[5]` @
`model_t+0x18A0`. Built as a side effect of assembly
(`translate_pseudo_instrs_partial_v2 @0x2763d0`): `N>1` → `INSERT (N-1)<<6`;
`N==0` → `DELETE 0x40`; `N==1` → no record. Consumed by `get_neff_ip @0x2faee0` /
`kbin_patch_device_ip_to_neff_ip @0x2fb3a0` (device PC → source slot index): the
`0x40`/`<<6` stride **confirms 64-byte slots**.

> **NOTE — this NEFF `kbin_patch` table is WARN-only.** Append failures only WARN
> (`"Debug info may be inaccurate"`); it is a debug/trace PC→source map, **not**
> required for execution correctness. It is *entirely distinct* from the device-ELF
> UCPL relocator of §3 — see the §3.5 contrast. `[HIGH·OBSERVED — relocation-weights §2/§4]`

---

## 2. The compression model — zlib-only, whole-archive `[HIGH·OBSERVED]`

**One-line verdict: the runtime inflates NEFF payloads with zlib ONLY — DEFLATE
via `inflate`, no zstd/lz4/brotli/lzma/bz2 path exists.** Re-grounded this pass by
three independent checks.

**Static inventory** (`nm libnrt.so`):

```
inflate @0x502260   inflateInit2_ @0x5020d0   inflateEnd @0x504490
inflate_fast @0x505a50   inflate_table @0x504e60   adler32 @0x501060   crc32 @0x501d10
banner: "inflate 1.3.1 Copyright 1995-2024 Mark Adler"
NO deflate / deflateInit2_ / compress / gz* symbols (inflate-only vendoring)
nm | rg -ci 'zstd|lz4|brotli'  = 0      nm | rg -c 'lzma_'  = 0      nm | rg -c 'BZ2_'  = 0
libarchive filters defined:  archive_read_support_filter_gzip @0x4d1f40  (and ONLY gzip;
                             no bzip2/xz/lzma/zstd/lz4 filter symbols)
```

**The call edge** (each byte-verified this pass):

```
neff_parse @0x4ca3f0
  → archive_read_support_filter_gzip @0x4ca5a5   (its ONLY caller in libnrt)
  → archive_read_open_memory
  → per-member archive_read_data → gzip_filter_read @0x4d1970
       → crc32 / inflateInit2_ / inflate / inflateEnd
         (inflate's ONLY caller is gzip_filter_read @0x4d1c04)
```

**Granularity = the whole inner archive.** For pkg2 the *entire* inner archive
(`kelf-a.json`, `neff.json`, all `sgNN/*`, `ucode_lib.json`, the `.npy` weight
members …) is a **single** gzip stream; `neff_parse` gunzips that one stream and
walks the decompressed tar. There is no "section X compressed, section Y not", and
no second-stage codec on weights. For pkg1 the gzip filter bids 0 and `inflate` is
never reached. So `pkg_version` is the runtime switch that **arms** (pkg2) or
**disarms** (pkg1) the inflate path.

> **NOTE — the gzip wrapper is inflated by zlib.** The pkg2 member is a standard
> RFC-1952 gzip stream (`1f 8b 08 00`, CM=8 DEFLATE, ISIZE @ stream-end). libarchive's
> gzip filter strips the gzip framing and feeds the DEFLATE body to zlib `inflate`.
> Confirmed on the fixture: `1f8b08` magic, CM=8, ISIZE=20480 == inflated length.
> `[HIGH·OBSERVED — concrete-carve §2, re-confirmed this pass]`

> **GOTCHA — `liblzma`/`libbz2`/`libz.so` ship in the customop SDK but are NOT in
> the runtime inflate path.** Standalone codec `.so`s under
> `…/gpsimd/custom_op/c10/lib/` are host-toolchain transitive deps; `libnrt.so`
> has **no** `DT_NEEDED` on any of them and its inflate is the statically-vendored
> zlib 1.3.1 above. There is also a Rust `Adler32` hasher symbol in `libnrt.so`
> (from an unrelated Rust addr2line component) — it is not on the NEFF codec edge.
> `[HIGH·OBSERVED]`

> **GOTCHA — pkg2's MD5 is over the COMPRESSED gzip bytes, not the tar.** The
> integrity hash domain is `data[data_size]` = the raw gzip stream. Re-verified:
> `md5(1709 compressed bytes) = 61b3cab2 4369b326 a2b3ee40 a744c746` == `header.hash[0..15]`;
> `md5(20480 inflated tar bytes) = 0a677f86…` does **not** match. A writer must
> MD5 the gzip output, not the tar input. `[HIGH·OBSERVED — re-confirmed this pass]`

---

## 3. The device-ELF ↔ NEFF mapping + UCPL prelink / relocation `[HIGH·OBSERVED]`

The GPSIMD custom-op **kernel** the eight Q7 pool cores execute is **not** TPB
sequencer microcode — it is a 32-bit Xtensa **position-independent ELF**
(`e_machine=94/XTENSA`, `ET_EXEC`). It enters by one of two paths and is relocated
by the `libnrtucode_internal.so` prelink engine into device IRAM/DRAM. The §1.7
engine `.bin` and this Q7 ELF are two different ISAs in the same NEFF model.

### 3.1 Where the device ELF comes from (two injection paths) `[HIGH·OBSERVED]`

- **(A) Default Q7 library** (NEFF ships none). On `arch_type==2`,
  `kelf_load_from_neff` calls `ucode_get_q7_lib @0x2265a0`. The default ext-ISA
  library is **baked into the runtime** — 16 embedded Xtensa ELF32 blobs in
  `libnrtucode_internal.so` (a 4×4 matrix: arch ∈ {CAYMAN, MARIANA, MARIANA_PLUS,
  MAVERICK} × `EXTISA_{0..3}`), registered under JSON key `ext_isa_ucode_lib_def`
  with `flags=6`. The fixture's ground-truth blob is the CAYMAN `EXTISA_0` at host
  file offset `0x2EF7E0`. Flavor from env `NEURON_UCODE_FLAVOR ∈ {debug,test,perf-default}`.
- **(B) NEFF-supplied library**. `def.json "ucode_lib"` names a JSON manifest;
  each element → `parse_one_ucode_lib @0x4b1610`; `"library"` names the device-ELF
  BIN inside the tar (same format, `[INFERRED]` since no NEFF-supplied BIN is in
  the corpus). Either way the bytes land in `ucode_lib.content` and are handed to
  the prelink engine.

### 3.2 The device ELF shape (ground-truthed at `0x2EF7E0`) `[HIGH·OBSERVED]`

`xxd -s 0x2EF7E0` re-verified this pass:

```
e_ident  7f 45 4c 46 / 01 01 01 00      ELFCLASS32, EI_DATA=1 (LSB)
e_type   0x02 (ET_EXEC)                  e_machine 0x5e (94 XTENSA)
e_entry  0x01005610                      e_phoff 0x34   e_phentsize 0x20   e_phnum 4
                                         e_shoff 0x9CE8  e_shnum 0x23(35)  e_shstrndx 0x22
```

Program headers (`Elf32_Phdr`, 32 B each), re-dumped this pass:

| Phdr | type | p_offset | p_vaddr | filesz | memsz | flags | align | region |
|------|------|----------|---------|--------|-------|-------|-------|--------|
| 0 | LOAD | `0x0100` | `0x01000000` | `0x6F1E` | `0x6F1E` | 5 (R+X) | `0x80` | CODE → IRAM |
| 1 | LOAD | `0x7080` | `0x02000000` | `0x0450` | `0x048C` | 7 (R+W) | `0x80` | DATA → DRAM |
| 2 | LOAD | `0x7500` | `0x03000000` | `0x0C08` | `0x0C08` | 3 | `0x80` | host-only |
| 3 | DYNAMIC | `0x7500` | `0x03000000` | `0x00A4` | `0x00A4` | 6 | `0x4` | host-only |

`validate_dynamic_load @0x9b71f0` enforces exactly this 3-LOAD + 1-DYNAMIC shape.
The `.bss` zero-pad is `memsz 0x48C − filesz 0x450 = 0x3C` exactly. The data
segment lands *before* code at install. The device-IP entry `0x01005610` is the
Q7 per-core dispatch loop entry. Segments 2/3 (`.dynamic` + `.rela.got`) are
**never staged to device** — the host relocator consumes them.

> **NOTE — "PI" here means relocated-at-load, not PIE.** The image is `ET_EXEC`
> with a `.rela.got`; it is repositioned by the prelinker's segment-delta
> relocation (§3.3), not by a runtime dynamic linker. `[HIGH·OBSERVED]`

### 3.3 The prelink + relocation walk `[HIGH·OBSERVED]`

At `nrtucode_ll_create @0x9b1a90` (per Q7-POOL loadable-library install) the
engine: **(1)** `xtlib_verify_magic @0x9b6d40` gate (`7f454c46` + ELFCLASS32) + the
§3.2 phdr shape; **(2)** `get_dyn_info @0x9b6ed0` walks `PT_DYNAMIC`'s `Elf32_Dyn`
array; **(3)** `prelink_load_lib @0x9b5e70` copies CODE into a poison-filled
(`0xA5`) IRAM host buffer and DATA into a DRAM host buffer, zero-padding to
`p_memsz` (overflow → `"Segment exceeds the size of the split region on device"`);
**(4)** `prelink_relocate_lib @0x9b6160` walks the relocations.

The `Elf32_Dyn` array decoded this pass (at VMA `0x03000000`, file-off `0x7500`):

```
DT_INIT(12)=0x01000000  DT_FINI(13)=0x01006F10
DT_HASH(4)=0x030000A4   DT_SYMTAB(6)=0x030000B4  DT_STRTAB(5)=0x030000C4
DT_STRSZ(10)=1          DT_SYMENT(11)=0x10       DT_DEBUG(21)=0
DT_RELA(7)=0x030000C8   DT_RELASZ(8)=0x0B40      DT_RELAENT(9)=0x0C
0x70000003=0x03000C08   0x7000000C=0x03000C0C    (Xtensa-PI extension tags)
```

`DT_RELA = 0x030000C8` → file-off `0x75C8` (PHDR2 maps vaddr `0x03000000` → file
`0x7500`; `+0xC8`); `DT_RELASZ = 0xB40` / `DT_RELAENT 0x0C` → **240** `Elf32_Rela`
`{u32 r_offset; u32 r_info; u32 r_addend}`.

`reloc_addr @0x9b6130` maps a link-VA to its staged address by the **segment
delta** (decompiled this pass):

```c
u32 reloc_addr(desc *d, int link_va) {
    u32 sep = (d->split_flag != 0) << 10;          // +1024 nudge when split
    if (link_va + sep >= d->seg_boundary)          // at/above boundary → DATA
        return link_va + (d->dst_data_addr - d->seg_boundary);
    else                                           // below → CODE
        return link_va + (d->dst_code_addr - d->src_code_offs);
}
```

The four relocation **types** (low byte of `r_info`), with the histogram
re-classified this pass from the actual 240-entry table — **counts byte-exact**:

| type | name | count | apply path |
|------|------|------:|------------|
| `0` | `R_XTENSA_NONE` | **8** | table padding; skipped |
| `5` | **`R_XTENSA_RELATIVE`** *(see §3.6 CORRECTION)* | **30** | in-place additive 32-bit data-word rebase via `reloc_addr` |
| `20`(`0x14`)..`34` | `R_XTENSA_SLOT{0..14}_OP` | **101** | instruction-immediate, **low** half → `relocate_op @0x9b6660` |
| `35`(`0x23`)..`49` | `R_XTENSA_SLOT{0..14}_ALT` | **101** | instruction-immediate, **high** half (`>>16`) → `relocate_op` |

→ split **8 / 30 / 101 / 101 = 240**. SLOT entries appear in `{35-high, 20-low}`
pairs (the classic Xtensa two-instruction address split). `relocate_op` decodes
the Xtensa instruction (16/24-bit + FLIX bundles) and splices `value` into the
slot's immediate via `.rodata` keep-masks; `"Unknown instruction format"` →
status 12. The dispatch is an if/else chain (`prelink_relocate_lib` has no switch
table); `"Unknown relocation type %d"` @`0x9b663d` is the fall-through.

The type-5 apply (decompiled this pass) is **`*word = reloc_addr(*word +
r_addend)`** — it reads the existing 32-bit value at the site (`"Invalid load @
0x%x"` on fault), adds the addend, rebases through the segment delta, and stores
it back (`"Invalid store (0x%x) @ 0x%x"`). The aligned fast path is a direct
read/write; the unaligned path uses a 2-load/2-store split with merge masks
`dword_9AAF70`/`dword_9AAF7C`. Bounds: every patch site must lie in the per-arch
`memory_bounds` reserved windows; `"Invalid relocation address %u"` → status 8.

> **GOTCHA — symbol resolution is degenerate by contract.** All 240 relocations
> have `ELF32_R_SYM(r_info) == 0` (sym=0); `DT_STRSZ == 1` (an empty string
> table). `r_info > 0xFF` (any non-zero symbol index) → `prelink_relocate_lib`
> returns status 5. There is **no** dynamic-symbol lookup — a packaged ucode
> library must be fully self-relative. "Resolution" is purely the segment-base
> arithmetic in `reloc_addr`. `[HIGH·OBSERVED — re-confirmed: all 240 sym=0, all 30 type-5 sym=0/addend=0, targeting DATA VMA 0x02000xxx]`

### 3.4 The UCPL header + on-device install `[HIGH·OBSERVED]`

On success `prelink @0x9b5d60` emits a `0x20`-byte UCPL descriptor (magic
`0x204C504355` = `"UCPL "`, the qword little-endian `55 43 50 4C 20` — re-verified
both as the `.rodata` string pool `"UCPL "` and as a `constants_used` literal in
`prelink`):

```
+0x00 u64 magic = 0x204C504355 "UCPL "
+0x08 u32 code_seg_len   = align4(code p_memsz)
+0x0C u32 data_seg_off   = 0x20 + align32(code_seg_len)
+0x10 u32 data_seg_len   = align4(data p_memsz)
+0x14 u32 init_fn        = DT_INIT (0x01000000)
+0x18 u32 fini_fn        = DT_FINI (0x01006F10)
+0x1C u32 start_sym
```

`nrtucode_ll_create @0x9b1a90` pushes the two relocated buffers through the
nrtucode PLATFORM vtable (write `{32-B UCPL header, IRAM buf, DRAM buf}`, broadcast
to all 8 cores); the lib is size-gated `> 0x10000` →
`"Prelinked library would be larger than the available buffer on device"`.
`nrtucode_ll_get_load_sequence @0x9b1fb0` then emits N 64-byte TPB-sequencer slots
(library-load opcode, half-word marker `0x1095`) carrying the staged device addr +
size, INSERTed into the POOL PREAMBLE (§1.9) — the bridge from the host-relocated
UCPL image to the device.

**The device dispatch (POOL → Q7).** After relocation the POOL TPB-sequencer
stream's compute opcodes are handed to a Q7 core; each core linear-scans its
`kernel_info_table` — 17 rows of `{u8 0; u8 0; u8 spec; u8 opcode; u32 funcVA}`
at VMA `0x02000380` (in the relocated DATA segment), dumped byte-exact this pass:

| idx | spec | opcode | mnemonic | funcVA |
|----:|-----:|--------|----------|--------|
| 0 | 0 | `0x7E` | IOTA | `0x01000080` |
| 1 | 0 | `0x7C` | CROSS_LANE_REDUCE | `0x010003F8` |
| 2 | 0 | `0x7D` | CROSS_LANE_REDUCE(alt) | `0x01000410` |
| 3 | 0 | `0x45` | POOL | `0x01000B90` |
| 4 | 0 | `0x51` | (tensor-scalar) | `0x0100105C` |
| 5 | 0 | `0x41` | TENSOR_TENSOR_ARITH | `0x01000F1C` |
| 6 | 0 | `0xF0` | EXTENDED_INST spec0 | `0x01003370` |
| 7 | 1 | `0xF0` | EXTENDED_INST spec1 | `0x01003380` |
| 8 | 2 | `0xF0` | EXTENDED_INST spec2 | `0x01003484` |
| 9 | 4 | `0xF0` | EXTENDED_INST spec4 | `0x010037A8` |
| 10 | 3 | `0xF0` | EXTENDED_INST spec3 | `0x01003A60` |
| 11 | 0 | `0x52` | (tensor-scalar) | `0x01003B40` |
| 12 | 0 | `0x46` | COPY | `0x010040C0` |
| 13 | 0 | `0x47` | CAST | `0x01004160` |
| 14 | 0 | `0xBE` | (cast/transpose) | `0x01004204` |
| 15 | 0 | `0xF2` | NONZERO_WITH_COUNT | `0x0100484C` |
| 16 | 0 | `0x7B` | DEQUANT | `0x01004DC4` |

The core compares the packed `(spec, opcode)` key and `callx8`'s `funcVA`. The
1-byte opcode column **is** the `TONGA_ISA_TPB_OPCODE` low byte (no translation).
`EXTENDED_INST 0xF0` is the custom-op entry: five rows differ only by the `spec`
byte (`{0,1,2,4,3}`) — a two-level dispatch into `dispatch_extended_inst`. A
GPSIMD custom op's loaded kernel is the `funcVA` reached via `(spec, 0xF0)`. All
funcVAs lie in `[0x01000000, 0x01006F1E)` (the IRAM CODE segment).

### 3.5 Contrast — the two relocators (do not conflate) `[HIGH]`

| axis | §1.9 NEFF `kbin_patch` (libnrt) | §3 UCPL prelink (libnrtucode) |
|------|---------------------------------|--------------------------------|
| what is relocated | NEFF per-engine 64-B sequencer SLOT stream | a 32-bit Xtensa PI ELF (the Q7 ext-ISA kernel) |
| table format | `kbin_patch_location_t` (16 B `{off,count,type,section}`) | `Elf32_Rela` (12 B `{off,info,add}`) |
| entry "type" | INSERT/MODIFY/DELETE (0/1/2) | NONE/RELATIVE/SLOT\*\_OP/\_ALT (0/5/20../35..) |
| granularity | whole 64-B slots | Xtensa instruction immediates + 32-bit data words |
| direction | build+trace, device-PC ↔ NEFF-IP (index, reverse) | load-time, link-VA → device-VA (address-exact, forward) |
| failure semantics | **WARN-only** (trace aid) | **FATAL** — aborts the install |
| consumer | `exec_print_engine_ip` / fault trace | `nrtucode_ll_create` / Q7 install |

They share **nothing** but the word "relocation". §1.9 is the NEFF-side debug map;
§3 is the correctness-critical device-ELF loader.

### 3.6 CORRECTION — type 5 is `R_XTENSA_RELATIVE`, not `R_XTENSA_32`

> **CORRECTION.** The Part-8 host-prelinker page
> `runtime/prelinker-ucpl.md` (#832/#816) and the DX-NEFF-10 backing report name
> reloc type 5 **`R_XTENSA_32`**, deliberately standardizing on that label to
> match the device-blob histogram. This capstone names it **`R_XTENSA_RELATIVE`**,
> agreeing with the sibling consumer page
> [`neff-elf-relationship.md`](./neff-elf-relationship.md) (which carries the
> native `ncore2gp` Tensilica `readelf -rW` mnemonic) and the binary evidence:
>
> 1. The Xtensa psABI fixes `1 = R_XTENSA_32`, `5 = R_XTENSA_RELATIVE`. Type 5 is
>    therefore **not** `R_XTENSA_32` by value — `R_XTENSA_32` is type **1** (which
>    does not appear in this image).
> 2. All 240 relocations have `sym=0` and `DT_STRSZ=1` (empty string table). The
>    type-5 apply is `*word = reloc_addr(*word + r_addend)` — it **reads the
>    existing word** and rebases it by the segment delta. That is the base-relative
>    `B + A` semantics of `R_XTENSA_RELATIVE`; it is **not** the symbol-resolved
>    `S + A` store of `R_XTENSA_32` (there is no symbol to resolve).
>
> The two labels describe the **same code path** (`prelink_relocate_lib @0x9b6160`,
> type-5 branch) and the **same value 5** — the disagreement is purely the ELF enum
> name. Part-8 standardizes on `R_XTENSA_32` to match its "device-blob ground
> truth"; this capstone and #881 standardize on `R_XTENSA_RELATIVE` to match the
> psABI value mapping and the symbol-less, read-then-rebase semantics. *These pages
> are not edited here; the divergence is recorded as a NOTE only.*
> `[CORRECTION·HIGH — re-grounded: 240 relocs sym=0, type-5 apply reads in-place word, psABI 5=RELATIVE]`

---

## 4. The I/O ABI — metaneff (HOST) + var/mem_ref (DEVICE) `[HIGH·OBSERVED]`

A loadable model has **two I/O key-rings on a shared index space**:

- the **device** var table (NEFF tar, §1.6): `var_id → mem_ref{type,size,buffer,
  dtype,shape}`; the dense `0..N-1` invariant.
- the **host** metaneff (a *separate* proto3 string, **not** in the tar) handed to
  `torch.classes.neuron.{Model,SPMDModel}(neff_bytes, metaneff_bytes, …)`. Native
  binary of record: `libtorchneuron.so`.

### 4.1 The metaneff proto schema (proto3, package `metaneff`) `[HIGH·OBSERVED]`

| msg | field (#, tag) | type |
|-----|----------------|------|
| `MetaTensor` | 1 `name`(0x0A), 2 `shape`(rep int64,0x12), 3 `data_type`(enum,0x18), 4 `content`(0x22), 5 `allow_dynamic_batch_size`(0x28), 6 `checkpoint_key`(0x32, proto3-opt), 7 `type`(enum,0x38), 8 `user_input_key`(0x42, proto3-opt) | — |
| `ModelConfig` | 1 `num_infer`, 2 `timeout`, 3 `optimal_ncg_size`, 4 `async_load`(0x20), 5 `lazy_load`(0x28), 6 `return_aliases`(0x30) | — |
| `MetaNeff` | 1 `input_tensors`(rep,0x0A), 2 `output_tensors`(rep,0x12), 3 `model_config`(0x1A), 4 `serialized_graph_def`(HLO,0x22), 5 `name`(0x2A), 6 `output_aliases_to`(map<i64,i64>,0x32), 7 `num_user_inputs`(0x38), 8 `num_states`(0x40), 9 `num_weights`(0x48) | — |

`MetaTensor.DataType` (value 11 reserved gap): `UNDEFINED=0 FLOAT=1 INT32=2 BYTE=3
STRING=4 BOOL=5 UINT8=6 INT8=7 UINT16=8 INT16=9 INT64=10 FLOAT16=12 DOUBLE=13
BFLOAT16=14 F8E4M3FN=15 F8E5M2=16`. `MetaTensor.Type`: `UNDEFINED_TYPE=0
USER_INPUT=1 INPUT_STATE=2 INPUT_WEIGHT=3`.

### 4.2 The binding contract `[HIGH·OBSERVED]`

```
MetaTensor index i (inputs then outputs) == NEFF var_id i == nrt tensor-set ordinal i == device mem_ref[i]
```

`MetaTensor.name` must be exactly `"input{i}"`/`"output{i}"` (the runtime
`set_input`/`set_output` lookup key). `MetaTensor.type` partitions which device
slots the host fills from user args vs internal checkpoint state/weights:
`USER_INPUT(1) → MR_INPUT(5)`, output → `MR_OUTPUT(6)`, `INPUT_STATE(2)` +
`checkpoint_key` → `MR_SB(1)`, `INPUT_WEIGHT(3) → MR_BUFFER(3)`.
`output_aliases_to` (map<output_index, input_index>) = donated/in-place buffer
reuse. **Weights are not in the metaneff — they ride in the NEFF tar (§1.6).**

> **GOTCHA — NRT status `1003` is success-ish.** `nrt_allocate_tensor_set` /
> `nrt_tensor_allocate` / `nrt_execute` tolerate `1003` (pending/async); any other
> nonzero throws. `[HIGH·OBSERVED — metaneff-io-abi §4.3]`

---

## 5. The version / feature-bits compatibility model — three tiers `[HIGH·OBSERVED]`

A NEFF carries compat at three independent tiers, gated by different functions in
load order; distinct error codes make the failing tier diagnosable.

### 5.1 Tier 1 — container (`neff_parse @0x4ca3f0`)

`neff_version_major` must be `0..2` (hard ceiling; else code 10
`NRT_UNSUPPORTED_NEFF_VERSION`, `"accepted version range: 0.x-2.x"`).
`pkg_version ∈ {1,2}` (else code 10 `"Unsupported NEFF packager"`). Data fit:
`file_size - 1024 ≥ data_size` (else code 2 `NRT_INVALID`). Integrity (`a3`):
pkg1 SHA256 / pkg2 MD5 over `data[data_size]`; mismatch → code 2.

### 5.2 Tier 1 — the `feature_bits` forward-compat trapdoor (only armed when `major==2`)

```
if ((feature_bits & 0x7FFFFFFFF8000000) != 0)  →  code 10
    "...compiled by a newer version of Neuron compiler.
     Features supported by this Neuron Runtime: 0x7FFFFFF."
```

Exact arithmetic, re-grounded from `neff_parse`'s `constants_used` this pass:
mask `0x7FFFFFFFF8000000` (= the literal `9223372036720558080`) = **bits 27..62**
(the trapdoor); supported `0x7FFFFFF` (= `134217727`) = **bits 0..26** (what the
runtime understands); `mask & supported = 0` (disjoint), `mask | supported` =
bits 0..62; bit 63 untested. The trapdoor printf is at `0x4ca7ac`. The compiler
opts a forward feature in by setting a high bit; every predating runtime
deterministically **refuses** rather than mis-executing. **No override.** (major
0/1 predate the bitmap and bypass the check; the printf passes the literal `"2"`.)

### 5.3 Tier 2 — architecture target (`kelf::parse_target @0x497c10` + `kelf::load @0x497dc0`)

`kelf-a.json "target"` → `al_hal_tpb_arch_type`: `"sunda"/"v2"(0x3276)→SUNDA(2)`,
`"cayman"/"v3"(0x3376)→CAYMAN(3)`, `"mariana"/"v4"→MARIANA(4)`, `"*"→current
instance arch`, `""→SUNDA(2)`; else `"Invalid target '%s'"` → `NRT_INVALID`.
`kelf::load` compares target vs the live instance arch; mismatch → **hard reject**
(`NEFF_ARCH_INCOMPAT`, `"--target"` hint) **unless** `NEURON_RT_ALLOW_LEGACY_NEFF=1`
(WARN + proceed). Target `"*"` always matches (the fixture's target). `kelf-a.json
"version"` (`"0.5"`) is parse-only, not a gate.

> **NOTE — three arch numberings.** Do not conflate the SW/HAL `arch_type`
> ordinals (`SUNDA=2 CAYMAN=3 MARIANA=4`), the hardware `arch_id` codename bytes
> (`0x05 0x0c 0x14 0x1c`), and the ExtISA-lib-ID `CSWTCH.113 = {6,13,21}` (indexed
> by `arch_type−2`). They are three distinct scales. `[HIGH·OBSERVED]`

### 5.4 Tier 3 — per-section

- **GPSIMD ucode-lib**: MAJOR-equality semver vs the runtime constant
  `"1.21.1.0"` (`.rodata 0x84fe68`, byte-exact `31 2e 32 31 2e 31 2e 30`; major must
  == 1; mismatch → `"Mismatch between GPSIMD lib version and NRT version!"`).
  `cpu_id == 0` (assert `kelf2kbin.cpp:0x691`); `total_cpus ∈ {1,8}` (1 = one Q7
  core, 8 = the cluster); dup-name reject; staging caps (≤97 functions global,
  name ≤64, ≤28/core). **Not overridable.** The version JSON key is
  `ulib_to_ucode_version` (present in `libnrt.so`).
- **SB carveout** (EVTACCEL, `kbl_model_add @0x3058e0`, CAYMAN only): strict value
  contract `{start_partition 0, num_partitions 128, fixed offset, size 8}`; type
  string must be `evtaccel` (qword `0x6C65636361747665`); off-contract →
  `NRT_INVALID`; absent → allowed. `sb_carveout` (40 B):
  `type@0 offset@8 size@16 start_partition@24 num_partitions@32`.
- **Legacy-SB guard**: missing/corrupt `runtime_statebuffer_reservation` on a
  requiring arch → reject unless `NEURON_RT_ALLOW_LEGACY_NEFF=1`.

### 5.5 Skew policy

Precedence **container → arch → section**; distinct codes (`NRT_INVALID=2`,
`NRT_UNSUPPORTED_NEFF_VERSION=10`) + cause (`NEFF_ARCH_INCOMPAT`). Forward-incompat
is **always** a clean refusal. Override surface: `NEURON_RT_ALLOW_LEGACY_NEFF=1`
relaxes **only** the arch gate and the legacy-SB guard — never the `feature_bits`
trapdoor or the ucode semver gate. The integrity hash is **unkeyed** (integrity,
not authentication): a recomputed hash on a modified NEFF passes; there is **no
cryptographic root of trust** in the NEFF load path.

---

## 6. The reimplementer read / write CHECKLIST `[HIGH]`

### TO READ a NEFF

1. Read 1024 B; interpret `neff_header_t` (§1.1). Verify `header_size==0x400`,
   `version_major ≤ 2`, `pkg_version ∈ {1,2}`, `file_size-1024 ≥ data_size`; for
   `major==2` check `(feature_bits & 0x7FFFFFFFF8000000) == 0`; optionally verify
   the hash (pkg1 SHA256 / pkg2 MD5 over the `data[data_size]` **compressed** bytes).
2. Take `data[0..data_size]`; if pkg2 gunzip the **whole** stream with zlib
   `inflate` (§2 — one gzip member, **not** per-section); walk the GNU-ustar tar;
   strip leading `"./"`; skip dirs and the `wavegraph-bin.json`/checksum side-files;
   build `name → (buf,size)`.
3. Parse `kelf-a.json` (graphs/target/version); apply the Tier-2 arch gate. Per
   graph parse `sgNN/def.json` keys in order (§1.5): `var`, `dma_queue`,
   `replica_groups`/`src_target_pairs`, `ucode_lib`,
   `runtime_statebuffer_reservation`, `n_config`, per-engine `{dma[], instr→.bin}`.
4. Build the var table (dense `var_id 0..N-1`, `num_outputs > 0`). Resolve `"file"`
   vars: `.npy` → verify magic `0x4e93`, data at `header+(hlen+10)`; else raw `.bin`
   → `MR_BUFFER` (§1.6). Stage `{MR_SB, MR_BUFFER, MR_TMP_BUF, MR_VIRTUAL_TMP_BUF}`
   to HBM/SBUF.
5. Decode each `<engine>.bin` as 64-B slots (8-B SP slots for `sp.bin`):
   `byte0 = opcode = base|(engine<<5)`, `byte1 = 0x10` word_len, then events
   (4/8 B) + payload (§1.7). RT-class (`0xC1`/`0xC8`/`0xCA`) are PSEUDO placeholders
   to be relocated.
6. For a GPSIMD custom op: take the `ucode_lib` `"library"` bytes; verify they are
   an Xtensa PI ELF32 (`e_machine 94`, `ET_EXEC`, the 3-LOAD + 1-DYNAMIC shape,
   §3.2); apply the semver/`cpu_id`/`total_cpus` gates (§5.4); prelink + relocate
   per §3.3 (the 12-B `Elf32_Rela` walk, types `{0, 5=R_XTENSA_RELATIVE, 20, 35}`,
   segment-delta remap via `reloc_addr`, per-arch bounds, **all relocs sym-less**)
   into IRAM/DRAM; emit the UCPL header (§3.4); install via the POOL load-sequence;
   the kernel dispatches via `kernel_info_table` on `(spec, 0xF0)`.
7. The metaneff (if present, separate) gives the host I/O ordering (§4):
   `MetaTensor` index `i` == `var_id i`.

### TO WRITE a NEFF

1. Emit each engine's pseudo `.bin` (64-B slots; RT pseudos for DMA/collective
   triggers carrying queue names / tensor ids; SP as 8-B slots).
2. Emit `def.json` (var table with dense `var_id 0..N-1` and `num_outputs > 0`,
   `dma_queue`, `ucode_lib` manifest if a custom op, `sb_reservation` if EVTACCEL
   on CAYMAN, `n_config` for FP8).
3. For a custom op: ship the Q7 kernel as an Xtensa PI ELF32 (3 LOAD + DYNAMIC,
   `DT_RELA` relocations, all **sym-less** — `R_XTENSA_RELATIVE`(5) for data words,
   SLOT\*\_OP/\_ALT pairs for instruction immediates; `p_align 0x80`; major version
   `1` to match `"1.21.1.0"`); name it via `ucode_lib "library"`; opcode `0x85`
   (= `-123`) for built-in else a user ext-ISA opcode; `total_cpus ∈ {1,8}`.
4. Pack the GNU-ustar tar (`kelf-a.json` with target `"*"` or a concrete arch +
   version `"0.5"`; `neff.json`; `sgNN/*`; `ucode_lib.json` + the kernel ELF; weight
   `.npy`/`.bin` per constant var). gzip the **whole** tar as **one** stream (pkg2).
5. Fill `neff_header_t`: `pkg_version=2`, `header_size=0x400`, `data_size` (the
   **compressed** length), `neff_version 2.minor`, `feature_bits =` OR of features
   used (keep within bits 0..26 for the target runtime), `uuid`, `name`,
   `vnc_size`; compute `MD5(data[data_size])` (over the **gzip output**) →
   `hash[0..15]`.
6. Concatenate `[1024-B header][gzip data]`. Ship the metaneff alongside (**not** in
   the tar) for the torch host path (§4).

---

## 7. The struct + enum catalog (one table) `[HIGH·OBSERVED]`

| struct | size | owner | key fields |
|--------|-----:|-------|------------|
| `neff_header_t` | 1024 | container | pkg_version/header_size/data_size/version/hash/uuid/feature_bits §1.1 |
| `neff_t` | 48 | container | `std::map<string,(buf,size)> files` |
| `kbin_mem_ref` | 152 | var table | mr_type/name/size/var_id/dtype/shape[8]/`buffer@+0x80`/union §1.6 |
| `mem_ref` (runtime, `new 0x88`) | ~208 | var table | resolved relocation record (`buffer@+0x58`, PA/dmem) |
| `MetaTensor` / `MetaNeff` (host proto) | — | metaneff | name/shape/data_type/type · input/output_tensors/aliases §4.1 |
| `TONGA_ISA_TPB_INST_HEADER` | 4 | seq | opcode/inst_word_len(0x10)/debug_cmd/hint |
| 64-B TPB slot | 64 | seq | header4 + events(4/8) + payload §1.7 |
| SP slot | 8 | seq | `{opcode:7,phase:1}` + payload §1.7 |
| `PSEUDO_TRIGGER_COLLECTIVE` | 64 | seq | op/dtype/group/in/out/num/ctype |
| `kernel_info_entry` | 8 | device ELF | `{0,0,spec,opcode,funcVA}` §3.4 |
| `ucode_lib` (runtime) | 80 | ucode | name/content/flags/cpu_id/total_cpus §3 |
| `kbin_ucode_lib_function` | 258 | ucode | name[256]/opcode/sub_opcode §3 |
| `kbin_dma_desc` | 5456 | dma | type/op/to/from[32]/cce/block §1.8 |
| `kbin_dma_ring_instance_t` | 264 | dma | name[256]/ndesc/desc[] §1.8 |
| `kbin_patch_location_t` | 16 | reloc | offset/count(slots)/type/section §1.9 |
| `kbin_patch_info_t` | 80 | reloc | `eng_patch[5]` @`model_t+0x18A0` |
| `sb_carveout` | 40 | version | type/offset/size/start_part/nparts §5.4 |
| `Elf32_Phdr` (device) | 32 | prelink | p_type/offset/vaddr/filesz/memsz/flags §3.2 |
| `Elf32_Rela` (device) | 12 | prelink | r_offset/r_info/r_addend §3.3 |
| `dyn_info` (prelink) | 64 | prelink | seg bases/boundary/rela\*/count/split §3.3 |
| `UCPL header` | 32 | prelink | magic/code_seg_len/data_seg_len/init/fini §3.4 |

**Key enums** `[OBSERVED]`:

```
kbin_mr_type          INVALID0 SB1 BUFFER_STAGED2 BUFFER3 TMP_BUF4 INPUT5 OUTPUT6
                      PTR7 VIRTUAL_TMP_BUF8 LIST9 PTR_TABLE10 REMOTE11
al_hal_tpb_eng_type   PE0 ACT1 POOL2 DVE3 SP4 (MAX5)
TONGA_ISA_TPB_ENGINE  PE0 ACT1 POOL2 ALL3 RT6 SIM7
kbin_dma_ring_type    ENG0..LAST18 (CUSTOM_OP16)
kbin_patch_type       INSERT0 MODIFY1 DELETE2
kbin_patch_section    PREAMBLE0 POSTAMBLE1 MAIN2 FUNCATION3   (binary misspelling)
al_hal_tpb_arch_type  INVALID0 _1 SUNDA2 CAYMAN3 MARIANA4 (NUM5)
kbin_sb_carveout_type INVALID0 EVTACCEL1
R_XTENSA (device)     NONE0  RELATIVE5  SLOT{0..14}_OP=20..  SLOT{0..14}_ALT=35..
MetaTensor.DataType   UNDEFINED0 FLOAT1 INT32_2 BYTE3 STRING4 BOOL5 UINT8_6 INT8_7
                      UINT16_8 INT16_9 INT64_10 FLOAT16_12 DOUBLE13 BFLOAT16_14
                      F8E4M3FN15 F8E5M2_16
MetaTensor.Type       UNDEFINED_TYPE0 USER_INPUT1 INPUT_STATE2 INPUT_WEIGHT3
NRT_STATUS            NRT_INVALID2  NRT_UNSUPPORTED_NEFF_VERSION10
```

---

## 8. Ten facts a reimplementer must hold `[HIGH]`

1. **Two-level self-contained package**: a 1024-B C-struct header + an in-memory
   gzip-GNU-ustar tar (libarchive, no temp files, no literal magic). `pkg_version`
   selects raw-tar + SHA256 (1) vs gzip-tar + MD5 (2); the hash is integrity-only,
   optional, **unkeyed**, and (pkg2) over the **compressed** bytes.
2. The codec is **zlib-only** (inflate 1.3.1, statically vendored, inflate-only),
   reached once via libarchive's gzip filter; the **whole** archive is one gzip
   stream (pkg2); **no** per-section codec, **no** lzma/bz2/zstd/lz4. pkg1 bypasses
   inflate entirely.
3. `feature_bits` bits **27..62** are a forward-compat trapdoor: any high bit ⇒ a
   clean refusal, not silent mis-execution. Armed only for `major==2`. No override.
4. The var table (`mem_ref`/`kbin_mr_type`) is the **device** I/O ABI; the metaneff
   is the matching **host** proto key-ring (**not** in the tar). `MetaTensor` index
   `i` == `var_id i` == nrt-tensor ordinal `i`; weights live **only** in the NEFF tar.
5. Engine `.bin` = 64-byte-slot TPB **sequencer** microcode, `opcode =
   base|(engine<<5)`, **not** Xtensa (PE/ACT/POOL/DVE; SP is a distinct 8-byte ISA).
   Missing engines = 0-byte placeholders. RT-class pseudos are runtime-patched,
   never hardware-executed.
6. The GPSIMD custom-op **kernel** is a 32-bit Xtensa PI ELF32 (3 LOAD + DYNAMIC),
   distinct from the engine `.bin`. It is prelinked/relocated (`Elf32_Rela`; types
   `NONE/RELATIVE/SLOT*_OP/_ALT`, **all sym-less**) into device IRAM/DRAM by
   `libnrtucode_internal`, wrapped in a `"UCPL "` header, and dispatched on-device
   via the POOL `EXTENDED_INST 0xF0` + Q7 `kernel_info_table` `(spec,opcode)→funcVA`.
   **This device reloc is FATAL-on-fail.**
7. The NEFF-side `kbin_patch` table (INSERT/MODIFY/DELETE × PREAMBLE/POSTAMBLE/MAIN/
   FUNCATION) is a **separate, WARN-only** debug/trace PC→source map — do not
   conflate with the device-ELF relocator (§3.5).
8. The per-engine loaded image is **assembled** (PREAMBLE/MODEL_SWITCH/MAIN/
   POSTAMBLE/FUNCTION) by `ib_create_one_block` and DMA-staged to HBM by the **same
   path as weights** (`dmem_alloc_aligned` + `dmem_buf_copyin`). The `def.json`
   `.bin` is only the MAIN body.
9. Weights/constants ride as tar members named by the var table; `numpy_load`
   strips the v1 npy header (magic `0x4e93`); they become `MR_BUFFER` mem_refs
   DMA-staged into HBM (`TONGA_DRAM`); `MR_SB` lives in the 32 MiB on-chip SBUF AXI
   aperture (no copy).
10. Three-tier compat (container → arch → section) with distinct error codes and one
    diagnosable failing tier; overrides exist only for the arch / legacy-SB gates
    (`NEURON_RT_ALLOW_LEGACY_NEFF=1`), never the feature trapdoor or the ucode semver.

---

## 9. The compiler producer seam `[MED·CARRIED]`

The container documented here is **emitted** by the Neuron compiler's packager.
The libnrt loader strings name the compiler indirectly (`"--target … neuronx-cc"`),
and the fixture header's `neff_build_version` reads
`"2.0.21884.0%kaena-tools/develop@…"`. The write side itself — the
`NeffPackager::writePackageFile` / `NeffFileWriter::initializeNeffHeader` /
`writeArchiveFile` seam, conventionally the *walrus* `neff_packager` — lives in the
compiler/back-end library `libwalrus.so`, **not** in this runtime extraction, so it
is tagged **CARRIED** (grounded by [`container-byte-format.md`](./container-byte-format.md)
from `libwalrus.so` symbols; not re-pinnable from `libnrt.so` alone — the runtime
has zero `NeffPackager`/`initializeNeffHeader` strings). The producer mirror of
every rule above (MD5-over-compressed, dense `var_id`, `feature_bits` in bits 0..26,
GNU-ustar packing, the §6 write checklist) is the Part-12 page, cited as
`compiler/neff-packager.md`.

---

## 10. Open items `[LOW]`

- The per-bit names within `feature_bits 0..26` (only the boundary 27 and the
  supported mask `0x7FFFFFF` are OBSERVED; the producer's `initializeNeffHeader`
  pins the names).
- `translate_one_pseudo_instr_v2`'s exact per-pseudo expansion factor `N` (the
  INSERT/DELETE structure is OBSERVED; the count `N` is data/queue-dependent at runtime).
- The 8-B NEURON_ISA event-mode numeric enum names (wait `0x04` / update `0x13`
  decoded operationally; the formal 8-B-dialect constants were not located as a
  shipped header).
- `relocate_op`'s exact per-FLIX-slot immediate bit-map for each sub-format (the
  shift/mask constants are observed; each maps to a specific `ncore2gp` TIE slot
  layout not re-derived field-by-field).
- `numpy_load` handles only v1 npy headers (`u16 hlen`); v2/v3 large-weight headers
  unproven.
- Whether any shipped Q7 image is ever big-endian (the byte-swap path exists; all
  16 embedded blobs observed are `EI_DATA==1` little-endian); and whether a
  NEFF-supplied `ucode_lib` BIN is byte-identical in format (INFERRED — none present
  in the corpus to read byte-exact).
