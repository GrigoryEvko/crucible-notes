# Statement & Control Flow Codegen

The statement code generator converts EDG IL statement nodes into LLVM IR basic blocks and terminators. It is the control flow backbone of NVVM IR generation: every `if`, `while`, `for`, `switch`, `goto`, `return`, and compound block passes through a single recursive dispatcher (`sub_9363D0`) that reads a statement-kind byte and fans out to 17 specialized handlers. Each handler creates named basic blocks following a fixed naming convention, connects them with conditional or unconditional branches, and attaches metadata for branch prediction and loop optimization. Understanding this subsystem means understanding exactly how C/CUDA source-level control flow maps to the LLVM IR that downstream optimization passes will transform.

**Binary coordinates:** Handlers span `0x930000`--`0x948000` (~96 KB). The dispatcher itself is at `0x9363D0`; the most complex handler (the inline-`asm` statement emitter at `sub_932270`, IL kind 18) is ~12 KB and 2,476 instructions — a duplicate of `sub_1292420` documented in [Inline Assembly Codegen](./irgen-functions.md#inline-assembly-codegen). Try/catch is not in the dispatch table: the EDG frontend strips it before codegen because CUDA device exceptions are disabled by default (`DEFAULT_EXCEPTIONS_ENABLED = 0`), and the NVVM IR verifier (`sub_2C76F10`) rejects `landingpad`, `invoke`, and `resume` instructions outright.

## Statement Dispatcher — `sub_9363D0` (emitStmt)

```c
void emitStmt(CGModule *cg, StmtNode *stmt);
```

The dispatcher is the only entry point for statement lowering. All control flow handlers, compound statements, and even the top-level function body driver call `emitStmt` recursively.

**Entry logic:**

1. If `cg->currentBB` (offset +96) is NULL, create an anonymous unreachable basic block via `createBB("")` and insert it. This is the "dead code after return" safety net — it ensures the IR builder always has an insertion point, even for unreachable code that follows a `return` or `goto`.

2. Read `stmt->stmtKind` (byte at StmtNode offset +40).

3. Special fast path: if kind == 8 (return), set the IRBuilder debug location and push a scope record, then call `emitReturn` (`sub_9313C0`) and return immediately. Returns get priority handling because they terminate the current BB and may trigger cleanup scope unwinding.

4. General path: set the IRBuilder debug location and push a scope record, then dispatch on kind through a switch table.

### Kind Dispatch Table

| Kind | Statement type | Handler | Address |
|------|---------------|---------|---------|
| 0 | Expression statement | `emitExprStmt` | `sub_921EA0` |
| 1 | `if` statement | `emitIfStmt` | `sub_937020` |
| 2 | `if constexpr` (C++17) | `emitConstexprIf` | `sub_936F80` |
| 5 | `while` loop | `emitWhile` | `sub_937180` |
| 6 | `goto` | `emitGoto` | `sub_931270` |
| 7 | Label statement | `emitLabel` | `sub_930570` |
| 8 | `return` | `emitReturn` | `sub_9313C0` |
| 11 | Compound `{ ... }` | `emitCompound` | `sub_9365F0` |
| 12 | `do-while` loop | `emitDoWhile` | `sub_936B50` |
| 13 | `for` loop | `emitFor` | `sub_936D30` |
| 15 | `case` label | `emitCase` | `sub_935670` |
| 16 | `switch` statement | `emitSwitch` | `sub_9359B0` |
| 17 | Variable declaration | `emitDeclStmt` | `sub_9303A0` |
| 18 | `asm` statement (`__asm__(...)`) | `emitAsmStmt` | `sub_932270` |
| 20 | Cleanup/destructor scope | `emitCleanupScope` | `sub_931670` |
| 24 | Null/empty statement | *(return immediately)* | — |
| 25 | Expression statement (alt) | `emitExprStmt` | `sub_921EA0` |

Kinds 0 and 25 share the same handler. The split likely distinguishes C expression-statements from GNU statement-expressions or a similar EDG internal distinction. Any unrecognized kind triggers `fatal("unsupported statement type")`.

Gaps in the numbering (3, 4, 9, 10, 14, 19, 21--23) either correspond to statement types handled entirely in the EDG frontend (lowered before codegen sees them) or are reserved for future use.

The IL-18 handler `sub_932270` is the statement-path (Path A) duplicate of the inline-asm-to-LLVM `InlineAsm` translator; the near-identical Path B variant lives at `sub_1292420`. Both implement the same 7-phase template parser, constraint table, and operand binding logic with different diagnostic function pointers. See [Inline Assembly Codegen](./irgen-functions.md#inline-assembly-codegen) for the full pipeline.

---

## If Statement — `sub_937020`

Reads from the StmtNode: condition expression at offset +48, then-body at +72, else-body at +80 (may be NULL).

### BB Layout: if/else

```text
    ┌─────────────────────┐
    │    current BB        │
    │  %cond = ...         │
    │  br i1 %cond,        │
    │    label %if.then,    │
    │    label %if.else     │
    └──┬──────────────┬────┘
       │              │
       ▼              ▼
 ┌──────────┐   ┌──────────┐
 │ if.then  │   │ if.else  │
 │ <then>   │   │ <else>   │
 │ br %end  │   │ br %end  │
 └────┬─────┘   └────┬─────┘
      │               │
      ▼               ▼
    ┌─────────────────────┐
    │      if.end          │
    └─────────────────────┘
```

### BB Layout: if without else

```text
    ┌─────────────────────┐
    │    current BB        │
    │  %cond = ...         │
    │  br i1 %cond,        │
    │    label %if.then,    │
    │    label %if.end      │
    └──┬──────────────┬────┘
       │              │
       ▼              │
 ┌──────────┐         │
 │ if.then  │         │
 │ <then>   │         │
 │ br %end  │         │
 └────┬─────┘         │
      │               │
      ▼               ▼
    ┌─────────────────────┐
    │      if.end          │
    └─────────────────────┘
```

**LLVM IR pseudocode:**

```llvm
  %cond = icmp ne i32 %x, 0           ; evalCondition: convert to i1
  br i1 %cond, label %if.then, label %if.else, !prof !0

if.then:
  ; ... then-body codegen ...
  br label %if.end

if.else:
  ; ... else-body codegen ...
  br label %if.end

if.end:
  ; continues here

!0 = !{!"branch_weights", i32 2000, i32 1}  ; if __builtin_expect(x, 1)
```

### Branch Weight Metadata

`sub_92F9D0` examines `__builtin_expect` annotations on branch bodies by checking bit flags at StmtNode offset +41:

| Flag | Source annotation | Weight encoding | Metadata attached |
|------|------------------|----------------|-------------------|
| bit 0x10 | `__builtin_expect(x, 1)` — likely | weightHint = 1 | `!{!"branch_weights", i32 2000, i32 1}` |
| bit 0x20 | `__builtin_expect(x, 0)` — unlikely | weightHint = 2 | `!{!"branch_weights", i32 1, i32 2000}` |
| neither | no annotation | weightHint = 0 | *(no metadata)* |

The 2000:1 ratio represents 99.95% prediction confidence. For compound statements (kind 11), the function recurses into the compound's first child statement to find the annotation.

### Constexpr If — `sub_936F80`

C++17 `if constexpr` is fully resolved during EDG frontend semantic analysis. By the time the codegen sees it, only the taken branch body survives. The handler reads a selection record from offset +72: a bit at +24 determines which of two fields contains the surviving body pointer. If non-null, it creates `constexpr_if.body` and `constexpr_if.end` BBs and emits the body with an unconditional branch to `.end`. If null (dead branch entirely eliminated), no codegen occurs at all.

---

## While Loop — `sub_937180`

```text
    ┌─────────────────────┐
    │    current BB        │
    │  br label %while.cond│
    └─────────┬───────────┘
              │
              ▼
    ┌─────────────────────┐◄──────────────┐
    │   while.cond         │               │
    │  %c = ...            │               │
    │  br i1 %c,           │               │
    │    label %while.body, │               │
    │    label %while.end   │               │
    └──┬──────────────┬────┘               │
       │              │                    │
       ▼              │                    │
 ┌──────────────┐     │                    │
 │ while.body   │     │                    │
 │ <body>       │     │                    │
 │ br %cond ────┼─────┼────────────────────┘
 └──────────────┘     │         backedge with
                      │         !llvm.loop metadata
                      ▼
              ┌──────────────┐
              │  while.end   │
              └──────────────┘
```

**LLVM IR pseudocode:**

```llvm
  br label %while.cond

while.cond:
  %c = icmp slt i32 %i, %n
  br i1 %c, label %while.body, label %while.end

while.body:
  ; ... body codegen ...
  br label %while.cond, !llvm.loop !1

while.end:
  ; continues here

!1 = !{!1, !2}                                    ; self-referential loop ID
!2 = !{!"llvm.loop.mustprogress"}
```

The backedge branch (`br label %while.cond` from `while.body`) always receives `!llvm.loop` metadata via `emitLoopMustProgress` (`sub_930810`). If the loop carries `#pragma unroll`, additional unroll metadata is merged into the same MDNode (see [Loop Metadata](#loop-metadata-pragma-unroll-and-mustprogress) below).

---

## Do-While Loop — `sub_936B50`

The key structural difference from `while`: the body executes before the condition. The condition BB follows the body.

```text
    ┌─────────────────────┐
    │    current BB        │
    │  br label %do.body   │
    └─────────┬───────────┘
              │
              ▼
    ┌─────────────────────┐◄──────────────┐
    │   do.body            │               │
    │  <body>              │               │
    │  br label %do.cond   │               │
    └─────────┬───────────┘               │
              │                            │
              ▼                            │
    ┌─────────────────────┐               │
    │   do.cond            │               │
    │  %c = ...            │               │
    │  br i1 %c,           │               │
    │    label %do.body, ──┼───────────────┘
    │    label %do.end     │     backedge
    └──────────────┬──────┘
                   │
                   ▼
           ┌──────────────┐
           │   do.end      │
           └──────────────┘
```

**LLVM IR pseudocode:**

```llvm
  br label %do.body

do.body:
  ; ... body codegen ...
  br label %do.cond

do.cond:
  %c = icmp ne i32 %x, 0
  br i1 %c, label %do.body, label %do.end, !llvm.loop !1

do.end:
  ; continues here
```

The backedge is the conditional branch in `do.cond` (true edge back to `do.body`). Debug location is set separately for the condition expression using the condition node's own source location (offset +36 from the condition expression node).

---

## For Loop — `sub_936D30`

The most complex loop handler. Reads four components from the StmtNode: init statement at offset +80 field [0], condition at +48, increment expression at +80 field [1], and body at +72. Any of init, condition, and increment may be NULL.

```text
    ┌─────────────────────┐
    │    current BB        │
    │  <init statement>    │    ← emitted in current BB if non-null
    │  br label %for.cond  │
    └─────────┬───────────┘
              │
              ▼
    ┌─────────────────────┐◄──────────────┐
    │   for.cond           │               │
    │  %c = ... or true    │               │
    │  br i1 %c,           │               │
    │    label %for.body,   │               │
    │    label %for.end     │               │
    └──┬──────────────┬────┘               │
       │              │                    │
       ▼              │                    │
 ┌──────────────┐     │                    │
 │  for.body    │     │                    │
 │  <body>      │     │                    │
 │  br %for.inc │     │                    │
 └──────┬───────┘     │                    │
        │             │                    │
        ▼             │                    │
 ┌──────────────┐     │                    │
 │  for.inc     │     │                    │
 │  <increment> │     │                    │
 │  br %for.cond┼─────┼────────────────────┘
 └──────────────┘     │         backedge
                      ▼
              ┌──────────────┐
              │   for.end    │
              └──────────────┘
```

**LLVM IR pseudocode:**

```llvm
  ; init: i = 0
  store i32 0, ptr %i.addr, align 4
  br label %for.cond

for.cond:
  %i = load i32, ptr %i.addr, align 4
  %cmp = icmp slt i32 %i, %n
  br i1 %cmp, label %for.body, label %for.end

for.body:
  ; ... body codegen ...
  br label %for.inc

for.inc:
  %i1 = load i32, ptr %i.addr, align 4
  %inc = add nsw i32 %i1, 1
  store i32 %inc, ptr %i.addr, align 4
  br label %for.cond, !llvm.loop !1

for.end:
  ; continues here
```

**Special cases:**

- **Null condition:** If the condition expression is NULL (e.g., `for(;;)`), the handler calls `ConstantInt::getTrue` (`sub_ACD6D0`) to create an unconditionally-true condition, producing an infinite loop.
- **Volatile increment:** If the increment expression operates on a volatile pointer (type descriptor `& 0xFB == 8` and `isVolatile()` returns true), the store is marked volatile.
- **Scope tracking:** Outside "fast codegen" mode (`dword_4D04658 == 0`), pushes a `DW_TAG_lexical_block` debug scope at for-loop entry via `sub_941230`/`sub_9415C0` and pops it at exit via `sub_93FF00`. This generates correct DWARF scoping so debuggers see `for`-local variables in the right scope.

The `for.inc` BB is only created when an increment expression exists. If omitted, the body branches directly back to `for.cond`.

---

## Switch Statement — `sub_9359B0`

The largest pure-control-flow handler (~550 decompiled lines; the inline-`asm` handler at `sub_932270` is larger but is not control flow). Uses a three-phase approach with an internal open-addressing hash table.

### Phase 1: Build case-to-BB mapping

Iterates the case list (linked list at `stmt[10]+16`, next pointer at +32). For each case label, creates a `switch_case.target` BB. Also creates one `switch_case.default_target` BB for the default case. Stores the mapping in an open-addressing hash table at CGModule offsets +496 through +520.

**Hash table layout (32-byte entries):**

| CGModule offset | Field |
|----------------|-------|
| +496 | numEntries |
| +504 | bucket array pointer |
| +512 | numOccupied |
| +516 | numTombstones |
| +520 | capacity |

Uses the standard DenseMap infrastructure with LLVM-layer sentinels (-4096 / -8192). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function and growth policy.

### Phase 2: Emit LLVM SwitchInst

Evaluates the switch condition via `sub_92F410`, then creates a `SwitchInst` via `sub_B53A60` (`SwitchInst::Create`) with the case count, default target BB, and condition value. Each case constant is added via `sub_B53E30` (`SwitchInst::addCase`).

### Phase 3: Emit body

Creates a `switch_child_entry` BB, inserts it, and recursively emits the switch body. If the switch has no explicit `default:` case, emits a fallthrough to the `switch_case.default_target` BB.

**LLVM IR pseudocode:**

```llvm
  %val = load i32, ptr %x.addr
  switch i32 %val, label %switch_case.default_target [
    i32 0, label %switch_case.target
    i32 1, label %switch_case.target1
    i32 5, label %switch_case.target2
  ]

switch_case.target:                ; case 0
  ; ...
  br label %switch_child_entry     ; fallthrough or break

switch_case.target1:               ; case 1
  ; ...

switch_case.default_target:        ; default
  ; ...
```

Note that cicc always emits an LLVM `switch` instruction. The decision to lower a switch into a jump table versus sequential comparisons is made later by the SelectionDAG backend (specifically `NVPTXTargetLowering`), not during IR generation. The codegen produces a clean, canonical `switch` and lets the backend optimize the dispatch strategy.

### Case Label — `sub_935670`

When the recursive statement walk encounters a case label (kind 15), it looks up the parent switch node (asserts `stmtKind == 16`), finds the pre-allocated target BB from the hash table, and calls `insertBB` to make it the current insertion point. Fatal error `"basic block for case statement not found!"` if the hash table lookup fails.

For the default case (identified by a null value at +8), retrieves the last entry in the mapping vector.

---

## Goto and Label Statements

### Goto — `sub_931270`

Reads the target label from `stmt->auxData+128`. Fatal error if null: `"label for goto statement not found!"`.

Two code paths based on cleanup state:

**Simple goto** (no active cleanups, CGModule offset +240 == 0): Resolves the label to its BB via `sub_946C80` and emits an unconditional branch.

**Goto with cleanups** (offset +240 != 0): Before branching, the handler must destroy all local variables whose scope is being exited. Calls `sub_9310E0` to compute the destruction set, iterates each variable calling `sub_9465D0` to emit destructor calls, resets the cleanup stack, then resolves and branches to the label BB.

```llvm
  ; goto with cleanup: jumping out of scope with a std::string local
  call void @_ZNSsD1Ev(ptr %str)     ; ~string()
  br label %label_target
```

### Label — `sub_930570`

Resolves the label to its BB via `sub_946C80` and inserts it as the current basic block via `insertBB`. The BB name comes from the label's symbol name in the EDG IL.

### Computed Goto (GCC `&&label` Extension)

Computed goto is handled in the expression codegen layer, not the statement dispatcher. Expression kind `0x6F` at `sub_921EA0` calls the block-address emitter (`sub_1285E30(builder, label, 1)`) to produce an LLVM `blockaddress` constant for the `&&label` GCC extension; expression kind `0x71` calls the same emitter with flag `0` (`sub_1285E30(builder, label, 0)`) to produce the `indirectbr` instruction for `goto *p`; expression kind `0x70` (label-as-value, distinct entry point via `sub_12812E0`) materializes the label reference as a typed value. The resulting `indirectbr` instruction is lowered later by `IndirectBrExpandPass` (pipeline parser index 247, `"indirectbr-expand"`) because NVPTX does not natively support indirect branches — they are expanded into a switch over all possible target labels.

---

## Return Statement — `sub_9313C0`

Reads the return expression from StmtNode offset +48. Dispatches on CGModule return-type information (offsets +208 and +216):

**Path A — Aggregate (struct) return:** If the return type is aggregate (`sub_91B770` returns true), emits a memcpy-like sequence into the `sret` pointer via `sub_947E80`. For multi-register returns (offset +216 > 0), uses bit-width analysis (`_BitScanReverse64`) to determine the return bit layout.

**Path B — Scalar return:** Evaluates the expression, creates a `ReturnInst` via `sub_B4D3C0`, and may bitcast the value for ABI compliance via `sub_AE5020`.

**Path C — Void return with expression:** Evaluates the expression for side effects only (calls `emitExprStmt`), then falls through to emit a void return.

**Cleanup before return:** If `cg->hasCleanups` (offset +240) is set, calls `sub_9310E0` to compute the set of locals requiring destruction, emits destructor calls in reverse order, resets the cleanup stack, then emits an unconditional branch to the function's unified return block (offset +200).

```llvm
  ; return with cleanup unwind
  call void @_ZN3FooD1Ev(ptr %obj)    ; ~Foo()
  store i32 %retval, ptr %retval.addr
  br label %return

return:                               ; unified return BB
  %0 = load i32, ptr %retval.addr
  ret i32 %0
```

The unified return block pattern means every `return` in a function branches to a single shared `return` BB rather than emitting `ret` directly. This is standard in compilers because it simplifies cleanup handling and produces cleaner IR for optimization.

---

## Inline `asm` Statement — `sub_932270`

IL kind 18 routes to `sub_932270` (~12 KB, 2,476 instructions). The dispatcher's sole call site lives in the `case 18:` arm at `0x9363D0+0x...`, and `sub_932270` itself has exactly one caller — this statement dispatcher. Its body is the 7-phase CUDA `__asm__()` template-to-LLVM `InlineAsm` translator: template scan with `%N` / `%cN` / `%=` / `%[name]` handling, `'C'` constraint string-literal extraction, constraint-class parsing, operand binding, and tied-operand validation. Diagnostic strings include `"symbolic operand reference not supported!"`, `"asm operand index requested is larger than the number of asm operands provided!"`, `"error extracting string literal operand"`, `"error extracting address of constant for 'C' constraint"`, and `"tied input/output operands not supported!"`.

This is the **Path A** copy of the inline-asm emitter; a near-identical Path B copy at `sub_1292420` lives in the function-codegen module and is called from `sub_1296350`. Both share the same constraint table and parser structure; they differ only in which diagnostic and value-resolution helpers they invoke. The full 7-phase pipeline (template parsing, template reconstruction, constraint classification, operand binding, `InlineAsm` construction, sideeffects/alignstack flags, call instruction emission) is documented in detail at [Inline Assembly Codegen](./irgen-functions.md#inline-assembly-codegen).

There is no separate try/catch handler in the statement dispatcher: CUDA device code disables exceptions by default (`DEFAULT_EXCEPTIONS_ENABLED = 0`) and the EDG frontend lowers any host-only try/catch before codegen sees it. The NVVM IR verifier (`sub_2C76F10`) reinforces this by rejecting `landingpad`, `invoke`, and `resume` outright.

---

## Cleanup/Destructor Scope — `sub_931670`

Handles statement kind 20. Only active when `cg->hasCleanups` (offset +240) is set.

Walks a linked list at StmtNode offset +72. For each entry where the byte at +8 equals 7 (indicating a variable with non-trivial destructor):

1. Extracts the variable reference at `entry[2]` (offset +16).
2. Checks visibility flags (bits 0x60 at +170, byte +177 != 5) to skip external and static symbols.
3. Looks up the variable in the CGModule's var-lookup hash table (offsets +8 through +24) using the same hash function as the switch table.
4. If the variable is already registered for cleanup (checked via `sub_91CCF0`), adds it to the pending cleanup list and emits an immediate destructor call via `sub_9465D0`.
5. If not yet registered, just adds it to the pending list for later processing.

This mechanism ensures that C++ automatic variables with non-trivial destructors are properly destroyed when their scope exits — whether by normal control flow, `goto`, `return`, or exception propagation.

---

## Compound Statement — `sub_9365F0`

Handles `{ ... }` blocks (kind 11). This is the workhorse that ties everything together: the function body itself is a compound statement, and every block scope creates a nested compound.

**Cleanup frame management:** When `cg->hasCleanups` (offset +240) is set, pushes a new cleanup frame onto the cleanup stack (offset +424). Each frame is a 24-byte record: `{pendingDestructors ptr, end, capacity}`.

**Variable declarations:** Iterates local declarations at scope fields [14] and [15] (linked lists). For each local variable, emits an `alloca` or initializer as needed. If the variable has a non-trivial destructor, registers it in the cleanup set.

**Statement iteration:** Walks the child statement linked list starting at StmtNode offset +72, following `nextStmt` pointers at +16. For each child, calls `emitStmt(cg, child)` recursively. Between statements, checks whether pending cleanups need flushing (temporaries with non-trivial destructors). If new cleanup entries appeared since the last check, iterates them in reverse order and emits destructor calls.

**Statement-expression support (GNU extension):** For `({...})` expressions, if the last statement in the block is an expression (kind 0 or 25), treats its value as the compound's result. Fatal error: `"unexpected: last statement in statement expression is not an expression!"` if the last statement is not an expression type.

**Scope tracking:** Outside fast-codegen mode, pushes a `DW_TAG_lexical_block` debug scope at entry and pops at exit, so debuggers correctly associate variables with their lexical scope.

---

## Variable Declaration — `sub_9303A0`

Reads the variable descriptor from StmtNode offset +72, then the variable's symbol from descriptor +8.

**Initialization dispatch** (based on byte at symbol +177):

| Value | Meaning |
|-------|---------|
| 4 | Block-scope static — `fatal("block scope static variable initialization is not supported!")` |
| 0, 3 | No dynamic init needed — skip codegen |
| 2 | Dynamic initialization — main path |

Dynamic init sub-dispatch (descriptor +48 byte):

| Sub-kind | Handler | Purpose |
|----------|---------|---------|
| 1 | `sub_91DAD0` | Load-address style init |
| 2 | `sub_91FFE0` | Emit initializer expression |
| 3 | `sub_92F410` | Direct expression evaluation |
| other | — | `fatal("unsupported dynamic initialization")` |

After computing the initializer value, the handler checks for volatile store qualification, computes alignment via `sub_91CB50`, retrieves the alloca/global address via `sub_9439D0`, and emits the store via `sub_923130`.

```llvm
  %x = alloca i32, align 4              ; from function prologue
  %init = call i32 @compute_value()     ; dynamic initialization
  store i32 %init, ptr %x, align 4      ; emitDeclStmt
```

Block-scope static variables (`static int x = expr;`) are explicitly unsupported and fatal. In CUDA device code, block-scope statics have no sensible semantics (no persistent thread-local storage across kernel invocations), so this restriction is intentional.

---

## Loop Metadata: Pragma Unroll and Mustprogress

### Pragma Unroll — `sub_9305A0`

Called from while, do-while, and for handlers when StmtNode offset +64 (pragma annotation) is non-NULL. Parses `"unroll %d"` from the pragma string via `sscanf`.

| Count value | Metadata produced |
|-------------|-------------------|
| `0x7FFFFFFF` (INT_MAX) | `!{!"llvm.loop.unroll.full"}` |
| Specific N | `!{!"llvm.loop.unroll.count", i32 N}` |
| <= 0 | `fatal("Unroll count must be positive.")` |
| Parse failure | `fatal("Parsing unroll count failed!")` |

The metadata is wrapped in the standard LLVM loop-ID self-referential MDNode pattern:

```llvm
  br label %for.cond, !llvm.loop !3

!3 = !{!3, !4}                                      ; self-ref loop ID
!4 = !{!"llvm.loop.unroll.count", i32 8}
```

Global flag `dword_4D046B4` ("skip pragma" mode) gates this entirely — when set, `sub_9305A0` returns immediately.

### Loop Mustprogress — `sub_930810`

Called on **every** loop backedge (while, do-while, for). Creates `!{!"llvm.loop.mustprogress"}` and attaches it to the backedge branch. If the backedge already has `!llvm.loop` metadata (from pragma unroll), the existing operands are read and the mustprogress node is appended to create a combined MDNode:

```llvm
  br label %while.cond, !llvm.loop !5

!5 = !{!5, !6, !7}                                  ; merged: self-ref + unroll + mustprogress
!6 = !{!"llvm.loop.unroll.count", i32 4}
!7 = !{!"llvm.loop.mustprogress"}
```

This metadata tells the LLVM optimizer that loops must make forward progress — it is allowed to remove provably-infinite side-effect-free loops. This corresponds to the C++ forward progress guarantee required by the standard.

---

## Infrastructure Functions

### createBB — `sub_945CA0`

Allocates an 80-byte `BasicBlock` object and initializes it with the LLVM context from CGModule offset +40. The name parameter produces the characteristic BB names visible throughout this page: `"if.then"`, `"while.cond"`, `"for.inc"`, `"switch_case.target"`, `"constexpr_if.body"`, etc.

### insertBB — `sub_92FEA0`

```c
void insertBB(CGModule *cg, BasicBlock *bb, int canDelete);
```

Finalizes the current BB (emits an implicit unconditional branch to `bb` if the current BB lacks a terminator), then inserts `bb` into the function's BB list. If `canDelete` is 1 and the BB has no predecessors, the BB is immediately freed — this garbage-collects unreachable continuation blocks (e.g., `if.end` when both branches terminate, `while.end` when the loop is infinite).

The `canDelete=1` flag is used for `if.end`, `while.end`, `for.end`, and `do.end` BBs.

### finalizeBB / emitBr — `sub_92FD90`

If the current BB exists and its last instruction is NOT a terminator (opcode check: `opcode - 30 > 10` filters out `br`, `ret`, `switch`, etc.), creates a `BranchInst` to the target BB and inserts it. Then clears `cg->currentBB` and the insert point.

### emitCondBr — `sub_945D00`

Creates a conditional `BranchInst` with true/false targets and optional branch weight metadata. When `weightHint != 0`, attaches `!prof branch_weights` metadata via `MDBuilder::createBranchWeights`.

### evalCondition — `sub_921E00`

Evaluates a condition expression and converts the result to `i1`. Checks for aggregate types (fatal error if the condition is an aggregate), determines signedness, evaluates the expression, then emits `icmp ne 0` (integer) or `fcmp une 0.0` (floating point) to produce a boolean.

---

## EDG StmtNode Layout

Reconstructed from usage patterns across all statement handlers:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 4 | Source location: line number |
| +4 | 2 | Source location: column number |
| +16 | 8 | `nextStmt` — linked list pointer |
| +40 | 1 | `stmtKind` — enum value (0--25 observed) |
| +41 | 1 | Flags (bit 0x10 = likely, bit 0x20 = unlikely) |
| +48 | 8 | `exprPayload` / condition expression pointer |
| +64 | 8 | Pragma annotation (NULL or `"unroll N"` string) |
| +72 | 8 | `auxData` — kind-specific (then-body, label, variable descriptor, etc.) |
| +80 | 8 | `auxData2` — kind-specific (else-body for if, init/increment for for, etc.) |

## CGModule Offsets Used by Statement Codegen

| Offset | Size | Field |
|--------|------|-------|
| +8 | 8 | `varLookupTable.buckets` |
| +24 | 4 | `varLookupTable.capacity` |
| +40 | 8 | `llvmContext` |
| +96 | 8 | `currentBB` (BasicBlock pointer) |
| +104 | 8 | `insertPoint` |
| +192 | 8 | `currentFunction` (Function pointer) |
| +200 | 8 | `returnBlock` (unified return BB) |
| +208 | 8 | `returnValue` / sret pointer |
| +216 | 4 | `returnAlignment` |
| +240 | 1 | `hasCleanups` flag |
| +248 | — | `cleanupSet` (DenseSet tracking which vars need cleanup) |
| +424 | 8 | `cleanupStack` pointer (24-byte frames) |
| +496 | 8 | `switchHashTable.count` |
| +504 | 8 | `switchHashTable.buckets` |
| +512 | 4 | `switchHashTable.numOccupied` |
| +516 | 4 | `switchHashTable.numTombstones` |
| +520 | 4 | `switchHashTable.capacity` |
| +528 | 8 | `currentScope` pointer |

## Global Mode Flags

| Global | Purpose |
|--------|---------|
| `dword_4D04658` | **Fast codegen mode.** Skips debug location emission, scope tracking, and some pragma processing. Corresponds to `-G0` or equivalent "no debug" mode. |
| `dword_4D046B4` | **Skip pragma mode.** `sub_9305A0` (pragma unroll handler) returns immediately. Also gates some compound-statement declaration processing. |
| `dword_4F077C4` | **CUDA compilation mode.** Value 2 triggers alternate volatile-qualification logic in for-loop increment and variable declaration codegen. |

## Complete BB Naming Reference

Every basic block created by the statement codegen uses one of these exact names:

| Statement type | BB names created |
|---------------|-----------------|
| `if` | `if.then`, `if.else`, `if.end` |
| `if constexpr` | `constexpr_if.body`, `constexpr_if.end` |
| `while` | `while.cond`, `while.body`, `while.end` |
| `do-while` | `do.body`, `do.cond`, `do.end` |
| `for` | `for.cond`, `for.body`, `for.inc`, `for.end` |
| `switch` | `switch_case.target` (per case), `switch_case.default_target`, `switch_child_entry` |
| `goto` / `label` | *(named from label symbol)* |
| `return` | *(branch to unified return block)* |
| `compound { }` | *(no BBs unless cleanup)* |
| dead code | `""` (anonymous unreachable BB) |

These names survive into the final LLVM IR dump (`-Xcuda-ptxas=-v`) and are visible in optimization pass debug output. Recognizing them immediately tells you which source-level construct produced a given IR region.
