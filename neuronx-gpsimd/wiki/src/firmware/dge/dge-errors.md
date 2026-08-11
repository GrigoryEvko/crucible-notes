# DGE Error Notifications

This page reconstructs the **error-notification path of the DGE (Descriptor-Generation
Engine)** in the Q7/POOL GPSIMD firmware: how the DGE **detects** a per-descriptor fault, how
it **packs** an error-notification record, how that record is **delivered** to the host, and how
the whole path is **gated** by a global feature flag and a per-descriptor suppress bit. It is the
last stage of the DGE descriptor-generation sub-lane — it picks up exactly where the
[descriptor-emit path](dge-emit.md) detects an out-of-bounds (OOB) address and traces what
happens from the failed bounds check to the host-drained notification.

The DGE detects errors **in software** (a per-descriptor validity check evaluated *before* a
descriptor is emitted), but it **delivers** them through the *same* on-die notification
machinery the [SEQ Error-Handler](../seq/error-handler.md) uses. That split — **separate
detection, shared delivery** — is the central reconciliation this page resolves, and it is
proven below from the `.rodata` translation-unit layout and a call census of the DGE dispatch
region.

Everything here is **byte-pinned to shipped artifacts**: the DEBUG
POOL firmware images carved out of `libnrtucode.a` and decoded with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, `ConfigName=Xm_ncore2gp`, uarch "Cairo",
`TargetHWVersion=NX1.1.4`, `IsaMaxInstructionSize=32` FLIX/VLIW); the shipped clean C ISA
headers (`neuron_*_arch_isa`, compile-verified with `gcc -I … ; offsetof/sizeof`); and the
shipped cayman arch-regs notification schema. Every log line resolves against the firmware's own
DRAM string image; every struct/enum/field/predicate is read from the shipped headers. Where an
earlier reading disagrees with the binary, **the binary wins**, and an
in-place **CORRECTION** says so.

Confidence tags follow the [project model](../../reference/confidence-model.md): `OBSERVED` = a
byte/string/instruction/struct read from a shipped artifact; `INFERRED` = reasoned
over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

> **NOTE — the honest limit, stated once up front.** The DGE bounds-check + dispatch **function
> bodies are FLIX-desynced** (the same MED ceiling the [setup](dge-setup.md) and
> [error-handler](../seq/error-handler.md) pages hit). The
> `"Failed bounds check"` (VA `0x83105`) and `"Dispatched error notification"` (VA `0x8318e`)
> strings are **neither** `const16`-loaded **nor** held as `l32r` literals anywhere in IRAM
> (0 hits each), whereas `"NO BACKEND FOUND"` (`0x8312f`) and `"Select backend Pool"`
> (`0x83157`) **are** cleanly `const16`-loaded (4 and 2 sites). The bounds-fail/dispatch strings
> are therefore reached via a **string-pool base register + a computed displacement**, so the
> per-instruction dispatch body — and the exact numeric `error_id`/`notific_type` the DGE OOB
> writes — is **not byte-recoverable**. The authoritative grounding is therefore: the byte-exact
> **strings** (§2), the compile-verified notification **structs** (§3), the header **gating
> predicates** (§5), and the call **census** (§4, §7). This is documented, not hidden.
> `[HIGH/OBSERVED]` for the const16/literal absence; the desync ceiling is the corpus-wide MED.

---

## 0. The detect → gate → dispatch → deliver spine in one diagram

```
  ┌────────────────── DGE error-notification path (Q7/POOL, per descriptor) ─────────────────┐
  │                                                                                           │
  │  DETECTION  (dge_reshape.cpp / dge_backend_rtl.cpp TU, BEFORE the descriptor is emitted)  │
  │    ├─ src/dst address bounds check  (the primary source — from dge-emit)                  │
  │    │     "bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx)"   addr<=limit per direction    │
  │    │        └─ addr > limit  ─────────► "S: DGE: Failed bounds check. $S[%i]+=%i."        │
  │    │                                     (VA 0x83105 — names overflowed $S[i] + offset)   │
  │    ├─ no backend available  ──────────► "S: DGE: NO BACKEND FOUND, doing nothing"         │
  │    │                                     (VA 0x8312f — do nothing, may dispatch notif)    │
  │    └─ structural / descriptor-invalid ► HandleIllegalInstr (call8 0x13f80 @0x103a3)       │
  │           │                                                                               │
  │           ▼   GATE 1: NEURON_FEATURE_FLAGS bit 1 ("DGE packet notification", local-reg 38)│
  │           │   GATE 2: failing-direction.bc_disable_oob_error_notif == 0  (per descriptor) │
  │           │            (indirect path: + idx_bound_is_err policy, §5c)                    │
  │           ▼                                                                               │
  │  DISPATCH  pack 16-B NEURON_ISA_TPB_ERROR_NOTIFICATION                                    │
  │           {error_id@0, metadata_lo@1, header@3, metadata_hi@4, timestamp@8}               │
  │           "S: DGE: Dispatched error notification"   (VA 0x8318e)                          │
  │           │                                                                               │
  │           ▼   SHARED on-die egress: call8 0xa2f8 (rer/wer notify helper, 4× in region)    │
  │           │   — the SAME primitive build_error_record@0x13e40 / raise_error@0x13e18 use   │
  │           ▼                                                                               │
  │  DELIVERY  per-TPB NOTIFIC instruction-notification queue (notific_10_queue)              │
  │           route = sw_queue_num3 (errors_NT)  →  16-B ring entry  →  host phase-bit drain  │
  │           │                                                                               │
  │  RECOVERY descriptor NOT emitted; engine CONTINUES (no 0x13e00 FATAL call — report+go-on) │
  └───────────────────────────────────────────────────────────────────────────────────────────┘
```

One-line verdict: a DGE OOB is a **software descriptor-content validation** that fails *before*
emit, logs `"Failed bounds check"`, and — gated by the feature flag and the per-descriptor
suppress bit — packs the **arch-common 16-byte error-notification record** and pushes it through
the **shared** on-die notify primitive into the **NOTIFIC error ring** the host drains. It is
**report-and-continue**, never the SEQ hard halt.

---

## 1. Image identification + addressing model

The DGE error path lives in the **main per-engine POOL firmware images** carved from the static
archive

```
extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/
  gpsimd/custom_op/c10/lib/libnrtucode.a
```

as members `img_<GEN>_NX_POOL_DEBUG_<SEG>_contents.c.o`; the carve is
`ar p <member> | objcopy -O binary --only-section=.rodata`. All eight images
(sizes in bytes, sha256 prefix):

| Member (DEBUG) | Seg | `.rodata` size | sha256 (prefix) | Role here |
|---|---|---|---|---|
| `img_CAYMAN_NX_POOL_DEBUG_IRAM` | IRAM | 116768 | `8e4412b99201` | **trace image**: dispatch-region call census (§4/§7) |
| `img_CAYMAN_NX_POOL_DEBUG_DRAM` | DRAM | 28448 | `7bdf6ed7ccd2` | DGE error string corpus (§2) + TU adjacency proof (§6) |
| `img_MARIANA_NX_POOL_DEBUG_IRAM` | IRAM | 114816 | `41b6c798bff3` | per-gen presence |
| `img_MARIANA_NX_POOL_DEBUG_DRAM` | DRAM | 28672 | `ec067304e6cf` | per-gen presence |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_IRAM` | IRAM | 119616 | `9b514bb6d45a` | per-gen presence |
| `img_MARIANA_PLUS_NX_POOL_DEBUG_DRAM` | DRAM | 29024 | `d2e1552a13f1` | per-gen presence |
| `img_SUNDA_NX_POOL_DEBUG_IRAM` | IRAM | 59600 | `d97d1a8e6fca` | v3 presence baseline (no DGE emit/notif) |
| `img_SUNDA_NX_POOL_DEBUG_DRAM` | DRAM | 14432 | `298ae996c1a3` | v3 presence baseline |

The `CAYMAN_NX_POOL` DEBUG IRAM (`8e4412b9…`) / DRAM (`7bdf6ed7…`) pair is the **same firmware**
the [DGE setup](dge-setup.md) and [SEQ error-handler](../seq/error-handler.md) pages anchor on —
hashes match exactly. `[HIGH/OBSERVED]`

**Addressing rules.** IRAM loads at device VA `0x0` (IRAM offset == VA). The DRAM image loads at
device VA `0x80000`, so **DRAM-string VA = file-offset + 0x80000** (every string VA below uses
this). The firmware materialises a DRAM VA with a `const16 aX,8 ; const16 aX,0xNNNN` pair (high
half `0x0008`).

> **GOTCHA — the FLIX/VLIW desync ceiling.** These are linked, densely-scheduled FLIX bundles
> (up to 32 B) with literal pools interleaved in `.text` and **no `.symtab`**. A linear sweep
> decodes recognisable bundles correctly but loses sync across literal/selector-byte boundaries,
> rendering those spans as `.byte` with bogus targets. So: the **string corpus is ground truth**
> (byte-exact `.rodata`); the **notification structs / gating predicates are ground truth**
> (gcc-compiled from the shipped clean headers); the **notification route is CARRIED** from the
> arch-regs schema (CSR-06); but the **dispatch body offsets are MED/desynced** — see
> [the confidence model](../../reference/confidence-model.md).

---

## 2. The DGE error-notification string corpus — ground truth

Read byte-exact from `CAYMAN_NX_POOL_DEBUG` DRAM `.rodata` (`rg -a -b -o`; VA = off + `0x80000`).
These strings **encode** the detection conditions and the dispatch event.

### 2a. The detection + dispatch strings

| VA | file off | String (byte-exact) | Role |
|---|---|---|---|
| `0x83105` | `0x3105` | `S: DGE: Failed bounds check. $S[%i]+=%i.` | OOB **detection** log: names the overflowed shape register `$S[i]` (one of `NUM_DGE_SHAPE_REGISTERS`=4) and the `+offset` (the iteration cursor) that pushed its effective address past the bound limit. |
| `0x8312f` | `0x312f` | `S: DGE: NO BACKEND FOUND, doing nothing` | no-backend **detection**: DGE does nothing and may dispatch the notification. |
| `0x83157` | `0x3157` | `S: DGE: Select backend Pool` | backend-selector context (the no-backend fall-through is the error arm). |
| `0x83173` | `0x3173` | `S: DGE: Select backend RTL` | backend-selector context. |
| `0x8318e` | `0x318e` | `S: DGE: Dispatched error notification` | the **delivery** event: emitted AFTER the bounds-fail, UNLESS suppressed by `bc_disable_oob_error_notif` or the feature flag. |

All five offsets are byte-grounded in `cayman_dram.bin`. `[HIGH/OBSERVED]`

### 2b. The runtime bounds-check evaluation (the detection arithmetic)

Inside the Pool/RTL/software backend log line, the substring (byte-confirmed at off `0x30c2` =
VA `0x830c2`):

```
bounds:(%1d 0x%llx<=0x%llx, %1d 0x%llx<=0x%llx)
```

is **two** `{bc_enabled-flag (%1d), effective_addr (0x%llx) <= bound_limit (0x%llx)}` pairs, src
then dst. The DGE evaluates `addr <= limit` per direction; `addr > limit` triggers the §2a
`Failed bounds check`. The construction of this check (per-descriptor, before emit) is the
[emit page](dge-emit.md)'s subject; this page picks up at the failed comparison.
`[HIGH/OBSERVED]` substring; `[HIGH/INFERRED]` per-direction semantics.

### 2c. The shared SEQ-egress strings (in this same NX DRAM)

The DGE dispatch reaches the **same** report sink the SEQ error-handler uses (§4); that sink's
log strings live in the same DRAM image:

| VA | file off | String |
|---|---|---|
| `0x82286` | `0x2286` | `S: NOTIFY` |
| `0x82257` | `0x2257` | `S: sending interrupt` |
| `0x8226d` | `0x226d` | `S: sending notification` |

These are the report-sink `0xa450`'s log strings (matching the [SEQ error-handler](../seq/error-handler.md)
§6b read); the DGE dispatch reaches the same external-register notify primitive.

### 2d. Per-gen string presence (`rg -c -a` over carved DRAMs)

| String | SUNDA | CAYMAN | MARIANA | MARIANA_PLUS |
|---|---|---|---|---|
| `Failed bounds check` | 0 | 1 | 1 | 1 |
| `Dispatched error notification` | 0 | 1 | 1 | 1 |

The DGE error-notification path is **CAYMAN-onward**, absent in SUNDA — matching the DGE emit
path itself (the [backend-selector](dge-backend-selector.md) per-gen table).

> **COUNT DISCIPLINE.** The "0/1" figures above are the **per-image substring-occurrence count**
> (`rg -c -a '<string>' <gen>_dram.bin`, one count per carved DRAM). They are *not* a `'S: '`
> census and not a symbol-table count. Re-run command for any audit:
> `for g in sunda cayman mariana mariana_plus; do rg -c -a 'Dispatched error notification'
> ${g}_dram.bin; done`.

---

## 3. The notification record FORMAT — compile-verified

The DGE error notification is the **arch-common 16-byte `NEURON_ISA` notification record**. All
sizes/offsets/enums below are compile-verified
(`gcc -I .../neuron_cayman_arch_isa/common ; offsetof/sizeof printed`).

### 3a. The record envelope

```c
#define NEURON_ISA_NOTIFICATION_NBYTES  0x10   /* every queue entry is 16 B = 128 b */
/* sizeof(NEURON_ISA_NOTIFICATION) == 16  (the union of all record types) — VERIFIED */

typedef struct {                       /* sizeof == 1 — VERIFIED */
    uint8_t  notific_type:5;           /* see NEURON_ISA_NOTIFICATION_TYPE */
    uint8_t  software_queue_overflow:1;
    uint8_t  hardware_queue_overflow:1;
    uint8_t  phase:1;                  /* the ring-wrap poll bit */
} NEURON_ISA_NOTIFICATION_HEADER;
```

`[HIGH/OBSERVED]` — `NBYTES=0x10`, `sizeof(NOTIFICATION)==16`, `sizeof(HEADER)==1`, bitfield
order compile-verified.

### 3b. The error flavour — `NEURON_ISA_TPB_ERROR_NOTIFICATION` (sizeof == 16, VERIFIED)

The struct body, read verbatim from `aws_neuron_isa_notification.h` and its measured offsets:

```c
typedef struct {                                    /* sizeof == 16 — VERIFIED */
    uint8_t                            error_id;     /* +0   see NEURON_ISA_TPB_ERROR_TYPE      */
    NEURON_ISA_TPB_ERROR_METADATA_LO   metadata_lo;  /* +1   union, sizeof 2 — bits 15:0        */
    NEURON_ISA_NOTIFICATION_HEADER     header;       /* +3   notific_type = ERROR / TPB_ERROR   */
    NEURON_ISA_TPB_ERROR_METADATA_HI   metadata_hi;  /* +4   union, sizeof 4 — bits 47:16       */
    NEURON_ISA_NOTIFICATION_TIMESTAMP  timestamp;    /* +8   64-b ps counter (NOTIFIC-snapshot) */
} NEURON_ISA_PACKED NEURON_ISA_TPB_ERROR_NOTIFICATION;
```

Measured offsets: `error_id @0`, `metadata_lo @1`, `header @3`,
`metadata_hi @4`, `timestamp @8`; `sizeof(METADATA_LO)==2`, `sizeof(METADATA_HI)==4`.

> **GOTCHA — `metadata_lo` straddles the `header` byte.** The layout is **not** a clean
> `{u8, u8, u16, u32, u64}`. The 16-bit `metadata_lo` sits at `+1..+2`, the 1-byte `header` at
> `+3`, and the 4-byte `metadata_hi` at `+4..+7`. A reimplementer packing this record must place
> the header byte **between** the two metadata halves, not after them. The error metadata is
> thus a 48-bit field split `lo@+1 (15:0)` / `hi@+4 (47:16)` around the header.

### 3c. The type + error-id enums (compile-verified values)

```c
/* NEURON_ISA_NOTIFICATION_TYPE — header notific_type:5 */
ERROR     = 0x03   /* engine-error / interrupt+warning class (n_type-distinguished)  */
DMA       = 0x02
TPB_ERROR = 0x1f   /* the TPB error-notification class (the TPB_ERROR_NOTIFICATION)  */

/* NEURON_ISA_TPB_ERROR_TYPE — the error_id @+0 */
FP_UNDERFLOW=0x00 FP_NAN=0x01 FP_INF=0x02 FP_OVERFLOW=0x03 MEMORY_ERROR=0x04
FAKE_ERROR=0x05 SEMAPHORE_ERROR=0x06 EVENT_ERROR=0x07 PSUM_COLLISION=0x08
SEQUENCER_NONFATAL=0x09 SEQUENCER_FATAL=0x0a
```

A DGE OOB / descriptor-gen fault is a **SEQUENCER-class** error: `SEQUENCER_NONFATAL (0x09)` for
the recoverable/skip case, `SEQUENCER_FATAL (0x0a)` for a hard one. Its `metadata_hi` union arm is:

```c
typedef struct {                       /* NEURON_ISA_TPB_SEQUENCER_GENERIC_ERROR_METADATA_HI */
    uint32_t block_id:4;
    uint32_t program_counter:12;
    uint8_t  info;   /* BAD_OPCODE=opcode, SIGNAL=signo, FP={inexact,underflow,overflow,
                        div-zero,invalid-exc}, EXCEPTION={Type:4, Cause:4 from corebits.h},
                        OTHER=reserved   (comment verbatim) */
} NEURON_ISA_PACKED ...SEQUENCER_GENERIC_ERROR_METADATA_HI;
```

> **CORRECTION — the sequencer metadata_hi is `{block_id:4, program_counter:12, info:8}`, not
> just `info:8`.** A prior survey names only the `info:8` sub-field. The header shows the full
> 32-bit union arm carries the faulting **engine `block_id`** (4 b)
> and the **program counter** (12 b) alongside `info` (8 b). For a DGE OOB the `block_id` is the
> engine that owns the descriptor, the `program_counter` is the faulting instruction, and `info`
> carries the condition byte. The record therefore self-identifies the source engine in
> `metadata_hi.block_id` — which is how the host distinguishes a DGE error from a SEQ error on
> the shared error ring (§4c).

> **NOTE — which `notific_type` the DGE OOB writes is INFERRED, not byte-recovered.** The enum
> values are OBSERVED; the SEQUENCER error-class mapping is OBSERVED; but *which* of
> `ERROR (0x03)` / `TPB_ERROR (0x1f)` the DGE dispatch body writes to `header.notific_type`, and
> *which* `error_id` (`0x09` vs `0x0a`), is a **desynced body field** — the firmware names the
> *condition* via the string set, not the numeric field. The host routes by `notific_type` (§4),
> so this matters; it is flagged `[MED/INFERRED]`. The reconciliation with the SEQ packer (§4c)
> bounds it: the DGE shares the SEQ record family, and the SEQ class for an
> engine/sequencer fault is the same SEQUENCER metadata arm.

### 3d. The DGE *completion* record (the non-error sibling, for contrast)

The same feature flag (§5a) also gates the SUCCESS-path "DGE packet notification" —
`NEURON_ISA_TPB_DGE_METADATA_NOTIFICATION` (`sizeof == 16`, VERIFIED), a TPB EXPLICIT
notification with `debug_hint == METADATA_TYPE_DGE (0x1)`:

```c
typedef struct {                          /* sizeof == 16 — VERIFIED */
    uint32_t program_counter:20;
    uint32_t debug_hint:4;                /* = NEURON_ISA_TPB_INSTRUCTION_METADATA_TYPE_DGE = 0x1 */
    NEURON_ISA_NOTIFICATION_HEADER header;
    uint16_t dma_map;                     /* bitmap of 16 DMA engine IDs                       */
    uint16_t num_packets:12;              /* DMA packets per engine (0 => 4096)                */
    uint16_t reserved:2;
    uint16_t infer_program_counter:1;
    uint16_t dma_group_id:1;
    uint64_t timestamp;
} NEURON_ISA_PACKED NEURON_ISA_TPB_DGE_METADATA_NOTIFICATION;
```

This is the success telemetry the **same** feature-flag bit 1 enables — one flag covers the
whole DGE→host notification class (completion *and* error). `[HIGH/OBSERVED]` header;
`[HIGH/INFERRED]` the shared-flag reading.

---

## 4. The delivery PATH — shared on-die egress → NOTIFIC error ring

The delivery is **two layers**: an on-die firmware producer (this blob) and a hardware NOTIFIC
transport (the arch-regs schema). The DGE does **not** have its own egress — it shares the SEQ's.

### 4a. The on-die producer — the shared notify primitive `0xa2f8`

A call census of the **DGE dispatch region** (IRAM `0xf000`–`0x10600`, the backend-select +
bounds + dispatch code) shows the dispatch reaches `0xa2f8`, the
external-register notify helper:

```text
; DGE dispatch-region call census — CAYMAN_NX_POOL_DEBUG IRAM 0xf000–0x10600
;   call8 0xa2f8   ×4   <-- the rer/wer external-register notify helper (shared with SEQ)
;   call8 0x18b84  ×12  ; the 'S:' printf-logger
;   call8 0x18bfc  ×9   ; the 'S:' printf-logger (second variadic entry)
;   call8 0x1a084  ×6   ; printf helper
;   call8 0x13f80  ×1   ; HandleIllegalInstr @0x103a3 (structural-invalid descriptor, §4d)
;   call8 0x13e00  ×0   ; *** NO FATAL raise wrapper — report-and-continue (§7) ***
;   call8 0xa2e0   ×0   ; *** NO halt-dispatch ***
```

`0xa2f8` is the **same** external-register write primitive the SEQ
[`build_error_record@0x13e40`](../seq/error-handler.md) calls internally
(`0x13e40` → `call8 0xa2f8 @0x13e71`). So the DGE dispatch and the SEQ packer **share
the same notify-write helper**. `[HIGH/OBSERVED]`

> **CORRECTION — the DGE region calls `0xa2f8` four times, not three.** A prior survey
> reports `0xa2f8 (3×)` in `0xa000`–`0x105ff`. A census over `0xf000`–`0x10600`
> (the string-loading dispatch region) returns **4** `call8 0xa2f8` sites
> (`0xf1e0`, `0xf685`, `0xfba0`, `0x10040`). The count is a refinement, not a contradiction —
> the conclusion (the DGE reaches the shared notify primitive) is unchanged.

The full external-register write (the exact NOTIFIC-input-buffer CSR) is in a FLIX-desynced span
— `0xa2f8`'s body renders partly as `.byte`/ill (the same MED limit as the
[SEQ report sink `0xa450`](../seq/error-handler.md) §6b). The **call to the shared primitive is
OBSERVED**; the precise destination address is **MED — desynced**.

The SEQ delivery half, for the reconciliation:

```text
00013e18 <raise_error>:                 ; the shared raise core
    13e18:  entry  a1, 48
    13e20:  l32i   a2, a1, 8            ; a2 = packed record
    13e23:  .byte 20 15 f3              ; *** wur a2, UR#0x15 (21) — host-pollable error latch ***
    13e29:  call8  0xa450               ; report sink ("S: NOTIFY"/"sending interrupt"/"…notification")
    13e2c:  retw.n
```

`wur UR#0x15` (`20 15 f3`, the `f3`-major WUR, identical idiom to the
`wur.fcr = 20 e8 f3` the SEQ page anchors) and the `call8 0xa450` egress.

### 4b. The hardware transport — the NOTIFIC instruction-notification queue

The record lands in the per-TPB **NOTIFIC instruction-notification queue** (`notific_10_queue`,
the cayman arch-regs schema). Grounded from the shipped register headers:

- **10 SW notification queues.** The `notific_sw_backpressure` register field mask is `0x3ff`
  (10 bits) — one backpressure bit per SW queue, i.e. `NUM_SW_Q = 10` (`notific_10_queue.h`).
- **Per-queue ring descriptor regs.** Each SW NQ ring carries `base_addr_lo`, `base_addr_hi`,
  `head_ptr` (SW consumer), `tail_ptr` (HW producer), `threshold` (`notific_10_queue.yaml`).

The producer (here the DGE/error logic) emits a 16-B record into a HW input buffer; NOTIFIC
snapshots the 64-b `timestamp`, sets `phase`/overflow bits in the `header`, routes it to a SW NQ
index, coalesces + write-orders, and AXI-writes the 16-B entry to `base + tail`; `tail_ptr`
advances 16 B mod size (ring). The host drains by polling the `phase` bit at `base + head_ptr` and
writing `head_ptr` back (the drain ACK). The ring mechanics are `[HIGH/OBSERVED]` from the
schema; the firmware→NOTIFIC-input write is the §4a egress — `[MED]` edge (desynced).

### 4c. The route — `sw_queue_num3` (errors_NT), shared with the SEQ error path

The cluster `tpb.notific` routing block maps each engine's notif classes to a 4-bit SW queue
number; the `sw_queue_num<N>` fields are present in the shipped `tpb.json` schema (descriptions
name the per-class routes: `…PE sequencer NX generated notifications`, `…explicit notifications`,
`…engine generated notifications`, etc.). The DGE/engine **ERROR** notif routes to
`sw_queue_num3 = errors_NT` — the **same** route the
[SEQ error-handler](../seq/error-handler.md) §7 pins for the SEQ error notification.

> **NOTE — the route is CARRIED, not grounded byte-exact.** The cayman arch-regs
> JSON ships the `sw_queue_num0..5` field *names*, but the
> per-field route *descriptions* are sparse/ciphered in the carved schema (the string-pool cipher
> the corpus uses). The `errors_NT == sw_queue_num3` binding is therefore **`[CARRIED]` from
> CSR-06** (the notification-queue survey) at that report's confidence — exactly as the SEQ
> error-handler page treats it. Both the DGE OOB and the SEQ error land in the **same** errors_NT
> ring; the host distinguishes source by the record's `error_id` and `metadata_hi.block_id`
> (§3c). `[HIGH/INFERRED]` the shared ring (both pages pin `sw_queue_num3`); `[CARRIED]` the
> route name.

### 4d. The structural / descriptor-invalid leg — `HandleIllegalInstr`

The census shows one `call8 0x13f80` (`HandleIllegalInstr`) at `0x103a3`, reachable from the DGE
region. This is **not** the OOB-address path; it is the **structural** descriptor-validity
failure (a malformed `BOUND_CHECK_REG`, an out-of-range shape index, a non-readable wide-offset
register — the `has_valid_*` predicates of §5). A descriptor that fails *structural* validity is
an **illegal instruction**, routed into the SEQ error TU's `HandleIllegalInstr` (FATAL on the SEQ
severity model), distinct from the *content* OOB which is report-and-continue. So the DGE has two
error severities: **content OOB → report-and-continue (notif)**; **structural-invalid descriptor
→ HandleIllegalInstr (SEQ FATAL)**. `[HIGH/OBSERVED]` the call site; the structural-vs-content
split is `[HIGH/INFERRED]` from the validity-predicate set + the FATAL target.

### 4e. The overflow / backpressure on the error ring

If the errors_NT SW NQ is full: a `sw_backpressure` bit stalls the producer (lossless), or an
`ignore_full` policy overwrites at tail (lossy, with `software_queue_overflow` set in the record
header `+3`). A write to a *disabled* queue is dropped and raises a NOTIFIC interrupt. This is the
layer where NOTIFIC's own faults (queue-full / AXI write-error) cross into the SoC errtrig fabric
(§8) — a different, lower layer than the DGE content error. `[HIGH/OBSERVED]` from the schema;
the errtrig crossover is `[HIGH/INFERRED]`.

---

## 5. The GATING — feature flag + per-descriptor override

A notification is dispatched **iff** the engine-level feature flag is set **AND** this
descriptor's direction has `bc_disable_oob_error_notif == 0`. Two independent gates, both
OBSERVED.

### 5a. Engine-level gate — `NEURON_FEATURE_FLAGS` bit 1 (verbatim)

```c
// aws_neuron_isa_xt_general_local_reg_defines.h
// NRT is responsible for updating this to enable/disable features
// Bit 0: DVE perf mode - Sets if performance mode is supported on this particular NEFF
// Bit 1: DGE packet notification - Sets if packet notifications are enabled
// Bit 2: EVT Accel Address provided - …
#define AWS_NEURON_ISA_XT_NEURON_FEATURE_FLAGS        38
```

`NEURON_FEATURE_FLAGS` is **XT local register 38**; **bit 1** globally enables/disables DGE
packet notifications for the whole engine. The host (NRT) owns the bit. When bit 1 is clear,
**no** DGE notification (completion *or* error) is dispatched. `[HIGH/OBSERVED]`

The on-device **read** of reg 38 in the dispatch path is in a FLIX-desynced span (no clean
`rur`/`movi 38` recovered in the DGE region) — the **gate is header-OBSERVED**; the **read
instruction is not byte-recoverable** (`[MED]`).

### 5b. Per-descriptor gate — `bc_disable_oob_error_notif` (compile-verified)

```c
typedef struct NEURON_ISA_TPB_BOUND_CHECK_REG {  /* sizeof == 1 — VERIFIED */
    NEURON_ISA_TPB_REG_NUM bc_reg : 6;            /* bound-register number; bc_reg+1 = high 32 b
                                                     in wide-offset mode */
    uint8_t bc_disable_oob_error_notif : 1;       /* bit 6 — suppress the OOB error notification */
    uint8_t bc_enabled : 1;                       /* bit 7 — 1 = perform the bounds check        */
} NEURON_ISA_PACKED NEURON_ISA_TPB_BOUND_CHECK_REG;
```

Every descriptor carries **two** of these (src + dst). `bc_disable_oob_error_notif` (bit 6)
suppresses the notification for **this** descriptor's direction even when the bounds check itself
runs and fails.

> **CORRECTION — the "suppress the OOB error notification" gloss is derived, not an inline header
> comment.** A prior survey attributes that phrase to a comment on the struct field
> at `aws_neuron_isa_tpb_common.h:709`. The field at line 709
> carries **no inline comment** (`uint8_t bc_disable_oob_error_notif : 1;` followed by
> whitespace). The semantic — "suppress the OOB error notification for this direction" — is the
> **correct** reading, but it is grounded in the **field name + the gating predicate (§5c/§5d)**,
> not an embedded comment. (The field name itself, `bc_disable_oob_error_notif`, is a recovered
> binary-derived symbol — fully citeable.) `[HIGH/OBSERVED]` (struct), `[HIGH/INFERRED]` (the gloss).

### 5c. The indirect-path interaction — the `idx_bound_is_err` policy (predicate verbatim)

```c
typedef struct NEURON_ISA_TPB_DMA_INDIRECT_FLAGS {  /* sizeof == 1 — VERIFIED */
    NEURON_ISA_TPB_INDIRECT_DMA_ADDRESSING_MODE indirect_mode : 2;
    uint8_t                                     idx_bound_is_err : 1;
    uint8_t                                     non_unique_dst_idx : 1;
    NEURON_ISA_TPB_INDIRECT_DIM                 gather_dim : 2;
    NEURON_ISA_TPB_INDIRECT_DIM                 scatter_dim : 2;
} NEURON_ISA_PACKED NEURON_ISA_TPB_DMA_INDIRECT_FLAGS;
```

The validity predicate (a Rust-syntax spec annotation embedded as a comment block in
`aws_neuron_isa_tpb_common.h`, lines 1369–1386, verbatim):

```rust
// fn has_valid_idx_bound_check_reg(bound_check_reg: BoundCheckReg, flags: DmaIndirectFlags ) -> bool {
//       has_valid_idx_bound_check_reg_no_flags(bound_check_reg)
//    && (   flags.idx_bound_is_err == 1
//        || bound_check_reg.bc_disable_oob_error_notif == 0)
// }
```

For an **indirect** (gather/scatter) descriptor, **EITHER** the index-bound must be declared a
hard error (`idx_bound_is_err == 1`) **OR** the OOB notification must remain enabled
(`bc_disable_oob_error_notif == 0`). You may **not** both disable the notification **and** decline
to treat it as an error — the header **forbids a silent index-OOB**. This is the indirect path's
error-vs-notify policy knob.

> **GOTCHA — the predicate is a spec annotation in a `//`-comment, not executable C.** The
> `has_valid_*` functions ship as **commented-out Rust pseudocode** inside the C header (the
> arch-ISA validity spec). They are binary-derived, citeable evidence of the *intended* validity
> rule the firmware enforces, but a reimplementer must encode the rule themselves — there is no
> compiled C function named `has_valid_idx_bound_check_reg` to call.

### 5d. The base validity predicate (for completeness)

```rust
// fn has_valid_bound_check_reg(bound_check_reg: BoundCheckReg, marker: u8 ) -> bool {
//       (   (bound_check_reg.bc_enabled == 0)
//        && (bound_check_reg.bc_reg == 0)
//        && (bound_check_reg.bc_disable_oob_error_notif == 0))
//    || (   (bound_check_reg.bc_enabled == 1)
//        && is_valid_register_read_with_marker(bound_check_reg.bc_reg, marker))
// }
```

A bound-check reg is valid **iff** fully-disabled-and-zero **OR** enabled-with-a-valid register
read. A malformed `BOUND_CHECK_REG` is itself a descriptor-validity error caught **before** emit
(routed to the §4d structural leg, not the §2a OOB leg).

### 5e. The composite dispatch decision

```c
/* the dispatch decision, the consistent reading of the two OBSERVED gates */
dispatch_error_notification =
       NEURON_FEATURE_FLAGS.bit1                         /* engine-level: DGE packet notif on */
    && (failing_descriptor.<dir>.bc_disable_oob_error_notif == 0)   /* per-descriptor suppress */
    /* indirect path: AND NOT silenced by the idx_bound_is_err policy of §5c */ ;
```

Both gates are OBSERVED; the **AND-combination** is the consistent reading of the
`bc_disable_oob_error_notif` suppress semantic + the "DGE packet notification" feature-flag name +
the `"… unless suppressed … Dispatched error notification"` string ordering (§2a). `[HIGH/INFERRED]`
the composite.

---

## 6. Relationship to the SEQ Error-Handler — separate detection, shared delivery

### 6a. The TU-separation proof (the `.rodata` adjacency)

The DGE error strings cluster at VA `0x83105`–`0x8318e` and are **immediately followed** by the
reshape/backend TU's source strings (from `cayman_dram.bin`, VA = off + `0x80000`):

| file off | VA | String |
|---|---|---|
| `0x31b4` | `0x831b4` | `analyze_tensor_reshape` |
| `0x31cb` | `0x831cb` | `dge_reshape.cpp` |
| `0x3529` | `0x83529` | `dge_backend_rtl.cpp` |
| `0x3a55` | `0x83a55` | `error_notifications.cpp` (the **separate, later** SEQ block) |
| `0x3ab9` | `0x83ab9` | `error_handler.cpp` |
| `0x3bb0` | `0x83bb0` | `signal_handler.cpp` |

So the DGE detection + dispatch strings (`0x83105`–`0x8318e`) belong to the **`dge_reshape.cpp` /
`dge_backend_rtl.cpp` translation unit**, *not* `error_notifications.cpp`. The
`error_notifications.cpp` / `error_handler.cpp` / `signal_handler.cpp` strings are a **separate,
later** `.rodata` block (`0x83a55`+, the SEQ Error-Handler TU). `[HIGH/OBSERVED]` — the
adjacency places the DGE strings in the reshape/backend TU. (This matches what the SEQ
error-handler page flagged: the DGE strings are a "different engine TU.")

### 6b. The verdict — separate SOURCE, shared TRANSPORT

| Aspect | DGE OOB path | SEQ Error-Handler path | Shared? |
|---|---|---|---|
| **Detection** | per-descriptor SOFTWARE bounds-check in `dge_reshape`/`dge_backend_rtl`, evaluated BEFORE emit | HW trap / decode-table miss → 6 fault entry points (`HandleBadOpcode`, …) | **No** — distinct sources |
| **Severity model** | report-and-continue (no `0x13e00`) | binary recoverable (FP) vs FATAL-spin (all others) | **No** |
| **Record family** | 16-B `NEURON_ISA_TPB_ERROR_NOTIFICATION` (SEQUENCER class) | same record family via `build_error_record@0x13e40` | **Yes** |
| **Notify primitive** | `call8 0xa2f8` (×4 in DGE region) | `0xa2f8` inside `build_error_record@0x13e71`; `0xa450` report sink | **Yes** |
| **Host channel** | NOTIFIC errors_NT ring (`sw_queue_num3`) | `UR#0x15` + NOTIFIC `sw_queue_num3` errors_NT ring | **Yes** (same ring) |

**Detection is separate, delivery is shared.** The DGE OOB is a descriptor-content validation
that is **not** a HW trap, **not** routed through the SEQ fault entry points
(`HandleBadOpcode 0x13f58` / `HandleIllegalInstr 0x13f80` / `HandleIntDivZero 0x13f34` /
`HandleFPError 0x13eb0` / `signal_handler 0x14014`), and **does not call the FATAL raise wrapper
`0x13e00`** from its dispatch region (census §4a: `0x13e00 ×0`). But it packs the **same** 16-B
record family and egresses through the **same** notify primitive (`0xa2f8` / `0xa450`) into the
**same** errors_NT ring. `[HIGH/OBSERVED]` detection separation (TU adjacency + no-FATAL census);
`[HIGH/INFERRED]` delivery sharing (the `0xa2f8` call + the common errors_NT route).

### 6c. A fourth policy class

The SEQ error-handler's binary recoverable-vs-fatal policy (severity-2 → Setup-Halt → infinite
spin; severity-1 → return, FP only) does **not** apply to the DGE OOB. Nor is the DGE OOB the
SEQ **soft** PC-bounds skip (which only logs a WARNING and never notifies). The DGE OOB is a
**fourth** policy class:

```
SEQ:  { soft-warn-skip  |  recoverable-FP (return)  |  hard-spin (FATAL) }
DGE:  { report-to-runtime + skip-descriptor + continue }   <-- NEW
```

It posts a **structured runtime notification** (unlike the soft PC-bounds warn-only) but does
**not** halt the engine (unlike the hard-spin). `[HIGH/INFERRED]` — the no-FATAL-call is OBSERVED;
the classification follows from it + the bounds-gate-emit semantics. (The *structural* invalid
descriptor of §4d does take the SEQ FATAL `HandleIllegalInstr` leg — a different DGE error class.)

---

## 7. The recovery behaviour — report-and-continue

### 7a. No on-device halt on a DGE content OOB

The DGE dispatch region (`0xf000`–`0x10600`) call census shows **no** call to the
SEQ FATAL raise wrapper `0x13e00` and **no** call to the halt-dispatch `0xa2e0` (both `×0`,
§4a). The terminal SEQ spin (`j 0x13e14`) is **not** on the DGE content-OOB path. For context,
`0x13e00` is called **6×** across the whole IRAM (the SEQ fault entry points) — **0** of them in
the DGE region.

### 7b. Skip-the-descriptor + continue

The bounds check **gates emit** (the [emit page](dge-emit.md): `addr <= limit` is evaluated
per direction *before* issuing the descriptor). On failure: the descriptor is **not** emitted,
the notification is posted (gates permitting), and the DGE **proceeds** to the next descriptor.
The `"NO BACKEND FOUND, doing nothing"` arm is the same model — the DGE explicitly does nothing
(no descriptor emitted), may dispatch the notification, and continues. `[HIGH/INFERRED]` the
skip-and-continue model; the exact post-dispatch control flow is FLIX-desynced (`[MED]`).

> **NOTE — the host-side contract is consistent with report-and-continue.** The host twin
> library's analogous PC-bounds check (`nrtucode.h`, `enable_pc_bounds_check`, recovered string)
> states: *"If the PC is out of bounds, the core will raise an error notification to runtime and
> goes in the error state. … when PC is generated by speculative prefetching, then normal
> execution flow won't be interrupted."* — a raise-to-runtime + soft-state model with a
> speculative-skip carve-out, the same shape as the DGE OOB. `[HIGH/INFERRED]` the alignment
> (the contract is OBSERVED in the host header; the DGE binding to it is reasoned).

---

## 8. Relationship to the errtrig / FIS fabric — distinct layers

A DGE descriptor-content OOB does **not** feed the SoC errtrig `int_cause` directly. Two
**stacked** error fabrics at different layers:

```
  DGE content OOB (this page)            NOTIFIC transport fault (lower layer)
  ─────────────────────────             ────────────────────────────────────
  software validation failure           NOTIFIC AXI write-error / SW-NQ-full /
  on a descriptor being built           queue-disabled drop / wr-buffer-full
        │                                       │
        ▼  16-B ERROR record                    ▼  NOTIFIC interrupt
  errors_NT SW NQ  →  host drain         errtrig int_cause  →  apex  →  Q7/GIC
  (functional/telemetry notification)    (RAS/fabric fault)
```

The DGE content OOB is a **functional/telemetry** notification posted *into* the NOTIFIC queue;
it is not a RAS/fabric fault. The errtrig fabric captures the **transport** faults of the same
NOTIFIC queue (a NOTIFIC AXI write-error, an SW-NQ-full, etc.). So the queue that **delivers** the
DGE error notification can itself raise an errtrig fault **if its transport fails** — but the DGE
content OOB is one layer above, posted into the queue, not into the errtrig. A DGE OOB only
reaches the errtrig **indirectly**, and only if the NOTIFIC write of its own error record fails.
`[HIGH/INFERRED]` (the layering follows from the two schemas; no string ties a DGE content OOB
directly to errtrig — confirming the split); the apex→Q7/GIC hop is `[CARRIED/LOW]` from the
interrupt-fabric survey.

---

## 9. Per-generation presence

### 9a. The emitter (firmware, `rg -c -a` over carved NX_POOL DRAMs)

| GEN | `Failed bounds check` | `Dispatched error notification` | DGE emit present? |
|---|---|---|---|
| SUNDA (v3) | 0 | 0 | NO |
| CAYMAN | 1 | 1 | YES |
| MARIANA (v4) | 1 | 1 | YES |
| MARIANA_PLUS | 1 | 1 | YES |

The DGE error-notification **emitter** tracks the DGE emit path: **CAYMAN-onward**, absent in
SUNDA. `[HIGH/OBSERVED]`

### 9b. The format + gating fields — what is frozen vs per-gen

Census of the four shipped arch-ISA header trees:

| Artifact | SUNDA | CAYMAN | MARIANA | MAVERICK |
|---|---|---|---|---|
| `TPB_ERROR_NOTIFICATION` + `SEQUENCER_FATAL` (record format) | present | present | present | present |
| `TPB_DGE_METADATA_NOTIFICATION` (completion format) | present | present | present | present |
| `BOUND_CHECK_REG {bc_disable_oob_error_notif}` (suppress field) | present | present | present | present |
| `has_valid_idx_bound_check_reg` predicate (the policy) | present | present | present | present |
| `local_reg_defines.h` → `NEURON_FEATURE_FLAGS` reg 38 bit-1 | **absent** | present | present | present |

> **CORRECTION — the *record format and the ISA gating fields* are arch-common (all 4); the
> *engine feature-flag definition* is CAYMAN-onward.** A prior survey says the
> `NEURON_FEATURE_FLAGS` bit-1 definition is "present even in the sunda header." In fact
> **SUNDA's ISA tree has no `local_reg_defines.h`** at all (the only file in its
> `common/` is `aws_neuron_isa_notification.h`), so `NEURON_FEATURE_FLAGS` is **not** defined for
> SUNDA. What *is* arch-common in SUNDA is the **record format** (`TPB_ERROR_NOTIFICATION`,
> `SEQUENCER_FATAL`, `TPB_DGE_METADATA_NOTIFICATION`) **and** the **gating fields**
> (`BOUND_CHECK_REG.bc_disable_oob_error_notif` `sizeof==1`, the `has_valid_idx_bound_check_reg`
> predicate) — all present in SUNDA's headers. So: the FORMAT and the ISA-level suppress field
> are frozen across all four; the **engine-level feature flag (reg 38) and the EMITTER are
> CAYMAN-onward** — both tracking the DGE emit path's arrival.
>
> *(SUNDA's notification header is additionally name-ciphered — type names render as
> `NEURON_ISA_…_ln` — a string-pool obfuscation artifact; the values still decode, e.g.
> `…ERROR_TYPE_…ln = 0x0a` matches `SEQUENCER_FATAL`.)*

### 9c. The NOTIFIC delivery

The `notific_*_queue` block + the 16-B record + the errors_NT route ship under all arch dirs; the
10-queue variant (`notific_10_queue`) is the production instantiation. `[CARRIED]` from the
notification-queue survey (CSR-06); the 10-queue `NUM_SW_Q` + ring regs are grounded in §4b.

---

## 10. Confidence ledger

**HIGH / OBSERVED** (direct disasm / byte read / compiled header):

- Carve reproduced; all 8 image sha256 prefixes match the FW-23/09 anchors (`CAYMAN_NX_POOL`
  IRAM `8e4412b9…` / DRAM `7bdf6ed7…`).
- The DGE error-string corpus byte-exact (`0x83105 / 0x8312f / 0x83157 / 0x83173 / 0x8318e`; the
  `bounds:(…)` substring at `0x830c2`; the shared SEQ-egress strings `0x82286 / 0x82257 /
  0x8226d`); the per-gen presence (SUNDA 0/0, CAYMAN/MARIANA/MARIANA_PLUS 1/1).
- The TU adjacency placing the DGE strings in `dge_reshape.cpp` / `dge_backend_rtl.cpp`, with
  `error_notifications.cpp` the separate later SEQ block at `0x83a55`.
- The 16-B `NEURON_ISA_TPB_ERROR_NOTIFICATION` + offsets (`error_id@0`/`metadata_lo@1`/
  `header@3`/`metadata_hi@4`/`timestamp@8`) + `sizeof==16`; `NOTIFICATION_HEADER` bitfield;
  `NOTIFICATION_TYPE {ERROR=0x03, DMA=0x02, TPB_ERROR=0x1f}`; `TPB_ERROR_TYPE
  {…SEQUENCER_NONFATAL=0x09, SEQUENCER_FATAL=0x0a}`; `SEQUENCER_GENERIC_ERROR_METADATA_HI
  {block_id:4, program_counter:12, info:8}`; `TPB_DGE_METADATA_NOTIFICATION sizeof==16` +
  `METADATA_TYPE_DGE=0x1` (all gcc compile-verified).
- `BOUND_CHECK_REG {bc_reg:6, bc_disable_oob_error_notif:1, bc_enabled:1}` `sizeof==1`;
  `DMA_INDIRECT_FLAGS {idx_bound_is_err}` `sizeof==1`; the `has_valid_idx_bound_check_reg` /
  `has_valid_bound_check_reg` predicates verbatim (header lines 1369–1386); the
  `NEURON_FEATURE_FLAGS` local-reg 38 bit-1 "DGE packet notification" definition.
- The DGE dispatch-region call census: `0xa2f8 ×4`, `0x18b84 ×12`, `0x18bfc ×9`, `0x13f80 ×1`
  (`@0x103a3`), **`0x13e00 ×0`**, `0xa2e0 ×0`; `0x13e00 ×6` total in IRAM, 0 in the DGE region.
- The shared SEQ machinery: `build_error_record@0x13e40 → call8 0xa2f8 @0x13e71`;
  `raise_error@0x13e18` `wur UR#0x15` (`20 15 f3`) + `call8 0xa450`; `get_block_id` reads
  `DRAM[0x85f38]`.
- The const16/l32r absence proof for `0x3105`/`0x318e` (0 hits each) vs the const16-clean
  `0x312f` (4)/`0x3157` (2).
- The per-arch header census of the format + gating fields + feature-flag (§9b table).
- `notific_10_queue`: `NUM_SW_Q=10` (`sw_backpressure` mask `0x3ff`) + ring-descriptor regs.

**HIGH / INFERRED:** the AND-combination of the two gates (§5e); the separate-detection /
shared-delivery verdict (§6 — TU + no-FATAL OBSERVED, the `0xa2f8` share OBSERVED); the shared
errors_NT ring for DGE + SEQ (§4c — both pages pin `sw_queue_num3`); the report-and-continue
recovery model (§7); the content-error-vs-transport-error layering (§8); the structural-vs-content
DGE error split (§4d); the shared-flag-gates-both reading (§3d/§5a).

**MED:** the exact `error_id`/`notific_type` the DGE OOB writes (§3c — body desynced); the
on-device reg-38 read site (§5a — desynced); the precise NOTIFIC-input-buffer / external-register
write address (§4a — `0xa2f8`/`0xa450` bodies FLIX-desynced); the exact post-dispatch control
flow (§7b).

**CARRIED:** the `errors_NT == sw_queue_num3` route name (§4c, from CSR-06); the apex→Q7/GIC hop
for a NOTIFIC-transport fault (§8, from the interrupt-fabric survey, `[LOW]`).

---

## 11. Cross-references

- **[DGE Descriptor Emit](dge-emit.md)** — the per-descriptor src/dst bounds check that is the
  primary error TRIGGER; this page picks up at the detected OOB.
- **[SEQ Error-Handler / Fault Reporting](../seq/error-handler.md)** — the committed SEQ TU whose
  `build_error_record@0x13e40` / `raise_error@0x13e18` (`wur UR#0x15` + `call8 0xa450`) /
  `0xa2f8` notify primitive the DGE delivery **shares**, and against which §6 reconciles the
  separate-detection / shared-delivery split.
- **[DGE 3-Backend Selector](dge-backend-selector.md)** — the `NO BACKEND FOUND` arm (one of the
  §2a detection sources) and the `bounds`/error-notif log lines; its per-gen table is the
  emitter-presence anchor (§9a).
- **[DGE Setup + Context Init](dge-setup.md)** — the carve, addressing model, and the
  `dge_decode_fast` → backend-select spine upstream of this page.
- **NOTIFIC instruction-notification queue** (`../../control/csr/notific-queue.md`) — **Part 13.**
  Decodes the `notific_10_queue` block (the SW NQ ring regs, `errors_NT` route,
  coalescing/overflow) this page delivers into. *(The `NUM_SW_Q=10` + ring regs are grounded here
  §4b, the `errors_NT` route is CARRIED §4c.)*
- **Device→host notification fabric** (`../../control/interrupt/device-host-notification.md`) —
  **Part 13.** Decodes the firmware→host notification transport (the `0xa2f8`/`0xa450` egress →
  NOTIFIC → MSI-X chain) and the errtrig crossover of §8.
- **[The Confidence & Walls Model](../../reference/confidence-model.md)** — the normative
  definition of the `[CONF/PROV]` tags and the FLIX-desync MED ceiling cited throughout.
