# klr::ExtendedInst & the CBOR Wire Format

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 carry the identical class — same `.dynsym` `klr::ExtendedInst` / `klr::OperatorExtendedInstWrapper` symbols). Subject binary: `neuronxcc/.../data/bin/nki_klr_sim` — the **klr simulator/driver**, a 5,788,224-byte C++ ELF with fully-demangled symbols in `.dynsym`. `PT_LOAD` is mapped 1:1, so virtual address equals file offset for `.text`/`.rodata` and every `sub_*@0xADDR` below is read straight off that binary's IDA decompile. The identical record is also compiled into `libwalrus.so` / `libBIR.so` (the backend cross-module match, [7.21](klir-codegen-dispatch.md)). Evidence is tagged CONFIRMED (literal store/read in a decompiled body, cross-checked ser↔des↔to_string) / STRONG (one body + symbol/RTTI corroboration) / INFERRED (symbol + cross-module) / SPECULATIVE.*

## Abstract

`klr::ExtendedInst` is the **beta2** intermediate-representation record that carries a custom operator through the serialized-KLR path — the `KlirToBirCodegen` lane ([7.21](klir-codegen-dispatch.md)), the dormant sibling of the beta3 `BirCodeGenLoop` driver. It is not the BIR-level custom-op object; it sits one IR level **upstream** of `bir::InstCustomOp` and the `0x85`/`0x86` ISA words. On the wire it is a **CBOR** (RFC 8949) record carried under semantic **tag 210**, and in the klr `Operator` variant it appears as **kind 70** (`klr::OperatorExtendedInstWrapper`). This page pins the 80-byte record layout field-by-field, reproduces the CBOR (de)serialization as annotated pseudocode against real symbols, decodes both nested tags byte-for-byte, and documents the one structural surprise the simulator hands us: **`nki_klr_sim` deserializes and prints `ExtendedInst` but flatly refuses to lower it** — its per-`Operator` codegen has no case for kind 70.

The bar is that a reimplementer can encode or decode an `ExtendedInst` record on the klr wire, recognize its two nested CBOR tags, and place it correctly in the IR descent. Three independent function bodies — serializer, deserializer, and `to_string` printer — agree on the same six fields and the same 80-byte footprint, so the layout is CONFIRMED at the strongest available level: byte-exact across a full round-trip.

For reimplementation, the contract is:

- An `ExtendedInst` is a flat 80-byte record of six fields — `opcode:u32`, `hasRead:u8`, `hasWrite:u8`, `ports:u32`, `data0:std::list<u32>`, `data1:std::list<u32>` — reached through a `make_shared` inplace control block. There is **no** op-name string, **no** argument vector, and **no** payload blob at this level.
- Its inner CBOR frame is `0xD9 0xD2 0x00 0x86` — tag-marker, tag-number `0xD200` (hi=210, lo=0), wrapping a 6-element array. The `6` is the **field count**, not an argument or payload count.
- Wrapped in a klr `Operator`, it is variant **kind 70** (`0x46`, 1-based) with an outer CBOR frame `0xD9 0xDA 0x45` + array(1) (hi=218, wire-variant=69 = kind−1).

## Where it sits in the IR descent

The serialized-KLR stream `nki_klr_sim` reads is a recursive CBOR AST: a `Block` of statements, each statement an `Operator`, each `Operator` a polymorphic variant. `ExtendedInst` is reached as one such variant:

```
Block (tag 104,0,3)                        Block_des  sub_5548C0
  └─ Operator  (outer tag 0xD9 0xDA …)      Operator_des sub_5523D0  (variant switch)
        └─ kind 70  → ExtendedInst          case 69 → ExtendedInst_des sub_5397E0
              (inner tag 0xD9 0xD2 0x00 …)
```

`Block_des` (`sub_5548C0`) carries the literal `"Expecting Block:(104,0,3)"`; `Operator_des` (`sub_5523D0`) switches on the outer variant byte and, for the `ExtendedInst` variant, calls `ExtendedInst_des`. This places `ExtendedInst` as a leaf klr `Operator` inside a klr `Block`. [CONFIRMED — the `Block`→`Operator`→`ExtendedInst` des chain, decompiled bodies.]

`ExtendedInst` is the klr-level container; its downstream realization — `bir::InstCustomOp` plus the `CUSTOM_OP_HEADER 0x85` + `PAYLOAD 0x86` ISA pair — is produced on the libwalrus/Penguin side, not here (see the CustomOp wire layer, `custom-ops/` planned, and [7.21](klir-codegen-dispatch.md)). The bridge that fills `InstCustomOp` from an `ExtendedInst` is not present in `nki_klr_sim`. [INFERRED — cross-module; `nki_klr_sim` carries no `bir::InstCustomOp` / `CustomOpLib` strings at all.]

## 1. The 80-byte field layout (CONFIRMED, byte-exact)

Three function bodies agree on the same offsets. The deserializer allocates the record and writes each field; the serializer reads the same offsets back; the printer reads them a third time.

| off | field | type | ser primitive | des primitive | to_string read |
|-----|-------|------|---------------|---------------|----------------|
| +0 | `opcode` | `u32` | `Nat_ser` | `Nat_des` | `*(u32*)(p+0)` |
| +4 | `hasRead` | `u8` (bool) | `Bool_ser` | `Bool_des` | `*(u8*)(p+4)` |
| +5 | `hasWrite` | `u8` (bool) | `Bool_ser` | `Bool_des` | `*(u8*)(p+5)` |
| +8 | `ports` | `u32` | `Nat_ser` | `Nat_des` | `*(u32*)(p+8)` |
| +16 | `data0` | `std::list<u32>` | `List_Nat_ser` | `List_des` | head +16 / size +32 |
| +40 | `data1` | `std::list<u32>` | `List_Nat_ser` | `List_des` | head +40 / size +56 |

`sizeof == 80` (`+64..+79` = the second list's trailing slack / tail padding; only `+0..+56` are touched by any of the three bodies). `data0`/`data1` are doubly-linked `std::list<u32>` — each node is a 24-byte `_List_node_base{prev,next}` + the `u32` value at node+16, freed via `operator delete(…, 0x18u)`. The `+32`/`+56` size words and the `i = *(i64*)i` link-walk in the printer prove `std::list`, **not** `std::vector` and **not** a `vector<TensorRef>`. [CONFIRMED — ser `sub_531450`, des `sub_5397E0`, to_string `sub_56C800` all agree.]

> **GOTCHA — there is no op-name, no num_arguments, no num_payloads, no args vector at this level.** Those fields live one IR level down, on `bir::InstCustomOp` and in the `0x85` CUSTOM_OP_HEADER word. The only op identity an `ExtendedInst` carries is the raw `opcode:u32`. A grep of `nki_klr_sim`'s strings finds no `"CustomOpLib"`, `"libbuiltincustomop"`, `"SundaCustomOpLibrary"`, or builtin-leaf names — op-name → `.so` resolution is a BIR/runtime concern, not a klr one. [CONFIRMED for the negative.]

### (A) Serializer — `klr::ExtendedInst_ser` (`sub_531450` @ `0x531450`, size 161)

The body is a short-circuit chain: emit the tag, then one primitive per field in order.

```c
// sub_531450(FILE *s, ExtendedInst **a2):  *a2 -> payload base
//   a2 is &shared_ptr; *a2 dereferences to the record (block+16).
sub_5670C0(s, 210, 0, 6);              // tag writer: 0xD9 0xD2 0x00 + Nat(6)
sub_5672D0(s, **a2);                   // Nat_ser(opcode)      payload +0
sub_5672C0(s, *((u8 *)*a2 + 4));       // Bool_ser(hasRead)    payload +4
sub_5672C0(s, *((u8 *)*a2 + 5));       // Bool_ser(hasWrite)   payload +5
sub_5672D0(s, (*a2)[2]);               // Nat_ser(ports)       payload +8   (u32 index 2)
sub_52F590(s, (QWORD *)*a2 + 2);       // List_Nat_ser(data0)  payload +16  (qword index 2)
return sub_52F590(s, (QWORD *)*a2 + 5);// List_Nat_ser(data1)  payload +40  (qword index 5)
```

Helper chain: `sub_5672D0 → sub_567310` = `Nat_ser`; `sub_5672C0 → sub_5674C0` = `Bool_ser`; `sub_52F590` = `List_Nat_ser` (writes a list-header then one `Nat` per node, value at node+16); `sub_5670C0 → sub_567760` = the tag writer. [CONFIRMED — `sub_531450` body.]

### (B) Deserializer — `klr::ExtendedInst_des` (`sub_5397E0` @ `0x5397e0`, size 1410)

```c
// sub_5397E0(__int64 *a1 /*out shared_ptr*/, FILE *a2):
sub_567090(a2);                        // read 3-byte tag -> (v21=hi, v22=lo, v23=count)
if (v21 != 0xD2 /*210*/ || v22 != 0 || v23 != 6)
    throw runtime_error("Expecting ExtendedInst:(210,0,6)");
                                       // (no tag found -> "Could not find tag, expecting ExtendedInst:210,0")
v6 = operator new(0x50);               // 80-byte make_shared inplace block
*(QWORD *)(v6 + 0)  = off_9739E0;      //   control-block vtable (Sp_counted_ptr_inplace)
*(QWORD *)(v6 + 8)  = 0x100000001;     //   use/weak refcounts = 1 / 1
//   payload base = v6 + 16; both std::list heads self-initialised to empty
*(u32 *)(pay + 0) = sub_567130(a2);    // Nat_des  -> opcode
*(u8  *)(pay + 4) = sub_567100(a2);    // Bool_des -> hasRead
*(u8  *)(pay + 5) = sub_567100(a2);    // Bool_des -> hasWrite
*(u32 *)(pay + 8) = sub_567130(a2);    // Nat_des  -> ports
sub_52FEB0(&tmp, a2);  /* splice into pay+16; size -> pay+32 */   // List_des -> data0
sub_52FEB0(&tmp, a2);  /* splice into pay+40; size -> pay+56 */   // List_des -> data1
```

Helpers: `sub_567130` = `Nat_des` (`"expecting Nat"`); `sub_567100` = `Bool_des` (`"expecting Bool"`); `sub_52FEB0` = `List_des` (`"expecting List"`). List nodes are freed via `operator delete(…, 0x18u)` — 24-byte `std::list` nodes. [CONFIRMED — `sub_5397E0` body; `operator new(0x50u)` literal at the alloc site.]

> **GOTCHA — the vtable at block+0 is `make_shared` machinery, not a class vtable.** RTTI names it `std::_Sp_counted_ptr_inplace<klr::ExtendedInst, …>` (typeinfo `0x971a48`, vtable `0x9739d0`/`off_9739E0`). `klr::ExtendedInst` itself is a **non-polymorphic POD** — it has no standalone `_ZTIN3klr12ExtendedInstE` typeinfo and the payload (block+16) has no vptr; its first byte is `opcode`. The record is always reached through a `shared_ptr` to that inplace block. [CONFIRMED — `rtti.json`: the only `ExtendedInst` typeinfo is the `Sp_counted_ptr_inplace` wrapper.]

### (C) Printer — `klr::to_string(ExtendedInst&)` (`sub_56C800` @ `0x56c800`, size 1624)

Emits `"ExtendedInst(opcode=…, hasRead=…, hasWrite=…, ports=…, data0=[…], data1=[…])"`, reading `opcode` at `+0`, `hasRead` at `+4`, `hasWrite` at `+5`, `ports` at `+8`, then iterating each list (head `+16`/`+40`, cached size `+32`/`+56`, link-walk `i = *(i64*)i` with the `u32` value at node+16, i.e. `*((u32*)node+4)`). The `"ExtendedInst("` literal is at `0x874b08`, `"data0="` follows it. [CONFIRMED — `sub_56C800` body + string table.]

## 2. The CBOR encoding — tag 210 (CONFIRMED)

The klr codec is canonical CBOR. The primitive reader `sub_567DD0` (the `Nat`/header reader) is textbook RFC 8949:

```c
// sub_567DD0(FILE *stream, u64 *out): read one CBOR data-item header + length
fread(&hdr, 1, 1, stream);
addl  = hdr & 0x1F;          // additional-info  (low 5 bits)
major = hdr >> 5;            // major type        (high 3 bits)
if (addl <= 0x17) v = addl;                       // inline value 0..23
else switch (addl) {
    case 0x18: v = read_u8 (stream);          break;   // 1 length byte
    case 0x19: v = bswap16(read_u16(stream));  break;   // 2 bytes, big-endian
    case 0x1A: v = bswap32(read_u32(stream));  break;   // 4 bytes
    case 0x1B: v = bswap64(read_u64(stream));  break;   // 8 bytes
    default:   return false;
}
*out = v;
return (major == 4);          // caller requires major-type-4 ARRAY
```

The big-endian byteswaps and the 23/24/25/26/27 (`0x17`/`0x18`..`0x1B`) length ladder are exactly CBOR's; the `major == 4` return constrains the array-header path. [CONFIRMED — `sub_567DD0` body.]

The semantic-tag layer sits on top:

- **Tag reader** `sub_567F10` (@ `0x567f10`): `fread` 3 bytes; **require `ptr[0] == 0xD9`** (CBOR major-6 tag, 2-byte tag-number follows); out `hi = ptr[1]`, `lo = ptr[2]`; then read a `Nat` (the element count) and reject if `> 0x17`. ⇒ wire shape `0xD9 <hi> <lo> Nat`.
- **Tag writer** `sub_567760` (@ `0x567760`): `ptr[0] = -39` (= `0xD9`); `ptr[1] = hi`; `ptr[2] = lo`; `fwrite` 3 bytes; then write the `Nat` count (`<= 0x17`). An exact mirror.

So the `ExtendedInst` tag — from `sub_531450 → sub_5670C0(s, 210, 0, 6)` — lands on the wire as:

```
   0xD9     0xD2     0x00     0x86
   ^tag-6   ^hi=210  ^lo=0    ^array, len 6   (0x80 | 6 = 0x86 = CBOR major-4 array(6))
```

The CBOR tag number is `0xD200 = (210 << 8) | 0 = 53760`; its content is a 6-element array. [CONFIRMED — tag writer/reader bodies + the `sub_5670C0(s,210,0,6)` literal.]

> **GOTCHA — the colloquial "(210,0,6)" tuple drops the marker byte and mis-reads the `6`.** The full inner wire form is `0xD9 0xD2 0x00 0x86`; the leading `0xD9` major-6 tag marker is mandatory. And the `6` is the CBOR **array element count = the number of serialized fields (6, fixed)** — it is *not* `num_arguments` and *not* `num_payloads`. Those counts do not exist at the klr level; they are bir-level (`InstCustomOp` / the `0x85` header). The deserializer enforces `count == 6` exactly. [CONFIRMED — des `sub_5397E0` checks `v23 == 6`.]

Decomposed: `210` is the klr instruction-KIND tag hi-byte for the `ExtendedInst` record class, `0` the lo/sub-byte, `6` the array arity. The outer `Operator` wrapper (next section) uses a **different** hi-byte (218) and a per-variant lo-byte — two nested CBOR tags, an inner `0xD2` inside an outer `0xDA`.

## 3. The Operator wrapper — kind 70 (CONFIRMED)

`klr::Operator` is a polymorphic variant base; `klr::OperatorExtendedInstWrapper` is the variant that carries an `ExtendedInst`, parallel to `OperatorTensorTensorWrapper`, `OperatorScalarTensorTensorWrapper`, and the other ~75 variants. RTTI/vtable for the wrapper: typeinfo `0x972168`, vtable `0x974a70`/`off_974A80`, demangled `std::_Sp_counted_ptr_inplace<klr::OperatorExtendedInstWrapper, …>` (string `0x8b39c0`). [CONFIRMED — `rtti.json`.]

The wrapper object is a 24-byte payload inside a `0x28` shared-count inplace block:

| where | content |
|-------|---------|
| block+0 | shared-count (control-block) vtable `off_974A80` |
| block+8 | refcounts (use=1, weak=1 → `0x100000001`) |
| block+16 → payload +0 | `u32 discriminant = 70` (`0x46`, 1-based klr `Operator`-kind enum) |
| payload +8 | `std::shared_ptr<klr::ExtendedInst>` (the wrapped record) |

`to_string(OperatorExtendedInstWrapper&)` (`sub_56D480` @ `0x56d480`) emits `"OperatorExtendedInstWrapper("` (string `0x874bc0`), dereferences the inner `shared_ptr` at `wrapper+8`, calls `to_string(ExtendedInst&)` on it, and closes the paren. [CONFIRMED — `sub_56D480` reads `*(a2+8)` and tail-calls `sub_56C800`.]

### The outer Operator CBOR tag

The `Operator` serializer (`sub_534460` @ `0x534460`) and deserializer (`sub_5523D0` @ `0x5523d0`) frame every variant under hi-byte **218** (`0xDA`) with a 1-element array:

```c
// Operator_ser  sub_534460(FILE *s, Operator ***a2):
kind = *(u32 *)*a2;                    // in-memory discriminant (1-based)
if ((u32)(kind - 1) > 0x4B)            // valid kinds 1..76; else:
    throw runtime_error("Unknown Operator type in serialization");
sub_5670C0(s, 218, kind - 1, 1);       // outer tag: 0xD9 0xDA <kind-1> array(1)
switch (kind) {
    /* … */
    case 0x46:                         // kind 70 = ExtendedInst
        return sub_531450(s, (uint**)v5 + 1);   // -> ExtendedInst_ser
    /* … */
}
```

```c
// Operator_des  sub_5523D0 — require outer tag hi == 0xDA(218); switch(wire-variant):
case 69:                               // wire byte 69 = kind 70 minus 1
    require(array_count == 1) else "Wrong number of elements";
    v107 = operator new(0x28);
    *(u32  *)(v107 + 16) = 70;          // restore in-memory discriminant = 70
    *(QWORD *)v107       = off_974A80;  // wrapper control-block vtable
    sub_5397E0(out, a2);               // -> ExtendedInst_des
```

⇒ the wrapper wire frame for an `ExtendedInst` is `0xD9 0xDA 0x45` + array(1). [CONFIRMED — ser writes `kind-1`, des restores `+1`, both pinned in the bodies.]

> **GOTCHA — the deliberate ±1 between the on-wire variant byte and the in-memory enum.** On the wire the variant is `69` (`0x45`, 0-based). In memory the discriminant is `70` (`0x46`, 1-based). The serializer emits `kind − 1`; the deserializer writes `70` for wire-case `69`. Reading the wire byte `0x45` as the kind, or the in-memory `0x46` as the wire byte, is off by one in opposite directions. [CONFIRMED — `sub_534460` / `sub_5523D0`.]

## 4. The simulator consumes but refuses to codegen ExtendedInst (CONFIRMED)

`nki_klr_sim` is a klr-stream **consumer**: within it, `klr::ExtendedInst` is *only ever* constructed by the deserializer `sub_5397E0` (the sole xref to the inplace control-block typeinfo/vtable is from there), and the wrapper is *only ever* constructed by the `Operator` deserializer `sub_5523D0`. There is no in-binary builder that mints an `ExtendedInst` from operands — the record arrives already-encoded from an upstream producer (the NKI frontend / libwalrus that emits the klr CBOR stream). [CONFIRMED — `xrefs.json`.]

The decisive finding is on the lowering side. The klr `Operator` → BIR codegen dispatcher in this simulator is `sub_50EDA0` (carrying `"encounter operator "` and `"Unsupported operator "`), a 77-way switch on the `Operator` discriminant at payload +0. Its case labels run `… case 0x44, case 0x45, case 0x47, case 0x48 …` — **there is no `case 0x46`**. Kind 70 therefore falls through to:

```c
default:                               // sub_50EDA0, no case 0x46
    throw runtime_error("Unsupported operator " + std::to_string(70));
```

So this simulator deserializes, round-trips, and pretty-prints an `ExtendedInst`, but its `KlirToBirCodegen`-style dispatcher deliberately leaves kind 70 out of scope and raises at lowering time. The `ExtendedInst → bir::InstCustomOp` lowering (and the `0x85`/`0x86` ISA emission) happens in libwalrus, not here. [CONFIRMED — `sub_50EDA0`: `case 0x45` and `case 0x47` present, `0x46` absent, default raises `"Unsupported operator "`.]

This reconciles the two IR levels cleanly: the klr `(210,0,6)` record is the upstream container; the `bir::InstCustomOp` + `CUSTOM_OP_HEADER 0x85` / `PAYLOAD 0x86` pair is the downstream realization, produced by the libwalrus/Penguin custom-op path. `nki_klr_sim` does codegen the *typed* klr ops it does have cases for (matmul, activation, tensor-scalar, …) — `ExtendedInst` is the one variant it round-trips but will not lower. [STRONG — the negative is CONFIRMED in `nki_klr_sim`; the positive downstream chain is cross-referenced to the backend, not re-derived here.]

## 5. Adversarial self-verification

The five strongest claims, re-checked against the binary:

| Claim | Verdict | Anchor |
|-------|---------|--------|
| 80-byte record | **CONFIRMED** | `operator new(0x50u)` literal in des `sub_5397E0`; payload = block+16, fields end at +56, slack +64..+79 |
| CBOR tag-210 (`0xD9 0xD2 0x00 …`) | **CONFIRMED** | writer `sub_567760` (`ptr[0]=-39`=0xD9); ser `sub_5670C0(s,210,0,6)`; des rejects unless `hi==0xD2 && lo==0 && count==6` |
| Operator kind 70 / wire 69 | **CONFIRMED** | ser `sub_5670C0(s,218,kind-1,1)` case `0x46`; des case `69 → disc=70`, vtable `off_974A80` |
| CustomOp-carrier role / lowering refusal | **CONFIRMED (negative) / INFERRED (positive)** | `sub_50EDA0` has no case `0x46` → `"Unsupported operator"`; the `→ InstCustomOp` bridge is libwalrus-side, not in this binary |
| Field layout +0/+4/+5/+8/+16/+40 | **CONFIRMED** | ser `sub_531450`, des `sub_5397E0`, to_string `sub_56C800` agree on all six offsets |

Re-verify ceiling — what is **not** pinnable from `nki_klr_sim` alone:

- **`data0` vs `data1` semantics.** Both are `std::list<u32>` the simulator only round-trips. Whether they are (input-descriptor ints, output-descriptor ints) or (sub-op+immediates, operand ints) is set by the upstream **producer**, which is not in this binary. [OPEN.]
- **The `ports` u32 bit assignment** (engine/port mask) is read as an opaque `Nat`. [OPEN.]
- **The `opcode` → libwalrus-opcode / GpSimd-ucode-key mapping.** `opcode` is a raw int with no name table in `nki_klr_sim`. [OPEN.]
- **Whether any shipped op actually emits an `ExtendedInst`.** Because the simulator refuses to codegen it, `ExtendedInst` may be a legacy/raw-escape carrier consumed only by the libwalrus/Penguin path; a captured klr CBOR sample with a `0xD9 0xD2 0x00` frame would settle it. [OPEN.]
- **`+64..+79`** of the 80-byte record is assumed tail padding / list slack; only `+0..+56` are touched by any body. No 7th field was observed. [SPECULATIVE.]

## Cross-references

- [7.21 KlirToBirCodegen Dispatch Core](klir-codegen-dispatch.md) — the beta2 lowering lane that owns this record on the backend side; the `klr::Stmt`/`klr::Operator` node tree.
- klr node schema (7.33, `bir/` planned) — the sibling `klr::Operator` variant catalog this record is kind 70 of.
- The CustomOp wire layer (Part 11, `custom-ops/` planned) — `bir::InstCustomOp` and the `CUSTOM_OP_HEADER 0x85` + `PAYLOAD 0x86` ISA pair, the downstream realization of this carrier.
- [Collective / GPSIMD / CustomOp Encoding](../isa/collective-customop-encoding.md) — the engine-level ISA words a custom op ultimately becomes.
