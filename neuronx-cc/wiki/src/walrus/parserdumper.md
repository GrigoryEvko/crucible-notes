# BIR Dumper and `bir_roundtrip`

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 share the `.text` logic). The text emitter lives in `neuronxcc/starfish/lib/libBIRParserDumper.so` (DT_NEEDED `libBIR.so`); the round-trip harness is the static driver `…data/data/bin/bir_roundtrip` (DT_NEEDED `libBIR.so`, **not** `libBIRParserDumper.so`). For `.text`/`.rodata` the virtual address equals the file offset in both objects (re-verified: the string `"ulib_to_ucode_version"` xxd-matches at file offset `0x172767`). Other wheels differ; treat every address as version-pinned.*

## Abstract

The name "BIRParserDumper" promises two halves; only one is real. `libBIRParserDumper.so` contains no BIR-JSON *parser* — zero `createFromJson` references, and the only symbols matching "Parser" are `llvm::cl::parser<Enum>` command-line template instantiations. What it actually carries is **`bir::Dumper`**, a CRTP `IRVisitor<bir::Dumper, void>` that walks `Module → Function → BasicBlock → Instruction` and emits a flat, depth-indented, human-readable text dump in `<name>: <type>, key=value, …` style. This is a debug emitter, linked into exactly one consumer — `walrus_driver`'s `--emit-bir` path.

It is **not** the canonical serializer. The wire format is nlohmann brace-JSON produced by `libBIR`'s symmetric `adl_serializer<bir::Module>::{from_json, to_json}` pair — the two-pass loader documented in [The Two-Pass BIR-JSON Loader](../bir/json-loader.md) and the per-instruction `Instruction::toJson` writer in [The BIR-JSON Write Path](../bir/json-writer.md). The `bir::Dumper` is a *second, independent* serializer that re-implements per-field emission (it owns the field keys and the `, key=value` syntax) but **delegates enum→string** to `libBIR` — it imports exactly 25 distinct `bir::to_json(json&, <Enum>)` symbols (all UND, resolved at load).

The page covers three things a reimplementer must reproduce: the two distinct serializers and which binary uses which; the Dumper CRTP base and its per-op override shape; and the **version-triplet** — three `std::string` provenance fields (`ulib_to_ucode_version`, `ulib_to_isa_version`, `nki_binary_version_identifier`) emitted *per-instruction*, not in a module header.

For reimplementation, the contract is:

- **Two serializers, two purposes.** `bir::Dumper` = flat text debug dump (`libBIRParserDumper`, into `walrus_driver`). `adl_serializer<Module>` = canonical brace-JSON (`libBIR`, the round-trippable wire format). `bir_roundtrip` exercises the *second*.
- **The Dumper CRTP.** A non-polymorphic `IRVisitor<Dumper, void>` base; a manual 110-case switch on the opcode byte `Inst+0x58`; ~110 `Dumper::visitInst<X>` per-op override bodies; enum→string delegated to `libBIR`'s 25 imported `to_json(Enum)`.
- **The version-triplet.** Three runtime `std::string` fields carried on `InstCustomOp` / `InstNKIKLIRKernel`, emitted as `, key=value` pairs. Not constants — provenance set during NKI / custom-op lowering.

| | |
|---|---|
| **Text emitter** | `bir::Dumper` — CRTP `IRVisitor<bir::Dumper, void>`, `libBIRParserDumper.so` |
| **Top dump driver** | `bir::Dumper::visit(bir::Module&)` `@0xb4580` (848 B) |
| **Per-inst dispatch** | `IRVisitor<bir::Dumper, void>::visit(bir::Instruction&)` `@0xd14d0` (W, 2272 B) — 110-case |
| **Round-trip driver** | `bir_roundtrip` `main @0x427dc0` (889 B) — `cin → from_json → to_json → dump(2) → cout` |
| **Canonical (de)serializer** | `libBIR` `adl_serializer<bir::Module>::{from_json, to_json}` (UND in both clients) |
| **Enum→string delegation** | 25 distinct `bir::to_json(json&, <Enum>)` UND imports (resolved from `libBIR`) |
| **Version-triplet** | `ulib_to_ucode_version` `@0x172767`, `ulib_to_isa_version` `@0x172796`, `nki_binary_version_identifier` `@0x1729d4` |

---

## The Two Serializers

There are two independent BIR serializers in this build, and the task's two binaries exercise *different* ones.

**S1 — canonical brace-JSON (`libBIR`).** `adl_serializer<bir::Module>::from_json` / `::to_json` is the symmetric nlohmann (de)serializer: `from_json` is the two-pass `createFromJson` loader; `to_json` is the per-`Instruction` `toJson` emitter. The C++ object graph *is* the wire schema (JSON and CBOR are two encodings of one schema). This is what `bir_roundtrip` uses, and the only re-ingestible format. See [json-loader](../bir/json-loader.md) and [json-writer](../bir/json-writer.md).

**S2 — flat text debug dump (`bir::Dumper`, `libBIRParserDumper`).** A CRTP visitor that emits `<name>: <type>, key=value, …` — *not* braces. It owns its own per-field emission (a roster of `, key=`-prefixed literals in `.rodata`) but **delegates enum→string** to `libBIR`: 25 UND `bir::to_json(json&, <Enum>)` imports (`ActivationFunctionType`, `AluOpType`, `AxisListType`, `DGEType`, `DMAQoSClass`, `MatmultPerfMode`, `PoolFunctionType`, `RandomAlgorithmKind`, `TSCMode`, `TransposeOps`, `CollectiveKind`, … — confirmed: exactly 25 UND `to_json` enum symbols in the dynsym). Its sole linker consumer is `walrus_driver`.

> **CORRECTION (D-G10 / S2-10 §4b) —** an earlier pass recorded "Roundtrip driver: `bir_roundtrip` uses `libBIRParserDumper`." That is wrong. `bir_roundtrip`'s `DT_NEEDED` lists `libBIR.so` and **not** `libBIRParserDumper.so`; both `adl_serializer<bir::Module>::from_json` and `::to_json` are UND→`libBIR`; and the binary has **zero** `bir::Dumper` symbols (`.symtab` is stripped, dynsym has 1304 entries, none `bir::Dumper`). The Dumper's only consumer is `walrus_driver`. CERTAIN.

> **CORRECTION (D-E06) —** an earlier note claimed `libBIRParserDumper` "has only `bir::Dumper`/`Parser`, no per-op serializers." In fact it carries ~110 real `Dumper::visitInst<X>` bodies (e.g. `visitInstCustomOp @0xaffd0`, `visitInstNKIKLIRKernel @0xb21d0`), each emitting one op's fields. These are a *second* text-format emitter, distinct from the canonical brace-JSON `toJson` that stays in `libBIR`. CERTAIN.

There is no BIR-JSON *parser* in `libBIRParserDumper` at all. Parsing is entirely `libBIR`'s `adl_serializer<bir::Module>::from_json`; the only "Parser" classes physically present are the `llvm::cl::parser<Enum>` command-line instantiations (see [Command-Line Enum Parsers](#command-line-enum-parsers)).

---

## The Dumper CRTP and Dispatch

### Purpose

`bir::Dumper` is a curiously-recurring-template-pattern visitor: `IRVisitor<bir::Dumper, void>` is the base, `bir::Dumper` is the derived self-type. It is **non-polymorphic** — there is no `_ZTI`/`_ZTV` for `bir::Dumper`, so dispatch is a manual switch on the instruction opcode byte, not a vtable. This is the structural twin of the verifier ([legality-dispatch](legality-dispatch.md)) and simulator `IRVisitor`s, which use the same hand-rolled switch over `Inst+0x58`.

### Object Layout

```text
bir::Dumper  (non-polymorphic CRTP IRVisitor<bir::Dumper, void>)
  +0x00   visitor base state
  +0x08   std::ostream*   output stream     // dereferenced as *(this+1) everywhere
  +0x10   int             indent depth      // ++ in enterX, -- in leaveX
  (engine-name cache: DenseMap<bir::EngineInfo, std::string> referenced by enterModule)
```

The pretty-print model is hierarchical and newline-delimited. `enterBlock(name, kind) @0xbc2b0` (weak) emits indent + `"<name>"` (+ `"<kind>"` if nonempty) + an open token, writes `'\n'`, flushes, then `++*(this+0x10)`. `leaveBlock @0xbc430` decrements and emits the close token. `enterModule`/`leaveModule` bracket the depth the same way. Each `enterX` flushes a line, so the output is a depth-indented tree.

### Top Driver

`bir::Dumper::visit(bir::Module&) @0xb4580` (848 B, disasm-pinned — Hex-Rays failed on it) emits **main-first**, matching the loader's `"main"` privilege:

```c
function Dumper::visit(Module &M):              // 0xb4580
    enterModule(M)                              // 0xb459e

    main = M.getFunctionByName("main")          // 0xb45c6 — immediate 0x6E69616D = "main"
    if main:                                    // emit the main function FIRST
        enterFunction(main)                     // 0xb4600..
        for BB in main.blocks:
            enterBasicBlock(BB)
            for I in BB.insts: IRVisitor::visit(I)
            leaveBasicBlock(BB)
        leaveFunction(main)

    leaveModule(M)                              // 0xb4682

    for fn in M.functions (M+0x230) ∪ M.nki_functions:   // 0xb4687..0xb48c3
        if fn == main: continue                 // cmp [savedMainPtr], fn; jz — skip main
        enterFunction(fn); …per-BB nest…; leaveFunction(fn)
```

Emission order: `enterModule` header → main function → `leaveModule` → remaining + NKI functions. The main-first ordering is deliberate and mirrors the `createFromJson` `"main"` special-case in the loader.

> **QUIRK —** `leaveModule` is emitted *before* the non-main functions, not after the whole module. The Dumper closes the module bracket once main is dumped and then appends the rest; a reimplementer who closes the module only after every function will produce a structurally different dump.

### Per-Instruction Dispatch

`IRVisitor<bir::Dumper, void>::visit(bir::Instruction&) @0xd14d0` is a **weak** symbol (the CRTP template instantiation), 2272 B, switching on the opcode byte at `Inst+0x58`:

```c
function IRVisitor<Dumper,void>::visit(Instruction &I):     // 0xd14d0
    opcode = I.bytes[0x58]
    switch (opcode):
      case 0x69 (105, Loop):                                // nested-body ops:
      case 0x6A (106, DynamicForLoop):                      //   NO enterInstruction
      case 0x6C (108, DoWhile):
          enterInst<X>(I)
          for BB in I.body: enterBasicBlock(BB); … visit … ; leaveBasicBlock(BB)
          leaveInst<X>(I)
          return
      default:
          enterInstruction(I)                               // 0xd1697
          if I.bytes[0x58] > 0x6D (109): goto unknown       // ja default
          jpt = jpt_D16B3[I.bytes[0x58]]                    // 110-case jump table, 0..109
          Dumper::visitInst<X>(I)                           // per-op field emitter
          leaveInstruction(I)                               // 0xd16ed — common tail
          return
    unknown:                                                // 0xd16b5
        NeuronAssertion(code 0x9D=157,
            "Unknown Instruction type encountered!")        // string @0xd16c1
```

The three nested-body opcodes (`Loop`/`DynamicForLoop`/`DoWhile`) take a separate `enterInst<X>` path that recurses into the nested basic-block body and **skips** `enterInstruction`. All others go through `enterInstruction → jump table → visitInst<X> → leaveInstruction`. The default arm raises `NeuronAssertion` code 157.

### Per-Op Field Emission

Each of the ~110 `Dumper::visitInst<X>` bodies (`0x877f0..0xb21d0`) follows the same shape (witnessed on `visitInstCustomOp @0xaffd0`):

```c
function Dumper::visitInst<X>(Instruction &I):
    // (i) header
    emit name; getEngineString(I) @0x814c0  // "<engine_id>: EngineType2string(engine)"
                                            //   when engine_id != 0 (EngineType2string @libBIR)
    emit instruction_type

    // (ii) operands
    dumpArgumentsOutputs(I) @0x86810        // walk operand role-lists at I+0xC0 (Argument vector)
                                            //   → per arg: dumpArgument/dumpAP/dumpSymbolicAP

    // (iii) op-fields  — the , key=value template
    for field in op.fields:
        json j; j["<key>"] = value          // operator[]<char const*>
        s = j.at("<key>")                   // sub_851E0: json → stringstream
        emit ", <key>=" + s                 // comma-prefixed inline pair

    // (iv) tail
    dumpSyncInfo(I) @0x80f30                // sync_info
    emit close token
```

Enum-valued fields route through the 25 imported `bir::to_json(json&, <Enum>)` symbols; `MaybeAffine` fields use `bir::MaybeAffine<bool>::writeIntoJson` (the `QuasiAffineExpr` half is `libBIR`, pelican v1/v2-gated). The field-key roster is `.rodata` literals: `", dma_qos="` `@0x171400`, `", scale="` `@0x171420`, `", alpha="` `@0x17142f`, `", func="` `@0x17143d`, `", acc="` `@0x171449`, `", reduce_op="` `@0x1714af`, `", perf_mode="` `@0x1716a2`, `", compress_ratio="` `@0x171732`, … (131 distinct keys total).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `bir::Dumper::visit(Module&)` | `0xb4580` | 848 | top dump driver (main-first) | CERTAIN |
| `IRVisitor<Dumper,void>::visit(Instruction&)` | `0xd14d0` (W) | 2272 | 110-case opcode dispatch | CERTAIN |
| `bir::Dumper::enterModule(Module&)` | `0x85550` | 1529 | module open + arch + attrs | CERTAIN |
| `bir::Dumper::leaveModule(Module&)` | `0xb41e0` | 927 | module close | CERTAIN |
| `bir::Dumper::enterInstruction(Instruction&)` | `0x848d0` | 1368 | inst header (Hex-Rays failed; disasm) | CERTAIN |
| `bir::Dumper::leaveInstruction(Instruction&)` | `0x80d00` | 218 | inst tail/close | CERTAIN |
| `bir::Dumper::enterBlock(string,string)` | `0xbc2b0` (W) | 377 | indent block + `++depth` | CERTAIN |
| `bir::Dumper::getEngineString(EngineType)` | `0x814c0` | 1289 | `"id: EngineType2string(e)"` | CERTAIN |
| `bir::Dumper::dumpArgumentsOutputs(Instruction*)` | `0x86810` | 685 | operand-list emit | CERTAIN |
| `bir::Dumper::dumpSyncInfo(Instruction*)` | `0x80f30` | 560 | `sync_info` emit | CERTAIN |
| `bir::Dumper::visitInstCustomOp(InstCustomOp&)` | `0xaffd0` | — | custom-op fields + ulib versions | CERTAIN |
| `bir::Dumper::visitInstNKIKLIRKernel(…)` | `0xb21d0` | — | NKI fields + nki version id | CERTAIN |

---

## The Version-Triplet

Three provenance keys live in `libBIRParserDumper` `.rodata` and are emitted **per-instruction**, not in a module header. The keys are confirmed by xxd at their offsets; the separators (`", key="`) sit just past each key string.

| Key | Key `@` | Separator `, key=` `@` | Carried on | Field offset | Emitter |
|---|---|---|---|---|---|
| `ulib_to_ucode_version` | `0x172767` | `0x17277d` | `InstCustomOp` | inner `+0x130`/`+0x138` (ptr/len) | `visitInstCustomOp` |
| `ulib_to_isa_version` | `0x172796` | `0x1727aa` | `InstCustomOp` | inner `+0x150` | `visitInstCustomOp` |
| `nki_binary_version_identifier` | `0x1729d4` | `0x17a9c0` | `InstNKIKLIRKernel` | inner `+0x130` | `visitInstNKIKLIRKernel` |

`visitInstCustomOp @0xaffd0` loads the ucode-version `std::string` (ptr/len at `0xb036e`/`0xb0375`) and the isa-version `std::string` (`lea` at `0xb0471`), wraps each in a temporary json, and emits `, ulib_to_ucode_version=<value>` / `, ulib_to_isa_version=<value>` via the `, key=value` template (key/sep emission at disasm `0xb0394`/`0xb03c6`/`0xb03eb` for ucode, `0xb0485`/`0xb04af`/`0xb04d4` for isa).

`visitInstNKIKLIRKernel @0xb21d0` reads the `std::string` at inner `+0x130` and emits `, nki_binary_version_identifier=<value>` (key at disasm `0xb2524`, separator at `0xb2609`), gated by two `std::string::compare` guards at `0xb2592`/`0xb25cd` (a skip-if-default/empty test).

> **NOTE —** the values are **not constants**. They are runtime `std::string` fields carried on the instruction, set during NKI / custom-op lowering, recording the µlib→µcode, µlib→ISA, and NKI-binary versions for the runtime's ABI-compatibility checks. The Dumper emits whatever the field holds; no version number is baked into the dumper.

---

## `bir_roundtrip` — `main @0x427dc0`

A stdin→stdout JSON round-trip equivalence harness using `libBIR`'s `adl_serializer`. It does **not** touch `bir::Dumper`. Disasm-pinned call order:

```c
function main():                                // 0x427dc0 (889 B)
    json doc; sub_43B470(&doc)                  // 0x427ddf — zero-init document
    lexer.scan(std::cin); parser.parse(allow_exceptions, doc)   // 0x427f31 / 0x427f63
    sub = doc["module"]                         // aModule "module" @lea 0x427f98
    Module module("module")                     // 0x427fad — bir::Module::Module(string) [UND]
    adl_serializer<bir::Module>::from_json(sub, module)   // 0x427fd5 — libBIR two-pass loader [UND]
    json out
    adl_serializer<bir::Module>::to_json(out, module)     // 0x427ff7 — libBIR per-inst toJson [UND]
    text = out.dump(2, ' ', ensure_ascii, error_handler)  // 0x42801a — 2-space JSON (edx=2, ecx=0x20)
    std::cout << text; cout.put('\n'); cout.flush()        // 0x428030
    return 0
```

`from_json` and `to_json` are both **UND** in the dynsym and resolve to `libBIR` — confirmed. The harness is a **serialize-determinism / schema-fidelity** check: it re-emits the model (`json → Module → json → stdout`) so an *external* diff (the test fixture) compares input vs output JSON. Any field the loader drops, reorders non-canonically, or the emitter mis-spells shows as a diff. It exercises the full `createFromJson↔toJson` pair (both passes, all opcodes, pelican v1/v2 by the doc's `"version"`) on real BIR-JSON fed on stdin. It does **not** itself assert equality — there is no internal compare call; equivalence is judged by the driver comparing re-emitted stdout to the canonical expected output.

> **GOTCHA —** `bir_roundtrip` uses `libBIR`'s `to_json` + `nlohmann::dump(indent=2)`, **not** `bir::Dumper`. The two produce different output formats (brace-JSON vs flat `, key=value` text). A reimplementer who routes the round-trip through the text Dumper will never reproduce the wire-canonical JSON that the test diffs against.

---

## Command-Line Enum Parsers

The only "Parser" classes physically in `libBIRParserDumper` are `llvm::cl::parser<Enum>` `generic_parser` template instantiations — the command-line string↔enum machinery, **not** the on-wire BIR (de)serializers. They are present in `bir_roundtrip` too (e.g. `cl::parser<bir::EngineType>::parse @0x431aa0`, `cl::parser<bir::DMAQoSClass>::parse @0x4354c0`, both weak), but **dead there**: `main` never calls `cl::ParseCommandLineOptions` — it ignores `argv` and reads stdin. Their live use is `walrus_driver`'s flag surface.

The algorithm is the stock LLVM `generic_parser_base::parse`: linearly scan the parser's `Values` vector (`OptionEnumValue` entries `{name_ptr@+0, name_len@+8, desc, enum_val(int)@+40, Valid(byte)@+44}`), compare the input `StringRef` by memcmp, on match write `*out = entry[+40]` (assert `"Valid && \"invalid option value\""`), on miss emit `"Cannot find option named '<x>'!"`. The accepted-name roster is built at static-init via `cl::values(...)`.

> **NOTE —** the exact static-init `cl::values()` name roster (the spelled-out accepted strings) for the simulator's `MemSimMode`/`SyncMode` parsers was not dumped — the `Values` table is built in static ctors and indexed by `getOption(i)`. The enum *ordinals* are documented in the D-D03/D-D05/D-D09/D-D12 enum strands; the command-line *spellings* need an `.init_array` walk. (MEDIUM — gap.)

---

## Related Components

| Name | Relationship |
|---|---|
| [The Two-Pass BIR-JSON Loader](../bir/json-loader.md) | `adl_serializer<Module>::from_json` — the canonical parse path `bir_roundtrip` invokes |
| [The BIR-JSON Write Path](../bir/json-writer.md) | `adl_serializer<Module>::to_json` / `Instruction::toJson` — the canonical emit path; the Dumper's text emitter is its second-format twin |
| [Legality Dispatch](legality-dispatch.md) | the verifier cluster this debug serializer belongs to; same hand-rolled `Inst+0x58` `IRVisitor` switch, non-polymorphic |

## Cross-References

- [The Two-Pass BIR-JSON Loader](../bir/json-loader.md) — the parse half of the canonical (de)serializer; `bir_roundtrip` step 5
- [The BIR-JSON Write Path](../bir/json-writer.md) — the emit half; `bir_roundtrip` step 6; contrasts with the Dumper's flat text
- [Legality Dispatch](legality-dispatch.md) — the BIR verifier cluster; structural twin of the Dumper's opcode-switch `IRVisitor`
