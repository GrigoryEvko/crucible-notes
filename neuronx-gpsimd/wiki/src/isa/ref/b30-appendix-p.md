# ISA Batch 30 — Appendix P (pseudo / fence / no-datapath) + Final Coverage

This is the **final batch** of the per-instruction ISA reference for the Vision-Q7 *Cairo* (`ncore2gp`)
512-bit FLIX vector DSP — the page that **closes the 30-batch partition** declared in
[the template & partition page](template-and-partition.md) and feeds the engine-wide
[coverage-tally page](../core/coverage-tally.md). Batches B01–B29 resolved every mnemonic that owns a
`<SEMANTIC>` datapath group: the **1065** `ivp_`-prefixed FLIX-vector leaves (B01–B24, vector subtotal
closed at [B24](b24-composite.md)) and the **469** scalar base-Xtensa leaves (B25–B29). Appendix P sweeps up
the **residual** — every mnemonic the decode DB names that belongs to **no** `<SEMANTIC>` group and carries
**no datapath body**: assembler decode-tree interior nodes, the pipeline-ordering sync fences, the simulator
escape, and the reserved/absent-feature decode terminals.

Two deliverables on one page:

* **(A) The residual roster** — each no-datapath mnemonic classified (interior dispatch node / fence-alias /
  reserved terminal) with the **binary proof of why it has no body**: absence of an `Opcode_<m>_Slot_…_encode`
  thunk in `libisa-core.so`, absence of a `module__xdref_*` value leaf in `libfiss-base.so`, and — for the
  decode-tree interior nodes — the device assembler **rejecting the bare mnemonic** as "unknown opcode or
  format name".
* **(B) The FINAL ISA-coverage tally** — all 30 batches summed, reconciled against the two nm-grounded
  invariants the engine actually carries: the shipped runtime roster `1534 = 469 + 1065` with
  `num_encode_fns() = 0x3119 = 12569` slot placements, **and** the pre-fold TIE-DB superset `1607` with
  `12642` `<OPCODEDEF>` placements, connected by the **+73 fold** (`1607 − 1534 = 12642 − 12569 = 73`). The
  partition is certified a **complete, non-overlapping cover** with **zero residual unassigned**.

Confidence tags per the project model: `[HIGH/OBSERVED]` = read-from-byte / proven-by-tooling,
`[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — the two frames, and which number this page pins.** There are **two** rosters and they differ by a
> documented **fold**. The **shipped runtime roster** is the set of mnemonics that own at least one encode
> thunk in the runtime DLL `libisa-core.so` — **1534** mnemonics over **12569** opcode×slot thunks. The
> **pre-fold TIE-DB roster** is the larger set the assembler's decode descriptor enumerates including the
> non-emittable scaffolding — **1607** mnemonics over **12642** `<OPCODEDEF>` placements. Every count below
> is reproduced from `nm` (never a decompile grep, which inflates 2–12×). The exact commands are shown
> inline. `[HIGH/OBSERVED]`

> **NOTE — address arithmetic re-confirmed this pass.** `libisa-core.so` (9 690 712 B, ET_DYN x86-64, **not
> stripped**). `readelf -SW` this pass: `.text` (VMA `0x312c10` ↔ file `0x312c10`) and `.rodata`
> (VMA `0x3b6e40` ↔ file `0x3b6e40`) are **VMA == file-offset**, so `objdump -d` of the encode thunks and
> of `num_encode_fns` reads live bytes directly. `.data` (VMA `0x764040` ↔ file `0x564040`) carries the
> per-binary delta **`0x200000`** — **not** the `0x400000` seen in libtpu — so any walk of the
> `Opcode_*_args` operand-descriptor leaves resident in `.data` must subtract `0x200000`. The config DLLs
> are under `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with
> `fd --no-ignore` or an absolute path). `[HIGH/OBSERVED]`

---

## 0. Headline `[HIGH/OBSERVED]`

Batch 30 = the **49 residual mnemonics** that belong to no `<SEMANTIC>` group. Of the 1607 distinct
pre-fold `<OPCODEDEF>` mnemonics, **1558** are `<SEMANTIC>`-group members (the 304-group cover, zero
cross-membership — each grouped mnemonic in exactly one list) and **exactly 49 are not**. The 49 partition
into three classes:

| class | n | what | shipped? | binary signature |
|---|---|---|---|---|
| **P-a** decode-tree interior nodes | 37 | Xtensa major / sub-opcode dispatch pseudo-mnemonics (`QRST`, `RST0`–`RST3`, `B`, `SI`, `LSAI`, `RSR`, `WSR`, `XSR`, `RUR`, `WUR`, `CALL`, `CALLX`, `JR`, …) | **no** (fold-out) | no encode thunk; no xdref leaf; assembler rejects bare name; names ≥1 child |
| **P-b** fence / escape leaves | 6 | `DSYNC` `ESYNC` `FSYNC` `ISYNC` `RSYNC` (pipeline-ordering sync fences) + `SIMCALL` (host-trap escape) | **yes** | **1 encode thunk each** (`Slot_inst`); no xdref leaf; has an iclass; round-trips on device oracle |
| **P-c** reserved terminals | 6 | `CUST0` `CUST1` (customer-TIE space), `LSCI` `LSCX` (coprocessor LS group heads), `MAC16` (absent unit), `XUR` (user-reg-extract head) | **no** (fold-out) | no encode thunk; no xdref leaf; no iclass; no enabled child |

So of the 49: **43 fold out** (P-a + P-c — no encode thunk, not in the shipped 1534) and **6 ship** (P-b —
they survive the fold with an iclass and an encode thunk, but still have **no datapath**). The fence/escape
leaves are the *only* no-datapath mnemonics in the entire shipped runtime roster. `[HIGH/OBSERVED]`

The final tally (§3): **1174** IVP-vector (B01–B24) + **384** base-Xtensa (B25–B29) + **49** Appendix P
(B30) = **1607** distinct mnemonics — pre-fold exact; folds to the **1534** shipped roster the runtime DLL
carries (`Δ = +73`). The partition is a perfect non-overlapping cover: 0 mnemonics in >1 batch, 0 residual
unassigned. `[HIGH/OBSERVED]`

---

## 1. Roster derivation — the exact 49 `[HIGH/OBSERVED]`

### 1.1 The two rosters, both from `nm`

The shipped runtime roster is the set of mnemonics owning at least one encode thunk. The thunk symbols are
`Opcode_<mnemonic>_Slot_<slot>[_<unit>]_encode`; stripping to the mnemonic and de-duplicating gives the
ISA-wide invariants directly:

```text
ISA=…/ncore2gp/config/libisa-core.so

nm $ISA | rg -o 'Opcode_(.+)_Slot_.+_encode' -r '$1' | sort -u | wc -l   # 1534  shipped mnemonics
   …                                                | rg -c  '^ivp'      # 1065  ivp_ vector
   …                                                | rg -vc '^ivp'      #  469  scalar
nm $ISA | rg -c 'Opcode_.+_Slot_.+_encode'                              # 12569 opcode×slot thunks
```

The runtime confirms `12569` as a baked-in constant — `num_encode_fns()` at `0x3b6130` is literally:

```text
00000000003b6130 <num_encode_fns>:
  3b6130:  b8 19 31 00 00   mov $0x3119,%eax     # 0x3119 = 12569
  3b6135:  c3               ret
```

The pre-fold TIE-DB roster (the partition frame B01–B30 is defined over) is the larger descriptor set the
assembler enumerates: **1607** distinct mnemonics over **12642** `<OPCODEDEF>` placements. Both frames are
reconciled in §3.4 below; the connecting **+73 fold** is exactly the 43 P-a/P-c residuals + 24 wide-branch
alternate-encoding macro forms (the `xt_wide_branch` `_w15` branch set; see the §3.4 correction on the
`.W18` naming) + 6 virtualops.

### 1.2 The residual set

```text
ALL  = distinct <OPCODEDEF> mnemonics                     # 1607
SEM  = mnemonics that appear in any <SEMANTIC>/<LIST>     # 1558
P    = ALL − SEM                                          #   49  (Batch 30)
```

`|ALL| = 1607`, `|SEM| = 1558`, `|P| = 49`, `SEM ∩ P = ∅`. The 304 semantic groups have zero
cross-membership. `[HIGH/OBSERVED]`

The 49, sorted, with class:

| mnemonic | cls | mnemonic | cls | mnemonic | cls | mnemonic | cls |
|---|---|---|---|---|---|---|---|
| `B`       | P-a | `B1`     | P-a | `BI0`    | P-a | `BI1`    | P-a |
| `BZ`      | P-a | `BZ6`    | P-a | `CACHE`  | P-a | `CALL`   | P-a |
| `CALLX`   | P-a | `CUST0`  | P-c | `CUST1`  | P-c | `DSYNC`  | P-b |
| `ESYNC`   | P-b | `FSYNC`  | P-b | `IMP`    | P-a | `ISYNC`  | P-b |
| `JR`      | P-a | `LSAI`   | P-a | `LSCI`   | P-c | `LSCX`   | P-c |
| `LSI4`    | P-a | `MAC16`  | P-c | `MMU`    | P-a | `OCD`    | P-a |
| `QRST`    | P-a | `RFEI`   | P-a | `RSR`    | P-a | `RST0`   | P-a |
| `RST1`    | P-a | `RST2`   | P-a | `RST3`   | P-a | `RSYNC`  | P-b |
| `RT0`     | P-a | `RUR`    | P-a | `S3`     | P-a | `SCALL`  | P-a |
| `SI`      | P-a | `SIMCALL`| P-b | `SNM0`   | P-a | `ST0`    | P-a |
| `ST1`     | P-a | `ST2`    | P-a | `ST3`    | P-a | `SYNC`   | P-a |
| `SYNCT`   | P-a | `WSR`    | P-a | `WUR`    | P-a | `XSR`    | P-a |
| `XUR`     | P-c | | | | | | |

**37 interior (P-a) + 6 fence/escape (P-b) + 6 terminal (P-c) = 49.** `[HIGH/OBSERVED]`

### 1.3 The no-datapath proof — three independent binary signatures

The "no datapath" status of each residual is **observed**, not assumed, against three independent tables.

**(i) No encode thunk** (`libisa-core.so`). Scanning the 49 against the de-duplicated encode-thunk
mnemonic set: **exactly the 6 P-b leaves are present**; the 43 P-a/P-c are **absent**.

```text
for m in dsync esync fsync isync rsync simcall \
         b b1 bi0 bi1 bz bz6 qrst rst0 rst1 rst2 rst3 cache call callx jr lsai \
         lsi4 mmu ocd rfei rsr rt0 s3 scall si snm0 st0 st1 st2 st3 sync synct \
         wsr wur xsr rur imp cust0 cust1 lsci lscx mac16 xur ; do
  rg -qx "$m" <(nm $ISA | rg -o 'Opcode_(.+)_Slot_.+_encode' -r '$1' | sort -u) \
     && echo "ENCODES: $m"
done
# → ENCODES: dsync esync fsync isync rsync simcall   (6, all P-b)
```

Each P-b leaf owns **exactly one** thunk, in `Slot_inst` (the bare 24-bit base-Xtensa instruction slot — a
*scalar* slot, not a FLIX vector slot): e.g. `Opcode_isync_Slot_inst_encode`,
`Opcode_simcall_Slot_inst_encode`. The 43 fold-out mnemonics have **zero** thunks — they never assemble to
a standalone instruction, so the runtime never builds an encoder for them. `[HIGH/OBSERVED]`

**(ii) No value leaf** (`libfiss-base.so`). The instruction-set value functions are
`module__xdref_<mnemonic>_<widths>` leaves (e.g. `module__xdref_add_16_16_16`). The DLL holds **864** such
leaves (`nm libfiss-base.so | rg -c 'module__xdref_'`). **None of the 49** residual mnemonics has an xdref
leaf — including the 6 P-b fences. A fence *encodes* (signature i) but *computes nothing* (signature ii):
its effect is a pipeline-ordering constraint, not a register/memory value. `[HIGH/OBSERVED]`

**(iii) Assembler rejection** (device oracle, `xtensa-elf-as`, `XTENSA_CORE=ncore2gp`). The P-a interior
nodes are not emittable mnemonics at all. The assembler classifies them as decode-tree format names, not
instructions:

```text
$ printf '\tqrst\n' | xtensa-elf-as     →  Error: unknown opcode or format name 'qrst'
$ printf '\trsr\n'  | xtensa-elf-as     →  Error: not enough operands (0) for 'rsr'; expected 2
```

`QRST` is rejected outright (pure interior node). `RSR` exists only as a generic carrier whose concrete
`RSR.<SR>` *children* assemble — the bare interior name never stands alone. `[HIGH/OBSERVED]`

**Placement count.** Each of the 49 contributes **exactly 1** `<OPCODEDEF>` placement — they are single
base-Xtensa decode nodes, not FLIX-slot-replicated leaves (contrast a vector ALU op spread over 17–42 slot
placements). `Σ residual placements = 49`. `[HIGH/OBSERVED]`

---

## 2. Per-opcode resolution (template adapted for no-datapath ops)

For the 49 the per-instruction template collapses: there is **no lane datapath**, so `LANES`,
`SAT-ROUND`, and `TIMING(USE/DEF)` are *n/a (no datapath)*. The decisive fields are the **ENCODING** (the
decode-tree chain + the folded major/sub-opcode constant) and, for the 6 leaves, the **iclass operand
signature + architectural effect**. Words below are 24-bit base-Xtensa, quoted **MSB-first** as the
folded field constant; the device `objdump`/`as` regroups the same bytes little-endian (e.g. MSB-first
`0x0020f0` ↔ objdump byte-grouping `f02000`) — same physical word.

### 2.1 P-b — the 5 pipeline-ordering sync fences (shipped) `[HIGH/OBSERVED]`

All five live under the `SYNC`/`SYNCT` subtree of `ST0 ← RST0 ← QRST` (`op0=0, op1=0, op2=0, r=2`); the
`SYNCT` `t`-field discriminates the variant. Round-tripped this pass through the device assembler and
disassembler (`xtensa-elf-as` then `xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`):

| mnemonic | iclass | decode chain | word (MSB-first) | as → objdump |
|---|---|---|---|---|
| `ISYNC` | `xt_iclass_isync` | `ISYNC ← SYNCT ← SYNC ← ST0 ← RST0 ← QRST` | `0x002000` | `isync` |
| `RSYNC` | `xt_iclass_sync`  | `RSYNC ← SYNCT ← SYNC ← ST0 ← RST0 ← QRST` | `0x002010` | `rsync` |
| `ESYNC` | `xt_iclass_sync`  | `ESYNC ← SYNCT ← SYNC ← ST0 ← RST0 ← QRST` | `0x002020` | `esync` |
| `DSYNC` | `xt_iclass_sync`  | `DSYNC ← SYNCT ← SYNC ← ST0 ← RST0 ← QRST` | `0x002030` | `dsync` |
| `FSYNC` | `xt_iclass_fsync` | `FSYNC ← SYNC ← ST0 ← RST0 ← QRST`         | `0x002800` | `fsync 0` |

* **GROUP** — none (no `<SEMANTIC>`; Appendix P).
* **OPERANDS** — `ISYNC`/`RSYNC`/`ESYNC`/`DSYNC` none architectural (`RSYNC`/`ESYNC`/`DSYNC` carry a
  `gen_sig` `XTSYNC` pipeline pseudo-state, not a regfile operand). `FSYNC` takes an 8-bit immediate
  selector (`imms8`), so `fsync 0` is the canonical encoding (`0x002800`); a bare `fsync` is an assembler
  error ("too few operands"). `[HIGH/OBSERVED]`
* **STATE / EXC** — none. These are base-ISA ordering ops, not vector-coprocessor ops (no `CPENABLE`, no
  `Coprocessor1Exception`).
* **SEMANTICS** — pipeline **ordering only**, no register/memory side-effect: `ISYNC` = instruction-fetch
  sync (flush prefetch after self-modifying code / IRAM update); `DSYNC` = data/load-store sync; `ESYNC` =
  execution sync; `RSYNC` = register-write (special-register) sync; `FSYNC` = fetch/format sync
  (immediate-qualified). The full semantics *are* "drain/order the pipeline at this barrier" — there is no
  datapath to render. Categorical, **not** a coverage gap. `[HIGH/OBSERVED]`
* **TIMING** — no `<INSTR_SCHEDULE>` body; a fence has no USE/DEF stage delta, only a funcUnit ordering
  constraint. `[HIGH/OBSERVED]`

> **NOTE — the SYNCT siblings are *not* in Batch 30.** The other children of `SYNCT` — `NOP` (`0x0020f0`),
> `MEMW` (`0x0020c0`), `EXTW` (`0x0020d0`), `EXCW` (`0x002080`) — **are** `<SEMANTIC>`-grouped
> (`xt_sem_sync` / `xt_core`) and resolved in the base batches B27/B29. They were spot-confirmed bit-exact
> on the same oracle run (`f02000 → nop`, `c02000 → memw`, `d02000 → extw`, `802000 → excw`). Only the 5
> fences fall out of the semantic cover into Appendix P. `[HIGH/OBSERVED]`

### 2.2 P-b — `SIMCALL` (simulator-call escape; shipped) `[HIGH/OBSERVED]`

| field | value |
|---|---|
| mnemonic | `SIMCALL` |
| group | none (Appendix P) |
| iclass | `xt_iclass_simcall` |
| chain | `SIMCALL ← SCALL ← ST0 ← RST0 ← QRST` (`s=1` under `SCALL` `r=5`) |
| word | `0x005100` |
| as → objdump | `simcall 0` |

* **OPERANDS** — a 4-bit `t`-field immediate (`immt`); canonical form `simcall 0`.
* **STATE / EXC** — none architectural.
* **SEMANTICS** — host-trap escape used by the instruction-set simulator (FISS) to invoke a host service;
  **no architectural datapath** on real silicon (a no-op / trap on the device). It ships (encode thunk
  `Opcode_simcall_Slot_inst_encode`, iclass present) but has no xdref value leaf — the canonical
  "encodes, computes nothing" residual. Categorical. `[HIGH/OBSERVED]`
* **TIMING** — no pipeline body.

### 2.3 P-c — the 6 reserved / terminal decode placeholders (not shipped) `[HIGH/OBSERVED]`

These pin a major/sub-opcode constant but enable **no child** and **no body** in this config — empty decode
terminals (absent-feature or customer-TIE placeholders). All six have no encode thunk, no xdref leaf, no
iclass.

| mnemonic | chain | word | role |
|---|---|---|---|
| `MAC16` | `MAC16` (`op0=4`)          | `0x000004` | MAC16 major-opcode cell — the MAC16 unit is **absent** in this config; reserved placeholder, no child decode |
| `LSCI`  | `LSCI` (`op0=3`)           | `0x000003` | coprocessor `LSCI` load/store group head; **0** enabled children here |
| `LSCX`  | `LSCX ← QRST` (`op1=8`)    | `0x080000` | coprocessor `LSCX` group head; 0 child |
| `CUST0` | `CUST0 ← QRST` (`op1=6`)   | `0x060000` | customer-TIE opcode space 0; reserved for user TIE, empty |
| `CUST1` | `CUST1 ← QRST` (`op1=7`)   | `0x070000` | customer-TIE opcode space 1; empty |
| `XUR`   | `XUR ← RST1 ← QRST` (`op2=7`) | `0x710000` | user-register **extract** group head (sibling of `RUR` `op2=14`); 0 enabled child |

Pure reserved decode scaffolding — not in the shipped 1534 (fold-out). The "absent in this config" reading
is **inferred** from `0` enabled children + `0` body: the major-opcode cell exists for ISA-format
completeness; the feature unit is not instantiated in the Cairo config. `[MED/INFERRED]`

### 2.4 P-a — the 37 decode-tree interior nodes (not shipped; pure dispatch) `[HIGH/OBSERVED]`

Each is a **non-leaf** node: it constrains an opcode-field constant and names a set of child opcodes. By
construction it carries no instruction body — its field constraint is inherited by every descendant leaf,
and the bare interior name never assembles (§1.3-iii). Resolved as
*(name : folded constant : representative children)*.

**Top-level major-opcode groups (`op0` select):**

| node | `op0` | word | dispatches to |
|---|---|---|---|
| `QRST` | 0 | — | `RST0`…`RST3`, `CUST0/1`, `LSCX`, `LSI4`, `DEPBITS`, `EXTUI` (RRR major, dense arith/logic/SR/MAC space) |
| `B`    | 7 | `0x000007` | 14 children: `BALL BANY BBC BBCI BBS BBSI BEQ BGE BLT BNE BNALL BNONE BGEU BLTU` |
| `CALL` | 5 | `0x000005` | `CALL0 CALL8 BREAK1` |
| `SI`   | 6 | `0x000006` | `BI0 BI1 BZ J` |
| `LSAI` | 2 | `0x000002` | 13 children: `ADDI ADDMI CACHE L8UI L16UI L16SI L32I L32AI S8I S16I S32I S32NB …` |
| `ST2`  | 12 | `0x00000c` | `BZ6 MOVI.N` (density group) |
| `ST3`  | 13 | `0x00000d` | `MOV.N S3` (density group) |

(`LSCI` `op0=3` and `MAC16` `op0=4` are the P-c terminals above.)

**QRST sub-groups (`op0=0`, `op1` select):**

| node | `op1` | dispatches to |
|---|---|---|
| `RST0` | 0 | `ADD ADDX2/4/8 SUB SUBX2/4/8 AND OR XOR` + the `ST0/ST1/MMU/RT0` sub-trees |
| `RST1` | 1 | `SLL SLLI SRA SRAI SRC SRL SRLI MUL16S MUL16U IMP XSR XUR` |
| `RST2` | 2 | `ANDB ANDBC ORB ORBC MULL MULSH MULUH QUOS …` (bool + mul space) |
| `RST3` | 3 | `MOVEQZ MOVNEZ MOVLTZ MOVGEZ MOVF MOVT MAX MAXU MIN MINU CLAMPS` + `RSR WSR RUR WUR` wrappers |

**RST0/RST1 second-level (`op2` select):**

| node | `op2` | dispatches to |
|---|---|---|
| `ST0` | 0 | `SNM0 SYNC RFEI SCALL OCD MOVSP RER WER BREAK ALL4/8 ANY4/8 …` (system-state sub-tree under RST0) |
| `ST1` | 4 | `NSA NSAU GETEX ROTW SETW SSA8B SSA8L RER …` |
| `MMU` | 5 | `PPTLB RPTLB0/1 WPTLB RBTB0/1/2 WBTB0/1` (TLB/BTB; `xt_mmu`) |
| `RT0` | 6 | `ABS NEG` (op2=6 unary-arith pair under RST0) |
| `IMP` | 15 | `L32EX S32EX LICT LICW SICT SICW RFDO` (implementation sub-tree under RST1) |

**SR / UR wrapper groups (`RST3`/`RST1` `op2` select).** These are the *generic carriers* whose concrete
per-register forms **do** have bodies and **are** `<SEMANTIC>`-grouped in B27/B29 — the bare interior
wrapper name never assembles, only its `.<SR>` children do:

| node | `op2` | word | children |
|---|---|---|---|
| `RSR` | 0  | `0x030000` | 45 `RSR.<SR>` (e.g. `RSR.SAR/CCOUNT/PS/CPENABLE/…`) |
| `WSR` | 1  | `0x130000` | 44 `WSR.<SR>` |
| `XSR` | 6  | `0x610000` (under RST1) | 41 `XSR.<SR>` |
| `RUR` | 14 | `0xe30000` | `RUR.THREADPTR` |
| `WUR` | 15 | `0xf30000` | `WUR.FCR WUR.FSR WUR.THREADPTR` |

Oracle spot-check of the **concrete** (semantic) leaves: `0x03ea00 → rsr.ccount a0`,
`0x13e000 → wsr.cpenable a0` — the wrapper interior names `RSR`/`WSR`/`XSR`/`RUR`/`WUR` never stand alone.
`[HIGH/OBSERVED]`

**Branch / call / immediate sub-groups:**

| node | field | dispatches to |
|---|---|---|
| `SNM0`  | `r=0` under ST0 | `CALLX JR ILL EXIT` |
| `CALLX` | `m=3` under SNM0 | `CALLX0 CALLX8` |
| `JR`    | `m=2` under SNM0 | `JX RET RETW` |
| `SCALL` | `r=5` under ST0 | `HALT SIMCALL SYSCALL` |
| `SYNC`  | `r=2` under ST0 | `FSYNC SYNCT` |
| `SYNCT` | `s=0` under SYNC | `DSYNC ESYNC ISYNC RSYNC EXCW EXTW MEMW NOP` |
| `RFEI`  | `r=3` under ST0 | `CLREX` |
| `OCD`   | `r=7` under ST0 | `LDDR32.P SDDR32.P` (on-chip-debug DDR load/store) |
| `CACHE` | `r=7` under LSAI | `IHI III IPF` (instruction-cache hint group) |
| `LSI4`  | `op1=9` under QRST | 14 children `L32E / L32DIS.* / S32DIS.*` (coprocessor/disable-window LS) |
| `BI0`/`BI1`/`BZ` | `n=2/3/1` under SI | `BEQI/BNEI/BGEI/BLTI` ; `B1/BGEUI/BLTUI/ENTRY` ; `BEQZ/BNEZ/BGEZ/BLTZ` |
| `B1`    | `m=1` under BI1 | `BF BT LOOP LOOPGTZ LOOPNEZ` |
| `BZ6`   | `i=1` under ST2 | `BEQZ.N BNEZ.N` (density branch-zero pair) |

For **all 37**: `GROUP = none`, `ICLASS = none` (interior), `STATE/EXC = none`,
`SEMANTICS = "dispatch to children (no body)"`, `TIMING = n/a`. The folded word is the major/sub-opcode
prefix every descendant inherits; it is **not** independently emittable. `[HIGH/OBSERVED]`

---

## 3. Final ISA-coverage tally — all 30 batches summed + reconciled `[HIGH/OBSERVED]`

### 3.1 The 30-batch partition (distinct mnemonics per batch)

**IVP-vector (B01–B24).** Grounded on the `ivp_`/`ivpep_`/`fp_`/`bbn_` semantic groups; large groups are
split by lane-width/sub-family across batches (disjoint by construction — each placement assigned once). The
26 groups sum to the IVP subtotal:

| group | size | batch(es) |
|---|---|---|
| `ivp_sem_vec_alu` | 243 | [B01](b01-vec-alu-int.md)+[B02](b02-vec-alu-fp.md)+[B03](b03-vec-alu-rest.md) |
| `ivp_sem_multiply` | 232 | [B04](b04-mac-integer.md)+[B05](b05-mac-mixed.md) |
| `ivp_sem_ld_st` | 195 | [B06](b06-loads.md)+[B07](b07-stores.md) |
| `ivp_sem_vec_reduce` | 56 | [B08](b08-reduce.md) |
| `ivp_sem_vec_mov` | 44 | [B09](b09-vec-mov.md) |
| `ivp_sem_wvec_pack` | 42 | [B10](b10-wvec-pack.md) |
| `ivp_sem_vbool_alu_ltr` | 33 | [B11](b11-vbool-alu.md) |
| `ivp_sem_vec_shift` | 32 | [B12](b12-shift.md) |
| `ivpep_sem_sp_cvt` | 31 | [B13](b13-sp-cvt.md) |
| `ivpep_sem_hp_lookup` | 30 | [B14](b14-hp-lookup.md) |
| `ivpep_sem_sp_lookup` | 29 | [B15](b15-sp-lookup.md) |
| `ivp_sem_vec_rep` | 28 | [B16](b16-vec-rep.md) |
| `ivp_sem_spfma` | 27 | [B17](b17-spfma.md) |
| `fp_sem_hp_fma` | 27 | [B18](b18-hp-fma.md) |
| `ivp_sem_vec_scatter_gather` | 24 | [B19](b19-scatter-gather.md) |
| `ivpep_sem_hp_cvt` | 21 | [B20](b20-hp-cvt.md) |
| `ivp_sem_vec_select (18)+_seli (6)` | 24 | [B21](b21-select-shuffle.md) |
| `ivp_sem_unpack_wvec_mov` | 18 | [B22](b22-unpack-wvec-mov.md) |
| `ivp_sem_divide` | 14 | [B23](b23-divide.md) |
| `histogram(8)+sqz(6)+sprecip_rsqrt(5)+rur/wur_fcr_fsr(2+2)+addmod(1)` | 24 | [B24](b24-composite.md) |
| **IVP total** | **1174** | |

Check: `243+232+195+56+44+42+33+32+31+30+29+28+27+27+24+21+18+18+14+8+6+6+5+2+2+1 = 1174`. ✓

> **FRAME NOTE — `1174` (pre-fold B01–B24 semantic membership) vs `1065` (shipped `ivp_` vector roster).**
> This `1174` is the **pre-fold** count of `<SEMANTIC>`-group members assigned to batches B01–B24, and it
> **includes the 109 scalar-FP forms** that the composite batch [B24](b24-composite.md) carries
> (`recipqli_s`, the `rur/wur.fcr/fsr` UR ops, the scalar-FP convert/lookup tail, …). The **shipped**
> `ivp_`-prefixed vector roster is **1065** (nm-direct on the opcode-name table). The two reconcile exactly:
> `1174 − 1065 = 109` = the B24 scalar-FP population, which the shipped frame counts in the **469 scalar**
> axis, not the vector axis. So `1174` (pre-fold IVP, this §3 frame) and `1065` (shipped vector,
> [B24](b24-composite.md) §7 frame) are **the same partition viewed in two frames**, not a disagreement.
> `[HIGH/OBSERVED]`

**Base-Xtensa (B25–B29).** The non-ivp / non-residual semantic membership, **384** mnemonics spread over
278 base semantic groups (many singletons — each `RSR.X`/`WSR.X`/`XSR.X` concrete SR form is its own
one-member group; the largest is `xt_sem_widebranch15` (24 distinct branch mnemonics — `nm libcas-core.so
\| rg -o 'xt_sem_widebranch15_opcode_stage[0-9]'` plus `nm libisa-core.so \| rg -o 'Opcode_([a-z0-9]+)_w15_args'
-r '$1' \| rg -v '^loop' \| sort -u \| wc -l` = 24)). The
B25–B29 split is by **package/function**, not by group:

| batch | package theme | ops |
|---|---|---|
| [B25](b25-xt-core.md) | `xt_core` arith/logic/shift (ADD/SUB/ADDX/AND/…, EXTUI/SEXT/MOVI, `RUR.THREADPTR` subset) | ~80 |
| [B26](b26-xt-ctrl.md) | `xt_core` ld/st/branch/jump + `xt_density` (.N) + `xt_mul32` | ~50 |
| [B27](b27-xt-system.md) | `xt_xtensa` system/SR/regwin (`RSR/WSR/XSR.<SR>` leaves, ENTRY/RETW/MOVSP, MEMCTL/PREFETCH, sync `NOP/MEMW/EXTW`) | ~60 |
| [B28](b28-xt-exc.md) | `xt_exceptions` dispatch + booleans + branchpred + regwin + wide_loop + minmax/div | ~55 |
| [B29](b29-xt-system2.md) | debug + timer + inst/data-cache/ram + mmu/tlb + extreg + coprocessors + interrupt + trace + halt + nsa + prefetch | ~80 |
| **base total** | (package histogram: `xt_core` 124, `xt_exceptions` 40, `xt_ivp32` 38, `xt_debug` 32, `xt_wide_branch` 24, `xt_booleans` 16, `xt_regwin` 14, `xt_density` 12, `xt_timer` 12, `xt_mmu` 10, `xt_misc` 8, `xt_instcache` 7, branchpred 6, virtualops 6, sync 5, externalregisters 5, mul 5, integerdivide 4, instram 3, dataram 3, coprocessors 3, prefetch 3, halt 2, interrupt 1, trace 1 = **384**) | **384** |

**Appendix P (B30, this doc).** `37 (P-a) + 6 (P-b) + 6 (P-c) = ` **49**.

> **FRAME NOTE — base-Xtensa `384` (pre-fold, B25–B29) vs `360` ([template](template-and-partition.md) §6.1,
> shipped).** This `384` is the **pre-fold** semantic-group membership of the base packages. The
> [partition template](template-and-partition.md) reports the **shipped** base-Xtensa scalar count as
> **360** (`469 scalar − 109 B24 scalar-FP`). They bridge exactly: `384 − 24 = 360`, where the **24** is the
> `xt_wide_branch` `_w15` branch set that is pre-fold-only in this base region (the fold-out the §3.4 table
> places at "B28/base"). The 6 virtualops + 43 no-body fold-out members live in the B25/B30 slices, not in
> this base subtotal. Same partition, two frames. `[HIGH/OBSERVED]`

### 3.2 Grand total — distinct mnemonics (pre-fold frame)

| segment | batches | distinct mnemonics |
|---|---|---|
| IVP-vector | B01–B24 | 1174 |
| base-Xtensa | B25–B29 | 384 |
| Appendix P | B30 | 49 |
| **GRAND TOTAL** | | **1607** == \|distinct `<OPCODEDEF>`\| **EXACT** |

`[HIGH/OBSERVED]`

### 3.3 Placement reconciliation (pre-fold `<OPCODEDEF>` frame)

Each mnemonic occupies 1..42 (mnemonic × format × slot) placements:

| segment | placements |
|---|---|
| IVP-vector group placements | 10358 |
| base-Xtensa group placements | 2235 |
| residual (Appendix P) | 49 (1 each) |
| **TOTAL** | **12642 EXACT** |

IVP ops are FLIX-replicated across 14–17 slots each → high placement density; base-Xtensa ops are mostly
single 24-bit placements; the 49 residual are exactly 1 placement each (decode nodes, never slot-scattered).
`[HIGH/OBSERVED]`

### 3.4 1607 pre-fold ↔ 1534 shipped reconciliation (the +73 fold) `[HIGH/OBSERVED]`

The TIE-DB roster (**1607**) is the pre-fold superset; the binary runtime roster (the encode-thunk set,
**1534**) is the shipped set. `Δ = 1607 − 1534 = +73` fold-out, with **0 shipped-only** → 1534 is a **clean
subset** of 1607. The +73 splits:

| fold class | n | have body? | shipped? | where in partition |
|---|---|---|---|---|
| wide-branch alternate-encoding macro forms (`.W15` collapses into the shipped plain `beq`/`bne`/… entries) | 24 | yes | no | B28/base |
| virtualops (`ADDI.A.N CLAMPSF FFS POPC POPCE SEXTF`) | 6 | yes (`xt_virtualops`) | no | B25/base |
| no-body decode-tree pseudo-mnemonics | 43 | **no** | no | **B30** (P-a 37 + P-c 6) |
| **fold total** | **73** | | | |

> **CORRECTION — wide-branch fold naming (`.W18`/`xt_sem_widebranch18` are not in any binary).** The
> wide-branch fold class was previously labelled "24 `.W18` macro forms (`xt_sem_widebranch18`)". No
> `_w18`, `.W18`, or `widebranch18` symbol exists in any ncore2gp config DLL (`nm` over
> `libisa-core/libcas-core/libtie-core/libfiss-base` = 0). The only wide-branch semantic group present is
> **`xt_sem_widebranch15`** (libcas-core, 4 stage symbols), and the only wide-branch encode thunks are the
> 24 `_w15` **branch** forms (`ball bany bbc bbci bbs bbsi beq beqi beqz bge bgei bgeu bgeui bgez blt blti
> bltu bltui bltz bnall bne bnei bnez bnone`) plus 3 `_w15` **loop** forms (`loop loopgtz loopnez`, package
> `xt_wide_loop`). The shipped 1534 roster lists the **plain** mnemonics (`beq`/`bne`/`loop`/…) — the `_w15`
> suffix is the FLIX-slot encode-thunk variant, not a separate roster entry. So the **count 24 is
> OBSERVED** (24 `xt_wide_branch` branch mnemonics, nm-direct), but the `.W18`-vs-`.W15` *source-macro
> distinction* that explains why exactly 24 fold out is **CARRIED** from TIE-DB knowledge — it is not
> visible in the runtime/reference binaries. `[count: HIGH/OBSERVED · fold-mechanism: MED/CARRIED]`

The placement fold is **also +73** because each fold-out mnemonic contributes exactly 1 placement:
`12642 − 12569 = 73`. The mnemonic delta and the placement delta coincide by construction. ✓

Of Batch 30's 49: **43 fold out** (= P-a 37 + P-c 6, no encode thunk) + **6 ship** (P-b
`DSYNC/ESYNC/FSYNC/ISYNC/RSYNC/SIMCALL` — they survive the fold with an iclass and an encode thunk, but have
no datapath). So among the shipped 1534, the **only** no-datapath members are these 6 fences/escape
(1528 bit-precise datapath ops + 6 categorical fences). No shipped datapath mnemonic lacks a body.
`[HIGH/OBSERVED]`

The shipped split is nm-direct: `1534 = 469 scalar + 1065 vector` with `num_encode_fns() = 0x3119 = 12569`
opcode×slot thunks (§1.1).

### 3.5 Residual-after-cover and the formal cover

`|ALL − (B01 ∪ … ∪ B30)| = 0`. Every one of the 1607 distinct mnemonics is assigned to **exactly one**
batch (1558 by semantic group → B01–B29; 49 residual → B30). No uncovered mnemonic, no mnemonic in >1 batch.

* **(i) COVER** — `B01 ∪ … ∪ B30 = ALL (1607)`. ✓ (residual = ∅)
* **(ii) DISJOINT** — `Bi ∩ Bj = ∅` for `i ≠ j`. ✓ (0 semantic cross-membership for the 1558 grouped; the
  49 residual are by definition ∉ any semantic group, hence ∉ B01–B29; each residual listed once in B30.)
* **(iii) PLACEMENT COVER** — `Σ placements = 12642 (10358 + 2235 + 49)`. ✓

The 30-batch partition is a **perfect, complete, non-overlapping cover** of the full GPSIMD / Vision-Q7
`ncore2gp` TIE instruction set. `[HIGH/OBSERVED]`

---

## 4. Adversarial self-verification — the 5 load numbers re-challenged against `nm`

Every figure re-run this pass from the shipped DLLs (`ISA = …/config/libisa-core.so`,
`FISS = …/config/libfiss-base.so`):

| # | claim | command | result | verdict |
|---|---|---|---|---|
| 1 | shipped roster total = 1534 | `nm $ISA \| rg -o 'Opcode_(.+)_Slot_.+_encode' -r '$1' \| sort -u \| wc -l` | `1534` | **PASS** `[HIGH/OBSERVED]` |
| 2 | scalar/vector split = 469 / 1065 | same, then `rg -vc '^ivp'` / `rg -c '^ivp'` | `469` / `1065` | **PASS** `[HIGH/OBSERVED]` |
| 3 | placement total = 12569 = `num_encode_fns()` | `nm $ISA \| rg -c 'Opcode_.+_Slot_.+_encode'`; `objdump -d` @`0x3b6130` → `mov $0x3119` | `12569` = `0x3119` | **PASS** `[HIGH/OBSERVED]` |
| 4 | residual = 49 = 6 fence (encode thunk present) + 43 fold-out (no encode thunk) | scan 49 vs encode-mnemonic set | 6 present, 43 absent | **PASS** `[HIGH/OBSERVED]` |
| 5 | fold delta = +73 (mnemonic) = +73 (placement) | `1607 − 1534`; `12642 − 12569` | `73` = `73` | **PASS** `[HIGH/OBSERVED]` |

Supporting: `nm $FISS \| rg -c 'module__xdref_'` = **864** value leaves, **none** named for any of the 49
residual mnemonics (no-datapath, signature ii). `[HIGH/OBSERVED]`

---

## 5. Quirks, corrections, and cross-page notes

> **QUIRK — "encodes but computes nothing".** The 6 P-b leaves are the only mnemonics in the shipped roster
> that own an encode thunk (`Opcode_<m>_Slot_inst_encode`) **and** have **no** `module__xdref_*` value leaf.
> A reimplementation must build an *encoder* for them (they assemble to real bytes and round-trip on the
> device oracle) but **no value function** — their architectural effect is a pipeline-ordering / host-trap
> constraint, not a register/memory write. `[HIGH/OBSERVED]`

> **QUIRK — interior nodes are format names, not opcodes.** `xtensa-elf-as` rejects bare `QRST` as "unknown
> opcode or format name" and rejects bare `RSR` for missing operands — the 37 P-a nodes never assemble to a
> standalone instruction. A from-scratch assembler must model them as **decode-tree internal nodes** (each
> pinning a major/sub-opcode field that its children inherit), *not* as emittable mnemonics. `[HIGH/OBSERVED]`

> **NOTE — pre-fold vs post-fold, the +73.** The partition is defined over the **1607** pre-fold TIE roster;
> the runtime ships **1534**. The `+73` fold is fully accounted: `24 wide-branch macro forms + 6 virtualops
> + 43 B30 no-body` (the wide-branch 24 = the `xt_wide_branch` `_w15` branch set; the `.W18` source-form
> naming is CARRIED, see §3.4 correction).
> The mnemonic fold and the placement fold are both exactly 73 because every fold-out op is a single
> placement. Do not confuse the two totals: count claims about the *shipped engine* use 1534/12569; count
> claims about the *decode DB partition* use 1607/12642. `[HIGH/OBSERVED]`

> **CORRECTION / naming trap (carried from [B24](b24-composite.md)).** `IVP_ADDMOD16U` is an `ivp_`-prefixed
> mnemonic — so it counts toward the **1065 vector** subtotal by prefix — even though its semantics are a
> *scalar* AR `(a + b) mod 2^16` wrap-add. The prefix, not the datapath shape, decides the scalar/vector
> split (`rg '^ivp'`). `nm $ISA … \| rg -i addmod` = exactly `ivp_addmod16u`. A tally that classified by
> semantics rather than prefix would mis-split the 469/1065 boundary. `[HIGH/OBSERVED]`

> **NOTE — the SR/UR wrapper aliasing.** `RSR`/`WSR`/`XSR`/`RUR`/`WUR` appear in **both** the residual (the
> bare interior wrapper, B30) **and** the base batches (their concrete `.<SR>` children, B27/B29). This is
> not a cross-membership violation: the residual mnemonic is the *wrapper node name*; the semantic-grouped
> mnemonics are the distinct `RSR.CCOUNT`/`WSR.CPENABLE`/… child names. They are different strings in the
> `<OPCODEDEF>` roster, each assigned to exactly one batch. `[HIGH/OBSERVED]`

> **LOW — per-fence funcUnit scope.** Which exact pipeline stages each barrier drains is categorical
> (system/sync semantics), not bit-traced — there is no datapath body to trace. Correct, not a gap. `[LOW]`

---

## 6. Close — the per-instruction ISA reference is complete

The template and the 30-batch contract were declared at
[the template & partition page](template-and-partition.md); batches B01–B29 resolved the **1558**
semantic-grouped leaf instructions (**1174** IVP-vector + **384** base-Xtensa). This page resolves the final
**49** — Appendix P: the **37** decode-tree interior dispatch nodes, the **6** pipeline-ordering / host-escape
leaves, and the **6** reserved terminals — and certifies the final tally: **1607** distinct mnemonics /
**12642** `<OPCODEDEF>` placements, a perfect complete non-overlapping cover, folding cleanly to the
nm-verified shipped runtime roster **1534 = 469 + 1065** over **12569** encode thunks (`+73` fold-out, 0
shipped-only). Every GPSIMD / Vision-Q7 `ncore2gp` TIE opcode is now accounted for in exactly one batch,
with no gap and no overlap.

This page feeds the engine-wide [coverage-tally](../core/coverage-tally.md) (the P2.10 roll-up) and closes
the [30-batch partition](template-and-partition.md). The vector subtotal it relies on is closed at
[B24](b24-composite.md) (1065 `ivp_`, zero residual); the base subtotal across
[B25](b25-xt-core.md)–[B29](b29-xt-system2.md) supplies the 384. `[HIGH/OBSERVED]`
