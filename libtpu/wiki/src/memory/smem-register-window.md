# SMEM Register-Window

> *Every symbol, offset, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VA equals file offset at `0xe63c000`; `.rodata` VA equals file offset at `0x84a0000`. Other versions will differ.*

## Abstract

"SMEM register-window" is a phrase that does not name anything in `libtpu.so`. There is no symbol or string `SmemRegisterWindow`, `SregWindow`, `RegisterWindow`, `WindowBase`, `window_base`, `reg_window`, or `SmemSpillRegister` anywhere in the binary's 2.9M strings or 1.2M symbols. This page exists to answer the question the phrase *implies* — "is a window of SMEM mapped into the scalar register file, the way a SPARC register window or a rotating ISA register file works?" — and the answer is **no**. The [SPU scalar register file](../isa/slot-spu-scalar.md) is a flat, hardware-bounded 32-entry file with a 5-bit index and *no* base/size window register, *no* banking selector, and *no* overflow/underflow trap. SMEM is its spill backing store, reached by ordinary scalar load/store opcodes ([SMEM Scalar Memory](smem-scalar-memory.md)), not by a windowed mapping.

What *does* exist — and what a reimplementer who heard "SMEM register-window" was probably pointed at — are three separately-named mechanisms, none of which windows the SREG file. Two of them genuinely window *memory* (one of which can sit in SMEM); the third windows *predicate* registers. The cleanest fit for the phrase is the **CBREG** (circular-buffer register): a single register that *holds* a `{base, offset, size}` window onto on-chip memory and produces a self-advancing, wrap-at-the-end address stream. A CBREG is a register that contains a memory window — the exact inverse of a window onto registers.

This page is the disambiguation-and-reconciliation note for the memory subsystem. It fixes the negative result (the SREG file is un-windowed and SMEM has no register-window machinery), enumerates the three real "windows" and states which resource each one actually windows, and shows how each relates to SMEM specifically. It does **not** re-derive the CBREG opcode bit-layout (that is [CBREG Circular-Buffer Register](../sparsecore/cbreg.md)), the SPU slot field grid ([SPU / Scalar Slot](../isa/slot-spu-scalar.md)), the scalar predicate file ([Predicate Slot](../isa/slot-predicate.md)), or the SMEM allocator ([SMEM Scalar Memory](smem-scalar-memory.md)).

For reimplementation, the contract is:

- **The negative result is the architecture.** A reimplementation must *not* allocate a window-base/window-size register pair for the SREG file, must *not* implement SPARC-style window overflow/underflow traps, and must spill SREG pressure to SMEM explicitly (LSRA-v2), exactly as `libtpu` does. The SREG index is 5 bits; there are 32 of them; that is the whole file.
- **"Register window" is overloaded across three resource classes.** The CBREG `{base,offset,size}` memory window (a register holding a window onto memory), the pipeline `OperandWindow` (a sub-tile of a tensor operand staged in VMEM / **SMEM** / HOST), and the rotating *predicate* file (the only true hardware "window onto registers" on TPU). Knowing which is which is the deliverable.
- **The SMEM tie-ins are real but indirect.** SMEM appears in the CBREG's possible address space, as a legal placement for an `OperandWindow`, and as the optional storage for dynamic window sizes in the fixed-window pipeline emitter — but in none of these is a window mapped *over* the scalar register file.

| | |
|---|---|
| **SREG file (the thing falsely "windowed")** | flat **32** entries, **5-bit** index; bound enforced by `ProtoUtils::EncodingToScalarRegister` @ `0x1e871e40` (`idx > 0x1F` → error) |
| **SREG bank names (no windowed variant)** | `SC_SCS_SREGS`, `SC_TAC_SREGS`, `SC_TEC_SREGS` (.rodata) — no `ROTATING`/`WINDOWED`/`BANKED` SREG bank exists |
| **CBREG (register that holds a memory window)** | banks `SC_{SCS,TAC,TEC}_CBREGS_{BASE,OFFSET,SIZE}`; 16/sequencer; detail → [cbreg.md](../sparsecore/cbreg.md) |
| **Pipeline OperandWindow (memory sub-tile)** | `xla::jellyfish::SetWindowParams(…OperandWindow&, Shape const&, mlir::MemRefType, …)` @ `0x112658a0`; space tag stored at OperandWindow `+0x4C1` |
| **OperandWindow space tags** | `0x3` = `kVmem`, `0x5` = `kSmem`, `0xD` = HOST (read from `SetWindowParams`) |
| **Rotating PREDICATE window (true reg window)** | banks `SC_SCS_ROTATING_PREGS`, `SC_TEC_ROTATING_PREGS`; backend gate `TPUGfcSubtarget::hasRotatingPredicates()` @ `0x13c62c20` = `1` (Trillium-only) |
| **SREG overflow handling** | software spill to reserved SMEM (LSRA-v2); **no** hardware window, **no** overflow trap |
| **Negative search** | 0 hits: `SmemRegisterWindow` / `SregWindow` / `RegisterWindow` / `WindowBase` / `window_base` / `reg_window` / `SmemSpillRegister`; 0 TPU hits: `window_overflow` / `window_underflow` / `register_window_trap` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The Question, and the Negative Result

### Purpose

A reader arriving from a classical-architecture background reads "SMEM register-window" as one of two familiar things: (i) a SPARC-style sliding window of a large physical register file that maps a *window* of registers into the architectural names `r0..r31`, with hardware save/restore traps backed by a memory spill area; or (ii) a rotating register file (IA-64 style) where the architectural register name is offset by a window base that advances per loop iteration. Both would be "a window of memory/registers mapped into the scalar register file." Neither exists on TPU.

### What the binary says

The SPU scalar register file is flat. The encoder that turns a `ScalarYEncoding` operand into a physical SREG index, `platforms_deepsea::jellyfish::isa::ProtoUtils::EncodingToScalarRegister` (`0x1e871e40`), rejects any index above `0x1F`:

```c
// EncodingToScalarRegister @ 0x1e871e40 (decompiled, condensed)
if ( a2 > 0x1F )                 // index >= 32 is illegal
    LogFatal(/* "scalar register out of range" */);
return /* flat index a2 */;
```

There are exactly 32 architectural SREGs, addressed by a 5-bit field, with no window-base register added to the index and no bank selector multiplying the name space. The SparseCore register-bank taxonomy confirms this from the other direction: the scalar banks `SC_SCS_SREGS`, `SC_TAC_SREGS`, `SC_TEC_SREGS` exist in `.rodata`, and a search for any `SC_*_ROTATING_SREGS` / `SC_*_WINDOWED_SREGS` / `SC_*_BANKED_SREGS` bank returns **zero** hits. The predicate bank *does* have a rotating variant (§4); the scalar bank does not.

> **NOTE —** The [memory overview](overview.md) §1 states this in one line: "'register window' is a misnomer for every on-chip tier here. SMEM, CMEM, and SFLAG are all flat byte/word arrays … Scalar register windowing lives on the SREG file (allocated by LSRA-v2), and SMEM is merely its spill backing store." This page is the proof and the enumeration behind that sentence.

### The absence is exhaustive

The phrase's literal spellings are searched and absent across the entire binary:

| Searched token | Hits in `libtpu.so` | Meaning |
|---|---|---|
| `SmemRegisterWindow`, `SregWindow`, `SmemRegisterFile`, `SmemSpillRegister` | 0 | no windowed-SMEM register file is named |
| `RegisterWindow`, `RegWindow`, `WindowBase`, `window_base`, `reg_window` | 0 | no register-window primitive of any kind |
| `window_overflow`, `window_underflow`, `register_window_trap`, `save_restore_window`, `spill_window`, `fill_window` | 0 TPU (only `zlib_rs::deflate::fill_window`, unrelated) | no SPARC-style window-trap machinery |

The single `fill_window` hit belongs to the statically-linked `zlib_rs` and has nothing to do with registers. The absence of overflow/underflow/trap tokens is the decisive evidence: a windowed register file *requires* a spill/fill mechanism, and that mechanism is simply not present.

### Considerations

The reimplementer's takeaway is procedural, not just descriptive. Because the SREG file is flat and trap-free, SREG pressure beyond 32 live values is resolved by the compiler, not the hardware. The LSRA-v2 allocator spills to a reserved SMEM region (`FLAGS_xla_jf_lsra_v2_reserved_smem` @ `0x223afaa8`); a spilled SREG becomes a `ScalarStoreSmem*` to that region and a later `ScalarLoadSmem*` back — see [SMEM Scalar Memory](smem-scalar-memory.md) for the load/store opcode family. There is no hardware fast-path: the cost of a spill is two explicit scalar memory ops, and the compiler is responsible for minimizing it.

---

## 2. CBREG — A Register That Holds a Memory Window

### Purpose

The mechanism that best matches the *intent* of "a register that windows memory" is the SparseCore **CBREG** (circular-buffer register). It inverts the SPARC idea: instead of a window of memory mapped *into* the register name space, a single register *holds* a sliding window *onto* memory and emits a wrapping address stream. This is a v5+ SparseCore feature only (Viperfish `vfc`, Ghostlite `glc`, Trillium `gfc`); JF/DF/PF have no CBREG.

### Mechanism

A CBREG is three sub-registers — `{base, offset, size}` — physically stored in three separate hardware banks but addressed as one register through a 4-bit CBREG index. The bank names are present in `.rodata` for all three SparseCore sequencers:

```text
SC_SCS_CBREGS_BASE   SC_SCS_CBREGS_OFFSET   SC_SCS_CBREGS_SIZE
SC_TAC_CBREGS_BASE   SC_TAC_CBREGS_OFFSET   SC_TAC_CBREGS_SIZE
SC_TEC_CBREGS_BASE   SC_TEC_CBREGS_OFFSET   SC_TEC_CBREGS_SIZE
```

`base` is a windowed pointer into on-chip memory (the address space — SMEM vs TILE_SPMEM — is fixed when `base` is written); `size` is the modulus the offset wraps at; `offset` is the live position. An access reads or writes `base + (offset mod size)`. The `…PostUpdate` access variants additionally advance `offset = (offset + step) mod size` after the access — classic circular-buffer auto-increment. There is no modulo instruction; the wrap is a property of the OFFSET counter, and "overflow" is the wrap by design, never a trap.

The scalar-ALU opcodes that drive a CBREG are confirmed in the decompiled emitter glue:

| Op (symbol fragment) | Action | Where confirmed |
|---|---|---|
| `SparseCoreScalarAlu_ReadCbreg` | read a CBREG sub-field into an SREG | scalar-ALU op set (gfc/glc/vfc) |
| `SparseCoreScalarAlu_WriteCbreg` | write an SREG into a CBREG sub-field | scalar-ALU op set |
| `SparseCoreScalarAlu_AddCbreg` | add a delta to the CBREG offset | scalar-ALU op set |
| `SparseCoreScalarAlu_MoveCbreg` | copy a CBREG (gfc only) | scalar-ALU op set |
| `SparseCoreScalarAlu_ScalarLoadCircularBuffer` | load `base+(offset mod size)` | `isa_emitter::EmitScalarLoadOrStoreFromCb<…SparseCoreScsBundle…>` @ `0x13a5e560`; `…TacBundle…` @ `0x13a03d60` |
| `SparseCoreScalarAlu_ScalarStoreCircularBuffer` | store to the windowed address | `EmitScalarLoadOrStoreFromCb<…>` @ `0x13a5e8e0` |
| `SparseCoreScalarAlu_ScalarLoadCircularBufferPostUpdate` | load, then advance offset (wrap) | `EmitScalarLoadOrStoreFromCb<…>` @ `0x13a5e3a0` |
| `SparseCoreScalarAlu_ScalarStoreCircularBufferPostUpdate` | store, then advance offset (wrap) | `EmitScalarLoadOrStoreFromCb<…>` @ `0x13a5e720` |

The `EmitScalarLoadOrStoreFromCb` template is instantiated per SparseCore sequencer bundle type (`SparseCoreScsBundle`, `SparseCoreTacBundle`, and the TEC bundle), which is why the same four circular-buffer load/store shapes recur at distinct addresses — one set per engine.

### The compiler-facing surface

XLA drives the CBREG through LLVM intrinsics and an MLIR dialect, all present in the binary as strings/symbols: `llvm.tpu.allocate.cbreg` allocates one; `llvm.tpu.cbreg.add.offset[.in.place]` (MLIR `sc_tpu.advance_cb_offset[_in_place]`) advances the window; `llvm.tpu.rdcbreg.offset` / `llvm.tpu.rdcbreg.size` (MLIR `tpu_rdcbreg_offset` `create` @ `0x14734820`) read the live position and modulus. The lowering builds these inside the SparseCore pipeline emitters (§3) so a tile streams through a circular on-chip buffer under one auto-incrementing register window.

> **CROSS-REF —** The CBREG `{base,size,offset}` sub-register selector (`CbregMetadata` = {0=BASE,1=SIZE,2=OFFSET}), the exact scalar-ALU bit layout (opcode at bundle bit 154; operand slots at slot-relative bits 10/15/21), and the per-op opcode values (`Read=0x36`, `Write=0x35`, `Add=0x33`, `Move` primary `0x00`/sub `0x1b`) are owned by [CBREG Circular-Buffer Register](../sparsecore/cbreg.md). The opcode roster lives in [SCS Scalar Opcode Enumeration](../sparsecore/scalar-opcode-enum.md); the gather/scatter consumer in [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md). This page only fixes that a CBREG is a *register holding a memory window*, not a window onto registers.

### Considerations

A reimplementer must keep the direction straight. A CBREG occupies a dedicated hardware register triple, not an SREG slot — `ReadCbreg`/`WriteCbreg` *move between* SREGs and CBREG sub-fields. The window it describes lives in SMEM or TILE_SPMEM, and the modulus is software-set; if the underlying buffer is too small the lowering errors out (`min_circular_buffer_byte_count`, `circular_buffer_size` default 1000). The CBREG is the SparseCore's address-stream generator; it is to the embedding datapath what a DMA descriptor ring is to a NIC.

---

## 3. Pipeline OperandWindow — A Memory Sub-Tile That Can Live in SMEM

### Purpose

The second mechanism that co-occurs with "window" and "SMEM" is the pipeline **OperandWindow**: a sliding sub-tile of a tensor operand that the compiler stages in an on-chip memory space for a pipelined kernel. This is the most *literal* reading of "SMEM window" — an `OperandWindow` can be requested in SMEM — but it is a buffer window, not a register window.

### Mechanism

On the TensorCore fusion/conv side the window is configured by `xla::jellyfish::SetWindowParams(PipelineEmitter::OperandWindow&, Shape const&, mlir::MemRefType, …)` at `0x112658a0` (source `platforms/xla/mosaic/python/mosaic_windowing_util.cc`). The decompiled function reads the MLIR memory-space attribute off the operand's `MemRefType` and maps it to a one-byte space tag stored at `OperandWindow + 0x4C1` (`+1217`):

```c
// SetWindowParams @ 0x112658a0 (decompiled, condensed)
MemorySpace = mlir::MemRefType::getMemorySpace(&memref);
attr        = (MemorySpaceAttr) MemorySpace;
Value       = attr ? mlir::tpu::MemorySpaceAttr::getValue(attr) : 0;
if      (Value == 0)  v99 = 3;        // (null attr) -> kVmem
else if (Value == 1)  v99 = 5;        //              -> kSmem
else if (Value == 6)  v99 = 13;       //              -> HOST
else                                   //              -> reject
    return xla::InvalidArgument(
        "Operand windows can only be requested in VMEM, SMEM, or HOST");
*(uint8_t *)(operand_window + 1217) = v99;   // store space tag at +0x4C1
```

So the legal placements are tag `0x3` (`kVmem`), `0x5` (`kSmem`), and `0xD` (HOST); any other MLIR memory space is a compile-time `absl::InvalidArgument`. Two adjacent diagnostics in the same function pin the model further: `"Scalar windows not implemented."` (a scalar-shaped window is rejected) and `"An output window does not have a memory space assigned."` (an unassigned output window).

A second, sibling family lives on the SparseCore side: `xla::tpu::sparse_core::OperandWindow` and `PipelineEmitterInterface::OperandWindowDescription`, materialized by the `FixedWindowPipelineEmitter` (FWPE) and `VariableWindowPipelineEmitter` (VWPE). Their window buffer typically lands in SPMEM. The MLIR attribute that requests a window is `window_params`, validated by strings such as "expected %d window_params based on the number of inputs and outputs" and "the window shape specified in the `window_params` attribute must match the full operand shape in a persistent argument."

### The SREG connection (and why it is *not* a register window)

The one place an `OperandWindow` touches the scalar register file is its *offset*: the window's running offset is materialized in an SREG (`window_offset->ProducesSreg()`), then fed into scalar/DMA address generation. That is the correct mental model — the SPU *computes* a window offset into a scalar register; the window itself is the memory tile, not the register. The register holds a scalar value (the offset), exactly as it would for any address computation; nothing is windowed *over* the register file.

### FWPE: dynamic window sizes can be stored in SMEM

There is one more way SMEM and "window" co-occur. The flag `FLAGS_xla_sc_disable_fwpe_syncadds` (@ `0x223354e0`, cl::opt name `xla_sc_disable_fwpe_syncadds`) carries the help string, confirmed verbatim in `.rodata`:

> "Use TileSmem to store dynamic window sizes in fixed-window pipeline emitter instead of using sync-adds."

When set, the FWPE stores the *dynamic window sizes* in a TileSmem tile (`llvm.tpu.allocate.tilesmem` / `mlir::sparse_core::tpu_allocate_tilesmem::create` @ `0x146d7740`) rather than recomputing them via sync-add chains. This is window *metadata* living in SMEM — again, no register window is involved; SMEM here is a scratch table for size values.

> **GOTCHA — "window in SMEM" ≠ "register window."** An `OperandWindow` placed in `kSmem`, and FWPE window sizes cached in TileSmem, are both *data in SMEM*. Neither maps a window of SMEM into the SREG name space. A reimplementation that conflates "operand window may be requested in SMEM" with "the scalar register file has an SMEM window" will model a register-allocation mechanism that does not exist.

---

## 4. Rotating PREDICATE Registers — The Only True Register Window

### Purpose

There *is* a hardware register window on TPU. It is the only one — and it windows **predicate** registers, not scalar registers, and is not in SMEM. It exists because SparseCore software-pipelines its embedding/scatter loops, and rotating predicates implement the prolog/kernel/epilog of a pipelined loop without explicit prolog/epilog code.

### Mechanism

The SparseCore SCS and TEC sequencers carry both a flat predicate bank and a rotating one:

```text
SC_SCS_PREGS   + SC_SCS_ROTATING_PREGS     (rotating variant present)
SC_TEC_PREGS   + SC_TEC_ROTATING_PREGS     (rotating variant present)
SC_TAC_PREGS                                (NO rotating variant)
```

Note the TAC sequencer has *no* `SC_TAC_ROTATING_PREGS` bank — confirmed by the absence of that string. The rotating file is 16 entries (enum literals `SPARSECORE_ROTATE_PREDICATION_PREG0_IS_1 … PREG15_IS_1`), and the rotation is driven by scalar-ALU ops `SetRotatingPredicateRegisterH` and `BranchRelativeRotatingPregH` (field `branch_relative_rotating_preg`).

The compiler enables it per-generation through the LLVM backend, gated on the subtarget. The decompiled gate methods are unambiguous:

| Subtarget | `hasRotatingPredicates()` returns | Codename |
|---|---|---|
| `TPUGfcSubtarget` (@ `0x13c62c20`) | **`1`** | Trillium (v6e) |
| `TPUVfcSubtarget` (@ `0x13c5f1e0`) | `0` | Viperfish (v5e) |
| `TPUGlcSubtarget` | `0` | Ghostlite (v5p) |
| `TPUBcSubtarget` | `0` | BarnaCore |

So while the rotating-PREG *bank names* are present for SCS/TEC, the backend only *enables* rotation on Trillium. The [Predicate Slot](../isa/slot-predicate.md) page records the same fact ("Rotating predicates — Trillium-only — `TPUGfcSubtarget::hasRotatingPredicates()` = 1; all others 0"). Two flags govern use and emulation: `FLAGS_xla_sc_rotating_predicates` (@ `0x223359d8`) and `FLAGS_xla_sc_emulate_rotating_predicates` (@ `0x22335978`); the LLVM-side toggles are `enable-rotating-predicate` and `enable-rotating-predicate-emulation`, the latter described as folding prolog and epilog into the loop "but not actually using rotating predicate support."

> **CONFIDENCE — HIGH (refinement over raw evidence).** The raw finding noted gfc/glc-prefixed rotating-PREG op symbols and read "vfc shows the rotating-preg bank name." The decompiled `hasRotatingPredicates()` gate is more precise: only `TPUGfcSubtarget` returns `1`; `vfc`, `glc`, and the BarnaCore subtarget all return `0`. The bank *names* may be present across SCS/TEC, but the compiler emits rotation only on Trillium. Treat the bank-name presence as the ISA schema and the subtarget flag as the per-gen enablement.

### Considerations

This is the one place a reimplementer should model an actual sliding register window — but it sits in the predicate file of the SparseCore SCS/TEC sequencer, not in the SREG file and not in SMEM. The window position advances per loop iteration (modulo scheduling); the emulation mode unrolls the prolog/epilog instead of rotating. If a task says "TPU has a register window," this is the only correct referent, and it is predicate-only.

---

## 5. Reconciliation — Why All Sources Agree

### The taxonomy in one table

Per SparseCore sequencer, the resource banks present in 0.0.40 and whether each has a windowed variant:

| Bank | Resource | Windowed variant? | Windows what? |
|---|---|---|---|
| `SC_{SCS,TAC,TEC}_SREGS` | scalar registers (flat 32) | **NO** | — |
| `SC_{SCS,TEC,TAC}_DREGS`/VREGS | data / vector registers | NO | — |
| `SC_SCS_PREGS`, `SC_TEC_PREGS` | predicate registers | **YES** → `SC_{SCS,TEC}_ROTATING_PREGS` | predicate regs |
| `SC_TAC_PREGS` | predicate registers | NO | — |
| `SC_{SCS,TAC,TEC}_CBREGS_{BASE,OFFSET,SIZE}` | circular-buffer register triple | *is itself* a window | tile/scalar **memory** |
| `SC_{SCS,TAC,TEC}_SMEM` | scalar memory (flat) | NO | — |

The only "window onto registers" is the rotating predicate file; the only "register that *is* a window onto memory" is the CBREG triple; the SREG file and SMEM are both flat. That single table is the entire disambiguation.

### The three claims that looked like a conflict

The apparent tension between sibling documentation was purely terminological — "register window" was used for three different resource classes, none of them the SREG file:

| Source claim | Status | Why |
|---|---|---|
| The SPU has *no* register window | **TRUE** | flat 32 SREGs, 5-bit index, LSRA spill to SMEM; `EncodingToScalarRegister` bounds `idx ≤ 0x1F` |
| SMEM has no register-window machinery | **TRUE** | flat byte/word memory; the windows that touch SMEM are the CBREG memory-window and `OperandWindow` buffer placement, neither of which windows registers |
| MXU gains act as a stationary weight "window" | **TRUE as metaphor** | `GainLatchModeProto` latches weights into the systolic array; the binary uses *no* "window" term for it — unrelated to SMEM and to the SPU |

The MXU "GainLatch register window" deserves a one-line note for completeness: the weight-stationary gain registers are described as a "window" only descriptively. The binary names them `GainLatchModeProto` (`GAIN_LATCH_MODE_NONE` / `_NO_XPOSE_*` / `_XPOSE_*` / `_PACKED_*` …) and never uses "window." The gains are an MXU-internal register set, latched stationary across many matmuls — functionally a "window" of weights, but unrelated to the SPU SREG file and to SMEM. It must not be conflated with the SMEM/SREG question.

### Definitive answer

There is **no SMEM register-window** in the sense of a windowed or rotating *scalar* register file, and no register-window mapped over SMEM. The SREG file is a flat, hardware-bounded 32-entry file with software (LSRA-v2) spill to SMEM. The phrase points, at most, to: the **CBREG** (a register holding a `{base,offset,size}` memory window — the best fit), the pipeline **OperandWindow** (a memory sub-tile that may be staged *in* SMEM), or the rotating **PREDICATE** file (the only true hardware register window, predicate-only, Trillium-enabled). A reimplementation models all three as distinct mechanisms and leaves the SREG file flat.

---

## Cross-References

- [Memory Hierarchy Overview](overview.md) — §1 fixes "register window is a misnomer" for every on-chip tier; this page is the proof.
- [SMEM Scalar Memory](smem-scalar-memory.md) — the flat SMEM scalar model and the `ScalarLoad/StoreSmem*` family that backs SREG spills.
- [SFLAG Sync-Flag Tier](sflag-protocol.md) — the sibling flat on-chip tier; likewise not a register window.
- [SPU / Scalar Slot](../isa/slot-spu-scalar.md) — the 32-entry flat SREG file, the 5-bit field, and `EncodingToScalarRegister`'s `idx ≤ 0x1F` bound.
- [Predicate Slot](../isa/slot-predicate.md) — the scalar predicate file and the Trillium-only rotating-predicate gate.
- [CBREG Circular-Buffer Register](../sparsecore/cbreg.md) — the CBREG `{base,offset,size}` triple, `CbregMetadata` selector, and scalar-ALU op bit-layout.
- [SCS Scalar Opcode Enumeration](../sparsecore/scalar-opcode-enum.md) — the CBREG-op opcode values in the SparseCore scalar ISA.
- [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) — the gather/scatter consumer of the CBREG address window.
- [Memory-Space Assignment (MSA)](../compiler/msa-overview.md) — the HBM↔VMEM coloring pass; SMEM is *not* MSA-managed.
- Index entry: Part X — On-Chip Memory & DMA / Memory tiers — [back to index](../index.md)
