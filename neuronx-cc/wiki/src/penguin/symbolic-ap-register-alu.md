# Symbolic-AP → Register-ALU Materialization

> **Version pin —** neuronx-cc `2.24.5133.0+58f8de22`, cp310 wheel. The bodies described
> here live in `libwalrus.so` (the heavy back-end the `walrus_driver` ELF declares
> `NEEDED`; addresses are libwalrus virtual addresses, `VA == file-offset` for `.text`
> / `.rodata`). Sibling strands: assembleDynamicInfo / extractDynamicApToStandAlone
> (KlirToBirCodegen), the LowerAP skeleton, InstRegisterAlu (IT73) / InstRegisterMove
> (IT74), the AccessPattern hierarchy, and the pelican `Expr` / `QuasiAffineExpr` model.

## Abstract

A Penguin/BIR instruction does not address SBUF or PSUM with a fixed byte offset when the
loop trip-counts or gather indices are only known at run time. Instead its operand is a
**symbolic access pattern** — a `bir::SymbolicAccessPattern` whose tile and block
addresses are not integers but `QuasiAffineExpr` trees over the loop induction variables.
Before the back-end can colour registers and emit a descriptor, every such symbolic
address must be turned into an actual sequence of scalar-engine ALU instructions that
*compute* the address into a hardware register, and the operand must be replaced by a
**register access pattern** (`bir::RegisterAccessPattern`) that names those registers.
That kind-2 → kind-3 conversion is what this page reconstructs.

The work is split across two passes that share one emitter vocabulary. **`LowerAP`**
(pass slot `lower_ap`, after per-engine split and before the colouring allocator) owns
`convertSymAP`, which lowers a `SymbolicAccessPattern` whose offsets are affine forms over
*loop axes*: it mints up to seven virtual registers on the consuming instruction's engine,
walks each affine expression into a sum-of-products register-ALU chain via
`lowerAffineExpr`, delinearizes the flat element offset into the engine's
`(partition, free)` 2-D byte space, optionally loads a 64-bit runtime base through a
`-ADDR` pointer tensor, and builds the kind-3 `RegisterAccessPattern`. The later
**`ExpandInstLate`** pass owns `ExpandDynamicAPInfo` / `ExpandDynamicAPInfoImpl`, which
materializes the *residual* `DynamicAPINFO` runtime offset carried by a
`PhysicalAccessPattern` (the gather/scatter and dynamic-DMA flavour) into the same
`InstRegisterAlu` arithmetic — this time CSE'ing identical `pelican::Expr` sub-trees
through a hash cache. A third helper, **`rewireDynamicAPRegisters`**, re-binds the
`regref` pointers embedded in the dynamic expressions when registers are later renamed.

Both passes funnel into one hardware-level primitive: `bir::InstRegisterAlu` (IT73),
`dst = src ⟨AluOp⟩ imm` with the engine stamped at `inst+0x90` and the ALU opcode at
`inst+0xf0`. The recovered opcode ordinals — pinned by immediate constants in the
delinearization sequence — are **add = 4, mult = 6, divide = 7, mod = 0x1B (27),
bypass = 0**.

## At a glance

| | |
|---|---|
| **Front (loop-axis affine)** | `LowerAP::convertSymAP(bir::SymbolicAccessPattern*)` @ `0x11b7f80` (8270 B) |
| **Affine leaf** | `neuronxcc::backend::lowerAffineExpr(Inst*, QuasiAffineExpr*, Reg* dst, Reg* tmp, BB*)` @ `0x109b5b0` |
| **Residual driver** | `ExpandInstLateImpl::ExpandDynamicAPInfo(bir::Instruction&)` @ `0xcd4dc0` |
| **Residual expander** | `ExpandInstLateImpl::ExpandDynamicAPInfoImpl(PhysicalAccessPattern&, int&, vector<Instruction*>&)` @ `0xccd030` (~31 KB) |
| **Register rebind** | `rewireDynamicAPRegisters(PhysicalAccessPattern&, Function&, unordered_map<string,…>&)` @ `0xef91d0` |
| **Emit target** | `bir::InstRegisterAlu` (IT73): engine @ `+0x90`, AluOp @ `+0xf0` |
| **AluOp ordinals** | `add=4`, `mult=6`, `divide=7`, `mod=0x1B`, `bypass=0` |
| **Kind tag** | symbolic (kind-2) ↔ register (kind-3); discriminated by the backing memloc tag + the C++ AP class |
| **Drivers** | `LowerAP::run` `0x11ba970` → `convertSymAPtoRegAP` `0x11ba8b0` → `convertSymAPForInst` `0x11b9fd0` → `convertSymAP` |

> **GROUNDING —** The libwalrus.so / libBIR.so shared objects are *not* present in this
> repo's extracted tree (the `walrus_driver` ELF links them as `NEEDED` but only the
> driver shell ships in the corpus, `.text` `0x43e9b0..0x489402`). Every address, offset,
> opcode immediate, and rodata string below is carried from the IDA database of
> `libwalrus.so` (cp310) on which the two backing analyses were performed; they are
> binary-derived, not source-derived. Where a claim could only be reasoned structurally
> it is tagged INFERRED.

---

## 1. What "kind-2 → kind-3" means

There are three distinct C++ access-pattern classes, and the address representation is
the thing that changes between them:

| Class | Address representation | Role |
|---|---|---|
| `bir::SymbolicAccessPattern` (kind-2, "symbolic") | per-axis `QuasiAffineExpr` over loop induction variables (`getTileAddrs()` / `getBlockAddrs()`) | what the front-end / `assembleDynamicInfo` emits for dynamic shapes |
| `bir::PhysicalAccessPattern` (kind-1, may carry a `DynamicAPINFO`) | concrete physical pattern, but the *offset* can still be a runtime `DynamicAPINFO` expr at `pap+0x1d8` | the gather/scatter and dynamic-DMA residual flavour |
| `bir::RegisterAccessPattern` (kind-3, "register") | concrete `bir::Register*` in `regref` / `reg_ap_offset` / `reg_sub_tensor_id` | the lowered form the colouring allocator and the ADDR4 encoder consume |

A `SymbolicAccessPattern` (sizeof `0x1B8`, `ArgumentKind == 2`, type-name `symbolic_ap` /
`symbolic_pwap`) holds its addresses as `pelican::Expr` trees, reached through two virtual
getters:

```text
getTileAddrs()   →  the TILE (inner / within-tile)  address QuasiAffineExpr
getBlockAddrs()  →  the BLOCK (outer / sub-tensor)   address QuasiAffineExpr
```

Each `QuasiAffineExpr` is 32 bytes: `RefPtr<Expr>` at `+0x0` plus a `SmallVector<LoopAxis*>`
at `+0x8` — a **simple affine form** `c + Σ_axis coef_axis · IV_axis` over the loop
induction variables, with the legality of any nested div/mod already decided upstream.

The conversion is then exact: take the affine offset, run `lowerAffineExpr` (§2) to emit a
register-ALU program computing it, then build a `RegisterAccessPattern` whose **pattern
pairs (strides / counts) are copied verbatim** but whose **offset and location become
register-valued**. The static geometry stays compile-time; only the dynamic offset is
lowered to registers.

> **CORRECTION — the regref offset is `+0xE8`, not `+0x40`.** One of the two backing
> analyses conflated two different `regref` fields. The kind-3 `RegisterAccessPattern`
> (sizeof `0x118`) stores its registers at object offsets **`regref` `+0xE8`,
> `reg_ap_offset` `+0xF0`, `reg_sub_tensor_id` `+0xF8`** (witnessed by the `setRegLocation`
> / `setRegAPOffset` stores and the `*(regAP+248)` write). The `+0x40` `regref` belongs to
> the *`bir::BirIntRuntimeValue` Expr-node* inside a dynamic expression tree — the field
> `rewireDynamicAPRegisters` patches (§5). They are unrelated objects; do not merge them.

---

## 2. `lowerAffineExpr` — the affine-expr → register-ALU Horner walk

This is the leaf that turns **one** `QuasiAffineExpr` into a sequence of register-ALU
micro-ops computing it into a destination register. It is shared with the branch-predicate
lowering (`convertPredToBranch` materializes branch conditions the same way). Signature
(from the mangled name + disasm):

```c
void lowerAffineExpr(bir::Instruction* I, bir::QuasiAffineExpr* E,
                     bir::Register* dst, bir::Register* tmp, bir::BasicBlock* BB);
```

### Algorithm

```c
// lowerAffineExpr @ 0x109b5b0  (≈313-line body)
void lowerAffineExpr(Instruction* I, QuasiAffineExpr* E,
                     Register* dst, Register* tmp, BasicBlock* BB)
{
    // 0. Only flat affine forms reach here.  Div/mod/floordiv were normalised away
    //    upstream (delinearization legality decided by isLegalDelinearizedAddress).
    assert(E->isSimpleAffineExpr() &&             // @0x109b603
           "Only simple affine expr supported");  // utils.cpp:0x2D7  → __assert_fail

    EngineType eng = *(u32*)(I + 0x90);           // the consuming inst's engine

    // 1. CONSTANT TERM:  dst := c
    int64 c = sext32_64(E->getAxisCoef(/*axis=*/NULL));   // @0x109b621
    addRegMov(I, name, ValueUnion(c), dst, BB, eng);      // @0x109b677  → InstRegisterMove (IT74)

    // 2. PER-AXIS SUM-OF-PRODUCTS:  dst += coef_a · IV_a
    for (LoopAxis* a : E->axes /* SmallVector @E+0x8, count @E+0x10 */) {
        // 2a. Find the per-engine induction-variable COUNTER register for this axis.
        //     The LoopAxis carries an Rb_tree<EngineType, Register*> at axis+0x168;
        //     look up the entry for THIS engine (eng).
        Register* ivreg = a->engineRegMap.at(eng);        // map::at, throws on miss
        if (!ivreg)
            reportError("loop axis hasn't been assigned register "
                        "for induction variable!");        // rodata 0x1DD3400

        int64 coef = sext32_64(E->getAxisCoef(a));        // @0x109baa8

        // 2b. tmp := ivreg * coef        (InstRegisterAlu, AluOp mult = 6)
        addRegAluOneRegArgOneImm(I, name, /*AluOp=*/6, ivreg, ValueUnion(coef),
                                 /*dst=*/tmp, BB, eng);     // @0x109bb04   (edx = 6)

        // 2c. dst := dst + tmp           (InstRegisterAlu, AluOp add = 4)
        addRegAluTwoRegArg(I, name, /*AluOp=*/4, /*a=*/dst, /*b=*/tmp,
                           /*out=*/dst, BB);                // @0x109bb69   (edx = 4)
    }
    // dst now holds  c + Σ_axis coef_axis · IV_axis
}
```

### The affine-node → ALU-opcode table

This is the mapping a reimplementer needs. `lowerAffineExpr` handles only the flat case;
the table is small *because* div/mod never reach this leaf:

| AffineExpr node | Emitted op | AluOp | Emitter | Operands |
|---|---|---|---|---|
| constant term `c` | seed accumulator | — (move) | `addRegMov` | `dst := imm` (IT74 `InstRegisterMove`) |
| `coef · IV_axis` | scale | **6 = mult** | `addRegAluOneRegArgOneImm` | `tmp := ivreg · imm` |
| running accumulate | add | **4 = add** | `addRegAluTwoRegArg` | `dst := dst + tmp` |
| `div` / `mod` / `floordiv` | — | — | *not emitted here* | pre-normalised upstream; survival ⇒ `__assert_fail` |

> **NOTE — register threading.** A *single* shared `tmp` register holds every
> `coef·IV` product transiently; the `dst` accumulator is read-modify-written each axis.
> No fresh register is minted per axis, so only two registers are live at once — minimal
> pressure for the colouring allocator. The induction-variable registers themselves are
> **looked up, not minted** — they are produced earlier by the loop-lowering pass that
> owns the structured loop; `lowerAffineExpr` only emits arithmetic into the `dst`/`tmp`
> that `convertSymAP` (§3) handed it.

> **GOTCHA — div/mod are *not* a missing feature.** Because the dispatch asserts
> `isSimpleAffineExpr()` and the non-simple branch is assert-only, a div/mod that escapes
> the upstream `pelican::AffineExpr` simplifiers is a hard error, not a fallback path. The
> partition/free *split* that does need a divide is done explicitly by `convertSymAP`
> itself (§4), with literal opcode immediates — not by recursing into `lowerAffineExpr`.

---

## 3. `convertSymAP` — minting the register nursery

Before any arithmetic, `convertSymAP` allocates the working registers via
`Function::addRegister(name, EngineType, width=1)`, **all on the consuming instruction's
engine** (`engine = *(u32*)(inst+0x90)`, so the address arithmetic rides the same engine
as the instruction it feeds). Each register name encodes the full lineage so the allocator
and the encoder can trace a register back to its AP and role:

```text
"Reg_" + <reg-id> + "_" + <inst-name> + <SUFFIX> + <location-name>
```

The seven suffixes, in mint order (recovered from the string LEAs in the disasm):

| Suffix | Var | Role | Survives into kind-3 as |
|---|---|---|---|
| `_RegAPOffset_` | v90 | within-partition (free-axis) byte offset | `reg_ap_offset` (`+0xF0`) |
| `_RegSubTensorId_` | v79 | block / sub-tensor index (partition ordinal) | `reg_sub_tensor_id` (`+0xF8`) |
| `_RegTemp_` | v99 | scratch for the affine walks + the delinearization math | — (intermediate) |
| `_RegDramAddr0_` | v83 | low half of the 64-bit runtime base address | — |
| `_RegDramAddr1_` | v81 | high half of the 64-bit runtime base address | — |
| `_RegTemp1_` | v77 | second scratch (64-bit ALU carry / temp) | — |
| `_RegAPAddr_` | v104 | the **final computed address** register | `regref` (`+0xE8`) |

Only `RegAPOffset` / `RegSubTensorId` / `RegAPAddr` survive into the kind-3
`RegisterAccessPattern`; the rest are intermediates consumed by the ALU chain. (Evidence:
`addRegister` ×7 @ `0x11b83d0` / `85b8` / `878c` / `8ab0` / `8c80` / `8e50` / `92ef`, with
`edx = engine`, `ecx = width = 1`; suffix LEAs `aRegapoffset` `0x11b8284` … `aRegapaddr`
`0x11b91e0`, prefix `aReg_` `0x11b8403`.)

---

## 4. `convertSymAP` — the full materialization sequence

```c
// LowerAP::convertSymAP @ 0x11b7f80  (8270-B body)
void LowerAP::convertSymAP(bir::SymbolicAccessPattern* a2)
{
    Instruction* I  = a2->parent;          // a2+0x20  — the consuming inst we splice into
    // ── 4.1  ENTRY GUARD: the symbolic AP must NOT be DRAM ─────────────────────────
    if (a2->isLocationDRAM())                                    // @0x11b7ff4
        reportError("cannot be DRAM");                           // lower_ap.cpp:162
    Function* F   = I->getFunction();
    EngineType eng = *(u32*)(I + 0x90);
    std::string locName = a2->location /*a2+0x38*/ ->name();     // tail of every reg name
    // three "StorageBase type does not match" guards: location must be a MemoryLocationSet
    if (!a2->addrTensor /*a2+0x38→+1064*/)
        reportError("Addr tensor has not been created yet");

    // ── 4.2  Mint the seven registers (§3) ─────────────────────────────────────────
    Register* RegAPOffset    = F->addRegister("Reg_..._RegAPOffset_"   + locName, eng, 1);
    Register* RegSubTensorId = F->addRegister("Reg_..._RegSubTensorId_"+ locName, eng, 1);
    Register* RegTemp        = F->addRegister("Reg_..._RegTemp_"       + locName, eng, 1);
    Register* RegDramAddr0   = F->addRegister("Reg_..._RegDramAddr0_"  + locName, eng, 1);
    Register* RegDramAddr1   = F->addRegister("Reg_..._RegDramAddr1_"  + locName, eng, 1);
    Register* RegTemp1       = F->addRegister("Reg_..._RegTemp1_"      + locName, eng, 1);
    Register* RegAPAddr      = F->addRegister("Reg_..._RegAPAddr_"     + locName, eng, 1);

    // ── 4.3  Lower the two affine addresses (the CORE; §2) ─────────────────────────
    assert(getTileAddrs(a2)  && "symAP tile_addrs affineExpr is null");   // :186
    assert(getBlockAddrs(a2) && "symAP block_addrs affineExpr is null");  // :187
    lowerAffineExpr(I, getTileAddrs(a2),  /*dst=*/RegAPOffset,    /*tmp=*/RegTemp); // @0x11b88bf
    lowerAffineExpr(I, getBlockAddrs(a2), /*dst=*/RegSubTensorId, /*tmp=*/RegTemp); // @0x11b88e7
    //  TILE  (inner)  affine → RegAPOffset  (the free-axis flat offset)
    //  BLOCK (outer)  affine → RegSubTensorId (the partition / sub-tensor ordinal)

    // ── 4.4 / 4.5  Runtime base-address load through a "-ADDR" Pointer MemoryLocation ─
    //  Build (or validate) a "<locName>-ADDR" Pointer ML (kind 8 / subkind 8, Dtype int64),
    //  setReferenceMemLoc → the index/addr tensor.  Then load the 64-bit base into the pair:
    MemoryLocation* ptrML = getOrCreate("<locName>-ADDR");                  // @0x11b8f70..
    addTensorLoad(I, name, ptrML, RegDramAddr0 /*lo*/, RegDramAddr1 /*hi*/, 0, 0); // @0x11b9028
    addRegAluTwo64bitRegArg(I, name, /*AluOp=*/4 /*ADD*/,
                            RegDramAddr0, RegDramAddr1, RegTemp, RegTemp1,
                            RegDramAddr0, RegDramAddr1);                    // @0x11b9107  (64-bit add)
    addTensorLoad(I, name, ptrML, RegAPAddr, ...);                         // @0x11b9343  (resolve final addr)

    // ── 4.6  Partition / free-axis DELINEARIZATION  (SB / PSUM path) ────────────────
    //  Runs only if Pattern.size != 0; else __assert_fail "getPattern()[0].getStep() > 0".
    int64 step = a2->Pattern[0].getStep();   assert(step > 0);             // lower_ap.cpp:0x101
    int64 part_stride = a2->isLocationPSUM() ? 0x8000     // PSUM bank stride
                      : a2->isLocationSB()   ? 0x40000    // SB  partition stride
                      : (reportError("DRAM AP should not reach LowerAP" /*:271*/), 0);
    int64 dtype_bytes = qword_1DF4040[a2->Dtype];         // Dtype ≤ 0x13 guarded

    //  RegAPAddr = base + (off / step)·part_stride + (off % step)·dtype_bytes
    addRegAluOneRegArgOneImm(/*7 DIVIDE*/, RegAPOffset, step,        RegTemp);  // @0x11b93c5
    addRegAluOneRegArgOneImm(/*6 MULT*/,   RegTemp,     part_stride, RegTemp);  // @0x11b944b
    addRegAluTwoRegArg      (/*4 ADD*/,    RegAPAddr,   RegTemp,     RegAPAddr); // @0x11b94b1
    addRegAluOneRegArgOneImm(/*0x1B MOD*/, RegAPOffset, step,        RegTemp);  // @0x11b952b
    addRegAluOneRegArgOneImm(/*6 MULT*/,   RegTemp,     dtype_bytes, RegTemp);  // @0x11b95a9
    addRegAluTwoRegArg      (/*4 ADD*/,    RegAPAddr,   RegTemp,     RegAPAddr); // @0x11b9610

    // ── 4.7  Matmul second-operand end-address bias (special case) ──────────────────
    if (I->opcode == 8 /*InstMatmult*/ && a2 == I->getArgument(1)) {
        int64 n = getNumElementsPerPartition(a2);
        addRegAluOneRegArgOneImm(/*4 ADD*/, RegAPAddr,
                                 /*imm=*/dtype_bytes * (n - 1), RegAPAddr);
    }

    // ── 5  Build the kind-3 RegisterAccessPattern + atomic operand swap (§5) ─────────
    ...
}
```

### 4.6 — why the delinearization

The flat element offset (`RegAPOffset`) addresses a logical 1-D tensor, but SBUF and PSUM
are physically **2-D**: a partition axis with a large stride, and a free byte axis within
each partition. The sequence splits the flat offset and re-linearizes into the engine's
byte-address space:

```text
partition_ordinal = RegAPOffset / step          (DIVIDE op 7)
partition_base    = partition_ordinal · part_stride   (MULT op 6, part_stride = 0x40000 SB / 0x8000 PSUM bank)
free_elem_index   = RegAPOffset % step          (MOD op 0x1B)
free_byte_offset  = free_elem_index · dtype_bytes     (MULT op 6, dtype_bytes = qword_1DF4040[Dtype])
RegAPAddr        += partition_base + free_byte_offset  (two ADD op 4)
```

> **CORRECTION — the opcode table is `{add=4, mult=6, divide=7, mod=0x1B, bypass=0}`, not
> just `{4, 6}`.** An earlier reading pinned only `add=4` / `mult=6` (from the affine walk)
> and left `divide` / `mod` as a GAP. The `4.6` delinearization settles it directly: the
> `DIVIDE` immediate appears at `0x11b93c5` and the `MOD` immediate `0x1B` at `0x11b952b`,
> consistent with the `AluOpType` enum `add=4, mult=6, divide=7, mod=0x1B(27), bypass=0`.
> The 64-bit base path at `4.4` reuses `add=4`. (The broader 0..29 `klr::AluOp` name table
> — `… 11 divide … 24 mod … 25 mult …` — is a *separate, larger* software enum; do **not**
> assume `bir::AluOpType` shares its ordinals. The hardware `AluOpType` values above are
> the ones stamped at `InstRegisterAlu+0xf0`.)

> **QUIRK — the matmul RHS points at the *last* element.** For `InstMatmult` argument 1
> (the moving / RHS operand) the address is biased by `(numElemsPerPartition − 1)·dtype_bytes`
> so the descriptor starts at the end of the partition. The `opcode==8` + `getArgument(1)`
> + `getNumElementsPerPartition` guards are confirmed; the "consumed in reverse" rationale
> is INFERRED.

---

## 5. Building the kind-3 RegisterAccessPattern and splicing it in

After the address registers are computed, `convertSymAP` constructs the replacement
operand and swaps it into the instruction atomically:

```c
// tail of convertSymAP  (RegisterAccessPattern sizeof 0x118 = 280 B, kind-3)
RegisterAccessPattern* regAP = new(tc_new(280)) RegisterAccessPattern(I);
assert(isa<MemoryLocationSet>(a2->location));             // StorageBase type == 1
regAP->setLocation(a2->location);                         // bind the MemoryLocationSet
regAP->setType(a2->Dtype);                                // *(regAP+0x30) = dtype
regAP->setRegLocation(RegAPAddr);     //  regref         @+0xE8  = the computed start address
regAP->setPattern(a2->Pattern);       //  copy the APPair (step,num) SmallVector  a2+0x50 → regAP+0x50
regAP->setRegAPOffset(RegAPOffset);   //  reg_ap_offset  @+0xF0  = the free-axis offset register
*(Register**)(regAP + 0xF8) = RegSubTensorId;  // reg_sub_tensor_id  @+0xF8 = partition ordinal

if (a2->isOutput /* a2+0x2c */ == 0) {                    // reader operand
    I->insertArgument(iter, regAP);  I->removeArgument(*a2);
} else {                                                  // writer operand
    I->insertOutput(iter, regAP);    I->removeOutput(*a2);
}
++*(u32*)(this + 0x60);   // LowerAP stat counter of converted symbolic APs
```

The static `Pattern` (strides / counts) is copied **verbatim** — the encoder still needs
that geometry for `num_elem` / stride — while the three register fields carry the now
register-valued address. The `const_ap_offset` / `const_sub_tensor_id` / `is_regloc_offset`
fields keep their constructor defaults; this path is fully register-driven.
`insertArgument`/`insertOutput` place the new operand and `removeArgument`/`removeOutput`
drop the symbolic one — a single atomic operand swap. (Evidence: `tc_new(280)` + ctor;
`setLocation` / `setType`-via-vtable+16 / `setRegLocation` / `setPattern`-via-vtable+152 /
`setRegAPOffset` @ `0x11b9837`; `reg_sub_tensor_id` store `*(regAP+248)`;
`insertArgument`/`Output` branch on `a2+0x2c`; counter `++*(this+0x60)`.)

### Where `convertSymAP` is driven from

```text
LowerAP::run(Module&)             0x11ba970   per-fn; arch guard cmp ebx,0x13
  → convertSymAPtoRegAP(BB&)      0x11ba8b0   recursive (opcode 0x69 = Loop recurses into holder blocks)
    → convertSymAPForInst(Inst&)  0x11b9fd0   walks operands; collectSymArg dedups each SymbolicAP once
      → convertSymAP(symAP)       0x11b7f80   THIS function, once per deduped symbolic AP
```

It runs on the post-per-engine-split, pre-register-allocation module; the registers it
mints are virtual and are coloured by the later `coloring_allocator_reg` pass, then stamped
by the encoder into the ADDR4 register-mode field (bit-31 set ⇒ register id + reg_ap_offset).

---

## 6. `ExpandDynamicAPInfo` — the residual `DynamicAPINFO` driver

`convertSymAP` handles *symbolic* (kind-2) APs that survive to `lower_ap`. The later
`expand_inst_late` pass handles the **other** half: by that point some operands are
`PhysicalAccessPattern`s that still carry a `DynamicAPINFO` companion at `pap+0x1d8` (the
scalar / register-backed runtime offset that `assembleDynamicInfo` packaged). This driver
materializes that residual offset into real `InstRegisterAlu` ops.

```c
// ExpandInstLateImpl::ExpandDynamicAPInfo @ 0xcd4dc0  (≈689-ins body)
void ExpandDynamicAPInfo(Instruction& I)
{
    std::vector<PhysicalAccessPattern*> work;     // v107
    std::vector<int>                    indIds;   // v109

    // 1. Scan the Output list (I+0xA0) and Argument list (I+0xB0); for each operand that
    //    isa<PhysicalAccessPattern>:
    for (Argument* ap : operands(I)) {
        if (!isa<PhysicalAccessPattern>(ap)) continue;
        if (isScalarDynamicOffsetAP(ap)) {                         // @0xcd5039
            work.push_back(ap);
            if (isScalarIndirectDynamicAP(ap))                     // @0xcd5072
                indIds.push_back(getIndirectArgId(ap));            // @0xcd527b
        }
    }

    // 2. DENSIFY the indirect-arg ids: id' = id − (#collected ids strictly < id).
    //    (SSE _mm_cmpgt_epi32 reduction = the "count-less-than" rank.)  @0xcd53d3 / 0xcd535c
    for (PhysicalAccessPattern* ap : work-with-indirect)
        setIndirectArgId(ap, rank(getIndirectArgId(ap), indIds));

    // 3. EXPAND each collected AP into a register-compute chain.
    int inserted = 0;
    std::vector<Instruction*> emitted;            // v92
    for (PhysicalAccessPattern* ap : work)
        ExpandDynamicAPInfoImpl(this, *ap, inserted, emitted);     // §7  @0xcd54f0

    log("Expanding N dynamic accesses of <inst> resulted in M inserted instructions");

    // 4. Splice the emitted instructions into the BB, then re-link dependency edges.
    insertInto(BB, emitted);                                       // sub_CC3CA0
    bir::rewireDeps<Instruction>(I);                               // @0xcd57a9
}
```

> **NOTE — the indirect-arg densification.** When several indirect (gather) operands are
> pulled out for expansion, their argument ids are re-numbered into a dense `0..k-1` range
> so the encoder's indirect-arg table has no holes. This is bookkeeping, not arithmetic,
> but it is part of the same pass and easy to miss.

---

## 7. `ExpandDynamicAPInfoImpl` — Expr → register with CSE

Given a `PhysicalAccessPattern` carrying a `DynamicAPINFO` (its `aff_expr` = a pelican
`{(Expr, coef)}` vector plus a static residue `c`), this emits the `InstRegisterAlu` (IT73)
sequence that computes `addr = c + Σ_i coef_i · index_i` into a register and binds it onto
the AP. It is the *pelican-Expr* analogue of `lowerAffineExpr`'s loop-axis walk — here the
terms are runtime index expressions (`IndirectArgExpr` / `IntRuntimeValueBase`).

The distinguishing feature is **common-sub-expression elimination on the affine DAG**:

```c
// ExpandInstLateImpl::ExpandDynamicAPInfoImpl @ 0xccd030  (≈31 KB, the largest of the three)
//
// KEY STRUCTURES:
//   std::unordered_map<RefPtr<pelican::Expr>, bir::Register*>  exprRegMap;  // custom ExprRefHasher
//   std::deque<RefPtr<pelican::Expr>>                          worklist;    // tree traversal
//   std::vector<pair<Dtype,Dtype>>                             dtypeBook;   // per-term dtype
//
void ExpandDynamicAPInfoImpl(PhysicalAccessPattern& ap, int& inserted,
                             std::vector<Instruction*>& out)
{
    Register* sumReg = /* 64-bit accumulator */;
    bool first = true;
    for (auto& [Expr_i, coef_i] : ap.DynamicAPINFO.aff_expr) {
        assert(coef_i > 0 && "DynamicAPInfo coefficient must be positive");   // "Coef > 0"

        // resolve the index register for Expr_i — MEMOIZED  (CSE across terms/operands)
        Register* idxReg = exprRegMap.find(Expr_i);                  // reuse if computed
        if (!idxReg) {
            idxReg = compute(Expr_i);   // mint "-dynamic-index-reg-" / "-dynamic-offset-reg-",
                                        // emit the term's own sub-tree → register
            exprRegMap[Expr_i] = idxReg;
        }
        assert(idxReg && "each dynamic-offset term should have a register");
        assert(idxReg->getEngine() == I.getEngine());     // "RegDynIndex->getEngine() == I.getEngine()"

        // term = idxReg · coef_i           (InstRegisterAlu mult, op 6)  [inst+0xF0]=6 @0xccdd2a
        Register* termReg = addRegister("-reg-mult-...");
        emitInstRegisterAlu(MULT/*6*/, idxReg, coef_i, termReg);    // names "-reg-mult-out-"/"-reg-mul-"

        // sumReg += term                   (InstRegisterAlu add,  op 4)  [inst+0xF0]=4 @0xcceb02
        if (first) { emitRegPairZeroInit(BB, I, sumReg, Dtype, name); first = false; } // @0xcd11eb
        emitInstRegisterAlu(ADD/*4*/, sumReg, termReg, sumReg);     // names "-reg-add-"
    }
    // sumReg = Σ coef_i · index_i ; the static c folds in as the AffineExpr's empty-terms constant.
    // (asserted "Backend after Unroll pass can only have AffineExpr with empty terms")
    out += emitted; inserted += count;
}
```

Every emitted `InstRegisterAlu` is built **directly** via `insertElement<InstRegisterAlu>`
(not through the `addRegAlu*` wrappers `convertSymAP` uses), inheriting the consuming
instruction's engine at `inst+0x90` and setting its AluOp at `inst+0xf0`. 64-bit
accumulators are zero-seeded first by `emitRegPairZeroInit` (`@0xcd11eb`).

> **NOTE — the CSE cache is the point.** Two dynamic dims that share a common affine
> sub-expression resolve through `exprRegMap.find()` to the *same* register, so the shared
> term's `InstRegisterAlu` chain is emitted once. A parallel
> `MemoryLocation* → vector<Instruction*>` map records, per memory location, all
> instructions emitted to materialize its address, so later cleanup / rewire passes can
> find them.

Guard strings worth knowing for a reimplementation (all dumped from the binary):
`"Scalar dynamic offset must have 1 element"`, `"Only DynamicDMA can have DynamicAP"` (a
residual `DynamicAPINFO` at this stage is legal only on a dynamic-DMA instruction),
`"DynamicAPInfo lowering for specific AffineOp not implemented"` (an unsupported pelican
op-kind — e.g. a FloorDiv/Modulo nesting the expander cannot lower — is a hard error).

### `DynamicAPINFO` field surface

Recovered from the imported method set (the definition lives in libBIR; the accessor
surface is authoritative, the field offsets INFERRED from entry-block reads):

| Field | Type | Note |
|---|---|---|
| `staticOffsetPortion` | `long` | `setStaticOffsetPortion(long)` — compile-time constant base |
| `actualPattern` | `SmallVector<APPair>` | `setActualPattern(...)` — stride / count pairs (dim-size & stride arrays read at entry) |
| `dynamicExprs` | container of `QuasiAffineExpr` | per-dim runtime offset, reached at `pap+0x1d8` |
| `axisRegs` | `DenseMap<DynamicForLoopAxis*, Register*>` | binds each dynamic-for-loop induction axis to its runtime register — the bridge to a loop's trip value |

Predicates: `getCanonicalPattern`, `getNumElementsPerPartition`, `isPartitionContiguous`.
AP-level taxonomy this consumes: `isScalarDynamicOffsetAP`, `isScalarIndirectDynamicAP`,
`isTensorIndirectDynamicAP`, `get/setIndirectArgId`, `getIndirectOffsetAP`,
`getNumIndirectIndices`, `getTensorIndirectArgId`, `setOffset`, `forEachDynamicExpr`.

---

## 8. `rewireDynamicAPRegisters` — re-binding `regref` after renaming

When a later rewrite splits or clones a dynamic AP and re-mints its registers, the
`DynamicAPINFO`'s runtime-offset expressions still hold their backing register **by
pointer** — the `bir::BirIntRuntimeValue.regref` field at Expr-node offset **`+0x40`**
(this is the field the §1 CORRECTION distinguishes from the kind-3 AP's `regref` at
`+0xE8`). `rewireDynamicAPRegisters` walks every dynamic expression and re-points each
`regref` to the new register via a `oldName → newName` map.

```c
// rewireDynamicAPRegisters @ 0xef91d0  (dispatcher) + the per-expr lambda @ 0xefa700
void rewireDynamicAPRegisters(PhysicalAccessPattern& ap, Function& F,
                              std::unordered_map<std::string,std::string>& nameRemap)
{
    if (ap.DynamicAPINFO /* ap+0x1d8 */ == nullptr) return;     // no dynamic exprs → nothing to do

    ap.forEachDynamicExpr([&](QuasiAffineExpr& qae) {           // @0x3fd7178
        Expr* n = qae.expr;                                     // RefPtr<Expr>
        // node kinds 6..13 = the AffineIdx / runtime-value family (BirIntRuntimeValue is kind-7)
        if ((u32)(n->kind /*n+0x10*/) - 6 <= 7) {
            Register* reg = *(Register**)(n + 0x40);            // ⭐ BirIntRuntimeValue.regref @+0x40
            if (reg) {
                std::string newName = nameRemap.at(reg->name /*reg+296*/);  // @0xefa752 _Map_base::at
                Register* newReg = F.getRegisterByName(newName);            // @0xefa75d
                if (newReg && newReg != reg)
                    *(Register**)(n + 0x40) = newReg;           // ⭐ REBIND  @0xefa76c
            }
        }
        // recurse into n's children (RefPtr intrusive refcount: inc on entry / dec on exit)
    });
}
```

A name **normalizer** (`0xef9250..0xef93e2`) runs each register name through
`__cxa_demangle` + `strstr('>')` / space-trim before lookup, so the map can key on a clean
register token regardless of how the reference was spelled (handles a leading `*` pointer
marker, demangled type prefixes). A sibling `_Map_base<string, BasicBlock*>::at` rewires
any embedded branch target baked into a dynamic expr in the same pass.

`rewireDynamicAPRegisters` emits **no arithmetic** — it only patches `regref` pointers so
the symbolic offset exprs reference the live registers. The sibling rewrites that supply
the name remap are `rewriteTensorCopyDynamicSrcDst` (`0xcd5c60`) and
`handle64BitStaticOffset` (`0xccc700`), the same `ExpandInstLateImpl` visitor set.

---

## 9. End-to-end lifecycle

```text
front-end dynamic shapes
   │  symbolic dims become QuasiAffineExpr offsets on a Symbolic/Physical AP (kind-2)
   ▼
codegen + assembleDynamicInfo            →  kind-1 PhysicalAP (static) + pelican aff_expr
                                            companion (IndirectArgExpr / BirIntRuntimeValue
                                            + coef + c) → DynamicAPINFO @ pap+0x1d8
   ▼
lower_ap   (LowerAP::run → …toRegAP → …ForInst → convertSymAP, §2-5)
   │  per symbolic AP:  lowerAffineExpr  →  mov(c) + Σ mult(6) + add(4)  InstRegisterAlu chain
   │                    partition/free delinearize: div(7)/mult(6)/mod(0x1B)/mult(6)/add(4)×2
   │                    "-ADDR" Pointer ML + addTensorLoad (64-bit base via add(4))
   ▼  → kind-3 RegisterAccessPattern { regref(+0xE8)=RegAPAddr,
                                        reg_ap_offset(+0xF0)=RegAPOffset,
                                        reg_sub_tensor_id(+0xF8)=RegSubTensorId }  + copied Pattern
expand_inst_late   (ExpandDynamicAPInfo §6 → ExpandDynamicAPInfoImpl §7)
   │  residual DynamicAPINFO on a kind-1 PhysicalAP:  forEachDynamicExpr + Expr→Register CSE
   │  → InstRegisterAlu mult(6)/add(4) chain + emitRegPairZeroInit; densify indirect_arg_id
   │  rewireDynamicAPRegisters §8: rebind each BirIntRuntimeValue.regref(+0x40) via name→name map
   ▼
coloring_allocator_reg              →  colours the virtual registers minted above
   ▼
encoder (CoreV*Gen assignStartAddr<ADDR4>)  →  kind-3 → ADDR4 bit-31 register-mode + reg id + reg_ap_offset
```

The two passes are complementary, not redundant: `convertSymAP` lowers **loop-axis affine**
symbolic APs (the `getTileAddrs`/`getBlockAddrs` form) and does the SB/PSUM 2-D
delinearization; `ExpandDynamicAPInfoImpl` lowers the **residual pelican `DynamicAPINFO`**
runtime offset (the gather / dynamic-DMA form) with CSE. Both emit the same IT73
`InstRegisterAlu` primitive with `{engine@+0x90, aluOp@+0xf0}` operands, and both leave
the static stride pattern compile-time while moving only the dynamic offset into registers.

---

## 10. Confidence and gaps

- **CONFIRMED (disasm-anchored in libwalrus.so):** all five target addresses; the
  seven-register nursery and its name suffixes; the `lowerAffineExpr` simple-affine
  sum-of-products walk and the `mult=6` / `add=4` opcodes; the §4.6 delinearization with
  `divide=7` / `mod=0x1B` immediates and the `0x40000` / `0x8000` partition strides; the
  IT73 `{engine@+0x90, aluOp@+0xf0}` fields; the kind-3 `RegisterAccessPattern` field
  layout (`regref@+0xE8` / `reg_ap_offset@+0xF0` / `reg_sub_tensor_id@+0xF8`); the
  `ExpandDynamicAPInfo` predicate set and indirect-arg densification; the `ExprRefHasher`
  CSE cache; the `rewireDynamicAPRegisters` `regref@+0x40` rebind via name→name→Register.
- **STRONG:** the DRAM-pair vs scratch role split of `RegDramAddr0/1` / `RegTemp1`; the
  outer-driver → Impl worklist threading; the `BasicBlock` rewire sibling map; the
  `DynamicAPINFO` field layout (from accessor names + entry-block array reads); the
  branch-by-branch inner dispatch of `ExpandDynamicAPInfoImpl` (sampled at the emit-call
  level, not byte-complete).
- **INFERRED:** the matmul-RHS "point at last element" rationale (the opcode/arg/element
  guards are CONFIRMED); the `DynamicAPINFO` field *offsets* (the method surface is
  authoritative, the offsets reasoned from entry reads).
- **GROUNDING LIMIT:** `libwalrus.so` / `libBIR.so` are not present in this repo's
  extracted tree, so the cited bodies could not be re-disassembled here; the anchors are
  carried from the libwalrus IDA database the backing analyses ran against. The `walrus_driver`
  ELF that *does* ship declares both as `NEEDED`.
- **GAP:** the `DynamicAPINFO` setter bodies (defined in libBIR, imported here); the exact
  integer the front-end stamps for the symbolic/register "kind" labels (mapped to the
  C++ AP class + backing memloc tag, not re-derived to a raw libBIR enum constant).
