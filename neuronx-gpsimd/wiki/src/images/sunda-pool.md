# SUNDA × POOL image (the v2 baseline)

The **SUNDA × POOL** firmware image is the **v2 floor** of the GPSIMD POOL engine
(`engine_idx = 2`, `arch_id 5` / `coretype 6`, NC-v2 — the **oldest** generation). It ships the
*same two-core dual-dispatch structure* as the committed [CAYMAN × POOL](./cayman-pool.md) baseline
— an `NX_POOL` SEQ sequencer (`sunda/seq/`) plus a separate `Q7_POOL` compute core (`sunda/pool/`) —
but it is the **subtractive minimum** of that structure. This page is a **DIFF page**: it leads with
what SUNDA **LACKS** versus CAYMAN, then carves both cores byte-exact and tabulates the per-core
delta. Read [cayman-pool.md](./cayman-pool.md) for the shared dual-dispatch core, the 178-slot SEQ
hub mechanics, and the `kernel_info_table` packed-key format; this page does not re-derive them.

Unlike the v3+ generations, **everything on SUNDA is byte-grounded** — `arch_id 5` is **OBSERVED**
(not the MAVERICK-style `arch_id 36` INFERRED), so no INFERRED-interior caveats apply. Every fact
below is pinned to a carve from `libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`), the runtime
EXTISA container `libnrtucode_extisa.so`, or the static `libnrtucode.a`, and decoded with the shipped
`ncore2gp` `xtensa-elf-objdump`. Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

> **NOTE — the objects used.** Internal container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**; first R `LOAD` identity-maps `off 0x0 == vaddr 0x0`, so each `IMG-PTR` is
> simultaneously the `.rodata` VA and file offset — carve = `so[ptr : ptr+size]`). Runtime EXTISA
> container: `…/aws/neuron/lib/libnrtucode_extisa.so` (sha256 `dc00763d…`), which resolves SUNDA's
> **weak-undefined** EXTISA getters. Static archive: `…/custom_op/c10/lib/libnrtucode.a` (the DEBUG
> superset, used for the seq/pool module enumeration). Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump` (GNU Binutils
> 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo, Xtensa24, RI-2022.9,
> `NX1.1.4`, FLIX/VLIW 32B). All carve sha256, both reset vectors, the 18-entry `kernel_info_table`,
> the absence scans, and the disassembly health were reproduced this session (exit 0). `[HIGH/OBSERVED]`

---

## 1. The headline — the v2 floor (what SUNDA LACKS)

SUNDA × POOL is the **same** `NX_POOL` SEQ + `Q7_POOL` compute two-core engine as CAYMAN, but the
**floor**: every later generation is a strict superset over it. The eight subtractions that define
the v2 baseline, each byte-anchored:

1. **NO `0xF0` ExtendedInst bridge** — on *either* core. The SUNDA `Q7_POOL` `kernel_info_table` has
   **zero** `0xf0` rows; the string `ExtendedInst`/`extended` appears nowhere in either SUNDA POOL
   core. CAYMAN's whole dual-dispatch escape (SEQ `0xf0` → Q7 spec sub-dispatch) **arrives at
   CAYMAN**; SUNDA's two cores are coupled by the external-lib loader / direct-opcode path. `[HIGH/OBSERVED]`
2. **NO RNG** — no `Xorwow`/`Lfsr`/`Pcg`/`Philox`/`Rand` body in the EXTISA SO, the JSON manifest,
   the 18-entry table, or either DRAM (0 hits, four scans). The RNG *enum* is ISA-level; the v2 POOL
   *image* ships no RNG kernel (§7d reconciles). `[HIGH/OBSERVED]`
3. **NO TensorDequantize / cptc / MX** — no `0x7b` (dequant) or `0xe4` (cptc) row, no
   `dequant`/`cptc`/`proc_*bit`/`mx` string (0 hits). `[HIGH/OBSERVED]`
4. **NO SB2SB `0xBF` on-chip collective** — no `0xbf` row, no `sb2sb`/`collective` string (0 hits).
   `[HIGH/OBSERVED]`
5. **NO `0xf2` GetSequenceBounds / NonzeroWithCount**, **NO `0xbe` kernel**, **NO Sort**, **NO
   ConvLutLoad**, **NO gather/2D-transpose** (`0xf1`/`0xbd`). `[HIGH/OBSERVED]`
6. **The handler-observability floor:** the SUNDA `NX_POOL` DEBUG DRAM carries **zero** `'S:'`
   handler-name logs (vs CAYMAN's 187 / 41 distinct handlers) and the Q7 DEBUG DRAM carries **zero**
   `'P%i:'` kernel logs (vs CAYMAN's 156). The per-handler self-naming the SX-IMG-06/11 method
   relies on was **not yet present** at v2. `[HIGH/OBSERVED]`
7. **RELEASE-only packaging:** `internal.so` surfaces **8** SUNDA POOL getters — **no**
   DEBUG/PERF/TEST split, **no** PROF (CAM/TABLE), **no** DKL variant, **no** in-library EXTISA. The
   v2 floor is one production flavor. `[HIGH/OBSERVED]`
8. **The dispatch floor:** the `NX_POOL` SEQ front-end is a **raw-compare chain** (no `addi a2,-65`
   `0x41`-normalization, no 178-slot indexed table); the `Q7_POOL` back-end is **one flat 18-entry
   `kernel_info_table`, all spec=0** (no two-level `(opcode<<24)\|(spec<<16)` escape). `[HIGH/OBSERVED]`

What SUNDA **keeps** (the shared structure with CAYMAN): the dual reset vectors, the shared
`.globstruct` magic `0x6099cb34`, the SEQ control spine (fetch/cache/fsm/branch/move/alu_op),
`dge_reshape`, `soc_window` address translation, and the base pool/tensor/dma compute kernel set.

> **WALL — `arch_id 5` is OBSERVED for SUNDA.** SUNDA is fully byte-grounded; the v2-floor
> subtractions are each anchored to a missing opcode row / missing struct / 0-hit string scan, **not**
> to an INFERRED architecture id. (Contrast the MAVERICK page's `arch_id 36` INFERRED caveat.)
> `[HIGH/OBSERVED]`

> **GOTCHA — the `'S:'`-log set-diff is INAPPLICABLE on SUNDA.** Because the SUNDA `NX_POOL` DEBUG
> DRAM has **no** `'S:'` handler logs, the CAYMAN-page method of enumerating handlers by name-string
> set-diff **cannot run** here. The SUNDA handler inventory is recovered instead from (i) the
> assertion **source-path module set** (§4) and (ii) the Q7 `kernel_info_table` (§5). `[HIGH/OBSERVED]`

---

## 2. The 8 image getters (RELEASE-only) + the 2 weak EXTISA

`nm libnrtucode_internal.so | rg -c 'SUNDA.*POOL.*_get$'` = **8** strong + **2** weak. There is **no**
SUNDA POOL DEBUG / PERF / TEST / PROF / DKL getter (`rg 'SUNDA.*POOL' | rg -i 'DEBUG\|PERF\|TEST\|PROF\|DKL'`
= empty). Each strong getter is the canonical `(img-ptr, size)` stub disassembled from
`.text 0x9b2ea0…0x9b3010`:

```text
SUNDA_Q7_POOL_RELEASE_IRAM_get @ .text 0x9b2fa0:
  48 8d 05 49 5c 69 ff   lea  -0x96a3b7(%rip),%rax   # 0x48bf0  (= IMG-PTR, .rodata identity)
  48 89 07               mov  %rax,(%rdi)
  48 c7 06 d0 42 00 00   movq $0x42d0,(%rsi)          # = SIZE
  c3                     ret
```

### 2a. `NX_POOL` (CLS = NX) — 4 getters (the SEQ sequencer)

| VARIANT | REGION | IMG-PTR (.rodata = file off) | SIZE | STATUS |
| --- | --- | --- | --- | --- |
| RELEASE | IRAM | `0x02be10` | `0x0d040` | REAL (SEQ code; reset `j 0x1dc`) |
| RELEASE | DRAM | `0x038e50` | `0x02730` | REAL (SEQ data; **no `S:` logs**) |
| RELEASE | SRAM | `0x03b580` | `0` | EMPTY (cursor → `NX_SP` IRAM) |
| RELEASE | EXTRAM | `0x03b580` | `0` | EMPTY (cursor → `NX_SP` IRAM) |

### 2b. `Q7_POOL` (CLS = Q7) — 4 getters (the compute shell)

| VARIANT | REGION | IMG-PTR | SIZE | STATUS |
| --- | --- | --- | --- | --- |
| RELEASE | IRAM | `0x048bf0` | `0x042d0` | REAL (Q7 compute shell; reset `j 0x220`) |
| RELEASE | DRAM | `0x04cec0` | `0x0a540` | REAL (Q7 data; **no `P%i:` logs**) |
| RELEASE | SRAM | `0x057400` | `0` | EMPTY (cursor → Q7 EXTRAM) |
| RELEASE | EXTRAM | `0x057400` | `0x01b40` | REAL — **the sole non-zero EXTRAM in the catalog** |

### 2c. The SUNDA EXTISA — weak-undefined in `internal.so`

`SUNDA_Q7_POOL_RELEASE_EXTISA_0_{SO,JSON}_get` are `NOTYPE WEAK UND` (value 0, no body); they resolve
only when the runtime container `libnrtucode_extisa.so` is present. The real SUNDA EXTISA ELF lives in
that container (§5). Contrast CAYMAN, which ships **4** in-library EXTISA SO/JSON getters under PERF.
`[HIGH/OBSERVED]`

> **QUIRK — the sole non-zero EXTRAM in the 386-getter catalog.** `SUNDA_Q7_POOL_RELEASE_EXTRAM` is
> the **only** EXTRAM region in the entire image catalog that carries real bytes (`0x1b40`). Its head
> is `36 61 00` = `entry a1, 48` (a **function prologue**, not a reset vector) and it decodes as real
> windowed-ABI FLIX code (19 `entry` / 55 `retw`, exit 0) — a SUNDA-unique EXTRAM-resident auxiliary
> code band, consistent with the external-lib loader stub set on the Q7 DRAM (§5c). `[HIGH/OBSERVED
> bytes+decode; the loader-stub ROLE INFERRED-HIGH from the loader strings + the entry prologue]`

### 2d. Carve provenance + 2-source byte-identity

Carve rule (identity map): `blob = internal.so[IMG-PTR : IMG-PTR+SIZE]`. The 5 real images carved +
sha256'd this session, each then reconciled byte-identical (`cmp`, sha256 EQUAL) against the matching
`libnrtucode.a` member `.rodata` (`objcopy -O binary --only-section=.rodata`):

| IMAGE | SIZE | sha256 |
| --- | --- | --- |
| `SUNDA_NX_POOL_IRAM` | `0xd040` | `0ef85167…f06b68` |
| `SUNDA_NX_POOL_DRAM` | `0x2730` | `b0a5284e…03f207` |
| `SUNDA_Q7_POOL_IRAM` | `0x42d0` | `9c0a678e…cb2013` |
| `SUNDA_Q7_POOL_DRAM` | `0xa540` | `8a22303a…1fb282` |
| `SUNDA_Q7_POOL_EXTRAM` | `0x1b40` | `da19ac4d…2335fc` |

The `.a` ships **16** SUNDA POOL members (`NX_POOL` 8 + `Q7_POOL` 8 = DEBUG+RELEASE × 4 regions). The
internal `.so` surfaces only the **RELEASE** flavor; the `.a`'s SUNDA **DEBUG** members
(`SUNDA_{NX,Q7}_POOL_DEBUG_{IRAM,DRAM}`) are **not** getters but are extracted here for the module set
(§4). 5/5 RELEASE carves IDENTICAL to the `.a`. `[HIGH/OBSERVED]`

> **NOTE — the IDA v3 sidecars corroborate the contiguity layout.** The decompiled getter
> (`ida/…/context/SUNDA_Q7_POOL_RELEASE_IRAM_get_0x9b2fa0.md`) reads
> `*a1 = &SUNDA_NX_SP_RELEASE_SRAM_get_data; *a2 = 17104 (0x42d0)` — i.e. the Q7 IRAM img-ptr aliases
> the **`NX_SP`** SRAM-cursor data symbol, the byte-level fingerprint that **`NX_SP` is interleaved
> between the two POOL cores** on SUNDA (the order is `…PE → NX_POOL → NX_SP → Q7_POOL`, by contiguity
> arithmetic). All 8 getters have sidecars. `[HIGH/OBSERVED]`

---

## 3. Flat-image geometry + the reset / boot diff vs CAYMAN

SUNDA × POOL ships two flat device packaging forms (`NX_POOL`, `Q7_POOL`) plus the EXTRAM band and the
external EXTISA ELF. The DRAM head word is `34 cb 99 60` (`0x6099cb34`, the `.globstruct` magic —
**byte-identical to CAYMAN**) on both cores. `[HIGH/OBSERVED]`

**(A) `NX_POOL` IRAM — reset `j 0x1dc` (the value is == CAYMAN; the `{v2,v3}` prologue family):**

```text
IRAM head = 06 76 00 00 00 00  86 77 00 00 00 00
  0x000: 06 76 00   j 0x1dc       ; primary reset -> boot
  0x006: 86 77 00   j 0x1e8       ; secondary    -> halt
  0x1dc: const16 a0,0 ; const16 a0,148 (0x94) ; jx a0   -> enter_run @0x94
  0x1e8: halt 0
```

**(B) `Q7_POOL` IRAM — reset `j 0x220` (CAYMAN Q7 was `j 0x200`; a +0x20 shift):**

```text
IRAM head = 06 87 00 00 00 00  86 88 00 00 00 00
  0x000: 06 87 00   j 0x220       ; primary reset -> boot
  0x006: 86 88 00   j 0x22c       ; secondary    -> halt
  0x220: const16 a0,0 ; const16 a0,148 (0x94) ; jx a0   -> enter_run @0x94
  0x22c: halt 0
```

> **The signature of two cores.** `NX_POOL` boots via `06 76 00 00` (`j 0x1dc`, shared with the other
> NX engines); `Q7_POOL` boots via `06 87 00 00` (`j 0x220`), a **distinct** primary-jump byte —
> verified `xxd -l 12` on both IRAM carves. The differing reset bytes are the byte-level proof that
> `NX_POOL` and `Q7_POOL` are separate cores in one engine cluster. `[HIGH/OBSERVED]`

**The boot diff, distilled** (this is the v2-vs-v3 boot signature):

| core | SUNDA reset | CAYMAN reset | SUNDA `enter_run` | CAYMAN `enter_run` | Δ |
| --- | --- | --- | --- | --- | --- |
| `NX_POOL` | `j 0x1dc` | `j 0x1dc` (==) | `@0x94` (148) | `@0x90` (144) | entry **+0x4** |
| `Q7_POOL` | `j 0x220` | `j 0x200` | `@0x94` (148) | `@0x90` (144) | reset **+0x20**, entry **+0x4** |

SUNDA shares the `NX` reset-`j` **value** with CAYMAN (the `{v2,v3}` reset-prologue family) but
relocates its `enter_run` entry-point by `+0x4` and its Q7 reset trampoline by `+0x20`. The v2 boot is
a relocated variant of the same trampoline shape, converging both cores on `enter_run @0x94`. The
SUNDA POOL window base is `0x00100000` (`start_ctrl 0x00100010`, `run_state 0x00100014`) — the v2
window floor, distinct from the CAYMAN-family `0x04000000`. `[HIGH/OBSERVED]`

> **GOTCHA — `enter_run @0x94`, not `@0x90`.** A naive cross-gen reader copying CAYMAN's `enter_run
> @0x90` onto SUNDA is wrong by 4 bytes: `0x1df`/`0x223` read `const16 a0, 148` (`0x94`), not `144`
> (`0x90`). Both SUNDA cores share the `+0x4` entry shift. `[HIGH/OBSERVED]`

**Disassembly health** (shipped `ncore2gp` objdump, exit 0): `NX_POOL` IRAM = 336 `entry` / 106 `retw`;
`Q7_POOL` IRAM = 115 `entry` / 266 `retw`; the EXTRAM band = 19 `entry` / 55 `retw`. Both cores plus
the EXTRAM band carry real windowed-ABI / FLIX-vector code. `[HIGH/OBSERVED]`

---

## 4. The `NX_POOL` SEQ core — raw-compare dispatch, no normalized table

The SUNDA `NX_POOL` front-end uses a **raw-compare** dispatch chain, **not** the `addi`-`0x41`-
normalized indexed table CAYMAN's `NX_POOL` uses. Decoded in the SUNDA NX RELEASE IRAM this session:

- There is **no** `addi a2,a2,-65` (the `'A'`-base normalization) anywhere in the SUNDA NX IRAM (0
  hits). CAYMAN's `NX_POOL` carries `addi a2,a2,-65 ; movi a3,177 ; bgeu` (the indexed 178-slot/55-real
  table, §4a of cayman-pool.md). SUNDA does **not**.
- SUNDA instead emits direct equality compares against the raw opcode, e.g. `@0x1b20`:
  `movi a6, 187` (`0xbb` = `dma_memcopy_indirect`) — the **same numeric opcodes** as the SUNDA Q7
  `kernel_info_table` (§5). This is the raw-compare-chain style (the form CAYMAN's PE engine uses),
  not the normalized indexed jump. `[HIGH/OBSERVED for the addi-absence + the raw-compare immediate;
  MED for "no indexed sub-table anywhere" — the FLIX-desynced linear sweep cannot 100% exclude a small
  hidden indexed table, the documented SX-FW-00 limitation]`

The SUNDA NX SEQ **module set** (recovered from the DEBUG DRAM assertion source paths — the only
handler evidence available, since the `'S:'` logs are absent) is **19 modules** under
`/opt/workspace/NeuronUcode/sunda/seq/` (+ shared `src/decode/`, `src/handlers/`): `alu_op.cpp`,
`branch.cpp`, `branch_prefetch_hint.cpp`, `cache.hpp`, `dge_reshape.cpp`, `error_handler.cpp`,
`error_notifications.cpp`, `evsem_block.hpp`, `exception_handler.hpp`, `fetch.hpp`, `fsm.hpp`,
`interrupt_handler.hpp`, `legacy_dma.hpp`, `move.cpp`, `move_shape.cpp`, `signal_handler.cpp`,
`soc_window.hpp`, `soc_window_manager.hpp`, `translation.cpp`. `[HIGH/OBSERVED]`

So the SUNDA NX SEQ core has the fetch / cache / fsm / branch control spine + move / alu_op decode +
`dge_reshape` (the reshape path **predates** SUNDA — distinct from the v4+ DGE FAST-path) +
`legacy_dma` + `soc_window` address translation + exception/interrupt/signal handlers — the SEQ control
core, but **without** the per-handler `'S:'` self-naming logs and **without** the `ExtendedInst`
bridge handler.

The SUNDA NX SEQ dispatch, as annotated C (the v2-specific raw-compare form; contrast the
indexed-table loop in [cayman-pool.md §4a](./cayman-pool.md)):

```c
/* SUNDA NX_POOL — the v2-floor SEQ raw-compare dispatch (sunda/seq/, NO 'S:' logs).
 * No 0x41-base normalization, no 178-slot indexed table — a direct equality chain
 * over the RAW opcode byte. The same opcode numbers appear in the Q7 kernel_info_table. */
void nx_pool_dispatch_v2(uint8_t opcode, decoded_inst_t *ins) {
    /* NO: addi a2,a2,-65 ; movi a3,177 ; bgeu  (that is the CAYMAN+ indexed path). */
    switch (opcode) {                         /* compiled as a movi/beq compare chain */
        case 0xb8: return dma_memcopy(ins);          /* movi a?,184 ; beq */
        case 0xbb: return dma_memcopy_indirect(ins); /* movi a6,187  ; beq  @0x1b20 */
        case 0x41: return tensor_tensor_arith(ins);
        case 0x43: return tensor_scalar_arith(ins);
        /* ... the v2 base pool/tensor/dma opcodes, raw-compared in sequence ... */
        default:   return error_handler_bad_opcode(ins); /* no 'S:' name emitted */
    }
}
```

---

## 5. The `Q7_POOL` `kernel_info_table` — the authoritative SUNDA kernel set

The SUNDA Q7 kernels live **entirely in the runtime EXTISA container**, not baked into the flat Q7
IRAM (which is only `0x42d0` / 17 KB — the dispatch + external-lib-loader shell). This is why the Q7
EXTISA getters are weak-undefined in `internal.so`.

### 5a. The container ELF

Carved this session from `libnrtucode_extisa.so`:

```text
SUNDA EXTISA_0  foff 0x921660  sz 0xd308  EM_XTENSA(94) ET_EXEC  entry 0x010000c8
  sha256 444497066f5e1738e24d2db6a373f64e13da5625180a1bfcdf97f82f58ab84c0   (== SX-IMG-01 anchor)
+ JSON  foff 0x920fa0  sz 0x6c0  sha256 d282bd4f…  (library "all.stripped.so",
        ulib_to_ucode_version 1.21.1.0, 17 functions)
readelf -SW (ncore2gp):
  [1]  .text             VA 0x01000000  off 0x100    size 0xa9be
  [5]  .data             VA 0x02000080  off 0xab80   size 0x6dc
  [6]  kernel_info_table VA 0x02000760  off 0xb260   size 0x90   (18 entries × 8B)
  [7]  .bss              VA 0x020007f0  off 0xb2f0   size 0x64
  [15] .rela.got         (R_XTENSA funcVA relocs)
```

> **NOTE — one `.data`, no separate `.globstruct`.** Unlike CAYMAN's `EXTISA_0` (which has a separate
> `.globstruct @0x02000408`), the SUNDA EXTISA carries a **single** `.data` section (0 `globstruct`
> sections, `readelf` confirmed). The SUNDA build is the standalone `all.stripped.so`. `[HIGH/OBSERVED]`

### 5b. The 18-entry table (decoded byte-exact, all spec=0)

Record format: `{ u8 0; u8 0; u8 spec @+2; u8 opcode @+3; u32_le funcVA @+4 }`. **All 18 specs are 0**
— one flat linear-scan table, **no** two-level escape. Names from the SUNDA JSON map:

| idx | opcode | spec | funcVA | kernel |
| --- | --- | --- | --- | --- |
| 0 | `0xe7` (231) | 0 | `0x01000748` | `pool_indirect_copy` |
| 1 | `0x74` (116) | 0 | `0x01001904` | `pool_tensor_scalar_addr` |
| 2 | `0x67` (103) | 0 | `0x010023c4` | `pool_pool_buffer_load` |
| 3 | `0x68` (104) | 0 | `0x01002590` | `pool_gather` |
| 4 | `0x46` (70) | 0 | `0x01002700` | `pool_copy` |
| 5 | `0x47` (71) | 0 | `0x01002844` | `pool_cast` |
| 6 | `0xb8` (184) | 0 | `0x01002f80` | `dma_memcopy` |
| 7 | `0xbb` (187) | 0 | `0x0100474c` | `dma_memcopy_indirect` |
| 8 | `0x7e` (126) | 0 | `0x01005e80` | `pool_iota` |
| 9 | `0x41` (65) | 0 | `0x01006398` | `pool_tensor_tensor_arith_op` |
| 10 | `0x7c` (124) | 0 | `0x01007d30` | `pool_cross_lane_reduce_arith` |
| 11 | `0x7d` (125) | 0 | `0x01007d48` | `pool_cross_lane_reduce_bitvec` |
| 12 | `0x49` (73) | 0 | `0x01007fc4` | `pool_memset` |
| 13 | `0x7a` (122) | 0 | `0x010084dc` | `pool_load_pool_argument` |
| 14 | `0x79` (121) | 0 | `0x01008520` | `pool_embedding_update` |
| 15 | `0x43` (67) | 0 | `0x0100a0d8` | `pool_tensor_scalar_arith_op` |
| 16 | `0x44` (68) | 0 | `0x0100a0d8` | `pool_tensor_scalar_arith` (op68 alias; **shares funcVA with op67**) |
| 17 | `0x92` (146) | 0 | `0x0100a118` | `pool_tensor_scalar_affine_select` |

The JSON names **17** functions (it omits the op68 alias at idx 16); the JSON op-set ⊆ the SO op-set.
All `funcVA`s carry an `R_XTENSA` reloc. `[HIGH/OBSERVED]`

The Q7 dispatch is the **flat** linear scan (no `0xf0` special case, since there are no `0xf0` rows):

```c
/* SUNDA Q7_POOL — the v2-floor kernel_info_table back-end (dispatch.hpp).
 * ONE flat 18-entry table, ALL spec=0 -> linear scan by opcode only, callx8 funcVA.
 * No (opcode<<24)|(spec<<16) two-level escape: the spec byte is always 0. */
typedef struct { uint8_t z0, z1, spec, opcode; uint32_t funcVA; } kit_entry_t;
#define KIT    ((const kit_entry_t *)(0x02000760))   /* SUNDA EXTISA_0: 18 entries */
#define KIT_N  18

void q7_pool_dispatch_v2(unsigned cpu_id, uint8_t opcode, decoded_inst_t *ins) {
    for (int i = 0; i < KIT_N; ++i)
        if (KIT[i].opcode == opcode) {                /* spec is always 0 on SUNDA */
            ((kernel_fn_t)KIT[i].funcVA)(ins, cpu_id);/* callx8 -> tensor/dma kernel */
            return;
        }
    /* miss: NO "UNKNOWN EXTENDED OPCODE" arm exists — there is no 0xf0 family. */
}
```

### 5c. The Q7 dispatcher infra — an external-lib-loading shell

The SUNDA Q7 DEBUG/RELEASE DRAM carries the **external-lib loader** string set (`.kernel_info_table`,
`load_external_libraries_impl`, `external_lib_loader.hpp`, `external_lib.cpp/.hpp/_funcs.hpp`,
`call_start_symbol`, `xtensa_unload`, `load_from_nx_addr`, `load_tables`, `dispatch_wrapper.hpp`,
`memory_manager.hpp`, `data_transfer.hpp`, the
`entry.total_cpus == 1 || == NUM_POOL_CORES` assertion, the
`SUNDA_UCODE_FILE_IO_QUEUE_FULL_POLICY_OVERWRITE` policy), plus the GPSIMD exception arms
(`sunda/pool/src/exception.hpp`: ILLEGAL INSTRUCTION / UNALIGNED MEMORY ACCESS / BUS DECODE FAILURE /
STACK OVERFLOW / DIVIDE BY ZERO / GENERIC GPSIMD EXCEPTION). Source tree
`/opt/workspace/NeuronUcode/sunda/pool/src/`.

> **NOTE — SUNDA's BASE Q7 build IS the external-lib-loading kind; there is NO separate SUNDA DKL
> image.** What CAYMAN factored into a separate **DKL** variant (`dynamic_kernel_dispatch` / prelink
> library) is the **default** on SUNDA: the Q7 IRAM is the dispatch + loader shell (17 KB vs CAYMAN's
> 90 KB DEBUG), and the kernels load at runtime from the EXTISA container. The weak-undefined EXTISA
> getter + the 17 KB IRAM + the loader symbol set are the three byte-anchors of this. `[HIGH/OBSERVED
> strings; runtime-load semantics INFERRED-HIGH]`

---

## 6. The per-core delta tables (CAYMAN → SUNDA, mostly subtractive)

### 6a. `NX_POOL` SEQ core delta

| axis | CAYMAN v3 | SUNDA v2 | direction |
| --- | --- | --- | --- |
| reset / `enter_run` | `j 0x1dc` → `@0x90` | `j 0x1dc` (==) → `@0x94` | entry **+0x4** |
| SEQ dispatch | `addi a2,-65` + 178-slot indexed table (55 real / 123 default `0x3198`) | **raw-compare chain** (no normalization, no indexed table) | **simplified** |
| distinct handlers | **41** `'S:'` handlers | (un-enumerable — see below) | — |
| `'S:'` handler logs | 187 (41 self-named) | **0** (observability floor) | **removed** |
| `0xF0 ExtendedInst` | present (the SEQ bridge) | **ABSENT** (no `0xf0`, no string) | **removed** |
| `dge_reshape` | present | present (predates SUNDA) | **kept** |
| `soc_window` translation | present | present | **kept** |
| PROF (CAM/TABLE) | 2 getters (shared 47-rec CAM + 8 KiB) | **NONE** | **removed** |
| window base | `0x04000000` family | `0x00100000` | v2 floor |

> **GOTCHA — the SUNDA handler set is NOT enumerable by name.** Because the v2 DEBUG build emits **no**
> `'S:'` handler logs, "SUNDA has N handlers" cannot be stated as a name-set. The reduction is anchored
> instead to the **missing opcodes/structs/strings** (the `ExtendedInst`/RNG/dequant/SB2SB absences,
> §1) and the 19-module SEQ set (§4). The *opcode-space* reduction is `89` SUNDA struct→opcode bindings
> vs CAYMAN's `99` (SUNDA lacks the 12 CAYMAN-added structs: `S3D3_COLLECTIVE` (SB2SB `0xBF`),
> `S3D3_NONZERO_WITH_COUNT`/`S3D3_SEQ_BOUNDS` (`0xf2`), `S3D3_TENS_DEQUANT` (`0x7b`), `S2_CONVLUT`,
> `DMA_GATHER_XPOSE` (`0xf1`), `DMA_DIRECT2D_XPOSE` (`0xbd`), `S4D3_MM`, …). `[HIGH/OBSERVED for the
> absences; the struct-count delta CARRIED from SX-GEN-05]`

> **NOTE — the lone SUNDA-only structs CAYMAN DROPPED.** `CUSTOM_OP_HEADER` and `CUSTOM_OP_PAYLOAD`
> (the static custom-op header/payload pair) are the **only** struct *removals* in the whole
> SUNDA→…→MAVERICK chain — CAYMAN replaced them with the `kernel_info_table`/EXTISA mechanism. So the
> SUNDA↔CAYMAN delta is **almost** purely additive (CAYMAN adds 12), with exactly these 2 dropped.
> `[HIGH/CARRIED from SX-GEN-05]`

### 6b. `Q7_POOL` compute core delta

| axis | CAYMAN v3 | SUNDA v2 | direction |
| --- | --- | --- | --- |
| reset / `enter_run` | `j 0x200` → `@0x90` | `j 0x220` → `@0x94` | reset **+0x20**, entry **+0x4** |
| IRAM size | `0x16360` PERF / `0x1ea40` DEBUG (90 KB) | `0x42d0` (17 KB, **loader shell**) | **shrunk** |
| `kernel_info_table` | `17 / 1 / 2 / 9` multi-lib split + 5 `0xf0` spec rows | **18 flat entries, all spec=0** | **flattened** |
| `0xF0` spec sub-dispatch | present (`(opcode<<24)\|(spec<<16)` key) | **ABSENT** (no `0xf0`, spec≡0) | **removed** |
| EXTISA libs | 4 (in-library getters) | 1 (weak-undef; runtime container `all.stripped.so`) | **reduced** |
| `'P%i:'` kernel logs | 156 | **0** | **removed** |
| EXTRAM | NONE | `0x1b40` (the **sole non-zero** EXTRAM) | **v2-unique** |
| TensorDequantize / cptc / MX | present (`0x7b`/`0xe4`, `proc_4bit_mx_8`, `cptc<1..6>`) | **ABSENT** (0 hits) | **removed** |
| GetSequenceBounds / NonzeroWithCount | present (`0xf2`) | **ABSENT** | **removed** |
| SB2SB `0xBF` collective | present | **ABSENT** | **removed** |
| Sort / ConvLutLoad / gather-2D-xpose | present | **ABSENT** | **removed** |
| RNG | SW Xorwow | **ABSENT** (no kernel body; §7d) | **removed** |
| DKL variant | full DKL family | **NONE** (loading IS the base) | folded into base |
| dtype | 16 base codes (numeric) | 16 base codes (== CAYMAN) | **kept** |

The SUNDA-side **base** compute (TensorTensor / TensorScalar / Copy / Cast / Iota / CrossLaneReduce /
Gather / EmbeddingUpdate / Memset / IndirectCopy / DmaMemcopy) is **present** but at the SUNDA v2
opcode numbers (`0x41`/`0x43`/`0x44`/`0x49`…), which differ from CAYMAN's (`0x45`/`0x51`/`0x52`…) — the
v2→v3 opcode renumbering. `[HIGH/OBSERVED for the SUNDA table bytes; the "same kernel, renumbered
opcode" mapping INFERRED-HIGH from the JSON names + SX-GEN-05]`

---

## 7. The v2-floor absences, proven (the page's value)

Each absence below is anchored to a **0-hit string scan** of the SUNDA EXTISA_0 SO + JSON + both POOL
DRAMs, plus the absence of the corresponding `kernel_info_table` opcode row — image-level confirmation,
not header inference. `[HIGH/OBSERVED]`

### 7a. NO `0xF0` ExtendedInst bridge

Zero `0xf0` rows in the 18-entry table; `ExtendedInst`/`extended` = 0 hits across the SO, JSON, and
both DRAMs. The dual-dispatch escape **arrives at CAYMAN**; SUNDA's cores are coupled by the
external-lib loader / direct-opcode path. `[HIGH/OBSERVED]`

### 7b. NO TensorDequantize / cptc / MX, NO SB2SB, NO seq-bounds

No `0x7b` (dequant), `0xe4` (cptc), `0xf2` (seq-bounds/nonzero), `0xbe`, or `0xbf` (SB2SB) row;
`dequant`/`cptc`/`proc_*bit`/`mx`/`sb2sb`/`collective`/`sequence_bound`/`nonzero`/`gather_xpose`/`sort`
all = 0 hits. SUNDA = the compute floor; these arrive at CAYMAN (SB2SB/transposes per SX-GEN-08).
`[HIGH/OBSERVED]`

### 7c. dtype / PROF / EXTRAM

- **dtype:** the SUNDA NX `move.cpp:41` assertion is **byte-identical to CAYMAN** —
  `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}`. No FP4/CPTC/MX named strings (those numeric codes arrive
  at MARIANA). SUNDA = the 16-code base floor, == CAYMAN. `[HIGH/OBSERVED]`
- **PROF:** **ABSENT** — no `PROF_CAM`/`PROF_TABLE` getter, no `hwdecode_` `.a` member. SUNDA
  establishes the no-HW-decode-profiling floor. `[HIGH/OBSERVED]`
- **EXTRAM:** the `0x1b40` Q7 EXTRAM is the **sole non-zero EXTRAM** in the 386-getter catalog — a flat
  Xtensa code band (`entry a1, 48`), the SUNDA-unique auxiliary/loader segment. `[HIGH/OBSERVED]`

### 7d. RNG — the enum-vs-image reconciliation

> **NOTE — the SUNDA RNG *enum* exists at the ISA-header level, but the SUNDA POOL *image* ships NO RNG
> kernel body. Both are true; there is no contradiction (this is a NOTE, not a CORRECTION).**
>
> A prior page ([rng-seed-state-ops.md](../firmware/kernels/rng-seed-state-ops.md)) records that
> SUNDA's `RAND_ALGORITHM` enum is `{LFSR=0, PCG32=1, PHILOX=2}` (**no `XORWOW`**) — defined in the
> **ISA header** `…/custom_op/c10/include/neuron_sunda_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
> **L874–878** (companion `RAND_SRC` at L888–889 = `{RNG_LFSR=0, RNG_PHILOX=2}`; the validity table in
> `…/aws_neuron_isa_tpb_enums.h` L559–561 confirms `RAND_ALGORITHM_MAX_PLUS_ONE = 3`). That is a
> **host-side algorithm-namespace declaration** for NeuronCore-v2 — it enumerates the algorithms the
> ISA *names*, independent of any firmware kernel. `[HIGH/OBSERVED — sunda arch-ISA headers]`
>
> This page proves, at the **POOL image level**, that the SUNDA POOL firmware carries **no RNG kernel
> body at all**: 0 hits for `Xorwow`/`Lfsr`/`Pcg`/`Philox`/`Rand`/`Rng` in the EXTISA_0 SO, the JSON
> manifest (17 functions — all pool/DMA primitives, none RNG), the 18-entry `kernel_info_table` (no
> RNG opcode), and both POOL DRAMs. The `RandGetState`/`RandSetState` **handler bodies** (and the SW
> `Xorwow(SW)` seed-init body, present in `libnrtucode_internal.so`) first ship on **CAYMAN** (per the
> CAYMAN baseline §5c, the RNG handlers are POOL-only there), where the enum **grows** to add
> `RAND_ALGORITHM_XORWOW = 3` (cayman `common.h` L895–898). So the SUNDA enum is **forward-namespace**
> (it lists the v2 algorithm namespace), the kernel is **generational** (the first working body is
> CAYMAN's SW Xorwow). The truncated SUNDA enum and the absent SUNDA POOL kernel body are the same
> fact viewed from two layers — there is no vestige and the prior page is not wrong. `[HIGH/OBSERVED
> for the image absence; the enum claim HIGH/OBSERVED carried from the RNG page]`

---

## 8. The v2-baseline characterization (the floor matrix)

SUNDA × POOL opens the bottom of the POOL image matrix. The full floor-vs-reference table:

| axis | SUNDA v2 (the floor) | CAYMAN v3 (the reference) |
| --- | --- | --- |
| POOL getters in `internal.so` | **8** (RELEASE-only) | 46 (DEBUG/PERF/TEST/PROF/DKL/EXTISA) |
| flavors | RELEASE only | DEBUG, PERF, TEST (+ DKL) |
| PROF (CAM/TABLE) | **NONE** | 2 (shared 47-rec CAM + 8 KiB) |
| DKL variant | **NONE** (loading is the base) | full DKL family |
| in-library EXTISA | **NONE** (weak-undef; container) | 4 EXTISA SO/JSON getters |
| EXTISA libs | 1 (single `all.stripped.so`) | 4 (17/1/2/9-entry split) |
| `kernel_info_table` | 18 flat entries, all spec=0 | 17/1/2/9 multi-lib + 5 `0xf0` specs |
| `0xF0 ExtendedInst` bridge | **ABSENT** | PRESENT (the dual-dispatch escape) |
| NX dispatch | raw-compare chain | `addi`-`0x41`-normalized 178-slot table |
| NX `'S:'` handler logs | **0** (observability floor) | 187 (41-handler self-naming) |
| Q7 `'P%i:'` kernel logs | **0** | 156 |
| Q7 IRAM size | `0x42d0` (17 KB, loader shell) | `0x16360` PERF / `0x1ea40` DEBUG (90 KB) |
| Q7 EXTRAM | `0x1b40` (the sole non-zero) | NONE |
| RNG | **NONE** (image; enum is ISA-level) | SW Xorwow |
| TensorDequantize / cptc / MX | **NONE** | PRESENT |
| GetSequenceBounds / NonzeroWC | **NONE** | PRESENT (`0xf2`) |
| SB2SB `0xBF` collective | **ABSENT** | PRESENT |
| gather / direct-2D transpose | **ABSENT** | PRESENT (`0xf1`/`0xbd`) |
| Sort / ConvLutLoad | **ABSENT** (no string) | PRESENT |
| dtype | 16 base codes (numeric) | 16 base codes (== SUNDA) |
| reset NX / Q7 | `j 0x1dc` / `j 0x220` | `j 0x1dc` / `j 0x200` |
| `enter_run` | `@0x94` | `@0x90` |
| window base | `0x00100000` | `0x04000000` family |
| DRAM `.globstruct` magic | `0x6099cb34` (== CAYMAN) | `0x6099cb34` |
| source tree | `sunda/seq/` + `sunda/pool/` | `cayman/seq/` + `dispatch.hpp` |
| custom-op structs | **HAS** `CUSTOM_OP_HEADER`/`PAYLOAD` | CAYMAN dropped them |

**The v2-floor thesis.** SUNDA × POOL is the **same** two-core SEQ+POOL engine structure as CAYMAN
(`engine_idx = 2`, dual reset vectors, shared `.globstruct` magic, the base pool/tensor/dma kernel set)
but the **floor** across five axes: (1) the single-flavor, no-PROF/no-DKL/no-in-library-EXTISA
**packaging floor**; (2) the **compute floor** — 18 flat kernels, no ExtendedInst bridge, no RNG,
no dequant/cptc/MX, no SB2SB, no gather/2D-transpose, no Sort/ConvLutLoad/seq-bounds; (3) the
**observability floor** — no `'S:'`/`'P%i:'` self-naming logs even in DEBUG; (4) the **dispatch
floor** — a raw-compare NX chain and a single external-lib-loaded flat Q7 table; (5) the
**static-custom-op floor** — the lone surviving `CUSTOM_OP_HEADER`/`PAYLOAD` pair. Everything
CAYMAN/MARIANA/MAVERICK POOL adds is a strict superset over this. `[synthesis INFERRED-HIGH; every
constituent fact OBSERVED]`

---

## 9. Honesty ledger

**HIGH / OBSERVED (reproduced this session):**

- 8 SUNDA POOL getters parsed instruction-exact (5 real + 3 zero-size cursors) + 2 weak-undef EXTISA;
  RELEASE-only (`nm`-confirmed: no DEBUG/PERF/TEST/PROF/DKL getter).
- 5 real carves sha256-verified, byte-identical (`cmp`) to the `libnrtucode.a` member `.rodata` (5/5);
  the `.a` superset = 16 SUNDA POOL members (DEBUG members extracted for the module set).
- Reset vectors decoded: NX `06 76 00` (`j 0x1dc`, == CAYMAN); Q7 `06 87 00` (`j 0x220`, CAYMAN
  `j 0x200`); **both → `enter_run @0x94`** (CAYMAN `@0x90`, +0x4); DRAM head `0x6099cb34` (== CAYMAN).
- SUNDA NX has **no** `addi a2,-65` normalization (0 hits); raw-compare `movi a6,187` (`0xbb`) @0x1b20.
- SUNDA NX_POOL DEBUG DRAM: **0** `'S:'` logs; Q7 DEBUG DRAM: **0** `'P%i:'` logs; the 19-module SEQ
  set + the Q7 external-lib loader string set extracted.
- SUNDA EXTISA_0 carved (sha `444497066f5e1738`, EM_XTENSA/ET_EXEC, entry `0x010000c8`); 18-entry
  `kernel_info_table` decoded byte-exact (all spec=0, opcodes `0x41..0xe7`, **no `0xf0`**); single
  `.data` (no `.globstruct`); JSON 17-func map (op-set ⊆ SO, op68 alias).
- Absence scans = 0 hits: RNG, dequant/cptc/MX, SB2SB/collective/seq-bounds/nonzero, ExtendedInst/Sort.
- dtype assertion `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}` (== CAYMAN); no PROF; the `0x1b40` Q7
  EXTRAM = the sole non-zero EXTRAM (flat code, 19 `entry`/55 `retw`).
- IDA v3 sidecars present for all 8 getters (the Q7 IRAM sidecar confirms the `NX_SP` contiguity
  cursor).

**MED / INFERRED:**

- "No indexed SEQ table anywhere on SUNDA NX" — the `addi`-absence + raw-compares are OBSERVED; a small
  hidden indexed sub-table cannot be 100% excluded by the FLIX-desynced linear sweep (SX-FW-00).
- The `0x1b40` Q7 EXTRAM segment's exact ROLE (loader stub vs aux kernel) — bytes/decode OBSERVED, role
  INFERRED-HIGH from the loader strings + the `entry` prologue.
- The v2→v3 opcode **renumbering** mapping (SUNDA `0x41`/`0x43`/… vs CAYMAN `0x45`/`0x51`/…) — both
  tables OBSERVED; the "same kernel, renumbered" reading INFERRED-HIGH from JSON names + SX-GEN-05.
- The external-lib runtime-load semantics — INFERRED-HIGH from the loader symbol set + the 17 KB IRAM +
  the weak-undef EXTISA.

**LOW / NOT CLAIMED:** the exact per-opcode SUNDA NX dispatch chain (FLIX-desync); the per-kernel
operand layout of each of the 18 SUNDA kernels; which silicon part/runtime path loads RELEASE vs the
(`.a`-only) SUNDA DEBUG build.

---

## 10. Cross-references

- [CAYMAN × POOL](./cayman-pool.md) — the v3 dual-dispatch **baseline this page diffs against**
  (committed); the shared two-core dispatch core, the 178-slot SEQ hub, the `0xf0` escape.
- [SUNDA × SP (remaining)](./sunda-sp-remaining.md) — the sibling v2 SP carve.
- [SUNDA v2 baseline](../generations/sunda-v2-baseline.md) — the cross-engine v2-floor characterization.
- [Cross-gen kernel_info matrix](./cross-gen-kernel-info-matrix.md) — the SUNDA→…→MAVERICK
  `kernel_info_table` evolution.
- [RNG seed/state ops](../firmware/kernels/rng-seed-state-ops.md) — the `RAND_ALGORITHM` enum and the
  per-gen RNG handler presence (the §7d reconciliation source).
- [kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md) — the 8-byte record format.
- [Image Catalog Index](./image-catalog-index.md) — the 386-getter map (SUNDA POOL rows).
- [MARIANA × POOL](./mariana-pool.md) / [MAVERICK × POOL](./maverick-pool.md) — the forward diffs.
