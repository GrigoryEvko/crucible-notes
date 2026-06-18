# LoadActFuncSet / On-Chip LUT-Load

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The lowering and the ctrl-word packer live in `neuronxcc/starfish/lib/libwalrus.so`; the BIR op schema and the latency oracle live in `libBIR.so`. For `.text`/`.rodata` the virtual address equals the file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

The Activation engine (`EngineType 2`, external name "Scalar") computes a transcendental like `exp`, `sigmoid`, or `gelu` by reading a piecewise-polynomial lookup image — a `(bkt.bin, ctrl.bin)` table pair — out of its on-chip table memory ([10.4](bkt-ctrl-blob.md) decodes that image). Only **one** such image is resident at a time. Before a block of `InstActivation` ops can run, the compiler must make the right image resident; the instruction that does that is **`InstLoadActFuncSet`** (BIR instruction-type 6, "IT6"), and it lowers to a single 64-byte wire bundle with opcode **`0x23`** — silicon mnemonic `ActivationTableLoad`.

This is the most compact bundle in the entire Activation-engine ISA. It carries **exactly one** op-specific datum: an integer **set index** — `act_func_set_id` on the BIR-JSON wire, `act_tbl_sel` at the silicon-encoder layer — stored at instruction offset `+0xF0` and packed into bundle byte `+0x23`. There is **no** access pattern, **no** operand, and crucially **no LUT source address**: the `(bkt, ctrl)` images are engine/ROM-resident artifacts (pre-built offline, shipped in the wheel under `neuronxcc/pwp/`), and the index merely selects *which* resident image becomes active. The index is the **zero-based enumeration order** of the set in `act_info.json`'s `act_func_sets[]` array — resolved from the set's name through a `std::map<std::string, int>` built while parsing that JSON.

Two findings shape the rest of the page. First, the **packer is trivial but the selection is not**: `generateInstLoadActFuncSet` does a one-line map lookup and a store, but it is fed by a set-cover pass (`calculateBestSets`) that decides *which* sets to load and *when*, because a load installs a whole set and many consecutive activations can share it. Second, **the LUT-load has no cost model**: the `bir::Hwm` latency methods for `InstLoadActFuncSet` are `__assert_fail` stubs in this build, and no per-instruction `getLatencyExec` exists for it — so the scheduler amortises the refill structurally (fewest loads + prefetch hoist + global dedup), never charges a cycle number for it.

For reimplementation, the contract is:

- The **wire layout** of the `ActivationTableLoad` (0x23) bundle: which byte carries the set index, and the `fill_reg` guard that forbids a register-addressed fill.
- The **set-index encoding**: set-name → `std::map<string,int>` index → `inst+0xF0` → bundle `+0x23`, with the range check and the `0xFFFFFFFF` ("no active set") sentinel default.
- The **`dynamic_pwp` gate**: the `ModuleAttribute "neff_feature_dynamic_pwp"` and the NEFF feature bit `0x40` that advertise dynamic table loading to the runtime, versus the legacy static-ROM path.
- The **insertion pipeline** (set-cover + prefetch + dedup) that mints these ops, so a reimplementer knows a `LoadActFuncSet` is never authored by the front-end — it is synthesised by `lower_act`.
- The **no-cost-model finding**, so a reimplementer does not invent a latency the engine model does not have.

| | |
|---|---|
| **BIR op** | `bir::InstLoadActFuncSet` (IT6) |
| **Silicon opcode** | `0x23` — mnemonic `ActivationTableLoad` (gen-invariant) |
| **Pseudo-opcode** | `0xC6` — `PseudoLoadActFuncSet` (scheduling placeholder) |
| **Wire field** | `act_tbl_sel` (silicon) / `act_func_set_id` (BIR-JSON), `inst+0xF0` → bundle `+0x23` |
| **Default / sentinel** | `0xFFFFFFFF` (−1) = "no active set" |
| **Encoder** | `CoreV2GenImpl::visitInstLoadActFuncSet@0x1250b30` (gen-invariant; no V3/V4 override) |
| **Lowering / packer** | `LowerPWPImpl::generateInstLoadActFuncSet@0x1156510` |
| **Set-cover** | `LowerPWPImpl::calculateBestSets@0x11597e0` |
| **Insertion** | `LowerPWPImpl::addLoadInstructionBefore@0x1155620` |
| **Dynamic gate** | `NeffPackager::writeNEFFFeatures@0x15294b0` — bit `0x40`, `"neff_feature_dynamic_pwp"`@0x1529c18 |
| **Valid engine** | `EngineType 2` (Activation/"Scalar") only — `InstLoadActFuncSet::getValidEngines@0x43ef80` |
| **Latency** | **none** — `bir::Hwm` methods are `__assert_fail` stubs (`@0x4737c0/0x474c60/0x476100/0x4775a0`) |
| **Bundle** | 64 bytes, `inst_word_len = 0x10` (16 dwords) |

> **NOTE —** This page owns the **selection and addressing** half of the LUT story: how a set is named, indexed, packed, and made resident. The **content** half — what the `bkt.bin`/`ctrl.bin` bytes mean and how the engine evaluates a function inside the resident image — is [10.4](bkt-ctrl-blob.md). The function *catalog* and the 21/14-set roster are [Activation Function Catalog](act-function-catalog.md). The plain `Activation` (0x21) encoder that *uses* the loaded set is [2.11 Activation Encoding](../isa/activation-encoding.md). The engine datapath and an architectural view of the table memory is [1.10 Activation Engine](../arch/activation-engine.md).

---

## 1. The two-op contract: load installs, activation references

The Activation engine's function-table mechanism is a two-instruction contract, and both instructions key on the **same wire byte, `+0x23`**:

| op | IT | opcode | role | what `+0x23` holds |
|---|---|---|---|---|
| `LoadActFuncSet` | 6 | `0x23` | **installs** a func-set | the raw set index `act_tbl_sel` (no func-remap) |
| `Activation` | 4 | `0x21` | **references** a func *within* it | a func code via the 31-entry func-remap |

A `LoadActFuncSet` makes a named image resident; a subsequent `Activation` names one function *inside* that image by its silicon LUT code (`= pwp neuron_id`, e.g. `Exp→0x07`, `Sigmoid→0x05`, `Tanh→0x06`, via the func-remap at `0x1df57a0`). The Activation op carries **no** set id — `InstActivation::readFieldsFromJson@0x417f00` never touches the `"act_func_set_id"` key. The contract is therefore *implicit residency*: the engine resolves an Activation's func code against whatever set the most recent `LoadActFuncSet` made resident, and the `lower_act` pass (§4) is responsible for guaranteeing the correct set is resident before each Activation by inserting the minimal number of loads.

> **GOTCHA —** Do not expect the load to carry the table data, or the activation to carry the set. The two halves of "reference the loaded function" are split across two ops sharing one bundle byte: the **load** picks the image (raw index), the **activation** picks the function (remapped code). Neither carries a memory address — the images are ROM/engine-resident.

The same byte `+0x23` doubling as both fields is structurally exact and intentional: it is the single "activation table / function selector" slot in the 64-byte compute word ([2.11](../isa/activation-encoding.md) walks the Activation side; this page walks the Load side). [CONFIRMED — the two encoders both write `+0x23`; `InstActivation` reads no set-id key.]

---

## 2. The wire bundle and the encoder

The L3 wire encoder is `CoreV2GenImpl::visitInstLoadActFuncSet@0x1250b30` (797 bytes). It is **gen-invariant** — there is no `CoreV3`/`CoreV4` override, so gen4 reuses this body verbatim (consistent with the fact that only `Activate2` (0x25) has a gen4 override on the Activation engine).

### 2.1 The 64-byte bundle

Every Activation-engine bundle is 64 bytes built through the shared `CoreV2GenImpl::setupHeader@0x1172120`, which stamps `[opcode][0x10][0x00 0x00]` into bytes `0..3` (`inst_word_len = 0x10` = 16 dwords = 64 B). `LoadActFuncSet` fills exactly one more byte and zeroes the rest:

| off | field | source | conf |
|---|---|---|---|
| `0x00` | opcode `0x23` | `setupHeader(src=0x23)` | CONFIRMED |
| `0x01` | `inst_word_len 0x10` | `setupHeader` | CONFIRMED |
| `0x02..0x03` | reserved `0x0000` | `setupHeader` | CONFIRMED |
| `0x23` | **`act_tbl_sel`** (set index, range-checked byte) | `inst+0xF0` → range-checked setter `@0x124e710` | CONFIRMED |
| *all other 60 bytes* | `0x00` | — (no AP, no operand, no LUT address) | CONFIRMED |

### 2.2 Opcode proof and the `fill_reg` guard

Opcode `0x23` is stamped at all three codegen-mode arms (the backend has a dry-run / generate / alt-bin tri-mode dispatch on `this+0x270`):

```text
0x1250c18  mov dword [rbp+var_A0], 0x23   ; RUN_ISA_CHECKS arm: opcode → Rb_tree<u32> key
0x1250c8b  mov byte  [rbp+var_A0], 0x23   ; GENERATE_ISACODE arm: opcode → setupHeader src
0x1250dda  mov byte  [rbp+var_A1], 0x23   ; alt-bin arm:        opcode → setupHeader src
```

It is independently re-validated by the ISA validity checker `core_v2::dbg_is_valid_acttableld@0x128d650`, whose body does `cmp r11b, 0x23` on the opcode byte (two sites, `@0x128d77e` / `@0x128d7c8`). [CONFIRMED]

Before writing the selector, the encoder enforces a guard: `inst+0x110` (the `fill_reg` register-mode flag) **must be 0**, or it raises `boost::throw_exception<out_of_range>` (`@0x74b410`):

```text
0x1250cab  movzx eax, byte [r13+0x110]   ; r13 = the InstLoadActFuncSet
0x1250cb3  test  al, al
0x1250cb5  jnz   loc_74B410               ; → out_of_range
0x1250cbb  mov   eax, [r13+0xF0]          ; eax = act_func_set_id
```

> **QUIRK —** The load **cannot** be a register-addressed fill. The same `fill_reg@0x110` guard appears on `Dropout` (0x7F), but `LoadActFuncSet` has no register-fill semantics at all — the guard exists to reject a malformed IR that tried to make the set index register-relative. The static-int set id is the only valid form the *encoder* accepts (a loop-dependent symbolic id is handled earlier; see §3.3).

### 2.3 The range-checked setter

The selector is written by a shared range-checking setter (`sub_124E710`) that takes `(this, &bundle_byte, &error_state, inst, field_name)`, casts the integer at `inst+0xF0` to a byte, range-checks it, and on overflow emits `reportError("...check: 'instr.act_tbl_sel'")`. The two non-dry-run arms each call it with the field-name string `"instr.act_tbl_sel"` (`@0x1c8506b`):

```text
; GENERATE_ISACODE arm                       ; alt-bin arm
0x1250cbb mov   eax, [r13+0xF0]              0x1250e0a mov   eax, [r13+0xF0]
0x1250cc8 lea   rdi, [r12+0x23]   ; →bundle  0x1250e11 lea   rdi, [rbp+var_80+3] ; →stack bundle+0x23
0x1250ccd lea   rsi, "instr.act_tbl_sel"     0x1250e1b lea   rsi, "instr.act_tbl_sel"
0x1250cd7 mov   [rbp+var_60], eax            0x1250e22 mov   [rbp+var_60], eax
0x1250cd7 call  sub_124E710                  0x1250e25 call  sub_124E710
```

Both arms then `fwrite(bundle, 1, 0x40, sink)` — one 64-byte bundle (`@0x1250d07` generate / `@0x1250d8b` alt-bin). [CONFIRMED — full encoder body read; offsets and stamps verbatim.]

---

## 3. The set-index encoding (name → integer)

The datum at `inst+0xF0` is produced by `LowerPWPImpl::generateInstLoadActFuncSet@0x1156510` (the IDA `0x5f33c0` body is a PLT thunk; the real body is the high-VA one). The set is named by string; the wire wants an integer; the conversion is a single map lookup.

### 3.1 `AllActSetName2ActInfo` — the name→index dictionary

`fillAllActInfos@0x115af90` parses `act_info.json` (reading the rodata JSON keys `"act_func_sets"`, `"pwp_file_keys"`, per-set `"name"`) into a structure typed as:

```c
// llvm::MapVector<string, ActFuncSetInfo, std::map<string, int>>
//   the SmallVector<pair<string, ActFuncSetInfo>> preserves act_info.json array order
//   the std::map<string, int> IS the name→index dictionary
struct AllActSetName2ActInfo;
```

Because the `MapVector` preserves insertion order and insertion follows the JSON array, **each set's index equals its position in `act_func_sets[]`** — `exp_and_others = 0` … `derivative_gelu_apprx_sigmoid_and_others = 20` for the Trainium roster ([10.2 §3/§4](act-function-catalog.md) lists all 21 Trainium / 14 `with_ln` sets in array order).

### 3.2 The lookup and store

```c
// generateInstLoadActFuncSet @0x1156510 — the name→index→store core
int GlobalActFuncSetId =
    AllActSetName2ActInfo.map[ActSetName];     // std::map<string,int>::operator[] @0x1156881
assert(AllActSetName2ActInfo.count(ActSetName) > 0          // @rodata 0x1d4ff60
       && "the set NAME must exist");
assert((int)AllActSetName2ActInfo.size() > GlobalActFuncSetId   // @rodata 0x1d50040
       && "Invalid Activation function table ID");          // index in range

new_inst->act_func_set_id /* +0xF0 */ = GlobalActFuncSetId; // mov [rcx+0xF0], ebx
new_inst->fill_reg        /* +0x110 */ = 0;                 // mov byte [rax+0x110], 0
```

Byte-exact anchors:

```text
0x1156881  call _Map_base<string, pair<string const, int>>::operator[]   ; the dictionary
0x11568b6  mov  [rcx+0xF0], ebx        ; store the index into the new inst
0x1157ab6  mov  [rax+0xF0], ebx        ; twin store on the debug-info-present path
0x1157abc  mov  byte [rax+0x110], 0    ; fill_reg = 0
```

The map's value type is `int` (`std::map<string, pair<string const, int>>` per the demangled `operator[]` symbol), confirming the index is a plain signed integer, not a richer descriptor. [CONFIRMED — map symbol, store offset, and both asserts read verbatim.]

> **GOTCHA —** The minted load's name carries a `"-PWP"` suffix (rodata `@0x1c835c5`, `lea rsi, "-PWP"` `@0x1156643`) — these instructions show up in a BIR dump as `<…>-PWP`. That is just the synthesised name, not a separate field.

### 3.3 The static / symbolic split and the `0xFFFFFFFF` default

On the BIR-JSON side, the field round-trips through `act_func_set_id`:

- **Read:** `InstLoadActFuncSet::readFieldsFromJson@0x4155c0` reads JSON key `"act_func_set_id"` into `inst+0xF0` (`lea rdi, [r12+0xF0]` `@0x41571b`).
- **Write:** `toJson@0x435850` emits `"act_func_set_id"` **only if** the field ≠ `0xFFFFFFFF`:

  ```text
  0x43586a  cmp dword [rbx+0xF0], 0xFFFFFFFF   ; the "no active set" sentinel
  0x435876  lea rsi, "act_func_set_id"
  ```

The `0xFFFFFFFF` (−1) value is therefore the **struct default** of `+0xF0`: an unset load means "no active set," and it is the same sentinel the global-dedup pass (§4c) uses to track residency.

The set id can also be **loop-dependent**. A second flag at `inst+0x110` (the `fill_reg` byte the encoder requires to be 0) is reused at BIR-eval time as a static-vs-symbolic selector:

```c
// getActFuncSetIdEvalIfNeeded @0x402de0
long act_func_set_id(const DenseMap<AffineIdx*, long>& idxMap) const {
    if (flag@0x110 == 0)  return id@0xF0;                 // static int
    else                  return QuasiAffineExpr::eval(&id@0xF0, idxMap); // symbolic
}
```

```text
0x402de0  movzx eax, byte [rdi+0x110]
0x402de7  test  al, al
0x402de9  jnz   loc_402DF8           ; symbolic path
0x402deb  mov   eax, [rdi+0xF0]      ; static path: return the int
0x402e0b  call  QuasiAffineExpr::eval ; symbolic path: eval the affine expr
```

[CONFIRMED — read/write/eval bodies all anchored.]

> **CORRECTION —** D-J13 named only the silicon wire field `act_tbl_sel`; D-AG07 named the BIR-text form `act_func_set_id`. They are the **same datum at `+0xF0`**, two layers: BIR-JSON key `act_func_set_id`, silicon field `act_tbl_sel`. The encoder reads `+0xF0` and packs it into bundle `+0x23` with the wire name `instr.act_tbl_sel`.

---

## 4. Who inserts the load: set-cover + prefetch + dedup

A `LoadActFuncSet` is **never authored by the front-end** — the asserts `"InstLoadActFuncSet should not exist before lower activate"` (`@0x1d4f580`) and `"Module has no Activation Function Tables set up for this InstLoadActFuncSet"` guard exactly that. The ops are *minted* by the `lower_act` lowering, then optimised by two follow-on passes. Together they form the LUT-residency pipeline:

**(a) `lower_act` — local minimal cover.** `LowerPWPImpl::visitInstruction@0x1151110` scans the IR for `InstActivation` users (dispatching on the opcode at `inst+0x58`, reading the `ActivationFunctionType` at `inst+0x90`). `calculateBestSets@0x11597e0` then runs a **set-cover** over the LUT-driven activation functions (the cheap-but-not-hardwired members of the 31-entry `ActivationFunctionType` enum), picking the fewest resident sets so set-sharing activations don't each reload — and `reportError("Invalid activation function found in instruction")` (`@0x1d4ff90`) if a function belongs to no shipped set. `addLoadInstructionBefore@0x1155620` → `generateInstLoadActFuncSet` (§3) mints one IT6 per chunk, stamps the chosen index, and copies the triggering func onto the load.

**(b) `optimize_prefetch_act_control` — latency-hiding hoist.** `OptimizePrefetchActLoadImpl::visitInstruction@0x11653f0` hoists each load **as early as possible** to hide the (un-modelled, but physically real) table refill behind upstream compute. It bounds the index per Activation engine:

```text
assert("Eng2UsedActTables->count(actTblLoad->getEngineInfo()) > 0 &&
        (int)Eng2UsedActTables->lookup(actTblLoad->getEngineInfo()).size() > actFuncSetId")
```

(`Eng2UsedActTables` = `DenseMap<EngineInfo, vector<…>>`; the set index is bounded against the per-engine set count.)

**(c) `optimize_act_control` — global dedup.** `OptimizeActControlImpl::enterBasicBlock@0x11618a0` scans IT6 ops (`inst+0x50 == 0x6`), tracks the **resident set** with the `0xFFFFFFFF` ("no active set") sentinel, and **eliminates** any load whose set a dominating earlier load already made resident (global redundant-reload elimination). The sentinel here *is* the `toJson` default of §3.3.

> **NOTE —** The trio is *local minimal cover* (a) + *prefetch hoist* (b) + *global dedup* (c). The whole point of the cover is amortisation: a load installs an entire set (its hero function plus a fixed tail of cheap co-resident 1-piece funcs, [10.4 §1](bkt-ctrl-blob.md)), so the refill happens once per resident-set epoch, not once per activation.

Two structural budget asserts in `generateInstLoadActFuncSet` cap the residency model:

- `"the number of activation tables must be <= 8"` — **at most 8 resident func-sets** can be referenced per engine. This is distinct from the `act_func_set_id` index range (0..20): the index numbers the *shipped catalog*, the `≤ 8` caps how many a single kernel keeps live.
- `"Engine2UsedActSetNames.size() <= 1"` (and the CoreV4 variant `== 0`) — one func-set *family* per engine.

[STRONG — pass structure, symbols, and all asserts anchored. The precise cover heuristic inside `calculateBestSets` is byte-traced in [10.7 (set-cover)](set-cover.md): it is a **greedy first-fit** single forward sweep, *not* an optimal power-set minimiser — so "minimal number of loads" / "minimal cover" here means greedily-fewest by first-fit, not provably-optimal.]

---

## 5. The `dynamic_pwp` gate

Whether the compiler emits these dynamic table loads at all is gated by a NEFF feature. `NeffPackager::writeNEFFFeatures@0x15294b0` walks a series of `Module::getAttribute(ModuleAttribute)` tests, each `or`-ing a bit into a 64-bit feature mask accumulated in `r14`. The `dynamic_pwp` branch:

```text
0x1529c18  lea rsi, "neff_feature_dynamic_pwp"   ; the attribute name
0x1529c5d  or  r14, 0x40                          ; feature bit 0x40
```

So `dynamic_pwp` is **NEFF feature bit `0x40`**, written into the container's `neff_features` array. It is keyed off the registered `bir::ModuleAttribute "neff_feature_dynamic_pwp"` (interned in `libBIR`'s `ModuleAttribute2string` pool; the string is also present in `libBIR` rodata). When the front-end/HLO lowering sets that module attribute, the dynamic-PWP path is active: LUT sets are loaded dynamically (the inserted `ActivationTableLoad` ops of §4), and the NEFF advertises the capability so the **runtime expects dynamic table loads** rather than the legacy static-ROM path.

A separate global toggle, `enableDynamicActTable` (`.bss`, with the literal `"DynamicActTable"` `@0x1c83025` referenced in the queue-allocation layer), selects static-vs-dynamic act-table *mode* at codegen time:

- **Static mode** — a single pre-loaded ROM set; no per-op `LoadActFuncSet`.
- **Dynamic mode** — the set-cover path of §4 emits `ActivationTableLoad` ops and sets the `neff_feature_dynamic_pwp` module attribute.

[CONFIRMED for the bit/attribute (anchored byte-exact); STRONG for the `enableDynamicActTable` role; SPECULATIVE for the precise static↔dynamic decision rule — the upstream HLO pass that *sets* the module attribute is not in these binaries.]

---

## 6. The pseudo-opcode `0xC6`

The silicon ISA-name tables (`core_vN::enum_variant_string_opcode`, the 256-entry jump tables at `0x127aea0` / `0x1369a40` / `0x143fd80`) name opcode `0x23` `ActivationTableLoad` — identical across `core_v2`/`v3`/`v4`, so the load is gen-invariant. They also name a **separate** opcode `0xC6` = `PseudoLoadActFuncSet` (both rodata strings present: `"PSEUDO_LOAD_ACT_FUNC_SET"`@0x1c84608 and `"PseudoLoadActFuncSet"`@0x1c85cf5).

| opcode | mnemonic | realises |
|---|---|---|
| `0x21` | `Activate` | `InstActivation` (IT4, plain) |
| `0x23` | **`ActivationTableLoad`** | **`InstLoadActFuncSet` (IT6)** |
| `0x24` | `ActivationReadAccumulator` | the IT5/IT101 accumulator drain |
| `0x25` | `Activate2` | `InstActivation` (gen4-only override) |
| `0xC6` | `PseudoLoadActFuncSet` | scheduling pseudo (this section) |

The real load is emitted as `0x23`; the `0xC6` pseudo is the placeholder the scheduling/prefetch passes (§4b/c) manipulate — a pseudo so the resident-set bookkeeping survives hoisting and dedup without consuming a real engine cycle, later materialised to `0x23`. [STRONG for the pseudo's existence and ISA-name; INFERRED for the exact `0xC6 → 0x23` materialisation point.]

---

## 7. No cost model: the LUT-load is amortised, never timed

The single most counter-intuitive property of `LoadActFuncSet` is that the build has **no latency for it**. The `bir::Hwm` latency oracle declares four methods for the op, and all four are `__assert_fail` stubs:

| symbol | addr | body |
|---|---|---|
| `Hwm::getLatency(InstLoadActFuncSet)` | `0x4737c0` | `__assert_fail("false && \"getLatency( const InstLoadActFuncSet & ) not implemented\"")` |
| `Hwm::getLatencyReadInit(InstLoadActFuncSet, EngineType)` | `0x474c60` | `__assert_fail` stub |
| `Hwm::getLatencyExec(InstLoadActFuncSet, EngineType)` | `0x476100` | `__assert_fail("false && \"getLatencyExec( const InstLoadActFuncSet &, bir::EngineType ) not implemented\"")`, line `0x326` |
| `Hwm::getLatencyWriteDrain(InstLoadActFuncSet, EngineType)` | `0x4775a0` | `__assert_fail` stub |

Each body is 35 bytes (`sub rsp,8; lea rcx/rsi/rdi <strings>; call __assert_fail`) — i.e. a guaranteed abort if anyone asks for the typed latency. There is **no** per-instruction override either: `InstLoadActFuncSet` has no own `getLatencyExec`. The contrast is decisive — `InstActivation::getLatencyExec@0x3ea570` is a **real 459-byte cost model**, and `InstReadActivationAccumulator::getLatencyExec` is real too, so the Activation engine *is* modelled; only the table load is not.

```text
; getLatencyExec(InstLoadActFuncSet, EngineType) @0x476100 — the whole body
0x476104  lea rcx, "virtual int64_t bir::Hwm::getLatencyExec(...)"
0x47610b  mov edx, 0x326                ; assert line
0x476110  lea rsi, "/opt/workspace/KaenaCompilerNativeBuild-310/..."
0x476117  lea rdi, "false && \"getLatencyExec( const InstLoadActFuncSet ... not implemented\""
0x47611e  call __assert_fail
```

The consequence for a reimplementer: the perf-sim / time-aware scheduler **must not** call the typed latency for this op (it would abort) — it treats the load via a generic default, effectively a fixed/near-zero slot. The refill cost is paid **structurally** — by the §4 trio that minimises the number of loads and hoists each one early — not by a cycle number charged per instruction.

> **CORRECTION —** Earlier strand notes (D-AG07 §6) listed `getLatencyReadInit / getLatencyExec / getLatencyWriteDrain` as if they "model the load cost." They exist as *symbols*, but their *bodies* are `__assert_fail` stubs in this build — the cost is not modelled. The structural amortisation (fewest loads + prefetch + dedup) is the real cost-management mechanism, confirmed by the absence of any non-stub latency.

---

## 8. End-to-end: from a `sigmoid` activation to a resident set

Putting the pieces together, the lifecycle of a single `InstActivation(func = Sigmoid)` reaching the Activation engine:

1. **`lower_act` scan** — `visitInstruction@0x1151110` records that this kernel chunk needs `Sigmoid` (and whatever else); `calculateBestSets@0x11597e0` finds the shipped set whose `"act"` co-residency dict contains `Sigmoid` (e.g. `sigmoid_and_others`, index 2 in the Trainium roster) and that covers the chunk's other funcs.
2. **Mint the load** — `addLoadInstructionBefore@0x1155620` inserts an `InstLoadActFuncSet` ahead of the chunk; `generateInstLoadActFuncSet@0x1156510` resolves `"sigmoid_and_others"` → index via the `std::map<string,int>`, stores it to `inst+0xF0`, zeroes `fill_reg`.
3. **Hoist + dedup** — the prefetch pass moves the load earlier; the dedup pass drops it if a dominating load already made `sigmoid_and_others` resident.
4. **Encode the load** — `CoreV2GenImpl::visitInstLoadActFuncSet@0x1250b30` emits opcode `0x23`, packs the index into bundle byte `+0x23` via the range-checked `instr.act_tbl_sel` setter.
5. **Encode the activation** — the later `InstActivation` (opcode `0x21`) puts `Sigmoid`'s func code (`0x05`, the func-remap value) into the *same* byte `+0x23`. It carries no set id.
6. **Run** — the engine loads the `sigmoid_and_others` `(bkt, ctrl)` image into its single resident LUT bank (the `0x23` bundle's effect), then resolves func code `0x05` against that image and evaluates the piecewise-polynomial ([10.4 §2/§7](bkt-ctrl-blob.md)).

The `(set index in the load) + (func code in the activation) + (implicit residency)` triple is the complete "select and address a function on the Activation engine" mechanism. The set-cover ([10.7](set-cover.md)) is the optimisation that makes the load count minimal.

---

## 9. Confidence ledger

| claim | tag | anchor |
|---|---|---|
| Encoder `CoreV2GenImpl::visitInstLoadActFuncSet@0x1250b30`, gen-invariant (no V3/V4 override) | CONFIRMED | functions.json; no override symbol |
| Opcode `0x23`, three stamps + `dbg_is_valid_acttableld cmp r11b,0x23` | CONFIRMED | `0x1250c18/c8b/dda`; `0x128d77e/7c8` |
| Selector `inst+0xF0` → bundle `+0x23` via range-checked `instr.act_tbl_sel` setter | CONFIRMED | `0x1250cbb→cc8→cd7`; `0x1250e0a→e11→e25` |
| `fill_reg@0x110` guard must be 0, else `out_of_range@0x74b410` | CONFIRMED | `0x1250cab/cb5`, `0x1250dfa/e04` |
| Set index = `std::map<string,int>` position = `act_info.json` array order | CONFIRMED | `operator[]@0x1156881`; `mov [rcx+0xF0],ebx@0x11568b6` |
| Asserts: name must exist; index in range; `≤8` tables; one family/engine | CONFIRMED | rodata `0x1d4ff60/0x1d50040`; "≤ 8" / `Engine2UsedActSetNames` |
| BIR-JSON key `act_func_set_id`; default `0xFFFFFFFF` (−1); read `@0x4155c0`, write `@0x435850` | CONFIRMED | `cmp [rbx+0xF0],0xFFFFFFFF@0x43586a`; `lea [r12+0xF0]@0x41571b` |
| Static/symbolic split via flag `@0x110`; symbolic = `QuasiAffineExpr::eval` | CONFIRMED | `getActFuncSetIdEvalIfNeeded@0x402de0` |
| Insertion = `lower_act` set-cover + prefetch hoist + global dedup | STRONG | `0x11597e0 / 0x11653f0 / 0x11618a0` + asserts |
| `dynamic_pwp` = `ModuleAttribute "neff_feature_dynamic_pwp"` + NEFF bit `0x40` | CONFIRMED | `0x1529c18` (string) + `0x1529c5d` (`or r14,0x40`) |
| `enableDynamicActTable` global toggles static-vs-dynamic mode | STRONG | `"DynamicActTable"@0x1c83025` |
| Silicon names: `0x23 ActivationTableLoad`, `0xC6 PseudoLoadActFuncSet` | CONFIRMED | `enum_variant_string_opcode@0x127aea0/0x143fd80`; rodata strings |
| **No cost model** — all 4 `Hwm` latency methods are `__assert_fail` stubs; no per-inst override | CONFIRMED | `0x4737c0/0x474c60/0x476100/0x4775a0` (35 B each); `InstActivation::getLatencyExec@0x3ea570` is real (459 B) |
| Valid engine = Activation (EngineType 2) only | CONFIRMED | `InstLoadActFuncSet::getValidEngines@0x43ef80` |
| `0xC6 → 0x23` materialisation point | INFERRED | pseudo string present; exact lowering not byte-traced |
| Precise static↔dynamic decision rule / upstream attribute setter | SPECULATIVE | HLO/front-end, not in these binaries |

> **Provenance.** Every address and string on this page was re-verified against the cp310 `libwalrus.so` / `libBIR.so` IDA sidecars (`*_functions.json`, `disasm/*.asm`, `*_rodata.bin`). The `(bkt, ctrl)` blob layout these loads select is [10.4](bkt-ctrl-blob.md); the function catalog and set roster are [Activation Function Catalog](act-function-catalog.md); the Activation (0x21) encoder that consumes the resident set is [2.11](../isa/activation-encoding.md); the engine datapath view is [1.10](../arch/activation-engine.md). The minimal-cover heuristic gets its own page ([10.7 (set-cover)](set-cover.md)).

*Sources: D-M07 (LoadActFuncSet / LUT-load mechanism), D-AG07 (PWP bkt/ctrl blob + ctrl-word packer), D-J13 (CoreV2/V4 RNG + activation-engine wire encoders).*
