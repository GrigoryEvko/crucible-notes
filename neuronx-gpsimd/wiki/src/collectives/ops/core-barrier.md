# PSEUDO_CORE_BARRIER (`0xD8`)

`NEURON_ISA_TPB_PSEUDO_CORE_BARRIER_STRUCT`, opcode
**`NEURON_ISA_TPB_OPCODE_PSEUDO_CORE_BARRIER = 0xd8`**
(`aws_neuron_isa_tpb_common.h:286`). This is the **cross-core / intra-VNC**
barrier — the compiler-emitted placeholder that synchronizes the physical
NeuronCores ("pcores") fused into one Virtual NeuronCore (VNC). It is the first
of the three-barrier trilogy: **CORE `0xD8`** (this page) /
[SYNC `0xD5`](sync-barrier.md) / [DMA `0xC3`](dma-barrier.md).

Every fact on this page is grounded in the shipped, redistributable C ISA
headers (`aws-neuronx-gpsimd-customop-lib_0.21.2.0`), the per-arch
`instruction_mapping.json`, the host ucode decoder `libnrtucode_internal.so`,
and the committed sibling pages cited inline. The struct layout below is
**compile-verified** (`gcc` `sizeof`/`offsetof` against the real shipped
headers) on **all four** arch variants.

> **TL;DR.** `0xD8` is a 64-byte **pseudo** instruction whose doc comment is
> verbatim *"Neuron ISA / TPB / vnc barrier — Pseudo instruction that translates
> to a barrier used to sync pcores within a VNC"*
> (`aws_neuron_isa_tpb_pseudo_core_barrier.h:13`). It carries exactly three
> meaningful fields past the common header — an inline `events` arrive/wait
> block, a barrier `id` (u32 @12), and a rendezvous `semaphore` (u32 @16) — and
> is lowered by NRT/KRT into concrete `EVENT_SEMAPHORE`/`POLL_SEM` hardware ops
> over the 256-entry `EVT_SEM` semaphore array before the engine sequencer ever
> sees it. There is **no** participant bitmask, **no** count field, and **no**
> timeout: the participant model and the threshold ride entirely on the
> `events` block's `WAIT_FOR_SEM_GE_IMM` + `semaphore_value`.

---

## 1. Struct layout — compile-verified, byte-identical on 4 archs

`aws_neuron_isa_tpb_pseudo_core_barrier.h:19-25` (cayman / NC-v3 copy; the other
three arch copies are byte-identical in the struct and the doc comment — see
§1.1):

```c
typedef struct NEURON_ISA_TPB_PSEUDO_CORE_BARRIER_STRUCT {
    NEURON_ISA_TPB_HEADER header;        // 4    ( 0 -  3)
    NEURON_ISA_TPB_EVENTS events;        // 8    ( 4 - 11)
    uint32_t              id;            // 4    (12 - 15)
    uint32_t              semaphore;     // 4    (16 - 19)
    uint8_t               reserved1[44]; // 44   (20 - 63)
} NEURON_ISA_TPB_PSEUDO_CORE_BARRIER_STRUCT;
```

| off | size | field | C type | meaning | tag |
|---|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode(1B packed)=0xD8, inst_word_len, debug_cmd, debug_hint}` (§2). | HIGH / OBSERVED |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | inline `EVT_SEM` wait+update block `{wait_mode(1B), wait_idx(1B), update_mode(1B), update_idx(1B), semaphore_value(4B)}` — the arrive/wait primitive (§3). | HIGH / OBSERVED |
| 12 | 4 | `id` | `uint32_t` | barrier **identifier** (which barrier instance). No assert constrains its range (§4). | HIGH layout / MED meaning |
| 16 | 4 | `semaphore` | `uint32_t` | the HW **semaphore** index/handle the participating pcores rendezvous on (§5). | HIGH layout / MED meaning |
| 20 | 44 | `reserved1` | `uint8_t[44]` | padding to 64 B; emit `0`, ignore on decode. | HIGH / OBSERVED |
| **64** | | | | one TPB instruction word. | HIGH / OBSERVED |

Compile-verified this session (`gcc`, real shipped header `#include`d):

```
NEURON_ISA_TPB_HEADER                        sizeof=4
NEURON_ISA_TPB_EVENTS                        sizeof=8
NEURON_ISA_TPB_PSEUDO_CORE_BARRIER_STRUCT    sizeof=64
  .header off=0 size=4   .events off=4 size=8
  .id     off=12 size=4  .semaphore off=16 size=4  .reserved1 off=20 size=44
```

`header.opcode` is a `NEURON_ISA_TPB_OPCODE` carrying
`NEURON_ISA_PACKED = __attribute__((__packed__))`
(`aws_neuron_isa_tpb_common.h:8`, `:305`) so it occupies exactly **1 byte**
(`sizeof(NEURON_ISA_TPB_OPCODE)==1`, verified). That is what makes the 4-byte
header (and the byte positions above) exact. **[HIGH / OBSERVED]**

### 1.1. The four arch copies — what actually differs

A diff of the `pseudo_core_barrier.h` file across the four arch packages found
**exactly one** differing line — the version tag in the copyright comment:

| arch dir | NC version (header L3) | CORE struct | tag |
|---|---|---|---|
| `neuron_sunda_arch_isa` | `ISA header for NC-v2` | `sizeof=64, id@12, sem@16, op=0xd8` | HIGH / OBSERVED (compile) |
| `neuron_cayman_arch_isa` | `ISA header for NC-v3` | `sizeof=64, id@12, sem@16, op=0xd8` | HIGH / OBSERVED (compile) |
| `neuron_mariana_arch_isa` | `ISA header for NC-v4` | `sizeof=64, id@12, sem@16, op=0xd8` | HIGH / OBSERVED (compile) |
| `neuron_maverick_arch_isa` | `ISA header for NC-v5` | `sizeof=64, id@12, sem@16, op=0xd8` | HIGH / OBSERVED (compile) |

The struct body, every field offset, the opcode constant, and the doc comment
(*"used to sync pcores within a VNC"*) are byte-identical across all four; only
the `NC-vN` tag changes. CAYMAN = **NC-v3**.

> **CORRECTION (vs. an earlier survey note that flagged MAVERICK/v5 interiors as
> header-OBSERVED-only).** For *this* struct the maverick (v5) header compiles to
> the identical 64-byte / `id@12` / `sem@16` layout as v2–v4 — it is
> **compile-OBSERVED**, not merely header-observed. The byte-grounded claim holds
> on all four ISA generations. (Other maverick-only structs may still warrant the
> v5-INFERRED flag; this one does not.)

### 1.2. INST_UNION binding + mapping

`aws_neuron_isa_tpb_util.h:60` places `0xD8` in the universal instruction union:

```c
NEURON_ISA_TPB_PSEUDO_CORE_BARRIER_STRUCT pseudo_core_barrier;
```

and `instruction_mapping.json:102-104` binds struct → opcode (identical token in
all four arch JSONs):

```json
"NEURON_ISA_TPB_PSEUDO_CORE_BARRIER_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_PSEUDO_CORE_BARRIER"
]
```

> **NOTE — the mapping token matches the enum exactly.** Unlike the DMA barrier
> (§7, where the mapping JSON references a non-existent
> `...OPCODE_PSEUDO_DMA_BARRIER` token while the enum spells it
> `...DMABARRIER`), CORE_BARRIER's mapping token
> `NEURON_ISA_TPB_OPCODE_PSEUDO_CORE_BARRIER` is exactly the enum constant at
> `aws_neuron_isa_tpb_common.h:286`. No naming anomaly here.

### 1.3. No structural validator

`aws_neuron_isa_tpb_assert.h:12648` is a bare section marker
`//* pseudo_core_barrier_assert.h` immediately followed by
`//* pseudo_cur_processing_rank_id_assert.h:12651` — **no body**. There is no
`is_valid_*_core_barrier` predicate; the only validity check is set membership:
`0xD8` is in `is_valid_enum_opcode` (`aws_neuron_isa_tpb_enums.h:328`). Like
[TriggerCollective `0xC8`](trigger-collective.md) and the rank-id pseudo-op,
`0xD8` is **validated and lowered by the runtime, not by the static ISA assert
layer**. **[HIGH / OBSERVED]**

### 1.4. Example 64-byte image (layout-illustrative)

Field VALUES below are illustrative of a plausible counted-barrier encoding; the
**byte positions** are the observed fact. opcode at `[0]`, `events` at `[4..11]`,
`id` at `[12..15]`, `semaphore` at `[16..19]`:

```
events = { wait_mode = WAIT_FOR_SEM_GE_IMM (0x05), wait_idx = 7,
           update_mode = SEM_INC_COMPLETE (0x13), update_idx = 7,
           semaphore_value = 4 },   id = 0x2A,   semaphore = 7

byte:  d8 40 00 00  05 07 13 07  04 00 00 00  2a 00 00 00
       07 00 00 00  00 ... (zero-filled through byte 63)
       └opcode      └events──────────────────┘└id────────┘
                    └semaphore at [16..19]
```

> **GOTCHA — `inst_word_len`.** `header.inst_word_len`
> (`aws_neuron_isa_tpb_common.h:413`, the second header byte, `0x40` above) is the
> instruction word length, **not** an opcode-class field. Do not confuse it with
> `semaphore_value` or `id`. All TPB instruction words in this ISA are 64 bytes.

---

## 2. Base field types (`aws_neuron_isa_tpb_common.h`)

`NEURON_ISA_TPB_HEADER` (4 B, L411-416):

```c
typedef struct NEURON_ISA_TPB_HEADER {
    NEURON_ISA_TPB_OPCODE opcode;        // 1 B packed
    uint8_t               inst_word_len;
    uint8_t               debug_cmd;
    uint8_t               debug_hint;
} NEURON_ISA_TPB_HEADER;                 // sizeof==4
```

`NEURON_ISA_TPB_EVENTS` (8 B, L418-424) — the generic per-instruction
arrive/wait block every TPB instruction can carry (identical to the one in
[TriggerCollective](trigger-collective.md) and the
[SB2SB remote-copy](../../firmware/kernels/sb2sb-remote-copy.md) `events` field):

```c
typedef struct NEURON_ISA_TPB_EVENTS {
    NEURON_ISA_TPB_WAIT_MODE   wait_mode;        // +0  1 B packed
    uint8_t                    wait_idx;         // +1  EVT_SEM index 0..255
    NEURON_ISA_TPB_UPDATE_MODE update_mode;      // +2  1 B packed
    uint8_t                    update_idx;       // +3  EVT_SEM index 0..255
    uint32_t                   semaphore_value;  // +4  imm value / GE target
} NEURON_ISA_TPB_EVENTS;                         // sizeof==8
```

`sizeof(NEURON_ISA_TPB_WAIT_MODE)==1` and `sizeof(NEURON_ISA_TPB_UPDATE_MODE)==1`
(both packed, verified) — so the 4 single-byte mode/idx fields pack tightly
before the u32. **[HIGH / OBSERVED]**

The 8-bit `wait_idx`/`update_idx` (0..255) index **exactly** the 256-entry
`EVT_SEM` semaphore array (`2^8 = 256`, §5). The u32 `semaphore_value` matches
the 32-bit `EVT_SEM` semaphore counter width. The index→256-array and
width→32-bit facts are OBSERVED on both sides; the binding is INFERRED-STRONG
(same reconciliation TriggerCollective uses). **[HIGH layout / MED binding]**

> **NOTE — single shared value vs. the extended block.** CORE_BARRIER uses the
> 8-byte `NEURON_ISA_TPB_EVENTS`, whose **single** `semaphore_value` serves as
> *both* the wait GE-target and (if used) the update immediate. The ISA also
> defines `NEURON_ISA_TPB_EVENTS_EXTENDED` (`:432`, 12 B) with separate
> `sem_wait_value` / `sem_update_value` — but CORE_BARRIER does **not** use it.
> A barrier wants one threshold, so the plain block suffices.

---

## 3. The arrive/wait protocol — the `events` block as the primitive

The barrier is a textbook **counted (sense-reversing) semaphore barrier** over a
single shared HW semaphore. The relevant enum values are all OBSERVED, packed
1-byte, in `aws_neuron_isa_tpb_common.h`:

**WAIT side** — `NEURON_ISA_TPB_WAIT_MODE` (L313-330):

| value | name | role |
|---|---|---|
| `0x1` / `0x2` / `0x3` / `0x4` | `WAIT_FOR_SEM_EQ/LT/LE/GT_IMM` | other compares |
| **`0x5`** | **`WAIT_FOR_SEM_GE_IMM`** | **the GE-compare a counted barrier waits on** |
| `0x81`..`0x85` | `..._EQ/LT/LE/GT/GE_REG` | register-operand forms |
| `0x6` / `0x7` / `0xe` / `0xf` | `WAIT_FOR_EVT_*` | event (not semaphore) waits |
| `0xff` | `INVALID` | |

**ARRIVE side** — `NEURON_ISA_TPB_UPDATE_MODE` (L347-370):

| value | name | role |
|---|---|---|
| **`0x3`** | **`SEM_INC_READ`** | arrive = increment (fires after reads-done) |
| **`0x13`** | **`SEM_INC_COMPLETE`** | arrive = increment (fires on full completion) |
| `0x4` / `0x14` | `SEM_DEC_READ` / `SEM_DEC_COMPLETE` | the decrement / reset leg |
| `0x5` / `0x15` | `SEM_ADD_IMM_READ` / `_COMPLETE` | add-immediate |
| `0x7` / `0x17` | `SEM_SUB_IMM_READ` / `_COMPLETE` | sub-immediate |
| `0x9` / `0x19` | `SEM_WR_IMM_READ` / `_COMPLETE` | overwrite (barrier reset/init) |
| `0xff` | `INVALID` | |

The `_READ` vs `_COMPLETE` suffix selects whether the update fires after the
instruction's reads are done versus when the instruction fully completes
(common.h `UpdateMode` comment). **[HIGH / OBSERVED]**

The counted barrier built from these is:

```c
/* Counted barrier on shared semaphore S, participant count N.
 * S = events.update_idx / cb->semaphore (the resolved EVT_SEM index, §5).
 * N = events.semaphore_value (the participant count / GE target). */

/* ARRIVE — each pcore, once, on reaching the barrier point: */
events.update_mode = SEM_INC_COMPLETE (0x13);   /* atomic S += 1 (HW RMW) */
events.update_idx  = S;

/* WAIT — each pcore spins until everyone has arrived: */
events.wait_mode      = WAIT_FOR_SEM_GE_IMM (0x05);  /* block while S < N */
events.wait_idx       = S;
events.semaphore_value = N;                          /* the GE threshold */

/* (optional) RESET for reuse — decrement back to 0: */
events.update_mode = SEM_DEC_COMPLETE (0x14);   /* the "and_dec" half */
```

Every participant `INC`s one shared counter, then all spin on
`WAIT_FOR_SEM_GE_IMM` until the counter reaches `N`, then proceed. The struct
exposes precisely the three knobs this needs: the shared semaphore
(`semaphore` / `events` idx), the arrive op (`update_mode = SEM_INC`), and the
wait+threshold (`wait_mode = GE` + `semaphore_value = N`). **[enum values +
EVT_SEM atomic-RMW OBSERVED; the CORE_BARRIER → this INC/GE pair binding is the
architectural reading, INFERRED-STRONG — corroborated by the NCFW
`add_semaphore_inc` / `wait_ge_and_dec` pair, §6.]**

### 3.1. Mapping onto the `EVT_SEM` hardware windows

`EVT_SEM` exposes **256** hardware semaphores through four operation-aliased,
4-byte-stride windows over the *same* physical 32-bit counters
(`csrs/tpb/tpb_events_semaphores_axi.json`, read byte-exact in the committed
[RDMA cross-die page](../../dma/rdma-cross-die.md) and the
[SB2SB kernel page](../../firmware/kernels/sb2sb-remote-copy.md)):

| window symbol | `AddressOffset` | access | role for CORE_BARRIER |
|---|---|---|---|
| `tpb_semaphores_read` | `0x1000` | RO | the `WAIT_FOR_SEM_GE` poll reads here |
| `tpb_semaphores_set` | `0x1400` | WO | barrier reset / init (overwrite to 0) |
| `tpb_semaphores_inc` | **`0x1800`** | WO atomic += | the **ARRIVE** (`update_mode = SEM_INC`) writes here |
| `tpb_semaphores_dec` | `0x1C00` | WO atomic -= | the `and_dec` reset (`update_mode = SEM_DEC`) writes here |

`ArraySize 256`, `BundleSizeInBytes 0x4` (4-B stride). So the lowered
CORE_BARRIER's arrive is a write to `EVT_SEM +0x1800 + i*4`; its wait is a poll
of `EVT_SEM +0x1000 + i*4` for `>= N`; its optional reset writes
`EVT_SEM +0x1C00 + i*4` — where `i` is the resolved `semaphore` index ∈ 0..255.
**[windows OBSERVED HIGH (CSR JSON + two committed sibling pages); the
window-selection by CORE_BARRIER's update/wait modes is INFERRED-STRONG — the op
NAMES and the 256 / 32-bit geometry match exactly, but no header prints the
literal `+offset` against `0xD8`.]**

---

## 4. Pseudo-op class — why the consumer side, who lowers it

`0xD8 = 0b1101_1000`. Its upper three bits are `0b110`, placing it in the
`0xC0..0xDF` pseudo range. The OPCODE-enum FIXME comment
(`aws_neuron_isa_tpb_common.h:263`) states the rule verbatim:

> *"currently NRT relies on the fact that all pseudo instructions have upper
> three bits of the opcode equal to 0b110. Pseudo instructions are generated by
> compiler and translated into non-pseudo HW instructions by NRT."*

So CORE_BARRIER is the **consumer-side marker** the **graph compiler** emits to
bracket a cross-core sync point; the **runtime (NRT/KRT)** rewrites it into
concrete HW instructions before dispatch. The natural lowering targets are the
two real HW semaphore opcodes present in the same enum:

* `NEURON_ISA_TPB_OPCODE_EVENT_SEMAPHORE = 0xa0` (`:240`) — the HW semaphore
  arrive/wait instruction.
* `NEURON_ISA_TPB_OPCODE_POLL_SEM = 0xb3` (`:255`) — the TOP_SP (engine `#5`,
  `NEURON_ISA_TPB_NEURON_ENGINE_TOP_SP = 5`, `:145`) poll accelerator.

**[opcode-class + "replaced by runtime" OBSERVED HIGH; the specific 1:1 lowering
to `0xa0`/`0xb3` is INFERRED MED — those are the only HW sema/poll opcodes, and
the expansion list is not in the shipped headers.]**

> **GOTCHA — two different "pseudo" predicates.** `0xD8` is a member of
> `is_valid_enum_opcode` (`enums.h:328`) — a valid TPB opcode — but it is **not**
> a member of `is_valid_enum_pseudo_opcode` (`enums.h:352`), which is a *different,
> tiny* `pseudo_opcode` enum = `{INVALID, PSEUDO_EXIT_EXECUTION,
> PSEUDO_LIBRARY_RELOAD_INDEX}` (program-control markers, unrelated to the
> `0xCx/0xDx` pseudo *instruction* opcodes). Don't conflate the two namespaces.

---

## 5. The participant model — VNC / pcore / LNC

The doc comment scopes it precisely: *"sync pcores within a VNC"*. Decoding the
vocabulary from the shipped headers:

* **pcore** — a *physical* NeuronCore (one of NC0..NC7 on a Cayman package).
* **VNC** — a *virtual* NeuronCore: a **group of pcores** presented as one
  logical NeuronCore. The grouping is `NEURON_ISA_TPB_LNC_SIZE_FMT`
  (`aws_neuron_isa_tpb_common.h:815-820`, the same enum the SB2SB iDMA collective
  uses), with the NC↔group map verbatim in the header comments:

| enum | value | grouping (header comment) | pcores/VNC |
|---|---|---|---|
| `LNC1` | 0 | "NC copies to itself. Used for self-test only." | 1 |
| `LNC2` | 1 | "NC0-NC1, NC2-NC3, NC4-NC5, NC6-NC7" | 2 |
| `LNC4` | 2 | "NC0-NC3, NC4-NC7" | 4 |
| `LNC8` | 3 | "NC0-7" | 8 |

A VNC of LNC-size `S` contains `S` physical NeuronCores, and CORE_BARRIER's
participant count `N` (`events.semaphore_value`) is the number of pcores in that
VNC that must arrive. **[LNC enum OBSERVED HIGH; VNC == an LNC-grouped pcore set
INFERRED MED — the doc says "VNC", the LNC enum defines the grouping, and the two
are the same logical-NeuronCore abstraction.]**

**Who the participants are NOT:** they are **not** the 8 SPMD custom-op Q7 cores
([multicore-spmd](../../abi/multicore-spmd.md),
[spmd-teardown](../../runtime/spmd-teardown.md)), and **not** the cross-package
collective ranks (the `group_id`/`channel_id` replica groups of
[TriggerCollective](trigger-collective.md)). Those ranks are synchronized by the
coarser NCFW device barrier (§6), not by CORE_BARRIER. **[INFERRED MED — the doc
comment scopes it "within a VNC"; the rank-level scope belongs to the NCFW
layer.]**

> **NOTE — `NEURON_CORE_VERSION` is unrelated.**
> `NEURON_ISA_TPB_NEURON_CORE_VERSION` (`:131`) defines only `V2 = 2` / `V3 = 3`
> ("V1 ISA is maintained in separate package"). That is the **ISA architecture
> generation**, not a VNC count and not the participant model. Flagged here to
> prevent conflation with the LNC grouping.

---

## 6. Reconciliation with the NCFW device barrier

CORE_BARRIER (ISA, compiler-emitted) and the **NCFW device barrier** (firmware-
resident counted-semaphore descriptor) are the two ends of the *same* mechanism
— the consumer placeholder and the runtime descriptor it parameterizes. The op
pair is byte-exact on both sides:

| CORE_BARRIER field | NCFW device-barrier analogue | shared HW primitive |
|---|---|---|
| `semaphore` (u32 @16) | `barrier_sema[k][i]` (soc_addr CSR ptr per step/peer) | the `EVT_SEM` semaphore |
| `events.update_mode = SEM_INC` | `add_semaphore_inc` (ucode op `0x10A0` subop `21`) | ARRIVE = INC (`+0x1800`) |
| `events.wait_mode = WAIT_FOR_SEM_GE_IMM` | `add_semaphore_wait_ge_and_dec` (`0x10A0` subop `20`) — poll READ `>=` target, then DEC | WAIT = READ-GE (`+0x1000`), reset = DEC (`+0x1C00`) |
| `events.semaphore_value` | `target_sema_val[k][i]` (u32 GE target) | threshold `N` = participants |
| `id` (u32 @12) | *(no direct NCFW field — see below)* | barrier identifier |

The firmware ARRIVE/WAIT op pair (`add_semaphore_inc 0x10A0/21` /
`add_semaphore_wait_ge_and_dec 0x10A0/20`) selects exactly the same semantics
CORE_BARRIER's `events` block encodes (`SEM_INC` + `WAIT_FOR_SEM_GE_IMM`), both
targeting the same `EVT_SEM` INC/READ/DEC windows, both 32-bit-wide
(`semaphore_value u32 == target_sema_val u32 == EVT_SEM value [31:0]`). **[op
names + width match OBSERVED HIGH across the CORE_BARRIER enum + the NCFW device
barrier + the `EVT_SEM` CSR; "CORE_BARRIER is the consumer of THIS device-barrier
struct" binding is INFERRED MED.]**

> **IMPORTANT — two couplings, different scopes; do not conflate:**
>
> * **(a) CORE_BARRIER is the *intra-VNC* (cross-pcore) barrier** — it
>   synchronizes the physical cores of **one** logical NeuronCore (its own doc:
>   "sync pcores within a VNC"). Fine-grain, compiler-dropped inline.
> * **(b) The NCFW device barrier is the broader *cross-engine + cross-rank/die*
>   collective fence** — a multi-step peer-fanout barrier that brackets
>   ring/mesh phases (see the [NCFW dispatch loop](../ncfw/main-dispatch-loop.md)
>   and the [unified collective architecture](architecture-synthesis.md)).
>   Coarse-grain, firmware-run.
>
> Both ride the **same** underlying HW primitive (`EVT_SEM` INC + WAIT-GE over
> the 256 semaphores) and the **same** op pair, at **different** scopes. The
> runtime can realize a CORE_BARRIER with the same `add_semaphore_inc` /
> `wait_ge_and_dec` calls, just over the VNC's pcores rather than the
> collective's ranks. **[scopes OBSERVED in the respective doc comments/structs;
> "same primitive, different scope" INFERRED MED.]**

**Absent-field cross-check.** The NCFW device-barrier struct carries **no**
`barrier_id`, **no** participant count, **no** timeout, **no** phase. CORE_BARRIER,
by contrast, *does* carry an `id` (u32 @12). So the **barrier-id lives on the
ISA/compiler side** (which barrier this instruction is), while cross-rank
participant identity lives firmware-side (implicitly, via soc_addr high bits).
**Neither side carries a timeout** (§8). **[presence of `id` in CORE_BARRIER and
its absence in the NCFW struct both OBSERVED; "id lives compiler-side" INFERRED
MED.]**

---

## 7. The three barrier types — boundaries (CORE vs SYNC vs DMA)

All three are 64-byte **pseudo**-ops carrying the common header + the 8-byte
`events` block; they differ in barrier-specific payload and scope. Field
inventories compile-verified this session (all `sizeof==64`):

| opcode | pseudo-op | barrier-specific fields | scope (verbatim doc comment) |
|---|---|---|---|
| **`0xD8`** | `PSEUDO_CORE_BARRIER` | `id` u32 @12 + `semaphore` u32 @16 + `reserved1[44]` | *"sync pcores within a VNC"* — **CROSS-CORE** (intra-VNC) |
| `0xD5` | `PSEUDO_SYNC_BARRIER` | none — `events` only + `reserved0[52]` @12 | *"all-engine-barrier for loop-on-chip flow"* — **CROSS-ENGINE** (within one core) |
| `0xC3` | `PSEUDO_DMABARRIER` | `barrier_metadata` u32 @12 + `reserved0[48]` @16 | *"DmaBarrier assures that writes to SBUF are completed ... replaced by Nop(NUM_ROWS), by KRT"* — **DMA-ORDERING** (SBUF staggered-write fence) |

Compile-verified offsets: SYNC `events@4` then `reserved0@12 size=52`; DMA
`barrier_metadata@12 size=4` then `reserved0@16 size=48`. **[HIGH / OBSERVED]**

The boundaries, from the verbatim doc comments:

* **CORE_BARRIER (`0xD8`)** — synchronizes **physical cores** (pcores) of one
  VNC. The *widest* scope of the three: it spans **multiple NeuronCores**.
  Carries a barrier `id` + a rendezvous `semaphore` (the counted-semaphore
  handshake of §3). This is *the* cross-core barrier.
* **[SYNC_BARRIER (`0xD5`)](sync-barrier.md)** — *"all-engine-barrier for
  loop-on-chip flow"*: aligns all the **engines** (PE/ACT/POOL/DVE/SP/TOP_SP —
  `NEURON_ISA_TPB_NEURON_ENGINE`, `:139-146`) **within one core** at a loop
  boundary. No `id`/`semaphore` field; relies purely on the inline `events`
  block. **Narrower** than CORE_BARRIER (one core's engines, not multiple cores).
* **[DMABARRIER (`0xC3`)](dma-barrier.md)** — **not a rendezvous barrier at
  all**: a **DMA-ordering fence**. TPB SBUF reads/writes are *staggered* across
  partitions; a `WR_DONE` event actually means only partition 0 finished. A DMA
  could read a high partition before partition 0 and break the staggering.
  `DMABARRIER` inserted before a `DmaTrigger` guarantees that race cannot happen.
  KRT lowers it to `Nop(NUM_ROWS)`. Carries `barrier_metadata`, no semaphore.
  This is a **memory-consistency fence**, not a multi-actor counted barrier.

One-line taxonomy:

```
0xD8 CORE_BARRIER = counted-semaphore barrier over the PCORES of a VNC (cross-core).
0xD5 SYNC_BARRIER = all-ENGINE fence within a single core (loop-on-chip).
0xC3 DMABARRIER   = SBUF staggered-write DMA-ordering Nop fence (no rendezvous).
```

> **QUIRK — DMA-barrier enum/mapping naming mismatch (DMA only; CORE is clean).**
> The OPCODE enum spells it `PSEUDO_DMABARRIER` (no underscore, `0xc3`,
> `common.h:265`), but `instruction_mapping.json:108-110` binds the struct
> `PSEUDO_DMA_BARRIER_STRUCT` → a token `...OPCODE_PSEUDO_DMA_BARRIER` (with
> underscore) that **does not exist** as an enum constant (only `...DMABARRIER`
> does). The struct/util.h member also use the underscored `pseudo_dma_barrier`.
> This cosmetic mismatch affects the **DMA barrier only** — CORE_BARRIER and
> SYNC_BARRIER's mapping tokens match their enum names exactly. See
> [dma-barrier.md](dma-barrier.md). **[OBSERVED HIGH — grepped both spellings.]**

See [collective-enums.md](collective-enums.md) for the full pseudo-op opcode map.

---

## 8. Error / timeout — there is none

No timeout, no error field, no validator anywhere in the CORE_BARRIER path:

* The struct carries only `header`/`events`/`id`/`semaphore`/`reserved` — no
  timeout, no error-code, no max-spin field (probe + header read).
* No `is_valid_*_core_barrier` assert body (`assert.h:12648`, empty marker, §1.3).
* The `WAIT_MODE` enum (§3) has **no** "wait-or-timeout" variant — only
  `EQ/LT/LE/GT/GE` (imm and reg) and the event waits. There is no escape mode.

**Consequence:** a CORE_BARRIER is an **unconditional spin** on
`WAIT_FOR_SEM_GE`. If a participating pcore never arrives (never INCs the shared
semaphore up to `N`), the waiters block **indefinitely**. Liveness is a
compiler/runtime guarantee (every VNC pcore emits its matching arrive), **not**
enforced by the instruction. **[absence of a timeout OBSERVED HIGH; the
indefinite-spin consequence INFERRED MED — the GE wait has no escape in the
WAIT_MODE enum.]**

---

## 9. Device handler — there is no `0xD8` dispatch arm

There is **no** dedicated device handler for opcode `0xD8` — the correct,
expected finding for a pseudo-op:

* `0xD8` is a **pseudo** opcode (§4); pseudo-ops are rewritten by NRT/KRT into
  **non-pseudo** HW instructions *before* the instruction stream reaches the
  engine sequencer. The SEQ/POOL device handlers therefore never see `0xD8` —
  they see the lowered form (`EVENT_SEMAPHORE 0xa0` / `POLL_SEM 0xb3`).
* The host ucode decoder `libnrtucode_internal.so` (x86-64, `BuildID[sha1]
  9cbf78c6...`, **not stripped**) has **zero** `core_barrier`/`barrier`
  symbols, **zero** barrier strings, and decodes via a C function-pointer
  table `hwdecode_table_list` (`.data`, `0x9b9090`, size `0x260` = **76**
  8-byte slots — slot `N` = `symbol + 8*N`, the C fn-ptr table convention; no
  C++ vtable here). None of those 76 slots is a `0xD8` decoder.
* A repo-wide search for `CORE_BARRIER` / `0xd8` finds it **only** in the four
  arch ISA headers + their `instruction_mapping.json` — **zero** occurrences in
  any runtime or firmware binary.

The *actual* device-side execution is the `EVT_SEM` semaphore primitive: the
firmware's `add_semaphore_inc` (ucode `0x10A0` subop `21`) writes the
`SEMAPHORE_INC` (`+0x1800`) window (arrive);
`add_semaphore_wait_ge_and_dec` (`0x10A0` subop `20`) polls `SEMAPHORE_READ`
(`+0x1000`) for `>= target` then writes `SEMAPHORE_DEC` (`+0x1C00`) (wait+reset).
The TOP_SP `POLL_SEM` (`0xb3`) is the per-iteration poll accelerator. **These**
are the handlers the lowered CORE_BARRIER runs through. **[the no-`0xD8`-handler
sweep is OBSERVED HIGH (nm/strings/rg this session); that the lowering uses the
EVT_SEM ucode ops is INFERRED MED.]**

> **NOTE — section-offset deltas on this binary.**
> `libnrtucode_internal.so` section layout this session:
> `.rodata` VMA `0x46b0` == file `0x46b0` (no delta); but `.data` VMA `0x9ba4a8`
> vs file `0x9b74a8` (**delta `0x3000`**) and `.text` VMA `0x9b01a0` vs file
> `0x9af1a0` (delta `0x1000`). Confirm per-section with `readelf -SW` before any
> `.data` byte read on this binary — only `.rodata` is VMA==fileoffset here.

---

## 10. ABI reconciliation — collective-only, not a custom-op primitive

Is CORE_BARRIER available to the 8-core SPMD custom-op kernels? **No.** An
exhaustive sweep of the custom-op device library
(`libneuroncustomop.a`) found **zero** barrier / semaphore / spinlock / atomic /
cross-core sync primitives: the 8 SPMD Q7 cores are **fully independent** — each
on private dataram + a private `hbm_scratch` sub-window + a private SoC aperture,
identity by `PRID` only (see [multicore-spmd](../../abi/multicore-spmd.md) and
[spmd-teardown](../../runtime/spmd-teardown.md)).

This is **consistent**, not contradictory, with CORE_BARRIER's existence —
they live in two **disjoint layers**:

1. CORE_BARRIER is a **pseudo-op** (§4) — emitted by the **graph compiler** and
   rewritten by NRT/KRT. Custom-op kernels are hand-written C compiled by the
   customer's `xt-clang++`; they never emit TPB pseudo-ops.
2. Its doc scope is "pcores within a VNC" — the logical-NeuronCore fusion
   (`LNC1/2/4/8`) the SB2SB collective uses. That is the **collective /
   fused-graph** world, not the per-core SPMD custom-op world (a fixed 8-core
   POOL cluster with no VNC fusion).
3. It appears **only** in the ISA headers (the compiler/runtime contract), with
   zero references in any runtime binary **and** zero in `libneuroncustomop.a`.

**Conclusion:** CORE_BARRIER is **collective / fused-NeuronCore-only**. The
graph compiler/runtime emits it to sync the pcores of a VNC during collective and
loop-on-chip flows; the SPMD custom-op kernels neither use nor have access to it.
That is precisely *why* the "no-cross-core-sync in custom ops" finding and the
existence of CORE_BARRIER are both true at once. **[each premise OBSERVED; the
layering conclusion INFERRED-STRONG.]**

---

## 11. Reimplementation checklist

To emit / decode a Vision-Q7-compatible CORE_BARRIER:

1. **Encode** a 64-byte word: `header.opcode = 0xD8`,
   `header.inst_word_len = 64`; `events = {wait_mode, wait_idx, update_mode,
   update_idx, semaphore_value}`; `id` (u32 @12); `semaphore` (u32 @16); zero
   `reserved1[20..63]`. Layout is identical on NC-v2/v3/v4/v5.
2. **For the arrive leg**, set `events.update_mode = SEM_INC_COMPLETE (0x13)`
   (or `SEM_INC_READ 0x3`) and `events.update_idx = i`.
3. **For the wait leg**, set `events.wait_mode = WAIT_FOR_SEM_GE_IMM (0x05)`,
   `events.wait_idx = i`, `events.semaphore_value = N` (the pcore count of the
   target VNC, per `LNC_SIZE_FMT`).
4. **Resolve `semaphore` → an `EVT_SEM` index `i` ∈ 0..255**; arrive writes
   `EVT_SEM +0x1800 + i*4`, wait polls `EVT_SEM +0x1000 + i*4`, reset writes
   `EVT_SEM +0x1C00 + i*4`.
5. **Do not** expect the engine sequencer to decode `0xD8` — lower it to
   `EVENT_SEMAPHORE (0xa0)` / `POLL_SEM (0xb3)` in the runtime first, exactly as
   the NCFW `add_semaphore_inc 0x10A0/21` / `add_semaphore_wait_ge_and_dec
   0x10A0/20` pair does.
6. **Provide liveness yourself** — there is no timeout; every VNC pcore *must*
   emit its arrive or the barrier deadlocks.

---

## Cross-references

* [TriggerCollective `0xC8`](trigger-collective.md) — shares the
  `group_id`/`channel_id` replica-group vocabulary and the `events` block; the
  rank-level collective trigger (vs. this pcore-level barrier).
* [PSEUDO_SYNC_BARRIER `0xD5`](sync-barrier.md) — the all-engine, within-core
  loop-on-chip fence (trilogy sibling).
* [PSEUDO_DMABARRIER `0xC3`](dma-barrier.md) — the SBUF staggered-write
  DMA-ordering Nop fence (trilogy sibling; enum/mapping naming anomaly).
* [Collective-Type + cc_op Enum Reference](collective-enums.md) — the full
  pseudo-op opcode map.
* [The Unified Collective-Communication Architecture](architecture-synthesis.md)
  — where the coarse NCFW barrier brackets ring/mesh phases.
* [NCFW main dispatch loop](../ncfw/main-dispatch-loop.md) — the firmware
  context that runs the device-side counted-semaphore barrier.
* [Multicore API (8-core SPMD)](../../abi/multicore-spmd.md) /
  [SPMD teardown](../../runtime/spmd-teardown.md) — the *independent* custom-op
  cores that do **not** use CORE_BARRIER (§10).
* [RDMA cross-die](../../dma/rdma-cross-die.md) /
  [SB2SB remote copy](../../firmware/kernels/sb2sb-remote-copy.md) — the
  `EVT_SEM` `+0x1800` INC window and the 256-semaphore array, byte-exact.
