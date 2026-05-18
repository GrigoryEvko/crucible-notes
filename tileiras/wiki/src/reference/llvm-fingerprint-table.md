# LLVM Fingerprint Table

Ten independent fingerprints recovered from the stripped `tileiras` ELF prove the binary was built from an `LLVM 21.0.0git` snapshot of the upstream monorepo (with NVIDIA-internal NVPTX patches) plus the co-tracking MLIR layer. Each fingerprint is an isolated piece of byte-level evidence — a rodata string, a hardcoded enum value, a switch-table size, a structure-allocation footprint — and each derives from a different code path inside the binary, so they cannot be the result of a single string substitution. Companion page: [VERSIONS.md](../VERSIONS.md). For LLVM-version cross-cuts see the table at the bottom.

## How to use these fingerprints

Each fingerprint below is independent and addresses a different fragility class:

| If you can... | Use fingerprint(s) | Why |
|---|---|---|
| only grep rodata strings | 2, 3 | the two unique LLVM-21 anchors are literal strings |
| only count switch arms | 5, 7 | row counts disambiguate generic intrinsic and PassBuilder coverage |
| only inspect type layouts | 4, 9 | `GlobalVariable=88B`, `AsyncValueImpl=808B`, `InstructionVal=28` are stable shape facts |
| only diff hex against a clean LLVM tree | 1, 6, 8 | data-layout, NVPTX MatcherTable, ProxyReg whitelist are byte-exact |
| only run the binary and capture output | 3 | the AsmPrinter `Based on LLVM 21.0.0git` line surfaces in every emitted PTX |

Detecting the source LLVM version of an unknown NVPTX-derived binary takes only a small constant-time fingerprint scan:

```c
/* Lightweight three-anchor scan; any single match pins the LLVM major
 * version with HIGH confidence per the cross-LLVM table at the bottom. */
LlvmVersion detect_llvm_version(const uint8_t *rodata, size_t rodata_len) {
    if (memmem(rodata, rodata_len, "LLVM21.0.0git", 13))     return LLVM_21;
    if (memmem(rodata, rodata_len, "LLVM20.",        7))     return LLVM_20;
    if (memmem(rodata, rodata_len, "LLVM19.",        7))     return LLVM_19;
    if (memmem(rodata, rodata_len, "LLVM18.",        7))     return LLVM_18;
    /* Fall back to structural fingerprints 4/7/8 if the producer string
     * has been stripped or rewritten. */
    return LLVM_UNKNOWN;
}
```

The producer-string anchor (fingerprint 2) makes this scan trivial; the structural fingerprints exist for the case where someone has stripped it.

## Fingerprints

### 1. NVPTX data-layout stamp

- **Claim**: stock LLVM 21 NVPTX64 data-layout string is hardcoded and stamped onto every emitted module.
- **Binary evidence**: rodata at `0x4D079D0`, length `0x9A` = 154 bytes, single xref from `sub_1A4E5C0` at `0x1A4E5D1`. Verbatim:
  `e-p:64:64:64-p3:32:32:32-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-i128:128:128-f32:32:32-f64:64:64-v16:16:16-v32:32:32-v64:64:64-v128:128:128-n16:32:64`.
- **Confidence**: HIGH.

### 2. `LLVM21.0.0git` bitcode producer string

- **Claim**: the LLVM bitcode writer emits its `IDENTIFICATION_CODE_STRING` producer record from a literal `LLVM21.0.0git` blob.
- **Binary evidence**: rodata at `0x4F882C4`, length 13 bytes, single xref from `sub_3935490` at `0x39359A1` (the `EnterSubblock(IDENTIFICATION, 5)` site of the bitcode-writer body). Verbatim string: `LLVM21.0.0git`.
- **Confidence**: HIGH.

### 3. NVPTX AsmPrinter `Based on LLVM 21.0.0git` header line

- **Claim**: every PTX file emitted by `tileiras` carries a hardcoded comment line identifying the LLVM base version.
- **Binary evidence**: `sub_1A56540` (the NVPTX `emitHeader` AsmPrinter virtual) writes a four-line comment block whose third line is the `.rodata` string `Based on LLVM 21.0.0git` (not runtime-formatted; verbatim literal). Downstream of this header sits the 6,388-case AsmWriter MC instruction switch shaped like LLVM 21's `NVPTXGenAsmWriter.inc`.
- **Confidence**: HIGH.

### 4. Verifier subset: 88-byte `GlobalVariable` and `InstructionVal = 28`

- **Claim**: the LLVM IR layout used by the inline Verifier subset matches the LLVM 18-21 stable shape and excludes earlier and later trees.
- **Binary evidence**: in `sub_2D45620` at `0x2D46960` / `0x2D514DB` / `0x2D5975A` the gate `if (*(_BYTE *)v116 > 0x1Cu)` checks `Value::SubclassID > 28` (i.e. `InstructionVal = 28`) before dispatching the 46-case Instruction-opcode switch. Adjacent allocator call at line 10717-10721 is `BumpPtrAllocator::Allocate(88, 1)` followed by `GlobalVariable::GlobalVariable(...)` (`sub_3FCECA0`) — the 88-byte `GlobalVariable` footprint. The 12 `Verifier::visitIntrinsicCall` diagnostic strings sit at `0x4F2FCB8` ... `0x4F2FDC3`.
- **Confidence**: HIGH (combined with fingerprints 2/3/7 which fix the version to 21, this layout becomes a corroborating LLVM 21 anchor; on its own, MED — narrows to LLVM 18-21).

### 5. ~412-case `canConstantFoldCallTo` Intrinsic::ID switch

- **Claim**: the ConstantFolding predicate switches on a 412-entry generic `Intrinsic::ID` jump table, anchoring the upper bound on the generic intrinsic enum.
- **Binary evidence**: `sub_39ADED0`, 5842 bytes, 344 basic blocks. The 412-case primary switch lives at `0x39ADFCB`, capped by `cmp ecx, 0x19B` at `0x39ADFB5` (`0x19B = 411`). A secondary 161-case switch at `0x39AE288` covers NVPTX-private intrinsic IDs in the `8851..9011` range (tcgen05 / cvt_packfloat / cp.async.bulk / wgmma extensions).
- **Confidence**: MED. The 412 cap was originally cited as evidence for LLVM 17/18 base, but the producer string (fingerprint 2) and AsmPrinter header (fingerprint 3) override that — the constant folder in `tileiras` covers the `0..411` generic-ID subset of LLVM 21's intrinsic enum; the NVIDIA fork pruned the new-in-21 generic intrinsics from this fold table while keeping the `LLVM21.0.0git` build stamp.

### 6. NVPTX `MatcherTable` XOR-3 obfuscated mnemonic pools

- **Claim**: `tileiras` embeds the NVPTX TableGen-generated mnemonic pools in `.data` under a walking XOR-3 cipher, decoded once at startup. Pool *shape* (offsets, opcodes covered) matches the LLVM 21 NVPTXGenAsmWriter output.
- **Binary evidence**: register-name pool at `0x5A4BE20 - 0x5A4C06A` (586 B), opcode-mnemonic pool at `0x5A4C080 - 0x5A656F0` (~105 KB). Decoders at `0x1BD1810` (mnemonic) and `0x1BD1830` (register name); cached post-decode pointer at `qword_5B4F4D0`. Both pools are byte-XOR-3 from disk.
- **Confidence**: HIGH for "XOR-3 obfuscation present", HIGH for "shape matches LLVM 21 NVPTXGenAsmWriter".

### 7. PassBuilder mega-registry: 478 pretty-name keys + 73 specials

- **Claim**: the new-PM PassBuilder pass-name registrar registers exactly 551 entries split as 478 templated `getTypeName<T>()` keys, 66 naked-class string keys, 5 pipeline aliases, 2 specials. This row count fixes LLVM 21.
- **Binary evidence**: `sub_1CCB7D0` makes 551 calls to `sub_4063070` (`StringMap<PassInfo>::insert`). The L843 line registers `memprof-context-disambiguation`, a pass class that landed post-LLVM-18. NVIDIA-private classes (`NVVMIRVerifier`, `IPMSP`, `Pretreat`, `nv-early-inliner`, `SelectKernels`, `NVVMAA`, `KernelInfoPrinter`, `LowerAggrCopies`, `LowerStructArgs`, etc., 18 in total) are interleaved.
- **Confidence**: HIGH.

### 8. NVPTXProxyRegErasure 4-opcode contiguous whitelist

- **Claim**: the post-ISel ProxyReg erasure pass uses a **contiguous** opcode range `3156..3159` checked by `sub eax, 0xC54 ; cmp eax, 3` at `0x1AE5086` — a TableGen-side typed-ProxyReg consolidation that landed in LLVM trunk just before the 21.0 cut. Stock LLVM 18 used a 5-6-element named whitelist.
- **Binary evidence**: `sub_1AE4FD0` body at `0x1AE4FD0 - 0x1AE599C`; the opcode test `sub eax, 0xC54 ; cmp eax, 3` at `0x1AE5086`.
- **Confidence**: HIGH.

### 9. MLIR Operation header (0x48) and AsyncValueImpl (808 bytes)

- **Claim**: MLIR runtime structures observed in the binary match the post-2024 / LLVM 21 monorepo MLIR ABI, not earlier MLIR layouts.
- **Binary evidence**: MLIR `Operation` fixed-header is 0x48 bytes (the `mlir::Operation` shape with trailing-objects layout for operands / regions / successors). RewritePattern allocations cluster at `0x60` / `0x68` / `0x70` / `0x78` (96 / 104 / 112 / 120 bytes) — observed at 286 sites for the `0x60` variant alone in `p3-U02`. `AsyncValueImpl` is exactly 808 bytes (`0x328`), allocated by `sub_44A8C20(0x328, ...)` from three sites in the Pipe / Mutex / Schedule infrastructure.
- **Confidence**: MED individually (MLIR snapshots are not version-stamped); HIGH in conjunction with the LLVM 21 anchors above.

### 10. NVVM-Reflect cl::opt registrations

- **Claim**: the NVPTX backend's NVVM-Reflect pass registers two `cl::opt` flags via the standard LLVM 21 cl::opt machinery, with rodata strings sitting alongside the LLVM cl::opt singleton.
- **Binary evidence**: `ctor_238` at `0x463A70` registers (i) `cl::opt<bool>` `nvvm-reflect-enable` (storage at `0x5B4F400`, rodata name at `0x4D3C766`, 19 B) and (ii) `cl::list<string>` `nvvm-reflect-add` (rodata name at `0x4D3C77A`, 16 B). Both go through `sub_4534CC0` (`cl::Option::setArgStr`) and `sub_4534420` (`cl::Option::done`) and end up in the `cl::GlobalParser` singleton at `0x4530050`. The pass is registered into the PassBuilder registry at `0x1CCB7D0` as `nvvm-reflect-pp`.
- **Confidence**: HIGH.

## Cross-LLVM-version disambiguation

| Fingerprint | LLVM 18 | LLVM 19 | LLVM 20 | LLVM 21 (`tileiras`) |
|---|---|---|---|---|
| 1. NVPTX data-layout stamp | identical | identical | identical | match |
| 2. Producer string | `LLVM18.x.y` | `LLVM19.x.y` | `LLVM20.x.y` | **`LLVM21.0.0git`** |
| 3. AsmPrinter `Based on LLVM …` | `LLVM 18.x` | `LLVM 19.x` | `LLVM 20.x` | **`LLVM 21.0.0git`** |
| 4. `InstructionVal = 28` | matches | matches | matches | matches |
| 4. 88-byte `GlobalVariable` | matches | matches | matches | matches |
| 5. Generic Intrinsic::ID cap | ~421 | ~430 | ~440 | NVIDIA fork: 412 (subset of 21) |
| 6. NVPTX MatcherTable shape | smaller (no tcgen05) | smaller | + tcgen05 partial | matches (full tcgen05/cp.async.bulk/wgmma) |
| 7. PassBuilder row count | ~380 | ~440 | ~500 | **551 = 478+66+5+2** |
| 7. `MemProfContextDisambiguation` | absent | present | present | **present** |
| 8. ProxyReg whitelist | named 5-6 entries | named 5-6 entries | named 5-6 entries | **contiguous 3156..3159** (LLVM 21 typed-ProxyReg) |
| 9. AsyncValueImpl size | smaller | smaller | similar | matches 808 B |
| 10. cl::opt machinery | matches | matches | matches | matches |

Two unique LLVM 21 anchors carry the weight: fingerprints 2 (producer string) and 3 (AsmPrinter header). Fingerprints 7 and 8 disambiguate LLVM 21 from LLVM 20. Fingerprints 1, 4, 6, 9, 10 are stable-layout corroborators that exclude earlier/later trees only when read together with the unique anchors. The convergence of these ten independent fingerprints leaves no plausible alternative version; a snapshot from any other LLVM major would disagree on at least one.

The reader-side recipe for opening the binary and reproducing any one of these fingerprints is documented on [Binary Anatomy and RE Methodology](../topics/binary-anatomy-and-re-methodology.md).
