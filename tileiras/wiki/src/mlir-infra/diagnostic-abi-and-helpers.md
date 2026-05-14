# Diagnostic ABI and Helpers

## Abstract

Every user-visible error, warning, note, and remark produced by Tileiras flows through a single 208-byte
`Diagnostic` body. Verifiers, parsers, conversion patterns, pass drivers, and dialect-init routines all
seed that body through one of three constructors, stream fragments into a 4-slot inline argument
buffer, and rely on an `InFlightDiagnostic` RAII wrapper to flush through a context-registered handler.
This page documents the exact body layout, the 24-byte `DiagnosticArg` 3-tuple, the bit-packed severity
word at `+0x10`, and the constructor / streamer / destructor triad that builds and tears it down.

## Diagnostic Body

`sub_44A8C20(0xD0)` allocates the heap body, zero-fills it, and hands it off to one of the seeds for
population. The first 200 bytes are state; the remainder is a 64-byte inline sink buffer the default
handler uses when no external sink is registered. Offsets below come verbatim from `sub_446EC50`
(the `emitOpError` seed) and `sub_4448AC0` (the destructor).

```c
typedef struct Diagnostic {
    /*+0x00*/ Location          loc;                   // interned LocationAttr*, 0 once flushed
    /*+0x10*/ uint16_t          packed_severity_flags; // class | (op_prefix<<8) | (trace<<9)
    /*+0x18*/ DiagnosticArg    *args_begin;            // == &inline_args[0] until spill
    /*+0x20*/ uint32_t          args_size;             // low dword of the 0x400000000 init
    /*+0x24*/ uint32_t          args_cap;              // high dword; starts at 4
    /*+0x28*/ DiagnosticArg     inline_args[4];        // 24 B per slot = 96 B
    /*+0x88*/ SmallVector<std::string, 0> owned_strings;
    /*+0xA0*/ SmallVector<Diagnostic, 0>  notes;       // child diagnostics, 0xC0-byte bodies
    /*+0xB8*/ raw_ostream      *inline_sink;           // initialised to self+0xC8 by ctor
    /*+0xC8*/ uint8_t           alive;                 // 1 after ctor, 0 once emitted
} Diagnostic;                                          // sizeof = 208 (0xD0)
```

`args_begin` at `+0x18` points into the inline buffer at `+0x28` until the argument count crosses
four; the streamer then promotes to heap storage and rewrites the pointer. `owned_strings` at `+0x88`
holds any payload the diagnostic had to copy — typically `Twine` outputs and any `const char *` whose
lifetime is shorter than the body. `notes` at `+0xA0` is a vector of pointers to child `Diagnostic`
bodies; children are slightly smaller (`0xC0`) because they reuse the parent's sink rather than
carrying their own.

## DiagnosticArg 3-Tuple

Every streamed argument is a 24-byte 3-tuple. The streamer dispatches on `kind` and interprets
`value` and `aux` according to the table below. The constructor sets `kind` to `1` (placeholder) on
every inline slot, so an unstreamed diagnostic prints no body text.

```c
typedef struct DiagnosticArg {
    /*+0x00*/ uint8_t  kind;          // 1..6, see table
    /*+0x01*/ uint8_t  pad[7];
    /*+0x08*/ uint64_t value;         // scalar or primary pointer
    /*+0x10*/ uint64_t aux;           // length, twine kind, or unused
} DiagnosticArg;                      // sizeof = 24
```

| Kind | Meaning                                                                      |
|------|------------------------------------------------------------------------------|
| 1    | placeholder (unset; ctor default)                                            |
| 2    | int64 — value lives in `value`                                               |
| 3    | const char* — pointer in `value`, length recomputed by the printer          |
| 4    | heap-string — body owns a `std::string` in `owned_strings`                  |
| 5    | StringRef — `{value=ptr, aux=len}`                                           |
| 6    | Twine — `{value=twine_ptr, aux=twine_kind}`; large path renders into kind 4 |

The streamer at `sub_44488C0` walks `args_begin` at a 24-byte stride. New arguments fill
`inline_args[0..3]` first; the fifth and any later argument spills to a heap-grown vector reached
through the same `args_begin` pointer, so consumers never special-case the small-buffer state.

## Severity Word

The 16-bit field at `+0x10` packs the severity class into the low byte and two boolean flags into the
next byte. The upper bits are reserved and zero in every observed value.

| Bit range | Field         | Encoding                                                |
|-----------|---------------|---------------------------------------------------------|
| `0..7`    | severity class | `1=Note`, `2=Warning`, `3=Error`, `4=Remark`           |
| `8`       | op-prefix flag | printer prepends `' op "<name>"` boilerplate           |
| `9`       | trace flag    | a trace/child note is attached                          |
| `10..15`  | reserved      | always zero                                             |

Five concrete words appear across the binary: `0x101`, `0x103`, `0x104`, `0x302`, `0x503`. `0x101` is
the constructor default — class 1 (Note) with the op-prefix bit set, used by `attachNote` paths.
`0x103` is the canonical verifier-failure word: Error class with op-prefix. `0x104` is the Remark
flavour used by the `diagnostic emitted with trace:\n` child diagnostic. `0x302` and `0x503` are
inliner-set words whose bit 9 says the diagnostic carries a structured trace note. The shape of
these bytes mirrors the `flags |= 4` and `pass[5] |= 4` failure-bit patterns used by the `Schedule`
and pass-pipeline state machines elsewhere in the binary, which keeps the printer from having to
reach back through those records when it decides whether to emit the `error:` prefix.

## Construction

Three seeds populate the body. `sub_446EC50` is the `emitOpError` constructor: given a `Location`
and an `Operation*`, it allocates 208 bytes via `sub_44A8C20(0xD0)`, zero-fills, writes the location
to `+0x00`, writes packed severity `0x103` to `+0x10`, sets `args_begin = self+0x28`, packs
`args_size=0` and `args_cap=4` into the 64-bit word at `+0x20` through the immediate `0x400000000`,
points `inline_sink` at `self+0xC8`, sets `alive=1`, and finally streams in the op-name fragment
through `sub_44487A0` (which emits an `' op "<name>"` kind-3 argument).

`sub_4470160` is a 12-byte thin wrapper forwarding to `sub_446EC50` without the op-name prefix —
the free-standing `emitError` entry point for parser and driver code that has no `Operation *` to
hand. `sub_444B3A0` is the generic constructor: it takes a `Location` and an explicit severity class,
and switches on the stack-trace path when the global `--mlir-print-stacktrace-on-diagnostic` toggle
is set. With that toggle live, it creates a child diagnostic through `sub_444B160`, streams the
literal `"diagnostic emitted with trace:\n"`, sets the child's severity word to `0x104`, and pushes
the rendered backtrace as a kind-4 argument.

## Streaming Arguments

`sub_44488C0` is `Diagnostic::operator<<(DiagnosticArg&&)` and its char-pointer / StringRef overloads.
Each call writes a 24-byte record into `args_begin[args_size]` and bumps `args_size`. When
`args_size` reaches `args_cap`, the streamer reallocates onto the heap and rewrites `args_begin` to
the new buffer; the four inline slots at `+0x28` stay in place but zeroed, so the destructor can
still scan them safely.

The streamer also owns the kind-4 promotion path. When a streamed `Twine` does not fit the small
representation, `sub_4581720` renders it into a `std::string`, pushes the string into
`owned_strings` at `+0x88`, and rewrites the argument's `kind` to `4` with `value` pointing into the
owning vector. The same mechanism rescues a `const char *` whose lifetime is known to be shorter
than the diagnostic — the helper copies the bytes into `owned_strings` and upgrades the kind from 3
to 4 so the body is self-contained at flush time.

## Notes and Trace Chains

`attachNote(Location)` is implemented by `sub_444B160`. It allocates a 192-byte child body (no inline
sink), copies the parent's location into the child's `+0x00` if the caller did not pass a fresh one,
appends the child pointer to the parent's `notes` vector at `+0xA0`, and returns a reference to the
child so the caller can stream additional fragments into it. The child's `inline_sink` is left null;
the parent's destructor will render the child against the parent's sink at flush time.

```c
Diagnostic *attach_note(Diagnostic *parent, Location loc) {
    Diagnostic *child = (Diagnostic *)sub_44A8C20(0xC0);
    memset(child, 0, 0xC0);
    child->loc = loc ? loc : parent->loc;
    child->packed_severity_flags = 0x101;
    child->args_begin = &child->inline_args[0];
    child->args_size = 0;
    child->args_cap = 4;
    child->alive = 1;
    vector_push(&parent->notes, child);
    return child;
}
```

The trace path uses the same primitive, but the parent constructor sets bit 9 of its own severity
word so the printer knows to walk into the child without re-checking the global toggle.

## Sink Chain

`inline_sink` at `+0xB8` is an `raw_ostream*`. The constructor points it at the inline buffer at
`+0xC8`, which gives the default handler a 64-byte small-string sink for assembling the formatted
message. `op->emitError(...)` and `op->emitWarning(...)` overwrite this pointer with `llvm::errs()`
when the caller wants direct stderr output; capture tools (such as the diagnostic-handler interface
that backs in-process IR tests) replace it with their own ostream.

The destructor flushes through whatever sink is currently installed, so the choice of sink is the
only place a caller can intercept the formatted output before it reaches the registered context
handler. Replacing the sink does not bypass the handler chain — the handler still runs against the
structured `Diagnostic` after the sink flushes the inline rendering.

## Destruction and Flush

`sub_4448AC0` is the destructor — the only function permitted to flip `alive` from `1` to `0`. The
flush path is short:

```c
void diagnostic_destroy(Diagnostic *d) {
    if (d->loc != 0) {
        sub_44488C0(d->loc->context->engine_mutex, d);   // engine handler chain
        d->loc = 0;
    }
    if (d->alive == 1) {
        d->alive = 0;
        for (Diagnostic *n : d->notes)   { diagnostic_destroy(n); free(n); }
        for (std::string &s : d->owned_strings) { s.~basic_string(); }
        if (d->args_begin != &d->inline_args[0]) {
            free(d->args_begin);
        }
    }
}
```

`alive` at `+0xC8` is the double-emit guard. A moved-out diagnostic — the common case when an
`InFlightDiagnostic` is returned as a `LogicalResult` — clears its location pointer; the destructor
of the moved-from shell then sees `loc == 0` and skips the handler call, then sees `alive == 0` and
skips the cleanup. The same byte is why `attachNote` cannot be called after a diagnostic has been
flushed: the engine clears `loc` first, so a follow-on append would have nothing to attach to and the
printer would lose the parent context.

## Engine Entry Point

`sub_44488C0` is the function the destructor calls when `loc != 0`. It takes the engine's pthread
mutex and the diagnostic body, locks the mutex, walks the registered diagnostic handler chain — an
intrusive linked list rooted in the engine — and offers the diagnostic to each handler in turn. The
first handler that returns `true` consumes the diagnostic. If no handler consumes it, the default
handler emits an `"error: "` prefix when the severity class is exactly `2`, walks the argument
vector through `sub_4448570` to render each kind, emits a trailing newline, and flushes the sink.

The engine mutex is released whichever handler accepted the diagnostic, so a handler that throws is
a hard contract violation. The codebase relies on every handler being noexcept and on the engine
never being re-entered from inside a handler callback.

## Reimplementation Checklist

1. Allocate the body as exactly 208 bytes; respect the offsets at `+0x10`, `+0x18`, `+0x28`, `+0x88`,
   `+0xA0`, `+0xB8`, and `+0xC8`.
2. Initialise `args_begin` to point at the inline buffer; pack `args_size=0` and `args_cap=4` into
   the 64-bit word at `+0x20`.
3. Set `alive=1` in the constructor and clear it only in the destructor.
4. Use the packed 16-bit severity word with class in the low byte and op-prefix / trace flags in
   bits 8 and 9.
5. Spill arguments to heap once the inline four slots are full; rewrite `args_begin`.
6. Use 192-byte child bodies for notes; share the parent's sink at flush time.
7. Treat `loc == 0` as the move-from / already-flushed sentinel.
8. Emit the `error:` prefix only for severity class `2`; rely on the op-prefix bit for the rest.

## How to Recognize in a Binary

The 208-byte (`0xD0`) allocation immediately followed by a zero-fill, a write of one of the five
canonical severity words (`0x101`, `0x103`, `0x104`, `0x302`, `0x503`) to the `+0x10` offset, and the
single 64-bit `0x400000000` store at `+0x20` (`{size=0, cap=4}`) is the unambiguous fingerprint. Any
function that allocates `0xD0` bytes through `sub_44A8C20` and then stores `0x400000000` at offset
`+0x20` is a `Diagnostic` constructor, regardless of which severity it ultimately writes.

The complementary destructor fingerprint at `sub_4448AC0` is the `loc != 0 ? engine_call : skip`
guard followed by the `alive == 1` cleanup gate. Any function that branches on a qword at `+0x00` of
a 208-byte object, then on a byte at `+0xC8` of the same object, is `Diagnostic::~Diagnostic`. The
double-flush guard is the same byte at `+0xC8`.

## Consumers

`Operation::emitOpError` (`sub_446EC50`) is the primary entry — every verifier in the binary calls it
when an invariant fails. The pattern application drivers documented in
[Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) seed `0x103`-class diagnostics when a
`matchAndRewrite` returns failure with an explanation. The scheduler reuses the same severity-bit
pattern in its `Schedule.flags |= 4` failure-bit encoding; see
[Modulo Scheduler and Rau-Style Placement](../scheduler/modulo-scheduler-and-rau.md).

## Cross-References

[Operation Layout](operation-layout.md) covers the `Operation` header that `emitOpError` reads its
mnemonic from. [Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) documents the
context that owns the diagnostic engine and its handler chain. [Modulo Scheduler and Rau-Style
Placement](../scheduler/modulo-scheduler-and-rau.md) reuses the same severity-bit pattern in its
`Schedule.flags |= 4` failure-bit encoding.
