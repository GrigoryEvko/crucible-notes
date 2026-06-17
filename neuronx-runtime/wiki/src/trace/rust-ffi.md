# neuron_rustime: the cbindgen FFI Boundary

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, soname `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`, version namespace `NRT_2.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present and `.text`/`.rodata`/`.data.rel.ro` VMA equals file offset, so every `0x…` is both an analysis VMA and a file offset. The Rust crate `neuron_rustime` is statically linked from `rustc 1.91.1` (`/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/`), with `log 0.4.28`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — all 25 boundary symbols (12 `nrt_sys_trace_*` shims + 7 `nrt_interned_string_db_*` shims + `init_rust_logger` + 3 `NeuronLogger` trait methods + 5 reverse-FFI helpers) are each pinned to a `.symtab` symbol + `.text` address (`nm`-verified); the abort-on-unwind landing pads, the `GOT[0xc064b0] → panic_cannot_unwind` reloc, and the `(al=is_err, edx=status)` result ABI are cross-checked against objdump disassembly and the `.eh_frame` FDE/LSDA. The exact positional argument semantics of `new_event_with_seq` are `[MED]` (the unnamed slot between `track` and `nc_idx` is inferred from the Rust prologue, not from a named DWARF arg). · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

`libnrt.so` is one ELF holding two languages: a C/C++ KaenaRuntime core and a first-party Rust crate, `neuron_rustime`, compiled in and linked statically. Everything that crosses between them crosses through a single, disciplined seam — a band of cbindgen-generated `extern "C"` shims at `0x508040..0x50a000` that marshal C structs and pointers into the Rust modules `sys_trace` (the capture engine + serde read-out), `string_db` (the SipHash interner), and `logging` (the `log`-crate bridge). The shims are thin: each validates its C arguments, copies fields into a stack-resident Rust value, tail-calls one named `neuron_rustime::*` target, and re-packs the Rust `Result` back into a C status. The Rust engines themselves live higher in `.text` (`0x5aa3b0..0x5b25d0`) and are owned by sibling pages; this page owns the **boundary** — the export surface, the calling convention, the ownership contract, and what happens when a Rust panic reaches the edge.

The familiar reference frame is an **ordinary cbindgen C ABI over a `panic="unwind"` crate**, but with one decision a reimplementer must get right: the boundary is **abort-on-unwind, not `catch_unwind`**. There is no `std::panic::catch_unwind` on any shim. Instead, each `extern "C"` function carries the Rust-1.81+ auto-inserted C-unwind guard: its `.eh_frame` FDE has an LSDA whose landing pad — the instruction placed just past the shim's `ret` — is `call *GOT[0xc064b0]`, an `R_X86_64_RELATIVE` reloc to `core::panicking::panic_cannot_unwind` @`0x6b9e3`, which loads `"panic in a function that cannot unwind"` (`.rodata` @`0xa3019c`, 38 bytes) and tail-calls the nounwind abort. 27 such guards span the band. A panic inside any `neuron_rustime` impl therefore unwinds only as far as its shim and then **aborts the process**; it never propagates into a C/C++ frame, and — because `neuron_rustime` installs no custom panic hook — its message prints via `std::panicking::default_hook` to `stderr`, *not* through the `nlog` "RUST" sink ([runtime/logging](../runtime/logging.md)).

This page documents the seam to reimplementation accuracy: the **FFI-export table** for the whole `0x508040..0x50a000` band, with the address-adjacent plain-C functions (`nds_*`, `dx_ring_*`, `nlog_backtrace`) explicitly marked **not** part of the Rust FFI (§1); the **panic-containment model** — the abort-on-unwind guard, the LSDA landing pad, and the `GOT` reloc that targets `panic_cannot_unwind` (§2); the **logger bridge** wiring `init_rust_logger → NeuronLogger::{log,enabled,flush}` into `nlog` (§3); and the **GOT-reloc / cbindgen calling-convention mechanics** — the `(al, edx)` `Result` packing, the fallible-C-string ingress idiom, and the C-frees-Rust ownership contract (§4). The capture ring ([rust-capture](rust-capture.md)), the serde_json path ([rust-serde](rust-serde.md)), the interner internals ([interned-strings](../runtime/interned-strings.md)), and the `nlog` facility ([runtime/logging](../runtime/logging.md)) are boundaries — cross-linked, not re-derived.

For reimplementation, the contract is:

- **The export surface** — 12 `nrt_sys_trace_*` shims (`0x509650..0x509d08`), 7 `nrt_interned_string_db_*` shims (`0x508960..0x509650`), `init_rust_logger` (`0x508860`), 3 `NeuronLogger` trait methods, and 5 reverse-FFI `neuron_rustime::*` helpers (`0x5082c0..0x508690`) — every one pinned to an address and a named Rust target.
- **The calling convention** — unit-returning fallibles pack `Result<(),Status>` as `(al = is_err, edx = status_code)`; value-returning fallibles use an sret `(tag, payload)` out-param with `tag==1 == Err`. Scalars by value; opaque handles/configs by raw `*const`/`*mut` (read + copied into a stack Rust value, never owned by the shim).
- **The fallible-C-string ingress** — every `char*` is `non-NULL → strlen → CStr::to_str` UTF-8-validated *before* any Rust call; on NULL/invalid the shim returns `NRT_INVALID (2)` (or `0`) **without** entering Rust.
- **The C-frees-Rust ownership contract** — `fetch_events` / `get_entries` hand Rust-allocated buffers to C; they are reclaimable **only** through the matching free shim (`buffer_free_internal` / `free_entries`), which reconstructs `CString::from_raw` + `__rust_dealloc`. Mixing libc `free()` corrupts the heap.
- **The panic model** — abort-on-unwind, no `catch_unwind`. Each shim's LSDA landing pad is `call *GOT[0xc064b0]` → `panic_cannot_unwind` @`0x6b9e3` → nounwind abort. A panic crossing the boundary kills the process; it does not unwind into C and does not surface through `nlog`.

| | |
|---|---|
| **FFI band** | `0x508040..0x50a000` (cbindgen `extern "C"` shims + reverse helpers + `NeuronLogger` methods) |
| **Rust crate** | `neuron_rustime` (`rustc 1.91.1`); engines at `0x5aa3b0..0x5b25d0` (sibling pages) |
| **sys_trace shims** | 12 fns `0x509650..0x509d08` → `neuron_rustime::sys_trace::{capture,api}::*` |
| **string_db shims** | 7 fns `0x508960..0x509650` → `neuron_rustime::string_db::*` |
| **Logger bridge** | `init_rust_logger` @`0x508860`; `NeuronLogger::{log @0x50a4a0, enabled @0x50a760, flush @0x508820}` |
| **Reverse helpers** | `0x5082c0..0x508690` — Rust fns called *from* C++ that call *back into* C |
| **Panic model** | **abort-on-unwind** — 27 LSDA guards → `call *GOT[0xc064b0]` → `panic_cannot_unwind` @`0x6b9e3` |
| **Panic target** | `core::panicking::panic_cannot_unwind` @`0x6b9e3`; msg `"panic in a function that cannot unwind"` @`0xa3019c` (38 B) |
| **Result ABI** | unit fallible: `(al = is_err, edx = status)`; value fallible: sret `(tag, payload)`, `tag==1 == Err` |
| **Build profile** | `panic="unwind"` (`rust_eh_personality` @`0x565f30`); `NEEDED libgcc_s.so.1` — but unwinding never crosses the boundary |
| **NOT Rust FFI** | `nds_*` @`0x508040`, `dx_ring_*` @`0x508160`, `nlog_backtrace` @`0x508220` — address-adjacent plain C |

---

## 1. The FFI-Export Surface

### Purpose

The `0x508040..0x50a000` band is the entire C↔Rust boundary. Reading it correctly requires one act of discipline up front: the band is **not** wholly Rust FFI. Its first three function clusters (`0x508040..0x508280`) are plain-C functions from unrelated translation units that the linker happened to place adjacent to the cbindgen shims; they call no Rust and are owned by other subsystems. Everything from `0x5082c0` onward *is* the Rust seam. A reimplementer mapping the band by address alone, without DWARF TU attribution, will mis-classify those three clusters as part of the bridge.

### NOT Rust FFI — the address-adjacent plain-C functions

These three clusters sit at the low end of the band and are excluded from the Rust boundary. DWARF attributes each to a C source file in a non-Rust subsystem; objdump shows none of them references a `neuron_rustime::*` symbol.

| C symbol | Address | DWARF TU | Subsystem | Why it is NOT Rust FFI |
|---|---|---|---|---|
| `nds_*` | `0x508040` | `nds/neuron_ds_process.c` | Neuron DataStore | C shared-memory counter plane; calls into `nds`, not Rust |
| `dx_ring_*` | `0x508160` | `dx/ring.c` | DMA-exec ring | C ring-buffer helper; no Rust target |
| `nlog_backtrace` | `0x508220` | `nlog/backtrace.c` | nlog facility | C backtrace formatter ([runtime/logging](../runtime/logging.md)); not the Rust logger bridge |

> **GOTCHA —** `nlog_backtrace` @`0x508220` is the trap. It lives in the `nlog/` source tree, shares the `nlog` name prefix with the Rust *logger bridge* (§3), and sits 0x640 bytes before `init_rust_logger` @`0x508860` — but it is plain C and has nothing to do with the `NeuronLogger` trait. The logger bridge is `init_rust_logger` + the three `NeuronLogger::*` methods, *not* `nlog_backtrace`. A reimplementer must split the `nlog` name across two languages: `nlog_*` (C facility) and `<NeuronLogger as log::Log>::*` (Rust impl that calls *into* the C facility). `[HIGH]`

### The FFI-export table

Everything below is Rust FFI: an `extern "C"` C symbol (or a mangled `neuron_rustime::*` reverse helper) that bridges into the Rust crate. Each shim validates, marshals, and tail-calls one named target; the marshalling one-liners and the Rust-side engines are detailed in §3–§4 and the sibling pages.

**A. The logger bridge** (init + 3 trait methods) — wires `neuron_rustime::LOGGER` into the `log` crate and routes Rust diagnostics into C `nlog` (§3).

| C / Rust symbol | Address | Rust target / role | Conf |
|---|---|---|---|
| `init_rust_logger` | `0x508860` | `log::set_logger(LOGGER, &unk_BF9378)`; on Ok `MAX_LOG_LEVEL_FILTER=5`; on Err WARN `"logger already set"` @`0xa15360` | HIGH |
| `<NeuronLogger as log::Log>::log` | `0x50a4a0` | gate on `nlog_get_log_level("RUST")` → `format!` → `CString` → `nlog_write("RUST",…,"%s",msg)` — the Rust→C log sink | HIGH |
| `<NeuronLogger as log::Log>::enabled` | `0x50a760` | gate test against `nlog_get_log_level("RUST")` | HIGH |
| `<NeuronLogger as log::Log>::flush` | `0x508820` | no-op (`nlog` writes synchronously) | HIGH |

**B. The sys_trace cbindgen shims** (12 fns, `0x509650..0x509d08`) — marshal C structs/pointers into `neuron_rustime::sys_trace::{capture,api}::*`. The config surface, the 312-byte `nrt_sys_trace_config_t`, and the ring engine are owned by [rust-capture](rust-capture.md); the JSON read-out by [rust-serde](rust-serde.md).

| C symbol | Address | Rust target / role | Conf |
|---|---|---|---|
| `nrt_sys_trace_buffer_free_internal` | `0x509650` | `CString::from_raw` + `__rust_dealloc` — C-frees-Rust cleanup of `fetch_events` output | HIGH |
| `nrt_sys_trace_capture_close` | `0x509680` | `capture::capture_close`; `(al,edx)` status-if-err else 0 | HIGH |
| `nrt_sys_trace_capture_drain_events` | `0x5096a0` | `capture::drain_events(&ret2,…)`; sret `(tag,payload)`, `tag==1 == Err` | HIGH |
| `nrt_sys_trace_capture_intern_string` | `0x5096e0` | fallible-C-string ingress → `capture::intern_string`; 0 on null/bad-UTF8 | HIGH |
| `nrt_sys_trace_capture_start` | `0x509740` | marshal `nrt_sys_trace_config_t` → stack `Config` (clamp max-events, copy NC + event bitmaps) → `capture::capture_start` | HIGH |
| `nrt_sys_trace_capture_stop` | `0x509980` | `capture::capture_stop`; `(al,edx)` | HIGH |
| `nrt_sys_trace_fetch_events_internal` | `0x5099a0` | build `api::FetchOptions` → `api::fetch_events` → NUL-check → `(*out_buf, *out_len=len-1)`; WARN on bad args | HIGH |
| `nrt_sys_trace_get_combined_string_db` | `0x509c70` | `capture::combine_all_interned_strings`; `(al,edx)` | HIGH |
| `nrt_sys_trace_new_event` | `0x509c90` | `capture::new_event_with_seq(phase,type,&data,track,?,nc_idx,seq=0)` — primary hot path | HIGH |
| `nrt_sys_trace_new_event_with_seq` | `0x509cc0` | `capture::new_event_with_seq(…,a12)` — 12-arg sequenced variant | HIGH |
| `nrt_sys_trace_ring_size` | `0x509cf0` | `capture::ring_size` (tail-call passthrough) | HIGH |
| `nrt_sys_trace_total_event_count` | `0x509d00` | `capture::total_event_count` (tail-call; band end `0x509d08`) | HIGH |

**C. The string_db cbindgen shims** (7 fns, `0x508960..0x509650`) — `extern "C"` wrappers over `neuron_rustime::string_db::*`. The shard topology, the SipHash id function, and the SwissTable are owned by [interned-strings](../runtime/interned-strings.md).

| C symbol | Address | Rust target / role | Conf |
|---|---|---|---|
| `nrt_interned_string_db_combine_shards` | `0x508960` | iterate src buckets → `InternedDataShard::register_entry` into dst; no in-binary callers (external surface) | HIGH |
| `nrt_interned_string_db_free_entries` | `0x508a10` | `__rust_dealloc(ids)` + per-name `CString::from_raw` + `__rust_dealloc(names)`; WARN on null args | HIGH |
| `nrt_interned_string_db_get_entries` | `0x508ba0` | walk shard → leak `Vec<u64>` ids + `Vec<CString>` names to C; WARN `"Invalid arguments"` on any NULL | HIGH |
| `nrt_interned_string_db_get_id` | `0x509250` | fallible-C-string ingress → `string_db::get_id` (pure SipHash); 0 on null/bad-UTF8 | HIGH |
| `nrt_interned_string_db_register_entry` | `0x5092a0` | `(name,id,shard)`; validate → `InternedDataShard::register_entry`; WARN on null/bad-UTF8 | HIGH |
| `nrt_interned_string_db_shard_alloc` | `0x509490` | TLS shard-id counter + `__rust_alloc` → boxed `InternedDataShard` | HIGH |
| `nrt_interned_string_db_shard_free` | `0x509540` | walk SwissTable, dealloc each name buffer + table + box | HIGH |

> **NOTE —** `nrt_interned_string_db_*` is one of *two* "interned string" subsystems sharing a name prefix. These seven shims call the Rust `string_db` crate. The mangled C++ `_Z37…get_event_name` / phase / cc-algo / mem-usage lookups in `nrt_interned_string_db.cpp` are a **separate** static C++ event-name table with **no** Rust call ([rust-serde §4](rust-serde.md) and [event-taxonomy](event-taxonomy.md) own that side). A reimplementer must not collapse the two: one is a `hashbrown` SwissTable keyed on SipHash ids, the other is a compile-time string array. `[HIGH]`

**D. The reverse-FFI helpers** (5 mangled `neuron_rustime::*` fns, `0x5082c0..0x508690`) — Rust functions called *from* C++ that themselves call *back into* C, producing Rust→C→Rust round trips on the error/logging path.

| Rust symbol | Address | Role | Conf |
|---|---|---|---|
| `neuron_rustime::cc_algo_name` | `0x5082c0` | collectives algorithm name (used by C++ `_Z39…get_cc_algo_name`; feeds the serde `algorithm` key, [rust-serde §3](rust-serde.md)) | HIGH |
| `neuron_rustime::visible_vnc_count` | `0x508390` | → C `nrt_get_visible_vnc_count(&n)`; WARN `"nrt_get_visible_vnc_count() failed"` on err | HIGH |
| `neuron_rustime::dma_mem_usage_name` | `0x508480` | DMA `usage_type` name (used by C++ `get_mem_usage_type`; feeds serde `usage_type` key) | HIGH |
| `neuron_rustime::visible_virtual_cores` | `0x508550` | → C `nrt_gconf()`; asserts/panics `"nrt_gconf() returned null"` on null | HIGH |
| `neuron_rustime::should_include_default_algo` | `0x508630` | CcExec `algorithm`-key gate (serde) | HIGH |

> **QUIRK —** the reverse helpers invert the usual FFI direction. `cc_algo_name`/`dma_mem_usage_name`/`should_include_default_algo` are Rust functions *called from* the C++ trace path; `visible_vnc_count`/`visible_virtual_cores` are Rust functions that *call back into* C (`nrt_get_visible_vnc_count`, `nrt_gconf`). So a single `fetch_events` serialize can cross the boundary three times — C++ → Rust serde → (reverse helper) C `nrt_gconf` → back into Rust. The panic guard (§2) applies to the reverse helpers too: `visible_virtual_cores`'s `"nrt_gconf() returned null"` assertion is a panic that aborts at the boundary rather than unwinding into the C++ caller. `[HIGH]`

---

## 2. The Panic-Containment Model

### Purpose

A `panic="unwind"` crate inside a C ELF is a liability: if a Rust panic unwinds through a `extern "C"` frame into C/C++ code that has no `.eh_frame` cleanup, the result is undefined behavior. `neuron_rustime` resolves this not with `catch_unwind` (which would convert the panic to a recoverable `Err`) but with the stricter Rust-1.81+ default: every `extern "C"` shim is a **C-unwind boundary** that *aborts* if a panic reaches it. This is the single most important boundary fact for a reimplementer — get it wrong and a panic that should kill the process instead silently corrupts a C frame, or a panic that should be contained instead leaks an exception into libgcc's unwinder.

### The build profile

The crate is built `panic="unwind"`, and the unwinding machinery is fully present: `rust_eh_personality` @`0x565f30`, `std::panicking::catch_unwind::cleanup` @`0x65f1d`, `resume_unwind` @`0x65f60`, `__rustc::rust_panic` @`0x64790`, `__rust_foreign_exception` @`0x50b930`; the ELF lists `NEEDED libgcc_s.so.1`, and the `.eh_frame` CIE @`0x76d24` carries the `"zPLR"` augmentation with a personality routine. Unwinding works *inside* Rust — it just never crosses a shim. There is no `catch_unwind` on any FFI shim (none observed across the band).

### The abort-on-unwind guard

Each shim's `.eh_frame` FDE carries an LSDA. The LSDA's single landing pad is the instruction placed immediately **after** the shim's `ret` — a `call *GOT[0xc064b0]`. That GOT slot is an `R_X86_64_RELATIVE` reloc resolving to `core::panicking::panic_cannot_unwind` @`0x6b9e3`, which loads the message `"panic in a function that cannot unwind"` (`.rodata` @`0xa3019c`, 38 bytes) and tail-calls the nounwind abort (`panic_nounwind_nobacktrace`, `GOT[0xc05930]`). **27 such guards** exist across `0x508000..0x50a000` — one per shim that can re-enter Rust. The landing-pad addresses are byte-confirmed for the representative shims:

| Shim | `ret` neighborhood | Landing pad (`call *GOT[0xc064b0]`) |
|---|---|---|
| `capture_close` | `0x509680` | `0x509691` |
| `capture_start` | `0x509740` | `0x509971` |
| `new_event` | `0x509c90` | `0x509cb4` |
| `register_entry` | `0x5092a0` | `0x509299` |
| `get_entries` | `0x508ba0` | `0x508b7b` |
| `free_entries` | `0x508a10` | `0x508a02` |

### Algorithm — the containment mechanism

```c
// Models the cbindgen extern "C" shim + its C-unwind abort guard.
// The guard is NOT in the source — it is the Rust-1.81+ auto-inserted
// "function that cannot unwind" landing pad, emitted by the FDE/LSDA.
function nrt_sys_trace_capture_close(handle):           // 0x509680, extern "C"
    // --- normal path ---
    r = neuron_rustime::sys_trace::capture::capture_close(handle)   // CALL into Rust
    if r.is_err:                                        // (al = is_err, edx = status) — §4
        return r.status                                 // edx
    return 0
    // ret @0x5096…  ── on the normal path, control returns to the C caller here

    // --- the LSDA landing pad (just past `ret`, @0x509691) ---
LANDING_PAD:                                            // reached ONLY by an in-flight unwind
    // a panic inside capture_close started unwinding; the personality routine
    // (rust_eh_personality @0x565f30) consults this FDE's LSDA and lands here:
    call *GOT[0xc064b0]                                 // R_X86_64_RELATIVE -> 0x6b9e3
    //   == core::panicking::panic_cannot_unwind:
    //        load "panic in a function that cannot unwind" (0xa3019c, 38 B)
    //        tail-call panic_nounwind_nobacktrace (GOT[0xc05930]) -> abort()
    //   <unreachable — process is dead>
```

> **QUIRK —** what happens if a Rust panic reaches the C boundary: the process **aborts**, immediately and unconditionally, and the panic message does **not** go through `nlog`. The unwinder, finding the shim's `cannot-unwind` LSDA, runs `panic_cannot_unwind` → nounwind abort *before* the panic can cross into the C frame — so C/C++ never sees a foreign exception, and the original panic's `default_hook` text (printed to `stderr`, since `neuron_rustime` installs no `set_hook`) is the *only* diagnostic. A reimplementer who expects a panic in, say, `fetch_events`'s interior-NUL `unwrap_failed` path (the `"json contained nul byte"` @`0xa153e6` / `"called Result::unwrap() on an Err value"` @`0xa15424` surface) to return an `NRT_STATUS` is wrong — it aborts. The defensive NUL checks are *normally unreachable* invariants, not recoverable error paths. `[HIGH]`

> **NOTE —** the `panic_cannot_unwind` text @`0xa3019c` is distinct from the panic *content*. `panic_cannot_unwind` is the abort site's own message ("a function that cannot unwind panicked"); the original cause (e.g. `"nrt_gconf() returned null"`, `"msg contained nul byte"` @`0xa15445`, an out-of-range index) is formatted and printed by `default_hook` on the *inner* panic, before the unwind reaches the guard. Both land on `stderr`; neither reaches syslog, `dmesg`, or the `error_log` ring ([runtime/logging §5](../runtime/logging.md)). `[HIGH]`

### Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `core::panicking::panic_cannot_unwind` | `0x6b9e3` | abort site: load `"…cannot unwind"` → nounwind abort; LSDA target of every shim | HIGH |
| `rust_eh_personality` | `0x565f30` | personality routine; consults FDE LSDA to land on the abort guard | HIGH |
| `std::panicking::catch_unwind::cleanup` | `0x65f1d` | unwind cleanup — present (unwind build) but **never** on the FFI surface | HIGH |
| `std::panicking::try::do_call` / `resume_unwind` | `0x65f60` | resume path — Rust-internal only | HIGH |
| `__rustc::rust_panic` | `0x64790` | panic entry; raises the Rust exception that the guard then aborts | HIGH |
| `__rust_foreign_exception` | `0x50b930` | foreign-exception trap (a C++ exception entering Rust) | HIGH |
| `(GOT slot)` | `0xc064b0` | `R_X86_64_RELATIVE` → `0x6b9e3`; the shims' indirect call target | HIGH |
| `(GOT slot)` | `0xc05930` | `panic_nounwind_nobacktrace` → `abort` | HIGH |

---

## 3. The Logger Bridge

### Purpose

`neuron_rustime` has **no log writer of its own** — no `stdout`/`stderr` sink, no file. `init_rust_logger` @`0x508860` installs the static `neuron_rustime::LOGGER` into the `log` crate as the global logger, and the `NeuronLogger` `log::Log` impl forwards every Rust `info!`/`warn!`/`error!` straight back into the C `nlog_write("RUST", …)` sink, gated by `nlog_get_log_level("RUST")`. This makes the C `nlog` facility the single terminal for both languages. This page owns the **FFI wiring** of that bridge — the `set_logger` call, the trait-method dispatch, and the Rust→C call mechanics; the C `nlog` facility on the far side (the 12×2 level matrix, the five-sink fan-out, the coalescing ring) is owned by [runtime/logging](../runtime/logging.md).

### Entry Point

```text
nrt_init (0x94e90, public API)
  └─ nlog_init (0x224a30, C)                       [call site 0x95d91]
       └─ init_rust_logger (0x508860)              [call site 0x224a52]
            ├─ log::set_logger(neuron_rustime::LOGGER, &unk_BF9378)
            ├─ on Ok:  log::MAX_LOG_LEVEL_FILTER (0xcb0788) = 5   (Trace -> never pre-filter)
            └─ on Err: WARN "logger already set" (0xa15360)

<any neuron_rustime fn>::{info!/warn!/error!/…}
  └─ log::__private_api::GlobalLogger::log
       └─ <NeuronLogger as log::Log>::log (0x50a4a0)
            ├─ nlog_get_log_level("RUST", &lvl)               [C gate]
            ├─ alloc::fmt::format::format_inner -> CString     [format the record args]
            └─ nlog_write("RUST", target, level, /*coaleced*/1, /*api*/0, "%s", msg)  [C sink]
```

The bridge is live before any Rust code runs: `init_rust_logger` is called from `nlog_init` (← `nrt_init`), so the logger is registered during runtime bootstrap.

### The level mapping

`log::Level {Error 1, Warn 2, Info 3, Debug 4, Trace 5}` coincides numerically with `nrt_log_level_t {ERROR 1 … TRACE 5}` (DWARF enum, verified), so the bridge compares the record level directly against the C level — no translation table. `init_rust_logger` sets the `log` crate's static `MAX_LOG_LEVEL_FILTER` @`0xcb0788` to `5` (Trace) so the crate's compile-time fast-path filter never drops a record; **all** filtering is delegated to `nlog` per-call via `nlog_get_log_level("RUST")`.

### Algorithm — the bridge

```c
// init_rust_logger @0x508860
function init_rust_logger():                                     // -> ptr
    if log::set_logger(neuron_rustime::LOGGER, &unk_BF9378) == Err:   // already set
        log!(warn, "logger already set")                         // 0xa15360, via the crate's own logger
    else:
        log::MAX_LOG_LEVEL_FILTER = 5                            // 0xcb0788: Trace -> never pre-filter

// <neuron_rustime::logging::NeuronLogger as log::Log>::log @0x50a4a0
function NeuronLogger_log(record):
    lvl = DISABLED
    nlog_get_log_level("RUST", &lvl)                             // C gate, "RUST" slot
    if (lvl - 1) < 4 && record.level > lvl:                      // finite gate (ERROR..DEBUG) -> drop above it
        return                                                   //   TRACE(5)/DISABLED(0) bypass this branch
    msg  = format!(record.args)                                  // alloc::fmt::format -> String
    cstr = CString::new(msg)                                     // panics "msg contained nul byte" (0xa15445) on interior NUL
    nlog_write("RUST", record.target, record.level, /*coaleced*/1, /*api*/0, "%s", cstr.ptr)
    drop(cstr); drop(msg)                                        // __rust_dealloc both

// <… NeuronLogger …>::enabled @0x50a760
function NeuronLogger_enabled(metadata):                         // -> bool
    lvl = DISABLED
    nlog_get_log_level("RUST", &lvl)
    return (lvl - 1 >= 4) || (lvl >= metadata.level)             // TRACE/unfiltered, or level permits

// <… NeuronLogger …>::flush @0x508820
function NeuronLogger_flush():
    return                                                       // no-op: nlog writes synchronously
```

> **NOTE —** `NeuronLogger::log` calls `nlog_write` with `coaleced_msg = 1, api_level = 0`, so Rust lines take the **compact** coalesced prefix (`"RUST:<target> "`) and join the per-thread coalescing ring like any non-API line — they are flushed together with the C lines on the next API-level ERROR, not emitted standalone. The record's `target` (the Rust module path, e.g. `neuron_rustime::sys_trace::capture`) becomes the `nlog` header's `func` field. The ring mechanics are owned by [runtime/logging §3](../runtime/logging.md). `[HIGH]`

> **QUIRK —** the logger bridge carries only `log!`-**macro** diagnostics — never panic text. A Rust panic does not reach `NeuronLogger::log`; it aborts at the FFI boundary (§2) and its message goes to `stderr` via `default_hook`, bypassing `nlog` entirely. So the "RUST" submodule's `nlog` stream shows the crate's *intentional* diagnostics but is silent on the *fatal* ones. A reimplementer wiring a Rust crate into a C log facility must wire the panic hook separately if panic text is wanted in the unified log; `neuron_rustime` does not. `[HIGH]`

---

## 4. GOT-Reloc and cbindgen Calling-Convention Mechanics

### Purpose

The shims are uniform: a fixed prologue that validates and marshals C arguments, one tail-call into Rust, and a fixed epilogue that re-packs the Rust return into a C status. A reimplementer who reproduces these four mechanics — argument marshalling, the `Result` ABI, the fallible-C-string ingress, and the C-frees-Rust ownership contract — reproduces the entire boundary discipline; the per-shim differences are just *which* Rust target is called.

### Argument marshalling (C → Rust)

Opaque handles and config structs are passed as raw `*const`/`*mut` (e.g. `nrt_sys_trace_config_t*`, `shard**`). The shim **never takes ownership** of a caller struct — it *reads* fields and copies them into a stack-resident Rust value. `capture_start` is the canonical case: it copies the 256-byte NC bitmap (`cfg+8`), the event-type bitmaps (`cfg+264`/`+280`/`+294`), and the clamped `max_events` into a stack `Config`, then passes `&Config` by reference. Scalars (`nc_idx`, `event_type`, `phase`, `max_events`) pass by value; bounds are enforced Rust-side (`new_event_with_seq` bounds-checks `nc_idx < 0x100`).

### The Result ABI

Unit-returning fallibles (`capture_start/stop/close`, `get_combined_string_db`, `register_entry`) return Rust `Result<(),Status>` ABI-packed into two registers: `al` = is-err flag, `edx` = status code. The shim epilogue is a fixed idiom:

```asm
; the unit-fallible Result epilogue (verified at capture_start 0x50995d, capture_close 0x509686)
xor    ecx, ecx            ; zero = success status
test   al, 1              ; is_err flag in al bit 0
cmovne ecx, edx           ; if err, take the Rust status from edx
mov    eax, ecx           ; return status (0 on Ok)
ret
```

Value-returning fallibles (`drain_events`) use an sret out-param `(tag, payload)`; `tag==1 == Err`, on which the shim returns `payload` as the status, else writes `payload` to the caller's out-pointer and returns 0.

> **CORRECTION (W2-RUST-FFI) —** the IDA decompiles of the unit-fallible epilogue render as `if (v1 & 1) return v2; return 0;` with `v2` appearing **uninitialized**. That is a decompiler artifact, not a real bug: `edx` *is* set by the Rust ABI return (the `cmovne ecx, edx` reads it), but IDA does not model the cross-call register convention and shows `v2` as a dangling read. The status returned on the error path is the Rust `Status`, byte-confirmed in the raw disassembly at `0x50995d` and `0x509686`. A reimplementer reading the decompile must not "fix" the uninitialized read. `[HIGH]`

### The fallible-C-string ingress

Every `char*` argument follows one idiom: validate non-NULL, then UTF-8-validate via `CStr::to_str`, *before* any Rust call. On NULL or invalid UTF-8 the shim returns `NRT_INVALID (2)` (or `0` for the pure-value shims `intern_string`/`get_id`) **without entering Rust** — and, for the logging shims, emits a WARN.

```c
// The canonical fallible-C-string ingress, shared by intern_string,
// get_id, register_entry, get_entries.  Models e.g. register_entry @0x5092a0.
function string_ingress(name, shard):                  // -> NRT_STATUS
    if name == NULL || shard == NULL:
        nlog WARN "invalid name or shard. Cannot register interned string."  // 0xa15383
        return NRT_INVALID                             // 2 — NO Rust call
    str = CStr::to_str(name)                           // core::ffi::c_str::CStr::to_str (UTF-8)
    if str is Err:
        nlog WARN "invalid name. Cannot register interned string."           // 0xa153ba
        return NRT_INVALID                             // 2 — NO Rust call
    return InternedDataShard::register_entry(shard, id, str.ptr, str.len)    // NOW enter Rust
```

### The C-frees-Rust ownership contract

`fetch_events` and `get_entries` return Rust-allocated memory to C. The contract is **C-frees-Rust**: the caller must call the matching free shim, which reconstructs the Rust allocation and drops it on the *Rust* allocator. Mixing libc `free()` here corrupts the heap, because the buffer came from `__rust_alloc`, not `malloc`.

| Producer shim | Returns | Free shim | Reclaims via |
|---|---|---|---|
| `nrt_sys_trace_fetch_events_internal` @`0x5099a0` | `CString` ptr + `len-1` | `nrt_sys_trace_buffer_free_internal` @`0x509650` | `CString::from_raw` + `__rust_dealloc` |
| `nrt_interned_string_db_get_entries` @`0x508ba0` | `u64 ids[]` + `char* names[]` + count | `nrt_interned_string_db_free_entries` @`0x508a10` | per-name `CString::from_raw` + `__rust_dealloc` ×3 |

`shard_alloc`/`shard_free` are the matching box lifecycle: `shard_alloc` is `Box::into_raw` → C; `shard_free` is `Box::from_raw` ← C.

> **GOTCHA —** the produced buffers carry a defensive interior-NUL scan that **panics** (`core::result::unwrap_failed` on `CString::new`) if a NUL is found — and that panic, per §2, *aborts the process*. For `fetch_events` the surface is `"json contained nul byte"` @`0xa153e6`; for `get_entries` it is the per-name `CString::new`. These are normally-unreachable invariants (JSON and interned names are NUL-free by construction), so a reimplementer should treat them as `assert`-grade, not as a recoverable error class — but must still emit the scan, because a malformed name reaching the boundary must abort rather than hand C a truncated string. `[HIGH]`

### Ownership / lifetime summary

| Subsystem | State location | Handoff |
|---|---|---|
| `sys_trace` capture | process **global** (`GLOBAL_LOCK` @`0xcb0898`, `G_STATE` @`0xcb08b0`, `CONTEXTS` @`0xc0d300`) | C holds *no* handle; shims operate on the singleton behind `GLOBAL_LOCK` ([rust-capture](rust-capture.md)) |
| `string_db` shard | **caller-owned** | `shard_alloc` `Box::into_raw` → C; threaded through `register_entry`/`get_entries`/`combine_shards`/`shard_free` ([interned-strings](../runtime/interned-strings.md)) |
| `string_db` id | **stateless** | `get_id` is a pure SipHash; no shard, no lock |

---

## Related Components

| Name | Relationship |
|---|---|
| `neuron_rustime::sys_trace::capture::*` (`0x5af600..0x5b25d0`) | the ring-buffer engine behind the sys_trace shims — [rust-capture](rust-capture.md) |
| `neuron_rustime::sys_trace::api::fetch_events` @`0x5aa3b0` | the serde_json read-out the `fetch_events_internal` shim drives — [rust-serde](rust-serde.md) |
| `neuron_rustime::string_db::*` (`0x5b4410..0x5b4990`) | the SipHash interner + SwissTable behind the string_db shims — [interned-strings](../runtime/interned-strings.md) |
| `nlog_*` C facility (`nlog_write`/`nlog_get_log_level`) | the C log sink the `NeuronLogger` bridge calls into — [runtime/logging](../runtime/logging.md) |
| `core::panicking::panic_cannot_unwind` @`0x6b9e3` | the abort target every shim's LSDA landing pad calls |

## Cross-References

- [Logging (nlog), ntrace and the Rust Logger Bridge](../runtime/logging.md) — the C `nlog` facility the `NeuronLogger` bridge (§3) terminates in; the 12×2 level matrix, the five-sink fan-out, and the coalescing ring on the far side of `nlog_write`
- [neuron_rustime: sys_trace Capture Engine](rust-capture.md) — the Rust engine behind the 12 sys_trace shims; the global capture state (`GLOBAL_LOCK`/`CONTEXTS`) the shims operate on, and the `log!` diagnostics that flow through the §3 bridge
- [neuron_rustime: serde_json Serializer](rust-serde.md) — the JSON read-out the `fetch_events_internal` shim drives; the reverse-FFI helpers (`cc_algo_name`/`dma_mem_usage_name`/`should_include_default_algo`) feed its `custom_data` keys
- [Interned String Database](../runtime/interned-strings.md) — the Rust `string_db` engine behind the 7 string_db shims; the caller-owned shard, the pure-hash `get_id`, and the C-frees-Rust export round-trip
- [back to index](../index.md)
