# Snapshot / Decomposed Input Format

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, tool ELF `neuronxcc/starfish/bin/snapshot-unpack` (cp310; BuildID `5eb26caa7b12ffc4`, 226 MB, not stripped). Other wheels differ; treat every address as version-pinned. Section deltas for this binary: `.text` VA→fileoff `−0x100000`, `.rodata` VA→fileoff `−0x200000`. All addresses below are VA.*

## Abstract

A captured `xla::HloSnapshot` is the framework's way of freezing one concrete execution — the HLO module plus the *actual* argument and result tensors baked in. `snapshot-unpack` is the offline utility that turns such a capture back into compiler-consumable inputs: it writes the embedded `HloModuleProto` to disk (`./model.hlo` by default) and optionally dumps each baked-in literal as a NumPy `.npy`. This page documents the *input* side — the wire format that comes in, not the NEFF that eventually goes out.

There are **two** input encodings, and the choice is not made by a `--decomposed` flag. It is the string value of one cl::opt, `snapshot-type` (default `"snapshot"`), compared in `main` (`0x1e25170`):

- `snapshot-type=snapshot` — the whole file is **one** `HloSnapshot` proto, slurped with `MessageLite::ParseFromIstream`. Literals live *inside* the snapshot (`arguments` + `result`).
- `snapshot-type=decomposed` — a **custom length-framed stream**: a framed leading `HloSnapshot`, then a repeated `LiteralProto` block of inputs, then an optional repeated block of outputs. Each frame is sized by a one-byte length prefix whose payload is itself a tiny serialized `LiteralProto`.
- anything else — `cerr << "ERROR: Unsupported snapshot format\n"`, exit 1.

The decomposed format exists because a producer can stream literals it computed *separately* from the HLO module, instead of round-tripping every tensor through the snapshot proto. The format has **no magic number and no file header**; the only structure is the length prefix. Decoding it correctly hinges on one counter-intuitive primitive whose IDA auto-name is a lie — `read_uint64` does not read a u64.

| | |
|---|---|
| **Entry / dispatch** | `main` @ `0x1e25170` (2135 B); `snapshot-type` compares at `0x1e251f8`, `0x1e25265` |
| **Length primitive** | `(anon)::read_uint64(ifstream&)` @ `0x1e24440` (245 B) — `_ZN12_GLOBAL__N_111read_uint64E…` |
| **Repeated reader** | `(anon)::read_repeated_message<LiteralProto>` @ `0x1e24910` (`.constprop.0`) |
| **NumPy dump** | `(anon)::dumpHelper(LiteralProto&, const string&, int)` @ `0x1e24b00` (1372 B) |
| **Final conversion** | `hilo::literal2npy(const Literal&, const string&, bool, const string&)` @ `0x73cc580` |
| **Compat shims** | `hilo::convertProtoForTF2_11(HloModuleProto*)` @ `0x1e260a0`; `(LiteralProto*)` @ `0x1e26150` |
| **Element stride** | `sizeof(xla::LiteralProto)` = `0x1A8` (push_back stride) |
| **Wire output** | `<output-prefix><module>` (`./model.hlo`) + `<output-prefix><name><idx>.npy` |

## The length primitive — `read_uint64` is a misnomer

> **GOTCHA — `read_uint64` does not read a raw little-endian u64.** The IDA auto-name is wrong. It reads a **1-byte length L**, then **L bytes parsed as an `xla::LiteralProto`**, and returns a `uint64` pulled from inside that proto. Integers on the decomposed wire are *boxed* as miniature serialized `LiteralProto` messages.

The disassembly is unambiguous (`0x1e24440`):

```c
// _ZN12_GLOBAL__N_111read_uint64ERSt14basic_ifstreamIcSt11char_traitsIcEE
uint64_t read_uint64(std::ifstream &in) {
    char lenbuf[1];
    in.read(lenbuf, 1);                       // 0x1e2446e: mov $0x1,%edx ; Si::read  → ONE byte
    long L = (signed char)lenbuf[0];          // 0x1e24473: movsbq  → SIGN-EXTENDED (see QUIRK)
    char *body = operator new[](L);           // 0x1e2447e: _Znam
    in.read(body, L);                         // 0x1e24493: Si::read  → L bytes
    xla::LiteralProto lp(/*arena=*/nullptr);  // 0x1e244a3: LiteralProto ctor
    lp.ParseFromString(std::string(body, L)); // 0x1e244c7: MessageLite::ParseFromString
    uint64_t v = *(uint64_t*)((char*)&lp + 0x70);  // 0x1e244ea: mov -0x190(%rbp) == lp+0x70 ; (%rax)
    delete[] body;                            //   (lp base is -0x200(%rbp); 0x200-0x190 = 0x70)
    return v;                                 // 0x1e24516: mov %r12,%rax ; ret
}
```

The returned integer is `lp.u64s(0)` — the first (and only) element of the packed `u64s` repeated field (field #7), which sits at in-memory offset `+0x70` in the `LiteralProto` object.

*Anchors: `movsbq -0x201(%rbp)` (the signed length byte); `_Znam`; `ParseFromString` @ `0x1e244c7`; the return load `-0x190(%rbp)` then `(%rax)` @ `0x1e244ea`.* The `+0x70 ⇒ u64s` field-number mapping is [INFERRED] from the access pattern rather than from a generated descriptor; the *wire* bytes are standard protobuf either way.

> **QUIRK — the length byte is read as a *signed* char.** `movsbq` sign-extends `lenbuf[0]`. A producer must keep every length-prefix box **< 128 bytes**; a byte ≥ 0x80 sign-extends to a negative `L`, which then flows into `operator new[]((size_t)L)` as a near-`SIZE_MAX` request — a guaranteed `bad_alloc`/abort, not a 128–255-byte read. This cap applies only to the *integer-box message* that encodes a length, never to the literal *body* it precedes (whose length is the full 64-bit boxed value).

### Two reads, one primitive: box-as-integer vs box-as-length

`read_uint64` is the *only* framing primitive, but callers use its return value two different ways:

| Use | Caller | Returned `uint64` means |
|---|---|---|
| **box-as-integer** | element count `N`; leading-message length `Lm` | the literal integer payload `lp.u64s(0)` |
| **box-as-length** | per-element header inside `read_repeated_message` | the byte length `L` of the body that follows |

So a single framed element on the decomposed wire is always two physical reads: a 1-byte-prefixed integer box (giving a length), immediately followed by that many bytes of real `LiteralProto` body.

## Byte-layout spec — the decomposed stream

```
Primitive A — boxed-uint64   (read_uint64 @0x1e24440)
  off  width  field
  0    1      L      u8 length of the box payload (signed → keep L < 128)
  1    L      msg    serialized xla::LiteralProto carrying ONE integer in u64s
  ----        ⇒ returns lp.u64s(0)  (in-mem lp+0x70)

Primitive B — length-delimited LiteralProto element   (inline in read_repeated_message)
  step              field
  Primitive A    →  hdr    boxed-uint64 here used as the byte length L of the body
  L bytes        →  body   serialized xla::LiteralProto (the real literal), ParseFromString

Composite C — repeated<LiteralProto>   (read_repeated_message @0x1e24910)
  step           via            field
  1              Primitive A    N        element count (N==0 ⇒ empty; test %rax,%rax @0x1e24945)
  2..(N times)   Primitive B    element  pushed into out vector (stride 0x1A8)

Whole DECOMPOSED file   (main 0x1e2527f .. 0x1e2554a)
  region    via                       contents
  [0]       Primitive A, then raw      leading HloSnapshot:
                                         Lm = read_uint64(in)            ; 0x1e25286
                                         buf = new char[Lm]; in.read(buf,Lm)
                                         HloSnapshot::ParseFromString(buf); 0x1e252f2
                                         (NOTE: leading msg is an HloSnapshot,
                                          NOT a bare HloModuleProto)
  [1]       Composite C               value_input literals     (run inputs)   ; 0x1e253d9
  [2] (opt) Composite C               snapshot_output literals (only if
                                         --dump-snapshot-outputs)             ; 0x1e254ae
```

There is **no terminator** and no outer count for the two repeated sections — each is self-delimited by its own leading boxed `N` (Primitive A). The leading region `[0]` is framed by a *single* Primitive-A box giving its byte length `Lm`, then `Lm` raw bytes parsed as `HloSnapshot` — note it is **not** wrapped via Primitive B (no nested `LiteralProto` body), it is a bare new+read+ParseFromString.

> **NOTE — the leading HloSnapshot may be literal-empty.** In the decomposed flow the real tensors arrive in regions `[1]`/`[2]`. `main` only ever reads `snap.hlo().hlo_module()` out of region `[0]`; its baked-in `arguments`/`result` are ignored. A producer can therefore ship a snapshot whose literal sets are empty/dummy and stream the data separately. This is [INFERRED] from `main` never touching `snap.arguments()` on the decomposed branch, not from an explicit check.

## Dispatch — `main` (`0x1e25170`)

Both branches do the same three things — extract the `HloModuleProto`, run `convertProtoForTF2_11`, write `<output-prefix><module>`. The only structural difference is *where the literals come from*.

```c
cl::ParseCommandLineOptions(argc, argv, "snapshot-unpack\n", ...);     // 0x1e251c7
std::ifstream in(snapshotFile, ios::in|ios::binary);                   // mode 4
if (!in.is_open()) { cerr << "Could not open: " << snapshotFile; return 1; }

if (snapshotType.compare("snapshot") == 0) {                           // 0x1e251f8  BRANCH A
    xla::HloSnapshot snap;                                             // 0x1e25226 ctor
    if (snap.ParseFromIstream(&in)) {                                  // 0x1e25235  whole file = one proto
        auto mod = make_unique<HloModuleProto>();
        mod->CopyFrom(snap.hlo().hlo_module());
        hilo::convertProtoForTF2_11(mod.get());
        ofstream out(outputPrefix + moduleFile);                       // "./model.hlo"
        mod->SerializeToOstream(&out);                                 // 0x1e25701
        for (i, arg : snap.arguments())  dumpHelper(arg, "value_input", i);  // 0x1e25774
        if (dumpOutputs) {                                             // 0x1e257b5  (byte @0x9860B98)
            auto &result = snap.hlo()…result;
            if (result.shape().element_type() == 13 /*TUPLE*/)         // 0x1e257e2: cmpl $0xd,0x58(%rax)
                for (j, sub : result.tuple_literals()) dumpHelper(sub, "snapshot_output", j);
            else dumpHelper(result, "snapshot_output", 0);
        }
    } else r14 = 1;                                                    // parse fail → exit 1
}
else if (snapshotType.compare("decomposed") == 0) {                   // 0x1e25265  BRANCH B
    uint64 Lm = read_uint64(in);                                      // 0x1e25286  region [0] length
    char *buf = new char[Lm]; in.read(buf, Lm);
    xla::HloSnapshot snap; snap.ParseFromString({buf, Lm});           // 0x1e252f2
    auto mod = make_unique<HloModuleProto>();
    mod->CopyFrom(snap.hlo().hlo_module());                           // 0x1e25352
    hilo::convertProtoForTF2_11(mod.get());                          // 0x1e2535a
    ofstream out(outputPrefix + moduleFile);
    mod->SerializeToOstream(&out);                                    // 0x1e2539d
    vector<LiteralProto> inputs;
    read_repeated_message(in, inputs);                               // 0x1e253d9  region [1]
    for (i, lit : inputs) dumpHelper(lit, "value_input", i);          // 0x1e25439
    if (dumpOutputs) {                                                // 0x1e25472
        vector<LiteralProto> outs;
        read_repeated_message(in, outs);                            // 0x1e254ae  region [2]
        for (j, lit : outs) dumpHelper(lit, "snapshot_output", j);    // 0x1e25511
    }
}
else { cerr << "ERROR: Unsupported snapshot format\n"; r14 = 1; }    // 0x1e2564e
in.close(); return r14;                                               // 0 ok, 1 error
```

The element-type gate `cmpl $0xd,0x58(%rax)` (`0x1e257e2`) tests `shape.element_type() == 13`, the XLA `PrimitiveType::TUPLE`: a tuple result iterates `tuple_literals`, a scalar/array result dumps a single literal. The compare itself is read directly; the `13 ⇒ TUPLE` reading comes from XLA's `PrimitiveType` numbering (PRED=1 … F64=12, TUPLE=13), cross-checked against the ordering of the binary's own type-assertion strings.

> **GOTCHA — none of these four names is a command-line flag.** `snapshot` and `decomposed` are the two accepted *values* of the single `snapshot-type` option; `value_input` and `snapshot_output` are the hard-coded `name` arguments handed to `dumpHelper`, which become the `.npy` filename stems. The five real `cl::opt`s are listed below.

### CLI surface (cl::opt, recovered from the ctor @`0x1e22550`)

| cl::opt | Flag / argstr | Kind | Default | Role |
|---|---|---|---|---|
| `snapshotFile` | `snapshot` (Positional) | positional | — | input snapshot file |
| `moduleFile` | `module` | string | `"model.hlo"` | output HLO module path stem |
| `outputPrefix` | `output-prefix` | string | `"./"` | prefix for every output path |
| `snapshotType` | `snapshot-type` | string | `"snapshot"` | selects `snapshot` vs `decomposed` |
| `dumpOutputs` | `dump-snapshot-outputs` | bool | `false` | also dump baked-in outputs (byte @`0x9860B98`) |

Banner `"snapshot-unpack\n"` (@`0x3c8310`), category `"snapshot-unpack options"`. Defaults decoded from the ctor stack immediates: `module` = `0x6C682E6C65646F6D` → `"model.hl"`+`'o'` = `"model.hlo"`; `snapshot-type` = `0x746F687370616E73` (LE) = `"snapshot"`; `output-prefix` = `"./"`.

## NumPy-dump path — `dumpHelper` (`0x1e24b00`)

Every literal — baked-in (branch A) or streamed (branch B) — funnels through the same dumper:

```c
void dumpHelper(xla::LiteralProto &lit, const std::string &name, int idx) {
    hilo::convertProtoForTF2_11(&lit);                             // 0x1e24b35  compat shim
    StatusOr<Literal> s =
        xla::MutableLiteralBase::CreateFromProto(lit, /*prohibit_empty_literal=*/true); // 0x1e24b4a (edx=1)
    if (!s.ok()) absl::…ThrowBadStatusOrAccess(s.status());        // 0x1e25012
    Literal v = std::move(*s);
    std::string fn = outputPrefix + name;                         // operator+
    fn.append(std::to_string(idx));                               // signed; '-' for negatives
    fn.append(".npy", 4);                                         // 0x1e24dd7 (@0x39e78c)
    hilo::literal2npy(v, fn, /*bool*/false, /*string*/"");        // 0x1e24e45
}
```

Filenames are therefore:

```
inputs   ./ + value_input     + i + .npy   →  ./value_input0.npy,  ./value_input1.npy, …
outputs  ./ + snapshot_output + j + .npy   →  ./snapshot_output0.npy, …
module   ./ + model.hlo                    →  ./model.hlo
```

`CreateFromProto` is called with `prohibit_empty_literal=true` (`mov $0x1,%edx` @`0x1e24b3a`), so an empty/degenerate literal is a hard error routed through `ThrowBadStatusOrAccess`, not silently skipped. The terminal conversion `hilo::literal2npy` (@`0x73cc580`, real symbol `_ZN4hilo11literal2npyERKN3xla7LiteralERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEbSB_`) is the static C++ `Literal`→`.npy` serializer, called from `0x1e24e45`. The meanings of its 3rd (`bool=false`) and 4th (`string=""`) arguments — Fortran order? dtype override? — are [UNRESOLVED] here; they belong to the hilo utility layer.

## Verbatim string evidence (VA; verified at fileoff = VA − 0x200000)

| VA | String | Role |
|---|---|---|
| `0x393561` | `snapshot` | `snapshot-type` value / positional argstr |
| `0x3db8e6` | `decomposed` | `snapshot-type` value |
| `0x3a23dc` | `value_input` | dumpHelper name, inputs |
| `0x39ae27` | `snapshot_output` | dumpHelper name, outputs |
| `0x39e78c` | `.npy` | output extension |
| `0x298a50` | `ERROR: Unsupported snapshot format\n` | unknown `snapshot-type` |
| `0x3df7a8` | `Could not open: ` | file-open failure |
| `0x3cbd42` | `<snapshot or decomposed>` | `snapshot-type` desc |
| `0x3c8310` | `snapshot-unpack\n` | ParseCommandLineOptions banner |

## Producer contract — how to write a decomposed file

A reimplementer producing a decomposed stream must emit, in order:

1. **Region [0]:** one Primitive-A box encoding the byte length `Lm` of the HloSnapshot (the integer `Lm` serialized as a single-element `u64s` `LiteralProto`, prefixed by its own 1-byte length `< 128`), then the `Lm` raw bytes of the serialized `HloSnapshot`.
2. **Region [1]:** one Primitive-A box encoding the input count `N`, then `N` × (Primitive-A box of body length `L`, then `L` bytes of serialized input `LiteralProto`).
3. **Region [2] (only if the consumer runs `--dump-snapshot-outputs`):** the same `[N][len][body]…` shape for outputs.

> **GOTCHA — region [2] is consumer-gated, not stream-gated.** Whether the reader *attempts* to read region [2] depends on the reader's `--dump-snapshot-outputs` flag, not on anything in the stream. If a producer always writes outputs but the consumer is run without the flag, region [2] is left unread in the file (harmless, the stream is closed); if the consumer *is* given the flag but the producer omitted region [2], the trailing `read_uint64` hits EOF and the resulting empty/garbage box yields an empty `outs` vector or a parse failure. Keep producer and consumer agreed on whether outputs are present.

## Cross-references

- **[3.16 XLA InferGoldens](xla-infergoldens.md)** — golden-input inference; the literals `snapshot-unpack` emits as `value_input*.npy` are the same kind of concrete tensors the golden path consumes.
- **Part 12 — Snapshot packaging / NEFF** — the output `model.hlo` re-enters the compile pipeline; the `.npy` dumps feed repro/debug, not the NEFF wire.
