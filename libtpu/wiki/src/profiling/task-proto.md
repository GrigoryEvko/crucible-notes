# Task Proto

> *All addresses, field numbers, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — full C++ symbols are present, and `.text`/`.rodata`/`.lrodata` VMA equals file offset. Other versions will differ.*

## Abstract

The `tensorflow.profiler.Task` proto is the xprof profiler's **per-worker device-metadata record**: one message per host/worker that pins down *who* captured a profile (build provenance, host identity, the launch command line), *when* (the profile window), and — the part that matters to the device-trace timeline — *at what clock rates* the silicon was running. It is a flat proto3 message of 18 singular scalar fields, carved byte-exact from the descriptor pool `FileDescriptorProto` @ `0xbe999a0` (path `third_party/xprof/plugin/tensorboard_plugin_profile/protobuf/task.proto`, package `tensorflow.profiler`). At runtime it is the *value* of a `Map<uint32 task_id, Task>` — one record keyed by worker ordinal — read by the cheap-op gate `xprof::XlaJfProfileCheapOps(Task const&)` @ `0xf2ca280`.

Three of the 18 fields are clock rates: `tensor_core_freq_hz` (f11), `sparse_core_freq_hz` (f12), and `gtc_freq_hz` (f13), all `uint64`. These are the runtime companions to the clock-domain conversion documented on [riegeli Trace Container](riegeli-trace-container.md): every device-plane `XEvent` offset is `round(gtc · 1e9 / (clk << 4))`, and the divisor `clk` is a GTC frequency. This page supplies the *constants*; the divide math and the resync-aware piecewise converter live on the riegeli page. There is one subtlety a reimplementer must internalize up front, and it is a [correction](#the-baked-vs-runtime-clock-authority) to the naive reading: the in-`libtpu` converter does **not** read `Task.gtc_freq_hz` — it loads a **baked per-DeviceType kHz table** at `.lrodata 0x1c60480`. The Task proto's `*_freq_hz` fields are the captured-profile record of the same constants, surfaced for post-hoc tooling.

The Task proto is *identity and clocks*, not device telemetry. Contrast it with the [tpu-telemetry proto](tpu-telemetry-proto.md), which is a periodic device-state snapshot (power, throttle, ECC, temperatures): telemetry answers "what is the chip doing right now"; Task answers "which build, which host, which clock domain produced this profile". The profiler surfaces the Task fields as `XStat`s on the `kTaskEnvPlaneName` host plane via a 17-entry `TaskEnvStatType` dictionary; the [XPlane/XStat/TraceMe](xplane-xstat-traceme.md) page owns the plane-assembly mechanics, this page owns the Task→XStat field mapping. Notably, the chip's PCI identity and topology coordinates are **not** scalar Task fields — chip identity rides the `DeviceIdentifiers` PCI tuple in the device-trace container, and topology is a free-form `system_topology` XStat string.

For reimplementation, the contract is:

- **The 18-field Task proto schema** — field number → name → proto type → semantic group, all byte-exact from the descriptor pool, including the three clock-rate fields f11/f12/f13.
- **The clock-constant role** — which field feeds the device timebase (`gtc_freq_hz`), which is the *actual* converter input (the baked kHz table, not the proto field), and the resulting trace-wrap period.
- **The 17-entry `TaskEnvStatType` XStat dictionary** — the Task→host-plane surface mapping, recovered from the str→enum init list @ `0x21c20f00`.
- **The identity boundary** — what identity *is* in this proto (host/build/command line) versus what lives elsewhere (PCI chip identity, topology).

| | |
|---|---|
| **Proto** | `tensorflow.profiler.Task` — proto3, 18 singular scalar fields |
| **Descriptor** | `FileDescriptorProto` @ `0xbe999a0` (path `…/protobuf/task.proto`, 417-byte `DescriptorProto`) |
| **Runtime table** | `Task::_table_` @ `0x2164fed0`; `Task_globals_` @ `0x22266028` |
| **Container** | `Map<uint32 task_id, Task>` (one record per worker; `TryEmplaceInternal` @ `0xf2fa900`) |
| **Consumer** | `xprof::XlaJfProfileCheapOps(Task const&)` @ `0xf2ca280` (reads clocks/version) |
| **Clock fields** | f11 `tensor_core_freq_hz`, f12 `sparse_core_freq_hz`, f13 `gtc_freq_hz` (all `uint64`) |
| **Clock field strings** | `tensor_core_freq_hz` @ `0xbe99b16`, `sparse_core_freq_hz` @ `0xbe99b33`, `gtc_freq_hz` @ `0xbe99b50` |
| **Actual converter input** | baked per-DeviceType kHz table @ `.lrodata 0x1c60480` (stride `0x448`, `int32 @+4`) |
| **Host XStat dictionary** | 17-entry `TaskEnvStatType`, str→enum init list @ `0x21c20f00`; plane `kTaskEnvPlaneName` @ `0x21c9e0e8` |

---

## The Task Proto Schema

### Purpose

The Task message is profiler session metadata: a `Map<uint32 task_id, Task>` populated once per profiling session, one entry per host/worker. The `uint32` key is the worker ordinal; the value carries the build that produced the binary, the host that ran it, the time window the profiler captured, the per-engine clock rates, and the host resource caps. The map type is byte-confirmed from the `MapEntryFuncs<uint32, Task, FieldType 13, FieldType 11>` / `Map<uint32, Task>::TryEmplaceInternal` @ `0xf2fa900` symbols — `FieldType 13` is the `uint32` key, `FieldType 11` is the embedded-message value.

### Field Map

All 18 fields are singular (label 1) proto3 scalars, carved from the 417-byte `DescriptorProto` (message_type[0] inside the `FileDescriptorProto` @ `0xbe999a0`, field-4 at `+0x5e`) and cross-checked by `protoc --decode_raw`. Proto type codes: `1=double`, `3=int64`, `4=uint64`, `8=bool`, `9=string`, `13=uint32`. Every field name was confirmed verbatim against `.rodata` (see [Field-String Anchors](#field-string-anchors)).

| f# | Name | Type | Group | Meaning | Confidence |
|---|---|---|---|---|---|
| 1 | `changelist` | int64 | build | source revision the binary was built from | CERTAIN |
| 2 | `clean_build` | bool | build | clean vs dirty workspace at build time | CERTAIN |
| 3 | `build_time` | int64 | build | unix build timestamp | CERTAIN |
| 4 | `build_target` | string | build | build-target label (serves as the version string) | CERTAIN |
| 5 | `command_line` | string | host | the launch command-line args | CERTAIN |
| 6 | `start_time` | int64 | host | process start time | CERTAIN |
| 7 | `task_address` | string | host | worker BNS / network address | CERTAIN |
| 8 | `profile_time_ns` | uint64 | window | profile start wall-clock, ns | CERTAIN |
| 9 | `profile_duration_ms` | uint32 | window | profile length, ms | CERTAIN |
| 10 | `host_trace_level` | uint32 | window | host `TraceMe` verbosity level | CERTAIN |
| **11** | **`tensor_core_freq_hz`** | **uint64** | **clock** | **TensorCore cycle clock (Hz)** | **CERTAIN** |
| **12** | **`sparse_core_freq_hz`** | **uint64** | **clock** | **SparseCore cycle clock (Hz)** | **CERTAIN** |
| **13** | **`gtc_freq_hz`** | **uint64** | **clock** | **Global Time Counter clock (Hz) — the timebase divisor** | **CERTAIN** |
| 14 | `peak_memory_usage` | uint64 | resource | peak host RSS | CERTAIN |
| 15 | `cpu_limit` | double | resource | Borg CPU limit | CERTAIN |
| 16 | `cpu_usage` | double | resource | Borg CPU usage | CERTAIN |
| 17 | `workspace_id` | string | build | workspace identifier | CERTAIN |
| 18 | `snapshot` | int64 | build | workspace snapshot id | CERTAIN |

The five semantic groups are not declared in the proto — they are the natural partition of the field set and the same grouping the [host XStat dictionary](#the-taskenvstattype-xstat-dictionary) follows: **build provenance** (1, 2, 3, 4, 17, 18), **host/task identity** (5, 6, 7), **profile window** (8, 9, 10), **clock rates** (11, 12, 13), **resource caps** (14, 15, 16).

> **NOTE —** the numbering is not contiguous-by-group: `workspace_id` (17) and `snapshot` (18) are build-provenance fields appended after the resource caps, the usual sign of fields added in a later proto revision. A reimplementer must key on the field *number*, not on position-in-group, because the wire tag is the number.

### What Identity Is — and Is Not — Here

The Task proto's identity fields are **host/build identity**, not **chip identity**:

- **Host identity** is `task_address` (f7, the worker BNS/address), `command_line` (f5), and `start_time` (f6). A `uint32 task_id` map key disambiguates workers within a session.
- **Build identity** is `changelist`/`build_time`/`build_target`/`clean_build`/`workspace_id`/`snapshot` — the version provenance of the libtpu binary itself.

The chip-level identity a reimplementer might expect here — PCI vendor/device tuple, chip coordinates, device ordinal — is **not** in this proto:

> **GOTCHA —** the chip's PCI identity is the 12-byte `asic_sw.proto.DeviceIdentifiers` tuple carried in the *device-trace container* (`JfTrace.device_identifiers`, field 19), not a Task field. The fleet/topology coordinates are surfaced as the free-form `system_topology` XStat string (`TaskEnvStatType` 15), not as structured Task fields. A reimplementation that looks in the Task proto for chip coords or a device ordinal will find none — those belong to the [device-trace container](riegeli-trace-container.md#the-key--deviceidentifiers)'s `DeviceIdentifiers` keying and the host-plane topology XStat, respectively.

### Field-String Anchors

The field names sit consecutively in `.rodata` immediately after the `DescriptorProto`. The three clock-rate fields are the load-bearing anchors for the timebase and were confirmed byte-exact:

| Field string | `.rodata` addr | Status |
|---|---|---|
| `tensor_core_freq_hz` | `0xbe99b16` | CERTAIN (verbatim in binary) |
| `sparse_core_freq_hz` | `0xbe99b33` | CERTAIN (verbatim in binary) |
| `gtc_freq_hz` | `0xbe99b50` | CERTAIN (verbatim in binary) |

The full name run spans `0xbe99a03`–`0xbe99b50` (`Task`, `changelist`, … `gtc_freq_hz`); the path string `…tensorboard_plugin_profile/protobuf/task.proto` and package `tensorflow.profiler` are both present verbatim, anchoring the `FileDescriptorProto` at `0xbe999a0`.

---

## The Clock-Rate Fields and the Timebase

### Purpose

The three `uint64` clock-rate fields (f11/f12/f13) are the runtime clock constants that the device-trace pipeline needs to turn a raw hardware tick into wall-clock picoseconds. Each engine — TensorCore, SparseCore — has its own cycle clock for converting a per-engine duration-cycle count to time; the Global Time Counter has a third, chip-wide clock. The conversion *math* that consumes these constants is owned by [riegeli Trace Container](riegeli-trace-container.md#the-gtc-tick--picosecond-timebase); this section states precisely which constant plays which role and where the *actual* runtime value comes from.

### The Roles

| Field | f# | Domain | Role in the timebase |
|---|---|---|---|
| `gtc_freq_hz` | 13 | chip-wide Global Time Counter | **the device-timeline divisor**: every device-plane `XEvent` offset is `round(gtc · 1e9 / (gtc_freq_hz << 4))` |
| `tensor_core_freq_hz` | 11 | TensorCore cycle clock | converts a TensorCore per-event *duration-cycle* count to time |
| `sparse_core_freq_hz` | 12 | SparseCore cycle clock | converts a SparseCore per-event duration-cycle count to time |

The `gtc_freq_hz` role is the central one: the `TraceHeader.timestamp` on every 16-byte trace packet is a GTC tick in ×16 fixed-point (low 4 bits fractional), and `TpuXLineBuilder::AddEvent(GtcSpan)` @ `0xf1df1e0` divides it by `gtc_freq · 16` to land in picoseconds. The two ×16 factors — one in the tick, one in the `<< 4` of the divisor — cancel exactly. The per-engine `tensor_core_freq_hz`/`sparse_core_freq_hz` are inferred from the field names to scale the per-band duration-cycle counts the codec decodes; the precise per-subsystem subscriber that divides by each was not traced end-to-end (LOW confidence on the exact per-payload binding; the field *names and types* are CERTAIN).

> **NOTE —** the GTC clock is what makes the device timeline *globally* coherent. Because the GTC is chip-wide and cross-chip synchronized (programmed by `SetGtcConfiguration`), one frequency converts every core's and every chip's events onto a single wall-clock axis. The per-engine TensorCore/SparseCore clocks, by contrast, are only needed to expand a *relative* duration on an event that is already placed by the GTC. This is why the GTC clock is the divisor in the offset placement and the engine clocks scale only durations.

### The Baked-vs-Runtime Clock Authority

There are two sources of the GTC frequency in the binary, and a reimplementer must understand which one the in-`libtpu` converter actually uses:

1. **The runtime `Task.gtc_freq_hz` (f13, `uint64` Hz)** — captured into the profile at session time, this proto field.
2. **A baked per-DeviceType table** at `.lrodata 0x1c60480` (stride `0x448` per DeviceType, `int32 @+4` = GTC freq in **kHz**, `int32 @+8` = timestamp bit-width 48/45/64). The values are `700000` kHz (pxc/jellyfish-class, 48-bit ts), `800000`/`833000` kHz (vxc/gxc 45-bit families), and `1333000` kHz (a 64-bit-ts jellyfish-class family).

> **CORRECTION (P-3-406 vs P-3-399) —** the GtcSpanConverter's clock divisor is loaded from the **baked per-DeviceType kHz table**, not from `Task.gtc_freq_hz`. The converter ctor @ `0xf2cb6e0` does `clk = table_base[DeviceType*0x448 + 4]` unconditionally; the divide in `AddEvent` is `gtc · 1e9 / (clk_khz << 4)`, and the kHz unit is *proven* by byte arithmetic (`gtc_x16=16` → 700000 kHz = 1428.571 ps, 800000 = 1250.000 ps, 833000 = 1200.480 ps, 1333000 = 750.188 ps, all matching `ticks/(clk_khz·1000)·1e12`). The Task proto's `gtc_freq_hz` is the **captured-profile companion** of the same constant — the on-the-wire record of the clock, surfaced for downstream tooling — not the value the in-binary converter divides by.

This matters two ways for a reimplementer:

- **If you are reimplementing the in-libtpu device→XPlane conversion**, drive the divide from the per-DeviceType kHz table keyed on the DeviceType ordinal, exactly as the binary does. Reading `Task.gtc_freq_hz` and dividing by it (in Hz, not kHz) would be off by 16× from the fractional scaling *and* would use the wrong source.
- **If you are reimplementing a post-hoc trace reader** that only has the serialized profile, `Task.gtc_freq_hz` is your authority — the baked table is internal to libtpu and unavailable to an external reader. The two should agree numerically (`gtc_freq_hz` Hz == `clk_khz · 1000`).

Whether a captured profile's runtime `Task.gtc_freq_hz` ever *overrides* the baked clk in the converter ctor was **not** observed — the ctor loads the baked table with no runtime-Hz override path in view (INFERRED; the `*_freq_hz` fields may exist purely for the post-hoc reader). Marked LOW confidence on the override question; CERTAIN that the in-libtpu converter uses the baked kHz table.

### Trace-Wrap Period

The same clock constant fixes how long the GTC runs before its fixed-width counter wraps. With the timestamp bit-width `w` from the baked table (`+8`: 48 / 45 / 64) and the GTC frequency:

```text
wrap_period_s = 2^w / gtc_freq_hz
              = 2^48 / 700e6  ≈ 4.0e5 s  (pxc/vlc, 48-bit ts, 700 MHz)
              = 2^45 / 800e6  ≈ 4.4e4 s  (vfc/glc/gfc, 45-bit ts, 800 MHz)
```

A reimplementer parsing a long trace must account for this wrap; the resync-aware piecewise converter (`GtcSpanConverter::TimespanFromGtcSpan` @ `0xf2cb7e0`, owned by the [riegeli page](riegeli-trace-container.md#the-conversion--addeventgtcspan)) handles GTC resets mid-trace via a binary search over `{walltime_ns, gtc}` calibration anchors, precisely because a single flat divide cannot survive a counter wrap or resync.

---

## The TaskEnvStatType XStat Dictionary

### Purpose

The profiler does not surface the Task proto as a proto on the timeline — it renders selected Task fields (plus a few derived values) as `XStat`s on the `kTaskEnvPlaneName` host plane (plane name @ `0x21c9e0e8`, of the form `/host:<n>`). The mapping from a stat name to its enum id is the 17-entry `TaskEnvStatType` enum, recovered from the str→enum init list @ `0x21c20f00`. The [XPlane/XStat](xplane-xstat-traceme.md) page owns how a plane and its `XStatMetadata` are built; this dictionary is the Task-specific surface.

### The Dictionary

17 records, stride `0x18` (enum at `+0x10` = values 1..17, string `{ptr@+0, len@+8}` with the ptr relocated at load via `R_X86_64_RELATIVE`). Every name was confirmed verbatim in `.rodata`. The "← Task field" column is the provenance of each stat:

| Enum | XStat name | `.rodata` str | ← Task field / derivation | Confidence |
|---|---|---|---|---|
| 1 | `build_changelist` | `0x84d97b6` | `changelist` (f1) | CERTAIN |
| 2 | `build_snapshot` | `0x84e35fb` | `snapshot` (f18) | CERTAIN |
| 3 | `build_workspace_id` | `0x86fd57b` | `workspace_id` (f17) | CERTAIN |
| 4 | `clean_build` | `0x86f91d7` | `clean_build` (f2) | CERTAIN |
| 5 | `build_time` | `0x86bf23b` | `build_time` (f3) | CERTAIN |
| 6 | `build_target` | `0x8503333` | `build_target` (f4) | CERTAIN |
| 7 | `command_line_args` | `0x854efee` | `command_line` (f5) | CERTAIN |
| 8 | `process_start_time` | `0x86be930` | `start_time` (f6) | CERTAIN |
| 9 | `task_bns` | `0x8544af9` | `task_address` (f7) | CERTAIN |
| 10 | `profile_start_time` | `0x86beaa0` | `profile_time_ns` (f8) | CERTAIN |
| 11 | `profile_stop_time` | `0x86bec17` | `profile_time_ns` + `profile_duration_ms` (f8+f9) | HIGH |
| 12 | `peak_memory_usage` | `0x86d7111` | `peak_memory_usage` (f14) | CERTAIN |
| 13 | `borg_cpu_limit` | `0x84f9429` | `cpu_limit` (f15) | CERTAIN |
| 14 | `borg_cpu_usage` | `0x86d7123` | `cpu_usage` (f16) | CERTAIN |
| 15 | `system_topology` | `0x84b9c7f` | topology coords (free-form string, not a Task scalar) | HIGH |
| 16 | `compilation_task_info` | `0x85db5e3` | compiler/version metadata | HIGH |
| 17 | `profile_options` | `0x85363f2` | the requested profile options | HIGH |

The mapping is mostly 1:1 with Task fields, with three derived/external entries: `profile_stop_time` (11) is computed (`start + duration`), and `system_topology` (15) / `compilation_task_info` (16) / `profile_options` (17) come from elsewhere in the profiler session, not from scalar Task fields. Names 1–14 are byte-exact projections of Task; 15–17 are HIGH-confidence on the *binding to a source* (the strings are byte-exact, the exact producer was not traced for the three non-Task entries).

> **QUIRK —** the clock-rate fields f11/f12/f13 are **not** in this host-plane dictionary. The clocks feed the *device-plane* timebase (the GTC→ps divide), not the host Task-environment plane. A reimplementation that expects a `gtc_freq_hz` XStat on the host plane will not find one — the clocks are consumed numerically by the converter, never surfaced as a host-plane attribute. The host plane carries only identity/version/window/resource facts.

### The Lookup Mechanism

Both directions (name→enum and enum→name) are an `absl::flat_hash_map` built once and cached:

```c
// GetTaskEnvStatTypeStr @0x1c8eb8c0 / FindTaskEnvStatType @0x1c8eba20
//   forward map (str->enum) @0x22579b30, reverse (enum->str) @0x22579b20, guard @0x22579b38
function BuildTaskEnvStatTypeMaps():                 // StrMap lambda @0x1c8ec5e0
    once (guard @0x22579b38):                         // call_once
        m = flat_hash_map(/*capacity*/ 17);            // mov $0x11,%edx @0x1c8ec770
        for (name, enum) in init_list @0x21c20f00:     // 17 records, stride 0x18
            m.insert(name -> enum);                    // ptr field relocated R_X86_64_RELATIVE
        forward = m; reverse = invert(m);
```

The init list's string-pointer field is `0` in the file image; the actual `.rodata` address is the `R_X86_64_RELATIVE` addend applied at load. The map capacity `17` (`0x11`) is hard-coded at `0x1c8ec770`, matching the enum cardinality.

---

## The Task Metadata Pipeline

```text
profiler session start
   │
   ▼
Map<uint32 task_id, Task>            (one Task per host/worker; TryEmplaceInternal @0xf2fa900)
   │  Task { changelist/build_*/snapshot/workspace_id     [build provenance]
   │         command_line/start_time/task_address          [host identity]
   │         profile_time_ns/profile_duration_ms/host_trace_level   [window]
   │         tensor_core_freq_hz/sparse_core_freq_hz/gtc_freq_hz     [clock rates]
   │         peak_memory_usage/cpu_limit/cpu_usage          [resource caps] }
   │
   ├──────────────► host plane (kTaskEnvPlaneName)
   │   surfaced as XStats via TaskEnvStatType (17 entries):
   │     build_changelist … task_bns … profile_start/stop_time …
   │     system_topology · compilation_task_info · profile_options
   │   (NB: the *_freq_hz clocks are NOT surfaced here)
   │
   └──────────────► device timebase  [riegeli-trace-container.md]
       gtc_freq_hz (f13) == the captured record of the GTC clock
          │  in-libtpu converter divides by the BAKED per-DeviceType kHz table @0x1c60480,
          │  not by this field — Task.gtc_freq_hz is the post-hoc-reader companion
          ▼
       TpuXLineBuilder::AddEvent(GtcSpan) @0xf1df1e0
          device_offset_ps = round(gtc · 1e9 / (clk_khz << 4))
```

---

## Relevant Symbols and Offsets

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `FileDescriptorProto` (task.proto) | `0xbe999a0` | the 417-byte descriptor the 18-field map is carved from | CERTAIN |
| Field-name run | `0xbe99a03`–`0xbe99b50` | `Task`, `changelist`, … `gtc_freq_hz` | CERTAIN |
| `Task::_table_` | `0x2164fed0` | runtime reflection table | CERTAIN |
| `Task_globals_` | `0x22266028` | proto default-instance globals | CERTAIN |
| `Map<uint32,Task>::TryEmplaceInternal` | `0xf2fa900` | the per-worker map insert | CERTAIN |
| `MapEntryFuncs<uint32,Task,…>` | `0xf2f8060` | map-entry serialization funcs (key=u32, value=Task) | CERTAIN |
| `XlaJfProfileCheapOps(Task const&)` | `0xf2ca280` | the consumer that reads clocks/version | HIGH |
| `TaskEnvStatType` init list | `0x21c20f00` | 17 `{str,enum}` records, stride `0x18` | CERTAIN |
| `kTaskEnvPlaneName` | `0x21c9e0e8` | the host Task-env plane name | CERTAIN |
| `GetTaskEnvStatTypeStr` | `0x1c8eb8c0` | enum→string accessor | CERTAIN |
| `FindTaskEnvStatType` | `0x1c8eba20` | string→enum accessor | CERTAIN |
| `TaskEnvStatType` map guard / fwd / rev | `0x22579b38` / `0x22579b30` / `0x22579b20` | the once-built `flat_hash_map`s | CERTAIN |
| Baked per-DeviceType GTC clk table | `0x1c60480` (`.lrodata`) | stride `0x448`; `+4` clk(kHz), `+8` ts-width | CERTAIN |
| `GtcSpanConverter::ctor(DeviceType)` | `0xf2cb6e0` | loads `clk` from the baked table — the actual divisor source | CERTAIN |
| `TpuXLineBuilder::AddEvent(GtcSpan)` | `0xf1df1e0` | the GTC→ps divide that consumes the clock | CERTAIN |

> **NOTE —** the Task proto's *population site* — the host-side function that writes `tensor_core_freq_hz`/`sparse_core_freq_hz`/`gtc_freq_hz`, the build/version fields, and the topology into a `Task` at profile time — was not located as a single setter. The consumer (`XlaJfProfileCheapOps` @ `0xf2ca280`) reads it; the producer is likely a host-side xprof session-info collector not present in the device-side decode path traced here (gap, not a guess).

---

## Related Components

| Component | Relationship |
|---|---|
| [riegeli Trace Container](riegeli-trace-container.md) | consumes the GTC clock constant this proto records; owns the `round(gtc · 1e9 / (clk << 4))` divide and the resync-aware piecewise converter |
| [tpu-telemetry proto](tpu-telemetry-proto.md) | the orthogonal device-state snapshot (power/throttle/ECC/temps); Task is profiler identity+clocks, telemetry is live device metrics — different stacks |
| [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) | owns the plane/`XStatMetadata` assembly that the 17-entry `TaskEnvStatType` dictionary surfaces Task fields onto |
| [Profiling and Telemetry Overview](overview.md) | the capture→encode→decode→xplane pipeline; the Task proto is the per-worker metadata record that frames a captured profile |

## Cross-References

- [riegeli Trace Container](riegeli-trace-container.md) — the timebase that divides by the GTC clock; this page supplies the `gtc_freq_hz` constant (and the baked-table correction)
- [tpu-telemetry proto](tpu-telemetry-proto.md) — contrast: device-state metrics vs this proto's profiler identity/clocks
- [XPlane / XStat / TraceMe](xplane-xstat-traceme.md) — the host-plane XStat assembly the `TaskEnvStatType` dictionary feeds
- [Profiling and Telemetry Overview](overview.md) — the five-stage device-trace pipeline this metadata record frames
