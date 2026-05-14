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

Bring-up follows the same shape as upstream LLVM NVPTX with one structural
twist: the factory chain that constructs the `NVPTXTargetMachine` is folded
under NVIDIA's private peephole-pass selection. `LLVMInitializeNVPTXTargetInfo`
registers the target names through `TargetRegistry::RegisterTarget`. That call
runs from a `__attribute__((constructor))`-style global initializer, so by the
time `main` enters the compiler the two target records (`nvptx`, `nvptx64`) are
already in the registry. `LLVMInitializeNVPTXTarget` fills the constructor slots
for the target services used later by MC emission and target-machine creation.

The factory function the registry stores under each target record is not the
upstream `createNVPTXTargetMachine`. It is an NVIDIA-private variant that,
after building the base target machine, walks the global peephole-pass table
and installs the subset legal on the requested target chip. The selection is
data-driven: each entry in the peephole table carries a chip-feature predicate
that the factory evaluates against the parsed feature string. Peepholes whose
predicates fail are skipped; the survivors become part of the per-target-machine
pass pipeline returned to the caller. Caching the target machine therefore also
caches the peephole-pass selection — rebuilding with a different chip/feature
combination forces both the target machine and the peephole list to be
reconstructed.

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
| `NVPTXAsmPrinter` | Emits module headers, directives, sections, and PTX instruction text. The constructor slot points at the LTO-folded printer described below, not a generic LLVM `AsmPrinter`. |

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

## The LTO-Folded AsmPrinter Class

The class the target registry stores under `NVPTXAsmPrinter` is a single, very
large function produced by NVIDIA's whole-program LTO build. It is not the
upstream `NVPTXAsmPrinter` class hierarchy — at link time the LTO pipeline
collapses the upstream class, its TableGen-generated `AsmWriter` subclass, the
operand-print helpers, the modifier-print helpers, and most of the per-opcode
print-shape helpers into a single dispatch function with a giant switch ladder
over MC opcodes. The dispatcher inlines the operand-print and modifier-print
work directly into each case rather than calling through small helpers.

What survives as separate, non-inlined methods is the part of the printer the
target machine and the pass manager call from outside: the constructor, the
`runOnMachineFunction` entry point, the section-change hook described above,
the module-level header emitter, and the global-variable emitter. These methods
are the ones whose addresses the target registry stores and whose vtable slots
the rest of the backend dispatches through.

The MLIR side of the same class handles selected `nvvm.*` ops — the dialect's
custom printers for ops that do not lower to a single PTX instruction, and for
which the TableGen `assemblyFormat` is not enough. The MC-instruction side
handles selected `MachineInst` opcodes — the per-MC-opcode dispatcher. Both
sides share the operand-print and modifier-print code, which is why they end up
folded into the same LTO function: the inliner sees the shared callees and
collapses both call graphs around them.

A reimplementation does not need to reproduce the LTO fold. The contract is
that one printer object serves both MLIR-side `nvvm.*` printing and MC-side PTX
emission, and that the two sides share modifier-print and operand-print
infrastructure. How the implementation factors that contract is a build-time
decision; the LTO fold is the choice NVIDIA's release build makes.

## Mnemonic Pool Decode

PTX mnemonics live in a `.data`-resident pool the AsmPrinter reads from. The
pool is not stored in cleartext. The bytes in `.data` are obfuscated under a
walking-XOR cipher; the printer decodes the pool in place on first use, then
all subsequent mnemonic lookups read decoded bytes directly.

The cipher is a stride-3 walking XOR. Byte at offset `i` in the pool is
decoded by XORing with key byte `(3 * i) mod 256`. The decode is in-place and
single-pass: the printer walks the pool from offset zero to the end exactly
once, XORing each byte against its computed key byte, and then sets a flag
that future readers consult before doing any work. The decode runs under a
`pthread_once` guard so concurrent compilations cannot trigger overlapping
in-place writes; the once-init body holds a process-global lock, walks the
pool, and releases the lock with the decoded state visible to all threads.

```c
static pthread_once_t mnemonic_pool_once = PTHREAD_ONCE_INIT;
static char mnemonic_pool[POOL_SIZE]; // .data-resident, obfuscated at link time

static void decode_mnemonic_pool(void) {
    for (size_t i = 0; i < POOL_SIZE; ++i) {
        mnemonic_pool[i] ^= (char)((3u * (unsigned)i) & 0xff);
    }
}

const char *mnemonic_for_opcode(unsigned mc_opcode) {
    pthread_once(&mnemonic_pool_once, decode_mnemonic_pool);

    uint32_t lo_offset = mnemonic_offset_table_lo[mc_opcode];
    uint32_t hi_offset = mnemonic_offset_table_hi[mc_opcode];
    return &mnemonic_pool[lo_offset | (hi_offset << 16)];
}
```

Two offset tables index into the decoded pool — a low-16-bit table and a
high-16-bit table, both keyed by MC opcode. Joining them produces a 32-bit
byte offset into the pool, which lets the printer address mnemonics up to a
4 GiB pool size even though the pool itself fits in a few tens of kilobytes.
The two-table split survives the LTO fold; the printer's MC-opcode dispatcher
emits the same `(lo | (hi << 16))` reconstruction inline in every case.

A smaller separate pool with the same XOR-3 decode scheme holds physical
register names. Both pools are decoded by the same once-init body, so the
mnemonic table and the register-name table become available simultaneously
on the first call to the printer.

## NVVM Intrinsic Mapping Table

The translation from `nvvm.*` MLIR ops to LLVM IR happens through a
one-to-one mapping table the target-init layer installs into the MLIR-to-LLVM
translator. Each `nvvm.*` op carries one of two lowering keys:

- `llvm_intrinsic_id`: the op lowers to a single `call` of an `llvm.nvvm.*`
  intrinsic. The translator looks up the intrinsic by ID and emits an LLVM
  `IntrinsicInst` with the op's operands as arguments.
- `inline_asm_template`: the op lowers to an `llvm.inline_asm` call whose
  template is a PTX fragment with `${0}`, `${1}`, etc. placeholders for the
  operands. The translator substitutes the operand SSA values, emits the
  `InlineAsm` IR, and lets the later NVPTX backend pass copy the inline-asm
  body verbatim into the PTX output.

The choice between the two paths is per-op, baked into the table. Ops that
correspond to a single PTX instruction with a stable encoding (most `mma.*`,
`tma.*`, and `mbarrier.*` ops) go through the intrinsic path. Ops that
correspond to compound PTX sequences or that depend on assembly-level
modifiers the LLVM intrinsic surface does not expose go through the
inline-asm path. The inline-asm template typically embeds the modifier
directly into the template string rather than passing it as an operand,
because the LLVM `asm` constraint vocabulary cannot express PTX modifier
combinatorics in general.

```c
typedef enum { LOWER_INTRINSIC, LOWER_INLINE_ASM } NvvmLoweringKind;

typedef struct NvvmOpLowering {
    StringRef         op_name;            // e.g. "nvvm.barrier0"
    NvvmLoweringKind  kind;
    union {
        uint32_t      intrinsic_id;       // valid when kind == LOWER_INTRINSIC
        struct {
            StringRef template_str;       // PTX template with ${i} placeholders
            StringRef constraints;        // LLVM inline-asm constraint string
            bool      has_side_effects;
        } asm_data;                       // valid when kind == LOWER_INLINE_ASM
    };
} NvvmOpLowering;
```

The table is populated by the dialect's `addPattern` calls during target-init.
On every `nvvm.*` op encountered by the translator, the dispatcher consults
the table, builds either the intrinsic call or the inline-asm call, and
attaches the same memory-effect and side-effect attributes the op carried in
MLIR. Attaching the effects matters: if the op was marked memory-writing in
the dialect, the resulting LLVM call must be marked the same way, or the
LLVM optimizer will see it as a pure call and start hoisting or eliminating
it on the device IR.

## blobLinkedLib Attribute on `gpu.module`

The `blobLinkedLib` attribute on a `gpu.module` carries the precompiled
bitcode payload that gets linked into the LLVM module during NVPTX bring-up.
The attribute is the only point at which libdevice (or another bitcode
helper library) enters the compiler — there is no command-line `-mlink-bc`
flag or filesystem lookup. The driver attaches the attribute to the module
during front-end processing, and the bring-up layer consumes it during the
LLVM-link step described below.

The attribute value is an MLIR `BlobAttr` whose payload can be one of two
shapes:

- An inline byte array: the bitcode payload is embedded directly in the IR.
  Used when the driver wants to pin a specific libdevice version into a
  reproducible build.
- A filesystem path: the bitcode lives on disk and the bring-up layer reads
  it at link time. Used by the normal CUDA toolchain build where libdevice
  is shipped as a separate `.bc` file alongside the compiler.

```c
LLVMModule *load_blob_linked_lib(GPUModuleOp module) {
    Attribute attr = module.attributes()["blobLinkedLib"];
    if (attr == NULL) {
        return NULL;
    }

    BlobPayload payload = resolve_blob_payload(attr);

    if (payload.kind == BLOB_FILE && !is_regular_file(payload.path)) {
        diagnose("blobLinkedLib: bitcode path does not exist or is not a file");
        return NULL;
    }

    ParseResult parsed = parse_llvm_bitcode(payload);
    if (!parsed.ok) {
        diagnose("blobLinkedLib: failed to parse embedded bitcode");
        return NULL;
    }

    return parsed.module;
}
```

The loader runs at the start of the NVPTX bring-up. The parsed module is
linked into the main LLVM module via `llvm::Linker` with the
`LinkOnlyNeeded` flag so unused helpers do not bloat the final PTX. From
this point on the helpers are ordinary LLVM IR — the optimizer sees them as
internal functions and can inline, specialize, and DCE them like any other
device function.

The contract for a reimplementation: a `gpu.module` without `blobLinkedLib`
proceeds through the NVPTX pipeline with no implicit libdevice link. Math
intrinsics that need libdevice helpers (transcendentals, denormal handling,
some integer conversions) emit linker-error diagnostics rather than silently
falling back to a default library. The driver layer is responsible for
attaching the attribute when the kernel actually needs libdevice.

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
and around the pipeline: target-machine selection, the `blobLinkedLib` bitcode
linkage step, and the NVIDIA-private peephole-pass selection the factory
installed when the target machine was built.

## Cross-References

- [Codegen Overview](overview.md) — the seven-stage backend contract these
  primitives feed into.
- [AsmPrinter Monster and Windows](asm-printer-monster-and-windows.md) — the
  6,388-case dispatcher that the LTO-folded printer described here implements,
  and the per-SM print-shape windows it dispatches into.
- [NVPTX Subtarget and Feature Matrix](nvptx-subtarget-and-feature-matrix.md) —
  the chip/feature predicates the peephole-pass selection consults.
- [libdevice Overview](../libdevice/overview.md) — the bitcode library most
  often delivered through the `blobLinkedLib` attribute.
- [Lowering: Target and Debuginfo](../lowering/target-and-debuginfo.md) — the
  point at which `gpu.module` acquires the `#nvvm.target` and `blobLinkedLib`
  attributes the bring-up reads.
