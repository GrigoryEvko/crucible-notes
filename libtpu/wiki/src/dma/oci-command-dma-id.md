# OCI Command DMA-id Selectors

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`, `.rodata` base `0x84a0000`); `.data.rel.ro` carries a `0x200000` VMA→file delta. All addresses are VMA. Other versions will differ.*

## Abstract

An **OCI command** (off-chip-interconnect command) is a single device trace event that carries **up to three embedded DMA transactions** — not one. The on-wire OCI read/write command record reserves three `trace_id_header` slots (`cmd0` / `cmd1` / `cmd2`), each naming one DMA transaction's identity, plus an `index_valid` bitmask that says which of the three slots are live. When the profiler reconstructs a DMA timeline from these commands, it must answer one question per transaction: *what 38-bit `dma_id` keys this transaction*, so that a command's begin event (e.g. `OciCommonReadCmdIssuedFromEngine`, id 22) pairs with its completion event (`OciCommonCompletedInTcs`, id 96) by sharing a `dma_id`.

That question is answered by the **`CmdDmaIdFromEntry<T>` selectors**: six byte-identical template instantiations (one per OCI command message type `T`) that take the command message and an `int selector` (the transaction index 0..2), gate the pick on the `index_valid` bitmask, read `trace_id_header_cmd{selector}`, and compose the 38-bit composite `dma_id` from that header's `{transaction_id, core_id, chip_id}`. The dispatcher `TraceEntryWrapper<pxc>::GetDmaId(int)` reads the on-wire `trace_point_id`, verifies the command's protobuf `oneof` discriminator, and routes the six OCI command ids (22, 23, 26, 54, 55, 96) to the matching helper — while every *other* (single-header) trace id falls through to a no-selector tail that composes the same 38-bit layout from one inline `trace_id_header`.

This page owns three things and a reimplementer must get all three right:

- **The six `CmdDmaIdFromEntry<T>` selectors** — which OCI command each serves, the `int selector → trace_id_header_cmd{0,1,2}` map (cpp offset `msg + 0x18 + 8·selector`), the `index_valid` bit-`selector` gate, the null-pointer fallback to the `TraceIdHeader_globals_` default instance, and the byte-exact 38-bit `dma_id` composition.
- **The three header bands the OCI command carries** — `trace_id_header_cmd0/1/2` (the three embedded transactions' identity keys at cpp `+0x18`/`+0x20`/`+0x28`), the `index_valid` per-transaction valid bitmask (`+0x30`), and the per-transaction `id_index0/1/2` (`+0x34`/`+0x38`/`+0x3c`) — read from the embedded `FileDescriptorProto` pool and the parse-table FieldEntry cpp offsets.
- **The id → routing binding** — `GetDmaId(int)` dispatch (`trace_point_id → oneof check → helper`), the 38-bit `dma_id` as the routing key, and how the consumer `ConvertTpuTraceToXPlane<pxc>` keys an `absl::flat_hash_map<uint64, DmaTransfer>` on it to pair begin/end into a DMA timeline span. The consumer calls `GetDmaId(0)` — selector 0 / `cmd0` only.

> **SCOPE —** The four-id ICR Node-Fabric DMA *band decode* (ids 48/50/51/91, the egress/ingress span reconstruction) is on [`../routing/icr-node-fabric-dma.md`](../routing/icr-node-fabric-dma.md) — link, do not duplicate. The intra-chip descriptor those commands stage is on [`intra-chip-descriptor.md`](intra-chip-descriptor.md). This page owns the **OCI *command* DMA-id selectors, the three header bands the command carries, and the id→routing binding**.

| | |
|---|---|
| **Selectors** | `xprof::tpu::CmdDmaIdFromEntry<…pxc::profiler::OciCommon…>` — six instantiations |
| **Selector VAs** | `0xf69a500` / `0xf69a560` / `0xf69a5c0` / `0xf69a620` / `0xf69a680` / `0xf69a6e0` (byte-identical) |
| **Dispatcher** | `TraceEntryWrapper<…pxc::profiler::TraceEntry>::GetDmaId(int)` @ `0xf699ca0` |
| **OCI command ids** | 22, 23, 26, 54, 55, 96 (on-wire `trace_point_id`) |
| **Header bands** | `trace_id_header_cmd0/1/2` @ cpp `+0x18`/`+0x20`/`+0x28` (the up-to-3 embedded transactions) |
| **Valid gate** | `index_valid` (uint32, proto f4, cpp `+0x30`) — bit `selector` must be set |
| **`dma_id` width** | 38 bits = `transaction_id[0:21] \| (core_id&7)<<21 \| (chip_id&0x3fff)<<24` |
| **Default fallback** | `…pxc::profiler::TraceIdHeader_globals_` (all-zero → `dma_id == 0`) |
| **Consumer** | `ConvertTpuTraceToXPlane<pxc>` — `GetDmaId(0)` @ `0xf26c8d9` → `flat_hash_map<uint64, DmaTransfer>` |
| **Evidence grade** | Reimplementation-grade / **Confirmed** (byte-anchored against the IDA decompile + FDP pool) |

---

## 1. The OCI command as a command-with-embedded-DMA-transactions record

The OCI command band is the deepsea read/write COMMAND trace: a command issued by an engine onto the off-chip interconnect, observed at several points on its journey (issued from engine, accepted at the memory node, completed at the TensorCore Sequencer). The crucial structural fact is that **one command event encodes one to three DMA transactions**, each transaction identified by its own `trace_id_header`.

Six OCI command message classes share **one** protobuf schema, byte-exact across all six and cross-checked against both the `FileDescriptorProto` field list and the parse-table FieldEntry cpp offsets:

| proto # | field name | type | cpp off | wire tag | role |
|---------|-----------------------|---------|---------|----------|------|
| 1 | `trace_id_header_cmd0` | message | `0x18` | `0x0a` | embedded DMA transaction #0 identity (band 0) |
| 2 | `trace_id_header_cmd1` | message | `0x20` | `0x12` | embedded DMA transaction #1 identity (band 1) |
| 3 | `trace_id_header_cmd2` | message | `0x28` | `0x1a` | embedded DMA transaction #2 identity (band 2) |
| 4 | `index_valid` | uint32 | `0x30` | `0x20` | per-transaction valid bitmask (bit N = `cmdN` live) |
| 5 | `id_index0` | uint32 | `0x34` | `0x28` | transaction #0 id-index (17-bit on wire) |
| 6 | `id_index1` | uint32 | `0x38` | `0x30` | transaction #1 id-index |
| 7 | `id_index2` | uint32 | `0x3c` | `0x38` | transaction #2 id-index |
| 8 | `node_type` | enum | `0x40` | `0x40` | command endpoint class (`NodeTypeValues`) |

> Message layout: vtable `@0`, `InternalMetadata` `@0x8`, hasbits `@0x10`, the three submessage pointers `cmd0`/`cmd1`/`cmd2` `@0x18`/`0x20`/`0x28`, `index_valid` `@0x30`, `id_index0/1/2` `@0x34`/`0x38`/`0x3c`, `node_type` `@0x40`. The dtor frees exactly the three submessage pointers at `+0x18`/`+0x20`/`+0x28` (each a `TraceIdHeader`); the ctor zeroes `0x10..0x43`, so the three pointers start null. These offsets are precisely the selector's read sites (§2).

The three `trace_id_header_cmd0/1/2` are **the three header bands** this page owns. Each is the identity of one DMA transaction the command fired. The `index_valid` bitmask says which of the three bands are populated: a command that issues a single DMA sets bit 0 only; a command that fans out to three transactions sets bits 0, 1, and 2. The `id_index0/1/2` are 17-bit per-transaction indices (their precise role — queue slot vs. descriptor-ring pointer vs. re-order tag — is not part of the `dma_id` key; see §6).

### 1.1 The embedded `TraceIdHeader` (the per-transaction identity)

Each of the three header bands is a `TraceIdHeader` submessage. Its three fields are the sole source of the 38-bit `dma_id`:

| proto # | field name | type | cpp off | wire tag | `dma_id` bits |
|---------|----------------|--------|---------|----------|---------------|
| 1 | `transaction_id` | uint32 | `0x18` | `0x08` | `[0:21]` |
| 2 | `core_id` | enum | `0x1c` | `0x10` | `[21:24]` (`& 7`) |
| 3 | `chip_id` | uint32 | `0x20` | `0x18` | `[24:38]` (`& 0x3fff`) |

`core_id` is `TraceIdHeaderCoreIdValues` (pxc / BarnaCore generation): `0=RESERVED, 1=NONCORE, 2=TC0, 3=TC1, 4=BC0, 5=BC1, 6=BC2, 7=BC3`. SparseCore generations substitute `SC0..SC3` for `BC0..BC3` (cross-generation rename; the field width and the `& 7` mask are unchanged).

`node_type` is `NodeTypeValues` — the command endpoint class:

```text
0 = NODE_TYPE_TCS   1 = NODE_TYPE_BC    2 = NODE_TYPE_CMQ   3 = NODE_TYPE_HBMQ
4 = NODE_TYPE_UHI   5 = NODE_TYPE_ICR   6 = NODE_TYPE_QNM
```

> **NOTE —** `node_type` labels the command's endpoint (which on-chip node class issued/received it) and is a payload field, **not** part of the `dma_id` key and **not** read by the selector. The same enum appears on the OCI-message band; see [`../routing/icr-node-fabric-dma.md`](../routing/icr-node-fabric-dma.md) §2.2.

---

## 2. The six `CmdDmaIdFromEntry<T>` selectors

Each OCI command message type `T` gets one template instantiation of `CmdDmaIdFromEntry<T>(const T& msg, int selector)`. All six bodies are **byte-identical** — one C++ template, emitted six times — differing only in the template-name relocation and the per-type `*_globals_` default-instance address. The selector answers: *"give me the `dma_id` of the `selector`-th embedded DMA transaction of this command, if that transaction is valid."*

| selector VA | template type `T` (OCI command message) | on-wire id | `oneof` (proto f, at `entry+0x28`) |
|-------------|------------------------------------------|------------|------------------------------------|
| `0xf69a500` | `OciCommonReadCmdIssuedFromEngine` | 22 | 15 |
| `0xf69a560` | `OciCommonMemReadReqFromEngine` | 23 | 16 |
| `0xf69a5c0` | `OciCommonWriteCmdAcceptedAtMn` | 26 | 19 |
| `0xf69a620` | `OciCommonOciWriteCommand` | 54 | 35 |
| `0xf69a680` | `OciCommonOciReadCommand` | 55 | 36 |
| `0xf69a6e0` | `OciCommonCompletedInTcs` | 96 | 53 |

> All six decompiled bodies are identical down to the byte. The body (selector `0xf69a500` shown; the other five differ only in `T` and the `*_globals_` address):

```c
// CmdDmaIdFromEntry<…OciCommonReadCmdIssuedFromEngine>(a1 = const T& msg, a2 = int selector)
unsigned __int64 CmdDmaIdFromEntry(__int64 a1, unsigned int a2)
{
  // index_valid (uint32 @msg+0x30) >> selector
  unsigned __int64 v3 = (unsigned __int64)*(unsigned int *)(a1 + 48) >> a2;
  unsigned __int64 result = 0;                       // present = 0 (absent)
  if ( a2 <= 2 && (v3 & 1) != 0 )                    // selector in 0..2 AND index_valid bit set
  {
    // trace_id_header_cmd{selector} pointer @ msg + 0x18 + 8*selector
    TraceIdHeader **v5 = *(TraceIdHeader ***)(a1 + 8LL * a2 + 24);
    if ( !v5 )
      v5 = &TraceIdHeader_globals_;                  // null submessage → all-zero default instance
    return  ((_DWORD)v5[3]        & 0x1FFF00)        // transaction_id[8:21]  (header +0x18)
          | (( *((_DWORD *)v5 + 7) & 7u)   << 21)    // core_id[0:3]   << 21  (header +0x1c)
          | ((unsigned __int64)((_DWORD)v5[4] & 0x3FFF) << 24)  // chip_id[0:14] << 24 (header +0x20)
          |  (unsigned __int8)*((_DWORD *)v5 + 6);   // transaction_id[0:8]   (header +0x18)
  }
  return result;                                     // selector > 2 OR bit clear → present = 0
}
```

The result is wrapped in `std::optional<unsigned long>`: the composed 38-bit value plus a presence bit (`dl = 1` on the composing path, `0` on the absent path). An absent result causes the consumer to drop the entry (it is not a DMA-pairing event).

### 2.1 The `selector → trace_id_header` map

The `int selector` is the *transaction index*. It indexes the three header bands by simple pointer arithmetic on the message:

| selector | picks field | proto # | cpp offset (`msg + 0x18 + 8·sel`) | meaning |
|----------|-----------------------|---------|-----------------------------------|---------|
| 0 | `trace_id_header_cmd0` | f1 | `msg + 0x18` | 1st embedded DMA transaction |
| 1 | `trace_id_header_cmd1` | f2 | `msg + 0x20` | 2nd embedded DMA transaction |
| 2 | `trace_id_header_cmd2` | f3 | `msg + 0x28` | 3rd embedded DMA transaction |
| ≥ 3 | (out of range) | — | — | return absent (`present = 0`) |

The bound `a2 <= 2` rejects any selector outside 0..2 before the index is used, so the `8·selector` index can only address the three header slots.

### 2.2 The `index_valid` gate

```text
rsi = msg.index_valid (uint32 @ msg+0x30)
rsi >>= selector
if (rsi & 1) == 0  →  return absent (present = 0)
```

`index_valid` (proto f4) is the per-transaction valid bitmask: bit N means "transaction N is present and `trace_id_header_cmd{N}` is meaningful." A command carrying one DMA sets bit 0; a command that fans out to three sets bits 0/1/2. If the bit for the requested `selector` is clear, the selector returns absent and the transaction is dropped — even though the command record physically reserves all three header slots.

### 2.3 The null-pointer fallback

If `index_valid` bit `selector` *is* set but the `cmd{selector}` submessage pointer is null (`msg[+0x18 + 8·sel] == 0`), the selector substitutes the proto-runtime default instance `…pxc::profiler::TraceIdHeader_globals_`. That block is all-zero, so the composed `dma_id` is 0 (with `present = 1`). In practice a set valid-bit implies a constructed submessage; this is the protobuf default-instance safety path that keeps the read total.

> **CAUTION —** The selector reads the header's `transaction_id` **twice** to assemble the 21-bit field across the byte boundary: `(transaction_id & 0x1FFF00)` covers bits `[8:21]`, and `(uint8)transaction_id` covers bits `[0:8]`. A naive single-mask read of `transaction_id & 0x1FFFFF` is equivalent and simpler; the binary's two-read form is a compiler artifact, not a semantic distinction. The reimplementer should treat `transaction_id` as a single 21-bit field.

---

## 3. The 38-bit composite `dma_id` (the routing key)

The selector composes one 38-bit identifier from the picked header's three fields. This composite is the **routing key** — the value that decides which timeline slot a transaction lands in and therefore which begin event pairs with which end event.

```text
dma_id = (transaction_id & 0x1FFFFF)            // bits [0:21]   21-bit per-transaction txn id
       | ((core_id  & 0x7)    << 21)            // bits [21:24]   3-bit core_id enum
       | ((chip_id  & 0x3FFF) << 24)            // bits [24:38]  14-bit chip id
present = 1
```

| field | source (header offset) | mask | shift | id bits |
|-------|------------------------|------|-------|---------|
| `transaction_id` | `+0x18` | `0x1FFFFF` | 0 | `[0:21]` |
| `core_id` | `+0x1c` | `0x7` | 21 | `[21:24]` |
| `chip_id` | `+0x20` | `0x3FFF` | 24 | `[24:38]` |

The chip-id field is 14 bits wide (`& 0x3FFF`) in this pxc build; earlier generations used a narrower (12-bit) chip id, widened to 14 bits in later silicon. The full result is 38 bits. This is the same bit layout the [ICR Node-Fabric DMA band](../routing/icr-node-fabric-dma.md) uses for its single-header `dma_id` extraction — the OCI *command* selector and the NF-band extractor converge on one composite ID format, so a transaction's id is comparable across the two bands.

> The byte-exact composition appears in two places: each `CmdDmaIdFromEntry<T>` selector (§2 body), and the `GetDmaId(int)` no-selector tail (`LABEL_172`, §4) for single-header trace ids. Both read `transaction_id` at header `+0x18`, `core_id` at `+0x1c`, `chip_id` at `+0x20`, and emit the identical `0x1FFF00 | (core_id&7)<<21 | (chip_id&0x3fff)<<24 | (uint8)transaction_id` expression.

---

## 4. The dispatcher: `GetDmaId(int)` (id → selector → routing key)

`TraceEntryWrapper<…pxc::profiler::TraceEntry>::GetDmaId(int)` @ `0xf699ca0` is the entry point that turns a live trace entry into a `dma_id`. It dereferences the wrapper to reach the underlying `TraceEntry` (`v2 = *(entry + 0x10)`), reads the on-wire `trace_point_id` from the trace header (`+0x18`), and switches on it (a `0x96`-arm jump table, bound-checked `≤ 0x95`, default → return 0).

The switch has two kinds of arms:

**(a) The six OCI command arms.** For ids 22/23/26/54/55/96 the arm verifies the protobuf `oneof` discriminator (`*(v2 + 0x28) == <oneof>`), loads the live command-payload submessage pointer (`v2 + 0x20`) — or the command's `*_globals_` default instance on a `oneof` mismatch — and *tail-calls* the matching `CmdDmaIdFromEntry<T>` selector, forwarding the original `int` argument as the selector:

```c
case 22:  // OciCommonReadCmdIssuedFromEngine
  if ( *(_DWORD *)(v2 + 40) == 15 )                 // oneof case 15 active?
    v7 = *(...*)(v2 + 32);                           // live command payload  (entry+0x20)
  else
    v7 = &OciCommonReadCmdIssuedFromEngine_globals_; // default instance
  return CmdDmaIdFromEntry<…OciCommonReadCmdIssuedFromEngine>(v7, a2 /* selector */);

case 55:  // OciCommonOciReadCommand
  if ( *(_DWORD *)(v2 + 40) == 36 )
    v9 = *(...*)(v2 + 32);
  else
    v9 = &OciCommonOciReadCommand_globals_;
  return CmdDmaIdFromEntry<…OciCommonOciReadCommand>(v9, a2);
// …id 23 (oneof 16), id 26 (oneof 19), id 54 (oneof 35), id 96 (oneof 53) identical shape
```

**(b) The single-header tail.** Every *other* trace id (the OCI/UHI/CMQ/ICI/NF single-header events, and the ICR band ids 48/50/51/91) carries exactly one inline `TraceIdHeader` at the entry-submessage's `+0x18`. Those arms load that header (or the per-id `*_globals_`, or finally `TraceIdHeader_globals_` if null) and fall into the common tail `LABEL_172`, which composes the **same** 38-bit layout as §3. The tail is the no-selector twin of the selector composition.

```c
LABEL_172:
  v3 = *((_DWORD *)v6 + 6);                          // transaction_id  (header +0x18)
  v4 =  ((unsigned int)v3 & 0x1FFF00)
      | ((*((_DWORD *)v6 + 7) & 7) << 21)            // core_id         (header +0x1c)
      | ((unsigned __int64)((_DWORD)v6[4] & 0x3FFF) << 24);  // chip_id  (header +0x20)
  return v4 | (unsigned __int8)v3;
```

> The on-wire id → `oneof` map (id 22 → 15, 23 → 16, 26 → 19, 54 → 35, 55 → 36, 96 → 53) matches the OCI command `oneof` field numbers exactly, and each arm forwards `entry+0x20` (live payload) / `*_globals_` (fallback) into the selector. The default arm returns 0 (absent).

---

## 5. The id → routing binding (the consumer)

`GetDmaId(int)` has **exactly one caller** in the unit — `ConvertTpuTraceToXPlane<…pxc::profiler::TraceEntry>` @ `0xf26c8d9` — proven by a full `.text` rel32 scan. The caller invokes it with **selector 0**:

```c
DmaId = TraceEntryWrapper<…pxc::profiler::TraceEntry>::GetDmaId(v14, 0);  // selector 0 = cmd0
// test presence bit; if absent → drop (not a DMA-pairing event)
// else: DmaId is the key into flat_hash_map<uint64, DmaTransfer>
```

The 38-bit `dma_id` keys an `absl::flat_hash_map<unsigned long, xprof::tpu::(anon)::DmaTransfer>` (policy `FlatHashMapPolicy<unsigned long, DmaTransfer>`; insert via `PrepareInsertSmallNonSoo` / `PrepareInsertLarge`; key hashed by `Hash<long>`). Begin and end events that share a `dma_id` land in the **same** `DmaTransfer` slot, producing one begin/end span. The map is drained into device XEvents by `MergeOverlappingTransfers` → `ConvertDmaTransfersToXPlane`.

This is the *routing*: the `dma_id` routes a command's transaction to a single timeline slot, so that:

```text
OciCommonReadCmdIssuedFromEngine (id 22)  ── begin marker ─┐
                                                            ├─ same dma_id → one DmaTransfer span
OciCommonCompletedInTcs          (id 96)  ── end marker ───┘
```

> **CONFIRMED-PARTIAL —** The consumer keys on **`cmd0` only**. `GetDmaId(int)` is called from exactly this one site, always with `selector = 0`. The selector template *supports* `selector 1`/`2` (the 2nd/3rd embedded transaction's `dma_id` is extractable), but no consumer in this unit requests them: whether a multi-transaction OCI command is ever split into more than one timeline span by a different pass (or by a newer-generation builder) is **unverified** in this binary. The pxc DMA timeline pairs on the first embedded transaction's `dma_id`.

### 5.1 Where this differs from the ICR Node-Fabric band

The OCI *command* band (this page) and the ICR Node-Fabric *DMA* band ([`../routing/icr-node-fabric-dma.md`](../routing/icr-node-fabric-dma.md)) are two distinct paths into the same `flat_hash_map<uint64, DmaTransfer>` machinery, but they are read by different code:

| | OCI command band (this page) | ICR Node-Fabric DMA band |
|---|------------------------------|--------------------------|
| Trace ids | 22, 23, 26, 54, 55, 96 | 48, 50, 51, 91 |
| Headers per event | up to 3 (`cmd0/1/2`, gated by `index_valid`) | exactly 1 (inline `trace_id_header`) |
| `dma_id` extractor | `CmdDmaIdFromEntry<T>(msg, selector)` | `GetDmaId` per-id arm → single-header tail |
| Begin/end pairing | command issued (22/26) ↔ completed (96), same `dma_id` | descriptor (91) / egress-msg (50); ICI first/last packet (48) |
| Owns | command DMA-id selectors + 3 header bands + id→routing | the 48/50/51/91 payload decode + egress/ingress span |

Both use the identical 38-bit `dma_id` layout (§3) and the same `flat_hash_map` policy, so a transaction's id is the same value whether it is observed on the command band or the NF band — which is what lets the two bands' events for one transaction collate.

---

## 6. What the selector does not decide

> **NOTE —** The selector reads only the picked header's `{transaction_id, core_id, chip_id}` and the `index_valid` gate. Everything else on the OCI command record is dropped by the selector path:
>
> - **`id_index0/1/2`** (the 17-bit per-transaction indices at `+0x34`/`+0x38`/`+0x3c`) are decoded into the proto but are **not** part of the `dma_id` key. Their semantic role — command-queue slot, descriptor-ring pointer, or re-order tag distinct from `transaction_id` — is **LOW / unverified** here; no consumer reading them as a timeline key was located.
> - **`node_type`** (`+0x40`) labels the endpoint class but does not select a lane or key a slot.
> - The **begin/end discriminator** inside the `DmaTransfer` pairing — which `trace_point_id` marks the begin (id 22/26) vs. the end (id 96) of one span, and how `MergeOverlappingTransfers` folds same-`dma_id` events — is the `DmaTransfer` struct's own logic, not the selector's; it is documented with the band decode, not here.

The per-generation (`vfc`/`vlc`/`glc`/`gfc`) equivalents of these six selectors are out of scope: `GetDmaId(int)` here is the pxc-template instantiation, and whether the newer generations emit their own `CmdDmaIdFromEntry<gen::profiler::OciCommon…>` selectors (with native 14-bit chip id and the SparseCore `core_id` rename) or fold the command DMA band differently is **unverified** — no per-generation selector symbols were searched here.

---

## 7. Reference offsets

> **OCI command message** (`OciCommonOciReadCommand` and its five siblings, identical schema): vtable `@0`, `InternalMetadata` `@0x8`, hasbits `@0x10`, `trace_id_header_cmd0` `@0x18` / `cmd1` `@0x20` / `cmd2` `@0x28` (submessage ptrs), `index_valid` `@0x30`, `id_index0/1/2` `@0x34`/`0x38`/`0x3c`, `node_type` `@0x40`. The dtor frees the three `cmd*` submessages (each a `TraceIdHeader`, alloc `0x28` B); the ctor zeroes `0x10..0x43` (the three ptrs start null).
>
> **`TraceIdHeader`** (pxc): `transaction_id` `@0x18` · `core_id` `@0x1c` · `chip_id` `@0x20`. Default prototype `…pxc::profiler::TraceIdHeader_globals_` (all-zero → `dma_id == 0`).
>
> **Key sites:** six selectors `0xf69a500` / `0xf69a560` / `0xf69a5c0` / `0xf69a620` / `0xf69a680` / `0xf69a6e0` (byte-identical) · dispatcher `GetDmaId(int)` `0xf699ca0` (jump table `0x96` arms, bound `0x95`, default-drop) · OCI command arms within the dispatcher: id 22 (`oneof` 15), id 23 (16), id 26 (19), id 54 (35), id 55 (36), id 96 (53) · single-header tail `LABEL_172` · consumer `ConvertTpuTraceToXPlane<pxc>` `GetDmaId(0)` call site `0xf26c8d9` (single caller) · `flat_hash_map<uint64, DmaTransfer>` insert `PrepareInsertSmallNonSoo` / `PrepareInsertLarge`.
>
> **`dma_id` layout:** `transaction_id[0:21] (0x1FFFFF)` | `core_id[21:24] (&7 <<21)` | `chip_id[24:38] (&0x3fff <<24)` → 38 bits.

---

## Cross-References

- [`../routing/icr-node-fabric-dma.md`](../routing/icr-node-fabric-dma.md) — the four-id ICR Node-Fabric DMA band (48/50/51/91): the payload decode and egress/ingress span reconstruction that share the same 38-bit `dma_id` and `flat_hash_map<uint64, DmaTransfer>` machinery.
- [`../routing/nf-descriptor.md`](../routing/nf-descriptor.md) — the on-wire Node-Fabric DMA descriptor record the cross-chip transactions stage.
- [`intra-chip-descriptor.md`](intra-chip-descriptor.md) — the intra-chip `OciDescriptorCommonIssuedFromTcs` descriptor: the on-chip DMA an OCI command's transactions reference.
- [`rolled-strided-general.md`](rolled-strided-general.md) — the rolled / strided / general transfer-body emitters that fill those descriptors.
- [`host-device-dma.md`](host-device-dma.md) — the host↔device DMA path (`MemcpyH2D` / `MemcpyD2H` lanes), a sibling timeline band.
- [`uhi-host-interface.md`](uhi-host-interface.md) — the UHI host-interface band (`NODE_TYPE_UHI`), another OCI endpoint class.
