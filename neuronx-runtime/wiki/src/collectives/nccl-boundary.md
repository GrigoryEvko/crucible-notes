# The libnrt ↔ libnccom Boundary (nec_ / nccl)

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present, and the host-side seam TUs are `/opt/workspace/KaenaRuntime/enc/enc.cc` (the `ncclInit` handoff), `enc/nec.cc` (the `nec_*` reverse shims), and `enc/neuron_nccl.cc` (the `nccl*` forwarders). Where a symbol belongs to the peer collective library it is tagged `nccom:0x…` and pins to `libnccom.so.2.31.24` (`aws-neuronx-collectives 2.31.24.0-1a31ba186`, SONAME `libnccom.so.2`, build-id `9c00176c081788c9435d27d11bb40e92495463f0`). `.text`/`.rodata` VMA == file offset in both, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (disasm- and dynsym-anchored, both sides)** — the asymmetric link (no `DT_NEEDED libnccom` in `libnrt`; `DT_NEEDED libnrt.so.1` + VERNEED `NRT_2.0.0` in `libnccom`) is from `readelf -d`/`-V` on both binaries; the 37-slot `.bss` table and the `call *slot(%rip)` trampolines from `objdump -d` on `libnrt`; the 16 `nec_*` + 4 `nrt_*` reverse imports from `nm -D --undefined-only --with-symbol-versions` on `libnccom` matched name-for-name against `libnrt`'s `@@NRT_2.0.0` definitions. This page is the **conceptual** boundary view — the seam mechanism and the wrapper shape; the symbol-by-symbol catalogue is owned by [the ABI page](../nccom/abi.md). · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

`libnrt` and `libnccom` link to each other **in opposite directions by two unrelated mechanisms**, and that asymmetry is the one fact a reimplementer must reproduce exactly. In the FORWARD direction (`libnrt` → `libnccom`, the call-*out* into the NCCL fork) there is **no ELF link at all**: `libnrt.so` carries no `DT_NEEDED` on `libnccom` and holds zero `neuron*` symbols in its dynamic table. The link is forged at runtime. The embedded `ncclInit` (`0x1bff30`) `dlopen`s `"libnccom.so"` with `RTLD_NOW` once at `nrt_init`, runs a numeric version gate, then `dlsym`s **37** `neuron*` entry points into a contiguous `.bss` function-pointer table (`0xc967b8..0xc968d8`, 8-byte stride). The rest of `libnrt` never names those exports: it calls a family of thin local `nccl*` trampolines, each a one-instruction indirect jump through its slot, guarded by a sticky `nccl_init_error` sentinel that short-circuits every call if the one-time `dlopen`/`dlsym` failed.

In the REVERSE direction (`libnccom` → `libnrt`, the call-*back* into the runtime) the link is **hard and versioned**. `libnccom.so` carries `DT_NEEDED libnrt.so.1` and a `.gnu.version_r` entry requiring `(libnrt.so.1, NRT_2.0.0)` — and only `NRT_2.0.0`. It imports **20** symbols, all `@NRT_2.0.0`: **16** `nec_*` device/topology/proxy callbacks (`0x1bfd40..0x1bff20`) and **4** `nrt_*` runtime queries, every one resolving at *load* time against a `libnrt` `@@NRT_2.0.0` definition. The `nec_*` shims are thin tail-call thunks into `libnrt`'s NDL/`encd` device layer — they answer the topology, device-sizing, dynamic-size, and proxy-semaphore questions the fork must re-enter the runtime to ask. There is no `enc_*` symbol in the boundary: the collective *algorithm* engine (`alg_ring`/`kangaring`/`mesh`/RDH) is statically embedded inside `libnrt`, so the only thing crossing into `libnccom` is the transport/bootstrap layer.

This page documents the **seam itself**: the two link models (runtime `dlopen`-forward vs. load-time versioned-reverse), the all-or-nothing `dlsym` handoff and its compat-89 gate, the uniform shape of a `nccl*` forwarding wrapper (resolve-slot → check `nccl_init_error` → forward → log), and the role split of the `nec_*` reverse callbacks. The full per-symbol table — every `neuron*` definer address, `.bss` slot, and trampoline, and every `nec_*`/`nrt_*` definer — lives in [the ABI catalogue](../nccom/abi.md); this page summarizes that table by group and explains the *mechanism* a reimplementer must rebuild.

For reimplementation, the contract of the boundary is:

- **The FORWARD leg is a dlsym target list, not an export list.** A `libnccom` reimplementation defines all 37 `neuron*` as plain global `T` exports with **no version node**; a `libnrt` reimplementation `dlopen`s + `dlsym`s them in order, NULL-checks every result, and **zeros all 37 slots on the first failure** — collectives degrade to all-or-nothing, never partial. The only forward compatibility token is the numeric `89`, returned by `neuronGetVersionInfo` and checked at `0x1bff9b`.
- **The REVERSE leg is a versioned linker bind.** A `libnccom` reimplementation must `DT_NEEDED libnrt.so.1` and bind all 20 callbacks `@NRT_2.0.0`; a `libnrt` reimplementation must export those 20 names `@@NRT_2.0.0`. Dropping any one from the `NRT_2.0.0` node breaks `libnccom` at *load*, before any `dlopen` runs.
- **The wrapper shape is uniform and decides correctness on the degrade path.** Every per-call `nccl*` forwarder reads the shared `nccl_init_error` `ostringstream` (`0xc96640`) first: if `ncclInit` recorded a fault, it logs and returns `1` (`NRT_FAILURE`) *without* dereferencing the (now-NULL) slot. Two functions deliberately skip the guard — `ncclGetVersionInfo` (pre-init probe) and `ncclNetworkProxyProgress` (hot proxy tick). Reproduce the guard, the two exemptions, and the `return 1` on error, or the runtime will fault-on-NULL the moment collectives are unavailable.

## At a glance

| | |
|---|---|
| **Boundary** | `libnrt.so.1` ↔ `libnccom.so.2` (two packages, two build-ids) |
| **FORWARD** | 37 `neuron*` — `libnrt` `dlopen`+`dlsym`, **no version**, no `DT_NEEDED` |
| **REVERSE** | 16 `nec_*` + 4 `nrt_*` = 20 — `libnccom` linker imports, all **`@NRT_2.0.0`** |
| **Forward entry** | `ncclInit @0x1bff30` (`dlopen "libnccom.so", RTLD_NOW`) — sole internal caller `enc_init @0xffbb0` |
| **Forward slot table** | `.bss` `0xc967b8..0xc968d8` (8-byte stride, 37 slots), filled all-or-nothing |
| **Forward guard** | `nccl_init_error` `ostringstream @0xc96640` (`_ZL15nccl_init_error`), `tellp()!=0` short-circuit |
| **Forward gate** | numeric compat id `89` (`0x59`): `libnrt 0x1bff9b cmpq` ↔ `libnccom nccom:0x464a8 movq` |
| **Reverse definers** | `nec_* 0x1bfd40..0x1bff20`, `nrt_* 0x83e10/0x84210/0x940b0/0x94400`, all `@@NRT_2.0.0` |
| **Reverse gate** | ELF version `NRT_2.0.0` (`libnccom` VERNEED → `libnrt` version_d node) |
| **`nec_*` thunk target** | `libnrt`'s NDL / `encd` device layer (e.g. `nec_get_device_count → jmp ndl_available_devices`) |
| **Re-derived counts** | FORWARD 37 · REVERSE **16 `nec_` + 4 `nrt_` = 20** (`@NRT_2.0.0`); see [§ count](#the-nec_-count-re-derived) |
| **Catalogue** | full symbol table → [nccom/abi.md](../nccom/abi.md) |

---

## 1. The Two Link Models

The boundary is asymmetric **by mechanism**, not merely by direction. The forward and reverse legs are resolved by two different dynamic-linker facilities at two different times, and conflating them is the classic reimplementation error. The reference frame is a plugin host: `libnrt` treats the collectives transport the way a program treats an optional `dlopen`'d codec — present-or-degrade, name-resolved at runtime — while `libnccom` treats `libnrt` the way any shared object treats its `DT_NEEDED` dependency — hard, versioned, resolved at load.

### FORWARD — runtime `dlopen` + `dlsym`, unversioned

`libnrt` never names `libnccom` in its ELF. Its `DT_NEEDED` list is nine toolchain libraries (`libgcc_s`, `libutil`, `librt`, `libpthread`, `libdl`, `libstdc++`, `libm`, `libc`, `ld-linux`) and nothing else; `nm -D libnrt.so | rg neuron` is empty — there is no `neuron*` symbol, defined or undefined, in the runtime's dynamic table. The link is built at runtime by `ncclInit` (`0x1bff30`): `dlopen("libnccom.so", RTLD_NOW)`, then `dlsym` each `neuron*` name out of the handle and store the resolved pointer into a fixed `.bss` slot. Because `dlsym` matches on **name only**, the 37 targets are exported from `libnccom` as plain `T` globals with **no `@version` tag**. The compatibility check that *would* be a symbol version in a hard-linked ABI is, on this leg, a separate numeric handshake (§2).

### REVERSE — hard `DT_NEEDED` + versioned import

`libnccom` *does* name `libnrt`. `readelf -d libnccom.so` shows `NEEDED libnrt.so.1`, and `readelf -V` shows a `.gnu.version_r` entry requiring `(libnrt.so.1, NRT_2.0.0)`. Every `nec_*`/`nrt_*` import therefore reaches the dynamic linker tagged `@NRT_2.0.0` and is resolved by the standard symbol-versioning rule against `libnrt`'s `@@NRT_2.0.0` definitions — at **load** time, before `ncclInit` ever runs. There is exactly one required version node (`NRT_2.0.0`); `libnccom` requires nothing from `NRT_3.0.0` (the `nrta_*` async family), so a newer `libnrt` that keeps `NRT_2.0.0` stays load-compatible.

```text
            libnrt.so.1                              libnccom.so.2
        ┌───────────────────┐                    ┌───────────────────┐
        │  ncclInit 0x1bff30 │── dlopen ─────────▶│  (no DT_NEEDED on  │  FORWARD
        │  ┌──────────────┐  │   RTLD_NOW         │   libnrt for this  │  runtime, no version
        │  │ .bss slots   │◀─┼── dlsym 37×neuron* │   leg)             │  37 neuron* (bare T)
        │  │ 0xc967b8..d8 │  │                    │                    │
        │  └──────────────┘  │                    │                    │
        │   nccl* trampolines│── call *slot ─────▶│  neuron* exports   │
        │                    │                    │                    │
        │  nec_* @@NRT_2.0.0 │◀── @NRT_2.0.0 ─────│  16 nec_ + 4 nrt_  │  REVERSE
        │  nrt_* @@NRT_2.0.0 │   versioned import  │  UND imports       │  load-time, NRT_2.0.0
        │  (NDL/encd layer)  │── jmp encd_* ──┐    │  DT_NEEDED         │  hard link
        └───────────────────┘                │    │   libnrt.so.1      │
                              device layer ◀──┘    └───────────────────┘
```

> **QUIRK —** the same mistake fails loudly on one leg and silently passes on the other. Exporting a `nec_*` callback *un*versioned breaks `libnccom`'s load outright — its import is `@NRT_2.0.0` and an unversioned definition does not satisfy a versioned need. Versioning a `neuron*` export, by contrast, changes nothing the forward leg observes: `dlsym` ignores the version and binds the default. A reimplementer who reasons "it is one collectives ABI, version it consistently" will either break the reverse load or waste effort versioning forward symbols the linker never inspects. The two legs are not one ABI with two directions; they are two ABIs that happen to span the same pair of binaries.

---

## 2. The FORWARD Handoff — `ncclInit`

### Purpose

`ncclInit` (`0x1bff30`) is the one-time bridge that turns the unlinked forward leg into a callable table. It is reached exactly once, from `enc_init @0xffbb0` (the `nrt_init` process setup), and it is all-or-nothing: it either populates all 37 `.bss` slots and leaves `nccl_init_error` empty, or it zeroes every slot, records an error string, and leaves the whole runtime in degrade-to-no-collectives mode. The per-call `nccl*` wrappers (§3) read the outcome through the shared sentinel; they never re-attempt the load.

### Entry Point

```text
nrt_init
  └─ enc_init 0xffbb0                          ── process setup (sole caller of ncclInit)
       └─ ncclInit 0x1bff30                      ── *** the FORWARD handoff ***
            ├─ dlopen("libnccom.so", RTLD_NOW)   ── str @0x843132; NULL → record error, return FAILURE
            ├─ dlsym("neuronGetVersionInfo")     ── str @0x84313e → slot 0xc968d8 (gate #0)
            ├─ neuronGetVersionInfo(&vi, 56)     ── fills nec_version_info_t (56 B)
            ├─ cmpq $0x59, 0x30(%rsp)  @0x1bff9b ── compat id == 89? else "Incompatible version…"
            ├─ dlsym × 36 remaining neuron*      ── each: dlsym + dlerror + NULL-check + store
            │    (any NULL → zero ALL 37 slots @0x1c0045 + set nccl_init_error → ncclSystemError)
            └─ dlopen("libnccom-net.so")  [soft] ── str @0x84343d; on fail probe libfabric fi_version
```

### Algorithm — the all-or-nothing load + compat gate

```c
// ncclInit @0x1bff30 — the one-time FORWARD handoff (TU enc/enc.cc)
function ncclInit():                                          // 0x1bff30
    handle = dlopen("libnccom.so", RTLD_NOW);                // 0x843132 ; RTLD_NOW=2
    if (!handle):
        record(nccl_init_error,                              // 0xc96640
            "Error opening libnccom.so, cannot use collective operations! ... %s", dlerror());
        return NRT_FAILURE;

    // --- gate #0: version/compat probe (dlsym'd FIRST) ---
    neuronGetVersionInfo = dlsym(handle, "neuronGetVersionInfo");   // 0x84313e → slot 0xc968d8
    if (!neuronGetVersionInfo):
        record(nccl_init_error, "Get Version Info API is not implemented by aws-neuronx-collectives.");
        return NRT_FAILURE;
    nec_version_info_t vi = {0};                             // 56-byte stack struct, zeroed
    (*neuronGetVersionInfo)(&vi, 56);                        // fills major/minor/patch/maint/git_hash/compat
    if (vi.compatibility_version != 89):                     // off 48 ; cmpq $0x59,0x30(%rsp) @0x1bff9b
        record(nccl_init_error,
            "Incompatible version of aws-neuronx-collectives.  Compatibility id: %lu, expected: 89",
            vi.compatibility_version);                       // ", expected: " @0x843153
        return NRT_FAILURE;
    nlog("ENC", "check_version", INFO, "Using CCOM %lu.%lu.%lu.%lu-%s", vi.major, ...);  // 0x843160

    // --- the remaining 36 neuron* into the contiguous .bss table (all-or-nothing) ---
    for name in NEURON_SYMS[1..37]:                          // GetUniqueId..NetCloseListen, fixed order
        slot = dlsym(handle, name);                          // name literals @0x84318c..0x843428
        if (!slot):
            nlog("ENC", "ncclInit", ERROR,
                 "cannot find symbol \"%s\" in NCCL. Reason: %s", name, dlerror());  // 0x7f6328+
            zero_all_37_slots();                             // 0x1c0045: movq $0,<slot> ×37
            record(nccl_init_error, ...);                    // arm the per-call short-circuit
            return NRT_FAILURE;                              // ncclSystemError — degrade to no-collectives
        *(.bss slot for name) = slot;                        // 0xc967b8..0xc968d8, 8-byte stride

    // --- soft net-plugin load (not part of the 37; failure is non-fatal) ---
    net = dlopen("libnccom-net.so", RTLD_NOW);              // 0x84343d
    if (net): libnccl_net_handle = net;                     // 0xc96628 ; "dlopened libnccom-net.so\n"
    else:     probe_libfabric_fi_version();                 // "fi_version" @0x84344d; EFA-installer hint
    return NRT_SUCCESS;
```

> **GOTCHA —** the load is all-or-nothing, and the failure path zeros **every** slot, not just the one that failed. If `dlsym` of the 25th symbol returns NULL, the handler at `0x1c0045` writes `movq $0, <slot>` across all 37 entries — the first 24 successfully-resolved pointers are deliberately discarded. A reimplementer who leaves the already-resolved slots populated after a mid-sequence failure builds a table that is *partially* callable: some `nccl*` wrappers would forward into `libnccom` while others short-circuit, and a collective that touched both would corrupt state. The only two states the table is ever allowed to be in are "all 37 valid" or "all 37 NULL + `nccl_init_error` armed."

> **NOTE —** the version probe is gate #0 and is `dlsym`'d *before* the other 36, deliberately. `neuronGetVersionInfo` (slot `0xc968d8`, the high end of the table) must resolve and report compat `89` before a single other symbol is touched; on `89` mismatch `ncclInit` returns without resolving the rest, so the slots stay NULL and the sentinel is armed. The numeric `89` is the *only* forward compatibility token — it is independent of the ELF `NRT_2.0.0` tag that governs the reverse leg (§4), checked by a different mechanism (`cmpq` of a struct field, not the dynamic linker) at a different time (first call, not load). The two skew axes never meet; a build can satisfy one and violate the other.

---

## 3. The `nccl*` Forwarding Wrappers — the Wrapper Shape

### Purpose

Every `neuron*` entry point in the FORWARD table is fronted by a thin local `nccl*` trampoline (linkage `t`, TUs `enc/enc.cc` and `enc/neuron_nccl.cc`). The rest of `libnrt` calls these, never the `neuron*` exports directly, so the runtime has exactly one place that knows the load might have failed: the wrapper. Each one is the *same shape* — read the shared `nccl_init_error` sentinel, short-circuit with `NRT_FAILURE` if a prior fault was recorded, otherwise forward through the `.bss` slot and log on a non-zero return. The uniformity is the reimplementation contract: a reader who reconstructs one wrapper has reconstructed all of them, minus the two documented exemptions.

### Algorithm — a representative forwarding wrapper

```c
// representative nccl* per-call forwarder — shape shared by ~35 wrappers
// exemplar: ncclGetUniqueId @0x1c0a50 → neuronGetUniqueId (slot 0xc968d0, nccom:0x46660)
// the trampoline body is macro-generated in enc/neuron_nccl.cc; addresses below are exemplar sites
function ncclGetUniqueId(id /*128B out*/, rank_n, hint):       // 0x1c0a50
    // --- (1) the sticky init-error short-circuit (read the shared ostringstream) ---
    if (tellp(&nccl_init_error) != 0):                         // 0xc96640 ; "did ncclInit fault?"
        nlog("ENC", "ncclGetUniqueId", ERROR,
             "NCCL init error: %s", nccl_init_error.str());    // fmt @0x84347b (shared by all wrappers)
        clear(&nccl_init_error);                               // reset the stream after reporting
        return 1;                                              // NRT_FAILURE — do NOT touch the NULL slot

    // --- (2) forward through the .bss slot ncclInit filled ---
    rc = (*neuronGetUniqueId)(id, rank_n, hint);               // call *0xc968d0(%rip) — indirect, dlsym'd

    // --- (3) log on failure, propagate the fork's status verbatim ---
    if (rc != 0):
        nlog("ENC", "ncclGetUniqueId", ERROR,
             "failed neuronGetUniqueId request to NCCL");      // one literal per wrapper
    return rc;
```

The short-circuit is what makes the all-or-nothing load (§2) safe to consume: when `ncclInit` zeroed the slots, the slot for `neuronGetUniqueId` is NULL, but the wrapper never reaches the indirect call — `tellp(&nccl_init_error) != 0` diverts it to `return 1` first. The `nccl_init_error` object is a static `std::__cxx11::ostringstream` (`_ZL15nccl_init_error`); the wrappers read its stringbuf members (`qword_C966xx`, offsets `+0x20..+0x58` from `0xc96640`) to format the recorded message, then clear it. Each wrapper carries its own `"failed neuron<Sym> request to NCCL"` literal (e.g. `"failed neuronGetUniqueId request to NCCL"`), but they all share the one `"NCCL init error: %s"` format string at `0x84347b`. The matching `.cold` siblings (one 0x30-byte pad per wrapper, band `0x5547c..0x558fc`) are pure GCC exception landing pads — `std::string::_M_dispose` + `_Unwind_Resume` for the temporary string built when formatting the init-error message — and carry no logic.

### The two exemptions

Two wrappers deliberately deviate from the shape, and a reimplementer who applies the guard uniformly will break both:

| Wrapper | Addr | Deviation | Why |
|---|---|---|---|
| `ncclGetVersionInfo` | `0x1c0a10` | **no `nccl_init_error` guard**; zero-fills `nec_version_info_t` then calls the slot if non-NULL, else `NRT_FAILURE` | it is the *pre-init query* path — it runs during/around the version gate, before the sentinel is meaningfully armed; guarding it would gate the version check against itself |
| `ncclNetworkProxyProgress` | `0x1c1aa0` | **no `nccl_init_error` guard**; calls the slot directly, logs on failure | it is the **hot proxy tick** ([proxy-driver](proxy-driver.md) §4), re-entered every loop iteration; the guard's `ostringstream::tellp` is too costly per-tick and is provably unnecessary (the proxy never arms after a clean init) |

> **QUIRK —** the hot path skips the guard *for the same reason it is hot*. `ncclNetworkProxyProgress` is called once per driver-loop iteration on the `"proxy-<tid>"` thread; the other wrappers are control-plane, called a handful of times per communicator. Paying an `ostringstream::tellp` + potential `std::string` format on every proxy tick would tax the inner loop for a fault that, by construction, can only be latched at `ncclInit` time — long before any proxy task exists. The wrapper trusts that if `ncclInit` failed, no proxy task was ever enqueued (the collective never bootstrapped), so the slot is either valid or unreachable. A reimplementer who "fixes" this by adding the guard for consistency slows the data-plane loop to protect against a state that cannot occur on this path.

### Wrapper inventory by group

The wrappers fall into the control-plane / bootstrap / proxy / net groups below; the per-symbol `neuron*` target, `.bss` slot, and `nccom:` definer for each are in the [ABI catalogue](../nccom/abi.md#forward--37-neuron-dlsym-targets-definer-libnccom-user-libnrt).

| Group | Representative wrappers | Role | Guard? |
|---|---|---|---|
| version / init | `ncclGetVersionInfo 0x1c0a10`, `ncclGetUniqueId 0x1c0a50`, `ncclInitGlobalComm 0x1c0c30`, `ncclInitComm 0x1c0e10`, `ncclBuildGraphComm 0x1c0ff0`, `ncclGetCommInfo 0x1c11b0`, `ncclFreeComm 0x1c1370`, `ncclExpandConnectivity 0x1c1530` | communicator lifecycle / topology readback | yes (except `GetVersionInfo`) |
| bootstrap | `ncclBootstrapSend 0x1c1af0`, `ncclBootstrapRecv 0x1c1cc0`, `ncclBootstrapAllGather 0x1c1e90` | host-socket unique-id / signature exchange ([comm-context](comm-context.md) §5) | yes |
| network proxy | `ncclStartNetworkProxy 0x1c16f0`, `ncclStopNetworkProxy 0x1c18e0`, `ncclNetworkProxyProgress 0x1c1aa0` | the proxy-FIFO seam ([proxy-driver](proxy-driver.md) §4) | start/stop yes, **progress no** |
| net transport / OFI | `ncclNetRegMr 0x1c2070`, `ncclNetListen 0x1c32d0`, `ncclNetIsend 0x1c3da0`, `ncclNetTest 0x1c4310`, … | EFA/libfabric MR + connection + I/O ([transport-efa](../nccom/transport-efa.md)) | yes |

---

## 4. The REVERSE Callbacks — `nec_*` and `nrt_*`

### Purpose

The forward leg lets `libnrt` *drive* the fork; the reverse leg lets the fork *ask the runtime*. `libnccom` cannot see the runtime's device-driver state — which NeuronCores exist, their PCI BDFs, MLA/routing-id topology, pod reachability, the dynamic per-peer send/recv sizes a variable-peer collective needs, or the proxy-completion semaphore words — so it imports 20 callbacks that re-enter `libnrt`. Sixteen are `nec_*` (the device/topology/proxy surface, TU `enc/nec.cc`); four are `nrt_*` (general runtime queries). All 20 bind `@NRT_2.0.0` at load. Critically, **no `enc_*` symbol crosses here** (`nm -uD libnccom.so | rg 'U enc_'` = 0): the collective *algorithm* engine is embedded in `libnrt`, so only the transport/topology questions cross into the fork.

### The `nec_*` shim shape

Most `nec_*` exports are one-instruction tail-call thunks into the NDL / `encd` device layer — the reverse-leg analogue of the forward `nccl*` trampolines, but without a guard (the link is hard, so the target is always present). A few are non-trivial:

```c
// the nec_* reverse-callback shapes (TU enc/nec.cc) — three representative kinds

// (a) pure tail-call thunk — the common case
nec_get_device_count(out, n):              // 0x1bfd50
    jmp ndl_available_devices;             // 0xc2180 — DIRECTION PROOF: a libnrt-internal NDL call

// (b) inline body — no forwarding at all
nec_inc_semaphore(volatile uint32_t *sem, uint32_t val):   // 0x1bfd40
    mov %esi, (%rdi);  ret;                // *sem = val — the proxy-completion edge ([proxy-driver] §5)

// (c) re-entrant callback — calls back into the runtime's PUBLIC API
nec_get_peer_mla_idx(nec_dev, mla_idx, port):              // 0x1bfe30
    assert(nrt_get_instance_info(&info, sizeof(info)) == NRT_SUCCESS);   // 0x7d0d98
    if (nrt_is_trn2_trn3_family(info.family)):
        port = encd_arch_get_port(mla_idx, port);          // TRN2/TRN3 port remap
    return encd_arch_get_peer_mla_idx(nec_dev, mla_idx, port);          // sub_256D50
```

> **NOTE —** `nec_get_peer_mla_idx` (`0x1bfe30`) is a **bounded re-entrant** callback: it is invoked by `libnccom` (reverse) and itself calls `nrt_get_instance_info` (which is *also* a reverse import the fork uses directly). The re-entry terminates — `nrt_get_instance_info` reads device-driver state and calls no further `nec_*` — so there is no callback cycle. A reimplementer must keep this property: a `nec_*` callback may re-enter the runtime's query API, but must not re-enter the *fork* (no `nec_* → neuron*` edge exists), or the two binaries would recurse across the seam.

### Role groups

The 16 `nec_*` split into four roles; the `nrt_*` four are general queries. Per-symbol definer addresses and thunk targets are in the [ABI catalogue](../nccom/abi.md#reverse--16-nec_-devicetopologyproxy-callbacks-definer-libnrt-user-libnccom). The companion 4 `nrt_*` queries are catalogued [here](../nccom/abi.md#reverse--4-nrt_-runtime-queries-definer-libnrt-user-libnccom).

| Group | Symbols | Role |
|---|---|---|
| device sizing / enumeration | `nec_get_virtual_core_size 0x1bfdd0`, `nec_get_device_count 0x1bfd50`, `nec_get_device_pci_bdf 0x1bfd60` | how many cores, how big, which PCI device |
| MLA / routing topology | `nec_build_port_and_rid_map 0x1bfdf0`, `nec_is_mla_available 0x1bfe00`, `nec_mla_idx_to_rid 0x1bfe10`, `nec_rid_to_mla_idx 0x1bfe20`, `nec_get_peer_mla_idx 0x1bfe30` | NeuronLink adapter presence, index↔routing-id, peer lookup |
| pod reachability | `nec_get_p2p_pod_peer_node 0x1bfeb0`, `nec_pod_node_can_access_peer_node 0x1bfed0` | P2P/switch pod peer-node resolution + access predicate |
| dynamic-size / proxy / log | `nec_get_dynamic_send_size_bytes 0x1bfef0`, `nec_get_dynamic_send_offset_bytes 0x1bff00`, `nec_get_dynamic_recv_offset_bytes 0x1bff10`, `nec_set_recv_size_bytes 0x1bff20`, `nec_inc_semaphore 0x1bfd40`, `nec_ndl_printk 0x1bfee0` | variable-peer collective sizing, proxy-completion semaphore, log bridge into `ncclDebugLog` |
| `nrt_*` runtime queries | `nrt_get_total_vnc_count 0x83e10`, `nrt_get_instance_info 0x84210`, `nrt_get_version 0x940b0`, `nrt_get_libnccl_net 0x94400` | bootstrap gate, arch metadata, version hash, net-plugin handle handoff |

> **QUIRK —** four of the dynamic-size shims **reshape their arguments** as they forward — they are not pure thunks, they are ABI adapters. `nec_get_dynamic_send_size_bytes` (`0x1bfef0`) forwards to `enc_get_send_count_size_bytes` but *drops* the `rank_n` argument and *swaps* `(data_type_sz, dst_rank)`; the offset variants forward all four args unchanged. The reshape is deliberate — the fork's reverse-call signature and the runtime's internal signature were specified independently, and `nec.cc` adapts between them at the seam. A reimplementer who forwards these four argument-for-argument (treating them as thunks like the other twelve) passes the wrong operands to the size computation, and every variable-peer collective miscomputes its per-peer byte counts. The reshape is the one place a `nec_*` shim is *not* a transparent pass-through.

---

## The `nec_` Count, Re-derived

The reverse callback count was reported two different ways in earlier analysis, and the discrepancy is worth pinning down because it is the single number a `libnccom` reimplementation must bind exactly.

The true count, re-derived from `nm -D --undefined-only --with-symbol-versions libnccom.so` matched against `libnrt`'s `@@NRT_2.0.0` definitions, is **16 `nec_*` + 4 `nrt_*` = 20 imports**, all `@NRT_2.0.0`, with **zero** binding to `NRT_3.0.0`. The 16 `nec_*` definers occupy the contiguous `libnrt` band `0x1bfd40..0x1bff20`:

```text
0x1bfd40 nec_inc_semaphore                  0x1bfe10 nec_mla_idx_to_rid
0x1bfd50 nec_get_device_count               0x1bfe20 nec_rid_to_mla_idx
0x1bfd60 nec_get_device_pci_bdf             0x1bfe30 nec_get_peer_mla_idx
0x1bfdd0 nec_get_virtual_core_size          0x1bfeb0 nec_get_p2p_pod_peer_node
0x1bfdf0 nec_build_port_and_rid_map         0x1bfed0 nec_pod_node_can_access_peer_node
0x1bfe00 nec_is_mla_available               0x1bfee0 nec_ndl_printk
                                            0x1bfef0 nec_get_dynamic_send_size_bytes
                                            0x1bff00 nec_get_dynamic_send_offset_bytes
                                            0x1bff10 nec_get_dynamic_recv_offset_bytes
                                            0x1bff20 nec_set_recv_size_bytes      → 16 total
```

> **CORRECTION (ENC-08) —** an early single-cell pass over the `enc/nec.cc` band reported **12** `nec_*` callbacks, and the original raw boundary note carried that figure. The true dynsym count is **16**. The "12" was a band-of-analysis artifact: that pass swept only `0x1bfdf0..0x1bff20` (the twelve topology/dynamic-size shims that live in one cell) and did not include the four lower-addressed callbacks `nec_inc_semaphore` (`0x1bfd40`), `nec_get_device_count` (`0x1bfd50`), `nec_get_device_pci_bdf` (`0x1bfd60`), and `nec_get_virtual_core_size` (`0x1bfdd0`), which sit just below it and are owned by sibling cells. The both-binaries closure (`nm -D` on `libnccom`'s import side, cross-checked against `libnrt`'s `@@NRT_2.0.0` export side) settles it at **16 + 4 = 20**, the figure the [ABI catalogue](../nccom/abi.md) carries and the count a reimplementation must export.

> **NOTE —** the `libnrt`-local `nec_get_version_info` (`0x1bfde0`, a `jmp ncclGetVersionInfo 0x1c0a10`) and the eleven `nec_vil_*` helpers (`0x260240+`) are **not** in the dynamic symbol table and are correctly absent from `libnccom`'s import list — they are intra-`libnrt` calls, not boundary symbols. Counting them would inflate the reverse set; the boundary is the dynsym surface only, which is why the count is exactly 16, not 17 or 27.

---

## Related Components

| Name | Relationship |
|---|---|
| `ncclInit` (`@0x1bff30`) | the FORWARD handoff: `dlopen` + 37×`dlsym` + compat-89 gate (§2) |
| `nccl_init_error` (`@0xc96640`) | the sticky sentinel every per-call wrapper reads (§3) |
| `.bss` slot table (`0xc967b8..0xc968d8`) | the 37 `dlsym`'d `neuron*` pointers the `nccl*` trampolines call through |
| `nec_*` band (`0x1bfd40..0x1bff20`) | the 16 reverse callbacks `libnccom` imports `@NRT_2.0.0` (§4) |
| `ncclNetworkProxyProgress` (`@0x1c1aa0`) | the hot, guard-exempt seam wrapper into the proxy FIFO |
| `nec_inc_semaphore` (`@0x1bfd40`) | the inline proxy-completion reverse edge the transport drives |

## Cross-References

- [The libnrt ↔ libnccom ABI](../nccom/abi.md) — the symbol-by-symbol catalogue this page summarizes: every `neuron*` definer/`.bss` slot/trampoline and every `nec_*`/`nrt_*` definer, with the re-derived counts
- [Comm Context and Bootstrap](comm-context.md) — the `enc_glb_comm` / per-NC node bootstrap that drives `ncclGetUniqueId`/`ncclInitComm`/`ncclBootstrapSend` across this boundary
- [Bananaphone IPC and the Proxy Driver](proxy-driver.md) — the `"proxy-<tid>"` driver whose `step()` issues the guard-exempt `ncclNetworkProxyProgress` and consumes the `nec_inc_semaphore` edge
- [Send / Recv Primitives and Protocols](../nccom/send-recv-prims.md) — the `libnccom`-side `netNeuron{Send,Recv}Proxy` callbacks the proxy seam ultimately reaches, and the descriptor FIFOs they consume
- [Overview: the NCCL Fork (2.31.24+nrt2.0)](../nccom/overview.md) — the `libnccom` fork as a whole: what stays in-process in `libnrt` vs. what crosses the `dlopen` boundary
- [back to index](../index.md)
