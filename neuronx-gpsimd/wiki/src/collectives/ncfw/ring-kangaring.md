# Ring + Kangaring Collective

This page is the reimplementer's reference for the **NCFW ring all-reduce
framework** and its **kangaring** (Kangaroo-ring) variant — the per-channel ring
config tape, the four directional semaphores, the **kring** peer-semaphore handshake,
the **N-read / 1-write (NR1W)** reduce fold, the skip/hop fanout, the per-step
sequencing, and the per-arch ×4 schema. It is the firmware-side companion to the host
lowering in [ALL_REDUCE](../ops/all-reduce.md): a logical all-reduce is composed by
the host NRT into a ring (or kangaring) program that the NCFW **management core**
sequences and that the in-SDMA **CCE** reduce executes.

**The NCFW core is a scalar Xtensa-LX control core, not a Vision-Q7 FLIX engine.** It
has no shipped disassembler, so the device step *schedule* is not directly decodable.
The structural recovery here is from the **host-side** `ncfw_log_*` decoders in
`libncfw.so` (x86-64) — pretty-printers that walk the firmware's DRAM-resident
collective structs and therefore mirror the exact field layouts the firmware reads —
and from the host **encoder** primitives + DWARF in `libnrt.so`. The device
*execution substrate* (the wait/signal/snapshot leaf primitives the case bodies call)
is recovered separately by re-decoding the carved NCFW IRAM under the **scalar-LX
length rule** with the native `xtensa-elf-objdump XTENSA_CORE=ncore2gp` (§8).

The page default is `[HIGH × OBSERVED]` (read directly from a binary/disasm/DWARF/
bytes); claims that depart from it carry an explicit `[CONFIDENCE × PROVENANCE]` tag,
with `INFERRED` (deduced from naming/structure) or `CARRIED` (established on a
committed sibling page).

> **Provenance.** Host decoders + firmware blobs from `libncfw.so`
> (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`,
> `opt/aws/neuron/lib/libncfw.so`, BuildID `a98f8e1ca2294582835310c3a1092e0a5e500db5`,
> ELF64 x86-64, **not stripped**, 615 640 bytes — `stat`/`readelf -n` confirm).
> `.text` and `.rodata` are VMA==file-offset (`.text` @`0x10c0`, `.rodata`
> @`0x65000`); `.data` carries a `0x1000` delta (`readelf -SW`). The host encoder +
> the `reduction_type_t`/`encd_neigh` DWARF are from `libnrt.so.2.31.24.0`
> (same package, 122 956 336 bytes, with `.debug_info`). The carved v3/CAYMAN NCFW
> IRAM (`v3_ncfw_iram_bin` @VMA `0x79860`, **19 392 = 0x4bc0** bytes,
> SHA-256 `d7bc8b81…e4afd`) is the device-side target.
> [identity OBSERVED HIGH]

---

## 1. Orientation — the decoder call tree

`libncfw.so` embeds **four** firmware blob pairs and four parallel sets of
`ncfw_log_*` decoders, selected by a Neuron HW-generation arch id. The selector
`libncfw_get_image` (@`0x1179`) and the logger dispatch `libncfw_ctx_log` (@`0x1309`)
both branch on `cmp [..],{0x05,0x0c,0x14,0x1c}` and route to per-codename loggers:

| arch id | blob | codename / `ncfw_ctx_log` | gen |
| ------- | ---- | ------------------------- | --- |
| `0x05`  | `v2_ncfw_{i,d}ram_bin` | `sunda_ncfw_ctx_log` @`0x1a12b`        | NC-v2 |
| `0x0c`  | `v3_ncfw_{i,d}ram_bin` | `cayman_ncfw_ctx_log` @`0x32ed2`       | NC-v3 |
| `0x14`  | `v4_ncfw_{i,d}ram_bin` | `mariana_ncfw_ctx_log` @`0x4bc79`      | NC-v4 |
| `0x1c`  | `v4_plus_ncfw_{i,d}ram_bin` | `mariana_plus_ncfw_ctx_log` @`0x64a20` | NC-v4+ |

[arch dispatch + blob labels + ctx_log call targets OBSERVED HIGH — `cmp [rbp-0x4],0xNN`
legs @`0x1199`/`0x11ad`/`0x11c1`/`0x11c7` in `get_image`; the `call sunda/cayman/mariana/
mariana_plus_ncfw_ctx_log` @`0x135c`/`0x1375`/`0x138e`/`0x13a7` in `ctx_log`.]

> **CORRECTION — codename↔gen mapping.** The binary routes arch `0x0c` →
> `cayman_ncfw_ctx_log` and arch `0x14` → `mariana_ncfw_ctx_log`. A prior survey note
> paired `0x0c`→mariana / `0x14`→cayman; the `libncfw_ctx_log` call targets above show
> **CAYMAN = NC-v3 (arch `0x0c`)**, matching the committed
> [collective-enums](../ops/collective-enums.md) and the
> [DMA-reprogram APB-broadcast](dma-reprogram-apb-bcast.md) Cayman=NC-v3 anchor.
> [OBSERVED HIGH — the call-target labels are not ambiguous.]

> **NOTE — v5 / MAVERICK is ABSENT.** `libncfw.so` ships **exactly four** blobs and
> four loggers; there is no `v5_ncfw_*_bin` symbol and no `maverick` decoder, and the
> dispatch has no fifth `cmp` leg. Any NC-v5/MAVERICK NCFW interior is **file-absent
> here** — not stated as fact on this page. [absence OBSERVED HIGH — `nm | rg -i
> 'v5_ncfw|maverick'` empty; only four `cmp [rbp-0x4],0xNN` legs.]

Because all four arch variants share an identical struct schema, every `ncfw_log_*`
symbol appears **4×** with byte-identical sizes (§7). Offsets below use the v2/sunda
copy; they are schema-wide.

The RING/KANGARING decoder call tree (the configs side):

```
ncfw_log_algo_configs            @0x1961c   (union: ring | mesh | hierarchical)
  └─ ncfw_log_algo_ring_configs               @0x8544   "channels":[…]
       └─ i=0..31: ncfw_log_algo_ring_channels_configs  @0x6020   (32×)
            ├─ ncfw_log_algorithm_ring_neighbor (next_neigh)      @0x37f1
            ├─ ncfw_log_algorithm_ring_neighbor (prev_neigh)
            ├─ recv/send/post/dma_compl sema  (soc_addr u64 each)
            ├─ ncfw_log_algo_ring_kring_peer_semas               @0x4d64
            └─ ncfw_log_dma_channel_apb_bcast                    @0x4899

ncfw_log_algo_ctx                @0x18cd2   (union)
  └─ ncfw_log_algo_ring_ctx (runtime per-channel scoreboard)     @0x16a00
```

[symbols + addresses OBSERVED HIGH — `nm -nS`; all four copies enumerated in §7.]

---

## 2. The per-channel ring config struct (stride **0x94 = 148 bytes**)

`ncfw_log_algo_ring_configs` (@`0x8544`, size `0x4d7`) emits a single key
`"channels": [ … ]` of **32 entries**. The per-channel base is `cfg + 148*i`, proven
byte-exact from the shift/add chain in the loop body:

```asm
; ncfw_log_algo_ring_configs, per-channel base computation
87e0: mov rax,rdx        ; rax = i
87e3: shl rax,0x3        ; 8i
87e7: add rax,rdx        ; 9i
87ea: shl rax,0x2        ; 36i
87ee: add rax,rdx        ; 37i
87f1: shl rax,0x2        ; 148i   <- STRIDE = 0x94 = 148
87f9: lea rdi,[rax+rdx]  ; cfg + 148*i        (rdx already = cfg here)
...
8818: cmp DWORD PTR [rbp-0x38],0x1f   ; i <= 31 ?
881c: jle 87da                         ; => EXACTLY 32 CHANNELS
```

[stride arithmetic + loop bound OBSERVED HIGH — bytes @`0x87e0`–`0x881c`. The v3/v4
copies (`ring_configs` @`0x212eb`/`0x3a092`) show the identical `shl3/+/shl2/+/shl2`
chain.]

> **CORRECTION — stride is 148, not 149.** An earlier note recorded the per-channel
> stride as `0x95 = 149` ("i<<3,+i,<<2,+i,<<2,+i = 149"). The byte-exact arithmetic is
> `8i → 9i → 36i → 37i → 148i` ⇒ **`0x94 = 148`**. The highest field referenced is
> `+0x90` (1 byte), so a channel spans `+0x00..+0x90` = 145 used bytes with **3 pad
> bytes** (`+0x91..+0x93`) inside a 148-byte stride. [OBSERVED HIGH.]

A "channel" here is **one ring instance** (one CC-topology ring); `channel_id` is the
loop index, not a stored field. 32 channels ⇒ up to 32 concurrent collective
topologies.

### 2.1 Channel field layout (148-byte stride)

| off | size | field | decoded by / load (sunda copy) | conf |
| ---- | ---- | ----- | ------------------------------ | ---- |
| `+0x00` | `0x1c` | `next_neigh` | `ring_neighbor` (call @`0x618e`, base direct) | HIGH |
| `+0x1c` | `0x1c` | `prev_neigh` | `ring_neighbor` (`lea rdx,[rax+0x1c]` @`0x619a`) | HIGH |
| `+0x38` | 8 | `recv_sema.soc_addr` | `mov r12,[rax+0x38]` @`0x64af` | HIGH |
| `+0x40` | 8 | `send_sema.soc_addr` | `mov r12,[rax+0x40]` @`0x6a8d` | HIGH |
| `+0x48` | 8 | `post_sema.soc_addr` | `mov r12,[rax+0x48]` @`0x706b` | HIGH |
| `+0x50` | 8 | `dma_compl_sema.soc_addr` | `mov r12,[rax+0x50]` @`0x7640` | HIGH |
| `+0x58` | `0x20` | `kring_peer_semas` | `lea rdx,[rax+0x58]` @`0x7906` → call `0x4d64` @`0x7924` | HIGH |
| `+0x78` | `0x14` | `dma_apb_bcast` | `lea rdx,[rax+0x78]` @`0x7930` → call `0x4899` @`0x794e` | HIGH |
| `+0x8d` | 1 | `spad_slot_idx` | `movzx eax,[rax+0x8d]` @`0x7c89` | HIGH |
| `+0x8e` | 1 | `fold_n` | `movzx eax,[rax+0x8e]` @`0x7eb0` | HIGH |
| `+0x8f` | 1 | `kangaring_is_primary` | `movzx eax,[rax+0x8f]` @`0x80d7` (key `"kangaring_is_primary"`@`0x6520e`) | HIGH |
| `+0x90` | 1 | `kangaring_num_peers` | `movzx eax,[rax+0x90]` @`0x82fe` (key `"kangaring_num_peers"`@`0x65225`) | HIGH |

[every offset OBSERVED HIGH — the per-field `mov/movzx/lea` immediates above; the
sub-decoder call targets `0x4d64`/`0x4899` confirmed by `nm`. JSON key strings live in
the single `.rodata` table at `0x65000`: `next_neigh`@`0x6518e`, `recv_sema`@`0x651a4`,
`send_sema`@`0x651ae`, `post_sema`@`0x651b8`, `dma_compl_sema`@`0x651c2`,
`kring_peer_semas`@`0x651d1`, `fold_n`@`0x650db`.]

The **four directional semaphores** (`+0x38..+0x50`) are each a single 64-bit SOC
physical address (the printer wraps each as `{"addr":{"soc_addr":"0x%016lX"}}` via
`ncfw_log_addr`, one `soc_addr` u64). They are pointers into the **EVT_SEM** CSR block
(§6). Directional roles, shared by ring and kangaring:

- **`recv_sema`** — signalled by the peer that pushes data *into* this rank (confirmed
  by the encoder: `__post_send`'s local path calls `encd_dma_inc_recv_sema`, §4).
- **`send_sema`** — this rank signals after it pushes data to a neighbour.
- **`post_sema`** — post/commit semaphore (phase boundary).
- **`dma_compl_sema`** — fired by the DMA/CCE engine on transfer completion.

[names + soc_addr OBSERVED HIGH; directional meaning INFERRED MED except `recv_sema`
which is OBSERVED via the encoder.]

### 2.2 `ring_neighbor` sub-struct (`next_neigh`/`prev_neigh`, `0x1c` = 28 B)

`ncfw_log_algorithm_ring_neighbor` (@`0x37f1`, size `0x9d2`):

| off | size | field | load | conf |
| ---- | ---- | ----- | ---- | ---- |
| `+0x00` | `fold_n×8` | `net_idx_addrs[]` | `mov r12,[rax+rdx*8]` @`0x3f2c` | HIGH |
| `+0x18` | 1 | `fold_n` | `movzx eax,[rax+0x18]` @`0x3f00` — **is the loop bound** | HIGH |
| `+0x19` | 1 | `type` | `movzx eax,[rax+0x19]` @`0x3c82` (link type) | HIGH |

The `net_idx_addrs` array is `fold_n`-long (≤3 by the geometry: `3×8 = 24` bytes before
`fold_n`@`+0x18`): the per-element loop is literally bounded by `[rax+0x18]`:

```asm
3eed: mov DWORD PTR [rbp-0x70],0x0   ; j = 0
3f00: movzx eax,BYTE PTR [rax+0x18]  ; fold_n
3f0a: cmp DWORD PTR [rbp-0x70],eax   ; j < fold_n ?
3f2c: mov r12,QWORD PTR [rax+rdx*8]  ; net_idx_addrs[j]
3fb1/…: add DWORD PTR [rbp-0x70],0x1 ; j++
```

`net_idx_addrs` holds the multiple physical RDMA/network endpoints a folded logical
neighbour maps to. This neighbour-level `fold_n`@`+0x18` is **distinct** from the
channel-level `fold_n`@`+0x8e`. [OBSERVED HIGH — the loop is bounded by `[rax+0x18]`;
"RDMA endpoint" semantics INFERRED MED.]

### 2.3 `dma_apb_bcast` sub-struct (`+0x78`, `0x14` = 20 B)

`ncfw_log_dma_channel_apb_bcast` (@`0x4899`, size `0x4cb`) — the APB-broadcast DMA
descriptor that fans a tail-pointer update to all masked peers in one shot:

| off | size | field | load | conf |
| ---- | ---- | ----- | ---- | ---- |
| `+0x00` | 8 | `m2s_tail_ptr.soc_addr` | (mem→sema ring tail ptr) | HIGH |
| `+0x08` | 8 | `s2m_tail_ptr.soc_addr` | `lea rcx,[rax+0x8]` @`0x4a53` | HIGH |
| `+0x10` | 4 | `mask` | `mov ebx,[rax+0x10]` @`0x4b70` (u32 peer mask) | HIGH |

[offsets OBSERVED HIGH; `m2s`/`s2m` = "memory↔semaphore" tail pointers, INFERRED MED
from the recurring naming in both the apb tail ptrs and the runtime counters (§3).
This descriptor is detailed on [DMA-reprogram APB-broadcast](dma-reprogram-apb-bcast.md).]

---

## 3. Runtime context — the live per-channel scoreboard

`ncfw_log_algo_ring_ctx` (@`0x16a00`, size `0x19c6`) emits `"channels":[…]` with the
same 32-entry loop; each runtime record is **16 bytes** (`shl rdx,0x4` @`0x171c9`).
This is the firmware's mutable per-channel state — **identical for ring and
kangaring** (there is no kangaring-specific runtime field):

| off | size | field | fmt | meaning (INFERRED from name) | conf |
| ---- | ---- | ----- | --- | ---------------------------- | ---- |
| `+0x00` | 2 | `recv_cnt` | `%hu` | chunks received this ring | HIGH |
| `+0x02` | 2 | `send_credit` | `%hu` | flow-control credit to next peer | HIGH |
| `+0x04` | 2 | `repeat_cnt` | `%hu` | ring-step repeat counter | HIGH |
| `+0x06` | 2 | `m2s_val` | `%hu` | memory→semaphore counter | HIGH |
| `+0x08` | 2 | `s2m_val` | `%hu` | semaphore→memory counter | HIGH |
| `+0x0e` | 1 | `slot_idx` | `%hhu` | current spad slot index | HIGH |
| `+0x0f` | 1 | `run_state` | `%hhu` | ring step state-machine state | HIGH |

(`+0x0a..+0x0d` reserved / not printed.) `recv_cnt` + `send_credit` are the classic
ring all-reduce flow-control pair: each step the firmware bumps `recv_cnt`, consumes
`send_credit`, advances `m2s_val`/`s2m_val`, and steps `run_state`. [field offsets +
16-byte stride OBSERVED HIGH; key strings `recv_cnt`@`0x655f1`, `send_credit`@`0x655fc`,
`repeat_cnt`@`0x6560a`, `m2s_val`@`0x653ac`, `s2m_val`@`0x653b6`, `slot_idx`@`0x65203`,
`run_state`@`0x65622`. State-machine role INFERRED MED.]

---

## 4. The kring peer-semaphore handshake (the kangaring fanout)

The kangaring (Kangaroo-ring) variant is the same 32-channel ring tape with two extra
per-channel role flags and a **peer-fanout** semaphore sub-struct that replaces the
plain ring's strict 1-to-1 `next_neigh` signal with a **1-primary / N-secondary** hop.

### 4.1 `kring_peer_semas` (`+0x58`, `0x20` = 32 B)

`ncfw_log_algo_ring_kring_peer_semas` (@`0x4d64`, size `0x12bc`):

| off | size | field | load | conf |
| ---- | ---- | ----- | ---- | ---- |
| `+0x00` | 8 | `mine.soc_addr` | `mov r12,QWORD PTR [rax]` @`0x5234` | HIGH |
| `+0x08` | 8 | `peers[0].soc_addr` | `mov r12,[rax+rdx*8+0x8]` @`0x5afb` (base+8+i·8) | HIGH |
| `+0x10` | 8 | `peers[1].soc_addr` | — same load, i=1 | HIGH |
| `+0x18` | 8 | `peers[2].soc_addr` | — same load, i=2 | HIGH |

The peer fanout is a **fixed 3-slot** array (`mine` is the single own-semaphore at
`+0x00`):

```asm
560b: mov DWORD PTR [rbp-0x98],0x0       ; peer index i = 0
5afb: mov r12,QWORD PTR [rax+rdx*8+0x8]  ; peers[i].soc_addr  (base + 8 + i*8)
5dcd: add DWORD PTR [rbp-0x98],0x1       ; i++
5dd4: cmp DWORD PTR [rbp-0x98],0x2       ; i <= 2 ?
5ddb: jle 561a                            ; => EXACTLY 3 PEER SLOTS [0..2]
```

[mine`+0x00`, peers`+0x08`/`+0x10`/`+0x18`, the 3-slot loop OBSERVED HIGH — bytes
@`0x5234`/`0x5afb`/`0x5dd4`/`0x5ddb`. JSON keys `mine`@`0x65171`, `peers`@`0x6517b`,
`peer_id`@`0x65182`, `soc_addr`@`0x6511d`. `peer_id` is the loop index (`%d` of `i`),
not a stored field — peer order is positional.]

Emitted JSON:

```json
"kring_peer_semas": {
  "mine":  { "addr": { "soc_addr": "0x...." } },
  "peers": [ { "peer_id": 0, "addr": { "soc_addr": "0x...." } }, … ] }
```

The role flags (channel `+0x8f`/`+0x90`, §2.1):

- **`kangaring_is_primary`** (`+0x8f`, bool) — is THIS rank the channel **primary**
  (the "kangaroo" aggregator) or a secondary? Selects the 8-engine (primary) vs
  4-engine (secondary) DMA-engine fanout map (§5.4).
- **`kangaring_num_peers`** (`+0x90`, u8 ≤ 3) — how many of `peers[0..2]` are **live**;
  the count the primary must signal/poll in a step.

[is_primary / num_peers OBSERVED HIGH; the role model INFERRED MED, corroborated by the
8-vs-4 engine maps (§5.4) and the `ENCD_NEIGH_NEXT_PEER_RMTV` skip-ahead ordinal (§5.2).]

### 4.2 How a rank signals its peer(s) — `__post_send`

The device handshake op is recovered from the host encoder leaf `enc_primitive::
__post_send(int sema_idx, encd_neigh neigh)` (@`0x148a20`, leaf, `0xbd` bytes):

```asm
148a20: cmp QWORD PTR [rdi+0xc8],0x0   ; has a network connection?
148a2a: je  148a80                      ;   no  -> LOCAL path
148a30: cmp edx,0x1                      ; neigh type (encd_neigh) == 1 (net) ?
148a35: cmp esi,0x1ff ; jg <assert>      ; sema_idx bounded to 0x1ff (511)
148a55: and ecx,0x1ff                    ; net-index wrap to 0x1ff
148a76: jmp 243f20 <encd_dma_update_net_index>   ; NET : bump net-index sema
;  ---- LOCAL path (148a80): ----
148a9a: jmp 245590 <encd_dma_inc_recv_sema>      ; LOCAL: INC the peer's recv_sema
```

So a signal to a **local** neighbour increments that neighbour's `recv_sema` (the
channel `+0x38` field of §2.1); a **net** neighbour bumps a net-index semaphore with
the index wrapped `&0x1ff`. Companion primitives: `__post_recv(int)` (@`0x1494d0`, arm
receive), `__mark_step(bool)` (@`0x149610`, step boundary). These lower to the device
EVT_SEM op model (§6). [the local/net split + the two tail-call targets + the
`sema_idx`≤511 bound OBSERVED HIGH — bytes @`0x148a20`–`0x148a9a`.]

### 4.3 The per-step peer protocol

Reconstructed (the leaf primitives are OBSERVED HIGH; the exact per-step ordering is
INFERRED MED — the *schedule* runs on the LX core, §8):

```
for each kangaring step k:
  READ-FOLD:  recv_reduce_copy / __recv_reduce_write read-and-CCE-reduce from the
              N peers (vector<encd_neigh>), each peer's recv_sema (local) or
              net-index sema (net) waited up to the chunk-ready count       (§5.1)
  WRITE:      direct_reduce_send_kangaring writes the folded result to the 1
              node_next, then __post_send INCs that next peer's recv_sema    (§4.2)
  MARK:       __mark_step advances the step; __post_recv arms the next receive
```

i.e. a per-step, **per-peer** INC + wait-GE handshake using the `kring_peer_semas`
`mine` (own, waited) + `peers[0..2]` (signalled) CSRs.

> **NOTE — distinct from the global counted barrier.** The
> [device barrier](neff-device-barrier.md) is a **global, 4-step counted** barrier
> (a `barrier_sema[4]`/step + `target_sema_val[4]`/step fan-IN over ≤4 leader
> semaphores) that **brackets** an entire collective phase; it carries no peer-list and
> no per-data-step structure. The kring `kring_peer_semas` is a **per-step,
> per-data-chunk PEER** handshake — a rank's own `mine` semaphore + its up-to-3
> ring-neighbour peer semaphores, signalled/waited every reduce-scatter / all-gather
> step as data rotates around the channel. The two compose: the barrier brackets the
> phase, the kring peer-semas sequence the steps **within** it. [structural distinction
> HIGH — the barrier struct has no peer-list; the kring struct has no per-step
> target-value array; different decoders walk different DRAM regions.] The ring
> send/wait completion flags driving each step live in the cc_op word — see
> [ring send/wait protocol](ring-protocol-config-command.md).

---

## 5. The NR1W fold — N reads, 1 write

### 5.1 `reduction_type_t` and the N-read mechanism

The fold pattern is the `reduction_type_t` enum (DWARF DIE `<0x60c99b>` in
`libnrt.so`), the **3rd integer arg (`ecx`)** of every reduce
step primitive:

| value | name | fold |
| ----- | ---- | ---- |
| `0` | `RING_2R1W` | plain ring: 2 reads / 1 write |
| `1` | `RING_2R2W` | ring: 2 reads / 2 writes |
| `2` | `KANGARING_NR1W` | **kangaring: N reads / 1 write** |

[OBSERVED HIGH — `DW_TAG_enumerator` `RING_2R1W` const_value `0` @`<0x60c99c>`,
`RING_2R2W`=`1` @`<0x60c9a2>`, `KANGARING_NR1W`=`2` @`<0x60c9a8>`. Matches the committed
[collective-enums §3.5](../ops/collective-enums.md#35-reduction_type_t--the-ring-readwrite-fold-pattern-typedef--die-0x60c99b).]

The reduce step primitives that take it (`libnrt`, demangled signatures OBSERVED HIGH):

```cpp
enc_primitive::recv_reduce_send (enc_half_chunk_index, SDMA_CCETYPE,
                                 reduction_type_t, bool, bool)            @0x16ad70
enc_primitive::recv_reduce_copy (enc_half_chunk_index, SDMA_CCETYPE,
                                 reduction_type_t, vector<encd_neigh>, bool) @0x16b030
enc_primitive::recv_reduce_copy_send (…, reduction_type_t, bool, bool)    @0x16aed0
enc_primitive::__recv_reduce_write (enc_half_chunk_index, SDMA_CCETYPE,
                                 reduction_type_t, vector<encd_neigh>, bool, bool) @0x16a0a0
enc_primitive::direct_reduce_send_kangaring (enc_half_chunk_index, SDMA_CCETYPE) @0x158120
```

The **N-read** mechanism is in `__recv_reduce_write` (@`0x16a0a0`): it takes a
`std::vector<encd_neigh>` (the set of N source neighbours to read-and-reduce in ONE
step) and **accumulates one address+neighbour tuple per source**, then assembles a
single multi-source CCE-reduce descriptor:

```asm
; __recv_reduce_write — the N-source accumulation into 1 write
16a3f8 / 16a45d / 16a573 / 16a87f / 16abae :  call vector<addr_neigh_tuple_t>::_M_realloc_append  ; per-source tuple
16a210 / 16a6ab : call enc_primitive::__get_pgt_offset(...)            ; per-source page-table offset
16ac72          : call enc_primitive::__record_net_src_addr(...)       ; build the CCE source list
16ac4e          : call enc_primitive::__post_send(int, encd_neigh)     ; signal next
16a246          : call enc_primitive::__mark_step(bool)                ; step boundary
```

[OBSERVED HIGH — the `addr_neigh_tuple_t` vector append × ≥5 + `__get_pgt_offset` per
source + `__record_net_src_addr` + the single CCE descriptor are the literal call
census of `__recv_reduce_write`. **N sources fold into 1 CCE-reduce write.**]

The per-element reduce arithmetic (FMA/ADD/MIN/MAX) is **not** in `reduction_type_t` —
it rides the `SDMA_CCETYPE` argument and is performed in-SDMA by the **CCE** engine; see
[CCE in-transfer reduce](../../dma/cce-in-transfer.md) (`kbin_cce_op_to_sdma_cce_op`
@`0x2664c0`, `CSWTCH.21 = {1,0,3,2}`).

`enc_half_chunk_index { CHUNK_H0=0, CHUNK_H1=1, ENC_CHUNK_SPLIT_N=2 }` (DWARF, OBSERVED
HIGH @`<0x60c2b3>`): each chunk is split into 2 **halves**; the kangaring composer issues
`direct_reduce_send_kangaring` **per half** (the half-split is the
pipelining/double-buffering unit).

### 5.2 `encd_neigh` ordinals and the skip-ahead hop

`encd_neigh` (DWARF DIE `<0x3a717>`, OBSERVED HIGH):

| `0` LOCAL | `1` NEXT | `2` PREV | `3` GATEWAY | `4` PEER_RMTV | `5` PEER_RMTV2 | `6` PEER_LOCAL | `7` **NEXT_PEER_RMTV** | `8` INVALID / NUM |
| --------- | -------- | -------- | ----------- | ------------- | -------------- | -------------- | ---------------------- | ----------------- |

The kangaring `vector<encd_neigh>` is populated with `PEER_*` + `NEXT_*` ordinals;
`ENCD_NEIGH_NEXT_PEER_RMTV = 7` ("next-peer-remote-V") is a **skip-ahead** peer — the
*next* ring step's remote peer — the structural root of the "kangaroo" hop: a single
step reaches a NEXT-step peer, not only the adjacent one. [enum OBSERVED HIGH;
"NEXT_PEER_RMTV = the skip-ahead hop" INFERRED MED from the name + the fanout-vector
usage.]

### 5.3 Ring vs Kangaring — the byte-exact distinction

The two host composers make the distinction concrete. The decisive signal is the
`reduction_type_t` immediate in `ecx` before every reduce call:

**PLAIN RING reduce-scatter** — `enc_metaring_primitive::__compose_redsct_channel(int,
node, node_next)` (@`0x16d800`): no peer vector, a single `node_next`, `ecx = 0`:

```asm
16e00c: xor ecx,ecx ; call recv_reduce_send  (16e016)   ; reduction_type = RING_2R1W=0
16e028: xor ecx,ecx ; call recv_reduce_send  (16e035)
16e086: xor ecx,ecx ; call recv_reduce_copy  (16e0b2)
```

**KANGARING reduce-scatter** — `enc_metaring_primitive::__compose_redsct_channel_kangaring(
int, node, node_next, vector<node_peer>)` (@`0x16b300`): takes the N-peer vector,
`ecx = 2`, plus 4× the kangaring direct reduce-send:

```asm
16bc56 / 16bc66 / 16bc8f / 16bc9f : call direct_reduce_send_kangaring  (×4)
16bcdf: mov ecx,0x2 ; call recv_reduce_send  (16bcea)   ; reduction_type = KANGARING_NR1W=2
16bcff: mov ecx,0x2 ; call recv_reduce_send  (16bd0c)
16bd64: mov ecx,0x2 ; call recv_reduce_copy  (16bd8b)   ; with vector<encd_neigh>
16bde9: mov ecx,0x2 ; call recv_reduce_copy  (16be0b)
```

[the `xor ecx,ecx` (ring) vs `mov ecx,0x2` (kangaring) immediate before **every**
reduce call is the decisive byte-exact distinction — OBSERVED HIGH in both composers.]

The struct types confirm the N-read/1-write topology (DWARF `byte_size`, OBSERVED HIGH):
`node_peer` = **56 bytes** (DIE `<0x60c422>`; `input`@0 — the READ source — `output`@24,
`neigh`@48); `node_next` = **48 bytes** (DIE `<0x60c3d0>`; `output`@0 — the WRITE dest).
A **vector of `node_peer`** (N read sources) folds into **one `node_next`** (1 write).

The all-gather side mirrors this: RING `__compose_allreduce_channel(int, node,
node_next)` (@`0x171600`) vs KANGARING `__compose_allreduce_channel_kangaring(int, node,
node_next, vector<node_peer>)` (@`0x1726f0`); the kangaring channel builder is
`__compose_channel_kangaring(int)` (@`0x173c20`). [symbols OBSERVED HIGH.]

### 5.4 Why "kangaroo" — the DMA-engine hop maps

The primary rank hops/fans OUT to many peers in one step using **twice** the DMA
engines a secondary uses. The hop maps are shipped `.rodata` tables (`libnrt`, 32×u32,
`0xff…` sentinel; `.rodata` VMA==file-offset @`0x7cf000`), byte-dumped:

| table | VMA | engines |
| ----- | --- | ------- |
| `cayman_kangaring_dma_map_primary_d2d` | `0x9c9f20` | `{4,5,6,7,12,13,14,15}` (**8**) |
| `cayman_kangaring_dma_map_secondary_d2d` | `0x9c9ea0` | `{4,5,6,7}` (**4**) |
| `cayman_kangaring_dma_map_primary_pcie` | `0x9ca020` | `{0,1,2,3,8,9,10,11}` (**8**) |
| `cayman_kangaring_dma_map_secondary_pcie` | `0x9c9fa0` | `{0,1,2,3}` (**4**) |

(mariana copies @`0x9bf6a0`/`0x9bf620`/`0x9bf7a0`/`0x9bf720`, same shape.) [bytes
OBSERVED HIGH — `objdump -s -j .rodata`: `04 00 00 00 05 00 00 00 …` then
`ff ff ff ff` sentinel at the 9th slot of the primary maps, 5th of the secondary maps.]

The selector `cayman_get_kangaring_dma_engine_id_from_tbl` (@`0x25b890`) picks the table
by `(is_primary[sil], is_d2d[dl])` and indexes `[table + slot*4]`, slot bounded ≤`0xf`:

```asm
25b897: test sil,sil          ; is_primary ?
25b89c: test dl,dl            ; is_d2d ?
25b8a0: lea rax,[…primary_pcie]   25b8ca: lea rax,[…primary_d2d]
25b8ea: lea rax,[…secondary_pcie] 25b900: lea rax,[…secondary_d2d]
25b8aa: cmp r8d,0xf           ; slot bounded <= 15
25b8b0: movsxd r8,r8d         ; index = slot
```

So `kangaring_is_primary=1` selects the **8-engine primary** map (the kangaroo that
hops WIDE, signalling all `kangaring_num_peers` peers); secondaries use 4. `d2d`
(die-to-die) vs `pcie` picks the transport route. The 8-vs-4 split **is** the hop
fanout: the primary reaches 2× the peers per step (skip-ahead) vs the plain ring's
adjacent-only step. [tables + selector OBSERVED HIGH; "primary = kangaroo wide hop"
INFERRED MED-strong from the `is_primary` flag + the 8-vs-4 count + `NEXT_PEER_RMTV`.]

Hop-set builders (host, executed at config time, OBSERVED HIGH symbols):
`get_neighbor_kangaring` @`0x235780`, `enc_alg_kangaring_init_nbr_tokens` @`0xfda80`,
`encd_alg_kangaring_init_channel` @`0x24e050`, `encd_{get,set}_kangaring_active_channel_n`
@`0x2370f0`/`0x2370c0`. The per-rank hop *schedule* is built here but **executes on the
LX firmware** (not host-decodable). [symbols OBSERVED HIGH; schedule MED.]

---

## 6. Tying the semaphores to hardware — EVT_SEM

Every `kring_peer_semas` field (`mine`, `peers[0..2]`) and every channel sema
(`recv`/`send`/`post`/`dma_compl`) is a 64-bit `soc_addr` (`"0x%016lX"`) pointing into
the SOC **EVT_SEM** 256-entry semaphore CSR block (READ/SET/INC/DEC op windows). The
handshake op model (host → device):

- **SIGNAL a peer** = `add_semaphore_inc` (device op `0x10A0` subop `21`) → write the
  peer's `SEMAPHORE_INC[i]` window (the **`+0x1800`** INC window). On the host this is
  `__post_send`'s `encd_dma_inc_recv_sema` (local) / `encd_dma_update_net_index` (net,
  index wrapped `&0x1ff`). The `dma_apb_bcast` (channel `+0x78`) fans the write to all
  masked peers in one APB-broadcast DMA.
- **WAIT on `mine`** = `add_semaphore_wait_ge_and_dec` (device op `0x10A0` subop `20`)
  → POLL `SEMAPHORE_READ[i]` until `>= target`, then write `SEMAPHORE_DEC[i]`.

[soc_addr representation OBSERVED HIGH; the EVT_SEM binding INFERRED-STRONG — the
soc_addr integers are runtime-populated in firmware DRAM (the `.data` caveat), only the
structural access is static. The `EVT_SEM` INC window `+0x1800` is CARRIED.]

> **NOTE — red herring avoided.** The strings `direct_trigger_sema_%d` (`@0x65257`) and
> `event_wait_sema` (`@0x6526e`) in `libncfw.so` belong to the **MESH** event tape
> (`ncfw_log_configs_algo_mesh_events`), **not** the ring/kangaring path. The kangaring
> peer-sema decoder references only `mine`/`peers`/`peer_id`/`addr`/`soc_addr`. See
> [Mesh Collective](mesh-collective.md). [string xref range-checked OBSERVED HIGH.]

---

## 7. Per-arch ×4 — schema identity

Every ring/kangaring decoder appears 4× with **byte-identical sizes** (`nm -nS`,
OBSERVED HIGH):

| decoder | v2/sunda | v3/cayman | v4/mariana | v4+/m_plus | size |
| ------- | -------- | --------- | ---------- | ---------- | ---- |
| `ncfw_log_algo_ring_kring_peer_semas` | `0x4d64` | `0x1db0b` | `0x368b2` | `0x4f659` | `0x12bc` |
| `ncfw_log_algo_ring_channels_configs` | `0x6020` | `0x1edc7` | `0x37b6e` | `0x50915` | `0x2524` |
| `ncfw_log_algo_ring_configs` | `0x8544` | `0x212eb` | `0x3a092` | `0x52e39` | `0x4d7` |
| `ncfw_log_algorithm_ring_neighbor` | `0x37f1` | `0x1c598` | `0x3533f` | `0x4e0e6` | `0x9d2` |
| `ncfw_log_dma_channel_apb_bcast` | `0x4899` | `0x1d640` | `0x363e7` | `0x4f18e` | `0x4cb` |
| `ncfw_log_algo_ring_ctx` | `0x16a00` | `0x2f7a7` | `0x4854e` | `0x612f5` | `0x19c6` |

The kring struct-offset immediates (`[rax]`, `[rax+rdx*8+0x8]`, the peer loop init/
cmp`0x2`/inc`0x1`), the channel layout (`kring`@`+0x58`, `apb`@`+0x78`, `is_primary`
@`+0x8f`, `num_peers`@`+0x90`, `recv`@`+0x38`…`dma_compl`@`+0x50`), and the **148-byte
stride** + 32-channel loop are **schema-wide** across all four NCFW generations; only
the rip-relative call/string displacements differ (all four point into the single
`.rodata` string table at `0x65000`). The host `libnrt.so` is one binary serving all
gens; the kangaring DMA hop maps exist per-arch (`cayman_*`/`mariana_*`) with the same
`{8-primary, 4-secondary}` shape. [OBSERVED HIGH.]

> **NOTE — v5 / MAVERICK.** No fifth (NC-v5/MAVERICK) NCFW blob or decoder ships in
> `libncfw.so` (§1). Any v5 ring/kangaring interior is **inferred-absent / file-absent**
> — not asserted here.

---

## 8. The device side — what the scalar-LX re-decode confirms

The NCFW management core is a **scalar Xtensa-LX**, not a Vision-Q7 FLIX engine; its
case-body *interiors* (the `0x3c..` ring cluster) do **not** linearize (the dense
op0=`e`/`f` operand bytes desync any linear sweep, and no LX TIE config ships to name
the `e`/`f`-leader ops). What **does** lift to HIGH — re-decoded byte-exact on the
carved **v3/CAYMAN** IRAM (`0x4bc0` bytes, SHA `d7bc8b81…e4afd`) with the native
`xtensa-elf-objdump XTENSA_CORE=ncore2gp` under the scalar-LX length rule (`op0 e/f =
3-byte, else 2-byte; resync at retw.n`) — is the **leaf primitive substrate** the ring
case bodies call:

```asm
; DEVICE SEMAPHORE-WAIT-GE (spin-poll), v3 @0x3498
3498: c0 20 00  memw                ; ordering barrier before the CSR read
349b: 28 0a     l32i.n a2,a10,0     ; a2 = *(sema CSR)   (a10 = CSR ptr)
349d: 27 b3 f7  bgeu   a3,a2,0x3498 ; spin while target(a3) >= val(a2)
34a0: 1d f0     retw.n              ; release when val > target

; DEVICE SEMAPHORE-SIGNAL (fenced CSR store), v3 @0x31c7
31c7: c0 20 00  memw                ; barrier
31ca: 49 0a     s32i.n a4,a10,0     ; *(CSR) = a4  (the SEMAPHORE_INC/SET write)

; DISPATCH READ (handler = *(DRAM+0xB0 + index*4)), v3 @0x3bf8
3bf8: 24 b0 00  const16 a2,0xB0
3bfb: 20 23 a0  addx4   a2,a3,a2
3bfe: 58 02     l32i.n  a5,a2,0

; IDLE LOOP, v3 @0x4b6c
4b6c: 00 7f 00  waiti 15
4b6f: c6 fa ff  j 0x4b5e
```

The wait family is the full `{wait-ge, wait-le, wait-eq, wait-ne}` set in two register
banks (`a2/a3` and `a5/a4`), **byte-stable 10/10/10** across v3/v4/v4+ (the a10
CSR-pointer convention); every CSR read in a wait and every CSR write in a signal is
**memw-fenced**. This is the device side of `add_semaphore_wait_ge_and_dec` (host op
`0x10A0/20`) + `add_semaphore_inc` (host op `0x10A0/21`) — exactly the §4/§6 handshake,
now seen on the LX core. The host **2R1W** (two semaphore reads, one write per step)
maps onto the two wait register banks + the one fenced signal store; the kangaring
**NR1W** is the same two banks supporting >1 outstanding read before the single fenced
signal. [device primitives OBSERVED HIGH — bytes decoded with the native ncore2gp
objdump; the leading operand bytes mis-decode but the stream RE-CONVERGES at the
verified `memw`; the per-step *schedule* (which sema, which target, which order) stays
MED — it lives in the `e/f`-dense body interior + the runtime-populated firmware DRAM
target values.]

---

## 9. Hard numbers

| quantity | value | provenance |
| -------- | ----- | ---------- |
| channels per ring config / ctx | **32** | loop `cmp …,0x1f ; jle`, OBSERVED HIGH |
| per-channel config stride | **`0x94` = 148 B** | `shl3/+/shl2/+/shl2` @`0x87e0`, OBSERVED HIGH |
| per-channel runtime ctx stride | `0x10` = 16 B | `shl rdx,0x4` @`0x171c9`, OBSERVED HIGH |
| `kring_peer_semas` size | `0x20` = 32 B (1 `mine` + 3 `peers`) | OBSERVED HIGH |
| kangaring peer fanout | **3 slots** | loop `cmp …,0x2 ; jle`, OBSERVED HIGH |
| `ring_neighbor` size | `0x1c` = 28 B; `net_idx_addrs[fold_n]` | OBSERVED HIGH |
| `dma_apb_bcast` size | `0x14` = 20 B | OBSERVED HIGH |
| `reduction_type_t` KANGARING_NR1W | **`2`** | DWARF DIE `<0x60c9a8>`, OBSERVED HIGH |
| `node_peer` / `node_next` size | 56 B / 48 B | DWARF byte_size, OBSERVED HIGH |
| kangaring hop fanout (DMA engines) | primary **8** / secondary **4** | `.rodata` maps, OBSERVED HIGH |
| arch ids → blob | `0x05`=v2/sunda, `0x0c`=v3/cayman, `0x14`=v4/mariana, `0x1c`=v4+/m_plus | OBSERVED HIGH |
| NCFW generations shipped | **4** (v5/MAVERICK absent) | OBSERVED HIGH |
| device wait/signal substrate | memw-fenced spin-poll + fenced store | scalar-LX decode, OBSERVED HIGH |

---

## 10. Residual uncertainty

- The **kangaring hop schedule** (which `peers[]` are signalled in which step for a
  given `num_peers`/`is_primary`) executes inside the LX firmware; only the data
  structures it consumes (§4/§5) are host-decodable. [LOW on schedule; HIGH on
  structures.]
- The concrete `soc_addr` / per-step target **integers** are runtime-populated in
  firmware DRAM (the `.data` caveat), not in any shipped static file. [LOW on integers.]
- `m2s`/`s2m` expansion ("memory↔semaphore") is inferred from the recurring naming in
  both the apb tail ptrs (§2.3) and the runtime counters (§3); not spelled out in any
  string. [MED.]
- Whether the ring all-reduce reuses the device barrier's exact 4-step `target_sema_val`
  array or a channel-scoped variant is INFERRED — the barrier decoder is the only one
  exposing a per-step target-value array. [MED.]
- Any **NC-v5 / MAVERICK** NCFW interior is **file-absent**; not stated as fact here.

---

## See also

- [ALL_REDUCE](../ops/all-reduce.md) — the host lowering that selects ring vs
  kangaring (`enc_can_post_single_cycle_ring` @`0xfaa80`,
  `enc_can_post_kangaring_operation` @`0xfbe70`, intra-only).
- [Collective enums](../ops/collective-enums.md) — `enc_alg_type {RING=0, …,
  KANGARING=3}`, `reduction_type_t {RING_2R1W=0, RING_2R2W=1, KANGARING_NR1W=2}`.
- [CCE in-transfer reduce](../../dma/cce-in-transfer.md) — the per-element FMA/ADD/MIN/
  MAX arithmetic the NR1W fold drives.
- [S3D3 Collective (SB2SB `0xBF`)](../ops/s3d3-collective.md) — the per-step transport
  the ring step moves bytes with.
- [DMA-reprogram APB-broadcast](dma-reprogram-apb-bcast.md) — the channel `+0x78`
  `dma_apb_bcast` tail-pointer fanout.
- [NEFF device barrier](neff-device-barrier.md) — the global 4-step counted barrier the
  kring handshake is distinct from but composes with.
- [Ring send/wait + config command](ring-protocol-config-command.md) — the cc_op word
  and `ring_wait_complete` / `ring_send_complete` step flags.
- [pring descriptors](pring-descriptors.md) — the persistent DMA descriptor ring.
- [Mesh](mesh-collective.md) and [Hierarchical](hierarchical-collective.md) — the other
  two `algo_type` union arms of `ncfw_log_algo_configs`.
- [spad cc_op / tsync](spad-ccop-tsync.md) — the spad-ctrl entry that routes
  `algo_type=3` to this ring tape.
