# Collectives → Custom-Call Forward Conversion

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The passes live in the `neuronxcc/starfish/bin/hlo-opt` ELF. This binary is **not** mapped VA==fileoff: `.rodata` file offset = VA − 0x200000 and `.text` file offset = VA − 0x201000 (verified from the section headers). Every rodata string on this page was read with that delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

Three of `hlo-opt`'s HLO passes are where stock XLA collective ops stop being generic `HloInstruction`s and become **Neuron custom-calls**. The centerpiece is `xla::ConvertCollectivesToCustomCall` (pass key `convert-collectives-to-custom-call`), which rewrites exactly three native collective opcodes — `kAllReduce`, `kReduceScatter`, `kAllGather` — into XLA `kCustomCall` instructions whose `custom_call_target` string is built at runtime from a fixed naming contract: `AwsNeuron<Collective>[<ReduceOp>]<dtype>`. There is no `kCustomCall` for `kAllToAll` or `kCollectivePermute` here; both fall straight through the opcode gate and are lowered on their own paths ([4.8](collectivepermute-to-allgather.md)). The naming contract is the thing a reimplementer must reproduce byte-for-byte, because downstream legalization (Part 8) dispatches on these exact target strings.

The other two passes are fusion, not lowering, but they share the same file and the same collective family. `xla::FuseSendRecv` (key `fuse-send-recv`) pairs `Send`/`Recv` by `channel_id` — keyed on the **Done** opcodes `kSendDone`/`kRecvDone`, not the bare `Send`/`Recv` — and emits a fused `kCustomCall` carrying a `channel_id=<N>` backend-config plus an output tuple, hard-failing with `FailedPrecondition` if any half lacks a `channel_id`. `xla::FuseReduceScatter` (key `fuse-reduce-scatter`) is a 74-byte driver that ORs two helpers, `concatenationFusion` and `concatenationDividendFusion`, over the entry computation only; both detect N `reduce-scatter` ops feeding one `concatenate` and merge them into one wider `kReduceScatter` via `CreateReduceScatter`.

All three are **Neuron-authored passes** (`xla::` namespace, no upstream-XLA equivalent), reusing stock XLA instruction factories (`CreateCustomCall`, `CreateReduceScatter`, `CreateTuple`). The binary was archived with the decompiler disabled, so the reconstruction is from raw `objdump -d -M intel` on the live ELF, the IDA `context/*.md` callee sidecars, the `HloOpcodeString` opcode-enum anchor, and byte-exact rodata reads. The opcode gate, the target-name templates, the suffix tables, and the exclusion of `kAllToAll`/`kCollectivePermute` are all directly observed. Two things are not: the per-collective `backend_config` key format and the `FuseSendRecv` fused target literal, both of which are produced by deferred closures and runtime string operations rather than static constants.

For reimplementation, the contract is:

- The opcode gate: only `kAllReduce(7)`, `kReduceScatter(87)`, `kAllGather(4)` convert; everything else `continue`s. The instruction opcode is `byte [HloInstruction + 0x14]`.
- The target-name assembly: base template (16-byte `movups`) + optional reduce-op suffix (AllReduce/ReduceScatter only) + dtype suffix, producing strings like `AwsNeuronAllReduceAdd_bf16` and `AwsNeuronAllGather_f16`.
- The two fusion criteria: `Send`/`Recv` pairing by `channel_id`; N reduce-scatters under a `concatenate` (through opt-barrier/tuple plumbing) merged when they share replica-groups, reduction body, and scatter dimension.

| | |
|---|---|
| **Pass #11 — ConvertCollectivesToCustomCall** | `Run` @ `0x1e90dc0`; `name()` @ `0x1e90300` = `convert-collectives-to-custom-call` |
| **Pass #18 — FuseSendRecv** | `Run` @ `0x1ead8c0` (12,655 B); `name()` @ `0x1eace80` = `fuse-send-recv` |
| **Pass #28 — FuseReduceScatter** | `Run` @ `0x1eace30` (74 B); `name()` @ `0x1ea6720` = `fuse-reduce-scatter` |
| **Opcode field** | `byte ptr [HloInstruction + 0x14]` (verified at every match site) |
| **Handled opcodes** | `kAllReduce=7` · `kReduceScatter=87 (0x57)` · `kAllGather=4` |
| **NOT handled** | `kAllToAll=10 (0xa)` · `kCollectivePermute=29 (0x1d)` — no opcode path |
| **Target contract** | `AwsNeuron<Collective>[<ReduceOp>]<dtype>` (runtime-assembled) |
| **dtype switch** | `jmp QWORD PTR [rax*8+0x408920]` @ `0x1e9107b` (13-case) |

---

## ConvertCollectivesToCustomCall — native collective → `AwsNeuron*`

### Purpose

This pass is the forward direction of Neuron's collective custom-call boundary: it takes the three native collective opcodes XLA produces during SPMD partitioning and replaces each with a `kCustomCall` whose target string encodes the collective kind, the reduction op, and the element dtype. After this pass, the collective is opaque to generic XLA and is handled only by Neuron's own lowering (Part 8 `lower-local-collectives`). The reduce-op and dtype are folded **into the name** rather than carried as attributes, so the entire dispatch decision downstream is a string compare on `custom_call_target`.

### Entry Point

```text
xla::ConvertCollectivesToCustomCall::Run  @ 0x1e90dc0
  └─ HloModule::entry_computation             (rdx+0x38 first/entry computation ptr)
  └─ HloComputation::MakeInstructionPostOrder  @ 0x9634ab0   ── worklist
  └─ HloInstruction::called_computations       @ 0x964c8a0   ── reduce body (AllReduce/RS)
  └─ HloInstruction::replica_groups            @ 0x965e7e0   ── attr copy
  └─ xla::ReplicaGroup copy-ctor               @ 0x985c970
  └─ [deferred rewrite closures]               @ loop-exit 0x1e91e62 ── build CustomCall
```

### Algorithm

```c
// xla::ConvertCollectivesToCustomCall::Run @ 0x1e90dc0
StatusOr<bool> Run(HloModule *module, const flat_hash_set<string_view>& /*exec_threads*/):
    comp = module->entry_computation();         // rdx+0x38; if null -> return {false} (no change)
    if (comp == nullptr) return StatusOr<bool>{false};

    po = comp->MakeInstructionPostOrder();      // 0x9634ab0

    for (inst : po):
        op = inst[0x14];                        // HloInstruction opcode byte
        // ---- THE GATE: only three native collective opcodes convert ----
        switch (op):
            case 7:   base = "AwsNeuronAllReduce";    break;   // kAllReduce      @0x1e90ea9 cmp al,0x7
            case 0x57:base = "AwsNeuronReduceScatter";break;   // kReduceScatter  @0x1e90e68 cmp al,0x57
            case 4:   base = "AwsNeuronAllGather";     break;  // kAllGather      @0x1e90e70 cmp al,0x4
            default:  continue;                                // AllToAll/CollPermute/etc fall through @0x1e90e78

        // ---- reduce-op suffix (AllReduce & ReduceScatter only) ----
        if (base != "AwsNeuronAllGather"):
            // operand-type pre-guard @0x1e90faa/fbd requires both operand types == 0x4C
            cc = inst->called_computations();   // 0x964c8a0
            if (cc.size() == 1):                // AllGather has 0 -> skips this whole block
                rop = cc[0]->root_instruction()[0x14];
                switch (rop):
                    case 1:    name = base + "Add"; break;   // kAdd      0x238418
                    case 0x43: name = base + "Max"; break;   // kMaximum  0x230851
                    case 0x44: name = base + "Min"; break;   // kMinimum  0x224a9d
                    default:   name = base + "UNKNOWNOP";    // len>8 path 0x26a6e5
        else:
            name = base;                        // AllGather: no reduce-op

        // ---- dtype suffix (switch keyed on result Shape element_type) ----
         et = result_shape.element_type();      // Shape::array_state byte, shr for packed bit
        if (et == 0xB):  name += "_f32";        // F32 handled pre-switch @0x1e91fd0  (0x23fc8f)
        else:
            // sub eax,4 ; cmp eax,0xC ; jmp [rax*8+0x408920]   @0x1e9107b  (13-case)
            switch (et):
                case 16: name += "_bf16"; break;  // 0x210919
                case 10: name += "_f16";  break;  // 0x27e6e7
                case 8:  name += "_u32";  break;  // 0x224aa1
                case 5:  name += "_s64";  break;  // 0x24f472
                case 4:  name += "_s32";  break;  // 0x23fc94
                default: name += "_UNKNOWNDTYPE"; // 6,7,9,11-15  (0x25f0b5)

        // ---- capture into deferred rewrite closure ----
        groups = inst->replica_groups();        // 0x965e7e0; each ReplicaGroup copy-ctor'd, ids flattened
        // channel_id (shr rax,1 @0x1e9125a) + all_gather/scatter dimension captured here;
        // the actual CreateCustomCall is run by the closures at loop-exit, NOT inline.
        defer rewrite(inst, name, groups, channel_id, dimension);

    // loop exit @0x1e91e62: execute accumulated closures (vector stride 0x20,
    //   virtual call [entry+0x18] then call [entry] with edx=3)
    return StatusOr<bool>{ true };              // changed
```

> **QUIRK —** the reduce-op and dtype are part of the *name*, not attributes. `AwsNeuronAllReduceAdd_bf16` and `AwsNeuronAllReduceMax_f32` are different custom-call targets, not one target with two attribute sets. A reimplementer who emits a single `AwsNeuronAllReduce` target plus a `reduce_op`/`dtype` attribute will not match Neuron's downstream string dispatch.

> **QUIRK —** `AllGather` carries **no** reduce-op infix. Its `called_computations().size()` is 0 (it has no reduce body), so the size-1 guard fails and control jumps straight to the dtype suffix. The target is `AwsNeuron AllGather <dtype>` with nothing between. `AllReduce` and `ReduceScatter` always have exactly one called computation (the reduction body) and always take the infix.

### The naming contract

The target string is assembled in three pieces. The base is loaded as a 16-byte `movups` of a rodata template, then completed with a tail word/dword store (the template strings overlap into adjacent rodata, which is why a raw 24-byte read shows trailing garbage):

| Collective | Opcode | Base template (rodata) | Tail store | Base name |
|---|---|---|---|---|
| `kAllReduce` | 7 | `0x403d10` = `AwsNeuronAllRedu` | word `0x6563`=`ce` | `AwsNeuronAllReduce` |
| `kReduceScatter` | 87 (0x57) | `0x403d20` = `AwsNeuronReduceS` | dword `catt` + word `re` | `AwsNeuronReduceScatter` |
| `kAllGather` | 4 | `0x407e10` = `AwsNeuronAllGath` | word `0x7265`=`er` | `AwsNeuronAllGather` |

The reduce-op infix (AllReduce / ReduceScatter only) is selected by the reduction body's root opcode:

| reduce-root opcode | infix | rodata | Confidence |
|---|---|---|---|
| `kAdd` = 1 | `Add` | `0x238418` | CERTAIN |
| `kMaximum` = 67 (0x43) | `Max` | `0x230851` | CERTAIN |
| `kMinimum` = 68 (0x44) | `Min` | `0x224a9d` | CERTAIN |
| other (len>8 path) | `UNKNOWNOP` | `0x26a6e5` | CERTAIN |

The dtype suffix is keyed on the **result** Shape's element type (`F32` is special-cased before the switch; the rest go through the 13-case jump table at `0x408920`):

| PrimitiveType | value | suffix | rodata | Confidence |
|---|---|---|---|---|
| F32 | 11 (0xB) | `_f32` | `0x23fc8f` (pre-switch `0x1e91fd0`) | CERTAIN |
| BF16 | 16 | `_bf16` | `0x210919` | CERTAIN |
| F16 | 10 | `_f16` | `0x27e6e7` | CERTAIN |
| U32 | 8 | `_u32` | `0x224aa1` | CERTAIN |
| S64 | 5 | `_s64` | `0x24f472` | CERTAIN |
| S32 | 4 | `_s32` | `0x23fc94` | CERTAIN |
| other (6,7,9,11–15) | — | `_UNKNOWNDTYPE` | `0x25f0b5` | CERTAIN |

The product of these three tables is the full target-name space. Worked examples:

```text
AwsNeuronAllReduceAdd_bf16        ; all-reduce, sum, bf16
AwsNeuronAllReduceMax_f32         ; all-reduce, max, f32 (dtype via pre-switch path)
AwsNeuronReduceScatterMin_s32     ; reduce-scatter, min, s32
AwsNeuronReduceScatterAdd_u32     ; reduce-scatter, sum, u32
AwsNeuronAllGather_f16            ; all-gather, NO reduce-op, f16
AwsNeuronAllGather_bf16           ; all-gather, NO reduce-op, bf16
```

> **GOTCHA —** these target names are **runtime-assembled** (base `movups` template + suffix string concatenation), so they do **not** appear as whole `AwsNeuron…` literals in a static rodata string scan. A custom-call catalog built only from `^AwsNeuron…$` rodata strings will miss the entire collective family. The pieces (`AwsNeuronAllRedu`, `_bf16`, `Add`, …) are present; the assembled name is not.

### Attribute mapping

```c
// replica_groups: copied verbatim into the new custom-call
groups = inst->replica_groups();                 // 0x965e7e0
for (g : groups):
    g2 = ReplicaGroup(g);                         // copy-ctor 0x985c970
    for (id : g.replica_ids()):
        flat.push_back(id);                        // _M_realloc_insert<int> 0x1e907c0, loop 0x1e91288..0x1e914cc

// channel_id / collective-dimension: captured into the rewrite closure
// (shr rax,1 @0x1e9125a reads the optional channel_id; array_state count = dimension)
// carried in the new CC's backend_config — exact key format not isolated
```

The `replica_groups` are preserved exactly. The `channel_id`, `use_global_device_ids`, and the collective dimension (`all_gather_dim` for AllGather, `scatter_dim` for ReduceScatter) are captured into the per-collective deferred closure and carried into the custom-call's `backend_config`; there is **no inline `CreateCustomCall`** call site inside `Run` — the closures run at loop-exit — so the exact `backend_config` key form is not recovered [UNRESOLVED].

### Conversion table

| NativeOp (opcode) | CustomCall target | Attrs preserved | Confidence |
|---|---|---|---|
| `kAllReduce` (7) | `AwsNeuronAllReduce<Add\|Max\|Min><dtype>` | replica_groups (verbatim); reduce-op + dtype in name; channel_id/use_global_ids in closure | CERTAIN (name); MEDIUM (channel) |
| `kReduceScatter` (87) | `AwsNeuronReduceScatter<Add\|Max\|Min><dtype>` | replica_groups; reduce-op + dtype in name; scatter-dim in closure | CERTAIN (name); MEDIUM (dim) |
| `kAllGather` (4) | `AwsNeuronAllGather<dtype>` (**no** reduce-op) | replica_groups; dtype in name; all_gather_dim in closure | CERTAIN (name); MEDIUM (dim) |
| `kAllToAll` (10) | **NOT handled** — falls through gate | — | CERTAIN (no 0xa path) |
| `kCollectivePermute` (29) | **NOT handled** here | — | CERTAIN (no 0x1d path) |

> **NOTE —** `kCollectivePermute` is rewritten to `kAllGather` first by a separate pass (`NeuronCollectivePermuteToAllGather`, see [4.8](collectivepermute-to-allgather.md)); the resulting AllGather is then convertible *here*. So a collective-permute does eventually become an `AwsNeuronAllGather*` custom-call, but only after a prior rewrite — never directly in this pass.

### The exclusion, proved

The opcode gate was read straight off the disassembly. The matches are three `cmp al,<op>` against the opcode byte at `[inst+0x14]`:

```asm
1e90ea5: movzx  eax, BYTE PTR [rax+0x14]   ; opcode = inst+0x14
1e90ea9: cmp    al, 0x7                    ; kAllReduce      -> AwsNeuronAllReduce
1e90e68: cmp    al, 0x57                   ; kReduceScatter  -> AwsNeuronReduceScatter
1e90e70: cmp    al, 0x4                    ; kAllGather      -> AwsNeuronAllGather
1e90e78: ...                               ; else: advance to next inst (no rewrite)
```

A sweep of the entire `Run` body (`0x1e90dc0`–`0x1e92100`) for `cmp al,0xa` (AllToAll=10) and `cmp al,0x1d` (CollectivePermute=29) returns **no matches** — there is no code path that branches on either opcode. The exclusion is not an inference from absence of a name; it is the absence of the compare instruction itself.

---

## FuseSendRecv — pair Send/Recv into a fused custom-call

### Purpose

Point-to-point `Send`/`Recv` pairs (the building blocks of pipeline-parallel and explicit cross-replica transfers) are matched by `channel_id` and collapsed into a single fused `kCustomCall` that carries the channel id in its `backend_config` and returns a tuple. The pass exists so the downstream Neuron lowering sees one transfer op per channel instead of the four-instruction `Send`/`SendDone`/`Recv`/`RecvDone` quad.

### Entry Point

```text
xla::FuseSendRecv::Run  @ 0x1ead8c0   (12,655 B, 612 bb, 90 try-blocks)
  └─ HloComputation::MakeInstructionPostOrder
  └─ HloInstruction::channel_id              @ 0x965e9b0  ── optional<int64> pairing key
  └─ xla::FailedPrecondition                 @ 0x1eacef0  ── missing channel_id
  └─ HloInstruction::CreateCustomCall        @ 0x964eac0  ── 5 sites; fused op, api=1
  └─ HloInstruction::CreateTuple             @ 0x9665520  ── fused output packing
  └─ ReplaceAllUsesWithDifferentShape        @ 0x965fb40
  └─ BackendConfigWrapper::GetRawString      @ 0x1eaceb0  ── reuse original config
```

### Algorithm

```c
// xla::FuseSendRecv::Run @ 0x1ead8c0
StatusOr<bool> Run(HloModule *module, const flat_hash_set<string_view>&):
    comp = module->entry_computation();         // asserts nullptr != entry_computation_ (0x2108c9)
    po   = comp->MakeInstructionPostOrder();

    for (inst : po):
        op = inst[0x14];
        if (op == 0x67):                        // kSendDone=103  @0x1ead998 cmp al,0x67
            send = inst->mutable_operand(0);    // the producing Send
            cid  = send->channel_id();          // 0x965e9b0 -> optional<int64>; has_value -> var_598
            if (!cid.has_value())
                return FailedPrecondition("Send and Recv must have channel_id");  // 0x28a4f0
            sends[cid] = send;
        else if (op == 0x54):                   // kRecvDone=84   @0x1ead980 cmp al,0x54
            recv = inst->mutable_operand(0);
            cid  = recv->channel_id();
            if (!cid.has_value())
                return FailedPrecondition("Send and Recv must have channel_id");
            recvs[cid] = recv;

    for ((cid, send, recv) : pair_by_channel(sends, recvs)):
        cfg = "channel_id=" + decimal(cid);     // 0x24b747 + 2-digit table 0x4091a0 (div-by-100 magic)
        cc  = CreateCustomCall(fused_shape,     // 0x964eac0
                               operands,        // send data operand(s)
                               target_name,     // RUNTIME-built string (r14/[rbp-0x638]) - see GAP
                               cfg,             // backend_config
                               /*api=*/1);      // CustomCallApiVersion::API_VERSION_ORIGINAL
        out = CreateTuple({cc, ...});           // 0x9665520
        ReplaceAllUsesWithDifferentShape(...);  // 0x965fb40
        RemoveInstruction(old send/recv/done);  // 0x963dee0
    return StatusOr<bool>{ changed };
```

The pairing key is `channel_id`. The pass keys on the **Done** opcodes (`kSendDone=103`, `kRecvDone=84`), reaches back through `mutable_operand(0)` to the producing `Send`/`Recv`, and reads its `channel_id()`. The fused `backend_config` is the literal `"channel_id="` concatenated with the decimal channel id, rendered via the two-digit ASCII lookup table at `0x4091a0` (`"0001…99"`, with the `0x346dc5d63886594b` divide-by-100 magic). The custom-call is created at `CustomCallApiVersion = 1` (`API_VERSION_ORIGINAL`).

> **GOTCHA —** the match keys are `kSendDone` (103) / `kRecvDone` (84), **not** `kSend`/`kRecv` — the pass name suggests otherwise. A reimplementation that scans for bare `Send`/`Recv` will never fire: the Done node is the anchor, and the actual Send/Recv is reached one operand-hop back. Pairing is by `channel_id`, and a missing one on either half is a hard `FailedPrecondition("Send and Recv must have channel_id")` (`0x28a4f0`).

### Token-shape validators

The fused rewrite walks the token plumbing and asserts shape invariants. Each guard below is a verbatim rodata string:

| String | rodata | Guards |
|---|---|---|
| `Unexpected non-root tuple uses token` | `0x34bcb0` | token must flow only into the root tuple |
| `Unexpected non-CustomCall op returns token` | `0x3c22d8` | only a custom-call may produce a token |
| `Unexpected instruction type uses token` | `0x3f1448` | token consumer type check |
| `Cannot handle nested tuple shape` | `0x2e4770` | flat-tuple-only restriction |
| `nullptr != entry_computation_` | `0x2108c9` (`./xla/hlo/ir/hlo_module.h` `0x25f076`) | entry computation present |

### The fused target name

The fused custom-call's `custom_call_target` is **constructed at runtime** (string ops via `r14`/`[rbp-0x638]`), and an exhaustive rodata-reference sweep of the 12.6 KB function finds **no `AwsNeuron*` literal** anywhere — the only printable strings referenced are `"channel_id="` and `"Send and Recv must have channel_id"`. The name is therefore derived from the matched `Send`/`Recv` (most plausibly copied from their existing custom-call target via `BackendConfigWrapper::GetRawString`), not a fixed `AwsNeuronSendRecv`-style literal. The exact literal is [UNRESOLVED] — the string is not a static constant, and this page does not invent a name for it.

---

## FuseReduceScatter — merge N reduce-scatters under a concat

### Purpose

When SPMD partitioning produces several `reduce-scatter` ops that all feed a single `concatenate`, this pass merges them into one wider `kReduceScatter` and drops the concat. It is a structural fusion (concat-based), distinct from the key-based combiner `NeuronReduceScatterCombiner` ([4.5](collective-combiners.md)); the two are complementary, not duplicates.

### Entry Point

```text
xla::FuseReduceScatter::Run  @ 0x1eace30   (74 B — thin driver)
  ├─ HloModule::entry_computation              @ 0x1eaa310
  ├─ xla::concatenationFusion(comp)            @ 0x1eabf80   (ebx)
  └─ xla::concatenationDividendFusion(comp)    @ 0x1eaa6c0   (or ebx,eax)
       └─ both -> xla::fuseWithOperands         @ 0x1eaa460
            └─ HloInstruction::CreateReduceScatter @ 0x9668310
```

### Algorithm

```c
// xla::FuseReduceScatter::Run @ 0x1eace30 — full 74-byte body
StatusOr<bool> Run(HloModule *module, const flat_hash_set<string_view>&):
    comp    = module->entry_computation();                  // 0x1eaa310
    changed = xla::concatenationFusion(comp);               // 0x1eabf80 -> ebx
    changed = changed | xla::concatenationDividendFusion(comp); // 0x1eaa6c0 -> or ebx,eax
    return StatusOr<bool>{ status=OK, value=(bool)changed };// [r12]=0, [r12+8]=bl
```

Two independent helpers, ORed; both operate on the **entry computation only**.

```c
// xla::fuseWithOperands @ 0x1eaa460 — the shared rewriter
bool fuseWithOperands(HloComputation *comp,
                      vector<HloInstruction*> operands,
                      HloReduceScatterInstruction *base):
    groups = base->device_list().replica_groups();         // CollectiveDeviceList::replica_groups 0x96240f0
    body   = base->to_apply();                              // 0x9660060  (reduction computation)
    shp    = base->shape();                                 // 0x9650370
    nw     = CreateReduceScatter(shp, operands, body, groups,// 0x9668310
                                 constrain_layout,
                                 optional<channel_id>,
                                 use_global_device_ids,
                                 scatter_dimension);
    comp->AddInstruction(nw);                               // 0x96370d0
    comp->ReplaceInstruction(old, nw);                      // 0x963fe50
```

The new wide reduce-scatter inherits `replica_groups`, `to_apply` (reduction body), and `scatter_dimension` from `base`; its operands are the union of the fused inputs. The trailing `concatenate` is eliminated because the wide reduce-scatter already produces the concatenated shape.

### Fusion criteria

`concatenationFusion` (@ `0x1eabf80`, 3759 B) builds an `xla::match::` pattern (a 6-deep operand-impl chain) and walks the post-order. The opcode constants stamped into the pattern template are read directly at `0x1eabfd8+`: `0x22`=`kConcatenate(34)`, `0x48`=`kOptimizationBarrier(72)`, `0x78`=`kTuple(120)`; the leaf operand matcher tests opcode `87`=`kReduceScatter`. It recognises a `concatenate` (possibly reached through opt-barrier / tuple plumbing) whose operands are multiple `reduce-scatter`s, then calls `fuseWithOperands`.

`concatenationDividendFusion` (@ `0x1eaa6c0`, 6272 B) has the same skeleton but adds `MakeBinaryHlo` (`0x90f2760`), `MakeBroadcastHlo` (`0x90f0d50`), and `LiteralBase::IsAll(int8)` (`0x9773090`). It matches the mean idiom `reduce-scatter(divide(concat(...), broadcast(const)))`: it hoists the broadcast-constant divisor out of the per-operand divide, fuses the reduce-scatters via the same `fuseWithOperands`, and re-applies one division afterward. `IsAll(c)` verifies the divisor literal is a uniform constant.

| Helper | Detects | Requires | Result | Confidence |
|---|---|---|---|---|
| `concatenationFusion` | `concat(RS, RS, …)` (thru opt-barrier/tuple) | shared replica_groups + to_apply + scatter-dim | 1 wide `kReduceScatter` | HIGH |
| `concatenationDividendFusion` | `RS(div(concat(…), bcast(const)))` | divisor `IsAll` uniform const | fused `kReduceScatter` + re-applied divide | HIGH |

> **NOTE —** `FuseReduceScatter` (#28) is **not** the same machinery as the combiner `NeuronReduceScatterCombiner` (#78). #28 is concat-based structural fusion over the entry computation; #78 is a key-based combiner using the XLA combiner + `CombineKey`. They run independently and target different IR shapes.

---

## Reconstructed signatures

```cpp
// #11 — vtable 0x408ad0
class xla::ConvertCollectivesToCustomCall : public xla::HloModulePass {
  absl::string_view name() const;                                  // 0x1e90300 = "convert-collectives-to-custom-call"
  StatusOr<bool> Run(HloModule*, const flat_hash_set<string_view>&);// 0x1e90dc0
};
// #18 — vtable 0x4098a0
class xla::FuseSendRecv : public xla::HloModulePass {
  absl::string_view name() const;                                  // 0x1eace80 = "fuse-send-recv"
  StatusOr<bool> Run(HloModule*, const flat_hash_set<string_view>&);// 0x1ead8c0
};
// #28 — vtable 0x409818
class xla::FuseReduceScatter : public xla::HloModulePass {
  absl::string_view name() const;                                  // 0x1ea6720 = "fuse-reduce-scatter"
  StatusOr<bool> Run(HloModule*, const flat_hash_set<string_view>&);// 0x1eace30
};
bool xla::concatenationFusion(xla::HloComputation*);               // 0x1eabf80
bool xla::concatenationDividendFusion(xla::HloComputation*);       // 0x1eaa6c0
bool xla::fuseWithOperands(xla::HloComputation*, std::vector<HloInstruction*>,
                           xla::HloReduceScatterInstruction* /*base*/);   // 0x1eaa460
// reused stock-XLA factories:
HloInstruction::CreateCustomCall(Shape, Span<HloInstruction* const>, string_view target,
                                 std::string backend_config, CustomCallApiVersion);  // 0x964eac0 (api=1)
HloInstruction::CreateReduceScatter(Shape, Span<HloInstruction* const>, HloComputation*,
                                    Span<ReplicaGroup const>, bool constrain_layout,
                                    optional<int64> channel_id, bool use_global_device_ids,
                                    int64 scatter_dimension);       // 0x9668310
HloInstruction::CreateTuple(Span<HloInstruction* const>);          // 0x9665520
absl::Status xla::FailedPrecondition(...);                         // 0x1eacef0
```

---

## Evidence summary

| Claim | Anchor |
|---|---|
| Exactly three collectives convert | `Run` contains `cmp al,0x7` (`0x1e90ea9`), `cmp al,0x57` (`0x1e90e68`), `cmp al,0x4` (`0x1e90e70`), each branching to a distinct base-name builder; there is no fourth collective compare |
| AllToAll and CollectivePermute are excluded | sweeping the full `Run` body for `cmp al,0xa` and `cmp al,0x1d` returns zero hits — the exclusion is the absence of the compare instruction, not an inference from a missing name |
| Target-name templates and suffix tables | every base template (`AwsNeuronAllRedu` / `AwsNeuronReduceS` / `AwsNeuronAllGath`), every reduce-op (`Add`/`Max`/`Min`/`UNKNOWNOP`), and every dtype suffix (`_f32`/`_bf16`/`_f16`/`_u32`/`_s64`/`_s32`/`_UNKNOWNDTYPE`) read byte-exact from rodata at the cited VAs (delta −0x200000) |
| `FuseReduceScatter` is `concatenationFusion` OR `concatenationDividendFusion` | the whole 74-byte `Run` is `entry_computation()` → `call concatenationFusion` (`ebx`) → `call concatenationDividendFusion` → `or ebx,eax` → return; `fuseWithOperands` calls `CreateReduceScatter` (`0x9668310`) with the full mangled signature |
| `FuseSendRecv` keys on the Done opcodes and emits a custom-call + tuple | `cmp al,0x54` and `cmp al,0x67` present; `channel_id()`, `FailedPrecondition`, `CreateCustomCall` (string_view target + string backend_config + CustomCallApiVersion), and `CreateTuple` all called from the body |

## Limits of this reading

- The per-collective `backend_config` key format for `ConvertCollectivesToCustomCall` is not recovered: the rewrite runs through deferred closures with no inline `CreateCustomCall` call site.
- The `FuseSendRecv` fused target literal is not recovered: the name is assembled at runtime, and no `AwsNeuron*` constant is referenced anywhere in the 12.6 KB function.

---

## Related Components

| Pass | # | Relationship |
|---|---|---|
| `NeuronCollectivePermuteToAllGather` | 86 | rewrites `kCollectivePermute` → `kAllGather` so it becomes convertible here ([4.8](collectivepermute-to-allgather.md)) |
| `NeuronReduceScatterCombiner` | 78 | key-based RS combiner; complementary to #28's concat-based fusion ([4.5](collective-combiners.md)) |
| `lower-local-collectives` (Part 8) | — | consumes the `AwsNeuron*` custom-calls this pass emits |

## Cross-References

- [Collective-Permute Path](collectivepermute-to-allgather.md) — 4.8; where `kAllToAll`/`kCollectivePermute` are actually lowered (not here)
- [Collective Combiners](collective-combiners.md) — 4.5; the key-based RS/AR combiners that run alongside these fusions
- [SPMD Emission](../distribution/spmd-emission.md) — Part 13; produces the native collectives this pass converts
- [Lower-Local-Collectives](../walrus/lower-local-collectives.md) — Part 8; the consumer of the `AwsNeuron*` custom-call family
