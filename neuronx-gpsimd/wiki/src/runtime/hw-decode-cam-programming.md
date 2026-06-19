# HW-Decode CAM-Table Programming

> **Scope.** How the host Neuron runtime (`libnrt.so`, KaenaHal) programs the
> per-engine **HW-Decode profiler CAM + PROFILE table** into the Vision-Q7 Pool
> engine's device CSR space during bring-up on **Cayman (NC-v3)** and **Mariana
> (NC-v4)**, and the per-arch **Q7 run-stall release** divergence: the host
> `0xFF→0x00` CSR broadcast on Cayman/Mariana versus the **SEQ/SP EVT_SEM**
> rendezvous on **Sunda (NC-v2)**.
>
> **Source of truth.** All host evidence is byte-exact from
> `libnrt.so.2.31.24.0` (host x86-64, BuildID `8bb57aba…`, KaenaHal-2.31.0.0,
> git `0b044f4ce`). Disassembly is bounded (`objdump --start/--stop`). Device
> CSR semantics are cross-referenced to the SX-/DX- carves and the device-side
> [dual-fetch](../firmware/seq/dual-fetch.md) page.
>
> **ARCH WALL.** `sunda`/`cayman`/`mariana` = NC-v2/v3/v4 are byte-grounded in
> this binary (per-arch KaenaHal `__FILE__`/symbol strings). NC-v5 and any
> `arch_id 36` mapping are **INFERRED** and not asserted here.

---

## 0. Two mechanisms, one bring-up — the executive picture

The Pool-engine bring-up path (`aws_hal_stpb_<arch>_pooling_init`) diverges by
arch in exactly two places, both of which are visible as a **structural
difference in the two `pooling_init` call chains** (§4):

1. **HW-Decode CAM table** (this page, §1–§3). Cayman/Mariana insert a
   `hw_decode_table_init` step between `ucode_eng_init` and `dma_init` that
   programs a per-engine opcode-match **CAM** plus a parallel 128-byte-record
   **PROFILE table** into the device `hw_decode` CSR aperture, then sets the
   `disable_hw_decode` control bit. **Sunda omits the step entirely** — every
   Sunda hook is a no-op stub or an unconditional `__assert_fail`, and the
   binary ships **zero** v2 hw_decode blobs (`v3:16, v4:16, v2:0`).

2. **Q7 run-stall release** (§5–§6). Cayman/Mariana release all 8 Q7 cores with
   a single host CSR write to `q7.release_run_stall` (`+0x3000`), clearing the
   8-bit reset mask `0xFF→0x00`. **Sunda's host Q7 hook is a no-op** (`repz
   ret`); the Q7 cluster is instead released/driven by the **SEQ/SP** via the
   **EVT_SEM** event-semaphore fabric (a `WAIT_GE_AND_DEC` rendezvous at the
   per-TPB `SEMAPHORE_INC` window).

Both divergences share the same root cause: the v3/v4 hardware exposes
**consolidated host-programmable control surfaces** (CAM apertures, a
release byte-mask CSR) that v2 lacks, pushing v2 onto the SEQ/EVT_SEM software
path.

> **GOTCHA — "hw_decode" is overloaded.** The token names **two distinct
> things** that merely co-reside in the device `hw_decode` CSR bundle (base
> `0x4000`):
>
> - **(a) The HOST profiler CAM** — *this page*. An opcode-match-then-count /
>   breakpoint table, programmed from `libnrt` into the device `PROFILE_CAM`
>   aperture.
> - **(b) The O1 "HW-Decode" FETCH front-end** — the HW-FIFO-assisted
>   instruction-fetch FSM inside the NX device firmware (the HIGHER fetch table;
>   see [dual-fetch §6](../firmware/seq/dual-fetch.md)). Selected at runtime by
>   the CSR bit `hw_decode.control.disable_hw_decode` (bundle base `0x4000`,
>   bit0).
>
> The host CAM **(a)** programs the profiler; the CSR bit **(b)** selects fetch
> mode. Sunda has **neither**: its profiler params assert, and its NX core uses
> the SW-fetch FSM ("HW decode mode is not supported on v2 arch"). This is the
> full O1 resolution carried into the runtime layer.

---

## 1. The host dispatch — `hw_decode_init_tables` `@0x225150`

The arch fan-out that wires the embedded `.rodata` blobs into the per-engine
destination globals lives in a single function. `HIGH, OBSERVED`.

```text
0000000000225150 <hw_decode_init_tables>:
  225165:  call   44bca0 <al_hal_tpb_get_arch_type>
  22516a:  cmp    $0x3,%eax  ; je 2254d0          ; -> v3 (Cayman) blob set
  225173:  cmp    $0x4,%eax  ; jne 225400         ; v4 (Mariana) falls through;
                                                  ;   else (arch 2 = Sunda) -> 225400
  22517c:  lea    ...,%rax   # c96a20 <hw_decode_pe_cam>     ; dest global
  225183:  lea    ...,%rdx   # 85cfa0 <v4_hw_decode_pe_cam_bin>   ; v4 blob
  22518a:  mov    %rdx,(%rax)                     ; hw_decode_pe_cam = v4_..._bin
  ... (8 ptr + 8 size assignments: {PE,ACT,POOL,DVE} x {cam,table}) ...
```

```c
/* hw_decode_init_tables @0x225150 — arch fan-out (annotated) */
void hw_decode_init_tables(void *rsp_dbg /* optional debug response ptr */)
{
    int arch = al_hal_tpb_get_arch_type();      /* 0x44bca0 */

    if (arch == 3) {                            /* Cayman (v3) -> 0x2254d0 */
        hw_decode_pe_cam   = v3_hw_decode_pe_cam_bin;    /* + sizes      */
        hw_decode_pool_cam = v3_hw_decode_pool_cam_bin;  /* @0x863c60    */
        /* ... ACT, DVE cam/table, all 16 globals from v3 blobs ...      */
    } else if (arch == 4) {                     /* Mariana (v4) — fallthrough */
        hw_decode_pe_cam   = v4_hw_decode_pe_cam_bin;    /* @0x85cfa0    */
        hw_decode_pool_cam = v4_hw_decode_pool_cam_bin;  /* @0x85ab60    */
        /* ... all 16 globals from v4 blobs ...                          */
    } else {                                    /* arch 2 = Sunda -> 0x225400 */
        /* NO table set: returns {ptr,size} sentinels                    *
         * (xor r8d,r8d; ecx=1; edx=1; eax=0). Nothing to program.       */
    }

    /* Optional file-override (fseek/ftell/fread) taken ONLY when the    *
     * debug response pointer rsp_dbg[0] != NULL.                        */
}
```

**Per-engine destination globals** (libnrt `.bss`, nm-verified span
`0xc969a8..0xc96a20`):

```text
hw_decode_dve_table_size  @0xc969a8   hw_decode_dve_cam_size  @0xc969e8
hw_decode_dve_table       @0xc969b0   hw_decode_dve_cam       @0xc969f0
hw_decode_act_table_size  @0xc969b8   hw_decode_act_cam_size  @0xc969f8
hw_decode_act_table       @0xc969c0   hw_decode_act_cam       @0xc96a00
hw_decode_pool_table_size @0xc969c8   hw_decode_pool_cam_size @0xc96a08
hw_decode_pool_table      @0xc969d0   hw_decode_pool_cam      @0xc96a10
hw_decode_pe_table_size   @0xc969d8   hw_decode_pe_cam_size   @0xc96a18
hw_decode_pe_table        @0xc969e0   hw_decode_pe_cam        @0xc96a20
```

> **NOTE — embedded blob inventory (`nm | rg -c`).**
> `v2_hw_decode_* = 0`, `v3_hw_decode_* = 16`, `v4_hw_decode_* = 16`. The 16 =
> 4 engines `{PE,ACT,POOL,DVE}` × `{cam,table}` × `{blob,size}`. **Sunda truly
> ships no HW-Decode profiler content — there is nothing to program.**

---

## 2. The blob formats — CAM + PROFILE table

The embedded blobs are byte-identical to the SX-IMG-26 device carve, so the
host library is shipping the exact image the device `PROFILE_CAM` block
consumes. All values below are `HIGH, OBSERVED` (read directly from `.rodata`).

### 2a. CAM blob — 64 slots × 16 bytes

```c
/* One 0x400-byte CAM = 64 slots. Each slot is 16 bytes: */
struct hw_decode_cam_slot {
    uint32_t opcode_id;   /* opcode to match                    */
    uint32_t mask;        /* 0xff -> exact-byte match; 0x00 -> wildcard */
    uint32_t enable;      /* 1 -> slot armed                    */
    uint32_t rsvd;        /* 0                                  */
};
```

v3 POOL CAM (`xxd @0x863c60`, `cam_size @0x863c40 = 0x400`):

```text
slot0: {opcode=0x01, mask=0xff, enable=1, rsvd=0}
slot1: {opcode=0x06, mask=0xff, enable=1, rsvd=0}
slot2: {opcode=0x02, mask=0xff, enable=1, rsvd=0}
slot3: {opcode=0x07, mask=0xff, enable=1, rsvd=0}
...
last armed slot = {0,0,1,0}  (wildcard catch-all);  trailing slots = 0
```

### 2b. PROFILE table blob — 64 records × 128 bytes

```c
/* One 0x2000-byte TABLE = 64 records of 128 bytes, parallel-indexed to    *
 * the CAM by slot. Sparse: only the first <= 0x17 bytes of each record    *
 * are meaningful (a breakpoint / profile descriptor).                     */
```

v3 POOL header (`xxd @0x861c40`, `table_size @0x861c20 = 0x2000`):

```text
01 02 00 00 | 10 00 00 26 | 60 61 63 6f | 66 67 6c 6d | 62 00 00 00 | 00 10 00 00
= { 0x00000201, 0x26000010, 'oca`'(0x6f636160), 'mlgf'(0x6d6c6766),
    'b'(0x62), 0x1000, ... }
```

The first word pair is a magic/version field per engine; the record body is the
breakpoint/profile descriptor consumed by the device `PROFILE_CAM` block.

### 2c. Armed-slot counts (`HIGH`, libnrt-read; == SX-IMG-26)

| arch        | PE | ACT | POOL | DVE |
| ----------- | -- | --- | ---- | --- |
| CAYMAN (v3) | 47 | 47  | 47   | 47  |
| MARIANA (v4)| 22 | 25  | **1**| 48  |

> **QUIRK — Mariana POOL is a 1-armed empty placeholder.** `xxd` of the v4 POOL
> CAM `@0x85ab60` shows `slot0 = {0,0,1,0}` (pure wildcard catch-all) and **all
> remaining slots zero**; the table `@0x858b40` is just a size field
> (`0x1900` at word 5). i.e. **Mariana POOL hw_decode profiling is effectively
> disarmed** while PE/ACT/DVE are populated. Whether POOL is later armed at
> runtime via the `hw_decode_init_tables` file-override path (§1) or is
> permanently disarmed is **unconfirmed (LOW)**.

---

## 3. The Pool-engine programmer + the per-engine apertures

### 3a. `get_eng_hw_decode_table_params_<arch>` — SoC base resolver

The per-engine device aperture is resolved by an arch-specific function that
maps an **engine index** to a SoC base, then folds the bit-35 address down to
TPB-local. `HIGH, OBSERVED`.

```c
/* aws_hal_get_eng_hw_decode_table_params_cayman @0x47af40 (annotated) */
void get_eng_hw_decode_table_params_cayman(
        unsigned eng, uint64_t *cam_base, uint64_t *table_base,
        uint64_t *cam_size, uint64_t *table_size)
{
    uint64_t base;
    switch (eng) {
        case 0: base = 0x802670000; break;   /* PE   */
        case 1: base = 0x802470000; break;   /* ACT  */
        case 2: base = 0x802b88000; break;   /* POOL */   /* <-- see CORRECTION */
        case 3: base = 0x803070000; break;   /* DVE  */   /* <-- see CORRECTION */
        default: __assert_fail(/* line 0xd6 */);
    }
    *cam_base    = base;                     /* CAM   @base,       size 0x1000 */
    *cam_size    = 0x1000;
    *table_base  = base + 0x1000;            /* TABLE @base+0x1000, size 0x2000 */
    *table_size  = 0x2000;
    /* bit-35 fold: add 0xfffffff800000000  (== subtract 0x800000000) to map  *
     * SoC -> TPB-local. POOL 0x802b88000 -> 0x2b88000.                        */
    *cam_base   += 0xfffffff800000000;
    *table_base += 0xfffffff800000000;
}
```

`get_eng_hw_decode_table_params_mariana @0x4776e0` has the same shape with the
v4 bases.

> **CORRECTION — engine-index → engine mapping (eng 2 = POOL, eng 3 = DVE).**
> An earlier carve (DX-RT-04 §1.7) labelled the cases *eng 2 = DVE* and *eng 3 =
> POOL*. **The actual callers prove the opposite.** Each
> `<arch>_<eng>_hw_decode_table_init` passes a fixed engine index to
> `get_eng_hw_decode_table_params_cayman @0x47af40`:
>
> | caller (table_init)                  | `%edi` passed | → base resolved |
> | ------------------------------------ | ------------- | --------------- |
> | `cayman_pe_hw_decode_table_init`     | `0` (`xor %edi,%edi`) | `0x802670000` (PE)   |
> | `cayman_act_hw_decode_table_init`    | `1`           | `0x802470000` (ACT)  |
> | `cayman_pooling_hw_decode_table_init`| **`2`**       | **`0x802b88000`** (POOL) |
> | `cayman_dve_hw_decode_table_init`    | **`3`**       | **`0x803070000`** (DVE)  |
>
> So **eng 2 = POOL → `0x802b88000`** and **eng 3 = DVE → `0x803070000`**. The
> *base addresses* in the prior carve were correct (POOL really is
> `0x802b88000`); only the eng-index *labels* for POOL/DVE were swapped. The
> table above is the byte-verified mapping.

> **NOTE — Sunda params are a hard error.**
> `get_eng_hw_decode_table_params_sunda @0x479030` is an **unconditional**
> `__assert_fail` (`PRETTY @0x9f2520 =
> "aws_hal_get_eng_hw_decode_table_params_sunda"`, line `0xa2`, file
> `…/src/sunda/arch/aws_hal_arch_offsets_sunda.c`). The assertion-expression
> string passed in `rdi` (`@0x84e186`) is the 1-char literal **`"0"`** — i.e.
> `assert(0)`, a permanent fail — **not** `"TableEntryIsNonEmptyList(b)"` (that
> string lives at the adjacent `@0x84e189`; the prior carve over-read by 3
> bytes). Calling the Sunda params resolver is a programming error by
> construction.

### 3b. `pooling_hw_decode_table_init` — the actual programmer

```c
/* aws_hal_stpb_cayman_pooling_hw_decode_table_init @0x473650 (annotated) */
int cayman_pooling_hw_decode_table_init(void *tpb, ...)
{
    uint64_t cam_base, cam_size, table_base, table_size;

    /* eng index 2 = POOL (see CORRECTION above) */
    get_eng_hw_decode_table_params_cayman(2, &cam_base, &table_base,
                                          &cam_size, &table_size);   /* 0x47af40 */

    if (cam_size > aperture)                 /* "POOL HW decode CAM table too big!" */
        return err;
    write_padded(cam_base, hw_decode_pool_cam, cam_size,
                 "POOL HW Decode CAM");       /* 0x473eb0 */

    if (table_size > aperture)                /* "POOL HW decode PROFILE table too big!" */
        return err;
    write_padded(table_base, hw_decode_pool_table, table_size,
                 "POOL HW Decode PROFILE");   /* 0x473eb0 */
    /* on failure: "Failed to program profile table for POOL hw decode" */

    get_xt_local_reg_offset(...);             /* 0x47afe0 */

    /* FINAL init action: set the device hw_decode.control bit0.        */
    set_tpb_xt_local_reg_hw_decode_control_disable_hw_decode(...);     /* 0x47b2a0 */
    return 0;
}

/* aws_hal_stpb_sunda_pooling_hw_decode_table_init @0x46e8c0 = no-op stub:
 *   31 c0   xor %eax,%eax
 *   c3      ret                                                       */
```

### 3c. The `disable_hw_decode` writer — RMW bit0 at CSR `0x4000`

```c
/* aws_hal_arch_cayman_set_tpb_xt_local_reg_hw_decode_control_disable_hw_decode
 * @0x47b2a0 — byte-identical mariana @0x477bb0.  (annotated)          */
void cayman_set_disable_hw_decode(void *tpb /* rdi */, int value /* esi */)
{
    uint32_t *csr = (uint32_t *)((char *)tpb + 0x4000);  /* lea 0x4000(%rdi) */
    uint32_t v = al_reg_read32(csr);                     /* 0x2658a0         */
    v &= 0xfffffffe;                                     /* clear bit0       */
    v |= (value != 0);                                   /* setne %sil; or   */
    al_reg_write32(csr, v);                              /* 0x265c50         */
}

/* aws_hal_arch_sunda_set_..._disable_hw_decode @0x4796f0 = no-op:
 *   f3 c3   repz ret                                                  */
```

> **The CSR bundle.** `hw_decode` bundle base `0x4000`: `control@0x4000`,
> `profile_cam_search_vector@0x4028`, `hw_decode_flush_cntr@0x402C` (SX-CSR-01
> §6). The host RMW above touches `control.bit0`, which selects the device
> fetch FSM (the O1 mechanism — [dual-fetch §6](../firmware/seq/dual-fetch.md)).

---

## 4. The two `pooling_init` chains — the structural per-arch evidence

This is the single most decisive artifact: the v3/v4 chain **inserts**
`hw_decode_table_init` between `ucode_eng_init` and `dma_init`, and only the
v3/v4 chain performs a **real** Q7 CSR release. `HIGH, OBSERVED` (call targets
read directly).

```text
CAYMAN  aws_hal_stpb_cayman_pooling_init @0x4737b0:
  4737e3: call 473370  pooling_regs_init
  473805: call 473430  pooling_ucode_seq_init
  473824: call 451080  aws_hal_q7_ucode_eng_init
  473848: call 473650  pooling_hw_decode_table_init    <== INSERTED (v3/v4 only)
  47385f: call 4735d0  pooling_dma_init
  473867: call 473570  pooling_release_seq_run_stall    [NX  +0x00 = 0]
  47386f: call 4735a0  pooling_release_eng_run_stall     [Q7  +0x3000 = 0 -> 0xFF->0x00]

SUNDA   aws_hal_stpb_sunda_pooling_init @0x46e8d0:
  46e903: call 46e5d0  pooling_regs_init
  46e925: call 46e6a0  pooling_ucode_seq_init
  46e944: call 451080  aws_hal_q7_ucode_eng_init
                       (NO hw_decode step)
  46e95b: call 46e840  pooling_dma_init
  46e963: call 46e7e0  pooling_release_seq_run_stall     [NX  +0x04 = 0]
  46e96b: call 46e810  pooling_release_eng_run_stall      -> tail-jmp no-op q7 stub 0x4796e0
```

Note that **`q7_ucode_eng_init @0x451080` is the same function for both arches** —
the only structural differences are the inserted `hw_decode_table_init` and the
divergent release tails.

---

## 5. Q7 run-stall release — the host CSR path (Cayman/Mariana)

### 5a. Reset state (`HIGH`, SX-CSR-01)

```text
q7.release_run_stall   CSR @0x3000 [7:0]   reset = 0xFF   (all 8 Q7 cores stalled)
nx.release_run_stall   (NX SEQ)            reset = 1      (SEQ held)
adjacent: start_ctrl 0x3004; run_state_0..7 0x3008..0x3024;
          intr_ctrl 0x3028; intr_info 0x302C.. (per-core latch)
```

### 5b. The host writers (`HIGH, OBSERVED`)

```c
/* aws_hal_arch_cayman_write_tpb_xt_local_reg_q7_release_run_stall @0x47b290
 *   48 81 c7 00 30 00 00   add $0x3000,%rdi
 *   e9 ..                  jmp al_reg_write32 (0x265c50)
 * mariana @0x477ba0 = byte-identical.  Caller passes value 0:               *
 *   CSR 0x3000 := 0x00  -> 0xFF->0x00, releasing all 8 cores at once.       */

/* NX(SEQ) release on Cayman:
 *   aws_hal_arch_cayman_write_tpb_nx_local_reg_release_run_stall @0x47b280
 *   e9 ..                  jmp al_reg_write32       -> writes NX +0x00       */
```

The `pooling_release_eng_run_stall @0x4735a0` caller loads `%esi = 0` (`xor
%esi,%esi`) before the tail-jmp, so the Q7 release byte-mask is cleared in one
shot.

---

## 6. Q7 run-stall release — the SEQ/EVT_SEM path (Sunda)

### 6a. The host hooks are no-ops (`HIGH, OBSERVED`)

```c
/* aws_hal_arch_sunda_write_tpb_xt_local_reg_q7_release_run_stall @0x4796e0
 *   f3 c3   repz ret      -> NO write to CSR 0x3000 on Sunda.               *
 * The symbol name confirms it is the Q7 hook, deliberately empty.          */

/* Sunda NX(SEQ) release @0x4796a0 writes NX +0x04 (lea 0x4(%rbx) before     *
 * al_reg_write32) — a DISTINCT NX offset from Cayman's +0x00.               *
 * (It also emits an al_hal_log line before the write.)                     */
```

So on Sunda the host releases the **SEQ** run-stall (NX `+0x04 = 0`) but leaves
the **Q7 CSR `0x3000` at `0xFF`** — the cores are never host-released. They come
up under SEQ/SP control via the EVT_SEM fabric.

> **GOTCHA — the Sunda `release_eng` path still *calls* the q7 hook.**
> `sunda_pooling_release_eng_run_stall @0x46e810` loads `%esi = 0` and tail-jmps
> into the q7 hook at `0x4796e0` — but that hook is `repz ret`, so the call is a
> nop. The flow *looks* identical to Cayman at the chain level; the divergence
> is entirely inside the leaf hook.

### 6b. EVT_SEM substrate + address builders (`HIGH`)

```text
per-TPB 1 MiB EVT_SEM unit (SX-ADDR-08); for TPB_0 @ 0x2802700000:
  events  window @0x0000   (256 events)
  sem read @0x1000 / set @0x1400 / inc @0x1800 / dec @0x1C00   (256 sems)
```

```c
/* Host->device address builders (HIGH, OBSERVED).
 * All tail-jmp through aws_hal_stpb_get_axi_offset @0x458e00.          */

/* tdrv_arch_get_sem_inc_addr_sunda @0x30b1a0:  lea 0x2701800(,%rsi,4) */
uint64_t sem_inc_sunda(unsigned sem) { return 0x2701800 + 4*sem; }   /* INC win @0x1800 */
/* tdrv_arch_get_evt_addr_sunda     @0x30b1b0:  lea 0x2700000(,%rsi,4) */
uint64_t evt_sunda    (unsigned sem) { return 0x2700000 + 4*sem; }   /* EVENT  @0x0     */

/* tdrv_arch_get_sem_inc_addr_cayman @0x30c170: (0x2009c0600 + sem) << 2 */
uint64_t sem_inc_cayman(unsigned sem){ return 0x80271800 + 4*sem; }  /* SoC INC window  */
/* tdrv_arch_get_evt_addr_cayman     @0x30c190 */
uint64_t evt_cayman    (unsigned sem){ return 0x80270000 + 4*sem; }
```

> **CORRECTION (carried, byte-confirmed).** The true Sunda sem-INC base is
> `0x2701800` (== the `SEMAPHORE_INC` window `@0x1800`), not the stale
> `0x2705C00` from P-3-65.

### 6c. SP descriptor builders (`HIGH, OBSERVED`)

```text
add_evsem                     @0x2737d0
add_semaphore_wait_ge_and_dec @0x273a20:  op=0x10A0, sub-op byte=0x14 (=20,
                                          WAIT_GE_AND_DEC), value movl=0x1
add_semaphore_wait_ge         @0x273ac0
aws_hal_sp_topsp_evsem_notif_hw_bp_enable_{sunda 0x46c0c0 / cayman 0x471820 /
                                           mariana 0x465700}
```

```c
/* add_semaphore_wait_ge_and_dec @0x273a20 — descriptor layout (annotated) */
struct sp_sem_wait_desc {                       /* on-stack build           */
    uint16_t op;        /* @+0x00 = 0x10A0       (mov $0x10a0,%eax; mov %ax) */
    uint8_t  field5;    /* @+0x20 = 0x05         (movb $0x5)                 */
    uint8_t  subop;     /* @+0x22 = 0x14 (=20 WAIT_GE_AND_DEC) (movb $0x14)  */
    uint8_t  sem_lo;    /* @+0x21,@+0x23 = %dl   (semaphore id bytes)        */
    uint32_t sel;       /* @+0x24 = %ecx                                     */
    uint32_t value;     /* @+0x28 = 0x1          (movl $0x1) — wait threshold*/
    /* remainder zeroed via movups %xmm0 stores                             */
};
```

### 6d. The release/run handshake (`MED, INFERRED` — reconstructed)

The full sequence is reconstructed from the OBSERVED address/descriptor builders
plus DX-SEC-01; only the addresses are byte-observed in `libnrt`, the gating
order lives in the SP/SEQ device program.

```c
/* Sunda Q7 bring-up rendezvous (reconstructed) */
/* 1. Host releases the NX/SEQ run-stall (NX +0x04 = 0). Q7 CSR 0x3000      *
 *    stays 0xFF — host never clears it (§6a).                              */
/* 2. The SEQ/SP program is set up to WAIT_GE_AND_DEC (op 0x10A0, sub20,    *
 *    val=1) on the Q7 sync semaphore at sem_inc_sunda(sem)=0x2701800+4*sem.*/
/* 3. The Q7 library, on boot-ready/function-exit, write32's its           *
 *    tpb_evt_sem inc address (0x2701800 + 4*sem), incrementing the sem.    */
/* 4. The SP's WAIT_GE_AND_DEC unblocks (sem >= 1, then decrements), emits  *
 *    a CTRL_NO 0x10A6 -> NQ (notification queue) -> MSI-X to host.         */
/* 5. The SP thereby gates the Q7 run-state (run_state_0..7 @0x3008..0x3024)*
 *    through this event-semaphore rendezvous + run_state, NOT a single     *
 *    host 0xFF->0x00 CSR-0x3000 broadcast.                                 */
```

> **NOTE — boot sentinel (`MED, INFERRED`).** Prior device-image carves
> (P-3-65/P-3-48) describe the Q7 ucode writing `0x6099CB34` to `DRAM[0]` after
> release, with the host CAS-ing `→0x502B2DA1` to claim the core. These
> constants live in the **device** ucode images, **not** re-observed in
> `libnrt` this pass. The host-side counterpart symbol
> `ucode_lib_core_on_ucode_booted @0xc96ad8` **is** confirmed present.

---

## 7. Consolidated per-arch matrix (`HIGH, OBSERVED` unless noted)

| property                      | SUNDA (v2)            | CAYMAN (v3)         | MARIANA (v4)        |
| ----------------------------- | --------------------- | ------------------- | ------------------- |
| `get_eng…params` fn           | `__assert_fail`       | 4-eng SoC bases     | 4-eng SoC bases     |
| `hw_decode_table_init`        | no-op stub `@0x46e8c0`| REAL `@0x473650`    | REAL `@0x467bd0`    |
| position in `pooling_init`    | absent                | after ucode_eng, before dma | after ucode_eng, before dma |
| hw_decode blobs in libnrt     | 0                     | 16                  | 16                  |
| POOL CAM armed                | (none)                | 47                  | 1 (placeholder)     |
| PE / ACT / DVE armed          | (none)                | 47 / 47 / 47        | 22 / 25 / 48        |
| `disable_hw_decode` writer    | no-op `@0x4796f0`     | RMW bit0 @ `0x4000` | RMW bit0 @ `0x4000` |
| NX(SEQ) release offset        | NX `+0x04`            | NX `+0x00`          | NX `+0x00`          |
| `q7_release_run_stall`        | no-op `@0x4796e0`     | `+0x3000` `0xFF→0`  | `+0x3000` `0xFF→0`  |
| Q7 release driver             | SEQ/SP via EVT_SEM    | host CSR broadcast  | host CSR broadcast  |
| sem_inc base                  | `0x2701800 + 4*sem`   | `0x80271800 + 4*sem`| (mariana variant)   |

---

## 8. Reimplementation checklist

To reproduce v3/v4 Pool-engine bring-up:

1. **Wire the blobs.** Call the `hw_decode_init_tables` equivalent: `arch==3` →
   v3 blobs, `arch==4` → v4 blobs, else (Sunda) → do nothing.
2. **Resolve apertures** per engine via the index→base map (§3a, the
   *corrected* mapping: PE=0, ACT=1, **POOL=2**, **DVE=3**), CAM `@base` size
   `0x1000`, TABLE `@base+0x1000` size `0x2000`, then subtract `0x800000000`
   (bit-35 fold) for TPB-local.
3. **Program the CAM then the PROFILE table** with `write_padded`; bail with the
   "…too big!" diagnostic if `size > aperture`.
4. **Set `hw_decode.control.bit0`** (CSR bundle `0x4000`) as the final init
   action via the RMW in §3c.
5. **Release order:** `release_seq_run_stall` (NX `+0x00 = 0`) → then
   `release_eng_run_stall` (Q7 `+0x3000 = 0`, the `0xFF→0x00` broadcast).
6. **Sunda:** skip steps 1–4 (params assert, blobs absent); release the SEQ at
   NX `+0x04 = 0` and let the SP's `WAIT_GE_AND_DEC` (op `0x10A0`/sub `0x14`)
   rendezvous at `0x2701800 + 4*sem` gate the Q7 cores via `run_state`.

---

## 9. Adversarial self-verification

The top-5 primary claims, each re-checked with a bounded command against
`libnrt.so.2.31.24.0`.

| # | claim | bounded check | result |
| - | ----- | ------------- | ------ |
| 1 | Sunda `pooling_hw_decode_table_init @0x46e8c0` = no-op (`xor eax,eax; ret`); Sunda q7 + disable hooks `@0x4796e0/0x4796f0` = `f3 c3` | `objdump -d --start=0x46e8c0…` / `…0x4796e0…` | **PASS** — `31 c0 c3` and `f3 c3` exactly |
| 2 | Cayman/Mariana q7 release = `add $0x3000,%rdi; jmp al_reg_write32` (byte-identical) | `objdump -d --start=0x47b290 / 0x477ba0` | **PASS** — identical bar the rel32 jmp delta |
| 3 | `disable_hw_decode` writer = RMW bit0 at `lea 0x4000(%rdi)` (read32/and 0xfffffffe/or/write32) | `objdump -d --start=0x47b2a0` | **PASS** — exact RMW; mariana `@0x477bb0` same |
| 4 | Blob counts `v2:0 / v3:16 / v4:16`; v3 POOL CAM size `0x400`, slots `{1,6,2,7}×0xff`; armed v3 POOL=47, v4 POOL=1 | `nm \| rg -c`; `python3` struct read `@0x863c40/0x863c60/0x85ab60` | **PASS** — all exact |
| 5 | Two `pooling_init` chains: v3 inserts `hw_decode_table_init` (call `@0x473848`→`0x473650`); Sunda omits it; only v3 hits real q7 CSR | `objdump -d --start=0x4737b0 / 0x46e8d0 \| rg call` | **PASS** — call targets exact |

**Additional verified during authoring (the CORRECTIONs):**

- **eng-index map.** `cayman_{pe,act,pooling,dve}_hw_decode_table_init` pass
  `%edi = 0,1,2,3` respectively (`objdump` bounded), and
  `get_eng_hw_decode_table_params_cayman @0x47af40` maps `2→0x802b88000` (POOL),
  `3→0x803070000` (DVE). → **eng 2 = POOL, eng 3 = DVE** (prior carve swapped
  the labels). **CONFIRMED.**
- **Sunda assert string.** `__assert_fail` `rdi = 0x84e186` points at the
  1-char literal `"0"` (`assert(0)`), with `"TableEntryIsNonEmptyList(b)"` at
  the adjacent `0x84e189`. `PRETTY @0x9f2520` =
  `"aws_hal_get_eng_hw_decode_table_params_sunda"`, line `0xa2`. **CONFIRMED.**

---

## 10. Open items (`LOW`)

- The exact Q7 `run_state` gating sequence on Sunda (which SP descriptor sets
  which `run_state_0..7` bit) is `INFERRED`; it lives in the SP/SEQ device
  program, not in `libnrt`.
- Boot-sentinel constants `0x6099CB34` / `0x502B2DA1` are from prior
  device-image carves, not re-observed in `libnrt` this pass.
- Whether Mariana POOL profiling is later armed at runtime (file-override path,
  §1) vs permanently disarmed is unconfirmed.

---

## See also

- [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md) — the
  complementary device-side mechanism: the O1 resolution (the HW-Decode FETCH
  FSM vs the Sunda SW-fetch FSM), selected by `hw_decode.control.disable_hw_decode`
  (the CSR bit this page's host RMW writes).
- [The `aws_hal_q7_*` HAL](aws-hal-q7.md) — the host HAL surface that wraps the
  per-arch `pooling_init` chains.
- [Boot / Reset Sequence + Startup Config](../uarch/boot-reset.md) — the
  device-side Q7 reset vector and C-runtime that the host release un-stalls.
- **PROF / CSR pages (Part 13, control/*)** — the device `hw_decode` CSR bundle
  (`control@0x4000`, `profile_cam_search_vector@0x4028`,
  `hw_decode_flush_cntr@0x402C`), the `q7.release_run_stall@0x3000` byte-mask,
  and the EVT_SEM 1 MiB unit (inc window `@0x1800`) — *not yet authored; plain
  text.*
