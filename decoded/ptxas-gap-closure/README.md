# ptxas gap-closure — confidence-upgrade pass

Binary-derived resolution of the open / MEDIUM-confidence items left by the prior ptxas
deep-mine (CUDA 13.0.88, `ptxas/ptxas`, 37.7 MB stripped x86-64 ELF). Each item was driven by
constructor-grounded disassembly and cross-checked control/data flow. On any mismatch with prior
docs the binary wins.

All claims are binary-derived: every recovered fact is cited to a binary address/value. Recovered
names come from the binary's own embedded strings, vtables, and JSON-dumper keys. Where the shipping
13.0.88 binary exposes fields or suffixes that older documentation lacks, those are called out as
binary-only additions.

## Files

| File | Contents |
|---|---|
| `resolved_items.tsv` | One row per (item, subitem): resolution, binary_evidence, corroborated, confidence |
| `README.md` | This file |
| `wiki_corrections.md` | Exactly which already-published wiki pages/tables to correct, with the change |

## Items and verdicts (summary)

| Item | Verdict | New confidence |
|---|---|---|
| F1 +40 sched_slot | **CONFIRMED** pointer to a lazily-allocated, polymorphic (vtable@0) per-inst scheduling/latency/barrier record; the SASS control word lives in this record's interior — UPGRADE LOW→HIGH | high |
| F1 +32 control_word | **CORRECTION**: reserved/dead u32 (zero-init only); no opcode-co-located reader. The control word is in the +40 record, not at +32 | high |
| F1 operand bit31 | = is-DESTINATION flag, **not** sign/negate — CORRECTION | high |
| F1 packed kind (bits28-30) | 3-bit DAG kind: Unknown/VReg/Imm32/Imm64/Lab/Sym/Info/Null (diverges from prior wiki behavioral labels; binary arbitrates) | high (DAG enum) |
| F1 32B descriptor kinds | constructor-grounded: 3=Imm, 9=cond-pred, 10=reg-alt-src, 13=addr-base, 14/16=mem-offset, 15=imm-twin; 4/5/7/8/11=match-only variants. md-style enum REFUTED | high (3,9,10) / med (rest) |
| F1 kind 1↔2 anchor | OPEN: predicate-helper naming (1=reg,2=pred) vs emitter sentinel widths (swapped) — structure solid, name binding unresolved | med |
| F4 category | encoding/opcode-CLASS family, NOT SM-generation | high |
| F4 variant | sequential modifier-driven sub-opcode; alt-encoding is a separate runtime flag | high |
| F4 field_a | .text ptr to per-operand-field encoder fn (tail-calls bit-packer) | high |
| F4 field_b | operand-field-kind classifier code (0=none … 0xFFFFFFFF=end) | high |
| F5 coalescing | conservative interference-checked, preference/bias-driven optimistic coloring | high |
| F5 class-6 selector 5/6/7 | Tensor/Acc accumulator operand-vector indices; paired-lane semantics live in a separate constraint-type switch | high |
| F5 variant codes 2/4 | **CORRECTION**: variant = per-generation chip ordinal, NOT a suffix selector. var2=sm_86, var4=sm_88(gen-7)/sm_120(gen-9). a/f suffixes are an orthogonal naming property | high |
| F6 families | discrete per-arch lowering generations (4 method-table families + COORD in the binary) | high |
| F6 type_id 0-25 | DagType enum (UNK…ADDRESS); full naming recovered | high |
| F8 OKT flags | **CORRECTION**: descriptor table (72B stride @0x1CE9C40) has one xref = the JSON dumper; flags are emitted verbatim, NEVER bit-tested by ptxas → flag bits are a static-init schema convention for external tooling, not runtime behavior. okt_descriptors.tsv field layout was shifted +16B | high (untested) |
| F8 descriptor↔name join | RECOVERABLE & confirmed (PICK-order == enum-order == descriptor index) | high |

## Method

- Binary: `ptxas/ptxas` + 39,894 decompiled functions in `ptxas/decompiled/`, `objdump`/`nm`/`r2`.
  VA→file: `.text`/`.rodata` use VA−0x400000; `.data` uses VA−0x400000 too but VMA≠fileoffset for
  some structs (delta 0x400000) — handled per region.
- Constructors, allocator call sites, and vtable layouts were the primary lever. The **296-byte
  instruction object** is the code-generator-level `Inst` record (the larger object built by the IR
  constructors), NOT the smaller assembler-level instruction record — establishing this from the
  allocator size resolved several offset questions and corrected the operand bit-31 meaning.

## Scope notes (binary-only additions)

- Arch suffixes `a`/`f`/`c` (variant codes) are handled only by the newer SM-string parser in this
  binary. Variant-code suffix semantics are therefore binary-only.
- The OKT knob descriptor `flags`/`param`/`default`/`bss_offset` fields are 13.0.88 runtime additions
  beyond the basic `{name, type, description}` schema. Flag-bit meanings are binary-only.
- The 32-byte ISel operand-descriptor kind enum (1–16) is a lowered per-arch descriptor distinct from
  the 8-value DAG-level packed kind; the lowered classes are binary-only.
