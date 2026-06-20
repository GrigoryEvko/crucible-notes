# CSR — Xtensa NX (NX-vs-Q7 diff)

> **Scope.** This page documents the **`xtensa_nx`** NeuronX-core CSR aperture — the
> Cadence Tensilica on-chip-debug / TRAX-trace / performance-monitor / OCD / CoreSight
> register block — **as a byte-exact diff against the `xtensa_q7` baseline**, not as a
> standalone dump. The Q7 block is the full reference and is tabulated on its own page
> ([CSR — Xtensa Q7](xtensa-q7.md)); here we enumerate **only the deltas** where NX
> diverges, plus the small set of identifying registers (CFGIDs, ID-block) needed to
> prove the two cores are distinct Xtensa configurations. Everything not listed as a
> delta is **inherited unchanged** from Q7 and must be read there.
>
> Both descriptors are the shipped, binary-derived `cayman-arch-regs` CSR JSON
> (`csrs/xtensa_nx/xtensa_nx.json`, `csrs/xtensa_q7/xtensa_q7.json`); they are SVD-style
> register maps consumed at build time by the GPSIMD HAL generator and are therefore
> citeable as static-analysis artifacts. All counts, offsets, positions, access types,
> and reset values below were re-derived from scratch by exhaustive enumeration of the
> two JSON trees (`RegFile → RegistersBundleArrays → Registers → BitFields`) — the diff
> rests on the schema, not on any prior report.

**Applicability.** Cayman / NC-v3 (the schema's own target). The two-core split this
diff exposes is the Cayman generation's GPSIMD complex. Carry-forward to later silicon
(v5) is **INFERRED** — flagged inline where it appears; no v5 CSR JSON is present in the
shipped descriptor set, so every v5 statement is extrapolation, not observation.

**Confidence tags.** `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`. *OBSERVED* = a literal
JSON value (offset/name/position/access/most resets). *INFERRED* = architectural reading
beyond the JSON text. *CARRIED* = asserted true for v5 by continuity, not re-measured.

---

## 1. What NX *is*, relative to Q7

`xtensa_nx` and `xtensa_q7` are **two Cadence-generated Xtensa debug/trace/PMU register
maps** packaged side-by-side in the same `cayman-arch-regs` bundle. They are built from
the **same Cadence debug/perf IP** — identical bundle layout, identical OCD-module
configuration, identical CoreSight ROM identity, identical 8-counter PMU — and differ in
exactly three places that the silicon forced apart: the **debug-instruction-register
width** (OCD `DIR` word count), the **ECC/RAS fault-reporting surface** (`FAULTINFOHI`),
and the **TRAX configuration hash** (`TRAXID.CFGID`). Those three deltas are the entire
NX-vs-Q7 difference at the register level.

The firmware front-end runs a **two-engine split**: a scalar **SEQ / management** core and
a wide **compute / vector** core. The diff in §4 maps cleanly onto that split —
**NX is the SEQ-side core**, Q7 the compute-side core — and the `DIR`-width delta is the
single strongest piece of register-level evidence for it. The NX core is the engine the
**SEQ firmware drives**: the SEQ main loop (see
[SEQ engine main loop](../../firmware/seq/main-loop.md)) and the
[dual-fetch front-end](../../firmware/seq/dual-fetch.md) issue against this exact debug
surface (DSR/DIR injection, TRAX trigger PC, PMU counter selects). The per-engine
`*_LOCAL_REG` aperture that sits *in front of* this Xtensa block is documented at
[`tpb_xt_local_reg`](tpb-xt-local-reg.md).

`RegFile` scalars (both cores, OBSERVED HIGH):
`DataWidth=32`, `AddrWidth=14`, `SizeInBytes=0x4000`, `InterfaceType=APB`,
`Type=REGFILE`, `RegfileFlavor=POSEDGE`. The 14-bit address width and `0x4000` size are
**explicitly populated in both** NX and Q7 descriptors; the address span `0x3F00 + 0x100
= 0x4000` is exactly covered (CoreSight ends the map at `0x3FFC`). The APB interface and
`POSEDGE` flavor are byte-identical between cores — the **bus wrapper is shared**; only
the register *contents* differ.

> **CORRECTION (vs SX-CSR-03 §SCHEMA note).** SX-CSR-03 states the Q7 descriptor "left
> the same `RegFile` scalars null" while NX populated them. Re-reading
> `xtensa_q7.json` directly, **both** cores carry `SizeInBytes="0x4000"`,
> `AddrWidth="14"`, `RegfileFlavor="POSEDGE"`, `InterfaceType="APB"` populated and
> identical. There is **no descriptor-metadata asymmetry** in the top-level scalars — the
> `0x4000` size is explicit for *both*. The substantive deltas are exactly the three in
> §4. (MED × OBSERVED — re-grounded on the raw JSON.)

---

## 2. Counts — NX vs Q7 (re-derived from the JSON)

Re-enumerated from both trees independently. The sibling Q7 page reports **78 / 296 / 5**;
that re-derives **exactly** here (no Q7 correction). NX is strictly smaller.

| Metric | **NX** | Q7 (baseline) | Δ (NX − Q7) |
|---|---|---|---|
| Bundles | **5** | 5 | 0 |
| Registers (total) | **73** | 78 | **−5** |
| Bitfields (total) | **275** | 296 | **−21** |
| Register access RW / RO | **57 / 16** | 62 / 16 | −5 RW / 0 RO |
| Field access RW / RO | **143 / 132** | 147 / 149 | −4 RW / −17 RO |
| Address span | `0x4000` | `0x4000` | 0 |

**Per-bundle register / field counts** (NX, OBSERVED HIGH):

| Bundle | Base | Size | **NX regs** | Q7 regs | **NX fields** | Q7 fields | Delta |
|---|---|---|---|---|---|---|---|
| `Trax_Registers` | `0x0000` | `0x1000` | 12 | 12 | 63 | 63 | none (1 reset Δ) |
| `Performance_Monitor_Registers` | `0x1000` | `0x1000` | 26 | 26 | 107 | 107 | identical |
| `OCD_Registers` | `0x2000` | `0x1000` | **11** | 15 | **66** | 70 | **−4 regs / −4 fields** |
| `Miscellaneous_Registers` | `0x3000` | `0x0F00` | **4** | 5 | **19** | 36 | **−1 reg / −17 fields** |
| `CoreSight_Registers` | `0x3F00` | `0x0100` | 20 | 20 | 20 | 20 | identical |

**Reconciliation (all exact):** `78 − 5 = 73` regs ✓ · `296 − 21 = 275` fields ✓ ·
`147 − 4(DIR) = 143` RW fields ✓ · `149 − 17(FAULTINFOHI) = 132` RO fields ✓.
No within-bundle offset collisions (each bundle: `#regs == #unique offsets`); every
register has ≥1 bitfield (no empty registers). (OBSERVED HIGH.)

> The 8-bit `RESERVED` padding fields are counted as bitfields throughout (they carry
> `Position`/`AccessType`/`ResetValue` records in the JSON), so "fields" here means *all*
> `BitFields` entries, not just named-function bits. This convention matches the Q7 page.

---

## 3. The diff is a strict subset: NX = Q7 minus five registers

The headline result, stated precisely:

- **NX-only registers: 0.** NX adds **nothing** Q7 lacks.
- **NX-only fields: 0.** No new bits anywhere.
- **Q7-only registers: 5** — `OCD/DIR4`, `OCD/DIR5`, `OCD/DIR6`, `OCD/DIR7`,
  `Misc/FAULTINFOHI`.
- **Q7-only fields: 21** — the 4 `DIRn` words (RW) + the 17 `FAULTINFOHI` bits (RO).
- **Shared-but-changed: exactly 1 field** — `Trax/TRAXID/CFGID` reset value.
- **Shared register offset/access changes: 0.** Every one of the 73 common registers has
  a **byte-identical absolute offset and `AccessType`** in both cores.

So NX is **Q7 with two amputations and one reconfigured ID hash** — nothing grafted on.
This is the cleanest possible diff shape: a strict register-set subset plus a single reset
delta. (OBSERVED HIGH, by full set/record comparison of both JSON trees.)

---

## 4. The delta table (the page's spine)

### 4a. Q7-only registers absent from NX (the amputations)

| Reg | Bundle | Abs (in Q7) | Access | Fields | NX status | Meaning |
|---|---|---|---|---|---|---|
| `DIR4` | OCD | `0x2030` | RW | 1 (`DIR4` \[31:0\]) | **absent** | Debug-instr word 4 |
| `DIR5` | OCD | `0x2034` | RW | 1 (`DIR5` \[31:0\]) | **absent** | Debug-instr word 5 |
| `DIR6` | OCD | `0x2038` | RW | 1 (`DIR6` \[31:0\]) | **absent** | Debug-instr word 6 |
| `DIR7` | OCD | `0x203c` | RW | 1 (`DIR7` \[31:0\]) | **absent** | Debug-instr word 7 |
| `FAULTINFOHI` | Misc | `0x3030` | RW\* | 17 (all RO) | **absent** | per-RAM ECC/RAS breakdown |

\* `FAULTINFOHI`'s *register-level* `AccessType` is `RW` in the Q7 JSON, but **all 17 of
its bitfields are RO** — it is a read-only status surface; the register-level RW is a
generator artifact. The 17 fields (Q7-only, OBSERVED HIGH):

| Bit | Field | Bit | Field | Bit | Field |
|---|---|---|---|---|---|
| 0 | `DRamCorr` | 9 | `ICacheCorr` | 15 | `DPRmDrtUnc` |
| 1 | `DRamUnc` | 10 | `ICacheRefl` | 16 | `IPRmCorr` |
| 2 | `IRamCorr` | 11 | `ICacheUnc` | 17 | `IPRmClnRfl` |
| 3 | `IRamUnc` | 12 | `DPRmCorr` | 18 | `IPRmClnUnc` |
| 8:4 | `RESERVED0` | 13 | `DPRmClnRfl` | 19 | `ECCTstMde` |
| — | — | 14 | `DPRmClnUnc` | 31:20 | `RESERVED1` |

These name correctable / uncorrectable / refill / clean / dirty ECC conditions across
**DataRAM, InstrRAM, ICache, D-prefetch-RAM and I-prefetch-RAM**, plus an ECC-test-mode
bit. NX exposes the **summary** fault register (`FAULTINFOLO`, identical in both — see §5)
but **not** this per-array ECC itemisation.

### 4b. NX-only registers / fields

**None.** (OBSERVED HIGH — `NX-only regs: []`, `NX-only fields: 0`.) The brief asked
whether NX adds anything — extra PMU events, a wider trace, an NX-specific debug path. The
descriptor says **no**: NX introduces zero registers and zero bitfields that Q7 lacks. The
NX core is a *reduction* of the Q7 debug surface, never an extension of it.

### 4c. Shared-but-changed — the single field delta

| Reg.field | Position | Access | **NX reset** | Q7 reset | Δ |
|---|---|---|---|---|---|
| `Trax/TRAXID/CFGID` | `15:0` | RO | **25088 (`0x6200`)** | 27136 (`0x6A00`) | reset only |

Position (`15:0`), access (`RO`), and absolute offset (`0x0000`) are **unchanged**; the
**only** difference is the reset value. This is the *sole* bit-pattern delta among the 73
shared registers. (OBSERVED HIGH.)

### 4d. Shared register offset / access changes

**None.** Every common register lands at a byte-identical absolute offset with an
identical `AccessType`. The two maps are layout-identical wherever they overlap. (OBSERVED
HIGH.)

---

## 5. What NX inherits unchanged (do not re-tabulate — see the Q7 page)

The following are **byte-identical** between NX and Q7 and are fully tabulated on
[CSR — Xtensa Q7](xtensa-q7.md). Listed here only to bound the diff:

- **Trax bundle (12 regs / 63 fields)** — identical *except* `TRAXID.CFGID` (§4c). The
  shared invariants that prove the trace hardware is the same size: `TRAXSTAT.MEMSZ`
  reset = **13** in both → TraceRAM = 2¹³ = **8 KB** in both cores; `TRAXCTRL.ATID6_1`
  reset = **60 (`0x3C`)** in both. So the **CFGID delta is *not* a trace-size delta** — the
  TraceRAM is the same 8 KB on both cores; CFGID differs on some other TRAX config knob.
  `TRAXADDR`/`TRAXDATA`/`TRIGGERPC`/`PCMATCHCTRL`/`DELAYCNT`/`MEMADDRSTART`/`MEMADDREND`/
  `EXTTIMELO`/`EXTTIMEHI` are field-for-field identical. (OBSERVED HIGH.)
  - *Note (both cores):* `EXTTIMELO`/`EXTTIMEHI` carry register-level `RW` but a single
    `RO` `[31:0]` field — read-only external-time taps, RW at register granularity is a
    generator artifact (same pattern as `FAULTINFOHI`).
- **Performance_Monitor bundle (26 regs / 107 fields)** — **identical, every bit.**
  `PMG` (global enable) + `INTPC` + `PM0..PM7` (8 counters) + `PMCTRL0..7` + `PMSTAT0..7`.
  The PMU is the **standard 8-counter Xtensa PMU**, shared verbatim. (OBSERVED HIGH.)
- **OCD bundle — the 11 shared regs** — `OCDID` (`CFGID=1538`, `MAJVER=4`, `MINVER=1` —
  **identical in both**), `DCRCLR`/`DCRSET` (14 fields each), `DSR` (all 25 fields
  identical), `DDR`, `DDREXEC`, `DIR0EXEC`, `DIR0..DIR3`. The amputation is purely the
  *upper* `DIR` half (§4a). (OBSERVED HIGH.)
- **Miscellaneous bundle — the 4 shared regs** — `PWRCTL`, `PWRSTAT` (`reserved`
  reset = 4369 = `0x1111`, the PSO tie-high pattern), `ERISTAT`, `FAULTINFOLO` (7 fields:
  `UserCode`, `HaltCode`, `TE`, `DE`, `Halted`, `PFatalError` + reserved — **identical**).
  (OBSERVED HIGH.)
- **CoreSight bundle (20 regs / 20 fields)** — **identical names, offsets, access AND
  reset values**, including the full ID block:
  `Peripheral_ID0=0x03 ID1=0x21 ID2=0x0f ID3=0x00 ID4=0x24 ID5..7=0x00`,
  `Component_ID0=0x0d ID1=0x90 ID2=0x05 ID3=0xb1`. Decodes to part `0x103`, JEP-106
  continuation 4 / code `0x72` = **Tensilica/Cadence** designer, 4×4 KB block count. Both
  cores present the **same CoreSight ROM identity** to an external ROM-table walker — they
  are distinguished by their internal `TRAXID`/`DIR`/`FAULTINFO` surfaces, *not* by their
  CoreSight PIDR. (OBSERVED HIGH for names/offsets/access; ID decode MED × INFERRED.)

> **Reset-value caveat (both cores, CARRIED from the Q7 page).** Eight CoreSight
> management registers — `ITCTRL`, `CLAIMSET`, `CLAIMCLR`, `LOCKACCESS`, `LOCKSTATUS`,
> `AUTHSTATUS`, `DEVID`, `DEVTYPE` — all carry reset `0x000000b1`, the same byte as the
> legitimate `Component_ID3=0xB1`. This is a **generator-default placeholder** (no real
> reset specified, `0xB1` reused), present *identically* in NX and Q7 — **there is no
> NX-specific placeholder anomaly.** Treat those eight resets as UNVERIFIED (LOW); their
> offsets / names / access are HIGH. (A naive grep finds "9" `0xb1` registers per core, but
> the 9th is the genuine `Component_ID3` — the placeholder set is the same 8 in both.)

---

## 6. Why each delta exists — the architectural reading

### 6a. `DIR4..DIR7` absent → NX is the **narrow / scalar** core (D1, strongest signal)

`DIRn` (Debug Instruction Register words) hold the instruction a *stopped* core executes
via `DDREXEC` / `DIR0EXEC` injection. The number of `DIR` words is the **maximum
instruction-word length the core can be asked to single-step-inject** — a direct proxy for
the core's native instruction-encoding width.

- **Q7: 8 `DIR` words = 256 bits.** A wide-SIMD / compute datapath whose widest debug-
  injected instruction spans many 32-bit parcels (Vision-class FLIX/VLIW bundles).
- **NX: 4 `DIR` words = 128 bits.** A **narrower** encoding — consistent with a scalar
  SEQ / management core whose instruction words never exceed 128 bits.

This is the **single strongest architectural signal in the whole diff** and it directly
supports the SEQ-vs-compute two-engine split: instruction-injection width tracks
instruction-encoding width, and a wide compute core genuinely *needs* more `DIR` parcels
than a scalar control core. (Delta OBSERVED HIGH; the width→datapath reading INFERRED,
MED.) The SEQ-side relationship is exactly why the
[SEQ main loop](../../firmware/seq/main-loop.md) and
[dual-fetch front-end](../../firmware/seq/dual-fetch.md) target the NX debug surface: SEQ
fetches and steps **scalar** management code, so 4 `DIR` words suffice.

### 6b. `FAULTINFOHI` absent → NX has a **smaller local-memory ECC surface** (D2)

`FAULTINFOHI` is the per-array ECC/RAS breakdown (DataRAM / InstrRAM / ICache /
D-prefetch / I-prefetch correctable+uncorrectable+refill flags). NX keeps the **summary**
`FAULTINFOLO` (halt / fatal-error / user / halt code) but drops the itemised breakdown.

A **compute** core (Q7) carries large `DataRAM` / `IPref` / `DPref` arrays — more ECC
surface worth itemising. A **control** core (NX/SEQ) either has fewer/smaller local RAM
arrays, or centralises its ECC reporting elsewhere, so the per-array register is not
instantiated. This **corroborates** the split — more compute RAM ⇒ more ECC surface — but
is weaker than §6a: *absence* of a reporting register is softer evidence than a datapath-
width signal. (Delta OBSERVED HIGH; the reading INFERRED, LOW→MED.)

### 6c. `TRAXID.CFGID` 0x6200 vs 0x6A00 → the cores are **distinct Xtensa configs** (D3)

`TRAXID.CFGID` is a **Cadence-assigned hash of the exact TRAX configuration** ("unique for
each possible configuration of the debug Module" — the descriptor's own text). Two
*different* CFGIDs **prove** NX and Q7 are distinct Xtensa configurations, not the same
core mapped twice. Critically, `OCDID.CFGID` is **identical (1538)** in both → the two
cores share the **same OCD-module configuration** and differ only in their TRAX config
(and, per §6a/§6b, in `DIR` width and ECC surface). Since `MEMSZ=13` (8 KB) in both, the
CFGID delta reflects some non-trace-size TRAX knob. (Delta OBSERVED HIGH; the "distinct
configs" conclusion INFERRED but near-certain, HIGH.)

### 6d. PMU / CoreSight identical → same Cadence debug/perf IP

Identical PMU (8 counters, same `SELECT` width `[12:8]` = 5 bits → same max event-category
space) and identical CoreSight identity show the two cores are **built from the same
Cadence debug/perf IP**, differentiated *only* where the datapath forces it (`DIR` width)
or where local memory differs (ECC). The descriptors do **not** enumerate the concrete
PMU `SELECT`/`MASK` event *catalogue*, so a per-core event-*list* difference can neither be
confirmed nor denied here — on the JSON's own evidence the PMU is **shared verbatim**.
(OBSERVED HIGH for layout; event-catalogue equivalence LOW.)

> **Caveat on "vector debug surface".** Neither core's CSR map contains any *named*
> vector/Vision-P debug register (no `VEC*` / Vision-specific debug regs in either JSON).
> So the diff does **not** show "NX lacks a Vision debug surface that Q7 has" at the
> register-name level — that surface is simply not modelled in these descriptors. The
> scalar-vs-vector distinction surfaces **only indirectly**, via the `DIR`-word count
> (§6a) and ECC breadth (§6b). Calling Q7 the "vector" core is an **inference** from those
> two signals, not from any named vector register. (LOW × INFERRED.)

---

## 7. Reimplementation notes

### 7a. Discriminating NX from Q7 at runtime

A debugger walking the CSR aperture can identify which core it is attached to **without a
chip-ID strap**, using only the deltas above. Three orthogonal probes, cheapest first:

```c
/* Identify an attached Xtensa debug aperture as NX (SEQ/scalar) or Q7 (compute).
 * `base` is the APB window base of the xtensa_* RegFile (0x4000 span).
 * All reads are RO ID fields; no side effects. Returns 1=NX, 0=Q7, -1=unknown. */
static inline uint32_t csr_rd(uintptr_t base, uint32_t off) {
    return *(volatile uint32_t *)(base + off);   /* APB, 32-bit, POSEDGE */
}

int xtensa_core_is_nx(uintptr_t base) {
    /* Probe 1 (definitive): TRAXID.CFGID, abs 0x0000, bits [15:0].
     * NX = 0x6200, Q7 = 0x6A00. Single read, no state. */
    uint32_t cfgid = csr_rd(base, 0x0000) & 0xFFFFu;
    if (cfgid == 0x6200u) return 1;   /* NX  */
    if (cfgid == 0x6A00u) return 0;   /* Q7  */

    /* Probe 2 (corroborating): DIR7 presence at abs 0x203C.
     * Q7 instantiates DIR4..DIR7; NX stops at DIR3 (abs 0x202C). A write/read
     * round-trip to 0x203C reads back the written word on Q7; on NX the address
     * is unmapped within the OCD bundle and reads back 0 (or aborts on APB).
     * Only meaningful when DSR.Stopped is set (DIR is writable only then). */
    /* (left to the caller: gated on OCD-mode + DSR.Stopped, abs DSR=0x2010 bit4) */

    /* Probe 3 (corroborating): FAULTINFOHI presence at abs 0x3030.
     * Present on Q7 (17 RO ECC bits), absent on NX. */

    return -1;  /* OCDID/CoreSight are identical and cannot discriminate */
}
```

`OCDID.CFGID` (`0x2000`, =1538 on both) and the entire CoreSight ID block are **identical**
and **cannot** distinguish the cores — only `TRAXID.CFGID` and the two amputations can.
(OBSERVED HIGH for the constants; Probe-2/3 abort-vs-zero behaviour is the standard APB
unmapped-aperture response, INFERRED MED — the JSON specifies absence, not the bus reply.)

### 7b. DIR-injection loop width

A debug-injection engine that single-steps an instruction into a stopped core must size
its `DIR` write loop to the core's word count — **4 words for NX, 8 for Q7** — or it will
write to unmapped OCD offsets on NX:

```c
/* Inject a wide instruction into a stopped Xtensa core via DIRn + DIR0EXEC.
 * n_dir = 4 for NX (xtensa_nx), 8 for Q7 (xtensa_q7).  Caller must hold the
 * core Stopped (DSR.Stopped==1) and in OCD mode (DCR.EnableOCD==1); DIRn are
 * writable ONLY then. abs offsets: DIR0=0x2020 .. DIR(n-1)=0x2020+4*(n-1),
 * DIR0EXEC=0x201C (writing it executes the assembled instruction). */
void xtensa_inject(uintptr_t base, const uint32_t *words, unsigned n_dir) {
    /* Fill the upper DIR words first (DIR1..), then DIR0EXEC last so the
     * write to DIR0EXEC both loads DIR0 and triggers execution. */
    for (unsigned i = n_dir; i-- > 1; )
        *(volatile uint32_t *)(base + 0x2020 + 4*i) = words[i];
    *(volatile uint32_t *)(base + 0x201C) = words[0];   /* DIR0EXEC: load+exec */
    /* Poll DSR.ExecDone (abs 0x2010 bit0); check DSR.ExecException (bit1). */
}
```

For NX, passing `n_dir=8` would target `0x2030..0x203C` — **unmapped** in the NX OCD bundle
— so the width must track the core. (Abs offsets OBSERVED HIGH; the fill-upper-then-exec
ordering is the standard Xtensa OCD injection sequence the `DIR0EXEC`/`DSR.ExecBusy`
descriptions imply, INFERRED MED.)

### 7c. v5 carry-forward (INFERRED)

No `xtensa_nx`/`xtensa_q7` CSR JSON exists for v5 in the shipped descriptor set. If v5
preserves the Cayman two-engine structure, the **shape** of this diff is expected to hold
— a narrower SEQ-side core (fewer `DIR` words, no per-array ECC reg) versus a wider
compute core — but the exact `CFGID` values, `DIR` word counts, and ECC field sets are
**unverified for v5** and must be re-derived from a v5 descriptor when one appears. Every
numeric constant on this page is **Cayman/NC-v3 OBSERVED**; do not carry `0x6200`/`0x6A00`,
the 4-vs-8 `DIR` split, or the 73/275 counts to v5 without re-measurement. (CARRIED, LOW.)

---

## 8. Cross-references

- [CSR — Xtensa Q7](xtensa-q7.md) — the full baseline register/field tables NX inherits.
- [SEQ engine — main loop](../../firmware/seq/main-loop.md) — the firmware driving the NX core.
- [SEQ — dual-fetch front-end](../../firmware/seq/dual-fetch.md) — scalar fetch/step path.
- [`tpb_xt_local_reg`](tpb-xt-local-reg.md) — the per-engine `*_LOCAL_REG` aperture in front of this block.

---

*Provenance: byte-exact diff of the shipped, binary-derived `cayman-arch-regs` CSR
descriptors `csrs/xtensa_nx/xtensa_nx.json` and `csrs/xtensa_q7/xtensa_q7.json`. Counts,
offsets, positions, access types and reset values re-derived from scratch by exhaustive
JSON enumeration; architectural interpretations flagged INFERRED. No external register
documentation was consulted — register/field semantics quoted are the descriptors' own
embedded `Description` strings.*
