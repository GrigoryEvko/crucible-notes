# Codename ↔ Generation Cross-Walk

This page is the single canonical join table for every GPSIMD generation
identifier the rest of the book uses. Whenever a later page writes "SUNDA",
"coretype 13", "arch_id 0x1c", "v4+", "NC-v5", or "the MAVERICK twin", it is
naming a cell in the table below. The relations between those columns — and
exactly which ones are read straight from the binary versus deduced from a
stride — are the whole point of this page.

The hard rule that organises everything: **`coretype = arch_id + 1`**, with a
**+8 coretype stride** between adjacent generations. The five `coretype` values
are literal switch-case constants observed in the disassembly; the `arch_id`
values are the NCFW selector bytes for the four shipped generations and the
`coretype − 1` extrapolation for the unshipped fifth. The `coretype` column is
the one to trust unconditionally; the `arch_id` column is firmware-byte-grounded
for four rows and inferred for one.

Every cell carries a confidence tag: **HIGH / MED / LOW** crossed with
**OBSERVED** (read from bytes / disassembly / a shipped header this session),
**INFERRED** (deduced from the stride, naming, or structure with no contradicting
evidence), or **CARRIED** (taken from a cross-referenced sibling surface at its
stated confidence). The starred `36*` on the MAVERICK `arch_id` is the single most
important caveat on the page; it is never to be presented as binary-observed.

---

## 1 · The master cross-walk

There are **five unified-NeuronCore GPSIMD generations** plus **one pre-unified
outlier (TONGA)**. The five form a single arithmetic family keyed on a
`coretype` and an `arch_id`; TONGA sits *outside* that family and is discussed in
§5.

| Codename | NC-ver | coretype `[OBSERVED]` | arch_id `[= ct−1]` | NCFW v# / sel-byte | shipped? | EXTISA blob identity | product family `[CARRIED]` | PCI ID `[CARRIED]` | DKMS arch `[CARRIED]` |
|---|---|---|---|---|---|---|---|---|---|
| **SUNDA** | NC-v2 | **6** | **5** (0x05) | v2 / `0x05` | YES (all 4 libs) | 1 lib (weak-undef `EXTISA_0`); the only generation with a *real* JSON opcode manifest | Trn1 / Trn1N + Inf2 / Inf2E | `0x7164`, `0x7264` | `NEURON_ARCH_V2` |
| **CAYMAN** | NC-v3 | **13** | **12** (0x0c) | v3 / `0x0c` | YES (all 4 libs) | 4 libs `EXTISA_0..3`; own compile; 16 stub JSONs | Trn2 (+AC/E/N/P/U/UAC) | `0x7364` | `NEURON_ARCH_V3` |
| **MARIANA** | NC-v4 | **21** | **20** (0x14) | v4 / `0x14` | YES (all 4 libs) | 4 libs; distinct compile, same opcode contract | Trn3 / TRN3PDS98 (+SwitchV1) | `0x7564` (GA) | `NEURON_ARCH_V4` |
| **MARIANA_PLUS** | NC-v4+ | **29** | **28** (0x1c) | v4+ / `0x1c` | YES (all 4 libs) | 4 libs **byte-identical to MARIANA** (adds 0 new EXTISA bytes) | Trn3-pre / TRN3PDS98 (SwitchV1 ultra-server) | `0x7565` (pre) | `NEURON_ARCH_V4` |
| **MAVERICK** | NC-v5 | **37** | **36\*** `[INFERRED]` | — (no v#, no NCFW image) | **NO — internal twin only** | 4 libs (ET_DYN, clang-15 / XtensaTools-15.05); exist *only* in `libnrtucode_internal.so` `.rodata` | *(unshipped — no product binding in corpus)* | — | — |
| *TONGA* | *NC-v1 (outlier)* | *— (none)* | *— (none)* | *— (none)* | *legacy ISA headers only* | *— (no EXTISA blob, no NCFW image)* | *Inf1* | *—* | *(legacy V1)* |

**Confidence on the table as a whole:**

- The five codenames; `coretype {6,13,21,29,37}`; `arch_id {5,12,20,28}`;
  `coretype = arch_id + 1`; the +8 stride; the NCFW selector bytes
  `{0x05,0x0c,0x14,0x1c}` — all **HIGH / OBSERVED**, re-disassembled and
  re-`nm`-listed against the shipped binaries this session.
- MAVERICK `coretype 37` — **HIGH / OBSERVED** (two independent firmware reads,
  §3).
- MAVERICK `arch_id 36*` — **MED / INFERRED** (the `coretype − 1` extrapolation;
  there is *no* NCFW v5 selector byte, §3).
- The product / PCI-ID / DKMS-arch columns — **HIGH / OBSERVED in the platform
  and tools surfaces, CARRIED here** (they are not in the GPSIMD firmware itself;
  see §6). The MAVERICK product binding is genuinely **open** — nothing in this
  corpus names a "Trn4".

---

## 2 · The relation, proved row by row

The arithmetic is small enough to verify against the disassembly directly. The
NCFW image selector `libncfw_get_image` is a `cmpl`-ladder on the host-side
`arch_id`; its case constants are the `arch_id` column. The `coretype` is the
positional ordinal of each generation's `Q7_POOL` member in the `NRTUCODE_CORE_*`
enum. `coretype = arch_id + 1` is therefore a join between two independently
observed firmware structures — not an assumption.

```
codename       NCFW sel-byte (arch_id)   coretype (enum ordinal)   ct − arch_id
-----------    -----------------------   ----------------------    ------------
SUNDA          0x05  =  5                 6                          1
CAYMAN         0x0c  = 12                 13                         1
MARIANA        0x14  = 20                 21                         1
MARIANA_PLUS   0x1c  = 28                 29                         1
MAVERICK       (none — no NCFW byte)      37 (observed two ways)     1*  (INFERRED)
```

The `get_image` ladder, byte-exact, routes each `arch_id` to its firmware-version
leg and defaults everything above `0x1c`:

```
  1199: cmpl $0x1c,-0x4(%rbp) ; je 12a7  ->  v4+  (MARIANA_PLUS)
  11a7: ja   12ec                        ;  arch_id > 0x1c  ->  default (return 2)
  11ad: cmpl $0x14,-0x4(%rbp) ; je 1262  ->  v4   (MARIANA)
  11c1: cmpl $0x5, -0x4(%rbp) ; je 11d2  ->  v2   (SUNDA)
  11c7: cmpl $0xc, -0x4(%rbp) ; je 121a  ->  v3   (CAYMAN)
  11cd: jmp  12ec                        ;  default -> return 2
```

The selector set is **exactly** `{0x1c, 0x14, 0x05, 0x0c}`. There is **no**
branch on `0x24` (= 36) and **no** branch on `0x25` (= 37). A hypothetical
MAVERICK `arch_id 0x24` falls through the `ja` arm to the `return 2` default —
the NCFW image→generation map is a *total function on the four shipped
generations and undefined for MAVERICK*. **[HIGH / OBSERVED — re-disassembled
this session; the absent `0x24` is OBSERVED-negative.]**

> **Adversarial self-verify.** The ladder is *unordered* (`0x1c` first, then a
> `ja` guard, then `0x14`, `0x05`, `0x0c`), which makes the codename↔v# pairing
> easy to mis-read — and a prior cross-reference wave did exactly that (it
> crossed the CAYMAN/MARIANA labels on the v3/v4 rows). The canonical pairing is
> triple-anchored and re-checked here: the `je` targets resolve in firmware-blob
> address order (`v2 < v3 < v4 < v4+`), the four `.c` source strings are
> `sunda.c < cayman.c < mariana.c < mariana_plus.c`, and the eight
> `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blob symbols sit at strictly increasing
> `.rodata` offsets in the same order. All three agree: `0x05=v2=SUNDA`,
> `0x0c=v3=CAYMAN`, `0x14=v4=MARIANA`, `0x1c=v4+=MARIANA_PLUS`.

---

## 3 · The MAVERICK row — what is OBSERVED, what is INFERRED

MAVERICK is the one row where the two key columns disagree on their grounding.
Carry this distinction exactly.

**`coretype 37` is OBSERVED** — anchored two independent ways in the firmware,
not by the +8 stride alone:

1. **The header enum ordinal.** The shipped `nrtucode.h` `NRTUCODE_CORE_*` enum
   auto-increments from `SUNDA_NX_ACT = 0`. Each generation's `Q7_POOL` member
   lands at `6 / 13 / 21 / 29 / 37`. The +8 stride is the enum's *own* structure:
   the MAVERICK block drops `NX_ACT` (the ACT-into-DVE fold) and drops `Q7_CCE`,
   but adds an `NX__REMOVED__` placeholder, so `MAVERICK_NX_POOL` still lands at
   `29 + 8 = 37`. This is a direct firmware-header observation of `37`, not an
   extrapolation. **[HIGH / OBSERVED]**
2. **The two runtime resolver immediates.** In the internal twin,
   `ll_get_libraries_from_opcodes` does `cmp $0x25,%edi` (37) with
   `movabs $0x2020202000` (bit 37 set → `{13,21,29,37}`), and
   `opset_get_library_index` does `cmp $0x25` with `movabs $0x2020202040`
   (bit 37 set → `{6,13,21,29,37}`). Both accept `coretype 37`. The shipped
   front lib rejects it. **[HIGH / OBSERVED — bitmasks decoded this session.]**

**`arch_id 36*` is INFERRED** — there is *no* firmware byte for it:

- The only `arch_id`-keyed firmware structures are the two NCFW selectors
  (`get_image`, `ctx_log`), whose ladders compare *exactly* `{0x05,0x0c,0x14,0x1c}`
  with a `ja` default. There is **no `0x24` and no `0x25`** NCFW selector byte.
- No `arch_id` symbol, define, or table anywhere emits `36` for MAVERICK.

So `arch_id 36 = 0x24` is `coretype − 1 = 37 − 1`, consistent with the four
shipped rows but with no direct anchor. Mark it `36*` and never present it as
binary-observed. **[MED / INFERRED]**

**Why there is no NCFW image at all.** `libncfw.so` ships exactly eight
firmware-blob symbols `{v2,v3,v4,v4_plus} × {iram,dram}`; the symbol immediately
after `v4_plus_ncfw_iram_bin_size` is the compiler-generated `__GNU_EH_FRAME_HDR`
— the blob region is physically closed, with no room for a fifth image. Zero
`maverick`/`v5` string; four `.c` strings only. MAVERICK is realised only in the
newer (clang-15) internal symbol twin's dispatch tables and its four
internal-only Q7 ELFs. **[HIGH / OBSERVED — multi-binary, §4.]**

> MAVERICK still *has* collective capability — it simply does it the same
> on-engine way CAYMAN..MARIANA_PLUS do (the Q7-POOL `SB2SB_COLLECTIVE` opcode
> `0xBF` + the SP/sync engine), not via an NCFW management core. The NCFW absence
> is a missing *orchestration layer*, not a missing feature. See
> [The MAVERICK Profile](../generations/maverick-profile.md).

---

## 4 · Shipped vs internal — the 4/5 split

"Is GPSIMD four generations or five?" is a binary-version question, not an
inconsistency. The shipped runtime path tops out at MARIANA_PLUS; MAVERICK lives
only in the non-shipped symbol twin.

| lib | SUNDA | CAYMAN | MARIANA | M_PLUS | MAVERICK | bound |
|---|---|---|---|---|---|---|
| `libncfw.so` (NCFW images) | v2 | v3 | v4 | v4+ | — | `arch_id ≤ 0x1c` (28) |
| `libnrtucode.so` (front getter) | ✓ | ✓ | ✓ | ✓ | — (stub) | `ct ≤ 29` / idx `≤ 0x17` |
| `libnrtucode_extisa.so` (blob container) | ✓ | ✓ | ✓ | ✓ | — | n/a (container) |
| `libnrtucode.a` (static) | —¹ | ✓ | ✓ | ✓ | — | n/a |
| `libnrtucode_internal.so` (twin) | ✓² | ✓ | ✓ | ✓ | **✓** | `ct ≤ 0x25` (37) |

¹ SUNDA contents resolved from the container as a weak-undef. ² The internal twin
also carries the SUNDA weak-undef plus the four MAVERICK-only Q7 ELFs.

The split is sharp in the string tallies: a `maverick` literal appears **189
times** in `libnrtucode_internal.so` and **0 times** in the shipped front
`libnrtucode.so` (re-counted this session). Whether a field deployment links the
4-gen front or a 5-gen internal build is out of scope of these binaries.
**[HIGH / OBSERVED; the "internal twin = newer 5-gen build" reading is
MED / INFERRED.]**

---

## 5 · TONGA — the pre-unified outlier (out of the stride model)

TONGA is a real, older codename, but it is **not** a sixth GPSIMD generation and
it is **not** part of the unified `coretype = arch_id + 1` / +8-stride family. It
is the legacy "L"-family ISA-header predecessor — the historical NC-v1 from which
the modern `neuron_*_arch_isa` headers descend — with **zero** GPSIMD runtime
identity:

- **No** `coretype`, **no** `arch_id`, **no** `NRTUCODE_CORE_TONGA`, **no**
  `tonga_libs`, **no** NCFW image, **no** EXTISA blob; absent from every
  `get_image` / `get_ext_isa` / `get_num_ext_isa_libs` selector. A
  `strings | rg -i tonga` on the internal twin returns zero. **[HIGH / OBSERVED]**
- Its dtype enum is a distinct **`TONGA_ISA_TPB_DTYPE_*`** family of **8 codes**
  (`INVALID, UINT8, UINT16, BFLOAT16, FP16, INT32, FP32, INT64`) — a strict
  subset of the 16-code `NEURON_ISA_TPB_DTYPE_*` set the five GPSIMD generations
  use. A different enum *name family* (`TONGA_` vs `NEURON_`) means a different,
  older ISA, not a GPSIMD generation. **[HIGH / OBSERVED — header read this
  session.]**
- On disk, `arch-headers/` has **six** dirs (the five codenames + `tonga`), but
  `neuron_*_arch_isa/` has only **four** ISA dirs `{sunda, cayman, mariana,
  maverick}`. TONGA appears only in the legacy `arch-isa/` tree, never as a
  `neuron_tonga_arch_isa` GPSIMD ISA. **[HIGH / OBSERVED]**

TONGA is kept in the table's *chronology* row for completeness — and bound to the
Inf1 product / legacy `V1` arch by the platform compiler surface (`Tonga →
"Inferentia"`) — but it is **explicitly excluded from the five-generation runtime
line**. Anyone who finds `tonga` in an ABI header must treat it as the legacy
"L" / TONGA ISA, never as a sixth coretype.

The full ordering, oldest → newest:

```
TONGA(NC-v1, legacy)  ⊳  SUNDA(v2)  ⊳  CAYMAN(v3)  ⊳  MARIANA(v4)  ⊳  MARIANA_PLUS(v4+)  ⊳  MAVERICK(v5)
└── outside the unified stride ──┘  └──────── coretype = arch_id + 1, +8 stride ────────┘
```

---

## 6 · The "v5" label is overloaded — disambiguate

Two distinct "v5"-shaped tokens live in this corpus, on **different axes at
different layers**. Conflating them is the most common cross-reference error, so
keep them apart explicitly:

| token | axis | what it actually is | coretype / arch_id |
|---|---|---|---|
| **`CoreV5` / `core_v5`** | compiler ArchLevel (host platform / `libwalrus`) | the **Trn3-PRE / MARIANA_PLUS** variant slot — a Trn3-family *pre-release*, shipped-tooling-visible | `coretype 29` / `arch_id 0x1c` (an **NCFW-present** gen) |
| **`NC-v5` / GPSIMD `coretype 37`** | GPSIMD coretype / NC-banner axis | the genuine 5th **silicon** — **MAVERICK** | `coretype 37` / `arch_id 36*` (**no** NCFW image) |

The platform's index crosswalk places `CoreV5` *under* Mariana/Trn3 as the
"trn3pre variant" — not a fifth silicon row. So the host compiler reaching
"CoreV5" does **not** mean it reaches MAVERICK; it means it enumerates a Trn3-pre
ArchLevel. The GPSIMD `coretype-37` anchors in §3 (the header ordinal + the twin
resolver bytes) are on the GPSIMD axis and are independent of the compiler
ArchLevel axis. **[HIGH / OBSERVED for both axes; the
"`CoreV5` = MARIANA_PLUS-region, not MAVERICK" reading is MED / INFERRED-STRONG,
anchored on the explicit "trn3pre variant" label.]**

The product / PCI-ID / DKMS-arch columns in §1 are likewise on a *different
surface* from the GPSIMD firmware. They are observed in the host platform
compiler readings (`Sunda → "Trainium1"`, `Cayman → "Trainium2"`,
`Mariana → "Gen4"/Trn3`), the DKMS `NEURON_ARCH_V2/3/4` PCI-ID table
(`0x7164 … 0x7565`), and the NKI `nc_version` gen-config (`gen2/gen3/gen4`) — and
**every one of those host surfaces caps at MARIANA / Trn3 / `NEURON_ARCH_V4`**.
The numeric `coretype → silicon-part` binding is **not inside any GPSIMD binary
in this corpus**; the product column is carried, not firmware-grounded, and the
MAVERICK product family is genuinely open (no "Trn4" is named anywhere — do not
fabricate one).

---

## 7 · Per-generation EXTISA blob identity (one line each)

The substantive per-generation device descriptor is the compiled Xtensa Q7
EXTISA `*_SO` image (its `kernel_info_table` *is* the opcode→function table); the
accompanying `*_JSON` blobs are 32-byte `{"dummy_message": "hello world"}` stubs
in every generation except SUNDA.

| gen | EXTISA libs | image identity (vs siblings) | toolchain |
|---|---|---|---|
| **SUNDA** | 1 (`Q7_POOL_RELEASE_EXTISA_0`, weak-undef) | the **only** generation with a real 17-function JSON opcode manifest; body resolved from the container | XtensaTools-14.09 / clang-10 |
| **CAYMAN** | 4 (`EXTISA_0..3`) | own compile; sizes `a260/f5c/1500/6974` | XtensaTools-14.09 / clang-10 |
| **MARIANA** | 4 | distinct compile of the CAYMAN opcode contract | XtensaTools-14.09 / clang-10 |
| **MARIANA_PLUS** | 4 | **byte-identical to MARIANA** for all four libs (sha256 match); adds **zero** new EXTISA bytes | XtensaTools-14.09 / clang-10 |
| **MAVERICK** | 4 (internal twin only) | entirely separate build — DYN/PIC, fully stripped, distinct sha256 | **XtensaTools-15.05 / clang-15** |

The five per-generation lib tables are `nm`-confirmed at
`sunda_libs@0x9b8f80 < cayman_libs@0x9b8f90 < mariana_libs@0x9b8fd0 <
mariana_plus_libs@0x9b9010 < maverick_libs@0x9b9050`, indexed by a `coretype − 6`
jump table. The opcode *contract* (`lib0 = 17 / lib1 = 1 / lib2 = 2 / lib3 = 9`
entries) is identical across all four shipped generations — only the compiled
machine code differs. **[HIGH / OBSERVED]**

A key structural fact this implies: **MARIANA ≡ MARIANA_PLUS at the device-image
level.** Their EXTISA blobs are byte-identical, their NCFW DRAM is byte-identical,
and MARIANA_PLUS has **no** `neuron_mariana_plus_arch_isa` dir — it shares
MARIANA's ISA. MARIANA_PLUS is a distinct codename at the NCFW / getter / coretype
level (its own `arch_id 0x1c`, its own `ctx_log`, its own NCFW IRAM, its own
`*_libs`, its own `arch-headers` dir) but a *code / feature-flag delta* on the
MARIANA silicon ISA — selected at runtime via `NEURON_RT_DBG_V4_PLUS=0/1` (the env
that replaced the removed `NRTUCODE_MPLUS_ON_MARIANA` flag, whose
"flag has been removed" tripwire string is still present in the shipped front
lib). **[HIGH / OBSERVED]**

---

## 8 · Provenance and spot-checks (re-run this session)

```
NCFW = neuronx-runtime/.../opt/aws/neuron/lib/libncfw.so
INT  = neuronx-gpsimd/.../custom_op/c10/lib/libnrtucode_internal.so   (5-gen twin, not stripped)
SO   = neuronx-gpsimd/.../custom_op/c10/lib/libnrtucode.so            (4-gen shipped front)
INC  = neuronx-gpsimd/.../custom_op/c10/include

# NCFW selector ladder -> {0x1c,0x14,0x05,0x0c}, ja default for >0x1c, no 0x24/0x25:
objdump -d $NCFW | rg -A60 '<libncfw_get_image>:' | rg 'cmpl|ja '
# NCFW codenames (four .c strings only, no maverick.c):
strings $NCFW | rg -x '(sunda|cayman|mariana|mariana_plus).c'
# NCFW blob symbols (eight: v2/v3/v4/v4_plus x iram/dram, no v5):
nm $NCFW | rg '_ncfw_(iram|dram)_bin$'
# coretype 37 resolver immediates (cmp $0x25 + bit-37 masks):
objdump -d $INT | rg -A40 '<nrtucode_get_num_ext_isa_libs>:' | rg 'cmp .*0x25|movabs'
#   python: 0x2020202000 -> {13,21,29,37};  0x2020202040 -> {6,13,21,29,37}
# five per-gen lib tables (sunda<cayman<mariana<mariana_plus<maverick):
nm $INT | rg '(sunda|cayman|mariana|mariana_plus|maverick)_libs$'
# maverick split: internal=189, front=0:
rg -c -i maverick $INT ; rg -c -i maverick $SO
# 5-gen enum (internal) vs 4-gen front; arch-isa dirs (4) vs arch-headers (6 incl tonga):
strings $INT | rg -o 'NRTUCODE_CORE_[A-Z_]+'
ls -d $INC/neuron_*_arch_isa ; ls $INC/arch-headers
# maverick dtype expansion + SB2SB; tonga subset:
rg 'FP4_EXP2|FP8_EXP2|SFP8_E8|CPTC1|MXTENSOR_V2|SB2SB_COLLECTIVE' \
   $INC/neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h
rg 'TONGA_ISA_TPB_DTYPE_' $INC/arch-isa/tpb/aws_tonga_isa_tpb_common.h
```

> **A note on grounding.** Naming and DWARF caveats specific to this corpus: the
> device config libs carry full `.symtab` names (use `nm`) but no DWARF — line
> info lives in the host `libnrt.so` `.debug_info`. The `.data` VMA↔file-offset
> delta is per-binary; measure it with `readelf -SW` before any `.data`
> `xxd`/`objdump`. `extracted/` is gitignored, so the paths above are absolute /
> `--no-ignore`. Every count on this page is grounded via `nm`/`rg -c` against the
> binary, not a decompile grep.

---

## See also

- [The Gen-Invariance Thesis](../orientation/gen-invariance.md) — why most of the
  GPSIMD machine is identical across these rows, and the few axes that actually move.
- [Codename ↔ Generation Map](../generations/codename-generation-map.md) — the
  Part-6 narrative version of this table with the per-generation deltas.
- [SUNDA v2 Baseline Topology](../generations/sunda-v2-baseline.md),
  [MARIANA_PLUS (v4+) Generation Delta](../generations/mariana-plus-delta.md),
  [MAVERICK (v5) Profile](../generations/maverick-profile.md) — the per-row
  deep-dives.
- [Cross-Generation Arch-ISA Header Diff](../generations/arch-isa-header-diff.md)
  and [Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md)
  — the dtype/opcode expansion chain summarised in §5/§7.
- [Master Per-Generation Capability Matrix](../generations/master-capability-matrix.md)
  — the full 15-subsystem × 5-generation grid.
- [Appendix: Codename ↔ NC-ver ↔ coretype ↔ arch_id Cross-Walk](../appendix/codename-crosswalk-table.md)
  — the printable quick-reference card.
