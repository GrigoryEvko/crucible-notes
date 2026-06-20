# Wiki pages/tables to correct (from gap-closure findings)

Each entry: the published wiki location, the current claim, and the correction. Corrections are
binary-grounded; every recovered name comes from the binary's own embedded strings, vtables, and
JSON-dumper keys, cited to a binary address/value. DO NOT edit the wiki in this pass — this file is
the change list.

---

## 1. `src/ir/instructions.md` — operand packed-word bit 31 (CORRECTION, high confidence)

- **Lines 193, 196–197, 230**: bit 31 labeled "sign/negative flag (S)" / `is_neg = (operand_word >> 31) & 1`.
- **Correction**: bit 31 is the **is-DESTINATION (def) marker**, not a sign/negate flag. Evidence: the
  operand reader `sub_7E6090` tests `operand_word < 0` (bit 31) together with `kind == 1` to mark
  *register definitions* for ref-count/def-use bookkeeping (`sub_7E6090:257,281`; def flag `0x8000000`
  set when the operand is a def). Operand negate/abs modifiers live in the modifier byte (bits 20–27),
  not bit 31. Rename the field `S` → `D` (def), and the `is_neg` variable → `is_def`.

## 2. `src/ir/instructions.md` — operand type field (bits 28-30) table (FLAG / re-verify, medium)

- **Lines 209–218**: the bits-28-30 type table lists `2=Predicate register, 3=Uniform register,
  4=Address/offset, 5=Symbol/constant, 6=Predicate guard, 7=Immediate`.
- **Issue**: this is a behavioral inference. There are **two different operand-kind encodings** in ptxas:
  (a) the 3-bit *packed-word* kind (bits 28-30, only 8 values), and (b) the 32-byte *ISel descriptor*
  kind (1–16). The page conflates them. The packed 3-bit kind cannot hold 8 distinct semantic operand
  classes plus immediate plus uniform-reg plus address — that is the 16-value descriptor space.
- **Action**: split the page into (a) packed-word kind (8 values) and (b) descriptor kind (16 values),
  and re-label per the binary determination (see `resolved_items.tsv` rows F1-IR packed-kind and
  F1-ISel 32B-kinds). The known anchors: kind 1 = register; the guard predicate is the *last* operand;
  type 7 (all bits set) is the sentinel/unused value. (Pending binary agent for the exact 16-value
  descriptor names; do not publish the current 2/3/4/7 packed labels as settled.)

## 3. `src/ir/instructions.md` — +40 sched_slot (UPGRADE, high confidence)

- **Line 23**: `+40 ptr sched_slot "Scheduling state pointer"` — currently stated, prior confidence LOW.
- **Correction/upgrade**: keep the type as a **pointer**; upgrade to HIGH confidence and add: the
  pointer is **lazily allocated** (NULL until the scheduler runs), holds the per-instruction scheduling
  state (availability link + SASS control fields), and is allocated on first scheduling use. This is the
  per-inst SchedInfo handle, not an inline scalar.

## 4. `src/ir/instructions.md` — +32 control_word (CORRECTION, high confidence)

- **Lines 21, 338–344**: `+32 control_word "Scheduling control word (stall cycles, yield...)"`.
- **Correction**: `+32` is a **reserved/dead u32** (zero-init only; `sub_7DD010@0x7dd043 movl $0,0x20`).
  An exhaustive sweep of every function using the `0xFFFFCFFF` opcode mask found **no reader/writer of
  `instr+32` on the same object that reads `instr+72`**. The SASS control word (stall/yield/wait-barrier/
  read-barrier/reuse) does **not** live at `+32` — it lives **inside the record pointed to by `+40`**
  (barrier bits at `(*(+40))+168`/`+172`, stall cycles at `(*(+40))+144`). Re-label `+32` as
  reserved/unused and move the "Control Word" section (lines 338–344) to describe the `+40` record's
  interior. (The prior LOW confidence came from instruction-`+32` vs VREG-`+32` (`coalesce_chain`) offset
  aliasing.)

## 5. `src/ir/registers.md` — coalescing model (ADD detail, high confidence)

- **Lines 599+**: coalescing pass `sub_9B1200` "merges source and destination VRs into the same physical
  register"; alias chain followed during assignment.
- **Add**: classify the model — it is **conservative, interference-checked, preference/bias-driven
  optimistic coloring** (not pure preference, not classic Briggs degree-count). Coalesce candidates are
  *linked* and colored first (pushed on top of the coloring stack); the merge is committed at color time
  only if the target physical register passes a point-interference bitvector check (plus alignment and
  "don't split a vector word" checks). Sub-register aliasing supports SAME / high-subword / low-subword
  coalescing (half↔double size). Gated by the `RegAllocCoalescing` knob; MAC/MMA accumulator coalescing
  is a separate enable.

## 6. `src/ir/registers.md` — class-6 sub-register selector 5/6/7 (ADD, high confidence)

- **Lines 35, 174, 191**: class 6 documented as sharing a class-3/class-6 retry channel.
- **Add**: register **class 6 = Tensor/Accumulator** (MMA/WGMMA). The selector values 5/6/7 are
  **computed accumulator operand-vector indices** (accumulator base operand at
  `operand[numOps − pairAdj − 5]`, then +1/+2 walk consecutive 32-bit accumulator lanes); the partition
  *span* comes from a separate 3-bit field `(desc & 7)+1` scaled by instruction subtype (×2 for subtype
  19, ÷2 for subtype 13). The static "paired low / paired high / aligned pair" meaning belongs to a
  *different* constraint-type switch (driven by pair-align bits 26–27), not to the class-6 value itself.

## 7. `src/reference/instruction-legality.md` — record struct (CORRECTION + refine, high confidence)

- **Lines 29–30**: dominant record stride 24 B `{key:u32, key_hi:u32, handler:u64, reserved:u64}`.
- **Refine to the true TWO-LEVEL structure** (reader `sub_10EAFD0`; twin `sub_10EAF00` drives a parallel
  table at `0x2304BA0`):
  - **Level 1** — a 16-byte descriptor array at `0x22FD8C0`, one per instruction class:
    `{+0x00 base_ptr → level-2 sub-table, +0x08 count}` (e.g. `{0x2304AE0, 7}`, `{0x2304A60, 5}`);
    indexed by class via `shl $4` then `mov 0x22fd8c0(%rax)`.
  - **Level 2** — 24-byte (`0x18`) legality records (size proven via `lea(rax,rax,2);shl$3`, next-record
    `lea 0x18`, and reverse `imul …;sar $3`): `+0x00` **key** (byte0=class, byte1=index; records sorted
    ascending, looked up by **binary search** `cmp (%rcx),%r10b` + `movzbl 0x1(%rcx)`); `+0x08`
    **handler** = an Itanium C++ **pointer-to-member-function** that is **called** (`jmp *%rax`), *not*
    compared (low bit set ⇒ virtual, even ⇒ direct; direct stubs are `mov $1,%eax; ret` always-legal
    validators at `0x118C000`/`0x118E200`, some are real validators); `+0x10` = the **`this`-pointer
    adjustment** of the member-fn-ptr pair (`ctx + record[+0x10]`), `0` in all observed records.
- **Correction**: `+0x10` is **not** a flags/`reserved` field and **not** an sm-gating mask, and there is
  **no** `0x08000000` "run generic validation" sentinel in the record — those prior interpretations are
  wrong. Arch gating lives inside the per-handler validators, not in the record.

## 8. `src/codegen/encoding-tables.md` — category/variant + bitfield lookup (ADD, high)

- **Add** the dispatch-row semantics: 24-byte rows `{handler, pad, (category<<8 | variant)}`, where
  **category = encoding/opcode-class family** (NOT an SM-generation bucket — the SM split is selected by
  *which* per-SM array is indexed) and **variant = a sequential modifier-driven sub-opcode** (the
  alternate-encoding choice is a *separate* runtime flag on the operand struct, not the table variant).
- **Add** the `encoding_bitfield_lookup` semantics: `field_a` (when `field_b==0`) = a `.text` pointer to
  a per-operand-field **encoder** function that marshals parsed operand fields and tail-calls the
  bitfield-insert primitive (`setBits(start,width,value)` writing the SASS code bitstring); `field_b` =
  an operand-field-**kind classifier** code (0 = none; small codes = specific field classes;
  `0xFFFFFFFF` = end-of-field-list sentinel).

## 9. `src/targets/version-codes.md` — variant byte semantics (MAJOR CORRECTION, high confidence)

- **Current claim** (the page's variant model): variant 0 = plain, 1 = `a`, 3 = `c`, 5 = `f` — i.e. the
  low byte is a **suffix-letter selector**.
- **Correction**: the variant byte is a **per-generation chip ordinal index, NOT a suffix selector**.
  There is no runtime code mapping a variant number to a letter. The `a`/`f` suffixes are an *orthogonal*
  hardcoded naming property — `sm_120`, `sm_120a`, `sm_120f` all collapse to the *same* internal code
  `0x9004` (stamped at `profile+0x15c`); the suffix letter is discarded once the profile is built.
  Concretely:
  - gen-7: var1=`sm_80`, var2=`sm_86`, var3=`sm_87`, var4=`sm_88`, var5=`sm_89` (the contiguous
    `compute_86/87/88/89` strings at VA `0x201f4d6/4e1/4f2/503` prove the ordinal run).
  - gen-9: var0=`sm_100`, var1=`sm_101`/`sm_110`, var3=`sm_103`, var4=`sm_120`, var5=`sm_121`.
  - So **variant 2 = `sm_86`** (gen-7; slot `0x9002` is zero/unused in gen-9), **variant 4 = `sm_88`**
    (gen-7) / `sm_120` (gen-9). Both are real, user-selectable `-arch` targets — *not* internal-only,
    and *never* a suffix character. "variant 3" is `sm_87`/`sm_103`, never a `c` suffix.
  - String inventory: only `a` (15×) and `f` (14×) suffix strings exist, all gen-9; no `b/c/d/e`. The
    apparent `sm_9f` is a disassembler artifact of the hardcoded `sm_90a` deprecation-message builder.
  - Constructors: `0x60ac30 movl $0x7002` (sm_86), `0x60ab30 movl $0x7004` (sm_88), `0x608df0 *=0x9004`
    (sm_120); reader `sub_60FBF0:871` stores `word_2020620[arch_enum-20]` opaquely, never extracting a
    letter.
- **Action**: replace the suffix-selector model with the chip-ordinal model; keep a separate short note
  that suffix letters (`a`/`f`) are a name-registration property collapsing multiple names to one code.

## 10. ISel families page (`src/codegen/*` / `src/ir/*`) — node-family → SM mapping (ADD, high)

- **Add**: the 4 ISel node-family method-table groups (`0x22A5AA0`, `0x22A6E70`, `0x22A8248`,
  `0x22A9BB0`) + the COORD family (`0x22AA9F8`) are discrete **per-architecture lowering generations**,
  not a single parameterized path. Map them to codegen generations (Volta/Turing, Ampere/Ada,
  Hopper/Blackwell, Mercury/Blackwell-tensor) per the binary determination; COORD = shared/coordinator
  family. Add the operand `type_id` 0–25 enum (DagType: UNK, NONE, BITS8/16/32/64, FLOAT, HALF, FIXED,
  LONG, ULONG, INT, UINT, SHORT, USHORT, BYTE, UBYTE, DOUBLE, BOOL, LBOOL, LOGICAL, SLOGICAL, TEXTURE,
  SAMPLER, CC, ADDRESS).

## 11. Knobs page / `decoded/ptxas-knobs-builtins/okt_descriptors.tsv` — OKT descriptor (CORRECTION + ADD)

- **Correct the descriptor field layout** (`okt_descriptors.tsv` was shifted by 16 bytes / 2 fields). The
  descriptor table is at VA `0x1CE9C40`, 1000 entries, **72-byte stride (9 qwords)**, with the canonical
  field map taken from the binary's own JSON dumper keys: field[0]@0x00 = bss_offset slot, field[2]@0x10
  = type, field[4]@0x20 = min, field[5]@0x28 = max, field[6]@0x30 = default, field[7]@0x38 = stepsize,
  **field[8]@0x40 = flags**. (The prior tsv's "field[6] flags" was actually field[8]'s bytes under a
  base offset by +16; the flags *values* were right, the field index/base were wrong.)
- **Correct the flags interpretation**: ptxas does **not** bit-test the OKT flags. The descriptor table
  has exactly **one** xref — the JSON knob dumper `sub_446240` (the `-knob DUMP_KNOBS_TO_FILE` path) —
  which emits the flags string *verbatim*. There is no `&1/&2/&4`/`strtol` on the flags field anywhere.
  So bits 0x1/0x2/0x4 are a **source/static-init metadata convention** (schema for external tooling),
  with no runtime behavior in this binary. The runtime knob *values* live in a separate `.bss` region
  indexed by field[0] = bss_offset (parsed via the 64-byte name objects), not this table. The descriptor
  is **9 `char*` to ASCII strings** (a JSON schema, not a packed binary struct) — which is *why* the flags
  string is emitted verbatim. Loaded by exactly one instruction `mov $0x1ce9c40,%ebx @0x44675C` inside
  `sub_446240`. Entry count is exactly `(0x1CFB580 − 0x1CE9C40)/72 = 1000`.
- **Statistical** per-bit meanings (n=1000; mark as a static-init schema convention for external
  tooling, not runtime behavior, since untested): `0x1` ≈ has-explicit-default; `0x2` ≈
  range-checked/tunable (the "hidden/internal gating"
  hypothesis is *refuted* — the dumper emits all knobs); `0x4` ≈ budget/category tag (57/59 set entries
  are `OKT_BDGT`; the combo `0x5` never occurs). Histogram: 0x0:534, 0x2:155, 0x3:112, 0x1:103, 0x4:59,
  0x6:36, 0x7:1.
- **Add**: the descriptor↔name positional join is **recoverable** — descriptor index `i` ↔ name index
  `i` (the knob table and the knob-index enum are built in lockstep from one ordered macro; runtime
  confirms the same `i` indexes the 64-byte ROT13-name array `*(reg+16)+(i<<6)` and the 72-byte value
  array `*(reg+72)+72*i`, with `GetKnobIndex = sub_79B240` returning `i`). **Caveat for alignment**: ~19
  leading entries in the descriptor table are non-knob helper strings (`NamedPhases`,
  `DUMP_KNOBS_TO_FILE`, `GetKnobIndex`, phase names) that must be filtered before aligning the 1000
  descriptors with `knob_names.tsv`. Type tokens: NONE/INT/BUDGET/INT_RANGE/INT_LIST/DOUBLE/STRING/WHEN/
  OPCODE_LIST; the OPCODE_LIST value space is
  {ARRIVES, DFMA, FFMA, HFMA2, HMMA, IMAD, IDP, IMMA, LDG, LDGSTS, LDS, LDSM, MEMBAR, STG, STS, XMAD}.
