# The klr Node Schema & KLR Wire Format

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 twins carry the identical roster and counts). Subject binary: `neuronxcc/.../bin/nki_klr_sim` — the stripped KLR simulator/driver that **statically links the whole `translate_nki_ast_to_bir` lowering set plus the `klr::*` CBOR deserializers**, so both the node deserializers and the `codegenNc<Op>` consumer bodies live in `.text` (`0x4dxxxx`–`0x56xxxx`). Names are recovered from RTTI typeinfo and from assert/source-line strings in `.rodata` (`0x87xxxx`–`0x88xxxx`); `.text`/`.rodata` have VA == file-offset. Evidence below is from this binary: `strings`, the IDA decompiled/disasm sidecars, and `xrefs.json`. Every claim is tagged CONFIRMED (read literally off a body/string) / STRONG (multi-evidence) / INFERRED / SPECULATIVE.*

## Abstract

**KLR** ("Kernel Language Representation") is the NKI front-end's lowered IR — a tree of `klr::*` C++ records (libwalrus-defined, mirrored by a Python `klr` dataclass module on the trace side). On disk it is a single recursive **CBOR** (RFC 8949) document; in memory it is a hierarchy of `std::shared_ptr`-held nodes. This page is the data-model reference: the **four-level tagged-union descent** (`Contents → LncKernel → Stmt → Operator → <Op>`), the **type-id roster** that names every serializable record class, the three universal operand records (**`TensorRef`**, **`Access`**, **`Immediate`**), and the **`KLRFile` container frame** that wraps the whole document. It is the schema the [7.21 KlirToBirCodegen dispatch core](klir-codegen-dispatch.md) consumes and that [7.32 `klr::ExtendedInst`](klr-extendedinst-cbor.md) extends with one leaf op.

KLR is to NKI roughly what LLVM bitcode is to a `.ll` file: a stable, versioned serialization of an in-memory IR, but with two differences a reimplementer must internalize. First, the discriminant model is **nominal, not positional** — every node, at every level, leads with an integer tag at offset `+0`, and the deserializer asserts the *exact* `(type-id, sub-id, field-count)` triple before it will read a body. Second, the wire is **canonical CBOR**: each node is a CBOR tag (major 6) carrying a CBOR array (major 4) of its fields, and the magic that marks the outer container is itself just a CBOR tag-number. The codec primitives (the `Nat` reader, the tag reader/writer) are owned by [7.32](klr-extendedinst-cbor.md#2-the-cbor-encoding--tag-210); this page builds on them and does **not** re-derive them.

The roster size is the page's first correction. The brief and the working notes cite an "88-entry type-id table"; the binary carries exactly **87** distinct `Expecting <Class>:(type-id,sub-id,field-count)` assert strings — one per deserializable record class — and a byte-identical second family (`Could not find tag, expecting …`). The two families name the same 87 classes; across them there are **86 distinct primary type-id bytes**, because `Activate2` and `KLRFile` collide on `217` and are split only by the sub-id byte. The full 87-row table is reproduced in [§3](#3-the-type-id-roster-87-records).

For reimplementation, the contract is:

- The **four discriminated levels** and their tag constants — what `*(int*)node` means at each level, and which assert gates each descent.
- The **node header framing** on the wire: `0xD9 <type-id> <sub-id> Nat(field-count)`, the per-class triple, and how `KLRFile`'s `0xD9F703` is the *container's* tag-number rather than three literal lead bytes.
- The **three universal operand records** — `TensorRef` (memory-space tag → access vs. HBM path), `Access` (AP-form tag), `Immediate` (`int`/`float` tag) — pinned from their consumer bodies.
- The **shared-pointer object model**: every node is `Ptr<T> = shared_ptr<T>` (16 bytes), every field read ref-count-bracketed, the tag always at `node+0`.

| | |
|---|---|
| **Subject binary** | `nki_klr_sim` (stripped; statically links `translate_nki_ast_to_bir` + `klr::*` deserializers) |
| **IR level** | KLR — NKI's lowered IR, one level above `bir::Instruction` |
| **Wire format** | Canonical CBOR (RFC 8949): node = tag(major 6) → array(major 4) of fields |
| **Container deserializer** | `sub_53A210` — `KLRFile` reader; xrefs the `Expecting KLRFile:(217,247,3)` assert |
| **Node header reader** | `sub_567F10` (@`0x567f10`) — `fread` 3 bytes, require `[0]==0xD9`, then `Nat` count |
| **`Nat` / array reader** | `sub_567DD0` (@`0x567dd0`) — CBOR header decode (owned by [7.32](klr-extendedinst-cbor.md)) |
| **Roster size** | **87** record classes (199 total `klr::` RTTI names incl. wrappers); 86 distinct type-id bytes |
| **KLRFile magic** | tag-number `0xD9F7` `=(217<<8)\|247`, arity `3` — i.e. tuple `(217,247,3)` |
| **Driver** | `sub_4D2820` — `fopen(file,"r")` → header → meta → contents; asserts `Contents::Tag::lnc` |

---

## 1. The shared-pointer object model

Every `klr::*` node is handled as `Ptr<T> = std::shared_ptr<T>` — a 16-byte `{raw_ptr, control_block}` pair. The decompiled bodies make this unmistakable: every field touch is bracketed by a ref-count bump on the control block, which appears as the recurring `_libc_single_threaded ? ++*(refcount) : _InterlockedAdd(refcount, 1)` idiom around each read (e.g. `sub_4F4970` lines 18–24). A reimplementer can ignore the bumps for layout purposes — they are noise around the real access — but they confirm that the object pointer is the *first* word of the `Ptr` and the control block the second.

The **discriminant convention is uniform**: a node's TAG is the first dword, `*(int*)node`. This holds at every level of the descent and for every operand record. The codegen always reads `*(int*)*Ptr` (deref the shared_ptr, then load `+0`) before branching — see `sub_50EDA0` line 253 (`switch(*(DWORD*)*a2)`), `sub_4F6950` line 43 (`*(DWORD*)a3->[0]`), `sub_4F4970` line 14 (`**a2`). [CONFIRMED — three independent bodies.]

> **NOTE —** the in-memory variant tag (`*(int*)node`, e.g. `Operator::Tag::cmpBranch == 47`) is a **different number** from the on-wire serialization type-id ([§3](#3-the-type-id-roster-87-records), e.g. `CmpBranch` ser type-id `193`). The first is the C++ `std::variant` index; the second is the CBOR tag byte. Do not conflate them — `NcMatMul` is in-memory Operator-tag `0x16`=22 but wire type-id `183`. [CONFIRMED — §7 of the working notes; cross-checked against `sub_50EDA0` and the `Expecting NcMatMul:(183,0,9)` string.]

---

## 2. The four-level tagged-union descent

The deserializer hands `nki_klr_sim` a `KLRFile`, and the driver walks down through four sealed sum types to reach a leaf op. Each level is a discriminated union whose discriminant is the `int` at `+0` of the carrier; each descent step is guarded by a literal assert string read verbatim from `.rodata`.

### The descent

```text
KLRFile                                       sub_53A210  (magic 0xD9F7 + arity 3)
  └─ KLRMetaData (235,0,1)                     sub_53A640
  └─ Contents   (tagged union)                assert: contents->tag == klr::Contents::Tag::lnc
       └─ LncKernel (108,0,7)                  one per logical neuron-core → list<Stmt>
            └─ Stmt  (tagged union)            assert: lastStmt->tag == klr::Stmt::Tag::oper
                 └─ StmtOperWrapper → Operator (tagged union, ~60 arms)
                      └─ Operator<Op>Wrapper → <Op>   assert: op->tag == Operator::Tag::cmpBranch
                           operands: vector<Ptr<TensorRef>> + scalar/imm attrs
```

### The discriminant levels

Five discriminated unions appear in the descent and operand graph. Each is a sealed set of `Wrapper` arms whose tag lives at carrier `+0`.

| Union | Discriminant tags (named) | Carrier / evidence | Confidence |
|---|---|---|---|
| `klr::Contents` | `lnc` (the `LncKernel` arm); `Wrapper` RTTI also names `hlo`/`kernel`/`nki`/`python` arms | driver `sub_4D2820:624,673` assert `contents->tag == klr::Contents::Tag::lnc` | CONFIRMED (lnc), STRONG (others, from `Contents{Lnc,Nki,Hlo,Kernel,Python}Wrapper` RTTI) |
| `klr::Stmt` | `oper` (the `StmtOperWrapper` arm) | `sub_512130:515` assert `lastStmt->tag == klr::Stmt::Tag::oper` | CONFIRMED |
| `klr::Operator` | ~60 arms, one per leaf op; `cmpBranch == 47` (`0x2F`) | `sub_512130:531` assert; `sub_50EDA0` switch = 60 cases | CONFIRMED |
| `klr::ReplicaGroup` | `literal == 3`, plus `named`/`unspecified` arms | `sub_500990:136`, `sub_4FF840:110` assert `…Tag::literal && "guaranteed by frontend API"` | CONFIRMED (literal=3), INFERRED (named/unspecified ordinals) |
| `klr::TensorRef` | `1` = sbuf/psum access path, `4` = HBM path | `sub_4F6950` ([§4](#4-tensorref--the-universal-operand)) | CONFIRMED |

The `StmtOperWrapper → Operator → Operator<Op>Wrapper → <Op>` chain is the **wrapper pattern**: each leaf op `klr::<Op>` is boxed by an `Operator<Op>Wrapper` variant arm, and `codegenOperator` (`sub_50EDA0`) unboxes via the variant tag → a thunk → `codegenNc<Op>(Ptr<klr::<Op>>)`. The rationale and the full Operator→leaf routing table live in [7.21](klir-codegen-dispatch.md#3-the-master-routing-table--kloperator-kind--wrapper--leaf); this page documents the *records*, not the dispatch.

> **GOTCHA —** the brief calls this a "four-level tree". It is four *discriminated* levels (`Contents`, `Stmt`, `Operator`, leaf), but the carrier nesting is deeper: a `StmtOperWrapper` sits between `Stmt` and `Operator`, and an `Operator<Op>Wrapper` between `Operator` and `<Op>`. A reimplementer who models only four physical objects will mis-place the two wrapper boxes and read fields at the wrong offset. [CONFIRMED — RTTI `StmtOperWrapper`, `Operator…Wrapper`.]

---

## 3. The type-id roster (87 records)

Every deserializable `klr::*` record class carries a header assert of the form `Expecting <Class>:(type-id, sub-id, field-count)`, emitted before the class reads its body. Mining `nki_klr_sim` for this string family yields exactly **87** distinct rows; a second, byte-identical family (`Could not find tag, expecting <Class>:type-id,sub-id`) names the **same 87** classes (verified by set-diff). These two families together are the authoritative wire schema.

> **CORRECTION (KLR-1) —** the working notes and the task brief cite an **"88-entry"** type-id table. The binary carries **87** distinct `Expecting` strings across all three Python ABIs (cp310/311/312 all = 87). There is no 88th record. The "88" appears to be an off-by-one in the source notes; pin to **87**. Of the 87, there are **86 distinct primary type-id bytes** — `Activate2(217,0,14)` and `KLRFile(217,247,3)` share byte `217`, disambiguated by the sub-id (`0` vs `247`). [CONFIRMED — `strings | rg '^Expecting ' | sort -u | wc -l` = 87 on all three binaries.]

The roster is a structured catalog, not a data dump — each row's type-id is the CBOR tag byte that selects it on the wire, the sub-id is `0` for every record except the `KLRFile` container, and the field-count is the CBOR array arity the deserializer enforces exactly. Rows are grouped by role; every `(type-id, sub-id, field-count)` triple is verbatim from the `.rodata` `Expecting` string.

### Container & structural records

| Class | type-id | sub | fields | Role |
|---|---|---|---|---|
| `KLRFile` | 217 | **247** | 3 | The outer document frame (the only non-zero sub-id; see [§5](#5-the-klrfile-container-frame)) |
| `KLRMetaData` | 235 | 0 | 1 | Document metadata, read right after the frame |
| `Pos` | 100 | 0 | 5 | Source position |
| `Block` | 104 | 0 | 3 | Statement block |
| `Kernel` | 105 | 0 | 4 | Generic kernel container |
| `SharedConstantFile` | 106 | 0 | 2 | Shared-constant reference |
| `Edges` | 107 | 0 | 2 | Graph edges helper |
| `LncKernel` | 108 | 0 | 7 | The logical-neuron-core kernel (the `Contents::lnc` payload) |

### Operand / shape records

| Class | type-id | sub | fields | Role |
|---|---|---|---|---|
| `Shape` | 112 | 0 | 2 | Tensor shape |
| `Address` | 113 | 0 | 7 | Memory address descriptor |
| `TensorName` | 114 | 0 | 8 | Named tensor (HBM operand) |
| `Slice` | 115 | 0 | 4 | Slice descriptor |
| `AccessBasic` | 118 | 0 | 3 | Basic access (stride/num AP) |
| `APPair` | 119 | 0 | 3 | One `{stride,num}` access-pattern pair |
| `AccessPattern` | 120 | 0 | 6 | Multi-dim access pattern |
| `BirAccessPattern` | 124 | 0 | 7 | Pre-lowered BIR access pattern |
| `TensorHbm` | 126 | 0 | 4 | HBM-resident tensor |
| `TensorSram` | 127 | 0 | 6 | SRAM-resident tensor |
| `DataPattern` | 134 | 0 | 3 | Data-movement pattern |

### Leaf operator records

This is the body of the roster — the ~60 leaf ops the codegen consumes. (The Operator-variant in-memory tags differ; see [7.21](klir-codegen-dispatch.md#3-the-master-routing-table--kloperator-kind--wrapper--leaf).)

| Class | type-id | fields | Class | type-id | fields |
|---|---|---|---|---|---|
| `Dropout` | 147 | 5 | `TensorScalar` | 180 | 9 |
| `Activate` | 148 | 7 | `TensorScalarReduce` | 181 | 8 |
| `NcActivate` | 149 | 9 | `TensorTensor` | 182 | 6 |
| `AffineSelect` | 150 | 5 | `NcMatMul` | 183 | 9 |
| `NcAffineSelect` | 151 | 6 | `TensorPartitionReduce` | 184 | 4 |
| `DmaCopy` | 152 | 5 | `SelectReduce` | 185 | 9 |
| `NcDmaCopy` | 153 | 7 | `SequenceBounds` | 186 | 3 |
| `DmaTranspose` | 154 | 6 | `SendRecv` | 187 | 6 |
| `Transpose` | 155 | 4 | `SendRecvCompute` | 188 | 6 |
| `LoadMaskRegister` | 156 | 1 | `TensorLoad` | 190 | 2 |
| `Shuffle` | 157 | 4 | `TensorStore` | 191 | 2 |
| `MemSet` | 158 | 4 | `RegisterMove` | 192 | 2 |
| `Iota` | 159 | 3 | `CmpBranch` | 193 | 6 |
| `LoadStationary` | 160 | 2 | `RegisterAluOp` | 194 | 4 |
| `MatMul` | 161 | 2 | `QuantizeMX` | 195 | 3 |
| `LocalGather` | 162 | 4 | `MatMulMX` | 196 | 7 |
| `NcLocalGather` | 163 | 5 | `DmaCompute` | 197 | 4 |
| `RangeSelect` | 164 | 10 | `CollectiveOp` | 199 | 8 |
| `NcRangeSelect` | 165 | 12 | `RankId` | 200 | 1 |
| `ScalarTensorTensor` | 166 | 8 | `CurrentProcessingRankId` | 201 | 5 |
| `NcScalarTensorTensor` | 167 | 8 | `Send` | 202 | 3 |
| `CopyPredicated` | 168 | 5 | `Recv` | 203 | 4 |
| `TensorTensorScan` | 169 | 9 | `CoreBarrier` | 204 | 3 |
| `MatchValueLoad` | 170 | 1 | `Rng` | 205 | 2 |
| `FindIndex8` | 171 | 4 | `Rand2` | 206 | 3 |
| `MatchReplace8` | 172 | 6 | `RandGetState` | 207 | 2 |
| `Max8` | 173 | 3 | `SetRngSeed` | 208 | 1 |
| `BatchNormAggregate` | 174 | 3 | `RandSetState` | 209 | 2 |
| `BatchNormStats` | 175 | 3 | `ExtendedInst` | 210 | 6 |
| `Reciprocal` | 176 | 3 | `TensorScalarCumulative` | 211 | 9 |
| `Copy` | 177 | 3 | `NcNGather` | 212 | 4 |
| `NcCopy` | 178 | 4 | `NonzeroWithCount` | 213 | 4 |
| `TensorReduce` | 179 | 7 | `DevicePrint` | 215 | 3 |
| | | | `Exponential` | 216 | 6 |
| | | | `Activate2` | 217 | 14 |

> **NOTE —** `ExtendedInst` is wire type-id **210**, matching [7.32](klr-extendedinst-cbor.md)'s CBOR tag-210 exactly (`0xD9 0xD2 0x00 0x86`). `Activate2` is the 14-field extended activation — the largest leaf — and is the record that shares byte `217` with the container. The 199 total `klr::` RTTI names (`strings | rg N3klr | sort -u` = 199) include these 87 deserializable records plus their `Operator<Op>Wrapper`, `TensorRef*Wrapper`, `Immediate*Wrapper`, `ReplicaGroup*Wrapper`, and `Contents*Wrapper` variant arms — the wrappers are not separately serialized records, so they carry no `Expecting` string. [CONFIRMED — counts.]

---

## 4. TensorRef — the universal operand

`TensorRef` is the operand record every op references. Its consumer is `codegenTensorRefImpl` (`sub_4F6950`), with a thin wrapper `codegenTensorRef` (`sub_4F6B20`, `allowDynamic=0`). The deref'd `Ptr<TensorRef>` carries the memory-space tag at `+0`.

### The memory-space dispatch

```c
function codegenTensorRefImpl(Ptr<TensorRef> tr, bool allowDynamic):   // sub_4F6950
    // reject dynamic AP unless the caller opts in (NcCopy does)
    if isDynamicAccess(tr) && !allowDynamic:                  // sub_4F4750
        throw runtime_error("Dynamic Access is not allowed in the instruction.")

    int tag = *(int*)tr;                                      // tag at +0 (line 43)
    if tag == 1:                                              // sbuf / psum access path
        access = tr->ap         // Ptr<Access> at TensorRef+8
        ap     = tr->[+16]      // the AP sub-object
        return codegenAccess(access, ap)                      // sub_4F6810
    else if tag == 4:                                         // HBM tensor path
        return codegenHbmTensor(tr)                           // sub_4F3050 — TensorName/Shape/Address
    else:
        assert(false && "Unsupported tensorRef type encountered!")  // cpp:0x135
```

Tags `1` and `4` are the only two handled; everything else aborts at `klir_to_bir_codegen.cpp:0x135`. The five memory-space arms in RTTI — `TensorRefSbufWrapper`, `TensorRefPsumWrapper`, `TensorRefHbmWrapper`, `TensorRefRegisterWrapper`, `TensorRefAbstractWrapper` — collapse onto these two paths at codegen time (sbuf and psum both reach tag `1`; the `to_string` prefixes `"TensorRefSbufWrapper("`/`"TensorRefPsumWrapper("` are present in `.rodata`). [CONFIRMED — `sub_4F6950` body + RTTI.]

### The Access sub-object

The tag-`1` path hands `Access` (`TensorRef+8`) and the AP (`TensorRef+16`) to `codegenAccess` (`sub_4F6810`), which switches on the Access tag at `+0`:

| Access tag | Form | Result |
|---|---|---|
| `1` | `AccessBasic` / `AccessPattern` | stride/num AP → `APPair` vector |
| `4` | `BirAccessPattern` | a pre-lowered BIR access pattern |
| else | — | `throw "Unsupported access type encountered <n>"` (`sub_4F6810:38`) |

`isDynamicAccess` (`sub_4F4750`) returns true iff the access is tag `1` **and** its offset is a register/dynamic value rather than a constant; ordinary ops reject it, and `NcCopy` is the one op that passes `allowDynamic=1` to route `TensorCopy` → `TensorCopyDynamicSrc`/`…DynamicDst`. [CONFIRMED — `sub_4F6950` line 26 gate, `sub_50B110` routing.]

### TensorRef composed layout

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `tag` | `+0x00` | `int` | memory space: `1`=sbuf/psum-access, `4`=hbm | CONFIRMED |
| `ap` | `+0x08` | `Ptr<Access>` | the access-pattern sub-object (tag-`1` path) | CONFIRMED |
| `tensor`/`name` | `+0x10` | `Ptr<…>` | `TensorName` for HBM; sbuf storage handle otherwise | STRONG |

The HBM arm (`sub_4F3050`) emits the BIR arg with `TensorClass=6`, base-partition `8`, elem-size `2`, built from the `TensorName` string and the `Shape`/`Address` sub-objects. [CONFIRMED — `sub_4F3050` constants.]

---

## 5. The KLRFile container frame

The outer document frame is read by `sub_53A210`. It is the only record whose sub-id byte is non-zero, and the only place the brief's `0xD9F703` token appears.

### The deserializer

```c
function deserialize_KLRFile(out, FILE *s):              // sub_53A210
    if !read_node_header(s, &type_id, &sub_id, &count):  // sub_567090 → sub_567F10
        throw runtime_error("Could not find tag, expecting KLRFile:217,247")
    if type_id != 0xD9 || sub_id != 0xF7 || count != 3:  // 0xD9=217, 0xF7=247  (lines 59, 21-24 disasm)
        throw runtime_error("Expecting KLRFile:(217,247,3) ... got:(...)")

    f = operator new(0x20)            // 32-byte shared_ptr inplace control block (line 109)
    f->refcount = {1,1}; f->vtbl = off_974D58
    f->field0 = read_nat(s)           // sub_567130 → "expecting Nat"
    f->field1 = read_nat(s)
    f->field2 = read_nat(s)           // the 3 KLRFile fields (lines 115-117)
    *out = f
```

The header reader `sub_567F10` (via thunk `sub_567090`) does the actual byte work — `fread(ptr,1,3,s)`, then **`if ptr[0] != 0xD9 return 0`** (disasm `0x567f43: cmp [ptr], 0xD9`), then `*type_id = ptr[1]`, `*sub_id = ptr[2]`, and reads the array arity as a `Nat` capped at `0x17` (23). So the *literal* on-wire bytes of the `KLRFile` frame are **`0xD9 0xD9 0xF7`** + `Nat(3)`: a CBOR major-6 tag marker, then the two-byte tag-number `0xD9F7`, then a CBOR array of three. [CONFIRMED — `sub_567F10` body + `sub_53A210` disasm.]

> **QUIRK — `0xD9F703` is the tag-*number*, not the first three wire bytes.** The brief's `0xD9F703` is the `(type-id=217, sub-id=247, arity=3)` tuple rendered hi-lo-count: the CBOR tag-number is `(217<<8) | 247 = 0xD9F7 = 55799`, and `3` is the array arity. On the wire there is a **fourth, leading** byte — the `0xD9` CBOR major-6 "tag, 2-byte argument follows" marker — so the actual prefix is `0xD9 0xD9 0xF7`. This mirrors [7.32](klr-extendedinst-cbor.md#2-the-cbor-encoding--tag-210)'s `ExtendedInst` frame `0xD9 0xD2 0x00 0x86` (marker + tag-number `0xD200` + array(6)); `KLRFile` is the same shape with tag-number `0xD9F7` and arity `3`. A reimplementer who writes the literal bytes `D9 F7 03` (dropping the marker) produces an unparseable stream. [CONFIRMED.]

### The driver

`sub_4D2820` is the `nki_klr_sim` entry that drives the whole descent: `fopen(*argv, "r")` (line 148) → `deserialize_KLRFile` → `KLRMetaData` (`sub_53A640`) → `Contents`, then asserts `contents->tag == klr::Contents::Tag::lnc` (lines 624, 673), extracts the `LncKernel`, and calls `KlirToBirCodegen::codegenLncKernel`. A KLR file whose `Contents` is any arm other than `lnc` aborts here — `nki_klr_sim` only simulates the LncKernel payload. [CONFIRMED — driver body + assert strings.]

---

## 6. Immediate — the scalar/constant operand

`Immediate` is the scalar-constant operand record. Its consumer `codegenImmediate` (`sub_4F4970`) is a clean, complete body — the smallest fully-pinnable record on the page — and returns `std::variant<float,int>`.

```c
function codegenImmediate(Ptr<Immediate> imm):    // sub_4F4970
    int tag = *(int*)imm;                          // tag at +0 (line 14)
    if tag == 4:                                    // FLOAT
        float v = read_float(imm)                   // sub_4F2070, payload at imm+4
        return variant{ v, discriminant=0 }         // BYTE4 = 0
    else if tag == 3:                               // INT
        int v = read_int(imm)                       // sub_4F2080, payload at imm+4
        return variant{ v, discriminant=1 }         // BYTE4 = 1
    else:
        assert(false && "Unsupported immediate type encountered")  // cpp:0x1A9
```

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `tag` | `+0x00` | `int` | `3`=INT (`ImmediateIntWrapper`), `4`=FLOAT (`ImmediateFloatWrapper`) | CONFIRMED |
| `value` | `+0x04` | `int`/`float` | 4-byte payload, read per-tag; both arms read `*(T*)(imm+4)` | CONFIRMED |

The variant's returned discriminant is `0` for the float arm, `1` for the int arm. The RTTI roster also lists `ImmediatePointerWrapper` and `ImmediateRegisterWrapper`, but `codegenImmediate` handles **only** `int`/`float` — pointer and register immediates are resolved elsewhere (e.g. an op's scale/bias operand, like `NcActivate`'s tagged `ScaleOperand`), never through this const-folding path. [CONFIRMED — `sub_4F4970` body + the `cpp:0x1A9` assert; pointer/register handling INFERRED from the absent cases.]

---

## 7. Worked node layouts

The codegen bodies reveal each leaf's field offsets directly. Two are reproduced here as representative — the multi-operand matmul and the routed copy — both pinned to their consumer bodies. The remaining ~60 follow the same `[+0] tag, [+10/+20/…] Ptr<TensorRef> operands, [+30…] scalar attrs` skeleton; their routing tags are tabulated in [7.21](klir-codegen-dispatch.md#3-the-master-routing-table--kloperator-kind--wrapper--leaf).

### `NcMatMul` (183,0,9) — `codegenNcMatMul` `sub_4F7580`

Lowers to `bir::InstMatmult` (the `"matmult_"` name at `sub_4F7580:92`). The two list-size asserts pin the tile geometry.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `operand0` | `+0x00` | `Ptr<TensorRef>` | stationary input |
| `operand1` | `+0x10` | `Ptr<TensorRef>` | moving input |
| `operand2` | `+0x20` | `Ptr<TensorRef>` | output |
| `perfModeAttr{0,1,2}` | `+0x48..0x4A` | `byte`×3 | matmul boolean attrs (transpose / perf-mode / quantize) |
| `tilePositionVector` | `+0x38` | `std::list<int>` | asserted `size()==2` (`sub_4F7580:260`) → `{x,y}` |
| `tileSizeVector` | `+0x50` | `std::list<int>` | asserted `size()==2` (`sub_4F7580:185`) → `{x,y}` |
| `calcMode` | `+0x68` | `int` | `==2` ⇒ sets `InstMatmult+720` (`*((DWORD*)*a2+26)==2`, line 145) |

[CONFIRMED — `tileSizeVector.size()==2` / `tilePositionVector.size()==2` asserts and the `node[26]==2` test, all read off `sub_4F7580`.]

### `NcCopy` (178,0,4) — `codegenNcCopy` `sub_50B110`

Two `TensorRef` operands, then routed to one of three BIR copy variants by whether src/dst carry a dynamic access:

```c
function codegenNcCopy(Ptr<NcCopy> n):        // sub_50B110
    dst = n->[+0x00]; src = n->[+0x10]
    if  isDynamicAccess(src):  emit InstTensorCopyDynamicSrc   // "TensorCopyDynamicSrc_"
    elif isDynamicAccess(dst): emit InstTensorCopyDynamicDst   // "TensorCopyDynamicDst_"
    else:                      emit InstTensorCopy             // "TensorCopy_"
    dtype = n->[+0x28]; codegenDtype(dtype) -> Inst+144        // sub_4F20E0
```

`NcCopy` is the op that *permits* dynamic access (it calls `codegenTensorRef` with `allowDynamic=1`); every other op rejects it via the `"Dynamic Access is not allowed"` throw in [§4](#4-tensorref--the-universal-operand). [CONFIRMED — `sub_50B110` routing + the three BIR name strings.]

---

## 8. klr → bir enum translation

The codegen maps klr enum ordinals to bir enum ordinals through small index LUTs of the form `return LUT[klr_val - base]`, all in `nki_klr_sim` `.rodata`. The `klr_val - 1` / `klr_val - 2` base offset means klr enum `0` is an "unset/none" sentinel that never indexes the table.

| Helper | Function | LUT | base | Maps |
|---|---|---|---|---|
| `codegenAluOp` | `sub_4F20B0` | `0x8AE500` | 1 | klr `AluOp` → bir `AluOpType` |
| `codegenDtype` | `sub_4F20E0` | `0x8AE4E0` | 2 | klr `Dtype` → `bir::Dtype` |
| `codegenActivationFunc` | `sub_4F2030` | `0x8AE5E0` | 1 | klr `ActFn` → bir activation enum |
| `codegenDtype` (variant) | `sub_4F2100` | — | 1 | `NcActivate` dtype/round form |

These are the klr-side mirror of the BIR enum cross-walk. [CONFIRMED — function addresses + LUT base offsets from the working notes; cross-checked against the `sub_4F20xx` cluster.]

---

## 9. Adversarial self-verification

The five strongest claims, re-checked against the binary:

| Claim | Verdict | Evidence |
|---|---|---|
| The type-id roster is **87** records (not 88) | **CONFIRMED — corrects the brief** | `strings nki_klr_sim \| rg '^Expecting ' \| sort -u \| wc -l` = **87** on cp310/311/312; second `Could not find tag` family also 87, identical name set (set-diff empty). The "88" is a notes off-by-one. |
| Four discriminated descent levels with the named tags | **CONFIRMED** | `Contents::Tag::lnc` (driver `sub_4D2820:624`), `Stmt::Tag::oper` (`sub_512130:515`), `Operator::Tag::cmpBranch`==47 (`sub_512130:531`; switch `sub_50EDA0`=60 cases), `ReplicaGroup::Tag::literal`==3 (`sub_500990:136`) |
| `KLRFile` magic = tag-number `0xD9F7` + arity `3` | **CONFIRMED** | `Expecting KLRFile:(217,247,3)` (`0x8740fd`); `sub_53A210` disasm `cmp [..],0D9h` / `cmp [..],0F7h`; `sub_567F10` requires `ptr[0]==0xD9`. `0xD9F703` is the *tuple*, the literal prefix is `0xD9 0xD9 0xF7`. |
| `TensorRef` tag {1=access, 4=hbm}; `Access` {1,4}; `Immediate` {3=int, 4=float} | **CONFIRMED** | `sub_4F6950` (cpp:0x135), `sub_4F6810` ("Unsupported access type"), `sub_4F4970` (cpp:0x1A9) — three independent assert-bearing bodies |
| In-memory variant tag ≠ on-wire type-id | **CONFIRMED** | `NcMatMul` Operator-tag `0x16`=22 vs wire `183`; `cmpBranch` tag `47` vs wire `193`; `switch(*(int*)*op)` in `sub_50EDA0` is the variant index, the `Expecting` byte is the wire id |

### Verification ceiling — what is *not* pinned

- **`Contents` arm ordinals other than `lnc`.** Only `lnc` is asserted in this binary (the others abort before codegen). The `hlo`/`kernel`/`nki`/`python` arms are named by `Contents*Wrapper` RTTI but their tag *values* are not read off any body here. [INFERRED.]
- **`ReplicaGroup::Tag` values for `named`/`unspecified`.** Only `literal==3` is asserted; the other two arms exist in RTTI but their ordinals are not pinned. [INFERRED.]
- **Per-field semantics inside each leaf's CBOR array.** The roster pins each record's *arity* (the field count the deserializer enforces) and §7 pins two records' *offsets*; the field-by-field meaning of the other ~60 leaves is not exhaustively traced here — it is recoverable from each `codegenNc<Op>` body the same way §7 recovers `NcMatMul`/`NcCopy`. [Method shown, remainder OPEN.]
- **Whether the in-memory offset (`+0x08` `ap`, `+0x10` name) and the on-wire field *order* coincide.** The page pins both, but a producer could serialize fields in a different order than the in-memory layout; that round-trip ordering is owned by [7.32](klr-extendedinst-cbor.md)'s serializer analysis, not re-derived here. [Deferred to 7.32.]

---

## Cross-References

- [KlirToBirCodegen Dispatch Core & the Master Routing Table](klir-codegen-dispatch.md) — 7.21; the 60-case `codegenOperator` switch and the Operator→leaf routing that **consumes** these records
- [klr::ExtendedInst & the CBOR Wire Format](klr-extendedinst-cbor.md) — 7.32; owns the CBOR codec primitives (`Nat` reader `sub_567DD0`, tag reader/writer), the tag-210 frame, and the `Operator` variant kind-70 encoding this page's `ExtendedInst` row points to
- [Container Model](container-model.md) — the BIR-side container the lowered KLR feeds into
- [Memory Location](memory-location.md) — the BIR memory-space model `TensorRef` lowers onto
- [Value Model](value-model.md) — the BIR value/operand model that `Immediate` and `TensorRef` become
