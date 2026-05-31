# Twine, StringRef, and format

## Abstract

The `tileiras` binary inherits the LLVM-style trio of cheap string types — `StringRef`, `Twine`, and `format_object` — but only `StringRef` survives in canonical form. Every diagnostic message, verifier complaint, and register / opcode mnemonic the NVPTX printer sinks into `raw_svector_ostream` ultimately bottoms out in a 16-byte `(ptr, len)` pair: `StringRef`. The Twine concatenation type survives only as a fall-back rendering path inside the `Diagnostic::operator<<` switch (kinds 5 and 6, plus the catch-all that materialises a Twine into a `SmallString` and re-emits it as a kind-3 DiagArg). The `formatv("sm_{}", N)`-style format engine collapses to a small `raw_ostream`-backed sequence of `write(ptr, len)` calls. No `format_object<...>` virtual dispatch survives — every observed `"sm_{N}"` callsite is open-coded as `<<` operators against a `raw_svector_ostream` rather than a templated `formatv`.

This page catalogues the three string-passing ABIs the binary actually uses: the 16-byte `StringRef` calling convention shared by attribute parsers, printers, and verifiers; the `Twine` append family that drives diagnostic concatenation; and the `raw_svector_ostream` chain that NVVM verifiers use to build candidate-tuple strings before sinking them into a single owned-string DiagArg.

## StringRef 16-byte `(ptr, len)` ABI

`StringRef` is passed by value as two machine words, returned as two machine words, and stored as a 16-byte structure inside larger records such as diagnostic arguments, attribute storage, and `SmallString` heads.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| `+0x00` | 8 | `const char *ptr` | static-string pointer or heap-owned buffer |
| `+0x08` | 8 | `size_t len` | byte length; never includes NUL |

Every consumer enforces the 16-byte stride. `Diagnostic::operator<<(StringRef)` copies `(ptr, len)` into a 24-byte diagnostic-argument slot and stamps the appropriate argument kind. The `const char*` overloads first compute `strlen`, then use the same 24-byte push shape with kind 3.

## Twine append family

`Twine` is the LLVM lazy-concatenation tree — leaves are `StringRef`s, integers, or raw `const char*` pointers, interior nodes are tagged concat operators. `tileiras` retains this design as a fall-back diagnostic rendering path. A diagnostic argument arriving as a Twine triggers the renderer to walk the tree into a temporary `SmallString`, then re-push the rendered bytes as an ordinary string argument. In practice, diagnostics that need formatting usually flow through `raw_svector_ostream` instead.

## raw_svector_ostream

The canonical diagnostic-formatting pipeline in `tileiras` is a `raw_svector_ostream` layered over a stack `SmallString`. Verifiers stream literal fragments, separators, and typed values into that buffer, then promote the final string into a single owned diagnostic argument at flush time. Diagnostics such as `"unimplemented variant for MMA shape <...>"` use the same shape.

| Operation | Role | Notes |
|---|---|---|
| stream C string | `raw_ostream::operator<<(StringRef)` | Computes length and forwards to explicit write. |
| write bytes | `raw_ostream::write(const char*, size_t)` | Emits literal separators like `", "` and `">"`. |
| stream typed value | typed-value stream operator | Emits `sm_{N}` and similar formatted scalars from stored type information. |

The chain is invoked from HH02 line-by-line as:

```c
out << "unimplemented variant for MMA shape <";
out << multiplicand_a;
out.write(", ", 2);
out << multiplicand_b;
out.write(", ", 2);
out << multiplicand_c;
out << ">";
```

Here `out` is a `raw_svector_ostream` aimed at a stack `SmallString`. The verifier later promotes the `SmallString` into a single owned diagnostic argument before returning control to the diagnostic engine.

## `formatv("sm_{}")` style format strings

The binary contains no surviving `formatv` template instantiation. Common SM-version strings such as `"sm_50"`, `"sm_52"`, `"sm_60"`, and `"sm_120"` are selected by lookup table when possible. The generic `sm_{N}` case writes `"sm_"` and then streams the decimal compute capability. PTX register prefixes such as `"%r{N}"`, `"%f{N}"`, `"%p{N}"`, and `"%rd{N}"` follow the same pattern.

## Owned-string DiagArg kind=4

Kind 4 is the only diagnostic-argument flavor that owns its payload. It stores a pointer to a `{data, length}` pair rather than the direct `(ptr, len)` pair used by borrowed strings. When the diagnostic receives kind 4 it heap-copies the bytes and appends the allocation to the diagnostic's owned-string vector. Verifiers use kind 4 for stack `SmallString` buffers because the source storage would otherwise dangle by the time the diagnostic engine emits. The diagnostic destructor frees those owned buffers.

## Reimplementation Notes

```text
emit_formatted_error(parts):
    buffer = SmallString()
    out = raw_svector_ostream(buffer)

    for part in parts:
        if part.is_literal:
            out.write(part.ptr, part.len)
        else:
            out << part.value

    diag << DiagnosticArg.owned_string(buffer)
```

The ownership rule is the important part: borrowed `StringRef` arguments may point at static strings, but any stack-built `SmallString` must be promoted to an owned diagnostic argument before the stack frame unwinds.
