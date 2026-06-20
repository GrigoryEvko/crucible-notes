# The NEFF ↔ ELF Relationship

> **Scope.** The GPSIMD device ucode — the Cadence Vision-Q7 NX "Cairo" ext-ISA
> kernel image that runs on the 8 Q7 POOL cores — is **not** a NEFF-native blob and
> **not** a flat image at rest. It is a complete **32-bit little-endian Xtensa
> Position-Independent ELF executable**. This page reverses, byte by byte: (1) the
> device ELF header and the `e_machine=94` / `e_flags=0x300` Tensilica identity;
> (2) the **LOAD → IRAM/DRAM** segment-to-region map, which *is* the spatial join
> between the NEFF container and the device program; (3) the in-ELF
> `kernel_info_table` — the device-resident opcode→funcVA dispatch ABI; (4) the
> `.rela.got` relocation table consumed host-side by the prelinker; (5) the two
> delivery channels (resident-default vs NEFF-supplied) and the **UCPL** prelinked
> in-flight form that binds the ELF to the device. The container framing
> (`neff_header_t`, the gzip-tar walk, `def.json`/`ucode_lib` parse) is set in
> [NEFF Container Byte Format](./container-byte-format.md); the host relocator that
> consumes the `.rela.got` is reversed in
> [The Host Prelinker — UCPL](../runtime/prelinker-ucpl.md) and
> [The Ucode Relocation Consumer](../runtime/ucode-relocation-consumer.md).

**Tag legend.** Each claim carries `[CONF × PROV]`: confidence `HIGH/MED/LOW` ×
provenance `OBSERVED` (bytes/struct/string read from a shipped artifact — the
carved device blob via the native `ncore2gp` `xtensa-elf-readelf`, or host
`readelf`/`nm`/`xxd`/`grep` on `libnrtucode_internal.so`), `INFERRED` (reasoned
from observed bytes / decompiled control flow), `CARRIED` (re-grounded from a
sibling page). v2/v3/v4 (CAYMAN/MARIANA/MARIANA_PLUS) facts are byte-grounded;
v5 (MAVERICK) is header-OBSERVED only and called out where it appears.

**Binaries of record.** Host x86-64 ELF `libnrtucode_internal.so`
(`aws-neuronx-gpsimd-customop-lib 0.21.2.0`, 10,276,288 B, not stripped) — carries
both the host-side relocator *and* the 16 embedded device images in its `.rodata`.
The native device toolchain is `XTENSA_CORE=ncore2gp` —
`extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-readelf` /
`…/xtensa-elf-objdump`. The ground-truth device blob is the Xtensa PI ELF carved
from host file offset **`0x2EF7E0`** (= the `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get.data`
symbol).

> **HOST-vs-DEVICE — two distinct address worlds.** Keep them apart. In the host
> `libnrtucode_internal.so`, section VMA ≠ file offset: per `readelf -SW`, `.text`
> VMA `0x9b01a0` / off `0x9af1a0` (Δ=`0x1000`), `.data.rel.ro` Δ=`0x2000`, `.data`
> Δ=`0x3000`. All offsets like `0x2EF7E0` cited as "where a blob lives" are **host
> file offsets** into `.rodata` (which for this lib *does* have VMA==fileoffset in
> the `.rodata` range). The **device** ELF has its own VMA scheme entirely:
> `.text@0x01000000`, data`@0x02000000`, dynamic`@0x03000000`. Never mix them.
> `[HIGH × OBSERVED]`

---

## 0. Headline verdicts — the answers, up front  `[HIGH]`

**Q1 — what form does the device ucode ride in?** A **32-bit LE Xtensa
Position-Independent ELF executable** (`ELFCLASS32`, `EI_DATA=LSB`, `ET_EXEC`,
`e_machine=0x5E=94`, `e_flags=0x300`, 4 program headers). It is *not* a raw image
and *not* a UCPL blob at rest. "UCPL" is the 32-byte header prepended at **load
time** after the host prelinker has flattened the ELF into two `{code,data}`
buffers and patched their immediates. Lifecycle:

```
ELF32-PI (at rest)  --prelink-->  {flat IRAM buf, flat DRAM buf} + UCPL hdr (in flight)
                                  --PIO/DMA-->  device IRAM/DRAM (resident)
```

**Q2 — which segment → which region, and where is the reloc table?** Three LOAD
segments: `R+X` `.text` → device **IRAM**; `R+W` data (`.rodata`/`.data`/
`kernel_info_table`/`.globstruct`/`.bss`) → device **DRAM**; a third `R+W` LOAD +
`PT_DYNAMIC` (`.dynamic` + `.rela.got`) is **never staged** — the host relocator
consumes it. The reloc table is the ELF's `.rela.got` (`Elf32_Rela`, 240 entries),
pointed at by `DT_RELA`/`DT_RELASZ`/`DT_RELAENT`.

**Q3 — how does NEFF/metaneff I/O reach the kernel?** The host var-table /
metaneff binds tensors to dense `var_id` ordinals (see
[NEFF Container Byte Format](./container-byte-format.md)); on the device side the
opcode reaches the Q7 kernel through the in-DRAM `kernel_info_table`, whose 8-byte
rows `{0,0,spec,opcode,u32 funcVA}` are the dispatch ABI. The `kernel_info_table`
is the device-resident end of the I/O ABI; the NEFF var table is the host end.

---

## 1. The device ucode is an Xtensa PI ELF — byte-exact header  `[HIGH × OBSERVED]`

`xxd` of the 32-byte `Elf32_Ehdr` at host file offset `0x2EF7E0`:

```
002ef7e0: 7f45 4c46 0101 0100 0000 0000 0000 0000   .ELF............
002ef7f0: 0200 5e00 0100 0000 1056 0001 3400 0000   ..^......V..4...
002ef800: e89c 0000 0003 0000 3400 2000 0400 2800   ........4. ...(.
002ef810: 2300 2200 ...                             #.".
```

Decoded, and re-confirmed field-by-field by the native `ncore2gp`
`xtensa-elf-readelf -h` on the carved blob:

| `Ehdr` field | bytes | value |
| --- | --- | --- |
| `e_ident` | `7f454c46 01 01 01 00` | `ELFCLASS32`, `EI_DATA=1` (LSB), `EI_VERSION=1` |
| `e_type` | `0200` | `ET_EXEC` (2) |
| `e_machine` | `5e00` | `0x5E=94` — Tensilica Xtensa Processor |
| `e_version` | `01000000` | `0x1` |
| `e_entry` | `10560001` | `0x01005610` |
| `e_phoff` | `34000000` | `52` |
| `e_shoff` | `e89c0000` | `40168` (`0x9CE8`) |
| `e_flags` | `00030000` | `0x300` — Xtensa config selector (density/option mask) |
| `e_ehsize` | `3400` | `52` |
| `e_phentsize` / `e_phnum` | `2000` / `0400` | `32` / `4` |
| `e_shentsize` / `e_shnum` | `2800` / `2300` | `40` / `35` |
| `e_shstrndx` | `2200` | `34` |

`file(1)` independently reports *"ELF 32-bit LSB executable, Tensilica Xtensa,
version 1 (SYSV), dynamically linked, stripped."* All 16 embedded blobs (§2) share
`7f454c46 01 01 01` + `e_type=2` + `e_machine=0x5E` + `e_flags=0x300` (verified by
`xxd` of the 18-byte prefix at each of the 16 `.rodata` offsets). `[HIGH × OBSERVED]`

> **NOTE — "position-independent" means *via relocation*, not PIE/GOT.** The image
> is `ET_EXEC`, yet it carries a rich `.rela.got` (§5). The link-time VAs
> (`0x1000000` code / `0x2000000` data / `0x3000000` dynamic) are **symbolic bases**
> that the host prelinker rebases into the per-core reserved IRAM/DRAM apertures;
> the same image is broadcast to all 8 Q7 cores. Hence load-time relocation rather
> than a fixed layout. `[HIGH × INFERRED]`

> **QUIRK — host-readable Xtensa ELF, but FLIX code.** The `.text` is Vision-Q7
> ext-ISA FLIX (variable-length bundles, the `ncore2gp` config). It is **not** the
> scalar Xtensa-LX of the NCFW management core; do not conflate the two Xtensa
> configs. The carved blob's `.xt.prop.<mangled>` per-function property sections
> (§4) are the FLIX-config marker. `[HIGH × OBSERVED for .xt.prop; CARRIED for FLIX class]`

---

## 2. The resident image set — 16 ELF blobs, 4 arch × 4 ext-ISA libs  `[HIGH × OBSERVED]`

A scan for the 32-bit-LE ELF magic in `libnrtucode_internal.so`
(`grep -aboP '\x7f\x45\x4c\x46\x01\x01\x01'`) returns exactly **16** hits — the
host ELF at offset 0 uses `7f454c46 02` (ELF64) and is excluded. Joining each hit
to its `nm` getter symbol (`*_SO_get.data`) gives a clean **4×4** grid: four
architectures, each shipping four EXTISA libraries:

| arch (core-kind) | EXTISA_0 | EXTISA_1 | EXTISA_2 | EXTISA_3 |
| --- | --- | --- | --- | --- |
| **CAYMAN** (13) | `0x2EF7E0`† | `0x2F9A60` | `0x2FA9E0` | `0x2FBF00` |
| **MARIANA** (21) | `0x5893C0` | `0x593640` | `0x5945C0` | `0x595AE0` |
| **MARIANA_PLUS** (29) | `0x855240` | `0x85F4C0` | `0x860440` | `0x861960` |
| **MAVERICK** (37) | `0x994DE0` | `0x99CDB0` | `0x99DA40` | `0x99ECE0` |

† the ground-truth blob (§§1,3,4,5). All sixteen `*_SO_get.data` symbols are named
`{ARCH}_Q7_POOL_PERF_EXTISA_{0..3}` — the `Q7_POOL` confirms Vision-Q7 POOL-engine
ucode; `PERF` is the perf-default flavor (§ selector). The grid offsets match the
ELF-magic scan 1:1. `[HIGH × OBSERVED]`

Each `EXTISA_N` is paired with an `EXTISA_N_JSON_get.data` manifest blob at the
`.rodata` offset immediately preceding the next SO (e.g.
`CAYMAN_…_EXTISA_0_JSON_get.data @0x2F9A40`). This pass, those four shipped JSON
companions read as a **placeholder stub** — `xxd @0x2F9A40` = the 32 bytes
`{"dummy_message": "hello world"}`, immediately followed by the next ELF magic at
`0x2F9A60`. The per-lib opcode manifest is *not* baked into the resident set; the
SO blob carries its dispatch table **internally** in the ELF `kernel_info_table`
(§4), so the resident default needs no external JSON. The *real* opcode manifest is
the NEFF-supplied `ucode_lib.json` (§6, Channel B). `[HIGH × OBSERVED]`

**The selector** `nrtucode_get_ext_isa_internal @0x9b2b30` indexes a per-arch
table of getter pairs (`nm`-confirmed): `sunda_libs@0x9b8f80`,
`cayman_libs@0x9b8f90`, `mariana_libs@0x9b8fd0`, `mariana_plus_libs@0x9b9010`,
`maverick_libs@0x9b9050`. Each table is an array of `(SO_get, JSON_get)` pairs:
lib index `a4` reads `v11[2*a4]` (SO) and `v11[2*a4+1]` (JSON). The arch tables sit
**0x40 apart** (`0x9b8fd0 − 0x9b8f90 = 0x40` = 8 pointers = 4 libs × 2 getters),
confirming 4 ext-ISA libs/arch. Flavor (`a3`) comes from env `NEURON_UCODE_FLAVOR`
∈ `{debug,DEBUG,test,TEST,perf-default}`. `[HIGH × OBSERVED — nm + xxd]`

**The count gate** `nrtucode_get_num_ext_isa_libs @0x9b2c90` returns 4 for
core-kinds in the bitmask `0x2020202000` — verified bits `{13,21,29,37}` =
Cayman/Mariana/Mariana+/Maverick, the Q7-POOL kinds that prelink — 1 for Sunda
(kind 6, resident, no prelink), else error. `[HIGH × OBSERVED]`

> **GOTCHA — MAVERICK has a *distinct* device memory map.** Carve + native
> `readelf -lW` on the MAVERICK blob (`0x994DE0`): its code LOAD `VirtAddr =
> 0x00000000` (entry `0x594c`), not `0x01000000`. CAYMAN/MARIANA/MARIANA_PLUS all
> use code LOAD `0x01000000` (MARIANA entry `0x1005658`, byte-confirmed). All four
> arches share data LOAD `0x02000000` and dynamic `0x03000000`. A reimplementer
> must read the code base from the blob's own `Phdr`, never assume `0x1000000`.
> `[HIGH × OBSERVED for code-vaddr; INFERRED for the device aperture mapping of kind 37]`

> **GOTCHA — `extracted/` is gitignored.** `fd`/`rg` skip the device blobs and
> sidecars by default; use `--no-ignore` or absolute paths. The native
> `xtensa-elf-readelf`/`objdump` need `XTENSA_CORE=ncore2gp` and `XTENSA_SYSTEM`
> pointing at `…/gpsimd_tools_tgz/tools/ncore2gp/config`.

---

## 3. The ELF segment → device-region map — the spatial join  `[HIGH × OBSERVED]`

Native `ncore2gp` `xtensa-elf-readelf -lW` on the ground-truth blob — **this** is
the "how the device ELF's LOAD segments correspond to the staged device layout"
deliverable:

| Phdr | Type | Off | VAddr | FileSz | MemSz | Flg | Align | → device region |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [0] | LOAD | `0x0100` | `0x01000000` | `0x6F1E` | `0x6F1E` | `R E` | `0x80` | **IRAM** (instruction mem) |
| [1] | LOAD | `0x7080` | `0x02000000` | `0x0450` | `0x048C` | `RWE` | `0x80` | **DRAM** (data mem) |
| [2] | LOAD | `0x7500` | `0x03000000` | `0x0C08` | `0x0C08` | `W E` | `0x80` | host-only (`.dynamic`+rela) |
| [3] | DYNAMIC | `0x7500` | `0x03000000` | `0x00A4` | `0x00A4` | `RW` | `0x4` | host-only (reloc descriptor) |

**Segment → section map** (printed verbatim by `readelf`):

| seg | sections |
| --- | --- |
| 0 (IRAM) | `.text` |
| 1 (DRAM) | `.rodata` `.eh_frame` `.ctors` `.dtors` `.data` `kernel_info_table` `.globstruct` `.bss` |
| 2 / 3 | `.dynamic` `.rela.got` |

Interpretation, joining to the device-write path (the staging detail is
[CARRIED from the prelinker page](../runtime/prelinker-ucpl.md)):

- **seg0** (`.text`, `FileSz == MemSz == 0x6F1E`) is the executable Q7 FLIX code; it
  stages into the per-core IRAM reserved window. `[OBS code; staging CARRIED]`
- **seg1** (data) has `FileSz 0x450 < MemSz 0x48C`: the `0x3C`-byte tail is `.bss`,
  which the loader **zero-pads**. `readelf -SW` confirms `.bss` is `NOBITS`,
  Addr `0x02000450`, Size `0x3C` — and `0x48C − 0x450 = 0x3C` *exactly*. seg1 stages
  into the per-core DRAM window and carries the `kernel_info_table` (§4) and
  `.globstruct`. The **data image lands before the code image** (DRAM-then-IRAM
  write order). `[HIGH × OBSERVED arithmetic; order CARRIED]`
- **seg2/seg3** (`.dynamic` + `.rela.got`) are **never written to device**: they are
  the relocation metadata the host prelinker consumes to patch seg0/seg1 in host
  scratch buffers *before* the device write. This is exactly why
  [`validate_dynamic_load @0x9b71f0`](../runtime/prelinker-ucpl.md) expects the
  `{R+X LOAD, R+W LOAD, R+W DYNAMIC}` shape — it **is** this 4-phdr layout.
  `[HIGH × OBSERVED + CARRIED]`

The staging alignment = `max(p_align)` over LOAD segs = **`0x80`** (all three LOADs
show `Align 0x80`). `[HIGH × OBSERVED]`

> **NOTE — the seg2 reloc payload is self-contained.** seg2 spans
> `[0x3000000, 0x3000000+0xC08) = [0x3000000, 0x3000C08)`. `.dynamic@0x3000000`,
> `.rela.got@0x30000C8` (end `0x30000C8 + 0xB40 = 0x3000C08`), and `DT_PLTGOT =
> 0x3000C08` all land inside — the reloc table's end coincides exactly with the GOT
> base and the segment top. `[HIGH × OBSERVED arithmetic]`

---

## 4. The in-ELF `kernel_info_table` — the device I/O dispatch ABI  `[HIGH × OBSERVED]`

The ELF carries an **actual section** named `kernel_info_table` (`readelf -SW`):

```
[ 7] kernel_info_table  PROGBITS  02000380  007400  000088  00  WA  0  0  8
```

VAddr `0x02000380` (inside the DRAM segment), file off `0x7400`, size `0x88` =
17 rows × 8 B, 8-byte aligned. `xxd @0x7400` decodes byte-exact to the device
dispatch table — each row is `{u8 0; u8 0; u8 spec; u8 opcode; u32 funcVA}` (LE):

| idx | spec | opcode | TONGA mnemonic | funcVA |
| --- | --- | --- | --- | --- |
| 0 | 0 | `0x7E` | IOTA | `0x01000080` |
| 1 | 0 | `0x7C` | CROSS_LANE_REDUCE | `0x010003F8` |
| 2 | 0 | `0x7D` | CROSS_LANE_REDUCE (alt) | `0x01000410` |
| 3 | 0 | `0x45` | POOL | `0x01000B90` |
| 4 | 0 | `0x51` | (tensor-scalar class) | `0x0100105C` |
| 5 | 0 | `0x41` | TENSOR_TENSOR_ARITH | `0x01000F1C` |
| 6 | 0 | `0xF0` | EXTENDED_INST spec 0 | `0x01003370` |
| 7 | 1 | `0xF0` | EXTENDED_INST spec 1 | `0x01003380` |
| 8 | 2 | `0xF0` | EXTENDED_INST spec 2 | `0x01003484` |
| 9 | 4 | `0xF0` | EXTENDED_INST spec 4 | `0x010037A8` |
| 10 | 3 | `0xF0` | EXTENDED_INST spec 3 | `0x01003A60` |
| 11 | 0 | `0x52` | (tensor-scalar class) | `0x01003B40` |
| 12 | 0 | `0x46` | COPY | `0x010040C0` |
| 13 | 0 | `0x47` | CAST | `0x01004160` |
| 14 | 0 | `0xBE` | (cast/transpose class) | `0x01004204` |
| 15 | 0 | `0xF2` | NONZERO_WITH_COUNT | `0x0100484C` |
| 16 | 0 | `0x7B` | DEQUANT | `0x01004DC4` |

Raw first rows for audit (`xxd @0x7400`): `0000007e 80000001` (IOTA→`0x01000080`),
`0000007c f8030001` (`0x7C`→`0x010003F8`), … `0000007b c44d0001`
(DEQUANT→`0x01004DC4`). **Validation:** every `funcVA` lands inside the
`.text`/IRAM segment `[0x1000000, 0x1006F1E)` — the table is internally consistent.
The 1-byte opcode column **is** the `TONGA_ISA_TPB_OPCODE` low byte (no translation
layer). The five `EXTENDED_INST(0xF0)` rows differ only by the spec byte
`{0,1,2,4,3}` — the two-level `(spec, 0xF0)` dispatch into the
`dispatch_extended_inst` handler (the device string
`"dispatch_extended_inst(%d) : num_chans = %0d"` is present in the host lib).
`[HIGH × OBSERVED]`

**Kernel inventory.** The ELF's `.xt.prop.<mangled>` per-function property sections
(readelf section names) name every kernel reached via the table above; demangled
(`c++filt`):

| mnemonic | kernel symbol(s) |
| --- | --- |
| IOTA (`0x7E`) | `iota_impl<true/false>()` |
| CLR (`0x7C/0x7D`) | `pool_cross_lane_reduce_arith()` / `_bitvec()`, `cross_lane_reduce_impl(bool)`, `clr_reduce_local(uint, uchar, NEURON_ISA_TPB_REDUCE_OP, float)` |
| POOL (`0x45`) | `decode_pool(bool)` |
| TENSOR_TENSOR (`0x41`) | `decode_tensor_tensor_arith(uint)`, `tensor_tensor_arith_impl(uint,uint,uint)`, `tensor_tensor_64bit_dispatch<VectorInt64\|VectorUint64>(…, ALU_OP)`, `tensor_tensor_64bit_bitvec_dispatch<…>`, `setup_64bit_rw(uint, NEURON_ISA_TPB_ALU_OP)` |
| EXTENDED_INST (`0xF0`) | `pool_extended_inst_copy()`, `decode_extended_inst_tensor_tensor_arith(bool,uint)`, `get_sequence_bounds_impl(uint, NEURON_ISA_TPB_DTYPE)` |
| NONZERO (`0xF2`) | `nonzero_with_count_impl<float\|int>(uint,int,int,uint,uint)` |
| DEQUANT (`0x7B`) | `decode_tensor_dequantize(bool)`, `TensorDequantize::proc_4bit_mx_8(uint)` |

The `NEURON_ISA_TPB_ALU_OP`/`REDUCE_OP`/`DTYPE` enums in these signatures tie the
device kernel directly to the TONGA/NEURON sequencer ISA. This is the device-side
counterpart of the host var-table I/O ABI: an opcode in the POOL `.bin` sequencer
stream resolves, on-device, to one of these `funcVA`s. `[HIGH × OBSERVED]`

`.globstruct` (VAddr `0x02000408`, size `0x48`, `readelf -SW`) is a per-core data
record in the DRAM segment; its bytes show four `0x10000` dwords + four `0x00FFFFFF`
+ a `0xFFFFFFFF` trailer — the device-side scratch/bounds/IRAM-cache record the
kernels read (the host `memory_bounds` `0x10000`-class apertures mirrored
device-side). `[HIGH × OBSERVED bytes; field semantics INFERRED]`

---

## 5. The relocation table — location, format, types  `[HIGH × OBSERVED]`

**Location** (`readelf -SW`):

```
[33] .rela.got  RELA  030000c8  0075c8  000b40  0c  A  0  0  4
```

VAddr `0x030000C8` (in seg2, the host-only R+W segment), file off `0x75C8`, size
`0xB40`, entry size `0x0C`. Pointed at by `.dynamic` (`readelf -dW`, 15 entries):

| tag | value |
| --- | --- |
| `DT_RELA` (7) | `0x30000C8` |
| `DT_RELASZ` (8) | `2880` (`0xB40`) |
| `DT_RELAENT` (9) | `12` |
| `DT_PLTGOT` (3) | `0x3000C08` |
| `DT_HASH` (4) | `0x30000A4` |
| `DT_SYMTAB` (6) | `0x30000B4` |
| `DT_STRTAB` (5) | `0x30000C4` |
| `DT_STRSZ` (10) | `1` (trivial — stripped) |
| `DT_SYMENT` (11) | `16` |
| `DT_INIT` (12) | `0x1000000` |
| `DT_FINI` (13) | `0x1006F10` |
| `DT_DEBUG` (21) | `0x0` |
| `0x70000000` (DT_XTENSA_*) | `0x3000C0C` |
| `0x70000001` | `0x0` |
| `DT_NULL` (0) | `0x0` |

The `0x70000000`-class tags are the Tensilica DT_XTENSA_* PI-reloc extension
pointers; here the table is fully expressed via `DT_RELA`, so the LOPROC pointer
indexes the GOT/aux at `0x3000C08+`. `[HIGH × OBSERVED]`

> **CORRECTION vs the DX-NEFF-09 source draft (`.dynamic` entry list).** That draft
> listed a third LOPROC tag `0x70000002 = 0` and omitted `DT_SYMENT` and
> `DT_DEBUG`. Native `readelf -dW` shows **15** entries: `DT_SYMENT (11) = 16` and
> `DT_DEBUG (21) = 0` are present; only `0x70000000` and `0x70000001` LOPROC tags
> exist (no `0x70000002`); the 15th is `DT_NULL`. `[HIGH × OBSERVED]`

**Format.** `0xB40 / 0x0C = 240` `Elf32_Rela` entries `{u32 r_offset; u32 r_info;
u32 r_addend}`; `r_info` low byte = reloc type. Raw first entries (`xxd @0x75C8`):

```
r_offset=0x0100001B  r_info=0x23  r_addend=0x0200025C   (SLOT0_ALT, type 35)
r_offset=0x0100001E  r_info=0x14  r_addend=0x0200025C   (SLOT0_OP,  type 20)
00000000 00000000 00000000                              (NONE padder, type 0)
00000000 00000000 00000000                              (NONE padder)
```

**Types** — native `ncore2gp` `xtensa-elf-readelf -rW` histogram, ground-truth over
all 240:

| type | mnemonic | count | meaning |
| --- | --- | --- | --- |
| 0 | `R_XTENSA_NONE` | 8 | table padding (skipped) |
| 5 | `R_XTENSA_RELATIVE` | 30 | 32-bit data-word relative fixup |
| 20–34 | `R_XTENSA_SLOT{0..14}_OP` | 101 | instruction-immediate, **low** half |
| 35–49 | `R_XTENSA_SLOT{0..14}_ALT` | 101 | instruction-immediate, **high** half (`>>16`) |

The `8 / 30 / 101 / 101` split matches the host relocator's decode exactly. The
SLOT0_OP / SLOT0_ALT pairing — each address split into a low-half and a high-half
immediate — is visible directly in the dump: every site appears as an `ALT` (high)
+ `OP` (low) pair against the same data symbol (e.g. `0100001B SLOT0_ALT` /
`0100001E SLOT0_OP`, both `+0x200025C`). `[HIGH × OBSERVED]`

> **CORRECTION vs siblings #832 / #816.** Those pages name type 5
> **`R_XTENSA_32`**. The **official `ncore2gp` Tensilica `readelf` mnemonic is
> `R_XTENSA_RELATIVE`** — confirmed by the histogram above (30 ×
> `R_XTENSA_RELATIVE`). The semantics are the same one those pages describe
> (addend-add-then-rebase of a 32-bit data word); only the printed name differs.
> The shared reloc-type-set anchor `{0=NONE, 5, 20–34=SLOT*_OP, 35–49=SLOT*_ALT}`
> still holds; reconcile the *name* of type 5 toward `R_XTENSA_RELATIVE` (the value
> `5` is unchanged). `[HIGH × OBSERVED]`

**Consumer** (the device analog of ELF dynamic relocation, host-resident — see
[The Host Prelinker](../runtime/prelinker-ucpl.md) and
[The Ucode Relocation Consumer](../runtime/ucode-relocation-consumer.md)):
`prelink_relocate_lib @0x9b6160` walks the 240 entries; each link-VA is rebased by
the code-vs-data segment delta (chosen at the seg boundary); types 20/35 funnel
into `relocate_op @0x9b6660` (the Xtensa per-FLIX-slot immediate writer); type 5
patches a 32-bit data word (unaligned-safe split). A failed reloc **aborts** the
install (correctness-critical), unlike the NEFF `kbin_patch` table (warn-only trace
aid). The `.rela.got` is the forward, load-time, address-exact relocator;
`kbin_patch` is the reverse, trace-time, slot-index one — they share only the word
"relocation". `[symbols OBSERVED; algorithm CARRIED]`

```c
/* prelink_relocate_lib @0x9b6160 — apply the 240 .rela.got entries.
 * Names/offsets are real symbols in libnrtucode_internal.so. */
static int prelink_relocate_lib(pil_t *pil, dyn_info_t *dyn) {
    Elf32_Rela *r = (Elf32_Rela *)dyn->rela;             /* DT_RELA  = 0x30000C8 */
    uint32_t    n = dyn->relasz / dyn->relaent;          /* 0xB40/0x0C = 240     */
    for (uint32_t i = 0; i < n; i++) {
        uint32_t type = r[i].r_info & 0xff;
        if (type == 0 /*R_XTENSA_NONE*/) continue;       /* 8 padders skipped    */

        /* rebase link-VA -> staged scratch-buffer address: pick code vs data
         * delta by which segment r_offset falls in (.text@0x1000000 -> iram_buf,
         * data@0x2000000 -> dram_buf). [CARRIED prelinker page] */
        uint8_t *site = reloc_addr(pil, r[i].r_offset);

        if (type == 5 /*R_XTENSA_RELATIVE*/) {
            /* 32-bit data-word fixup, unaligned-safe store */
            store32_unaligned(site, reloc_value(pil, r[i].r_addend));
        } else if (type >= 20 /*SLOT_OP*/ && type <= 49 /*SLOT_ALT range*/) {
            /* instruction-immediate: write low (OP) or high (ALT,>>16) half into
             * the FLIX slot via the per-slot immediate encoder. */
            relocate_op(site, type, reloc_value(pil, r[i].r_addend));   /* @0x9b6660 */
        } else {
            return ERR_BAD_RELOC;                          /* aborts the install */
        }
    }
    return 0;
}
```

---

## 6. The two delivery channels — resident-default vs NEFF-supplied  `[HIGH]`

A device ELF reaches a Q7 core by **one of two channels**; a reimplementer must
keep them distinct (the NEFF carries the *second*, not the first):

**Channel A — RESIDENT DEFAULT** (installed at `nrt_init`, *not* in any NEFF).
Source: the 16 Xtensa PI ELF blobs embedded in `libnrtucode_internal.so` `.rodata`
(§2), selected by `(arch core-kind, flavor)` via
`nrtucode_get_memory_image @0x9b2960` → `nrtucode_get_ext_isa_internal @0x9b2b30`.
`nrtucode_ll_create @0x9b1a90` runs prelink on the chosen ELF, emits the UCPL pair
(§7), and the runtime PIO-writes it to all 8 Q7 cores at bring-up. This is the
resident base ucode the `kernel_info_table` (§4) belongs to. `[HIGH × OBSERVED]`

**Channel B — NEFF-SUPPLIED `ucode_lib`** (rides inside the NEFF tar). `def.json`
`"ucode_lib"` → a manifest (e.g. `ucode_lib.json`) inside the tar; its `"library"`
key names the device-code BIN, fetched into `ucode_lib.content`. At `kbl_model_add`
the GPSIMD-class arch routes the bytes through the **same** nrtucode
loadable-library path as Channel A.

> **FORMAT IDENTITY `[HIGH × INFERRED]`.** The staging path that consumes
> `ucode_lib.content` is the nrtucode loadable-library API whose only relocator is
> prelink, and prelink's first act — `validate_dynamic_load @0x9b71f0` — **requires**
> the 4-phdr Xtensa-PI ELF shape (§3). Therefore a NEFF-supplied custom-op
> `ucode_lib` BIN is the **same** ELF32-PI format as the resident blobs: the
> compiler emits a Q7 ext-ISA ELF, the NEFF tars it, the runtime prelinks it exactly
> as Channel A. This is INFERRED — no NEFF-supplied BIN is present in *this* corpus
> subset to read byte-exact — but the format constraint is OBSERVED in the
> `validate_dynamic_load` gate. The NEFF-supplied `ucode_lib.json` is the **real**
> opcode manifest (the resident JSON companions of §2 are dummy stubs); its
> `"opcode"`/version/cpu_id/total_cpus gates bind the custom op to its `(spec,0xF0)`
> `kernel_info_table` row device-side.

**The override seam** (a third, non-NEFF way): `nrt_set_pool_eng_ucode(
nrt_ucode_info{iram{bin,size}, dram{bin,size}})` lets a caller register a raw
`{iram,dram}` pair *before* `nrt_init`; the engine-init substitutes it for the
stock pool ucode with **no signature check**. This seam takes the **already-
prelinked** flat pair (the §7 UCPL output form), *not* an ELF — it bypasses prelink.
So: NEFF/resident channels feed ELFs to prelink; the override seam feeds a
pre-relocated flat pair. `[CARRIED]`

---

## 7. The UCPL prelinked form — the ELF after relocation (in flight)  `[HIGH × OBSERVED + CARRIED]`

`prelink @0x9b5d60` turns the device ELF into the device-install form: not a NEFF
member, not a rest format — the transient output of relocation, described by a
**32-byte UCPL header**. The magic `"UCPL "` is present in the host lib (`grep`
@ file offset `0x9B5BE0`); the field layout is the instruction-anchored one from
[The Host Prelinker §3.6](../runtime/prelinker-ucpl.md):

```c
typedef struct ucpl_header_t {     /* 0x20 = 32 bytes, written to device offset 0 */
    char     magic[8];     /* +0x00  "UCPL " + 3 NUL  (qword 0x204C504355)        */
    uint32_t code_seg_len; /* +0x08  align4(code p_memsz)                          */
    uint32_t data_seg_off; /* +0x0c  0x20 + align32(code_seg_len)                  */
    uint32_t data_seg_len; /* +0x10  align4(data p_memsz)                          */
    uint32_t init_fn;      /* +0x14  pil.init  (= DT_INIT 0x1000000)               */
    uint32_t fini_fn;      /* +0x18  pil.fini  (= DT_FINI 0x1006F10)               */
    uint32_t start_sym;    /* +0x1c  pil.start_sym (the device entry point)        */
} ucpl_header_t;           /* magic 0x204C504355; emitted once per artifact        */
```

The two buffers it describes are **flat** images (no ELF headers): seg0 `.text`
copied+relocated into the IRAM scratch, seg1 data copied+zero-padded
(`.bss`)+relocated into the DRAM scratch. `nrtucode_ll_create @0x9b1a90` then
`mem_write_buf`s three regions to device: the 32-B UCPL header, then `iram_buf`,
then `dram_buf`, broadcast to all 8 Q7 pool cores. The strings
`"Failed prelink with status = %d"` and `"Prelinked library would be larger than
the available buffer on device"` (both present in the host lib) gate this.
`[strings + magic OBSERVED; field layout CARRIED #832]`

> **CORRECTION vs the DX-NEFF-09 source draft (UCPL fields).** An earlier sketch
> labeled `+0x0C` "code_sz_align" and `+0x14` "data_load_va". The byte-exact,
> instruction-anchored layout (#832 §3.6, disassembled from `prelink
> @0x9b5e16..0x9b5e5b`) is the one above: `+0x0C` is `data_seg_off =
> 0x20 + align32(code_seg_len)`, and the `+0x14`/`+0x18` 8-byte store is
> `{init_fn, fini_fn}` (the ELF's `DT_INIT`/`DT_FINI`, here `0x1000000` /
> `0x1006F10`), with `start_sym` at `+0x1C`. Defer to #832. `[HIGH × OBSERVED]`

The full rest→resident transform of one device program:

```
Xtensa PI ELF (seg0 .text / seg1 data / seg2-3 .dynamic+.rela.got)
  -- prelink_load_lib @0x9b5e70 : copy seg0 -> iram_buf ; seg1 -> dram_buf ;
                                  zero-pad 0x3C-byte .bss (memsz 0x48C - filesz 0x450)
  -- prelink_relocate_lib @0x9b6160 : apply 240 .rela.got entries into the buffers
  -- emit ucpl_header_t {magic, code_seg_len, data_seg_off, data_seg_len, init/fini, start_sym}
  -> mem_write_buf(UCPL hdr) ; mem_write_buf(iram_buf -> device IRAM) ;
     mem_write_buf(dram_buf -> device DRAM)        [broadcast to all 8 Q7 pool cores]
  -> kernel_info_table now resident in device DRAM ; .text resident in IRAM.
```

---

## 8. metaneff / var-table → device I/O ABI binding  `[HIGH]`

The host half (set in [NEFF Container Byte Format](./container-byte-format.md)):
metaneff `MetaTensor` index `i` == NEFF `var_id i` == nrt tensor-set ordinal `i`
== device `mem_ref[i]`. This page supplies the **device half** — how that I/O
reaches the Q7 ELF kernel:

1. **Per-inference operand path** `[CARRIED]`. For a GPSIMD custom op the `var_id i`
   operand rides a DMA descriptor on the `KBIN_DMA_RING_TYPE_CUSTOM_OP(=16)` ring;
   the POOL `.bin` sequencer's `PSEUDO_DMA_TRIGGER` slots move SBUF↔HBM operands. The
   kernel does not parse metaneff; it consumes the relocated DRAM operands the
   runtime staged at the `var_id` slots.

2. **Opcode → kernel path** `[OBSERVED §4 + CARRIED]`. The custom op rides the POOL
   TPB-sequencer stream as `EXTENDED_INST(0xF0)`. When the SEQ hands `0xF0` to a Q7
   core, the device runtime linear-scans the in-DRAM `kernel_info_table` (§4) for the
   `(spec, 0xF0)` row and `callx8`s its `funcVA`. The ELF's `kernel_info_table` **is**
   the device dispatch ABI; its opcode column is the TONGA opcode the compiler
   emitted in the POOL `.bin`. The NEFF-supplied `ucode_lib.json` `"opcode"` gate
   assigns a custom op its `(spec,0xF0)` row in a NEFF-supplied ELF's
   `kernel_info_table`.

3. **Load-sequence bridge** `[CARRIED]`. The staged ELF (resident or NEFF) is copied
   into the running Q7 core by a TPB-sequencer micro-program that the runtime
   *inserts* into the POOL preamble — the seam where the relocated device ELF image
   becomes addressable by the POOL sequencer that the var-table I/O drives.

**Binding chain (host → device, one custom op):**

```
host at::Tensor --(metaneff MetaTensor[i].name/type)--> nrt_tensor[i]
  --(NEFF var table, var_id i)--> device mem_ref[i]
  --CUSTOM_OP(16) DMA ring--(POOL .bin PSEUDO_DMA_TRIGGER)--> device DRAM/SBUF operand
AND POOL .bin EXTENDED_INST(0xF0)
  --(kernel_info_table (spec,0xF0), THIS ELF §4)--> Q7 funcVA callx8
  --> the kernel reads the staged operand.
```

metaneff = host key-ring; NEFF var table = device key-ring; ELF `kernel_info_table`
= device opcode→code dispatch. The shared index spaces are the dense `var_id` and
the TONGA opcode.

---

## 9. Struct / offset catalog  `[HIGH]`

| object | size | §/PROV | key fields |
| --- | --- | --- | --- |
| `Elf32_Ehdr` (device) | 52 | §1 OBS | class32/LSB/`ET_EXEC`/mach `0x5E`/flags `0x300`/phnum 4/shnum 35 |
| `Elf32_Phdr` | 32 | §3 OBS | `{type,off,vaddr,paddr,filesz,memsz,flags,align}`; 4 of them |
| PT_LOAD#0 (code) | — | §3 OBS | vaddr `0x1000000` (MAVERICK `0x0`) `R+X` → IRAM |
| PT_LOAD#1 (data) | — | §3 OBS | vaddr `0x2000000` `R+W` → DRAM |
| PT_LOAD#2 + PT_DYNAMIC | — | §3 OBS | vaddr `0x3000000` → host-only |
| `.dynamic` (`Elf32_Dyn[15]`) | `0xA4` | §5 OBS | `DT_RELA/RELASZ/RELAENT/INIT/FINI/HASH/SYMTAB/STRTAB/SYMENT/PLTGOT/LOPROC{0,1}` |
| `.rela.got` (`Elf32_Rela[240]`) | `0xB40` | §5 OBS | `{r_offset,r_info,r_addend}` 12 B; types 0/5/20/35 |
| `kernel_info_table` row | 8 | §4 OBS | `{u8 0; u8 0; u8 spec; u8 opcode; u32 funcVA}`; 17 rows |
| `.globstruct` | `0x48` | §4 OBS | per-core scratch/bounds record |
| `ucpl_header_t` | `0x20` | §7 OBS/CARR | magic `0x204C504355`/`code_seg_len`/`data_seg_off`/`data_seg_len`/`{init,fini}`/`start_sym` |
| `nrt_ucode_info` (override seam) | 32 | §6 CARR | `{iram{bin,size}, dram{bin,size}}` |

---

## 10. End-to-end — one GPSIMD custom op, NEFF bytes to Q7 execution  `[HIGH]`

- **COMPILE** `[CARRIED]`: NKI/HLO custom op → `neuronx-cc` → a Q7 ext-ISA ELF32-PI
  (the device kernel, §1 shape) + a `ucode_lib.json` manifest + the POOL `.bin`
  sequencer stream with an `EXTENDED_INST(0xF0)` at the op site → tarred into the
  NEFF.
- **CONTAINER** `[CARRIED]`: NEFF = 1024-B header + gzip-tar. `def.json
  "ucode_lib"` → the manifest; `"library"` → the ELF BIN tar member.
- **LOAD** `[CARRIED + OBSERVED §§1–7]`: `nrt_init` picks the per-arch resident ELF
  (or, for a custom op, the NEFF-supplied ELF at stage time) →
  `validate_dynamic_load` (4-phdr Xtensa-PI shape, §3) → `prelink_load_lib` (copy
  seg0→`iram_buf`, seg1→`dram_buf`, zero-pad `.bss`) → `prelink_relocate_lib` (240
  `.rela.got` entries, §5) → emit `ucpl_header_t` (§7) → `mem_write_buf` ×3 (UCPL
  hdr, `iram_buf`→IRAM, `dram_buf`→DRAM) broadcast to 8 Q7 cores. The
  `kernel_info_table` (§4) is now resident in device DRAM.
- **EXECUTE** `[OBSERVED §4 + CARRIED]`: `nrt_execute` releases the POOL stream; the
  SEQ hits `EXTENDED_INST(0xF0)`; the Q7 core scans the resident `kernel_info_table`
  for `(spec,0xF0)` and `callx8`s `funcVA`; the kernel reads the `var_id`-staged
  DRAM/SBUF operands; completion via the notification queue.

**The NEFF↔ELF join in one line:** a NEFF tar member `"library"` is an Xtensa PI
ELF; its `R+X` LOAD → device IRAM, its `R+W` LOAD (incl. `kernel_info_table`) →
device DRAM, its `.rela.got` (in the dynamic LOAD) is consumed host-side by
prelink; the `kernel_info_table` row's `(spec,opcode)` is the device-side I/O ABI
the NEFF's var-table/metaneff I/O ultimately dispatches into.

---

## 11. Confidence / open items  `[meta]`

**HIGH × OBSERVED (first-hand, native `ncore2gp` readelf on the carved blob):** the
`ELFCLASS32`/`LSB`/`ET_EXEC`/`e_machine=0x5E` Xtensa ELF with `e_flags 0x300` and
the 4-phdr `{R+X LOAD, R+W LOAD, R+W LOAD(.dynamic+rela), PT_DYNAMIC}` shape; the
segment→section staging map and the `.bss` zero-pad arithmetic
(`0x48C−0x450 = 0x3C`); the 16-blob 4×4 arch grid mapped to the `*_SO_get.data`
offsets, the dummy JSON companions, the selector getter-pair tables and the
`0x2020202000` count-gate bitmask; the `kernel_info_table` decoded byte-exact (17
rows, the five `(spec,0xF0)` rows, funcVAs in-range) and the `.xt.prop` kernel
inventory; the `.rela.got` location/format/240-entry count and the `8/30/101/101`
type histogram; the UCPL magic + the prelink/validate strings.

**HIGH × INFERRED (not byte-pinned in this corpus):** the NEFF-supplied `ucode_lib`
BIN is the same ELF32-PI format (the `validate_dynamic_load` gate requires it; no
NEFF-supplied BIN present here to read byte-exact); `.globstruct` field semantics;
MAVERICK's code-vaddr-`0x0` device aperture mapping (the vaddr is OBSERVED, the
aperture is reasoned).

**LOW / not resolved:** whether any shipped Q7 ELF is ever big-endian (all 16 here
are `EI_DATA=LSB`); the per-FLIX-slot immediate bit map inside `relocate_op`
(CARRIED); a byte-exact NEFF `ucode_lib` tar member (absent from this corpus
subset).

A reimplementer needs §1 (ELF shape) + §3 (segment→region map) + §4
(`kernel_info_table` dispatch) + §5 (`.rela.got`) to **read** a device ELF; §6/§7
(channels + UCPL) to **stage** it; §8 to **bind** it to the NEFF/metaneff I/O ABI.

**Cross-references:** [NEFF Container Byte Format](./container-byte-format.md) ·
[NEFF Container Capstone](./container-capstone.md) ·
[The Host Prelinker — UCPL](../runtime/prelinker-ucpl.md) ·
[The Ucode Relocation Consumer](../runtime/ucode-relocation-consumer.md) ·
[SUNDA arch5 EXTISA ELF](../images/sunda-arch5-extisa.md) ·
[Q7 ELF VADDR + per-core memory model](../abi/q7-elf-vaddr.md).
