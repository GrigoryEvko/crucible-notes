# NVPTX Bring-up and Target Init

## Abstract

NVPTX bring-up is the handoff point between the Tileiras dialect-lowering
pipeline and the stock-shaped LLVM `TargetMachine` configured for PTX
emission. By the time this layer runs, the MLIR pipeline has already
produced LLVM/NVVM IR.

The layer owns target registration, MC-layer object construction, the
`NVPTXAsmPrinter` section model, the embedded-device-library linker, target
machine caching, and the LLVM optimization pipeline driver. The
reimplementation contract is a sequence, not a static constructor layout:
register both NVPTX triples, build consistent MC services, resolve the
target machine from the requested chip/features, link device bitcode, run
the LLVM pipeline, then emit PTX through the NVPTX asm printer.

Two choices distinguish Tileiras from a plain LLVM build. First,
`nvptx` and `nvptx64` share one constructor table; the triple controls
pointer size and ABI details downstream. Second, libdevice never travels
LLVM's ordinary filesystem search path. It arrives as an MLIR `BlobAttr`
on the `gpu.module` and is parsed into an LLVM module before
optimization.

## Target Registration Chain

Bring-up follows the same shape as upstream LLVM NVPTX. `LLVMInitializeNVPTXTargetInfo`
registers the target names, and `LLVMInitializeNVPTXTarget` fills the constructor
slots for the target services used later by MC emission and target-machine
creation.

| Service | Role |
|---|---|
| `LLVMInitializeNVPTXTargetInfo` | Registers `nvptx` and `nvptx64` target records. |
| `LLVMInitializeNVPTXTarget` | Installs all target constructor callbacks. |
| `NVPTXMCAsmInfo` | Defines PTX comments, directives, pointer size, and asm syntax. |
| `MCInstrInfo` | Supplies instruction descriptors for the NVPTX opcode set. |
| `NVPTXRegisterInfo` | Supplies physical registers and register-class descriptors. |
| `MCSubtargetInfo` | Supplies CPU and feature tables used by legality checks. |
| `MCInstrAnalysis` | Supplies branch and instruction-analysis helpers. |
| `MCAsmBackend` | Supplies MC assembly backend services. |
| `MCCodeEmitter` | Supplies MC instruction encoding hooks where LLVM expects them. |
| `NVPTXAsmPrinter` | Emits module headers, directives, sections, and PTX instruction text. |

Both 32-bit and 64-bit targets receive the same service table. The triple decides
whether the compilation is `nvptx` or `nvptx64`, and the MC asm-info constructor
turns that into the pointer-size and stack-slot-size choices needed by the ABI.

## NVPTXMCAsmInfo Constructor

`NVPTXMCAsmInfo` starts from ordinary LLVM MC defaults and then replaces the
host-assembly pieces that make no sense for PTX. PTX has no ELF-style
`.text`, `.bss`, `.data`, `.globl`, or `.weak` directives, so those fields become
comments or PTX-specific byte directives. Inline assembly gets wrapped in
comments so `ptxas` receives the inline body without host-assembler markers.

| Field | NVPTX value |
|---|---|
| `PointerSize` | 4 for `nvptx`, 8 for `nvptx64` |
| `CalleeSaveStackSlotSize` | matches pointer size |
| `CommentString` | `//` |
| `PrivateGlobalPrefix` | `$L__` |
| `CommentColumn` | 4 |
| `InlineAsmStart` / `InlineAsmEnd` | commented begin/end markers |
| `AsciiDirective` | `.b8` |
| `Data8bitsDirective` | `.b8 ` |
| `Data32bitsDirective` | `.b32 ` |
| `Data64bitsDirective` | `.b64 ` |
| `GlobalDirective` | commented `.globl` surrogate |
| `WeakRefDirective` | commented `.weak` surrogate |
| `UseIntegratedAssembler` | disabled |
| `SupportsDebugInformation` | enabled |

PTX assembly must never depend on host object-file section semantics. The
asm-info layer turns LLVM's generic MC vocabulary into PTX comments and PTX
byte directives before the printer writes a module.

## Section Changes

`NVPTXAsmPrinter::changeSection` implements the brace-bound function-body model
used by PTX. Instead of switching among ELF sections, the printer emits a
commented section header and opens or closes a brace-delimited body.

```c
void change_nvptx_section(AsmPrinter *printer, MCSection *next, raw_ostream *os) {
    if (printer->current_section == next) {
        os_write(os, "\t}\n");
        printer->current_section = NULL;
        return;
    }

    print_commented_section_header(next, os);
    os_write(os, "\t{\n");
    printer->current_section = next;
}
```

Emitted PTX kernels therefore appear inside `{` and `}` rather than between
`.text` and `.size` markers. The section line is documentation for readers and
debug tooling; `ptxas` treats it as a comment.

## Embedded Bitcode Linker

LLVM expects libdevice math to be linked at LLVM-IR level before the NVPTX asm
printer runs. Tileiras supplies that library as an MLIR `BlobAttr` named
`blobLinkedLib` on the `gpu.module`. The linker resolves the attribute to either
inline bytes or a file payload, parses it as LLVM bitcode, and appends the module
to the link queue.

```c
LLVMModule *load_embedded_device_library(GPUModuleOp module) {
    Attribute attr = module.attributes()["blobLinkedLib"];
    if (attr == NULL) {
        return NULL;
    }

    BlobPayload payload = resolve_blob_payload(attr);

    if (payload.kind == BLOB_FILE && !is_regular_file(payload.path)) {
        diagnose("device-library bitcode path does not exist or is not a file");
        return NULL;
    }

    ParseResult parsed = parse_llvm_bitcode(payload);
    if (!parsed.ok) {
        diagnose("failed to parse embedded device-library bitcode");
        return NULL;
    }

    return parsed.module;
}
```

This is the only point where libdevice, or another embedded math bitcode
payload, enters the LLVM pipeline. From here on the optimizer sees the helper
definitions as ordinary LLVM IR.

## Target-Machine Cache

Target-machine creation resolves the target triple, looks up the registered
target, builds `TargetOptions`, selects the requested `mcpu`, and calls the
target's `TargetMachine` constructor. The resulting object is cached so repeated
compilations with the same target settings do not rebuild the LLVM backend state.

```c
TargetMachine *get_or_create_nvptx_target_machine(TargetCache *cache,
                                                  TargetRequest request) {
    if (cache->machine != NULL && target_request_equal(cache->request, request)) {
        return cache->machine;
    }

    const Target *target = lookup_target(request.triple);
    if (target == NULL) {
        diagnose("failed to look up NVPTX target for requested triple");
        return NULL;
    }

    TargetOptions options = default_nvptx_target_options();
    TargetMachine *machine = target->create_target_machine(
        request.triple, request.mcpu, request.features, options);

    cache->request = request;
    cache->machine = machine;
    return machine;
}
```

The cache key must include the triple, chip, and feature string. A target
machine reused across incompatible feature sets makes later legality checks
observe the wrong subtarget.

## LLVM Pass Pipeline

The optimization driver accepts the requested optimization level, ensures a
target machine exists, and asks LLVM `PassBuilder` for the per-module default
pipeline. Invalid optimization levels become diagnostics before any pass manager
is built.

```c
bool run_llvm_pipeline(LLVMModule *module, TargetMachine *tm, OptLevel level) {
    if (!is_valid_opt_level(level)) {
        diagnose("invalid LLVM optimization level");
        return false;
    }

    if (tm == NULL) {
        diagnose("target machine unavailable; cannot optimize with LLVM");
        return false;
    }

    PassBuilder builder(tm);
    ModulePassManager mpm = builder.build_per_module_default_pipeline(level);
    mpm.run(*module);
    return true;
}
```

The pipeline shape is the stock LLVM decomposition: early simplification, module
simplification, function simplification, inlining, vectorization, module
optimization, and post-pass cleanup. Tileiras-specific behavior happens before
and around the pipeline: target-machine selection, embedded bitcode linkage, and
PTX-specific MC configuration.
