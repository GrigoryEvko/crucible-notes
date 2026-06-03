# GetPjrtApi Thunk & tpu_plugin Object

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

A PJRT plugin is a `.so` with exactly one job: hand a framework a pointer to a `PJRT_Api` function-pointer table. `libtpu.so` does this through a two-symbol arrangement that is worth pulling apart, because the obvious single-function design is *not* what the binary does. The public, exported, versioned symbol — `GetPjrtApi @ 0xe6a83a0` — is a five-byte `jmp` thunk that carries no logic at all. It tail-calls an internal, **unexported** engine, `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440`, which is the real lazy builder: a straight-line run of seventeen Itanium-ABI `__cxa_guard`-protected one-shot blocks that materialize the extension chain and the table, then returns a pointer to a function-local static. That static — `GetTpuPjrtApi()::pjrt_api @ 0x227BA840` — is a 1120-byte zero-filled slab in `.lbss` (NOBITS); it is the only `PJRT_Api` instance in the process, and it does **not** exist as an image in any `PROGBITS` section. There is nothing to disassemble in the table itself at rest; its 140 slots are written at first call by `pjrt::CreatePjrtApi @ 0xf874160`.

This is the C++ Meyers-singleton pattern applied to a C ABI: an exported C trampoline in front of a C++ function-local static, lazy-built under a guard, leaked at process exit. The split exists for two reasons. First, the public name must be the canonical lowercase-`jrt` `GetPjrtApi` (matching every other PJRT plugin) while the internal builder keeps the descriptive `GetTpuPjrtApi` name and stays out of the dynamic symbol table — so a framework cannot accidentally bind to the builder, and the builder's TPU-specific identity stays private. Second, the thunk lets the linker place the exported entry in a tiny stub section while the multi-kilobyte builder lives wherever the rest of `pjrt::tpu_plugin` does. The functions the builder binds into the table's five TPU slots are members of that `pjrt::tpu_plugin` namespace — `PJRT_Client_Create`, `PJRT_Plugin_Initialize`, `PJRT_TopologyDescription_Create`, `PJRT_ExecuteContext_Create` — and they are the only place the generic XLA PJRT table touches TPU silicon.

This page owns the **exported thunk**, the **`GetTpuPjrtApi` lazy-init builder** (its guard structure, return path, and the `CreatePjrtApi` call that ends it), the **`pjrt_api` singleton** (storage, lifetime, why it is invisible at rest), and the **`tpu_plugin` namespace object** the slots bind to. It does **not** reproduce the 140-slot field table ([../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md)), the 16-node `__cxa_guard` extension build order ([../pjrt/extension-chain.md](../pjrt/extension-chain.md)), or the dlopen-time static-init landscape and `PJRT_Plugin_Initialize` driver bring-up ([module-init-plugin-discovery.md](module-init-plugin-discovery.md)).

For reimplementation, the contract is:

- **The thunk shape** — one exported `GetPjrtApi@@VERS_1.0`, body `jmp <builder>`, no prologue, no arguments.
- **The lazy-init guard structure** — 17 `__cxa_guard` blocks in `GetTpuPjrtApi`; 16 build extension nodes, the 17th calls `CreatePjrtApi`; the function returns `&pjrt_api` regardless of which guards it actually ran this call.
- **The singleton** — a `.lbss` function-local static, 1120 bytes, zero at load, written once, immutable thereafter, never freed.
- **The `tpu_plugin` object** — the four TPU-specialized slot implementations and the one generic-XLA attributes implementation that `CreatePjrtApi` receives as arguments.

| | |
|---|---|
| **Exported entry symbol** | `GetPjrtApi @ 0xe6a83a0` (5-byte `jmp` thunk, `GetPjrtApi@@VERS_1.0`) |
| **Thunk target / builder** | `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` (1336 B, **not** exported) |
| **Table constructor** | `pjrt::CreatePjrtApi @ 0xf874160` (1872 B, 17th guard only) |
| **Singleton storage** | `GetTpuPjrtApi()::pjrt_api @ 0x227BA840`, `.lbss` (NOBITS), 1120 B = 140 × 8 |
| **Guard count** | 17 `__cxa_guard` blocks (16 extension builders + 1 `CreatePjrtApi`) |
| **Guard implementation** | libtpu's own libc++abi `__cxa_guard_acquire/release/abort @ 0x213e9ac0 / 0x213e9be0 / 0x213e9c20` |
| **Chain head passed to slot 1** | `&host_memory_allocator_extension @ 0x224C3F68` |
| **TPU-injected slots** | 8, 9, 15, 87, 103 (Plugin_Initialize, Plugin_Attributes, Client_Create, TopologyDescription_Create, ExecuteContext_Create) |
| **C-API version** | v0.103 — version qword `0x6700000000` → `{major=0, minor=0x67=103}` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## The Exported Thunk — `GetPjrtApi`

### Purpose

`GetPjrtApi` is the single rendezvous symbol every XLA front-end resolves with `dlsym`. It exists to give that name a stable, versioned, exported address while keeping all the actual logic in an internal function. The thunk has no behavior of its own; it forwards.

### Entry Point

```text
framework (JAX / TF / PyTorch-XLA)
  dlsym(handle, "GetPjrtApi")           ── the ONLY exported name matching /Pjrt/
    │
    └─ GetPjrtApi  0xe6a83a0  (5 bytes)  ── e9 9b 20 00 00  jmp 0xe6aa440
         └─ pjrt::tpu_plugin::GetTpuPjrtApi  0xe6aa440  (the real builder)
```

### Algorithm

The thunk is a single tail-call. IDA renders it as a one-liner; the raw bytes are a relative `jmp`.

```c
// GetPjrtApi @ 0xe6a83a0  (5 bytes, attribute: thunk)
//   machine code:  e9 9b 20 00 00   jmp 0xe6aa440
const PJRT_Api* GetPjrtApi(void) {
    return pjrt::tpu_plugin::GetTpuPjrtApi();   // tail call — no own frame
}
```

Because it is a `jmp` and not a `call`, `GetPjrtApi` consumes no stack frame and leaves no return address of its own: the builder's `ret` returns directly to the framework. A reimplementation can equally emit the builder under the public name; the thunk is a linker/visibility convenience, not a semantic requirement.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `GetPjrtApi` | `0xe6a83a0` | 5 B | Exported `jmp` thunk → builder | CONFIRMED |
| `pjrt::tpu_plugin::GetTpuPjrtApi` | `0xe6aa440` | 1336 B | The builder it forwards to (internal) | CONFIRMED |

> **GOTCHA —** spelling and casing are part of the ABI. The exported symbol is `GetPjrtApi` (lowercase `jrt`); `GetTpuPjrtApi` is internal and **not** in the dynamic symbol table. A loader that `dlsym`s `GetTpuPjrtApi`, or a build that exports only the `Tpu`-cased name, fails discovery. The ~217 `Tpu*_*` symbols that *are* exported are the legacy StreamExecutor C-ABI, never reached through PJRT — see [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md).

> **NOTE —** `GetPjrtApi` is the only `GLOBAL FUNC` export matching `/Pjrt/`, versioned `GetPjrtApi@@VERS_1.0`. The signature is the canonical PJRT plugin entry: `const PJRT_Api* GetPjrtApi(void)` — no arguments, returns the table pointer.

---

## The Lazy Builder — `GetTpuPjrtApi`

### Purpose

`GetTpuPjrtApi` is the construction engine for the entire PJRT surface. On the first call it builds the 16 `.bss`-resident extension nodes and writes the 140-slot table; on every subsequent call it skips past all 17 satisfied guards and returns the same pointer. It is the function-local-static lifetime owner of `pjrt_api`.

### Entry Point

```text
GetTpuPjrtApi  0xe6aa440
  ├─ guard 1..16:  CreateXxxExtension(&node, &prev_node[, tpu_fns])  ── 16 .bss nodes
  │                  (RawBuffer → … → HostMemoryAllocator; build order owned by
  │                   ../pjrt/extension-chain.md)
  └─ guard 17:     CreatePjrtApi(&pjrt_api, Client_Create, ExecuteContext_Create,
                                 TopologyDescription_Create, Plugin_Initialize,
                                 &host_memory_allocator_extension, Plugin_Attributes_Xla)
                     └─ writes all 140 slots into 0x227BA840
       return &pjrt_api  =  0x227BA840   (.lbss)
```

### Algorithm

The body is seventeen identical `__cxa_guard`-gated blocks followed by a single `return`. Each block tests the guard byte, acquires it if unset, runs the one-shot work, and releases. The decompile confirms this verbatim; below it is condensed to the first builder, the structural invariant, and the final `CreatePjrtApi` block.

```c
// pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440  (1336 B)
// Each of the 17 blocks is: if (!guard_byte && __cxa_guard_acquire(&guard)) { work; __cxa_guard_release(&guard); }
function GetTpuPjrtApi():

    // --- guards 1..16: build the 16 .bss extension nodes (construction order) ---
    once(raw_buffer_extension):                          // built FIRST
        CreateRawBufferExtension(&raw_buffer_extension,
                                 &profiler_extension);    // next = .data Profiler seed (type 1)
    once(layouts_extension):
        CreateLayoutsExtension(&layouts_extension, &raw_buffer_extension);
    // … 13 more builders, each next = previously-built node …
    once(host_memory_allocator_extension):               // built LAST → becomes chain head
        CreateHostMemoryAllocatorExtension(&host_memory_allocator_extension,
                                           &multi_slice_extension);

    // --- guard 17: materialize the 140-slot table ---
    once(pjrt_api):
        CreatePjrtApi(&pjrt_api,                          // 0x227BA840 (.lbss)
                      PJRT_Client_Create,                 // a2 → slot 15
                      PJRT_ExecuteContext_Create,         // a3 → slot 103
                      PJRT_TopologyDescription_Create,    // a4 → slot 87
                      PJRT_Plugin_Initialize,             // a5 → slot 8
                      &host_memory_allocator_extension,   // a6 → slot 1 (extension_start)
                      PJRT_Plugin_Attributes_Xla);        // a7 → slot 9

    return &pjrt_api;                                     // 0x227BA840 — unconditional
```

The 16 extension builders and their exact construction order, `next`-linking, and creator addresses are owned by [../pjrt/extension-chain.md](../pjrt/extension-chain.md); this page reproduces only the first and last to fix the shape and the seed/head relationship. The point that belongs here is the *control structure*: the return is unconditional and independent of which guards ran this call. On the first call all 17 blocks execute; on every later call the guard-byte fast path (`!(_BYTE)guard` is false) skips every block and the function falls straight to `return &pjrt_api`.

> **QUIRK —** the table constructor `CreatePjrtApi` is gated by the *same kind* of guard as the 16 extension nodes — it is the 17th `__cxa_guard` block, sharing the `pjrt_api` guard byte, not a separate mechanism. So "build the extensions" and "build the table" are seventeen peers in one function, not two phases. A reimplementer who builds the table eagerly (at `dlopen`, or outside a guard) loses the lazy-on-first-call and concurrent-serialization semantics the framework relies on.

> **GOTCHA —** the guard variables are libtpu's **own** libc++abi `__cxa_guard_acquire/release/abort @ 0x213e9ac0 / 0x213e9be0 / 0x213e9c20`, not glibc's. They are 17 distinct guard bytes in `.bss` (e.g. the `raw_buffer_extension` guard `@ 0x224C3980` region). Concurrent first-callers serialize through Itanium-ABI guard semantics: the loser blocks until the winner releases, then sees the satisfied byte and skips. After the one-shot the bytes stay set for process lifetime and readers take no lock.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `pjrt::tpu_plugin::GetTpuPjrtApi` | `0xe6aa440` | 1336 B | The 17-guard lazy builder | CONFIRMED |
| `pjrt::CreatePjrtApi` | `0xf874160` | 1872 B | 17th-guard slot-fill constructor | CONFIRMED |
| `__cxa_guard_acquire` | `0x213e9ac0` | — | libtpu's own one-shot acquire | CONFIRMED |
| `__cxa_guard_release` | `0x213e9be0` | — | libtpu's own one-shot release | CONFIRMED |
| `__cxa_guard_abort` | `0x213e9c20` | — | libtpu's own one-shot abort | CONFIRMED |

### Considerations

The builder runs only the PJRT-table and extension-chain construction. It does **not** initialize the TPU driver, scan silicon, or run the GoogleInitializer module DAG — those happen later, when the framework calls `PJRT_Plugin_Initialize` (slot 8) and `PJRT_Client_Create` (slot 15). At the moment `GetTpuPjrtApi` returns, the table is populated but the hardware is untouched. That separation is what lets a framework inspect `struct_size`, the version, and the extension chain (pure metadata) before committing to driver bring-up. The deep init path is owned by [module-init-plugin-discovery.md](module-init-plugin-discovery.md).

---

## The `pjrt_api` Singleton

### Purpose

`pjrt_api` is the one and only `PJRT_Api` instance the process ever produces. It is a function-local static of `GetTpuPjrtApi`, which is why its construction is guarded and its lifetime is the whole process. Understanding its storage is essential for a reimplementer: there is no static table image to copy out of the binary, so the slot values must be reconstructed from `CreatePjrtApi`'s body, not read from a section dump.

### Storage and Layout

```text
.lbss (section [47], NOBITS — no file bytes)
  0x227BA840 ┌─────────────────────────────────────────────┐
             │ GetTpuPjrtApi()::pjrt_api  (1120 B = 140×8)  │
             │   slot 0  +0x000  struct_size       = 1120   │  written by CreatePjrtApi
             │   slot 1  +0x008  extension_start    → chain  │  (guard 17), zero at load
             │   slots 2..4      pjrt_api_version   {0,103}  │
             │   slots 5..139    135 fn-ptrs                 │
             └─────────────────────────────────────────────┘
```

`.lbss` is the *large* BSS — NOBITS, occupies no file space, zero-filled by the loader. At `dlopen` the entire 1120-byte slab is zero. It stays zero until the first `GetPjrtApi` call runs guard 17 and `CreatePjrtApi` writes every slot. The header writes are byte-confirmed against `CreatePjrtApi @ 0xf874160`: `*a1 = 1120; a1[1] = chain_head; a1[2] = 24; a1[3] = 0; a1[4] = 0x6700000000`.

| Field | Value | Source | Confidence |
|---|---|---|---|
| Storage VA | `0x227BA840` | `.lbss` static, `mov rbx, offset` in builder | CONFIRMED |
| Section | `.lbss` [47], NOBITS | section table | CONFIRMED |
| Size | 1120 B (= 140 × 8) | `*a1 = 1120` in `CreatePjrtApi` | CONFIRMED |
| Value at load | all zero | NOBITS, zero-filled | CONFIRMED |
| Written by | `CreatePjrtApi @ 0xf874160` (guard 17) | builder call site | CONFIRMED |
| Lifetime | process (leaked-on-exit) | function-local static, no atexit dtor | CONFIRMED |

> **NOTE —** the slot-by-slot reconstruction — which of the 135 fn-ptrs map to which `pjrt::PJRT_*` wrapper — lives on [../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md). What this page fixes is *where the table is and how it comes to exist*: a zero `.lbss` slab, written once under guard 17. A static analyst who greps for a populated `PJRT_Api` in `PROGBITS` finds nothing; the table is a runtime artifact.

> **QUIRK —** the singleton is never freed. There is no `atexit`/`__cxa_thread_atexit` registration for `pjrt_api` or the 16 `.bss` extension nodes — they are the normal leaked Meyers-singleton lifetime for a plugin `.so`. The process's `FINI_ARRAY` tears down only a trivial `__do_fini` stub and BoringSSL per-thread RNG state, not the PJRT table. Clients, executables, and buffers are torn down through their explicit `PJRT_*_Destroy` C-API calls, not through the table's lifetime.

---

## The `tpu_plugin` Object

### Purpose

`pjrt::tpu_plugin` is the namespace that owns the TPU specialization of an otherwise-generic XLA PJRT table. Of the 140 slots, `CreatePjrtApi` hardcodes 130 to compile-fixed `pjrt::PJRT_*` wrappers (shared with the generic XLA layer) and takes only **five** function pointers from its caller. Four of those five are `pjrt::tpu_plugin` members — the genuine TPU hooks; the fifth is the generic XLA attributes implementation. Knowing which five slots are caller-supplied tells a reimplementer exactly where the TPU backend attaches to the generic table.

### The Injected Slots

The `CreatePjrtApi` argument-to-slot mapping is byte-confirmed: in `GetTpuPjrtApi` the call passes `(Client_Create, ExecuteContext_Create, TopologyDescription_Create, Plugin_Initialize, &host_memory_allocator_extension, Plugin_Attributes_Xla)`; in `CreatePjrtApi` those land as `a1[15]=a2`, `a1[103]=a3`, `a1[87]=a4`, `a1[8]=a5`, `a1[1]=a6`, `a1[9]=a7`. The mangled symbol of `CreatePjrtApi` encodes this exact parameter order (`Client_Create_Args`, `ExecuteContext_Create_Args`, `TopologyDescription_Create_Args`, `Plugin_Initialize_Args`, `PJRT_Extension_Base*`, `Plugin_Attributes_Args`), corroborating the register-to-slot trace.

| Slot | Field | Implementation | Address | Namespace | Confidence |
|---|---|---|---|---|---|
| 8 | `PJRT_Plugin_Initialize` | `tpu_plugin::PJRT_Plugin_Initialize` | `0xe6a9d00` | `pjrt::tpu_plugin` (TPU) | CONFIRMED |
| 9 | `PJRT_Plugin_Attributes` | `pjrt::PJRT_Plugin_Attributes_Xla` | `0xf85f080` | `pjrt` (generic XLA) | CONFIRMED |
| 15 | `PJRT_Client_Create` | `tpu_plugin::PJRT_Client_Create` | `0xe6a8840` | `pjrt::tpu_plugin` (TPU) | CONFIRMED |
| 87 | `PJRT_TopologyDescription_Create` | `tpu_plugin::PJRT_TopologyDescription_Create` | `0xe6a9b20` | `pjrt::tpu_plugin` (TPU) | CONFIRMED |
| 103 | `PJRT_ExecuteContext_Create` | `tpu_plugin::PJRT_ExecuteContext_Create` | `0xe6a9a80` | `pjrt::tpu_plugin` (TPU) | CONFIRMED |

Slot 1 (`extension_start`) is also caller-supplied — argument `a6`, the chain head `&host_memory_allocator_extension @ 0x224C3F68` — but it is a data pointer, not a `tpu_plugin` function. The remaining 130 function-pointer slots are `lea`-loaded constants in `CreatePjrtApi`'s body: compile-fixed `pjrt::PJRT_*` wrappers (e.g. `a1[5]=PJRT_Error_Destroy`, `a1[16]=PJRT_Client_Destroy`), shared with the generic XLA PJRT layer and not TPU-specialized.

> **NOTE —** slot 9 is the deliberate odd one out. Although it is an *injected* argument (`a7`), the value `CreatePjrtApi`'s caller passes is `pjrt::PJRT_Plugin_Attributes_Xla @ 0xf85f080` — the generic XLA attributes implementation (it advertises `xla_version`, `supported_devices`, serialization metadata), **not** a `tpu_plugin` override. So of the five caller-supplied slots, only four (8/15/87/103) are genuinely TPU-specific. A reimplementer must inject the TPU `Client_Create`/`Plugin_Initialize`/`TopologyDescription_Create`/`ExecuteContext_Create` but may reuse the stock XLA attributes function.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `pjrt::tpu_plugin::PJRT_Client_Create` | `0xe6a8840` | Silicon scan + live client construction (slot 15) | CONFIRMED |
| `pjrt::tpu_plugin::PJRT_Plugin_Initialize` | `0xe6a9d00` | One-time TPU driver bring-up (slot 8) | CONFIRMED |
| `pjrt::tpu_plugin::PJRT_TopologyDescription_Create` | `0xe6a9b20` | AOT pod topology, no client (slot 87) | CONFIRMED |
| `pjrt::tpu_plugin::PJRT_ExecuteContext_Create` | `0xe6a9a80` | Per-execution context (slot 103) | CONFIRMED |
| `pjrt::PJRT_Plugin_Attributes_Xla` | `0xf85f080` | Generic XLA attribute table (slot 9) | CONFIRMED |

### Considerations

The four TPU slots are bound here but not *run* here. `PJRT_Plugin_Initialize` is the framework's first deep call after discovery — it acquires the cross-process TPU lock, reads `LIBTPU_INIT_ARGS`, and runs the GoogleInitializer module DAG that registers the per-`TpuVersion` HAL factories. `PJRT_Client_Create` is where the silicon scan actually happens and a live `xla::PjRtClient` is built on top of the StreamExecutor `TpuPlatform`. Both are owned by [module-init-plugin-discovery.md](module-init-plugin-discovery.md); this page is concerned only with the fact that `GetTpuPjrtApi` wires their addresses into slots 8 and 15 via `CreatePjrtApi`.

---

## Related Components

| Component | Relationship |
|---|---|
| `GetPjrtApi @ 0xe6a83a0` | The exported `jmp` thunk; the only PJRT entry symbol |
| `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` | The 17-guard lazy builder it forwards to |
| `pjrt::CreatePjrtApi @ 0xf874160` | The 17th-guard constructor that fills the 140 slots |
| `GetTpuPjrtApi()::pjrt_api @ 0x227BA840` | The `.lbss` singleton the builder returns |
| `pjrt::tpu_plugin` object (5 injected slots) | The TPU specialization wired into slots 8/9/15/87/103 |
| 16-node `__cxa_guard` extension chain | The guards 1..16 in the same builder; owned by `../pjrt/extension-chain.md` |
| `Tpu*_*` C-ABI (~217 exports) | The legacy StreamExecutor surface that shares the binary but is not reached through PJRT |

## Cross-References

- [overview.md](overview.md) — the lifecycle section map this page sits under
- [module-init-plugin-discovery.md](module-init-plugin-discovery.md) — the dlopen-time static-init landscape and the `PJRT_Plugin_Initialize` → `Client_Create` driver bring-up the slots reach into
- [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md) — the legacy `Tpu*_*` StreamExecutor C-ABI bootstrap that shares the binary
- [../pjrt/overview.md](../pjrt/overview.md) — the PJRT C-ABI map: handshake, struct shape, and region index for the table this builder returns
- [../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md) — the full 140-slot field-by-field reconstruction of the `pjrt_api` singleton
- [../pjrt/extension-chain.md](../pjrt/extension-chain.md) — the 16 `__cxa_guard` extension builders (guards 1..16) and the newest-first chain they assemble
