# BIR Simulator: The Interactive DAP Debugger

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, `neuronxcc/starfish/lib/libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`, cp310 wheel; the cp311/cp312 layout is identical). For `.text`/`.rodata`, virtual address equals file offset; `.data` / `.data.rel.ro` carry a small VMA→file-offset delta — subtract it before `xxd`'ing a `.data.rel.ro`-resident vtable/typeinfo. Two address frames coexist in the corpus: the function-body frame used by the cross-symbol disassembly (e.g. `Controller::cont @0x86e610`) and the per-symbol IDA-sidecar frame (`@0x5efae0` for the same function). Both are cited where they differ; treat every address as version-pinned.*

## Abstract

The BIR simulator ships with a full **interactive source-level debugger** that speaks Microsoft's **Debug Adapter Protocol (DAP)** — the same JSON-over-`Content-Length` wire that VS Code, nvim-dap, and every other DAP client drive. A developer can set a breakpoint on a line of BIR text, launch the functional simulator ([§7.34 — Dispatch & Whole-Machine State](sim-dispatch-state.md)), step over a `Matmult`, and inspect a `MemoryObject` partition or a scalar register *as a tree of variables in an IDE*. The unusual part is the target: DAP normally drives a CPU process or a language VM; here it drives a **tensor-machine interpreter** running on a worker thread, where a "stack frame" is a BIR call frame, a "thread" is a Neuron core (LNC), and a "variable" is a slice of `SBUF`/`PSUM`/`DRAM` rendered through an access-pattern expression.

The implementation is split across two shared objects, and the split is the first thing a reimplementer must internalize. The **simulation engine** is `libBIRSimulator.so` (plain `birsim::` namespace — `InstVisitor`, `Memory`, `RegState`); it defines a pure hook interface, `birsim::DebuggerCallbacks`, that its per-instruction loop calls on every step. The **debugger and the entire DAP stack** live in `libwalrus.so` under `birsim::debugger::`, statically linked against Google's **cppdap** library (`dap::`). The walrus-side `Controller` *implements* `DebuggerCallbacks`; that inheritance edge is how the host-side debugger reaches into the engine's step loop. The two halves rendezvous through `neuronxcc::backend::BirSim`, the driver object that owns the engine and is handed the `bir::Module` vector to simulate.

This page documents four things at reimplementation depth: (1) the **DAP message loop** — `Content-Length` framing, JSON (de)serialization through cppdap's reflection machinery, and `seq`/`request_seq` correlation; (2) the **handler table** — exactly 17 request handlers and the four events the debugger emits, plus the deliberately minimal two-flag capability advertisement; (3) the **breakpoint/step machinery** — how a source line maps to a `bir::Instruction*`, how the engine thread blocks on a condition variable at a stop point, and how the `StepType` policy decides when to re-stop; and (4) the **debugger class hierarchy** — `Adapter` / `Debugger` / `Controller` / `DapCallbacks` / `DapSink` and the two-family `Expr` / `ExprValue` inspection AST that backs `evaluate`/`variables`.

For reimplementation, the contract is:

- The **host/engine split**: `Controller : public birsim::DebuggerCallbacks`; the four hook vtable slots the engine calls; `BirSim::setDebuggerCallbacks` as the install point.
- The **DAP transport**: `Content-Length:<N>\r\n\r\n<json>` framing, nlohmann-backed `dap::Serializer`/`Deserializer`, the per-type `TypeOf<T>::serializeFields` reflection bodies, and the `seq`/`request_seq` envelope.
- The **17-handler dispatch** keyed on the `command` string (via cppdap's typed `registerHandler<RequestT>`), the **two** advertised capabilities, and the session lifecycle from `initialize` to `disconnect`.
- The **pause mechanism**: sim on its own `std::thread`; `onInstPtrSet` → `shouldStop(stack)` → mutex + `condition_variable::wait()`; the DAP thread releases it via a continue/step command.
- The **inspection AST**: `Expr` (parsed) → `eval()` → `ExprValue` (rendered), reading live `RegState`/`Memory` through an `InstVisitor*` bound per frame by `ExprContext::setFrameInfo`.

| | |
|---|---|
| **Debugger + DAP binary** | `libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`); `birsim::debugger::` = 287 dynsyms / 415 decompiled funcs |
| **Engine binary** | `libBIRSimulator.so` — plain `birsim::` (`InstVisitor`/`Memory`, 418 syms); **zero** `dap::`/`debugger` symbols |
| **DAP library** | cppdap (`dap::`, 1204 dynsyms) statically linked into `libwalrus.so`; JSON backend = nlohmann::json `v3.11.3` |
| **DAP type registry** | 201 `dap::BasicTypeInfo<dap::X>` registrations (45 req + 46 resp + 18 evt + 34 struct + 58 container) |
| **Session host** | `birsim::debugger::Adapter` — ctor `@0x861cc0`, `run @0x85a6b0`, `bindio @0x85b020`(stdio)/`@0x85ce30`(path) |
| **Request handlers** | 17 total — `registerSignalHandlers @0x85a700` (Init/Disconnect) + `registerDebugger @0x85a860` (15 debug) |
| **Engine hook** | `birsim::DebuggerCallbacks` (defined in `libBIRSimulator.so`); `Controller` is its walrus-side implementation |
| **Advertised caps** | exactly two: `supportsConfigurationDoneRequest` + `supportsStepInTargetsRequest` |

---

## 1. The host/engine split — DebuggerCallbacks is the seam

The debugger does not live where the brief's title suggests. **[CONFIRMED]** — `nm -CD libBIRSimulator.so | rg birsim::debugger` returns **zero** class typeinfos; all 287 `birsim::debugger::` dynsyms, all 38 debugger typeinfos, and the entire cppdap integration are in `libwalrus.so`. This is the correct factoring: `libBIRSimulator.so` is the pure per-opcode kernel engine ([§7.34](sim-dispatch-state.md)); `libwalrus.so` is the driver/host that *owns and drives* the engine. The debugger is a host-side concern.

The two halves are stitched together by one interface defined on the **engine** side:

```c
// birsim::DebuggerCallbacks — the hook interface, DEFINED in libBIRSimulator.so.
// The BIR sim loop (simulateWithSyncs / getNextInstruction, §7.34) calls these
// every step. shouldPause has a weak default body in the engine; the other three
// are pure virtual (no default symbol) → an implementation is mandatory.
struct DebuggerCallbacks {                          // abstract; 4 hooks + dtors
    virtual void onInstPtrSet(bir::Instruction*, unsigned core) = 0; // [vt 0]
    virtual void onModuleExit(unsigned core)                    = 0; // [vt 1]
    virtual bool skipInstruction(bir::Instruction*)             = 0; // [vt 2]
    virtual bool shouldPause(unsigned core);   // [vt 3] weak default @0x213100 (engine)
    virtual ~DebuggerCallbacks();
};
```

The engine exposes one install point and the state accessors the debugger reads:

```text
neuronxcc::backend::BirSim::setDebuggerCallbacks(birsim::DebuggerCallbacks*)  @0x1109430 (libwalrus)
birsim::InstVisitor::getRegState()                 @0x1aa740 (libBIRSimulator) → §7.34 RegState
birsim::InstVisitor::getMemory() / Memory          (libBIRSimulator)          → §7.34 Memory
```

The walrus-side `Controller` *is* the `DebuggerCallbacks` implementation. **[CONFIRMED]** — the `Controller` typeinfo is `si` (single-inheritance) onto `birsim::DebuggerCallbacks`, and `Controller::start` calls `BirSim::setDebuggerCallbacks(this)` before spawning the worker thread. So when the engine's step loop fires `onInstPtrSet`, it is calling straight into the debugger.

> **QUIRK — DAP over a tensor interpreter.** Every familiar DAP noun is remapped to a simulator concept. A *thread* is a Neuron core (`ThreadsRequest` → `getNumCores`); a *stack frame* is a BIR call frame (`SmallVector<bir::Instruction*,2>`, ≤2 inline); a *variable* is a slice of device memory rendered through an access-pattern expression; a *breakpoint* is a `bir::Instruction*`. The wire is 100% standard DAP, so an off-the-shelf IDE drives it unmodified — but what it drives is a numerical reference interpreter, not a CPU.

---

## 2. The DAP wire — Content-Length framing and cppdap reflection

### 2.1 Transport

Each DAP message on the stream is the standard JSON-RPC-over-`Content-Length` frame:

```text
Content-Length: <N>\r\n
\r\n
<N bytes of UTF-8 JSON>
```

The header literal is read by cppdap's `ContentReader`. **[CONFIRMED]** — `dap::ContentReader::read[abi:cxx11]() @0x1816a40` opens with a `match("Content-Length:")` then a `scan("Content-Length:")` over the byte stream; the write side is `dap::ContentWriter`. The duplex pair is `dap::ReaderWriter::create(Reader, Writer)`, bound to a transport by the `Adapter`:

```text
birsim::debugger::Adapter::bindio()                         @0x85b020   — stdio (default)
birsim::debugger::Adapter::bindio(filesystem::path const&)  @0x85ce30   — file / pipe
dap::spy(Reader, Writer, char const*)                                    — optional tee to a log
```

### 2.2 JSON codec and field reflection

The JSON body is (de)serialized by cppdap's reflection layer on top of nlohmann::json `v3.11.3` (`json_abi_v3_11_3` symbols are pervasive). Every protocol type `T` is registered exactly once as a Meyers-singleton `dap::BasicTypeInfo<T>` whose vtable carries `name`/`size`/`alignment`/`construct`/`copyConstruct`/`destruct`/`serialize`/`deserialize`; `serialize`/`deserialize` forward to generated per-type field bodies:

```c
// dap::TypeOf<T>::serializeFields(FieldSerializer*, const void* obj)
//   — the cppdap DAP_FIELD reflection expanded by macro: one field(name, &member)
//   call per member, in declaration order. Example (LaunchRequestEx @0x878fe0):
strcpy(buf, "stopOnStart");                              // the JSON key, SSO-packed
field("stopOnStart", TypeOf<optional<boolean>>::type()); // bound to the member type
// optional<T> wire layout: { T value@+0; bool set@+1; } — if !set, the field is
// OMITTED from the emitted JSON object (present-or-absent semantics).
```

There are **201** such `BasicTypeInfo<dap::X>` registrations. **[CONFIRMED — typeinfo enumeration]** — this is the canonical count (the "213" cited in some earlier notes over-counts a dozen `TypeOf`-only primitive/`optional<>` singletons that drift with the toolchain). The breakdown:

| Group | Count | What |
|-------|-------|------|
| Requests | 45 | every standard DAP `*Request` body |
| Responses | 46 | every `*Response` + `ErrorResponse` (the +1, the generic failure envelope) |
| Events | 18 | every `*Event` notification body |
| Named structs / scalars | 34 | `Source`, `StackFrame`, `Scope`, `Variable`, `Breakpoint`, `Capabilities`, … + `any`/`boolean`/`integer`/`number` + `LaunchRequestEx` |
| Container TypeInfos | 58 | `optional<…>` / `vector<…>` / `variant<…>` / `map<…>` per-container descriptors (not protocol messages) |

cppdap statically registers the **entire** DAP spec surface even though the debugger handles a small subset; the unused 184 types are linked but never reach a handler. See [§7.41-sibling D-R08](sim-dispatch-state.md) for the full roster.

### 2.3 Envelope correlation

The JSON-RPC envelope (`seq`/`type`/`command`/`event`/`request_seq`/`success`/`body`/`arguments`) is driven by cppdap's `Session::Impl`:

```text
Impl::processRequest(NlohmannDeserializer*, dap::integer seq)  — decode a request; seq = correlation id
Impl::processResponse(const Deserializer*)                     — match request_seq → pending callback
Impl::processEvent(const Deserializer*)                        — dispatch an "event" message
Impl::send(TypeInfo* req, TypeInfo* resp, void*, callback)     — emit request + register response cb
Impl::send(TypeInfo*, void*)                                   — emit an event / response
```

**[STRONG]** — these `(anon)::Impl::*` names come from the cppdap closure typeinfo strings; the envelope keys are cppdap-standard. The dispatch on the inbound `command` string is performed inside cppdap's `Session` (a typed handler table keyed by `TypeOf<RequestT>::type()`), not by a hand-rolled string switch in the debugger — the debugger registers *typed* handlers and cppdap routes the `command` field to them.

---

## 3. The handler table — 17 requests, 4 events, 2 capabilities

The `Adapter` registers handlers in two phases. Each registration binds a `dap::TypeOf<RequestT>::type()` singleton to a thunk via a `registerSafeHandler<RequestT>` template that wraps the body in an exception guard — a throw becomes a `dap::ErrorResponse` rather than a crashed session.

### 3.1 Lifecycle handlers — `registerSignalHandlers @0x85a700`

```text
InitializeRequest   → sub_85C7A0   (builds Capabilities, §3.3)
InitializeResponse  → "sent" cb sub_861CB0 (Session slot +72) → emits InitializedEvent after flush
DisconnectRequest   → sub_85C6D0   (Controller::kill + release Adapter::run)
```

### 3.2 Debug handlers — `registerDebugger @0x85a860` (15, in registration order)

**[CONFIRMED — verbatim from the decompiled body]**: each row is a `TypeOf<Req>::type()` paired with its thunk via `_mm_insert_epi64(closure, thunk, 1)`, in exactly this order.

| # | Request (`command`) | Thunk | Debugger / Controller method |
|---|---|---|---|
| 1 | `HoverRequest`* | `sub_861DD0` | `Debugger::getHover(addr,col)` / `(path,line)` |
| 2 | `SetBreakpointsRequest` | `sub_85F660` | `Debugger::BreakpointEditor::setBreakpoints` |
| 3 | `ConfigurationDoneRequest` | `sub_85B120` | `Controller::configureLaunch` → `Debugger::start` |
| 4 | `LaunchRequestEx`* | `sub_85BFE0` | start session (custom launch, §5) |
| 5 | `ThreadsRequest` | `sub_85C210` | cores-as-threads → `getNumCores` |
| 6 | `StackTraceRequest` | `sub_864EE0` | `Debugger::getStack` → `vector<StackFrame>` |
| 7 | `NextRequest` | `sub_85BE70` | `Controller::stepOver` |
| 8 | `StepInTargetsRequest` | `sub_85F1D0` | `Debugger::getStepInTargets` |
| 9 | `StepInRequest` | `sub_85BD00` | `Controller::stepIn` |
| 10 | `StepOutRequest` | `sub_85BB90` | `Controller::stepOut` |
| 11 | `ContinueRequest` | `sub_85C0A0` | `Controller::cont` |
| 12 | `PauseRequest` | `sub_85B1C0` | `Controller::pause` |
| 13 | `ScopesRequest` | `sub_860530` | variable scopes (`ExprContext`) |
| 14 | `EvaluateRequest` | `sub_85BB80` | `ExprParser::parse` → `Expr::eval` → `ExprValue` (§6) |
| 15 | `VariablesRequest` | `sub_85EB20` | `ExprValue::getChildren` tree (§6) |

`*` = extensions over base DAP. `HoverRequest` is a cppdap/vendor request (standard DAP folds hover into `EvaluateRequest{context:"hover"}`); `LaunchRequestEx` is Neuron's custom launch body (§5). With the two lifecycle requests, the debugger answers **17** of the 45 registered request types; the other 28 (Disassemble, Read/WriteMemory, SetVariable, conditional/data/function/instruction breakpoints, StepBack/ReverseContinue, Modules, Restart, …) have **no** handler and are answered by cppdap's default error path — fully consistent with the minimal capability set in §3.3.

### 3.3 The two-flag capability advertisement

The `initialize` handler default-constructs a `dap::InitializeResponse` (whose `body` is a `dap::Capabilities`) and forces exactly **two** `optional<bool>` capabilities to `{value=1, set=1}`. **[CONFIRMED — decompile of `sub_85C7A0`]**: only two source `0x101` half-word writes (`v45=257`, `v88=257`) and their two present-flag copies into the response body (`v129=1`, `v173=1`) appear in the whole handler; every other capability is left `set=0` and is therefore **omitted** from the JSON, i.e. treated as `false` by the client.

```c
// sub_85C7A0 — the Initialize handler, capability population (verbatim shape):
dap::InitializeResponse resp;                 // body = default Capabilities (all unset)
v45 = 257; /* 0x101 = {value:1, set:1} */     // source optional<bool> #1
v88 = 257; /* 0x101 */                         // source optional<bool> #2
... copy v45 → resp.body offset 178 (supportsConfigurationDoneRequest.value);
    resp.body[179] = 1;                        //  ... and its present flag (off+1)
... copy v88 → resp.body offset 222 (supportsStepInTargetsRequest.value);
    resp.body[223] = 1;                        //  ... and its present flag
send(resp);                                    // → InitializeResponse, then InitializedEvent
```

The two byte offsets (178, 222) index the `Capabilities` struct exactly at `supportsConfigurationDoneRequest` and `supportsStepInTargetsRequest` per the field-order table in `TypeOf<Capabilities>::serializeFields @0x17fb750`. **[CONFIRMED]**

> **GOTCHA — advertise only what the client must gate on.** The debugger handles 15 debug requests but advertises only 2 capabilities. The reason: `configurationDone` and `stepInTargets` are precisely the two whose *client-side use* is gated on a capability flag. Continue/Next/StepIn/StepOut/Pause/StackTrace/Scopes/Variables/Evaluate/Threads/SetBreakpoints are **base DAP** — no advertisement needed, they just work. `Hover` is served even though `supportsEvaluateForHovers` is **not** set, because it is routed as the cppdap-extension `HoverRequest` independently of that flag. A reimplementer who advertises Disassemble/ReadMemory/EvaluateForHovers to "match the handlers" would mislead the client into issuing requests with no handler.

### 3.4 Events emitted

Only **four** of the 18 registered events are ever sent:

| Event | JSON `event` | Source | When |
|---|---|---|---|
| `StoppedEvent` | `stopped` | `DapCallbacks::stop` | engine hit a breakpoint / finished a step / paused |
| `TerminatedEvent` | `terminated` | `DapCallbacks::terminate` | simulation ended |
| `OutputEvent` | `output` | `DapSink::consume` + `Callbacks::output` | log records + program `DevicePrint` → debug console |
| `InitializedEvent` | `initialized` | `InitializeResponse` "sent" callback | after the initialize response is flushed |

`StoppedEvent.reason` is the `birsim::debugger::to_string(StopReason)` literal. **[CONFIRMED — `to_string @0x873c10`]** returns `"step"` / `"pause"` / `"breakpoint"`; any out-of-range value falls to a `"Please open a support ticket…"` assert, proving the enum has **exactly three** values: `step=0`, `pause=1`, `breakpoint=2`. These are exactly the DAP-standard `StoppedEvent.reason` strings.

---

## 4. Breakpoints and stepping — how the engine halts

### 4.1 Threading model

The simulation runs on **its own** `std::thread`; the DAP `Session` runs on the `Adapter` thread. Two `birsim::debugger::Signal` objects (each a `std::mutex` + `std::condition_variable`) plus the `Controller`'s own mutex/cv form the cross-thread handshake.

```text
Adapter thread (DAP I/O)                 Engine thread (sim worker)
------------------------                 --------------------------
Adapter::run @0x85a6b0                   Controller::start spawns it (BirSim::setDebuggerCallbacks)
  Signal::fire(@+56)  ── arms session →  simulate bir::Module vector
  Signal::wait(@+152) ── parks ────┐       per inst: DebuggerCallbacks::onInstPtrSet(inst, core)
                                   │           → getStack(core) → shouldStop(stack)
  Continue/Next/StepIn/… handler   │           → if stop: lock; (stop callback → StoppedEvent);
    → Controller::cont/stepOver/…  │                       condition_variable::wait()  ◄── BLOCKS
    → notify_all ──────────────────┘                       (released by the cont/step notify)
  Disconnect → Controller::kill → Signal::fire(@+152)
```

### 4.2 The pause point — `Controller::onInstPtrSet @0x871b70`

This is the central halt mechanism. The engine calls it for every instruction; it decides whether to stop and, if so, blocks the engine thread on a condition variable. **[CONFIRMED — decompile]**:

```c
// Controller::onInstPtrSet(bir::Instruction* inst, unsigned core)  — engine-thread hook
void onInstPtrSet(bir::Instruction* inst, unsigned core) {
    unsigned it = inst->type - 68;                         // (engine inst-type bias)
    if (this->pendingStepTarget == inst)                   // clear a satisfied step-target
        this->pendingStepTarget = nullptr;
    Stack = Controller::getStack(core);                    // SmallVector<bir::Instruction*,2>
    StepType* policy = /* active StepType for `core` */;
    bool stop = Controller::shouldStop(policy, inst, core);// dispatches StepType::shouldStop(stack)
    if (stop) {
        pthread_mutex_lock((char*)this + 352);             // the Controller mutex @+352
        (*(vtable_slot_59)((char*)this + 448, …));         // → DapCallbacks::stop(core, reason)
                                                           //    → dap::StoppedEvent on the session
        std::condition_variable::wait();                   // PARK engine thread until cont/step
        pthread_mutex_unlock(...);
    }
}
```

`shouldStop` is **not** a vtable slot — it is an internal helper that dispatches to the *active* `StepType` policy object's virtual `shouldStop(SmallVector<bir::Instruction*,2> const&)`. The `StoppedEvent` is sent through the `Callbacks` pointer at `this+448` (vtable slot index 59 in the decompile), which is the installed `DapCallbacks`. The mutex at `Controller+352` and cv pairing match the `Controller` object map (mutex@+352, condvar@+392, running@+440).

### 4.3 The `StepType` policy family

Stepping is implemented as a small **policy hierarchy**: a DAP step command installs a `StepType` object on the core, then releases the engine thread; on each subsequent `onInstPtrSet` the policy's `shouldStop(stack)` decides when to re-stop, purely by inspecting the call-stack depth.

```c
// StepType — abstract; the ONLY virtual is shouldStop (vt slot 0).
struct StepType {
    virtual bool shouldStop(SmallVector<bir::Instruction*,2> const& stack) const = 0;
    virtual ~StepType();
};
struct Cont     : StepType {};                          // never stop (run to breakpoint)
struct StepOver : StepType { StepOver(unsigned core); };// stop when stack depth ≤ entry depth
struct StepIn   : StepType { StepIn(unsigned core, bir::Function* target); }; // stop on target entry
struct StepOut  : StepType { StepOut(unsigned core); }; // stop when stack depth < entry depth
```

**[CONFIRMED]** — only `StepIn` carries a `bir::Function*` (the call target to descend into); `StepOver`/`StepOut`/`Cont` take only the core id. The step decision is therefore a stack-depth comparison over the inline-2 `SmallVector` — exactly enough state for over/in/out.

### 4.4 Two breakpoint editors — source line ↔ instruction

Breakpoints exist at two granularities, and a `SetBreakpoints` request flows through both:

```text
Debugger::BreakpointEditor   (HIGH level — source/module granular)
  setBreakpoints(path, vector<line>)  /  setBreakpoint(path, line) @0x883900
      ↓ resolves source line → bir::Instruction* via the line map (§5.2)
Controller::BreakpointEditor (LOW level — instruction granular)
  setBreakpoint(bir::Instruction*) @0x870090  /  delBreakpoint(bir::Instruction*) @0x86e7c0
```

The DAP `SetBreakpointsRequest` handler (`sub_85F660`) takes the client's `{path, lines[]}`, asks `Debugger::BreakpointEditor` to map each line to the instruction that *starts* that BIR statement, and installs an instruction-level breakpoint via `Controller::BreakpointEditor`. At run time, a breakpoint is just an instruction-pointer match consulted inside the `shouldStop` path; a hit produces `StopReason::breakpoint`.

---

## 5. The class hierarchy and the session lifecycle

### 5.1 Object graph

```text
Adapter            @ctor 0x861cc0   owns dap::Session @this+248, a Logger, two Signals
  │                                 (@+56 / @+152, each mutex+condvar), ExprContext @+264
  ├─ run @0x85a6b0  bindio @0x85b020/@0x85ce30  registerSignalHandlers @0x85a700
  │                 registerDebugger @0x85a860  setDebugger @0x85e250
  ├─ DapCallbacks : birsim::debugger::Callbacks   @ctor 0x85b000  (takes dap::Session&)
  │     stop(core, StopReason) @0x85a110 → StoppedEvent     [vt 0]
  │     terminate()            @0x85ddb0 → TerminatedEvent  [vt 1]
  │     output(string)         @0x8650c0 (inherited)        [vt 2]
  └─ DapSink   : logging::CustomSink (boost.log)  @ctor 0x874980  (takes dap::Session&)
        consume(record_view, string) @0x8749a0 → OutputEvent;  flush @0x874970

Debugger           @ctor (templated) 0x88b6c0 / 0x88bb10   holds unique_ptr<Controller>,
  │                vector<bir::Module*>, neuronxcc::backend::BirSim&
  ├─ createFromFiles(modules, BirSim&, sources) @0x885180   — LIVE debugging
  ├─ createFromDump(...)                        @0x886fa0   — POST-MORTEM replay
  ├─ getStack / getStepInTargets / getHover / updateContext / generateLineMap
  └─ start() @0x881510 → Controller::start

Controller : public birsim::DebuggerCallbacks   @ctor 0x86e0b0   (the engine-thread driver)
      onInstPtrSet/onModuleExit/skipInstruction/shouldPause   ← the 4 hook vtable slots
      cont / pause / kill / join / stepIn / stepOut / stepOver / start  ← non-virtual DAP surface
      mutex @+352, condvar @+392, kill-flag @+64, running @+440, unique_ptr<Callbacks>
```

`DapCallbacks` is the concrete `Callbacks` the `Controller` notifies; it translates engine events into cppdap events. `DapSink` is a separate boost.log sink that plugs the compiler's logging stream into the debug console (each log record → `OutputEvent`) — complementing `Callbacks::output`, which carries program-level `DevicePrint`/`print` output.

### 5.2 Source/line model

`Debugger::generateLineMap(istream&, bir::Module const&)` reads BIR **text** through a small `CondensedParser` (`parseFunction`/`parseBasicBlock`/`skipBlock`/`getLine`) and builds the line↔BB/function map that makes source-level breakpoints and stack traces possible. `getSource`/`getSourceIndex`/`getSourceLine` expose it to the DAP `Source`/`StackTrace` responses.

### 5.3 End-to-end lifecycle

```text
(1) bindio          Adapter binds dap::Session reader/writer to stdio (or a path).
(2) initialize      InitializeRequest → sub_85C7A0: builds Capabilities (the two flags),
                    sends InitializeResponse.
(3) initialized     "sent" callback (Session slot +72 → sub_861CB0) fires InitializedEvent.
(4) launch          LaunchRequestEx → sub_85BFE0 (custom launch config; reads "stopOnStart", §5.4).
(5) configure       client sends SetBreakpoints (→ both BreakpointEditors), then
                    ConfigurationDoneRequest → sub_85B120 → Debugger::start() → Controller::start
                    → BirSim::setDebuggerCallbacks(controller) → spawn engine thread → simulate.
(6) run loop        client issues Continue/Next/StepIn/StepOut/Pause → Controller queue;
                    engine hits a breakpoint / completes a step → onInstPtrSet → DapCallbacks::stop
                    → StoppedEvent("breakpoint"/"step"/"pause"). Logs stream as OutputEvents.
(7) inspect         on a stop, client issues Threads/StackTrace/Scopes/Variables/Evaluate/
                    StepInTargets/Hover → handlers in §3.2 read live sim state (§6).
(8) disconnect      DisconnectRequest → sub_85C6D0: Controller::kill (stop engine),
                    Signal::fire(@+152) (release Adapter::run), reply DisconnectResponse; teardown.
```

### 5.4 `LaunchRequestEx` — the one custom field

The debugger does **not** handle the stock `dap::LaunchRequest`; it registers and handles a custom subclass `dap::LaunchRequestEx`. **[CONFIRMED — `TypeOf<LaunchRequestEx>::serializeFields @0x878fe0`]**: it adds exactly **one** field on top of the inherited launch arguments:

```c
field("stopOnStart", TypeOf<optional<boolean>>::type());  // break at the first instruction
```

i.e. the only Neuron-specific launch argument is a *stop-on-start* flag (halt at the first instruction vs. free-run). No module-list or core-mask fields are added at the DAP layer — those reach the engine via the `Debugger`/`Controller` objects, not the launch JSON.

---

## 6. State inspection — the two-family Expr AST

`evaluate`, `variables`, and `hover` are backed by an expression evaluator with **two** distinct trees that must not be conflated (a third, `pelican::Expr`, is an unrelated middle-end family).

```text
birsim::debugger::Expr       — the PARSED inspection AST (what to read from sim state)
birsim::debugger::ExprValue  — the EVALUATED RESULT tree (rendered to the DAP "variables" view)
```

### 6.1 `Expr` — the inspection AST

```text
Expr  (abstract; vt = [ ~dtor, ~dtor, dump(filesystem::path&), eval() ])
 ├─ EagerExpr        eval @0x88dc00   — evaluate-now wrapper (caches an ExprValue)
 ├─ ByteExpr         eval @0x8b5280   — raw, dtype-agnostic byte read
 ├─ PhyApExpr        eval @0x8b31d0   — physical access-pattern read (overrides dump)
 ├─ RegExpr          eval @0x8b2520   — scalar register-file read
 ├─ DumpExpr         eval @0x8b2580   — dump an AP region to a .npy/file (overrides dump)
 └─ IndexExpr<T>                       — templated indexed scalar read; 9 instantiations
       T ∈ { schar, uchar, short, ushort, int, uint, long, ulong, float }   (NO double/bool)
```

`eval()` is pure per-leaf and returns `unique_ptr<ExprValue>` — **this is the bridge** `Expr → ExprValue`. `dump()` has a default body in `Expr`; only `PhyApExpr` and `DumpExpr` override it.

### 6.2 `ExprValue` — the rendered result

```text
ExprValue  (abstract; vt = [ ~dtor, ~dtor, summarize[cxx11](), getChildren() ])
 ├─ NestedValue                      — addChild/getChildren/summarize (expandable node)
 │    └─ MemObjValue   ::create<T>    — a MemoryObject rendered as a nested DAP variable
 ├─ MessageValue                     — a leaf string/error ("not available")
 └─ ConstValue<T>                     — a scalar leaf; 10 instantiations (the 9 above + bool)
```

`summarize()` produces the DAP variable **value** string; `getChildren()` produces the **variables** expansion tree. `MemObjValue::create<T>` instantiates for the same 9 dtypes as `PhyApExpr` (no `bool`/`double`) — fp16/bf16/fp8 are decoded to `float` at read time per the engine cast path.

### 6.3 Reading live sim state

The eval leaves read **live** `InstVisitor` state ([§7.34](sim-dispatch-state.md)), bound per frame:

```c
// ExprContext::setFrameInfo(bir::Instruction* curInst, birsim::InstVisitor* sim)  @0x606b40
//   — binds the eval context to the CURRENT sim frame (the InstVisitor IS the machine state).

// RegExpr::eval @0x8b2520:
RegState& rs = sim->getRegState();                    // §7.34 RegState @ InstVisitor+0x150
scalar = rs.read(reg, arg);                            // libBIRSimulator RegState::read @0x198880
return ConstValue<T>(scalar)  // dtype-keyed; on miss → MessageValue("…")

// PhyApExpr::eval @0x8b31d0:  reconstruct a MemoryObject COPY from sim->Memory
//   (§7.34: Memory = DenseMap<MemoryLocation* → shared_ptr<MemoryObject>>)
//   → dtype-dispatch to MemObjValue::create<T> → NestedValue tree (addChild per element).
//   ctor takes EXACTLY the §7.34 runAP inputs: MemoryLocation + AP-pairs + offset + dtype.
```

`updateContext(ExprContext&, StackIndex)` rebinds the eval context to a chosen stack frame (calls `setFrameInfo` for that frame), so `evaluate`/`variables` always read the frame the IDE has selected.

### 6.4 The inspection DSL

`ExprParser` (recursive descent) turns a watch/evaluate **string** into an `Expr` tree. Grammar productions each build one leaf:

| Production | Builds | Notes |
|---|---|---|
| `parsePhysicalAp` | `PhyApExpr` | memloc + AP pairs + dtype |
| `parseRegAccessAp` | `RegExpr` | register access |
| `parseDumpAp` | `DumpExpr` | dump AP to file |
| `parseMemLoc` | (resolves a named `MemoryLocation`) | err: `no memloc with name …` |
| `parseApPair` | `bir::APPair{step,num}` | one access-pattern pair |
| `parsePattern` | `IndexExpr<T>` | indexing pattern |
| `parseDtype` | `bir::tryString2Dtype` | err: `Invalid dtype …` |

Memory-space keyword tokens are `"sb"` (SBUF), `"psum"` (PSUM), `"dram"` (DRAM) — matching the [`MemoryLocation`](memory-location.md) property model. A parse failure produces a structured `ParserError` trace ("in current function" / "in current context").

---

## 7. Reimplementation Notes & Confidence

### 7.1 Function map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `Adapter::registerDebugger` | `0x85a860` | binds the 15 debug request handlers | CERTAIN |
| `Adapter::registerSignalHandlers` | `0x85a700` | Init / Disconnect + Initialized-on-sent | CERTAIN |
| `Adapter::run` | `0x85a6b0` | Signal fire/wait, final status | HIGH |
| `Adapter::bindio` | `0x85b020` / `0x85ce30` | stdio / path transport | CERTAIN |
| `sub_85C7A0` (Initialize handler) | `0x85c7a0` | Capabilities = the two flags | CERTAIN |
| `to_string(StopReason)` | `0x873c10` | `"step"`/`"pause"`/`"breakpoint"` | CERTAIN |
| `DapCallbacks::stop` | `0x85a110` | → `StoppedEvent` (decompile failed; disasm + call-site confirm) | HIGH |
| `DapSink::consume` | `0x8749a0` | log record → `OutputEvent` | CERTAIN |
| `Controller::onInstPtrSet` | `0x871b70` | pause point: shouldStop → mutex → cv-wait | CERTAIN |
| `Controller::cont` / `kill` | `0x86e610` / `0x86e6d0` (body frame) | resume / stop engine | HIGH |
| `Controller::start(BirSim&, modules)` | `0x5eea60` | `setDebuggerCallbacks` + spawn thread | HIGH |
| `Controller::BreakpointEditor::setBreakpoint` | `0x870090` | instruction-level breakpoint | HIGH |
| `Debugger::start` | `0x881510` | → `Controller::start` | HIGH |
| `ExprContext::setFrameInfo` | `0x606b40` (sidecar frame) | bind eval ctx to a live `InstVisitor*` | CERTAIN |
| `RegExpr::eval` / `PhyApExpr::eval` | `0x8b2520` / `0x8b31d0` | read RegState / Memory → `ExprValue` | CERTAIN |
| `TypeOf<LaunchRequestEx>::serializeFields` | `0x878fe0` | the single `stopOnStart` field | CERTAIN |
| `TypeOf<Capabilities>::serializeFields` | `0x17fb750` | capability field/offset order | CERTAIN |
| `dap::ContentReader::read` | `0x1816a40` | `"Content-Length:"` frame reader | CERTAIN |

### 7.2 Adversarial self-verification (5 strongest claims)

1. **"15 debug handlers in this exact order with these thunks."** Verified verbatim in the decompiled `registerDebugger @0x85a860`: the `TypeOf<XRequest>::type()` / `sub_XXXXXX` pairs match the §3.2 table 1:1. **Holds — CERTAIN.**
2. **"Exactly two capabilities advertised."** Verified in `sub_85C7A0`: the only capability writes are `v45=257`/`v88=257` (=`0x101`) plus two present-flag copies (`v129=1`/`v173=1`) at body offsets 178/222 — matching `supportsConfigurationDoneRequest`/`supportsStepInTargetsRequest` in the `Capabilities` serializer field order. **Holds — CERTAIN.**
3. **"StopReason has exactly 3 values mapped to the DAP reason strings."** `to_string @0x873c10` returns `"breakpoint"`/`"pause"`/`"step"` and falls to a support-ticket assert for any other value. **Holds — CERTAIN.**
4. **"The engine thread blocks on a condition variable at a stop."** `onInstPtrSet @0x871b70` shows `getStack` → `shouldStop` → `pthread_mutex_lock(this+352)` → vtable-slot-59 callback (the `DapCallbacks` stop → `StoppedEvent`) → `std::condition_variable::wait()`. **Holds — CERTAIN** for the mechanism; the exact StoppedEvent construction is read from the call site since `stop()`'s own decompile failed (so the *event-build* step is HIGH, not CERTAIN).
5. **"Wire is Content-Length-framed JSON."** `ContentReader::read @0x1816a40` literally `match`/`scan`s `"Content-Length:"`. **Holds — CERTAIN.**

### 7.3 Honest ceiling

- The **address-frame duality** is real: the per-symbol IDA sidecars report some functions at a different base than the cross-symbol disassembly (e.g. `Controller::cont` at `0x86e610` in the body frame vs `0x5efae0` in its own sidecar; `start` at `0x5eea60`). Both are this binary; a reimplementer cross-referencing addresses must check both frames before concluding a mismatch. The §7.1 table notes which frame each address is in where they diverge.
- `DapCallbacks::stop @0x85a110` did **not** decompile (Hex-Rays produced no `cfunc`); its `StoppedEvent` emission is inferred from the disasm and the `to_string`/`dap::StoppedEvent` references plus the `onInstPtrSet` call site — **HIGH, not CERTAIN**.
- The cppdap `Session` internals (`Impl::processRequest`/`send`, `seq`/`request_seq` machine) are read from closure typeinfo strings, not a full decompile of every thunk — **STRONG**. The `command`-string routing being inside cppdap's typed `Session` (not a hand-rolled switch in the debugger) is a structural inference from the typed `registerHandler<RequestT>` registration shape — **STRONG**.
- `libwalrus.so` itself is not materialized as a standalone file in the on-disk snapshot; all addresses derive from its IDA decompiled/disasm sidecars (md5 `1d93972b81e619ce6d178a0e4b9003b3`). VA==file-offset holds for `.text`/`.rodata`; the vtable/typeinfo addresses in `.data.rel.ro` carry the section delta.

---

## Cross-References

- [BIR Simulator: Dispatch & Whole-Machine State](sim-dispatch-state.md) — the `InstVisitor` / `Memory` / `RegState` / `SyncState` state model the debugger inspects, and the 110-case visitor it single-steps.
- [BIR Simulator: Core Arithmetic — Cast / Accumulate / RNE](sim-core-arithmetic.md) — the dtype cast path that decodes fp16/bf16/fp8 to `float` (why `IndexExpr<T>`/`MemObjValue::create<T>` have no half/fp8 instantiations).
- [BIR Simulator: Matmul / MX Quantize / Dequantize](sim-matmul-mx.md) — a representative per-op kernel the debugger steps over.
- [BIR Simulator: Elementwise / Reduce + Data-Movement + Indirect](sim-elementwise-datamove.md) — the data-movement kernels whose `MemoryObject` outputs `PhyApExpr` renders.
- [`MemoryLocation`](memory-location.md) — the `sb`/`psum`/`dram` memory-space model the inspection DSL parses against.
- [`InstructionType`](instruction-type.md) — the opcode discriminant a `bir::Instruction*` breakpoint is set on.
