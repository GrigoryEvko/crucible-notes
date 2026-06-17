# Public C API: Device, Config and Env Parsing

> *All addresses, offsets, and symbol names on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries full DWARF — function, struct-field, and enum names survive. All four `PT_LOAD` segments are identity-mapped: `.text`, `.rodata`, **and `.data` are VMA == file offset** — read any `.data`/`.bss` global at its VMA directly, with no relocation delta. All addresses are analysis VMAs. Other versions will differ.*
>
> *Evidence grade: **Confirmed (byte-anchored)** — every function address, struct size, and field offset is nm/DWARF-verified; the getenv→field bindings are recovered from `objdump` `lea` targets joined to the DWARF struct layout (`P1-L-API-05`, `P1-F-KNOBS`, `P1-F-ENV`). · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **device-introspection and config-from-environment** surface of the Neuron Runtime: the four public count queries (`nrt_get_total_vnc_count`, `nrt_get_total_nc_count`, `nrt_get_visible_vnc_count`, `nrt_get_visible_nc_count`), the instance-identity query (`nrt_get_instance_info` `@0x84210`), the internal device dispatcher they share (`nrt_get_dev_info` `@0x83ad0`), and the env-config parser cluster — headed by `nrt_config_parse_init_config` `@0x8a1d0` — that turns ~75 `NEURON_RT_*` variables into two process-global config structs. These are the functions a framework calls *before* it loads a model: "how many cores can I see," "what instance am I on," and "what did the operator configure via the environment."

The familiar reference frame is a **two-level core-naming scheme** layered on top of a flat physical-core count, exactly the shape of CUDA's *visible-device masking* (`CUDA_VISIBLE_DEVICES`) crossed with a *virtual-to-physical* translation table. The hardware exposes some number of **physical TPB cores** (ptpb) per Neuron device; the runtime groups them into **Logical Neuron Cores** (vtpb / LNC, "virtual NeuronCores") of a configurable **virtual-core size** (the LNC size). A virtual-core size of 1 means one logical core per physical core; a size of 2 fuses two physical cores into one logical core (the v2-arch maximum). Every count API answers in *logical* cores, and the translation between the two namespaces is the math this page derives. The `_vnc_` and `_nc_` symbol pairs are not two different counts — the `_nc_` symbols are **one-instruction tail-call thunks** into the `_vnc_` bodies, a deliberate aliasing that a reimplementer must reproduce, not duplicate.

The page applies the recurring H3 vocabulary to three units: **§1** the device dispatcher and the four count APIs, with the ptpb↔vtpb translation math; **§2** `nrt_get_instance_info` and the `nrt_instance_info_t` layout; and **§3** the env-config parse machine — the typed `nrt_config_parse` helper family, the getenv-callsite → config-field population pattern, and the two-struct destination model. The **full** 73-member / 47-member struct catalogue is owned by [config-structs](config-structs.md) and the **full** 138-name env catalogue by [env-vars](env-vars.md); this page derives the *mechanism* (how a name becomes a field) and cites only the fields its own code paths touch. The `nrt_init` caller and the lifecycle state guard live in [api-lifecycle](api-lifecycle.md).

For reimplementation, the contract is:

- **The two core namespaces** — physical TPB cores (ptpb) vs Logical Neuron Cores (vtpb / LNC), related by a configured *virtual-core size*; every public count API returns logical cores.
- **The ptpb↔vtpb translation** — `vtpb = ptpb / size` and its inverse, fed by `parse_vnc_config` (`@0x83b40`), which resolves the size from `NEURON_LOGICAL_NC_CONFIG` / `NEURON_RT_VIRTUAL_CORE_SIZE` with the power-of-two and `(nc_per_device % size)==0` invariants.
- **The nc-vs-vnc thunk identity** — `nrt_get_total_nc_count` and `nrt_get_visible_nc_count` are tail-call aliases of the `_vnc_` bodies; the same computation answers both.
- **The visible-core resolution order** — `INIT`/`CHILD` reads the cached `visible_virtual_cores` vector; otherwise `NEURON_RT_VISIBLE_CORES` → `NEURON_RT_NUM_CORES` → total-count fallback.
- **The env→field parse pattern** — a typed `nrt_config_parse(name, default, &dest)` family where the 2nd argument *is* the compile-time default and `&dest` points into one of two distinct config structs (the 656-byte static `nrt_config` or the 296-byte heap `nrt_global_config`).

| | |
|---|---|
| **Device dispatcher** | `nrt_get_dev_info` `@0x83ad0` (107 B) — `funtime ? nrt_fake_dev_info : tdrv_get_dev_info` |
| **Total cores** | `nrt_get_total_vnc_count` `@0x83e10` (997 B) · thunk `nrt_get_total_nc_count` `@0x84200` |
| **Visible cores** | `nrt_get_visible_vnc_count` `@0x855b0` (1710 B) · thunk `nrt_get_visible_nc_count` `@0x85c60` |
| **Virtual-core size** | `parse_vnc_config` `@0x83b40` → `cfg.virtual_core_size` (`+8`) |
| **Visible-core list** | `parse_visible_virtual_cores` `@0x85170` → `cfg.visible_virtual_cores` (`+16`, `std::vector<int>`) |
| **Instance identity** | `nrt_get_instance_info` `@0x84210` (1501 B) → `nrt_instance_info_t` (32 B) |
| **Master env parser** | `nrt_config_parse_init_config` `@0x8a1d0` (23088 B, 713 BB) — `nrt_init`-only |
| **Funtime/fake parser** | `nrt_config_parse_funtime` `@0x83760` (879 B) — `NEURON_RT_FAKE_INSTANCE_TYPE` |
| **getenv PLT** | `getenv@GLIBC_2.2.5` `@0x3cfd0` (43 call sites — the *only* Neuron env path) |
| **Static config struct** | `nrt_config` (`nrt_config_0`) `@.bss 0xc5c480` (656 B, 73 members) |
| **Heap config struct** | `nrt_global_config` via `ngc @0xc5c460` / `nrt_gconf()` `@0x82670` (296 B, 47 members) |

> **NOTE —** the `_no_state` suffix on the translation helpers (`vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state`, `vtpb_translate_vtpb_to_ptpb_no_state`) marks the *stateless* arithmetic form — it derives the answer from `(count, size)` arguments alone, without consulting the per-process device book. The count APIs use the stateless form because they run before (or independently of) device bring-up; the lifecycle allocator in [api-lifecycle](api-lifecycle.md) uses the same translation primitives during `nrt_init`'s LNC reservation. The helpers themselves live in the `vtpb` cross-cutting layer and are cited, not derived, here.

---

## 1. Device Query and Core-Count Translation

### Purpose

The four count APIs answer "how many NeuronCores exist" (total) and "how many can this process touch" (visible), always in **logical** (vtpb / LNC) units. All four descend through one internal dispatcher, `nrt_get_dev_info` (`@0x83ad0`), which resolves the raw hardware shape — `nd_count` (number of Neuron devices), `nc_per_device` (physical TPB cores per device), the arch type, and the `available_devices[]` list — then apply the ptpb→vtpb translation using the configured virtual-core size. The dispatcher is the single fork between real hardware and simulation: it parses the funtime flag once and routes to either `nrt_fake_dev_info` (simulated device table) or `tdrv_get_dev_info` (the driver) accordingly.

### Entry Point

```text
nrt_get_total_nc_count (0x84200)  ── THUNK (1 insn) ──► nrt_get_total_vnc_count (0x83e10)
                                                          │
nrt_get_visible_nc_count (0x85c60) ─ THUNK ─► nrt_get_visible_vnc_count (0x855b0)
                                                          │
both vnc bodies                                           ▼
   ├─ nrt_get_dev_info (0x83ad0)                  ── nd_count, nc_per_device, arch, available_devices[32]
   │     └─ nrt_config_parse_funtime (0x83760)    ── parse NEURON_RT_FAKE_INSTANCE_TYPE once (idempotent)
   │     └─ [funtime] nrt_fake_dev_info  :  [real] tdrv_get_dev_info          (both cross out of this cell)
   ├─ parse_vnc_config (0x83b40)                   ── resolve virtual-core size → cfg.virtual_core_size(+8)
   │     └─ nrt_config_parse(uint, base 2)         ── NEURON_LOGICAL_NC_CONFIG / NEURON_RT_VIRTUAL_CORE_SIZE
   ├─ vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state(total_ptpb, size, &out)   ── total path
   └─ [visible only] parse_visible_virtual_cores (0x85170)
         └─ nrt_config_parse<int,true> (0x84fe0)   ── expand "a-b,c" → vector<int>
         └─ vtpb_translate_vtpb_to_ptpb_no_state   ── validate each logical core's physical backing
```

`nrt_get_total_vnc_count` and `nrt_get_visible_vnc_count` each open with an `NlogErrorContextManager` (the per-API `"Generic API Failure"` flush guard described in [api-lifecycle](api-lifecycle.md)) and return `NRT_STATUS`; the requested count is written through an out-pointer.

### Algorithm

```c
// Models nrt_get_total_vnc_count @0x83e10. Returns NRT_STATUS; *out = logical core count.
// nrt_get_total_nc_count @0x84200 is a 1-insn tail-call thunk forwarding all args here.
function nrt_get_total_vnc_count(uint32 *out):
    NlogErrorContextManager ctx("nrt_get_total_vnc_count")   // RAII log-flush guard
    rc = nrt_get_dev_info(&nd_count, &nc_per_device, &arch, available_devices)   // 0x83ad0
    if rc != NRT_SUCCESS:
        log_err("Failed to parse vnc config")  // shared diag string; rc propagated
        return rc
    total_ptpb = nc_per_device * nd_count                    // flat physical TPB-core count

    // resolve the virtual-core (LNC) size — NEURON_LOGICAL_NC_CONFIG is the env arg here
    rc = parse_vnc_config("NEURON_LOGICAL_NC_CONFIG", &size) // 0x83b40 → cfg.virtual_core_size(+8)
    if rc != NRT_SUCCESS: return rc

    // ptpb → vtpb: logical = physical / size  (stateless arithmetic form)
    rc = vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state(total_ptpb, size, out)
    if rc != NRT_SUCCESS:
        log_err("Failed to convert vtpb to ptpb num cores")
    return rc

// Models nrt_get_dev_info @0x83ad0 — the real/sim device dispatcher.
function nrt_get_dev_info(*nd_count, *nc_per_device, *arch, available_devices[32]):
    nrt_config_parse_funtime()                               // 0x83760 — parse FAKE_INSTANCE_TYPE once
    if nrt_config_0.funtime:                                 // cfg +337
        return nrt_fake_dev_info(...)                        // simulated device table
    else:
        return tdrv_get_dev_info(...)                        // driver query

// Models parse_vnc_config @0x83b40 — resolve the LNC / virtual-core size.
function parse_vnc_config(string env_name, uint32 *out):
    if arch == SUNDA:                                        // v1-switch family
        *out = 1; return NRT_SUCCESS                         // forced: one logical core per physical
    if getenv(env_name):                                    // e.g. NEURON_LOGICAL_NC_CONFIG
        *out = nrt_config_parse_uint(env_name, base=2)
    else if getenv("NEURON_RT_VIRTUAL_CORE_SIZE"):
        *out = nrt_config_parse_uint("NEURON_RT_VIRTUAL_CORE_SIZE", base=2)
    else:
        *out = (nc_per_device == 1) ? 1 : 2                  // default: 2, or 1 on single-core devices
    // invariants enforced before return:
    assert is_power_of_two(*out)                             // "out != 0" + po2 check
    assert (nc_per_device % *out) == 0                       // "(nc_per_device % out) == 0"
    if v2_arch && *out > 2:                                  // "Only LNC Size of 2 is supported … on this instance"
        return NRT_INVALID
    return NRT_SUCCESS
```

The visible-count body differs only in its source of truth: when the runtime is already up (or in a forked child) it returns the **cached** allocation rather than re-deriving from the environment.

```c
// Models nrt_get_visible_vnc_count @0x855b0. Thunk: nrt_get_visible_nc_count @0x85c60.
function nrt_get_visible_vnc_count(uint32 *out):
    NlogErrorContextManager ctx("nrt_get_visible_vnc_count")
    if nrt_init_state == INIT || nrt_init_state == CHILD:    // already allocated (api-lifecycle.md)
        *out = nrt_config_0.visible_virtual_cores.size()     // cfg +16, the cached vector
        return NRT_SUCCESS
    // pre-init: derive from the environment, in priority order
    nrt_config_parse_funtime()
    if getenv("NEURON_RT_VISIBLE_CORES"):
        parse_vnc_config("NEURON_LOGICAL_NC_CONFIG", &size)
        parse_visible_virtual_cores("NEURON_RT_VISIBLE_CORES", size, &list)  // 0x85170
        *out = list.size(); return NRT_SUCCESS
    if getenv("NEURON_RT_NUM_CORES"):
        *out = nrt_config_parse_uint("NEURON_RT_NUM_CORES"); return NRT_SUCCESS
    return nrt_get_total_nc_count(out)                        // fallback: whole-device visibility
```

> **QUIRK —** `nrt_get_total_nc_count` (`@0x84200`) and `nrt_get_visible_nc_count` (`@0x85c60`) are **the same computation** as their `_vnc_` counterparts, not a separate "physical core" count. Each is a single tail-call instruction (IDA `thunk` attribute) that forwards all register/stack arguments into the `_vnc_` body — `nrt_get_total_nc_count → nrt_get_total_vnc_count` (10 forwarded args), `nrt_get_visible_nc_count → nrt_get_visible_vnc_count` (18 forwarded args). The `nc` / `vnc` distinction is **purely nominal naming compatibility**: "NeuronCore" and "virtual NeuronCore" are the same logical-core abstraction in this build, both already expressed in vtpb/LNC units. A reimplementer must alias the two symbols (so the `_nc_` export and the `_vnc_` export resolve to one body) — *not* implement a second function that returns physical (ptpb) cores. There is no public API that returns a raw physical-core count; ptpb is an internal quantity that never escapes the translation layer.

> **GOTCHA —** the visible count is **stateful after `nrt_init`**. Before init it is environment-derived (the priority ladder above); at and after `INIT` (and in any forked `CHILD`) it returns `visible_virtual_cores.size()` from the cached vector that `nrt_init` populated during LNC allocation. A reimplementation that always re-parses `NEURON_RT_VISIBLE_CORES` will diverge from the runtime whenever the operator changes the env between init and a later query, and will mis-report in a forked child whose env was inherited but whose allocation is fixed. The state word is `nrt_init_state @0xc5d1a0`; the cached vector is `nrt_config_0.visible_virtual_cores` at offset `+16`.

### The translation math, stated plainly

Let `P = nc_per_device × nd_count` be the flat physical-TPB-core count and `s` the virtual-core size (LNC size). The two namespaces relate by integer division:

```text
  vtpb (logical cores) = ptpb / s          ── vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state
  ptpb (physical cores) = vtpb * s          ── vtpb_vtpb_num_cores_to_ptpb_num_cores_no_state
  one logical core idx L → physical cores [L*s .. L*s + s - 1]  ── vtpb_translate_vtpb_to_ptpb_no_state
```

`parse_vnc_config` guarantees the division is exact: it asserts `s` is a power of two and that `s` divides `nc_per_device`. The default `s` is **2** (two physical cores fused per logical core), dropping to **1** on a device that has only one physical core per device, and forced to **1** on the SUNDA (v1-switch) arch. The v2 arch caps `s` at 2; later archs admit larger fusion. Because the relation is exact division, `nrt_get_total_vnc_count` is `P / s` and the per-process visible count is `|visible_virtual_cores|` — never a remainder.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_get_dev_info` | `0x83ad0` | 107 | Device dispatcher: `funtime ? fake : tdrv` device query | HIGH |
| `nrt_get_total_vnc_count` | `0x83e10` | 997 | Total logical-core count = `P / s` | HIGH |
| `nrt_get_total_nc_count` | `0x84200` | thunk | Tail-call alias of `_vnc_` body (same computation) | CERTAIN |
| `nrt_get_visible_vnc_count` | `0x855b0` | 1710 | Visible logical cores; cached after `INIT` | HIGH |
| `nrt_get_visible_nc_count` | `0x85c60` | thunk | Tail-call alias of `_vnc_` body (same computation) | CERTAIN |
| `parse_vnc_config` | `0x83b40` | — | Resolve virtual-core size `s` with po2 / divisibility asserts | HIGH |
| `parse_visible_virtual_cores` | `0x85170` | — | Expand `NEURON_RT_VISIBLE_CORES` → vector, validate accessibility | HIGH |
| `vtpb_ptpb_num_cores_to_vtpb_num_cores_no_state` | — | — | `ptpb → vtpb` (`/s`); cross-cutting `vtpb` layer | HIGH |
| `vtpb_translate_vtpb_to_ptpb_no_state` | — | — | Logical-core-idx → physical-core range; visibility check | HIGH |

### Considerations

`parse_visible_virtual_cores` does more than count: it builds a bitmap of physically-accessible cores from `available_devices[]`, then validates each requested logical core via `vtpb_translate_vtpb_to_ptpb_no_state` against that bitmap, erroring `"Specified core not accessible! is /dev/neuron%u visible to process?"` when a requested core has no visible physical backing — the device-visibility check is what makes `NEURON_RT_VISIBLE_CORES` a *security-relevant* knob (it cannot widen a process's device access beyond what the cgroup / `/dev/neuron*` permissions already grant). The number-range expansion (`"a-b,c"` syntax) is the templated `nrt_config_parse<int,true>` (`@0x84fe0`) → `nrt_config_parse_number_range<int,true>`, which `strtoll`s each token through the `__gnu_cxx::__stoa<long long>` wrapper (`@0x8fcf0`); a malformed range throws `invalid_argument` / `out_of_range` from that wrapper rather than silently truncating.

---

## 2. Instance Identity Query

### Purpose

`nrt_get_instance_info` (`@0x84210`, 1501 B) fills a caller-supplied `nrt_instance_info_t` (32 B) with the EC2 instance **family**, **size**, **arch name**, and **device revision** — the data a framework prints in a banner or uses to pick a kernel variant. It is the runtime's answer to "what hardware am I actually on," distinct from the *count* APIs (how many cores) of §1. The result is computed once and cached lock-free; subsequent calls return the cache.

### Algorithm

```c
// Models nrt_get_instance_info @0x84210. Returns NRT_STATUS.
// Out struct nrt_instance_info_t (32 B): family(+0), size(+4), arch_name[16](+8), device_revision[8](+24).
function nrt_get_instance_info(nrt_instance_info_t *info, size_t len):
    if len > 0x20:                                          // 32
        warn_once("Not all fields will be returned, info size is: %lu, copy size is: %lu", len, 32)
    cached = nrt_get_instance_info::cached_info             // static nrt_instance_info_t* (calloc 32)
    if cached != NULL: { memcpy(info, cached, min(len,32)); return NRT_SUCCESS }   // cache hit

    local = {0}
    nrt_config_parse_funtime()                             // 0x83760
    if nrt_config_0.funtime:                               // simulated instance
        local.family = nrt_config_0.fake_family            // cfg +340
        local.size   = nrt_config_0.fake_size              // cfg +344
        strncpy(local.arch_name, nrt_get_arch_name_from_family(local.family), 15)
    else:                                                  // real hardware
        rc = nrt_get_dev_info(...)                         // 0x83ad0 — arch type
        if rc: { log_err("Failed to get device info by nrt_get_dev_info()"); return rc }
        fopen(sysfs "product_name"); read; strtok_r → (family_str, size_str)   // EC2 product_name
        local.family = nrt_get_instance_family(family_str)
        local.size   = nrt_get_instance_size(size_str)
        // CAYMAN special-case: product_name first 8 bytes == "EPIC-TGH"(+'7') → family 5
        if product_name starts "EPIC-TGH7": local.family = 5
        // MARIANA fixups: unknown → family 14 ; size == 9 → 1
        strncpy(local.arch_name, nrt_get_arch_name_from_family(local.family), 15)
        // device_revision: "pre" when arch==4 && hw_revision <= 'A'; else from rev table
        local.device_revision = (arch==4 && tdrv_get_hw_revision() <= REV_A) ? "pre" : rev_string

    // publish the cache lock-free; the CAS loser frees its own copy
    p = calloc(1, 32); *p = local
    if !CAS64(&nrt_get_instance_info::cached_info, NULL, p): free(p)
    memcpy(info, &nrt_get_instance_info::cached_info, min(len, 32))
    return NRT_SUCCESS
```

> **QUIRK —** the family enum is not a clean 1:1 map from the sysfs `product_name`. Two product strings are special-cased in the body: a `product_name` beginning `"EPIC-TGH7"` (decoded from the first-8-bytes constant `0x4847542D43495045` = `"EPIC-TGH"` followed by `'7'`) is forced to **family 5** (CAYMAN), and an unrecognized MARIANA product falls back to **family 14** with a `size == 9 → 1` fixup. A reimplementer who maps `product_name` to a family by a simple lookup table will mis-identify exactly these two silicon variants. The full family enum and its cloud-instance mapping are owned by [arch/generations-enum](../arch/generations-enum.md); this page records only that the mapping has these two binary-confirmed exceptions (MEDIUM confidence on the enum values 5 / 14, which are decompiler constants).

> **NOTE —** the cache is a single `static nrt_instance_info_t* cached_info`, published with one `_InterlockedCompareExchange64`. Two threads racing the first call each compute a full copy; the CAS winner installs its pointer and the loser frees its own buffer. The result is therefore stable for process lifetime and never recomputed — a reimplementation may compute it eagerly at init or lazily, but must guarantee a single stable answer (the `funtime` path can pin `family` / `size` to operator-chosen fakes, so the cache must not be populated before `nrt_config_parse_funtime` has run).

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_get_instance_info` | `0x84210` | 1501 | Fill `nrt_instance_info_t`; lock-free cache | HIGH |
| `nrt_config_parse_funtime` | `0x83760` | 879 | Parse `NEURON_RT_FAKE_INSTANCE_TYPE`; set `funtime` / `fake_family` / `fake_size` | HIGH |
| `nrt_get_instance_family` / `nrt_get_instance_size` | — | — | Parse the sysfs `product_name` tokens (cross-cutting) | MEDIUM |
| `nrt_get_arch_name_from_family` | — | — | Family → `arch_name[16]` string (cross-cutting) | MEDIUM |

### Encoding — `nrt_instance_info_t` (32 bytes)

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `family` | `+0` | `uint32_t` | Instance-family enum (CAYMAN `EPIC-TGH7` → 5; unknown MARIANA → 14) |
| `size` | `+4` | `uint32_t` | Instance size (MARIANA `size == 9` → 1) |
| `arch_name` | `+8` | `char[16]` | `strncpy(15)` from `nrt_get_arch_name_from_family` |
| `device_revision` | `+24` | `char[8]` | `"pre"` when `arch==4 && hw_revision <= 'A'`, else rev string |

---

## 3. Environment-Config Parsing

### Purpose

The runtime centralizes *all* `NEURON_RT_*` ingestion in `nrt_config.cpp`. The master parser `nrt_config_parse_init_config` (`@0x8a1d0`, the single densest non-init function at 23 KB / 713 basic blocks) runs once from `nrt_init`, reading ~75 environment variables and writing each into one of two process-global config structs. The design goal a reimplementer must reproduce is the **uniform typed-parse pattern**: every knob is read by a helper that takes the variable *name*, a compile-time *default*, and the *destination field address* — so the default is recoverable directly from the call site and every knob shares one getenv / default / store path.

### Entry Point

```text
nrt_init (0x94e90)                                   ── api-lifecycle.md, step (4)
  └─ nrt_config_parse_init_config (0x8a1d0)          ── master; ~75 NEURON_RT_* vars
       ├─ calloc(296,1) → nrt_global_config ; ngc = it  (published @0xc5c460)
       ├─ nrt_config_parse_funtime (0x83760)         ── NEURON_RT_FAKE_INSTANCE_TYPE
       ├─ parse_vnc_config (0x83b40)                 ── NEURON_LOGICAL_NC_CONFIG
       ├─ parse_visible_virtual_cores (0x85170)      ── NEURON_RT_VISIBLE_CORES
       ├─ parse_low_latency_tasks_cpu_affinity (0x85c70)  ── cpu_set_t mask
       ├─ get_enc_proxy_histogram_config (0x847f0)   ── 5-token string grammar
       ├─ parse_cc_alg_types / parse_hbm_scrub_init_val   ── bitmap / scrub knobs
       └─ typed nrt_config_parse(name, default, &dest) ×~70  ── the uniform pattern
              └─ getenv@plt (0x3cfd0)                ── the only Neuron env read
       ── returns max(NRT_STATUS) over all parses    ── via emplace_back<NRT_STATUS> (0x85f20)

  Separately, also from nrt_init / nrt_inspect_begin:
  └─ nrt_config_parse_inspect_config (0x86760)       ── NEURON_RT_INSPECT_* (own page)
  And from nrt_load_util / nrt_cc_schedule:
  └─ nrt_config_parse_model_config (0x85f50)         ── per-model knobs (own page)
```

### Algorithm

```c
// The uniform env→field pattern. Each of the 6 typed overloads getenv()s the name,
// falls back to `default`, parses, and stores into *dest. The 2nd arg IS the default.
//   0x7ff20  nrt_config_parse(string, long,  long&)     0x81120  (string, uint,  uint&)
//   0x813d0  nrt_config_parse(string, ulong, ulong&)    0x81a90  (string, bool,  bool&)
//   0x80720  nrt_config_parse(string, char*, char*&)    0x84fe0  <int,true>(string, vec, vec&)
function nrt_config_parse(string name, T default_value, T *dest):    // one overload per T
    const char *s = getenv(name.c_str())                 // 0x3cfd0 — the single env read
    if s == NULL:
        *dest = default_value                            // recoverable: literal at the call site
    else:
        *dest = parse_T(s)                               // strtoll/strtoull/atoi/"0"|"1"/strdup per T
    return NRT_SUCCESS

// Models the master parser's per-knob body, nrt_config_parse_init_config @0x8a1d0.
function nrt_config_parse_init_config():
    char *gcfg = calloc(296, 1)                          // the heap nrt_global_config buffer
    nrt_get_instance_info(&info)                         // arch/family → arch-dependent defaults
    statuses = vector<NRT_STATUS>()

    // ── each knob is one of these calls; dest is &cfg.field OR gcfg+off ──
    nrt_config_parse("NEURON_RT_NUM_CORES",        0,        &cfg.num_cores)            // cfg +12
    parse_vnc_config("NEURON_LOGICAL_NC_CONFIG",             &cfg.virtual_core_size)    // cfg +8
    nrt_config_parse("NEURON_RT_RESET_CORES",      1,        &cfg.reset_cores)          // cfg +64
    nrt_config_parse("NEURON_RT_DBG_DISABLE_DGE",  0,        gcfg + 28)                 // gcfg dbg_disable_dge
    nrt_config_parse("NEURON_RT_UCODE_LIB_PATH",   NULL,     &cfg.neuron_ucode_path)    // cfg +224 (strdup)
    nrt_config_parse("NEURON_RT_CC_CHUNK_SIZE",    0x200000, &cfg.cc_chunk_size)        // cfg +196
    // … ~70 more, plus the specialized parsers (vnc/visible/affinity/histogram/cc_alg/hbm) …
    statuses.emplace_back(rc_of_each)                    // 0x85f20

    ngc = gcfg                                           // PUBLISH the 296-B struct (init:1689)
    return max(statuses)                                 // aggregate worst status
```

> **QUIRK —** the destination of a parse is **one of two distinct structs**, not one config object. The `lea` that computes `&dest` targets either the **656-byte static singleton** `nrt_config` (`nrt_config_0 @0xc5c480`, base register `%rax` / `%rcx` = the global) — read everywhere as `nrt_config_0.field` — or the **296-byte heap buffer** `nrt_global_config` (`%r14` = `calloc`'d pointer), published to `ngc @0xc5c460` and read everywhere via `nrt_gconf()` (`@0x82670`, which is literally `return ngc;`). The same call-instruction shape (`lea OFF(base),%rdx; call nrt_config_parse`) writes both; only the base register tells them apart. A reimplementer who merges them into one struct will mis-place every field — e.g. `funtime` lives in *both* (at `cfg+337` and `gcfg+184`, each set independently), and the FP8 numeric fields are `gcfg+252..+260` while `fail_on_nan` is `cfg+252`. The two structs and their full member lists are derived in [config-structs](config-structs.md).

> **NOTE —** `getenv` is reached through exactly **one** PLT slot (`getenv@GLIBC_2.2.5 @0x3cfd0`, 43 call sites) for the entire Neuron config surface — there is no scattered `getenv` use. (`secure_getenv @0x3d9d0` exists but its 4 call sites are all inside vendored `std::filesystem::temp_directory_path`, not Neuron knobs.) This single-funnel property is what lets the env catalogue be complete: every `NEURON_RT_*` read is a typed-helper call whose name argument is a `.rodata` literal. The exhaustive 138-name table — name, parser, target field, default, type — is owned by [env-vars](env-vars.md) and not reproduced here.

### getenv-callsite → config-field map (representative)

This is a **sample** of the getenv→field bindings to show the pattern (callsite address inside the master parser, the typed helper, the destination struct+field, and the recovered default). The full catalogue is in [env-vars](env-vars.md); the full field layouts in [config-structs](config-structs.md). `cfg.` = the 656-byte static `nrt_config`; `gcfg.` = the 296-byte `*ngc`.

| Env Variable | Callsite | Helper / Parser | Destination Field | Default | Conf |
|---|---|---|---|---|---|
| `NEURON_RT_NUM_CORES` | `0x8a74c` | `nrt_config_parse(uint)` | `cfg.num_cores` (`+12`) | **0** | HIGH |
| `NEURON_LOGICAL_NC_CONFIG` | `0x8a683` | `parse_vnc_config` | `cfg.virtual_core_size` (`+8`) | **2** (1 if 1 core/dev) | HIGH |
| `NEURON_RT_VISIBLE_CORES` | `0x8a845` | `parse_visible_virtual_cores` | `cfg.visible_virtual_cores` (`+16`) | **—** (empty) | HIGH |
| `NEURON_RT_FAKE_INSTANCE_TYPE` | `0x8379f` | `nrt_config_parse_funtime` (direct `getenv`) | `cfg.funtime` / `fake_family` / `fake_size` (`+337` / `+340` / `+344`) | **—** (unset) | HIGH |
| `NEURON_RT_RESET_CORES` | `0x8acbd` | `nrt_config_parse(bool)` | `cfg.reset_cores` (`+64`) | **1** | HIGH |
| `NEURON_RT_UCODE_LIB_PATH` | `0x8e0a8` | `nrt_config_parse(char*)` | `cfg.neuron_ucode_path` (`+224`, `strdup`) | **NULL** | HIGH |
| `NEURON_RT_DBG_DISABLE_DGE` | `0x8d397` | `nrt_config_parse(uint)` | `gcfg.dbg_disable_dge` (`+28`) | **0** | HIGH |
| `NEURON_RT_DBG_DISABLE_DGE_BOUND_CHECK` | `0x8d47d` | `nrt_config_parse(uint)` | `gcfg.dge_disable_bound_check` (`+32`) | **0** | HIGH |
| `NEURON_RT_CC_CHUNK_SIZE` | `0x8c3e3` | `nrt_config_parse(uint)` | `cfg.cc_chunk_size` (`+196`) | **0x200000** | HIGH |
| `NEURON_RT_DBG_DMA_PACKETIZATION_SIZE` | `0x8dfba` | `nrt_config_parse(uint)` | `cfg.dma_packetization_size` (`+212`) | **0x1000** (cap `0xFFFFF`) | HIGH |
| `NEURON_RT_ASYNC_EXEC_MAX_INFLIGHT_REQUESTS` | `0x8f10a` | `nrt_config_parse(uint)` | `gcfg.async_exec_max_inflight_requests` (`+24`) | **0** (clamp 63) | HIGH |
| `NEURON_RT_LOCAL_CORE_DUMP_DIRECTORY` | init | `nrt_config_parse(char*)` | `gcfg.local_core_dump_directory` (`+168`) | `"/tmp/neuron-core-dump/dt-%d-cid-%c"` | MEDIUM |

> **GOTCHA —** the parsed default is the **literal 2nd argument** at the call site, but several knobs override it *arch-dependently* after the parse — e.g. `NEURON_RT_DBG_HW_DECODE_TOGGLE` defaults to `11` / `3` / `0` by arch (4 / 3 / else), and `NEURON_RT_DBG_INTRA_RDH_CHANNEL_BUFFER_SIZE` to 40 MB / 80 MB by arch. A reimplementer who treats the inline default as final will mis-configure these on the wrong silicon; the arch-dependent overrides are noted per-knob in [env-vars](env-vars.md). A second trap: a handful of knobs (`dbg_disable_dge`, `dge_disable_bound_check`) are **DMA-bounds-check bypasses** — security-relevant, since a NEFF with an out-of-range index can drive an out-of-bounds device DMA when the bound check is disabled; treat their defaults (`0` = checks on) as the safe baseline.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `nrt_config_parse_init_config` | `0x8a1d0` | 23088 | Master `NEURON_RT_*` → cfg/gcfg parser (`nrt_init`-only) | HIGH |
| `nrt_config_parse` (6 typed overloads) | `0x7ff20`/`0x81120`/`0x813d0`/`0x81a90`/`0x80720`/`0x84fe0` | — | `getenv` → default → typed store into `*dest` | HIGH |
| `nrt_config_parse_funtime` | `0x83760` | 879 | `NEURON_RT_FAKE_INSTANCE_TYPE` → `funtime` / `fake_*` | HIGH |
| `get_enc_proxy_histogram_config` | `0x847f0` | — | 5-token `istringstream` grammar → `cc_proxy_histogram_config` (`+480`) | MEDIUM |
| `parse_low_latency_tasks_cpu_affinity` | `0x85c70` | — | env → `cpu_set_t` mask (`cfg+352`, 128 B) | MEDIUM |
| `nrt_gconf` | `0x82670` | — | `return ngc;` — the 296-B struct accessor | CERTAIN |
| `nrt_config_free` | `0x82680` | — | `free(ngc); ngc=0` (teardown) | HIGH |
| `std::vector<NRT_STATUS>::emplace_back` | `0x85f20` | 37 | Collect per-knob status; parser returns the max | HIGH |

### Considerations

The master parser returns the **worst** `NRT_STATUS` across all knobs (each parse's status is `emplace_back`'d and `max`'d), so a single invalid env value fails `nrt_init` rather than being silently ignored. Three knobs carry validation that aborts init outright: `NEURON_RT_DEBUG_STREAM_BUFFER_SIZE` must be a power of two, `NEURON_RT_DBG_DMA_PACKETIZATION_SIZE` is capped at `0xFFFFF`, and `ranks_per_network_proxy > 1` requires a HW barrier. The `get_enc_proxy_histogram_config` parser (`@0x847f0`) is the one knob with a *string grammar* rather than a scalar: it `istringstream`-splits `"bucket_usecs num_buckets per_neff_warmup warmup output_path"` into exactly 5 tokens via `std::getline`, throwing `length_error "Output path too long"` if the path exceeds 127 bytes — the grammar and the `enc_proxy_histogram_config_t` (168 B) layout are derived in [config-structs](config-structs.md). The `cached_info` / `nrt_config_0` / `ngc` globals are all `.bss`-resident at their VMAs (identity-mapped; no `0x400000` delta), so a verifier can read them at the cited addresses directly.

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_init` (`0x94e90`) | The sole caller of `nrt_config_parse_init_config`; consumes the count APIs and the cfg/gcfg structs during LNC allocation |
| `vtpb_*_no_state` family | The cross-cutting ptpb↔vtpb translation primitives every count API and the LNC allocator share |
| `nrt_config_parse_inspect_config` (`0x86760`) | Sibling parser for `NEURON_RT_INSPECT_*`; same getenv→field pattern, own destination (`nrt_inspect_config_t`) |
| `nrt_config_parse_model_config` (`0x85f50`) | Per-model knob parser called from the load/schedule path, not from init |
| `nrt_fake_dev_info` / `tdrv_get_dev_info` | The two device-query backends `nrt_get_dev_info` dispatches between on `funtime` |

## Cross-References

- [Configuration: nrt_config and nrt_global_config](config-structs.md) — the full 73-member / 47-member struct layouts, the two-struct model, and the histogram / affinity sub-structs this page only samples
- [Environment Variable Catalog (NEURON_RT_*)](env-vars.md) — the exhaustive 138-name env table (name → parser → field → default → type) and the arch-dependent default overrides
- [Public C API: Lifecycle and Init/Teardown](api-lifecycle.md) — `nrt_init`'s call into the env parser, the `NRT_INIT_STATE` guard that gates the cached visible count, and the LNC-reservation use of the same `vtpb_*` translation
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](../arch/generations-enum.md) — the instance-family enum (CAYMAN `EPIC-TGH7` → 5, MARIANA → 14) and the codename↔cloud-name mapping `nrt_get_instance_info` resolves
- [Overview](overview.md) — where this device/config surface sits in the six-layer runtime-core map (layer [1], `nrt/nrt_config.cpp` + `utils/instance_helpers.c`)
