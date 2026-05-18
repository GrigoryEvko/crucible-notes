# Pipe_ and Mutex_ Value-Header Layout

## Abstract

Tileiras's warp-specialized scheduler emits two families of IR-visible SSA values that name the producer/consumer handshakes flowing between agents: `Pipe_<N>` for streaming dataflow and `Mutex_<N>` for exclusive access. The same 808-byte (`0x328`) heap record backs both flavours; the canonical field-by-field layout, the three constructors that allocate it, the shared zero-fill plus self-pointer prologue, and the four-state lifecycle (ZEROED → SKELETON → CONSTRUCTED → PAYLOADED) all live in [AsyncValue and BLAKE3 Interning — AsyncValueImpl Header](../mlir-infra/asyncvalue-and-blake3-interning.md#asyncvalueimpl-header). This page is the scheduler-side companion: it covers what each family *means* to the schedule DAG, when a coordination value is born, how it transitions through arrival and wait, the `nv_tile.aws.stage` / `nv_tile.aws.order` attribute parser that threads scheduling keys back into the header, and the four upstream invariants that decide whether a handshake survives later passes.

The three constructors are anchored at three call sites in the binary: `sub_8E0070` writes the literal `"Mutex_"` into the IR name slot, and `sub_8E9450` and `sub_8EA0B0` each write `"Pipe_"` for the scalar and tile flavours respectively (the two `Pipe_` constructors stay distinct because the consumer payload initialiser and the verifier they invoke differ). The shared AWS-attribute parser is `sub_8FB180`, called from `sub_8FCD40` (the `MaterializeSchedule` entry point for `Mutex_`) and `sub_8FD260` (the `Pipe_` entry).

## Pipe_ vs Mutex_ Semantics

`Pipe_` and `Mutex_` are the two coordination primitives the warp-specialized model uses. They look similar at the storage level — same 808-byte record, same allocator, same attribute parser — but the contract they enforce is different and the scheduler ranks them at different positions in the dependence DAG.

A `Pipe_` value models a producer/consumer handshake over a ring buffer of depth `d`. The producer arrives on slot `k mod d` after writing its payload; the consumer waits on the same slot and then reads. The ring buffer allows the producer to run up to `d-1` iterations ahead of the consumer, which is what gives software pipelining its overlap. The scheduler treats a `Pipe_` as a directed edge with bounded slack: the producer's stage can precede the consumer's stage by any amount up to `d`, and that slack feeds into the dependence-MII computation in [Resource Constraint Builder](resource-constraint-builder-and-rrt.md).

A `Mutex_` value models an exclusive-access handshake on a single counter slot. The acquiring side bumps the counter, performs the protected work, and releases it; any second acquirer must wait until the release before entering. The scheduler treats a `Mutex_` as a serialisation edge with no slack: the protected work in iteration `i` must complete before the protected work in iteration `i+1` can start. That zero-slack semantics is what makes named-barrier allocation in [Buffer Assignment and Named Barriers](buffer-assignment-and-mbarriers.md) the central handshake mechanism — every `Mutex_` consumes exactly one slot from the 32-slot pool and holds it for the full live range.

| Property | `Pipe_` | `Mutex_` |
|---|---|---|
| Payload | producer-supplied scalar or tile | none — synchronisation only |
| Slack across iterations | up to `depth - 1` | none |
| Hardware backing | ring buffer in SMEM or TMEM, optionally with mbarrier object | named barrier slot from the per-CTA 32-slot pool |
| Buffer-assignment cost | `depth` × per-slot SMEM/TMEM footprint | 1 named barrier slot |
| Schedule-edge semantics | partial order with bounded skew | strict serialisation |
| Constructor address | `sub_8E9450` (scalar) / `sub_8EA0B0` (tile) | `sub_8E0070` |

## Lifecycle

Each coordination value passes through three observable phases. The scheduler emits a value into the IR during materialisation; the executing program then arrives on it from the producer side and waits on it from the consumer side. Identity is stable across all three phases.

```c
// Phase 1: construction — emitted once during MaterializeSchedule.
auto value = construct_pipe_or_mutex(producer, consumer, stage, order, depth);

// Phase 2: arrival — producer signals payload-ready or work-complete.
for (int iter = 0; iter < N; ++iter) {
    write_payload(value, iter % value.depth);    // Pipe_ only
    arrive(value, iter % value.depth);
}

// Phase 3: wait — consumer blocks until the matching arrival.
for (int iter = 0; iter < N; ++iter) {
    wait(value, iter % value.depth);
    use_payload(value, iter % value.depth);      // Pipe_ only
}
```

The scheduler's job is to choose the producer and consumer stages so that the wait in phase 3 is satisfied by an earlier arrival in phase 2 — never a future one. The `(stage, order)` pair attached to the header at construction time is what makes that scheduling decision durable through every subsequent pass.

## Three Constructors

Three constructors share the zero-fill plus self-pointer prologue and then specialise: a `Mutex_` flavour at `sub_8E0070` backed by one named-barrier slot, a scalar `Pipe_` flavour at `sub_8E9450` backed by a small ring of scalar values (default depth 2), and a tile-shaped `Pipe_` flavour at `sub_8EA0B0` backed by a ring buffer with an attached `Layout` slot for tile traffic. All three end up routing the IR-visible name through the same SSO short-string append helper (`sub_44E1740`) with literal length 6 for `"Mutex_"` and 5 for `"Pipe_"`; both `Pipe_` flavours print under the same name `Pipe_<N>` because the trailing `<N>` is a per-function monotone counter appended at print time, not a stored field. The schedule comparator never needs to disambiguate them because the payload value's type already encodes the scalar-versus-tile split. The two `Pipe_` flavours stay as separate constructors rather than one templated body because the parameter shape, the consumer-payload initialiser, and the verifier they each invoke differ.

The header itself comes from a bump-pointer arena, which guarantees pointer stability. The embedded `DenseMap<Operation*, T>` instances rely on that stability because they hash on the header address itself; relocating a header after construction silently breaks every later probe. The per-constructor field initialisation, the `kind` byte that distinguishes the three at runtime, the optional-flag pair that drives the verifier dispatch, and the shared tail that promotes a header from CONSTRUCTED to PAYLOADED are all spelled out in [AsyncValue and BLAKE3 Interning — Construction Prologue](../mlir-infra/asyncvalue-and-blake3-interning.md#construction-prologue).

## Attribute Parser

The AWS-attribute parser is the single function `sub_8FB180`. It walks the parent operation's attribute dictionary looking for two named integer attributes — `nv_tile.aws.order` (queried first) and `nv_tile.aws.stage` — both expected to point at the canonical `i32` `TypeID` marker at `unk_5BE5F40`. `stage` is the producer's stage index in the steady-state pipeline; `order` is its intra-stage order. Together they form the lexicographic key `(stage, order)` that the schedule comparator reads to decide producer-before-consumer in the final emit order.

The parser uses a two-step lookup that reflects how MLIR stores attributes. It first checks a one-byte flag at offset `47` of the parent op — set when the op carries the inline-attribute fast path — and on a hit calls `sub_446DC50` to read the inline slot; only on a miss does it fall back to the full dictionary scan via `sub_440E370(op + 56, "nv_tile.aws.order", 17)`. The literal length `17` is the strlen of `nv_tile.aws.order` and `nv_tile.aws.stage` (both are exactly 17 bytes), which is why one length argument serves both probes. Any attribute that resolves to a type pointer different from `unk_5BE5F40` is rejected as a typed-attribute mismatch — the parser nulls the slot rather than reading the wrong width.

When both attributes are present and type-checked, the parser writes them into the header's status slot. Absent attributes do not fail at parse time — the slot stays at the zero-fill default and the structural check shifts to the AWS verifier, which decides whether the missing keys are tolerable for this flavour or a hard failure. Mutex flavours require both keys; pipe flavours can tolerate one absent key when the matching schedule slot is also absent.

## Verifiers

Three structural verifiers run after construction. Each pins a different invariant; any failure sets the pass-level failure flag so downstream emit-phase consumers skip the corrupt header.

| Verifier | Invariant | Scheduler consequence on failure |
|---|---|---|
| Type-match | producer and consumer view the same SSA value type across the handshake | upstream lowering bug; the schedule never had a coherent producer-consumer pair |
| Depth | the ring-buffer depth is within the hardware limit for the flavour | buffer-assignment over-allocated; the merged pipeline class is wider than the hardware can ring |
| AWS-attribute | `nv_tile.aws.stage` / `nv_tile.aws.order` are present when the flavour requires them | `Schedule::solve` failed to write its result; the schedule comparator has no key to sort by |

The type-match verifier is the strictest of the three: producer and consumer view the *same* SSA value, so a type mismatch points to an upstream lowering bug rather than a user error. The AWS-attribute verifier is the dispatch hub: it reads the `(stage, order)` pair the parser wrote and decides whether the schedule is internally consistent.

## Schedule-Resource Invariants

Four invariants tie this header back to the scheduler analysis and must hold for a `MaterializeSchedule` output to be coherent.

1. **`(stage, order)` matches the analysis.** The pair written into the status slot must be exactly the pair `ScheduleAnalysis` recorded for the producer op. The schedule comparator reads only this slot at emission time; any drift between analysis and header is invisible until late-pass verification fails.
2. **Pipe_ depth fits the buffer-assignment record.** The ring-buffer depth on a `Pipe_` is bounded by the buffer-assignment record's allocated slot count. Phase 4 of buffer assignment may have merged pipelines so two pipes share one physical buffer; the depth on the surviving header must reflect the merged class, not the pre-merge values. The merge path emits `"unable to merge two different pipelines: "` if the survivors disagree on depth or `(stage, order)`.
3. **Mutex_ named-barrier index is in `[0, 31]`.** The named barrier index written into the `Mutex_` header is one of the slots Phase 2 allocated from the 32-slot pool. Indices outside that range mean a buffer-assignment bug, never a frontend error.
4. **Mbarrier shape matches the producer-tail flavour.** A `Pipe_` whose `producer_tail` lowers to `cutlass.pipeline.producer_tail` must use a non-transactional mbarrier; the transactional flavour is gated by `sub_145AFD0`, which emits `"using transaction mbarrier is not supported"`. A `Pipe_` whose producer carries a non-empty payload must not be backed by a named barrier alone — the named-barrier path emits `"the producer_tail implemented with named barrier does not yet support user payload"` because there is nowhere to stage the payload byte count.

A reimplementation must preserve all four. Violating (1) corrupts the per-op stage/order map; violating (2) emits a ring buffer that overflows under steady-state pressure; violating (3) emits a `bar.sync` against a slot the hardware does not have; violating (4) emits an arrive/wait pair that the runtime accepts but that drops the payload silently.

## Failure Handling

The scheduler sees one of two outcomes per coordination value: a fully constructed header attached to the parent operation, or a verifier-set failure flag that gates the emit phase. The constructor side of failure handling (allocation aborting on OOM, the partially constructed header that the verifier still returns) is layout-mechanics — see the canonical page. The schedule side is the gating: a single malformed handshake sets the pass-level failure bit and skips the emit phase entirely, so a corrupt header never feeds into later passes that walk the schedule DAG. This split keeps the diagnostic stream useful: one focused error per malformed handshake, no cascade.

The failure diagnostics partition into three layers, each emitted by a different function. The constructor layer signals allocation failure through the standard arena OOM path. The attribute-parser layer is silent — a missing or type-mismatched attribute writes zero into the status slot and the verifier reports later. The downstream-pass layer emits the user-visible strings: buffer-assignment Phase 2 emits `"fails to assign named barrier"` from `sub_13692E0` when it runs out of 32-slot pool entries; the pipeline-merge pass (`sub_135CD10`) emits `"mbarrier has wait-like users, cannot share pipeline buffer."` when a `Pipe_` candidate for sharing already has consumer-side mbarrier waits in flight, and `"unable to merge two different pipelines: "` when two merge candidates carry mismatched headers.

## Usage and Contract

`MaterializeSchedule` is the only caller and invokes the three constructors after `Schedule::solve` has emitted its producer-consumer groupings. Each constructor takes the parent operation pointer (the source of the AWS attribute dictionary), the producer-side scheduling info already written by the modulo scheduler, and — for the two `Pipe_` flavours — the ring-buffer depth requested by upstream buffer assignment. The IR-visible name string, the `(stage, order)` pair written by the AWS-attribute parser, and the consumer payload are the public outputs downstream verification, printing, and lowering passes read.

Three things have to happen at materialisation time for a coordination value to be coherent with the rest of the schedule. First, every `Pipe_` value emitted into the IR has to outlive the producer's last arrival and the consumer's last wait; the arena lifetime gives this for free, but a pass that drops a `Pipe_` value before materialisation finishes leaves dangling references in the schedule's per-op stage map. Second, the value's `(stage, order)` pair has to be set before any later pass walks the IR — the AWS-attribute parser is responsible for this, and the verifier emits a hard diagnostic if it finds a `Mutex_` value with an unset pair. Third, the named-barrier index inside a `Mutex_` header has to come from `BufferAssignment`'s 32-slot allocation table; any other source produces a `bar.sync` against a slot the hardware does not have.

## QUIRKs

**The literal length 17 covers both attribute keys exactly.** `sub_8FB180` passes `0x11u` as the length argument to `sub_440E370` for both the `nv_tile.aws.order` and `nv_tile.aws.stage` lookups. The two attribute names happen to be the same length to the byte (17 ASCII characters each), so the parser hard-codes one literal length and reuses it across both probes. Renaming either attribute in a reimplementation requires changing the corresponding length constant; a rename that drifts the two lengths apart breaks the second probe silently because `sub_440E370` does a strict bounded compare.

**The `i32` type guard rejects `i64` stage indices outright.** Both attribute probes compare the returned attribute's type pointer against the single global `unk_5BE5F40` and null the slot on mismatch — `i64` or `index`-typed values for `stage`/`order` are dropped before they ever reach the header. Combined with the fact that absent attributes also produce a zero slot, the verifier cannot distinguish "attribute was wrong type" from "attribute was missing"; both surface as the same AWS-verifier failure, and the diagnostic loses fidelity.

**Named-barrier `Pipe_` and user payloads are mutually exclusive.** When buffer-assignment chooses the named-barrier backing for a `Pipe_` (the cheap path — one slot instead of an mbarrier object), the materializer refuses any payload-carrying producer. The diagnostic `"the producer_tail implemented with named barrier does not yet support user payload"` is the only signal; a reimplementation that silently coerces payload-carrying pipes into the named-barrier path will compile but lose every arrived payload because `bar.sync` has no transaction byte count to track.

## Cross-References

[AsyncValue and BLAKE3 Interning](../mlir-infra/asyncvalue-and-blake3-interning.md) is the field-by-field layout of the 808-byte header these constructors allocate, including the prologue, the three constructor specialisations, the lifecycle state machine, and the failure-handling path. [Modulo Scheduler and Rau-Style Placement](modulo-scheduler-and-rau.md) documents the schedule that supplies the `(stage, order)` pairs the AWS-attribute parser threads into the header. [Schedule Solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) describes the materialisation boundary where these headers are emitted into IR. [Buffer Assignment and Named Barriers](buffer-assignment-and-mbarriers.md) supplies the ring-buffer depth and the named-barrier index that constrain the schedule-resource invariants above, and is where the pipeline-merge pass `sub_135CD10` decides whether two `Pipe_` headers can share one physical buffer.
