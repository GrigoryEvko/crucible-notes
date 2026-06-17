# The 13 Q7 Microcode Blobs

> *All blob offsets, header bytes, section VMAs, and digests on this page apply to `libnrtucode_extisa.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (host build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, ELF64 x86-64 DYN, 9,656,488 B, **stripped** — `nm -D`/symtab-only, **no DWARF**). The host `.rodata` is at `0xb000` with VMA == file offset, so every `0x…` host offset below is **both** an analysis VMA and a file offset; a blob carves directly with `dd skip=<offset> count=<size>`. The carved Xtensa **ELF32** objects are unstripped-enough to keep their `.comment`, `kernel_info_table`, and `.xt.prop.<mangled>` section names, but carry **no `.symtab`** (the per-kernel name survives only as a section name). Other versions will differ.*
>
> *Evidence grade: **Confirmed (byte-anchored)** — the 14 ELF magics, every carved ELF32 header (`readelf -h`), the `v4 ≡ v4_plus` byte-identity (`cmp`), every `sha256[0:16]`, the `kernel_info_table` 8-byte-record decode, the SUNDA manifest↔table join, and the demangled `.xt.prop` rosters were re-derived from the binary this pass and agree with [Dispatch Tables](dispatch-tables.md) (zero CORRECTIONs). The IVP/TIE *instruction* bodies are not decoded — they need the proprietary Vision-IVP32 `.tie` config (see [IVP Catalog](ivp-isa-catalog.md)). · Part XI — GPSIMD / Q7 Microcode & ISA · [back to index](../index.md)*

## Abstract

`libnrtucode_extisa.so` does not store the GPSIMD pool kernels as host functions — it embeds them as **13 complete Tensilica-Xtensa ELF32 `EXEC` objects** baked into its `.rodata`, one device microcode library per `{arch, role}` slot. Scanning the host file for the `\x7fELF` magic finds exactly **14** images: the host x86-64 object at offset `0`, then 13 little-endian Xtensa ELF32 objects (`e_machine = Tensilica Xtensa`, `e_flags = 0x300`, `XtensaTools-14.09 clang 10.0.1`). These are the "Q7" pool-engine ulibs the [dispatch layer](dispatch-tables.md) hands back through the `{body_getter, hdr_getter}` getter pair and the [microcode loader](microcode-loader.md) relocates into device IRAM/DRAM behind a `"UCPL "` header.

The 13 blobs are not 13 distinct images. They are **9 distinct images plus 4 byte-identical re-points**: a single **SUNDA** library (arch6, the only tier shipping a real JSON opcode manifest) and a 4-way-split **Cayman** family at three silicon tiers — **v3** (arch13), **v4** (arch21), and **v4_plus** (arch29). A `cmp` of every v4 library against the matching v4_plus library shows them **byte-for-byte identical** (`v4_plus` re-points at the v4 bytes), while v3 differs in `.text` size and codegen. So the inventory collapses to `SUNDA + v3×4 + v4×4 = 9` distinct images. This is the structural evidence behind the `MARIANA_PLUS = MARIANA-silicon-running-v4-ucode` binding the [dispatch page](dispatch-tables.md) records.

Each blob is a self-contained Xtensa C-runtime image with three artifacts a reimplementer must read to launch a kernel. (1) The **`.text`** (PROGBITS R+X @ device VMA `0x01000000`) holds the pool kernels, each a textbook Xtensa-LX windowed routine beginning `entry a1,<frame>` (opcode byte `0x36`) and ending `retw.n`. (2) The **`kernel_info_table`** (PROGBITS @ device VMA `0x0200xxxx`) is the on-device **opcode→entry map**: an array of 8-byte records, each a big-endian pool opcode joined to a little-endian `.text` entry VA. The on-device dispatcher indexes this table by the incoming Neuron-ISA pool opcode and makes a windowed call to the entry. (3) The **`.xt.prop.<mangled>`** sections — one per kernel — carry the kernel's **mangled C++ symbol** as the section name, the only surviving source of per-kernel names in the stripped images. This page is the **carve and inventory + table-format** page for those three artifacts; the relocation machinery is owned by [microcode-loader](microcode-loader.md), the IVP op encoding by [ivp-isa-catalog](ivp-isa-catalog.md).

For reimplementation, the contract is:

- **The carve** — 14 `\x7fELF` magics in the host file, 13 Xtensa ELF32 at the offsets §2 lists; each blob is `host.bytes[off : off + size]` (`.rodata` VMA == file offset), header `readelf -h`-validatable as ELF32 / LE / EXEC / EM=Xtensa / `e_flags 0x300`.
- **The 9-distinct collapse** — `v4_libN ≡ v4_plus_libN` byte-for-byte (`cmp`), v3 distinct; so 13 blobs = 9 images. A reimplementer ships 9 and re-points the 4.
- **The `kernel_info_table` record** — 8 bytes = `[u32 BE opcode][u32 LE entry_va]`, `N = section_size / 8` records, at section VMA `0x0200xxxx`; the dispatcher does `entry = table[lookup(opcode)].entry_va`, windowed-`call`.
- **The `.xt.prop` name model** — per-kernel mangled C++ symbol preserved as a section name despite the stripped `.symtab`; demangle to recover the kernel roster.
- **The manifest vs stub split** — SUNDA ships a real 1728-byte JSON opcode→name manifest; the 12 Cayman header slots ship a 32-byte `{"dummy_message":"hello world"}` stub, so the Cayman opcode→name join is by the in-blob table, not a shipped manifest.

| | |
|---|---|
| **Carrier** | `libnrtucode_extisa.so`, build-id `7bb03bc4…`, 9,656,488 B, stripped (no DWARF) |
| **Store location** | host `.rodata` @ `0xb000` (VMA == file offset); blobs span `0x14020`..`0x92e968` |
| **ELF magic count** | **14** = 1 host x86-64 (`@0x0`) + **13** Xtensa ELF32 (`EM=Xtensa`, `e_flags 0x300`) |
| **Distinct images** | **9** = SUNDA + Cayman v3×4 + v4×4 (`v4_plus×4` ≡ v4, `cmp`-identical) |
| **Toolchain (`.comment`)** | `XtensaTools-14.09 clang version 10.0.1` — every blob |
| **Device `.text` VMA** | `0x01000000` (R+X); `kernel_info_table` @ `0x0200xxxx` (WA) |
| **`kernel_info_table` record** | 8 B = `[u32 BE opcode][u32 LE entry_va]`; `N = size/8` |
| **SUNDA manifest** | real JSON @ host `0x920fa0` (1728 B), `ulib_to_ucode_version 1.21.1.0`, 17 ops / 18 records |
| **Cayman header** | 32-B stub `{"dummy_message":"hello world"}` @ host `0x5b0ca0` (×12) |
| **In-blob TIE proof** | `_TIE_xt_ivp32_xb_vec2Nx8U` in `cptc_decode_impl<1..6>` (lib3 only) |

---

## 1. The Carve and the 9-Distinct Collapse

### Purpose

Before reading any kernel, a reimplementer must answer three questions host-side: which bytes are a blob, how long it is, and which `{arch, role}` slot it serves. The first two are answered by the ELF magic scan and the getter size word; the third by the [dispatch tables](dispatch-tables.md) (`arch_id − 6 → T0..T3`, `lib_idx*16` into the handler table). This section establishes the container model — the carve, the ELF32 header that validates it, and the `cmp`-proven collapse from 13 stored blobs to 9 distinct images — so the inventory and table-format sections that follow rest on a fixed footing.

### The carve

`.rodata` is at host `0xb000` with VMA == file offset, and each blob is a contiguous ELF32 object whose size is the value the dispatch getter returns (`movq $size,(%rsi)` in the body getter, e.g. SUNDA `0xd308`). So the carve is mechanical — no container descent, no decompression:

```c
// reconstruct the inventory from libnrtucode_extisa.so
function carve_q7_blobs(host_elf):
    rodata = section(host_elf, ".rodata")               // @0xb000, VMA == file offset
    magics = scan(host_elf.bytes, b"\x7fELF")           // → 14 offsets (1 host + 13 Xtensa)
    for (arch, lib_idx, off, size) in dispatch_slots():  // {body,hdr} getter pair → off, size
        blob = host_elf.bytes[off : off + size]          // off is the file offset
        assert readelf_h(blob).machine == "Tensilica Xtensa"
        assert readelf_h(blob).e_flags == 0x300 and blob.elfclass == ELF32
        record(arch, lib_idx, off, size, sha256(blob), kernel_count(blob))
```

The carved header is the validation hook. Every blob's `readelf -h` reads identically except for `e_entry` and section sizes — ELFCLASS32, 2's-complement little-endian, `Type EXEC`, `Machine Tensilica Xtensa Processor`, `Version 0x1`, `e_flags 0x300`, `e_phentsize 32` (4 program headers), `e_shentsize 40`, `e_shnum 17` (SUNDA). The SUNDA header verbatim: `e_entry 0x010000c8`, `e_phoff 52`, `e_shoff 53344`, `.text` at device VMA `0x01000000` size `0xa9be` (43,454 B), `kernel_info_table` at device VMA `0x02000760` size `0x90`.

> **NOTE —** `e_entry` is the C `_start` / dispatch trampoline (a windowed `entry a1,32` prologue at the ELF entry), **not** a pool kernel. The pool kernels are reached only through `kernel_info_table` entries (§3). A reimplementer that jumps to `e_entry` to run a kernel will land in the runtime's startup, not in `pool_copy`. The entry trampoline's own body is an IVP/TIE bundle (`op0=4`) that is **not** decoded here — it needs the `.tie` config. CONFIDENCE: **HIGH** (`e_entry` read from each header; the kernel path is the table, byte-confirmed in §3).

### The 9-distinct collapse

The 13 blobs serve four arch tiers, but the silicon tiers share code. A `cmp` of each v4 library against the matching v4_plus library is **byte-identical** for all four libs; v3 differs (smaller `.text`, different codegen). So `v4_plus` is not a fifth image set — it re-points the dispatch handler table `T3` at the same blobs `T2` (arch21/v4) returns. The inventory is therefore:

```text
13 stored blobs                              9 distinct images
  SUNDA  lib0                       ─────────  SUNDA lib0                (arch6)
  v3     lib0 lib1 lib2 lib3        ─────────  v3 lib0..lib3             (arch13, distinct)
  v4     lib0 lib1 lib2 lib3        ──┐
  v4_plus lib0 lib1 lib2 lib3       ──┴──────  v4 lib0..lib3             (arch21 ≡ arch29)
                                              (v4_plus re-points at v4 bytes — cmp-identical)
```

> **QUIRK —** `v4_plus_libN` is **byte-for-byte identical** to `v4_libN` for every `N` (`cmp` returns equal; the `sha256[0:16]` match in the inventory table — `9f2ce049…` for lib0, etc. — is the same on both tiers). The 13-vs-9 gap is not redundant *content*, it is redundant *dispatch slots*: arch21 and arch29 both route to the same library bytes (the [dispatch page](dispatch-tables.md)'s `T2`/`T3` handler tables point their getters at distinct host addresses that nonetheless hold identical blobs). A reimplementer collapses to 9 distinct images and maps both arch21 and arch29 onto the v4 set; v3 must be kept separate because its `.text` size and digest differ. CONFIDENCE: **HIGH** (`cmp` of all four pairs; sha256 equal). The redundancy is stated here once — the inventory table does not re-list the four v4_plus rows as separate images.

---

## 2. The Blob Inventory

### The inventory table

Carved this pass from host `.rodata` (VMA == file offset); size from the dispatch body-getter `movq $size` word; `sha256[0:16]` recomputed from the carved bytes; `#K` = `kernel_info_table` record count (`section_size / 8`); `#xtp` = `.xt.prop.<mangled>` section count. The four `v4_plus` slots are folded into the v4 rows per §1; their host offsets (`0x014020`/`0x01a9c0`/`0x01bee0`/`0x01ce60` for lib3/lib2/lib1/lib0) re-point at byte-identical content. Confidence is **HIGH** for every row — offsets, headers, digests, and counts all reproduce against the binary.

| Image | arch_id | role | host off / VMA | size | `sha256[0:16]` | `#K` | `#xtp` |
|---|---|---|---|---|---|---|---|
| SUNDA lib0 | 6 | full pool set (real manifest) | `0x921660` | `0xd308` (54,024) | `444497066f5e1738` | 18 | 5 |
| v3 lib0 | 13 | main-compute | `0x5b0cc0` | `0xa260` (41,568) | `910d41c3ededce67` | 17 | 21 |
| v3 lib1 | 13 | iota-only | `0x5afd40` | `0x0f5c` (3,932) | `469c24137cc5fe08` | 1 | 2 |
| v3 lib2 | 13 | cross-lane-reduce | `0x5ae820` | `0x1500` (5,376) | `efd06876f5701b97` | 2 | 4 |
| v3 lib3 | 13 | CPTC / conv-LUT / dequant | `0x5a7e80` | `0x6974` (26,996) | `052ac31c4e096212` | 9 | 19 |
| v4 lib0 | 21 ≡ 29 | main-compute | `0x2ffee0` | `0xa260` (41,568) | `9f2ce049608c0a88` | 17 | 21 |
| v4 lib1 | 21 ≡ 29 | iota-only | `0x2fef60` | `0x0f5c` (3,932) | `3c2dea8c7359d1ea` | 1 | 2 |
| v4 lib2 | 21 ≡ 29 | cross-lane-reduce | `0x2fda40` | `0x1500` (5,376) | `e8564f9c0eb648b2` | 2 | 4 |
| v4 lib3 | 21 ≡ 29 | CPTC / conv-LUT / dequant | `0x2f70a0` | `0x6974` (26,996) | `8477ff2690f30cc3` | 9 | 19 |

The 14 verbatim ELF magic offsets in the host file (`grep -abo '\x7fELF'`): `0x0` (host) · `0x14020 0x1a9c0 0x1bee0 0x1ce60` (v4_plus lib3..0) · `0x2f70a0 0x2fda40 0x2fef60 0x2ffee0` (v4 lib3..0) · `0x5a7e80 0x5ae820 0x5afd40 0x5b0cc0` (v3 lib3..0) · `0x921660` (SUNDA) — no hidden 15th blob.

### The four-way role split (Cayman)

The Cayman family is split into four single-purpose libraries; SUNDA bundles the equivalent kernels into one. The split is read off the `kernel_info_table` opcode sets and the `.xt.prop` rosters (§4), not a shipped manifest:

| lib | role | pool opcodes (from `kernel_info_table`) |
|---|---|---|
| lib0 | main-compute | `65,69,70,71,81,82,123,124,125,126,190,242` + extended `240,496,752,1008,1264` |
| lib1 | iota-only | `126` (`pool_iota`) |
| lib2 | cross-lane-reduce | `124,125` (arith / bitvec) |
| lib3 | CPTC / conv-LUT / dequant | `69,123,124,125,126,190,228,242,2032` (228 = CPTC decode; 2032 = extended) |

> **GOTCHA —** the Cayman opcode→**name** join is not a shipped artifact. SUNDA's header getter returns a real JSON manifest, but all 12 Cayman header getters return the 32-byte `{"dummy_message":"hello world"}` stub ([dispatch-tables](dispatch-tables.md) §3). The opcode *values* in the `kernel_info_table` are exact (decoded byte-for-byte), and the `.xt.prop` names are exact, but pairing a Cayman opcode to a `.xt.prop` kernel is by role/size inference, not a manifest. The real Cayman opcode→library map lives in `libnrt`'s `ext_isa_ucode_lib_def` (a different cell). CONFIDENCE: **HIGH** (opcodes + names), **MED** (Cayman opcode↔name pairing). The large opcodes `240/496/752/1008/1264/2032` are extended-instruction encodings; opcode 240 @lib0 is a stub `entry/movi.n/retw.n` placeholder whose real body is at opcode 496 (HIGH for the stub-vs-real split, MED for the full ext-opcode→kernel table).

---

## 3. The `kernel_info_table` Format and Lookup

### Purpose

The `kernel_info_table` is the **host-interface** to a blob: it is how the on-device dispatcher turns an incoming Neuron-ISA pool opcode into a `.text` entry address and launches the kernel. A reimplementer who reproduces this one structure — the 8-byte record, the endianness split, the windowed-call convention — can drive any of the 9 images by opcode. It is the GPSIMD analogue of a syscall dispatch vector: a flat opcode-keyed table of code entries, indexed once per pool instruction.

### The record format

The section is `N × 8` bytes at device VMA `0x0200xxxx` (e.g. SUNDA `0x02000760` size `0x90` → 18 records; v3 lib0 `0x02000380` size `0x88` → 17; v3 lib3 `0x020008c8` size `0x48` → 9). Each record is **mixed-endian** — a quirk that bites a reimplementer who reads the whole record as one word:

| Offset | Field | Type | Meaning | Confidence |
|---|---|---|---|---|
| `+0x0` | `opcode` | u32 **big-endian** | the Neuron-ISA pool opcode the device dispatcher matches on | HIGH |
| `+0x4` | `entry_va` | u32 **little-endian** | the `.text` entry address (device VMA `0x010xxxxx`) of the kernel | HIGH |

The first SUNDA record verbatim (`xxd` of the section, file offset `0xb260`): `00 00 00 e7 | 48 07 00 01`. The opcode is `0x000000e7` read big-endian = **231** (`pool_indirect_copy`); the entry is `0x01000748` read little-endian. Read the whole 8 bytes as one LE u64 and the opcode field becomes `0xe7000000` — wrong by a byte-swap. The split is deliberate: the opcode is stored network-order (the host pool-ISA convention), the entry is stored native Xtensa little-endian (a code address the device fetches directly).

### The lookup

```c
// kernel dispatch on the Q7 device: opcode → entry → windowed call.
// Models the on-device dispatcher reading kernel_info_table @ device VMA 0x0200xxxx.
struct kinfo_rec {                               // 8 bytes, section is N of these
    u32 opcode_be;                               // +0x0  big-endian pool opcode
    u32 entry_va_le;                             // +0x4  little-endian .text entry (0x010xxxxx)
};

function dispatch_pool_op(blob, pool_opcode):    // pool_opcode = incoming Neuron-ISA opcode
    kinfo = section(blob, "kernel_info_table");  // base @ device VMA 0x0200xxxx
    n     = kinfo.size / 8;                       // record count (SUNDA: 0x90/8 = 18)
    for i in 0 .. n-1:                            // linear / table scan over records
        rec = kinfo[i];                           // 8-byte record at kinfo.base + i*8
        if bswap32(rec.opcode_be) == pool_opcode: // compare host-order opcode
            entry = rec.entry_va_le;              // .text VA, already native LE
            // windowed-ABI launch: loader frames the args, jumps to the kernel.
            // every kernel begins `entry a1,<frame>` (byte 0x36) and ends `retw.n`.
            return windowed_call(entry);          // e.g. op231 → 0x01000748
    return DISPATCH_MISS;                          // opcode not implemented by this lib
```

Two records can share one entry: SUNDA `op67` and `op68` both decode to entry `0x0100a0d8` (one kernel, two opcodes — `pool_tensor_scalar_arith_op`). So the 18 records expose 17 distinct kernels. The lookup must treat the table as `opcode → entry` many-to-one, not a 1:1 array.

> **NOTE —** every kernel entry begins with the Xtensa-LX windowed prologue `entry a1,<frame>` (opcode byte `0x36`; the frame size is in the next bytes — `41 00` = 32-byte frame, `01 02` = 256-byte frame for the large DMA/memset kernels), and ends `retw.n` (`1d f0`). This was spot-decoded on 30+ entries across all blobs. It is decisive proof these are Xtensa LX windowed-ABI routines and that the host-interface is a standard windowed call: the loader sets up the frame and jumps to `table[opcode].entry`. The kernel **body** (the IVP/TIE vector loop) is *not* decoded — past the first IVP bundle (`op0=4`) the base-ISA decoder desyncs; it is flagged opaque and left to [ivp-isa-catalog](ivp-isa-catalog.md). CONFIDENCE: **HIGH** (prologue/epilogue bytes), opaque body by design.

---

## 4. The Per-Image Kernel Roster (`.xt.prop` names)

### Purpose

The stripped blobs carry no `.symtab`, so the only per-kernel names are the `.xt.prop.<mangled>` section names — one section per kernel, named with the kernel's mangled C++ symbol. Demangling them recovers the roster: which pool operations each library implements, and (for the join) which kernel a `kernel_info_table` entry runs. This section gives the demangled rosters and the SUNDA manifest-to-table join.

### The `.xt.prop` name model

`.xt.prop.*` is an Xtensa property section (the toolchain's per-function alignment/literal metadata); the compiler emits one per function and names it `.xt.prop.<function-symbol>`. Because the section *name* lives in the section-header string table (`.shstrtab`), it survives the `.symtab` strip that removes the symbol itself. A reimplementer recovers the roster with `readelf -SW | grep .xt.prop` then `c++filt` — there is no other name source in the blob.

> **NOTE —** the `.xt.prop` section gives the kernel's *name* and a property record, **not** a guaranteed `.text` offset; only `kernel_info_table` entries are address-exact. So the roster names *what* a library implements; the table says *where*. For named kernels not in the table (helpers like `cross_lane_reduce_impl`, `clr_reduce_local`), the name is recovered but the entry VA is not table-anchored. CONFIDENCE: **HIGH** (names), the helper `.text` VAs **LOW** (not table-bound).

### SUNDA — manifest joined to the table

SUNDA is the only tier with a real opcode manifest (JSON @ host `0x920fa0`, 1728 B, `ulib_to_ucode_version 1.21.1.0`). Joining the manifest's `{name, opcode}` to the in-blob `kernel_info_table` gives the full opcode→name→entry roster — names and table agree exactly (17 names ↔ 18 records, `op67≡op68`):

| opcode | manifest name | entry VA |
|---|---|---|
| 231 | `pool_indirect_copy` | `0x01000748` |
| 116 | `pool_tensor_scalar_addr` | `0x01001904` |
| 103 | `pool_pool_buffer_load` | `0x010023c4` |
| 104 | `pool_gather` (`ic_util::send_gather_request`) | `0x01002590` |
| 70 | `pool_copy` | `0x01002700` |
| 71 | `pool_cast` | `0x01002844` |
| 184 | `dma_memcopy` (frame 256) | `0x01002f80` |
| 187 | `dma_memcopy_indirect` | `0x0100474c` |
| 126 | `pool_iota` (`iota_kernel<true/false>`) | `0x01005e80` |
| 65 | `pool_tensor_tensor_arith_op` (`tensor_tensor_arith`) | `0x01006398` |
| 124 | `pool_cross_lane_reduce_arith` | `0x01007d30` |
| 125 | `pool_cross_lane_reduce_bitvec` | `0x01007d48` |
| 73 | `pool_memset` (frame 256) | `0x01007fc4` |
| 122 | `pool_load_pool_argument` | `0x010084dc` |
| 121 | `pool_embedding_update` (`embedding_update`) | `0x01008520` |
| 67 / 68 | `pool_tensor_scalar_arith_op` (shared) | `0x0100a0d8` |
| 146 | `pool_tensor_scalar_affine_select` | `0x0100a118` |

SUNDA carries only **5** `.xt.prop` sections (a subset of the 17 kernels — the toolchain emits a property section only where the function needs one): the demangled names are `ic_util::send_gather_request(NEURON_ISA_TPB_ADDR4, unsigned short*, unsigned int, NEURON_ISA_TPB_DTYPE, unsigned int, bool)`, `void iota_kernel<true>()`, `void iota_kernel<false>()`, `tensor_tensor_arith(unsigned int, NEURON_ISA_TPB_ALU_OP, unsigned int)`, and `embedding_update(embedding_update_lib::embed_update_info*, unsigned short)` — the SUNDA-unique gather + embedding-table-update ops.

### Cayman rosters (demangled `.xt.prop`)

The Cayman libraries carry richer `.xt.prop` rosters (no manifest; names only). The distinct kernel families across the four libs:

- **lib0 / main-compute (21 sections):** `iota_impl<true/false>`, `pool_cross_lane_reduce_arith()`, `cross_lane_reduce_impl(bool)`, `clr_reduce_local(u32, u8, NEURON_ISA_TPB_REDUCE_OP, float)`, `decode_pool(bool)`, `decode_tensor_tensor_arith(u32)`, `setup_64bit_rw(u32, NEURON_ISA_TPB_ALU_OP)`, `tensor_tensor_arith_impl(u32,u32,u32)`, `tensor_tensor_64bit_dispatch<VectorInt64/VectorUint64>(...)`, `tensor_tensor_64bit_bitvec_dispatch<...>(...)`, `pool_extended_inst_copy()`, `decode_extended_inst_tensor_tensor_arith(bool,u32)`, `get_sequence_bounds_impl(u32, NEURON_ISA_TPB_DTYPE)`, `nonzero_with_count_impl<float/int>(...)`, `decode_tensor_dequantize(bool)`, `TensorDequantize::proc_4bit_mx_8(u32)`.
- **lib1 / iota-only (2):** `iota_impl<true>`, `iota_impl<false>`.
- **lib2 / cross-lane-reduce (4):** `pool_cross_lane_reduce_arith()`, `pool_cross_lane_reduce_bitvec()`, `cross_lane_reduce_impl(bool)`, `clr_reduce_local(...)`. The op124/op125 entries are thin trampolines that each `call0` the shared `cross_lane_reduce_impl(bool)` → `clr_reduce_local(...)`; the bool selects arith vs bitvec.
- **lib3 / CPTC-conv-dequant (19):** the lib0 helpers plus `pool_conv_lut_load()` and the CPTC decoder — the **only** in-blob carrier of a TIE *type* name.

> **QUIRK —** the single hard, in-blob proof of the IVP32 TIE vector-register class lives only in lib3, in the six `cptc_decode_impl<1..6>` instantiations. The demangled signature is `void cptc_decode_impl<(unsigned char)N>(unsigned int, _TIE_xt_ivp32_xb_vec2Nx8U, _TIE_xt_ivp32_xb_vec2Nx8U, unsigned short, unsigned short, unsigned short, unsigned char, bool)` — the CPTC (compute-record) decoder takes **two** `_TIE_xt_ivp32_xb_vec2Nx8U` (2N×8-bit-unsigned) IVP vector registers as arguments. No `_TIE_*`/`IVP_*` type name appears in SUNDA / lib0 / lib1 / lib2 (verified by a strings sweep of each carved blob); their IVP usage is via untyped intrinsics inside `.text`, flagged opaque in disasm and not symbolically resolvable without the `.tie` config. A reimplementer mapping the IVP vector ABI has exactly one symbolic anchor — this `cptc_decode_impl` signature in lib3. CONFIDENCE: **HIGH** (the mangled name `_Z16cptc_decode_implILh1EEvj25_TIE_xt_ivp32_xb_vec2Nx8US0_ttthb` demangles as shown; absence elsewhere verified by sweep).

---

## Related Components

| Name | Relationship |
|---|---|
| `kernel_info_table` (in every blob) | The on-device opcode→entry map this page decodes; 8-byte mixed-endian records |
| `.xt.prop.<mangled>` sections | The per-kernel name carrier surviving the `.symtab` strip; the roster source |
| SUNDA JSON manifest (`@0x920fa0`) | The one real opcode→name manifest; the Cayman headers are stubs |
| `nrtucode_get_ext_isa` / `T0..T3` getters | The [dispatch](dispatch-tables.md) layer that returns these blobs by `arch_id` + `lib_idx` |
| `nrtucode_ll_create` (loader) | The [loader](microcode-loader.md) that relocates a carved blob into device IRAM/DRAM |

## Cross-References

- [The IVP Vector ISA Catalog (1065 Ops)](ivp-isa-catalog.md) — the IVP/TIE op space the kernel bodies are built from; `_TIE_xt_ivp32_xb_vec2Nx8U` and the FLIX formats the disasm here leaves opaque
- [The Microcode Loader (RELA + FLIX, UCPL)](microcode-loader.md) — what `ll_create` does with these blobs: `.rela.*` relocation, FLIX immediate encode, the `"UCPL "` device header
- [ext-ISA Dispatch Tables and arch-id Scheme](dispatch-tables.md) — the `arch_id − 6 → T0..T3`, `lib_idx*16` routing that selects which of these 13 blobs (9 images) a request gets; the SUNDA-manifest-vs-Cayman-stub split
- [Embedded NCFW Payloads (8 RAW Xtensa Blobs)](../firmware/embedded-payloads.md) — the *sibling* carrier's header-less RAW blobs, the contrast that makes these ELF-wrapped Q7 images notable
- [back to index](../index.md)
