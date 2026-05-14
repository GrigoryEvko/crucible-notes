# Diagnostic Helpers

## Abstract

Most attribute and operation verifiers in `tileiras` share a small set of diagnostic helper shapes. They split into three roles: clones of `Diagnostic::operator<<(const char* literal)` that push borrowed C strings; a generic 24-byte `SmallVector::push_back` used for MMA-shape candidate records; and a `SmallVector<std::string, N>` family used when verifiers build richer candidate tuples. These helpers are pre-emission infrastructure — actual formatted text still flows through `raw_svector_ostream` into a single owned diagnostic string when needed.

A trap to avoid: the 24-byte stride appears in two unrelated roles in this binary and must not be conflated. The literal-append helpers push a `DiagnosticArgument` (kind 3, borrowed C-string) whose stride happens to be 24 bytes. The MMA allowlist verifier pushes 24-byte *candidate records* that are not diagnostic arguments at all and carry no kind tag. Both look like 24-byte `SmallVector::push_back` shapes in the binary, but only the first interacts with the diagnostic engine. The companion page for the `StringRef` / `Twine` / `raw_svector_ostream` chain that human-facing diagnostics actually flow through is [Twine, StringRef, format](twine-stringref-and-format.md).

## Six byte-identical Diagnostic literal-append clones

The literal-append helpers all implement the same operation. Each accepts `(Diagnostic*, const char*)`, builds a stack `DiagnosticArgument` with kind 3, computes the literal length with `strlen`, grows the embedded argument vector when needed, copies one 24-byte argument slot, and increments the argument count.

The embedded `Diagnostic` body layout these clones index is:

| Offset | Width | Field |
|--------|------:|-------|
| `+0x00` | 8 B | `Location*` |
| `+0x08` | 8 B | ctx ptr / packed severity dword |
| `+0x10` | 8 B | `args_begin` (24-B-stride `DiagArg` array head) |
| `+0x18` | 4 B | `args_size` |
| `+0x1C` | 4 B | `args_cap` (initialised to `4`) |
| `+0x20` | 96 B | `inline_args[4]` (four 24-B `DiagArg` slots) |

The packed initial state stores zero size and inline capacity four. The diagnostic body may be embedded in a larger stack frame, so helpers are written against the argument-vector head rather than a whole diagnostic object with one fixed surrounding layout.

| Helper family | Signature | Stride | Notes |
|---|---|---:|---|
| literal append clone | `(Diagnostic*, const char*)` | 24 B | pushes borrowed C-string argument, kind 3 |
| hot verifier clone | `(Diagnostic*, const char*)` | 24 B | same body emitted into verifier-heavy translation units |
| support-verifier clone | `(Diagnostic*, const char*)` | 24 B | same body used by tcgen05 and TMA support verifiers |

Each clone is the same operation as the named `Diagnostic::operator<<(const char*)`: wrap as kind 3, compute length, push a 24-byte argument. Every observed call site passes a static string literal.

## Generic 24-byte SmallVector push_back

The generic 24-byte push helper looks like a seventh literal clone at first glance, but it is a plain copy into a standalone `SmallVector`. Its input is a pre-built 24-byte record, not a C string; it stamps no diagnostic kind; it never calls `strlen`. The vector head layout is `{begin, size, cap, inline}`.

The MMA multiplicand allowlist verifier uses it to build shape-multiplicand candidate records. The 24-byte stride coincidence with `DiagArg` is incidental — the elements are MMA-shape records, not diagnostics.

## SmallVector<std::string,N> lifetime managers

The `SmallVector<std::string, N>` helper family manages verifier-side string vectors. Each `std::string` uses libc++ SSO with a 48-byte element stride. The outer vector head is the usual `{begin, size, cap, inline}` layout.

| Role | Stride | Notes |
|---|---:|---|
| move into fresh storage | 48 B | Moves SSO or heap-backed strings into grown storage. |
| `emplace_back(StringRef)` | 48 B | Grows if needed, then constructs a string from borrowed bytes. |
| `push_back(const std::string&)` | 48 B | Handles self-aliasing across growth. |
| copy assignment | 48 B | Reuses, grows, or destroys elements based on source and destination size. |
| reserve/grow | 48 B | Allocates a larger buffer and moves old strings. |
| move assignment | 48 B | Uses inline or pointer-swap branches. |
| `~SmallVector<std::string, N>()` | 48 B | Frees non-SSO payloads and non-inline vector buffers. |
| `~SmallVector<TupleRecord, N>()` | 112 B | Walks nested string vectors before freeing the outer buffer. |

The grow helper walks existing elements, initializes SSO headers in the new slots, copies non-empty string bytes, then frees old non-SSO payloads in reverse order. The emplace helpers build strings from `StringRef`; copy assignment branches on source size versus destination size and capacity; move assignment uses inline and pointer-swap branches.

The tuple-record destructor handles a nested vector at the end of each outer element. It frees the inner string payloads first, frees the inner buffer if it is not inline, then continues the outer reverse walk.

The MMA allowlist verifier uses three inner per-multiplicand vectors, deep-copies them into outer tuple slots, then move-assigns the tuple into the candidate list. On the miss path, the human-facing diagnostic takes the `raw_ostream` formatted `SmallString` route rather than formatting through these vector helpers.

## Reimplementation Notes

```text
append_literal(diag, literal):
    arg = DiagnosticArgument(kind="cstring", ptr=literal, len=strlen(literal))
    diag.args.push(arg)

build_candidate_strings(candidates):
    records = SmallVector()
    for candidate in candidates:
        records.push(make_candidate_record(candidate))
    return records
```

Do not confuse the 24-byte MMA candidate records with diagnostic arguments. They share a stride, but only diagnostic arguments carry a diagnostic kind tag.
