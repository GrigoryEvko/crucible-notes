# DGE 3-Backend Selector

The **DGE backend-selector** is the device-firmware mechanism — translation
unit `dge_backend_rtl.cpp` — that, once the reshape engine has resolved a
Reshape Kind, a DMA count and a partition split, decides **which of three
descriptor-generation backends** actually builds and issues the DMA descriptors:

- **Pool** — the POOL-engine fixed-function 2‑dim descriptor path,
- **RTL** — the hardware SDMA / RTL DGE that programs the UDMA rings and lets
  hardware expand a wide 5+2‑dim descriptor,
- **software** — the Q7 SW‑DGE that the firmware walks itself on the GPSIMD Q7
  cores, carrying the richest feature set (gather/scatter + dtype cast +
  arbitrary reshape).

The visible selection is a **single boolean read of a backend-availability
table** held in a fixed BSS global. The selector reads `table[0]` (the Pool
availability flag); if nonzero it logs `S: DGE: Select backend Pool` and
dispatches the Pool descriptor builder, otherwise it falls through to
`S: DGE: NO BACKEND FOUND, doing nothing` and optionally raises an error
notification. The RTL and software arms are reached from the
`dge_backend_rtl.cpp` body that references the *same* table base.

This page picks up where [DGE Setup + Context Init](dge-setup.md) hands off (the
context-init `sw=%d` flag and the availability-table populate happen there) and
routes to a backend, whose emit path is covered by
[DGE Emit](dge-emit.md). The whole selector + all three backends are **NC‑v3+
(CAYMAN onward)**; the v2 part (SUNDA) ships no DGE selector.

> **Scope.** All facts below derive from static analysis of the shipped GPSIMD
> firmware images (`libnrtucode.a` members), the shipped clean C ISA headers
> (`neuron_cayman_arch_isa`), and the shipped Cadence Xtensa toolchain
> (`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`). Confidence and evidence tags
> per [the confidence model](../../reference/confidence-model.md):
> HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED.

---

## Carved object & provenance

The selector lives in the **NX‑POOL sequencer firmware** — the `*_NX_POOL`
members of `libnrtucode.a`, source files `dge_decode_fast.cpp` /
`dge_reshape.cpp` / `dge_backend_rtl.cpp`. The byte-exact selector logic decodes
out of the **CAYMAN NX‑POOL DEBUG IRAM** image; the decision strings live in the
matching **DRAM** `.rodata`.

```text
AR    = .../custom_op/c10/lib/libnrtucode.a  (10 235 636 B)
carve = ar p $AR <member> > t.o ; \
        objcopy -O binary --only-section=.rodata t.o out.bin
```

| Image (member)                          | seg  | size    | sha256[:12]    |
|-----------------------------------------|------|---------|----------------|
| `img_CAYMAN_NX_POOL_DEBUG_IRAM`         | IRAM | 116 768 | `8e4412b99201`  |
| `img_CAYMAN_NX_POOL_DEBUG_DRAM`         | DRAM | 28 448  | `7bdf6ed7ccd2`  |
| `img_MARIANA_NX_POOL_DEBUG_IRAM`        | IRAM | 114 816 | `41b6c798bff3`  |
| `img_MARIANA_NX_POOL_DEBUG_DRAM`        | DRAM | 28 672  | `ec067304e6cf`  |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_IRAM`   | IRAM | 119 616 | `9b514bb6d45a`  |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_DRAM`   | DRAM | 29 024  | `d2e1552a13f1`  |
| `img_SUNDA_NX_POOL_DEBUG_IRAM`          | IRAM | 59 600  | `d97d1a8e6fca`  |
| `img_SUNDA_NX_POOL_DEBUG_DRAM`          | DRAM | 14 432  | `298ae996c1a3`  |
| `img_CAYMAN_Q7_POOL_DEBUG_DRAM`         | DRAM | 89 344  | `226f4254d475`  |

**Addressing rule.** The DRAM image loads at device VA `0x80000`, so a DRAM
string VA = file-offset + `0x80000`; IRAM file-offset == IRAM device VA.

> **GOTCHA — disassembly fidelity.** The NX‑POOL IRAM is linked, densely-scheduled
> FLIX/VLIW with literal pools interleaved in `.text` and **no `.symtab` / no
> `.xt.prop`**. The Pool-select *anchor* (`const16`+`l32i.n`+`beqz`) decodes
> cleanly and is quoted byte-exact below; the **RTL / software arm bodies
> FLIX-desync** a few instructions past the table read (the `.byte` litter in the
> disassembly is the tell). Therefore the authoritative sources are the
> byte-exact `.rodata` **strings**, the clean Pool-select **anchor**, the
> compile-verified **structs**, and the byte-exact **RDM schema** — never a
> desynced opcode read. Body-level claims beyond `table[0]` are tagged MED.

> **NOTE — toolchain.** Disassembly used the native Vision‑Q7 tool
> `gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump` with
> `XTENSA_CORE=ncore2gp` (Xtensa24, RI‑2022.9, `IsaMaxInstructionSize=32`,
> FLIX/VLIW). The scalar‑LX‑mismatched config mis-bundles these images; see
> [the FLIX decoding note](../../reference/flix-decoding.md) for why the body
> desyncs.

---

## 1. The selection algorithm

The selection is a **single-flag availability gate**, not a capability matrix.
Two structurally-identical Pool-select arms (per-direction / per-tensor) appear
in CAYMAN IRAM at `0xf544` and `0xfa72`; the MARIANA_PLUS twin is at `0xf9b0`.
The anchor decodes byte-exact:

```text
; CAYMAN NX-POOL IRAM @ 0xf544  (raw: 44a05d a45731 0c06 3804 16d304 a56a09)
f544: 44a05d   const16 a4, 0x5da0   ; a4 = &backend-availability table (BSS)
f547: a45731   const16 a10,0x3157   ; a10 = &"S: DGE: Select backend Pool" (VA 0x83157)
f54a: 0c06     movi.n  a6, 0        ; a6 = 0  (the NO-BACKEND return value)
f54c: 3804     l32i.n  a3, a4, 0    ; a3 = table[0]  (Pool-backend available?)
f54e: 16d304   beqz    a3, 0xf59f   ; !available -> fall to NO-BACKEND (0xf5a2)
f551: a56a09   call8   0x18bfc      ; (available) -> 'S:' logger("Select backend Pool")
;     ... per-direction Pool descriptor builders (callees ~0x14f48 / ~0x15474) ...
;     ... [FLIX-desync past here] ...
f5a2: a42f31   const16 a10,0x312f   ; a10 = &"S: DGE: NO BACKEND FOUND, doing nothing"
f5a5: 656509   call8   0x18bfc      ; 'S:' logger("NO BACKEND FOUND ...")
f5a8: 62622f   s32i    a6, a2, 188  ; store the 0 return value; retw.n
```

**[HIGH/OBSERVED]**

As annotated C pseudocode, naming the real symbols/addresses:

```c
// dge_backend_rtl.cpp — backend dispatch (NX-POOL sequencer; CAYMAN+)
// avail_table @ BSS 0x5da0 (CAYMAN) / 0x5fe0 (MARIANA, MARIANA_PLUS)
// — a per-backend boolean availability vector, RUNTIME-populated at setup.

static const uint32_t *const avail_table = (uint32_t *)0x5da0;  // const16 a4

int dge_select_backend(dge_ctx *c /* ... reshape result ... */) {
    int rc = 0;                                   // movi.n a6, 0

    if (avail_table[0] /* Pool flag */ != 0) {    // l32i.n a3,[a4]; beqz a3,...
        S_log("S: DGE: Select backend Pool");     // VA 0x83157, logger @0x18bfc
        return dge_pool_emit(c);                  // DIRECT2D 2-dim path
    }

    // [body FLIX-desynced: the dge_backend_rtl.cpp third table-ref site
    //  (const16 a2,0x5da0 @0x108ff) checks the RTL / software slot here.
    //  Reconstructed from the 2 "Select" labels + 3 log lines + sw=%d flag:]
    //   if (avail_table[RTL_SLOT])  { S_log("Select backend RTL");  rtl_emit(c); }
    //   else if (c->sw)             { /* software (Q7 SW-DGE) backend */ }

    S_log("S: DGE: NO BACKEND FOUND, doing nothing");  // VA 0x8312f
    // optional: S_log("S: DGE: Dispatched error notification") (see dge-errors.md)
    return rc;                                          // returns 0; do nothing
}
```

The visible decision is exactly: **read `table[0]`; nonzero → Pool (log + emit);
zero → NO BACKEND (log + return 0, do nothing).** The RTL / software dispatch
lives in the `dge_backend_rtl.cpp` body, which references the *same* table base
in a desynced span — so the exact table index each backend's availability lives
at and the full Pool→RTL→software fall-through order are **not byte-recoverable
beyond `table[0]=Pool`** (MED). **[HIGH/OBSERVED `table[0]`; MED slot indices]**

### The decision strings (byte-exact `.rodata`)

Read from CAYMAN NX‑POOL DEBUG DRAM `.rodata` (VA = off + `0x80000`):

| VA         | file off | string                                          |
|------------|----------|-------------------------------------------------|
| `0x83157`  | `0x3157` | `S: DGE: Select backend Pool`                   |
| `0x83173`  | `0x3173` | `S: DGE: Select backend RTL`                    |
| `0x8312f`  | `0x312f` | `S: DGE: NO BACKEND FOUND, doing nothing`       |
| `0x8318e`  | `0x318e` | `S: DGE: Dispatched error notification`         |
| `0x83105`  | `0x3105` | `S: DGE: Failed bounds check. $S[%i]+=%i.\n`    |
| `0x83529`  | `0x3529` | `dge_backend_rtl.cpp`  (`__FILE__`)             |
| `0x8353d`  | `0x353d` | `dge_backend_rtl`      (`__func__` / symbol)    |

**[HIGH/OBSERVED]**

> **QUIRK — 2 "Select" labels, 3 backend log lines.** There are **two**
> `Select backend` labels (Pool, RTL) but **three** backend descriptor log lines
> (Pool / RTL / software, below). The **software backend has no "Select backend
> software" label** — it is reached via the context `sw=%d` flag and the
> `dge_decode_fast` SW decode, not as an arm of the availability-table selector
> that logs `Select`. **[HIGH/OBSERVED count; INFERRED reading]**

### The availability table

The table base is loaded by a `const16` immediate at **exactly three sites per
generation**: the two Pool-select arms plus one `dge_backend_rtl.cpp` body site:

| gen           | table base | sites (IRAM VA)                    |
|---------------|------------|------------------------------------|
| CAYMAN        | `0x5da0`   | `0xf544`, `0xfa72`, `0x108ff`      |
| MARIANA       | `0x5fe0`   | (same shape)                       |
| MARIANA_PLUS  | `0x5fe0`   | `0xf9b0`, `0xfee6`, `0x10de4`      |

**[HIGH/OBSERVED]** The first two sites load into `a4`; the third
(`0x108ff` CAYMAN / `0x10de4` MP) loads into `a2` and immediately desyncs — this
is the RTL/software dispatch site.

The table is in the DGE BSS context region (CAYMAN `0x5d90..0x5e20`, the
per-context struct the setup function packs from). Because the SRAM/BSS carve is
**zero**, the table is **runtime-populated at boot/setup** from a HW-capability
probe — so "which backend is available" is decided at init, not baked into
`.rodata`. **[HIGH/OBSERVED base + 3 sites; INFERRED populate-at-setup]**

> **NOTE — two-tier selection.** Backend selection is **two-tier**: (1) the
> context binds an `sw`/`hw` mode at setup — `DGE contexts ... init sw=%d ...`
> (VA `0x834b0`) — where `sw=1` ⇒ the Q7 software backend and `sw=0` ⇒ a HW
> backend; (2) the per-descriptor availability table gate then picks Pool vs RTL
> vs NO-BACKEND among the HW backends. The `sw=%d` flag is documented in
> [DGE Setup + Context Init](dge-setup.md). **[HIGH/OBSERVED `sw=%d`; INFERRED
> two-tier reading]**

---

## 2. The three backends

Each backend dumps the descriptor it built before issuing it; the **field set in
the log line *is* the backend's signature**. Read byte-exact from CAYMAN DRAM
`.rodata`:

**POOL (2-dim) — VA `0x8304c`:**
```text
S: DGE $S[%i]+=%i@dmacomplete src=[0x%x]@0x%llx[0x%x,0x%x][%u,%u]
 dst=[0x%x]@0x%llx[0x%x,0x%x][%u,%u] cast:0x%x->0x%x
 bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx) compute_op: 0x%x
```
→ **2** step + **2** num per side; **has** cast + bounds + `compute_op`.

**RTL (5+2-dim) — VA `0x8354d`:**
```text
S: DGE RTL backend: $S[%i]+=%i@dmacomplete
 src=[0x%x]@0x%llx[0x%x,0x%x,0x%x,0x%x,0x%x|0x%x,0x%x][%u,%u,%u,%u,%u|%u,%u]
 dst=[0x%x]@0x%llx[0x%x,0x%x,0x%x,0x%x,0x%x|0x%x,0x%x][%u,%u,%u,%u,%u|%u,%u]
```
→ **5** outer + **2** inner step/num per side (the "5\|2" split); **no** cast,
**no** `compute_op`, **no** bounds — a pure wide-stride HW transport descriptor.

**SOFTWARE (4-dim) — VA `0x83940`:**
```text
S: DGE software backend: $S[%i]+=%i@dmacomplete
 src=[0x%x]@0x%llx[0x%x,0x%x,0x%x,0x%x][%u,%u,%u,%u]
 dst=[0x%x]@0x%llx[0x%x,0x%x,0x%x,0x%x][%u,%u,%u,%u]
 cast:0x%x->0x%x, indirection_dim=0x%x, reshape_kind=0x%x
```
→ **4** step + **4** num per side; **has** cast + `indirection_dim` +
`reshape_kind` — the richest feature set (gather/scatter + dtype cast +
arbitrary reshape).

**[HIGH/OBSERVED — byte-exact strings at the cited offsets]**

> **Decode.** Descriptor *width* grows Pool(2) < software(4) < RTL(5+2=7), but
> the *feature* set is richest on software (cast+indirection+reshape) and barest
> on RTL (pure wide transport). Pool sits in between (cast + fused compute, 2‑D).

### Backend contrast table

| backend  | desc dims | cast | indir | reshape | compute_op | expansion | exec engine          | when chosen |
|----------|-----------|------|-------|---------|------------|-----------|----------------------|-------------|
| **Pool**     | 2     | yes  | no    | no      | yes        | firmware  | POOL descriptor path | `table[0]` set — the PRIMARY/default backend; simple 2‑D strided memcopy/compute |
| **software** | 4     | yes  | yes   | yes     | no (in log)| firmware  | Q7 cores (SW‑DGE)    | context `sw=1` — general gather/scatter + dtype cast + arbitrary reshape |
| **RTL**      | 5+2   | no   | no    | no      | no         | **hardware** | RTL DGE + RDM     | RTL slot available — widest pure transport; HW does the multi-dim expansion |

**[HIGH/OBSERVED field sets; INFERRED expansion-locus + exec engine]**

All three ultimately emit **16‑B `SDMA_CME_BD_DESC`** ring entries into the
**DGE_MEMORY** carveout (1 GiB @ `0x2040000000`) and ring the **same M2S/S2M
tail-pointer doorbell**; the RDM does the tail write-back for all. The difference
is **where** the multi-dim expansion happens: Pool/software = firmware-side
(DIMPUSH loop-nest / Q7 spray), RTL = hardware-side.

### Per-backend descriptor field grounding (compile-verified)

The Pool 2‑dim descriptor maps onto the shipped 64‑B ISA struct
`NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT` (`ISA_STATIC_ASSERT(sizeof == 64)`).
The struct fields are exactly the Pool log field set:

```c
typedef struct NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT {
    NEURON_ISA_TPB_HEADER          header;           // 4   (0-3)
    NEURON_ISA_TPB_EVENTS          events;           // 8   (4-11)
    NEURON_ISA_TPB_DMA_CONFIGS     dma_configs;      // 1   (12)   <-- priority_class
    uint8_t                        semaphore;        // 1   (13)
    uint8_t                        sem_increment;    // 1   (14)
    NEURON_ISA_TPB_DGE_COMPUTE_OP  compute_op;       // 1   (15)
    NEURON_ISA_TPB_ADDR8           src_start_addr;   // 8   (16-23)
    int32_t                        src_step_elem[2]; // 8   (24-31)
    uint16_t                       src_num_elem[2];  // 4   (32-35)
    uint16_t                       src_elem_size;    // 2   (36-37)
    NEURON_ISA_TPB_BOUND_CHECK_REG src_bound_reg;    // 1   (38)
    NEURON_ISA_TPB_BOUND_CHECK_REG dst_bound_reg;    // 1   (39)
    NEURON_ISA_TPB_ADDR8           dst_start_addr;   // 8   (40-47)
    int32_t                        dst_step_elem[2]; // 8   (48-55)
    uint16_t                       dst_num_elem[2];  // 4   (56-59)
    uint16_t                       dst_elem_size;    // 2   (60-61)
    NEURON_ISA_TPB_DTYPE           in_dtype;         // 1   (62)   <-- cast (in)
    NEURON_ISA_TPB_DTYPE           out_dtype;        // 1   (63)   <-- cast (out)
} NEURON_ISA_TPB_DMA_DIRECT2D_STRUCT;
```

The fused-reduction `compute_op` is enumerated:

```c
NEURON_ISA_TPB_DGE_COMPUTE_OP_NONE     = 0x00,  // B = A; standard memcpy
NEURON_ISA_TPB_DGE_COMPUTE_OP_ADD      = 0x01,  // B += A;
NEURON_ISA_TPB_DGE_COMPUTE_OP_MULTIPLY = 0x02,  // B *= A;
NEURON_ISA_TPB_DGE_COMPUTE_OP_MAX      = 0x03,  // B = MAX(A, B);
NEURON_ISA_TPB_DGE_COMPUTE_OP_MIN      = 0x04,  // B = MIN(A, B);
```

The software backend's `indirection_dim` is the ISA enum
`NEURON_ISA_TPB_INDIRECT_DIM` (`X=0, Y=1, Z=2, W=3`). The RTL **5+2** descriptor
has **no single 64‑B ISA struct** that enumerates a 5+2 split — it is the RTL
DGE's **HW-internal** wide form the backend programs into the rings, not a
shipped struct. **[HIGH/OBSERVED struct + enums; MED/INFERRED RTL HW form]**

> **The software backend is NOT a legacy iDMA path.** The software backend is the
> *active, feature-rich* general path. The shipped ISA header names it verbatim:
> the `DmaGatherTranspose` descriptor "uses the **SW-DGE backend with Q7
> processors in the Gpsimd engine**"
> (`aws_neuron_isa_tpb_dma_gather_xpose.h:17`). It handles exactly the
> gather/cast/reshape cases the Pool/RTL fixed-function descriptors cannot, is
> present CAYMAN-onward, and has its own Q7 execution leg. **[HIGH/OBSERVED]**
> For the iDMA cache-fill path it is *contrasted with*, see
> [iDMA / Legacy DMA](idma-legacy-dma.md).

---

## 3. The RTL backend's hardware interface

The RTL/HW backend's descriptor-generation + write-back hardware is the **UDMA
M2S/S2M per-queue descriptor rings** (the `rx.base`/`tx.base` of the DGE context,
set up in [DGE Setup + Context Init](dge-setup.md)) plus the **RDM —
Ring-Descriptor-Manager**: a 4 KiB (`0x1000`) APB block under INTC.

What the RTL backend programs / what the RDM HW does (byte-exact from the shipped
`rdm_model.json`; the RTL body itself is desynced so the *binding* edge is
inferred):

- **Per queue `i`** (24 queues): `queue_base_{lo,hi}_{m2s,s2m}_i` (host AXI ring
  base, 57‑bit), `queue_tail_ptr_{lo,hi}_{m2s,s2m}_i` (the AXI address the tail
  pointer is **written back** to), `queue_size_i` (in **16‑B-descriptor units**;
  "M2S and S2M queues must have the same size").
- The RDM **issues writes only** — its read auto-responder is permanently
  clock-gated (`ctrl.clkgate_req` reset = 1, "RDM does not implement reads").
- On each descriptor write the RDM **overwrites the 2‑bit `ringId` field**
  (`ringid_enable` reset = 1, `ringid_initial` reset = `0x55555555` = 2 bits per
  queue) and maintains a linked-list of in-flight AXI write IDs
  (`sta_ids_available` / `sta_queue_head`). On a write-response (B-channel)
  error it freezes tail-ptr writes (`control.tail_ptr_write_disable_auto`).

So the **RTL backend = the producer/doorbell**: it builds the wide 5+2‑dim
descriptor, programs the M2S/S2M rings and bumps the per-queue tail-inc doorbell
(`s32i a3, a4, 0`); the **RDM HW = the tail-writeback / ringId-inject / notify
HW**. The descriptor is wide precisely because the **RTL hardware does the
multi-dim expansion** the firmware (Pool/software) does in software.

> **NOTE — iDMA glr CSR reconcile.** The RTL backend's ring programming targets
> the UDMA M2S/S2M descriptor rings + the RDM tail-pointer block above. This is
> *distinct from* the iDMA "generate-local-ring" (glr) CSRs at `0x1120 / 0x1140
> / 0x1160 / 0x1180` that drive the legacy IRAM cache-fill DMA path — see
> [iDMA / Legacy DMA](idma-legacy-dma.md). No string in the (desynced) RTL body
> ties `dge_backend_rtl.cpp` to a specific RDM or glr register, so the
> "RTL backend programs *this*" edge is INFERRED.

**[HIGH/OBSERVED RDM schema + ring doorbell; INFERRED RTL-programs-this edge]**

---

## 4. The `priority_class` (5 classes) the selector consults

The DGE descriptor carries a **priority class** in its `dma_configs` byte
(byte 12 of every `DIRECT2D` descriptor, above). The ISA header
(`aws_neuron_isa_tpb_common.h`) defines it as a **3‑bit field** with a validity
predicate of **`priority_class <= 4`** — i.e. **five classes, 0..4**:

```c
typedef struct NEURON_ISA_TPB_DMA_CONFIGS {
    uint8_t priority_class    : 3;   // priority class
    uint8_t reserved_bitfield : 5;   // must be zero
} NEURON_ISA_PACKED NEURON_ISA_TPB_DMA_CONFIGS;   // sizeof == 1

// is_valid_dma_configs(dma_configs) :=
//       (dma_configs.priority_class <= 4)     <-- five classes, 0..4
//    && (dma_configs.reserved_bitfield == 0)
```

Compile-verified: `sizeof(NEURON_ISA_TPB_DMA_CONFIGS) == 1`; the
`priority_class <= 4` predicate is at `aws_neuron_isa_tpb_common.h:2071`.
**[HIGH/OBSERVED]**

**Where it is read and how it affects selection.** The priority class is a field
of the descriptor every backend builds, but it is the **RTL / HW backend** that
is the priority-class-aware path: the priority arbitrates **which
descriptor-generation request the hardware services first** (QoS). The five
descriptor classes correspond to the **host 5-slot priority-class map**
configured at DGE setup — see [DGE Setup + Context Init](dge-setup.md). The map's
host-side wiring (which host queue feeds which device class) is documented
forward in Part 8.

> **NOTE — forward links.** The host-side **priority-class map** is documented in
> [runtime/dge-host-api.md](../../runtime/dge-host-api.md) (Part 8), and the
> **QoS/builder** side in
> [dma/dge-builder-qos.md](../../dma/dge-builder-qos.md) (Part 9). This page
> covers only the device-side `priority_class` field the selector consults.

> **CORRECTION — shape-register count symbol.** The `$S[%i]` shape-register file
> all three backend logs print is **4‑deep**, but the backing report cited the
> symbol as `NUM_DGE_SHAPE_REGISTERS == 4`. In the shipped headers the actual
> symbol is `NEURON_ISA_TPB_n = 4U`, with the validity check indexing
> `shape_registers[0..3]` (`aws_neuron_isa_tpb_ctrl_ms.h` /
> `aws_neuron_isa_tpb_assert.h`). The **4-deep fact holds**; the symbol name does
> not. **[HIGH/OBSERVED]**

---

## 5. Per-generation presence

The whole selector + all three backends are **CAYMAN-onward (NC‑v3+)** and
**absent in SUNDA (NC‑v2)**. Counted with `rg -c -a` (metric:
**pattern-occurrence count over the carved DRAM `.rodata`**):

| string (metric: `rg -c -a` over DRAM)   | SUNDA | CAYMAN | MARIANA | MARIANA_PLUS | CAYMAN_Q7 |
|-----------------------------------------|-------|--------|---------|--------------|-----------|
| `S: DGE: Select backend Pool`           | 0     | 1      | 1       | 1            | 0         |
| `S: DGE: Select backend RTL`            | 0     | 1      | 1       | 1            | 0         |
| `NO BACKEND`                            | 0     | 1      | 1       | 1            | 0         |
| `S: DGE $S[%i]+=%i@dmacomplete` (Pool)  | 0     | 1      | 1       | 1            | 0         |
| `S: DGE RTL backend`                    | 0     | 1      | 1       | 1            | 0         |
| `S: DGE software backend`               | 0     | 1      | 1       | 1            | 0         |
| `dge_backend_rtl`                       | 0     | 1      | 1       | 1            | 0         |

**[HIGH/OBSERVED]**

- **SUNDA (NC‑v2): absent.** Every selector string counts **0** — SUNDA's NX_POOL
  has only a reshape stub and no DGE selector. This is the per-gen wall:
  **HWDGE (the RTL backend) and the whole selector arrive with CAYMAN.**
- **CAYMAN / MARIANA / MARIANA_PLUS (NC‑v3 / v4): present** and **byte-identical
  in field set**. Only string *offsets* shift by the inserted MARIANA_PLUS
  fast-path strings: Pool `0x3157 → 0x3197`, RTL
  `0x3173 → 0x31b3`, NO-BACKEND `0x312f → 0x316f` (all `+0x40`).
- **CAYMAN_Q7: absent.** The Q7 image carries **none** of the selector strings —
  the selector is **NX‑POOL-only** (the NX sequencer runs the backend pick); the
  Q7 cores are the *software backend's execution leg*, reached after the NX
  selects `sw=1`.
- **MAVERICK (NC‑v5): INFERRED.** No Maverick firmware ships in this
  `libnrtucode.a` (`ar t | rg -i maverick` is empty), so Maverick's runtime
  backend behaviour is **not determinable from the firmware**. The Maverick ISA
  *headers* do ship (`neuron_maverick_arch_isa`) — including the `gather_xpose`
  descriptor with the same "SW-DGE backend with Q7 processors" comment — so the
  DGE ISA surface survives into Maverick. Whether Maverick's NX_POOL firmware
  retains the same 3-backend selector is **INFERRED / out of scope**.
  **[HIGH/OBSERVED absence; LOW any Maverick runtime claim]**

> **NOTE — RDM hardware is gen-wide.** The RTL backend's *hardware* (the RDM) is
> byte-identical across SUNDA/CAYMAN/MARIANA/MARIANA_PLUS (Maverick header-only).
> So the RTL backend's HW **exists on every gen including SUNDA** — but SUNDA's
> firmware has no DGE selector to use it. The HW exists gen-wide; the firmware
> *emitter* is CAYMAN-onward. **[HIGH/OBSERVED — RDM schema.]**

The `dge_backend_rtl` string is also present in the symbol-bearing
`img_CAYMAN_NX_POOL_TEST_DRAM` `.rodata` — confirming `dge_backend_rtl` is a
**real compiled function** whose name was retained, not merely a DEBUG format
string.

---

## 6. End-to-end selection + dispatch

```text
CONTEXT SETUP (dge-setup.md):
  "DGE contexts ... init sw=%d engine=%u queue_idx=%u queue_num=%u"
     sw=%d  -> SOFTWARE-vs-HARDWARE tier (sw=1 => Q7 software backend)
     (BSS availability table populated from a HW-capability probe at setup)
        |
        v
RESHAPE (dge-reshape): analyze_tensor_reshape -> Reshape Kind + #DMA + split
        |
        v
BACKEND SELECT (THIS page, dge_backend_rtl.cpp, NX-POOL only):
  const16 a4, &avail_table (0x5da0/0x5fe0) ; l32i a3,[a4] ; beqz
    table[0] != 0 ? --yes--> "Select backend Pool" -> POOL emit (2-dim)
                    --no -->  [dge_backend_rtl.cpp: RTL slot?]
                               --avail--> "Select backend RTL" -> RTL HW path
                               --else (sw=1)--> SOFTWARE backend (Q7 SW-DGE)
                               --else--> "NO BACKEND FOUND, doing nothing"
                                         (+ optional "Dispatched error
                                          notification", dge-errors.md)
        |
        v   per backend (see dge-emit.md):
        |   POOL    : DIRECT2D 64-B -> GENERATE + 2x DIMPUSH -> firmware 2-D walk
        |   SOFTWARE: 4-dim + cast/indir/reshape -> Q7 decode/spray -> RDM doorbell
        |   RTL     : 5+2-dim -> program UDMA rings + RDM -> HARDWARE expands
        v
TRANSPORT: M2S/S2M tail-ptr doorbell -> RDM write-backs tail + injects 2-bit ringId
        |
        v
ERRORS (dge-errors.md): bounds OOB / NO BACKEND -> report-and-continue
```

---

## Cross-references

- [DGE Setup + Context Init](dge-setup.md) — the context-init `sw=%d` flag, the
  availability-table populate, and the hand-off **to** this selector (Part 5, #683).
- [DGE Emit](dge-emit.md) — the GENERATE / DIMPUSH / REGWRITE the chosen backend
  issues, and the per-backend descriptor emit (Part 5, #686).
- [DGE Errors](dge-errors.md) — the `NO BACKEND FOUND` fall-through and the
  `Dispatched error notification` the selector's no-backend arm feeds.
- [iDMA / Legacy DMA](idma-legacy-dma.md) — the legacy IRAM cache-fill DMA path
  the software backend is contrasted with, and the glr CSRs (Part 5, #682).
- [runtime/dge-host-api.md](../../runtime/dge-host-api.md) — the host-side
  priority-class map (Part 8, #825).
- [dma/dge-builder-qos.md](../../dma/dge-builder-qos.md) — the host QoS/builder
  side (Part 9, #837).
- [Confidence model](../../reference/confidence-model.md) — the
  HIGH/MED/LOW × OBSERVED/INFERRED tagging used throughout.

---

## Reproduction

```sh
export XTENSA_SYSTEM=.../gpsimd_tools_tgz/tools/XtensaTools/config
export XTENSA_CORE=ncore2gp
AR=.../custom_op/c10/lib/libnrtucode.a
INC=.../custom_op/c10/include/neuron_cayman_arch_isa

# carve (per member): ar p $AR img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o > t.o ;
#   objcopy -O binary --only-section=.rodata t.o cayman_iram.bin
#   -> cayman_iram 8e4412b9 / cayman_dram 7bdf6ed7 / mp_iram 9b514bb6

# selector strings (VA = off + 0x80000):
rg -a -b -o 'Select backend Pool|Select backend RTL|NO BACKEND|RTL backend|software backend|dge_backend_rtl' cayman_dram.bin
#   Pool 0x3157, RTL 0x3173, NO-BACKEND 0x312f, RTL-log 0x354d, SW-log 0x3940, rtl.cpp 0x3529

# Pool-select ANCHOR (decodes cleanly):
xtensa-elf-objdump -D -b binary -m xtensa --start-address=0xf544 --stop-address=0xf5b0 cayman_iram.bin
#   const16 a4,0x5da0 ; const16 a10,0x3157 ; l32i.n a3,a4,0 ; beqz a3,0xf59f
xtensa-elf-objdump -D -b binary -m xtensa cayman_iram.bin | rg 'const16.*0x5da0'   # 0xf544 0xfa72 0x108ff

# per-gen presence: rg -c -a 'Select backend Pool' {sunda,cayman,mariana,mariana_plus}_dram.bin  # sunda 0, others 1
ar t $AR | rg -i maverick    # empty -> no Maverick firmware

# headers:
rg -n 'priority_class' $INC/tpb/aws_neuron_isa_tpb_common.h            # :714 (:3 field), :2071 (<= 4 gate)
rg -n 'SW-DGE backend' $INC/tpb/aws_neuron_isa_tpb_dma_gather_xpose.h  # :17
# RDM HW: cayman-arch-regs rdm/rdm_model.json (24 queues; tail-ptr WB; ringid_enable)
```
