# Atomic + Memory-Ordering Model

This page documents the memory-consistency model the GPSIMD Vision-Q7 (`ConfigName
ncore2gp`, arch Xtensa24/XEA3, uarch "Cairo") exposes to a custom op: the atomic
substrate (the `L32EX`/`S32EX` exclusive monitor, `ATOMCTL`, the release-sync
pair), the fence catalog (`MEMW`/`EXTW`/`DSYNC`/`ISYNC`/`RSYNC`/`ESYNC`/`EXCW`/`FSYNC`),
the store-buffer / AXI-master transaction model, the management-plane `STRONG_ORDER`
enforcement, and the **one rule** a kernel must follow — fence before a doorbell.

Every encoding, count, and config fact below was ground three ways: the device
toolchain oracle (`xtensa-elf-as` + `xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`),
the opcode encode-thunk constants in `libisa-core.so`, and the cycle-accurate ISS
in `libcas-core.so` (unstripped). Per-claim tags are
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

Cross-refs: the system-ISA opcode references
[B27 — System / SR / Window / Sync](../isa/ref/b27-xt-system.md) (owns the
`SYNCT` fence group) and
[B29 — Debug / Cache / RAM / Atomic](../isa/ref/b29-xt-system2.md) (owns the
`ATOMCTL` SR); [B28 — Exception dispatch](../isa/ref/b28-xt-exc.md) (owns `EXCW`);
[B30 — Appendix-P](../isa/ref/b30-appendix-p.md) (the decode-group fold hosting
`L32EX`/`S32EX` under `IMP`, `GETEX` under `ST1`, `CLREX` under `RFEI`, the
`FSYNC`/sync residuals). Forward links: the
[Co-issue matrix](co-issue-matrix.md) for the LSU pipe stages.

---

## 0. TL;DR — what a custom op may assume

[HIGH/OBSERVED] The Q7 exposes the canonical Xtensa-NX (XEA3) **weak,
store-buffered** consistency model with **explicit-fence** ordering. The full
atomic + ordering surface ships and is oracle-confirmed, but the shipped GPSIMD
software **never relies on the multi-core part of it**:

* **Exclusive monitor (LL/SC), not CAS.** `HAVE_EXCLUSIVE = 1`,
  `HAVE_S32C1I = 0`. The atomic RMW primitive is the `L32EX`/`S32EX`
  reservation pair; the legacy LX compare-and-swap `S32C1I` is **absent** (the
  device assembler rejects it).
* **Release-sync pair present.** `HAVE_RELEASE_SYNC = 1` → `L32AI` (acquire load)
  / `S32RI` (release store), the cheap one-sided ordering primitives.
* **Eight fences ship**: `ISYNC RSYNC ESYNC DSYNC MEMW EXTW EXCW FSYNC` — all
  with confirmed encode thunks + oracle round-trip.
* **One fence is actually used.** A sweep of all 10 objects in
  `libneuroncustomop.a` finds **zero** `L32EX`/`S32EX`/`L32AI`/`S32RI` and
  **zero** barrier/semaphore/spinlock/CAS symbols. The only ordering primitive
  the shipped code emits is `MEMW` — overwhelmingly the **doubled `memw ; memw`
  barrier before a DMA doorbell** in `data_transfer.o`.
* **Data-race-free by construction.** The 8 Q7 cores run SPMD on per-core-private
  memory (`PRID`-indexed, `sram0_0_seg = 0x84000000 + prid*0x200000`), share no
  mutable structure, and so need only producer→consumer ordering against the DMA
  engine — supplied by `MEMW`, not by the atomics.

> **The single correctness rule.** Before ringing a DMA/engine doorbell that
> publishes a descriptor or buffer you just wrote, issue `memw` (the shipped
> idiom is `memw ; memw`) so the descriptor/payload writes drain past the
> 8-entry write buffer and become visible to the engine **before** the
> tail-pointer store launches it. Everything else on this page is the machinery
> behind that rule — most of it dormant in the shipped stack but live in the
> silicon. [HIGH/OBSERVED — §6.]

---

## 1. Config grounding — the atomic substrate

[HIGH/OBSERVED] From `core-isa.h` (ncore2gp), the option group that defines the
atomic/ordering surface:

| `XCHAL_*` macro | value | meaning |
|---|---|---|
| `HAVE_EXCLUSIVE` | `1` | `L32EX`/`S32EX` exclusive load/store present (`core-isa.h:71`) |
| `HAVE_S32C1I` | `0` | legacy LX compare-and-swap **absent** (`core-isa.h:77`) |
| `HAVE_RELEASE_SYNC` | `1` | `L32AI`/`S32RI` acquire/release present (`core-isa.h:76`) |
| `NUM_WRITEBUFFER_ENTRIES` | `8` | the store buffer that decouples stores from the pipe (`:227`) |
| `HAVE_PIF` / `HAVE_AXI` | `1` / `1` | an outbound AXI master bus (`:353`,`:355`) |
| `HAVE_AXI_ECC` | `1` | ECC on the AXI fabric path (`:356`) |
| `HAVE_ACELITE` | `1` | ACE-Lite — the exclusive/snoop transaction substrate (`:357`) |
| `HAVE_PIF_WR_RESP` | `1` | AXI write responses tracked (drain can KNOW commit) (`:359`) |
| `HAVE_MIMIC_CACHEATTR` | `0` | no per-region cache-attr mimic (`:785`) |
| `DCACHE_SIZE` | `0` | **no L1 D-cache** — data side = DataRAM + write buffer (`:298`) |
| `DCACHE_IS_COHERENT` | `0` | **no MP hardware coherence** (`:302`) |
| `ICACHE_SIZE` | `16384` | the only cacheable space is the 16 KB I-cache (`:296`) |
| `XEA_VERSION` | `3` | XEA3 (NX) exception/memory architecture (`:719`) |
| `UNALIGNED_LOAD/STORE_EXCEPTION` | `1`/`1` | unaligned access traps (`:235`,`:236`) |

> **READING — this is a "monitor, not CAS" core.** NX replaced the LX conditional
> store `S32C1I` with the ARM-style **LL/SC exclusive monitor** (`L32EX`/`S32EX`)
> and added the one-sided `RELEASE_SYNC` pair. `DCACHE_IS_COHERENT = 0` means
> there is no internal coherence engine — an exclusive that must be globally
> atomic against another master goes out as an **ACE-Lite exclusive transaction**
> on the AXI master, routed by `ATOMCTL` (§3). All shared data lives in
> non-D-cacheable AXI/SBUF space, so the operative `ATOMCTL` category is the
> external one. [HIGH/OBSERVED config; routing INFERRED canonical NX.]

### 1.1 The opcode roster ships — encode thunks + oracle

[HIGH/OBSERVED] `libisa-core.so` carries **exactly one** `_Slot_inst_encode`
thunk per opcode for every atomic/fence op. The base word is the `movl $imm` the
thunk writes into the instruction slot (operand fields OR in afterward):

```
$ nm libisa-core.so | rg 'Opcode_(l32ex|s32ex|getex|clrex|l32ai|s32ri|...)_Slot_inst_encode'
  Opcode_l32ex_Slot_inst_encode   →  movl $0xf14000   ; base, +operands
  Opcode_s32ex_Slot_inst_encode   →  movl $0xf15000
  Opcode_getex_Slot_inst_encode   →  movl $0x40a000
  Opcode_clrex_Slot_inst_encode   →  movl $0x003120   ; no operands
  Opcode_l32ai_Slot_inst_encode   →  movl $0x00b002
  Opcode_s32ri_Slot_inst_encode   →  movl $0x00f002
  Opcode_rsr_atomctl_Slot_inst_encode → movl $0x036300
  Opcode_wsr_atomctl_Slot_inst_encode → movl $0x136300
  Opcode_xsr_atomctl_Slot_inst_encode → movl $0x616300
  Opcode_memw / extw / excw / dsync / esync / isync / rsync / fsync …
```

[HIGH/OBSERVED] **Oracle round-trip** on the device toolchain
(`XTENSA_CORE=ncore2gp`, `XTENSA_SYSTEM=tools/XtensaTools/config`) — assemble,
then disassemble back to the exact mnemonic + operand + byte:

```asm
l32ex   a2, a3   → f14320      isync           → 002000
s32ex   a2, a3   → f15320      rsync           → 002010
getex   a2       → 40a020      esync           → 002020
clrex            → 003120      dsync           → 002030
rsr.atomctl a3   → 036330      excw            → 002080
wsr.atomctl a3   → 136330      memw            → 0020c0
xsr.atomctl a3   → 616330      extw            → 0020d0
l32ai   a2, a3, 0 → 00b322     fsync   0       → 002800
s32ri   a2, a3, 0 → 00f322
```

> **QUIRK — `s32c1i` is rejected.** The device assembler answers
> `Error: unknown opcode or format name 's32c1i'` (exit 1). `HAVE_S32C1I = 0` is
> not a suppressed-but-present feature; the opcode does not exist in this config.
> This is the binary proof that the only atomic RMW path is the exclusive
> monitor. [HIGH/OBSERVED.]

> **NOTE — `fsync` needs its immediate.** `FSYNC` carries an 8-bit selector
> (`imms8`); the canonical encoding is `fsync 0 = 0x002800`. A bare `fsync` is an
> assembler error ("too few operands"). [HIGH/OBSERVED — B30 §2.1.]

---

## 2. The exclusive monitor — `L32EX` / `S32EX` reservation model

[HIGH/OBSERVED encoding + operand shape + TIE package + XTSYNC stage;
MED/INFERRED the LL/SC success/fail-in-AR contract.]

`L32EX`/`S32EX` belong to TIE package **`xt_core`** (`_TIE_xt_core_L32EX`,
`_TIE_xt_core_S32EX`), decode-group `IMP` (`op0=0, op1=1, op2=15`), `r=4` (load)
/ `r=5` (store). Operand list (from `Iclass_xt_iclass_l32ex_args`): `art` (data
register) + `ars` (address register) — **one** 32-bit aligned memory operand, the
address in `ars`, data in/out via `art`. Both issue through the single scalar
load-store unit (`funcUnit_uses → "XT_LOADSTORE_UNIT"`).

| op | word (`a2,a3`) | group | role | priv | XTSYNC |
|----|----|----|----|----|----|
| `L32EX` | `0xf14320` | `IMP` `op2=15` `r=4` | load + **arm** the reservation | no | DEF @6 |
| `S32EX` | `0xf15320` | `IMP` `op2=15` `r=5` | conditional store; win/lose → `art` | no | DEF @6 |

```c
// L32EX at, as  — load-exclusive (xt_core; word 0xf14320 for at=a2,as=a3)
uint32_t L32EX(uint32_t *addr) {              // addr = AR[s] (ars)
  arm_reservation(addr & ~(GRANULE - 1));     // take the local granule reservation
  return *addr;                               // 32-bit aligned load (unaligned → trap)
}                                             // → AR[t] (art); +XTSYNC barrier @6

// S32EX at, as  — store-exclusive (xt_core; word 0xf15320)
bool S32EX(uint32_t *addr, uint32_t val) {    // val = AR[t] (art), addr = AR[s]
  if (!reservation_valid(addr)) {             // lost to an intervening write / clrex /
    AR[t] = 0;                                //   exception-return / snooped foreign write
    return false;                             //   → store SQUASHED, status 0 into AR[t]
  }
  switch (atomctl_field(memtype(addr))) {     // ATOMCTL per-memtype 2-bit policy (§3)
    case ATOMCTL_RCW:      *addr = val; break;        // internal read-conditional-write
    case ATOMCTL_INTERNAL: *addr = val; break;        // internal-bus exclusive
    case ATOMCTL_EXCEPTION: raise(LoadStoreError); break; // region can't do exclusives
  }
  clear_reservation();
  AR[t] = 1;                                  // status 1 (won) into AR[t], in place
  return true;                                // +XTSYNC barrier @6
}
```

The reservation is **cleared** by: a successful `S32EX`, any `CLREX`, an
exception/interrupt return, or a conflicting write to the granule by another
master (the monitor's snoop, via ACE-Lite). [opcode mechanics OBSERVED; the
success/fail-in-`art` + granule semantics are the canonical NX `L32EX`/`S32EX`
contract, INFERRED — the shipped code never exercises them, so no live trace
exists.]

### 2.1 Monitor companions — `GETEX`, `CLREX`

| op | word | group | role | XTSYNC |
|----|----|----|----|----|
| `GETEX` | `0x40a020` (`a2`) | `ST1` `op2=4` `r=0xa` | transfer monitor/lock state ↔ AR (the `WaitEX` read-and-arm idiom) | DEF @6 |
| `CLREX` | `0x003120` | `RFEI` `r=3` under `ST0` | drop the local reservation unconditionally | DEF @6 |

`GETEX` operands (`Iclass_xt_iclass_getex_args`): `art` + `XTSYNC` + `ars` — the
monitor-state transfer with its self-fence. `CLREX` takes no architectural
operand; its `stateArgs` are `XTSYNC, XTSYNC, art` — it touches only the monitor
+ barrier. Use `CLREX` to drop a stale reservation before a context switch or
after a failed CAS-retry.

### 2.2 The CAS a user would build (the only atomic RMW the Q7 offers)

```c
// canonical NX LL/SC compare-and-swap retry loop — no shipped code uses this.
// The monitor self-fences via XTSYNC @6, so back-to-back exclusives are ordered.
retry:
    l32ex  a3, a_addr          // a3 = *addr, arm monitor
    bne    a3, a_expected, fail // not the value we wanted → bail
    mov    a4, a_new
    s32ex  a4, a_addr          // a4 = 1 if won the reservation, 0 if lost
    beqz   a4, retry           // lost (foreign write / context switch) → retry
fail:
    // ...
```

### 2.3 Exclusive-monitor pipeline timing (ISS-confirmed)

[HIGH/OBSERVED — `libcas-core.so` cycle model.] `L32EX`/`S32EX` run the normal
scalar LSU pipe (7-stage `R0/E3/M4/W6`, DX-HW-03 §5.2) and assert the
exclusive-monitor barrier on the **XTSYNC register pipeline** at **stage 6**:

| ISS function (`libcas-core.so`) | stage | what it does |
|---|---|---|
| `x24_Inst_0_L32EX_inst_stage5` @`0x16abf80` | 5 | `nx_ScalarMemDataIn32_0_interface` → **load data @5** |
| `x24_Inst_0_L32EX_inst_stage6` @`0x16abf20` | 6 | `mov $0x6,%esi; call my_XTSYNC_def` → **XTSYNC DEF @6** |
| `x24_Inst_0_inst_L32EX_issue` @`0x16785d0` | 6 | `my_XTSYNC_set_def(6)` → reserves the barrier slot at issue |
| `x24_Inst_0_S32EX_inst_stage5` @`0x16abcd0` | 5 | `nx_ScalarMemDataOut32_0_interface` → **store data @5** |
| `x24_Inst_0_S32EX_inst_stage6` @`0x16abc70` | 6 | `my_XTSYNC_def(6)` → **XTSYNC DEF @6** |
| `GETEX`/`CLREX` `*_inst_stage6` @`0x16abb50`/`0x16abae0` | 6 | `my_XTSYNC_def(6)` |

The store data retires into the **8-entry write buffer @5** (scalar) and drains
asynchronously (§5). The XTSYNC@6 makes every exclusive self-serializing: the
monitor arm/check cannot reorder around the surrounding pipe.

> **NOTE — XTSYNC is the exclusives' barrier mechanism, not the plain fences'.**
> An exhaustive census of `my_XTSYNC_def`/`set_def` call sites in `libcas-core.so`
> shows XTSYNC is asserted **only** by SR-write opcodes (every `WSR.*`/`XSR.*` at
> stage 6, window-shifted to 7/8 for `PS`/`WPTLB`) and by
> `L32EX`/`S32EX`/`GETEX`/`CLREX` (stage 6). The `MEMW`/`EXTW`/`DSYNC`/`RSYNC`/
> `ESYNC` fences do **not** route through XTSYNC at all — they are modeled as
> scratch-field descriptors plus dedicated `x24_Inst_0_inst_{DSYNC,RSYNC,ESYNC}_stall`
> accessors. The "XTSYNC@2/3/4" tags on those fences in B27's ISS table are the
> opcode-DB **scheduler** USE-stage annotations (§4), a different layer from the
> cas-core barrier pipeline. Both are true; do not conflate the layers. [HIGH/OBSERVED.]

---

## 3. `ATOMCTL` (SR#99, 0x63) — the exclusive-access ordering/cacheability control

[HIGH/OBSERVED SR number + field + privilege + package + ISS timing;
MED/INFERRED the per-memtype bit decomposition.] `ATOMCTL` is the control
register of the NX exclusive-access monitor — it decides, **per memory type**,
how an `L32EX`/`S32EX` is handled. Owned by [B29 §3.9](../isa/ref/b29-xt-system2.md);
TIE package `xt_sync` (`_TIE_xt_sync_RSR_ATOMCTL` …).

* **SR number** `99 = 0x63` (decoded as `(0x0363 >> 8) & 0xff` from the encode
  word). Encodings: `RSR.ATOMCTL a3 = 0x036330`, `WSR = 0x136330`, `XSR = 0x616330`.
* **State field** — the string-table descriptor in `libisa-core.so` reads
  `9:0:s:ATOMCTL:0` and the carried state table (SX-ISA-05 entry 29) gives
  `[9:0:s:ATOMCTL:0, 23:9:c]`: a **10-bit state field spanning bits [9:0]** with
  the upper bits **[23:9] a constant**.

> **CORRECTION — the field is bits [9:0] (10 bits), not "9 bits".** Bit range
> `[9:0]` inclusive is **ten** bits wide. Earlier notes that say "9-bit state
> field" describe the high bit index, not the width. The accompanying constant
> field is `[23:9]`. [HIGH/OBSERVED — `libisa-core.so` `9:0:s:ATOMCTL:0` +
> SX-ISA-05 entry 29.]

* **Privilege.** `RSR.ATOMCTL`'s `stateArgs` are `MS_DISPST`, `PSRING`,
  `InOCDMode` — the privilege-gate trio. Like every B29 SR it raises
  `PrivilegedException` when `(MS_DISPST == 0) && |PS.RING && !InOCDMode`. The
  exclusives themselves are **non-privileged** (their `stateArgs` are only
  `XTSYNC`) — so a kernel can run `l32ex`/`s32ex` but cannot reprogram their
  routing policy without ring-0.

### 3.1 The per-memory-type field (the policy `S32EX` reads)

[MED/INFERRED — the canonical NX `ATOMCTL` layout; this config never writes
`ATOMCTL`, so the bit split is not directly observed in shipped code.] The 10-bit
field partitions memory by its cache/order attribute and, per category, selects
how an exclusive is handled:

| category (memory attr) | relevant here? | exclusive handling (2-bit sub-field) |
|---|---|---|
| **non-cacheable / external** (AXI/PIF + the SBUF aperture) | **yes** — all shared data lives here | route to the **ACE-Lite external monitor**, or raise `LoadStoreError` if the region cannot do exclusives |
| cacheable write-back (the I-cache octants) | no (instruction side only) | internal RCW (read-conditional-write) |
| cacheable write-through | no | internal-bus exclusive or exception per the field |

On ncore2gp the operative category is the **external** one (no D-cache, so all
data is non-D-cacheable AXI/SBUF space). `ATOMCTL` is therefore what decides
whether an `L32EX`/`S32EX` to the SBUF aperture becomes an ACE-Lite exclusive
transaction on the AXI master or faults. Since the shipped software uses no
exclusives, `ATOMCTL` is programmed once at boot by firmware/LSP and never touched
by a custom op.

### 3.2 `ATOMCTL` pipeline timing (ISS-confirmed)

[HIGH/OBSERVED — `libcas-core.so`.]

| ISS function | stage | effect |
|---|---|---|
| `x24_Inst_0_RSR_ATOMCTL_inst_stage4` @`0x1694b00` | 4 | `nx_RSRBus_interface`; `my_ATOMCTL_use(4)` → **ATOMCTL USE @4** |
| `x24_Inst_0_RSR_ATOMCTL_inst_stage5` @`0x1694a90` | 5 | indirect AR reg-def → **`art` DEF @5** |
| `x24_Inst_0_WSR_ATOMCTL_inst_stage6` @`0x1694860` | 6 | `nx_WSRBus_interface` (**WSRBus @6**); `my_ATOMCTL_def(6)`; `my_XTSYNC_def(6)` |
| `x24_Inst_0_XSR_ATOMCTL_inst_*` @`0x1694560` | 4 + 6 | mirrors WSR: ATOMCTL USE@4 then ATOMCTL/XTSYNC DEF@6 |

So `RSR.ATOMCTL` reads the policy at **stage 4** (serialised against in-flight
exclusive accesses via the ATOMCTL pipeline) and DEFs `art @5`; `WSR`/`XSR`
broadcast the new policy at **stage 6** with `ATOMCTL @6 + XTSYNC @6 + WSRBus @6`
— a pipeline-resyncing write.

> **CORRECTION — `RSR.ATOMCTL` does *not* assert XTSYNC on the read path in the
> cas-core model.** No `my_XTSYNC_*` call appears in any `RSR_ATOMCTL_inst_stage*`
> function — only `my_ATOMCTL_use(4)`. The read *is* serialised against in-flight
> exclusives, but via the **ATOMCTL** register pipeline at stage 4, not via
> XTSYNC. (The B29 §3.9 page's "USEs `XTSYNC @4`" describes the opcode-DB
> scheduler annotation; the cycle model realises the serialisation through the
> dedicated ATOMCTL pipeline instead.) `WSR`/`XSR.ATOMCTL` *do* assert XTSYNC@6.
> [HIGH/OBSERVED — `libcas-core.so` `x24_Inst_0_RSR_ATOMCTL_inst_stage4`
> @`0x1694b6b` vs `x24_Inst_0_WSR_ATOMCTL_inst_stage6` @`0x16948d9`.]

---

## 4. The fence catalog — what each ordering primitive orders

[HIGH/OBSERVED] All `*SYNC`-family fences share `op0=0, op1=0, op2=0, r=2` (the
`SYNC`/`SYNCT` subtree of `ST0 ← RST0 ← QRST`); the `SYNCT` `t`-field selects the
variant. They carry **no** data operand, **no** regfile DEF, and **no** exception
— each is a schedule-only pipeline barrier. The `SYNCT` siblings are
`{DSYNC ESYNC ISYNC RSYNC EXCW EXTW MEMW NOP}` (`NOP = 0x0020f0`); `FSYNC` sits
one level up under `SYNC` with an 8-bit immediate.

### 4.1 Pipeline-domain fences (order the core's own pipe state)

| Mn | `t` | word | what it orders | ISS USE-stage |
|----|----|----|----|----|
| `ISYNC` | 0 | `0x002000` | flush fetch/decode so prior `WSR`/cache-config/IRAM writes are seen by **subsequent fetch** (full I-barrier) | (I-barrier) |
| `RSYNC` | 1 | `0x002010` | stall until pending `XTSYNC`-tagged SR/AR writes retire | `XTSYNC@3` |
| `ESYNC` | 2 | `0x002020` | execution sync — superset of `RSYNC` (drain whole E/M pipe) | `XTSYNC@4` |
| `DSYNC` | 3 | `0x002030` | order load/store + `WSR.MEMCTL` vs following memory ops | `XTSYNC@2` |
| `FSYNC` | (s8) | `0x002800` | fetch/format-pipeline sync, immediate-qualified (rare) | — |

### 4.2 Memory-visibility fences (order accesses as other masters see them)

| Mn | `t` | word | what it orders | ISS USE-stage | drains wr-buf? |
|----|----|----|----|----|----|
| `MEMW` | 12 | `0x0020c0` | **all** memory accesses (loads + stores, internal + external) across the barrier — the C "memory clobber" | `MEMW@2` | orders all |
| `EXTW` | 13 | `0x0020d0` | drain the **external/device** write buffer; order writes to external (AXI/PIF) space | `EXTW@8` | **yes (ext)** |
| `EXCW` | 8 | `0x002080` | exception-wait: outstanding exceptions/imprecise faults resolve before continuing | (excw) | no |

> **GOTCHA — the `*SYNC` fences are not interchangeable.** They resync different
> domains, ordered by their scheduler USE-stage:
> `DSYNC` (2, data-memory) < `RSYNC` (3, SR/AR writes) < `ESYNC` (4, full
> execution). `ISYNC` is the *instruction-fetch* barrier — the only one that
> flushes the fetch front-end, and the one that **must** follow any `WSR` to a
> fetch-affecting register (`MEMCTL`, cache config, `IBREAK*`) or an IRAM/`IHI`
> write before the change is observed by a fetch. `EXTW` (stage 8) is the deepest
> — it drains the *external* write buffer and waits the AXI write-response
> (`PIF_WR_RESP=1`); use it before a device-register write that must observe all
> prior device writes committed. Picking `MEMW` where `EXTW` is needed leaves
> device writes unflushed. [HIGH/OBSERVED stages — B27 §3.9.]

> **NOTE — USE-stage vs barrier-mechanism.** The `XTSYNC@2/3/4` / `MEMW@2` /
> `EXTW@8` numbers above are the opcode-DB **scheduler** USE-stages (the
> `INSTR_SCHEDULE` metadata the assembler uses to place fences). In the
> `libcas-core.so` cycle model these fences are realised through scratch-field
> descriptors and `_stall` accessors, **not** through the XTSYNC register
> pipeline (which only the exclusives and SR writes use — §2.3). The two views
> are consistent: the scheduler knows the fence's ordering depth; the cycle model
> enforces it via stalls. [HIGH/OBSERVED.]

### 4.3 Release-sync — the one-sided half-barriers

[HIGH/OBSERVED encoding + operands; INFERRED acquire/release semantics.] Package
`xt_sync`. Operands (`Iclass_xt_iclass_{l32ai,s32ri}_args`): `art` + `ars` +
`uimm8x4` (base + scaled 8-bit displacement).

| op | word (`a2,a3,0`) | role |
|----|----|----|
| `L32AI` | `0x00b322` | **acquire load** — its result is observed before any later memory access in program order |
| `S32RI` | `0x00f322` | **release store** — globally visible only after all prior memory accesses |

The lightweight alternative to a full `MEMW` around a flag: a producer does its
writes, then `S32RI`'s a ready-flag (release); a consumer `L32AI`'s the flag
(acquire), then reads the payload. The Q7 has them (`RELEASE_SYNC=1`) but the
shipped customop code does not use them — it uses the heavier `memw;memw`
doorbell instead (§6).

### 4.4 Fence-choice rule (practical)

* need a later instruction to **see a prior SR write** → `RSYNC` (or `ISYNC` if
  the write changes instruction decoding/fetch).
* need **ordered loads/stores within this core** → `DSYNC`.
* need a value **globally visible before launching a peer/DMA** → `MEMW` (the
  doorbell rule, §6).
* need **device-register writes fully drained** to the fabric → `EXTW` (waits the
  write buffer + AXI response).
* need an **atomic RMW** → `L32EX`/`S32EX` loop (the monitor self-fences via
  XTSYNC@6).
* need **cheap producer→consumer flag handoff** → `S32RI`/`L32AI`.

### 4.5 Atomic + ordering op table (consolidated)

| mnemonic | word (`a2/a3`) | group / package | role | priv | barrier |
|----|----|----|----|----|----|
| `L32EX` | `0xf14320` | `IMP op2=15 r=4` / `xt_core` | load-exclusive: load + arm reservation | no | XTSYNC DEF @6 |
| `S32EX` | `0xf15320` | `IMP op2=15 r=5` / `xt_core` | store-exclusive: cond. store, win/lose → AR | no | XTSYNC DEF @6 |
| `GETEX` | `0x40a020` | `ST1 op2=4 r=0xa` / `xt_core` | get/swap monitor state ↔ AR (WaitEX) | no | XTSYNC DEF @6 |
| `CLREX` | `0x003120` | `RFEI r=3` / `xt_core` | clear local reservation | no | XTSYNC DEF @6 |
| `RSR.ATOMCTL` | `0x036330` | `RST3` sr=99 / `xt_sync` | read atomic-order policy | **YES** | ATOMCTL USE @4; `art` @5 |
| `WSR.ATOMCTL` | `0x136330` | `RST3` sr=99 / `xt_sync` | write atomic-order policy | **YES** | ATOMCTL/XTSYNC/WSRBus DEF @6 |
| `XSR.ATOMCTL` | `0x616330` | `RST1` sr=99 / `xt_sync` | swap atomic-order policy | **YES** | USE@4 + DEF@6 |
| `L32AI` | `0x00b322` | `xt_sync` (LSAI) | acquire load (half-barrier) | no | (acquire) |
| `S32RI` | `0x00f322` | `xt_sync` | release store (half-barrier) | no | (release) |
| `ISYNC` | `0x002000` | `SYNCT t=0` | I-fetch vs prior config/SR/icache writes | no | (I-barrier) |
| `RSYNC` | `0x002010` | `SYNCT t=1` | following ops vs pending SR/AR writes | no | `XTSYNC@3` |
| `ESYNC` | `0x002020` | `SYNCT t=2` | whole E/M pipe (superset of `RSYNC`) | no | `XTSYNC@4` |
| `DSYNC` | `0x002030` | `SYNCT t=3` | following mem ops vs prior load/store + `MEMCTL` | no | `XTSYNC@2` |
| `EXCW` | `0x002080` | `SYNCT t=8` | outstanding exceptions resolve | no | (excw) |
| `MEMW` | `0x0020c0` | `SYNCT t=12` | **all** memory accesses across the barrier | no | `MEMW@2` |
| `EXTW` | `0x0020d0` | `SYNCT t=13` | external/device writes; drain ext wr-buf | no | `EXTW@8` |
| `FSYNC` | `0x002800` | `SYNC s8` | fetch/format pipeline (imm-qualified) | no | — |
| `(S32C1I)` | **absent** | `HAVE_S32C1I=0` | legacy LX CAS — **not present** | — | — |

---

## 5. The store-buffer + AXI-master transaction model

### 5.1 Data-side topology

[HIGH/OBSERVED — `core-isa.h` + DX-HW-03 §5.2.] No D-cache. The data side is:

* **Local DataRAM0** — `VADDR=PADDR 0x00080000`, 64 KB, **4 banks** (`DATARAM0_BANKS=4`),
  512-bit data bus, single-cycle SRAM folded into the pipe (scalar return @5,
  vector @9). `ECC_PARITY=0` (local RAM unprotected). IDMA-attached
  (`DATARAM0_HAVE_IDMA=1`).
* **8-entry write buffer** (`NUM_WRITEBUFFER_ENTRIES=8`) — decouples stores from
  the pipe, feeding the outbound AXI/ACE-Lite master (ECC on, `PIF_WR_RESP=1`).
* **No on-core SBUF compute port.** SBUF, system RAM, and the I/O blocks are all
  reached as memory-mapped **AXI master** transactions.

### 5.2 Store lifecycle (why ordering isn't free)

[HIGH/OBSERVED structure; cycle counts SoC-dependent.] A scalar `S32I` retires
its store data into the write buffer at pipe **stage 5** (a vector store at @11)
and the pipe moves on — the store drains **asynchronously** from the 8-entry
buffer to its target (DataRAM bank or the AXI master). Consequences:

1. Two stores to **different targets** can drain **out of order**.
2. A store can still be **in the buffer** when a later load or a doorbell executes.

The write buffer is the source of the weak store ordering the fences exist to
tame.

### 5.3 Outstanding-transaction capacity

[HIGH/OBSERVED — `core-isa.h`.]

| master | capacity | notes |
|---|---|---|
| core LSU store buffer | **8 entries** | `NUM_WRITEBUFFER_ENTRIES=8` |
| on-core iDMA engine | **32 outstanding** | `IDMA_MAX_OUTSTANDING_REQ=32`, `REORDERBUF=0` (in-order completion), 128-bit data, 1 channel, descriptor ≤64 B; separate master from the LSU |
| AXI master | write responses tracked | `PIF_WR_RESP=1` → `EXTW`/buffer drain can KNOW an external write committed; `AXI_ECC=1` protects the fabric path |

> **NOTE — in-order iDMA completion.** `IDMA_HAVE_REORDERBUF=0` means the on-core
> iDMA completes in request order; the *SDMA CME* engine reached over the AXI
> doorbell (§6) is a separate device whose ordering you guarantee with the
> doorbell fence, not with the iDMA's in-order property. [HIGH/OBSERVED.]

### 5.4 The SBUF path

[HIGH/OBSERVED — DX-DMA-06 §5–7.] A GPSIMD load/store to SBUF is an AXI/ACE-Lite
master transaction through the **axi2sram** bridge; on-core the Q7 sees SBUF via a
pinned NX-local window mapped (per `tpb_idx`) by `aws_hal_stpb_get_axi_offset`,
aperture size **0x2000000 (32 MiB)**. The Q7 has **no dedicated SBUF compute
port** — it arbitrates as a DMA slot behind the 2-stage SBUF arbiter + TDM.
Consequences:

* an SBUF access is the **longest** data path the core issues (on-core pipe stage
  + AXI round-trip + arbiter stall);
* it is the access a custom op **most** needs to order against the DMA engine —
  which it does with `MEMW` (§6);
* **PSUM has no AXI aperture** (no named `psum_*` CSR; DX-DMA-06 §4): the Q7
  physically cannot touch PSUM. The compiler's "GPSIMD args must be in SBUF" rule
  *is* this absence, not a policy.

### 5.5 Ordering guarantees (the contract)

[HIGH where config-grounded; INFERRED on the canonical NX consistency contract.]

| relation | guarantee without a fence |
|---|---|
| this core's own dependent accesses (same address) | **ordered** — load reads the most recent program-order store via LSU/write-buffer forwarding; in-order issue → a core sees its own accesses sequentially |
| this core's stores to **different** addresses | **not ordered** — the 8-entry write buffer drains out of order/async |
| internal (DataRAM) vs external (AXI/SBUF/PIF) targets | **not ordered** relative to each other |
| this core vs DMA / another master | **not ordered, not atomic** — needs a fence (`MEMW`/`EXTW`/`DSYNC`), the exclusive monitor (`L32EX`/`S32EX` + `ATOMCTL`), or `S32RI`/`L32AI` |
| a plain aligned 32-bit load/store | **single-copy atomic** |
| a wider/unaligned access | **not atomic** — unaligned traps (`UNALIGNED_*_EXCEPTION=1`) |

In one line: **sequential consistency for a single core's own accesses, and
nothing stronger across masters unless you fence.**

---

## 6. The one real fence — the doubled doorbell barrier

[HIGH/OBSERVED — `data_transfer.o` disassembled with the device objdump.] The SDMA
launch sequence in `dma_data_transfer` (`_Z17dma_data_transferPvybj`,
`data_transfer.o` @`0x4f0`) is the **central correctness fence in the whole custom-op
path**. After building the BD-ring descriptor pair, the code emits the literal
**`memw ; memw`** doubled barrier before the doorbell store:

```text
data_transfer.o : _Z17dma_data_transferPvybj
  5c3:  s32i.n  a4, a3, 0        ; last descriptor-word writes to the BD ring (in DataRAM)
  5c5:  s32i.n  a5, a3, 12       ;   …
  5c7:  0020c0  memw             ; barrier 1  ── drain ALL prior stores past the write buffer
  5ca:  0020c0  memw             ; barrier 2  ── the doubled idiom (not foldable/elidable)
  5cd:  l32i.n  a3, a1, 20       ; load the doorbell (tail-inc) MMIO register pointer
  5cf:  s32i.n  a15, a3, 0       ; *doorbell = a15   ── the tail-pointer write that LAUNCHES the engine
  5d1:  0020c0  memw             ; post-store barrier
  …
  62c:  l8ui    a8, a3, 3        ; completion poll: load byte3 of the completion descriptor
  62f:  extui   a8, a8, 0, 2     ;   isolate bits[1:0] (the generation/ring tag)
  632:  bne     a8, a6, 62c      ;   busy-wait until the engine flips the tag → done
```

```c
// the shipped SDMA launch + completion idiom, reconstructed from data_transfer.o
void dma_launch(_dma_ctx_t *ctx, /* … */) {
  // … write the tx/rx BD descriptors into the BD rings carved in DataRAM …
  ring[i].word0 = w0; ring[i].word1 = w1; ring[i].buf_ptr = soc_addr;  // 5c3,5c5…

  __asm__ volatile("memw; memw" ::: "memory");   // 5c7, 5ca — drain BEFORE the doorbell
  *ctx->m2s_inc_reg = 1;                          // 5cf — M2S tail++ (launch); also s2m_inc_reg
  __asm__ volatile("memw" ::: "memory");          // 5d1

  // completion: synchronous poll, no interrupt
  while ((((volatile uint8_t *)comp_desc)[3] & 0x3) != expected_tag)  // 62c–632
    ;                                             // dependent reads of the DMA'd data are
}                                                 //   correctly ordered after this poll
```

The two `memw`'s force every prior store (the descriptor bytes just written to the
BD ring) to be **globally visible before** the tail-pointer-increment doorbell that
tells the SDMA CME engine "go". Without it the engine could latch a tail advance
and DMA-read a descriptor whose body is still in the write buffer → garbage
transfer. The doubling is the canonical Tensilica idiom to guarantee the barrier is
not folded/elided. [HIGH/OBSERVED — read directly from `data_transfer.o`
@`0x5c7–0x5cf`; the `_dma_ctx_t` layout (`m2s_inc_reg@0`, `s2m_inc_reg@8`,
tx/rx/comp BD bases, ring tail/gen tag) is SX-ABI-12 §6a.]

### 6.1 The customop sweep — zero atomics, MEMW-only

[HIGH/OBSERVED — device objdump of all 10 `libneuroncustomop.a` objects.]

| object | `l32ex/s32ex/getex/clrex/l32ai/s32ri` | `memw` | `extw/dsync/excw` |
|---|---|---|---|
| `data_transfer.o` | **0** | **17** | 0 |
| `start_exit.o` | 0 | 5 | 0 |
| `translation.o` | 0 | 4 | 0 |
| `stack_switch.o` | 0 | 3 | 0 |
| `wrapper_api.o` | 0 | 2 | 0 |
| `NeuronAllocator.o` | 0 | 1 | 0 |
| `allocator.o` / `parallel.o` / `switch_stack.o` / `TensorTcmAccessor.o` | 0 | 0 | 0 |
| **total** | **0** | **32** | **0** |

**Zero** exclusives, **zero** release-sync, **zero** `EXTW`/`DSYNC`/`EXCW`,
**zero** barrier/semaphore/spinlock/CAS symbols (the only "mutex" symbols are
libc++ single-thread no-op shims). The `data_transfer.o` `memw×17` are all SDMA
doorbell barriers (the §6 idiom appears at offsets `0x22e, 0x381, 0x3d8, 0x3ec,
0x420…0x474, 0x4d7, 0x5c7, 0x5ca, 0x5d1`); none cross-core. [HIGH/OBSERVED —
SX-ABI-13 §8 corroborates.]

---

## 7. `STRONG_ORDER` — the management-plane ordering enforcement

[HIGH/OBSERVED — carried DX-SEC-01 §4e.] `STRONG_ORDER` is **not** an instruction
and **not** a kernel fence — it is a **host/debugger-armed "surprise"** handled by
the SEQ management core. The SEQ run-state FSM polls its surprises every iteration
(`poll-surprises @0x6af4 → sunda_check_surprises @0x6b0c → sunda_handle_surprises
@0x6cf4`), a fixed-order bit-mask dispatcher over a 2-byte surprise word:

| bit | name | action | terminal? |
|----|----|----|----|
| 9 | **`STRONG_ORDER`** | "re-program ordering" | **non-terminal (continue)** |
| 8 | `STEP_CNT` | single-step countdown; Setup-Halt(reason=64) at 0 | terminal at 0 |
| 3 | `EXT_BREAK` | external breakpoint; Setup-Halt(reason=8) | terminal |
| 2 | `INS_BREAK` | instruction breakpoint; Setup-Halt(reason=4) | terminal |
| (none) | unhandled | "Unhandled surprise" assert → FW-09 FATAL spin | hard fault |

`STRONG_ORDER` is the **only non-terminal ordering surprise**: when the host sets
bit 9, the SEQ re-programs the core's ordering/strength policy (the same machinery
`ATOMCTL` + the fences expose at the ISA level — e.g. forcing strongly-ordered AXI
attributes / disabling write-buffer reordering for a bring-up window) and then
continues execution. It is the run-time, out-of-band **promotion lever** over the
otherwise weakly-ordered store path: the host can promote the core to strong
ordering without recompiling the kernel. Per-gen the taxonomy is byte-identical
CAYMAN/MARIANA/MARIANA_PLUS; SUNDA shares the taxonomy at a different CSR aperture
(halt `0x00100808` vs `0x04000014`).

> **READING.** `STRONG_ORDER` is to the memory model what a chicken-bit is to a
> speculation feature — a host override of the default weak ordering, used during
> debug/bring-up, sitting **above** the ISA fences/`ATOMCTL` the kernel itself
> would use. [HIGH that it is the ordering-reprogram surprise; INFERRED that
> "re-program ordering" = strong-ordering AXI/write-buffer policy promotion.]

---

## 8. How a custom op must fence for correctness

[HIGH/OBSERVED for RULES 1–2; INFERRED from the model for 3–5.]

* **RULE 1 (the only one the shipped code needs).** Before ringing a DMA/engine
  **doorbell** that publishes a descriptor or buffer you just wrote, issue `MEMW`
  (the shipped idiom is `memw ; memw`) so the writes drain past the write buffer
  and are visible to the engine before the tail-pointer store launches it (§6).
  **Mandatory.**
* **RULE 2 (completion).** After launching, **poll** the completion descriptor's
  generation tag (busy-wait on byte3 bits[1:0], §6). A dependent read of the DMA'd
  data is correctly ordered after the poll because the poll-load and the data-load
  are program-order dependent on the same buffer.
* **RULE 3 (self-modifying / config).** After writing IRAM or a decode-affecting
  SR, issue `ISYNC` before fetching the affected code; after a `WSR` whose value a
  near instruction reads, issue `RSYNC` (§4.4).
* **RULE 4 (device registers).** To guarantee a device-register write has
  **committed** to the fabric (not just left the pipe), issue `EXTW` (drains the
  write buffer + waits the AXI write response, `PIF_WR_RESP=1`).
* **RULE 5 (atomic RMW, hypothetical).** If a future custom op shares a location
  with another master, use the `L32EX`/`S32EX` retry loop (§2.2) with `ATOMCTL`
  pre-set (by ring-0 firmware) to route exclusives to the AXI/ACE-Lite external
  monitor — the monitor self-fences via XTSYNC@6.

> **What a custom op must NOT assume:** that two stores to different SBUF/HBM
> addresses reach memory in program order without a fence; that an SBUF store is
> visible to the DMA engine the cycle after it issues; that a plain store is a
> release. All three are **false** under §5.5 and require `MEMW` / `S32RI`.
> [HIGH inference from the write-buffer model.]

### 8.1 The layered ordering stack

| layer | mechanism | who drives it | when |
|---|---|---|---|
| ISA fences | `MEMW`/`EXTW`/`DSYNC`/`ISYNC`/`RSYNC`/`ESYNC`/`EXCW` + `L32AI`/`S32RI` | kernel/compiler | every doorbell (`memw×2`) + ifetch |
| atomic RMW | `L32EX`/`S32EX` (+`GETEX`/`CLREX`), gated by `ATOMCTL` | kernel (**unused** in shipped code) | never, in shipped code |
| exclusive policy | `ATOMCTL` SR (per-memtype route) | firmware/LSP boot | once at init |
| mgmt override | `STRONG_ORDER` surprise (bit 9) | host/debugger | debug/bring-up, out-of-band |
| cross-core | NCFW `CORE_BARRIER` (counted semaphore, `PSEUDO_CORE_BARRIER 0xD8`) | NCFW mgmt core | collective phase brackets |

The custom-op kernel only ever touches the **top row** (and only its `MEMW`),
because the per-core-private SPMD design (`PRID`-indexed,
`sram0_0_seg = 0x84000000 + prid*0x200000`, no-lock heaps, no shared mutable
structure) removes the need for everything below it. The lower rows are proven
absent from `libneuroncustomop.a`. [HIGH/OBSERVED.]

---

## 9. Verification ledger

| # | claim | how grounded | verdict |
|---|---|---|---|
| 1 | 8 fences + 4 atomics + ATOMCTL + release-sync all ship | one `_Slot_inst_encode` thunk each in `libisa-core.so` + oracle round-trip (all 17 words) | **HIGH/OBSERVED** |
| 2 | exclusive monitor, **not** CAS | `HAVE_EXCLUSIVE=1`, `HAVE_S32C1I=0`; assembler **rejects** `s32c1i` ("unknown opcode", exit 1) | **HIGH/OBSERVED** |
| 3 | the `memw;memw` doorbell fence | literal `memw;memw` @`data_transfer.o:0x5c7/0x5ca` before doorbell store @`0x5cf` in `dma_data_transfer`; completion poll @`0x62c` | **HIGH/OBSERVED** |
| 4 | `ATOMCTL` = SR#99, field `[9:0]` (10-bit) + `[23:9]` const, privileged, `xt_sync` | encode word `(0x363>>8)=0x63`; `9:0:s:ATOMCTL:0` string; `stateArgs = MS_DISPST/PSRING/InOCDMode`; `_TIE_xt_sync_*_ATOMCTL` | **HIGH/OBSERVED** |
| 5 | weak/store-buffered, DRF-by-construction | `WRITEBUFFER=8`, `DCACHE_IS_COHERENT=0`, async drain; zero atomics/barriers in 10 customop objects; per-core-private SPMD | **HIGH/OBSERVED** |
| 6 | `L32EX`/`S32EX`/`GETEX`/`CLREX` data@5 + **XTSYNC@6**; `xt_core`; LSU unit | `libcas-core.so` `*_inst_stage5` (`nx_ScalarMemData*`) + `*_inst_stage6` (`my_XTSYNC_def(6)`); `funcUnit_uses = XT_LOADSTORE_UNIT`; `_TIE_xt_core_*` | **HIGH/OBSERVED** |
| 7 | `RSR.ATOMCTL` USE@4 / `art`@5; `WSR`/`XSR` DEF@6 (ATOMCTL+XTSYNC+WSRBus) | `libcas-core.so` `RSR_ATOMCTL_inst_stage4` (`my_ATOMCTL_use(4)`), `stage5` (AR def); `WSR_ATOMCTL_inst_stage6` (3 defs @6) | **HIGH/OBSERVED** |
| 8 | per-memtype `ATOMCTL` 2-bit field split | canonical Xtensa NX `ATOMCTL`; no `ATOMCTL` write in shipped code to bit-decode | **MED/INFERRED** |
| 9 | LL/SC success/fail-in-AR + granule semantics | opcodes/encodings/operand shape OBSERVED; the monitor contract is canonical NX | **MED/INFERRED** |
| 10 | `STRONG_ORDER` = surprises bit 9, non-terminal | carried DX-SEC-01 §4e (`sunda_handle_surprises @0x6cf4`) | **HIGH/OBSERVED (carried)** |

### Corrections and divergences recorded on this page

1. **`ATOMCTL` field width.** It is bits **[9:0] = 10 bits** with `[23:9]`
   constant, not a "9-bit" field (§3, CORRECTION). Grounded in
   `libisa-core.so` `9:0:s:ATOMCTL:0` + SX-ISA-05 entry 29 `[9:0:s:ATOMCTL:0,
   23:9:c]`.
2. **`RSR.ATOMCTL` XTSYNC.** The cas-core cycle model serialises the read via the
   **ATOMCTL** pipeline at stage 4, with **no** XTSYNC call on the read path — the
   "XTSYNC@4" in the B29 §3.9 opcode-DB annotation is the scheduler's USE-stage,
   not the realised barrier (§3.2, CORRECTION). `WSR`/`XSR.ATOMCTL` *do* assert
   XTSYNC@6.
3. **Fence barrier mechanism.** `MEMW`/`EXTW`/`DSYNC`/`RSYNC`/`ESYNC` do **not**
   route through the XTSYNC register pipeline in the cas-core model — XTSYNC is
   used only by the exclusives and SR writes. The `XTSYNC@2/3/4` / `MEMW@2` /
   `EXTW@8` figures are the opcode-DB scheduler USE-stages, a distinct layer
   (§2.3, §4.2 NOTE). Both views are consistent.
