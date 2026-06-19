# ll Load / Unload Sequence Generators

The host-side generators that emit the **`0x1095` load/unload record** stream a
loadable-library handle (`nrtucode_ll_t`) hands off to the on-device external-lib
loader. Two public exports — `nrtucode_ll_get_load_sequence` and
`nrtucode_ll_get_unload_sequence` — are **thin shims into a single shared body**,
`nrtucode_ll_get_sequence_common`. The shim injects a hidden *direction* immediate
(`1` = load, `2` = unload); the body builds a fixed array of **64-byte (`0x40`)
records**, each stamped with the 16-bit magic **`0x1095`**, then `memcpy`s them into
the caller's instruction buffer.

> **Scope.** This page documents the host-side **producer**. The device-side
> **consumer** — `load_external_libraries_impl`, which parses each `0x1095` record's
> direction/selector/device_addr — is
> [`../firmware/pool/external-lib-loader.md`](../firmware/pool/external-lib-loader.md)
> (committed, FW-16). The two pages reconcile field-for-field; see §8.

**Binary.** `libnrtucode_internal.so` — `0.21.2.0` customop build, 10,276,288 B,
BuildID `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd`, **not stripped**. Every address,
offset and constant below is read directly from this binary.

| Quantity | Value | Anchor | Tag |
|---|---|---|---|
| `.rodata` VMA−fileoff Δ | `0` (`readelf`: VMA `0x46b0` / off `0x46b0`) | section table | OBSERVED·HIGH |
| `.text` VMA−fileoff Δ | `0x1000` (VMA `0x9b01a0` / off `0x9af1a0`) | section table | OBSERVED·HIGH |
| C++ `_ZTV` vtables | **0** (`nm \| rg -c '_ZTV'` → 0) | symbol table | OBSERVED·HIGH |

> **GOTCHA — no C++ vtables in this binary.** `libnrtucode_internal.so` is plain C:
> zero `_ZTV` symbols. The one indirect call in the body (`call *0x20(%rax)`,
> §4 step 6) is a **struct field pointer** (a memhandle's `device_addr` slot), not a
> C++ vtable dispatch. Do **not** apply the `_ZTV + 0x10` slot rule here; function-ptr
> tables in this binary are plain C arrays (slot N = `base + 8*N`, first at `+0x00`).

---

## 1. Symbol / address table `[HIGH·OBSERVED]`

`nm libnrtucode_internal.so` — all six symbols verified at the exact addresses below:

| internal addr | size | symbol | role |
|---|---|---|---|
| `0x9b1fb0` | `0x10` | `nrtucode_ll_get_load_sequence` | **load shim** — injects `edx=1`, jumps to common |
| `0x9b2440` | `0x10` | `nrtucode_ll_get_unload_sequence` | **unload shim** — injects `edx=2`, jumps to common |
| `0x9b1fc0` | `0x479` | `nrtucode_ll_get_sequence_common` | **shared body** — builds the `0x1095` records |
| `0x9b1e30` | `0xb5` | `nrtucode_ll_get_load_sequence_num_instrs` | load buffer-sizing query |
| `0x9b1ef0` | `0xb5` | `nrtucode_ll_get_unload_sequence_num_instrs` | unload buffer-sizing query |
| `0x9b24a0` | `0x05` | `nrtucode_ll_get_library_size` | sibling size accessor (`mov 0x18(rdi),rax; ret`) |

The body is `t` (local) — both public shims `T` (global) tail-call into it. The body
is the **sole producer** of the `0x1095` record: `objdump -d \| rg -c '0x1095'` = **8**
across the whole binary, and all 8 are `movw $0x1095` stores inside this one function
(addresses `0x9b20f7`, `0x9b2137`, `0x9b216b`, `0x9b21c0`, `0x9b2215`, `0x9b226a`,
`0x9b22bf`, `0x9b2314` — the record-0 builder plus the 7-fold unroll). `[OBSERVED·HIGH]`

---

## 2. The two shims — byte-exact `[HIGH·OBSERVED]`

Both shims are 16 bytes; they reshuffle SysV registers and inject the direction
immediate. The **only semantic difference is the immediate** (`01` vs `02`).

```
; nrtucode_ll_get_load_sequence  @ 0x9b1fb0
9b1fb0: 4d 89 c1           mov  %r8,%r9        ; arg5 num_instrs_pushed -> r9
9b1fb3: 49 89 d0           mov  %rdx,%r8       ; arg3 capacity          -> r8
9b1fb6: ba 01 00 00 00     mov  $0x1,%edx      ; *** DIRECTION = 1 (LOAD)  ***
9b1fbb: eb 03              jmp  9b1fc0         ; -> get_sequence_common (short jmp)

; nrtucode_ll_get_unload_sequence @ 0x9b2440
9b2440: 4d 89 c1           mov  %r8,%r9        ; arg5 num_instrs_pushed -> r9
9b2443: 49 89 d0           mov  %rdx,%r8       ; arg3 capacity          -> r8
9b2446: ba 02 00 00 00     mov  $0x2,%edx      ; *** DIRECTION = 2 (UNLOAD) ***
9b244b: e9 70 fb ff ff     jmp  9b1fc0         ; -> get_sequence_common (near jmp)
```

> **NOTE.** The `eb` (short) vs `e9` (near) `jmp` opcodes differ only because the load
> shim sits adjacent to the body while the unload shim sits far from it — a layout
> artifact, not a behavioral difference.

After the reshuffle, the public signature inside the body is:

```c
nrtucode_result_t nrtucode_ll_get_load_sequence(   // and ..._unload_sequence
    nrtucode_loadable_library_t *ll,    // rdi ; NULL => abort "ll"
    nrtucode_core_t             *core,  // rsi ; NULL => abort "core"
    size_t   instr_buf_max_instrs,      // rdx -> r8 ; buffer CAPACITY (record COUNT)
    char    *instr_buf,                 // rcx ; NULL => abort "instr_buf"; 8-byte aligned
    size_t  *num_instrs_pushed);        // r8  -> r9 ; NULL => abort; out = count written
//  edx (hidden direction) injected by the shim: 1=load, 2=unload.
```

`instr_buf_max_instrs` is a **count of records**, not a byte length: the count
(1 or 8) is compared against it and the record stride is `0x40`, so one `0x1095`
record = one device "instruction". `[OBSERVED·HIGH]`

**Return codes** (`eax`): `0` = OK (records written, `*num_instrs_pushed = count`);
`7` = capacity `< count` (no write, `mov $0x7,%eax`@`0x9b2085`/`0x9b2099` before the
`cmp %r13,%rdi` test); `8` = `instr_buf` not 8-byte aligned (`test $0x7,%bl`@`0x9b1ff8`
fails → logs *"instruction buffer is not correctly aligned"*, `mov $0x8,%eax`@`0x9b2022`).
`[OBSERVED·HIGH]`

**NULL-arg contract is FATAL** (`fprintf "...`%s` is null" + abort`), one abort site per
arg, all naming the fn `nrtucode_ll_get_sequence_common` (`.rodata 0x4e0d`):

| arg | name string (`.rodata`, Δ=0) | abort site |
|---|---|---|
| `ll` | `0x4d41` `"ll"` | `0x9b238d` |
| `core` | `0x492f` `"core"` | `0x9b23b8` |
| `instr_buf` | `0x50a3` `"instr_buf"` | `0x9b23e3` |
| `num_instrs_pushed` | `0x51a6` `"num_instrs_pushed"` | `0x9b240e` |

All four strings dumped byte-exact at the file offsets above (`.rodata` Δ=0).
`[OBSERVED·HIGH]`

---

## 3. The `0x1095` record — 64-byte slot field map `[HIGH·OBSERVED]`

Each record is built field-by-field on a stack scratch buffer (one `mov`-immediate
or register store per field) then `memcpy`'d to `instr_buf`. Every store below is
decoded from the disassembly of the record-0 builder (`0x9b20f3..0x9b212d`); the
record's `rsi` base is the per-record slot pointer.

| off | sz | field | value / source | writer instruction (internal addr) | tag |
|---|---|---|---|---|---|
| `+0x00` | 2 | `magic` | const **`0x1095`** | `movw $0x1095,(%rsi)` `0x9b20f7` | OBSERVED·HIGH |
| `+0x02` | 8 | `_rsv0` | `0` | `movq $0x0,0x2(%rsi)` `0x9b20fc` | OBSERVED·HIGH |
| `+0x0a` | 2 | `_rsv1` | `0` | `movw $0x0,0xa(%rsi)` `0x9b2104` | OBSERVED·HIGH |
| `+0x0c` | 1 | `direction` | `r14b` = `1` load / `2` unload | `mov %r14b,0xc(%rsi)` `0x9b210a` | OBSERVED·HIGH (val) / INFERRED·HIGH (sense) |
| `+0x0d` | 1 | `lane_mask` | const **`0xff`** | `movb $0xff,0xd(%rsi)` `0x9b2126` | OBSERVED·HIGH (val) / INFERRED·MED (meaning) |
| `+0x0e` | 2 | `_rsv2` | `0` | `movw $0x0,0xe(%rsi)` `0x9b210e` | OBSERVED·HIGH |
| `+0x10` | 8 | `device_addr` | `rax` = staged-image SoC addr (`0` if not resolved) | `mov %rax,0x10(%rsi)` `0x9b2114` | OBSERVED·HIGH (write) / INFERRED·HIGH (role) |
| `+0x18` | 4 | `library_selector` | `edi` = `ll->library_selector` (low 32 of `ll[+0x10]`) | `mov %edi,0x18(%rsi)` `0x9b212a` | OBSERVED·HIGH (write) / INFERRED·HIGH (role) |
| `+0x1c` | 4 | `image_size` | `ecx` = `ll->prelinked_size` (`0` if not resolved) | `mov %ecx,0x1c(%rsi)` `0x9b2118` | OBSERVED·HIGH (write) / INFERRED·HIGH (role) |
| `+0x20` | 32 | `_rsv_tail` | `0` | `movaps %xmm0,0x20/0x30(%rsi)` `0x9b211e`/`0x9b2122` | OBSERVED·HIGH |

```c
struct nrtucode_ll_seq_record_t {   /* sizeof = 0x40 (64) ; identical for load/unload */
    uint16_t magic;             /* +0x00  always 0x1095                            */
    uint8_t  _rsv0[8];          /* +0x02  zero (one unaligned qword store)         */
    uint16_t _rsv1;             /* +0x0a  zero                                     */
    uint8_t  direction;         /* +0x0c  1=load, 2=unload  (the discriminator)    */
    uint8_t  lane_mask;         /* +0x0d  always 0xff (broadcast/sentinel)         */
    uint16_t _rsv2;             /* +0x0e  zero                                     */
    uint64_t device_addr;       /* +0x10  staged UCPL image SoC addr; 0 on unload  */
    uint32_t library_selector;  /* +0x18  EXTISA lib UID (0=base POOL, 3=CPTC)     */
    uint32_t image_size;        /* +0x1c  prelinked_size; 0 on unload              */
    uint8_t  _rsv_tail[0x20];   /* +0x20..+0x3f  zero (reserved)                   */
};
```

> **GOTCHA — this is the OBSERVED byte map, not a guessed source struct.** `+0x02` is
> written as one unaligned 8-byte store; a faithful reconstruction uses a packed or
> byte-array layout. The point is the **byte offsets and constants**, which a
> reimplementer can emit verbatim.

> **NOTE — `library_selector` width.** The `nrtucode_ll_t` declares this field as a
> 64-bit `ll[+0x10]` ([`nrtucode-ll-create.md`](nrtucode-ll-create.md) §1), but the
> generator reads only the **low 32 bits** (`mov 0x10(%r15),%edi`@`0x9b20f3`) and
> stores a `uint32_t` at record `+0x18`. The selector space is `{0, 3}`, so the
> truncation is harmless; the on-wire selector is a `u32`. `[OBSERVED·HIGH]`

**`lane_mask` / `0xff` (`+0x0d`)** — `[INFERRED·MED]`. A constant `0xff` immediately
after the direction byte; most plausibly an all-lanes / all-cores broadcast mask or an
unconditional-enable sentinel the device loader reads. The **value** is OBSERVED·HIGH;
the **meaning** is not provable in this host binary — the device consumer (FW-16)
would confirm.

The three live payload fields are read straight out of the `nrtucode_ll_t` built by
[`nrtucode_ll_create`](nrtucode-ll-create.md) (no prelink, alloc, or device write
happens here — the generator is a pure handoff descriptor):

| `ll` field (create) | → record field | meaning |
|---|---|---|
| `ll[+0x00]` `context` | (compared to `core` ctx) | decides count 1 vs 8 |
| `ll[+0x08]` `device_memhandle` | `+0x10 device_addr` | SoC addr of the staged `UCPL ` image |
| `ll[+0x10]` `library_selector` | `+0x18 library_selector` | which EXTISA lib this image realises |
| `ll[+0x18]` `prelinked_size` | `+0x1c image_size` | total staged bytes (≤ 64 KiB, capped at create) |

---

## 4. The shared generation algorithm `[HIGH·OBSERVED]`

Walking the body (`0x9b1fc0`) with `r14b` = the injected direction:

1. **NULL-guard** `ll`/`core`/`instr_buf`/`num_instrs_pushed` (abort each, §2).
   `0x9b1fd1`..`0x9b1ff2`.
2. **Alignment guard** `test $0x7,%bl` on `instr_buf`@`0x9b1ff8`; nonzero → log
   *"...instruction buffer is not correctly aligned"* (`.rodata 0x50e3`, sev 1),
   `mov $0x8,%eax`@`0x9b2022`, return.
3. `r12 = ll->context` (`ll[+0x00]`)@`0x9b2039`;
   `rax = nrtucode_core_get_context(core)`@`0x9b203f`; cache `core_ctx` at `[rbp-0x38]`.
4. **CONTEXT MATCH decides the count** — `cmp %rax,%r12`@`0x9b2048`:
   - `ll->context == core_ctx` → **count = 1** (`mov $0x1,%edi`@`0x9b2094`) — the normal path;
   - else → **count = 8** (`mov $0x8,%edi`@`0x9b2080`) — fallback. The mismatch branch
     **also** logs the assert *"check `ll->context == nrtucode_core_get_context(core)`
     failed"* (`.rodata 0x4e68` fmt + `0x475c` expr, sev 1) — but does **not** abort and
     does **not** early-return; it continues and builds 8 records.
5. **Capacity check** — `mov $0x7,%eax` preset, then `cmp %r13,%rdi` (`r13`=capacity,
   `rdi`=count). Capacity `<` count → return `7`, **no write**.
   `0x9b208a` (count 8) / `0x9b209e` (count 1).
6. **`device_addr` / `image_size` resolution gate** — **only on the load path with a
   staged image**:
   ```
   9b20ac: cmp $0x1,%r14b              ; direction == 1 (load)?
   9b20b0: jne 9b20dc                  ; not load -> 0/0
   9b20b2: cmpq $0x0,0x18(%r15)        ; ll->prelinked_size != 0 ?
   9b20b7: je  9b20dc                  ; not staged -> 0/0
   9b20b9: mov 0x8(%r12),%rax          ; ll->context -> ctx[+0x08] (memhandle vtbl)
   9b20be: mov 0x8(%r15),%rsi          ; ll->device_memhandle (ll[+0x08])
   9b20cc: call *0x20(%rax)            ; ctx->device_addr(ctx, memhandle) -> rax
   9b20d6: mov 0x18(%r15),%ecx         ; ecx = ll->prelinked_size (u32)
   9b20dc: xor %eax,%eax ; xor %ecx,%ecx  ; else device_addr=0, size=0
   ```
   So `device_addr`/`size` are populated **only on the same-context LOAD**; on UNLOAD
   (`r14b=2`) and on the cross-context fallback they are **always 0**.
7. **Stack alloca**: `shl $0x6,%edi` (count × `0x40`), `sub %rdi,%rsi`, `mov %rsi,%rsp`
   → scratch buffer. `0x9b20e0`..`0x9b20f0`.
8. **Build `count` identical records** (§3). `edi = ll->library_selector` is loaded
   **once**@`0x9b20f3` and reused for every record. Same-context emits 1 record and
   jumps straight to `memcpy` (`cmp -0x38(%rbp),%r12 ; je 0x9b2369`@`0x9b212d`);
   cross-context falls through and builds all 8 (unroll at `+0x40`@`0x9b2137`,
   `+0x80`@`0x9b216b`, `+0xc0`@`0x9b21c0`, `+0x100`@`0x9b2215`, `+0x140`@`0x9b226a`,
   `+0x180`@`0x9b22bf`, `+0x1c0`@`0x9b2314`). All `count` records are **byte-identical**.
9. **`memcpy(instr_buf, scratch, count*0x40)`**@`0x9b2369`.
10. **`*num_instrs_pushed = count`**@`0x9b2375`; `xor %eax,%eax; ret`.

**Output summary:**

| path | count | record contents |
|---|---|---|
| same-ctx **LOAD** | 1 | `{magic, dir=1, 0xff, device_addr, selector, image_size}` (full) |
| same-ctx **UNLOAD** | 1 | `{magic, dir=2, 0xff, 0, selector, 0}` |
| cross-ctx (either dir) | 8 | 8 **identical** copies of the above + a logged ctx-mismatch warning |

> **NOTE — why 8?** `[INFERRED·MED]` The literal `mov $0x8,%edi` is OBSERVED·HIGH. With
> `device_addr` unresolved (mismatched context) the generator cannot stage to a single
> core, so it emits 8 copies — most plausibly one per device core/lane of the 8-core
> GPSIMD pool. The "one per core" reading is inferred from pool topology, not provable
> in this host binary; FW-16's `NUM_POOL_CORES` treats 8 as the pool count consistently.

---

## 5. The `num_instrs` sizing queries `[HIGH·OBSERVED]`

`nrtucode_ll_get_load_sequence_num_instrs` (`0x9b1e30`) and the unload twin
(`0x9b1ef0`) answer "how big a buffer do I need?". Each NULL-guards `ll`/`core`,
computes `core_ctx`, then:

- `ll->context == core_ctx` → **return `1`** (`mov $0x1,%eax`);
- else → log the assert and **return `8`** (`mov $0x8,%eax`) — an **error code**, not
  the fallback count.

> **QUIRK — generator/query asymmetry.** The `num_instrs` query treats cross-context as
> a **hard error** (rc 8), while the generator merely **warns** and emits 8 records. A
> well-behaved caller sizes its buffer from `num_instrs` (= 1) and only ever hits the
> 1-record path. **In correct usage, the load/unload sequence is EXACTLY ONE 64-byte
> record.** The 8-record path is a defensive fallback for the API-misuse of passing a
> core in the wrong context. `[OBSERVED·HIGH]`

> **NOTE — the warning names its own sibling.** Inside the body, the ctx-mismatch
> assert picks the function-name token by a direction-keyed `cmove`:
> ```
> 9b204d: cmp $0x1,%r14b            ; ZF set iff direction == 1 (load)
> 9b2051: lea ...0x4de4,%rax        ; "nrtucode_ll_get_load_sequence_num_instrs"
> 9b2058: lea ...0x5078,%r8         ; "nrtucode_ll_get_unload_sequence_num_instrs"
> 9b205f: cmove %rax,%r8            ; dir==1 -> load name; else keep unload name
> ```
> So the load path's warning names `0x4de4`, the unload path's names `0x5078`. Both
> strings verified byte-exact in `.rodata`. `[OBSERVED·HIGH]`

---

## 6. The load vs unload distinction `[HIGH·OBSERVED]`

LOAD and UNLOAD are **one function** differing only by the direction immediate the shim
injects. The inverse-map is therefore a **field/dataflow map**, not an 8-row
distinct-opcode map (the "8 records" are homogeneous copies, not 8 teardown steps):

| # | aspect | LOAD (`edx=1`) | UNLOAD (`edx=2`) | note |
|---|---|---|---|---|
| 1 | entry shim | `0x9b1fb0` `mov edx,1` | `0x9b2440` `mov edx,2` | only the dir imm differs |
| 2 | magic `+0x00` | `0x1095` | `0x1095` | same container magic |
| 3 | direction `+0x0c` | `1` | `2` | the discriminator |
| 4 | lane_mask `+0x0d` | `0xff` | `0xff` | unchanged |
| 5 | device_addr `+0x10` | staged SoC addr (same-ctx) | **`0` always** (gate skips) | LOAD resolves, UNLOAD skips |
| 6 | library_selector `+0x18` | `ll->library_selector` | `ll->library_selector` | names the lib both ways |
| 7 | image_size `+0x1c` | `prelinked_size` (same-ctx) | **`0` always** (gate skips) | LOAD resolves, UNLOAD skips |
| 8 | count same-ctx / cross-ctx | 1 / 8 | 1 / 8 | mirror |
| 9 | num_instrs twin | `0x9b1e30`, names `0x4de4` | `0x9b1ef0`, names `0x5078` | each names its own fn |

On UNLOAD the resolution block `0x9b20b2..0x9b20da` — **including** the memhandle
`call *0x20(%rax)` — is **dead code** (the `cmp $0x1,%r14b ; jne` gate skips it). So an
unload record carries, in practice, **exactly two live values**: `direction = 2` and
`library_selector`. The device loader must locate the library's resident footprint
from its own state (it loaded the lib; the host gives it no address to unmap). The
shipped public header documents this role verbatim: the unload sequence is "a sequence
of 1+ instructions required to unload instruction implementation kernels … inserted
into the instruction stream at end of model execution or before a new library is
loaded." `[OBSERVED·HIGH]`

---

## 7. The 8-record unload path and teardown ordering `[HIGH·OBSERVED]`

The task's "8-record path" is the **cross-context fallback** described in §4 step 4 —
not a distinct teardown-opcode set. Reading each `movw $0x1095` site confirms records
1..7 are unrolled byte-copies at `+0x40` increments (e.g. record-1:
`movw $0x1095,0x40(%rsi)`@`0x9b2137`, `mov %r14b,0x4c(%rsi)`@`0x9b214b` =
direction at `+0x4c` = `+0x0c + 0x40`). All 8 stamps live within
`0x9b20f7..0x9b2314`. `[OBSERVED·HIGH]`

The unload generator **frees nothing** (host or device). Its complete call set on the
record path is `{nrtucode_core_get_context, nrtucode_context_log (warn/align),
memcpy}`; there is no `free`, no `device_free`, no objcount change. Host-object
teardown is [`nrtucode_ll_destroy`](nrtucode-ll-create.md)'s job (device_free of the
staging buffer + free of the host struct). The lifecycle:

| step | function | action |
|---|---|---|
| create | `nrtucode_ll_create` | malloc host `ll(0x48)`, prelink, `device_malloc` staging buf → `ll[+0x08]`, set selector/size |
| **load-seq** | `nrtucode_ll_get_load_sequence` | emit `0x1095` record(s), `dir=1`, device_addr+size live; device loader **maps** the image |
| **unload-seq** | `nrtucode_ll_get_unload_sequence` | emit `0x1095` record(s), `dir=2`, device_addr+size = `0`; device loader **tears down** the kernels |
| destroy | `nrtucode_ll_destroy` | `device_free` the staging buf, free host `ll`, objcount-- (no sequence) |

> **NOTE — ordering is a runtime contract.** Correct teardown is unload-sequence
> (device) **before** destroy (host free); the two are disjoint and neither enforces
> the order internally. `[INFERRED·HIGH]`

---

## 8. Reconciliation with the device loader (FW-16) `[HIGH·OBSERVED]`

The committed
[`../firmware/pool/external-lib-loader.md`](../firmware/pool/external-lib-loader.md)
documents `load_external_libraries_impl` — the device consumer of this exact record.
Its `0x1095` field table reconciles **field-for-field** with the host writers decoded
in §3:

| field | this page (host writer) | FW-16 (device reader) | agree? |
|---|---|---|---|
| `direction +0x0c` | `1` load / `2` unload | `1` / `2` | ✓ |
| `device_addr +0x10` | live (load) / `0` (unload) | live / **`0`** | ✓ |
| `library_selector +0x18` | `{0, 3}` | live `{0, 3}` | ✓ |
| `image_size +0x1c` | live (load) / `0` (unload) | live / **`0`** | ✓ |
| stride / magic | `0x40` / `0x1095` | `0x40` / `0x1095` | ✓ |
| count fallback | 8 (cross-ctx) | up-to-8, `NUM_POOL_CORES` | ✓ |

> **CORRECTION — none required.** The host (this page, OBSERVED producer) and the
> device (FW-16, marked `[CARRIED]` from the RT generators) agree on every record
> field, the `0x40` stride, the `0x1095` magic, the direction discriminator, and the
> 8-core fallback width. This page is the **byte-verified ground truth** that FW-16
> carries; no divergence to correct.

FW-16 independently flags the open item this page's §6 raises: on unload the device
receives **only** a `library_selector` (no `device_addr`/`image_size`), so it must map
selector → resident region from its own load-time state; if it cannot, the teardown
could no-op and leave the EXTISA region mapped — a **device-side** completeness
question (not a host leak; destroy frees the staging buffer regardless). That parse is
the FW-16/FW-17 boundary, out of this host binary's reach. `[MED·INFERRED]`

**Related pages.** The `nrtucode_ll_t` struct and create/destroy lifecycle:
[`nrtucode-ll-create.md`](nrtucode-ll-create.md). The kernel-info table the device
loader cross-references: [`../firmware/pool/kernel-info-table.md`](../firmware/pool/kernel-info-table.md).
The UCODE_LL upload descriptor / host-device handoff:
[`host-device-descriptor-handoff.md`](host-device-descriptor-handoff.md).

---

## 9. Adversarial self-verification `[OBSERVED]`

Each strongest claim re-challenged against `libnrtucode_internal.so`:

1. **Two generators = shims into one body.** `nm` → `nrtucode_ll_get_load_sequence`@`0x9b1fb0`
   and `nrtucode_ll_get_unload_sequence`@`0x9b2440`; both `jmp 9b1fc0`
   (`nrtucode_ll_get_sequence_common`). `objdump` shows `mov $0x1,%edx` (load) /
   `mov $0x2,%edx` (unload) before the jump. **Confirmed.**
2. **64-byte field map.** Every store decoded at the cited address: magic `0x1095`@`0x9b20f7`,
   direction `+0x0c`@`0x9b210a`, `0xff`@`0x9b2126`, device_addr `+0x10`@`0x9b2114`,
   selector `+0x18`@`0x9b212a`, size `+0x1c`@`0x9b2118`, tail `movaps`@`0x9b211e`/`0x9b2122`.
   Stride `shl $0x6` (= `0x40`)@`0x9b20a9`/`0x9b20ea`. **Confirmed.**
3. **Magic `0x1095`.** `objdump -d \| rg -c '0x1095'` = **8**; all 8 are `movw $0x1095`
   inside this function's address range. **Confirmed sole producer.**
4. **1-vs-8 expansion loop.** `cmp %rax,%r12`@`0x9b2048` → `je` to `mov $0x1,%edi`@`0x9b2094`
   (count 1) vs `mov $0x8,%edi`@`0x9b2080` (count 8); same-ctx skip `je 0x9b2369`@`0x9b2131`.
   **Confirmed.**
5. **8-record unload path.** Records 1..7 unrolled at `+0x40` increments;
   record-1 `movw $0x1095,0x40(%rsi)`@`0x9b2137`. UNLOAD device_addr/size gate skipped
   (`cmp $0x1,%r14b ; jne 0x9b20dc`@`0x9b20ac`). **Confirmed.**

All five hold against the binary. The single non-OBSERVED claims are the `0xff`
`lane_mask` *meaning* (value OBSERVED, meaning INFERRED·MED) and the "8 = per-core"
reading (INFERRED·MED) — both explicitly tagged and deferred to FW-16/FW-17.
