# Glossary

This glossary defines the terms used throughout the cudafe++ wiki. It collects the vocabulary the analysis inherits from three distinct sources: the IDA Pro / Hex-Rays toolchain that produced the raw evidence, the EDG C++ Front End 6.6 the binary was built from, and the CUDA-specific layer NVIDIA bolted on top. Entries are grouped by domain and listed alphabetically within each group. Cross-links point to the wiki pages that develop each concept in depth.

Operation names, struct field offsets, and IDA anchor identifiers appear in backticks. Where a term has a confidence implication (CONFIRMED, HIGH, MEDIUM, LOW), the relevant evidence chain is noted; otherwise every entry is treated as established documentation. See the [Methodology](./methodology.md) page for the meaning of the confidence tags and the module-attribution chain.

## A. Reverse-Engineering Anchors

These terms refer to artifacts of IDA Pro's auto-analysis and the conventions the wiki uses to cite specific routines and data items in the stripped binary. They are not part of the original EDG or CUDA vocabulary.

| Term | Meaning |
| --- | --- |
| `byte_<hex>` | IDA's auto-generated name for a 1-byte global at address `<hex>`. Used in the wiki for single-byte flag globals such as `byte_F3E140` (the warning-emission gate). |
| `dword_<hex>` | IDA's auto-generated name for a 4-byte global at address `<hex>`. Used for integer flag globals and counters such as `dword_E26AC8` (an option-stack depth field). |
| Hex-Rays | The x86-64 decompiler bundled with IDA Pro 9.0 that produced the pseudocode for all 6,501 functions analyzed in this wiki. See [Methodology — Decompilation Quality](./methodology.md#decompilation-quality) for systematic limitations of the output. |
| IDA Pro | The commercial disassembler used to recover cudafe++ behavior from its stripped ELF binary. The IDA database (`cudafe++.i64`, 247 MB) holds all analysis state and is the source of every artifact referenced in this wiki. |
| `off_<hex>` | IDA's auto-generated name for a pointer-typed global at address `<hex>`. The IL kind name table `off_E6DD80` and the error table `off_88FAA0` are the two most-cited examples. |
| `qword_<hex>` | IDA's auto-generated name for an 8-byte global at address `<hex>`. Used for pointer globals (heap roots, dispatch hooks) and 64-bit counters. |
| `sub_<hex>` | IDA's auto-generated name for a function at virtual address `<hex>`. Every routine in the binary is referenced by this address-based identifier because the binary is stripped of its symbol table. Example: `sub_4F2930` is the internal assert handler at `0x4F2930`. |
| Sweep | A Phase 1 analysis pass over a contiguous address range (typically 128–256 KB) that enumerates every function in that range. The 20-sweep grid covers the full `.text` section from `0x403000` to `0x82A000`. See [Methodology — Phase 1](./methodology.md#phase-1-address-range-sweeps). |
| `unk_<hex>` | IDA's auto-generated name for a global of unknown or untyped width at address `<hex>`. Appears for structured records that IDA's auto-analysis did not fully classify. |
| Walk | A traversal of the IL tree driven by `il_walk.c`. EDG defines five callback slots (enter-node, exit-node, enter-scope, exit-scope, descend-children); the keep-in-IL predicate, the IL display function, and the constexpr interpreter are all walks. The synonyms "tree walk", "tree traversal", and "IL traversal" appear in prose with the same meaning. See [IL Tree Walking](./il/walking.md). |
| Xref | A cross-reference. Every call, jump, data read, data write, and offset reference in the binary appears as an xref record in `cudafe++_xrefs.json`. The wiki uses six IDA xref type codes: `dr_O` (data offset), `dr_W` (data write), `dr_R` (data read), `fl_CN` (code near call), `fl_CF` (code far/ordinary flow), and `fl_JN` (code near jump). See [Methodology — Extraction Script](./methodology.md#extraction-script). |

## B. EDG Frontend Core

These terms are inherited from the Edison Design Group C++ Front End 6.6 the binary was built from. They name the data structures, traversal mechanisms, and lifecycle phases of the parser.

| Term | Meaning |
| --- | --- |
| `ck_*` | The `constant_kind` enum prefix. 16 values that classify constant expressions stored in the IL (integer, float, complex, character, null pointer, etc.). |
| `dik_*` | The `dynamic_init_kind` enum prefix. 9 values classifying dynamic-initialization mechanisms emitted for global objects with non-trivial constructors. |
| EDG | Edison Design Group, the company licensing the C++ Front End that cudafe++ is built on. The binary was compiled from EDG version 6.6, as confirmed by the `EDG_6.6` tag in the assertion path-strings embedded in every assertion. See [EDG 6.6 Overview](./edg/overview.md). |
| EDG callbacks | The five-slot callback table consumed by every IL walker: `enter_node`, `exit_node`, `enter_scope`, `exit_scope`, and `descend_children`. Each walk-driven pass (keep-in-IL, display, constexpr, name lookup, code generation) registers a different set of callbacks against the same walker infrastructure. See [IL Tree Walking](./il/walking.md). |
| `enk_*` | The `expression_node_kind` enum prefix. 36 values that discriminate IL expression nodes (binary op, unary op, function call, member access, cast, etc.). |
| Entity | A named or anonymous declaration node — variable, routine, type, namespace, template parameter — that lives in the IL. The execution-space bitfield at offset `+182` lives on the entity node, as does its scope back-pointer and template metadata. Wiki prose uses "entity node" and the bare "node" (in context) interchangeably; both refer to this record. See [Entity Node Layout](./structs/entity-node.md). |
| Entity kind (`entity_kind`) | The 1-byte tag on every entity node distinguishing variable, routine, type, namespace, template parameter, and the half-dozen rarer kinds. See [Entity Node Layout](./structs/entity-node.md). |
| Entry kind (`entry_kind`) | The 1-byte tag (values 0–84) that discriminates IL node types. The 85 entry kinds and the sub-kind enums (`tk_*`, `enk_*`, `stmk_*`, `sck_*`, `ck_*`, `dik_*`) together name every shape of node the IL can hold. See [IL Overview](./il/overview.md). |
| Expr kind (`expr_kind`) | Either a synonym for `entry_kind` when the node is known to be an expression, or specifically the `enk_*` sub-discriminator. The wiki uses "expression kind" for the latter. |
| `gen_lambda` | Backend code generation for a lambda expression site. Implemented at `sub_47B890`. Emits the host-side wrapper helpers (`__nv_hdl_wrapper_t`, `__nv_hdl_helper`) that bridge a host-device lambda to the kernel that captures it. See [Host-Device Lambda Wrapper](./lambda/host-device-wrapper.md). |
| `gen_routine` | Backend code generation for a single C++ routine declaration, implemented at `sub_47BFD0` as `gen_routine_decl`. The two-pass invocation emits first the `static void __device_stub__<name>(...)` body and then the host-callable wrapper. See [Kernel Stub Generation](./cuda/kernel-stubs.md). |
| `gen_template` | The master source-sequence dispatcher in `cp_gen_be.c` at `sub_47ECC0`. Switches on entry kind and routes each top-level IL node to its specific emitter (routine, variable, lambda, class, template instantiation, etc.). See [Backend Code Generation](./pipeline/backend.md). |
| IL | EDG's Intermediate Language — the typed, scope-linked node graph that holds every declaration, type, expression, statement, scope, and template after parsing. Every wiki page that touches semantics returns to the IL at some point. See [IL Overview](./il/overview.md) for the 85 entry kinds. |
| Name lookup | EDG's resolution machinery in `lookup.c` and `scope_stk.c` that maps an identifier to its declaring entity by walking the scope stack and applying ADL (argument-dependent lookup), qualified-lookup, and class-member lookup rules. The scope stack is the active set of [Scope Entry](./structs/scope-entry.md) records. |
| PCH | Precompiled header. EDG's serialized parser state, gated by the `pch_*` CLI flags (116–121) and implemented in `pch.c`. CUDA builds rarely use PCH, but the support code is present in cudafe++. |
| Region | An IL arena partition. Region 1 holds file-scope nodes that persist for the whole translation unit; regions 2..N hold per-function nodes that are freed after each function body is processed. Allocation is bump-style inside 64 KB blocks. See [IL Allocation](./il/allocation.md). |
| `reset_tu_state` | The per-TU zeroing routine at `sub_7A4860` that clears the 424-byte TU descriptor and a list of associated globals between compilation units inside a single cudafe++ process. The function is the practical boundary between two translation units when cudafe++ is reused. |
| Scope kind (`scope_kind`, `sck_*`) | The 1-byte tag on a [Scope Entry](./structs/scope-entry.md) (9 values: file, function, block, class, namespace, template, anonymous, etc.) that determines its lookup rules and lifetime. |
| `stmk_*` | The `statement_kind` enum prefix. 26 values that classify IL statement nodes (compound, if, while, return, declaration, asm, etc.). |
| `tk_*` | The `type_kind` enum prefix. 22 values that classify IL type nodes (fundamental, pointer, reference, array, function, class, enum, etc.). See [Type Node](./structs/type-node.md). |
| Translation unit (TU) | One `.cu` source file as seen by the frontend, plus all transitively included headers. The TU descriptor is a 424-byte struct; `reset_tu_state` (`sub_7A4860`) zeros per-TU globals between compilations. See [Translation Unit Descriptor](./structs/translation-unit.md). |
| Type kind (`type_kind`) | See `tk_*`. The 1-byte tag on a [Type Node](./structs/type-node.md) discriminating fundamental, derived, and compound types. |
| Wrapup | EDG's term for the 5-pass IL finalization that follows the parse, implemented in `fe_wrapup.c`. Each pass walks the IL applying a different transformation — template instantiation completion, dependent-name resolution, default-argument substitution, vtable layout, and final attribute propagation — before backend emission begins. See [Frontend Wrapup](./pipeline/fe-wrapup.md). |
| `cp_gen_be.c` | The EDG module holding the backend code-generation dispatcher and its per-kind emitters. Every `gen_*` routine — `gen_routine_decl`, `gen_template`, `gen_lambda` — lives here. Identified via the embedded assertion path-strings. See [Backend Code Generation](./pipeline/backend.md). |
| `nv_transforms.c` | The single NVIDIA-authored EDG module that holds the bulk of NVIDIA's CUDA-specific transformations. It is called from `class_decl.c`, `cp_gen_be.c`, and `statements.c` but does not itself call back into the EDG parser, producing a clean lateral extension boundary. See [Methodology — Call Graph Analysis](./methodology.md#call-graph-analysis). |

## C. CUDA / NVIDIA Additions

These terms name the CUDA-specific layer NVIDIA added on top of the EDG base. They cover kernel stubs, separate-compilation modes, the lambda wrapper machinery, and the registration glue that binds host code to its fatbin payload.

| Term | Meaning |
| --- | --- |
| `.cudafe1.c` / `.cudafe1.cpp` | The downstream filename `nvcc` gives the `.int.c` output of cudafe++. The format is identical; the suffix differs by language mode (the C++ form stays `.cpp`). See [.int.c File Format](./output/int-c-format.md). |
| `__cudaRegisterFatBinary` | The CUDA runtime entry point cudafe++ emits a call to from the generated `.int.c` constructor. Receives the fatbin pointer and registers the host translation unit so that subsequent `__cudaRegisterFunction` and `__cudaRegisterVar` calls have a binding context. See [Module ID & Registration](./output/module-id.md). |
| `__device_stub__` | The naming prefix `gen_routine_decl` applies to the static-linkage host-side launcher emitted for every `__global__` kernel. The pattern is `static void __device_stub__<mangled_kernel_name>(...) { cudaLaunchKernel(...); }`. The visible kernel symbol is a thin wrapper around this stub. See [Kernel Stub Generation](./cuda/kernel-stubs.md). |
| Device stub | The host-callable forwarding function emitted into `.int.c` for every `__global__` kernel. The body is a `cudaLaunchKernel` call; the symbol carries the `__device_stub__<name>` prefix and `static` linkage by default. Synonymous with "kernel stub"; the "host stub" entry below names the same routine from the host compiler's perspective. See [Kernel Stub Generation](./cuda/kernel-stubs.md). |
| Fatbin (fatbinary) | The downstream container produced by the `fatbinary` tool that bundles PTX and SASS payloads for multiple GPU architectures into one blob. cudafe++ does not produce a fatbin itself but emits the `__cudaRegisterFatBinary` glue and the [Module ID](./output/module-id.md) that the runtime uses to bind a host TU to its fatbin payload. Prose uses "fatbin" and "fatbinary" interchangeably (the latter also names the linker tool). |
| `_GLOBAL__N_<module_id>` | The Itanium-ABI mangling prefix for anonymous namespaces, with the [Module ID](./output/module-id.md) appended to give each TU a unique anonymous-namespace name. The `_NV_ANON_NAMESPACE` macro expands to this form. See [Module ID & Registration](./output/module-id.md). |
| Host reference array | One of six `.nvHR*` ELF sections (`.nvHRKE`/`KI`/`DE`/`DI`/`CE`/`CI`) embedded into the `.int.c` output. The array lists mangled names of `__global__` kernels and `__device__`/`__constant__` variables. The CUDA runtime reads these at startup to build the host-device symbol binding table. See [Host Reference Arrays](./output/host-reference-arrays.md). |
| Host stub | The same routine as a device stub viewed from the host compiler's perspective: it is the host-linkage entry point that nvcc's downstream tools resolve. The `--host-stub-linkage-explicit` and `--static-host-stub` CLI flags control its linkage. See [Kernel Stub Generation](./cuda/kernel-stubs.md). |
| JIT mode | The compilation mode that emits PTX only (no SASS), leaving final SASS compilation to the driver at run time on the user's GPU. Gated by `--cuda-jit-mode` and related flags. See [JIT Mode](./cuda/jit-mode.md). |
| Kernel stub | Synonym for device stub. The host-callable forwarding function emitted into `.int.c` for every `__global__` kernel. See [Kernel Stub Generation](./cuda/kernel-stubs.md). |
| Module ID | A CRC32-derived hexadecimal string (computed by `make_module_id` at `sub_5AF830`) that uniquely identifies a TU's fatbin and seeds anonymous-namespace mangling. The same module ID appears in `_GLOBAL__N_<module_id>` and in the `__fatBinSegment` registration call. See [Module ID & Registration](./output/module-id.md). |
| `_NV_ANON_NAMESPACE` | The macro cudafe++ emits at the top of `.int.c` that expands to `_GLOBAL__N_<module_id>`. Using a TU-unique name for every anonymous namespace prevents cross-TU symbol collisions under RDC linkage. See [Module ID & Registration](./output/module-id.md). |
| `__nv_dl_wrapper_t` | The "device-launchable" wrapper template injected into the host stream by [Preamble Injection](./lambda/preamble-injection.md). Wraps a closure object so that the device side can be reached from the host launcher through a normal function-pointer call rather than through a vtable. |
| `__nv_hdl_helper` | A small static helper template injected alongside `__nv_hdl_wrapper_t` that resolves the closure-call target at compile time. Used to keep the host and device sides of a host-device lambda in sync without runtime dispatch. See [Host-Device Lambda Wrapper](./lambda/host-device-wrapper.md). |
| `__nv_hdl_wrapper_t` | The "host-device-lambda" wrapper template injected into the host stream that wraps a host-device closure into a value the kernel can take by parameter. Deliberately avoids vtables and uses static function pointers instead, so that the device side has no virtual-call dependency. See [Host-Device Lambda Wrapper](./lambda/host-device-wrapper.md). |
| Preamble injection | The backend's emission of `__nv_dl_wrapper_t`, `__nv_hdl_wrapper_t`, and `__nv_hdl_create_wrapper_t` template definitions into the host stream, triggered by the synthetic type `__nv_lambda_preheader_injection`. Performed by `nv_emit_lambda_preamble` (`sub_6BCC20`). See [Preamble Injection](./lambda/preamble-injection.md). |
| RDC (Relocatable Device Code) | Separate compilation mode for device code, enabled by `--device-c` (CLI flag 77, also written `-rdc=true`). Allows device symbols to be defined in one TU and referenced from another, with the device linker `nvlink` resolving cross-TU references at link time. The opposite is whole-program mode (the default). Wiki prose uses "RDC", "separate compilation mode", and "sepcomp" as synonyms; the diagnostic tag `device_launch_no_sepcomp` keeps the latter form alive. See [RDC Mode](./cuda/rdc-mode.md). |
| Sepcomp | Synonym for RDC. The internal term EDG and NVIDIA share for "separate compilation": each TU compiles its own device code independently and emits relocatable PTX for the device linker to merge. See [RDC Mode](./cuda/rdc-mode.md). |
| `__wrapper_device_stub_` | The naming prefix `gen_routine_decl` applies to the visible host symbol that wraps the `__device_stub__` launcher. The pattern is `void <name>(...) { __wrapper__device_stub_<name>(...); }`. See [Kernel Stub Generation](./cuda/kernel-stubs.md). |

## D. Diagnostics and Pragma Engine

These terms cover the error-reporting subsystem and the pragma-handling layer that the EDG core and NVIDIA's CUDA extensions share. The diagnostic system is reachable from every other subsystem because almost every front-end function can emit an error.

| Term | Meaning |
| --- | --- |
| `nv_diag_suppress` | The CUDA pragma form (`#pragma nv_diag_suppress <N>`) that suppresses a specific diagnostic by number. Backed by an entry in the active pragma stack; the suppression scope is determined by where the matching `nv_diag_default` or `nv_diag_pop` appears. See [SARIF & Pragma Control](./diagnostics/sarif-pragmas.md). |
| Pragma engine | The cudafe++ subsystem implemented in `pragma.c` that recognizes the `#pragma` directives the lexer extracts, dispatches to per-pragma handlers (CUDA's `nv_diag_*`, EDG's `#pragma once`, `#pragma message`, etc.), and updates the pragma stack. See [Pragma Engine](./edg/pragma-engine.md). |
| Pragma stack | The per-TU stack of pragma-state frames the pragma engine maintains. Each `#pragma nv_diag_push` allocates a new frame inheriting the current suppression set; each `#pragma nv_diag_pop` discards the top frame. Lookup of "is this diagnostic suppressed?" reads the top of the stack. See [SARIF & Pragma Control](./diagnostics/sarif-pragmas.md). |
| SARIF | Static Analysis Results Interchange Format — the JSON-schema diagnostic format the cudafe++ diagnostic emitter can produce in addition to the human-readable text form. Selected by `--sarif` CLI flags. See [SARIF & Pragma Control](./diagnostics/sarif-pragmas.md). |

## E. Internal C++ Idioms (Hex-Rays Output)

These terms label recurring shapes in the Hex-Rays pseudocode that the wiki cites by name. They are not part of the EDG vocabulary but help discuss the binary's runtime behavior in compact form.

| Term | Meaning |
| --- | --- |
| Dispatcher | A function that switches on a kind byte (entry kind, token kind, attribute kind, statement kind) and forwards to one of many handler functions. `sub_47ECC0` (`gen_template`) is the source-sequence dispatcher; `attribute_display_name` dispatches on attribute kind. |
| Factory | A static method that constructs a template-instantiated wrapper. Used here specifically for `__nv_hdl_create_wrapper_t::__nv_hdl_create_wrapper`, which the backend emits at every host-device lambda site to wrap the closure into an `__nv_hdl_wrapper_t`. See [Host-Device Lambda Wrapper](./lambda/host-device-wrapper.md). |
| Vtable slot | One pointer-sized entry in a C++ virtual method table. Hex-Rays reconstructs virtual calls as `*(qword *)(vptr + 8*N)(...)`; the analysis assigns each slot index to a method by tracing the override matrix. See [Virtual Override Matrix](./reference/virtual-override-matrix.md). The host-device lambda wrapper deliberately avoids vtables and uses static function pointers instead. |

## Reading Notes

Module names appear in backticks: `attribute.c`, `il.c`, `fe_wrapup.c`. Function names are written with their address in the same form Hex-Rays produces: `gen_routine_decl` (`sub_47BFD0`). Struct field offsets are written as bare hex offsets when the wiki cites them in pseudocode (e.g., `entity + 182` for the execution-space bitfield), and otherwise are described by their reconstructed names. Cross-links into the wiki always point at the most specific page that develops the term; the [Methodology](./methodology.md) page is the authoritative source for the evidence chain behind every claim.
