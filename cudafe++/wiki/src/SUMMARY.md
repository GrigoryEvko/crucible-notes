# Summary

[cudafe++ v13.0 — Reverse Engineering Reference](./index.md)

---

# Overview

- [Function Map](./function-map.md)
- [Binary Layout](./binary-layout.md)
- [Methodology](./methodology.md)

# Compilation Pipeline

- [Pipeline Overview](./pipeline/overview.md)
- [Entry Point & Initialization](./pipeline/entry.md)
- [CLI Processing](./pipeline/cli.md)
- [Frontend Invocation](./pipeline/frontend.md)
- [Frontend Wrapup](./pipeline/fe-wrapup.md)
- [Backend Code Generation](./pipeline/backend.md)
- [Timing & Exit](./pipeline/timing-exit.md)

# CUDA Execution Model

- [Execution Spaces](./cuda/execution-spaces.md)
- [Memory Spaces](./cuda/memory-spaces.md)
- [Cross-Space Call Validation](./cuda/cross-space-validation.md)
- [Device/Host Separation](./cuda/device-host-separation.md)
- [Kernel Stub Generation](./cuda/kernel-stubs.md)
- [Kernel Launch Syntax (`<<<...>>>`)](./cuda/kernel-launch-syntax.md)
- [RDC Mode](./cuda/rdc-mode.md)
- [JIT Mode](./cuda/jit-mode.md)
- [Architecture Feature Gating](./cuda/arch-gating.md)

# CUDA Attributes

- [Attribute System Overview](./attributes/overview.md)
- [\_\_global\_\_ Function Constraints](./attributes/global-function.md)
- [Launch Configuration](./attributes/launch-config.md)
- [\_\_grid\_constant\_\_](./attributes/grid-constant.md)
- [\_\_managed\_\_ Variables](./attributes/managed-variables.md)
- [Minor Attributes](./attributes/minor-attributes.md)
- [\_\_nv\_\* Builtin Intrinsic Names](./attributes/nv-builtin-intrinsics.md)

# Lambda Transformations

- [Extended Lambda Overview](./lambda/overview.md)
- [Device Lambda Wrapper](./lambda/device-wrapper.md)
- [Host-Device Lambda Wrapper](./lambda/host-device-wrapper.md)
- [Capture Handling](./lambda/capture-handling.md)
- [Preamble Injection](./lambda/preamble-injection.md)
- [Lambda Restrictions](./lambda/restrictions.md)

# EDG Intermediate Language

- [IL Overview](./il/overview.md)
- [IL Allocation](./il/allocation.md)
- [IL Tree Walking](./il/walking.md)
- [Keep-in-IL (Device Code Selection)](./il/keep-in-il.md)
- [IL Display](./il/display.md)
- [IL Comparison & Copy](./il/comparison-copy.md)

# Host Output Generation

- [.int.c File Format](./output/int-c-format.md)
- [CUDA Runtime Boilerplate](./output/cuda-runtime.md)
- [Host Reference Arrays](./output/host-reference-arrays.md)
- [Module ID & Registration](./output/module-id.md)

# EDG Frontend Internals

- [EDG 6.6 Overview](./edg/overview.md)
- [Lexer & Tokenizer](./edg/lexer.md)
- [Expression Parser](./edg/expression-parser.md)
- [Declaration Parser](./edg/declaration-parser.md)
- [Overload Resolution](./edg/overload-resolution.md)
- [Template Engine](./edg/template-engine.md)
- [CUDA Template Restrictions](./edg/template-cuda.md)
- [Constexpr Interpreter](./edg/constexpr-interpreter.md)
- [Name Mangling](./edg/name-mangling.md)
- [Type System](./edg/type-system.md)
- [Pragma Engine](./edg/pragma-engine.md)

# Error & Diagnostic System

- [Diagnostic Overview](./diagnostics/overview.md)
- [CUDA Error Catalog](./diagnostics/cuda-errors.md)
- [Format Specifiers](./diagnostics/format-specifiers.md)
- [SARIF & Pragma Control](./diagnostics/sarif-pragmas.md)

# Data Structures

- [Entity Node Layout](./structs/entity-node.md)
- [Scope Entry](./structs/scope-entry.md)
- [Translation Unit Descriptor](./structs/translation-unit.md)
- [Type Node](./structs/type-node.md)
- [Template Instance Record](./structs/template-instance.md)

# Configuration

- [CLI Flag Inventory](./config/cli-flags.md)
- [EDG Build Configuration](./config/edg-build-config.md)
- [Architecture Detection](./config/arch-detection.md)
- [Experimental Flags](./config/experimental-flags.md)

# Reference

- [EDG Source File Map](./reference/edg-source-map.md)
- [Global Variable Index](./reference/global-variables.md)
- [Token Kind Table](./reference/token-kinds.md)
- [Error Message Catalog](./reference/error-catalog.md)
- [Virtual Override Matrix](./reference/virtual-override-matrix.md)
- [Glossary](./glossary.md)
