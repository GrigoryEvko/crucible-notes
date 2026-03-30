# Bitcode Reader/Writer

CICC v13.0 contains the complete LLVM 20.0.0 bitcode serialization infrastructure -- reader, writer, metadata loader, module summary IO, and the full intrinsic upgrader -- spread across two address ranges. The `0x9F0000`--`0xA2FFFF` range hosts a first copy of the bitcode reader/writer core used by the standalone libNVVM pipeline, while the `0x1500000`--`0x157FFFF` range hosts the primary copy used by the two-phase compilation path. Both copies are structurally identical LLVM `BitcodeReader.cpp` and `BitcodeWriter.cpp` compiled at different link addresses. The reader is stock upstream LLVM 20.0.0 with no NVIDIA modifications to the deserialization logic itself. The writer, however, contains a single critical NVIDIA change: it stamps `"LLVM7.0.1"` as the bitcode producer identification string rather than the true `"LLVM20.0.0"`, preserving backward compatibility with the NVVM IR ecosystem.

The bitcode subsystem sits at the boundary between all pipeline stages. The [standalone pipeline](../pipeline/entry.md) validates magic bytes on entry, the [module linker](../lto/index.md) reads bitcode from separate compilation objects, the [two-phase orchestrator](../pipeline/emission.md) serializes per-function bitcode blobs between Phase I and Phase II, and the [NVVM container](../structs/nvvm-container.md) wraps bitcode payloads in a proprietary envelope. Every bitcode load also runs the intrinsic upgrader -- a 700+ KB AutoUpgrade subsystem that includes roughly 240 KB of effectively-dead x86 intrinsic renaming tables.

## Key Facts

| | |
|---|---|
| **Reader (primary copy)** | `sub_151B070` (`0x151B070`, 123 KB) -- parseFunctionBody |
| **Reader (standalone copy)** | `sub_9F2A40` (`0x9F2A40`, 185 KB) -- parseFunctionBody |
| **Writer** | `sub_1538EC0` (`0x1538EC0`, 58 KB) -- writeModule |
| **Metadata reader** | `sub_A09F80` (`0xA09F80`, 121 KB) -- MetadataLoader::parseOneMetadata |
| **X86 AutoUpgrade (name)** | `sub_156E800` (`0x156E800`, 593 KB) -- UpgradeIntrinsicFunction |
| **X86 AutoUpgrade (call)** | `sub_A939D0` (`0xA939D0`, 457 KB) -- UpgradeIntrinsicCall |
| **NVVM version checker** | `sub_157E370` (`0x157E370`, 7 KB) |
| **NVVM version checker (standalone)** | `sub_12BFF60` (`0x12BFF60`, 9 KB) |
| **Producer init (ctor_036)** | `0x48CC90` (544 bytes) -- reads `LLVM_OVERRIDE_PRODUCER` |
| **Producer init (ctor_154)** | `0x4CE640` (215 bytes) -- reads `LLVM_OVERRIDE_PRODUCER` |
| **Address range (primary)** | `0x1500000`--`0x157FFFF` |
| **Address range (standalone copy)** | `0x9F0000`--`0xA2FFFF` |
| **Address range (AutoUpgrade)** | `0xA80000`--`0xABFFFF` |
| **Hardcoded producer string** | `"LLVM7.0.1"` (writer), `"20.0.0"` (internal fallback) |
| **NVVM IR version gate** | major == 3, minor <= 2 |
| **Upstream source** | `lib/Bitcode/Reader/BitcodeReader.cpp`, `lib/Bitcode/Writer/BitcodeWriter.cpp`, `lib/IR/AutoUpgrade.cpp` |

## Bitcode Format Basics

LLVM bitcode uses two magic signatures. The pipeline validates both at module load time:

| Magic Bytes | Meaning | Where Checked |
|---|---|---|
| `0xDE 0xC0 0x17 0x0B` | Raw LLVM bitcode stream | `sub_12C06E0` (module linker) |
| `0x42 0x43 0xC0 0xDE` | Bitcode wrapper format (offset + size header around raw stream) | Same function |

If neither signature matches, the pipeline sets `*error_code = 9` ("invalid bitcode") and aborts. The wrapper format is more common in practice -- `nvcc` generates wrapper-format `.bc` files that embed the raw stream at an offset specified in the wrapper header. The wrapper header is 20 bytes:

```c
struct BitcodeWrapperHeader {
    uint32_t magic;       // 0x42, 0x43, 0xC0, 0xDE
    uint32_t version;     // wrapper version (0)
    uint32_t offset;      // byte offset to raw bitcode within file
    uint32_t size;        // size of raw bitcode in bytes
    uint32_t cpu_type;    // target CPU type (0 for NVPTX)
};
```

After magic validation, the bitstream enters the block-structured reader. LLVM bitcode is organized into nested blocks, each identified by a block ID. The reader uses abbreviation tables (defined in BLOCKINFO blocks) to decode records within each block efficiently using variable-bit-rate (VBR) encoding.

An epoch check runs after magic validation: `"Incompatible epoch: Bitcode '<X>' vs current: '<Y>'"`. This ensures the bitcode was produced by a compatible LLVM generation.

## Bitcode Reader

### Module Parser (`sub_1505110`, 60 KB)

The top-level entry reads `MODULE_BLOCK` records from the bitcode stream. It processes:

- Global variable declarations and definitions
- Function declarations (bodies are deferred for lazy materialization)
- Calling conventions and comdat groups
- Module-level metadata, type tables, and value symbol tables
- Data layout and target triple strings

Error strings: `"Invalid calling convention ID"`, `"Invalid function comdat ID"`, `"Invalid global variable comdat ID"`, `"Invalid type for value"`.

### parseFunctionBody (`sub_151B070` / `sub_9F2A40`)

The function body parser is the largest single reader function. The standalone copy `sub_9F2A40` is 185 KB (5,706 decompiled lines) with 174 error string references. The primary copy `sub_151B070` is 123 KB. Both decode the same `FUNCTION_BLOCK` records:

- **57 FUNC_CODE instruction record types** (switch cases 1--65), covering every LLVM IR opcode: `INST_BINOP`, `INST_CAST`, `INST_GEP`, `INST_SELECT`, `INST_CMP`, `INST_RET`, `INST_BR`, `INST_SWITCH`, `INST_INVOKE`, `INST_CALL` (opcode 85), `INST_UNREACHABLE`, `INST_PHI`, `INST_ALLOCA`, `INST_LOAD`, `INST_STORE`, `INST_ATOMICRMW`, `INST_CMPXCHG`, `INST_FENCE`, `INST_EXTRACTVAL`, `INST_INSERTVAL`, `INST_LANDINGPAD`, `INST_RESUME`, `INST_CLEANUPPAD`, `INST_CATCHPAD`, `INST_CATCHSWITCH`, `INST_CALLBR`, `INST_FREEZE`, and others.
- **4 nested sub-blocks**: constants (`0xB`), metadata (`0xE`), use-list order (`0x10`), operand bundles (`0x12`).
- **53 unique error strings** including: `"Alignment value is too large"`, `"Invalid record"`, `"Invalid record: Unsupported version of DISubrange"`, `"METADATA_NAME not followed by METADATA_NAMED_NODE"`.

For each `INST_CALL` record (opcode 85), the reader calls into the AutoUpgrade machinery to rename deprecated intrinsics. This is the hook that triggers the 700+ KB x86 upgrader on every call instruction -- even though the upgrader's x86 branches are dead code for NVPTX targets.

Pseudocode for the top-level body parse loop:

```cpp
Error parseFunctionBody(Function *F) {
    SmallVector<uint64_t, 64> Record;
    while (true) {
        BitstreamEntry Entry = Stream.advance();
        switch (Entry.Kind) {
        case BitstreamEntry::Error:
            return error("Malformed block");
        case BitstreamEntry::EndBlock:
            return resolveForwardRefs();
        case BitstreamEntry::SubBlock:
            switch (Entry.ID) {
            case CONSTANTS_BLOCK_ID:  // 0xB
                parseConstants(); break;
            case METADATA_BLOCK_ID:   // 0xE
                parseMetadataAttachment(); break;
            case USELIST_BLOCK_ID:    // 0x10
                parseUseListBlock(); break;
            case OPERAND_BUNDLE_TAGS_BLOCK_ID: // 0x12
                parseOperandBundleTags(); break;
            }
            break;
        case BitstreamEntry::Record:
            unsigned Code = Stream.readRecord(Entry.ID, Record);
            switch (Code) {
            case FUNC_CODE_INST_BINOP: /* ... */ break;
            case FUNC_CODE_INST_CAST:  /* ... */ break;
            // ... 55 more cases ...
            case FUNC_CODE_INST_CALL:
                // Parse callee, args, calling convention
                // If callee is intrinsic:
                //   UpgradeIntrinsicFunction(callee, &newCallee);
                //   if (newCallee) UpgradeIntrinsicCall(CI, newCallee);
                break;
            }
        }
    }
}
```

### Lazy Materialization (`sub_1503DC0`, 13 KB)

Function bodies are not parsed eagerly. The module parser records each function's byte offset in the bitcode stream, and `materializeFunctions` seeks to that position on demand. Error strings: `"Could not find function in stream"`, `"Expect function block"`, `"Expect SubBlock"`, `"Trying to materialize functions before seeing function blocks"`. The [two-phase compilation](../pipeline/emission.md) exploits this by materializing individual functions for per-function Phase II optimization.

### Bitstream Infrastructure

| Function | Address | Size | Role |
|----------|---------|------|------|
| `readBlockInfoBlock` | `0x150F8E0` | 42 KB | Reads BLOCKINFO block (abbreviation definitions) |
| `readAbbreviatedField` | `0x1510D70` | 38 KB | Expands abbreviated records (fixed, VBR, array, blob) |
| `readAbbrevRecord` | `0x1513230` | 20 KB | Reads one abbreviation-defined record |
| `readRecord` | `0x150E2B0` | 19 KB | Core `BitstreamCursor::readRecord` |
| `parseMetadataBlock` | `0x1518180` | 29 KB | Parses METADATA_BLOCK for function-level metadata |
| `parseFunctionMetadata` | `0x1520420` | 32 KB | Metadata/value-table builder during function parse |
| `parseMetadataStrings` | `0x1522160` | 13 KB | Reads metadata string table |
| `parseTypeBlock / constants` | `0x15083D0` | 26 KB | TYPE_BLOCK or CONSTANTS_BLOCK parser |
| `parseValueRecord` | `0x1515740` | 9 KB | Value record decoder |
| `string table reader` | `0x15140E0` | 13 KB | Bitcode string table entries |
| `readBlobRecord` | `0x1514C40` | 9 KB | Blob-type record reader |
| `skipBlock` | `0x15127D0` | 13 KB | Block skipping and cursor navigation |
| `parseModuleSummaryIndex` | `0x150B5F0` | 63 KB | ThinLTO summary parser |
| `materializeFunctions` | `0x1503DC0` | 13 KB | Lazy function body materialization |
| `parseModule` | `0x1505110` | 60 KB | Top-level MODULE_BLOCK parser |
| `ThinLTO GUID lookup` | `0x150A160` | 7 KB | GUID-based summary index lookup |
| `parseGlobalInits` | `0x1504A60` | 8 KB | Global variable initializer parser |

## Bitcode Writer

### writeModule (`sub_1538EC0`, 58 KB)

The top-level writer serializes an entire `Module` to a bitcode stream. It orchestrates sub-writers in a fixed order:

1. Enumerate all values via `ValueEnumerator` (`sub_15467B0`, 23 KB)
2. Write identification block (with producer string -- see next section)
3. Write MODULE_BLOCK header
4. Write type table (`sub_1530240`, 12 KB)
5. Write attribute groups (`sub_152F610`, 8 KB)
6. Write global variables
7. Write function declarations
8. For each defined function: `writeFunction` (`sub_1536CD0`, 40 KB)
9. Write metadata (`sub_1531F90`, 27 KB) + metadata records (`sub_15334D0`, 8 KB)
10. Write value symbol table (`sub_1533CF0`, 16 KB)
11. Write named metadata / comdat records (`sub_15311A0`, 14 KB)
12. If ThinLTO: write module summary (`sub_1535340`, 26 KB)

### writeFunction (`sub_1536CD0`, 40 KB)

Writes one `FUNCTION_BLOCK` containing all instructions, each encoded via `writeInstruction` (`sub_1528720`, 27 KB). Instructions are encoded as (opcode, operand_ids...) records where operand IDs are relative to the value table. The writer uses abbreviations for compact encoding of common instruction patterns.

### Value Enumeration

Before writing, the `ValueEnumerator` assigns a dense numeric ID to every value in the module. This is the reverse of what the reader does (mapping IDs back to Values).

| Function | Address | Size | Role |
|----------|---------|------|------|
| `enumerateModule` | `0x15467B0` | 23 KB | Top-level module enumeration |
| `enumerateValues` | `0x1542B00` | 26 KB | Assigns numeric IDs to all values |
| `optimizeConstants` | `0x1548410` | 8 KB | Reorders constants for better compression |
| `TypeFinder helper` | `0x153E1D0` | 7 KB | Recursive type discovery |

### Writer Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| `writeModule` | `0x1538EC0` | 58 KB | Top-level module serializer |
| `writeFunction` | `0x1536CD0` | 40 KB | Per-function FUNCTION_BLOCK writer |
| `writeMetadata` | `0x1531F90` | 27 KB | METADATA_BLOCK writer |
| `writeInstruction` | `0x1528720` | 27 KB | Single instruction encoder |
| `writeModuleSummary` | `0x1535340` | 26 KB | ThinLTO summary serializer |
| `writeValueSymbolTable` | `0x1533CF0` | 16 KB | VALUE_SYMTAB_BLOCK writer |
| `writeNamedMetadata` | `0x15311A0` | 14 KB | Named metadata / comdat writer |
| `writeType / globalVar` | `0x1530240` | 12 KB | Type descriptors or global variable records |
| `emitAbbreviation` | `0x152AB40` | 11 KB | Abbreviation definition writer |
| `emitRecord` | `0x152A250` | 9 KB | Low-level record emission |
| `writeConstants helper` | `0x1527BB0` | 9 KB | Constant value encoder |
| `writeMetadataRecords` | `0x15334D0` | 8 KB | Dispatcher for 37 metadata node types |
| `writeAttributeGroup` | `0x152F610` | 8 KB | ATTRIBUTE_GROUP_BLOCK writer |
| `emitVBR` | `0x15271D0` | 7 KB | Variable bit-rate integer encoding |
| `emitCode` | `0x15263C0` | 7 KB | Core abbreviated/unabbreviated record emission |
| `emitBlob` | `0x1528330` | -- | Blob data emission |

## Producer String Hack

This is the single most important NVIDIA deviation in the bitcode subsystem. Two global constructors cooperate to set the producer identification string:

**`ctor_036` at `0x48CC90`** (544 bytes): Reads `LLVM_OVERRIDE_PRODUCER` from the environment. If unset, falls back to the string `"20.0.0"` (the true LLVM version). Stores the result in the global `qword_4F837E0`. Also registers `disable-bitcode-version-upgrade` (`cl::opt<bool>`).

**`ctor_154` at `0x4CE640`** (215 bytes): Also reads `LLVM_OVERRIDE_PRODUCER`. Falls back to `"7.0.1"`. Stores into a separate global.

When `writeModule` (`sub_1538EC0`) writes the IDENTIFICATION_BLOCK, it emits the string `"LLVM7.0.1"` as the producer. This is assembled from the prefix `"LLVM"` plus the version string `"7.0.1"` loaded from the ctor_154 global.

The consequence is that any tool reading CICC's output bitcode (including older libNVVM, nvdisasm, or third-party NVVM IR consumers) sees producer `"LLVM7.0.1"` and interprets the bitcode as LLVM 7.x-era IR. Internally, the IR is LLVM 20.0.0 -- all modern instruction opcodes, metadata formats, and type encodings are present. The producer string is purely a compatibility marker that tells downstream tools which NVVM IR version spec to apply, not the actual LLVM version.

Why `7.0.1` specifically: NVVM IR 2.0 was defined against LLVM 7.0.1. The NVVM toolchain ecosystem (libNVVM, nvcc's device compilation pipeline) standardized on this version string as the "NVVM IR format identifier." Upgrading the producer string would require coordinated changes across the entire CUDA toolkit and all consumers.

```c
// Pseudocode for producer string initialization
static const char *producer_version;

void ctor_036() {  // at 0x48CC90
    const char *env = getenv("LLVM_OVERRIDE_PRODUCER");
    if (!env) env = "20.0.0";  // true LLVM version
    global_4F837E0 = env;
    // Also registers: -disable-bitcode-version-upgrade (cl::opt<bool>)
}

void ctor_154() {  // at 0x4CE640
    const char *env = getenv("LLVM_OVERRIDE_PRODUCER");
    if (!env) env = "7.0.1";   // NVVM IR compat marker
    producer_version = env;
}

// In writeModule (sub_1538EC0):
void writeIdentificationBlock(BitstreamWriter &Stream) {
    Stream.EnterSubblock(IDENTIFICATION_BLOCK_ID);
    // Writes: "LLVM" + producer_version → "LLVM7.0.1"
    Stream.EmitRecord(IDENTIFICATION_CODE_STRING, "LLVM");
    Stream.EmitRecord(IDENTIFICATION_CODE_EPOCH, CurrentEpoch);
    Stream.ExitBlock();
}
```

**Reimplementation note**: A reimplementation must write `"LLVM7.0.1"` as the producer for compatibility with the existing NVVM ecosystem. Setting `LLVM_OVERRIDE_PRODUCER` to a different value will change the embedded string. The `disable-bitcode-version-upgrade` flag controls whether the reader's AutoUpgrade logic activates for version-mismatched bitcode.

## X86 AutoUpgrade -- Why to Skip It

The intrinsic upgrader is the single largest code mass in the entire cicc binary. Two functions dominate:

| Function | Address | Size | Role |
|----------|---------|------|------|
| `UpgradeIntrinsicFunction` | `sub_156E800` | 593 KB | Name-based intrinsic rename lookup (271 string patterns) |
| `UpgradeIntrinsicCall` | `sub_A939D0` | 457 KB | Call instruction rewriter |
| `X86 intrinsic upgrade helper` | `sub_A8A170` | 195 KB | SSE/AVX/AVX-512 family tables |
| `UpgradeIntrinsicCall (2nd copy)` | `sub_15644B0` | 89 KB | Companion call upgrader |
| `NVVM upgrade dispatcher` | `sub_A8E250` | 52 KB | nvvm.atomic, nvvm.shfl, nvvm.cp.async, nvvm.tcgen05, nvvm.cluster, nvvm.ldg |
| `NVVM call rewriting` | `sub_A91130` | 28 KB | NVVM-specific call rewriter |
| `NVVM annotation metadata upgrade` | `sub_A84F90` | 14 KB | maxclusterrank, maxntid, etc. |
| `UpgradeModuleFlags` | `0x156C720` | 10 KB | Module flag upgrader |
| `UpgradeLoopMetadata` | `0x156A1F0` | 7 KB | llvm.loop.interleave.count, llvm.loop.vectorize.* |

**Total intrinsic upgrader code**: approximately 1.4 MB across all copies and helpers.

The x86 portion (roughly 1.0 MB) handles SSE/SSE2/SSE4.1/SSE4.2/SSSE3, AVX2, AVX-512 (mask operations, conversions, FMA variants), and ARM NEON patterns (`^arm\.neon\.vld`, `^arm\.neon\.vst`). These branches are functionally dead for NVPTX -- no CUDA program will ever contain an `@llvm.x86.sse2.padds.b` intrinsic. However, the code is NOT unreachable in the CFG sense: the reader calls `UpgradeIntrinsicFunction` on every intrinsic name, the function does a string-prefix match, and falls through the x86/ARM branches without matching. The x86 code paths simply never activate.

**Reimplementation guidance**: You can safely exclude the x86 and ARM AutoUpgrade tables (sub_A8A170, the x86 portions of sub_A939D0, and the ARM patterns in sub_15644B0). The NVVM-relevant upgraders must be preserved:

| Preserved | NVVM Intrinsic Families |
|-----------|------------------------|
| `sub_A8E250` | `nvvm.atomic.*`, `nvvm.shfl.*`, `nvvm.cp.async.*`, `nvvm.tcgen05.*`, `nvvm.cluster.*`, `nvvm.ldg.*` |
| `sub_A91130` | NVVM-specific call rewrites |
| `sub_A84F90` | NVVM annotation metadata (maxclusterrank, maxntid, etc.) |
| `sub_156A1F0` | Loop vectorization metadata (llvm.loop.interleave.count) |
| `sub_156C720` | Module flags |

Stripping the x86 upgrader saves approximately 1.0 MB of binary size and significant reverse-engineering effort, with zero functional impact on GPU compilation.

## Metadata Reader

### MetadataLoader::parseOneMetadata (`sub_A09F80`, 121 KB)

The metadata reader handles 42 distinct metadata record types in a single switch statement. Each case constructs one metadata node:

- DI metadata nodes: `DISubprogram`, `DIFile`, `DICompileUnit`, `DIVariable`, `DILocation`, `DIType`, `DIExpression`, `DISubrange`, `DIEnumerator`, `DIGlobalVariableExpression`, `DIModule`, `DINamespace`, `DITemplateTypeParameter`, `DITemplateValueParameter`, `DICompositeType`, `DIDerivedType`, `DIBasicType`, `DILexicalBlock`, `DILexicalBlockFile`, `DILabel`, `DIImportedEntity`, `DIMacro`, `DIMacroFile`, `DICommonBlock`, `DIGenericSubrange`, `DIStringType`, `DIArgList`
- LLVM metadata nodes: `MDTuple`, `MDString`, named metadata
- NVVM annotations: `nvvm.annotations` (parsed as named metadata carrying per-kernel attributes)

The function is called from `parseMetadataBlock` (`sub_1518180`, 29 KB), which reads the block structure, and `parseFunctionMetadata` (`sub_1520420`, 32 KB), which processes function-level metadata attachments.

**Value materialization** (`sub_A10370`, 33 KB) handles forward references in metadata. When a metadata node references a value that hasn't been parsed yet, the materializer resolves it once the value becomes available.

## Module Summary Serialization

Two pairs of functions handle ThinLTO module summary IO:

### Summary Writer (`sub_1535340`, 26 KB)

Writes the `MODULE_STRTAB_BLOCK` and `GLOBALVAL_SUMMARY_BLOCK` into the bitcode stream. For each function/alias/global:

- Encodes the GUID hash (64-bit FNV-1a on the mangled name)
- Writes call graph edges with hotness annotations
- Writes reference edges (global value references)
- For ThinLTO: writes module path strings, type test GUIDs

Error string: `"Unexpected anonymous function when writing summary"`.

The NVIDIA-extended summary fields (import priority, complexity budget, kernel bit, CUDA attributes) are written by the [NVModuleSummary builder](../lto/module-summary.md) into the standard summary records via additional flag bits and extended record fields.

### Summary Reader (`sub_150B5F0`, 63 KB)

Reads the summary index from bitcode. Handles GUID hashes, function/alias summaries, module paths. Error strings: `"Alias expects aliasee summary"`, `"Invalid hash length"`, `"Invalid Summary Block: version expected"`, `"Malformed block"`.

### Summary Writer (standalone copy) (`sub_A2D2B0`, 48 KB)

A second copy of the summary/metadata writer exists at `0xA2D2B0` in the standalone pipeline's address range.

## NVVM IR Version Validation

CICC gates bitcode acceptance on two version checks:

### Module-Level Version Gate (`sub_157E370`, 7 KB)

After parsing the module, this function reads the `"nvvmir.version"` named metadata node. The metadata contains a pair of integers `(major, minor)`. The check enforces:

```
major == 3  AND  minor <= 2
```

If the check fails, the function calls `sub_16BD130` which emits `"Broken module found, compilation aborted!"` and terminates compilation. If the module passes the version check, it proceeds to `sub_166CBC0` (`verifyModule` `[MEDIUM confidence]` -- identification based on call position after bitcode parsing and before optimization, consistent with LLVM's standard verify-after-parse pattern, but no diagnostic string directly confirms the function name) for structural IR verification, then `sub_15ACB40` for post-verification processing.

A second instance at `sub_12BFF60` (9 KB) in the standalone pipeline performs the same check with additional `llvm.dbg.cu` debug info presence validation.

### Environment Override (`NVVM_IR_VER_CHK`)

The `NVVM_IR_VER_CHK` environment variable controls whether version validation runs at all:

| Value | Effect |
|-------|--------|
| Unset or non-`"0"` | Version check enabled (default) |
| `"0"` | Version check bypassed, no version mismatch errors |

The check is: `if (!env || strtol(env, NULL, 10) != 0)` then enforce version. This means any non-zero numeric string also enables the check. Only the literal string `"0"` disables it.

Two verifier instances exist:
- `sub_12BFF60` at `0x12BFF60` (standalone pipeline)
- `sub_2259720` at `0x2259720` (second instance, possibly duplicate link unit)

## Configuration

### Environment Variables

| Variable | Effect | Default |
|----------|--------|---------|
| `LLVM_OVERRIDE_PRODUCER` | Overrides bitcode producer identification string | `"7.0.1"` (ctor_154) / `"20.0.0"` (ctor_036) |
| `NVVM_IR_VER_CHK` | Set to `"0"` to bypass NVVM IR version validation | Enabled |

### cl::opt Flags

| Flag | Type | Default | Effect |
|------|------|---------|--------|
| `disable-bitcode-version-upgrade` | bool | false | Disable automatic bitcode upgrade for version mismatch |
| `bitcode-mdindex-threshold` | int | 25 | Number of metadata entries above which an index is emitted |
| `disable-ondemand-mds-loading` | bool | false | Disable lazy metadata loading |
| `write-relbf-to-summary` | bool | false | Write relative block frequency to ThinLTO function summary |
| `print-summary-global-ids` | bool | false | Print global IDs when reading module summary |
| `import-full-type-definitions` | bool | false | Import full type definitions in ThinLTO |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM 20.0.0 | CICC v13.0 |
|--------|----------------------|------------|
| Producer string | `"LLVM20.0.0"` | `"LLVM7.0.1"` (hardcoded via ctor_154) |
| Producer override | `LLVM_OVERRIDE_PRODUCER` env var | Same mechanism, different default |
| Version upgrade disable | `disable-bitcode-version-upgrade` exists | Same, registered in ctor_036 |
| NVVM IR version gate | Does not exist | `nvvmir.version` metadata check (major==3, minor<=2) |
| NVVM IR version bypass | Does not exist | `NVVM_IR_VER_CHK=0` environment variable |
| X86 AutoUpgrade | Active for x86 targets | Present but dead code (NVPTX only) |
| NVVM intrinsic upgrade | Does not exist | nvvm.atomic, nvvm.shfl, nvvm.cp.async, etc. upgraders added |
| NVVM annotation upgrade | Does not exist | maxclusterrank, maxntid metadata upgrader added |
| Module summary | Standard ModuleSummaryAnalysis | Extended with NVModuleSummary (import priority, kernel bit, complexity budget) |
| Binary copies | Single instance | Two copies (0x9F range, 0x150 range) at different link addresses |

## Function Map

### Reader (primary, `0x1500000`--`0x1522000`)

| Address | Size | Function |
|---------|------|----------|
| `0x1503DC0` | 13 KB | `materializeFunctions` |
| `0x1504A60` | 8 KB | `parseGlobalInits` |
| `0x1505110` | 60 KB | `parseModule` |
| `0x15083D0` | 26 KB | `parseTypeBlock / Constants` |
| `0x150A160` | 7 KB | `ThinLTO GUID lookup` |
| `0x150B5F0` | 63 KB | `parseModuleSummaryIndex` |
| `0x150E2B0` | 19 KB | `readRecord` |
| `0x150F8E0` | 42 KB | `readBlockInfoBlock` |
| `0x1510D70` | 38 KB | `readAbbreviatedField` |
| `0x1513230` | 20 KB | `readAbbrevRecord` |
| `0x15127D0` | 13 KB | `skipBlock` |
| `0x15140E0` | 13 KB | `string table reader` |
| `0x1514C40` | 9 KB | `readBlobRecord` |
| `0x1515740` | 9 KB | `parseValueRecord` |
| `0x15177F0` | 7 KB | `bitcode record helper` |
| `0x1518180` | 29 KB | `parseMetadataBlock` |
| `0x1519820` | 7 KB | `bitcode record helper` |
| `0x1519BD0` | 7 KB | `bitcode record helper` |
| `0x151B070` | 123 KB | `parseFunctionBody` |
| `0x1520420` | 32 KB | `parseFunctionMetadata` |
| `0x1522160` | 13 KB | `parseMetadataStrings` |

### Reader (standalone copy, `0x9F0000`--`0xA20000`)

| Address | Size | Function |
|---------|------|----------|
| `0x9F2A40` | 185 KB | `parseFunctionBody` |
| `0xA09F80` | 121 KB | `MetadataLoader::parseOneMetadata` |
| `0xA10370` | 33 KB | `value materialization` |
| `0x9FF220` | 31 KB | `writer helper` |
| `0xA2D2B0` | 48 KB | `module summary / metadata writer` |

### Writer (`0x1525000`--`0x1549000`)

| Address | Size | Function |
|---------|------|----------|
| `0x15263C0` | 7 KB | `emitCode` |
| `0x15271D0` | 7 KB | `emitVBR` |
| `0x1527BB0` | 9 KB | `writeConstants helper` |
| `0x1528720` | 27 KB | `writeInstruction` |
| `0x152A250` | 9 KB | `emitRecord` |
| `0x152AB40` | 11 KB | `emitAbbreviation` |
| `0x152F610` | 8 KB | `writeAttributeGroup` |
| `0x1530240` | 12 KB | `writeType / GlobalVar` |
| `0x15311A0` | 14 KB | `writeNamedMetadata / comdat` |
| `0x1531F90` | 27 KB | `writeMetadata` |
| `0x15334D0` | 8 KB | `writeMetadataRecords` (37 callees) |
| `0x1533CF0` | 16 KB | `writeValueSymbolTable` |
| `0x1535340` | 26 KB | `writeModuleSummary` (ThinLTO) |
| `0x1536CD0` | 40 KB | `writeFunction` |
| `0x1538EC0` | 58 KB | `writeModule` |

### Intrinsic Upgrader (`0xA80000`--`0xABFFFF` + `0x1560000`--`0x1580000`)

| Address | Size | Function |
|---------|------|----------|
| `0x156E800` | 593 KB | `UpgradeIntrinsicFunction` |
| `0xA939D0` | 457 KB | `UpgradeIntrinsicCall` |
| `0xA8A170` | 195 KB | `X86 intrinsic upgrade helper` |
| `0x15644B0` | 89 KB | `UpgradeIntrinsicCall` (2nd copy) |
| `0xA8E250` | 52 KB | `NVVM upgrade dispatcher` |
| `0xA91130` | 28 KB | `NVVM call rewriting` |
| `0xA84F90` | 14 KB | `NVVM annotation metadata upgrade` |
| `0xA7CD60` | 10 KB | `UpgradeIntrinsicFunction` (short, matches "nvvm.", "ftz.") |
| `0x156C720` | 10 KB | `UpgradeModuleFlags` |
| `0x156A1F0` | 7 KB | `UpgradeLoopMetadata` |

### NVVM Version / Producer

| Address | Size | Function |
|---------|------|----------|
| `0x157E370` | 7 KB | `NVVM version checker` (primary) |
| `0x12BFF60` | 9 KB | `NVVM version checker` (standalone) |
| `0x2259720` | -- | `NVVM version checker` (duplicate instance) |
| `0x48CC90` | 544 B | `ctor_036` -- producer init + disable-bitcode-version-upgrade |
| `0x4CE640` | 215 B | `ctor_154` -- producer init ("7.0.1" default) |

### Value Enumeration (`0x1540000`--`0x1549000`)

| Address | Size | Function |
|---------|------|----------|
| `0x1542B00` | 26 KB | `enumerateValues` |
| `0x15467B0` | 23 KB | `enumerateModule` |
| `0x1548410` | 8 KB | `optimizeConstants` |
| `0x15445A0` | 11 KB | `metadata enumeration helper` |
| `0x15450E0` | 9 KB | `ValueEnumerator helper` |
| `0x1547D80` | 9 KB | `ValueEnumerator helper` |
| `0x1543FA0` | 7 KB | `ValueEnumerator helper` |
| `0x1542750` | 7 KB | `ValueEnumerator helper` |
| `0x153E1D0` | 7 KB | `TypeFinder helper` |

## Cross-References

- [NVVM Container](../structs/nvvm-container.md) -- wraps bitcode in the proprietary transport format
- [LTO & Module Optimization](../lto/index.md) -- consumes bitcode from separate compilation objects
- [NVModuleSummary Builder](../lto/module-summary.md) -- extends module summary with CUDA-specific fields; serialized by `sub_1535340`
- [Two-Phase Compilation](../pipeline/emission.md) -- serializes/deserializes per-function bitcode between phases
- [Pipeline Entry](../pipeline/entry.md) -- magic byte validation on bitcode input
- [Environment Variables](../config/env-vars.md) -- `LLVM_OVERRIDE_PRODUCER`, `NVVM_IR_VER_CHK`
- [Binary Layout](../binary-layout.md) -- address range context for reader/writer clusters
