# Pipe_ and Mutex_ Value-Header Layout

## Abstract

Tileiras's AWS (warp-specialised) scheduler emits two families of IR-visible SSA values that name the producer/consumer handshakes flowing between agents: `Pipe_<N>` for the streaming case and `Mutex_<N>` for the exclusive case. The same 808-byte (`0x328`) heap record backs both flavours — [AsyncValue and BLAKE3 Interning](../mlir-infra/asyncvalue-and-blake3-interning.md) covers it field by field. This page is the scheduler-side companion: it names the three constructors that allocate the header, identifies which constructor owns each IR-visible flavour, and documents the `nv_tile.aws.stage` / `nv_tile.aws.order` attribute parser that threads stage and order keys back into the header for the schedule comparator `sub_8F7900` to read.

Every header in this family comes from `sub_44A8C20(0x328)` — a bump-pointer wrapper that guarantees pointer stability. The embedded `DenseMap<Operation*, T>` instances depend on that stability because they hash with `(op>>9) ^ (op>>4)` on the header address itself. The three constructors share a 14-line initialiser prologue: zero-fill the 808 bytes, point the three inline `SmallString` heads at their own inline buffers and stamp them with capacity marker `0x300000000`, point the four `SmallVector<u64,6>` heads at their own inline storage and stamp them with `0x600000000`, set the `hasValue` discriminator at byte 64, write the IR-visible name string at offset 0 through `sub_44E1740`, and prime the embedded DenseMaps' probe seed from `(op>>9) ^ (op>>4)`.

## Three Constructors

After the shared prologue, the three constructors specialise. Two flavours share the IR-visible name `"Pipe_"`; the `SmallVector` flavour at offset `+328` (scalar vs tile-shaped) and the verifier shape they accept tell them apart.

| Constructor | IR name | Bytes | Consumer payload | Notes |
|---|---|---|---|---|
| `sub_8E0070` | `Mutex_` | 3240 | single counter slot | exclusive handshake: one producer holds the slot until release |
| `sub_8E9450` | `Pipe_` | 3157 | small ring buffer (default depth = 2) | pipe-style handshake carrying scalar values |
| `sub_8EA0B0` | `Pipe_` | 3264 | tile-shaped ring buffer with a `Layout` slot | pipe-style handshake carrying tile values |

The name string at offset 0 is the IR-visible identifier; the `<N>` suffix is appended at print time from a per-function monotone counter, never stored in the header. Two `Pipe_` flavours sharing the same name string is deliberate — the binary keeps them as separate constructors instead of a single templated body because the parameter shape, the consumer-payload initialiser, and the structural verifier they call all differ. Their IR-visible spelling differs only in the trailing counter.

## Attribute Parser

`sub_8FB180` is the AWS-attribute side of construction. It walks the parent operation's attribute dictionary looking for two named integer attributes — `"nv_tile.aws.stage"` and `"nv_tile.aws.order"`, both `i32` — that name the producer's position in the software-pipelined loop. `stage` is the producer's stage index in the steady-state pipeline; `order` is its intra-stage order. Together they form the lexicographic key `(stage, order)` that the schedule comparator `sub_8F7900` later reads to decide producer-before-consumer in the final emit order.

When both attributes are present, the parser writes `stage` into the header at offset `+440` (the `statusBits0` slot named in the cross-referenced field table) and `order` at offset `+444`. Absent attributes do not fail at parse time — both offsets stay at the zero-fill default and the structural check shifts to the AWS verifier `sub_8F87A0`, which decides whether the missing keys are tolerable for this flavour or a hard failure.

## Verifiers

Three structural verifiers run after construction. They share the same diagnostic helper but each pin a different invariant; any failure marks the pass at `pass[5] |= 4` after emitting through `sub_446CE00`.

| Verifier | Invariant |
|---|---|
| `sub_8F5410` | producer and consumer types match across the handshake |
| `sub_8F80E0` | ring-buffer depth is within the hardware limit for the flavour |
| `sub_8F87A0` | AWS attributes `nv_tile.aws.stage` / `nv_tile.aws.order` are present when the flavour requires them |

The type-match verifier `sub_8F5410` is the strictest: producer and consumer view the same SSA value, so a type mismatch points to an upstream lowering bug rather than a user error. The depth verifier `sub_8F80E0` distinguishes the scalar and tile pipe flavours by reading the `SmallVector` at offset `+328`. The AWS-attribute verifier `sub_8F87A0` is the dispatch hub — it reads the `(stage, order)` pair the parser wrote and decides whether the schedule is internally consistent.

## Failure Handling

Allocation failure from `sub_44A8C20(0x328)` returns NULL into the constructor. The constructor does not check the return value before writing the prologue — that is deliberate. Tileiras treats arena allocation failure as a fatal out-of-memory condition that surfaces as a SIGSEGV on the first write, not as a recoverable error. Callers therefore see either a fully constructed header or a process abort — no observable half-constructed state to handle.

Verifier failure takes a different path. Each verifier emits a diagnostic through `sub_446CE00` and ORs `4` into the pass-level failure flags at `pass[5]`. The constructor returns the partially constructed header so the caller can attach it to the parent operation for later diagnostic printing, but downstream consumers gate on the pass-level failure bit and skip the emit phase entirely.

## Usage and Contract

The materialization pass `MaterializeSchedule` is the only caller, and it invokes the constructors after `Schedule::solve` has emitted its producer-consumer groupings. Each constructor takes the parent operation pointer (the source of the AWS attribute dictionary), the producer-side scheduling info already written by the modulo scheduler, and — for the two `Pipe_` flavours — the ring-buffer depth requested by upstream buffer assignment. The IR-visible name string, the `(stage, order)` pair at offsets `+440` / `+444`, and the consumer payload at `+328` are the public outputs that downstream verification, printing, and lowering passes read. Allocation must come from a bump-pointer arena because the embedded `DenseMap<Operation*, T>` instances hash on the header address — relocating the header after construction silently breaks every later probe.

## Reimplementation Invariants

- Allocate every header from a bump-pointer arena via `sub_44A8C20(0x328)`. The embedded DenseMaps hash on the header address; relocation after construction breaks every later probe.
- Run the 14-line self-pointer prologue before any specialisation writes. The inline-vs-heap discriminators on the `SmallString` and `SmallVector` heads rely on `data == &inline[0]`.
- Preserve the IR-visible name strings exactly: `"Mutex_"` for `sub_8E0070`, `"Pipe_"` for both `sub_8E9450` and `sub_8EA0B0`. The trailing counter is appended at print time and is not stored in the header.
- Write `nv_tile.aws.stage` into byte offset `+440` and `nv_tile.aws.order` into `+444`. The schedule comparator `sub_8F7900` reads them by absolute byte offset.
- Run the three verifiers `sub_8F5410`, `sub_8F80E0`, `sub_8F87A0` after construction and before the emit phase. Failure must OR `4` into `pass[5]`, not abort the pass.
- Treat allocation failure as fatal. Adding a NULL check in the constructor changes the surface from process abort to silent corruption and is worse, not better.

## Cross-References

[AsyncValue and BLAKE3 Interning](../mlir-infra/asyncvalue-and-blake3-interning.md) is the full field-by-field layout of the 808-byte header these constructors allocate, with the prologue body, the `Pipe::emitPayload` tail at `sub_8E7A70`, and the dual DenseMap widths. [Modulo Scheduler and Rau-Style Placement](modulo-scheduler-and-rau.md) documents the schedule that supplies the `(stage, order)` pairs the AWS-attribute parser threads into the header. [Schedule Solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) describes the materialisation boundary where the headers documented here are emitted into IR.
