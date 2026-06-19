# Stack-Switch Dispatch

`switch_stack_or_call_wrapper` is the launch handoff that decides — at the moment a
custom-op kernel is about to run — whether to execute the kernel directly on the
tiny on-core Xtensa stack, or to first allocate a large stack in HBM scratch, swap
the stack pointer to it, run the kernel there, and free it on return. It is the
public entry point the per-customer generated launcher calls; it is *not* called
anywhere inside the shipped runtime archive itself.

This page decodes that handoff **instruction-by-instruction** from the two compiled
objects that implement it — `stack_switch.o` (the C++ TU) and `switch_stack.o`
(hand-written Xtensa assembly) — both shipped, unstripped, with DWARF v4, inside
`libneuroncustomop.a`. Every claim is anchored to a native-disassembly address, a
DWARF DIE offset, or an ELF relocation.

> **Provenance.** All facts below are read from the ELF32-Xtensa relocatable objects
> and their DWARF, disassembled with the shipped GNU Xtensa `xtensa-elf-objdump`
> under core config `ncore2gp`. No external or vendor source tree is referenced. The
> `.cpp` source path embedded in `DW_AT_name`
> (`.../SundaCustomOpLibrary/custom_op/library/stack_switch.cpp`) is the producer's
> own debug record, not a consulted artifact.

Related: [`customop_*` marshalling entries](./customop-marshalling.md) (the kernel
launcher these run *inside* — the actual `at::Tensor`/scalar arguments are pulled
there, **after** the stack choice is made, never across this switch);
[device memory allocators](./device-allocators.md) (`neuron_hbm_allocate` — the HBM
scratch heap the large stack comes from); [`build_custom_op.py`
codegen](./build-custom-op-codegen.md) (emits the per-customer launcher that calls
this entry and wires the kernel's return to `switchBack`). The Xtensa
stack-limit (KSL/ISL) exception model that the hardware uses to police the HBM
stack floor is a separate Part-13 page (not yet authored).

---

## 1. Artifacts, symbols, and how to reproduce the disassembly

Both objects are members of the static runtime archive:

```
extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/
  gpsimd/custom_op/neuron/libneuroncustomop.a
    └─ stack_switch.o   (C++14, XtensaTools-14.09 clang 10.0.1; full DWARF)
    └─ switch_stack.o   (hand-written asm; line/info DWARF only — no var DIEs)
```

Disassemble natively (the local `llvm-objdump` has no Xtensa backend; the shipped
GNU binutils objdump does, under the `ncore2gp` core):

```sh
ar x libneuroncustomop.a stack_switch.o switch_stack.o
export XTENSA_SYSTEM=.../gpsimd_tools/tools/ncore2gp/config
export XTENSA_CORE=ncore2gp
.../tools/XtensaTools/bin/xtensa-elf-objdump --xtensa-core=ncore2gp -dr stack_switch.o
```

It emits correct per-instruction Xtensa+FLIX disassembly (one harmless
`TIE checksum does not match` warning from config drift — it does not affect any of
the base/windowed-ISA or SR instructions decoded here). All addresses below are
section-relative within each object. *[HIGH / OBSERVED — disasm runs clean.]*

### 1.1 `stack_switch.o` symbol inventory

`nm -S` (`.text = 0x1f5`, `.literal = 0x4`, `.bss = 0x10`): *[HIGH / OBSERVED — nm]*

| addr | size | bind | symbol (demangled) |
|------|------|------|--------------------|
| `0x000` | `0x0b2` | `T` | `allocate_hbm_stack(uint32_t size, uint32_t entry_PC, uint32_t return_PC)` |
| `0x0b4` | `0x01d` | `T` | `deallocate_hbm_stack(uint64_t ptr)` |
| `0x0d4` | `0x121` | `T` | `switch_stack_or_call_wrapper(uint32_t wrapper_fn_ptr, uint32_t stack_size)` |

The four `.bss` globals, with `DW_AT_location DW_OP_addr` offsets confirmed against
the disassembly relocations: *[HIGH / OBSERVED — DWARF + nm + relocs]*

| `.bss` off | size | symbol | DWARF type | decl line |
|------------|------|--------|------------|-----------|
| `0x00` | 1 B | `switched_stack` | `bool` | `stack_switch.cpp:12` |
| `0x04` | 4 B | `old_stack` | `uint32_t[1]` | `:13` |
| `0x08` | 4 B | `new_stack` | `uint32_t[1]` | `:14` |
| `0x0c` | 4 B | `new_stack_base` | `uint32_t[1]` | `:15` |

Undefined imports (the callees this TU relies on): `neuron_hbm_allocate`,
`neuron_hbm_deallocate`, `neuron_translate`, `neuron_translate_ctx`,
`init_neuron_hbm_allocator`, `init_neuron_dataram_allocator`,
`_init_libc_heap_allocator`, `_init_translate_ctx`, `_use_default_malloc`, `memset`,
`abort`, and `saveContext` (the one symbol pulled from `switch_stack.o`).

### 1.2 `switch_stack.o` symbol inventory

`nm` (`.text = 0x110`, hand-written asm): *[HIGH / OBSERVED — nm]*

| addr | bind | symbol | role |
|------|------|--------|------|
| `0x000` | `T` | `saveContext` | save regs+SRs, spill windows, fall into `switchStack` |
| `0x058` | `t` | `switchStack` | install new SP/PS/ISL, `jx` to `entry_PC` |
| `0x081` | `T` | `switchBack` | rotate `WINDOWBASE`, restore SRs+regs, `jx` back |
| `0x09d` | `t` | `WBN_zero` … `change_WBP` | `WINDOWBASE`-arithmetic helpers used by `switchBack` |

Undefined: `new_stack`, `new_stack_base`, `old_stack` (the `stack_switch.o`
globals), and `xthal_window_spill_nw` (the Xtensa HAL "spill all windows" routine,
defined in the toolchain `libhal.a`).

> **NOTE — who references whom.** A whole-archive `nm` sweep finds **zero**
> undefined (`U`) references to `switch_stack_or_call_wrapper`, `switchBack`, or
> `saveContext` from any *other* member. `saveContext` is referenced only from
> within `switch_stack_or_call_wrapper` (reloc at `0x1c7`). `switch_stack_or_call_wrapper`
> is the public entry the **generated** launcher calls, and `switchBack` is the
> address the generated launcher seeds as the kernel's return target — neither is
> wired up inside the library. The generated launcher lives in
> [`build_custom_op.py` codegen](./build-custom-op-codegen.md), not in this
> package. *[HIGH / OBSERVED — nm over all members.]*

---

## 2. The header contract — and a binary-vs-header discrepancy

`stack_switch.hpp` (the shipped contract header) declares: *[HIGH / OBSERVED — header]*

```c++
#define MAX_STACK_SIZE 0x400000          // HBM stack max 4 MB, dynamically allocated
extern bool switched_stack;              // runtime flag: was the stack switched?
uint64_t allocate_hbm_stack(uint32_t entry_PC, uint32_t return_PC);   // 2-arg (!)
int      deallocate_hbm_stack(uint64_t ptr);
#ifndef STACK_SIZE
   #define STACK_SIZE 4196               // default conservative stack budget (4196 B)
#endif
static_assert(STACK_SIZE <= MAX_STACK_SIZE, "STACK_SIZE cannot be greater than 4MB.");
int switch_stack_or_call_wrapper(uint32_t wrapper_fn_ptr, uint32_t stack_size = STACK_SIZE);
```

`wrapper_api.h` types the kernel entry as `typedef int (*wrapper_fn)();` — so
`wrapper_fn_ptr` is a 32-bit code pointer to an `int()`. The header comment states
the design intent verbatim: the default `STACK_SIZE` is deliberately tiny "so no
switch will be attempted", and a customer who knows the kernel needs a deep stack
passes a large `STACK_SIZE` to **force** the switch.

> **CORRECTION — to SX-ABI-09 §2 wording (documentation lag confirmed at the byte
> level).** The compiled object's DWARF disagrees with the header on the private
> helper's arity. The mangled name is `_Z18allocate_hbm_stackjjj` =
> `allocate_hbm_stack(unsigned, unsigned, unsigned)` — **three** `uint32` args — and
> the DWARF names them, in order, `size`, `entry_PC`, `return_PC` (DIE `0x9b1`/`0x9bc`/`0x9c7`,
> `decl_line 19`). The header declares only two (`entry_PC`, `return_PC`). The
> object — emitted by the ABI's own compiler — is authoritative: the real signature
> is `allocate_hbm_stack(uint32_t size, uint32_t entry_PC, uint32_t return_PC) ->
> uint64_t`. `switch_stack_or_call_wrapper`'s 2-arg `(jj)` signature matches the
> header exactly (DIE `0xa1d`, params `wrapper_fn_ptr`/`stack_size`).
> *[HIGH / OBSERVED — mangled name + DWARF formal_parameter DIEs.]*

---

## 3. `switch_stack_or_call_wrapper` — the decision logic, byte-exact

DWARF: `low_pc 0xd4`, `high_pc 0x1f5`; `DW_AT_frame_base = DW_OP_reg1` (i.e. CFA =
`a1`/SP); returns `int`. Param locations: `wrapper_fn_ptr` and `stack_size` live in
location lists (incoming `a2`/`a3` per the windowed-call ABI). Named locals from
DWARF: `isl` (uint32, decl_line 70), `sp` (uint32, decl_line 70),
`remaining_stack_space` (uint32, decl_line 92), and `hbm_stack_addr` — a
**`volatile uint64_t`** at `DW_OP_fbreg +8` on the local frame (DIE `0xa86`,
`DW_AT_type → 0xb56 = DW_TAG_volatile_type`, decl_line 96). The `return_label` is a
`DW_TAG_label` at `low_pc 0x1ca` (DIE `0xa94`). *[HIGH / OBSERVED — DWARF.]*

### 3a. Prologue + runtime init (`0x0d4`–`0x10d`)

```asm
; --- DWARF lines from the decoded line table (addr → stack_switch.cpp:line) ---
 d4: entry  a1, 48                ; l52  windowed prologue, 48-byte frame
 d7: const16 a4, &_use_default_malloc      (R_XTENSA …  _use_default_malloc)   ; l54
 dd: movi   a5, 1
 e0: s8i    a5, a4, 0             ;       _use_default_malloc = 1  (force libc malloc
                                  ;       *during* allocator bring-up)
 e3: const16 a5, &init_neuron_hbm_allocator
 e9: callx8 a5                    ; l60  init_neuron_hbm_allocator()
 ec: const16 a5, &init_neuron_dataram_allocator
 f2: callx8 a5                    ; l61  init_neuron_dataram_allocator()
 f5: const16 a5, &_init_libc_heap_allocator
 fe: callx8 a5                    ; l62  _init_libc_heap_allocator()
101: movi   a5, 0
104: s8i    a5, a4, 0             ; l64  _use_default_malloc = 0  (switch to neuron heaps)
107: const16 a4, &_init_translate_ctx
10d: callx8 a4                    ; l68  _init_translate_ctx()
```

The wrapper's first act is to stand up the entire runtime — the HBM heap, the
dataram/TCM heap, the libc heap, and the translation context — bracketing the
allocator init with `_use_default_malloc = 1 … = 0` so the allocators can be
constructed using plain libc `malloc` before they take over `malloc` themselves.
*[HIGH / OBSERVED — disasm + line table.]*

### 3b. The decision (`0x110`–`0x12d`) — switch iff the live stack is too small

```asm
110: rsr.isl a4                   ; l73  a4 = ISL  (the stack-LIMIT special register)
113: mov.n   a6, a1               ; l80  a6 = sp   (current stack pointer)
115: addi.n  a11, a1, 8           ;       (spill-slot setup, &hbm_stack_addr lo)
117: { const16 a6, &switched_stack ;      FLIX bundle — VLIW read-before-write:
       sub.a   a4, a6, a4 }       ; l92    the `sub` reads the OLD a6 (= sp), THEN
                                  ;        const16 overwrites a6 with &switched_stack.
                                  ;        => a4 = sp - ISL = remaining_stack_space
122: saltu   a7, a4, a3           ; l93  a7 = (remaining_stack_space < stack_size) ? 1 : 0
12a: s8i     a7, a6, 0            ; l93  switched_stack = a7   (publish the global flag)
12d: { bgeu.w15 a4, a3, 0x1e5     ; l95  if remaining >= stack_size -> DIRECT CALL (0x1e5)
       movi    a6, 0 }            ;       else fall through to the SWITCH path
```

The whole decision is three arithmetic instructions and a branch:

```c
uint32_t isl       = read_special_register(ISL);   // hardware stack-limit register
uint32_t sp        = current_stack_pointer();      // a1
uint32_t remaining = sp - isl;                     // headroom to the HW stack floor
switched_stack     = (remaining < stack_size);     // global record of the path taken
if (remaining >= stack_size)
    goto direct_call;                              // 0x1e5 — run on the on-core stack
// else: allocate an HBM stack and switch
```

> **The ISL register is the entire trick.** `ISL` is the hardware stack-limit
> special register: a store below `ISL` raises an Xtensa stack-limit-violation
> exception. So `sp - ISL` is *exactly* "bytes of headroom to the hardware-enforced
> floor of the current stack", and the switch is taken only when the kernel's
> declared `stack_size` budget would not fit underneath it. Because the default
> `STACK_SIZE = 4196` is deliberately tiny, the common case is `remaining >=
> stack_size` and **no switch** happens. *[HIGH / OBSERVED — `rsr.isl`/`sub`/`saltu`/`bgeu`.]*

> **GOTCHA — FLIX read-before-write.** The `sub.a a4, a6, a4` at `0x117` shares its
> FLIX bundle with `const16 a6, &switched_stack`. Within one VLIW bundle the `sub`
> reads the *incoming* `a6` (still `= sp` from `0x113`) before `const16`'s write of
> `a6` is committed. Reading the bundle as if `a6` were already `&switched_stack`
> would mis-derive `remaining`. The line table attributes `0x110 rsr.isl` to
> `cpp:73`, `0x113 mov.n` to `cpp:80`, the subtract to `cpp:92`, and the `saltu`/`s8i`
> to `cpp:93` — minor source-line attribution drift versus the `decl_line 70`
> the variable DIEs carry; the instructions and addresses are exact. *[HIGH / OBSERVED.]*

---

## 4. The HBM stack — allocation, translation, layout, limit install

`allocate_hbm_stack` is `DW_INL_inlined` into `switch_stack_or_call_wrapper` at
`call_line 96` (inlined-subroutine DIE `0xa9f`, abstract-origin `0x9a1`). Its body is
emitted at `0x135`–`0x1b2` (identical to the standalone copy at `0x000`–`0x0b2`).
*[HIGH / OBSERVED — DWARF inlined_subroutine + disasm.]*

### Step 1 — allocate in HBM scratch (`0x13b`–`0x144`)

```asm
13b: s32i  a5, a1, 12             ; hbm_stack_addr hi  := 0   (clear the u64 spill slot)
13e: s32i  a5, a1, 8              ; hbm_stack_addr lo  := 0
141: callx8 neuron_hbm_allocate   ; neuron_hbm_allocate(stack_size, &hbm_stack_addr, 0)
144: { bnez.w15 a10, 0x1b4        ; if ret != 0 -> SKIP layout (leaves hbm_stack_addr == 0)
       movi a7, -16 }             ;   (-16 = ~0xF mask, pre-staged for the alignment below)
```

`neuron_hbm_allocate`'s prototype (`custom_op.h`) is `int neuron_hbm_allocate(size_t
nbytes, uint64_t *ptr, size_t alignment = 0)`, and the header states "If `alignment`
not specified or zero, uses [alig]nment of 64". The call passes `alignment = 0`, so
the HBM block is **64-byte aligned**. `nbytes = stack_size` (the same budget the
decision compared against), capped by the header `static_assert` at
`MAX_STACK_SIZE = 0x400000 = 4 MB`. The returned 64-bit SoC/HBM address is written
to the volatile `hbm_stack_addr`. The backing arena is the HBM **scratch** heap that
`init_neuron_hbm_allocator` stood up in §3a — a distinct heap from the dataram/TCM
heap. See [device memory allocators](./device-allocators.md). *[HIGH / OBSERVED —
disasm + header.]*

### Step 2 — translate the 64-bit HBM address to a 32-bit NX SP (`0x14c`–`0x168`)

```asm
14c: callx8 neuron_translate_ctx               ; fetch the translation context
158: { const16 a4,&neuron_translate; l32i a13,a1,12 }   ; a13 = hbm_addr hi
160: { const16 a4,&neuron_translate; l32i a12,a1,8  }   ; a12 = hbm_addr lo
168: callx8 neuron_translate                   ; q7_stack_addr = neuron_translate(ctx, hbm_addr)
```

`neuron_translate` maps the 64-bit HBM address into a 32-bit NX-local address that
the Xtensa `SP` register can actually hold and dereference (`a1` is a 32-bit
register; `addr_size = 4`). Because the translation window spans far more than the
≤ 4 MB stack, one translate covers the whole stack's lifetime — there is no
per-frame re-translation. *[HIGH / OBSERVED — disasm; window-coverage is INFERRED
from the ≤ 4 MB cap.]*

### Step 3 — compute the new SP and the new stack limit (`0x16b`–`0x18e`)

The stack grows **down**. `q7_stack_addr` is the *low* (base) end; `q7_stack_addr +
size` is the *high* (top) end. The top is 16-byte aligned, then 128 bytes are
reserved at the top for the saved-context block; that becomes the initial `SP`.

```asm
16b: { add.a a3, a10, a3 ; mov.a a4, a10 }     ; a3 = base + size = q7_TOP ; a4 = q7_BASE
17b: { and a5, a3, a7 ; movi a7,128 ; movi a12,128 }  ; a5 = q7_TOP & ~0xF  (16B-aligned top)
18b: addi  a3, a5, -128                        ; a3 = aligned_top - 128 = context-block base
                                               ;      (= the SP that switchStack installs)
```

The resulting NX-address-space layout (grows downward): *[HIGH / OBSERVED — disasm.]*

```
 high addr ─┐
            │   aligned_top = (q7_BASE + size) & ~0xF
            │     ├─ top 128 bytes: the saved-context block (built in Step 4)
new_stack ──┤     └─ aligned_top - 128   ← initial SP value
            │
            │   usable stack (size bytes), spills grow downward into it
            │
new_stack_base = q7_BASE  ← LOW addr = stack LIMIT (installed into ISL)
 low addr  ─┘
```

- Alignment: HBM block **64 B** (allocator default); SP top **16 B** (the `& ~0xF`).
- `new_stack = aligned_top - 128` is the initial `SP`. `new_stack_base = q7_BASE` is
  the low-address limit that `switchStack` writes into `ISL`. *[HIGH / OBSERVED.]*

### Step 4 — build the saved-context block at the top, publish the globals (`0x191`–`0x1b2`)

```asm
191: callx8 memset                ; memset(context_block_base, 0, 128)
                                  ;   — zero ONLY the 128-byte context block, not the
                                  ;     whole stack
194: addi.a a14, a5, -96          ; a14 = aligned_top - 96 = context base + 32
197: l32r   a15, .literal         ; a15 = 0x1ca  (return_PC; the literal whose
                                  ;     R_XTENSA_32 value is .text+0x1ca)
19a: s32i.n a7, a14, 48           ; ctx[+48] (= base+80) = a7   (PS-slot image)
19f: s32i.n a15, a14, 0           ; ctx[+0]  (= base+32) = 0x1ca (resume PC = return_label)
1a1: s32i.n a2, a14, 52           ; ctx[+52] (= base+84) = a2 = wrapper_fn_ptr (= entry_PC)
1b0: s32i.n a3, a7, 0             ; new_stack      = aligned_top - 128   (the SP to install)
1b2: s32i.n a4, a14, 0            ; new_stack_base = q7_BASE             (the ISL limit value)
```

The 128-byte context block at the top of the new stack therefore carries exactly the
three things `switchStack` needs to enter the kernel: the **resume PC** (`0x1ca`,
back in this function), the **PS image**, and the **entry PC** (`wrapper_fn_ptr`).
The `0x1ca` literal is confirmed two ways: the `.literal` section's single
`R_XTENSA_32 .text + 0` reloc with the in-place value `0x000001ca`, and the DWARF
`return_label` at `low_pc 0x1ca`. *[HIGH / OBSERVED — disasm + reloc + DWARF.]*

### The saved-context block format

`saveContext` (next section) writes the full block for the **old** stack; the offsets
it uses define the struct. Base = `SP - 128`: *[HIGH / OBSERVED — `switch_stack.o` stores.]*

| off | field | off | field | off | field |
|-----|-------|-----|-------|-----|-------|
| `+0`  | `a0` | `+16` | `a4` | `+64` | `lbeg` |
| `+4`  | `a1` (SP) | `+20` | `a5` | `+68` | `lend` |
| `+8`  | `a2` | `+24` | `a6` | `+72` | `lcount` |
| `+12` | `epc` | `+28` | `a7` | `+76` | `sar` |
|       |      |       |      | `+80` | `ps` |
|       |      |       |      | `+84` | `prefctl` |
|       |      |       |      | `+88` | `isl` |

> **NOTE.** The inlined builder in Step 4 *reuses* offsets `+80`/`+84` of the new
> stack's block to hold the new frame's PS image and the entry target, and stores the
> resume PC at `+32` (relative to context base, i.e. `a14+0`). `switchStack` reads
> `ps@+80`, `entry@+84`, `resume@+32` — fully consistent with the builder's stores.
> *[HIGH / OBSERVED.]*

---

## 5. The call, return, and restore — across the switch

### 5a. Direct-call path — no switch (`0x1e5`)

When `remaining >= stack_size`, the `bgeu.w15` at `0x12d` jumps straight here:

```asm
1e5: callx8 a2                    ; a2 = wrapper_fn_ptr — call the kernel launcher on the
                                  ;      CURRENT on-core stack (a plain windowed call)
1e8: movi  a2, 0
1ea: retw.n                       ; return 0
```

The kernel's `int` return value is discarded — `switch_stack_or_call_wrapper`
always returns 0 on success. The kernel's real output is delivered via the
marshalling layer ([`customop_*`](./customop-marshalling.md)), not through this int.
This is the common path, by design (tiny default `STACK_SIZE`). *[HIGH / OBSERVED.]*

### 5b. Allocation-failure guard (`0x1b4`–`0x1c5`)

After the inlined allocate, the wrapper reloads the **volatile** `hbm_stack_addr`
(the `memw` barriers at `0x1b4`/`0x1bb` are the `volatile uint64_t` load/store
fences) and aborts if it is null:

```asm
1b4: memw
1b7: s32i.n a6,a1,12 ; s32i.n a5,a1,8   ; store hbm_stack_addr (hi/lo)
1bb: memw
1be: l32i.n a2,a1,12 ; l32i.n a3,a1,8   ; reload hbm_stack_addr (lo/hi)
1c2: or    a2, a3, a2             ; a2 = (hi | lo)  — nonzero iff address != 0
1c5: beqz.n a2, 0x1ec             ; if hbm_stack_addr == 0 -> abort()  (0x1ec)
1c7: j     saveContext            ; else: take the switch
```

`0x1ec` is `const16 a2,&abort ; callx8 a2`. A nonzero return from
`neuron_hbm_allocate` (the `bnez.w15 → 0x1b4` skip at `0x144`) leaves
`hbm_stack_addr == 0` and routes here too. **There is no fallback to the small
stack** — allocation failure is a hard `abort()`. *[HIGH / OBSERVED.]*

### 5c. The switch — `saveContext` → `switchStack` (`switch_stack.o`)

`0x1c7: j saveContext`. `saveContext` (`switch_stack.o` `0x000`–`0x055`) saves the
old context into the block at `sp-128`, neutralizes the zero-overhead loop count,
spills all live register windows, and falls through into `switchStack`:

```asm
; saveContext — old-stack save + window spill
 00: wsr.epc a3 ; isync
 06: addi  a3, a1, -128           ; a3 = old context base = sp - 128
 09: s32i.n a0,a3,0  ; s32i.n a1,a3,4 ; s32i.n a2,a3,8     ; a0, a1(sp), a2
 0f: s32i.n a4..a7 -> a3,+16/+20/+24/+28
 17: rsr.epc a2 ; s32i a2,a3,12                            ; epc  -> +12
 1c: rsr.lbeg/lend/lcount/sar/ps/prefctl/isl -> +64/+68/+72/+76/+80/+84/+88
 46: movi  a2, 0 ; wsr.lcount a2 ; isync                  ; lcount := 0 (no stale loop)
 4f: const16 a0, &xthal_window_spill_nw
 55: callx0 a0                    ; *** SPILL ALL LIVE REGISTER WINDOWS to the OLD stack ***
                                  ;     callx0 returns to 0x58 = switchStack (intentional
                                  ;     fall-through)
```

```asm
; switchStack (0x58-0x7e) — the actual SP / PS / ISL install
 58: old_stack = a3              ; remember the OLD context base (a3 from saveContext)
 60: a1 = *new_stack             ; *** SP  <- the new HBM stack ***            (0x66)
 68: a4 = new_ctx[80] ; wsr.ps a4   ; install new PS                          (0x6b)
 6e: a4 = *new_stack_base ; wsr.isl a4 ; *** install new STACK LIMIT (ISL) *** (0x76)
 79: a8 = new_ctx[32]            ; a8 = resume PC (= 0x1ca)
 7b: a4 = new_ctx[84] ; jx a4    ; *** jump into wrapper_fn (entry_PC) ***
```

After `0x7e`, the kernel launcher runs with `SP` inside the HBM block and `ISL`
guarding the HBM floor. *[HIGH / OBSERVED — `switch_stack.o` disasm.]*

> **Window spill is mandatory before an SP swap.** This core is the windowed
> Xtensa ABI: every function starts `entry a1, N` and ends `retw.n`; the register
> file is backed by the stack and is spilled/filled lazily by window
> overflow/underflow exceptions. If `SP` is moved to a different memory region while
> live windows still have their backing store on the *old* stack, a later window
> underflow would read garbage. `xthal_window_spill_nw` flushes every live window to
> the old on-core stack *before* the swap, so the HBM stack starts with a clean
> window region. The kernel's deep call chains then spill **naturally into HBM** as
> it recurses — which is the entire point of giving it up to 4 MB. *[HIGH / OBSERVED
> on the pre-switch spill + SP swap; MED on the "natural HBM spill" intent.]*

### 5d. The return — `switchBack` (`switch_stack.o` `0x081`–`0x10d`)

The generated launcher seeds the kernel's return address to `switchBack` (that is
why `switchBack` is an exported `T` symbol that nothing in the library references).
On kernel return, `switchBack` first repairs `WINDOWBASE` — a plain `jx` (used by
`switchStack`) did not adjust `WINDOWBASE` the way an `entry`/`call` would, so the
windowed register file must be re-synchronized before any `retw` runs:

```asm
; switchBack — WINDOWBASE rotation (0x81-0xc2)
 81: rsr.wb a2                    ; read WINDOWBASE
 84: movi a3,15
 86: srli a4,a2,4 ; and a5,a3,a4  ; a5 = (WB >> 4) & 0xF
 8c: srli a4,a2,8 ; and a6,a3,a4  ; a6 = (WB >> 8) & 0xF
 92: bne a5,a6, rotate (0xa2)     ; field-compare driven nibble fix-up:
 95: beqz.n a6, WBN_zero (0x9d)   ;   +0x700 / -0x10 / +0x70 nudges to the WB fields,
 ...                              ;   then re-pack the nibble:
 b0: movi a3,112 ; and a4,a2,a3 ; srli a4,a4,4 ; srli a2,a2,4 ; slli a2,a2,4 ; or a2,a4,a2
 c2: wsr.wb a2                    ; write the repaired WINDOWBASE
```

```asm
; switchBack — restore SRs + regs from the OLD context, jump back (0xc5-0x10d)
 c5: a3 = *old_stack              ; old context base
 cd: restore lbeg/lend/lcount/sar/ps/prefctl/isl <- ctx +64/+68/+72/+76/+80/+84/+88
 f7: isync
 fa: restore a0,a1(SP),a2,a4-a7   <- ctx +0/+4/+8/+16/+20/+24/+28
108: a3 = ctx[12]                 ; (restore a3 last; +12 holds epc/a3 image)
10a: isync
10d: jx a8                        ; a8 = saved resume PC = 0x1ca
```

`switchBack` restores the **old** `SP` (from the `a1` image at ctx`+4`) and the
**old** `ISL` (ctx`+88`), so on resume the function is back on the on-core stack with
the original hardware stack limit. *[HIGH / OBSERVED.]*

### 5e. Resume + free (`0x1ca`–`0x1f2`)

Control lands back at `return_label = 0x1ca`, on the old stack:

```asm
1ca: memw                                          ; l104  re-read volatile hbm_stack_addr
1cd: const16 a2, &neuron_hbm_deallocate
1d0: l32i a11,a1,12 ; l32i.n a10,a1,8 ; callx8 a2   ; deallocate_hbm_stack(hbm_stack_addr)
1dd: bnez.n a10, 0x1ec                              ; if dealloc ret != 0 -> abort()
1df: movi a2,0 ; retw.n                             ; l112  return 0
```

`deallocate_hbm_stack` is inlined here (inlined-subroutine DIE `0xaf1`, `call_line
104`); it forwards to `neuron_hbm_deallocate(hbm_stack_addr)` and aborts on a nonzero
return. The HBM stack is freed on every exit of the switch path — there is **no RAII
guard object**; this is a hand-coded save / switch / run / restore / free sequence.
*[HIGH / OBSERVED.]*

---

## 6. Arguments do not cross the switch

`switch_stack_or_call_wrapper` transfers exactly **two** things into the new stack:
the entry point (`wrapper_fn_ptr` → `entry_PC`, stored at ctx`+84`) and the resume
point (`return_label = 0x1ca`, stored at ctx`+32`). The kernel's actual
`at::Tensor`/scalar arguments are *not* marshalled here. They are pulled by the
[`customop_next_*` marshalling](./customop-marshalling.md) **inside** `wrapper_fn`,
after it is already running on whichever stack was chosen — out of the `ArgParser`
singleton in `.bss` and the TPB instruction stream, both reachable identically from
either stack. So the "argument pass" across the switch is just a function pointer
plus a clean stack; everything else is global / TPB state. *[HIGH / OBSERVED for the
two-value handoff; INFERRED for the marshalling-side arg pull, cross-referenced.]*

---

## 7. Overflow protection = the hardware ISL stack limit

There is **no software bounds check** inside the running kernel. Overflow protection
is delegated to the hardware `ISL` stack-limit register, which `switchStack`
installs to `new_stack_base` (the LOW end of the HBM stack) at `0x76` before
entering the kernel. If the kernel's `SP` descends past `new_stack_base`, the Xtensa
core raises a stack-limit-violation exception — the same KSL/ISL stack-limit fault
machinery the firmware error handler fields. This is exactly why a deep kernel
*needs* the switch: on the tiny on-core stack, `ISL` sits just below the small
frame, and the kernel would trip the stack-limit fault almost immediately; on the
HBM stack, `ISL` is moved down to the floor of a multi-megabyte arena, so the deep
call chain fits and the fault only fires on genuine exhaustion of the 4 MB budget.

`saveContext`/`switchBack` save and restore the old `ISL` (ctx`+88`) around the
switch, so the on-core limit is intact again on resume. The KSL/ISL exception model
itself — the special registers and the handler that delivers the fault — is the
Xtensa exception-model page (Part 13, not yet authored); here we only observe that
the library *installs* the limit and the hardware *enforces* it. *[HIGH / OBSERVED
that `switchStack` writes `ISL = new_stack_base` and that the old `ISL` is
saved/restored; MED that the resulting fault is the specific KSL/ISL exception,
grounded in the SR being the same `isl` register.]*

---

## 8. End-to-end model (C pseudocode, real symbols/addresses)

```c
// stack_switch.cpp  (switch_stack_or_call_wrapper @ 0xd4-0x1f5)
int switch_stack_or_call_wrapper(uint32_t wrapper_fn_ptr,        // a2 = entry_PC
                                 uint32_t stack_size /*=4196*/) { // a3
    // --- 0xd4-0x10d : bring up the runtime --------------------------------
    _use_default_malloc = 1;                  // use libc malloc during init
    init_neuron_hbm_allocator();              // HBM scratch heap
    init_neuron_dataram_allocator();          // dataram/TCM heap
    _init_libc_heap_allocator();
    _use_default_malloc = 0;                  // neuron allocators now own malloc
    _init_translate_ctx();

    // --- 0x110-0x12d : the decision ---------------------------------------
    uint32_t isl       = RSR(ISL);            // hardware stack-limit register
    uint32_t sp        = REG(a1);
    uint32_t remaining = sp - isl;            // FLIX read-before-write @0x117
    switched_stack     = (remaining < stack_size);
    if (remaining >= stack_size)              // bgeu.w15 -> 0x1e5
        goto direct_call;

    // --- 0x135-0x1b2 : allocate + lay out the HBM stack (inlined) ---------
    volatile uint64_t hbm_stack_addr = 0;
    if (neuron_hbm_allocate(stack_size, (uint64_t*)&hbm_stack_addr, /*align*/0) != 0)
        goto check_alloc;                     // bnez.w15 -> 0x1b4 (addr stays 0)

    void*    ctx          = neuron_translate_ctx();
    uint32_t q7_base      = neuron_translate(ctx, hbm_stack_addr);   // 64b -> 32b NX
    uint32_t aligned_top  = (q7_base + stack_size) & ~0xFu;          // 16B-aligned top
    uint32_t ctx_base     = aligned_top - 128;                       // = new SP
    memset((void*)ctx_base, 0, 128);                                 // zero only the ctx
    *(uint32_t*)(ctx_base + 32) = 0x1ca;          // resume PC  (return_label)
    *(uint32_t*)(ctx_base + 80) = /*PS image*/;   // new-frame PS
    *(uint32_t*)(ctx_base + 84) = wrapper_fn_ptr;  // entry_PC
    new_stack      = ctx_base;                     // SP switchStack installs
    new_stack_base = q7_base;                       // ISL limit (HBM floor)

check_alloc:                                  // 0x1b4-0x1c7
    if (hbm_stack_addr == 0) abort();         // beqz.n -> 0x1ec
    // saveContext -> switchStack : save OLD ctx+regs, spill windows,
    //   SP <- new_stack, ISL <- new_stack_base, jx wrapper_fn (entry_PC).
    saveContext();                            // j saveContext @0x1c7
    // ... wrapper_fn runs on the HBM stack ...
    // ... wrapper_fn returns into switchBack: rotate WINDOWBASE,
    //     restore OLD SP/SRs/ISL, jx 0x1ca ...

return_label:                                 // 0x1ca (resumed on the OLD stack)
    if (neuron_hbm_deallocate(hbm_stack_addr) != 0) abort();   // 0x1dd
    return 0;                                                   // 0x1df

direct_call:                                  // 0x1e5
    ((wrapper_fn)wrapper_fn_ptr)();           // run on the on-core stack (ret discarded)
    return 0;                                                   // 0x1e8
}
```

---

## 9. Confidence ledger

**HIGH / OBSERVED** (native Xtensa disasm + DWARF + ELF symtab/relocs/CFI + header):

- All three `stack_switch.o` functions and four `.bss` globals — addresses, sizes,
  signatures. `switch_stack_or_call_wrapper(uint32, uint32) -> int` matches the
  header; `allocate_hbm_stack` is really 3-arg `(size, entry_PC, return_PC)` per
  DWARF, contradicting the 2-arg header.
- The decision `remaining = SP - ISL; switch iff remaining < stack_size;
  switched_stack records it` — decoded instruction-by-instruction
  (`rsr.isl`/`sub`/`saltu`/`s8i`/`bgeu.w15`), FLIX read-before-write reconciled.
- HBM allocation via `neuron_hbm_allocate` (HBM scratch heap, default 64 B align),
  translate to a 32-bit NX SP, 16 B-aligned top, 128 B context block,
  `new_stack = SP` / `new_stack_base = low limit`; `memset` zeroes only the 128 B
  block; `hbm_stack_addr` is `volatile uint64_t` (`memw` barriers + DWARF
  `DW_TAG_volatile_type`).
- The saved-context block format (`a0`–`a7`, `epc`, `lbeg`/`lend`/`lcount`/`sar`/
  `ps`/`prefctl`/`isl`) from `saveContext`; `switchStack` installs SP/PS/ISL and
  `jx`'s `entry_PC`; `switchBack` rotates `WINDOWBASE`, restores SRs+regs, and
  `jx`'s back; `return_PC = 0x1ca` confirmed via the `.literal` `R_XTENSA_32`
  `.text + 0x1ca` value and the DWARF `return_label`.
- `xthal_window_spill_nw` window spill *before* the SP swap.
- Guards: `alloc == 0` / `dealloc != 0` → `abort()`; `ISL` installed as the HBM
  stack limit.
- `switch_stack_or_call_wrapper` is never called inside the archive; `switchBack` is
  exported but unreferenced (it is the kernel return target wired by the generated
  launcher).

**HIGH / SOURCE** (header contract; behaviour independently observed in the object):
`MAX_STACK_SIZE = 0x400000`, default `STACK_SIZE = 4196`, the `static_assert`, and
the "default tiny so the switch is rarely taken" design intent; `wrapper_fn =
int(*)()` with the `int` return discarded.

**MED:** the 3-arg-vs-2-arg `allocate_hbm_stack` header discrepancy is documentation
lag (object authoritative); "kernel overflow on the HBM stack raises the KSL/ISL
stack-limit exception" (grounded in `switchStack` writing `ISL = new_stack_base`, but
the actual fault delivery lives in firmware, not this object); "windows spill
naturally into HBM as the kernel recurses" is the design intent — observed here is
only the pre-switch spill plus the SP swap.

**Gap:** the customer-side generated launcher (which calls
`switch_stack_or_call_wrapper` and wires `wrapper_fn`'s return to `switchBack`) is
emitted at custom-op build time and is not in this package; its existence and
contract are inferred from the header and the unreferenced-`switchBack` /
never-called-`switch_stack_or_call_wrapper` facts. Everything *inside* the shipped
runtime is byte-exact.
