# Cross-Gen `kernel_info_table` / Opcode Matrix

> **Scope.** This is the image-grounded, byte-exact **Rosetta** that the per-generation
> firmware kernel pages cross-link: *opcode → (kernel name, engine, operand struct,
> dtype-set)*, **per generation**, carved directly from the actual Q7 POOL firmware
> images embedded in `libnrtucode_internal.so`. Its spine is the **two-dispatch-surface
> model** — the SEQ engine's ASCII-hub jump table on the NX core versus the Q7 POOL
> core's `kernel_info_table` linear-scan — and the master opcode→kernel join across all
> five generations.
>
> The **ISA-evolution step-diff narrative** and the **TONGA v1 deep-dive** live on the
> sibling page [Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md);
> this page does **not** re-derive TONGA. Here we stay on the image-grounded KIT matrix.

---

## Provenance & confidence model

Everything below is derived from **static analysis of shipped binaries only**: the
embedded device firmware images inside `libnrtucode_internal.so`, the shipped
`arch_isa` opcode enums (`aws_neuron_isa_tpb_common.h`), the shipped
`instruction_mapping.json`, the demangled `.xt.prop` symbols, and the IDA `*_enums.json`
artifacts. No external source tree was referenced.

**Spot-verifier binary.**
`extracted/.../custom_op/c10/lib/libnrtucode_internal.so`,
sha256 `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`
(re-hashed this task — **MATCH** the image-catalog anchor). Disassembler:
the shipped Cadence `ncore2gp` `xtensa-elf-objdump` (Binutils 2.34, Xtensa Tools 14.09).

| Tag | Meaning |
| --- | ------- |
| `HIGH` | direct disassembly / byte read / hash match |
| `MED` | strong inference (reloc-anchored, `.xt.prop`-routed, or shared-region trampoline) |
| `LOW` | plausible, not pinned |
| `OBSERVED` | read directly from the bytes / disasm |
| `CARRIED` | brought forward from an upstream image carve (e.g. SUNDA, out of this package) |

> **NOTE.** Five KIT cells across four generations were **re-carved byte-exact this
> task** to anchor the matrix (see [§7 Spot-verification](#7-spot-verification--five-cells-re-carved-byte-exact)).
> All five blob hashes and key columns matched their anchors.

---

## 0. The verdict up front

* **There are TWO distinct dispatch surfaces, on TWO separate Xtensa cores, bridged by
  ONE opcode (`0xF0`).** This is the central structural fact the whole matrix hangs on.
  Surface **A** is the SEQ engine's ASCII-hub jump table on the NX core
  (`NX_POOL`/`NX_ACT`/`NX_DVE`/`NX_PE`/`NX_SP`); Surface **B** is the Q7 POOL compute
  core's `kernel_info_table`. The `0xF0` *ExtendedInst* handler is the explicit escape
  that forwards `(0xF0, spec)` from Surface A to Surface B. **No third dispatch level.**
  `[HIGH/OBSERVED — §1, §7]`
* **The Q7 POOL `kernel_info_table` key set is generation-stable and byte-for-key
  identical across the v3→v5 band.** Re-carving and key-diffing the `(opcode,spec)`
  column of `EXTISA_0` from the shipped binary across **three** generations gives
  CAYMAN (`910d41c3`) ≡ MARIANA (`9f2ce049`) ≡ MAVERICK (`a92c8ba0`) — all 17 entries,
  `ADDED=[] / REMOVED=[]`. MARIANA_PLUS `EXTISA_0_SO` is byte-identical to MARIANA
  (`9f2ce049`). The POOL compute opcode space **does not grow** from CAYMAN through
  MAVERICK; only the `funcVA`s relocate. `[HIGH/OBSERVED — re-carved this task, §7]`
* **SUNDA is the floor and the only key-set outlier.** SUNDA's Q7 table is a **single
  flat 18-entry table**, all `spec=0`, opcodes `0x41..0xe7`, with **no `0xF0` rows** (no
  ExtendedInst bridge), no `0x7b` dequant, no `0xe4` cptc, no `0xbe`/`0xf2`. SUNDA's Q7
  is an external-lib-loading **shell** — its kernels live in a separate runtime
  container (`libnrtucode_extisa.so`, in the `neuronx-runtime` package, **not** this
  checkout). SUNDA's 18 rows are `[HIGH/CARRIED]` from the SUNDA image carve.
* **Generation opcode-space growth happens on OTHER engines, not POOL's compute table.**
  The TPB OPCODE enum grew `159 → 165` (`+6` net) from MARIANA to MAVERICK; DVE gained
  `+7` (RNG/sparsity/MX) and PE gained `+4` (PeManageSeed/MX/ConvLutLoad) at
  CAYMAN→MARIANA. But POOL's `NX_POOL` SEQ table stayed `177`-bound and POOL's Q7
  `kernel_info_table` key set stayed identical across all of v3/v4/v4+/v5. **The "MX /
  dtype expansion" is realized on DVE and PE — not as new POOL KIT rows.**
  `[HIGH/OBSERVED]`
* **`QUANTIZE_MX` (`0xe3`) binds DVE, not POOL.** `0xe3` is **absent** from all four
  MAVERICK Q7 EXTISA POOL KITs (re-carved: not in the 17-entry `EXTISA_0`); it is armed
  only in the MAVERICK DVE PROF CAM. POOL's only MX surface is **`0x7b`
  TENSOR_DEQUANTIZE** (`EXTISA_0` idx16). `[HIGH/OBSERVED — re-carved this task]`

---

## 1. The two-dispatch-surface model — the spine of the matrix

The GPSIMD POOL slice is **two Xtensa cores**: a front-end **SEQ** engine on the NX core
that *fetches the instruction stream*, and a back-end **Q7 POOL** compute core that *runs
the tensor kernel*. They have **two completely different dispatch mechanisms**, joined by
a single escape opcode.

| | **Surface A — SEQ ASCII dispatch** | **Surface B — Q7 `kernel_info_table`** |
| --- | --- | --- |
| core | NX (front-end fetch) | Q7 (compute back-end) |
| log prefix | `'S:'` | `'P%i:'` (per pool-core index `i`) |
| reset vector | `j 0x1dc` (CAYMAN/SUNDA); per-gen relocated | `j 0x200` (CAYMAN); `j 0x220` (SUNDA, +0x20) |
| table location | DRAM `0x80814` (DEBUG) / `0x80218` (PERF) | EXTISA ELF section `kernel_info_table` @ VMA `0x02000380` |
| key | `index = opcode_byte − 0x41` (`'A'`-base; DVE uses `−0x30` `'0'`) | packed `u32 = (opcode<<24)\|(spec<<16)`; record `{0,0,spec@+2,opcode@+3,fv_le@+4}` |
| lookup | direct-indexed jump (`addx4`) | **linear scan** by packed key |
| width | **178 slots** (bound `movi a3,177`) | **17 entries** (`EXTISA_0`) `+1/+2/+9` |
| real handlers | ~55 real / 123 default (POOL) | every entry is real (`callx8 funcVA`) |
| handler form | C++ `Handler::execute()` (4 hops: table→tramp→impl→thunk) | flat C kernel fn (1 hop: table→funcVA trampoline→worker) |
| miss policy | `"S: Bad Opcode"` → hard spin | `"P%i: UNKNOWN OPCODE=0x%x"` |
| packaging | flat IRAM/DRAM device segment | `EXTISA_n` `EM_XTENSA` ELF (+ flat Q7 IRAM) |
| reach | **fetches every opcode** in the stream | reached via the per-opcode handler and the `0xF0` bridge |

### The `0xF0` ExtendedInst bridge

`[HIGH/OBSERVED — spot-verified §7]`

SEQ-side opcode `0xF0` resolves (via the NX core's index `0xF0 − 0x41 = 0xAF`, slot at
DRAM file offset `0x814 + 0xAF*4 = 0xad0`) to the **ExtendedInst trampoline** `0x3190`.
This handler is **POOL-exclusive** — absent on the ACT/DVE/PE/SP SEQ tables. It forwards
`(0xF0, spec)` to the Q7 core, where the `kernel_info_table` registers `0xF0` **five
times** in `EXTISA_0` (spec `0,1,2,4,3`) plus once more in `EXTISA_3` (spec `7`, the cptc
extended path). The **spec byte sub-selects** the kernel. There is no third dispatch
level.

### Surface A — SEQ ASCII dispatch (C pseudocode)

The NX SEQ engine normalizes the opcode to an `'A'`-base index and does a direct-indexed
jump into the 178-slot table. The handler is a C++ `Handler::execute()` virtual, so the
"resolved" address is a trampoline that re-dispatches through the engine vtable.

```c
/* Surface A: SEQ ASCII-hub dispatch (the NX core; 'S:' dialect).            */
/* Table base @ DRAM 0x80814 (DEBUG build) / 0x80218 (PERF). 178 slots.      */
/* Reconstructed from the dram.bin carve (sha 7bdf6ed7) + the 0x2e5f..0x2e62 */
/* prologue: addi a2,a2,-65 ; movi a3,177.                                    */

#define SEQ_TABLE_BASE   0x80814u      /* DEBUG */
#define SEQ_TABLE_BOUND  177u          /* movi a3,177 -> 178 entries 0..177  */
#define SEQ_OPCODE_BIAS  0x41u         /* 'A'  (DVE engine uses 0x30 = '0')  */

static seq_handler_t  seq_table[178];  /* DRAM-resident; index = op - 'A'    */

void seq_dispatch(uint8_t opcode, const seq_inst_t *inst, EngineState *eng)
{
    uint32_t idx = (uint32_t)(opcode - SEQ_OPCODE_BIAS);   /* normalize 'A'   */
    if (idx > SEQ_TABLE_BOUND) {                           /* out of table    */
        seq_log(eng, "S: Bad Opcode");                     /* -> hard spin    */
        engine_fault_spin(eng);
        return;
    }
    seq_handler_t h = seq_table[idx];                      /* addx4 + load    */
    /* C++ Handler::execute() — 4 hops: table -> tramp -> impl -> thunk.      */
    h->vtbl->execute(h, inst, eng);                        /* virtual call    */
    /* opcode 0xF0 lands here too: its handler is the ExtendedInst trampoline */
    /* (slot @ file 0xad0 = 0x3190) which forwards (0xF0, spec) to Surface B. */
}
```

### Surface B — Q7 `kernel_info_table` (C pseudocode)

The Q7 POOL core packs `(opcode, spec)` into a `u32` key and **linearly scans** the
`kernel_info_table` for the matching record, then does a single `callx8` to the
`funcVA`. There is no normalization and no jump table — it is a flat record scan.

```c
/* Surface B: Q7 POOL kernel_info_table dispatch (the 'P%i:' dialect).        */
/* Table @ EXTISA ELF section VMA 0x02000380. 8-byte records, linear scan.    */
/* Record layout (re-carved byte-exact this task):                            */
/*   off+0..1 : 0,0       (pad / reserved)                                     */
/*   off+2    : spec      (the 0xF0 sub-selector; 0 for plain opcodes)         */
/*   off+3    : opcode    (the TPB opcode byte)                                */
/*   off+4..7 : funcVA    (little-endian; the kernel entry point)              */

typedef struct { uint8_t pad0, pad1, spec, opcode; uint32_t funcVA; } kit_entry_t;

/* The packed key the scan compares against: (opcode<<24)|(spec<<16).         */
#define KIT_KEY(op, sp)  (((uint32_t)(op) << 24) | ((uint32_t)(sp) << 16))

void q7_pool_dispatch(uint8_t opcode, uint8_t spec,
                      const kit_entry_t *kit, size_t n_entries,
                      const pool_inst_t *inst, PoolState *p)
{
    uint32_t want = KIT_KEY(opcode, spec);
    for (size_t i = 0; i < n_entries; ++i) {               /* LINEAR SCAN     */
        if (KIT_KEY(kit[i].opcode, kit[i].spec) == want) {
            pool_kernel_fn fn = (pool_kernel_fn)(uintptr_t)kit[i].funcVA;
            fn(inst, p);                                   /* callx8 funcVA   */
            return;
        }
    }
    pool_log(p, "P%i: UNKNOWN OPCODE=0x%x", p->core_idx, opcode);
}
```

> **GOTCHA — SUNDA's surfaces differ (the v2 floor).** SUNDA Surface A is a
> **raw-compare chain** (no `−0x41` normalization, no 178-slot table); SUNDA Surface B
> is a **single flat 18-entry table** with **no `0xF0` bridge**. On SUNDA the two cores
> are coupled by the external-lib loader / direct-opcode path, not the `0xF0` escape.
> `[HIGH/CARRIED]`

---

## 2. The Q7 POOL `kernel_info_table` — cross-gen, `EXTISA_0` (the MAIN compute table)

The 17-entry `EXTISA_0` table is the authoritative POOL compute back-end for v3..v5. The
`(opcode,spec)` key set is **identical** across CAYMAN / MARIANA / MARIANA_PLUS /
MAVERICK (re-carved this task — see [§7](#7-spot-verification--five-cells-re-carved-byte-exact));
only the `funcVA`s relocate. The `funcVA`s shown are the **CAYMAN `ET_EXEC`** values
(`.text` @ `0x01000000`), re-read byte-exact this task.

> `[idx / opcode / spec / funcVA — HIGH/OBSERVED, re-carved. kernel name HIGH where
> `.xt.prop` EXACT, MED where routed. struct/dtype CARRIED from the FW reports.]`

| idx | op | spec | KERNEL (resolved) | ENGINE | STRUCT (FW) | DTYPE / route | funcVA (CAYMAN) | conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | `0x7e` | 0 | `pool_iota` (`iota_impl<t/f>`) | Q7_POOL | PSEUDO/iota | INT (index ramp) | `0x01000080` | HIGH(op)/MED(route) |
| 1 | `0x7c` | 0 | `pool_cross_lane_reduce_arith` | Q7_POOL | S3D3 (reduce) | arith full op set | `0x010003f8` | HIGH `[.xt.prop EXACT]` |
| 2 | `0x7d` | 0 | `pool_cross_lane_reduce_bitvec` | Q7_POOL | S3D3 (reduce) | bitvec (and/or/xor) | `0x01000410` | HIGH `[.xt.prop EXACT]` |
| 3 | `0x45` | 0 | `decode_pool(bool)` `[Pool]` | Q7_POOL | S3D3 (pool) | avg/max pool | `0x01000b90` | HIGH(route)/MED(op-name) |
| 4 | `0x51` | 0 | `decode_tensor_tensor` (bitvec `'Q'`) | Q7_POOL | S3S3D3_TT | DTYPE_PAIR bitvec | `0x0100105c` | MED |
| 5 | `0x41` | 0 | `pool_tensor_tensor_arith_op` | Q7_POOL | S3S3D3_TT | DTYPE_PAIR arith | `0x01000f1c` | HIGH(op)/MED(route) |
| 6 | `0xf0` | 0 | ExtendedInst spec0 (`EngineNop`) | Q7_POOL | (ext bridge) | n/a | `0x01003370` | MED |
| 7 | `0xf0` | 1 | `pool_extended_inst_copy` | Q7_POOL | (ext bridge) | copy | `0x01003380` | HIGH `[.xt.prop EXACT]` |
| 8 | `0xf0` | 2 | `decode_extended_inst_tensor_tensor` | Q7_POOL | (ext bridge) | S3S3D3_TT | `0x01003484` | HIGH |
| 9 | `0xf0` | 4 | ExtendedInst spec4 → Rand band | Q7_POOL | (ext bridge) | → decode_pool/Rand | `0x010037a8` | MED |
| 10 | `0xf0` | 3 | ExtendedInst spec3 → Rand band | Q7_POOL | (ext bridge) | → decode_pool/Rand | `0x01003a60` | MED |
| 11 | `0x52` | 0 | op `0x52` (`'R'`) dispatch | Q7_POOL | (.data tbl) | — | `0x01003b40` | **LOW** |
| 12 | `0x46` | 0 | `pool_copy` | Q7_POOL | S3D3 (copy) | bit-accurate copy | `0x010040c0` | HIGH(op)/MED(route) |
| 13 | `0x47` | 0 | `pool_cast` | Q7_POOL | S3D3 (cast) | in→FP32→out | `0x01004160` | HIGH(op)/MED(route) |
| 14 | `0xbe` | 0 | `get_sequence_bounds` `[GetSeqBounds]` | Q7_POOL | S3D3_SEQ_BOUNDS | dtype-keyed bounds | `0x01004204` | HIGH(op)/MED(route) |
| 15 | `0xf2` | 0 | `get_sequence_bounds`/NonzeroWCount | Q7_POOL | S3D3_NONZERO_WC | `<float>`/`<int>` | `0x0100484c` | MED |
| 16 | `0x7b` | 0 | `decode_tensor_dequantize` `[TensDeq]` | Q7_POOL | S3D3_TENS_DEQUANT | `proc_4bit_mx_8` (MX) | `0x01004dc4` | HIGH(route) |

**Per-gen `funcVA` reloc:** CAYMAN (`ET_EXEC`, `.text` @ `0x01000000`) → MARIANA
(`+0..+0x44` monotonic reloc) → MARIANA_PLUS (byte-identical SO to MARIANA) → MAVERICK
(stripped `ET_DYN`, `.text` @ `0x0`, fresh rebuild; **same key set**, re-ordered
`funcVA`s). `[HIGH/OBSERVED]`

> **CORRECTION — idx14/15 (`0xbe` vs `0xf2`).** The SEQ-side `'S:'` log lists
> "GetSequenceBounds" and "NonzeroWithCount" as **distinct** handlers. The shipped
> maverick enum pins `GET_SEQUENCE_BOUNDS = 0xbe` and `NONZERO_WITH_COUNT = 0xf2`
> (confirmed below). The `0xf2` trampoline routes into a **shared** sequence-bounds /
> dequant region, hence the historical "get_sequence_bounds/dequant" label on the
> `0xf2` row — but the **authoritative opcode split** is `0xbe = GetSequenceBounds`,
> `0xf2 = NonzeroWithCount`. Any source that wrote "`0xf2 = GetSequenceBounds`
> loosely conflated them. `[HIGH/OBSERVED — enum pins]`

### The smaller EXTISA images (re-carved byte-exact this task)

* **`EXTISA_1`** (1 entry @ VMA `0x02000048`): `0x7e/0` (iota). `[HIGH/OBSERVED]`
* **`EXTISA_2`** (2 entries @ VMA `0x02000070`): `0x7c/0`, `0x7d/0` (cross-lane reduce).
  `[HIGH/OBSERVED]`
* **`EXTISA_3`** (9 entries @ VMA `0x020008c8`, the cptc/MX codec family)
  `[HIGH/OBSERVED — re-carved, funcVAs below]`:

  | idx | op | spec | kernel | funcVA (CAYMAN) |
  | --- | --- | --- | --- | --- |
  | 0 | `0x7e` | 0 | `pool_iota` | `0x01000080` |
  | 1 | `0x7c` | 0 | clr_arith | `0x010003f8` |
  | 2 | `0x7d` | 0 | clr_bitvec | `0x01000410` |
  | 3 | `0x45` | 0 | `decode_pool` `[Pool]` | `0x01000b90` |
  | 4 | `0xbe` | 0 | `get_sequence_bounds` | `0x01000dac` |
  | 5 | `0xf2` | 0 | nonzero / seq-bounds | `0x010013f4` |
  | 6 | `0x7b` | 0 | `decode_tensor_dequantize` | `0x01001964` |
  | 7 | `0xe4` | 0 | **cptc dispatcher** (`CONV_LUT_LOAD`; `cptc_decode_impl<1..6>` DTYPE-selected) | `0x01002258` |
  | 8 | `0xf0` | 7 | ExtendedInst spec7 → cptc extended path | `0x01003b64` |

  `0xe4` = `NEURON_ISA_TPB_OPCODE_CONV_LUT_LOAD` per the shipped maverick enum
  (confirmed below); present on both CAYMAN and MAVERICK. The cptc codec family is
  selected **inside the kernel** by DTYPE/spec.

---

## 3. The SUNDA Q7 POOL table — the 18-entry flat floor (the only key outlier)

Single flat table, **all `spec=0`**, **no `0xF0`**. `[HIGH/CARRIED]` — the SUNDA EXTISA
container (`libnrtucode_extisa.so`, sha `444497066f5e1738`) ships in the `neuronx-runtime`
package, **not** this checkout's customop-lib package, so these rows are byte-reads
carried from the SUNDA image carve, not re-verified here.

| idx | op | KERNEL (SUNDA JSON name map) | SEQ-equivalent (CAYMAN+ opcode) |
| --- | --- | --- | --- |
| 0 | `0xe7` | `pool_indirect_copy` | `0xe7` IndirectCopy (S4D4_IC) |
| 1 | `0x74` | `pool_tensor_scalar_addr` | `0x74` TensorScalarAddr |
| 2 | `0x67` | `pool_pool_buffer_load` | `0x67` PoolBufferLoad band |
| 3 | `0x68` | `pool_gather` (`send_gather_request`) | `0x68` TensorGather (S4D4_GT) |
| 4 | `0x46` | `pool_copy` | `0x46` Copy |
| 5 | `0x47` | `pool_cast` | `0x47` Cast |
| 6 | `0xb8` | `dma_memcopy` | `0xb8` DmaMemcopy |
| 7 | `0xbb` | `dma_memcopy_indirect` | `0xbb` DmaMemcopyIndirect (DMA_INDIRECT1D) |
| 8 | `0x7e` | `pool_iota` (`iota_kernel`) | `0x7e` Iota |
| 9 | `0x41` | `pool_tensor_tensor_arith_op` | `0x41` TensorTensorArith (S3S3D3_TT) |
| 10 | `0x7c` | `pool_cross_lane_reduce_arith` | `0x7c` CrossLaneReduce arith |
| 11 | `0x7d` | `pool_cross_lane_reduce_bitvec` | `0x7d` CrossLaneReduce bitvec |
| 12 | `0x49` | `pool_memset` | `0x49` MEMSET/RNG |
| 13 | `0x7a` | `pool_load_pool_argument` | `0x7a` LoadPoolArgument |
| 14 | `0x79` | `pool_embedding_update` | `0x79` EmbeddingUpdate |
| 15 | `0x43` | `pool_tensor_scalar_arith_op` | `0x43` TensorScalarArith |
| 16 | `0x44` | `pool_tensor_scalar_arith` (op68 alias) | `0x44` TensorScalarPtrArith (shares funcVA w/`0x43`) |
| 17 | `0x92` | `pool_tensor_scalar_affine_select` | `0x92` TensorScalarAffineSelect |

> **SUNDA-vs-CAYMAN+ diff.** SUNDA **lacks** the entire extended layer present in
> CAYMAN's `EXTISA_0` — `0xF0×5` (ExtendedInst bridge), `0x7b` (TensorDequantize),
> `0xe4` (cptc), `0xbe` (GetSequenceBounds), `0xf2` (NonzeroWithCount), `0x52`, `0x51`.
> SUNDA carries instead the indirection/DMA primitives **flat**
> (`0xe7`/`0x68`/`0xb8`/`0xbb`/`0x74`/`0x67`/`0x7a`/`0x49`/`0x92`) that on CAYMAN+ are
> **SEQ-handler-reached**, not `kernel_info_table` rows. `[HIGH/CARRIED]`

---

## 4. The consolidated opcode matrix — the full union

This is the page's spine: rows are the **union** of (the SEQ 178-table real opcodes) ∪
(the Q7 `kernel_info_table` opcodes) ∪ (the `0xF0` ExtendedInst specs). Columns:

* **OP** — TPB opcode byte (ASCII shown where in `0x41..0x7e`).
* **SURF** — which dispatch surface(s): `A` = SEQ ASCII, `B` = Q7 KIT, `A→B` = bridged.
* **ENG** — the engine that **executes** it (`POOL` = Q7_POOL, `NX` = NX SEQ, `DVE`, `PE`, `ACT`, `SP`).
* **STRUCT** — the operand struct (FW report).
* **DTYPE** — the supported dtype family.
* **GEN PRESENCE** — `SU CA MA MP MV` (SUNDA / CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK);
  `Y` present, `-` absent, `~` present-but-remapped/SEQ-only at that gen, `?` not byte-pinned.
* **SRC / conf** — the report that established the binding.

> **Matrix dimensions: 49 opcode rows × 5 generations.** Every `kernel_info_table` cell
> (Surface B) is byte-grounded `HIGH/OBSERVED`; SEQ-only and engine-cross cells carry the
> per-report confidence.

### 4a. The shared SEQ control/move core (Surface A; all NX engines)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0xa7` | MOVE | A | NX | move (full reg) | U32/I32/FP32 | Y | Y | Y | Y | Y | FW-78 HIGH |
| (—) | AluOp/BRANCH/NOP/NOTIFY/POLL_SEM/Redirect/SET_OM/STRONG_ORDER/Event_Sem/Halt/WRITE/TensorLoad/TensorStore/INS_\*/EXT_BREAK (the 18 shared SEQ handlers — control spine, not numbered) | A | NX | CTRL | n/a | Y | Y | Y | Y | Y | FW-07 HIGH |

### 4b. The POOL compute opcodes (Surface B Q7 KIT + their SEQ `'S:'` decode)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x41` | TensorTensorArith (`pool_tensor_tensor_arith_op`) | A→B | POOL | S3S3D3_TT | DTYPE_PAIR arith | Y | Y | Y | Y | Y | FW-49 / KIT5 HIGH |
| `0x51` | TensorTensorBitvec (`decode_tensor_tensor` `'Q'`) | A→B | POOL | S3S3D3_TT | bitvec | - | Y | Y | Y | Y | FW-49 / KIT4 MED (SU:~) |
| `0x43` | TensorScalarArith | A→B | POOL | S3D3_TS | arith | Y | Y | Y | Y | Y | FW-50 / KIT(SU15) HIGH |
| `0x53` | TensorScalarBitvec | A | NX/POOL | S3D3_TS | bitvec | ? | Y | Y | Y | Y | FW-50 HIGH(op) |
| `0x44` | TensorScalarPtrArith (dep.) | A→B | POOL | S3D3_TS | arith (ptr-imm) | Y | Y | Y | Y | Y | FW-50 / KIT(SU16) HIGH |
| `0x54` | TensorScalarPtrBitvec (dep.) | A | NX | S3D3_TS | bitvec (ptr) | ? | Y | Y | Y | Y | FW-50 HIGH(op) |
| `0x45` | Pool (avg/max) | A→B | POOL | S3D3 (pool) | pool | Y | Y | Y | Y | Y | FW-70 / KIT3 HIGH(op) |
| `0x46` | Copy / `pool_copy` | A→B | POOL | S3D3 (copy) | bit-accurate | Y | Y | Y | Y | Y | FW-72 / KIT12 HIGH |
| `0x47` | Cast / `pool_cast` | A→B | POOL | S3D3 (cast) | in→FP32→out | Y | Y | Y | Y | Y | FW-72 / KIT13 HIGH |
| `0x49` | MEMSET/RNG (`pool_memset`) | A→B | POOL | S3D3 | n/a | Y | ~ | ~ | ~ | ~ | IMG-23 HIGH(SU) |
| `0x7a` | LoadPoolArgument | A→B | POOL | PSEUDO | n/a | Y | Y | Y | Y | Y | IMG-23 HIGH |
| `0x67` | PoolBufferLoad | A→B | POOL | PSEUDO | n/a | Y | ~ | ~ | ~ | ~ | IMG-23 HIGH(SU) |
| `0x7e` | Iota (`pool_iota`) | A→B | POOL | PSEUDO/iota | INT index ramp | Y | Y | Y | Y | Y | FW-51 / KIT0 HIGH |
| `0x7c` | CrossLaneReduce arith | A→B | POOL | S3D3 (reduce) | arith | Y | Y | Y | Y | Y | FW-51 / KIT1 HIGH `[xtprop]` |
| `0x7d` | CrossLaneReduce bitvec | A→B | POOL | S3D3 (reduce) | bitvec | Y | Y | Y | Y | Y | FW-51 / KIT2 HIGH `[xtprop]` |
| `0x92` | TensorScalarAffineSelect | A→B | POOL | S3D3_TS | select | Y | Y | Y | Y | Y | IMG-23(SU17) HIGH(SU) |
| `0x74` | TensorScalarAddr | A→B | POOL | S3D3_TS (addr) | arith (addr op) | Y | Y | Y | Y | Y | IMG-23(SU1) HIGH(SU) |

### 4c. The indirection / DMA primitives (index-tensor; FW-74)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x68` | TensorGather (`pool_gather`) | A→B | POOL | S4D4_GT | idx U8/16/32 | Y | Y | Y | Y | Y | FW-74 / IMG-23(SU3) HIGH |
| `0x79` | EmbeddingUpdate | A→B | POOL | PSEUDO | scatter-reduce | Y | Y | Y | Y | Y | FW-74 / IMG-23(SU14) HIGH |
| `0xe7` | IndirectCopy | A→B | POOL | S4D4_IC | indexed copy | Y | Y | Y | Y | Y | FW-74 / IMG-23(SU0) HIGH |
| `0xb8` | DmaMemcopy | A→B | POOL | (DMA) | n/a | Y | Y | Y | Y | Y | FW-74 / IMG-23(SU6) HIGH |
| `0xbb` | DmaMemcopyIndirect | A→B | POOL | DMA_INDIRECT1D | by-index DMA | Y | Y | Y | Y | Y | FW-74 / IMG-23(SU7) HIGH |

### 4d. The extended-instruction bridge (`0xF0`; CAYMAN+ only; POOL-exclusive)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0xf0` | ExtendedInst (5 specs + cptc spec7) | A→B | POOL | (escape) | per-spec (copy/tt/rand/cptc) | - | Y | Y | Y | Y | FW-07/15/18 HIGH |

`0xF0` sub-selectors (the `kernel_info_table` `0xF0` rows, §2): `spec0` EngineNop ·
`spec1` ExtCopy · `spec2` ExtTensorTensor · `spec3,4` Rand band · `spec7` cptc extended
(`EXTISA_3`).

### 4e. The extended compute layer (CAYMAN+; absent on SUNDA)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x7b` | TensorDequantize (`decode_tensor_dequantize`) | A→B | POOL | S3D3_TENS_DEQUANT | MX 4-bit; `proc_4bit_mx_8` | - | Y | Y | Y | Y | FW-75/63 / KIT16 HIGH(route) |
| `0xe4` | ConvLutLoad / cptc disp. | A→B | POOL | S2_CONVLUT | `cptc_decode<1-6>` | - | Y | Y | Y | Y | IMG-21 / KIT3.7 HIGH |
| `0xbe` | GetSequenceBounds | A→B | POOL | S3D3_SEQ_BOUNDS | dtype-keyed | - | Y | Y | Y | Y | FW-77 / KIT14 HIGH(op) |
| `0xf2` | NonzeroWithCount | A→B | POOL | S3D3_NONZERO_WC | `<float>`/`<int>` | - | Y | Y | Y | Y | FW-73 / KIT15 HIGH(op) |
| `0x52` | op `0x52` (`'R'`) dispatch | B | POOL | (.data tbl) | — | - | Y | Y | Y | Y | FW-18 / KIT11 **LOW** |

### 4f. POOL SEQ-only handlers (Surface A; no KIT row — reached as SEQ handler)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x6b` | StreamTranspose | A | NX/POOL | S4D4_TR | 32×32 transpose | ? | Y | Y | Y | Y | FW-59 HIGH(op) |
| (—) | ConvLutLoad/Sort/SB2SB_Collective(`0xBF`)/ModifyPoolConfig/RandGetState/RandSetState | A | NX(POOL) | various | (general compute) | - | Y | Y | Y | Y | IMG-06 HIGH(presence) |

### 4g. The DVE compute opcodes (Surface A; `engine_idx=3`; MX/RNG/sparsity)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x30` | Exponential | A | DVE | S3D3_TS | fp | - | ~ | Y | Y | Y | FW-42 HIGH (MA add) |
| `0x76` | Rand (deprecated `'n'`) | A | DVE | — | — | ? | ? | ? | ? | ? | FW-48 (not maintained) |
| `0x77` | RandGetState | A | DVE/POOL | — | seed state | - | ~ | Y | Y | Y | FW-48 HIGH (MA add on DVE) |
| `0x78` | RandSetState | A | DVE/POOL | — | seed state | - | ~ | Y | Y | Y | FW-48 HIGH (MA add on DVE) |
| `0xe0` | SparsityCompress | A | DVE | S3D3_SC | fp8/bf16/fp16 | - | - | Y | Y | Y | FW-60 HIGH (MA add) |
| `0xe1` | SparsityCompressTag | A | DVE | S2D2D2_SC | u16 tag (D16/32) | - | - | Y | Y | Y | FW-60 HIGH (MA add) |
| `0xe2` | Rand2 | A | DVE | — | XORWOW uniform | - | - | Y | Y | Y | FW-48 HIGH (MA add) |
| `0xe3` | **QuantizeMx** | A | **DVE** | — | MX block quant | - | - | Y | Y | **Y** (DVE PROF CAM; **named handler dropped on MV; POOL ABSENT**) | FW-60/IMG-09 HIGH |

> **QUIRK — `0xe3` is a DVE opcode, not a POOL KIT row.** Re-carving the MAVERICK
> `EXTISA_0` POOL KIT (17 entries) this task confirmed `0xe3` is **absent**. On MAVERICK
> the `QuantizeMx` *named handler* is even dropped from the DVE roster (60→59); `0xe3` is
> armed only in the MAVERICK DVE PROF CAM. **POOL's only MX surface is `0x7b`
> TensorDequantize** (`EXTISA_0` idx16). `[HIGH/OBSERVED]`

### 4h. The PE matmul opcodes (Surface A; systolic array)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x01` | Ldweights (LoadStationary) | A | PE | S3_LW | weight load | Y | Y | Y | Y | Y | FW-66/NKI-01 HIGH `[hdr]` |
| `0x05` | Matmul (MultiplyMoving) | A | PE | S3_MM | MAC→PSUM | Y | Y | Y | Y | Y | FW-66/NKI-01 HIGH `[hdr]` |
| `0x06` | LdTags | A | PE | S3_LT | tag load | Y | Y | Y | Y | Y | FW-60/IMG-05 HIGH |
| `0x07` | MatmulSparse | A | PE | S4D3_MM | 2/3/4 fmap | ~ | Y | Y | Y | Y | FW-60/GEN-05 HIGH |
| `0x08` | PeManageSeed | A | PE | — | PE seed state | - | - | Y | Y | Y | FW-66/IMG-10 HIGH (MA add) |
| `0x09` | LdweightsMX | A | PE | S3_LW (MX) | MX weights | - | - | Y | Y | Y(†) | IMG-10 HIGH (MA add) |
| `0x0a` | MatmulMX | A | PE | S3_MM (MX) | MX matmul | - | - | Y | Y | Y(†) | IMG-10/FW-66 HIGH (MA add) |

> **NOTE.** (†) On MAVERICK, `MatmulMX`/`LdWeightMX` (`0x09`/`0x0A`) **fold into Matmul**
> via the `MXTensorV2` path (still PE); the opcodes persist but the named handlers merge.
> `PeManageSeed`/MX (`0x08`/`0x09`/`0x0a`) first ship at **MARIANA** (CAYMAN = 0).
> `CONV_LUT_LOAD` `0xe4` is **CAYMAN-first**. `[HIGH/OBSERVED]`

### 4i. The ACT opcodes (Surface A; folded into DVE-PROF at MAVERICK)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0x21` | Activate | A | ACT | S3D3_AC | PWL/affine | Y | Y | Y | Y | Y | FW-76 HIGH |
| `0x22` | ActivateQuantize | A | ACT | S3D3_AQ | requantize | Y | Y | Y | Y | Y | FW-76 HIGH |
| `0x23` | ActivationTableLoad | A | ACT | CTRL_NO (pseudo) | LUT DMA stage | Y | Y | Y | Y | Y | FW-76 HIGH |
| `0x24` | ActivationReadAccumulator | A | ACT | D1_RD | accumulator | Y | Y | Y | Y | Y | FW-76 HIGH |
| `0x25` | Activate2 | A | ACT | — | 2nd activate | ? | Y | Y | Y | Y | IMG-19 HIGH(op) |

### 4j. The MAVERICK `+6` enum additions (159→165; control/DMA/vector — NOT POOL KIT)

| OP | KERNEL/HANDLER | SURF | ENG | STRUCT | DTYPE | SU | CA | MA | MP | MV | SRC / conf |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0xb6` | COMPACT_CONTROL_INST | A | NX/ctrl | — | control | - | - | - | - | Y | IMG-20 HIGH(byte) |
| `0xb9` | DMA_MEMCPY2 | A | NX/DMA | — | DMA | - | - | - | - | Y | IMG-20 HIGH(byte) |
| `0xba` | DMA_IMMEDIATE | A | NX/DMA | — | DMA imm | - | - | - | - | Y | IMG-20 HIGH(byte) |
| (—) | ACTIVATE_MULTIPASS | A | ACT | — | multipass act | - | - | - | - | Y | IMG-19 HIGH(name)/MED(byte) |
| (—) | TENSOR_SCALAR_INT_WIDE | A | DVE/POOL | — | wide int TS | - | - | - | - | Y | IMG-19 HIGH(name)/MED(byte) |
| (—) | TENSOR_TENSOR_INT_WIDE | A | DVE/POOL | — | wide int TT | - | - | - | - | Y | IMG-19 HIGH(name)/MED(byte) |

> The `0xb6`/`0xb9`/`0xba` bytes are **confirmed from the shipped maverick enum** this
> task: `COMPACT_CONTROL_INST = 0xb6`, `DMA_MEMCPY2 = 0xb9`, `DMA_IMMEDIATE = 0xba`.

---

## 5. The per-gen opcode-space evolution (the floor → superset chain)

The GPSIMD opcode space evolves as a **strict superset chain**. Growth lands on
**specific engines** per generation; POOL's compute table (the Q7 `kernel_info_table`)
is the **most stable** surface — key-identical v3..v5. The full step-by-step
add/remove narrative and the TONGA v1 deep-dive are on the sibling
[Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md);
here is the concise count ladder.

| GEN | arch/ct | POOL Q7 KIT | POOL NX SEQ | OTHER-ENGINE OPCODE DELTA |
| --- | --- | --- | --- | --- |
| **SUNDA** (v2) | arch5 / ct6 | **18 flat**, all `spec=0`, **NO `0xF0`**, no dequant/cptc/seqbounds | raw-compare chain (no `−0x41`, no 178-table) | **THE FLOOR.** No RNG, no MX/cptc, no SB2SB, no GetSeqBounds/NonzeroWC, no Sort/ConvLutLoad. dtype 16-base. |
| **CAYMAN** (v3) | arch12 / ct13 | **17/1/2/9**, `0xF0×5`+spec7, full extended layer **ESTABLISHED** | 178-table, `−0x41` norm, 177-bound, 55 real | `+0xF0` bridge; `+0x7b` dequant; `+0xe4` cptc; `+0xbe` seqbounds; `+0xf2` NonzeroWC; `+0xBF` SB2SB; `+Sort`; `+ConvLutLoad`. RNG = Xorwow(SW) only. dtype 16-base. |
| **MARIANA** (v4) | arch20 / ct21 | **17/1/2/9 KEY-IDENTICAL** (`funcVA +0..+0x44` monotonic reloc) | 177-table UNCHANGED (== CAYMAN) | DVE `+7`: `0x30` Exp, `0x77`/`0x78` RandGet/SetState, `0xe0`/`0xe1` Sparsity, `0xe2` Rand2, `0xe3` QuantizeMx. PE `+4`: `0x08` PeManageSeed, `0x09` LdweightsMX, `0x0a` MatmulMX, `0xe4` ConvLutLoad. **ENUM 152→159.** dtype `+FP4_EXP2(0x10)+CPTC1-7(0x19-1F)`. |
| **MARIANA_PLUS** (v4+) | arch28 / ct29 | **17/1/2/9 BYTE-IDENTICAL** SO to MARIANA (`9f2ce049`) | 177-table (recompile) | RNG **unchanged** vs MARIANA. v4+ delta is the **DGE fast-path + `tensor_reshape_transpose_sb2sb`** on the NX SEQ side, **NOT** opcode-space. **STABLE.** |
| **MAVERICK** (v5) | arch36 (INFERRED) / ct37 (OBSERVED) | **17/1/2/9 KEY-IDENTICAL** (stripped `ET_DYN`, fresh rebuild, re-ordered funcVAs, `a92c8ba0`) | DGE/reshape DROPPED firmware-wide (HW-DMA re-arch) | POOL Q7 KIT: **0 added / 0 removed.** **ENUM 159→165 (+6):** `0xb6` COMPACT_CONTROL, `0xb9` DMA_MEMCPY2, `0xba` DMA_IMMEDIATE, ACTIVATE_MULTIPASS, TENSOR_SCALAR_INT_WIDE, TENSOR_TENSOR_INT_WIDE. ACT-fold: `0x23`/`0x25` PROF-armed on DVE. dtype `+FP8_EXP2(0x11)+INT4(0x12)+SFP8_E8..E5(0x13-0x16)`. |

> **v4+ = recompile/flag-refresh, NOT a distinct ISA.** The v4 and v4+ KIT key-sets are
> identical (`9f2ce049` byte-for-byte); MARIANA_PLUS is a re-flag/re-build, not a new
> opcode generation. `[HIGH/OBSERVED]`

**Opcode-count ladder (TPB OPCODE enum):**
SUNDA(v2) floor → CAYMAN(v3) → MARIANA(v4) `152..159` → MARIANA_PLUS(v4+) `== MARIANA` →
MAVERICK(v5) `165`. **Net additive.** The only struct *removals* in the whole chain are
SUNDA's `CUSTOM_OP_HEADER`/`PAYLOAD` (CAYMAN replaced them with the
`kernel_info_table`/EXTISA mechanism). `[HIGH/CARRIED]`

**dtype-space evolution** `[HIGH/OBSERVED — FW-80 ordinal table]`:

* **SUNDA == CAYMAN:** 16-base codes `0x00..0x0F` (INVALID/UINT64/INT8/UINT8/INT16/
  UINT16/BFLOAT16/FP16/INT32/UINT32/FP32/FP32R/INT64/FP8_EXP3/FP8_EXP4/FP8_EXP5).
* **MARIANA adds:** `FP4_EXP2(0x10)` + `CPTC1..7(0x19..0x1F)` (MX element/transport +
  trellis).
* **MAVERICK adds:** `FP8_EXP2(0x11)` + `INT4(0x12)` + `SFP8_E8..E5(0x13..0x16)` (MX
  element + block-scale). FP32 is the universal convert hub; MX rides UINT32 transport +
  `dequant_fmt`.

---

## 6. The master Rosetta join — NKI ↔ SEQ opcode ↔ Q7 `kernel_info` ↔ firmware handler

This is the join that lets a tool walk an **NKI op name → SEQ opcode → `kernel_info`
entry → firmware kernel symbol**. The **NKI column** is `MED/CARRIED` (out-of-corpus —
the NKI wheel and `emit_<op>` MLIR emitters are **not** in this checkout; only the
`s3_lw.h` crumb spells two NKI names in-corpus). Every **device** column
(opcode / KIT / `'S:'` / `'P%i:'` handler) is `HIGH/OBSERVED`.

| NKI.isa op (CARRIED) | OP | SEQ `'S:'` handler | Q7 `'P%i:'` kernel | SRC |
| --- | --- | --- | --- | --- |
| `tensor_tensor` | `0x41` | Tensor-Tensor | TensorTensorArith | FW-49 |
| (tensor_tensor bitvec) | `0x51` | Tensor-Tensor | TensorTensorBitvec | FW-49 |
| `tensor_scalar` | `0x43` | Tensor-Scalar | tensor_scalar_arith | FW-50 |
| (tensor_scalar bitvec) | `0x53` | Tensor-Scalar | — | FW-50 |
| (tensor_scalar ptr, dep.) | `0x44` | Tensor-Scalar-PTR | pool_tensor_scalar_arith | FW-50 |
| (pool avg/max) | `0x45` | Pool | Pool : num_chans | FW-70 |
| `tensor_copy` (copy) | `0x46` | Copy | Copy / pool_copy | FW-72 |
| `tensor_copy` (cast) | `0x47` | Cast | Cast / pool_cast | FW-72 |
| `tensor_partition_reduce` | `0x7c` | CrossLaneReduce | TensorReduce / clr_arith | FW-51 |
| (cross-lane bitvec) | `0x7d` | CrossLaneReduce | TensorReduceBitvec | FW-51 |
| `iota` | `0x7e` | Iota | Iota : num_chans | FW-51 |
| (TensorDequantize / MX) | `0x7b` | TensorDequantize | TensorDequantize | FW-75/63 |
| (GetSequenceBounds) | `0xbe` | GetSequenceBounds | GetSequenceBounds | FW-77 |
| (NonzeroWithCount) | `0xf2` | NonzeroWithCount | NonzeroWithCount | FW-73 |
| (ConvLutLoad / cptc) | `0xe4` | ConvLutLoad | cptc_decode_impl`<1..6>` | IMG-21/FW-15 |
| (Gather) | `0x68` | TensorGather | pool_gather | FW-74 `[hdr s4d4_ic]` |
| (IndirectCopy) | `0xe7` | IndirectCopy | pool_indirect_copy | FW-74 |
| (EmbeddingUpdate) | `0x79` | EmbeddingUpdate | pool_embedding_update | FW-74 |
| (ExtendedInst) | `0xf0` | ExtendedInst | ExtendedInst`<Variant>` | FW-07/15/18 |
| `rand_get_state` | `0x77` | RandGetState | RandGetState | FW-48/66 |
| `rand_set_state` | `0x78` | RandSetState | RandSetState | FW-48/66 |
| (Rand2) | `0xe2` | (DVE) | (DVE Rand2) | FW-48 |
| (StreamTranspose) | `0x6b` | StreamTranspose | — | FW-59 |
| `nc_matmul` (MultiplyMoving) | `0x05` | Matmul | (PE) | FW-66 `[hdr s3_lw]` |
| (LoadStationary / LdWeight) | `0x01` | Ldweights | (PE) | FW-66 `[hdr s3_lw]` |
| `nc_matmul_mx` | `0x0a` | MatmulMX | (PE) | FW-66/IMG-10 |
| (PeManageSeed) | `0x08` | PeManageSeed | (PE) | FW-66/IMG-10 |
| (Exponential) | `0x30` | Exponential (DVE) | (DVE) | FW-42 |
| (move) | `0xa7` | MOVE | — | FW-78 |
| (activate) | `0x21` | Activate (ACT) | (ACT) | FW-76 |

> **PROVENANCE on the NKI column.** Only the `s3_lw.h` crumb spells two NKI names
> in-corpus ("LoadStationary in NKI", "Multiply Moving in NKI"). All other NKI op names
> are `CARRIED` (`MED`); the **device** side of every row (opcode + KIT + `'S:'`/`'P%i:'`
> handler) is `HIGH/OBSERVED`.

---

## 7. Spot-verification — five cells re-carved byte-exact

All against `libnrtucode_internal.so` (`b7c67e89`, **re-hashed MATCH** this task).
`[HIGH/OBSERVED]`

| # | Cell | Carve (file off : size) | sha8 | Anchor | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | **CAYMAN `EXTISA_0`** (Surface B, v3) | `0x2ef7e0` : `0xa260` | `910d41c3` | IMG-06/FW-18 | **PASS** — KIT @file `0x7400`, 17 entries: idx0 `0x7e/0/0x01000080` … idx16 `0x7b/0/0x01004dc4`, the five `0xF0` rows (spec `0,1,2,4,3`). |
| 2 | **NX_POOL DEBUG DRAM SEQ + bridge** (Surface A, v3) | `0x1cdc40` : `0x6f20` | `7bdf6ed7` | FW-07 | **PASS** — SEQ tbl @file `0x814`: `'A'`(`0x41`)=`0x3074`, `'E'`(`0x45`)=`0x3064`, `0xF0` slot @file `0xad0`=`0x3190` (ExtendedInst trampoline). |
| 3 | **MARIANA `EXTISA_0`** (Surface B, v4) | `0x5893c0` : `0xa260` | `9f2ce049` | IMG-11 | **PASS** — 17-entry `(opcode,spec)` key column re-decoded; key diff vs CAYMAN: `ADDED=[] / REMOVED=[]` → IDENTICAL. |
| 4 | **MAVERICK `EXTISA_0`** (Surface B, v5, stripped DYN) | `0x994de0` : `0x7fb0` | `a92c8ba0` | IMG-21 | **PASS** — KIT @file `0x7038`, 17-entry key column; vs CAYMAN: `ADDED=[] / REMOVED=[]` → IDENTICAL. `0xe3` **absent**. |
| 5 | **CAYMAN `EXTISA_3`** cptc 9-entry (Surface B) | `0x2fbf00` : `0x6974` | `052ac31c` | IMG-06 | **PASS** — KIT @VMA `0x020008c8` (file `0x4748`, size `0x48` = 9 entries): `0x7e`/`0x7c`/`0x7d`/`0x45`/`0xbe`/`0xf2`/`0x7b`/`0xe4`(cptc disp `0x01002258`)/`0xf0`-spec7(`0x01003b64`). `EXTISA_1` (1: `0x7e`) + `EXTISA_2` (2: `0x7c`,`0x7d`) also re-carved + confirmed. |

> **NOTE on the `funcVA` prologue decode.** A linear-sweep `ncore2gp` objdump of a
> `funcVA` target shows **FLIX-bundle desync** (the documented `ncore2gp` LX-vs-Vision
> limitation), so the per-`funcVA` `entry a1,N` prologue claim rests on the in-section
> `R_XTENSA_RELATIVE` relocs (one-per-entry, 8-byte stride, addend 0) + the `.xt.prop`
> EXACT matches — **not** on a clean linear disassembly. The **entry-COUNT and KEY-SET**
> (the primary facts of this matrix) **are** byte-exact and were re-verified.
> `[HIGH for count/keys; funcVA-prologue HIGH/CARRIED from the reloc-anchored proof.]`

> **Enum cross-check (shipped `aws_neuron_isa_tpb_common.h`, maverick).** Re-read this
> task: `TENSOR_DEQUANTIZE = 0x7b`, `COMPACT_CONTROL_INST = 0xb6`, `DMA_MEMCPY2 = 0xb9`,
> `DMA_IMMEDIATE = 0xba`, `GET_SEQUENCE_BOUNDS = 0xbe`, `QUANTIZE_MX = 0xe3`,
> `CONV_LUT_LOAD = 0xe4`, `EXTENDED_INST = 0xf0`, `NONZERO_WITH_COUNT = 0xf2`. Every
> byte pin above matches. `[HIGH/OBSERVED]`

---

## 8. The unresolved opcodes (honest remaining-decode targets)

Opcodes present in a SEQ/`kernel_info` table **not yet** mapped to a fully-decoded kernel
(or whose route is `LOW`/`MED`), flagged honestly:

**LOW-confidence `kernel_info_table` rows** (route not pinned by `.xt.prop` EXACT or a
clean single-target trampoline):

* **`0x52`** (`'R'`) — `EXTISA_0` idx11 (`funcVA 0x01003b40`). Trampoline loads a `.data`
  table (`0x02002f04`); no SUNDA-JSON name match, no `.xt.prop` EXACT. `[LOW]`
* **`0xf0` spec0/spec3/spec4** (`EXTISA_0` idx6/10/9) — the EngineNop + the two Rand-band
  routes. Specs 1/2 are HIGH; 0/3/4 route through shared `decode_pool`/`tensor_tensor_
  64bit` paths. `[MED]`
* **`0xf0` spec7** (`EXTISA_3` idx8) — the cptc extended path; `cptc_decode_impl<1..6>`
  DTYPE-selected, the per-impl DTYPE binding not exhaustively decoded. `[MED]`

**MED-confidence opcode→kernel routes** (op number HIGH, internal body MED):

* **`0x45` Pool** (`decode_pool`) — op HIGH, the avg-vs-max + per-channel reduce body
  partly FLIX-desynced. `[MED route]`
* **`0xf2` idx15** — the shared sequence-bounds/dequant region trampoline; the exact
  NonzeroWithCount-vs-GetSequenceBounds split inside the shared region is MED (the
  opcode split itself, `0xbe`/`0xf2`, is HIGH per the enum). `[MED]`

**SEQ-only handlers without a decoded KIT row** (SEQ-only on CAYMAN+, no Q7 KIT entry —
reached as SEQ handlers; bodies not separately decoded): `ModifyPoolConfig`, `Sort`
(`decode_sort`), `SB2SB_Collective` (`0xBF`), the `ConvLutLoad` SEQ arm.
`[presence HIGH/OBSERVED; per-kernel decode PENDING]`

**MAVERICK `+6` enum opcodes not byte-pinned in the carves** (numeric ordinal only):
`ACTIVATE_MULTIPASS`, `TENSOR_SCALAR_INT_WIDE`, `TENSOR_TENSOR_INT_WIDE` — names from the
shipped maverick enum, but the byte value + dispatch trampoline are FLIX-desynced / not
in a clean carve slot. `[name HIGH, byte MED]`

**Out-of-corpus** (cannot be re-verified in this checkout):

* The SUNDA Q7 EXTISA container (`libnrtucode_extisa.so`, sha `444497066f5e1738`) lives
  in the `neuronx-runtime` package — SUNDA's 18 rows (§3) are CARRIED.
* The NKI Python wheel + the `emit_<op>` MLIR emitters (the compiler half of §6).

---

## 9. Reconciliation of the "MAVERICK 6 new opcodes" premise

A common informal claim is that MAVERICK adds six opcodes
`0xb6/0xb9/0xba/0x26/0xf3/0xf4` plus the FIND_INDEX8/MATCH names. Reconciled against the
OBSERVED carves + the shipped maverick enum (re-read this task):

* **Genuine MAVERICK `+6` enum growth (159→165)** is `0xb6 COMPACT_CONTROL_INST`,
  `0xb9 DMA_MEMCPY2`, `0xba DMA_IMMEDIATE` `[HIGH/OBSERVED byte]` + `ACTIVATE_MULTIPASS`,
  `TENSOR_SCALAR_INT_WIDE`, `TENSOR_TENSOR_INT_WIDE` `[HIGH name / MED byte]`. So
  `0xb6`/`0xb9`/`0xba` **ARE** three of the six (CONFIRMED). **`0x26`/`0xf3`/`0xf4` are
  NOT** the pinned bytes for the other three — those three are the INT_WIDE / MULTIPASS
  names, ordinal-not-byte-pinned. `0x26`/`0xf3`/`0xf4` are not corroborated by the carves
  and are **not asserted** here.
* **`FIND_INDEX8` (`0x6e`), `MATCH_VALUE_LOAD` (`0x6d`), `MATCH_REPLACE8` (`0x6f`),
  `MAX8` (`0x6c`)** are **PRE-EXISTING MARIANA-gen DVE opcodes**. At MAVERICK they are
  newly **PROF-armed** on DVE as part of the ACT→DVE fold — they are **NOT** v5
  opcode-space additions. The "+10 PROF arm"
  (`0x23/0x25/0x58/0x61/0x62/0x6c/0x6d/0x6e/0x6f/0x99`) re-arms pre-existing opcodes onto
  DVE; it is a **PROF-table change**, not opcode growth. `[HIGH/OBSERVED]`

This is the honest correction the synthesis is obligated to make.

---

## 10. Honesty ledger

**`HIGH / OBSERVED`** (re-derived byte-exact this task against the shipped binary):

* `internal.so` `b7c67e89` re-hashed MATCH.
* CAYMAN `EXTISA_0` (`910d41c3`) 17-entry KIT re-decoded byte-exact (SPOT 1).
* NX_POOL DEBUG DRAM (`7bdf6ed7`) SEQ table `'A'`/`'E'`/`0xF0` slots (SPOT 2).
* MARIANA (`9f2ce049`) + MAVERICK (`a92c8ba0`) key sets re-decoded; **both key-identical
  to CAYMAN** (`ADDED=[]/REMOVED=[]`) (SPOT 3/4) — cross-gen key stability v3→v4→v5
  confirmed from the binaries, not inferred.
* CAYMAN `EXTISA_3` (`052ac31c`) 9-entry cptc table (incl. `0xe4` + `0xf0`/spec7) +
  `EXTISA_1` (1: `0x7e`) + `EXTISA_2` (2: `0x7c`/`0x7d`) (SPOT 5).
* `0xe3` **absent** from MAVERICK `EXTISA_0`; `0x7b` present — POOL MX surface = `0x7b`.
* The opcode byte pins from the shipped maverick enum.
* The two-dispatch-surface model + the `0xF0` bridge reconciliation.

**`HIGH / CARRIED`** (byte-read upstream, not re-verified here): SUNDA's 18-entry flat
table (§3, out-of-corpus); MARIANA_PLUS `EXTISA_0` byte-identical-to-MARIANA
(`9f2ce049`); the per-opcode struct + dtype bindings.

**`MED / INFERRED`:** several KIT `funcVA` routes (the funcVA prologue claim rests on
reloc + `.xt.prop`, not linear disasm — FLIX-desync); the NKI op-name column (§6, CARRIED
out-of-corpus); the MAVERICK `+6` names ACTIVATE_MULTIPASS / `*_INT_WIDE` byte values
(name HIGH, byte MED).

**`LOW / NOT CLAIMED`:** the `0x52` route; the exhaustive per-opcode SEQ table rows
(FLIX-desync); the SUNDA per-kernel operand layouts; which silicon/runtime selects
DEBUG vs PERF vs DKL; the informal `0x26`/`0xf3`/`0xf4` MAVERICK bytes (§9 — not
corroborated, not asserted).

---

## See also

* [`kernel_info_table` Layout](../firmware/pool/kernel-info-table.md) — the 8-byte record
  format, the `(opcode<<24)|(spec<<16)` key, and the reloc-anchored funcVA proof.
* [Opcode-Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the per-opcode
  kernel ledger that this matrix joins against.
* [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md) — Surface A: the 178-entry
  `'A'`-base ASCII jump table and the `0xF0` bridge slot.
* [Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md) —
  the ISA-evolution step-diff narrative and the TONGA v1 deep-dive (not duplicated here).
* [Codename / Generation Map](../generations/codename-generation-map.md) — the
  SUNDA→CAYMAN→MARIANA→MARIANA_PLUS→MAVERICK ladder (arch_id / ct_id).
* [MAVERICK Profile](../generations/maverick-profile.md) — the v5 image set, the `+6`
  enum growth, and the ACT→DVE fold.
