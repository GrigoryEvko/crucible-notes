# Error/Status String Templates

> *All offsets, symbols, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu_lts_20260413_b_RC00`, ELF x86-64, 781,691,048 bytes, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped). Other builds will differ.*

## Abstract

libtpu builds its diagnostics out of **format-string templates** baked into `.rodata` and filled at the call site. There are two formatting idioms and one wrapping convention. The dominant idiom is C-printf-style — `absl::StrFormat("…%s…%d…", args)` and `LOG(...) << absl::StrFormat(...)` — which accounts for the large majority of the surface; the minority idiom is absl positional substitution — `absl::Substitute("…$0…$1…", args)` — confined mostly to the megascale DCN runtime, the collective buffer-size validators, and a statically-linked protobuf descriptor validator. Most of these templates are then wrapped in an `absl::Status` by one of three factory families (see [Status-Code Mapping](#status-code-mapping)), so the same string is simultaneously the human-readable error and the payload of a `StatusOr<T>` that propagates back through PJRT to a JAX/XLA user.

This page is a **reference catalog**, not an algorithm trace. Its value is the grouped, deduplicated table of *real* templates with their placeholders explained and their `absl::Status` code attributed where that attribution is byte-confirmed. The recovered surface is roughly 2,937 distinct error/status templates; this page does not reproduce all of them — it documents the *structure* of the space (the placeholder grammar, the subsystem partition, the three Status-construction idioms, the byte-confirmed argument types) and gives a representative, source-anchored sample per subsystem so a reimplementer can predict the shape of any template not listed.

Placeholder semantics are the through-line. A `%s` is almost never a raw C string: it is some object's `ToString()` / `AbslStringify` — a `Shape`, a `Layout`, an `HloInstruction` name, a device name, an opcode mnemonic. A `%d`/`%lld`/`%zu` is a dimension, an ordinal, a count, or a byte size. Pointers (`%p`) and floats (`%f`) appear only in low-level driver and cost-model paths. The catalog calls out, per template, what each placeholder *means*, because the printf type alone (`%s` = `char*`) hides the real C++ argument.

> **NOTE —** the *error/status* templates here are distinct from user-facing **hint** strings (suggestions phrased as "try…", "consider…", not failures), which live on [`hint-strings.md`](hint-strings.md), and from the internal **pass-name** strings (pipeline-stage identifiers, not diagnostics) on [`internal-pass-names.md`](internal-pass-names.md). This page owns the error/status format-string catalog, the placeholder semantics, and the status-code mapping.

| | |
|---|---|
| **Distinct error/status templates** | ~2,937 (~2,799 printf-style, ~138 positional `$N`/`%v`) |
| **Status-construction idioms** | 3 (`<Code>StrCat` factory, `MakeErrorStream`, prose `absl::<Code>Error`) |
| **Byte-confirmed arg-type factories** | 99 `xla::<Code>StrCat<Types…>` instantiations |
| **`MakeErrorStream` return-type ops** | 390 `MakeErrorStreamWithOutput<T>` (one per `StatusOr<T>`) |
| **Top placeholders** | `%s` (~2778), `%d` (~2031), `%u` (116), `%zu` (84), `%x` (55), `%lld` (52) |
| **Largest subsystem block** | Compile / HLO / verifier (~875 candidates) |
| **Spot-confirmed against decompile** | 12 of 12 representative templates (see report) |

---

## How To Read This Catalog

### The two formatting idioms

```text
printf-style   "Argument to Cholesky must have rank >= 2; shape was %s"
               filled by  absl::StrFormat(template, shape.ToString())
               or         LOG(ERROR) << absl::StrFormat(template, …)

positional     "ALL_TO_ALL not supported when buffer size is not divisble
                by number of endpoints. Buffer Size: $0 Number of
                endpoints: $1. MegascaleInfo: $2"
               filled by  absl::Substitute(template, size, n, info)
```

The printf surface uses `%`-specs; the positional surface uses `$0`, `$1`, … (and a handful of `%v`, the absl stringify-any sigil). The two never mix inside one template. A reimplementer keying off `%`-specs alone will miss the ~138 positional templates entirely.

### Placeholder grammar

The full `%`-spec occurrence distribution across all templates is heavily skewed toward strings and ints:

| Spec | Count | C type | Real C++ argument (typical) |
|---|---:|---|---|
| `%s` | ~2778 | `char*` | a `ToString()` / `AbslStringify` result — Shape, Layout, instruction name, device name, opcode |
| `%d` | ~2031 | `int` | a dimension index, ordinal, count, status/state enum |
| `%u` | 116 | `unsigned` | a count or index that cannot be negative |
| `%zu` / `%zd` | 84 / 19 | `size_t` / `ssize_t` | a byte size or element count |
| `%x` / `%#x` / `%02x` | 55 / 19 / 11 | `unsigned` (hex) | an address fragment, a register/bit mask, a chip ID byte |
| `%lld` / `%ld` / `%lu` / `%llu` | 52 / 48 / 40 / 4 | `long long` / `long` / `unsigned long` | a large byte size or 64-bit count |
| `%p` | 32 | `void*` | a buffer/driver-object pointer (driver + cost-model paths only) |
| `%c` | 24 | `char` | a brace/bracket literal or an axis letter (`'{'`, `'}'`, `'X'`) |
| `%f` | 19 | `double`/`float` | a ratio/fraction (cost model, hbm-fraction config) |
| `%v` | 6 | absl-stringify | an object with an `AbslStringify` overload (e.g. `TensorCoreBundle`) |

> **GOTCHA —** `%s` does *not* tell you the argument is a string in the program. It tells you the call site passed a `const char*`, which is overwhelmingly the product of an object's `ToString()`. When reimplementing, the real type behind a `%s` is recoverable only at the call site (e.g. whether the operand of "%s vs. %s" is a `Shape::ToString()` or an `HloInstruction::name()`); the catalog notes the likely type but does not byte-confirm it for the printf family.

### Confidence and attribution

Templates marked **CERTAIN** were spot-confirmed verbatim in the decompiled call site (see the report). The *subsystem* grouping is a keyword classification of the prose, not a per-template call-site trace; it is **HIGH** confidence for the prose content and **MEDIUM** for the exact owning pass. The **status code** column is **CERTAIN** only for the 99 `<Code>StrCat` factories (the mangled symbol names the code) and for the 390 `MakeErrorStream` sites (the macro names the code); for every other template the code is **inferred** from the prose and the dominant idiom of its subsystem, and is marked **MEDIUM** or **LOW** accordingly.

---

## Compile / HLO / Codegen / Verifier — ~875 templates

The single largest block. These are the shape-inference and HLO-verifier diagnostics a JAX/XLA user hits when a program is malformed: rank checks, dimension-range checks, shape-equality checks, layout mismatches. Almost all are `%s`/`%d`-heavy and almost all wrap `InvalidArgument` (a few RET_CHECK → `Internal`). The verifier's signature is the **`vs.` comparison style** — `"%d vs. %d"`, `"%s vs. %s"` — which appears wherever two quantities must match.

| Offset | Template (`%`-spec) | Placeholders | Code | Conf. |
|---|---|---|---|---|
| `0x857d595` | `Argument to Cholesky must have rank >= 2; shape was %s` | `%s`=Shape | InvalidArgument | CERTAIN |
| `0x857c6ef` | `Argument to symmetrize must have >= 2 dimensions, got %s` | `%s`=Shape | InvalidArgument | HIGH |
| `0x8580369` | `All reduced tensors must have the same dimension. Tensor 0 has shape %s, Tensor %d has shape %s` | `%s`=Shape, `%d`=index, `%s`=Shape | InvalidArgument | HIGH |
| `0x85803c9` | `All operands to AfterAll must be tokens; operand %d has shape %s` | `%d`=operand idx, `%s`=Shape | InvalidArgument | HIGH |
| `0x872a259` | `broadcast_dimensions contains invalid value %d for result with rank %d` | `%d`=value, `%d`=rank | InvalidArgument | CERTAIN |
| `0xa02f654` | `Broadcast dimension %d mismatch: %d != %d; %s and %s.` | `%d`=dim, `%d`/`%d`=sizes, `%s`/`%s`=Shapes | InvalidArgument | HIGH |
| `0xa02c26f` | `Cannot concatenate arrays that differ in dimensions other than the one being concatenated. Dimension %d in both shapes must be equal (or compatible): %s vs %s.` | `%d`=dim, `%s`/`%s`=Shapes | InvalidArgument | HIGH |
| `0xa02f9ba` | `Cannot bitcast types with undivisible bit-widths: %s => %s.` | `%s`/`%s`=PrimitiveType | InvalidArgument | HIGH |
| `0xa01cdc3` | `Bitcast requires a new on-device shape to have the same size of %d bytes, but got %d bytes.` | `%d`/`%d`=byte sizes | InvalidArgument | HIGH |
| `0xa030ae8` | `Cannot infer shape: attempting to index into non-tuple: %s.` | `%s`=Shape | InvalidArgument | HIGH |
| `0x857b3fa` | `sharding's tile count and device count does not match: %d vs. %d; shape=%s, sharding=%s` | `%d`/`%d`=counts, `%s`/`%s`=Shape/Sharding | InvalidArgument | HIGH |
| `0x858b317` | `Arguments to TriangularSolve have shapes with different ranks: %s vs. %s` | `%s`/`%s`=Shapes | InvalidArgument | HIGH |
| `0xa0a2a82` | `Binary op shape inference: %s; lhs: %s; rhs: %s is not implemented.` | `%s`=op, `%s`/`%s`=Shapes | Unimplemented | HIGH |
| `0x8728000` | `Binary op expects 2 operands, but got %d` | `%d`=count | RET_CHECK→Internal | HIGH |
| `0x858400c` | `Bad scalar opcode in slot 0, opcode: %d bundle: %v, bits: %s` | `%d`=opcode, `%v`=TensorCoreBundle, `%s`=bits | InvalidArgument | CERTAIN |
| `0x857b280` | `Cannot feed constants into bundle packer.  Copy them to registers first. instr=%s` | `%s`=instruction | Internal | HIGH |
| `0x8584e00` | `Cannot find a free bundle slot in bundle %s: %s` | `%s`/`%s`=bundle/reason | Internal | HIGH |

> **NOTE —** `0x858400c` ("Bad scalar opcode in slot 0…") resolves in the decompile to `platforms_deepsea::jellyfish::isa::DecoderBcsDf::DecodeScalar0Slot` (and a per-gen `DecoderJf` twin). Its `%v` is the `TensorCoreBundle`'s `AbslStringify`, and it is one of the 6 `TensorCoreBundle`-bearing `<Code>StrCat` factories (see [Arg-Type Decode](#status-code-mapping)). The per-generation decoder variants (gxc/gfc, gxc/glc, vxc) each emit their own copy, so near-identical templates with the same prose are *separate* rodata strings.

---

## Scheduler / Fuel / FIFO — ~15 templates

Latency-hiding scheduler, annotation-range checks, the `--xla_fuel` budget, and the on-device FIFO push/pop ordering. The annotation templates lean on `%c` for the literal brace/bracket characters; the FIFO templates chain `%s :: %s` to attach instruction context.

| Offset | Template | Placeholders | Code | Conf. |
|---|---|---|---|---|
| `0x8796ba8` | `annotation arg must be in correct order as given; expected %c{%d%c but got %c{%d%c` | `%c`=brace, `%d`=id | Internal | HIGH |
| `0x8571b33` | `annotation %c{%d%c is out of bounds` | `%c`=brace, `%d`=id | Internal | HIGH |
| `0x858a654` | `annotation range was not closed; expected %c}%c: %s` | `%c`=brace, `%s`=context | Internal | HIGH |
| `0x857fa91` | `async-done for %s must be scheduled before %s` | `%s`/`%s`=instructions | RET_CHECK→Internal | HIGH |
| `0x857fabf` | `async-done for %s must be scheduled on core %d before %s` | `%s`=instr, `%d`=core | RET_CHECK→Internal | HIGH |
| `0x858a9b8` | `Cannot schedule FIFO pop instruction when the FIFO is empty %s :: %s` | `%s :: %s`=instr context | Internal | HIGH |
| `0x857ad7e` | `Cannot schedule FIFO push instruction when the FIFO is full.  FIFO name: %s. (element count %d vs %d). %s :: %s%s` | `%s`=name, `%d vs %d`=counts | Internal | HIGH |
| `0xa02bf0c` | `Conflicting schedule type requirements in computation rooted at %s.` | `%s`=computation | Internal | HIGH |
| `0xa086733` | `Reference instruction %s was not found in the schedule.` | `%s`=instruction | Internal | HIGH |
| `0x862868f` | `GVN: Not replacing %s because GVN is out of fuel` | `%s`=instruction | (LOG, not Status) | MEDIUM |
| `0x862865f` | `halt before %s because lowering is out of fuel` | `%s`=instruction | (LOG, not Status) | MEDIUM |
| `0xa03ca6a` | `Illegal value for --xla_fuel. Saw %s, but expected token %s to be an integer.` | `%s`/`%s`=value/token | InvalidArgument | CERTAIN |

> **NOTE —** `0xa03ca6a` ("Illegal value for --xla_fuel…") was confirmed inside the `xla::MakeDebugOptionsFlags` flag-parsing closure — it is a flag-value validator, the error counterpart to the `--xla_fuel` flag documented on the flag-name side. The two "out of fuel" strings at `0x862865f`/`0x862868f` are diagnostic LOG output, not Status payloads (no factory wraps them); they are catalogued for completeness but should not be assumed to be recoverable as a Status code.

---

## MSA / Memory / Allocation — ~79 templates

Memory-space assignment, prefetch/alternate-memory, HBM defragmentation, and the heap allocator. Byte-size placeholders dominate (`%lld`, `%zu`). The two over-budget templates wrap `ResourceExhausted`; mismatch/verification templates wrap `Internal`.

| Offset | Template | Placeholders | Code | Conf. |
|---|---|---|---|---|
| `0xa030cd9` | `AllocateBufferForMemorySpace: Unsupported memory space: %s.` | `%s`=memory space | InvalidArgument | HIGH |
| `0x858aa58` | `Allocation (size=%lld) would exceed memory (size=%lld) :: %s :: %s` | `%lld`/`%lld`=sizes, `%s :: %s`=context | ResourceExhausted | HIGH |
| `0xa083c62` | `BufferAllocation::Slice for instruction %s at index %s cannot be determined at compile-time.` | `%s`/`%s`=instr/index | Internal | HIGH |
| `0x857ce50` | `DefineBuffer: Mismatch in memory spaces: %s vs %s` | `%s vs %s`=spaces | Internal | HIGH |
| `0x8584ecd` | `Error defragmenting HBM %s: %s` | `%s`/`%s`=region/reason | Internal | HIGH |
| `0xa1300d0` | `Failed to allocate %zu bytes. Memory limit: %zu bytes. Used: %zu bytes.)` | `%zu`×3=req/limit/used | ResourceExhausted | HIGH |
| `0x8728f50` | `Invalid HBM offset %d` | `%d`=offset | InvalidArgument | HIGH |
| `0x872cb26` | `Invalid memory space for input memory space colors: %d` | `%d`=color | InvalidArgument | HIGH |
| `0xa01cf08` | `Out of memory allocating %d bytes.` | `%d`=byte size | ResourceExhausted | CERTAIN |
| `0xa09a63e` | `Number of bytes %lld allocated must be a multiple of chunk size %lld.` | `%lld`/`%lld`=size/chunk | InvalidArgument | HIGH |
| `0x857eed1` | `Register allocator verification failure: live range %s; instruction %s` | `%s`/`%s`=range/instr | Internal | HIGH |
| `0xa02b12f` | `Scoped allocation with size %s and limit %s exceeded scoped %s limit by %s.` | `%s`×4=sizes/labels | ResourceExhausted | HIGH |

> **CORRECTION (P-3-201) —** the template `0xa13e8e5` "Failed to allocate node (%zu bytes). Memory limit: %zu [bytes]. Used: %zu [bytes].)" was grouped under MSA in the raw findings. Decompilation places it in `perfetto::protovm::RwProtoCursor::CreateNodeFromField` — it is the **Perfetto tracing library's** arena allocator, not the TPU memory-space assignment path. The genuine TPU heap-allocator over-budget string is `0xa1300d0` ("Failed to allocate %zu bytes…"), confirmed in `xla::AlignedAllocator::Allocate`. Do not attribute `0xa13e8e5` to TPU MSA.

---

## ICI / Collective — ~90 templates

Inter-chip-interconnect link health, routing, GTC synchronization, and the collective (all-reduce / all-gather / reduce-scatter / all-to-all) buffer-size validators. This block mixes printf-style ICI driver errors with the positional `$N` collective validators (which are megascale-tagged — see the next section). Codes lean `Internal` and `DeadlineExceeded`.

| Offset | Template | Idiom | Placeholders | Code | Conf. |
|---|---|---|---|---|---|
| `0xa0b3abc` | `Cannot find unicast link next hop routing table for link port %d.` | printf | `%d`=port | Internal | HIGH |
| `0xa030c51` | `Coordinate assignment failed for the slice's target %s ICI network because there are chips disconnected from the rest of the slice: %s.` | printf | `%s`/`%s`=target/chips | Internal | HIGH |
| `0xa0d5412` | `Detected ICI link failures along %d dimensions, but only 1-dimensional link fault is allowed..` | printf | `%d`=dim count | FailedPrecondition | HIGH |
| `0xa05e59a` | `Failed to add link information: chip %d already has a %c direction link.` | printf | `%d`=chip, `%c`=axis | Internal | HIGH |
| `0x855f8c6` | `Failed to detect GTC reset before timeout %s expires` | printf | `%s`=duration | DeadlineExceeded | HIGH |
| `0x8727005` | `Failed to turn down ICI link %d during slice reset, state=%d` | printf | `%d`=link, `%d`=state | Internal | HIGH |
| `0x871106b` | `GTC failed to converge (max diff %d > %d) before timeout (%s) expired` | printf | `%d > %d`=diff, `%s`=timeout | DeadlineExceeded | HIGH |
| `0x872a345` | `Hop ID %d is out of bound of ICI route path with length %d` | printf | `%d`/`%d`=hop/len | Internal | HIGH |
| `0x8583d20` | `ICI Probe failed.  local port: %d name: %s took %d us. status: %s` | printf | `%d`=port, `%s`=name, `%d`=us, `%s`=status | Internal | CERTAIN |
| `0xa0a81a9` | `ICI resiliency only allow 1-dimensional link failures, but link failures along %d dimensions are discovered.` | printf | `%d`=dim count | FailedPrecondition | HIGH |
| `0xa0ba533` | `ICI routing failed to retrieve %dth hop dimension from bit encoded cache data.` | printf | `%d`=hop | Internal | HIGH |
| `0x9a573e9` | `ALL_REDUCE Output buffer size is not == Input buffer size. Input size: $0 Output size: $1 Group Size $2 Key: $3 Module: $4 MegascaleInfo: $5` | positional | `$0..$5`=sizes/key/module/info | InvalidArgument | HIGH |
| `0x9c142f9` | `ALL_GATHER Input buffer size is not (Output buffer size /  group size). Input size: $0 …` | positional | `$0…`=sizes/info | InvalidArgument | HIGH |
| `0x9c14380` | `REDUCE_SCATTER Output buffer size is not (Input buffer size /  group size). …` | positional | `$0…`=sizes/info | InvalidArgument | HIGH |
| `0x9d1493e` | `ALL_TO_ALL not supported when buffer size is not divisble by number of endpoints. Buffer Size: $0 Number of endpoints: $1. MegascaleInfo: $2` | positional | `$0`/`$1`/`$2` | Unimplemented | CERTAIN |

> **NOTE —** `0x8583d20` ("ICI Probe failed…") was confirmed in `asic_sw::driver::deepsea::ici::SliceConfiguration::GetLocalTopology`; `0x9d1493e` ("ALL_TO_ALL not supported…") in `xla::megascale::runtime::HostCommandSchedulerFactory…GenerateCommunicationIrsFromTransferRegistry`. The collective buffer-size validators are positional (`$N`) even though they sit on the ICI/collective path — they are emitted from the megascale runtime, which is the positional-idiom stronghold.

---

## Megascale (DCN Runtime / Aggregator) — ~21 templates

Cross-host data-center-network coordination: barrier-participant accounting, the corrupted-buffer detector, launch-id timeouts, and the coordinator's error digest. This is the heart of the positional `$N` idiom. The coordinator's hang-digest emits one prose variant per cause branch.

| Offset | Template | Placeholders | Code | Conf. |
|---|---|---|---|---|
| `0x9e6f85e` | `Extra barrier participant. Expected: $0 Message $1` | `$0`=expected, `$1`=msg | Internal | HIGH |
| `0x9e6fa4d` | `Mismatched number of barrier participants: Expected: $0 Msg: $1` | `$0`=expected, `$1`=msg | Internal | HIGH |
| `0x9d149cb` | `MegaScale Corrupted Buffer Detected. Key: $0 Checksum at Sender: $1 Current checksum: $2` | `$0`=key, `$1`/`$2`=checksums | DataLoss/Internal | HIGH |
| `0x9b273a4` | `Timed out waiting for $0 graphs to complete at launch_id $1. Already completed: $2. StepGloballyInProgress: $3 Timeout: $4` | `$0..$4`=count/id/state | DeadlineExceeded | HIGH |
| `0xa122d03` | `MegaScale devices cannot be queried except from jax. (%d)` | `%d`=error code | FailedPrecondition | MEDIUM |

The coordinator's hang digest emits the prose `"Megascale detects a hang that is likely caused by …"` once per cause branch (BAD_TPU_CHIP, BAD_SC_CHIP, DATA_INPUT_STALL, DIFFERENT_MODULE, FINGERPRINT_MISMATCH, NETWORKING_ISSUE, PROGRAM_NOT_QUEUED, UNKNOWN_CAUSE), and the operator-actionable follow-ups (`"Please remove the hosts from the fleet and restart the workload"`, `"Please check the workers to make sure the data input pipeline is working properly"`). The abort path is in [Fatal / Abort Surface](#fatal--abort-surface).

---

## SparseCore / Embedding — ~68 templates

SparseCore (`xla_sc_`) and BarnaCore embedding configuration: alignment requirements, table/feature counts, the partitioner objective enum, and the SMEM row-pointer budget. User-facing (a model-config error) and almost all `InvalidArgument`.

| Offset | Template | Placeholders | Code | Conf. |
|---|---|---|---|---|
| `0xa0a9715` | `barna_core_infeed_queue_hbm_address must be %d-byte aligned.` | `%d`=alignment | InvalidArgument | HIGH |
| `0xa0b6320` | `barna_core_infeed_queue_hbm_size must be a multiple of %d.` | `%d`=multiple | InvalidArgument | HIGH |
| `0xa030dd4` | `Could not find valid TPU batch of length at least %d at position %d for row %d. The embedding work in one sample exceeds what the BarnaCore can process: %s. %s.` | `%d`×3=len/pos/row, `%s`/`%s`=detail | InvalidArgument | HIGH |
| `0x872d950` | `Dynamic learning rate tag: %d not found in the TPU embedding configuration, instead found: %d. tag set size: %d` | `%d`×3=tag/found/size | InvalidArgument | HIGH |
| `0xa02cd93` | `Embedding table is expected to have element type %s or %s.` | `%s`/`%s`=types | InvalidArgument | HIGH |
| `0x86fa1ad` | `Failed to parse TPU embedding partitioner optimization objective "%s". Valid options: performance, hbm_usage, hybrid` | `%s`=value | InvalidArgument | HIGH |
| `0xa11773a` | `hbm_limits_for_embeddings.min_fraction (%f) must be <= hbm_limits_for_embeddings.max_fraction (%f)` | `%f`/`%f`=fractions | InvalidArgument | HIGH |
| `0xa0d36d1` | `Invalid num_features: %d found for table: %s in the TPU embedding configuration. Valid values are >0.` | `%d`=count, `%s`=table | InvalidArgument | CERTAIN |
| `0xa0b7076` | `Logical replicas must evenly divide the SparseCores in the system. logical_replicas = %d, physical_sparse_cores = %d.` | `%d`/`%d`=counts | InvalidArgument | HIGH |
| `0xa069f4e` | `Number of TPU tables on row: %d exceeds what the BarnaCore hardware supports: %d > %d. This is mostly likely a result of incorrect partitioning.` | `%d`×3=row/got/max | InvalidArgument | HIGH |
| `0xa0ff40e` | `Row pointers would exceed available SCS Smem (%d bytes > %d bytes)` | `%d`/`%d`=used/avail | ResourceExhausted | HIGH |
| `0xa07d172` | `Scatter operand has %d elements, which exceeds the 32-bit limit. Unsupported on SparseCore.` | `%d`=count | Unimplemented | HIGH |

> **NOTE —** `0xa0d36d1` ("Invalid num_features…") was confirmed in `tensorflow::PopulateMissingFieldsInTPUEmbeddingConfig`. The `%f` floats in `0xa11773a` are a rare case where `%f` is genuinely a configuration ratio, not a cost-model internal.

---

## Runtime / Driver / PJRT — ~177 templates

The driver state machine, device/ordinal validation, firmware-queue transitions, DMA-buffer accounting, and the PJRT C-API boundary. The most idiom-mixed block: `%p` and `errno` (`%d`) appear here, and codes split between `FailedPrecondition` (state-machine guards) and `Internal`.

| Offset | Template | Placeholders | Code | Conf. |
|---|---|---|---|---|
| `0xa0b7366` | `Attempted to register programmable interrupt with bad index: %d. Number of programmable interrupts: %d.` | `%d`/`%d`=index/count | InvalidArgument | HIGH |
| `0xa0430a3` | `Cannot remove a driver for %s, was not found in map.` | `%s`=driver name | NotFound | HIGH |
| `0xa077a78` | `Cannot transition to %s: the firmware queues are not in %s state; they are in %s state.` | `%s`×3=states | FailedPrecondition | HIGH |
| `0x96c33b2` | `Can't close driver while in state %s; are multiple threads trying to open / close?` | `%s`=state | FailedPrecondition | HIGH |
| `0x94b68ce` | `Can't get the optimized program for executable \`%s\`: MPMD execution is not supported by PJRT C API` | `%s`=executable | Unimplemented | HIGH |
| `0xa09fdcd` | `Chip count (%d) is not supported.` | `%d`=count | InvalidArgument | HIGH |
| `0x872d1a0` | `Close of core dump fd failed with errno: %d` | `%d`=errno | Internal | HIGH |
| `0xa0a9937` | `%d DMA buffers were still outstanding when the driver was re-opened. These buffers must be unmapped before the driver can be re-opened.` | `%d`=count | FailedPrecondition | HIGH |
| `0xa0d10bd` | `Device id '$0' is out of bound. Number of devices is $1.` | `$0`/`$1`=id/count | InvalidArgument | HIGH |
| `0x8679159` | `device ordinal value (%d) must be non-negative` | `%d`=ordinal | InvalidArgument | CERTAIN |
| `0xa1a96ba` | `executable is built for device %s of type "%s"; cannot run it on device %s of type "%s"` | `%s`×4=device/type | InvalidArgument | HIGH |
| `0xa00ab4b` | `Expected %d chips per tray, actually found a tray with %d chips.` | `%d`/`%d`=expected/found | FailedPrecondition | HIGH |
| `0x858a3de` | `failed initializing StreamExecutor for device ordinal %d: %s` | `%d`=ordinal, `%s`=reason | Internal | HIGH |
| `0x721616` | `Failed to convert multipod chip id %d to single-pod chip id.` | `%d`=chip id | Internal | HIGH |

> **NOTE —** `0x8679159` ("device ordinal value (%d) must be non-negative") was confirmed in `stream_executor::StreamExecutorAddressAllocator::GetStreamExecutor`. Many runtime templates feed the executor's async stream path; see [`execute-async-on-stream.md`](execute-async-on-stream.md) for where these surface during enqueue.

### PJRT-C-API / protobuf-descriptor (linked library, not TPU)

The positional `$N` family is also populated by a statically-linked protobuf extension-declaration validator. These are catalogued for completeness but are **not TPU code** and should not be attributed to libtpu's own surface:

| Offset | Template | Note |
|---|---|---|
| `0xa0cf40b` | `"$0" extension field $1 is expected to be $2.` | protobuf descriptor validator |
| `0xa0eba97` | `"$0" extension field $1 is expected to be type "$2", not "$3".` | protobuf descriptor validator |
| `0xa0d0fab` | `$0 cannot declare both \`metadata\` and \`declaration\` as extension declaration for extension #$1.` | protobuf descriptor validator |

---

## CHECK / RET_CHECK / Self-Check Templates

The absl `CHECK`/`QCHECK`/`DCHECK` family and XLA's `TPU_RET_CHECK` do not carry full message templates — they emit a fixed *prefix* and then append the stringified source expression and any streamed `<< "msg"`. The comparison macros (`CHECK_EQ`/`NE`/`GE`) append the operand values with the `"%d vs. %d"` / `"%s vs. %s"` format that recurs across the verifier block.

| Offset | Template | Macro / origin | Conf. |
|---|---|---|---|
| `0xa1a64de` | `Check failed: '` | absl `CHECK`/`QCHECK` prefix (quoted) | HIGH |
| `0xa1f4a87` | `Check failed in ` | absl CHECK with file/line | HIGH |
| `0xa285fb1` | `Check failed: ` | absl CHECK prefix (no quote) | HIGH |
| `0xa183292` | `TPU_RET_CHECK failure (` | XLA TPU RET_CHECK macro | CERTAIN |
| `0xa0ab3ca` | `Hostname Verification Check failed.` | gRPC TLS hostname-verify CHECK | HIGH |
| `0xa2300c5` | `MakeErrorStream destructed without getting absl::Status: ` | XLA status_macros self-check | CERTAIN |
| `0xa2300ff` | `MakeErrorStream shift called after getting absl::Status: ` | XLA status_macros self-check | HIGH |
| `0xa27f1b3` | `MakeErrorStream got absl::Status more than once: ` | XLA status_macros self-check | HIGH |

> **GOTCHA —** the `CHECK(expr) << "msg"` macro inlines the *source expression text* (`spmem_buffer_type != nullptr`, `dynamic_size, nullptr`) into `.rodata`. Large fragments of literal C++ source — even whole lambda bodies — appear in the string table as a side effect. These are CHECK-condition evidence, **not** error templates in the printf sense, and are excluded from the template count. A reimplementer scanning strings for "templates" must filter them out or be flooded with source fragments.

`0xa183292` was confirmed in `tpu::internal::RetCheckFailSlowPath`; `0xa2300c5` in `xla::status_macros::MakeErrorStream::Impl::~Impl` (the destructor self-check that fires when a built-but-unconsumed Status is dropped).

---

## Fatal / Abort Surface

The intentional-abort surface is small and centralized — ICI hard failures, the megascale coordinator abort, internal-bug `LOG(FATAL)`s, and one library guard that is not TPU-specific.

| Offset | Template | Path | Conf. |
|---|---|---|---|
| `0xa1e7cb3` | `!!!! FATAL ERROR !!!! for ` | ICI `FatalErrorCheck` | HIGH |
| `0x8864055` | `!!!! FATAL ERROR !!!! observed errors are: [` | `AsyncDriver::HandleFatalError` composite | HIGH |
| `0xa046045` | `Fatal error occurred. Data links will go down.` | ICI hard-failure marker | HIGH |
| `0xa1b3810` | `FATAL ERROR RECEIVED FROM HARDWARE!!!` | hardware fatal interrupt | HIGH |
| `0x8a2941d` | `Fatal error in creation of RWB Fusion. Please file a bug with XLA-TPU` | fusion internal-bug `LOG(FATAL)` | CERTAIN |
| `0xbe7d460` | `FATAL ERROR: This binary was compiled with <isa> enabled, but this feature is not available on this processor (go/sigill-fail-fast).` | absl CPU-feature startup guard (12 ISA variants) | HIGH |

The megascale coordinator's abort prose — `"Aborting the coordinator after collecting errors from all workers as megascale_error_reporter_abort_on_hang is set to true. All workers will also abort after they detect the coordinator is shutdown."` — is a `LOG(FATAL)` gated on a flag.

> **NOTE —** the `0xbe7d460` family (12 variants: aes, avx, mmx, pclmul, popcnt, sse, sse2, sse3, sse4.1, sse4.2, ssse3, …) is the **absl CPU-feature startup guard**, a library abort that fires before any TPU code runs if the host CPU lacks an ISA the binary was built for. It is not a TPU diagnostic. `0x8a2941d` ("Fatal error in creation of RWB Fusion…") was confirmed in `xla::jellyfish::TpuInstructionFusion::RwbFusionHelper` — a genuine internal-bug fatal with a "file a bug" marker.

---

## Status-Code Mapping

A template becomes an `absl::Status` through one of three factory idioms. Only the first two name the code in the binary; the prose family and the bare printf-into-`LOG` paths require call-site disassembly to confirm the code.

| Idiom | Count | What it is | Code attribution |
|---|---:|---|---|
| `xla::status_macros::MakeErrorStream` | 390 | `RET_CHECK(c) << …` / `return InvalidArgument(…) << …`; 390 `MakeErrorStreamWithOutput<T>` conversion ops, one per `StatusOr<T>` return type. The dominant XLA idiom by call-site count. | code named by the macro at the site |
| `xla::InvalidArgumentStrCat<…>` | 38 | interleaved literal/typed-arg factory | **CERTAIN** → InvalidArgument |
| `xla::UnimplementedStrCat<…>` | 31 | same | **CERTAIN** → Unimplemented |
| `xla::InternalStrCat<…>` | 29 | same | **CERTAIN** → Internal |
| `xla::ResourceExhaustedStrCat<…>` | 1 | same | **CERTAIN** → ResourceExhausted |
| `absl::<Code>Error("…")` prose | 1+ | direct prose factory (e.g. `absl::InternalError("Invalid error type")`) | code named by the function |

The `<Code>StrCat` factory is the unique place where format-arg C++ types are recoverable **without disassembly**: each instantiation emits an Itanium-mangled symbol `_ZN3xla<len><Code>StrCatIJ<typepack>EEC2E…` whose `<typepack>` names every interleaved literal segment and typed argument byte-for-byte.

### Arg-Type Decode (mangled type pack)

```text
RA<N>_Kc   const char (&)[N]  — a literal segment of N chars (N counts the
                                 NUL, so the visible literal is N-1 chars)
RA<N>_S1_  another const char (&)[N] — a later literal (S1_ back-references
                                 the already-named const char type)
m / l / i / f          unsigned long(size_t) / long / int / float, by value
Rm / Rl / Ri / Rf      &  of each
RKm / RKi              const unsigned long& / const int&
NSt…basic_string…      std::string by value;  RKNSt… = const std::string&
NSt…basic_string_view… std::string_view
N3tpu10TpuVersionE     tpu::TpuVersion (enum), by value
N…isa16TensorCoreBundleE  the per-gen ISA TensorCoreBundle (rendered via its
                          AbslStringify → the %v sigil)
N4absl8StatusOrI…E     absl::StatusOr<…>, by value
```

Worked decode (each a real instantiation symbol):

```text
InvalidArgumentStrCatIJRA74_KcmEE
    → InvalidArgument("<73-char literal>", size_t)
InvalidArgumentStrCatIJRA16_KcRfRA20_S1_EE
    → InvalidArgument("<15ch>", float&, "<19ch>")     (the only float-bearing
      factory family — a cost/ratio message)
InvalidArgumentStrCatIJRA19_KcRlRA38_S1_RKiEE
    → InvalidArgument("<18ch>", long&, "<37ch>", const int&)
InvalidArgumentStrCatIJRA26_KcN3tpu10TpuVersionEEE
    → InvalidArgument("<25ch>", tpu::TpuVersion)
InvalidArgumentStrCatIJRA65_Kc…isa16TensorCoreBundleEEE
    → InvalidArgument("<64ch>", const TensorCoreBundle&)   (the "Bad scalar
      opcode … bundle: %v" family — per-gen gxc/gfc, gxc/glc, vxc variants)
UnimplementedStrCatIJRA253_KcEE
    → Unimplemented("<252-char literal>")   (the longest single-literal
      Unimplemented message — a "not supported" explanation block)
```

Byte-confirmed arg-type frequency across the 99 factories: `std::string` 29, `std::string_view` 15, `long` 13, `TensorCoreBundle` 6, `size_t` 5, `StatusOr<…>` 3, `const int&` 2, `float` 2, `TpuVersion` 1. Even at the byte-confirmed level, args are overwhelmingly string-ish + `long`; `float` and pointer are vanishingly rare — consistent with the printf-spec distribution above.

### StatusCode keyword distribution

String-table keyword occurrence (noise-filtered), an upper-bound *prose* signal of which codes the surface favors — **not** a per-template count:

```text
InvalidArgument 300 | Unimplemented 169 | NotFound 75 | FailedPrecondition 56
ResourceExhausted 21 | Unavailable 15 | OutOfRange 14 | Aborted 8
DeadlineExceeded 3 | AlreadyExists 3 | PermissionDenied 2 | DataLoss 1
```

> **GOTCHA —** the raw `Internal` keyword count (~16,823) is dominated by the `/internal/` source-path component in inlined `__FILE__` strings, not by error templates. Do not read it as an Internal-status count. The byte-confirmed Internal total is the 29 `InternalStrCat` factories plus the `MakeErrorStream` Internal sites.

---

## User-Facing vs Internal Split

The prose itself signposts the audience. **User-facing** (operator / JAX-user actionable, no bug id): the HLO verifier shape/rank/dimension block, the `--xla_fuel` flag-value error, the embedding-config errors with their valid-value hints, the megascale digest follow-ups, and the device/executable mismatch. **Internal** (XLA-bug markers): strings carrying `b/<id>`, `go/<link>`, `"please file a bug"`, or `"should not happen"`.

| Offset | Internal-bug marker template | Marker |
|---|---|---|
| `0x858c562` | `Kernel body fingerprint collision detected for key: … Please file a bug with the XLA team …` | "file a bug" |
| `0x8a2941d` | `Fatal error in creation of RWB Fusion. Please file a bug with XLA-TPU` | "file a bug" |
| `0x96c1211` | `XLA has not implemented dynamic sized slice with non-trival stride yet. Please file a bug against XLA` | "file a bug" |
| `0x96c12d4` | `Unimplemented reduce-window in fusion cost modeling. Please file a bug with XLA` | "file a bug" |
| `0x9fd476d` | `tightened domains are empty. This should not happen except if we proven infeasibility or optimality.` | "should not happen" |
| `0xa0c9452` | `Encountered unexpected layout … This should not happen - please file a bug against XLA.` | "should not happen" + "file a bug" |
| `0x99e1405` | `Close() appears to be hanging, this might be a deadlock see b/147787375` | `b/<id>` |

> **NOTE —** `b/<id>` and `go/<link>` tokens are the strongest internal signal. They also tag known-gap TODOs (`"TODO(b/157237781) support VFIO device %s"`, `"TODO: b/475913712 - Expected Gather indices to be bitpacked …"`), which are not live errors but become Unimplemented messages when their guarded path is hit.

---

## At-a-Glance: Templates per Subsystem

| Subsystem | Candidates | Dominant idiom | Dominant code | Audience |
|---|---:|---|---|---|
| Compile / HLO / verifier | ~875 | StrFormat `%s`/`%d` + RET_CHECK | InvalidArgument | user-facing |
| Runtime / driver / PJRT | ~177 | StrFormat `%s`/`%d`/`%p` + errno | Internal / FailedPrecondition | mixed |
| ICI / collective | ~90 | StrFormat `%d`/`%s` + `$N` (msc) | Internal / DeadlineExceeded | mixed |
| MSA / memory / allocation | ~79 | StrFormat `%zu`/`%lld` | ResourceExhausted / Internal | mixed |
| SparseCore / embedding | ~68 | StrFormat `%d`/`%s`/`%f` | InvalidArgument | user-facing |
| Megascale (DCN runtime) | ~21 | Substitute `$0`/`$1` | Internal / LOG(FATAL) | mixed |
| Scheduler / fuel / FIFO | ~15 | StrFormat `%s`/`%d`/`%c` | Internal / RET_CHECK | mixed |
| PJRT-C-API / protobuf-desc | (lib) | Substitute `$0`/`$1` | InvalidArgument | (not TPU) |
| **Total distinct templates** | **~2,937** | 2,799 printf + 138 positional | — | — |

> **GOTCHA —** the subsystem counts are first-match-wins over a prose keyword set (ordering: compile → MSA → ICI → SparseCore → runtime → scheduler → megascale). A template that matches two groups is counted once, under the earlier group. The total (~2,937) carries a ±~50 caveat where the error-prose gate admits a few help strings or rejects a few terse errors — those are properly hint strings ([`hint-strings.md`](hint-strings.md)), not errors.

---

## Cross-References

- [Runtime & Execution Overview](overview.md) — where in the PJRT execution path these Status objects originate and propagate
- [Hint Strings](hint-strings.md) — the *suggestion* surface (try/consider/recommended), contrasted with the failures here; the ±50 boundary cases live there
- [Internal Pass Names](internal-pass-names.md) — pipeline-stage identifier strings that the compile/HLO error templates reference by name
- [Execute Async on Stream](execute-async-on-stream.md) — the executor enqueue path that surfaces the runtime/driver `StatusOr<T>` templates at run time
