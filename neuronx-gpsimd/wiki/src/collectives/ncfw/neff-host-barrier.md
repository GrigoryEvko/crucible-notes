# NEFF Host Barrier + Step-Config Sequencing

This page documents the **host side of the NCFW NEFF barrier** — the host↔device
synchronization handshake that complements the on-device barrier, together with the
full per-step barrier configuration and the **4-step barrier algorithm**. It is the
host-x86-64 counterpart to the device-internal fence: where the
[NEFF Device Barrier](neff-device-barrier.md) page owns the on-device 4-step
execution, **this page owns (1) the `host_barrier` sub-schema (`barrier_start` /
`barrier_done`, the two semaphore CSRs the host pokes), (2) the `barrier_configs`
parent struct and its `execute_device_barrier` gate, (3) the 4-step algorithm tables
that decide *what each step does*, and (4) the host-side build/handshake path that
constructs that config and drives the round-trip.**

Two binaries back this page, and the distinction matters:

* **`libncfw.so`** is the firmware-config **logger** — it walks the struct the
  firmware reads and pretty-prints it as JSON. It tells us the **layout** but only
  *names* the fields.
* **`libnrt.so`** (the host Neuron RunTime) is the **encoder + executor** — it
  *builds* the very struct `libncfw` logs and *drives* the host↔device handshake.
  It tells us the **semantics**: the named constants, the algorithm tables, the
  semaphore-ID assignment, and the host-CC-mode sema selection. This page grounds the
  field *meaning* (not just the layout) in real encoder code.

> **GOTCHA — two Xtensa cores, do not cross the wires.** The struct decoded here is
> read by the **scalar Xtensa-LX management core** (NCFW), *not* the Vision-Q7 "Cairo"
> FLIX datapath that runs custom-op kernels. The barrier semaphores are TOP_SP
> hardware semaphores; the NCFW LX core arms them and the firmware's leader
> stream-processors increment them. Neither involves the Q7 SIMD ucode (which ships
> in `libnrtucode_extisa.so`). See [NCFW DRAM Images + ctx_log](ncfw-dram-ctx-log.md).

**Provenance & confidence.** Every fact below is read **this session** from two
shipped host libraries with stock binutils (`readelf` / `nm` / `objdump -M intel` /
`sha256sum`), the IDA-recovered sidecars (functions / structures / enums / strings /
disasm — themselves binary-derived), and a Python ELF/byte/struct reader. Lawful
interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no vendor source
snapshot consulted. Tags follow the
[Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte /
size / symbol / disassembler output read from a binary **this pass**; `CARRIED` =
OBSERVED in a cited prior carve and reused; `INFERRED` = reasoned over those. Callouts:
**QUIRK** (counter-intuitive but real), **GOTCHA** (a reimplementation trap), **NOTE**
(orientation), **CORRECTION** (overturns a prior reading).

> **THE v5 / MAVERICK WALL.** `libncfw` ships **exactly four** NCFW generations
> (v2/v3/v4/v4_plus); `nm` lists exactly four copies of every barrier decoder and the
> four arch parents read the gate identically (§7). **There is no MAVERICK (v5) NCFW
> barrier decoder in this binary** — any claim about a v5 NCFW barrier interior would
> be fabrication. v5 is named here only to mark the wall. `[WALL]`

---

## 0. Target binaries (anchors match this session)

| Field | Value | How verified `[HIGH/OBSERVED]` |
|---|---|---|
| `libncfw.so` path | `…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libncfw.so` | `fd --no-ignore` |
| `libncfw.so` size | **615640** bytes | `stat -c %s` |
| `libncfw.so` SHA-256 | `598920d743762c03b3007c089829c02d0095408bf431fa3533e508c5f0aa3e49` | `sha256sum` |
| `libncfw.so` BuildID | `a98f8e1ca2294582835310c3a1092e0a5e500db5` | `readelf -n` |
| `libncfw.so` `.rodata` | PROGBITS, **VMA == off == `0x65000`**, size `0x2c8e4` | `readelf -SW` |
| `libnrt.so` path | `…/opt/aws/neuron/lib/libnrt.so.2.31.24.0` | IDA `input_file` |
| `libnrt.so` SHA-256 | `956382de73f4cced5d9a0dc040ca82843fb37aef00d5bf2241f343ff02cd59a6` | IDA metadata |
| `libnrt.so` `.text` | VMA `0x3dbc0`–`0x7ce6d9` (**VMA == file off**) | IDA segments |
| `libnrt.so` `.rodata` | VMA `0x7cf000`–`0xad41b3`, size `0x305a73` (**VMA == file off**) | IDA segments |
| `libnrt.so` functions | **17372** recovered | IDA metadata |

Every byte, string, and array quoted below lives in `.text` / `.rodata`, where **VMA
equals file offset** in both binaries. No `.data` VMA↔file delta correction is needed
for any quoted byte (the per-step `soc_addr` *integers* are written at runtime by
`libnrt`; this page fixes the layout, the algorithm, and the sema-ID semantics, not the
runtime address values).

---

## 1. The decoder/call tree — one base, two views

`libncfw`'s parent decoder `ncfw_log_neff_barrier_config @0x10f8b` receives one config
base pointer and fans it out to **both** sub-decoders **plus** the device step
decoder, **all sharing the identical base**. This is the structural keystone of the
whole page, so it is verified instruction-by-instruction.

```
ncfw_log_neff_barrier_config              @0x10f8b   [HIGH/OBSERVED]
  │  10fa5: mov [rbp-0x60], rcx           ; stash cfg base
  │  11216: mov rax, [rbp-0x60]           ; reload cfg
  │  1121a: movzx eax, BYTE PTR [rax+0x124]; execute_device_barrier (u8)
  │  11221: and eax, 0x1                  ; mask bit0
  │  11253: lea rdx,[rip+0x540ce]  # 0x65328 "%p" ; printed pointer-style
  │  11265: call snprintf@plt
  ├─ host_barrier  call:                  key 0x6545a "host_barrier"
  │     11313: mov rdx, [rbp-0x60]        ; cfg  (RELOAD, unmodified)
  │     1132b: call 0xd613 <ncfw_log_neff_host_barrier_config>
  └─ device_barrier call:                 key 0x65467 "device_barrier"
        11330: mov rdx, [rbp-0x60]        ; SAME cfg (RELOAD, unmodified)
        11348: call 0xfef5 <ncfw_log_neff_device_barrier_config>
```

Both call sites literally re-load `[rbp-0x60]` and pass it unmodified
(`0x11313`/`0x11330`). And the device decoder in turn passes that **same base, no
offset**, to the step decoder:

```
ncfw_log_neff_device_barrier_config       @0xfef5    [HIGH/OBSERVED]
  ff1b: mov [rbp-0xb0], rcx               ; stash cfg
  10e3f: mov rdx, [rbp-0xb0]              ; cfg (no offset)
  10e53: mov rcx, rdx
  10e60: call 0xed02 <ncfw_log_neff_device_barrier_step_config>
```

> **CORRECTION (vs the NCFW device-barrier carve's "separate sub-struct bases" note).**
> `host_barrier` and `device_barrier` do **not** receive pointers to their own
> sub-structs. They are **two views of the same 0x124-byte struct**: the host view's
> `barrier_start@+0x00` / `barrier_done@+0x08` overlay the exact bytes the device view
> reads as `barrier_step_config[0].m2s_val` / `s2m_val` / `barrier_sema[0]`. The
> `execute_device_barrier` flag at `+0x124` selects which interpretation is live. The
> IDA-recovered parent type confirms this exactly (§2.2): `neff_barrier_configs_t` is
> size `0x128` with an anonymous overlay at `+0x000` and the gate bitfield at `+0x124`.
> `[HIGH/OBSERVED — both call sites reload [rbp-0x60]; the parent struct type backs it]`

### 1.1 The shared address helper — `ncfw_log_addr @0x41c3`

`barrier_start` and several device fields are emitted through one shared helper:

```c
// ncfw_log_addr @0x41c3  [HIGH/OBSERVED — signature reconstructed from disasm]
//   edi=compact?, rsi=buf, edx=indent, rcx=key, r8=struct_ptr
// emits:  "<key>": { "addr": { "soc_addr": "0x%016lX" } }
void ncfw_log_addr(int compact, char *buf, int indent,
                   const char *key, const u64 *struct_ptr) {
    u64 v = struct_ptr[0];            // 0x43dd: mov r12, [rax]   (reads +0x00)
    // emits key 0x65176 "addr", 0x6511c "soc_addr",
    //        format 0x65127 "%s: \"0x%016lX\"\n"
}
```

---

## 2. The `host_barrier` sub-schema — `ncfw_log_neff_host_barrier_config @0xd613`

The host-barrier struct is **exactly two `soc_addr` fields = 16 bytes**, byte-identical
across all four arch copies (size `0x7a9` each, §7). Args: `rdi=buf`, `esi=indent`,
`rdx=key`, `rcx=cfg`.

| off | size | field | type / how read | conf |
|---|---|---|---|---|
| `+0x00` | 8 | `barrier_start` | `soc_addr_t` via `ncfw_log_addr` | HIGH/OBSERVED |
| `+0x08` | 8 | `barrier_done` | `semaphore_t`, inline | HIGH/OBSERVED |

```c
// ncfw_log_neff_host_barrier_config @0xd613  [HIGH/OBSERVED]
void ncfw_log_neff_host_barrier_config(char *buf, int indent,
                                       const char *key, const u8 *cfg) {
    // barrier_start: via the shared helper (which prints the addr/soc_addr wrapper)
    //   d7b2: mov r8, rcx            ; r8 = cfg
    //   d7b5: lea rcx,[rip+0x57bb5]  # 0x65371 "barrier_start"
    //   d7bf: mov edi, 1
    //   d7c4: call 0x41c3 <ncfw_log_addr>   ; reads cfg[+0x00]
    log_addr(/*compact=*/1, buf, indent, /*key=*/"barrier_start", (u64*)(cfg + 0x00));

    // barrier_done: OPEN-CODED inline, but emits the byte-identical wrapper
    //   d870: lea rdx,[rip+0x57b08]  # 0x6537f "barrier_done"
    //   da3c: mov rax, [rbp-0x70]    ; rax = cfg
    //   da40: mov r12, [rax+0x8]     ; reads cfg[+0x08]
    u64 done = *(u64*)(cfg + 0x08);   // "barrier_done":{"addr":{"soc_addr":"0x%016lX"}}
}
```

The **only** two struct dereferences in the whole 0x7a9-byte function are
`ncfw_log_addr(cfg)` at `0xd7c4` (reading `cfg+0x00`) and `[rax+0x8]` at `0xda40`. So
`host_barrier` is exactly `{ barrier_start@+0x0, barrier_done@+0x8 }`, 16 bytes,
nothing more. The IDA struct `neff_host_barrier_configs_t` (size `0x10`) confirms it:
`barrier_start soc_addr_t @+0x0`, `barrier_done semaphore_t @+0x8`. `[HIGH/OBSERVED]`

> **NOTE — the inline/helper asymmetry is a codegen artifact.** `barrier_start` uses
> the shared `ncfw_log_addr`; `barrier_done` is open-coded. Both emit the identical
> `"addr":{"soc_addr":"0x…"}` wrapper. They are *both* addr-wrapped u64 `soc_addr`
> semaphore CSR pointers; the field types differ only in their IDA names
> (`soc_addr_t` vs `semaphore_t`, both `{ addr_t addr; }` = 8 bytes).

### 2.1 `.rodata` strings that prove the shape (all read this session)

| VMA | bytes |
|---|---|
| `0x65371` | `barrier_start` |
| `0x6537f` | `barrier_done` |
| `0x6545a` | `host_barrier` |
| `0x65467` | `device_barrier` |
| `0x65176` | `addr` |
| `0x6511c` | `"soc_addr"` |
| `0x65127` | `%s: "0x%016lX"\n` |
| `0x65328` | `%p` |

Emitted JSON shape:

```json
"host_barrier": {
  "barrier_start": { "addr": { "soc_addr": "0x...." } },
  "barrier_done":  { "addr": { "soc_addr": "0x...." } }
}
```

### 2.2 The parent struct (IDA-recovered types) — the overlay made explicit

The `libnrt` type recovery makes the §1 overlay precise. The three barrier struct
types ship as IDA TIL types (binary-derived) at the sizes the `libncfw` byte layout
predicts:

```c
struct neff_barrier_configs_t {                 // size 0x128  [HIGH/OBSERVED]
    /* +0x000 */ <anon overlay>;                // device view ∪ host view (0x124 B)
    /* +0x124 */ uint8_t execute_device_barrier : 1;  // the gate (masked &1)
    /* +0x124 */ uint8_t __reserved0 : 7;
    /* +0x125 */ uint8_t __reserved[3];         // tail pad to 0x128
};
struct neff_host_barrier_configs_t {            // size 0x10
    /* +0x00 */ soc_addr_t  barrier_start;
    /* +0x08 */ semaphore_t barrier_done;
};
```

> **CORRECTION / RESOLUTION of the device-barrier carve's "open: struct bytes past
> `+0x124`".** The tail is **not** mystery padding: `execute_device_barrier` is a
> 1-bit field at `+0x124` followed by `__reserved0:7` and a 3-byte `__reserved[3]`,
> bringing `neff_barrier_configs_t` to `0x128`. The gate byte therefore carries seven
> spare bits. `[HIGH/OBSERVED — IDA struct member offsets/sizes]`

---

## 3. The `device_barrier` struct — `ncfw_log_neff_device_barrier_config @0xfef5`

Re-verified byte-for-byte this session (every offset OBSERVED via its load
instruction). Reproduced for self-containment, with one field the logger does **not**
expose, recovered from the IDA struct.

| off | size | field | type | load (sunda/v2 copy) |
|---|---|---|---|---|
| `+0x000` | `0xD0` | `barrier_step_config[4]` | per-step `[4]` | step decoder gets base `@0x10e60` (§4) |
| `+0x0D0` | `0x20` | `dma_sync_sema[4]` | `semaphore_t×4` | `lea [rax+0xd0] @0x1010d`; child `@0xf5d0` |
| `+0x0F0` | `0x14` | `dma_apb_bcast` | `dma_channel_apb_bcast_t` | `lea [rax+0xf0] @0x100e0`; child `@0x4899` |
| `+0x104` | 8 | `start_network_proxy` | `soc_addr_t` | `lea [rax+0x104] @0x1035d` |
| `+0x10C` | 4 | `dma_sync_sema_value` | `u32` | `mov ebx,[rax+0x10c] @0x1024c` (`"%u"`) |
| `+0x110` | 4 | `tdrbp_low` | `u32` | `mov ebx,[rax+0x110] @0x104a1` |
| `+0x114` | 4 | `tdrbp_high` | `u32` | `mov ebx,[rax+0x114] @0x106c4` |
| `+0x118` | 4 | `desc_count` | `u32` | `mov ebx,[rax+0x118] @0x108e7` |
| `+0x11C` | 4 | `total_desc_n` | `u32` | **NOT logged** — IDA struct only |
| `+0x120` | 2 | `dma_engines_bitmap` | `u16` | `movzx eax,[rax+0x120] @0x10b0a` (`"%hu"`) |
| `+0x122` | 1 | `queue_id` | `u8` | `movzx eax,[rax+0x122] @0x10d31` (`"%hhu"`) |
| `+0x123` | 1 | `__reserved` | `u8` | IDA struct only |

> **GOTCHA — `total_desc_n@+0x11C` is real but invisible to the logger.** The
> `libncfw` decoder reads `desc_count@+0x118` and then jumps straight to
> `dma_engines_bitmap@+0x120`, silently stepping over `+0x11C`. The
> `neff_device_barrier_configs_t` IDA struct (size `0x124`) shows the gap is
> `total_desc_n uint32_t`. A reimplementation that mirrored only the logged fields
> would mis-size the struct by 4 bytes. `[HIGH/OBSERVED — IDA struct + the
> 0x118→0x120 load gap]`

The `dma_apb_bcast` substruct (IDA `dma_channel_apb_bcast_t`, size `0x14`):
`m2s_tail_ptr addr_t @+0x0`, `s2m_tail_ptr addr_t @+0x8`, `mask u32 @+0x10` —
the same `m2s`/`s2m` tail-pointer pair as the ring/apb-bcast path. `[HIGH/OBSERVED]`

**Named semantic grounding (from `libnrt`):** `barrier_step_config` has
`NEFF_DEVICE_BARRIER_MAX_STEPS = 4` entries; each step's sema array is
`NEFF_DEVICE_BARRIER_MAX_SEMA_COUNT = 4` = `NEFF_DEVICE_BARRIER_MAX_LEADERS = 4`. The
runtime asserts `idx < NEFF_DEVICE_BARRIER_MAX_LEADERS` (string present), so
`barrier_sema[4]` is **the up-to-4 LEADER stream-processors' barrier semaphores** —
the fan-in is specifically 4 leader SPs. `dma_engines_bitmap` is filled by
`<arch>_get_dma_eng_for_barrier`, `barrier_sema` CSRs by
`<arch>_get_barrier_sem_mapping` (§6). `[HIGH/OBSERVED — strings/symbols present]`

---

## 4. Step-config decode + the 4-step algorithm

### 4.0 Per-step descriptor — `ncfw_log_neff_device_barrier_step_config @0xed02`

4 entries, **52-byte stride** (`0x34`), proved by the index arithmetic and the loop
bound. The IDA type `neff_device_barrier_per_step_configs_t` (size `0x34`) matches the
byte layout exactly.

```
; STRIDE proof @0xf00d-0xf023  [HIGH/OBSERVED]
;   f00d: mov  eax,[rbp-0x54]   ; idx
;   f010: movsxd rdx, eax
;   f016: add  rax, rax         ; 2i
;   f019: add  rax, rdx         ; 3i
;   f01c: shl  rax, 2           ; 12i
;   f020: add  rax, rdx         ; 13i
;   f023: shl  rax, 2           ; 52i = 0x34
; LOOP bound @0xf4c1: cmp [rbp-0x54],0x3 ; jle  => idx 0..3  (4 steps)
```

| off | size | field | type | load |
|---|---|---|---|---|
| `+0x00` | 2 | `m2s_val` | `u16` | `movzx eax,WORD [base+idx*0x34] @0xf031` |
| `+0x02` | 2 | `s2m_val` | `u16` | `movzx eax,WORD [base+idx*0x34+0x2] @0xf23e` |
| `+0x04` | `0x20` | `barrier_sema[4]` | `semaphore_t×4` | `lea [..+0x4] @0xf355` → child `@0xddbc` |
| `+0x24` | `0x10` | `target_sema_val[4]` | `u32×4` | `lea [..+0x24] @0xf396` → child `@0xe6e1` |

The two children loop exactly 4× each:
`ncfw_log_neff_device_barrier_semaphores @0xddbc` reads `mov r12,[base+rdx*8] @0xe24d`
(u64, stride 8); `target_sema_val` child `@0xe6e1` reads `mov ebx,[base+rdx*4] @0xe9f6`
(u32, stride 4). `[HIGH/OBSERVED]`

`m2s_val`/`s2m_val` are per-step "memory→semaphore" / "semaphore→memory" counter
deltas (same `m2s`/`s2m` naming as the apb-bcast tail pointers). `[widths
HIGH/OBSERVED; "counter delta" INFERRED MED]`

### 4.1 What makes 4 steps — the compile-time tables (`libnrt .rodata`)

Four named const arrays, **raw bytes read this session** from
`…_rodata.bin` (VMA == file offset, base `0x7cf000`):

| symbol | VMA | bytes |
|---|---|---|
| `_ZL18barrier_step_sizes` | `0x857b80` | `01 04 04 01` = `{1,4,4,1}` |
| `_ZL36barrier_step_sizes_for_switch_family` | `0x857af8` | `01 02 02 01` = `{1,2,2,1}` |
| `_ZL23barrier_algorithm_steps` | `0x857ba0` | 128 B (32×u32) |
| `_ZL41barrier_algorithm_steps_for_switch_family` | `0x857b00` | 128 B (32×u32) |

Each `algorithm_steps` table is `[step:0..3][entry:0..3] = { field0:u32, field1:u32 }`
— **step stride `0x20`** (4 entries × 8 B), **entry stride 8 B**. `barrier_step_sizes
[step]` says how many of the 4 entries are **active** that step. Decoded raw:

```
DEFAULT  (barrier_step_sizes = {1,4,4,1}):
  step0 [1]: (0,0)
  step1 [4]: (1,0) (2,0) (3,0) (4,0)
  step2 [4]: (1,1) (2,1) (3,1) (4,1)
  step3 [1]: (0,1)
  inactive tail slots padded with (7,2)

SWITCH-FAMILY  (barrier_step_sizes = {1,2,2,1}; NVSwitch-like fabric):
  step0 [1]: (0,0)
  step1 [2]: (5,0) (6,0)
  step2 [2]: (5,1) (6,1)
  step3 [1]: (0,1)
  inactive tail slots padded with (7,2)
```

`[all raw bytes + decode HIGH/OBSERVED]`

**What the two fields ARE — the IDA enums decode the table exactly.** Both
`barrier_type_t` and `barrier_operation_t` are recovered as 4-byte IDA enums
(binary-derived):

```c
enum barrier_type_t {              // field0 (the SEARCH KEY)   [HIGH/OBSERVED]
  BARRIER_INTRA_CHIP             = 0,
  BARRIER_INTRA_POD             = 1,
  BARRIER_INTER_CHIP            = 2,
  BARRIER_INTER_CHIP_RING_LOCAL = 3,
  BARRIER_INTER_CHIP_RING_REMOTE= 4,
  BARRIER_INTRA_RACK            = 5,
  BARRIER_INTER_RACK            = 6,
  BARRIER_TYPE_MAX              = 7,    // = the (7,_) pad type
};
enum barrier_operation_t {         // field1 (the RETURNED operation)
  BARRIER_ENTRY       = 0,
  BARRIER_EXIT        = 1,
  BARRIER_OP_TYPE_MAX = 2,             // = the (_,2) pad op
};
```

> **CORRECTION — field1 is ENTRY/EXIT, not "signal/wait/nop".** An earlier reading
> INFERRED field1 ∈ {signal=0, wait=1, nop=2}. The recovered `barrier_operation_t`
> enum names it literally: **`BARRIER_ENTRY=0`, `BARRIER_EXIT=1`,
> `BARRIER_OP_TYPE_MAX=2`** (the pad). field0 is the `barrier_type_t` key, **not** a
> sema id. So the 4-step program reads:
>
> | step | DEFAULT entries | meaning |
> |---|---|---|
> | 0 | `(INTRA_CHIP, ENTRY)` | local-chip **arrive** |
> | 1 | `(INTRA_POD\|INTER_CHIP\|RING_LOCAL\|RING_REMOTE, ENTRY)` | **arrive** across up to 4 scope tiers |
> | 2 | `(same four types, EXIT)` | **release** across those tiers |
> | 3 | `(INTRA_CHIP, EXIT)` | local-chip **release** |
>
> SWITCH-FAMILY collapses step1/step2 to two entries using `INTRA_RACK=5` /
> `INTER_RACK=6` (the NVSwitch fabric aggregates rack-scope arrivals). The `{1,4,4,1}`
> vs `{1,2,2,1}` size choice is the **only** algorithm difference between fabrics; the
> 52-byte descriptor schema is identical (§7). `[the table values + enum names
> HIGH/OBSERVED; the per-step ENTRY/EXIT→signal/wait lowering MED — see §5]`

### 4.2 The table consumer — `search_barr_type_in_step @0xf58a0`

```
; _ZL24search_barr_type_in_step14barrier_type_tiP19barrier_operation_tPi  @0xf58a0
;   search_barr_type_in_step(barrier_type_t edi, int step esi,
;                            barrier_operation_t* rdx, int* rcx) -> bool
;   [HIGH/OBSERVED — full 155-byte body read this session]
f58a2: lea r14, _ZL23barrier_algorithm_steps
f58ba: call nrt_is_neuron_switch_v1_family
f58c8: cmovnz r14, algorithm_steps_for_switch_family     ; pick table by fabric
f58dd/e4: rax = switch ? step_sizes_for_switch_family : step_sizes
f58e8: movzx ecx,[rax + step]            ; cl = active count for this step
f58f5: shl rdx,5 ; rdx = algorithm_steps + step*0x20     ; step row base
f5908: cmp [rdx + i*8], ebx              ; match FIELD0 vs barrier_type (edi)
f5914: mov edx,[r14 + (i + step*4)*8 + 4]; FIELD1 = operation
f5919: mov [op_type], edx                ; *out_operation = operation
f591d: mov [step_offset], eax            ; *out_index = matched entry index
f5920: mov eax,1 ; ret                   ; found
```

This confirms field0 is the `barrier_type` **search key**, field1 is the **returned
operation**, step stride `0x20`, entry stride 8, with the per-step match count gated by
`barrier_step_sizes[step]`. It is **the** map from `(barrier_type, step)` to the
operation a step performs, and it is called from
`barrier_composer::compose_ncfw_configs_for_barrier` (§6). `[HIGH/OBSERVED]`

---

## 5. Semaphore-ID assignment + value/threshold semantics

The per-step operation lowers to TOP_SP semaphore signal/wait. The TOP_SP semaphore-ID
assignment (the `ENCD_*_SEMA_ID` slot constants) is recovered from `libnrt`:

| id | constant | role |
|---|---|---|
| 0 | `ENCD_TRIGGER_SEMA_ID` | step0/step3 "self" trigger |
| 1 | `ENCD_COMPLETION_SEMA_ID` | completion |
| 2 | `ENCD_BARRIER_SEMA_ID0` | device-mode barrier-done base |
| 3 | `ENCD_BARRIER_SEMA_ID1` | second device-mode barrier sema |
| 4 | `ENCD_BARRIER_DMA_SYNC_SEMA_ID` | == `dma_sync_sema` (§3 `+0xd0`) |
| 5 | `ENCD_FUNCTION_SWITCH_SEMA_ID` | switch-family rack-scope sema |
| 6 | `ENCD_HOST_BARRIER_SEMA_ID0` | **host `barrier_start`** (host-CC mode) |
| 7 | `ENCD_HOST_BARRIER_SEMA_ID1` | **host `barrier_done`** (host-CC mode) |
| `MAX-1` | `ENCD_NULL_SEMA_ID` | `MAX_TOP_SP_SEMAPHORES-1` |

> **NOTE — keep two numbering spaces distinct.** field0 in §4 indexes
> `barrier_type_t` (chip/pod/chip-ring/rack scopes), **not** these sema ids. The sema
> ids are how each `(type, op)` entry is *lowered* to a concrete CSR by the per-arch
> `get_barrier_sem_mapping` (§6); the algorithm tables are scope-typed, the lowering is
> sema-id-typed. Conflating them is the trap.

**SIGNAL / WAIT primitives (device-side ISA, byte-confirmed in `libnrt`):**

```
; add_semaphore_inc @0x273860            => SIGNAL (BARRIER_ENTRY leg)  [HIGH/OBSERVED]
;   273868: mov eax, 0x10A0   ; opcode
;   27387d: mov BYTE [..], 0x15 (=21)    ; subop 21
;   273882: imm = 1                       ; increment by 1
; add_semaphore_wait_ge_and_dec @0x273a20 => WAIT (BARRIER_EXIT leg)    [HIGH/OBSERVED]
;   273a28: mov eax, 0x10A0   ; opcode
;   273a46: mov BYTE [..], 0x05            ; compare mode (>=)
;   273a4f: mov BYTE [..], 0x14 (=20)     ; subop 20
;   273a54: imm = 1                        ; decrement by 1 after pass
;   273a4b: store ecx                      ; the GE threshold
```

So WAIT (op `BARRIER_EXIT`) blocks until `barrier_sema[step][leader] >=
target_sema_val[step][leader]` then **decrements**; SIGNAL (op `BARRIER_ENTRY`) does
`add_semaphore_inc`. Related variants present: `add_semaphore_inc_val @0x273820`,
`add_semaphore_wait_ge @0x273ac0`, `add_semaphore_wait_eq_and_inc @0x2739d0`,
`add_semaphore_wait_eq_and_clear @0x273a70`. `[opcodes/subops HIGH/OBSERVED; the
per-step ENTRY→inc / EXIT→wait-ge-dec binding INFERRED MED-HIGH]`

The `target_sema_val[step][leader]` u32 array is the per-(step,leader) GE threshold;
with `MAX_LEADERS=4`, step1/step2 wait the AND of up to four GE conditions — i.e. all
participating leaders must arrive. `dma_sync_sema[4]` (sema id 4) must reach
`dma_sync_sema_value` (`+0x10c`) to declare the DMA/RDMA transport drained — the bridge
folding transport completion into the step counter. `[fields HIGH/OBSERVED; the
all-leaders-AND + bridge role INFERRED MED]`

---

## 6. The host build + handshake path (`libnrt` encoder/executor)

This is the part the device-barrier carve could only infer: the host code that
**builds** the struct and **drives** the round-trip.

### 6.1 `execute_device_barrier` gating

```
; in the libncfw parent (identical in all 4 arch copies)  [HIGH/OBSERVED]
11216: mov   rax,[rbp-0x60]              ; cfg
1121a: movzx eax,BYTE PTR [rax+0x124]    ; execute_device_barrier (u8)
11221: and   eax,0x1                     ; -> bit0
11253: lea   rdx,[rip+...] # 0x65328 "%p"; printed pointer-style
11265: call  snprintf@plt
```

> **QUIRK — a boolean printed as a pointer.** `execute_device_barrier` is a 1-bit flag
> (`&1`) but the logger formats it with **`%p`** (`0x65328`), so it surfaces in the
> JSON as `0x0`/`0x1`. When set, the on-device 4-step barrier runs; when clear, only
> the host-barrier handshake gates the collective. `[bit-mask + offset + format
> HIGH/OBSERVED; the "set ⇒ device barrier runs" semantics INFERRED MED from the gate
> name + the parent struct overlay]`

### 6.2 The host↔device handshake — who arms `barrier_start`, who waits `barrier_done`

The host arms `barrier_start` (release the device into the collective), the device
leaders increment `barrier_done`, and the host (and non-leader workers) wait on
`barrier_done`. The leader-done **inc-address computation** is byte-confirmed:

```
; encd_get_leader_sps_barrier_done_semas_inc_addrs @0x240d40  [HIGH/OBSERVED]
;   for each leader SP (drv_ctx[+0x90] of them, per-SP stride 0x138):
240d8f: mov rbx, [sp_struct + 0x6820]    ; SP semaphore base
240d96: call encd_is_host_cc             ; al = (ctx[+0x231] == 1)
240d9b: cmp al, 1
240d9d: sbb edi, edi                     ; host_cc -> 0 ; else -> -1
240daa: and edi, 0xfffffffc              ;          0 ;       -4
240dad: add edi, 6                       ;   sem_id = 6 (host_cc) else 2
240db0: call encd_arch_get_sp_sema_i_ofst_for_bar_mapped_vaddr
240dbe: add rbx, rcx                     ; inc-addr = SP base + sema offset
240dc4: mov [out + idx*8], rbx           ; store the leader's done inc-addr
```

```c
// encd_is_host_cc @0x234a90   [HIGH/OBSERVED]  — a single per-context flag
bool encd_is_host_cc(const ctx_t *ctx) {
    return ctx->byte_0x231 == 1;          // 234a90: cmp byte [rdi+0x231],1; setz al
}
```

So the **leader-done sema is id 6 (`ENCD_HOST_BARRIER_SEMA_ID0`) in host-CC mode,
else id 2 (`ENCD_BARRIER_SEMA_ID0`)**. `[HIGH/OBSERVED]`

The host-wait leg is corroborated by `libnrt` strings and the host API surface:

```
"non-leader workers waiting at barrier exceeded %d seconds"
"failure waiting for barrier release from gid %d"
"EXEC_STATE_WAIT_BARRIER_PROXY"
"Waiting on barrier proxy task: %u sec"
```
Exports / symbols: `nrt_barrier` (native export), `add_sync_barrier`,
`kbl_sync_mode_exec_enc_barrier`, `kbl_async_mode_exec_enc_barrier`,
`bp_barrier_wait` / `bp_barrier_release` / `bp_barrier_hangup`,
`enc_check_proxy_barrier_task_status @0xfd380`.

**Round-trip (now code-grounded):**

1. **HOST** arms `barrier_start` (host_barrier sema id 6) → releases the device's
   TOP_SPs into the collective.
2. **DEVICE** runs the 4-step device barrier (§4) + the ring/mesh data phase.
3. **DEVICE** leader SPs `add_semaphore_inc` `barrier_done` (sema id 7 in host-CC mode)
   — inc-addrs from `encd_get_leader_sps_barrier_done_semas_inc_addrs`.
4. **HOST** (and non-leader workers) **wait** on `barrier_done` before proceeding.

`[the leader-INC-on-done is HIGH/OBSERVED; the host being the writer of barrier_start
and waiter on barrier_done is INFERRED MED from the symbol/string set]`

> **NOTE — host-CC mode MERGES the two legs.** When `ctx[+0x231]==1`, the device
> leader's done-increment targets sema id 6/7 — i.e. the **same** semaphores the host
> polls as `host_barrier.barrier_done`. So host-CC makes the host barrier and the
> device barrier's completion the *same object*; non-host-CC keeps them on distinct
> sema ids (2/3 vs 6/7). The host barrier is the host↔device leg (always present); the
> 4-step device barrier is the device-internal cross-leader leg (gated by
> `execute_device_barrier`). A collective runs as: `host barrier_start →
> [if gate: 4-step device barrier] → ring/mesh data → [4-step device barrier] → leaders
> INC barrier_done → host waits barrier_done`. `[merge HIGH/OBSERVED from the
> sbb/and/add-6 selection; bracketing INFERRED MED]`

### 6.3 Config-staging entry points

The host runtime builds the struct via these `libnrt` symbols (all OBSERVED present):

```c
// encd_prep_ncfw_barr_config @0x2555d0  — per-(step,leader) sema staging  [HIGH/OBSERVED]
//   255604: call nrt_is_neuron_switch_v1_family       ; pick sema mapping by fabric
//   255642: call encd_arch_get_barrier_sem_mapping(tpb, barrier_type, switch_family)
//   25566c: movzx ecx, [ctx+0x231]                    ; read host_cc flag
//   255673: imul r9, 0x138                            ; per-SP stride 0x138
//   25570f: call encd_arch_get_sp_base_addr
//   255721: call encd_arch_get_sp_sema_r_ofst
//   255735: add rbx, rax                              ; soc_addr = SP base + sema ofst
//   255745: mov [config + ofst + 0x5a7c], rbx         ; write computed soc_addr
//   (asserts: "Invalid bit set %d for bar type", "Trying to set an already set bit";
//    src /opt/workspace/KaenaRuntime/tdrv/encd.c:0x37CD)
```

| symbol | addr | role |
|---|---|---|
| `encd_prep_ncfw_barr_config` | `0x2555d0` | per-(step,leader) sema staging (above) |
| `barrier_composer::compose_ncfw_configs_for_barrier` | `0x13d1e0` | iterates types; calls `search_barr_type_in_step @0x13d345`; RB-tree of nodes keyed on `barrier_type` @`[node+0x20]` |
| `barrier_composer::prep_dma_descs_for_barrier` | `0x13dd00` | builds the TDR-ring DMA descriptors (`tdrbp_low/high` + `desc_count`, §3) |
| `validate_and_populate_global_barrier_table` | `0x209ac0` | global table populate (863 B) |
| `encd_get_leader_sps_barrier_done_semas_inc_addrs` | `0x240d40` | leader done inc-addrs (§6.2) |
| `encd_set_ncfw_start_network_proxy_trigger` | `0x255530` | stages `start_network_proxy` (§3) |
| `encd_arch_get_barrier_sem_mapping` | `0x256110` | arch sema-mapping dispatcher |
| `encd_arch_get_sp_base_addr` | `0x255940` | SP base resolver |
| `encd_arch_get_sp_sema_r_ofst` | `0x2558c0` | r-window sema offset |
| `encd_arch_get_sp_sema_i_ofst_for_bar_mapped_vaddr` | `0x255920` | i-window (inc) sema offset (§6.2) |
| `encd_libncfw_init` / `encd_ncfw_init` / `encd_ncfw_configure_device_init` | `0x251cc0` / `0x251eb0` / `0x230c70` | NCFW lifecycle init |

Per-arch builders: `{sunda,mariana,cayman}_get_barrier_sem_mapping`
(`0x25e4f0`/`0x257530`/`0x25b380`) and `{sunda,mariana,cayman}_get_dma_eng_for_barrier`
(`0x25e680`/`0x259240`/`0x25c560`). Recovered config types:
`neff_barrier_configs_t`, `neff_host_barrier_configs_t`,
`neff_device_barrier_per_step_configs_t`, `barrier_composer`, `barrier_sem_mapping`.
`[all HIGH/OBSERVED — symbols/structures present]`

---

## 7. Per-arch ×4 variants

Every `libncfw` barrier decoder appears **4×** (arch images `0x05`/`0x0c`/`0x14`/`0x1c`
— SUNDA / CAYMAN(NC-v3) / MARIANA / MARIANA_PLUS; **do not invert these arch_ids**).
Counts re-grounded via `nm libncfw.so | rg -c`: exactly **4** copies each of the
parent, host-barrier, device-barrier, and step decoders. The host-barrier decoder is
**byte-identical in size (`0x7a9`) across all four**, and all four parents read the
gate with the identical `movzx [rax+0x124]; and eax,0x1`.

| arch | host_barrier | device_barrier | step_config | parent |
|---|---|---|---|---|
| v2 (SUNDA) | `0xd613` | `0xfef5` | `0xed02` | `0x10f8b` |
| v3 (CAYMAN) | `0x263ba` | `0x28c9c` | `0x27aa9` | `0x29d32` |
| v4 (MARIANA) | `0x3f161` | `0x41a43` | `0x40850` | `0x42ad9` |
| v4+ (MARIANA_PLUS) | `0x57f08` | `0x5a7ea` | `0x595f7` | `0x5b880` |

The **only** arch/topology variation in the *algorithm* (the `libnrt` side) is the
switch-family table selection (`{1,4,4,1}` + `barrier_algorithm_steps` vs `{1,2,2,1}` +
`barrier_algorithm_steps_for_switch_family`), chosen at runtime by
`nrt_is_neuron_switch_v1_family @0x5cb320` — **not** a per-image schema change. The
`libncfw` struct layout (offsets / strides / 4-counts) is schema-wide. `[HIGH/OBSERVED]`

---

## 8. Confidence summary

**HIGH / OBSERVED (read directly this session):**

* `host_barrier` = exactly 2 `soc_addr` (`barrier_start@+0x00` via `ncfw_log_addr`,
  `barrier_done@+0x08` inline), 16 B; `neff_host_barrier_configs_t` size `0x10` (§2).
* Parent passes **one shared base** to host + device + step decoders (all reload
  `[rbp-0x60]`/`[rbp-0xb0]`); host view overlays device `step_config[0]` (§1).
* `neff_barrier_configs_t` size `0x128`; `execute_device_barrier` = 1-bit field
  `@+0x124` (`movzx`+`and 1`, `%p`), `__reserved0:7` + `__reserved[3]` tail (§2.2/§6.1).
* Step descriptor: 4 steps, 52-B stride (13i<<2 proof), `m2s`/`s2m` u16,
  `barrier_sema[4]` u64 @`+0x4`, `target_sema_val[4]` u32 @`+0x24`; both children loop
  4× (stride 8 / stride 4) (§4.0).
* Device struct offsets `0x00`..`0x123` re-verified, plus the unlogged
  `total_desc_n@+0x11C` from the IDA struct (§3).
* 4-step algorithm tables: `barrier_step_sizes {1,4,4,1}` / switch `{1,2,2,1}`;
  `barrier_algorithm_steps[step][entry]={type,op}` raw bytes; consumer
  `search_barr_type_in_step` proves layout + selection (§4).
* `barrier_type_t` / `barrier_operation_t` enums recovered with member values —
  field1 = `BARRIER_ENTRY=0`/`BARRIER_EXIT=1`/pad=2 (§4.1).
* host-CC selects host-barrier sema id 6 for leader-done inc (`sbb`/`and`/`add 6`) via
  `encd_get_leader_sps_barrier_done_semas_inc_addrs`; `host_cc = ctx[+0x231]==1` (§6.2).
* Staging entry points + per-arch sema/dma-eng mappers all present (§6.3).
* 4-arch byte-identical host decoder size + identical gate code (§7).

**INFERRED / MED:** handshake *direction* (host writes `barrier_start`, waits
`barrier_done`) — the leader-INC-on-done is HIGH, host being writer/waiter is MED; the
per-step ENTRY→`add_semaphore_inc` / EXIT→`add_semaphore_wait_ge_and_dec` binding;
the all-leaders-AND fan-in; host/device-barrier bracketing.

**LOW / OPEN:** `m2s_val` vs `s2m_val` precise per-step meaning (counter vs expected
delta); whether all 4 `barrier_sema`/`target` slots per step are always populated or
sparse (set by the per-arch sem mapping at runtime).

---

## Cross-references

* [NEFF Device Barrier](neff-device-barrier.md) — the on-device 4-step execution
  counterpart of this config.
* [NCFW Ring Send/Wait + Config Schema + Host Command](ring-protocol-config-command.md)
  — the collective config schema and host command protocol that carries this struct.
* [pring (Persistent DMA Descriptor Ring)](pring-descriptors.md) — the TDR-ring DMA
  descriptors (`tdrbp`/`desc_count`) `prep_dma_descs_for_barrier` populates.
* [RDMA Cross-Die SBUF→SBUF P2P](../../dma/rdma-cross-die.md) — the EVT_SEM atomic
  semaphore windows (read `0x1000`, inc `0x1800`) underlying signal/wait.
* [All-Reduce Lowering](../ops/all-reduce.md) — the `__select_algorithms @0x14c620`
  executor spine that drives the collective around these barriers.
* [TOP_SP Lowering](../ops/top-sp-lowering.md) — the host TOP_SP trigger
  (`kaena_khal` vtable `+0x708` write, doorbell `LOCAL_REG+0x15a0 = 0x615a0`).
* [NCFW DRAM Images + ctx_log](ncfw-dram-ctx-log.md) — `neff_ctx.barrier_completed@+0x5c`,
  the firmware-side completion flag the host barrier mirrors.
