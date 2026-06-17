# IOFIC Interrupt Model

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME `libnrt.so.1`, ELF64, **not stripped**, DWARF present; `.text` VMA == file offset, so every `0x47…`/`0x22…` is an analysis VMA). The IOFIC HAL is **vendored**: KaenaHal-2.31.0.0 `src/common/iofic/al_hal_iofic.c` (21 `al_iofic_*` fns) + `src/common/udma/al_hal_udma_iofic.c` (the `al_udma_iofic_*` policy layer), statically linked. Source attribution is by the burned-in `__FILE__`/`__LINE__` assertion literals (these vendored TUs carry no `.debug_line`; `addr2line` returns `:?`).*
> *Evidence grade: **Confirmed (byte-anchored)** — every register offset (`+0x00`/`+0x10`/`+0x18`/`+0x28`/`+0x30`), the `group << 6` stride, and the `~bits` write-1-to-unmask convention are read directly out of the `libnrt.so` disassembly. This page owns the **userspace** IOFIC view. The authoritative kernel register layout is owned by [kernel/udma-iofic](../kernel/udma-iofic.md) and is **not re-derived here**. Other versions will differ. · Part VIII — DMA & Descriptor Engine · [back to index](../index.md)*

## Abstract

The **IOFIC** — I/O Fabric Interrupt Controller — is the AnnapurnaLabs/Alpine UDMA interrupt aggregator, and this page documents the face of it that `libnrt.so` actually touches: a thin, leaf-level **register-poke HAL** of two-dozen `al_iofic_*` / `al_udma_iofic_*` functions, each of which does a NULL/range assert and then one or two MMIO operations against a per-group control window. There is no locking, no allocation, and no asynchrony anywhere in the cell — it is a pure MMIO shim sitting on top of the KaenaRuntime CSR accessors (`al_reg_read32`/`write32`/`poll32` in `tdrv/hal_platform.c`, a boundary out of this cell). A reader who knows any "interrupt controller with a per-source cause/mask/unmask register file" — a GIC distributor, an APIC, an MSI-X capability — already owns the frame. The IOFIC is that, organized **two levels deep**: a top-level ("primary") controller with four group summaries A–D, and a secondary controller where the individual UDMA error/abort causes live, funneled up into primary group D.

The decisive fact about the userspace IOFIC is **negative**: the runtime configures it but does not depend on it for completion. The whole `al_iofic_*` surface is wired for the **error / abort** path — parity faults, AXI timeouts, ring-id mismatches — where the policy layer (`al_udma_iofic_m2s_error_ints_unmask` / `_s2m_…` / `_config_ex`) programs each group's control register for posedge-latched, MSI-X-coalesced delivery, clears the relevant bits out of the per-group **abort mask** so the engine *aborts a faulting DMA*, and unmasks the cause so it can summarize into one MSI-X. Completion, by contrast, is never read off this controller. Even though the [16-byte descriptor](descriptor-format.md) carries an `INT_EN` bit, the M2M copy path observes completion by **polling a host-memory marker** (`0xabcdef01`), not by servicing a completion IRQ — the model owned end-to-end by [ring-cycle](ring-cycle.md). The IOFIC's `MASK_MSI_X` programming means a fired error *summary* becomes a single MSI-X for an error/notification handler outside this cell; the busy data path never blocks on it.

This page documents four artifacts a reimplementer must reproduce: (1) the **per-group register window** — a `0x40`-byte control block at `base + (group << 6)` with cause `+0x00`, mask `+0x10`, mask-clear `+0x18`, control `+0x28`, abort-mask `+0x30`; (2) the **two-level group model** — primary groups A–D (D is the funnel) and the secondary A/B/C error groups, with the `0x100/0x200/0x400/0x800` rev-keyed secondary-level summary bits; (3) the **register-poke primitives** — `config` / `unmask` (write `~bits`) / `mask` (RMW-OR) / `read_cause` / `clear_cause` (write `~bits`), each pinned to its symbol+addr+offset; and (4) the **policy composition** — the `config → abort_mask_clear → unmask` ordering the error-unmask entry points apply, with the `0x28` control flag (`SET_ON_POSEDGE | MASK_MSI_X`). The *bit semantics* of each secondary error cause (the M2S/S2M parity/AXI-timeout tables) are programmed from the kernel and owned by [kernel/udma-iofic](../kernel/udma-iofic.md); this page owns the userspace *mechanism*, not the kernel cause catalog.

For reimplementation, the contract is:

- **The group window** — each IOFIC group is a `0x40`-byte register block at `regs_base + (group << 6)`; the five fields the HAL touches are `INT_CAUSE@+0x00`, `INT_MASK@+0x10`, `INT_MASK_CLEAR@+0x18`, `INT_CONTROL@+0x28`, `INT_ABORT_MSK@+0x30`. The `<< 6` stride is byte-verified in every accessor (`shl rbx, 6`).
- **The write conventions** — `unmask` writes `~bits` to `INT_MASK_CLEAR` (set bit ⇒ unmask, atomic vs. HW auto-mask); `clear_cause` writes `~bits` to `INT_CAUSE` (W1C-complement); `mask` is a read-OR-write of `INT_MASK`. A reimplementer who writes `bits` (not `~bits`) inverts the entire controller.
- **The two-level model** — primary groups `{A=0, B=1, C=2, D=3}`; secondary groups `{A=0 (M2S err), B=1 (S2M err), C=2 (shared AXI parity)}`; primary group D is the funnel that summarizes the secondary controller. Group C of the secondary exists only on rev ≥ 4 (the only silicon shipped).
- **The error-only policy** — `config(grp, 0x28)` (posedge + MSI-X-coalesce) → `abort_mask_clear(grp, bits)` (enable abort-on-cause) → `unmask(grp, bits)`. Repeated per secondary group, then the primary D summary is unmasked. Completion is **not** in this path.
- **The negative invariant** — no `al_iofic_read_cause` / `read_and_clear_cause` call sits on the M2M completion path. Completion is the `0xabcdef01` host-marker poll ([ring-cycle](ring-cycle.md)); the IOFIC is initialized for faults and otherwise left alone.

| | |
|---|---|
| **Vendored from** | KaenaHal-2.31.0.0 — `al_hal_iofic.c` (21 fns, band `0x47c060`–`0x47d100`) + `al_hal_udma_iofic.c` (policy, `0x476120`–`0x4770d0`) |
| **Levels** | two — **primary** (groups A–D, D = secondary funnel) and **secondary** (groups A/B/C = M2S err / S2M err / shared AXI parity) |
| **Group window** | `0x40` bytes at `regs_base + (group << 6)` (`shl rbx, 6` in every accessor) |
| **Field offsets** | `INT_CAUSE +0x00` · `INT_MASK +0x10` · `INT_MASK_CLEAR +0x18` · `INT_CONTROL +0x28` · `INT_ABORT_MSK +0x30` |
| **Control flag** | `0x28` = `SET_ON_POSEDGE`(bit 3) `\|` `MASK_MSI_X`(bit 5) — group summary → one MSI-X |
| **Unmask convention** | `al_iofic_unmask` writes **`~bits`** to `INT_MASK_CLEAR` (`not esi`; set bit ⇒ unmasked) |
| **Clear convention** | `al_iofic_clear_cause` writes **`~bits`** to `INT_CAUSE` (W1C-complement) |
| **Abort wiring** | `al_iofic_abort_mask_clear` RMW-clears bits in `INT_ABORT_MSK@+0x30` ⇒ engine aborts on those causes |
| **Sec-level summary bits** | rev ≤ 3: `{0x100, 0x200, 0,    0}` @`0x855060`; rev > 3: `{0x100, 0x200, 0x400, 0x800}` @`0x855070` |
| **Completion role** | **none** — completion is the `0xabcdef01` host-marker poll ([ring-cycle](ring-cycle.md)); `INT_EN` descriptor bit unused on M2M |
| **CSR boundary** | `al_reg_read32@0x2658a0` / `al_reg_write32@0x265c50` / `al_reg_poll32@0x47d470` (`tdrv/hal_platform.c`, other cell) |

---

## 1. The Per-Group Register Window

### Purpose

Every IOFIC operation is an access to one *group's* `0x40`-byte control block, addressed as `regs_base + (group << 6)`. The HAL never models the controller as a struct; it computes the byte offset inline (`movsxd` the group, `shl …, 6`, `lea [base + grp*0x40 + field]`) and hands the address to a CSR accessor. Pinning these five offsets is the whole of the layout a userspace reimplementer needs — the surrounding two-level container geometry (where the secondary block sits, the `0x1c00` gap, the rev-4 struct map) is the kernel's authoritative map, owned by [kernel/udma-iofic](../kernel/udma-iofic.md).

### The window

The five fields the userspace HAL touches, each byte-verified against the disassembly of the accessor that uses it:

| Field | Offset | Accessor (addr) | Disasm proof |
|---|---|---|---|
| `INT_CAUSE` | `+0x00` | `al_iofic_read_cause` `0x47c970`, `al_iofic_clear_cause` `0x47cb70`, `al_iofic_poll_specific_cause` `0x47cae0` | `lea rdi, [rbp+rbx+0]` `@0x47c989` |
| `INT_MASK` | `+0x10` | `al_iofic_mask` `0x47c810`, `al_iofic_read_mask` `0x47c890`, `al_iofic_write_mask` `0x47c900` | `lea rbx, [r12+rbx+10h]` `@0x47c825` |
| `INT_MASK_CLEAR` | `+0x18` | `al_iofic_unmask` `0x47c7a0`, `al_iofic_unmask_offset_get` `0x47c720` | `lea rdi, [r12+rbx+18h]` `@0x47c7b7` |
| `INT_CONTROL` | `+0x28` | `al_iofic_config` `0x47c350`, `al_iofic_control_flags_get` `0x47c3d0`, `_set_on_posedge`/`_moder_*` | `lea rdi, [rbp+rbx+28h]` `@0x47c369` |
| `INT_ABORT_MSK` | `+0x30` | `al_iofic_abort_mask_clear` `0x47cfa0`, `al_iofic_abort_mask`/`_set`/`_read` | `lea rbp, [r12+rbp+30h]` `@0x47cfb5` |

The `group << 6` stride is identical in every one of these functions — `shl rbx, 6` after sign-extending the `int group` argument — so a group index of 0 lands at `regs_base+0`, group 1 at `+0x40`, group 3 (primary D) at `+0xc0`, and so on. The control-register bit layout the HAL exercises (the rest of the `0x40` window is rev/CSR detail in the kernel map):

```text
INT_CONTROL @ +0x28  (al_iofic_config writes this whole word; the moder/posedge fns RMW slices)
  bit 0       AUTO_CLEAR        ── al_iofic_read_and_clear_cause checks this: if SET, HW self-clears
  bit 3       SET_ON_POSEDGE    ── cause latches on rising edge   (al_iofic_control_set_on_posedge RMW)
  bit 5       MASK_MSI_X        ── group summary collapses to ONE MSI-X   (0x20)
  bits[23:16] legacy moder interval   (al_iofic_legacy_moder_interval_config)
  bits[27:24] moderation resolution   (al_iofic_moder_res_config)
  → 0x28 = (1<<3)|(1<<5) = SET_ON_POSEDGE | MASK_MSI_X  ── the flag every error-unmask entry writes
```

> **NOTE —** the `0x28` control word the policy layer writes is **posedge + MSI-X-coalesce**, not "enable interrupts". Configuring a group only sets *how* a fired cause is latched and delivered (edge-latched, summarized into a single MSI-X); the cause is still masked. Delivery requires the separate `unmask` step (§2). The DWARF `.debug_macro` independently carries the matching field names (`IOFIC_GRP_CTRL_INT_CONTROL_GRP_MASK_MSI_X_MASK 0x20`, `…_SET_ON_POSEDGE`, `…_AUTO_CLEAR`), confirming the Alpine layout without relying on the inline literal alone.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `al_iofic_handle_init` | `0x47c060` | init handle: store `regs_base@+0`, `group_num@+12` (≤ `AL_IOFIC_MAX_GROUPS`), derive `rev@+8 = (reg[+0x28]>>28)&3` | HIGH |
| `al_iofic_config` | `0x47c350` | write `INT_CONTROL@+0x28 = flags` (whole word) | HIGH |
| `al_iofic_control_flags_get` | `0x47c3d0` | read `INT_CONTROL@+0x28` | HIGH |
| `al_iofic_control_set_on_posedge` | `0x47c440` | RMW `INT_CONTROL` bit 3 | HIGH |
| `al_iofic_moder_res_config` | `0x47c4d0` | RMW `INT_CONTROL` bits[27:24] | HIGH |
| `al_iofic_legacy_moder_interval_config` | `0x47c560` | RMW `INT_CONTROL` bits[23:16] | HIGH |
| `al_iofic_unmask_offset_get` | `0x47c720` | return MMIO address of `INT_MASK_CLEAR@+0x18` | HIGH |

---

## 2. The Cause / Mask / Unmask Primitives

### Purpose

Above the offset arithmetic sit the five primitives the controller is actually driven with: arm a cause for delivery (`unmask`), suppress one (`mask`), read which fired (`read_cause`), acknowledge it (`clear_cause`), and the abort wiring (`abort_mask_clear`). Two of them — `unmask` and `clear_cause` — invert their argument before writing, which is the single easiest thing to get wrong in a reimplementation and the reason this section pins the exact instruction.

### Algorithm

```c
// The five userspace IOFIC register pokes. base = regs_base, grp scaled by <<6 (stride 0x40).
// Each does test(base)==NULL → assert/abort, then exactly one or two CSR ops.

// al_iofic_unmask — 0x47c7a0.  ARM causes for delivery.
function al_iofic_unmask(base, grp, bits):              // models 0x47c7a0
    addr = base + (grp << 6) + 0x18                     // INT_MASK_CLEAR  (lea …+18h @0x47c7b7)
    al_reg_write32(addr, ~bits)                         // not esi @0x47c7bc: SET bit => UNMASK
    // mask-CLEAR register, not mask: write is atomic vs HW auto-mask / a concurrent CPU.

// al_iofic_mask — 0x47c810.  SUPPRESS causes (read-modify-write OR).
function al_iofic_mask(base, grp, bits):                // models 0x47c810
    addr = base + (grp << 6) + 0x10                     // INT_MASK     (lea …+10h @0x47c825)
    al_reg_write32(addr, al_reg_read32(addr) | bits)    // or eax,ebp @0x47c835

// al_iofic_read_cause — 0x47c970.  WHICH causes fired (the ISR read; never on the data path).
function al_iofic_read_cause(base, grp):                // models 0x47c970  -> u32
    return al_reg_read32(base + (grp << 6) + 0x00)      // INT_CAUSE    (lea …+0 @0x47c989)

// al_iofic_clear_cause — 0x47cb70.  ACK fired causes (W1C-complement).
function al_iofic_clear_cause(base, grp, bits):         // models 0x47cb70
    addr = base + (grp << 6) + 0x00                     // INT_CAUSE
    al_reg_write32(addr, ~bits)                         // not esi @0x47cb8e: 0-bit clears the cause

// al_iofic_abort_mask_clear — 0x47cfa0.  ENABLE engine abort on selected causes (RMW).
function al_iofic_abort_mask_clear(base, grp, bits):    // models 0x47cfa0
    addr = base + (grp << 6) + 0x30                     // INT_ABORT_MSK   (lea …+30h @0x47cfb5)
    cur  = al_reg_read32(addr)
    al_reg_write32(addr, (bits & cur) ^ cur)            // and+xor @0x47cfc2/c7: CLEAR bits in abort mask
    // clearing an abort-mask bit ARMS abort: the UDMA aborts the engine when that cause fires.
```

> **GOTCHA —** `unmask` and `clear_cause` both write the **complement** of their `bits` argument (`not esi` at `@0x47c7bc` / `@0x47cb8e`), and `abort_mask_clear` *clears* the bits it is given out of a register named "mask". The three are all "set bit in arg ⇒ enable the behavior" at the call site, but the hardware semantics are inverted (a *clear* mask register, a *clear* abort-mask register). A reimplementer who writes `bits` directly to `INT_MASK_CLEAR` will unmask exactly the causes the caller meant to leave masked, and one who writes `bits` (not the RMW-clear) to `INT_ABORT_MSK` will disarm abort for every cause not named. Match the register: mask-set is `mask` (`+0x10`, RMW-OR); unmask is `mask-clear` (`+0x18`, `~bits`); cause-ack is `cause` (`+0x00`, `~bits`); abort-arm is `abort-msk` (`+0x30`, RMW-clear).

### The cause-iteration helpers (error/notification side only)

A small family snapshots all groups' `INT_CAUSE` and drains the set bits — used by an error/notification handler, **never** by the completion path. They are listed for completeness because a reimplementer of the error-IRQ side needs them, but no DMA copy ever calls them:

```c
// al_iofic_cause_iter_init (0x47c9e0) + _next (0x47c190): snapshot then tzcnt-drain.
function al_iofic_cause_iter_init(iofic):               // 0x47c9e0
    for g in 0 .. iofic.group_num:                      // group_num @ handle+0xc
        iofic.shadow[g] = al_iofic_read_cause(iofic.regs_base, g)   // shadow @ handle+0x10+4*g

function al_iofic_cause_iter_next(iofic, *out_group, *out_bit):     // 0x47c190 -> 0 when drained
    for g in current .. group_num:
        if iofic.shadow[g] != 0:
            bit = tzcnt(iofic.shadow[g])                // lowest set cause
            iofic.shadow[g] &= ~(1 << bit)              // clear it in the shadow
            *out_group = g; *out_bit = bit; return 1
    return 0
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `al_iofic_unmask` | `0x47c7a0` | arm: write `~bits` to `INT_MASK_CLEAR@+0x18` | HIGH |
| `al_iofic_mask` | `0x47c810` | suppress: RMW-OR `bits` into `INT_MASK@+0x10` | HIGH |
| `al_iofic_read_mask` / `al_iofic_write_mask` | `0x47c890` / `0x47c900` | read / overwrite `INT_MASK@+0x10` | HIGH |
| `al_iofic_read_cause` | `0x47c970` | read `INT_CAUSE@+0x00` | HIGH |
| `al_iofic_clear_cause` | `0x47cb70` | ack: write `~bits` to `INT_CAUSE@+0x00` | HIGH |
| `al_iofic_clear_cause_verify` | `0x47cbf0` | clear then re-read; `0xFFFFFFFB` if any bit survives | HIGH |
| `al_iofic_read_and_clear_cause` | `0x47cc80` | read cause; clear `cause & mask` unless `INT_CONTROL` bit 0 (AUTO_CLEAR) set | HIGH |
| `al_iofic_clear_cause_before_abort` | `0x47cd30` | read+clear+re-read with two diagnostic logs; returns surviving-abort mask | HIGH |
| `al_iofic_poll_specific_cause` | `0x47cae0` | `al_reg_poll32` on `INT_CAUSE` (error/notification, not completion) | HIGH |
| `al_iofic_abort_mask_clear` | `0x47cfa0` | RMW-clear `bits` in `INT_ABORT_MSK@+0x30` ⇒ arm abort | HIGH |
| `al_iofic_cause_iter_init` / `_next` | `0x47c9e0` / `0x47c190` | snapshot all groups' cause; tzcnt-drain set bits | HIGH |

---

## 3. The Two-Level Group Model

### Purpose

The IOFIC is two controllers stacked. The **primary** level has four group summaries A–D; group D is special — it is the funnel that summarizes the **secondary** level, where every individual UDMA error/abort cause physically lives. The userspace policy layer programs the secondary groups for the actual error causes and then unmasks primary group D so the secondary's summary propagates to the top. A reimplementer must reproduce this funnel discipline: unmasking a secondary cause is invisible at the top until primary D is also unmasked.

### The levels and groups

```text
PRIMARY level  (al_iofic group indices 0..3)
  group A = 0   ┐
  group B = 1   ├─ A/B/C = completion / summary groups — NEVER unmasked on the Neuron path
  group C = 2   ┘   (programmed only for ring-id-error suppression on the kernel side)
  group D = 3  ──── the FUNNEL: summarizes the SECONDARY controller up to the top level

SECONDARY level (al_iofic group indices 0..2, indexed off the secondary base)
  group A = 0  ──── M2S (Tx) error causes        (m2s_error_ints_unmask drives this)
  group B = 1  ──── S2M (Rx) error causes        (s2m_error_ints_unmask drives this)
  group C = 2  ──── shared M2S+S2M AXI-fifo parity  (rev >= 4 only — the only silicon shipped)
```

The secondary-level **summary bit** a group contributes upward is a rev-keyed `.rodata` table, byte-decoded from the binary. `al_udma_iofic_sec_level_int_get` (`0x476e40`) selects the table by `udma->rev_id` (`cmp dword [rbp+0x1880], 3` `@0x476e72`) and indexes it by group:

| `rev_id` | Table addr | Group A | Group B | Group C | Group D |
|---|---|---|---|---|---|
| ≤ 3 | `0x855060` | `0x100` | `0x200` | `0` (asserts) | `0` (asserts) |
| > 3 | `0x855070` | `0x100` | `0x200` | `0x400` | `0x800` |

> **QUIRK —** on `rev_id ≤ 3` the secondary-level table returns `0` for groups 2 and 3, and `al_udma_iofic_sec_level_int_get` then **fails the `bit != 0` assertion** (`"bit"` literal `@0x476e83`) and aborts. The two extra summary bits (`0x400`/`0x800`) and the secondary group C only exist on rev > 3. A reimplementer who hard-codes a 4-entry table for all revisions will program a non-existent group-C summary on older silicon. The valid-group rule is rev-gated: secondary group count is 2 below rev 4, 3 at rev ≥ 4.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `al_udma_iofic_sec_level_int_get` | `0x476e40` | rev-keyed secondary-level summary bit for a group (`0x855060`/`0x855070`) | HIGH |
| `al_udma_iofic_get_ext_app_bit` | `0x476dd0` | the external-app cause bit consumed by `unmask_ext_app` (rev/arch-keyed) | HIGH |
| `al_udma_iofic_unmask_ext_app` | `0x476f70` | unmask the ext-app bit on primary group D; optional `clear_cause(grp3)` first | HIGH |

---

## 4. The Error-Unmask Policy Composition

### Purpose

The policy layer (`al_hal_udma_iofic.c`) composes the §2 primitives into the actual bring-up sequence. Its shape is invariant across the three entry points: for each group it must arm, **configure** the control register for posedge + MSI-X-coalesce, **clear the abort mask** so the engine aborts on those causes, then **unmask** the causes — and finally lift the primary-D funnel. This is the only place the IOFIC is programmed for real work, and it is exclusively the *error/abort* path.

### Algorithm

```c
// al_udma_iofic_error_ints_unmask_one — 0x4770d0.  The minimal one-group composition.
// Used to blanket-enable a single raw group-0 control block (the gen-space AXI sub-IOFICs).
function al_udma_iofic_error_ints_unmask_one(ctrl, bits):    // models 0x4770d0
    al_iofic_config(ctrl, /*group=*/0, 0x28)                 // edx=0x28 @0x4770d7: POSEDGE|MSI_X
    al_iofic_abort_mask_clear(ctrl, 0, bits)                 // @0x4770ee: arm abort on `bits`
    al_iofic_unmask(ctrl, 0, bits)                           // @0x477100: arm delivery for `bits`

// al_udma_iofic_m2s_error_ints_unmask — 0x476650.  The Tx-error bring-up (s2m mirror @0x476a00).
// Secondary group A (M2S errors) + group C (shared parity), then primary group D funnel.
function al_udma_iofic_m2s_error_ints_unmask(udma):          // models 0x476650
    sec  = udma.gen_int_regs.secondary                       // secondary IOFIC base
    prim = udma.gen_int_regs.primary
    al_iofic_config(sec,  /*A=*/0, 0x28)                     // @0x47668d  POSEDGE|MSI_X
    al_iofic_config(prim, /*D=*/3, 0x28)                     // @0x4766a4/a9 (esi=3)
    al_iofic_abort_mask_clear(sec, 0, M2S_GROUP_A_ERROR_BITS)   // edx=0x0FFFFFFF @0x4766c9: arm abort
    al_iofic_abort_mask_clear(sec, 2, M2S_GROUP_C_ERROR_BITS)   // group C (esi=2), edx=0x3F @0x4767b3
    al_iofic_unmask(sec, 0, M2S_GROUP_A_ERROR_BITS)            // @0x4766f6  arm group-A causes
    al_iofic_unmask(sec, 2, M2S_GROUP_C_ERROR_BITS)           // @0x476742  arm group-C causes (esi=2)
    al_iofic_unmask(prim, 3, sec_level_int_get(udma, ...))    // @0x476716/18 (esi=3): lift D funnel
```

> **NOTE —** the exact cause-bit *masks* the userspace policy writes (`0x0FFFFFFF` for secondary group A, `0x3F` for group C, the per-bit meanings of each) are the same parity/AXI-timeout/ring-id catalog the **kernel** driver programs verbatim, and that catalog — the M2S/S2M error-bit tables, the ring-id-mismatch suppression, the secondary-group-C split — is owned authoritatively by [kernel/udma-iofic](../kernel/udma-iofic.md). This page does not re-derive those bit meanings; it pins the userspace *mechanism* (which group, which register, which order) and treats the masks as opaque catalog values supplied by the policy layer. The `al_udma_iofic_config_ex` entry (`0x476120`) is the fuller variant: it configures **all four** primary groups 0–3 (`xor esi,esi` then `esi=1,2,3` `@0x476191`–`@0x4761d3`) before unmasking/abort-arming each — the complete-init path, versus the m2s/s2m error-only paths.

> **NOTE — completion is polled, not interrupt-driven.** No function in this cell reads `INT_CAUSE` on the DMA completion path. The M2M copy engine has **no hardware completion queue** (`cdesc_base == NULL` on every Neuron M2M queue); completion is observed by polling a 4-byte host-memory marker — the sentinel `0xabcdef01` written by a self-copy descriptor staged last, busy-polled every 1 µs (kernel) / 10 µs (userspace software path) against a host word. The [16-byte descriptor](descriptor-format.md) *has* an `INT_EN` bit and the IOFIC *is* configured with `MASK_MSI_X` so an error summary can raise one MSI-X, but the runtime deliberately chooses the host-marker poll for completion because a small host↔device copy finishes in microseconds and an interrupt round-trip would dominate. The IOFIC is initialized for **faults** (parity, AXI timeout, ring-id, abort-on-error) and is otherwise off the hot path. The full completion model — the marker, the poll budget, the recycle CAS — is owned by [ring-cycle](ring-cycle.md).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `al_udma_iofic_config_ex` | `0x476120` | full init: config all 4 primary groups, then unmask + abort-arm each | HIGH |
| `al_udma_iofic_m2s_error_ints_unmask` | `0x476650` | Tx-error bring-up: sec grp A + C, then primary D funnel | HIGH |
| `al_udma_iofic_s2m_error_ints_unmask` | `0x476a00` | Rx-error mirror: sec grp B + C, then primary D funnel | HIGH |
| `al_udma_iofic_error_ints_unmask_one` | `0x4770d0` | minimal one-group `config(0x28)→abort_clear→unmask` on a raw block | HIGH |
| `al_udma_iofic_unmask_offset_get_adv` | `0x476550` | resolve the `INT_MASK_CLEAR` MMIO address for a (level, group) | HIGH |

---

## 5. Considerations

- **The error/abort role is the whole role.** A reimplementer who wires the IOFIC into the completion path will find it never fires there — the engine writes a host marker, not a completion-queue entry, and no `read_cause` sits on that path. The controller exists to *abort a faulting DMA* and summarize one MSI-X to an error/notification handler outside this cell; it is configured at `udma_init` and then left alone by the data path.
- **The write conventions are the trap.** Two registers are *clear* registers (`INT_MASK_CLEAR@+0x18`, `INT_ABORT_MSK@+0x30`) and two primitives write the *complement* of their argument (`unmask`, `clear_cause`). The call-site convention ("set bit in arg ⇒ enable") is uniform; the register-level semantics are inverted. Get the register/inversion pairing wrong and the controller is precisely backwards.
- **The funnel must be lifted.** Unmasking a secondary cause is invisible until primary group D is also configured and unmasked — every error-unmask entry ends with the primary-D step for exactly this reason. The rev-keyed secondary-level summary bits (`0x100`/`0x200`/`0x400`/`0x800`) are what D's mask must carry; the two high bits and secondary group C exist only on rev ≥ 4.
- **The CSR accessors are a boundary, not characterized here.** `al_reg_read32`/`write32`/`poll32` (`tdrv/hal_platform.c`, `al_reg_read32@0x2658a0`) are the actual MMIO/PCIe touch with the volatile-load + `"PCIe read failed %p"` fault path; the IOFIC HAL is a pure leaf above them. Their barrier/volatile semantics are owned by the platform CSR layer, not this cell.
- **The userspace masks are policy, the kernel masks are the catalog.** The userspace policy layer writes coarse masks (`0x0FFFFFFF`, `0x3F`) that select swaths of the secondary cause space; the *per-bit meaning* of each parity/AXI-timeout/ring-id cause is the kernel's authoritative table ([kernel/udma-iofic](../kernel/udma-iofic.md)). Treat the userspace mechanism (this page) and the kernel cause catalog (that page) as the two halves of the IOFIC story, reconciled at the group/level/offset model both share.

---

## Related Components

| Name | Relationship |
|---|---|
| `al_iofic_config` / `_unmask` / `_mask` / `_read_cause` / `_clear_cause` | the five register-poke primitives, one MMIO op each |
| `al_iofic_abort_mask_clear` (`0x47cfa0`) | RMW-clears `INT_ABORT_MSK@+0x30` to arm engine abort on a cause |
| `al_udma_iofic_m2s_error_ints_unmask` / `_s2m_…` / `_config_ex` | the policy composition: `config(0x28) → abort_clear → unmask`, then lift primary D |
| `al_udma_iofic_sec_level_int_get` (`0x476e40`) | rev-keyed secondary-level summary bit (`0x855060`/`0x855070`) |
| `al_reg_read32` / `write32` / `poll32` (`tdrv/hal_platform.c`) | the CSR accessor boundary the HAL bottoms out at (other cell) |

## Cross-References

- [UDMA IOFIC Interrupt Controller](../kernel/udma-iofic.md) — the **authoritative** kernel register layout: the two-level container geometry (primary `0x400`, secondary `@0x2000`), the per-bit M2S/S2M/group-C error cause catalog, ring-id-error suppression, and the rev-4 struct map this userspace page treats as opaque
- [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md) — why completion is polled rather than interrupt-driven: the `0xabcdef01` host-marker model, the per-arch poll budget, and the unused `INT_EN` descriptor bit
- [DMA & Descriptor Engine — Part Map](overview.md) — the unified DMA frame; places the IOFIC as the interrupt controller that is initialized for errors but kept off the completion path
- [UDMA Main and Queue Management](../kernel/udma-main.md) — `udma_init`, the `struct udma` (`gen_int_regs@+0x50`, `rev_id@+0x1880`) the policy layer dereferences, and where the error-unmask entry points are called during engine bring-up
- [back to index](../index.md)
