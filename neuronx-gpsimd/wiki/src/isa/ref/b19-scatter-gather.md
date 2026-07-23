# ISA Batch 19 — SuperGather Scatter/Gather (`ivp_sem_vec_scatter_gather`)

This batch is the **indexed memory engine** of the Vision-Q7 *Cairo* (`ncore2gp`) vector ISA: the
24 mnemonics that turn a `vec` register full of per-lane offsets into 32 (or 16, or 64) independent
memory transactions in a single issue. It is the only group in the partition whose datapath is **not**
an in-core ALU — every member is a `LdSt`-family memory-port op that drives the external **SuperGather**
host engine. Three shapes live here: **two-phase gather** (`gathera*` posts per-lane addresses into a
gather-staging register, `gatherd*` drains the collected elements back into a `vec`); **single-phase
scatter** (`scatter*`, vec → indexed memory); and **scatter-increment** (`scatterinc*`, an in-memory
read-modify-write `+1` — the histogram primitive). A move-into-staging op (`movgatherd`) and a drain
barrier (`scatterw`) round out the 24.

The defining property this batch owns is the **per-lane indirect address `addr[k] = base + offset[k]·elem_sz`**
computed inside the core and handed — together with a per-lane validity predicate — to a memory port
that the rest of the pipeline cannot see compute values. This is the structural opposite of the
aligned vector load/store (a single base, contiguous lanes) and of the regular lane bridges of
[B16](b16-vec-rep.md): here **every lane carries its own independent address**. The
[partition classifier](template-and-partition.md) routes these here by the
`gather|scatter|movgather` verb match; the family is **disjoint** from B17 (`spfma`) and B18
(`hp_fma`) with no shared placement.

Everything below is grounded in the shipped binaries: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks read byte-for-byte), the **register-file
geometry** walked directly out of the `libisa-core.so` regfile descriptor table, the **value
semantics** by reading the matching `opcode__/writeback__ivp_*__stage_10` bodies in `libfiss-base.so`,
the **issue/timing** from the per-op `*_issue`/`*_stall` bodies in `libcas-core.so`, and a byte-exact
**encode/decode oracle** from the device-native `xtensa-elf-as`/`xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`). Confidence tags per
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-round-trip, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` =
re-used at a sibling page's confidence.

> **NOTE — address arithmetic.** `libisa-core.so` (sha256
> `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`, 9 690 712 B, ET_DYN x86-64, not
> stripped). Per `readelf -SW`: `.text` (`0x312c10`) and `.rodata` (`0x3b6e40`) are
> **VMA == file-offset**; `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) carries the per-binary
> delta **`0x200000`** — **not** libtpu's `0x400000`. The regfile descriptor table is at VMA
> `0x74a800` (file `0x54a800`, inside `.data.rel.ro`), stride **56**. Encode thunks live in `.text`
> (VMA == file). `libfiss-base.so` (sha256 `260b110cd59c76b090cbdeb4d5d90f5245be34792618c023ab963ce108d3cc94`,
> 12 330 016 B, NOT stripped, self-contained — zero `nx_*` host callbacks) has `.text` VMA == file.
> Both libraries are in `extracted/` (gitignored; reach with `fd --no-ignore` or an absolute path).

---

## 0. The 24-mnemonic roster

`nm libfiss-base.so | rg 'opcode__ivp_(gather|scatter|movgather).*__stage_10' | sort -u` returns
**exactly 24** distinct mnemonics — the same 24 the `ivp_sem_vec_scatter_gather` SEMANTIC enumerates
and the same 24 `libisa-core.so` carries encode thunks for.

| Sub-family | Phase / role | Mnemonics | n |
| :--- | :--- | :--- | :-: |
| **Gather-address (A)** | post `addr+pred` → `gvr` | `GATHERANX16`, `GATHERANX16T`, `GATHERANX8U`, `GATHERANX8UT`, `GATHERAN_2X32`, `GATHERAN_2X32T` | 6 |
| **Gather-drain (D)** | drain `gvr` → `vec` | `GATHERDNX16`, `GATHERDNX8S`, `GATHERD2NX8_H`, `GATHERD2NX8_L` | 4 |
| **Move-to-staging** | `vec` → `gvr` seed | `MOVGATHERD` | 1 |
| **Scatter** | `vec` → indexed mem | `SCATTERNX16`, `SCATTERNX16T`, `SCATTERNX8U`, `SCATTERNX8UT`, `SCATTERN_2X32`, `SCATTERN_2X32T`, `SCATTER2NX8_H`, `SCATTER2NX8_L`, `SCATTER2NX8T_H`, `SCATTER2NX8T_L` | 10 |
| **Scatter-increment** | `mem[addr] += 1` (RMW) | `SCATTERINCNX16`, `SCATTERINCNX16T` | 2 |
| **Scatter-wait** | engine drain barrier | `SCATTERW` | 1 |
| | | **Total** | **24** |

The `T` suffix is the **predicated** form (a `vbool` masks the lane set); `H`/`L` are the high/low
32-lane halves of a 64-lane 8-bit (`2NX8`) gather/scatter; `U` is unsigned-byte; `S` is the
signed-byte drain. The width tokens are datapath axes of the one 512-bit config, **not** silicon
generations: `NX16` = 32 lanes × 16 bit, `NX8U`/`2NX8` = 64 lanes × 8 bit, `N_2X32` = 16 lanes × 32 bit.

---

## 1. The carriers: register files and engine ports

### 1.1 The four register files this batch touches

The `libisa-core.so` regfile descriptor table (file offset `0x54a800`, **56-byte stride**, eight
entries) was walked directly. Each entry holds a name pointer, accessor pointers, and a
`(bit_width:u32, count:u32, 0, flags:u32)` tuple at entry offset `+0x18`. Reading the name pointers
(`.rodata` VMA == file-offset) resolves every file:

| idx | name | prefix | bit_width | count | flags | role here |
| :-: | :--- | :----- | --------: | ----: | :---- | :--- |
| 0 | `AR` | `a` | 32 | 64 | `0x05` | scalar base pointer (`ars`) |
| 1 | `BR` | `b` | 1 | 16 | `0x05` | (boolean; not used here) |
| 2 | `vec` | `v` | 512 | 32 | `0x05` | offset vector (in) / drain dest / scatter data |
| 3 | `vbool` | `vb` | 64 | 16 | `0x05` | per-lane predicate (the `T` input) |
| 4 | `valign` | `u` | 512 | 4 | `0x05` | (not used by this batch) |
| 5 | `wvec` | `wv` | 1536 | 4 | `0x05` | (wide accumulator; not used here) |
| 6 | `b32_pr` | `pr` | 64 | 16 | `0x05` | (packed predicate; `movgatherd` b32 input) |
| **7** | **`gvr`** | **`gr`** | **512** | **8** | **`0x0d`** | **the gather-staging file (`gr0..gr7`)** |

> **QUIRK — `gvr` is the only file flagged `0x0d`.** Every other regfile descriptor carries
> `flags=0x05`; `idx7` (`gvr`, prefix `gr`) carries **`0x0d`** — the extra bit `0x08` set. That bit is
> the structural marker of the gather-staging file: the only register file that is written by one
> instruction (the A-phase `gt` def) and read by a *different* instruction (the D-phase `gs` use)
> across a multi-cycle latency window, never directly named as an ALU operand. Eight entries
> (`count=8`), 512 bits each — `gr0..gr7`, each holding 16 per-lane 32-bit address descriptors plus a
> collected-data buffer. The register ctype is `xb_gsr` (the firmware spelling); `libctype.so` exposes
> it only through the access thunks `Function_TIE_xt_ivp32_xb_gsr_loadi`/`_storei` (`@0x3e640`/`0x3d550`),
> whose bodies do a **four-`movdqu` 64-byte (512-bit) copy** — confirming the 512-bit width
> independently of the ISA descriptor.

### 1.2 The SuperGather host ports

`libfiss-base.so` is a self-contained, in-process value oracle: the gather/scatter *element bytes*
travel through an external **memory port**, not an in-core leaf. `nm -D libcas-core.so | rg 'nx_GS|nx_ScatterData'`
lists **six** port symbols (four logical groups, two of them dual-pipe `_0`/`_1`) the cycle model
drives:

| Port symbol(s) | width | sched stage | role |
| :--- | :--- | :--- | :--- |
| `VAddrBase` (`= ars`) | 32 b | 1 (early) | scalar base address into the engine |
| `nx_GSControl_{0,1}_interface` | 16 b | 1 (early) | engine control word (operation + offset-size + elem_sz) |
| `nx_GSVAddrOffset_0_interface` | 512 b | 10 | per-lane byte-address offset vector |
| `nx_GSEnable_{0,1}_interface` | 512 b | 10 | per-lane enable / validity mask (predicate AND) |
| `nx_ScatterData_0_interface` | 512 b | 10 | per-lane store-data vector (scatter side) |

The base and control resolve a cycle early (stage 1); the 512-bit lane payload resolves at the
SuperGather extended stage 10. The engine has its own fault model: `LoadStoreSGAccErrorException_exc`
(`@0x1783190` in `libcas-core.so`) and `ImpreciseSGFPVecException_exc` (`@0x177cad0`).

### 1.3 The 16-bit `GSControl` word

The control word the host port consumes is a fan-in of per-mnemonic selectors, packed
`{8'd0, elem_sz_fld[1:0], offst_sz_fld[1:0], operation_fld[3:0]}` (8 + 2 + 2 + 4 = 16 bits exactly):

```c
/* GSControl packing — the per-mnemonic selector fan-in.
 * operation_fld names which of the 6 engine ops fired; elem_sz scales
 * the byte address; offst_sz selects the offset-lane stride.        */
uint16_t gs_control(op_t op) {
    unsigned operation =                          /* operation_fld[3:0] */
          op_is_gathera(op)    ? 1                 /* A-phase post        */
        : op_is_gatherd(op)    ? 2                 /* D-phase drain       */
        : op_is_movgatherd(op) ? 3                 /* vec -> gvr seed     */
        : op_is_scatter(op)    ? 4                 /* indexed store       */
        : op_is_scatterw(op)   ? 5                 /* drain barrier       */
        : op_is_scatterinc(op) ? 6                 /* RMW +1 (histogram)  */
        : 0;
    unsigned elem_sz =                            /* elem_sz_fld[1:0]    */
          (op_is_nx8(op) || op_is_2nx8(op)) ? 0   /*  8-bit element      */
        : (op_is_scatterw(op) || op_is_nx16(op)) ? 1  /* 16-bit element  */
        : op_is_n_2x32(op)   ? 2                  /* 32-bit element      */
        : 0;
    unsigned offst_sz =                           /* offst_sz_fld[1:0]   */
          (op_is_n_2x32(op) && !op_is_scatterw(op)) ? 1 : 0;
    return (elem_sz << 6) | (offst_sz << 4) | operation;
}
```

The `elem_sz_fld` code `{0,1,2}` maps to a **byte scale `{1, 2, 4}`**: the host computes
`addr[k] = base + offset[k]·elem_sz`, where `elem_sz` is **1** for `NX8U`/`2NX8`, **2** for `NX16`, and
**4** for `N_2X32` (§5 worked example).

---

## 2. The encoding model

Every member except `SCATTERW` is a FLIX-slotted coprocessor op. `libisa-core.so` carries one
`Opcode_<mnem>_Slot_<slot>_encode` thunk per placement (129 thunks across the gather/scatter family);
each thunk is a bare `movl $imm,(%rdi); ret` where `imm` is the slot's fixed selector field shifted
into its bundle position.

### 2.1 Slot placement per sub-family

| Sub-family | Slot / unit | FLIX formats |
| :--- | :--- | :--- |
| Gather-address (A), `movgatherd`, scatter-inc | **S0 LdSt** (F1 = LdStALU) | F0/F1/F2/F3/F6/F7 + N0/N1/N2 |
| Gather-drain (D), `gatherd*` | **S1 Ld** | F0/F1/F2/F3/F4/F6/F7/N2 |
| Scatter (plain, non-inc) | **F1 S0 LdStALU + N2 S0 LdSt** (a 2-placement set) | F1, N2 |
| `SCATTERW` | **not FLIX-slotted** (standalone 24-bit `inst`) | — |

The A-phase, scatter, scatter-inc, and `movgatherd` issue in the address-generation slot **S0**; the
D-phase drain issues in the load slot **S1** (it reads staged data, not memory). This S0/S1 split is
why a gather is *two* instructions: the address post and the data drain occupy different FLIX slots and
can co-issue with each other's neighbours. Plain `SCATTERNX16` ships **only** F1/S0 + N2/S0 (no F0
placement), whereas scatter-inc and the A-phase span the full slot set.

### 2.2 Selector CONSTs (read from the encode thunks)

Reading the thunk bodies byte-for-byte (`objdump -d` the `_encode` symbol) gives the exact selector
`imm`. The `imm` is the slot field already shifted into bundle position; right-shifting recovers the
`fld_<FMT>_S<n>_<class>_<hi>_<lo>` constant:

| Mnemonic / slot | thunk addr | `imm` | recovered field | shift |
| :--- | :--- | :--- | :--- | :--- |
| `GATHERANX16` F0/S0 LdSt | `0x3637a0` | `0x10f88000` | bits[31:14] = `0x43e2` | `<<14` |
| `GATHERANX16` F1/S0 LdStALU | `0x3637b0` | `0x134ee000` | bits[31:12] = `0x134ee` | `<<12` |
| `SCATTERNX16` F1/S0 LdStALU | `0x363d50` | `0x12640000` | bits[31:14] = `0x4990` | `<<14` |
| `SCATTERNX16` N2/S0 LdSt | `0x363d60` | `0x11670000` | bits[31:14] = `0x459c` | `<<14` |
| `SCATTERINCNX16` F0/S0 LdSt | `0x363e40` | `0x10f88180` | (the inc selector) | — |
| `SCATTERW` `inst` | `0x363e30` | `0x00060000` | `fld_Inst_23_0 = 0x060000` | (whole word) |

The A-phase width discriminator lives in the 2-bit field that distinguishes the three widths sharing
base `0x43e2`: `NX16=0`, `N_2X32=1`, `NX8U=2`. The plain-scatter N2/S0 selectors form a contiguous
block `0x4598..0x459e` — the operation/width fan-in into `GSControl` (§2.4 differential).

> **NOTE — register-select field map (slot-local, MSB-first, split-aware).** From the
> `fld_ivp_sem_vec_scatter_gather_*` FIELDDEFs, confirmed live by varying each operand (§4):
> `gs` (gvr in, D-phase) bits[2:0]; `gt` (gvr out, A-phase) bits[6:4] in S0 LdSt; `vt` (vec out, drain
> dest) bits[12:8]; `vs` (vec in, offset) bits[13:9] (slot-context); `vr` (vec in, scatter data)
> bits[13:9]; `vbr` (vbool in, `T` predicate) bits[16:13]. The `gvr` index is a 3-bit field (`& 0x7`)
> — exactly the 8-entry geometry of §1.1.

### 2.3 Shared ABI

All 23 coprocessor members (`SCATTERW` excepted) gate on `CPENABLE` (the vector-coprocessor enable,
sampled at sched stage 3) and raise `Coprocessor1Exception` — *before* any memory effect — if the
enable bit is clear; the op is then squashed. `SCATTERW` carries no `CPENABLE` and no exception: it is
a pure pipeline barrier with empty in/out operand lists, only the engine-drain DEFs.

### 2.4 Selector-CONST differential (N2/S0 scatter slot, device round-trip)

Assembling the five N2-format plain-scatter bundles and reading the LE 64-bit bundle words back from
`xtensa-elf-objdump` isolates the per-mnemonic selector (everything else identical):

```
SCATTERNX16    029c58c49c0443ef   N2/S0 const 0x459c   (byte[3]=c4)
SCATTERNX8U    029c58c69c0443ef   0x459e   c4->c6  (+2)   width bit
SCATTER2NX8_H  029c58c09c0443ef   0x4598   c4->c0  (-4)
SCATTER2NX8_L  029c58c29c0443ef   0x459a   c4->c2  (-2)
SCATTERN_2X32  029c58e09c0443ef   0x4599   58c4->58e0   width bit set
```

The deltas land in exactly the bundle bits carrying the N2/S0 selector, and their magnitudes match the
recovered CONST deltas — the device assembler emits the same selector the `libisa-core.so` thunks
encode, i.e. the `GSControl` operation/width fan-in.

---

## 3. The gather-address (A) phase — 6 ops

```
OPERANDS  OUT[gt : gvr (512b)]   IN[ars : AR (32b, base), vs : vec (512b, offsets)]  (+ vbr : vbool for T)
STATE/EXC STATE_IN = CPENABLE@3 ;  EXC = Coprocessor1Exception
FIRMWARE  xb_gsr IVP_GATHERANX16(const short* base, xb_vecNx16U off)   /* returns the gsr   */
          NX8U: const unsigned char* base ;  AN_2X32: const int* base  /* T adds vboolN pred */
```

The A-phase posts per-lane byte addresses and a validity predicate into the staging register `gt`. The
value body `opcode__ivp_gatheranx16__stage_10` (`@0x2bfc60` in `libfiss-base.so`) does five things on
the op-context (`%rdi`) only — no memory callback:

```c
/* opcode__ivp_gatheranx16__stage_10  @0x2bfc60  (libfiss-base.so).
 * Stages the per-lane offset image + sentinel + control marker into the gsr.
 * elem_sz: NX16=2, NX8U=1, N_2X32=4 (the byte scale the host applies). */
void gathera_stage10_NX16(op_ctx_t *c) {
    /* (1) copy the 16 per-lane 32-bit offsets into the gsr offset image
     *     (identity copy 0xd4.. -> 0x118.., 16 words = the staged addresses). */
    for (int k = 0; k < 16; k++)
        c->gsr_off[k] = c->voff[k];               /* base+off*elem_sz done host-side */

    /* (2) INIT the collected-data buffer to the "not-yet-collected" sentinel.
     *     16x  movl $0xffffffff, 0x15c+4k(%rdi)  @0x2bfd31..  (verified: 16 stores) */
    for (int k = 0; k < 16; k++)
        c->gsr_data[k] = 0xffffffffu;             /* the OOB / miss sentinel */

    /* (3) zero the per-lane validity/enable accumulators (0x2c.., 0x6c..). */
    memset(c->enable, 0, 16 * sizeof(uint32_t));

    /* (4) write the PER-WIDTH staging control marker @0x158:
     *     NX16 -> 0x41,  NX8U -> 0x01,  N_2X32 -> 0x91  (the elem_sz descriptor). */
    c->gsr_marker = 0x41;                          /* movl $0x41,0x158(%rdi) @0x2bfee0 */

    /* (5) fold the offset-size discriminator from c->ctl[0x24] into GSControl. */
    c->offst_sz = ((c->ctl >> 1) ^ 1) & 1;
}
```

The per-lane address itself, `addr[k] = base + offset[k]·elem_sz` with the bounds/predicate AND, is
named by `writeback__ivp_gatheranx16` (§3.1). The unsigned offset vector indexes *elements*; the host
scales to bytes by `elem_sz`. For the `T` form, `valid[k] &= vbr[k]`.

> **QUIRK — the `0xffffffff` not-yet-collected sentinel.** The A-phase writes **sixteen** literal
> `movl $0xffffffff` stores (`@0x2bfd31..0x2bfdc7`, one per lane-word) into the collected-data buffer
> `0x15c..0x198` *before* any element is gathered. A lane whose predicate is 0 (out of bounds, or
> masked by `T`) is never overwritten, so its drained value stays `0xffffffff`. This is the in-band
> OOB sentinel: the engine does not need a separate miss flag — the un-gathered lane simply retains
> `0xffffffff` and the host/firmware applies the documented miss policy (pool_gather → 0) downstream.

> **QUIRK — the per-width staging control marker.** `@0x158` the A-phase writes a *per-width* byte:
> **`NX16 → 0x41`** (`0b0100_0001`: bit6 = 16-bit element, bit0 = active/valid), **`NX8U → 0x01`**
> (`0b0000_0001`: 8-bit element, only the active flag), **`N_2X32 → 0x91`** (`0b1001_0001`: bit7\|bit4 =
> 32-bit element, bit0 = active flag). Disassembled at `0x2bfee0` / `0x2bf8d0` / `0x2c04f0`. This is
> the in-process companion to the `GSControl.elem_sz_fld`: the gsr carries the element-size descriptor
> so the drain knows the lane width. A model that hard-codes the `NX16` value `0x41` for all widths is
> wrong.

**Encoding (representative `GATHERANX16`, 9 placements).** Base selector `0x43e2` (F0/S0), width field
`{NX16=0, N_2X32=1, NX8U=2}` in bits[8:7]; the `T` variants narrow the selector and carry `vbr` in
bits[16:13]. **Timing:** `ars@1`, `vs(offset)@10`, `CPENABLE@3`; DEF `gt(gvr)@10`,
`GSVAddrOffset/GSEnable@10`, `VAddrBase/GSControl@1`. The gvr def posts at LAT `0xa` (10), the AR base
at LAT 1 (read literally from the `GATHERANX8UT_issue` body: `mov $0x1,%esi` before the AR hook,
`mov $0xa,%esi` before the gvr hook).

### 3.1 The writeback GSEnable validity-AND merge

`writeback__ivp_gatheranx16` (`@0x2c0070`) merges the freshly
gathered lanes with the existing destination register under the per-lane enable mask. The disassembly
shows the pattern `and EN ; not EN ; and 0xNN(%rdi) ; or` repeated over `0x2c, 0x30, 0x34, 0x38, …`
(the 16 vec writeback slots):

```c
/* writeback__ivp_gatheranx16  @0x2c0070  (libfiss-base.so).
 * Per lane-word i: keep the gathered element where enabled, else fall
 * through to the existing destination register data. The exact GSEnable AND.
 *   2c0099: and  %r8d,%r9d        ; gathered & EN
 *   2c009c: not  %r8d             ; ~EN
 *   2c009f: and  0x2c(%rdi),%r8d  ; newdata & ~EN
 *   2c00a3: or   %r9d,%r8d        ; (gathered & EN) | (newdata & ~EN)   */
void gather_writeback(op_ctx_t *c, uint32_t dest[16]) {
    if (c->wb_guard != 0) return;                 /* 0x1a4(%rdi)==0 gates the merge */
    for (int i = 0; i < 16; i++) {
        uint32_t en  = c->enable[i];              /* 0x6c+4i : 0x0 or 0xffffffff   */
        uint32_t nd  = c->newdata[i];             /* 0x2c+4i : existing reg data   */
        dest[i] = (c->gathered[i] & en) | (nd & ~en);
    }
}
```

The enable word per lane is `0x0` (disabled) or `0xffffffff` (enabled) — produced by
`module__xdref_bitkillf_32_4` (`@0x82d010`), whose entire body is the bit0 sign-broadcast:

```c
/* module__xdref_bitkillf_32_4  @0x82d010  (libfiss-base.so).
 *   82d010: shl $0x1f,%esi   ; push lane-predicate bit0 to the sign
 *   82d013: sar $0x1f,%esi   ; arithmetic-broadcast -> 0x0 or 0xffffffff
 *   82d016: mov %esi,(%rdx)  */
uint32_t bitkillf_32_4(uint32_t pred_lane) {
    return (uint32_t)((int32_t)(pred_lane << 31) >> 31);   /* 0x0 / 0xffffffff */
}
```

For the predicated `T` forms, the 16-bit `vbMask` is expanded by **sixteen** `bitkillf_32_4` calls —
one per lane — into the 16 per-lane enable words; for the non-predicated forms all 16 enables are
`0xffffffff`. `GSEnable = op_pred ? vbr_expanded : all-ones`.

---

## 4. The gather-drain (D) phase + `movgatherd` — 5 ops

```
GATHERDNX16 / NX8S : OUT[vt : vec (512b)]   IN[gs : gvr (512b)]
GATHERD2NX8_L      : OUT[vt : vec, low 32 i8 half]   IN[gs : gvr]
GATHERD2NX8_H      : OUT[vt : vec, high half merged /*inout*/]   IN[gs : gvr]
MOVGATHERD         : OUT[gt : gvr (512b)]   IN[vs : vec (512b)]   (vec -> gsr seed)
FIRMWARE  xb_vecNx16  IVP_GATHERDNX16(xb_gsr gsr)   /* drain -> 32x i16          */
          xb_vecNx16  IVP_GATHERDNX8S(xb_gsr gsr)   /* drain signed 8->16        */
          xb_vec2Nx8  IVP_GATHERD2NX8_L(xb_gsr gsr) /* low 32 i8 lanes (fresh)   */
          void        IVP_GATHERD2NX8_H(xb_vec2Nx8 a /*inout*/, xb_gsr gsr)  /* high merge */
          xb_gsr      IVP_MOVGATHERD(xb_vec2Nx8 v)  /* move a vector INTO the gsr */
```

The drain muxes the staged collected data (posted during the A-phase) into the destination `vec` image
— `opcode__ivp_gatherdnx16__stage_10` (`@0x2c23f0`, body `[0x2c23f0, 0x2c2540)` to the next symbol
`stateload__ivp_gatherdnx16`) touches only `%rdi`, **zero calls** — an inline mux, no memory callback;
the width/sign mux is the only per-mnemonic difference:

```c
/* opcode__ivp_gatherdnx16__stage_10  @0x2c23f0  (libfiss-base.so).
 * Unpack the 16 staged gathered lanes (gsr 0x70..0xac) into the dest-vec
 * writeback slots (0x2c..0x68) with an inline width/sign mux; init the
 * per-lane writeback validity to 0xffffffff. No memory read, no sub-calls. */
void gatherd_stage10(op_ctx_t *c, vec_t *vt, gsr_t *gs) {
    for (int k = 0; k < 16; k++) {
        uint32_t e = gs->collected[k];        /* staged element (0xffffffff if miss) */
        vt->lane[k] =
              IS_NX16(c)   ? (e & 0xffff)                     /* raw 16-bit       */
            : IS_NX8S(c)   ? (uint32_t)(int8_t)(e & 0xff)     /* sign-extend 8->16 */
            : IS_2NX8_L(c) ? (e & 0xff)                       /* low  i8 half     */
            :                ((e & 0xff) << 8);               /* 2NX8_H high half  */
        vt->valid[k] = 0xffffffffu;
    }
}
```

`GATHERD2NX8_H` is an `inout`: it merges the high 8-bit half into the existing `vec` while preserving
the low half seeded by `GATHERD2NX8_L` — the 64-lane i8 gather needs the two-half / two-gsr split.
`MOVGATHERD` is the inverse: it moves a vector *into* the gsr (seeds an in-place re-gather).
`[HIGH/OBSERVED structure; MED/INFERRED for the exact `_H` half-lane mask]`

**Encoding.** Drain ops live in **S1 Ld** (not the A/scatter S0); `GATHERDNX16` has 8 placements. The
S1 drain selector for `GATHERDNX16` is the bundle word `02a462285ce0c52f` (§6.3). `MOVGATHERD` is S0
LdSt (it writes `gt:gvr` from `vs:vec`). **Timing:** `gs(gvr)@10`, `CPENABLE@3`; DEF `vt(vec)@11` — the
drained vector lands **one stage later** than the post (LAT 11 vs 10), and the drain `*_stall` waits on
the gvr at use-LAT 9 (the bypass window).

> **QUIRK — the two-phase issue.** A logical gather `IVP_GATHERNX16(base, off)` is the *composition*
> `IVP_GATHERDNX16(IVP_GATHERANX16(base, off))` — two distinct instructions in two distinct FLIX slots
> (S0 post, S1 drain) separated by a latency-aware interlock. The A-phase posts the gvr at stage 10;
> the D-phase drain reads it at use-LAT 9 and retires `vt` at stage 11. The `my_gvr_stall` interlock
> (`libcas-core.so @0x17a9970`) blocks the drain until the posted gvr has propagated far enough to be
> forwardable across the 32-deep scoreboard ring (window 10). The two phases must *both* be scheduled —
> a drain without a matching post reads a stale gsr. This is why the family ships address-post and
> data-drain as separate mnemonics rather than one fused gather op.

---

## 5. Scatter, scatter-increment, scatter-wait — 13 ops

### 5.1 Plain scatter

```
OPERANDS  IN[vr : vec (512b, store DATA), ars : AR (32b, base), vs : vec (512b, offsets)]
          (+ vbr : vbool for T).  OUT = engine ports (VAddrBase, GSVAddrOffset, GSControl,
          GSEnable, ScatterData) — no register dest.
FIRMWARE  void IVP_SCATTERNX16(xb_vecNx16 data, short* base, xb_vecNx16U off)
```

Scatter is **single-phase** — no gsr staging. Per enabled lane `k`, the op posts
`addr[k] = base + offset[k]·elem_sz` and `data[k] = vr[k]` to the scatter port with the `GSEnable[k]`
validity bit; disabled lanes are killed (not written). `elem_sz`: `NX16=2, NX8U=1, N_2X32=4, 2NX8=1`.
The byte-pack into lanes is done by `module__xdref_replo8_16_16` (`@0x82d020`):

```c
/* opcode__ivp_scatternx8u__stage_10  @0x2c3c70  (libfiss-base.so;
 * body [0x2c3c70, 0x2c4160) to the next symbol stateload__ivp_scatternx8u).
 * Exactly 32 calls to module__xdref_replo8_16_16 @0x82d020 — one per lane —
 * pack the low-8 scatter datum into each of the 32 store lanes. No memory
 * callback in the stage body. */
void scatter_stage10_nx8u(op_ctx_t *c) {
    for (int k = 0; k < 32; k++)
        c->scatter_data[k] = replo8(c->vr[k]);    /* movzbl; (shl/or pack) */
    /* address = base + off*1 ; GSEnable gates per lane ; post to ScatterData port */
}
```

**Encoding.** Plain scatter is the 2-placement set **F1/S0 LdStALU + N2/S0 LdSt**:

| Mnemonic | F1/S0 bits[31:14] | N2/S0 bits[31:14] |
| :--- | :--- | :--- |
| `SCATTERNX16` | `0x4990` | `0x459c` |
| `SCATTERNX8U` | `0x4992` | `0x459e` |
| `SCATTERN_2X32` | `0x4991` | `0x4599` |
| `SCATTER2NX8_L` | `0x4973` | `0x459a` |
| `SCATTER2NX8_H` | `0x4971` | `0x4598` |

The N2/S0 block `0x4598..0x459e` is the width/half/operation fan-in (§2.4). `T` variants narrow to
bits[31:19] + a [14] sub-bit + bits[16:13] `vbr`. **Timing:** `ars@1`, `vr@10`, `vs@10`, `CPENABLE@3`;
`VAddrBase/GSControl@1`, `GSVAddrOffset/GSEnable/ScatterData@10`. The scatter `*_stall` adds the
`WB_P`/`WB_N` writeback-port structural hazards and `MS_DISPST` (multi-slot dispatch).

### 5.2 Scatter-increment (the histogram RMW)

```
OPERANDS  IN[ars : AR (32b, base), vs : vec (512b, offsets)]  (+ vbr for T).  NO data operand vr!
FIRMWARE  void IVP_SCATTERINCNX16(short* base, xb_vecNx16U off)   /* base + offset only */
```

`opcode__ivp_scatterincnx16__stage_10` (`@0x2c8a50`) has **zero `call`s** — a
fully-inline body (`ret` at `0x2c8c5e`). It marshals *only* the offset operand (`0x50 → 0x94`) and
inits the staging area (`0xd8..` to `0xffffffff`); there is **no separate data window** — the `+1`
increment is implicit:

```c
/* opcode__ivp_scatterincnx16__stage_10  @0x2c8a50  (libfiss-base.so).
 *   2c8a50: mov 0x50(%rdi),%eax       ; offset operand (no data operand)
 *   2c8a59: mov %eax,0x94(%rdi)       ; marshal into staging
 *   2c8b00..: 16x movl $0xffffffff    ; init no-data staging region
 * Per enabled lane: mem[base + offset[k]*2] += 1   (read-modify-write). */
void scatterinc_stage10(op_ctx_t *c) {
    for (int k = 0; k < 32; k++)
        if (c->enable[k])                          /* T: enable &= vbr[k] */
            mem_rmw_add(c->base + c->off[k] * 2, +1);   /* the histogram accumulate */
}
```

**Encoding.** S0 LdSt, full slot set (`F0/F1/F2/F3/F6/F7 + N2`, all `s0_ldst`, F1=`ldstalu`):
`F0/S0 = 0x43e2` + bits[8:4]=`0x18` (encode CONST `0x10f88180`); `N2/S0 bits[31:14] = 0x4628`; `T`:
`F0/S0 bits[31:17]=0x863` + bits[7:4]=`0xe`. **Timing:** `ars@1`, `vs@10`, `CPENABLE@3`; engine ports
as scatter.

> **QUIRK — scatter-inc collision-accumulate (the histogram resolution).** When two or more lanes carry
> the **same** target address, `SCATTERINCNX16` accumulates: each colliding lane contributes `+1` to
> the same memory location. The engine serialises the read-modify-write across colliding lanes, so the
> final cell value is the *count* of lanes that hit it. Because the value contribution is a commutative
> sum, the histogram total is **order-independent** — the exact cross-lane cycle interleave is a
> port-timing detail that does not affect the value. Worked example below.
> `[HIGH/OBSERVED value / MED/INFERRED exact hardware cycle ordering]`

#### Worked multi-lane collision example

Two lanes hitting the same address, with a 16-bit histogram table `hist[]` (the `NX16` element width,
`elem_sz = 2`):

```
Setup:  base = &hist[0]   (hist[] zero-initialised, int16)
        offset vector v2  = [ 5, 7, 5, 7, 5, ... ]   (lanes 0 and 2 BOTH index element 5;
                                                       lanes 1 and 3 BOTH index element 7)
        GSEnable          = all lanes valid          (non-predicated SCATTERINCNX16)

Per-lane address (addr[k] = base + offset[k] * elem_sz, elem_sz = 2):
        lane 0:  &hist[0] + 5*2 = byte addr 10  -> hist[5]
        lane 1:  &hist[0] + 7*2 = byte addr 14  -> hist[7]
        lane 2:  &hist[0] + 5*2 = byte addr 10  -> hist[5]   <-- COLLIDES with lane 0
        lane 3:  &hist[0] + 7*2 = byte addr 14  -> hist[7]   <-- COLLIDES with lane 1

RMW resolution (each enabled lane: mem[addr] += 1):
        hist[5]:  lane 0 reads 0, writes 1 ; lane 2 reads 1, writes 2   -> hist[5] = 2
        hist[7]:  lane 1 reads 0, writes 1 ; lane 3 reads 1, writes 2   -> hist[7] = 2

Result:  hist[5] = 2,  hist[7] = 2   (each address accumulates the +1 of BOTH colliding lanes).
```

Generalising: all 32 lanes targeting element 7 (`offset = [7,7,…,7]`) yield `hist[7] = 32`; a
`collide_pairs` pattern (`offset[k] = k/2`, two lanes per address) yields `+2` per touched element.
The accumulate is the serialised RMW the SuperGather engine applies across collisions; the sum is
commutative, so the histogram total is bit-exact regardless of the per-lane RMW order.

### 5.3 Scatter-wait (barrier)

```
OPERANDS  NONE.   STATE/EXC  none (no CPENABLE, no exception).
FIRMWARE  void IVP_SCATTERW(void).
```

`SCATTERW` is the scatter-writeback / drain barrier: it waits until all outstanding posted scatters
(and the increment RMWs) have committed to memory, ordering scatter → subsequent-load. No datapath, no
register effect — only `DEF GSControl@1 + GSEnable@10` (drains the engine). It is the single member
with an `opcode_complete__ivp_scatterw` completion hook (`@0x2c8a40` — the only gather/scatter op with
one; its body is the trivial `xor %eax,%eax; ret`).

**Encoding.** **Not FLIX-slotted** — a standalone 24-bit scalar instruction (`inst` format): the entire
3-byte instruction word is `fld_Inst_23_0 = 0x060000`. The encode thunk
`Opcode_ivp_scatterw_Slot_inst_encode` (`@0x363e30`) emits exactly `movl $0x60000,(%rdi)`. The device
assembles `IVP_SCATTERW` to the three bytes `06 00 00` (§6.1).

---

## 6. Worked bit-patterns — device round-trip

Every example below was assembled by `xtensa-elf-as` and disassembled back by `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`, `XTENSA_SYSTEM=…/ncore2gp/config`); bytes are shown as objdump
prints them (stored order). All disassembled back to the exact mnemonic + operand spelling.

### 6.1 `SCATTERW` (standalone 24-bit insn)

```asm
    ivp_scatterw
```
```
bytes (3 B):  06 00 00        -> disasm: "ivp_scatterw"
fld_Inst_23_0 = 0x060000 = the entire instruction word.   [matches §5.3 CONST exactly]
```

### 6.2 `GATHERANX16 gr0, a3, v2` (A-phase post, → gsr)

```asm
    { ivp_gatheranx16  gr0, a3, v2; nop }
```
```
bundle (8 B, N2 fmt):  02 9c 60 00 9c 04 43 4f
    -> disasm: "{ ivp_gatheranx16  gr0, a3, v2; nop }"
OUT gt=gr0, IN ars=a3 (base), vs=v2 (unsigned offset vector).
Posts addr[k] = a3 + v2[k]*2 + validity into gr0 @stage 10.
```

The width discriminator is byte[3]: `GATHERANX16 -> 00`, `GATHERANX8U -> 02`, `GATHERAN_2X32 -> 04`
(round-tripped: `029c60009c04434f` / `029c60029c04434f` / `029c60049c04434f`).

### 6.3 `GATHERDNX16 v5, gr0` (D-phase drain, gsr → vec)

```asm
    { nop; ivp_gatherdnx16  v5, gr0 }
```
```
bundle (8 B):  02 a4 62 28 5c e0 c5 2f
    -> disasm: "{ nop; ivp_gatherdnx16  v5, gr0 }"   (S1 Ld slot)
OUT vt=v5, IN gs=gr0. Drains the 32x i16 collected elements out of gr0 into v5 @stage 11.
```

Varying `gr0 -> gr7` changes byte[3] `e0 -> e7` (the 3-bit `gs` field bits[2:0]); varying `v5 -> v8`
changes the `vt` field — both confirmed live.

### 6.4 `SCATTERINCNX16 a3, v2` (RMW histogram, no data operand)

```asm
    { ivp_scatterincnx16  a3, v2; nop }
```
```
bundle (8 B):  02 9c 62 40 9c 04 43 6f
    -> disasm: "{ ivp_scatterincnx16  a3, v2; nop }"
IN ars=a3 (base), vs=v2 (offset). Per enabled lane: mem[a3 + v2[k]*2] += 1.
The TWO-operand spelling (no data vr) confirms the no-data RMW shape.
```

### 6.5 `T` (predicated) and `movgatherd` round-trips

```
GATHERANX16T gr0, a3, v2, vb1   -> "{ ivp_gatheranx16t gr0, a3, v2, vb1; nop }"  029c54029c04430f
SCATTERNX16T v1,  a3, v2, vb1   -> "{ ivp_scatternx16t v1,  a3, v2, vb1; nop }"  029c2a029c04432f
MOVGATHERD   gr1, v2            -> "{ ivp_movgatherd   gr1, v2;          nop }"  029c60069c04406f
GATHERDNX8S  v6,  gr0           -> "{ nop; ivp_gatherdnx8s   v6, gr0 }"          02a4622878f0c52f
GATHERD2NX8_L v6, gr0           -> "{ nop; ivp_gatherd2nx8_l v6, gr0 }"          02a4622878d8c52f
```

The `vbr` (vbool) operand ANDs into `GSEnable` per lane (byte[3] tracks the vbool index: `vb1 -> 54`,
`vb15 -> ...c6`). Varying the gvr index across the A/D forms confirms the `gt`/`gs` 3-bit fields. The
full A/D/scatter/inc/movgatherd/T/H/L/width round-trip assembled clean and disassembled back to the
exact mnemonic + operand spelling.

---

## 7. Differential validation

The family was run through the four-oracle bit-exact method on a 4 700-probe value+marshal corpus plus
a 3 264-lane-word live `GSEnable`-merge fuzz plus a 13-bundle device round-trip — zero divergences.
Because the gathered/scattered element *bytes* travel through the SuperGather memory port (not an
in-process leaf), the differential splits in two: `[HIGH/OBSERVED, CARRIED from the validation pass]`

* **Layer 1 (in-process marshal, driven live):** the offset → gsr image, the `0xffffffff` sentinel,
  the validity-zeroing, the per-width control marker (`0x41`/`0x01`/`0x91`), the scatter-inc no-data
  marshal, and the writeback `(gathered & EN) | (newdata & ~EN)` merge — all executed against the real
  shipped `libfiss-base.so` and bit-exact.
* **Layer 2 (end-to-end value over a shared memory table):** gather with OOB → miss, scatter with
  last-writer-wins for duplicate destinations, scatter-add histogram (`+32` for 32-on-1, `+2` for
  collide-pairs), and the three `elem_sz` byte-scales `{2, 1, 4}` — all bit-exact.

The `elem_sz` scale was verified directly: with `byte[i] = i` and a single offset 5, `NX16` gathers
byte addr 10 (`0x0b0a`), `NX8U` byte addr 5 (`0x05`), `N_2X32` byte addr 20 (`0x1514`) — `addr =
base + offset·elem_sz` with `elem_sz ∈ {2, 1, 4}` exactly.

> **CORRECTION — the per-width marker is not a constant.** An initial closed-form model hard-coded the
> gsr staging control marker `@0x158` as the `NX16` value `0x41` for all widths. Executing the real
> `libfiss-base.so` A-phase bodies surfaced a per-width constant (`NX16=0x41`, `NX8U=0x01`,
> `N_2X32=0x91`), disassembled at `0x2bfee0` / `0x2bf8d0` / `0x2c04f0` and root-caused as the elem_sz
> descriptor. The model is corrected to be width-aware (§3). The offset image and the `0xffffffff`
> sentinel were already bit-exact across all three widths.

This family is the value-level resolution of the firmware indirection engine: the
[firmware indirection / gather kernels](../../firmware/kernels/indirection-gather.md) lower
`nc_n_gather → GATHER` and `local_gather → INDIRECT_COPY` onto exactly these SuperGather ops, and the
[cas/fiss SuperGather ISS model](../../iss/cas-supergather.md) is the cycle/value oracle for the
two-phase post/drain. See also the
[formal gather/scatter semantics](../semantics/group-semantics-i.md) for the TIE-level `GSControl`
RTL and the [gather/scatter differential validation](../../validation/gather-scatter.md) for the
4-oracle bit-exact ledger.

---

## 8. Coverage / verification ledger

* `[HIGH/OBSERVED]` Roster = **exactly 24** mnemonics (`nm libfiss-base.so` over the
  `opcode__ivp_(gather|scatter|movgather).*__stage_10` symbols); disjoint from B17 (`spfma`) and B18
  (`hp_fma`).
* `[HIGH/OBSERVED]` GVR geometry walked directly from the `libisa-core.so` regfile descriptor table
  (file `0x54a800`, stride 56): `idx7 = gvr` (prefix `gr`), **512 bit × 8**, flags **`0x0d`** (the
  unique `0x08` bit) — alongside `vec` (512×32), `vbool` (64×16), `valign` (512×4), `AR` (32×64),
  `BR`, `wvec`, `b32_pr`. Name pointers resolved from `.rodata`; the 512-bit width cross-checked from
  the `xb_gsr_loadi` ctype thunk's 64-byte copy.
* `[HIGH/OBSERVED]` Per-width A-phase staging marker `@0x158`: `NX16=0x41` (`@0x2bfee0`), `NX8U=0x01`
  (`@0x2bf8d0`), `N_2X32=0x91` (`@0x2c04f0`); 16× `0xffffffff` sentinel (`@0x2bfd31..`); writeback
  `(g&EN)|(nd&~EN)` (`@0x2c0099..`); `bitkillf_32_4` = `shl 0x1f; sar 0x1f` (`@0x82d010`); drain an
  inline mux (zero calls, `@0x2c23f0`); scatter-inc inline (zero calls, `@0x2c8a50`); `scatternx8u`
  32× lane pack via `replo8_16_16` (`@0x2c3c70`, body to `0x2c4160`); `scatterw` the sole
  `opcode_complete` (`@0x2c8a40`).
* `[HIGH/OBSERVED]` Selector CONSTs read from the encode thunks: `GATHERANX16` F0/S0 `0x10f88000`
  (`0x43e2<<14`), `SCATTERNX16` N2/S0 `0x11670000` (`0x459c<<14`) and F1/S0 `0x12640000` (`0x4990<<14`),
  `SCATTERINCNX16` F0/S0 `0x10f88180`, `SCATTERW` `0x00060000`; 6 host ports + 2 SG faults present in
  `libcas-core.so`.
* `[HIGH/OBSERVED]` Slot map: A-phase/scatter/scatter-inc/`movgatherd` = S0 LdSt (F1 = LdStALU);
  drain = S1 Ld; `SCATTERW` = standalone `inst`. Timing: base/control @stage 1, lane payload + A-phase
  gsr write @stage 10, D-phase vec drain @stage 11, `CPENABLE` @stage 3 (`mov $0xa/$0x1/$0xb,%esi`
  read from the `libcas-core.so` `*_issue` bodies).
* `[HIGH/OBSERVED]` 17-bundle device round-trip (A×3 widths, D×4, movgatherd, scatter×5, scatter-inc,
  T×2): every sub-family assembled and disassembled back to the exact mnemonic + operand
  spelling; the N2/S0 scatter differential matches the recovered CONST deltas.
* `[MED/INFERRED]` The exact cross-lane *cycle interleave* of the `SCATTERINCNX16` RMW under collision
  is a port-timing detail; the accumulate *value* (commutative sum) is order-independent and bit-exact.
* `[MED/INFERRED]` The `GATHERD2NX8_H` `inout` high-half merge mask (preserve low, write high) is read
  from the firmware CTYPE (`a /*inout*/`) and the `_L`/`_H` pairing — structurally covered, not
  transcribed bit-by-bit.
* `[LOW]` The full per-slot bit scatter across all 8–13 placements per op is not hand-enumerated; the
  primary S0/S1 map + the representative selector CONSTs + the device round-trip stand in for the
  complete per-slot decode.
