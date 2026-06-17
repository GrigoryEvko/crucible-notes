# DVE Engine — Microcode-Table Architecture

> *All symbols, addresses, and data on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; see [versions](../reference/versions.md)). The microcode tables ship as JSON + headerless `.bin` blobs under `neuronxcc/dve/dve_bin_gen{2,3,4}/`; the opcode→name encoder (`enum_variant_string_opcode`) lives in `libwalrus.so` (build-id `92b4d331`, `.text` VA == file offset); the BIR `EngineType` enum lives in `libBIR.so` (`a9b1ea38`). Other wheels differ; treat every address as version-pinned.*

## Abstract

The **DVE** ("Deep Vector Engine", a.k.a. the **Vector** engine; `KaenaDVE` in its version blob) is Trainium's data-movement / vector leg — copy, cast, transpose, stream-shuffle, gather, tensor-scalar / tensor-tensor / tensor-reduce, pool, batch-norm statistics, RNG, memset, dropout, and accumulator read-back. Unlike the [PE engine](pe-engine.md), whose datapath is hand-encoded in C++ visitors, the DVE is **microcode-programmed**: its behavior is not compiled into the encoder but shipped as a set of **indexed lookup tables** that the runtime loads into the engine. The compiler's job is to pick *which* table-set the executable carries; the engine's job is to take an opcode integer, index its tables, and run the microcode rows it finds.

This is the architectural reason the DVE has no per-op visitor explosion: adding an opcode means adding a row to a table, not a function to the backend. The cost is a layer of indirection a reimplementer must reproduce exactly. Every DVE op is an integer in `[48..240]`; that integer is the **direct index** into a 256-entry `opcode_table`; the entry it lands on is a *packed descriptor* (a control-row index plus an optional slow-program index); those indices select rows in the `control` table(s); and each control row owns an 8-lane block in the `datapath_table`. The compiler ships three kinds of table-set — **default**, **transformer** (with gen2 fwd/bwd/bwd2 splits), and **saturate** (gen4 only) — each a complete, independently-compiled microcode image. The opcode subset enabled in a set is encoded as which `opcode_table` slots are nonzero.

The roster **grows monotonically in capability across generations**: gen2 carries 46 default opcodes, gen3 carries 52, gen4 carries 59 — a 46→52→59 progression driven by new datatype/mode-specialized ops, sparsity, and MX-quantize. The table *schema* also changes once: gen2 has a single `control_table` (3 keys); gen3 and gen4 split it into `control_fast_table` + `control_slow_table` (4 keys), promoting a hidden gen2 "slow program index" field into a first-class table. This page is the **engine-architecture view** — the table roster, the schema, the opcode growth, the opcode→name Rosetta, and the indexed-lookup execution flow. The **byte-exact on-device decode** of each blob (entry strides, field maps, datapath word layout) is [ISA 2.27: DVE datapath table](../isa/dve-datapath-table.md) and its siblings; this page cross-references those layouts rather than duplicating them.

For reimplementation, the contract is:

- **The table-set roster and lifecycle** — three kinds (`default` / `transformer[_fwd/_bwd/_bwd2]` / `saturate`), what each carries, and that membership is encoded as nonzero-vs-zero `opcode_table` slots, not as a separate op list.
- **The per-generation table schema** — gen2 = `{opcode, control, datapath}` (3 keys); gen3/gen4 = `{opcode, control_fast, control_slow, datapath}` (4 keys).
- **The 46→52→59 opcode growth** — the exact per-generation ADD/REMOVE deltas, and the lifecycle classes (stable core, gen-3 additions, gen-4 additions, gen-2-only drops).
- **The opcode→name Rosetta** — `neuronxcc::core_vN::enum_variant_string_opcode(int)`, a literal `case <opcode>: "<Name>"` switch, one per generation, that names every integer in the roster.
- **The indexed-lookup flow** — `opcode → opcode_table[op] → (control rows) → datapath block`, the chain the engine walks to turn an opcode into microcode.

| | |
|---|---|
| **Engine** | `EngineType::DVE` = ordinal **5**; `EngineType2string → "DVE"`, `EngineType2ExternalName → "Vector"` (`libBIR`) |
| **Internal name** | `KaenaDVE` (per `version.json`; `brazil_version` and `git_sha` both **empty** — pin to the wheel build) |
| **Table-set kinds** | `default`; `transformer` (+ gen2 `transformer_fwd`/`_bwd`/`_bwd2`); `saturate` (gen4 only) |
| **Per-gen sets** | gen2 = 5 sets; gen3 = 5 sets; gen4 = 3 sets (`default`, `saturate`, `transformer`) |
| **Schema (gen2)** | `dve_table_keys = ["opcode_table", "control_table", "datapath_table"]` (3 keys) |
| **Schema (gen3/gen4)** | `dve_table_keys = ["opcode_table", "control_fast_table", "control_slow_table", "datapath_table"]` (4 keys) |
| **Default opcode counts** | gen2 = **46**, gen3 = **52**, gen4 = **59** (`jq '.ops\|length'`, re-derived) |
| **Opcode index space** | `opcode_table` is a 256-entry LUT indexed by the raw opcode integer; roster `[48..240]` is a subset |
| **Opcode→name** | `neuronxcc::core_v2::enum_variant_string_opcode` (thunk `0x5edcb0` → off `0x3DCC640`); `core_v3`/`core_v4` siblings — one literal-string switch per gen |
| **Cross-build** | all three `dve_info.json` are byte-identical across cp310/cp311/cp312 (`cmp -s` OK) |

---

## The Table-Set Roster

### Purpose

A **table-set** is one complete microcode image for the DVE: a curated subset of opcodes plus the three (gen2) or four (gen3/gen4) `.bin` lookup tables that implement them. The compiler does not encode DVE behavior in code — it *selects* a table-set and ships that set's blobs into the executable, where the runtime loads them into the engine. A reimplementer's first decision is therefore "which set", because the set is the unit of microcode the silicon runs.

Each `dve_info.json` describes the sets for one generation. The top-level `dve_table_keys` array names the tables each set carries; each entry in the `tables` array is one set, with a `name`, one `.bin` filename per `dve_table_keys` entry (named `<setname>_<key>.bin`), and an `ops` array — the integer opcodes **enabled** in that set, sorted ascending.

### The three kinds

```text
default      — the general-purpose DVE microcode. The widest opcode roster of any
               set in its generation; the gen3/gen4 default is the full superset of
               all that gen's sets. This is the set a normal compile ships.

transformer  — a roster tuned for transformer/attention kernels. In gen2 it is split
               into four variants:
                 transformer        (general transformer)
                 transformer_fwd     (forward-pass specialized)
                 transformer_bwd     (backward-pass specialized)
                 transformer_bwd2    (a second backward variant)
               gen3 keeps all four splits; gen4 COLLAPSES them to a single
               "transformer" set.

saturate     — gen4 ONLY. Same opcode roster as gen4 default (byte-identical ops[]),
               but a DIFFERENT microcode payload: a saturating-arithmetic datapath
               configuration over the same opcodes. The set name selects MICROCODE,
               not just opcode membership.
```

| Gen | Sets present | Count | Confidence |
|---|---|---|---|
| gen2 | `default`, `transformer`, `transformer_fwd`, `transformer_bwd`, `transformer_bwd2` | 5 | CONFIRMED (`jq`) |
| gen3 | `default`, `transformer`, `transformer_fwd`, `transformer_bwd`, `transformer_bwd2` | 5 | CONFIRMED (`jq`) |
| gen4 | `default`, `saturate`, `transformer` | 3 | CONFIRMED (`jq`) |

> **QUIRK — `saturate` is not an alias of `default`.** Its `ops[]` is byte-identical to gen4 default (`jq '.tables[0].ops == .tables[1].ops'` → `true`), so it enables the exact same 59 opcodes. But its four `.bin` blobs all differ from default: `cmp` puts the first datapath divergence at byte 9161. The two sets wire the same sequencer and the same opcode→row map, yet hand the datapath lanes different field values — `saturate` is a pure datapath-payload variant that makes the arithmetic ops clamp instead of wrap. A reimplementer who treats `saturate` as a synonym for `default` ships wrong silicon behavior on overflow.

### How membership is encoded

A set's `ops[]` is a convenience list; the *authoritative* membership lives in the set's `opcode_table.bin`. The opcode table is a 256-entry LUT indexed by the raw opcode integer (see [The Indexed-Lookup Flow](#the-indexed-lookup-flow)); an opcode is **enabled in a set iff its slot is nonzero**. Disabling an op in a derived set is therefore "zero the slot", and enabling one is "write the descriptor". Comparing gen2 `default` vs `transformer` opcode tables, 34 of 256 entries differ: ops dropped from `transformer` (e.g. 88, 108–111) go to `0x0`; ops added (138, 139, 143, 154, 155) become nonzero; ops in both carry **different** control-row values, because each set compacts its own control/datapath tables independently (CONFIRMED — opcode-table diff, ISA 2.25).

### KaenaDVE version stamp

Every generation ships an identical 80-byte `version.json`:

```json
{ "KaenaDVE": { "brazil_version": "", "git_sha": "" } }
```

Both fields are empty in this release — the DVE component carries no semantic version of its own, so the only reliable handle on the table generation is the wheel build (`2.24.5133.0+58f8de22`). (CONFIRMED — `cat` over all three.)

> **NOTE — the roster is a property of the toolchain version, not the Python ABI.** The three `dve_info.json` are byte-identical across the cp310, cp311, and cp312 wheels (`cmp -s`, all OK). A reimplementer can treat the DVE table roster as fixed per toolchain release and ignore the interpreter ABI entirely.

---

## The Per-Generation Table Schema

### Purpose

`dve_table_keys` names the microcode tables each set in a generation carries, and **it is per-generation different** — the one schema change in the DVE's history. A reimplementer who hard-codes "three tables" will mis-parse gen3/gen4 (which have four), and one who hard-codes "four" will over-read gen2.

### The schema, per gen

| Gen | `dve_table_keys` | Keys | Confidence |
|---|---|---|---|
| gen2 | `["opcode_table", "control_table", "datapath_table"]` | 3 | CONFIRMED (`jq`) |
| gen3 | `["opcode_table", "control_fast_table", "control_slow_table", "datapath_table"]` | 4 | CONFIRMED (`jq`) |
| gen4 | `["opcode_table", "control_fast_table", "control_slow_table", "datapath_table"]` | 4 | CONFIRMED (`jq`) |

The change is a **single control table (gen2) splitting into fast + slow (gen3, unchanged at gen4)**. The `opcode_table` and `datapath_table` keys are constant across all three generations.

> **CORRECTION (DVE-SCHEMA) —** an earlier overview claimed gen3 had a *null* control table (opcode + datapath only) and that the fast/slow split first appeared at gen4. Direct `jq` of `dve_table_keys` refutes this: gen3 already carries both `control_fast_table` and `control_slow_table`, and the gen3 directory ships `default_control_fast_table.bin` (4096 B) + `default_control_slow_table.bin` (4096 B) — not a null. The schema lineage is gen2 (3-key, single control) → gen3 (4-key, split control) → gen4 (4-key, identical to gen3). There is no null-control generation.

### Why the control table split

The split is a **latency-class partition** of the microcode. In gen2, an opcode-table entry's high bits already carried a hidden "slow program index" (a 7-bit field above the 7-bit control-row field), and iterative ops parked on idle control rows while the real work ran from that slow index. Gen3 promoted that hidden field into its own first-class 256-row `control_slow_table` (the index field scaling ×2 to address the doubled table — see the byte-level proof in [ISA 2.26: control table](../isa/dve-control-table.md)):

```text
control_fast — the single-issue / low-latency datapath microcode reached by every
               op's base control row. The op's datapath block hangs off the fast row.

control_slow — the multi-cycle / iterative microcode (reductions, batch-norm passes,
               gather, cumulative) reached only by ops whose opcode_table entry carries
               a nonzero slow-row index.
```

So `control_fast` = one datapath block fired inline; `control_slow` = a loopable microcode program in its own table. The set of ops that carry a nonzero slow index is exactly the known iterative DVE families (tensor-reduce, batch-norm, gather, cumulative). (STRONG — the slow-op set matches the iterative families; the gen2-hidden-field ↔ gen3-slow-table correspondence is byte-exact in ISA 2.26.)

### Blob geometry per generation

The `.bin` blobs are **headerless raw LUTs** — no magic, opaque content; the deep decode is the ISA microcode-page job. Their sizes per generation (default set):

| Table | gen2 | gen3 | gen4 | Confidence |
|---|---|---|---|---|
| `opcode_table` | 2048 B | 1024 B | 1024 B | CONFIRMED (`ls`) |
| `control_table` | 2048 B (single) | — | — | CONFIRMED |
| `control_fast_table` | — | 4096 B | 4096 B | CONFIRMED |
| `control_slow_table` | — | 4096 B | 4096 B | CONFIRMED |
| `datapath_table` | 8192 B | 16384 B | 16384 B | CONFIRMED |

The `opcode_table` **halves** at gen3 (2048→1024) and the `datapath_table` **doubles** (8192→16384). Neither is a row-count change: the opcode table holds 256 entries in both, but gen2 packs each as a `u64` (8 B, five high bytes wasted) and gen3 re-packs as a `u32` (4 B); the datapath doubles because the control split takes the row count from 128 (gen2) to 256 (gen3) and the datapath keeps 64 B per row. gen4 keeps gen3's geometry but lights ~55% more datapath blocks and widens the datapath word (~33-bit at gen3 to ~61-bit at gen4), which is why gen4's same-size datapath is ~2.3× denser. (CONFIRMED sizes; the width/density mechanism is ISA 2.27.)

---

## The Opcode Roster and Its Growth

### Purpose

The DVE opcode roster is the set of integers a generation's `default` table enables. It grows 46→52→59 across generations, and the growth is not a clean append — gen2→gen3 both adds and removes ops. A reimplementer needs the exact deltas to know which opcodes a given silicon generation will accept and which it will reject.

### The counts and deltas

| Transition | Added | Removed | Net | Result | Confidence |
|---|---|---|---|---|---|
| gen2 → gen3 | `{96, 147, 152, 154, 155, 188, 230, 233, 240}` (9) | `{140, 141, 159}` (3) | +6 | 46 → 52 | CONFIRMED (`jq $b-$a`) |
| gen3 → gen4 | `{48, 119, 120, 224, 225, 226, 227}` (7) | `{}` (0) | +7 | 52 → 59 | CONFIRMED (clean superset) |

The arithmetic checks: `46 + 9 − 3 = 52`; `52 + 7 + 0 = 59`. gen4 default is a **clean superset** of gen3 default (no removals); gen2→gen3 is **not** a pure append — three legacy ops are dropped.

> **GOTCHA — gen2→gen3 is not a monotone +9.** It is +9 ADD and −3 REMOVE for a net +6. The three removed ops `{140, 141, 159}` (`TensorReduceAddBf16`, `TensorReduceMaxBf16`, `EngineNop`) silently leave the default roster; 140 and 141 leave the ISA entirely (the bf16-fused tensor-reduce ops retired at gen3), while 159 stays in the gen3 ISA but drops out of the default *roster*. A reimplementer who models the growth as "add the 9 gen3 ops to gen2's 46" gets 55, not 52, and keeps three dead opcodes alive.

### Opcode lifecycle classes

The 65-opcode universe (the union of every set in every generation) partitions into stable, introduced, and dropped classes relative to the `default` roster:

| Class | Opcodes | Count | Confidence |
|---|---|---|---|
| **Stable core** — in all three default sets | 65–73, 77, 78, 81–84, 88, 94, 97–102, 105–111, 114, 127, 130–132, 142, 148, 153, 157, 158, 229, 232, 234 | 43 | CONFIRMED |
| **Introduced at gen3**, kept through gen4 | 96, 147, 152, 154, 155, 188, 230, 233, 240 | 9 | CONFIRMED |
| **Introduced at gen4** (genuinely new to the roster) | 48, 119, 120, 224, 225, 226, 227 | 7 | CONFIRMED |
| **Dropped after gen2** (gen2 default only) | 140, 141, 159 | 3 | CONFIRMED |
| **Transformer-family only** (never in any default) | 138, 139, 143 | 3 | CONFIRMED |

The 43-opcode stable core is the generation-independent DVE kernel; the entire 46→52→59 growth layers on top of it (`43 + gen-specific additions = each gen's default`). Three opcodes (138 `TensorTensorAddBf16`, 139 `TensorTensorMultBf16`, 143 `TensorTensorSubBf16`) are gen2-transformer-exclusive — present only in gen2 transformer sets, gone by gen3. Note opcode **48** (`Exponential`) is the first op *below* the gen2/gen3 opcode floor of 65, and `{224, 225, 226, 227}` are a new high contiguous cluster — both gen4 additions (CONFIRMED — `jq` set diff).

### Default-set presence grid

A compact view of which generation's `default` set each opcode is in. Read `234` = "in gen2, gen3, and gen4 default"; `..4` = "gen4 default only"; `2..` = "gen2 default only"; `.34` = "gen3 and gen4"; `xform` = "transformer-set only, never default":

```text
 48:..4   65:234   66:234   67:234   68:234   69:234   70:234   71:234
 72:234   73:234   77:234   78:234   81:234   82:234   83:234   84:234
 88:234   94:234   96:.34   97:234   98:234   99:234  100:234  101:234
102:234  105:234  106:234  107:234  108:234  109:234  110:234  111:234
114:234  119:..4  120:..4  127:234  130:234  131:234  132:234  138:xform
139:xform 140:2..  141:2..  142:234  143:xform 147:.34  148:234  152:.34
153:234  154:.34  155:.34  157:234  158:234  159:2..  188:.34  224:..4
225:..4  226:..4  227:..4  229:234  230:.34  232:234  233:.34  234:234
240:.34
```

> **NOTE — `transformer` is a different shape per generation.** In gen2, `transformer` is **not** a subset of `default`: it drops `{88, 108, 109, 110, 111}` but *adds* `{138, 139, 143, 154, 155, 230}` — six ops the gen2 default lacks. So gen2 default is not the gen2 superset (the gen2 union over all five sets is 52 opcodes, six more than default's 46). By gen3, `transformer` becomes a strict subset of default (drops only); by gen4, `transformer` is the most aggressive prune, dropping all the new gen4 low/high op classes `{48, 119, 120, 224–227}` plus 82, 132, 188. A reimplementer cannot assume `transformer ⊆ default` — that holds only from gen3 on.

---

## The Opcode→Name Rosetta

### Purpose

The roster `ops[]` are bare integers; the canonical *name* of each opcode lives in the libwalrus CoreVN encoder, not in any JSON. There is no pure-Python opcode map in the wheel — the `isa/*_info` Cython modules expose only a computed `arch_isa_opcode` property, and the generated `get_isa_opcode` base raises `NotImplementedError`. The integers are the **silicon / wire ISA opcode** the libwalrus backend writes into each `NEURON_ISA_TPB_*_STRUCT`, and their names come from a debug stringifier the encoder ships.

### The stringifier

Each generation's encoder carries `neuronxcc::core_vN::enum_variant_string_opcode(int, char*, int)` — a literal `switch` of `case <opcode>: strncpy(buf, "<Name>", …)`. The integer is the wire opcode; the string is its canonical name. The generation binding is `core_v2 ↔ gen2`, `core_v3 ↔ gen3`, `core_v4 ↔ gen4` (the CoreVN ABI level *is* the DVE generation).

```text
neuronxcc::core_v2::enum_variant_string_opcode   — gen2 name table
neuronxcc::core_v3::enum_variant_string_opcode   — gen3 name table  (thunk @ 0x5edcb0
                                                    → jmp [off_3DCC640])
neuronxcc::core_v4::enum_variant_string_opcode   — gen4 name table
```

```c
// neuronxcc::core_vN::enum_variant_string_opcode(int op, char *buf, int buflen)
// a per-generation literal-string switch; one arm per ISA opcode the gen defines.
char *enum_variant_string_opcode(int op, char *buf, int buflen):
    switch (op):
        case 65:  return copy(buf, "TensorTensorArithOp");   // CONFIRMED, all 3 gens
        case 70:  return copy(buf, "Copy");
        case 188: return copy(buf, "RangeSelect");           // NEW @ core_v3 (absent in v2)
        case 227: return copy(buf, "QuantizeMX");            // NEW @ core_v4 (✔ matches the
                                                             //   sim's independent 0xE3 pin)
        ...                                                  // one case per roster opcode
        default:  return copy(buf, "Unknown");
```

> **NOTE — the Rosetta bodies are recovered as a literal switch; this page re-confirmed the symbol and its generation binding, and treats the individual case strings as STRONG (from the recovered switch) rather than re-disassembling all ~150 arms here.** The `core_v3` thunk is byte-present (`0x5edcb0: ff 25 8a e9 7d 03  jmp cs:off_3DCC640`), and the per-op names corroborate independently against the `libwalrus` `klr::*_des` stringifier roster (`Copy_des`, `Max8_des`, `FindIndex8_des`, `MatchReplace8_des`, `SelectReduce_des`, `TensorTensor_des`, `ScalarTensorTensor_des`, `Dropout_des` are all real recovered symbols) and against the simulator's independent pin of opcode `0xE3 = 227 = QuantizeMX`. Two independent paths agree on the new gen4 MX opcode.

### Named roster (the integers that grow the default set)

The opcodes that enter or leave the default roster across generations, named:

| Int | Hex | Name | Δ | Confidence |
|---|---|---|---|---|
| 48 | 0x30 | `Exponential` | ADD @ gen4 (roster-promoted; ISA-pre-existing) | CONFIRMED name / STRONG |
| 96 | 0x60 | `BatchNormStats` | ADD @ gen3 (base BN-stats; gen2 had only 97) | CONFIRMED name / STRONG |
| 119 | 0x77 | `RandGetState` | ADD @ gen4 (RNG-state read; roster-promoted) | CONFIRMED name / STRONG |
| 120 | 0x78 | `RandSetState` | ADD @ gen4 (RNG-state write; roster-promoted) | CONFIRMED name / STRONG |
| 140 | 0x8C | `TensorReduceAddBf16` | REMOVE @ gen3 (ISA-removed) | CONFIRMED name / STRONG |
| 141 | 0x8D | `TensorReduceMaxBf16` | REMOVE @ gen3 (ISA-removed) | CONFIRMED name / STRONG |
| 147 | 0x93 | `TransposeTensorScalarArithOp` | ADD @ gen3 (ISA-pre-existing) | CONFIRMED name / STRONG |
| 152 | 0x98 | `TensorScalarSelect` | ADD @ gen3 (ISA-pre-existing) | CONFIRMED name / STRONG |
| 154 | 0x9A | `TensorScalarCacheReduce` | gen2-xform → gen3 default | CONFIRMED name / STRONG |
| 155 | 0x9B | `DveReadAccumulator` | gen2-xform → gen3 default | CONFIRMED name / STRONG |
| 159 | 0x9F | `EngineNop` | REMOVE @ gen3 (roster only; ISA kept) | CONFIRMED name / STRONG |
| 188 | 0xBC | `RangeSelect` | ADD @ gen3 (**NEW** core_v3 ISA op) | CONFIRMED name / STRONG |
| 224 | 0xE0 | `SparsityCompress` | ADD @ gen4 (**NEW** core_v4 ISA op) | CONFIRMED name / STRONG |
| 225 | 0xE1 | `SparsityCompressTag` | ADD @ gen4 (**NEW** core_v4 ISA op) | CONFIRMED name / STRONG |
| 226 | 0xE2 | `Rand2` | ADD @ gen4 (**NEW** core_v4 ISA op) | CONFIRMED name / STRONG |
| 227 | 0xE3 | `QuantizeMX` | ADD @ gen4 (**NEW** core_v4 ISA op; ✔ sim-pinned) | CONFIRMED |
| 230 | 0xE6 | `TensorScalarCacheCumulative` | gen2-xform → gen3 default | CONFIRMED name / STRONG |
| 233 | 0xE9 | `DveReadIndices` | ADD @ gen3 (**NEW** core_v3 ISA op) | CONFIRMED name / STRONG |
| 240 | 0xF0 | `ExtendedInst` | ADD @ gen3 (extended-instruction escape; ISA-pre-existing) | CONFIRMED name / STRONG |

> **QUIRK — the gen4 growth is two-pronged.** Of the seven gen4 additions, `{48, 119, 120}` (`Exponential`, `RandGetState`, `RandSetState`) are **roster promotions** of opcodes the ISA already defined in v2/v3 — the silicon could always decode them; gen4 merely added them to the DVE default repertoire. The other four `{224, 225, 226, 227}` (`SparsityCompress`, `SparsityCompressTag`, `Rand2`, `QuantizeMX`) are **genuinely new core_v4 ISA opcodes**, absent from the core_v3 switch — the sparsity + MX-quantize hardware extension. A reimplementer must not assume "new in the default roster" means "new silicon"; only the latter four are new ISA.

> **GOTCHA — the CoreVN opcode space is NOT the BIR `InstructionType` enum.** The DVE/CoreVN opcode (this page, range `1..255`, DVE subset `[48..240]`) is the L3 wire opcode the libwalrus encoder emits. The BIR `InstructionType` (range `0..109`) is libBIR's L1 in-memory instruction-kind tag for dispatch. They are **separate spaces with coincidental numeric overlap**: CoreVN opcode 96 = `BatchNormStats`, but BIR `InstructionType` 96 = `QuantizeMx`; CoreVN 227 = `QuantizeMX`. A reimplementer who indexes one table with the other's integer gets a silently wrong op. This page owns only the CoreVN/DVE wire space.

---

## The Indexed-Lookup Flow

### Purpose

The table-sets are only useful if the engine can turn an opcode into microcode. The lookup is a fixed chain of indexed dereferences — the architectural heart of the table-driven design — and a reimplementer must reproduce it exactly. The chain is the same shape every generation; only the entry width and the number of control tables change.

### The chain

```text
  opcode (int, [48..240])
     │  direct index into the 256-entry opcode_table  (entry == 0 ⇒ op DISABLED in this set)
     ▼
  opcode_table[op]  =  packed descriptor
     │  gen2:   bits[0:7]  = control row,   bits[7:]  = slow-program index
     │  gen3/4: bits[0:8]  = control_fast row, bits[8:] = control_slow row (0 ⇒ fast-only)
     ▼
  control_fast[fast_row]                     ── the per-step sequencer / control microcode word
  (+ control_slow[slow_row]  if slow_row ≠ 0) ── the iterative / multi-cycle microcode program
     │  the FAST control row also keys the datapath block
     ▼
  datapath_table[fast_row * 8 .. fast_row * 8 + 7]  ── the 8 datapath-lane config words for this step
```

The opcode table is **directly indexed by the opcode integer** — there is no hash, no search. Entry 0 doubles as the engine's idle/reset descriptor (its value resolves to control row 0, the NOP row). An entry of zero at any other index means the opcode is not enabled in this set; that is exactly how a derived set (transformer, saturate) disables an op.

### Algorithm

```c
// Runtime microcode lookup (the engine's decode; the loader lives in firmware,
// not in this wheel — reconstructed from the table geometry, not a decompiled loader).
function dve_lookup(int op, TableSet *set):
    desc = set.opcode_table[op]                  // 256-entry LUT; stride 8B gen2 / 4B gen3,4
    if desc == 0:
        raise IllegalOpcode(op)                  // op not enabled in this set

    if set.schema == GEN2:                        // single control table
        ctrl_row = desc & 0x7F                    // bits[0:7]
        slow_idx = desc >> 7                       // bits[7:]  (proto-slow program selector)
        seq      = set.control_table[ctrl_row]
        block    = &set.datapath_table[ctrl_row * 8]   // 8 × u64 = the lane config
        return (seq, slow_idx, block)
    else:                                          // GEN3 / GEN4: split control
        fast_row = desc & 0xFF                     // bits[0:8]
        slow_row = desc >> 8                        // bits[8:]  (0 ⇒ fast-path only)
        seq_fast = set.control_fast_table[fast_row]
        seq_slow = (slow_row != 0) ? set.control_slow_table[slow_row] : NULL
        block    = &set.datapath_table[fast_row * 8]   // datapath keyed by the FAST row
        return (seq_fast, seq_slow, block)
```

The byte-level strides and field maps that make this concrete (8 B / 4 B opcode entries; 16 B control rows with a 12-byte word; 64 B = 8×u64 datapath blocks) are byte-verified in [ISA 2.25–2.27](../isa/dve-opcode-table.md); the chain above is the architectural contract those layouts implement.

### Worked example — opcode 65 across generations

The same logical op (`TensorTensorArithOp`) acquires a slow program as the generations evolve, visible directly in the opcode-table bytes:

| Gen | `opcode_table[65]` (LE) | Decode | Confidence |
|---|---|---|---|
| gen2 | `03 00 00 00 00 00 00 00` | row 3, slow_idx 0 — fast-only | CONFIRMED (byte) |
| gen3 | `…` | fast_row 16, slow_row 48 | CONFIRMED (ISA 2.25) |
| gen4 | `10 30 00 00` | `0x3010` → fast_row 16, slow_row 48 | CONFIRMED (byte) |

Opcode 65 had **no** slow program in gen2 (fast row 3 only), then acquired iterative microcode at gen3 (slow row 48) and kept it at gen4 — the engine's response to the same opcode grows richer without the opcode integer changing. A fast-only op like gen4 `opcode_table[48]` = `0x08` (fast_row 8, slow 0) lights its datapath block inline; a pure slow op like gen2 `opcode_table[102]` = `0x09cf` (control row 79, slow index 19) parks on an idle base row and runs entirely from its slow program. (CONFIRMED — `xxd` byte-read.)

> **QUIRK — the opcode table is a superset of the JSON `ops[]`.** Parsing gen2's `default_opcode_table.bin` yields nonzero entries at 50 indices, four more than the JSON's 46: `{147, 240, 241, 242}` are always-resident helper descriptors the table pre-stages (147 and 240 become default ops at gen3 — the table stages them a generation early). A reimplementer who derives the live opcode set purely from nonzero table slots will see four ops the `ops[]` list does not advertise; the `ops[]` array is the authoritative *enabled* set, the table is a slightly wider staging area. (CONFIRMED — ISA 2.25 nonzero-index enumeration.)

---

## Per-Generation Summary

| Facet | gen2 | gen3 | gen4 |
|---|---|---|---|
| Device class (INFERRED) | Trn1 / Inf2 | Trn2 | Trn3 |
| CoreVN ABI | `core_v2` | `core_v3` | `core_v4` |
| Table-sets | 5 (`default` + 4 transformer) | 5 (`default` + 4 transformer) | 3 (`default`, `saturate`, `transformer`) |
| `dve_table_keys` | 3 (single control) | 4 (fast/slow split) | 4 (fast/slow split) |
| Default opcode count | 46 | 52 | 59 |
| Opcode floor / ceiling | 65 / 234 | 65 / 240 | 48 / 240 |
| `opcode_table` entry | `u64` (8 B) | `u32` (4 B) | `u32` (4 B) |
| `datapath_table` size | 8192 B | 16384 B | 16384 B |
| New-this-gen ISA ops | (baseline) | 188 `RangeSelect`, 233 `DveReadIndices` | 224–227 (Sparsity / Rand2 / QuantizeMX) |
| Has `saturate` set | no | no | **yes** |

> **NOTE — the gen↔device mapping is INFERRED, not stamped in the DVE JSON.** The DVE blobs carry no device label; the `gen2≈Trn1/Inf2`, `gen3≈Trn2`, `gen4≈Trn3` mapping is derived from the codename taxonomy and the `core_vN` ABI ordering, which the new-ISA-op boundaries corroborate exactly (224–227 appear precisely at `core_v4`). The fit is exact, so confidence is high, but the label itself is derived. See [Codename Taxonomy](codename-taxonomy.md).

## Gaps and Confidence

- **Roster, counts, deltas, schema — CONFIRMED.** Every `ops[]`, the 46/52/59 counts, the gen2→gen3 +9/−3 and gen3→gen4 +7/−0 deltas, the 3-vs-4-key schema, the gen3 fast/slow split, `version.json` empties, all blob sizes, the `saturate.ops == default.ops` identity with differing blobs, and cross-build (cp310/11/12) identity are all byte-verified from the shipped JSON and `.bin` blobs in this turn.
- **Opcode→name binding — CONFIRMED symbol / STRONG per-name.** The `enum_variant_string_opcode` stringifier symbol and its `core_vN ↔ genN` binding are confirmed (thunk byte-present, names corroborated by the `klr::*_des` roster and the simulator's `0xE3 = 227` pin); the individual case strings are taken from the recovered switch rather than re-disassembling all ~150 arms here.
- **gen↔device mapping — INFERRED** (from taxonomy + ABI ordering, corroborated by ISA-op boundaries).
- **control_fast / control_slow semantics — STRONG** (the slow-op set matches the iterative families; the gen2-hidden-field ↔ gen3-slow-table correspondence is byte-exact in the ISA microcode page).
- **Byte-exact table interiors — out of scope here.** Entry strides, control-word field maps, and datapath-word bit layout live in [ISA 2.25–2.27](../isa/dve-datapath-table.md); this page cross-references rather than duplicates them.

## Cross-References

- [PE Engine](pe-engine.md) — sibling compute engine (the systolic matmul array); contrast its hand-coded visitor datapath with the DVE's table-driven design.
- [Pool Engine](pool-engine.md) — sibling reduce/pool leg; several DVE opcodes (`Max8` 108, `FindIndex8` 110, `MatchReplace8` 111) are emitted by the Pool-leg `InstMaxIndex`/`InstMax` encoders.
- [ISA 2.25: DVE opcode table](../isa/dve-opcode-table.md) — the byte-exact `opcode_table` entry decode (strides, packed-descriptor fields, nonzero-index enumeration).
- [ISA 2.26: DVE control table](../isa/dve-control-table.md) — the control-word field map and the byte-exact gen2-slow-index ↔ gen3-slow-row correspondence.
- [ISA 2.27: DVE datapath table](../isa/dve-datapath-table.md) — the 8-lane datapath block, the per-gen word-width growth, and the saturate-vs-default payload diff.
- [Arch Object Model](arch-object-model.md) — the `EngineType` enum (DVE = 5, external name "Vector") and the per-core engine-geometry slots the DVE binds to.
- [Codename Taxonomy](codename-taxonomy.md) — the gen↔device mapping (gen2/3/4 ↔ Trn1-Inf2/Trn2/Trn3) this page treats as INFERRED.
