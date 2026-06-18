# birverifier Per-Op Verification

> *All addresses on this page apply to `libwalrus.so` from neuronx_cc 2.24.5133.0+58f8de22 (cp310; BuildID `92b4d331a42d7e80bb839e03218d2b9b0c23c346`). The cp311/cp312 wheels share the same `.text` logic. Other versions will differ.*

## Abstract

The neuronx-cc backend's first legality gate — **L1** — is `birverifier`, an `IRVisitor`-style pass that walks every BIR instruction and rejects illegal ops *before* codegen lowers them to the 64-byte wire word. It is the conceptual analogue of an LLVM `MachineVerifier`, but factored differently: each per-op driver (`birverifier::InstVisitor::visitInst<Op>`) is a *thin* sequence of calls into a small menu of **shared check helpers** — engine, arch-level, memory-type, partition-access, dtype — plus one or two op-specific checkers. The legality *facts* are split between the verifier (engine vectors, shape/AP asserts, hard-coded numeric bounds) and the BIR op class in `libBIR` (the per-op valid-dtype sets, which the verifier only membership-tests).

This page documents the **per-op-family L1 logic**: the shared helper primitives every verifier composes, the **MX-matmul 18-assert contract** (the headline — two functions, `checkMatmultMxInputs` with 11 asserts and `checkMatmultMxInstruction` with 7, enumerated exhaustively with predicate text, source line, and `NeuronAssertion` `ErrorCode`), the **collective replica-group gate** in `checkCollectiveCompute`, and the **kernel / CustomOp gates**. Every assert string transcribed here is the literal argument to the `NeuronAssertion` message builder `sub_FBFA80`, read from the decompiled bodies; the `ErrorCode` is the 2nd integer argument to `logging::NeuronAssertion<neuronxcc::backend::ErrorCode>::NeuronAssertion(...)`.

For reimplementation, the contract is:

- **The shared-helper menu** — the five primitives (`checkValidEngines`, `checkArchLevel`, `checkMemType`, `checkLegalPartitionAccess`, `checkDataType`) and exactly which assert / `ErrorCode` each raises, so a reimplementer reproduces the *common* rejection surface once rather than per op.
- **The MX-matmul 18-assert contract** — every predicate, its numeric bound, its source line, and its `ErrorCode`, because this is the densest and most silicon-specific gate in the verifier and the one most likely to be reimplemented wrong.
- **The collective replica-group gate** — how the "matching replica groups" invariant is actually enforced (an element-ratio check against `replica_groups_shape`, not a group-list `memcmp`).
- **The kernel / CustomOp gates** — base-partition, NKI buffer-shape, and the legacy-arch restriction on `CustomOp`.

The verifier is the **L1** half of a conjunctive `L1 ∧ L2` legality system. L2 — the 64-byte wire-word silicon validator (`is_valid_neuron_engine_instruction`) reached from `CoreV?GenImpl::runSingleISACheck` — runs later, on the emitted word, and catches things L1 structurally cannot (the dual-FP8 silicon restriction is the canonical example: it lives only at L2). The L0/L1 dispatcher that *routes* a BIR op to the per-op driver below, and the L2 wire cascade these checks precede, are documented on their own pages (the legality-dispatch page, in-flight; the L2 wire-validator page, in-flight). The MX legality contract is shared with the MX-matmul legality contract page (planned); the collective replica-group rules are revisited in the collectives part (planned). This page is the per-op *verifier-body* reference.

| | |
|---|---|
| **Binary** | `libwalrus.so` (64,973,024 B), `.text`/`.rodata` `VA == fileoff` |
| **Layer** | L1 — `birverifier`, pre-codegen, operates on BIR / symbolic IR |
| **Dispatch** | `bir::IRVisitor<birverifier::InstVisitor,void>::visit` → `visitInst<Op>` |
| **Assert builder** | `sub_FBFA80` (predicate string) → `logging::NeuronAssertion<…ErrorCode>` |
| **Source anchor** | every assert cites `…/src-3.10.16/neuronxcc/walrus/verifier/src/inst_visitor.cpp:<N>` |
| **MX contract** | `checkMatmultMxInputs` @`0x1007420` (11 asserts) + `checkMatmultMxInstruction` @`0x10139a0` (7 asserts) = **18** |
| **dtype byte-size LUT** | `qword_1DEFBA0` `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}` (xxd-verified) |

> **NOTE — the two-VA-frame artifact.** Every `check*` helper exists **twice** in the binary: a small `.`-prefixed thunk in the `0x5e****`–`0x62****` cluster, and the real body in the `0xfa****`–`0x104****` cluster. The `visitInst<Op>` drivers call the thunk; the thunk tail-jumps to the body. This page cites the **real bodies**. So `checkMemType` appears as both `0x62c790` (thunk-frame) and `0xfd12f0` (body) in prior reports — both are real, they name the same logic. The named MX targets `checkMatmultMxInputs` @`0x1007420` / `checkMatmultMxInstruction` @`0x10139a0` are the real bodies; their thunks are @`0x5fb140` / @`0x6090d0`.

---

## The Verifier Shape

### Purpose

`birverifier` is the live BIR legality enforcer. It does **not** call into `libBIR`'s dormant L0 verifier; it re-encodes the legality facts it needs (engine vectors come from inline `rodata` templates, arch-level min/max are passed as immediates, dtype sets are pulled from the BIR op class via virtual accessors). Each op gets a `visitInst<Op>` driver that composes shared helpers, so the rejection surface is uniform: the same `ErrorCode` 400 means "wrong engine" for *every* op, 366/367 means "wrong arch" for every op, and so on.

### Entry Point

```text
bir::IRVisitor<birverifier::InstVisitor, void>::visit(I)
  └─ birverifier::InstVisitor::visitInst<Op>(I)        ── per-op driver (≈120 ops)
       ├─ visitInstruction(I)                          ── common Instruction shell
       ├─ checkMemType(I, dir, idx, allowed)           ── 0xfd12f0   [code 310]
       ├─ checkTensorIndirectIO(I, inSet, outSet)
       ├─ checkArchLevel(I, optional<min>, optional<max>) ── 0xfd2540 [366 / 367]
       ├─ checkValidEngines(I, vector<EngineType>)     ── 0x103a200  [code 400]
       └─ <op-specific>  e.g. checkMatmultMxInputs / checkCollectiveCompute / …
```

### The Five Shared Primitives

Each driver composes a subset of five primitives. Their bodies were read end-to-end; the assert text and `ErrorCode` are verbatim.

| Helper | Body @ | Rule (verbatim assert) | `ErrorCode` / line | Conf |
|---|---|---|---|---|
| `checkValidEngines` | `0x103a200` | `I.engine` (`Inst+0x90`) `== 7`(ALL) or `== 0`(Unassigned) early-passes; else linear scan of the inline `vector<EngineType>` (value 7 = wildcard slot). Fail predicate `"validEngines"`. | **400** / `inst_visitor.cpp:1368` | CERTAIN |
| `checkArchLevel` | `0xfd2540` | `curArch = Module+172`; if `min.has_value && curArch < min` → `"curArchLevel >= min.value()"`; if `max.has_value && curArch > max` → `"curArchLevel <= max.value()"`. | **366** / `:1299`, **367** / `:1307` | CERTAIN |
| `checkMemType` | `0xfd12f0` | `arg = getArgIndirectionArgOrOutput(I,dir,idx)` (`dir==2`→output, else input); `arg.memloc.memtype ∈ allowed` (a `SmallVector<MemoryType,3>`), else fail (message lists `MemoryType2string`-joined expected vs actual). | **310** / `:1285` | CERTAIN |
| `checkLegalPartitionAccess` | `0xfc9520` | per-AP: `"Pattern accesses > 128 partitions"`, `"Access pattern crosses 64th partition boundary."`, `"Access pattern did not start at parition 0 or 64. Starts at partition "` [sic — original typo "parition"]. | (string) | CERTAIN |
| `checkDataType` | `0xfd10c0` | `arg.dtype ∈ I.getXxxValidDtypes()` — the **set is owned by the BIR op class** (`getSrcValidDtypes()` / `getDstValidDtypes()` / `…OffsetsValidDtypes`, `libBIR`-resident); the verifier only membership-tests. Miss → `reportError(inst, IO, idx, msg)` @`0x6175a0`. | (libBIR set) | CERTAIN |

> **QUIRK — engine legality is re-encoded, not shared.** The legal-engine vector is **built inline** by each `visitInst<Op>` (e.g. `Pool` builds `{5}=DVE` with a literal `*v=5`; `Activation` builds `{2}`), then passed to `checkValidEngines`. The same engine-template literals appear at L0 (`libBIR` `rodata`) and L2 (`neuron_isa_check_opcode_on_engine` bitmaps), but there is **no call edge** between the three — they are kept in sync by convention. A reimplementer must reproduce the inline vector per op *and* the L2 opcode/engine bitmap, and keep them consistent by hand.

> **GOTCHA — passing L1 ⇏ passing L2.** The L1 menu has *no* dual-FP8 rule. The silicon's FP8×FP8 quadrant/alignment restriction is `core_v4::s3_lw_dual_fp8_restrictions` @`0x14ac040`, reached only from the L2 wire cascade. The nearest L1 analogue is `checkFP8TypeConsistency` @`0xfdf100` (`"LegacyTensor == nullptr || OcpTensor == nullptr"` — a module-wide ban on *mixing* legacy-fp8 and OCP-fp8 representations), which is a different, weaker rule. An instruction can clear all of L1 and still be rejected by L2.

---

## The MX-Matmul 18-Assert Contract

### Purpose

`bir::InstMatmultMx` is the OCP-MXFP scaled matmul: block-scaled FP4/FP8 inputs (x4-packed) with E8M0 per-block scales. It carries **six** access patterns, queried positionally:

```text
arg0 = ifmap data        (x4-packed FP4/FP8)   getArgument<AccessPattern>(I,0)
arg1 = weights data      (x4-packed FP4/FP8)   getArgument<AccessPattern>(I,1)
arg2 = ifmap scales      (E8M0 uint8 [P/8,F/4]) getArgument<AccessPattern>(I,2)
arg3 = weights scales    (E8M0 uint8 [P/8,F/4]) getArgument<AccessPattern>(I,3)
out0 = PSUM result                              getOutput<AccessPattern>(I,0)
```

Legality is split across **two** verifier bodies, called in sequence from `visitInstMatmultMx` @`0xfb9a50`:

- **`checkMatmultMxInputs`** @`0x1007420` (9,777 B, 422 BBs) — the **shape / numeric** contract: 11 asserts on per-partition element counts, the data↔scale x4 ratio, scale start-partition alignment, PSUM-bank capacity, the K bound, even-block packing, and partition-count agreement.
- **`checkMatmultMxInstruction`** @`0x10139a0` (6,808 B, 291 BBs) — the **instruction-level** contract: 7 asserts on `engine == PE`, data/scale same SBUF quadrant, scale start-partition `% 4`, column never tiled, row tile == partitions, and replication-shift == 0.

Eleven plus seven is the **18-assert MX contract**.

> **NOTE — the x4-unpack gate.** Before the asserts, each of the 4 inputs + output computes a *logical* element count: `n = AP.getNumElementsPerPartitionConsideringIndirection(); dt = *(DWORD*)(AP+0x30); if (dt == 2 || (unsigned)(dt - 8) <= 1) n *= 4;`. The x4 dtype set is `{2 = fp4_e2m1_x4, 8 = fp8_e4m3_x4, 9 = fp8_e5m2_x4}`. So every `NumElementsPerPartition` in the asserts below is the **unpacked** (post-x4) count — identical to the set the simulator uses. The data↔scale `×4` ratio is therefore a literal check of the packed/scale relationship.

### Algorithm — checkMatmultMxInputs (11 asserts)

```c
function checkMatmultMxInputs(I):                       // 0x1007420
    // x4-expanded per-partition element counts (see x4-unpack gate above)
    ifK   = x4_unpack(arg0);    wtK   = x4_unpack(arg1);     // K (contraction)
    ifSc  = x4_unpack(arg2);    wtSc  = x4_unpack(arg3);     // E8M0 scale counts
    outE  = x4_unpack(out0);                                 // output free elems
    ifSp  = getStartPartition(arg2);  wtSp = getStartPartition(arg3);
    ifP   = arg0.Pattern[0].num;  wtP = arg1.Pattern[0].num; outP = out0.Pattern[0].num;

    // #1  line 3067  code 228
    assert(ifK == 4 * ifSc);   // "(ifMapNumElementsPerPartition == 4 * ifMapScalesNumElementsPerPartition)"
    // #2  line 3069  code 229
    assert(wtK == 4 * wtSc);   // "(weightsNumElementsPerPartition == 4 * weightsScalesNumElementsPerPartition)"
    // #3  line 3072  code 230 — ifmap scale starts in LOWER half of its 32-partition quadrant
    assert(ifSp % 32 < 16);    // "(ifmapScalesStartPartition % 32 < 16)"      (tested (sp & 0x10)==0)
    // #4  line 3073  code 231
    assert(wtSp % 32 < 16);    // "(weightsScalesStartPartition % 32 < 16)"
    // #5  line 3079  code 232 — ifmap K fits one PSUM bank's worth of output slots × 32
    assert(ifK <= max_ifmap_elements);   // "(ifMapNumElementsPerPartition <= max_ifmap_elements)"
    // #6  line 3083  code 233 — HARD K bound = 128 partitions × 4 (the x4 container)
    assert(wtK <= 128 * 4);    // "(weightsNumElementsPerPartition <= 128 * 4)"   (tested wtK > 0x200)
    // #7  line 3084  code 289 — unpacked weights K is an EVEN number of x4 blocks
    assert((wtK / 4) % 2 == 0);// "(weightsNumElementsPerPartition / 4) % 2 == 0" (tested (wtK & 4)==0)
    // #8  line 3091  code 234 — ifmap partition count is a power-of-two PE tile
    assert(ifP == 32 || ifP == 64 || ifP == 128);
        // "(ifmapNumPartitionsAccessed == 32 || ifmapNumPartitionsAccessed == 64 || ifmapNumPartitionsAccessed == 128)"
    // #9  line 3096  code 235 — ifmap & weights share the K-contraction partition dim
    assert(ifP == wtP);        // "ifmapNumPartitionsAccessed == weightsNumPartitionsAccessed"
    // #10 line 3099  code 236 — output partitions (N) × 4 == weights K
    assert(outP * 4 == wtK);   // "outputNumPartitionsAccessed * 4 == weightsNumElementsPerPartition"
    // #11 line 3102  code 237 — output free elems × 4 == ifmap K
    assert(outE * 4 == ifK);   // "outputNumElementsPerPartition * 4 == ifMapNumElementsPerPartition"
```

| # | line | predicate (verbatim) | bound / meaning | code |
|---|---|---|---|---|
| 1 | 3067 | `ifMapNumElementsPerPartition == 4 * ifMapScalesNumElementsPerPartition` | data has 4× the free elems of its E8M0 scale stream (the `F/4`) | 228 |
| 2 | 3069 | `weightsNumElementsPerPartition == 4 * weightsScalesNumElementsPerPartition` | same, weights | 229 |
| 3 | 3072 | `ifmapScalesStartPartition % 32 < 16` | scale starts in lower half of its 32-part quadrant | 230 |
| 4 | 3073 | `weightsScalesStartPartition % 32 < 16` | same, weights | 231 |
| 5 | 3079 | `ifMapNumElementsPerPartition <= max_ifmap_elements` | PSUM-bank fit (derivation below) | 232 |
| 6 | 3083 | `weightsNumElementsPerPartition <= 128 * 4` | hard K bound = 512 | 233 |
| 7 | 3084 | `(weightsNumElementsPerPartition / 4) % 2 == 0` | weights K = even # of x4 blocks | 289 |
| 8 | 3091 | `ifmapNumPartitionsAccessed == 32 \|\| == 64 \|\| == 128` | power-of-two PE partition tile | 234 |
| 9 | 3096 | `ifmapNumPartitionsAccessed == weightsNumPartitionsAccessed` | shared K-contraction dim | 235 |
| 10 | 3099 | `outputNumPartitionsAccessed * 4 == weightsNumElementsPerPartition` | output N × 4 == weights K | 236 |
| 11 | 3102 | `outputNumElementsPerPartition * 4 == ifMapNumElementsPerPartition` | output free × 4 == ifmap K | 237 |

> **CORRECTION (D-G02 §3.1) —** an earlier compute-family report transcribed the codes for asserts 9/10/11 in scrambled order (listing `outputNumElementsPerPartition*4==ifMap…` as code 235, `ifmap==weights` as 236, `outputNumPartitions*4==weights` as 237). The binary's throw sites, read in body order at `0x1007420`, are unambiguous: line 3091→234, 3096→235, 3099→236, 3102→237, with the predicate strings exactly as tabled above. The dedicated MX report (D-G05 §1) had the correct mapping; this page follows the binary.

#### `max_ifmap_elements` (assert #5) derivation

The bound in assert #5 is computed, not a constant:

```c
outDtype      = out0.getType();                              // *(DWORD)(out+48)
if (outDtype > 0x13) llvm_unreachable("Unknown dtype");      // Dtype.h — valid enum ∈ [0,19]
psumBankBytes = Hwm::getSingleton(arch)->vtbl[+0x78](arch);  // per-arch HWM query
elemsPerBank  = psumBankBytes / qword_1DEFBA0[outDtype];     // dtype byte-size LUT
max_ifmap_elements = 32 * elemsPerBank;                      // ×32 = 32 K per OCP block
```

`qword_1DEFBA0` @`0x1DEFBA0` is the per-dtype **byte-size** LUT, 20 entries, xxd-verified byte-exact:

```text
index:  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19
bytes:  1  1  2  1  1  1  1  1  4  4  2  2  2  2  4  4  4  4  8  8
```

This table is byte-identical to the simulator's dtype stride table — encoder, simulator, and verifier share one dtype-size source. `psumBankBytes` itself is a per-arch HWM value (Trn2/Cayman) not pinned here (the formula `32 · bankBytes/dtypeBytes` is CERTAIN; the concrete `bankBytes` is an HWM-table fact — `INFERRED` per-arch value).

### Algorithm — checkMatmultMxInstruction (7 asserts)

```c
function checkMatmultMxInstruction(I):                  // 0x10139a0
    engine = *(DWORD*)(this + 0x90);                    // bir::Instruction engine field
    // quadrant(x) = startPartition >> 5  (128 partitions / 4 quadrants of 32)
    ifQ  = getStartPartition(arg0) >> 5;  ifScQ = getStartPartition(arg2) >> 5;
    wtQ  = getStartPartition(arg1) >> 5;  wtScQ = getStartPartition(arg3) >> 5;
    ifScSp = getStartPartition(arg2);     wtScSp = getStartPartition(arg3);
    nParts = arg0.Pattern[0].num;
    sub_10125B0(this, &row_tile_pos, &col_tile_pos, &row_tile_size, &col_tile_size);

    // #1  line 2994  code 205 — MX matmul ONLY legal on the PE (systolic) engine
    assert(engine == EngineType::PE);          // "I.getEngine() == bir::EngineType::PE"   (PE == 3)
    // #2  line 3012  code 212 — ifmap data & its scale in the SAME 32-partition quadrant
    assert(ifQ == ifScQ);                      // "ifmapStartQuadrant == ifmapScalesStartQuadrant"
    // #3  line 3014  code 213
    assert(wtQ == wtScQ);                      // "weightsStartQuadrant == weightsScalesStartQuadrant"
    // #4  line 3017  code 214 — both scale streams 4-partition aligned (4-col MX block)
    assert(ifScSp % 4 == 0 && wtScSp % 4 == 0);
        // "(ifmapScalesStartPartition % 4 == 0) && (weightsScalesStartPartition % 4 == 0)"
    for i in [0, row_tile_pos.size()):                  // ONE loop over the tile descriptor
        // #5  line 3031  code 241 — COLUMN never tiled (full 128-wide PE column or none)
        assert((col_tile_pos[i] == -1 || col_tile_pos[i] == 0)
            && (col_tile_size[i] == -1 || col_tile_size[i] == 128));
        // #6  line 3028  code 240 — ROW tile == full partition count (no row sub-tiling)
        assert(row_tile_size[i] == -1 || (unsigned)row_tile_size[i] == nParts);
    // #7  line 3037  code 204 — MX matmul forbids weight-replication shift
    assert(I.getReplicationShiftAmnt() == 0);  // "I.getReplicationShiftAmnt() == 0"
```

| # | line | predicate (verbatim) | meaning | code |
|---|---|---|---|---|
| 1 | 2994 | `I.getEngine() == bir::EngineType::PE` | engine `Inst+0x90 == 3`; MX matmul is PE-only | 205 |
| 2 | 3012 | `ifmapStartQuadrant == ifmapScalesStartQuadrant` | ifmap data & scale co-reside in one quadrant | 212 |
| 3 | 3014 | `weightsStartQuadrant == weightsScalesStartQuadrant` | weights data & scale co-reside | 213 |
| 4 | 3017 | `(ifmapScalesStartPartition % 4 == 0) && (weightsScalesStartPartition % 4 == 0)` | scales 4-partition aligned | 214 |
| 5 | 3031 | `(col_tile_pos[i] == -1 \|\| col_tile_pos[i] == 0) && (col_tile_size[i] == -1 \|\| col_tile_size[i] == 128)` | column never sub-tiled | 241 |
| 6 | 3028 | `(row_tile_size[i] == -1) \|\| ((unsigned)row_tile_size[i] == numPartitionsAccessed)` | row tile == partition count | 240 |
| 7 | 3037 | `I.getReplicationShiftAmnt() == 0` | no weight-replication shift | 204 |

> **CORRECTION (D-G02 §3.2) —** an earlier compute-family report listed the `checkMatmultMxInstruction` codes shifted by one row (assigning code 204 to `engine==PE` at 2994, code 205 to the *replication* assert, and 214/240/241 to the wrong lines). Read directly at `0x10139a0`, the throw sites pair as tabled: `2994`→205 (`EngineType::PE`), `3012`→212, `3014`→213, `3017`→214, `3031`→241 (col), `3028`→240 (row), `3037`→204 (`ReplicationShiftAmnt`). The dedicated MX report (D-G05 §2) was correct. Note the source lines for the row/col tile asserts are *out of address order* (the `3031` col check precedes the `3028` row check in the body), which is what produced the confusion.

> **QUIRK — the block_size=32 is enforced structurally, never stored.** The verifier never writes a literal `32` as block-size. The 32-element OCP-MXFP block (8 partitions × 4 columns, one E8M0 byte) is *implied* by the conjunction of: the data:scale 4:1 element ratio (#1/#2), the same-quadrant + `%4`-aligned + `%32<16` scale placement (Instruction #2/#3/#4, Inputs #3/#4), and the `×4` output relationships (#10/#11). A reimplementer who only checks `block_size == 32` against a stored field will find no such field; the constraint *is* these eight asserts.

### Considerations

`checkMatmultMxInstruction` reads `getReplicationShiftAmnt()` through a `std::variant` getter; the variant tag is checked first (a mis-tagged value throws `bad_variant_access` before the assert is even evaluated). The exact struct offset of that variant field (`~0xB0`/`0xC0` region) is `HIGH` not `CERTAIN` — the predicate text and code 204 are CERTAIN. The `sub_10125B0` tile-descriptor extractor produces four parallel `int32` vectors and self-asserts they are equal length (`row_tile_pos.size() == col_tile_pos.size() == row_tile_size.size() == col_tile_size.size()`); its per-tile derivation math from the AP geometry was not fully transcribed (`INFERRED`).

There is **no** separate `LdWeightMx`-vs-`MatmultMx` L1 assert: the MX wire bundle (`LdWeightMx` + `MatmultMx`) is constructed by the CoreV4 *encoder*, so the verifier checks the unified `InstMatmultMx` (the six APs above), not the two wire halves.

---

## The Collective Replica-Group Gate

### Purpose

Collective ops (`CollectiveCompute`/`Send`/`Recv`, and the coarse-grain `AllGather`/`ReduceScatter`/`AllToAllV`/`SendRecv` variants) are verified in three layers: a base `visitInst` driver, a `checkCollective` IO-tensor gate, and the heavyweight `checkCollectiveCompute` @`0x1033630` (2,916 lines) that holds the replica-group / channel / cc-dimension legality.

### Algorithm — the IO and ratio gates

```c
function checkCollective(I):                            // 0xff7bd0 — switch on I.opcode (Inst+0x88)
    switch (opcode):
      case 48 (CollectiveCompute):
        if (has input AP)  reportError "Collective instruction cannot read IO tensors";   // line 3493
        if (has output AP) reportError "Collective instruction cannot write IO tensors";   // line 3497
      case 49 (CollectiveSend): // input-IO check only
      case 50 (CollectiveRecv): // output-IO check only
      default: reportError "instruction is not a collective";                              // line 3489

function checkCollectiveCompute(I):                     // 0x1033630
    assert(Module.getArchLevel() > ArchLevel::Sunda);                              // code 371 / 319
    assert(I.num_outputs() == 1 || (isCCHavingSBScratchPad(I) && I.num_outputs() == 2)); // code 318
    assert(kindSupported);   // code 311 — the per-arch × (dim,kind) SUPPORTED-MAP gate
    assert(arg.isPartitionContiguous());  // code 312
    assert(arg.isLocationSB());           // code 313
    // ── the MATCHING-REPLICA-GROUPS gate (element-ratio, keyed on replica_groups_shape) ──
    // AllGather:     in/out elem ratio must equal the replica-group size
    //   error "Illegal src/dst ratio for AllGather: in=…"      (keyed "replica_groups_shape")
    // ReduceScatter: error "Illegal src/dst ratio for ReduceScatter: in=…"
    //   error "src/dst element ratio must be the same for all inputs: …"
    // SendRecv:      assert(I.num_arguments() == I.getRecvFromRank().size());     // code 74
    //                assert(OutAP.getType() == InAP.getType());                   // code 75
    // AllToAllV:     assert(I.num_arguments() == I.num_outputs()+1
    //                       || I.num_arguments() == I.num_outputs());
    //   error "AllToAllV metadata input must be uint32, got …"
    //   error "AllToAllV requires at least one data input and one metadata input, got …"
```

> **QUIRK — "matching replica groups" is an element-ratio check, not a group-list compare.** The invariant that a collective's input/output sizes agree with the partitioning across ranks is enforced as a *ratio* between the input and output `getNumElementsAccessed`, checked against `replica_groups_shape` (the ranks-per-group), via the `"Illegal src/dst ratio for AllGather/ReduceScatter"` and `"src/dst element ratio must be the same for all inputs"` errors. There is **no** direct `memcmp` of a replica-group list at L1. A reimplementer reproducing this gate validates the ratio, not the group membership.

> **NOTE — the AllToAllV argument count is more permissive than first reported.** The binary's predicate is `"I.num_arguments() == I.num_outputs() + 1 || I.num_arguments() == I.num_outputs()"` (the `+1` is the metadata input; the equal-count case applies when a channel-buffer output is present), with the human-readable string `"AllToAllV must have one more input than outputs (metadata), or equal count when a channel buffer output is present. Got …"`. An earlier report (D-G04) summarized only the `+1` case.

### Considerations

`checkCollectivesInDynamicCFG` @`0xfd0420` adds a context rule: a collective placed inside a dynamic-CFG basic block (a loop body, per `isDynamicCFGBasicBlock`) must be **local** — `assert(isLocal)` raises code **123** (`"isLocal"`, `inst_visitor.cpp:1024`); a `SendRecv` in that context raises code **1500**. The `kindSupported` map (`qword_3E01660`) is a `.bss` runtime-built two-level `unordered_map` keyed by `(arch/version, kind)`; its exact per-`(dim,kind)` contents are not byte-dumped (the rows live in the static-initializer stream, not a parseable `rodata` table) — `MED` confidence on the *set*, `CERTAIN` on the *mechanism*.

---

## Kernel and CustomOp Gates

### Purpose

Kernel-class ops (`BIRKernel`, `NKIKernel`, `NKIKLIRKernel`) and `CustomOp` wrap opaque or externally-defined bodies; their L1 rules are about *interface* legality (where their inputs/outputs may live, what arch they run on, what buffer shapes they declare) rather than per-op math.

### Algorithm

```c
function visitInstBIRKernel(I):                         // 0xfa7f40
    visitInstruction; checkTensorIndirectIO; checkArchLevel(min=20 /*Sunda*/);
    checkValidEngines({0} /*Unassigned wildcard*/);
    checkBIRKernel(I);                                  // 0xfdcd90
        // for each I/O memloc cast to MemoryLocation:
        assert(mem.getBasePartition() == 0);            // "mem.getBasePartition() == 0"
        //   human string: "Kernel inputs/outputs must be allocated at base partition 0"

function visitInstNKIKernel(I):                         // 0xfa80c0
    … checkValidEngines({0});
    checkNKIKernelFunction(I)      @0xfe1af0;           // assert "I.getFunc() != nullptr"
    checkNKIKernelBufferShapes(I)  @0xfe2160;
        // line 5286 code 1680: SBUF pair both-zero or both-positive
        //   "(sb_num_partitions == 0 && sb_per_partition_size == 0)
        //    || (sb_num_partitions > 0 && sb_per_partition_size > 0)"
        // line 5296 code 1681: "psum_bank_count <= ArchModel.device.core.psumbuf.numBanks"
        // line 5300 code 1682: "psum_num_partitions <= NUM_PARTITIONS"
        // line 5308 code 1683: "I.num_outputs() >= num_sb_buffers + num_psum_buffers"
    checkNKIKernelAddressRotationScope(I);

function visitInstNKIKLIRKernel(I):                     // 0xfa8260
    … checkValidEngines({0});  // no extra shape check — KLIR form is pre-validated

function visitInstCustomOp(I):                          // 0xfa7bd0
    for each input idx:  checkMemType(I, 1, idx, {16, 2, …});
    for each output idx: checkMemType(I, 2, idx, …);
    checkArchLevel(min=20); checkValidEngines({1} /*Pool*/);
    checkCustomOp(I)               @0xfdbf90;
        assert(Arch == ArchLevel::Sunda || Arch == ArchLevel::Tonga);  // code 178
        //   "Arch == ArchLevel::Sunda || Arch == ArchLevel::Tonga"

function visitInstInlineASMBytes(I):                    // 0xfab780
    … checkValidEngines({1} /*Pool*/);  // raw bytes are opaque — no interface check
```

| Op | driver @ | engine | op-specific gate | key assert / code | Conf |
|---|---|---|---|---|---|
| `BIRKernel` | `0xfa7f40` | `{0}` wildcard | `checkBIRKernel` @`0xfdcd90` | `mem.getBasePartition() == 0` | CERTAIN |
| `NKIKernel` | `0xfa80c0` | `{0}` | `checkNKIKernelFunction` @`0xfe1af0` + `…BufferShapes` @`0xfe2160` | `getFunc() != nullptr`; `psum_bank_count <= numBanks` (**1681**); `psum_num_partitions <= NUM_PARTITIONS` (**1682**) | CERTAIN |
| `NKIKLIRKernel` | `0xfa8260` | `{0}` | — | (pre-validated KLIR) | CERTAIN |
| `CustomOp` | `0xfa7bd0` | `{1}` Pool | `checkCustomOp` @`0xfdbf90` | `Arch == Sunda \|\| Tonga` / **178** | CERTAIN |
| `InlineASMBytes` | `0xfab780` | `{1}` Pool | — | (opaque bytes) | CERTAIN |

> **GOTCHA — `CustomOp` is legacy-arch only.** `checkCustomOp` @`0xfdbf90` raises `NeuronAssertion` code **178** unless the module arch is `Sunda` or `Tonga`. On `core_v4`/`core_v5` a `CustomOp` is rejected at L1 outright — a reimplementer targeting current silicon will never legally emit one.

### Considerations

`visitInstGeneric` @`0xfa4980` is the *permissive* base several ops forward to (`SwitchQueueInstance`, `ResetQueueInstance`, the plain `Collective`): it builds the **full** 8-engine vector from `rodata` `xmmword_1DD2CC0`/`1DD2CD0` = `{0,3,2,1,4,5,6,7}` and passes `checkArchLevel(min=20)` — so it gates arch but lets any engine through. Queue-instance ops have *no* queue-specific L1 rule; queue legality is enforced elsewhere (the DMA-QoS verifier).

---

## Control-Flow and RNG/Select Family (brief)

Control-flow ops (`UnconditionalBranch`/`Return`/`Call`/`CompareAndBranch`/`BranchHint`/`Loop`/`DynamicForLoop`/`DoWhile`) all use a **wildcard** engine set `{7}=ALL`, so `checkValidEngines` always passes; their legality is CFG/axis/loop-nest plus per-enum min-arch via `lookupMinArchForISAEnum` + `checkArchLevelForEnum`. The loop verifiers carry the only non-trivial asserts:

| Op | driver @ | key asserts (line / code) | Conf |
|---|---|---|---|
| `Loop` | `0x103f450` | `I.isSchedulingUnit()` (920/**91**, post-unroll 926/**321**) — a non-scheduling-unit loop must have empty nested ranges | CERTAIN |
| `DynamicForLoop` | `0x1040000` | lb/stride are constant affine (`getNumTerms()==0`, codes 182/183/187/188); ub is a 1-term affine with a runtime-int register (codes 184/185/186/195) | CERTAIN |
| `DoWhile` | `0x1041e40` | condition is a `BirIntRuntimeValue` (969/**119**); `!isLegacyLoopImplementationEnabled` (**141**) | CERTAIN |

RNG/Select/Iota/Memset/Dropout pick fixed engines (`Rng` @`0xfb1a60` is the only op with **per-arch** engine selection — `sunda`→`{5}=DVE`, gen3/core_v4/core_v5 each pick their own); their op-specific checks (`checkSelect` @`0x1027b40`, `checkIota`, `checkMemsetMode`) gate shape/mode. These are documented for completeness; the compute-op-family rules (`Activation`, `TensorScalar*`, `TensorReduce`, `BN*`, `QuantizeMx`) are the subject of the legality-dispatch page (in-flight).

---

## Verifier Infrastructure Functions

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `sub_FBFA80` | `0xfbfa80` | `NeuronAssertion` predicate-string builder (the verbatim assert text) | CERTAIN |
| `logging::NeuronAssertion<…ErrorCode>::NeuronAssertion` | — | throws with the 2nd-arg `ErrorCode` | CERTAIN |
| `reportError(inst, IO, idx, msg)` | `0x6175a0` | dtype/mem error builder (used by `checkDataType`/`checkMemType`) | CERTAIN |
| `checkValidEngines` | `0x103a200` | engine ∈ inline vector; 7/0 wildcard; code 400 | CERTAIN |
| `checkArchLevel` | `0xfd2540` | min/max arch window; codes 366/367 | CERTAIN |
| `checkMemType` | `0xfd12f0` | arg mem-loc ∈ allowed `MemoryType` set; code 310 | CERTAIN |
| `checkLegalPartitionAccess` | `0xfc9520` | ≤128 partitions / 64-boundary / start 0\|64 | CERTAIN |
| `checkDataType` | `0xfd10c0` | dtype ∈ `I.getXxxValidDtypes()` (libBIR set) | CERTAIN |
| `checkMatmultMxInputs` | `0x1007420` | the 11 shape asserts (thunk `0x5fb140`) | CERTAIN |
| `checkMatmultMxInstruction` | `0x10139a0` | the 7 instruction asserts (thunk `0x6090d0`) | CERTAIN |
| `getStartPartition` | `0xfc6e30` | first partition an AP touches | CERTAIN |
| `sub_10125B0` | `0x10125b0` | row/col tile-descriptor extractor (4 parallel int32 vectors) | CERTAIN |
| `qword_1DEFBA0` | `0x1DEFBA0` | per-dtype byte-size LUT (20 entries) | CERTAIN |
| `checkCollective` | `0xff7bd0` | collective IO-tensor gate (3489/3493/3497) | CERTAIN |
| `checkCollectiveCompute` | `0x1033630` | replica-group / channel / cc-dim gate | CERTAIN |
| `checkCollectivesInDynamicCFG` | `0xfd0420` | local-collective rule (code 123) | CERTAIN |
| `checkBIRKernel` | `0xfdcd90` | base-partition == 0 | CERTAIN |
| `checkNKIKernelBufferShapes` | `0xfe2160` | SBUF/PSUM buffer-shape bounds | CERTAIN |
| `checkCustomOp` | `0xfdbf90` | Sunda\|\|Tonga; code 178 | CERTAIN |
| `checkFP8TypeConsistency` | `0xfdf100` | legacy XOR ocp fp8 (module-wide) | CERTAIN |

## Related Components

| Name | Relationship |
|---|---|
| Legality-dispatch (L0/L1/L2) | the dispatcher that routes a BIR op to the `visitInst<Op>` driver above and the compute-op-family rules (in-flight) |
| L2 wire-validator | the 64-byte silicon cascade these L1 checks precede; hosts the dual-FP8 gate L1 lacks (in-flight) |
| MX-matmul legality contract | shares the 18-assert MX contract from the simulator/encoder side (planned) |
| Collectives part | the replica-group / cc-kind rules in the runtime collectives context (planned) |

## Cross-References

- [Opcode Master Table](opcode-master.md) — the BIR opcode enum these `visitInst<Op>` drivers dispatch on
- [order-column-tiled-mms / legalize-mm-accumulation-groups](matmul-ordering-accgroups.md) — the matmul accumulation-group legalization downstream of the MX shape gate
- [local-collectives](local-collectives.md) — the local-collective lowering whose legality the `isLocal` gate enforces
- [translate-nki-ast-to-bir & Loop Unroll Passes](translate-nki-unroll.md) — produces the NKI-kernel and loop ops these verifiers gate
