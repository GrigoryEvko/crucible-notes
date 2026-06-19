# DGE Descriptor-Emit Path

This page reconstructs the **device-side descriptor-emit path of the DGE** — the stage that
runs *after* [`dge_decode_fast`](dge-setup.md) has classified a request into a Reshape Kind and
the [3-backend selector](dge-backend-selector.md) has chosen Pool / RTL / software. Emit is the
step that turns the chosen backend's 4-dim internal shape (`dge_shape[i] = { num[4], step[4] }`)
into the actual **descriptor program** the DMA engine executes. It does that through **three
push micro-ops** — `GENERATE`, `DIMPUSH`, `REGWRITE` — guarded by a **per-descriptor src/dst
bounds check**, and it produces a **64-byte high-level TPB DMA instruction word** that the
backend lowers into **16-byte SDMA BD ring entries**.

Everything here is **byte-pinned to shipped artifacts disassembled / read this session**: the
DEBUG NX-POOL firmware images carved out of `libnrtucode.a`, decoded with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, FLIX/VLIW, `IsaMaxInstructionSize=32`), and the
shipped clean C ISA headers (`neuron_cayman_arch_isa`), compile-checked with `gcc`. The push
ops are recovered from the firmware's **own embedded format strings** (`S:` prefix); the
descriptor structs and the bounds-check predicate from the **shipped ISA-header struct/predicate
definitions** (`ISA_STATIC_ASSERT`ed `sizeof == 64`). Where the backing reading disagrees with
the re-disassembly, **the binary wins**, and an in-place **CORRECTION** says so.

Confidence tags follow the [project model](../../reference/confidence-model.md): `OBSERVED` = a
byte / string / instruction / header read from a shipped artifact this session; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed
with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

> **GOTCHA — emit *bodies* are FLIX-desynced; strings + structs are ground truth.** The push-op
> function bodies load their format strings from a **string-pool base register** (`0x83400`,
> confirmed as an IRAM literal **51×** this session) plus a small displacement, **not** via
> `const16` hi/lo pairs — so `objdump`'s linear sweep loses bundle sync inside the emit bodies
> and the per-instruction body is **not byte-recoverable**. This is documented, not hidden. The
> authoritative artifacts on this page are therefore (a) the **byte-exact format strings**
> (§2), (b) the **compile-verified 64-B descriptor structs** (§3, §5), and (c) the **header
> validity predicates** (§5) — never a desynced opcode read. Every body-level claim is tagged
> `MED` and grounded on those three, exactly the discipline the
> [setup page](dge-setup.md) and the [backend selector](dge-backend-selector.md) established.

---

## 0. The emit pipeline in one diagram

```
  ┌──────────────── DGE descriptor-emit path (NX-POOL sequencer; CAYMAN+) ────────────────┐
  │                                                                                        │
  │   reshape result: dge_shape[src/dst] = { num[4], step[4] } + elem_size + dtype-pair    │
  │        │   (from dge_decode_fast + analyze_tensor_reshape; backend already chosen)      │
  │        ▼                                                                               │
  │   for each target channel DMA[d] (d ∈ MEMCOPY_DMA_CFG.lo bitfield):                    │
  │     (1) [ push REGWRITE to DMA[d] ]      control/trigger reg-write   (CAYMAN/MARIANA;   │
  │                                          *** RETIRED on MARIANA_PLUS ***)               │
  │     (2)   push GENERATE to DMA[d]: %s : addr, elem_size, sem_num                        │
  │                                          → one data-transfer descriptor                │
  │     (3)   push DIMPUSH  to DMA[d]: [num,num][step,step]   × one per tensor dimension    │
  │                                          → the nested multi-D loop-nest                 │
  │        │                                                                               │
  │        ▼   PER-DESCRIPTOR BOUNDS CHECK  (src + dst, register-indirect):                 │
  │            bounds:(%1d addr<=limit, %1d addr<=limit)                                    │
  │              addr > limit ?  → "S: DGE: Failed bounds check. $S[%i]+=%i."               │
  │                               → (unless bc_disable_oob_error_notif) "Dispatched error   │
  │                                  notification"  (→ dge-errors.md)                       │
  │        │                                                                               │
  │        ▼   the assembled 64-B DIRECT2D / INDIRECT1D / GATHER_XPOSE instruction word     │
  │            lowers → 16-B SDMA BD ring entries                                           │
  │            (16-B unit PINNED by MEMCOPY_CARVEOUT_CFG: "byte offset / 16", 64-B aligned) │
  │        │                                                                               │
  │        ▼   M2S/S2M tail-pointer doorbell  → RDM tail-writeback + ringId inject          │
  └────────────────────────────────────────────────────────────────────────────────────────┘
```

One-line verdict: **emit composes `[REGWRITE] + GENERATE + N×DIMPUSH` onto each `DMA[d]`,
bounds-checks src and dst per descriptor, and assembles a 64-B high-level descriptor that lowers
to 16-B SDMA BD ring entries** — then the upstream tail-pointer doorbell (covered on the
[setup page §7](dge-setup.md)) launches the engine.

---

## 1. Carved object + addressing

The emit ops live in the **NX-POOL sequencer firmware** (the `*_NX_POOL` members of
`libnrtucode.a`), **not** the Q7 image — the Q7 leg builds the low-level SDMA BD rings
(`rdma_desc_gen`/`start`, the `P%i:` corpus), while the NX/POOL leg runs the
decode → reshape → backend-select → push lowering. The three `S: push …` strings are present
**only** in the `*_NX_POOL` DEBUG DRAMs and **absent** from the Q7 DRAM. All carves were
re-computed and sha-confirmed this session (`ar p … | objcopy -O binary --only-section=.rodata`):

| Member (DEBUG)                          | Seg  | `.rodata` size      | sha256[:12]    | Role here |
|-----------------------------------------|------|---------------------|----------------|-----------|
| `img_CAYMAN_NX_POOL_DEBUG_DRAM`         | DRAM | 28448 B             | `7bdf6ed7ccd2`  | **emit string corpus** (all three pushes + bounds) |
| `img_CAYMAN_NX_POOL_DEBUG_IRAM`         | IRAM | 116768 B            | `8e4412b99201`  | string-pool-base census (`0x83400` 51×) |
| `img_MARIANA_NX_POOL_DEBUG_DRAM`        | DRAM | 28672 B             | `ec067304e6cf`  | v4 baseline — all three pushes |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_DRAM`   | DRAM | 29024 B             | `d2e1552a13f1`  | **REGWRITE-retired** image (GENERATE+DIMPUSH only) |
| `img_CAYMAN_Q7_POOL_DEBUG_DRAM`         | DRAM | 89344 B             | `226f4254d475`  | descriptor-count guard strings (`ring_ndesc`) |
| `img_SUNDA_NX_POOL_DEBUG_DRAM`          | DRAM | 14432 B             | `298ae996c1a3`  | v2 baseline — **no** emit ops |

**[HIGH / OBSERVED — `stat -c%s` + `sha256sum` this session; all six match the
[setup](dge-setup.md) / [selector](dge-backend-selector.md) carves.]**

**Addressing rule.** The DRAM image loads at device VA `0x80000`, so a **DRAM-string VA =
file-offset + `0x80000`** (every VA below uses this); IRAM file-offset == IRAM device VA.

---

## 2. The emit format-string corpus — ground truth

Read byte-exact from the `CAYMAN_NX_POOL` DEBUG DRAM `.rodata` (`strings -a -t x`, offsets
re-confirmed this session). These strings *encode* the emit ops, their operand sets, and the
bounds check; they are the single most reliable artifact for the emit path.

**The three push ops:**

| file off | VA | String (byte-exact) |
|---|---|---|
| `0x37ea` | `0x837ea` | `S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]` |
| `0x3821` | `0x83821` | `S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i` |
| `0x3870` | `0x83870` | `S: push REGWRITE to DMA[%d]` |

**The bounds-check + error strings:**

| file off | VA | String |
|---|---|---|
| `0x3105` | `0x83105` | `S: DGE: Failed bounds check. $S[%i]+=%i.` |
| `0x318e` | `0x8318e` | `S: DGE: Dispatched error notification` |
| `0x312f` | `0x8312f` | `S: DGE: NO BACKEND FOUND, doing nothing` |

**The per-backend descriptor-dump lines** (each backend dumps the descriptor it built, *with its
embedded `bounds:` field*; the full field grammar is decoded on the
[backend-selector page §2](dge-backend-selector.md) — here we use only the `bounds:` tail):

| file off | VA | Backend | `bounds:` tail |
|---|---|---|---|
| `0x304c` | `0x8304c` | Pool (2-dim) | `… bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx) compute_op: 0x%x` |
| `0x354d` | `0x8354d` | RTL (5+2-dim) | *(no `bounds:` — pure wide transport)* |
| `0x3940` | `0x83940` | software (4-dim) | *(no `bounds:`; carries `indirection_dim`, `reshape_kind`)* |

**[HIGH / OBSERVED — `strings -a -t x` byte offsets + exact text re-confirmed this session; the
`bounds:` field appears only in the Pool dump line.]**

> **NOTE — the `%s` direction tag is `RD` / `WR`, byte-resolved inline.** The GENERATE format
> string ends (NUL) at off `0x3869`; the two NUL-separated literals **`RD` (off `0x386a`)** and
> **`WR` (off `0x386d`)** sit in the gap before the REGWRITE string at `0x3870` (byte-read this
> session). So the `%s` of `push GENERATE … %s : addr=…` resolves to **`RD`** (the M2S /
> read-from-source leg, `addr` = src SoC) or **`WR`** (the S2M / write-to-dest leg, `addr` = dst
> SoC) — the descriptor's transfer direction. The two literals are byte-adjacent in `.rodata`.
> **[HIGH/OBSERVED — `RD`@`0x386a`, `WR`@`0x386d` byte-read inline after the GENERATE string.]**

> **NOTE — string-pool base, why the bodies desync.** The push strings are reached as
> `base(0x83400) + displacement` (GENERATE `0x83821` = `0x83400 + 0x421`; DIMPUSH
> `0x837ea` = `+0x3ea`; REGWRITE `0x83870` = `+0x470`), where the IRAM literal `0x00083400`
> appears **51×** and the `dge_shape`-dump pool literal `0x00082400` appears **102×**
> (`python3 … d.count(…)` this session, metric: 4-byte little-endian literal count over
> `caynx_iram.bin`). Because the loads come off a base-pointer register and not a `const16`
> hi/lo immediate pair, they are **not `const16`-visible** and the emit bodies render as `.byte`
> — the FLIX-desync ceiling from §0. The *clean*-decoding DGE strings (`Select backend Pool`
> `0x3157`, `NO BACKEND` `0x312f`) *are* `const16`-loaded by contrast. **[HIGH/OBSERVED.]**

---

## 3. The descriptor the emit produces — the 64-B high-level word

The high-level descriptor `GENERATE` emits is **one of three 64-byte ISA structs**, selected by
the Reshape Kind: `DIRECT2D` (Pool / 2-dim), `INDIRECT1D` (indexed copy), `GATHER_XPOSE`
(gather-transpose). All three carry `ISA_STATIC_ASSERT(sizeof == 64)` and were re-read **and
`gcc`-compiled** from the shipped `neuron_cayman_arch_isa/tpb` headers this session (the compile
emitted `DIRECT2D/INDIRECT1D/GATHER_XPOSE sizeof = 64`). The full `DIRECT2D` field table + the
`compute_op` enum live on the [backend-selector page §2](dge-backend-selector.md); this page
reproduces only the **fields the push ops fill** and the **bound-reg offsets** the §5 check
references, so the 64-B↔16-B mapping (§6) is self-contained.

**Where each struct keeps its two `BOUND_CHECK_REG`s** (compile-verified offsets this session):

| 64-B struct | bound regs (offset) | what they gate | the addr each guards |
|---|---|---|---|
| `NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT` | `src_bound_reg` `+38`, `dst_bound_reg` `+39` | the **data** src/dst | `src_start_addr` `+16`, `dst_start_addr` `+40` |
| `NEURON_ISA_TPB_DMA_INDIRECT1D_STRUCT` | `src_idx_bound_reg` `+58`, `dst_idx_bound_reg` `+59` | the **index arrays** (addressing is index-driven) | `src_idx_start_addr` `+48`, `dst_idx_start_addr` `+52` |
| `NEURON_ISA_TPB_DMA_GATHER_XPOSE_STRUCT` | `src_idx_bound_reg` `+15`, `dst_bound_reg` `+51` | the gather **index** + the **data dst** | `src_idx_start_addr` `+36`, `dst_start_addr` `+40` |

**[HIGH / OBSERVED — every offset measured (header read + `gcc offsetof`) this session; the
`DIRECT2D` `ISA_STATIC_ASSERT(sizeof == 64)` is at `aws_neuron_isa_tpb_dma_direct2d.h:45`,
`INDIRECT1D` at `…dma_indirect1d.h:23`, `GATHER_XPOSE` at `…dma_gather_xpose.h:40/61`.]**

> **GOTCHA — `INDIRECT1D` and `GATHER_XPOSE` bound the *index* stream, not the data.** Re-read
> from the headers this session: `INDIRECT1D` carries **`src_idx_bound_reg` / `dst_idx_bound_reg`**
> (the index-array bounds), and `GATHER_XPOSE` carries **`src_idx_bound_reg`** (index) plus a
> data-side **`dst_bound_reg`**. A reimplementer must wire the bound register to the right
> address per Kind: for an indexed copy the descriptor's effective address is *index-driven*, so
> the bound that matters is on the **index** read, not the data base. **[HIGH/OBSERVED.]**

---

## 4. The three emit micro-ops

The three pushes are the entire emit instruction set. Reconstructed below as annotated C
pseudocode naming the real strings / struct fields / addresses; the per-instruction body is not
byte-recoverable (§0), so the pseudocode is grounded on the **format strings** + the
**descriptor structs** + the §6 lowering, **not** a desynced opcode read.

### 4.1 `GENERATE` — emit one data-transfer descriptor

```text
S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i
```

`GENERATE` pushes **one** data-transfer descriptor onto channel `DMA[d]`. Its four operands map
1:1 onto the 64-B struct fields and, via §6, onto the 16-B BD:

```c
/* dge emit (NX-POOL); strings byte-exact, body FLIX-desynced (§0). */
/* DMA[d] : target DMA channel index (d ∈ MEMCOPY_DMA_CFG.lo bitfield, §6).            */
/* dir    : "RD" (M2S/read, addr = src SoC) | "WR" (S2M/write, addr = dst SoC).        */

void push_GENERATE(int d, const char *dir, uint64_t addr,
                   uint16_t elem_size, uint8_t sem_num)
{
    LOG("S: push GENERATE to DMA[%d]: %s : addr=0x%llx, elem_size=%d, sem_num=%i",
        d, dir, addr, elem_size, sem_num);

    /* fills the 64-B high-level descriptor word (one DIRECT2D/INDIRECT1D/GATHER_XPOSE): */
    desc.start_addr = addr;       /* DIRECT2D src_start_addr(+16) / dst_start_addr(+40) */
                                  /*   ADDR8: immediate | addr-from-table | shape-reg   */
    desc.elem_size  = elem_size;  /* src_elem_size(+36) / dst_elem_size(+60), u16       */
    desc.semaphore  = sem_num;    /* DIRECT2D semaphore(+13); fired on dma-complete     */
    /* -> lowers to the 16-B BD buf_ptr (addr) + the BD completion sem (§6)              */
}
```

| operand | meaning | 64-B field | 16-B BD (§6) | tag |
|---|---|---|---|---|
| `%d` (`DMA[d]`) | target DMA channel index within the engine's allotted DMAs | — (channel, not in-word) | selects which ring | `[HIGH/OBSERVED]` |
| `%s` (`RD`/`WR`) | transfer direction: read-from-src (M2S) vs write-to-dst (S2M) | direction of the BD | M2S vs S2M ring | `[HIGH/OBSERVED]` |
| `addr=0x%llx` | 64-bit SoC address read/written | `src_start_addr`/`dst_start_addr` (ADDR8) | `buf_ptr` | `[addr field HIGH/OBSERVED; BD-slot map HIGH/INFERRED]` |
| `elem_size=%d` | per-element byte size | `src_elem_size`/`dst_elem_size` (u16) | (× DIMPUSH counts) → BD length | `[HIGH/OBSERVED]` |
| `sem_num=%i` | completion-semaphore index | `semaphore`(+13) + `sem_increment`(+14) | BD completion sem | `[HIGH/OBSERVED]` |

The `@dmacomplete` token in the backend dump line (`$S[%i]+=%i@dmacomplete`) is this `sem_num`:
on DMA-complete the engine bumps shape register `$S[i]` by the descriptor's increment, releasing
the consumer. **[HIGH/OBSERVED string; the `addr`/`elem_size`/`sem_num`→BD-slot map HIGH/INFERRED
from the §3 struct + the §6 lowering.]**

### 4.2 `DIMPUSH` — push one loop/dimension level

```text
S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]
```

`DIMPUSH` pushes **one nesting level** of the multi-D DMA iteration. The format is a *pair of
pairs*: a `[num,num]` **u16** count pair and a `[step,step]` **i32 SIGNED** stride pair —
exactly the `dge_shape` `num[]` / `step[]` for this dimension, one entry per side (src + dst):

```c
/* one DIMPUSH per tensor dimension; [u16 counts][i32 SIGNED strides].                 */
void push_DIMPUSH(int d, uint16_t num_src, uint16_t num_dst,
                  int32_t step_src, int32_t step_dst)
{
    LOG("S: push DIMPUSH to DMA[%d]: [%u,%u][%d,%d]",
        d, num_src, num_dst, step_src, step_dst);

    /* accumulates into the descriptor's nested loop-nest:                              */
    desc.src_num_elem[k]  = num_src;   desc.dst_num_elem[k]  = num_dst;   /* u16        */
    desc.src_step_elem[k] = step_src;  desc.dst_step_elem[k] = step_dst;  /* i32 SIGNED */
    /* k advances per DIMPUSH; #DIMPUSH == the backend's descriptor dim count           */
}
```

The `%u` are u16 (matching the `0x%04x`-printed `dge_shape` num fields); the `%d` are **signed**
i32 (the struct's `int32_t … step_elem[2]` — header-verbatim). **The signedness is the point**:
a negative stride is a reverse-axis walk, which is exactly how a transpose realizes an axis
permutation by reordering `step_elem[]` rather than touching data. **[HIGH/OBSERVED — the
`[%u,%u]`/`[%d,%d]` widths + the `int32_t step_elem[]` header read.]**

How `DIMPUSH` builds the N-D iteration: the emit walks the reshape's `num_elem[4]` / `step_elem[4]`
and issues **one `DIMPUSH` per dimension**; the pushed `(num,step)` pairs accumulate into the
descriptor's nested loop-nest, which the engine then expands into the BD stream (the inner two
dims fold into one 2-D strided BD via `src/dst_step_elem[2]` + `num_elem[2]`). This is why a
`DIRECT2D` descriptor carries **2** dims, the software backend **4**, and the RTL backend **5+2**
(the three [backend dump lines](dge-backend-selector.md)): **`#DIMPUSH == the descriptor's dim
count for the chosen backend.** **[`DIMPUSH` op + the num/step arrays HIGH/OBSERVED; "one DIMPUSH
per dim feeding the loop-nest → BD walk" HIGH/INFERRED; the exact inner-fold rule MED.]**

> **QUIRK — `DIMPUSH` is a *pair of pairs*, advancing src and dst cursors in lock-step.**
> `[%u,%u][%d,%d]` is **not** a single `(num,step)`. The two entries in each bracket are the
> **src** and **dst** legs of *this* dimension: `DIMPUSH` advances both cursors for the
> dimension together, matching the descriptor's parallel `src_*` / `dst_*` `step`/`num` arrays.
> A reimplementer who reads it as one `(count,stride)` will mis-walk the dst. **[`[%u,%u][%d,%d]`
> shape HIGH/OBSERVED; the src/dst pairing reading MED — inferred from the two-pair log shape +
> the symmetric src/dst struct arrays.]**

### 4.3 `REGWRITE` — push an inline control-register write

```text
S: push REGWRITE to DMA[%d]
```

`REGWRITE` is the leanest op (no operand args). It pushes a **register-write entry** into the
descriptor stream of `DMA[d]` — an inline "program a control register" descriptor, distinct from
a data-transfer BD. In the descriptor stream this is the control/trigger programming (e.g.
writing the SDMA broadcast/trigger base before the data BDs), so a `REGWRITE` lowers onto a
**control-op** BD word (vs the COPY-op word a `GENERATE` data BD produces).

```c
void push_REGWRITE(int d /*, reg, value (FLIX-desynced) */)
{
    LOG("S: push REGWRITE to DMA[%d]", d);
    /* inline control-register-write descriptor: a control-op BD, not a data COPY BD     */
}
```

**[op existence + "control/trigger" role HIGH/OBSERVED (string + the §6/§7 stream model); the
exact control-op BD-word encoding INFERRED-MED — the REGWRITE body is FLIX-desynced and the
16-B BD COPY/ctrl optype is a forward Part-9 fact (§6).]**

> **QUIRK — `REGWRITE` is RETIRED on MARIANA_PLUS.** Re-grepped this session (metric:
> `rg -c -a` pattern-occurrence over the carved DRAM `.rodata`):
>
> | gen (DRAM) | `push GENERATE` | `push DIMPUSH` | `push REGWRITE` |
> |---|---|---|---|
> | SUNDA (v2) | 0 | 0 | 0 |
> | CAYMAN (v3) | 1 | 1 | **1** |
> | MARIANA (v4) | 1 | 1 | **1** |
> | MARIANA_PLUS (v4+) | 1 | 1 | **0** ← retired |
>
> CAYMAN/MARIANA carry **all three** ops; **MARIANA_PLUS carries `GENERATE` + `DIMPUSH` but
> `push REGWRITE` is gone** (count 0), while SUNDA carries **none** of the three (no DGE emit on
> v2 at all). The v4+ streamlined fast-path folds the trigger/register programming into the
> GENERATE/DIMPUSH emit, so a separate `REGWRITE` descriptor is no longer pushed; the other two
> ops + the bounds check + setup survive (counts 1 / 1 / bounds-check 1 / Setting-up 1 on
> MARIANA_PLUS, re-confirmed). **[per-gen counts HIGH/OBSERVED — `rg -c -a` this session;
> "folded into the fast path" reading INFERRED-HIGH.]**

---

## 5. The per-descriptor bounds check — src/dst validity gate

Every emitted descriptor carries **two** `NEURON_ISA_TPB_BOUND_CHECK_REG`s (one src, one dst;
§3). The bound register is a **1-byte bitfield** (compile-verified `sizeof == 1`):

```c
/* aws_neuron_isa_tpb_common.h:707 — read verbatim this session */
typedef struct NEURON_ISA_TPB_BOUND_CHECK_REG {
    NEURON_ISA_TPB_REG_NUM bc_reg : 6;                     // bound-register number;
                                                           //   if WIDE offset, bc_reg+1 holds
                                                           //   the HIGH 32 bits of the bound
    uint8_t                bc_disable_oob_error_notif : 1; // suppress the OOB error notification
    uint8_t                bc_enabled : 1;                 // 1 = perform the bounds check
} NEURON_ISA_PACKED NEURON_ISA_TPB_BOUND_CHECK_REG;        // sizeof == 1
```

`bc_reg` (6 bits) is a **hardware bound-register number** holding the buffer's upper LIMIT; the
check compares the descriptor's effective address against that register's value. In wide-offset
mode `bc_reg+1` supplies the high 32 bits (a 64-bit limit across a register pair).
**[HIGH/OBSERVED — header read + `sizeof == 1` this session.]**

### 5.1 The compile-time validity predicate (header, verbatim)

The shipped header carries the validity predicates as **commented spec-language pseudocode**
(Rust-like `fn`, not compiled C) — read verbatim this session:

```c
/* aws_neuron_isa_tpb_common.h:1370 */
// fn has_valid_bound_check_reg(bound_check_reg: BoundCheckReg, marker: u8) -> bool {
//       (   (bound_check_reg.bc_enabled == 0)
//        && (bound_check_reg.bc_reg == 0)
//        && (bound_check_reg.bc_disable_oob_error_notif == 0))      // fully-disabled, all-zero
//    || (   (bound_check_reg.bc_enabled == 1)
//        && is_valid_register_read_with_marker(bound_check_reg.bc_reg, marker)) // enabled+valid
// }
```

i.e. the bound-reg is valid **either** fully disabled (all three fields zero) **or** enabled and
referencing a valid register read for the address's table marker. The address-side companion is
`is_valid_dge_shape_reg(start_addr, reg_num)` (`…common.h:2075`): when the address is
shape-from-register, its shape-reg index must be `< NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS`
(`= 4U`, `aws_neuron_isa_tpb_common.h:34`, the 4-deep `$S[i]` file). **[HIGH/OBSERVED — both
predicates + the `= 4U` constant read this session.]**

> **GOTCHA — the index-bound variant adds a flags gate.** For the index-driven descriptors
> (`INDIRECT1D`/`GATHER_XPOSE`), the header also defines `has_valid_idx_bound_check_reg(bcr,
> flags)` = `has_valid_idx_bound_check_reg_no_flags(bcr) && (flags.idx_bound_is_err == 1 ||
> bcr.bc_disable_oob_error_notif == 0)` (read verbatim, `…common.h:1382`). So the *index* bound
> couples to a `DmaIndirectFlags.idx_bound_is_err` bit: a reimplementer cannot reuse the plain
> data-bound validity for the index stream. **[HIGH/OBSERVED — header read this session; sharper
> than the backing report, which named only the data-side predicate.]**

### 5.2 The runtime check (backend dump line, byte-exact)

The Pool-backend descriptor dump (`0x8304c`, §2) embeds the runtime bounds result, byte-exact:

```text
bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx)
```

This is **two** `{bc_enabled-flag(%1d), effective_addr(0x%llx) <= bound_limit(0x%llx)}` pairs —
the **first pair is the SRC** check, the **second the DST**. The DGE evaluates `addr <= limit`
per direction before issuing the descriptor; the `%1d` is the `bc_enabled` flag (`0` ⇒ the check
is skipped). **[HIGH/OBSERVED — the `bounds:` grammar byte-exact; src-then-dst order INFERRED
from the `src=…`-then-`dst=…` ordering of the same dump line.]**

### 5.3 The overflow path (the trigger the errors page consumes)

On `addr > limit` the DGE logs **`S: DGE: Failed bounds check. $S[%i]+=%i.`** (`0x83105`) —
naming the **shape register `$S[i]`** (one of the 4) and the **`+offset`** that pushed its
iteration cursor past the bound. Unless `bc_disable_oob_error_notif` is set, the DGE then logs
**`S: DGE: Dispatched error notification`** (`0x8318e`). The notification *delivery* — the packet/
RDM-completion side, gated by `NEURON_FEATURE_FLAGS` bit 1 = **"DGE packet notification - Sets if
packet notifications are enabled"** (local-reg **38**,
`aws_neuron_isa_xt_general_local_reg_defines.h` verbatim this session) — is owned by the
[errors page](dge-errors.md). **[bounds-fail + dispatch strings + the feature-flag bit
HIGH/OBSERVED; the delivery mechanism is the errors-page boundary, noted.]**

### 5.4 Three orthogonal bound layers

The DGE actually enforces **three** distinct bounds, only the first of which is the per-desc
address gate:

1. **Per-descriptor ADDRESS** (§5.1–5.3): `src`/`dst` `addr <= limit`, register-indirect.
2. **Total-BYTE equality** (header pseudocode, verbatim `aws_neuron_isa_tpb_common.h:2123`):
   `is_valid_src_dst_element_count_without_cce` requires
   `(src_num_elem * src_elem_size) == (dst_num_elem * dst_elem_size)` — src total bytes == dst
   total bytes.
3. **Descriptor-COUNT vs ring** (Q7 side): the `DescriptorStream` guard
   `P%i: Generating %i descriptors; ring_ndesc = %i` (`0x84140`) and the hard error
   `P%i: ERROR: DescriptorStream wrote %d descriptors, expected %d` (`0x85040`) bound the emitted
   descriptor count against the ring size — and `MEMCOPY_CARVEOUT_CFG` "limits the maximum number
   of descriptors in a single memcopy" (§6).

**[all three predicates / strings HIGH/OBSERVED this session.]**

---

## 6. The descriptor-format reconciliation — 64-B high-level ↔ 16-B SDMA BD

The DGE emits a **64-byte high-level TPB DMA instruction word** (one `DIRECT2D` / `INDIRECT1D` /
`GATHER_XPOSE`, all `ISA_STATIC_ASSERT`ed + `gcc`-checked 64 B, §3). The backend then **expands**
it into **16-byte SDMA BD ring entries** — one or more per partition / per length-chunk. The
push ops supply the high-level fields; the lowering does the fan-out:

| 64-B high-level field (from push op) | lowers to 16-B SDMA BD | via |
|---|---|---|
| `GENERATE.addr` → `src/dst_start_addr` (ADDR8) | BD **`buf_ptr`** (the SoC base of the read/write) | direction (`RD`→src, `WR`→dst) |
| `GENERATE.elem_size` × `DIMPUSH` `num[]` | BD **length** (transfer byte count) | `elem_size × Π num[]`, chunked if it exceeds the BD length field |
| `DIMPUSH` `step[]` / `num[]` (the loop-nest) | the **strided walk** across BDs | one inner-loop iteration → one strided BD (the 2-D inner dims fold into one BD) |
| `GENERATE.sem_num` → `semaphore`(+13) | BD **completion sem** | fired on dma-complete (`@dmacomplete`) |
| `REGWRITE` (control/trigger) | a BD **control-op word** (not a COPY-op data BD) | inline reg-write entry |

> **CORRECTION — the 16-B BD struct is *not* a header artifact in this distribution.** The
> backing report cited `SDMA_CME_BD_DESC` as the 16-byte BD struct and implied it is
> compile-verifiable here. Re-checking this session (`rg --no-ignore` across the whole
> `…/c10/include` tree), **no `SDMA_CME_BD_DESC` C struct exists in this `0.21.2.0`
> distribution** — the identifier appears only in carried prior-analysis notes, and the
> `etl/buffer_descriptors.h` hit is an unrelated template library. So the 16-byte BD *layout*
> (`buf_ptr` @ +8, the u16 length_meta with 64-KiB chunking, the RD=M2S/WR=S2M binding) is
> **CARRIED cross-analysis, not OBSERVED in these headers**. What *is* OBSERVED here is the
> **16-byte descriptor unit itself** (pinned in §6.1 below) and the 64-B high-level structs (§3).
> The per-field MAPPING is `[HIGH/INFERRED]` (the consistent reading of the §3 struct + the §2
> push strings); the bit-exact 16-B word0/word1 packing is a **forward Part-9 fact**, see the
> NOTE below. **[16-B unit HIGH/OBSERVED; the BD field layout CARRIED; the mapping HIGH/INFERRED.]**

### 6.1 What PINS the 16-byte unit

The 16-byte descriptor granularity is not assumed — it is **fixed by `MEMCOPY_CARVEOUT_CFG`**
(local-reg **39**, `aws_neuron_isa_xt_general_local_reg_defines.h`, verbatim this session):

```text
// DGE Pool carveout configuration (only usable on Pool).
// - lower 16 bits are the carveout region's offset in each partition *in number of descriptors*
//   (that is, the byte offset / 16)
//   - The carveout must be aligned to 64 bytes, a DMA restriction
// - upper 16 bits are the carveout region size in number of descriptors.
//   - The size of the carveout region (...) limits the maximum number of descriptors in a single memcopy
#define AWS_NEURON_ISA_XT_MEMCOPY_CARVEOUT_CFG  39
```

The carveout offset is **"byte offset / 16" in number of descriptors** — i.e. **one descriptor =
16 bytes**, and the region must be **64-byte aligned (a DMA restriction)**. The companion
`MEMCOPY_DMA_CFG` (local-reg **40**) is `{ lo16 = which DMAs this engine may use, hi16 = which
queues }` — the `lo16` bitfield is the **`DMA[d]` index space** the push ops target. So the
descriptor program emitted by `GENERATE`/`DIMPUSH`/`REGWRITE` lands in the Pool carveout window
as a sequence of 16-byte BDs, and the per-memcopy descriptor count is hard-capped by the
carveout size. **[HIGH/OBSERVED — both local-reg defines + the "byte offset / 16" comment read
verbatim this session.]**

> **NOTE — the exact 16-B BD bitfield packing is a forward Part-9 fact.** The 64-B high-level
> field → 16-B BD word0/word1 bit packing (the COPY-op vs control-op `optype`, the `length_meta`
> width, the gen-tag) is decoded in
> [`../../dma/dge-microop-encoding.md`](../../dma/dge-microop-encoding.md) and the per-queue ring
> field layout in
> [`../../dma/descriptor-ring-field-tables.md`](../../dma/descriptor-ring-field-tables.md). Both
> are **Part 9 forward links — not yet authored.** This page establishes the *mapping* (which
> high-level field feeds which BD slot) and the *16-byte unit* (pinned above); the bit-exact BD
> encoding is deferred to those pages. **[mapping HIGH/INFERRED; bit-exact encoding = forward.]**

---

## 7. Descriptor-stream assembly — how the three ops compose

The emitted descriptor **program** (the BD stream the DMA engine executes) is assembled per
channel `DMA[d]` from the three ops, in the order the backend issues them:

```
  per channel DMA[d] (d ∈ MEMCOPY_DMA_CFG.lo16 bitfield):
    (1) [ push REGWRITE to DMA[d] ]   program control/trigger regs   [CAYMAN/MARIANA only]
    (2)   push GENERATE to DMA[d]     the data-transfer descriptor (addr + elem_size + sem)
    (3)   push DIMPUSH  to DMA[d] × N  the nested loop bounds + strides (num/step), one per dim
    --- bounds check src + dst (§5) ---
    --- the assembled 64-B descriptor lowers to 16-B BDs in the carveout (§6) ---
    (4)   M2S/S2M tail-pointer doorbell  (covered on dge-setup.md §7)
```

- **(1)–(3)** compose the descriptor. On CAYMAN/MARIANA an optional `REGWRITE` leads, then one
  `GENERATE`, then `N` `DIMPUSH` (one per tensor dimension, `N` = the backend's dim count:
  Pool 2, software 4, RTL 5+2). On MARIANA_PLUS the `REGWRITE` is gone (§4.3).
- The descriptors **land in the DGE Pool carveout** of `DGE_MEMORY` (the 1 GiB descriptor-RAM
  region; the per-partition carveout is the per-memcopy window into it), as 16-byte BDs (§6).
- **(4)** Once the stream is assembled, the TPB engine **bumps the M2S(TX)/S2M(RX) tail-pointer-
  increment CSR** by the descriptor count — a single `s32i.n a3,a4,0` store, decoded clean on the
  [setup page §7](dge-setup.md) `@IRAM 0x17388` — handing the producer's stream to the RDM, which
  write-backs the tail and injects the 2-bit `ringId`.

**[the (1)–(4) composition HIGH/OBSERVED (string presence + carveout/DMA-cfg regs + the §7
doorbell); the *exact* issue order of (1)–(3) is MED — the bodies are FLIX-desynced, the order is
the consistent reading of the op set + the [setup](dge-setup.md) / [selector](dge-backend-selector.md)
flow.]**

> **NOTE — emit (NX) ↔ `rdma_desc_gen` (Q7) are two legs of one model.** For the local memcopy
> leg, the NX `push` ops fill the Pool carveout + bump the tail-pointer here. For the SB2SB /
> remote leg, the *same* composition is what the Q7 `rdma_desc_gen` builds and `rdma_desc_start`
> launches (per-engine CME BD ring + local/remote sema BDs). The correspondences:
> `GENERATE.sem_num` ↔ `rdma_desc_gen`'s local/remote sem; `DIMPUSH` `num`/`step` ↔ the
> partition/free-dim walk; `REGWRITE` ↔ the trigger program. The `tensor_reshape_transpose_sb2sb`
> Kind (MARIANA_PLUS) routes the emitted descriptors to the SB2SB transport instead of the local
> ring. **[HIGH cross-ref; the exact NX↔Q7 edge MED via the FLIX-desync.]**

---

## 8. The operand source — what the push ops consume

The push ops consume, per descriptor, the **64-B descriptor struct fields** (§3): `start_addr`
(ADDR8, src/dst), `step_elem[N]` (i32 signed), `num_elem[N]` (u16), `elem_size` (u16),
`bound_reg × 2` (§5), `semaphore` + `sem_increment`, `compute_op`, `in/out_dtype`,
`dma_configs.priority_class`. The DGE's internal working state that fills these is
`dge_shape[i] = { num[4], step[4] }` + `elem_size` + the `is_src` flag (from
[decode](dge-setup.md) + [reshape](dge-reshape.md)), backed by the **4-deep `$S[i]` shape-register
file** (`NEURON_ISA_TPB_NUM_DGE_SHAPE_REGISTERS = 4U`). The descriptor **Kind**
(`DIRECT2D`/`INDIRECT1D`/`GATHER_XPOSE`) selects which struct layout the push fills and **how many
`DIMPUSH` dims** it issues. The reshape driver linkage:

```
  analyze_tensor_reshape → Reshape Kind + #DMA + partition split → post-transpose num[4]/step[4]
        │  (dge-reshape.md)
        ▼  backend select: Pool | RTL | software  (dge-backend-selector.md)
  [REGWRITE?] + GENERATE(addr,elem_size,sem) + DIMPUSH × #dims ([num,num][step,step] per dim)
        │  onto DMA[0 .. #DMA-1]
        ▼  per-descriptor bounds check src+dst (§5)
  64-B descriptor → 16-B BDs in the DGE_MEMORY carveout (§6)
        │
        ▼  M2S/S2M tail-pointer doorbell → DMA engine executes → RDM tail write-back
```

**[the 64-B struct fields HIGH/OBSERVED; the `dge_shape` → struct fill HIGH/INFERRED (the working
state is OBSERVED on the [setup](dge-setup.md) / [reshape](dge-reshape.md) pages); the
reshape→push per-dim edge HIGH/INFERRED — both the reshape output (num/step) and the push ops are
OBSERVED, the "reshape feeds the push per dim" mapping is the consistent reading, MED on the exact
issue order.]**

---

## 9. Cross-references

- [DGE Setup + Context Init](dge-setup.md) — the upstream `dge_decode_fast` 3-KIND decode, the
  `dge_shape = { num[4], step[4] }` IR this page emits, and the **tail-pointer doorbell §7** that
  launches the assembled stream (Part 5, #683 — authored).
- [DGE 3-Backend Selector](dge-backend-selector.md) — the Pool/RTL/software pick that precedes
  emit, the full `DIRECT2D` 64-B field table + `compute_op` enum + the three backend dump lines
  whose `bounds:` tail §5.2 reads, and the RDM 24-queue ring schema (Part 5, #684 — authored).
- [DGE Reshape Engine](dge-reshape.md) — `analyze_tensor_reshape` / `tensor_reshape_transpose`:
  the post-transpose `num[4]`/`step[4]` the `DIMPUSH` ops consume (Part 5, #685).
- [DGE Error Notifications](dge-errors.md) — the consumer of §5.3: the OOB-error notification
  *delivery* + RDM completion that `Failed bounds check` / `Dispatched error notification` feed,
  gated by `NEURON_FEATURE_FLAGS` bit 1 (Part 5, #687 — **concurrently authored; currently a
  stub. NOTE: link forward.**).
- [`../../dma/dge-microop-encoding.md`](../../dma/dge-microop-encoding.md) — the bit-exact 16-B
  SDMA BD word0/word1 packing the GENERATE/DIMPUSH/REGWRITE pushes lower into (Part 9, #840 —
  **forward link, not yet authored. NOTE.**).
- [`../../dma/descriptor-ring-field-tables.md`](../../dma/descriptor-ring-field-tables.md) — the
  per-queue descriptor-ring field tables (`buf_ptr`, length, ringId) the 16-B BDs populate
  (Part 9, #842 — **forward link, not yet authored. NOTE.**).
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the normative
  `[CONF/PROV]` tag definitions and the FLIX-desync MED ceiling cited throughout.

---

## 10. Reproduction

```sh
export XTENSA_SYSTEM=.../gpsimd_tools_tgz/tools/XtensaTools/config
export XTENSA_CORE=ncore2gp
AR=.../custom_op/c10/lib/libnrtucode.a
INC=.../custom_op/c10/include/neuron_cayman_arch_isa

# carve the NX-POOL DRAMs (VA = off + 0x80000):
ar p $AR img_CAYMAN_NX_POOL_DEBUG_DRAM_contents.c.o > t.o
objcopy -O binary --only-section=.rodata t.o caynx_dram.bin   # 28448 / 7bdf6ed7ccd2
#   (also MARIANA_NX ec067304, MARIANA_PLUS_NX d2e1552a, CAYMAN_Q7 226f4254, SUNDA_NX 298ae996)

# the three push strings + bounds (offset of the "S:" prefix):
strings -a -t x caynx_dram.bin | grep -E 'push (GENERATE|DIMPUSH|REGWRITE)|Failed bounds check'
#   DIMPUSH 0x37ea, GENERATE 0x3821, REGWRITE 0x3870, bounds-fail 0x3105
strings -a -t x -n 2 caynx_dram.bin | grep -E '^[0-9a-f]+ (RD|WR)$'   # RD 0x386a, WR 0x386d

# REGWRITE retirement (metric: rg -c -a pattern-occurrence over DRAM .rodata):
for f in sunda caynx marnx mpnx; do rg -c -a 'push REGWRITE' ${f}_dram.bin; done
#   sunda 0, caynx 1, marnx 1, mpnx 0   <- retired on MARIANA_PLUS

# string-pool base census (caynx_iram.bin = the IRAM member):
python3 -c "d=open('caynx_iram.bin','rb').read(); \
  print('0x83400', d.count((0x83400).to_bytes(4,'little')), \
        '0x82400', d.count((0x82400).to_bytes(4,'little')))"   # 51, 102

# struct + bound-reg + predicates (header read; gcc sizeof/offsetof check):
rg -n 'NEURON_ISA_TPB_BOUND_CHECK_REG' $INC/tpb/aws_neuron_isa_tpb_common.h          # :707 struct
rg -n 'has_valid_bound_check_reg' $INC/tpb/aws_neuron_isa_tpb_common.h               # :1370 predicate
rg -n 'is_valid_dge_shape_reg' $INC/tpb/aws_neuron_isa_tpb_common.h                  # :2075
rg -n 'is_valid_src_dst_element_count_without_cce' $INC/tpb/aws_neuron_isa_tpb_common.h  # :2123
rg -n 'ISA_STATIC_ASSERT.*== 64' $INC/tpb/aws_neuron_isa_tpb_dma_direct2d.h          # :45 sizeof==64
#   gcc -I $INC/tpb verify_dge.c && ./a.out  ->  all three sizeof==64; bound_reg @ +38/+39

# the 16-B unit + DMA[d] index space + feature flag:
rg -n -B5 'MEMCOPY_CARVEOUT_CFG|MEMCOPY_DMA_CFG|NEURON_FEATURE_FLAGS' \
  $INC/common/aws_neuron_isa_xt_general_local_reg_defines.h   # 39 ("byte offset / 16"), 40, 38 (bit1)
# NB: SDMA_CME_BD_DESC is NOT a struct in this distribution (rg --no-ignore is empty) -> 16-B
#     BD field layout is carried cross-analysis; the 16-B *unit* is the CARVEOUT_CFG fact above.
```
