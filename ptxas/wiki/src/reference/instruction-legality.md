# Instruction Legality Matrix

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

Whether a given `(opcode, modifier-combination)` is legal on the selected target is decided by a
layered gate. The central data structure is a **two-level per-class dispatch table** in `.rodata`;
it is one axis of a three-layer model.

## The legality table — two levels

The reader is `sub_10EAFD0` (a twin, `sub_10EAF00`, drives a parallel table at `0x2304BA0`). It is
**not** a single flat array; it is a two-level structure indexed first by instruction class, then
by `(class, index)` key.

### Level 1 — per-class descriptor array (16 B records)

At VMA `0x22FD8C0`, one 16-byte descriptor per instruction class:

```text
+0x00  u64  base_ptr    → the level-2 sub-table for this class
+0x08  u64  count        number of level-2 records in that sub-table
```

The class index selects a descriptor by `shl $4` (× 16) then `mov 0x22FD8C0(%rax)`. Observed
descriptors include `{0x2304AE0, 7}` and `{0x2304A60, 5}` — i.e. small per-class sub-tables.

### Level 2 — sorted legality records (24 B records)

Each level-1 `base_ptr` points to an array of **24-byte (`0x18`)** records. The 24-byte stride is
proven three independent ways in the reader: the index multiply `lea (%rax,%rax,2); shl $3`
(× 24), the next-record advance `lea 0x18(%rcx)`, and the reverse division `imul …; sar $3`
(÷ 24). Each record is:

```text
+0x00  u32  key            byte0 = class, byte1 = index   (records SORTED ascending by key)
+0x08  u64  handler        Itanium C++ pointer-to-member-function — CALLED, not compared
+0x10  u64  this_adjust    the member-fn-ptr pair's this-pointer adjustment (0 in all observed records)
```

Lookup is a **binary search** over the sorted keys: `cmp (%rcx),%r10b` then `movzbl 0x1(%rcx)` to
disambiguate equal class bytes. On a hit, the `handler` is **invoked** as a pointer-to-member
function (`ctx = caller_ctx + record[+0x10]`, then a tail `jmp *%rax`), with the standard Itanium
encoding: low bit set ⇒ virtual dispatch, even ⇒ a direct function pointer. Some direct handlers
are real per-`(opcode, modifier)` validators; others are always-legal stubs — the two-byte-prefix
`mov $1,%eax; ret` validators at `0x118C000` / `0x118E200`.

### What changed from the earlier description

`+0x10` is **not** a flags/`reserved` field and **not** an SM-gating mask, and there is **no**
`0x08000000` "run generic validation" sentinel embedded in the record. Those earlier interpretations
(and the single flat `{key, handler, reserved}` stride) were wrong. Architecture gating is decided
**inside** each per-handler validator, not by a field in the record.

| Field | Earlier (incorrect) reading | Binary-grounded reading |
|---|---|---|
| table shape | one flat `u32` array, uniform stride | two-level: 16 B class descriptors → 24 B records |
| `handler` slot | sometimes a `key`, sometimes a pointer, sometimes `0x08000000` | always a member-fn-ptr that is *called* |
| `+0x10` | flags / SM-gating mask | member-fn-ptr `this`-adjust (0 observed) |
| arch gating | a sentinel value in the record | logic inside the called validator |

## The three-layer gate

1. **Per-SM encoder dispatch — the hard gate.** Generation tables keyed by `(fmt<<8)|minor` →
   handler. An instruction is encodable on a target **iff** its key resolves to a handler in that
   generation's table. The five *classic* tables (sm50–7x / sm75 / sm80–8x / sm86–89 / sm100+) share
   one handler pool; their per-gen opcode counts grow monotonically (newer arches add opcodes) and
   **492 opcodes are common to all five**. Two further Blackwell-generation blocks — sm_110 (Jetson
   Thor) and sm_120/sm_121 (consumer Blackwell) — sit beyond them with entirely disjoint emitter
   code, so the full dispatch region holds **seven** arch blocks (see
   [encoding-tables → Dispatch Reconciliation](../codegen/encoding-tables.md#dispatch-reconciliation--four-encoder-layers-plus-an-isel-vtable)).
2. **Per-class legality dispatch** (the two-level table above) — the STANDARD validation layer that
   calls a per-`(class, index)` validator before per-SM encoding.
3. **PTX-ISA-version gate** — a second axis independent of the target SM. Diagnostics gate
   instructions/modifiers by both `.target sm_NN` *and* PTX ISA version (e.g. *"Instruction '%s'
   without '.sync' is not supported on .target sm_70 and higher from PTX ISA version 6.4"*).

## STANDARD vs EXTENDED

The instruction registry tags each of its 1,410 forms STANDARD or EXTENDED — **1,141 STANDARD +
269 EXTENDED**. EXTENDED forms are the extended-ISA / fused / approximate variants (`mad.fused.hi`,
`tanh`, `ex2`, …) that are arch- or option-gated above the standard set. (See
[Operand-Type Signatures & Attributes](operand-types-attributes.md).)

## Gating diagnostics

The messages emitted on a failed `(SM, ISA, instruction, modifier)` tuple include:
*"Instruction '%s' not supported on .target '%s'"*, *"Feature '%s' not supported on .target '%s'"*,
*"Unsupported instruction '%s' used when compiling for '%s' target architecture"*, and
*"Modifier '%s' on instruction '%s' … not supported starting %s and later architectures"*. (Full
set in [Diagnostics & Messages](diagnostics.md).)

The legality records and the gating-diagnostic strings are in the repo at `decoded/ptxas-targets/`
(`instruction_legality.tsv`, `gating_diagnostics.tsv`).

## Cross-References

- [SM Version Codes](../targets/version-codes.md) — the target-identity tables this gate keys on.
- [SASS Instruction Encoding](../codegen/encoding.md) — the encoder the per-SM dispatch feeds.
- [Operand-Type Signatures & Attributes](operand-types-attributes.md) — the STANDARD/EXTENDED registry.
